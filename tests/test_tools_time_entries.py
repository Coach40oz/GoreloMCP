"""tools/time_entries.py: list_time_entries, get_time_entry, create_time_entry, update_time_entry,
list_billing_roles, list_work_types and delete_time_entry. Offline: MockGorelo plus an in-process client."""

import json
import types
import typing
from pathlib import Path
from typing import Annotated

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
    paged_envelope,
    paged_responder,
    uid,
)

import tools  # noqa: F401  (importing the package registers every tool)
from gorelo_client import FORBIDDEN_OPS
from settings import TOOLSETS
from tools import _common, time_entries
from pydantic import Field, Strict

from tools._common import MAX_ID, REGISTRY, StrictId

pytestmark = pytest.mark.anyio

EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)
DROP = object()  # in make_args: remove this key from the base arguments
DESCRIPTION_MAX, PARAM_DESCRIPTION_MAX = 900, 220  # CONTRIBUTING.md: never more than these
MODULE_GROUP = ("tools.time_entries", "tools.contracts", "tools.forms")
LATEST_SPEC = Path(__file__).resolve().parent.parent / "backups" / "swagger-latest.json"  # the raw published spec
# The tool list of the module group (time_entries, contracts, forms) (json.dumps of each tool as a client lists it, summed). The advertised schemas
# are compacted centrally (server.compact_input_schema), so what the modules still control is their descriptions.
# Measured 16185 bytes (descriptions are 7685 of them); the limit is that plus 10 percent, rounded up to the next 100.
# 16727 bytes after the shape text of 2026-10-03 (the list and get descriptions name the User, Ticket and Task objects);
# the limit did not move. 17118 bytes before the closed ticket sentence of create_time_entry and 17229 after it
# (111 bytes); the limit did not move.
# 1.5.0: the group gained create_contract and update_contract (contract 6c30770dffc0; 14 tools now) and the new query and body
# texts of list_contracts and get_contract: 37845 bytes measured, the limit is that plus about 1 percent, rounded up to the next 100.
# 1.6.0: actual_hours and distance are strict numbers with a bound (the schema now carries exclusiveMinimum and minimum): 38250 bytes
# became 38324; the limit is the new size plus about 1 percent, rounded up to the next 100.
# 1.7.0: create_time_entry gained auto_service_line, the wider ticket_id text and two description sentences (38324 became 38760);
# the limit is the new size plus about 1 percent, rounded up to the next 100.
# 1.7.0, the own service line, the location and date scope and the read-back notes: 38825 bytes measured; the limit still fits and stays.
# 1.7.0, the other labor lines, the margin refusal and the read-back checks (the create_time_entry description is 900 characters): 38839 bytes measured; the limit still fits and stays.
MODULE_BUDGET_BYTES = 39200
VERIFY_AFTER_SHAPE = "the write may have been applied, so verify it with a read before repeating it"

# What the time entry tools must provide: name -> (kind, ops)
EXPECTED_TOOLS = {
    "list_time_entries": ("read", ["GET /v1/time-entries"]),
    "get_time_entry": ("read", ["GET /v1/time-entries/{timeEntryId}"]),
    "create_time_entry": (
        "write",
        [
            "POST /v1/time-entries",
            "GET /v1/time-entries/{timeEntryId}",
            "GET /v1/tickets",
            "GET /v1/tickets/{ticketId}",
            "GET /v1/contracts",
        ],
    ),
    "update_time_entry": ("write", ["PATCH /v1/time-entries/{timeEntryId}"]),
    "list_billing_roles": ("read", ["GET /v1/billing-roles"]),
    "list_work_types": ("read", ["GET /v1/work-types"]),
    "delete_time_entry": ("destructive", ["DELETE /v1/time-entries/{timeEntryId}"]),
}


def reference(n=1, number="2", title="Printer offline"):
    """A ticket or task reference as the published TimeEntryModel has it (NumberedReferenceModel): Id, Number, Title."""
    return {"Id": uid(n), "Number": number, "Title": title}


def entry(entry_id=501, **overrides):
    """A time entry as the live API returns it (PascalCase): the published
    TimeEntryModel, with the user, ticket and task as objects (User {Id, Name}, Ticket and Task {Id, Number, Title}) and
    Task null for a ticket entry. There is no flat UserId, TicketId or TaskId (see flat_entry)."""
    record = {
        "Id": entry_id,
        "Ticket": reference(1),
        "Task": None,
        "User": {"Id": 9201, "Name": "Alex Example"},
        "StartedOn": "2026-10-01T14:00:00Z",
        "EndedOn": "2026-10-01T15:30:00Z",
        "ActualHours": 1.5,
        "AdjustedHours": 1.5,
        "BillableStatus": {"Id": 1, "Name": "Billable"},
        "BillingRole": {"Id": 3, "Name": "Technician"},
        "WorkType": {"Id": 4, "Name": "Remote"},
        "ServiceLine": {"Id": 77, "Name": "Managed Services"},
        "Comment": "Swapped the toner",
        "Distance": None,
        "Attachments": [],
        "CreatedOn": "2026-10-01T15:31:00Z",
        "UpdatedOn": None,
    }
    record.update(overrides)
    return record


def flat_entry(entry_id=501, **overrides):
    """The record the live API returned on 2026-10-02 instead: the same entry with the flat ids UserId, TicketId and
    TaskId in place of the User, Ticket and Task objects. The tools never read either shape, so it passes through too."""
    record = entry(entry_id)
    user, ticket, task = (record.pop(key) for key in ("User", "Ticket", "Task"))
    record.update(UserId=user["Id"], TicketId=ticket and ticket["Id"], TaskId=task and task["Id"])
    record.update(overrides)
    return record


def make_args(base, **overrides):
    args = dict(base)
    for key, value in overrides.items():
        if value is DROP:
            args.pop(key, None)
        else:
            args[key] = value
    return args


def rejected(tool, status, code, detail):
    """The text format_gorelo_error gives for a Gorelo error envelope with one notification."""
    return f"Gorelo rejected {tool} (HTTP {status}, code {code}): {detail} [trace {TEST_TRACE_ID}]"


@pytest.fixture
def server(server_factory):
    return server_factory()


@pytest.fixture
def destructive_server(server_factory):
    return server_factory(destructive=True)


def module_specs():
    return {spec.name: spec for spec in REGISTRY.specs if spec.fn.__module__ == "tools.time_entries"}


# --------------------------------------------------------------------------
# Declarations: names, kinds, toolset, ops, hints, descriptions
# --------------------------------------------------------------------------


def test_the_module_declares_exactly_the_documented_tools():
    specs = module_specs()
    assert {name: (spec.kind, spec.ops) for name, spec in specs.items()} == EXPECTED_TOOLS
    assert {spec.toolset for spec in specs.values()} == {"time"}


def test_only_update_and_delete_carry_the_destructive_hint():
    hints = {name: spec.destructive_hint for name, spec in module_specs().items()}
    assert hints == {
        "list_time_entries": False,
        "get_time_entry": False,
        "create_time_entry": False,
        "update_time_entry": True,
        "list_billing_roles": False,
        "list_work_types": False,
        "delete_time_entry": True,
    }


def test_the_private_copies_of_the_shared_helpers_are_gone():
    # Rule: positive_id, positive_ids, guid, guids, created_id, expect_object and
    # describe_value come from tools._common; this module keeps no copy of them
    for name in ("_positive_id", "_int_ids", "_guid", "_guid_ids", "_kind_of", "_shape_error", "_record"):
        assert not hasattr(time_entries, name), name
    assert time_entries.positive_id is _common.positive_id and time_entries.guids is _common.guids
    assert time_entries.created_id is _common.created_id and time_entries.expect_object is _common.expect_object


def test_every_declared_op_exists_in_the_spec_and_none_is_forbidden(spec_index):
    for name, spec in module_specs().items():
        for op in spec.ops:
            assert op in spec_index.ops, f"{name}: {op}"
            assert op not in FORBIDDEN_OPS, f"{name}: {op}"


# Spec fields of POST /v1/time-entries that no parameter sends, each with the reason (a decision for 1.5.0).
UNEXPOSED_CREATE_FIELDS = {
    "CreatedOn": "backdated historical import: never invoiced and cannot be changed or deleted later; left out on purpose",
}


def test_the_field_maps_cover_exactly_the_spec_fields(spec_index):
    create = spec_index.op("POST /v1/time-entries")
    assert set(time_entries.CREATE_FIELDS.values()) | set(UNEXPOSED_CREATE_FIELDS) == set(create.body["fields"])
    assert not set(time_entries.CREATE_FIELDS.values()) & set(UNEXPOSED_CREATE_FIELDS)
    update = spec_index.op("PATCH /v1/time-entries/{timeEntryId}")
    assert set(time_entries.UPDATE_FIELDS.values()) == set(update.body["fields"])
    listing = spec_index.op("GET /v1/time-entries")
    assert set(time_entries.LIST_QUERY.values()) | {"PageSize", "Cursor"} == set(listing.query_params)
    assert listing.paged and not spec_index.op("GET /v1/billing-roles").paged
    assert not spec_index.op("GET /v1/work-types").paged


async def test_create_time_entry_has_no_created_on_parameter_by_the_owners_decision(server_factory, mock_gorelo):
    listed = {t.name: t for t in await list_tools(server_factory())}
    params = set(listed["create_time_entry"].inputSchema["properties"])
    assert not params & {"created_on", "created_at", "backdate", "back_dated"}
    for value in time_entries.CREATE_FIELDS.values():
        assert value != "CreatedOn"
    assert mock_gorelo.requests == []


async def test_the_tools_appear_in_the_time_toolset_only_and_delete_needs_the_destructive_flag(server_factory):
    time_only = {t.name for t in await list_tools(server_factory(toolsets={"time"}, destructive=True))}
    assert set(EXPECTED_TOOLS) <= time_only
    without_deletes = {t.name for t in await list_tools(server_factory(toolsets={"time"}, destructive=False))}
    assert set(EXPECTED_TOOLS) - {"delete_time_entry"} <= without_deletes and "delete_time_entry" not in without_deletes
    others = {t.name for t in await list_tools(server_factory(toolsets=set(TOOLSETS) - {"time"}, destructive=True))}
    assert not others & set(EXPECTED_TOOLS)


async def test_schemas_annotations_and_descriptions(server_factory):
    listed = {t.name: t for t in await list_tools(server_factory(destructive=True))}
    for name, (kind, _ops) in EXPECTED_TOOLS.items():
        tool = listed[name]
        schema = tool.inputSchema
        for param, entry_schema in schema["properties"].items():
            description = entry_schema.get("description")
            assert description, f"{name}.{param} has no description"
            assert len(description) <= PARAM_DESCRIPTION_MAX, f"{name}.{param}: {len(description)} characters"
            assert EM_DASH not in description and EN_DASH not in description
        text = tool.description
        assert text and len(text) <= DESCRIPTION_MAX, f"{name}: {len(text)} characters"
        if kind != "read":
            assert "Side effects:" in text, name  # a read says nothing: its readOnlyHint says it
        assert EM_DASH not in text and EN_DASH not in text
        assert tool.annotations.readOnlyHint is (kind == "read")
        assert tool.annotations.destructiveHint is (name in ("update_time_entry", "delete_time_entry"))
    assert "Paging: pass next_cursor back as cursor with the SAME filters until has_more is false." in listed[
        "list_time_entries"
    ].description
    for name in ("list_billing_roles", "list_work_types"):
        assert "there is no paging" in listed[name].description
        assert listed[name].inputSchema["properties"] == {}
    create = listed["create_time_entry"]
    assert create.inputSchema["required"] == ["user_id"]
    assert listed["update_time_entry"].inputSchema["required"] == ["time_entry_id"]
    delete = listed["delete_time_entry"].inputSchema
    assert delete["required"] == ["time_entry_id"]
    assert delete["properties"]["confirm"]["type"] == "boolean" and delete["properties"]["confirm"]["default"] is False
    assert listed["list_time_entries"].inputSchema["properties"]["page_size"]["default"] == 100


# Every parameter that takes an id of another record names the tool that lists it (in the parameter text).
ID_SOURCES = {
    "list_time_entries": {
        "client_ids": "list_clients",
        "location_ids": "list_client_locations",
        "ticket_ids": "list_tickets",
        "task_ids": "list_project_tasks",
        "user_ids": "list_org_users",
        "invoice_ids": "list_invoices",
    },
    "get_time_entry": {"time_entry_id": "list_time_entries"},
    "create_time_entry": {
        "user_id": "list_org_users",
        "ticket_id": "list_tickets",
        "task_id": "list_project_tasks",
        "billing_role_id": "list_billing_roles",
        "work_type_id": "list_work_types",
        "service_line_id": "list_contracts",
    },
    "update_time_entry": {
        "time_entry_id": "list_time_entries",
        "user_id": "list_org_users",
        "billing_role_id": "list_billing_roles",
        "work_type_id": "list_work_types",
        "service_line_id": "list_contracts",
    },
    "delete_time_entry": {"time_entry_id": "list_time_entries"},
}


async def test_every_id_parameter_names_the_tool_that_resolves_it(server_factory):
    listed = {t.name: t for t in await list_tools(server_factory(destructive=True))}
    for tool, params in ID_SOURCES.items():
        properties = listed[tool].inputSchema["properties"]
        for param, source in params.items():
            assert source in properties[param]["description"], f"{tool}.{param} does not name {source}"


async def test_the_descriptions_carry_what_the_documentation_requires(server_factory):
    listed = {t.name: t for t in await list_tools(server_factory(destructive=True))}
    text = {name: " ".join(tool.description.split()) for name, tool in listed.items()}

    def param(tool, name):
        return listed[tool].inputSchema["properties"][name]["description"]

    create = text["create_time_entry"]
    for fragment in (
        "ONE ticket or ONE task",
        "rounded UP by the work type's minimum and increment",
        "billed to the ticket's client",
        "can draw down contract hours",
        "all three must agree",
        "Logging twice records the time twice",
        "check list_time_entries before repeating",
        "only the re-read failed: do not repeat the write",
    ):
        assert fragment in create, fragment
    assert "1 Billable, 2 No charge, 3 Non-billable" in param("create_time_entry", "billable_status_id")
    for name in ("billable_status_id", "billing_role_id", "work_type_id", "service_line_id"):
        assert "Default: ticket's" in param("create_time_entry", name), name
    # the whole fallback chain of the role and the work type (it had lost "else the provider's first")
    for name in ("billing_role_id", "work_type_id"):
        assert "Default: ticket's, else user's, else the provider's first." in param("create_time_entry", name), name
    assert "stops automatic contract assignment" in param("create_time_entry", "no_service_line")
    assert "Not with service_line_id" in param("create_time_entry", "no_service_line")
    # a task id comes from a projects tool, which exists only when the projects toolset is on
    assert param("create_time_entry", "task_id") == "Task GUID (list_project_tasks, projects toolset)."
    assert param("list_time_entries", "task_ids") == "Task GUIDs (list_project_tasks, projects toolset)."
    # the attachments are the name and url upload_attachment returned, nothing else
    attachments = param("create_time_entry", "attachments")
    assert "Pass only the name and url from upload_attachment, unchanged." in attachments
    assert "Use only links the user gave you" not in attachments  # the old rule is replaced, not stacked
    update = text["update_time_entry"]
    for fragment in (
        "OPEN time entry",
        "re-stamps its cost rate",
        "merge with the entry's current values",
        "refused (409)",
        "delete the entry and log it again",
        "moves its contract hours from the old contract to the new",
        # the delete tool exists only when deletes are enabled, so naming it says so
        "delete the entry and log it again (delete_time_entry, when deletes are enabled)",
    ):
        assert fragment in update, fragment
    assert "takes the entry off its contract" in param("update_time_entry", "remove_from_contract")
    assert "erases the comment" in param("update_time_entry", "clear_comment")
    delete = text["delete_time_entry"]
    for fragment in (
        "Reopened",
        "Deleted",
        "second call with confirm=true",
        "closed ticket",
        "(409)",
        "no tool restores it",
        "reverts its billing ledger entries",
        "Ask the user first; needs confirm=true.",  # the consistency rule of every destructive tool
    ):
        assert fragment in delete, fragment
    assert "Must be true to delete" in param("delete_time_entry", "confirm")


# Two 409s that Gorelo gives: create_time_entry when the ticket is closed and update_time_entry when the entry is invoiced.
# create_time_entry says the first one before the call, so the model stops asking Gorelo for it. update_time_entry and delete_time_entry already said the second one, so they got no new sentence.
CLOSED_TICKET_SENTENCE = (
    "Gorelo refuses time on a closed ticket (409): log time before closing, or ask the user before reopening it."
)
INVOICED_UPDATE_SENTENCE = "Approved, completed, invoiced and void entries are refused (409)."
INVOICED_DELETE_SENTENCE = (
    "Approved, completed, invoiced and void entries, and entries on a closed ticket (reopen it first), are refused (409)."
)


async def test_create_time_entry_carries_the_closed_ticket_409_fact_and_the_others_keep_their_invoiced_sentence(server_factory):
    listed = {t.name: t for t in await list_tools(server_factory(destructive=True))}
    text = {name: " ".join(tool.description.split()) for name, tool in listed.items()}
    create = text["create_time_entry"]
    assert create.count(CLOSED_TICKET_SENTENCE) == 1  # once: added, never stacked
    # the template order: what to know before the call comes before "Side effects:"
    assert create.index(CLOSED_TICKET_SENTENCE) < create.index("Side effects:")
    # no other tool repeats it (the default tool list has a byte budget)
    assert [name for name, tool_text in text.items() if CLOSED_TICKET_SENTENCE in tool_text] == ["create_time_entry"]
    # update and delete already said that an invoiced entry is refused (409), and got no second sentence for it
    assert text["update_time_entry"].count(INVOICED_UPDATE_SENTENCE) == 1
    assert text["delete_time_entry"].count(INVOICED_DELETE_SENTENCE) == 1
    for name in ("update_time_entry", "delete_time_entry"):
        assert "An invoiced entry cannot be" not in text[name], name
        assert text[name].count("invoiced") == 1, name
    # the delete text has its own words for a closed ticket, so the create sentence is not repeated there
    assert "ask the user before reopening it" not in text["delete_time_entry"]
    for name in ("create_time_entry", "update_time_entry", "delete_time_entry"):
        assert len(listed[name].description) <= DESCRIPTION_MAX, (name, len(listed[name].description))
        for param_name, schema in listed[name].inputSchema["properties"].items():
            assert len(schema["description"]) <= PARAM_DESCRIPTION_MAX, (name, param_name)


def test_the_closed_ticket_sentence_is_short():
    # the default tools/list has a measured byte budget (tests/test_integration.py): the sentence costs its length in every conversation
    assert len(CLOSED_TICKET_SENTENCE) <= 110
    assert EM_DASH not in CLOSED_TICKET_SENTENCE and EN_DASH not in CLOSED_TICKET_SENTENCE


def test_the_module_documents_the_two_409_refusals():
    doc = " ".join(time_entries.__doc__.split())
    assert "Two 409 refusals that Gorelo gives (plain refusals, Gorelo wrote nothing)" in doc
    assert "create_time_entry (" in doc and "update_time_entry (" in doc
    assert "times" not in doc.split("Two 409 refusals")[1].split("Shape of a record")[0]  # the module text carries no counts
    assert "Time entry cannot be created because ticket <id> is closed. Reopen the ticket first." in doc
    assert (
        "Time entry <id> cannot be changed because it has already been invoiced. "
        "Only a time entry that is still open can be changed."
    ) in doc
    assert "Gorelo's own 409 text still reaches the model unchanged" in doc
    assert "The descriptions of update_time_entry and delete_time_entry already say that an invoiced entry is refused (409)" in doc
    # the two Gorelo codes are kept by the tests (the tool modules carry no number of 3 or more digits in their text, see
    # tests/test_tools_tickets.py), so the module text does not repeat them
    assert "070205" not in doc and "070206" not in doc
    assert "The tests keep the Gorelo codes of those two answers." in doc


async def test_the_package_tool_list_stays_within_its_budget(server_factory):
    names = {spec.name for spec in REGISTRY.specs if spec.fn.__module__ in MODULE_GROUP}
    package = [t for t in await list_tools(server_factory(destructive=True)) if t.name in names]
    assert len(package) == len(names) == 14
    size = sum(len(json.dumps(t.model_dump(mode="json", exclude_none=True))) for t in package)
    assert size <= MODULE_BUDGET_BYTES, f"{size} bytes, budget {MODULE_BUDGET_BYTES}"


# --------------------------------------------------------------------------
# list_time_entries
# --------------------------------------------------------------------------


async def test_list_time_entries_sends_every_filter_under_its_spec_name(server, mock_gorelo, spec_index):
    mock_gorelo.on("GET", "/v1/time-entries", paged_envelope([entry(1), entry(2)], next_cursor="c2", total_count=7))
    result = await call_tool(
        server,
        "list_time_entries",
        {
            "client_ids": [11, 12],
            "location_ids": [21],
            "ticket_ids": [uid(1), uid(2).upper()],
            "task_ids": [uid(3)],
            "user_ids": [9201, 9202],
            "invoice_ids": [uid(4)],
            "started_since": "2026-10-01T00:00:00Z",
            "started_before": "2026-10-02T00:00:00+02:00",
            "created_since": "2026-09-01T00:00:00Z",
            "created_before": "2026-10-02T00:00:00Z",
            "updated_since": "2026-09-15T12:00:00-05:00",
            "updated_before": "2026-10-02T00:00:00Z",
            "page_size": 25,
            "cursor": "abc",
        },
    )
    request = mock_gorelo.last
    assert (request.method, request.path, request.json) == ("GET", "/v1/time-entries", None)
    assert request.query == {
        "ClientIds": "11,12",
        "LocationIds": "21",
        "TicketIds": f"{uid(1)},{uid(2)}",
        "TaskIds": uid(3),
        "UserIds": "9201,9202",
        "InvoiceIds": uid(4),
        "StartedSince": "2026-10-01T00:00:00Z",
        "StartedBefore": "2026-10-01T22:00:00Z",
        "CreatedSince": "2026-09-01T00:00:00Z",
        "CreatedBefore": "2026-10-02T00:00:00Z",
        "UpdatedSince": "2026-09-15T17:00:00Z",
        "UpdatedBefore": "2026-10-02T00:00:00Z",
        "PageSize": "25",
        "Cursor": "abc",
    }
    assert set(request.query) == set(spec_index.op("GET /v1/time-entries").query_params)
    assert result == {
        "items": [entry(1), entry(2)],
        "count": 2,
        "total_count": 7,
        "has_more": True,
        "next_cursor": "c2",
        "page_size": 25,
        "filters": {
            "client_ids": [11, 12],
            "location_ids": [21],
            "ticket_ids": [uid(1), uid(2)],
            "task_ids": [uid(3)],
            "user_ids": [9201, 9202],
            "invoice_ids": [uid(4)],
            "started_since": "2026-10-01T00:00:00Z",
            "started_before": "2026-10-01T22:00:00Z",
            "created_since": "2026-09-01T00:00:00Z",
            "created_before": "2026-10-02T00:00:00Z",
            "updated_since": "2026-09-15T17:00:00Z",
            "updated_before": "2026-10-02T00:00:00Z",
        },
    }


async def test_list_time_entries_with_no_filter_sends_only_the_default_page_size(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/time-entries", paged_envelope([]))
    result = await call_tool(server, "list_time_entries")
    assert mock_gorelo.last.query == {"PageSize": "100"}
    assert result == {
        "items": [], "count": 0, "total_count": 0, "has_more": False, "next_cursor": None,
        "page_size": 100, "filters": {},
    }


async def test_a_bare_32_digit_guid_is_sent_in_canonical_form(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/time-entries", paged_envelope([]))
    await call_tool(server, "list_time_entries", {"ticket_ids": [uid(1).replace("-", "")]})
    assert mock_gorelo.last.query["TicketIds"] == uid(1)


@pytest.mark.parametrize(
    "asked, used", [(0, 1), (-5, 1), (1, 1), (100, 100), (200, 200), (201, 200), (500, 200)]
)
async def test_the_page_size_is_clamped_and_reported(server, mock_gorelo, asked, used):
    mock_gorelo.on("GET", "/v1/time-entries", paged_envelope([entry(1)]))
    result = await call_tool(server, "list_time_entries", {"page_size": asked})
    assert mock_gorelo.last.query["PageSize"] == str(used)
    assert result["page_size"] == used


async def test_paging_follows_the_cursor_with_the_same_filters(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/time-entries", paged_responder([[entry(1)], [entry(2)]], total_count=2))
    filters = {"ticket_ids": [uid(1)], "page_size": 1}
    first = await call_tool(server, "list_time_entries", filters)
    assert first["has_more"] is True and first["next_cursor"] == "c1" and first["count"] == 1
    second = await call_tool(server, "list_time_entries", {**filters, "cursor": first["next_cursor"]})
    assert second["has_more"] is False and second["next_cursor"] is None and second["items"] == [entry(2)]
    assert "Cursor" not in mock_gorelo.requests[0].query
    assert mock_gorelo.requests[1].query == {"TicketIds": uid(1), "PageSize": "1", "Cursor": "c1"}
    assert second["filters"] == first["filters"] == {"ticket_ids": [uid(1)]}


async def test_an_empty_window_is_an_explicit_empty_page_not_a_silent_one(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/time-entries", paged_envelope([]))
    result = await call_tool(server, "list_time_entries", {"user_ids": [99999]})
    assert result["items"] == [] and result["count"] == 0 and result["total_count"] == 0
    assert result["filters"] == {"user_ids": [99999]} and result["has_more"] is False


@pytest.mark.parametrize(
    "property_name, param",
    [
        ("TicketIds", "ticket_ids"),
        ("InvoiceIds", "invoice_ids"),
        ("StartedSince", "started_since"),
        ("PageSize", "page_size"),
        ("Cursor", "cursor"),
    ],
)
async def test_a_gorelo_error_names_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    body = error_envelope(400, [("070101", "That value is not valid.", property_name)])
    mock_gorelo.on("GET", "/v1/time-entries", body)
    text = await call_tool_error(server, "list_time_entries", {})
    assert text == rejected("list_time_entries", 400, "070101", f"{param}: That value is not valid.")


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({"client_ids": []}, "client_ids: expected at least one id, got an empty list"),
        ({"client_ids": [0]}, "client_ids[0]: expected a positive whole number such as 123, got zero or a negative number"),
        ({"client_ids": [5, -1]}, "client_ids[1]: expected a positive whole number"),
        ({"client_ids": [MAX_ID + 1]}, "client_ids[0]: expected a positive whole number such as 123, got a number above"),
        ({"location_ids": [-4]}, "location_ids[0]: expected a positive whole number"),
        ({"user_ids": [0]}, "user_ids[0]: expected a positive whole number"),
        ({"ticket_ids": []}, "ticket_ids: expected at least one GUID, got an empty list"),
        ({"ticket_ids": ["TCK-4242"]}, "ticket_ids[0]: expected a GUID such as"),
        ({"task_ids": ["not-a-guid"]}, "task_ids[0]: expected a GUID"),
        ({"ticket_ids": ["{" + uid(1) + "}"]}, "ticket_ids[0]: expected a GUID"),
        ({"ticket_ids": ["urn:uuid:" + uid(1)]}, "ticket_ids[0]: expected a GUID"),
        ({"ticket_ids": [" " + uid(1)]}, "ticket_ids[0]: expected a GUID"),  # nothing is trimmed or guessed
        ({"ticket_ids": [uid(1), "TCK-4242"]}, "ticket_ids[1]: expected a GUID"),
        ({"task_ids": [""]}, "task_ids[0]: expected a GUID such as"),
        ({"invoice_ids": ["1001"]}, "invoice_ids[0]: expected a GUID"),
        ({"started_since": "2026-10-01T00:00:00"}, "started_since: '2026-10-01T00:00:00' has no UTC offset"),
        ({"started_before": "2026-10-01"}, "started_before: '2026-10-01' has no UTC offset"),
        ({"created_since": "yesterday"}, "created_since: 'yesterday' is not an ISO 8601 datetime"),
        ({"created_before": "  "}, "created_before: expected an ISO 8601 datetime"),
        ({"updated_since": "2026-13-45T00:00:00Z"}, "updated_since: '2026-13-45T00:00:00Z' is not an ISO 8601 datetime"),
        ({"updated_before": "tomorrow"}, "updated_before: 'tomorrow' is not an ISO 8601 datetime"),
        (
            {"started_since": "2026-10-02T00:00:00Z", "started_before": "2026-10-01T00:00:00Z"},
            "started_before: must be later than started_since",
        ),
        (
            {"created_since": "2026-10-01T00:00:00Z", "created_before": "2026-10-01T00:00:00Z"},
            "created_before: must be later than created_since",
        ),
        (
            {"updated_since": "2026-10-01T12:00:00+02:00", "updated_before": "2026-10-01T09:00:00Z"},
            "updated_before: must be later than updated_since",
        ),
        ({"cursor": "   "}, "cursor: must not be empty or whitespace only"),
        ({"client_ids": ["abc"]}, "client_ids"),
        ({"page_size": "many"}, "page_size"),
        ({"unknown_filter": 1}, "unknown_filter"),
    ],
)
async def test_list_time_entries_rejects_bad_input_before_any_http_call(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "list_time_entries", args)
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_a_window_is_compared_after_converting_to_utc(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/time-entries", paged_envelope([]))
    await call_tool(
        server,
        "list_time_entries",
        {"started_since": "2026-10-01T10:00:00+02:00", "started_before": "2026-10-01T09:00:00Z"},
    )
    assert mock_gorelo.last.query["StartedSince"] == "2026-10-01T08:00:00Z"
    assert mock_gorelo.last.query["StartedBefore"] == "2026-10-01T09:00:00Z"


async def test_windows_compare_fractions_of_a_second_as_instants_not_as_text(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/time-entries", paged_envelope([]))
    # 10:00:00 is before 10:00:00.5 although "...00Z" sorts after "...00.5Z" as text
    await call_tool(
        server,
        "list_time_entries",
        {"started_since": "2026-10-01T10:00:00Z", "started_before": "2026-10-01T10:00:00.500000+00:00"},
    )
    assert mock_gorelo.last.query["StartedBefore"] == "2026-10-01T10:00:00.500000Z"
    text = await call_tool_error(
        server,
        "list_time_entries",
        {"started_since": "2026-10-01T10:00:00.5Z", "started_before": "2026-10-01T10:00:00Z"},
    )
    assert "started_before: must be later than started_since" in text
    assert len(mock_gorelo.requests) == 1


async def test_a_shape_that_is_not_a_page_raises_instead_of_returning_nothing(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/time-entries", envelope({"Id": 1}))
    text = await call_tool_error(server, "list_time_entries", {})
    assert text.startswith("Gorelo returned an unexpected response for list_time_entries")
    assert "expected Data to be a list" in text


# --------------------------------------------------------------------------
# get_time_entry
# --------------------------------------------------------------------------


async def test_get_time_entry_returns_gorelos_record_unchanged(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/time-entries/501", envelope(entry(501)))
    result = await call_tool(server, "get_time_entry", {"time_entry_id": 501})
    assert result == entry(501)
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", "/v1/time-entries/501", {}, None)
    assert len(mock_gorelo.requests) == 1


async def test_get_time_entry_for_a_task_entry_passes_the_task_through(server, mock_gorelo):
    record = entry(502, Ticket=None, Task=reference(9, "T-7", "Wire the switch"), AdjustedHours=None)
    mock_gorelo.on("GET", "/v1/time-entries/502", envelope(record))
    result = await call_tool(server, "get_time_entry", {"time_entry_id": 502})
    assert result == record and result["Ticket"] is None
    assert result["Task"] == {"Id": uid(9), "Number": "T-7", "Title": "Wire the switch"}


def test_the_fixture_is_the_published_time_entry_model(spec_index):
    # a record has exactly the keys of the published TimeEntryModel, and the user, ticket and
    # task are the objects the spec names: User a CodeModel {Id, Name}, Ticket and Task NumberedReferenceModels
    # {Id, Number, Title}. (Task is null for a ticket entry: the spec says one of Ticket and Task is set, the other null.)
    model = spec_index.schema("TimeEntryModel")["fields"]
    record = entry()
    assert set(record) == set(model) and not {"UserId", "TicketId", "TaskId"} & set(record)
    kinds = {"User": "CodeModel", "Ticket": "NumberedReferenceModel", "Task": "NumberedReferenceModel"}
    assert {name: model[name]["ref"] for name in kinds} == kinds
    assert set(record["User"]) == set(spec_index.schema("CodeModel")["fields"]) == {"Id", "Name"}
    assert set(record["Ticket"]) == set(spec_index.schema("NumberedReferenceModel")["fields"]) == {"Id", "Number", "Title"}
    assert record["Task"] is None
    # the record of 2026-10-02 is the same entry with the three objects replaced by their flat ids
    flat = flat_entry()
    assert set(flat) == (set(model) - {"User", "Ticket", "Task"}) | {"UserId", "TicketId", "TaskId"}
    assert (flat["UserId"], flat["TicketId"], flat["TaskId"]) == (9201, uid(1), None)


@pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")
def test_the_record_shape_the_docs_describe_is_what_the_raw_spec_says():
    document = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]
    model = schemas["TimeEntryModel"]["properties"]
    assert {name: model[name]["$ref"].rsplit("/", 1)[-1] for name in ("User", "Ticket", "Task")} == {
        "User": "CodeModel",
        "Ticket": "NumberedReferenceModel",
        "Task": "NumberedReferenceModel",
    }
    assert not {"UserId", "TicketId", "TaskId"} & set(model)
    assert set(schemas["CodeModel"]["properties"]) == {"Id", "Name"}
    assert set(schemas["NumberedReferenceModel"]["properties"]) == {"Id", "Number", "Title"}
    listing = " ".join(document["paths"]["/v1/time-entries"]["get"]["description"].split())
    assert "one of `Ticket` and `Task` carries the id, number and title, and the other is null" in listing


async def test_list_and_get_pass_the_user_ticket_and_task_objects_through_unchanged(server, mock_gorelo):
    rows = [
        entry(1),
        entry(2, Ticket=None, Task=reference(2, "T-9", "Wire the switch"), User={"Id": 9202, "Name": "Sam Sample"}),
    ]
    mock_gorelo.on("GET", "/v1/time-entries", paged_envelope(rows))
    mock_gorelo.on("GET", "/v1/time-entries/2", envelope(rows[1]))
    listed = await call_tool(server, "list_time_entries")
    one = await call_tool(server, "get_time_entry", {"time_entry_id": 2})
    assert listed["items"] == rows and one == rows[1]
    for record in (*listed["items"], one):
        assert {"User", "Ticket", "Task"} <= set(record) and not {"UserId", "TicketId", "TaskId"} & set(record)
    shown = [(r["User"]["Id"], (r["Ticket"] or {}).get("Id"), (r["Task"] or {}).get("Id")) for r in listed["items"]]
    assert shown == [(9201, uid(1), None), (9202, None, uid(2))]


async def test_the_flat_ids_of_2026_10_02_pass_through_unchanged_too(server, mock_gorelo):
    # no code in the module reads either shape: a record reaches the caller exactly as Gorelo sent it
    rows = [flat_entry(1), flat_entry(2, TicketId=None, TaskId=uid(2), UserId=9202)]
    mock_gorelo.on("GET", "/v1/time-entries", paged_envelope(rows))
    mock_gorelo.on("GET", "/v1/time-entries/2", envelope(rows[1]))
    listed = await call_tool(server, "list_time_entries")
    one = await call_tool(server, "get_time_entry", {"time_entry_id": 2})
    assert listed["items"] == rows and one == rows[1]
    for record in (*listed["items"], one):
        assert {"UserId", "TicketId", "TaskId"} <= set(record) and not {"User", "Ticket", "Task"} & set(record)


async def test_the_list_and_get_descriptions_name_the_objects_and_the_flat_ids_of_2026_10_02(server_factory):
    shown = {t.name: " ".join(t.description.split()) for t in await list_tools(server_factory(destructive=True))}
    listing, one = shown["list_time_entries"], shown["get_time_entry"]
    assert (
        "Rows name their user, ticket and task as objects: User {Id, Name}, Ticket and Task {Id, Number, Title} "
        "(Task is null for a ticket entry). On 2026-10-02 the API returned flat UserId, TicketId and TaskId instead, "
        "so read the objects."
    ) in listing
    assert "the same record as a list_time_entries row, with User, Ticket and Task as objects." in one
    assert (
        "User is {Id, Name}; Ticket and Task are {Id, Number, Title} (Task is null for a ticket entry). "
        "On 2026-10-02 the API returned flat UserId, TicketId and TaskId instead, so read the objects."
    ) in one
    assert "A deleted entry is a 404, like an unknown id." in one
    for text in (listing, one):
        assert "(ids, not objects)" not in text and "with flat UserId, TicketId and TaskId" not in text  # replaced, not stacked


def test_the_module_documents_the_shape_of_a_record_and_what_it_did_not_probe():
    doc = " ".join(time_entries.__doc__.split())
    assert "Shape of a record (live probe 2026-10-03)" in doc and "return the published TimeEntryModel" in doc
    assert (
        "User {Id, Name}, Ticket {Id, Number, Title} and Task {Id, Number, Title}, with Task null for a ticket entry"
    ) in doc
    assert "There is no flat UserId, TicketId or TaskId." in doc
    assert "On 2026-10-02 the live API returned the flat ids UserId, TicketId and TaskId instead" in doc
    assert "so a reader keys on the objects" in doc
    assert "The tools hand records back unchanged and never read those fields, so no code here depends on either shape" in doc
    assert "The answer of PATCH was not probed for this" in doc
    assert "{Id, Name} objects of the published TimeEntryModel" not in doc  # Ticket and Task are {Id, Number, Title}


@pytest.mark.parametrize(
    "notification, detail",
    [
        (("070404", "Time entry not found."), "Time entry not found."),
        (("070404", "Time entry not found.", "timeEntryId"), "time_entry_id: Time entry not found."),
    ],
)
async def test_get_time_entry_reports_a_404(server, mock_gorelo, notification, detail):
    mock_gorelo.on("GET", "/v1/time-entries/9", error_envelope(404, [notification]))
    text = await call_tool_error(server, "get_time_entry", {"time_entry_id": 9})
    assert text == rejected("get_time_entry", 404, "070404", detail)


@pytest.mark.parametrize(
    "bad, what", [(0, "zero or a negative number"), (-1, "zero or a negative number"), (MAX_ID + 1, "a number above")]
)
async def test_get_time_entry_rejects_a_bad_id_by_name(server, mock_gorelo, bad, what):
    text = await call_tool_error(server, "get_time_entry", {"time_entry_id": bad})
    assert text.startswith(f"time_entry_id: expected a positive whole number such as 123, got {what}")
    assert mock_gorelo.requests == []


async def test_get_time_entry_takes_the_largest_gorelo_id(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/time-entries/{MAX_ID}", envelope(entry(7)))
    assert await call_tool(server, "get_time_entry", {"time_entry_id": MAX_ID}) == entry(7)


async def test_get_time_entry_needs_an_integer_id(server, mock_gorelo):
    text = await call_tool_error(server, "get_time_entry", {"time_entry_id": "abc"})
    assert "time_entry_id" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "body, fragment",
    [
        (envelope([entry(1)]), "expected Data to be a non-empty object but got a list of 1 item"),
        (envelope(True), "expected Data to be a non-empty object but got a boolean"),
        (envelope(False), "expected Data to be a non-empty object but got a boolean"),
        (envelope({}), "expected Data to be a non-empty object but got an empty object"),
        (envelope(None), "Gorelo reported success but Data is null"),
    ],
)
async def test_get_time_entry_refuses_an_answer_that_is_not_a_record(server, mock_gorelo, body, fragment):
    mock_gorelo.on("GET", "/v1/time-entries/5", body)
    text = await call_tool_error(server, "get_time_entry", {"time_entry_id": 5})
    assert text.startswith("Gorelo returned an unexpected response for get_time_entry") and fragment in text
    assert "refusing to guess" in text and "verify" not in text.lower()  # a read: nothing may have been applied


# --------------------------------------------------------------------------
# create_time_entry
# --------------------------------------------------------------------------

CREATE_BASE = {
    "user_id": 9201,
    "ticket_id": uid(1),
    "started_on": "2026-10-01T14:00:00Z",
    "ended_on": "2026-10-01T15:30:00Z",
}


def on_create(mock_gorelo, new_id=501, record=None):
    mock_gorelo.on("POST", "/v1/time-entries", envelope({"Id": new_id}))
    mock_gorelo.on("GET", f"/v1/time-entries/{new_id}", envelope(record if record is not None else entry(new_id)))


async def test_create_time_entry_sends_only_what_was_given_then_returns_the_reread_record(server, mock_gorelo):
    on_create(mock_gorelo)
    args = {
        "user_id": 9201,
        "ticket_id": uid(1),
        "started_on": "2026-10-01T09:00:00-05:00",
        "ended_on": "2026-10-01T10:30:00-05:00",
    }
    result = await call_tool(server, "create_time_entry", args)
    post, get = mock_gorelo.requests
    assert (post.method, post.path, post.query) == ("POST", "/v1/time-entries", {})
    assert post.json == {
        "TicketId": uid(1),
        "UserId": 9201,
        "StartedOn": "2026-10-01T14:00:00Z",
        "EndedOn": "2026-10-01T15:30:00Z",
    }
    assert (get.method, get.path, get.query, get.json) == ("GET", "/v1/time-entries/501", {}, None)
    assert result == entry(501)
    assert len(mock_gorelo.requests) == 2


async def test_no_default_is_invented_for_anything_the_caller_left_out(server, mock_gorelo):
    on_create(mock_gorelo)
    await call_tool(server, "create_time_entry", {"user_id": 9201, "ticket_id": uid(1), "actual_hours": 0.5, "started_on": "2026-10-01T14:00:00Z"})
    body = mock_gorelo.requests[0].json
    assert set(body) == {"TicketId", "UserId", "StartedOn", "ActualHours"}
    for left_out in ("ServiceLineId", "BillableStatusId", "BillingRoleId", "WorkTypeId", "Comment", "Distance", "Attachments"):
        assert left_out not in body


@pytest.mark.parametrize(
    "extra, expected_body",
    [
        ({"ended_on": "2026-10-01T15:30:00Z"}, {"StartedOn": "2026-10-01T14:00:00Z", "EndedOn": "2026-10-01T15:30:00Z"}),
        ({"actual_hours": 1.5}, {"StartedOn": "2026-10-01T14:00:00Z", "ActualHours": 1.5}),
    ],
)
async def test_start_plus_either_other_time_field_is_enough(server, mock_gorelo, extra, expected_body):
    on_create(mock_gorelo)
    args = {"user_id": 9201, "ticket_id": uid(1), "started_on": "2026-10-01T14:00:00Z", **extra}
    await call_tool(server, "create_time_entry", args)
    assert mock_gorelo.requests[0].json == {"TicketId": uid(1), "UserId": 9201, **expected_body}


async def test_end_plus_hours_is_enough(server, mock_gorelo):
    on_create(mock_gorelo)
    await call_tool(server, "create_time_entry", {"user_id": 9201, "ticket_id": uid(1), "ended_on": "2026-10-01T15:30:00Z", "actual_hours": 2})
    assert mock_gorelo.requests[0].json == {
        "TicketId": uid(1), "UserId": 9201, "EndedOn": "2026-10-01T15:30:00Z", "ActualHours": 2,
    }


async def test_all_three_time_fields_are_sent_and_left_for_gorelo_to_check(server, mock_gorelo):
    on_create(mock_gorelo)
    args = make_args(CREATE_BASE, actual_hours=3.0)  # does NOT agree with the 1.5 hour span: Gorelo decides
    await call_tool(server, "create_time_entry", args)
    assert mock_gorelo.requests[0].json["ActualHours"] == 3.0
    assert mock_gorelo.requests[0].json["StartedOn"] == "2026-10-01T14:00:00Z"
    assert mock_gorelo.requests[0].json["EndedOn"] == "2026-10-01T15:30:00Z"


async def test_a_task_entry_sends_task_id_and_no_ticket_id(server, mock_gorelo):
    on_create(
        mock_gorelo,
        503,
        entry(503, Ticket=None, Task=reference(2, "T-9", "Wire the switch"), User={"Id": 9202, "Name": "Sam Sample"}),
    )
    result = await call_tool(
        server, "create_time_entry", {"user_id": 9202, "task_id": uid(2), "started_on": "2026-10-01T14:00:00Z", "actual_hours": 0.75}
    )
    assert mock_gorelo.requests[0].json == {
        "TaskId": uid(2), "UserId": 9202, "StartedOn": "2026-10-01T14:00:00Z", "ActualHours": 0.75,
    }
    assert result["Task"]["Id"] == uid(2) and result["Ticket"] is None and result["User"]["Id"] == 9202


async def test_every_field_goes_out_under_its_pascal_case_name(server, mock_gorelo, spec_index):
    on_create(mock_gorelo)
    args = {
        **CREATE_BASE,
        "actual_hours": 1.5,
        "billable_status_id": 2,
        "billing_role_id": 3,
        "work_type_id": 4,
        "service_line_id": 77,
        "comment": "Swapped the toner",
        "distance": 12.5,
        "attachments": [{"name": "report.pdf", "url": "https://files.example.test/report.pdf"}],
    }
    await call_tool(server, "create_time_entry", args)
    body = mock_gorelo.requests[0].json
    assert body == {
        "TicketId": uid(1),
        "UserId": 9201,
        "StartedOn": "2026-10-01T14:00:00Z",
        "EndedOn": "2026-10-01T15:30:00Z",
        "ActualHours": 1.5,
        "BillableStatusId": 2,
        "BillingRoleId": 3,
        "WorkTypeId": 4,
        "ServiceLineId": 77,
        "Comment": "Swapped the toner",
        "Distance": 12.5,
        "Attachments": [{"Name": "report.pdf", "Url": "https://files.example.test/report.pdf"}],
    }
    # every spec field is reachable (TaskId through the task variant) except the deliberately unexposed ones
    assert set(body) | {"TaskId"} | set(UNEXPOSED_CREATE_FIELDS) == set(spec_index.op("POST /v1/time-entries").body["fields"])
    assert not set(body) & set(UNEXPOSED_CREATE_FIELDS)


@pytest.mark.parametrize("status", [1, 2, 3])
async def test_billable_status_ids_one_to_three_are_accepted(server, mock_gorelo, status):
    on_create(mock_gorelo)
    await call_tool(server, "create_time_entry", make_args(CREATE_BASE, billable_status_id=status))
    assert mock_gorelo.requests[0].json["BillableStatusId"] == status


async def test_a_zero_distance_is_sent_not_dropped(server, mock_gorelo):
    on_create(mock_gorelo)
    await call_tool(server, "create_time_entry", make_args(CREATE_BASE, distance=0))
    assert mock_gorelo.requests[0].json["Distance"] == 0


async def test_no_service_line_sends_an_explicit_null(server, mock_gorelo):
    on_create(mock_gorelo)
    await call_tool(server, "create_time_entry", make_args(CREATE_BASE, no_service_line=True))
    body = mock_gorelo.requests[0].json
    assert "ServiceLineId" in body and body["ServiceLineId"] is None
    assert set(body) == {"TicketId", "UserId", "StartedOn", "EndedOn", "ServiceLineId"}
    assert b'"ServiceLineId":null' in mock_gorelo.requests[0].content.replace(b" ", b"")


async def test_no_service_line_false_sends_nothing_about_the_service_line(server, mock_gorelo):
    on_create(mock_gorelo)
    await call_tool(server, "create_time_entry", make_args(CREATE_BASE, no_service_line=False))
    assert "ServiceLineId" not in mock_gorelo.requests[0].json


async def test_an_explicit_service_line_is_sent_as_its_id(server, mock_gorelo):
    on_create(mock_gorelo)
    await call_tool(server, "create_time_entry", make_args(CREATE_BASE, service_line_id=77))
    assert mock_gorelo.requests[0].json["ServiceLineId"] == 77


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"ticket_id": DROP}, "ticket_id and task_id: give exactly one of them"),
        ({"ticket_id": DROP, "task_id": DROP}, "got neither"),
        ({"task_id": uid(2)}, "got both"),
        ({"ticket_id": DROP, "task_id": "123"}, "task_id: expected a GUID"),
        ({"ticket_id": ""}, "ticket_id: must not be empty; give a ticket GUID, a number such as 1234"),
        ({"ticket_id": "   "}, "ticket_id: must not be empty"),
        ({"ticket_id": 0}, "ticket_id: expected a positive whole number"),
        ({"ticket_id": "T" * 201}, "ticket_id: at most 200 characters, got 201"),
        ({"auto_service_line": True, "service_line_id": 77}, "auto_service_line: cannot be true together with service_line_id or no_service_line"),
        ({"auto_service_line": True, "no_service_line": True}, "auto_service_line: cannot be true together with service_line_id or no_service_line"),
        (
            {"auto_service_line": True, "ticket_id": DROP, "task_id": uid(2)},
            "auto_service_line: needs ticket_id (the client is read from the ticket), so it cannot be used for a task entry",
        ),
        ({"auto_service_line": True, "task_id": uid(2)}, "ticket_id and task_id: give exactly one of them"),
        ({"auto_service_line": True, "ticket_id": DROP}, "ticket_id and task_id: give exactly one of them"),
        ({"auto_service_line": "true"}, "auto_service_line"),
        ({"auto_service_line": 1}, "auto_service_line"),
        ({"user_id": 0}, "user_id: expected a positive whole number such as 123, got zero or a negative number"),
        ({"user_id": -5}, "user_id: expected a positive whole number"),
        ({"user_id": MAX_ID + 1}, "user_id: expected a positive whole number such as 123, got a number above"),
        ({"started_on": DROP, "ended_on": DROP}, "got none of them"),
        ({"ended_on": DROP}, "got only started_on"),
        ({"started_on": DROP}, "got only ended_on"),
        ({"started_on": DROP, "ended_on": DROP, "actual_hours": 2}, "got only actual_hours"),
        ({"started_on": "2026-10-01T14:00:00"}, "started_on: '2026-10-01T14:00:00' has no UTC offset"),
        ({"ended_on": "2026-10-01"}, "ended_on: '2026-10-01' has no UTC offset"),
        ({"ended_on": "soon"}, "ended_on: 'soon' is not an ISO 8601 datetime"),
        ({"ended_on": "2026-10-01T13:00:00Z"}, "ended_on: must be later than started_on"),
        ({"ended_on": "2026-10-01T14:00:00Z"}, "ended_on: must be later than started_on"),
        ({"ended_on": "2026-10-01T09:30:00-05:00", "started_on": "2026-10-01T15:00:00Z"}, "ended_on: must be later than started_on"),
        ({"actual_hours": 0}, "Input should be greater than 0"),
        ({"actual_hours": -1.5}, "Input should be greater than 0"),
        ({"billable_status_id": 0}, "billable_status_id: must be one of 1 (Billable), 2 (No charge), 3 (Non-billable), got 0"),
        ({"billable_status_id": 4}, "billable_status_id: must be one of 1 (Billable), 2 (No charge), 3 (Non-billable), got 4"),
        ({"billing_role_id": 0}, "billing_role_id: expected a positive whole number"),
        ({"billing_role_id": MAX_ID + 1}, "billing_role_id: expected a positive whole number"),
        ({"work_type_id": -2}, "work_type_id: expected a positive whole number"),
        ({"work_type_id": MAX_ID + 1}, "work_type_id: expected a positive whole number"),
        ({"service_line_id": 0}, "service_line_id: expected a positive whole number"),
        ({"service_line_id": MAX_ID + 1}, "service_line_id: expected a positive whole number"),
        ({"service_line_id": 77, "no_service_line": True}, "no_service_line: cannot be true together with service_line_id"),
        ({"comment": ""}, "comment: must not be empty or whitespace only"),
        ({"comment": "   "}, "comment: must not be empty or whitespace only"),
        ({"distance": -1}, "Input should be greater than or equal to 0"),
        ({"attachments": []}, "attachments: expected a non-empty list of objects"),
        ({"attachments": [{"name": "a.txt"}]}, "attachments[0].url: required, and must be non-empty text"),
        ({"attachments": [{"url": "https://x.example.test/a"}]}, "attachments[0].name: required, and must be non-empty text"),
        ({"attachments": [{"name": " ", "url": "https://x.example.test/a"}]}, "attachments[0].name: required"),
        (
            {"attachments": [{"name": "a", "url": "https://x.example.test/a"}, {"name": "b", "url": ""}]},
            "attachments[1].url: required",
        ),
        (
            {"attachments": [{"name": "a", "url": "https://x.example.test/a", "size": "5"}]},
            'attachments[0]: unknown key(s) size; only "name" and "url" are accepted',
        ),
        (
            {"attachments": [{"name": "n" * 5000, "url": "https://x.example.test/a"}]},
            "attachments: the serialised list is",
        ),
    ],
)
async def test_create_time_entry_rejects_bad_input_before_any_http_call(server, mock_gorelo, overrides, fragment):
    text = await call_tool_error(server, "create_time_entry", make_args(CREATE_BASE, **overrides))
    assert fragment in text
    if "Input should be" in fragment:  # the bound is the schema's: the message names the parameter
        assert next(iter(overrides)) in text
    assert mock_gorelo.requests == []


async def test_create_time_entry_needs_a_user(server, mock_gorelo):
    text = await call_tool_error(server, "create_time_entry", make_args(CREATE_BASE, user_id=DROP))
    assert "user_id" in text and "Missing required argument" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("unknown", [{"client_id": 5}, {"ServiceLineId": 5}, {"billable": True}])
async def test_create_time_entry_refuses_a_parameter_it_does_not_have(server, mock_gorelo, unknown):
    text = await call_tool_error(server, "create_time_entry", {**CREATE_BASE, **unknown})
    assert next(iter(unknown)) in text
    assert mock_gorelo.requests == []


async def test_the_attachment_size_check_accepts_a_list_just_under_the_limit(server, mock_gorelo):
    on_create(mock_gorelo)
    # {"Name":"<n>","Url":"u"} costs 21 characters of structure plus the two values; stay a little under 5000
    attachments = [{"name": "n" * 4000, "url": "u" * 900}]
    await call_tool(server, "create_time_entry", make_args(CREATE_BASE, attachments=attachments))
    assert mock_gorelo.requests[0].json["Attachments"] == [{"Name": "n" * 4000, "Url": "u" * 900}]


@pytest.mark.parametrize(
    "notification, detail",
    [
        (("070101", "The service line does not belong to the ticket's client.", "ServiceLineId"),
         "service_line_id: The service line does not belong to the ticket's client."),
        (("070101", "ActualHours does not match the span between StartedOn and EndedOn.", "ActualHours"),
         "actual_hours: ActualHours does not match the span between StartedOn and EndedOn."),
        (("070101", "Unknown user.", "UserId"), "user_id: Unknown user."),
        (("070101", "Unknown ticket.", "TicketId"), "ticket_id: Unknown ticket."),
        (("070101", "Unknown task.", "TaskId"), "task_id: Unknown task."),
        (("070101", "Invalid billable status.", "BillableStatusId"), "billable_status_id: Invalid billable status."),
        (("070101", "Unknown role.", "BillingRoleId"), "billing_role_id: Unknown role."),
        (("070101", "Unknown work type.", "WorkTypeId"), "work_type_id: Unknown work type."),
        (("070101", "Too long.", "Attachments"), "attachments: Too long."),
        (("070101", "Bad url.", "Attachments[0].Url"), "attachments (item 1, Url): Bad url."),
        (("070101", "Bad instant.", "StartedOn"), "started_on: Bad instant."),
        (("070101", "Bad instant.", "EndedOn"), "ended_on: Bad instant."),
        (("070101", "Too far.", "Distance"), "distance: Too far."),
    ],
)
async def test_create_time_entry_maps_a_gorelo_400_to_the_snake_case_parameter(server, mock_gorelo, notification, detail):
    mock_gorelo.on("POST", "/v1/time-entries", error_envelope(400, [notification]))
    text = await call_tool_error(server, "create_time_entry", CREATE_BASE)
    assert text == rejected("create_time_entry", 400, "070101", detail)
    assert len(mock_gorelo.requests) == 1  # no re-read, no retry


@pytest.mark.parametrize(
    "message",
    [
        "A closed ticket cannot take time.",
        "The ticket has no client.",
        "The client is on credit hold.",
    ],
)
async def test_create_time_entry_reports_a_409_with_gorelos_reason(server, mock_gorelo, message):
    mock_gorelo.on("POST", "/v1/time-entries", error_envelope(409, [("070901", message)]))
    text = await call_tool_error(server, "create_time_entry", CREATE_BASE)
    assert text == rejected("create_time_entry", 409, "070901", message)
    assert len(mock_gorelo.requests) == 1


async def test_create_time_entry_shows_the_409_for_a_closed_ticket_as_a_plain_refusal(server, mock_gorelo):
    # Gorelo's own answer for a closed ticket (the ticket id here is invented)
    message = f"Time entry cannot be created because ticket {uid(1)} is closed. Reopen the ticket first."
    mock_gorelo.on("POST", "/v1/time-entries", error_envelope(409, [("070205", message)]))
    text = await call_tool_error(server, "create_time_entry", CREATE_BASE)
    assert text == rejected("create_time_entry", 409, "070205", message)
    # Gorelo refused, so nothing was written: no "verify with a read" advice, no retry, no re-read
    assert "may have been applied" not in text and "Verify" not in text and "did not confirm" not in text
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("POST", "/v1/time-entries")]


async def test_create_time_entry_reports_an_unknown_ticket_404(server, mock_gorelo):
    mock_gorelo.on("POST", "/v1/time-entries", error_envelope(404, [("070404", "Ticket not found.", "TicketId")]))
    text = await call_tool_error(server, "create_time_entry", CREATE_BASE)
    assert text == rejected("create_time_entry", 404, "070404", "ticket_id: Ticket not found.")


async def test_a_timeout_on_the_post_says_gorelo_did_not_confirm_and_nothing_is_repeated(server, mock_gorelo):
    mock_gorelo.on("POST", "/v1/time-entries", httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "create_time_entry", CREATE_BASE)
    assert text.startswith("Gorelo did not confirm create_time_entry (the request timed out).")
    assert "Verify with a read before retrying." in text
    assert [r.method for r in mock_gorelo.requests] == ["POST"]


async def test_a_5xx_on_the_post_is_marked_as_possibly_applied(server, mock_gorelo):
    mock_gorelo.on("POST", "/v1/time-entries", error_envelope(500, [("070001", "Internal error.")]))
    text = await call_tool_error(server, "create_time_entry", CREATE_BASE)
    assert text.startswith("Gorelo rejected create_time_entry (HTTP 500, code 070001): Internal error.")
    assert "Gorelo may have applied the change before failing. Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "reread, reason",
    [
        (
            error_envelope(500, [("070001", "Internal error.")]),
            f"GET /v1/time-entries/{{timeEntryId}} answered HTTP 500 (code 070001): Internal error. [trace {TEST_TRACE_ID}]",
        ),
        (
            error_envelope(404, [("070404", "Time entry not found.")]),
            f"GET /v1/time-entries/{{timeEntryId}} answered HTTP 404 (code 070404): Time entry not found. [trace {TEST_TRACE_ID}]",
        ),
        (httpx.ReadTimeout("slow"), "GET /v1/time-entries/{timeEntryId} timed out"),
        (envelope(None), "GET /v1/time-entries/{timeEntryId}: Gorelo reported success but Data is null; refusing to guess"),
    ],
)
async def test_a_failed_reread_returns_the_id_and_a_warning_and_never_repeats_the_write(
    server, mock_gorelo, reread, reason
):
    mock_gorelo.on("POST", "/v1/time-entries", envelope({"Id": 777}))
    mock_gorelo.on("GET", "/v1/time-entries/777", reread)
    result = await call_tool(server, "create_time_entry", CREATE_BASE)
    assert result == {
        "Id": 777,
        "warning": f"the write succeeded; re-reading it failed: {reason}. Do not repeat the write; read it again later.",
    }
    assert len(mock_gorelo.calls("POST")) == 1 and len(mock_gorelo.calls("GET")) == 1


@pytest.mark.parametrize(
    "answer, problem",
    [
        (envelope({}), "Data is an object without an Id"),
        (envelope(None), "Data is null, not an object with an Id"),
        (envelope(True), "Data is a boolean, not an object with an Id"),
        (envelope(False), "Data is a boolean, not an object with an Id"),
        (envelope([{"Id": 1}]), "Data is a list of 1 item, not an object with an Id"),
        (envelope({"Id": None}), "Data.Id is null"),
        (envelope({"Id": 0}), "Data.Id is zero or negative"),
        (envelope({"Id": True}), "Data.Id is a boolean"),
        (envelope({"Id": ""}), "Data.Id is blank"),
    ],
)
async def test_a_post_answer_without_a_usable_id_raises_and_tells_the_model_to_verify(
    server, mock_gorelo, answer, problem
):
    mock_gorelo.on("POST", "/v1/time-entries", answer)
    text = await call_tool_error(server, "create_time_entry", CREATE_BASE)
    assert text.startswith("Gorelo returned an unexpected response for create_time_entry: POST /v1/time-entries: ")
    assert "Gorelo reported success but the answer carries no usable Id for the record" in text and problem in text
    assert VERIFY_AFTER_SHAPE in text and text.lower().count("verify") == 1
    assert [r.method for r in mock_gorelo.requests] == ["POST"]  # never repeated, never read back


async def test_the_reread_happens_after_the_post_with_the_id_gorelo_returned(server, mock_gorelo):
    mock_gorelo.on("POST", "/v1/time-entries", envelope({"Id": 4242}))
    mock_gorelo.on("GET", "/v1/time-entries/4242", envelope(entry(4242, Comment="the record")))
    result = await call_tool(server, "create_time_entry", CREATE_BASE)
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [
        ("POST", "/v1/time-entries"),
        ("GET", "/v1/time-entries/4242"),
    ]
    assert result["Id"] == 4242 and result["Comment"] == "the record"


# --------------------------------------------------------------------------
# create_time_entry: ticket_id as a GUID, a number or a display number (1.7.0)
# --------------------------------------------------------------------------


def ticket_row(n=1, number=1234, display=None):
    """A GET /v1/tickets row, reduced to the fields the resolver reads."""
    return {"Id": uid(n), "Number": number, "DisplayNumber": display or f"TCK-{number}", "Title": "Printer offline"}


def request_log(mock_gorelo):
    return [(r.method, r.path) for r in mock_gorelo.requests]


async def test_a_guid_ticket_id_costs_no_lookup_request(server, mock_gorelo):
    on_create(mock_gorelo)
    await call_tool(server, "create_time_entry", CREATE_BASE)
    assert request_log(mock_gorelo) == [("POST", "/v1/time-entries"), ("GET", "/v1/time-entries/501")]


@pytest.mark.parametrize("given", [uid(1).upper(), uid(1).replace("-", ""), " " + uid(1) + " "])
async def test_a_guid_in_any_spelling_is_sent_canonical_with_no_lookup(server, mock_gorelo, given):
    on_create(mock_gorelo)
    await call_tool(server, "create_time_entry", make_args(CREATE_BASE, ticket_id=given))
    assert mock_gorelo.calls("POST")[0].json["TicketId"] == uid(1)
    assert mock_gorelo.calls("GET", "/v1/tickets") == []


@pytest.mark.parametrize("given, query", [("TCK-1234", "TCK-1234"), (1234, "1234"), ("tck-1234", "tck-1234"), ("1234", "1234")])
async def test_a_display_number_or_a_number_is_looked_up_before_the_post(server, mock_gorelo, given, query):
    mock_gorelo.on("GET", "/v1/tickets", paged_envelope([ticket_row(1, 1234), ticket_row(2, 12340)]))
    on_create(mock_gorelo)
    await call_tool(server, "create_time_entry", make_args(CREATE_BASE, ticket_id=given))
    assert request_log(mock_gorelo) == [
        ("GET", "/v1/tickets"),
        ("POST", "/v1/time-entries"),
        ("GET", "/v1/time-entries/501"),
    ]
    lookup = mock_gorelo.calls("GET", "/v1/tickets")[0]
    assert lookup.query["Query"] == query
    assert mock_gorelo.calls("POST")[0].json["TicketId"] == uid(1)


@pytest.mark.parametrize(
    "rows, fragment",
    [
        ([], "ticket_id: no ticket has the number or display number 'TCK-1234' (Gorelo found no ticket matching that text)"),
        ([ticket_row(2, 12340)], "none with that exact number"),
        ([ticket_row(1, 1234), ticket_row(2, 1234)], "ticket_id: 'TCK-1234' matches several tickets"),
    ],
)
async def test_an_unknown_or_ambiguous_ticket_reference_is_refused_with_no_post(server, mock_gorelo, rows, fragment):
    mock_gorelo.on("GET", "/v1/tickets", paged_envelope(rows))
    text = await call_tool_error(server, "create_time_entry", make_args(CREATE_BASE, ticket_id="TCK-1234"))
    assert fragment in text
    assert request_log(mock_gorelo) == [("GET", "/v1/tickets")]


async def test_a_failed_lookup_is_refused_with_no_post(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/tickets", error_envelope(500, [("070001", "Internal error.")]))
    text = await call_tool_error(server, "create_time_entry", make_args(CREATE_BASE, ticket_id="TCK-1234"))
    assert "Gorelo rejected create_time_entry (HTTP 500" in text
    assert mock_gorelo.calls("POST") == []


async def test_a_task_entry_makes_no_lookup(server, mock_gorelo):
    on_create(mock_gorelo)
    await call_tool(server, "create_time_entry", make_args(CREATE_BASE, ticket_id=DROP, task_id=uid(2)))
    assert request_log(mock_gorelo) == [("POST", "/v1/time-entries"), ("GET", "/v1/time-entries/501")]


async def test_the_ticket_id_filter_of_the_list_stays_guid_only(server, mock_gorelo):
    text = await call_tool_error(server, "list_time_entries", {"ticket_ids": ["TCK-1234"]})
    assert "ticket_ids[0]: expected a GUID" in text
    assert mock_gorelo.requests == []


async def test_the_ticket_id_parameter_says_what_it_takes(server_factory):
    listed = {t.name: t for t in await list_tools(server_factory())}
    properties = listed["create_time_entry"].inputSchema["properties"]
    text = properties["ticket_id"]["description"]
    assert "GUID" in text and "TCK-1234" in text and "1234" in text and "list_tickets" in text
    assert "ticket_ids" in listed["list_time_entries"].inputSchema["properties"]
    assert "not display numbers" in listed["list_time_entries"].inputSchema["properties"]["ticket_ids"]["description"]


# --------------------------------------------------------------------------
# create_time_entry: auto_service_line (1.7.0)
# --------------------------------------------------------------------------

AUTO_BASE = {**CREATE_BASE, "auto_service_line": True}
TICKET_PATH = f"/v1/tickets/{uid(1)}"
NO_HOURLY_SENT = "auto_service_line found no Hourly service line for this ticket, so none was sent"
REFUSED_AS_SHAPE = "refusing to guess, so nothing was written"
LOST = (
    "service line {sent} was sent but the entry reads back with {shown}; check the entry in Gorelo before it is invoiced; "
    "do not log the time again."
)
# a record with no ServiceLine KEY is not "no service line": no claim about the line or the rate, a warning that it cannot be checked
FIELD_MISSING = "The answer carries no ServiceLine field, so the service line could not be checked; check the entry with get_time_entry."
FIELD_MISSING_SENT = (
    "service line {sent} was sent but the answer carries no ServiceLine field, so it could not be checked; check the entry "
    "with get_time_entry before it is invoiced; do not log the time again."
)


def line(line_id, name="Remote support", terms=2, terms_name=None):
    """A ContractServiceLineModel, reduced: Id, Name and LaborTerms {Id, Name}."""
    labels = {1: "Unlimited Hours", 2: "Hourly", 3: "Block Hours", 4: "No Labor Terms", 5: "Limited Hours"}
    return {
        "Id": line_id,
        "Name": name,
        "LaborTerms": {"Id": terms, "Name": terms_name if terms_name is not None else labels[terms]},
        "LaborRates": {"Id": 1, "Name": "Use default labor rates"},
    }


def contract(contract_id, lines, name=None, **fields):
    """A ContractModel, reduced to what auto_service_line reads. By default: Active, for all locations, started long ago, no end."""
    row = {
        "Id": contract_id,
        "ClientId": 5001,
        "Name": name or f"Contract {contract_id}",
        "Status": {"Id": 1, "Name": "Active"},
        "IsForAllLocations": True,
        "LocationIds": [],
        "StartDate": "2020-01-01",
        "EndDate": None,
        "ServiceLines": lines,
    }
    for key, value in fields.items():
        if value is DROP:
            row.pop(key, None)
        else:
            row[key] = value
    return row


def scoped(contract_id, lines, location_ids, name=None, **fields):
    """A contract that covers only the given locations."""
    return contract(contract_id, lines, name, IsForAllLocations=False, LocationIds=list(location_ids), **fields)


def ticket_detail(client_id=5001, location_id=None, own_line=None, **overrides):
    """A TicketDetailModel, reduced to the fields auto_service_line reads. BillingOverride is always an object, as live."""
    override = {"ServiceLine": own_line, "BillingRole": None, "WorkType": None, "BillableStatus": None}
    detail = {"Id": uid(1), "ClientId": client_id, "LocationId": location_id, "BillingOverride": override}
    for key, value in overrides.items():
        if value is DROP:
            detail.pop(key, None)
        else:
            detail[key] = value
    return detail


def on_auto(mock_gorelo, contracts, **ticket):
    """The reads of auto_service_line: the ticket and the client's contracts (a list of pages, or one page)."""
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail(**ticket)))
    pages = contracts if contracts and isinstance(contracts[0], list) else [contracts]
    mock_gorelo.on("GET", "/v1/contracts", paged_responder(pages))


def landed(line_id, name="Remote support", **overrides):
    """The entry as Gorelo reads it back when the service line stuck."""
    return entry(501, ServiceLine={"Id": line_id, "Name": name}, **overrides)


async def auto_picks(server, mock_gorelo, contracts, expected, *, args=None, **ticket):
    """Run auto_service_line against these contracts and check the POST carried exactly `expected` as ServiceLineId."""
    on_auto(mock_gorelo, contracts, **ticket)
    on_create(mock_gorelo, record=landed(expected))
    result = await call_tool(server, "create_time_entry", args or AUTO_BASE)
    assert mock_gorelo.calls("POST")[0].json["ServiceLineId"] == expected
    return result


async def auto_sends_none(server, mock_gorelo, contracts, *, args=None, record=None, **ticket):
    """Run auto_service_line where no line is found; the POST must carry no ServiceLineId. Returns the result."""
    on_auto(mock_gorelo, contracts, **ticket)
    on_create(mock_gorelo, record=record if record is not None else entry(501, ServiceLine=None))
    result = await call_tool(server, "create_time_entry", args or AUTO_BASE)
    assert "ServiceLineId" not in mock_gorelo.calls("POST")[0].json
    return result


async def auto_refused(server, mock_gorelo, contracts=(), *, args=None, reads=None, **ticket):
    """Run auto_service_line where it must refuse: the error text comes back, and no POST was made."""
    on_auto(mock_gorelo, list(contracts), **ticket)
    text = await call_tool_error(server, "create_time_entry", args or AUTO_BASE)
    assert mock_gorelo.calls("POST") == []
    if reads is not None:
        assert request_log(mock_gorelo) == reads
    return text


async def test_auto_service_line_sends_the_one_hourly_line_after_reading_the_ticket_and_the_contracts(server, mock_gorelo):
    on_auto(
        mock_gorelo,
        [contract(10, [line(900, "Unlimited care", terms=1), line(901, "Hourly support")]), contract(11, [line(902, "Blocks", 3)])],
    )
    on_create(mock_gorelo, record=landed(901, "Hourly support"))
    result = await call_tool(server, "create_time_entry", AUTO_BASE)
    assert request_log(mock_gorelo) == [
        ("GET", TICKET_PATH),
        ("GET", "/v1/contracts"),
        ("POST", "/v1/time-entries"),
        ("GET", "/v1/time-entries/501"),
    ]
    contracts = mock_gorelo.calls("GET", "/v1/contracts")[0]
    assert contracts.query == {"ClientIds": "5001", "StatusIds": "1", "PageSize": "200"}
    post = mock_gorelo.calls("POST")[0].json
    assert post["ServiceLineId"] == 901 and post["TicketId"] == uid(1)
    # the line stuck, so there is no warning; the two other labor lines beside the Hourly one are named in a note
    assert result == {
        **landed(901, "Hourly support"),
        "note": (
            "auto_service_line sent the Hourly line 'Hourly support' (Id 901), but the covering contracts also have 2 other "
            "labor lines: 'Unlimited care' (Unlimited Hours); 'Blocks' (Block Hours). Gorelo may have picked one of those on "
            "its own; service_line_id picks another line, and leaving out auto_service_line lets Gorelo choose."
        ),
    }


async def test_auto_service_line_is_sent_like_an_explicit_service_line_id(server, mock_gorelo):
    on_auto(mock_gorelo, [contract(10, [line(901)])])
    on_create(mock_gorelo, record=landed(901))
    await call_tool(server, "create_time_entry", AUTO_BASE)
    auto_body = mock_gorelo.calls("POST")[0].json
    mock_gorelo.reset()
    on_create(mock_gorelo, record=landed(901))
    await call_tool(server, "create_time_entry", {**CREATE_BASE, "service_line_id": 901})
    assert mock_gorelo.calls("POST")[0].json == auto_body


async def test_auto_service_line_reads_every_page_of_contracts(server, mock_gorelo):
    await auto_picks(
        server, mock_gorelo, [[contract(10, [line(900, terms=1)])], [contract(11, [line(901, "Late page")])]], 901
    )
    assert len(mock_gorelo.calls("GET", "/v1/contracts")) == 2


async def test_auto_service_line_with_a_display_number_resolves_reads_then_posts(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/tickets", paged_envelope([ticket_row(1, 1234)]))
    await auto_picks(server, mock_gorelo, [contract(10, [line(901)])], 901, args={**AUTO_BASE, "ticket_id": "TCK-1234"})
    assert request_log(mock_gorelo) == [
        ("GET", "/v1/tickets"),
        ("GET", TICKET_PATH),
        ("GET", "/v1/contracts"),
        ("POST", "/v1/time-entries"),
        ("GET", "/v1/time-entries/501"),
    ]


async def test_auto_service_line_with_a_guid_makes_no_ticket_lookup(server, mock_gorelo):
    await auto_picks(server, mock_gorelo, [contract(10, [line(901)])], 901)
    assert mock_gorelo.calls("GET", "/v1/tickets") == []


async def test_the_same_line_seen_twice_is_one_candidate(server, mock_gorelo):
    await auto_picks(server, mock_gorelo, [[contract(10, [line(901)])], [contract(10, [line(901)])]], 901)


async def test_hourly_is_the_labor_terms_code_not_the_name(server, mock_gorelo):
    # code 2 with another name counts, the name "Hourly" on another code does not
    contracts = [contract(10, [line(901, terms=2, terms_name="Time and materials"), line(902, terms=3, terms_name="Hourly")])]
    await auto_picks(server, mock_gorelo, contracts, 901)


async def test_several_hourly_lines_are_refused_and_listed_with_no_post(server, mock_gorelo):
    text = await auto_refused(
        server,
        mock_gorelo,
        [
            contract(10, [line(901, "Remote support")], name="Managed Services"),
            contract(11, [line(902, "Onsite", 2), line(903, "Blocks", 3)], name="Projects"),
        ],
    )
    assert text.startswith("auto_service_line: client 5001 has 2 Hourly service lines on Active contracts that cover the ticket (")
    assert "contract 'Managed Services', line 'Remote support', line Id 901" in text
    assert "contract 'Projects', line 'Onsite', line Id 902" in text
    assert "903" not in text and "Blocks" not in text  # a Block Hours line is not a candidate
    assert text.endswith("pass service_line_id with the Id of the one to bill")


# --- other labor lines beside the one Hourly line --------------------------------------------


def other_note(hourly_label, hourly_id, count, listed):
    return (
        f"auto_service_line sent the Hourly line {hourly_label} (Id {hourly_id}), but the covering contracts also have {count}: "
        f"{listed}. Gorelo may have picked one of those on its own; service_line_id picks another line, and leaving out "
        "auto_service_line lets Gorelo choose."
    )


async def test_block_hours_and_limited_hours_beside_the_hourly_line_are_named_in_a_note(server, mock_gorelo):
    contracts = [contract(10, [line(901, "Hourly support"), line(902, "Block pack", 3), line(903, "Capped", 5)])]
    result = await auto_picks(server, mock_gorelo, contracts, 901)
    assert result == {
        **landed(901),
        "note": other_note("'Hourly support'", 901, "2 other labor lines", "'Block pack' (Block Hours); 'Capped' (Limited Hours)"),
    }


async def test_one_other_labor_line_is_said_in_the_singular(server, mock_gorelo):
    result = await auto_picks(server, mock_gorelo, [contract(10, [line(901, "Hourly"), line(902, "Care", 1)])], 901)
    assert result["note"] == other_note("'Hourly'", 901, "1 other labor line", "'Care' (Unlimited Hours)")


async def test_an_unknown_labor_terms_id_counts_as_other_labor_and_shows_gorelos_name_for_it(server, mock_gorelo):
    contracts = [contract(10, [line(901, "Hourly"), line(902, "Prepaid", 9, terms_name="Prepaid Hours")])]
    result = await auto_picks(server, mock_gorelo, contracts, 901)
    assert result["note"] == other_note("'Hourly'", 901, "1 other labor line", "'Prepaid' (Prepaid Hours)")


async def test_an_unknown_labor_terms_id_with_no_name_is_shown_by_its_id(server, mock_gorelo):
    nameless = {"Id": 902, "Name": "Odd", "LaborTerms": {"Id": 9}}
    result = await auto_picks(server, mock_gorelo, [contract(10, [line(901, "Hourly"), nameless])], 901)
    assert result["note"] == other_note("'Hourly'", 901, "1 other labor line", "'Odd' (labor terms Id 9)")


async def test_more_than_three_other_labor_lines_are_listed_three_and_counted_the_rest(server, mock_gorelo):
    others = [line(910 + n, f"Other {n}", terms) for n, terms in enumerate((1, 3, 5, 1, 3))]
    result = await auto_picks(server, mock_gorelo, [contract(10, [line(901, "Hourly"), *others])], 901)
    assert result["note"] == other_note(
        "'Hourly'",
        901,
        "5 other labor lines",
        "'Other 0' (Unlimited Hours); 'Other 1' (Block Hours); 'Other 2' (Limited Hours); and 2 more",
    )
    assert "Other 3" not in result["note"] and "Other 4" not in result["note"]


async def test_exactly_three_other_labor_lines_are_all_listed_with_no_and_more(server, mock_gorelo):
    others = [line(910 + n, f"Other {n}", 1) for n in range(3)]
    result = await auto_picks(server, mock_gorelo, [contract(10, [line(901, "Hourly"), *others])], 901)
    assert "and " not in result["note"].split("also have 3 other labor lines: ")[1].split(". Gorelo")[0]


async def test_other_labor_lines_of_every_covering_contract_count_and_a_line_seen_twice_counts_once(server, mock_gorelo):
    contracts = [
        [contract(10, [line(901, "Hourly"), line(902, "Care", 1)], name="A")],
        [contract(10, [line(901, "Hourly"), line(902, "Care", 1)], name="A"), contract(11, [line(903, "Pack", 3)], name="B")],
    ]
    result = await auto_picks(server, mock_gorelo, contracts, 901)
    assert result["note"] == other_note("'Hourly'", 901, "2 other labor lines", "'Care' (Unlimited Hours); 'Pack' (Block Hours)")


async def test_other_labor_lines_of_a_skipped_contract_make_no_note(server, mock_gorelo):
    # one contract ended long ago, one is for another location, one starts later: their Unlimited and Block lines do not count
    contracts = [
        contract(10, [line(901, "Hourly")]),
        contract(11, [line(902, "Old care", 1)], EndDate="2020-06-01", name="Old"),
        scoped(12, [line(903, "Branch pack", 3)], [8], name="Branch"),
        contract(13, [line(904, "Later", 5)], StartDate="2027-01-01", name="Later"),
    ]
    result = await auto_picks(server, mock_gorelo, contracts, 901, location_id=7)
    assert result == landed(901)


async def test_no_labor_terms_lines_alone_beside_the_hourly_line_make_no_note(server, mock_gorelo):
    result = await auto_picks(server, mock_gorelo, [contract(10, [line(901, "Hourly"), line(902, "Nothing", 4)])], 901)
    assert result == landed(901)


async def test_a_covering_contract_without_an_hourly_line_still_contributes_its_other_lines(server, mock_gorelo):
    contracts = [contract(10, [line(901, "Hourly")], name="A"), contract(11, [line(902, "Care", 1)], name="B")]
    result = await auto_picks(server, mock_gorelo, contracts, 901)
    assert result["note"] == other_note("'Hourly'", 901, "1 other labor line", "'Care' (Unlimited Hours)")


@pytest.mark.parametrize(
    "record, warning",
    [
        (entry(501, ServiceLine=None), LOST.format(sent=901, shown="no service line")),
        (entry(501, ServiceLine={"Id": 66, "Name": "Gorelo pick"}), LOST.format(sent=901, shown="service line 'Gorelo pick' (Id 66)")),
        (entry(501, ServiceLine="Remote"), LOST.format(sent=901, shown="a ServiceLine that cannot be read")),
    ],
)
async def test_the_other_labor_lines_note_is_added_only_when_the_read_back_shows_the_sent_line(server, mock_gorelo, record, warning):
    on_auto(mock_gorelo, [contract(10, [line(901, "Hourly"), line(902, "Care", 1)])])
    on_create(mock_gorelo, record=record)
    result = await call_tool(server, "create_time_entry", AUTO_BASE)
    assert result == {**record, "warning": warning}


async def test_the_other_labor_lines_note_is_not_added_after_a_failed_reread(server, mock_gorelo):
    on_auto(mock_gorelo, [contract(10, [line(901, "Hourly"), line(902, "Care", 1)])])
    mock_gorelo.on("POST", "/v1/time-entries", envelope({"Id": 777}))
    mock_gorelo.on("GET", "/v1/time-entries/777", error_envelope(500, [("070001", "Internal error.")]))
    result = await call_tool(server, "create_time_entry", AUTO_BASE)
    assert set(result) == {"Id", "warning"} and "re-reading it failed" in result["warning"]


async def test_the_other_labor_lines_note_is_appended_to_a_note_the_record_already_has(server, mock_gorelo):
    on_auto(mock_gorelo, [contract(10, [line(901, "Hourly"), line(902, "Care", 1)])])
    on_create(mock_gorelo, record=landed(901, note="an earlier note"))
    result = await call_tool(server, "create_time_entry", AUTO_BASE)
    assert result["note"] == "an earlier note " + other_note("'Hourly'", 901, "1 other labor line", "'Care' (Unlimited Hours)")


async def test_other_labor_lines_make_no_note_when_no_hourly_line_was_found(server, mock_gorelo):
    # no Hourly line: the existing "found no Hourly line" note stands alone
    result = await auto_sends_none(server, mock_gorelo, [contract(10, [line(900, terms=1), line(902, terms=3)])])
    assert result["note"] == NO_HOURLY_SENT + "; the entry has no service line and bills at the billing role's default rate."


async def test_other_labor_lines_make_no_note_when_the_ticket_has_its_own_line(server, mock_gorelo):
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail(own_line={"Id": 940, "Name": "Ticket line"})))
    on_create(mock_gorelo, record=landed(940, "Ticket line"))
    own = await call_tool(server, "create_time_entry", AUTO_BASE)
    assert own["note"] == "The ticket's own service line 'Ticket line' (Id 940) was used."


async def test_the_several_hourly_lines_refusal_is_unchanged_by_other_labor_lines(server, mock_gorelo):
    text = await auto_refused(
        server, mock_gorelo, [contract(10, [line(901), line(903, "Care", 1)], name="A"), contract(11, [line(902)], name="B")]
    )
    assert text.startswith("auto_service_line: client 5001 has 2 Hourly service lines on Active contracts that cover the ticket (")
    assert "Care" not in text


# --- the constants -----------------------------------------------------------


def test_the_hourly_and_active_codes_are_plain_ints_pinned_to_the_contracts_tables():
    from tools import contracts

    assert type(time_entries.HOURLY_LABOR_TERMS) is int and time_entries.HOURLY_LABOR_TERMS == 2
    assert type(time_entries.NO_LABOR_TERMS) is int and time_entries.NO_LABOR_TERMS == 4
    assert type(time_entries.ACTIVE_CONTRACT_STATUS) is int and time_entries.ACTIVE_CONTRACT_STATUS == 1
    # a drift in tools/contracts.py is caught here, not at import
    assert contracts.LABOR_TERMS[time_entries.HOURLY_LABOR_TERMS] == "Hourly"
    assert contracts.LABOR_TERMS[time_entries.NO_LABOR_TERMS] == "No Labor Terms"
    # the whole scale: Hourly and No Labor Terms are named by the module, the other three are "other labor" by exclusion
    assert dict(contracts.LABOR_TERMS) == {1: "Unlimited Hours", 2: "Hourly", 3: "Block Hours", 4: "No Labor Terms", 5: "Limited Hours"}
    assert contracts.CONTRACT_STATUS_LABELS[time_entries.ACTIVE_CONTRACT_STATUS] == "Active"


def test_no_tool_module_imports_another_tool_module():
    import ast

    tree = ast.parse(Path(time_entries.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    other_tools = {name for name in imported if name.startswith("tools.") and name not in ("tools._common", "tools._ticket_ref")}
    assert other_tools == set()
    assert not hasattr(time_entries, "LABOR_TERMS") and not hasattr(time_entries, "CONTRACT_STATUS_LABELS")


# --- the ticket's own service line -----------------------------------------------


async def test_the_ticket_own_service_line_is_sent_with_no_contracts_request_and_a_note(server, mock_gorelo):
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail(own_line={"Id": 940, "Name": "Ticket line"})))
    on_create(mock_gorelo, record=landed(940, "Ticket line"))
    result = await call_tool(server, "create_time_entry", AUTO_BASE)
    assert request_log(mock_gorelo) == [
        ("GET", TICKET_PATH),
        ("POST", "/v1/time-entries"),
        ("GET", "/v1/time-entries/501"),
    ]
    assert mock_gorelo.calls("POST")[0].json["ServiceLineId"] == 940
    assert result == {**landed(940, "Ticket line"), "note": "The ticket's own service line 'Ticket line' (Id 940) was used."}


async def test_the_ticket_own_service_line_beats_an_hourly_line_of_a_contract(server, mock_gorelo):
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail(own_line={"Id": 940, "Name": "Ticket line"})))
    mock_gorelo.on("GET", "/v1/contracts", paged_envelope([contract(10, [line(901)])]))  # never asked
    on_create(mock_gorelo, record=landed(940, "Ticket line"))
    await call_tool(server, "create_time_entry", AUTO_BASE)
    assert mock_gorelo.calls("GET", "/v1/contracts") == []
    assert mock_gorelo.calls("POST")[0].json["ServiceLineId"] == 940


async def test_the_ticket_own_service_line_needs_no_client_and_no_location(server, mock_gorelo):
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail(client_id=None, own_line={"Id": 940, "Name": "Ticket line"})))
    on_create(mock_gorelo, record=landed(940, "Ticket line"))
    await call_tool(server, "create_time_entry", AUTO_BASE)
    assert mock_gorelo.calls("POST")[0].json["ServiceLineId"] == 940


async def test_a_nameless_ticket_own_service_line_is_still_used_and_named_by_its_id(server, mock_gorelo):
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail(own_line={"Id": 940})))
    on_create(mock_gorelo, record=landed(940))
    result = await call_tool(server, "create_time_entry", AUTO_BASE)
    assert result["note"] == "The ticket's own service line (no name) (Id 940) was used."


@pytest.mark.parametrize(
    "record, shown",
    [
        (entry(501, ServiceLine=None), "no service line"),
        (entry(501, ServiceLine={"Id": 66, "Name": "Other line"}), "service line 'Other line' (Id 66)"),
        (entry(501, ServiceLine="Remote"), "a ServiceLine that cannot be read"),
    ],
)
async def test_the_ticket_own_service_line_that_does_not_stick_warns_and_adds_no_note(server, mock_gorelo, record, shown):
    # the note says the line "was used": it is added only when the read-back shows that line
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail(own_line={"Id": 940, "Name": "Ticket line"})))
    on_create(mock_gorelo, record=record)
    result = await call_tool(server, "create_time_entry", AUTO_BASE)
    assert result == {**record, "warning": LOST.format(sent=940, shown=shown)}
    assert mock_gorelo.calls("POST")[0].json["ServiceLineId"] == 940


async def test_the_ticket_own_service_line_with_a_failed_reread_adds_no_note(server, mock_gorelo):
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail(own_line={"Id": 940, "Name": "Ticket line"})))
    mock_gorelo.on("POST", "/v1/time-entries", envelope({"Id": 777}))
    mock_gorelo.on("GET", "/v1/time-entries/777", error_envelope(500, [("070001", "Internal error.")]))
    result = await call_tool(server, "create_time_entry", AUTO_BASE)
    assert set(result) == {"Id", "warning"} and "re-reading it failed" in result["warning"]


async def test_the_ticket_own_service_line_with_a_record_that_has_no_service_line_field_adds_the_field_warning_and_no_note(
    server, mock_gorelo
):
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail(own_line={"Id": 940, "Name": "Ticket line"})))
    record = entry(501)
    record.pop("ServiceLine")
    on_create(mock_gorelo, record=record)
    result = await call_tool(server, "create_time_entry", AUTO_BASE)
    assert result == {**record, "warning": FIELD_MISSING_SENT.format(sent=940)}


SHAPE_OF_THE_TICKET = "GET /v1/tickets/{ticketId}: "


@pytest.mark.parametrize(
    "override, fragment",
    [
        (DROP, "BillingOverride is "),
        (None, "BillingOverride is "),
        ("x", "BillingOverride is "),
        ([], "BillingOverride is "),
        ({}, "BillingOverride has no ServiceLine"),
        ({"ServiceLine": "x"}, "BillingOverride.ServiceLine is neither null nor an object with a whole number Id"),
        ({"ServiceLine": []}, "BillingOverride.ServiceLine is neither null nor an object with a whole number Id"),
        ({"ServiceLine": {"Name": "no id"}}, "BillingOverride.ServiceLine is neither null nor an object with a whole number Id"),
        ({"ServiceLine": {"Id": "9"}}, "BillingOverride.ServiceLine is neither null nor an object with a whole number Id"),
        ({"ServiceLine": {"Id": True}}, "BillingOverride.ServiceLine is neither null nor an object with a whole number Id"),
        ({"ServiceLine": {"Id": 0}}, "BillingOverride.ServiceLine is neither null nor an object with a whole number Id"),
        ({"ServiceLine": {"Id": 9.5}}, "BillingOverride.ServiceLine is neither null nor an object with a whole number Id"),
    ],
)
async def test_a_billing_override_that_is_not_an_object_with_a_usable_service_line_is_a_shape_error(
    server, mock_gorelo, override, fragment
):
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail(BillingOverride=override)))
    text = await call_tool_error(server, "create_time_entry", AUTO_BASE)
    assert SHAPE_OF_THE_TICKET in text and fragment in text and REFUSED_AS_SHAPE in text
    assert request_log(mock_gorelo) == [("GET", TICKET_PATH)]


@pytest.mark.parametrize("location_id", ["7", 0, -3, True, 1.5, [7]])
async def test_a_ticket_with_an_unusable_location_id_is_a_shape_error(server, mock_gorelo, location_id):
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail(location_id=location_id)))
    text = await call_tool_error(server, "create_time_entry", AUTO_BASE)
    assert SHAPE_OF_THE_TICKET + "LocationId is " in text and REFUSED_AS_SHAPE in text
    assert request_log(mock_gorelo) == [("GET", TICKET_PATH)]


async def test_a_ticket_with_a_null_client_and_no_own_line_is_refused_with_no_contract_read_and_no_post(server, mock_gorelo):
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail(ClientId=None)))
    text = await call_tool_error(server, "create_time_entry", AUTO_BASE)
    assert text.startswith("auto_service_line: the ticket has no client (ClientId is empty)")
    assert request_log(mock_gorelo) == [("GET", TICKET_PATH)]


@pytest.mark.parametrize("key", ["ClientId", "LocationId"])
async def test_a_ticket_answer_without_a_client_id_or_location_id_key_is_a_shape_error_not_a_default(server, mock_gorelo, key):
    # a missing key is not the same as null: null means "the ticket has none", a missing key means the answer is not what we read
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail(**{key: DROP})))
    text = await call_tool_error(server, "create_time_entry", AUTO_BASE)
    assert SHAPE_OF_THE_TICKET + f"Data has no {key} (null when the ticket has none)" in text and REFUSED_AS_SHAPE in text
    assert request_log(mock_gorelo) == [("GET", TICKET_PATH)]


async def test_a_ticket_answer_without_a_client_id_key_is_a_shape_error_even_when_it_has_its_own_line(server, mock_gorelo):
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail(own_line={"Id": 940, "Name": "Ticket line"}, ClientId=DROP)))
    text = await call_tool_error(server, "create_time_entry", AUTO_BASE)
    assert "Data has no ClientId" in text and mock_gorelo.calls("POST") == []


@pytest.mark.parametrize("client_id", ["5001", 0, -3, True, 12.5, [5001]])
async def test_a_ticket_with_an_unusable_client_id_is_a_shape_error(server, mock_gorelo, client_id):
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail(client_id=client_id)))
    text = await call_tool_error(server, "create_time_entry", AUTO_BASE)
    assert SHAPE_OF_THE_TICKET + "ClientId is " in text and REFUSED_AS_SHAPE in text
    assert request_log(mock_gorelo) == [("GET", TICKET_PATH)]


@pytest.mark.parametrize("data", [None, [], "x"])
async def test_a_ticket_answer_that_is_not_an_object_is_a_shape_error(server, mock_gorelo, data):
    mock_gorelo.on("GET", TICKET_PATH, envelope(data))
    text = await call_tool_error(server, "create_time_entry", AUTO_BASE)
    assert "GET /v1/tickets/{ticketId}" in text
    assert mock_gorelo.calls("POST") == []


# --- what is checked on the contracts that were read ---------------------------------


@pytest.mark.parametrize(
    "bad_contract, fragment",
    [
        ("text", "contract 1 is a string, not an object"),
        (contract(10, [], ServiceLines=DROP), "contract 1 has ServiceLines as"),
        (contract(10, [], ServiceLines=None), "contract 1 has ServiceLines as"),
        (contract(10, [], ServiceLines={"Id": 1}), "contract 1 has ServiceLines as"),
        (contract(10, ["x"]), "contract 1 has a service line without a whole number Id"),
        (contract(10, [{"Name": "no id", "LaborTerms": {"Id": 2}}]), "without a whole number Id"),
        (contract(10, [{"Id": "9", "LaborTerms": {"Id": 2}}]), "without a whole number Id"),
        (contract(10, [{"Id": True, "LaborTerms": {"Id": 2}}]), "without a whole number Id"),
        (contract(10, [{"Id": 0, "LaborTerms": {"Id": 2}}]), "without a whole number Id"),
        (contract(10, [{"Id": 9}]), "service line 9, has no usable LaborTerms Id"),
        (contract(10, [{"Id": 9, "LaborTerms": None}]), "has no usable LaborTerms Id"),
        (contract(10, [{"Id": 9, "LaborTerms": {"Name": "Hourly"}}]), "has no usable LaborTerms Id"),
        (contract(10, [{"Id": 9, "LaborTerms": {"Id": "2"}}]), "has no usable LaborTerms Id"),
    ],
)
async def test_an_unusable_contract_row_is_a_shape_error_never_a_guess(server, mock_gorelo, bad_contract, fragment):
    # a good Hourly line beside the bad row must not rescue it: the whole answer is refused
    text = await auto_refused(server, mock_gorelo, [bad_contract, contract(11, [line(901)])])
    assert "GET /v1/contracts: " in text and fragment in text and REFUSED_AS_SHAPE in text


@pytest.mark.parametrize(
    "fields",
    [
        {"ClientId": 5002},
        {"ClientId": "5001"},
        {"ClientId": None},
        {"ClientId": DROP},
        {"ClientId": True},
    ],
)
async def test_a_contract_of_another_client_is_a_shape_error_and_a_good_row_does_not_rescue_it(server, mock_gorelo, fields):
    text = await auto_refused(
        server, mock_gorelo, [contract(10, [line(900)], **fields), contract(11, [line(901)])], reads=[("GET", TICKET_PATH), ("GET", "/v1/contracts")]
    )
    assert "GET /v1/contracts: contract 1 does not belong to the ticket's client" in text and REFUSED_AS_SHAPE in text


@pytest.mark.parametrize(
    "status",
    [{"Id": 2, "Name": "Expired"}, {"Id": 3, "Name": "Archived"}, {"Name": "Active"}, {"Id": "1"}, {"Id": True}, None, "Active", DROP],
)
async def test_a_contract_that_is_not_active_is_a_shape_error(server, mock_gorelo, status):
    text = await auto_refused(server, mock_gorelo, [contract(10, [line(901)], Status=status)])
    assert "GET /v1/contracts: contract 1 is not an Active contract" in text and REFUSED_AS_SHAPE in text


async def test_a_count_mismatch_of_the_contract_scan_is_a_shape_error(server, mock_gorelo):
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail()))
    mock_gorelo.on("GET", "/v1/contracts", paged_responder([[contract(10, [line(901)])]], total_count=3))
    text = await call_tool_error(server, "create_time_entry", AUTO_BASE)
    assert "GET /v1/contracts: Gorelo reported 3 contracts but 1 rows were read" in text and REFUSED_AS_SHAPE in text
    assert mock_gorelo.calls("POST") == []


@pytest.mark.parametrize("value", ["yes", None, 1, 0, "true", DROP])
async def test_an_unusable_is_for_all_locations_is_a_shape_error(server, mock_gorelo, value):
    text = await auto_refused(server, mock_gorelo, [contract(10, [line(901)], IsForAllLocations=value)])
    assert "contract 1 has IsForAllLocations as" in text and REFUSED_AS_SHAPE in text


@pytest.mark.parametrize("value", [None, "7", 7, {}, [0], [True], ["7"], [7.5], [-1], DROP])
async def test_an_unusable_location_ids_is_a_shape_error(server, mock_gorelo, value):
    text = await auto_refused(server, mock_gorelo, [contract(10, [line(901)], LocationIds=value)])
    assert "contract 1 has LocationIds as" in text and REFUSED_AS_SHAPE in text


@pytest.mark.parametrize("value", [None, "", "soon", 20261001, True, [], "2026-13-40", "2026-10", "2026-W40-1"])
async def test_an_unparseable_start_date_is_a_shape_error(server, mock_gorelo, value):
    text = await auto_refused(server, mock_gorelo, [contract(10, [line(901)], StartDate=value)])
    assert "contract 1 has StartDate as" in text and REFUSED_AS_SHAPE in text


@pytest.mark.parametrize("value", ["", "never", 20261001, True, [], "2026-02-31", "2026-10"])
async def test_an_unparseable_end_date_is_a_shape_error(server, mock_gorelo, value):
    text = await auto_refused(server, mock_gorelo, [contract(10, [line(901)], EndDate=value)])
    assert "contract 1 has EndDate as" in text and REFUSED_AS_SHAPE in text


async def test_a_contract_row_without_an_end_date_key_is_a_shape_error(server, mock_gorelo):
    text = await auto_refused(server, mock_gorelo, [contract(10, [line(901)], EndDate=DROP)])
    assert "GET /v1/contracts: contract 1 has no EndDate (null when the contract is open-ended)" in text and REFUSED_AS_SHAPE in text


async def test_a_null_end_date_is_open_ended(server, mock_gorelo):
    await auto_picks(server, mock_gorelo, [contract(10, [line(901)], EndDate=None)], 901)


async def test_a_bad_row_is_refused_even_when_a_contract_around_it_is_skipped(server, mock_gorelo):
    # the first contract is outside its dates and would be skipped; the second has a bad scope. Every row is checked.
    contracts = [contract(10, [line(900)], EndDate="2020-02-01"), contract(11, [line(901)], LocationIds="all")]
    text = await auto_refused(server, mock_gorelo, contracts)
    assert "contract 2 has LocationIds as" in text


async def test_a_date_time_for_a_contract_date_is_read_by_its_day(server, mock_gorelo):
    contracts = [contract(10, [line(901)], StartDate="2020-01-01T00:00:00Z", EndDate="2030-01-01T00:00:00")]
    await auto_picks(server, mock_gorelo, contracts, 901)


# --- location scope --------------------------------------------------------------------


async def test_a_contract_that_lists_the_ticket_location_covers_it(server, mock_gorelo):
    await auto_picks(server, mock_gorelo, [scoped(10, [line(901)], [3, 7])], 901, location_id=7)


async def test_a_contract_that_does_not_list_the_ticket_location_is_skipped(server, mock_gorelo):
    contracts = [scoped(10, [line(901)], [3, 8], name="Branch"), contract(11, [line(902, "All sites")], name="Everywhere")]
    await auto_picks(server, mock_gorelo, contracts, 902, location_id=7)


async def test_a_contract_for_all_locations_covers_a_ticket_with_a_location(server, mock_gorelo):
    await auto_picks(server, mock_gorelo, [contract(10, [line(901)])], 901, location_id=7)


async def test_two_hourly_lines_of_which_one_is_for_another_location_are_not_several(server, mock_gorelo):
    contracts = [scoped(10, [line(901)], [8]), scoped(11, [line(902)], [7]), contract(12, [line(903, terms=3)])]
    await auto_picks(server, mock_gorelo, contracts, 902, location_id=7)


async def test_two_covering_hourly_lines_are_several_even_when_one_is_location_scoped(server, mock_gorelo):
    text = await auto_refused(
        server, mock_gorelo, [scoped(10, [line(901)], [7], name="Site"), contract(11, [line(902)], name="All")], location_id=7
    )
    assert "has 2 Hourly service lines" in text and "line Id 901" in text and "line Id 902" in text


async def test_a_scoped_contract_with_an_hourly_line_and_a_ticket_with_no_location_is_refused(server, mock_gorelo):
    text = await auto_refused(
        server,
        mock_gorelo,
        [scoped(10, [line(901)], [7], name="Branch support"), contract(11, [line(902)])],
        reads=[("GET", TICKET_PATH), ("GET", "/v1/contracts")],
    )
    assert text == (
        "auto_service_line: the ticket has no location (LocationId is empty) and contract 'Branch support' is limited to "
        "locations and has an Hourly service line, so the tool cannot tell whether it applies; pass service_line_id with the "
        "Id of the line to bill"
    )


async def test_a_scoped_contract_with_an_hourly_line_refuses_even_with_one_other_hourly_line_to_pick(server, mock_gorelo):
    text = await auto_refused(server, mock_gorelo, [contract(11, [line(902)]), scoped(10, [line(901)], [7], name="Branch")])
    assert "contract 'Branch' is limited to locations" in text and "service_line_id" in text


async def test_a_scoped_contract_without_an_hourly_line_does_not_stop_a_ticket_with_no_location(server, mock_gorelo):
    contracts = [scoped(10, [line(900, terms=1), line(903, terms=3)], [7]), contract(11, [line(902)])]
    await auto_picks(server, mock_gorelo, contracts, 902)


async def test_a_ticket_with_no_location_uses_a_contract_for_all_locations(server, mock_gorelo):
    await auto_picks(server, mock_gorelo, [contract(10, [line(901)])], 901)


async def test_a_scoped_hourly_contract_outside_its_dates_does_not_stop_a_ticket_with_no_location(server, mock_gorelo):
    contracts = [scoped(10, [line(901)], [7], EndDate="2020-06-01"), contract(11, [line(902)])]
    await auto_picks(server, mock_gorelo, contracts, 902)


# --- date scope (the entry's day is 2026-10-01 in CREATE_BASE) ---------------------------


async def test_a_contract_that_starts_in_the_future_is_skipped(server, mock_gorelo):
    contracts = [contract(10, [line(901)], StartDate="2026-12-01"), contract(11, [line(902)], StartDate="2026-09-01")]
    await auto_picks(server, mock_gorelo, contracts, 902)


async def test_a_contract_that_ended_in_the_past_is_skipped(server, mock_gorelo):
    contracts = [contract(10, [line(901)], EndDate="2026-08-31"), contract(11, [line(902)], EndDate="2026-10-31")]
    await auto_picks(server, mock_gorelo, contracts, 902)


MARGIN_START = (
    "auto_service_line: contract 'Contract 10' starts on 2026-10-02, the day after the entry's day (2026-10-01, UTC), and has an "
    "Hourly service line, so the tool cannot tell whether it applies (Gorelo's contract dates are calendar days in a time zone "
    "it does not know); pass service_line_id with the Id of the line to bill"
)
MARGIN_END = (
    "auto_service_line: contract 'Contract 10' ended on 2026-09-30, the day before the entry's day (2026-10-01, UTC), and has an "
    "Hourly service line, so the tool cannot tell whether it applies (Gorelo's contract dates are calendar days in a time zone "
    "it does not know); pass service_line_id with the Id of the line to bill"
)


@pytest.mark.parametrize(
    "fields, outcome",
    [
        ({"StartDate": "2026-10-01"}, "taken"),  # starts on the day: covers it outright
        ({"StartDate": "2026-10-02"}, MARGIN_START),  # one day after the day: inside the margin only, with an Hourly line
        ({"StartDate": "2026-10-03"}, "skipped"),  # two days after: clearly outside
        ({"EndDate": "2026-10-01"}, "taken"),  # ends on the day
        ({"EndDate": "2026-09-30"}, MARGIN_END),  # one day before the day: inside the margin only
        ({"EndDate": "2026-09-29"}, "skipped"),  # two days before: clearly outside
    ],
)
async def test_the_one_day_margin_on_both_sides_of_the_entry_day(server, mock_gorelo, fields, outcome):
    on_auto(mock_gorelo, [contract(10, [line(901)], **fields)])
    on_create(mock_gorelo, record=landed(901) if outcome == "taken" else entry(501, ServiceLine=None))
    if outcome not in ("taken", "skipped"):  # refused before any write, with the text that names the contract and its date
        text = await call_tool_error(server, "create_time_entry", AUTO_BASE)
        assert text == outcome
        assert mock_gorelo.calls("POST") == [] and request_log(mock_gorelo) == [("GET", TICKET_PATH), ("GET", "/v1/contracts")]
        return
    result = await call_tool(server, "create_time_entry", AUTO_BASE)
    body = mock_gorelo.calls("POST")[0].json
    if outcome == "taken":
        assert body["ServiceLineId"] == 901
    else:
        assert "ServiceLineId" not in body and "outside its dates" in result["note"]


@pytest.mark.parametrize("fields", [{"StartDate": "2026-10-02"}, {"EndDate": "2026-09-30"}], ids=["starts the day after", "ended the day before"])
async def test_a_margin_only_contract_without_an_hourly_line_is_skipped_with_a_reason_not_refused(server, mock_gorelo, fields):
    contracts = [contract(10, [line(900, terms=1), line(902, terms=4)], name="Edge", **fields)]
    result = await auto_sends_none(server, mock_gorelo, contracts)
    assert result["note"].endswith("1 Active contract skipped because the contract does not cover the ticket's location or the entry's day: 'Edge' (outside its dates).")


async def test_a_margin_only_contract_without_an_hourly_line_does_not_block_another_contracts_pick(server, mock_gorelo):
    contracts = [contract(10, [line(900, terms=1)], StartDate="2026-10-02"), contract(11, [line(901)])]
    result = await auto_picks(server, mock_gorelo, contracts, 901)
    assert result == landed(901)  # the skipped contract's Unlimited line is not an "other line" of a covering contract: no note


@pytest.mark.parametrize("fields", [{"StartDate": "2026-10-02"}, {"EndDate": "2026-09-30"}], ids=["starts the day after", "ended the day before"])
async def test_a_margin_only_contract_with_an_hourly_line_refuses_even_when_another_contract_has_the_one_to_pick(server, mock_gorelo, fields):
    text = await auto_refused(server, mock_gorelo, [contract(11, [line(902)]), contract(10, [line(901)], **fields)])
    assert text.startswith("auto_service_line: contract 'Contract 10' ") and "pass service_line_id" in text


async def test_a_margin_only_contract_for_other_locations_is_skipped_for_its_location_not_refused(server, mock_gorelo):
    # it would not apply to this ticket whatever its dates are, so there is nothing to decide
    contracts = [scoped(10, [line(901)], [8], name="Branch", StartDate="2026-10-02"), contract(11, [line(902)])]
    await auto_picks(server, mock_gorelo, contracts, 902, location_id=7)


async def test_a_margin_only_contract_that_lists_the_ticket_location_is_refused(server, mock_gorelo):
    text = await auto_refused(server, mock_gorelo, [scoped(10, [line(901)], [7], StartDate="2026-10-02")], location_id=7)
    assert text == MARGIN_START


async def test_a_margin_only_contract_for_all_locations_is_refused_for_a_ticket_with_no_location(server, mock_gorelo):
    text = await auto_refused(server, mock_gorelo, [contract(10, [line(901)], EndDate="2026-09-30")])
    assert text == MARGIN_END


@pytest.mark.parametrize("start, outcome", [("2026-10-02", "taken"), ("2026-10-03", "margin"), ("2026-10-04", "skipped")])
async def test_the_entry_day_is_the_utc_date_of_started_on(server, mock_gorelo, start, outcome):
    # 2026-10-01 23:30 at -05:00 is 2026-10-02 04:30 UTC: the day is 2026-10-02, so a contract starting on 2026-10-02 covers
    # it outright, 2026-10-03 is the day after (margin only: refused) and 2026-10-04 is two days after it (skipped); the local
    # date, 2026-10-01, would have said something else for each
    args = {**AUTO_BASE, "started_on": "2026-10-01T23:30:00-05:00", "ended_on": "2026-10-02T00:30:00-05:00"}
    contracts = [contract(10, [line(901)], StartDate=start)]
    if outcome == "taken":
        await auto_picks(server, mock_gorelo, contracts, 901, args=args)
    elif outcome == "margin":
        text = await auto_refused(server, mock_gorelo, contracts, args=args)
        assert "starts on 2026-10-03, the day after the entry's day (2026-10-02, UTC)" in text
    else:
        result = await auto_sends_none(server, mock_gorelo, contracts, args=args)
        assert "'Contract 10' (outside its dates)" in result["note"]


def only_ended_and_hours(ended, hours, **extra):
    """AUTO_BASE with no started_on: the entry is dated by ended_on minus actual_hours."""
    return make_args(AUTO_BASE, started_on=DROP, ended_on=ended, actual_hours=hours, **extra)


async def test_without_started_on_the_entry_day_is_ended_on_minus_actual_hours_not_today(server, mock_gorelo):
    # an entry of 2026-08-15 (the real date today is irrelevant): a contract that ended 2026-08-10 is clearly outside, one
    # that starts 2026-08-17 is clearly outside, one that ended on 2026-08-15 covers the day, and one that ended on 2026-08-14
    # is inside the margin only (refused, with the day it computed)
    args = only_ended_and_hours("2026-08-15T10:00:00Z", 2)
    contracts = [
        contract(10, [line(900)], EndDate="2026-08-10", name="Ended"),
        contract(11, [line(901)], EndDate="2026-08-15", name="Ends on the day"),
        contract(12, [line(902)], StartDate="2026-08-17", name="Starts later"),
    ]
    await auto_picks(server, mock_gorelo, contracts, 901, args=args)


async def test_without_started_on_a_contract_that_ended_the_day_before_the_computed_day_is_refused(server, mock_gorelo):
    args = only_ended_and_hours("2026-08-15T10:00:00Z", 2)
    text = await auto_refused(server, mock_gorelo, [contract(13, [line(903)], EndDate="2026-08-14", name="Ended the day before")], args=args)
    assert "'Ended the day before' ended on 2026-08-14, the day before the entry's day (2026-08-15, UTC)" in text


async def test_an_end_date_after_the_entry_day_covers_an_entry_dated_by_ended_on_minus_hours(server, mock_gorelo):
    # Date arithmetic only: an entry of 2026-09-10 (ended_on minus actual_hours) against an EndDate of 2026-09-30. An Active
    # contract does not carry a past EndDate in the spec's own terms, so this pins how an EndDate after the day is read, not a
    # contract that ended since.
    args = only_ended_and_hours("2026-09-10T10:00:00Z", 1.5)
    await auto_picks(server, mock_gorelo, [contract(10, [line(901)], StartDate="2026-01-01", EndDate="2026-09-30")], 901, args=args)


async def test_a_contract_that_was_not_open_in_the_month_of_the_entry_is_skipped(server, mock_gorelo):
    args = only_ended_and_hours("2026-09-10T10:00:00Z", 1.5)
    result = await auto_sends_none(
        server, mock_gorelo, [contract(10, [line(901)], StartDate="2026-01-01", EndDate="2026-08-01", name="Earlier")], args=args
    )
    assert "'Earlier' (outside its dates)" in result["note"]


async def test_an_entry_that_crosses_utc_midnight_is_dated_by_its_start(server, mock_gorelo):
    # ended_on 2026-10-02 01:00 UTC minus 2 hours starts 2026-10-01 23:00 UTC: the day is the 1st (not the 2nd, the day it ends),
    # so a contract that starts on the 3rd is clearly outside (it would be inside the margin for the 2nd)
    args = only_ended_and_hours("2026-10-02T01:00:00Z", 2)
    result = await auto_sends_none(server, mock_gorelo, [contract(10, [line(901)], StartDate="2026-10-03", name="Later")], args=args)
    assert "'Later' (outside its dates)" in result["note"]


async def test_a_crossing_entry_with_started_on_is_dated_by_started_on(server, mock_gorelo):
    args = {**AUTO_BASE, "started_on": "2026-10-01T23:00:00Z", "ended_on": "2026-10-02T01:00:00Z"}
    result = await auto_sends_none(server, mock_gorelo, [contract(10, [line(901)], StartDate="2026-10-03", name="Later")], args=args)
    assert "'Later' (outside its dates)" in result["note"]


async def test_started_on_with_actual_hours_is_dated_by_started_on(server, mock_gorelo):
    # started 2026-10-01 22:00 UTC for 3 hours ends 2026-10-02: dated by the start (the 1st), a contract that starts on the 2nd
    # is the day after (margin only); dated by the end it would have covered the day outright and been picked
    args = make_args(AUTO_BASE, ended_on=DROP, actual_hours=3, started_on="2026-10-01T22:00:00Z")
    text = await auto_refused(server, mock_gorelo, [contract(10, [line(901)], StartDate="2026-10-02")], args=args)
    assert "starts on 2026-10-02, the day after the entry's day (2026-10-01, UTC)" in text


async def test_started_on_with_actual_hours_covers_a_contract_that_starts_on_that_day(server, mock_gorelo):
    args = make_args(AUTO_BASE, ended_on=DROP, actual_hours=3, started_on="2026-10-01T22:00:00Z")
    await auto_picks(server, mock_gorelo, [contract(10, [line(901)], StartDate="2026-10-01")], 901, args=args)


async def test_started_on_given_wins_over_ended_on_minus_actual_hours(server, mock_gorelo):
    # all three given: the start is used as sent (Gorelo checks that they agree), here the 1st
    args = {**AUTO_BASE, "started_on": "2026-10-01T09:00:00Z", "ended_on": "2026-10-01T10:00:00Z", "actual_hours": 1}
    result = await auto_sends_none(server, mock_gorelo, [contract(10, [line(901)], StartDate="2026-10-03", name="Later")], args=args)
    assert "'Later' (outside its dates)" in result["note"]


def test_the_module_has_no_clock():
    import ast

    tree = ast.parse(Path(time_entries.__file__).read_text(encoding="utf-8"))
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            calls.add(f"{getattr(owner, 'id', getattr(owner, 'attr', '?'))}.{node.func.attr}")
    assert not calls & {"datetime.now", "datetime.utcnow", "date.today", "datetime.today"}, calls
    assert not hasattr(time_entries, "_utc_today")


def test_a_day_cannot_be_derived_without_started_on_or_both_other_fields():
    with pytest.raises(ValueError):
        time_entries._entry_day(None, "2026-10-01T10:00:00Z", None)
    with pytest.raises(ValueError):
        time_entries._entry_day(None, None, 1.5)



# --- no Hourly line found: the note says what the read-back shows -------------------


NO_CONTRACT = [contract(10, [line(900, terms=1), line(902, terms=4)])]


async def test_no_hourly_line_sends_none_and_a_billable_entry_with_no_line_says_it_bills_at_the_default_rate(server, mock_gorelo):
    result = await auto_sends_none(server, mock_gorelo, NO_CONTRACT)
    assert result == {
        **entry(501, ServiceLine=None),
        "note": NO_HOURLY_SENT + "; the entry has no service line and bills at the billing role's default rate.",
    }


async def test_no_hourly_line_and_gorelo_applied_a_line_by_its_own_rule_says_that_and_not_the_default_rate(server, mock_gorelo):
    record = entry(501, ServiceLine={"Id": 66, "Name": "Gorelo pick"})
    result = await auto_sends_none(server, mock_gorelo, NO_CONTRACT, record=record)
    assert result == {**record, "note": NO_HOURLY_SENT + "; Gorelo applied service line 'Gorelo pick' (Id 66) by its own rule."}
    assert "default rate" not in result["note"]


@pytest.mark.parametrize("status", [{"Id": 2, "Name": "No charge"}, {"Id": 3, "Name": "Non-billable"}])
async def test_no_hourly_line_and_an_entry_that_is_not_billable_says_nothing_about_rates(server, mock_gorelo, status):
    result = await auto_sends_none(server, mock_gorelo, NO_CONTRACT, record=entry(501, ServiceLine=None, BillableStatus=status))
    assert result["note"] == NO_HOURLY_SENT + "."
    assert "rate" not in result["note"]


@pytest.mark.parametrize("status", [None, {}, {"Id": True, "Name": "Billable"}, "Billable"])
async def test_no_hourly_line_and_a_billable_status_that_cannot_be_read_says_nothing_about_rates(server, mock_gorelo, status):
    result = await auto_sends_none(server, mock_gorelo, NO_CONTRACT, record=entry(501, ServiceLine=None, BillableStatus=status))
    assert result["note"] == NO_HOURLY_SENT + "."


async def test_no_hourly_line_and_a_record_with_no_service_line_key_makes_no_claim_about_the_rate_and_warns(server, mock_gorelo):
    record = entry(501)
    record.pop("ServiceLine")
    result = await auto_sends_none(server, mock_gorelo, NO_CONTRACT, record=record)
    assert result == {
        **record,
        "note": NO_HOURLY_SENT + "; the entry's ServiceLine could not be read from Gorelo's answer, so check it with get_time_entry.",
        "warning": FIELD_MISSING,
    }
    assert "rate" not in result["note"]


async def test_no_hourly_line_and_a_failed_reread_says_the_entry_could_not_be_read_back(server, mock_gorelo):
    on_auto(mock_gorelo, NO_CONTRACT)
    mock_gorelo.on("POST", "/v1/time-entries", envelope({"Id": 777}))
    mock_gorelo.on("GET", "/v1/time-entries/777", error_envelope(500, [("070001", "Internal error.")]))
    result = await call_tool(server, "create_time_entry", AUTO_BASE)
    assert set(result) == {"Id", "warning", "note"}  # the read-back warning is kept, the note rides beside it
    assert "re-reading it failed" in result["warning"] and "Do not repeat the write" in result["warning"]
    assert result["note"] == NO_HOURLY_SENT + "; the entry could not be read back, so check its ServiceLine with get_time_entry."
    assert "rate" not in result["note"]


@pytest.mark.parametrize("shown", ["Remote", 5, [], {}, {"Name": "no id"}, {"Id": "9"}, {"Id": True}])
async def test_no_hourly_line_and_a_service_line_that_cannot_be_read_says_so(server, mock_gorelo, shown):
    result = await auto_sends_none(server, mock_gorelo, NO_CONTRACT, record=entry(501, ServiceLine=shown))
    assert result["note"] == (
        NO_HOURLY_SENT + "; the entry's ServiceLine could not be read from Gorelo's answer, so check it with get_time_entry."
    )


@pytest.mark.parametrize("contracts", [[], [contract(10, [])]], ids=["no contract", "a contract with no lines"])
async def test_a_client_with_nothing_to_pick_gets_the_note_that_fits_the_record(server, mock_gorelo, contracts):
    result = await auto_sends_none(server, mock_gorelo, contracts)
    assert result["note"] == NO_HOURLY_SENT + "; the entry has no service line and bills at the billing role's default rate."


async def test_the_note_names_the_contracts_skipped_for_location_and_for_dates(server, mock_gorelo):
    contracts = [
        scoped(10, [line(901)], [8], name="Branch"),
        contract(11, [line(902)], name="Old", EndDate="2025-01-01"),
        contract(12, [line(903)], name="Future", StartDate="2027-01-01"),
        contract(13, [line(900, terms=1)], name="Unlimited"),
    ]
    result = await auto_sends_none(server, mock_gorelo, contracts, location_id=7)
    assert result["note"] == (
        NO_HOURLY_SENT
        + "; the entry has no service line and bills at the billing role's default rate."
        + " 3 Active contracts skipped because the contract does not cover the ticket's location or the entry's day: "
        "'Branch' (not for the ticket's location); 'Old' (outside its dates); 'Future' (outside its dates)."
    )


async def test_the_skipped_note_says_one_contract_in_the_singular_and_names_the_no_location_case(server, mock_gorelo):
    result = await auto_sends_none(server, mock_gorelo, [scoped(10, [line(900, terms=1)], [8], name="Branch")])
    assert result["note"].endswith(
        "1 Active contract skipped because the contract does not cover the ticket's location or the entry's day: "
        "'Branch' (limited to locations and the ticket has none)."
    )


async def test_the_skipped_note_lists_five_contracts_and_counts_the_rest(server, mock_gorelo):
    contracts = [contract(10 + n, [line(900 + n)], name=f"Old {n}", EndDate="2020-01-01") for n in range(7)]
    result = await auto_sends_none(server, mock_gorelo, contracts)
    assert "7 Active contracts skipped" in result["note"]
    assert "'Old 4' (outside its dates); and 2 more." in result["note"] and "Old 5" not in result["note"]


async def test_the_skipped_contracts_ride_on_the_note_of_every_record_shape(server, mock_gorelo):
    contracts = [contract(10, [line(901)], name="Old", EndDate="2020-01-01")]
    skipped = " 1 Active contract skipped because the contract does not cover the ticket's location or the entry's day: 'Old' (outside its dates)."
    result = await auto_sends_none(
        server, mock_gorelo, contracts, record=entry(501, ServiceLine=None, BillableStatus={"Id": 3, "Name": "Non-billable"})
    )
    assert result["note"] == NO_HOURLY_SENT + "." + skipped


async def test_a_contract_that_is_picked_makes_no_skipped_note(server, mock_gorelo):
    contracts = [contract(10, [line(901)]), contract(11, [line(902)], name="Old", EndDate="2020-01-01")]
    result = await auto_picks(server, mock_gorelo, contracts, 901)
    assert result == landed(901)


# --- a service line that was sent but did not stick -------------------------------------



@pytest.mark.parametrize(
    "record, shown",
    [
        (entry(501, ServiceLine=None), "no service line"),
        (entry(501, ServiceLine={"Id": 66, "Name": "Other line"}), "service line 'Other line' (Id 66)"),
        (entry(501, ServiceLine={"Id": 66}), "service line (no name) (Id 66)"),
        (entry(501, ServiceLine="Remote"), "a ServiceLine that cannot be read"),
        (entry(501, ServiceLine={"Name": "no id"}), "a ServiceLine that cannot be read"),
    ],
)
async def test_an_explicit_service_line_that_does_not_stick_adds_a_warning(server, mock_gorelo, record, shown):
    on_create(mock_gorelo, record=record)
    result = await call_tool(server, "create_time_entry", {**CREATE_BASE, "service_line_id": 901})
    assert mock_gorelo.calls("POST")[0].json["ServiceLineId"] == 901
    assert result == {**record, "warning": LOST.format(sent=901, shown=shown)}


async def test_an_explicit_service_line_without_a_ServiceLine_key_in_the_record_warns_that_it_could_not_be_checked(server, mock_gorelo):
    record = entry(501)
    record.pop("ServiceLine")
    on_create(mock_gorelo, record=record)
    result = await call_tool(server, "create_time_entry", {**CREATE_BASE, "service_line_id": 901})
    assert result == {**record, "warning": FIELD_MISSING_SENT.format(sent=901)}  # not "reads back with no service line"
    assert "no service line" not in result["warning"]


async def test_the_auto_pick_without_a_ServiceLine_key_in_the_record_warns_and_adds_no_note(server, mock_gorelo):
    on_auto(mock_gorelo, [contract(10, [line(900, terms=1), line(901)])])
    record = entry(501)
    record.pop("ServiceLine")
    on_create(mock_gorelo, record=record)
    result = await call_tool(server, "create_time_entry", AUTO_BASE)
    assert result == {**record, "warning": FIELD_MISSING_SENT.format(sent=901)}  # the "other labor lines" note needs the line in the record


async def test_no_service_line_true_with_a_record_that_has_no_service_line_key_still_says_it_could_not_be_checked(server, mock_gorelo):
    record = entry(501)
    record.pop("ServiceLine")
    on_create(mock_gorelo, record=record)
    result = await call_tool(server, "create_time_entry", {**CREATE_BASE, "no_service_line": True})
    assert result == {**record, "warning": FIELD_MISSING}


async def test_an_explicit_service_line_that_sticks_adds_nothing(server, mock_gorelo):
    on_create(mock_gorelo, record=landed(901))
    result = await call_tool(server, "create_time_entry", {**CREATE_BASE, "service_line_id": 901})
    assert result == landed(901)


async def test_an_explicit_service_line_on_a_task_entry_is_checked_the_same_way(server, mock_gorelo):
    on_create(mock_gorelo, record=entry(501, ServiceLine={"Id": 66, "Name": "Other line"}, Ticket=None, Task=reference(2)))
    result = await call_tool(server, "create_time_entry", make_args(CREATE_BASE, ticket_id=DROP, task_id=uid(2), service_line_id=901))
    assert result["warning"] == LOST.format(sent=901, shown="service line 'Other line' (Id 66)")


async def test_the_auto_pick_that_does_not_stick_adds_a_warning_and_no_note(server, mock_gorelo):
    on_auto(mock_gorelo, [contract(10, [line(901)])])
    on_create(mock_gorelo, record=entry(501, ServiceLine={"Id": 66, "Name": "Other line"}))
    result = await call_tool(server, "create_time_entry", AUTO_BASE)
    assert result == {**entry(501, ServiceLine={"Id": 66, "Name": "Other line"}), "warning": LOST.format(sent=901, shown="service line 'Other line' (Id 66)")}


async def test_a_pick_with_the_record_showing_none_warns_about_it(server, mock_gorelo):
    on_auto(mock_gorelo, [contract(10, [line(901)])])
    on_create(mock_gorelo, record=entry(501, ServiceLine=None))
    result = await call_tool(server, "create_time_entry", AUTO_BASE)
    assert result["warning"] == LOST.format(sent=901, shown="no service line") and "note" not in result


async def test_the_warning_is_appended_to_a_warning_the_record_already_has_never_replacing_it(server, mock_gorelo):
    record = entry(501, ServiceLine=None, warning="an earlier warning")
    on_create(mock_gorelo, record=record)
    result = await call_tool(server, "create_time_entry", {**CREATE_BASE, "service_line_id": 901})
    assert result["warning"] == "an earlier warning " + LOST.format(sent=901, shown="no service line")
    assert {k: v for k, v in result.items() if k != "warning"} == {k: v for k, v in record.items() if k != "warning"}


async def test_a_note_is_appended_to_a_note_the_record_already_has_never_replacing_it(server, mock_gorelo):
    record = entry(501, ServiceLine=None, note="an earlier note")
    on_create(mock_gorelo, record=record)
    result = await call_tool(server, "create_time_entry", CREATE_BASE)
    assert result["note"].startswith("an earlier note The entry has no service line")


async def test_a_failed_reread_with_a_line_sent_keeps_its_warning_and_adds_no_claim(server, mock_gorelo):
    mock_gorelo.on("POST", "/v1/time-entries", envelope({"Id": 777}))
    mock_gorelo.on("GET", "/v1/time-entries/777", error_envelope(500, [("070001", "Internal error.")]))
    result = await call_tool(server, "create_time_entry", {**CREATE_BASE, "service_line_id": 901})
    assert set(result) == {"Id", "warning"} and "re-reading it failed" in result["warning"]
    assert "was sent but" not in result["warning"]


# --- the note for a Billable entry with no service line and none for no_service_line -------


async def test_a_billable_entry_whose_record_has_no_service_line_key_gets_the_field_warning_and_no_rate_note(server, mock_gorelo):
    record = entry(501)
    record.pop("ServiceLine")
    on_create(mock_gorelo, record=record)
    result = await call_tool(server, "create_time_entry", CREATE_BASE)
    assert result == {**record, "warning": FIELD_MISSING}  # absent is not "none": no claim that it bills at the default rate


async def test_a_billable_ticket_entry_with_no_service_line_gets_a_note_beside_the_record(server, mock_gorelo):
    record = entry(501, ServiceLine=None)
    on_create(mock_gorelo, record=record)
    result = await call_tool(server, "create_time_entry", CREATE_BASE)
    assert result == {**record, "note": time_entries.NO_SERVICE_LINE_NOTE_TICKET}
    assert result["note"] == (
        "The entry has no service line and bills at the billing role's default rate. A client with a contract usually needs "
        "service_line_id (get_contract lists the lines) or auto_service_line=true."
    )
    assert "ServiceLineId" not in mock_gorelo.calls("POST")[0].json


async def test_a_billable_task_entry_with_no_service_line_is_told_only_service_line_id(server, mock_gorelo):
    record = entry(501, ServiceLine=None, Ticket=None, Task=reference(2))
    on_create(mock_gorelo, record=record)
    result = await call_tool(server, "create_time_entry", make_args(CREATE_BASE, ticket_id=DROP, task_id=uid(2)))
    assert result == {**record, "note": time_entries.NO_SERVICE_LINE_NOTE_TASK}
    assert result["note"] == (
        "The entry has no service line and bills at the billing role's default rate. A client with a contract usually needs "
        "service_line_id (get_contract lists the lines)."
    )
    assert "auto_service_line" not in result["note"]  # a task entry cannot use it: the tool refuses it with task_id


async def test_the_note_never_replaces_or_hides_a_field(server, mock_gorelo):
    record = entry(501, ServiceLine=None, Comment="kept")
    on_create(mock_gorelo, record=record)
    result = await call_tool(server, "create_time_entry", CREATE_BASE)
    assert {key: value for key, value in result.items() if key != "note"} == record


async def test_an_entry_with_a_service_line_gets_no_note(server, mock_gorelo):
    on_create(mock_gorelo)  # entry() has ServiceLine {Id 77}
    result = await call_tool(server, "create_time_entry", CREATE_BASE)
    assert "note" not in result and "warning" not in result


@pytest.mark.parametrize("status", [{"Id": 2, "Name": "No charge"}, {"Id": 3, "Name": "Non-billable"}])
async def test_an_entry_that_is_not_billable_gets_no_note(server, mock_gorelo, status):
    on_create(mock_gorelo, record=entry(501, ServiceLine=None, BillableStatus=status))
    result = await call_tool(server, "create_time_entry", CREATE_BASE)
    assert "note" not in result


@pytest.mark.parametrize("status", [None, {}, {"Id": True, "Name": "Billable"}, {"Name": "Billable"}, "Billable"])
async def test_a_billable_status_that_cannot_be_read_gets_no_note(server, mock_gorelo, status):
    on_create(mock_gorelo, record=entry(501, ServiceLine=None, BillableStatus=status))
    result = await call_tool(server, "create_time_entry", CREATE_BASE)
    assert "note" not in result


async def test_a_service_line_that_cannot_be_read_gets_no_note_when_nothing_was_sent(server, mock_gorelo):
    on_create(mock_gorelo, record=entry(501, ServiceLine="Remote"))
    result = await call_tool(server, "create_time_entry", CREATE_BASE)
    assert "note" not in result and "warning" not in result


async def test_no_service_line_true_gets_no_note(server, mock_gorelo):
    on_create(mock_gorelo, record=entry(501, ServiceLine=None))
    result = await call_tool(server, "create_time_entry", {**CREATE_BASE, "no_service_line": True})
    assert result == entry(501, ServiceLine=None)
    assert mock_gorelo.calls("POST")[0].json["ServiceLineId"] is None  # the explicit null still goes out


async def test_no_service_line_true_gets_no_note_even_when_gorelo_applies_a_line(server, mock_gorelo):
    on_create(mock_gorelo)  # the record shows a line although null was sent: reported as it is, with no claim of ours
    result = await call_tool(server, "create_time_entry", {**CREATE_BASE, "no_service_line": True})
    assert result == entry(501)


async def test_the_warning_shape_of_a_failed_reread_gets_no_note(server, mock_gorelo):
    mock_gorelo.on("POST", "/v1/time-entries", envelope({"Id": 777}))
    mock_gorelo.on("GET", "/v1/time-entries/777", error_envelope(500, [("070001", "Internal error.")]))
    result = await call_tool(server, "create_time_entry", CREATE_BASE)
    assert set(result) == {"Id", "warning"}


async def test_an_entry_gorelo_gave_a_line_by_its_own_rule_gets_no_note_when_auto_service_line_was_not_used(server, mock_gorelo):
    on_create(mock_gorelo, record=entry(501, ServiceLine={"Id": 66, "Name": "Gorelo's pick"}))
    result = await call_tool(server, "create_time_entry", CREATE_BASE)
    assert result == entry(501, ServiceLine={"Id": 66, "Name": "Gorelo's pick"})


def test_the_billable_id_is_the_billable_row_of_the_table():
    assert time_entries.BILLABLE_STATUSES[time_entries.BILLABLE_STATUS_ID] == "Billable"


# --- nothing is written when auto_service_line refuses ----------------------------------------


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(lambda m: m.on("GET", TICKET_PATH, error_envelope(404, [("070404", "Ticket not found.")])), id="ticket missing"),
        pytest.param(lambda m: on_auto(m, [], BillingOverride=None), id="override shape"),
        pytest.param(lambda m: on_auto(m, [], client_id=None), id="no client"),
        pytest.param(lambda m: on_auto(m, [contract(10, [line(901)], ClientId=1)]), id="other client"),
        pytest.param(lambda m: on_auto(m, [contract(10, [line(901)], Status={"Id": 2})]), id="expired"),
        pytest.param(lambda m: on_auto(m, [scoped(10, [line(901)], [7])]), id="scoped and no location"),
        pytest.param(lambda m: on_auto(m, [contract(10, [line(901)]), contract(11, [line(902)])]), id="several"),
        pytest.param(lambda m: on_auto(m, [contract(10, [line(901)], StartDate="soon")]), id="bad date"),
        pytest.param(lambda m: on_auto(m, [contract(10, [line(901)], StartDate="2026-10-02")]), id="margin only"),
        pytest.param(lambda m: on_auto(m, [contract(10, [line(901)], EndDate=DROP)]), id="no EndDate key"),
        pytest.param(lambda m: m.on("GET", TICKET_PATH, envelope(ticket_detail(LocationId=DROP))), id="no LocationId key"),
    ],
)
async def test_every_refusal_of_auto_service_line_makes_no_post_and_no_reread(server, mock_gorelo, setup):
    setup(mock_gorelo)
    await call_tool_error(server, "create_time_entry", AUTO_BASE)
    assert mock_gorelo.calls("POST") == []
    assert [path for _, path in request_log(mock_gorelo)] == [p for p in (TICKET_PATH, "/v1/contracts") if mock_gorelo.calls("GET", p)]


# --- purely local refusals cost no request, however the ticket is given ---------------------------------------------


@pytest.mark.parametrize("comment", ["", "   ", "\n\t"])
@pytest.mark.parametrize("ticket", [uid(1), "TCK-1234", "1234"], ids=["guid", "display number", "number"])
async def test_a_blank_comment_with_auto_service_line_sends_no_request_at_all(server, mock_gorelo, ticket, comment):
    text = await call_tool_error(server, "create_time_entry", {**AUTO_BASE, "ticket_id": ticket, "comment": comment})
    assert "comment: must not be empty or whitespace only" in text
    assert mock_gorelo.requests == []


async def test_a_blank_comment_is_refused_before_the_ticket_lookup_without_auto_service_line_too(server, mock_gorelo):
    text = await call_tool_error(server, "create_time_entry", {**CREATE_BASE, "ticket_id": "TCK-1234", "comment": " "})
    assert "comment: must not be empty or whitespace only" in text and mock_gorelo.requests == []


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        # ended_on in year 1 minus hours: the day before 0001-01-01 does not exist
        (dict(ended_on="0001-01-01T01:00:00Z", actual_hours=2), "ended_on and actual_hours: ended_on=0001-01-01T01:00:00Z minus actual_hours=2.0 falls outside the dates that can be represented"),
        # a duration no timedelta can hold
        (dict(ended_on="2026-10-01T10:00:00Z", actual_hours=1e300), "actual_hours: 1e+300 is too large to date the entry"),
        (dict(ended_on="2026-10-01T10:00:00Z", actual_hours=1e12), "actual_hours: 1000000000000.0 is too large to date the entry"),
    ],
)
@pytest.mark.parametrize("ticket", [uid(1), "TCK-1234"], ids=["guid", "display number"])
async def test_an_entry_day_that_cannot_be_computed_is_a_value_error_before_any_request(server, mock_gorelo, ticket, overrides, fragment):
    args = make_args(AUTO_BASE, started_on=DROP, ticket_id=ticket, **overrides)
    text = await call_tool_error(server, "create_time_entry", args)
    assert fragment in text and "OverflowError" not in text and "date value out of range" not in text
    assert mock_gorelo.requests == []


def test_entry_day_turns_an_overflow_into_a_value_error_that_names_the_value():
    with pytest.raises(ValueError, match="ended_on and actual_hours: ended_on=0001-01-01T01:00:00Z minus actual_hours=2 falls"):
        time_entries._entry_day(None, "0001-01-01T01:00:00Z", 2)
    with pytest.raises(ValueError, match="actual_hours: 1e\\+300 is too large"):
        time_entries._entry_day(None, "2026-10-01T10:00:00Z", 1e300)


async def test_a_huge_actual_hours_with_a_start_is_not_an_overflow_of_the_day_and_is_left_to_gorelo(server, mock_gorelo):
    # started_on dates the entry, so actual_hours is never added to a date; the day is the start's
    on_auto(mock_gorelo, [contract(10, [line(901)])])
    on_create(mock_gorelo, record=landed(901))
    args = make_args(AUTO_BASE, ended_on=DROP, actual_hours=1e300)
    await call_tool(server, "create_time_entry", args)
    assert mock_gorelo.calls("POST")[0].json["ActualHours"] == 1e300


async def test_a_failed_contract_read_stops_before_the_post(server, mock_gorelo):
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail()))
    mock_gorelo.on("GET", "/v1/contracts", error_envelope(500, [("070001", "Internal error.")]))
    text = await call_tool_error(server, "create_time_entry", AUTO_BASE)
    assert text.startswith("Gorelo rejected create_time_entry (HTTP 500")
    assert mock_gorelo.calls("POST") == []


async def test_a_missing_ticket_stops_auto_service_line_before_any_contract_read(server, mock_gorelo):
    mock_gorelo.on("GET", TICKET_PATH, error_envelope(404, [("070404", "Ticket not found.")]))
    text = await call_tool_error(server, "create_time_entry", AUTO_BASE)
    assert "404" in text
    assert request_log(mock_gorelo) == [("GET", TICKET_PATH)]


async def test_auto_service_line_false_reads_nothing_extra(server, mock_gorelo):
    on_create(mock_gorelo)
    await call_tool(server, "create_time_entry", {**CREATE_BASE, "auto_service_line": False})
    assert request_log(mock_gorelo) == [("POST", "/v1/time-entries"), ("GET", "/v1/time-entries/501")]


async def test_the_worst_case_request_count_of_auto_service_line_with_a_display_number(server, mock_gorelo, monkeypatch):
    # the lookup finds the ticket on its last allowed page, the contracts take two pages: 20 + 1 + 2 + POST + re-read
    from tools import _ticket_ref

    pages = _ticket_ref.LOOKUP_MAX_PAGES
    rows = [[ticket_row(50 + i, 90000 + i)] for i in range(pages - 1)] + [[ticket_row(1, 1234)]]
    mock_gorelo.on("GET", "/v1/tickets", paged_responder(rows))
    await auto_picks(
        server,
        mock_gorelo,
        [[contract(10, [line(900, terms=1)])], [contract(11, [line(901)])]],
        901,
        args={**AUTO_BASE, "ticket_id": "TCK-1234"},
    )
    assert len(mock_gorelo.requests) == pages + 1 + 2 + 1 + 1


async def test_the_new_texts_stay_within_the_limits_and_say_what_is_true(server_factory):
    listed = {t.name: t for t in await list_tools(server_factory())}
    create = listed["create_time_entry"]
    schema = create.inputSchema["properties"]["auto_service_line"]
    assert schema["type"] == "boolean" and schema["default"] is False
    assert len(schema["description"]) <= PARAM_DESCRIPTION_MAX
    for fragment in ("ticket's own service line", "Hourly", "Active contracts", "location", "entry's day", "ticket_id", "service_line_id", "no_service_line"):
        assert fragment in schema["description"], fragment
    text = " ".join(create.description.split())
    assert "auto_service_line=true" in text and "service_line_id" in text
    assert "the ticket's own line wins, else the Hourly line over other labor lines" in text  # the Hourly pick is made even beside other labor lines
    assert "A Billable entry reading back with no line gets a note; a line that did not stick, a warning." in text
    assert len(create.description) <= DESCRIPTION_MAX
    for name in ("service_line_id", "no_service_line"):
        assert len(create.inputSchema["properties"][name]["description"]) <= PARAM_DESCRIPTION_MAX


def test_every_note_and_warning_text_is_free_of_dashes_and_names_its_parameters_in_snake_case():
    constants = {
        name: value
        for name, value in vars(time_entries).items()
        if isinstance(value, str) and (name.startswith(("NO_", "SKIP_", "SHOWN_")) or name in ("OWN_LINE_NOTE", "OTHER_LABOR_NOTE", "LINE_LOST_WARNING"))
    }
    assert len(constants) >= 15 and {"OTHER_LABOR_NOTE", "NO_FIELD_WARNING", "NO_FIELD_SENT_WARNING"} <= set(constants)
    for name, value in constants.items():
        assert EM_DASH not in value and EN_DASH not in value, name
    assert "auto_service_line" in time_entries.NO_HOURLY_SENT
    assert "get_time_entry" in time_entries.NO_HOURLY_UNREAD and "get_time_entry" in time_entries.NO_HOURLY_UNREADABLE


# --------------------------------------------------------------------------
# update_time_entry
# --------------------------------------------------------------------------


async def test_update_time_entry_sends_only_the_given_fields_and_returns_the_patch_answer(server, mock_gorelo):
    updated = entry(55, ActualHours=2.25, AdjustedHours=2.25, BillableStatus={"Id": 2, "Name": "No charge"})
    mock_gorelo.on("PATCH", "/v1/time-entries/55", envelope(updated))
    result = await call_tool(server, "update_time_entry", {"time_entry_id": 55, "actual_hours": 2.25, "billable_status_id": 2})
    assert result == updated
    request = mock_gorelo.last
    assert (request.method, request.path, request.query) == ("PATCH", "/v1/time-entries/55", {})
    assert request.json == {"ActualHours": 2.25, "BillableStatusId": 2}
    assert len(mock_gorelo.requests) == 1  # PATCH answers with the entry: no re-read


@pytest.mark.parametrize(
    "param, value, body",
    [
        ("started_on", "2026-10-01T09:00:00-05:00", {"StartedOn": "2026-10-01T14:00:00Z"}),
        ("ended_on", "2026-10-01T16:00:00Z", {"EndedOn": "2026-10-01T16:00:00Z"}),
        ("actual_hours", 2.5, {"ActualHours": 2.5}),
        ("billable_status_id", 3, {"BillableStatusId": 3}),
        ("user_id", 9202, {"UserId": 9202}),
        ("billing_role_id", 8, {"BillingRoleId": 8}),
        ("work_type_id", 9, {"WorkTypeId": 9}),
        ("service_line_id", 77, {"ServiceLineId": 77}),
        ("comment", "New note", {"Comment": "New note"}),
        ("distance", 14.2, {"Distance": 14.2}),
        ("distance", 0, {"Distance": 0}),
    ],
)
async def test_each_update_field_is_sent_alone_under_its_pascal_case_name(server, mock_gorelo, param, value, body):
    mock_gorelo.on("PATCH", "/v1/time-entries/55", envelope(entry(55)))
    await call_tool(server, "update_time_entry", {"time_entry_id": 55, param: value})
    assert mock_gorelo.last.json == body


async def test_update_time_entry_can_send_every_field_at_once(server, mock_gorelo, spec_index):
    mock_gorelo.on("PATCH", "/v1/time-entries/55", envelope(entry(55)))
    args = {
        "time_entry_id": 55,
        "started_on": "2026-10-01T14:00:00Z",
        "ended_on": "2026-10-01T15:30:00Z",
        "actual_hours": 1.5,
        "billable_status_id": 1,
        "user_id": 9202,
        "billing_role_id": 3,
        "work_type_id": 4,
        "service_line_id": 77,
        "comment": "Fixed",
        "distance": 3.5,
    }
    await call_tool(server, "update_time_entry", args)
    body = mock_gorelo.last.json
    assert set(body) == set(spec_index.op("PATCH /v1/time-entries/{timeEntryId}").body["fields"])
    assert body["StartedOn"] == "2026-10-01T14:00:00Z" and body["ServiceLineId"] == 77 and body["Comment"] == "Fixed"
    assert "TicketId" not in body and "Attachments" not in body


async def test_remove_from_contract_sends_an_explicit_service_line_null(server, mock_gorelo):
    mock_gorelo.on("PATCH", "/v1/time-entries/55", envelope(entry(55, ServiceLine=None)))
    result = await call_tool(server, "update_time_entry", {"time_entry_id": 55, "remove_from_contract": True})
    request = mock_gorelo.last
    assert request.json == {"ServiceLineId": None}
    assert b'"ServiceLineId":null' in request.content.replace(b" ", b"")
    assert result["ServiceLine"] is None


async def test_clear_comment_sends_an_empty_string(server, mock_gorelo):
    mock_gorelo.on("PATCH", "/v1/time-entries/55", envelope(entry(55, Comment=None)))
    await call_tool(server, "update_time_entry", {"time_entry_id": 55, "clear_comment": True})
    assert mock_gorelo.last.json == {"Comment": ""}


async def test_both_flags_can_be_used_together_with_other_changes(server, mock_gorelo):
    mock_gorelo.on("PATCH", "/v1/time-entries/55", envelope(entry(55)))
    await call_tool(
        server,
        "update_time_entry",
        {"time_entry_id": 55, "remove_from_contract": True, "clear_comment": True, "billable_status_id": 2},
    )
    assert mock_gorelo.last.json == {"BillableStatusId": 2, "ServiceLineId": None, "Comment": ""}


async def test_false_flags_send_nothing(server, mock_gorelo):
    mock_gorelo.on("PATCH", "/v1/time-entries/55", envelope(entry(55)))
    await call_tool(
        server,
        "update_time_entry",
        {"time_entry_id": 55, "remove_from_contract": False, "clear_comment": False, "distance": 1},
    )
    assert mock_gorelo.last.json == {"Distance": 1}


UPDATE_CHANGE_NAMES = (
    "started_on, ended_on, actual_hours, billable_status_id, user_id, billing_role_id, work_type_id, "
    "service_line_id, comment, distance, remove_from_contract and clear_comment: give at least one of them"
)


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({}, UPDATE_CHANGE_NAMES),
        ({"remove_from_contract": False, "clear_comment": False}, UPDATE_CHANGE_NAMES),
        ({"time_entry_id": 0, "distance": 1}, "time_entry_id: expected a positive whole number such as 123, got zero"),
        ({"time_entry_id": -3, "distance": 1}, "time_entry_id: expected a positive whole number"),
        ({"time_entry_id": MAX_ID + 1, "distance": 1}, "time_entry_id: expected a positive whole number such as 123, got a number above"),
        ({"remove_from_contract": True, "service_line_id": 77}, "remove_from_contract: cannot be true together with service_line_id"),
        ({"clear_comment": True, "comment": "x"}, "clear_comment: cannot be true together with comment"),
        ({"clear_comment": True, "comment": ""}, "clear_comment: cannot be true together with comment"),
        ({"comment": ""}, "comment: must not be empty or whitespace only; to erase the note use clear_comment=true"),
        ({"comment": "  "}, "comment: must not be empty or whitespace only; to erase the note use clear_comment=true"),
        ({"started_on": "2026-10-01T14:00:00"}, "started_on: '2026-10-01T14:00:00' has no UTC offset"),
        ({"ended_on": "later"}, "ended_on: 'later' is not an ISO 8601 datetime"),
        (
            {"started_on": "2026-10-01T15:00:00Z", "ended_on": "2026-10-01T15:00:00Z"},
            "ended_on: must be later than started_on",
        ),
        (
            {"started_on": "2026-10-01T15:00:00Z", "ended_on": "2026-10-01T14:00:00Z"},
            "ended_on: must be later than started_on",
        ),
        ({"actual_hours": 0}, "Input should be greater than 0"),
        ({"actual_hours": -2}, "Input should be greater than 0"),
        ({"billable_status_id": 0}, "billable_status_id: must be one of 1 (Billable), 2 (No charge), 3 (Non-billable), got 0"),
        ({"billable_status_id": 9}, "billable_status_id: must be one of 1 (Billable), 2 (No charge), 3 (Non-billable), got 9"),
        ({"user_id": 0}, "user_id: expected a positive whole number"),
        ({"user_id": MAX_ID + 1}, "user_id: expected a positive whole number"),
        ({"billing_role_id": -1}, "billing_role_id: expected a positive whole number"),
        ({"billing_role_id": MAX_ID + 1}, "billing_role_id: expected a positive whole number"),
        ({"work_type_id": 0}, "work_type_id: expected a positive whole number"),
        ({"work_type_id": MAX_ID + 1}, "work_type_id: expected a positive whole number"),
        ({"service_line_id": 0}, "service_line_id: expected a positive whole number"),
        ({"service_line_id": MAX_ID + 1}, "service_line_id: expected a positive whole number"),
        ({"distance": -0.5}, "Input should be greater than or equal to 0"),
        ({"ticket_id": uid(1)}, "ticket_id"),
        ({"attachments": [{"name": "a", "url": "b"}]}, "attachments"),
    ],
)
async def test_update_time_entry_rejects_bad_input_before_any_http_call(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "update_time_entry", {"time_entry_id": 55, **args})
    assert fragment in text
    if "Input should be" in fragment:  # the bound is the schema's: the message names the parameter
        assert next(iter(args)) in text
    assert mock_gorelo.requests == []


async def test_update_time_entry_needs_the_entry_id(server, mock_gorelo):
    text = await call_tool_error(server, "update_time_entry", {"comment": "x"})
    assert "time_entry_id" in text and "Missing required argument" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "notification, detail",
    [
        (("070101", "The service line does not belong to the entry's client.", "ServiceLineId"),
         "service_line_id: The service line does not belong to the entry's client."),
        (("070101", "Unknown work type.", "WorkTypeId"), "work_type_id: Unknown work type."),
        (("070101", "Unknown user.", "UserId"), "user_id: Unknown user."),
        (("070101", "Must be later than the start.", "EndedOn"), "ended_on: Must be later than the start."),
        (("070101", "Must be greater than 0.", "ActualHours"), "actual_hours: Must be greater than 0."),
        (("070101", "Invalid billable status.", "BillableStatusId"), "billable_status_id: Invalid billable status."),
        (("070101", "Unknown role.", "BillingRoleId"), "billing_role_id: Unknown role."),
        (("070101", "Note too long.", "Comment"), "comment: Note too long."),
        (("070101", "Bad mileage.", "Distance"), "distance: Bad mileage."),
        (("070101", "Bad instant.", "StartedOn"), "started_on: Bad instant."),
    ],
)
async def test_update_time_entry_maps_a_gorelo_400_to_the_snake_case_parameter(server, mock_gorelo, notification, detail):
    mock_gorelo.on("PATCH", "/v1/time-entries/55", error_envelope(400, [notification]))
    text = await call_tool_error(server, "update_time_entry", {"time_entry_id": 55, "comment": "x"})
    assert text == rejected("update_time_entry", 400, "070101", detail)


async def test_update_time_entry_reports_the_409_for_an_entry_that_is_not_open(server, mock_gorelo):
    message = "Only open time entries can be changed."
    mock_gorelo.on("PATCH", "/v1/time-entries/55", error_envelope(409, [("070901", message)]))
    text = await call_tool_error(server, "update_time_entry", {"time_entry_id": 55, "comment": "x"})
    assert text == rejected("update_time_entry", 409, "070901", message)


async def test_update_time_entry_shows_the_409_for_an_invoiced_entry_as_a_plain_refusal(server, mock_gorelo):
    # Gorelo's own answer for an invoiced entry (the entry id here is invented)
    message = (
        "Time entry 55 cannot be changed because it has already been invoiced. "
        "Only a time entry that is still open can be changed."
    )
    mock_gorelo.on("PATCH", "/v1/time-entries/55", error_envelope(409, [("070206", message)]))
    text = await call_tool_error(server, "update_time_entry", {"time_entry_id": 55, "comment": "x"})
    assert text == rejected("update_time_entry", 409, "070206", message)
    assert "may have been applied" not in text and "Verify" not in text and "did not confirm" not in text
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("PATCH", "/v1/time-entries/55")]


async def test_update_time_entry_reports_a_404_for_the_id(server, mock_gorelo):
    mock_gorelo.on("PATCH", "/v1/time-entries/55", error_envelope(404, [("070404", "Time entry not found.", "timeEntryId")]))
    text = await call_tool_error(server, "update_time_entry", {"time_entry_id": 55, "comment": "x"})
    assert text == rejected("update_time_entry", 404, "070404", "time_entry_id: Time entry not found.")


async def test_a_timeout_on_the_patch_says_gorelo_did_not_confirm(server, mock_gorelo):
    mock_gorelo.on("PATCH", "/v1/time-entries/55", httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_time_entry", {"time_entry_id": 55, "comment": "x"})
    assert text.startswith("Gorelo did not confirm update_time_entry") and "Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "answer, kind",
    [
        (envelope(None), "null"),
        (envelope([1]), "a list of 1 item"),
        (envelope(True), "a boolean"),
        (envelope(False), "a boolean"),
        (envelope({}), "an empty object"),
    ],
)
async def test_a_patch_answer_that_is_not_a_record_raises_and_tells_the_model_to_verify(server, mock_gorelo, answer, kind):
    mock_gorelo.on("PATCH", "/v1/time-entries/55", answer)
    text = await call_tool_error(server, "update_time_entry", {"time_entry_id": 55, "comment": "x"})
    assert text.startswith("Gorelo returned an unexpected response for update_time_entry")
    assert f"expected Data to be a non-empty object but got {kind}" in text
    assert VERIFY_AFTER_SHAPE in text and text.lower().count("verify") == 1
    assert len(mock_gorelo.requests) == 1


# --------------------------------------------------------------------------
# list_billing_roles and list_work_types
# --------------------------------------------------------------------------

ROLES = [
    {"Id": 3, "Name": "Technician", "HourlyRate": 125.0, "Tax": {"Id": 1, "Name": "No tax"}, "CoaCode": None},
    {"Id": 8, "Name": "Engineer", "HourlyRate": 175.0, "Tax": {"Id": 1, "Name": "No tax"}, "CoaCode": "4010"},
]
WORK_TYPES = [
    {
        "Id": 4, "Name": "Remote", "BillableStatus": {"Id": 1, "Name": "Billable"}, "HourlyMultiplier": 1.0,
        "MinimumTimeInMinutes": 15, "IncrementTimeInMinutes": 15, "IsDefaultOutsideBusinessHours": False,
        "Tax": {"Id": 1, "Name": "No tax"}, "CoaCode": None,
    },
]


@pytest.mark.parametrize(
    "tool, path, rows",
    [("list_billing_roles", "/v1/billing-roles", ROLES), ("list_work_types", "/v1/work-types", WORK_TYPES)],
)
async def test_the_lookup_lists_are_plain_gets_without_paging_parameters(server, mock_gorelo, tool, path, rows):
    mock_gorelo.on("GET", path, envelope(rows))
    result = await call_tool(server, tool)
    assert result == {"items": rows, "count": len(rows)}
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", path, {}, None)
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "tool, path", [("list_billing_roles", "/v1/billing-roles"), ("list_work_types", "/v1/work-types")]
)
async def test_an_empty_lookup_is_an_explicit_empty_list(server, mock_gorelo, tool, path):
    mock_gorelo.on("GET", path, envelope([]))
    assert await call_tool(server, tool) == {"items": [], "count": 0}


@pytest.mark.parametrize(
    "tool, path", [("list_billing_roles", "/v1/billing-roles"), ("list_work_types", "/v1/work-types")]
)
async def test_the_lookup_lists_refuse_parameters_and_unexpected_shapes(server, mock_gorelo, tool, path):
    text = await call_tool_error(server, tool, {"page_size": 50})
    assert "page_size" in text and mock_gorelo.requests == []
    mock_gorelo.on("GET", path, envelope({"Id": 1}))
    text = await call_tool_error(server, tool)
    assert text.startswith(f"Gorelo returned an unexpected response for {tool}")
    assert "expected Data to be a list but got an object" in text


@pytest.mark.parametrize(
    "tool, path", [("list_billing_roles", "/v1/billing-roles"), ("list_work_types", "/v1/work-types")]
)
async def test_the_lookup_lists_report_a_gorelo_error(server, mock_gorelo, tool, path):
    mock_gorelo.on("GET", path, error_envelope(500, [("070001", "Internal error.")]))
    text = await call_tool_error(server, tool)
    assert text == rejected(tool, 500, "070001", "Internal error.")


# --------------------------------------------------------------------------
# delete_time_entry
# --------------------------------------------------------------------------


@pytest.mark.parametrize("args", [{}, {"confirm": False}])
async def test_delete_time_entry_refuses_without_confirm_and_makes_no_http_call(destructive_server, mock_gorelo, args):
    text = await call_tool_error(destructive_server, "delete_time_entry", {"time_entry_id": 55, **args})
    assert text.startswith("confirm: refusing to delete time entry 55 without confirm=true.")
    assert "reverts its billing ledger entries" in text and "needs a second delete" in text
    assert "Call again with confirm=true if you really want to delete time entry 55." in text
    assert mock_gorelo.requests == []


async def test_delete_time_entry_with_confirm_sends_one_delete_and_returns_the_outcome(destructive_server, mock_gorelo):
    mock_gorelo.on("DELETE", "/v1/time-entries/55", envelope({"Id": 55, "Outcome": "Deleted"}))
    result = await call_tool(destructive_server, "delete_time_entry", {"time_entry_id": 55, "confirm": True})
    assert result == {"Id": 55, "Outcome": "Deleted"}
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("DELETE", "/v1/time-entries/55", {}, None)
    assert len(mock_gorelo.requests) == 1


async def test_a_reopened_entry_needs_a_second_confirmed_delete(destructive_server, mock_gorelo):
    mock_gorelo.on(
        "DELETE",
        "/v1/time-entries/55",
        in_order(envelope({"Id": 55, "Outcome": "Reopened"}), envelope({"Id": 55, "Outcome": "Deleted"})),
    )

    async def delete():
        return await call_tool(destructive_server, "delete_time_entry", {"time_entry_id": 55, "confirm": True})

    assert await delete() == {"Id": 55, "Outcome": "Reopened"}
    assert await delete() == {"Id": 55, "Outcome": "Deleted"}
    assert [r.method for r in mock_gorelo.requests] == ["DELETE", "DELETE"]


async def test_the_tool_does_not_loop_on_reopened_by_itself(destructive_server, mock_gorelo):
    mock_gorelo.on("DELETE", "/v1/time-entries/55", envelope({"Id": 55, "Outcome": "Reopened"}))
    await call_tool(destructive_server, "delete_time_entry", {"time_entry_id": 55, "confirm": True})
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "status, notification, detail",
    [
        (409, ("070901", "Only open time entries can be deleted."), "Only open time entries can be deleted."),
        (409, ("070901", "The ticket is closed.", "timeEntryId"), "time_entry_id: The ticket is closed."),
        (404, ("070404", "Time entry not found.", "TimeEntryId"), "time_entry_id: Time entry not found."),
        (404, ("070404", "Time entry not found."), "Time entry not found."),
    ],
)
async def test_delete_time_entry_reports_gorelos_refusal(destructive_server, mock_gorelo, status, notification, detail):
    mock_gorelo.on("DELETE", "/v1/time-entries/55", error_envelope(status, [notification]))
    text = await call_tool_error(destructive_server, "delete_time_entry", {"time_entry_id": 55, "confirm": True})
    assert text == rejected("delete_time_entry", status, notification[0], detail)
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize("confirm", ["yes", "true", "1", 1, 0, None])
async def test_confirm_must_be_the_boolean_true_not_text_or_a_number(destructive_server, mock_gorelo, confirm):
    # pydantic would coerce "yes", "true" or 1 to True in lax mode; the parameter is strict so only JSON true passes
    text = await call_tool_error(destructive_server, "delete_time_entry", {"time_entry_id": 55, "confirm": confirm})
    assert "confirm" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", [0, -7, MAX_ID + 1])
async def test_delete_time_entry_rejects_a_bad_id_with_no_http_call(destructive_server, mock_gorelo, bad):
    text = await call_tool_error(destructive_server, "delete_time_entry", {"time_entry_id": bad, "confirm": True})
    assert text.startswith("time_entry_id: expected a positive whole number such as 123, got ")
    assert mock_gorelo.requests == []


async def test_delete_time_entry_exists_only_when_deletes_are_enabled(server_factory, mock_gorelo):
    off = server_factory(destructive=False)
    assert "delete_time_entry" not in {t.name for t in await list_tools(off)}
    text = await call_tool_error(off, "delete_time_entry", {"time_entry_id": 55, "confirm": True})
    assert "unknown tool" in text.lower()
    assert mock_gorelo.requests == []


async def test_a_timeout_on_the_delete_says_gorelo_did_not_confirm(destructive_server, mock_gorelo):
    mock_gorelo.on("DELETE", "/v1/time-entries/55", httpx.ReadTimeout("slow"))
    text = await call_tool_error(destructive_server, "delete_time_entry", {"time_entry_id": 55, "confirm": True})
    assert text.startswith("Gorelo did not confirm delete_time_entry") and "Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1  # a delete is never retried


OUTCOME_ADVICE = "The change may or may not have been applied. Verify with a read before retrying."


@pytest.mark.parametrize(
    "answer, fragment, advice",
    [
        (envelope({"Id": 55}), "Data has no Outcome (expected Deleted or Reopened)", OUTCOME_ADVICE),
        (envelope({"Id": 55, "Outcome": ""}), "Data has no Outcome", OUTCOME_ADVICE),
        (envelope({"Id": 55, "Outcome": None}), "Data has no Outcome", OUTCOME_ADVICE),
        (envelope({"Id": 55, "Outcome": 3}), "Data has no Outcome", OUTCOME_ADVICE),
        (envelope(True), "expected Data to be a non-empty object but got a boolean", VERIFY_AFTER_SHAPE),
        (envelope(False), "expected Data to be a non-empty object but got a boolean", VERIFY_AFTER_SHAPE),
        (envelope(None), "expected Data to be a non-empty object but got null", VERIFY_AFTER_SHAPE),
        (envelope({}), "expected Data to be a non-empty object but got an empty object", VERIFY_AFTER_SHAPE),
        (envelope([{"Id": 55}]), "expected Data to be a non-empty object but got a list of 1 item", VERIFY_AFTER_SHAPE),
    ],
)
async def test_a_delete_answer_without_an_outcome_raises_and_tells_the_model_to_verify(
    destructive_server, mock_gorelo, answer, fragment, advice
):
    mock_gorelo.on("DELETE", "/v1/time-entries/55", answer)
    text = await call_tool_error(destructive_server, "delete_time_entry", {"time_entry_id": 55, "confirm": True})
    assert text.startswith("Gorelo returned an unexpected response for delete_time_entry") and fragment in text
    assert advice in text and text.lower().count("verify") == 1
    assert len(mock_gorelo.requests) == 1  # a delete is never repeated by the tool


async def test_an_outcome_other_than_deleted_or_reopened_is_passed_through_unchanged(destructive_server, mock_gorelo):
    mock_gorelo.on("DELETE", "/v1/time-entries/55", envelope({"Id": 55, "Outcome": "SomethingNew"}))
    result = await call_tool(destructive_server, "delete_time_entry", {"time_entry_id": 55, "confirm": True})
    assert result == {"Id": 55, "Outcome": "SomethingNew"}


# --------------------------------------------------------------------------
# Strict ids: JSON true, "5" and 5.0 are refused instead of becoming an id
# --------------------------------------------------------------------------

UPDATE_BASE = {"time_entry_id": 55, "comment": "x"}

# (tool, arguments that are valid on their own, the id parameter under test, whether it is a list)
ID_PARAMS = [
    ("list_time_entries", {}, "client_ids", True),
    ("list_time_entries", {}, "location_ids", True),
    ("list_time_entries", {}, "user_ids", True),
    ("get_time_entry", {}, "time_entry_id", False),
    ("create_time_entry", CREATE_BASE, "user_id", False),
    ("create_time_entry", CREATE_BASE, "billable_status_id", False),
    ("create_time_entry", CREATE_BASE, "billing_role_id", False),
    ("create_time_entry", CREATE_BASE, "work_type_id", False),
    ("create_time_entry", CREATE_BASE, "service_line_id", False),
    ("update_time_entry", UPDATE_BASE, "time_entry_id", False),
    ("update_time_entry", UPDATE_BASE, "billable_status_id", False),
    ("update_time_entry", UPDATE_BASE, "user_id", False),
    ("update_time_entry", UPDATE_BASE, "billing_role_id", False),
    ("update_time_entry", UPDATE_BASE, "work_type_id", False),
    ("update_time_entry", UPDATE_BASE, "service_line_id", False),
    ("delete_time_entry", {"confirm": True}, "time_entry_id", False),
]
STRICT_ID_CASES = [pytest.param(*case, id=f"{case[0]}.{case[2]}") for case in ID_PARAMS]
# billable_status_id is a strict integer too, but it is checked against 1-3 and not as a Gorelo id
RANGE_ID_CASES = [pytest.param(*case, id=f"{case[0]}.{case[2]}") for case in ID_PARAMS if case[2] != "billable_status_id"]


@pytest.mark.parametrize("bad", [True, False, "5", 5.0, "abc"])
@pytest.mark.parametrize("tool, base, param, is_list", STRICT_ID_CASES)
async def test_every_id_parameter_refuses_json_true_text_and_floats(
    destructive_server, mock_gorelo, tool, base, param, is_list, bad
):
    text = await call_tool_error(destructive_server, tool, {**base, param: [bad] if is_list else bad})
    assert param in text and "valid integer" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "bad, what",
    [(0, "zero or a negative number"), (-3, "zero or a negative number"), (MAX_ID + 1, "a number above")],
)
@pytest.mark.parametrize("tool, base, param, is_list", RANGE_ID_CASES)
async def test_every_id_parameter_is_range_checked_by_name(
    destructive_server, mock_gorelo, tool, base, param, is_list, bad, what
):
    text = await call_tool_error(destructive_server, tool, {**base, param: [bad] if is_list else bad})
    named = f"{param}[0]" if is_list else param
    assert text.startswith(f"{named}: expected a positive whole number such as 123, got {what}")
    assert mock_gorelo.requests == []


# actual_hours and distance are strict numbers: FastMCP's lax mode would turn JSON true into 1.0 and false into 0.0, so a
# distance of false would be sent as 0 and a duration of true as one hour. These go through the MCP schema.
NOT_A_NUMBER = [pytest.param(True, id="json-true"), pytest.param(False, id="json-false"), pytest.param("1.5", id="text")]


@pytest.mark.parametrize("bad", NOT_A_NUMBER)
@pytest.mark.parametrize("param", ["actual_hours", "distance"])
async def test_create_time_entry_refuses_a_boolean_or_text_number_at_the_schema(server, mock_gorelo, param, bad):
    text = await call_tool_error(server, "create_time_entry", make_args(CREATE_BASE, **{param: bad}))
    assert param in text and "valid number" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", NOT_A_NUMBER)
@pytest.mark.parametrize("param", ["actual_hours", "distance"])
async def test_update_time_entry_refuses_a_boolean_or_text_number_at_the_schema(server, mock_gorelo, param, bad):
    text = await call_tool_error(server, "update_time_entry", {"time_entry_id": 55, param: bad})
    assert param in text and "valid number" in text
    assert mock_gorelo.requests == []


async def test_a_whole_number_of_hours_and_a_zero_distance_still_pass_the_strict_schema(server, mock_gorelo):
    on_create(mock_gorelo)
    await call_tool(server, "create_time_entry", make_args(CREATE_BASE, ended_on=DROP, actual_hours=2, distance=0))
    body = mock_gorelo.calls("POST")[0].json
    assert body["ActualHours"] == 2 and body["Distance"] == 0 and body["Distance"] is not False


def lax_int(tp):
    """True when `tp` is, or contains, an int that is not strict: a plain int takes JSON true as 1 and "5" as 5."""
    origin = typing.get_origin(tp)
    if origin is Annotated:
        inner, *metadata = typing.get_args(tp)
        if inner is int:
            return not any(isinstance(item, Strict) for item in metadata)
        return lax_int(inner)
    if origin in (typing.Union, types.UnionType, list):
        return any(lax_int(arg) for arg in typing.get_args(tp))
    return tp is int


def test_the_guard_for_lax_integers_sees_a_plain_int_and_accepts_the_strict_types():
    assert lax_int(Annotated[int, Field(description="x")])
    assert lax_int(Annotated[list[int] | None, Field(description="x")])
    assert lax_int(Annotated[int | None, Field(description="x")])
    assert not lax_int(Annotated[StrictId, Field(description="x")])
    assert not lax_int(Annotated[StrictId | None, Field(description="x")])
    assert not lax_int(Annotated[list[StrictId] | None, Field(description="x")])
    assert not lax_int(Annotated[str | None, Field(description="x")])


def test_every_integer_parameter_of_the_package_is_a_strict_id():
    # Rule: no integer id (single or list) is a plain int. page_size is a count, not an id.
    checked = []
    for spec in REGISTRY.specs:
        if spec.fn.__module__ not in MODULE_GROUP:
            continue
        for param, hint in typing.get_type_hints(spec.fn, include_extras=True).items():
            if param in ("return", "ctx", "page_size"):
                continue
            checked.append(f"{spec.name}.{param}")
            assert not lax_int(hint), f"{spec.name}.{param} is a plain int: type it StrictId"
    assert len(checked) > 50  # the guard did look at the parameters


async def test_an_id_list_that_is_not_a_list_is_refused(server, mock_gorelo):
    text = await call_tool_error(server, "list_time_entries", {"user_ids": 9201})
    assert "user_ids" in text and "valid list" in text
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# Whole-module guards
# --------------------------------------------------------------------------


async def test_each_tool_sends_exactly_the_operations_it_declares(server_factory, mock_gorelo):
    """Replay one happy-path call of each tool and compare the requests it made with its declared ops: it sends
    nothing undeclared and declares nothing it does not use."""
    server = server_factory(destructive=True)
    mock_gorelo.on("GET", "/v1/time-entries", paged_envelope([entry(1)]))
    mock_gorelo.on("GET", "/v1/time-entries/501", envelope(entry(501)))
    mock_gorelo.on("POST", "/v1/time-entries", envelope({"Id": 501}))
    mock_gorelo.on("PATCH", "/v1/time-entries/501", envelope(entry(501)))
    mock_gorelo.on("DELETE", "/v1/time-entries/501", envelope({"Id": 501, "Outcome": "Deleted"}))
    mock_gorelo.on("GET", "/v1/billing-roles", envelope(ROLES))
    mock_gorelo.on("GET", "/v1/work-types", envelope(WORK_TYPES))
    calls = {
        "list_time_entries": {},
        "get_time_entry": {"time_entry_id": 501},
        "create_time_entry": CREATE_BASE,
        "update_time_entry": {"time_entry_id": 501, "comment": "x"},
        "list_billing_roles": {},
        "list_work_types": {},
        "delete_time_entry": {"time_entry_id": 501, "confirm": True},
    }
    specs = module_specs()
    mock_gorelo.on("GET", "/v1/tickets", paged_envelope([ticket_row(1, 1234)]))
    mock_gorelo.on("GET", TICKET_PATH, envelope(ticket_detail()))
    mock_gorelo.on("GET", "/v1/contracts", paged_envelope([contract(10, [line(901)])]))
    # create_time_entry has three paths: a GUID (POST and re-read), a display number (the ticket lookup) and auto_service_line
    # (the ticket and the contracts); together they send exactly what it declares
    variants = {
        "create_time_entry": [
            CREATE_BASE,
            {**CREATE_BASE, "ticket_id": "TCK-1234"},
            {**CREATE_BASE, "auto_service_line": True},
        ]
    }
    for name, args in calls.items():
        mock_gorelo.reset()
        for variant in variants.get(name, [args]):
            await call_tool(server, name, variant)
        sent = {f"{r.method} {r.path}" for r in mock_gorelo.requests}
        declared = {op.replace("{timeEntryId}", "501").replace("{ticketId}", uid(1)) for op in specs[name].ops}
        assert sent == declared, (name, sent, declared)
