"""tools/contacts.py: list_contacts, get_contact, create_contact, update_contact."""

import json
import re
import typing
from pathlib import Path

import httpx
import pytest
from conftest import (
    TEST_TRACE_ID,
    call_tool,
    call_tool_error,
    envelope,
    error_envelope,
    in_order,
    list_tools,
    make_ctx,
    paged_envelope,
    paged_responder,
    pagination,
)
from fastmcp.exceptions import ToolError

import tools.contacts as contacts_module
from gorelo_client import FORBIDDEN_OPS
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio

LATEST_SPEC = Path(__file__).resolve().parent.parent / "backups" / "swagger-latest.json"
CONTACT_SCALE = "1 Active, 3 Temporary, 4 Archived"

CONTACTS = "/v1/contacts"
CONTACT_5 = "/v1/contacts/5"  # update_contact: the id is in the path only (the command has no ContactId)

CREATE_PARAMS = [
    "client_id", "first_name", "last_name", "primary_email", "location_id", "secondary_email", "mobile_phone",
    "mobile_phone_country_code", "office_phone", "office_phone_country_code", "job_title", "department",
    "time_zone", "description", "tag_ids",
]
UPDATE_PARAMS = [
    "contact_id", "first_name", "last_name", "primary_email", "location_id", "mobile_phone",
    "mobile_phone_country_code", "office_phone", "office_phone_country_code", "job_title", "department",
    "time_zone", "description", "clear_fields", "secondary_email", "tag_ids",
]

TOOLS = {
    # name: (kind, ops, destructive_hint, parameters in order, required parameters)
    "list_contacts": (
        "read", ["GET /v1/contacts"], False,
        [
            "client_id", "client_ids", "location_ids", "status_ids", "query", "created_since", "created_before",
            "updated_since", "updated_before", "page_size", "cursor",
        ],
        [],
    ),
    "list_contact_tags": ("read", ["GET /v1/contacts/tags"], False, [], []),
    "get_contact": ("read", ["GET /v1/contacts/{contactId}"], False, ["contact_id"], ["contact_id"]),
    "create_contact": (
        "write", ["POST /v1/contacts"], False, CREATE_PARAMS, ["client_id", "first_name", "last_name", "primary_email"]
    ),
    "update_contact": (
        "write", ["GET /v1/contacts/{contactId}", "PATCH /v1/contacts/{contactId}"], True, UPDATE_PARAMS, ["contact_id"]
    ),
}

# The UpdateContactCommand fields (update_contact copies every one of them from the record, except TagIds, which it sends only
# when the caller gives tag_ids: a stored tag that went inactive would make the request a 400).
COMMAND_KEYS = {
    "FirstName", "LastName", "ClientId", "LocationId", "PrimaryEmail", "MobilePhone",
    "MobilePhoneCountryCode", "OfficePhone", "OfficePhoneCountryCode", "JobTitle", "Department", "TimeZone",
    "Description", "TagIds",
}
READ_ONLY_KEYS = {"Id", "Alias", "Status", "CreatedOn", "UpdatedOn", "IsTemporaryContact", "MergedWithContactId"}
# The command fields Gorelo requires: always sent, always with a value.
REQUIRED_KEYS = {"FirstName", "LastName", "ClientId", "PrimaryEmail"}
# The optional command fields a contact may not have: left out of the body when it has no value.
OPTIONAL_KEYS = COMMAND_KEYS - REQUIRED_KEYS - {"TagIds"}

# The `note` of an update that leaves a stored region code out because the contact has no such phone, pinned word for word
# (tools/contacts.py _cleared_note): the region parameter with the code it held, and why.
MOBILE_NOTE = (
    "mobile_phone_country_code (was US) was cleared because the contact has no mobile phone, and Gorelo refuses a region "
    "code without its phone number."
)
OFFICE_NOTE = (
    "office_phone_country_code (was US) was cleared because the contact has no office phone, and Gorelo refuses a region "
    "code without its phone number."
)
BOTH_NOTE = (
    "mobile_phone_country_code (was US) and office_phone_country_code (was CA) were cleared because the contact has no "
    "mobile phone and no office phone, and Gorelo refuses a region code without its phone number."
)
# The two phone pairs: phone parameter, region parameter, their command fields and the note of the pair's stored region code.
PHONE_PAIR_CASES = [
    pytest.param("mobile_phone", "mobile_phone_country_code", "MobilePhone", "MobilePhoneCountryCode", MOBILE_NOTE, id="mobile"),
    pytest.param("office_phone", "office_phone_country_code", "OfficePhone", "OfficePhoneCountryCode", OFFICE_NOTE, id="office"),
]


@pytest.fixture
def server(server_factory):
    return server_factory(toolsets={"core"})


def contact_record(contact_id=5, **extra):
    """A contact as GET /v1/contacts/{contactId} returns it: command fields plus response-only fields."""
    record = {
        "Id": contact_id,
        "FirstName": "Jane",
        "LastName": "Doe",
        "ClientId": 9101,
        "LocationId": 9001,
        "PrimaryEmail": "jane.doe@example.invalid",
        "MobilePhone": "5555550142",
        "MobilePhoneCountryCode": "US",
        "OfficePhone": "5555550199",
        "OfficePhoneCountryCode": "US",
        "JobTitle": "Engineer",
        "Department": "IT",
        "TimeZone": "UTC",
        "Description": "Notes",
        "TagIds": [3, 7],
        "Alias": "jdoe",
        "Status": {"Id": 1, "Name": "Active"},
        "IsTemporaryContact": False,
        "MergedWithContactId": None,
        "CreatedOn": "2026-01-01T00:00:00Z",
        "UpdatedOn": None,
    }
    record.update(extra)
    return record


def command(**overrides):
    """The UpdateContactCommand update_contact should send for contact_record(), with overrides. It has no contact
    id: the id is in the path.

    An override of None means "the contact has no such value after the update": the field is LEFT OUT of the body
    (Gorelo clears a phone, job title, department, time zone or description that a PATCH leaves out, and an explicit null is never sent).
    """
    body = {
        "FirstName": "Jane",
        "LastName": "Doe",
        "ClientId": 9101,
        "LocationId": 9001,
        "PrimaryEmail": "jane.doe@example.invalid",
        "MobilePhone": "5555550142",
        "MobilePhoneCountryCode": "US",
        "OfficePhone": "5555550199",
        "OfficePhoneCountryCode": "US",
        "JobTitle": "Engineer",
        "Department": "IT",
        "TimeZone": "UTC",
        "Description": "Notes",
    }
    body.update(overrides)
    return {key: value for key, value in body.items() if value is not None}


def trace(text):
    return f"{text} [trace {TEST_TRACE_ID}]"


def flat(text):
    return " ".join(text.split())


def only(record, *drop):
    return {key: value for key, value in record.items() if key not in drop}


def routed_update(mock, record=None, answer=None):
    """GET /v1/contacts/5 returns `record`, PATCH /v1/contacts/5 (the id is in the path) returns `answer` (default: the record)."""
    record = contact_record() if record is None else record
    mock.on("GET", CONTACT_5, envelope(record))
    mock.on("PATCH", CONTACT_5, envelope(record if answer is None else answer))


def assert_no_nulls(body):
    """ the PATCH body never carries an explicit null, and the four required fields always have a value."""
    assert [key for key, value in body.items() if value is None] == [], f"explicit null sent: {body}"
    assert REQUIRED_KEYS <= set(body)


def patched(mock):
    """The PATCH requests, each checked for explicit nulls."""
    requests = [r for r in mock.requests if r.method == "PATCH"]
    for request in requests:
        assert_no_nulls(request.json)
    return requests


# --------------------------------------------------------------------------
# Declarations
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_each_tool_is_declared_as_documented(name, spec_index):
    kind, ops, destructive, _params, _required = TOOLS[name]
    spec = next(s for s in REGISTRY.specs if s.name == name)
    assert (spec.toolset, spec.kind, spec.ops, spec.destructive_hint) == ("core", kind, ops, destructive)
    assert all(op in spec_index.ops for op in spec.ops)
    assert not set(spec.ops) & FORBIDDEN_OPS


@pytest.mark.parametrize("name", sorted(TOOLS))
async def test_each_tool_exposes_exactly_the_documented_parameters(name, server):
    _kind, _ops, _destructive, params, required = TOOLS[name]
    tool = next(t for t in await list_tools(server) if t.name == name)
    assert list(tool.inputSchema["properties"]) == params
    assert tool.inputSchema.get("required", []) == required
    assert all(p.get("description") for p in tool.inputSchema["properties"].values())


@pytest.mark.parametrize("name", sorted(TOOLS))
async def test_annotations_follow_the_kind_and_the_overwrite_rule(name, server):
    kind, _ops, destructive, _params, _required = TOOLS[name]
    tool = next(t for t in await list_tools(server) if t.name == name)
    assert tool.annotations.readOnlyHint is (kind == "read")
    assert tool.annotations.destructiveHint is destructive


async def test_the_defaults_the_model_sees_are_none_meaning_not_given(server):
    # "Not given" is how the compacted schema (server.compact_input_schema, see test_schema_compaction.py) shows an
    # optional parameter: it is not in "required" and it advertises no default at all, not even "default": null.
    listed = {t.name: t for t in await list_tools(server)}
    tools = {name: tool.inputSchema["properties"] for name, tool in listed.items()}
    required = {name: set(tool.inputSchema.get("required", [])) for name, tool in listed.items()}
    assert tools["list_contacts"]["page_size"]["default"] == 200
    for name in ("client_id", "client_ids", "status_ids", "query", "cursor"):
        assert "default" not in tools["list_contacts"][name] and name not in required["list_contacts"], name
    for name in UPDATE_PARAMS[1:]:
        assert "default" not in tools["update_contact"][name] and name not in required["update_contact"], name
    for name in CREATE_PARAMS[4:]:
        assert "default" not in tools["create_contact"][name] and name not in required["create_contact"], name


def has_body_field(op, path):
    return path in op.body["fields"]


@pytest.mark.parametrize("name", ["create_contact", "update_contact"])
def test_every_field_map_path_is_a_body_field_of_the_op(name, spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == name)
    op = spec_index.op(spec.ops[-1])
    for param, path in spec.field_map.items():
        if param == "contact_id":  # update_contact: the path placeholder, kept for error naming; never a body field
            assert name == "update_contact" and path in op.path_placeholders
            assert not has_body_field(op, path) and "ContactId" not in op.body["fields"]
            continue
        assert has_body_field(op, path), f"{name}: {param} -> {path} is not in {op.key}"


def test_the_field_maps_cover_the_command_fields_the_tools_can_set(spec_index):
    create = next(s for s in REGISTRY.specs if s.name == "create_contact").field_map
    update = next(s for s in REGISTRY.specs if s.name == "update_contact").field_map
    assert set(create.values()) == set(spec_index.op("POST /v1/contacts").body["fields"])
    # everything but ClientId, which update_contact copies from the record and never takes as a parameter, plus the
    # path placeholder contact_id (the command has no ContactId, so it is not a body field)
    body_params = {param: path for param, path in update.items() if param != "contact_id"}
    assert update["contact_id"] == "contactId"
    assert set(body_params.values()) == set(spec_index.op("PATCH /v1/contacts/{contactId}").body["fields"]) - {"ClientId"}
    assert "client_id" not in update


def test_the_field_map_of_get_contact_names_the_path_placeholder(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "get_contact")
    assert spec.field_map == {"contact_id": "contactId"}
    assert set(spec.field_map.values()) == set(spec_index.op("GET /v1/contacts/{contactId}").path_placeholders)


def test_list_contacts_field_map_uses_client_ids_and_never_the_legacy_client_id(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "list_contacts")
    op = spec_index.op("GET /v1/contacts")
    assert spec.field_map["client_id"] == "ClientIds" and spec.field_map["client_ids"] == "ClientIds"
    assert {path.lower() for path in spec.field_map.values()} == {q.lower() for q in op.query_params} - {"clientid"}


def test_update_contact_command_is_exactly_the_spec_command_without_what_the_api_never_returns(spec_index):
    op = spec_index.op("PATCH /v1/contacts/{contactId}")
    assert COMMAND_KEYS | {"SecondaryEmail"} == set(op.body["fields"])
    assert set(op.body["required"]) <= COMMAND_KEYS
    assert set(contacts_module.COPIED_FIELDS) | {"TagIds"} == COMMAND_KEYS  # TagIds is sent only with tag_ids
    assert "ContactId" not in op.body["fields"]  # the contact id is in the path (contract e15cb5a18ec2)


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_docstrings_follow_the_template(name):
    spec = next(s for s in REGISTRY.specs if s.name == name)
    doc = spec.fn.__doc__
    if name == "list_contact_tags":  # the 1.5.0 read states its ids and that it changes nothing
        assert "Resolve ids first:" in doc and "Side effects: None (read-only)." in doc
    else:
        assert (TOOLS[name][0] == "write") == ("Side effects:" in doc)  # only writes have side effects to state
    assert ("Paging:" in doc) == (name == "list_contacts")


@pytest.mark.parametrize(
    "name, param, resolver",
    [
        ("list_contacts", "client_id", "list_clients"),
        ("list_contacts", "client_ids", "list_clients"),
        ("get_contact", "contact_id", "list_contacts"),
        ("create_contact", "client_id", "list_clients"),
        ("create_contact", "location_id", "list_client_locations"),
        ("update_contact", "contact_id", "list_contacts"),
        ("update_contact", "location_id", "list_client_locations"),
    ],
)
async def test_each_id_description_names_the_tool_that_resolves_it(server, name, param, resolver):
    tool = next(t for t in await list_tools(server) if t.name == name)
    assert resolver in tool.inputSchema["properties"][param]["description"]


# The sentence of update_contact's description about the secondary emails: the API cannot show them, the user sees them in the
# Gorelo app, so the model asks the user for the complete list; the list replaces all of them and [] erases them.
SECONDARY_SENTENCE = (
    "The API cannot show the secondary emails, the user sees them in the Gorelo app: to change them ask the user for the "
    "complete list; secondary_email REPLACES all ([] erases)."
)


def test_the_update_docstring_says_what_gorelo_clears_what_it_keeps_and_what_secondary_email_replaces():
    doc = flat(contacts_module.update_contact.__doc__)
    # live, 2026-10-24: a PATCH clears the phones, job title, department, time zone and description it leaves out, so the
    # tool reads the contact first and re-sends them; a LocationId and the secondary emails that are left out are kept
    assert "Gorelo clears the phones, job title, department, time zone and description an update leaves out" in doc
    assert "so this tool reads the contact and re-sends them." in doc
    assert "Location and secondary emails you do not send are kept" in doc
    # the API cannot show the secondary emails but the user sees them in the Gorelo app, so the model asks the user for the
    # complete list; a list that is passed replaces them all
    assert SECONDARY_SENTENCE in doc
    assert "The location can be moved, not cleared." in doc
    assert "Side effects: overwrites the contact" in doc and "an app edit made between the read and the write is lost" in doc
    # nothing is left of the false claim that nobody can read the list, of the old flag or of the old refusal
    for gone in ("nobody can read", "clear_secondary_email_ok", "refused before anything is sent", "replaces the whole contact"):
        assert gone not in doc, gone


async def test_the_update_parameters_explain_clear_fields_and_secondary_email(server):
    tool = next(t for t in await list_tools(server) if t.name == "update_contact")
    props = tool.inputSchema["properties"]
    # the names clear_fields accepts are in the schema (a Literal list), not repeated in the text
    assert props["clear_fields"]["type"] == "array"
    assert props["clear_fields"]["items"] == {"type": "string", "enum": list(contacts_module.CLEARABLE_FIELDS)}
    assert "location_id" not in props["clear_fields"]["items"]["enum"]  # Gorelo cannot clear a contact's location
    assert props["clear_fields"]["description"] == (
        "Fields to clear: the contact ends up without them. Not with a value for the same field."
    )
    assert [f for f in contacts_module.CLEARABLE_FIELDS if f.endswith("_country_code")] == [
        "mobile_phone_country_code", "office_phone_country_code",
    ]
    secondary = props["secondary_email"]["description"]
    assert secondary == (
        "COMPLETE list; replaces ALL secondary emails ([] erases them). The API cannot show them but the user sees them in "
        "the Gorelo app: to change them, ask the user for the complete list. Omit it to keep them."
    )
    assert len(secondary) <= 220  # MAX_PARAM_DESCRIPTION_CHARS of tests/test_integration.py
    assert "nobody can read" not in secondary and "Gorelo app" in secondary and "ask the user" in secondary
    assert "clear_secondary_email_ok" not in props  # the flag is gone: omitting secondary_email loses nothing


def test_the_create_docstring_says_nothing_is_inferred():
    doc = flat(contacts_module.create_contact.__doc__)
    assert "nothing else is inferred or defaulted" in doc and "creates a real contact" in doc
    assert "cannot delete contacts" in doc


async def test_the_contact_status_texts_are_the_new_scale_in_every_contacts_tool(server):
    listed = {tool.name: tool for tool in await list_tools(server) if tool.name in TOOLS}
    assert set(listed) == set(TOOLS)
    status = listed["list_contacts"].inputSchema["properties"]["status_ids"]["description"]
    assert status == (
        "1 Active, 3 Temporary, 4 Archived, up to 100. Omit it and Archived contacts are left out; give it and only those "
        "are returned."
    )
    assert len(status) <= 160
    listing = flat(listed["list_contacts"].description)
    assert "Archived contacts are left out unless status_ids asks for status 4; deleted contacts are never listed." in listing
    assert len(listing) <= 700
    for tool in listed.values():  # no word of the old scale in any text the model reads
        for text in [tool.description, *[p["description"] for p in tool.inputSchema["properties"].values()]]:
            assert "Inactive" not in text and "Archive." not in text, tool.name


@pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")
def test_the_contact_status_scale_is_the_one_in_the_spec_text():
    # the StatusIds text of the full OpenAPI snapshot (contract a7954b24844b), read as the spec states it
    spec = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))
    parameters = spec["paths"][CONTACTS]["get"]["parameters"]
    text = " ".join(next(p for p in parameters if p["name"] == "StatusIds")["description"].split())
    assert text == (
        "Comma-separated contact status ids to include: 1 Active, 3 Temporary, 4 Archived. Default excludes Archived; "
        "when supplied, only the listed statuses are returned. Any other value is a 400. Up to 100 values. Deleted contacts "
        "are never listed."
    )
    listed = text.split("include:")[1].split("Default")[0]
    scale = {int(number): label for number, label in re.findall(r"(\d+) ([A-Za-z ]+?)(?:,|\.)", listed)}
    assert scale == contacts_module.CONTACT_STATUS_LABELS and contacts_module.STATUS_IDS_MAX == 100


def test_the_module_text_names_the_contact_status_scale():
    doc = flat(contacts_module.__doc__)
    assert "1 Active, 3 Temporary, 4 Archived" in doc and "2 Inactive is gone" in doc


# --------------------------------------------------------------------------
# list_contacts
# --------------------------------------------------------------------------


async def test_list_contacts_with_no_filters_sends_only_the_page_size(server, mock_gorelo):
    rows = [contact_record(5), contact_record(6, FirstName="Joe")]
    mock_gorelo.on("GET", CONTACTS, paged_envelope(rows, total_count=1234))
    result = await call_tool(server, "list_contacts")
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", CONTACTS, {"PageSize": "200"}, b"")
    assert result == {
        "items": rows, "count": 2, "total_count": 1234, "has_more": False, "next_cursor": None,
        "page_size": 200, "filters": {},
    }


async def test_a_single_client_is_sent_as_client_ids_never_the_legacy_client_id(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACTS, paged_envelope([contact_record()], total_count=1))
    result = await call_tool(server, "list_contacts", {"client_id": 9101})
    assert mock_gorelo.last.query == {"ClientIds": "9101", "PageSize": "200"}
    assert "ClientId" not in mock_gorelo.last.query
    assert result["filters"] == {"client_id": 9101}


async def test_several_clients_are_sent_as_one_comma_separated_client_ids(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACTS, paged_envelope([contact_record()], total_count=1))
    result = await call_tool(server, "list_contacts", {"client_ids": [9101, 9102, 12]})
    assert mock_gorelo.last.query == {"ClientIds": "9101,9102,12", "PageSize": "200"}
    assert result["filters"] == {"client_ids": [9101, 9102, 12]}


async def test_client_id_and_client_ids_together_are_a_local_error(server, mock_gorelo):
    text = await call_tool_error(server, "list_contacts", {"client_id": 9101, "client_ids": [9102]})
    assert text == "client_id or client_ids: give only one of them (client_id for a single client, client_ids for several)"
    assert mock_gorelo.requests == []


async def test_an_empty_client_ids_list_is_not_the_same_as_no_filter(server, mock_gorelo):
    text = await call_tool_error(server, "list_contacts", {"client_ids": []})
    assert "client_ids: expected at least one id, got an empty list" in text
    assert mock_gorelo.requests == []
    both = await call_tool_error(server, "list_contacts", {"client_id": 9101, "client_ids": []})
    assert both.startswith("client_id or client_ids: give only one of them")


async def test_list_contacts_sends_every_filter_under_its_spec_name(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACTS, paged_envelope([contact_record()], next_cursor="c2", total_count=500))
    result = await call_tool(
        server,
        "list_contacts",
        {
            "client_ids": [9101],
            "status_ids": [1, 3],
            "query": "jane@",
            "created_since": "2026-01-01T00:00:00Z",
            "created_before": "2026-02-01T09:00:00-05:00",
            "updated_since": "2026-03-01T00:00:00+00:00",
            "updated_before": "2026-04-01T12:30:15.250000Z",
            "page_size": 25,
            "cursor": "c1",
        },
    )
    assert mock_gorelo.last.query == {
        "ClientIds": "9101",
        "StatusIds": "1,3",
        "Query": "jane@",
        "CreatedSince": "2026-01-01T00:00:00Z",
        "CreatedBefore": "2026-02-01T14:00:00Z",
        "UpdatedSince": "2026-03-01T00:00:00Z",
        "UpdatedBefore": "2026-04-01T12:30:15.250000Z",
        "PageSize": "25",
        "Cursor": "c1",
    }
    assert result["has_more"] is True and result["next_cursor"] == "c2" and result["page_size"] == 25
    assert result["filters"] == {
        "client_ids": [9101],
        "status_ids": [1, 3],
        "query": "jane@",
        "created_since": "2026-01-01T00:00:00Z",
        "created_before": "2026-02-01T14:00:00Z",
        "updated_since": "2026-03-01T00:00:00Z",
        "updated_before": "2026-04-01T12:30:15.250000Z",
    }


async def test_list_contacts_follows_the_cursor_with_the_same_filters(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACTS, paged_responder([[contact_record(5)], [contact_record(6)]], total_count=2))
    first = await call_tool(server, "list_contacts", {"client_id": 9101, "page_size": 1})
    second = await call_tool(server, "list_contacts", {"client_id": 9101, "page_size": 1, "cursor": first["next_cursor"]})
    assert first["has_more"] is True and second["has_more"] is False and second["items"] == [contact_record(6)]
    assert [r.query for r in mock_gorelo.requests] == [
        {"ClientIds": "9101", "PageSize": "1"},
        {"ClientIds": "9101", "PageSize": "1", "Cursor": "c1"},
    ]


@pytest.mark.parametrize("asked, sent", [(500, 200), (0, 1), (-9, 1), (50, 50)])
async def test_list_contacts_clamps_page_size_and_reports_the_size_used(server, mock_gorelo, asked, sent):
    mock_gorelo.on("GET", CONTACTS, paged_envelope([contact_record()]))
    result = await call_tool(server, "list_contacts", {"page_size": asked})
    assert mock_gorelo.last.query["PageSize"] == str(sent) and result["page_size"] == sent


async def test_list_contacts_reports_an_empty_page_as_an_empty_page(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACTS, envelope([], pagination(None, 0, has_more=False)))
    result = await call_tool(server, "list_contacts", {"client_id": 1})
    assert result["items"] == [] and result["count"] == 0 and result["total_count"] == 0


@pytest.mark.parametrize("statuses, sent", [([1], "1"), ([3], "3"), ([4], "4"), ([1, 3, 4], "1,3,4"), ([4, 4], "4,4")])
async def test_list_contacts_accepts_each_status_of_the_scale_and_sends_it_comma_joined(server, mock_gorelo, statuses, sent):
    mock_gorelo.on("GET", CONTACTS, paged_envelope([contact_record()]))
    result = await call_tool(server, "list_contacts", {"status_ids": statuses})
    assert mock_gorelo.last.query == {"StatusIds": sent, "PageSize": "200"}
    assert result["filters"] == {"status_ids": statuses}


@pytest.mark.parametrize(
    "statuses, fragment",
    [
        ([2], f"status_ids[0]: expected {CONTACT_SCALE}, got 2"),
        ([1, 5], f"status_ids[1]: expected {CONTACT_SCALE}, got 5"),
        ([0], f"status_ids[0]: expected {CONTACT_SCALE}, got 0"),
        ([-4], f"status_ids[0]: expected {CONTACT_SCALE}, got -4"),
        ([2**63], "status_ids[0]: expected a positive whole number such as 123, got a number above "),
        ([3] * 101, "status_ids: at most 100 values, got 101"),
    ],
)
async def test_list_contacts_refuses_a_status_off_the_scale_or_too_many_before_any_http(server, mock_gorelo, statuses, fragment):
    assert fragment in await call_tool_error(server, "list_contacts", {"status_ids": statuses})
    assert mock_gorelo.requests == []


async def test_list_contacts_accepts_exactly_100_status_values(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACTS, paged_envelope([contact_record()]))
    await call_tool(server, "list_contacts", {"status_ids": [1] * 100})
    assert mock_gorelo.last.query["StatusIds"] == ",".join(["1"] * 100)


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"client_id": 0}, "client_id: expected a positive whole number"),
        ({"client_id": -4}, "client_id: expected a positive whole number"),
        ({"client_ids": [9101, 0]}, "client_ids[1]: expected a positive whole number"),
        ({"client_ids": [-1]}, "client_ids[0]: expected a positive whole number"),
        ({"status_ids": []}, "status_ids: expected at least one id, got an empty list"),
        ({"status_ids": [1, 0]}, f"status_ids[1]: expected {CONTACT_SCALE}, got 0"),
        ({"query": ""}, "query: must not be empty or whitespace only"),
        ({"created_since": "2026-01-01T00:00:00"}, "created_since: '2026-01-01T00:00:00' has no UTC offset"),
        ({"updated_before": "last week"}, "updated_before: 'last week' is not an ISO 8601 datetime"),
        ({"cursor": ""}, "cursor: must not be empty or whitespace only"),
    ],
)
async def test_list_contacts_local_errors_name_the_parameter_and_send_nothing(server, mock_gorelo, arguments, fragment):
    assert fragment in await call_tool_error(server, "list_contacts", arguments)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "property_name, param",
    [
        ("ClientIds", "client_id or client_ids"),
        ("StatusIds", "status_ids"),
        ("PageSize", "page_size"),
        ("UpdatedBefore", "updated_before"),
        ("Query", "query"),
    ],
)
async def test_list_contacts_maps_a_gorelo_error_to_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", CONTACTS, error_envelope(400, [("070101", "Rejected value.", property_name)]))
    text = await call_tool_error(server, "list_contacts", {})
    assert text == trace(f"Gorelo rejected list_contacts (HTTP 400, code 070101): {param}: Rejected value.")


async def test_list_contacts_refuses_the_legacy_lowercase_body_instead_of_returning_zero_rows(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACTS, {"data": [{"id": 5}], "nextCursor": None, "hasMore": False})
    text = await call_tool_error(server, "list_contacts", {})
    assert text.startswith("Gorelo returned an unexpected response for list_contacts")


# --------------------------------------------------------------------------
# get_contact
# --------------------------------------------------------------------------


async def test_get_contact_returns_the_record_unchanged(server, mock_gorelo):
    record = contact_record()
    mock_gorelo.on("GET", CONTACT_5, envelope(record))
    result = await call_tool(server, "get_contact", {"contact_id": 5})
    assert result == record and "SecondaryEmail" not in result
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", CONTACT_5, {}, b"")
    assert len(mock_gorelo.requests) == 1


async def test_get_contact_maps_a_missing_contact_to_a_tool_error(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contacts/99999", error_envelope(404, [("070404", "Contact not found.")]))
    text = await call_tool_error(server, "get_contact", {"contact_id": 99999})
    assert text == trace("Gorelo rejected get_contact (HTTP 404, code 070404): Contact not found.")


@pytest.mark.parametrize("property_name", ["contactId", "ContactId"])
async def test_get_contact_maps_a_gorelo_error_about_the_id_to_contact_id(server, mock_gorelo, property_name):
    mock_gorelo.on("GET", CONTACT_5, error_envelope(400, [("070101", "The id is not valid.", property_name)]))
    text = await call_tool_error(server, "get_contact", {"contact_id": 5})
    assert text == trace("Gorelo rejected get_contact (HTTP 400, code 070101): contact_id: The id is not valid.")


@pytest.mark.parametrize("bad", [0, -2])
async def test_get_contact_rejects_an_id_that_cannot_exist_naming_contact_id(server, mock_gorelo, bad):
    text = await call_tool_error(server, "get_contact", {"contact_id": bad})
    assert text.startswith("contact_id: expected a positive whole number")
    assert mock_gorelo.requests == []


async def test_get_contact_refuses_a_success_without_data(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACT_5, envelope(None))
    assert "Data is null" in await call_tool_error(server, "get_contact", {"contact_id": 5})


@pytest.mark.parametrize("data, found", [({}, "an empty object"), ([contact_record()], "a list of 1 item"), (False, "a boolean")])
async def test_get_contact_refuses_a_record_that_is_not_an_object(server, mock_gorelo, data, found):
    mock_gorelo.on("GET", CONTACT_5, envelope(data))
    text = await call_tool_error(server, "get_contact", {"contact_id": 5})
    assert text.startswith("Gorelo returned an unexpected response for get_contact: GET /v1/contacts/{contactId}: expected Data to be a non-empty object")
    assert f"but got {found}" in text and "refusing to guess" in text


# --------------------------------------------------------------------------
# create_contact
# --------------------------------------------------------------------------

REQUIRED_FOUR = {"client_id": 9101, "first_name": "Jane", "last_name": "Doe", "primary_email": "jane@example.invalid"}


async def test_create_contact_sends_exactly_the_four_required_fields_and_nothing_else(server, mock_gorelo):
    created = contact_record(5, LocationId=None, MobilePhone=None, JobTitle=None, TimeZone=None)
    mock_gorelo.on("POST", CONTACTS, envelope(created))
    result = await call_tool(server, "create_contact", REQUIRED_FOUR)
    assert result == created
    request = mock_gorelo.last
    assert (request.method, request.path, request.query) == ("POST", CONTACTS, {})
    assert request.json == {
        "ClientId": 9101, "FirstName": "Jane", "LastName": "Doe", "PrimaryEmail": "jane@example.invalid"
    }


async def test_create_contact_infers_nothing_and_reads_nothing_first(server, mock_gorelo):
    # The legacy tool fetched the client and its locations and filled in a time zone, regions and a
    # location. Gorelo accepts the four fields, so exactly one request is made.
    mock_gorelo.on("POST", CONTACTS, envelope(contact_record()))
    await call_tool(server, "create_contact", REQUIRED_FOUR)
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("POST", CONTACTS)]
    body = mock_gorelo.last.json
    for invented in ("LocationId", "TimeZone", "MobilePhoneCountryCode", "OfficePhoneCountryCode", "JobTitle", "Department", "Description"):
        assert invented not in body


async def test_create_contact_sends_every_optional_field_under_its_pascal_name(server, mock_gorelo):
    mock_gorelo.on("POST", CONTACTS, envelope(contact_record()))
    await call_tool(
        server,
        "create_contact",
        {
            **REQUIRED_FOUR,
            "location_id": 9001,
            "secondary_email": ["a@example.invalid", "b@example.invalid"],
            "mobile_phone": "5555550142",
            "mobile_phone_country_code": "US",
            "office_phone": "5555550100",
            "office_phone_country_code": "CA",
            "job_title": "Engineer",
            "department": "IT",
            "time_zone": "UTC",
            "description": "Created by test",
        },
    )
    assert mock_gorelo.last.json == {
        "ClientId": 9101,
        "FirstName": "Jane",
        "LastName": "Doe",
        "PrimaryEmail": "jane@example.invalid",
        "LocationId": 9001,
        "SecondaryEmail": ["a@example.invalid", "b@example.invalid"],
        "MobilePhone": "5555550142",
        "MobilePhoneCountryCode": "US",
        "OfficePhone": "5555550100",
        "OfficePhoneCountryCode": "CA",
        "JobTitle": "Engineer",
        "Department": "IT",
        "TimeZone": "UTC",
        "Description": "Created by test",
    }


@pytest.mark.parametrize("phone, region, phone_key, region_key, note", PHONE_PAIR_CASES)
async def test_a_region_without_its_phone_is_a_local_error_naming_the_region_and_nothing_is_sent(
    server, mock_gorelo, phone, region, phone_key, region_key, note
):
    # Gorelo answers HTTP 400, code 070101, "OfficePhone is required when OfficePhoneCountryCode is provided" (live, on a test
    # client, 2026-10-24), so the region code is refused here, before any request, for each pair
    text = await call_tool_error(server, "create_contact", {**REQUIRED_FOUR, region: "US"})
    assert text == (
        f"{region}: Gorelo refuses a region code without its phone number; pass {phone} too, or leave {region} out"
    )
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("phone, region, phone_key, region_key, note", PHONE_PAIR_CASES)
async def test_a_region_next_to_its_phone_is_sent_with_it(server, mock_gorelo, phone, region, phone_key, region_key, note):
    mock_gorelo.on("POST", CONTACTS, envelope(contact_record()))
    await call_tool(server, "create_contact", {**REQUIRED_FOUR, phone: "5555550142", region: "US"})
    assert mock_gorelo.last.json == {
        "ClientId": 9101, "FirstName": "Jane", "LastName": "Doe", "PrimaryEmail": "jane@example.invalid",
        phone_key: "5555550142", region_key: "US",
    }


async def test_the_other_phone_does_not_satisfy_a_region(server, mock_gorelo):
    text = await call_tool_error(
        server,
        "create_contact",
        {**REQUIRED_FOUR, "mobile_phone_country_code": "US", "office_phone": "5555550100", "office_phone_country_code": "CA"},
    )
    assert text.startswith("mobile_phone_country_code: Gorelo refuses a region code without its phone number; pass mobile_phone too")
    assert mock_gorelo.requests == []


async def test_a_blank_phone_next_to_a_region_is_still_refused_as_blank(server, mock_gorelo):
    # the phone was given, so it is the blank phone that is wrong, and the message says so (not the region)
    text = await call_tool_error(
        server, "create_contact", {**REQUIRED_FOUR, "office_phone": "  ", "office_phone_country_code": "US"}
    )
    assert text.startswith("office_phone: must not be empty or whitespace only")
    assert "Gorelo refuses a region code" not in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "phone, region",
    [("mobile_phone", "mobile_phone_country_code"), ("office_phone", "office_phone_country_code")],
)
async def test_a_phone_without_its_region_is_a_local_error_naming_the_region(server, mock_gorelo, phone, region):
    text = await call_tool_error(server, "create_contact", {**REQUIRED_FOUR, phone: "5555550142"})
    assert text.startswith(f"{region}: required when {phone} is given")
    assert "assumes none" in text
    assert mock_gorelo.requests == []


async def test_the_other_phones_region_does_not_satisfy_a_phone(server, mock_gorelo):
    text = await call_tool_error(
        server, "create_contact", {**REQUIRED_FOUR, "mobile_phone": "1", "office_phone_country_code": "US"}
    )
    assert text.startswith("mobile_phone_country_code: required when mobile_phone is given")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("region_param", ["mobile_phone_country_code", "office_phone_country_code"])
@pytest.mark.parametrize("dial_code", ["1", "+1", "us", "USA", ""])
async def test_a_dial_code_or_a_badly_written_region_is_rejected(server, mock_gorelo, region_param, dial_code):
    text = await call_tool_error(server, "create_contact", {**REQUIRED_FOUR, region_param: dial_code})
    assert text.startswith(f"{region_param}: expected a 2 letter ISO region code in capitals")
    assert "dial codes like 1 or +1 are not accepted" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"first_name": ""}, "first_name: must not be empty or whitespace only"),
        ({"last_name": "   "}, "last_name: must not be empty or whitespace only"),
        ({"primary_email": ""}, "primary_email: must not be empty or whitespace only"),
        ({"job_title": ""}, "job_title: must not be empty or whitespace only"),
        ({"department": " "}, "department: must not be empty or whitespace only"),
        ({"time_zone": ""}, "time_zone: must not be empty or whitespace only"),
        ({"description": ""}, "description: must not be empty or whitespace only"),
        ({"mobile_phone_country_code": "US", "mobile_phone": ""}, "mobile_phone: must not be empty or whitespace only"),
        ({"secondary_email": []}, "secondary_email: must not be an empty list; omit it to create the contact without secondary emails"),
        ({"secondary_email": ["a@example.invalid", " "]}, "secondary_email: every address must be non-empty text"),
        ({"client_id": 0}, "client_id: expected a positive whole number"),
        ({"location_id": 0}, "location_id: expected a positive whole number"),
        ({"location_id": -5}, "location_id: expected a positive whole number"),
    ],
)
async def test_create_contact_local_errors_name_the_parameter_and_send_nothing(server, mock_gorelo, arguments, fragment):
    assert fragment in await call_tool_error(server, "create_contact", {**REQUIRED_FOUR, **arguments})
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("missing", ["client_id", "first_name", "last_name", "primary_email"])
async def test_create_contact_requires_the_four_fields(server, mock_gorelo, missing):
    arguments = {k: v for k, v in REQUIRED_FOUR.items() if k != missing}
    assert missing in await call_tool_error(server, "create_contact", arguments)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "property_name, param",
    [
        ("ClientId", "client_id"),
        ("FirstName", "first_name"),
        ("LastName", "last_name"),
        ("PrimaryEmail", "primary_email"),
        ("SecondaryEmail", "secondary_email"),
        ("LocationId", "location_id"),
        ("MobilePhone", "mobile_phone"),
        ("MobilePhoneCountryCode", "mobile_phone_country_code"),
        ("OfficePhone", "office_phone"),
        ("OfficePhoneCountryCode", "office_phone_country_code"),
        ("JobTitle", "job_title"),
        ("Department", "department"),
        ("TimeZone", "time_zone"),
        ("Description", "description"),
    ],
)
async def test_create_contact_maps_a_gorelo_error_to_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    mock_gorelo.on("POST", CONTACTS, error_envelope(400, [("070101", "Value is not valid.", property_name)]))
    text = await call_tool_error(server, "create_contact", REQUIRED_FOUR)
    assert text == trace(f"Gorelo rejected create_contact (HTTP 400, code 070101): {param}: Value is not valid.")


async def test_create_contact_shows_gorelos_phone_message_on_the_phone_parameter(server, mock_gorelo):
    message = "Mobile phone validation failed: Invalid phone number format: Missing or invalid default region."
    mock_gorelo.on("POST", CONTACTS, error_envelope(400, [("070101", message, "MobilePhone")]))
    text = await call_tool_error(
        server, "create_contact", {**REQUIRED_FOUR, "mobile_phone": "555", "mobile_phone_country_code": "US"}
    )
    assert f"mobile_phone: {message}" in text


async def test_create_contact_reports_a_body_gorelo_could_not_read_without_inventing_a_parameter(server, mock_gorelo):
    mock_gorelo.on("POST", CONTACTS, error_envelope(400, [("070201", "Invalid or malformed request body.")]))
    text = await call_tool_error(server, "create_contact", REQUIRED_FOUR)
    assert text == trace("Gorelo rejected create_contact (HTTP 400, code 070201): Invalid or malformed request body.")


async def test_create_contact_that_times_out_says_gorelo_did_not_confirm_and_is_not_retried(server, mock_gorelo):
    mock_gorelo.on("POST", CONTACTS, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "create_contact", REQUIRED_FOUR)
    assert text.startswith("Gorelo did not confirm create_contact") and "Verify with a read before retrying" in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "data, problem",
    [
        (None, "Data is null, not an object with an Id"),
        ({}, "Data is an object without an Id"),
        ([], "Data is an empty list, not an object with an Id"),
        (False, "Data is a boolean, not an object with an Id"),
        (7, "Data is a number, not an object with an Id"),
        ({"FirstName": "Jane"}, "Data is an object without an Id"),
        ({"Id": None}, "Data.Id is null"),
        ({"Id": 0}, "Data.Id is zero or negative"),
        ({"Id": True}, "Data.Id is a boolean"),
        ({"Id": " "}, "Data.Id is blank"),
    ],
)
async def test_create_contact_refuses_an_answer_that_is_not_a_record_with_an_id(server, mock_gorelo, data, problem):
    # created_id(): a write whose answer cannot be used raises (shape, write_unconfirmed), never returns success
    mock_gorelo.on("POST", CONTACTS, envelope(data))
    text = await call_tool_error(server, "create_contact", REQUIRED_FOUR)
    assert text.startswith("Gorelo returned an unexpected response for create_contact: POST /v1/contacts: ")
    assert f"Gorelo reported success but the answer carries no usable Id for the record ({problem})" in text
    assert "the write may have been applied, so verify it with a read before repeating it" in text
    assert len(mock_gorelo.requests) == 1  # never repeated by the tool


# --------------------------------------------------------------------------
# update_contact: the full command
# --------------------------------------------------------------------------


async def test_update_contact_reads_the_contact_then_sends_the_complete_command(server, mock_gorelo):
    updated = contact_record(JobTitle="CTO")
    routed_update(mock_gorelo, answer=updated)
    result = await call_tool(server, "update_contact", {"contact_id": 5, "job_title": "CTO"})
    assert result == updated
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", CONTACT_5), ("PATCH", CONTACT_5)]
    get, patch = mock_gorelo.requests
    assert get.query == {} and get.content == b""
    assert patch.query == {} and patch.json == command(JobTitle="CTO")


async def test_update_contact_puts_the_id_in_the_path_and_sends_no_contact_id_in_the_body(server, mock_gorelo):
    # contract e15cb5a18ec2: PATCH /v1/contacts/{contactId}, and UpdateContactCommand has no ContactId field
    routed_update(mock_gorelo)
    mock_gorelo.on("GET", "/v1/contacts/6", envelope(contact_record(6)))
    mock_gorelo.on("PATCH", "/v1/contacts/6", envelope(contact_record(6, JobTitle="CTO")))
    await call_tool(server, "update_contact", {"contact_id": 5, "job_title": "CTO"})
    await call_tool(server, "update_contact", {"contact_id": 6, "job_title": "CTO"})
    patches = patched(mock_gorelo)
    assert [(r.path, r.raw_path) for r in patches] == [(CONTACT_5, CONTACT_5), ("/v1/contacts/6", "/v1/contacts/6")]
    for request in patches:
        assert "ContactId" not in request.json and "contactId" not in request.json and "Id" not in request.json
    assert not any(r.method == "PATCH" and r.path == CONTACTS for r in mock_gorelo.requests)  # never the removed form


def test_update_contact_declares_the_published_operation_and_no_override_is_involved(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "update_contact")
    assert spec.ops == ["GET /v1/contacts/{contactId}", "PATCH /v1/contacts/{contactId}"]
    assert all(op in spec_index.ops and op not in spec_index.override_ops for op in spec.ops)  # published, not swapped in
    assert not spec_index.op(spec.ops[1]).is_live_override
    assert "PATCH /v1/contacts" not in spec_index.ops  # the collection form is gone from the published spec
    op = spec_index.op(spec.ops[1])
    assert op.path_params == {"contactId": {"type": "integer", "format": "int64"}}
    assert "ContactId" not in op.body["fields"] and set(op.body["required"]) == REQUIRED_KEYS
    doc = contacts_module.__doc__ or ""
    assert "PATCH /v1/contacts/{contactId}" in doc and "no ContactId field" in doc and "section retired" in doc


async def test_update_contact_reports_a_405_as_an_error_never_as_a_success(server, mock_gorelo):
    # what the retired collection form answered; if Gorelo ever moves the operation again this must stay loud
    mock_gorelo.on("GET", CONTACT_5, envelope(contact_record()))
    mock_gorelo.on("PATCH", CONTACT_5, httpx.Response(405, headers={"Allow": "GET, POST"}))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "job_title": "X"})
    assert text.startswith("Gorelo returned an unexpected response for update_contact: PATCH /v1/contacts/{contactId}: ")
    assert "HTTP 405" in text and [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]


async def test_update_contact_sends_every_command_field_and_never_a_read_only_one(server, mock_gorelo, spec_index):
    routed_update(mock_gorelo)
    await call_tool(server, "update_contact", {"contact_id": 5, "department": "Ops"})
    body = patched(mock_gorelo)[0].json
    assert set(body) == COMMAND_KEYS - {"TagIds"}  # TagIds only with tag_ids
    assert not set(body) & READ_ONLY_KEYS
    op = spec_index.op("PATCH /v1/contacts/{contactId}")
    assert set(body) <= set(op.body["fields"]) and set(op.body["required"]) <= set(body)


async def test_update_contact_preserves_every_value_it_was_not_told_to_change(server, mock_gorelo):
    routed_update(mock_gorelo)
    await call_tool(server, "update_contact", {"contact_id": 5, "first_name": "Janet"})
    body = patched(mock_gorelo)[0].json
    expected = command(FirstName="Janet")
    assert body == expected
    # a PATCH with only the required fields clears the phones and the job title, so the update sends their current values too
    for kept in ("MobilePhone", "MobilePhoneCountryCode", "OfficePhone", "JobTitle", "Department", "TimeZone", "Description", "LocationId"):
        assert body[kept] == contact_record()[kept]


async def test_update_contact_omits_the_values_the_contact_does_not_have_and_sends_no_null(server, mock_gorelo):
    # PATCH /v1/contacts/{contactId} clears a field it leaves out (a LocationId is kept), so an explicit null adds nothing; most of these
    # fields are not nullable in the spec, so an explicit null is never sent
    sparse = contact_record(
        LocationId=None, MobilePhone=None, MobilePhoneCountryCode=None, OfficePhone=None,
        OfficePhoneCountryCode=None, JobTitle=None, Department=None, TimeZone=None, Description=None, TagIds=[],
    )
    routed_update(mock_gorelo, record=sparse)
    await call_tool(server, "update_contact", {"contact_id": 5, "last_name": "Smith"})
    body = patched(mock_gorelo)[0].json
    assert body == {
        "FirstName": "Jane", "LastName": "Smith", "ClientId": 9101,
        "PrimaryEmail": "jane.doe@example.invalid",
    }
    assert None not in body.values()  # no value of the body is null
    assert set(body) == REQUIRED_KEYS and not set(body) & OPTIONAL_KEYS


async def test_update_contact_copies_empty_strings_from_the_record_as_they_are(server, mock_gorelo):
    routed_update(mock_gorelo, record=contact_record(JobTitle="", Description=""))
    await call_tool(server, "update_contact", {"contact_id": 5, "department": "Ops"})
    body = patched(mock_gorelo)[0].json
    assert body["JobTitle"] == "" and body["Description"] == "" and body["Department"] == "Ops"


async def test_update_contact_takes_the_client_from_the_record_and_cannot_move_it(server, mock_gorelo):
    routed_update(mock_gorelo, record=contact_record(ClientId=9102))
    await call_tool(server, "update_contact", {"contact_id": 5, "job_title": "X"})
    assert patched(mock_gorelo)[0].json["ClientId"] == 9102
    tool = next(t for t in await list_tools(server) if t.name == "update_contact")
    assert "client_id" not in tool.inputSchema["properties"]
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "client_id": 9101, "job_title": "X"})
    assert "client_id" in text and len(patched(mock_gorelo)) == 1


@pytest.mark.parametrize(
    "param, value, key",
    [
        ("first_name", "Janet", "FirstName"),
        ("last_name", "Smith", "LastName"),
        ("primary_email", "janet@example.invalid", "PrimaryEmail"),
        ("location_id", 9002, "LocationId"),
        ("mobile_phone", "5555550100", "MobilePhone"),
        ("mobile_phone_country_code", "CA", "MobilePhoneCountryCode"),
        ("office_phone", "5555550101", "OfficePhone"),
        ("office_phone_country_code", "GB", "OfficePhoneCountryCode"),
        ("job_title", "CTO", "JobTitle"),
        ("department", "Ops", "Department"),
        ("time_zone", "America/New_York", "TimeZone"),
        ("description", "New notes", "Description"),
    ],
)
async def test_each_changeable_field_replaces_exactly_its_own_command_field(server, mock_gorelo, param, value, key):
    routed_update(mock_gorelo)
    await call_tool(server, "update_contact", {"contact_id": 5, param: value})
    assert patched(mock_gorelo)[0].json == command(**{key: value})


async def test_several_changes_are_applied_in_one_command(server, mock_gorelo):
    routed_update(mock_gorelo)
    await call_tool(
        server,
        "update_contact",
        {
            "contact_id": 5,
            "first_name": "Janet",
            "mobile_phone": "5555550100",
            "mobile_phone_country_code": "CA",
            "job_title": "CTO",
            "clear_fields": ["department", "description"],
        },
    )
    body = patched(mock_gorelo)[0].json
    assert body == command(
        FirstName="Janet", MobilePhone="5555550100", MobilePhoneCountryCode="CA", JobTitle="CTO",
        Department=None, Description=None,  # cleared: left out of the body
    )
    assert "Department" not in body and "Description" not in body
    assert len(mock_gorelo.requests) == 2


# --------------------------------------------------------------------------
# update_contact: clearing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "param, key",
    [
        ("job_title", "JobTitle"),
        ("department", "Department"),
        ("time_zone", "TimeZone"),
        ("description", "Description"),
    ],
)
async def test_clear_fields_leaves_the_named_field_out_of_the_body_and_keeps_the_rest(server, mock_gorelo, param, key):
    # clearing is done by omitting the field (Gorelo clears what a PATCH leaves out), never by sending a null. The two phones are
    # not in this list: a phone that is cleared alone takes its stored region code with it (see the tests of that rule below)
    routed_update(mock_gorelo)
    result = await call_tool(server, "update_contact", {"contact_id": 5, "clear_fields": [param]})
    body = patched(mock_gorelo)[0].json
    assert key not in body
    assert body == {k: v for k, v in command().items() if k != key}
    assert None not in body.values()
    assert "note" not in result


async def test_a_phone_and_its_region_can_be_cleared_together(server, mock_gorelo):
    routed_update(mock_gorelo)
    await call_tool(
        server,
        "update_contact",
        {"contact_id": 5, "clear_fields": ["mobile_phone", "mobile_phone_country_code"]},
    )
    body = patched(mock_gorelo)[0].json
    assert "MobilePhone" not in body and "MobilePhoneCountryCode" not in body
    assert body == command(MobilePhone=None, MobilePhoneCountryCode=None)


async def test_every_clearable_field_can_be_cleared_at_once(server, mock_gorelo):
    routed_update(mock_gorelo)
    await call_tool(
        server,
        "update_contact",
        {"contact_id": 5, "clear_fields": list(contacts_module.CLEARABLE_FIELDS)},
    )
    body = patched(mock_gorelo)[0].json
    assert body == command(
        MobilePhone=None, MobilePhoneCountryCode=None, OfficePhone=None,
        OfficePhoneCountryCode=None, JobTitle=None, Department=None, TimeZone=None, Description=None,
    )
    # every clearable field is gone, the four required ones remain with values, and so do the location (it cannot be cleared)
    # and the tags are not sent at all (clear_fields never touches them: only tag_ids=[] removes tags)
    assert set(body) == REQUIRED_KEYS | {"LocationId"} and body["LocationId"] == 9001


async def test_a_repeated_name_in_clear_fields_is_cleared_once(server, mock_gorelo):
    routed_update(mock_gorelo)
    await call_tool(
        server, "update_contact",
        {"contact_id": 5, "clear_fields": ["job_title", "job_title"]},
    )
    body = patched(mock_gorelo)[0].json
    assert body == command(JobTitle=None) and "JobTitle" not in body


async def test_clear_fields_for_a_field_that_is_already_null_is_left_out_too(server, mock_gorelo):
    routed_update(mock_gorelo, record=contact_record(Department=None))
    await call_tool(server, "update_contact", {"contact_id": 5, "clear_fields": ["department"]})
    body = patched(mock_gorelo)[0].json
    assert body == command(Department=None) and "Department" not in body


async def test_an_empty_clear_fields_list_is_a_local_error_and_sends_nothing(server, mock_gorelo):
    text = await call_tool_error(
        server, "update_contact", {"contact_id": 5, "clear_fields": []}
    )
    assert "clear_fields: must not be an empty list; omit it when nothing should be cleared" in text
    assert mock_gorelo.requests == []


# What update_contact says when asked to clear the location (live, on a test client, 2026-10-24: a PATCH that leaves LocationId
# out or sends it as null keeps the location).
LOCATION_CANNOT_BE_CLEARED = (
    "clear_fields: Gorelo cannot clear a contact's location through the API (leaving it out or sending null keeps it; "
    "verified 2026-10-24); pass location_id to move the contact to another location of its client, or clear it in the "
    "Gorelo app"
)

# What update_contact says when asked to clear something that cannot be cleared.
CANNOT_BE_CLEARED = [
    (["first_name"], "clear_fields: first_name cannot be cleared because Gorelo requires it"),
    (["last_name"], "clear_fields: last_name cannot be cleared because Gorelo requires it"),
    (["primary_email"], "clear_fields: primary_email cannot be cleared because Gorelo requires it"),
    (["secondary_email"], "clear_fields: secondary_email is not cleared through clear_fields; pass secondary_email=[]"),
    (["client_id"], "clear_fields: unknown field 'client_id'; accepted: mobile_phone,"),
    (["location_id"], LOCATION_CANNOT_BE_CLEARED),
    (["JobTitle"], "clear_fields: unknown field 'JobTitle'"),
    (["job_title", "bogus"], "clear_fields: unknown field 'bogus'"),
]


@pytest.mark.parametrize("clear_fields, fragment", CANNOT_BE_CLEARED)
async def test_clear_fields_is_a_literal_list_so_the_schema_refuses_every_other_name(server, mock_gorelo, clear_fields, fragment):
    # like update_item's, clear_fields offers only the clearable names; any other is refused by the schema
    # before the tool runs (the error lists the accepted names), with no HTTP call
    text = await call_tool_error(
        server, "update_contact", {"contact_id": 5, "clear_fields": clear_fields}
    )
    assert "clear_fields." in text and "Input should be" in text
    for accepted in contacts_module.CLEARABLE_FIELDS:
        assert f"'{accepted}'" in text, accepted
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("clear_fields, fragment", CANNOT_BE_CLEARED)
async def test_update_contact_called_directly_still_explains_why_a_name_cannot_be_cleared(
    client_factory, mock_gorelo, clear_fields, fragment
):
    # defence in depth for a caller that skips the schema (a test, another helper): the friendly reasons stay
    async with client_factory() as client:
        with pytest.raises(ToolError) as caught:
            await contacts_module.update_contact(
                make_ctx(client), contact_id=5, clear_fields=clear_fields
            )
    assert fragment in str(caught.value)
    assert mock_gorelo.requests == []


def test_the_clearable_names_are_the_literal_of_the_schema_and_never_the_required_fields():
    assert contacts_module.CLEARABLE_FIELDS == typing.get_args(contacts_module.ClearableField)
    assert not set(contacts_module.CLEARABLE_FIELDS) & set(contacts_module.REQUIRED_FIELDS)
    assert "secondary_email" not in contacts_module.CLEARABLE_FIELDS
    assert set(contacts_module.CLEARABLE_FIELDS) <= set(contacts_module.UPDATE_FIELD_MAP)


async def test_a_field_cannot_be_given_a_value_and_cleared_in_the_same_call(server, mock_gorelo):
    text = await call_tool_error(
        server, "update_contact",
        {"contact_id": 5, "job_title": "CTO", "clear_fields": ["job_title"]},
    )
    assert text == "job_title: cannot be given a value and listed in clear_fields in the same call"
    assert mock_gorelo.requests == []


async def test_a_blank_value_next_to_clear_fields_is_just_a_second_way_of_saying_clear(server, mock_gorelo):
    routed_update(mock_gorelo)
    await call_tool(
        server, "update_contact",
        {"contact_id": 5, "job_title": "", "department": "  ", "clear_fields": ["job_title", "department"]},
    )
    body = patched(mock_gorelo)[0].json
    assert body == command(JobTitle=None, Department=None) and "JobTitle" not in body and "Department" not in body


@pytest.mark.parametrize(
    "param", ["mobile_phone", "office_phone", "job_title", "department", "time_zone", "description"]
)
@pytest.mark.parametrize("blank", ["", "   "])
async def test_a_blank_optional_value_is_rejected_with_the_way_to_clear_it(server, mock_gorelo, param, blank):
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, param: blank})
    assert text == (
        f"{param}: must not be empty or whitespace only; to remove the stored value pass "
        f'clear_fields=["{param}"]'
    )
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("param", ["first_name", "last_name", "primary_email"])
async def test_a_blank_required_value_is_rejected_and_cannot_be_cleared(server, mock_gorelo, param):
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, param: " "})
    assert text == f"{param}: must not be empty; Gorelo requires it and it cannot be cleared"
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# update_contact: the location and the secondary emails
# --------------------------------------------------------------------------

# Live, on a test client, 2026-10-24: a PATCH that leaves LocationId and SecondaryEmail out KEEPS both
# (a search by Query on a secondary address was the oracle: no read returns SecondaryEmail), "LocationId": null keeps the
# location, and "SecondaryEmail": [] clears the secondary emails. So an update that does not pass secondary_email just sends
# no SecondaryEmail, and a location is never cleared.


async def test_an_update_without_secondary_email_is_not_refused_and_sends_no_secondary_email(server, mock_gorelo):
    routed_update(mock_gorelo)
    result = await call_tool(server, "update_contact", {"contact_id": 5, "job_title": "CTO"})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", CONTACT_5), ("PATCH", CONTACT_5)]  # one read, one write
    body = patched(mock_gorelo)[0].json
    assert "SecondaryEmail" not in body and body == command(JobTitle="CTO")
    assert "note" not in result


async def test_a_secondary_email_list_is_sent_whole(server, mock_gorelo):
    routed_update(mock_gorelo)
    await call_tool(
        server, "update_contact",
        {"contact_id": 5, "job_title": "CTO", "secondary_email": ["a@example.invalid", "b@example.invalid"]},
    )
    assert patched(mock_gorelo)[0].json == command(
        JobTitle="CTO", SecondaryEmail=["a@example.invalid", "b@example.invalid"]
    )


async def test_an_empty_secondary_email_list_erases_them_explicitly(server, mock_gorelo):
    routed_update(mock_gorelo)
    await call_tool(server, "update_contact", {"contact_id": 5, "job_title": "CTO", "secondary_email": []})
    assert patched(mock_gorelo)[0].json == command(JobTitle="CTO", SecondaryEmail=[])


async def test_secondary_email_alone_is_a_valid_update(server, mock_gorelo):
    routed_update(mock_gorelo)
    await call_tool(server, "update_contact", {"contact_id": 5, "secondary_email": ["only@example.invalid"]})
    assert patched(mock_gorelo)[0].json == command(SecondaryEmail=["only@example.invalid"])
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]


async def test_a_blank_secondary_address_is_a_local_error(server, mock_gorelo):
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "secondary_email": ["ok@example.invalid", ""]})
    assert text.startswith("secondary_email: every address must be non-empty text")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("flag", [True, False, "true"])
async def test_the_removed_secondary_email_flag_is_refused_by_the_schema(server, mock_gorelo, flag):
    tool = next(t for t in await list_tools(server) if t.name == "update_contact")
    assert "clear_secondary_email_ok" not in tool.inputSchema["properties"]
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "job_title": "CTO", "clear_secondary_email_ok": flag})
    assert "clear_secondary_email_ok" in text and "Unexpected keyword argument" in text
    assert mock_gorelo.requests == []


async def test_the_location_from_the_record_is_still_copied_when_the_caller_does_not_pass_one(server, mock_gorelo):
    routed_update(mock_gorelo, record=contact_record(LocationId=9007))
    await call_tool(server, "update_contact", {"contact_id": 5, "job_title": "CTO"})
    assert patched(mock_gorelo)[0].json == command(JobTitle="CTO", LocationId=9007)


async def test_a_new_location_replaces_the_stored_one(server, mock_gorelo):
    routed_update(mock_gorelo, record=contact_record(LocationId=9007))
    await call_tool(server, "update_contact", {"contact_id": 5, "location_id": 9002})
    assert patched(mock_gorelo)[0].json == command(LocationId=9002)


async def test_clearing_the_location_is_refused_before_any_http_call_naming_clear_fields(client_factory, mock_gorelo):
    async with client_factory() as client:
        with pytest.raises(ToolError) as caught:
            await contacts_module.update_contact(make_ctx(client), contact_id=5, clear_fields=["location_id"])
    assert str(caught.value) == LOCATION_CANNOT_BE_CLEARED
    assert mock_gorelo.requests == []


async def test_clearing_the_location_through_the_server_is_refused_by_the_schema_with_no_http_call(server, mock_gorelo):
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "clear_fields": ["location_id"]})
    assert "clear_fields." in text and "Input should be 'mobile_phone', 'mobile_phone_country_code'" in text  # not offered
    assert mock_gorelo.requests == []


async def test_a_location_cannot_be_cleared_next_to_other_clears_either(client_factory, mock_gorelo):
    async with client_factory() as client:
        with pytest.raises(ToolError) as caught:
            await contacts_module.update_contact(
                make_ctx(client), contact_id=5, clear_fields=["job_title", "location_id"]
            )
    assert str(caught.value) == LOCATION_CANNOT_BE_CLEARED
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# update_contact: at least one change, ids, regions
# --------------------------------------------------------------------------


async def test_update_contact_with_nothing_to_change_is_a_local_error(server, mock_gorelo):
    text = await call_tool_error(server, "update_contact", {"contact_id": 5})
    assert text.startswith("nothing to update: pass at least one of first_name, last_name, primary_email")
    assert "secondary_email, tag_ids or clear_fields" in text
    assert mock_gorelo.requests == []


async def test_update_contact_rejects_an_id_that_cannot_exist_naming_contact_id(server, mock_gorelo):
    for bad in (0, -1):
        text = await call_tool_error(server, "update_contact", {"contact_id": bad, "job_title": "X"})
        assert text.startswith("contact_id: expected a positive whole number")
    assert mock_gorelo.requests == []


async def test_update_contact_rejects_a_location_that_cannot_exist(server, mock_gorelo):
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "location_id": 0})
    assert text.startswith("location_id: expected a positive whole number")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("region_param", ["mobile_phone_country_code", "office_phone_country_code"])
@pytest.mark.parametrize("dial_code", ["1", "+1", "us", ""])
async def test_update_contact_rejects_a_dial_code_or_a_badly_written_region(server, mock_gorelo, region_param, dial_code):
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, region_param: dial_code})
    assert text.startswith(f"{region_param}: expected a 2 letter ISO region code in capitals")
    assert mock_gorelo.requests == []


async def test_a_new_phone_keeps_the_region_the_contact_already_has(server, mock_gorelo):
    routed_update(mock_gorelo)
    await call_tool(server, "update_contact", {"contact_id": 5, "mobile_phone": "5555550100"})
    body = patched(mock_gorelo)[0].json
    assert body["MobilePhone"] == "5555550100" and body["MobilePhoneCountryCode"] == "US"


@pytest.mark.parametrize(
    "phone, region, phone_key, region_key",
    [
        ("mobile_phone", "mobile_phone_country_code", "MobilePhone", "MobilePhoneCountryCode"),
        ("office_phone", "office_phone_country_code", "OfficePhone", "OfficePhoneCountryCode"),
    ],
)
async def test_a_new_phone_for_a_contact_with_no_region_needs_one_and_nothing_is_written(
    server, mock_gorelo, phone, region, phone_key, region_key
):
    routed_update(mock_gorelo, record=contact_record(**{phone_key: None, region_key: None}))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, phone: "5555550100"})
    assert text.startswith(f"{region}: {phone} needs an ISO region code (for example US or CA)")
    assert f'pass {region}, for example "US" (no default is assumed)' in text
    assert patched(mock_gorelo) == []
    mock_gorelo.reset()
    routed_update(mock_gorelo, record=contact_record(**{phone_key: None, region_key: None}))
    await call_tool(server, "update_contact", {"contact_id": 5, phone: "5555550100", region: "CA"})
    body = patched(mock_gorelo)[0].json
    assert body[phone_key] == "5555550100" and body[region_key] == "CA"


async def test_clearing_only_the_region_of_a_filled_phone_is_a_local_error(server, mock_gorelo):
    routed_update(mock_gorelo)
    text = await call_tool_error(
        server, "update_contact",
        {"contact_id": 5, "clear_fields": ["mobile_phone_country_code"]},
    )
    assert text.startswith("mobile_phone_country_code: mobile_phone needs an ISO region code")
    assert "clear mobile_phone as well, or keep mobile_phone_country_code" in text
    assert patched(mock_gorelo) == []


async def test_a_phone_the_caller_did_not_touch_is_never_blocked_by_a_missing_region(server, mock_gorelo):
    # legacy data: a phone with no region stored. A change to something else must still go through.
    routed_update(mock_gorelo, record=contact_record(MobilePhone="5555550142", MobilePhoneCountryCode=None))
    await call_tool(server, "update_contact", {"contact_id": 5, "job_title": "CTO"})
    body = patched(mock_gorelo)[0].json
    # the region the contact does not have is left out of the body, never sent as an explicit null
    assert body["MobilePhone"] == "5555550142" and "MobilePhoneCountryCode" not in body
    assert None not in body.values()


# --------------------------------------------------------------------------
# update_contact: a region code stored without its phone
# --------------------------------------------------------------------------

# Gorelo answers HTTP 400, code 070101, PropertyName OfficePhone, "OfficePhone is required when OfficePhoneCountryCode is
# provided" to an update of a contact that holds a region code and no phone. PATCH clears what it leaves out, the full
# command copied the stored code, and Gorelo refuses a region code without its phone number, so ANY update of such a
# contact failed. A code that comes from the stored record is now left out of the command and reported in a `note`; a code
# the caller passes in the call is refused here, after the one read and before any write.

FIRST_NAME_ONLY = {"contact_id": 5, "first_name": "Janet"}
CLEARED_SENTENCE = "A stored region code with no phone is left out; the result's `note` names it."


@pytest.mark.parametrize("phone, region, phone_key, region_key, note", PHONE_PAIR_CASES)
async def test_a_stored_region_code_with_no_phone_is_left_out_of_the_command_and_reported_in_a_note(
    server, mock_gorelo, phone, region, phone_key, region_key, note
):
    record = contact_record(**{phone_key: None, region_key: "US"})
    answer = contact_record(FirstName="Janet", **{phone_key: None, region_key: None})
    routed_update(mock_gorelo, record=record, answer=answer)
    result = await call_tool(server, "update_contact", FIRST_NAME_ONLY)
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", CONTACT_5), ("PATCH", CONTACT_5)]
    body = patched(mock_gorelo)[0].json
    assert phone_key not in body and region_key not in body  # neither the phone nor the code is sent
    assert body == command(FirstName="Janet", **{phone_key: None, region_key: None})  # everything else is copied
    assert result == {**answer, "note": note}  # Gorelo's record, plus the one key the tool added
    assert region in result["note"] and "(was US)" in result["note"]


async def test_two_stored_region_codes_with_no_phones_are_left_out_under_one_note(server, mock_gorelo):
    record = contact_record(MobilePhone=None, MobilePhoneCountryCode="US", OfficePhone=None, OfficePhoneCountryCode="CA")
    answer = contact_record(
        FirstName="Janet", MobilePhone=None, MobilePhoneCountryCode=None, OfficePhone=None, OfficePhoneCountryCode=None
    )  # Gorelo's answer shows both codes gone
    routed_update(mock_gorelo, record=record, answer=answer)
    result = await call_tool(server, "update_contact", FIRST_NAME_ONLY)
    body = patched(mock_gorelo)[0].json
    assert body == command(
        FirstName="Janet", MobilePhone=None, MobilePhoneCountryCode=None, OfficePhone=None, OfficePhoneCountryCode=None
    )
    assert result == {**answer, "note": BOTH_NOTE}
    assert len(mock_gorelo.requests) == 2


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "spaces"])
@pytest.mark.parametrize("phone, region, phone_key, region_key, note", PHONE_PAIR_CASES)
async def test_a_blank_stored_phone_counts_as_empty(server, mock_gorelo, phone, region, phone_key, region_key, note, blank):
    routed_update(
        mock_gorelo,
        record=contact_record(**{phone_key: blank, region_key: "US"}),
        answer=contact_record(FirstName="Janet", **{phone_key: None, region_key: None}),
    )
    result = await call_tool(server, "update_contact", FIRST_NAME_ONLY)
    body = patched(mock_gorelo)[0].json
    assert phone_key not in body and region_key not in body  # the blank phone is not sent either: the contact has no phone
    assert body == command(FirstName="Janet", **{phone_key: None, region_key: None})
    assert result["note"] == note


@pytest.mark.parametrize("stored_region", [None, "US"], ids=["no-stored-code", "stored-code"])
@pytest.mark.parametrize("stored_phone", [None, "", "   "], ids=["null", "empty", "blank"])
@pytest.mark.parametrize("phone, region, phone_key, region_key, note", PHONE_PAIR_CASES)
async def test_a_region_code_passed_for_a_contact_without_that_phone_is_refused_after_the_read_and_nothing_is_written(
    server, mock_gorelo, phone, region, phone_key, region_key, note, stored_phone, stored_region
):
    routed_update(mock_gorelo, record=contact_record(**{phone_key: stored_phone, region_key: stored_region}))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, region: "CA"})
    assert text == (
        f"{region}: Gorelo refuses a region code without its phone number; pass {phone} too, or leave {region} out "
        f"(contact 5 would have no {phone} after this update)"
    )
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", CONTACT_5)]  # exactly one read, no PATCH


@pytest.mark.parametrize("stored_region", [None, "US"], ids=["no-stored-code", "stored-code"])
@pytest.mark.parametrize("phone, region, phone_key, region_key, note", PHONE_PAIR_CASES)
async def test_the_phone_passed_with_its_region_code_is_sent_with_both_and_there_is_no_note(
    server, mock_gorelo, phone, region, phone_key, region_key, note, stored_region
):
    routed_update(mock_gorelo, record=contact_record(**{phone_key: None, region_key: stored_region}))
    result = await call_tool(
        server, "update_contact", {"contact_id": 5, phone: "5555550100", region: "CA"}
    )
    assert patched(mock_gorelo)[0].json == command(**{phone_key: "5555550100", region_key: "CA"})
    assert "note" not in result


@pytest.mark.parametrize("phone, region, phone_key, region_key, note", PHONE_PAIR_CASES)
async def test_a_new_phone_takes_the_region_code_the_contact_holds_without_a_phone(
    server, mock_gorelo, phone, region, phone_key, region_key, note
):
    routed_update(mock_gorelo, record=contact_record(**{phone_key: None, region_key: "US"}))
    result = await call_tool(server, "update_contact", {"contact_id": 5, phone: "5555550100"})
    assert patched(mock_gorelo)[0].json == command(**{phone_key: "5555550100"})  # the stored "US" goes with it
    assert "note" not in result


async def test_a_stored_phone_with_its_stored_region_code_is_copied_unchanged_and_there_is_no_note(server, mock_gorelo):
    routed_update(mock_gorelo)
    result = await call_tool(server, "update_contact", FIRST_NAME_ONLY)
    assert patched(mock_gorelo)[0].json == command(FirstName="Janet")
    assert "note" not in result and result == contact_record()


@pytest.mark.parametrize("phone, region, phone_key, region_key, note", PHONE_PAIR_CASES)
async def test_clearing_a_phone_alone_takes_its_stored_region_code_with_it_and_says_so(
    server, mock_gorelo, phone, region, phone_key, region_key, note
):
    # the phone would be gone and its code would stay: the same combination Gorelo refuses
    routed_update(mock_gorelo, answer=contact_record(**{phone_key: None, region_key: None}))
    result = await call_tool(server, "update_contact", {"contact_id": 5, "clear_fields": [phone]})
    assert patched(mock_gorelo)[0].json == command(**{phone_key: None, region_key: None})
    assert result["note"] == note


@pytest.mark.parametrize("phone, region, phone_key, region_key, note", PHONE_PAIR_CASES)
async def test_clearing_the_region_code_of_a_contact_without_that_phone_needs_no_note(
    server, mock_gorelo, phone, region, phone_key, region_key, note
):
    routed_update(mock_gorelo, record=contact_record(**{phone_key: None, region_key: "US"}))
    result = await call_tool(server, "update_contact", {"contact_id": 5, "clear_fields": [region]})
    assert patched(mock_gorelo)[0].json == command(**{phone_key: None, region_key: None})
    assert "note" not in result


@pytest.mark.parametrize("phone, region, phone_key, region_key, note", PHONE_PAIR_CASES)
async def test_a_region_code_passed_next_to_the_clear_of_its_phone_is_refused(
    server, mock_gorelo, phone, region, phone_key, region_key, note
):
    routed_update(mock_gorelo)
    text = await call_tool_error(
        server, "update_contact", {"contact_id": 5, region: "CA", "clear_fields": [phone]}
    )
    assert text.startswith(f"{region}: Gorelo refuses a region code without its phone number; pass {phone} too")
    assert [r.method for r in mock_gorelo.requests] == ["GET"]


async def test_create_and_update_refuse_a_lone_region_code_in_the_same_words(server, mock_gorelo):
    created = await call_tool_error(server, "create_contact", {**REQUIRED_FOUR, "office_phone_country_code": "US"})
    routed_update(mock_gorelo, record=contact_record(OfficePhone=None, OfficePhoneCountryCode=None))
    updated = await call_tool_error(
        server, "update_contact", {"contact_id": 5, "office_phone_country_code": "US"}
    )
    assert updated.startswith(created) and updated != created  # the update adds which contact and why


async def test_the_secondary_email_list_travels_with_the_command_that_leaves_a_stale_region_code_out(server, mock_gorelo):
    routed_update(
        mock_gorelo,
        record=contact_record(OfficePhone=None, OfficePhoneCountryCode="US"),
        answer=contact_record(FirstName="Janet", OfficePhone=None, OfficePhoneCountryCode=None),
    )
    result = await call_tool(
        server, "update_contact", {"contact_id": 5, "first_name": "Janet", "secondary_email": ["a@example.invalid"]}
    )
    assert patched(mock_gorelo)[0].json == command(
        FirstName="Janet", OfficePhone=None, OfficePhoneCountryCode=None, SecondaryEmail=["a@example.invalid"]
    )
    assert result["note"] == OFFICE_NOTE


async def test_a_contact_with_a_stale_region_code_is_updated_without_a_secondary_email_decision(server, mock_gorelo):
    # the case of a stale region code, now with nothing to decide about secondary emails: one read, one write, no SecondaryEmail
    routed_update(
        mock_gorelo,
        record=contact_record(OfficePhone=None, OfficePhoneCountryCode="US"),
        answer=contact_record(FirstName="Janet", OfficePhone=None, OfficePhoneCountryCode=None),
    )
    result = await call_tool(server, "update_contact", {"contact_id": 5, "first_name": "Janet"})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", CONTACT_5), ("PATCH", CONTACT_5)]
    assert patched(mock_gorelo)[0].json == command(FirstName="Janet", OfficePhone=None, OfficePhoneCountryCode=None)
    assert result["note"] == OFFICE_NOTE


# The note is built from Gorelo's ANSWER, not from what was sent: a code is called cleared only when the answer shows that
# field null or blank. When the answer still shows the code the note says so, and the tool does not raise (the PATCH was
# answered 2xx with a record).
STILL_SHOWN = {
    "mobile_phone_country_code": (
        "mobile_phone_country_code (was US) was left out of the update, but Gorelo's answer still shows it; "
        "check with get_contact."
    ),
    "office_phone_country_code": (
        "office_phone_country_code (was US) was left out of the update, but Gorelo's answer still shows it; "
        "check with get_contact."
    ),
}


@pytest.mark.parametrize("phone, region, phone_key, region_key, note", PHONE_PAIR_CASES)
async def test_the_note_does_not_say_cleared_when_the_answer_still_shows_the_code(
    server, mock_gorelo, phone, region, phone_key, region_key, note
):
    record = contact_record(**{phone_key: None, region_key: "US"})
    answer = contact_record(FirstName="Janet", **{phone_key: None, region_key: "US"})  # Gorelo kept the code
    routed_update(mock_gorelo, record=record, answer=answer)
    result = await call_tool(server, "update_contact", FIRST_NAME_ONLY)  # no error
    assert patched(mock_gorelo)[0].json == command(FirstName="Janet", **{phone_key: None, region_key: None})
    assert result == {**answer, "note": STILL_SHOWN[region]}
    assert "cleared" not in result["note"]


@pytest.mark.parametrize("phone, region, phone_key, region_key, note", PHONE_PAIR_CASES)
@pytest.mark.parametrize("blank", [None, "", "   "], ids=["null", "empty", "spaces"])
async def test_the_note_says_cleared_when_the_answer_shows_the_code_null_or_blank(
    server, mock_gorelo, phone, region, phone_key, region_key, note, blank
):
    answer = contact_record(FirstName="Janet", **{phone_key: None, region_key: blank})
    routed_update(mock_gorelo, record=contact_record(**{phone_key: None, region_key: "US"}), answer=answer)
    result = await call_tool(server, "update_contact", FIRST_NAME_ONLY)
    assert result["note"] == note


async def test_one_code_cleared_and_one_still_shown_get_one_note_that_tells_them_apart(server, mock_gorelo):
    record = contact_record(MobilePhone=None, MobilePhoneCountryCode="US", OfficePhone=None, OfficePhoneCountryCode="CA")
    answer = contact_record(
        FirstName="Janet", MobilePhone=None, MobilePhoneCountryCode=None, OfficePhone=None, OfficePhoneCountryCode="CA"
    )
    routed_update(mock_gorelo, record=record, answer=answer)
    result = await call_tool(server, "update_contact", FIRST_NAME_ONLY)
    assert result["note"] == (
        MOBILE_NOTE + " office_phone_country_code (was CA) was left out of the update, but Gorelo's answer still shows it; "
        "check with get_contact."
    )


async def test_two_codes_that_the_answer_still_shows_are_named_together(server, mock_gorelo):
    record = contact_record(MobilePhone=None, MobilePhoneCountryCode="US", OfficePhone=None, OfficePhoneCountryCode="CA")
    routed_update(mock_gorelo, record=record, answer=record)
    result = await call_tool(server, "update_contact", FIRST_NAME_ONLY)
    assert result["note"] == (
        "mobile_phone_country_code (was US) and office_phone_country_code (was CA) were left out of the update, but "
        "Gorelo's answer still shows them; check with get_contact."
    )


async def test_an_answer_that_does_not_show_the_field_does_not_claim_it_was_cleared(server, mock_gorelo):
    record = contact_record(OfficePhone=None, OfficePhoneCountryCode="US")
    routed_update(mock_gorelo, record=record, answer=only(record, "OfficePhoneCountryCode"))
    result = await call_tool(server, "update_contact", FIRST_NAME_ONLY)
    assert result["note"] == (
        "office_phone_country_code (was US) was left out of the update, but Gorelo's answer does not show it; "
        "check with get_contact."
    )


async def test_a_new_phone_still_needs_its_region_code_when_the_other_pair_holds_a_stale_one(server, mock_gorelo):
    routed_update(
        mock_gorelo,
        record=contact_record(MobilePhone=None, MobilePhoneCountryCode=None, OfficePhone=None, OfficePhoneCountryCode="US"),
    )
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "mobile_phone": "5555550100"})
    assert text.startswith("mobile_phone_country_code: mobile_phone needs an ISO region code (for example US or CA)")
    assert patched(mock_gorelo) == []  # nothing is written, the stale office code included


@pytest.mark.parametrize("answer", [True, None, {}, []], ids=["boolean", "null", "empty-object", "empty-list"])
async def test_when_the_write_answers_without_a_record_the_dropped_code_adds_no_note_and_the_error_is_the_old_one(
    server, mock_gorelo, answer
):
    mock_gorelo.on("GET", CONTACT_5, envelope(contact_record(OfficePhone=None, OfficePhoneCountryCode="US")))
    mock_gorelo.on("PATCH", CONTACT_5, envelope(answer))
    text = await call_tool_error(server, "update_contact", FIRST_NAME_ONLY)
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]
    assert "OfficePhoneCountryCode" not in patched(mock_gorelo)[0].json  # the PATCH did go out without the stale code
    assert text.startswith(
        "Gorelo returned an unexpected response for update_contact: PATCH /v1/contacts/{contactId}: "
        "expected Data to be a non-empty object but got "
    )
    assert "the write may have been applied, so verify it with a read before repeating it" in text
    assert "cleared" not in text and "office_phone_country_code" not in text


def test_the_update_docstring_says_a_stored_region_code_with_no_phone_is_left_out_and_reported():
    doc = flat(contacts_module.update_contact.__doc__)
    assert CLEARED_SENTENCE in doc
    assert doc.index(CLEARED_SENTENCE) < doc.index("Side effects:")  # the side effects stay last, as the template has it


async def test_the_update_description_the_model_sees_has_the_sentence_and_stays_within_the_cap(server):
    tool = next(t for t in await list_tools(server) if t.name == "update_contact")
    assert CLEARED_SENTENCE in flat(tool.description)
    assert len(tool.description) <= 900  # MAX_TOOL_DESCRIPTION_CHARS of tests/test_integration.py


def test_the_module_text_explains_the_region_code_rule():
    doc = flat(contacts_module.__doc__)
    assert "Gorelo refuses a region code without its phone number" in doc and "code 070101" in doc
    assert "left OUT of the command" in doc and "`note`" in doc
    assert "built from Gorelo's ANSWER, not from what was sent" in doc


# --------------------------------------------------------------------------
# update_contact: what Gorelo returns on the read
# --------------------------------------------------------------------------


async def test_a_missing_contact_stops_at_the_read_with_a_tool_error(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/contacts/99999", error_envelope(404, [("070404", "Contact not found.")]))
    text = await call_tool_error(server, "update_contact", {"contact_id": 99999, "job_title": "X"})
    assert text == trace("Gorelo rejected update_contact (HTTP 404, code 070404): Contact not found.")
    assert [r.method for r in mock_gorelo.requests] == ["GET"]


async def test_a_record_without_data_stops_at_the_read(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACT_5, envelope(None))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "job_title": "X"})
    assert "Data is null" in text
    assert [r.method for r in mock_gorelo.requests] == ["GET"]


@pytest.mark.parametrize("key", ["MobilePhone", "JobTitle", "ClientId", "FirstName", "LocationId", "Description"])
async def test_a_record_missing_a_field_it_would_have_to_copy_is_refused_before_the_write(server, mock_gorelo, key):
    routed_update(mock_gorelo, record=only(contact_record(), key))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "department": "Ops"})
    assert text.startswith("Gorelo returned an unexpected response for update_contact: GET /v1/contacts/{contactId}: the contact record has no " + key)
    assert (
        "refusing to send an update built from it, because Gorelo clears the phone, region code, job title, department, time "
        "zone and description fields that an update leaves out, so a record that lacks one of its fields cannot be copied safely"
    ) in text
    assert "replaces the whole contact" not in text
    assert patched(mock_gorelo) == []


async def test_every_missing_field_is_listed_at_once(server, mock_gorelo):
    routed_update(mock_gorelo, record=only(contact_record(), "MobilePhone", "OfficePhone", "TimeZone"))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "department": "Ops"})
    assert "the contact record has no MobilePhone, OfficePhone, TimeZone;" in text


async def test_a_missing_field_does_not_matter_when_the_caller_supplies_that_field(server, mock_gorelo):
    routed_update(mock_gorelo, record=only(contact_record(), "MobilePhone"))
    await call_tool(server, "update_contact", {"contact_id": 5, "mobile_phone": "5555550100"})
    assert patched(mock_gorelo)[0].json["MobilePhone"] == "5555550100"


async def test_a_missing_field_does_not_matter_when_the_caller_clears_that_field(server, mock_gorelo):
    routed_update(mock_gorelo, record=only(contact_record(), "JobTitle"))
    await call_tool(server, "update_contact", {"contact_id": 5, "clear_fields": ["job_title"]})
    assert "JobTitle" not in patched(mock_gorelo)[0].json


async def test_a_record_without_an_id_is_refused(server, mock_gorelo):
    routed_update(mock_gorelo, record=only(contact_record(), "Id"))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "department": "Ops"})
    assert "the contact record has no Id" in text
    assert patched(mock_gorelo) == []


async def test_a_record_for_another_contact_is_refused(server, mock_gorelo):
    routed_update(mock_gorelo, record=contact_record(contact_id=6))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "department": "Ops"})
    assert "asked for contact 5 but Gorelo returned contact 6; refusing to update" in text
    assert patched(mock_gorelo) == []


@pytest.mark.parametrize("data, found", [([contact_record()], "a list of 1 item"), ({}, "an empty object"), (True, "a boolean")])
async def test_a_record_that_is_not_an_object_is_refused_before_the_write(server, mock_gorelo, data, found):
    mock_gorelo.on("GET", CONTACT_5, envelope(data))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "department": "Ops"})
    assert text.startswith("Gorelo returned an unexpected response for update_contact: GET /v1/contacts/{contactId}: ")
    assert f"expected Data to be a non-empty object but got {found}; refusing to guess" in text
    assert [r.method for r in mock_gorelo.requests] == ["GET"]


async def test_a_contact_with_no_client_cannot_be_updated_and_nothing_is_written(server, mock_gorelo):
    routed_update(mock_gorelo, record=contact_record(ClientId=None))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "department": "Ops"})
    assert text.startswith("contact_id: contact 5 has no client in Gorelo (ClientId is null)")
    assert "cannot attach a contact to a client" in text
    assert patched(mock_gorelo) == []


@pytest.mark.parametrize(
    "param, key, value",
    [
        ("first_name", "FirstName", "Janet"),
        ("last_name", "LastName", "Smith"),
        ("primary_email", "PrimaryEmail", "janet@example.invalid"),
    ],
)
async def test_a_contact_with_no_value_for_a_required_field_is_refused_unless_the_caller_gives_one(
    server, mock_gorelo, param, key, value
):
    # the five required fields are always sent with a value, never null and never left out. A record that has
    # none for one of them (and a caller who gave none) is refused locally, naming the parameter to pass.
    routed_update(mock_gorelo, record=contact_record(**{key: None}))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "department": "Ops"})
    assert text.startswith(f"{param}: contact 5 has no ")
    assert f"in Gorelo and Gorelo's update requires one; pass {param} with the value it should have" in text
    assert patched(mock_gorelo) == []
    mock_gorelo.reset()
    routed_update(mock_gorelo, record=contact_record(**{key: None}))
    await call_tool(server, "update_contact", {"contact_id": 5, param: value})
    body = patched(mock_gorelo)[0].json
    assert body[key] == value and None not in body.values()


SPARSE_RECORD = {
    "LocationId": None, "MobilePhone": None, "MobilePhoneCountryCode": None, "OfficePhone": None,
    "OfficePhoneCountryCode": None, "JobTitle": None, "Department": None, "TimeZone": None, "Description": None,
}


@pytest.mark.parametrize(
    "record_values",
    [
        pytest.param({}, id="full-record"),
        pytest.param(SPARSE_RECORD, id="every-optional-field-null"),
        pytest.param({"JobTitle": None, "Department": None}, id="two-fields-null"),
        pytest.param({"MobilePhone": None, "MobilePhoneCountryCode": None, "LocationId": None}, id="phone-and-location-null"),
        pytest.param({"JobTitle": "", "Description": ""}, id="empty-strings-are-values-not-nulls"),
    ],
)
@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"job_title": "CTO"}, id="change-one"),
        pytest.param({"clear_fields": ["department", "description"]}, id="clear-two"),
        pytest.param({"clear_fields": list(contacts_module.CLEARABLE_FIELDS)}, id="clear-all"),
        pytest.param({"job_title": "CTO", "clear_fields": ["department"]}, id="change-and-clear"),
        pytest.param({"secondary_email": ["a@example.invalid"]}, id="secondary-email-list"),
        pytest.param({"secondary_email": []}, id="secondary-email-erased-explicitly"),
    ],
)
async def test_the_body_never_carries_a_null_whatever_the_record_and_the_clearing(server, mock_gorelo, record_values, extra):
    # as a matrix: a copied null is omitted, a cleared field is omitted, an empty string is kept, the required
    # fields are always present with values, and no value of the body is ever null
    record = contact_record(**record_values)
    routed_update(mock_gorelo, record=record)
    await call_tool(server, "update_contact", {"contact_id": 5, **extra})
    body = patched(mock_gorelo)[0].json  # patched() already asserts: no null anywhere, the four required fields present
    expected = {}
    expected.update({k: v for k, v in record.items() if k in contacts_module.COPIED_FIELDS and v is not None})
    for param in extra.get("clear_fields", []):
        expected.pop(contacts_module.UPDATE_FIELD_MAP[param], None)
    if "job_title" in extra:
        expected["JobTitle"] = extra["job_title"]
    if "secondary_email" in extra:
        expected["SecondaryEmail"] = extra["secondary_email"]
    assert body == expected
    assert REQUIRED_KEYS <= set(body) and None not in body.values()
    assert isinstance(body.get("SecondaryEmail", []), list)  # an erased list is [], not null


async def test_an_id_that_arrives_as_a_string_in_the_record_still_matches(server, mock_gorelo):
    routed_update(mock_gorelo, record=contact_record(contact_id="5"))
    await call_tool(server, "update_contact", {"contact_id": 5, "department": "Ops"})
    assert "ContactId" not in patched(mock_gorelo)[0].json  # the contact id is in the path only
    assert patched(mock_gorelo)[0].path == CONTACT_5


# --------------------------------------------------------------------------
# update_contact: what Gorelo returns on the write
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "property_name, param",
    [
        ("contactId", "contact_id"),
        ("ContactId", "contact_id"),
        ("FirstName", "first_name"),
        ("PrimaryEmail", "primary_email"),
        ("SecondaryEmail", "secondary_email"),
        ("LocationId", "location_id"),
        ("MobilePhone", "mobile_phone"),
        ("MobilePhoneCountryCode", "mobile_phone_country_code"),
        ("OfficePhone", "office_phone"),
        ("JobTitle", "job_title"),
        ("TimeZone", "time_zone"),
        ("Description", "description"),
    ],
)
async def test_update_contact_maps_a_gorelo_error_to_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", CONTACT_5, envelope(contact_record()))
    mock_gorelo.on("PATCH", CONTACT_5, error_envelope(400, [("070101", "Value is not valid.", property_name)]))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "job_title": "X"})
    assert text == trace(f"Gorelo rejected update_contact (HTTP 400, code 070101): {param}: Value is not valid.")


async def test_a_property_the_tool_has_no_parameter_for_keeps_gorelos_own_name(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACT_5, envelope(contact_record()))
    mock_gorelo.on("PATCH", CONTACT_5, error_envelope(400, [("070101", "Client is not active.", "ClientId")]))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "job_title": "X"})
    assert text == trace("Gorelo rejected update_contact (HTTP 400, code 070101): ClientId: Client is not active.")


async def test_update_contact_that_times_out_on_the_write_says_to_verify_and_is_not_retried(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACT_5, envelope(contact_record()))
    mock_gorelo.on("PATCH", CONTACT_5, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "job_title": "X"})
    assert text.startswith("Gorelo did not confirm update_contact") and "Verify with a read before retrying" in text
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]


async def test_update_contact_after_a_server_error_on_the_write_says_it_may_have_been_applied(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACT_5, envelope(contact_record()))
    mock_gorelo.on("PATCH", CONTACT_5, httpx.Response(503, text="down", headers={"content-type": "text/plain"}))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "job_title": "X"})
    assert "The change may or may not have been applied" in text
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]


async def test_a_read_that_times_out_is_a_plain_read_failure(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACT_5, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "job_title": "X"})
    assert text.startswith("Gorelo did not answer update_contact") and "retrying is safe" in text
    assert [r.method for r in mock_gorelo.requests] == ["GET"]


@pytest.mark.parametrize(
    "answer, found",
    [
        (True, "a boolean"),
        (False, "a boolean"),
        (None, "null"),
        ({}, "an empty object"),
        ([], "an empty list"),
        ("ok", "a string"),
    ],
)
async def test_when_the_write_does_not_answer_with_a_record_the_tool_raises_and_does_not_read_back(
    server, mock_gorelo, answer, found
):
    # expect_object(): the write may have been applied, so the model must verify it, never repeat it.
    # A read-back would show the OLD record after a `false` and look like success, so there is none.
    mock_gorelo.on("GET", CONTACT_5, envelope(contact_record()))
    mock_gorelo.on("PATCH", CONTACT_5, envelope(answer))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "job_title": "CTO"})
    assert text.startswith("Gorelo returned an unexpected response for update_contact: PATCH /v1/contacts/{contactId}: ")
    assert f"expected Data to be a non-empty object but got {found}" in text
    assert "the write may have been applied, so verify it with a read before repeating it" in text
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]


async def test_update_contact_returns_the_record_the_write_answered_with(server, mock_gorelo):
    fresh = contact_record(JobTitle="CTO")
    mock_gorelo.on("GET", CONTACT_5, envelope(contact_record()))
    mock_gorelo.on("PATCH", CONTACT_5, envelope(fresh))
    result = await call_tool(server, "update_contact", {"contact_id": 5, "job_title": "CTO"})
    assert result == fresh
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]


# --------------------------------------------------------------------------
# Strict ids
# --------------------------------------------------------------------------

STRICT_CASES = [
    ("list_contacts", {}, "client_id"),
    ("get_contact", {"contact_id": 5}, "contact_id"),
    ("create_contact", dict(REQUIRED_FOUR), "client_id"),
    ("create_contact", dict(REQUIRED_FOUR), "location_id"),
    ("update_contact", {"contact_id": 5, "job_title": "X"}, "contact_id"),
    ("update_contact", {"contact_id": 5, "job_title": "X"}, "location_id"),
]


@pytest.mark.parametrize("name, arguments, param", STRICT_CASES)
@pytest.mark.parametrize("bad", [True, False, "5", "abc", 5.0, 5.5, [5]])
async def test_a_single_id_is_strict_and_nothing_is_sent(server, mock_gorelo, name, arguments, param, bad):
    text = await call_tool_error(server, name, {**arguments, param: bad})
    assert param in text and "valid integer" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("name, arguments, param", [c for c in STRICT_CASES if c[2] in ("contact_id", "client_id") and c[0] != "list_contacts"])
async def test_a_required_id_cannot_be_null(server, mock_gorelo, name, arguments, param):
    text = await call_tool_error(server, name, {**arguments, param: None})
    assert param in text and "valid integer" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("param", ["client_ids", "status_ids"])
@pytest.mark.parametrize("bad", [[True], ["5"], [5.0], [1, True], [None], "5", 5])
async def test_the_id_lists_are_strict_item_by_item(server, mock_gorelo, param, bad):
    text = await call_tool_error(server, "list_contacts", {param: bad})
    assert param in text and ("valid integer" in text or "valid list" in text)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("value", [2**63, 10**30])
async def test_ids_above_the_largest_gorelo_id_are_refused_naming_the_parameter(server, mock_gorelo, value):
    for name, arguments, param in (
        ("get_contact", {}, "contact_id"),
        ("list_contacts", {}, "client_id"),
        ("create_contact", dict(REQUIRED_FOUR), "location_id"),
    ):
        text = await call_tool_error(server, name, {**arguments, param: value})
        assert text.startswith(f"{param}: expected a positive whole number such as 123, got a number above ")
    text = await call_tool_error(server, "list_contacts", {"client_ids": [1, value]})
    assert text.startswith("client_ids[1]: expected a positive whole number such as 123, got a number above ")
    assert mock_gorelo.requests == []


async def test_an_explicit_null_for_every_optional_parameter_is_still_accepted(server, mock_gorelo):
    # the schema no longer advertises null for optional parameters, but validation still accepts it
    routed_update(mock_gorelo)
    nulls = {name: None for name in UPDATE_PARAMS[1:] if name != "secondary_email"}
    await call_tool(
        server, "update_contact",
        {"contact_id": 5, **nulls, "job_title": "CTO", "secondary_email": None},
    )
    assert patched(mock_gorelo)[0].json == command(JobTitle="CTO")
    mock_gorelo.on("GET", CONTACTS, paged_envelope([contact_record()]))
    await call_tool(
        server, "list_contacts",
        {"client_id": None, "client_ids": None, "status_ids": None, "query": None, "cursor": None, "created_since": None},
    )
    assert mock_gorelo.last.query == {"PageSize": "200"}


async def test_the_optional_parameters_default_to_null_without_advertising_a_null_branch(server):
    # The server compacts every advertised schema (server.compact_input_schema, see test_schema_compaction.py): an
    # optional parameter shows only its real type and description, with no null branch and no "default": null.
    # The declared types are plain `X | None`, so an explicit null is still accepted (see the test above).
    tools = {t.name: t.inputSchema["properties"] for t in await list_tools(server)}
    assert all("anyOf" not in p for name in ("list_contacts", "create_contact", "update_contact") for p in tools[name].values())
    assert not [
        (name, pname)
        for name in ("list_contacts", "create_contact", "update_contact")
        for pname, p in tools[name].items()
        if "default" in p and p["default"] is None
    ]
    update = tools["update_contact"]
    assert update["clear_fields"]["type"] == "array"
    assert update["clear_fields"]["items"] == {"type": "string", "enum": list(contacts_module.CLEARABLE_FIELDS)}
    assert update["location_id"]["type"] == "integer" and "default" not in update["location_id"]
    assert tools["list_contacts"]["client_ids"]["items"] == {"type": "integer"}


# --------------------------------------------------------------------------
# Contract 6c30770dffc0 (release 1.5.0): contact tags and the LocationIds filter
# --------------------------------------------------------------------------

TAGS = "/v1/contacts/tags"


async def test_list_contact_tags_reads_the_unpaged_list_and_sends_no_query(server, mock_gorelo):
    rows = [
        {"Id": 3, "Name": "Approver", "CreatedOn": None, "UpdatedOn": None, "Description": "Can approve"},
        {"Id": 7, "Name": "VIP", "CreatedOn": None, "UpdatedOn": None, "Description": None},
    ]
    mock_gorelo.on("GET", TAGS, envelope(rows))
    result = await call_tool(server, "list_contact_tags", {})
    assert result == {"items": rows, "count": 2}
    assert (mock_gorelo.last.method, mock_gorelo.last.path, mock_gorelo.last.query) == ("GET", TAGS, {})


async def test_list_contact_tags_reports_an_empty_list_as_count_zero(server, mock_gorelo):
    mock_gorelo.on("GET", TAGS, envelope([]))
    assert await call_tool(server, "list_contact_tags", {}) == {"items": [], "count": 0}


async def test_list_contact_tags_maps_a_gorelo_error_and_refuses_a_non_list_answer(server, mock_gorelo):
    mock_gorelo.on("GET", TAGS, in_order(
        error_envelope(403, [("080203", "API key does not have 'Contacts' read access", None)]),
        envelope({"Id": 1}),
    ))
    text = await call_tool_error(server, "list_contact_tags", {})
    assert "Contacts" in text and "list_contact_tags" in text
    assert "expected Data to be a list" in await call_tool_error(server, "list_contact_tags", {})


async def test_list_contacts_sends_location_ids_comma_joined_as_locationids(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACTS, paged_envelope([contact_record()]))
    result = await call_tool(server, "list_contacts", {"location_ids": [9001, 9002], "client_id": 9101})
    assert mock_gorelo.last.query == {"ClientIds": "9101", "LocationIds": "9001,9002", "PageSize": "200"}
    assert result["filters"] == {"client_id": 9101, "location_ids": [9001, 9002]}


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"location_ids": []}, "location_ids"),
        ({"location_ids": [0]}, "location_ids"),
        ({"location_ids": list(range(1, 102))}, "location_ids"),
    ],
)
async def test_list_contacts_refuses_a_bad_location_ids_before_any_http(server, mock_gorelo, arguments, fragment):
    text = await call_tool_error(server, "list_contacts", arguments)
    assert fragment in text and mock_gorelo.requests == []


async def test_list_contacts_maps_a_locationids_error_to_the_parameter(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACTS, error_envelope(400, [("070101", "A filter accepts at most 100 ids.", "LocationIds")]))
    text = await call_tool_error(server, "list_contacts", {"location_ids": [1]})
    assert text == trace("Gorelo rejected list_contacts (HTTP 400, code 070101): location_ids: A filter accepts at most 100 ids.")


async def test_create_contact_sends_tag_ids_as_tagids_and_checks_the_answer(server, mock_gorelo):
    mock_gorelo.on("POST", CONTACTS, envelope(contact_record(TagIds=[3, 7])))
    result = await call_tool(server, "create_contact", {**REQUIRED_FOUR, "tag_ids": [7, 3]})
    assert mock_gorelo.last.json == {
        "ClientId": 9101, "FirstName": "Jane", "LastName": "Doe", "PrimaryEmail": "jane@example.invalid",
        "TagIds": [7, 3],
    }
    assert result["TagIds"] == [3, 7]


async def test_create_contact_without_tag_ids_sends_no_tagids(server, mock_gorelo):
    mock_gorelo.on("POST", CONTACTS, envelope(contact_record(TagIds=[])))
    await call_tool(server, "create_contact", REQUIRED_FOUR)
    assert "TagIds" not in mock_gorelo.last.json


@pytest.mark.parametrize(
    "tags, fragment",
    [([], "tag_ids: must not be an empty list"), ([0], "tag_ids"), ([True], "tag_ids"), (list(range(1, 102)), "tag_ids: at most 100")],
)
async def test_create_contact_refuses_bad_tag_ids_before_any_http(server, mock_gorelo, tags, fragment):
    text = await call_tool_error(server, "create_contact", {**REQUIRED_FOUR, "tag_ids": tags})
    assert fragment in text and mock_gorelo.requests == []


async def test_create_contact_maps_a_tagids_error_to_the_parameter(server, mock_gorelo):
    mock_gorelo.on("POST", CONTACTS, error_envelope(400, [("070101", "Tag 99 is not an active contact tag.", "TagIds")]))
    text = await call_tool_error(server, "create_contact", {**REQUIRED_FOUR, "tag_ids": [99]})
    assert text == trace("Gorelo rejected create_contact (HTTP 400, code 070101): tag_ids: Tag 99 is not an active contact tag.")


async def test_create_contact_answer_that_shows_other_tags_is_a_write_unconfirmed_error(server, mock_gorelo):
    mock_gorelo.on("POST", CONTACTS, envelope(contact_record(TagIds=[3])))
    text = await call_tool_error(server, "create_contact", {**REQUIRED_FOUR, "tag_ids": [3, 7]})
    assert "TagIds differ" in text and "get_contact" in text and len(mock_gorelo.requests) == 1


async def test_update_contact_without_tag_ids_sends_no_tagids_even_when_the_contact_has_tags(server, mock_gorelo):
    # the spec says "omit to leave it unchanged": re-sending a stored tag that has become inactive would be a 400 on TagIds
    routed_update(mock_gorelo, record=contact_record(TagIds=[2, 9]))
    await call_tool(server, "update_contact", {"contact_id": 5, "job_title": "CTO"})
    body = patched(mock_gorelo)[0].json
    assert "TagIds" not in body and body == command(JobTitle="CTO")


async def test_update_contact_of_a_contact_with_no_tags_sends_no_tagids(server, mock_gorelo):
    routed_update(mock_gorelo, record=contact_record(TagIds=[]))
    await call_tool(server, "update_contact", {"contact_id": 5, "job_title": "CTO"})
    assert "TagIds" not in patched(mock_gorelo)[0].json


async def test_update_contact_with_tag_ids_sends_that_list_instead_of_the_stored_one(server, mock_gorelo):
    routed_update(mock_gorelo, record=contact_record(TagIds=[2, 9]), answer=contact_record(TagIds=[4, 5]))
    result = await call_tool(server, "update_contact", {"contact_id": 5, "tag_ids": [5, 4]})
    assert patched(mock_gorelo)[0].json == command(TagIds=[5, 4])
    assert result["TagIds"] == [4, 5]


async def test_update_contact_tag_ids_alone_is_enough_to_update(server, mock_gorelo):
    routed_update(mock_gorelo, record=contact_record(TagIds=[]), answer=contact_record(TagIds=[7]))
    await call_tool(server, "update_contact", {"contact_id": 5, "tag_ids": [7]})
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH"]


async def test_update_contact_with_an_empty_tag_ids_clears_every_tag_with_an_explicit_empty_list(server, mock_gorelo):
    routed_update(mock_gorelo, record=contact_record(TagIds=[2, 9]), answer=contact_record(TagIds=[]))
    result = await call_tool(server, "update_contact", {"contact_id": 5, "tag_ids": []})
    body = patched(mock_gorelo)[0].json
    assert body == command(TagIds=[]) and body["TagIds"] == []
    assert result["TagIds"] == []


async def test_update_contact_clear_fields_never_touches_the_tags(server, mock_gorelo):
    routed_update(mock_gorelo, record=contact_record(TagIds=[2, 9]))
    await call_tool(server, "update_contact", {"contact_id": 5, "clear_fields": ["job_title"]})
    body = patched(mock_gorelo)[0].json
    assert "TagIds" not in body and "JobTitle" not in body
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "clear_fields": ["tag_ids"]})
    assert "clear_fields" in text and len(patched(mock_gorelo)) == 1


@pytest.mark.parametrize(
    "tags, fragment",
    [([0], "tag_ids"), ([True], "tag_ids"), (list(range(1, 102)), "tag_ids: at most 100"), ("3", "tag_ids")],
)
async def test_update_contact_refuses_bad_tag_ids_before_any_http(server, mock_gorelo, tags, fragment):
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "tag_ids": tags})
    assert fragment in text and mock_gorelo.requests == []


async def test_update_contact_maps_a_tagids_error_to_the_parameter(server, mock_gorelo):
    mock_gorelo.on("GET", CONTACT_5, envelope(contact_record()))
    mock_gorelo.on("PATCH", CONTACT_5, error_envelope(400, [("070101", "Tag 99 is not an active contact tag.", "TagIds")]))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "tag_ids": [99]})
    assert text == trace("Gorelo rejected update_contact (HTTP 400, code 070101): tag_ids: Tag 99 is not an active contact tag.")


async def test_update_contact_answer_that_shows_other_tags_is_a_write_unconfirmed_error(server, mock_gorelo):
    routed_update(mock_gorelo, record=contact_record(TagIds=[2]), answer=contact_record(TagIds=[2]))
    text = await call_tool_error(server, "update_contact", {"contact_id": 5, "tag_ids": [8]})
    assert "TagIds differ" in text and "get_contact" in text


async def test_update_contact_ignores_the_tagids_of_the_record_whatever_its_shape(server, mock_gorelo):
    # the stored tags are never copied, so a record without TagIds, or with an odd one, is no reason to refuse
    mock_gorelo.on("GET", CONTACT_5, envelope(only(contact_record(), "TagIds")))
    mock_gorelo.on("PATCH", CONTACT_5, envelope(contact_record(TagIds=[4])))
    await call_tool(server, "update_contact", {"contact_id": 5, "job_title": "X"})
    assert "TagIds" not in patched(mock_gorelo)[0].json
    mock_gorelo.requests.clear()
    routed_update(mock_gorelo, record=contact_record(TagIds="3,7"))
    await call_tool(server, "update_contact", {"contact_id": 5, "job_title": "X"})
    assert "TagIds" not in patched(mock_gorelo)[0].json
    # given tag_ids, the list is sent as the caller gave it
    mock_gorelo.requests.clear()
    routed_update(mock_gorelo, record=contact_record(), answer=contact_record(TagIds=[4]))
    await call_tool(server, "update_contact", {"contact_id": 5, "tag_ids": [4]})
    assert patched(mock_gorelo)[0].json == command(TagIds=[4])


async def test_the_tag_and_location_ids_name_the_tool_that_resolves_them(server):
    tools = {t.name: t for t in await list_tools(server)}
    for name in ("create_contact", "update_contact"):
        assert "list_contact_tags" in tools[name].inputSchema["properties"]["tag_ids"]["description"]
    assert "list_client_locations" in tools["list_contacts"].inputSchema["properties"]["location_ids"]["description"]
    assert "[] removes them all" in tools["update_contact"].inputSchema["properties"]["tag_ids"]["description"]


def test_list_contact_tags_docstring_names_what_it_resolves():
    doc = flat(contacts_module.list_contact_tags.__doc__)
    assert "tag_ids" in doc and "create_contact" in doc and "update_contact" in doc
