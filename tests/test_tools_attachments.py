"""upload_attachment (tools/attachments.py), offline.

The tool goes through an in-process fastmcp Client on top of MockGorelo, so what is asserted is what a model
would see and what Gorelo would receive: one multipart POST with the parts file, itemType and itemId, the
decoded bytes exactly, the media type, the {name, url} result, a Gorelo error mapped to the snake_case
parameter and every local validation error with zero HTTP calls.

Further behavior: the "next step" note in the result is built from item_type (a ticket upload points at
create_ticket_comment, a task or project upload at create_project_comment), the tool text stays generic, the
item id is checked with the shared guid helper, and a success whose Data is not the expected object raises a
shape error instead of returning anything.

Also: a task or project upload is refused locally unless the projects toolset is enabled, and an
upload whose outcome is unknown gets its own advice, like post_alert: the file may already be stored, the API
cannot list or delete it, so it must not be uploaded again without the user.
"""

import base64
import logging

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
    uid,
)
from fastmcp.exceptions import ToolError

import tools.attachments as attachments
from gorelo_client import FORBIDDEN_OPS
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio

ITEM = uid(1)
COMMENTS = f"/v1/tickets/{ITEM}/comments"
COMMENT = uid(2)
UPLOAD = "/v1/attachments"
URL = "https://files.example.test/tickets/00000001/notes.txt?sig=AbC%2Bd%3D&se=2026-10-02T00%3A00%3A00Z"
MIB = 1024 * 1024


TICKET_NOTE = (
    "The url carries a time-limited access token. Pass name and url together, unchanged, as one entry of the "
    "attachments of create_ticket_comment; do not store the url. "
    "Until a comment carries it the file stays orphaned on the record."
)
TASK_NOTE = (
    "The url carries a time-limited access token. Pass name and url together, unchanged, as one entry of the "
    "attachments of create_project_comment with project_id and task_id; do not store the url. "
    "Until a comment carries it the file stays orphaned on the record."
)
PROJECT_NOTE = (
    "The url carries a time-limited access token. Pass name and url together, unchanged, as one entry of the "
    "attachments of create_project_comment with project_id; do not store the url. "
    "Until a comment carries it the file stays orphaned on the record."
)


def stored(name="notes.txt", url=URL):
    """What Gorelo answers: a CommentAttachmentModel."""
    return {"Name": name, "Url": url}


@pytest.fixture
def server(server_factory):
    return server_factory()


def text_upload(**overrides):
    arguments = {"item_type": "ticket", "item_id": ITEM, "filename": "notes.txt", "content_text": "hello"}
    arguments.update(overrides)
    return arguments


async def refused(server, mock, arguments, *fragments):
    """The call fails locally: the error carries every fragment and Gorelo was never contacted."""
    text = await call_tool_error(server, "upload_attachment", arguments)
    for fragment in fragments:
        assert fragment in text, f"{fragment!r} not in: {text}"
    assert mock.requests == []
    return text


# --------------------------------------------------------------------------
# What the module declares
# --------------------------------------------------------------------------


def test_declaration_matches_the_declaration(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "upload_attachment")
    assert (spec.toolset, spec.kind, spec.ops) == ("tickets", "write", ["POST /v1/attachments"])
    assert spec.destructive_hint is False
    assert all(op in spec_index.ops and op not in FORBIDDEN_OPS for op in spec.ops)
    assert {s.name for s in REGISTRY.specs if s.fn.__module__ == "tools.attachments"} == {"upload_attachment"}
    assert "POST /v1/attachments" in attachments.__doc__


async def test_the_schema_has_no_way_to_name_a_url_to_fetch(server_factory):
    tools = {t.name: t for t in await list_tools(server_factory())}
    schema = tools["upload_attachment"].inputSchema
    assert set(schema["properties"]) == {
        "item_type", "item_id", "filename", "content_base64", "content_text", "content_type"
    }
    assert schema["required"] == ["item_type", "item_id", "filename"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["item_type"]["enum"] == ["ticket", "task", "project"]
    for definition in schema["properties"].values():
        assert definition.get("description")
    hints = tools["upload_attachment"].annotations
    assert (hints.readOnlyHint, hints.destructiveHint) == (False, False)


async def test_a_url_parameter_is_refused_and_nothing_is_fetched_or_sent(server, mock_gorelo):
    for name in ("url", "source_url", "file_url"):
        text = await call_tool_error(server, "upload_attachment", text_upload(**{name: "http://169.254.169.254/latest"}))
        assert name in text and "Unexpected keyword argument" in text
    assert mock_gorelo.requests == []


async def test_the_docstring_states_what_the_documentation_requires(server_factory):
    tools = {t.name: t for t in await list_tools(server_factory())}
    text = " ".join(tools["upload_attachment"].description.split())
    for fragment in (
        "content_base64", "content_text", "Give exactly one of", "10 MB decoded", "URLs are never fetched",
        "together and unchanged", "comment's attachments", "the result's note names the tool",
        "time-limited token", "do not store it", "orphaned", "no API delete", "Nothing is emailed",
        # what to do when the outcome is unknown
        "If a call fails or times out the file may already be stored: do not upload again without asking the user",
    ):
        assert fragment in text, fragment


async def test_the_tool_text_stays_generic_because_the_note_names_the_comment_tool(server_factory):
    # which comment tool takes the pair depends on item_type, so the listing must not pick one
    tools = {t.name: t for t in await list_tools(server_factory())}
    schema = tools["upload_attachment"].inputSchema["properties"]
    for text in [tools["upload_attachment"].description] + [definition["description"] for definition in schema.values()]:
        assert "create_ticket_comment" not in text and "create_project_comment" not in text, text


async def test_the_parameters_say_where_the_item_id_comes_from_and_the_size_limits(server_factory):
    tools = {t.name: t for t in await list_tools(server_factory())}
    schema = tools["upload_attachment"].inputSchema["properties"]
    for fragment in ("get_ticket", "list_tickets", "list_project_tasks", "list_projects", "projects toolset"):
        assert fragment in schema["item_id"]["description"], fragment
    # the model learns from the schema that task and project uploads depend on a toolset
    assert "projects toolset" in schema["item_type"]["description"]
    assert "255" in schema["filename"]["description"]
    assert "content_text" in schema["content_base64"]["description"]
    assert "content_base64" in schema["content_text"]["description"]
    assert all(len(definition["description"]) <= 160 for definition in schema.values())
    assert len(tools["upload_attachment"].description) <= 700


# --------------------------------------------------------------------------
# The request Gorelo receives
# --------------------------------------------------------------------------


async def test_a_text_upload_is_one_multipart_post_with_the_three_parts(server, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, envelope(stored()))
    content = "h\u00e9llo\nw\u00f6rld\r\n"
    result = await call_tool(server, "upload_attachment", text_upload(content_text=content))
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.json) == ("POST", UPLOAD, {}, None)
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert request.form == {"itemType": "Ticket", "itemId": ITEM}
    assert request.files == {"file": ("notes.txt", content.encode("utf-8"), "text/plain")}
    assert len(mock_gorelo.requests) == 1
    assert set(result) == {"name", "url", "note"}
    assert result["name"] == "notes.txt" and result["url"] == URL
    assert result["note"] == TICKET_NOTE


async def test_every_byte_of_a_base64_upload_arrives_exactly(server, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, envelope(stored("chart.png", "https://files.example.test/c?sig=1")))
    raw = bytes(range(256)) * 5 + b"\r\n\r\n--boundary-looking-bytes--\r\n"
    result = await call_tool(
        server, "upload_attachment",
        {"item_type": "ticket", "item_id": ITEM, "filename": "chart.png", "content_base64": base64.b64encode(raw).decode("ascii")},
    )
    assert mock_gorelo.last.files == {"file": ("chart.png", raw, "image/png")}
    assert result["name"] == "chart.png" and result["url"] == "https://files.example.test/c?sig=1"


@pytest.mark.parametrize("kind, sent", [("ticket", "Ticket"), ("task", "Task"), ("project", "Project")])
async def test_the_item_type_is_sent_in_gorelos_capitalisation(server, mock_gorelo, kind, sent):
    mock_gorelo.on("POST", UPLOAD, envelope(stored()))
    await call_tool(server, "upload_attachment", text_upload(item_type=kind))
    assert mock_gorelo.last.form["itemType"] == sent


async def test_the_item_id_is_sent_in_canonical_form(server, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, envelope(stored()))
    for spelling in (ITEM.upper(), ITEM.replace("-", ""), ITEM):
        await call_tool(server, "upload_attachment", text_upload(item_id=spelling))
        assert mock_gorelo.last.form["itemId"] == ITEM


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("report.pdf", "application/pdf"),
        ("notes.txt", "text/plain"),
        ("data.json", "application/json"),
        ("photo.png", "image/png"),
        ("scan.jpeg", "image/jpeg"),
        ("PHOTO.PNG", "image/png"),
        ("my report (final) v2.pdf", "application/pdf"),
        ("data:foo,bar.png", "image/png"),  # a "scheme:" in a name must not be read as a URL
        ("http:report.pdf", "application/pdf"),
        ("README", "application/octet-stream"),
        ("blob.unknownext123", "application/octet-stream"),
        (".hidden", "application/octet-stream"),
        ("archive.gz", "application/octet-stream"),
        ("report.txt.gz", "application/octet-stream"),  # guessed as text/plain with an encoding: not what the bytes are
    ],
)
async def test_the_media_type_defaults_from_the_filename(server, mock_gorelo, filename, expected):
    mock_gorelo.on("POST", UPLOAD, envelope(stored(filename)))
    await call_tool(server, "upload_attachment", text_upload(filename=filename))
    assert mock_gorelo.last.files["file"][0] == filename
    assert mock_gorelo.last.files["file"][2] == expected


@pytest.mark.parametrize(
    "given, seen",
    [
        ("application/x-custom", "application/x-custom"),
        ("text/plain; charset=utf-8", "text/plain"),
        ("  application/pdf  ", "application/pdf"),
        ("image/svg+xml", "image/svg+xml"),
    ],
)
async def test_a_given_content_type_wins_over_the_extension(server, mock_gorelo, given, seen):
    mock_gorelo.on("POST", UPLOAD, envelope(stored()))
    await call_tool(server, "upload_attachment", text_upload(content_type=given))
    assert mock_gorelo.last.files["file"][2] == seen


async def test_a_text_part_can_be_sent_with_the_full_content_type_header(server, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, envelope(stored()))
    await call_tool(server, "upload_attachment", text_upload(content_type="text/plain; charset=utf-8"))
    assert b"Content-Type: text/plain; charset=utf-8" in mock_gorelo.last.content


async def test_a_non_ascii_filename_reaches_the_wire_as_utf8(server, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, envelope(stored("r\u00e9sum\u00e9.txt")))
    await call_tool(server, "upload_attachment", text_upload(filename="r\u00e9sum\u00e9.txt"))
    assert 'filename="r\u00e9sum\u00e9.txt"'.encode("utf-8") in mock_gorelo.last.content


async def test_only_the_spec_defined_parts_are_sent(server, mock_gorelo, spec_index):
    mock_gorelo.on("POST", UPLOAD, envelope(stored()))
    await call_tool(server, "upload_attachment", text_upload())
    fields = spec_index.ops["POST /v1/attachments"].body["fields"]
    assert set(mock_gorelo.last.form) | set(mock_gorelo.last.files) == set(fields) == {"file", "itemType", "itemId"}


async def test_the_file_content_and_name_never_reach_the_logs(server, mock_gorelo, caplog):
    mock_gorelo.on("POST", UPLOAD, envelope(stored()))
    with caplog.at_level(logging.DEBUG):
        await call_tool(server, "upload_attachment", text_upload(filename="payroll-2026.csv", content_text="SECRET-CONTENT-123"))
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "upload_attachment" in logged  # the client did log the call ...
    assert "SECRET-CONTENT-123" not in logged and "payroll-2026" not in logged and ITEM not in logged  # ... without values


# --------------------------------------------------------------------------
# The result
# --------------------------------------------------------------------------


async def test_the_result_carries_gorelos_name_and_url_unchanged_with_the_note(server, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, envelope(stored("renamed-by-gorelo (1).txt", URL)))
    result = await call_tool(server, "upload_attachment", text_upload())
    assert result["name"] == "renamed-by-gorelo (1).txt" and result["url"] == URL
    note = " ".join(result["note"].split())
    for fragment in ("time-limited access token", "name and url together", "create_ticket_comment", "orphaned"):
        assert fragment in note


@pytest.mark.parametrize(
    "kind, note, absent",
    [
        ("ticket", TICKET_NOTE, ("create_project_comment", "project_id", "task_id")),
        ("task", TASK_NOTE, ("create_ticket_comment",)),
        ("project", PROJECT_NOTE, ("create_ticket_comment", "task_id")),
    ],
)
async def test_the_next_step_note_is_built_from_the_item_type(server, mock_gorelo, kind, note, absent):
    # ticket -> create_ticket_comment; task -> create_project_comment with project_id and task_id;
    # project -> create_project_comment with project_id
    mock_gorelo.on("POST", UPLOAD, envelope(stored()))
    result = await call_tool(server, "upload_attachment", text_upload(item_type=kind))
    assert result["note"] == note
    assert result["name"] == "notes.txt" and result["url"] == URL and set(result) == {"name", "url", "note"}
    for forbidden in absent:
        assert forbidden not in result["note"], forbidden
    for always in ("time-limited access token", "name and url together, unchanged", "do not store the url", "orphaned"):
        assert always in result["note"]


def test_the_note_has_one_entry_per_item_type_the_tool_accepts():
    assert set(attachments.NEXT_STEP) == set(attachments.ITEM_TYPES)
    for kind in attachments.ITEM_TYPES:
        assert attachments.next_step_note(kind).count("attachments of ") == 1


async def test_the_result_feeds_straight_into_a_comment(server_factory, mock_gorelo):
    server = server_factory()
    mock_gorelo.on("POST", UPLOAD, envelope(stored()))
    mock_gorelo.on("POST", COMMENTS, envelope({"Id": COMMENT}))
    mock_gorelo.on("GET", f"{COMMENTS}/{COMMENT}", envelope({"Id": COMMENT, "Attachments": [stored()]}))
    uploaded = await call_tool(server, "upload_attachment", text_upload())
    comment = await call_tool(
        server, "create_ticket_comment",
        {"ticket_id": ITEM, "body": "<p>See the attached notes.</p>",
         "attachments": [{"name": uploaded["name"], "url": uploaded["url"]}]},
    )
    assert mock_gorelo.requests[1].json == {
        "Body": "<p>See the attached notes.</p>", "ConversationTypeId": 2, "Attachments": [{"Name": "notes.txt", "Url": URL}]
    }
    assert comment["Attachments"] == [stored()]


# --------------------------------------------------------------------------
# Local validation: every one of these fails before any HTTP call
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, fragments",
    [
        ({"content_text": None}, ["content_base64 or content_text: exactly one is required"]),
        ({"content_base64": "aGVsbG8="}, ["content_base64 and content_text: give exactly one of them, not both"]),
        ({"content_text": ""}, ["content_text: must not be empty or whitespace only"]),
        ({"content_text": "  \n\t"}, ["content_text: must not be empty or whitespace only"]),
        ({"content_text": None, "content_base64": ""}, ["content_base64: must not be empty or whitespace only"]),
        ({"content_text": None, "content_base64": "   "}, ["content_base64: must not be empty or whitespace only"]),
        ({"content_text": "x", "content_base64": ""}, ["content_base64: must not be empty or whitespace only"]),
    ],
)
async def test_exactly_one_non_blank_content_parameter_is_required(server, mock_gorelo, overrides, fragments):
    await refused(server, mock_gorelo, text_upload(**overrides), *fragments)


@pytest.mark.parametrize(
    "content",
    [
        "not base64!",
        "aGVsbG8",  # padding missing
        "aGVsbG8===",  # too much padding
        "aGVs bG8=",  # a space
        "aGVs\nbG8=",  # a line break (MIME style wrapping)
        "aGVsbG8=\n",  # a trailing line break
        " aGVsbG8=",  # a leading space
        "data:text/plain;base64,aGVsbG8=",  # a data URI
        "aGVs-G8_",  # the URL-safe alphabet
        "aGVsbG8=aGVsbG8=",  # data after the padding
        "h\u00e9llo",  # not ASCII
        "====",
    ],
)
async def test_content_base64_must_be_strict_standard_base64(server, mock_gorelo, content):
    text = await refused(
        server, mock_gorelo,
        {"item_type": "ticket", "item_id": ITEM, "filename": "a.bin", "content_base64": content},
        "content_base64: not valid standard base64", "no whitespace or line breaks",
    )
    assert content not in text  # the (possibly huge) value is never echoed


@pytest.mark.parametrize(
    "filename, fragment",
    [
        ("", "filename: must not be empty or whitespace only"),
        ("   ", "filename: must not be empty or whitespace only"),
        ("a/b.txt", "filename: must be a plain file name without a path"),
        ("../etc/passwd", "filename: must be a plain file name without a path"),
        ("a\\b.txt", "filename: must be a plain file name without a path"),
        ("C:\\temp\\a.txt", "filename: must be a plain file name without a path"),
        ("..", "filename: must be a real file name, not only dots"),
        (".", "filename: must be a real file name, not only dots"),
        ("...", "filename: must be a real file name, not only dots"),
        ("a\nb.txt", "filename: must not contain control characters"),
        ("a\r\nX-Evil: 1.txt", "filename: must not contain control characters"),
        ("a\x00.txt", "filename: must not contain control characters"),
        ("a\tb.txt", "filename: must not contain control characters"),
        (" a.txt", "filename: must not start or end with whitespace"),
        ("a.txt ", "filename: must not start or end with whitespace"),
        ("x" * 256, "filename: at most 255 characters, got 256"),
    ],
)
async def test_the_filename_must_be_a_plain_name(server, mock_gorelo, filename, fragment):
    await refused(server, mock_gorelo, text_upload(filename=filename), fragment)


@pytest.mark.parametrize("filename", ["Acme payroll/2026.xlsx", " Acme payroll.xlsx", "....", "Acme\\payroll.xlsx"])
async def test_a_refused_filename_is_never_echoed_into_the_error(server, mock_gorelo, filename):
    # errors reach the server log, and a file name can carry client data
    text = await refused(server, mock_gorelo, text_upload(filename=filename), "filename:")
    assert "Acme" not in text and "payroll" not in text


async def test_a_255_character_filename_is_accepted(server, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, envelope(stored()))
    name = "x" * 251 + ".txt"
    await call_tool(server, "upload_attachment", text_upload(filename=name))
    assert mock_gorelo.last.files["file"][0] == name


@pytest.mark.parametrize(
    "item_id",
    ["abc", "TCK-2029", "", "123", ITEM + "0", ITEM[:-1], f"{ITEM}/..", "..%2F..", "{" + ITEM + "}", "urn:uuid:" + ITEM,
     f" {ITEM}\n", f"{ITEM} ", f"\t{ITEM}"],
)
async def test_the_item_id_must_be_a_uuid(server, mock_gorelo, item_id):
    await refused(server, mock_gorelo, text_upload(item_id=item_id), "item_id: expected a GUID such as")


async def test_the_item_type_must_be_ticket_task_or_project(server, mock_gorelo):
    await refused(server, mock_gorelo, text_upload(item_type="invoice"), "item_type", "'ticket'", "'task'", "'project'")
    await refused(server, mock_gorelo, text_upload(item_type="Ticket"), "item_type")


@pytest.mark.parametrize(
    "content_type",
    ["text", "text/", "/plain", "text plain", "text/plain\r\nX-Evil: 1", "text/pl\nain", "*/*", "", "   "],
)
async def test_a_content_type_must_look_like_a_media_type(server, mock_gorelo, content_type):
    await refused(server, mock_gorelo, text_upload(content_type=content_type), "content_type")


def test_helpers_refuse_what_the_mcp_layer_would_already_have_coerced():
    # a model never gets here (pydantic checks the types first); direct callers and future code do
    for bad in (None, 5, ["ticket"], "Ticket", "invoice", ""):
        with pytest.raises(ValueError, match="item_type"):
            attachments._item_type(bad)
    assert [attachments._item_type(kind) for kind in ("ticket", "task", "project")] == ["Ticket", "Task", "Project"]
    for bad in (None, 5, b"a.txt", ["a.txt"]):
        with pytest.raises(ValueError, match="filename"):
            attachments._filename(bad)
    for bad in (5, b"abc", ["abc"]):
        with pytest.raises(ValueError, match="content_base64: must be text"):
            attachments._file_bytes(bad, None)
        with pytest.raises(ValueError, match="content_text: must be text"):
            attachments._file_bytes(None, bad)
    for bad in (5, b"text/plain", ["text/plain"]):
        with pytest.raises(ValueError, match="content_type: must be text"):
            attachments._content_type("a.txt", bad)
    assert attachments._content_type("a.txt", None) == "text/plain"


def test_the_module_keeps_no_private_copy_of_a_shared_helper():
    for name in ("_item_id", "_shown", "_kind", "_UUID_HYPHENATED", "_UUID_BARE", "NOTE"):
        assert not hasattr(attachments, name), name


@pytest.mark.parametrize("bad", [5, True, None, ["abc"], {"Id": ITEM}])
async def test_a_non_text_item_id_is_refused_by_the_schema_before_the_tool_runs(server, mock_gorelo, bad):
    await refused(server, mock_gorelo, text_upload(item_id=bad), "item_id")


@pytest.mark.parametrize("missing", ["item_type", "item_id", "filename"])
async def test_the_three_identifying_fields_are_required(server, mock_gorelo, missing):
    arguments = text_upload()
    del arguments[missing]
    await refused(server, mock_gorelo, arguments, missing, "required")


# --------------------------------------------------------------------------
# a task or project upload needs the projects toolset
# --------------------------------------------------------------------------

NO_PROJECTS = frozenset({"core", "tickets"})  # upload_attachment is a tickets tool; the projects toolset is off


@pytest.mark.parametrize("kind", ["task", "project"])
async def test_a_task_or_project_upload_is_refused_locally_when_the_projects_toolset_is_off(
    server_factory, mock_gorelo, kind
):
    server = server_factory(toolsets=NO_PROJECTS)
    text = await refused(server, mock_gorelo, text_upload(item_type=kind), "item_type", "GORELO_TOOLSETS")
    assert text.startswith(f"item_type: a {kind} upload needs the projects toolset, which is not enabled on this server")
    assert "add it to GORELO_TOOLSETS" in text
    assert "create_project_comment is a projects tool" in text  # why: nothing could attach the file afterwards
    assert "cannot be deleted through the API, so nothing was uploaded" in text
    assert text.endswith("Tell the user.")


@pytest.mark.parametrize("kind", ["task", "project"])
async def test_the_toolset_refusal_comes_before_the_file_is_looked_at(server_factory, mock_gorelo, kind):
    # a huge or broken payload must not be decoded for an upload that is refused anyway
    server = server_factory(toolsets=NO_PROJECTS)
    arguments = {"item_type": kind, "item_id": ITEM, "filename": "a.bin", "content_base64": "not base64!"}
    text = await refused(server, mock_gorelo, arguments, "GORELO_TOOLSETS")
    assert "content_base64" not in text


async def test_a_ticket_upload_needs_no_projects_toolset(server_factory, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, envelope(stored()))
    server = server_factory(toolsets=frozenset({"tickets"}))
    result = await call_tool(server, "upload_attachment", text_upload(item_type="ticket"))
    assert result["note"] == TICKET_NOTE
    assert mock_gorelo.last.form["itemType"] == "Ticket"


@pytest.mark.parametrize("kind, sent", [("task", "Task"), ("project", "Project")])
@pytest.mark.parametrize("toolsets", [frozenset({"tickets", "projects"}), frozenset({"projects"} | NO_PROJECTS)])
async def test_a_task_or_project_upload_goes_through_when_the_projects_toolset_is_on(
    server_factory, mock_gorelo, kind, sent, toolsets
):
    mock_gorelo.on("POST", UPLOAD, envelope(stored()))
    result = await call_tool(server_factory(toolsets=toolsets), "upload_attachment", text_upload(item_type=kind))
    assert mock_gorelo.last.form["itemType"] == sent
    assert result["note"] == (TASK_NOTE if kind == "task" else PROJECT_NOTE)
    assert len(mock_gorelo.requests) == 1


async def test_the_toolset_check_reads_the_servers_own_toolsets_and_ignores_case(client_factory, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, envelope(stored()))
    async with client_factory() as client:
        for toolsets in (["projects"], ["tickets", "Projects"], ("core", "PROJECTS", "tickets")):
            ctx = make_ctx(client, toolsets=toolsets)
            result = await attachments.upload_attachment(ctx, **text_upload(item_type="task"))
            assert result["name"] == "notes.txt"
    assert len(mock_gorelo.requests) == 3


@pytest.mark.parametrize("lifespan", [{}, {"toolsets": None}, {"toolsets": []}, {"toolsets": ["tickets", "core"]}])
@pytest.mark.parametrize("kind", ["task", "project"])
async def test_a_ctx_that_does_not_list_projects_is_treated_as_not_enabled(client_factory, mock_gorelo, lifespan, kind):
    # nothing is assumed: no toolset information means the upload is refused, with no HTTP call
    async with client_factory() as client:
        with pytest.raises(ToolError, match="GORELO_TOOLSETS"):  # the decorator turns the ValueError into a ToolError
            await attachments.upload_attachment(make_ctx(client, **lifespan), **text_upload(item_type=kind))
    assert mock_gorelo.requests == []


async def test_a_ticket_upload_is_never_refused_for_a_missing_toolset_list(client_factory, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, envelope(stored()))
    async with client_factory() as client:
        result = await attachments.upload_attachment(make_ctx(client), **text_upload(item_type="ticket"))
    assert result["url"] == URL


# --------------------------------------------------------------------------
# The size cap
# --------------------------------------------------------------------------


async def test_over_ten_megabytes_is_refused_locally(server, mock_gorelo):
    assert attachments.MAX_UPLOAD_BYTES == 10 * MIB
    raw = b"\x00" * (10 * MIB + 1)
    text = await refused(
        server, mock_gorelo,
        {"item_type": "ticket", "item_id": ITEM, "filename": "big.bin", "content_base64": base64.b64encode(raw).decode("ascii")},
        "content_base64: the decoded file (10485761 bytes) is larger than the 10 MB (10485760 bytes) this tool accepts",
        "Gorelo allows more",
    )
    assert len(text) < 400  # the payload is not echoed


async def test_a_huge_base64_text_is_refused_before_it_is_decoded(server, mock_gorelo):
    await refused(
        server, mock_gorelo,
        {"item_type": "ticket", "item_id": ITEM, "filename": "big.bin", "content_base64": "A" * (14 * MIB + 8)},
        "content_base64: the decoded file is larger than the 10 MB (10485760 bytes) this tool accepts",
    )


async def test_text_over_ten_megabytes_is_refused_locally(server, mock_gorelo):
    await refused(
        server, mock_gorelo, text_upload(content_text="x" * (10 * MIB + 1)),
        "content_text: the decoded file is larger than the 10 MB (10485760 bytes) this tool accepts",
    )
    # multi-byte characters count by their UTF-8 size, not by their number
    await refused(
        server, mock_gorelo, text_upload(content_text="\u20ac" * (4 * MIB)),
        "content_text: the decoded file (12582912 bytes) is larger than the 10 MB (10485760 bytes) this tool accepts",
    )


def test_exactly_ten_megabytes_is_accepted_and_one_more_byte_is_not():
    cap = attachments.MAX_UPLOAD_BYTES
    assert len(attachments._file_bytes(None, "x" * cap)) == cap
    assert len(attachments._file_bytes(base64.b64encode(b"y" * cap).decode("ascii"), None)) == cap
    for over_base64, over_text in ((base64.b64encode(b"y" * (cap + 1)).decode("ascii"), None), (None, "x" * (cap + 1))):
        with pytest.raises(ValueError, match="larger than the 10 MB \\(10485760 bytes\\)"):
            attachments._file_bytes(over_base64, over_text)


@pytest.mark.parametrize("encoding", ["text", "base64"])
async def test_the_cap_boundary_through_the_tool(server, mock_gorelo, monkeypatch, encoding):
    monkeypatch.setattr(attachments, "MAX_UPLOAD_BYTES", 64)
    mock_gorelo.on("POST", UPLOAD, envelope(stored()))

    def arguments(size):
        raw = b"z" * size
        if encoding == "text":
            return text_upload(content_text=raw.decode("ascii"))
        return {"item_type": "ticket", "item_id": ITEM, "filename": "notes.txt",
                "content_base64": base64.b64encode(raw).decode("ascii")}

    await call_tool(server, "upload_attachment", arguments(64))
    assert mock_gorelo.last.files["file"][1] == b"z" * 64
    sent = len(mock_gorelo.requests)
    text = await call_tool_error(server, "upload_attachment", arguments(65))
    assert "larger than the 64 bytes this tool accepts" in text
    assert len(mock_gorelo.requests) == sent


def test_a_lone_surrogate_cannot_be_written_as_utf8_and_says_so():
    with pytest.raises(ValueError, match="content_text: contains a character that cannot be written as UTF-8"):
        attachments._file_bytes(None, "ok \ud800 broken")


# --------------------------------------------------------------------------
# Gorelo errors and unexpected answers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "property_name, mapped",
    [
        ("itemId", "item_id"),
        ("ItemId", "item_id"),
        ("itemType", "item_type"),
        ("ItemType", "item_type"),
        ("file", "filename or content_base64 or content_text"),
    ],
)
async def test_a_gorelo_error_names_the_snake_case_param(server, mock_gorelo, property_name, mapped):
    mock_gorelo.on("POST", UPLOAD, error_envelope(400, [("070101", "That value is not valid.", property_name)]))
    text = await call_tool_error(server, "upload_attachment", text_upload())
    assert text == (
        f"Gorelo rejected upload_attachment (HTTP 400, code 070101): {mapped}: That value is not valid. "
        f"[trace {TEST_TRACE_ID}]"
    )
    assert len(mock_gorelo.requests) == 1


async def test_an_unknown_record_is_a_gorelo_404_and_nothing_is_stored(server, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, error_envelope(404, [("070401", "Ticket not found.", "itemId")]))
    text = await call_tool_error(server, "upload_attachment", text_upload())
    assert text.startswith("Gorelo rejected upload_attachment (HTTP 404, code 070401): item_id: Ticket not found.")


async def test_every_notification_is_shown(server, mock_gorelo):
    mock_gorelo.on(
        "POST", UPLOAD,
        error_envelope(400, [("070101", "Unknown type.", "itemType"), ("070101", "File is empty.", "file")]),
    )
    text = await call_tool_error(server, "upload_attachment", text_upload())
    assert "item_type: Unknown type." in text and "filename or content_base64 or content_text: File is empty." in text


def assert_upload_advice(text, how=None):
    """ the file may already be stored, the API cannot list or delete it, so do not upload it again."""
    assert text.startswith("Gorelo did not confirm upload_attachment (")
    if how is not None:
        assert text.startswith(f"Gorelo did not confirm upload_attachment ({how}). "), text
    assert "The file may already be stored." in text
    assert (
        "Uploaded files cannot be listed or deleted through the API, so do not upload it again without asking "
        "the user first."
    ) in text
    # the generic advice is impossible here (no read can find an uploaded file), so it must not be given
    assert "Verify with a read before retrying" not in text and "verify it with a read before repeating it" not in text
    assert "retrying is safe" not in text and "may or may not have been applied" not in text


@pytest.mark.parametrize(
    "answer, found",
    [
        (envelope(None), "its answer could not be used"),
        (envelope({}), "its answer could not be used"),
        (envelope(True), "its answer could not be used"),
        (envelope(False), "its answer could not be used"),
        (envelope("stored"), "its answer could not be used"),
        (envelope([stored()]), "its answer could not be used"),
    ],
)
async def test_an_answer_that_is_not_an_object_is_reported_as_unconfirmed(server, mock_gorelo, answer, found):
    # the write succeeded as far as Gorelo says, but nothing usable came back: never a result, and never the
    # generic "verify with a read" (the file cannot be listed, so a read cannot find it)
    mock_gorelo.on("POST", UPLOAD, answer)
    text = await call_tool_error(server, "upload_attachment", text_upload())
    assert_upload_advice(text, found)
    assert len(mock_gorelo.requests) == 1


@pytest.mark.parametrize(
    "answer, missing",
    [
        (envelope({"Name": "a.txt"}), "Url"),
        (envelope({"Url": URL}), "Name"),
        (envelope({"Name": "", "Url": URL}), "Name"),
        (envelope({"Name": "a.txt", "Url": ""}), "Url"),
        (envelope({"Name": 7, "Url": URL}), "Name"),
        (envelope({"Name": "a.txt", "Url": None}), "Url"),
        (envelope({"Name": "   ", "Url": URL}), "Name"),
        (envelope({"Id": 5}), "Name and Url"),
        (envelope({"Name": "  ", "Url": ""}), "Name and Url"),
    ],
)
async def test_an_answer_without_a_usable_name_and_url_is_reported_as_unconfirmed(server, mock_gorelo, answer, missing):
    mock_gorelo.on("POST", UPLOAD, answer)
    text = await call_tool_error(server, "upload_attachment", text_upload())
    assert_upload_advice(text, f"Gorelo answered success but its answer has no usable {missing}")
    assert "[trace" not in text  # a success carries no trace id
    assert len(mock_gorelo.requests) == 1


async def test_a_timeout_is_unconfirmed_and_is_not_retried(server, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "upload_attachment", text_upload())
    assert_upload_advice(text, "the request timed out")
    assert "[trace" not in text  # a timeout has no answer, so no trace id
    assert len(mock_gorelo.requests) == 1


async def test_a_connection_failure_is_unconfirmed_and_is_not_retried(server, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, httpx.ConnectError("refused"))
    text = await call_tool_error(server, "upload_attachment", text_upload())
    assert_upload_advice(text, "the connection failed")
    assert len(mock_gorelo.requests) == 1


async def test_a_gateway_error_is_unconfirmed_and_is_not_retried(server, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, httpx.Response(502, text="<html>Bad Gateway</html>"))
    text = await call_tool_error(server, "upload_attachment", text_upload())
    assert_upload_advice(text, "Gorelo answered HTTP 502")
    assert "unexpected response" not in text and "[trace" not in text
    assert len(mock_gorelo.requests) == 1


async def test_a_5xx_envelope_gives_gorelos_message_the_upload_advice_and_the_trace_id(server, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, error_envelope(500, [("070500", "Storage is down.")]))
    text = await call_tool_error(server, "upload_attachment", text_upload())
    assert_upload_advice(text, "Gorelo answered HTTP 500: Storage is down.")
    assert text.endswith(f" [trace {TEST_TRACE_ID}]")
    assert len(mock_gorelo.requests) == 1


async def test_a_200_that_is_not_an_envelope_is_unconfirmed_too(server, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"}))
    text = await call_tool_error(server, "upload_attachment", text_upload())
    assert_upload_advice(text, "its answer could not be used")
    assert len(mock_gorelo.requests) == 1


async def test_a_4xx_refusal_is_not_an_unconfirmed_write_and_keeps_the_normal_text(server, mock_gorelo):
    # nothing was stored, so "the file may already be stored" would be wrong
    mock_gorelo.on("POST", UPLOAD, error_envelope(400, [("070101", "That value is not valid.", "itemId")]))
    text = await call_tool_error(server, "upload_attachment", text_upload())
    assert text == (
        f"Gorelo rejected upload_attachment (HTTP 400, code 070101): item_id: That value is not valid. "
        f"[trace {TEST_TRACE_ID}]"
    )
    assert "may already be stored" not in text


async def test_a_rate_limit_that_persists_is_reported_as_not_processed(server, mock_gorelo):
    mock_gorelo.on("POST", UPLOAD, httpx.Response(429, headers={"Retry-After": "100000"}, json={"error": "slow"}))
    text = await call_tool_error(server, "upload_attachment", text_upload())
    assert "rate limiting" in text and "did not process this request" in text
    assert len(mock_gorelo.requests) == 1
