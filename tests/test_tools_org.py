"""tools/org.py: list_org_roles, list_org_groups, get_org_group, list_org_users, get_org_user."""

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
    list_tools,
    make_ctx,
    paged_envelope,
    paged_responder,
    pagination,
)
from fastmcp.exceptions import ToolError

import tools.org as org_module
from gorelo_client import FORBIDDEN_OPS
from tools._common import MAX_ID, REGISTRY

pytestmark = pytest.mark.anyio

EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)
LATEST_SPEC = Path(__file__).resolve().parent.parent / "backups" / "swagger-latest.json"
needs_spec_text = pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")

ROLES = "/v1/organization/roles"
GROUPS = "/v1/organization/groups"
USERS = "/v1/organization/users"
GROUP_PATH = f"{GROUPS}/7201"
USER_PATH = f"{USERS}/9201"

TOOLS = {
    "list_org_roles": ["GET /v1/organization/roles"],
    "list_org_groups": ["GET /v1/organization/groups"],
    "get_org_group": ["GET /v1/organization/groups/{groupId}"],
    "list_org_users": ["GET /v1/organization/users"],
    "get_org_user": ["GET /v1/organization/users/{userId}"],
}

# Arguments that each tool does not take: a paged list takes page_size and cursor only, the others take none of them.
NOT_TAKEN = {
    "list_org_roles": ["page_size", "cursor", "query"],
    "list_org_groups": ["page_size", "cursor", "query"],
    "get_org_group": ["page_size", "cursor", "query"],
    "list_org_users": ["page_size", "cursor"],
    "get_org_user": ["page_size", "cursor", "query"],
}


def role_record(role_id=3, name="Technician", **extra):
    record = {"Id": role_id, "Name": name, "CreatedOn": "2024-01-02T03:04:05Z", "UpdatedOn": None}
    record.update(extra)
    return record


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


async def call_directly(client_factory, tool, **kwargs):
    """Call the decorated function itself (no pydantic in front of it): for the checks the schema normally pre-empts."""
    async with client_factory() as client:
        return await tool(make_ctx(client), **kwargs)


# --------------------------------------------------------------------------
# Declarations
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_each_tool_is_declared_as_a_core_read_tool_over_its_op(name, spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == name)
    assert (spec.toolset, spec.kind, spec.ops, spec.destructive_hint) == ("core", "read", TOOLS[name], False)
    assert all(op in spec_index.ops for op in spec.ops) and not set(spec.ops) & FORBIDDEN_OPS


async def test_list_org_groups_takes_no_parameter(server):
    tool = next(t for t in await list_tools(server) if t.name == "list_org_groups")
    assert tool.inputSchema.get("properties", {}) == {}
    assert not tool.inputSchema.get("required")
    assert (tool.annotations.readOnlyHint, tool.annotations.destructiveHint) == (True, False)


async def test_list_org_users_takes_its_four_filters_and_no_paging(server):
    tool = next(t for t in await list_tools(server) if t.name == "list_org_users")
    schema = tool.inputSchema
    assert set(schema["properties"]) == {"status_ids", "group_ids", "query", "updated_since"}
    status = schema["properties"]["status_ids"]
    assert status["type"] == "array" and status["items"]["type"] == "integer"
    assert status["description"] == org_module.USER_STATUSES
    assert "given, only those statuses" in status["description"]
    assert schema["properties"]["group_ids"]["items"]["type"] == "integer"
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["properties"]["updated_since"]["type"] == "string"
    assert not set(schema.get("required") or [])
    assert (tool.annotations.readOnlyHint, tool.annotations.destructiveHint) == (True, False)


@pytest.mark.parametrize("name", sorted(TOOLS))
async def test_a_paging_or_filter_argument_the_tool_does_not_take_is_refused_before_any_http_call(
    server, mock_gorelo, name
):
    # the legacy list_org_groups sent pageSize=200 and Gorelo answered 400; a tool that takes no query has no query param
    for extra in NOT_TAKEN[name]:
        text = await call_tool_error(server, name, {extra: 5 if extra == "page_size" else "x"})
        assert extra in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_docstrings_follow_the_template(name):
    doc = next(s for s in REGISTRY.specs if s.name == name).fn.__doc__
    assert "Paging:" not in doc  # no paging for the caller
    if name in TEMPLATE_READS:  # the 1.5.0 reads state their ids and that they change nothing
        assert "Resolve ids first:" in doc and "Side effects: None (read-only)." in doc
    else:
        assert "Side effects:" not in doc


# the 1.5.0 reads that carry the full template lines
TEMPLATE_READS = {"list_org_roles", "get_org_group", "get_org_user"}


def test_the_template_reads_name_the_tool_that_resolves_their_id():
    assert "group_id -> list_org_groups" in org_module.get_org_group.__doc__
    assert "user_id -> list_org_users" in org_module.get_org_user.__doc__


def test_the_groups_docstring_says_it_is_not_paged_and_the_users_docstring_says_every_page_is_read():
    groups = flat(org_module.list_org_groups.__doc__)
    assert "not paged" in groups and "group id the ticket tools take" in groups
    users = flat(org_module.list_org_users.__doc__)
    assert "all pages are read for you" in users and "truncated is false when the list is complete" in users
    assert "technician id other tools take" in users
    assert "without status_ids Gorelo lists Pending and Active" in users
    assert "status_ids=[1, 2, 3] for every user" in users
    assert "List every user" not in users


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


async def test_list_org_users_sends_status_ids_on_every_page_and_echoes_them(server, mock_gorelo):
    mock_gorelo.on("GET", USERS, paged_responder([[user_record(1)], [user_record(2)]], total_count=2))
    result = await call_tool(server, "list_org_users", {"status_ids": [2, 3]})
    assert [r.query for r in mock_gorelo.requests] == [
        {"StatusIds": "2,3", "PageSize": "200"},
        {"StatusIds": "2,3", "PageSize": "200", "Cursor": "c1"},
    ]
    assert result["filters"] == {"status_ids": [2, 3]}
    assert result["count"] == 2 and result["total_count"] == 2
    assert (result["truncated"], result["complete_scan"], result["count_mismatch"]) == (False, True, False)


@pytest.mark.parametrize("status", [1, 2, 3])
async def test_list_org_users_accepts_each_status_of_the_scale(server, mock_gorelo, status):
    mock_gorelo.on("GET", USERS, paged_envelope([user_record(9201)], total_count=1))
    result = await call_tool(server, "list_org_users", {"status_ids": [status]})
    assert mock_gorelo.last.query == {"StatusIds": str(status), "PageSize": "200"}
    assert result["filters"] == {"status_ids": [status]}


@pytest.mark.parametrize(
    ("status_ids", "fragment"),
    [
        ([0], "status_ids[0]: expected 1 Pending, 2 Active, 3 Disabled, got 0"),
        ([4], "status_ids[0]: expected 1 Pending, 2 Active, 3 Disabled, got 4"),
        ([1, -1], "status_ids[1]: expected 1 Pending, 2 Active, 3 Disabled, got -1"),
        ([2, 9], "status_ids[1]: expected 1 Pending, 2 Active, 3 Disabled, got 9"),
        ([MAX_ID], "status_ids[0]: expected 1 Pending, 2 Active, 3 Disabled, got 9223372036854775807"),
        ([], "status_ids: expected at least one id, got an empty list"),
        ([MAX_ID + 1], "status_ids[0]: expected a positive whole number such as 123, got a number above"),
    ],
)
async def test_list_org_users_refuses_a_status_outside_the_scale_before_any_http_call(
    server, mock_gorelo, status_ids, fragment
):
    text = await call_tool_error(server, "list_org_users", {"status_ids": status_ids})
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", [True, "5", 5.0, "abc"])
async def test_list_org_users_status_ids_refuse_json_true_text_and_floats(server, mock_gorelo, bad):
    text = await call_tool_error(server, "list_org_users", {"status_ids": [bad]})
    assert "status_ids" in text and "valid integer" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    ("bad", "kind"),
    [(True, "a boolean"), (5.0, "a decimal number"), ("5", "a string"), (None, "null")],
)
async def test_list_org_users_direct_call_keeps_the_id_messages(client_factory, mock_gorelo, bad, kind):
    with pytest.raises(ToolError) as info:
        await call_directly(client_factory, org_module.list_org_users, status_ids=[bad])
    assert str(info.value) == f"status_ids[0]: expected a positive whole number such as 123, got {kind}"
    assert mock_gorelo.requests == []


async def test_list_org_users_maps_a_status_ids_rejection_to_the_snake_case_name(server, mock_gorelo):
    mock_gorelo.on("GET", USERS, error_envelope(400, [("070101", "Not valid.", "StatusIds")]))
    text = await call_tool_error(server, "list_org_users", {"status_ids": [2]})
    assert text == trace("Gorelo rejected list_org_users (HTTP 400, code 070101): status_ids: Not valid.")


def user_detail_record(user_id=9201, **extra):
    """A UserDetailModel: the list row plus RoleId and GroupIds."""
    record = user_record(user_id)
    record.update({"RoleId": 3, "GroupIds": [7201]})
    record.update(extra)
    return record


# --------------------------------------------------------------------------
# The rows the fixtures serve are the spec's schemas
# --------------------------------------------------------------------------


def test_the_fixtures_are_shaped_like_the_spec(spec_index):
    def fields(name):
        return set(spec_index.schema(name)["fields"])

    assert set(role_record()) == fields("RoleModel")
    assert set(group_record()) == fields("GetGroupsResponseModel")
    assert set(user_record()) == fields("UserModel")
    assert set(user_detail_record()) == fields("UserDetailModel")


# --------------------------------------------------------------------------
# list_org_roles
# --------------------------------------------------------------------------


async def test_list_org_roles_makes_a_plain_get_with_no_query_string(server, mock_gorelo):
    rows = [role_record(1, "Administrator"), role_record(3, "Technician")]
    mock_gorelo.on("GET", ROLES, envelope(rows))
    result = await call_tool(server, "list_org_roles")
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", ROLES, {}, b"")
    assert len(mock_gorelo.requests) == 1
    assert result == {"items": rows, "count": 2}


async def test_list_org_roles_returns_an_empty_list_as_an_empty_list(server, mock_gorelo):
    mock_gorelo.on("GET", ROLES, envelope([]))
    assert await call_tool(server, "list_org_roles") == {"items": [], "count": 0}


@pytest.mark.parametrize("data", [None, {"Items": []}, "none", True])
async def test_list_org_roles_refuses_an_answer_that_is_not_a_list(server, mock_gorelo, data):
    mock_gorelo.on("GET", ROLES, envelope(data))
    text = await call_tool_error(server, "list_org_roles")
    assert text.startswith("Gorelo returned an unexpected response for list_org_roles")
    assert "expected Data to be a list" in text


async def test_list_org_roles_maps_a_gorelo_error_to_a_tool_error(server, mock_gorelo):
    mock_gorelo.on("GET", ROLES, error_envelope(401, [("070401", "Invalid API key.")]))
    text = await call_tool_error(server, "list_org_roles")
    assert text == trace("Gorelo rejected list_org_roles (HTTP 401, code 070401): Invalid API key.")


async def test_list_org_roles_names_a_missing_scope(server, mock_gorelo):
    mock_gorelo.on("GET", ROLES, error_envelope(403, [("080203", "API key does not have 'Organization' scope")]))
    text = await call_tool_error(server, "list_org_roles")
    assert "the API key does not have the 'Organization' scope" in text


def test_the_roles_docstring_says_it_resolves_the_role_id_of_a_user():
    text = flat(org_module.list_org_roles.__doc__)
    assert "not paged" in text and "get_org_user" in text and "RoleId" in text


# --------------------------------------------------------------------------
# get_org_group
# --------------------------------------------------------------------------


async def test_get_org_group_reads_one_record_by_its_id(server, mock_gorelo):
    record = group_record(7201, "Everyone")
    mock_gorelo.on("GET", GROUP_PATH, envelope(record))
    result = await call_tool(server, "get_org_group", {"group_id": 7201})
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", GROUP_PATH, {}, b"")
    assert len(mock_gorelo.requests) == 1
    assert result == record


async def test_get_org_group_maps_a_404_to_the_group_id_param(server, mock_gorelo):
    mock_gorelo.on("GET", GROUP_PATH, error_envelope(404, [("070404", "Group not found.", "groupId")]))
    text = await call_tool_error(server, "get_org_group", {"group_id": 7201})
    assert text == trace("Gorelo rejected get_org_group (HTTP 404, code 070404): group_id: Group not found.")


@pytest.mark.parametrize("data", [None, [], "group", True])
async def test_get_org_group_refuses_an_answer_that_is_not_one_object(server, mock_gorelo, data):
    mock_gorelo.on("GET", GROUP_PATH, envelope(data))
    text = await call_tool_error(server, "get_org_group", {"group_id": 7201})
    assert text.startswith("Gorelo returned an unexpected response for get_org_group")
    assert "refusing to guess" in text


@pytest.mark.parametrize("bad", [True, "7201", 7201.0])
async def test_get_org_group_refuses_a_group_id_that_is_not_an_integer(server, mock_gorelo, bad):
    text = await call_tool_error(server, "get_org_group", {"group_id": bad})
    assert "group_id" in text and "valid integer" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    ("bad", "kind"),
    [(0, "zero or a negative number"), (-3, "zero or a negative number"), (MAX_ID + 1, "a number above")],
)
async def test_get_org_group_refuses_an_out_of_range_id_before_any_http_call(client_factory, mock_gorelo, bad, kind):
    with pytest.raises(ToolError) as info:
        await call_directly(client_factory, org_module.get_org_group, group_id=bad)
    assert str(info.value).startswith("group_id: expected a positive whole number such as 123, got ")
    assert kind in str(info.value)
    assert mock_gorelo.requests == []


def test_the_group_docstring_says_its_members_come_from_list_org_users():
    text = flat(org_module.get_org_group.__doc__)
    assert "same shape as a list_org_groups row" in text and "list_org_users with group_ids" in text


# --------------------------------------------------------------------------
# list_org_users: the new filters
# --------------------------------------------------------------------------


async def test_list_org_users_sends_group_query_and_updated_since_under_their_spec_names(server, mock_gorelo):
    mock_gorelo.on("GET", USERS, paged_envelope([user_record(9201)], total_count=1))
    result = await call_tool(
        server,
        "list_org_users",
        {"group_ids": [7201, 7202], "query": "alex", "updated_since": "2026-10-01T09:30:00-05:00"},
    )
    assert mock_gorelo.last.query == {
        "GroupIds": "7201,7202",
        "Query": "alex",
        "UpdatedSince": "2026-10-01T14:30:00Z",
        "PageSize": "200",
    }
    assert result["filters"] == {
        "group_ids": [7201, 7202],
        "query": "alex",
        "updated_since": "2026-10-01T14:30:00Z",
    }
    assert (result["count"], result["total_count"], result["complete_scan"], result["truncated"]) == (1, 1, True, False)


async def test_list_org_users_sends_the_new_filters_on_every_page(server, mock_gorelo):
    mock_gorelo.on("GET", USERS, paged_responder([[user_record(1)], [user_record(2)]], total_count=2))
    result = await call_tool(server, "list_org_users", {"group_ids": [7201], "query": "Example"})
    assert [r.query for r in mock_gorelo.requests] == [
        {"GroupIds": "7201", "Query": "Example", "PageSize": "200"},
        {"GroupIds": "7201", "Query": "Example", "PageSize": "200", "Cursor": "c1"},
    ]
    assert result["count"] == 2 and result["filters"] == {"group_ids": [7201], "query": "Example"}


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({"group_ids": []}, "group_ids: expected at least one id, got an empty list"),
        ({"group_ids": [0]}, "group_ids[0]: expected a positive whole number such as 123, got zero or a negative number"),
        ({"group_ids": [1] * 101}, "group_ids: Gorelo accepts at most 100 ids in one filter, got 101"),
        ({"query": ""}, "query: must not be empty or whitespace only"),
        ({"query": "   "}, "query: must not be empty or whitespace only"),
        ({"query": "x" * 201}, "query: at most 200 characters, got 201"),
        ({"updated_since": "2026-10-01T09:30:00"}, "updated_since: '2026-10-01T09:30:00' has no UTC offset"),
        ({"updated_since": "last week"}, "updated_since: 'last week' is not an ISO 8601 datetime"),
    ],
)
async def test_list_org_users_refuses_a_bad_new_filter_before_any_http_call(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "list_org_users", args)
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    ("property_name", "param"),
    [("GroupIds", "group_ids"), ("Query", "query"), ("UpdatedSince", "updated_since"), ("StatusIds", "status_ids")],
)
async def test_list_org_users_maps_a_gorelo_rejection_of_each_filter_to_its_param(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", USERS, error_envelope(400, [("070101", "Not valid.", property_name)]))
    args = {"status_ids": [2]} if param == "status_ids" else {"group_ids": [7201]}
    text = await call_tool_error(server, "list_org_users", args)
    assert text == trace(f"Gorelo rejected list_org_users (HTTP 400, code 070101): {param}: Not valid.")


def test_the_users_docstring_says_where_the_role_and_the_groups_are():
    text = flat(org_module.list_org_users.__doc__)
    assert "Rows lack RoleId and GroupIds: get_org_user has them." in text


# --------------------------------------------------------------------------
# get_org_user
# --------------------------------------------------------------------------


async def test_get_org_user_reads_one_user_with_its_role_and_groups(server, mock_gorelo):
    record = user_detail_record(9201)
    mock_gorelo.on("GET", USER_PATH, envelope(record))
    result = await call_tool(server, "get_org_user", {"user_id": 9201})
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", USER_PATH, {}, b"")
    assert len(mock_gorelo.requests) == 1
    assert result == record


async def test_get_org_user_returns_a_disabled_user_too(server, mock_gorelo):
    record = user_detail_record(9201, Status={"Id": 3, "Name": "Disabled"})
    mock_gorelo.on("GET", USER_PATH, envelope(record))
    result = await call_tool(server, "get_org_user", {"user_id": 9201})
    assert result["Status"] == {"Id": 3, "Name": "Disabled"}


async def test_get_org_user_maps_a_404_to_the_user_id_param(server, mock_gorelo):
    mock_gorelo.on("GET", USER_PATH, error_envelope(404, [("070404", "User not found.", "userId")]))
    text = await call_tool_error(server, "get_org_user", {"user_id": 9201})
    assert text == trace("Gorelo rejected get_org_user (HTTP 404, code 070404): user_id: User not found.")


@pytest.mark.parametrize("data", [None, [], "user", True])
async def test_get_org_user_refuses_an_answer_that_is_not_one_object(server, mock_gorelo, data):
    mock_gorelo.on("GET", USER_PATH, envelope(data))
    text = await call_tool_error(server, "get_org_user", {"user_id": 9201})
    assert text.startswith("Gorelo returned an unexpected response for get_org_user")
    assert "refusing to guess" in text


@pytest.mark.parametrize("bad", [True, "9201", 9201.0])
async def test_get_org_user_refuses_a_user_id_that_is_not_an_integer(server, mock_gorelo, bad):
    text = await call_tool_error(server, "get_org_user", {"user_id": bad})
    assert "user_id" in text and "valid integer" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    ("bad", "kind"),
    [(0, "zero or a negative number"), (-9, "zero or a negative number"), (MAX_ID + 1, "a number above")],
)
async def test_get_org_user_refuses_an_out_of_range_id_before_any_http_call(client_factory, mock_gorelo, bad, kind):
    with pytest.raises(ToolError) as info:
        await call_directly(client_factory, org_module.get_org_user, user_id=bad)
    assert str(info.value).startswith("user_id: expected a positive whole number such as 123, got ")
    assert kind in str(info.value)
    assert mock_gorelo.requests == []


def test_the_user_docstring_says_a_disabled_user_is_returned_and_where_the_names_come_from():
    text = flat(org_module.get_org_user.__doc__)
    assert "a disabled one included" in text and "list_org_roles" in text and "list_org_groups" in text


def operation(method, path):
    return json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["paths"][path][method]


def param_text(method, path, name):
    return " ".join(next(p["description"] for p in operation(method, path)["parameters"] if p["name"] == name).split())


@needs_spec_text
def test_the_user_status_scale_is_the_one_the_spec_text_documents():
    text = param_text("get", "/v1/organization/users", "StatusIds")
    assert {int(n): label for label, n in re.findall(r"([A-Z][a-z]+)=(\d)", text)} == org_module.USER_STATUS_LABELS
    assert org_module.USER_STATUS_LABELS == {1: "Pending", 2: "Active", 3: "Disabled"}
    assert "Omit for Pending and Active" in text
    assert "disabled users are listed only when 3 is named" in text
    assert "Any other id is a 400" in text


@needs_spec_text
def test_the_new_operations_say_what_the_tools_say():
    roles = " ".join(operation("get", "/v1/organization/roles")["summary"].split())
    assert "Resolves the RoleId a user carries. Not paginated." in roles
    user = " ".join(operation("get", "/v1/organization/users/{userId}")["description"].split())
    assert "A user of any status is returned, a Disabled one included. An id that is not a user of this account is a 404." in user
    group = " ".join(operation("get", "/v1/organization/groups/{groupId}")["description"].split())
    assert "An id that is not an active group of this account is a 404." in group
    assert "GET /v1/organization/users?groupIds={groupId} lists" in group


@needs_spec_text
def test_the_new_user_filters_are_the_ones_the_spec_documents():
    assert param_text("get", "/v1/organization/users", "GroupIds") == (
        "Comma-separated group ids (from GET /v1/organization/groups); keeps users who are members of any of them. Up to 100 values."
    )
    assert param_text("get", "/v1/organization/users", "Query") == (
        "Keyword matched against the user's first name, last name and email. Up to 200 characters."
    )
    assert "ISO-8601 timestamp; keeps only users updated at or after it." == param_text(
        "get", "/v1/organization/users", "UpdatedSince"
    )


def test_the_org_module_text_is_plain_ascii_without_dashes():
    for path in (Path(__file__).resolve().parent.parent / "tools" / "org.py", Path(__file__)):
        source = path.read_text(encoding="utf-8")
        assert source.isascii(), path.name
        assert EM_DASH not in source and EN_DASH not in source, path.name
