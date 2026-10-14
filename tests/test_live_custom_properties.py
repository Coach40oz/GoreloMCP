"""The custom_properties area of the live write matrix (--with-custom-properties), run offline.

Three layers:

* Harness level (always runs): the area is driven against a scripted tool session (PropertySession) that sits in front of the
  REAL LiveGuard (write mode, require_intents=True, allow_custom_properties) and a real manifest. Nothing here depends on the
  code of update_client: the session answers what update_client answers (the client record, with a CustomProperties entry for
  every custom field) or raises the errors the real tool raises, and turns each call into the requests the tool sends, so the
  guard judges the real bodies. With custom fields update_client first reads the client (a GET) and refuses locally an Id the
  client does not list (no PATCH, no second GET); otherwise it sends the PATCH and reads the client again (a second GET), and
  checks the fields on that read.
* The cleanup (always runs): the real _Cleaner behind the real cleanup guard, with a scripted tools client.
* End to end (runs once update_client reads the client before it writes custom fields): run_matrix against a FakeGorelo that
  serves custom fields, with the real tools, the real guard and the real cleanup.

The test client is the one client a live run may write a custom field value to, and only through this area, and only while no
custom field of that client is set (an update that Gorelo applied as a replace would destroy a saved value): the field is
recorded in the manifest BEFORE the first write, every other field must read as at the first read after each step, and the
cleanup clears the field if the run stopped between the set and the clear.
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from conftest import MockGorelo, envelope, error_envelope
from fastmcp import Client
from site_helper import site_config_env  # noqa: F401  (autouse: points GORELO_SITE_CONFIG at invented values)
from test_live_write_matrix import (  # noqa: F401  (fixtures and helpers of the matrix tests)
    GOLDEN_CUSTOM_PROPERTIES,
    RUN,
    TEST_CLIENT,
    FakeGorelo,
    _never_read_the_real_env,
    area_ops,
    lines,
    results,
    run,
)

from scripts.live import _env, cleanup, write_matrix
from scripts.live.guard import CUSTOM_PROPERTY_UNKNOWN_ID, GuardViolation, LiveGuard
from scripts.live.manifest import Manifest
from scripts.live.smoke import GuardTripped, ToolFailed
from server import build_server
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio

BASE = "https://api.usw.gorelo.io/v1"
UNKNOWN = CUSTOM_PROPERTY_UNKNOWN_ID
CLIENT_OP = "GET /v1/clients/{clientId}"
FIELD = 31  # the field the area picks in the default tenant: the unset text field with a default and the lowest Id
OTHER = 35  # a field of the default tenant that the area never sends (an unset number with a default)
SHOWN_FIRST = "the default owner"  # the default Value that FIELD shows while it is unset


def field(ident, name, kind, *, value=None, is_set=False):
    """One CustomPropertyValue as Gorelo answers it (contract 1b1c0cec0e9a)."""
    return {
        "Id": ident, "Variable": name.lower().replace(" ", "_"), "Name": name, "Type": {"Id": len(kind), "Name": kind},
        "Value": value, "IsSet": is_set,
    }


def default_tenant():
    """Four client custom fields, none of them set: a text field with no default and the lowest Id (20), the pick (31: text, with a
    default), a number (35) and a text field with no default (40)."""
    return [
        field(40, "Support tier", "Text"),
        field(OTHER, "Seats", "Number", value="5"),
        field(20, "Notes", "Large Text"),
        field(FIELD, "Contract owner", "Text", value=SHOWN_FIRST),
    ]


def alarm_text(ident, name):
    """update_client's error for a custom field that changed although it was not in the call, as the tool words it (the end to end
    tests drive the real tool, so this copy is checked against it): it quotes the name of the field and, for a field that held a
    value, the value."""
    return (
        "Gorelo returned an unexpected response for update_client: PATCH /v1/clients/{clientId}: the update was sent, but a read of "
        "the client after it does not show custom fields that were not in this call as they were before it "
        f'(custom field {ident} ("{name}") was unset, now reads set). Tell the user at once, and ask before putting anything back: '
        "another user or an automation may have made the change. Gorelo answered with success; the update may have been applied, "
        "so verify it with a read before repeating it (get_client(client_id=9501))"
    )


# --------------------------------------------------------------------------
# Harness level: a scripted session in front of the real guard and manifest
# --------------------------------------------------------------------------


class PropertySession:
    """Stands in for ToolSession. get_client and update_client answer from a small state of custom fields; each call becomes the
    requests the real tool sends, judged by the real guard (a GET included):

    * get_client: a GET of the client;
    * update_client with custom fields: a GET of the client first, then, when the client lists every Id that was sent, the PATCH
      of the entries (the saved ones first, then the cleared ones as a JSON null) and a second GET of the client (a PATCH that
      Gorelo refuses ends the call before it). The tool's own check that the fields it did not send are as they were is not
      emulated: the area makes that check itself, and the tests damage answers and reads to prove it.

    unknown: what happens when update_client names an Id that is no custom field of the client:
      "local" the tool refuses after its read: no HTTP status, the Id in the text, no PATCH (the one request is that GET);
      "wrong-text" the same, but the text does not name the Id; "no-read" the tool refuses before any request;
      "read-fails" the read is answered with HTTP 500; "timeout" the read gets no answer; "unusable-read" the read is answered but
      its CustomProperties cannot be used (the tool's shape error, which says nothing was written);
      "sends-patch" the tool sends the PATCH with the Id anyway (the guard refuses it); "slips-a-write" the tool sends a PATCH of
      another recorded field (the guard allows it), then refuses locally; "accepts" the tool answers with a record.
    replace: every PATCH also resets the custom fields it did not send (an update applied as a replace).
    """

    def __init__(self, guard, pacer, manifest, *, allowed, fields=None, unknown="local", replace=False):
        self.guard, self.pacer, self.manifest, self.allowed = guard, pacer, manifest, allowed
        self.fields = default_tenant() if fields is None else fields
        self.unknown, self.replace = unknown, replace
        self.calls: list[tuple[str, dict]] = []
        self.requests: list[str] = []  # the method of every request that reached the guard and was allowed
        self.bodies: list[tuple[str, dict]] = []
        self.reads = 0  # get_client calls of the area (the tool's own read before a write is not one of them)
        self.recorded_at_first_write: set | None = None
        # knobs that damage an answer: "read" and "answer" are functions of the record (they change it in place or return another),
        # "read_at" the numbers of the get_client calls (counted from 1) and "answer_at" the numbers of the PATCHes (counted from 1)
        # they apply to; without them every read and every answer is damaged
        self.hurt: dict[str, object] = {}
        self.required: set[int] = set()  # fields Gorelo refuses to clear (a required field with no default)

    # -- the state ---------------------------------------------------------

    def record(self):
        rows = [{key: value for key, value in entry.items() if not key.startswith("_")} for entry in self.fields]
        return {"Id": TEST_CLIENT, "Name": "the test client", "CustomProperties": rows}

    def entry(self, ident):
        return next((e for e in self.fields if e["Id"] == ident), None)

    def _send(self, method, path, body):
        request = httpx.Request(method, BASE + path, json=body)
        before = len(self.guard.violations)
        try:
            self.guard.check(request)
        except GuardViolation as exc:
            assert len(self.guard.violations) > before
            raise GuardTripped(str(exc)) from None
        self.pacer.requests += 1
        self.requests.append(method)
        if method != "GET":
            if self.recorded_at_first_write is None:
                self.recorded_at_first_write = set(self.manifest.ids("custom_property"))
            self.bodies.append((f"{method} {path}", body or {}))

    # -- the tools -----------------------------------------------------------

    async def call(self, name, arguments=None):
        arguments = dict(arguments or {})
        if name not in self.allowed:
            raise RuntimeError(f"{name} is not one of the tools this script may call")
        self.calls.append((name, arguments))
        client = arguments["client_id"]
        if name == "get_client":
            self.reads += 1
            self._send("GET", f"/clients/{client}", None)
            return self.damaged("read", self.record(), self.reads)
        assert name == "update_client", name
        # what update_client sends: the saved entries first, then the cleared ones as a JSON null (the design of update_client)
        entries = [{"Id": item["id"], "Value": item["value"]} for item in arguments.get("custom_properties") or []]
        entries += [{"Id": ident, "Value": None} for ident in arguments.get("clear_custom_property_ids") or []]
        missing = [item for item in entries if self.entry(item["Id"]) is None]
        if missing and self.unknown == "no-read":
            raise ToolFailed("update_client", self.local_text(missing[0]["Id"], client), sent=False)
        self._send("GET", f"/clients/{client}", None)  # the tool reads the client before it writes custom fields
        if missing:
            return self.unknown_field(missing[0], client)
        self._send("PATCH", f"/clients/{client}", {"CustomProperties": entries})
        sent = len(self.bodies)
        for item in entries:
            ident = item["Id"]
            current = self.entry(ident)
            if item["Value"] is None:
                if ident in self.required:
                    raise ToolFailed(
                        "update_client",
                        "Gorelo rejected update_client (HTTP 400, code 070101): custom_properties (item 1, id): the field is "
                        "required and would be left with no value",
                        sent=True,
                    )
                current.update(Value=current.get("_default"), IsSet=False)
            else:
                current.setdefault("_default", current["Value"])
                current.update(Value=item["Value"], IsSet=True)
        if self.replace:
            sent_ids = {item["Id"] for item in entries}
            for other in self.fields:
                if other["Id"] not in sent_ids and other["IsSet"]:
                    other.update(Value=other.get("_default"), IsSet=False)
        self._send("GET", f"/clients/{client}", None)  # the tool reads the client again after the PATCH
        return self.damaged("answer", self.record(), sent)

    def damaged(self, kind, answer, count):
        function = self.hurt.get(kind)
        if function is None or count not in self.hurt.get(f"{kind}_at", {count}):
            return answer
        changed = function(answer)
        return answer if changed is None else changed

    @staticmethod
    def local_text(ident, client):
        """What the tool says when the client does not list an Id it was given (it names the parameter, the item and the Id)."""
        return f"custom_properties (item 1, id): custom field {ident} is not a custom field of client {client} (get_client lists them)"

    def unknown_field(self, item, client):
        mode, ident = self.unknown, item["Id"]
        if mode == "read-fails":
            raise ToolFailed("update_client", "Gorelo rejected update_client (HTTP 500, code 1): the read failed", sent=True)
        if mode == "timeout":
            raise ToolFailed(
                "update_client",
                "Gorelo did not confirm update_client (the request timed out). The change may or may not have been applied. "
                "Verify with a read before retrying.",
                sent=True,
            )
        if mode == "unusable-read":
            raise ToolFailed(
                "update_client",
                "Gorelo returned an unexpected response for update_client: GET /v1/clients/{clientId}: the client's CustomProperties "
                "is null, not a list; nothing was written, because the state of the custom fields before the update is unknown",
                sent=True,
            )
        if mode == "sends-patch":
            self._send("PATCH", f"/clients/{client}", {"CustomProperties": [item]})  # the guard refuses it: GuardTripped
        if mode == "slips-a-write":  # the guard refuses a write to the cleaned field, so the tool writes another recorded one
            self.manifest.created(
                "custom_property", 99, self.manifest.label("custom property 99"),
                {"client_id": TEST_CLIENT, "name": "x", "type": "Text", "was_set": False, "value": RUN},
            )
            self._send("PATCH", f"/clients/{client}", {"CustomProperties": [{"Id": 99, "Value": None}]})
        if mode == "accepts":
            return self.record()
        text = "custom_properties (item 1, id): refused" if mode == "wrong-text" else self.local_text(ident, client)
        raise ToolFailed("update_client", text, sent=True)


def matrix_for(tmp_path, *, with_custom_properties=True, **session_options):
    manifest = Manifest(RUN, tmp_path / "run.json")
    pacer = SimpleNamespace(requests=0)
    guard = LiveGuard("write", manifest, require_intents=True, allow_custom_properties=with_custom_properties)
    session = PropertySession(guard, pacer, manifest, allowed=write_matrix.tools_for(False), **session_options)
    matrix = write_matrix.Matrix(
        manifest=manifest, guard=guard, pacer=pacer, session=session, secret="sekrit-key-1",
        clock=lambda: datetime(2099, 10, 2, 10, 15, 0, tzinfo=timezone.utc), lookup_wait=0, scopes_missing=set(),
        with_custom_properties=with_custom_properties,
    )
    return matrix, session, manifest


def only_record(manifest):
    (record,) = manifest.all_created()
    return record


def stuck(entry_id, **changes):
    """A function that hurts an answer: the entry of one custom field takes these values."""

    def hurt(answer):
        for row in answer["CustomProperties"]:
            if row["Id"] == entry_id:
                row.update(changes)

    return hurt


def without(entry_id):
    """A function that hurts an answer: the entry of one custom field is no longer listed."""

    def hurt(answer):
        answer["CustomProperties"] = [row for row in answer["CustomProperties"] if row["Id"] != entry_id]

    return hurt


def with_new_field(ident):
    """A function that hurts an answer: a custom field that the first read did not list shows up."""

    def hurt(answer):
        answer["CustomProperties"].append(field(ident, "Newcomer", "Text"))

    return hurt


async def test_the_happy_path_sets_clears_and_has_the_tool_refuse_an_id_that_is_no_field_before_any_patch(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)
    await write_matrix.area_custom_properties(matrix)
    assert [name for name, _ in session.calls] == [
        "get_client", "update_client", "get_client", "update_client", "get_client", "update_client", "get_client",
    ]
    assert session.calls[0][1] == {"client_id": TEST_CLIENT}
    assert session.calls[1] == ("update_client", {"client_id": TEST_CLIENT, "custom_properties": [{"id": FIELD, "value": RUN}]})
    assert session.calls[3] == ("update_client", {"client_id": TEST_CLIENT, "clear_custom_property_ids": [FIELD]})
    assert session.calls[5] == ("update_client", {"client_id": TEST_CLIENT, "custom_properties": [{"id": UNKNOWN, "value": RUN}]})
    # every update_client call is a GET, its PATCH and a second GET; the one with the unknown Id is its first GET alone
    assert session.requests == ["GET", "GET", "PATCH", "GET", "GET", "GET", "PATCH", "GET", "GET", "GET", "GET"]
    assert [body for _, body in session.bodies] == [
        {"CustomProperties": [{"Id": FIELD, "Value": RUN}]},
        {"CustomProperties": [{"Id": FIELD, "Value": None}]},  # a JSON null: how Gorelo clears a saved value
    ]
    assert matrix.pacer.requests == 11 and matrix.guard.allowed_count == 11 and matrix.guard.writes_allowed == 2
    assert not matrix.guard.violations
    # the field is back to what it was: unset, showing its default; nothing else was touched
    assert session.entry(FIELD)["IsSet"] is False and session.entry(FIELD)["Value"] == SHOWN_FIRST
    assert {e["Id"]: (e["IsSet"], e["Value"]) for e in session.fields if e["Id"] != FIELD} == {
        40: (False, None), OTHER: (False, "5"), 20: (False, None),
    }
    record = only_record(manifest)
    assert (record.kind, record.id, record.label) == ("custom_property", FIELD, f"{RUN} custom property {FIELD}")
    assert record.details == {
        "client_id": TEST_CLIENT, "name": "Contract owner", "type": "Text", "was_set": False, "value": RUN, "value_before": SHOWN_FIRST,
    }
    assert record.cleaned and record.outcome == "cleared by the write matrix (update_client clear_custom_property_ids)"
    assert manifest.unresolved_intents() == []
    assert matrix.notes == [
        f"custom field {FIELD} (Type 'Text') of client {TEST_CLIENT}: unset before the run and picked because it has a default (its "
        "Value is not null), so a clear can restore it: the lowest Id among such fields; recorded in the manifest before the first write",
        f"custom field {FIELD}: set to the run id, read, cleared, read; the other 3 custom field(s) read as at the first read after "
        "every step",
        f"negative check, update_client with the Id {UNKNOWN}, which is not a client custom field: the tool refused it itself after "
        "reading the client: its one request was that GET, no PATCH was sent; the last read shows the same 4 custom fields as the first",
    ]
    notes = " | ".join(matrix.notes)
    assert "Contract owner" not in notes and SHOWN_FIRST not in notes and "Seats" not in notes  # ids and kinds, never a name or a value
    assert "PropertyName" not in notes


async def test_the_field_is_recorded_in_the_manifest_before_the_first_write(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)
    await write_matrix.area_custom_properties(matrix)
    assert session.recorded_at_first_write == {FIELD}
    # and the guard enforces it: without that record the very same write is refused
    bare, session2, manifest2 = matrix_for(tmp_path / "other")
    with pytest.raises(GuardTripped, match=f"custom field {FIELD} is not one the run recorded in the manifest"):
        await session2.call("update_client", {"client_id": TEST_CLIENT, "custom_properties": [{"id": FIELD, "value": RUN}]})
    assert manifest2.all_created() == [] and session2.bodies == [] and session2.requests == ["GET"]


async def test_the_note_that_the_field_was_recorded_comes_before_the_writes_and_the_one_that_it_was_cleared_after_the_check(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)
    seen = []
    real = session.call

    async def spying(name, arguments=None):
        seen.append((name, len(matrix.notes)))  # how many notes existed when each call was made
        return await real(name, arguments)

    session.call = spying
    await write_matrix.area_custom_properties(matrix)
    first_update = next(count for name, count in seen if name == "update_client")
    assert first_update == 1  # "recorded in the manifest" was already written before the first write
    assert "recorded in the manifest before the first write" in matrix.notes[0] and "cleared" not in matrix.notes[0]
    assert "set to the run id, read, cleared, read" in matrix.notes[1]
    assert [count for name, count in seen if name == "get_client"] == [0, 1, 1, 2]  # the new note only after the clear check


@pytest.mark.parametrize(
    "hurt",
    [
        pytest.param({"answer": stuck(FIELD, IsSet=False), "answer_at": {1}}, id="the-set-answer-does-not-show-it"),
        pytest.param({"read": stuck(FIELD, IsSet=False, Value=SHOWN_FIRST), "read_at": {2}}, id="the-read-after-the-set-does-not-show-it"),
        pytest.param({"answer": stuck(FIELD, IsSet=True), "answer_at": {2}}, id="the-clear-answer-shows-it-still-set"),
        pytest.param({"read": stuck(FIELD, IsSet=False, Value=RUN), "read_at": {3}}, id="the-read-after-the-clear-shows-the-text"),
    ],
)
async def test_the_note_that_the_field_was_set_read_cleared_read_is_not_written_when_a_check_fails(tmp_path, hurt):
    matrix, session, manifest = matrix_for(tmp_path)
    session.hurt.update(hurt)
    with pytest.raises(write_matrix.CheckFailed):
        await write_matrix.area_custom_properties(matrix)
    assert len(matrix.notes) == 1 and "recorded in the manifest before the first write" in matrix.notes[0]
    assert not any("read, cleared, read" in note for note in matrix.notes)
    assert not only_record(manifest).cleaned


async def test_without_the_flag_the_area_is_skipped_and_nothing_is_read_recorded_or_sent(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, with_custom_properties=False)
    with pytest.raises(write_matrix.Skipped) as stopped:
        await write_matrix.area_custom_properties(matrix)
    assert str(stopped.value) == write_matrix.CUSTOM_PROPERTIES_SKIP_REASON
    assert "the one write the live guard otherwise never allows on that client" in write_matrix.CUSTOM_PROPERTIES_SKIP_REASON
    assert session.calls == [] and manifest.all_created() == [] and matrix.pacer.requests == 0


async def test_without_the_flag_the_guard_refuses_a_custom_field_write_to_the_test_client_even_for_a_recorded_field(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, with_custom_properties=False)
    manifest.created("custom_property", FIELD, manifest.label("custom property"), {
        "client_id": TEST_CLIENT, "name": "x", "type": "Text", "was_set": False, "value": RUN})
    with pytest.raises(GuardTripped, match=f"client {TEST_CLIENT} is one of the records the live tests run on and is never changed"):
        await session.call("update_client", {"client_id": TEST_CLIENT, "custom_properties": [{"id": FIELD, "value": RUN}]})


async def test_a_tenant_without_client_custom_fields_is_a_skip_after_one_read(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, fields=[])
    with pytest.raises(write_matrix.Skipped, match=r"^no client custom fields are defined \(get_client of the test client lists none\); nothing was written$"):
        await write_matrix.area_custom_properties(matrix)
    assert [name for name, _ in session.calls] == ["get_client"] and manifest.all_created() == [] and session.bodies == []


# -- while a custom field is set: nothing for a replace to destroy -----------------------------------------


@pytest.mark.parametrize(
    "fields, ids",
    [
        pytest.param(
            [field(1, "Tier", "Text", value="Gold", is_set=True), field(2, "Seats", "Number"), field(3, "Notes", "Text")], "1", id="the-text-field-is-set"
        ),
        pytest.param(
            [field(1, "Tier", "Text"), field(2, "Seats", "Number", value="SECRET-5", is_set=True), field(3, "Notes", "Text")], "2", id="a-number-is-set"
        ),
        pytest.param(
            [field(7, "Renewal", "Date", value="2030-01-01", is_set=True), field(4, "VIP", "Checkbox", value="CHK-1", is_set=True), field(9, "Notes", "Text")],
            "4, 7", id="two-are-set",
        ),
    ],
)
async def test_a_tenant_where_any_custom_field_is_set_is_a_skip_that_names_the_ids_and_writes_nothing(tmp_path, fields, ids):
    matrix, session, manifest = matrix_for(tmp_path, fields=fields)
    with pytest.raises(write_matrix.Skipped) as stopped:
        await write_matrix.area_custom_properties(matrix)
    reason = str(stopped.value)
    assert reason == (
        f"custom field(s) {ids} of the test client are set (IsSet true), so nothing was written: the area runs only while none is, "
        "so that an update applied as a replace would have nothing to destroy"
    )
    for entry in fields:
        assert entry["Name"] not in reason and not (entry["Value"] and entry["Value"] in reason)  # ids only: no name, no value
    (note,) = matrix.notes  # every field with its Id, kind and IsSet, never a value
    assert note.startswith("custom fields of the test client (Id, Type, IsSet): ")
    for entry in fields:
        assert f"{entry['Id']} ({entry['Type']['Name']}, IsSet {str(entry['IsSet']).lower()})" in note
    assert "SECRET" not in note and "Gold" not in note
    assert [name for name, _ in session.calls] == ["get_client"] and manifest.all_created() == [] and session.bodies == []
    assert matrix.pacer.requests == 1 and matrix.guard.writes_allowed == 0


async def test_a_long_list_of_set_fields_is_cut_in_the_reason_and_the_note_names_all(tmp_path):
    fields = [field(ident, f"Field {ident}", "Number", value="x", is_set=True) for ident in range(1, 21)]
    matrix, session, manifest = matrix_for(tmp_path, fields=fields)
    with pytest.raises(write_matrix.Skipped) as stopped:
        await write_matrix.area_custom_properties(matrix)
    reason = str(stopped.value)
    assert reason.startswith("custom field(s) 1, 2, 3, 4, 5, 6, 7, 8 and 12 more of the test client are set (IsSet true)")
    assert len(reason) < write_matrix.DETAIL_LIMIT
    (note,) = matrix.notes
    assert "20 (Number, IsSet true)" in note and "more" not in note


async def test_an_update_applied_as_a_replace_has_nothing_to_destroy_in_a_tenant_the_area_accepts(tmp_path):
    """With no custom field set, a replace resets nothing that was there: the area passes. With one set it is a skip, so the
    same replace never gets the chance to wipe it (the tenant below is not written at all)."""
    matrix, session, manifest = matrix_for(tmp_path, replace=True)
    await write_matrix.area_custom_properties(matrix)
    assert only_record(manifest).cleaned and matrix.guard.writes_allowed == 2
    held = [field(1, "Tier", "Text", value="Gold", is_set=True), field(2, "Notes", "Text")]
    matrix2, session2, manifest2 = matrix_for(tmp_path / "held", fields=held, replace=True)
    with pytest.raises(write_matrix.Skipped, match=r"custom field\(s\) 1 of the test client are set"):
        await write_matrix.area_custom_properties(matrix2)
    assert session2.bodies == [] and session2.fields[0]["Value"] == "Gold" and session2.fields[0]["IsSet"] is True


# -- the field that is picked ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields, picked, with_default",
    [
        pytest.param(
            [field(90, "Last", "Text", value="d90"), field(12, "Seats", "Number"), field(70, "Large", "LARGE TEXT", value="d70"),
             field(55, "First with default", "Multi-line text", value="d55"), field(30, "No default", "Text")],
            55, True, id="the-lowest-id-with-a-default-beats-a-lower-id-without",
        ),
        pytest.param(
            [field(90, "Last", "Text"), field(12, "Seats", "Number", value="5"), field(70, "Large", "LARGE TEXT"), field(55, "Mid", "Multi-line text")],
            55, False, id="none-has-a-default-so-the-lowest-id",
        ),
        pytest.param([field(9, "Only", "Text", value="")], 9, True, id="an-empty-default-is-a-default"),
    ],
)
async def test_the_field_is_the_unset_text_field_with_a_default_and_the_lowest_id_else_the_lowest_id(tmp_path, fields, picked, with_default):
    matrix, session, manifest = matrix_for(tmp_path, fields=fields)
    await write_matrix.area_custom_properties(matrix)
    assert session.calls[1][1]["custom_properties"] == [{"id": picked, "value": RUN}]
    assert only_record(manifest).id == picked
    default_case = "it has a default (its Value is not null), so a clear can restore it: the lowest Id among such fields"
    no_default_case = (
        "no unset text field has a default, so it is the lowest Id; a required field cannot be cleared back to nothing and "
        "would stay set (a leftover)"
    )
    assert (default_case in matrix.notes[0]) is with_default and (no_default_case in matrix.notes[0]) is not with_default
    assert only_record(manifest).details["value_before"] == next(e["Value"] for e in fields if e["Id"] == picked)


def test_unset_text_field_applies_the_rule_to_the_fields_it_is_given():
    entries = [
        {"Id": 8, "IsSet": False, "Type": {"Id": 1, "Name": "Number"}},
        {"Id": 6, "IsSet": True, "Type": {"Id": 2, "Name": "Text"}, "Value": "x"},
        {"Id": 4, "IsSet": False, "Type": {"Id": 2, "Name": "Large text"}},
        {"Id": 2, "IsSet": False, "Type": {"Id": 2, "Name": "Text"}},
    ]
    assert write_matrix.unset_text_field(entries)["Id"] == 2  # no Value anywhere: the lowest Id (a record may not carry the key)
    entries.append({"Id": 5, "IsSet": False, "Type": {"Id": 2, "Name": "text"}, "Value": "a default"})
    assert write_matrix.unset_text_field(entries)["Id"] == 5  # the only one with a default, although 2 has the lowest Id
    assert write_matrix.unset_text_field(entries[:1]) is None and write_matrix.unset_text_field([]) is None


@pytest.mark.parametrize(
    "fields",
    [
        [field(1, "Seats", "Number"), field(2, "Renewal", "Date"), field(3, "VIP", "Checkbox")],  # no text field
        [field(1, "Seats", "Number", value="5"), field(2, "Renewal", "Date", value="2030-01-01")],  # defaults, still no text field
    ],
)
async def test_no_unset_text_field_is_a_skip_that_lists_each_field_with_its_id_type_and_isset_and_no_value(tmp_path, fields):
    matrix, session, manifest = matrix_for(tmp_path, fields=fields)
    with pytest.raises(write_matrix.Skipped) as stopped:
        await write_matrix.area_custom_properties(matrix)
    reason = str(stopped.value)
    assert reason.startswith("no custom field of the test client is an unset text field, so nothing was written; its fields (Id, Type, IsSet): ")
    for entry in fields:
        assert f"{entry['Id']} ({entry['Type']['Name']}, IsSet {str(entry['IsSet']).lower()})" in reason
    for entry in fields:
        assert entry["Name"] not in reason and not (entry["Value"] and entry["Value"] in reason)  # no name, no value
    assert [name for name, _ in session.calls] == ["get_client"] and manifest.all_created() == [] and session.bodies == []


async def test_a_long_list_of_fields_in_the_reason_is_cut_and_says_how_many_more_while_the_note_names_all(tmp_path):
    fields = [field(ident, f"Field {ident}", "Number") for ident in range(1, 21)]
    matrix, session, manifest = matrix_for(tmp_path, fields=fields)
    with pytest.raises(write_matrix.Skipped) as stopped:
        await write_matrix.area_custom_properties(matrix)
    reason = str(stopped.value)
    assert write_matrix.FIELDS_SHOWN == 8
    assert "8 (Number, IsSet false) and 12 more" in reason and "9 (Number" not in reason
    assert len(reason) < write_matrix.DETAIL_LIMIT  # the summary cuts a detail at DETAIL_LIMIT: the reason fits whole
    (note,) = matrix.notes
    assert note.startswith("custom fields of the test client (Id, Type, IsSet): 1 (Number, IsSet false), 2 (Number")
    assert "20 (Number, IsSet false)" in note and "more" not in note  # the note names every one of them


@pytest.mark.parametrize(
    "kind, takes_it",
    [("Text", True), ("text", True), ("TEXT", True), ("Large Text", True), ("Textarea", True), ("Context", True),  # contains "text"
     ("Number", False), ("Date", False), ("Dropdown", False), ("Checkbox", False), ("Url", False)],
)
async def test_a_type_name_that_contains_text_in_any_case_is_a_text_field(tmp_path, kind, takes_it):
    matrix, session, manifest = matrix_for(tmp_path, fields=[field(9, "Only", kind)])
    if takes_it:
        await write_matrix.area_custom_properties(matrix)
        assert only_record(manifest).id == 9
    else:
        with pytest.raises(write_matrix.Skipped, match="no custom field of the test client is an unset text field"):
            await write_matrix.area_custom_properties(matrix)
        assert manifest.all_created() == []


@pytest.mark.parametrize(
    "damage, said",
    [
        pytest.param(lambda r: r.pop("CustomProperties"), "get_client: CustomProperties is missing", id="no-key"),
        pytest.param(lambda r: r.update(CustomProperties=None), "get_client: CustomProperties is not a list \\(NoneType\\)", id="null"),
        pytest.param(lambda r: r.update(CustomProperties=[{"Id": 1}]), "get_client: CustomProperties item 1: Name is not text", id="a-field-with-no-name"),
        pytest.param(
            lambda r: r["CustomProperties"][0].update(IsSet="no"), "get_client: CustomProperties item 1: IsSet is not a boolean", id="isset-text"
        ),
    ],
)
async def test_a_client_record_without_the_published_custom_fields_is_api_drift_and_fails_before_any_write(tmp_path, damage, said):
    matrix, session, manifest = matrix_for(tmp_path)
    session.hurt["read"] = damage
    with pytest.raises(write_matrix.CheckFailed, match=said):
        await write_matrix.area_custom_properties(matrix)
    assert manifest.all_created() == [] and session.bodies == []


async def test_an_id_the_negative_check_would_send_that_is_a_real_field_stops_the_area_before_anything_is_written(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, fields=[*default_tenant(), field(UNKNOWN, "Impossible", "Number")])
    with pytest.raises(write_matrix.CheckFailed, match=f"custom field {UNKNOWN}, the Id the negative check sends, is a client custom field here"):
        await write_matrix.area_custom_properties(matrix)
    assert manifest.all_created() == [] and session.bodies == []


# -- the set ---------------------------------------------------------------


@pytest.mark.parametrize(
    "answer, said",
    [
        pytest.param(stuck(FIELD, IsSet=False), r"update_client custom field 31 IsSet: expected True, got False", id="isset-false"),
        pytest.param(stuck(FIELD, Value="something else"), "update_client custom field 31 Value: expected the text the run sent, got str", id="another-value"),
        pytest.param(stuck(FIELD, Value=None), "update_client custom field 31 Value: expected the text the run sent, got null", id="null-value"),
        pytest.param(without(FIELD), "has 0 entries for custom field 31", id="no-entry"),
        pytest.param(lambda a: a["CustomProperties"].append(dict(a["CustomProperties"][-1])), "CustomProperties item 5: Id repeats an earlier custom field", id="a-repeated-entry"),
    ],
)
async def test_a_set_that_the_answer_does_not_show_fails_the_area_and_leaves_the_record_for_the_cleanup(tmp_path, answer, said):
    matrix, session, manifest = matrix_for(tmp_path)
    session.hurt.update(answer=answer, answer_at={1})
    with pytest.raises(write_matrix.CheckFailed, match=said):
        await write_matrix.area_custom_properties(matrix)
    record = only_record(manifest)
    assert not record.cleaned and record.details["was_set"] is False  # the cleanup reads the client and clears what is the run's
    assert len(session.bodies) == 1 and [n for n, _ in session.calls] == ["get_client", "update_client"]  # nothing after the failed check


async def test_a_fresh_read_after_the_set_that_does_not_show_it_fails_the_area_and_stops(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)
    session.hurt.update(read=stuck(FIELD, IsSet=False, Value=SHOWN_FIRST), read_at={2})
    with pytest.raises(write_matrix.CheckFailed, match="get_client after the set custom field 31 IsSet: expected True, got False"):
        await write_matrix.area_custom_properties(matrix)
    assert not only_record(manifest).cleaned and len(session.bodies) == 1


async def test_a_set_that_is_refused_by_gorelo_fails_the_area_and_the_record_stays_for_the_cleanup(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)

    async def refuses(name, arguments=None):
        if name == "update_client":
            raise ToolFailed(name, "Gorelo rejected update_client (HTTP 400, code 070101): custom_properties (item 1, value): too long", sent=True)
        return await real(name, arguments)

    real, session.call = session.call, refuses
    with pytest.raises(ToolFailed, match="custom_properties \\(item 1, value\\): too long"):
        await write_matrix.area_custom_properties(matrix)
    assert not only_record(manifest).cleaned


# -- every other custom field must read as at the first read, after every step ---------------------------------


STAGES = [
    # (where the damage is made, the numbers it applies to, the label of the check, the PATCHes sent so far, whether the record is cleaned by then)
    pytest.param("answer", {1}, "update_client", 1, False, id="set-answer"),
    pytest.param("read", {2}, "get_client after the set", 1, False, id="read-after-the-set"),
    pytest.param("answer", {2}, "update_client \\(clear\\)", 2, False, id="clear-answer"),
    pytest.param("read", {3}, "get_client after the clear", 2, False, id="read-after-the-clear"),
    pytest.param("read", {4}, "get_client after the negative check", 2, True, id="last-read"),
]
DAMAGES = [
    pytest.param(stuck(OTHER, IsSet=True, Value="LEAKED-9"), [OTHER], id="another-field-became-set"),
    pytest.param(stuck(OTHER, Value="LEAKED-9"), [OTHER], id="another-field-shows-another-value"),
    pytest.param(without(OTHER), [OTHER], id="another-field-is-gone"),
    pytest.param(with_new_field(77), [77], id="a-field-is-new"),
]


@pytest.mark.parametrize("kind, at, label, patches, cleaned", STAGES)
@pytest.mark.parametrize("damage, ids", DAMAGES)
async def test_a_change_to_any_other_custom_field_fails_the_area_at_once_naming_the_ids_only(tmp_path, kind, at, label, patches, cleaned, damage, ids):
    matrix, session, manifest = matrix_for(tmp_path)
    session.hurt.update({kind: damage, f"{kind}_at": at})
    with pytest.raises(write_matrix.CheckFailed) as failed:
        await write_matrix.area_custom_properties(matrix)
    shown = ", ".join(str(ident) for ident in ids)
    assert re.fullmatch(
        rf"{label}: custom field\(s\) {shown} of client {TEST_CLIENT} do not read as at the first read of this run "
        r"\(IsSet, Value or presence changed\); check them in the Gorelo app",
        str(failed.value),
    ), str(failed.value)
    for private in ("Seats", "LEAKED", "Newcomer", "Contract owner", SHOWN_FIRST):  # ids only: never a name or a value
        assert private not in str(failed.value)
    assert len(session.bodies) == patches and only_record(manifest).cleaned is cleaned
    assert len(matrix.notes) == (2 if cleaned else 1)  # only the notes of the steps that passed were written


@pytest.mark.parametrize("kind, at, label", [("answer", {2}, "update_client \\(clear\\)"), ("read", {3}, "get_client after the clear")])
async def test_a_chosen_field_that_is_not_back_at_its_first_value_after_the_clear_fails_the_area(tmp_path, kind, at, label):
    matrix, session, manifest = matrix_for(tmp_path)
    session.hurt.update({kind: stuck(FIELD, Value="another default"), f"{kind}_at": at})  # IsSet false, and not the text: still wrong
    with pytest.raises(write_matrix.CheckFailed, match=rf"^{label}: custom field\(s\) {FIELD} of client {TEST_CLIENT} do not read as at the first read"):
        await write_matrix.area_custom_properties(matrix)
    assert not only_record(manifest).cleaned


async def test_a_chosen_field_without_a_default_must_come_back_without_one(tmp_path):
    fields = [field(5, "Note", "Text"), field(6, "Seats", "Number")]
    matrix, session, manifest = matrix_for(tmp_path, fields=fields)
    session.hurt.update(read=stuck(5, Value=""), read_at={3})  # null before the run, an empty text after the clear
    with pytest.raises(write_matrix.CheckFailed, match=r"get_client after the clear: custom field\(s\) 5 of client"):
        await write_matrix.area_custom_properties(matrix)
    assert only_record(manifest).details["value_before"] is None


# -- the clear ---------------------------------------------------------------


@pytest.mark.parametrize(
    "answer, said",
    [
        pytest.param(stuck(FIELD, IsSet=True), "update_client \\(clear\\) custom field 31 IsSet: expected False, got True", id="still-set"),
        pytest.param(stuck(FIELD, IsSet=False, Value=RUN), "update_client \\(clear\\) custom field 31: IsSet is false but Value still shows the text the run sent", id="stale-value"),
    ],
)
async def test_a_clear_that_the_answer_does_not_show_fails_the_area_and_leaves_the_record_for_the_cleanup(tmp_path, answer, said):
    matrix, session, manifest = matrix_for(tmp_path)
    session.hurt.update(answer=answer, answer_at={2})
    with pytest.raises(write_matrix.CheckFailed, match=said):
        await write_matrix.area_custom_properties(matrix)
    assert not only_record(manifest).cleaned and len(session.bodies) == 2


async def test_a_fresh_read_after_the_clear_that_still_shows_the_text_fails_the_area_and_leaves_the_record(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)
    session.hurt.update(read=stuck(FIELD, IsSet=False, Value=RUN), read_at={3})
    with pytest.raises(write_matrix.CheckFailed, match="get_client after the clear custom field 31: IsSet is false but Value still shows"):
        await write_matrix.area_custom_properties(matrix)
    assert not only_record(manifest).cleaned and len(session.bodies) == 2


async def test_a_required_field_that_gorelo_will_not_clear_fails_the_area_and_stays_a_leftover_for_the_cleanup(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)
    session.required.add(FIELD)
    with pytest.raises(ToolFailed, match="the field is required and would be left with no value"):
        await write_matrix.area_custom_properties(matrix)
    assert not only_record(manifest).cleaned
    assert session.entry(FIELD)["IsSet"] is True  # still set: this is what the cleanup then reports


async def test_after_the_clear_the_guard_takes_no_further_write_for_the_field_a_null_included(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)
    await write_matrix.area_custom_properties(matrix)
    assert only_record(manifest).cleaned
    for arguments in ({"custom_properties": [{"id": FIELD, "value": RUN}]}, {"clear_custom_property_ids": [FIELD]}):
        with pytest.raises(GuardTripped, match=f"custom field {FIELD} was already cleared by this run"):
            await session.call("update_client", {"client_id": TEST_CLIENT, **arguments})
    assert len(session.bodies) == 2 and matrix.guard.writes_allowed == 2  # neither refused write was sent or counted


# -- the negative check: the tool refuses the unknown Id locally, after one GET and before any PATCH -----------------


async def test_an_id_that_the_tool_refuses_locally_after_its_read_passes_with_no_patch_sent(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, unknown="local")
    await write_matrix.area_custom_properties(matrix)
    after_the_clear = session.requests[len(session.requests) - session.requests[::-1].index("PATCH"):]
    assert after_the_clear == ["GET", "GET", "GET", "GET"]  # the tool's second read of the clear, the area's fresh read, the
    # negative check's one GET (no PATCH, no second read), the area's last read
    assert [body for _, body in session.bodies] == [
        {"CustomProperties": [{"Id": FIELD, "Value": RUN}]}, {"CustomProperties": [{"Id": FIELD, "Value": None}]},
    ]
    assert not any(UNKNOWN == entry["Id"] for _, body in session.bodies for entry in body["CustomProperties"])
    assert any("the tool refused it itself after reading the client" in note for note in matrix.notes)


@pytest.mark.parametrize(
    "mode, said",
    [
        pytest.param("wrong-text", r"was not refused locally after one GET and before any PATCH, so the negative check did not pass \(1 request\(s\), no HTTP answer\): custom_properties \(item 1, id\): refused", id="a-local-error-that-does-not-name-the-id"),
        pytest.param("no-read", r"\(0 request\(s\), no HTTP answer\): custom_properties \(item 1, id\): custom field 9000000000000 is not a custom field", id="refused-before-any-request"),
        pytest.param("read-fails", r"\(1 request\(s\), HTTP 500\): Gorelo rejected update_client \(HTTP 500", id="the-read-failed"),
        pytest.param("timeout", r"\(1 request\(s\), no HTTP answer\): Gorelo did not confirm update_client", id="no-answer-to-the-read"),
        pytest.param("unusable-read", r"\(1 request\(s\), no HTTP answer\): Gorelo returned an unexpected response for update_client: GET", id="the-read-cannot-be-used"),
    ],
)
async def test_anything_but_a_local_refusal_after_one_get_fails_the_area(tmp_path, mode, said):
    matrix, session, manifest = matrix_for(tmp_path, unknown=mode)
    with pytest.raises(write_matrix.CheckFailed, match=said):
        await write_matrix.area_custom_properties(matrix)
    assert only_record(manifest).cleaned  # the field of the run was restored before the check began
    assert not any(note.startswith("negative check") for note in matrix.notes)


async def test_a_write_that_gets_through_during_the_negative_check_fails_the_area_even_when_the_error_looks_local(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, unknown="slips-a-write")
    with pytest.raises(write_matrix.CheckFailed, match=rf"let 1 write request\(s\) through to Gorelo; look at client {TEST_CLIENT} in the Gorelo app"):
        await write_matrix.area_custom_properties(matrix)
    assert matrix.guard.writes_allowed == 3


async def test_a_success_for_an_id_that_is_no_field_fails_the_area_and_says_where_to_look(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, unknown="accepts")
    with pytest.raises(write_matrix.CheckFailed, match=rf"update_client ACCEPTED custom field {UNKNOWN}.*a PATCH reached Gorelo.*client {TEST_CLIENT}.*{RUN}"):
        await write_matrix.area_custom_properties(matrix)
    assert only_record(manifest).cleaned


async def test_a_patch_that_carries_the_unknown_id_is_refused_by_the_guard_and_stops_the_run(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, unknown="sends-patch")
    with pytest.raises(GuardTripped, match=f"custom field {UNKNOWN} is not one the run recorded in the manifest"):
        await write_matrix.area_custom_properties(matrix)
    assert matrix.guard.violations and len(session.bodies) == 2  # the PATCH with the Id was never sent
    assert only_record(manifest).cleaned


async def test_the_run_stops_with_the_guards_text_for_a_patch_with_the_unknown_id(tmp_path):
    """Through the area runner: a refused request is a failed area that stops the run, never a passed negative check."""
    matrix, session, manifest = matrix_for(tmp_path, unknown="sends-patch")
    found = await write_matrix._run_area(matrix, write_matrix.Area(write_matrix.CUSTOM_PROPERTIES_AREA, write_matrix.area_custom_properties))
    assert found.status == "FAIL" and found.tripped and "the guard refused a request, the run stops" in found.detail


async def test_a_request_the_guard_refuses_stops_the_area_with_the_guard_text(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)
    matrix.guard.allow_custom_properties = False  # as if the run had not enabled the area
    with pytest.raises(GuardTripped, match="is one of the records the live tests run on and is never changed"):
        await write_matrix.area_custom_properties(matrix)
    assert [n for n, _ in session.calls] == ["get_client", "update_client"] and session.bodies == []  # nothing was sent
    assert not only_record(manifest).cleaned  # recorded before the write; the cleanup reads the client and finds it unset


# -- what the area prints of an update_client error: ids and Gorelo's messages, never a quoted name or value ---------


def test_without_quotes_cuts_every_double_quoted_string_with_its_escapes_and_nothing_else():
    assert _env.without_quotes('a "b" c') == 'a "..." c'
    assert _env.without_quotes(r'x "a \"quoted\" word" y "z"') == 'x "..." y "..."'
    assert _env.without_quotes("nothing quoted (HTTP 400): 'single' stays") == "nothing quoted (HTTP 400): 'single' stays"
    assert _env.without_quotes(_env.without_quotes('"a" "b"')) == '"..." "..."'  # applying it twice changes nothing more
    assert _env.without_quotes("") == ""


def alarm_with_a_value(ident=OTHER):
    """The alarm of update_client for a field that HELD a value: its name and its previous value are both quoted."""
    return alarm_text(ident, "Seats").replace("was unset, now reads set", 'was set to "Somebody\'s own text", now reads unset')


async def test_the_detail_of_an_area_whose_update_client_call_failed_has_its_quoted_text_cut_out(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)
    real = session.call

    async def alarms(name, arguments=None):
        if name == "update_client":
            raise ToolFailed(name, alarm_with_a_value(), sent=True)
        return await real(name, arguments)

    session.call = alarms
    found = await write_matrix._run_area(matrix, write_matrix.Area(write_matrix.CUSTOM_PROPERTIES_AREA, write_matrix.area_custom_properties))
    assert found.status == "FAIL" and not found.tripped
    assert f'custom field {OTHER} ("...") was set to "...", now reads unset' in found.detail  # the ids and the shape stay
    for private in ("Seats", "Somebody"):
        assert private not in found.detail
    report = write_matrix.MatrixReport(run_id=RUN, manifest_path="m.json", results=[found]).render()
    assert not any("Seats" in line or "Somebody" in line for line in report)


async def test_every_area_cuts_the_quoted_text_of_an_update_client_error_but_keeps_that_of_other_tools(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)

    def failing(tool, text):
        async def area(m):
            raise ToolFailed(tool, text, sent=True)

        return write_matrix.Area("clients", area)

    quoted = 'Gorelo said "Seats" was "Somebody\'s own text"'
    cut = await write_matrix._run_area(matrix, failing("update_client", quoted))
    kept = await write_matrix._run_area(matrix, failing("create_ticket", quoted))
    assert cut.detail == 'Gorelo said "..." was "..."'
    assert kept.detail == quoted  # the other tools' messages are printed as they are


async def test_the_negative_check_cuts_the_quoted_text_of_the_error_it_reports(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path, unknown="wrong-text")
    real = session.call

    async def quoted(name, arguments=None):
        if name == "update_client" and arguments.get("custom_properties", [{}])[0].get("id") == UNKNOWN:
            raise ToolFailed(name, 'custom_properties (item 1, id): "Seats" is somebody\'s "own text"', sent=True)
        return await real(name, arguments)

    session.call = quoted
    with pytest.raises(write_matrix.CheckFailed) as failed:
        await write_matrix.area_custom_properties(matrix)
    assert 'custom_properties (item 1, id): "..." is somebody\'s "..."' in str(failed.value) and "Seats" not in str(failed.value)


def test_the_negative_check_does_not_claim_to_learn_what_gorelo_names_in_its_errors():
    source = inspect.getsource(write_matrix.refuse_unknown_field) + inspect.getsource(write_matrix.area_custom_properties)
    assert "PropertyName" not in source and "HTTP 400" not in source and "write-unconfirmed" not in source
    assert not hasattr(write_matrix, "tool_write_unconfirmed")  # nothing in the area waits for that error any more


# --------------------------------------------------------------------------
# The cleanup: the real _Cleaner behind the real cleanup guard, with a scripted tools client
# --------------------------------------------------------------------------


class ScriptedTools:
    """Stands in for the fastmcp Client of the cleanup. call_tool returns what the real client returns: a result with
    is_error, content and structured_content. Every call becomes the requests that the REAL cleanup guard judges: a GET for
    get_client, and for update_client a GET of the client (the tool reads it first), the PATCH and a second GET.

    clear: "ok" the clear is applied (the field falls back to its default); "fail" an HTTP 400; "still-set" the answer shows the
    field as it was; "collateral" the clear is applied but another field reads differently in the answer; "wrong-value" the field is
    unset in the answer but shows another Value; "quoted-error" an error that quotes the name and the value of another field;
    "no-list" the answer has no CustomProperties; and three ways of update_client's alarm for custom fields that changed without
    being sent (it quotes their names): "alarm" the clear is applied AND another field is set meanwhile, so a fresh read shows it;
    "alarm-undone" the alarm names the field but a fresh read shows nothing changed; "alarm-blind" like "alarm", but the fresh
    read fails; "alarm-unapplied" like "alarm", but Gorelo ignored the clear."""

    def __init__(self, guard, fields=None, *, read="ok", clear="ok"):
        self.guard = guard
        self.fields = default_tenant() if fields is None else fields
        self.read, self.clear = read, clear
        self.calls: list[tuple[str, dict]] = []
        self.requests: list[str] = []
        self.bodies: list[dict] = []

    def result(self, data):
        return SimpleNamespace(is_error=False, content=[], structured_content=data)

    def failure(self, text):
        return SimpleNamespace(is_error=True, content=[SimpleNamespace(text=text)], structured_content=None)

    def record(self, client):
        rows = [{k: v for k, v in e.items() if not k.startswith("_")} if isinstance(e, dict) else e for e in self.fields]
        return {"Id": client, "CustomProperties": rows}

    async def call_tool(self, name, arguments, raise_on_error=False):
        self.calls.append((name, dict(arguments)))
        client = arguments["client_id"]
        if name == "get_client":
            self.guard.check(httpx.Request("GET", f"{BASE}/clients/{client}"))
            self.requests.append("GET")
            if self.read == "fail":
                return self.failure("Gorelo rejected get_client (HTTP 500, code 1): down")
            if self.read == "no-list":
                return self.result({"Id": client})
            return self.result(self.record(client))
        assert name == "update_client"
        self.guard.check(httpx.Request("GET", f"{BASE}/clients/{client}"))  # the tool reads the client before it writes
        self.requests.append("GET")
        entries = [{"Id": ident, "Value": None} for ident in arguments["clear_custom_property_ids"]]
        body = {"CustomProperties": entries}
        before = len(self.guard.violations)
        try:
            self.guard.check(httpx.Request("PATCH", f"{BASE}/clients/{client}", json=body))
        except GuardViolation:
            assert len(self.guard.violations) > before
            return self.result({"Id": client})  # the cleanup sees the violation count grow and reports the refusal
        self.requests.append("PATCH")
        self.bodies.append(body)
        if self.clear == "fail":  # Gorelo refuses the PATCH: the tool raises before its second read
            return self.failure("Gorelo rejected update_client (HTTP 400, code 070101): custom_properties (item 1, id): required field")
        self.guard.check(httpx.Request("GET", f"{BASE}/clients/{client}"))  # the tool reads the client again after the PATCH
        self.requests.append("GET")
        if self.clear == "still-set":
            return self.result(self.record(client))
        if self.clear == "quoted-error":
            return self.failure(
                'update_client: something went wrong (custom field 35 ("Seats") was set to "Somebody\'s own text", now reads unset). '
                "Tell the user at once."
            )
        if self.clear == "alarm-unapplied":  # the alarm, although Gorelo ignored the clear: the run's field still holds its text
            next(entry for entry in self.fields if entry["Id"] == OTHER).update(IsSet=True, Value="Somebody's own text")
            return self.failure(alarm_text(OTHER, "Seats"))
        for ident in arguments["clear_custom_property_ids"]:
            for entry in self.fields:
                if entry["Id"] == ident:
                    entry.update(Value=entry.get("_default"), IsSet=False)
        if self.clear in ("alarm", "alarm-undone", "alarm-blind"):
            if self.clear != "alarm-undone":
                next(entry for entry in self.fields if entry["Id"] == OTHER).update(IsSet=True, Value="Somebody's own text")
            if self.clear == "alarm-blind":
                self.read = "fail"  # the cleanup's fresh read after the alarm gets an HTTP 500
            return self.failure(alarm_text(OTHER, "Seats"))
        answer = self.record(client)
        rows = answer["CustomProperties"]
        if self.clear == "collateral":
            next(row for row in rows if row["Id"] == OTHER).update(IsSet=True, Value="Somebody's own text")
        if self.clear == "wrong-value":
            next(row for row in rows if row["Id"] == FIELD).update(Value="another default")
        if self.clear == "no-list":
            return self.result({"Id": client})
        return self.result(answer)


def recorded_manifest(tmp_path, *, details=None, field_id=FIELD):
    manifest = Manifest(RUN, tmp_path / "cleanup.json")
    base = {"client_id": TEST_CLIENT, "name": "Contract owner", "type": "Text", "was_set": False, "value": RUN, "value_before": SHOWN_FIRST}
    details = base if details is None else {key: value for key, value in {**base, **details}.items() if value is not write_matrix._MISSING}
    manifest.created("custom_property", field_id, manifest.label(f"custom property {field_id}"), details)
    return manifest


def cleaner_for(manifest, tools_fields=None, **options):
    guard = LiveGuard("write", manifest, cleanup=True, allow_custom_properties=True)
    tools = ScriptedTools(guard, tools_fields, **options)
    return cleanup._Cleaner(manifest, guard, tools, None, "sekrit-key-1"), tools, guard


def set_to_the_run_text(fields=None):
    rows = default_tenant() if fields is None else fields
    for entry in rows:
        if entry["Id"] == FIELD:
            entry.update(_default=entry["Value"], Value=RUN, IsSet=True)
    return rows


async def test_the_cleanup_clears_a_field_that_still_shows_the_run_text_and_only_that(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, guard = cleaner_for(manifest, set_to_the_run_text())
    await cleaner.run(leftovers=False)
    assert tools.calls == [("get_client", {"client_id": TEST_CLIENT}), ("update_client", {"client_id": TEST_CLIENT, "clear_custom_property_ids": [FIELD]})]
    assert tools.requests == ["GET", "GET", "PATCH", "GET"]  # its own read, then the tool's read, the PATCH and its second read
    assert tools.bodies == [{"CustomProperties": [{"Id": FIELD, "Value": None}]}] and not guard.violations
    assert guard.writes_allowed == 1
    record = manifest.record("custom_property", FIELD)
    assert record.cleaned and record.outcome == cleanup.CUSTOM_PROPERTY_CLEARED
    assert cleanup.CUSTOM_PROPERTY_CLEARED == "cleared by the cleanup (update_client clear_custom_property_ids)"
    assert manifest.leftovers() == []
    entry = next(e for e in tools.fields if e["Id"] == FIELD)
    assert entry["IsSet"] is False and entry["Value"] == SHOWN_FIRST


async def test_a_second_cleanup_of_a_cleaned_field_sends_nothing(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text())
    await cleaner.run(leftovers=False)
    again, tools2, _ = cleaner_for(manifest, set_to_the_run_text())
    await again.run(leftovers=False)
    assert tools2.calls == [] and len(manifest.record("custom_property", FIELD).attempts) == 1


async def test_a_field_that_is_not_set_any_more_is_cleaned_without_a_write(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest)  # the default tenant: field 31 is unset and shows its default
    await cleaner.run(leftovers=False)
    assert [name for name, _ in tools.calls] == ["get_client"] and tools.bodies == []
    record = manifest.record("custom_property", FIELD)
    assert record.cleaned and record.outcome == "already unset (IsSet false), nothing to clear"


async def test_a_value_somebody_changed_since_is_never_cleared_and_is_a_leftover_naming_the_field_and_the_client(tmp_path):
    manifest = recorded_manifest(tmp_path)
    fields = default_tenant()
    next(e for e in fields if e["Id"] == FIELD).update(Value="Somebody's own text", IsSet=True)
    cleaner, tools, _ = cleaner_for(manifest, fields)
    await cleaner.run(leftovers=False)
    assert [name for name, _ in tools.calls] == ["get_client"] and tools.bodies == []  # not touched
    record = manifest.record("custom_property", FIELD)
    assert not record.cleaned
    assert record.outcome == (
        f"left over: custom property {FIELD} of client {TEST_CLIENT} is set to a value that is not the run's text {RUN} "
        "(somebody changed it since, or the field does not show what it holds), so it was not cleared; check it in the Gorelo app"
    )
    assert "Somebody" not in record.outcome  # never the other value


@pytest.mark.parametrize(
    "details",
    [
        {"was_set": True}, {"was_set": write_matrix._MISSING}, {"was_set": None}, {"was_set": 0}, {"was_set": "false"},
        {"client_id": write_matrix._MISSING}, {"client_id": "9501"}, {"client_id": True},
        {"value": write_matrix._MISSING}, {"value": ""}, {"value": 5},
    ],
)
async def test_a_record_that_does_not_say_the_field_was_unset_before_is_not_touched_and_is_a_leftover(tmp_path, details):
    manifest = recorded_manifest(tmp_path, details=details)
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text())
    await cleaner.run(leftovers=False)
    assert tools.calls == []  # not even a read
    outcome = manifest.record("custom_property", FIELD).outcome
    assert outcome.startswith(f"left over: custom property {FIELD} of client ") and "so the cleanup does not touch it" in outcome


@pytest.mark.parametrize("read, cause", [("fail", "Gorelo rejected get_client \\(HTTP 500, code 1\\): down"), ("no-list", "the client record has no CustomProperties list")])
async def test_a_client_that_cannot_be_read_leaves_the_field_a_leftover_with_the_command_to_run_again(tmp_path, read, cause):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text(), read=read)
    await cleaner.run(leftovers=False)
    assert [name for name, _ in tools.calls] == ["get_client"] and tools.bodies == []
    outcome = manifest.record("custom_property", FIELD).outcome
    assert outcome.startswith(f"left over: custom property {FIELD} of client {TEST_CLIENT} could not be read first, so it may still hold the run's text {RUN}; ")
    assert f"python -m scripts.live.cleanup {manifest.path}" in outcome and "clear it in the Gorelo app" in outcome
    assert re.search(rf"\(the cause: .*{cause}", outcome)


async def test_a_clear_that_gorelo_refuses_is_a_leftover_that_says_the_clear_was_not_confirmed(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text(), clear="fail")
    await cleaner.run(leftovers=False)
    record = manifest.record("custom_property", FIELD)
    assert not record.cleaned
    assert record.outcome.startswith(
        f"left over: custom property {FIELD} of client {TEST_CLIENT} may still hold the run's text {RUN}: update_client did not confirm the clear; clear it in the Gorelo app"
    )
    assert "(the cause: Gorelo rejected update_client (HTTP 400, code 070101)" in record.outcome and len(record.outcome) <= 500


async def test_a_clear_whose_answer_does_not_show_the_field_unset_is_a_leftover(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text(), clear="still-set")
    await cleaner.run(leftovers=False)
    assert "the answer to the clear does not show IsSet false" in manifest.record("custom_property", FIELD).outcome
    assert not manifest.record("custom_property", FIELD).cleaned


async def test_a_clear_whose_answer_has_no_custom_fields_is_a_leftover(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text(), clear="no-list")
    await cleaner.run(leftovers=False)
    assert "the answer to the clear does not show IsSet false" in manifest.record("custom_property", FIELD).outcome


async def test_a_clear_after_which_another_field_reads_differently_is_a_leftover_that_starts_with_the_ids(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text(), clear="collateral")
    await cleaner.run(leftovers=False)
    record = manifest.record("custom_property", FIELD)
    assert not record.cleaned and tools.bodies == [{"CustomProperties": [{"Id": FIELD, "Value": None}]}]  # the field itself was cleared
    assert record.outcome == (
        f"left over: custom field(s) {OTHER} of client {TEST_CLIENT} read differently after the clear of custom property {FIELD}, "
        "which was cleared (IsSet false); check those fields in the Gorelo app (a re-run reports them again until "
        f'"changed_fields" is deleted from this record in {manifest.path})'
    )
    assert record.details["changed_fields"] == [OTHER]  # kept: a re-run reports them again
    assert "Somebody" not in record.outcome and "Seats" not in record.outcome  # ids only
    assert "run this cleanup again" not in record.outcome  # the advice is to look at the fields, not to re-run
    assert cleanup.custom_property_leftover_kind(record) == "changed"
    assert not cleanup.is_known_undeletable(record)


async def test_a_cleared_field_that_shows_another_value_than_before_the_run_is_a_leftover(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text(), clear="wrong-value")
    await cleaner.run(leftovers=False)
    record = manifest.record("custom_property", FIELD)
    assert not record.cleaned
    assert record.outcome == (
        f"left over: custom property {FIELD} of client {TEST_CLIENT} was cleared (IsSet false), but its Value is not the one it had "
        "before the run; check it in the Gorelo app"
    )


async def test_a_record_without_the_value_before_skips_that_comparison_but_not_the_one_of_the_other_fields(tmp_path):
    old = recorded_manifest(tmp_path / "old", details={"value_before": write_matrix._MISSING})
    cleaner, tools, _ = cleaner_for(old, set_to_the_run_text(), clear="wrong-value")
    await cleaner.run(leftovers=False)
    assert old.record("custom_property", FIELD).cleaned  # no first Value to compare with
    again = recorded_manifest(tmp_path / "old2", details={"value_before": write_matrix._MISSING})
    cleaner, tools, _ = cleaner_for(again, set_to_the_run_text(), clear="collateral")
    await cleaner.run(leftovers=False)
    assert f"custom field(s) {OTHER} of client {TEST_CLIENT} read differently after the clear" in again.record("custom_property", FIELD).outcome


# -- while another custom field is set the cleanup sends no write -------------------------------------------


def with_set(fields, ident, value="Somebody's own text", is_set=True):
    for entry in fields:
        if entry["Id"] == ident:
            entry.update(Value=value, IsSet=is_set)
    return fields


async def test_the_cleanup_sends_no_write_while_another_custom_field_is_set_and_names_the_field_and_the_client(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, guard = cleaner_for(manifest, with_set(set_to_the_run_text(), OTHER))
    await cleaner.run(leftovers=False)
    assert tools.calls == [("get_client", {"client_id": TEST_CLIENT})] and tools.bodies == [] and guard.writes_allowed == 0
    record = manifest.record("custom_property", FIELD)
    assert not record.cleaned
    assert record.outcome == (
        f"left over: custom property {FIELD} of client {TEST_CLIENT} still holds the run's text {RUN}: other custom fields of the "
        f"test client are set, so the cleanup sends no write; clear custom property {FIELD} in the Gorelo app "
        f"(the cause: custom field(s) {OTHER} are set)"
    )
    assert "Somebody" not in record.outcome and "Seats" not in record.outcome  # ids only
    assert cleanup.custom_property_leftover_kind(record) == "others_set"
    assert "changed_fields" not in record.details  # nothing changed: a re-run decides afresh from its own read
    assert not cleanup.is_known_undeletable(record)


@pytest.mark.parametrize(
    "marks, shown",
    [
        pytest.param({OTHER: True}, "35", id="one-field-is-set"),
        pytest.param({40: True, OTHER: True}, "35, 40", id="two-fields-are-set-and-listed-in-order"),
        pytest.param({OTHER: None}, "35", id="an-unreadable-isset-is-not-provably-unset"),
        pytest.param({OTHER: "yes"}, "35", id="a-text-isset-is-not-provably-unset"),
    ],
)
async def test_any_other_custom_field_that_is_not_provably_unset_stops_the_clear(tmp_path, marks, shown):
    manifest = recorded_manifest(tmp_path)
    fields = set_to_the_run_text()
    for ident, flag in marks.items():
        with_set(fields, ident, is_set=flag)
    cleaner, tools, guard = cleaner_for(manifest, fields)
    await cleaner.run(leftovers=False)
    assert tools.bodies == [] and guard.writes_allowed == 0
    assert manifest.record("custom_property", FIELD).outcome.endswith(
        f"clear custom property {FIELD} in the Gorelo app (the cause: custom field(s) {shown} are set)"
    )


async def test_a_long_list_of_set_fields_is_cut_in_the_leftover_and_the_clear_is_not_sent(tmp_path):
    manifest = recorded_manifest(tmp_path)
    fields = set_to_the_run_text([field(FIELD, "Contract owner", "Text", value=SHOWN_FIRST)] + [field(i, f"F{i}", "Number", value="x", is_set=True) for i in range(1, 21)])
    cleaner, tools, _ = cleaner_for(manifest, fields)
    await cleaner.run(leftovers=False)
    outcome = manifest.record("custom_property", FIELD).outcome
    assert "(the cause: custom field(s) 1, 2, 3, 4, 5, 6, 7, 8 and 12 more are set)" in outcome and len(outcome) <= 500
    assert tools.bodies == []


async def test_an_entry_that_is_no_object_proves_nothing_so_the_cleanup_reads_it_as_unusable_and_sends_nothing(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest, [*set_to_the_run_text(), "junk"])
    await cleaner.run(leftovers=False)
    assert tools.bodies == [] and [name for name, _ in tools.calls] == ["get_client"]
    outcome = manifest.record("custom_property", FIELD).outcome
    assert outcome.startswith(f"left over: custom property {FIELD} of client {TEST_CLIENT} could not be read first, so it may still hold the run's text {RUN}; ")
    assert "(the cause: an entry of the client's CustomProperties is not an object)" in outcome


async def test_while_other_fields_are_set_a_field_that_is_already_unset_is_cleaned_without_a_write(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest, with_set(default_tenant(), OTHER))  # FIELD shows its default: nothing to clear
    await cleaner.run(leftovers=False)
    record = manifest.record("custom_property", FIELD)
    assert record.cleaned and record.outcome == "already unset (IsSet false), nothing to clear" and tools.bodies == []


async def test_while_other_fields_are_set_a_value_somebody_changed_is_still_reported_as_changed_and_not_as_set_others(tmp_path):
    manifest = recorded_manifest(tmp_path)
    fields = with_set(with_set(default_tenant(), OTHER), FIELD, value="Somebody else's text")
    cleaner, tools, _ = cleaner_for(manifest, fields)
    await cleaner.run(leftovers=False)
    assert "(somebody changed it since" in manifest.record("custom_property", FIELD).outcome and tools.bodies == []


async def test_once_the_other_fields_are_unset_again_a_rerun_clears_the_field(tmp_path):
    manifest = recorded_manifest(tmp_path)
    fields = with_set(set_to_the_run_text(), OTHER)
    cleaner, tools, _ = cleaner_for(manifest, fields)
    await cleaner.run(leftovers=False)
    assert not manifest.record("custom_property", FIELD).cleaned and tools.bodies == []
    with_set(fields, OTHER, value="5", is_set=False)  # somebody cleared the other field in the app
    again, tools2, _ = cleaner_for(manifest, fields)
    await again.run(leftovers=False)
    record = manifest.record("custom_property", FIELD)
    assert record.cleaned and record.outcome == cleanup.CUSTOM_PROPERTY_CLEARED and len(tools2.bodies) == 1


# -- other fields that read differently after the clear: the ids come first and are kept until acknowledged ------


def acknowledge(manifest):
    """What a person does after checking the fields in the Gorelo app: delete the changed_fields entry from the manifest file."""
    data = json.loads(manifest.path.read_text(encoding="utf-8"))
    for record in data["records"]:
        record["details"].pop("changed_fields", None)
    manifest.path.write_text(json.dumps(data), encoding="utf-8")
    return Manifest.load(manifest.path)


async def test_the_alarm_of_update_client_is_a_leftover_that_starts_with_the_ids_a_fresh_read_shows(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, guard = cleaner_for(manifest, set_to_the_run_text(), clear="alarm")
    await cleaner.run(leftovers=False)
    assert tools.requests == ["GET", "GET", "PATCH", "GET", "GET"]  # the cleanup's read, the tool's three requests, a fresh read
    record = manifest.record("custom_property", FIELD)
    assert not record.cleaned
    assert record.outcome == (
        f"left over: custom field(s) {OTHER} of client {TEST_CLIENT} read differently after the clear of custom property {FIELD}, "
        "which now reads unset; check those fields in the Gorelo app (a re-run reports them again until "
        f'"changed_fields" is deleted from this record in {manifest.path})'
    )
    assert len(record.outcome) <= 500 and record.details["changed_fields"] == [OTHER]
    for gone in ("Seats", "Somebody", "update_client did not confirm", "run this cleanup again"):
        assert gone not in record.outcome
    assert cleanup.custom_property_leftover_kind(record) == "changed"
    report = cleanup.CleanupReport(run_id=RUN, leftovers=manifest.leftovers())
    assert not report.ok and f"LEFTOVER   custom_property {FIELD} {RUN} custom property {FIELD} (left over: custom field(s) {OTHER} of client" in report.render()


@pytest.mark.parametrize(
    "mode, own",
    [
        pytest.param("alarm-undone", "now reads unset", id="a-fresh-read-shows-nothing-changed-any-more"),
        pytest.param("alarm-blind", "was probably cleared (the clear was sent)", id="the-fresh-read-fails"),
    ],
)
async def test_the_alarm_names_its_own_ids_when_a_fresh_read_cannot_show_them(tmp_path, mode, own):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text(), clear=mode)
    await cleaner.run(leftovers=False)
    record = manifest.record("custom_property", FIELD)
    assert not record.cleaned and record.details["changed_fields"] == [OTHER]
    assert record.outcome.startswith(
        f"left over: custom field(s) {OTHER} of client {TEST_CLIENT} read differently after the clear of custom property {FIELD}, which {own}; "
    )
    assert "Seats" not in record.outcome


async def test_an_alarm_after_a_clear_that_gorelo_ignored_tells_the_operator_to_clear_the_run_field_too(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text(), clear="alarm-unapplied")
    await cleaner.run(leftovers=False)
    record = manifest.record("custom_property", FIELD)
    assert not record.cleaned and record.details["changed_fields"] == [OTHER]
    assert record.outcome.startswith(
        f"left over: custom field(s) {OTHER} of client {TEST_CLIENT} read differently after the clear of custom property {FIELD}, "
        "which still holds the run's text (clear it in the Gorelo app too); check those fields in the Gorelo app"
    )


def test_the_words_for_the_state_of_the_run_field_after_an_alarm_are_kinds_only():
    rows = lambda **change: [{"Id": FIELD, "IsSet": True, "Value": RUN, **change}]  # noqa: E731
    assert cleanup.own_state(None, FIELD, RUN) == "was probably cleared (the clear was sent)"
    assert cleanup.own_state([], FIELD, RUN) == "is no longer listed"
    assert cleanup.own_state(rows(IsSet=False), FIELD, RUN) == "now reads unset"
    assert cleanup.own_state(rows(), FIELD, RUN) == "still holds the run's text (clear it in the Gorelo app too)"
    assert cleanup.own_state(rows(Value="Somebody's own text"), FIELD, RUN) == "now reads set to another value (check it in the Gorelo app too)"
    assert "Somebody" not in cleanup.own_state(rows(Value="Somebody's own text"), FIELD, RUN)


async def test_the_words_the_cleanup_looks_for_are_the_ones_the_tool_uses_and_the_ids_are_read_from_them():
    assert cleanup.COLLATERAL_ALARM in alarm_text(OTHER, "Seats")
    assert cleanup.alarm_ids(alarm_text(OTHER, "Seats")) == [OTHER]
    two = alarm_text(OTHER, "Seats").replace(f"(custom field {OTHER}", '(custom field 52 ("Notes") was set to "x", now reads unset; custom field 35')
    assert cleanup.alarm_ids(two) == [35, 52]
    assert cleanup.alarm_ids("Custom field(s) 9 were set to an empty value; no entry for custom field(s) 7") == []  # not a field it names
    assert cleanup.alarm_ids('custom field 4 ("custom field 99")') == [4]  # a name is quoted text, which is cut out first


async def test_a_rerun_keeps_reporting_the_ids_and_never_marks_the_record_cleaned_until_they_are_acknowledged(tmp_path):
    manifest = recorded_manifest(tmp_path)
    first, tools, _ = cleaner_for(manifest, set_to_the_run_text(), clear="alarm")
    await first.run(leftovers=False)
    still = tools.fields  # the run's field is cleared, the other field is set
    again, tools2, _ = cleaner_for(manifest, still)
    await again.run(leftovers=False)
    record = manifest.record("custom_property", FIELD)
    assert not record.cleaned and tools2.bodies == [] and len(record.attempts) == 2
    assert record.outcome.startswith(
        f"left over: custom field(s) {OTHER} of client {TEST_CLIENT} read differently after the clear of custom property {FIELD}, which now reads unset; check those fields"
    )
    assert not cleanup.CleanupReport(run_id=RUN, leftovers=manifest.leftovers()).ok
    # the same after the file is read again (the details live in the manifest, not in the process)
    reloaded = Manifest.load(manifest.path)
    third, tools3, _ = cleaner_for(reloaded, still)
    await third.run(leftovers=False)
    assert not reloaded.record("custom_property", FIELD).cleaned and reloaded.details("custom_property", FIELD)["changed_fields"] == [OTHER]
    # somebody checked the fields in the Gorelo app and deletes the entry from the manifest: the next cleanup is clean
    acknowledged = acknowledge(reloaded)
    last, tools4, _ = cleaner_for(acknowledged, still)
    await last.run(leftovers=False)
    done = acknowledged.record("custom_property", FIELD)
    assert done.cleaned and done.outcome == "already unset (IsSet false), nothing to clear" and tools4.bodies == []


async def test_a_rerun_that_has_another_reason_to_leave_the_field_still_names_the_unacknowledged_ids_first(tmp_path):
    manifest = recorded_manifest(tmp_path, details={"changed_fields": [OTHER]})
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text(), clear="fail")
    await cleaner.run(leftovers=False)
    assert manifest.record("custom_property", FIELD).outcome.startswith(
        f"left over: custom field(s) {OTHER} of client {TEST_CLIENT} changed during an earlier clear and are not acknowledged yet; "
        f"custom property {FIELD} of client {TEST_CLIENT} may still hold the run's text {RUN}: update_client did not confirm the clear"
    )
    assert cleanup.custom_property_leftover_kind(manifest.record("custom_property", FIELD)) == "changed"


async def test_a_rerun_that_clears_the_field_but_has_unacknowledged_ids_is_not_clean_either(tmp_path):
    manifest = recorded_manifest(tmp_path, details={"changed_fields": [OTHER]})
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text())
    await cleaner.run(leftovers=False)
    record = manifest.record("custom_property", FIELD)
    assert len(tools.bodies) == 1 and not record.cleaned  # the clear was sent and worked, the old alarm is still not acknowledged
    assert record.outcome.startswith(f"left over: custom field(s) {OTHER} of client {TEST_CLIENT} read differently after the clear of custom property {FIELD}, which was cleared (IsSet false);")


async def test_ids_kept_before_are_merged_with_new_ones_and_a_long_list_is_cut_in_the_outcome_but_not_in_the_details(tmp_path):
    manifest = recorded_manifest(tmp_path, details={"changed_fields": list(range(100, 112))})
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text(), clear="collateral")
    await cleaner.run(leftovers=False)
    record = manifest.record("custom_property", FIELD)
    assert record.details["changed_fields"] == [OTHER, *range(100, 112)]
    assert record.outcome.startswith(f"left over: custom field(s) 35, 100, 101, 102, 103, 104, 105, 106 and 5 more of client {TEST_CLIENT} read differently")
    assert len(record.outcome) <= 500


async def test_the_matrix_summary_line_depends_on_the_kind_of_leftover(tmp_path):
    def summary_for(manifest):
        report = cleanup.CleanupReport(run_id=RUN, leftovers=manifest.leftovers())
        return write_matrix.MatrixReport(run_id=RUN, manifest_path="m.json", cleanup=report).render()

    plain = recorded_manifest(tmp_path / "plain")
    await cleaner_for(plain, set_to_the_run_text(), clear="fail")[0].run(leftovers=False)
    others = recorded_manifest(tmp_path / "others")
    await cleaner_for(others, with_set(set_to_the_run_text(), OTHER))[0].run(leftovers=False)
    changed = recorded_manifest(tmp_path / "changed")
    await cleaner_for(changed, set_to_the_run_text(), clear="alarm")[0].run(leftovers=False)
    assert [cleanup.custom_property_leftover_kind(m.record("custom_property", FIELD)) for m in (plain, others, changed)] == [
        "plain", "others_set", "changed",
    ]
    may_still_hold = f"a custom field value is left over: custom property {FIELD} of client {TEST_CLIENT} may still hold {RUN}; run python -m scripts.live.cleanup m.json"
    assert any(line.startswith(may_still_hold) for line in summary_for(plain))
    assert (
        f"a custom field value is left over: custom property {FIELD} of client {TEST_CLIENT} still holds {RUN}, and other custom fields of "
        f"that client are set, so the cleanup sends no write: clear custom property {FIELD} in the Gorelo app, then run "
        "python -m scripts.live.cleanup m.json to close the record"
    ) in summary_for(others)
    assert not any("may still hold" in line for line in summary_for(others))
    lines_changed = summary_for(changed)
    assert (
        f"custom field(s) {OTHER} of client {TEST_CLIENT} read differently after the run's field {FIELD} was cleared: check them in the Gorelo app; "
        f'python -m scripts.live.cleanup m.json reports them again until "changed_fields" is deleted from custom property {FIELD} in that manifest'
    ) in lines_changed
    assert not any("may still hold" in line or "it clears the field only while" in line for line in lines_changed)  # no re-run as the advice
    assert lines_changed[-1] == "result: FAILED"
    for lines_ in (summary_for(others), lines_changed):
        assert not any("Seats" in line or "Somebody" in line for line in lines_)


async def test_a_field_with_no_default_is_cleared_back_to_null_and_compared_with_null(tmp_path):
    manifest = recorded_manifest(tmp_path, details={"value_before": None})
    fields = [field(FIELD, "Contract owner", "Text"), field(OTHER, "Seats", "Number", value="5")]
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text(fields))
    await cleaner.run(leftovers=False)
    assert manifest.record("custom_property", FIELD).cleaned and tools.fields[0]["Value"] is None


async def test_the_cause_of_a_failed_clear_has_its_quoted_text_cut_out_so_no_name_or_value_of_another_field_is_kept(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text(), clear="quoted-error")
    await cleaner.run(leftovers=False)
    outcome = manifest.record("custom_property", FIELD).outcome
    assert outcome.startswith(f"left over: custom property {FIELD} of client {TEST_CLIENT} may still hold the run's text {RUN}: update_client did not confirm the clear")
    assert "custom field 35 (\"...\") was set to \"...\"" in outcome  # the ids and the shape stay
    assert "Seats" not in outcome and "Somebody" not in outcome


async def test_a_field_the_client_no_longer_lists_has_nothing_left_to_clear(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest, [e for e in default_tenant() if e["Id"] != FIELD])
    await cleaner.run(leftovers=False)
    record = manifest.record("custom_property", FIELD)
    assert record.cleaned and record.outcome == "the client no longer lists this custom field (nothing to clear)" and tools.bodies == []


async def test_a_field_with_an_unreadable_isset_that_shows_another_value_is_a_leftover(tmp_path):
    manifest = recorded_manifest(tmp_path)
    fields = default_tenant()
    next(e for e in fields if e["Id"] == FIELD).update(Value="other", IsSet=None)
    cleaner, tools, _ = cleaner_for(manifest, fields)
    await cleaner.run(leftovers=False)
    assert "has an unreadable IsSet, so it was not cleared" in manifest.record("custom_property", FIELD).outcome and tools.bodies == []


def test_the_changed_fields_of_two_reads_are_the_ids_whose_state_differs_and_nothing_else():
    was = [{"Id": 1, "IsSet": False, "Value": None}, {"Id": 2, "IsSet": False, "Value": "d"}, {"Id": 3, "IsSet": True, "Value": "x"}]
    assert cleanup._changed_fields(was, [dict(e) for e in was], 99) == []
    now = [{"Id": 1, "IsSet": True, "Value": "y"}, {"Id": 2, "IsSet": False, "Value": "d"}, {"Id": 4, "IsSet": False, "Value": None}]
    assert cleanup._changed_fields(was, now, 99) == [1, 3, 4]  # 1 changed, 3 is gone, 4 is new; numbers in order
    assert cleanup._changed_fields(was, now, 1) == [3, 4]  # the field the cleanup cleared is judged on its own
    assert cleanup._changed_fields(was, "not a list", 99) == [1, 2, 3] and cleanup._changed_fields(None, None, 1) == []
    junk = [{"Id": True, "IsSet": False}, "x", {"IsSet": True}, {"Id": 6, "IsSet": False, "Value": None}]
    assert cleanup._changed_fields(junk, [], 99) == [6]  # an entry that is no object with an Id is left out


async def test_the_cleanup_guard_refuses_a_clear_for_a_record_the_manifest_does_not_hold_as_unset(tmp_path):
    manifest = recorded_manifest(tmp_path)
    guard = LiveGuard("write", manifest, cleanup=True, allow_custom_properties=True)
    manifest.update_details("custom_property", FIELD, was_set=True)  # changed under the cleanup
    with pytest.raises(GuardViolation, match="not recorded as unset before the run"):
        guard.check(httpx.Request("PATCH", f"{BASE}/clients/{TEST_CLIENT}", json={"CustomProperties": [{"Id": FIELD, "Value": None}]}))


async def test_the_report_names_a_leftover_field_with_its_id_and_the_client_and_the_exit_is_not_clean(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text(), clear="fail")
    await cleaner.run(leftovers=False)
    report = cleanup.CleanupReport(run_id=RUN, leftovers=manifest.leftovers())
    text = report.render()
    assert f"LEFTOVER   custom_property {FIELD} {RUN} custom property {FIELD} (left over: custom property {FIELD} of client {TEST_CLIENT}" in text
    assert not report.ok and text.endswith("result: SOMETHING IS LEFT OVER")
    assert not cleanup.is_known_undeletable(manifest.record("custom_property", FIELD))  # a plain leftover, not a known residue


async def test_a_leftover_field_has_its_own_line_in_the_matrix_summary(tmp_path):
    manifest = recorded_manifest(tmp_path)
    cleaner, tools, _ = cleaner_for(manifest, set_to_the_run_text(), clear="fail")
    await cleaner.run(leftovers=False)
    summary = write_matrix.MatrixReport(
        run_id=RUN, manifest_path="m.json", cleanup=cleanup.CleanupReport(run_id=RUN, leftovers=manifest.leftovers())
    ).render()
    assert (
        f"a custom field value is left over: custom property {FIELD} of client {TEST_CLIENT} may still hold {RUN}; run "
        "python -m scripts.live.cleanup m.json (it clears the field only while it still holds that text), or clear it in the Gorelo app"
    ) in summary
    assert summary[-1] == "result: FAILED" and "if anything is left over: python -m scripts.live.cleanup m.json" in summary
    clean = write_matrix.MatrixReport(run_id=RUN, manifest_path="m.json", cleanup=cleanup.CleanupReport(run_id=RUN)).render()
    assert not any("custom field value is left over" in line for line in clean)


async def test_run_cleanup_gives_its_guard_the_option_only_when_the_manifest_holds_a_custom_property_to_undo(
    tmp_path, make_settings, monkeypatch
):
    made = []

    class Spy(LiveGuard):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(kwargs)

    monkeypatch.setattr(cleanup, "LiveGuard", Spy)

    async def go(manifest):
        # no route at all: a request would be unmatched, which the cleanup reports as a failed tool call (a leftover), never raises
        return await cleanup.run_cleanup(
            manifest, settings=make_settings(destructive=True), transport=MockGorelo().transport, pace=0, echo=lambda line: None,
            verify_clients=False,
        )

    await go(Manifest(RUN, tmp_path / "none" / "m.json"))  # nothing to undo
    pending = await go(recorded_manifest(tmp_path / "pending"))  # a custom property that is not cleaned
    cleaned = recorded_manifest(tmp_path / "cleaned")
    cleaned.cleaned("custom_property", FIELD, "cleared by the write matrix (update_client clear_custom_property_ids)")
    await go(cleaned)  # one that is
    assert made == [{"cleanup": True}, {"cleanup": True, "allow_custom_properties": True}, {"cleanup": True}]
    assert not pending.ok and [record.kind for record in pending.leftovers] == ["custom_property"]  # unmatched read: left over, no crash


def test_the_cleanup_docstring_says_who_clears_what_and_when():
    text = " ".join(cleanup.__doc__.split())
    for said in (
        "A custom property (kind custom_property, made by write_matrix --with-custom-properties) is the one thing a cleanup writes to the test client itself",
        "ONLY when the manifest says the field was unset before the run (was_set exactly false)",
        "When the field still shows the run's text it sends the clear only if that read shows NO other custom field set",
        "a clear that Gorelo applied as a replace would wipe a value somebody put back since",
        "While another field is set the cleanup sends nothing and leaves a LEFTOVER (\"other custom fields of the test client are set, so the cleanup sends no write; clear custom property N in the Gorelo app\")",
        "sends the clear (update_client with clear_custom_property_ids, a null Value",
        "the tool reads the client before it writes and again after the PATCH, to check the other fields",
        "and every other custom field exactly as the read made just before the clear showed it (IsSet, Value and presence)",
        "whose outcome STARTS with their ids, says that the run's field was cleared or probably cleared and sends the operator to check those fields in the Gorelo app, not to re-run the cleanup",
        "the ids are kept in the record's details (changed_fields), a re-run reports them again and never marks the record cleaned, until somebody acknowledges them by deleting that entry from the manifest",
        "somebody changed it since and it is left alone",
        "Whatever is not cleared is a LEFTOVER (not a known residue) that names the field Id and the client",
        "The guard of the cleanup gets allow_custom_properties only when the manifest holds a custom_property record",
        "The text of a tool error that goes into an outcome has its quoted strings cut out",
    ):
        assert said in text, said


# --------------------------------------------------------------------------
# Tool sets, areas and flags
# --------------------------------------------------------------------------


def test_the_area_needs_no_tool_of_its_own_and_the_flag_does_not_widen_the_tool_set():
    assert {"get_client", "update_client"} <= write_matrix.MATRIX_TOOLS
    assert write_matrix.tools_for(False) == write_matrix.tools_for(False, False, False, False)
    assert write_matrix.tools_for(False, False, True) == write_matrix.APPROVED_INVOICE_TOOLS  # the approved run takes no custom tool


def test_the_area_sits_right_after_the_custom_assets_and_is_selected_by_name():
    keys = [area.key for area in write_matrix.AREAS]
    assert keys.index("custom_assets") + 1 == keys.index("custom_properties") == keys.index("invoices") - 1
    assert write_matrix.CUSTOM_PROPERTIES_AREA == "custom_properties"
    assert [a.key for a in write_matrix.select_areas(["custom_properties"], False)] == ["custom_properties"]
    assert [a.key for a in write_matrix.select_areas([" Custom_Properties "], True)] == ["custom_properties"]


def test_the_area_does_not_name_a_tenant_id():
    source = inspect.getsource(write_matrix.area_custom_properties)
    assert "field_id" in source and "= 31" not in source and "FIELD" not in source
    assert write_matrix.FIELDS_SHOWN == 8


def test_the_command_line_has_the_flag_and_it_is_off_by_default(capsys):
    with pytest.raises(SystemExit):
        write_matrix.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "--with-custom-properties" in out and "the test client ITSELF" in out
    assert "the one write the live guard allows on that client" in out and "also under --only custom_properties" in out
    assert "it runs only while no custom field of that client is set" in out
    assert write_matrix.run_matrix.__kwdefaults__["with_custom_properties"] is False


async def test_run_matrix_builds_its_guard_with_the_option_only_for_the_flag(run, monkeypatch):
    seen = []
    real = write_matrix.LiveGuard

    class Spy(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen.append(kwargs.get("allow_custom_properties"))

    monkeypatch.setattr(write_matrix, "LiveGuard", Spy)
    await run(FakeGorelo(), areas=["custom_properties"])
    await run(FakeGorelo(), areas=["custom_properties"], with_custom_properties=True, started=datetime(2099, 10, 2, 10, 16, 0, tzinfo=timezone.utc))
    assert seen == [False, True]


async def test_without_the_flag_a_run_never_sends_anything_for_the_area(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["custom_properties"])  # --only custom_properties without --with-custom-properties
    found = results(report)["custom_properties"]
    assert found.status == "skipped" and found.requests == 0 and found.detail == write_matrix.CUSTOM_PROPERTIES_SKIP_REASON
    assert fake.mock.requests == [] and report.ok and report.exit_code == 0 and report.summary["created"] == 0


@pytest.mark.parametrize("flag", ["with_items", "with_invoices", "with_custom_assets", "with_custom_properties", "leftovers", "skip_email"])
async def test_the_approved_invoice_flag_beside_the_custom_properties_flag_is_a_usage_error(run, tmp_path, flag):
    fake = FakeGorelo()
    named = "--" + flag.replace("_", "-")
    with pytest.raises(write_matrix.UsageError, match=f"--with-approved-invoice runs alone: it cannot be combined with {named}$"):
        await run(fake, areas=["approved_invoice"], with_approved_invoice=True, **{flag: True})
    assert fake.mock.requests == []


def test_the_module_docstring_describes_the_area_its_flag_its_record_and_its_leftover():
    text = " ".join(write_matrix.__doc__.split())
    for said in (
        "[--with-custom-assets] [--with-custom-properties] [--with-approved-invoice]",
        "custom_properties get_client of the TEST CLIENT, then ONE client custom field value on that client itself",
        "SKIPPED unless --with-custom-properties is given (also under --only custom_properties without it: nothing is sent, not even a read)",
        "Every update_client call with custom fields reads the client first (a GET) and refuses locally an Id the client does not list (no PATCH, no second GET)",
        "otherwise it sends the PATCH, reads the client again (a second GET) and checks on that read that every custom field it did not send is as it was",
        "The area keeps {Id: (IsSet, Value)} from its first read and is SKIPPED, with the Ids of the fields that are set (never a value), while ANY custom field of the test client is set (IsSet true)",
        "an update that Gorelo applies as a replace would then have something to destroy, and with nothing set it has not",
        "with the lowest Id among those whose Value is not null (an unset field shows its default there, so a clear can restore it); only when none has a default, the lowest Id (the note says which case applied)",
        "the reason lists each field's Id, Type.Name and IsSet, never a value: the first eight in the reason, all of them in a note",
        "recorded in the manifest (kind custom_property: Id, Name, Type.Name, was_set false, the text, value_before: the Value of the first read) BEFORE the first write",
        "(the note \"recorded in the manifest\" is written then)",
        "after each of these four answers every other custom field must read exactly as at the first read (IsSet, Value, still listed, none new)",
        "the chosen field must be back at its first IsSet false and Value: any difference FAILS the area at once, naming the field Ids only and telling the operator to check them in the Gorelo app",
        "Only then is the record marked cleaned and the note \"set, read, cleared, read\" written",
        "must be REFUSED LOCALLY by the tool, after its read of the client and before any PATCH",
        "it passes only when the one request of that call was that GET, the guard let no write through and the tool's error has no HTTP status and names the Id",
        "a PATCH that carried the Id would be refused by the guard, which stops the run",
        "A last get_client must show every custom field as at the first read",
        "No custom property PATCH error is expected, so the run learns nothing of Gorelo's own names for such errors",
        "the cleanup clears the field ONLY when the manifest says it was unset before AND its current Value still equals the run id",
        "(the cleanup then reads the client and clears the field it recorded: its own GET, then the GET, the PATCH and the second GET of update_client)",
        "LEFTOVER (exit status 1)",
        "A required text field with no default cannot be cleared back to nothing",
        "what stops update_client from changing the test client in any other run is the guard",
        "(a field whose record is cleaned takes the null only)",
        "custom_properties without --with-custom-properties, while a custom field of the test client is set or with no unset text field to use",
    ):
        assert said in text, said
    for gone in ("either Gorelo's HTTP 400 or its own write-unconfirmed error", "it is an unknown of the API", "PropertyName"):
        assert gone not in text, gone


async def test_a_run_with_the_flag_records_the_field_in_a_manifest_that_survives_a_reload(tmp_path):
    matrix, session, manifest = matrix_for(tmp_path)
    await write_matrix.area_custom_properties(matrix)
    again = Manifest.load(manifest.path)
    assert again.details("custom_property", FIELD)["was_set"] is False and again.record("custom_property", FIELD).cleaned
    assert again.details("custom_property", FIELD)["value_before"] == SHOWN_FIRST


# --------------------------------------------------------------------------
# End to end with the real tools (runs once update_client reads the client before it writes custom fields)
# --------------------------------------------------------------------------

UPDATE_CLIENT = next(spec for spec in REGISTRY.specs if spec.name == "update_client")
TOOL_READS_FIRST = (
    {"custom_properties", "clear_custom_property_ids"} <= set(inspect.signature(UPDATE_CLIENT.fn).parameters)
    and CLIENT_OP in UPDATE_CLIENT.ops
)
needs_tool = pytest.mark.skipif(not TOOL_READS_FIRST, reason="update_client does not read the client before it writes custom fields yet")


class PropertiesFake(FakeGorelo):
    """FakeGorelo whose clients carry custom fields: GET returns CustomProperties, PATCH upserts them by Id.

    `fields` are the client custom fields of the tenant (Id, Variable, Name, Type, default); `saved` holds the values per client
    (the test client may start with some). `replace`: a PATCH also drops every value it did not send (an update applied as a
    replace). `required`: field Ids Gorelo will not clear. A handler may be wrapped to hurt an answer or a read."""

    def __init__(self, *, fields=None, saved=None, replace=False, required=(), **options):
        super().__init__(**options)
        self.fields = fields if fields is not None else [
            {"Id": 20, "Variable": "notes", "Name": "Notes", "Type": {"Id": 3, "Name": "Large Text"}, "default": None},
            {"Id": OTHER, "Variable": "seats", "Name": "Seats", "Type": {"Id": 2, "Name": "Number"}, "default": "5"},
            {"Id": FIELD, "Variable": "owner", "Name": "Contract owner", "Type": {"Id": 1, "Name": "Text"}, "default": SHOWN_FIRST},
            {"Id": 40, "Variable": "tier", "Name": "Support tier", "Type": {"Id": 1, "Name": "Text"}, "default": None},
        ]
        self.saved: dict[int, dict[int, str]] = {key: dict(value) for key, value in (saved or {}).items()}
        self.replace, self.required = replace, set(required)
        self.patches: list[dict] = []
        self.handlers["GET /v1/clients/{clientId}"] = self.get_client_with_fields
        self.handlers["PATCH /v1/clients/{clientId}"] = self.patch_client_with_fields

    def custom_properties(self, client_id):
        saved = self.saved.get(client_id, {})
        return [
            {
                "Id": f["Id"], "Variable": f["Variable"], "Name": f["Name"], "Type": dict(f["Type"]),
                "Value": saved.get(f["Id"], f["default"]), "IsSet": f["Id"] in saved,
            }
            for f in self.fields
        ]

    def get_client_with_fields(self, q):
        client = self.clients.get(int(self.part(q, 3)))
        if client is None:
            return self.missing("client")
        return envelope({**client, "CustomProperties": self.custom_properties(client["Id"])})

    def patch_client_with_fields(self, q):
        client = self.clients.get(int(self.part(q, 3)))
        if client is None:
            return self.missing("client")
        self.patches.append(q.json)
        for key in ("Name", "AlternateName", "BillingName"):
            if q.json.get(key):
                client[key] = q.json[key]
        known = {f["Id"] for f in self.fields}
        sent = set()
        for item in q.json.get("CustomProperties") or []:
            ident, value = item["Id"], item["Value"]
            if ident not in known:
                return error_envelope(400, [("070101", "The custom field does not exist.", "CustomProperties[0].Id")])
            sent.add(ident)
            if value is None:
                if ident in self.required:
                    return error_envelope(400, [("070101", "A required field cannot be left with no value.", "CustomProperties[0].Value")])
                self.saved.get(client["Id"], {}).pop(ident, None)
            else:
                self.saved.setdefault(client["Id"], {})[ident] = value
        if self.replace and sent:
            self.saved[client["Id"]] = {ident: v for ident, v in self.saved.get(client["Id"], {}).items() if ident in sent}
        return envelope({**client, "CustomProperties": self.custom_properties(client["Id"])})


@needs_tool
async def test_end_to_end_the_area_passes_and_leaves_the_test_client_as_it_was(run, lines):
    fake = PropertiesFake()
    report = await run(fake, areas=["custom_properties"], with_custom_properties=True)
    assert report.ok and report.exit_code == 0, "\n".join(lines)
    assert results(report)["custom_properties"].status == "pass"
    assert area_ops(fake, report) == {"custom_properties": GOLDEN_CUSTOM_PROPERTIES, "cleanup": []}  # the cleanup sends nothing
    assert results(report)["custom_properties"].requests == 11
    assert fake.saved.get(TEST_CLIENT, {}) == {}  # unset again
    assert fake.clients[TEST_CLIENT] == {"Id": TEST_CLIENT, "Name": "the test client", "AlternateName": "the test client alt", "BillingName": "the test client"}
    assert fake.patches == [
        {"CustomProperties": [{"Id": FIELD, "Value": RUN}]},
        {"CustomProperties": [{"Id": FIELD, "Value": None}]},
    ]  # the Id that is no field never reaches Gorelo: the tool refuses it after its read
    assert report.summary == {"created": 1, "cleaned": 1, "leftovers": 0, "undeletable": 0, "unresolved_intents": 0}
    assert report.refused == 0 and report.cleanup.ok and lines[-1] == "result: PASSED"
    notes = results(report)["custom_properties"].notes
    assert any("the tool refused it itself after reading the client" in note for note in notes)
    assert any(note.startswith(f"custom field {FIELD}: set to the run id, read, cleared, read") for note in notes)
    assert not fake.mock.unmatched


@needs_tool
async def test_end_to_end_the_unknown_id_is_refused_by_the_tool_after_one_get_and_no_request_carries_it(run):
    fake = PropertiesFake()
    report = await run(fake, areas=["custom_properties"], with_custom_properties=True)
    assert report.ok
    on_the_client = [r for r in fake.mock.requests if r.path == f"/v1/clients/{TEST_CLIENT}"]
    assert [r.method for r in on_the_client] == ["GET", "GET", "PATCH", "GET", "GET", "GET", "PATCH", "GET", "GET", "GET", "GET"]
    assert all(str(UNKNOWN) not in json.dumps(r.json) for r in on_the_client if r.method == "PATCH")
    assert report.summary["leftovers"] == 0


@needs_tool
async def test_end_to_end_a_read_the_tool_cannot_use_before_the_negative_update_fails_the_area_and_nothing_is_written(run, lines):
    fake = PropertiesFake()
    original = fake.handlers["GET /v1/clients/{clientId}"]
    reads = []

    def breaks_the_read_of_the_negative_check(request):
        reads.append(request.path)
        answer = original(request)
        if len(reads) == 8:  # first read, set (two reads of the tool, fresh), clear (two reads of the tool, fresh), then the negative check
            answer["Data"]["CustomProperties"] = None
        return answer

    fake.handlers["GET /v1/clients/{clientId}"] = breaks_the_read_of_the_negative_check
    report = await run(fake, areas=["custom_properties"], with_custom_properties=True)
    found = results(report)["custom_properties"]
    assert found.status == "FAIL" and "the negative check did not pass (1 request(s)" in found.detail
    assert "nothing was written, because" in found.detail  # the tool says so when its read cannot be used: the detail keeps that part
    assert len(found.detail) <= write_matrix.DETAIL_LIMIT
    assert len(fake.patches) == 2 and not any(str(UNKNOWN) in json.dumps(patch) for patch in fake.patches)
    assert fake.saved.get(TEST_CLIENT, {}) == {} and report.summary["leftovers"] == 0  # the field of the run was cleared before it


@needs_tool
async def test_end_to_end_a_tenant_where_a_custom_field_is_set_is_a_skip_and_no_replace_can_destroy_it(run):
    fake = PropertiesFake(saved={TEST_CLIENT: {40: "Gold"}}, replace=True)
    report = await run(fake, areas=["custom_properties"], with_custom_properties=True)
    found = results(report)["custom_properties"]
    assert found.status == "skipped" and found.requests == 1 and found.detail.startswith("custom field(s) 40 of the test client are set")
    assert fake.patches == [] and fake.saved[TEST_CLIENT] == {40: "Gold"} and report.ok and report.summary["created"] == 0


@needs_tool
async def test_end_to_end_a_replace_in_a_tenant_with_nothing_set_destroys_nothing_and_the_area_passes(run, lines):
    fake = PropertiesFake(replace=True)
    report = await run(fake, areas=["custom_properties"], with_custom_properties=True)
    assert report.ok, "\n".join(lines)
    assert fake.saved.get(TEST_CLIENT, {}) == {}


async def rerun_cleanup(report, fake, make_settings, echo):
    """The cleanup command again, on the manifest of a finished run."""
    return await cleanup.run_cleanup(
        Manifest.load(report.manifest_path), settings=make_settings(destructive=True), transport=fake.mock.transport, pace=0,
        echo=echo, verify_clients=False,
    )


@needs_tool
async def test_end_to_end_a_field_that_changes_after_the_tools_checks_fails_the_area_and_the_cleanup_sends_no_write(run, lines, make_settings):
    fake = PropertiesFake()
    original = fake.handlers["GET /v1/clients/{clientId}"]
    reads = []

    def somebody_sets_another_field_before_the_fresh_read(request):
        reads.append(request.path)
        if len(reads) == 4:  # the area's first read, the tool's reads before and after the set, then the area's fresh read
            fake.saved[TEST_CLIENT][OTHER] = "LEAKED-9"
        return original(request)

    fake.handlers["GET /v1/clients/{clientId}"] = somebody_sets_another_field_before_the_fresh_read
    report = await run(fake, areas=["custom_properties"], with_custom_properties=True)
    found = results(report)["custom_properties"]
    assert found.status == "FAIL" and not report.ok
    assert found.detail.startswith(f"get_client after the set: custom field(s) {OTHER} of client {TEST_CLIENT} do not read as at the first read")
    assert "LEAKED" not in found.detail and "Seats" not in found.detail  # never a value, never a name
    # another custom field is set, so a clear that Gorelo might apply as a replace could wipe it: the cleanup sends nothing and the
    # run's text stays until somebody clears it in the Gorelo app
    assert fake.saved[TEST_CLIENT] == {FIELD: RUN, OTHER: "LEAKED-9"} and fake.patches == [{"CustomProperties": [{"Id": FIELD, "Value": RUN}]}]
    record = next(r for r in Manifest.load(report.manifest_path).all_created() if r.kind == "custom_property")
    assert not record.cleaned and cleanup.custom_property_leftover_kind(record) == "others_set"
    assert report.summary["leftovers"] == 1 and not report.cleanup.ok
    assert area_ops(fake, report)["cleanup"] == ["GET /v1/clients/{clientId}"]  # its own read, and nothing else
    assert f"left over: custom property {FIELD} of client {TEST_CLIENT} still holds the run's text {RUN}: {cleanup.CUSTOM_PROPERTY_OTHERS_SET}; clear custom property {FIELD} in the Gorelo app" in record.outcome
    assert (
        f"a custom field value is left over: custom property {FIELD} of client {TEST_CLIENT} still holds {RUN}, and other custom fields of that "
        f"client are set, so the cleanup sends no write: clear custom property {FIELD} in the Gorelo app, then run "
        f"python -m scripts.live.cleanup {report.manifest_path} to close the record"
    ) in lines
    assert lines[-1] == "result: FAILED" and "LEAKED" not in "\n".join(lines)
    # somebody clears the other field in the app; the cleanup command then clears the run's text
    fake.saved[TEST_CLIENT].pop(OTHER)
    after = await rerun_cleanup(report, fake, make_settings, lines.append)
    assert after.ok and fake.saved.get(TEST_CLIENT, {}) == {} and fake.patches[-1] == {"CustomProperties": [{"Id": FIELD, "Value": None}]}


@needs_tool
async def test_end_to_end_the_tools_own_alarm_fails_the_area_with_its_text_cut_of_names_and_the_cleanup_sends_no_write(run, lines):
    fake = PropertiesFake()
    original = fake.handlers["PATCH /v1/clients/{clientId}"]
    calls = []

    def the_answer_shows_another_field_set(request):
        calls.append(request)
        if len(calls) == 1:  # Gorelo changes another field with the first PATCH, and the tool's second read shows it
            fake.saved.setdefault(TEST_CLIENT, {})[OTHER] = "LEAKED-9"
        return original(request)

    fake.handlers["PATCH /v1/clients/{clientId}"] = the_answer_shows_another_field_set
    report = await run(fake, areas=["custom_properties"], with_custom_properties=True)
    found = results(report)["custom_properties"]
    assert found.status == "FAIL" and not report.ok
    assert f"custom field {OTHER}" in found.detail  # the tool's own error names the field that changed, whatever else it says
    assert cleanup.COLLATERAL_ALARM in found.detail  # it is the alarm of the tool, not a check of the area
    assert '("...")' in found.detail and "Seats" not in found.detail  # the name of the field is quoted text: cut out of what is printed
    assert "LEAKED" not in found.detail and "LEAKED" not in "\n".join(lines)  # and so would be the value of a field that held one
    assert [r.method for r in fake.mock.requests if r.path == f"/v1/clients/{TEST_CLIENT}"][:4] == ["GET", "GET", "PATCH", "GET"]
    assert fake.saved[TEST_CLIENT] == {FIELD: RUN, OTHER: "LEAKED-9"}  # another field is set: the cleanup sent no write
    assert report.summary["leftovers"] == 1 and area_ops(fake, report)["cleanup"] == ["GET /v1/clients/{clientId}"]


@needs_tool
async def test_end_to_end_the_alarm_words_the_cleanup_looks_for_are_the_ones_the_real_tool_uses(make_settings):
    fake = PropertiesFake(saved={TEST_CLIENT: {FIELD: RUN}})
    original = fake.handlers["PATCH /v1/clients/{clientId}"]

    def the_clear_also_sets_another_field(request):
        answer = original(request)
        fake.saved[TEST_CLIENT][OTHER] = "LEAKED-9"
        return answer

    fake.handlers["PATCH /v1/clients/{clientId}"] = the_clear_also_sets_another_field
    server = build_server(make_settings(destructive=True), transport=fake.mock.transport)
    async with Client(server) as tools:
        result = await tools.call_tool("update_client", {"client_id": TEST_CLIENT, "clear_custom_property_ids": [FIELD]}, raise_on_error=False)
    text = " ".join(" ".join(getattr(block, "text", "") for block in result.content).split())
    assert result.is_error and cleanup.COLLATERAL_ALARM in text and cleanup.alarm_ids(text) == [OTHER]
    assert text.startswith(alarm_text(OTHER, "Seats").split(" Tell the user")[0]) and "LEAKED" not in cleanup.without_quotes(text)


@needs_tool
async def test_end_to_end_the_standalone_cleanup_sends_no_write_while_another_custom_field_is_set(tmp_path, make_settings):
    fake = PropertiesFake(saved={TEST_CLIENT: {FIELD: RUN, OTHER: "Somebody's own text"}}, replace=True)
    manifest = recorded_manifest(tmp_path)
    echoed = []
    report = await cleanup.run_cleanup(
        manifest, settings=make_settings(destructive=True), transport=fake.mock.transport, pace=0, echo=echoed.append, verify_clients=False,
    )
    assert [(r.method, r.path) for r in fake.mock.requests] == [("GET", f"/v1/clients/{TEST_CLIENT}")]  # its read, and no write at all
    assert fake.patches == [] and fake.saved[TEST_CLIENT] == {FIELD: RUN, OTHER: "Somebody's own text"}  # the replace had nothing to wipe
    assert not report.ok and [record.id for record in report.leftovers] == [FIELD]
    assert any(
        f"left over: custom property {FIELD} of client {TEST_CLIENT} still holds the run's text {RUN}: {cleanup.CUSTOM_PROPERTY_OTHERS_SET}; "
        f"clear custom property {FIELD} in the Gorelo app (the cause: custom field(s) {OTHER} are set)" in line for line in echoed
    )
    assert "Somebody" not in "\n".join(echoed) and "Seats" not in "\n".join(echoed)


@needs_tool
async def test_end_to_end_the_alarm_during_the_standalone_cleanup_is_kept_until_it_is_acknowledged(tmp_path, make_settings):
    fake = PropertiesFake(saved={TEST_CLIENT: {FIELD: RUN}})
    original = fake.handlers["PATCH /v1/clients/{clientId}"]

    def the_clear_also_sets_another_field(request):
        answer = original(request)
        fake.saved[TEST_CLIENT][OTHER] = "LEAKED-9"  # somebody (or an automation) sets another field in the same moment
        return answer

    fake.handlers["PATCH /v1/clients/{clientId}"] = the_clear_also_sets_another_field
    manifest = recorded_manifest(tmp_path)
    echoed = []
    options = dict(settings=make_settings(destructive=True), transport=fake.mock.transport, pace=0, echo=echoed.append, verify_clients=False)
    report = await cleanup.run_cleanup(manifest, **options)
    assert [r.method for r in fake.mock.requests] == ["GET", "GET", "PATCH", "GET", "GET"]  # its read, the tool's three, a fresh read
    record = manifest.record("custom_property", FIELD)
    assert not record.cleaned and not report.ok and record.details["changed_fields"] == [OTHER]
    assert record.outcome == (
        f"left over: custom field(s) {OTHER} of client {TEST_CLIENT} read differently after the clear of custom property {FIELD}, "
        "which now reads unset; check those fields in the Gorelo app (a re-run reports them again until "
        f'"changed_fields" is deleted from this record in {manifest.path})'
    )
    assert fake.saved[TEST_CLIENT] == {OTHER: "LEAKED-9"}  # the run's field was cleared, the other field is as Gorelo has it
    assert "LEAKED" not in "\n".join(echoed) and "Seats" not in "\n".join(echoed)
    # a re-run finds the run's field unset, and still does not mark the record cleaned, or exit 0
    fake.mock.requests.clear()
    again = await cleanup.run_cleanup(Manifest.load(manifest.path), **options)
    assert not again.ok and [r.method for r in fake.mock.requests] == ["GET"]
    assert Manifest.load(manifest.path).details("custom_property", FIELD)["changed_fields"] == [OTHER]
    # somebody checks the field in the Gorelo app and deletes the entry: the next cleanup is clean
    acknowledged = acknowledge(Manifest.load(manifest.path))
    last = await cleanup.run_cleanup(acknowledged, **options)
    assert last.ok and acknowledged.record("custom_property", FIELD).outcome == "already unset (IsSet false), nothing to clear"


@needs_tool
async def test_end_to_end_a_run_whose_cleanup_meets_the_alarm_ends_with_a_summary_that_names_the_ids_and_not_a_rerun(run, lines):
    fake = PropertiesFake()
    get = fake.handlers["GET /v1/clients/{clientId}"]
    patch = fake.handlers["PATCH /v1/clients/{clientId}"]
    reads, patches = [], []

    def the_fresh_read_after_the_set_fails(request):
        reads.append(request.path)
        if len(reads) == 4:  # the area's first read, the tool's reads before and after the set, then the area's fresh read
            return error_envelope(500, [("070500", "boom")])
        return get(request)

    def the_clear_of_the_cleanup_also_sets_another_field(request):
        patches.append(request)
        answer = patch(request)
        if len(patches) == 2:  # the first PATCH is the set of the area, the second the clear of the cleanup
            fake.saved[TEST_CLIENT][OTHER] = "LEAKED-9"
        return answer

    fake.handlers["GET /v1/clients/{clientId}"] = the_fresh_read_after_the_set_fails
    fake.handlers["PATCH /v1/clients/{clientId}"] = the_clear_of_the_cleanup_also_sets_another_field
    report = await run(fake, areas=["custom_properties"], with_custom_properties=True)
    assert results(report)["custom_properties"].status == "FAIL" and not report.ok and report.summary["leftovers"] == 1
    record = next(r for r in Manifest.load(report.manifest_path).all_created() if r.kind == "custom_property")
    assert record.details["changed_fields"] == [OTHER] and cleanup.custom_property_leftover_kind(record) == "changed"
    assert any(line.startswith(f"  LEFTOVER   custom_property {FIELD} ") and f"(left over: custom field(s) {OTHER} of client {TEST_CLIENT} read differently" in line for line in lines)
    manifest_path = report.manifest_path
    assert (
        f"custom field(s) {OTHER} of client {TEST_CLIENT} read differently after the run's field {FIELD} was cleared: check them in the Gorelo app; "
        f'python -m scripts.live.cleanup {manifest_path} reports them again until "changed_fields" is deleted from custom property {FIELD} in that manifest'
    ) in lines
    assert not any(line.startswith("a custom field value is left over") for line in lines)
    assert lines[-1] == "result: FAILED" and "LEAKED" not in "\n".join(lines) and "Seats" not in "\n".join(lines)


@needs_tool
async def test_end_to_end_a_run_that_stops_after_the_set_has_its_field_cleared_by_the_cleanup(run, lines):
    fake = PropertiesFake()
    original = fake.handlers["GET /v1/clients/{clientId}"]
    reads = []

    def breaks_after_the_set(request):
        reads.append(request.path)
        if len(reads) == 4:  # the first read, the tool's reads before and after the set, and then the fresh read after the set
            return error_envelope(500, [("070500", "boom")])
        return original(request)

    fake.handlers["GET /v1/clients/{clientId}"] = breaks_after_the_set
    report = await run(fake, areas=["custom_properties"], with_custom_properties=True)
    found = results(report)["custom_properties"]
    assert found.status == "FAIL" and "get_client" in found.detail
    # the cleanup read the client and cleared the field, because it still showed the run's text (update_client reads it first too)
    assert fake.saved.get(TEST_CLIENT, {}) == {}
    assert area_ops(fake, report)["cleanup"] == ["GET /v1/clients/{clientId}", "GET /v1/clients/{clientId}", "PATCH /v1/clients/{clientId}", "GET /v1/clients/{clientId}"]
    assert fake.patches[-1] == {"CustomProperties": [{"Id": FIELD, "Value": None}]}
    record = next(r for r in Manifest.load(report.manifest_path).all_created() if r.kind == "custom_property")
    assert record.cleaned and record.outcome == cleanup.CUSTOM_PROPERTY_CLEARED
    assert report.cleanup.ok and report.summary["leftovers"] == 0 and not report.ok  # the area failed, nothing is left over


@needs_tool
async def test_end_to_end_a_second_read_that_fails_after_the_set_fails_the_area_and_the_cleanup_clears_the_field(run, lines):
    fake = PropertiesFake()
    original = fake.handlers["GET /v1/clients/{clientId}"]
    reads = []

    def breaks_the_tools_read_after_the_set(request):
        reads.append(request.path)
        if len(reads) == 3:  # the area's first read, the tool's read before the set, then the tool's read after it
            return error_envelope(500, [("070500", "boom")])
        return original(request)

    fake.handlers["GET /v1/clients/{clientId}"] = breaks_the_tools_read_after_the_set
    report = await run(fake, areas=["custom_properties"], with_custom_properties=True)
    found = results(report)["custom_properties"]
    assert found.status == "FAIL" and not report.ok
    assert fake.patches == [{"CustomProperties": [{"Id": FIELD, "Value": RUN}]}, {"CustomProperties": [{"Id": FIELD, "Value": None}]}]
    assert fake.saved.get(TEST_CLIENT, {}) == {}  # the write was sent and the tool could not check it: the cleanup undid it
    assert area_ops(fake, report)["cleanup"] == ["GET /v1/clients/{clientId}", "GET /v1/clients/{clientId}", "PATCH /v1/clients/{clientId}", "GET /v1/clients/{clientId}"]
    record = next(r for r in Manifest.load(report.manifest_path).all_created() if r.kind == "custom_property")
    assert record.cleaned and record.outcome == cleanup.CUSTOM_PROPERTY_CLEARED and report.summary["leftovers"] == 0


@needs_tool
async def test_end_to_end_an_update_that_gorelo_ignores_fails_the_area_and_the_cleanup_finds_nothing_to_clear(run, lines):
    fake = PropertiesFake()
    original = fake.handlers["PATCH /v1/clients/{clientId}"]

    def ignores_the_set(request):
        if request.json["CustomProperties"][0]["Value"] is not None:
            fake.patches.append(request.json)  # received, answered with success, applied to nothing
            client = fake.clients[TEST_CLIENT]
            return envelope({**client, "CustomProperties": fake.custom_properties(TEST_CLIENT)})
        return original(request)

    fake.handlers["PATCH /v1/clients/{clientId}"] = ignores_the_set
    report = await run(fake, areas=["custom_properties"], with_custom_properties=True)
    found = results(report)["custom_properties"]
    assert found.status == "FAIL" and not report.ok and str(FIELD) in found.detail
    assert fake.saved.get(TEST_CLIENT, {}) == {} and len(fake.patches) == 1  # nothing was saved, so the cleanup sends no clear
    assert area_ops(fake, report)["cleanup"] == ["GET /v1/clients/{clientId}"]
    record = next(r for r in Manifest.load(report.manifest_path).all_created() if r.kind == "custom_property")
    assert record.cleaned and record.outcome == "already unset (IsSet false), nothing to clear" and report.summary["leftovers"] == 0


@needs_tool
async def test_end_to_end_an_interrupted_run_has_its_field_cleared_by_the_cleanup_that_runs_in_finally(run, lines, monkeypatch):
    fake = PropertiesFake()

    class Abort(BaseException):
        pass

    real = write_matrix.check_field_set

    def interrupted(*args, **kwargs):
        real(*args, **kwargs)  # the set was confirmed ...
        raise Abort("stop")  # ... and the run is interrupted right after it

    monkeypatch.setattr(write_matrix, "check_field_set", interrupted)
    with pytest.raises(Abort):
        await run(fake, areas=["custom_properties"], with_custom_properties=True)
    assert fake.saved.get(TEST_CLIENT, {}) == {}  # the cleanup, which ran before the interruption left the run, cleared it
    assert fake.patches == [{"CustomProperties": [{"Id": FIELD, "Value": RUN}]}, {"CustomProperties": [{"Id": FIELD, "Value": None}]}]
    assert any(line == "result: FAILED" for line in lines) and any("interrupted (Abort)" in line for line in lines)
    assert any(line.startswith(f"  cleaned    custom_property {FIELD} ") and line.endswith(f"({cleanup.CUSTOM_PROPERTY_CLEARED})") for line in lines)


@needs_tool
async def test_end_to_end_a_field_somebody_changed_in_between_is_left_alone_and_reported(run, lines):
    fake = PropertiesFake()
    original = fake.handlers["PATCH /v1/clients/{clientId}"]

    def somebody_else_edits_then_clear_never_comes(request):
        answer = original(request)
        fake.saved[TEST_CLIENT][FIELD] = "somebody's own text"  # a person types over the run's text right after the set
        return answer

    fake.handlers["PATCH /v1/clients/{clientId}"] = somebody_else_edits_then_clear_never_comes
    report = await run(fake, areas=["custom_properties"], with_custom_properties=True)
    assert results(report)["custom_properties"].status == "FAIL"  # the fresh read does not show the text the run sent
    assert fake.saved[TEST_CLIENT][FIELD] == "somebody's own text"  # never cleared: it is not the run's any more
    assert area_ops(fake, report)["cleanup"] == ["GET /v1/clients/{clientId}"]
    assert report.summary["leftovers"] == 1 and not report.cleanup.ok
    assert any(f"custom property {FIELD} of client {TEST_CLIENT} is set to a value that is not the run's text" in line for line in lines)
    assert any(line.startswith("a custom field value is left over: custom property 31 of client 9501") for line in lines)
    assert lines[-1] == "result: FAILED"


@needs_tool
async def test_end_to_end_a_required_field_gorelo_will_not_clear_is_a_leftover_naming_the_field_and_the_client(run, lines):
    fake = PropertiesFake(required={FIELD})
    report = await run(fake, areas=["custom_properties"], with_custom_properties=True)
    assert results(report)["custom_properties"].status == "FAIL"
    assert fake.saved[TEST_CLIENT][FIELD] == RUN  # still set: the clear was refused twice (the area, then the cleanup)
    assert report.summary["leftovers"] == 1 and not report.ok
    assert area_ops(fake, report)["cleanup"] == ["GET /v1/clients/{clientId}", "GET /v1/clients/{clientId}", "PATCH /v1/clients/{clientId}"]  # a refused PATCH ends the tool's call
    leftover = next(line for line in lines if line.startswith(f"  LEFTOVER   custom_property {FIELD} "))
    assert (
        f"left over: custom property {FIELD} of client {TEST_CLIENT} may still hold the run's text {RUN}: update_client did not confirm the clear"
    ) in leftover


@needs_tool
async def test_end_to_end_a_tenant_without_a_usable_field_is_a_skip_and_writes_nothing(run):
    fake = PropertiesFake(fields=[{"Id": 7, "Variable": "seats", "Name": "Seats", "Type": {"Id": 2, "Name": "Number"}, "default": None}])
    report = await run(fake, areas=["custom_properties"], with_custom_properties=True)
    found = results(report)["custom_properties"]
    assert found.status == "skipped" and found.requests == 1 and "7 (Number, IsSet false)" in found.detail
    assert fake.patches == [] and report.ok and report.summary["created"] == 0


@needs_tool
async def test_end_to_end_the_flag_does_not_make_any_other_area_write_the_test_client(run):
    fake = PropertiesFake()
    report = await run(fake, with_custom_properties=True, skip_email=True)
    assert results(report)["custom_properties"].status == "pass"
    patches = [r for r in fake.mock.requests if r.method == "PATCH" and r.path.startswith("/v1/clients/")]
    assert [p.path for p in patches] == [f"/v1/clients/8200", f"/v1/clients/{TEST_CLIENT}", f"/v1/clients/{TEST_CLIENT}"]
    assert set(patches[0].json) == {"AlternateName"}  # the temporary client of the clients area
    assert all(set(p.json) == {"CustomProperties"} for p in patches[1:])
    assert report.ok and fake.leftovers() == []
