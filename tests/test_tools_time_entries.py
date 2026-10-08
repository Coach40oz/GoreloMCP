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
# the limit did not move.
MODULE_BUDGET_BYTES = 17900
VERIFY_AFTER_SHAPE = "the write may have been applied, so verify it with a read before repeating it"

# What the time entry tools must provide: name -> (kind, ops)
EXPECTED_TOOLS = {
    "list_time_entries": ("read", ["GET /v1/time-entries"]),
    "get_time_entry": ("read", ["GET /v1/time-entries/{timeEntryId}"]),
    "create_time_entry": ("write", ["POST /v1/time-entries", "GET /v1/time-entries/{timeEntryId}"]),
    "update_time_entry": ("write", ["PATCH /v1/time-entries/{timeEntryId}"]),
    "list_billing_roles": ("read", ["GET /v1/billing-roles"]),
    "list_work_types": ("read", ["GET /v1/work-types"]),
    "delete_time_entry": ("destructive", ["DELETE /v1/time-entries/{timeEntryId}"]),
}


def reference(n=1, number="2", title="Printer offline"):
    """A ticket or task reference as the published TimeEntryModel has it (NumberedReferenceModel): Id, Number, Title."""
    return {"Id": uid(n), "Number": number, "Title": title}


def entry(entry_id=501, **overrides):
    """A time entry as the live API returns it (PascalCase), verified by the probe of 2026-10-03: the published
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


def test_the_field_maps_cover_exactly_the_spec_fields(spec_index):
    create = spec_index.op("POST /v1/time-entries")
    assert set(time_entries.CREATE_FIELDS.values()) == set(create.body["fields"])
    update = spec_index.op("PATCH /v1/time-entries/{timeEntryId}")
    assert set(time_entries.UPDATE_FIELDS.values()) == set(update.body["fields"])
    listing = spec_index.op("GET /v1/time-entries")
    assert set(time_entries.LIST_QUERY.values()) | {"PageSize", "Cursor"} == set(listing.query_params)
    assert listing.paged and not spec_index.op("GET /v1/billing-roles").paged
    assert not spec_index.op("GET /v1/work-types").paged


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
        "it can draw down contract hours",
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


async def test_the_package_tool_list_stays_within_its_budget(server_factory):
    names = {spec.name for spec in REGISTRY.specs if spec.fn.__module__ in MODULE_GROUP}
    package = [t for t in await list_tools(server_factory(destructive=True)) if t.name in names]
    assert len(package) == len(names) == 12
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
        ({"ticket_ids": ["TCK-2029"]}, "ticket_ids[0]: expected a GUID such as"),
        ({"task_ids": ["not-a-guid"]}, "task_ids[0]: expected a GUID"),
        ({"ticket_ids": ["{" + uid(1) + "}"]}, "ticket_ids[0]: expected a GUID"),
        ({"ticket_ids": ["urn:uuid:" + uid(1)]}, "ticket_ids[0]: expected a GUID"),
        ({"ticket_ids": [" " + uid(1)]}, "ticket_ids[0]: expected a GUID"),  # nothing is trimmed or guessed
        ({"ticket_ids": [uid(1), "TCK-2029"]}, "ticket_ids[1]: expected a GUID"),
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
    # the probe of 2026-10-03: a record has exactly the keys of the published TimeEntryModel, and the user, ticket and
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
    # every spec field is reachable (TaskId through the task variant)
    assert set(body) | {"TaskId"} == set(spec_index.op("POST /v1/time-entries").body["fields"])


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
        ({"ticket_id": "TCK-2029"}, "ticket_id: expected a GUID"),
        ({"ticket_id": DROP, "task_id": "123"}, "task_id: expected a GUID"),
        ({"ticket_id": ""}, "ticket_id: expected a GUID"),
        ({"ticket_id": "{" + uid(1) + "}"}, "ticket_id: expected a GUID"),
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
        ({"actual_hours": 0}, "actual_hours: must be a number of decimal hours greater than 0"),
        ({"actual_hours": -1.5}, "actual_hours: must be a number of decimal hours greater than 0"),
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
        ({"distance": -1}, "distance: must be a distance of 0 or more"),
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
        ({"actual_hours": 0}, "actual_hours: must be a number of decimal hours greater than 0"),
        ({"actual_hours": -2}, "actual_hours: must be a number of decimal hours greater than 0"),
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
        ({"distance": -0.5}, "distance: must be a distance of 0 or more"),
        ({"ticket_id": uid(1)}, "ticket_id"),
        ({"attachments": [{"name": "a", "url": "b"}]}, "attachments"),
    ],
)
async def test_update_time_entry_rejects_bad_input_before_any_http_call(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "update_time_entry", {"time_entry_id": 55, **args})
    assert fragment in text
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
    for name, args in calls.items():
        mock_gorelo.reset()
        await call_tool(server, name, args)
        sent = {f"{r.method} {r.path}" for r in mock_gorelo.requests}
        declared = {op.replace("{timeEntryId}", "501") for op in specs[name].ops}
        assert sent == declared, (name, sent, declared)
