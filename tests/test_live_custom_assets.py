"""The custom_assets area of the live write matrix (--with-custom-assets), run offline.

Two layers:

* Harness level (always runs): the area is driven against a scripted tool session (AssetSession) that sits in front of the
  REAL LiveGuard (write mode, require_intents=True, allow_custom_assets) and a real manifest. Nothing here depends on the
  custom asset tools themselves: the session answers what coder A's tools answer (a row, or {Id, warning}) and turns each
  call into the request the tool would send, so the guard judges the real bodies.
* End to end (runs once the three custom asset tools are registered): run_matrix against FakeGorelo extended with the
  custom asset routes, with the real tools, the real guard and the real cleanup.

A custom asset cannot be deleted through the API: the cleanup must send nothing for it and report it as a known residue.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from conftest import MockGorelo, envelope, error_envelope, paged_envelope
from site_helper import site_config_env  # noqa: F401  (autouse: points GORELO_SITE_CONFIG at invented values)
from test_live_write_matrix import (  # noqa: F401  (fixtures and helpers of the matrix tests)
    GOLDEN,
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
TOOLS_REGISTERED = {"list_custom_asset_hardware_types", "create_custom_asset", "update_custom_asset"} <= {
    spec.name for spec in REGISTRY.specs
}
needs_tools = pytest.mark.skipif(not TOOLS_REGISTERED, reason="the custom asset tools (coder A) are not registered yet")
PASCAL = {
    "name": "Name", "client_id": "ClientId", "location_id": "LocationId", "hardware_type_id": "HardwareTypeId",
    "description": "Description", "contact_id": "ContactId", "warranty_end_date": "WarrantyEndDate",
    "ip_address": "IpAddress", "serial_number": "SerialNumber",
}


# --------------------------------------------------------------------------
# Harness level: a scripted session in front of the real guard and manifest
# --------------------------------------------------------------------------


class AssetSession:
    """Stands in for ToolSession. Each tool call becomes the request the real tool would send, checked by the real guard."""

    def __init__(self, guard: LiveGuard, pacer, *, allowed: frozenset[str], hardware=None, wrong_client="refuse"):
        self.guard, self.pacer, self.allowed = guard, pacer, allowed
        self.hardware = [{"Id": HARDWARE, "Name": "Laptop"}, {"Id": 43, "Name": "Server"}] if hardware is None else hardware
        self.wrong_client = wrong_client  # "refuse" (HTTP 400), "accept", "server" (HTTP 500) or "local" (no request)
        self.calls: list[tuple[str, dict]] = []
        self.bodies: list[tuple[str, dict]] = []
        self.asset: dict | None = None
        self.break_create: str | None = None  # "reject", "warning"
        self.mangle: dict = {}  # field -> value the read-back shows instead (after a create or an update)

    def _send(self, method: str, path: str, body: dict | None) -> None:
        request = httpx.Request(method, BASE + path, json=body)
        before = len(self.guard.violations)
        try:
            self.guard.check(request)
        except GuardViolation as exc:
            assert len(self.guard.violations) > before
            raise GuardTripped(str(exc)) from None
        self.pacer.requests += 1
        self.bodies.append((f"{method} {path}", body or {}))

    def _row(self) -> dict:
        row = dict(self.asset)
        row.update(self.mangle)
        return row

    async def call(self, name: str, arguments=None) -> dict:
        arguments = dict(arguments or {})
        if name not in self.allowed:
            raise RuntimeError(f"{name} is not one of the tools this script may call")
        self.calls.append((name, arguments))
        if name == "list_custom_asset_hardware_types":
            self.pacer.requests += 1
            return {"items": self.hardware, "count": len(self.hardware)}
        if name == "list_client_locations":
            self.pacer.requests += 1
            return {"items": [{"Id": LOCATION, "ClientId": TEST_CLIENT, "IsDefault": True}], "count": 1}
        if name == "create_custom_asset":
            body = {PASCAL[key]: value for key, value in arguments.items() if value is not None}
            self._send("POST", "/assets/custom", body)
            if self.break_create == "reject":
                raise ToolFailed(name, "Gorelo rejected create_custom_asset (HTTP 400, code 070101): Name is required", sent=True)
            self.asset = {
                "Id": "00000001-aaaa-4bbb-8ccc-000000000001", "Description": "", "ContactId": None, "IpAddress": None,
                "SerialNumber": None, "WarrantyEndDate": None, **body,
            }
            if self.break_create == "warning":
                return {"Id": self.asset["Id"], "warning": "the write succeeded; re-reading it failed: HTTP 500"}
            return self._row()
        if name == "update_custom_asset":
            ident = arguments.pop("custom_asset_id")
            clear = arguments.pop("clear_fields", None) or []
            body = {PASCAL[key]: value for key, value in arguments.items() if value is not None}
            body.update({PASCAL[key]: None for key in clear})
            if arguments["client_id"] != TEST_CLIENT and self.wrong_client == "local":
                raise ToolFailed(name, "client_id: must be the asset's client", sent=False)
            self._send("PATCH", f"/assets/custom/{ident}", body)
            if arguments["client_id"] != TEST_CLIENT:
                if self.wrong_client == "refuse":
                    raise ToolFailed(name, "Gorelo rejected update_custom_asset (HTTP 400, code 070102): wrong client", sent=True)
                if self.wrong_client == "server":
                    raise ToolFailed(name, "Gorelo failed update_custom_asset (HTTP 500, code 1)", sent=True)
                if self.wrong_client in ("429", "401", "403", "404", "409"):
                    raise ToolFailed(name, f"Gorelo rejected update_custom_asset (HTTP {self.wrong_client}, code 1): no", sent=True)
                if self.wrong_client == "timeout":
                    raise ToolFailed(name, "Gorelo did not answer update_custom_asset in time; it may have applied the write", sent=True)
                if self.wrong_client == "unconfirmed":
                    raise ToolFailed(
                        name, "Gorelo did not confirm update_custom_asset (HTTP 400): the write may have applied", sent=True
                    )
            for key, value in body.items():
                self.asset[key] = "" if (key == "Description" and value is None) else value
            return self._row()
        raise AssertionError(f"unexpected tool {name}")


def matrix_for(tmp_path, *, with_custom_assets=True, **session_options):
    manifest = Manifest(RUN, tmp_path / "run.json")
    pacer = SimpleNamespace(requests=0)
    guard = LiveGuard("write", manifest, require_intents=True, allow_custom_assets=with_custom_assets)
    session = AssetSession(
        guard, pacer, allowed=write_matrix.tools_for(False, False, False, with_custom_assets), **session_options
    )
    matrix = write_matrix.Matrix(
        manifest=manifest, guard=guard, pacer=pacer, session=session, secret="sekrit-key-1",
        clock=lambda: datetime(2099, 10, 2, 10, 15, 0, tzinfo=timezone.utc), lookup_wait=0, scopes_missing=set(),
        with_custom_assets=with_custom_assets,
    )
    return matrix, session, manifest


async def test_the_happy_path_creates_updates_clears_and_checks_a_wrong_client(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)
    await write_matrix.area_custom_assets(matrix)
    assert [name for name, _ in session.calls] == [
        "list_custom_asset_hardware_types", "list_client_locations", "create_custom_asset",
        "update_custom_asset", "update_custom_asset", "update_custom_asset",
    ]
    created = dict(session.calls)["create_custom_asset"]
    assert created == {
        "name": f"{RUN}-asset", "client_id": TEST_CLIENT, "location_id": LOCATION, "hardware_type_id": HARDWARE,
        "serial_number": f"{RUN}-sn", "ip_address": "192.0.2.10", "warranty_end_date": "2100-10-02",
    }
    (post, create_body), (_, rename), (_, clear), (_, wrong) = session.bodies
    assert post == "POST /assets/custom" and create_body["Name"].startswith("MCPTEST-") and create_body["ClientId"] == TEST_CLIENT
    assert rename == {
        "ClientId": TEST_CLIENT, "Name": f"{RUN}-asset v2", "IpAddress": "192.0.2.11", "Description": f"{RUN}-asset: updated",
    }
    assert clear == {"ClientId": TEST_CLIENT, "Description": None, "IpAddress": None, "WarrantyEndDate": None}
    assert wrong["ClientId"] == SECOND  # the negative check names the other test client and Gorelo refuses it
    assert session.asset["Name"] == f"{RUN}-asset v2" and session.asset["SerialNumber"] == f"{RUN}-sn"
    assert session.asset["ClientId"] == TEST_CLIENT and session.asset["IpAddress"] is None
    (record,) = manifest.all_created()
    assert (record.kind, record.id, record.label, record.details) == (
        "custom_asset", session.asset["Id"], f"{RUN}-asset", {"client_id": TEST_CLIENT},
    )
    assert manifest.unresolved_intents() == [] and not matrix.guard.violations
    assert any("refused by Gorelo (HTTP 400)" in note for note in matrix.notes)
    assert any(session.asset["Id"] in note and "stays on the test client" in note for note in matrix.notes)


async def test_the_first_hardware_type_is_used_with_no_id_written_into_the_code(tmp_path):
    matrix, session, _ = matrix_for(tmp_path, hardware=[{"Id": 777, "Name": "Printer"}, {"Id": 3, "Name": "Phone"}])
    await write_matrix.area_custom_assets(matrix)
    assert dict(session.calls)["create_custom_asset"]["hardware_type_id"] == 777
    source = open(write_matrix.__file__, encoding="utf-8").read()
    assert "hardware_type_id=" not in source.split("async def area_custom_assets")[1].split("async def area_invoices")[0]


async def test_without_the_flag_the_area_is_skipped_and_nothing_is_announced_or_sent(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, with_custom_assets=False)
    with pytest.raises(write_matrix.Skipped) as stopped:
        await write_matrix.area_custom_assets(matrix)
    assert str(stopped.value) == write_matrix.CUSTOM_ASSETS_SKIP_REASON
    assert "cannot be deleted through the API" in write_matrix.CUSTOM_ASSETS_SKIP_REASON
    assert session.calls == [] and manifest.all_created() == [] and manifest.unresolved_intents() == []


async def test_the_custom_asset_tools_cannot_be_called_without_the_flag(tmp_path):
    matrix, session, _ = matrix_for(tmp_path, with_custom_assets=False)
    for name in ("create_custom_asset", "update_custom_asset", "list_custom_asset_hardware_types"):
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


async def test_a_wrong_client_that_gorelo_accepts_fails_the_area_and_says_where_to_look(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, wrong_client="accept")
    with pytest.raises(write_matrix.CheckFailed, match=rf"ACCEPTED by Gorelo.*client {SECOND}.*{RUN}-asset"):
        await write_matrix.area_custom_assets(matrix)
    assert len(manifest.all_created()) == 1


def moved_record(manifest):
    (record,) = manifest.all_created()
    return record


async def test_a_wrong_client_that_gorelo_accepts_is_recorded_on_the_second_client(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, wrong_client="accept")
    with pytest.raises(write_matrix.CheckFailed, match="ACCEPTED"):
        await write_matrix.area_custom_assets(matrix)
    assert moved_record(manifest).details == {"client_id": SECOND}
    assert any("MOVED" in note and str(SECOND) in note and "stays on the test client" not in note for note in matrix.notes)
    assert cleanup.custom_asset_residue(SECOND) == f"stays on client {SECOND}, not on the test client {cleanup.CUSTOM_ASSET_RESIDUE_TAIL}"
    assert cleanup.is_known_undeletable(
        SimpleNamespace(kind="custom_asset", outcome=cleanup.custom_asset_residue(SECOND))
    )


@pytest.mark.parametrize("mode", ["server", "timeout", "unconfirmed"])
async def test_a_wrong_client_with_an_unknown_outcome_records_an_unknown_client(tmp_path, mode):
    matrix, session, manifest = matrix_for(tmp_path, wrong_client=mode)
    with pytest.raises(write_matrix.CheckFailed, match=rf"was unexpected.*unknown whether the asset moved to client {SECOND}"):
        await write_matrix.area_custom_assets(matrix)
    assert moved_record(manifest).details == {"client_id": cleanup.CUSTOM_ASSET_CLIENT_UNKNOWN}
    text = cleanup.custom_asset_residue(cleanup.CUSTOM_ASSET_CLIENT_UNKNOWN)
    assert "test client or on the second client" in text and text.endswith(cleanup.CUSTOM_ASSET_RESIDUE_TAIL)


@pytest.mark.parametrize("mode", ["429", "401", "403", "404", "409", "local"])
async def test_a_wrong_client_not_refused_with_400_is_not_evaluated_and_the_asset_stays(tmp_path, mode):
    matrix, session, manifest = matrix_for(tmp_path, wrong_client=mode)
    with pytest.raises(write_matrix.CheckFailed, match="not refused by Gorelo with HTTP 400, so the negative check was not evaluated"):
        await write_matrix.area_custom_assets(matrix)
    assert moved_record(manifest).details == {"client_id": TEST_CLIENT}  # Gorelo did not process it: nothing moved


async def test_the_not_evaluated_text_names_the_status(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, wrong_client="429")
    with pytest.raises(write_matrix.CheckFailed, match=r"\(HTTP 429\)"):
        await write_matrix.area_custom_assets(matrix)


def test_the_residue_text_defaults_to_the_test_client():
    assert cleanup.custom_asset_residue() == cleanup.custom_asset_residue(TEST_CLIENT) == cleanup.CUSTOM_ASSET_RESIDUE


async def test_the_cleanup_outcome_and_the_summary_name_where_a_moved_asset_is(tmp_path, make_settings):
    matrix, session, manifest = matrix_for(tmp_path, wrong_client="accept")
    with pytest.raises(write_matrix.CheckFailed):
        await write_matrix.area_custom_assets(matrix)
    out: list[str] = []
    report = await cleanup.run_cleanup(
        manifest, settings=make_settings(destructive=True), transport=MockGorelo().transport, pace=0, echo=out.append,
        verify_clients=False,
    )
    (record,) = report.undeletable
    assert record.outcome == cleanup.custom_asset_residue(SECOND) and "stays on the test client" not in record.outcome
    summary = write_matrix.MatrixReport(
        run_id=RUN, manifest_path="m.json", cleanup=write_matrix.CleanupReport(run_id=RUN),
        assets=[str(record.id)], asset_clients=write_matrix.residue_asset_clients(manifest),
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
    assert "Gorelo app" in cleanup.CUSTOM_ASSET_RESIDUE and "cannot be deleted through the API" in cleanup.CUSTOM_ASSET_RESIDUE


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
# Tool sets, areas and flags
# --------------------------------------------------------------------------


def test_the_custom_asset_tools_are_named_apart_and_only_the_flag_opens_them():
    assert write_matrix.CUSTOM_ASSET_TOOLS == {
        "list_custom_asset_hardware_types", "create_custom_asset", "update_custom_asset",
    }
    assert not write_matrix.CUSTOM_ASSET_TOOLS & write_matrix.MATRIX_TOOLS
    assert write_matrix.tools_for(False) == write_matrix.tools_for(False, False, False, False)
    assert not write_matrix.CUSTOM_ASSET_TOOLS & write_matrix.tools_for(True, True)
    assert write_matrix.tools_for(False, False, False, True) == (
        write_matrix.MATRIX_TOOLS - write_matrix.ITEM_TOOLS - write_matrix.INVOICE_TOOLS | write_matrix.CUSTOM_ASSET_TOOLS
    )
    assert write_matrix.tools_for(True, True, False, True) == write_matrix.MATRIX_TOOLS | write_matrix.CUSTOM_ASSET_TOOLS
    # the approved invoice run has its own set and the custom flag adds nothing to it
    assert write_matrix.tools_for(False, False, True, True) == write_matrix.APPROVED_INVOICE_TOOLS


def test_the_area_sits_between_items_and_invoices_and_is_selected_by_name():
    keys = [area.key for area in write_matrix.AREAS]
    assert keys.index("items") + 1 == keys.index("custom_assets") == keys.index("invoices") - 1
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
    assert "--with-custom-assets" in out and "CANNOT be deleted through the API" in out


# --------------------------------------------------------------------------
# End to end with the real tools (runs once coder A's tools are registered)
# --------------------------------------------------------------------------


class AssetsFake(FakeGorelo):
    """FakeGorelo plus the custom asset routes: hardware types, the list by ClientIds, POST, and PATCH that refuses a client
    that does not own the asset (HTTP 400), like Gorelo does."""

    def __init__(self, **options):
        super().__init__(**options)
        self.assets: dict[str, dict] = {}
        self.hardware = [{"Id": HARDWARE, "Name": "Laptop"}, {"Id": 43, "Name": "Server"}]
        self.route("GET", "/v1/assets/custom/hardware-types", lambda q: envelope(self.hardware))
        self.route("GET", "/v1/assets/custom", self.list_assets)
        self.route("POST", "/v1/assets/custom", self.post_asset)
        self.route("PATCH", "/v1/assets/custom/{customAssetId}", self.patch_asset)

    def list_assets(self, q):
        wanted = {int(x) for x in q.query.get("ClientIds", "").split(",") if x}
        return paged_envelope([dict(a) for a in self.assets.values() if not wanted or a["ClientId"] in wanted])

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
            return self.missing("custom asset")
        if q.json.get("ClientId") != asset["ClientId"]:
            return error_envelope(400, [("070102", "The asset does not belong to this client.", "ClientId")])
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
async def test_end_to_end_the_third_update_names_the_other_client_and_gorelo_refuses_it(run):
    fake = AssetsFake()
    report = await run(fake, areas=["custom_assets"], with_custom_assets=True)
    assert report.ok
    patches = [r for r in fake.mock.requests if r.method == "PATCH"]
    assert [p.json["ClientId"] for p in patches] == [TEST_CLIENT, TEST_CLIENT, SECOND]
    assert patches[1].json == {"ClientId": TEST_CLIENT, "Description": None, "IpAddress": None, "WarrantyEndDate": None}
    assert fake.assets and next(iter(fake.assets.values()))["ClientId"] == TEST_CLIENT  # the refused update changed nothing


@needs_tools
async def test_end_to_end_a_gorelo_that_accepts_the_wrong_client_fails_the_area_and_the_run(run, lines):
    fake = AssetsFake()
    original = fake.patch_asset

    def lenient(q):
        if q.json.get("ClientId") == SECOND:
            return envelope({"Id": fake.part(q, 4)})  # accepted (and ignored)
        return original(q)

    fake.handlers["PATCH /v1/assets/custom/{customAssetId}"] = lenient
    report = await run(fake, areas=["custom_assets"], with_custom_assets=True)
    found = results(report)["custom_assets"]
    assert found.status == "FAIL" and "ACCEPTED by Gorelo" in found.detail
    assert not report.ok and report.exit_code == 1 and lines[-1] == "result: FAILED"
    assert report.summary["undeletable"] == 1 and report.summary["leftovers"] == 0  # the asset is still a known residue


@needs_tools
async def test_end_to_end_the_area_is_skipped_by_default_in_a_full_run(run):
    fake = AssetsFake()
    report = await run(fake)
    assert results(report)["custom_assets"].status == "skipped" and fake.assets == {}
    assert not any("/assets/custom" in r.path for r in fake.mock.requests)


@needs_tools
async def test_end_to_end_a_run_with_every_optional_area_but_the_approved_invoice_passes(run, lines):
    fake = AssetsFake()
    report = await run(fake, with_items=True, with_invoices=True, with_custom_assets=True)
    assert report.ok, "\n".join(lines)
    assert [r.status for r in report.results if r.area not in ("approved_invoice",)] == ["pass"] * (len(report.results) - 1)
    assert report.summary["undeletable"] == 2  # the uploaded file and the custom asset
    assert area_ops(fake, report)["custom_assets"] == GOLDEN_CUSTOM_ASSETS and GOLDEN["items"]
