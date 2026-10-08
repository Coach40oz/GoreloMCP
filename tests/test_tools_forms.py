"""tools/forms.py: list_forms, list_form_responses and create_form_submission_link.

Offline only: an API key without the Forms scope gets a 403 (code 080203), so these tests
are the only coverage. They include the missing-scope message for every tool."""

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
    uid,
)

import tools  # noqa: F401  (importing the package registers every tool)
from gorelo_client import FORBIDDEN_OPS, GoreloAPIError
from settings import DEFAULT_TOOLSETS, TOOLSETS
from tools import _common, forms
from tools._common import MAX_ID, REGISTRY

pytestmark = pytest.mark.anyio

EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)
DESCRIPTION_MAX, PARAM_DESCRIPTION_MAX = 900, 220  # CONTRIBUTING.md: never more than these

EXPECTED_TOOLS = {
    "list_forms": ("read", ["GET /v1/forms"]),
    "list_form_responses": ("read", ["GET /v1/forms/{formId}/responses"]),
    "create_form_submission_link": ("write", ["POST /v1/forms/{formId}/submission-links"]),
}
SCOPE_NOTE = ("080203", "API key does not have 'Forms' scope")


def form(form_id="aB3dE9", title="New starter checklist", **overrides):
    """A FormListItemModel as GET /v1/forms returns it (PascalCase)."""
    record = {
        "Id": form_id,
        "Title": title,
        "Status": {"Id": 1, "Name": "Active"},
        "ResponseCount": 4,
        "AllowInPortal": True,
        "GroupIds": [7201],
        "TagIds": [12],
        "ClientIds": [],
        "CreatedOn": "2026-08-01T09:00:00Z",
        "UpdatedOn": "2026-09-30T12:00:00Z",
    }
    record.update(overrides)
    return record


def form_response(response_id="9fK2xQ", **overrides):
    """A FormResponseListItemModel: answers carry FieldId, Label and Type (the form itself is not readable)."""
    record = {
        "Id": response_id,
        "TicketId": uid(1),
        "TaskId": None,
        "SubmittedAt": "2026-10-01T10:00:00Z",
        "Answers": [
            {"FieldId": "f1", "Label": "Full name", "Type": "Short_Answer", "TextValue": "Mira Calder",
             "NumberValue": None, "OptionValues": None},
            {"FieldId": "f2", "Label": "Laptops needed", "Type": "Number", "TextValue": None,
             "NumberValue": 2.0, "OptionValues": None},
            {"FieldId": "f3", "Label": "Software", "Type": "Checkbox", "TextValue": None,
             "NumberValue": None, "OptionValues": ["Office", "VPN"]},
            {"FieldId": "f4", "Label": "Notes", "Type": "Long_Answer", "TextValue": None,
             "NumberValue": None, "OptionValues": None},
        ],
    }
    record.update(overrides)
    return record


def link(**overrides):
    record = {"Link": "https://forms.example.test/f/aB3dE9?t=abc123", "ExpiresOn": "2026-10-09T10:00:00Z"}
    record.update(overrides)
    return record


def rejected(tool, status, code, detail):
    return f"Gorelo rejected {tool} (HTTP {status}, code {code}): {detail} [trace {TEST_TRACE_ID}]"


def scope_error(tool):
    return (
        f"Gorelo rejected {tool} (HTTP 403, code 080203): the API key does not have the 'Forms' scope. "
        f"Grant it on the API key in Gorelo, then retry [trace {TEST_TRACE_ID}]"
    )


@pytest.fixture
def server(server_factory):
    return server_factory()


def module_specs():
    return {spec.name: spec for spec in REGISTRY.specs if spec.fn.__module__ == "tools.forms"}


# --------------------------------------------------------------------------
# Declarations
# --------------------------------------------------------------------------


def test_the_module_declares_exactly_the_documented_tools():
    specs = module_specs()
    assert {name: (spec.kind, spec.ops) for name, spec in specs.items()} == EXPECTED_TOOLS
    assert {spec.toolset for spec in specs.values()} == {"forms"}
    assert {spec.destructive_hint for spec in specs.values()} == {False}


def test_the_private_copies_of_the_shared_helpers_are_gone():
    for name in ("_int_ids", "_guid", "_kind_of", "_shape_error"):
        assert not hasattr(forms, name), name
    assert forms.positive_ids is _common.positive_ids and forms.guid is _common.guid
    assert forms.expect_object is _common.expect_object


def test_the_declared_ops_exist_and_the_field_maps_are_the_spec_ones(spec_index):
    for spec in module_specs().values():
        for op in spec.ops:
            assert op in spec_index.ops and op not in FORBIDDEN_OPS
    listing = spec_index.op("GET /v1/forms")
    assert listing.paged and set(forms.LIST_FIELDS.values()) == set(listing.query_params)
    responses = spec_index.op("GET /v1/forms/{formId}/responses")
    assert responses.paged and set(responses.query_params) == {"PageSize", "Cursor"}
    assert responses.path_placeholders == ("formId",)
    create = spec_index.op("POST /v1/forms/{formId}/submission-links")
    assert set(forms.LINK_BODY.values()) == set(create.body["fields"])
    assert create.path_placeholders == ("formId",)
    # the form id pattern the tools check locally is the spec's own
    assert create.path_params["formId"]["pattern"] == "^[A-Za-z0-9_-]{1,50}$"
    assert responses.path_params["formId"]["pattern"] == "^[A-Za-z0-9_-]{1,50}$"


async def test_the_forms_toolset_is_off_by_default_and_holds_exactly_these_tools(server_factory):
    assert "forms" in TOOLSETS and "forms" not in DEFAULT_TOOLSETS
    default = {t.name for t in await list_tools(server_factory(toolsets=set(DEFAULT_TOOLSETS), destructive=True))}
    assert not default & set(EXPECTED_TOOLS)
    only_forms = {t.name for t in await list_tools(server_factory(toolsets={"forms"}, destructive=True))}
    assert set(EXPECTED_TOOLS) <= only_forms
    no_deletes = {t.name for t in await list_tools(server_factory(toolsets={"forms"}, destructive=False))}
    assert set(EXPECTED_TOOLS) <= no_deletes  # nothing here is destructive: the write tool is not gated


async def test_schemas_annotations_and_descriptions(server_factory):
    listed = {t.name: t for t in await list_tools(server_factory(toolsets={"forms"}))}
    for name, (kind, _ops) in EXPECTED_TOOLS.items():
        tool = listed[name]
        for param, entry_schema in tool.inputSchema["properties"].items():
            description = entry_schema.get("description")
            assert description, f"{name}.{param} has no description"
            assert len(description) <= PARAM_DESCRIPTION_MAX, f"{name}.{param}: {len(description)} characters"
            assert EM_DASH not in description and EN_DASH not in description
        text = " ".join(tool.description.split())
        assert len(tool.description) <= DESCRIPTION_MAX, f"{name}: {len(tool.description)} characters"
        if kind != "read":
            assert "Side effects:" in text, name  # a read says nothing: its readOnlyHint says it
        assert EM_DASH not in text and EN_DASH not in text
        assert tool.annotations.readOnlyHint is (kind == "read") and tool.annotations.destructiveHint is False
    for name in ("list_forms", "list_form_responses"):
        assert "Paging: pass next_cursor back as cursor with the SAME" in " ".join(listed[name].description.split())
        assert listed[name].inputSchema["properties"]["page_size"]["default"] == 50
    assert listed["list_form_responses"].inputSchema["required"] == ["form_id"]
    assert listed["create_form_submission_link"].inputSchema["required"] == ["form_id"]
    assert not listed["list_forms"].inputSchema.get("required")  # every filter is optional
    responses = " ".join(listed["list_form_responses"].description.split())
    for fragment in ("FieldId", "Label", "question text at submission time", "No API returns the form definition"):
        assert fragment in responses, fragment
    create = " ".join(listed["create_form_submission_link"].description.split())
    for fragment in (
        "without a login",
        "give it only to the person who should",
        "(at most one)",
        "ExpiresOn",
        "seven days after issue",
        "every call issues a new, separate link",
        "did not confirm",
        "the API cannot list links",
        "ask the user before requesting another",
    ):
        assert fragment in create, fragment


# Every parameter that takes an id of another record names the tool that lists it (in the parameter text).
ID_SOURCES = {
    "list_forms": {"group_ids": "list_org_groups", "tag_ids": "list_ticket_tags", "client_ids": "list_clients"},
    "list_form_responses": {"form_id": "list_forms"},
    "create_form_submission_link": {
        "form_id": "list_forms",
        "ticket_id": "list_tickets",
        "task_id": "list_project_tasks",
    },
}


async def test_every_id_parameter_names_the_tool_that_resolves_it(server_factory):
    listed = {t.name: t for t in await list_tools(server_factory(toolsets={"forms"}))}
    for tool, params in ID_SOURCES.items():
        properties = listed[tool].inputSchema["properties"]
        for param, source in params.items():
            assert source in properties[param]["description"], f"{tool}.{param} does not name {source}"


async def test_the_task_id_says_the_tool_that_lists_tasks_needs_the_projects_toolset(server_factory):
    # a forms-only server has no list_project_tasks, so the text says where it comes from and what it needs
    listed = {t.name: t for t in await list_tools(server_factory(toolsets={"forms"}))}
    assert "list_project_tasks" not in listed  # the tool the text points at is not there on this server
    text = listed["create_form_submission_link"].inputSchema["properties"]["task_id"]["description"]
    assert text == "Active task GUID (list_project_tasks, projects toolset) in an active project."


# --------------------------------------------------------------------------
# list_forms
# --------------------------------------------------------------------------


async def test_list_forms_sends_every_filter_under_its_spec_name(server, mock_gorelo, spec_index):
    mock_gorelo.on("GET", "/v1/forms", paged_envelope([form("aB3dE9"), form("zZ9yX8")], next_cursor="c2", total_count=5))
    result = await call_tool(
        server,
        "list_forms",
        {
            "status_ids": [1, 2],
            "group_ids": [7201, 7202],
            "tag_ids": [12],
            "client_ids": [9101],
            "allow_in_portal": True,
            "query": "starter",
            "sort_order": "asc",
            "page_size": 20,
            "cursor": "abc",
        },
    )
    request = mock_gorelo.last
    assert (request.method, request.path, request.json) == ("GET", "/v1/forms", None)
    assert request.query == {
        "StatusIds": "1,2",
        "GroupIds": "7201,7202",
        "TagIds": "12",
        "ClientIds": "9101",
        "AllowInPortal": "true",
        "Query": "starter",
        "SortOrder": "asc",
        "PageSize": "20",
        "Cursor": "abc",
    }
    assert set(request.query) == set(spec_index.op("GET /v1/forms").query_params)
    assert result == {
        "items": [form("aB3dE9"), form("zZ9yX8")],
        "count": 2,
        "total_count": 5,
        "has_more": True,
        "next_cursor": "c2",
        "page_size": 20,
        "filters": {
            "status_ids": [1, 2],
            "group_ids": [7201, 7202],
            "tag_ids": [12],
            "client_ids": [9101],
            "allow_in_portal": True,
            "query": "starter",
            "sort_order": "asc",
        },
    }


async def test_list_forms_without_filters_sends_only_the_page_size(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/forms", paged_envelope([form()]))
    result = await call_tool(server, "list_forms")
    assert mock_gorelo.last.query == {"PageSize": "50"}
    assert result["filters"] == {} and result["page_size"] == 50 and result["items"] == [form()]


async def test_allow_in_portal_false_is_sent_and_reported_not_dropped(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/forms", paged_envelope([]))
    result = await call_tool(server, "list_forms", {"allow_in_portal": False})
    assert mock_gorelo.last.query == {"AllowInPortal": "false", "PageSize": "50"}
    assert result["filters"] == {"allow_in_portal": False}


@pytest.mark.parametrize("order", ["asc", "desc"])
async def test_both_sort_orders_are_sent_as_given(server, mock_gorelo, order):
    mock_gorelo.on("GET", "/v1/forms", paged_envelope([]))
    await call_tool(server, "list_forms", {"sort_order": order})
    assert mock_gorelo.last.query["SortOrder"] == order


@pytest.mark.parametrize("asked, used", [(0, 1), (-1, 1), (1, 1), (50, 50), (200, 200), (250, 200)])
async def test_list_forms_clamps_the_page_size_because_this_op_rejects_out_of_range_values(server, mock_gorelo, asked, used):
    mock_gorelo.on("GET", "/v1/forms", paged_envelope([]))
    result = await call_tool(server, "list_forms", {"page_size": asked})
    assert mock_gorelo.last.query["PageSize"] == str(used) and result["page_size"] == used


async def test_list_forms_pages_with_the_same_filters(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/forms", paged_responder([[form("aaaaaa")], [form("bbbbbb")]], total_count=2))
    filters = {"query": "starter", "page_size": 1}
    first = await call_tool(server, "list_forms", filters)
    assert first["has_more"] is True and first["next_cursor"] == "c1"
    second = await call_tool(server, "list_forms", {**filters, "cursor": first["next_cursor"]})
    assert second["has_more"] is False and second["next_cursor"] is None and second["items"] == [form("bbbbbb")]
    assert mock_gorelo.requests[1].query == {"Query": "starter", "PageSize": "1", "Cursor": "c1"}
    assert first["filters"] == second["filters"] == {"query": "starter"}


async def test_list_forms_reports_an_empty_page_explicitly(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/forms", paged_envelope([]))
    result = await call_tool(server, "list_forms", {"query": "nothing like this"})
    assert result["items"] == [] and result["count"] == 0 and result["total_count"] == 0


@pytest.mark.parametrize(
    "property_name, param",
    [
        ("StatusIds", "status_ids"),
        ("GroupIds", "group_ids"),
        ("TagIds", "tag_ids"),
        ("ClientIds", "client_ids"),
        ("AllowInPortal", "allow_in_portal"),
        ("Query", "query"),
        ("SortOrder", "sort_order"),
        ("PageSize", "page_size"),
        ("Cursor", "cursor"),
    ],
)
async def test_list_forms_maps_a_gorelo_400_to_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", "/v1/forms", error_envelope(400, [("070101", "Not valid.", property_name)]))
    text = await call_tool_error(server, "list_forms", {})
    assert text == rejected("list_forms", 400, "070101", f"{param}: Not valid.")


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({"status_ids": []}, "status_ids: expected at least one id, got an empty list"),
        ({"status_ids": [3]}, "status_ids: Gorelo form statuses are 1 (Active), 2 (Archived), got 3"),
        ({"status_ids": [1, 0]}, "status_ids[1]: expected a positive whole number such as 123, got zero or a negative number"),
        ({"status_ids": [MAX_ID + 1]}, "status_ids[0]: expected a positive whole number such as 123, got a number above"),
        ({"group_ids": []}, "group_ids: expected at least one id, got an empty list"),
        ({"group_ids": [0]}, "group_ids[0]: expected a positive whole number such as 123, got zero or a negative number"),
        ({"tag_ids": [-2]}, "tag_ids[0]: expected a positive whole number"),
        ({"tag_ids": [MAX_ID + 1]}, "tag_ids[0]: expected a positive whole number"),
        ({"client_ids": [0]}, "client_ids[0]: expected a positive whole number"),
        ({"client_ids": [9101, MAX_ID + 1]}, "client_ids[1]: expected a positive whole number"),
        ({"client_ids": ["abc"]}, "client_ids"),
        ({"query": ""}, "query: must not be empty or whitespace only"),
        ({"query": "   "}, "query: must not be empty or whitespace only"),
        ({"sort_order": "sideways"}, "sort_order"),
        ({"sort_order": "DESC"}, "sort_order"),
        ({"cursor": " "}, "cursor: must not be empty or whitespace only"),
        ({"page_size": "x"}, "page_size"),
        ({"allow_in_portal": "maybe"}, "allow_in_portal"),
        ({"form_id": "aB3dE9"}, "form_id"),
    ],
)
async def test_list_forms_rejects_bad_input_before_any_http_call(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "list_forms", args)
    assert fragment in text
    assert mock_gorelo.requests == []


# (tool, the id parameter under test)
STRICT_ID_CASES = [
    pytest.param("list_forms", "status_ids", id="list_forms.status_ids"),
    pytest.param("list_forms", "group_ids", id="list_forms.group_ids"),
    pytest.param("list_forms", "tag_ids", id="list_forms.tag_ids"),
    pytest.param("list_forms", "client_ids", id="list_forms.client_ids"),
]


@pytest.mark.parametrize("bad", [True, False, "5", 5.0, "abc"])
@pytest.mark.parametrize("tool, param", STRICT_ID_CASES)
async def test_every_id_list_refuses_json_true_text_and_floats(server, mock_gorelo, tool, param, bad):
    text = await call_tool_error(server, tool, {param: [bad]})
    assert param in text and "valid integer" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("tool, param", STRICT_ID_CASES)
async def test_an_id_list_that_is_not_a_list_is_refused(server, mock_gorelo, tool, param):
    text = await call_tool_error(server, tool, {param: 1})
    assert param in text and "valid list" in text
    assert mock_gorelo.requests == []


async def test_list_forms_missing_scope_gets_the_missing_scope_message(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/forms", error_envelope(403, [SCOPE_NOTE]))
    text = await call_tool_error(server, "list_forms")
    assert text == scope_error("list_forms")
    assert len(mock_gorelo.requests) == 1


async def test_list_forms_refuses_an_answer_that_is_not_a_page(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/forms", envelope({"Id": "aB3dE9"}))
    text = await call_tool_error(server, "list_forms")
    assert text.startswith("Gorelo returned an unexpected response for list_forms") and "expected Data to be a list" in text


# --------------------------------------------------------------------------
# list_form_responses
# --------------------------------------------------------------------------


async def test_list_form_responses_reads_one_forms_responses_with_the_answers_untouched(server, mock_gorelo):
    rows = [form_response("r1"), form_response("r2", TicketId=None, TaskId=uid(2))]
    mock_gorelo.on("GET", "/v1/forms/aB3dE9/responses", paged_envelope(rows, total_count=2))
    result = await call_tool(server, "list_form_responses", {"form_id": "aB3dE9"})
    request = mock_gorelo.last
    assert (request.method, request.path, request.json) == ("GET", "/v1/forms/aB3dE9/responses", None)
    assert request.query == {"PageSize": "50"}
    assert result == {
        "items": rows,
        "count": 2,
        "total_count": 2,
        "has_more": False,
        "next_cursor": None,
        "page_size": 50,
        "filters": {"form_id": "aB3dE9"},
    }
    assert result["items"][0]["Answers"][0]["Label"] == "Full name"
    assert result["items"][0]["Answers"][2]["OptionValues"] == ["Office", "VPN"]
    assert result["items"][0]["Answers"][3]["TextValue"] is None  # a blank answer stays blank
    assert len(mock_gorelo.requests) == 1


async def test_list_form_responses_sends_the_paging_names_only(server, mock_gorelo, spec_index):
    mock_gorelo.on("GET", "/v1/forms/aB3dE9/responses", paged_envelope([form_response()], next_cursor="n2", total_count=9))
    result = await call_tool(server, "list_form_responses", {"form_id": "aB3dE9", "page_size": 5, "cursor": "abc"})
    assert mock_gorelo.last.query == {"PageSize": "5", "Cursor": "abc"}
    assert set(mock_gorelo.last.query) == set(spec_index.op("GET /v1/forms/{formId}/responses").query_params)
    assert result["has_more"] is True and result["next_cursor"] == "n2" and result["page_size"] == 5


@pytest.mark.parametrize("asked, used", [(0, 1), (1, 1), (200, 200), (999, 200)])
async def test_list_form_responses_clamps_the_page_size(server, mock_gorelo, asked, used):
    mock_gorelo.on("GET", "/v1/forms/aB3dE9/responses", paged_envelope([]))
    result = await call_tool(server, "list_form_responses", {"form_id": "aB3dE9", "page_size": asked})
    assert mock_gorelo.last.query["PageSize"] == str(used) and result["page_size"] == used


async def test_list_form_responses_pages_with_the_same_form(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/forms/aB3dE9/responses", paged_responder([[form_response("r1")], [form_response("r2")]]))
    first = await call_tool(server, "list_form_responses", {"form_id": "aB3dE9", "page_size": 1})
    second = await call_tool(
        server, "list_form_responses", {"form_id": "aB3dE9", "page_size": 1, "cursor": first["next_cursor"]}
    )
    assert [r["Id"] for r in first["items"] + second["items"]] == ["r1", "r2"]
    assert second["has_more"] is False
    assert mock_gorelo.requests[1].query == {"PageSize": "1", "Cursor": "c1"}
    assert {r.path for r in mock_gorelo.requests} == {"/v1/forms/aB3dE9/responses"}


@pytest.mark.parametrize("form_id", ["a", "aB3dE9", "a_b-C9", "x" * 50, "0123456789"])
async def test_form_ids_that_match_the_spec_pattern_are_accepted(server, mock_gorelo, form_id):
    mock_gorelo.on("GET", f"/v1/forms/{form_id}/responses", paged_envelope([]))
    result = await call_tool(server, "list_form_responses", {"form_id": form_id})
    assert result["filters"] == {"form_id": form_id}
    assert mock_gorelo.last.path == f"/v1/forms/{form_id}/responses"


@pytest.mark.parametrize(
    "form_id", ["", " ", "a b", "x" * 51, "a/b", "a\\b", "..", "a?b", "a#b", "a%2Fb", "form\u00e9", "ab.cd", " aB3dE9"]
)
async def test_form_ids_outside_the_spec_pattern_are_rejected_by_name_before_any_http_call(server, mock_gorelo, form_id):
    text = await call_tool_error(server, "list_form_responses", {"form_id": form_id})
    assert text.startswith("form_id: expected a form Id from list_forms (1 to 50 letters, digits, '_' or '-')")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "form_id, what", [("s3cret value", "text that is not a form Id"), ("", "an empty string")]
)
async def test_a_bad_form_id_is_described_and_never_quoted(server, mock_gorelo, form_id, what):
    text = await call_tool_error(server, "list_form_responses", {"form_id": form_id})
    assert text == f"form_id: expected a form Id from list_forms (1 to 50 letters, digits, '_' or '-'), got {what}"
    assert "s3cret" not in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "notification, detail",
    [
        (("070404", "Form not found."), "Form not found."),
        (("070404", "Form not found.", "formId"), "form_id: Form not found."),
    ],
)
async def test_list_form_responses_reports_a_404(server, mock_gorelo, notification, detail):
    mock_gorelo.on("GET", "/v1/forms/zzzzzz/responses", error_envelope(404, [notification]))
    text = await call_tool_error(server, "list_form_responses", {"form_id": "zzzzzz"})
    assert text == rejected("list_form_responses", 404, "070404", detail)


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({}, "form_id"),
        ({"form_id": "aB3dE9", "cursor": ""}, "cursor: must not be empty or whitespace only"),
        ({"form_id": "aB3dE9", "page_size": "x"}, "page_size"),
        ({"form_id": "aB3dE9", "status_ids": [1]}, "status_ids"),
    ],
)
async def test_list_form_responses_rejects_bad_input_before_any_http_call(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "list_form_responses", args)
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_list_form_responses_missing_scope_gets_the_missing_scope_message(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/forms/aB3dE9/responses", error_envelope(403, [SCOPE_NOTE]))
    text = await call_tool_error(server, "list_form_responses", {"form_id": "aB3dE9"})
    assert text == scope_error("list_form_responses")


async def test_list_form_responses_maps_a_paging_400_to_the_snake_case_parameter(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/forms/aB3dE9/responses", error_envelope(400, [("070101", "Out of range.", "PageSize")]))
    text = await call_tool_error(server, "list_form_responses", {"form_id": "aB3dE9"})
    assert text == rejected("list_form_responses", 400, "070101", "page_size: Out of range.")


# --------------------------------------------------------------------------
# create_form_submission_link
# --------------------------------------------------------------------------

LINK_PATH = "/v1/forms/aB3dE9/submission-links"


async def test_a_link_for_the_form_alone_sends_an_empty_object_and_returns_gorelos_answer(server, mock_gorelo):
    mock_gorelo.on("POST", LINK_PATH, envelope(link()))
    result = await call_tool(server, "create_form_submission_link", {"form_id": "aB3dE9"})
    assert result == link()
    request = mock_gorelo.last
    assert (request.method, request.path, request.query) == ("POST", LINK_PATH, {})
    assert request.json == {}
    assert len(mock_gorelo.requests) == 1  # no re-read exists for a link


async def test_a_link_for_a_ticket_sends_only_ticket_id(server, mock_gorelo):
    mock_gorelo.on("POST", LINK_PATH, envelope(link()))
    await call_tool(server, "create_form_submission_link", {"form_id": "aB3dE9", "ticket_id": uid(5).upper()})
    assert mock_gorelo.last.json == {"TicketId": uid(5)}


async def test_a_link_for_a_task_sends_only_task_id(server, mock_gorelo):
    mock_gorelo.on("POST", LINK_PATH, envelope(link()))
    await call_tool(server, "create_form_submission_link", {"form_id": "aB3dE9", "task_id": uid(6)})
    assert mock_gorelo.last.json == {"TaskId": uid(6)}


async def test_a_bare_32_digit_guid_is_sent_in_canonical_form(server, mock_gorelo):
    mock_gorelo.on("POST", LINK_PATH, envelope(link()))
    await call_tool(server, "create_form_submission_link", {"form_id": "aB3dE9", "ticket_id": uid(5).replace("-", "")})
    assert mock_gorelo.last.json == {"TicketId": uid(5)}


async def test_the_link_body_never_carries_anything_the_caller_did_not_give(server, mock_gorelo, spec_index):
    mock_gorelo.on("POST", LINK_PATH, envelope(link()))
    await call_tool(server, "create_form_submission_link", {"form_id": "aB3dE9", "ticket_id": uid(5)})
    assert set(mock_gorelo.last.json) <= set(spec_index.op("POST /v1/forms/{formId}/submission-links").body["fields"])
    assert "TaskId" not in mock_gorelo.last.json and "formId" not in mock_gorelo.last.json


async def test_both_a_ticket_and_a_task_is_a_local_error(server, mock_gorelo):
    text = await call_tool_error(
        server, "create_form_submission_link", {"form_id": "aB3dE9", "ticket_id": uid(5), "task_id": uid(6)}
    )
    assert text == (
        "ticket_id and task_id: give at most one of them (the submission is filed against one ticket or one "
        "task), got both"
    )
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({"form_id": "a b"}, "form_id: expected a form Id from list_forms"),
        ({"form_id": ""}, "form_id: expected a form Id from list_forms"),
        ({"form_id": "x" * 51}, "form_id: expected a form Id from list_forms"),
        ({"form_id": "a/../b"}, "form_id: expected a form Id from list_forms"),
        ({"form_id": "aB3dE9", "ticket_id": "TCK-2029"}, "ticket_id: expected a GUID"),
        ({"form_id": "aB3dE9", "ticket_id": ""}, "ticket_id: expected a GUID"),
        ({"form_id": "aB3dE9", "task_id": "12345"}, "task_id: expected a GUID"),
        ({"form_id": "aB3dE9", "task_id": uid(1)[:-1]}, "task_id: expected a GUID"),
        ({}, "form_id"),
        ({"form_id": "aB3dE9", "confirm": True}, "confirm"),
        ({"form_id": "aB3dE9", "expires_in_days": 30}, "expires_in_days"),
    ],
)
async def test_create_form_submission_link_rejects_bad_input_before_any_http_call(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "create_form_submission_link", args)
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "notification, detail",
    [
        (("070101", "The ticket is not active.", "TicketId"), "ticket_id: The ticket is not active."),
        (("070101", "The task is not active.", "TaskId"), "task_id: The task is not active."),
        (("070101", "Send at most one of TicketId and TaskId."), "Send at most one of TicketId and TaskId."),
    ],
)
async def test_a_gorelo_400_names_the_snake_case_parameter(server, mock_gorelo, notification, detail):
    mock_gorelo.on("POST", LINK_PATH, error_envelope(400, [notification]))
    text = await call_tool_error(server, "create_form_submission_link", {"form_id": "aB3dE9", "ticket_id": uid(5)})
    assert text == rejected("create_form_submission_link", 400, "070101", detail)


@pytest.mark.parametrize(
    "notification, detail",
    [
        (("070404", "Form not found."), "Form not found."),
        (("070404", "Form not found.", "formId"), "form_id: Form not found."),
        (("070404", "The form is archived."), "The form is archived."),
    ],
)
async def test_an_archived_or_unknown_form_is_reported_as_gorelos_404(server, mock_gorelo, notification, detail):
    mock_gorelo.on("POST", LINK_PATH, error_envelope(404, [notification]))
    text = await call_tool_error(server, "create_form_submission_link", {"form_id": "aB3dE9"})
    assert text == rejected("create_form_submission_link", 404, "070404", detail)


async def test_create_form_submission_link_missing_scope_gets_the_missing_scope_message(server, mock_gorelo):
    mock_gorelo.on("POST", LINK_PATH, error_envelope(403, [SCOPE_NOTE]))
    text = await call_tool_error(server, "create_form_submission_link", {"form_id": "aB3dE9"})
    assert text == scope_error("create_form_submission_link")
    assert len(mock_gorelo.requests) == 1  # a refusal is not retried


# when Gorelo may have issued the link anyway, the API cannot list links, so a read cannot settle it.
UNCONFIRMED_LINK = (
    "A submission link may already have been issued and the API cannot list links, so do not request another "
    "one without asking the user first."
)


@pytest.mark.parametrize(
    "response, what, trace",
    [
        (httpx.ReadTimeout("slow"), "the request timed out", False),
        (httpx.ConnectError("refused"), "the connection failed", False),
        (error_envelope(500, [("070001", "Internal error.")]), "Gorelo answered HTTP 500 without a usable link", True),
        (
            error_envelope(503, [("070001", "Unavailable.")], trace_id=None),
            "Gorelo answered HTTP 503 without a usable link",
            False,
        ),
        (  # a gateway page instead of an envelope
            httpx.Response(502, text="<html>Bad gateway</html>", headers={"content-type": "text/html"}),
            "Gorelo answered HTTP 502 without a usable link",
            False,
        ),
    ],
)
async def test_an_unconfirmed_link_request_says_a_link_may_exist_and_not_to_ask_for_another(
    server, mock_gorelo, response, what, trace
):
    mock_gorelo.on("POST", LINK_PATH, response)
    text = await call_tool_error(server, "create_form_submission_link", {"form_id": "aB3dE9", "ticket_id": uid(5)})
    assert text == (
        f"Gorelo did not confirm create_form_submission_link ({what}). {UNCONFIRMED_LINK}"
        + (f" [trace {TEST_TRACE_ID}]" if trace else "")
    )
    assert "Verify with a read" not in text  # a read cannot settle it
    assert [r.method for r in mock_gorelo.requests] == ["POST"]  # the tool never asks for a second link itself


@pytest.mark.parametrize(
    "answer",
    [
        envelope({"ExpiresOn": "2026-10-09T10:00:00Z"}),
        envelope({"Link": "  ", "ExpiresOn": "2026-10-09T10:00:00Z"}),
        envelope({"Link": None}),
        envelope({"Link": 5}),
        envelope({}),
        envelope(None),
        envelope(True),
        envelope(False),
        envelope([link()]),
        envelope("https://forms.example.test/f/aB3dE9"),
    ],
)
async def test_an_answer_without_a_usable_link_gets_the_same_unconfirmed_link_error(server, mock_gorelo, answer):
    mock_gorelo.on("POST", LINK_PATH, answer)
    text = await call_tool_error(server, "create_form_submission_link", {"form_id": "aB3dE9"})
    assert text == (
        "Gorelo did not confirm create_form_submission_link (Gorelo answered HTTP 200 without a usable link). "
        + UNCONFIRMED_LINK
    )
    assert text.count("did not confirm") == 1
    assert [r.method for r in mock_gorelo.requests] == ["POST"]


@pytest.mark.parametrize(
    "status, notification",
    [(400, ("070101", "The ticket is not active.", "TicketId")), (404, ("070404", "Form not found.", "formId"))],
)
async def test_a_refusal_is_not_an_unconfirmed_link(server, mock_gorelo, status, notification):
    # a 4xx says Gorelo did nothing: the plain rejection stays, with no "may already have been issued"
    mock_gorelo.on("POST", LINK_PATH, error_envelope(status, [notification]))
    text = await call_tool_error(server, "create_form_submission_link", {"form_id": "aB3dE9", "ticket_id": uid(5)})
    assert text.startswith("Gorelo rejected create_form_submission_link") and "may already have been issued" not in text


def test_the_unconfirmed_link_text_covers_an_error_without_a_status():
    bare = GoreloAPIError("x", op_key=forms.LINK_OP, kind="shape", write_unconfirmed=True)
    assert str(forms._link_unconfirmed(bare)) == (
        f"Gorelo did not confirm create_form_submission_link (Gorelo's answer had no usable link). {UNCONFIRMED_LINK}"
    )


async def test_each_call_issues_its_own_link(server, mock_gorelo):
    mock_gorelo.on("POST", LINK_PATH, envelope(link()))
    await call_tool(server, "create_form_submission_link", {"form_id": "aB3dE9"})
    await call_tool(server, "create_form_submission_link", {"form_id": "aB3dE9"})
    assert [r.method for r in mock_gorelo.requests] == ["POST", "POST"]


# --------------------------------------------------------------------------
# Whole-module guard
# --------------------------------------------------------------------------


async def test_each_tool_sends_exactly_the_operations_it_declares(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/forms", paged_envelope([form()]))
    mock_gorelo.on("GET", "/v1/forms/aB3dE9/responses", paged_envelope([form_response()]))
    mock_gorelo.on("POST", LINK_PATH, envelope(link()))
    specs = module_specs()
    for name, args in (
        ("list_forms", {}),
        ("list_form_responses", {"form_id": "aB3dE9"}),
        ("create_form_submission_link", {"form_id": "aB3dE9"}),
    ):
        mock_gorelo.reset()
        await call_tool(server, name, args)
        sent = {f"{r.method} {r.path}" for r in mock_gorelo.requests}
        assert sent == {op.replace("{formId}", "aB3dE9") for op in specs[name].ops}, name
