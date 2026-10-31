"""tools/documents.py: the structured document reads (toolset "documents", all read only):
list_structured_documents, get_structured_document, list_structured_document_submissions and
get_structured_document_submission. Offline only (MockGorelo and an in-process client).

Each tool gets the exact method, path, query names and result shape, a Gorelo error mapped to the snake_case parameter,
every local refusal with zero HTTP calls, and checks on the texts: answer text is data, and a file link opens without a
login until it expires, so the text says who should get it.
"""

import json
import logging
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
    pagination,
    paged_envelope,
    paged_responder,
)
from fastmcp.exceptions import ToolError

from gorelo_client import is_side_effect_get
from tools import documents as documents_module
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio

EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)
LATEST_SPEC = Path(__file__).resolve().parent.parent / "backups" / "swagger-latest.json"
needs_spec_text = pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")

LIST = "/v1/documents/structured"
SD = "Qk7t2a"
SUB = "Xy9p4m"
VERSION_ID = "Vm4k8c"
SD_PATH = f"{LIST}/{SD}"
SUBS_PATH = f"{SD_PATH}/submissions"
SUB_PATH = f"{SUBS_PATH}/{SUB}"
CLIENT = 9102
IMAGE_URL = "https://files.example.test/submissions/1/photo.png?sig=READ-TOKEN"
FILE_URL = "https://files.example.test/submissions/1/plan.pdf?sig=READ-TOKEN-2"

NAMES = (
    "list_structured_documents",
    "get_structured_document",
    "list_structured_document_submissions",
    "get_structured_document_submission",
)
PAGED = ("list_structured_documents", "list_structured_document_submissions")
OPS = {
    "list_structured_documents": ["GET /v1/documents/structured"],
    "get_structured_document": ["GET /v1/documents/structured/{structuredDocumentId}"],
    "list_structured_document_submissions": ["GET /v1/documents/structured/{structuredDocumentId}/submissions"],
    "get_structured_document_submission": [
        "GET /v1/documents/structured/{structuredDocumentId}/submissions/{submissionId}"
    ],
}


@pytest.fixture(autouse=True)
def _quiet_fastmcp_tracebacks():
    """FastMCP logs every tool error with a Rich traceback. These tests read the error text the model would see."""
    logger = logging.getLogger("fastmcp")
    level = logger.level
    logger.setLevel(logging.CRITICAL)
    yield
    logger.setLevel(level)


@pytest.fixture
def server(server_factory):
    """Every toolset on (the documents toolset is among them)."""
    return server_factory(destructive=True)


def trace(text):
    return f"{text} [trace {TEST_TRACE_ID}]"


def flat(text):
    return " ".join((text or "").split())


async def call_directly(client_factory, tool, **kwargs):
    """Call the decorated function itself (no pydantic in front of it): for the checks the schema normally pre-empts."""
    async with client_factory() as client:
        return await tool(make_ctx(client), **kwargs)


def structured_row(sd_id=SD, name="Onboarding questionnaire", version=2, **extra):
    """A StructuredDocumentListItemModel: the current version, no fields."""
    record = {
        "Id": sd_id,
        "Name": name,
        "Version": version,
        "VersionId": VERSION_ID,
        "CreatedOn": "2026-09-01T08:00:00Z",
        "UpdatedOn": "2026-10-02T09:30:00Z",
    }
    record.update(extra)
    return record


def structured_field(field_id="f1", kind="Question_Short_Answer", label="<p>Site name</p>", **extra):
    """A StructuredDocumentFieldModel."""
    field = {
        "Id": field_id,
        "Type": kind,
        "Label": label,
        "Required": True,
        "ShowInList": True,
        "UseForTitle": True,
        "Options": None,
        "ConditionalLogic": {"Operator": "and", "When": [], "Then": []},
    }
    field.update(extra)
    return field


def structured_detail(sd_id=SD, **extra):
    """A StructuredDocumentDetailModel: the row plus its Fields."""
    record = structured_row(sd_id)
    record["Fields"] = [
        structured_field(),
        structured_field("f2", "Heading_1", "<h1>Access</h1>", Required=False, UseForTitle=False),
    ]
    record.update(extra)
    return record


def answer(field_id="f1", kind="Short_Answer", label="<p>Site name</p>", **extra):
    """A StructuredDocumentAnswerModel: every value null unless the answer type fills it."""
    row = {
        "FieldId": field_id,
        "Label": label,
        "Type": kind,
        "TextValue": "Head office",
        "NumberValue": None,
        "OptionValues": None,
        "ImageUrls": None,
        "AgentAssetIds": None,
        "CustomAssetIds": None,
        "ContactIds": None,
        "DocumentIds": None,
        "StructuredDocumentSubmissions": None,
        "UptimeIds": None,
    }
    row.update(extra)
    return row


def submission_row(sub_id=SUB, client_id=CLIENT, title="Head office", **extra):
    """A StructuredDocumentSubmissionListItemModel."""
    row = {
        "Id": sub_id,
        "Title": title,
        "VersionId": "Vs5u1q",
        "Version": 1,
        "StructuredDocumentId": SD,
        "ClientId": client_id,
        "Answers": [answer()],
        "CreatedOn": "2026-10-03T10:00:00Z",
        "CreatedById": 9201,
        "UpdatedOn": "2026-10-03T10:00:00Z",
    }
    row.update(extra)
    return row


def submission_detail(sub_id=SUB, **extra):
    """A StructuredDocumentSubmissionDetailModel: the row plus its Attachments."""
    row = submission_row(sub_id)
    row["Attachments"] = [{"Name": "plan.pdf", "Url": FILE_URL}]
    row.update(extra)
    return row


# --------------------------------------------------------------------------
# Declarations
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_each_tool_is_declared_as_a_documents_read_tool_over_its_ops(name, spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == name)
    assert (spec.toolset, spec.kind, spec.ops, spec.destructive_hint) == ("documents", "read", OPS[name], False)
    assert all(op in spec_index.ops for op in spec.ops)
    assert not any(is_side_effect_get(op) for op in spec.ops)


async def test_the_four_tools_take_exactly_their_parameters(server):
    tools = {tool.name: tool for tool in await list_tools(server)}
    assert set(tools["list_structured_documents"].inputSchema["properties"]) == {
        "query", "sort_by", "sort_order", "page_size", "cursor",
    }
    assert set(tools["list_structured_document_submissions"].inputSchema["properties"]) == {
        "structured_document_id", "client_ids", "sort_by", "sort_order", "page_size", "cursor",
    }
    assert set(tools["get_structured_document"].inputSchema["properties"]) == {"structured_document_id"}
    assert set(tools["get_structured_document_submission"].inputSchema["properties"]) == {
        "structured_document_id", "submission_id",
    }
    for name in NAMES:
        assert (tools[name].annotations.readOnlyHint, tools[name].annotations.destructiveHint) == (True, False), name


async def test_the_sort_enums_keep_the_member_order_the_other_list_tools_pin(server):
    tools = {tool.name: tool for tool in await list_tools(server)}
    for name in ("list_structured_documents", "list_structured_document_submissions"):
        properties = tools[name].inputSchema["properties"]
        assert properties["sort_by"]["enum"] == ["updatedOn", "createdOn"], name
        assert properties["sort_order"]["enum"] == ["asc", "desc"], name


def test_the_module_declares_the_structured_reads_and_no_structured_write():
    names = {spec.name for spec in REGISTRY.specs if spec.fn.__module__ == documents_module.__name__}
    structured = {name for name in names if "structured" in name}
    assert structured == set(NAMES)
    assert all(spec.kind == "read" for spec in REGISTRY.specs if spec.name in NAMES)


# --------------------------------------------------------------------------
# The rows the fixtures serve are the spec's schemas
# --------------------------------------------------------------------------


def test_the_fixtures_are_shaped_like_the_spec(spec_index):
    def fields(name):
        return set(spec_index.schema(name)["fields"])

    assert set(structured_row()) == fields("StructuredDocumentListItemModel")
    assert set(structured_detail()) == fields("StructuredDocumentDetailModel")
    assert set(structured_field()) == fields("StructuredDocumentFieldModel")
    assert set(answer()) == fields("StructuredDocumentAnswerModel")
    assert set(submission_row()) == fields("StructuredDocumentSubmissionListItemModel")
    assert set(submission_detail()) == fields("StructuredDocumentSubmissionDetailModel")
    assert set(submission_detail()["Attachments"][0]) == fields("CommentAttachmentModel")


# --------------------------------------------------------------------------
# list_structured_documents
# --------------------------------------------------------------------------


async def test_list_structured_documents_makes_a_plain_paged_get(server, mock_gorelo):
    rows = [structured_row(), structured_row("Aa1Bb2", "Equipment")]
    mock_gorelo.on("GET", LIST, paged_envelope(rows, total_count=2))
    result = await call_tool(server, "list_structured_documents")
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", LIST, {"PageSize": "50"}, b"")
    assert len(mock_gorelo.requests) == 1
    assert result == {
        "items": rows,
        "count": 2,
        "total_count": 2,
        "has_more": False,
        "next_cursor": None,
        "page_size": 50,
        "filters": {},
    }


async def test_every_filter_goes_out_under_its_spec_name(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, paged_envelope([structured_row()]))
    result = await call_tool(
        server,
        "list_structured_documents",
        {"query": "onboard", "sort_by": "updatedOn", "sort_order": "asc", "page_size": 25},
    )
    assert mock_gorelo.last.query == {"Query": "onboard", "SortBy": "updatedOn", "SortOrder": "asc", "PageSize": "25"}
    assert result["filters"] == {"query": "onboard", "sort_by": "updatedOn", "sort_order": "asc"}
    assert result["page_size"] == 25


async def test_paging_follows_next_cursor_with_the_same_filters(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, paged_responder([[structured_row()], [structured_row("Aa1Bb2")]], total_count=2))
    args = {"sort_by": "createdOn", "page_size": 1}
    one = await call_tool(server, "list_structured_documents", args)
    assert (one["has_more"], one["next_cursor"], one["total_count"]) == (True, "c1", 2)
    two = await call_tool(server, "list_structured_documents", {**args, "cursor": one["next_cursor"]})
    assert (two["has_more"], two["next_cursor"]) == (False, None)
    assert [r.query for r in mock_gorelo.requests] == [
        {"SortBy": "createdOn", "PageSize": "1"},
        {"SortBy": "createdOn", "PageSize": "1", "Cursor": "c1"},
    ]
    assert one["filters"] == two["filters"] == {"sort_by": "createdOn"}


@pytest.mark.parametrize("given, used", [(0, 1), (-3, 1), (1, 1), (200, 200), (999, 200)])
async def test_page_size_is_clamped_and_reported(server, mock_gorelo, given, used):
    mock_gorelo.on("GET", LIST, paged_envelope([]))
    result = await call_tool(server, "list_structured_documents", {"page_size": given})
    assert mock_gorelo.last.query["PageSize"] == str(used) and result["page_size"] == used


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({"cursor": ""}, "cursor: must not be empty or whitespace only"),
        ({"cursor": "  "}, "cursor: must not be empty or whitespace only"),
        ({"query": ""}, "query: must not be empty or whitespace only"),
        ({"query": "x" * 201}, "query: at most 200 characters, got 201"),
        ({"sort_by": "name"}, "sort_by"),
        ({"sort_order": "up"}, "sort_order"),
    ],
)
async def test_list_structured_documents_refuses_a_bad_argument_before_any_http_call(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "list_structured_documents", args)
    assert fragment in text
    assert mock_gorelo.requests == []


async def test_calling_list_structured_documents_directly_refuses_what_the_schema_would_stop(client_factory, mock_gorelo):
    with pytest.raises(ToolError, match="sort_by: must be one of 'updatedOn', 'createdOn', got a string"):
        await call_directly(client_factory, documents_module.list_structured_documents, sort_by="name")
    with pytest.raises(ToolError, match="sort_order: must be one of 'asc', 'desc', got a string"):
        await call_directly(client_factory, documents_module.list_structured_documents, sort_order="up")
    with pytest.raises(ToolError, match="query: expected text, got a number"):
        await call_directly(client_factory, documents_module.list_structured_documents, query=5)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    ("property_name", "param"),
    [("SortBy", "sort_by"), ("SortOrder", "sort_order"), ("Query", "query"), ("PageSize", "page_size"), ("Cursor", "cursor")],
)
async def test_a_gorelo_400_names_the_snake_case_param(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", LIST, error_envelope(400, [("070101", "Not a valid value.", property_name)]))
    text = await call_tool_error(server, "list_structured_documents")
    assert text == trace(f"Gorelo rejected list_structured_documents (HTTP 400, code 070101): {param}: Not a valid value.")


async def test_list_structured_documents_refuses_an_answer_that_is_not_a_list(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, envelope({"Items": []}, pagination(None, 0, has_more=False)))
    text = await call_tool_error(server, "list_structured_documents")
    assert text.startswith("Gorelo returned an unexpected response for list_structured_documents")
    assert "expected Data to be a list" in text


async def test_list_structured_documents_names_a_missing_documents_scope(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, error_envelope(403, [("080203", "API key does not have 'Documents' scope")]))
    text = await call_tool_error(server, "list_structured_documents")
    assert "the API key does not have the 'Documents' scope" in text


async def test_list_structured_documents_on_a_timeout_says_retrying_is_safe(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "list_structured_documents")
    assert text.startswith("Gorelo did not answer list_structured_documents") and "retrying is safe" in text


# --------------------------------------------------------------------------
# get_structured_document
# --------------------------------------------------------------------------


async def test_get_structured_document_reads_one_record_unchanged(server, mock_gorelo):
    record = structured_detail()
    mock_gorelo.on("GET", SD_PATH, envelope(record))
    result = await call_tool(server, "get_structured_document", {"structured_document_id": SD})
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", SD_PATH, {}, b"")
    assert len(mock_gorelo.requests) == 1
    assert result == record


async def test_get_structured_document_takes_a_version_id_as_the_path(server, mock_gorelo):
    mock_gorelo.on("GET", f"{LIST}/{VERSION_ID}", envelope(structured_detail()))
    await call_tool(server, "get_structured_document", {"structured_document_id": VERSION_ID})
    assert mock_gorelo.last.path == f"{LIST}/{VERSION_ID}"


async def test_get_structured_document_maps_a_404_to_the_snake_case_param(server, mock_gorelo):
    mock_gorelo.on(
        "GET", SD_PATH, error_envelope(404, [("070404", "Structured document not found.", "structuredDocumentId")])
    )
    text = await call_tool_error(server, "get_structured_document", {"structured_document_id": SD})
    assert text == trace(
        "Gorelo rejected get_structured_document (HTTP 404, code 070404): structured_document_id: Structured document not found."
    )


@pytest.mark.parametrize("bad", ["", "../other", "a b", "x" * 65, "Qk7t2a?x=1", "Qk7t2\u00e9"])
async def test_a_structured_id_that_is_not_a_path_token_is_refused_before_any_http_call(server, mock_gorelo, bad):
    text = await call_tool_error(server, "get_structured_document", {"structured_document_id": bad})
    assert text.startswith("structured_document_id: expected an Id of 1 to 64 letters, digits, '_' or '-'")
    assert mock_gorelo.requests == []


async def test_calling_get_structured_document_directly_refuses_a_number(client_factory, mock_gorelo):
    with pytest.raises(ToolError, match="structured_document_id: expected an Id of 1 to 64 letters"):
        await call_directly(client_factory, documents_module.get_structured_document, structured_document_id=5)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("data", [None, [], "doc"])
async def test_get_structured_document_refuses_an_answer_that_is_not_one_object(server, mock_gorelo, data):
    mock_gorelo.on("GET", SD_PATH, envelope(data))
    text = await call_tool_error(server, "get_structured_document", {"structured_document_id": SD})
    assert text.startswith("Gorelo returned an unexpected response for get_structured_document")
    assert "refusing to guess" in text


# --------------------------------------------------------------------------
# list_structured_document_submissions
# --------------------------------------------------------------------------


async def test_list_submissions_makes_a_paged_get_under_the_document(server, mock_gorelo):
    rows = [submission_row()]
    mock_gorelo.on("GET", SUBS_PATH, paged_envelope(rows, total_count=1))
    result = await call_tool(server, "list_structured_document_submissions", {"structured_document_id": SD})
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", SUBS_PATH, {"PageSize": "50"}, b"")
    assert result == {
        "items": rows,
        "count": 1,
        "total_count": 1,
        "has_more": False,
        "next_cursor": None,
        "page_size": 50,
        "filters": {"structured_document_id": SD},
    }


async def test_every_submission_filter_goes_out_under_its_spec_name(server, mock_gorelo):
    mock_gorelo.on("GET", SUBS_PATH, paged_envelope([submission_row()]))
    result = await call_tool(
        server,
        "list_structured_document_submissions",
        {"structured_document_id": SD, "client_ids": [9102, 9103], "sort_by": "updatedOn", "sort_order": "desc", "page_size": 10},
    )
    assert mock_gorelo.last.query == {"ClientIds": "9102,9103", "SortBy": "updatedOn", "SortOrder": "desc", "PageSize": "10"}
    assert result["filters"] == {
        "structured_document_id": SD,
        "client_ids": [9102, 9103],
        "sort_by": "updatedOn",
        "sort_order": "desc",
    }


async def test_submission_paging_keeps_the_same_filters(server, mock_gorelo):
    mock_gorelo.on("GET", SUBS_PATH, paged_responder([[submission_row()], [submission_row("Mm3Nn4")]], total_count=2))
    args = {"structured_document_id": SD, "client_ids": [CLIENT]}
    one = await call_tool(server, "list_structured_document_submissions", args)
    two = await call_tool(server, "list_structured_document_submissions", {**args, "cursor": one["next_cursor"]})
    assert (one["has_more"], two["has_more"]) == (True, False)
    assert [r.query for r in mock_gorelo.requests] == [
        {"ClientIds": str(CLIENT), "PageSize": "50"},
        {"ClientIds": str(CLIENT), "PageSize": "50", "Cursor": "c1"},
    ]


async def test_the_rows_come_back_as_gorelo_wrote_them_with_their_links(server, mock_gorelo):
    row = submission_row(Answers=[answer("f9", "Image_Carousel", "<p>Photos</p>", ImageUrls=[IMAGE_URL])])
    mock_gorelo.on("GET", SUBS_PATH, paged_envelope([row]))
    result = await call_tool(server, "list_structured_document_submissions", {"structured_document_id": SD})
    assert result["items"] == [row]
    assert result["items"][0]["Answers"][0]["ImageUrls"] == [IMAGE_URL]


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({"client_ids": []}, "client_ids: expected at least one id, got an empty list"),
        ({"client_ids": [0]}, "client_ids[0]: expected a positive whole number such as 123, got zero or a negative number"),
        ({"client_ids": list(range(1, 102))}, "client_ids: Gorelo accepts at most 100 ids in one filter, got 101"),
        ({"cursor": ""}, "cursor: must not be empty or whitespace only"),
        ({"sort_by": "name"}, "sort_by"),
        ({"structured_document_id": "../x"}, "structured_document_id: expected an Id of 1 to 64 letters"),
    ],
)
async def test_list_submissions_refuses_a_bad_argument_before_any_http_call(server, mock_gorelo, args, fragment):
    text = await call_tool_error(server, "list_structured_document_submissions", {"structured_document_id": SD, **args})
    assert fragment in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    ("property_name", "param"),
    [("ClientIds", "client_ids"), ("SortBy", "sort_by"), ("SortOrder", "sort_order"), ("PageSize", "page_size")],
)
async def test_a_gorelo_400_on_a_submission_filter_names_the_snake_case_param(server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", SUBS_PATH, error_envelope(400, [("070101", "Not a valid value.", property_name)]))
    text = await call_tool_error(server, "list_structured_document_submissions", {"structured_document_id": SD})
    assert text == trace(f"Gorelo rejected list_structured_document_submissions (HTTP 400, code 070101): {param}: Not a valid value.")


async def test_a_gorelo_404_on_the_list_names_the_structured_document_param(server, mock_gorelo):
    mock_gorelo.on(
        "GET", SUBS_PATH, error_envelope(404, [("070404", "Structured document not found.", "structuredDocumentId")])
    )
    text = await call_tool_error(server, "list_structured_document_submissions", {"structured_document_id": SD})
    assert text == trace(
        "Gorelo rejected list_structured_document_submissions (HTTP 404, code 070404): "
        "structured_document_id: Structured document not found."
    )


async def test_list_submissions_refuses_an_answer_that_is_not_a_list(server, mock_gorelo):
    mock_gorelo.on("GET", SUBS_PATH, envelope(submission_row(), pagination(None, 1, has_more=False)))
    text = await call_tool_error(server, "list_structured_document_submissions", {"structured_document_id": SD})
    assert text.startswith("Gorelo returned an unexpected response for list_structured_document_submissions")
    assert "expected Data to be a list" in text


# --------------------------------------------------------------------------
# get_structured_document_submission
# --------------------------------------------------------------------------


async def test_get_submission_reads_one_record_unchanged(server, mock_gorelo):
    record = submission_detail()
    mock_gorelo.on("GET", SUB_PATH, envelope(record))
    result = await call_tool(
        server, "get_structured_document_submission", {"structured_document_id": SD, "submission_id": SUB}
    )
    request = mock_gorelo.last
    assert (request.method, request.path, request.query, request.content) == ("GET", SUB_PATH, {}, b"")
    assert len(mock_gorelo.requests) == 1
    assert result == record


async def test_get_submission_takes_a_version_id(server, mock_gorelo):
    mock_gorelo.on("GET", f"{SUBS_PATH}/{VERSION_ID}", envelope(submission_detail()))
    await call_tool(
        server, "get_structured_document_submission", {"structured_document_id": SD, "submission_id": VERSION_ID}
    )
    assert mock_gorelo.last.path == f"{SUBS_PATH}/{VERSION_ID}"


async def test_get_submission_maps_a_404_to_the_snake_case_params(server, mock_gorelo):
    mock_gorelo.on("GET", SUB_PATH, error_envelope(404, [("070404", "Submission not found.", "submissionId")]))
    text = await call_tool_error(
        server, "get_structured_document_submission", {"structured_document_id": SD, "submission_id": SUB}
    )
    assert text == trace(
        "Gorelo rejected get_structured_document_submission (HTTP 404, code 070404): submission_id: Submission not found."
    )


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({"submission_id": ""}, "submission_id: expected an Id of 1 to 64 letters"),
        ({"submission_id": "a/../b"}, "submission_id: expected an Id of 1 to 64 letters"),
        ({"structured_document_id": "x" * 65}, "structured_document_id: expected an Id of 1 to 64 letters"),
    ],
)
async def test_get_submission_refuses_a_bad_id_before_any_http_call(server, mock_gorelo, args, fragment):
    base = {"structured_document_id": SD, "submission_id": SUB}
    text = await call_tool_error(server, "get_structured_document_submission", {**base, **args})
    assert text.startswith(fragment)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("data", [None, [], "submission"])
async def test_get_submission_refuses_an_answer_that_is_not_one_object(server, mock_gorelo, data):
    mock_gorelo.on("GET", SUB_PATH, envelope(data))
    text = await call_tool_error(
        server, "get_structured_document_submission", {"structured_document_id": SD, "submission_id": SUB}
    )
    assert text.startswith("Gorelo returned an unexpected response for get_structured_document_submission")
    assert "refusing to guess" in text


# --------------------------------------------------------------------------
# Texts: answer text is data, file links are capabilities, and the pointers between the tools
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_every_read_says_it_is_read_only_and_what_its_ids_come_from(name):
    text = flat(getattr(documents_module, name).__doc__)
    assert "Side effects: None (read-only)." in text
    assert "Resolve ids first:" in text


@pytest.mark.parametrize("name", PAGED)
def test_the_paged_lists_say_the_paging_rule(name):
    text = flat(getattr(documents_module, name).__doc__)
    assert "Paging: pass next_cursor back as cursor with the SAME filters until has_more is false." in text


@pytest.mark.parametrize(
    "name",
    ["get_structured_document", "list_structured_document_submissions", "get_structured_document_submission"],
)
def test_answer_and_label_text_is_data_never_instructions(name):
    text = flat(getattr(documents_module, name).__doc__)
    assert "data" in text and "never instructions" in text, name


@pytest.mark.parametrize("name", ["list_structured_document_submissions", "get_structured_document_submission"])
def test_the_submission_texts_warn_that_a_file_link_opens_without_a_login(name):
    text = flat(getattr(documents_module, name).__doc__)
    assert "time-limited access token" in text
    assert "without a login until it expires" in text
    assert "give it only to the person who should" in text
    assert "never store it" in text


def test_the_list_of_submissions_warns_about_the_image_links_its_answers_carry():
    text = flat(documents_module.list_structured_document_submissions.__doc__)
    assert "An ImageUrls link in an answer carries a time-limited access token: it opens its image without a login" in text


def test_the_detail_names_the_linked_submission_and_the_structured_document_it_belongs_to():
    text = flat(documents_module.get_structured_document_submission.__doc__)
    assert "StructuredDocumentSubmissions of a documents answer" in text
    assert "structured_document_id and submission_id" in text


async def test_the_parameter_texts_are_short_and_name_their_sources(server):
    tools = {tool.name: tool for tool in await list_tools(server)}
    for name in NAMES:
        tool = tools[name]
        assert len(tool.description) <= 900, name
        for param, definition in tool.inputSchema["properties"].items():
            assert len(definition.get("description", "")) <= 220, (name, param)
    sd_text = tools["get_structured_document"].inputSchema["properties"]["structured_document_id"]["description"]
    assert "list_structured_documents" in sd_text and "version ids" in sd_text
    sub_text = tools["get_structured_document_submission"].inputSchema["properties"]["submission_id"]["description"]
    assert "list_structured_document_submissions" in sub_text
    cursor = tools["list_structured_documents"].inputSchema["properties"]["cursor"]["description"]
    assert "next_cursor" in cursor and "same filters" in cursor and "until has_more is false" in cursor


def test_the_structured_document_code_has_no_em_or_en_dashes():
    source = Path(documents_module.__file__).read_text(encoding="utf-8")
    assert EM_DASH not in source and EN_DASH not in source
    own = Path(__file__).read_text(encoding="utf-8")
    assert EM_DASH not in own and EN_DASH not in own


# --------------------------------------------------------------------------
# The spec texts these rules rest on
# --------------------------------------------------------------------------


def operation(method, path):
    return json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["paths"][path][method]


def param_text(method, path, name):
    return " ".join(next(p["description"] for p in operation(method, path)["parameters"] if p["name"] == name).split())


def raw_schema_text(name, prop):
    raw = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["components"]["schemas"]
    return " ".join(raw[name]["properties"][prop]["description"].split())


@needs_spec_text
def test_the_list_and_read_texts_are_the_spec_texts():
    listing = " ".join(operation("get", LIST)["description"].split())
    assert "One row per structured document, showing its current version; a deleted one is never listed." in listing
    assert param_text("get", LIST, "SortBy") == "Sort field: `createdOn` (default) or `updatedOn`."
    assert param_text("get", LIST, "SortOrder") == "Sort direction: `asc` or `desc` (default)."
    submissions = " ".join(operation("get", f"{LIST}/{{structuredDocumentId}}/submissions")["description"].split())
    assert "One row per submission, showing its current version and answers; a deleted one is never listed." in submissions
    detail = " ".join(operation("get", f"{LIST}/{{structuredDocumentId}}/submissions/{{submissionId}}")["description"].split())
    assert "Takes the submission's `Id`, or any of its version ids" in detail


@needs_spec_text
def test_the_file_links_carry_a_time_limited_token_the_texts_rely_on():
    assert raw_schema_text("StructuredDocumentSubmissionDetailModel", "Attachments") == (
        "Files attached to the submission. Each url carries a time-limited access token; do not store it."
    )
    assert "Each url carries a time-limited access token; do not store it." in raw_schema_text(
        "StructuredDocumentAnswerModel", "ImageUrls"
    )


def test_the_submission_link_model_holds_only_ids_so_nothing_in_it_opens_a_page(spec_index):
    # StructuredDocumentSubmissionLinkModel (a documents answer's pick of another submission) has no url: its two ids
    # are read with get_structured_document_submission, which needs the API key, so it gets no "give it only to" text.
    assert set(spec_index.schema("StructuredDocumentSubmissionLinkModel")["fields"]) == {"StructuredDocumentId", "SubmissionId"}
