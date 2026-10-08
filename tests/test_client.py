"""gorelo_client.py: every behaviour of the one place that talks to Gorelo (offline, MockTransport)."""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import (
    TEST_API_KEY,
    TEST_TRACE_ID,
    UnexpectedRequest,
    envelope,
    error_envelope,
    in_order,
    notification,
    paged_envelope,
    paged_responder,
    pagination,
    path_params_for,
    uid,
)

import gorelo_client
from gorelo_client import (
    CURRENT_TOOL,
    EXPORT_NOTE,
    FORBIDDEN_OPS,
    PAGE_SIZE_MAX,
    PAGE_SIZE_MIN,
    SIDE_EFFECT_GETS,
    AllResult,
    BinaryResult,
    GoreloAPIError,
    GoreloClient,
    Page,
    is_forbidden_op,
    is_side_effect_get,
    normalize_op_key,
)
from spec import SpecIndex, SpecViolation

pytestmark = pytest.mark.anyio

SECRET = "SECRET-VALUE-123"
CLIENT_OP = "GET /v1/clients/{clientId}"
CREATE_CLIENT = "POST /v1/clients"
PDF_OP = "GET /v1/invoices/{invoiceId}/pdf"


def renamed(op_key, name="id"):
    """The same operation with every path placeholder called `name`: what a future spec rename would produce."""
    return re.sub(r"\{[^{}]*\}", "{" + name + "}", op_key)


# The spelling Gorelo used until 2026-10-02 (a placeholder called {id}). It is built, not written out, so no test
# or tool of this repository names an old operation key by accident.
OLD_CLIENT_DELETE = renamed("DELETE /v1/clients/{clientId}")
INVOICE = uid(1)
PDF_PATH = f"/v1/invoices/{INVOICE}/pdf"
TICKET = uid(2)
COMMENT_OP = "DELETE /v1/tickets/{ticketId}/comments/{commentId}"


def pdf_response(content=b"%PDF-1.7 fake", **headers):
    return httpx.Response(200, content=content, headers={"content-type": "application/pdf", **headers})


# --------------------------------------------------------------------------
# Constants and result types
# --------------------------------------------------------------------------


def test_constants():
    assert gorelo_client.GORELO_BASE_URL == "https://api.usw.gorelo.io/v1"
    assert (PAGE_SIZE_MIN, PAGE_SIZE_MAX) == (1, 200)
    assert FORBIDDEN_OPS == frozenset(
        {
            "DELETE /v1/clients/{clientId}",
            "DELETE /v1/contacts/{contactId}",
            "DELETE /v1/tickets/{ticketId}",
            "DELETE /v1/assets/agents/{deviceId}",
            "DELETE /v1/assets/custom/{customAssetId}",
            "DELETE /v1/contracts/{contractId}",
            "POST /v1/api-keys",
        }
    )


def test_forbidden_ops_exist_in_the_spec_and_are_deletes_except_the_api_key_creation(spec_index):
    for key in FORBIDDEN_OPS:
        assert key in spec_index.ops, f"{key} is not in the spec index: a rename would have silently un-forbidden it"
        assert spec_index.op(key).method == ("POST" if key == "POST /v1/api-keys" else "DELETE"), key


def test_side_effect_gets_are_the_pdf_export_and_exist_in_the_spec(spec_index):
    assert SIDE_EFFECT_GETS == frozenset({"GET /v1/invoices/{invoiceId}/pdf"})
    for key in SIDE_EFFECT_GETS:
        op = spec_index.op(key)
        assert op.method == "GET" and op.is_binary and key not in FORBIDDEN_OPS
    assert "export" in EXPORT_NOTE and "retry records another export event" in EXPORT_NOTE


def test_result_dataclasses_have_the_documented_fields():
    page = Page(items=[1], next_cursor=None, has_more=False, total_count=1, page_size=50)
    assert (page.items, page.next_cursor, page.has_more, page.total_count, page.page_size) == ([1], None, False, 1, 50)
    done = AllResult(items=[], total_count=None, complete=True, pages=1, count_mismatch=False)
    assert (done.items, done.total_count, done.complete, done.pages, done.count_mismatch) == ([], None, True, 1, False)
    blob = BinaryResult(content=b"x", filename=None, content_type=None)
    assert (blob.content, blob.filename, blob.content_type) == (b"x", None, None)


def test_gorelo_api_error_defaults_and_normalised_notifications():
    err = GoreloAPIError("boom")
    assert (err.status, err.op_key, err.kind, err.notifications, err.trace_id, err.write_unconfirmed) == (
        None, None, "http", [], None, False,
    )
    err = GoreloAPIError(
        "bad", status=400, op_key="POST /v1/x", kind="http", trace_id="t",
        notifications=[{"Code": "070101", "Message": "m", "PropertyName": "Phone"}, {"code": "c", "message": "n"}],
    )
    assert err.notifications == [
        {"code": "070101", "message": "m", "property": "Phone"},
        {"code": "c", "message": "n", "property": None},
    ]
    assert str(err) == "bad" and isinstance(err, Exception)


def test_gorelo_api_error_str_is_a_one_liner_and_has_a_default_message():
    assert "\n" not in str(GoreloAPIError("line one\nline two"))
    assert str(GoreloAPIError(status=404, op_key="GET /v1/x", kind="http")) == "GET /v1/x failed (http, HTTP 404)"


# --------------------------------------------------------------------------
# Headers, content types, URLs
# --------------------------------------------------------------------------


async def test_requests_carry_x_api_key_and_never_authorization(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/7", envelope({"Id": 7}))
    async with client_factory() as client:
        assert await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="get_client") == {"Id": 7}
    request = mock_gorelo.last
    assert request.headers["x-api-key"] == TEST_API_KEY
    assert request.headers["accept"] == "application/json"
    assert "authorization" not in request.headers
    assert request.url.startswith("https://api.usw.gorelo.io/v1/clients/7")
    assert request.path == "/v1/clients/7"


async def test_a_get_has_no_default_content_type(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/7", envelope({"Id": 7}))
    async with client_factory() as client:
        await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert "content-type" not in mock_gorelo.last.headers


async def test_a_json_post_sets_content_type_per_request(client_factory, mock_gorelo):
    mock_gorelo.on("POST", "/v1/clients", envelope({"Id": 1}))
    async with client_factory() as client:
        await client.post(CREATE_CLIENT, json_body={"Name": "Acme", "Location": {"Name": "HQ"}}, tool="create_client")
    request = mock_gorelo.last
    assert request.headers["content-type"] == "application/json"
    assert request.json == {"Name": "Acme", "Location": {"Name": "HQ"}}


async def test_a_multipart_request_carries_a_boundary(client_factory, mock_gorelo):
    mock_gorelo.on("POST", "/v1/attachments", envelope({"Name": "a.txt", "Url": "https://files.example.test/a"}))
    async with client_factory() as client:
        data = await client.post_multipart(
            "POST /v1/attachments",
            files={"file": ("a.txt", b"hello world", "text/plain")},
            form={"itemType": "Ticket", "itemId": "11111111-1111-1111-1111-111111111111"},
            tool="upload_attachment",
        )
    assert data == {"Name": "a.txt", "Url": "https://files.example.test/a"}
    request = mock_gorelo.last
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=") and len(content_type.split("boundary=")[1]) > 8
    assert request.files == {"file": ("a.txt", b"hello world", "text/plain")}
    assert request.form == {"itemType": "Ticket", "itemId": "11111111-1111-1111-1111-111111111111"}
    assert request.json is None


async def test_the_base_url_can_be_changed(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/7", envelope({"Id": 7}))
    async with client_factory(base_url="https://gorelo.example.test/v1/") as client:
        await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert mock_gorelo.last.url.startswith("https://gorelo.example.test/v1/clients/7")


async def test_untyped_path_tokens_are_percent_encoded_on_the_wire(client_factory, mock_gorelo):
    # The live spec has no untyped placeholder without a pattern, so use a synthetic spec for this one.
    from test_spec_loader import tiny_data

    mock_gorelo.on("GET", "/v1/things/{thingId}", envelope({"Id": 1}))
    async with client_factory(spec=SpecIndex(tiny_data())) as client:
        await client.get_one("GET /v1/things/{thingId}", path_params={"thingId": "caf\u00e9:@+x"}, tool="t")
    assert mock_gorelo.last.raw_path == "/v1/things/caf%C3%A9%3A%40%2Bx"


# --------------------------------------------------------------------------
# Envelope handling
# --------------------------------------------------------------------------


async def test_envelope_unwrap_for_object_list_and_boolean_data(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/7", envelope({"Id": 7, "Name": "Acme"}))
    mock_gorelo.on("GET", "/v1/organization/groups", envelope([{"Id": 7201, "Name": "Everyone"}]))
    mock_gorelo.on("POST", "/v1/alerts", envelope(True))
    async with client_factory() as client:
        assert await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t") == {"Id": 7, "Name": "Acme"}
        assert await client.get_list("GET /v1/organization/groups", tool="t") == [{"Id": 7201, "Name": "Everyone"}]
        body = {"Name": "n", "ClientId": 1, "Resource": "r", "Severity": 1}
        assert await client.post("POST /v1/alerts", json_body=body, tool="t") is True


async def test_request_returns_the_whole_envelope(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/7", envelope({"Id": 7}))
    async with client_factory() as client:
        result = await client.request(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert result["IsSuccess"] is True and result["Data"] == {"Id": 7} and result["StatusCode"] == 200


async def test_success_with_null_data_is_a_shape_error_for_get_one(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/7", envelope(None))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert info.value.kind == "shape" and "Data is null" in str(info.value)


async def test_is_success_false_with_http_200_raises_an_envelope_error(client_factory, mock_gorelo):
    body = error_envelope(400, [("070101", "Name is required", "Name")])
    mock_gorelo.on("POST", "/v1/clients", body, status=200)
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.post(CREATE_CLIENT, json_body={"Name": "x"}, tool="create_client")
    err = info.value
    assert err.kind == "envelope" and err.status == 200 and err.write_unconfirmed is False
    assert err.notifications == [{"code": "070101", "message": "Name is required", "property": "Name"}]
    assert err.trace_id == TEST_TRACE_ID


async def test_http_errors_carry_notifications_and_the_trace_id(client_factory, mock_gorelo):
    body = error_envelope(
        400,
        [
            notification("070101", "Mobile phone validation failed", "MobilePhone"),
            notification("070101", "Invalid country code", "MobilePhoneCountryCode"),
            notification("070201", "Invalid or malformed request body."),
        ],
        trace_id="00-abc-def-01",
    )
    mock_gorelo.on("PATCH", "/v1/contacts/1", body)
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.patch(
                "PATCH /v1/contacts/{contactId}", path_params={"contactId": 1}, json_body={"ClientId": 1}, tool="update_contact"
            )
    err = info.value
    assert (err.kind, err.status, err.op_key) == ("http", 400, "PATCH /v1/contacts/{contactId}")
    assert err.trace_id == "00-abc-def-01" and err.write_unconfirmed is False
    assert err.notifications == [
        {"code": "070101", "message": "Mobile phone validation failed", "property": "MobilePhone"},
        {"code": "070101", "message": "Invalid country code", "property": "MobilePhoneCountryCode"},
        {"code": "070201", "message": "Invalid or malformed request body.", "property": None},
    ]
    one_line = str(err)
    assert "\n" not in one_line and "400" in one_line and "PATCH /v1/contacts/{contactId}" in one_line
    assert "MobilePhone: Mobile phone validation failed" in one_line


async def test_a_404_envelope_is_an_http_error(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/9", error_envelope(404, [("070401", "Client not found")]))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 9}, tool="get_client")
    assert info.value.status == 404 and info.value.kind == "http"


@pytest.mark.parametrize(
    "response, detail",
    [
        pytest.param(httpx.Response(200, text="<html>proxy error</html>", headers={"content-type": "text/html"}), "not JSON", id="html"),
        pytest.param(httpx.Response(200, content=b""), "empty body", id="empty-body"),
        pytest.param(httpx.Response(200, json=[]), "JSON a list", id="bare-empty-list"),
        pytest.param(httpx.Response(200, json=[{"Id": 1}]), "JSON a list", id="bare-list"),
        pytest.param(httpx.Response(200, json={}), "without a boolean IsSuccess", id="empty-object"),
        pytest.param(httpx.Response(200, json={"data": [{"clientId": 1}], "nextCursor": None, "hasMore": False}), "without a boolean IsSuccess", id="legacy-lowercase-envelope"),
        pytest.param(httpx.Response(200, json={"IsSuccess": "yes", "Data": {}}), "without a boolean IsSuccess", id="is-success-not-boolean"),
        pytest.param(httpx.Response(200, json="ok"), "JSON a string", id="bare-string"),
        pytest.param(httpx.Response(200, content=b"null", headers={"content-type": "application/json"}), "JSON null", id="json-null"),
    ],
)
async def test_unknown_response_shapes_raise_instead_of_returning_empty_data(client_factory, mock_gorelo, response, detail):
    mock_gorelo.on("GET", "/v1/clients/7", response)
    mock_gorelo.on("GET", "/v1/organization/groups", response)
    async with client_factory() as client:
        for call in (
            client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t"),
            client.get_list("GET /v1/organization/groups", tool="t"),
            client.request(CLIENT_OP, path_params={"clientId": 7}, tool="t"),
        ):
            with pytest.raises(GoreloAPIError) as info:
                await call
            err = info.value
            assert err.kind == "shape" and err.status == 200
            assert "unexpected response shape; refusing to guess" in str(err)
            assert detail in str(err)
            assert err.write_unconfirmed is False  # a GET changed nothing


async def test_the_body_of_an_unreadable_response_is_never_quoted(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/7", httpx.Response(200, text=f"<html>{SECRET}</html>"))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert SECRET not in str(info.value)


async def test_an_unreadable_2xx_to_a_write_is_flagged_unconfirmed(client_factory, mock_gorelo):
    mock_gorelo.on("POST", "/v1/clients", httpx.Response(200, text="<html>gateway page</html>"))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.post(CREATE_CLIENT, json_body={"Name": "x", "Location": {"Name": "y"}}, tool="create_client")
    assert info.value.kind == "shape" and info.value.write_unconfirmed is True


async def test_a_non_envelope_error_body_is_a_shape_error_that_keeps_the_http_status(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/7", httpx.Response(502, text="<html>Bad Gateway</html>"))
    mock_gorelo.on("GET", "/v1/clients/8", httpx.Response(400, json={"title": "Bad Request", "errors": {"$.Name": ["x"]}}))
    mock_gorelo.on("GET", "/v1/clients/9", httpx.Response(404, content=b""))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
        assert info.value.kind == "shape" and info.value.status == 502 and info.value.notifications == []
        assert "HTTP 502" in str(info.value) and "refusing to guess" in str(info.value)
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 8}, tool="t")
        assert info.value.kind == "shape" and info.value.status == 400
        assert "Bad Request" in str(info.value) and "$.Name" in str(info.value)
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 9}, tool="t")
        assert info.value.kind == "shape" and info.value.status == 404 and "HTTP 404" in str(info.value)


async def test_a_gateway_page_in_answer_to_a_write_is_flagged_unconfirmed(client_factory, mock_gorelo):
    mock_gorelo.on("POST", "/v1/clients", httpx.Response(502, text="<html>Bad Gateway</html>"))
    mock_gorelo.on("POST", "/v1/alerts", httpx.Response(400, text="<html>Bad Request</html>"))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.post(CREATE_CLIENT, json_body={"Name": "x", "Location": {"Name": "y"}}, tool="t")
        assert (info.value.kind, info.value.status, info.value.write_unconfirmed) == ("shape", 502, True)
        with pytest.raises(GoreloAPIError) as info:
            await client.post("POST /v1/alerts", json_body={"Name": "n", "ClientId": 1, "Resource": "r", "Severity": 1}, tool="t")
        assert (info.value.kind, info.value.status, info.value.write_unconfirmed) == ("shape", 400, False)


@pytest.mark.parametrize("status, expected", [(400, False), (404, False), (409, False), (500, True), (502, True), (503, True)])
async def test_server_errors_on_writes_are_flagged_unconfirmed_but_reads_are_not(client_factory, mock_gorelo, status, expected):
    mock_gorelo.on("POST", "/v1/clients", error_envelope(status, [("070001", "failed")]))
    mock_gorelo.on("GET", "/v1/clients/7", error_envelope(status, [("070001", "failed")]))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.post(CREATE_CLIENT, json_body={"Name": "x", "Location": {"Name": "y"}}, tool="t")
        assert info.value.write_unconfirmed is expected
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
        assert info.value.write_unconfirmed is False


# --------------------------------------------------------------------------
# Local validation happens before any HTTP call
# --------------------------------------------------------------------------

FORBIDDEN_CALLS = {
    "DELETE /v1/clients/{clientId}": {"clientId": 1},
    "DELETE /v1/contacts/{contactId}": {"contactId": 1},
    "DELETE /v1/tickets/{ticketId}": {"ticketId": "11111111-1111-1111-1111-111111111111"},
    "DELETE /v1/assets/agents/{deviceId}": {"deviceId": "11111111-1111-1111-1111-111111111111"},
    "DELETE /v1/assets/custom/{customAssetId}": {"customAssetId": "11111111-1111-1111-1111-111111111111"},
    "DELETE /v1/contracts/{contractId}": {"contractId": 1},
    "POST /v1/api-keys": {},
}


def test_the_forbidden_cases_cover_every_forbidden_op():
    assert set(FORBIDDEN_CALLS) == set(FORBIDDEN_OPS)


@pytest.mark.parametrize("op_key", sorted(FORBIDDEN_CALLS))
async def test_forbidden_operations_raise_with_zero_http_calls(client_factory, mock_gorelo, op_key):
    params = FORBIDDEN_CALLS[op_key]
    client = client_factory()  # not even started: the check comes first
    for call in (
        client.delete(op_key, path_params=params, tool="t"),
        client.request(op_key, path_params=params, tool="t"),
        client.post(op_key, path_params=params, tool="t"),
        client.get_one(op_key, path_params=params, tool="t"),
    ):
        with pytest.raises(GoreloAPIError) as info:
            await call
        assert info.value.kind == "forbidden" and info.value.op_key == op_key and info.value.status is None
    async with client:
        with pytest.raises(GoreloAPIError, match="deliberately not available"):
            await client.delete(op_key, path_params=params, tool="t")
    assert mock_gorelo.requests == []


def test_normalize_op_key_writes_every_placeholder_as_empty_braces():
    assert normalize_op_key(OLD_CLIENT_DELETE) == "DELETE /v1/clients/{}"
    assert normalize_op_key("DELETE /v1/clients/{clientId}") == "DELETE /v1/clients/{}"
    assert normalize_op_key("GET /v1/tickets/{ticketId}/comments/{commentId}") == "GET /v1/tickets/{}/comments/{}"
    assert normalize_op_key("POST /v1/api-keys") == "POST /v1/api-keys"  # nothing to normalize
    assert normalize_op_key("GET /v1/alerts") == "GET /v1/alerts"
    assert normalize_op_key("") == ""


def test_the_shape_of_every_forbidden_op_is_distinct_and_keeps_the_method_and_the_literal_path():
    shapes = [normalize_op_key(key) for key in FORBIDDEN_OPS]
    assert len(set(shapes)) == len(FORBIDDEN_OPS)
    assert "DELETE /v1/clients/{}" in shapes and "POST /v1/api-keys" in shapes and "DELETE /v1/assets/agents/{}" in shapes
    for shape in shapes:
        assert shape.split(" ", 1)[0] in ("DELETE", "POST") and "/v1/" in shape


@pytest.mark.parametrize("op_key", sorted(FORBIDDEN_OPS))
@pytest.mark.parametrize("name", ["id", "x", "clientId", "someOtherName", "ID"])
def test_a_forbidden_op_is_forbidden_whatever_its_placeholders_are_called(op_key, name):
    assert is_forbidden_op(op_key)
    assert is_forbidden_op(renamed(op_key, name))


def test_is_forbidden_op_is_false_for_everything_else():
    for allowed in (
        "GET /v1/clients/{clientId}", "PATCH /v1/clients/{clientId}", "POST /v1/clients", "GET /v1/tickets/{ticketId}",
        "DELETE /v1/tickets/{ticketId}/comments/{commentId}", "DELETE /v1/items/{itemId}", "GET /v1/api-keys",
        "PATCH /v1/api-keys", "POST /v1/api-keys/{keyId}", "POST /v1/alerts", "DELETE /v1/clients",
        "DELETE /v1/clients/{clientId}/locations", "POST /v1/invoices", "GET /v1/invoices/{invoiceId}",
    ):
        assert not is_forbidden_op(allowed), allowed
    for not_a_key in (None, 5, [OLD_CLIENT_DELETE], OLD_CLIENT_DELETE.encode()):
        assert is_forbidden_op(not_a_key) is False  # nothing to refuse here: the spec lookup rejects it


def test_the_forbidden_shapes_in_the_live_spec_are_exactly_the_forbidden_ops(spec_index):
    """Every operation of the spec whose shape is forbidden is one of FORBIDDEN_OPS as the spec spells it today:
    a placeholder rename would show up here (the spec key and the FORBIDDEN_OPS key would differ)."""
    assert {key for key in spec_index.ops if is_forbidden_op(key)} == set(FORBIDDEN_OPS)


@pytest.mark.parametrize("op_key", sorted(FORBIDDEN_CALLS))
@pytest.mark.parametrize("name", ["id", "renamedPlaceholder"])
async def test_a_forbidden_op_with_renamed_placeholders_is_still_refused_with_zero_http_calls(
    client_factory, mock_gorelo, op_key, name
):
    variant = renamed(op_key, name)
    params = {name: value for value in FORBIDDEN_CALLS[op_key].values()}
    for call in (
        lambda c: c.delete(variant, path_params=params, tool="t"),
        lambda c: c.request(variant, path_params=params, tool="t"),
        lambda c: c.post(variant, path_params=params, tool="t"),
        lambda c: c.patch(variant, path_params=params, tool="t"),
        lambda c: c.get_one(variant, path_params=params, tool="t"),
        lambda c: c.get_page(variant, path_params=params, tool="t"),
        lambda c: c.get_all(variant, path_params=params, tool="t"),
        lambda c: c.get_binary(variant, path_params=params, max_bytes=10, tool="t"),
    ):
        client = client_factory()  # not even started: the check comes first, before the spec is asked about the key
        with pytest.raises(GoreloAPIError) as info:
            await call(client)
        # forbidden, not "unknown operation": the key is not in the spec index, but its shape is forbidden
        assert info.value.kind == "forbidden" and info.value.op_key == variant and info.value.status is None
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("helper", ["post", "request", "post_multipart"])
async def test_the_api_key_creation_can_never_be_called(client_factory, mock_gorelo, helper):
    body = {"Name": "k", "Description": "d", "Scopes": ["Project"]}
    async with client_factory() as client:
        for key in ("POST /v1/api-keys", renamed("POST /v1/api-keys", "keyId")):
            if helper == "post":
                call = client.post(key, json_body=body, tool="t")
            elif helper == "request":
                call = client.request(key, json_body=body, tool="t")
            else:
                call = client.post_multipart(key, files={"file": ("a", b"x")}, form={"Name": "k"}, tool="t")
            with pytest.raises(GoreloAPIError) as info:
                await call
            assert info.value.kind == "forbidden" and info.value.op_key == key
    assert mock_gorelo.requests == []


async def test_a_running_tool_cannot_call_the_api_key_creation_even_if_it_declared_it(client_factory, mock_gorelo):
    """The forbidden check comes before the declared-ops check: declaring a forbidden op does not make it callable."""
    token = CURRENT_TOOL.set(running_tool("POST /v1/api-keys", OLD_CLIENT_DELETE, name="rogue"))
    try:
        async with client_factory() as client:
            for key in ("POST /v1/api-keys", OLD_CLIENT_DELETE):
                with pytest.raises(GoreloAPIError) as info:
                    await client.request(key, tool="rogue")
                assert info.value.kind == "forbidden"
    finally:
        CURRENT_TOOL.reset(token)
    assert mock_gorelo.requests == []


async def test_unknown_operations_raise_a_spec_error_with_zero_http_calls(client_factory, mock_gorelo):
    async with client_factory() as client:
        for key in ("GET /v1/nope", "get /v1/clients", "GET /v1/clients/", "DELETE /v1/clients"):
            with pytest.raises(GoreloAPIError) as info:
                await client.request(key, tool="t")
            assert info.value.kind == "spec" and "unknown Gorelo operation" in str(info.value)
    assert mock_gorelo.requests == []


async def test_unknown_query_names_raise_before_any_http_call(client_factory, mock_gorelo):
    async with client_factory() as client:
        with pytest.raises(SpecViolation) as info:
            await client.get_page("GET /v1/tickets", query={"StatusId": 1}, tool="list_tickets")
        assert info.value.field == "StatusId" and "StatusIds" in str(info.value)
        with pytest.raises(SpecViolation):
            await client.get_one("GET /v1/clients/{clientId}", path_params={"clientId": 1}, query={"Bogus": "1"}, tool="t")
    assert mock_gorelo.requests == []


async def test_paging_params_are_rejected_on_an_unpaged_op(client_factory, mock_gorelo):
    async with client_factory() as client:
        for name in ("PageSize", "pagesize", "Cursor"):
            with pytest.raises(SpecViolation) as info:
                await client.request("GET /v1/organization/groups", query={name: 5}, tool="t")
            assert info.value.field == name
        with pytest.raises(SpecViolation):
            await client.get_list("GET /v1/clients/{clientId}/locations", path_params={"clientId": 1}, query={"PageSize": 200}, tool="t")
    assert mock_gorelo.requests == []


async def test_unknown_body_fields_raise_before_any_http_call_nested_too(client_factory, mock_gorelo):
    async with client_factory() as client:
        with pytest.raises(SpecViolation) as info:
            await client.post(CREATE_CLIENT, json_body={"Name": "x", "Nme": "y"}, tool="create_client")
        assert info.value.field == "Nme"
        with pytest.raises(SpecViolation) as info:
            await client.post(CREATE_CLIENT, json_body={"Name": "x", "Location": {"Name": "HQ", "Phonee": "1"}}, tool="create_client")
        assert info.value.field == "Location.Phonee"
        with pytest.raises(SpecViolation) as info:
            await client.patch("PATCH /v1/tickets/{ticketId}", path_params={"ticketId": TICKET}, json_body={"BillingOverride": {"ContractServiceId": 1}}, tool="update_ticket")
        assert info.value.field == "BillingOverride.ContractServiceId"
    assert mock_gorelo.requests == []


async def test_a_body_for_an_operation_without_one_is_rejected(client_factory, mock_gorelo):
    async with client_factory() as client:
        with pytest.raises(SpecViolation, match="takes no request body"):
            await client.request(CLIENT_OP, path_params={"clientId": 1}, json_body={"Name": "x"}, tool="t")
    assert mock_gorelo.requests == []


async def test_json_and_multipart_cannot_be_mixed_up(client_factory, mock_gorelo):
    async with client_factory() as client:
        with pytest.raises(SpecViolation, match="multipart"):
            await client.request("POST /v1/attachments", json_body={"itemType": "Ticket"}, tool="t")
        with pytest.raises(SpecViolation, match="JSON body"):
            await client.request(CREATE_CLIENT, files={"file": ("a", b"x")}, tool="t")
        with pytest.raises(SpecViolation, match="not both"):
            await client.request("POST /v1/attachments", json_body={"itemType": "x"}, files={"file": ("a", b"x")}, tool="t")
        with pytest.raises(SpecViolation, match="at least one file"):
            await client.post_multipart("POST /v1/attachments", files={}, form={"itemType": "Ticket"}, tool="t")
        with pytest.raises(SpecViolation) as info:
            await client.post_multipart("POST /v1/attachments", files={"file": ("a", b"x")}, form={"itemKind": "Ticket"}, tool="t")
        assert info.value.field == "itemKind"
        with pytest.raises(SpecViolation, match="both as a file and as text"):
            await client.post_multipart("POST /v1/attachments", files={"file": ("a", b"x")}, form={"file": "x"}, tool="t")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("value", [None, "", "   ", "\n\t", b"", b"  "], ids=repr)
async def test_a_none_or_blank_form_value_is_refused_naming_the_field(client_factory, mock_gorelo, value):
    # It used to be sent as an empty text part (None) or accepted silently. Dropping it would hide the bug too.
    form = {"itemType": value, "itemId": uid(1)}
    async with client_factory() as client:
        with pytest.raises(SpecViolation) as info:
            await client.post_multipart("POST /v1/attachments", files={"file": ("a.txt", b"x")}, form=form, tool="upload_attachment")
        assert info.value.field == "itemType" and "'itemType'" in str(info.value) and "None or blank" in str(info.value)
        with pytest.raises(SpecViolation) as info:  # the low level call checks the same
            await client.request("POST /v1/attachments", files={"file": ("a.txt", b"x")}, form=form, tool="upload_attachment")
        assert info.value.field == "itemType"
    assert mock_gorelo.requests == []


async def test_form_values_must_be_text_or_numbers_and_unknown_names_win(client_factory, mock_gorelo):
    async with client_factory() as client:
        for bad in (["a"], {"a": 1}, ("a",), object()):
            with pytest.raises(SpecViolation) as info:
                await client.post_multipart("POST /v1/attachments", files={"file": ("a", b"x")}, form={"itemType": bad}, tool="t")
            assert info.value.field == "itemType" and "takes text or a number" in str(info.value)
        with pytest.raises(SpecViolation, match="unknown multipart form field 'bogus'") as info:
            await client.post_multipart("POST /v1/attachments", files={"file": ("a", b"x")}, form={"bogus": None}, tool="t")
        assert info.value.field == "bogus"
    assert mock_gorelo.requests == []


async def test_non_blank_form_values_are_sent(client_factory, mock_gorelo):
    mock_gorelo.on("POST", "/v1/attachments", envelope({"Name": "a.txt", "Url": "https://files.example.test/a"}))
    async with client_factory() as client:
        await client.post_multipart(
            "POST /v1/attachments", files={"file": ("a.txt", b"hi", "text/plain")},
            form={"itemType": "Ticket", "itemId": uid(1)}, tool="upload_attachment",
        )
        await client.post_multipart("POST /v1/attachments", files={"file": ("a.txt", b"hi")}, form={}, tool="t")  # no form at all is fine
    assert mock_gorelo.requests[0].form == {"itemType": "Ticket", "itemId": uid(1)}


async def test_an_unknown_query_name_is_refused_even_when_its_value_is_none(client_factory, mock_gorelo):
    async with client_factory() as client:
        with pytest.raises(SpecViolation) as info:
            await client.get_page("GET /v1/tickets", query={"StatusId": None}, tool="list_tickets")
        assert info.value.field == "StatusId" and "StatusIds" in str(info.value)
        with pytest.raises(SpecViolation) as info:
            await client.get_all("GET /v1/tickets", query={"Querry": None, "Query": "x"}, tool="t")
        assert info.value.field == "Querry"
        with pytest.raises(SpecViolation) as info:
            await client.request(CLIENT_OP, path_params={"clientId": 1}, query={"Bogus": None}, tool="t")
        assert info.value.field == "Bogus"
        with pytest.raises(SpecViolation) as info:  # an unpaged op has no paging names at all
            await client.get_list("GET /v1/organization/groups", query={"PageSize": None}, tool="t")
        assert info.value.field == "PageSize"
        with pytest.raises(SpecViolation):
            await client.get_one(CLIENT_OP, path_params={"clientId": 1}, query={"x": None}, tool="t")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("name", ["PageSize", "pagesize", "Cursor", "cursor"])
async def test_paging_names_are_refused_in_query_even_when_their_value_is_none(client_factory, mock_gorelo, name):
    async with client_factory() as client:
        with pytest.raises(SpecViolation, match="page_size= and cursor=") as info:
            await client.get_page("GET /v1/clients", query={name: None}, tool="t")
        assert info.value.field == name
    assert mock_gorelo.requests == []


async def test_known_names_with_none_values_are_dropped_not_sent(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/tickets", paged_envelope([]))
    async with client_factory() as client:
        await client.get_page("GET /v1/tickets", query={"StatusIds": None, "statusids": None, "Query": "x", "TagIds": None}, page_size=5, tool="t")
        await client.get_all("GET /v1/tickets", query={"ClientIds": None}, tool="t")
    assert mock_gorelo.requests[0].query == {"Query": "x", "PageSize": "5"}
    assert mock_gorelo.requests[1].query == {"PageSize": "200"}


async def test_a_required_query_name_given_as_none_is_still_missing(client_factory, mock_gorelo):
    from test_spec_loader import tiny_data

    async with client_factory(spec=SpecIndex(tiny_data())) as client:
        with pytest.raises(SpecViolation, match="required query parameter 'Must'"):
            await client.request("GET /v1/needs", query={"Must": None}, tool="t")
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# body values must have the shape the spec gives them, and only schema field names are logged
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_key, path_params, body, fragment, field",
    [
        ("PATCH /v1/contacts/{contactId}", {"contactId": 1}, {"ClientId": 1, "SecondaryEmail": "a@b"}, "takes a list", "SecondaryEmail"),
        ("POST /v1/clients", None, {"Name": "x", "Location": "HQ"}, "takes an object", "Location"),
        ("PATCH /v1/tickets/{ticketId}", {"ticketId": TICKET}, {"BillingOverride": 5}, "takes an object", "BillingOverride"),
        ("POST /v1/items", None, {"Name": "n", "SubItems": "x"}, "takes a list", "SubItems"),
        ("POST /v1/items", None, {"Name": "n", "SubItems": {"Bogus": 1}}, "takes a list", "SubItems"),
        ("POST /v1/items", None, {"Name": "n", "SubItems": [5]}, "must be an object", "SubItems[0]"),
    ],
)
async def test_a_value_of_the_wrong_shape_is_refused_before_any_http_call(
    client_factory, mock_gorelo, caplog, op_key, path_params, body, fragment, field
):
    caplog.set_level(logging.DEBUG)
    async with client_factory() as client:
        with pytest.raises(SpecViolation) as info:
            await client.request(op_key, path_params=path_params, json_body=body, tool="t")
    assert fragment in str(info.value) and info.value.field == field
    assert mock_gorelo.requests == []
    assert not [r for r in caplog.records if r.name == "gorelo_client" and r.levelno == logging.INFO]  # never sent, never logged
    assert "Bogus" not in " ".join(r.getMessage() for r in caplog.records)


async def test_a_dict_under_an_array_field_is_refused_and_its_keys_are_never_logged(client_factory, mock_gorelo, caplog):
    from test_spec_loader import tiny_data

    caplog.set_level(logging.DEBUG)
    async with client_factory(spec=SpecIndex(tiny_data())) as client:
        for body in (
            {"Parts": {SECRET: 1}},  # a dict where the array of objects belongs
            {"Tags": {SECRET: 1}},  # a dict where an array of integers belongs
            {"Tags": [{SECRET: 1}]},  # a dict as an element of an array of integers
            {"Parts": [{"Quantity": 1}, {SECRET: 1}]},  # an unknown key inside an array element
        ):
            with pytest.raises(SpecViolation):
                await client.post("POST /v1/things", json_body=body, tool="make_thing")
    assert mock_gorelo.requests == []
    assert SECRET not in " ".join(r.getMessage() for r in caplog.records)


async def test_only_names_of_schema_fields_are_logged(client_factory, mock_gorelo, caplog):
    # "Free" is a free-form object: its field name is a schema field, but the keys inside it are data.
    from test_spec_loader import tiny_data

    caplog.set_level(logging.INFO, logger="gorelo_client")
    mock_gorelo.on("POST", "/v1/things", envelope({"Id": 1}))
    body = {
        "Name": SECRET,
        "Free": {f"{SECRET}-key": {"deeper": SECRET}},
        "Tags": [1, 2],
        "Parts": [{"Quantity": 1, "Sku": SECRET}, {"Quantity": 2}],
        "Location": {"Name": SECRET},
    }
    async with client_factory(spec=SpecIndex(tiny_data())) as client:
        await client.post("POST /v1/things", json_body=body, tool="make_thing")
    (line,) = [r.getMessage() for r in caplog.records if r.name == "gorelo_client" and r.levelno == logging.INFO]
    assert "body=Free,Location,Location.Name,Name,Parts,Parts.Quantity,Parts.Sku,Tags" in line
    assert SECRET not in line and "deeper" not in line
    assert mock_gorelo.last.json == body  # and the body itself is sent untouched


async def test_multipart_names_logged_are_the_validated_form_and_file_fields(client_factory, mock_gorelo, caplog):
    caplog.set_level(logging.INFO, logger="gorelo_client")
    mock_gorelo.on("POST", "/v1/attachments", envelope({"Name": "a.txt", "Url": "u"}))
    async with client_factory() as client:
        await client.post_multipart(
            "POST /v1/attachments", files={"file": ("a.txt", b"x", "text/plain")},
            form={"itemType": "Ticket", "itemId": uid(1)}, tool="upload_attachment",
        )
    (line,) = [r.getMessage() for r in caplog.records if r.name == "gorelo_client" and r.levelno == logging.INFO]
    assert "body=file,itemId,itemType" in line


# --------------------------------------------------------------------------
# While a tool runs, only the operations it declared can be sent
# --------------------------------------------------------------------------


def running_tool(*ops, name="demo_tool"):
    return SimpleNamespace(name=name, ops=list(ops))


async def test_inside_a_running_tool_only_its_declared_operations_can_be_sent(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/7", envelope({"Id": 7}))
    token = CURRENT_TOOL.set(running_tool(CLIENT_OP))
    try:
        async with client_factory() as client:
            assert await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="demo_tool") == {"Id": 7}
            for make_call in (
                lambda: client.get_page("GET /v1/tickets", tool="demo_tool"),
                lambda: client.request("GET /v1/tickets", tool="demo_tool"),
                lambda: client.post(CREATE_CLIENT, json_body={"Name": "x"}, tool="demo_tool"),
                lambda: client.patch("PATCH /v1/time-entries/{timeEntryId}", path_params={"timeEntryId": 1}, json_body={}, tool="demo_tool"),
                lambda: client.delete("DELETE /v1/time-entries/{timeEntryId}", path_params={"timeEntryId": 1}, tool="demo_tool"),
                lambda: client.get_list("GET /v1/organization/groups", tool="demo_tool"),
                lambda: client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=10, tool="demo_tool"),
                lambda: client.post_multipart("POST /v1/attachments", files={"file": ("a", b"x")}, form={}, tool="demo_tool"),
                lambda: client.request("GET /v1/nope", tool="demo_tool"),
            ):
                with pytest.raises(GoreloAPIError) as info:
                    await make_call()
                err = info.value
                assert err.kind == "spec" and err.status is None and err.write_unconfirmed is False
                assert "demo_tool" in str(err) and CLIENT_OP in str(err) and "declares" in str(err)
    finally:
        CURRENT_TOOL.reset(token)
    assert len(mock_gorelo.requests) == 1  # only the declared GET went out


async def test_a_forbidden_operation_stays_forbidden_whatever_a_tool_declares(client_factory, mock_gorelo):
    forbidden = "DELETE /v1/clients/{clientId}"
    token = CURRENT_TOOL.set(running_tool(forbidden))
    try:
        async with client_factory() as client:
            with pytest.raises(GoreloAPIError) as info:
                await client.delete(forbidden, path_params={"clientId": 1}, tool="demo_tool")
    finally:
        CURRENT_TOOL.reset(token)
    assert info.value.kind == "forbidden" and mock_gorelo.requests == []


async def test_outside_any_tool_every_operation_can_be_sent(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/7", envelope({"Id": 7}))
    mock_gorelo.on("GET", "/v1/tickets", paged_envelope([]))
    assert CURRENT_TOOL.get() is None
    async with client_factory() as client:
        await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="script")
        await client.get_page("GET /v1/tickets", tool="script")
    assert len(mock_gorelo.requests) == 2


async def test_a_task_started_inside_a_tool_inherits_the_declared_operations(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/7", envelope({"Id": 7}))
    token = CURRENT_TOOL.set(running_tool(CLIENT_OP))
    try:
        async with client_factory() as client:
            results = await asyncio.gather(
                client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="demo_tool"),
                client.get_page("GET /v1/tickets", tool="demo_tool"),
                return_exceptions=True,
            )
    finally:
        CURRENT_TOOL.reset(token)
    assert results[0] == {"Id": 7}
    assert isinstance(results[1], GoreloAPIError) and results[1].kind == "spec"
    assert len(mock_gorelo.requests) == 1


async def test_empty_id_lists_raise_naming_the_param(client_factory, mock_gorelo):
    async with client_factory() as client:
        for value in ([], ()):
            with pytest.raises(SpecViolation) as info:
                await client.get_page("GET /v1/tickets", query={"StatusIds": value}, tool="list_tickets")
            assert info.value.field == "StatusIds" and "empty list" in str(info.value)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("value", [[1, ""], ["a,b"], [1.5], [None], [True], [[1]], ["  "]])
async def test_bad_id_list_members_raise(client_factory, mock_gorelo, value):
    async with client_factory() as client:
        with pytest.raises(SpecViolation) as info:
            await client.get_page("GET /v1/tickets", query={"StatusIds": value}, tool="t")
        assert info.value.field == "StatusIds"
    assert mock_gorelo.requests == []


async def test_query_values_are_prepared_lists_joined_booleans_lowercase_none_dropped(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/projects/{projectId}/tasks", paged_envelope([]))
    mock_gorelo.on("GET", "/v1/tickets", paged_envelope([]))
    async with client_factory() as client:
        await client.get_page(
            "GET /v1/tickets",
            query={"statusids": [1, 2, 3], "ClientIds": ("a", "b"), "Query": "printer", "TagIds": None, "SortOrder": "asc"},
            page_size=25,
            tool="t",
        )
        await client.get_page("GET /v1/projects/{projectId}/tasks", path_params={"projectId": uid(3)}, query={"IncompleteOnly": True}, tool="t")
        await client.get_page("GET /v1/projects/{projectId}/tasks", path_params={"projectId": uid(3)}, query={"IncompleteOnly": False}, tool="t")
    first, second, third = mock_gorelo.requests
    assert first.query == {"StatusIds": "1,2,3", "ClientIds": "a,b", "Query": "printer", "SortOrder": "asc", "PageSize": "25"}
    assert second.query["IncompleteOnly"] == "true" and third.query["IncompleteOnly"] == "false"


async def test_datetimes_in_a_query_are_converted_to_utc_z(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/tickets", paged_envelope([]))
    async with client_factory() as client:
        aware = datetime(2026, 10, 1, 9, 30, tzinfo=timezone(timedelta(hours=-5)))
        await client.get_page("GET /v1/tickets", query={"UpdatedSince": aware}, tool="t")
        assert mock_gorelo.last.query["UpdatedSince"] == "2026-10-01T14:30:00Z"
        with pytest.raises(SpecViolation, match="without a UTC offset"):
            await client.get_page("GET /v1/tickets", query={"UpdatedSince": datetime(2026, 10, 1, 9, 30)}, tool="t")


async def test_path_parameters_are_required_known_and_non_empty(client_factory, mock_gorelo):
    async with client_factory() as client:
        with pytest.raises(SpecViolation, match="'clientId' is required") as info:
            await client.get_one(CLIENT_OP, tool="t")
        assert info.value.field == "clientId"
        for bad in ("", "   ", None, True):
            with pytest.raises(SpecViolation, match="'clientId' is required"):
                await client.get_one(CLIENT_OP, path_params={"clientId": bad}, tool="t")
        with pytest.raises(SpecViolation, match="unknown path parameter 'client_id'"):
            await client.get_one(CLIENT_OP, path_params={"clientId": 1, "client_id": 1}, tool="t")
        with pytest.raises(SpecViolation, match="unknown path parameter"):
            await client.get_list("GET /v1/organization/groups", path_params={"id": 1}, tool="t")
        with pytest.raises(SpecViolation, match="'commentId' is required"):
            await client.get_one("GET /v1/tickets/{ticketId}/comments/{commentId}", path_params={"ticketId": TICKET}, tool="t")
        # a missing id is reported before a malformed one
        with pytest.raises(SpecViolation, match="'commentId' is required"):
            await client.get_one("GET /v1/tickets/{ticketId}/comments/{commentId}", path_params={"ticketId": "t"}, tool="t")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("dots", ["..", ".", "...", " .. "])
async def test_dot_segments_cannot_be_smuggled_through_a_path_id(client_factory, mock_gorelo, dots):
    # httpx collapses dot segments: DELETE .../comments/.. would become DELETE /v1/tickets/T, which is forbidden
    async with client_factory() as client:
        with pytest.raises(SpecViolation) as info:
            await client.delete(COMMENT_OP, path_params={"ticketId": TICKET, "commentId": dots}, tool="delete_ticket_comment")
        assert info.value.field == "commentId" and "UUID" in str(info.value)
        with pytest.raises(SpecViolation):
            await client.get_one(CLIENT_OP, path_params={"clientId": dots}, tool="t")
        with pytest.raises(SpecViolation):
            await client.delete(COMMENT_OP, path_params={"ticketId": dots, "commentId": uid(9)}, tool="t")
        # an untyped token (a form id) refuses dot-only values by its own rule
        with pytest.raises(SpecViolation, match="plain token") as info:
            await client.get_page("GET /v1/forms/{formId}/responses", path_params={"formId": dots}, tool="t")
        assert info.value.field == "formId"
    assert mock_gorelo.requests == []


TRAVERSAL_IDS = ["%2e%2e", "..%2F..", "a/../b", "../x", "...", ".. ", "../..", "%2E", "a%2Fb", "x?y=1", "x#y", "a\\b", "a b", "\t", "\u0000", "a\nb"]


@pytest.mark.parametrize("value", TRAVERSAL_IDS)
async def test_traversal_like_ids_are_refused_before_any_http_call_not_encoded(client_factory, mock_gorelo, value):
    # They used to be percent-encoded into one path segment. Now no placeholder takes them at all.
    async with client_factory() as client:
        with pytest.raises(SpecViolation) as info:
            await client.delete(COMMENT_OP, path_params={"ticketId": TICKET, "commentId": value}, tool="t")
        assert info.value.field == "commentId"
        with pytest.raises(SpecViolation) as info:
            await client.get_page("GET /v1/forms/{formId}/responses", path_params={"formId": value}, tool="t")
        assert info.value.field == "formId"
        with pytest.raises(SpecViolation) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": value}, tool="t")
        assert info.value.field == "clientId"
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# every path id is checked against what the spec says it is
# --------------------------------------------------------------------------

BAD_UUIDS = [
    "t", "abc", "../x", "a/b", "%2e%2e", "x" * 36,
    "11111111-1111-1111-1111-11111111111",  # one digit short
    "11111111-1111-1111-1111-1111111111111",  # one digit long
    " 11111111-1111-1111-1111-111111111111",
    "11111111-1111-1111-1111-111111111111 ",
    "11111111-1111-1111-1111-111111111111/",
    "11111111-1111-1111-1111-111111111111/../x",
    "{11111111-1111-1111-1111-111111111111}",
    "urn:uuid:11111111-1111-1111-1111-111111111111",
    "1111111111111111-1111-1111-111111111111",  # hyphens in the wrong places
    "g1111111-1111-1111-1111-111111111111",
    " " + "1" * 31,  # uuid.UUID alone would turn this into another id
    "1_" + "1" * 30,
    7, 7.5, ["x"], {"a": 1}, b"11111111-1111-1111-1111-111111111111",
]


@pytest.mark.parametrize("bad", BAD_UUIDS, ids=lambda value: repr(value)[:40])
async def test_a_uuid_path_id_must_be_a_uuid(client_factory, mock_gorelo, bad):
    async with client_factory() as client:
        with pytest.raises(SpecViolation) as info:
            await client.get_one("GET /v1/tickets/{ticketId}", path_params={"ticketId": bad}, tool="t")
        assert info.value.field == "ticketId" and "'ticketId'" in str(info.value) and "UUID" in str(info.value)
        assert info.value.op_key == "GET /v1/tickets/{ticketId}"
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "given",
    [uid(5), uid(5).upper(), uid(5).replace("-", ""), uid(5).upper().replace("-", ""), uuid.UUID(uid(5))],
    ids=["canonical", "upper", "bare", "upper-bare", "uuid-object"],
)
async def test_a_uuid_path_id_is_sent_in_canonical_form(client_factory, mock_gorelo, given):
    mock_gorelo.on("GET", f"/v1/tickets/{uid(5)}", envelope({"Id": 1}))
    async with client_factory() as client:
        await client.get_one("GET /v1/tickets/{ticketId}", path_params={"ticketId": given}, tool="t")
    assert mock_gorelo.last.raw_path == f"/v1/tickets/{uid(5)}"


@pytest.mark.parametrize("given, wire", [(7, "7"), ("7", "7"), ("007", "7"), (0, "0"), ("9700", "9700"), (2**63 - 1, str(2**63 - 1))])
async def test_an_integer_path_id_takes_ints_and_digit_strings(client_factory, mock_gorelo, given, wire):
    mock_gorelo.on("GET", f"/v1/clients/{wire}", envelope({"Id": 1}))
    async with client_factory() as client:
        await client.get_one(CLIENT_OP, path_params={"clientId": given}, tool="t")
    assert mock_gorelo.last.raw_path == f"/v1/clients/{wire}"


@pytest.mark.parametrize(
    "bad",
    ["7a", "7 ", " 7", "-1", -1, "+7", "7.0", 7.0, 7.5, "0x1f", "1e3", "\u0661\u0662", "\uff17", 2**63, "9" * 30,
     "9" * 5000, [7], {"a": 1}, "../1", "1/2", uid(1)],
    ids=lambda value: repr(value)[:30],
)
async def test_an_integer_path_id_must_be_a_whole_non_negative_number(client_factory, mock_gorelo, bad):
    async with client_factory() as client:
        with pytest.raises(SpecViolation) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": bad}, tool="t")
        assert info.value.field == "clientId" and "'clientId'" in str(info.value) and "whole number" in str(info.value)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "bad",
    ["a b", "a/b", "a\\b", "a%2Fb", "a%", "a?b", "a#b", "\t", "a\nb", "a\u0000b", "a\u202eb", "\u00a0x",
     "..", ".", "...", "has.dot", "x" * 51, "caf\u00e9", [1], {"a": 1}, 1.5],
    ids=lambda value: repr(value)[:30],
)
async def test_an_untyped_path_token_refuses_unsafe_values(client_factory, mock_gorelo, bad):
    async with client_factory() as client:
        with pytest.raises(SpecViolation) as info:
            await client.get_page("GET /v1/forms/{formId}/responses", path_params={"formId": bad}, tool="t")
        assert info.value.field == "formId" and "'formId'" in str(info.value)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("given, wire", [("form-1", "form-1"), ("F_2", "F_2"), ("x" * 50, "x" * 50), (12, "12")])
async def test_an_untyped_path_token_takes_plain_tokens(client_factory, mock_gorelo, given, wire):
    mock_gorelo.on("GET", f"/v1/forms/{wire}/responses", paged_envelope([]))
    async with client_factory() as client:
        await client.get_page("GET /v1/forms/{formId}/responses", path_params={"formId": given}, tool="t")
    assert mock_gorelo.last.raw_path == f"/v1/forms/{wire}/responses"


async def test_the_generic_token_rules_apply_even_where_the_spec_gives_no_pattern(client_factory, mock_gorelo):
    from test_spec_loader import tiny_data

    mock_gorelo.on("GET", "/v1/things/{thingId}", envelope({"Id": 1}))
    async with client_factory(spec=SpecIndex(tiny_data())) as client:
        await client.get_one("GET /v1/things/{thingId}", path_params={"thingId": "has.dot"}, tool="t")  # no pattern: fine
        for bad in ("a/b", "..", "a b", "a%2Fb", "a?b", "a#b", "a\\b"):
            with pytest.raises(SpecViolation) as info:
                await client.get_one("GET /v1/things/{thingId}", path_params={"thingId": bad}, tool="t")
            assert info.value.field == "thingId"
    assert len(mock_gorelo.requests) == 1


async def test_every_path_id_error_names_the_placeholder_and_never_repeats_the_value(client_factory, mock_gorelo):
    async with client_factory() as client:
        for op_key, params in (
            ("GET /v1/tickets/{ticketId}", {"ticketId": SECRET}),
            (CLIENT_OP, {"clientId": SECRET}),
            ("GET /v1/forms/{formId}/responses", {"formId": SECRET + "/"}),
        ):
            with pytest.raises(SpecViolation) as info:
                await client.request(op_key, path_params=params, tool="t")
            assert next(iter(params)) in str(info.value) and SECRET not in str(info.value)
    assert mock_gorelo.requests == []


async def test_any_untyped_path_id_stays_inside_its_own_path_segment(client_factory, mock_gorelo):
    # A seeded sweep over odd ids (unicode, control characters, slashes, dots, percent signs) on a
    # placeholder with no pattern: the request path is the spec path with exactly one segment for the
    # id (percent-encoded), or the id is refused for one of the documented reasons.
    import random
    import string
    from urllib.parse import quote

    from test_spec_loader import tiny_data

    rng = random.Random(7)
    alphabet = string.printable + "\u00e9\u00fc\u4e2d\u6587\U0001F600\u202e\u0000\u007f"
    specials = ["..", ".", "...", "./", "../", "/..", "a/..", "%2e", "%2e%2e", ".%2e", " .. "]
    mock_gorelo.on("GET", "/v1/things/{thingId}", envelope({"Id": "x"}))
    sent = refused = 0
    async with client_factory(spec=SpecIndex(tiny_data())) as client:
        for _ in range(600):
            value = rng.choice(specials) if rng.random() < 0.2 else "".join(rng.choice(alphabet) for _ in range(rng.choice([1, 2, 3, 8, 30])))
            try:
                await client.get_one("GET /v1/things/{thingId}", path_params={"thingId": value}, tool="t")
            except SpecViolation:
                refused += 1
                assert (
                    not value.strip()
                    or value.strip(".") == ""
                    or any(ch in "/\\%?#" for ch in value)
                    or any(ch.isspace() or not ch.isprintable() for ch in value)
                ), repr(value)
                continue
            sent += 1
            raw = mock_gorelo.last.raw_path
            assert raw == "/v1/things/" + quote(value, safe=""), (value, raw)
            assert raw.count("/") == 3
    assert sent > 100 and refused > 100


async def test_any_uuid_or_integer_path_id_is_refused_or_sent_in_canonical_form(client_factory, mock_gorelo):
    import random
    import re

    rng = random.Random(11)
    reference = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}")
    noise = "0123456789abcdefABCDEF-_ {}/.%+gG\t\u0661"
    mock_gorelo.on("GET", "/v1/tickets/{ticketId}", envelope({"Id": "x"}))
    mock_gorelo.on("GET", "/v1/clients/{clientId}", envelope({"Id": "x"}))
    sent = refused = 0
    async with client_factory() as client:
        for _ in range(500):
            base = str(uuid.UUID(int=rng.getrandbits(128)))
            mutation = rng.choice(["none", "upper", "bare", "swap", "insert", "delete", "pad"])
            value = {
                "none": base,
                "upper": base.upper(),
                "bare": base.replace("-", ""),
                "swap": (lambda i: base[:i] + rng.choice(noise) + base[i + 1:])(rng.randrange(len(base))),
                "insert": (lambda i: base[:i] + rng.choice(noise) + base[i:])(rng.randrange(len(base))),
                "delete": (lambda i: base[:i] + base[i + 1:])(rng.randrange(len(base))),
                "pad": rng.choice([" ", "\n", "/"]) + base,
            }[mutation]
            try:
                await client.get_one("GET /v1/tickets/{ticketId}", path_params={"ticketId": value}, tool="t")
            except SpecViolation:
                refused += 1
                assert reference.fullmatch(value) is None, repr(value)
                continue
            sent += 1
            assert reference.fullmatch(value) is not None, repr(value)
            assert mock_gorelo.last.raw_path == f"/v1/tickets/{str(uuid.UUID(value))}"
        for _ in range(300):
            number = rng.randrange(0, 10**6)
            value = rng.choice([number, str(number), f"{number}{rng.choice(noise)}", f"{rng.choice(noise)}{number}", -number - 1])
            try:
                await client.get_one(CLIENT_OP, path_params={"clientId": value}, tool="t")
            except SpecViolation:
                refused += 1
                assert not (isinstance(value, int) and value >= 0) and not (isinstance(value, str) and re.fullmatch("[0-9]+", value)), repr(value)
                continue
            sent += 1
            assert mock_gorelo.last.raw_path == f"/v1/clients/{int(value)}"
    assert sent > 150 and refused > 150


async def test_a_rewritten_request_path_is_never_sent(client_factory, mock_gorelo, monkeypatch):
    # Even if a bad path got past the id checks, the path on the wire is compared with the checked one.
    monkeypatch.setattr(GoreloClient, "_fill_path", lambda self, op, params: "/v1/tickets/T/comments/..")
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError, match="URL building changed the request path") as info:
            await client.request("DELETE /v1/tickets/{ticketId}/comments/{commentId}", tool="t")
        assert info.value.kind == "spec"
    assert mock_gorelo.requests == []


async def test_the_tool_name_is_required(client_factory):
    async with client_factory() as client:
        with pytest.raises(ValueError, match="tool="):
            await client.get_one(CLIENT_OP, path_params={"clientId": 1}, tool="")
        with pytest.raises(TypeError):
            await client.get_one(CLIENT_OP, path_params={"clientId": 1})  # type: ignore[call-arg]


async def test_helpers_check_the_method_and_the_paging_kind_of_the_op(client_factory, mock_gorelo):
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError, match="POST operation") as info:
            await client.get_one(CREATE_CLIENT, tool="t")
        assert info.value.kind == "spec"
        with pytest.raises(GoreloAPIError, match="GET operation"):
            await client.post("GET /v1/clients", tool="t")
        with pytest.raises(GoreloAPIError, match="paged operation"):
            await client.get_one("GET /v1/clients", tool="t")
        with pytest.raises(GoreloAPIError, match="paged operation"):
            await client.get_list("GET /v1/clients", tool="t")
        with pytest.raises(GoreloAPIError, match="not a paged operation"):
            await client.get_page("GET /v1/organization/groups", tool="t")
        with pytest.raises(GoreloAPIError, match="not a paged operation"):
            await client.get_all("GET /v1/clients/{clientId}", path_params={"clientId": 1}, tool="t")
        with pytest.raises(GoreloAPIError, match="file download"):
            await client.get_one(PDF_OP, path_params={"invoiceId": INVOICE}, tool="t")
        with pytest.raises(GoreloAPIError, match="not a file download"):
            await client.get_binary(CLIENT_OP, path_params={"clientId": 1}, max_bytes=10, tool="t")
        with pytest.raises(GoreloAPIError, match="does not take multipart"):
            await client.post_multipart(CREATE_CLIENT, files={"file": ("a", b"x")}, form={}, tool="t")
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


async def test_patch_sends_json_and_keeps_explicit_nulls(client_factory, mock_gorelo):
    mock_gorelo.on("PATCH", "/v1/time-entries/55", envelope({"Id": 55}))
    async with client_factory() as client:
        data = await client.patch("PATCH /v1/time-entries/{timeEntryId}", path_params={"timeEntryId": 55}, json_body={"ServiceLineId": None, "Comment": "c"}, tool="update_time_entry")
    assert data == {"Id": 55}
    assert mock_gorelo.last.method == "PATCH" and mock_gorelo.last.json == {"ServiceLineId": None, "Comment": "c"}


async def test_delete_sends_no_body_and_returns_data(client_factory, mock_gorelo):
    mock_gorelo.on("DELETE", "/v1/time-entries/55", envelope({"Id": 55, "Outcome": "Deleted"}))
    async with client_factory() as client:
        data = await client.delete("DELETE /v1/time-entries/{timeEntryId}", path_params={"timeEntryId": 55}, tool="delete_time_entry")
    assert data == {"Id": 55, "Outcome": "Deleted"}
    request = mock_gorelo.last
    assert request.method == "DELETE" and request.content == b"" and "content-type" not in request.headers


async def test_post_without_a_body_sends_none(client_factory, mock_gorelo):
    mock_gorelo.on("POST", "/v1/clients", envelope({"Id": 1}))
    async with client_factory() as client:
        await client.post(CREATE_CLIENT, tool="t")
    assert mock_gorelo.last.content == b"" and mock_gorelo.last.json is None


# --------------------------------------------------------------------------
# Paging
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "asked, used", [(0, 1), (-5, 1), (1, 1), (50, 50), (200, 200), (201, 200), (500, 200), ("75", 75)]
)
async def test_page_size_is_clamped_to_1_200_and_reported(client_factory, mock_gorelo, asked, used):
    mock_gorelo.on("GET", "/v1/clients", paged_envelope([{"Id": 1}], total_count=40))
    async with client_factory() as client:
        page = await client.get_page("GET /v1/clients", page_size=asked, tool="list_clients")
    assert page.page_size == used
    assert mock_gorelo.last.query == {"PageSize": str(used)}


async def test_page_size_must_be_a_number(client_factory):
    async with client_factory() as client:
        with pytest.raises(ValueError, match="page_size"):
            await client.get_page("GET /v1/clients", page_size="many", tool="t")  # type: ignore[arg-type]


async def test_get_page_reads_pagination_and_sends_the_cursor(client_factory, mock_gorelo):
    items = [{"Id": 1}, {"Id": 2}]
    mock_gorelo.on("GET", "/v1/clients", envelope(items, pagination("opaque.cursor==", 40)))
    async with client_factory() as client:
        page = await client.get_page("GET /v1/clients", query={"Query": "acme"}, page_size=2, cursor="prev.cursor", tool="list_clients")
    assert page == Page(items=items, next_cursor="opaque.cursor==", has_more=True, total_count=40, page_size=2)
    assert mock_gorelo.last.query == {"Query": "acme", "PageSize": "2", "Cursor": "prev.cursor"}


async def test_a_blank_cursor_means_the_first_page(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients", paged_envelope([]))
    async with client_factory() as client:
        for blank in (None, "", "   "):
            await client.get_page("GET /v1/clients", cursor=blank, tool="t")
        with pytest.raises(ValueError, match="cursor"):
            await client.get_page("GET /v1/clients", cursor=5, tool="t")  # type: ignore[arg-type]
    assert all("Cursor" not in r.query for r in mock_gorelo.requests)


async def test_paging_names_in_query_are_refused_for_get_page(client_factory, mock_gorelo):
    async with client_factory() as client:
        for name in ("PageSize", "cursor"):
            with pytest.raises(SpecViolation, match="page_size= and cursor="):
                await client.get_page("GET /v1/clients", query={name: 5}, tool="t")
    assert mock_gorelo.requests == []


async def test_a_paged_response_that_cannot_be_trusted_raises(client_factory, mock_gorelo):
    cases = {
        "has-more-no-cursor": envelope([{"Id": 1}], pagination(None, 5, has_more=True)),
        "data-not-a-list": envelope({"Id": 1}, pagination(None, 1)),
        "data-null-with-rows-unaccounted": envelope(None, pagination(None, 4)),
        "no-pagination-with-rows": envelope([{"Id": 1}]),
        "has-more-not-boolean": envelope([], {"HasMore": "no", "NextCursor": None, "TotalCount": 0}),
        "total-count-not-int": envelope([], pagination(None, 0) | {"TotalCount": "0"}),
        "pagination-not-object": envelope([], None) | {"DataContext": {"Pagination": []}},
    }
    async with client_factory() as client:
        for name, body in cases.items():
            mock_gorelo.reset()
            mock_gorelo._routes.clear()
            mock_gorelo.on("GET", "/v1/clients", body)
            with pytest.raises(GoreloAPIError) as info:
                await client.get_page("GET /v1/clients", tool="t")
            assert info.value.kind == "shape", name


async def test_empty_pages_are_accepted_only_with_corroboration(client_factory, mock_gorelo):
    async with client_factory() as client:
        mock_gorelo.on("GET", "/v1/clients", envelope([], pagination(None, 0)))
        page = await client.get_page("GET /v1/clients", tool="t")
        assert (page.items, page.has_more, page.total_count) == ([], False, 0)
        mock_gorelo._routes.clear()
        mock_gorelo.on("GET", "/v1/clients", envelope(None, pagination(None, 0)))  # nullable list, TotalCount 0
        page = await client.get_page("GET /v1/clients", tool="t")
        assert (page.items, page.has_more, page.total_count) == ([], False, 0)
        mock_gorelo._routes.clear()
        mock_gorelo.on("GET", "/v1/clients", envelope([]))  # no Pagination block but also no rows
        page = await client.get_page("GET /v1/clients", tool="t")
        assert (page.items, page.has_more, page.next_cursor, page.total_count) == ([], False, None, None)


async def test_get_all_follows_cursors_with_the_same_filters(client_factory, mock_gorelo):
    pages = [[{"Id": 1}, {"Id": 2}], [{"Id": 3}, {"Id": 4}], [{"Id": 5}]]
    mock_gorelo.on("GET", "/v1/clients", paged_responder(pages))
    async with client_factory() as client:
        result = await client.get_all("GET /v1/clients", query={"Query": "a", "StatusIds": [1, 2]}, tool="list_clients")
    assert result == AllResult(items=[{"Id": i} for i in range(1, 6)], total_count=5, complete=True, pages=3, count_mismatch=False)
    assert [r.query for r in mock_gorelo.requests] == [
        {"Query": "a", "StatusIds": "1,2", "PageSize": "200"},
        {"Query": "a", "StatusIds": "1,2", "PageSize": "200", "Cursor": "c1"},
        {"Query": "a", "StatusIds": "1,2", "PageSize": "200", "Cursor": "c2"},
    ]


async def test_get_all_clamps_its_page_size_too(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients", paged_responder([[{"Id": 1}]]))
    async with client_factory() as client:
        await client.get_all("GET /v1/clients", page_size=9999, tool="t")
        await client.get_all("GET /v1/clients", page_size=10, tool="t")
    assert [r.query["PageSize"] for r in mock_gorelo.requests] == ["200", "10"]


async def test_get_all_detects_a_repeated_cursor(client_factory, mock_gorelo):
    def stuck(request):
        return envelope([{"Id": 1}], pagination("same-cursor", 99))

    mock_gorelo.on("GET", "/v1/clients", stuck)
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_all("GET /v1/clients", tool="t")
    assert info.value.kind == "shape" and "already served" in str(info.value)
    assert len(mock_gorelo.requests) == 2  # page 1, then the page that repeated the cursor


async def test_get_all_stops_at_max_items_and_says_so(client_factory, mock_gorelo):
    pages = [[{"Id": 1}, {"Id": 2}], [{"Id": 3}, {"Id": 4}], [{"Id": 5}, {"Id": 6}]]
    mock_gorelo.on("GET", "/v1/clients", paged_responder(pages))
    async with client_factory() as client:
        result = await client.get_all("GET /v1/clients", max_items=3, tool="t")
        assert [row["Id"] for row in result.items] == [1, 2, 3]
        assert result.complete is False and result.pages == 2 and result.count_mismatch is False
        assert result.total_count == 6 and len(mock_gorelo.requests) == 2
        mock_gorelo.reset()
        result = await client.get_all("GET /v1/clients", max_items=4, tool="t")
        assert len(result.items) == 4 and result.complete is False and result.pages == 2  # more rows exist
        mock_gorelo.reset()
        result = await client.get_all("GET /v1/clients", max_items=6, tool="t")
        assert len(result.items) == 6 and result.complete is True and result.pages == 3  # exactly everything
        mock_gorelo.reset()
        result = await client.get_all("GET /v1/clients", max_items=100, tool="t")
        assert len(result.items) == 6 and result.complete is True


async def test_get_all_rejects_a_silly_max_items(client_factory):
    async with client_factory() as client:
        for bad in (0, -1):
            with pytest.raises(ValueError, match="max_items"):
                await client.get_all("GET /v1/clients", max_items=bad, tool="t")


async def test_get_all_flags_a_count_mismatch_and_warns_without_raising(client_factory, mock_gorelo, caplog):
    pages = [[{"Id": 1}, {"Id": 2}], [{"Id": 3}]]
    mock_gorelo.on("GET", "/v1/clients", paged_responder(pages, total_count=5))
    caplog.set_level(logging.WARNING, logger="gorelo_client")
    async with client_factory() as client:
        result = await client.get_all("GET /v1/clients", tool="list_clients")
    assert result.complete is True and result.count_mismatch is True and result.total_count == 5 and len(result.items) == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "TotalCount=5" in warnings[0].getMessage() and "3 rows" in warnings[0].getMessage()


async def test_get_all_without_a_mismatch_is_quiet(client_factory, mock_gorelo, caplog):
    mock_gorelo.on("GET", "/v1/clients", paged_responder([[{"Id": 1}, {"Id": 2}]]))
    caplog.set_level(logging.WARNING, logger="gorelo_client")
    async with client_factory() as client:
        result = await client.get_all("GET /v1/clients", tool="t")
    assert result.count_mismatch is False and not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_get_all_of_an_empty_collection(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients", paged_envelope([]))
    async with client_factory() as client:
        result = await client.get_all("GET /v1/clients", tool="t")
    assert result == AllResult(items=[], total_count=0, complete=True, pages=1, count_mismatch=False)


async def test_get_all_gives_up_on_endless_paging(client_factory, mock_gorelo, monkeypatch):
    monkeypatch.setattr(gorelo_client, "MAX_PAGES", 3)
    counter = iter(range(1000))
    mock_gorelo.on("GET", "/v1/clients", lambda request: envelope([{"Id": 1}], pagination(f"c{next(counter)}", 999)))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError, match="did not finish after 3 pages") as info:
            await client.get_all("GET /v1/clients", tool="t")
    assert info.value.kind == "shape" and len(mock_gorelo.requests) == 3


async def test_get_list_requires_a_list(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/tickets/statuses", envelope({"Id": 1}))
    mock_gorelo.on("GET", "/v1/tickets/types", envelope(None))
    async with client_factory() as client:
        for key in ("GET /v1/tickets/statuses", "GET /v1/tickets/types"):
            with pytest.raises(GoreloAPIError) as info:
                await client.get_list(key, tool="t")
            assert info.value.kind == "shape" and "expected Data to be a list" in str(info.value)


# --------------------------------------------------------------------------
# 429: retried within a budget, for every method
# --------------------------------------------------------------------------


def too_many(retry_after=None, body=None):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return httpx.Response(429, json=body if body is not None else {"Message": "slow down"}, headers=headers)


async def test_a_429_with_retry_after_seconds_is_retried_after_that_wait(client_factory, mock_gorelo, fake_sleep):
    mock_gorelo.on("GET", "/v1/clients/7", in_order(too_many(2), envelope({"Id": 7})))
    async with client_factory() as client:
        assert await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t") == {"Id": 7}
    assert fake_sleep.delays == [2.0] and len(mock_gorelo.requests) == 2


async def test_retry_after_may_be_an_http_date(client_factory, mock_gorelo, fake_sleep, monkeypatch):
    monkeypatch.setattr(gorelo_client, "_now_utc", lambda: datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc))
    mock_gorelo.on("GET", "/v1/clients/7", in_order(too_many("Thu, 01 Oct 2026 12:00:03 GMT"), envelope({"Id": 7})))
    async with client_factory() as client:
        await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert fake_sleep.delays == [3.0]


async def test_a_retry_after_date_in_the_past_means_retry_now(client_factory, mock_gorelo, fake_sleep, monkeypatch):
    monkeypatch.setattr(gorelo_client, "_now_utc", lambda: datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc))
    mock_gorelo.on("GET", "/v1/clients/7", in_order(too_many("Thu, 01 Oct 2026 11:00:00 GMT"), envelope({"Id": 7})))
    async with client_factory() as client:
        await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert fake_sleep.delays == [0.0]


@pytest.mark.parametrize(
    "body, expected",
    [
        ({"retry_after": 3}, 3.0),
        ({"retry_after": 1.5}, 1.5),
        ({"RetryAfter": 4}, 4.0),
        ({"retry_after": "2s"}, 2.0),
        ({"retry_after": "1.5s"}, 1.5),
        ({"RetryAfter": "500ms"}, 0.5),
        ({"retry_after": "7"}, 7.0),
        ({"retry_after": "00:00:05"}, 5.0),
        ({"DataContext": {"RetryAfter": 6}}, 6.0),
        ({"Data": {"retry_after": "8s"}}, 8.0),
        ({"retry_after": "soon"}, 1.0),
        ({"retry_after": True}, 1.0),
        ({"retry_after": -4}, 0.0),
        ({"message": "no hint at all"}, 1.0),
    ],
)
async def test_retry_after_in_the_body(client_factory, mock_gorelo, fake_sleep, body, expected):
    mock_gorelo.on("GET", "/v1/clients/7", in_order(too_many(body=body), envelope({"Id": 7})))
    async with client_factory() as client:
        await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert fake_sleep.delays == [expected]


async def test_a_429_with_no_hint_waits_one_second(client_factory, mock_gorelo, fake_sleep):
    mock_gorelo.on("GET", "/v1/clients/7", in_order(httpx.Response(429, text="Too Many Requests"), envelope({"Id": 7})))
    async with client_factory() as client:
        await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert fake_sleep.delays == [1.0]


async def test_the_header_wins_over_the_body_and_a_bad_header_falls_back(client_factory, mock_gorelo, fake_sleep):
    mock_gorelo.on(
        "GET", "/v1/clients/7",
        in_order(too_many(2, {"retry_after": 9}), too_many("garbage", {"retry_after": "3s"}), envelope({"Id": 7})),
    )
    async with client_factory() as client:
        await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert fake_sleep.delays == [2.0, 3.0]


async def test_writes_are_retried_on_429_too_because_gorelo_did_not_process_them(client_factory, mock_gorelo, fake_sleep):
    mock_gorelo.on("POST", "/v1/clients", in_order(too_many(1), envelope({"Id": 5})))
    async with client_factory() as client:
        data = await client.post(CREATE_CLIENT, json_body={"Name": "x", "Location": {"Name": "y"}}, tool="create_client")
    assert data == {"Id": 5} and len(mock_gorelo.requests) == 2 and fake_sleep.delays == [1.0]
    assert mock_gorelo.requests[0].json == mock_gorelo.requests[1].json


async def test_429_gives_up_after_the_retry_limit(client_factory, mock_gorelo, fake_sleep):
    mock_gorelo.on("GET", "/v1/clients/7", too_many(1))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="get_client")
    err = info.value
    assert err.kind == "rate_limit" and err.status == 429 and err.write_unconfirmed is False
    assert len(mock_gorelo.requests) == 4 and fake_sleep.delays == [1.0, 1.0, 1.0]


async def test_429_gives_up_when_the_total_wait_would_pass_the_budget(client_factory, mock_gorelo, fake_sleep):
    mock_gorelo.on("GET", "/v1/clients/7", too_many(10))
    async with client_factory(max_429_wait=20.0) as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert info.value.kind == "rate_limit"
    assert fake_sleep.delays == [10.0, 10.0] and len(mock_gorelo.requests) == 3


async def test_a_single_huge_retry_after_is_not_waited_for(client_factory, mock_gorelo, fake_sleep):
    mock_gorelo.on("GET", "/v1/clients/7", too_many(3600))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert info.value.kind == "rate_limit" and fake_sleep.delays == [] and len(mock_gorelo.requests) == 1


async def test_retries_can_be_switched_off(client_factory, mock_gorelo, fake_sleep):
    mock_gorelo.on("GET", "/v1/clients/7", too_many(1))
    async with client_factory(max_429_retries=0) as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert info.value.kind == "rate_limit" and fake_sleep.delays == [] and len(mock_gorelo.requests) == 1


async def test_a_429_that_clears_up_after_two_retries(client_factory, mock_gorelo, fake_sleep):
    mock_gorelo.on("GET", "/v1/clients/7", in_order(too_many(1), too_many(2), envelope({"Id": 7})))
    async with client_factory() as client:
        assert await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t") == {"Id": 7}
    assert fake_sleep.delays == [1.0, 2.0]


async def test_the_semaphore_is_free_while_a_429_wait_sleeps(client_factory, mock_gorelo):
    holder = {}

    async def sleep(delay):
        holder["locked_during_sleep"] = holder["client"]._semaphore.locked()

    mock_gorelo.on("GET", "/v1/clients/7", in_order(too_many(1), envelope({"Id": 7})))
    async with client_factory(max_concurrency=1, sleep=sleep) as client:
        holder["client"] = client
        await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert holder["locked_during_sleep"] is False


# --------------------------------------------------------------------------
# Timeouts and connection errors: never retried, writes flagged unconfirmed
# --------------------------------------------------------------------------


async def test_a_get_timeout_is_reported_and_not_retried(client_factory, mock_gorelo, fake_sleep):
    mock_gorelo.on("GET", "/v1/clients/7", httpx.ReadTimeout("timed out"))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="get_client")
    err = info.value
    assert err.kind == "timeout" and err.write_unconfirmed is False and err.status is None
    assert len(mock_gorelo.requests) == 1 and fake_sleep.delays == []


async def test_a_get_connection_error_is_a_transport_error(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/7", httpx.ConnectError("refused"))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert info.value.kind == "transport" and info.value.write_unconfirmed is False
    assert len(mock_gorelo.requests) == 1


async def test_a_post_timeout_is_not_retried_and_is_marked_write_unconfirmed(client_factory, mock_gorelo, fake_sleep):
    mock_gorelo.on("POST", "/v1/clients", httpx.ReadTimeout("timed out"))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.post(CREATE_CLIENT, json_body={"Name": "x", "Location": {"Name": "y"}}, tool="create_client")
    err = info.value
    assert err.kind == "timeout" and err.write_unconfirmed is True
    assert "did not confirm the write" in str(err) and "may or may not" in str(err) and "Verify with a read" in str(err)
    assert len(mock_gorelo.requests) == 1 and fake_sleep.delays == []


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c: c.patch("PATCH /v1/time-entries/{timeEntryId}", path_params={"timeEntryId": 1}, json_body={"Comment": "x"}, tool="t"), id="patch"),
        pytest.param(lambda c: c.delete("DELETE /v1/time-entries/{timeEntryId}", path_params={"timeEntryId": 1}, tool="t"), id="delete"),
        pytest.param(lambda c: c.post_multipart("POST /v1/attachments", files={"file": ("a", b"x")}, form={"itemType": "Ticket"}, tool="t"), id="multipart"),
    ],
)
@pytest.mark.parametrize("failure, kind", [(httpx.ConnectError("refused"), "transport"), (httpx.WriteTimeout("slow"), "timeout")])
async def test_other_writes_that_hit_a_transport_error_are_unconfirmed(client_factory, mock_gorelo, call, failure, kind):
    for method, path in (("PATCH", "/v1/time-entries/1"), ("DELETE", "/v1/time-entries/1"), ("POST", "/v1/attachments")):
        mock_gorelo.on(method, path, failure)
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await call(client)
    assert info.value.kind == kind and info.value.write_unconfirmed is True and len(mock_gorelo.requests) == 1


WRITE_CALLS = {
    "POST": (
        "/v1/clients",
        lambda c: c.post(CREATE_CLIENT, json_body={"Name": "x", "Location": {"Name": "y"}}, tool="create_client"),
    ),
    "PATCH": (
        "/v1/time-entries/55",
        lambda c: c.patch("PATCH /v1/time-entries/{timeEntryId}", path_params={"timeEntryId": 55}, json_body={"Comment": "x"}, tool="update_time_entry"),
    ),
    "DELETE": (
        "/v1/time-entries/55",
        lambda c: c.delete("DELETE /v1/time-entries/{timeEntryId}", path_params={"timeEntryId": 55}, tool="delete_time_entry"),
    ),
}

FAILURES = {
    "timeout": (httpx.ReadTimeout("slow"), "timeout"),
    "transport": (httpx.ConnectError("refused"), "transport"),
    "503-with-retry-after-header": (
        httpx.Response(503, headers={"Retry-After": "1"}, json=error_envelope(503, [("070001", "unavailable")])),
        "http",
    ),
    "503-with-retry-after-in-the-body": (
        httpx.Response(503, json={"retry_after": 1, "Message": "unavailable"}, headers={"Retry-After": "2"}),
        "shape",
    ),
}


@pytest.mark.parametrize("failure_name", sorted(FAILURES))
@pytest.mark.parametrize("method", sorted(WRITE_CALLS))
async def test_a_failed_post_patch_or_delete_is_sent_exactly_once_and_never_waits(
    client_factory, mock_gorelo, fake_sleep, method, failure_name
):
    # Only a 429 is retried. A timeout, a lost connection and even a 503 that says Retry-After may have
    # reached Gorelo, so repeating the write could apply it twice.
    path, call = WRITE_CALLS[method]
    failure, kind = FAILURES[failure_name]
    mock_gorelo.on(method, path, failure)
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await call(client)
    assert info.value.kind == kind and info.value.write_unconfirmed is True
    assert len(mock_gorelo.requests) == 1 and mock_gorelo.requests[0].method == method
    assert fake_sleep.delays == []


@pytest.mark.parametrize("failure_name", sorted(FAILURES))
async def test_a_failed_read_is_not_retried_either(client_factory, mock_gorelo, fake_sleep, failure_name):
    failure, _ = FAILURES[failure_name]
    mock_gorelo.on("GET", "/v1/clients/7", failure)
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="get_client")
    assert info.value.write_unconfirmed is False
    assert len(mock_gorelo.requests) == 1 and fake_sleep.delays == []


# --------------------------------------------------------------------------
# the PDF export is a GET that Gorelo records, so it fails like a write
# --------------------------------------------------------------------------


def _is_unconfirmed_export(err):
    return (
        err.write_unconfirmed is True
        and EXPORT_NOTE in str(err)
        and "may already be recorded" in str(err)
        and "retry records another export event" in str(err)
    )


@pytest.mark.parametrize("failure, kind", [(httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectError("refused"), "transport")])
async def test_a_pdf_export_that_times_out_or_loses_the_connection_may_already_be_recorded(
    client_factory, mock_gorelo, fake_sleep, failure, kind
):
    mock_gorelo.on("GET", PDF_PATH, failure)
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=1000, tool="export_invoice_pdf")
    assert info.value.kind == kind and _is_unconfirmed_export(info.value), str(info.value)
    assert "nothing was changed" not in str(info.value)
    assert len(mock_gorelo.requests) == 1 and fake_sleep.delays == []  # never retried


@pytest.mark.parametrize(
    "response, kind",
    [
        pytest.param(httpx.Response(500, json=error_envelope(500, [("070001", "boom")])), "http", id="500-envelope"),
        pytest.param(httpx.Response(503, headers={"Retry-After": "1"}, json=error_envelope(503, [("070001", "busy")])), "http", id="503-envelope"),
        pytest.param(httpx.Response(502, text="<html>Bad Gateway</html>"), "shape", id="502-gateway-page"),
        pytest.param(httpx.Response(200, text="<html>gateway</html>", headers={"content-type": "text/html"}), "shape", id="200-html"),
        pytest.param(httpx.Response(200, text="hello", headers={"content-type": "text/plain"}), "shape", id="200-plain-text"),
        pytest.param(httpx.Response(200, json=envelope({"Surprise": True})), "shape", id="200-json-envelope"),
        pytest.param(httpx.Response(200, content=b"", headers={"content-type": "application/pdf"}), "shape", id="200-empty-pdf"),
    ],
)
async def test_every_unreadable_or_failed_pdf_answer_is_unconfirmed_and_says_so(client_factory, mock_gorelo, fake_sleep, response, kind):
    mock_gorelo.on("GET", PDF_PATH, response)
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=1000, tool="export_invoice_pdf")
        assert info.value.kind == kind and _is_unconfirmed_export(info.value), str(info.value)
    assert len(mock_gorelo.requests) == 1 and fake_sleep.delays == []


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(200, text="<html>gateway</html>", headers={"content-type": "text/html"}), id="200-html"),
        pytest.param(httpx.Response(502, text="<html>Bad Gateway</html>"), id="502-gateway-page"),
        pytest.param(httpx.Response(200, content=b"", headers={"content-type": "application/pdf"}), id="200-empty-pdf"),
    ],
)
async def test_the_low_level_request_marks_the_pdf_export_unconfirmed_too(client_factory, mock_gorelo, response):
    mock_gorelo.on("GET", PDF_PATH, response)
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.request(PDF_OP, path_params={"invoiceId": INVOICE}, tool="export_invoice_pdf", binary=True, max_bytes=1000)
    assert _is_unconfirmed_export(info.value), str(info.value)


async def test_a_pdf_larger_than_the_cap_is_unconfirmed_too(client_factory, mock_gorelo):
    mock_gorelo.on("GET", PDF_PATH, pdf_response(b"x" * 50))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=10, tool="t")
    assert info.value.kind == "shape" and "10 byte cap" in str(info.value) and _is_unconfirmed_export(info.value)


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(error_envelope(404, [("070401", "Invoice not found")]), id="404"),
        pytest.param(error_envelope(400, [("070101", "bad")]), id="400"),
        pytest.param(error_envelope(403, [("080203", "API key does not have 'Billing' scope")]), id="403-scope"),
        pytest.param(error_envelope(409, [("070901", "blocked")]), id="409"),
        pytest.param(httpx.Response(429, headers={"Retry-After": "100000"}, json={"Message": "slow"}), id="429-gives-up"),
    ],
)
async def test_a_pdf_request_that_gorelo_clearly_refused_is_not_unconfirmed(client_factory, mock_gorelo, response):
    mock_gorelo.on("GET", PDF_PATH, response)
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=1000, tool="t")
    assert info.value.write_unconfirmed is False and EXPORT_NOTE not in str(info.value)


async def test_a_successful_pdf_download_carries_no_warning(client_factory, mock_gorelo):
    mock_gorelo.on("GET", PDF_PATH, pdf_response(b"%PDF-1.7 ok"))
    async with client_factory() as client:
        result = await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=1000, tool="t")
    assert result.content == b"%PDF-1.7 ok"


# --------------------------------------------------------------------------
# A placeholder rename cannot turn the PDF export back into a plain read (compared by shape, like FORBIDDEN_OPS)
# --------------------------------------------------------------------------

SPEC_INDEX_FILE = Path(__file__).resolve().parent.parent / "spec" / "spec_index.json"


def spec_with_renamed_pdf_placeholder(name):
    """(spec, key): the published spec with the PDF export spelled with another placeholder name, which is what a
    Gorelo rename (thirteen operations on 2026-10-02) would produce, and the operation key it now has."""
    data = json.loads(SPEC_INDEX_FILE.read_text(encoding="utf-8"))
    entry = data["ops"].pop(PDF_OP)
    entry["path"] = renamed(entry["path"], name)
    (old_name,) = entry["path_params"]
    entry["path_params"] = {name: entry["path_params"][old_name]}
    key = renamed(PDF_OP, name)
    data["ops"][key] = entry
    return SpecIndex(data), key


def test_the_shape_of_the_pdf_export_is_what_is_compared():
    assert normalize_op_key(PDF_OP) == "GET /v1/invoices/{}/pdf"
    assert {normalize_op_key(key) for key in SIDE_EFFECT_GETS} == {"GET /v1/invoices/{}/pdf"}


@pytest.mark.parametrize("name", ["id", "x", "invoiceId", "someOtherName", "ID"])
def test_a_side_effect_get_is_one_whatever_its_placeholder_is_called(name):
    for key in SIDE_EFFECT_GETS:
        assert is_side_effect_get(key)
        assert is_side_effect_get(renamed(key, name))


def test_is_side_effect_get_is_false_for_everything_else():
    for other in (
        "GET /v1/invoices/{invoiceId}", "GET /v1/invoices", "GET /v1/invoices/{invoiceId}/pdf/extra",
        "GET /v1/invoices/pdf", "GET /v1/invoices/{invoiceId}/attachments", "POST /v1/invoices/{invoiceId}/pdf",
        "DELETE /v1/invoices/{invoiceId}", "GET /v1/clients/{clientId}", "GET /v1/pdf",
    ):
        assert not is_side_effect_get(other), other
        assert not is_side_effect_get(renamed(other, "id")), other
    for not_a_key in (None, 5, [PDF_OP], PDF_OP.encode()):
        assert is_side_effect_get(not_a_key) is False


def test_the_side_effect_gets_in_the_live_spec_are_exactly_side_effect_gets(spec_index):
    """Every operation of the spec whose shape is a side-effect GET is one of SIDE_EFFECT_GETS as the spec spells it
    today: a placeholder rename would show up here (the spec key and the SIDE_EFFECT_GETS key would differ)."""
    assert {key for key in spec_index.ops if is_side_effect_get(key)} == set(SIDE_EFFECT_GETS)


def test_the_renamed_spec_of_these_tests_really_has_another_key():
    spec, key = spec_with_renamed_pdf_placeholder("documentId")
    assert key == "GET /v1/invoices/{documentId}/pdf" and key not in SIDE_EFFECT_GETS and PDF_OP not in spec.ops
    assert spec.op(key).path_placeholders == ("documentId",) and spec.op(key).is_binary


# (answer, error kind, the max_bytes the call is made with): the two "larger than the cap" answers need a small cap
RENAMED_PDF_ANSWERS = [
    pytest.param(httpx.Response(500, json=error_envelope(500, [("070001", "boom")])), "http", 1000, id="500-envelope"),
    pytest.param(httpx.Response(502, text="<html>Bad Gateway</html>"), "shape", 1000, id="502-gateway-page"),
    pytest.param(
        httpx.Response(200, text="<html>gateway</html>", headers={"content-type": "text/html"}), "shape", 1000, id="200-html"
    ),
    pytest.param(httpx.Response(200, json=envelope({"Surprise": True})), "shape", 1000, id="200-json-envelope"),
    pytest.param(
        httpx.Response(200, content=b"", headers={"content-type": "application/pdf"}), "shape", 1000, id="200-empty-pdf"
    ),
    pytest.param(pdf_response(b"x" * 50), "shape", 10, id="a-pdf-larger-than-the-cap"),
    pytest.param(
        httpx.Response(200, content=b"y" * 50, headers={"content-type": "text/html"}), "shape", 10, id="a-page-larger-than-the-cap"
    ),
]


@pytest.mark.parametrize("name", ["id", "documentId"])
@pytest.mark.parametrize("failure, kind", [(httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectError("refused"), "transport")])
async def test_a_renamed_pdf_export_that_times_out_or_loses_the_connection_may_still_be_recorded(
    client_factory, mock_gorelo, fake_sleep, name, failure, kind
):
    spec, key = spec_with_renamed_pdf_placeholder(name)
    mock_gorelo.on("GET", PDF_PATH, failure)
    async with client_factory(spec=spec) as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_binary(key, path_params={name: INVOICE}, max_bytes=1000, tool="export_invoice_pdf")
    assert info.value.kind == kind and _is_unconfirmed_export(info.value), str(info.value)
    assert "nothing was changed" not in str(info.value)  # the wording of a harmless read
    assert len(mock_gorelo.requests) == 1 and fake_sleep.delays == []  # never retried


@pytest.mark.parametrize("name", ["id", "documentId"])
@pytest.mark.parametrize("response, kind, limit", RENAMED_PDF_ANSWERS)
async def test_every_unreadable_or_failed_answer_to_a_renamed_pdf_export_is_unconfirmed_and_says_so(
    client_factory, mock_gorelo, fake_sleep, name, response, kind, limit
):
    spec, key = spec_with_renamed_pdf_placeholder(name)
    mock_gorelo.on("GET", PDF_PATH, response)
    async with client_factory(spec=spec) as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_binary(key, path_params={name: INVOICE}, max_bytes=limit, tool="export_invoice_pdf")
        assert info.value.kind == kind and _is_unconfirmed_export(info.value), str(info.value)
    assert len(mock_gorelo.requests) == 1 and fake_sleep.delays == []


# request() hands a 200 JSON envelope back as it is; it is get_binary that refuses it, so that answer is not one of these
@pytest.mark.parametrize("response, kind, limit", [a for a in RENAMED_PDF_ANSWERS if a.id != "200-json-envelope"])
async def test_the_low_level_request_marks_a_renamed_pdf_export_unconfirmed_too(
    client_factory, mock_gorelo, response, kind, limit
):
    spec, key = spec_with_renamed_pdf_placeholder("documentId")
    mock_gorelo.on("GET", PDF_PATH, response)
    async with client_factory(spec=spec) as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.request(
                key, path_params={"documentId": INVOICE}, tool="export_invoice_pdf", binary=True, max_bytes=limit
            )
    assert info.value.kind == kind and _is_unconfirmed_export(info.value), str(info.value)


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(error_envelope(404, [("070401", "Invoice not found")]), id="404"),
        pytest.param(error_envelope(400, [("070101", "bad")]), id="400"),
        pytest.param(error_envelope(403, [("080203", "API key does not have 'Billing' scope")]), id="403-scope"),
    ],
)
async def test_a_renamed_pdf_export_that_gorelo_clearly_refused_is_not_unconfirmed(client_factory, mock_gorelo, response):
    spec, key = spec_with_renamed_pdf_placeholder("documentId")
    mock_gorelo.on("GET", PDF_PATH, response)
    async with client_factory(spec=spec) as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_binary(key, path_params={"documentId": INVOICE}, max_bytes=1000, tool="t")
    assert info.value.write_unconfirmed is False and EXPORT_NOTE not in str(info.value)


async def test_a_successful_renamed_pdf_download_carries_no_warning(client_factory, mock_gorelo):
    spec, key = spec_with_renamed_pdf_placeholder("documentId")
    mock_gorelo.on("GET", PDF_PATH, pdf_response(b"%PDF-1.7 ok"))
    async with client_factory(spec=spec) as client:
        result = await client.get_binary(key, path_params={"documentId": INVOICE}, max_bytes=1000, tool="t")
    assert result.content == b"%PDF-1.7 ok"


async def test_an_ordinary_read_whose_placeholder_is_renamed_is_still_just_a_read(client_factory, mock_gorelo):
    # the shape rule must not turn other GETs into side-effect GETs: a failed invoice read is still harmless
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", httpx.ReadTimeout("slow"))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one("GET /v1/invoices/{invoiceId}", path_params={"invoiceId": INVOICE}, tool="get_invoice")
    assert info.value.write_unconfirmed is False and EXPORT_NOTE not in str(info.value)
    assert "this was a read, nothing was changed" in str(info.value)


async def test_exceptions_that_are_not_http_errors_pass_through_untouched(client_factory, mock_gorelo):
    class Guard(Exception):
        pass

    async def guard(request):
        raise Guard("blocked by the harness")

    async with client_factory(event_hooks={"request": [guard]}) as client:
        with pytest.raises(Guard):
            await client.post(CREATE_CLIENT, json_body={"Name": "x", "Location": {"Name": "y"}}, tool="t")
    assert mock_gorelo.requests == []  # the hook ran before anything was sent


async def test_event_hooks_see_requests_and_responses(client_factory, mock_gorelo):
    seen = []

    async def on_request(request):
        seen.append(("request", request.method, request.url.path))

    async def on_response(response):
        seen.append(("response", response.status_code))

    mock_gorelo.on("GET", "/v1/clients/7", envelope({"Id": 7}))
    async with client_factory(event_hooks={"request": [on_request], "response": [on_response]}) as client:
        await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert seen == [("request", "GET", "/v1/clients/7"), ("response", 200)]


# --------------------------------------------------------------------------
# Binary downloads
# --------------------------------------------------------------------------


async def test_binary_download_returns_bytes_filename_and_content_type(client_factory, mock_gorelo):
    mock_gorelo.on(
        "GET", PDF_PATH,
        pdf_response(b"%PDF-1.7 hello", **{"content-disposition": 'attachment; filename="INV-0042.pdf"'}),
    )
    async with client_factory() as client:
        result = await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=1000, tool="export_invoice_pdf")
    assert result == BinaryResult(content=b"%PDF-1.7 hello", filename="INV-0042.pdf", content_type="application/pdf")
    assert mock_gorelo.last.headers["accept"] == "application/pdf, application/json"


@pytest.mark.parametrize(
    "disposition, expected",
    [
        ('attachment; filename="inv 12.pdf"', "inv 12.pdf"),
        ("attachment; filename=plain.pdf", "plain.pdf"),
        ("attachment; filename*=UTF-8''caf%C3%A9.pdf", "caf\u00e9.pdf"),
        ('attachment; filename="../../etc/passwd"', "passwd"),
        ('attachment; filename="C:\\temp\\x.pdf"', "x.pdf"),
        ("inline", None),
        ("", None),
    ],
)
async def test_the_filename_comes_from_content_disposition_without_path_parts(client_factory, mock_gorelo, disposition, expected):
    headers = {"content-disposition": disposition} if disposition else {}
    mock_gorelo.on("GET", PDF_PATH, pdf_response(**headers))
    async with client_factory() as client:
        result = await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=1000, tool="t")
    assert result.filename == expected


async def test_binary_without_a_disposition_has_no_filename(client_factory, mock_gorelo):
    mock_gorelo.on("GET", PDF_PATH, pdf_response())
    async with client_factory() as client:
        result = await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=1000, tool="t")
    assert result.filename is None and result.content_type == "application/pdf"


async def test_max_bytes_is_enforced_from_the_declared_length(client_factory, mock_gorelo):
    mock_gorelo.on("GET", PDF_PATH, pdf_response(b"x" * 50))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=10, tool="t")
    assert info.value.kind == "shape" and "10 byte cap" in str(info.value) and "max_bytes" in str(info.value)


async def test_max_bytes_is_enforced_while_streaming_when_no_length_is_declared(client_factory):
    chunks_read = []

    async def body():
        for index in range(100):
            chunks_read.append(index)
            yield b"y" * 6

    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=body())

    async with client_factory(httpx.MockTransport(handler)) as client:
        with pytest.raises(GoreloAPIError, match="10 byte cap") as info:
            await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=10, tool="t")
    assert info.value.kind == "shape"
    assert len(chunks_read) < 100  # it stopped reading instead of buffering everything


async def test_a_download_exactly_at_the_cap_is_fine(client_factory, mock_gorelo):
    mock_gorelo.on("GET", PDF_PATH, pdf_response(b"z" * 10))
    async with client_factory() as client:
        result = await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=10, tool="t")
    assert len(result.content) == 10


async def test_an_empty_download_is_an_error(client_factory, mock_gorelo):
    mock_gorelo.on("GET", PDF_PATH, pdf_response(b""))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError, match="download is empty"):
            await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=10, tool="t")


async def test_an_enveloped_error_for_a_binary_op_is_raised_as_usual(client_factory, mock_gorelo):
    mock_gorelo.on("GET", PDF_PATH, error_envelope(404, [("070401", "Invoice not found")]))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=1000, tool="t")
    assert info.value.kind == "http" and info.value.status == 404 and info.value.trace_id == TEST_TRACE_ID


async def test_a_json_success_for_a_binary_op_is_a_shape_error_in_get_binary(client_factory, mock_gorelo):
    mock_gorelo.on("GET", PDF_PATH, envelope({"Surprise": True}))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError, match="expected a file download") as info:
            await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=1000, tool="t")
        assert info.value.kind == "shape"
        raw = await client.request(PDF_OP, path_params={"invoiceId": INVOICE}, tool="t", binary=True, max_bytes=1000)
        assert raw["Data"] == {"Surprise": True}


async def test_request_binary_defaults_to_a_safe_cap(client_factory, mock_gorelo):
    mock_gorelo.on("GET", PDF_PATH, pdf_response(b"ok"))
    async with client_factory() as client:
        result = await client.request(PDF_OP, path_params={"invoiceId": INVOICE}, tool="t", binary=True)
        assert result.content == b"ok"
        with pytest.raises(ValueError, match="max_bytes"):
            await client.request(PDF_OP, path_params={"invoiceId": INVOICE}, tool="t", binary=True, max_bytes=0)


@pytest.mark.parametrize(
    "content_type, body",
    [
        pytest.param("text/html", b"<html>sign in</html>", id="html"),
        pytest.param("text/plain", b"%PDF-1.7 pretending to be a pdf", id="plain-text"),
        pytest.param("text/html; charset=utf-8", b"<html/>", id="html-with-charset"),
        pytest.param("application/octet-stream", b"%PDF-1.7 x", id="octet-stream"),
        pytest.param("image/png", b"\x89PNG", id="png"),
        pytest.param(None, b"%PDF-1.7 no content type at all", id="no-content-type"),
    ],
)
async def test_a_2xx_of_an_unexpected_content_type_is_never_a_download(client_factory, mock_gorelo, content_type, body):
    headers = {"content-type": content_type} if content_type else {}
    mock_gorelo.on("GET", PDF_PATH, httpx.Response(200, content=body, headers=headers))
    async with client_factory() as client:
        for call in (
            client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=1000, tool="export_invoice_pdf"),
            client.request(PDF_OP, path_params={"invoiceId": INVOICE}, tool="t", binary=True, max_bytes=1000),
        ):
            with pytest.raises(GoreloAPIError) as info:
                await call
            err = info.value
            assert err.kind == "shape" and err.status == 200
            assert "unexpected response shape; refusing to guess" in str(err)
            assert "application/pdf was expected" in str(err)
            assert "pretending" not in str(err) and "sign in" not in str(err)  # the body is never quoted


async def test_a_body_of_an_unexpected_content_type_is_read_through_the_cap_while_streaming(client_factory):
    # application/octet-stream is not an expected download, so it is refused as an unexpected shape,
    # but after reading at most max_bytes (the same capped reader as a download), not after buffering it all
    chunks_read = []

    async def body():
        for index in range(100):
            chunks_read.append(index)
            yield b"y" * 6

    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=body())

    async with client_factory(httpx.MockTransport(handler)) as client:
        for call in (
            lambda: client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=10, tool="export_invoice_pdf"),
            lambda: client.request(PDF_OP, path_params={"invoiceId": INVOICE}, tool="t", binary=True, max_bytes=10),
        ):
            chunks_read.clear()
            with pytest.raises(GoreloAPIError) as info:
                await call()
            err = info.value
            assert err.kind == "shape" and err.status == 200
            assert "unexpected response shape; refusing to guess" in str(err)
            assert "content-type application/octet-stream" in str(err) and "10 byte cap (max_bytes)" in str(err)
            assert "application/pdf was expected" in str(err) and "yyyyyy" not in str(err)
            assert _is_unconfirmed_export(err), str(err)  # a 2xx answer to the PDF export: it may be recorded
            assert 0 < len(chunks_read) < 100  # it read up to the cap and stopped instead of buffering everything


async def test_a_declared_length_over_the_cap_refuses_an_unexpected_body_before_reading_any_of_it(client_factory):
    chunks_read = []

    async def body():
        chunks_read.append(0)
        yield b"y" * 6

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html", "content-length": "5000"}, content=body())

    async with client_factory(httpx.MockTransport(handler)) as client:
        with pytest.raises(GoreloAPIError, match=r"10 byte cap \(max_bytes\)") as info:
            await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=10, tool="t")
    assert info.value.kind == "shape" and "refusing to guess" in str(info.value)
    assert chunks_read == []  # the declared Content-Length was enough: not a byte was read


@pytest.mark.parametrize(
    "status, content_type, recorded",
    [
        pytest.param(200, "text/html", True, id="200-html"),
        pytest.param(502, "text/html", True, id="502-gateway-page"),
        pytest.param(404, "application/json", False, id="404-refused-so-nothing-recorded"),
        pytest.param(403, "text/html", False, id="403-refused-so-nothing-recorded"),
    ],
)
async def test_an_oversized_unexpected_answer_is_refused_unread_and_flagged_like_a_small_one(
    client_factory, status, content_type, recorded
):
    # the same rule as for a small body (see test_every_unreadable_or_failed_pdf_answer_is_unconfirmed...):
    # a 2xx or 5xx answer to the PDF export may have been recorded, a 4xx refusal was not
    chunks_read = []

    async def body():
        for index in range(100):
            chunks_read.append(index)
            yield b"z" * 6

    def handler(request):
        return httpx.Response(status, headers={"content-type": content_type}, content=body())

    async with client_factory(httpx.MockTransport(handler)) as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=10, tool="export_invoice_pdf")
    err = info.value
    assert err.kind == "shape" and err.status == status and f"HTTP {status}" in str(err)
    assert "10 byte cap (max_bytes)" in str(err) and "zzzzzz" not in str(err)
    assert err.write_unconfirmed is recorded and (EXPORT_NOTE in str(err)) is recorded
    assert ("a download of type application/pdf was expected" in str(err)) is (200 <= status < 300)
    assert len(chunks_read) < 100


async def test_the_cap_covers_an_enveloped_error_answer_of_a_binary_request_too(client_factory, mock_gorelo):
    # every body of a binary request goes through the cap: a body exactly at the cap is read and parsed as
    # usual, one byte over is refused unread (an error envelope is a few hundred bytes, the cap is megabytes)
    response = httpx.Response(404, json=error_envelope(404, [("070401", "Invoice not found")]))
    size = len(response.content)
    mock_gorelo.on("GET", PDF_PATH, response)
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=size, tool="t")
        assert info.value.kind == "http" and info.value.status == 404 and info.value.trace_id == TEST_TRACE_ID
        with pytest.raises(GoreloAPIError) as info:
            await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=size - 1, tool="t")
        assert info.value.kind == "shape" and info.value.status == 404 and info.value.write_unconfirmed is False
        assert f"{size - 1} byte cap (max_bytes)" in str(info.value)


async def test_the_expected_content_types_decide_what_a_download_is(client_factory, mock_gorelo):
    for number, content_type in ((11, "Application/PDF"), (12, "application/pdf; charset=binary"), (13, " application/pdf ")):
        mock_gorelo.on("GET", f"/v1/invoices/{uid(number)}/pdf", httpx.Response(200, content=b"%PDF", headers={"content-type": content_type}))
    mock_gorelo.on("GET", f"/v1/invoices/{uid(14)}/pdf", httpx.Response(200, content=b"%PDF", headers={"content-type": "application/octet-stream"}))
    async with client_factory() as client:
        for number in (11, 12, 13):  # case and parameters do not matter
            result = await client.get_binary(PDF_OP, path_params={"invoiceId": uid(number)}, max_bytes=100, tool="t")
            assert result.content == b"%PDF"
        with pytest.raises(GoreloAPIError, match="refusing to guess"):  # octet-stream is not in the default
            await client.get_binary(PDF_OP, path_params={"invoiceId": uid(14)}, max_bytes=100, tool="t")
        both = ("application/pdf", "application/octet-stream")
        result = await client.get_binary(PDF_OP, path_params={"invoiceId": uid(14)}, max_bytes=100, expected_content_types=both, tool="t")
        assert result.content_type == "application/octet-stream"
        assert mock_gorelo.last.headers["accept"] == "application/octet-stream, application/pdf, application/json"  # follows the list
        only_other = ("application/octet-stream",)
        with pytest.raises(GoreloAPIError, match="refusing to guess"):  # and a pdf is not accepted when it is not listed
            await client.get_binary(PDF_OP, path_params={"invoiceId": uid(11)}, max_bytes=100, expected_content_types=only_other, tool="t")


@pytest.mark.parametrize("bad", ["application/pdf", b"application/pdf", [], (), ["pdf"], ["application/pdf", 5], [""], ["application/"], None])
async def test_expected_content_types_must_be_a_list_of_content_types(client_factory, mock_gorelo, bad):
    async with client_factory() as client:
        with pytest.raises(ValueError, match="expected_content_types"):
            await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=100, expected_content_types=bad, tool="t")  # type: ignore[arg-type]
    assert mock_gorelo.requests == []


async def test_a_json_envelope_answer_to_a_binary_request_still_parses_as_an_envelope(client_factory, mock_gorelo):
    mock_gorelo.on("GET", PDF_PATH, error_envelope(404, [("070401", "Invoice not found")]))
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_binary(PDF_OP, path_params={"invoiceId": INVOICE}, max_bytes=1000, tool="t")
    assert info.value.kind == "http" and info.value.status == 404


# --------------------------------------------------------------------------
# Logging: names and numbers only, never values
# --------------------------------------------------------------------------


async def test_log_records_never_contain_values(client_factory, mock_gorelo, caplog, fake_sleep):
    caplog.set_level(logging.DEBUG)
    mock_gorelo.on("GET", "/v1/tickets", envelope([{"Title": SECRET}], pagination(None, 1)))
    mock_gorelo.on("POST", "/v1/clients", in_order(too_many(1), envelope({"Name": SECRET, "Id": 1})))
    mock_gorelo.on("PATCH", "/v1/contacts/1", error_envelope(400, [("070101", f"bad value {SECRET}", "MobilePhone")]))
    mock_gorelo.on("POST", "/v1/alerts", httpx.ReadTimeout("t"))
    async with client_factory() as client:
        await client.get_page("GET /v1/tickets", query={"Query": SECRET, "StatusIds": [SECRET]}, tool="list_tickets")
        await client.post(
            CREATE_CLIENT,
            json_body={"Name": SECRET, "Location": {"Name": SECRET, "Phone": SECRET}, "Domain": SECRET},
            tool="create_client",
        )
        with pytest.raises(GoreloAPIError):
            await client.patch(
                "PATCH /v1/contacts/{contactId}", path_params={"contactId": 1}, json_body={"ClientId": 1, "JobTitle": SECRET}, tool="update_contact"
            )
        with pytest.raises(GoreloAPIError):
            await client.post("POST /v1/alerts", json_body={"Name": SECRET, "ClientId": 1, "Resource": SECRET, "Severity": 1}, tool="post_alert")
    assert caplog.records, "expected some log records"
    for record in caplog.records:
        text = record.getMessage() + " " + " ".join(str(a) for a in (record.args if isinstance(record.args, tuple) else ()))
        assert SECRET not in text, text
        assert TEST_API_KEY not in text, text
    assert not [r for r in caplog.records if r.name.split(".")[0] in ("httpx", "httpcore")]


async def test_one_info_line_per_request_with_names_numbers_and_no_values(client_factory, mock_gorelo, caplog):
    caplog.set_level(logging.INFO, logger="gorelo_client")
    mock_gorelo.on("POST", "/v1/clients", envelope({"Id": 1}))
    mock_gorelo.on("GET", "/v1/tickets", paged_envelope([]))
    async with client_factory() as client:
        await client.post(
            CREATE_CLIENT, json_body={"Name": "n", "Location": {"Name": "l", "Phone": "p"}}, tool="create_client"
        )
        await client.get_page("GET /v1/tickets", query={"statusids": [1], "Query": "q"}, page_size=7, tool="list_tickets")
    lines = [r.getMessage() for r in caplog.records if r.name == "gorelo_client" and r.levelno == logging.INFO]
    assert len(lines) == 2
    post_line, get_line = lines
    for fragment in ("tool=create_client", "op=POST /v1/clients", "path=/v1/clients", "status=200", "attempts=1", "latency_ms=",
                     "query=none", "body=Location,Location.Name,Location.Phone,Name"):
        assert fragment in post_line, (fragment, post_line)
    for fragment in ("tool=list_tickets", "op=GET /v1/tickets", "status=200", "attempts=1", "query=PageSize,Query,StatusIds", "body=none"):
        assert fragment in get_line, (fragment, get_line)


async def test_log_lines_for_failures_and_retries(client_factory, mock_gorelo, caplog, fake_sleep):
    caplog.set_level(logging.INFO, logger="gorelo_client")
    mock_gorelo.on("GET", "/v1/clients/1", in_order(too_many(2), envelope({"Id": 1})))
    mock_gorelo.on("GET", "/v1/clients/2", error_envelope(404, [("070401", "nope")]))
    mock_gorelo.on("GET", "/v1/clients/3", httpx.ReadTimeout("t"))
    async with client_factory() as client:
        await client.get_one(CLIENT_OP, path_params={"clientId": 1}, tool="get_client")
        for ident in (2, 3):
            with pytest.raises(GoreloAPIError):
                await client.get_one(CLIENT_OP, path_params={"clientId": ident}, tool="get_client")
    records = [r for r in caplog.records if r.name == "gorelo_client"]
    warnings = [r.getMessage() for r in records if r.levelno == logging.WARNING]
    infos = [r.getMessage() for r in records if r.levelno == logging.INFO]
    assert len(warnings) == 1 and "429" in warnings[0] and "waiting 2.0s" in warnings[0] and "retry 1 of 3" in warnings[0]
    assert len(infos) == 3
    assert "status=200" in infos[0] and "attempts=2" in infos[0]
    assert "status=404" in infos[1] and "path=/v1/clients/2" in infos[1]
    assert "status=timeout" in infos[2]


async def test_a_forbidden_call_is_logged_as_a_warning(client_factory, caplog):
    caplog.set_level(logging.INFO, logger="gorelo_client")
    client = client_factory()
    with pytest.raises(GoreloAPIError):
        await client.delete("DELETE /v1/clients/{clientId}", path_params={"clientId": 1}, tool="rogue_tool")
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "tool=rogue_tool" in caplog.records[0].getMessage() and "forbidden" in caplog.records[0].getMessage()


def test_creating_a_client_silences_the_httpx_url_logging(spec_index):
    library_logger = logging.getLogger("httpx")
    original = library_logger.level
    try:
        library_logger.setLevel(logging.INFO)
        GoreloClient("k", spec=spec_index)
        assert library_logger.level == logging.WARNING
        library_logger.setLevel(logging.ERROR)
        GoreloClient("k", spec=spec_index)
        assert library_logger.level == logging.ERROR  # never lowered again
    finally:
        library_logger.setLevel(original)


def test_errors_survive_copy_and_pickle():
    import copy
    import pickle

    err = GoreloAPIError(
        "boom", status=409, op_key="DELETE /v1/items/{itemId}", kind="http", trace_id="t-1",
        write_unconfirmed=True, notifications=[{"Code": "070901", "Message": "blocked", "PropertyName": "ItemId"}],
    )
    violation = SpecViolation("POST /v1/clients", "Location.Phonee", "unknown body field")
    for original in (err, violation):
        for clone in (copy.copy(original), pickle.loads(pickle.dumps(original))):
            assert type(clone) is type(original) and str(clone) == str(original)
    clone = pickle.loads(pickle.dumps(err))
    assert (clone.status, clone.op_key, clone.kind, clone.trace_id, clone.write_unconfirmed) == (409, "DELETE /v1/items/{itemId}", "http", "t-1", True)
    assert clone.notifications == [{"code": "070901", "message": "blocked", "property": "ItemId"}]
    clone = pickle.loads(pickle.dumps(violation))
    assert (clone.op_key, clone.field) == ("POST /v1/clients", "Location.Phonee")


def test_a_non_string_message_is_tolerated():
    assert str(GoreloAPIError(404, status=404)) == "404"


# --------------------------------------------------------------------------
# Concurrency and lifecycle
# --------------------------------------------------------------------------


async def test_at_most_max_concurrency_requests_are_in_flight(client_factory):
    state = {"now": 0, "peak": 0}

    async def handler(request):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return httpx.Response(200, json=envelope({"Id": 1}))

    async with client_factory(httpx.MockTransport(handler), max_concurrency=2) as client:
        results = await asyncio.gather(*(client.get_one(CLIENT_OP, path_params={"clientId": i}, tool="t") for i in range(8)))
    assert len(results) == 8 and state["peak"] == 2


async def test_the_default_concurrency_is_four(client_factory):
    state = {"now": 0, "peak": 0}

    async def handler(request):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return httpx.Response(200, json=envelope({"Id": 1}))

    async with client_factory(httpx.MockTransport(handler)) as client:
        await asyncio.gather(*(client.get_one(CLIENT_OP, path_params={"clientId": i}, tool="t") for i in range(12)))
    assert state["peak"] == 4


async def test_lifecycle(client_factory, mock_gorelo):
    client = client_factory()
    assert client.is_closed is True
    with pytest.raises(RuntimeError, match="not started"):
        await client.get_one(CLIENT_OP, path_params={"clientId": 1}, tool="t")
    mock_gorelo.on("GET", "/v1/clients/1", envelope({"Id": 1}))
    async with client as started:
        assert started is client and client.is_closed is False
        with pytest.raises(RuntimeError, match="already started"):
            await client.__aenter__()
        await client.get_one(CLIENT_OP, path_params={"clientId": 1}, tool="t")
    assert client.is_closed is True
    with pytest.raises(RuntimeError, match="already closed"):
        await client.get_one(CLIENT_OP, path_params={"clientId": 1}, tool="t")
    with pytest.raises(RuntimeError, match="already started"):
        await client.__aenter__()  # a closed client cannot be restarted either


def test_constructor_validates_its_arguments(spec_index):
    for bad in ({"api_key": ""}, {"api_key": "  "}):
        with pytest.raises(ValueError, match="api_key"):
            GoreloClient(spec=spec_index, **bad)
    with pytest.raises(ValueError, match="max_concurrency"):
        GoreloClient("k", spec=spec_index, max_concurrency=0)
    with pytest.raises(ValueError, match="max_429"):
        GoreloClient("k", spec=spec_index, max_429_retries=-1)
    with pytest.raises(ValueError, match="timeout"):
        GoreloClient("k", spec=spec_index, timeout=0)
    assert GoreloClient("k", spec=spec_index, base_url="https://x.test/v1/").base_url == "https://x.test/v1"


def test_the_client_loads_the_default_spec_when_none_is_given():
    from spec import load_spec_index

    assert GoreloClient("k").spec is load_spec_index()


# --------------------------------------------------------------------------
# The test helpers themselves (the tool tests build on them)
# --------------------------------------------------------------------------


async def test_mock_gorelo_records_requests_in_detail(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/tickets", paged_envelope([]))
    async with client_factory() as client:
        await client.get_page("GET /v1/tickets", query={"Query": "a b&c", "StatusIds": [1, 2]}, page_size=5, tool="t")
    request = mock_gorelo.last
    assert (request.method, request.path) == ("GET", "/v1/tickets")
    assert request.query == {"Query": "a b&c", "StatusIds": "1,2", "PageSize": "5"}
    assert request.query_names == ["PageSize", "Query", "StatusIds"] and request.query_multi["PageSize"] == ["5"]
    assert request.headers["X-API-Key"] == TEST_API_KEY  # case-insensitive
    assert mock_gorelo.calls("GET", "/v1/tickets") == [request] and mock_gorelo.calls("POST") == []


async def test_mock_gorelo_routes_placeholders_queries_and_sequences(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/{clientId}", envelope({"Id": "any"}))
    mock_gorelo.on("GET", "/v1/clients/7", envelope({"Id": "seven"}))
    mock_gorelo.on("GET", "/v1/tickets", envelope([], pagination(None, 0)))
    mock_gorelo.on("GET", "/v1/tickets", envelope([{"Id": "q"}], pagination(None, 1)), query={"Query": "x"})
    mock_gorelo.on_op("GET /v1/tickets/{ticketId}", envelope({"Id": "t"}))
    async with client_factory() as client:
        assert (await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t"))["Id"] == "seven"  # exact beats placeholder
        assert (await client.get_one(CLIENT_OP, path_params={"clientId": 8}, tool="t"))["Id"] == "any"
        assert (await client.get_page("GET /v1/tickets", query={"Query": "x"}, tool="t")).items == [{"Id": "q"}]
        assert (await client.get_page("GET /v1/tickets", tool="t")).items == []
        assert (await client.get_one("GET /v1/tickets/{ticketId}", path_params={"ticketId": uid(4)}, tool="t"))["Id"] == "t"
    assert len(mock_gorelo.calls("GET", "/v1/clients/{clientId}")) == 2


async def test_mock_gorelo_unmatched_requests_fail_loudly_and_are_kept(client_factory, mock_gorelo):
    async with client_factory() as client:
        with pytest.raises(UnexpectedRequest, match="no route for GET /v1/clients/7"):
            await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
    assert len(mock_gorelo.unmatched) == 1
    mock_gorelo.reset()  # the fixture teardown would otherwise fail the test


async def test_mock_gorelo_in_order_runs_out_loudly(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/7", in_order(envelope({"Id": 1})))
    async with client_factory() as client:
        await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")
        with pytest.raises(UnexpectedRequest, match="more requests than"):
            await client.get_one(CLIENT_OP, path_params={"clientId": 7}, tool="t")


async def test_mock_gorelo_status_comes_from_the_envelope_unless_overridden(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/1", error_envelope(409, [("070901", "blocked")]))
    mock_gorelo.on("GET", "/v1/clients/2", error_envelope(409, [("070901", "blocked")]), status=200)
    async with client_factory() as client:
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 1}, tool="t")
        assert (info.value.kind, info.value.status) == ("http", 409)
        with pytest.raises(GoreloAPIError) as info:
            await client.get_one(CLIENT_OP, path_params={"clientId": 2}, tool="t")
        assert (info.value.kind, info.value.status) == ("envelope", 200)


def test_envelope_builders():
    assert envelope([1])["DataContext"] is None and envelope([1])["Notifications"] == []
    paged = paged_envelope([1, 2], next_cursor="c1")
    assert paged["DataContext"]["Pagination"] == {
        "NextCursor": "c1", "PreviousCursor": None, "HasMore": True, "HasPrevious": False, "TotalCount": 2,
    }
    assert pagination(None, 3)["HasMore"] is False and pagination("x", 3, has_more=False)["HasMore"] is False
    failure = error_envelope(400, [("070101", "m", "P"), notification("070201", "n")], trace_id="t")
    assert failure["IsSuccess"] is False and failure["Data"] is None and failure["DataContext"] == {"TraceId": "t"}
    assert failure["Notifications"][0] == {"Code": "070101", "Message": "m", "PropertyName": "P", "ActionHint": None, "DocUrl": None}
    assert error_envelope(500, trace_id=None)["DataContext"] is None


# --------------------------------------------------------------------------
# The offline guard of the suite
# --------------------------------------------------------------------------


def test_the_suite_blocks_real_network_connections():
    import socket

    from conftest import NetworkBlocked

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(NetworkBlocked):
            sock.connect(("127.0.0.1", 9))
        with pytest.raises(NetworkBlocked):
            sock.connect_ex(("127.0.0.1", 9))
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("api.usw.gorelo.io", 443)
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("api.usw.gorelo.io", 443), timeout=1)


async def test_a_client_without_a_mock_transport_cannot_reach_gorelo(spec_index):
    from conftest import NetworkBlocked

    async with GoreloClient(TEST_API_KEY, spec=spec_index) as client:
        # exactly the suite's guard: not a GoreloAPIError, which would mean the client swallowed it
        with pytest.raises(NetworkBlocked):
            await client.get_one(CLIENT_OP, path_params={"clientId": 1}, tool="t")


# --------------------------------------------------------------------------
# Every real operation can be sent: right method, right path, ids filled in
# --------------------------------------------------------------------------


def _all_callable_ops():
    from spec import load_spec_index

    return sorted(key for key in load_spec_index().ops if key not in FORBIDDEN_OPS)


@pytest.mark.parametrize("op_key", _all_callable_ops())
async def test_every_non_forbidden_operation_is_sent_to_its_own_path(client_factory, mock_gorelo, spec_index, op_key):
    op = spec_index.op(op_key)
    path_params = path_params_for(op)  # a valid id for each placeholder, by the type the spec gives it
    expected_path = op.path
    for name, value in path_params.items():
        expected_path = expected_path.replace("{" + name + "}", str(value))
    if op.is_binary:
        response = pdf_response()
    elif op.paged:
        response = paged_envelope([])
    else:
        response = envelope({"Id": 1})
    mock_gorelo.on(op.method, expected_path, response)
    async with client_factory() as client:
        if op.is_multipart:
            await client.post_multipart(op_key, path_params=path_params, files={"file": ("a.txt", b"x")}, form={}, tool="t")
        elif op.is_binary:
            await client.get_binary(op_key, path_params=path_params, max_bytes=100, tool="t")
        elif op.paged:
            await client.get_page(op_key, path_params=path_params, tool="t")
        elif op.method == "GET":
            await client.get_one(op_key, path_params=path_params, tool="t")
        else:
            body = {} if op.body is not None else None
            await client.request(op_key, path_params=path_params, json_body=body, tool="t")
    request = mock_gorelo.last
    assert (request.method, request.path) == (op.method, expected_path)
    assert request.headers["x-api-key"] == TEST_API_KEY
    if op.body is not None and not op.is_multipart:
        assert request.json == {}
    assert len(mock_gorelo.requests) == 1
