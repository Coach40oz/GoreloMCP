"""Forms, form responses and submission links (toolset "forms"; the API key needs the Forms scope).

Three tools, every one in toolset "forms":

    list_forms                    read    GET /v1/forms (paged)
    list_form_responses           read    GET /v1/forms/{formId}/responses (paged)
    create_form_submission_link   write   POST /v1/forms/{formId}/submission-links

Ops owned by this module (each must exist in spec/spec_index.json, none may be in FORBIDDEN_OPS; keep
the list in step with the ops=[...] of the declarations below):

    GET /v1/forms
    GET /v1/forms/{formId}/responses
    POST /v1/forms/{formId}/submission-links

Things to know:

* Scope. Every Forms operation needs the "Forms" scope on the API key. Without it Gorelo answers 403 with
  Notification code 080203 and format_gorelo_error turns that into the "missing scope" message. The
  key used during development did not have the scope, so these tools are tested offline only.
* The toolset is not in DEFAULT_TOOLSETS: the operator enables it with GORELO_TOOLSETS (forms or all).
* The API has no endpoint for a single form's definition. Each answer in a response carries its own FieldId
  and Label (the question text at submission time), which is the only question text the API offers.
* Form ids are short url-safe tokens (the spec pattern is ^[A-Za-z0-9_-]{1,50}$), not numbers or GUIDs.
* Integer ids are StrictId parameters (JSON true, "5" or 5.0 is refused by the schema) and are then
  range-checked by positive_ids; the ticket and task ids go through guid.
* The POST answers with {"Link", "ExpiresOn"} and there is no single-link GET, so the answer is returned
  as it is and there is no re-read. The API cannot list links either, so a POST
  whose outcome is unknown (timeout, connection failure, 5xx, an answer without a usable link) may have
  issued a link: create_form_submission_link raises a ToolError that says so and tells the model not to
  request another link without asking the user.
"""

import re
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from gorelo_client import GoreloAPIError
from tools._common import (
    StrictId,
    build_body,
    clamp_page_size,
    client_of,
    csv_ids,
    describe_value,
    expect_object,
    gorelo_tool,
    guid,
    non_empty,
    paged_result,
    positive_ids,
)

LIST_OP = "GET /v1/forms"
RESPONSES_OP = "GET /v1/forms/{formId}/responses"
LINK_OP = "POST /v1/forms/{formId}/submission-links"

# Form status ids as the GET /v1/forms StatusIds text gives them: "Active=1, Archived=2".
FORM_STATUSES = {1: "Active", 2: "Archived"}

# snake_case tool parameter -> Gorelo query name (GET /v1/forms). The same map turns a Gorelo PropertyName
# back into the parameter to fix in error messages.
LIST_QUERY = {
    "status_ids": "StatusIds",
    "group_ids": "GroupIds",
    "tag_ids": "TagIds",
    "client_ids": "ClientIds",
    "allow_in_portal": "AllowInPortal",
    "query": "Query",
    "sort_order": "SortOrder",
}
LIST_FIELDS = {**LIST_QUERY, "page_size": "PageSize", "cursor": "Cursor"}
RESPONSES_FIELDS = {"form_id": "formId", "page_size": "PageSize", "cursor": "Cursor"}

# snake_case tool parameter -> CreateSubmissionLinkCommand field. Every spec field is here and nothing else.
LINK_BODY = {"ticket_id": "TicketId", "task_id": "TaskId"}
LINK_FIELDS = {"form_id": "formId", **LINK_BODY}

_FORM_ID = re.compile(r"[A-Za-z0-9_-]{1,50}")  # the spec's pattern for the formId path parameter


# --------------------------------------------------------------------------
# Local validation (every message names the snake_case parameter)
# --------------------------------------------------------------------------


def _status_ids(param: str, values: Any) -> list[int] | None:
    """Form status ids: 1 Active and 2 Archived are the only ones Gorelo documents."""
    ids = positive_ids(param, values)
    for item in ids or []:
        if item not in FORM_STATUSES:
            accepted = ", ".join(f"{status} ({label})" for status, label in FORM_STATUSES.items())
            raise ValueError(f"{param}: Gorelo form statuses are {accepted}, got {item}")
    return ids


def _sort_order(param: str, value: Any) -> str | None:
    if value is not None and value not in ("asc", "desc"):
        raise ValueError(f'{param}: must be "asc" or "desc" (sorted by last update), got {describe_value(value)}')
    return value


def _form_id(param: str, value: Any) -> str:
    """The form Id token that list_forms returns (the spec pattern for the formId path parameter)."""
    if not isinstance(value, str) or not _FORM_ID.fullmatch(value):
        what = "text that is not a form Id" if isinstance(value, str) and value else describe_value(value)
        raise ValueError(
            f"{param}: expected a form Id from list_forms (1 to 50 letters, digits, '_' or '-'), got {what}"
        )
    return value


def _link_unconfirmed(err: GoreloAPIError) -> ToolError:
    """The ToolError for a link request whose outcome is unknown: Gorelo may have issued the link anyway.

    The API cannot list links, so a read cannot settle it: the model must not ask for another
    link on its own (each call issues a new one) and must ask the user first. The text says what happened
    to the request, never what Gorelo answered in detail, and carries the trace id when there is one.
    """
    if err.kind == "timeout":
        what = "the request timed out"
    elif err.kind == "transport":
        what = "the connection failed"
    elif err.status is not None:
        what = f"Gorelo answered HTTP {err.status} without a usable link"
    else:
        what = "Gorelo's answer had no usable link"
    trace = f" [trace {err.trace_id}]" if err.trace_id else ""
    return ToolError(
        f"Gorelo did not confirm create_form_submission_link ({what}). A submission link may already have been "
        "issued and the API cannot list links, so do not request another one without asking the user first."
        + trace
    )


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


@gorelo_tool(toolset="forms", kind="read", ops=[LIST_OP], field_map=LIST_FIELDS)
async def list_forms(
    ctx: Context,
    status_ids: Annotated[
        list[StrictId] | None, Field(description="1 Active, 2 Archived (deleted in the app). Default: Active only.")
    ] = None,
    group_ids: Annotated[
        list[StrictId] | None, Field(description="Technician group ids (list_org_groups).")
    ] = None,
    tag_ids: Annotated[
        list[StrictId] | None,
        Field(description="Tag ids the form applies to its tickets (list_ticket_tags)."),
    ] = None,
    client_ids: Annotated[
        list[StrictId] | None, Field(description="Forms restricted to these client ids (list_clients).")
    ] = None,
    allow_in_portal: Annotated[
        bool | None, Field(description="true: only portal forms; false: only non-portal forms.")
    ] = None,
    query: Annotated[str | None, Field(description="Title keyword (case-insensitive).")] = None,
    sort_order: Annotated[
        Literal["asc", "desc"] | None, Field(description="By last update; default desc.")
    ] = None,
    page_size: Annotated[int, Field(description="Rows per page, 1-200.")] = 50,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous page.")] = None,
) -> dict:
    """List forms, most recently updated first by default, one page at a time.

    ResponseCount counts submitted responses only, not pending links or drafts. An empty ClientIds means every client.
    Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.
    """
    filters: dict[str, Any] = {
        "status_ids": _status_ids("status_ids", status_ids),
        "group_ids": positive_ids("group_ids", group_ids),
        "tag_ids": positive_ids("tag_ids", tag_ids),
        "client_ids": positive_ids("client_ids", client_ids),
        "allow_in_portal": allow_in_portal,
        "query": non_empty("query", query),
        "sort_order": _sort_order("sort_order", sort_order),
    }
    non_empty("cursor", cursor)
    size = clamp_page_size(page_size)
    params = {
        LIST_QUERY[name]: csv_ids(name, value) if isinstance(value, list) else value for name, value in filters.items()
    }
    page = await client_of(ctx).get_page(LIST_OP, query=params, page_size=size, cursor=cursor, tool="list_forms")
    return paged_result(page, filters)


@gorelo_tool(toolset="forms", kind="read", ops=[RESPONSES_OP], field_map=RESPONSES_FIELDS)
async def list_form_responses(
    ctx: Context,
    form_id: Annotated[str, Field(description="Form Id from list_forms (letters, digits, '_' or '-').")],
    page_size: Annotated[int, Field(description="Rows per page, 1-200.")] = 50,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous page.")] = None,
) -> dict:
    """List a form's submitted responses, newest first, one page at a time.

    An answer carries FieldId, Label (the question text at submission time), Type and one of TextValue, NumberValue or OptionValues (all null when left blank). No API returns the form definition, so Label is the only question text.
    Paging: pass next_cursor back as cursor with the SAME form_id until has_more is false.
    """
    form = _form_id("form_id", form_id)
    non_empty("cursor", cursor)
    size = clamp_page_size(page_size)
    page = await client_of(ctx).get_page(
        RESPONSES_OP, path_params={"formId": form}, page_size=size, cursor=cursor, tool="list_form_responses"
    )
    return paged_result(page, {"form_id": form})


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------


@gorelo_tool(toolset="forms", kind="write", ops=[LINK_OP], field_map=LINK_FIELDS)
async def create_form_submission_link(
    ctx: Context,
    form_id: Annotated[str, Field(description="Form Id from list_forms (letters, digits, '_' or '-').")],
    ticket_id: Annotated[
        str | None, Field(description="Active ticket GUID (list_tickets), not the display number.")
    ] = None,
    task_id: Annotated[
        str | None,
        Field(description="Active task GUID (list_project_tasks, projects toolset) in an active project."),
    ] = None,
) -> dict:
    """Issue a submission link for a form and return {Link, ExpiresOn}.

    The link opens the form without a login, so whoever holds it can fill it in: give it only to the person who should. One submission through it is filed against the form and the ticket or task you name (at most one). An unused link expires seven days after issue (ExpiresOn).
    Side effects: every call issues a new, separate link; nothing is submitted until someone fills the form in. If a call fails with "did not confirm", a link may already exist and the API cannot list links: ask the user before requesting another.
    """
    form = _form_id("form_id", form_id)
    ticket = guid("ticket_id", ticket_id) if ticket_id is not None else None
    task = guid("task_id", task_id) if task_id is not None else None
    if ticket is not None and task is not None:
        raise ValueError(
            "ticket_id and task_id: give at most one of them (the submission is filed against one ticket or one "
            "task), got both"
        )
    body = build_body({"ticket_id": ticket, "task_id": task}, LINK_BODY)  # {} when neither is given
    try:
        data = await client_of(ctx).post(
            LINK_OP, path_params={"formId": form}, json_body=body, tool="create_form_submission_link"
        )
        data = expect_object(data, LINK_OP, tool="create_form_submission_link")
        link = data.get("Link")
        if not isinstance(link, str) or not link.strip():
            raise GoreloAPIError(
                f"{LINK_OP}: Data has no Link; refusing to guess",
                status=200, op_key=LINK_OP, kind="shape", write_unconfirmed=True,
            )
    except GoreloAPIError as err:
        if err.write_unconfirmed:
            raise _link_unconfirmed(err) from err
        raise
    return data
