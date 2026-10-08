"""spec.py: loading spec/spec_index.json, op lookup, query normalisation and body validation."""

import json
import os
import uuid
from pathlib import Path

import pytest

import spec as spec_module
from spec import (
    DEFAULT_INDEX_PATH,
    DEFAULT_OVERRIDES_PATH,
    OpSpec,
    SpecIndex,
    SpecViolation,
    load_spec_index,
    normalize_query,
    validate_body,
    validate_path_param,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def tiny_data():
    """A small synthetic spec index with nested objects, a list of objects, an enum ref and multipart."""
    obj = {"nullable": False, "type": None}
    return {
        "source": "test",
        "sha256": "a" * 64,
        "contract_sha256": "b" * 64,
        "openapi": "3.0.1",
        "ops": {
            "GET /v1/things": {
                "method": "GET", "path": "/v1/things", "paged": True, "page_size_rule": "clamp",
                "path_params": {},
                "query_params": {
                    "Cursor": {"type": "string", "required": False},
                    "PageSize": {"type": "integer", "required": False},
                    "StatusIds": {"type": "string", "required": False},
                },
                "body": None, "response": {"kind": "envelope", "schema": None, "data": "[Thing]"},
            },
            "GET /v1/things/{thingId}": {
                "method": "GET", "path": "/v1/things/{thingId}", "paged": False, "page_size_rule": None,
                "path_params": {"thingId": {"type": "string"}}, "query_params": {},
                "body": None, "response": {"kind": "envelope", "schema": None, "data": "Thing"},
            },
            "GET /v1/things/{thingId}/file": {
                "method": "GET", "path": "/v1/things/{thingId}/file", "paged": False, "page_size_rule": None,
                "path_params": {"thingId": {"type": "string"}}, "query_params": {},
                "body": None, "response": {"kind": "binary", "schema": None, "data": None},
            },
            "GET /v1/needs": {
                "method": "GET", "path": "/v1/needs", "paged": False, "page_size_rule": None,
                "path_params": {}, "query_params": {"Must": {"type": "string", "required": True}},
                "body": None, "response": {"kind": "envelope", "schema": None, "data": None},
            },
            "POST /v1/things": {
                "method": "POST", "path": "/v1/things", "paged": False, "page_size_rule": None,
                "path_params": {}, "query_params": {},
                "body": {
                    "content_type": "application/json-patch+json", "schema": "CreateThing", "required": ["Name"],
                    "fields": {
                        "Name": {"type": "string", "nullable": False},
                        "Location": {**obj, "ref": "Place"},
                        "Parts": {"type": "array", "nullable": True, "items": {"type": None, "ref": "Part"}},
                        "Tags": {"type": "array", "nullable": True, "items": {"type": "integer"}},
                        "Level": {**obj, "ref": "Level"},
                        "Count": {"type": "integer", "nullable": False},
                        "Inline": {
                            "type": "object", "nullable": True,
                            "fields": {"Depth": {"type": "integer", "nullable": False}}, "required_fields": [],
                        },
                        "Free": {"type": "object", "nullable": True},
                    },
                },
                "response": {"kind": "envelope", "schema": None, "data": "Thing"},
            },
            "POST /v1/things/{thingId}/upload": {
                "method": "POST", "path": "/v1/things/{thingId}/upload", "paged": False, "page_size_rule": None,
                "path_params": {"thingId": {"type": "string"}}, "query_params": {},
                "body": {
                    "content_type": "multipart/form-data", "schema": None, "required": [],
                    "fields": {
                        "file": {"type": "string", "nullable": False, "format": "binary"},
                        "itemType": {"type": "string", "nullable": False},
                    },
                },
                "response": {"kind": "envelope", "schema": None, "data": "Thing"},
            },
        },
        "schemas": {
            "Place": {
                "type": "object", "required": ["Name"],
                "fields": {"Name": {"type": "string", "nullable": False}, "Phone": {"type": "string", "nullable": True}},
            },
            "Part": {
                "type": "object", "required": [],
                "fields": {"Quantity": {"type": "integer", "nullable": False}, "Sku": {"type": "string", "nullable": True}},
            },
            "Level": {"type": "integer", "format": "int32", "required": [], "fields": {}, "enum": [1, 2, 3]},
        },
    }


@pytest.fixture
def tiny():
    return SpecIndex(tiny_data())


# --------------------------------------------------------------------------
# The real index
# --------------------------------------------------------------------------


def test_default_index_path_is_next_to_the_module():
    assert DEFAULT_INDEX_PATH == REPO_ROOT / "spec" / "spec_index.json"
    assert Path(spec_module.__file__).resolve().parent == REPO_ROOT


def test_the_real_index_loads_with_every_operation(spec_index):
    raw = json.loads(DEFAULT_INDEX_PATH.read_text(encoding="utf-8"))
    # the live overrides (spec/live_overrides.json, tests/test_live_overrides.py) swap nothing today: replace_ops is
    # empty since Gorelo published the two operations it used to work around (contract e15cb5a18ec2)
    swapped = json.loads(DEFAULT_OVERRIDES_PATH.read_text(encoding="utf-8"))["replace_ops"]
    assert swapped == {}
    assert set(spec_index.ops) == (set(raw["ops"]) - set(swapped)) | set(swapped.values())
    assert len(spec_index.ops) == len(raw["ops"]) == 98
    # the hashes still describe the published spec, which is what the changelog watcher compares
    assert spec_index.sha256 == raw["sha256"] and spec_index.contract_sha256 == raw["contract_sha256"]
    assert spec_index.openapi == raw["openapi"] and spec_index.source == raw["source"]


def test_opspec_fields_for_a_real_operation(spec_index):
    op = spec_index.op("GET /v1/tickets/{ticketId}")
    assert isinstance(op, OpSpec)
    assert (op.key, op.method, op.path) == ("GET /v1/tickets/{ticketId}", "GET", "/v1/tickets/{ticketId}")
    assert op.path_params == {"ticketId": {"type": "string", "format": "uuid"}}
    assert op.path_placeholders == ("ticketId",)
    assert op.query_params == {} and op.paged is False and op.page_size_rule is None and op.body is None
    assert op.response["kind"] == "envelope"


def test_query_params_keep_the_exact_spec_casing(spec_index):
    op = spec_index.op("GET /v1/tickets")
    assert op.paged is True and op.page_size_rule == "clamp"
    assert {"StatusIds", "ClientIds", "Cursor", "PageSize", "UpdatedSince"} <= set(op.query_params)
    assert "statusids" not in op.query_params


def test_body_and_multipart_and_binary_properties(spec_index):
    clients = spec_index.op("POST /v1/clients")
    assert clients.body is not None and clients.body["schema"] == "CreateClientCommand"
    assert clients.is_multipart is False and clients.is_binary is False
    upload = spec_index.op("POST /v1/attachments")
    assert upload.is_multipart is True
    assert spec_index.op("GET /v1/invoices/{invoiceId}/pdf").is_binary is True


def test_unknown_operation_is_a_keyerror_with_a_clear_message(spec_index):
    with pytest.raises(KeyError) as info:
        spec_index.op("GET /v1/ticket")
    message = info.value.args[0]
    assert "unknown Gorelo operation" in message and "'GET /v1/ticket'" in message
    assert "GET /v1/tickets" in message  # a close match is suggested


def test_operation_keys_are_exact(spec_index):
    for wrong in ("get /v1/tickets", "GET /v1/tickets/", "GET v1/tickets", "GET /tickets"):
        with pytest.raises(KeyError):
            spec_index.op(wrong)


def test_schema_lookup(spec_index):
    schema = spec_index.schema("ClientLocationRequest")
    assert "Phone" in schema["fields"] and schema["required"] == ["Name"]
    with pytest.raises(KeyError, match="unknown Gorelo schema"):
        spec_index.schema("NoSuchSchema")
    assert "TicketPriority" in spec_index.schemas


def test_ops_is_read_only(spec_index):
    with pytest.raises(TypeError):
        spec_index.ops["GET /v1/x"] = None  # type: ignore[index]


# --------------------------------------------------------------------------
# Loading and caching
# --------------------------------------------------------------------------


def test_load_is_cached_per_path():
    assert load_spec_index() is load_spec_index()
    assert load_spec_index() is load_spec_index(DEFAULT_INDEX_PATH)
    assert load_spec_index() is load_spec_index(REPO_ROOT / "spec" / ".." / "spec" / "spec_index.json")


def test_a_different_path_is_a_different_index_and_a_changed_file_is_reloaded(tmp_path):
    path = tmp_path / "index.json"
    path.write_text(json.dumps(tiny_data()), encoding="utf-8")
    first = load_spec_index(path)
    assert first is not load_spec_index() and first is load_spec_index(path)
    assert len(first.ops) == 6 and first.path == path.resolve()
    changed = tiny_data()
    del changed["ops"]["GET /v1/needs"]
    path.write_text(json.dumps(changed), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    second = load_spec_index(path)
    assert second is not first and len(second.ops) == 5


def test_bad_files_fail_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_spec_index(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_spec_index(broken)
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"ops": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="ops"):
        load_spec_index(empty)


# --------------------------------------------------------------------------
# normalize_query
# --------------------------------------------------------------------------


def test_normalize_query_returns_spec_casing_and_drops_none(tiny):
    op = tiny.op("GET /v1/things")
    result = normalize_query(op, {"statusids": "1,2", "PAGESIZE": 50, "cursor": None, "Cursor": None})
    assert result == {"StatusIds": "1,2", "PageSize": 50}
    assert list(result) == ["StatusIds", "PageSize"]  # input order is kept


def test_normalize_query_with_nothing_to_send(tiny):
    op = tiny.op("GET /v1/things")
    assert normalize_query(op, None) == {}
    assert normalize_query(op, {}) == {}
    assert normalize_query(op, {"StatusIds": None}) == {}


def test_normalize_query_checks_every_name_even_when_its_value_is_none(tiny):
    op = tiny.op("GET /v1/things")
    for given, field in (
        ({"StatusId": None}, "StatusId"),
        ({"Cursor": None, "Bogus": None}, "Bogus"),
        ({"statusids": "1", "Nope": None}, "Nope"),
        ({"PageSize": 5, "x": None}, "x"),
    ):
        with pytest.raises(SpecViolation) as info:
            normalize_query(op, given)
        assert info.value.field == field and info.value.op_key == "GET /v1/things"
    with pytest.raises(SpecViolation, match="no query parameters") as info:  # an unpaged op has no paging names
        normalize_query(tiny.op("GET /v1/things/{thingId}"), {"PageSize": None})
    assert info.value.field == "PageSize"


def test_a_none_value_still_counts_as_not_given_for_known_names(tiny):
    op = tiny.op("GET /v1/things")
    assert normalize_query(op, {"statusids": None, "StatusIds": "2"}) == {"StatusIds": "2"}  # not "given twice"
    with pytest.raises(SpecViolation, match="required query parameter 'Must'"):
        normalize_query(tiny.op("GET /v1/needs"), {"Must": None})


def test_unknown_query_name_raises_with_op_field_and_allowed_names(tiny):
    op = tiny.op("GET /v1/things")
    with pytest.raises(SpecViolation) as info:
        normalize_query(op, {"StatusId": "1"})
    err = info.value
    assert isinstance(err, ValueError)
    assert err.op_key == "GET /v1/things" and err.field == "StatusId"
    assert "did you mean 'StatusIds'" in str(err)
    for allowed in ("Cursor", "PageSize", "StatusIds"):
        assert allowed in str(err)


def test_paging_names_are_unknown_on_an_unpaged_op(tiny):
    op = tiny.op("GET /v1/things/{thingId}")
    with pytest.raises(SpecViolation, match="no query parameters") as info:
        normalize_query(op, {"PageSize": 10})
    assert info.value.field == "PageSize"


def test_lists_are_not_allowed_in_normalize_query(tiny):
    op = tiny.op("GET /v1/things")
    for value in ([1, 2], (1, 2), {1, 2}, {"a": 1}):
        with pytest.raises(SpecViolation, match="single value") as info:
            normalize_query(op, {"StatusIds": value})
        assert info.value.field == "StatusIds"


def test_the_same_name_in_two_casings_is_ambiguous(tiny):
    op = tiny.op("GET /v1/things")
    with pytest.raises(SpecViolation, match="given twice"):
        normalize_query(op, {"statusids": "1", "StatusIds": "2"})


def test_a_missing_required_query_param_is_reported(tiny):
    op = tiny.op("GET /v1/needs")
    assert normalize_query(op, {"must": "x"}) == {"Must": "x"}
    with pytest.raises(SpecViolation, match="required query parameter 'Must'"):
        normalize_query(op, {})


def test_every_real_op_accepts_its_own_names_in_any_casing(spec_index):
    for key, op in spec_index.ops.items():
        given = {name.lower(): "x" for name in op.query_params}
        assert list(normalize_query(op, given)) == list(op.query_params), key
        assert normalize_query(op, {name.upper(): "x" for name in op.query_params}).keys() == op.query_params.keys(), key


# --------------------------------------------------------------------------
# validate_body
# --------------------------------------------------------------------------


def test_a_valid_nested_body_passes(tiny):
    op = tiny.op("POST /v1/things")
    validate_body(op, {
        "Name": "n", "Location": {"Name": "HQ", "Phone": None}, "Parts": [{"Quantity": 1, "Sku": "a"}, {"Quantity": 2}],
        "Tags": [1, 2], "Level": 2, "Count": 3, "Inline": {"Depth": 1}, "Free": {"anything": {"goes": 1}},
    })
    validate_body(op, {})  # an empty object has no unknown keys


def test_none_means_no_body_and_always_passes(tiny):
    validate_body(tiny.op("POST /v1/things"), None)
    validate_body(tiny.op("GET /v1/things"), None)


def test_unknown_top_level_field(tiny):
    with pytest.raises(SpecViolation) as info:
        validate_body(tiny.op("POST /v1/things"), {"Name": "n", "Nme": "x"})
    err = info.value
    assert err.op_key == "POST /v1/things" and err.field == "Nme"
    assert "did you mean 'Name'" in str(err)
    assert "Allowed" in str(err) and "Location" in str(err) and "Parts" in str(err)


def test_field_names_are_case_sensitive(tiny):
    with pytest.raises(SpecViolation) as info:
        validate_body(tiny.op("POST /v1/things"), {"name": "n"})
    assert info.value.field == "name" and "did you mean 'Name'" in str(info.value)


def test_nested_unknown_field_has_a_dotted_path(tiny):
    with pytest.raises(SpecViolation) as info:
        validate_body(tiny.op("POST /v1/things"), {"Name": "n", "Location": {"Name": "HQ", "Phonee": "x"}})
    err = info.value
    assert err.field == "Location.Phonee"
    assert "did you mean 'Phone'" in str(err) and "in 'Location'" in str(err)


def test_inline_object_fields_are_checked_too(tiny):
    with pytest.raises(SpecViolation) as info:
        validate_body(tiny.op("POST /v1/things"), {"Inline": {"Deep": 1}})
    assert info.value.field == "Inline.Deep"


def test_list_elements_are_validated_one_by_one(tiny):
    with pytest.raises(SpecViolation) as info:
        validate_body(tiny.op("POST /v1/things"), {"Parts": [{"Quantity": 1}, {"Quantity": 1, "Quantityy": 2}]})
    assert info.value.field == "Parts[1].Quantityy"


def test_a_list_element_that_is_not_an_object_is_rejected(tiny):
    with pytest.raises(SpecViolation, match="must be an object") as info:
        validate_body(tiny.op("POST /v1/things"), {"Parts": [{"Quantity": 1}, 5]})
    assert info.value.field == "Parts[1]"


def test_lists_of_scalars_and_free_form_objects_are_not_inspected(tiny):
    validate_body(tiny.op("POST /v1/things"), {"Tags": [1, "two", None], "Free": {"X": [1, {"Y": 2}]}})


def test_structural_mistakes_are_caught(tiny):
    op = tiny.op("POST /v1/things")
    with pytest.raises(SpecViolation, match="does not take a list"):
        validate_body(op, {"Count": [1]})
    with pytest.raises(SpecViolation, match="does not take a list"):
        validate_body(op, {"Level": [1]})
    with pytest.raises(SpecViolation, match="does not take a list"):
        validate_body(op, {"Location": [{"Name": "x"}]})
    with pytest.raises(SpecViolation, match="not an object"):
        validate_body(op, {"Count": {"a": 1}})
    with pytest.raises(SpecViolation, match="not an object"):
        validate_body(op, {"Level": {"a": 1}})


@pytest.mark.parametrize("value", ["x", "a@b", 5, 1.5, True, b"x", {1}, {"Bogus": 1}], ids=repr)
def test_an_array_field_takes_a_list(tiny, value):
    op = tiny.op("POST /v1/things")
    for name in ("Parts", "Tags"):
        with pytest.raises(SpecViolation, match="takes a list") as info:
            validate_body(op, {name: value})
        assert info.value.field == name and info.value.op_key == "POST /v1/things"
        validate_body(op, {name: []})
        validate_body(op, {name: ()})
        validate_body(op, {name: None})


@pytest.mark.parametrize("value", ["HQ", 5, 1.5, True, b"x"], ids=repr)
def test_an_object_field_takes_an_object(tiny, value):
    op = tiny.op("POST /v1/things")
    for name in ("Location", "Inline", "Free"):  # a ref to an object schema, an inline object, a free-form object
        with pytest.raises(SpecViolation, match="takes an object") as info:
            validate_body(op, {name: value})
        assert info.value.field == name
    validate_body(op, {"Location": {}, "Inline": {}, "Free": {}})


def test_a_nested_value_of_the_wrong_shape_has_a_dotted_path(tiny):
    op = tiny.op("POST /v1/things")
    with pytest.raises(SpecViolation, match="takes an object") as info:
        validate_body(op, {"Parts": [{"Quantity": 1}, {"Quantity": 1}], "Inline": {"Depth": 1}, "Location": {"Name": "x", "Phone": "p"}, "Free": 3})
    assert info.value.field == "Free"
    with pytest.raises(SpecViolation, match="does not take a list") as info:
        validate_body(op, {"Inline": {"Depth": [1]}})
    assert info.value.field == "Inline.Depth"


def test_arrays_of_scalars_do_not_take_objects_or_lists_as_elements(tiny):
    op = tiny.op("POST /v1/things")
    with pytest.raises(SpecViolation, match="takes a single value, not an object") as info:
        validate_body(op, {"Tags": [1, {"secret": 1}]})
    assert info.value.field == "Tags[1]"
    with pytest.raises(SpecViolation, match="does not take a list") as info:
        validate_body(op, {"Tags": [[1]]})
    assert info.value.field == "Tags[0]"
    validate_body(op, {"Tags": [1, "two", None, 3.5, True]})  # the scalar type of an element is not inspected


def test_validate_body_returns_only_the_names_of_schema_fields(tiny):
    op = tiny.op("POST /v1/things")
    body = {
        "Name": "n",
        "Location": {"Name": "HQ", "Phone": None},
        "Parts": [{"Quantity": 1, "Sku": "a"}, {"Quantity": 2}],
        "Tags": [1, 2],
        "Free": {"secret key": {"deeper": [1, {"x": 2}]}},
        "Inline": {"Depth": 1},
    }
    names = validate_body(op, body)
    assert names == frozenset({
        "Name", "Location", "Location.Name", "Location.Phone", "Parts", "Parts.Quantity", "Parts.Sku",
        "Tags", "Free", "Inline", "Inline.Depth",
    })
    assert validate_body(op, {}) == frozenset() and validate_body(op, None) == frozenset()
    assert validate_body(tiny.op("GET /v1/things"), None) == frozenset()
    upload = tiny.op("POST /v1/things/{thingId}/upload")
    assert validate_body(upload, {"file": b"x", "itemType": "Ticket"}) == frozenset({"file", "itemType"})


def test_the_issue_examples_on_the_real_spec(spec_index):
    contacts = spec_index.op("PATCH /v1/contacts/{contactId}")  # the id is in the path, not in the body
    with pytest.raises(SpecViolation, match="takes a list") as info:
        validate_body(contacts, {"ClientId": 1, "SecondaryEmail": "a@b"})
    assert info.value.field == "SecondaryEmail"
    validate_body(contacts, {"ClientId": 1, "SecondaryEmail": ["a@b"]})
    clients = spec_index.op("POST /v1/clients")
    with pytest.raises(SpecViolation, match="takes an object") as info:
        validate_body(clients, {"Name": "x", "Location": "HQ"})
    assert info.value.field == "Location"
    ticket = spec_index.op("PATCH /v1/tickets/{ticketId}")
    with pytest.raises(SpecViolation, match="takes an object") as info:
        validate_body(ticket, {"BillingOverride": 5})
    assert info.value.field == "BillingOverride"
    items = spec_index.op("POST /v1/items")
    for bad in ("x", {"Bogus": 1}):
        with pytest.raises(SpecViolation, match="takes a list") as info:
            validate_body(items, {"Name": "n", "SubItems": bad})
        assert info.value.field == "SubItems"
    assert validate_body(items, {"Name": "n", "SubItems": [{"ItemId": uuid.uuid4().hex, "Quantity": 2}]}) == frozenset(
        {"Name", "SubItems", "SubItems.ItemId", "SubItems.Quantity"}
    )


def _real_shape(spec_index, entry):
    if entry.get("type") in ("array", "object"):
        return entry["type"]
    ref = entry.get("ref")
    if ref:
        schema = spec_index.schemas[ref]
        return "scalar" if "enum" in schema else schema["type"]
    return "scalar"


def test_every_real_body_field_takes_its_own_shape_and_refuses_the_others(spec_index):
    good = {"scalar": "x", "array": [], "object": {}}
    bad = {"scalar": [[1], {"a": 1}], "array": ["x", 5, {"a": 1}], "object": ["x", 5, [{"a": 1}]]}
    checked = 0
    for key, op in spec_index.ops.items():
        if op.body is None or op.is_multipart:
            continue
        for name, entry in op.body["fields"].items():
            shape = _real_shape(spec_index, entry)
            validate_body(op, {name: good[shape]})
            validate_body(op, {name: None})
            for value in bad[shape]:
                with pytest.raises(SpecViolation) as info:
                    validate_body(op, {name: value})
                assert info.value.field == name, (key, name, value)
            checked += 1
    assert checked > 150


def _populated(spec_index, entry, depth=0):
    """A well-shaped value for a body field entry, with every nested field filled in (depth limited)."""
    kind = entry.get("type")
    ref = entry.get("ref")
    schema = spec_index.schemas.get(ref) if ref else None
    if kind == "array":
        items = entry.get("items") or {}
        return [] if depth > 3 else [_populated(spec_index, items, depth + 1)]
    if kind == "object" or (schema is not None and "enum" not in schema and schema.get("type") == "object"):
        fields = entry.get("fields") or (schema or {}).get("fields") or {}
        if depth > 3:
            return {}
        return {name: _populated(spec_index, sub, depth + 1) for name, sub in fields.items()}
    if kind in ("integer", "number") or (schema is not None and "enum" in schema):
        return 1
    if kind == "boolean":
        return True
    return "x"


def test_every_real_body_accepts_a_fully_populated_well_shaped_body(spec_index):
    checked = 0
    for key, op in spec_index.ops.items():
        if op.body is None or op.is_multipart:
            continue
        body = {name: _populated(spec_index, entry) for name, entry in op.body["fields"].items()}
        names = validate_body(op, body)
        assert set(op.body["fields"]) <= names, key
        checked += 1
    assert checked == 30  # every JSON body of the live spec


def test_a_body_for_an_operation_without_one_is_a_violation(tiny):
    with pytest.raises(SpecViolation, match="takes no request body") as info:
        validate_body(tiny.op("GET /v1/things"), {"Anything": 1})
    assert info.value.op_key == "GET /v1/things"
    with pytest.raises(SpecViolation, match="takes no request body"):
        validate_body(tiny.op("GET /v1/things"), {})


def test_a_body_that_is_not_an_object_is_a_violation(tiny):
    for body in ([{"Name": "n"}], "text", 5):
        with pytest.raises(SpecViolation, match="must be a JSON object"):
            validate_body(tiny.op("POST /v1/things"), body)


def test_multipart_field_names_are_checked_against_the_multipart_fields(tiny):
    op = tiny.op("POST /v1/things/{thingId}/upload")
    validate_body(op, {"file": b"x", "itemType": "Ticket"})
    with pytest.raises(SpecViolation, match="multipart form field 'itemId'") as info:
        validate_body(op, {"file": b"x", "itemId": "123"})
    assert info.value.field == "itemId"
    with pytest.raises(SpecViolation):
        validate_body(op, {"File": b"x"})  # exact case


def test_the_real_attachment_upload_fields(spec_index):
    op = spec_index.op("POST /v1/attachments")
    validate_body(op, {"file": ("a.txt", b"x", "text/plain"), "itemId": "u", "itemType": "Ticket"})
    with pytest.raises(SpecViolation):
        validate_body(op, {"file": b"x", "ItemId": "u"})


def test_the_real_client_location_example(spec_index):
    op = spec_index.op("POST /v1/clients")
    validate_body(op, {"Name": "Acme", "Location": {"Name": "HQ", "Phone": "5555550142", "PhoneCountryCode": "US"}})
    with pytest.raises(SpecViolation) as info:
        validate_body(op, {"Name": "Acme", "Location": {"Name": "HQ", "Phonee": "x"}})
    assert info.value.field == "Location.Phonee"


def test_the_real_contact_body_rejects_the_pre_august_field_name(spec_index):
    op = spec_index.op("POST /v1/contacts")
    with pytest.raises(SpecViolation) as info:
        validate_body(op, {"ClientId": 1, "FirstName": "A", "LastName": "B", "PrimaryEmail": "a@b.test", "ClientLocationId": 5})
    assert info.value.field == "ClientLocationId" and "LocationId" in str(info.value)


def test_the_real_ticket_patch_billing_override_is_checked(spec_index):
    op = spec_index.op("PATCH /v1/tickets/{ticketId}")
    validate_body(op, {"BillingOverride": {"ServiceLineId": 1, "BillableStatusId": 2}, "PriorityId": 3})
    with pytest.raises(SpecViolation) as info:
        validate_body(op, {"BillingOverride": {"ContractServiceId": 1}})
    assert info.value.field == "BillingOverride.ContractServiceId"


def test_every_real_body_accepts_all_of_its_own_fields_as_null(spec_index):
    for key, op in spec_index.ops.items():
        if op.body is None:
            continue
        validate_body(op, {name: None for name in op.body["fields"]})


def test_every_real_nested_object_field_accepts_its_own_schema_fields(spec_index):
    checked = 0
    for key, op in spec_index.ops.items():
        if op.body is None or op.is_multipart:
            continue
        for name, entry in op.body["fields"].items():
            ref = entry.get("ref") or (entry.get("items") or {}).get("ref")
            schema = spec_index.schemas.get(ref) if ref else None
            if not schema or "enum" in schema or not schema["fields"]:
                continue
            nested = {field: None for field in schema["fields"]}
            validate_body(op, {name: [nested] if entry.get("type") == "array" else nested})
            with pytest.raises(SpecViolation):
                validate_body(op, {name: [{"NoSuchField": 1}] if entry.get("type") == "array" else {"NoSuchField": 1}})
            checked += 1
    assert checked >= 8


def test_violation_attributes_survive_str_and_inheritance():
    err = SpecViolation("GET /v1/x", "A.B", "boom")
    assert (err.op_key, err.field, str(err)) == ("GET /v1/x", "A.B", "boom")
    assert isinstance(err, ValueError)


# --------------------------------------------------------------------------
# validate_path_param
# --------------------------------------------------------------------------

CANONICAL = "3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b"


def test_a_uuid_placeholder_takes_a_uuid_and_returns_the_canonical_text(spec_index):
    op = spec_index.op("GET /v1/tickets/{ticketId}")
    for given in (CANONICAL, CANONICAL.upper(), CANONICAL.replace("-", ""), uuid.UUID(CANONICAL)):
        assert validate_path_param(op, "ticketId", given) == CANONICAL


@pytest.mark.parametrize(
    "bad",
    ["", " ", "t", "../x", CANONICAL + "/", " " + CANONICAL, "{" + CANONICAL + "}", "urn:uuid:" + CANONICAL, " " + "1" * 31, 7, None, True, [CANONICAL]],
    ids=repr,
)
def test_a_uuid_placeholder_refuses_everything_else(spec_index, bad):
    op = spec_index.op("GET /v1/tickets/{ticketId}")
    with pytest.raises(SpecViolation) as info:
        validate_path_param(op, "ticketId", bad)
    assert info.value.field == "ticketId" and info.value.op_key == op.key and "'ticketId'" in str(info.value)


def test_an_integer_placeholder(spec_index):
    op = spec_index.op("GET /v1/clients/{clientId}")
    assert [validate_path_param(op, "clientId", v) for v in (0, 7, "7", "007", 2**63 - 1)] == ["0", "7", "7", "7", str(2**63 - 1)]
    for bad in (-1, "-1", "7a", " 7", 7.0, "1.0", 2**63, "9" * 40, "\u0661", None, True, [7]):
        with pytest.raises(SpecViolation):
            validate_path_param(op, "clientId", bad)


def test_an_int32_integer_placeholder_has_a_smaller_range():
    data = tiny_data()
    data["ops"]["GET /v1/things/{thingId}"]["path_params"]["thingId"] = {"type": "integer", "format": "int32"}
    op = SpecIndex(data).op("GET /v1/things/{thingId}")
    assert validate_path_param(op, "thingId", 2**31 - 1) == str(2**31 - 1)
    with pytest.raises(SpecViolation, match="from 0 to 2147483647"):
        validate_path_param(op, "thingId", 2**31)


def test_an_untyped_placeholder_takes_one_plain_token(tiny, spec_index):
    op = tiny.op("GET /v1/things/{thingId}")
    for given in ("abc", "a.b", "caf\u00e9", "x:y@z", 12):
        assert validate_path_param(op, "thingId", given) == str(given)
    for bad in ("", "  ", "a/b", "a\\b", "a%2f", "a?b", "a#b", "a b", "a\tb", "a\x00b", "a\u200bb", ".", "..", "....", [1], {"a": 1}, 1.5, None, True):
        with pytest.raises(SpecViolation) as info:
            validate_path_param(op, "thingId", bad)
        assert info.value.field == "thingId"
    forms = spec_index.op("GET /v1/forms/{formId}/responses")  # this one also has a pattern
    assert validate_path_param(forms, "formId", "form_1-A") == "form_1-A"
    for bad in ("a.b", "x" * 51, "caf\u00e9"):
        with pytest.raises(SpecViolation, match="must match the pattern"):
            validate_path_param(forms, "formId", bad)


def test_a_pattern_python_cannot_read_does_not_block_a_safe_token():
    data = tiny_data()
    data["ops"]["GET /v1/things/{thingId}"]["path_params"]["thingId"] = {"type": "string", "pattern": "(unclosed"}
    op = SpecIndex(data).op("GET /v1/things/{thingId}")
    assert validate_path_param(op, "thingId", "abc") == "abc"
    with pytest.raises(SpecViolation):
        validate_path_param(op, "thingId", "a/b")


def test_an_unknown_placeholder_is_treated_as_an_untyped_token(tiny):
    op = tiny.op("GET /v1/things/{thingId}")
    assert validate_path_param(op, "other", "abc") == "abc"
    with pytest.raises(SpecViolation):
        validate_path_param(op, "other", "a/b")


def test_every_real_placeholder_accepts_a_valid_id_and_refuses_a_traversal(spec_index):
    from conftest import path_params_for

    checked = 0
    for key, op in spec_index.ops.items():
        for name, value in path_params_for(op).items():
            assert validate_path_param(op, name, value) == str(value), (key, name)
            for bad in ("..", "../x", "a/b", "a b"):
                with pytest.raises(SpecViolation):
                    validate_path_param(op, name, bad)
            checked += 1
    assert checked == sum(len(op.path_placeholders) for op in spec_index.ops.values()) and checked > 50
