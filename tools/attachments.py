"""Attachment upload, multipart (toolset "tickets").

upload_attachment stores a file against an existing ticket, task or project and returns the
{name, url} pair that a comment takes in its attachments (create_ticket_comment for a ticket,
create_project_comment for a task or a project: the note in the result names the right one). The file
arrives inline (base64 or UTF-8 text): the tool never fetches a URL, so it cannot be used to make the
server request an address of the caller's choosing.

Ops used by this module:

    POST /v1/attachments

Uses GoreloClient.post_multipart (multipart/form-data with the parts file, itemType and itemId). Gorelo
accepts uploads up to 44 MB; this tool caps the decoded file at 10 MB (10485760 bytes) because MCP payloads
are text.

Two rules that only this tool can state:

* A task or project upload is refused locally, with no HTTP call, unless the "projects" toolset is enabled on
  this server (server_info_of(ctx)["toolsets"]; the message names GORELO_TOOLSETS). The file can only be
  attached through create_project_comment, a projects tool, and an uploaded file can neither be listed nor
  deleted through the API, so an upload that nothing can attach would stay orphaned for good.
* An upload whose outcome is unknown (timeout, connection failure, 5xx, an unusable answer) is reported
  with upload-specific advice instead of the generic "verify with a read before retrying", which is
  impossible here: the file may already be stored, it cannot be listed or deleted through the API, so the
  model must not upload it again without asking the user (compare post_alert in tools/alerts.py).
"""

import base64
import mimetypes
import re
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from gorelo_client import GoreloAPIError
from tools._common import client_of, describe_value, expect_object, gorelo_tool, guid, non_empty, server_info_of

TOOLSET = "tickets"
UPLOAD = "POST /v1/attachments"

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # decoded size; Gorelo itself accepts up to 44 MB
FILENAME_MAX = 255
DEFAULT_CONTENT_TYPE = "application/octet-stream"
ITEM_TYPES = {"ticket": "Ticket", "task": "Task", "project": "Project"}
# The item types whose file can only be attached through a projects tool (create_project_comment).
PROJECT_ITEM_TYPES = ("task", "project")

# Gorelo reports a bad part by its form field name; the file part has three tool parameters behind it.
UPLOAD_FIELDS = {
    "item_type": "itemType",
    "item_id": "itemId",
    "filename": "file",
    "content_base64": "file",
    "content_text": "file",
}

# The comment tool that takes the {name, url} pair for each item_type (a task and a project are commented on
# through the project comment tool, which needs the project id, and the task id for a task).
NEXT_STEP = {
    "ticket": "create_ticket_comment",
    "task": "create_project_comment with project_id and task_id",
    "project": "create_project_comment with project_id",
}

_MEDIA_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*")


def next_step_note(item_type: str) -> str:
    """The note that travels with the result: what to do with name and url, by item_type."""
    return (
        "The url carries a time-limited access token. Pass name and url together, unchanged, as one entry of the "
        f"attachments of {NEXT_STEP[item_type]}; do not store the url. "
        "Until a comment carries it the file stays orphaned on the record."
    )


# --------------------------------------------------------------------------
# Local validation (every helper raises ValueError naming the snake_case parameter)
# --------------------------------------------------------------------------


def _has_control_character(text: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def _item_type(value: Any) -> str:
    if not isinstance(value, str) or value not in ITEM_TYPES:
        raise ValueError(f"item_type: expected one of {', '.join(ITEM_TYPES)}, got {describe_value(value)}")
    return ITEM_TYPES[value]


def _projects_toolset_on(ctx: Context, item_type: str) -> None:
    """A task or project upload needs the projects toolset (create_project_comment is the only way to attach it).

    Refused locally, before the file is decoded or any HTTP call, when server_info_of(ctx) does not list
    "projects" (a ctx that carries no toolsets counts as "not enabled": nothing is assumed).
    """
    if item_type not in PROJECT_ITEM_TYPES:
        return
    enabled = {str(name).lower() for name in server_info_of(ctx)["toolsets"] or ()}
    if "projects" not in enabled:
        raise ValueError(
            f"item_type: a {item_type} upload needs the projects toolset, which is not enabled on this server (add it "
            "to GORELO_TOOLSETS). Without it the file could not be attached through a comment (create_project_comment "
            "is a projects tool) and an uploaded file cannot be deleted through the API, so nothing was uploaded. "
            "Tell the user."
        )


def _how_it_failed(err: GoreloAPIError) -> str:
    """One phrase for what happened to an upload whose outcome is unknown (never quotes the request)."""
    if err.kind == "timeout":
        return "the request timed out"
    if err.kind == "transport":
        return "the connection failed"
    if err.status is not None and err.status >= 500:
        notes = [str(note["message"]) for note in err.notifications[:3] if note.get("message")]
        return f"Gorelo answered HTTP {err.status}" + (f": {'; '.join(notes)}" if notes else "")
    return "its answer could not be used"


def _not_confirmed(how: str, trace_id: str | None) -> ToolError:
    """The upload-specific error for an upload that may or may not have been stored.

    The generic text says "verify with a read before retrying", which is impossible here: the API cannot list
    uploaded files and cannot delete them. So the text says the file may already be stored and that the model
    must not upload it again without asking the user.
    """
    trace = f" [trace {trace_id}]" if trace_id else ""
    return ToolError(
        f"Gorelo did not confirm upload_attachment ({how}). The file may already be stored. Uploaded files cannot be "
        f"listed or deleted through the API, so do not upload it again without asking the user first.{trace}"
    )


def _filename(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("filename: is required (the name the file gets in Gorelo, for example report.pdf)")
    non_empty("filename", value)
    # the value is not echoed: file names can carry client data and errors end up in the server log
    if value != value.strip():
        raise ValueError("filename: must not start or end with whitespace")
    if "/" in value or "\\" in value:
        raise ValueError("filename: must be a plain file name without a path ('/' or '\\')")
    if value.strip(".") == "":
        raise ValueError("filename: must be a real file name, not only dots")
    if _has_control_character(value):
        raise ValueError("filename: must not contain control characters (line breaks, tabs, NUL)")
    if len(value) > FILENAME_MAX:
        raise ValueError(f"filename: at most {FILENAME_MAX} characters, got {len(value)}")
    return value


def _limit_text() -> str:
    mib = 1024 * 1024
    if MAX_UPLOAD_BYTES % mib == 0:
        return f"{MAX_UPLOAD_BYTES // mib} MB ({MAX_UPLOAD_BYTES} bytes)"
    return f"{MAX_UPLOAD_BYTES} bytes"


def _too_large(param: str, size: int | None = None) -> ValueError:
    actual = f" ({size} bytes)" if size is not None else ""
    return ValueError(
        f"{param}: the decoded file{actual} is larger than the {_limit_text()} this tool accepts "
        "(Gorelo allows more, but files travel through MCP as text); upload a smaller file"
    )


def _decode_base64(text: str) -> bytes:
    # Cheap guard first: base64 grows the data by 4/3, so a text this long cannot fit the limit once decoded.
    if len(text) > 4 * ((MAX_UPLOAD_BYTES + 2) // 3):
        raise _too_large("content_base64")
    try:
        return base64.b64decode(text, validate=True)
    except ValueError:  # binascii.Error (alphabet, padding) and non-ASCII text are both ValueErrors
        raise ValueError(
            "content_base64: not valid standard base64. Use the A-Z a-z 0-9 + / alphabet with = padding, "
            "no whitespace or line breaks, no data: prefix and no URL-safe - or _ characters"
        ) from None


def _encode_text(text: str) -> bytes:
    if len(text) > MAX_UPLOAD_BYTES:  # every character takes at least one byte
        raise _too_large("content_text")
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(
            "content_text: contains a character that cannot be written as UTF-8 (for example a lone surrogate); "
            "send the file as content_base64 instead"
        ) from None


def _file_bytes(content_base64: Any, content_text: Any) -> bytes:
    """The decoded file: exactly one of the two parameters must be given, and it must not be blank."""
    for param, value in (("content_base64", content_base64), ("content_text", content_text)):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{param}: must be text")
        non_empty(param, value)
    if content_base64 is not None and content_text is not None:
        raise ValueError("content_base64 and content_text: give exactly one of them, not both")
    if content_base64 is None and content_text is None:
        raise ValueError(
            "content_base64 or content_text: exactly one is required (the file content; base64 for any file, "
            "text for a text file)"
        )
    if content_base64 is not None:
        param, data = "content_base64", _decode_base64(content_base64)
    else:
        param, data = "content_text", _encode_text(content_text)
    if len(data) > MAX_UPLOAD_BYTES:
        raise _too_large(param, len(data))
    return data


def _content_type(filename: str, given: Any) -> str:
    """The part's Content-Type: the caller's, else guessed from the extension, else application/octet-stream."""
    if given is not None:
        if not isinstance(given, str):
            raise ValueError("content_type: must be text such as application/pdf")
        non_empty("content_type", given)
        text = given.strip()
        media = text.partition(";")[0].strip()
        if not _MEDIA_TYPE.fullmatch(media) or _has_control_character(text):
            raise ValueError(
                f"content_type: expected a media type such as text/plain or application/pdf, got {describe_value(given)}"
            )
        return text
    # guess_type reads a "scheme:" prefix as a URL (a name such as data:x,y.png would be answered text/plain);
    # a file name is not a URL, so the colons are neutralised for the guess only.
    guessed, encoding = mimetypes.guess_type(filename.replace(":", "_"))
    # A compressed file (report.txt.gz) is guessed as the type of what is inside it: not what these bytes are.
    return guessed if guessed and encoding is None else DEFAULT_CONTENT_TYPE


def _usable_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------


@gorelo_tool(toolset=TOOLSET, kind="write", ops=[UPLOAD], field_map=UPLOAD_FIELDS)
async def upload_attachment(
    ctx: Context,
    item_type: Annotated[
        Literal["ticket", "task", "project"],
        Field(description="What the file belongs to. task and project need the projects toolset."),
    ],
    item_id: Annotated[
        str,
        Field(
            description="UUID of that record (get_ticket, list_tickets; list_project_tasks, list_projects with the "
            "projects toolset)."
        ),
    ],
    filename: Annotated[str, Field(description="Name with extension, no path, max 255 characters.")],
    content_base64: Annotated[
        str | None, Field(description="Standard base64 (padded, no line breaks). Or use content_text.")
    ] = None,
    content_text: Annotated[
        str | None, Field(description="Text content, stored as UTF-8. Or use content_base64.")
    ] = None,
    content_type: Annotated[str | None, Field(description="Media type; omit to guess from the filename.")] = None,
) -> dict:
    """Upload a file to a ticket, task or project; returns name and url to pass, together and unchanged, in a comment's attachments (the result's note names the tool). Give exactly one of content_base64 or content_text (max 10 MB decoded); URLs are never fetched. The url has a time-limited token: attach soon, do not store it. An unattached file stays orphaned (no API delete). Nothing is emailed. If a call fails or times out the file may already be stored: do not upload again without asking the user."""
    kind = _item_type(item_type)
    _projects_toolset_on(ctx, item_type)
    item = guid("item_id", item_id)
    name = _filename(filename)
    data = _file_bytes(content_base64, content_text)
    media_type = _content_type(name, content_type)
    try:
        answer = await client_of(ctx).post_multipart(
            UPLOAD,
            files={"file": (name, data, media_type)},
            form={"itemType": kind, "itemId": item},
            tool="upload_attachment",
        )
        record = expect_object(answer, UPLOAD, tool="upload_attachment")
    except GoreloAPIError as err:
        if err.write_unconfirmed:
            raise _not_confirmed(_how_it_failed(err), err.trace_id) from err
        raise
    missing = [label for label in ("Name", "Url") if not _usable_text(record.get(label))]
    if missing:
        raise _not_confirmed(f"Gorelo answered success but its answer has no usable {' and '.join(missing)}", None)
    return {"name": record["Name"], "url": record["Url"], "note": next_step_note(item_type)}
