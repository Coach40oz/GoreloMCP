"""The custom_assets area of the live write matrix (--with-custom-assets), run offline.

Two layers:

* Harness level (always runs): the area is driven against a scripted tool session (AssetSession) that sits in front of the
  REAL LiveGuard (write mode, require_intents=True, allow_custom_assets, and allow_custom_asset_move for the move) and a real
  manifest. Nothing here depends on the custom asset tools themselves: the session answers what those tools answer (a row, or
  {Id, warning}) and turns each call into the requests the tool would send (contract a7954b24844b: the read-back is a GET by id, a
  call that names a client reads the asset first, a different client MOVES it), so the guard judges the real bodies.
* End to end (runs once the four custom asset tools are registered): run_matrix against FakeGorelo extended with the custom asset
  routes, with the real tools, the real guard and the real cleanup.

A custom asset is not deleted by the harness: the cleanup must send nothing for it and report it as a known residue.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from conftest import MockGorelo, envelope, error_envelope, paged_envelope
from site_helper import CLIENT_STATUS, site_config_env  # noqa: F401  (autouse: points GORELO_SITE_CONFIG at invented values)
from test_live_write_matrix import (  # noqa: F401  (fixtures and helpers of the matrix tests)
    GOLDEN,
    GOLDEN_ASSET_MOVE,
    GOLDEN_CUSTOM_ASSETS,
    RUN,
    RUN_START,
    SECOND,
    TEST_CLIENT,
    FakeGorelo,
    _never_read_the_real_env,
    area_ops,
    lines,
    results,
    run,
)

from scripts.live import cleanup, write_matrix
from scripts.live.guard import GuardViolation, LiveGuard
from scripts.live.manifest import Manifest
from scripts.live.smoke import GuardTripped, ToolFailed
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio

BASE = "https://api.usw.gorelo.io/v1"
LOCATION, HARDWARE = 9001, 42
TOOLS_REGISTERED = {"list_custom_asset_hardware_types", "create_custom_asset", "update_custom_asset", "get_custom_asset"} <= {
    spec.name for spec in REGISTRY.specs
}
needs_tools = pytest.mark.skipif(not TOOLS_REGISTERED, reason="the custom asset tools are not registered yet")
PASCAL = {
    "name": "Name", "client_id": "ClientId", "location_id": "LocationId", "hardware_type_id": "HardwareTypeId",
    "description": "Description", "contact_id": "ContactId", "warranty_end_date": "WarrantyEndDate",
    "ip_address": "IpAddress", "serial_number": "SerialNumber",
}


# --------------------------------------------------------------------------
# Harness level: a scripted session in front of the real guard and manifest
# --------------------------------------------------------------------------

SECOND_LOCATION = 9002  # the default location of the second client in the scripted session


class AssetSession:
    """Stands in for ToolSession. Each tool call becomes the requests the real tool sends (contract a7954b24844b), checked by the real
    guard:

    * get_custom_asset: a GET of the asset;
    * create_custom_asset: the POST, then the read-back GET by id;
    * update_custom_asset without client_id: the PATCH, then the read-back GET; WITH client_id: a GET first (the pre-read), then the
      PATCH and the read-back. A client_id that is not the asset's client MOVES it: without a location_id the tool refuses locally
      after its GET (no HTTP status, nothing else sent), and after the PATCH it checks that the read-back shows the new client.

    out / back: how Gorelo answers the move to the second client and the way back to the test client: "ok" (applied), "refuse"
    (HTTP 400, nothing applied), "server" (HTTP 500), "timeout", "unconfirmed" (HTTP 400 text saying the write may have applied)
    and "ignored" (a success that moved nothing: the read-back shows the old client, which the tool turns into a write-unconfirmed
    error). applied_on_error: whether a "server", "timeout" or "unconfirmed" answer was applied all the same.
    """

    def __init__(self, guard, pacer, *, allowed: frozenset[str], hardware=None, out="ok", back="ok", second_locations=None):
        self.guard, self.pacer, self.allowed = guard, pacer, allowed
        self.hardware = [{"Id": HARDWARE, "Name": "Laptop"}, {"Id": 43, "Name": "Server"}] if hardware is None else hardware
        self.second_locations = (
            [{"Id": SECOND_LOCATION, "ClientId": SECOND, "IsDefault": True}] if second_locations is None else second_locations
        )
        self.out, self.back, self.applied_on_error = out, back, False
        self.calls: list[tuple[str, dict]] = []
        self.bodies: list[tuple[str, dict]] = []
        self.requests: list[str] = []  # "METHOD path" of every request the guard let through, in order
        self.details_at_patch: list[Any] = []  # the client_id of the asset's manifest details when each PATCH was sent
        self.manifest = None  # set by matrix_for
        self.asset: dict | None = None
        self.break_create: str | None = None  # "reject", "warning"
        self.mangle: dict = {}  # field -> value the read-back shows instead (after a create or an update)

    def _send(self, method: str, path: str, body: dict | None = None) -> None:
        request = httpx.Request(method, BASE + path, json=body)
        before = len(self.guard.violations)
        try:
            self.guard.check(request)
        except GuardViolation as exc:
            assert len(self.guard.violations) > before
            raise GuardTripped(str(exc)) from None
        self.pacer.requests += 1
        self.requests.append(f"{method} {path}")
        if method != "GET":
            self.bodies.append((f"{method} {path}", body or {}))
            self.details_at_patch.append(self.manifest.details("custom_asset", self.asset["Id"]).get("client_id") if self.asset else None)

    def _row(self) -> dict:
        row = dict(self.asset)
        row.update(self.mangle)
        return row

    async def call(self, name: str, arguments=None) -> dict:
        arguments = dict(arguments or {})
        if name not in self.allowed:
            raise RuntimeError(f"{name} is not one of the tools this script may call")
        self.calls.append((name, dict(arguments)))  # a copy: the scripted tool pops the id off its own
        if name == "list_custom_asset_hardware_types":
            self.pacer.requests += 1
            return {"items": self.hardware, "count": len(self.hardware)}
        if name == "list_client_locations":
            self.pacer.requests += 1
            rows = self.second_locations if arguments["client_id"] == SECOND else [{"Id": LOCATION, "ClientId": TEST_CLIENT, "IsDefault": True}]
            return {"items": rows, "count": len(rows)}
        if name == "create_custom_asset":
            return self._create(name, arguments)
        if name == "get_custom_asset":
            self._send("GET", f"/assets/custom/{arguments['custom_asset_id']}")
            return self._row()
        if name == "update_custom_asset":
            return self._update(name, arguments)
        raise AssertionError(f"unexpected tool {name}")

    def _create(self, name: str, arguments: dict) -> dict:
        body = {PASCAL[key]: value for key, value in arguments.items() if value is not None}
        self._send("POST", "/assets/custom", body)
        if self.break_create == "reject":
            raise ToolFailed(name, "Gorelo rejected create_custom_asset (HTTP 400, code 070101): Name is required", sent=True)
        self.asset = {
            "Id": "00000001-aaaa-4bbb-8ccc-000000000001", "Description": "", "ContactId": None, "IpAddress": None,
            "SerialNumber": None, "WarrantyEndDate": None, **body,
        }
        self._send("GET", f"/assets/custom/{self.asset['Id']}")  # the read-back by id
        if self.break_create == "warning":
            return {"Id": self.asset["Id"], "warning": "the write succeeded; re-reading it failed: HTTP 500"}
        return self._row()

    def _update(self, name: str, arguments: dict) -> dict:
        ident = arguments.pop("custom_asset_id")
        clear = arguments.pop("clear_fields", None) or []
        client = arguments.get("client_id")  # None: the tool was not told a client (nothing can move)
        body = {PASCAL[key]: value for key, value in arguments.items() if value is not None}
        body.update({PASCAL[key]: None for key in clear})
        moving = client is not None and client != self.asset["ClientId"]
        if client is not None:
            self._send("GET", f"/assets/custom/{ident}")  # the pre-read of a call that names a client
            if moving and arguments.get("location_id") is None:
                raise ToolFailed(
                    name,
                    f"location_id: required when client_id moves the asset (it is at client {self.asset['ClientId']} now); give a "
                    f"location of client {client} (list_client_locations). Nothing was changed.",
                    sent=True,
                )
        self._send("PATCH", f"/assets/custom/{ident}", body)
        mode = ("ok" if not moving else self.out if client == SECOND else self.back)
        if mode in ("refuse", "server", "timeout", "unconfirmed"):
            if mode != "refuse" and self.applied_on_error:
                self._apply(body)
            text = {
                "refuse": "Gorelo rejected update_custom_asset (HTTP 400, code 070102): ClientId: wrong client",
                "server": "Gorelo failed update_custom_asset (HTTP 500, code 1)",
                "timeout": "Gorelo did not answer update_custom_asset in time; it may have applied the write",
                "unconfirmed": "Gorelo did not confirm update_custom_asset (HTTP 400): the write may have applied",
            }[mode]
            raise ToolFailed(name, text, sent=True)
        if mode != "ignored":
            self._apply(body)
        self._send("GET", f"/assets/custom/{ident}")  # the read-back by id
        row = self._row()
        if moving and row.get("ClientId") != client:
            raise ToolFailed(
                name,
                "Gorelo returned an unexpected response for update_custom_asset: PATCH /v1/assets/custom/{customAssetId}: Gorelo "
                f"reported success but the asset reads back at client {row.get('ClientId')}, not at client {client}; the move may or "
                "may not have been applied, so verify it with a read before repeating it",
                sent=True,
            )
        return row

    def _apply(self, body: dict) -> None:
        for key, value in body.items():
            self.asset[key] = "" if (key == "Description" and value is None) else value


def matrix_for(tmp_path, *, with_custom_assets=True, with_asset_move=False, second_client_active=True, session_class=AssetSession, **session_options):
    manifest = Manifest(RUN, tmp_path / "run.json")
    pacer = SimpleNamespace(requests=0)
    guard = LiveGuard(
        "write", manifest, require_intents=True, allow_custom_assets=with_custom_assets,
        allow_custom_asset_move=with_asset_move and with_custom_assets,
    )
    session = session_class(
        guard, pacer, allowed=write_matrix.tools_for(False, False, False, with_custom_assets), **session_options
    )
    session.manifest = manifest
    matrix = write_matrix.Matrix(
        manifest=manifest, guard=guard, pacer=pacer, session=session, secret="sekrit-key-1",
        clock=lambda: datetime(2099, 10, 2, 10, 15, 0, tzinfo=timezone.utc), lookup_wait=0, scopes_missing=set(),
        with_custom_assets=with_custom_assets, with_asset_move=with_asset_move, second_client_active=second_client_active,
    )
    return matrix, session, manifest


HAPPY_CALLS = [
    "list_custom_asset_hardware_types", "list_client_locations", "create_custom_asset", "get_custom_asset",
    "update_custom_asset", "update_custom_asset", "update_custom_asset",
]


async def test_the_happy_path_creates_reads_updates_clears_and_has_the_tool_refuse_a_move_without_a_location(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)
    await write_matrix.area_custom_assets(matrix)
    assert [name for name, _ in session.calls] == HAPPY_CALLS
    created = dict(session.calls)["create_custom_asset"]
    assert created == {
        "name": f"{RUN}-asset", "client_id": TEST_CLIENT, "location_id": LOCATION, "hardware_type_id": HARDWARE,
        "serial_number": f"{RUN}-sn", "ip_address": "192.0.2.10", "warranty_end_date": "2100-10-02",
    }
    updates = [arguments for name, arguments in session.calls if name == "update_custom_asset"]
    assert updates[0] == {  # the first update names no client at all: client_id is optional now
        "custom_asset_id": session.asset["Id"], "name": f"{RUN}-asset v2", "ip_address": "192.0.2.11", "description": f"{RUN}-asset: updated",
    }
    assert updates[1] == {  # the second names the asset's CURRENT client: the tool reads the asset first and sees no move
        "custom_asset_id": session.asset["Id"], "client_id": TEST_CLIENT, "clear_fields": ["description", "ip_address", "warranty_end_date"],
    }
    assert updates[2] == {"custom_asset_id": session.asset["Id"], "client_id": SECOND, "name": f"{RUN}-asset v2"}  # no location_id
    (post, create_body), (_, rename), (_, clear) = session.bodies  # the local refusal sent no write
    assert post == "POST /assets/custom" and create_body["Name"].startswith("MCPTEST-") and create_body["ClientId"] == TEST_CLIENT
    assert rename == {"Name": f"{RUN}-asset v2", "IpAddress": "192.0.2.11", "Description": f"{RUN}-asset: updated"}  # no ClientId
    assert clear == {"ClientId": TEST_CLIENT, "Description": None, "IpAddress": None, "WarrantyEndDate": None}
    assert len(session.requests) == 9 and session.requests[-1].startswith("GET /assets/custom/")  # the negative check was one GET
    assert matrix.guard.writes_allowed == 3
    assert session.asset["Name"] == f"{RUN}-asset v2" and session.asset["SerialNumber"] == f"{RUN}-sn"
    assert session.asset["ClientId"] == TEST_CLIENT and session.asset["IpAddress"] is None
    (record,) = manifest.all_created()
    assert (record.kind, record.id, record.label, record.details) == (
        "custom_asset", session.asset["Id"], f"{RUN}-asset", {"client_id": TEST_CLIENT},
    )
    assert manifest.unresolved_intents() == [] and not matrix.guard.violations
    assert any("refused locally after its read of the asset" in note for note in matrix.notes)
    assert any(session.asset["Id"] in note and "stays on the test client" in note for note in matrix.notes)
    assert "list_client_locations" not in [name for name, arguments in session.calls if arguments.get("client_id") == SECOND]


async def test_the_first_hardware_type_is_used_with_no_id_written_into_the_code(tmp_path):
    matrix, session, _ = matrix_for(tmp_path, hardware=[{"Id": 777, "Name": "Printer"}, {"Id": 3, "Name": "Phone"}])
    await write_matrix.area_custom_assets(matrix)
    assert dict(session.calls)["create_custom_asset"]["hardware_type_id"] == 777
    source = open(write_matrix.__file__, encoding="utf-8").read()
    assert "hardware_type_id=" not in source.split("async def area_custom_assets")[1].split("def check_document")[0]


async def test_without_the_flag_the_area_is_skipped_and_nothing_is_announced_or_sent(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, with_custom_assets=False)
    with pytest.raises(write_matrix.Skipped) as stopped:
        await write_matrix.area_custom_assets(matrix)
    assert str(stopped.value) == write_matrix.CUSTOM_ASSETS_SKIP_REASON
    assert "this harness never deletes a custom asset (the spec's soft DELETE is not used)" in write_matrix.CUSTOM_ASSETS_SKIP_REASON
    assert "cannot be deleted" not in write_matrix.CUSTOM_ASSETS_SKIP_REASON
    assert session.calls == [] and manifest.all_created() == [] and manifest.unresolved_intents() == []


async def test_the_custom_asset_tools_cannot_be_called_without_the_flag(tmp_path):
    matrix, session, _ = matrix_for(tmp_path, with_custom_assets=False)
    for name in ("create_custom_asset", "update_custom_asset", "list_custom_asset_hardware_types", "get_custom_asset"):
        with pytest.raises(RuntimeError, match="not one of the tools this script may call"):
            await session.call(name, {})


@pytest.mark.parametrize("types", [[], [{"Name": "no id"}], [{"Id": "7"}], [{"Id": True}], [{"Id": 0}]])
async def test_no_usable_hardware_type_is_a_skip_before_anything_is_created(tmp_path, types):
    matrix, session, manifest = matrix_for(tmp_path, hardware=types)
    with pytest.raises(write_matrix.Skipped, match="no hardware type with an Id"):
        await write_matrix.area_custom_assets(matrix)
    assert [name for name, _ in session.calls] == ["list_custom_asset_hardware_types"]
    assert manifest.all_created() == [] and manifest.unresolved_intents() == [] and session.bodies == []


async def test_a_rejected_create_settles_its_intent_and_records_nothing(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)
    session.break_create = "reject"
    with pytest.raises(ToolFailed, match="Name is required"):
        await write_matrix.area_custom_assets(matrix)
    assert manifest.all_created() == [] and manifest.unresolved_intents() == []


async def test_a_create_whose_read_back_failed_is_a_failed_area_but_the_asset_is_recorded(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)
    session.break_create = "warning"
    with pytest.raises(write_matrix.CheckFailed, match="create_custom_asset: the write succeeded but reading it back failed"):
        await write_matrix.area_custom_assets(matrix)
    (record,) = manifest.all_created()  # recorded right after the create answered, before any check
    assert record.kind == "custom_asset" and manifest.unresolved_intents() == []
    assert [name for name, _ in session.calls][-1] == "create_custom_asset"  # nothing else was sent


@pytest.mark.parametrize(
    "field, shown, message",
    [
        ("Name", "Another name", "create_custom_asset Name"),
        ("ClientId", SECOND, "create_custom_asset ClientId"),
        ("LocationId", 1, "create_custom_asset LocationId"),
        ("HardwareTypeId", 99, "create_custom_asset HardwareTypeId"),
        ("SerialNumber", "other", "create_custom_asset SerialNumber"),
        ("IpAddress", "192.0.2.99", "create_custom_asset IpAddress"),
        ("WarrantyEndDate", "2001-01-01T00:00:00", "create_custom_asset WarrantyEndDate"),
    ],
)
async def test_a_create_that_reads_back_differently_fails_the_area(tmp_path, field, shown, message):
    matrix, session, manifest = matrix_for(tmp_path)
    session.mangle = {field: shown}
    with pytest.raises(write_matrix.CheckFailed, match=message):
        await write_matrix.area_custom_assets(matrix)
    assert len(manifest.all_created()) == 1  # the asset exists and is recorded all the same


async def test_a_fresh_get_custom_asset_that_shows_another_asset_or_client_fails_the_area(tmp_path):
    matrix, session, _ = matrix_for(tmp_path)
    original = session.call

    async def other(name, arguments=None):
        row = await original(name, arguments)
        return {**row, "Id": "00000009-aaaa-4bbb-8ccc-000000000009"} if name == "get_custom_asset" else row

    session.call = other
    with pytest.raises(write_matrix.CheckFailed, match="get_custom_asset Id"):
        await write_matrix.area_custom_assets(matrix)


async def test_the_warranty_date_may_read_back_as_a_date_time(tmp_path):
    matrix, session, _ = matrix_for(tmp_path)
    session.mangle = {"WarrantyEndDate": "2100-10-02T00:00:00"}
    with pytest.raises(write_matrix.CheckFailed, match="update_custom_asset"):  # the create check passed (first 10 characters)
        await write_matrix.area_custom_assets(matrix)


@pytest.mark.parametrize("field", ["Description", "IpAddress", "WarrantyEndDate"])
async def test_a_cleared_field_that_still_reads_back_a_value_fails_the_area(tmp_path, field):
    matrix, session, _ = matrix_for(tmp_path)
    original = session.call

    async def stubborn(name, arguments=None):
        row = await original(name, arguments)
        if name == "update_custom_asset" and arguments.get("clear_fields"):
            row = {**row, field: "still here"}
        return row

    session.call = stubborn
    with pytest.raises(write_matrix.CheckFailed, match=f"clear_fields: {field} still reads back str"):
        await write_matrix.area_custom_assets(matrix)


async def test_cleared_fields_may_read_back_as_null_or_as_an_empty_text(tmp_path):
    matrix, session, _ = matrix_for(tmp_path)
    original = session.call

    async def empty(name, arguments=None):
        row = await original(name, arguments)
        if name == "update_custom_asset" and arguments.get("clear_fields"):
            row = {**row, "Description": "", "IpAddress": "", "WarrantyEndDate": None}
        return row

    session.call = empty
    await write_matrix.area_custom_assets(matrix)


async def test_a_clear_that_changes_the_client_fails_the_area(tmp_path):
    matrix, session, _ = matrix_for(tmp_path)
    original = session.call

    async def moved(name, arguments=None):
        row = await original(name, arguments)
        return {**row, "ClientId": SECOND} if name == "update_custom_asset" and arguments.get("clear_fields") else row

    session.call = moved
    with pytest.raises(write_matrix.CheckFailed, match="the current client moved nothing"):
        await write_matrix.area_custom_assets(matrix)


# the negative check: a move without a location_id must be refused LOCALLY by the tool, after one GET


class RefusalSession(AssetSession):
    """The tool's answer to a move without a location_id, whatever mode says: "local" is the real tool; the others are what it must not do."""

    def __init__(self, *args, mode="local", **options):
        super().__init__(*args, **options)
        self.mode = mode

    def _update(self, name: str, arguments: dict) -> dict:
        if arguments.get("client_id") == SECOND and arguments.get("location_id") is None and arguments.get("clear_fields") is None:
            if self.mode == "local":
                return super()._update(name, arguments)
            if self.mode == "no-read":  # refuses before any request
                raise ToolFailed(name, "location_id: required when client_id moves the asset", sent=False)
            if self.mode == "other-text":  # a local refusal for another reason
                self._send("GET", f"/assets/custom/{arguments['custom_asset_id']}")
                raise ToolFailed(name, "name: not blank", sent=True)
            if self.mode == "http":
                self._send("GET", f"/assets/custom/{arguments['custom_asset_id']}")
                raise ToolFailed(name, "Gorelo failed update_custom_asset (HTTP 500, code 1)", sent=True)
            if self.mode == "two-requests":
                self._send("GET", f"/assets/custom/{arguments['custom_asset_id']}")
                self._send("GET", f"/assets/custom/{arguments['custom_asset_id']}")
                raise ToolFailed(name, "location_id: required when client_id moves the asset", sent=True)
            if self.mode == "accepts":
                self._send("GET", f"/assets/custom/{arguments['custom_asset_id']}")
                self._send("PATCH", f"/assets/custom/{arguments['custom_asset_id']}", {"Name": arguments["name"]})
                return self._row()
        return super()._update(name, arguments)


def refusal_matrix(tmp_path, mode):
    manifest = Manifest(RUN, tmp_path / "run.json")
    pacer = SimpleNamespace(requests=0)
    guard = LiveGuard("write", manifest, require_intents=True, allow_custom_assets=True)
    session = RefusalSession(guard, pacer, allowed=write_matrix.tools_for(False, False, False, True), mode=mode)
    session.manifest = manifest
    matrix = write_matrix.Matrix(
        manifest=manifest, guard=guard, pacer=pacer, session=session, secret="sekrit-key-1",
        clock=lambda: datetime(2099, 10, 2, 10, 15, 0, tzinfo=timezone.utc), lookup_wait=0, scopes_missing=set(), with_custom_assets=True,
    )
    return matrix, session


NOT_REFUSED = (
    "update_custom_asset with another client_id and no location_id was not refused locally after one GET and before any PATCH, "
    "so the negative check did not pass"
)


@pytest.mark.parametrize(
    "mode, said",
    [
        ("no-read", f"{NOT_REFUSED} (0 request(s), no HTTP answer): location_id: required when client_id moves the asset"),
        ("other-text", f"{NOT_REFUSED} (1 request(s), no HTTP answer): name: not blank"),
        ("http", f"{NOT_REFUSED} (1 request(s), HTTP 500): Gorelo failed update_custom_asset (HTTP 500, code 1)"),
        (
            "accepts",
            "update_custom_asset with another client_id and no location_id was ACCEPTED (a PATCH reached Gorelo): the asset may "
            "have moved to client 9502; look for it in the Gorelo app",
        ),
    ],
)
async def test_a_move_without_a_location_that_the_tool_does_not_refuse_locally_after_one_get_fails_the_area(tmp_path, mode, said):
    matrix, session = refusal_matrix(tmp_path, mode)
    with pytest.raises(write_matrix.CheckFailed) as failed:
        await write_matrix.area_custom_assets(matrix)
    assert str(failed.value) == said


async def test_a_refusal_after_a_read_that_the_client_sent_again_is_still_the_local_refusal(tmp_path):
    # the client sends the read once more after a 429, a 502, 503 or 504 or a refused connection: two GETs and no write are the
    # same local refusal, and the note says how many requests there were
    matrix, session = refusal_matrix(tmp_path, "two-requests")
    await write_matrix.area_custom_assets(matrix)
    assert matrix.guard.writes_allowed == 3  # the same three writes as without the retry; the refused move wrote nothing
    assert any(
        "refused locally after its read of the asset, nothing was sent but GETs (2 requests: the read and the client's own retries "
        "of it)" in note
        for note in matrix.notes
    )


# the opt-in move to the second client and back (--with-asset-move)

MOVE_CALLS = [
    *HAPPY_CALLS, "list_client_locations", "update_custom_asset", "get_custom_asset", "update_custom_asset", "get_custom_asset",
]


async def test_the_move_goes_to_the_second_client_and_back_and_is_announced_in_the_manifest_first(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, with_asset_move=True)
    await write_matrix.area_custom_assets(matrix)
    assert [name for name, _ in session.calls] == MOVE_CALLS
    out, back = [arguments for name, arguments in session.calls if name == "update_custom_asset"][-2:]
    assert out == {"custom_asset_id": session.asset["Id"], "client_id": SECOND, "location_id": SECOND_LOCATION}
    assert back == {"custom_asset_id": session.asset["Id"], "client_id": TEST_CLIENT, "location_id": LOCATION}  # where it was created
    move_out, move_back = session.bodies[-2:]
    assert move_out[1] == {"ClientId": SECOND, "LocationId": SECOND_LOCATION}
    assert move_back[1] == {"ClientId": TEST_CLIENT, "LocationId": LOCATION}
    # the details named the second client when the move PATCH was sent (the announcement came first), and the test client at the end
    assert session.details_at_patch[-2] == SECOND and session.details_at_patch[-1] == SECOND
    (record,) = manifest.all_created()
    assert record.details == {"client_id": TEST_CLIENT}
    assert session.asset["ClientId"] == TEST_CLIENT and session.asset["LocationId"] == LOCATION
    assert matrix.guard.writes_allowed == 5 and not matrix.guard.violations and manifest.unresolved_intents() == []
    assert any("moved to client" in note and "and back to client" in note and "the same Id each time" in note for note in matrix.notes)
    assert len(session.requests) == 9 + 2 * (3 + 1)  # the nine of the plain area, then GET, PATCH, GET and a get_custom_asset each way


async def test_without_the_move_flag_the_second_clients_location_is_never_read_and_nothing_moves(tmp_path):
    matrix, session, _ = matrix_for(tmp_path, with_asset_move=False)
    await write_matrix.area_custom_assets(matrix)
    assert not any(name == "list_client_locations" and arguments["client_id"] == SECOND for name, arguments in session.calls)
    assert session.asset["ClientId"] == TEST_CLIENT and matrix.guard.allow_custom_asset_move is False


async def test_a_second_client_that_is_not_active_skips_the_move_with_a_note_and_sends_nothing_for_it(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, with_asset_move=True, second_client_active=False)
    await write_matrix.area_custom_assets(matrix)
    assert [name for name, _ in session.calls] == HAPPY_CALLS
    assert any("asset move skipped: the second client" in note and "not Active" in note for note in matrix.notes)
    assert manifest.all_created()[0].details == {"client_id": TEST_CLIENT}


async def test_a_second_client_without_a_location_skips_the_move_with_a_note(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, with_asset_move=True, second_locations=[])
    await write_matrix.area_custom_assets(matrix)
    assert [name for name, _ in session.calls] == [*HAPPY_CALLS, "list_client_locations"]
    assert any("has no location to move the asset to" in note for note in matrix.notes)
    assert manifest.all_created()[0].details == {"client_id": TEST_CLIENT}


async def test_a_move_that_gorelo_refuses_leaves_the_asset_at_the_test_client_and_fails_the_area_without_a_way_back(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, with_asset_move=True, out="refuse")
    with pytest.raises(ToolFailed, match="HTTP 400"):
        await write_matrix.area_custom_assets(matrix)
    assert manifest.all_created()[0].details == {"client_id": TEST_CLIENT}  # Gorelo said no: it never left
    assert [name for name, _ in session.calls][-1] == "update_custom_asset" and session.asset["ClientId"] == TEST_CLIENT
    assert len([c for c in session.calls if c[0] == "update_custom_asset"]) == 3 + 1  # three before the move, the refused move, no way back


@pytest.mark.parametrize("mode", ["server", "timeout", "unconfirmed", "ignored"])
@pytest.mark.parametrize("applied", [False, True])
async def test_a_move_with_an_unknown_outcome_is_followed_by_the_way_back_and_ends_at_the_test_client(tmp_path, mode, applied):
    matrix, session, manifest = matrix_for(tmp_path, with_asset_move=True, out=mode)
    session.applied_on_error = applied
    with pytest.raises(ToolFailed):
        await write_matrix.area_custom_assets(matrix)
    moves = [arguments for name, arguments in session.calls if name == "update_custom_asset"][3:]
    assert [m["client_id"] for m in moves] == [SECOND, TEST_CLIENT]  # the way back was asked for whatever the way out did
    assert session.asset["ClientId"] == TEST_CLIENT
    assert manifest.all_created()[0].details == {"client_id": TEST_CLIENT}  # confirmed home by the way back


@pytest.mark.parametrize("back", ["refuse", "server", "timeout", "unconfirmed", "ignored"])
async def test_a_way_back_that_fails_leaves_the_asset_recorded_at_the_second_client_when_the_way_out_was_clean(tmp_path, back):
    matrix, session, manifest = matrix_for(tmp_path, with_asset_move=True, back=back)
    session.applied_on_error = False
    with pytest.raises(ToolFailed):
        await write_matrix.area_custom_assets(matrix)
    details = manifest.all_created()[0].details["client_id"]
    unsure = back in ("server", "timeout", "unconfirmed", "ignored")  # Gorelo may or may not have applied it
    assert details == (cleanup.CUSTOM_ASSET_CLIENT_UNKNOWN if unsure else SECOND)
    assert session.asset["ClientId"] == SECOND  # the scripted Gorelo never brought it back


async def test_a_way_out_with_an_unknown_outcome_and_a_way_back_that_fails_is_recorded_as_unknown(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, with_asset_move=True, out="timeout", back="refuse")
    session.applied_on_error = True  # the timed-out move did happen, so the way back is a real move and Gorelo refuses it
    with pytest.raises(ToolFailed):
        await write_matrix.area_custom_assets(matrix)
    assert manifest.all_created()[0].details == {"client_id": cleanup.CUSTOM_ASSET_CLIENT_UNKNOWN}


async def test_a_move_that_reads_back_wrongly_is_still_followed_by_the_way_back_and_fails_the_area(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, with_asset_move=True)
    original = session.call

    async def wrong_location(name, arguments=None):
        row = await original(name, arguments)
        if name == "update_custom_asset" and arguments.get("client_id") == SECOND and arguments.get("location_id"):
            row = {**row, "LocationId": 1}
        return row

    session.call = wrong_location
    with pytest.raises(write_matrix.CheckFailed, match="update_custom_asset \\(move\\) LocationId"):
        await write_matrix.area_custom_assets(matrix)
    assert session.asset["ClientId"] == TEST_CLIENT and manifest.all_created()[0].details == {"client_id": TEST_CLIENT}


async def test_a_way_back_that_the_read_does_not_confirm_is_recorded_as_unknown(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, with_asset_move=True)
    original, reads = session.call, []

    async def liar(name, arguments=None):
        row = await original(name, arguments)
        if name == "get_custom_asset":
            reads.append(1)
            if len(reads) == 3:  # the read after the way back (1: after the create, 2: after the way out)
                return {**row, "ClientId": SECOND}
        return row

    session.call = liar
    with pytest.raises(write_matrix.CheckFailed, match="get_custom_asset ClientId after the way back"):
        await write_matrix.area_custom_assets(matrix)
    assert manifest.all_created()[0].details == {"client_id": cleanup.CUSTOM_ASSET_CLIENT_UNKNOWN}


async def test_a_guard_refusal_during_the_move_leaves_the_asset_recorded_as_unknown_and_stops_the_run(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, with_asset_move=True)
    matrix.guard.allow_custom_asset_move = False  # as if the run had not enabled the move
    with pytest.raises(GuardTripped, match="would MOVE the custom asset to the second client"):
        await write_matrix.area_custom_assets(matrix)
    assert manifest.all_created()[0].details == {"client_id": cleanup.CUSTOM_ASSET_CLIENT_UNKNOWN}


async def test_a_second_location_is_the_default_one_else_the_first_with_an_id(tmp_path):
    rows = [{"Id": 11, "ClientId": SECOND}, {"Id": 12, "ClientId": SECOND, "IsDefault": True}]
    matrix, session, _ = matrix_for(tmp_path, with_asset_move=True, second_locations=rows)
    await write_matrix.area_custom_assets(matrix)
    assert session.bodies[-2][1]["LocationId"] == 12
    matrix, session, _ = matrix_for(tmp_path / "other", with_asset_move=True, second_locations=[{"Id": 11}, {"Id": 12}])
    (tmp_path / "other").mkdir(exist_ok=True)
    await write_matrix.area_custom_assets(matrix)
    assert session.bodies[-2][1]["LocationId"] == 11


async def test_the_cleanup_outcome_and_the_summary_name_where_a_moved_asset_is(tmp_path, make_settings):
    matrix, session, manifest = matrix_for(tmp_path, with_asset_move=True, back="refuse")
    with pytest.raises(ToolFailed):
        await write_matrix.area_custom_assets(matrix)
    assert manifest.all_created()[0].details == {"client_id": SECOND}
    out: list[str] = []
    report = await cleanup.run_cleanup(
        manifest, settings=make_settings(destructive=True), transport=MockGorelo().transport, pace=0, echo=out.append,
        verify_clients=False,
    )
    (record,) = report.undeletable
    assert record.outcome == cleanup.custom_asset_residue(SECOND)
    summary = write_matrix.MatrixReport(
        run_id=RUN, manifest_path="m.json", cleanup=write_matrix.CleanupReport(run_id=RUN),
        assets=[str(record.id)], asset_texts=write_matrix.residue_asset_texts(manifest),
    ).render()
    assert f"reminder: custom asset {record.id} {cleanup.custom_asset_residue(SECOND)}" in summary


async def test_a_request_the_guard_refuses_stops_the_area_with_the_guard_text(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)
    matrix.guard.allow_custom_assets = False  # as if the run had not enabled the area
    with pytest.raises(GuardTripped, match="custom assets cannot be created unless the run enabled"):
        await write_matrix.area_custom_assets(matrix)
    assert manifest.all_created() == [] and manifest.unresolved_intents() == []  # the intent was settled as failed


# --------------------------------------------------------------------------
# Residue: the cleanup sends nothing, the report says what to do
# --------------------------------------------------------------------------


async def test_the_cleanup_sends_nothing_for_a_custom_asset_and_reports_a_known_residue(tmp_path, make_settings):
    matrix, session, manifest = matrix_for(tmp_path)
    await write_matrix.area_custom_assets(matrix)
    mock = MockGorelo()  # no routes: any request would be unmatched
    out: list[str] = []
    report = await cleanup.run_cleanup(
        manifest, settings=make_settings(destructive=True), transport=mock.transport, pace=0, echo=out.append,
        verify_clients=False,
    )
    assert mock.requests == [] and not mock.unmatched
    assert report.ok and report.leftovers == [] and report.cleaned == [] and report.unresolved == []
    (record,) = report.undeletable
    assert record.kind == "custom_asset" and record.outcome == cleanup.CUSTOM_ASSET_RESIDUE
    assert cleanup.is_known_undeletable(record)
    assert any(line.startswith("  known undeletable custom_asset ") for line in out)
    assert out[-1] == "result: cleanup complete; 1 known undeletable record remains (see above)"
    # a second cleanup finds the same outcome and adds nothing
    attempts = len(manifest.record("custom_asset", record.id).attempts)
    await cleanup.run_cleanup(
        manifest, settings=make_settings(destructive=True), transport=mock.transport, pace=0, echo=out.append, verify_clients=False
    )
    assert len(manifest.record("custom_asset", record.id).attempts) == attempts and mock.requests == []


def test_a_custom_asset_is_a_known_residue_only_with_the_residue_outcome():
    plain = SimpleNamespace(kind="custom_asset", outcome="delete failed")
    assert not cleanup.is_known_undeletable(plain)
    assert cleanup.is_known_undeletable(SimpleNamespace(kind="custom_asset", outcome=cleanup.CUSTOM_ASSET_RESIDUE))
    assert "Gorelo app" in cleanup.CUSTOM_ASSET_RESIDUE
    assert "this harness never deletes a custom asset (the spec's soft DELETE is not used)" in cleanup.CUSTOM_ASSET_RESIDUE
    assert "cannot be deleted through the API" not in cleanup.CUSTOM_ASSET_RESIDUE


def test_the_summary_tells_the_operator_the_asset_stays_until_removed_in_the_gorelo_app():
    report = write_matrix.MatrixReport(
        run_id=RUN, manifest_path="m.json", cleanup=write_matrix.CleanupReport(run_id=RUN), assets=["a-1", "a-2"]
    )
    text = report.render()
    assert f"reminder: custom asset a-1 {cleanup.CUSTOM_ASSET_RESIDUE}" in text
    assert f"reminder: custom asset a-2 {cleanup.CUSTOM_ASSET_RESIDUE}" in text
    assert text[-1] == "result: PASSED"  # a residue is not a failure
    assert write_matrix.MatrixReport(run_id=RUN, manifest_path="m.json").render()[-1] == "result: FAILED"
    assert not any("custom asset" in line for line in write_matrix.MatrixReport(
        run_id=RUN, manifest_path="m.json", cleanup=write_matrix.CleanupReport(run_id=RUN)).render())


def test_residue_assets_lists_the_custom_assets_of_the_manifest(tmp_path):
    manifest = Manifest(RUN, tmp_path / "r.json")
    assert write_matrix.residue_assets(manifest) == []
    manifest.created("custom_asset", "00000001-aaaa-4bbb-8ccc-000000000001", f"{RUN}-asset")
    manifest.created("client", 8200, f"{RUN} client")
    assert write_matrix.residue_assets(manifest) == ["00000001-aaaa-4bbb-8ccc-000000000001"]


# --------------------------------------------------------------------------
# The record while a move is unanswered: it says the asset MAY be on the second client, and names the client once Gorelo answers
# --------------------------------------------------------------------------


class OutcomeSession(AssetSession):
    """AssetSession that notes the residue text of the asset (its record outcome) at each PATCH sent and each get_custom_asset call."""

    def __init__(self, *args, **options):
        super().__init__(*args, **options)
        self.texts: list[tuple[str, str | None]] = []

    def record_text(self) -> str | None:
        return self.manifest.record("custom_asset", self.asset["Id"]).outcome if self.asset else None

    def _send(self, method: str, path: str, body: dict | None = None) -> None:
        if method == "PATCH":
            self.texts.append((f"PATCH {path}", self.record_text()))
        super()._send(method, path, body)

    async def call(self, name: str, arguments=None) -> dict:
        if name == "get_custom_asset":
            self.texts.append((name, self.record_text()))
        return await super().call(name, arguments)


async def test_the_record_says_the_asset_may_be_on_the_second_client_until_the_move_is_answered(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, with_asset_move=True, session_class=OutcomeSession)
    await write_matrix.area_custom_assets(matrix)
    pending = f"may be on the second client {SECOND} (its move is not answered yet), and stays there {cleanup.CUSTOM_ASSET_RESIDUE_TAIL}"
    assert write_matrix.custom_asset_unanswered_text(SECOND) == pending
    patches = [text for step, text in session.texts if step.startswith("PATCH")]
    assert patches[-2:] == [pending, pending]  # the move and the way back were sent while the record said the asset MAY be there
    reads = [text for step, text in session.texts if step == "get_custom_asset"]
    assert reads == [None, cleanup.custom_asset_residue(SECOND), cleanup.CUSTOM_ASSET_RESIDUE]  # named after the move, home after the way back
    (record,) = manifest.all_created()
    assert record.outcome == cleanup.CUSTOM_ASSET_RESIDUE and record.details == {"client_id": TEST_CLIENT}


async def test_an_interrupted_move_leaves_a_record_that_says_the_asset_may_be_on_the_second_client(tmp_path, make_settings):
    matrix, session, manifest = matrix_for(tmp_path, with_asset_move=True)
    original = session.call

    async def stopped_during_the_move(name, arguments=None):
        if name == "update_custom_asset" and (arguments or {}).get("client_id") == SECOND and (arguments or {}).get("location_id"):
            raise RuntimeError("the run was stopped while the move was unanswered")
        return await original(name, arguments)

    session.call = stopped_during_the_move
    with pytest.raises(RuntimeError, match="stopped while the move was unanswered"):
        await write_matrix.area_custom_assets(matrix)
    out: list[str] = []
    report = await cleanup.run_cleanup(
        manifest, settings=make_settings(destructive=True), transport=MockGorelo().transport, pace=0, echo=out.append,
        verify_clients=False,
    )
    (record,) = report.undeletable
    assert record.outcome == write_matrix.custom_asset_unanswered_text(SECOND)  # the cleanup keeps the matrix's text: it ends with the tail
    assert record.details == {"client_id": SECOND}  # the announcement is in the manifest, where the guard reads it
    summary = write_matrix.MatrixReport(
        run_id=RUN, manifest_path="m.json", cleanup=report, assets=[str(record.id)],
        asset_texts=write_matrix.residue_asset_texts(manifest),
    ).render()
    assert f"reminder: custom asset {record.id} {write_matrix.custom_asset_unanswered_text(SECOND)}" in summary
    assert f"reminder: {write_matrix.CUSTOM_ASSET_NEVER_MOVED_BACK}" in summary


def test_the_summary_repeats_the_text_of_each_asset_record():
    text = write_matrix.MatrixReport(
        run_id=RUN, manifest_path="m.json", cleanup=write_matrix.CleanupReport(run_id=RUN), assets=["a-1", "a-2"],
        asset_texts={"a-1": write_matrix.custom_asset_unanswered_text(SECOND), "a-2": cleanup.custom_asset_residue(SECOND)},
    ).render()
    assert f"reminder: custom asset a-1 {write_matrix.custom_asset_unanswered_text(SECOND)}" in text
    assert f"reminder: custom asset a-2 {cleanup.custom_asset_residue(SECOND)}" in text
    assert f"reminder: custom asset a-1 {cleanup.CUSTOM_ASSET_RESIDUE}" not in text


def test_the_summary_says_the_cleanup_never_moves_a_stranded_asset_back_and_only_when_there_is_an_asset():
    with_asset = write_matrix.MatrixReport(
        run_id=RUN, manifest_path="m.json", cleanup=write_matrix.CleanupReport(run_id=RUN), assets=["a-1"],
    ).render()
    assert f"reminder: {write_matrix.CUSTOM_ASSET_NEVER_MOVED_BACK}" in with_asset
    assert "a manual step in the Gorelo app" in write_matrix.CUSTOM_ASSET_NEVER_MOVED_BACK
    bare = write_matrix.MatrixReport(run_id=RUN, manifest_path="m.json", cleanup=write_matrix.CleanupReport(run_id=RUN)).render()
    assert not any("never moves" in line for line in bare)


def test_the_matrix_fails_closed_on_the_second_client_status_unless_the_run_says_it_is_active():
    assert inspect.signature(write_matrix.Matrix).parameters["second_client_active"].default is False


# --------------------------------------------------------------------------
# Tool sets, areas and flags
# --------------------------------------------------------------------------


def test_the_custom_asset_tools_are_named_apart_and_only_the_flag_opens_them():
    assert write_matrix.CUSTOM_ASSET_TOOLS == {
        "list_custom_asset_hardware_types", "create_custom_asset", "update_custom_asset", "get_custom_asset",
    }
    assert not write_matrix.CUSTOM_ASSET_TOOLS & write_matrix.MATRIX_TOOLS
    assert write_matrix.tools_for(False) == write_matrix.tools_for(False, False, False, False)
    assert not write_matrix.CUSTOM_ASSET_TOOLS & write_matrix.tools_for(True, True)
    assert write_matrix.tools_for(False, False, False, True) == (
        write_matrix.MATRIX_TOOLS - write_matrix.ITEM_TOOLS - write_matrix.INVOICE_TOOLS | write_matrix.CUSTOM_ASSET_TOOLS
    )
    assert write_matrix.tools_for(True, True, False, True) == write_matrix.MATRIX_TOOLS | write_matrix.CUSTOM_ASSET_TOOLS
    # the approved invoice run has its own set and the custom flag adds nothing to it
    assert write_matrix.tools_for(False, False, True, True) == write_matrix.tools_for(False, False, True)


def test_the_area_sits_after_items_and_before_the_custom_properties_and_documents_and_is_selected_by_name():
    keys = [area.key for area in write_matrix.AREAS]
    assert keys.index("items") + 1 == keys.index("custom_assets") == keys.index("custom_properties") - 1 == keys.index("documents") - 2
    assert write_matrix.CUSTOM_ASSETS_AREA == "custom_assets"
    assert [a.key for a in write_matrix.select_areas(["custom_assets"], False)] == ["custom_assets"]


def test_the_area_does_not_name_a_tenant_id_and_the_ip_is_from_the_documentation_range():
    assert write_matrix.CUSTOM_ASSET_IP == "192.0.2.10" and write_matrix.CUSTOM_ASSET_IP_CHANGED == "192.0.2.11"
    assert write_matrix.CUSTOM_ASSET_IP.startswith("192.0.2.")  # TEST-NET-1 (RFC 5737)


@pytest.mark.parametrize("flag", ["with_items", "with_invoices", "with_custom_assets", "leftovers", "skip_email"])
async def test_the_approved_invoice_flag_beside_the_custom_assets_flag_is_a_usage_error(run, tmp_path, flag):
    fake = FakeGorelo()
    named = "--" + flag.replace("_", "-")
    with pytest.raises(write_matrix.UsageError, match=f"--with-approved-invoice runs alone: it cannot be combined with {named}$"):
        await run(fake, areas=["approved_invoice"], with_approved_invoice=True, **{flag: True})
    assert fake.mock.requests == []


def test_the_command_line_has_the_flag_and_it_is_off_by_default(capsys):
    with pytest.raises(SystemExit):
        write_matrix.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "--with-custom-assets" in out and "this harness never deletes a custom asset (the spec's soft DELETE is not used)" in out


# --------------------------------------------------------------------------
# End to end with the real tools (runs once the custom asset tools are registered)
# --------------------------------------------------------------------------


class AssetsFake(FakeGorelo):
    """FakeGorelo plus the custom asset routes of contract a7954b24844b: hardware types, the list by ClientIds, the single GET, POST,
    and a PATCH that MOVES the asset when it names another client (the new client must be Active and a LocationId of that client is
    required, like Gorelo says), so the real tools, the real guard and the real cleanup run end to end."""

    def __init__(self, **options):
        super().__init__(**options)
        self.assets: dict[str, dict] = {}
        self.hardware = [{"Id": HARDWARE, "Name": "Laptop"}, {"Id": 43, "Name": "Server"}]
        self.route("GET", "/v1/assets/custom/hardware-types", lambda q: envelope(self.hardware))
        self.route("GET", "/v1/assets/custom", self.list_assets)
        self.route("GET", "/v1/assets/custom/{customAssetId}", self.get_asset)
        self.route("POST", "/v1/assets/custom", self.post_asset)
        self.route("PATCH", "/v1/assets/custom/{customAssetId}", self.patch_asset)

    def list_assets(self, q):
        wanted = {int(x) for x in q.query.get("ClientIds", "").split(",") if x}
        return paged_envelope([dict(a) for a in self.assets.values() if not wanted or a["ClientId"] in wanted])

    def get_asset(self, q):
        asset = self.assets.get(self.part(q, 4))
        return envelope(dict(asset)) if asset else self.missing_custom_asset()

    def post_asset(self, q):
        body, ident = q.json, self.guid()
        self.assets[ident] = {
            "Id": ident, "Banner": "", "Description": body.get("Description") or "", "ContactId": body.get("ContactId"),
            "IpAddress": body.get("IpAddress"), "SerialNumber": body.get("SerialNumber"),
            "WarrantyEndDate": body.get("WarrantyEndDate"), "HardwareType": "Laptop", "UpdatedOn": None,
            "Name": body["Name"], "ClientId": body["ClientId"], "LocationId": body["LocationId"],
            "HardwareTypeId": body["HardwareTypeId"], "CreatedOn": "2099-10-02T10:15:00Z",
        }
        return envelope({"Id": ident})

    def patch_asset(self, q):
        asset = self.assets.get(self.part(q, 4))
        if asset is None:
            return self.missing_custom_asset()
        client = q.json.get("ClientId")
        if client is not None and client != asset["ClientId"]:  # a new ClientId moves the asset
            if self.clients.get(client, {}).get("Status", {}).get("Id") != 1:
                return error_envelope(400, [("070101", "The client must be an active client.", "ClientId")])
            if q.json.get("LocationId") not in {row["Id"] for row in self.locations.get(client, [])}:
                return error_envelope(400, [("070101", "The location must belong to the new client.", "LocationId")])
        for key, value in q.json.items():
            asset[key] = ("" if value is None else value) if key == "Description" else value
        return envelope({"Id": asset["Id"]})


@needs_tools
async def test_end_to_end_the_area_passes_leaves_one_residue_and_the_run_still_passes(run, lines):
    fake = AssetsFake()
    report = await run(fake, areas=["custom_assets"], with_custom_assets=True)
    assert report.ok and report.exit_code == 0, "\n".join(lines)
    assert results(report)["custom_assets"].status == "pass"
    assert area_ops(fake, report) == {"custom_assets": GOLDEN_CUSTOM_ASSETS, "cleanup": []}  # the cleanup sends nothing
    (asset,) = fake.assets.values()
    assert asset["Name"] == f"{RUN}-asset v2" and asset["ClientId"] == TEST_CLIENT and asset["SerialNumber"] == f"{RUN}-sn"
    assert asset["Description"] == "" and asset["IpAddress"] is None and asset["WarrantyEndDate"] is None
    assert asset["HardwareTypeId"] == HARDWARE and asset["LocationId"] == 9001
    assert report.summary["created"] == 1 and report.summary["cleaned"] == 0
    assert report.summary["undeletable"] == 1 and report.summary["leftovers"] == 0 and report.cleanup.ok
    assert report.refused == 0 and report.assets == [asset["Id"]]
    assert f"known undeletable: custom_asset {asset['Id']}: {cleanup.CUSTOM_ASSET_RESIDUE}" in lines
    assert f"reminder: custom asset {asset['Id']} {cleanup.CUSTOM_ASSET_RESIDUE}" in lines
    assert lines[-1] == "result: PASSED"
    assert not fake.mock.unmatched and not any(r.method == "DELETE" for r in fake.mock.requests)


@needs_tools
async def test_end_to_end_the_updates_name_no_client_the_current_client_and_never_the_second_one_without_the_move_flag(run):
    fake = AssetsFake()
    report = await run(fake, areas=["custom_assets"], with_custom_assets=True)
    assert report.ok
    patches = [r for r in fake.mock.requests if r.method == "PATCH"]
    assert [p.json.get("ClientId") for p in patches] == [None, TEST_CLIENT]  # the negative check sent no PATCH at all
    assert "LocationId" not in patches[0].json and patches[1].json == {
        "ClientId": TEST_CLIENT, "Description": None, "IpAddress": None, "WarrantyEndDate": None,
    }
    assert not any(r.path == f"/v1/clients/{SECOND}/locations" for r in fake.mock.requests)  # the second client was not even read


@needs_tools
async def test_end_to_end_with_the_move_flag_the_asset_goes_to_the_second_client_and_comes_back(run, lines):
    fake = AssetsFake()
    report = await run(fake, areas=["custom_assets"], with_custom_assets=True, with_asset_move=True)
    assert report.ok, "\n".join(lines)
    assert area_ops(fake, report) == {"custom_assets": [*GOLDEN_CUSTOM_ASSETS, *GOLDEN_ASSET_MOVE], "cleanup": []}
    patches = [r for r in fake.mock.requests if r.method == "PATCH"]
    assert [p.json.get("ClientId") for p in patches] == [None, TEST_CLIENT, SECOND, TEST_CLIENT]
    assert patches[2].json == {"ClientId": SECOND, "LocationId": 9002}
    assert patches[3].json == {"ClientId": TEST_CLIENT, "LocationId": 9001}
    (asset,) = fake.assets.values()
    assert asset["ClientId"] == TEST_CLIENT and asset["LocationId"] == 9001 and asset["Name"] == f"{RUN}-asset v2"
    (record,) = Manifest.load(report.manifest_path).all_created()
    assert record.details == {"client_id": TEST_CLIENT} and report.refused == 0
    assert f"reminder: custom asset {asset['Id']} {cleanup.CUSTOM_ASSET_RESIDUE}" in lines  # back home: the residue is on the test client


@needs_tools
async def test_end_to_end_a_gorelo_that_refuses_the_move_fails_the_area_and_the_asset_stays(run, lines):
    fake = AssetsFake()
    fake.clients[SECOND]["Status"] = {"Id": 4, "Name": "Archived"}  # the second client is archived: Gorelo refuses the move
    from site_helper import CLIENT_STATUS

    CLIENT_STATUS[SECOND] = (1, "Active")  # ... although the check of the site clients (stubbed) said Active
    report = await run(fake, areas=["custom_assets"], with_custom_assets=True, with_asset_move=True)
    found = results(report)["custom_assets"]
    assert found.status == "FAIL" and "HTTP 400" in found.detail and "active client" in found.detail
    (asset,) = fake.assets.values()
    assert asset["ClientId"] == TEST_CLIENT
    assert [r.json.get("ClientId") for r in fake.mock.requests if r.method == "PATCH"] == [None, TEST_CLIENT, SECOND]  # no way back
    assert Manifest.load(report.manifest_path).all_created()[0].details == {"client_id": TEST_CLIENT}
    assert report.summary["undeletable"] == 1 and not report.ok and lines[-1] == "result: FAILED"


@needs_tools
async def test_end_to_end_a_gorelo_that_ignores_the_move_is_reported_and_the_way_back_still_goes_out(run):
    fake = AssetsFake()
    original = fake.patch_asset

    def ignoring(q):
        if q.json.get("ClientId") == SECOND:
            return envelope({"Id": fake.part(q, 4)})  # a success that moved nothing
        return original(q)

    fake.handlers["PATCH /v1/assets/custom/{customAssetId}"] = ignoring
    report = await run(fake, areas=["custom_assets"], with_custom_assets=True, with_asset_move=True)
    found = results(report)["custom_assets"]
    assert found.status == "FAIL" and "reads back at client 9501, not at client 9502" in found.detail
    assert [r.json.get("ClientId") for r in fake.mock.requests if r.method == "PATCH"] == [None, TEST_CLIENT, SECOND, TEST_CLIENT]
    assert next(iter(fake.assets.values()))["ClientId"] == TEST_CLIENT


@needs_tools
@pytest.mark.parametrize("status_id, status_name", [(4, "Archived"), (3, "Credit hold")])
async def test_end_to_end_a_second_client_that_is_not_active_is_never_read_or_written_by_the_move(run, lines, monkeypatch, status_id, status_name):
    fake = AssetsFake()
    fake.clients[SECOND]["Status"] = {"Id": status_id, "Name": status_name}
    monkeypatch.setitem(CLIENT_STATUS, SECOND, (status_id, status_name))  # the site check of the run reads the same status
    report = await run(fake, areas=["custom_assets"], with_custom_assets=True, with_asset_move=True)
    found = results(report)["custom_assets"]
    assert report.ok and found.status == "pass", "\n".join(lines)
    assert f"asset move skipped: the second client {SECOND} is not Active, and a moved asset needs an active client" in found.notes
    assert area_ops(fake, report) == {"custom_assets": GOLDEN_CUSTOM_ASSETS, "cleanup": []}  # the move sent nothing
    assert not any(f"/clients/{SECOND}" in r.path for r in fake.mock.requests)  # no read of its locations
    assert not any(isinstance(r.json, dict) and r.json.get("ClientId") == SECOND for r in fake.mock.requests)  # no PATCH with its ClientId
    assert not any(str(SECOND) in value for r in fake.mock.requests for value in r.query.values())  # no list filtered on it
    (asset,) = fake.assets.values()
    assert asset["ClientId"] == TEST_CLIENT


@needs_tools
async def test_end_to_end_the_area_is_skipped_by_default_in_a_full_run(run):
    fake = AssetsFake()
    report = await run(fake)
    assert results(report)["custom_assets"].status == "skipped" and fake.assets == {}
    assert not any("/assets/custom" in r.path for r in fake.mock.requests)


@needs_tools
async def test_end_to_end_a_run_with_every_optional_area_but_the_approved_invoice_passes(run, lines):
    fake = AssetsFake()
    report = await run(fake, with_items=True, with_invoices=True, with_custom_assets=True, with_asset_move=True, with_documents=True)
    assert report.ok, "\n".join(lines)
    # the approved invoice and the custom properties have flags of their own (the first one runs alone)
    new_areas = [area.key for area in write_matrix.OPT_INS]  # the 1.5.0 areas have flags of their own too
    assert [r.status for r in report.results if r.area not in ("approved_invoice", "custom_properties", *new_areas)] == ["pass"] * (
        len(report.results) - 2 - len(new_areas)
    )
    assert results(report)["custom_properties"].status == "skipped"
    assert report.summary["undeletable"] == 5  # the file of the comments area, the custom asset, two documents and the file of the first
    assert area_ops(fake, report)["custom_assets"] == [*GOLDEN_CUSTOM_ASSETS, *GOLDEN_ASSET_MOVE] and GOLDEN["items"]
