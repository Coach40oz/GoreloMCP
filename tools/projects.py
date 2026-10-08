"""Projects, sections, tags, types and project or task comments (toolset "projects").

The tools in this module (13):

    list_projects, get_project, create_project, update_project
    list_project_tags, list_project_types
    list_project_sections, create_project_section, update_project_section
    list_project_comments, get_project_comment, create_project_comment, delete_project_comment

The four comment tools serve both levels: without task_id they work on the project's own comments,
with task_id on that task's comments. Tasks, task conversations and approvals live in
tools/project_tasks.py.

Not exposed on purpose: project delete and section delete (DELETE /v1/projects/{projectId} and
DELETE /v1/projects/{projectId}/sections/{sectionId}). Each one soft-deletes every task under it.

An API key without the Project scope gets a 403 (code 080203), so these tools are covered by
offline tests only. A 403 reaches the model through format_gorelo_error's missing-scope message.

Contract e15cb5a18ec2 (2026-10-02) changed projects, and this module follows it: the project DueDate is TargetDate
(create, update, ClearTargetDate and the list filters TargetDateAfter and TargetDateBefore; tasks keep their
DueDate), CreateProjectCommand lost GroupIds and now requires ClientId, GroupId, LocationId, TypeId and Title,
ClosedOn belongs to a Closed project (it was Completed), and project comments carry Reactions.

The same contract documents every request field for the first time, and the tools follow those texts:

* update_project removes a value only through clear_fields, the explicit opt-in: the three lists Gorelo says an
  empty array clears (shared_with_contact_ids, tag_ids, watcher_ids), the description (Gorelo: send an empty string)
  and the lead (Gorelo: send 0). A bare blank value, an empty list or a bare 0 is refused and points to clear_fields.
  Gorelo documents the lead removal, but it cannot be probed because the key has no Project scope.
* group_ids replaces the technician groups and cannot be emptied: its field text has no "empty array clears them"
  sentence (the three lists that do say so are the ones clear_fields offers; the operation text's general "Lists
  replace outright: send an empty array to clear one" is read with that in mind), and CreateProjectCommand requires
  a group. The tool therefore never sends an empty GroupIds, whichever way the call is made (a direct call that names
  group_ids in clear_fields is refused too, not only the schema).
* status_reason is read by Gorelo only when status_id is sent, so it is refused without status_id.
* client_id needs location_id in the same call (a location belongs to a client), and the move clears the shared
  contacts.
* closed_on is for a Closed project (status_id 5, already or in this call). On create, updated_on must not be earlier
  than created_on. Gorelo also refuses a future CreatedOn, UpdatedOn or ClosedOn and a ClosedOn before the project was
  created, but the tool leaves those to Gorelo (its error names the parameter): the first needs the local clock and
  the second needs a read of the project.
* a lead is never also a watcher, on create as well as on update.
* comments: a project comment is Public (1) or Private (2) with no conversation id; a task comment needs a
  conversation_id for a side conversation (3) or an approval (4) and refuses one for Public and Private.
* no local clock assumptions in a backdating field: every instant needs an explicit offset (a time without one is
  refused, never read as local time), no instant is compared with the local clock, and nothing is guessed for a
  missing one: updated_on is compared with created_on only when both are given, because a missing created_on is
  Gorelo's "now", not ours. The CreatedOn and UpdatedOn of a task and the CreatedOn of a comment state no rule, so
  none is checked for them.
* the comment reads repeat what their texts warn about: attachment urls are temporary links, and on a task comment a
  null BodyHtml means the stored body could not be reached (it is not an empty comment).
* the author of a change (created_by_name, updated_by_name) is recorded as API when it is omitted, and a blank one is
  refused here instead of being swallowed into API.

delete_project_comment has its own advice for a delete whose outcome is unknown (timeout, connection failure,
5xx, an unusable answer): the generic "verify with a read before retrying" proves nothing for it, because
Gorelo may still return a deleted comment with its body, and deleting an already deleted comment succeeds, so
the advice is to repeat the delete, not to read (compare post_alert in tools/alerts.py).

Ops owned by this module:

    GET /v1/projects
    GET /v1/projects/{projectId}
    POST /v1/projects
    PATCH /v1/projects/{projectId}
    GET /v1/projects/tags
    GET /v1/projects/types
    GET /v1/projects/{projectId}/sections
    POST /v1/projects/{projectId}/sections
    PATCH /v1/projects/{projectId}/sections/{sectionId}
    GET /v1/projects/{projectId}/comments
    GET /v1/projects/{projectId}/comments/{commentId}
    POST /v1/projects/{projectId}/comments
    DELETE /v1/projects/{projectId}/comments/{commentId}
    GET /v1/projects/{projectId}/tasks/{taskId}/comments
    GET /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}
    POST /v1/projects/{projectId}/tasks/{taskId}/comments
    DELETE /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}
"""

import re
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from gorelo_client import GoreloAPIError
from tools._common import (
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
LIST_PROJECTS = "GET /v1/projects"
GET_PROJECT = "GET /v1/projects/{projectId}"
CREATE_PROJECT = "POST /v1/projects"
UPDATE_PROJECT = "PATCH /v1/projects/{projectId}"
LIST_TAGS = "GET /v1/projects/tags"
LIST_TYPES = "GET /v1/projects/types"
LIST_SECTIONS = "GET /v1/projects/{projectId}/sections"
CREATE_SECTION = "POST /v1/projects/{projectId}/sections"
UPDATE_SECTION = "PATCH /v1/projects/{projectId}/sections/{sectionId}"
LIST_PROJECT_COMMENTS = "GET /v1/projects/{projectId}/comments"
GET_PROJECT_COMMENT = "GET /v1/projects/{projectId}/comments/{commentId}"
CREATE_PROJECT_COMMENT = "POST /v1/projects/{projectId}/comments"
DELETE_PROJECT_COMMENT = "DELETE /v1/projects/{projectId}/comments/{commentId}"
LIST_TASK_COMMENTS = "GET /v1/projects/{projectId}/tasks/{taskId}/comments"
GET_TASK_COMMENT = "GET /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}"
CREATE_TASK_COMMENT = "POST /v1/projects/{projectId}/tasks/{taskId}/comments"
DELETE_TASK_COMMENT = "DELETE /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}"

SortOrder = Literal["asc", "desc"]
ConversationKind = Literal["public", "private", "side_conversation", "approval"]
# What clear_fields of update_project may remove (the explicit opt-in; see the module docstring). group_ids is not
# here on purpose: GroupIds has no "empty array clears them" sentence and CreateProjectCommand requires a group.
ProjectClearable = Literal["shared_with_contact_ids", "tag_ids", "watcher_ids", "description", "lead_assignee_id"]

# Comment conversation types: 1 Public, 2 Private, 3 Side Conversation, 4 Approval.
CONVERSATION_TYPE_IDS = {"public": 1, "private": 2, "side_conversation": 3, "approval": 4}
PROJECT_COMMENT_KINDS = ("private", "public")
# Only these two have a conversation id (public and private are the task's main thread).
CONVERSATION_ID_KINDS = ("side_conversation", "approval")

# Field maps: {snake_case param: "PascalField"}. They build the request and turn a Gorelo PropertyName
# back into the param to fix, so the list maps hold the query names too (PageSize and Cursor included).
LIST_PROJECTS_MAP = {
    "status_ids": "StatusIds",
    "client_ids": "ClientIds",
    "type_ids": "TypeIds",
    "lead_assignee_ids": "LeadAssigneeIds",
    "tag_ids": "TagIds",
    "group_ids": "GroupIds",
    "query": "Query",
    "updated_since": "UpdatedSince",
    "updated_before": "UpdatedBefore",
    "created_since": "CreatedSince",
    "created_before": "CreatedBefore",
    "target_date_after": "TargetDateAfter",
    "target_date_before": "TargetDateBefore",
    "sort_by": "SortBy",
    "sort_order": "SortOrder",
    "page_size": "PageSize",
    "cursor": "Cursor",
}
# CreateProjectCommand: GroupIds is gone since 2026-10-02, so a project has the one GroupId.
CREATE_PROJECT_MAP = {
    "title": "Title",
    "description": "Description",
    "client_id": "ClientId",
    "location_id": "LocationId",
    "lead_assignee_id": "LeadAssigneeId",
    "type_id": "TypeId",
    "tag_ids": "TagIds",
    "group_id": "GroupId",
    "watcher_ids": "WatcherIds",
    "shared_with_contact_ids": "SharedWithContactIds",
    "target_date": "TargetDate",
    "created_by_name": "CreatedByName",
    "created_on": "CreatedOn",
    "updated_on": "UpdatedOn",
}
UPDATE_PROJECT_MAP = {
    "title": "Title",
    "description": "Description",
    "client_id": "ClientId",
    "location_id": "LocationId",
    "shared_with_contact_ids": "SharedWithContactIds",
    "type_id": "TypeId",
    "tag_ids": "TagIds",
    "lead_assignee_id": "LeadAssigneeId",
    "watcher_ids": "WatcherIds",
    "group_ids": "GroupIds",
    "target_date": "TargetDate",
    "clear_target_date": "ClearTargetDate",
    "status_id": "StatusId",
    "status_reason": "StatusReason",
    "closed_on": "ClosedOn",
    "updated_by_name": "UpdatedByName",
}
# What clear_fields may remove, and what Gorelo expects on the wire for each (UpdateProjectCommand and the text of
# PATCH /v1/projects/{projectId}): [] for the three lists whose field text says "Empty array clears them", "" for the
# description ("Send an empty string to remove it") and 0 for the lead ("Send 0 to remove the lead").
PROJECT_LIST_PARAMS = ("shared_with_contact_ids", "tag_ids", "watcher_ids")
PROJECT_CLEAR_VALUES: dict[str, Any] = {
    **{param: [] for param in PROJECT_LIST_PARAMS},
    "description": "",
    "lead_assignee_id": 0,
}
CREATE_SECTION_MAP = {"title": "Title", "color": "Color", "created_by_name": "CreatedByName"}
UPDATE_SECTION_MAP = {"title": "Title", "color": "Color", "updated_by_name": "UpdatedByName"}
LIST_COMMENTS_MAP = {
    "conversation_types": "ConversationType",
    "conversation_id": "ConversationId",
    "sort_order": "SortOrder",
    "page_size": "PageSize",
    "cursor": "Cursor",
}
CREATE_COMMENT_MAP = {
    "body": "Body",
    "body_text": "BodyText",
    "conversation_type": "ConversationTypeId",
    "conversation_id": "ConversationId",
    "attachments": "Attachments",
    "created_by_name": "CreatedByName",
    "created_on": "CreatedOn",
}

PROJECT_STATUSES = "1 NotStarted, 2 InProgress, 3 OnHold, 4 Completed, 5 Closed."
PROJECT_CLOSED = 5  # the one status a ClosedOn date belongs to

SECTION_NOTE = (
    "Gorelo answers with the section Id only and has no single-section read. "
    "Read the section (Title, Color, TaskCount) with list_project_sections."
)

_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")


# --------------------------------------------------------------------------
# Private helpers (shared helpers live in tools/_common.py only)
# --------------------------------------------------------------------------


def _given(check: Callable[[str, Any], Any], param: str, value: Any) -> Any:
    """check(param, value) for a value the caller gave; None (not given) stays None."""
    return None if value is None else check(param, value)


def _color(param: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _COLOR.fullmatch(value):
        raise ValueError(
            f"{param}: expected a hex colour such as #56A0F9 ('#' followed by 6 hex digits), got {describe_value(value)}"
        )
    return value


def _clear_list(clear_fields: list[str] | None) -> list[str]:
    """The distinct names in clear_fields, in order, each one a field update_project can remove.

    [] is an error: omit clear_fields instead. The schema (ProjectClearable) already limits the names, but a direct
    call skips it and group_ids is a real field of the body, so a name outside PROJECT_CLEAR_VALUES is refused here
    too: sending GroupIds as "" or [] would be wrong, because the tool never empties the groups (the GroupIds text has
    no "empty array clears them" sentence)."""
    if clear_fields is None:
        return []
    non_empty("clear_fields", clear_fields)
    for index, name in enumerate(clear_fields):
        if not isinstance(name, str):
            raise ValueError(
                f"clear_fields[{index}]: expected a field name such as \"tag_ids\", got {describe_value(name)}"
            )
    names = list(dict.fromkeys(clear_fields))
    for name in names:
        if name == "group_ids":
            raise ValueError(
                "clear_fields: group_ids cannot be cleared, because the groups cannot be emptied (a new project "
                "always gets one); send the complete new list in group_ids instead"
            )
        if name not in PROJECT_CLEAR_VALUES:
            raise ValueError(f"clear_fields: {name!r} cannot be cleared; allowed: {', '.join(PROJECT_CLEAR_VALUES)}")
    return names


def _refuse_empty_lists(lists: dict[str, Any]) -> None:
    """Lists replace the stored ones, so [] is never a value; emptying one takes an explicit clear_fields."""
    for param, value in lists.items():
        if isinstance(value, list) and not value:
            raise ValueError(
                f"{param}: an empty list is not accepted because lists replace the stored one; "
                f"to remove every entry pass clear_fields=[\"{param}\"]"
            )


def _refuse_empty_groups(group_ids: Any) -> None:
    """group_ids replaces the technician groups and is never emptied.

    GroupIds has no "empty array clears them" sentence (unlike TagIds, WatcherIds and SharedWithContactIds) and
    CreateProjectCommand requires a GroupId, so unlike the other lists there is no clear_fields entry for it. (The
    response schema says PrimaryGroupId is null "when the project has no group", so a project without one may exist;
    nothing in the request texts lets a caller create that state, and this tool does not.)"""
    if isinstance(group_ids, list) and not group_ids:
        raise ValueError(
            "group_ids: an empty list is not accepted, because the groups cannot be emptied (a new project always "
            "gets one); send the complete new list (at least one group id) or omit group_ids"
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


def _lead_not_a_watcher(lead: int | None, watchers: list[int] | None) -> None:
    """One technician cannot be both the lead and a watcher (CreateProjectCommand and the update text say so)."""
    if lead is not None and lead in (watchers or []):
        raise ValueError(
            f"watcher_ids: technician {lead} is also the lead_assignee_id, and a technician cannot be both "
            "the lead and a watcher; drop it from watcher_ids or choose another lead"
        )


def _instant(param: str, value: str | None) -> tuple[str | None, datetime | None]:
    """(the UTC text for the request, the same moment as a datetime). A datetime without an offset is an error.

    The moment exists only to compare two times the caller gave with each other (the offsets decide, not the digits).
    Nothing in this module reads the local clock: Gorelo says a backdated CreatedOn, UpdatedOn or ClosedOn "must not
    be in the future", and it is Gorelo that enforces that, with an error that names the parameter."""
    text = utc_iso(param, value)
    return (None, None) if text is None else (text, datetime.fromisoformat(text))


def _query(filters: dict[str, Any], field_map: dict[str, str]) -> dict[str, Any]:
    """The query of a list call: lists become comma separated, None drops out, PascalCase names."""
    values = {
        param: csv_ids(param, value) if isinstance(value, list) else value
        for param, value in filters.items()
        if param in field_map
    }
    return build_body(values, field_map)


# The schema FastMCP shows the model is built from this class, so it carries no docstring and no field
# descriptions (they would be sent with every tool list); the attachments parameter says what name and url are.
class CommentAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: str


def _attachments(items: list[CommentAttachment] | None) -> list[dict[str, str]] | None:
    """Gorelo's Attachments: [{"Name", "Url"}]. None stays None; an empty list or a blank part is an error."""
    if items is None:
        return None
    non_empty("attachments", items)
    return [
        {
            "Name": non_empty(f"attachments[{index}].name", item.name),
            "Url": non_empty(f"attachments[{index}].url", item.url),
        }
        for index, item in enumerate(items)
    ]


def _conversation_types(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    if not values:
        raise ValueError(
            "conversation_types: the list must contain at least one type "
            "(omit conversation_types to include every type)"
        )
    return list(dict.fromkeys(values))


def _comment_ids(project_id: str, comment_id: str, task_id: str | None) -> dict[str, str]:
    """Validated path ids of a comment call: the task level has a taskId, the project level has none."""
    ids = {"projectId": guid("project_id", project_id)}
    if task_id is not None:
        ids["taskId"] = guid("task_id", task_id)
    ids["commentId"] = guid("comment_id", comment_id)
    return ids


def _how_it_failed(err: GoreloAPIError) -> str:
    """One phrase for what happened to a delete whose outcome is unknown (never quotes the request)."""
    if err.kind == "timeout":
        return "the request timed out"
    if err.kind == "transport":
        return "the connection failed"
    if err.status is not None and err.status >= 500:
        notes = [str(note["message"]) for note in err.notifications[:3] if note.get("message")]
        return f"Gorelo answered HTTP {err.status}" + (f": {'; '.join(notes)}" if notes else "")
    return "its answer could not be used"


def _delete_not_confirmed(how: str, trace_id: str | None) -> ToolError:
    """The delete-specific error for a delete that may or may not have been applied.

    The generic text says "verify with a read before retrying", which proves nothing here: Gorelo may still
    return a deleted comment with its body. Deleting an already deleted comment succeeds, so repeating is safe.
    """
    trace = f" [trace {trace_id}]" if trace_id else ""
    return ToolError(
        f"Gorelo did not confirm delete_project_comment ({how}). The comment may already be deleted. Repeating the "
        "delete is safe (it is idempotent: deleting an already deleted comment succeeds). Do not try to verify it "
        f"with a read: Gorelo may still return deleted comments.{trace}"
    )


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------


@gorelo_tool(toolset=TOOLSET, kind="read", ops=[LIST_PROJECTS], field_map=LIST_PROJECTS_MAP)
async def list_projects(
    ctx: Context,
    status_ids: Annotated[list[StrictId] | None, Field(description=PROJECT_STATUSES)] = None,
    client_ids: Annotated[list[StrictId] | None, Field(description="Client ids (list_clients).")] = None,
    type_ids: Annotated[list[str] | None, Field(description="Type ids (list_project_types).")] = None,
    lead_assignee_ids: Annotated[
        list[StrictId] | None, Field(description="Lead technician ids (list_org_users).")
    ] = None,
    tag_ids: Annotated[list[str] | None, Field(description="Tag ids (list_project_tags).")] = None,
    group_ids: Annotated[list[StrictId] | None, Field(description="Group ids (list_org_groups).")] = None,
    query: Annotated[str | None, Field(description="Keyword in title, number or display number.")] = None,
    updated_since: Annotated[str | None, Field(description="Updated at or after this instant.")] = None,
    updated_before: Annotated[str | None, Field(description="Updated strictly before this instant.")] = None,
    created_since: Annotated[str | None, Field(description="Created at or after this instant.")] = None,
    created_before: Annotated[str | None, Field(description="Created strictly before this instant.")] = None,
    target_date_after: Annotated[
        str | None,
        Field(
            description="Target date at or after this instant. Projects with no target date match neither "
            "target date filter."
        ),
    ] = None,
    target_date_before: Annotated[
        str | None, Field(description="Target date strictly before this instant.")
    ] = None,
    sort_by: Annotated[Literal["updatedOn", "createdOn"] | None, Field(description="Default updatedOn.")] = None,
    sort_order: Annotated[SortOrder | None, Field(description="Default desc (newest first).")] = None,
    page_size: Annotated[int, Field(description="Rows per page, 1-200 (clamped).")] = 50,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous page.")] = None,
) -> dict:
    """List projects, newest activity first by default, and return one page. Rows carry TargetDate (the date the project is targeted to finish); get_project adds Description, SharedWithContactIds and WatcherIds.
    Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.
    """
    filters = {
        "status_ids": positive_ids("status_ids", status_ids),
        "client_ids": positive_ids("client_ids", client_ids),
        "type_ids": guids("type_ids", type_ids),
        "lead_assignee_ids": positive_ids("lead_assignee_ids", lead_assignee_ids),
        "tag_ids": guids("tag_ids", tag_ids),
        "group_ids": positive_ids("group_ids", group_ids),
        "query": non_empty("query", query),
        "updated_since": utc_iso("updated_since", updated_since),
        "updated_before": utc_iso("updated_before", updated_before),
        "created_since": utc_iso("created_since", created_since),
        "created_before": utc_iso("created_before", created_before),
        "target_date_after": utc_iso("target_date_after", target_date_after),
        "target_date_before": utc_iso("target_date_before", target_date_before),
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    token = non_empty("cursor", cursor)
    page = await client_of(ctx).get_page(
        LIST_PROJECTS,
        query=_query(filters, LIST_PROJECTS_MAP),
        page_size=clamp_page_size(page_size),
        cursor=token,
        tool="list_projects",
    )
    return paged_result(page, filters)


@gorelo_tool(toolset=TOOLSET, kind="read", ops=[GET_PROJECT])
async def get_project(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
) -> dict:
    """Get one project's full record, including Description, SharedWithContactIds and WatcherIds. Sections and tasks are separate: list_project_sections, list_project_tasks."""
    data = await client_of(ctx).get_one(
        GET_PROJECT, path_params={"projectId": guid("project_id", project_id)}, tool="get_project"
    )
    return expect_object(data, GET_PROJECT, tool="get_project")


@gorelo_tool(toolset=TOOLSET, kind="write", ops=[CREATE_PROJECT, GET_PROJECT], field_map=CREATE_PROJECT_MAP)
async def create_project(
    ctx: Context,
    title: Annotated[str, Field(description="Project title.")],
    client_id: Annotated[StrictId, Field(description="Client id (list_clients).")],
    location_id: Annotated[StrictId, Field(description="Location id of that client (list_client_locations).")],
    type_id: Annotated[str, Field(description="Project type id (list_project_types).")],
    group_id: Annotated[
        StrictId, Field(description="Technician group id the project is assigned to (list_org_groups).")
    ],
    description: Annotated[str | None, Field(description="Project description.")] = None,
    lead_assignee_id: Annotated[
        StrictId | None, Field(description="Lead technician id (list_org_users). Omit for a project with no lead.")
    ] = None,
    tag_ids: Annotated[list[str] | None, Field(description="Tag ids (list_project_tags).")] = None,
    watcher_ids: Annotated[
        list[StrictId] | None,
        Field(description="Watcher technician ids (list_org_users). The lead is never also a watcher."),
    ] = None,
    shared_with_contact_ids: Annotated[
        list[StrictId] | None,
        Field(description="Contact ids to share the project with (list_contacts); they must belong to client_id."),
    ] = None,
    target_date: Annotated[
        str | None, Field(description="Date the project is targeted to finish (ISO 8601 with UTC offset).")
    ] = None,
    created_by_name: Annotated[
        str | None,
        Field(description="Creator name to record; omitted, Gorelo records API (an API key is not a person). Imports only."),
    ] = None,
    created_on: Annotated[
        str | None,
        Field(
            description="Backdated creation time (imports only): ISO 8601 with UTC offset, never in the future "
            "(Gorelo refuses it). Omit for now."
        ),
    ] = None,
    updated_on: Annotated[
        str | None,
        Field(
            description="Backdated last activity (imports only): never in the future (Gorelo refuses it), not "
            "earlier than created_on. Omitted, it equals the creation time."
        ),
    ] = None,
) -> dict:
    """Create a project and return its record. Required: title, client_id, location_id, type_id and group_id (a project belongs to one client, one of its locations, one project type and one technician group); everything else is optional.
    Side effects: creates a real project as NotStarted, with the app's numbering and the project-created notification the app sends (Gorelo does not say who receives it). Projects cannot be deleted through this server (a delete removes every task in it). If the result is {Id, warning} the project exists: do not create it again.
    """
    non_empty("title", title)
    created_text, created_at = _instant("created_on", created_on)
    updated_text, updated_at = _instant("updated_on", updated_on)
    if created_at is not None and updated_at is not None and updated_at < created_at:
        raise ValueError(f"updated_on: {updated_text} is earlier than created_on ({created_text}); it must not be")
    values = {
        "title": title,
        "description": description,
        "client_id": positive_id("client_id", client_id),
        "location_id": positive_id("location_id", location_id),
        "lead_assignee_id": _given(positive_id, "lead_assignee_id", lead_assignee_id),
        "type_id": guid("type_id", type_id),
        "tag_ids": guids("tag_ids", tag_ids),
        "group_id": positive_id("group_id", group_id),
        "watcher_ids": positive_ids("watcher_ids", watcher_ids),
        "shared_with_contact_ids": positive_ids("shared_with_contact_ids", shared_with_contact_ids),
        "target_date": utc_iso("target_date", target_date),
        "created_by_name": created_by_name,
        "created_on": created_text,
        "updated_on": updated_text,
    }
    _lead_not_a_watcher(values["lead_assignee_id"], values["watcher_ids"])
    body = build_body(values, CREATE_PROJECT_MAP)
    written = await client_of(ctx).post(CREATE_PROJECT, json_body=body, tool="create_project")
    new_id = created_id(written, CREATE_PROJECT, tool="create_project")
    return await reread_after_write(
        ctx, GET_PROJECT, path_params={"projectId": new_id}, tool="create_project", written_id=new_id
    )


@gorelo_tool(
    toolset=TOOLSET, kind="write", ops=[UPDATE_PROJECT, GET_PROJECT], field_map=UPDATE_PROJECT_MAP,
    destructive_hint=True,
)
async def update_project(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    title: Annotated[str | None, Field(description="New title; not blank.")] = None,
    description: Annotated[
        str | None, Field(description="New description; not blank. To remove the description use clear_fields.")
    ] = None,
    client_id: Annotated[
        StrictId | None,
        Field(description="Move the project to this client (list_clients); the move clears its shared contacts. Needs location_id."),
    ] = None,
    location_id: Annotated[
        StrictId | None,
        Field(description="Location of the client (list_client_locations); required whenever client_id is given."),
    ] = None,
    shared_with_contact_ids: Annotated[
        list[StrictId] | None, Field(description="Contact ids (list_contacts).")
    ] = None,
    type_id: Annotated[str | None, Field(description="Type id (list_project_types).")] = None,
    tag_ids: Annotated[list[str] | None, Field(description="Tag ids (list_project_tags).")] = None,
    lead_assignee_id: Annotated[
        StrictId | None,
        Field(
            description="Lead technician id (list_org_users); cannot also be a watcher. "
            "To remove the lead use clear_fields, never 0."
        ),
    ] = None,
    watcher_ids: Annotated[
        list[StrictId] | None,
        Field(description="Watcher technician ids (list_org_users). The lead is never also a watcher."),
    ] = None,
    group_ids: Annotated[
        list[StrictId] | None,
        Field(
            description="Group ids (list_org_groups): the complete new list of technician groups, at least one id. "
            "The groups cannot be emptied."
        ),
    ] = None,
    target_date: Annotated[
        str | None,
        Field(description="New target date (ISO 8601 with UTC offset). To remove it use clear_target_date."),
    ] = None,
    clear_target_date: Annotated[
        bool | None, Field(description="true removes the target date; not together with target_date.")
    ] = None,
    status_id: Annotated[
        StrictId | None,
        Field(
            description=f"{PROJECT_STATUSES} Completed is normally reached automatically (every task Done); "
            "Closed is the manual end state."
        ),
    ] = None,
    status_reason: Annotated[
        str | None,
        Field(description="Reason recorded with the status. Gorelo reads it only when status_id is sent, so give both."),
    ] = None,
    closed_on: Annotated[
        str | None,
        Field(
            description="Backdated close date: only for a Closed project (status_id 5, already or in this call); "
            "Gorelo refuses a future date or one before the project was created. It also becomes the last update."
        ),
    ] = None,
    updated_by_name: Annotated[
        str | None,
        Field(
            description="Author name to record; omitted, Gorelo records API (an API key is not a person). "
            "Not a change by itself."
        ),
    ] = None,
    clear_fields: Annotated[
        list[ProjectClearable] | None,
        Field(
            description="Fields to remove: description, lead_assignee_id (removes the lead) or the lists "
            "shared_with_contact_ids, tag_ids, watcher_ids. Not together with a value for the same field."
        ),
    ] = None,
) -> dict:
    """Change fields of one project and return its updated record. Send only what changes; at least one change is required. Lists (shared_with_contact_ids, tag_ids, watcher_ids, group_ids) REPLACE the stored list, so send the complete new list; empty one only through clear_fields (group_ids cannot be emptied: a new project always gets a group). The description and the lead are removed only through clear_fields too.
    Side effects: overwrites the fields given. Changing client_id clears the shared contacts and needs location_id. status_reason is recorded only together with status_id. Moving a project to Closed stamps ClosedOn, and moving a Closed project to any other status clears it. Projects cannot be deleted through this server (a delete removes every task in it). If the result is {Id, warning} the change was applied: do not repeat it.
    """
    pid = guid("project_id", project_id)
    clear = _clear_list(clear_fields)
    _refuse_empty_lists(
        {"shared_with_contact_ids": shared_with_contact_ids, "tag_ids": tag_ids, "watcher_ids": watcher_ids}
    )
    _refuse_empty_groups(group_ids)
    closed_text = utc_iso("closed_on", closed_on)
    values = {
        "title": title,
        "description": _clearable_text("description", description),
        "client_id": _given(positive_id, "client_id", client_id),
        "location_id": _given(positive_id, "location_id", location_id),
        "shared_with_contact_ids": positive_ids("shared_with_contact_ids", shared_with_contact_ids),
        "type_id": _given(guid, "type_id", type_id),
        "tag_ids": guids("tag_ids", tag_ids),
        "lead_assignee_id": _changed_lead(lead_assignee_id),
        "watcher_ids": positive_ids("watcher_ids", watcher_ids),
        "group_ids": positive_ids("group_ids", group_ids),
        "target_date": utc_iso("target_date", target_date),
        "clear_target_date": True if clear_target_date is True else None,
        "status_id": _given(positive_id, "status_id", status_id),
        "status_reason": non_empty("status_reason", status_reason),
        "closed_on": closed_text,
        "updated_by_name": updated_by_name,
    }
    if values["status_reason"] is not None and values["status_id"] is None:
        raise ValueError("status_reason: only recorded together with status_id; pass the status as well")
    if values["client_id"] is not None and values["location_id"] is None:
        raise ValueError(
            "location_id: required together with client_id, because a location belongs to a client; moving a "
            "project to another client needs a location of that client (list_client_locations), so give both "
            "client_id and location_id (the move also clears the shared contacts)"
        )
    if values["closed_on"] is not None and values["status_id"] not in (None, PROJECT_CLOSED):
        raise ValueError(
            f"closed_on: only accepted for a Closed project, but status_id is {values['status_id']} and the project "
            f"would not be Closed; give status_id {PROJECT_CLOSED} (Closed) with it, or leave status_id out when the "
            "project is already Closed"
        )
    if values["clear_target_date"] and values["target_date"] is not None:
        raise ValueError("target_date: cannot be set while clear_target_date is true; give only one of them")
    _lead_not_a_watcher(values["lead_assignee_id"], values["watcher_ids"])
    body = build_body(values, UPDATE_PROJECT_MAP, clear=clear, clear_values=PROJECT_CLEAR_VALUES)
    if not set(body) - {"UpdatedByName"}:
        changeable = ", ".join(param for param in UPDATE_PROJECT_MAP if param != "updated_by_name")
        raise ValueError(
            f"nothing to change: give at least one of {changeable} or clear_fields "
            "(updated_by_name alone changes nothing)"
        )
    written = await client_of(ctx).patch(
        UPDATE_PROJECT, path_params={"projectId": pid}, json_body=body, tool="update_project"
    )
    expect_object(written, UPDATE_PROJECT, tool="update_project")
    return await reread_after_write(
        ctx, GET_PROJECT, path_params={"projectId": pid}, tool="update_project", written_id=pid
    )


# --------------------------------------------------------------------------
# Project tags and types
# --------------------------------------------------------------------------


@gorelo_tool(toolset=TOOLSET, kind="read", ops=[LIST_TAGS])
async def list_project_tags(ctx: Context) -> dict:
    """List every project tag (Id, Name, Description). Project tags are separate from ticket tags. Not paged."""
    items = await client_of(ctx).get_list(LIST_TAGS, tool="list_project_tags")
    return list_result(items)


@gorelo_tool(toolset=TOOLSET, kind="read", ops=[LIST_TYPES])
async def list_project_types(ctx: Context) -> dict:
    """List every project type (Id, Name, Description). Not paged."""
    items = await client_of(ctx).get_list(LIST_TYPES, tool="list_project_types")
    return list_result(items)


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


@gorelo_tool(toolset=TOOLSET, kind="read", ops=[LIST_SECTIONS])
async def list_project_sections(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
) -> dict:
    """List a project's sections (board columns) with Id, Title, Color and TaskCount. Not paged."""
    items = await client_of(ctx).get_list(
        LIST_SECTIONS, path_params={"projectId": guid("project_id", project_id)}, tool="list_project_sections"
    )
    return list_result(items)


@gorelo_tool(toolset=TOOLSET, kind="write", ops=[CREATE_SECTION], field_map=CREATE_SECTION_MAP)
async def create_project_section(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    title: Annotated[str, Field(description="Section title.")],
    color: Annotated[
        str | None,
        Field(description="#RRGGBB hex, e.g. #56A0F9. Omit it and the section gets #56A0F9; a blank value is refused."),
    ] = None,
    created_by_name: Annotated[
        str | None, Field(description="Author name to record; omitted, Gorelo records API. Omit normally.")
    ] = None,
) -> dict:
    """Add a section (board column) to a project and return its Id. Gorelo has no single-section read: confirm with list_project_sections.
    Side effects: adds a section to the project's board; nothing is emailed. Sections cannot be deleted through this server (a delete removes every task in it).
    """
    pid = guid("project_id", project_id)
    non_empty("title", title)
    body = build_body(
        {"title": title, "color": _color("color", color), "created_by_name": created_by_name}, CREATE_SECTION_MAP
    )
    written = await client_of(ctx).post(
        CREATE_SECTION, path_params={"projectId": pid}, json_body=body, tool="create_project_section"
    )
    created_id(written, CREATE_SECTION, tool="create_project_section")
    return {**written, "note": SECTION_NOTE}


@gorelo_tool(
    toolset=TOOLSET, kind="write", ops=[UPDATE_SECTION], field_map=UPDATE_SECTION_MAP, destructive_hint=True
)
async def update_project_section(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    section_id: Annotated[str, Field(description="Section id (list_project_sections).")],
    title: Annotated[str | None, Field(description="New title; not blank.")] = None,
    color: Annotated[str | None, Field(description="New #RRGGBB hex, e.g. #56A0F9; not blank.")] = None,
    updated_by_name: Annotated[
        str | None,
        Field(
            description="Author name to record; omitted, Gorelo records API (an API key is not a person). "
            "Not a change by itself."
        ),
    ] = None,
) -> dict:
    """Rename or recolour one section and return its Id. Send only what changes (neither field can be blanked); at least one of title or color is required. Gorelo has no single-section read: confirm with list_project_sections.
    Side effects: overwrites the section's title and/or color. Sections cannot be deleted through this server (a delete removes every task in it).
    """
    pid = guid("project_id", project_id)
    sid = guid("section_id", section_id)
    if title is not None and not title.strip():
        raise ValueError("title: must not be blank (a section always has a title); omit it to keep the current one")
    values = {"title": title, "color": _color("color", color), "updated_by_name": updated_by_name}
    body = build_body(values, UPDATE_SECTION_MAP)
    if "Title" not in body and "Color" not in body:
        raise ValueError("nothing to change: give title and/or color (updated_by_name alone changes nothing)")
    written = await client_of(ctx).patch(
        UPDATE_SECTION,
        path_params={"projectId": pid, "sectionId": sid},
        json_body=body,
        tool="update_project_section",
    )
    result = expect_object(written, UPDATE_SECTION, tool="update_project_section")
    return {**result, "note": SECTION_NOTE}


# --------------------------------------------------------------------------
# Comments (project level, or task level when task_id is given)
# --------------------------------------------------------------------------


@gorelo_tool(
    toolset=TOOLSET, kind="read", ops=[LIST_PROJECT_COMMENTS, LIST_TASK_COMMENTS], field_map=LIST_COMMENTS_MAP
)
async def list_project_comments(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    task_id: Annotated[
        str | None, Field(description="Task id (list_project_tasks): list that task's comments instead.")
    ] = None,
    conversation_types: Annotated[
        list[ConversationKind] | None, Field(description="Task comments only; omit for every kind.")
    ] = None,
    conversation_id: Annotated[
        str | None,
        Field(
            description="Task comments only: one side conversation or approval (list_task_conversations); "
            "needs conversation_types with exactly that one type."
        ),
    ] = None,
    sort_order: Annotated[SortOrder | None, Field(description="By creation time. Default desc.")] = None,
    page_size: Annotated[int, Field(description="Comments per page, 1-200 (clamped).")] = 50,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous page.")] = None,
) -> dict:
    """List a project's comments, newest first, or one task's comments when task_id is given. Returns one page. Rows carry Reactions (the ReactionId and UserId of each reaction). Task rows can have BodyTruncated true: read that comment with get_project_comment for the full body. Attachment urls are temporary links: use them now, or read the comment again for a fresh one; never store them. Gorelo may still return deleted comments with their body.
    Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.
    """
    pid = guid("project_id", project_id)
    tid = _given(guid, "task_id", task_id)
    kinds = _conversation_types(conversation_types)
    conv_id = non_empty("conversation_id", conversation_id)
    if tid is None:
        if kinds is not None:
            raise ValueError(
                "conversation_types: only task comments have conversation types; "
                "give task_id as well, or leave conversation_types out"
            )
        if conv_id is not None:
            raise ValueError(
                "conversation_id: only task comments belong to a conversation; "
                "give task_id as well, or leave conversation_id out"
            )
    elif conv_id is not None and (kinds is None or len(kinds) != 1 or kinds[0] not in CONVERSATION_ID_KINDS):
        got = "conversation_types was not given" if kinds is None else f"conversation_types is {', '.join(kinds)}"
        raise ValueError(
            "conversation_id: needs conversation_types with exactly one type, side_conversation or approval "
            f"(public and private comments have no conversation id); {got}"
        )
    query = build_body(
        {
            "conversation_types": ",".join(str(CONVERSATION_TYPE_IDS[kind]) for kind in kinds) if kinds else None,
            "conversation_id": conv_id,
            "sort_order": sort_order,
        },
        LIST_COMMENTS_MAP,
    )
    path_params = {"projectId": pid} if tid is None else {"projectId": pid, "taskId": tid}
    token = non_empty("cursor", cursor)
    page = await client_of(ctx).get_page(
        LIST_PROJECT_COMMENTS if tid is None else LIST_TASK_COMMENTS,
        path_params=path_params,
        query=query,
        page_size=clamp_page_size(page_size),
        cursor=token,
        tool="list_project_comments",
    )
    filters = {
        "project_id": pid,
        "task_id": tid,
        "conversation_types": kinds,
        "conversation_id": conv_id,
        "sort_order": sort_order,
    }
    return paged_result(page, filters)


@gorelo_tool(toolset=TOOLSET, kind="read", ops=[GET_PROJECT_COMMENT, GET_TASK_COMMENT])
async def get_project_comment(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    comment_id: Annotated[str, Field(description="Comment id (list_project_comments).")],
    task_id: Annotated[
        str | None, Field(description="Task id (list_project_tasks); for a task comment.")
    ] = None,
) -> dict:
    """Get one comment of a project, or of one task when task_id is given, with its full body. It carries Reactions (the ReactionId and UserId of each reaction). On a task comment a null BodyHtml means the stored body could not be reached, not that the comment is empty. Attachment urls are temporary links: use them now, or read the comment again for a fresh one; never store them. A deleted comment may still be returned with its body."""
    ids = _comment_ids(project_id, comment_id, task_id)
    op = GET_PROJECT_COMMENT if task_id is None else GET_TASK_COMMENT
    data = await client_of(ctx).get_one(op, path_params=ids, tool="get_project_comment")
    return expect_object(data, op, tool="get_project_comment")


@gorelo_tool(
    toolset=TOOLSET,
    kind="write",
    ops=[CREATE_PROJECT_COMMENT, CREATE_TASK_COMMENT, GET_PROJECT_COMMENT, GET_TASK_COMMENT],
    field_map=CREATE_COMMENT_MAP,
)
async def create_project_comment(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    body: Annotated[str, Field(description="Comment body as HTML.")],
    task_id: Annotated[
        str | None, Field(description="Task id (list_project_tasks): comment on that task instead.")
    ] = None,
    body_text: Annotated[str | None, Field(description="The same body as markdown, if you keep one.")] = None,
    conversation_type: Annotated[
        ConversationKind,
        Field(
            description="private (default, internal) or public. Project comments allow only those; task comments "
            "also allow side_conversation and approval, which need conversation_id."
        ),
    ] = "private",
    conversation_id: Annotated[
        str | None,
        Field(
            description="Task comments only: the side conversation or approval to post into "
            "(list_task_conversations). Required for those two types, rejected for private and public."
        ),
    ] = None,
    attachments: Annotated[
        list[CommentAttachment] | None,
        Field(description="Files to attach. Pass only the name and url from upload_attachment, unchanged."),
    ] = None,
    created_by_name: Annotated[
        str | None,
        Field(description="Author name to record; omitted, Gorelo records API (an API key is not a person). Imports only."),
    ] = None,
    created_on: Annotated[
        str | None,
        Field(
            description="When the comment was written (ISO 8601 with UTC offset); omitted, now. "
            "Supply it only when importing a comment that existed elsewhere."
        ),
    ] = None,
) -> dict:
    """Post a comment on a project, or on one task when task_id is given, and return the created comment. The default is private: internal, emails nobody.
    Side effects: anything but private on a task puts the task into a waiting-on-contact state, fires automation and emails recipients: a side_conversation comment emails that conversation's recipients, an approval comment the approvers. Tell the user who will be emailed before posting anything that is not private. A public comment is outward-facing and Gorelo does not document its recipients for projects or tasks, so confirm with the user first. If the result is {Id, warning} the comment exists: do not post it again.
    """
    pid = guid("project_id", project_id)
    tid = _given(guid, "task_id", task_id)
    non_empty("body", body)
    conv_id = non_empty("conversation_id", conversation_id)
    if tid is None:
        if conversation_type not in PROJECT_COMMENT_KINDS:
            raise ValueError(
                f"conversation_type: a project comment can only be private or public, not {conversation_type}; "
                "side_conversation and approval exist only on tasks, so give task_id"
            )
        if conv_id is not None:
            raise ValueError(
                "conversation_id: project comments have no side conversations or approvals; "
                "conversation_id is only for task comments of type side_conversation or approval"
            )
    elif conversation_type in CONVERSATION_ID_KINDS:
        if conv_id is None:
            raise ValueError(
                f"conversation_id: required when conversation_type is {conversation_type} "
                "(Id from list_task_conversations)"
            )
    elif conv_id is not None:
        raise ValueError(
            f"conversation_id: not accepted for a {conversation_type} comment; "
            "it only applies to side_conversation and approval"
        )
    payload = build_body(
        {
            "body": body,
            "body_text": body_text,
            "conversation_type": CONVERSATION_TYPE_IDS[conversation_type],
            "conversation_id": conv_id,
            "attachments": _attachments(attachments),
            "created_by_name": created_by_name,
            "created_on": utc_iso("created_on", created_on),
        },
        CREATE_COMMENT_MAP,
    )
    post_op = CREATE_PROJECT_COMMENT if tid is None else CREATE_TASK_COMMENT
    read_op = GET_PROJECT_COMMENT if tid is None else GET_TASK_COMMENT
    path_params = {"projectId": pid} if tid is None else {"projectId": pid, "taskId": tid}
    written = await client_of(ctx).post(post_op, path_params=path_params, json_body=payload, tool="create_project_comment")
    comment_id = created_id(written, post_op, tool="create_project_comment")
    return await reread_after_write(
        ctx,
        read_op,
        path_params={**path_params, "commentId": comment_id},
        tool="create_project_comment",
        written_id=comment_id,
    )


@gorelo_tool(toolset=TOOLSET, kind="destructive", ops=[DELETE_PROJECT_COMMENT, DELETE_TASK_COMMENT])
async def delete_project_comment(
    ctx: Context,
    project_id: Annotated[str, Field(description="Project id (list_projects).")],
    comment_id: Annotated[str, Field(description="Comment id (list_project_comments).")],
    task_id: Annotated[
        str | None, Field(description="Task id (list_project_tasks); for a task comment.")
    ] = None,
    confirm: Annotated[StrictBool, Field(description="Must be true to delete. Ask the user first.")] = False,
) -> dict:
    """Delete one PRIVATE comment of a project, or of one task when task_id is given. This is a soft delete: the comment stops appearing but can be recovered in the app. Gorelo refuses a public, side conversation or approval comment (HTTP 409, ResourceNotDeletable); deleting one that is already deleted succeeds, so repeating a delete is safe.
    Side effects: nothing is emailed. Do not re-read to verify: Gorelo may still return deleted comments. Ask the user first; needs confirm=true.
    """
    ids = _comment_ids(project_id, comment_id, task_id)
    level = "project" if task_id is None else "task"
    require_confirm(
        confirm,
        action=f"delete {level} comment {ids['commentId']}",
        effect="This soft-deletes that one private comment (Gorelo refuses any other kind); it can be recovered in the app.",
    )
    op = DELETE_PROJECT_COMMENT if task_id is None else DELETE_TASK_COMMENT
    try:
        data = await client_of(ctx).delete(op, path_params=ids, tool="delete_project_comment")
        return expect_object(data, op, tool="delete_project_comment")
    except GoreloAPIError as err:
        if err.write_unconfirmed:
            raise _delete_not_confirmed(_how_it_failed(err), err.trace_id) from err
        raise
