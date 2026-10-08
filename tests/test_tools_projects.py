"""tools/projects.py: projects, sections, tags, types and project or task comments. Offline only.

An API key without the Project scope gets a 403 (code 080203), so every behaviour here is checked
against MockGorelo with realistic PascalCase envelopes: exact method, path, query names and body, result
shapes, Gorelo errors mapped to the snake_case param, each local validation error with zero HTTP calls,
and the destructive gate.
"""

import json
import logging
import re
from datetime import datetime
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

import tools.projects as module
from gorelo_client import is_forbidden_op
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio

# spec/spec_index.json carries no description text, so the enumerations the tools offer (sort values, status
# ids, conversation types) are pinned to the operation text of the full OpenAPI snapshot.
LATEST_SPEC = Path(__file__).resolve().parent.parent / "backups" / "swagger-latest.json"
needs_spec_text = pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")

PROJECT, TASK, SECTION, COMMENT, CONVERSATION = uid(1), uid(2), uid(3), uid(4), uid(5)
TYPE_ID, TAG_ID, TAG_ID_2 = uid(21), uid(22), uid(23)
SCOPE_NOTE = ("080203", "API key does not have 'Project' scope")
# What created_id() and expect_object() say about a write whose answer cannot be used.
UNUSABLE_ID = "Gorelo reported success but the answer carries no usable Id for the record"
VERIFY = "the write may have been applied, so verify it with a read before repeating it"


def assert_delete_advice(text, how=None):
    """ repeating the delete is safe (idempotent), and a read cannot verify it (deleted comments still show)."""
    assert text.startswith("Gorelo did not confirm delete_project_comment (")
    if how is not None:
        assert text.startswith(f"Gorelo did not confirm delete_project_comment ({how}). "), text
    assert "The comment may already be deleted." in text
    assert "Repeating the delete is safe (it is idempotent: deleting an already deleted comment succeeds)." in text
    assert "Do not try to verify it with a read: Gorelo may still return deleted comments." in text
    # the generic advice would send the model to a read that proves nothing
    assert "Verify with a read before retrying" not in text and VERIFY not in text
    assert "retrying is safe" not in text and "may or may not have been applied" not in text


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


def trace_text(text):
    return f"{text} [trace {TEST_TRACE_ID}]"


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


def project_row(**changes):
    record = {
        "Id": PROJECT, "Title": "Office move", "Number": 462, "DisplayNumber": "PRO-462", "ClientId": 9101,
        "LocationId": 9001, "LeadAssigneeId": 9201, "GroupIds": [7201], "PrimaryGroupId": 7201,
        "Status": {"Id": 1, "Name": "NotStarted"}, "StatusUpdatedOn": None, "StatusReason": "",
        "Type": {"Id": TYPE_ID, "Name": "Migration"}, "TagIds": [TAG_ID], "TargetDate": None,
        "ProgressPercent": 0.0, "LastUpdate": None, "CreatedOn": "2026-10-01T14:30:00Z", "UpdatedOn": None,
        "ClosedOn": None,
    }
    record.update(changes)
    return record


def project_detail(**changes):
    record = project_row(Description="Move the Springfield office", SharedWithContactIds=[9103], WatcherIds=[9202])
    record.update(changes)
    return record


def comment_record(comment_id=COMMENT, **changes):
    record = {
        "Id": comment_id, "ConversationId": None, "ConversationType": {"Id": 2, "Name": "Private"},
        "BodyHtml": "<p>Note</p>", "BodyText": None, "Author": {"Id": 9201, "Name": "Alex Example"},
        "Attachments": [], "Reactions": [], "CreatedOn": "2026-10-01T15:00:00Z", "UpdatedOn": None,
    }
    record.update(changes)
    return record


# One valid call per tool, used by the generic tests below.
VALID = {
    "list_projects": {},
    "get_project": {"project_id": PROJECT},
    "create_project": {"title": "Office move", "client_id": 9101, "location_id": 9001, "type_id": TYPE_ID, "group_id": 7201},
    "update_project": {"project_id": PROJECT, "title": "New title"},
    "list_project_tags": {},
    "list_project_types": {},
    "list_project_sections": {"project_id": PROJECT},
    "create_project_section": {"project_id": PROJECT, "title": "Backlog"},
    "update_project_section": {"project_id": PROJECT, "section_id": SECTION, "title": "Done"},
    "list_project_comments": {"project_id": PROJECT},
    "get_project_comment": {"project_id": PROJECT, "comment_id": COMMENT},
    "create_project_comment": {"project_id": PROJECT, "body": "<p>Hi</p>"},
    "delete_project_comment": {"project_id": PROJECT, "comment_id": COMMENT, "confirm": True},
}

LIST_PROJECTS = "GET /v1/projects"
GET_PROJECT = "GET /v1/projects/{projectId}"
CREATE_PROJECT = "POST /v1/projects"
UPDATE_PROJECT = "PATCH /v1/projects/{projectId}"
SECTIONS = "/v1/projects/{projectId}/sections"
PC = "/v1/projects/{projectId}/comments"
TC = "/v1/projects/{projectId}/tasks/{taskId}/comments"

# name -> (kind, ops exactly as the module lists them, destructiveHint)
EXPECTED = {
    "list_projects": ("read", [LIST_PROJECTS], False),
    "get_project": ("read", [GET_PROJECT], False),
    "create_project": ("write", [CREATE_PROJECT, GET_PROJECT], False),
    "update_project": ("write", [UPDATE_PROJECT, GET_PROJECT], True),
    "list_project_tags": ("read", ["GET /v1/projects/tags"], False),
    "list_project_types": ("read", ["GET /v1/projects/types"], False),
    "list_project_sections": ("read", [f"GET {SECTIONS}"], False),
    "create_project_section": ("write", [f"POST {SECTIONS}"], False),
    "update_project_section": ("write", [f"PATCH {SECTIONS}/{{sectionId}}"], True),
    "list_project_comments": ("read", [f"GET {PC}", f"GET {TC}"], False),
    "get_project_comment": ("read", [f"GET {PC}/{{commentId}}", f"GET {TC}/{{commentId}}"], False),
    "create_project_comment": (
        "write", [f"POST {PC}", f"POST {TC}", f"GET {PC}/{{commentId}}", f"GET {TC}/{{commentId}}"], False,
    ),
    "delete_project_comment": ("destructive", [f"DELETE {PC}/{{commentId}}", f"DELETE {TC}/{{commentId}}"], True),
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


def test_project_delete_and_section_delete_are_not_exposed_by_any_tool():
    # They exist in the spec (and are not in FORBIDDEN_OPS), but each one soft-deletes every task under it.
    not_exposed = {"DELETE /v1/projects/{projectId}", f"DELETE {SECTIONS}/{{sectionId}}"}
    for spec in REGISTRY.specs:
        assert not not_exposed & set(spec.ops), spec.name


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
        ("create_project", CREATE_PROJECT),
        ("update_project", UPDATE_PROJECT),
        ("create_project_section", f"POST {SECTIONS}"),
        ("update_project_section", f"PATCH {SECTIONS}/{{sectionId}}"),
        ("create_project_comment", f"POST {PC}"),
        ("create_project_comment", f"POST {TC}"),
    ],
)
def test_every_body_field_of_the_spec_has_a_param(spec_index, tool, op):
    fields = set(spec_index.ops[op].body["fields"])
    assert fields == set(specs()[tool].field_map.values())


def required_params(name):
    return Tool.from_function(specs()[name].fn).parameters.get("required", [])


@pytest.mark.parametrize(
    "tool, op",
    [
        ("create_project", CREATE_PROJECT),
        ("create_project_section", f"POST {SECTIONS}"),
        ("create_project_comment", f"POST {PC}"),
        ("create_project_comment", f"POST {TC}"),
    ],
)
def test_the_required_params_of_a_create_tool_are_exactly_the_fields_the_spec_requires(spec_index, tool, op):
    # No invented defaults: a field the spec requires is a required param (the caller must give it), and a
    # param that is required here but optional in the spec would block a call Gorelo accepts.
    field_map = specs()[tool].field_map
    required_fields = {field_map[param] for param in required_params(tool) if param in field_map}
    assert required_fields == set(spec_index.ops[op].body["required"])


def test_create_project_requires_client_location_type_group_and_title_in_that_order():
    # 2026-10-02: CreateProjectCommand requires ClientId, GroupId, LocationId, TypeId and Title.
    assert required_params("create_project") == ["title", "client_id", "location_id", "type_id", "group_id"]
    assert required_params("update_project") == ["project_id"]


@pytest.mark.parametrize(
    "tool, op, left_out",
    [
        ("list_projects", LIST_PROJECTS, set()),
        ("list_project_comments", f"GET {PC}", set()),
        # SortBy of the task comment list has a single value (createdOn), so there is nothing to choose.
        ("list_project_comments", f"GET {TC}", {"SortBy"}),
    ],
)
def test_every_query_name_of_the_spec_has_a_param(spec_index, tool, op, left_out):
    names = set(spec_index.ops[op].query_params) - left_out
    assert names <= set(specs()[tool].field_map.values())


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
        if name.startswith("list_") and "Paging:" in doc:
            assert "SAME filters" in doc, name


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


@pytest.mark.parametrize("name", ["list_projects", "list_project_comments"])
def test_paged_tools_state_the_paging_rule(name):
    assert "Paging: pass next_cursor back as cursor with the SAME filters until has_more is false." in specs()[name].fn.__doc__


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


@needs_spec_text
@pytest.mark.parametrize(
    "tool, param, path, spec_param",
    [
        ("list_projects", "sort_by", "/v1/projects", "SortBy"),
        ("list_projects", "sort_order", "/v1/projects", "SortOrder"),
        ("list_project_comments", "sort_order", "/v1/projects/{projectId}/comments", "SortOrder"),
        ("list_project_comments", "sort_order", "/v1/projects/{projectId}/tasks/{taskId}/comments", "SortOrder"),
    ],
)
def test_the_values_offered_are_exactly_the_ones_the_spec_text_allows(tool, param, path, spec_param):
    allowed = set(re.findall(r"`([A-Za-z]+)`", param_text("get", path, spec_param)))
    assert allowed and offered_values(tool, param) == allowed


@needs_spec_text
def test_the_status_and_conversation_ids_in_the_descriptions_match_the_spec_text():
    assert "NotStarted=1, InProgress=2, OnHold=3, Completed=4, Closed=5" in param_text("get", "/v1/projects", "StatusIds")
    for tool, param in (("list_projects", "status_ids"), ("update_project", "status_id")):
        assert "1 NotStarted, 2 InProgress, 3 OnHold, 4 Completed, 5 Closed" in param_description(tool, param)
    kinds = param_text("get", "/v1/projects/{projectId}/tasks/{taskId}/comments", "ConversationType")
    assert "`1` Public, `2` Private, `3` Side Conversation, `4` Approval" in kinds
    assert module.CONVERSATION_TYPE_IDS == {"public": 1, "private": 2, "side_conversation": 3, "approval": 4}


@needs_spec_text
def test_the_clear_values_are_the_ones_the_spec_text_documents():
    # Contract e15cb5a18ec2: every clear option is one the field texts document, with the wire value they name.
    text = spec_text("patch", "/v1/projects/{projectId}")
    assert "send an empty array to clear one" in text and "`ClearTargetDate: true` removes the target date" in text
    fields = schema_fields("UpdateProjectCommand")
    for field in ("SharedWithContactIds", "TagIds", "WatcherIds"):
        assert "Empty array clears them" in fields[field], field
    assert all(module.PROJECT_CLEAR_VALUES[param] == [] for param in module.PROJECT_LIST_PARAMS)
    assert "Send an empty string to remove it" in fields["Description"]
    assert module.PROJECT_CLEAR_VALUES["description"] == ""
    assert "Send 0 to remove the lead" in fields["LeadAssigneeId"] and "`LeadAssigneeId: 0` removes the lead" in text
    assert module.PROJECT_CLEAR_VALUES["lead_assignee_id"] == 0
    assert set(module.PROJECT_CLEAR_VALUES) == set(module.PROJECT_LIST_PARAMS) | {"description", "lead_assignee_id"}
    assert offered_values("update_project", "clear_fields") == set(module.PROJECT_CLEAR_VALUES)
    assert "Neither field is clearable" in spec_text("patch", "/v1/projects/{projectId}/sections/{sectionId}")


@needs_spec_text
def test_group_ids_is_not_clearable_because_its_field_text_says_nothing_about_clearing_and_create_requires_a_group(
    spec_index,
):
    # UpdateProjectCommand.GroupIds: "Replaces the technician groups", with no "Empty array clears them" sentence
    # (TagIds, WatcherIds and SharedWithContactIds have one), and CreateProjectCommand requires GroupId.
    fields = schema_fields("UpdateProjectCommand")
    assert fields["GroupIds"] == "Replaces the technician groups the project belongs to."
    assert "clear" not in fields["GroupIds"].lower()
    assert "GroupId" in spec_index.ops[CREATE_PROJECT].body["required"]
    assert "group_ids" not in module.PROJECT_CLEAR_VALUES and "group_ids" not in module.PROJECT_LIST_PARAMS
    assert "group_ids" not in offered_values("update_project", "clear_fields")


@needs_spec_text
def test_the_lead_is_removed_through_clear_fields_with_the_zero_the_spec_text_documents():
    # "Send 0 to remove the lead" is Gorelo's way; the tool offers it only as the clear_fields opt-in and never as a
    # bare 0 in lead_assignee_id. The Project scope is missing, so the zero itself cannot be probed live.
    assert "`LeadAssigneeId: 0` removes the lead" in spec_text("patch", "/v1/projects/{projectId}")
    assert "lead_assignee_id" in offered_values("update_project", "clear_fields")
    assert module.PROJECT_CLEAR_VALUES["lead_assignee_id"] == 0


@needs_spec_text
def test_the_status_reason_is_only_read_with_a_status_id_says_the_schema():
    assert "Only read when `StatusId` is sent" in schema_fields("UpdateProjectCommand")["StatusReason"]


@needs_spec_text
def test_the_backdating_rules_are_the_schema_texts_and_the_tool_checks_only_the_one_that_needs_no_clock():
    create = schema_fields("CreateProjectCommand")
    assert "Must not be in the future" in create["CreatedOn"]
    assert "Must not be in the future or earlier than `CreatedOn`" in create["UpdatedOn"]
    assert "Must not be in the future or earlier than the project's `CreatedOn`" in schema_fields("UpdateProjectCommand")["ClosedOn"]
    # What the tool checks is "UpdatedOn is not earlier than CreatedOn", and only when both are given: it needs nothing
    # but what the caller sent. "Not in the future" needs the local clock and "not before the project was created"
    # needs a read of the project, so both are Gorelo's to enforce (its error names the parameter).
    # no other backdating field of this family states a rule, so none is checked for them
    for name, field in (
        ("CreateTaskCommand", "CreatedOn"), ("CreateTaskCommand", "UpdatedOn"),
        ("CreateProjectCommentCommand", "CreatedOn"), ("CreateTaskCommentCommand", "CreatedOn"),
    ):
        text = schema_fields(name)[field]
        assert "future" not in text and "earlier" not in text, (name, field)


@needs_spec_text
def test_the_documented_rules_the_docstrings_repeat_are_in_the_spec_text():
    create = spec_text("post", "/v1/projects")
    assert "created in the `NotStarted` status" in create and "numbered from the same sequence the app uses" in create
    assert "same project-created notification" in create
    # The operation text still talks about GroupId or GroupIds (stale since 2026-10-02): CreateProjectCommand has no
    # GroupIds any more (see test_the_group_of_a_new_project_is_the_one_group_id), so the tool repeats none of it.
    update = spec_text("patch", "/v1/projects/{projectId}")
    assert "needs a `LocationId` in that client" in update and "clears the shared contacts" in update
    assert "a technician cannot be both the lead and a watcher" in update
    # 2026-10-02: ClosedOn belongs to a Closed project (it was Completed), Completed carries no date.
    assert "only accepted for a project that is Closed - already, or by this same request" in update
    assert "never for a future date or one before the project was created" in update
    assert "reopening a Closed project clears it" in update and "Completed is reached automatically" in update
    assert "Sending nothing at all is a 400" in update
    # 2026-10-02: the list filters are TargetDateAfter and TargetDateBefore, a project with no date matches neither
    assert "A project with no target date matches neither bound" in param_text("get", "/v1/projects", "TargetDateAfter")
    assert "strictly before" in param_text("get", "/v1/projects", "TargetDateBefore")
    assert "`#56A0F9`" in spec_text("post", "/v1/projects/{projectId}/sections")
    assert "so `3` and `4` are rejected" in spec_text("post", "/v1/projects/{projectId}/comments")
    task_comment = spec_text("post", "/v1/projects/{projectId}/tasks/{taskId}/comments")
    assert "waiting-on-the-contact state" in task_comment and "need the `ConversationId`" in task_comment
    for path in ("/v1/projects/{projectId}/comments/{commentId}", "/v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}"):
        assert "Only a private comment can be deleted" in spec_text("delete", path)
        assert "`ResourceNotDeletable`" in spec_text("delete", path)


def schema_fields(name):
    """{field: description} of a component schema of the full OpenAPI snapshot (the field texts live there)."""
    properties = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["components"]["schemas"][name]["properties"]
    return {field: " ".join(prop.get("description", "").split()) for field, prop in properties.items()}


@needs_spec_text
def test_the_create_project_parameter_texts_match_the_field_texts_of_the_schema():
    # 2026-10-02: the schema says which fields are required, and what the optional ones mean.
    fields = schema_fields("CreateProjectCommand")
    for field in ("Title", "ClientId", "LocationId", "TypeId", "GroupId"):
        assert fields[field].endswith("Required."), field
    assert "none is assigned by default" in fields["LeadAssigneeId"]
    assert "The lead is never also a watcher" in fields["WatcherIds"]
    assert "They must belong to `ClientId`" in fields["SharedWithContactIds"]
    assert "targeted to finish" in fields["TargetDate"]
    assert "technician group the project is assigned to" in fields["GroupId"]
    assert "Display name to record as the project creator" in fields["CreatedByName"]
    assert "omitted or blank the creator is recorded as `API`" in fields["CreatedByName"]
    assert "Must not be in the future" in fields["CreatedOn"]
    assert "Defaults to the effective creation time" in fields["UpdatedOn"]


@needs_spec_text
def test_the_update_project_parameter_texts_match_the_field_texts_of_the_schema():
    fields = schema_fields("UpdateProjectCommand")
    assert "To remove it, send `ClearTargetDate`" in fields["TargetDate"]
    assert "Completed is normally reached automatically once every task is Done" in fields["StatusId"]
    assert "Closed is the manual end state and stamps `ClosedOn`" in fields["StatusId"]
    assert "Moving a Closed project to any other status clears it" in fields["StatusId"]
    assert "The project must already be Closed, or be moved into that status by this same request" in fields["ClosedOn"]
    assert "Must not be in the future or earlier than the project's `CreatedOn`" in fields["ClosedOn"]
    assert "Clears the shared contacts" in fields["ClientId"].replace("**", "")
    # the pairings and removals the tool enforces or offers (2026-10-02): each one is a sentence of the field text
    assert "Send `LocationId` with it" in fields["ClientId"]
    assert "Only read when `StatusId` is sent" in fields["StatusReason"]
    assert "Send an empty string to remove it" in fields["Description"]
    assert "Send 0 to remove the lead" in fields["LeadAssigneeId"]
    assert "A supplied date also becomes the project's `UpdatedOn`" in fields["ClosedOn"]


@needs_spec_text
def test_the_comment_and_project_reads_carry_the_fields_the_docstrings_name():
    schemas = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["components"]["schemas"]
    for name in ("ProjectCommentListItemModel", "ProjectCommentModel", "TaskCommentListItemModel", "TaskCommentModel"):
        assert "Reactions" in schemas[name]["properties"], name
    assert set(schemas["CommentReactionModel"]["properties"]) == {"ReactionId", "UserId"}
    for name in ("ProjectListItemModel", "ProjectDetailModel"):
        assert "TargetDate" in schemas[name]["properties"] and "DueDate" not in schemas[name]["properties"], name


async def test_the_annotations_tell_reads_writes_and_overwrites_apart(dserver):
    tools = {t.name: t for t in await list_tools(dserver)}
    assert set(EXPECTED) <= set(tools)
    for name, (kind, _ops, hint) in EXPECTED.items():
        annotations = tools[name].annotations
        assert annotations.readOnlyHint is (kind == "read"), name
        assert annotations.destructiveHint is hint, name
        assert annotations.openWorldHint is True, name


def test_the_private_copies_of_the_shared_helpers_are_gone():
    # tools/_common.py owns positive_id(s), guid(s), created_id, expect_object and describe_value.
    for name in ("_uuid", "_uuids", "_positive", "_positives", "_shown", "_written_id", "_deleted", "_record", "_lead"):
        assert not hasattr(module, name), name


async def test_the_comment_delete_exists_only_when_deletes_are_enabled(server, dserver):
    assert "delete_project_comment" not in {t.name for t in await list_tools(server)}
    assert "delete_project_comment" in {t.name for t in await list_tools(dserver)}
    text = await call_tool_error(server, "delete_project_comment", {**VALID["delete_project_comment"]})
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


@pytest.mark.parametrize("param", ["project_id", "task_id", "comment_id", "section_id"])
async def test_a_malformed_path_id_is_refused_locally_naming_the_param(dserver, mock_gorelo, param):
    tested = 0
    async with Client(dserver) as session:  # one lifespan for every call below
        for name in sorted(EXPECTED):
            if param not in accepted_params(name):
                continue
            for bad in ("not-a-uuid", "12345", "", "../../etc", " " + uid(9), uid(9) + "\n"):
                tested += 1
                call = {**VALID[name], "task_id": TASK, "comment_id": COMMENT, "section_id": SECTION, param: bad}
                call = {k: v for k, v in call.items() if k in accepted_params(name)}
                call.pop("confirm", None)  # the id is checked before the confirm gate
                text = await call_tool_error(session, name, call)
                assert text.startswith(f"{param}: expected a GUID such as "), (name, bad, text)
    assert tested >= 6
    assert mock_gorelo.requests == []


async def test_a_path_id_is_sent_in_canonical_lowercase_form(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}", envelope(project_detail()))
    await call_tool(server, "get_project", {"project_id": PROJECT.upper()})
    await call_tool(server, "get_project", {"project_id": PROJECT.replace("-", "")})
    assert sent(mock_gorelo) == [("GET", f"/v1/projects/{PROJECT}")] * 2


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
    assert len(tested) >= 17  # list_projects 4, create_project 6 (no group_ids since 2026-10-02), update_project 7
    assert mock_gorelo.requests == []


def test_the_integer_id_params_are_the_ones_the_documentation_lists():
    found = {param for name in EXPECTED for param, _ in integer_params(name)}
    assert found == {
        "status_ids", "client_ids", "lead_assignee_ids", "group_ids", "client_id", "location_id", "lead_assignee_id",
        "group_id", "watcher_ids", "shared_with_contact_ids", "status_id",
    }


async def test_integer_ids_are_still_accepted_as_numbers(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/projects", paged_envelope([]))
    await call_tool(server, "list_projects", {"client_ids": [9101], "status_ids": [1], "group_ids": [7201]})
    assert mock_gorelo.last.query == {"ClientIds": "9101", "StatusIds": "1", "GroupIds": "7201", "PageSize": "50"}


# --------------------------------------------------------------------------
# list_projects
# --------------------------------------------------------------------------

ALL_FILTERS = {
    "status_ids": [2, 3], "client_ids": [9101, 9102], "type_ids": [TYPE_ID.upper()], "lead_assignee_ids": [9201, 9202],
    "tag_ids": [TAG_ID, TAG_ID_2], "group_ids": [7201], "query": "office",
    "updated_since": "2026-10-01T09:30:00-05:00", "updated_before": "2026-10-02T00:00:00Z",
    "created_since": "2026-01-01T00:00:00Z", "created_before": "2026-09-30T00:00:00+02:00",
    "target_date_after": "2026-11-01T00:00:00Z", "target_date_before": "2026-12-01T00:00:00Z",
    "sort_by": "createdOn", "sort_order": "asc",
}


async def test_list_projects_sends_every_filter_under_its_spec_name(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/projects", paged_envelope([project_row()], next_cursor="c2", total_count=40))
    await call_tool(server, "list_projects", {**ALL_FILTERS, "page_size": 25, "cursor": "c1"})
    request = mock_gorelo.last
    assert (request.method, request.path) == ("GET", "/v1/projects")
    assert request.query == {
        "StatusIds": "2,3", "ClientIds": "9101,9102", "TypeIds": TYPE_ID, "LeadAssigneeIds": "9201,9202",
        "TagIds": f"{TAG_ID},{TAG_ID_2}", "GroupIds": "7201", "Query": "office",
        "UpdatedSince": "2026-10-01T14:30:00Z", "UpdatedBefore": "2026-10-02T00:00:00Z",
        "CreatedSince": "2026-01-01T00:00:00Z", "CreatedBefore": "2026-09-29T22:00:00Z",
        "TargetDateAfter": "2026-11-01T00:00:00Z", "TargetDateBefore": "2026-12-01T00:00:00Z",
        "SortBy": "createdOn", "SortOrder": "asc", "PageSize": "25", "Cursor": "c1",
    }
    assert mock_gorelo.last.json is None


async def test_list_projects_returns_the_paged_shape_and_echoes_the_filters(server, mock_gorelo):
    rows = [project_row(), project_row(Id=uid(8), Title="Second", DisplayNumber="PRO-463")]
    mock_gorelo.on("GET", "/v1/projects", paged_envelope(rows, next_cursor="c2", total_count=40))
    result = await call_tool(server, "list_projects", {**ALL_FILTERS, "page_size": 2})
    assert result == {
        "items": rows, "count": 2, "total_count": 40, "has_more": True, "next_cursor": "c2", "page_size": 2,
        "filters": {
            "status_ids": [2, 3], "client_ids": [9101, 9102], "type_ids": [TYPE_ID], "lead_assignee_ids": [9201, 9202],
            "tag_ids": [TAG_ID, TAG_ID_2], "group_ids": [7201], "query": "office",
            "updated_since": "2026-10-01T14:30:00Z", "updated_before": "2026-10-02T00:00:00Z",
            "created_since": "2026-01-01T00:00:00Z", "created_before": "2026-09-29T22:00:00Z",
            "target_date_after": "2026-11-01T00:00:00Z", "target_date_before": "2026-12-01T00:00:00Z",
            "sort_by": "createdOn", "sort_order": "asc",
        },
    }


async def test_list_projects_without_filters_sends_only_the_page_size(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/projects", paged_envelope([project_row()]))
    result = await call_tool(server, "list_projects", {})
    assert mock_gorelo.last.query == {"PageSize": "50"}
    assert result["filters"] == {} and result["has_more"] is False and result["next_cursor"] is None
    assert (result["count"], result["total_count"], result["page_size"]) == (1, 1, 50)


@pytest.mark.parametrize(
    "given, used", [(500, 200), (201, 200), (200, 200), (1, 1), (0, 1), (-7, 1), (50, 50)]
)
async def test_list_projects_clamps_the_page_size_and_reports_the_size_used(server, mock_gorelo, given, used):
    mock_gorelo.on("GET", "/v1/projects", paged_envelope([]))
    result = await call_tool(server, "list_projects", {"page_size": given})
    assert mock_gorelo.last.query["PageSize"] == str(used)
    assert result["page_size"] == used


async def test_list_projects_follows_the_cursor_with_the_same_filters(server, mock_gorelo):
    pages = [[project_row(Id=uid(10))], [project_row(Id=uid(11))]]
    mock_gorelo.on("GET", "/v1/projects", paged_responder(pages))
    first = await call_tool(server, "list_projects", {"client_ids": [9101], "page_size": 1})
    assert first["has_more"] is True and first["next_cursor"] == "c1" and first["items"][0]["Id"] == uid(10)
    second = await call_tool(
        server, "list_projects", {"client_ids": [9101], "page_size": 1, "cursor": first["next_cursor"]}
    )
    assert second["has_more"] is False and second["next_cursor"] is None and second["items"][0]["Id"] == uid(11)
    assert [r.query for r in mock_gorelo.requests] == [
        {"ClientIds": "9101", "PageSize": "1"},
        {"ClientIds": "9101", "PageSize": "1", "Cursor": "c1"},
    ]


async def test_list_projects_an_empty_page_is_reported_as_zero_rows(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/projects", paged_envelope([]))
    result = await call_tool(server, "list_projects", {"query": "nothing matches"})
    assert (result["items"], result["count"], result["total_count"], result["has_more"]) == ([], 0, 0, False)


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"status_ids": []}, "status_ids: expected at least one id, got an empty list"),
        ({"client_ids": [0]}, "client_ids[0]: expected a positive whole number such as 123, got zero or a negative number"),
        ({"lead_assignee_ids": [9201, -1]}, "lead_assignee_ids[1]: expected a positive whole number"),
        ({"client_ids": [2**63]}, "client_ids[0]: expected a positive whole number such as 123, got a number above"),
        ({"group_ids": []}, "group_ids: expected at least one id, got an empty list"),
        ({"type_ids": ["abc"]}, "type_ids[0]: expected a GUID such as"),
        ({"tag_ids": [TAG_ID, "zzz"]}, "tag_ids[1]: expected a GUID such as"),
        ({"tag_ids": []}, "tag_ids: expected at least one GUID, got an empty list"),
        ({"query": "   "}, "query: must not be empty or whitespace only"),
        ({"query": ""}, "query: must not be empty or whitespace only"),
        ({"updated_since": "2026-10-01T09:30:00"}, "updated_since: '2026-10-01T09:30:00' has no UTC offset"),
        ({"updated_before": "yesterday"}, "updated_before: 'yesterday' is not an ISO 8601 datetime"),
        ({"created_since": "2026-10-01"}, "created_since: '2026-10-01' has no UTC offset"),
        ({"created_before": "nope"}, "created_before: 'nope' is not an ISO 8601 datetime"),
        ({"target_date_after": "2026-11-01T00:00:00"}, "target_date_after: '2026-11-01T00:00:00' has no UTC offset"),
        ({"target_date_before": "soon"}, "target_date_before: 'soon' is not an ISO 8601 datetime"),
    ],
)
async def test_list_projects_local_validation_errors_name_the_param_and_send_nothing(
    server, mock_gorelo, arguments, fragment
):
    text = await call_tool_error(server, "list_projects", arguments)
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "arguments, fragments",
    [
        ({"sort_by": "title"}, ["sort_by", "'updatedOn' or 'createdOn'"]),
        ({"sort_by": "UPDATEDON"}, ["sort_by", "'updatedOn' or 'createdOn'"]),
        ({"sort_order": "up"}, ["sort_order", "'asc' or 'desc'"]),
        ({"status_ids": ["open"]}, ["status_ids.0"]),
        ({"type_ids": [9201]}, ["type_ids.0", "Input should be a valid string"]),
    ],
)
async def test_list_projects_refuses_values_the_spec_text_does_not_allow(server, mock_gorelo, arguments, fragments):
    text = await call_tool_error(server, "list_projects", arguments)
    for fragment in fragments:
        assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("old", ["due_after", "due_before"])
async def test_list_projects_has_no_due_filters_any_more(server, mock_gorelo, old):
    # 2026-10-02: DueAfter and DueBefore became TargetDateAfter and TargetDateBefore. A call that still uses the old
    # names is refused here, so a filter is never dropped silently and Gorelo never gets a query name it rejects.
    text = await call_tool_error(server, "list_projects", {old: "2026-11-01T00:00:00Z"})
    assert old in text and "Unexpected keyword argument" in text
    assert mock_gorelo.requests == []


def test_list_projects_says_a_project_without_a_target_date_matches_neither_filter():
    after = param_description("list_projects", "target_date_after")
    assert "Projects with no target date match neither target date filter" in after
    assert "strictly before" in param_description("list_projects", "target_date_before")
    assert "TargetDate" in " ".join(specs()["list_projects"].fn.__doc__.split())


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
async def test_list_projects_a_blank_cursor_is_refused_locally(server, mock_gorelo, blank):
    # a blank cursor is a caller mistake, not "the first page" (that is no cursor at all)
    text = await call_tool_error(server, "list_projects", {"cursor": blank})
    assert text == "cursor: must not be empty or whitespace only"
    assert mock_gorelo.requests == []


async def test_list_projects_maps_a_gorelo_validation_error_to_the_param(server, mock_gorelo):
    mock_gorelo.on(
        "GET", "/v1/projects",
        error_envelope(
            400,
            [("070101", "TargetDateAfter is not a valid date.", "TargetDateAfter"), ("070101", "Bad cursor.", "Cursor")],
        ),
    )
    text = await call_tool_error(server, "list_projects", {"target_date_after": "2026-11-01T00:00:00Z"})
    assert text == (
        "Gorelo rejected list_projects (HTTP 400, code 070101): target_date_after: TargetDateAfter is not a valid date.; "
        f"cursor: Bad cursor. [trace {TEST_TRACE_ID}]"
    )


async def test_list_projects_refuses_a_response_that_is_not_a_list(server, mock_gorelo):
    mock_gorelo.on("GET", "/v1/projects", envelope(None))
    text = await call_tool_error(server, "list_projects", {})
    assert text.startswith("Gorelo returned an unexpected response for list_projects: GET /v1/projects: ")
    assert "expected Data to be a list but got null" in text


# --------------------------------------------------------------------------
# get_project
# --------------------------------------------------------------------------


async def test_get_project_returns_the_record_unchanged(server, mock_gorelo):
    record = project_detail()
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}", envelope(record))
    assert await call_tool(server, "get_project", {"project_id": PROJECT}) == record
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", f"/v1/projects/{PROJECT}", {}, None)


async def test_project_reads_return_the_target_date_unchanged(server, mock_gorelo):
    # 2026-10-02: the rows and the detail carry TargetDate (it was DueDate); the tools pass records through untouched.
    row = project_row(TargetDate="2026-12-31T00:00:00Z")
    detail = project_detail(TargetDate="2026-12-31T00:00:00Z")
    mock_gorelo.on("GET", "/v1/projects", paged_envelope([row]))
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}", envelope(detail))
    listed = await call_tool(server, "list_projects", {})
    assert listed["items"] == [row] and listed["items"][0]["TargetDate"] == "2026-12-31T00:00:00Z"
    assert "DueDate" not in listed["items"][0]
    assert await call_tool(server, "get_project", {"project_id": PROJECT}) == detail


async def test_get_project_unknown_project_is_reported_with_gorelos_message(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}", error_envelope(404, [("070401", "Project was not found.")]))
    text = await call_tool_error(server, "get_project", {"project_id": PROJECT})
    assert text == f"Gorelo rejected get_project (HTTP 404, code 070401): Project was not found. [trace {TEST_TRACE_ID}]"


@pytest.mark.parametrize(
    "data, kind",
    [
        ([{"Id": PROJECT, "Title": "secret-title"}], "a list of 1 item"), (True, "a boolean"),
        ("secret-title", "a string"), (5, "a number"), ({}, "an empty object"),
    ],
)
async def test_the_single_record_reads_refuse_an_answer_that_is_not_an_object_without_quoting_it(
    server, mock_gorelo, data, kind
):
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}", envelope(data))
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}/comments/{COMMENT}", envelope(data))
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}/tasks/{TASK}/comments/{COMMENT}", envelope(data))
    calls = (
        ("get_project", {"project_id": PROJECT}, GET_PROJECT),
        ("get_project_comment", {"project_id": PROJECT, "comment_id": COMMENT}, f"GET {PC}/{{commentId}}"),
        ("get_project_comment", {"project_id": PROJECT, "task_id": TASK, "comment_id": COMMENT}, f"GET {TC}/{{commentId}}"),
    )
    for tool, arguments, op in calls:
        text = await call_tool_error(server, tool, arguments)
        assert text == (
            f"Gorelo returned an unexpected response for {tool}: {op}: expected Data to be a non-empty object but got "
            f"{kind}; refusing to guess"
        )
        assert "secret-title" not in text


async def test_get_project_a_timeout_is_safe_to_retry(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}", httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "get_project", {"project_id": PROJECT})
    assert text == "Gorelo did not answer get_project (the request timed out). This was a read, so retrying is safe."


# --------------------------------------------------------------------------
# create_project
# --------------------------------------------------------------------------


# The five fields CreateProjectCommand requires since 2026-10-02, as the tool takes them and as they are sent.
REQUIRED_CREATE = {"title": "Office move", "client_id": 9101, "location_id": 9001, "type_id": TYPE_ID, "group_id": 7201}
REQUIRED_BODY = {"Title": "Office move", "ClientId": 9101, "LocationId": 9001, "TypeId": TYPE_ID, "GroupId": 7201}


def route_create_project(mock, record=None):
    mock.on("POST", "/v1/projects", envelope({"Id": PROJECT}))
    mock.on("GET", f"/v1/projects/{PROJECT}", envelope(record if record is not None else project_detail()))


async def test_create_project_with_only_the_required_fields_sends_exactly_those_and_returns_the_reread_record(
    server, mock_gorelo
):
    record = project_detail(Title="Office move")
    route_create_project(mock_gorelo, record)
    assert await call_tool(server, "create_project", REQUIRED_CREATE) == record
    assert sent(mock_gorelo) == [("POST", "/v1/projects"), ("GET", f"/v1/projects/{PROJECT}")]
    assert mock_gorelo.requests[0].json == REQUIRED_BODY
    assert mock_gorelo.requests[0].query == {} and mock_gorelo.requests[1].query == {}


async def test_create_project_sends_every_field_in_pascal_case(server, mock_gorelo):
    route_create_project(mock_gorelo)
    await call_tool(
        server,
        "create_project",
        {
            "title": "Office move", "description": "Move the Springfield office", "client_id": 9101, "location_id": 9001,
            "lead_assignee_id": 9201, "type_id": TYPE_ID.upper(), "tag_ids": [TAG_ID, TAG_ID_2],
            "group_id": 7201, "watcher_ids": [9202], "shared_with_contact_ids": [9103, 9104],
            "target_date": "2026-11-30T12:00:00-05:00", "created_by_name": "Import job",
            "created_on": "2026-01-02T08:00:00+02:00", "updated_on": "2026-01-03T00:00:00Z",
        },
    )
    assert mock_gorelo.requests[0].json == {
        "Title": "Office move", "Description": "Move the Springfield office", "ClientId": 9101, "LocationId": 9001,
        "LeadAssigneeId": 9201, "TypeId": TYPE_ID, "TagIds": [TAG_ID, TAG_ID_2], "GroupId": 7201,
        "WatcherIds": [9202], "SharedWithContactIds": [9103, 9104], "TargetDate": "2026-11-30T17:00:00Z",
        "CreatedByName": "Import job", "CreatedOn": "2026-01-02T06:00:00Z", "UpdatedOn": "2026-01-03T00:00:00Z",
    }


async def test_create_project_sends_every_field_the_spec_has_and_no_other(server, mock_gorelo, spec_index):
    route_create_project(mock_gorelo)
    await call_tool(
        server,
        "create_project",
        {
            **REQUIRED_CREATE, "description": "D", "lead_assignee_id": 9201, "tag_ids": [TAG_ID],
            "watcher_ids": [9202], "shared_with_contact_ids": [9103], "target_date": "2026-11-30T17:00:00Z",
            "created_by_name": "Import job", "created_on": "2026-01-02T06:00:00Z", "updated_on": "2026-01-03T00:00:00Z",
        },
    )
    assert set(mock_gorelo.requests[0].json) == set(spec_index.ops[CREATE_PROJECT].body["fields"])


@pytest.mark.parametrize(
    "arguments, extra",
    [
        ({"description": "Move the office"}, {"Description": "Move the office"}),
        ({"lead_assignee_id": 9201}, {"LeadAssigneeId": 9201}),
        ({"tag_ids": [TAG_ID.upper()]}, {"TagIds": [TAG_ID]}),
        ({"watcher_ids": [9202]}, {"WatcherIds": [9202]}),
        ({"shared_with_contact_ids": [9103]}, {"SharedWithContactIds": [9103]}),
        ({"target_date": "2026-11-30T12:00:00-05:00"}, {"TargetDate": "2026-11-30T17:00:00Z"}),
        ({"created_by_name": "Import job"}, {"CreatedByName": "Import job"}),
        ({"created_on": "2026-01-02T08:00:00+02:00"}, {"CreatedOn": "2026-01-02T06:00:00Z"}),
        ({"updated_on": "2026-01-03T00:00:00Z"}, {"UpdatedOn": "2026-01-03T00:00:00Z"}),
    ],
)
async def test_create_project_sends_an_optional_field_only_when_the_caller_gave_it(
    server, mock_gorelo, arguments, extra
):
    route_create_project(mock_gorelo)
    await call_tool(server, "create_project", {**REQUIRED_CREATE, **arguments})
    assert mock_gorelo.requests[0].json == {**REQUIRED_BODY, **extra}


@pytest.mark.parametrize("missing", ["title", "client_id", "location_id", "type_id", "group_id"])
async def test_create_project_refuses_a_call_without_a_required_field_and_names_it(server, mock_gorelo, missing):
    # 2026-10-02: Gorelo requires all five, and there is no invented default for any of them.
    call = {k: v for k, v in REQUIRED_CREATE.items() if k != missing}
    text = await call_tool_error(server, "create_project", call)
    assert missing in text and "Missing required argument" in text
    assert mock_gorelo.requests == []


async def test_create_project_with_nothing_names_every_missing_field(server, mock_gorelo):
    text = await call_tool_error(server, "create_project", {})
    for name in REQUIRED_CREATE:
        assert name in text, name
    assert mock_gorelo.requests == []


def test_the_group_of_a_new_project_is_the_one_group_id(spec_index):
    # 2026-10-02: CreateProjectCommand lost GroupIds and requires GroupId, so there is one group, never a list.
    assert "GroupIds" not in spec_index.ops[CREATE_PROJECT].body["fields"]
    assert "GroupId" in spec_index.ops[CREATE_PROJECT].body["required"]
    assert "group_ids" not in accepted_params("create_project") and "group_id" in accepted_params("create_project")
    assert "GroupIds" not in specs()["create_project"].field_map.values()
    assert "GroupIds" in spec_index.ops[UPDATE_PROJECT].body["fields"]  # an update still replaces the groups


@pytest.mark.parametrize("old", [{"group_ids": [7201]}, {"due_date": "2026-11-30T17:00:00Z"}])
async def test_create_project_refuses_the_parameters_the_contract_removed(server, mock_gorelo, old):
    text = await call_tool_error(server, "create_project", {**REQUIRED_CREATE, **old})
    assert next(iter(old)) in text and "Unexpected keyword argument" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad", [9201, True, None, ["x"]], ids=["number", "boolean", "null", "list"])
async def test_create_project_refuses_a_type_id_that_is_not_text(server, mock_gorelo, bad):
    text = await call_tool_error(server, "create_project", {**REQUIRED_CREATE, "type_id": bad})
    assert "type_id" in text and "Input should be a valid string" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"title": ""}, "title: must not be empty or whitespace only"),
        ({"title": "   "}, "title: must not be empty or whitespace only"),
        ({"group_id": 0}, "group_id: expected a positive whole number"),
        ({"group_id": -7201}, "group_id: expected a positive whole number"),
        ({"client_id": 0}, "client_id: expected a positive whole number"),
        ({"location_id": -4}, "location_id: expected a positive whole number"),
        ({"lead_assignee_id": 0}, "lead_assignee_id: expected a positive whole number such as 123, got zero or a negative number"),
        ({"lead_assignee_id": -1}, "lead_assignee_id: expected a positive whole number"),
        ({"type_id": "migration"}, "type_id: expected a GUID such as"),
        ({"type_id": ""}, "type_id: expected a GUID such as"),
        ({"tag_ids": ["x"]}, "tag_ids[0]: expected a GUID such as"),
        ({"tag_ids": []}, "tag_ids: expected at least one GUID, got an empty list"),
        ({"watcher_ids": []}, "watcher_ids: expected at least one id, got an empty list"),
        ({"watcher_ids": [0]}, "watcher_ids[0]: expected a positive whole number"),
        ({"shared_with_contact_ids": []}, "shared_with_contact_ids: expected at least one id, got an empty list"),
        ({"description": ""}, "description: must not be empty or whitespace only"),
        ({"created_by_name": "  "}, "created_by_name: must not be empty or whitespace only"),
        ({"target_date": "2026-11-30T17:00:00"}, "target_date: '2026-11-30T17:00:00' has no UTC offset"),
        ({"target_date": "soon"}, "target_date: 'soon' is not an ISO 8601 datetime"),
        ({"created_on": "2026-01-02T08:00:00"}, "created_on: '2026-01-02T08:00:00' has no UTC offset"),
        ({"updated_on": "last week"}, "updated_on: 'last week' is not an ISO 8601 datetime"),
    ],
)
async def test_create_project_local_validation_errors_name_the_param_and_send_nothing(
    server, mock_gorelo, arguments, fragment
):
    text = await call_tool_error(server, "create_project", {**REQUIRED_CREATE, **arguments})
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_create_project_refuses_a_lead_who_is_also_a_watcher(server, mock_gorelo):
    # CreateProjectCommand.WatcherIds: "The lead is never also a watcher." update_project already refused it.
    call = {**REQUIRED_CREATE, "lead_assignee_id": 9201, "watcher_ids": [9202, 9201]}
    text = await call_tool_error(server, "create_project", call)
    assert text == (
        "watcher_ids: technician 9201 is also the lead_assignee_id, and a technician cannot be both the lead and a "
        "watcher; drop it from watcher_ids or choose another lead"
    )
    assert mock_gorelo.requests == []


async def test_create_project_accepts_a_lead_who_is_not_a_watcher_and_watchers_without_a_lead(server, mock_gorelo):
    route_create_project(mock_gorelo)
    await call_tool(server, "create_project", {**REQUIRED_CREATE, "lead_assignee_id": 9201, "watcher_ids": [9202, 1600]})
    assert mock_gorelo.requests[0].json == {**REQUIRED_BODY, "LeadAssigneeId": 9201, "WatcherIds": [9202, 1600]}
    mock_gorelo.reset()
    await call_tool(server, "create_project", {**REQUIRED_CREATE, "watcher_ids": [9201]})
    assert mock_gorelo.requests[0].json == {**REQUIRED_BODY, "WatcherIds": [9201]}


class NoClock(datetime):
    """A datetime that refuses to say what time it is: swapped in for the module's datetime, it proves the clock is
    never read (the instants of a call are only compared with each other)."""

    @classmethod
    def now(cls, tz=None):
        raise AssertionError("the local clock was read")

    @classmethod
    def utcnow(cls):
        raise AssertionError("the local clock was read")

    @classmethod
    def today(cls):
        raise AssertionError("the local clock was read")


@pytest.fixture
def no_clock(monkeypatch):
    monkeypatch.setattr(module, "datetime", NoClock)


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        # UpdatedOn "Must not be ... earlier than CreatedOn": compared as instants, and only when both are given
        (
            {"created_on": "2026-01-03T00:00:00Z", "updated_on": "2026-01-02T23:59:59Z"},
            "updated_on: 2026-01-02T23:59:59Z is earlier than created_on (2026-01-03T00:00:00Z); it must not be",
        ),
        # the offsets decide, not the clock readings: created 22:00Z, updated 21:00Z
        (
            {"created_on": "2026-01-03T00:00:00+02:00", "updated_on": "2026-01-02T19:00:00-02:00"},
            "updated_on: 2026-01-02T21:00:00Z is earlier than created_on (2026-01-02T22:00:00Z)",
        ),
        # a later wall-clock reading is not a later instant: 01:00+05:00 on the 3rd is 20:00Z on the 2nd
        (
            {"created_on": "2026-01-02T23:00:00Z", "updated_on": "2026-01-03T01:00:00+05:00"},
            "updated_on: 2026-01-02T20:00:00Z is earlier than created_on (2026-01-02T23:00:00Z)",
        ),
    ],
)
async def test_create_project_updated_on_is_not_earlier_than_created_on(server, mock_gorelo, no_clock, arguments, fragment):
    text = await call_tool_error(server, "create_project", {**REQUIRED_CREATE, **arguments})
    assert text.startswith(fragment)
    assert mock_gorelo.requests == []


async def test_create_project_a_future_backdate_is_left_to_gorelo_and_no_clock_is_read(server, mock_gorelo, no_clock):
    # CreatedOn "Must not be in the future": Gorelo enforces it, the tool never compares a time with the local clock
    # (a clock that is a few seconds off must not refuse a call Gorelo would accept), and the offset is still honoured.
    route_create_project(mock_gorelo)
    await call_tool(
        server, "create_project",
        {**REQUIRED_CREATE, "created_on": "2999-01-01T00:00:00-05:00", "updated_on": "2999-06-01T00:00:00Z"},
    )
    assert mock_gorelo.requests[0].json == {
        **REQUIRED_BODY, "CreatedOn": "2999-01-01T05:00:00Z", "UpdatedOn": "2999-06-01T00:00:00Z",
    }
    mock_gorelo.reset()
    route_create_project(mock_gorelo)
    await call_tool(server, "create_project", {**REQUIRED_CREATE, "updated_on": "2999-06-01T00:00:00Z"})
    assert mock_gorelo.requests[0].json == {**REQUIRED_BODY, "UpdatedOn": "2999-06-01T00:00:00Z"}


async def test_create_project_a_future_backdate_refused_by_gorelo_names_the_param(server, mock_gorelo):
    mock_gorelo.on(
        "POST", "/v1/projects",
        error_envelope(
            400,
            [
                ("070101", "CreatedOn must not be in the future.", "CreatedOn"),
                ("070101", "UpdatedOn must not be in the future or earlier than CreatedOn.", "UpdatedOn"),
            ],
        ),
    )
    text = await call_tool_error(
        server, "create_project", {**REQUIRED_CREATE, "created_on": "2999-01-01T00:00:00Z", "updated_on": "2999-01-02T00:00:00Z"}
    )
    assert text == (
        "Gorelo rejected create_project (HTTP 400, code 070101): created_on: CreatedOn must not be in the future.; "
        f"updated_on: UpdatedOn must not be in the future or earlier than CreatedOn. [trace {TEST_TRACE_ID}]"
    )
    assert sent(mock_gorelo) == [("POST", "/v1/projects")]


@pytest.mark.parametrize(
    "created, updated, sent_created, sent_updated",
    [
        ("2026-01-02T08:00:00+02:00", "2026-01-03T00:00:00Z", "2026-01-02T06:00:00Z", "2026-01-03T00:00:00Z"),
        ("2026-01-02T08:00:00+02:00", "2026-01-02T06:00:00Z", "2026-01-02T06:00:00Z", "2026-01-02T06:00:00Z"),  # equal
        ("2026-01-03T00:00:00+02:00", "2026-01-02T19:00:00-03:00", "2026-01-02T22:00:00Z", "2026-01-02T22:00:00Z"),  # equal
        (None, "2026-01-03T00:00:00Z", None, "2026-01-03T00:00:00Z"),  # only compared when both are given
        ("2026-01-02T08:00:00Z", None, "2026-01-02T08:00:00Z", None),
    ],
)
async def test_create_project_accepts_backdated_times_that_are_in_order_and_sends_them_as_utc(
    server, mock_gorelo, created, updated, sent_created, sent_updated
):
    route_create_project(mock_gorelo)
    arguments = {**REQUIRED_CREATE}
    expected = {**REQUIRED_BODY}
    for param, wire, value, sent_value in (
        ("created_on", "CreatedOn", created, sent_created), ("updated_on", "UpdatedOn", updated, sent_updated)
    ):
        if value is not None:
            arguments[param] = value
            expected[wire] = sent_value
    await call_tool(server, "create_project", arguments)
    assert mock_gorelo.requests[0].json == expected


async def test_create_project_maps_gorelo_errors_to_the_param(server, mock_gorelo):
    mock_gorelo.on(
        "POST", "/v1/projects",
        error_envelope(
            400,
            [
                ("070101", "Title must not be empty.", "Title"),
                ("070101", "The group was not found.", "GroupId"),
                ("070101", "Location does not belong to the client.", "LocationId"),
                ("070101", "The type was not found.", "TypeId"),
                ("070101", "Contacts must belong to the client.", "SharedWithContactIds"),
                ("070101", "Target date is not valid.", "TargetDate"),
            ],
        ),
    )
    text = await call_tool_error(server, "create_project", REQUIRED_CREATE)
    assert text == (
        "Gorelo rejected create_project (HTTP 400, code 070101): title: Title must not be empty.; "
        "group_id: The group was not found.; location_id: Location does not belong to the client.; "
        "type_id: The type was not found.; shared_with_contact_ids: Contacts must belong to the client.; "
        f"target_date: Target date is not valid. [trace {TEST_TRACE_ID}]"
    )
    assert sent(mock_gorelo) == [("POST", "/v1/projects")]


async def test_create_project_a_failed_reread_returns_the_warning_and_never_repeats_the_write(server, mock_gorelo):
    mock_gorelo.on("POST", "/v1/projects", envelope({"Id": PROJECT}))
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}", error_envelope(500, [("070001", "Internal error.")]))
    result = await call_tool(server, "create_project", REQUIRED_CREATE)
    assert set(result) == {"Id", "warning"} and result["Id"] == PROJECT
    assert result["warning"].startswith("the write succeeded; re-reading it failed: GET /v1/projects/{projectId} answered HTTP 500")
    assert result["warning"].endswith("Do not repeat the write; read it again later.")
    assert sent(mock_gorelo) == [("POST", "/v1/projects"), ("GET", f"/v1/projects/{PROJECT}")]


async def test_create_project_a_post_that_answers_without_an_id_is_an_unconfirmed_write(server, mock_gorelo):
    mock_gorelo.on("POST", "/v1/projects", envelope(None))
    text = await call_tool_error(server, "create_project", REQUIRED_CREATE)
    assert text.startswith("Gorelo returned an unexpected response for create_project: POST /v1/projects: ")
    assert UNUSABLE_ID in text and "Data is null, not an object with an Id" in text
    assert VERIFY in text
    assert sent(mock_gorelo) == [("POST", "/v1/projects")]


@pytest.mark.parametrize(
    "data", [{}, {"Id": None}, {"Id": ""}, {"Id": 0}, {"Id": True}, {"Id": False}, {"Other": 1}, [{"Id": PROJECT}], "text", False]
)
async def test_create_project_any_answer_without_a_usable_id_is_refused(server, mock_gorelo, data):
    mock_gorelo.on("POST", "/v1/projects", envelope(data))
    text = await call_tool_error(server, "create_project", REQUIRED_CREATE)
    assert UNUSABLE_ID in text and VERIFY in text
    assert len(mock_gorelo.requests) == 1  # no re-read, no second write


async def test_create_project_a_timeout_says_the_write_is_unconfirmed_and_is_not_retried(server, mock_gorelo):
    mock_gorelo.on("POST", "/v1/projects", httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "create_project", REQUIRED_CREATE)
    assert text == (
        "Gorelo did not confirm create_project (the request timed out). The change may or may not have been "
        "applied. Verify with a read before retrying."
    )
    assert sent(mock_gorelo) == [("POST", "/v1/projects")]


async def test_create_project_a_server_error_says_gorelo_may_have_applied_the_change(server, mock_gorelo):
    mock_gorelo.on("POST", "/v1/projects", error_envelope(500, [("070001", "Internal error.")]))
    text = await call_tool_error(server, "create_project", REQUIRED_CREATE)
    assert "Gorelo rejected create_project (HTTP 500, code 070001): Internal error." in text
    assert "Gorelo may have applied the change before failing. Verify with a read before retrying." in text
    assert len(mock_gorelo.requests) == 1


def test_create_project_docstring_states_the_documented_side_effects():
    doc = " ".join(specs()["create_project"].fn.__doc__.split())
    for fragment in (
        "NotStarted", "the app's numbering", "project-created notification the app sends",
        "do not create it again",
        # the sweep: a write that can notify says who (Gorelo names nobody), and that no tool deletes a project
        "project-created notification the app sends (Gorelo does not say who receives it)",
        "Projects cannot be deleted through this server (a delete removes every task in it).",
    ):
        assert fragment in doc, fragment


def test_create_project_docstring_lists_the_five_required_fields_and_says_the_rest_is_optional():
    # the text must say exactly which fields are required, and no group list exists.
    doc = " ".join(specs()["create_project"].fn.__doc__.split())
    assert "Required: title, client_id, location_id, type_id and group_id" in doc
    assert "everything else is optional" in doc
    assert "Only title is required" not in doc and "group_ids" not in doc and "due_date" not in doc
    for param, resolver in (
        ("client_id", "list_clients"), ("location_id", "list_client_locations"), ("type_id", "list_project_types"),
        ("group_id", "list_org_groups"),
    ):
        assert resolver in param_description("create_project", param), param


def test_create_project_param_texts_follow_the_documented_rules():
    assert "Omit for a project with no lead" in param_description("create_project", "lead_assignee_id")
    assert "The lead is never also a watcher" in param_description("create_project", "watcher_ids")
    assert "must belong to client_id" in param_description("create_project", "shared_with_contact_ids")
    assert "targeted to finish" in param_description("create_project", "target_date")
    # 2026-10-02: the author and the backdating texts follow CreatedByName, CreatedOn and UpdatedOn
    assert "Gorelo records API" in param_description("create_project", "created_by_name")
    created = param_description("create_project", "created_on")
    assert "never in the future (Gorelo refuses it)" in created and "Omit for now" in created
    updated = param_description("create_project", "updated_on")
    assert "never in the future (Gorelo refuses it)" in updated and "not earlier than created_on" in updated
    assert "equals the creation time" in updated


# --------------------------------------------------------------------------
# update_project
# --------------------------------------------------------------------------


def route_update_project(mock, record=None):
    mock.on("PATCH", f"/v1/projects/{PROJECT}", envelope({"Id": PROJECT}))
    mock.on("GET", f"/v1/projects/{PROJECT}", envelope(record if record is not None else project_detail()))


@pytest.mark.parametrize(
    "arguments, body",
    [
        ({"title": "New title"}, {"Title": "New title"}),
        ({"description": "New text"}, {"Description": "New text"}),
        ({"client_id": 9102, "location_id": 77}, {"ClientId": 9102, "LocationId": 77}),
        ({"location_id": 77}, {"LocationId": 77}),
        ({"shared_with_contact_ids": [9103]}, {"SharedWithContactIds": [9103]}),
        ({"type_id": TYPE_ID.upper()}, {"TypeId": TYPE_ID}),
        ({"tag_ids": [TAG_ID, TAG_ID_2]}, {"TagIds": [TAG_ID, TAG_ID_2]}),
        ({"lead_assignee_id": 9202}, {"LeadAssigneeId": 9202}),
        ({"watcher_ids": [9201, 9202]}, {"WatcherIds": [9201, 9202]}),
        ({"group_ids": [7201]}, {"GroupIds": [7201]}),
        ({"target_date": "2026-11-30T12:00:00-05:00"}, {"TargetDate": "2026-11-30T17:00:00Z"}),
        ({"clear_target_date": True}, {"ClearTargetDate": True}),
        ({"status_id": 4}, {"StatusId": 4}),
        ({"status_id": 3, "status_reason": "Waiting for the landlord"}, {"StatusId": 3, "StatusReason": "Waiting for the landlord"}),
        ({"status_id": 5, "closed_on": "2026-09-30T18:00:00+02:00"}, {"StatusId": 5, "ClosedOn": "2026-09-30T16:00:00Z"}),
        ({"closed_on": "2026-09-30T18:00:00+02:00"}, {"ClosedOn": "2026-09-30T16:00:00Z"}),
        ({"title": "T", "updated_by_name": "Import job"}, {"Title": "T", "UpdatedByName": "Import job"}),
        ({"clear_fields": ["tag_ids"]}, {"TagIds": []}),
        ({"clear_fields": ["watcher_ids"]}, {"WatcherIds": []}),
        ({"clear_fields": ["shared_with_contact_ids"]}, {"SharedWithContactIds": []}),
        # 2026-10-02: the description is removed with "" and the lead with 0, both only through clear_fields
        ({"clear_fields": ["description"]}, {"Description": ""}),
        ({"clear_fields": ["lead_assignee_id"]}, {"LeadAssigneeId": 0}),
        (
            {"clear_fields": ["tag_ids", "watcher_ids", "tag_ids"], "title": "T"},
            {"Title": "T", "TagIds": [], "WatcherIds": []},
        ),
        (
            {"clear_fields": ["lead_assignee_id", "description", "watcher_ids"], "title": "T"},
            {"Title": "T", "LeadAssigneeId": 0, "Description": "", "WatcherIds": []},
        ),
        # a new lead and the removal of the watchers are different fields, so they may share one call
        ({"lead_assignee_id": 9201, "clear_fields": ["watcher_ids"]}, {"LeadAssigneeId": 9201, "WatcherIds": []}),
        # the move to another client names its location, and clearing the shared contacts in the same call is allowed
        (
            {"client_id": 9102, "location_id": 77, "clear_fields": ["shared_with_contact_ids"]},
            {"ClientId": 9102, "LocationId": 77, "SharedWithContactIds": []},
        ),
    ],
)
async def test_update_project_sends_only_what_the_caller_gave(server, mock_gorelo, arguments, body):
    record = project_detail(Title="After")
    route_update_project(mock_gorelo, record)
    result = await call_tool(server, "update_project", {"project_id": PROJECT, **arguments})
    assert result == record
    assert sent(mock_gorelo) == [("PATCH", f"/v1/projects/{PROJECT}"), ("GET", f"/v1/projects/{PROJECT}")]
    assert mock_gorelo.requests[0].json == body
    assert mock_gorelo.requests[0].query == {}


async def test_update_project_sends_every_field_in_one_call(server, mock_gorelo):
    route_update_project(mock_gorelo)
    await call_tool(
        server,
        "update_project",
        {
            "project_id": PROJECT, "title": "T", "description": "D", "client_id": 9102, "location_id": 77,
            "shared_with_contact_ids": [1], "type_id": TYPE_ID, "tag_ids": [TAG_ID], "lead_assignee_id": 9201,
            "watcher_ids": [9202], "group_ids": [7201], "target_date": "2026-11-30T17:00:00Z", "status_id": 5,
            "status_reason": "Signed off", "closed_on": "2026-09-30T17:00:00Z", "updated_by_name": "Import job",
        },
    )
    assert mock_gorelo.requests[0].json == {
        "Title": "T", "Description": "D", "ClientId": 9102, "LocationId": 77, "SharedWithContactIds": [1],
        "TypeId": TYPE_ID, "TagIds": [TAG_ID], "LeadAssigneeId": 9201, "WatcherIds": [9202], "GroupIds": [7201],
        "TargetDate": "2026-11-30T17:00:00Z", "StatusId": 5, "StatusReason": "Signed off",
        "ClosedOn": "2026-09-30T17:00:00Z", "UpdatedByName": "Import job",
    }


async def test_update_project_false_for_clear_target_date_is_the_same_as_leaving_it_out(server, mock_gorelo):
    route_update_project(mock_gorelo)
    await call_tool(server, "update_project", {"project_id": PROJECT, "title": "T", "clear_target_date": False})
    assert mock_gorelo.requests[0].json == {"Title": "T"}


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({}, "nothing to change: give at least one of title, description, client_id"),
        ({"updated_by_name": "Import job"}, "updated_by_name alone changes nothing"),
        ({"clear_target_date": False}, "nothing to change"),
        (
            {"clear_target_date": True, "target_date": "2026-11-30T17:00:00Z"},
            "target_date: cannot be set while clear_target_date is true",
        ),
        # 2026-10-02: a bare 0 is never sent: the lead is removed through clear_fields (Gorelo: "Send 0 to remove the lead")
        ({"lead_assignee_id": 0}, 'lead_assignee_id: 0 is not a technician id; to remove the lead pass clear_fields=["lead_assignee_id"]'),
        ({"lead_assignee_id": -3}, "lead_assignee_id: expected a positive whole number"),
        (
            {"lead_assignee_id": 9201, "clear_fields": ["lead_assignee_id"]},
            "lead_assignee_id: cannot be given a value and cleared in the same call",
        ),
        (
            {"lead_assignee_id": 9201, "watcher_ids": [9202, 9201]},
            "watcher_ids: technician 9201 is also the lead_assignee_id, and a technician cannot be both the lead and a watcher",
        ),
        ({"tag_ids": []}, 'tag_ids: an empty list is not accepted because lists replace the stored one; to remove every entry pass clear_fields=["tag_ids"]'),
        ({"watcher_ids": []}, 'clear_fields=["watcher_ids"]'),
        # 2026-10-02: the groups cannot be emptied (no "Empty array clears them" sentence, and create requires a group)
        (
            {"group_ids": []},
            "group_ids: an empty list is not accepted, because the groups cannot be emptied (a new project always "
            "gets one); send the complete new list (at least one group id) or omit group_ids",
        ),
        (
            {"group_ids": [], "clear_fields": ["tag_ids"]},
            "group_ids: an empty list is not accepted, because the groups cannot be emptied",
        ),
        ({"group_ids": [0]}, "group_ids[0]: expected a positive whole number"),
        ({"shared_with_contact_ids": []}, 'clear_fields=["shared_with_contact_ids"]'),
        ({"tag_ids": [], "clear_fields": ["tag_ids"]}, "tag_ids: an empty list is not accepted because lists replace the stored one"),
        ({"tag_ids": [TAG_ID], "clear_fields": ["tag_ids"]}, "tag_ids: cannot be given a value and cleared in the same call"),
        ({"clear_fields": []}, "clear_fields: must not be an empty list"),
        ({"title": ""}, "title: must not be empty or whitespace only"),
        # 2026-10-02: "Send an empty string to remove it" is offered only through clear_fields
        ({"description": "  "}, 'description: a blank value is not accepted; to remove it pass clear_fields=["description"]'),
        ({"description": ""}, 'description: a blank value is not accepted; to remove it pass clear_fields=["description"]'),
        ({"description": "New", "clear_fields": ["description"]}, "description: cannot be given a value and cleared in the same call"),
        ({"status_reason": ""}, "status_reason: must not be empty or whitespace only"),
        # 2026-10-02: StatusReason is "Only read when StatusId is sent"
        ({"status_reason": "Waiting"}, "status_reason: only recorded together with status_id; pass the status as well"),
        (
            {"status_reason": "Waiting", "title": "T", "clear_fields": ["tag_ids"]},
            "status_reason: only recorded together with status_id; pass the status as well",
        ),
        # 2026-10-02: ClientId "Send LocationId with it: a location belongs to a client"
        ({"client_id": 9102}, "location_id: required together with client_id"),
        ({"client_id": 9102, "title": "T"}, "give both client_id and location_id"),
        # 2026-10-02: ClosedOn belongs to a Closed project (a future ClosedOn is Gorelo's to refuse, see below)
        (
            {"status_id": 4, "closed_on": "2026-09-30T17:00:00Z"},
            "closed_on: only accepted for a Closed project, but status_id is 4 and the project would not be Closed; give status_id 5 (Closed)",
        ),
        ({"status_id": 2, "closed_on": "2026-09-30T17:00:00Z"}, "closed_on: only accepted for a Closed project, but status_id is 2"),
        ({"updated_by_name": "", "title": "T"}, "updated_by_name: must not be empty or whitespace only"),
        ({"client_id": 0}, "client_id: expected a positive whole number"),
        ({"location_id": 0}, "location_id: expected a positive whole number"),
        ({"status_id": 0}, "status_id: expected a positive whole number"),
        ({"type_id": "migration"}, "type_id: expected a GUID such as"),
        ({"tag_ids": ["x"]}, "tag_ids[0]: expected a GUID such as"),
        ({"watcher_ids": [0]}, "watcher_ids[0]: expected a positive whole number"),
        ({"target_date": "2026-11-30"}, "target_date: '2026-11-30' has no UTC offset"),
        ({"target_date": "soon"}, "target_date: 'soon' is not an ISO 8601 datetime"),
        ({"closed_on": "2026-11-30T17:00:00"}, "closed_on: '2026-11-30T17:00:00' has no UTC offset"),
    ],
)
async def test_update_project_local_validation_errors_name_the_param_and_send_nothing(
    server, mock_gorelo, arguments, fragment
):
    text = await call_tool_error(server, "update_project", {"project_id": PROJECT, **arguments})
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_update_project_refuses_a_name_clear_fields_does_not_allow(server, mock_gorelo):
    # group_ids is on this list since 2026-10-02: its field text has no "Empty array clears them" sentence, so the
    # groups cannot be emptied
    for field in (
        "title", "target_date", "clear_target_date", "status_id", "status_reason", "type_id", "client_id",
        "location_id", "closed_on", "group_ids",
    ):
        text = await call_tool_error(server, "update_project", {"project_id": PROJECT, "clear_fields": [field]})
        assert "clear_fields.0" in text and "Input should be" in text and "shared_with_contact_ids" in text, field
        assert "group_ids" not in text.split("Input should be", 1)[1].split("[type=")[0], field
    assert mock_gorelo.requests == []


def test_update_project_offers_exactly_the_documented_removals_in_clear_fields():
    assert offered_values("update_project", "clear_fields") == {
        "shared_with_contact_ids", "tag_ids", "watcher_ids", "description", "lead_assignee_id",
    }


async def test_update_project_removes_the_lead_only_through_clear_fields_never_with_a_bare_zero(server, mock_gorelo):
    route_update_project(mock_gorelo)
    await call_tool(server, "update_project", {"project_id": PROJECT, "clear_fields": ["lead_assignee_id"]})
    assert mock_gorelo.requests[0].json == {"LeadAssigneeId": 0}
    mock_gorelo.reset()
    # a bare 0 in lead_assignee_id is refused locally and points to the opt-in; so is any other non-id
    text = await call_tool_error(server, "update_project", {"project_id": PROJECT, "lead_assignee_id": 0})
    assert text == 'lead_assignee_id: 0 is not a technician id; to remove the lead pass clear_fields=["lead_assignee_id"]'
    for lead in (-1, -9201):
        text = await call_tool_error(server, "update_project", {"project_id": PROJECT, "lead_assignee_id": lead})
        assert text.startswith("lead_assignee_id: expected a positive whole number such as 123, got zero or a negative number")
    # a new lead and the removal of the lead cannot be asked for together
    text = await call_tool_error(
        server, "update_project",
        {"project_id": PROJECT, "lead_assignee_id": 9201, "clear_fields": ["lead_assignee_id"]},
    )
    assert text == "lead_assignee_id: cannot be given a value and cleared in the same call"
    assert mock_gorelo.requests == []


async def test_update_project_removes_the_description_only_through_clear_fields(server, mock_gorelo):
    route_update_project(mock_gorelo)
    await call_tool(server, "update_project", {"project_id": PROJECT, "clear_fields": ["description"]})
    assert mock_gorelo.requests[0].json == {"Description": ""}
    mock_gorelo.reset()
    for blank in ("", " ", "\n"):
        text = await call_tool_error(server, "update_project", {"project_id": PROJECT, "description": blank})
        assert text == 'description: a blank value is not accepted; to remove it pass clear_fields=["description"]'
    assert mock_gorelo.requests == []


async def test_update_project_never_sends_an_empty_group_list(server, mock_gorelo):
    # the other three lists can be emptied with clear_fields; GroupIds cannot (see the spec test above)
    for arguments in ({"group_ids": []}, {"group_ids": [], "title": "T"}, {"group_ids": [], "clear_fields": ["tag_ids"]}):
        text = await call_tool_error(server, "update_project", {"project_id": PROJECT, **arguments})
        assert text.startswith("group_ids: an empty list is not accepted, because the groups cannot be emptied")
        assert "clear_fields" not in text
    text = await call_tool_error(server, "update_project", {"project_id": PROJECT, "clear_fields": ["group_ids"]})
    assert "clear_fields.0" in text and "Input should be" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "name", ["group_ids", "title", "target_date", "clear_target_date", "client_id", "location_id", "status_id", "closed_on"]
)
async def test_calling_update_project_directly_cannot_clear_a_field_the_schema_does_not_offer(
    client_factory, mock_gorelo, name
):
    # The Literal of clear_fields normally pre-empts this. A direct call skips it, and every name here is a real field
    # of the body: GroupIds above all, because the tool never empties the groups, and a blank sent for any of the
    # others would be wrong.
    if name == "group_ids":
        expected = (
            "clear_fields: group_ids cannot be cleared, because the groups cannot be emptied (a new project always "
            "gets one); send the complete new list in group_ids instead"
        )
    else:
        expected = (
            f"clear_fields: '{name}' cannot be cleared; allowed: shared_with_contact_ids, tag_ids, watcher_ids, "
            "description, lead_assignee_id"
        )
    with pytest.raises(ToolError) as caught:
        await call_directly(client_factory, module.update_project, project_id=PROJECT, clear_fields=[name])
    assert str(caught.value) == expected
    # the same refusal when a valid name sits next to it
    with pytest.raises(ToolError) as caught:
        await call_directly(client_factory, module.update_project, project_id=PROJECT, clear_fields=["tag_ids", name])
    assert str(caught.value) == expected
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("bad, kind", [(5, "a number"), (None, "null"), (["tag_ids"], "a list of 1 item"), (True, "a boolean")])
async def test_calling_update_project_directly_refuses_a_clear_field_that_is_not_a_name(client_factory, mock_gorelo, bad, kind):
    with pytest.raises(ToolError) as caught:
        await call_directly(client_factory, module.update_project, project_id=PROJECT, clear_fields=["tag_ids", bad])
    assert str(caught.value) == f'clear_fields[1]: expected a field name such as "tag_ids", got {kind}'
    assert mock_gorelo.requests == []


async def test_calling_update_project_directly_still_clears_what_the_schema_offers(client_factory, mock_gorelo):
    # the direct path is not stricter than the schema path: every offered name goes through
    route_update_project(mock_gorelo)
    await call_directly(
        client_factory, module.update_project, project_id=PROJECT,
        clear_fields=["shared_with_contact_ids", "tag_ids", "watcher_ids", "description", "lead_assignee_id"],
    )
    assert mock_gorelo.requests[0].json == {
        "SharedWithContactIds": [], "TagIds": [], "WatcherIds": [], "Description": "", "LeadAssigneeId": 0,
    }


async def test_update_project_replaces_the_groups_with_the_complete_new_list(server, mock_gorelo):
    route_update_project(mock_gorelo)
    await call_tool(server, "update_project", {"project_id": PROJECT, "group_ids": [7201, 7202]})
    assert mock_gorelo.requests[0].json == {"GroupIds": [7201, 7202]}


async def test_update_project_status_reason_is_only_sent_with_the_status(server, mock_gorelo):
    route_update_project(mock_gorelo)
    await call_tool(server, "update_project", {"project_id": PROJECT, "status_id": 3, "status_reason": "Waiting"})
    assert mock_gorelo.requests[0].json == {"StatusId": 3, "StatusReason": "Waiting"}
    mock_gorelo.reset()
    text = await call_tool_error(server, "update_project", {"project_id": PROJECT, "status_reason": "Waiting"})
    assert text == "status_reason: only recorded together with status_id; pass the status as well"
    # the status alone is fine, and so is a status change with other fields
    await call_tool(server, "update_project", {"project_id": PROJECT, "status_id": 2, "title": "T"})
    assert mock_gorelo.requests[0].json == {"StatusId": 2, "Title": "T"}
    assert len([r for r in mock_gorelo.requests if r.method == "PATCH"]) == 1


async def test_update_project_moving_to_another_client_needs_its_location_and_names_both_params(server, mock_gorelo):
    text = await call_tool_error(server, "update_project", {"project_id": PROJECT, "client_id": 9102})
    assert text.startswith("location_id: required together with client_id")
    assert "client_id" in text and "location_id" in text and "list_client_locations" in text
    assert "clears the shared contacts" in text
    assert mock_gorelo.requests == []
    route_update_project(mock_gorelo)
    await call_tool(server, "update_project", {"project_id": PROJECT, "client_id": 9102, "location_id": 77})
    assert mock_gorelo.requests[0].json == {"ClientId": 9102, "LocationId": 77}
    mock_gorelo.reset()
    # a location alone (no move) is still allowed
    await call_tool(server, "update_project", {"project_id": PROJECT, "location_id": 77})
    assert mock_gorelo.requests[0].json == {"LocationId": 77}


async def test_update_project_closed_on_is_for_a_closed_project_and_the_local_clock_is_never_read(
    server, mock_gorelo, no_clock
):
    route_update_project(mock_gorelo)
    # already Closed (no status_id) or closed by this same request (status_id 5): accepted
    await call_tool(server, "update_project", {"project_id": PROJECT, "closed_on": "2026-09-30T17:00:00Z"})
    assert mock_gorelo.requests[0].json == {"ClosedOn": "2026-09-30T17:00:00Z"}
    mock_gorelo.reset()
    await call_tool(server, "update_project", {"project_id": PROJECT, "status_id": 5, "closed_on": "2026-09-30T19:00:00+02:00"})
    assert mock_gorelo.requests[0].json == {"StatusId": 5, "ClosedOn": "2026-09-30T17:00:00Z"}
    mock_gorelo.reset()
    # any other status in the same request means the project is not Closed afterwards
    for status in (1, 2, 3, 4):
        text = await call_tool_error(
            server, "update_project", {"project_id": PROJECT, "status_id": status, "closed_on": "2026-09-30T17:00:00Z"}
        )
        assert text.startswith(f"closed_on: only accepted for a Closed project, but status_id is {status}")
        assert "status_id 5 (Closed)" in text
    assert mock_gorelo.requests == []
    # ClosedOn "must not be in the future or earlier than the project's CreatedOn": the first needs the local clock and
    # the second a read, so both are Gorelo's to refuse. The instant goes out in UTC, the offset still decides.
    route_update_project(mock_gorelo)
    await call_tool(
        server, "update_project", {"project_id": PROJECT, "status_id": 5, "closed_on": "2999-01-01T00:00:00+02:00"}
    )
    assert mock_gorelo.requests[0].json == {"StatusId": 5, "ClosedOn": "2998-12-31T22:00:00Z"}


async def test_update_project_a_closed_on_gorelo_refuses_names_the_param(server, mock_gorelo):
    mock_gorelo.on(
        "PATCH", f"/v1/projects/{PROJECT}",
        error_envelope(400, [("070101", "ClosedOn must not be in the future or earlier than CreatedOn.", "ClosedOn")]),
    )
    text = await call_tool_error(
        server, "update_project", {"project_id": PROJECT, "status_id": 5, "closed_on": "2999-01-01T00:00:00Z"}
    )
    assert text == (
        "Gorelo rejected update_project (HTTP 400, code 070101): closed_on: ClosedOn must not be in the future or "
        f"earlier than CreatedOn. [trace {TEST_TRACE_ID}]"
    )
    assert sent(mock_gorelo) == [("PATCH", f"/v1/projects/{PROJECT}")]


async def test_create_project_refuses_lead_zero(server, mock_gorelo):
    text = await call_tool_error(server, "create_project", {**REQUIRED_CREATE, "lead_assignee_id": 0})
    assert text.startswith("lead_assignee_id: expected a positive whole number such as 123, got zero or a negative number")
    assert mock_gorelo.requests == []


async def test_update_project_maps_gorelo_errors_to_the_param(server, mock_gorelo):
    mock_gorelo.on(
        "PATCH", f"/v1/projects/{PROJECT}",
        error_envelope(
            400,
            [
                ("070101", "The location does not belong to the new client.", "LocationId"),
                ("070101", "ClosedOn needs a Closed project.", "ClosedOn"),
                ("070101", "A technician cannot be both lead and watcher.", "WatcherIds"),
                ("070101", "StatusReason is only read with a StatusId.", "StatusReason"),
                ("070101", "A project needs a technician group.", "GroupIds"),
            ],
        ),
    )
    text = await call_tool_error(server, "update_project", {"project_id": PROJECT, "client_id": 9102, "location_id": 77})
    assert text == (
        "Gorelo rejected update_project (HTTP 400, code 070101): location_id: The location does not belong to the "
        "new client.; closed_on: ClosedOn needs a Closed project.; watcher_ids: A technician cannot be both lead "
        "and watcher.; status_reason: StatusReason is only read with a StatusId.; group_ids: A project needs a "
        f"technician group. [trace {TEST_TRACE_ID}]"
    )
    assert sent(mock_gorelo) == [("PATCH", f"/v1/projects/{PROJECT}")]


@pytest.mark.parametrize("data", [None, {}, False, True, [PROJECT], "ok"])
async def test_update_project_a_patch_that_answers_with_anything_but_an_object_is_an_unconfirmed_write(server, mock_gorelo, data):
    mock_gorelo.on("PATCH", f"/v1/projects/{PROJECT}", envelope(data))
    text = await call_tool_error(server, "update_project", {"project_id": PROJECT, "title": "T"})
    assert text.startswith(
        "Gorelo returned an unexpected response for update_project: PATCH /v1/projects/{projectId}: expected Data to be a non-empty object but got "
    )
    assert VERIFY in text
    assert sent(mock_gorelo) == [("PATCH", f"/v1/projects/{PROJECT}")]  # no re-read of a write that cannot be confirmed


async def test_update_project_unknown_project_is_a_404_naming_nothing_else(server, mock_gorelo):
    mock_gorelo.on("PATCH", f"/v1/projects/{PROJECT}", error_envelope(404, [("070401", "Project was not found.")]))
    text = await call_tool_error(server, "update_project", {"project_id": PROJECT, "title": "T"})
    assert text.startswith("Gorelo rejected update_project (HTTP 404, code 070401): Project was not found.")
    assert sent(mock_gorelo) == [("PATCH", f"/v1/projects/{PROJECT}")]


async def test_update_project_a_failed_reread_returns_the_warning_and_never_repeats_the_write(server, mock_gorelo):
    mock_gorelo.on("PATCH", f"/v1/projects/{PROJECT}", envelope({"Id": PROJECT}))
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}", httpx.ReadTimeout("slow"))
    result = await call_tool(server, "update_project", {"project_id": PROJECT, "title": "T"})
    assert result["Id"] == PROJECT and set(result) == {"Id", "warning"}
    assert "GET /v1/projects/{projectId} timed out" in result["warning"]
    assert result["warning"].endswith("Do not repeat the write; read it again later.")
    assert [r.method for r in mock_gorelo.requests] == ["PATCH", "GET"]


async def test_update_project_a_timeout_on_the_patch_is_unconfirmed(server, mock_gorelo):
    mock_gorelo.on("PATCH", f"/v1/projects/{PROJECT}", httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "update_project", {"project_id": PROJECT, "title": "T"})
    assert text.startswith("Gorelo did not confirm update_project (the request timed out).")
    assert "Verify with a read before retrying." in text
    assert sent(mock_gorelo) == [("PATCH", f"/v1/projects/{PROJECT}")]


def test_update_project_docstring_states_what_replaces_what_and_the_pairings():
    doc = " ".join(specs()["update_project"].fn.__doc__.split())
    for fragment in (
        "REPLACE the stored list", "only through clear_fields", "at least one change is required",
        "Changing client_id clears the shared contacts and needs location_id", "do not repeat it",
        "Projects cannot be deleted through this server (a delete removes every task in it).",
        # 2026-10-02: what clear_fields removes, what it cannot, and the StatusReason pairing
        "group_ids cannot be emptied: a new project always gets a group",
        "The description and the lead are removed only through clear_fields too",
        "status_reason is recorded only together with status_id",
    ):
        assert fragment in doc, fragment
    # the pairings live next to the parameter they constrain
    client = param_description("update_project", "client_id")
    assert "Needs location_id" in client and "the move clears its shared contacts" in client
    assert "required whenever client_id is given" in param_description("update_project", "location_id")
    lead = param_description("update_project", "lead_assignee_id")
    assert "cannot also be a watcher" in lead and "To remove the lead use clear_fields, never 0" in lead
    assert "The lead is never also a watcher" in param_description("update_project", "watcher_ids")  # as on create
    assert "The lead cannot be removed" not in lead  # Gorelo documents "Send 0 to remove the lead"
    assert "To remove the description use clear_fields" in param_description("update_project", "description")
    groups = param_description("update_project", "group_ids")
    assert "complete new list" in groups and "at least one" in groups and "clear" not in groups
    assert "The groups cannot be emptied" in groups and "always has" not in groups
    reason = param_description("update_project", "status_reason")
    assert "only when status_id is sent" in reason and "give both" in reason
    removable = param_description("update_project", "clear_fields")
    for name in ("description", "lead_assignee_id", "shared_with_contact_ids", "tag_ids", "watcher_ids"):
        assert name in removable, name
    assert "group_ids" not in removable
    closed = param_description("update_project", "closed_on")
    assert "Closed project" in closed and "status_id 5" in closed and "before the project was created" in closed
    assert "Completed" not in closed  # 2026-10-02: ClosedOn belongs to a Closed project, Completed carries no date
    assert "also becomes the last update" in closed  # ClosedOn "also becomes its UpdatedOn"
    assert "removes the target date" in param_description("update_project", "clear_target_date")
    assert "To remove it use clear_target_date" in param_description("update_project", "target_date")


@needs_spec_text
def test_the_author_of_an_update_is_recorded_as_api_when_omitted_and_the_params_say_so():
    # UpdateProjectCommand.UpdatedByName: "An API key is not a person, so when it is omitted or blank the actor is
    # recorded as API"; UpdateSectionCommand.UpdatedByName: "Omitted or blank, it is recorded as API". The tools leave
    # it out unless the caller gives it and refuse a blank one (a blank would be swallowed into API without a word).
    assert "omitted or blank the actor is recorded as `API`" in schema_fields("UpdateProjectCommand")["UpdatedByName"]
    assert "Omitted or blank, it is recorded as `API`" in schema_fields("UpdateSectionCommand")["UpdatedByName"]
    for tool in ("update_project", "update_project_section"):
        text = param_description(tool, "updated_by_name")
        assert "omitted, Gorelo records API (an API key is not a person)" in text and "Not a change by itself" in text, tool


async def test_an_update_sends_no_author_unless_the_caller_gave_one(server, mock_gorelo):
    route_update_project(mock_gorelo)
    mock_gorelo.on("PATCH", f"/v1/projects/{PROJECT}/sections/{SECTION}", envelope({"Id": SECTION}))
    await call_tool(server, "update_project", {"project_id": PROJECT, "title": "T"})
    await call_tool(server, "update_project_section", {"project_id": PROJECT, "section_id": SECTION, "title": "T"})
    patches = [r.json for r in mock_gorelo.requests if r.method == "PATCH"]
    assert patches == [{"Title": "T"}, {"Title": "T"}]


def test_update_project_says_closing_stamps_closed_on_and_reopening_clears_it():
    doc = " ".join(specs()["update_project"].fn.__doc__.split())
    assert "Moving a project to Closed stamps ClosedOn, and moving a Closed project to any other status clears it." in doc
    status = param_description("update_project", "status_id")
    assert "1 NotStarted, 2 InProgress, 3 OnHold, 4 Completed, 5 Closed." in status
    assert "Completed is normally reached automatically" in status and "Closed is the manual end state" in status
    assert "due" not in status.lower()


@pytest.mark.parametrize("old", [{"due_date": "2026-11-30T17:00:00Z"}, {"clear_due_date": True}])
async def test_update_project_has_no_due_date_parameters_any_more(server, mock_gorelo, old):
    # 2026-10-02: DueDate and ClearDueDate became TargetDate and ClearTargetDate (the tasks keep theirs).
    text = await call_tool_error(server, "update_project", {"project_id": PROJECT, **old})
    assert next(iter(old)) in text and "Unexpected keyword argument" in text
    assert mock_gorelo.requests == []


def test_the_project_parameters_that_hold_a_date_are_the_target_date_ones():
    names = set()
    for tool in ("list_projects", "create_project", "update_project"):
        names |= {p for p in accepted_params(tool) if re.search(r"(^|_)(due|target)(_|$)", p)}
    assert names == {"target_date_after", "target_date_before", "target_date", "clear_target_date"}


# --------------------------------------------------------------------------
# list_project_tags and list_project_types
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool, path, row",
    [
        ("list_project_tags", "/v1/projects/tags", {"Id": TAG_ID, "Name": "Migration", "Description": "", "CreatedOn": "2026-01-01T00:00:00Z", "UpdatedOn": None}),
        ("list_project_types", "/v1/projects/types", {"Id": TYPE_ID, "Name": "Rollout", "Description": "", "CreatedOn": "2026-01-01T00:00:00Z", "UpdatedOn": None}),
    ],
)
async def test_the_lookup_lists_are_unpaged_and_return_the_list_shape(server, mock_gorelo, tool, path, row):
    mock_gorelo.on("GET", path, envelope([row, {**row, "Id": uid(99)}]))
    result = await call_tool(server, tool, {})
    assert result == {"items": [row, {**row, "Id": uid(99)}], "count": 2}
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("GET", path, {}, None)


@pytest.mark.parametrize("tool, path", [("list_project_tags", "/v1/projects/tags"), ("list_project_types", "/v1/projects/types")])
async def test_the_lookup_lists_report_an_empty_set_as_zero_items(server, mock_gorelo, tool, path):
    mock_gorelo.on("GET", path, envelope([]))
    assert await call_tool(server, tool, {}) == {"items": [], "count": 0}


@pytest.mark.parametrize("tool, path", [("list_project_tags", "/v1/projects/tags"), ("list_project_types", "/v1/projects/types")])
async def test_the_lookup_lists_refuse_an_answer_that_is_not_a_list(server, mock_gorelo, tool, path):
    mock_gorelo.on("GET", path, envelope(None))
    text = await call_tool_error(server, tool, {})
    assert f"Gorelo returned an unexpected response for {tool}" in text and "expected Data to be a list" in text


@pytest.mark.parametrize("tool, path", [("list_project_tags", "/v1/projects/tags"), ("list_project_types", "/v1/projects/types")])
async def test_the_lookup_lists_take_no_arguments(server, mock_gorelo, tool, path):
    text = await call_tool_error(server, tool, {"page_size": 10})
    assert "page_size" in text
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# list_project_sections
# --------------------------------------------------------------------------


async def test_list_project_sections_is_unpaged_and_returns_the_list_shape(server, mock_gorelo):
    rows = [
        {"Id": SECTION, "Title": "Backlog", "Color": "#56A0F9", "TaskCount": 4},
        {"Id": uid(31), "Title": "Done", "Color": "#00AA00", "TaskCount": 0},
    ]
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}/sections", envelope(rows))
    assert await call_tool(server, "list_project_sections", {"project_id": PROJECT}) == {"items": rows, "count": 2}
    request = mock_gorelo.last
    assert (request.method, request.path, request.query) == ("GET", f"/v1/projects/{PROJECT}/sections", {})


async def test_list_project_sections_a_project_without_sections_is_zero_items(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}/sections", envelope([]))
    assert await call_tool(server, "list_project_sections", {"project_id": PROJECT}) == {"items": [], "count": 0}


async def test_list_project_sections_unknown_project(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}/sections", error_envelope(404, [("070401", "Project was not found.")]))
    text = await call_tool_error(server, "list_project_sections", {"project_id": PROJECT})
    assert text.startswith("Gorelo rejected list_project_sections (HTTP 404, code 070401): Project was not found.")


# --------------------------------------------------------------------------
# create_project_section
# --------------------------------------------------------------------------


async def test_create_project_section_sends_title_color_and_author_and_points_to_the_list_tool(server, mock_gorelo):
    mock_gorelo.on("POST", f"/v1/projects/{PROJECT}/sections", envelope({"Id": SECTION}))
    result = await call_tool(
        server,
        "create_project_section",
        {"project_id": PROJECT, "title": "Backlog", "color": "#56a0f9", "created_by_name": "Import job"},
    )
    request = mock_gorelo.last
    assert (request.method, request.path, request.query) == ("POST", f"/v1/projects/{PROJECT}/sections", {})
    assert request.json == {"Title": "Backlog", "Color": "#56a0f9", "CreatedByName": "Import job"}
    assert result == {"Id": SECTION, "note": module.SECTION_NOTE}
    assert "list_project_sections" in result["note"]
    assert len(mock_gorelo.requests) == 1  # there is no single-section read to call


async def test_create_project_section_without_a_color_lets_gorelo_choose(server, mock_gorelo):
    mock_gorelo.on("POST", f"/v1/projects/{PROJECT}/sections", envelope({"Id": SECTION}))
    await call_tool(server, "create_project_section", {"project_id": PROJECT, "title": "Backlog"})
    assert mock_gorelo.last.json == {"Title": "Backlog"}  # the documented default is Gorelo's, never sent by the tool


@needs_spec_text
def test_the_section_color_text_is_the_one_of_the_schema():
    # CreateSectionCommand.Color: optional, "omit it and the section gets #56A0F9", "A supplied blank is rejected"
    color = schema_fields("CreateSectionCommand")["Color"]
    assert "Optional - omit it and the section gets `#56A0F9`" in color and "A supplied blank is rejected" in color
    text = param_description("create_project_section", "color")
    assert "Omit it and the section gets #56A0F9" in text and "a blank value is refused" in text
    assert "Gorelo records API" in param_description("create_project_section", "created_by_name")


@pytest.mark.parametrize("tool, arguments", [
    ("create_project_section", {"title": "T", "color": ""}),
    ("create_project_section", {"title": "T", "color": "   "}),
    ("update_project_section", {"section_id": SECTION, "color": ""}),
    ("update_project_section", {"section_id": SECTION, "title": "T", "color": " "}),
])
async def test_a_blank_section_color_is_refused_locally_on_create_and_on_update(server, mock_gorelo, tool, arguments):
    # "A supplied blank is rejected: a section always has a colour" (create) and "a supplied blank is rejected" (update)
    text = await call_tool_error(server, tool, {"project_id": PROJECT, **arguments})
    assert text.startswith("color: expected a hex colour such as #56A0F9")
    assert mock_gorelo.requests == []


def test_the_section_writes_say_that_a_section_cannot_be_deleted_through_this_server():
    # a section delete removes every task in the section, so no tool offers it and the section tools say so
    for name in ("create_project_section", "update_project_section"):
        doc = " ".join(specs()[name].fn.__doc__.split())
        assert "Sections cannot be deleted through this server (a delete removes every task in it)." in doc, name
    assert "nothing is emailed" in " ".join(specs()["create_project_section"].fn.__doc__.split())


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"title": ""}, "title: must not be empty or whitespace only"),
        ({"title": "  "}, "title: must not be empty or whitespace only"),
        ({"title": "T", "color": ""}, "color: expected a hex colour such as #56A0F9"),
        ({"title": "T", "color": "red"}, "color: expected a hex colour such as #56A0F9"),
        ({"title": "T", "color": "56A0F9"}, "color: expected a hex colour such as #56A0F9"),
        ({"title": "T", "color": "#56A0F"}, "color: expected a hex colour such as #56A0F9"),
        ({"title": "T", "color": "#56A0F9F"}, "color: expected a hex colour such as #56A0F9"),
        ({"title": "T", "color": "#GGGGGG"}, "color: expected a hex colour such as #56A0F9"),
        ({"title": "T", "created_by_name": " "}, "created_by_name: must not be empty or whitespace only"),
    ],
)
async def test_create_project_section_local_validation_errors_send_nothing(server, mock_gorelo, arguments, fragment):
    text = await call_tool_error(server, "create_project_section", {"project_id": PROJECT, **arguments})
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_a_bad_color_is_not_quoted_back(server, mock_gorelo):
    text = await call_tool_error(server, "create_project_section", {"project_id": PROJECT, "title": "T", "color": "#GGGGGG"})
    assert text == "color: expected a hex colour such as #56A0F9 ('#' followed by 6 hex digits), got a string"
    assert mock_gorelo.requests == []


async def test_create_project_section_maps_gorelo_errors_to_the_param(server, mock_gorelo):
    mock_gorelo.on(
        "POST", f"/v1/projects/{PROJECT}/sections",
        error_envelope(400, [("070101", "Color must not be blank.", "Color"), ("070101", "Title is required.", "Title")]),
    )
    text = await call_tool_error(server, "create_project_section", {"project_id": PROJECT, "title": "T"})
    assert text == (
        "Gorelo rejected create_project_section (HTTP 400, code 070101): color: Color must not be blank.; "
        f"title: Title is required. [trace {TEST_TRACE_ID}]"
    )


@pytest.mark.parametrize("data", [None, {}, {"Id": ""}, {"Id": 0}, {"Id": False}, [SECTION], False])
async def test_create_project_section_an_answer_without_an_id_is_an_unconfirmed_write(server, mock_gorelo, data):
    mock_gorelo.on("POST", f"/v1/projects/{PROJECT}/sections", envelope(data))
    text = await call_tool_error(server, "create_project_section", {"project_id": PROJECT, "title": "T"})
    assert UNUSABLE_ID in text and VERIFY in text
    assert len(mock_gorelo.requests) == 1


# --------------------------------------------------------------------------
# update_project_section
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments, body",
    [
        ({"title": "Done"}, {"Title": "Done"}),
        ({"color": "#00AA00"}, {"Color": "#00AA00"}),
        ({"title": "Done", "color": "#00AA00", "updated_by_name": "Import job"}, {"Title": "Done", "Color": "#00AA00", "UpdatedByName": "Import job"}),
        ({"color": "#00aa00", "updated_by_name": "Import job"}, {"Color": "#00aa00", "UpdatedByName": "Import job"}),
    ],
)
async def test_update_project_section_sends_only_what_the_caller_gave(server, mock_gorelo, arguments, body):
    path = f"/v1/projects/{PROJECT}/sections/{SECTION}"
    mock_gorelo.on("PATCH", path, envelope({"Id": SECTION}))
    result = await call_tool(server, "update_project_section", {"project_id": PROJECT, "section_id": SECTION, **arguments})
    request = mock_gorelo.last
    assert (request.method, request.path, request.query) == ("PATCH", path, {})
    assert request.json == body
    assert result == {"Id": SECTION, "note": module.SECTION_NOTE}
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize("data", [None, {}, False, True, [SECTION], "ok"])
async def test_update_project_section_an_answer_that_is_not_an_object_is_an_unconfirmed_write(server, mock_gorelo, data):
    # It used to be reported as success with the section id; an answer that cannot be used proves nothing.
    mock_gorelo.on("PATCH", f"/v1/projects/{PROJECT}/sections/{SECTION}", envelope(data))
    text = await call_tool_error(server, "update_project_section", {"project_id": PROJECT, "section_id": SECTION, "title": "T"})
    assert text.startswith(
        "Gorelo returned an unexpected response for update_project_section: "
        "PATCH /v1/projects/{projectId}/sections/{sectionId}: expected Data to be a non-empty object but got "
    )
    assert VERIFY in text
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({}, "nothing to change: give title and/or color"),
        ({"updated_by_name": "Import job"}, "updated_by_name alone changes nothing"),
        ({"title": ""}, "title: must not be blank (a section always has a title)"),
        ({"title": "   "}, "title: must not be blank (a section always has a title)"),
        ({"color": ""}, "color: expected a hex colour such as #56A0F9"),
        ({"color": "blue"}, "color: expected a hex colour such as #56A0F9"),
        ({"title": "T", "updated_by_name": ""}, "updated_by_name: must not be empty or whitespace only"),
    ],
)
async def test_update_project_section_local_validation_errors_send_nothing(server, mock_gorelo, arguments, fragment):
    text = await call_tool_error(server, "update_project_section", {"project_id": PROJECT, "section_id": SECTION, **arguments})
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_update_project_section_has_no_clear_option(server, mock_gorelo):
    for arguments in ({"clear_fields": ["title"]}, {"clear_title": True}):
        text = await call_tool_error(server, "update_project_section", {"project_id": PROJECT, "section_id": SECTION, **arguments})
        assert "Unexpected keyword argument" in text
    assert mock_gorelo.requests == []


async def test_update_project_section_maps_gorelo_errors_to_the_param(server, mock_gorelo):
    mock_gorelo.on(
        "PATCH", f"/v1/projects/{PROJECT}/sections/{SECTION}",
        error_envelope(400, [("070101", "Color must not be blank.", "Color")]),
    )
    text = await call_tool_error(server, "update_project_section", {"project_id": PROJECT, "section_id": SECTION, "color": "#000000"})
    assert text == (
        f"Gorelo rejected update_project_section (HTTP 400, code 070101): color: Color must not be blank. [trace {TEST_TRACE_ID}]"
    )


async def test_update_project_section_unknown_section_is_a_404(server, mock_gorelo):
    mock_gorelo.on(
        "PATCH", f"/v1/projects/{PROJECT}/sections/{SECTION}",
        error_envelope(404, [("070401", "Section was not found.")]),
    )
    text = await call_tool_error(server, "update_project_section", {"project_id": PROJECT, "section_id": SECTION, "title": "T"})
    assert text.startswith("Gorelo rejected update_project_section (HTTP 404, code 070401): Section was not found.")


# --------------------------------------------------------------------------
# list_project_comments
# --------------------------------------------------------------------------


async def test_list_project_comments_on_the_project_level(server, mock_gorelo):
    rows = [comment_record(), comment_record(uid(41), BodyHtml="<p>Two</p>")]
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}/comments", paged_envelope(rows, next_cursor="n1", total_count=9))
    result = await call_tool(
        server, "list_project_comments", {"project_id": PROJECT, "sort_order": "asc", "page_size": 2, "cursor": "p1"}
    )
    request = mock_gorelo.last
    assert (request.method, request.path) == ("GET", f"/v1/projects/{PROJECT}/comments")
    assert request.query == {"SortOrder": "asc", "PageSize": "2", "Cursor": "p1"}
    assert result == {
        "items": rows, "count": 2, "total_count": 9, "has_more": True, "next_cursor": "n1", "page_size": 2,
        "filters": {"project_id": PROJECT, "sort_order": "asc"},
    }


async def test_list_project_comments_defaults_send_only_the_page_size(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}/comments", paged_envelope([]))
    result = await call_tool(server, "list_project_comments", {"project_id": PROJECT})
    assert mock_gorelo.last.query == {"PageSize": "50"}
    assert result["filters"] == {"project_id": PROJECT} and result["count"] == 0 and result["page_size"] == 50


async def test_list_project_comments_on_the_task_level_with_conversation_filters(server, mock_gorelo):
    path = f"/v1/projects/{PROJECT}/tasks/{TASK}/comments"
    mock_gorelo.on("GET", path, paged_envelope([comment_record()]))
    result = await call_tool(
        server,
        "list_project_comments",
        {"project_id": PROJECT, "task_id": TASK, "conversation_types": ["public", "private"], "sort_order": "desc"},
    )
    request = mock_gorelo.last
    assert (request.method, request.path) == ("GET", path)
    assert request.query == {"ConversationType": "1,2", "SortOrder": "desc", "PageSize": "50"}
    assert result["filters"] == {
        "project_id": PROJECT, "task_id": TASK, "conversation_types": ["public", "private"], "sort_order": "desc",
    }


@pytest.mark.parametrize("kind, type_id", [("side_conversation", "3"), ("approval", "4")])
async def test_list_project_comments_reads_one_conversation_with_its_single_type(server, mock_gorelo, kind, type_id):
    path = f"/v1/projects/{PROJECT}/tasks/{TASK}/comments"
    mock_gorelo.on("GET", path, paged_envelope([comment_record(ConversationId=CONVERSATION)]))
    result = await call_tool(
        server,
        "list_project_comments",
        {"project_id": PROJECT, "task_id": TASK, "conversation_types": [kind], "conversation_id": CONVERSATION},
    )
    assert mock_gorelo.last.query == {"ConversationType": type_id, "ConversationId": CONVERSATION, "PageSize": "50"}
    assert result["filters"]["conversation_id"] == CONVERSATION


async def test_list_project_comments_a_repeated_type_counts_once(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}/tasks/{TASK}/comments", paged_envelope([]))
    result = await call_tool(
        server,
        "list_project_comments",
        {"project_id": PROJECT, "task_id": TASK, "conversation_types": ["approval", "approval"], "conversation_id": CONVERSATION},
    )
    assert mock_gorelo.last.query["ConversationType"] == "4"
    assert result["filters"]["conversation_types"] == ["approval"]


async def test_list_project_comments_a_task_list_with_all_four_types(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}/tasks/{TASK}/comments", paged_envelope([]))
    await call_tool(
        server,
        "list_project_comments",
        {"project_id": PROJECT, "task_id": TASK, "conversation_types": ["approval", "side_conversation", "private", "public"]},
    )
    assert mock_gorelo.last.query["ConversationType"] == "4,3,2,1"


@pytest.mark.parametrize("given, used", [(1000, 200), (0, 1), (-1, 1), (75, 75)])
async def test_list_project_comments_clamps_the_page_size(server, mock_gorelo, given, used):
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}/comments", paged_envelope([]))
    result = await call_tool(server, "list_project_comments", {"project_id": PROJECT, "page_size": given})
    assert mock_gorelo.last.query["PageSize"] == str(used) and result["page_size"] == used


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        (
            {"conversation_types": ["public"]},
            "conversation_types: only task comments have conversation types; give task_id as well",
        ),
        (
            {"conversation_id": CONVERSATION},
            "conversation_id: only task comments belong to a conversation; give task_id as well",
        ),
        (
            {"task_id": TASK, "conversation_id": CONVERSATION},
            "conversation_id: needs conversation_types with exactly one type, side_conversation or approval "
            "(public and private comments have no conversation id); conversation_types was not given",
        ),
        (
            {"task_id": TASK, "conversation_id": CONVERSATION, "conversation_types": ["side_conversation", "approval"]},
            "conversation_id: needs conversation_types with exactly one type, side_conversation or approval "
            "(public and private comments have no conversation id); conversation_types is side_conversation, approval",
        ),
        (
            {"task_id": TASK, "conversation_id": CONVERSATION, "conversation_types": ["public"]},
            "conversation_id: needs conversation_types with exactly one type, side_conversation or approval "
            "(public and private comments have no conversation id); conversation_types is public",
        ),
        (
            {"task_id": TASK, "conversation_id": CONVERSATION, "conversation_types": ["private"]},
            "conversation_id: needs conversation_types with exactly one type, side_conversation or approval",
        ),
        (
            {"task_id": TASK, "conversation_id": CONVERSATION, "conversation_types": ["public", "approval"]},
            "conversation_id: needs conversation_types with exactly one type, side_conversation or approval",
        ),
        (
            {"task_id": TASK, "conversation_types": []},
            "conversation_types: the list must contain at least one type",
        ),
        ({"task_id": TASK, "conversation_id": "  ", "conversation_types": ["approval"]}, "conversation_id: must not be empty or whitespace only"),
        ({"task_id": "nope"}, "task_id: expected a GUID such as"),
    ],
)
async def test_list_project_comments_local_validation_errors_send_nothing(server, mock_gorelo, arguments, fragment):
    text = await call_tool_error(server, "list_project_comments", {"project_id": PROJECT, **arguments})
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"conversation_types": ["internal"]}, "conversation_types.0"),
        ({"sort_order": "newest"}, "sort_order"),
    ],
)
async def test_list_project_comments_refuses_values_outside_the_allowed_set(server, mock_gorelo, arguments, fragment):
    text = await call_tool_error(server, "list_project_comments", {"project_id": PROJECT, "task_id": TASK, **arguments})
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_list_project_comments_maps_a_gorelo_error_to_the_param(server, mock_gorelo):
    mock_gorelo.on(
        "GET", f"/v1/projects/{PROJECT}/tasks/{TASK}/comments",
        error_envelope(400, [("070101", "ConversationId needs a single ConversationType.", "ConversationId")]),
    )
    text = await call_tool_error(
        server, "list_project_comments",
        {"project_id": PROJECT, "task_id": TASK, "conversation_types": ["approval"], "conversation_id": CONVERSATION},
    )
    assert text == (
        "Gorelo rejected list_project_comments (HTTP 400, code 070101): conversation_id: ConversationId needs a "
        f"single ConversationType. [trace {TEST_TRACE_ID}]"
    )


@pytest.mark.parametrize("task", [False, True])
@pytest.mark.parametrize("blank", ["", "   "])
async def test_list_project_comments_a_blank_cursor_is_refused_locally(server, mock_gorelo, task, blank):
    # at the project level and at the task level
    arguments = {"project_id": PROJECT, "cursor": blank, **({"task_id": TASK} if task else {})}
    text = await call_tool_error(server, "list_project_comments", arguments)
    assert text == "cursor: must not be empty or whitespace only"
    assert mock_gorelo.requests == []


async def test_list_project_comments_follows_the_cursor(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}/comments", paged_responder([[comment_record()], [comment_record(uid(42))]]))
    first = await call_tool(server, "list_project_comments", {"project_id": PROJECT, "page_size": 1})
    second = await call_tool(server, "list_project_comments", {"project_id": PROJECT, "page_size": 1, "cursor": first["next_cursor"]})
    assert (first["has_more"], second["has_more"], second["next_cursor"]) == (True, False, None)
    assert second["items"][0]["Id"] == uid(42)


REACTIONS = [{"ReactionId": 1, "UserId": 9201}, {"ReactionId": 3, "UserId": 9202}]


@pytest.mark.parametrize("task", [False, True])
async def test_comment_reads_return_the_reactions_unchanged(server, mock_gorelo, task):
    # 2026-10-02: project comments carry Reactions (task comments already did): [{ReactionId, UserId}].
    base = f"/v1/projects/{PROJECT}" + (f"/tasks/{TASK}" if task else "")
    ids = {"project_id": PROJECT, **({"task_id": TASK} if task else {})}
    record = comment_record(Reactions=REACTIONS)
    mock_gorelo.on("GET", f"{base}/comments", paged_envelope([record, comment_record(uid(43))]))
    mock_gorelo.on("GET", f"{base}/comments/{COMMENT}", envelope(record))
    listed = await call_tool(server, "list_project_comments", ids)
    assert listed["items"][0]["Reactions"] == REACTIONS and listed["items"][1]["Reactions"] == []
    assert await call_tool(server, "get_project_comment", {**ids, "comment_id": COMMENT}) == record


def test_the_comment_reads_say_comments_carry_reactions():
    for name in ("list_project_comments", "get_project_comment"):
        doc = " ".join(specs()[name].fn.__doc__.split())
        assert "Reactions (the ReactionId and UserId of each reaction)" in doc, name
    assert "Reactions" not in " ".join(specs()["create_project_comment"].fn.__doc__.split())


@needs_spec_text
def test_the_comment_reads_repeat_the_two_warnings_of_their_texts():
    # "Attachment and inline-image links carry a time-limited access token that expires, so fetch the comment again
    # rather than storing a url" (both lists), and of the single task comment: "If that stored body cannot be reached,
    # BodyHtml is null rather than empty, to distinguish a body that is unavailable from a comment that has none."
    again = "fetch the comment again rather than storing a url"
    assert again in spec_text("get", "/v1/projects/{projectId}/comments")
    assert again in spec_text("get", "/v1/projects/{projectId}/tasks/{taskId}/comments")
    task_comment = spec_text("get", "/v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}")
    assert "`BodyHtml` is null rather than empty" in task_comment
    temporary = "Attachment urls are temporary links: use them now, or read the comment again for a fresh one; never store them."
    for name in ("list_project_comments", "get_project_comment"):
        assert temporary in " ".join(specs()[name].fn.__doc__.split()), name
    single = " ".join(specs()["get_project_comment"].fn.__doc__.split())
    assert "On a task comment a null BodyHtml means the stored body could not be reached, not that the comment is empty." in single
    assert "BodyHtml" not in " ".join(specs()["list_project_comments"].fn.__doc__.split())  # the list rows say BodyTruncated instead


def test_list_project_comments_docstring_warns_about_truncated_and_deleted_comments():
    doc = " ".join(specs()["list_project_comments"].fn.__doc__.split())
    for fragment in ("BodyTruncated true", "get_project_comment", "deleted comments with their body"):
        assert fragment in doc, fragment
    assert "Task comments only" in param_description("list_project_comments", "conversation_types")
    conversation = param_description("list_project_comments", "conversation_id")
    assert "Task comments only" in conversation and "list_task_conversations" in conversation
    assert "exactly that one type" in conversation


# --------------------------------------------------------------------------
# get_project_comment
# --------------------------------------------------------------------------


async def test_get_project_comment_on_the_project_level(server, mock_gorelo):
    record = comment_record()
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}/comments/{COMMENT}", envelope(record))
    assert await call_tool(server, "get_project_comment", {"project_id": PROJECT, "comment_id": COMMENT}) == record
    request = mock_gorelo.last
    assert (request.method, request.path, request.query) == ("GET", f"/v1/projects/{PROJECT}/comments/{COMMENT}", {})


async def test_get_project_comment_on_the_task_level(server, mock_gorelo):
    record = comment_record(Source={"Id": 1, "Name": "Api"})
    path = f"/v1/projects/{PROJECT}/tasks/{TASK}/comments/{COMMENT}"
    mock_gorelo.on("GET", path, envelope(record))
    assert await call_tool(server, "get_project_comment", {"project_id": PROJECT, "task_id": TASK, "comment_id": COMMENT}) == record
    assert (mock_gorelo.last.method, mock_gorelo.last.path) == ("GET", path)


async def test_get_project_comment_unknown_comment_is_a_404(server, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/projects/{PROJECT}/comments/{COMMENT}", error_envelope(404, [("070401", "Comment was not found.")]))
    text = await call_tool_error(server, "get_project_comment", {"project_id": PROJECT, "comment_id": COMMENT})
    assert text.startswith("Gorelo rejected get_project_comment (HTTP 404, code 070401): Comment was not found.")


# --------------------------------------------------------------------------
# create_project_comment
# --------------------------------------------------------------------------


def route_comment(mock, task=False, record=None):
    base = f"/v1/projects/{PROJECT}" + (f"/tasks/{TASK}" if task else "")
    mock.on("POST", f"{base}/comments", envelope({"Id": COMMENT}))
    mock.on("GET", f"{base}/comments/{COMMENT}", envelope(record if record is not None else comment_record()))
    return base


async def test_create_project_comment_defaults_to_a_private_project_comment_and_returns_the_reread_record(server, mock_gorelo):
    record = comment_record(BodyHtml="<p>Kickoff done</p>")
    base = route_comment(mock_gorelo, record=record)
    result = await call_tool(server, "create_project_comment", {"project_id": PROJECT, "body": "<p>Kickoff done</p>"})
    assert result == record
    assert sent(mock_gorelo) == [("POST", f"{base}/comments"), ("GET", f"{base}/comments/{COMMENT}")]
    assert mock_gorelo.requests[0].json == {"Body": "<p>Kickoff done</p>", "ConversationTypeId": 2}
    assert mock_gorelo.requests[0].query == {} and mock_gorelo.requests[1].query == {}


async def test_create_project_comment_public_on_the_project(server, mock_gorelo):
    route_comment(mock_gorelo)
    await call_tool(server, "create_project_comment", {"project_id": PROJECT, "body": "<p>Hi</p>", "conversation_type": "public"})
    assert mock_gorelo.requests[0].json == {"Body": "<p>Hi</p>", "ConversationTypeId": 1}


@pytest.mark.parametrize(
    "kind, type_id, conversation_id",
    [("private", 2, None), ("public", 1, None), ("side_conversation", 3, CONVERSATION), ("approval", 4, CONVERSATION)],
)
async def test_create_project_comment_on_a_task_maps_each_conversation_type(server, mock_gorelo, kind, type_id, conversation_id):
    base = route_comment(mock_gorelo, task=True)
    arguments = {"project_id": PROJECT, "task_id": TASK, "body": "<p>Hi</p>", "conversation_type": kind}
    if conversation_id:
        arguments["conversation_id"] = conversation_id
    await call_tool(server, "create_project_comment", arguments)
    assert sent(mock_gorelo) == [("POST", f"{base}/comments"), ("GET", f"{base}/comments/{COMMENT}")]
    expected = {"Body": "<p>Hi</p>", "ConversationTypeId": type_id}
    if conversation_id:
        expected["ConversationId"] = conversation_id
    assert mock_gorelo.requests[0].json == expected


async def test_create_project_comment_sends_every_optional_field(server, mock_gorelo):
    route_comment(mock_gorelo, task=True)
    await call_tool(
        server,
        "create_project_comment",
        {
            "project_id": PROJECT, "task_id": TASK, "body": "<p>See file</p>", "body_text": "See file",
            "conversation_type": "side_conversation", "conversation_id": CONVERSATION,
            "attachments": [
                {"name": "plan.pdf", "url": "https://files.example.test/a?token=1"},
                {"name": "photo.png", "url": "https://files.example.test/b?token=2"},
            ],
            "created_by_name": "Import job", "created_on": "2026-09-01T10:00:00-04:00",
        },
    )
    assert mock_gorelo.requests[0].json == {
        "Body": "<p>See file</p>", "BodyText": "See file", "ConversationTypeId": 3, "ConversationId": CONVERSATION,
        "Attachments": [
            {"Name": "plan.pdf", "Url": "https://files.example.test/a?token=1"},
            {"Name": "photo.png", "Url": "https://files.example.test/b?token=2"},
        ],
        "CreatedByName": "Import job", "CreatedOn": "2026-09-01T14:00:00Z",
    }


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"conversation_type": "side_conversation"}, "conversation_type: a project comment can only be private or public, not side_conversation"),
        ({"conversation_type": "approval"}, "conversation_type: a project comment can only be private or public, not approval"),
        ({"conversation_type": "side_conversation", "conversation_id": CONVERSATION}, "a project comment can only be private or public"),
        ({"conversation_id": CONVERSATION}, "conversation_id: project comments have no side conversations or approvals"),
        ({"conversation_type": "public", "conversation_id": CONVERSATION}, "conversation_id: project comments have no side conversations or approvals"),
        ({"task_id": TASK, "conversation_type": "side_conversation"}, "conversation_id: required when conversation_type is side_conversation"),
        ({"task_id": TASK, "conversation_type": "approval"}, "conversation_id: required when conversation_type is approval"),
        ({"task_id": TASK, "conversation_type": "private", "conversation_id": CONVERSATION}, "conversation_id: not accepted for a private comment"),
        ({"task_id": TASK, "conversation_type": "public", "conversation_id": CONVERSATION}, "conversation_id: not accepted for a public comment"),
        ({"task_id": TASK, "conversation_id": CONVERSATION}, "conversation_id: not accepted for a private comment"),
        ({"task_id": TASK, "conversation_type": "approval", "conversation_id": " "}, "conversation_id: must not be empty or whitespace only"),
        ({"body": ""}, "body: must not be empty or whitespace only"),
        ({"body": "   "}, "body: must not be empty or whitespace only"),
        ({"body_text": ""}, "body_text: must not be empty or whitespace only"),
        ({"attachments": []}, "attachments: must not be an empty list"),
        ({"attachments": [{"name": " ", "url": "https://x.test/a"}]}, "attachments[0].name: must not be empty or whitespace only"),
        ({"attachments": [{"name": "a.pdf", "url": ""}]}, "attachments[0].url: must not be empty or whitespace only"),
        ({"created_by_name": ""}, "created_by_name: must not be empty or whitespace only"),
        ({"created_on": "2026-09-01T10:00:00"}, "created_on: '2026-09-01T10:00:00' has no UTC offset"),
        ({"created_on": "yesterday"}, "created_on: 'yesterday' is not an ISO 8601 datetime"),
        ({"task_id": "nope"}, "task_id: expected a GUID such as"),
    ],
)
async def test_create_project_comment_local_validation_errors_send_nothing(server, mock_gorelo, arguments, fragment):
    call = {"project_id": PROJECT, "body": "<p>Hi</p>", **arguments}
    text = await call_tool_error(server, "create_project_comment", call)
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "arguments, fragments",
    [
        ({"conversation_type": "internal"}, ["conversation_type", "'public', 'private', 'side_conversation' or 'approval'"]),
        ({"conversation_type": "Public"}, ["conversation_type"]),
        ({"attachments": [{"name": "a.pdf"}]}, ["attachments.0.url", "Field required"]),
        ({"attachments": [{"url": "https://x.test/a"}]}, ["attachments.0.name", "Field required"]),
        ({"attachments": [{"name": "a", "url": "https://x.test/a", "size": 3}]}, ["attachments.0.size", "Extra inputs are not permitted"]),
        ({"attachments": ["a.pdf"]}, ["attachments.0"]),
    ],
)
async def test_create_project_comment_refuses_values_the_schema_does_not_allow(server, mock_gorelo, arguments, fragments):
    text = await call_tool_error(server, "create_project_comment", {"project_id": PROJECT, "body": "<p>Hi</p>", **arguments})
    for fragment in fragments:
        assert fragment in text
    assert mock_gorelo.requests == []


@needs_spec_text
def test_the_comment_conversation_rules_are_the_ones_the_two_schemas_state():
    project = schema_fields("CreateProjectCommentCommand")
    assert "`1` Public or `2` Private" in project["ConversationTypeId"]
    assert "Defaults to 2 (Private)" in project["ConversationTypeId"].replace("**", "")
    assert "`3` and `4` are rejected" in project["ConversationTypeId"]
    assert "sending one is a 400" in project["ConversationId"]
    task = schema_fields("CreateTaskCommentCommand")
    assert "`1` Public, `2` Private, `3` Side Conversation, `4` Approval" in task["ConversationTypeId"]
    assert "Defaults to 2 (Private)" in task["ConversationTypeId"].replace("**", "")
    assert "Required for Side Conversation and Approval, and rejected for Public and Private" in task["ConversationId"]
    # BodyText is markdown (not plain text), on both comment commands
    for fields in (project, task):
        assert fields["BodyText"] == "The same body as markdown, if you keep one."
    assert param_description("create_project_comment", "body_text") == "The same body as markdown, if you keep one."


@pytest.mark.parametrize("task", [False, True], ids=["project comment", "task comment"])
@pytest.mark.parametrize("kind", ["public", "private", "side_conversation", "approval"])
@pytest.mark.parametrize("with_conversation", [False, True], ids=["no conversation_id", "conversation_id"])
async def test_create_project_comment_enforces_exactly_the_conversation_rules_of_the_two_schemas(
    server, mock_gorelo, task, kind, with_conversation
):
    # project comment: ConversationTypeId 1 Public or 2 Private only, and ANY ConversationId is a 400;
    # task comment: ConversationId required for 3 and 4, rejected for 1 and 2.
    if task:
        accepted = with_conversation == (kind in ("side_conversation", "approval"))
    else:
        accepted = kind in ("public", "private") and not with_conversation
    route_comment(mock_gorelo, task=task)
    arguments = {"project_id": PROJECT, "body": "<p>Hi</p>", "conversation_type": kind}
    if task:
        arguments["task_id"] = TASK
    if with_conversation:
        arguments["conversation_id"] = CONVERSATION
    if accepted:
        await call_tool(server, "create_project_comment", arguments)
        body = mock_gorelo.requests[0].json
        assert body["ConversationTypeId"] == {"public": 1, "private": 2, "side_conversation": 3, "approval": 4}[kind]
        assert ("ConversationId" in body) is with_conversation
    else:
        text = await call_tool_error(server, "create_project_comment", arguments)
        assert text.startswith(("conversation_type: ", "conversation_id: ")), text
        assert mock_gorelo.requests == []


async def test_create_project_comment_without_a_type_is_private_on_both_levels(server, mock_gorelo):
    # "Defaults to 2 (Private)": the tool says so and sends 2, the documented default, on a project and on a task
    for task in (False, True):
        route_comment(mock_gorelo, task=task)
        arguments = {"project_id": PROJECT, "body": "<p>Hi</p>", **({"task_id": TASK} if task else {})}
        await call_tool(server, "create_project_comment", arguments)
        assert mock_gorelo.requests[0].json == {"Body": "<p>Hi</p>", "ConversationTypeId": 2}
        mock_gorelo.reset()


async def test_create_project_comment_requires_a_body(server, mock_gorelo):
    text = await call_tool_error(server, "create_project_comment", {"project_id": PROJECT})
    assert "body" in text and "Missing required argument" in text
    assert mock_gorelo.requests == []


async def test_create_project_comment_maps_gorelo_errors_to_the_param(server, mock_gorelo):
    mock_gorelo.on(
        "POST", f"/v1/projects/{PROJECT}/tasks/{TASK}/comments",
        error_envelope(
            400,
            [
                ("070101", "Body is required.", "Body"),
                ("070101", "The conversation does not belong to this task.", "ConversationId"),
                ("070101", "Created on cannot be in the future.", "CreatedOn"),
                ("070101", "The url is not valid.", "Attachments[0].Url"),
            ],
        ),
    )
    text = await call_tool_error(
        server, "create_project_comment",
        {"project_id": PROJECT, "task_id": TASK, "body": "<p>x</p>", "conversation_type": "approval", "conversation_id": CONVERSATION},
    )
    assert text == (
        "Gorelo rejected create_project_comment (HTTP 400, code 070101): body: Body is required.; "
        "conversation_id: The conversation does not belong to this task.; created_on: Created on cannot be in the "
        f"future.; attachments (item 1, Url): The url is not valid. [trace {TEST_TRACE_ID}]"
    )
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize("task", [False, True])
async def test_create_project_comment_a_failed_reread_returns_the_warning_and_never_repeats_the_write(server, mock_gorelo, task):
    base = f"/v1/projects/{PROJECT}" + (f"/tasks/{TASK}" if task else "")
    mock_gorelo.on("POST", f"{base}/comments", envelope({"Id": COMMENT}))
    mock_gorelo.on("GET", f"{base}/comments/{COMMENT}", error_envelope(404, [("070401", "Comment was not found.")]))
    arguments = {"project_id": PROJECT, "body": "<p>Hi</p>", **({"task_id": TASK} if task else {})}
    result = await call_tool(server, "create_project_comment", arguments)
    assert set(result) == {"Id", "warning"} and result["Id"] == COMMENT
    assert result["warning"].startswith("the write succeeded; re-reading it failed: GET /v1/projects/{projectId}")
    assert result["warning"].endswith("Do not repeat the write; read it again later.")
    assert [r.method for r in mock_gorelo.requests] == ["POST", "GET"]


async def test_create_project_comment_a_timeout_says_the_comment_may_or_may_not_exist_and_is_not_retried(server, mock_gorelo):
    mock_gorelo.on("POST", f"/v1/projects/{PROJECT}/comments", httpx.ConnectError("refused"))
    text = await call_tool_error(server, "create_project_comment", {"project_id": PROJECT, "body": "<p>Hi</p>", "conversation_type": "public"})
    assert text == (
        "Gorelo did not confirm create_project_comment (the connection failed). The change may or may not have been "
        "applied. Verify with a read before retrying."
    )
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize("task", [False, True])
@pytest.mark.parametrize("data", [None, {}, {"Id": ""}, {"Id": 0}, {"Id": True}, [COMMENT], False])
async def test_create_project_comment_an_answer_without_an_id_is_an_unconfirmed_write(server, mock_gorelo, task, data):
    base = f"/v1/projects/{PROJECT}" + (f"/tasks/{TASK}" if task else "")
    mock_gorelo.on("POST", f"{base}/comments", envelope(data))
    arguments = {"project_id": PROJECT, "body": "<p>Hi</p>", **({"task_id": TASK} if task else {})}
    text = await call_tool_error(server, "create_project_comment", arguments)
    assert UNUSABLE_ID in text and VERIFY in text
    assert len(mock_gorelo.requests) == 1  # nothing is posted a second time


def test_create_project_comment_docstring_states_who_is_emailed_and_the_waiting_state():
    doc = " ".join(specs()["create_project_comment"].fn.__doc__.split())
    for fragment in (
        "The default is private: internal, emails nobody", "waiting-on-contact state", "fires automation",
        "emails recipients", "that conversation's recipients", "the approvers", "Tell the user who will be emailed",
        "Gorelo does not document its recipients for projects or tasks, so confirm with the user first",
        "do not post it again",
    ):
        assert fragment in doc, fragment
    kinds = param_description("create_project_comment", "conversation_type")
    assert "Project comments allow only those" in kinds and "side_conversation and approval" in kinds
    # 2026-10-02: the schema texts of BodyText (markdown), CreatedByName (API when omitted) and CreatedOn (now when omitted)
    assert param_description("create_project_comment", "body_text") == "The same body as markdown, if you keep one."
    assert "Gorelo records API" in param_description("create_project_comment", "created_by_name")
    created = param_description("create_project_comment", "created_on")
    assert "omitted, now" in created and "importing a comment that existed elsewhere" in created
    attachments = param_description("create_project_comment", "attachments")
    # only the name and url, as upload_attachment returned them
    assert "upload_attachment" in attachments
    assert "Pass only the name and url from upload_attachment, unchanged" in attachments


# --------------------------------------------------------------------------
# delete_project_comment
# --------------------------------------------------------------------------


@pytest.mark.parametrize("task", [False, True])
async def test_delete_project_comment_refuses_without_confirm_and_makes_no_http_call(dserver, mock_gorelo, task):
    arguments = {"project_id": PROJECT, "comment_id": COMMENT, **({"task_id": TASK} if task else {})}
    text = await call_tool_error(dserver, "delete_project_comment", arguments)
    level = "task" if task else "project"
    assert text.startswith(f"confirm: refusing to delete {level} comment {COMMENT} without confirm=true.")
    assert "soft-deletes that one private comment" in text
    assert f"Call again with confirm=true if you really want to delete {level} comment {COMMENT}." in text
    assert mock_gorelo.requests == []


async def test_delete_project_comment_confirm_false_is_refused_too(dserver, mock_gorelo):
    text = await call_tool_error(dserver, "delete_project_comment", {"project_id": PROJECT, "comment_id": COMMENT, "confirm": False})
    assert text.startswith("confirm: refusing to delete project comment")
    assert mock_gorelo.requests == []


async def test_delete_project_comment_on_the_project_level(dserver, mock_gorelo):
    path = f"/v1/projects/{PROJECT}/comments/{COMMENT}"
    mock_gorelo.on("DELETE", path, envelope({"Id": COMMENT}))
    result = await call_tool(dserver, "delete_project_comment", {"project_id": PROJECT, "comment_id": COMMENT, "confirm": True})
    assert result == {"Id": COMMENT}
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("DELETE", path, {}, None)
    assert len(mock_gorelo.requests) == 1  # no re-read: Gorelo may still return deleted comments


async def test_delete_project_comment_on_the_task_level(dserver, mock_gorelo):
    path = f"/v1/projects/{PROJECT}/tasks/{TASK}/comments/{COMMENT}"
    mock_gorelo.on("DELETE", path, envelope({"Id": COMMENT}))
    result = await call_tool(
        dserver, "delete_project_comment", {"project_id": PROJECT, "task_id": TASK, "comment_id": COMMENT, "confirm": True}
    )
    assert result == {"Id": COMMENT}
    assert (mock_gorelo.last.method, mock_gorelo.last.path) == ("DELETE", path)


@pytest.mark.parametrize("task", [False, True])
@pytest.mark.parametrize("data", [None, True, False, {}, [COMMENT], "ok"])
async def test_delete_project_comment_an_answer_that_is_not_an_object_is_an_unconfirmed_write(dserver, mock_gorelo, task, data):
    # a delete answers with Gorelo's Data object ({"Id": ...}); null, a bare boolean or {} is never success.
    base = f"/v1/projects/{PROJECT}" + (f"/tasks/{TASK}" if task else "")
    mock_gorelo.on("DELETE", f"{base}/comments/{COMMENT}", envelope(data))
    arguments = {"project_id": PROJECT, "comment_id": COMMENT, "confirm": True, **({"task_id": TASK} if task else {})}
    text = await call_tool_error(dserver, "delete_project_comment", arguments)
    # the advice is the delete's own (repeat it, do not read), not the generic "verify with a read"
    assert_delete_advice(text, "its answer could not be used")
    assert len(mock_gorelo.requests) == 1  # not retried by the tool


async def test_delete_project_comment_a_public_comment_is_refused_by_gorelo(dserver, mock_gorelo):
    mock_gorelo.on(
        "DELETE", f"/v1/projects/{PROJECT}/comments/{COMMENT}",
        error_envelope(409, [("070409", "Only a private comment can be deleted (ResourceNotDeletable).")]),
    )
    text = await call_tool_error(dserver, "delete_project_comment", {"project_id": PROJECT, "comment_id": COMMENT, "confirm": True})
    assert text == (
        "Gorelo rejected delete_project_comment (HTTP 409, code 070409): Only a private comment can be deleted "
        f"(ResourceNotDeletable). [trace {TEST_TRACE_ID}]"
    )
    assert len(mock_gorelo.requests) == 1


def delete_target(task):
    """(the path of the delete, its arguments) at the project level or at the task level."""
    base = f"/v1/projects/{PROJECT}" + (f"/tasks/{TASK}" if task else "")
    arguments = {"project_id": PROJECT, "comment_id": COMMENT, "confirm": True, **({"task_id": TASK} if task else {})}
    return f"{base}/comments/{COMMENT}", arguments


@pytest.mark.parametrize("task", [False, True])
async def test_delete_project_comment_a_timeout_is_unconfirmed_and_not_retried(dserver, mock_gorelo, task):
    path, arguments = delete_target(task)
    mock_gorelo.on("DELETE", path, httpx.ReadTimeout("slow"))
    text = await call_tool_error(dserver, "delete_project_comment", arguments)
    assert_delete_advice(text, "the request timed out")
    assert "[trace" not in text  # a timeout has no answer, so no trace id
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize("task", [False, True])
async def test_delete_project_comment_after_a_connection_failure_says_repeating_is_safe(dserver, mock_gorelo, task):
    path, arguments = delete_target(task)
    mock_gorelo.on("DELETE", path, httpx.ConnectError("refused"))
    text = await call_tool_error(dserver, "delete_project_comment", arguments)
    assert_delete_advice(text, "the connection failed")
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize("task", [False, True])
async def test_delete_project_comment_after_a_5xx_envelope_gives_gorelos_message_the_advice_and_the_trace_id(
    dserver, mock_gorelo, task
):
    path, arguments = delete_target(task)
    mock_gorelo.on("DELETE", path, error_envelope(500, [("070500", "Boom.")]))
    text = await call_tool_error(dserver, "delete_project_comment", arguments)
    assert_delete_advice(text, "Gorelo answered HTTP 500: Boom.")
    assert text.endswith(f" [trace {TEST_TRACE_ID}]")
    assert len(mock_gorelo.requests) == 1


async def test_delete_project_comment_after_a_gateway_page_says_repeating_is_safe(dserver, mock_gorelo):
    path, arguments = delete_target(False)
    mock_gorelo.on("DELETE", path, httpx.Response(502, text="<html>Bad Gateway</html>"))
    text = await call_tool_error(dserver, "delete_project_comment", arguments)
    assert_delete_advice(text, "Gorelo answered HTTP 502")
    assert len(mock_gorelo.requests) == 1


async def test_delete_project_comment_a_refusal_is_not_an_unconfirmed_write_and_keeps_the_normal_text(dserver, mock_gorelo):
    # a 409 or a 404 applied nothing, so "may already be deleted" and "repeating is safe" would be wrong
    path, arguments = delete_target(True)
    mock_gorelo.on("DELETE", path, error_envelope(404, [("070404", "Comment not found.")]))
    text = await call_tool_error(dserver, "delete_project_comment", arguments)
    assert "may already be deleted" not in text and "Repeating the delete" not in text
    assert text == trace_text("Gorelo rejected delete_project_comment (HTTP 404, code 070404): Comment not found.")


async def test_delete_project_comment_a_rate_limit_that_persists_is_not_an_unconfirmed_write(dserver, mock_gorelo):
    path, arguments = delete_target(False)
    mock_gorelo.on("DELETE", path, httpx.Response(429, headers={"Retry-After": "100000"}, json={"error": "slow"}))
    text = await call_tool_error(dserver, "delete_project_comment", arguments)
    assert "rate limiting" in text and "did not process this request" in text and "may already be deleted" not in text


def test_delete_project_comment_docstring_states_what_is_deleted():
    doc = " ".join(specs()["delete_project_comment"].fn.__doc__.split())
    for fragment in (
        "PRIVATE comment", "soft delete", "recovered in the app", "HTTP 409, ResourceNotDeletable",
        "already deleted succeeds, so repeating a delete is safe", "nothing is emailed", "Do not re-read to verify",
        "Ask the user first; needs confirm=true.",  # the consistency rule of every destructive tool
    ):
        assert fragment in doc, fragment
    assert "Ask the user first" in param_description("delete_project_comment", "confirm")


async def test_project_delete_and_section_delete_cannot_be_reached_through_any_tool(dserver, mock_gorelo):
    names = {t.name for t in await list_tools(dserver)}
    assert not {n for n in names if n in ("delete_project", "delete_project_section")}
    for name in ("delete_project", "delete_project_section"):
        text = await call_tool_error(dserver, name, {"project_id": PROJECT, "confirm": True})
        assert "unknown tool" in text.lower()
    assert mock_gorelo.requests == []
