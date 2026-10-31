"""Project tasks, task conversations and task approvals (toolset "projects").

The tools in this module (10):

    list_project_tasks, get_project_task, create_project_task, update_project_task, delete_project_task
    list_task_conversations, create_task_side_conversation, create_task_approval, get_task_approval
    update_task_comment

Task comments are posted, read and deleted with the comment tools in tools/projects.py (give them
task_id); the side conversation or approval created here supplies their conversation_id. update_task_comment (1.5.0)
edits a PRIVATE task comment: Body replaces the text, Attachments replaces the files outright (the tool reads the comment
first, with the GET of get_project_comment, and refuses to drop a file silently), UpdatedByName is the editor. Gorelo
refuses a public, side conversation or approval comment (409 ResourceUpdateBlocked, they were already sent outside the
account), notifies nobody and records the edit as UpdatedOn. Its helpers are the ones of update_project_comment in
tools/projects.py, repeated here on purpose: no tool module imports another.

An API key without the Project scope gets a 403 (code 080203), which reaches the model through
format_gorelo_error's missing-scope message. The offline tests cover these tools.

Contract e15cb5a18ec2 (2026-10-02) changed tasks as follows, and this module follows it: CreateTaskCommand now
requires SectionId (create_project_task has a required section_id), and task rows (list and detail) carry IsUnread and
IsWaitingOnThem. Tasks keep their DueDate and ClearDueDate; only the PROJECT due date became TargetDate.

The same contract documents every request field for the first time, and the tools follow those texts:

* update_project_task removes a value only through clear_fields, the explicit opt-in: the lists (Gorelo: send an
  empty array), the banner (Gorelo: send an empty string) and the lead (Gorelo: send 0). A bare blank value, an
  empty list or a bare 0 is refused and points to clear_fields. Gorelo documents the lead removal; it has not been
  probed live. A task's due date is removed with clear_due_date, never by omission.
* status_reason is read by Gorelo only when status_id is sent, so it is refused without status_id.
* status_id and the status_ids filter are checked against the task scale (1 Not started to 4 Done) before any request. The
  project scale is another one (it adds In progress, Completed and Closed), so a project status such as 5 Closed is
  refused here instead of being sent to a task. The scale is the one CreateTaskCommand.StatusId, UpdateTaskCommand.StatusId
  and the StatusIds filter of the task list document.
* section_id of an update must be an active section of the same project (text only: it needs a read to check).
* the CreatedOn and UpdatedOn of a new task state no rule (UpdatedOn only "Defaults to CreatedOn"), so none is
  checked: each needs an offset, and nothing is compared with the local clock or with the other one.
* updated_by_name is recorded as API when omitted, and a blank one is refused here instead of being swallowed into API.
* clear_fields is limited to the names TASK_CLEAR_VALUES lists in the schema and again in the code, so a direct call
  cannot turn another field into a blank.
* the list rows carry BlockedByTaskIds and BlockingTaskIds (TaskListItemModel), although the text of the single task
  read calls the relationships a field of the single task; the docstring follows the schema.

Ops owned by this module:

    GET /v1/projects/{projectId}/tasks
    GET /v1/projects/{projectId}/tasks/{taskId}
    POST /v1/projects/{projectId}/tasks
    PATCH /v1/projects/{projectId}/tasks/{taskId}
    DELETE /v1/projects/{projectId}/tasks/{taskId}
    GET /v1/projects/{projectId}/tasks/{taskId}/conversations
    POST /v1/projects/{projectId}/tasks/{taskId}/conversations/side-conversation
    POST /v1/projects/{projectId}/tasks/{taskId}/conversations/approval
    GET /v1/projects/{projectId}/tasks/{taskId}/approvals/{approvalId}
    PATCH /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}
    GET /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}   (the read before and after the edit)
"""

import re
from collections.abc import Callable
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field

from gorelo_client import GoreloAPIError
from tools._common import (
    MAX_ID,
    StrictBool,
    StrictId,
    build_body,
    clamp_page_size,
    client_of,
    created_id,
    csv_ids,
    describe_value,
    expect_object,
    gorelo_tool,
    guid,
    guids,
    list_result,
    non_empty,
    paged_result,
    positive_id,
    positive_ids,
    reread_after_write,
    require_confirm,
    utc_iso,
)

TOOLSET = "projects"

# Operation keys, exactly as in spec/spec_index.json.
LIST_TASKS = "GET /v1/projects/{projectId}/tasks"
GET_TASK = "GET /v1/projects/{projectId}/tasks/{taskId}"
CREATE_TASK = "POST /v1/projects/{projectId}/tasks"
UPDATE_TASK = "PATCH /v1/projects/{projectId}/tasks/{taskId}"
DELETE_TASK = "DELETE /v1/projects/{projectId}/tasks/{taskId}"
LIST_CONVERSATIONS = "GET /v1/projects/{projectId}/tasks/{taskId}/conversations"
CREATE_SIDE_CONVERSATION = "POST /v1/projects/{projectId}/tasks/{taskId}/conversations/side-conversation"
CREATE_APPROVAL = "POST /v1/projects/{projectId}/tasks/{taskId}/conversations/approval"
GET_APPROVAL = "GET /v1/projects/{projectId}/tasks/{taskId}/approvals/{approvalId}"
UPDATE_TASK_COMMENT = "PATCH /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}"
GET_TASK_COMMENT = "GET /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}"

SortOrder = Literal["asc", "desc"]
# What clear_fields of update_project_task may remove (the explicit opt-in; see the module docstring).
TaskClearable = Literal[
    "assisting_assignee_ids",
    "watcher_ids",
    "blocked_by_task_ids",
    "blocking_task_ids",
    "agent_asset_ids",
    "custom_asset_ids",
    "uptime_ids",
    "banner",
    "lead_assignee_id",
]

# Field maps: {snake_case param: "PascalField"}. They build the request and turn a Gorelo PropertyName
# back into the param to fix, so the list map holds the query names too (PageSize and Cursor included).
LIST_TASKS_MAP = {
    "section_ids": "SectionIds",
    "status_ids": "StatusIds",
    "assignee_ids": "AssigneeIds",
    "lead_assignee_ids": "LeadAssigneeIds",
    "incomplete_only": "IncompleteOnly",
    "due_after": "DueAfter",
    "due_before": "DueBefore",
    "sort_by": "SortBy",
    "sort_order": "SortOrder",
    "page_size": "PageSize",
    "cursor": "Cursor",
}
CREATE_TASK_MAP = {
    "title": "Title",
    "section_id": "SectionId",
    "lead_assignee_id": "LeadAssigneeId",
    "assisting_assignee_ids": "AssistingAssigneeIds",
    "watcher_ids": "WatcherIds",
    "priority_id": "PriorityId",
    "status_id": "StatusId",
    "due_date": "DueDate",
    "created_by_name": "CreatedByName",
    "created_on": "CreatedOn",
    "updated_on": "UpdatedOn",
}
UPDATE_TASK_MAP = {
    "title": "Title",
    "section_id": "SectionId",
    "status_id": "StatusId",
    "status_reason": "StatusReason",
    "priority_id": "PriorityId",
    "due_date": "DueDate",
    "clear_due_date": "ClearDueDate",
    "lead_assignee_id": "LeadAssigneeId",
    "assisting_assignee_ids": "AssistingAssigneeIds",
    "watcher_ids": "WatcherIds",
    "blocked_by_task_ids": "BlockedByTaskIds",
    "blocking_task_ids": "BlockingTaskIds",
    "agent_asset_ids": "AgentAssetIds",
    "custom_asset_ids": "CustomAssetIds",
    "uptime_ids": "UptimeIds",
    "banner": "Banner",
    "updated_by_name": "UpdatedByName",
}
# What clear_fields may remove, and what Gorelo expects on the wire for each (UpdateTaskCommand and the text of
# PATCH /v1/projects/{projectId}/tasks/{taskId}): [] for the lists ("Lists replace outright: send an empty array to
# clear one"), "" for the banner ("Send an empty string to remove it") and 0 for the lead ("Send 0 to remove the lead").
TASK_LIST_PARAMS = (
    "assisting_assignee_ids",
    "watcher_ids",
    "blocked_by_task_ids",
    "blocking_task_ids",
    "agent_asset_ids",
    "custom_asset_ids",
    "uptime_ids",
)
TASK_CLEAR_VALUES: dict[str, Any] = {
    **{param: [] for param in TASK_LIST_PARAMS},
    "banner": "",
    "lead_assignee_id": 0,
}
UPDATE_COMMENT_MAP = {
    "body": "Body",
    "attachments": "Attachments",
    "updated_by_name": "UpdatedByName",
}
SIDE_CONVERSATION_MAP = {"name": "Name", "email": "Email", "cc_emails": "CcEmails"}
APPROVAL_MAP = {"name": "Name", "contact_ids": "ContactIds"}

# The task status scale ("1 Not started, 2 Working on it, 3 On hold, 4 Done"): StatusId of a task create and update, and the
# StatusIds filter of the task list. A project has its own scale (see tools/projects.py), which a task never takes.
TASK_STATUS_LABELS = {1: "Not started", 2: "Working on it", 3: "On hold", 4: "Done"}
TASK_STATUS_SCALE = ", ".join(f"{status} {label}" for status, label in TASK_STATUS_LABELS.items())
TASK_STATUSES = f"{TASK_STATUS_SCALE}."
SIDE_CONVERSATION_NOTE = (
    "Gorelo answers with the conversation Id only and has no single-conversation read. Nothing is sent until "
    "you post a comment into it: create_project_comment with task_id, conversation_type=\"side_conversation\" "
    "and conversation_id set to this Id. See a task's conversations with list_task_conversations."
)

_EMAIL = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+")
PRIORITIES = "0 None, 1 Urgent, 2 High, 3 Normal, 4 Low"


# --------------------------------------------------------------------------
# Private helpers (shared helpers live in tools/_common.py only)
# --------------------------------------------------------------------------


def _given(check: Callable[[str, Any], Any], param: str, value: Any) -> Any:
    """check(param, value) for a value the caller gave; None (not given) stays None."""
    return None if value is None else check(param, value)


def _priority(value: Any) -> int | None:
    """PriorityId: the one scale tickets use, 0 None to 4 Low. None stays None.

    A whole number is quoted back (it is not caller data worth hiding); anything else is only described."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
        shown = value if isinstance(value, int) and not isinstance(value, bool) else describe_value(value)
        raise ValueError(f"priority_id: expected {PRIORITIES}, got {shown}")
    return value


def _status(param: str, value: Any) -> int:
    """One status of a task: 1 Not started to 4 Done. A caller's None means "not given", so wrap it with _given.

    The project scale is another one (it adds In progress, Completed and Closed), so a project status such as 5 is refused
    here, with zero HTTP calls, instead of being sent to a task. Every whole number outside the scale is answered with the
    scale and quoted back (it is not caller data worth hiding), 0 and the negative numbers included: positive_id's
    "expected a positive whole number such as 123" would suggest that any id will do. Whatever else is not an id (a
    boolean, a decimal, text, a list, or a number beyond int64, which is never quoted) keeps the message of positive_id."""
    if isinstance(value, int) and not isinstance(value, bool) and -MAX_ID <= value < 1:
        status = int(value)
    else:
        status = positive_id(param, value)
    if status not in TASK_STATUS_LABELS:
        raise ValueError(f"{param}: expected {TASK_STATUS_SCALE}, got {status}")
    return status


def _statuses(param: str, values: Any) -> list[int] | None:
    """The status_ids filter of the task list: every item on the task scale, a bad one named by its index.

    None, an empty list and a value that is not a list keep the messages of positive_ids."""
    if isinstance(values, (list, tuple)) and values:
        return [_status(f"{param}[{index}]", item) for index, item in enumerate(values)]
    return positive_ids(param, values)


def _email(param: str, value: Any) -> str:
    """One email address (no list, no display name). Gorelo does the real validation.

    The message never repeats the value: it is a third party's address and tool errors reach the logs."""
    if not isinstance(value, str) or not _EMAIL.fullmatch(value):
        raise ValueError(
            f"{param}: expected one email address such as vendor@example.com (a single address, no list, no display name)"
        )
    return value


def _clear_list(clear_fields: list[str] | None) -> list[str]:
    """The distinct names in clear_fields, in order, each one a field update_project_task can remove.

    [] is an error: omit clear_fields instead. The schema (TaskClearable) already limits the names, but a direct call
    skips it, so a name outside TASK_CLEAR_VALUES is refused here too (every field of the body is in the field map, and
    a name such as title or due_date would otherwise be sent as a blank)."""
    if clear_fields is None:
        return []
    non_empty("clear_fields", clear_fields)
    for index, name in enumerate(clear_fields):
        if not isinstance(name, str):
            raise ValueError(
                f"clear_fields[{index}]: expected a field name such as \"banner\", got {describe_value(name)}"
            )
    names = list(dict.fromkeys(clear_fields))
    for name in names:
        if name not in TASK_CLEAR_VALUES:
            raise ValueError(f"clear_fields: {name!r} cannot be cleared; allowed: {', '.join(TASK_CLEAR_VALUES)}")
    return names


def _refuse_empty_lists(lists: dict[str, Any]) -> None:
    """Lists replace the stored ones, so [] is never a value; emptying one takes an explicit clear_fields."""
    for param, value in lists.items():
        if isinstance(value, list) and not value:
            raise ValueError(
                f"{param}: an empty list is not accepted because lists replace the stored one; "
                f"to remove every entry pass clear_fields=[\"{param}\"]"
            )


def _clearable_text(param: str, value: Any) -> Any:
    """Text that is removed only through clear_fields: a blank value is refused and points there."""
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{param}: a blank value is not accepted; to remove it pass clear_fields=[\"{param}\"]")
    return value


def _changed_lead(value: Any) -> int | None:
    """lead_assignee_id of an update: None stays None, anything else must be a technician id.

    0 is how Gorelo removes the lead ("Send 0 to remove the lead"), and only clear_fields may ask for that, so a
    bare 0 is refused here and points to it."""
    if value == 0 and not isinstance(value, bool):
        raise ValueError(
            "lead_assignee_id: 0 is not a technician id; to remove the lead pass clear_fields=[\"lead_assignee_id\"]"
        )
    return None if value is None else positive_id("lead_assignee_id", value)


def _query(filters: dict[str, Any], field_map: dict[str, str]) -> dict[str, Any]:
    """The query of a list call: lists become comma separated, None drops out, PascalCase names."""
    values = {
        param: csv_ids(param, value) if isinstance(value, list) else value
        for param, value in filters.items()
        if param in field_map
    }
    return build_body(values, field_map)


def _task_ids(project_id: str, task_id: str) -> dict[str, str]:
    """Validated path ids of a task call, named by the snake_case params."""
    return {"projectId": guid("project_id", project_id), "taskId": guid("task_id", task_id)}


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


@gorelo_tool(toolset=TOOLSET, kind="read", ops=[LIST_TASKS], field_map=LIST_TASKS_MAP)
async def list_project_tasks(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    section_ids: Annotated[
        list[str] | None, Field(description="Section ids (list_project_sections).")
    ] = None,
    status_ids: Annotated[
        list[StrictId] | None, Field(description=f"{TASK_STATUSES} Omit for every status.")
    ] = None,
    assignee_ids: Annotated[
        list[StrictId] | None, Field(description="Technician ids (list_org_users); matches lead OR assisting.")
    ] = None,
    lead_assignee_ids: Annotated[
        list[StrictId] | None, Field(description="Technician ids (list_org_users); matches the lead only.")
    ] = None,
    incomplete_only: Annotated[bool | None, Field(description="true drops tasks in Done.")] = None,
    due_after: Annotated[
        str | None,
        Field(description="Due at or after this instant. Tasks with no due date match neither due filter."),
    ] = None,
    due_before: Annotated[str | None, Field(description="Due strictly before this instant.")] = None,
    sort_by: Annotated[
        Literal["boardOrder", "createdOn", "updatedOn"] | None, Field(description="Default boardOrder.")
    ] = None,
    sort_order: Annotated[SortOrder | None, Field(description="Default asc.")] = None,
    page_size: Annotated[int, Field(description="Rows per page, 1-200 (clamped).")] = 50,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous page.")] = None,
) -> dict:
    """List a project's tasks, in the board's own order by default, and return one page. Rows carry IsUnread and IsWaitingOnThem (true while the task waits on the client rather than on the service provider). Rows also carry BlockedByTaskIds and BlockingTaskIds (the tasks that must finish before this one, and the tasks it blocks), and LastUpdate, whose UpdateType is one of a fixed set of event names (it and Summary are empty for any other recorded event); get_project_task adds the linked assets, status details, recorded time, products and banner. There is no assigned-to-me filter: use assignee_ids.
    Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.
    """
    pid = guid("project_id", project_id)
    filters = {
        "project_id": pid,
        "section_ids": guids("section_ids", section_ids),
        "status_ids": _statuses("status_ids", status_ids),
        "assignee_ids": positive_ids("assignee_ids", assignee_ids),
        "lead_assignee_ids": positive_ids("lead_assignee_ids", lead_assignee_ids),
        "incomplete_only": incomplete_only,
        "due_after": utc_iso("due_after", due_after),
        "due_before": utc_iso("due_before", due_before),
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    token = non_empty("cursor", cursor)
    page = await client_of(ctx).get_page(
        LIST_TASKS,
        path_params={"projectId": pid},
        query=_query(filters, LIST_TASKS_MAP),
        page_size=clamp_page_size(page_size),
        cursor=token,
        tool="list_project_tasks",
    )
    return paged_result(page, filters)


@gorelo_tool(toolset=TOOLSET, kind="read", ops=[GET_TASK])
async def get_project_task(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    task_id: Annotated[str, Field(description="Task id (list_project_tasks).")],
) -> dict:
    """Get one task in full: linked assets and uptime checks, blocking relationships (both directions), status reason and details, recorded time, products and banner, plus the IsUnread and IsWaitingOnThem flags. LastUpdate.UpdateType is one of a fixed set of event names (it and Summary are empty for any other recorded event)."""
    data = await client_of(ctx).get_one(
        GET_TASK, path_params=_task_ids(project_id, task_id), tool="get_project_task"
    )
    return expect_object(data, GET_TASK, tool="get_project_task")


@gorelo_tool(toolset=TOOLSET, kind="write", ops=[CREATE_TASK, GET_TASK], field_map=CREATE_TASK_MAP)
async def create_project_task(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    title: Annotated[str, Field(description="Task title.")],
    section_id: Annotated[str, Field(description="Board section the task goes in (list_project_sections).")],
    lead_assignee_id: Annotated[
        StrictId | None, Field(description="Lead technician id (list_org_users).")
    ] = None,
    assisting_assignee_ids: Annotated[
        list[StrictId] | None, Field(description="Assisting technician ids (list_org_users).")
    ] = None,
    watcher_ids: Annotated[
        list[StrictId] | None, Field(description="Watcher technician ids (list_org_users).")
    ] = None,
    priority_id: Annotated[
        StrictId | None, Field(description=f"{PRIORITIES}. Omit and Gorelo uses Normal.")
    ] = None,
    status_id: Annotated[
        StrictId | None, Field(description=f"{TASK_STATUSES} Omit and Gorelo uses 1 Not started.")
    ] = None,
    due_date: Annotated[str | None, Field(description="Due date (ISO 8601 with UTC offset).")] = None,
    created_by_name: Annotated[
        str | None,
        Field(description="Creator name to record; omitted, Gorelo records API (an API key is not a person). Imports only."),
    ] = None,
    created_on: Annotated[
        str | None,
        Field(
            description="When the task was created (ISO 8601 with UTC offset); omitted, now. "
            "Supply it only when importing a task that existed elsewhere."
        ),
    ] = None,
    updated_on: Annotated[
        str | None,
        Field(description="When the task was last updated (ISO 8601 with UTC offset); omitted, it equals created_on. Imports only."),
    ] = None,
) -> dict:
    """Create a task on a project's board and return its record. Required: title and section_id (a project with no section needs create_project_section first); everything else is optional.
    Side effects: creates a real task. Creating a task moves a not-started project into progress, exactly as in the app, and fires the same automation triggers, except that a task created with created_on (a backdated import) sends no emails or notifications. If the result is {Id, warning} the task exists: do not create it again.
    """
    pid = guid("project_id", project_id)
    non_empty("title", title)
    values = {
        "title": title,
        "section_id": guid("section_id", section_id),
        "lead_assignee_id": _given(positive_id, "lead_assignee_id", lead_assignee_id),
        "assisting_assignee_ids": positive_ids("assisting_assignee_ids", assisting_assignee_ids),
        "watcher_ids": positive_ids("watcher_ids", watcher_ids),
        "priority_id": _priority(priority_id),
        "status_id": _given(_status, "status_id", status_id),
        "due_date": utc_iso("due_date", due_date),
        "created_by_name": created_by_name,
        "created_on": utc_iso("created_on", created_on),
        "updated_on": utc_iso("updated_on", updated_on),
    }
    body = build_body(values, CREATE_TASK_MAP)
    written = await client_of(ctx).post(
        CREATE_TASK, path_params={"projectId": pid}, json_body=body, tool="create_project_task"
    )
    new_id = created_id(written, CREATE_TASK, tool="create_project_task")
    return await reread_after_write(
        ctx,
        GET_TASK,
        path_params={"projectId": pid, "taskId": new_id},
        tool="create_project_task",
        written_id=new_id,
    )


@gorelo_tool(
    toolset=TOOLSET, kind="write", ops=[UPDATE_TASK, GET_TASK], field_map=UPDATE_TASK_MAP, destructive_hint=True
)
async def update_project_task(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    task_id: Annotated[str, Field(description="Task id (list_project_tasks).")],
    title: Annotated[str | None, Field(description="New title; not blank.")] = None,
    section_id: Annotated[
        str | None,
        Field(description="Move to this section (list_project_sections); it must be an active section of this same project."),
    ] = None,
    status_id: Annotated[StrictId | None, Field(description=TASK_STATUSES)] = None,
    status_reason: Annotated[
        str | None,
        Field(description="Reason recorded with the status. Gorelo reads it only when status_id is sent, so give both."),
    ] = None,
    priority_id: Annotated[StrictId | None, Field(description=f"{PRIORITIES}.")] = None,
    due_date: Annotated[
        str | None, Field(description="New due date (ISO 8601 with UTC offset). To remove it use clear_due_date.")
    ] = None,
    clear_due_date: Annotated[
        bool | None, Field(description="true removes the due date; not together with due_date.")
    ] = None,
    lead_assignee_id: Annotated[
        StrictId | None,
        Field(description="Lead technician id (list_org_users). To remove the lead use clear_fields, never 0."),
    ] = None,
    assisting_assignee_ids: Annotated[
        list[StrictId] | None,
        Field(description="Assisting technician ids (list_org_users): the complete new list. To remove them all use clear_fields."),
    ] = None,
    watcher_ids: Annotated[
        list[StrictId] | None,
        Field(description="Watcher technician ids (list_org_users): the complete new list. To remove them all use clear_fields."),
    ] = None,
    blocked_by_task_ids: Annotated[
        list[str] | None, Field(description="Tasks that block this one (list_project_tasks).")
    ] = None,
    blocking_task_ids: Annotated[
        list[str] | None, Field(description="Tasks this one blocks (list_project_tasks).")
    ] = None,
    agent_asset_ids: Annotated[
        list[str] | None, Field(description="Linked agent assets (list_agents).")
    ] = None,
    custom_asset_ids: Annotated[
        list[str] | None, Field(description="Linked custom assets (list_custom_assets).")
    ] = None,
    uptime_ids: Annotated[
        list[str] | None, Field(description="Linked uptime checks (list_uptime_checks).")
    ] = None,
    banner: Annotated[
        str | None, Field(description="New banner text; not blank. To remove the banner use clear_fields.")
    ] = None,
    updated_by_name: Annotated[
        str | None,
        Field(
            description="Author name to record; omitted, Gorelo records API (an API key is not a person). "
            "Not a change by itself."
        ),
    ] = None,
    clear_fields: Annotated[
        list[TaskClearable] | None,
        Field(
            description="Fields to remove: banner, lead_assignee_id (removes the lead) or a list field "
            "(assisting_assignee_ids, watcher_ids, ...). Not together with a value for the same field."
        ),
    ] = None,
) -> dict:
    """Change fields of one task and return its updated record. Send only what changes; at least one change is required. Lists (assisting_assignee_ids, watcher_ids, blocked_by_task_ids, blocking_task_ids, agent_asset_ids, custom_asset_ids, uptime_ids) REPLACE the stored list, so send the complete new list; empty one only through clear_fields. The banner and the lead are removed only through clear_fields too.
    Side effects: overwrites the fields given. Changing status_id fires the same automation the app fires; status_reason is recorded only together with status_id. If the result is {Id, warning} the change was applied: do not repeat it.
    """
    ids = _task_ids(project_id, task_id)
    clear = _clear_list(clear_fields)
    _refuse_empty_lists(
        {
            "assisting_assignee_ids": assisting_assignee_ids,
            "watcher_ids": watcher_ids,
            "blocked_by_task_ids": blocked_by_task_ids,
            "blocking_task_ids": blocking_task_ids,
            "agent_asset_ids": agent_asset_ids,
            "custom_asset_ids": custom_asset_ids,
            "uptime_ids": uptime_ids,
        }
    )
    values = {
        "title": title,
        "section_id": _given(guid, "section_id", section_id),
        "status_id": _given(_status, "status_id", status_id),
        "status_reason": non_empty("status_reason", status_reason),
        "priority_id": _priority(priority_id),
        "due_date": utc_iso("due_date", due_date),
        "clear_due_date": True if clear_due_date is True else None,
        "lead_assignee_id": _changed_lead(lead_assignee_id),
        "assisting_assignee_ids": positive_ids("assisting_assignee_ids", assisting_assignee_ids),
        "watcher_ids": positive_ids("watcher_ids", watcher_ids),
        "blocked_by_task_ids": guids("blocked_by_task_ids", blocked_by_task_ids),
        "blocking_task_ids": guids("blocking_task_ids", blocking_task_ids),
        "agent_asset_ids": guids("agent_asset_ids", agent_asset_ids),
        "custom_asset_ids": guids("custom_asset_ids", custom_asset_ids),
        "uptime_ids": guids("uptime_ids", uptime_ids),
        "banner": _clearable_text("banner", banner),
        "updated_by_name": updated_by_name,
    }
    if values["status_reason"] is not None and values["status_id"] is None:
        raise ValueError("status_reason: only recorded together with status_id; pass the status as well")
    if values["clear_due_date"] and values["due_date"] is not None:
        raise ValueError("due_date: cannot be set while clear_due_date is true; give only one of them")
    for param in ("blocked_by_task_ids", "blocking_task_ids"):
        if ids["taskId"] in (values[param] or []):
            raise ValueError(f"{param}: a task cannot block or be blocked by itself; drop task_id from {param}")
    body = build_body(values, UPDATE_TASK_MAP, clear=clear, clear_values=TASK_CLEAR_VALUES)
    if not set(body) - {"UpdatedByName"}:
        changeable = ", ".join(param for param in UPDATE_TASK_MAP if param != "updated_by_name")
        raise ValueError(
            f"nothing to change: give at least one of {changeable} or clear_fields "
            "(updated_by_name alone changes nothing)"
        )
    written = await client_of(ctx).patch(UPDATE_TASK, path_params=ids, json_body=body, tool="update_project_task")
    expect_object(written, UPDATE_TASK, tool="update_project_task")
    return await reread_after_write(
        ctx, GET_TASK, path_params=ids, tool="update_project_task", written_id=ids["taskId"]
    )


@gorelo_tool(toolset=TOOLSET, kind="destructive", ops=[DELETE_TASK])
async def delete_project_task(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    task_id: Annotated[str, Field(description="Task id (list_project_tasks).")],
    confirm: Annotated[StrictBool, Field(description="Must be true to delete. Ask the user first.")] = False,
) -> dict:
    """Delete one task of a project. This is a soft delete: the task stops appearing anywhere and stops being an automation target, but its history survives and it can be recovered in the app. Blocking relationships are cleared in both directions and any automation timer waiting on it is dropped.
    Side effects: nothing is emailed. Ask the user first; needs confirm=true.
    """
    ids = _task_ids(project_id, task_id)
    require_confirm(
        confirm,
        action=f"delete task {ids['taskId']}",
        effect="This soft-deletes the task and clears its blocking relationships in both directions; it can be recovered in the app.",
    )
    data = await client_of(ctx).delete(DELETE_TASK, path_params=ids, tool="delete_project_task")
    return expect_object(data, DELETE_TASK, tool="delete_project_task")


# --------------------------------------------------------------------------
# Task conversations and approvals
# --------------------------------------------------------------------------


@gorelo_tool(toolset=TOOLSET, kind="read", ops=[LIST_CONVERSATIONS])
async def list_task_conversations(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    task_id: Annotated[str, Field(description="Task id (list_project_tasks).")],
) -> dict:
    """List a task's conversations (Id, Type, Name, Email, CcEmails, CreatedOn). Not paged. Public and Private (the main thread) have a null Id; side conversations and approvals carry the Id to pass as conversation_id (create_project_comment, list_project_comments) or approval_id (get_task_approval)."""
    items = await client_of(ctx).get_list(
        LIST_CONVERSATIONS, path_params=_task_ids(project_id, task_id), tool="list_task_conversations"
    )
    return list_result(items)


@gorelo_tool(
    toolset=TOOLSET, kind="write", ops=[CREATE_SIDE_CONVERSATION], field_map=SIDE_CONVERSATION_MAP
)
async def create_task_side_conversation(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    task_id: Annotated[str, Field(description="Task id (list_project_tasks).")],
    name: Annotated[
        str, Field(description="Display name of the person the conversation is with, e.g. the vendor contact.")
    ],
    email: Annotated[
        str, Field(description="That person's email address: one address outside the task's own contacts, e.g. a vendor.")
    ],
    cc_emails: Annotated[
        list[str] | None, Field(description="Addresses to copy on the conversation, one per entry.")
    ] = None,
) -> dict:
    """Start a side conversation on a task, a thread with somebody outside its own contacts, and return its Id. Gorelo has no single-conversation read: see it with list_task_conversations.
    Side effects: creates the conversation empty and emails nobody. Nothing is emailed until a comment is posted into it (create_project_comment with conversation_type="side_conversation" and this conversation_id).
    """
    ids = _task_ids(project_id, task_id)
    non_empty("name", name)
    ccs = None
    if cc_emails is not None:
        non_empty("cc_emails", cc_emails)
        ccs = [_email(f"cc_emails[{index}]", item) for index, item in enumerate(cc_emails)]
    body = build_body({"name": name, "email": _email("email", email), "cc_emails": ccs}, SIDE_CONVERSATION_MAP)
    written = await client_of(ctx).post(
        CREATE_SIDE_CONVERSATION, path_params=ids, json_body=body, tool="create_task_side_conversation"
    )
    created_id(written, CREATE_SIDE_CONVERSATION, tool="create_task_side_conversation")
    return {**written, "note": SIDE_CONVERSATION_NOTE}


@gorelo_tool(toolset=TOOLSET, kind="write", ops=[CREATE_APPROVAL, GET_APPROVAL], field_map=APPROVAL_MAP)
async def create_task_approval(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    task_id: Annotated[str, Field(description="Task id (list_project_tasks).")],
    name: Annotated[str, Field(description="What the contacts are asked to approve.")],
    contact_ids: Annotated[
        list[StrictId],
        Field(
            description="Approver contact ids (list_contacts): active contacts of the task's client that carry an "
            "approver contact tag. At least one."
        ),
    ],
) -> dict:
    """Ask contacts to approve something on a task and return the approval. Each contact must be an active contact of the task's client AND carry an approver contact tag; tags are set in the Gorelo UI (the API cannot set them), and an ineligible contact is rejected before anything is written. Approvers start Pending: get_task_approval has the status.
    Side effects: creates the approval and puts the task into a waiting-on-contact state. Gorelo does not say whether creating it notifies the approvers, so tell the user they may be notified before you create one. The request text is posted with create_project_comment (task_id, conversation_type="approval", conversation_id = this Id), which emails the approvers. If the result is {Id, warning} the approval exists: do not create it again.
    """
    ids = _task_ids(project_id, task_id)
    non_empty("name", name)
    if not contact_ids:
        raise ValueError("contact_ids: give at least one contact id (an approval needs approvers)")
    body = build_body({"name": name, "contact_ids": positive_ids("contact_ids", contact_ids)}, APPROVAL_MAP)
    written = await client_of(ctx).post(CREATE_APPROVAL, path_params=ids, json_body=body, tool="create_task_approval")
    approval_id = created_id(written, CREATE_APPROVAL, tool="create_task_approval")
    return await reread_after_write(
        ctx,
        GET_APPROVAL,
        path_params={**ids, "approvalId": approval_id},
        tool="create_task_approval",
        written_id=approval_id,
    )


@gorelo_tool(toolset=TOOLSET, kind="read", ops=[GET_APPROVAL])
async def get_task_approval(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    task_id: Annotated[str, Field(description="Task id (list_project_tasks).")],
    approval_id: Annotated[
        str, Field(description="Id of an Approval row in list_task_conversations.")
    ],
) -> dict:
    """Get one approval on a task with its approvers. This is where the approval status lives: Denied as soon as anyone denies, Approved once everyone approves, Pending otherwise. list_task_conversations does not carry it."""
    ids = {**_task_ids(project_id, task_id), "approvalId": guid("approval_id", approval_id)}
    data = await client_of(ctx).get_one(GET_APPROVAL, path_params=ids, tool="get_task_approval")
    return expect_object(data, GET_APPROVAL, tool="get_task_approval")


# --------------------------------------------------------------------------
# Editing a comment (update_task_comment): the attachments are REPLACED outright, so the tool reads first
# --------------------------------------------------------------------------

ATTACHMENTS_MAX = 100  # UpdateTaskCommentCommand.Attachments: "Max 100"


class CommentAttachment(BaseModel):
    # One {name, url} pair, as upload_attachment returned it. No description text on purpose: this schema is part of the
    # tool listing (paid for in every conversation) and the attachments parameter explains it.

    model_config = ConfigDict(extra="forbid")

    name: str
    url: str


class CommentAttachmentsWouldBeDropped(ValueError):
    """The edit would remove current attachments the caller did not name. The text names them (the user's own file names,
    needed to fix the call), so the error carries log_summary: server.LogValueFilter logs that summary alone, without the
    traceback and without a file name (the same rule as AttachmentsWouldBeDropped in tools/documents.py)."""

    def __init__(self, message: str, log_summary: str = "") -> None:
        super().__init__(message)
        self.log_summary = log_summary

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.args[0], self.log_summary))


def _unconfirmed(op_key: str, detail: str) -> GoreloAPIError:
    """The write-unconfirmed shape error: Gorelo answered 2xx, but the read-back does not show the edit."""
    return GoreloAPIError(
        f"{op_key}: {detail}; the write may have been applied, so verify it with a read before repeating it "
        "(get_project_comment with task_id)",
        status=200,
        op_key=op_key,
        kind="shape",
        write_unconfirmed=True,
    )


def _url_key(url: str) -> str:
    """A url without its query string and fragment: the time-limited token is taken to be in the query (not probed), and
    Gorelo issues a fresh one on every read, so two urls of the same file differ only there."""
    parts = urlsplit(url.strip())
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"


def _attachment_pairs(items: list[CommentAttachment] | None) -> list[tuple[str, str]] | None:
    """The (name, url) pairs of the attachments parameter, checked. [] is an error: nothing is removed by omission."""
    if items is None:
        return None
    if not items:
        raise ValueError(
            "attachments: an empty list is not accepted because the list replaces the stored one; to remove files "
            "name them in remove_attachment_names (naming every current file removes them all)"
        )
    pairs: list[tuple[str, str]] = []
    seen: dict[tuple[str, str], int] = {}
    for index, item in enumerate(items):
        name, url = item.name, item.url
        if not name.strip():
            raise ValueError(f"attachments[{index}].name: must not be empty or whitespace only")
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            raise ValueError(
                f"attachments[{index}].url: expected the http or https url that upload_attachment returned "
                f"(or the one the comment read shows for a current attachment), got {describe_value(url)}"
            )
        key = (name, _url_key(url))
        if key in seen:
            raise ValueError(f"attachments[{index}]: repeats attachments[{seen[key]}] (same name and same file)")
        seen[key] = index
        pairs.append((name, url))
    return pairs


def _drop_names(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if not value:
        raise ValueError("remove_attachment_names: the list must contain at least one name (omit it to remove nothing)")
    for index, name in enumerate(value):
        if not name.strip():
            raise ValueError(f"remove_attachment_names[{index}]: must not be empty or whitespace only")
    return list(value)


def _current_attachments(record: Any, op_key: str) -> list[dict[str, str]]:
    """The Attachments of the comment Gorelo returned, each with a usable Name and Url. Anything else is a shape error
    BEFORE any write: the tool will not compare against a list it cannot read."""
    rows = record.get("Attachments") if isinstance(record, dict) else None
    if not isinstance(rows, list):
        raise GoreloAPIError(
            f"{op_key}: expected Attachments to be a list but got {describe_value(rows)}; refusing to guess, so "
            "nothing was changed",
            status=200,
            op_key=op_key,
            kind="shape",
        )
    found: list[dict[str, str]] = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("Name"), str)
            or not row["Name"].strip()
            or not isinstance(row.get("Url"), str)
            or not row["Url"].strip()
        ):
            raise GoreloAPIError(
                f"{op_key}: an attachment of the comment has no usable Name and Url; refusing to guess, so nothing "
                "was changed",
                status=200,
                op_key=op_key,
                kind="shape",
            )
        found.append({"Name": row["Name"], "Url": row["Url"]})
    return found


def _keys(rows: list[dict[str, str]]) -> list[tuple[str, str]]:
    return [(row["Name"], _url_key(row["Url"])) for row in rows]


def _final_attachments(
    comment_id: str,
    current: list[dict[str, str]],
    given: list[tuple[str, str]] | None,
    drops: list[str] | None,
) -> list[dict[str, str]]:
    """The whole attachment list to send, from the current list, the list the caller gave and the names to drop.

    Without `given`, the list is the current one minus the rows of the dropped names (urls from the read); a dropped name
    that more than one current attachment carries is refused, because it does not say which one. With `given`, an entry
    that matches a current attachment (same name and same url without its query) is that attachment (the url of the READ is
    sent, it is the fresher one) and any other entry is a new file. Each current attachment that is neither kept nor named
    in `drops` is refused: nothing is dropped silently. The result may be empty only when `drops` names every current
    attachment (the caller said so)."""
    keys = _keys(current)
    if given is not None and len(set(keys)) < len(keys):
        raise ValueError(
            "attachments: the comment lists the same file twice (same name and file), and this tool cannot keep or "
            "remove one copy only; change its attachments in the Gorelo app. Nothing was changed."
        )
    names = {row["Name"] for row in current}
    unknown = [index for index, name in enumerate(drops or []) if name not in names]
    if unknown:
        listed = ", ".join(f"remove_attachment_names[{index}]" for index in unknown)
        raise ValueError(f"{listed}: the comment has no current attachment with that name (the comment read lists them)")
    if given is None:
        for index, name in enumerate(drops or []):
            count = len([row for row in current if row["Name"] == name])
            if count > 1:
                raise ValueError(
                    f"remove_attachment_names[{index}]: {count} current attachments have that name, so it does not say "
                    "which one to remove; list the ones to keep in attachments with their name and url as the comment "
                    "read shows them, and name it here to remove the others. Nothing was changed."
                )
        final = [row for row in current if row["Name"] not in (drops or [])]
    else:
        by_key = dict(zip(keys, current))
        final = []
        kept: set[tuple[str, str]] = set()
        for name, url in given:
            key = (name, _url_key(url))
            if key in by_key:
                kept.add(key)
                final.append(by_key[key])
            else:
                final.append({"Name": name, "Url": url})
        for index, name in enumerate(drops or []):
            if all(key in kept for key in _keys([row for row in current if row["Name"] == name])):
                raise ValueError(
                    f"remove_attachment_names[{index}]: every current attachment with that name is also kept in "
                    "attachments, so nothing would be removed; give each file in only one of the two lists. "
                    "Nothing was changed."
                )
        missing = [
            row["Name"] for key, row in zip(keys, current) if key not in kept and row["Name"] not in (drops or [])
        ]
        if missing:
            listed = ", ".join(repr(name) for name in dict.fromkeys(missing))
            raise CommentAttachmentsWouldBeDropped(
                f"attachments: Gorelo replaces the whole attachment list, so the current attachment(s) {listed} would "
                "be removed. Add each to attachments with its name and url as the comment read shows them (an entry "
                "with the same name but another file is a NEW file, not that attachment), or name it in "
                "remove_attachment_names to remove it on purpose. Nothing was changed.",
                log_summary=(
                    f"update_task_comment on comment {comment_id} refused: {len(missing)} current attachment(s) "
                    "would be dropped; file names withheld from the log"
                ),
            )
    if len(final) > ATTACHMENTS_MAX:
        raise ValueError(f"attachments: a comment holds at most {ATTACHMENTS_MAX} files, this would leave {len(final)}")
    return final


@gorelo_tool(
    toolset=TOOLSET,
    kind="write",
    ops=[UPDATE_TASK_COMMENT, GET_TASK_COMMENT],
    field_map=UPDATE_COMMENT_MAP,
    destructive_hint=True,
)
async def update_task_comment(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    task_id: Annotated[str, Field(description="Task id (list_project_tasks).")],
    comment_id: Annotated[
        str, Field(description="Id of a PRIVATE task comment (list_project_comments with task_id).")
    ],
    body: Annotated[
        str | None, Field(description="New comment body as HTML, not blank. It replaces the text; it cannot be cleared.")
    ] = None,
    attachments: Annotated[
        list[CommentAttachment] | None,
        Field(
            description="The COMPLETE new list (max 100). Pass only name and url: new files as upload_attachment returned "
            "them, current ones as get_project_comment shows them. One left out is refused unless named in "
            "remove_attachment_names."
        ),
    ] = None,
    remove_attachment_names: Annotated[
        list[str] | None,
        Field(
            description="Names of current files to remove. Alone, a name shared by several files is refused; naming every "
            "current file removes them all."
        ),
    ] = None,
    updated_by_name: Annotated[
        str | None,
        Field(description="Editor name to record; omitted, Gorelo records API (an API key is not a person)."),
    ] = None,
) -> dict:
    """Edit a PRIVATE comment on a task and return the stored comment. Only a private comment can be edited: a public, side conversation or approval comment has already gone outside the account and is refused (HTTP 409, ResourceUpdateBlocked). Send only what changes. The edit is recorded as the comment's UpdatedOn.
    Attachments are replaced as a whole: when attachments or remove_attachment_names is given the tool reads the comment first and refuses to drop a current file unless remove_attachment_names names it.
    Side effects: overwrites the text (and files) of the comment; nobody is notified. For a project comment use update_project_comment. If the read-back fails the result is {Id, warning}: the edit WAS applied, do not repeat it.
    """
    ids = {**_task_ids(project_id, task_id), "commentId": guid("comment_id", comment_id)}
    cid = ids["commentId"]
    if body is not None and not body.strip():
        raise ValueError("body: must not be empty or whitespace only (a comment body cannot be cleared)")
    files = _attachment_pairs(attachments)
    drops = _drop_names(remove_attachment_names)
    wire = build_body(
        {"body": body, "updated_by_name": non_empty("updated_by_name", updated_by_name)}, UPDATE_COMMENT_MAP
    )
    if body is None and files is None and drops is None:
        raise ValueError(
            "nothing to change: give at least one of body, attachments or remove_attachment_names "
            "(updated_by_name only names the editor of another change)"
        )
    client = client_of(ctx)
    expected: list[str] | None = None  # the file names the read-back must show (None: files are not touched)
    if files is not None or drops is not None:
        before = await client.get_one(GET_TASK_COMMENT, path_params=ids, tool="update_task_comment")
        current = _current_attachments(
            expect_object(before, GET_TASK_COMMENT, tool="update_task_comment"), GET_TASK_COMMENT
        )
        final = _final_attachments(cid, current, files, drops)
        if _keys(final) != _keys(current):
            wire["Attachments"] = final
            expected = sorted(row["Name"] for row in final)
        elif body is None:
            raise ValueError("nothing to change: the comment already has exactly that attachment list")
    written = await client.patch(UPDATE_TASK_COMMENT, path_params=ids, json_body=wire, tool="update_task_comment")
    expect_object(written, UPDATE_TASK_COMMENT, tool="update_task_comment")
    record = await reread_after_write(
        ctx, GET_TASK_COMMENT, path_params=ids, tool="update_task_comment", written_id=cid
    )
    if expected is not None and "warning" not in record:
        shown = record.get("Attachments")
        names = (
            sorted(row["Name"] for row in shown if isinstance(row, dict) and isinstance(row.get("Name"), str))
            if isinstance(shown, list)
            else None
        )
        if names != expected:
            raise _unconfirmed(UPDATE_TASK_COMMENT, "the comment read after the edit does not show the files that were asked for")
    return record
