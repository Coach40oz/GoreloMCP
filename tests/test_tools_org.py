"""tools/org.py: list_org_groups, list_org_users."""

import httpx
import pytest
from conftest import (
    TEST_TRACE_ID,
    call_tool,
    call_tool_error,
    envelope,
    error_envelope,
    list_tools,
    paged_envelope,
    paged_responder,
    pagination,
)

import tools.org as org_module
from gorelo_client import FORBIDDEN_OPS
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio

GROUPS = "/v1/organization/groups"
USERS = "/v1/organization/users"

TOOLS = {
    "list_org_groups": ["GET /v1/organization/groups"],
    "list_org_users": ["GET /v1/organization/users"],
}


@pytest.fixture
def server(server_factory):
    return server_factory(toolsets={"core"})


def group_record(group_id=7201, name="Everyone", **extra):
    record = {
        "Id": group_id,
        "Name": name,
        "Alias": name.lower(),
        "OutboundEmail": "support@example.invalid",
        "CreatedOn": "2024-01-02T03:04:05Z",
        "UpdatedOn": None,
    }
    record.update(extra)
    return record


def user_record(user_id=9201, first="Alex", last="Example", **extra):
    record = {
        "Id": user_id,
        "FirstName": first,
        "LastName": last,
        "Email": f"{first.lower()}@example.invalid",
        "Status": {"Id": 1, "Name": "Active"},
        "TimeZone": "UTC",
        "CreatedOn": "2024-01-02T03:04:05Z",
        "UpdatedOn": None,
    }
    record.update(extra)
    return record


def trace(text):
    return f"{text} [trace {TEST_TRACE_ID}]"


def flat(text):
    return " ".join(text.split())


# --------------------------------------------------------------------------
# Declarations
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_each_tool_is_declared_as_a_core_read_tool_over_its_op(name, spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == name)
    assert (spec.toolset, spec.kind, spec.ops, spec.destructive_hint) == ("core", "read", TOOLS[name], False)
    assert all(op in spec_index.ops for op in spec.ops) and not set(spec.ops) & FORBIDDEN_OPS


@pytest.mark.parametrize("name", sorted(TOOLS))
async def test_neither_tool_takes_a_parameter(name, server):
    tool = next(t for t in await list_tools(server) if t.name == name)
    assert tool.inputSchema.get("properties", {}) == {}
    assert not tool.inputSchema.get("required")
    assert (tool.annotations.readOnlyHint, tool.annotations.destructiveHint) == (True, False)


@pytest.mark.parametrize("name", sorted(TOOLS))
@pytest.mark.parametrize("extra", ["page_size", "cursor", "query"])
async def test_a_paging_or_filter_argument_is_refused_before_any_http_call(server, mock_gorelo, name, extra):
    # the legacy list_org_groups sent pageSize=200 and Gorelo answered 400
    text = await call_tool_error(server, name, {extra: 5 if extra == "page_size" else "x"})
    assert extra in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_docstrings_follow_the_template(name):
    doc = next(s for s in REGISTRY.specs if s.name == name).fn.__doc__
    assert "Side effects:" not in doc and "Paging:" not in doc  # read only, and no paging for the caller


def test_the_groups_docstring_says_it_is_not_paged_and_the_users_docstring_says_every_page_is_read():
    groups = flat(org_module.list_org_groups.__doc__)
    assert "not paged" in groups and "group id the ticket tools take" in groups
    users = flat(org_module.list_org_users.__doc__)
    assert "all pages are read for you" in users and "truncated is false when the list is complete" in users
    assert "technician id other tools take" in users


# --------------------------------------------------------------------------
# list_org_groups
# --------------------------------------------------------------------------


async def test_list_org_groups_makes_a_plain_get_with_no_query_string(server, mock_gorelo):
    rows = [group_record(7201, "Everyone"), group_record(7202, "Service Desk")]
    mock_gorelo.on("GET", GROUPS, envelope(rows))
    result = await call_tool(server, "list_org_groups")
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", GROUPS, {}, b"")
    assert "?" not in request.url
    assert len(mock_gorelo.requests) == 1
    assert result == {"items": rows, "count": 2}


async def test_list_org_groups_works_where_a_page_size_would_be_rejected(server, mock_gorelo):
    def respond(request):
        if request.query:
            note = ("070101", "This query parameter is not recognized by this endpoint.", next(iter(request.query)))
            return error_envelope(400, [note])
        return envelope([group_record()])

    mock_gorelo.on("GET", GROUPS, respond)
    assert (await call_tool(server, "list_org_groups"))["count"] == 1


async def test_list_org_groups_returns_an_empty_list_as_an_empty_list(server, mock_gorelo):
    mock_gorelo.on("GET", GROUPS, envelope([]))
    assert await call_tool(server, "list_org_groups") == {"items": [], "count": 0}


@pytest.mark.parametrize("data", [None, {"Items": []}, "none", True])
async def test_list_org_groups_refuses_an_answer_that_is_not_a_list(server, mock_gorelo, data):
    mock_gorelo.on("GET", GROUPS, envelope(data))
    text = await call_tool_error(server, "list_org_groups")
    assert text.startswith("Gorelo returned an unexpected response for list_org_groups")
    assert "expected Data to be a list" in text


async def test_list_org_groups_maps_a_gorelo_error_to_a_tool_error(server, mock_gorelo):
    mock_gorelo.on("GET", GROUPS, error_envelope(401, [("070401", "Invalid API key.")]))
    text = await call_tool_error(server, "list_org_groups")
    assert text == trace("Gorelo rejected list_org_groups (HTTP 401, code 070401): Invalid API key.")


async def test_list_org_groups_names_a_missing_scope(server, mock_gorelo):
    mock_gorelo.on("GET", GROUPS, error_envelope(403, [("080203", "API key does not have 'Organization' scope")]))
    text = await call_tool_error(server, "list_org_groups")
    assert "the API key does not have the 'Organization' scope" in text


async def test_list_org_groups_on_a_timeout_says_retrying_is_safe(server, mock_gorelo):
    mock_gorelo.on("GET", GROUPS, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "list_org_groups")
    assert text.startswith("Gorelo did not answer list_org_groups") and "retrying is safe" in text


# --------------------------------------------------------------------------
# list_org_users
# --------------------------------------------------------------------------


async def test_list_org_users_reads_the_single_page_and_returns_the_all_result_shape(server, mock_gorelo):
    rows = [user_record(9201), user_record(9202, "Sam", "Sample")]
    mock_gorelo.on("GET", USERS, paged_envelope(rows, total_count=2))
    result = await call_tool(server, "list_org_users")
    assert result == {
        "items": rows,
        "count": 2,
        "total_count": 2,
        "truncated": False,
        "complete_scan": True,
        "count_mismatch": False,
        "filters": {},
    }
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", USERS, {"PageSize": "200"}, b"")
    assert len(mock_gorelo.requests) == 1


async def test_list_org_users_follows_every_cursor_with_the_same_page_size(server, mock_gorelo):
    pages = [[user_record(1)], [user_record(2)], [user_record(3)]]
    mock_gorelo.on("GET", USERS, paged_responder(pages, total_count=3))
    result = await call_tool(server, "list_org_users")
    assert [item["Id"] for item in result["items"]] == [1, 2, 3]
    assert result["count"] == 3 and result["total_count"] == 3
    assert (result["truncated"], result["complete_scan"], result["count_mismatch"]) == (False, True, False)
    assert [r.query for r in mock_gorelo.requests] == [
        {"PageSize": "200"},
        {"PageSize": "200", "Cursor": "c1"},
        {"PageSize": "200", "Cursor": "c2"},
    ]


async def test_list_org_users_flags_a_row_count_that_disagrees_with_the_total(server, mock_gorelo):
    mock_gorelo.on("GET", USERS, paged_envelope([user_record(1), user_record(2)], total_count=5))
    result = await call_tool(server, "list_org_users")
    assert result["count"] == 2 and result["total_count"] == 5
    assert result["count_mismatch"] is True and result["complete_scan"] is True and result["truncated"] is False


async def test_list_org_users_reports_an_empty_tenant_as_an_empty_complete_scan(server, mock_gorelo):
    mock_gorelo.on("GET", USERS, envelope([], pagination(None, 0, has_more=False)))
    result = await call_tool(server, "list_org_users")
    assert result["items"] == [] and result["count"] == 0 and result["total_count"] == 0
    assert result["complete_scan"] is True and result["truncated"] is False


async def test_list_org_users_stops_with_an_error_rather_than_loop_on_a_repeated_cursor(server, mock_gorelo):
    def respond(request):
        return envelope([user_record(1)], pagination("c1", 99))

    mock_gorelo.on("GET", USERS, respond)
    text = await call_tool_error(server, "list_org_users")
    assert text.startswith("Gorelo returned an unexpected response for list_org_users")
    assert "returned the cursor it had already served" in text
    assert len(mock_gorelo.requests) == 2


async def test_list_org_users_never_returns_a_partial_list_when_a_later_page_fails(server, mock_gorelo):
    mock_gorelo.on(
        "GET",
        USERS,
        lambda request: envelope([user_record(1)], pagination("c1", 2))
        if "Cursor" not in request.query
        else error_envelope(500, [("070500", "Boom.")]),
    )
    text = await call_tool_error(server, "list_org_users")
    assert text == trace("Gorelo rejected list_org_users (HTTP 500, code 070500): Boom.")
    assert len(mock_gorelo.requests) == 2


async def test_list_org_users_refuses_the_legacy_lowercase_body_instead_of_returning_zero_rows(server, mock_gorelo):
    mock_gorelo.on("GET", USERS, {"data": [{"id": 9201}], "nextCursor": None, "hasMore": False})
    text = await call_tool_error(server, "list_org_users")
    assert text.startswith("Gorelo returned an unexpected response for list_org_users")
    assert "refusing to guess" in text


async def test_list_org_users_maps_a_gorelo_error_to_a_tool_error(server, mock_gorelo):
    mock_gorelo.on("GET", USERS, error_envelope(401, [("070401", "Invalid API key.")]))
    text = await call_tool_error(server, "list_org_users")
    assert text == trace("Gorelo rejected list_org_users (HTTP 401, code 070401): Invalid API key.")


async def test_list_org_users_maps_a_page_size_rejection_to_the_page_size_name(server, mock_gorelo):
    mock_gorelo.on("GET", USERS, error_envelope(400, [("070101", "PageSize must be between 1 and 200.", "PageSize")]))
    text = await call_tool_error(server, "list_org_users")
    # the tool has no page_size parameter, so Gorelo's own name is kept
    assert text == trace("Gorelo rejected list_org_users (HTTP 400, code 070101): PageSize: PageSize must be between 1 and 200.")
