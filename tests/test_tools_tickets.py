"""tools/tickets.py: every ticket tool, offline (MockGorelo + an in-process FastMCP client).

Covers for each tool the exact method, path, query names and PascalCase body, the result shape, a Gorelo
error mapped to the snake_case parameter, every local validation error (with zero HTTP calls), strict ids
(JSON true, "5" and 5.0 are refused), the shape checks on the answer of a write, the read-before-write of a
partial billing override, and for the writes the re-read and its warning path. Fixtures use realistic
envelopes (PascalCase, IsSuccess, DataContext.Pagination) and the tenant ids used by the test fixtures
(statuses 1 New .. 4 Closed, types 7101 Incident, group 7201, users 9201 and 9202, clients 9102 and 9101).
"""

import ast
import json
import re
import typing
from pathlib import Path

import httpx
import pytest
from conftest import (
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
    uid,
)
from fastmcp.exceptions import ToolError

import tools._common as common
import tools.tickets as tickets  # importing it also imports the package, which registers every tool
from gorelo_client import FORBIDDEN_OPS
from pydantic import Strict
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parent.parent
EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)

LIST = "/v1/tickets"
STATUSES_PATH = "/v1/tickets/statuses"
TYPES_PATH = "/v1/tickets/types"
TAGS_PATH = "/v1/tickets/tags"
T1, T2, T3 = uid(1), uid(2), uid(3)

# What the shared positive_id / positive_ids / guid / guids helpers say (tools/_common.py).
NOT_POSITIVE = "expected a positive whole number such as 123, got zero or a negative number"
NO_IDS = "expected at least one id, got an empty list"
NOT_A_GUID = "expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got text that is not a GUID"


# --------------------------------------------------------------------------
# Fixtures and builders
# --------------------------------------------------------------------------


@pytest.fixture
def server(server_factory):
    return server_factory()


def ticket_row(n, **over):
    """A TicketListItemModel as the list endpoint returns it (PascalCase, nulls present)."""
    row = {
        "Id": uid(n),
        "Title": f"Ticket {n}",
        "Number": 2000 + n,
        "DisplayNumber": f"TCK-{2000 + n}",
        "ClientId": 9102,
        "LocationId": None,
        "ContactId": None,
        "CcContactIds": [],
        "LeadAssigneeId": None,
        "AssistingAssigneeIds": [],
        "WatcherIds": [],
        "GroupIds": [7201],
        "PrimaryGroupId": 7201,
        "Status": {"Id": 1, "Name": "New"},
        "StatusUpdatedOn": "2026-10-01T12:00:00Z",
        "StatusReason": "",
        "Priority": {"Id": 3, "Name": "Normal"},
        "Source": {"Id": 6, "Name": "Api"},
        "Type": {"Id": 7101, "Name": "Incident"},
        "TagIds": [],
        "ChecklistSummary": {"Completed": 0, "Total": 0},
        "IsUnread": False,
        "IsWaitingOnThem": False,
        "IsMerged": False,
        "MergedIntoTicketId": None,
        "MergedTicketIds": [],
        "Sla": {"FirstResponse": {"ElapsedBusinessMinutes": None}},
        "LastUpdate": {"On": "2026-10-01T12:00:00Z", "Summary": "", "UpdateType": ""},
        "CreatedOn": "2026-10-01T12:00:00Z",
        "UpdatedOn": "2026-10-01T12:00:00Z",
        "ClosedOn": None,
    }
    row.update(over)
    return row


def ticket_detail(n, **over):
    """A TicketDetailModel: the list fields plus Description, linked assets, Time, Products, BillingOverride..."""
    row = ticket_row(n)
    row.update(
        {
            "Description": "The 3rd floor printer is offline.",
            "AgentAssetIds": [],
            "CustomAssetIds": [],
            "UptimeIds": [],
            "Time": {"ActualHours": 0.0, "AdjustedHours": 0.0, "Breakdown": {}},
            "Products": {"Count": 0, "TotalAmount": 0.0},
            "BillingOverride": {
                "ServiceLine": {"Id": 0, "Name": ""},
                "BillingRole": {"Id": 0, "Name": ""},
                "WorkType": {"Id": 0, "Name": ""},
                "BillableStatus": {"Id": 0, "Name": ""},
            },
            "Shipments": [],
            "Banner": None,
        }
    )
    row.update(over)
    return row


def status_row(status_id, name, base, **over):
    """A StatusListItemModel: since contract e15cb5a18ec2 the base status is BaseStatus {Id, Name} (BaseStatusId is gone)."""
    row = {
        "Id": status_id, "Name": name, "Description": "", "Color": "#888", "SortOrder": status_id,
        "BaseStatus": {"Id": base, "Name": f"Base status {base}"}, "AskForReason": False,
        "CreatedOn": "2020-01-01T00:00:00Z", "UpdatedOn": None,
    }
    row.update(over)
    return row


def type_row(type_id, name, **over):
    row = {"Id": type_id, "Name": name, "Description": "", "IsAiType": False, "CreatedOn": None, "UpdatedOn": None}
    row.update(over)
    return row


def tag_row(tag_id, name, **over):
    row = {"Id": tag_id, "Name": name, "Description": "", "IsAiTag": False, "CreatedOn": None, "UpdatedOn": None}
    row.update(over)
    return row


STATUSES = [
    status_row(1, "New", 1),
    status_row(2, "In Progress", 2),
    status_row(3, "Solved", 3),
    status_row(4, "Closed", 4),
    status_row(6, "On Hold", 2, AskForReason=True),
    status_row(7301, "Awaiting Reply", 2),
]
TYPES = [
    type_row(7101, "Incident"),
    type_row(9301, "Request"),
    type_row(9302, "Maintenance"),
    type_row(9303, "Admin"),
    type_row(9304, "Other"),
]
TAGS = [tag_row(5, "VIP"), tag_row(6, "Onsite", IsAiTag=True)]


def lookups(mock, statuses=STATUSES, types=TYPES):
    mock.on("GET", STATUSES_PATH, envelope(statuses))
    mock.on("GET", TYPES_PATH, envelope(types))


def ids_of(result):
    return [row["Id"] for row in result["items"]]


def no_nulls(value):
    """True when no null appears anywhere in a request body (a PATCH null counts as absent)."""
    if isinstance(value, dict):
        return all(item is not None and no_nulls(item) for item in value.values())
    if isinstance(value, list):
        return all(no_nulls(item) for item in value)
    return True


def mine():
    return {spec.name: spec for spec in REGISTRY.specs if spec.fn.__module__ == "tools.tickets"}


# --------------------------------------------------------------------------
# Declarations: names, kinds, toolsets, ops, parameters, spec conformance
# --------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "list_tickets": ("read", ["GET /v1/tickets"]),
    "search_tickets": ("read", ["GET /v1/tickets", "GET /v1/tickets/statuses", "GET /v1/tickets/types"]),
    "get_ticket": ("read", ["GET /v1/tickets/{ticketId}", "GET /v1/tickets"]),
    "create_ticket": ("write", ["POST /v1/tickets", "GET /v1/tickets/{ticketId}"]),
    "update_ticket": ("write", ["PATCH /v1/tickets/{ticketId}", "GET /v1/tickets/{ticketId}"]),
    "list_ticket_statuses": ("read", ["GET /v1/tickets/statuses"]),
    "list_ticket_types": ("read", ["GET /v1/tickets/types"]),
    "list_ticket_tags": ("read", ["GET /v1/tickets/tags"]),
    "list_ticket_priorities": ("read", ["GET /v1/tickets"]),
    "list_ticket_sources": ("read", ["GET /v1/tickets"]),
}

EXPECTED_PARAMS = {
    "list_tickets": [
        "status_ids", "client_ids", "priority_ids", "type_ids", "lead_assignee_ids", "contact_ids", "tag_ids",
        "group_ids", "query", "updated_since", "updated_before", "created_since", "created_before", "sort_by",
        "sort_order", "page_size", "cursor",
    ],
    "search_tickets": [
        "client_id", "status", "priority", "type", "query", "updated_since", "created_since", "unassigned_only",
        "awaiting_client", "unread_only", "exclude_closed", "limit",
    ],
    "get_ticket": ["ticket_id"],
    "create_ticket": [
        "title", "description", "client_id", "status_id", "type_id", "priority_id", "source_id", "group_id",
        "contact_id", "cc_contact_ids", "location_id", "lead_assignee_id", "assisting_assignee_ids", "watcher_ids",
        "tag_ids", "agent_asset_ids", "custom_asset_ids", "uptime_ids", "created_on", "updated_on", "closed_on",
        "created_by_name", "is_unread", "send_created_email",
    ],
    "update_ticket": [
        "ticket_id", "title", "status_id", "priority_id", "type_id", "client_id", "location_id", "contact_id",
        "cc_contact_ids", "group_ids", "lead_assignee_id", "assisting_assignee_ids", "watcher_ids", "tag_ids",
        "agent_asset_ids", "custom_asset_ids", "uptime_ids", "closed_on", "updated_on", "updated_by_name",
        "billing_service_line_id", "billing_role_id", "billing_work_type_id", "billable_status_id", "clear_fields",
    ],
    "list_ticket_statuses": [],
    "list_ticket_types": [],
    "list_ticket_tags": [],
    "list_ticket_priorities": [],
    "list_ticket_sources": [],
}


def test_the_module_registers_exactly_the_documented_tools_with_their_kinds_toolsets_and_ops():
    registered = mine()
    assert sorted(registered) == sorted(EXPECTED_TOOLS)
    for name, (kind, ops) in EXPECTED_TOOLS.items():
        spec = registered[name]
        assert (spec.kind, spec.toolset, spec.ops) == (kind, "tickets", ops), name


def test_no_ticket_tool_can_delete_and_none_is_destructive():
    for name, spec in mine().items():
        assert spec.kind != "destructive", name
        assert all(op.split(" ", 1)[0] in ("GET", "POST", "PATCH") for op in spec.ops), name
        assert not set(spec.ops) & FORBIDDEN_OPS, name
    assert "DELETE /v1/tickets/{ticketId}" in FORBIDDEN_OPS


def test_only_update_ticket_carries_the_destructive_hint():
    hints = {name: spec.destructive_hint for name, spec in mine().items()}
    assert hints.pop("update_ticket") is True
    assert not any(hints.values()), hints


async def test_the_tools_a_client_sees_have_the_documented_parameters_defaults_and_annotations(server):
    seen = {tool.name: tool for tool in await list_tools(server) if tool.name in EXPECTED_TOOLS}
    assert sorted(seen) == sorted(EXPECTED_TOOLS)
    for name, params in EXPECTED_PARAMS.items():
        schema = seen[name].inputSchema
        assert list(schema["properties"]) == params, name
        for param, definition in schema["properties"].items():
            assert definition.get("description"), f"{name}.{param} needs a description"
    required = {name: seen[name].inputSchema.get("required", []) for name in EXPECTED_TOOLS}
    assert required["create_ticket"] == ["title", "description", "client_id", "status_id", "type_id", "priority_id", "source_id", "group_id"]
    assert required["update_ticket"] == ["ticket_id"] and required["get_ticket"] == ["ticket_id"]
    assert required["list_tickets"] == required["search_tickets"] == []
    props = {name: seen[name].inputSchema["properties"] for name in EXPECTED_TOOLS}
    assert props["list_tickets"]["page_size"]["default"] == 50
    assert props["search_tickets"]["limit"]["default"] == 50
    assert props["create_ticket"]["send_created_email"]["default"] is False
    # The advertised schema is compacted (server.compact_input_schema, see test_schema_compaction.py): an optional
    # parameter shows its real type directly, with no anyOf null branch and no "default": null.
    sort_by = props["list_tickets"]["sort_by"]
    sort_order = props["list_tickets"]["sort_order"]
    assert sort_by["enum"] == ["updatedOn", "createdOn"] and sort_order["enum"] == ["asc", "desc"]
    assert "default" not in sort_by and "default" not in sort_order
    assert {t["type"] for t in props["search_tickets"]["status"]["anyOf"]} == {"string", "integer"}
    for name, tool in seen.items():
        advertised = json.dumps(tool.inputSchema)
        assert '"type": "null"' not in advertised and '"default": null' not in advertised, name
    annotations = {name: tool.annotations for name, tool in seen.items()}
    assert annotations["update_ticket"].destructiveHint is True and annotations["update_ticket"].readOnlyHint is False
    assert annotations["create_ticket"].destructiveHint is False and annotations["create_ticket"].readOnlyHint is False
    for name in EXPECTED_TOOLS:
        if EXPECTED_TOOLS[name][0] == "read":
            assert annotations[name].readOnlyHint is True and annotations[name].destructiveHint is False, name


async def test_the_tickets_toolset_exposes_these_tools_and_the_core_toolset_does_not(server_factory):
    with_tickets = {tool.name for tool in await list_tools(server_factory(toolsets={"tickets"}))}
    assert set(EXPECTED_TOOLS) <= with_tickets
    without = {tool.name for tool in await list_tools(server_factory(toolsets={"core"}))}
    assert not set(EXPECTED_TOOLS) & without


def test_the_field_maps_name_real_spec_fields_and_cover_every_field(spec_index):
    create = spec_index.op("POST /v1/tickets")
    assert set(tickets.CREATE_FIELDS.values()) == set(create.body["fields"])
    update = spec_index.op("PATCH /v1/tickets/{ticketId}")
    billing = spec_index.schema("TicketBillingOverride")["fields"]
    expected = {name for name in update.body["fields"] if name != "BillingOverride"}
    expected |= {f"BillingOverride.{name}" for name in billing}
    assert set(tickets.UPDATE_FIELDS.values()) == expected
    listing = spec_index.op("GET /v1/tickets")
    assert set(tickets.LIST_QUERY_FIELDS.values()) | {"PageSize", "Cursor"} == set(listing.query_params)
    assert set(tickets.LIST_ERROR_FIELDS.values()) == set(listing.query_params)
    assert set(tickets.SEARCH_ERROR_FIELDS.values()) <= {"ClientIds", "StatusIds", "PriorityIds", "TypeIds", "Query", "UpdatedSince", "CreatedSince"}
    assert listing.paged and not spec_index.op("GET /v1/tickets/statuses").paged
    assert set(tickets.CLEARABLE_FIELDS) <= set(tickets.UPDATE_FIELDS)
    assert "BillingOverride.ContractServiceId" not in tickets.UPDATE_FIELDS.values()


def test_the_constant_tables_match_the_spec_enums(spec_index):
    assert [pid for pid, _ in tickets.PRIORITIES] == spec_index.schema("TicketPriority")["enum"] == [0, 1, 2, 3, 4]
    assert [name for _, name in tickets.PRIORITIES] == ["None", "Urgent", "High", "Normal", "Low"]
    assert [sid for sid, _ in tickets.SOURCES] == spec_index.schema("TicketSource")["enum"] == [1, 2, 3, 4, 5, 6]
    assert [name for _, name in tickets.SOURCES] == ["Web", "Email", "Phone", "Chat", "Alert", "Api"]
    assert tickets.BILLABLE_STATUSES == ((1, "Billable"), (2, "No charge"), (3, "Non-billable"))
    assert tickets.QUERY_MAX_CHARS == 200 and tickets.TITLE_MAX_CHARS == 250


def test_the_docstrings_carry_what_the_documentation_requires():
    doc = {name: " ".join((spec.fn.__doc__ or "").split()) for name, spec in mine().items()}
    for fragment in (
        "IsWaitingOnThem (renamed from IsAwaitingClient)",
        "Sla.FirstResponse.ElapsedBusinessMinutes",
        "ChecklistSummary",
        "ContactId (null without a primary contact)",
        "Description, time and billing are only in get_ticket",
        "use search_tickets",
        "Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.",
    ):
        assert fragment in doc["list_tickets"], fragment
    for fragment in (
        "awaiting_client, unassigned_only, unread_only and exclude_closed are applied to the rows read",
        "complete_scan",
        "truncated",
        "total_count",
        "count_mismatch",
        "matched",
        "scanned",
        "narrow the filters",
    ):
        assert fragment in doc["search_tickets"], fragment
    assert f"reads up to {tickets.SEARCH_SCAN_CAP} tickets" in doc["search_tickets"]
    assert f"raise limit (max {tickets.SEARCH_LIMIT_MAX})" in doc["search_tickets"]
    # prefer the GUID or the display number; a bare number scans several pages, and the scan is capped
    for fragment in (
        "Description", "Time", "Products", "BillingOverride", "Shipments", "Banner",
        "Prefer the GUID or the display number (TCK-1234)",
        "A bare number (1234) is searched with Query and can scan several pages",
        f"capped at {tickets.LOOKUP_MAX_PAGES} pages of {tickets.LOOKUP_PAGE_SIZE} tickets",
        "no match or several matches is an error",
    ):
        assert fragment in doc["get_ticket"], fragment
    # (create) and the keep list: no delete, ids first, automation, email, backdating, do not repeat
    for fragment in (
        "This server cannot delete tickets",
        "Resolve every id first",
        "never guess",
        "runs the tenant's ticket automation, which may still notify contacts",
        "send_created_email=true also emails the contact",
        "updated_on and closed_on require created_on",
        "{Id, warning}: the ticket WAS created, do not create it again",
    ):
        assert fragment in doc["create_ticket"], fragment
    # and update, and the keep list: billing keeps, lists replace, warnings, side effects, do not repeat
    for fragment in (
        "billing_* fields you leave out keep their current values",
        "changing client_id resets the contact, CCs and billing override and unlinks assets and uptime checks",
        "send new ones in the same call",
        "closed_on without updated_on also sets UpdatedOn",
        "a status change may email the contact",
        "notifications and timeline entries fire for each changed field",
        "REPLACE the whole list",
        "name it in clear_fields",
        "cannot be unassigned",
        "{Id, warning}: the update WAS applied, do not repeat it",
        # live, 2026-10-02: one technician cannot be lead (or assisting) and watcher in the same ticket
        "A technician cannot be lead (or assisting) and watcher at once.",
    ):
        assert fragment in doc["update_ticket"], fragment
    assert "0 None, 1 Urgent, 2 High, 3 Normal, 4 Low" in doc["list_ticket_priorities"]
    assert "The same ids work for create, update, filters and reads" in doc["list_ticket_priorities"]
    assert "1 Web, 2 Email, 3 Phone, 4 Chat, 5 Alert, 6 Api" in doc["list_ticket_sources"]
    assert "Id 7 appears on older tickets with no name and cannot be set" in doc["list_ticket_sources"]
    for name in ("list_ticket_statuses", "list_ticket_types", "list_ticket_tags"):
        assert "Id, Name" in doc[name] and "Returns {items, count}" in doc[name], name
    for name, spec in mine().items():  # the template line: a read tool says it has no side effects
        assert ("Side effects: None (read-only)." in doc[name]) == (spec.kind == "read"), name
    for text in doc.values():
        assert text.isascii() and EM_DASH not in text and EN_DASH not in text


# Where each id parameter says to resolve it (the tool names are what the model calls first).
RESOLVERS = {
    "list_tickets": {
        "status_ids": "list_ticket_statuses", "client_ids": "list_clients", "type_ids": "list_ticket_types",
        "lead_assignee_ids": "list_org_users", "contact_ids": "list_contacts", "tag_ids": "list_ticket_tags",
        "group_ids": "list_org_groups", "priority_ids": "list_ticket_priorities",
    },
    "search_tickets": {"client_id": "list_clients", "status": "list_ticket_statuses", "type": "list_ticket_types"},
    "create_ticket": {
        "client_id": "list_clients", "status_id": "list_ticket_statuses", "type_id": "list_ticket_types",
        "group_id": "list_org_groups", "contact_id": "list_contacts", "cc_contact_ids": "list_contacts",
        "location_id": "list_client_locations", "lead_assignee_id": "list_org_users",
        "assisting_assignee_ids": "list_org_users", "watcher_ids": "list_org_users", "tag_ids": "list_ticket_tags",
        "agent_asset_ids": "list_agents", "custom_asset_ids": "list_custom_assets", "uptime_ids": "list_uptime_checks",
    },
    "update_ticket": {
        "status_id": "list_ticket_statuses", "type_id": "list_ticket_types", "client_id": "list_clients",
        "location_id": "list_client_locations", "contact_id": "list_contacts", "cc_contact_ids": "list_contacts",
        "group_ids": "list_org_groups", "lead_assignee_id": "list_org_users",
        "assisting_assignee_ids": "list_org_users", "watcher_ids": "list_org_users", "tag_ids": "list_ticket_tags",
        "agent_asset_ids": "list_agents", "custom_asset_ids": "list_custom_assets", "uptime_ids": "list_uptime_checks",
        "billing_service_line_id": "list_contracts", "billing_role_id": "list_billing_roles",
        "billing_work_type_id": "list_work_types", "ticket_id": "get_ticket",
    },
}


async def test_every_id_parameter_names_the_tool_that_resolves_it(server):
    registered = {spec.name for spec in REGISTRY.specs}
    props = {tool.name: tool.inputSchema["properties"] for tool in await list_tools(server) if tool.name in RESOLVERS}
    for tool, params in RESOLVERS.items():
        for param, resolver in params.items():
            assert resolver in registered, (tool, param, resolver)
            assert resolver in props[tool][param]["description"], (tool, param)


async def test_the_parameter_descriptions_carry_the_warnings_and_the_scales(server):
    props = {tool.name: tool.inputSchema["properties"] for tool in await list_tools(server) if tool.name in EXPECTED_TOOLS}
    describe = {name: {p: d["description"] for p, d in params.items()} for name, params in props.items()}
    # false suppresses only the ticket-created email
    send = describe["create_ticket"]["send_created_email"]
    assert "True emails the contact a ticket-created message" in send
    assert "False (default) suppresses only that email: tenant automation rules may still notify contacts" in send
    assert "sends nothing" not in send
    # the client_id warning lists everything the move resets or unlinks
    client = describe["update_ticket"]["client_id"]
    for part in ("resets contact, CCs, billing", "unlinks assets, uptime checks"):
        assert part in client, part
    # lists replace; the scales are spelled out where a wrong guess costs something
    for name in ("cc_contact_ids", "group_ids", "assisting_assignee_ids", "watcher_ids", "tag_ids", "agent_asset_ids", "custom_asset_ids", "uptime_ids"):
        assert "Complete new" in describe["update_ticket"][name] and "replaces" in describe["update_ticket"][name], name
    assert "except group_ids" in describe["update_ticket"]["clear_fields"]
    assert "0 None, 1 Urgent, 2 High, 3 Normal, 4 Low" in describe["create_ticket"]["priority_id"]
    # the filters carry the same priority scale (it had shrunk to "0-4"): a wrong id filters the wrong tickets
    scale = "0 None, 1 Urgent, 2 High, 3 Normal, 4 Low"
    assert scale in describe["update_ticket"]["priority_id"]
    assert describe["list_tickets"]["priority_ids"] == f"Priority ids: {scale} (list_ticket_priorities)"
    assert describe["search_tickets"]["priority"] == f"Name or id: {scale}"
    assert "1 Web, 2 Email, 3 Phone, 4 Chat, 5 Alert, 6 Api" in describe["create_ticket"]["source_id"]
    assert "1 Billable, 2 No charge, 3 Non-billable" in describe["update_ticket"]["billable_status_id"]
    assert "cannot unassign" in describe["update_ticket"]["lead_assignee_id"]
    # live, 2026-10-02: a technician holds one role: Gorelo answers 400 "Technician already exists" for lead or assisting plus watcher
    conflict = 'Gorelo answers 400 "Technician already exists"'
    assert "Not also a watcher: " + conflict in describe["update_ticket"]["lead_assignee_id"]
    assert "Not also a watcher: " + conflict in describe["update_ticket"]["assisting_assignee_ids"]
    assert "Not also lead or assisting: " + conflict in describe["update_ticket"]["watcher_ids"]
    assert "needs created_on and a closed status_id" in describe["create_ticket"]["closed_on"]
    assert "needs a closed status" in describe["update_ticket"]["closed_on"]


def test_the_module_is_plain_ascii_without_dashes():
    for path in (REPO_ROOT / "tools" / "tickets.py", Path(__file__)):
        source = path.read_text(encoding="utf-8")
        assert source.isascii(), path.name
        assert EM_DASH not in source and EN_DASH not in source, path.name


# --------------------------------------------------------------------------
# list_ticket_priorities and list_ticket_sources: local tables, no HTTP
# --------------------------------------------------------------------------


async def test_list_ticket_priorities_is_the_one_local_scale_and_makes_no_http_call(server, mock_gorelo):
    result = await call_tool(server, "list_ticket_priorities")
    assert result == {
        "items": [
            {"Id": 0, "Name": "None"},
            {"Id": 1, "Name": "Urgent"},
            {"Id": 2, "Name": "High"},
            {"Id": 3, "Name": "Normal"},
            {"Id": 4, "Name": "Low"},
        ],
        "count": 5,
    }
    assert mock_gorelo.requests == []


async def test_list_ticket_sources_is_the_local_table_with_the_note_about_id_7(server, mock_gorelo):
    result = await call_tool(server, "list_ticket_sources")
    assert result["items"] == [
        {"Id": 1, "Name": "Web"},
        {"Id": 2, "Name": "Email"},
        {"Id": 3, "Name": "Phone"},
        {"Id": 4, "Name": "Chat"},
        {"Id": 5, "Name": "Alert"},
        {"Id": 6, "Name": "Api"},
    ]
    assert result["count"] == 6 and set(result) == {"items", "count", "note"}
    assert "Id 7" in result["note"] and "empty name" in result["note"] and "cannot be set" in result["note"]
    assert "source_id 1 to 6" in result["note"]
    assert 7 not in [row["Id"] for row in result["items"]]
    assert mock_gorelo.requests == []


async def test_the_local_tables_are_fresh_copies_on_every_call(server):
    first = await call_tool(server, "list_ticket_priorities")
    first["items"].clear()
    assert (await call_tool(server, "list_ticket_priorities"))["count"] == 5
    assert tickets.PRIORITIES[0] == (0, "None")


# --------------------------------------------------------------------------
# list_ticket_statuses, list_ticket_types, list_ticket_tags: unpaged lists
# --------------------------------------------------------------------------

LOOKUP_TOOLS = [
    pytest.param("list_ticket_statuses", STATUSES_PATH, STATUSES, id="statuses"),
    pytest.param("list_ticket_types", TYPES_PATH, TYPES, id="types"),
    pytest.param("list_ticket_tags", TAGS_PATH, TAGS, id="tags"),
]


@pytest.mark.parametrize("tool, path, rows", LOOKUP_TOOLS)
async def test_a_lookup_tool_is_one_get_with_no_query_and_returns_the_unpaged_list(server, mock_gorelo, tool, path, rows):
    mock_gorelo.on("GET", path, envelope(rows))
    result = await call_tool(server, tool)
    assert result == {"items": rows, "count": len(rows)}
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", path, {}, None)
    assert len(mock_gorelo.requests) == 1  # never PageSize or Cursor on an unpaged op


@pytest.mark.parametrize("tool, path, rows", LOOKUP_TOOLS)
async def test_a_lookup_tool_reports_an_empty_list_as_count_zero(server, mock_gorelo, tool, path, rows):
    mock_gorelo.on("GET", path, envelope([]))
    assert await call_tool(server, tool) == {"items": [], "count": 0}


@pytest.mark.parametrize("tool, path, rows", LOOKUP_TOOLS)
async def test_a_lookup_tool_refuses_a_data_that_is_not_a_list(server, mock_gorelo, tool, path, rows):
    mock_gorelo.on("GET", path, envelope({"Id": 1}))
    text = await call_tool_error(server, tool)
    assert text.startswith(f"Gorelo returned an unexpected response for {tool}:") and "expected Data to be a list" in text


@pytest.mark.parametrize("tool, path, rows", LOOKUP_TOOLS)
async def test_a_lookup_tool_names_itself_in_a_gorelo_error(server, mock_gorelo, tool, path, rows):
    mock_gorelo.on("GET", path, error_envelope(403, [("080203", "API key does not have 'Tickets' scope")], trace_id="00-t-1"))
    text = await call_tool_error(server, tool)
    assert text.startswith(f"Gorelo rejected {tool} (HTTP 403, code 080203): the API key does not have the 'Tickets' scope")
    assert text.endswith("[trace 00-t-1]")


# --------------------------------------------------------------------------
# list_tickets
# --------------------------------------------------------------------------


async def test_list_tickets_with_no_filters_is_one_get_with_only_the_page_size(server, mock_gorelo):
    rows = [ticket_row(1), ticket_row(2)]
    mock_gorelo.on("GET", LIST, paged_envelope(rows, next_cursor="c1", total_count=4321))
    result = await call_tool(server, "list_tickets")
    request = mock_gorelo.last
    assert (request.method, request.path, request.json) == ("GET", LIST, None)
    assert request.query == {"PageSize": "50"}
    assert result == {
        "items": rows,
        "count": 2,
        "total_count": 4321,
        "has_more": True,
        "next_cursor": "c1",
        "page_size": 50,
        "filters": {},
    }
    assert len(mock_gorelo.requests) == 1


async def test_list_tickets_sends_every_filter_with_the_spec_names_and_reports_them(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, paged_envelope([ticket_row(1)]))
    result = await call_tool(
        server,
        "list_tickets",
        {
            "status_ids": [1, 2],
            "client_ids": [9102, 9101],
            "priority_ids": [1, 2],
            "type_ids": [7101],
            "lead_assignee_ids": [9201],
            "contact_ids": [9103],
            "tag_ids": [5, 6],
            "group_ids": [7201],
            "query": "  printer  ",
            "updated_since": "2026-10-01T09:30:00-05:00",
            "updated_before": "2026-10-02T00:00:00Z",
            "created_since": "2026-09-01T00:00:00+00:00",
            "created_before": "2026-10-01T00:00:00Z",
            "sort_by": "createdOn",
            "sort_order": "asc",
            "page_size": 25,
            "cursor": "abc",
        },
    )
    assert mock_gorelo.last.query == {
        "StatusIds": "1,2",
        "ClientIds": "9102,9101",
        "PriorityIds": "1,2",
        "TypeIds": "7101",
        "LeadAssigneeIds": "9201",
        "ContactIds": "9103",
        "TagIds": "5,6",
        "GroupIds": "7201",
        "Query": "printer",
        "UpdatedSince": "2026-10-01T14:30:00Z",
        "UpdatedBefore": "2026-10-02T00:00:00Z",
        "CreatedSince": "2026-09-01T00:00:00Z",
        "CreatedBefore": "2026-10-01T00:00:00Z",
        "SortBy": "createdOn",
        "SortOrder": "asc",
        "PageSize": "25",
        "Cursor": "abc",
    }
    assert result["filters"] == {
        "status_ids": [1, 2],
        "client_ids": [9102, 9101],
        "priority_ids": [1, 2],
        "type_ids": [7101],
        "lead_assignee_ids": [9201],
        "contact_ids": [9103],
        "tag_ids": [5, 6],
        "group_ids": [7201],
        "query": "printer",
        "updated_since": "2026-10-01T14:30:00Z",
        "updated_before": "2026-10-02T00:00:00Z",
        "created_since": "2026-09-01T00:00:00Z",
        "created_before": "2026-10-01T00:00:00Z",
        "sort_by": "createdOn",
        "sort_order": "asc",
    }
    assert result["page_size"] == 25 and result["has_more"] is False and result["next_cursor"] is None


SINGLE_LIST_FILTERS = [
    ("status_ids", [7, 8], "StatusIds", "7,8"),
    ("client_ids", [9102], "ClientIds", "9102"),
    ("priority_ids", [2], "PriorityIds", "2"),
    ("type_ids", [7101, 9301], "TypeIds", "7101,9301"),
    ("lead_assignee_ids", [9201], "LeadAssigneeIds", "9201"),
    ("contact_ids", [9103, 9104], "ContactIds", "9103,9104"),
    ("tag_ids", [5], "TagIds", "5"),
    ("group_ids", [7201], "GroupIds", "7201"),
    ("query", "TCK-1234", "Query", "TCK-1234"),
    ("updated_since", "2026-10-01T00:00:00Z", "UpdatedSince", "2026-10-01T00:00:00Z"),
    ("updated_before", "2026-10-01T02:00:00+02:00", "UpdatedBefore", "2026-10-01T00:00:00Z"),
    ("created_since", "2026-09-30T20:00:00-04:00", "CreatedSince", "2026-10-01T00:00:00Z"),
    ("created_before", "2026-10-01T00:00:00Z", "CreatedBefore", "2026-10-01T00:00:00Z"),
    ("sort_by", "updatedOn", "SortBy", "updatedOn"),
    ("sort_by", "createdOn", "SortBy", "createdOn"),
    ("sort_order", "asc", "SortOrder", "asc"),
    ("sort_order", "desc", "SortOrder", "desc"),
]


@pytest.mark.parametrize("param, value, name, sent", SINGLE_LIST_FILTERS)
async def test_list_tickets_each_filter_alone_sends_only_its_own_query_name(server, mock_gorelo, param, value, name, sent):
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    result = await call_tool(server, "list_tickets", {param: value})
    assert mock_gorelo.last.query == {name: sent, "PageSize": "50"}
    assert list(result["filters"]) == [param]


async def test_list_tickets_allows_priority_zero_in_the_filter(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    await call_tool(server, "list_tickets", {"priority_ids": [0, 4]})
    assert mock_gorelo.last.query == {"PriorityIds": "0,4", "PageSize": "50"}


async def test_list_tickets_sends_a_single_filter_alone(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    await call_tool(server, "list_tickets", {"client_ids": [9102]})
    assert mock_gorelo.last.query == {"ClientIds": "9102", "PageSize": "50"}


@pytest.mark.parametrize("asked, used", [(0, 1), (-7, 1), (1, 1), (200, 200), (201, 200), (999, 200)])
async def test_list_tickets_clamps_the_page_size_and_reports_the_size_used(server, mock_gorelo, asked, used):
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    result = await call_tool(server, "list_tickets", {"page_size": asked})
    assert mock_gorelo.last.query["PageSize"] == str(used) and result["page_size"] == used


async def test_list_tickets_walks_pages_with_the_cursor_and_the_same_filters(server, mock_gorelo):
    pages = [[ticket_row(1), ticket_row(2)], [ticket_row(3)]]
    mock_gorelo.on("GET", LIST, paged_responder(pages))
    filters = {"client_ids": [9102], "status_ids": [1], "page_size": 2}
    first = await call_tool(server, "list_tickets", filters)
    assert first["has_more"] is True and first["next_cursor"] == "c1" and ids_of(first) == [uid(1), uid(2)]
    second = await call_tool(server, "list_tickets", {**filters, "cursor": first["next_cursor"]})
    assert second["has_more"] is False and second["next_cursor"] is None and ids_of(second) == [uid(3)]
    assert second["total_count"] == 3 and second["filters"] == first["filters"]
    one, two = mock_gorelo.requests
    assert "Cursor" not in one.query and two.query["Cursor"] == "c1"
    assert {k: v for k, v in two.query.items() if k != "Cursor"} == one.query


async def test_list_tickets_reports_an_empty_page_explicitly(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    result = await call_tool(server, "list_tickets", {"query": "nothing matches this"})
    assert result == {
        "items": [], "count": 0, "total_count": 0, "has_more": False, "next_cursor": None, "page_size": 50,
        "filters": {"query": "nothing matches this"},
    }


async def test_list_tickets_returns_gorelo_records_unchanged(server, mock_gorelo):
    row = ticket_row(1, ContactId=None, IsWaitingOnThem=True, ChecklistSummary={"Completed": 2, "Total": 5},
                     Sla={"FirstResponse": {"ElapsedBusinessMinutes": 12.5}})
    mock_gorelo.on("GET", LIST, paged_envelope([row]))
    item = (await call_tool(server, "list_tickets"))["items"][0]
    assert item == row and item["ContactId"] is None and item["Sla"]["FirstResponse"]["ElapsedBusinessMinutes"] == 12.5
    assert "Description" not in item and "BillingOverride" not in item


LIST_VALIDATION = [
    pytest.param({"status_ids": []}, f"status_ids: {NO_IDS}", id="empty-status-ids"),
    pytest.param({"client_ids": [0]}, f"client_ids[0]: {NOT_POSITIVE}", id="client-id-zero"),
    pytest.param({"client_ids": [5, 0]}, f"client_ids[1]: {NOT_POSITIVE}", id="second-client-id-zero"),
    pytest.param({"type_ids": [-3]}, f"type_ids[0]: {NOT_POSITIVE}", id="negative-type-id"),
    pytest.param({"group_ids": []}, f"group_ids: {NO_IDS}", id="empty-group-ids"),
    pytest.param({"lead_assignee_ids": [0]}, f"lead_assignee_ids[0]: {NOT_POSITIVE}", id="assignee-zero"),
    pytest.param({"contact_ids": [2**63]}, "contact_ids[0]: expected a positive whole number such as 123, got a number above", id="contact-id-above-int64"),
    pytest.param({"priority_ids": [5]}, "priority_ids[0]: must be one of 0 None, 1 Urgent, 2 High, 3 Normal, 4 Low", id="priority-5"),
    pytest.param({"priority_ids": [1, -1]}, "priority_ids[1]: must be one of 0 None", id="priority-negative"),
    pytest.param({"priority_ids": []}, "priority_ids: expected a non-empty list of ids, got an empty list", id="empty-priority-ids"),
    pytest.param({"query": "   "}, "query: must not be empty or whitespace only", id="blank-query"),
    pytest.param({"query": "x" * 201}, "query: at most 200 characters, got 201", id="long-query"),
    # a blank cursor is a caller mistake, not "the first page" (that is no cursor at all)
    pytest.param({"cursor": ""}, "cursor: must not be empty or whitespace only", id="blank-cursor"),
    pytest.param({"cursor": "   "}, "cursor: must not be empty or whitespace only", id="whitespace-cursor"),
    pytest.param({"cursor": "\n\t"}, "cursor: must not be empty or whitespace only", id="control-whitespace-cursor"),
    pytest.param({"updated_since": "2026-10-01T09:30:00"}, "updated_since: '2026-10-01T09:30:00' has no UTC offset", id="naive-updated-since"),
    pytest.param({"created_before": "yesterday"}, "created_before: 'yesterday' is not an ISO 8601 datetime", id="not-a-date"),
    pytest.param({"updated_before": ""}, "updated_before: expected an ISO 8601 datetime with a UTC offset", id="blank-date"),
    pytest.param(
        {"updated_since": "2026-10-02T00:00:00Z", "updated_before": "2026-10-01T00:00:00Z"},
        "updated_since: 2026-10-02T00:00:00Z is not earlier than updated_before (2026-10-01T00:00:00Z)",
        id="updated-range-reversed",
    ),
    pytest.param(
        {"updated_since": "2026-10-01T00:00:00Z", "updated_before": "2026-10-01T00:00:00Z"},
        "updated_since: 2026-10-01T00:00:00Z is not earlier than updated_before",
        id="updated-range-empty",
    ),
    pytest.param(
        {"created_since": "2026-10-01T02:00:00+02:00", "created_before": "2026-09-30T23:00:00Z"},
        "created_since: 2026-10-01T00:00:00Z is not earlier than created_before (2026-09-30T23:00:00Z)",
        id="created-range-reversed-across-offsets",
    ),
]


@pytest.mark.parametrize("args, fragment", LIST_VALIDATION)
async def test_list_tickets_local_validation_errors_name_the_parameter_and_send_nothing(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "list_tickets", args)
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "args, param",
    [
        ({"sort_by": "title"}, "sort_by"),
        ({"sort_by": "UPDATEDON"}, "sort_by"),
        ({"sort_order": "up"}, "sort_order"),
        ({"page_size": "many"}, "page_size"),
        ({"status_ids": ["new"]}, "status_ids"),
        ({"cursor": 12}, "cursor"),
    ],
)
async def test_list_tickets_rejects_a_value_of_the_wrong_kind_before_any_http_call(server, mock_gorelo, args, param):
    text = await call_tool_error(server, "list_tickets", args)
    assert param in text
    assert mock_gorelo.requests == []


async def test_list_tickets_maps_a_gorelo_error_to_the_snake_case_parameter(server, mock_gorelo):
    body = error_envelope(
        400,
        [("070101", "This query parameter is not recognized by this endpoint.", "StatusIds"), ("070101", "PageSize must be between 1 and 200.", "PageSize")],
        trace_id="00-abc-01",
    )
    mock_gorelo.on("GET", LIST, body)
    text = await call_tool_error(server, "list_tickets", {"status_ids": [1]})
    assert text == (
        "Gorelo rejected list_tickets (HTTP 400, code 070101): status_ids: This query parameter is not recognized "
        "by this endpoint.; page_size: PageSize must be between 1 and 200. [trace 00-abc-01]"
    )


@pytest.mark.parametrize("prop, param", [("Query", "query"), ("UpdatedSince", "updated_since"), ("SortBy", "sort_by"), ("Cursor", "cursor"), ("PriorityIds", "priority_ids")])
async def test_list_tickets_maps_every_query_name_back_to_its_parameter(server, mock_gorelo, prop, param):
    mock_gorelo.on("GET", LIST, error_envelope(400, [("070101", "bad value", prop)]))
    text = await call_tool_error(server, "list_tickets")
    assert f"{param}: bad value" in text


async def test_list_tickets_refuses_a_response_that_is_not_a_list(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, envelope({"Id": 1}, pagination(None, 1)))
    text = await call_tool_error(server, "list_tickets")
    assert text.startswith("Gorelo returned an unexpected response for list_tickets:") and "expected Data to be a list" in text


async def test_list_tickets_refuses_rows_without_pagination(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, envelope([ticket_row(1)]))
    text = await call_tool_error(server, "list_tickets")
    assert "paged response without Pagination" in text


# --------------------------------------------------------------------------
# search_tickets: name resolution
# --------------------------------------------------------------------------


async def test_search_tickets_without_filters_scans_with_the_page_size_only_and_makes_no_lookup_call(server, mock_gorelo):
    rows = [ticket_row(1), ticket_row(2)]
    mock_gorelo.on("GET", LIST, paged_envelope(rows))
    result = await call_tool(server, "search_tickets")
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", LIST)]
    assert mock_gorelo.last.query == {"PageSize": "200"}
    assert result == {
        "items": rows,
        "count": 2,
        "total_count": 2,
        "truncated": False,
        "complete_scan": True,
        "count_mismatch": False,
        "filters": {"limit": 50},
        "matched": 2,
        "scanned": 2,
    }


async def test_search_tickets_sends_exactly_the_server_side_query_and_resolves_names_with_the_list_endpoints(server, mock_gorelo):
    lookups(mock_gorelo)
    mock_gorelo.on("GET", LIST, paged_envelope([ticket_row(1)], total_count=1))
    result = await call_tool(
        server,
        "search_tickets",
        {
            "client_id": 9102,
            "status": "closed",
            "priority": "high",
            "type": "INCIDENT",
            "query": " printer ",
            "updated_since": "2026-10-01T09:30:00-05:00",
            "created_since": "2026-09-01T00:00:00Z",
            "limit": 10,
        },
    )
    scan = mock_gorelo.calls("GET", LIST)
    assert len(scan) == 1
    assert scan[0].query == {
        "ClientIds": "9102",
        "StatusIds": "4",
        "PriorityIds": "2",
        "TypeIds": "7101",
        "Query": "printer",
        "UpdatedSince": "2026-10-01T14:30:00Z",
        "CreatedSince": "2026-09-01T00:00:00Z",
        "PageSize": "200",
    }
    for lookup_path in (STATUSES_PATH, TYPES_PATH):
        calls = mock_gorelo.calls("GET", lookup_path)
        assert len(calls) == 1 and calls[0].query == {} and calls[0].json is None
    assert result["filters"] == {
        "client_id": 9102,
        "status": "Closed",
        "status_id": 4,
        "priority": "High",
        "priority_id": 2,
        "type": "Incident",
        "type_id": 7101,
        "query": "printer",
        "updated_since": "2026-10-01T14:30:00Z",
        "created_since": "2026-09-01T00:00:00Z",
        "limit": 10,
    }
    assert [r.method for r in mock_gorelo.requests] == ["GET"] * 3


@pytest.mark.parametrize("given", ["Closed", "closed", "CLOSED", "  closed  ", "cLoSeD"])
async def test_search_tickets_resolves_a_status_name_in_any_case_with_the_status_list(server, mock_gorelo, given):
    lookups(mock_gorelo)
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    result = await call_tool(server, "search_tickets", {"status": given})
    assert mock_gorelo.calls("GET", LIST)[0].query["StatusIds"] == "4"
    assert result["filters"]["status"] == "Closed" and result["filters"]["status_id"] == 4
    assert len(mock_gorelo.calls("GET", STATUSES_PATH)) == 1
    assert not mock_gorelo.calls("GET", TYPES_PATH)  # only the lookup that is needed


@pytest.mark.parametrize("given", [4, "4", " 4 ", "04"])
async def test_search_tickets_sends_a_status_id_as_it_is_without_any_lookup(server, mock_gorelo, given):
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    result = await call_tool(server, "search_tickets", {"status": given})
    assert [r.path for r in mock_gorelo.requests] == [LIST]
    assert mock_gorelo.last.query == {"StatusIds": "4", "PageSize": "200"}
    assert result["filters"] == {"status_id": 4, "limit": 50}  # an id has no name to report


async def test_search_tickets_an_unknown_status_or_type_id_is_not_checked_locally(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    result = await call_tool(server, "search_tickets", {"status": 99, "type": "7"})
    assert mock_gorelo.last.query == {"StatusIds": "99", "TypeIds": "7", "PageSize": "200"}
    assert result["count"] == 0 and result["filters"]["status_id"] == 99 and result["filters"]["type_id"] == 7


async def test_search_tickets_resolves_a_status_name_with_spaces_and_sends_a_type_id_without_a_lookup(server, mock_gorelo):
    lookups(mock_gorelo)
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    await call_tool(server, "search_tickets", {"status": "awaiting REPLY", "type": 9301})
    assert mock_gorelo.calls("GET", LIST)[0].query == {"StatusIds": "7301", "TypeIds": "9301", "PageSize": "200"}
    assert len(mock_gorelo.calls("GET", STATUSES_PATH)) == 1 and not mock_gorelo.calls("GET", TYPES_PATH)


async def test_search_tickets_resolves_a_type_name_with_the_type_list_only(server, mock_gorelo):
    lookups(mock_gorelo)
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    result = await call_tool(server, "search_tickets", {"type": "request"})
    assert mock_gorelo.calls("GET", LIST)[0].query == {"TypeIds": "9301", "PageSize": "200"}
    assert result["filters"]["type"] == "Request" and result["filters"]["type_id"] == 9301
    assert len(mock_gorelo.calls("GET", TYPES_PATH)) == 1 and not mock_gorelo.calls("GET", STATUSES_PATH)


@pytest.mark.parametrize(
    "given, wanted_id, wanted_name",
    [("None", 0, "None"), ("none", 0, "None"), ("urgent", 1, "Urgent"), ("HIGH", 2, "High"), ("Normal", 3, "Normal"), (" low ", 4, "Low"), (0, 0, "None"), (3, 3, "Normal"), ("2", 2, "High")],
)
async def test_search_tickets_resolves_priorities_from_the_constant_table_without_a_lookup_call(
    server, mock_gorelo, given, wanted_id, wanted_name
):
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    result = await call_tool(server, "search_tickets", {"priority": given})
    assert mock_gorelo.last.query == {"PriorityIds": str(wanted_id), "PageSize": "200"}
    assert result["filters"]["priority"] == wanted_name and result["filters"]["priority_id"] == wanted_id
    assert [r.path for r in mock_gorelo.requests] == [LIST]


async def test_search_tickets_unknown_status_name_is_a_local_error_that_lists_the_valid_names(server, mock_gorelo):
    lookups(mock_gorelo)
    text = await call_tool_error(server, "search_tickets", {"status": "Clsoed"})
    assert text == (
        "status: no ticket status is named 'Clsoed'. Valid statuses: New (1), In Progress (2), Solved (3), "
        "Closed (4), On Hold (6), Awaiting Reply (7301); a numeric id is accepted too"
    )
    assert [r.path for r in mock_gorelo.requests] == [STATUSES_PATH]  # the scan never starts


async def test_search_tickets_unknown_type_name_is_a_local_error_that_lists_the_valid_types(server, mock_gorelo):
    lookups(mock_gorelo)
    text = await call_tool_error(server, "search_tickets", {"type": "Incidnt"})
    assert text == (
        "type: no ticket type is named 'Incidnt'. Valid types: Incident (7101), Request (9301), Maintenance (9302), "
        "Admin (9303), Other (9304); a numeric id is accepted too"
    )
    assert [r.path for r in mock_gorelo.requests] == [TYPES_PATH]  # the scan never starts


@pytest.mark.parametrize("given", ["-1", "1.5", "4th", "#4"])
async def test_search_tickets_text_that_is_not_all_digits_is_a_name(server, mock_gorelo, given):
    lookups(mock_gorelo)
    text = await call_tool_error(server, "search_tickets", {"status": given})
    assert text.startswith(f"status: no ticket status is named {given!r}. Valid statuses: New (1)")
    assert not mock_gorelo.calls("GET", LIST)


@pytest.mark.parametrize("given", ["Urgentt", "Medium", "5", 5, -1, "9"])
async def test_search_tickets_unknown_priority_lists_the_table_and_sends_nothing(server, mock_gorelo, given):
    text = await call_tool_error(server, "search_tickets", {"priority": given})
    assert text.startswith("priority: no ticket priority ")
    assert "Valid priorities: None (0), Urgent (1), High (2), Normal (3), Low (4)" in text
    assert mock_gorelo.requests == []


async def test_search_tickets_validates_the_priority_before_it_looks_anything_up(server, mock_gorelo):
    text = await call_tool_error(server, "search_tickets", {"status": "Closed", "type": "Incident", "priority": "bogus"})
    assert text.startswith("priority: no ticket priority is named 'bogus'")
    assert mock_gorelo.requests == []


async def test_search_tickets_two_statuses_with_the_same_name_are_ambiguous(server, mock_gorelo):
    twins = STATUSES + [status_row(77, "CLOSED", 4)]
    lookups(mock_gorelo, statuses=twins)
    text = await call_tool_error(server, "search_tickets", {"status": "closed"})
    assert text == "status: 'closed' matches several ticket statuses (ids 4, 77); pass the numeric id instead"
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    await call_tool(server, "search_tickets", {"status": 77})  # the id still works
    assert mock_gorelo.calls("GET", LIST)[0].query["StatusIds"] == "77"


async def test_search_tickets_text_made_only_of_digits_is_always_an_id_never_a_name(server, mock_gorelo):
    odd = [{"Id": 5, "Name": "7"}, {"Id": 7, "Name": "Seven"}]
    lookups(mock_gorelo, statuses=odd)
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    await call_tool(server, "search_tickets", {"status": "7"})
    assert mock_gorelo.calls("GET", LIST)[0].query["StatusIds"] == "7" and not mock_gorelo.calls("GET", STATUSES_PATH)


async def test_search_tickets_with_no_usable_lookup_rows_says_so(server, mock_gorelo):
    lookups(mock_gorelo, statuses=[])
    text = await call_tool_error(server, "search_tickets", {"status": "Closed"})
    assert text == "status: no ticket status is named 'Closed'. Valid statuses: none; a numeric id is accepted too"


SEARCH_VALIDATION = [
    pytest.param({"limit": 0}, "limit: must be a whole number from 1 to 500;", id="limit-0"),
    pytest.param({"limit": 501}, "limit: must be a whole number from 1 to 500;", id="limit-501"),
    pytest.param({"limit": -5}, "limit: must be a whole number from 1 to 500;", id="limit-negative"),
    pytest.param({"client_id": 0}, f"client_id: {NOT_POSITIVE}", id="client-zero"),
    pytest.param({"query": ""}, "query: must not be empty or whitespace only", id="blank-query"),
    pytest.param({"query": "y" * 201}, "query: at most 200 characters, got 201", id="long-query"),
    pytest.param({"status": ""}, "status: must not be empty or whitespace only", id="blank-status"),
    pytest.param({"status": "   "}, "status: must not be empty or whitespace only", id="whitespace-status"),
    pytest.param({"priority": ""}, "priority: must not be empty or whitespace only", id="blank-priority"),
    pytest.param({"type": " "}, "type: must not be empty or whitespace only", id="blank-type"),
    pytest.param({"status": 0}, f"status: {NOT_POSITIVE}", id="status-id-zero"),
    pytest.param({"status": "0"}, f"status: {NOT_POSITIVE}", id="status-id-zero-text"),
    pytest.param({"type": 0}, f"type: {NOT_POSITIVE}", id="type-id-zero"),
    pytest.param({"updated_since": "2026-10-01"}, "updated_since: '2026-10-01' has no UTC offset", id="naive-updated-since"),
    pytest.param({"created_since": "2026-10-01T00:00:00"}, "created_since: '2026-10-01T00:00:00' has no UTC offset", id="naive-created-since"),
    pytest.param({"created_since": "soon"}, "created_since: 'soon' is not an ISO 8601 datetime", id="bad-created-since"),
]


@pytest.mark.parametrize("args, fragment", SEARCH_VALIDATION)
async def test_search_tickets_local_validation_errors_name_the_parameter_and_send_nothing(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "search_tickets", args)
    assert fragment in text
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# search_tickets: scanning, client-side filters, limit, honesty flags
# --------------------------------------------------------------------------


def mixed_rows():
    return [
        ticket_row(1, LeadAssigneeId=None, IsUnread=True, IsWaitingOnThem=False, ClosedOn=None),
        ticket_row(2, LeadAssigneeId=9201, IsUnread=False, IsWaitingOnThem=True, ClosedOn=None),
        ticket_row(3, LeadAssigneeId=None, IsUnread=False, IsWaitingOnThem=True, ClosedOn="2026-09-30T10:00:00Z"),
        ticket_row(4, LeadAssigneeId=9202, IsUnread=True, IsWaitingOnThem=False, ClosedOn="2026-09-29T10:00:00Z"),
        ticket_row(5, LeadAssigneeId=None, IsUnread=True, IsWaitingOnThem=True, ClosedOn=None),
    ]


@pytest.mark.parametrize(
    "args, wanted",
    [
        pytest.param({"unassigned_only": True}, [1, 3, 5], id="unassigned"),
        pytest.param({"awaiting_client": True}, [2, 3, 5], id="awaiting-true"),
        pytest.param({"awaiting_client": False}, [1, 4], id="awaiting-false"),
        pytest.param({"unread_only": True}, [1, 4, 5], id="unread"),
        pytest.param({"exclude_closed": True}, [1, 2, 5], id="open-only"),
        pytest.param({"unassigned_only": True, "awaiting_client": True, "exclude_closed": True}, [5], id="three-combined"),
        pytest.param({"unread_only": True, "exclude_closed": True, "unassigned_only": True}, [1, 5], id="unread-open-unassigned"),
        pytest.param({"unread_only": True, "awaiting_client": False}, [1, 4], id="unread-not-waiting"),
        pytest.param({"unassigned_only": False, "unread_only": False, "exclude_closed": False}, [1, 2, 3, 4, 5], id="all-false-is-no-filter"),
        pytest.param({"unassigned_only": True, "unread_only": True, "awaiting_client": True, "exclude_closed": True}, [5], id="all-four"),
    ],
)
async def test_search_tickets_applies_the_four_client_side_filters_to_the_scanned_rows(server, mock_gorelo, args, wanted):
    mock_gorelo.on("GET", LIST, paged_envelope(mixed_rows()))
    result = await call_tool(server, "search_tickets", args)
    assert ids_of(result) == [uid(n) for n in wanted]
    assert result["count"] == len(wanted) == result["matched"]
    assert result["scanned"] == 5 and result["total_count"] == 5
    assert result["truncated"] is False and result["complete_scan"] is True
    assert "note" not in result
    # the client-side flags never reach the API: only the page size is sent
    assert mock_gorelo.last.query == {"PageSize": "200"} and len(mock_gorelo.requests) == 1


SINGLE_SEARCH_FILTERS = [
    ({"client_id": 9102}, {"ClientIds": "9102"}),
    ({"priority": "Urgent"}, {"PriorityIds": "1"}),
    ({"query": "  TCK-1234 "}, {"Query": "TCK-1234"}),
    ({"updated_since": "2026-10-01T02:00:00+02:00"}, {"UpdatedSince": "2026-10-01T00:00:00Z"}),
    ({"created_since": "2026-09-30T20:00:00-04:00"}, {"CreatedSince": "2026-10-01T00:00:00Z"}),
    ({"status": 4}, {"StatusIds": "4"}),
    ({"type": 7101}, {"TypeIds": "7101"}),
]


@pytest.mark.parametrize("args, sent", SINGLE_SEARCH_FILTERS)
async def test_search_tickets_each_server_side_filter_alone_sends_only_its_own_query_name(server, mock_gorelo, args, sent):
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    await call_tool(server, "search_tickets", args)
    assert mock_gorelo.last.query == {**sent, "PageSize": "200"}


async def test_search_tickets_a_null_or_missing_boolean_counts_as_false(server, mock_gorelo):
    rows = [ticket_row(1, IsUnread=None, IsWaitingOnThem=None), ticket_row(2, IsUnread=True, IsWaitingOnThem=True)]
    del rows[0]["IsUnread"]  # missing in one row, present in the other: a missing flag is not set
    mock_gorelo.on("GET", LIST, paged_envelope(rows))
    assert ids_of(await call_tool(server, "search_tickets", {"unread_only": True})) == [uid(2)]
    assert ids_of(await call_tool(server, "search_tickets", {"awaiting_client": True})) == [uid(2)]
    assert ids_of(await call_tool(server, "search_tickets", {"awaiting_client": False})) == [uid(1)]


async def test_search_tickets_reports_the_active_client_side_filters_and_omits_the_inactive_ones(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, paged_envelope(mixed_rows()))
    result = await call_tool(server, "search_tickets", {"unassigned_only": True, "awaiting_client": False, "exclude_closed": True})
    assert result["filters"] == {"unassigned_only": True, "awaiting_client": False, "exclude_closed": True, "limit": 50}


async def test_search_tickets_combines_server_side_and_client_side_filters_with_and(server, mock_gorelo):
    lookups(mock_gorelo)
    mock_gorelo.on("GET", LIST, paged_envelope(mixed_rows()))
    result = await call_tool(server, "search_tickets", {"client_id": 9102, "status": "New", "unassigned_only": True})
    assert mock_gorelo.calls("GET", LIST)[0].query == {"ClientIds": "9102", "StatusIds": "1", "PageSize": "200"}
    assert ids_of(result) == [uid(1), uid(3), uid(5)]
    assert result["total_count"] == 5 and result["matched"] == 3


async def test_search_tickets_reads_every_page_and_reports_a_complete_scan(server, mock_gorelo):
    pages = [[ticket_row(1), ticket_row(2), ticket_row(3)], [ticket_row(4), ticket_row(5), ticket_row(6)], [ticket_row(7)]]
    mock_gorelo.on("GET", LIST, paged_responder(pages))
    result = await call_tool(server, "search_tickets", {"client_id": 9102})
    queries = [r.query for r in mock_gorelo.calls("GET", LIST)]
    assert queries == [
        {"ClientIds": "9102", "PageSize": "200"},
        {"ClientIds": "9102", "PageSize": "200", "Cursor": "c1"},
        {"ClientIds": "9102", "PageSize": "200", "Cursor": "c2"},
    ]
    assert ids_of(result) == [uid(n) for n in range(1, 8)]
    assert (result["count"], result["total_count"], result["scanned"], result["matched"]) == (7, 7, 7, 7)
    assert result["complete_scan"] is True and result["truncated"] is False and result["count_mismatch"] is False


async def test_search_tickets_filters_rows_from_every_page(server, mock_gorelo):
    pages = [
        [ticket_row(1, IsUnread=True), ticket_row(2, IsUnread=False)],
        [ticket_row(3, IsUnread=False), ticket_row(4, IsUnread=True)],
    ]
    mock_gorelo.on("GET", LIST, paged_responder(pages))
    result = await call_tool(server, "search_tickets", {"unread_only": True})
    assert ids_of(result) == [uid(1), uid(4)] and result["scanned"] == 4 and result["total_count"] == 4


async def test_search_tickets_limit_cuts_the_matches_and_says_so(server, mock_gorelo):
    rows = [ticket_row(n) for n in range(1, 61)]
    mock_gorelo.on("GET", LIST, paged_envelope(rows))
    result = await call_tool(server, "search_tickets")  # default limit 50
    assert result["count"] == 50 and ids_of(result) == [uid(n) for n in range(1, 51)]
    assert result["matched"] == 60 and result["scanned"] == 60 and result["total_count"] == 60
    assert result["truncated"] is True and result["complete_scan"] is True
    assert result["filters"]["limit"] == 50
    assert "60 tickets matched and 50 are returned" in result["note"] and "raise limit (max 500)" in result["note"]


async def test_search_tickets_a_limit_that_fits_every_match_is_not_truncated(server, mock_gorelo):
    rows = [ticket_row(n) for n in range(1, 61)]
    mock_gorelo.on("GET", LIST, paged_envelope(rows))
    for limit in (60, 500):
        result = await call_tool(server, "search_tickets", {"limit": limit})
        assert result["count"] == 60 and result["truncated"] is False and "note" not in result
        assert result["filters"]["limit"] == limit


async def test_search_tickets_limit_applies_after_the_client_side_filters(server, mock_gorelo):
    rows = [ticket_row(n, IsUnread=n % 2 == 0) for n in range(1, 11)]
    mock_gorelo.on("GET", LIST, paged_envelope(rows))
    result = await call_tool(server, "search_tickets", {"unread_only": True, "limit": 2})
    assert ids_of(result) == [uid(2), uid(4)]
    assert result["matched"] == 5 and result["scanned"] == 10 and result["truncated"] is True and result["complete_scan"] is True


async def test_search_tickets_a_scan_stopped_by_the_cap_is_flagged_not_silent(server, mock_gorelo, monkeypatch):
    monkeypatch.setattr(tickets, "SEARCH_SCAN_CAP", 4)
    pages = [[ticket_row(n) for n in range(1, 4)], [ticket_row(n) for n in range(4, 7)], [ticket_row(n) for n in range(7, 10)]]
    mock_gorelo.on("GET", LIST, paged_responder(pages))
    result = await call_tool(server, "search_tickets")
    assert len(mock_gorelo.calls("GET", LIST)) == 2  # the third page is never read
    assert result["scanned"] == 4 and result["count"] == 4 and ids_of(result) == [uid(n) for n in range(1, 5)]
    assert result["total_count"] == 9
    assert result["complete_scan"] is False and result["truncated"] is True
    assert "the scan stopped after 4 tickets (cap 4) of 9" in result["note"] and "add client_id, status" in result["note"]


async def test_search_tickets_a_capped_scan_without_a_total_count_does_not_print_none(server, mock_gorelo, monkeypatch):
    monkeypatch.setattr(tickets, "SEARCH_SCAN_CAP", 2)
    rows = [ticket_row(1), ticket_row(2), ticket_row(3)]
    mock_gorelo.on("GET", LIST, envelope(rows, pagination("c1", None, has_more=True)))
    result = await call_tool(server, "search_tickets")
    assert result["total_count"] is None and result["complete_scan"] is False and result["count"] == 2
    assert "the scan stopped after 2 tickets (cap 2): add client_id" in result["note"] and "None" not in result["note"]


async def test_search_tickets_a_scan_that_ends_exactly_at_the_cap_is_complete(server, mock_gorelo, monkeypatch):
    monkeypatch.setattr(tickets, "SEARCH_SCAN_CAP", 3)
    mock_gorelo.on("GET", LIST, paged_envelope([ticket_row(1), ticket_row(2), ticket_row(3)]))
    result = await call_tool(server, "search_tickets")
    assert result["complete_scan"] is True and result["truncated"] is False and "note" not in result


def test_search_tickets_the_default_scan_cap_is_large_enough_for_the_whole_tenant():
    assert tickets.SEARCH_SCAN_CAP >= 2000 and tickets.SEARCH_PAGE_SIZE == 200
    assert (tickets.SEARCH_LIMIT_DEFAULT, tickets.SEARCH_LIMIT_MAX) == (50, 500)


async def test_search_tickets_surfaces_a_total_count_that_disagrees_with_the_rows_read(server, mock_gorelo):
    rows = [ticket_row(1), ticket_row(2), ticket_row(3)]
    mock_gorelo.on("GET", LIST, envelope(rows, pagination(None, 5)))
    result = await call_tool(server, "search_tickets")
    assert result["count_mismatch"] is True and result["complete_scan"] is True and result["total_count"] == 5
    assert "Gorelo reported 5 matches but 3 rows were read" in result["note"] and "repeat the search" in result["note"]


async def test_search_tickets_an_empty_result_is_explicit(server, mock_gorelo):
    lookups(mock_gorelo)
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    result = await call_tool(server, "search_tickets", {"client_id": 9101, "status": "Closed", "unassigned_only": True})
    assert result == {
        "items": [],
        "count": 0,
        "total_count": 0,
        "truncated": False,
        "complete_scan": True,
        "count_mismatch": False,
        "filters": {"client_id": 9101, "status": "Closed", "status_id": 4, "unassigned_only": True, "limit": 50},
        "matched": 0,
        "scanned": 0,
    }


async def test_search_tickets_returns_gorelo_records_unchanged(server, mock_gorelo):
    row = ticket_row(1, ContactId=None, ClosedOn=None, Priority={"Id": 2, "Name": "High"})
    mock_gorelo.on("GET", LIST, paged_envelope([row]))
    assert (await call_tool(server, "search_tickets"))["items"] == [row]


@pytest.mark.parametrize(
    "args, param, field",
    [
        ({"awaiting_client": True}, "awaiting_client", "IsWaitingOnThem"),
        ({"awaiting_client": False}, "awaiting_client", "IsWaitingOnThem"),
        ({"unassigned_only": True}, "unassigned_only", "LeadAssigneeId"),
        ({"unread_only": True}, "unread_only", "IsUnread"),
        ({"exclude_closed": True}, "exclude_closed", "ClosedOn"),
    ],
)
async def test_search_tickets_refuses_a_filter_whose_field_no_row_carries_and_names_the_parameter(
    server, mock_gorelo, args, param, field
):
    rows = []
    for n in (1, 2, 3):
        row = ticket_row(n)
        del row[field]  # for example the old IsAwaitingClient name
        row["IsAwaitingClient"] = False
        rows.append(row)
    mock_gorelo.on("GET", LIST, paged_envelope(rows))
    text = await call_tool_error(server, "search_tickets", args)
    assert text.startswith("Gorelo returned an unexpected response for search_tickets:")
    assert f"none of the 3 scanned ticket rows has a {field} field, so the {param} filter cannot be applied" in text
    assert "refusing to guess" in text


async def test_search_tickets_names_only_the_filter_whose_field_is_missing(server, mock_gorelo):
    rows = [ticket_row(n) for n in (1, 2)]
    for row in rows:
        del row["IsUnread"]
    mock_gorelo.on("GET", LIST, paged_envelope(rows))
    text = await call_tool_error(server, "search_tickets", {"unread_only": True, "exclude_closed": True, "awaiting_client": True})
    assert "IsUnread field, so the unread_only filter cannot be applied" in text
    assert "awaiting_client filter" not in text and "exclude_closed filter" not in text


async def test_search_tickets_does_not_need_the_field_when_that_filter_is_off(server, mock_gorelo):
    row = ticket_row(1)
    del row["IsWaitingOnThem"]
    mock_gorelo.on("GET", LIST, paged_envelope([row]))
    assert (await call_tool(server, "search_tickets", {"unread_only": True}))["count"] == 0


async def test_search_tickets_a_row_that_lacks_a_nullable_field_is_treated_as_null_when_others_have_it(server, mock_gorelo):
    sparse = ticket_row(2)
    del sparse["ClosedOn"]
    mock_gorelo.on("GET", LIST, paged_envelope([ticket_row(1, ClosedOn="2026-09-01T00:00:00Z"), sparse]))
    result = await call_tool(server, "search_tickets", {"exclude_closed": True})
    assert ids_of(result) == [uid(2)]


@pytest.mark.parametrize(
    "args, over, param, field",
    [
        ({"unread_only": True}, {"IsUnread": "yes"}, "unread_only", "IsUnread"),
        ({"awaiting_client": True}, {"IsWaitingOnThem": 1}, "awaiting_client", "IsWaitingOnThem"),
        ({"awaiting_client": False}, {"IsWaitingOnThem": "no"}, "awaiting_client", "IsWaitingOnThem"),
    ],
)
async def test_search_tickets_refuses_a_flag_that_is_not_a_boolean_and_names_the_parameter(server, mock_gorelo, args, over, param, field):
    mock_gorelo.on("GET", LIST, paged_envelope([ticket_row(1, **over)]))
    text = await call_tool_error(server, "search_tickets", args)
    assert f"ticket field {field} is not a boolean, so the {param} filter cannot be applied" in text
    assert "refusing to guess" in text


@pytest.mark.parametrize(
    "args, names",
    [
        ({"unread_only": True}, "unread_only"),
        ({"awaiting_client": False, "exclude_closed": True}, "awaiting_client, exclude_closed"),
        ({"unassigned_only": True, "unread_only": True, "awaiting_client": True, "exclude_closed": True}, "awaiting_client, unassigned_only, unread_only, exclude_closed"),
    ],
)
async def test_search_tickets_refuses_a_row_that_is_not_an_object_and_names_the_filters_it_blocks(server, mock_gorelo, args, names):
    mock_gorelo.on("GET", LIST, paged_envelope(["not a ticket"]))
    text = await call_tool_error(server, "search_tickets", args)
    assert f"a ticket row is not an object, so the filter(s) {names} cannot be applied; refusing to guess" in text


async def test_search_tickets_without_client_side_filters_does_not_look_at_the_rows(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, paged_envelope(["not a ticket"]))
    assert (await call_tool(server, "search_tickets"))["items"] == ["not a ticket"]


async def test_search_tickets_maps_a_gorelo_error_to_the_snake_case_parameter(server, mock_gorelo):
    lookups(mock_gorelo)
    body = error_envelope(400, [("070101", "This query parameter is not recognized by this endpoint.", "StatusIds")], trace_id="00-abc-02")
    mock_gorelo.on("GET", LIST, body)
    text = await call_tool_error(server, "search_tickets", {"status": "Closed"})
    assert text == (
        "Gorelo rejected search_tickets (HTTP 400, code 070101): status: This query parameter is not recognized "
        "by this endpoint. [trace 00-abc-02]"
    )


@pytest.mark.parametrize("prop, param", [("ClientIds", "client_id"), ("PriorityIds", "priority"), ("TypeIds", "type"), ("Query", "query"), ("UpdatedSince", "updated_since"), ("CreatedSince", "created_since")])
async def test_search_tickets_maps_every_server_side_name_back_to_its_parameter(server, mock_gorelo, prop, param):
    mock_gorelo.on("GET", LIST, error_envelope(400, [("070101", "rejected", prop)]))
    text = await call_tool_error(server, "search_tickets")
    assert f"{param}: rejected" in text


async def test_search_tickets_a_failing_lookup_is_reported_by_the_tool_that_needed_it(server, mock_gorelo):
    mock_gorelo.on("GET", STATUSES_PATH, error_envelope(500, [("070001", "Internal error")], trace_id="00-t-5"))
    text = await call_tool_error(server, "search_tickets", {"status": "Closed"})
    assert text.startswith("Gorelo rejected search_tickets (HTTP 500, code 070001): Internal error")
    assert not mock_gorelo.calls("GET", LIST)


async def test_search_tickets_a_lookup_that_is_not_a_list_is_refused(server, mock_gorelo):
    mock_gorelo.on("GET", TYPES_PATH, envelope({"Id": 1}))
    text = await call_tool_error(server, "search_tickets", {"type": "Incident"})
    assert "expected Data to be a list" in text


async def test_search_tickets_a_repeated_cursor_is_an_error_not_a_loop(server, mock_gorelo):
    def same_cursor(request):
        return envelope([ticket_row(1)], pagination("loop", 9, has_more=True))

    mock_gorelo.on("GET", LIST, same_cursor)
    text = await call_tool_error(server, "search_tickets")
    assert "cursor it had already served" in text
    assert len(mock_gorelo.calls("GET", LIST)) == 2


# --------------------------------------------------------------------------
# get_ticket
# --------------------------------------------------------------------------


async def test_get_ticket_with_a_guid_is_one_detail_get_and_returns_the_record_unchanged(server, mock_gorelo):
    override = {
        "ServiceLine": {"Id": 11, "Name": "Support"},
        "BillingRole": {"Id": 22, "Name": "Tech"},
        "WorkType": {"Id": 33, "Name": "Remote"},
        "BillableStatus": {"Id": 1, "Name": "Billable"},
    }
    detail = ticket_detail(1, BillingOverride=override)
    mock_gorelo.on("GET", f"{LIST}/{T1}", envelope(detail))
    result = await call_tool(server, "get_ticket", {"ticket_id": T1})
    assert result == detail
    assert result["Description"] and result["BillingOverride"]["ServiceLine"] == {"Id": 11, "Name": "Support"}
    assert [(r.method, r.path, r.query) for r in mock_gorelo.requests] == [("GET", f"{LIST}/{T1}", {})]


@pytest.mark.parametrize("given", [T1.upper(), T1.replace("-", ""), f"  {T1}  ", T1.replace("-", "").upper()])
async def test_get_ticket_accepts_a_guid_in_any_spelling_and_sends_the_canonical_one(server, mock_gorelo, given):
    mock_gorelo.on("GET", f"{LIST}/{T1}", envelope(ticket_detail(1)))
    await call_tool(server, "get_ticket", {"ticket_id": given})
    assert [r.path for r in mock_gorelo.requests] == [f"{LIST}/{T1}"]


@pytest.mark.parametrize("given", ["1234", 1234, "  1234 "])
async def test_get_ticket_with_a_number_searches_with_query_then_reads_the_matching_ticket(server, mock_gorelo, given):
    rows = [
        ticket_row(1, Number=11234, DisplayNumber="TCK-11234", Title="Printer 1234 offline"),
        ticket_row(2, Number=1234, DisplayNumber="TCK-1234"),
        ticket_row(3, Number=12340, DisplayNumber="TCK-12340"),
    ]
    mock_gorelo.on("GET", LIST, paged_envelope(rows), query={"Query": "1234"})
    detail = ticket_detail(2, Number=1234, DisplayNumber="TCK-1234")
    mock_gorelo.on("GET", f"{LIST}/{T2}", envelope(detail))
    result = await call_tool(server, "get_ticket", {"ticket_id": given})
    assert result == detail
    search, read = mock_gorelo.requests
    assert (search.method, search.path, search.query) == ("GET", LIST, {"Query": "1234", "PageSize": "50"})
    assert (read.method, read.path, read.query) == ("GET", f"{LIST}/{T2}", {})


@pytest.mark.parametrize("given", ["TCK-1234", "tck-1234", "Tck-1234", " TCK-1234 "])
async def test_get_ticket_with_a_display_number_matches_it_case_insensitively(server, mock_gorelo, given):
    rows = [ticket_row(1, Number=1233, DisplayNumber="TCK-1233"), ticket_row(2, Number=1234, DisplayNumber="TCK-1234")]
    mock_gorelo.on("GET", LIST, paged_envelope(rows))
    mock_gorelo.on("GET", f"{LIST}/{T2}", envelope(ticket_detail(2)))
    await call_tool(server, "get_ticket", {"ticket_id": given})
    assert mock_gorelo.calls("GET", LIST)[0].query == {"Query": given.strip(), "PageSize": "50"}
    assert mock_gorelo.last.path == f"{LIST}/{T2}"


async def test_get_ticket_a_number_does_not_match_a_display_number_by_digits_alone(server, mock_gorelo):
    rows = [ticket_row(1, Number=7, DisplayNumber="TCK-1234")]
    mock_gorelo.on("GET", LIST, paged_envelope(rows))
    text = await call_tool_error(server, "get_ticket", {"ticket_id": "1234"})
    assert "no ticket has the number or display number '1234'" in text
    assert [r.path for r in mock_gorelo.requests] == [LIST]


async def test_get_ticket_no_match_is_a_local_error_that_says_so_and_never_reads_a_detail(server, mock_gorelo):
    rows = [ticket_row(1, Number=11234, DisplayNumber="TCK-11234"), ticket_row(2, Number=1235, DisplayNumber="TCK-1235")]
    mock_gorelo.on("GET", LIST, paged_envelope(rows))
    text = await call_tool_error(server, "get_ticket", {"ticket_id": "1234"})
    assert text == (
        "ticket_id: no ticket has the number or display number '1234' (Gorelo returned 2 ticket(s) matching that "
        "text, none with that exact number). Pass a ticket GUID, a number such as 1234 or a display number such as "
        "TCK-1234; to search titles use list_tickets with query"
    )
    assert [r.path for r in mock_gorelo.requests] == [LIST]


async def test_get_ticket_no_candidates_at_all_is_a_local_error_too(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    text = await call_tool_error(server, "get_ticket", {"ticket_id": "TCK-9999"})
    assert "no ticket has the number or display number 'TCK-9999' (Gorelo found no ticket matching that text)" in text
    assert [r.path for r in mock_gorelo.requests] == [LIST]


async def test_get_ticket_several_matches_is_an_error_that_lists_them(server, mock_gorelo):
    rows = [ticket_row(1, Number=1234, DisplayNumber="TCK-1234"), ticket_row(2, Number=7, DisplayNumber="1234")]
    mock_gorelo.on("GET", LIST, paged_envelope(rows))
    text = await call_tool_error(server, "get_ticket", {"ticket_id": "1234"})
    assert text == (
        f"ticket_id: '1234' matches several tickets: TCK-1234 (Id {T1}); 1234 (Id {T2}). "
        "Pass the ticket GUID of the one you mean"
    )
    assert [r.path for r in mock_gorelo.requests] == [LIST]


async def test_get_ticket_the_same_ticket_twice_in_a_page_is_one_match(server, mock_gorelo):
    row = ticket_row(1, Number=1234, DisplayNumber="TCK-1234")
    mock_gorelo.on("GET", LIST, paged_envelope([row, dict(row)]))
    mock_gorelo.on("GET", f"{LIST}/{T1}", envelope(ticket_detail(1)))
    assert (await call_tool(server, "get_ticket", {"ticket_id": "1234"}))["Id"] == T1


async def test_get_ticket_keeps_reading_pages_until_an_exact_match_or_the_end(server, mock_gorelo):
    pages = [
        [ticket_row(n, Number=100 + n, DisplayNumber=f"TCK-{100 + n}") for n in range(1, 4)],
        [ticket_row(4, Number=104, DisplayNumber="TCK-104"), ticket_row(5, Number=5, DisplayNumber="TCK-5")],
        [ticket_row(6, Number=106, DisplayNumber="TCK-106")],
    ]
    mock_gorelo.on("GET", LIST, paged_responder(pages))
    mock_gorelo.on("GET", f"{LIST}/{uid(5)}", envelope(ticket_detail(5, Number=5, DisplayNumber="TCK-5")))
    result = await call_tool(server, "get_ticket", {"ticket_id": "5"})
    assert result["Number"] == 5
    queries = [r.query for r in mock_gorelo.calls("GET", LIST)]
    assert queries == [{"Query": "5", "PageSize": "50"}, {"Query": "5", "PageSize": "50", "Cursor": "c1"}]  # stops at the match


async def test_get_ticket_reaching_the_end_of_the_pages_without_a_match_is_no_match(server, mock_gorelo):
    pages = [[ticket_row(1, Number=101, DisplayNumber="TCK-101")], [ticket_row(2, Number=102, DisplayNumber="TCK-102")]]
    mock_gorelo.on("GET", LIST, paged_responder(pages))
    text = await call_tool_error(server, "get_ticket", {"ticket_id": "9"})
    assert "no ticket has the number or display number '9' (Gorelo returned 2 ticket(s) matching that text" in text
    assert len(mock_gorelo.calls("GET", LIST)) == 2


async def test_get_ticket_stopping_at_the_page_limit_is_called_inconclusive_not_no_match(server, mock_gorelo, monkeypatch):
    monkeypatch.setattr(tickets, "LOOKUP_MAX_PAGES", 2)
    pages = [[ticket_row(n, Number=100 + n, DisplayNumber=f"TCK-{100 + n}")] for n in range(1, 5)]
    mock_gorelo.on("GET", LIST, paged_responder(pages))
    text = await call_tool_error(server, "get_ticket", {"ticket_id": "9"})
    assert text == (
        "ticket_id: '9' was not found in the first 2 of 4 tickets Gorelo returned for that text (the lookup reads at "
        "most 2 pages), so it is inconclusive. Pass the ticket GUID, or a longer display number such as TCK-1234"
    )
    assert len(mock_gorelo.calls("GET", LIST)) == 2
    assert not mock_gorelo.calls("GET", f"{LIST}/{{ticketId}}")


def test_get_ticket_the_lookup_walk_is_bounded_by_default():
    assert tickets.LOOKUP_PAGE_SIZE == 50 and 10 <= tickets.LOOKUP_MAX_PAGES <= 100


async def test_get_ticket_a_repeated_cursor_in_the_lookup_is_an_error(server, mock_gorelo):
    def same_cursor(request):
        return envelope([ticket_row(1, Number=1, DisplayNumber="TCK-1")], pagination("loop", 9, has_more=True))

    mock_gorelo.on("GET", LIST, same_cursor)
    text = await call_tool_error(server, "get_ticket", {"ticket_id": "999"})
    assert "cursor it had already served" in text


async def test_get_ticket_a_matching_row_without_an_id_is_refused(server, mock_gorelo):
    row = ticket_row(1, Number=1234)
    del row["Id"]
    mock_gorelo.on("GET", LIST, paged_envelope([row]))
    text = await call_tool_error(server, "get_ticket", {"ticket_id": "1234"})
    assert "a ticket row has no Id; refusing to guess" in text


async def test_get_ticket_a_list_row_that_is_not_an_object_is_refused(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, paged_envelope([42]))
    text = await call_tool_error(server, "get_ticket", {"ticket_id": "1234"})
    assert "a ticket row is not an object" in text


@pytest.mark.parametrize(
    "given, fragment",
    [
        ("", "ticket_id: must not be empty; give a ticket GUID, a number such as 1234 or a display number such as TCK-1234"),
        ("   ", "ticket_id: must not be empty"),
        ("g" * 201, "ticket_id: at most 200 characters, got 201"),
    ],
)
async def test_get_ticket_local_validation_errors_send_nothing(server, mock_gorelo, given, fragment):
    text = await call_tool_error(server, "get_ticket", {"ticket_id": given})
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_get_ticket_a_missing_ticket_is_a_gorelo_error_from_the_detail_read(server, mock_gorelo):
    mock_gorelo.on("GET", f"{LIST}/{T3}", error_envelope(404, [("070401", "Ticket not found")], trace_id="00-t-404"))
    text = await call_tool_error(server, "get_ticket", {"ticket_id": T3})
    assert text == "Gorelo rejected get_ticket (HTTP 404, code 070401): Ticket not found [trace 00-t-404]"


async def test_get_ticket_maps_a_query_error_of_the_lookup_to_ticket_id(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, error_envelope(400, [("070101", "Query is too long.", "Query")], trace_id="00-t-9"))
    text = await call_tool_error(server, "get_ticket", {"ticket_id": "1234"})
    assert text == "Gorelo rejected get_ticket (HTTP 400, code 070101): ticket_id: Query is too long. [trace 00-t-9]"


async def test_get_ticket_refuses_a_detail_whose_data_is_null(server, mock_gorelo):
    mock_gorelo.on("GET", f"{LIST}/{T1}", envelope(None))
    text = await call_tool_error(server, "get_ticket", {"ticket_id": T1})
    assert "Data is null" in text and text.startswith("Gorelo returned an unexpected response for get_ticket")


# --------------------------------------------------------------------------
# create_ticket
# --------------------------------------------------------------------------

CREATE_MIN = {
    "title": "Printer offline",
    "description": "The 3rd floor printer is offline.",
    "client_id": 9102,
    "status_id": 1,
    "type_id": 7101,
    "priority_id": 3,
    "source_id": 6,
    "group_id": 7201,
}
CREATE_MIN_BODY = {
    "Title": "Printer offline",
    "Description": "The 3rd floor printer is offline.",
    "ClientId": 9102,
    "StatusId": 1,
    "TypeId": 7101,
    "PriorityId": 3,
    "SourceId": 6,
    "GroupId": 7201,
    "SendTicketCreatedEmail": False,
}
CREATE_FULL = {
    **CREATE_MIN,
    "contact_id": 9103,
    "cc_contact_ids": [9104, 9105],
    "location_id": 3001,
    "lead_assignee_id": 9201,
    "assisting_assignee_ids": [9202],
    "watcher_ids": [9201, 9202],
    "tag_ids": [5, 6],
    "agent_asset_ids": [uid(11)],
    "custom_asset_ids": [uid(12).upper()],
    "uptime_ids": [uid(13).replace("-", "")],
    "created_on": "2020-03-01T09:00:00-05:00",
    "updated_on": "2020-03-02T00:00:00Z",
    "closed_on": "2020-03-03T12:00:00+02:00",
    "created_by_name": "Import bot",
    "is_unread": False,
    "send_created_email": True,
}
CREATE_FULL_BODY = {
    **CREATE_MIN_BODY,
    "ContactId": 9103,
    "CcContactIds": [9104, 9105],
    "LocationId": 3001,
    "LeadAssigneeId": 9201,
    "AssistingAssigneeIds": [9202],
    "WatcherIds": [9201, 9202],
    "TagIds": [5, 6],
    "AgentAssetIds": [uid(11)],
    "CustomAssetIds": [uid(12)],
    "UptimeIds": [uid(13)],
    "CreatedOn": "2020-03-01T14:00:00Z",
    "UpdatedOn": "2020-03-02T00:00:00Z",
    "ClosedOn": "2020-03-03T10:00:00Z",
    "CreatedByName": "Import bot",
    "IsUnread": False,
    "SendTicketCreatedEmail": True,
}


def created(mock, n=5, detail=None):
    mock.on("POST", LIST, envelope({"Id": uid(n)}))
    mock.on("GET", f"{LIST}/{uid(n)}", envelope(detail or ticket_detail(n)))


async def test_create_ticket_with_the_required_fields_posts_exactly_them_then_rereads_the_detail(server, mock_gorelo):
    created(mock_gorelo)
    result = await call_tool(server, "create_ticket", CREATE_MIN)
    assert result == ticket_detail(5)
    post, read = mock_gorelo.requests
    assert (post.method, post.path, post.query) == ("POST", LIST, {})
    assert post.json == CREATE_MIN_BODY and no_nulls(post.json)
    assert (read.method, read.path, read.query, read.json) == ("GET", f"{LIST}/{uid(5)}", {}, None)


async def test_create_ticket_sends_every_optional_field_in_pascal_case_with_normalized_values(server, mock_gorelo):
    created(mock_gorelo)
    await call_tool(server, "create_ticket", CREATE_FULL)
    body = mock_gorelo.calls("POST", LIST)[0].json
    assert body == CREATE_FULL_BODY and no_nulls(body)


async def test_create_ticket_never_sends_a_field_the_caller_did_not_give(server, mock_gorelo):
    created(mock_gorelo)
    await call_tool(server, "create_ticket", {**CREATE_MIN, "contact_id": 9103})
    body = mock_gorelo.calls("POST", LIST)[0].json
    assert set(body) == set(CREATE_MIN_BODY) | {"ContactId"}


SINGLE_CREATE_FIELDS = [
    ("contact_id", 9103, "ContactId", 9103),
    ("cc_contact_ids", [9104], "CcContactIds", [9104]),
    ("location_id", 3001, "LocationId", 3001),
    ("lead_assignee_id", 9201, "LeadAssigneeId", 9201),
    ("assisting_assignee_ids", [9202, 9201], "AssistingAssigneeIds", [9202, 9201]),
    ("watcher_ids", [9201], "WatcherIds", [9201]),
    ("tag_ids", [5, 6], "TagIds", [5, 6]),
    ("agent_asset_ids", [uid(21)], "AgentAssetIds", [uid(21)]),
    ("custom_asset_ids", [uid(22).upper()], "CustomAssetIds", [uid(22)]),
    ("uptime_ids", [uid(23).replace("-", "")], "UptimeIds", [uid(23)]),
    ("created_on", "2020-05-05T05:05:05-05:00", "CreatedOn", "2020-05-05T10:05:05Z"),
    ("created_by_name", "Import bot", "CreatedByName", "Import bot"),
    ("is_unread", True, "IsUnread", True),
    ("is_unread", False, "IsUnread", False),
]


@pytest.mark.parametrize("param, value, field, sent", SINGLE_CREATE_FIELDS)
async def test_create_ticket_each_optional_field_alone_adds_only_its_own_body_field(server, mock_gorelo, param, value, field, sent):
    created(mock_gorelo)
    await call_tool(server, "create_ticket", {**CREATE_MIN, param: value})
    assert mock_gorelo.calls("POST", LIST)[0].json == {**CREATE_MIN_BODY, field: sent}


@pytest.mark.parametrize("flag", [True, False])
async def test_create_ticket_always_states_whether_to_email_the_contact(server, mock_gorelo, flag):
    created(mock_gorelo)
    await call_tool(server, "create_ticket", {**CREATE_MIN, "send_created_email": flag})
    assert mock_gorelo.calls("POST", LIST)[0].json["SendTicketCreatedEmail"] is flag


async def test_create_ticket_sends_false_for_is_unread_and_keeps_priority_zero(server, mock_gorelo):
    created(mock_gorelo)
    await call_tool(server, "create_ticket", {**CREATE_MIN, "priority_id": 0, "is_unread": False})
    body = mock_gorelo.calls("POST", LIST)[0].json
    assert body["PriorityId"] == 0 and body["IsUnread"] is False


@pytest.mark.parametrize("priority", [0, 1, 2, 3, 4])
async def test_create_ticket_accepts_each_priority_and_sends_it_unchanged(server, mock_gorelo, priority):
    created(mock_gorelo)
    await call_tool(server, "create_ticket", {**CREATE_MIN, "priority_id": priority})
    assert mock_gorelo.calls("POST", LIST)[0].json["PriorityId"] == priority


@pytest.mark.parametrize("source", [1, 2, 3, 4, 5, 6])
async def test_create_ticket_accepts_each_source_and_sends_it_unchanged(server, mock_gorelo, source):
    created(mock_gorelo)
    await call_tool(server, "create_ticket", {**CREATE_MIN, "source_id": source})
    assert mock_gorelo.calls("POST", LIST)[0].json["SourceId"] == source


async def test_create_ticket_a_backdated_import_sends_the_three_dates_in_utc(server, mock_gorelo):
    created(mock_gorelo)
    await call_tool(
        server, "create_ticket",
        {**CREATE_MIN, "created_on": "2020-01-01T00:00:00Z", "updated_on": "2020-01-02T00:00:00Z", "closed_on": "2020-01-02T00:00:00Z"},
    )
    body = mock_gorelo.calls("POST", LIST)[0].json
    assert (body["CreatedOn"], body["UpdatedOn"], body["ClosedOn"]) == ("2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z", "2020-01-02T00:00:00Z")


async def test_create_ticket_created_on_alone_is_enough_for_a_backdated_ticket(server, mock_gorelo):
    created(mock_gorelo)
    await call_tool(server, "create_ticket", {**CREATE_MIN, "created_on": "2020-01-01T00:00:00+00:00"})
    body = mock_gorelo.calls("POST", LIST)[0].json
    assert body["CreatedOn"] == "2020-01-01T00:00:00Z" and "UpdatedOn" not in body and "ClosedOn" not in body


REREAD = "GET /v1/tickets/{ticketId}"


@pytest.mark.parametrize(
    "response, reason",
    [
        pytest.param(error_envelope(404, [("070401", "Ticket not found")], trace_id="00-t-1"),
                     f"{REREAD} answered HTTP 404 (code 070401): Ticket not found [trace 00-t-1]", id="404"),
        pytest.param(error_envelope(500, [("070001", "Internal error")], trace_id="00-t-2"),
                     f"{REREAD} answered HTTP 500 (code 070001): Internal error [trace 00-t-2]", id="500"),
        pytest.param(httpx.ReadTimeout("slow"), f"{REREAD} timed out", id="timeout"),
        pytest.param(httpx.ConnectError("refused"), f"the connection to Gorelo failed during {REREAD}", id="connection"),
        pytest.param(httpx.Response(502, text="<html>Bad Gateway</html>"), f"{REREAD}: unexpected response shape; refusing to guess (HTTP 502", id="gateway-page"),
        pytest.param(envelope(None), f"{REREAD}: Gorelo reported success but Data is null; refusing to guess", id="data-null"),
        pytest.param(envelope([ticket_detail(5)]), f"{REREAD} returned a list instead of the record", id="data-is-a-list"),
    ],
)
async def test_create_ticket_a_failed_reread_returns_the_id_and_a_warning_and_never_repeats_the_write(
    server, mock_gorelo, response, reason
):
    mock_gorelo.on("POST", LIST, envelope({"Id": uid(5)}))
    mock_gorelo.on("GET", f"{LIST}/{uid(5)}", response)
    result = await call_tool(server, "create_ticket", CREATE_MIN)
    assert set(result) == {"Id", "warning"} and result["Id"] == uid(5)
    assert result["warning"].startswith(f"the write succeeded; re-reading it failed: {reason}")
    assert result["warning"].endswith("Do not repeat the write; read it again later.")
    assert "create_ticket" not in result["warning"] and "Gorelo rejected" not in result["warning"]
    assert len(mock_gorelo.calls("POST", LIST)) == 1 and len(mock_gorelo.calls("GET")) == 1


async def test_create_ticket_reads_back_an_id_that_came_with_padding(server, mock_gorelo):
    mock_gorelo.on("POST", LIST, envelope({"Id": f"  {uid(9)} "}))
    mock_gorelo.on("GET", f"{LIST}/{uid(9)}", envelope(ticket_detail(9)))
    assert (await call_tool(server, "create_ticket", CREATE_MIN))["Id"] == uid(9)


async def test_create_ticket_the_reread_ids_are_the_ones_gorelo_returned(server, mock_gorelo):
    mock_gorelo.on("POST", LIST, envelope({"Id": uid(9).upper()}))
    mock_gorelo.on("GET", f"{LIST}/{uid(9)}", envelope(ticket_detail(9)))
    assert (await call_tool(server, "create_ticket", CREATE_MIN))["Id"] == uid(9)


CREATE_BAD_ANSWERS = [
    pytest.param(None, "Data is null, not an object with an Id", id="null"),
    pytest.param({}, "Data is an object without an Id", id="empty-object"),
    pytest.param(False, "Data is a boolean, not an object with an Id", id="false"),
    pytest.param(True, "Data is a boolean, not an object with an Id", id="true"),
    pytest.param([], "Data is an empty list, not an object with an Id", id="empty-list"),
    pytest.param([{"Id": uid(5)}], "Data is a list of 1 item, not an object with an Id", id="list-with-an-object"),
    pytest.param("done", "Data is a string, not an object with an Id", id="string"),
    pytest.param(0, "Data is a number, not an object with an Id", id="number"),
    pytest.param({"id": uid(5)}, "Data is an object without an Id", id="lowercase-key"),
    pytest.param({"Id": None}, "Data.Id is null", id="null-id"),
    pytest.param({"Id": ""}, "Data.Id is blank", id="blank-id"),
    pytest.param({"Id": "  "}, "Data.Id is blank", id="whitespace-id"),
    pytest.param({"Id": 0}, "Data.Id is zero or negative", id="zero-id"),
    pytest.param({"Id": True}, "Data.Id is a boolean", id="boolean-id"),
    pytest.param({"Id": 1.5}, "Data.Id is a decimal number", id="decimal-id"),
]


@pytest.mark.parametrize("data, problem", CREATE_BAD_ANSWERS)
async def test_create_ticket_a_success_without_a_usable_id_is_a_shape_error_that_says_to_verify_before_retrying(
    server, mock_gorelo, data, problem
):
    mock_gorelo.on("POST", LIST, envelope(data))
    text = await call_tool_error(server, "create_ticket", CREATE_MIN)
    assert text.startswith(
        "Gorelo returned an unexpected response for create_ticket: POST /v1/tickets: Gorelo reported success but "
        f"the answer carries no usable Id for the record ({problem})"
    )
    assert "the write may have been applied, so verify it with a read before repeating it" in text
    assert [r.method for r in mock_gorelo.requests] == ["POST"]  # not retried, not re-read


async def test_create_ticket_a_malformed_id_in_the_answer_is_a_warning_not_a_failure(server, mock_gorelo):
    mock_gorelo.on("POST", LIST, envelope({"Id": "not-a-guid"}))
    result = await call_tool(server, "create_ticket", CREATE_MIN)
    assert result["Id"] == "not-a-guid" and "'ticketId' must be a UUID" in result["warning"]
    assert [r.method for r in mock_gorelo.requests] == ["POST"]


async def test_create_ticket_a_numeric_id_in_the_answer_is_a_shape_error_because_a_ticket_id_is_a_guid(server, mock_gorelo):
    mock_gorelo.on("POST", LIST, envelope({"Id": 5}))
    text = await call_tool_error(server, "create_ticket", CREATE_MIN)
    assert text == (
        "Gorelo returned an unexpected response for create_ticket: POST /v1/tickets: Gorelo reported success but the "
        "new ticket's Id is a number, not a GUID; the write may have been applied, so verify it with a read before "
        "repeating it"
    )
    assert [r.method for r in mock_gorelo.requests] == ["POST"]  # not retried, no re-read of a number


@pytest.mark.parametrize(
    "prop, param",
    [
        ("Title", "title"), ("Description", "description"), ("ClientId", "client_id"), ("StatusId", "status_id"),
        ("TypeId", "type_id"), ("PriorityId", "priority_id"), ("SourceId", "source_id"), ("GroupId", "group_id"),
        ("ContactId", "contact_id"), ("CcContactIds", "cc_contact_ids"), ("LocationId", "location_id"),
        ("LeadAssigneeId", "lead_assignee_id"), ("TagIds", "tag_ids"), ("AgentAssetIds", "agent_asset_ids"),
        ("CreatedOn", "created_on"), ("ClosedOn", "closed_on"), ("CreatedByName", "created_by_name"),
        ("SendTicketCreatedEmail", "send_created_email"), ("statusid", "status_id"),
    ],
)
async def test_create_ticket_maps_a_gorelo_error_to_the_snake_case_parameter(server, mock_gorelo, prop, param):
    mock_gorelo.on("POST", LIST, error_envelope(400, [("070101", "is not valid", prop)], trace_id="00-c-1"))
    text = await call_tool_error(server, "create_ticket", CREATE_MIN)
    assert text == f"Gorelo rejected create_ticket (HTTP 400, code 070101): {param}: is not valid [trace 00-c-1]"
    assert len(mock_gorelo.requests) == 1  # no re-read after a rejected write


async def test_create_ticket_shows_every_notification_of_a_rejection(server, mock_gorelo):
    notes = [("070101", "StatusId must exist", "StatusId"), ("070101", "GroupId must exist", "GroupId"), ("070201", "Invalid or malformed request body.", None)]
    mock_gorelo.on("POST", LIST, error_envelope(400, notes, trace_id="00-c-2"))
    text = await call_tool_error(server, "create_ticket", CREATE_MIN)
    assert text == (
        "Gorelo rejected create_ticket (HTTP 400, code 070101/070201): status_id: StatusId must exist; "
        "group_id: GroupId must exist; Invalid or malformed request body. [trace 00-c-2]"
    )


async def test_create_ticket_a_timeout_is_not_confirmed_never_retried_and_never_reread(server, mock_gorelo):
    mock_gorelo.on("POST", LIST, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "create_ticket", CREATE_MIN)
    assert text.startswith("Gorelo did not confirm create_ticket (the request timed out). The change may or may not have been applied.")
    assert "Verify with a read before retrying" in text
    assert [r.method for r in mock_gorelo.requests] == ["POST"]


async def test_create_ticket_a_server_error_is_not_retried(server, mock_gorelo):
    mock_gorelo.on("POST", LIST, error_envelope(500, [("070001", "Internal error")]))
    text = await call_tool_error(server, "create_ticket", CREATE_MIN)
    assert "Gorelo may have applied the change before failing. Verify with a read before retrying." in text
    assert len(mock_gorelo.calls("POST", LIST)) == 1


async def test_create_ticket_a_scope_error_names_the_scope(server, mock_gorelo):
    mock_gorelo.on("POST", LIST, error_envelope(403, [("080203", "API key does not have 'Tickets' scope")]))
    text = await call_tool_error(server, "create_ticket", CREATE_MIN)
    assert "the API key does not have the 'Tickets' scope" in text


CREATE_VALIDATION = [
    pytest.param({"title": ""}, "title: must not be empty or whitespace only", id="blank-title"),
    pytest.param({"title": "   "}, "title: must not be empty or whitespace only", id="whitespace-title"),
    pytest.param({"title": "t" * 251}, "title: at most 250 characters, got 251", id="long-title"),
    pytest.param({"description": ""}, "description: must not be empty or whitespace only", id="blank-description"),
    pytest.param({"client_id": 0}, f"client_id: {NOT_POSITIVE}", id="client-zero"),
    pytest.param({"status_id": -1}, f"status_id: {NOT_POSITIVE}", id="status-negative"),
    pytest.param({"type_id": 0}, f"type_id: {NOT_POSITIVE}", id="type-zero"),
    pytest.param({"group_id": 0}, f"group_id: {NOT_POSITIVE}", id="group-zero"),
    pytest.param({"client_id": 2**63}, "client_id: expected a positive whole number such as 123, got a number above", id="client-above-int64"),
    pytest.param({"priority_id": 5}, "priority_id: must be one of 0 None, 1 Urgent, 2 High, 3 Normal, 4 Low", id="priority-5"),
    pytest.param({"priority_id": -1}, "priority_id: must be one of 0 None", id="priority-negative"),
    pytest.param({"source_id": 0}, f"source_id: must be one of {tickets.SOURCE_HELP}; Id 7 exists on older tickets but cannot be set", id="source-0"),
    pytest.param({"source_id": 7}, f"source_id: must be one of {tickets.SOURCE_HELP}; Id 7 exists on older tickets but cannot be set", id="source-7"),
    pytest.param({"contact_id": 0}, f"contact_id: {NOT_POSITIVE}", id="contact-zero"),
    pytest.param({"location_id": 0}, f"location_id: {NOT_POSITIVE}", id="location-zero"),
    pytest.param({"lead_assignee_id": 0}, f"lead_assignee_id: {NOT_POSITIVE}", id="assignee-zero"),
    pytest.param({"cc_contact_ids": []}, f"cc_contact_ids: {NO_IDS} (omit cc_contact_ids if you have no ids to give)", id="empty-cc"),
    pytest.param({"cc_contact_ids": [0]}, f"cc_contact_ids[0]: {NOT_POSITIVE}", id="cc-zero"),
    pytest.param({"assisting_assignee_ids": []}, f"assisting_assignee_ids: {NO_IDS}", id="empty-assisting"),
    pytest.param({"watcher_ids": []}, f"watcher_ids: {NO_IDS}", id="empty-watchers"),
    pytest.param({"tag_ids": []}, f"tag_ids: {NO_IDS}", id="empty-tags"),
    pytest.param({"agent_asset_ids": []}, "agent_asset_ids: expected at least one GUID, got an empty list", id="empty-agents"),
    pytest.param({"custom_asset_ids": []}, "custom_asset_ids: expected at least one GUID, got an empty list", id="empty-custom-assets"),
    pytest.param({"uptime_ids": []}, "uptime_ids: expected at least one GUID, got an empty list", id="empty-uptime"),
    pytest.param({"agent_asset_ids": ["not-a-uuid"]}, f"agent_asset_ids[0]: {NOT_A_GUID}", id="bad-agent-uuid"),
    pytest.param({"custom_asset_ids": [uid(1), "12345"]}, f"custom_asset_ids[1]: {NOT_A_GUID}", id="bad-custom-uuid"),
    pytest.param({"agent_asset_ids": [f" {uid(1)}"]}, f"agent_asset_ids[0]: {NOT_A_GUID}", id="padded-uuid-is-not-trimmed"),
    pytest.param({"uptime_ids": [""]}, "uptime_ids[0]: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got an empty string", id="blank-uptime-uuid"),
    pytest.param({"created_on": "2020-01-01T00:00:00"}, "created_on: '2020-01-01T00:00:00' has no UTC offset", id="naive-created-on"),
    pytest.param({"created_on": "2099-01-01T00:00:00Z"}, "created_on: 2099-01-01T00:00:00Z is in the future", id="future-created-on"),
    pytest.param({"created_on": "2099-01-01T00:00:00+05:00"}, "created_on: 2098-12-31T19:00:00Z is in the future", id="future-created-on-with-offset"),
    pytest.param({"updated_on": "2020-01-01T00:00:00Z"}, "updated_on: Gorelo only accepts updated_on together with created_on", id="updated-on-needs-created-on"),
    pytest.param({"closed_on": "2020-01-01T00:00:00Z"}, "closed_on: Gorelo only accepts closed_on together with created_on", id="closed-on-needs-created-on"),
    pytest.param({"created_on": "2020-01-01T00:00:00Z", "updated_on": "2099-01-01T00:00:00Z"}, "updated_on: 2099-01-01T00:00:00Z is in the future", id="future-updated-on"),
    pytest.param({"created_on": "2020-01-01T00:00:00Z", "closed_on": "2099-01-01T00:00:00Z"}, "closed_on: 2099-01-01T00:00:00Z is in the future", id="future-closed-on"),
    pytest.param({"created_on": "2020-01-02T00:00:00Z", "closed_on": "2020-01-01T00:00:00Z"}, "closed_on: 2020-01-01T00:00:00Z is earlier than created_on (2020-01-02T00:00:00Z)", id="closed-before-created"),
    pytest.param({"created_on": "2020-01-02T00:00:00Z", "updated_on": "2020-01-01T23:59:59Z"}, "updated_on: 2020-01-01T23:59:59Z is earlier than created_on", id="updated-before-created"),
    pytest.param({"created_on": "2020-01-02T00:00:00Z", "closed_on": "soon"}, "closed_on: 'soon' is not an ISO 8601 datetime", id="bad-closed-on"),
    pytest.param({"created_by_name": ""}, "created_by_name: must not be empty or whitespace only", id="blank-created-by-name"),
]


@pytest.mark.parametrize("override, fragment", CREATE_VALIDATION)
async def test_create_ticket_local_validation_errors_name_the_parameter_and_send_nothing(server, mock_gorelo, override, fragment):
    text = await call_tool_error(server, "create_ticket", {**CREATE_MIN, **override})
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("missing", sorted(CREATE_MIN))
async def test_create_ticket_every_legacy_required_parameter_is_still_required(server, mock_gorelo, missing):
    args = {k: v for k, v in CREATE_MIN.items() if k != missing}
    text = await call_tool_error(server, "create_ticket", args)
    assert missing in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("param", ["title", "description"])
async def test_create_ticket_a_blank_required_text_is_refused_without_advice_to_omit_it(server, mock_gorelo, param):
    # both are required: the message must not suggest leaving them out
    text = await call_tool_error(server, "create_ticket", {**CREATE_MIN, param: "  "})
    assert text == f"{param}: must not be empty or whitespace only"
    assert mock_gorelo.requests == []


async def test_create_ticket_rejects_values_of_the_wrong_kind_before_any_http_call(server, mock_gorelo):
    for override, param in (({"client_id": "acme"}, "client_id"), ({"priority_id": "high"}, "priority_id"), ({"tag_ids": ["x"]}, "tag_ids"), ({"is_unread": "maybe"}, "is_unread")):
        text = await call_tool_error(server, "create_ticket", {**CREATE_MIN, **override})
        assert param in text
    assert mock_gorelo.requests == []


async def test_create_ticket_validates_everything_before_the_write(server, mock_gorelo):
    created(mock_gorelo)
    # the second id is bad: nothing may be written even though the first fields are fine
    text = await call_tool_error(server, "create_ticket", {**CREATE_MIN, "watcher_ids": [9201, 0]})
    assert f"watcher_ids[1]: {NOT_POSITIVE}" in text
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# update_ticket
# --------------------------------------------------------------------------


def updated(mock, ticket=T1, n=1, detail=None):
    mock.on("PATCH", f"{LIST}/{ticket}", envelope({"Id": ticket}))
    mock.on("GET", f"{LIST}/{ticket}", envelope(detail or ticket_detail(n)))


async def test_update_ticket_sends_only_the_given_field_then_rereads_and_returns_the_detail(server, mock_gorelo):
    updated(mock_gorelo, detail=ticket_detail(1, Title="New title"))
    result = await call_tool(server, "update_ticket", {"ticket_id": T1, "title": "New title"})
    assert result == ticket_detail(1, Title="New title")
    patch, read = mock_gorelo.requests
    assert (patch.method, patch.path, patch.query) == ("PATCH", f"{LIST}/{T1}", {})
    assert patch.json == {"Title": "New title"}
    assert (read.method, read.path, read.query, read.json) == ("GET", f"{LIST}/{T1}", {}, None)


SINGLE_UPDATE_FIELDS = [
    ("title", "Renamed", {"Title": "Renamed"}),
    ("status_id", 4, {"StatusId": 4}),
    ("priority_id", 1, {"PriorityId": 1}),
    ("type_id", 9301, {"TypeId": 9301}),
    ("client_id", 9101, {"ClientId": 9101}),
    ("location_id", 3001, {"LocationId": 3001}),
    ("contact_id", 9103, {"ContactId": 9103}),
    ("cc_contact_ids", [9104, 9105], {"CcContactIds": [9104, 9105]}),
    ("group_ids", [7201], {"GroupIds": [7201]}),
    ("lead_assignee_id", 9202, {"LeadAssigneeId": 9202}),
    ("assisting_assignee_ids", [9201], {"AssistingAssigneeIds": [9201]}),
    ("watcher_ids", [9201, 9202], {"WatcherIds": [9201, 9202]}),
    ("tag_ids", [5], {"TagIds": [5]}),
    ("agent_asset_ids", [uid(21)], {"AgentAssetIds": [uid(21)]}),
    ("custom_asset_ids", [uid(22).upper()], {"CustomAssetIds": [uid(22)]}),
    ("uptime_ids", [uid(23).replace("-", "")], {"UptimeIds": [uid(23)]}),
    ("closed_on", "2020-05-05T05:05:05-05:00", {"ClosedOn": "2020-05-05T10:05:05Z"}),
]


@pytest.mark.parametrize("param, value, body", SINGLE_UPDATE_FIELDS)
async def test_update_ticket_each_field_alone_sends_only_its_own_body_field(server, mock_gorelo, param, value, body):
    updated(mock_gorelo)
    await call_tool(server, "update_ticket", {"ticket_id": T1, param: value})
    assert mock_gorelo.calls("PATCH")[0].json == body


async def test_update_ticket_sends_every_changeable_field_in_pascal_case(server, mock_gorelo):
    updated(mock_gorelo)
    await call_tool(
        server,
        "update_ticket",
        {
            "ticket_id": T1,
            "title": "Renamed",
            "status_id": 4,
            "priority_id": 2,
            "type_id": 9301,
            "client_id": 9101,
            "location_id": 3001,
            "contact_id": 9103,
            "cc_contact_ids": [9104],
            "group_ids": [7201, 7202],
            "lead_assignee_id": 9202,
            "assisting_assignee_ids": [9201],
            "watcher_ids": [9201, 9202],
            "tag_ids": [5],
            "agent_asset_ids": [uid(11)],
            "custom_asset_ids": [uid(12).upper()],
            "uptime_ids": [uid(13).replace("-", "")],
            "closed_on": "2020-03-03T12:00:00+02:00",
            "updated_on": "2020-03-03T10:30:00Z",
            "updated_by_name": "Import bot",
            "billing_service_line_id": 11,
            "billing_role_id": 22,
            "billing_work_type_id": 33,
            "billable_status_id": 2,
        },
    )
    body = mock_gorelo.calls("PATCH", f"{LIST}/{T1}")[0].json
    assert body == {
        "Title": "Renamed",
        "StatusId": 4,
        "PriorityId": 2,
        "TypeId": 9301,
        "ClientId": 9101,
        "LocationId": 3001,
        "ContactId": 9103,
        "CcContactIds": [9104],
        "GroupIds": [7201, 7202],
        "LeadAssigneeId": 9202,
        "AssistingAssigneeIds": [9201],
        "WatcherIds": [9201, 9202],
        "TagIds": [5],
        "AgentAssetIds": [uid(11)],
        "CustomAssetIds": [uid(12)],
        "UptimeIds": [uid(13)],
        "ClosedOn": "2020-03-03T10:00:00Z",
        "UpdatedOn": "2020-03-03T10:30:00Z",
        "UpdatedByName": "Import bot",
        "BillingOverride": {"ServiceLineId": 11, "BillingRoleId": 22, "WorkTypeId": 33, "BillableStatusId": 2},
    }
    assert no_nulls(body)


async def test_update_ticket_nests_the_four_billing_parameters_into_billing_override(server, mock_gorelo):
    updated(mock_gorelo)
    await call_tool(
        server, "update_ticket",
        {"ticket_id": T1, "billing_service_line_id": 11, "billing_role_id": 22, "billing_work_type_id": 33, "billable_status_id": 3},
    )
    body = mock_gorelo.calls("PATCH")[0].json
    assert body == {"BillingOverride": {"ServiceLineId": 11, "BillingRoleId": 22, "WorkTypeId": 33, "BillableStatusId": 3}}
    assert "ContractServiceId" not in json.dumps(body)


@pytest.mark.parametrize(
    "param, field, value",
    [
        ("billing_service_line_id", "ServiceLineId", 11),
        ("billing_role_id", "BillingRoleId", 22),
        ("billing_work_type_id", "WorkTypeId", 33),
        ("billable_status_id", "BillableStatusId", 1),
        ("billable_status_id", "BillableStatusId", 2),
        ("billable_status_id", "BillableStatusId", 3),
    ],
)
async def test_update_ticket_one_billing_parameter_on_a_ticket_with_no_override_sends_only_that_nested_field(
    server, mock_gorelo, param, field, value
):
    updated(mock_gorelo)  # the default detail has no billing override (every Id 0)
    await call_tool(server, "update_ticket", {"ticket_id": T1, param: value})
    assert mock_gorelo.calls("PATCH")[0].json == {"BillingOverride": {field: value}}


async def test_update_ticket_billing_next_to_other_fields_keeps_the_rest_at_the_top_level(server, mock_gorelo):
    updated(mock_gorelo)
    await call_tool(server, "update_ticket", {"ticket_id": T1, "status_id": 3, "billing_role_id": 22})
    assert mock_gorelo.calls("PATCH")[0].json == {"StatusId": 3, "BillingOverride": {"BillingRoleId": 22}}


# --------------------------------------------------------------------------
# update_ticket: a partial billing override is completed from the ticket first
# --------------------------------------------------------------------------

DETAIL_PATH = f"{LIST}/{T1}"


def billing_detail(service_line=0, role=0, work_type=0, status=0, **over):
    """A ticket detail whose BillingOverride holds these ids (0 means "not set", as the detail shows it)."""
    override = {
        "ServiceLine": {"Id": service_line, "Name": "Support" if service_line else ""},
        "BillingRole": {"Id": role, "Name": "Tech" if role else ""},
        "WorkType": {"Id": work_type, "Name": "Remote" if work_type else ""},
        "BillableStatus": {"Id": status, "Name": "Billable" if status else ""},
    }
    return ticket_detail(1, BillingOverride=override, **over)


async def test_update_ticket_one_billing_field_given_keeps_the_three_existing_ones(server, mock_gorelo):
    updated(mock_gorelo, detail=billing_detail(service_line=11, role=7, work_type=33, status=1))
    result = await call_tool(server, "update_ticket", {"ticket_id": T1, "billing_role_id": 22})
    assert [(r.method, r.path, r.query) for r in mock_gorelo.requests] == [
        ("GET", DETAIL_PATH, {}), ("PATCH", DETAIL_PATH, {}), ("GET", DETAIL_PATH, {}),
    ]  # the read before the write, then the read back
    assert mock_gorelo.calls("PATCH")[0].json == {
        "BillingOverride": {"ServiceLineId": 11, "BillingRoleId": 22, "WorkTypeId": 33, "BillableStatusId": 1}
    }
    assert result == billing_detail(service_line=11, role=7, work_type=33, status=1)


@pytest.mark.parametrize(
    "param, value, expected",
    [
        ("billing_service_line_id", 99, {"ServiceLineId": 99, "BillingRoleId": 7, "WorkTypeId": 33, "BillableStatusId": 1}),
        ("billing_role_id", 22, {"ServiceLineId": 11, "BillingRoleId": 22, "WorkTypeId": 33, "BillableStatusId": 1}),
        ("billing_work_type_id", 44, {"ServiceLineId": 11, "BillingRoleId": 7, "WorkTypeId": 44, "BillableStatusId": 1}),
        ("billable_status_id", 2, {"ServiceLineId": 11, "BillingRoleId": 7, "WorkTypeId": 33, "BillableStatusId": 2}),
    ],
)
async def test_update_ticket_each_single_billing_field_replaces_only_itself(server, mock_gorelo, param, value, expected):
    updated(mock_gorelo, detail=billing_detail(service_line=11, role=7, work_type=33, status=1))
    await call_tool(server, "update_ticket", {"ticket_id": T1, param: value})
    assert mock_gorelo.calls("PATCH")[0].json == {"BillingOverride": expected}


async def test_update_ticket_two_billing_fields_given_keep_the_two_existing_ones(server, mock_gorelo):
    updated(mock_gorelo, detail=billing_detail(service_line=11, role=7, work_type=33, status=1))
    await call_tool(server, "update_ticket", {"ticket_id": T1, "billing_work_type_id": 44, "billable_status_id": 3})
    assert mock_gorelo.calls("PATCH")[0].json == {
        "BillingOverride": {"ServiceLineId": 11, "BillingRoleId": 7, "WorkTypeId": 44, "BillableStatusId": 3}
    }


async def test_update_ticket_billing_fills_only_the_parts_that_are_set(server, mock_gorelo):
    updated(mock_gorelo, detail=billing_detail(role=7))  # service line, work type and status are not set
    await call_tool(server, "update_ticket", {"ticket_id": T1, "billing_service_line_id": 11})
    assert mock_gorelo.calls("PATCH")[0].json == {"BillingOverride": {"ServiceLineId": 11, "BillingRoleId": 7}}


# keeps all of these as "nothing is set": a null override, {}, null parts, an Id that is null or 0
# (and a mix of them). Every other shape is refused: see BILLING_READ_FAILURES.
NO_OVERRIDE_YET = [
    pytest.param(billing_detail(), id="every-id-0"),
    pytest.param(ticket_detail(1, BillingOverride={k: {"Id": None, "Name": None} for k in tickets.BILLING_PARTS.values()}), id="every-id-null"),
    pytest.param(ticket_detail(1, BillingOverride={k: None for k in tickets.BILLING_PARTS.values()}), id="parts-null"),
    pytest.param(ticket_detail(1, BillingOverride={}), id="override-empty"),
    pytest.param(ticket_detail(1, BillingOverride=None), id="override-null"),
    pytest.param(
        ticket_detail(
            1,
            BillingOverride={
                "ServiceLine": None,
                "BillingRole": {"Id": None, "Name": None},
                "WorkType": {"Id": 0, "Name": ""},
                "BillableStatus": {"Id": 0},
            },
        ),
        id="null-part-null-id-and-zero-id-mixed",
    ),
]


@pytest.mark.parametrize("detail", NO_OVERRIDE_YET)
async def test_update_ticket_billing_on_a_ticket_with_no_existing_override_sends_only_the_given_field(server, mock_gorelo, detail):
    updated(mock_gorelo, detail=detail)
    await call_tool(server, "update_ticket", {"ticket_id": T1, "billable_status_id": 2})
    assert [r.method for r in mock_gorelo.requests] == ["GET", "PATCH", "GET"]
    assert mock_gorelo.calls("PATCH")[0].json == {"BillingOverride": {"BillableStatusId": 2}}


async def test_update_ticket_billing_next_to_other_changes_completes_the_override_and_keeps_the_rest(server, mock_gorelo):
    updated(mock_gorelo, detail=billing_detail(service_line=11, role=7, work_type=33, status=1))
    await call_tool(server, "update_ticket", {"ticket_id": T1, "status_id": 3, "title": "x", "billing_role_id": 22})
    assert mock_gorelo.calls("PATCH")[0].json == {
        "StatusId": 3,
        "Title": "x",
        "BillingOverride": {"ServiceLineId": 11, "BillingRoleId": 22, "WorkTypeId": 33, "BillableStatusId": 1},
    }


async def test_update_ticket_all_four_billing_fields_need_no_read_first(server, mock_gorelo):
    updated(mock_gorelo, detail=billing_detail(service_line=11, role=7, work_type=33, status=1))
    await call_tool(
        server, "update_ticket",
        {"ticket_id": T1, "billing_service_line_id": 1, "billing_role_id": 2, "billing_work_type_id": 3, "billable_status_id": 1},
    )
    assert [r.method for r in mock_gorelo.requests] == ["PATCH", "GET"]  # nothing to fill: no read before the write
    assert mock_gorelo.calls("PATCH")[0].json == {
        "BillingOverride": {"ServiceLineId": 1, "BillingRoleId": 2, "WorkTypeId": 3, "BillableStatusId": 1}
    }


async def test_update_ticket_without_billing_fields_makes_no_read_before_the_write(server, mock_gorelo):
    updated(mock_gorelo)
    await call_tool(server, "update_ticket", {"ticket_id": T1, "title": "x"})
    assert [r.method for r in mock_gorelo.requests] == ["PATCH", "GET"]


async def test_update_ticket_moving_the_client_does_not_carry_the_old_clients_override_over(server, mock_gorelo):
    # the move resets the override, so the old client's service line must not be sent for the new client
    updated(mock_gorelo, detail=billing_detail(service_line=11, role=7, work_type=33, status=1))
    await call_tool(server, "update_ticket", {"ticket_id": T1, "client_id": 9101, "billing_role_id": 22})
    assert [r.method for r in mock_gorelo.requests] == ["PATCH", "GET"]
    assert mock_gorelo.calls("PATCH")[0].json == {"ClientId": 9101, "BillingOverride": {"BillingRoleId": 22}}


async def test_update_ticket_local_errors_come_before_the_billing_read(server, mock_gorelo):
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, "billing_role_id": 22, "title": ""})
    assert text == "title: must not be empty or whitespace only"
    assert mock_gorelo.requests == []


BILLING_READ_FAILURES = [
    pytest.param(
        error_envelope(404, [("070401", "Ticket not found")], trace_id="00-b-1"),
        "Gorelo rejected update_ticket (HTTP 404, code 070401): Ticket not found [trace 00-b-1]",
        id="404",
    ),
    pytest.param(
        error_envelope(500, [("070001", "Internal error")], trace_id="00-b-2"),
        "Gorelo rejected update_ticket (HTTP 500, code 070001): Internal error [trace 00-b-2]",
        id="500",
    ),
    pytest.param(
        httpx.ReadTimeout("slow"),
        "Gorelo did not answer update_ticket (the request timed out). This was a read, so retrying is safe.",
        id="timeout",
    ),
    pytest.param(
        httpx.ConnectError("refused"),
        "Gorelo did not answer update_ticket (the connection failed). This was a read, so retrying is safe.",
        id="connection",
    ),
    pytest.param(
        envelope(None),
        "Gorelo returned an unexpected response for update_ticket: GET /v1/tickets/{ticketId}: Gorelo reported success but Data is null",
        id="data-null",
    ),
    pytest.param(
        envelope([ticket_detail(1)]),
        "GET /v1/tickets/{ticketId}: expected Data to be a non-empty object but got a list of 1 item; refusing to guess",
        id="data-is-a-list",
    ),
    pytest.param(
        envelope({}),
        "GET /v1/tickets/{ticketId}: expected Data to be a non-empty object but got an empty object; refusing to guess",
        id="data-is-empty",
    ),
    pytest.param(
        envelope(ticket_detail(1, BillingOverride="none")),
        "GET /v1/tickets/{ticketId}: BillingOverride is a string, not an object, so the billing fields you did not give "
        "cannot be kept; refusing to guess",
        id="override-not-an-object",
    ),
    pytest.param(
        envelope(ticket_detail(1, BillingOverride={"ServiceLine": {"Id": "7", "Name": "x"}})),
        "GET /v1/tickets/{ticketId}: BillingOverride.ServiceLine.Id is a string, not an id",
        id="part-id-is-text",
    ),
    pytest.param(
        envelope(ticket_detail(1, BillingOverride={"WorkType": {"Id": -3, "Name": "x"}})),
        "GET /v1/tickets/{ticketId}: BillingOverride.WorkType.Id is negative, not an id",
        id="part-id-is-negative",
    ),
    pytest.param(
        envelope(ticket_detail(1, BillingOverride={"BillableStatus": {"Id": True, "Name": "x"}})),
        "GET /v1/tickets/{ticketId}: BillingOverride.BillableStatus.Id is a boolean, not an id",
        id="part-id-is-boolean",
    ),
    # a record that does not show where billing lives is refused, never read as "nothing is set"
    pytest.param(
        envelope({key: value for key, value in ticket_detail(1).items() if key != "BillingOverride"}),
        "GET /v1/tickets/{ticketId}: the ticket has no BillingOverride field, so the billing fields you did not give "
        "cannot be kept; refusing to guess",
        id="record-has-no-billing-override-key",
    ),
    pytest.param(
        envelope(ticket_detail(1, BillingOverride={"Contract": {"Id": 3, "Name": "x"}, "Note": "renamed"})),
        "GET /v1/tickets/{ticketId}: BillingOverride has none of ServiceLine, BillingRole, WorkType, BillableStatus "
        "(Gorelo may have renamed them), so the billing fields you did not give cannot be kept; refusing to guess",
        id="override-carries-none-of-the-four-parts",
    ),
    pytest.param(
        envelope(ticket_detail(1, BillingOverride={"ServiceLine": 11})),
        "GET /v1/tickets/{ticketId}: BillingOverride.ServiceLine is a number, not an object with an Id, so the "
        "billing fields you did not give cannot be kept; refusing to guess",
        id="part-is-a-bare-number",
    ),
    pytest.param(
        envelope(ticket_detail(1, BillingOverride={"BillingRole": "7"})),
        "GET /v1/tickets/{ticketId}: BillingOverride.BillingRole is a string, not an object with an Id",
        id="part-is-text",
    ),
    pytest.param(
        envelope(ticket_detail(1, BillingOverride={"WorkType": [{"Id": 3}]})),
        "GET /v1/tickets/{ticketId}: BillingOverride.WorkType is a list of 1 item, not an object with an Id",
        id="part-is-a-list",
    ),
    pytest.param(
        envelope(ticket_detail(1, BillingOverride={"WorkType": {"Name": "Remote"}})),
        "GET /v1/tickets/{ticketId}: BillingOverride.WorkType has no Id, so the billing fields you did not give "
        "cannot be kept; refusing to guess",
        id="part-has-no-id-key",
    ),
    pytest.param(
        envelope(ticket_detail(1, BillingOverride={"BillableStatus": {}})),
        "GET /v1/tickets/{ticketId}: BillingOverride.BillableStatus has no Id",
        id="part-is-an-empty-object",
    ),
    pytest.param(
        envelope(ticket_detail(1, BillingOverride={"ServiceLine": {"Id": 0, "Name": ""}, "BillingRole": 7})),
        "GET /v1/tickets/{ticketId}: BillingOverride.BillingRole is a number, not an object with an Id",
        id="one-good-part-does-not-excuse-a-bad-one",
    ),
]


@pytest.mark.parametrize("response, fragment", BILLING_READ_FAILURES)
async def test_update_ticket_a_billing_read_that_fails_or_cannot_be_used_sends_no_patch(server, mock_gorelo, response, fragment):
    mock_gorelo.on("PATCH", DETAIL_PATH, envelope({"Id": T1}))  # registered, and must never be called
    mock_gorelo.on("GET", DETAIL_PATH, response)
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, "billing_role_id": 22})
    assert fragment in text
    assert "may or may not have been applied" not in text and "the write may have been applied" not in text
    assert mock_gorelo.calls("PATCH") == []
    assert [r.method for r in mock_gorelo.requests] == ["GET"]  # the read is not retried either


async def test_update_ticket_a_billing_read_that_works_but_a_write_that_fails_is_reported_as_the_write(server, mock_gorelo):
    mock_gorelo.on("GET", DETAIL_PATH, envelope(billing_detail(service_line=11)))
    mock_gorelo.on(
        "PATCH", DETAIL_PATH,
        error_envelope(400, [("070101", "is not valid", "BillingOverride.BillingRoleId")], trace_id="00-b-3"),
    )
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, "billing_role_id": 22})
    assert text == "Gorelo rejected update_ticket (HTTP 400, code 070101): billing_role_id: is not valid [trace 00-b-3]"
    assert mock_gorelo.last.json == {"BillingOverride": {"ServiceLineId": 11, "BillingRoleId": 22}}


async def test_update_ticket_keeps_priority_zero(server, mock_gorelo):
    updated(mock_gorelo)
    await call_tool(server, "update_ticket", {"ticket_id": T1, "priority_id": 0})
    assert mock_gorelo.calls("PATCH")[0].json == {"PriorityId": 0}


@pytest.mark.parametrize("given", [T1.upper(), T1.replace("-", ""), T1.replace("-", "").upper()])
async def test_update_ticket_sends_the_canonical_guid_in_the_path_and_the_reread(server, mock_gorelo, given):
    updated(mock_gorelo)
    await call_tool(server, "update_ticket", {"ticket_id": given, "title": "x"})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("PATCH", f"{LIST}/{T1}"), ("GET", f"{LIST}/{T1}")]


@pytest.mark.parametrize("given", [f"  {T1}  ", f"{T1}\n", f" {T1}"])
async def test_update_ticket_does_not_trim_a_padded_guid(server, mock_gorelo, given):
    text = await call_tool_error(server, "update_ticket", {"ticket_id": given, "title": "x"})
    assert text.startswith(f"ticket_id: {NOT_A_GUID}; for a ticket number or display number")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "clear, body",
    [
        (["tag_ids"], {"TagIds": []}),
        (["cc_contact_ids"], {"CcContactIds": []}),
        (["assisting_assignee_ids"], {"AssistingAssigneeIds": []}),
        (["watcher_ids"], {"WatcherIds": []}),
        (["agent_asset_ids"], {"AgentAssetIds": []}),
        (["custom_asset_ids"], {"CustomAssetIds": []}),
        (["uptime_ids"], {"UptimeIds": []}),
        (["tag_ids", "watcher_ids"], {"TagIds": [], "WatcherIds": []}),
        (["tag_ids", "tag_ids"], {"TagIds": []}),
        (
            list(tickets.CLEARABLE_FIELDS),
            {"CcContactIds": [], "AssistingAssigneeIds": [], "WatcherIds": [], "TagIds": [], "AgentAssetIds": [], "CustomAssetIds": [], "UptimeIds": []},
        ),
    ],
)
async def test_update_ticket_clear_fields_sends_an_empty_list_for_each_named_list(server, mock_gorelo, clear, body):
    updated(mock_gorelo)
    await call_tool(server, "update_ticket", {"ticket_id": T1, "clear_fields": clear})
    assert mock_gorelo.calls("PATCH")[0].json == body


async def test_update_ticket_clear_fields_works_next_to_other_changes(server, mock_gorelo):
    updated(mock_gorelo)
    await call_tool(
        server, "update_ticket",
        {"ticket_id": T1, "title": "Cleaned", "clear_fields": ["watcher_ids"], "cc_contact_ids": [9103]},
    )
    assert mock_gorelo.calls("PATCH")[0].json == {"Title": "Cleaned", "WatcherIds": [], "CcContactIds": [9103]}


async def test_update_ticket_an_empty_list_together_with_its_clear_field_just_clears(server, mock_gorelo):
    updated(mock_gorelo)
    await call_tool(server, "update_ticket", {"ticket_id": T1, "tag_ids": [], "clear_fields": ["tag_ids"]})
    assert mock_gorelo.calls("PATCH")[0].json == {"TagIds": []}


async def test_update_ticket_a_value_and_a_clear_for_the_same_field_is_an_error(server, mock_gorelo):
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, "tag_ids": [5], "clear_fields": ["tag_ids"]})
    assert text == "tag_ids: cannot be given a value and cleared in the same call"
    assert mock_gorelo.requests == []


CANNOT_BE_CLEARED = ["group_ids", "lead_assignee_id", "contact_id", "title", "billing_role_id", "status_id", "nope", ""]


@pytest.mark.parametrize("name", CANNOT_BE_CLEARED)
async def test_update_ticket_clear_fields_refuses_anything_that_cannot_be_cleared(server, mock_gorelo, name):
    # clear_fields is a list of Literal names (like update_item's), so the schema refuses any other name
    # before the tool runs, and the error lists the names that are accepted
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, "clear_fields": ["tag_ids", name]})
    assert "clear_fields.1" in text and "Input should be" in text
    for allowed in tickets.CLEARABLE_FIELDS:
        assert f"'{allowed}'" in text, allowed
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("name", CANNOT_BE_CLEARED)
async def test_update_ticket_called_directly_still_refuses_a_name_that_cannot_be_cleared(client_factory, mock_gorelo, name):
    # defence in depth for a caller that skips the schema (a test, another helper): the tool checks the names too
    async with client_factory() as client:
        with pytest.raises(ToolError) as caught:
            await tickets.update_ticket(make_ctx(client), ticket_id=T1, clear_fields=["tag_ids", name])
    text = str(caught.value)
    assert text.startswith("clear_fields[1]: not a field that can be cleared; allowed: cc_contact_ids, assisting_assignee_ids, watcher_ids, tag_ids, agent_asset_ids, custom_asset_ids, uptime_ids.")
    assert "null counts as absent" in text and "group_ids must keep at least one group" in text
    assert not name or name not in text.split("allowed:")[0]  # the offending text is never echoed
    assert mock_gorelo.requests == []


async def test_the_clear_fields_schema_offers_exactly_the_clearable_list_params(server):
    tool = next(t for t in await list_tools(server) if t.name == "update_ticket")
    clear = tool.inputSchema["properties"]["clear_fields"]
    assert clear["type"] == "array" and clear["items"]["type"] == "string"
    assert clear["items"]["enum"] == list(tickets.CLEARABLE_FIELDS) == list(typing.get_args(tickets.ClearableField))
    assert "group_ids" not in clear["items"]["enum"]
    assert set(clear["items"]["enum"]) <= set(tickets.UPDATE_FIELDS)  # every offered name is a real parameter


async def test_update_ticket_an_empty_clear_fields_list_is_an_error(server, mock_gorelo):
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, "clear_fields": []})
    assert text == "clear_fields: must not be an empty list; omit it to leave the field unchanged"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("param", list(tickets.CLEARABLE_FIELDS))
async def test_update_ticket_an_empty_list_is_refused_and_points_at_clear_fields(server, mock_gorelo, param):
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, param: []})
    assert text == (
        f"{param}: an empty list is not accepted here; to remove every entry pass clear_fields=['{param}'], "
        f"or omit {param} to leave it unchanged"
    )
    assert mock_gorelo.requests == []


async def test_update_ticket_an_empty_group_list_is_refused_without_a_clear_option(server, mock_gorelo):
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, "group_ids": []})
    assert text == f"group_ids: {NO_IDS} (omit group_ids if you have no ids to give)"
    assert "clear_fields" not in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({}, id="nothing"),
        pytest.param({"updated_on": "2020-01-01T00:00:00Z"}, id="only-updated-on"),
        pytest.param({"updated_by_name": "Import bot"}, id="only-updated-by-name"),
        pytest.param({"updated_on": "2020-01-01T00:00:00Z", "updated_by_name": "Import bot"}, id="only-the-two-stamps"),
    ],
)
async def test_update_ticket_with_nothing_to_change_is_a_local_error_and_sends_nothing(server, mock_gorelo, extra):
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, **extra})
    assert text.startswith("update_ticket: nothing to change. Give at least one field to change besides ticket_id")
    assert "Gorelo rejects an empty PATCH" in text and "title, status_id, priority_id" in text and text.endswith("or clear_fields")
    listed = text.split("same call): ")[1]  # the fields to choose from do not include the two stamps
    assert "updated_on" not in listed and "updated_by_name" not in listed and "billable_status_id" in listed
    assert mock_gorelo.requests == []


async def test_update_ticket_the_two_stamp_fields_are_fine_next_to_a_real_change(server, mock_gorelo):
    updated(mock_gorelo)
    await call_tool(server, "update_ticket", {"ticket_id": T1, "status_id": 2, "updated_on": "2020-01-01T00:00:00Z", "updated_by_name": "Bot"})
    assert mock_gorelo.calls("PATCH")[0].json == {"StatusId": 2, "UpdatedOn": "2020-01-01T00:00:00Z", "UpdatedByName": "Bot"}


@pytest.mark.parametrize("given", ["1234", "TCK-1234", "", "   ", "not-a-guid", "3f2b8c1e-0d4a-4b7e-9a51", f"{T1}/comments", f"{T1}%2F.."])
async def test_update_ticket_ticket_id_must_be_a_guid_and_the_error_points_to_get_ticket(server, mock_gorelo, given):
    text = await call_tool_error(server, "update_ticket", {"ticket_id": given, "title": "x"})
    assert text.startswith("ticket_id: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got ")
    assert "call get_ticket first and pass its Id" in text
    assert mock_gorelo.requests == []


async def test_update_ticket_checks_ticket_id_before_anything_else(server, mock_gorelo):
    text = await call_tool_error(server, "update_ticket", {"ticket_id": "1234"})
    assert text.startswith("ticket_id: expected a GUID")
    assert mock_gorelo.requests == []


UPDATE_VALIDATION = [
    pytest.param({"title": ""}, "title: must not be empty or whitespace only", id="blank-title"),
    pytest.param({"title": "t" * 251}, "title: at most 250 characters, got 251", id="long-title"),
    pytest.param({"status_id": 0}, f"status_id: {NOT_POSITIVE}", id="status-zero"),
    pytest.param({"type_id": -2}, f"type_id: {NOT_POSITIVE}", id="type-negative"),
    pytest.param({"client_id": 0}, f"client_id: {NOT_POSITIVE}", id="client-zero"),
    pytest.param({"location_id": 0}, f"location_id: {NOT_POSITIVE}", id="location-zero"),
    pytest.param({"contact_id": 0}, f"contact_id: {NOT_POSITIVE}", id="contact-zero"),
    pytest.param({"lead_assignee_id": 0}, f"lead_assignee_id: {NOT_POSITIVE}", id="unassign-with-zero-is-refused"),
    pytest.param({"priority_id": 5}, "priority_id: must be one of 0 None, 1 Urgent, 2 High, 3 Normal, 4 Low", id="priority-5"),
    pytest.param({"priority_id": -1}, "priority_id: must be one of 0 None", id="priority-negative"),
    pytest.param({"billable_status_id": 0}, "billable_status_id: must be one of 1 Billable, 2 No charge, 3 Non-billable", id="billable-0"),
    pytest.param({"billable_status_id": 4}, "billable_status_id: must be one of 1 Billable, 2 No charge, 3 Non-billable", id="billable-4"),
    pytest.param({"billing_service_line_id": 0}, f"billing_service_line_id: {NOT_POSITIVE}", id="service-line-zero"),
    pytest.param({"billing_role_id": 0}, f"billing_role_id: {NOT_POSITIVE}", id="role-zero"),
    pytest.param({"billing_work_type_id": 0}, f"billing_work_type_id: {NOT_POSITIVE}", id="work-type-zero"),
    pytest.param({"cc_contact_ids": [0]}, f"cc_contact_ids[0]: {NOT_POSITIVE}", id="cc-zero"),
    pytest.param({"group_ids": [7201, 0]}, f"group_ids[1]: {NOT_POSITIVE}", id="group-zero"),
    pytest.param({"watcher_ids": [-4]}, f"watcher_ids[0]: {NOT_POSITIVE}", id="watcher-negative"),
    pytest.param({"agent_asset_ids": ["nope"]}, f"agent_asset_ids[0]: {NOT_A_GUID}", id="bad-agent-uuid"),
    pytest.param({"custom_asset_ids": [uid(1), "12345"]}, f"custom_asset_ids[1]: {NOT_A_GUID}", id="bad-custom-uuid"),
    pytest.param({"uptime_ids": ["   "]}, f"uptime_ids[0]: {NOT_A_GUID}", id="blank-uptime-uuid"),
    pytest.param({"closed_on": "2026-09-30T12:00:00"}, "closed_on: '2026-09-30T12:00:00' has no UTC offset", id="naive-closed-on"),
    pytest.param({"closed_on": "2099-01-01T00:00:00Z"}, "closed_on: 2099-01-01T00:00:00Z is in the future", id="future-closed-on"),
    pytest.param({"updated_on": "2099-01-01T00:00:00Z", "title": "x"}, "updated_on: 2099-01-01T00:00:00Z is in the future", id="future-updated-on"),
    pytest.param({"updated_on": "yesterday", "title": "x"}, "updated_on: 'yesterday' is not an ISO 8601 datetime", id="bad-updated-on"),
    pytest.param({"updated_by_name": "", "title": "x"}, "updated_by_name: must not be empty or whitespace only", id="blank-updated-by-name"),
]


@pytest.mark.parametrize("override, fragment", UPDATE_VALIDATION)
async def test_update_ticket_local_validation_errors_name_the_parameter_and_send_nothing(server, mock_gorelo, override, fragment):
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, **override})
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_update_ticket_rejects_values_of_the_wrong_kind_before_any_http_call(server, mock_gorelo):
    for override, param in (({"status_id": "closed"}, "status_id"), ({"clear_fields": "tag_ids"}, "clear_fields"), ({"tag_ids": "5"}, "tag_ids"), ({"billable_status_id": "yes"}, "billable_status_id")):
        text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, **override})
        assert param in text
    assert mock_gorelo.requests == []


async def test_update_ticket_cannot_unassign_or_clear_with_null(server, mock_gorelo):
    # null is "not given" for the model client too: it never reaches the body, so there is nothing to send
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, "lead_assignee_id": None})
    assert text.startswith("update_ticket: nothing to change")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "response, reason",
    [
        pytest.param(error_envelope(404, [("070401", "Ticket not found")], trace_id="00-t-1"),
                     f"{REREAD} answered HTTP 404 (code 070401): Ticket not found [trace 00-t-1]", id="404"),
        pytest.param(error_envelope(500, [("070001", "Internal error")], trace_id="00-t-2"),
                     f"{REREAD} answered HTTP 500 (code 070001): Internal error [trace 00-t-2]", id="500"),
        pytest.param(httpx.ReadTimeout("slow"), f"{REREAD} timed out", id="timeout"),
        pytest.param(httpx.Response(502, text="<html>Bad Gateway</html>"), f"{REREAD}: unexpected response shape; refusing to guess (HTTP 502", id="gateway-page"),
        pytest.param(envelope(True), f"{REREAD} returned a boolean instead of the record", id="data-is-a-boolean"),
    ],
)
async def test_update_ticket_a_failed_reread_returns_the_id_and_a_warning_and_never_repeats_the_write(
    server, mock_gorelo, response, reason
):
    mock_gorelo.on("PATCH", f"{LIST}/{T1}", envelope({"Id": T1}))
    mock_gorelo.on("GET", f"{LIST}/{T1}", response)
    result = await call_tool(server, "update_ticket", {"ticket_id": T1, "title": "x"})
    assert set(result) == {"Id", "warning"} and result["Id"] == T1
    assert result["warning"].startswith(f"the write succeeded; re-reading it failed: {reason}")
    assert result["warning"].endswith("Do not repeat the write; read it again later.")
    assert "update_ticket" not in result["warning"] and "Gorelo rejected" not in result["warning"]
    assert len(mock_gorelo.calls("PATCH")) == 1 and len(mock_gorelo.calls("GET")) == 1


async def test_update_ticket_the_id_in_a_reread_warning_is_the_canonical_guid(server, mock_gorelo):
    mock_gorelo.on("PATCH", f"{LIST}/{T1}", envelope({"Id": T1}))
    mock_gorelo.on("GET", f"{LIST}/{T1}", error_envelope(404, [("070401", "Ticket not found")]))
    for given in (T1.upper(), T1.replace("-", "").upper(), T1.replace("-", "")):
        result = await call_tool(server, "update_ticket", {"ticket_id": given, "title": "x"})
        assert result["Id"] == T1 and "warning" in result


@pytest.mark.parametrize("data", [{"Id": "something-else"}, {"Id": T1}, {"Id": None}, {"Other": 1}])
async def test_update_ticket_rereads_the_ticket_it_updated_whatever_object_the_patch_answered(server, mock_gorelo, data):
    mock_gorelo.on("PATCH", f"{LIST}/{T1}", envelope(data))
    mock_gorelo.on("GET", f"{LIST}/{T1}", envelope(ticket_detail(1)))
    assert (await call_tool(server, "update_ticket", {"ticket_id": T1, "title": "x"})) == ticket_detail(1)
    assert [r.method for r in mock_gorelo.requests] == ["PATCH", "GET"]


UPDATE_BAD_ANSWERS = [
    pytest.param(None, "null", id="null"),
    pytest.param({}, "an empty object", id="empty-object"),
    pytest.param(False, "a boolean", id="false"),
    pytest.param(True, "a boolean", id="true"),
    pytest.param([], "an empty list", id="empty-list"),
    pytest.param([{"Id": T1}], "a list of 1 item", id="list-with-an-object"),
    pytest.param("done", "a string", id="string"),
    pytest.param(0, "a number", id="number"),
]


@pytest.mark.parametrize("data, what", UPDATE_BAD_ANSWERS)
async def test_update_ticket_an_answer_that_is_not_an_object_is_a_shape_error_never_a_success(server, mock_gorelo, data, what):
    mock_gorelo.on("PATCH", f"{LIST}/{T1}", envelope(data))
    mock_gorelo.on("GET", f"{LIST}/{T1}", envelope(ticket_detail(1)))
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, "title": "x"})
    assert text.startswith(
        "Gorelo returned an unexpected response for update_ticket: PATCH /v1/tickets/{ticketId}: "
        f"expected Data to be a non-empty object but got {what}; the write may have been applied, so verify it with a "
        "read before repeating it"
    )
    assert [r.method for r in mock_gorelo.requests] == ["PATCH"]  # not retried, not re-read


@pytest.mark.parametrize(
    "prop, param",
    [
        ("Title", "title"), ("StatusId", "status_id"), ("PriorityId", "priority_id"), ("TypeId", "type_id"),
        ("ClientId", "client_id"), ("ContactId", "contact_id"), ("GroupIds", "group_ids"), ("LeadAssigneeId", "lead_assignee_id"),
        ("TagIds", "tag_ids"), ("ClosedOn", "closed_on"), ("UpdatedOn", "updated_on"), ("UpdatedByName", "updated_by_name"),
        ("BillingOverride.ServiceLineId", "billing_service_line_id"),
        ("BillingOverride.BillingRoleId", "billing_role_id"),
        ("BillingOverride.WorkTypeId", "billing_work_type_id"),
        ("BillingOverride.BillableStatusId", "billable_status_id"),
        ("ServiceLineId", "billing_service_line_id"),
        ("BillableStatusId", "billable_status_id"),
        ("WorkTypeId", "billing_work_type_id"),
        ("BillingRoleId", "billing_role_id"),
    ],
)
async def test_update_ticket_maps_a_gorelo_error_to_the_snake_case_parameter(server, mock_gorelo, prop, param):
    mock_gorelo.on("PATCH", f"{LIST}/{T1}", error_envelope(400, [("070101", "is not valid", prop)], trace_id="00-u-1"))
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, "title": "x"})
    assert text == f"Gorelo rejected update_ticket (HTTP 400, code 070101): {param}: is not valid [trace 00-u-1]"
    assert len(mock_gorelo.requests) == 1


async def test_update_ticket_shows_the_technician_conflict_gorelo_answers_when_lead_and_watcher_share_a_patch(server, mock_gorelo):
    # live, 2026-10-02: lead_assignee_id 9201 with watcher_ids [9201] in one PATCH is a 400 "Technician already exists"
    mock_gorelo.on(
        "PATCH", f"{LIST}/{T1}", error_envelope(400, [("070101", "Technician already exists", "WatcherIds")], trace_id="00-u-2")
    )
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, "lead_assignee_id": 9201, "watcher_ids": [9201]})
    assert text == "Gorelo rejected update_ticket (HTTP 400, code 070101): watcher_ids: Technician already exists [trace 00-u-2]"
    assert mock_gorelo.last.json == {"LeadAssigneeId": 9201, "WatcherIds": [9201]}  # sent as asked: Gorelo decides
    assert [r.method for r in mock_gorelo.requests] == ["PATCH"]  # nothing re-read, nothing repeated


async def test_update_ticket_moves_a_user_from_watcher_to_lead_in_two_calls(server, mock_gorelo):
    # what the write matrix does: set the watcher, clear it with clear_fields, and only then set the lead
    updated(mock_gorelo)
    await call_tool(server, "update_ticket", {"ticket_id": T1, "watcher_ids": [9201]})
    await call_tool(server, "update_ticket", {"ticket_id": T1, "clear_fields": ["watcher_ids"]})
    await call_tool(server, "update_ticket", {"ticket_id": T1, "lead_assignee_id": 9201})
    assert [r.json for r in mock_gorelo.calls("PATCH")] == [
        {"WatcherIds": [9201]}, {"WatcherIds": []}, {"LeadAssigneeId": 9201},
    ]


async def test_update_ticket_the_old_billing_field_name_would_be_unreadable_to_gorelo_so_it_is_never_sent(server, mock_gorelo):
    mock_gorelo.on("GET", f"{LIST}/{T1}", envelope(ticket_detail(1)))  # the partial override is read first
    mock_gorelo.on("PATCH", f"{LIST}/{T1}", error_envelope(400, [("070201", "Invalid or malformed request body.", None)]))
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, "billing_service_line_id": 11})
    assert "Invalid or malformed request body." in text
    assert "ContractServiceId" not in json.dumps(mock_gorelo.last.json)


async def test_update_ticket_a_missing_ticket_is_a_gorelo_404(server, mock_gorelo):
    mock_gorelo.on("PATCH", f"{LIST}/{T3}", error_envelope(404, [("070401", "Ticket not found")], trace_id="00-u-404"))
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T3, "title": "x"})
    assert text == "Gorelo rejected update_ticket (HTTP 404, code 070401): Ticket not found [trace 00-u-404]"
    assert [r.method for r in mock_gorelo.requests] == ["PATCH"]


async def test_update_ticket_the_null_only_rejection_probe_is_reported_as_gorelo_said_it(server, mock_gorelo):
    mock_gorelo.on("PATCH", f"{LIST}/{T1}", error_envelope(400, [("070101", "The request must contain at least one field to update.")]))
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, "title": "x"})
    assert "The request must contain at least one field to update." in text


async def test_update_ticket_a_timeout_is_not_confirmed_never_retried_and_never_reread(server, mock_gorelo):
    mock_gorelo.on("PATCH", f"{LIST}/{T1}", httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, "title": "x"})
    assert text.startswith("Gorelo did not confirm update_ticket (the request timed out). The change may or may not have been applied.")
    assert "Verify with a read before retrying" in text
    assert [r.method for r in mock_gorelo.requests] == ["PATCH"]


async def test_update_ticket_a_conflict_is_shown_with_its_message(server, mock_gorelo):
    mock_gorelo.on("PATCH", f"{LIST}/{T1}", error_envelope(409, [("070901", "The ticket must be in a closed status to set ClosedOn.", "ClosedOn")]))
    text = await call_tool_error(server, "update_ticket", {"ticket_id": T1, "closed_on": "2020-01-01T00:00:00Z"})
    assert "closed_on: The ticket must be in a closed status to set ClosedOn." in text
    assert len(mock_gorelo.requests) == 1


# --------------------------------------------------------------------------
# Cross-cutting
# --------------------------------------------------------------------------


async def test_an_explicit_null_means_not_given_for_every_optional_parameter(server, mock_gorelo):
    """Model clients often send null for what they leave out: it must behave exactly like omission."""
    mock_gorelo.on("GET", LIST, paged_envelope([ticket_row(1)]))
    mock_gorelo.on("POST", LIST, envelope({"Id": uid(5)}))
    mock_gorelo.on("GET", f"{LIST}/{uid(5)}", envelope(ticket_detail(5)))
    mock_gorelo.on("PATCH", f"{LIST}/{T1}", envelope({"Id": T1}))
    mock_gorelo.on("GET", f"{LIST}/{T1}", envelope(ticket_detail(1)))
    listing = {name: None for name in EXPECTED_PARAMS["list_tickets"] if name != "page_size"}
    await call_tool(server, "list_tickets", listing)
    assert mock_gorelo.last.query == {"PageSize": "50"}
    searching = {name: None for name in ("client_id", "status", "priority", "type", "query", "updated_since", "created_since", "awaiting_client")}
    await call_tool(server, "search_tickets", searching)
    assert mock_gorelo.last.query == {"PageSize": "200"}
    optional = [p for p in EXPECTED_PARAMS["create_ticket"] if p not in CREATE_MIN and p != "send_created_email"]
    await call_tool(server, "create_ticket", {**CREATE_MIN, **{name: None for name in optional}})
    assert mock_gorelo.calls("POST", LIST)[0].json == CREATE_MIN_BODY
    changes = {name: None for name in EXPECTED_PARAMS["update_ticket"] if name not in ("ticket_id", "title")}
    await call_tool(server, "update_ticket", {"ticket_id": T1, "title": "x", **changes})
    assert mock_gorelo.calls("PATCH")[0].json == {"Title": "x"}


async def test_a_success_status_with_is_success_false_is_still_an_error_for_a_read_and_for_a_write(server, mock_gorelo):
    failed = {
        "StatusCode": 200, "IsSuccess": False, "Data": None, "DataContext": {"TraceId": "00-f-1"},
        "Notifications": [{"Code": "070101", "Message": "Title is required.", "PropertyName": "Title", "ActionHint": None, "DocUrl": None}],
    }
    mock_gorelo.on("POST", LIST, failed)
    mock_gorelo.on("GET", LIST, failed)
    text = await call_tool_error(server, "create_ticket", CREATE_MIN)
    assert text == "Gorelo rejected create_ticket (HTTP 200, code 070101): title: Title is required. [trace 00-f-1]"
    text = await call_tool_error(server, "list_tickets")
    assert text.startswith("Gorelo rejected list_tickets (HTTP 200, code 070101): Title: Title is required.")
    assert [r.method for r in mock_gorelo.requests] == ["POST", "GET"]  # no re-read after the failed write


async def test_a_rate_limited_read_is_retried_by_the_client_and_still_returns_the_page(server, mock_gorelo):
    limited = httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "Rate limit exceeded", "retry_after": "0s"})
    mock_gorelo.on("GET", LIST, in_order(limited, paged_envelope([ticket_row(1)])))
    result = await call_tool(server, "list_tickets")
    assert result["count"] == 1 and len(mock_gorelo.calls("GET", LIST)) == 2


def tickets_op_key(path):
    """The op path (without the method) a recorded request path belongs to."""
    if path == LIST:
        return "/v1/tickets"
    if path in (STATUSES_PATH, TYPES_PATH, TAGS_PATH):
        return path
    return "/v1/tickets/{ticketId}"


async def test_every_tool_only_sends_operations_it_declared(server, mock_gorelo):
    # tools/_common.py refuses an undeclared op with a "spec" error; drive each network tool once and check
    # that none of those errors appears and that every request matches a declared op of its tool.
    lookups(mock_gorelo)
    mock_gorelo.on("GET", LIST, paged_envelope([ticket_row(2, Number=1234, DisplayNumber="TCK-1234")]))
    mock_gorelo.on("GET", TAGS_PATH, envelope(TAGS))
    mock_gorelo.on("GET", f"{LIST}/{T2}", envelope(ticket_detail(2)))
    mock_gorelo.on("POST", LIST, envelope({"Id": T2}))
    mock_gorelo.on("PATCH", f"{LIST}/{T2}", envelope({"Id": T2}))
    calls = {
        "list_tickets": {},
        "search_tickets": {"status": "New", "type": "Incident"},
        "get_ticket": {"ticket_id": "1234"},
        "create_ticket": CREATE_MIN,
        "update_ticket": {"ticket_id": T2, "title": "x"},
        "list_ticket_statuses": {},
        "list_ticket_types": {},
        "list_ticket_tags": {},
        "list_ticket_priorities": {},
        "list_ticket_sources": {},
    }
    specs = mine()
    for name, args in calls.items():
        before = len(mock_gorelo.requests)
        await call_tool(server, name, args)
        sent = {f"{r.method} {tickets_op_key(r.path)}" for r in mock_gorelo.requests[before:]}
        assert sent <= set(specs[name].ops), (name, sent)


def test_the_module_never_hardcodes_tenant_ids():
    """Status, type, group, user, tag, client and contact ids are tenant data."""
    tenant_ids = {7301, 7302, 7101, 7201, 9201, 9301, 9202, 9102, 9304, 9302, 9303, 9101, 9103}
    tree = ast.parse((REPO_ROOT / "tools" / "tickets.py").read_text(encoding="utf-8"))
    numbers = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and type(node.value) is int}
    assert not numbers & tenant_ids, numbers & tenant_ids
    names = {node.value.casefold() for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 40}
    assert not names & {"closed", "incident", "solved", "in progress", "everyone", "request", "maintenance"}


# --------------------------------------------------------------------------
# shared helpers, strict ids, neutral examples, concision
# --------------------------------------------------------------------------


def test_the_module_uses_the_shared_helpers_and_keeps_no_private_copies():
    for name in ("positive_id", "positive_ids", "guid", "guids", "created_id", "expect_object", "describe_value", "StrictId", "StrictBool"):
        assert getattr(tickets, name) is getattr(common, name), name
    for private in ("_positive_id", "_opt_id", "_filter_ids", "_priority_filter_ids", "_uuid_text", "_ticket_guid", "_shown", "_UUID_TEXT", "_write_list"):
        assert not hasattr(tickets, private), private


def lax_ints(annotation):
    """How many plain `int` (not Annotated[int, Strict()]) an annotation contains, however deep."""
    if typing.get_origin(annotation) is typing.Annotated:
        base, *metadata = typing.get_args(annotation)
        if base is int and any(isinstance(item, Strict) for item in metadata):
            return 0
        return lax_ints(base)
    if annotation is int:
        return 1
    return sum(lax_ints(argument) for argument in typing.get_args(annotation))


def test_every_integer_parameter_but_the_two_counts_is_strict_including_lists_and_unions():
    for name, spec in mine().items():
        for param, hint in typing.get_type_hints(spec.fn, include_extras=True).items():
            if param in ("return", "ctx", "page_size", "limit"):
                continue
            assert lax_ints(hint) == 0, (name, param)
    assert lax_ints(typing.get_type_hints(tickets.list_tickets, include_extras=True)["page_size"]) == 1  # the probe sees a plain int
    assert lax_ints(list[int] | None) == 1 and lax_ints(list[common.StrictId] | None) == 0


def has_list(annotation):
    if typing.get_origin(annotation) is typing.Annotated:
        return has_list(typing.get_args(annotation)[0])
    if typing.get_origin(annotation) is list:
        return True
    return any(has_list(argument) for argument in typing.get_args(annotation))


def test_clear_fields_covers_every_list_parameter_of_update_ticket_except_group_ids():
    # the clear_fields description says "any list param except group_ids": that must stay true
    hints = typing.get_type_hints(tickets.update_ticket, include_extras=True)
    list_params = {param for param, hint in hints.items() if param not in ("return", "ctx", "clear_fields") and has_list(hint)}
    assert list_params == set(tickets.CLEARABLE_FIELDS) | {"group_ids"}
    assert "group_ids" not in tickets.CLEARABLE_FIELDS


SCALAR_ID_PARAMS = {
    "search_tickets": ["client_id"],
    "create_ticket": ["client_id", "status_id", "type_id", "priority_id", "source_id", "group_id", "contact_id", "location_id", "lead_assignee_id"],
    "update_ticket": [
        "status_id", "priority_id", "type_id", "client_id", "location_id", "contact_id", "lead_assignee_id",
        "billing_service_line_id", "billing_role_id", "billing_work_type_id", "billable_status_id",
    ],
}
LIST_ID_PARAMS = {
    "list_tickets": ["status_ids", "client_ids", "priority_ids", "type_ids", "lead_assignee_ids", "contact_ids", "tag_ids", "group_ids"],
    "create_ticket": ["cc_contact_ids", "assisting_assignee_ids", "watcher_ids", "tag_ids"],
    "update_ticket": ["cc_contact_ids", "group_ids", "assisting_assignee_ids", "watcher_ids", "tag_ids"],
}
TEXT_OR_ID_PARAMS = {"search_tickets": ["status", "priority", "type"], "get_ticket": ["ticket_id"]}
BASE_ARGS = {
    "list_tickets": {}, "search_tickets": {}, "get_ticket": {}, "create_ticket": CREATE_MIN,
    "update_ticket": {"ticket_id": T1, "title": "x"},
}


@pytest.mark.parametrize(
    "tool, param, bad",
    [(tool, param, bad) for tool, params in SCALAR_ID_PARAMS.items() for param in params for bad in (True, False, "5", 5.0)],
)
async def test_an_id_that_is_a_boolean_a_string_or_a_decimal_is_refused_before_any_http_call(server, mock_gorelo, tool, param, bad):
    text = await call_tool_error(server, tool, {**BASE_ARGS[tool], param: bad})
    assert param in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "tool, param, bad",
    [(tool, param, bad) for tool, params in LIST_ID_PARAMS.items() for param in params for bad in ([True], ["5"], [5.0], [1, False], 5, "5")],
)
async def test_an_id_list_with_a_boolean_a_string_or_a_decimal_is_refused_before_any_http_call(server, mock_gorelo, tool, param, bad):
    text = await call_tool_error(server, tool, {**BASE_ARGS[tool], param: bad})
    assert param in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "tool, param, bad",
    [(tool, param, bad) for tool, params in TEXT_OR_ID_PARAMS.items() for param in params for bad in (True, False, 5.0, [1])],
)
async def test_a_name_or_id_parameter_refuses_a_boolean_or_a_decimal_as_the_id(server, mock_gorelo, tool, param, bad):
    text = await call_tool_error(server, tool, {**BASE_ARGS[tool], param: bad})
    assert param in text
    assert mock_gorelo.requests == []


async def test_a_numeric_text_is_still_an_id_for_the_name_or_id_parameters(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    await call_tool(server, "search_tickets", {"status": "5", "type": 7, "priority": "2"})
    assert mock_gorelo.last.query == {"StatusIds": "5", "PriorityIds": "2", "TypeIds": "7", "PageSize": "200"}


@pytest.mark.parametrize("bad", ["true", "false", "yes", 1, 0])
async def test_send_created_email_is_a_strict_boolean_so_nobody_is_emailed_by_a_lax_value(server, mock_gorelo, bad):
    text = await call_tool_error(server, "create_ticket", {**CREATE_MIN, "send_created_email": bad})
    assert "send_created_email" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("given, tail", [(0, "got zero or a negative number"), (-5, "got zero or a negative number"), (2**63, "got a number above")])
async def test_get_ticket_a_number_that_cannot_be_a_ticket_number_is_refused_without_a_search(server, mock_gorelo, given, tail):
    text = await call_tool_error(server, "get_ticket", {"ticket_id": given})
    assert text.startswith(f"ticket_id: expected a positive whole number such as 123, {tail}")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "data, what",
    [([ticket_detail(1)], "a list of 1 item"), (True, "a boolean"), ({}, "an empty object"), ("x", "a string"), (0, "a number")],
)
async def test_get_ticket_refuses_a_detail_that_is_not_an_object(server, mock_gorelo, data, what):
    mock_gorelo.on("GET", f"{LIST}/{T1}", envelope(data))
    text = await call_tool_error(server, "get_ticket", {"ticket_id": T1})
    assert text == (
        "Gorelo returned an unexpected response for get_ticket: GET /v1/tickets/{ticketId}: expected Data to be a "
        f"non-empty object but got {what}; refusing to guess"
    )


async def test_get_ticket_accepts_a_hyphenless_or_uppercase_guid_and_still_searches_other_text(server, mock_gorelo):
    mock_gorelo.on("GET", f"{LIST}/{T1}", envelope(ticket_detail(1)))
    for given in (T1.upper(), T1.replace("-", ""), f" {T1} "):
        assert (await call_tool(server, "get_ticket", {"ticket_id": given}))["Id"] == T1
    assert [r.path for r in mock_gorelo.requests] == [f"{LIST}/{T1}"] * 3  # never a search for a GUID


# Structural guard over every tools/*.py module: the model-facing text and the code carry no tenant id.
# A string constant (docstring, Field description, literal part of an f-string) may hold a number of 3 or more digits
# only if it is one of these neutral numbers (ISO dates are removed first). Anything else, such as "client_id such
# as 4821", fails the test.
NEUTRAL_TEXT_NUMBERS = {
    "8601",  # ISO 8601
    "080203",  # Gorelo notification code for a missing scope (public error code, not a tenant value)
    "070101",  # example Gorelo notification code in a docstring
    "100", "200", "250", "255", "500", "5000", "10485760", "65535",  # limits and sizes
    "400", "403", "404", "405", "409", "429",  # HTTP status codes
    "123", "456",  # the neutral id examples ("such as 123", "[123, 456]")
    "1042",  # INV-1042 example invoice number
    "1234",  # TCK-1234 example ticket number
}
# An int literal of 3 or more digits is allowed in code only as a module-level UPPER_CASE constant or as one of these.
NEUTRAL_INT_LITERALS = {100, 120, 127, 200, 250, 255, 500, 1024, 5000, 65535}  # page sizes, limits, ASCII DEL, ports
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:T[\d:.]+Z?)?")
_NUMBER = re.compile(r"(?<![0-9A-Za-z])\d{3,}(?![0-9A-Za-z])")


def _tool_modules():
    return sorted((REPO_ROOT / "tools").glob("*.py"))


def _constant_int_nodes(tree):
    """ids of int nodes under a module-level assignment whose targets are all UPPER_CASE names."""
    found = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if all(isinstance(t, ast.Name) and t.id.isupper() for t in targets):
                found.update(id(n) for n in ast.walk(node))
    return found


def test_the_tool_modules_are_found():
    assert len(_tool_modules()) >= 15


@pytest.mark.parametrize("path", _tool_modules(), ids=lambda p: p.name)
def test_no_tool_text_carries_a_tenant_looking_number(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for number in _NUMBER.findall(_DATE.sub(" ", node.value)):
                if number not in NEUTRAL_TEXT_NUMBERS:
                    bad.append((node.lineno, number))
    assert bad == [], f"{path.name}: numbers of 3+ digits in text that are not neutral examples: {bad}"


@pytest.mark.parametrize("path", _tool_modules(), ids=lambda p: p.name)
def test_no_tool_code_carries_a_tenant_looking_int_literal(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    named = _constant_int_nodes(tree)
    bad = [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and type(node.value) is int
        and abs(node.value) >= 100
        and id(node) not in named
        and abs(node.value) not in NEUTRAL_INT_LITERALS
    ]
    assert bad == [], f"{path.name}: int literals of 3+ digits outside named constants: {bad}"


def test_the_guard_would_catch_a_tenant_id_in_text():
    sample = "client_id such as 4821, due 2026-10-01T00:00:00Z, HTTP 404, TCK-1234"
    found = [n for n in _NUMBER.findall(_DATE.sub(" ", sample)) if n not in NEUTRAL_TEXT_NUMBERS]
    assert found == ["4821"]


@pytest.mark.parametrize(
    "tool, args",
    [
        ("list_tickets", {"client_ids": [0]}),
        ("search_tickets", {"client_id": 0}),
        ("create_ticket", {**CREATE_MIN, "client_id": 0}),
        ("update_ticket", {"ticket_id": T1, "status_id": 0}),
    ],
)
async def test_an_id_error_gives_a_neutral_example(server, mock_gorelo, tool, args):
    assert "such as 123" in await call_tool_error(server, tool, args)


def test_the_helpers_still_refuse_what_strict_typing_already_stops_before_the_tool_runs():
    # a direct call (a test, another helper) skips FastMCP's validation, so each check stands on its own
    with pytest.raises(ValueError, match="priority: expected a priority name or an id from 0 to 4, got a boolean"):
        tickets._resolve_priority("priority", True)
    with pytest.raises(ValueError, match="status: expected a positive whole number such as 123, got a boolean"):
        tickets._id_or_name("status", True)
    with pytest.raises(ValueError, match="priority_id: must be one of 0 None, 1 Urgent, 2 High, 3 Normal, 4 Low, got a boolean"):
        tickets._priority_id("priority_id", True)
    with pytest.raises(ValueError, match="source_id: must be one of .*, got a string; Id 7 exists"):
        tickets._source_id("source_id", "2")
    with pytest.raises(ValueError, match="billable_status_id: must be one of 1 Billable, 2 No charge, 3 Non-billable, got a number"):
        tickets._billable_status_id("billable_status_id", 1.0)
    with pytest.raises(ValueError, match="priority_ids: expected a non-empty list of ids, got a string"):
        tickets._each("priority_ids", "2", tickets._priority_id)
    with pytest.raises(ValueError, match="ticket_id: expected a positive whole number such as 123, got zero or a negative number"):
        tickets._ticket_reference(0)
    with pytest.raises(ValueError, match="ticket_id: must not be empty"):
        tickets._ticket_reference(True)
    assert tickets._ticket_reference(" TCK-1234 ") == "TCK-1234" and tickets._ticket_reference(1234) == "1234"


DESCRIPTION_LIMITS = {"update_ticket": 900}  # every other tool: 700 characters
PARAM_LIMIT = 160
# The size measure (json.dumps of each tool as a client lists it, summed). The advertised
# schemas are compacted centrally (server.compact_input_schema: no null branches, no "default": null), so what the
# module still controls is its descriptions. Measured 16150 bytes (descriptions are 7034 of them) with every
# statement kept; the limit is that plus 10 percent, rounded up to the next 100.
MODULE_BYTES_LIMIT = 17800


async def test_the_tool_list_keeps_to_the_concision_limits(server):
    listed = {tool.name: tool for tool in await list_tools(server) if tool.name in EXPECTED_TOOLS}
    assert sorted(listed) == sorted(EXPECTED_TOOLS)
    for name, tool in listed.items():
        assert 40 < len(tool.description) <= DESCRIPTION_LIMITS.get(name, 700), (name, len(tool.description))
        for param, definition in tool.inputSchema["properties"].items():
            assert 0 < len(definition["description"]) <= PARAM_LIMIT, (name, param, len(definition["description"]))


async def test_the_package_tool_list_does_not_grow_past_its_measured_size(server):
    listed = [tool for tool in await list_tools(server) if tool.name in EXPECTED_TOOLS]
    total = sum(len(json.dumps(tool.model_dump(mode="json", exclude_none=True))) for tool in listed)
    assert total <= MODULE_BYTES_LIMIT, total
