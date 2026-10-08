"""tools/project_tasks.py: project tasks, task conversations and task approvals. Offline only.

An API key without the Project scope gets a 403 (code 080203), so every behaviour here is checked
against MockGorelo with realistic PascalCase envelopes: exact method, path, query names and body, result
shapes, Gorelo errors mapped to the snake_case param, each local validation error with zero HTTP calls,
and the destructive gate.
"""

import json
import logging
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
    uid,
)
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool

import tools.project_tasks as module
from gorelo_client import is_forbidden_op
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio

# spec/spec_index.json carries no description text, so the enumerations the tools offer (sort values, status
# ids, the priority scale) are pinned to the operation text of the full OpenAPI snapshot.
LATEST_SPEC = Path(__file__).resolve().parent.parent / "backups" / "swagger-latest.json"
needs_spec_text = pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")

PROJECT, TASK, SECTION, CONVERSATION, APPROVAL = uid(1), uid(2), uid(3), uid(5), uid(6)
BLOCKER, ASSET, CUSTOM_ASSET, UPTIME = uid(7), uid(8), uid(9), uid(10)
SCOPE_NOTE = ("080203", "API key does not have 'Project' scope")
# What created_id() and expect_object() say about a write whose answer cannot be used.
UNUSABLE_ID = "Gorelo reported success but the answer carries no usable Id for the record"
VERIFY = "the write may have been applied, so verify it with a read before repeating it"

TASKS = "/v1/projects/{projectId}/tasks"
TASK_PATH = f"{TASKS}/{{taskId}}"
LIST_TASKS = f"GET {TASKS}"
GET_TASK = f"GET {TASK_PATH}"
CREATE_TASK = f"POST {TASKS}"
UPDATE_TASK = f"PATCH {TASK_PATH}"
DELETE_TASK = f"DELETE {TASK_PATH}"
CONVERSATIONS = f"{TASK_PATH}/conversations"
APPROVAL_PATH = f"{TASK_PATH}/approvals/{{approvalId}}"

PROJECT_URL = f"/v1/projects/{PROJECT}"
TASKS_URL = f"{PROJECT_URL}/tasks"
TASK_URL = f"{TASKS_URL}/{TASK}"


@pytest.fixture(autouse=True)
def quiet_fastmcp_error_logs():
    """FastMCP logs every tool error with a rich traceback (about 40 ms each). These tests read the error
    text the model would see instead, so the logger is silenced for the test and restored after it."""
    logger = logging.getLogger("fastmcp")
    previous = logger.level
    logger.setLevel(logging.CRITICAL)
    yield
    logger.setLevel(previous)


def sent(mock):
    return [(r.method, r.path) for r in mock.requests]


async def call_directly(client_factory, tool, **kwargs):
    """Call the decorated function itself (no pydantic in front of it): for the checks the schema normally pre-empts."""
    async with client_factory() as client:
        return await tool(make_ctx(client), **kwargs)


@pytest.fixture
def server(server_factory):
    return server_factory(toolsets={"projects"})


@pytest.fixture
def dserver(server_factory):
    return server_factory(toolsets={"projects"}, destructive=True)


def task_row(**changes):
    record = {
        "Id": TASK, "Title": "Rack the switch", "Number": 7, "DisplayNumber": "PRO-462-7", "SectionId": SECTION,
        "LeadAssigneeId": 9201, "AssistingAssigneeIds": [], "WatcherIds": [], "PrimaryGroupId": 7201,
        "Status": {"Id": 1, "Name": "NotStarted"}, "StatusUpdatedOn": None, "StatusReason": "",
        "Priority": {"Id": 3, "Name": "Normal"}, "DueDate": None, "IsUnread": False, "IsWaitingOnThem": False,
        "ChecklistSummary": {"Total": 0, "Completed": 0}, "BlockedByTaskIds": [], "BlockingTaskIds": [],
        "LastUpdate": None, "CreatedOn": "2026-10-01T14:30:00Z", "UpdatedOn": None, "ClosedOn": None,
    }
    record.update(changes)
    return record


def task_detail(**changes):
    record = task_row(
        ProjectId=PROJECT, ClientId=9101, StatusDetails=None, AgentAssetIds=[], CustomAssetIds=[], UptimeIds=[],
        Time=None, Products=None, BillingOverride=None, Shipments=[], Banner=None,
    )
    record.update(changes)
    return record


def approval_detail(**changes):
    record = {
        "Id": APPROVAL, "Type": {"Id": 4, "Name": "Approval"}, "Name": "Approve the downtime",
        "Status": {"Id": 1, "Name": "Pending"},
        "Approvers": [{"ContactId": 9103, "Status": {"Id": 1, "Name": "Pending"}}],
        "CreatedOn": "2026-10-01T15:00:00Z",
    }
    record.update(changes)
    return record


VALID = {
    "list_project_tasks": {"project_id": PROJECT},
    "get_project_task": {"project_id": PROJECT, "task_id": TASK},
    "create_project_task": {"project_id": PROJECT, "title": "Rack the switch", "section_id": SECTION},
    "update_project_task": {"project_id": PROJECT, "task_id": TASK, "title": "New title"},
    "delete_project_task": {"project_id": PROJECT, "task_id": TASK, "confirm": True},
    "list_task_conversations": {"project_id": PROJECT, "task_id": TASK},
    "create_task_side_conversation": {
        "project_id": PROJECT, "task_id": TASK, "name": "Vendor", "email": "vendor@example.com",
    },
    "create_task_approval": {"project_id": PROJECT, "task_id": TASK, "name": "Approve downtime", "contact_ids": [9103]},
    "get_task_approval": {"project_id": PROJECT, "task_id": TASK, "approval_id": APPROVAL},
}

# name -> (kind, ops exactly as the module lists them, destructiveHint)
EXPECTED = {
    "list_project_tasks": ("read", [LIST_TASKS], False),
    "get_project_task": ("read", [GET_TASK], False),
    "create_project_task": ("write", [CREATE_TASK, GET_TASK], False),
    "update_project_task": ("write", [UPDATE_TASK, GET_TASK], True),
    "delete_project_task": ("destructive", [DELETE_TASK], True),
    "list_task_conversations": ("read", [f"GET {CONVERSATIONS}"], False),
    "create_task_side_conversation": ("write", [f"POST {CONVERSATIONS}/side-conversation"], False),
    "create_task_approval": ("write", [f"POST {CONVERSATIONS}/approval", f"GET {APPROVAL_PATH}"], False),
    "get_task_approval": ("read", [f"GET {APPROVAL_PATH}"], False),
}


def specs():
    return {s.name: s for s in REGISTRY.specs if s.fn.__module__ == module.__name__}


# --------------------------------------------------------------------------
# Declarations and spec coverage
# --------------------------------------------------------------------------


def test_the_module_declares_exactly_the_tools_declared():
    found = specs()
    assert set(found) == set(EXPECTED)
    for name, (kind, ops, hint) in EXPECTED.items():
        spec = found[name]
        assert (spec.toolset, spec.kind, spec.ops, spec.destructive_hint) == ("projects", kind, ops, hint), name


def test_the_valid_call_table_covers_every_tool():
    assert set(VALID) == set(EXPECTED)


def test_every_declared_op_exists_in_the_spec_and_is_allowed(spec_index):
    for spec in specs().values():
        for op in spec.ops:
            assert op in spec_index.ops, f"{spec.name}: {op}"
            assert not is_forbidden_op(op)  # by shape, never by the exact text of the key


def spec_names(spec_index, ops):
    body, query = set(), set()
    for op in ops:
        entry = spec_index.ops[op]
        body |= set((entry.body or {}).get("fields") or {})
        query |= set(entry.query_params)
    return body, query


def test_every_field_map_target_is_a_real_body_field_or_query_name(spec_index):
    for spec in specs().values():
        body, query = spec_names(spec_index, spec.ops)
        for param, target in spec.field_map.items():
            assert target in body | query, f"{spec.name}: {param} -> {target} is not in the spec for {spec.ops}"


@pytest.mark.parametrize(
    "tool, op",
    [
        ("create_project_task", CREATE_TASK),
        ("update_project_task", UPDATE_TASK),
        ("create_task_side_conversation", f"POST {CONVERSATIONS}/side-conversation"),
        ("create_task_approval", f"POST {CONVERSATIONS}/approval"),
    ],
)
def test_every_body_field_of_the_spec_has_a_param(spec_index, tool, op):
    assert set(spec_index.ops[op].body["fields"]) == set(specs()[tool].field_map.values())


def required_params(name):
    return Tool.from_function(specs()[name].fn).parameters.get("required", [])


@pytest.mark.parametrize(
    "tool, op",
    [
        ("create_project_task", CREATE_TASK),
        ("create_task_side_conversation", f"POST {CONVERSATIONS}/side-conversation"),
        ("create_task_approval", f"POST {CONVERSATIONS}/approval"),
    ],
)
def test_every_field_the_spec_requires_is_a_required_param_of_the_create_tool(spec_index, tool, op):
    # No invented defaults: a field the spec requires is a required param. (An approval also requires contact_ids
    # here, because an approval needs approvers, although the spec marks only Name as required.)
    field_map = specs()[tool].field_map
    required_fields = {field_map[param] for param in required_params(tool) if param in field_map}
    assert set(spec_index.ops[op].body["required"]) <= required_fields


def test_create_project_task_requires_the_project_the_title_and_the_section():
    # 2026-10-02: CreateTaskCommand requires SectionId as well as Title (the project id is the path).
    assert required_params("create_project_task") == ["project_id", "title", "section_id"]


def test_every_query_name_of_the_task_list_has_a_param(spec_index):
    assert set(spec_index.ops[LIST_TASKS].query_params) == set(specs()["list_project_tasks"].field_map.values())


def operation(method, path):
    return json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["paths"][path][method]


def spec_text(method, path):
    return " ".join(operation(method, path)["description"].split())


def param_text(method, path, name):
    return " ".join(next(p["description"] for p in operation(method, path)["parameters"] if p["name"] == name).split())


def offered_values(tool, param):
    schema = Tool.from_function(specs()[tool].fn).parameters["properties"][param]
    values = set()
    for branch in schema.get("anyOf", [schema]):
        values |= set(branch.get("enum", [])) | set(branch.get("items", {}).get("enum", []))
    return values


def param_description(tool, param):
    return " ".join(Tool.from_function(specs()[tool].fn).parameters["properties"][param]["description"].split())


def test_the_task_sort_values_are_the_ones_the_documentation_names():
    assert offered_values("list_project_tasks", "sort_by") == {"boardOrder", "createdOn", "updatedOn"}
    assert offered_values("list_project_tasks", "sort_order") == {"asc", "desc"}


@needs_spec_text
@pytest.mark.parametrize("param, spec_param", [("sort_by", "SortBy"), ("sort_order", "SortOrder")])
def test_the_values_offered_are_exactly_the_ones_the_spec_text_allows(param, spec_param):
    allowed = set(re.findall(r"`([A-Za-z]+)`", param_text("get", "/v1/projects/{projectId}/tasks", spec_param)))
    assert allowed and offered_values("list_project_tasks", param) == allowed


@needs_spec_text
def test_the_status_and_priority_ids_in_the_descriptions_match_the_spec_text():
    assert "NotStarted=1, WorkingOnIt=2, OnHold=3, Done=4" in param_text("get", "/v1/projects/{projectId}/tasks", "StatusIds")
    for tool, param in (("list_project_tasks", "status_ids"), ("create_project_task", "status_id"), ("update_project_task", "status_id")):
        assert "1 NotStarted, 2 WorkingOnIt, 3 OnHold, 4 Done" in param_description(tool, param)
    create = spec_text("post", "/v1/projects/{projectId}/tasks")
    assert "None=0, Urgent=1, High=2, Normal=3, Low=4" in create and "Omit it for Normal" in create
    for tool, param in (("create_project_task", "priority_id"), ("update_project_task", "priority_id")):
        assert "0 None, 1 Urgent, 2 High, 3 Normal, 4 Low" in param_description(tool, param)
    assert module.PRIORITIES == "0 None, 1 Urgent, 2 High, 3 Normal, 4 Low"


@needs_spec_text
def test_the_clear_values_are_the_ones_the_spec_text_documents():
    # Contract e15cb5a18ec2: every clear option is one the texts document, with the wire value they name.
    text = spec_text("patch", "/v1/projects/{projectId}/tasks/{taskId}")
    assert "send an empty array to clear one" in text
    assert "`ClearDueDate: true` removes the due date" in text and "a date field is never cleared by omission" in text
    fields = schema_fields("UpdateTaskCommand")
    # the two lists whose field text says so, and the five others that the operation text covers ("Lists replace outright")
    assert "Send an empty array to remove them all" in fields["AssistingAssigneeIds"]
    assert "Send an empty array to remove them all" in fields["WatcherIds"]
    assert all(module.TASK_CLEAR_VALUES[param] == [] for param in module.TASK_LIST_PARAMS)
    assert "Send an empty string to remove it" in fields["Banner"] and module.TASK_CLEAR_VALUES["banner"] == ""
    assert "Send 0 to remove the lead" in fields["LeadAssigneeId"] and "`LeadAssigneeId: 0` removes the lead" in text
    assert module.TASK_CLEAR_VALUES["lead_assignee_id"] == 0
    assert set(module.TASK_CLEAR_VALUES) == set(module.TASK_LIST_PARAMS) | {"banner", "lead_assignee_id"}
    assert offered_values("update_project_task", "clear_fields") == set(module.TASK_CLEAR_VALUES)
    assert "Changing the status fires the same automation the app fires" in text


@needs_spec_text
def test_the_lead_is_removed_through_clear_fields_with_the_zero_the_spec_text_documents():
    # "Send 0 to remove the lead" is Gorelo's way; the tool offers it only as the clear_fields opt-in and never as a
    # bare 0 in lead_assignee_id. The Project scope is missing, so the zero itself cannot be probed live.
    assert "`LeadAssigneeId: 0` removes the lead" in spec_text("patch", "/v1/projects/{projectId}/tasks/{taskId}")
    assert "lead_assignee_id" in offered_values("update_project_task", "clear_fields")
    assert module.TASK_CLEAR_VALUES["lead_assignee_id"] == 0


@needs_spec_text
def test_the_task_texts_the_tool_repeats_are_in_the_schemas():
    update = schema_fields("UpdateTaskCommand")
    assert "Only read when `StatusId` is sent" in update["StatusReason"]
    assert "Must be an active section of the same project" in update["SectionId"]
    assert "Send null to leave it alone; to clear it, send `ClearDueDate`" in update["DueDate"]
    side = schema_fields("CreateTaskSideConversationCommand")
    assert "Display name of the person the conversation is with" in side["Name"]
    assert side["Email"] == "Their email address. Required." and "Addresses to copy" in side["CcEmails"]
    approval = schema_fields("CreateTaskApprovalCommand")
    assert "Each must be an active contact of the task's client and carry a contact tag marked as an approver" in approval["ContactIds"]
    create = schema_fields("CreateTaskCommand")
    assert "omitted or blank the creator is recorded as `API`" in create["CreatedByName"]
    assert "Defaults to `CreatedOn`" in create["UpdatedOn"]
    # no backdating field of a task states a rule about the future or about order, so none is checked
    for field in ("CreatedOn", "UpdatedOn"):
        assert "future" not in create[field] and "earlier" not in create[field], field


@needs_spec_text
def test_the_documented_rules_the_docstrings_repeat_are_in_the_spec_text():
    create = spec_text("post", "/v1/projects/{projectId}/tasks")
    assert "Creating a task moves a project that had not started into progress" in create
    assert "fires the same automation triggers" in create and "`PRO-462-7`" in create
    delete = spec_text("delete", "/v1/projects/{projectId}/tasks/{taskId}")
    assert "A soft delete" in delete and "Blocking relationships are cleared in both directions" in delete
    assert "any automation timer waiting on it is dropped" in delete
    approval = spec_text("post", "/v1/projects/{projectId}/tasks/{taskId}/conversations/approval")
    assert "active contact of the task's client **and** carry a contact tag marked as an approver" in approval
    assert "puts the task in a waiting-on-the-contact state" in approval
    assert "Disapproved as soon as anyone disapproves" in spec_text("get", "/v1/projects/{projectId}/tasks/{taskId}/approvals/{approvalId}")
    assert "null `Id`" in spec_text("get", "/v1/projects/{projectId}/tasks/{taskId}/conversations")
    assert "assignedToMe` is not offered" in spec_text("get", "/v1/projects/{projectId}/tasks")
    assert "The conversation is created empty" in spec_text("post", "/v1/projects/{projectId}/tasks/{taskId}/conversations/side-conversation")


def schema_fields(name):
    """{field: description} of a component schema of the full OpenAPI snapshot (the field texts live there)."""
    properties = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["components"]["schemas"][name]["properties"]
    return {field: " ".join(prop.get("description", "").split()) for field, prop in properties.items()}


@needs_spec_text
def test_the_create_task_parameter_texts_match_the_field_texts_of_the_schema():
    # 2026-10-02: SectionId is required next to Title; the defaults the descriptions state are the schema's.
    fields = schema_fields("CreateTaskCommand")
    assert fields["SectionId"].endswith("Required.") and fields["Title"].endswith("Required.")
    assert "Defaults to NotStarted" in fields["StatusId"] and "NotStarted=1, WorkingOnIt=2, OnHold=3, Done=4" in fields["StatusId"]
    assert "Defaults to Normal" in fields["PriorityId"]
    assert fields["LeadAssigneeId"].endswith("Optional.")
    assert "Display name to record as the task creator" in fields["CreatedByName"]


@needs_spec_text
def test_the_task_rows_carry_the_new_flags_and_keep_their_due_date():
    schemas = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["components"]["schemas"]
    for name in ("TaskListItemModel", "TaskDetailModel"):
        properties = schemas[name]["properties"]
        assert {"IsUnread", "IsWaitingOnThem", "DueDate"} <= set(properties), name
        assert "waiting on the client rather than on the service provider" in properties["IsWaitingOnThem"]["description"]
    # only the PROJECT due date became TargetDate: the task create, update and list keep DueDate, ClearDueDate, DueAfter
    assert "DueDate" in schemas["CreateTaskCommand"]["properties"]
    assert {"DueDate", "ClearDueDate"} <= set(schemas["UpdateTaskCommand"]["properties"])
    assert "TargetDate" not in schemas["UpdateTaskCommand"]["properties"]


# Ids whose values the description enumerates (there is no tool to list them).
ENUMERATED_IDS = {"status_id", "status_ids", "priority_id"}


def id_param_texts(name):
    """{param: description} of every id parameter of a tool that is resolved with another tool."""
    properties = Tool.from_function(specs()[name].fn).parameters["properties"]
    return {
        param: schema["description"]
        for param, schema in properties.items()
        if param.endswith(("_id", "_ids")) and param not in ENUMERATED_IDS
    }


def test_every_tool_docstring_follows_the_template():
    tool_names = {spec.name for spec in REGISTRY.specs}
    for name, spec in specs().items():
        doc = spec.fn.__doc__ or ""
        assert doc.strip().splitlines()[0].endswith("."), name
        if spec.kind != "read":  # a read tool says it is read-only through its annotation
            assert "Side effects:" in doc, name
        # Where each id comes from is said once, next to the parameter, not repeated in the description.
        for param, text in id_param_texts(name).items():
            assert tool_names & set(re.findall(r"[a-z]+(?:_[a-z]+)+", text)), f"{name}.{param} names no tool to resolve it"
    assert "Paging: pass next_cursor back as cursor with the SAME filters until has_more is false." in (
        specs()["list_project_tasks"].fn.__doc__
    )


async def test_every_tool_a_description_points_to_exists(dserver):
    registered = {spec.name for spec in REGISTRY.specs}
    mentioned = set()
    for tool in await list_tools(dserver):
        if tool.name not in EXPECTED:
            continue
        texts = [tool.description] + [prop.get("description", "") for prop in tool.inputSchema["properties"].values()]
        for text in texts:
            mentioned |= set(re.findall(r"\b(?:list|get|create|update|delete|upload)_[a-z_]+\b", text))
    assert mentioned and mentioned <= registered, sorted(mentioned - registered)


async def test_descriptions_stay_within_the_size_limits(dserver):
    tools = {t.name: t for t in await list_tools(dserver)}
    for name in EXPECTED:
        assert len(tools[name].description) <= 900, name
        for param, schema in tools[name].inputSchema["properties"].items():
            assert len(schema["description"]) <= 220, f"{name}.{param}"


async def test_the_annotations_tell_reads_writes_and_overwrites_apart(dserver):
    tools = {t.name: t for t in await list_tools(dserver)}
    assert set(EXPECTED) <= set(tools)
    for name, (kind, _ops, hint) in EXPECTED.items():
        annotations = tools[name].annotations
        assert annotations.readOnlyHint is (kind == "read"), name
        assert annotations.destructiveHint is hint, name
        assert annotations.openWorldHint is True, name


async def test_the_task_delete_exists_only_when_deletes_are_enabled(server, dserver):
    assert "delete_project_task" not in {t.name for t in await list_tools(server)}
    assert "delete_project_task" in {t.name for t in await list_tools(dserver)}
    text = await call_tool_error(server, "delete_project_task", VALID["delete_project_task"])
    assert "unknown tool" in text.lower()


# --------------------------------------------------------------------------
# Generic behaviour of every tool
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_a_missing_project_scope_surfaces_as_the_scope_message(dserver, mock_gorelo, name):
    for op in specs()[name].ops:
        mock_gorelo.on_op(op, error_envelope(403, [SCOPE_NOTE]))
    text = await call_tool_error(dserver, name, VALID[name])
    assert text.startswith(f"Gorelo rejected {name} (HTTP 403, code 080203): ")
    assert "the API key does not have the 'Project' scope. Grant it on the API key in Gorelo, then retry" in text
    assert f"[trace {TEST_TRACE_ID}]" in text
    assert len(mock_gorelo.requests) == 1  # refused once: no retry, no re-read


def accepted_params(name):
    return set(Tool.from_function(specs()[name].fn).parameters["properties"])


@pytest.mark.parametrize("param", ["project_id", "task_id", "approval_id"])
async def test_a_malformed_path_id_is_refused_locally_naming_the_param(dserver, mock_gorelo, param):
    tested = 0
    async with Client(dserver) as session:  # one lifespan for every call below
        for name in sorted(EXPECTED):
            if param not in accepted_params(name):
                continue
            for bad in ("not-a-uuid", "12345", "", "../../etc", " " + uid(9), uid(9) + "\n"):
                tested += 1
                call = {**VALID[name], param: bad}
                call.pop("confirm", None)  # the id is checked before the confirm gate
                text = await call_tool_error(session, name, call)
                assert text.startswith(f"{param}: expected a GUID such as "), (name, bad, text)
    assert tested >= 6
    assert mock_gorelo.requests == []


async def test_a_path_id_is_sent_in_canonical_lowercase_form(server, mock_gorelo):
    mock_gorelo.on("GET", TASK_URL, envelope(task_detail()))
    await call_tool(server, "get_project_task", {"project_id": PROJECT.upper(), "task_id": TASK.upper()})
    await call_tool(server, "get_project_task", {"project_id": PROJECT.replace("-", ""), "task_id": TASK.replace("-", "")})
    assert sent(mock_gorelo) == [("GET", TASK_URL)] * 2


def integer_params(name):
    """(param, is_list) of every integer parameter of a tool except page_size, read from its published schema."""
    found = []
    for param, schema in Tool.from_function(specs()[name].fn).parameters["properties"].items():
        if param == "page_size":  # a count, not an id: it is clamped, never looked up
            continue
        for branch in schema.get("anyOf", [schema]):
            if branch.get("type") == "integer":
                found.append((param, False))
            elif branch.get("type") == "array" and branch.get("items", {}).get("type") == "integer":
                found.append((param, True))
    return found


@pytest.mark.parametrize("bad", [True, "5", 5.0], ids=["boolean", "text", "decimal"])
async def test_every_integer_id_param_refuses_a_boolean_a_text_and_a_decimal(dserver, mock_gorelo, bad):
    # Strict ids: a lax int turns true into 1, "5" into 5 and 5.0 into 5, and would send that id to Gorelo.
    tested = []
    async with Client(dserver) as session:  # one lifespan for every call below
        for name in sorted(EXPECTED):
            for param, is_list in integer_params(name):
                call = {**VALID[name], param: [bad] if is_list else bad}
                text = await call_tool_error(session, name, call)
                assert param in text and "valid integer" in text, (name, param, text)
                tested.append((name, param))
    assert len(tested) >= 14  # list 3, create 5, update 5, approval 1
    assert mock_gorelo.requests == []


def test_the_integer_id_params_are_the_ones_the_documentation_lists():
    found = {param for name in EXPECTED for param, _ in integer_params(name)}
    assert found == {
        "status_ids", "assignee_ids", "lead_assignee_ids", "lead_assignee_id", "assisting_assignee_ids",
        "watcher_ids", "priority_id", "status_id", "contact_ids",
    }


async def test_integer_ids_are_still_accepted_as_numbers(server, mock_gorelo):
    mock_gorelo.on("GET", TASKS_URL, paged_envelope([]))
    await call_tool(server, "list_project_tasks", {"project_id": PROJECT, "status_ids": [1], "assignee_ids": [9201]})
    assert mock_gorelo.last.query == {"StatusIds": "1", "AssigneeIds": "9201", "PageSize": "50"}


def test_the_private_copies_of_the_shared_helpers_are_gone():
    # tools/_common.py owns positive_id(s), guid(s), created_id, expect_object and describe_value.
    for name in ("_uuid", "_uuids", "_positive", "_positives", "_shown", "_written_id", "_deleted", "_record", "_lead"):
        assert not hasattr(module, name), name


# --------------------------------------------------------------------------
# list_project_tasks
# --------------------------------------------------------------------------

ALL_FILTERS = {
    "section_ids": [SECTION.upper(), uid(31)], "status_ids": [1, 2], "assignee_ids": [9201, 9202],
    "lead_assignee_ids": [9201], "incomplete_only": True, "due_after": "2026-11-01T00:00:00-05:00",
    "due_before": "2026-12-01T00:00:00Z", "sort_by": "updatedOn", "sort_order": "desc",
}


async def test_list_project_tasks_sends_every_filter_under_its_spec_name(server, mock_gorelo):
    mock_gorelo.on("GET", TASKS_URL, paged_envelope([task_row()], next_cursor="c2", total_count=60))
    await call_tool(server, "list_project_tasks", {"project_id": PROJECT, **ALL_FILTERS, "page_size": 20, "cursor": "c1"})
    request = mock_gorelo.last
    assert (request.method, request.path) == ("GET", TASKS_URL)
    assert request.query == {
        "SectionIds": f"{SECTION},{uid(31)}", "StatusIds": "1,2", "AssigneeIds": "9201,9202", "LeadAssigneeIds": "9201",
        "IncompleteOnly": "true", "DueAfter": "2026-11-01T05:00:00Z", "DueBefore": "2026-12-01T00:00:00Z",
        "SortBy": "updatedOn", "SortOrder": "desc", "PageSize": "20", "Cursor": "c1",
    }
    assert request.json is None


async def test_list_project_tasks_returns_the_paged_shape_and_echoes_the_filters(server, mock_gorelo):
    rows = [task_row(), task_row(Id=uid(12), Title="Second", DisplayNumber="PRO-462-8")]
    mock_gorelo.on("GET", TASKS_URL, paged_envelope(rows, next_cursor="c2", total_count=60))
    result = await call_tool(server, "list_project_tasks", {"project_id": PROJECT, **ALL_FILTERS, "page_size": 2})
    assert result == {
        "items": rows, "count": 2, "total_count": 60, "has_more": True, "next_cursor": "c2", "page_size": 2,
        "filters": {
            "project_id": PROJECT, "section_ids": [SECTION, uid(31)], "status_ids": [1, 2], "assignee_ids": [9201, 9202],
            "lead_assignee_ids": [9201], "incomplete_only": True, "due_after": "2026-11-01T05:00:00Z",
            "due_before": "2026-12-01T00:00:00Z", "sort_by": "updatedOn", "sort_order": "desc",
        },
    }


async def test_list_project_tasks_without_filters_sends_only_the_page_size(server, mock_gorelo):
    mock_gorelo.on("GET", TASKS_URL, paged_envelope([task_row()]))
    result = await call_tool(server, "list_project_tasks", {"project_id": PROJECT})
    assert mock_gorelo.last.query == {"PageSize": "50"}
    assert result["filters"] == {"project_id": PROJECT} and (result["has_more"], result["next_cursor"]) == (False, None)


@pytest.mark.parametrize("flag, sent_value", [(True, "true"), (False, "false")])
async def test_list_project_tasks_incomplete_only_is_sent_only_when_given(server, mock_gorelo, flag, sent_value):
    mock_gorelo.on("GET", TASKS_URL, paged_envelope([]))
    await call_tool(server, "list_project_tasks", {"project_id": PROJECT, "incomplete_only": flag})
    assert mock_gorelo.last.query["IncompleteOnly"] == sent_value


@pytest.mark.parametrize("given, used", [(500, 200), (201, 200), (200, 200), (1, 1), (0, 1), (-7, 1), (50, 50)])
async def test_list_project_tasks_clamps_the_page_size_and_reports_the_size_used(server, mock_gorelo, given, used):
    mock_gorelo.on("GET", TASKS_URL, paged_envelope([]))
    result = await call_tool(server, "list_project_tasks", {"project_id": PROJECT, "page_size": given})
    assert mock_gorelo.last.query["PageSize"] == str(used) and result["page_size"] == used


async def test_list_project_tasks_follows_the_cursor_with_the_same_filters(server, mock_gorelo):
    mock_gorelo.on("GET", TASKS_URL, paged_responder([[task_row(Id=uid(20))], [task_row(Id=uid(21))]]))
    base = {"project_id": PROJECT, "status_ids": [2], "page_size": 1}
    first = await call_tool(server, "list_project_tasks", base)
    second = await call_tool(server, "list_project_tasks", {**base, "cursor": first["next_cursor"]})
    assert first["has_more"] is True and first["items"][0]["Id"] == uid(20)
    assert second["has_more"] is False and second["next_cursor"] is None and second["items"][0]["Id"] == uid(21)
    assert [r.query for r in mock_gorelo.requests] == [
        {"StatusIds": "2", "PageSize": "1"}, {"StatusIds": "2", "PageSize": "1", "Cursor": "c1"},
    ]


async def test_list_project_tasks_an_empty_page_is_reported_as_zero_rows(server, mock_gorelo):
    mock_gorelo.on("GET", TASKS_URL, paged_envelope([]))
    result = await call_tool(server, "list_project_tasks", {"project_id": PROJECT, "status_ids": [4]})
    assert (result["items"], result["count"], result["total_count"], result["has_more"]) == ([], 0, 0, False)


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"section_ids": []}, "section_ids: expected at least one GUID, got an empty list"),
        ({"section_ids": ["nope"]}, "section_ids[0]: expected a GUID such as"),
        ({"section_ids": [SECTION, "12"]}, "section_ids[1]: expected a GUID such as"),
        ({"status_ids": []}, "status_ids: expected at least one id, got an empty list"),
        ({"status_ids": [0]}, "status_ids[0]: expected a positive whole number such as 123, got zero or a negative number"),
        ({"assignee_ids": [-5]}, "assignee_ids[0]: expected a positive whole number"),
        ({"assignee_ids": [2**63]}, "assignee_ids[0]: expected a positive whole number such as 123, got a number above"),
        ({"assignee_ids": []}, "assignee_ids: expected at least one id, got an empty list"),
        ({"lead_assignee_ids": [9201, 0]}, "lead_assignee_ids[1]: expected a positive whole number"),
        ({"due_after": "2026-11-01T00:00:00"}, "due_after: '2026-11-01T00:00:00' has no UTC offset"),
        ({"due_after": "2026-11-01"}, "due_after: '2026-11-01' has no UTC offset"),
        ({"due_before": "soon"}, "due_before: 'soon' is not an ISO 8601 datetime"),
        # a blank cursor is a caller mistake, not "the first page" (that is no cursor at all)
        ({"cursor": ""}, "cursor: must not be empty or whitespace only"),
        ({"cursor": "   "}, "cursor: must not be empty or whitespace only"),
        ({"cursor": "\t"}, "cursor: must not be empty or whitespace only"),
    ],
)
async def test_list_project_tasks_local_validation_errors_name_the_param_and_send_nothing(
    server, mock_gorelo, arguments, fragment
):
    text = await call_tool_error(server, "list_project_tasks", {"project_id": PROJECT, **arguments})
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "arguments, fragments",
    [
        ({"sort_by": "title"}, ["sort_by", "'boardOrder', 'createdOn' or 'updatedOn'"]),
        ({"sort_by": "BOARDORDER"}, ["sort_by"]),
        ({"sort_order": "down"}, ["sort_order", "'asc' or 'desc'"]),
        ({"status_ids": ["done"]}, ["status_ids.0"]),
        ({"incomplete_only": "maybe"}, ["incomplete_only"]),
    ],
)
async def test_list_project_tasks_refuses_values_the_spec_text_does_not_allow(server, mock_gorelo, arguments, fragments):
    text = await call_tool_error(server, "list_project_tasks", {"project_id": PROJECT, **arguments})
    for fragment in fragments:
        assert fragment in text
    assert mock_gorelo.requests == []


async def test_list_project_tasks_offers_no_assigned_to_me_filter(server, mock_gorelo):
    text = await call_tool_error(server, "list_project_tasks", {"project_id": PROJECT, "assigned_to_me": True})
    assert "assigned_to_me" in text and "Unexpected keyword argument" in text
    assert mock_gorelo.requests == []


async def test_list_project_tasks_maps_gorelo_errors_to_the_param(server, mock_gorelo):
    mock_gorelo.on(
        "GET", TASKS_URL,
        error_envelope(400, [("070101", "A section id is not valid.", "SectionIds"), ("070101", "Bad cursor.", "Cursor")]),
    )
    text = await call_tool_error(server, "list_project_tasks", {"project_id": PROJECT, "section_ids": [SECTION]})
    assert text == (
        "Gorelo rejected list_project_tasks (HTTP 400, code 070101): section_ids: A section id is not valid.; "
        f"cursor: Bad cursor. [trace {TEST_TRACE_ID}]"
    )


async def test_list_project_tasks_unknown_project_is_a_404(server, mock_gorelo):
    mock_gorelo.on("GET", TASKS_URL, error_envelope(404, [("070401", "Project was not found.")]))
    text = await call_tool_error(server, "list_project_tasks", {"project_id": PROJECT})
    assert text.startswith("Gorelo rejected list_project_tasks (HTTP 404, code 070401): Project was not found.")


async def test_list_project_tasks_refuses_a_response_that_is_not_a_list(server, mock_gorelo):
    mock_gorelo.on("GET", TASKS_URL, envelope(None))
    text = await call_tool_error(server, "list_project_tasks", {"project_id": PROJECT})
    assert text.startswith("Gorelo returned an unexpected response for list_project_tasks: GET ")
    assert "expected Data to be a list but got null" in text


def test_list_project_tasks_docstring_says_there_is_no_assigned_to_me_filter():
    doc = " ".join(specs()["list_project_tasks"].fn.__doc__.split())
    assert "There is no assigned-to-me filter: use assignee_ids" in doc
    assert "1 NotStarted, 2 WorkingOnIt, 3 OnHold, 4 Done" in param_description("list_project_tasks", "status_ids")


@needs_spec_text
def test_list_project_tasks_docstring_says_the_rows_carry_the_blocking_ids_as_the_schema_does():
    # said the blocking relationships were only on get_project_task. TaskListItemModel (2026-10-01 and
    # 2026-10-02 alike) carries BlockedByTaskIds and BlockingTaskIds, so a list row has them; the text of the single
    # task read still calls "blocking relationships in both directions" a field of the single task (a contradiction
    # inside the spec), and the docstring follows the schema, which is what the fixtures of this file also use.
    schemas = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["components"]["schemas"]
    assert {"BlockedByTaskIds", "BlockingTaskIds"} <= set(schemas["TaskListItemModel"]["properties"])
    assert {"BlockedByTaskIds", "BlockingTaskIds"} <= set(task_row())
    doc = " ".join(specs()["list_project_tasks"].fn.__doc__.split())
    assert "Rows also carry BlockedByTaskIds and BlockingTaskIds" in doc
    assert "(the tasks that must finish before this one, and the tasks it blocks)" in doc
    assert "only on get_project_task" not in doc and "one call per task" not in doc
    assert "blocking relationships" in specs()["get_project_task"].fn.__doc__


async def test_list_project_tasks_returns_the_blocking_ids_of_a_row_unchanged(server, mock_gorelo):
    row = task_row(BlockedByTaskIds=[BLOCKER], BlockingTaskIds=[uid(11)])
    mock_gorelo.on("GET", TASKS_URL, paged_envelope([row]))
    listed = await call_tool(server, "list_project_tasks", {"project_id": PROJECT})
    assert listed["items"][0]["BlockedByTaskIds"] == [BLOCKER] and listed["items"][0]["BlockingTaskIds"] == [uid(11)]


def test_the_task_reads_say_rows_carry_the_unread_and_waiting_flags():
    # 2026-10-02: TaskListItemModel and TaskDetailModel gained IsUnread and IsWaitingOnThem.
    listed = " ".join(specs()["list_project_tasks"].fn.__doc__.split())
    assert "Rows carry IsUnread and IsWaitingOnThem (true while the task waits on the client rather than on the service provider)." in listed
    detail = " ".join(specs()["get_project_task"].fn.__doc__.split())
    assert "plus the IsUnread and IsWaitingOnThem flags" in detail
    assert "IsUnread" not in " ".join(specs()["create_project_task"].fn.__doc__.split())


async def test_task_reads_return_the_unread_and_waiting_flags_unchanged(server, mock_gorelo):
    row = task_row(IsUnread=True, IsWaitingOnThem=True)
    detail = task_detail(IsUnread=True, IsWaitingOnThem=False)
    mock_gorelo.on("GET", TASKS_URL, paged_envelope([row, task_row(Id=uid(12))]))
    mock_gorelo.on("GET", TASK_URL, envelope(detail))
    listed = await call_tool(server, "list_project_tasks", {"project_id": PROJECT})
    assert listed["items"][0]["IsUnread"] is True and listed["items"][0]["IsWaitingOnThem"] is True
    assert listed["items"][1]["IsUnread"] is False and listed["items"][1]["IsWaitingOnThem"] is False
    assert await call_tool(server, "get_project_task", {"project_id": PROJECT, "task_id": TASK}) == detail


# --------------------------------------------------------------------------
# get_project_task
# --------------------------------------------------------------------------


async def test_get_project_task_returns_the_record_unchanged(server, mock_gorelo):
    record = task_detail(AgentAssetIds=[ASSET], BlockedByTaskIds=[BLOCKER])
    mock_gorelo.on("GET", TASK_URL, envelope(record))
    assert await call_tool(server, "get_project_task", {"project_id": PROJECT, "task_id": TASK}) == record
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", TASK_URL, {}, None)


@pytest.mark.parametrize(
    "data, kind",
    [
        ([{"Id": TASK, "Title": "secret-title"}], "a list of 1 item"), (False, "a boolean"),
        ("secret-title", "a string"), (7, "a number"), ({}, "an empty object"),
    ],
)
async def test_the_single_record_reads_refuse_an_answer_that_is_not_an_object_without_quoting_it(
    server, mock_gorelo, data, kind
):
    mock_gorelo.on("GET", TASK_URL, envelope(data))
    mock_gorelo.on("GET", f"{TASK_URL}/approvals/{APPROVAL}", envelope(data))
    calls = (
        ("get_project_task", {"project_id": PROJECT, "task_id": TASK}, GET_TASK),
        ("get_task_approval", {"project_id": PROJECT, "task_id": TASK, "approval_id": APPROVAL}, f"GET {APPROVAL_PATH}"),
    )
    for tool, arguments, op in calls:
        text = await call_tool_error(server, tool, arguments)
        assert text == (
            f"Gorelo returned an unexpected response for {tool}: {op}: expected Data to be a non-empty object but got "
            f"{kind}; refusing to guess"
        )
        assert "secret-title" not in text


async def test_get_project_task_a_task_in_a_project_you_cannot_see_is_a_404(server, mock_gorelo):
    mock_gorelo.on("GET", TASK_URL, error_envelope(404, [("070401", "Task was not found.")]))
    text = await call_tool_error(server, "get_project_task", {"project_id": PROJECT, "task_id": TASK})
    assert text == f"Gorelo rejected get_project_task (HTTP 404, code 070401): Task was not found. [trace {TEST_TRACE_ID}]"


# --------------------------------------------------------------------------
# create_project_task
# --------------------------------------------------------------------------


def route_create_task(mock, record=None):
    mock.on("POST", TASKS_URL, envelope({"Id": TASK}))
    mock.on("GET", TASK_URL, envelope(record if record is not None else task_detail()))


async def test_create_project_task_with_only_the_required_fields_sends_exactly_those_and_returns_the_reread_record(
    server, mock_gorelo
):
    record = task_detail(Title="Rack the switch")
    route_create_task(mock_gorelo, record)
    assert await call_tool(server, "create_project_task", VALID["create_project_task"]) == record
    assert sent(mock_gorelo) == [("POST", TASKS_URL), ("GET", TASK_URL)]
    assert mock_gorelo.requests[0].json == {"Title": "Rack the switch", "SectionId": SECTION}
    assert mock_gorelo.requests[0].query == {} and mock_gorelo.requests[1].query == {}


async def test_create_project_task_sends_every_field_in_pascal_case(server, mock_gorelo):
    route_create_task(mock_gorelo)
    await call_tool(
        server,
        "create_project_task",
        {
            "project_id": PROJECT, "title": "Rack the switch", "section_id": SECTION.upper(), "lead_assignee_id": 9201,
            "assisting_assignee_ids": [9202, 1600], "watcher_ids": [1700], "priority_id": 2, "status_id": 2,
            "due_date": "2026-11-30T12:00:00-05:00", "created_by_name": "Import job",
            "created_on": "2026-01-02T08:00:00+02:00", "updated_on": "2026-01-03T00:00:00Z",
        },
    )
    assert mock_gorelo.requests[0].json == {
        "Title": "Rack the switch", "SectionId": SECTION, "LeadAssigneeId": 9201, "AssistingAssigneeIds": [9202, 1600],
        "WatcherIds": [1700], "PriorityId": 2, "StatusId": 2, "DueDate": "2026-11-30T17:00:00Z",
        "CreatedByName": "Import job", "CreatedOn": "2026-01-02T06:00:00Z", "UpdatedOn": "2026-01-03T00:00:00Z",
    }
    assert mock_gorelo.requests[0].path == TASKS_URL


@pytest.mark.parametrize("priority", [0, 1, 2, 3, 4])
async def test_create_project_task_accepts_every_priority_of_the_one_scale_including_none(server, mock_gorelo, priority):
    route_create_task(mock_gorelo)
    await call_tool(server, "create_project_task", {**VALID["create_project_task"], "title": "T", "priority_id": priority})
    assert mock_gorelo.requests[0].json == {"Title": "T", "SectionId": SECTION, "PriorityId": priority}


async def test_create_project_task_never_sends_a_priority_the_caller_did_not_give(server, mock_gorelo):
    route_create_task(mock_gorelo)
    await call_tool(server, "create_project_task", {**VALID["create_project_task"], "title": "T"})
    assert set(mock_gorelo.requests[0].json) == {"Title", "SectionId"}


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"title": ""}, "title: must not be empty or whitespace only"),
        ({"title": "   "}, "title: must not be empty or whitespace only"),
        ({"priority_id": 5}, "priority_id: expected 0 None, 1 Urgent, 2 High, 3 Normal, 4 Low, got 5"),
        ({"priority_id": -1}, "priority_id: expected 0 None, 1 Urgent, 2 High, 3 Normal, 4 Low, got -1"),
        ({"section_id": "backlog"}, "section_id: expected a GUID such as"),
        ({"lead_assignee_id": 0}, "lead_assignee_id: expected a positive whole number such as 123, got zero or a negative number"),
        ({"lead_assignee_id": -2}, "lead_assignee_id: expected a positive whole number"),
        ({"assisting_assignee_ids": []}, "assisting_assignee_ids: expected at least one id, got an empty list"),
        ({"assisting_assignee_ids": [0]}, "assisting_assignee_ids[0]: expected a positive whole number"),
        ({"watcher_ids": []}, "watcher_ids: expected at least one id, got an empty list"),
        ({"watcher_ids": [9202, -1]}, "watcher_ids[1]: expected a positive whole number"),
        ({"status_id": 0}, "status_id: expected a positive whole number"),
        ({"due_date": "2026-11-30T17:00:00"}, "due_date: '2026-11-30T17:00:00' has no UTC offset"),
        ({"created_on": "2026-01-02"}, "created_on: '2026-01-02' has no UTC offset"),
        ({"updated_on": "tomorrow"}, "updated_on: 'tomorrow' is not an ISO 8601 datetime"),
        ({"created_by_name": ""}, "created_by_name: must not be empty or whitespace only"),
    ],
)
async def test_create_project_task_local_validation_errors_name_the_param_and_send_nothing(
    server, mock_gorelo, arguments, fragment
):
    text = await call_tool_error(server, "create_project_task", {**VALID["create_project_task"], "title": "T", **arguments})
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("missing", ["title", "section_id"])
async def test_create_project_task_refuses_a_call_without_a_required_field_and_names_it(server, mock_gorelo, missing):
    # 2026-10-02: SectionId is required (it used to be left to Gorelo), and no section is picked for the caller.
    call = {k: v for k, v in VALID["create_project_task"].items() if k != missing}
    text = await call_tool_error(server, "create_project_task", call)
    assert missing in text and "Missing required argument" in text
    assert mock_gorelo.requests == []


async def test_create_project_task_with_neither_title_nor_section_names_both(server, mock_gorelo):
    text = await call_tool_error(server, "create_project_task", {"project_id": PROJECT})
    assert "title" in text and "section_id" in text and "Missing required argument" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", ["", "   ", "backlog", "12345", None])
async def test_create_project_task_refuses_a_section_that_is_not_a_guid(server, mock_gorelo, bad):
    text = await call_tool_error(server, "create_project_task", {**VALID["create_project_task"], "section_id": bad})
    assert "section_id" in text
    assert mock_gorelo.requests == []


async def test_create_project_task_maps_gorelo_errors_to_the_param(server, mock_gorelo):
    mock_gorelo.on(
        "POST", TASKS_URL,
        error_envelope(
            400,
            [
                ("070101", "Title must not be empty.", "Title"),
                ("070101", "The section is not in this project.", "SectionId"),
                ("070101", "Unknown priority.", "PriorityId"),
                ("070101", "Unknown technician.", "LeadAssigneeId"),
            ],
        ),
    )
    text = await call_tool_error(server, "create_project_task", {**VALID["create_project_task"], "title": "x"})
    assert text == (
        "Gorelo rejected create_project_task (HTTP 400, code 070101): title: Title must not be empty.; "
        "section_id: The section is not in this project.; priority_id: Unknown priority.; "
        f"lead_assignee_id: Unknown technician. [trace {TEST_TRACE_ID}]"
    )
    assert sent(mock_gorelo) == [("POST", TASKS_URL)]


async def test_create_project_task_a_failed_reread_returns_the_warning_and_never_repeats_the_write(server, mock_gorelo):
    mock_gorelo.on("POST", TASKS_URL, envelope({"Id": TASK}))
    mock_gorelo.on("GET", TASK_URL, error_envelope(500, [("070001", "Internal error.")]))
    result = await call_tool(server, "create_project_task", {**VALID["create_project_task"], "title": "T"})
    assert set(result) == {"Id", "warning"} and result["Id"] == TASK
    assert result["warning"].startswith("the write succeeded; re-reading it failed: GET /v1/projects/{projectId}/tasks/{taskId} answered HTTP 500")
    assert result["warning"].endswith("Do not repeat the write; read it again later.")
    assert sent(mock_gorelo) == [("POST", TASKS_URL), ("GET", TASK_URL)]


@pytest.mark.parametrize("data", [None, {}, {"Id": None}, {"Id": ""}, {"Id": 0}, {"Id": True}, [{"Id": TASK}], False])
async def test_create_project_task_an_answer_without_a_usable_id_is_an_unconfirmed_write(server, mock_gorelo, data):
    mock_gorelo.on("POST", TASKS_URL, envelope(data))
    text = await call_tool_error(server, "create_project_task", {**VALID["create_project_task"], "title": "T"})
    assert UNUSABLE_ID in text and VERIFY in text
    assert sent(mock_gorelo) == [("POST", TASKS_URL)]  # no re-read, no second write


async def test_create_project_task_a_timeout_is_unconfirmed_and_not_retried(server, mock_gorelo):
    mock_gorelo.on("POST", TASKS_URL, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "create_project_task", {**VALID["create_project_task"], "title": "T"})
    assert text == (
        "Gorelo did not confirm create_project_task (the request timed out). The change may or may not have been "
        "applied. Verify with a read before retrying."
    )
    assert sent(mock_gorelo) == [("POST", TASKS_URL)]


def test_create_project_task_docstring_lists_the_required_fields_and_the_section_text_says_it_is_needed():
    doc = " ".join(specs()["create_project_task"].fn.__doc__.split())
    assert "Required: title and section_id (a project with no section needs create_project_section first)" in doc
    assert "everything else is optional" in doc and "Only title is required" not in doc
    section = param_description("create_project_task", "section_id")
    assert "list_project_sections" in section
    assert "Omit" not in section and "let Gorelo decide" not in section
    assert "Omit and Gorelo uses NotStarted" in param_description("create_project_task", "status_id")


def test_create_project_task_docstring_states_the_documented_side_effects():
    doc = " ".join(specs()["create_project_task"].fn.__doc__.split())
    for fragment in (
        "moves a not-started project into progress", "fires the same automation triggers", "do not create it again",
    ):
        assert fragment in doc, fragment
    assert "0 None, 1 Urgent, 2 High, 3 Normal, 4 Low" in param_description("create_project_task", "priority_id")


# --------------------------------------------------------------------------
# update_project_task
# --------------------------------------------------------------------------


def route_update_task(mock, record=None):
    mock.on("PATCH", TASK_URL, envelope({"Id": TASK}))
    mock.on("GET", TASK_URL, envelope(record if record is not None else task_detail()))


@pytest.mark.parametrize(
    "arguments, body",
    [
        ({"title": "New title"}, {"Title": "New title"}),
        ({"section_id": SECTION.upper()}, {"SectionId": SECTION}),
        ({"status_id": 4}, {"StatusId": 4}),
        ({"status_id": 3, "status_reason": "Blocked by a supplier"}, {"StatusId": 3, "StatusReason": "Blocked by a supplier"}),
        ({"priority_id": 0}, {"PriorityId": 0}),
        ({"priority_id": 1}, {"PriorityId": 1}),
        ({"due_date": "2026-11-30T12:00:00-05:00"}, {"DueDate": "2026-11-30T17:00:00Z"}),
        ({"clear_due_date": True}, {"ClearDueDate": True}),
        ({"lead_assignee_id": 9202}, {"LeadAssigneeId": 9202}),
        ({"assisting_assignee_ids": [1, 2]}, {"AssistingAssigneeIds": [1, 2]}),
        ({"watcher_ids": [9201]}, {"WatcherIds": [9201]}),
        ({"blocked_by_task_ids": [BLOCKER.upper()]}, {"BlockedByTaskIds": [BLOCKER]}),
        ({"blocking_task_ids": [BLOCKER]}, {"BlockingTaskIds": [BLOCKER]}),
        ({"agent_asset_ids": [ASSET]}, {"AgentAssetIds": [ASSET]}),
        ({"custom_asset_ids": [CUSTOM_ASSET]}, {"CustomAssetIds": [CUSTOM_ASSET]}),
        ({"uptime_ids": [UPTIME]}, {"UptimeIds": [UPTIME]}),
        ({"banner": "Maintenance window"}, {"Banner": "Maintenance window"}),
        ({"title": "T", "updated_by_name": "Import job"}, {"Title": "T", "UpdatedByName": "Import job"}),
        ({"clear_fields": ["assisting_assignee_ids"]}, {"AssistingAssigneeIds": []}),
        ({"clear_fields": ["watcher_ids"]}, {"WatcherIds": []}),
        ({"clear_fields": ["blocked_by_task_ids"]}, {"BlockedByTaskIds": []}),
        ({"clear_fields": ["blocking_task_ids"]}, {"BlockingTaskIds": []}),
        ({"clear_fields": ["agent_asset_ids"]}, {"AgentAssetIds": []}),
        ({"clear_fields": ["custom_asset_ids"]}, {"CustomAssetIds": []}),
        ({"clear_fields": ["uptime_ids"]}, {"UptimeIds": []}),
        # 2026-10-02: the banner is removed with "" and the lead with 0, both only through clear_fields
        ({"clear_fields": ["banner"]}, {"Banner": ""}),
        ({"clear_fields": ["lead_assignee_id"]}, {"LeadAssigneeId": 0}),
        (
            {"clear_fields": ["watcher_ids", "uptime_ids", "watcher_ids"], "status_id": 2},
            {"StatusId": 2, "WatcherIds": [], "UptimeIds": []},
        ),
        (
            {"clear_fields": ["lead_assignee_id", "banner", "assisting_assignee_ids"], "status_id": 2},
            {"StatusId": 2, "LeadAssigneeId": 0, "Banner": "", "AssistingAssigneeIds": []},
        ),
        # a new lead and the removal of the watchers are different fields, so they may share one call
        ({"lead_assignee_id": 9201, "clear_fields": ["watcher_ids"]}, {"LeadAssigneeId": 9201, "WatcherIds": []}),
        ({"section_id": SECTION, "status_id": 3, "status_reason": "x"}, {"SectionId": SECTION, "StatusId": 3, "StatusReason": "x"}),
    ],
)
async def test_update_project_task_sends_only_what_the_caller_gave(server, mock_gorelo, arguments, body):
    record = task_detail(Title="After")
    route_update_task(mock_gorelo, record)
    result = await call_tool(server, "update_project_task", {"project_id": PROJECT, "task_id": TASK, **arguments})
    assert result == record
    assert sent(mock_gorelo) == [("PATCH", TASK_URL), ("GET", TASK_URL)]
    assert mock_gorelo.requests[0].json == body
    assert mock_gorelo.requests[0].query == {}


async def test_update_project_task_sends_every_field_in_one_call(server, mock_gorelo):
    route_update_task(mock_gorelo)
    await call_tool(
        server,
        "update_project_task",
        {
            "project_id": PROJECT, "task_id": TASK, "title": "T", "section_id": SECTION, "status_id": 3,
            "status_reason": "Parts", "priority_id": 2, "due_date": "2026-11-30T17:00:00Z", "lead_assignee_id": 9201,
            "assisting_assignee_ids": [9202], "watcher_ids": [1700], "blocked_by_task_ids": [BLOCKER],
            "blocking_task_ids": [uid(11)], "agent_asset_ids": [ASSET], "custom_asset_ids": [CUSTOM_ASSET],
            "uptime_ids": [UPTIME], "banner": "Heads up", "updated_by_name": "Import job",
        },
    )
    assert mock_gorelo.requests[0].json == {
        "Title": "T", "SectionId": SECTION, "StatusId": 3, "StatusReason": "Parts", "PriorityId": 2,
        "DueDate": "2026-11-30T17:00:00Z", "LeadAssigneeId": 9201, "AssistingAssigneeIds": [9202],
        "WatcherIds": [1700], "BlockedByTaskIds": [BLOCKER], "BlockingTaskIds": [uid(11)], "AgentAssetIds": [ASSET],
        "CustomAssetIds": [CUSTOM_ASSET], "UptimeIds": [UPTIME], "Banner": "Heads up", "UpdatedByName": "Import job",
    }


async def test_update_project_task_false_for_clear_due_date_is_the_same_as_leaving_it_out(server, mock_gorelo):
    route_update_task(mock_gorelo)
    await call_tool(server, "update_project_task", {"project_id": PROJECT, "task_id": TASK, "title": "T", "clear_due_date": False})
    assert mock_gorelo.requests[0].json == {"Title": "T"}


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({}, "nothing to change: give at least one of title, section_id, status_id"),
        ({"updated_by_name": "Import job"}, "updated_by_name alone changes nothing"),
        ({"clear_due_date": False}, "nothing to change"),
        ({"clear_due_date": True, "due_date": "2026-11-30T17:00:00Z"}, "due_date: cannot be set while clear_due_date is true"),
        # 2026-10-02: a bare 0 is never sent: the lead is removed through clear_fields (Gorelo: "Send 0 to remove the lead")
        ({"lead_assignee_id": 0}, 'lead_assignee_id: 0 is not a technician id; to remove the lead pass clear_fields=["lead_assignee_id"]'),
        ({"lead_assignee_id": -1}, "lead_assignee_id: expected a positive whole number"),
        (
            {"lead_assignee_id": 9201, "clear_fields": ["lead_assignee_id"]},
            "lead_assignee_id: cannot be given a value and cleared in the same call",
        ),
        # 2026-10-02: StatusReason is "Only read when StatusId is sent"
        ({"status_reason": "Waiting"}, "status_reason: only recorded together with status_id; pass the status as well"),
        (
            {"status_reason": "Waiting", "title": "T", "clear_fields": ["watcher_ids"]},
            "status_reason: only recorded together with status_id; pass the status as well",
        ),
        ({"priority_id": 9}, "priority_id: expected 0 None, 1 Urgent, 2 High, 3 Normal, 4 Low, got 9"),
        ({"priority_id": -1}, "priority_id: expected 0 None, 1 Urgent, 2 High, 3 Normal, 4 Low, got -1"),
        ({"assisting_assignee_ids": []}, 'assisting_assignee_ids: an empty list is not accepted because lists replace the stored one; to remove every entry pass clear_fields=["assisting_assignee_ids"]'),
        ({"watcher_ids": []}, 'clear_fields=["watcher_ids"]'),
        ({"blocked_by_task_ids": []}, 'clear_fields=["blocked_by_task_ids"]'),
        ({"blocking_task_ids": []}, 'clear_fields=["blocking_task_ids"]'),
        ({"agent_asset_ids": []}, 'clear_fields=["agent_asset_ids"]'),
        ({"custom_asset_ids": []}, 'clear_fields=["custom_asset_ids"]'),
        ({"uptime_ids": []}, 'clear_fields=["uptime_ids"]'),
        ({"watcher_ids": [], "clear_fields": ["watcher_ids"]}, "watcher_ids: an empty list is not accepted because lists replace the stored one"),
        ({"watcher_ids": [1], "clear_fields": ["watcher_ids"]}, "watcher_ids: cannot be given a value and cleared in the same call"),
        ({"clear_fields": []}, "clear_fields: must not be an empty list"),
        ({"blocked_by_task_ids": [TASK]}, "blocked_by_task_ids: a task cannot block or be blocked by itself"),
        ({"blocking_task_ids": [BLOCKER, TASK.upper()]}, "blocking_task_ids: a task cannot block or be blocked by itself"),
        ({"title": ""}, "title: must not be empty or whitespace only"),
        # 2026-10-02: "Send an empty string to remove it" is offered only through clear_fields
        ({"banner": " "}, 'banner: a blank value is not accepted; to remove it pass clear_fields=["banner"]'),
        ({"banner": ""}, 'banner: a blank value is not accepted; to remove it pass clear_fields=["banner"]'),
        ({"banner": "New", "clear_fields": ["banner"]}, "banner: cannot be given a value and cleared in the same call"),
        ({"status_reason": ""}, "status_reason: must not be empty or whitespace only"),
        ({"title": "T", "updated_by_name": ""}, "updated_by_name: must not be empty or whitespace only"),
        ({"section_id": "backlog"}, "section_id: expected a GUID such as"),
        ({"status_id": 0}, "status_id: expected a positive whole number"),
        ({"watcher_ids": [0]}, "watcher_ids[0]: expected a positive whole number"),
        ({"blocked_by_task_ids": ["x"]}, "blocked_by_task_ids[0]: expected a GUID such as"),
        ({"agent_asset_ids": [ASSET, "y"]}, "agent_asset_ids[1]: expected a GUID such as"),
        ({"custom_asset_ids": [12]}, "custom_asset_ids"),
        ({"uptime_ids": ["z"]}, "uptime_ids[0]: expected a GUID such as"),
        ({"due_date": "2026-11-30"}, "due_date: '2026-11-30' has no UTC offset"),
    ],
)
async def test_update_project_task_local_validation_errors_name_the_param_and_send_nothing(
    server, mock_gorelo, arguments, fragment
):
    text = await call_tool_error(server, "update_project_task", {"project_id": PROJECT, "task_id": TASK, **arguments})
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_update_project_task_refuses_a_name_clear_fields_does_not_allow(server, mock_gorelo):
    # the banner is not on this list since 2026-10-02 ("Send an empty string to remove it"): it is clearable now
    for field in (
        "title", "due_date", "clear_due_date", "status_id", "status_reason", "priority_id", "section_id", "updated_by_name",
    ):
        text = await call_tool_error(server, "update_project_task", {"project_id": PROJECT, "task_id": TASK, "clear_fields": [field]})
        assert "clear_fields.0" in text and "Input should be" in text and "watcher_ids" in text, field
    assert mock_gorelo.requests == []


def test_update_project_task_offers_exactly_the_documented_removals_in_clear_fields():
    assert offered_values("update_project_task", "clear_fields") == {
        "assisting_assignee_ids", "watcher_ids", "blocked_by_task_ids", "blocking_task_ids", "agent_asset_ids",
        "custom_asset_ids", "uptime_ids", "banner", "lead_assignee_id",
    }


ALLOWED_REMOVALS = (
    "assisting_assignee_ids, watcher_ids, blocked_by_task_ids, blocking_task_ids, agent_asset_ids, custom_asset_ids, "
    "uptime_ids, banner, lead_assignee_id"
)


@pytest.mark.parametrize(
    "name", ["title", "section_id", "status_id", "status_reason", "priority_id", "due_date", "clear_due_date", "updated_by_name"]
)
async def test_calling_update_project_task_directly_cannot_clear_a_field_the_schema_does_not_offer(
    client_factory, mock_gorelo, name
):
    # The Literal of clear_fields normally pre-empts this. A direct call skips it, and every name here is a real field
    # of the body that a blank would corrupt (a date is cleared with clear_due_date, never with a blank).
    expected = f"clear_fields: '{name}' cannot be cleared; allowed: {ALLOWED_REMOVALS}"
    for names in ([name], ["watcher_ids", name]):
        with pytest.raises(ToolError) as caught:
            await call_directly(
                client_factory, module.update_project_task, project_id=PROJECT, task_id=TASK, clear_fields=names
            )
        assert str(caught.value) == expected
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad, kind", [(5, "a number"), (None, "null"), (["banner"], "a list of 1 item"), (False, "a boolean")])
async def test_calling_update_project_task_directly_refuses_a_clear_field_that_is_not_a_name(
    client_factory, mock_gorelo, bad, kind
):
    with pytest.raises(ToolError) as caught:
        await call_directly(
            client_factory, module.update_project_task, project_id=PROJECT, task_id=TASK, clear_fields=["banner", bad]
        )
    assert str(caught.value) == f'clear_fields[1]: expected a field name such as "banner", got {kind}'
    assert mock_gorelo.requests == []


async def test_calling_update_project_task_directly_still_clears_what_the_schema_offers(client_factory, mock_gorelo):
    # the direct path is not stricter than the schema path: every offered name goes through
    route_update_task(mock_gorelo)
    await call_directly(
        client_factory, module.update_project_task, project_id=PROJECT, task_id=TASK,
        clear_fields=list(module.TASK_CLEAR_VALUES),
    )
    assert mock_gorelo.requests[0].json == {
        "AssistingAssigneeIds": [], "WatcherIds": [], "BlockedByTaskIds": [], "BlockingTaskIds": [],
        "AgentAssetIds": [], "CustomAssetIds": [], "UptimeIds": [], "Banner": "", "LeadAssigneeId": 0,
    }


@pytest.mark.parametrize("field", ["assisting_assignee_ids", "watcher_ids"])
async def test_update_project_task_removes_all_assistants_or_watchers_only_through_clear_fields(server, mock_gorelo, field):
    # UpdateTaskCommand.AssistingAssigneeIds and WatcherIds: "Send an empty array to remove them all"
    route_update_task(mock_gorelo)
    await call_tool(server, "update_project_task", {**VALID["update_project_task"], "title": "T", "clear_fields": [field]})
    assert mock_gorelo.requests[0].json == {"Title": "T", module.UPDATE_TASK_MAP[field]: []}
    mock_gorelo.reset()
    text = await call_tool_error(server, "update_project_task", {**VALID["update_project_task"], field: []})
    assert text == (
        f'{field}: an empty list is not accepted because lists replace the stored one; to remove every entry pass '
        f'clear_fields=["{field}"]'
    )
    assert mock_gorelo.requests == []


async def test_update_project_task_removes_the_lead_only_through_clear_fields_never_with_a_bare_zero(server, mock_gorelo):
    route_update_task(mock_gorelo)
    await call_tool(server, "update_project_task", {"project_id": PROJECT, "task_id": TASK, "clear_fields": ["lead_assignee_id"]})
    assert mock_gorelo.requests[0].json == {"LeadAssigneeId": 0}
    mock_gorelo.reset()
    # a bare 0 in lead_assignee_id is refused locally and points to the opt-in; so is any other non-id
    text = await call_tool_error(server, "update_project_task", {**VALID["update_project_task"], "lead_assignee_id": 0})
    assert text == 'lead_assignee_id: 0 is not a technician id; to remove the lead pass clear_fields=["lead_assignee_id"]'
    for lead in (-1, -9201):
        text = await call_tool_error(server, "update_project_task", {**VALID["update_project_task"], "lead_assignee_id": lead})
        assert text.startswith("lead_assignee_id: expected a positive whole number such as 123, got zero or a negative number")
    # a new lead and the removal of the lead cannot be asked for together
    text = await call_tool_error(
        server, "update_project_task",
        {**VALID["update_project_task"], "lead_assignee_id": 9201, "clear_fields": ["lead_assignee_id"]},
    )
    assert text == "lead_assignee_id: cannot be given a value and cleared in the same call"
    assert mock_gorelo.requests == []


async def test_update_project_task_removes_the_banner_only_through_clear_fields(server, mock_gorelo):
    route_update_task(mock_gorelo)
    await call_tool(server, "update_project_task", {"project_id": PROJECT, "task_id": TASK, "clear_fields": ["banner"]})
    assert mock_gorelo.requests[0].json == {"Banner": ""}
    mock_gorelo.reset()
    for blank in ("", " ", "\n"):
        text = await call_tool_error(server, "update_project_task", {**VALID["update_project_task"], "banner": blank})
        assert text == 'banner: a blank value is not accepted; to remove it pass clear_fields=["banner"]'
    assert mock_gorelo.requests == []


async def test_update_project_task_status_reason_is_only_sent_with_the_status(server, mock_gorelo):
    route_update_task(mock_gorelo)
    await call_tool(server, "update_project_task", {"project_id": PROJECT, "task_id": TASK, "status_id": 3, "status_reason": "Parts"})
    assert mock_gorelo.requests[0].json == {"StatusId": 3, "StatusReason": "Parts"}
    mock_gorelo.reset()
    text = await call_tool_error(server, "update_project_task", {"project_id": PROJECT, "task_id": TASK, "status_reason": "Parts"})
    assert text == "status_reason: only recorded together with status_id; pass the status as well"
    assert mock_gorelo.requests == []
    # the status alone is fine
    await call_tool(server, "update_project_task", {"project_id": PROJECT, "task_id": TASK, "status_id": 4})
    assert mock_gorelo.requests[0].json == {"StatusId": 4}


async def test_create_project_task_refuses_lead_zero(server, mock_gorelo):
    text = await call_tool_error(server, "create_project_task", {**VALID["create_project_task"], "lead_assignee_id": 0})
    assert text.startswith("lead_assignee_id: expected a positive whole number such as 123, got zero or a negative number")
    assert mock_gorelo.requests == []


async def test_update_project_task_maps_gorelo_errors_to_the_param(server, mock_gorelo):
    mock_gorelo.on(
        "PATCH", TASK_URL,
        error_envelope(
            400,
            [
                ("070101", "A blocking task is in another project.", "BlockingTaskIds"),
                ("070101", "Unknown asset.", "AgentAssetIds"),
                ("070101", "Unknown status.", "StatusId"),
            ],
        ),
    )
    text = await call_tool_error(server, "update_project_task", {"project_id": PROJECT, "task_id": TASK, "status_id": 9})
    assert text == (
        "Gorelo rejected update_project_task (HTTP 400, code 070101): blocking_task_ids: A blocking task is in "
        f"another project.; agent_asset_ids: Unknown asset.; status_id: Unknown status. [trace {TEST_TRACE_ID}]"
    )
    assert sent(mock_gorelo) == [("PATCH", TASK_URL)]


@pytest.mark.parametrize("data", [None, {}, False, True, [TASK], "ok"])
async def test_update_project_task_a_patch_that_answers_with_anything_but_an_object_is_an_unconfirmed_write(server, mock_gorelo, data):
    mock_gorelo.on("PATCH", TASK_URL, envelope(data))
    text = await call_tool_error(server, "update_project_task", {"project_id": PROJECT, "task_id": TASK, "title": "T"})
    assert text.startswith(
        "Gorelo returned an unexpected response for update_project_task: PATCH /v1/projects/{projectId}/tasks/{taskId}: "
        "expected Data to be a non-empty object but got "
    )
    assert VERIFY in text
    assert sent(mock_gorelo) == [("PATCH", TASK_URL)]  # no re-read of a write that cannot be confirmed


async def test_update_project_task_unknown_task_is_a_404(server, mock_gorelo):
    mock_gorelo.on("PATCH", TASK_URL, error_envelope(404, [("070401", "Task was not found.")]))
    text = await call_tool_error(server, "update_project_task", {"project_id": PROJECT, "task_id": TASK, "title": "T"})
    assert text.startswith("Gorelo rejected update_project_task (HTTP 404, code 070401): Task was not found.")
    assert sent(mock_gorelo) == [("PATCH", TASK_URL)]


async def test_update_project_task_a_failed_reread_returns_the_warning_and_never_repeats_the_write(server, mock_gorelo):
    mock_gorelo.on("PATCH", TASK_URL, envelope({"Id": TASK}))
    mock_gorelo.on("GET", TASK_URL, httpx.ReadTimeout("slow"))
    result = await call_tool(server, "update_project_task", {"project_id": PROJECT, "task_id": TASK, "title": "T"})
    assert set(result) == {"Id", "warning"} and result["Id"] == TASK
    assert "GET /v1/projects/{projectId}/tasks/{taskId} timed out" in result["warning"]
    assert result["warning"].endswith("Do not repeat the write; read it again later.")
    assert [r.method for r in mock_gorelo.requests] == ["PATCH", "GET"]


async def test_update_project_task_a_timeout_on_the_patch_is_unconfirmed(server, mock_gorelo):
    mock_gorelo.on("PATCH", TASK_URL, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_project_task", {"project_id": PROJECT, "task_id": TASK, "title": "T"})
    assert text.startswith("Gorelo did not confirm update_project_task (the request timed out).")
    assert "Verify with a read before retrying." in text
    assert sent(mock_gorelo) == [("PATCH", TASK_URL)]


def test_update_project_task_docstring_states_what_replaces_what():
    doc = " ".join(specs()["update_project_task"].fn.__doc__.split())
    for fragment in (
        "REPLACE the stored list", "only through clear_fields", "at least one change is required",
        "Changing status_id fires the same automation the app fires", "do not repeat it",
        # 2026-10-02: what clear_fields removes besides the lists, and the StatusReason pairing
        "The banner and the lead are removed only through clear_fields too",
        "status_reason is recorded only together with status_id",
    ):
        assert fragment in doc, fragment
    assert "removes the due date" in param_description("update_project_task", "clear_due_date")
    lead = param_description("update_project_task", "lead_assignee_id")
    assert "To remove the lead use clear_fields, never 0" in lead
    assert "The lead cannot be removed" not in lead  # Gorelo documents "Send 0 to remove the lead"
    assert "To remove the banner use clear_fields" in param_description("update_project_task", "banner")
    section = param_description("update_project_task", "section_id")
    assert "list_project_sections" in section and "active section of this same project" in section
    reason = param_description("update_project_task", "status_reason")
    assert "only when status_id is sent" in reason and "give both" in reason
    for param in ("assisting_assignee_ids", "watcher_ids"):
        text = param_description("update_project_task", param)
        assert "complete new list" in text and "use clear_fields" in text, param
    removable = param_description("update_project_task", "clear_fields")
    for name in ("banner", "lead_assignee_id", "assisting_assignee_ids", "watcher_ids"):
        assert name in removable, name


def test_the_task_write_params_that_state_a_default_or_a_name_follow_the_schema_texts():
    # 2026-10-02: CreatedByName (API when omitted), CreatedOn (now) and UpdatedOn (CreatedOn) of CreateTaskCommand
    assert "Gorelo records API" in param_description("create_project_task", "created_by_name")
    created = param_description("create_project_task", "created_on")
    assert "omitted, now" in created and "importing a task that existed elsewhere" in created
    assert "omitted, it equals created_on" in param_description("create_project_task", "updated_on")


@needs_spec_text
def test_the_author_of_a_task_update_is_recorded_as_api_when_omitted_and_the_param_says_so():
    # UpdateTaskCommand.UpdatedByName: "An API key is not a person, so when it is omitted or blank the actor is
    # recorded as API". The tool leaves it out unless the caller gives it and refuses a blank one.
    assert "omitted or blank the actor is recorded as `API`" in schema_fields("UpdateTaskCommand")["UpdatedByName"]
    text = param_description("update_project_task", "updated_by_name")
    assert "omitted, Gorelo records API (an API key is not a person)" in text and "Not a change by itself" in text


async def test_a_task_update_sends_no_author_unless_the_caller_gave_one(server, mock_gorelo):
    route_update_task(mock_gorelo)
    await call_tool(server, "update_project_task", {"project_id": PROJECT, "task_id": TASK, "title": "T"})
    assert mock_gorelo.requests[0].json == {"Title": "T"}


# --------------------------------------------------------------------------
# delete_project_task
# --------------------------------------------------------------------------


async def test_delete_project_task_refuses_without_confirm_and_makes_no_http_call(dserver, mock_gorelo):
    text = await call_tool_error(dserver, "delete_project_task", {"project_id": PROJECT, "task_id": TASK})
    assert text.startswith(f"confirm: refusing to delete task {TASK} without confirm=true.")
    assert "clears its blocking relationships in both directions" in text
    assert f"Call again with confirm=true if you really want to delete task {TASK}." in text
    assert mock_gorelo.requests == []


async def test_delete_project_task_confirm_false_is_refused_too(dserver, mock_gorelo):
    text = await call_tool_error(dserver, "delete_project_task", {"project_id": PROJECT, "task_id": TASK, "confirm": False})
    assert text.startswith("confirm: refusing to delete task")
    assert mock_gorelo.requests == []


async def test_delete_project_task_with_confirm_sends_one_delete(dserver, mock_gorelo):
    mock_gorelo.on("DELETE", TASK_URL, envelope({"Id": TASK}))
    result = await call_tool(dserver, "delete_project_task", {"project_id": PROJECT, "task_id": TASK, "confirm": True})
    assert result == {"Id": TASK}
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("DELETE", TASK_URL, {}, None)
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "data, kind",
    [(None, "null"), (True, "a boolean"), (False, "a boolean"), ({}, "an empty object"), ([TASK], "a list of 1 item"), ("ok", "a string")],
)
async def test_delete_project_task_an_answer_that_is_not_an_object_is_an_unconfirmed_write(dserver, mock_gorelo, data, kind):
    # a delete answers with Gorelo's Data object ({"Id": ...}); null, a bare boolean or {} is never success.
    mock_gorelo.on("DELETE", TASK_URL, envelope(data))
    text = await call_tool_error(dserver, "delete_project_task", {"project_id": PROJECT, "task_id": TASK, "confirm": True})
    assert text == (
        "Gorelo returned an unexpected response for delete_project_task: DELETE /v1/projects/{projectId}/tasks/{taskId}: "
        f"expected Data to be a non-empty object but got {kind}; {VERIFY}"
    )
    assert len(mock_gorelo.requests) == 1


async def test_delete_project_task_unknown_task_is_a_404(dserver, mock_gorelo):
    mock_gorelo.on("DELETE", TASK_URL, error_envelope(404, [("070401", "Task was not found.")]))
    text = await call_tool_error(dserver, "delete_project_task", {"project_id": PROJECT, "task_id": TASK, "confirm": True})
    assert text == f"Gorelo rejected delete_project_task (HTTP 404, code 070401): Task was not found. [trace {TEST_TRACE_ID}]"


async def test_delete_project_task_a_server_error_says_it_may_have_been_applied(dserver, mock_gorelo):
    mock_gorelo.on("DELETE", TASK_URL, error_envelope(500, [("070001", "Internal error.")]))
    text = await call_tool_error(dserver, "delete_project_task", {"project_id": PROJECT, "task_id": TASK, "confirm": True})
    assert "Gorelo may have applied the change before failing. Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1


def test_delete_project_task_docstring_states_what_is_deleted():
    doc = " ".join(specs()["delete_project_task"].fn.__doc__.split())
    for fragment in (
        "soft delete", "recovered in the app", "cleared in both directions", "automation timer", "nothing is emailed",
        "Ask the user first; needs confirm=true.",  # the consistency rule of every destructive tool
    ):
        assert fragment in doc, fragment
    assert "Ask the user first" in param_description("delete_project_task", "confirm")


# --------------------------------------------------------------------------
# list_task_conversations
# --------------------------------------------------------------------------


def conversation_rows():
    return [
        {"Id": None, "Type": {"Id": 1, "Name": "Public"}, "Name": "Public", "Email": None, "CcEmails": [], "CreatedOn": "2026-10-01T14:30:00Z"},
        {"Id": None, "Type": {"Id": 2, "Name": "Private"}, "Name": "Private", "Email": None, "CcEmails": [], "CreatedOn": "2026-10-01T14:30:00Z"},
        {"Id": CONVERSATION, "Type": {"Id": 3, "Name": "Side Conversation"}, "Name": "Vendor", "Email": "vendor@example.com", "CcEmails": ["boss@example.com"], "CreatedOn": "2026-10-01T15:00:00Z"},
        {"Id": APPROVAL, "Type": {"Id": 4, "Name": "Approval"}, "Name": "Approve the downtime", "Email": None, "CcEmails": [], "CreatedOn": "2026-10-01T16:00:00Z"},
    ]


async def test_list_task_conversations_is_unpaged_and_returns_the_list_shape(server, mock_gorelo):
    rows = conversation_rows()
    mock_gorelo.on("GET", f"{TASK_URL}/conversations", envelope(rows))
    assert await call_tool(server, "list_task_conversations", {"project_id": PROJECT, "task_id": TASK}) == {"items": rows, "count": 4}
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", f"{TASK_URL}/conversations", {}, None)


async def test_list_task_conversations_an_empty_list_is_zero_items(server, mock_gorelo):
    mock_gorelo.on("GET", f"{TASK_URL}/conversations", envelope([]))
    assert await call_tool(server, "list_task_conversations", {"project_id": PROJECT, "task_id": TASK}) == {"items": [], "count": 0}


async def test_list_task_conversations_refuses_an_answer_that_is_not_a_list(server, mock_gorelo):
    mock_gorelo.on("GET", f"{TASK_URL}/conversations", envelope(None))
    text = await call_tool_error(server, "list_task_conversations", {"project_id": PROJECT, "task_id": TASK})
    assert "Gorelo returned an unexpected response for list_task_conversations" in text and "expected Data to be a list" in text


async def test_list_task_conversations_takes_no_paging_arguments(server, mock_gorelo):
    text = await call_tool_error(server, "list_task_conversations", {"project_id": PROJECT, "task_id": TASK, "page_size": 5})
    assert "page_size" in text
    assert mock_gorelo.requests == []


async def test_list_task_conversations_unknown_task_is_a_404(server, mock_gorelo):
    mock_gorelo.on("GET", f"{TASK_URL}/conversations", error_envelope(404, [("070401", "Task was not found.")]))
    text = await call_tool_error(server, "list_task_conversations", {"project_id": PROJECT, "task_id": TASK})
    assert text.startswith("Gorelo rejected list_task_conversations (HTTP 404, code 070401): Task was not found.")


def test_list_task_conversations_docstring_explains_which_rows_carry_an_id():
    doc = " ".join(specs()["list_task_conversations"].fn.__doc__.split())
    for fragment in ("Public and Private (the main thread) have a null Id", "conversation_id", "approval_id", "get_task_approval"):
        assert fragment in doc, fragment


# --------------------------------------------------------------------------
# create_task_side_conversation
# --------------------------------------------------------------------------


async def test_create_task_side_conversation_sends_the_pascal_case_body_and_points_to_the_list_tool(server, mock_gorelo):
    path = f"{TASK_URL}/conversations/side-conversation"
    mock_gorelo.on("POST", path, envelope({"Id": CONVERSATION}))
    result = await call_tool(
        server,
        "create_task_side_conversation",
        {
            "project_id": PROJECT, "task_id": TASK, "name": "Vendor RMA", "email": "vendor@example.com",
            "cc_emails": ["boss@example.com", "ops@example.com"],
        },
    )
    request = mock_gorelo.last
    assert (request.method, request.path, request.query) == ("POST", path, {})
    assert request.json == {"Name": "Vendor RMA", "Email": "vendor@example.com", "CcEmails": ["boss@example.com", "ops@example.com"]}
    assert result == {"Id": CONVERSATION, "note": module.SIDE_CONVERSATION_NOTE}
    assert "Nothing is sent until you post a comment into it" in result["note"] and "list_task_conversations" in result["note"]
    assert len(mock_gorelo.requests) == 1  # there is no single-conversation read to call


async def test_create_task_side_conversation_without_cc_sends_no_cc_field(server, mock_gorelo):
    mock_gorelo.on("POST", f"{TASK_URL}/conversations/side-conversation", envelope({"Id": CONVERSATION}))
    await call_tool(server, "create_task_side_conversation", VALID["create_task_side_conversation"])
    assert mock_gorelo.last.json == {"Name": "Vendor", "Email": "vendor@example.com"}


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"name": ""}, "name: must not be empty or whitespace only"),
        ({"name": "  "}, "name: must not be empty or whitespace only"),
        ({"email": ""}, "email: expected one email address such as vendor@example.com"),
        ({"email": "vendor"}, "email: expected one email address"),
        ({"email": "vendor@example.com, ops@example.com"}, "email: expected one email address"),
        ({"email": "a@b.test;c@d.test"}, "email: expected one email address"),
        ({"email": "Vendor <vendor@example.com>"}, "email: expected one email address"),
        ({"email": "two@@example.com"}, "email: expected one email address"),
        ({"email": "@example.com"}, "email: expected one email address"),
        ({"email": "vendor@"}, "email: expected one email address"),
        ({"cc_emails": []}, "cc_emails: must not be an empty list"),
        ({"cc_emails": ["ok@example.com", "bad"]}, "cc_emails[1]: expected one email address"),
        ({"cc_emails": ["a@b.test, c@d.test"]}, "cc_emails[0]: expected one email address"),
    ],
)
async def test_create_task_side_conversation_local_validation_errors_send_nothing(server, mock_gorelo, arguments, fragment):
    text = await call_tool_error(server, "create_task_side_conversation", {**VALID["create_task_side_conversation"], **arguments})
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_create_task_side_conversation_never_repeats_a_bad_address_in_the_error(server, mock_gorelo):
    # The address belongs to a third party and tool errors reach the logs, so the message must not quote it.
    call = {**VALID["create_task_side_conversation"], "email": "jane.doe@example.com, john@example.com"}
    assert "jane.doe" not in await call_tool_error(server, "create_task_side_conversation", call)
    call = {**VALID["create_task_side_conversation"], "cc_emails": ["not an address jane.doe@example.com"]}
    text = await call_tool_error(server, "create_task_side_conversation", call)
    assert text.startswith("cc_emails[0]: expected one email address") and "jane.doe" not in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("missing", ["name", "email"])
async def test_create_task_side_conversation_requires_name_and_email(server, mock_gorelo, missing):
    call = {k: v for k, v in VALID["create_task_side_conversation"].items() if k != missing}
    text = await call_tool_error(server, "create_task_side_conversation", call)
    assert missing in text and "Missing required argument" in text
    assert mock_gorelo.requests == []


async def test_create_task_side_conversation_maps_gorelo_errors_to_the_param(server, mock_gorelo):
    mock_gorelo.on(
        "POST", f"{TASK_URL}/conversations/side-conversation",
        error_envelope(400, [("070101", "Email is not valid.", "Email"), ("070101", "A cc address is not valid.", "CcEmails[0]")]),
    )
    text = await call_tool_error(server, "create_task_side_conversation", VALID["create_task_side_conversation"])
    assert text == (
        "Gorelo rejected create_task_side_conversation (HTTP 400, code 070101): email: Email is not valid.; "
        f"cc_emails: A cc address is not valid. [trace {TEST_TRACE_ID}]"
    )


@pytest.mark.parametrize("data", [None, {}, {"Id": ""}, {"Id": 0}, {"Id": True}, [CONVERSATION], False])
async def test_create_task_side_conversation_an_answer_without_an_id_is_an_unconfirmed_write(server, mock_gorelo, data):
    mock_gorelo.on("POST", f"{TASK_URL}/conversations/side-conversation", envelope(data))
    text = await call_tool_error(server, "create_task_side_conversation", VALID["create_task_side_conversation"])
    assert UNUSABLE_ID in text and VERIFY in text
    assert len(mock_gorelo.requests) == 1


async def test_create_task_side_conversation_a_timeout_is_unconfirmed_and_not_retried(server, mock_gorelo):
    mock_gorelo.on("POST", f"{TASK_URL}/conversations/side-conversation", httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "create_task_side_conversation", VALID["create_task_side_conversation"])
    assert text.startswith("Gorelo did not confirm create_task_side_conversation (the request timed out).")
    assert len(mock_gorelo.requests) == 1


def test_create_task_side_conversation_name_is_the_person_the_conversation_is_with():
    # CreateTaskSideConversationCommand.Name: "Display name of the person the conversation is with" (the tool used to
    # describe it as the name of the conversation)
    name = param_description("create_task_side_conversation", "name")
    assert name.startswith("Display name of the person the conversation is with")
    assert "Name of the side conversation" not in name
    email = param_description("create_task_side_conversation", "email")
    assert "That person's email address" in email and "outside the task's own contacts" in email
    assert "Addresses to copy on the conversation" in param_description("create_task_side_conversation", "cc_emails")


def test_create_task_side_conversation_docstring_says_nothing_is_sent_until_a_comment_is_posted():
    doc = " ".join(specs()["create_task_side_conversation"].fn.__doc__.split())
    for fragment in (
        "creates the conversation empty and emails nobody",
        "Nothing is emailed until a comment is posted into it", 'conversation_type="side_conversation"',
        "list_task_conversations",
    ):
        assert fragment in doc, fragment


# --------------------------------------------------------------------------
# create_task_approval and get_task_approval
# --------------------------------------------------------------------------


async def test_create_task_approval_sends_the_body_and_returns_the_reread_approval(server, mock_gorelo):
    record = approval_detail()
    mock_gorelo.on("POST", f"{TASK_URL}/conversations/approval", envelope({"Id": APPROVAL}))
    mock_gorelo.on("GET", f"{TASK_URL}/approvals/{APPROVAL}", envelope(record))
    result = await call_tool(
        server, "create_task_approval",
        {"project_id": PROJECT, "task_id": TASK, "name": "Approve the downtime", "contact_ids": [9103, 9104]},
    )
    assert result == record
    assert sent(mock_gorelo) == [("POST", f"{TASK_URL}/conversations/approval"), ("GET", f"{TASK_URL}/approvals/{APPROVAL}")]
    assert mock_gorelo.requests[0].json == {"Name": "Approve the downtime", "ContactIds": [9103, 9104]}
    assert mock_gorelo.requests[0].query == {} and mock_gorelo.requests[1].query == {}


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"name": ""}, "name: must not be empty or whitespace only"),
        ({"name": "   "}, "name: must not be empty or whitespace only"),
        ({"contact_ids": []}, "contact_ids: give at least one contact id (an approval needs approvers)"),
        ({"contact_ids": [0]}, "contact_ids[0]: expected a positive whole number such as 123, got zero or a negative number"),
        ({"contact_ids": [9103, -3]}, "contact_ids[1]: expected a positive whole number"),
    ],
)
async def test_create_task_approval_local_validation_errors_send_nothing(server, mock_gorelo, arguments, fragment):
    text = await call_tool_error(server, "create_task_approval", {**VALID["create_task_approval"], **arguments})
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("missing", ["name", "contact_ids"])
async def test_create_task_approval_requires_name_and_contact_ids(server, mock_gorelo, missing):
    call = {k: v for k, v in VALID["create_task_approval"].items() if k != missing}
    text = await call_tool_error(server, "create_task_approval", call)
    assert missing in text and "Missing required argument" in text
    assert mock_gorelo.requests == []


async def test_create_task_approval_an_ineligible_contact_is_reported_against_contact_ids(server, mock_gorelo):
    mock_gorelo.on(
        "POST", f"{TASK_URL}/conversations/approval",
        error_envelope(400, [("070101", "Contact 9103 is not an approver of this task's client.", "ContactIds"), ("070101", "Name is required.", "Name")]),
    )
    text = await call_tool_error(server, "create_task_approval", VALID["create_task_approval"])
    assert text == (
        "Gorelo rejected create_task_approval (HTTP 400, code 070101): contact_ids: Contact 9103 is not an "
        f"approver of this task's client.; name: Name is required. [trace {TEST_TRACE_ID}]"
    )
    assert sent(mock_gorelo) == [("POST", f"{TASK_URL}/conversations/approval")]


async def test_create_task_approval_a_failed_reread_returns_the_warning_and_never_repeats_the_write(server, mock_gorelo):
    mock_gorelo.on("POST", f"{TASK_URL}/conversations/approval", envelope({"Id": APPROVAL}))
    mock_gorelo.on("GET", f"{TASK_URL}/approvals/{APPROVAL}", error_envelope(404, [("070401", "Approval was not found.")]))
    result = await call_tool(server, "create_task_approval", VALID["create_task_approval"])
    assert set(result) == {"Id", "warning"} and result["Id"] == APPROVAL
    assert result["warning"].startswith("the write succeeded; re-reading it failed: GET /v1/projects/{projectId}/tasks/{taskId}/approvals/{approvalId} answered HTTP 404")
    assert result["warning"].endswith("Do not repeat the write; read it again later.")
    assert [r.method for r in mock_gorelo.requests] == ["POST", "GET"]


@pytest.mark.parametrize("data", [None, {}, {"Id": None}, {"Id": ""}, {"Id": 0}, {"Id": True}, [APPROVAL], False])
async def test_create_task_approval_an_answer_without_an_id_is_an_unconfirmed_write(server, mock_gorelo, data):
    mock_gorelo.on("POST", f"{TASK_URL}/conversations/approval", envelope(data))
    text = await call_tool_error(server, "create_task_approval", VALID["create_task_approval"])
    assert UNUSABLE_ID in text and VERIFY in text
    assert len(mock_gorelo.requests) == 1  # nothing is read back or created a second time


async def test_create_task_approval_a_timeout_is_unconfirmed_and_not_retried(server, mock_gorelo):
    mock_gorelo.on("POST", f"{TASK_URL}/conversations/approval", httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "create_task_approval", VALID["create_task_approval"])
    assert text.startswith("Gorelo did not confirm create_task_approval (the request timed out).")
    assert len(mock_gorelo.requests) == 1


def test_create_task_approval_contact_ids_text_names_who_is_eligible():
    text = param_description("create_task_approval", "contact_ids")
    assert "active contacts of the task's client" in text and "approver contact tag" in text and "At least one" in text
    assert "list_contacts" in text


def test_create_task_approval_docstring_states_who_may_approve_and_what_it_triggers():
    doc = " ".join(specs()["create_task_approval"].fn.__doc__.split())
    for fragment in (
        "active contact of the task's client AND carry an approver contact tag",
        "tags are set in the Gorelo UI (the API cannot set them)", "rejected before anything is written",
        "waiting-on-contact state", "Nothing is emailed until a comment is posted into the approval",
        'conversation_type="approval"', "Approvers start Pending", "get_task_approval", "do not create it again",
    ):
        assert fragment in doc, fragment


async def test_get_task_approval_returns_the_record_unchanged(server, mock_gorelo):
    record = approval_detail(Status={"Id": 2, "Name": "Approved"})
    mock_gorelo.on("GET", f"{TASK_URL}/approvals/{APPROVAL}", envelope(record))
    assert await call_tool(server, "get_task_approval", {"project_id": PROJECT, "task_id": TASK, "approval_id": APPROVAL}) == record
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", f"{TASK_URL}/approvals/{APPROVAL}", {}, None)


async def test_get_task_approval_unknown_approval_is_a_404(server, mock_gorelo):
    mock_gorelo.on("GET", f"{TASK_URL}/approvals/{APPROVAL}", error_envelope(404, [("070401", "Approval was not found.")]))
    text = await call_tool_error(server, "get_task_approval", {"project_id": PROJECT, "task_id": TASK, "approval_id": APPROVAL})
    assert text == f"Gorelo rejected get_task_approval (HTTP 404, code 070401): Approval was not found. [trace {TEST_TRACE_ID}]"


def test_get_task_approval_docstring_says_this_is_where_the_status_lives():
    doc = " ".join(specs()["get_task_approval"].fn.__doc__.split())
    for fragment in ("This is where the approval status lives", "Disapproved as soon as anyone disapproves", "Approved once everyone approves", "Pending otherwise"):
        assert fragment in doc, fragment
