"""tools/deletions.py: list_deletions (GET /v1/deletions, new in contract a7954b24844b)."""

import inspect
import json
import re
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
    pagination,
    paged_envelope,
    uid,
)
from fastmcp.exceptions import ToolError

import tools.deletions as deletions_module
from gorelo_client import is_forbidden_op, is_side_effect_get
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio

DELETIONS = "/v1/deletions"
LIST_PARAMS = ["type_ids", "since", "page_size", "cursor"]
LATEST_SPEC = Path(__file__).resolve().parent.parent / "backups" / "swagger-latest.json"
TYPE_HELP = "1 Client, 2 Contact, 3 Contract, 4 Ticket, 5 Item, 6 Invoice, 7 Custom asset, 8 Project, 9 Task"
TYPE_NAMES = {1: "Client", 2: "Contact", 3: "Contract", 4: "Ticket", 5: "Item", 6: "Invoice", 7: "Custom asset", 8: "Project", 9: "Task"}


def trace(text):
    return f"{text} [trace {TEST_TRACE_ID}]"


def flat(text):
    return " ".join((text or "").split())


def deletion_row(n=1, type_id=1, **extra):
    """One DeletionResponse as Gorelo returns it. Id is text: a number for clients, contacts and contracts (types 1 to 3),
    a GUID for every other type."""
    row = {
        "Type": {"Id": type_id, "Name": TYPE_NAMES[type_id]},
        "Id": str(1000 + n) if type_id <= 3 else uid(n),
        "DeletedOn": "2026-10-16T12:00:00Z",
    }
    row.update(extra)
    return row


@pytest.fixture
def full_server(server_factory):
    """Every toolset on, delete tools off: list_deletions must not depend on either."""
    return server_factory()


async def call_directly(client_factory, **kwargs):
    """Call the decorated function itself (no pydantic in front of it): for the checks the schema normally pre-empts."""
    async with client_factory() as client:
        return await deletions_module.list_deletions(make_ctx(client), **kwargs)


# --------------------------------------------------------------------------
# Declaration
# --------------------------------------------------------------------------


def test_list_deletions_is_declared_as_a_core_read_tool_over_get_deletions(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "list_deletions")
    assert (spec.toolset, spec.kind, spec.ops, spec.destructive_hint) == ("core", "read", ["GET /v1/deletions"], False)
    op = spec_index.op("GET /v1/deletions")
    assert op.paged and op.page_size_rule == "reject" and "GET /v1/deletions" in spec_index.ops
    assert not any(is_forbidden_op(o) for o in spec.ops) and not any(is_side_effect_get(o) for o in spec.ops)


def test_the_module_declares_exactly_the_one_tool_and_names_its_op():
    names = {s.name: (s.kind, s.ops) for s in REGISTRY.specs if s.fn.__module__ == deletions_module.__name__}
    assert names == {"list_deletions": ("read", ["GET /v1/deletions"])}
    assert "GET /v1/deletions" in deletions_module.__doc__


def test_every_query_name_of_the_op_is_a_parameter_of_the_tool(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "list_deletions")
    op = spec_index.op("GET /v1/deletions")
    assert set(spec.field_map.values()) == set(op.query_params)  # nothing the spec offers is left out, nothing invented
    assert spec.field_map == {"type_ids": "TypeIds", "since": "Since", "page_size": "PageSize", "cursor": "Cursor"}


def test_the_fixture_is_shaped_like_the_spec(spec_index):
    assert set(deletion_row()) == set(spec_index.schema("DeletionResponse")["fields"])
    assert set(deletion_row()["Type"]) == set(spec_index.schema("CodeModel")["fields"])


async def test_the_parameters_are_the_documented_ones_and_only_type_ids_is_required(full_server):
    tool = next(t for t in await list_tools(full_server) if t.name == "list_deletions")
    properties = tool.inputSchema["properties"]
    assert list(properties) == LIST_PARAMS
    assert tool.inputSchema["required"] == ["type_ids"]
    assert all(p.get("description") for p in properties.values())
    assert properties["type_ids"]["type"] == "array" and properties["type_ids"]["items"]["type"] == "integer"
    assert properties["page_size"]["default"] == 50
    assert (tool.annotations.readOnlyHint, tool.annotations.destructiveHint, tool.annotations.idempotentHint) == (True, False, True)


async def test_the_parameter_texts_carry_the_type_scale_the_offset_rule_and_the_paging_rule(full_server):
    tool = next(t for t in await list_tools(full_server) if t.name == "list_deletions")
    properties = tool.inputSchema["properties"]
    assert TYPE_HELP in properties["type_ids"]["description"] and properties["type_ids"]["description"].startswith("Required.")
    assert "fails the whole call with a 403" in properties["type_ids"]["description"]
    assert all(len(p["description"]) <= 220 for p in properties.values())  # MAX_PARAM_DESCRIPTION_CHARS
    assert properties["since"]["description"].startswith("At or after this instant, UTC offset required")
    assert "a record with no recorded last update is never listed" in properties["since"]["description"]
    assert properties["page_size"]["description"] == "1-200, clamped."
    assert "same type_ids and since" in properties["cursor"]["description"]
    assert "until has_more is false" in properties["cursor"]["description"]


def test_the_docstring_gives_what_the_model_must_know():
    doc = flat(inspect.getdoc(deletions_module.list_deletions))
    for phrase in (
        "Not a history: each record appears once, with its latest deletion",
        "a record restored in the Gorelo app leaves the list",
        "Id is text: a number for clients, contacts and contracts, a GUID for the others",
        "DeletedOn is the record's last update, so it can move later if the record is edited; since is inclusive",
        "A contact is also listed when its client was deleted, a task when its project was",
        "A voided invoice is not deleted.",
        "clients Clients, contacts Contacts, contracts, items and invoices Billing, tickets Tickets, custom assets Assets, projects and tasks Project",
        "A type the key cannot read fails the call with a 403, code 070208 on type_ids",
        "Side effects: None (read-only).",
        "Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.",
    ):
        assert phrase in doc, phrase
    assert doc.startswith("List records deleted in Gorelo")
    assert len(doc.split("; returns")[0]) < 320  # the catalog cuts a longer first sentence
    assert len(doc) <= 900  # MAX_TOOL_DESCRIPTION_CHARS of tests/test_integration.py


def test_no_text_of_the_module_carries_an_em_or_en_dash():
    texts = [Path(deletions_module.__file__).read_text(encoding="utf-8"), inspect.getdoc(deletions_module.list_deletions)]
    assert not [t for t in texts if chr(0x2014) in t or chr(0x2013) in t]


def test_the_type_table_covers_the_documented_scale_and_nothing_else():
    assert deletions_module.DELETION_TYPES == TYPE_NAMES
    assert deletions_module.TYPE_HELP == TYPE_HELP


@pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")
def test_the_type_table_and_the_texts_are_the_ones_in_the_spec():
    """spec/spec_index.json keeps no description text, so the scale and the claims the docstring repeats are pinned to
    the full OpenAPI snapshot of contract a7954b24844b."""
    spec = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))
    op = spec["paths"]["/v1/deletions"]["get"]
    params = {p["name"]: flat(p["description"]) for p in op["parameters"]}
    types = {int(n): name.strip() for n, name in re.findall(r"(\d+) ([A-Z][A-Za-z ]*?)(?=,|\.)", params["TypeIds"])}
    assert types == deletions_module.DELETION_TYPES
    assert params["TypeIds"].startswith("Required.") and "Up to 10 values; any other value is a 400." in params["TypeIds"]
    assert "ISO-8601 UTC timestamp; keeps only deletions at or after it." in params["Since"]
    assert "Out of range is rejected, not clamped." in params["PageSize"]
    text = flat(op["description"])
    for claim in (
        "Each record appears once, with its latest deletion; this is not a history.",
        "A record restored in the Gorelo app leaves this feed and appears in its normal list again.",
        "is the deleted record's last update, so it can move later if the deleted record is edited.",
        "A voided invoice is not deleted: it is still returned by `GET /v1/invoices`.",
        "A record with no recorded last update has no deletion time and is not listed.",
        "clients `Clients`, contacts `Contacts`, contracts, items and invoices `Billing`, tickets `Tickets`, custom assets `Assets`, projects and tasks `Project`.",
        "Asking for a type the key cannot read is a 403 with code `070208` on `TypeIds`.",
    ):
        assert claim in text, claim
    id_text = flat(spec["components"]["schemas"]["DeletionResponse"]["properties"]["Id"]["description"])
    assert "a number for clients, contacts and contracts, a GUID for tickets, items, invoices, custom assets, projects and tasks" in id_text
    assert "Results are sorted by `DeletedOn` ascending, then type id, then id" in flat(op["summary"])

# --------------------------------------------------------------------------
# The request and the result
# --------------------------------------------------------------------------


async def test_list_deletions_with_only_type_ids_sends_the_page_size_and_nothing_else(full_server, mock_gorelo):
    rows = [deletion_row(1, 1), deletion_row(2, 4)]
    mock_gorelo.on("GET", DELETIONS, paged_envelope(rows, total_count=2))
    result = await call_tool(full_server, "list_deletions", {"type_ids": [1, 4]})
    request = mock_gorelo.last
    assert (request.method, request.path, request.raw_path) == ("GET", DELETIONS, "/v1/deletions")
    assert request.query == {"TypeIds": "1,4", "PageSize": "50"} and request.content == b""
    assert result == {
        "items": rows,
        "count": 2,
        "total_count": 2,
        "has_more": False,
        "next_cursor": None,
        "page_size": 50,
        "filters": {"type_ids": [1, 4]},
    }
    assert len(mock_gorelo.requests) == 1


async def test_every_parameter_goes_out_under_its_spec_name(full_server, mock_gorelo):
    mock_gorelo.on("GET", DELETIONS, paged_envelope([deletion_row()]))
    args = {"type_ids": [2, 1, 9], "since": "2026-10-01T08:00:00+02:00", "page_size": 25, "cursor": "opaque-cursor-1"}
    result = await call_tool(full_server, "list_deletions", args)
    assert mock_gorelo.last.query == {
        "TypeIds": "2,1,9",
        "Since": "2026-10-01T06:00:00Z",  # converted to UTC
        "PageSize": "25",
        "Cursor": "opaque-cursor-1",
    }
    # the echo is in the tool's own terms, so the next call can repeat it unchanged
    assert result["filters"] == {"type_ids": [2, 1, 9], "since": "2026-10-01T06:00:00Z"}
    assert result["page_size"] == 25


@pytest.mark.parametrize("type_id", sorted(TYPE_NAMES))
async def test_each_documented_type_id_is_sent_as_given(full_server, mock_gorelo, type_id):
    mock_gorelo.on("GET", DELETIONS, paged_envelope([]))
    result = await call_tool(full_server, "list_deletions", {"type_ids": [type_id]})
    assert mock_gorelo.last.query["TypeIds"] == str(type_id) and result["filters"]["type_ids"] == [type_id]


@pytest.mark.parametrize(
    "type_ids, wire, echoed",
    [
        ([1, 2, 3, 4, 5, 6, 7, 8, 9], "1,2,3,4,5,6,7,8,9", [1, 2, 3, 4, 5, 6, 7, 8, 9]),
        ([9, 8, 1], "9,8,1", [9, 8, 1]),
        ([4, 4, 1, 4], "4,1", [4, 1]),  # a repeat is dropped, the order kept
        ([1, 2, 3, 4, 5, 6, 7, 8, 9, 1, 2, 3], "1,2,3,4,5,6,7,8,9", [1, 2, 3, 4, 5, 6, 7, 8, 9]),  # 12 values, 9 distinct
    ],
)
async def test_type_ids_are_distinct_in_the_callers_order(full_server, mock_gorelo, type_ids, wire, echoed):
    mock_gorelo.on("GET", DELETIONS, paged_envelope([]))
    result = await call_tool(full_server, "list_deletions", {"type_ids": type_ids})
    assert mock_gorelo.last.query["TypeIds"] == wire and result["filters"]["type_ids"] == echoed


@pytest.mark.parametrize(
    "since, wire",
    [
        ("2026-10-01T08:00:00Z", "2026-10-01T08:00:00Z"),
        ("2026-10-01T08:00:00+00:00", "2026-10-01T08:00:00Z"),
        ("2026-10-01T08:00:00-05:00", "2026-10-01T13:00:00Z"),
        ("2026-10-01T08:00:00.250000Z", "2026-10-01T08:00:00.250000Z"),
    ],
)
async def test_since_is_converted_to_utc(full_server, mock_gorelo, since, wire):
    mock_gorelo.on("GET", DELETIONS, paged_envelope([]))
    result = await call_tool(full_server, "list_deletions", {"type_ids": [1], "since": since})
    assert mock_gorelo.last.query["Since"] == wire and result["filters"]["since"] == wire


async def test_without_since_no_since_is_sent_or_echoed(full_server, mock_gorelo):
    mock_gorelo.on("GET", DELETIONS, paged_envelope([]))
    result = await call_tool(full_server, "list_deletions", {"type_ids": [1]})
    assert "Since" not in mock_gorelo.last.query and "Cursor" not in mock_gorelo.last.query
    assert result["filters"] == {"type_ids": [1]}


@pytest.mark.parametrize("given, used", [(0, 1), (-5, 1), (1, 1), (50, 50), (200, 200), (201, 200), (5000, 200)])
async def test_page_size_is_clamped_to_1_200_and_the_size_used_is_reported(full_server, mock_gorelo, given, used):
    mock_gorelo.on("GET", DELETIONS, paged_envelope([]))
    result = await call_tool(full_server, "list_deletions", {"type_ids": [1], "page_size": given})
    assert mock_gorelo.last.query["PageSize"] == str(used) and result["page_size"] == used


async def test_paging_follows_next_cursor_with_the_same_filters(full_server, mock_gorelo):
    first = [deletion_row(1, 1), deletion_row(2, 2)]
    second = [deletion_row(3, 4)]
    mock_gorelo.on(
        "GET",
        DELETIONS,
        in_order(
            envelope(first, pagination("cursor-2", 3)),
            envelope(second, pagination(None, 3, has_more=False)),
        ),
    )
    filters = {"type_ids": [1, 2, 4], "since": "2026-10-01T00:00:00Z", "page_size": 2}
    one = await call_tool(full_server, "list_deletions", filters)
    assert (one["has_more"], one["next_cursor"], one["total_count"], one["count"]) == (True, "cursor-2", 3, 2)
    two = await call_tool(full_server, "list_deletions", {**filters, "cursor": one["next_cursor"]})
    assert (two["has_more"], two["next_cursor"], two["count"]) == (False, None, 1)
    first_query, second_query = (r.query for r in mock_gorelo.requests)
    base = {"TypeIds": "1,2,4", "Since": "2026-10-01T00:00:00Z", "PageSize": "2"}
    assert first_query == base and second_query == {**base, "Cursor": "cursor-2"}
    assert one["filters"] == two["filters"]
    assert [item["Id"] for item in one["items"] + two["items"]] == ["1001", "1002", uid(3)]


async def test_the_echoed_filters_can_be_sent_back_unchanged_for_the_next_page(full_server, mock_gorelo):
    mock_gorelo.on("GET", DELETIONS, paged_envelope([]))
    first = await call_tool(full_server, "list_deletions", {"type_ids": [3, 1], "since": "2026-10-01T08:00:00+02:00"})
    again = await call_tool(full_server, "list_deletions", {**first["filters"], "cursor": "c-2"})
    assert again["filters"] == first["filters"]
    assert mock_gorelo.requests[1].query["Since"] == "2026-10-01T06:00:00Z" and mock_gorelo.requests[1].query["TypeIds"] == "3,1"


async def test_an_empty_page_is_reported_as_one_with_its_total(full_server, mock_gorelo):
    mock_gorelo.on("GET", DELETIONS, envelope([], pagination(None, 0, has_more=False)))
    result = await call_tool(full_server, "list_deletions", {"type_ids": [6]})
    assert result["items"] == [] and result["count"] == 0 and result["total_count"] == 0 and result["has_more"] is False


async def test_the_rows_come_back_unchanged_with_the_id_as_text(full_server, mock_gorelo):
    rows = [deletion_row(n, type_id) for n, type_id in enumerate(range(1, 10), start=1)]
    mock_gorelo.on("GET", DELETIONS, paged_envelope(rows))
    result = await call_tool(full_server, "list_deletions", {"type_ids": list(range(1, 10))})
    assert result["items"] == rows and result["count"] == 9
    assert all(isinstance(item["Id"], str) for item in result["items"])
    assert [item["Id"] for item in result["items"][:3]] == ["1001", "1002", "1003"]  # a number, as text
    assert result["items"][3]["Id"] == uid(4)  # a GUID
    assert [item["Type"]["Name"] for item in result["items"]] == list(TYPE_NAMES.values())


async def test_list_deletions_refuses_an_answer_that_is_not_a_list(full_server, mock_gorelo):
    mock_gorelo.on("GET", DELETIONS, envelope({"Id": "1"}, pagination(None, 1, has_more=False)))
    text = await call_tool_error(full_server, "list_deletions", {"type_ids": [1]})
    assert text.startswith("Gorelo returned an unexpected response for list_deletions") and "expected Data to be a list" in text


async def test_list_deletions_sends_a_get_and_nothing_else(full_server, mock_gorelo):
    mock_gorelo.on("GET", DELETIONS, paged_envelope([deletion_row()]))
    await call_tool(full_server, "list_deletions", {"type_ids": [1]})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", DELETIONS)]


async def test_list_deletions_is_registered_in_the_core_toolset_only(server_factory):
    core = {t.name for t in await list_tools(server_factory(toolsets={"core"}))}
    assert "list_deletions" in core
    others = {t.name for t in await list_tools(server_factory(toolsets={"tickets", "time", "billing", "uptime", "projects", "forms"}))}
    assert "list_deletions" not in others
    off = {t.name for t in await list_tools(server_factory(toolsets={"core"}, destructive=False))}
    assert "list_deletions" in off  # a read tool needs no destructive switch

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "property_name, param",
    [("TypeIds", "type_ids"), ("Since", "since"), ("PageSize", "page_size"), ("Cursor", "cursor")],
)
async def test_a_gorelo_400_names_the_snake_case_param(full_server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", DELETIONS, error_envelope(400, [("070101", "A value that is not valid.", property_name)]))
    text = await call_tool_error(full_server, "list_deletions", {"type_ids": [1]})
    assert text == trace(f"Gorelo rejected list_deletions (HTTP 400, code 070101): {param}: A value that is not valid.")


async def test_a_type_the_key_cannot_read_is_a_403_with_code_070208_that_names_type_ids(full_server, mock_gorelo):
    mock_gorelo.on("GET", DELETIONS, error_envelope(403, [("070208", "The API key cannot read this record type.", "TypeIds")]))
    text = await call_tool_error(full_server, "list_deletions", {"type_ids": [1, 8]})
    assert text == trace("Gorelo rejected list_deletions (HTTP 403, code 070208): type_ids: The API key cannot read this record type.")
    assert len(mock_gorelo.requests) == 1  # one call, no retry with fewer types behind the model's back


async def test_a_missing_scope_reads_as_a_missing_scope(full_server, mock_gorelo):
    mock_gorelo.on("GET", DELETIONS, error_envelope(403, [("080203", "API key does not have 'Clients' scope")]))
    text = await call_tool_error(full_server, "list_deletions", {"type_ids": [1]})
    assert "the API key does not have the 'Clients' scope" in text


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")])
async def test_a_read_that_times_out_or_loses_the_connection_is_safe_to_retry(full_server, mock_gorelo, failure):
    mock_gorelo.on("GET", DELETIONS, failure)
    text = await call_tool_error(full_server, "list_deletions", {"type_ids": [1]})
    assert text.startswith("Gorelo did not answer list_deletions") and "This was a read, so retrying is safe." in text
    assert "write" not in text.lower()


async def test_a_server_error_on_the_read_is_reported_with_its_trace_id(full_server, mock_gorelo):
    mock_gorelo.on("GET", DELETIONS, error_envelope(500, [("070500", "Boom.")]))
    text = await call_tool_error(full_server, "list_deletions", {"type_ids": [1]})
    assert text == trace("Gorelo rejected list_deletions (HTTP 500, code 070500): Boom.")


# --------------------------------------------------------------------------
# Local validation (nothing is sent)
# --------------------------------------------------------------------------

LOCAL_ERRORS = [
    pytest.param({"type_ids": None}, f"type_ids: required; give a list such as [1, 4] (types: {TYPE_HELP})", id="type-ids-none"),
    pytest.param({"type_ids": []}, f"type_ids: expected at least one type id, got an empty list; types: {TYPE_HELP}", id="empty-type-ids"),
    pytest.param({"type_ids": "1,2"}, "type_ids: expected a list of type ids such as [1, 4], got a string", id="type-ids-text"),
    pytest.param({"type_ids": 4}, "type_ids: expected a list of type ids such as [1, 4], got a number", id="type-ids-number"),
    pytest.param({"type_ids": [0]}, f"type_ids[0]: 0 is not a record type; valid ids: {TYPE_HELP}", id="type-0"),
    pytest.param({"type_ids": [1, 10]}, "type_ids[1]: 10 is not a record type; valid ids: 1 Client", id="type-10"),
    pytest.param({"type_ids": [-1]}, "type_ids[0]: -1 is not a record type", id="negative-type"),
    pytest.param({"type_ids": [2**63]}, "type_ids[0]: 9223372036854775808 is not a record type", id="huge-type"),
    pytest.param({"type_ids": [True]}, "type_ids[0]: a boolean is not a record type", id="bool-type"),
    pytest.param({"type_ids": ["1"]}, "type_ids[0]: a string is not a record type", id="text-type"),
    pytest.param({"type_ids": [1.0]}, "type_ids[0]: a number is not a record type", id="decimal-type"),
    pytest.param({"type_ids": [1], "since": "yesterday"}, "since: 'yesterday' is not an ISO 8601 datetime", id="garbage-since"),
    pytest.param({"type_ids": [1], "since": "2026-10-01T08:00:00"}, "since: '2026-10-01T08:00:00' has no UTC offset", id="naive-since"),
    pytest.param({"type_ids": [1], "since": "2026-10-01"}, "since: '2026-10-01' has no UTC offset", id="date-only-since"),
    pytest.param({"type_ids": [1], "since": ""}, "since: expected an ISO 8601 datetime with a UTC offset", id="blank-since"),
    pytest.param({"type_ids": [1], "cursor": ""}, "cursor: must not be empty or whitespace only", id="blank-cursor"),
    pytest.param({"type_ids": [1], "cursor": "   "}, "cursor: must not be empty or whitespace only", id="whitespace-cursor"),
    pytest.param({"type_ids": [1], "page_size": "many"}, "page_size: expected an integer from 1 to 200", id="page-size-text"),
]


@pytest.mark.parametrize("args, fragment", LOCAL_ERRORS)
async def test_local_validation_errors_name_the_param_and_send_nothing(client_factory, mock_gorelo, args, fragment):
    with pytest.raises(ToolError) as info:
        await call_directly(client_factory, **args)
    assert fragment in str(info.value)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "args, param",
    [
        ({}, "type_ids"),
        ({"type_ids": "1"}, "type_ids"),
        ({"type_ids": 1}, "type_ids"),
        ({"type_ids": [1], "since": 20261001}, "since"),
        ({"type_ids": [1], "page_size": "many"}, "page_size"),
        ({"type_ids": [1], "cursor": 5}, "cursor"),
    ],
)
async def test_a_value_outside_the_schema_is_refused_with_the_param_name(full_server, mock_gorelo, args, param):
    text = await call_tool_error(full_server, "list_deletions", args)
    assert param in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", [True, "5", 5.0], ids=["json-true", "text-5", "decimal-5"])
async def test_type_ids_are_strict_true_and_text_are_refused_instead_of_becoming_a_type(full_server, mock_gorelo, bad):
    text = await call_tool_error(full_server, "list_deletions", {"type_ids": [bad]})
    assert "type_ids" in text and "valid integer" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
async def test_a_blank_cursor_is_refused_before_any_http_call(full_server, mock_gorelo, blank):
    text = await call_tool_error(full_server, "list_deletions", {"type_ids": [1], "cursor": blank})
    assert "cursor: must not be empty or whitespace only" in text
    assert mock_gorelo.requests == []


async def test_an_argument_the_tool_does_not_have_is_refused(full_server, mock_gorelo):
    for extra in ({"client_id": 5}, {"TypeIds": "1"}, {"status_ids": [1]}, {"sort_order": "asc"}, {"until": "2026-10-01T00:00:00Z"}):
        text = await call_tool_error(full_server, "list_deletions", {"type_ids": [1], **extra})
        assert next(iter(extra)) in text
    assert mock_gorelo.requests == []
