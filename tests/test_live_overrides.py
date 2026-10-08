"""The live overrides layer: spec/live_overrides.json swaps the published operations the live API disagrees with.

spec/spec_index.json mirrors Gorelo's published spec and is never edited. spec.load_spec_index() applies the
overrides that sit next to it. On 2026-10-02 the file held two: PATCH /v1/clients/{clientId} and
PATCH /v1/contacts/{contactId} stood in for the published collection forms (PATCH /v1/clients and PATCH /v1/contacts)
that answered 405. Gorelo published exactly those two operations the same day (contract e15cb5a18ec2), so the
overrides are RETIRED: replace_ops is empty and the two entries, with their probe evidence, sit in the `retired`
section of the file, which the loader ignores.

Three groups of tests:
* the shipped file (what it says today, that its history is kept, and the alarm that the retired operations are
  still published in the index);
* the loaded index (nothing is swapped, the published operations are what the client and the tools see);
* the loader, on small synthetic indexes of its own (the mechanism stays tested for the next override: an empty
  replace_ops is valid, `retired` is ignored, every malformed document is refused with a message that names it).

Offline: no network.
"""

import copy
import json
import os
import re
from pathlib import Path

import pytest
from conftest import envelope, uid

from gorelo_client import GoreloAPIError
from spec import (
    DEFAULT_INDEX_PATH,
    DEFAULT_OVERRIDES_PATH,
    OVERRIDES_FILENAME,
    RETIRED_ENTRY_FIELDS,
    SpecIndex,
    SpecViolation,
    apply_live_overrides,
    load_spec_index,
    parse_live_overrides,
    validate_body,
    validate_path_param,
)
from tools._common import REGISTRY

REPO_ROOT = Path(__file__).resolve().parent.parent

# The two retired overrides: the collection form the published spec had until 2026-10-01 (it answered 405) and the
# published path form that replaced it (contract e15cb5a18ec2).
RETIRED = {
    "PATCH /v1/clients": ("PATCH /v1/clients/{clientId}", "clientId"),
    "PATCH /v1/contacts": ("PATCH /v1/contacts/{contactId}", "contactId"),
}
PUBLISHED_AS = {published: key for published, (key, _name) in RETIRED.items()}
# since the 2026-10-08 spec every path parameter carries its serialization style
INT64 = {"type": "integer", "format": "int64"}
PREVIOUS_SPEC_SHA256 = "7e5bd5415de2952728c31bfd35f8f4d3f5835a156861aa3c5c34f906b297cfe7"
# The five keys of every retired entry of the shipped file. The loader's DROP THE OVERRIDE message has to list exactly
# these (see test_following_the_drop_the_override_message_...): an operator who writes the entry from the message alone
# must end up with a file that the checks below accept.
RETIRED_ENTRY_KEYS = ("retired_on", "reason", "published_as", "replaced", "evidence")


def raw_index():
    return json.loads(DEFAULT_INDEX_PATH.read_text(encoding="utf-8"))


def overrides_doc():
    return json.loads(DEFAULT_OVERRIDES_PATH.read_text(encoding="utf-8"))


def retired_entries(doc=None):
    """The retired section of an overrides document (default: the shipped file) by the operation each entry replaced:
    {"PATCH /v1/clients": entry, ...}. Needs `replaced` in every entry."""
    doc = overrides_doc() if doc is None else doc
    return {entry["replaced"]: entry for entry in doc["retired"].values()}


# The structural checks of the shipped file, as functions of a document, so that the same checks judge the shipped file
# and a file edited the way the loader's message says.


def retired_keys_problems(doc):
    """One line per retired entry whose keys are not exactly RETIRED_ENTRY_KEYS."""
    return [
        f"{name}: has {sorted(entry)}, the documented keys are {sorted(RETIRED_ENTRY_KEYS)}"
        for name, entry in doc["retired"].items()
        if set(entry) != set(RETIRED_ENTRY_KEYS)
    ]


def retired_alarm(doc, raw_ops):
    """(published_as keys the index no longer has, replaced keys the index has again) for the retired entries of `doc`,
    given the operation keys of the published index."""
    entries = retired_entries(doc)
    unpublished = sorted(entry["published_as"] for entry in entries.values() if entry["published_as"] not in raw_ops)
    back = sorted(entry["replaced"] for entry in entries.values() if entry["replaced"] in raw_ops)
    return unpublished, back


# --------------------------------------------------------------------------
# The shipped file
# --------------------------------------------------------------------------


def test_the_overrides_file_sits_next_to_the_index_and_is_valid():
    assert DEFAULT_OVERRIDES_PATH == DEFAULT_INDEX_PATH.with_name(OVERRIDES_FILENAME) == REPO_ROOT / "spec" / "live_overrides.json"
    doc = overrides_doc()
    assert set(doc) == {"description", "replace_ops", "evidence", "retired"}
    assert parse_live_overrides(doc) == {}  # nothing is swapped: the loader accepts an empty replace_ops
    assert doc["replace_ops"] == {} and doc["evidence"] == {}


def test_the_two_overrides_are_retired_with_their_reason_and_their_evidence():
    entries = retired_entries()
    assert set(entries) == set(RETIRED)
    assert len(overrides_doc()["retired"]) == 2
    for replaced, entry in entries.items():
        published_as, _name = RETIRED[replaced]
        assert entry["retired_on"] == "2026-10-02"
        assert entry["published_as"] == published_as and entry["replaced"] == replaced
        assert "e15cb5a18ec2" in entry["reason"] and published_as in entry["reason"] and "Published in contract" in entry["reason"]
        evidence = entry["evidence"]
        assert evidence["verified_on"] == "2026-10-02" and evidence["replaces"] == replaced
        assert evidence["published_spec_sha256"] == PREVIOUS_SPEC_SHA256  # the spec the facts were verified against
        probes = evidence["probes"]
        assert any(
            p["date"] == "2026-10-02" and p["request"] == replaced and p["status"] == 405 and "Allow: GET, POST" in p["note"]
            for p in probes
        )  # the collection form answered 405 ...
        assert any(p["date"] == "2026-10-01" and p["request"] == replaced and p["status"] == 200 for p in probes)  # ... it worked the day before
        path_form = published_as.split(" ", 1)[1]
        assert any(p["date"] == "2026-10-02" and p["status"] == 200 and path_form.rsplit("{", 1)[0] in p["request"] for p in probes)
    clients = entries["PATCH /v1/clients"]["evidence"]["probes"]
    contacts = entries["PATCH /v1/contacts"]["evidence"]["probes"]
    assert any(p["status"] == 404 for p in clients)  # a missing client
    assert any(p["status"] == 400 and "PrimaryEmail" in p["request"] for p in contacts)  # still a full-body command


def test_a_retired_entry_has_the_documented_keys_and_the_loader_ignores_the_section():
    """A retired entry is history: its evidence is kept as written, and the loader never validates or reads it."""
    doc = overrides_doc()
    assert doc["retired"] and retired_keys_problems(doc) == []
    for entry in doc["retired"].values():
        assert set(entry) == set(RETIRED_ENTRY_KEYS)
    assert parse_live_overrides({**doc, "retired": "anything at all"}) == {}
    assert parse_live_overrides({**doc, "retired": []}) == {}


def test_the_description_says_what_the_file_is_for_how_to_drop_an_entry_and_what_retired_is():
    text = " ".join(overrides_doc()["description"])
    for fragment in (
        "never edited by hand",
        "replace_ops maps the published operation key to the live one",
        "An empty replace_ops is valid",
        "retired is history only: the loader ignores it",
        "contract e15cb5a18ec2",
        "fail on purpose",
        "drop the entry",
        "move it to retired",
        "cannot detect this kind of drift: only live calls can",
        "docs/API-OBSERVED-BEHAVIOR.md",
    ):
        assert fragment in text, fragment
    # it names every key a retired entry needs, in the very words of the loader's message (one source:
    # spec.RETIRED_ENTRY_FIELDS), so that an operator who reads only this file writes an entry the tests accept
    assert f"as an object with exactly these keys: {RETIRED_ENTRY_FIELDS}" in text
    assert all(key in text for key in RETIRED_ENTRY_KEYS)
    assert "move it to retired with the five keys listed above" in text


def test_the_raw_index_is_the_spec_of_2026_10_02_and_still_mirrors_the_published_spec():
    raw = raw_index()
    assert len(raw["ops"]) == 98 and raw["op_count"] == 98  # 96 on 2026-10-02, plus the two payments operations of 2026-10-08
    assert raw["contract_sha256"].startswith("56005ff28330") and raw["sha256"] != PREVIOUS_SPEC_SHA256
    for collection, published_as in PUBLISHED_AS.items():
        assert published_as in raw["ops"] and collection not in raw["ops"]
        name = RETIRED[collection][1]
        assert raw["ops"][published_as]["path"] == published_as.split(" ", 1)[1]
        assert raw["ops"][published_as]["path_params"] == {name: INT64}


def test_the_retired_overrides_are_published_in_the_index():
    """THE ALARM. Every retired entry says Gorelo published its operation and removed the collection form it worked
    around. If a refreshed spec/spec_index.json (or a restored old one) no longer says so, the facts the retirement
    rested on are gone: re-verify the live API with the write matrix before anything else."""
    unpublished, back = retired_alarm(overrides_doc(), set(raw_index()["ops"]))
    assert not unpublished, (
        f"spec/spec_index.json no longer contains {unpublished}, which spec/live_overrides.json says were published in "
        "contract e15cb5a18ec2 (retired on 2026-10-02). Re-verify the live API (the write matrix); if the live API needs "
        "an override again, move the entry back to replace_ops with fresh evidence."
    )
    assert not back, f"the removed collection operations {back} are in the published spec again: re-verify the live API"


def active_override_problems(doc, raw):
    """The generic alarm for the ACTIVE overrides of a document, given the raw index data: (published key to live key for
    every live key the index now has, published keys the index no longer has, live keys whose evidence was gathered
    against another published spec)."""
    caught_up = {published: live for published, live in doc["replace_ops"].items() if live in raw["ops"]}
    vanished = sorted(published for published in doc["replace_ops"] if published not in raw["ops"])
    stale = sorted(live for live, entry in doc["evidence"].items() if entry.get("published_spec_sha256") != raw["sha256"])
    return caught_up, vanished, stale


def test_no_active_override_is_caught_up_by_the_published_spec_or_stale():
    """The generic form of the alarm, for any override that is ACTIVE (none today, so it checks nothing yet): the live key
    must not be in the published index (the loader refuses to load otherwise), the published key must be, and the evidence
    must have been gathered against the published spec that is in the repo."""
    caught_up, vanished, stale = active_override_problems(overrides_doc(), raw_index())
    assert not caught_up, (
        f"DROP THE OVERRIDE: spec/spec_index.json now contains {sorted(caught_up.values())}, so the published "
        "spec has caught up with spec/live_overrides.json. Verify the live API (the write matrix), then MOVE the "
        f"entry to the retired section: take {sorted(caught_up)} out of replace_ops and the matching evidence out of "
        f"evidence, and record it under retired, keyed by {sorted(caught_up.values())}, with these fields: "
        f"{RETIRED_ENTRY_FIELDS}. An empty replace_ops is valid, so the file stays. Then point the tools, the guard "
        "and the tests at the published operations, and update docs/API-OBSERVED-BEHAVIOR.md."
    )
    assert not vanished, (
        f"spec/spec_index.json no longer has {vanished}, which spec/live_overrides.json replaces: the published spec "
        "changed under the override. Verify the live API, then update the entry or move it to the retired section "
        f"with these fields: {RETIRED_ENTRY_FIELDS}."
    )
    assert not stale, f"{stale} were verified against another published spec: re-verify them and record the new hash"


# --------------------------------------------------------------------------
# What the loaded index looks like
# --------------------------------------------------------------------------


def test_nothing_is_swapped_in_the_loaded_index(spec_index):
    raw = raw_index()
    assert dict(spec_index.override_ops) == {} and len(spec_index.ops) == 98
    assert list(spec_index.ops) == list(raw["ops"])  # the published operations, in the published order
    assert spec_index.overrides_path == DEFAULT_OVERRIDES_PATH  # the file was read; it swaps nothing
    assert all(op.overridden_from is None and op.is_live_override is False for op in spec_index.ops.values())
    for collection, published_as in PUBLISHED_AS.items():
        assert published_as in spec_index.ops
        with pytest.raises(KeyError, match="not in the spec index"):
            spec_index.op(collection)


def test_everything_in_the_index_is_the_published_data(spec_index):
    raw = raw_index()
    for key, entry in raw["ops"].items():
        op = spec_index.op(key)
        assert (op.method, op.path, op.paged, op.page_size_rule) == (entry["method"], entry["path"], entry["paged"], entry["page_size_rule"])
        assert op.query_params == entry["query_params"] and op.path_params == entry["path_params"], key
        assert op.body == entry["body"] and op.response == entry["response"], key
    assert set(spec_index.schemas) == set(raw["schemas"])
    assert spec_index.sha256 == raw["sha256"] and spec_index.contract_sha256 == raw["contract_sha256"]


@pytest.mark.parametrize("collection", sorted(PUBLISHED_AS))
def test_the_published_path_operation_is_what_the_override_used_to_stand_in_for(spec_index, collection):
    published_as, name = RETIRED[collection]
    op = spec_index.op(published_as)
    assert (op.key, op.method, op.path) == (published_as, "PATCH", published_as.split(" ", 1)[1])
    assert op.path_params == {name: INT64} and op.path_placeholders == (name,)
    assert op.query_params == {} and op.paged is False and op.page_size_rule is None
    assert op.is_multipart is False and op.is_binary is False
    assert op.overridden_from is None and op.is_live_override is False


def test_the_published_index_is_available_on_request():
    published = load_spec_index(overrides=False)
    assert list(published.ops) == list(load_spec_index().ops)
    assert dict(published.override_ops) == {} and published.overrides_path is None
    assert load_spec_index() is not published and load_spec_index(overrides=False) is published


# --------------------------------------------------------------------------
# The published operations validate path ids and bodies
# --------------------------------------------------------------------------


@pytest.mark.parametrize("published_as, name", sorted(RETIRED.values()))
def test_the_path_id_is_validated_as_an_int64(spec_index, published_as, name):
    op = spec_index.op(published_as)
    assert [validate_path_param(op, name, value) for value in (1, 9501, "9501", "007", 2**63 - 1)] == [
        "1", "9501", "9501", "7", str(2**63 - 1),
    ]
    for bad in (None, True, "", "abc", "9501/../x", "../clients", -1, 2**63, 7.0, "1.5", uid(1), [9501]):
        with pytest.raises(SpecViolation) as info:
            validate_path_param(op, name, bad)
        assert info.value.op_key == published_as and info.value.field == name


def test_the_client_body_has_no_id():
    op = load_spec_index().op("PATCH /v1/clients/{clientId}")
    validate_body(op, {"AlternateName": "x"})
    validate_body(op, {"Name": "n", "StatusId": 2, "BillingName": "b", "AlternateName": "a"})
    validate_body(op, {})  # nothing is required
    with pytest.raises(SpecViolation) as info:
        validate_body(op, {"Id": 9501, "AlternateName": "x"})  # the body had an Id until contract e15cb5a18ec2
    assert info.value.field == "Id" and "unknown body field 'Id'" in str(info.value) and "AlternateName" in str(info.value)
    with pytest.raises(SpecViolation) as info:
        validate_body(op, {"Alternate": "x"})
    assert info.value.field == "Alternate" and "did you mean 'AlternateName'" in str(info.value)
    with pytest.raises(SpecViolation, match="does not take a list") as info:
        validate_body(op, {"StatusId": [2]})
    assert info.value.field == "StatusId"


def test_the_contact_body_has_no_contact_id_and_still_wants_the_four_required_fields():
    op = load_spec_index().op("PATCH /v1/contacts/{contactId}")
    full = {"ClientId": 9501, "FirstName": "A", "LastName": "B", "PrimaryEmail": "a@example.invalid"}
    assert sorted(op.body["required"]) == sorted(full)
    validate_body(op, {**full, "SecondaryEmail": []})
    with pytest.raises(SpecViolation) as info:
        validate_body(op, {**full, "ContactId": 5})  # the body had a ContactId until contract e15cb5a18ec2
    assert info.value.field == "ContactId" and "unknown body field 'ContactId'" in str(info.value)
    with pytest.raises(SpecViolation, match="takes a list") as info:
        validate_body(op, {**full, "SecondaryEmail": "a@b"})
    assert info.value.field == "SecondaryEmail"
    with pytest.raises(SpecViolation) as info:
        validate_body(op, {**full, "ClientLocationId": 5})  # the pre-August name
    assert info.value.field == "ClientLocationId" and "LocationId" in str(info.value)


def old_key(published_as, name):
    """The spelling Gorelo used before 2026-10-02: the same operation with its placeholder called id."""
    return published_as.replace("{" + name + "}", "{id}"), "id"


@pytest.mark.anyio
async def test_the_client_sends_the_published_operations_and_refuses_the_removed_ones(client_factory, mock_gorelo):
    mock_gorelo.on("PATCH", "/v1/clients/9501", envelope({"Id": 9501}))
    mock_gorelo.on("PATCH", "/v1/contacts/5", envelope({"Id": 5}))
    async with client_factory() as client:
        await client.patch(
            "PATCH /v1/clients/{clientId}", path_params={"clientId": 9501}, json_body={"AlternateName": "x"}, tool="update_client"
        )
        await client.patch(
            "PATCH /v1/contacts/{contactId}",
            path_params={"contactId": "5"},
            json_body={"ClientId": 1, "FirstName": "A", "LastName": "B", "PrimaryEmail": "a@b.test"},
            tool="update_contact",
        )
        for collection, published_as in PUBLISHED_AS.items():
            # the removed collection form is not an operation any more ...
            with pytest.raises(GoreloAPIError) as info:
                await client.patch(collection, json_body={"AlternateName": "x"}, tool="update_client")
            assert info.value.kind == "spec" and f"unknown Gorelo operation {collection!r}" in str(info.value)
            # ... and neither is the path form under its old placeholder name
            name = RETIRED[collection][1]
            old, old_name = old_key(published_as, name)
            with pytest.raises(GoreloAPIError) as info:
                await client.patch(old, path_params={old_name: 1}, json_body={}, tool="update_client")
            assert info.value.kind == "spec" and f"unknown Gorelo operation {old!r}" in str(info.value)
        with pytest.raises(SpecViolation, match="path parameter 'clientId' must be a whole number"):
            await client.patch("PATCH /v1/clients/{clientId}", path_params={"clientId": uid(1)}, json_body={}, tool="t")
        with pytest.raises(SpecViolation, match="path parameter 'clientId' is required"):
            await client.patch("PATCH /v1/clients/{clientId}", json_body={"AlternateName": "x"}, tool="t")
        with pytest.raises(SpecViolation, match="unknown path parameter 'id'"):
            await client.patch("PATCH /v1/clients/{clientId}", path_params={"id": 9501}, json_body={}, tool="t")
        for key, params, body in (
            ("PATCH /v1/clients/{clientId}", {"clientId": 9501}, {"Id": 9501, "AlternateName": "x"}),
            ("PATCH /v1/contacts/{contactId}", {"contactId": 5}, {"ContactId": 5, "ClientId": 1}),
        ):
            with pytest.raises(SpecViolation, match="unknown body field"):  # a body id is refused before any HTTP call
                await client.patch(key, path_params=params, json_body=body, tool="t")
    assert [(r.method, r.path, sorted(r.json)) for r in mock_gorelo.requests] == [
        ("PATCH", "/v1/clients/9501", ["AlternateName"]),
        ("PATCH", "/v1/contacts/5", ["ClientId", "FirstName", "LastName", "PrimaryEmail"]),
    ]


def test_the_tools_declare_the_published_operations_and_no_removed_one(spec_index):
    declared = {op for spec in REGISTRY.specs for op in spec.ops}
    assert not declared & set(RETIRED)  # the removed collection forms
    assert declared <= set(spec_index.ops)
    assert not declared & set(spec_index.override_ops)  # nothing is an override any more
    for published_as, _name in RETIRED.values():
        users = {spec.name for spec in REGISTRY.specs if published_as in spec.ops}
        assert users == {"update_client" if "clients" in published_as else "update_contact"}, published_as


# --------------------------------------------------------------------------
# The loader, on small indexes of its own
# --------------------------------------------------------------------------

THING_ID = {"type": "integer", "format": "int64"}


def published(**ops):
    """A small published index: a thing with GET and DELETE by id (id typed int64) and the collection PATCH that the
    live API no longer has. `ops` adds or replaces operations by key."""
    body = {
        "content_type": "application/json-patch+json",
        "schema": "UpdateThing",
        "required": ["Id"],
        "fields": {"Id": {"type": "integer", "format": "int64", "nullable": False}, "Name": {"type": "string", "nullable": True}},
    }
    entries = {
        "GET /v1/things/{thingId}": {
            "method": "GET", "path": "/v1/things/{thingId}", "operation_id": "get_v1_things_thingId", "paged": False,
            "page_size_rule": None, "path_params": {"thingId": dict(THING_ID)}, "query_params": {}, "body": None,
            "response": {"kind": "envelope", "schema": "ThingBaseResponse", "data": "Thing"}, "status_codes": ["200", "404"],
        },
        "DELETE /v1/things/{thingId}": {
            "method": "DELETE", "path": "/v1/things/{thingId}", "operation_id": "delete_v1_things_thingId", "paged": False,
            "page_size_rule": None, "path_params": {"thingId": dict(THING_ID)}, "query_params": {}, "body": None,
            "response": {"kind": "envelope", "schema": None, "data": None}, "status_codes": ["200"],
        },
        "PATCH /v1/things": {
            "method": "PATCH", "path": "/v1/things", "operation_id": "patch_v1_things", "paged": False,
            "page_size_rule": None, "path_params": {}, "query_params": {}, "body": body,
            "response": {"kind": "envelope", "schema": "ThingBaseResponse", "data": "Thing"}, "status_codes": ["200", "400"],
        },
        "POST /v1/things": {
            "method": "POST", "path": "/v1/things", "operation_id": "post_v1_things", "paged": False,
            "page_size_rule": None, "path_params": {}, "query_params": {}, "body": body,
            "response": {"kind": "envelope", "schema": None, "data": None}, "status_codes": ["200"],
        },
    }
    entries.update(ops)
    return {
        "source": "test", "sha256": "a" * 64, "contract_sha256": "b" * 64, "openapi": "3.0.1",
        "op_count": len(entries), "ops": entries, "schemas": {"Thing": {"type": "object", "required": [], "fields": {}}},
    }


def overrides(**changes):
    """A valid overrides document for published(): PATCH /v1/things becomes PATCH /v1/things/{thingId}."""
    doc = {
        "description": "test",
        "replace_ops": {"PATCH /v1/things": "PATCH /v1/things/{thingId}"},
        "evidence": {
            "PATCH /v1/things/{thingId}": {
                "verified_on": "2026-10-02",
                "probes": [{"date": "2026-10-02", "request": "PATCH /v1/things", "status": 405}],
            }
        },
    }
    doc.update(changes)
    return doc


def write(directory, name, document):
    path = directory / name
    path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
    return path


def test_the_overrides_next_to_an_index_are_applied(tmp_path):
    index = write(tmp_path, "spec_index.json", published())
    write(tmp_path, OVERRIDES_FILENAME, overrides())
    loaded = load_spec_index(index)
    assert "PATCH /v1/things" not in loaded.ops and "PATCH /v1/things/{thingId}" in loaded.ops
    assert dict(loaded.override_ops) == {"PATCH /v1/things/{thingId}": "PATCH /v1/things"}
    assert loaded.overrides_path == (tmp_path / OVERRIDES_FILENAME).resolve() and loaded.path == index.resolve()
    op = loaded.op("PATCH /v1/things/{thingId}")
    assert (op.method, op.path, op.path_params, op.overridden_from) == ("PATCH", "/v1/things/{thingId}", {"thingId": THING_ID}, "PATCH /v1/things")
    assert op.body["fields"]["Name"] == {"type": "string", "nullable": True} and op.response["data"] == "Thing"
    assert validate_path_param(op, "thingId", "12") == "12"


def test_an_index_without_overrides_next_to_it_is_loaded_as_it_is(tmp_path):
    index = write(tmp_path, "spec_index.json", published())
    loaded = load_spec_index(index)
    assert "PATCH /v1/things" in loaded.ops and dict(loaded.override_ops) == {} and loaded.overrides_path is None
    assert all(op.overridden_from is None for op in loaded.ops.values())


def test_overrides_can_be_switched_off_or_given_as_a_path(tmp_path):
    index = write(tmp_path, "spec_index.json", published())
    write(tmp_path, OVERRIDES_FILENAME, overrides())
    elsewhere = write(tmp_path, "other.json", overrides())
    assert "PATCH /v1/things" in load_spec_index(index, overrides=False).ops
    assert "PATCH /v1/things" in load_spec_index(index, overrides=None).ops
    given = load_spec_index(index, overrides=elsewhere)
    assert "PATCH /v1/things/{thingId}" in given.ops and given.overrides_path == elsewhere.resolve()
    assert "PATCH /v1/things/{thingId}" in load_spec_index(index, overrides=str(elsewhere)).ops
    with pytest.raises(FileNotFoundError):
        load_spec_index(index, overrides=tmp_path / "missing.json")


def test_the_spec_index_constructor_applies_overrides_only_when_given_them():
    plain = SpecIndex(published())
    assert "PATCH /v1/things" in plain.ops and dict(plain.override_ops) == {} and plain.overrides_path is None
    live = SpecIndex(published(), overrides=overrides())
    assert "PATCH /v1/things" not in live.ops and "PATCH /v1/things/{thingId}" in live.ops
    assert dict(live.override_ops) == {"PATCH /v1/things/{thingId}": "PATCH /v1/things"} and live.overrides_path is None
    assert live.sha256 == "a" * 64  # the hash of the published spec


def test_the_cache_follows_both_files_and_keeps_the_published_index_apart(tmp_path):
    index = write(tmp_path, "spec_index.json", published())
    sibling = write(tmp_path, OVERRIDES_FILENAME, overrides())
    first = load_spec_index(index)
    assert load_spec_index(index) is first
    unmodified = load_spec_index(index, overrides=False)
    assert unmodified is not first and load_spec_index(index) is first  # asking for the published one evicts nothing
    assert load_spec_index(index, overrides=False) is unmodified
    # an edited overrides file is picked up without a restart
    changed = overrides()
    changed["evidence"]["PATCH /v1/things/{thingId}"]["probes"].append({"date": "2026-10-03", "request": "PATCH /v1/things", "status": 405})
    sibling.write_text(json.dumps(changed), encoding="utf-8")
    stat = sibling.stat()
    os.utime(sibling, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    second = load_spec_index(index)
    assert second is not first and load_spec_index(index) is second
    # and deleting the overrides file turns the overlay off
    sibling.unlink()
    third = load_spec_index(index)
    assert third.overrides_path is None and "PATCH /v1/things" in third.ops


def test_bad_json_in_the_overrides_file_is_a_clear_error(tmp_path):
    index = write(tmp_path, "spec_index.json", published())
    write(tmp_path, OVERRIDES_FILENAME, "{not json")
    with pytest.raises(ValueError, match="live overrides .*live_overrides.json is not valid JSON"):
        load_spec_index(index)


def test_the_raw_data_is_never_modified_and_the_published_operation_id_is_not_carried_over():
    data = published()
    before = copy.deepcopy(data)
    new_data, replaced = apply_live_overrides(data, overrides())
    assert data == before
    assert replaced == {"PATCH /v1/things/{thingId}": "PATCH /v1/things"}
    entry = new_data["ops"]["PATCH /v1/things/{thingId}"]
    assert "operation_id" not in entry and "status_codes" not in entry  # they describe another operation
    assert entry["body"] == before["ops"]["PATCH /v1/things"]["body"] and entry["body"] is not data["ops"]["PATCH /v1/things"]["body"]
    assert list(new_data["ops"]) == ["GET /v1/things/{thingId}", "DELETE /v1/things/{thingId}", "PATCH /v1/things/{thingId}", "POST /v1/things"]
    assert new_data["sha256"] == "a" * 64


# --------------------------------------------------------------------------
# Loud failures
# --------------------------------------------------------------------------


def test_loading_fails_when_the_published_spec_has_caught_up(tmp_path):
    caught_up = published(**{"PATCH /v1/things/{thingId}": {**published()["ops"]["PATCH /v1/things"], "path": "/v1/things/{thingId}"}})
    index = write(tmp_path, "spec_index.json", caught_up)
    write(tmp_path, OVERRIDES_FILENAME, overrides())
    with pytest.raises(ValueError) as info:
        load_spec_index(index)
    text = str(info.value)
    assert "the published spec index now has 'PATCH /v1/things/{thingId}'" in text and "caught up" in text
    assert "DROP THE OVERRIDE: re-verify the live API, then MOVE the entry to the retired section of" in text
    assert "take 'PATCH /v1/things' out of replace_ops and 'PATCH /v1/things/{thingId}' out of evidence" in text
    # it names every field a retired entry needs, not only retired_on and the reason, and the key it goes under
    assert f"record it under retired, keyed by 'PATCH /v1/things/{{thingId}}', with these fields: {RETIRED_ENTRY_FIELDS}." in text
    assert listed_retired_fields(text) == list(RETIRED_ENTRY_KEYS)
    assert "An empty replace_ops is valid, so the file stays" in text
    assert "check that the tools and tests use the published operations" in text
    assert "live_overrides.json" in text
    # the old advice (delete the entry, delete the file) is gone: the history of an override is never thrown away
    assert "delete" not in text.replace(str(tmp_path), "").lower()


def listed_retired_fields(message):
    """The field names a loader message lists for a retired entry, in its order. They sit in the sentence that starts
    'with these fields:', each name in front of its explanation in brackets: "retired_on (YYYY-MM-DD), reason (text)..."."""
    clause = re.search(r"with these fields: (.+?)(?:\. (?=[A-Z])|$)", message)
    assert clause, f"the message does not list the fields of a retired entry: {message}"
    return re.findall(r"(?:^|, | and )([a-z]+(?:_[a-z]+)*) \(", clause.group(1))


def test_following_the_drop_the_override_message_leaves_a_file_the_shipped_file_checks_accept(tmp_path):
    """The message is a procedure, so do exactly what it says, and nothing it does not say: MOVE the entry to `retired`
    (keyed by the live key) with the fields it lists, and keep the file. The entry is built ONLY from the fields the
    message lists; then the loader accepts the file, swaps nothing and reads nothing from the history, and the checks of
    the shipped file (the documented keys, `replaced` to key the entry, the alarm that the published operation is in the
    index and the replaced one is gone) pass on it. A message that left a field out would leave an entry that fails them."""
    live_key, published_key = "PATCH /v1/things/{thingId}", "PATCH /v1/things"
    caught_up = published(**{live_key: {**published()["ops"][published_key], "path": "/v1/things/{thingId}"}})
    del caught_up["ops"][published_key]  # what Gorelo did: it published the live operation and dropped the old one
    caught_up["op_count"] = len(caught_up["ops"])
    index = write(tmp_path, "spec_index.json", caught_up)
    doc = overrides()
    write(tmp_path, OVERRIDES_FILENAME, doc)
    with pytest.raises(ValueError, match="DROP THE OVERRIDE: re-verify the live API, then MOVE the entry") as info:
        load_spec_index(index)
    message = str(info.value)
    # what an operator can put in each field, from the way the message describes it
    can_write = {
        "retired_on": "2026-10-03",  # (YYYY-MM-DD)
        "reason": "Published as PATCH /v1/things/{thingId} in a later contract.",  # (text)
        "published_as": live_key,  # (the operation key the published index has for it now)
        "replaced": published_key,  # (the published key the override stood in for)
        "evidence": doc["evidence"][live_key],  # (the old evidence, unchanged)
    }
    listed = listed_retired_fields(message)
    assert set(listed) <= set(can_write), f"the message lists a field this test cannot fill in: {listed}"
    moved = {name: can_write[name] for name in listed}  # nothing the message does not list is added
    after = {**doc, "replace_ops": {}, "evidence": {}, "retired": {live_key: moved}}  # the entry left both active tables
    write(tmp_path, OVERRIDES_FILENAME, after)
    loaded = load_spec_index(index)
    assert dict(loaded.override_ops) == {} and list(loaded.ops) == list(caught_up["ops"])  # the published spec as it is
    assert all(op.overridden_from is None and not op.is_live_override for op in loaded.ops.values())
    written = json.loads((tmp_path / OVERRIDES_FILENAME).read_text(encoding="utf-8"))
    assert written["retired"][live_key] == moved
    # the checks of the shipped file, run on the file the message produced
    assert set(written) == {"description", "replace_ops", "evidence", "retired"} and written["replace_ops"] == {}
    assert written["evidence"] == {} and parse_live_overrides(written) == {}  # nothing active is left
    assert retired_keys_problems(written) == []
    assert set(written["retired"][live_key]) == set(RETIRED_ENTRY_KEYS)
    assert set(retired_entries(written)) == {published_key}  # keyed by `replaced`: it needs that field
    assert retired_alarm(written, set(caught_up["ops"])) == ([], [])
    assert active_override_problems(written, caught_up) == ({}, [], [])


COMPLETE_ENTRY = {
    "retired_on": "2026-10-03",
    "reason": "Published in a later contract.",
    "published_as": "PATCH /v1/things/{thingId}",
    "replaced": "PATCH /v1/things",
    "evidence": overrides()["evidence"]["PATCH /v1/things/{thingId}"],
}


def history_of(entry):
    return {"replace_ops": {}, "evidence": {}, "retired": {"PATCH /v1/things/{thingId}": entry}}


@pytest.mark.parametrize("left_out", RETIRED_ENTRY_KEYS)
def test_an_entry_missing_any_field_the_message_must_list_fails_the_shipped_file_checks(left_out):
    """The round-trip test has teeth: an entry written from a message that left out one field is caught."""
    assert set(COMPLETE_ENTRY) == set(RETIRED_ENTRY_KEYS) and retired_keys_problems(history_of(COMPLETE_ENTRY)) == []
    short = {key: value for key, value in COMPLETE_ENTRY.items() if key != left_out}
    assert retired_keys_problems(history_of(short)) == [
        f"PATCH /v1/things/{{thingId}}: has {sorted(short)}, the documented keys are {sorted(RETIRED_ENTRY_KEYS)}"
    ]
    if left_out in ("replaced", "published_as"):  # the two fields the alarm reads
        with pytest.raises(KeyError) as info:
            retired_alarm(history_of(short), {"PATCH /v1/things/{thingId}"})
        assert info.value.args == (left_out,)


def test_the_entry_the_old_message_asked_for_fails_the_shipped_file_checks():
    """the old message named only retired_on, the reason and the old evidence. An entry with just those fails
    the documented-keys check and cannot even be keyed by what it replaced (retired_entries reads `replaced`)."""
    old_advice = {key: COMPLETE_ENTRY[key] for key in ("retired_on", "reason", "evidence")}
    doc = history_of(old_advice)
    assert retired_keys_problems(doc)
    with pytest.raises(KeyError) as info:
        retired_entries(doc)
    assert info.value.args == ("replaced",)


def test_loading_fails_when_the_published_operation_is_gone(tmp_path):
    data = published()
    del data["ops"]["PATCH /v1/things"]
    write(tmp_path, OVERRIDES_FILENAME, overrides())
    with pytest.raises(
        ValueError,
        match="replaces 'PATCH /v1/things', which is not in the spec index.*update the entry or move it to the retired section",
    ) as info:
        load_spec_index(write(tmp_path, "spec_index.json", data))
    # this message too sends a person to the retired section, so it lists every field of a retired entry
    assert f"keyed by 'PATCH /v1/things/{{thingId}}', with these fields: {RETIRED_ENTRY_FIELDS}" in str(info.value)
    assert listed_retired_fields(str(info.value)) == list(RETIRED_ENTRY_KEYS)


def test_a_placeholder_no_published_operation_types_is_refused():
    data = published()
    del data["ops"]["GET /v1/things/{thingId}"], data["ops"]["DELETE /v1/things/{thingId}"]
    with pytest.raises(ValueError, match=r"cannot type the path parameter \{thingId\} of 'PATCH /v1/things/\{thingId\}'"):
        apply_live_overrides(data, overrides())


def test_placeholders_the_published_operations_type_differently_are_refused():
    data = published()
    data["ops"]["DELETE /v1/things/{thingId}"]["path_params"]["thingId"] = {"type": "string", "format": "uuid"}
    with pytest.raises(ValueError, match=r"type the path parameter \{thingId\} of 'PATCH /v1/things/\{thingId\}' in different ways"):
        apply_live_overrides(data, overrides())


def test_a_live_operation_without_a_placeholder_needs_no_typing():
    data = published()
    doc = overrides(
        replace_ops={"PATCH /v1/things": "PUT /v1/things/all"},
        evidence={"PUT /v1/things/all": overrides()["evidence"]["PATCH /v1/things/{thingId}"]},
    )
    new_data, replaced = apply_live_overrides(data, doc)
    assert new_data["ops"]["PUT /v1/things/all"]["method"] == "PUT" and new_data["ops"]["PUT /v1/things/all"]["path_params"] == {}
    assert replaced == {"PUT /v1/things/all": "PATCH /v1/things"}


bad = overrides  # a valid document with some keys replaced or added


def without(key):
    doc = overrides()
    del doc[key]
    return doc


GOOD_EVIDENCE = overrides()["evidence"]["PATCH /v1/things/{thingId}"]


@pytest.mark.parametrize(
    "document, fragment",
    [
        pytest.param([], "must be a JSON object with replace_ops and evidence", id="not-an-object"),
        pytest.param("text", "must be a JSON object", id="text"),
        pytest.param(
            bad(replace=1), "unknown key(s) replace; allowed: description, replace_ops, evidence, retired", id="unknown-key"
        ),
        pytest.param(
            bad(retire={}), "unknown key(s) retire; allowed: description, replace_ops, evidence, retired", id="retired-misspelled"
        ),
        pytest.param(
            without("replace_ops") | {"retired": {}}, "replace_ops must be an object", id="retired-does-not-replace-replace-ops"
        ),
        pytest.param(
            bad(replace_ops={}, evidence={"PATCH /v1/stray/{strayId}": GOOD_EVIDENCE}, retired={"PATCH /v1/stray/{strayId}": {}}),
            "evidence for PATCH /v1/stray/{strayId}, which replace_ops does not list",
            id="retired-does-not-excuse-stray-evidence",
        ),
        pytest.param(without("replace_ops"), "replace_ops must be an object", id="no-replace-ops"),
        pytest.param(bad(replace_ops=[]), "replace_ops must be an object", id="replace-ops-list"),
        pytest.param(without("evidence"), "evidence must be an object", id="no-evidence"),
        pytest.param(bad(description=5), "description must be text or a list of text", id="description-number"),
        pytest.param(bad(description=["ok", ""]), "description must be text or a list of text", id="description-blank-line"),
        pytest.param(
            bad(replace_ops={"PATCH /v1/things": "/v1/things/{thingId}"}), "live operation key that is not 'METHOD /v1/path'", id="live-key-no-method"
        ),
        pytest.param(
            bad(replace_ops={"patch /v1/things": "PATCH /v1/things/{thingId}"}), "published operation key that is not", id="published-key-lowercase"
        ),
        pytest.param(bad(replace_ops={"PATCH /v1/things": 5}), "live operation key that is not", id="live-key-number"),
        pytest.param(
            bad(replace_ops={"PATCH /v1/things": "PATCH /v1/things"}), "replaces 'PATCH /v1/things' with itself", id="itself"
        ),
        pytest.param(
            bad(
                replace_ops={"PATCH /v1/things": "PATCH /v1/things/{thingId}", "POST /v1/things": "PATCH /v1/things/{thingId}"},
            ),
            "more than one published operation is replaced by PATCH /v1/things/{thingId}",
            id="same-live-key-twice",
        ),
        pytest.param(
            bad(
                replace_ops={"PATCH /v1/things": "PATCH /v1/things/{thingId}", "PATCH /v1/things/{thingId}": "PATCH /v1/other/{otherId}"},
                evidence={"PATCH /v1/things/{thingId}": GOOD_EVIDENCE, "PATCH /v1/other/{otherId}": GOOD_EVIDENCE},
            ),
            "PATCH /v1/things/{thingId} is both replaced and a replacement",
            id="chain",
        ),
        pytest.param(bad(evidence={}), "no evidence for 'PATCH /v1/things/{thingId}'", id="no-evidence-for-the-key"),
        pytest.param(
            bad(evidence={"PATCH /v1/things/{thingId}": GOOD_EVIDENCE, "PATCH /v1/stray/{strayId}": GOOD_EVIDENCE}),
            "evidence for PATCH /v1/stray/{strayId}, which replace_ops does not list",
            id="stray-evidence",
        ),
        pytest.param(bad(evidence={"PATCH /v1/things/{thingId}": "seen"}), "evidence for 'PATCH /v1/things/{thingId}' must be an object", id="evidence-text"),
        pytest.param(
            bad(evidence={"PATCH /v1/things/{thingId}": {**GOOD_EVIDENCE, "verified_on": "yesterday"}}),
            "needs verified_on, a date written YYYY-MM-DD",
            id="date-text",
        ),
        pytest.param(
            bad(evidence={"PATCH /v1/things/{thingId}": {k: v for k, v in GOOD_EVIDENCE.items() if k != "verified_on"}}),
            "needs verified_on",
            id="no-date",
        ),
        pytest.param(
            bad(evidence={"PATCH /v1/things/{thingId}": {**GOOD_EVIDENCE, "probes": []}}), "needs probes, a non-empty list", id="no-probes"
        ),
        pytest.param(
            bad(evidence={"PATCH /v1/things/{thingId}": {**GOOD_EVIDENCE, "probes": ["405"]}}), "probe 1 needs date", id="probe-text"
        ),
        pytest.param(
            bad(evidence={"PATCH /v1/things/{thingId}": {**GOOD_EVIDENCE, "probes": [{"date": "2026-10-02", "request": "x", "status": "405"}]}}),
            "probe 1 needs date (YYYY-MM-DD), request (text) and status",
            id="status-text",
        ),
        pytest.param(
            bad(evidence={"PATCH /v1/things/{thingId}": {**GOOD_EVIDENCE, "probes": [{"date": "2026-10-02", "request": "x", "status": True}]}}),
            "probe 1 needs date",
            id="status-bool",
        ),
        pytest.param(
            bad(evidence={"PATCH /v1/things/{thingId}": {**GOOD_EVIDENCE, "probes": [{"date": "10/02", "request": "x", "status": 405}]}}),
            "probe 1 needs date",
            id="probe-date",
        ),
        pytest.param(
            bad(evidence={"PATCH /v1/things/{thingId}": {**GOOD_EVIDENCE, "probes": [GOOD_EVIDENCE["probes"][0], {"date": "2026-10-02", "request": " ", "status": 405}]}}),
            "probe 2 needs date",
            id="second-probe-blank-request",
        ),
    ],
)
def test_a_malformed_overrides_document_is_refused_with_a_message_that_names_the_problem(document, fragment):
    with pytest.raises(ValueError) as info:
        parse_live_overrides(document, source="the file")
    assert fragment in str(info.value) and str(info.value).startswith("the file: ")
    with pytest.raises(ValueError, match="the file: "):
        apply_live_overrides(published(), document, source="the file")


def test_an_empty_replace_ops_is_valid_and_applies_nothing(tmp_path):
    index = write(tmp_path, "spec_index.json", published())
    write(tmp_path, OVERRIDES_FILENAME, {"replace_ops": {}, "evidence": {}})
    loaded = load_spec_index(index)
    assert list(loaded.ops) == list(published()["ops"]) and dict(loaded.override_ops) == {}
    assert loaded.overrides_path == (tmp_path / OVERRIDES_FILENAME).resolve()  # the file was read ...
    assert all(op.overridden_from is None and not op.is_live_override for op in loaded.ops.values())  # ... and swapped nothing
    new_data, replaced = apply_live_overrides(published(), {"replace_ops": {}, "evidence": {}})
    assert replaced == {} and new_data["ops"] == published()["ops"]


@pytest.mark.parametrize(
    "retired",
    [
        {"PATCH /v1/things/{thingId}": {"retired_on": "2026-10-02", "reason": "published", "evidence": {"probes": []}}},
        {},
        [],
        "history as text",
        5,
        None,
    ],
    ids=["history", "empty-object", "list", "text", "number", "null"],
)
def test_a_retired_section_is_accepted_and_ignored_whatever_it_holds(tmp_path, retired):
    assert parse_live_overrides({"replace_ops": {}, "evidence": {}, "retired": retired}) == {}
    index = write(tmp_path, "spec_index.json", published())
    write(tmp_path, OVERRIDES_FILENAME, {**overrides(), "retired": retired})
    loaded = load_spec_index(index)  # the active override is applied, and `retired` changes nothing about it
    assert dict(loaded.override_ops) == {"PATCH /v1/things/{thingId}": "PATCH /v1/things"}
    assert "PATCH /v1/things" not in loaded.ops and "PATCH /v1/things/{thingId}" in loaded.ops


def test_a_retired_key_does_not_count_as_a_published_or_a_live_key(tmp_path):
    """Retiring an entry removes it from replace_ops, so the loader neither swaps it nor complains that the published
    spec already has it (the published key being present is exactly why it was retired)."""
    caught_up = published(**{"PATCH /v1/things/{thingId}": {**published()["ops"]["PATCH /v1/things"], "path": "/v1/things/{thingId}"}})
    history = {"PATCH /v1/things/{thingId}": {"retired_on": "2026-10-02", "reason": "published", "replaced": "PATCH /v1/things"}}
    index = write(tmp_path, "spec_index.json", caught_up)
    write(tmp_path, OVERRIDES_FILENAME, {"replace_ops": {}, "evidence": {}, "retired": history})
    loaded = load_spec_index(index)
    assert "PATCH /v1/things/{thingId}" in loaded.ops and "PATCH /v1/things" in loaded.ops
    assert dict(loaded.override_ops) == {}


def test_a_valid_document_parses_to_its_replace_table():
    assert parse_live_overrides(overrides()) == {"PATCH /v1/things": "PATCH /v1/things/{thingId}"}
    assert parse_live_overrides({**overrides(), "description": ["one", "two"]}) == {"PATCH /v1/things": "PATCH /v1/things/{thingId}"}
    only = {"replace_ops": {}, "evidence": {}}
    assert parse_live_overrides(only) == {}  # nothing to replace is not an error
