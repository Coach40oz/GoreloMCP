"""tools/alerts.py: post_alert and list_alerts."""

import inspect
import json
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
    in_order,
    list_tools,
    make_ctx,
    pagination,
    paged_envelope,
    uid,
)
from fastmcp.exceptions import ToolError

import tools.alerts as alerts_module
from gorelo_client import FORBIDDEN_OPS
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio

ALERTS = "/v1/alerts"
PARAMS = ["name", "client_id", "resource", "severity", "description"]
REQUIRED = ["name", "client_id", "resource", "severity"]
GIVEN = {"name": "Disk usage above 90%", "client_id": 9101, "resource": "SRV-DB-01", "severity": 2}


@pytest.fixture
def server(server_factory):
    return server_factory(toolsets={"core"})


def trace(text):
    return f"{text} [trace {TEST_TRACE_ID}]"


def flat(text):
    return " ".join(text.split())


def assert_alert_advice(text):
    """Since list_alerts exists: the alert may already be posted, so do not post it again before list_alerts has
    been looked at, and look the right way (newest first, NO client filter: an external alert carries no client id)."""
    assert "The alert may already be posted." in text
    assert (
        "Before posting it again, check list_alerts (newest first, created_since a few minutes before this call, "
        "no client_ids filter: external alerts carry no client id) and ask the user first"
    ) in text
    # the old advice said no read could find an alert; that is no longer true and must not come back
    assert "cannot list" not in text and "check the Gorelo app" not in text
    # the generic advice names no read, so it must not be given instead of this one
    assert "Verify with a read" not in text and "verify it with a read" not in text
    assert "retrying is safe" not in text and "may or may not have been applied" not in text


# --------------------------------------------------------------------------
# Declaration
# --------------------------------------------------------------------------


def test_post_alert_is_declared_as_a_core_write_tool_over_post_alerts(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "post_alert")
    assert (spec.toolset, spec.kind, spec.ops) == ("core", "write", ["POST /v1/alerts"])
    assert spec.destructive_hint is False
    assert "POST /v1/alerts" in spec_index.ops and not set(spec.ops) & FORBIDDEN_OPS


def test_every_field_map_path_is_a_body_field_of_the_op(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "post_alert")
    op = spec_index.op("POST /v1/alerts")
    assert spec.field_map == {
        "name": "Name", "client_id": "ClientId", "resource": "Resource", "severity": "Severity", "description": "Description",
    }
    assert set(spec.field_map.values()) == set(op.body["fields"])
    assert set(op.body["required"]) <= set(spec.field_map.values())


async def test_the_parameters_are_the_documented_ones_with_the_four_required(server):
    tool = next(t for t in await list_tools(server) if t.name == "post_alert")
    assert list(tool.inputSchema["properties"]) == PARAMS
    assert tool.inputSchema["required"] == REQUIRED
    assert all(p.get("description") for p in tool.inputSchema["properties"].values())
    assert (tool.annotations.readOnlyHint, tool.annotations.destructiveHint) == (False, False)


async def test_the_severity_schema_text_gives_the_published_names_of_the_levels(server):
    tool = next(t for t in await list_tools(server) if t.name == "post_alert")
    assert tool.inputSchema["properties"]["severity"]["description"] == "1 Critical, 2 Error, 3 Warning, 4 Information."
    assert tool.inputSchema["properties"]["severity"]["type"] == "integer"
    assert "publishes no names" not in tool.inputSchema["properties"]["severity"]["description"]


def test_the_severity_names_are_the_ones_of_the_published_alert_severity_enum(spec_index):
    """AlertSeverity (contract e15cb5a18ec2): 1 Critical, 2 Error, 3 Warning, 4 Information. The index keeps only the
    numbers, so the names are the tool's own table; the numbers must stay exactly the enum's."""
    op = spec_index.op("POST /v1/alerts")
    assert op.body["fields"]["Severity"]["ref"] == "AlertSeverity"
    assert spec_index.schema("AlertSeverity")["enum"] == [1, 2, 3, 4] == list(alerts_module.SEVERITIES)
    assert alerts_module.SEVERITY_NAMES == {1: "Critical", 2: "Error", 3: "Warning", 4: "Information"}
    assert "AlertLevel" not in spec_index.schemas  # the unnamed enum it replaced


def test_the_docstring_and_the_module_text_use_the_published_severity_names():
    doc = flat(alerts_module.post_alert.__doc__)
    assert "Severity: 1 Critical, 2 Error, 3 Warning, 4 Information" in doc
    assert "publishes no names" not in doc
    module = flat(alerts_module.__doc__)
    assert "AlertSeverity" in module and "1 Critical, 2 Error, 3 Warning, 4 Information" in module


async def test_the_client_id_description_names_the_tool_that_resolves_it(server):
    tool = next(t for t in await list_tools(server) if t.name == "post_alert")
    assert "list_clients" in tool.inputSchema["properties"]["client_id"]["description"]


def test_the_docstring_states_the_side_effects_and_what_to_do_when_a_call_fails():
    doc = flat(alerts_module.post_alert.__doc__)
    assert "Side effects:" in doc
    assert "may open a ticket or notify technicians" in doc
    assert "tenant's alert rules" in doc
    assert "posting twice may notify twice" in doc
    assert "cannot be deleted through the API" in doc and "cannot be listed" not in doc
    assert "do not post it again until list_alerts" in doc and "ask the user first" in doc
    assert "(newest first, no client_ids filter: external alerts carry no client id)" in doc
    assert 'returns {"ok": true}' in doc


# --------------------------------------------------------------------------
# The request and the result
# --------------------------------------------------------------------------


async def test_post_alert_posts_the_three_required_fields_and_the_severity_and_returns_ok_true(server, mock_gorelo):
    mock_gorelo.on("POST", ALERTS, envelope(True))
    result = await call_tool(server, "post_alert", GIVEN)
    assert result == {"ok": True}
    assert len(mock_gorelo.requests) == 1
    request = mock_gorelo.last
    assert (request.method, request.path, request.query) == ("POST", ALERTS, {})
    assert request.json == {"Name": "Disk usage above 90%", "ClientId": 9101, "Resource": "SRV-DB-01", "Severity": 2}


async def test_post_alert_uses_the_path_without_a_trailing_slash(server, mock_gorelo):
    # the legacy tool posted to /alerts/ (the July path); the spec path is /v1/alerts
    mock_gorelo.on("POST", ALERTS, envelope(True))
    await call_tool(server, "post_alert", GIVEN)
    assert mock_gorelo.last.raw_path == "/v1/alerts"


async def test_post_alert_sends_the_description_when_given_and_only_then(server, mock_gorelo):
    mock_gorelo.on("POST", ALERTS, envelope(True))
    await call_tool(server, "post_alert", {**GIVEN, "description": "C: is at 93 percent"})
    assert mock_gorelo.last.json == {
        "Name": "Disk usage above 90%", "ClientId": 9101, "Resource": "SRV-DB-01", "Severity": 2,
        "Description": "C: is at 93 percent",
    }
    await call_tool(server, "post_alert", GIVEN)
    assert "Description" not in mock_gorelo.last.json


@pytest.mark.parametrize("severity", [1, 2, 3, 4])
async def test_each_severity_from_1_to_4_is_sent_as_a_number(server, mock_gorelo, severity):
    mock_gorelo.on("POST", ALERTS, envelope(True))
    await call_tool(server, "post_alert", {**GIVEN, "severity": severity})
    assert mock_gorelo.last.json["Severity"] == severity
    assert isinstance(mock_gorelo.last.json["Severity"], int) and not isinstance(mock_gorelo.last.json["Severity"], bool)


async def test_the_result_is_an_object_never_a_bare_boolean(server, mock_gorelo):
    mock_gorelo.on("POST", ALERTS, envelope(True))
    result = await call_tool(server, "post_alert", GIVEN)
    assert isinstance(result, dict) and result["ok"] is True


@pytest.mark.parametrize(
    "data, found",
    [
        (False, "a boolean"),
        (None, "null"),
        ({}, "an empty object"),
        ({"Id": 1}, "an object"),
        ("true", "a string"),
        (1, "a number"),
        ([], "an empty list"),
    ],
)
async def test_data_other_than_true_is_an_error_never_a_success_and_never_ok_false(server, mock_gorelo, data, found):
    # Data false used to come back as {"ok": false, "warning": ...}; a write whose answer is not the expected one raises
    mock_gorelo.on("POST", ALERTS, envelope(data))
    text = await call_tool_error(server, "post_alert", GIVEN)
    assert text.startswith(f"Gorelo did not confirm post_alert (Gorelo answered success but its Data was {found}, not true).")
    assert_alert_advice(text)
    assert len(mock_gorelo.requests) == 1  # never repeated by the tool


# --------------------------------------------------------------------------
# Local validation (nothing is sent)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("severity", [0, 5, -1, 99])
async def test_a_severity_outside_1_to_4_is_a_local_error_that_gives_the_published_names(server, mock_gorelo, severity):
    text = await call_tool_error(server, "post_alert", {**GIVEN, "severity": severity})
    assert text == (
        "severity: must be a whole number from 1 to 4 (1 Critical, 2 Error, 3 Warning, 4 Information), "
        f"got {severity!r}"
    )
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"name": ""}, "name: must not be empty or whitespace only"),
        ({"name": "   "}, "name: must not be empty or whitespace only"),
        ({"resource": ""}, "resource: must not be empty or whitespace only"),
        ({"resource": " "}, "resource: must not be empty or whitespace only"),
        ({"description": ""}, "description: must not be empty or whitespace only"),
        ({"client_id": 0}, "client_id: expected a positive whole number"),
        ({"client_id": -9101}, "client_id: expected a positive whole number"),
    ],
)
async def test_post_alert_local_errors_name_the_parameter_and_send_nothing(server, mock_gorelo, arguments, fragment):
    assert fragment in await call_tool_error(server, "post_alert", {**GIVEN, **arguments})
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("missing", REQUIRED)
async def test_each_of_the_four_required_parameters_is_required(server, mock_gorelo, missing):
    arguments = {k: v for k, v in GIVEN.items() if k != missing}
    assert missing in await call_tool_error(server, "post_alert", arguments)
    assert mock_gorelo.requests == []


async def test_resource_is_required_as_in_the_spec_unlike_the_legacy_tool(server, mock_gorelo):
    arguments = {k: v for k, v in GIVEN.items() if k != "resource"}
    assert "resource" in await call_tool_error(server, "post_alert", arguments)
    assert mock_gorelo.requests == []


async def test_a_non_numeric_severity_is_refused_before_any_http_call(server, mock_gorelo):
    assert "severity" in await call_tool_error(server, "post_alert", {**GIVEN, "severity": "high"})
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("severity", [True, False, "2", 2.0, 2.5, [2], None])
async def test_severity_is_strict_so_true_or_the_text_2_never_becomes_a_level(server, mock_gorelo, severity):
    # lax pydantic turns JSON true into 1 and "2" into 2; an alert cannot be taken back, so neither is accepted
    text = await call_tool_error(server, "post_alert", {**GIVEN, "severity": severity})
    assert "severity" in text and "valid integer" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("client_id", [True, "9101", 9101.0, None, [9101]])
async def test_client_id_is_a_strict_id_so_true_or_the_text_9101_is_refused(server, mock_gorelo, client_id):
    text = await call_tool_error(server, "post_alert", {**GIVEN, "client_id": client_id})
    assert "client_id" in text and "valid integer" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("client_id", [2**63, 10**30])
async def test_a_client_id_above_the_largest_gorelo_id_is_refused_locally(server, mock_gorelo, client_id):
    text = await call_tool_error(server, "post_alert", {**GIVEN, "client_id": client_id})
    assert text.startswith("client_id: expected a positive whole number such as 123, got a number above ")
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# What Gorelo can answer
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "property_name, param",
    [
        ("Name", "name"),
        ("ClientId", "client_id"),
        ("Resource", "resource"),
        ("Severity", "severity"),
        ("Description", "description"),
    ],
)
async def test_post_alert_maps_a_gorelo_error_to_the_snake_case_parameter(server, mock_gorelo, property_name, param):
    mock_gorelo.on("POST", ALERTS, error_envelope(400, [("070101", "Value is not valid.", property_name)]))
    text = await call_tool_error(server, "post_alert", GIVEN)
    assert text == trace(f"Gorelo rejected post_alert (HTTP 400, code 070101): {param}: Value is not valid.")


async def test_post_alert_shows_every_notification(server, mock_gorelo):
    notes = [("070101", "Name is too long.", "Name"), ("070101", "Client is inactive.", "ClientId")]
    mock_gorelo.on("POST", ALERTS, error_envelope(400, notes))
    text = await call_tool_error(server, "post_alert", GIVEN)
    assert "name: Name is too long.; client_id: Client is inactive." in text


async def test_post_alert_names_a_missing_scope(server, mock_gorelo):
    mock_gorelo.on("POST", ALERTS, error_envelope(403, [("080203", "API key does not have 'Alerts' scope")]))
    text = await call_tool_error(server, "post_alert", GIVEN)
    assert "the API key does not have the 'Alerts' scope" in text


async def test_post_alert_reports_a_body_gorelo_could_not_read_without_inventing_a_parameter(server, mock_gorelo):
    mock_gorelo.on("POST", ALERTS, error_envelope(400, [("070201", "Invalid or malformed request body.")]))
    text = await call_tool_error(server, "post_alert", GIVEN)
    assert text == trace("Gorelo rejected post_alert (HTTP 400, code 070201): Invalid or malformed request body.")


async def test_post_alert_that_times_out_says_the_alert_may_be_posted_and_is_never_retried(server, mock_gorelo):
    mock_gorelo.on("POST", ALERTS, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "post_alert", GIVEN)
    assert text.startswith("Gorelo did not confirm post_alert (the request timed out). ")
    assert_alert_advice(text)
    assert "[trace" not in text  # a timeout has no answer, so no trace id
    assert len(mock_gorelo.requests) == 1


async def test_post_alert_after_a_connection_failure_says_the_alert_may_be_posted(server, mock_gorelo):
    mock_gorelo.on("POST", ALERTS, httpx.ConnectError("refused"))
    text = await call_tool_error(server, "post_alert", GIVEN)
    assert text.startswith("Gorelo did not confirm post_alert (the connection failed). ")
    assert_alert_advice(text)
    assert len(mock_gorelo.requests) == 1


async def test_post_alert_after_a_5xx_envelope_gives_the_gorelo_message_the_alert_advice_and_the_trace_id(server, mock_gorelo):
    mock_gorelo.on("POST", ALERTS, error_envelope(500, [("070500", "Boom.")]))
    text = await call_tool_error(server, "post_alert", GIVEN)
    assert text.startswith("Gorelo did not confirm post_alert (Gorelo answered HTTP 500: Boom.). ")
    assert_alert_advice(text)
    assert text.endswith(f" [trace {TEST_TRACE_ID}]")
    assert len(mock_gorelo.requests) == 1


async def test_post_alert_after_a_5xx_gateway_page_says_the_alert_may_be_posted(server, mock_gorelo):
    mock_gorelo.on("POST", ALERTS, httpx.Response(500, text="boom", headers={"content-type": "text/plain"}))
    text = await call_tool_error(server, "post_alert", GIVEN)
    assert text.startswith("Gorelo did not confirm post_alert (Gorelo answered HTTP 500). ")
    assert_alert_advice(text)
    assert "[trace" not in text
    assert len(mock_gorelo.requests) == 1


async def test_post_alert_with_a_200_that_is_not_an_envelope_says_the_alert_may_be_posted(server, mock_gorelo):
    mock_gorelo.on("POST", ALERTS, httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"}))
    text = await call_tool_error(server, "post_alert", GIVEN)
    assert text.startswith("Gorelo did not confirm post_alert (its answer could not be used). ")
    assert_alert_advice(text)


async def test_a_refusal_with_a_4xx_is_not_an_unconfirmed_write_and_keeps_the_normal_text(server, mock_gorelo):
    # nothing was applied, so the "may already be posted" advice would be wrong
    mock_gorelo.on("POST", ALERTS, error_envelope(400, [("070101", "Client is inactive.", "ClientId")]))
    text = await call_tool_error(server, "post_alert", GIVEN)
    assert text == trace("Gorelo rejected post_alert (HTTP 400, code 070101): client_id: Client is inactive.")
    assert "may already be posted" not in text


async def test_a_rate_limited_post_says_it_was_not_processed_and_gives_no_alert_advice(server, mock_gorelo):
    mock_gorelo.on(
        "POST", ALERTS, httpx.Response(429, json={"error": "rate_limited", "retry_after": "0s"}), headers={"Retry-After": "0"}
    )
    text = await call_tool_error(server, "post_alert", GIVEN)
    assert "rate limiting requests (HTTP 429) for post_alert" in text and "may already be posted" not in text


async def test_post_alert_declares_no_read_so_it_makes_no_other_request(server, mock_gorelo):
    mock_gorelo.on("POST", ALERTS, envelope(True))
    await call_tool(server, "post_alert", {**GIVEN, "description": "d"})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("POST", ALERTS)]


# --------------------------------------------------------------------------
# list_alerts (GET /v1/alerts, new in contract e15cb5a18ec2)
# --------------------------------------------------------------------------

LIST_PARAMS = [
    "status", "type_ids", "client_ids", "device_ids", "created_since", "created_before", "sort_order", "page_size", "cursor",
]
LATEST_SPEC = Path(__file__).resolve().parent.parent / "backups" / "swagger-latest.json"
TYPE_HELP = (
    "Uptime=1, API=2, Script=3, External=4, Slide=5, Huntress=6, Error Event Log=7, Disk Usage=8, Connectivity=9, "
    "Process=10, Service=11, Antivirus=12, Redfish=13, CPU=14, Memory=15, Ping=16, Windows Updates=17, "
    "Warranty Expiration=18, Domain Expiration=19, Contract Expiration=20"
)


def alert_row(n=1, **extra):
    """One AlertListItemModel as Gorelo returns it (PascalCase; Type, Severity and Status are {Id, Name} codes)."""
    row = {
        "Id": uid(n),
        "Title": "Disk usage above 90%",
        "Type": {"Id": 4, "Name": "External"},
        "Severity": {"Id": 2, "Name": "Error"},
        "Status": {"Id": 1, "Name": "New"},
        "Message": "",
        "ClientId": None,  # an external alert carries no client id
        "DeviceId": None,
        "CheckId": None,
        "UptimeCheckId": None,
        "TicketId": None,
        "ContractId": None,
        "DomainId": None,
        "DismissedOn": None,
        "DismissedBy": None,
        "CreatedOn": "2026-10-02T17:00:00Z",
        "UpdatedOn": None,
    }
    row.update(extra)
    return row


@pytest.fixture
def full_server(server_factory):
    """Every toolset on, delete tools off: list_alerts must not depend on either."""
    return server_factory()


async def call_list_directly(client_factory, **kwargs):
    """Call the decorated function itself (no pydantic in front of it): for the checks the schema normally pre-empts."""
    async with client_factory() as client:
        return await alerts_module.list_alerts(make_ctx(client), **kwargs)


def test_list_alerts_is_declared_as_a_core_read_tool_over_get_alerts(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "list_alerts")
    assert (spec.toolset, spec.kind, spec.ops, spec.destructive_hint) == ("core", "read", ["GET /v1/alerts"], False)
    op = spec_index.op("GET /v1/alerts")
    assert op.paged and "GET /v1/alerts" in spec_index.ops and not set(spec.ops) & FORBIDDEN_OPS


def test_the_module_declares_exactly_the_two_alert_tools_and_names_both_ops():
    names = {s.name: (s.kind, s.ops) for s in REGISTRY.specs if s.fn.__module__ == alerts_module.__name__}
    assert names == {
        "post_alert": ("write", ["POST /v1/alerts"]),
        "list_alerts": ("read", ["GET /v1/alerts"]),
    }
    doc = alerts_module.__doc__
    assert "POST /v1/alerts" in doc and "GET /v1/alerts" in doc


def test_every_query_name_of_the_op_is_a_filter_or_a_paging_name_of_the_tool(spec_index):
    spec = next(s for s in REGISTRY.specs if s.name == "list_alerts")
    op = spec_index.op("GET /v1/alerts")
    assert set(spec.field_map.values()) == set(op.query_params)  # nothing the spec offers is left out, nothing invented
    assert spec.field_map["page_size"] == "PageSize" and spec.field_map["cursor"] == "Cursor"
    assert set(alerts_module.FILTER_FIELDS) == set(LIST_PARAMS) - {"page_size", "cursor"}


def test_the_fixture_is_shaped_like_the_spec(spec_index):
    fields = set(spec_index.schema("AlertListItemModel")["fields"])
    assert set(alert_row()) == fields
    code = set(spec_index.schema("CodeModel")["fields"])
    assert set(alert_row()["Type"]) == set(alert_row()["Severity"]) == set(alert_row()["Status"]) == code


async def test_the_parameters_are_the_documented_ones_none_is_required_and_every_one_is_described(full_server):
    tool = next(t for t in await list_tools(full_server) if t.name == "list_alerts")
    assert list(tool.inputSchema["properties"]) == LIST_PARAMS
    assert not tool.inputSchema.get("required")
    assert all(p.get("description") for p in tool.inputSchema["properties"].values())
    assert (tool.annotations.readOnlyHint, tool.annotations.destructiveHint, tool.annotations.idempotentHint) == (True, False, True)
    properties = tool.inputSchema["properties"]
    assert properties["sort_order"]["default"] == "desc" and properties["sort_order"]["enum"] == ["desc", "asc"]
    assert properties["page_size"]["default"] == 50
    assert properties["status"]["items"]["enum"] == ["new", "ignored", "ticketed"]
    assert properties["client_ids"]["type"] == properties["type_ids"]["type"] == "array"


async def test_the_parameter_texts_carry_the_id_sources_and_what_was_seen_of_the_client_filter(full_server):
    tool = next(t for t in await list_tools(full_server) if t.name == "list_alerts")
    text = {name: " ".join(p["description"].split()) for name, p in tool.inputSchema["properties"].items()}
    assert "list_clients" in text["client_ids"]
    # the model must be told the two facts it needs. Uptime and External alerts have no ClientId (the spec's
    # AlertListItemModel says so), so a client filter may leave them out: a model that filters by client and finds no
    # External alert must know to look without the filter. And live 2026-10-04: a Script alert whose ClientId is null
    # matched ClientIds through its device's client. "may", not "never": nothing was observed of Uptime and External alerts
    assert text["client_ids"] == (
        "From list_clients. Uptime and External alerts have no ClientId, so this filter may leave them out; "
        "a Script alert without one matched through its device."
    )
    assert "Uptime and External alerts have no ClientId" in text["client_ids"] and "may leave them out" in text["client_ids"]
    assert "a Script alert without one matched through its device" in text["client_ids"]
    assert len(text["client_ids"]) <= 160  # the concision limit of a parameter text (153 characters today)
    # ... and it does not repeat what the spec says and production contradicted ("never matches"): see
    # test_the_type_and_status_tables_are_the_ones_in_the_spec_text for the spec text, kept there as a fact about the spec
    for old in ("never match", "Uptime, External and Script alerts"):
        assert old not in text["client_ids"]
    assert "never" not in text["client_ids"].lower() and "always" not in text["client_ids"].lower()  # a hedge, no promise
    # the old text said "Not verified for Uptime and External alerts" and never told the model that they have no ClientId
    assert "Not verified" not in text["client_ids"] and "can still match through its device's client" not in text["client_ids"]
    assert "list_agents" in text["device_ids"]
    assert "At or after" in text["created_since"] and "Strictly before" in text["created_before"]
    assert "UTC offset" in text["created_since"] and "UTC offset" in text["created_before"]
    assert "named in the tool text" in text["type_ids"]
    assert text["cursor"] == "next_cursor from the previous call; same filters and sort; repeat until has_more is false."
    assert text["sort_order"] == "desc: newest first. asc: oldest first."


def test_the_docstring_gives_the_type_names_and_what_the_model_must_know():
    doc = " ".join(inspect.getdoc(alerts_module.list_alerts).split())
    assert TYPE_HELP in doc and alerts_module.TYPE_HELP == TYPE_HELP
    assert "(7-17 are device checks; 0 matches a type with no name)" in doc
    assert "including those sent with post_alert" in doc
    assert "Alerts cannot be deleted." in doc
    assert "DismissedOn and DismissedBy are set only while an alert is dismissed" in doc
    assert "newest first unless sort_order is asc" in doc
    assert "never match" not in doc  # the spec says so of the client filter; production does not (see the client_ids text)
    assert 40 < len(inspect.getdoc(alerts_module.list_alerts)) <= 700


def test_the_module_text_gives_the_spec_claim_as_the_specs_own_and_the_live_observation_as_what_was_seen():
    module = flat(alerts_module.__doc__)
    assert (
        "The spec says an alert type that carries no client id (Uptime, External and Script alerts) never matches the "
        "ClientIds filter. Production disagrees (live, 2026-10-04): a Script alert whose ClientId was null matched "
        "through its device's client. Nothing was observed for Uptime or External alerts"
    ) in module
    assert "the filter never matches an alert whose type carries no client id" not in module  # the old, unqualified claim
    assert "a check for an alert that post_alert raised must not filter by client" in module
    # the parameter text gives the model both facts (the hedge for Uptime and External, the Script alert seen live)
    assert (
        "The client_ids text of list_alerts gives the model both facts: Uptime and External alerts have no ClientId, so "
        "the filter may leave them out (a hedge, not a promise: nothing was observed), and a Script alert without one "
        "matched through its device"
    ) in module
    assert "promises nothing either way" not in module  # the old sentence, from before the text told the model the facts
    not_confirmed = flat(alerts_module._not_confirmed.__doc__)
    assert "the spec says the client_ids filter never matches such an alert" in not_confirmed
    assert "the client_ids filter never matches an alert whose type carries no client id, and an external alert" not in not_confirmed


@pytest.mark.skipif(not LATEST_SPEC.exists(), reason="backups/swagger-latest.json is missing")
def test_the_type_and_status_tables_are_the_ones_in_the_spec_text():
    """spec/spec_index.json keeps no description text, so the scales the tool offers are pinned to the parameter text
    of the full OpenAPI snapshot of contract e15cb5a18ec2."""
    parameters = json.loads(LATEST_SPEC.read_text(encoding="utf-8"))["paths"]["/v1/alerts"]["get"]["parameters"]

    def text(name):
        return " ".join(next(p for p in parameters if p["name"] == name)["description"].split())

    types = {int(number): name.strip() for name, number in re.findall(r"([A-Za-z][A-Za-z ]*?)=(\d+)", text("TypeIds"))}
    assert types == {key: name for key, name in alerts_module.ALERT_TYPES.items() if key}
    assert len(types) == 20
    assert "0 (Unknown) matches an alert this scale cannot name" in text("TypeIds") and alerts_module.ALERT_TYPES[0] == "Unknown"
    statuses = {name.lower(): int(number) for name, number in re.findall(r"([A-Za-z]+)=(\d+)", text("StatusIds"))}
    assert statuses == alerts_module.ALERT_STATUS_IDS == {"new": 1, "ignored": 2, "ticketed": 3}
    # A fact about the SPEC, kept as the spec words it. Production contradicts it (2026-10-04: a Script alert whose
    # ClientId is null was returned for ClientIds=9102 through its device's client), so the text of the tool follows
    # the live behavior and does not repeat this sentence (see the client_ids text test above).
    assert "An alert type that carries no client id never matches this filter." in text("ClientIds")
    assert "`desc` (default, newest first) or `asc`" in text("SortOrder")
    assert "strictly before" in text("CreatedBefore") and "at or after" in text("CreatedSince")


# -- the request and the result ---------------------------------------------------------------------------------------


async def test_list_alerts_without_filters_sends_the_page_size_and_the_default_sort_and_nothing_else(full_server, mock_gorelo):
    rows = [alert_row(2), alert_row(1)]
    mock_gorelo.on("GET", ALERTS, paged_envelope(rows, total_count=2))
    result = await call_tool(full_server, "list_alerts")
    request = mock_gorelo.last
    assert (request.method, request.path, request.raw_path) == ("GET", ALERTS, "/v1/alerts")
    assert request.query == {"PageSize": "50", "SortOrder": "desc"} and request.content == b""
    assert result == {
        "items": rows,
        "count": 2,
        "total_count": 2,
        "has_more": False,
        "next_cursor": None,
        "page_size": 50,
        "filters": {"sort_order": "desc"},
    }
    assert len(mock_gorelo.requests) == 1


async def test_every_filter_goes_out_under_its_spec_name(full_server, mock_gorelo):
    mock_gorelo.on("GET", ALERTS, paged_envelope([alert_row()]))
    args = {
        "status": ["new", "ticketed"],
        "type_ids": [1, 4, 0],
        "client_ids": [9102, 9101],
        "device_ids": [uid(7).upper(), uid(8).replace("-", "")],  # canonical lowercase on the wire
        "created_since": "2026-10-01T08:00:00Z",
        "created_before": "2026-10-02T08:00:00+02:00",  # converted to UTC
        "sort_order": "asc",
        "page_size": 25,
        "cursor": "opaque-cursor-1",
    }
    result = await call_tool(full_server, "list_alerts", args)
    assert mock_gorelo.last.query == {
        "StatusIds": "1,3",
        "TypeIds": "1,4,0",
        "ClientIds": "9102,9101",
        "DeviceIds": f"{uid(7)},{uid(8)}",
        "CreatedSince": "2026-10-01T08:00:00Z",
        "CreatedBefore": "2026-10-02T06:00:00Z",
        "SortOrder": "asc",
        "PageSize": "25",
        "Cursor": "opaque-cursor-1",
    }
    # the echo is in the tool's own terms (status as words), so the next call can repeat it unchanged
    assert result["filters"] == {
        "status": ["new", "ticketed"],
        "type_ids": [1, 4, 0],
        "client_ids": [9102, 9101],
        "device_ids": [uid(7), uid(8)],
        "created_since": "2026-10-01T08:00:00Z",
        "created_before": "2026-10-02T06:00:00Z",
        "sort_order": "asc",
    }
    assert result["page_size"] == 25


@pytest.mark.parametrize(
    "status, wire, echoed",
    [
        (["new"], "1", ["new"]),
        (["ignored"], "2", ["ignored"]),
        (["ticketed"], "3", ["ticketed"]),
        (["ignored", "ticketed"], "2,3", ["ignored", "ticketed"]),
        (["ticketed", "new", "ignored"], "3,1,2", ["ticketed", "new", "ignored"]),
        (["new", "new", "ticketed", "new"], "1,3", ["new", "ticketed"]),  # a repeat is dropped, the order kept
    ],
)
async def test_status_words_become_status_ids(full_server, mock_gorelo, status, wire, echoed):
    mock_gorelo.on("GET", ALERTS, paged_envelope([]))
    result = await call_tool(full_server, "list_alerts", {"status": status})
    assert mock_gorelo.last.query["StatusIds"] == wire and result["filters"]["status"] == echoed


async def test_status_zero_is_not_offered_as_a_word(full_server, mock_gorelo):
    for bad in (["unknown"], ["New"], ["1"], [1], [None], ["new", "gone"]):
        text = await call_tool_error(full_server, "list_alerts", {"status": bad})
        assert "status" in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "type_ids, wire, echoed",
    [
        ([1], "1", [1]),
        ([4], "4", [4]),
        ([0], "0", [0]),  # 0 (Unknown) is a documented value of the filter
        ([20], "20", [20]),
        ([1, 4, 20, 0], "1,4,20,0", [1, 4, 20, 0]),
        ([4, 4, 1, 4], "4,1", [4, 1]),
    ],
)
async def test_type_ids_on_the_published_scale_are_sent_as_given(full_server, mock_gorelo, type_ids, wire, echoed):
    mock_gorelo.on("GET", ALERTS, paged_envelope([]))
    result = await call_tool(full_server, "list_alerts", {"type_ids": type_ids})
    assert mock_gorelo.last.query["TypeIds"] == wire and result["filters"]["type_ids"] == echoed


async def test_the_type_table_covers_the_documented_scale_and_nothing_else():
    assert sorted(alerts_module.ALERT_TYPES) == list(range(0, 21))
    assert alerts_module.ALERT_TYPES[1] == "Uptime" and alerts_module.ALERT_TYPES[4] == "External"
    assert alerts_module.ALERT_TYPES[20] == "Contract Expiration" and alerts_module.ALERT_TYPES[0] == "Unknown"
    assert alerts_module.ALERT_STATUS_IDS == {"new": 1, "ignored": 2, "ticketed": 3}


@pytest.mark.parametrize("given, used", [(0, 1), (-5, 1), (1, 1), (50, 50), (200, 200), (201, 200), (5000, 200)])
async def test_page_size_is_clamped_to_1_200_and_the_size_used_is_reported(full_server, mock_gorelo, given, used):
    mock_gorelo.on("GET", ALERTS, paged_envelope([]))
    result = await call_tool(full_server, "list_alerts", {"page_size": given})
    assert mock_gorelo.last.query["PageSize"] == str(used) and result["page_size"] == used


async def test_paging_follows_next_cursor_with_the_same_filters_and_sort(full_server, mock_gorelo):
    first = [alert_row(3), alert_row(2)]
    second = [alert_row(1)]
    mock_gorelo.on(
        "GET",
        ALERTS,
        in_order(
            envelope(first, pagination("cursor-2", 3)),
            envelope(second, pagination(None, 3, has_more=False)),
        ),
    )
    filters = {"status": ["new"], "type_ids": [4], "sort_order": "asc", "page_size": 2}
    one = await call_tool(full_server, "list_alerts", filters)
    assert (one["has_more"], one["next_cursor"], one["total_count"], one["count"]) == (True, "cursor-2", 3, 2)
    two = await call_tool(full_server, "list_alerts", {**filters, "cursor": one["next_cursor"]})
    assert (two["has_more"], two["next_cursor"], two["count"]) == (False, None, 1)
    first_query, second_query = (r.query for r in mock_gorelo.requests)
    base = {"StatusIds": "1", "TypeIds": "4", "SortOrder": "asc", "PageSize": "2"}
    assert first_query == base and second_query == {**base, "Cursor": "cursor-2"}
    assert one["filters"] == two["filters"]
    assert [item["Id"] for item in one["items"] + two["items"]] == [uid(3), uid(2), uid(1)]


async def test_the_echoed_filters_can_be_sent_back_unchanged_for_the_next_page(full_server, mock_gorelo):
    mock_gorelo.on("GET", ALERTS, paged_envelope([]))
    first = await call_tool(
        full_server, "list_alerts", {"status": ["ignored"], "created_since": "2026-10-01T08:00:00+02:00", "client_ids": [5]}
    )
    again = await call_tool(full_server, "list_alerts", {**first["filters"], "cursor": "c-2"})
    assert again["filters"] == first["filters"]
    assert mock_gorelo.requests[1].query["CreatedSince"] == "2026-10-01T06:00:00Z"


async def test_an_empty_page_is_reported_as_one_with_its_total(full_server, mock_gorelo):
    mock_gorelo.on("GET", ALERTS, envelope([], pagination(None, 0, has_more=False)))
    result = await call_tool(full_server, "list_alerts", {"status": ["ticketed"]})
    assert result["items"] == [] and result["count"] == 0 and result["total_count"] == 0 and result["has_more"] is False


async def test_the_rows_come_back_unchanged_including_a_dismissed_one_and_one_without_a_client(full_server, mock_gorelo):
    dismissed = alert_row(
        2,
        Status={"Id": 2, "Name": "Ignored"},
        DismissedOn="2026-10-02T17:30:00Z",
        DismissedBy=9201,
        UpdatedOn="2026-10-02T17:30:00Z",
    )
    uptime = alert_row(3, Type={"Id": 1, "Name": "Uptime"}, UptimeCheckId=uid(40), Message="")
    process = alert_row(4, Type={"Id": 10, "Name": "Process"}, ClientId=9101, DeviceId=uid(41), CheckId="chk-9", Message="Safari not found")
    rows = [alert_row(1), dismissed, uptime, process]
    mock_gorelo.on("GET", ALERTS, paged_envelope(rows))
    result = await call_tool(full_server, "list_alerts")
    assert result["items"] == rows
    assert result["items"][1]["DismissedBy"] == 9201 and result["items"][0]["DismissedOn"] is None
    assert result["items"][0]["ClientId"] is None  # no client id on an external alert, as the spec says


async def test_a_row_without_a_client_id_that_gorelo_matched_through_its_device_is_returned_as_it_is(full_server, mock_gorelo):
    """Live 2026-10-04: list_alerts(client_ids=[9102]) returned a Script alert whose ClientId is null but whose DeviceId
    belongs to that client (the spec says such an alert never matches ClientIds). The filter is Gorelo's: the tool
    sends it as given and keeps every row of the answer, so it never drops a row for its null ClientId and never fills
    the ClientId in."""
    script = alert_row(5, Type={"Id": 3, "Name": "Script"}, ClientId=None, DeviceId=uid(41), Message="Script failed")
    mock_gorelo.on("GET", ALERTS, paged_envelope([script], total_count=1))
    result = await call_tool(full_server, "list_alerts", {"client_ids": [9102]})
    assert mock_gorelo.last.query == {"ClientIds": "9102", "PageSize": "50", "SortOrder": "desc"}
    assert result["items"] == [script] and result["items"][0]["ClientId"] is None and result["items"][0]["DeviceId"] == uid(41)
    assert (result["count"], result["total_count"], result["has_more"]) == (1, 1, False)
    assert result["filters"] == {"client_ids": [9102], "sort_order": "desc"}
    assert len(mock_gorelo.requests) == 1  # one read, no second look for the device's client


async def test_list_alerts_refuses_an_answer_that_is_not_a_list(full_server, mock_gorelo):
    mock_gorelo.on("GET", ALERTS, envelope({"Id": uid(1)}, pagination(None, 1, has_more=False)))
    text = await call_tool_error(full_server, "list_alerts")
    assert text.startswith("Gorelo returned an unexpected response for list_alerts") and "expected Data to be a list" in text


async def test_list_alerts_sends_a_get_and_nothing_else(full_server, mock_gorelo):
    mock_gorelo.on("GET", ALERTS, paged_envelope([alert_row()]))
    await call_tool(full_server, "list_alerts", {"status": ["new"]})
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", ALERTS)]


async def test_list_alerts_is_registered_in_the_core_toolset_only(server_factory):
    core = {t.name for t in await list_tools(server_factory(toolsets={"core"}))}
    assert {"list_alerts", "post_alert"} <= core
    others = {t.name for t in await list_tools(server_factory(toolsets={"tickets", "time", "billing", "uptime", "projects", "forms"}))}
    assert not {"list_alerts", "post_alert"} & others
    off = {t.name for t in await list_tools(server_factory(toolsets={"core"}, destructive=False))}
    assert "list_alerts" in off  # a read tool needs no destructive switch


# -- errors -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "property_name, param",
    [
        ("StatusIds", "status"),
        ("TypeIds", "type_ids"),
        ("ClientIds", "client_ids"),
        ("DeviceIds", "device_ids"),
        ("CreatedSince", "created_since"),
        ("CreatedBefore", "created_before"),
        ("SortOrder", "sort_order"),
        ("PageSize", "page_size"),
        ("Cursor", "cursor"),
    ],
)
async def test_a_gorelo_400_names_the_snake_case_param(full_server, mock_gorelo, property_name, param):
    mock_gorelo.on("GET", ALERTS, error_envelope(400, [("070101", "A value that is not valid.", property_name)]))
    text = await call_tool_error(full_server, "list_alerts")
    assert text == trace(f"Gorelo rejected list_alerts (HTTP 400, code 070101): {param}: A value that is not valid.")


async def test_a_missing_scope_reads_as_a_missing_scope(full_server, mock_gorelo):
    mock_gorelo.on("GET", ALERTS, error_envelope(403, [("080203", "API key does not have 'Alerts' scope")]))
    text = await call_tool_error(full_server, "list_alerts")
    assert "the API key does not have the 'Alerts' scope" in text


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")])
async def test_a_read_that_times_out_or_loses_the_connection_is_safe_to_retry(full_server, mock_gorelo, failure):
    mock_gorelo.on("GET", ALERTS, failure)
    text = await call_tool_error(full_server, "list_alerts")
    assert text.startswith("Gorelo did not answer list_alerts") and "This was a read, so retrying is safe." in text
    assert "write" not in text.lower()


async def test_a_server_error_on_the_read_is_reported_with_its_trace_id(full_server, mock_gorelo):
    mock_gorelo.on("GET", ALERTS, error_envelope(500, [("070500", "Boom.")]))
    text = await call_tool_error(full_server, "list_alerts")
    assert text == trace("Gorelo rejected list_alerts (HTTP 500, code 070500): Boom.")


LOCAL_LIST_ERRORS = [
    pytest.param({"status": []}, "status: must contain at least one of 'new', 'ignored', 'ticketed' (omit it for every status)", id="empty-status"),
    pytest.param({"status": "new"}, "status: expected a list such as ['new', 'ticketed'], got a string", id="status-not-a-list"),
    pytest.param({"status": ["bogus"]}, "status: expected one of 'new', 'ignored', 'ticketed', got a string", id="unknown-status"),
    pytest.param({"status": [1]}, "status: expected one of 'new', 'ignored', 'ticketed', got a number", id="numeric-status"),
    pytest.param({"type_ids": []}, "type_ids: expected at least one id, got an empty list (omit type_ids if you have none to give)", id="empty-type-ids"),
    pytest.param({"type_ids": 4}, "type_ids: expected a list of ids such as [1, 4], got a number", id="type-ids-not-a-list"),
    pytest.param({"type_ids": [1, 21]}, "type_ids[1]: 21 is not an alert type id; valid ids: 0 Unknown, 1 Uptime, 2 API", id="type-21"),
    pytest.param({"type_ids": [-1]}, "type_ids[0]: -1 is not an alert type id", id="negative-type"),
    pytest.param({"type_ids": [True]}, "type_ids[0]: a boolean is not an alert type id", id="bool-type"),
    pytest.param({"type_ids": ["4"]}, "type_ids[0]: a string is not an alert type id", id="text-type"),
    pytest.param({"type_ids": [4.0]}, "type_ids[0]: a number is not an alert type id", id="decimal-type"),
    pytest.param({"client_ids": []}, "client_ids: expected at least one id, got an empty list", id="empty-client-ids"),
    pytest.param({"client_ids": [5, 0]}, "client_ids[1]: expected a positive whole number such as 123, got zero or a negative number", id="client-id-zero"),
    pytest.param({"client_ids": [2**63]}, "client_ids[0]: expected a positive whole number such as 123, got a number above 9223372036854775807", id="client-id-beyond-int64"),
    pytest.param({"device_ids": []}, "device_ids: expected at least one GUID, got an empty list", id="empty-device-ids"),
    pytest.param({"device_ids": ["not-a-guid"]}, "device_ids[0]: expected a GUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b, got text that is not a GUID", id="bad-guid"),
    pytest.param({"device_ids": [uid(1), "12"]}, "device_ids[1]: expected a GUID", id="second-device-bad"),
    pytest.param({"device_ids": [" " + uid(1)]}, "device_ids[0]: expected a GUID", id="guid-with-a-space-is-not-trimmed"),
    pytest.param({"created_since": "yesterday"}, "created_since: 'yesterday' is not an ISO 8601 datetime", id="garbage-created-since"),
    pytest.param({"created_since": "2026-10-01T08:00:00"}, "created_since: '2026-10-01T08:00:00' has no UTC offset", id="naive-created-since"),
    pytest.param({"created_before": "2026-10-01"}, "created_before: '2026-10-01' has no UTC offset", id="date-only-created-before"),
    pytest.param(
        {"created_since": "2026-10-02T00:00:00Z", "created_before": "2026-10-01T00:00:00Z"},
        "created_since: must be earlier than created_before (created_since is inclusive, created_before is exclusive), so no alert could match",
        id="since-after-before",
    ),
    pytest.param(
        {"created_since": "2026-10-01T00:00:00Z", "created_before": "2026-10-01T02:00:00+02:00"},
        "created_since: must be earlier than created_before",
        id="same-instant-in-another-offset",
    ),
    pytest.param({"cursor": ""}, "cursor: must not be empty or whitespace only", id="blank-cursor"),
    pytest.param({"cursor": "   "}, "cursor: must not be empty or whitespace only", id="whitespace-cursor"),
]


@pytest.mark.parametrize("args, fragment", LOCAL_LIST_ERRORS)
async def test_local_validation_errors_name_the_param_and_send_nothing(client_factory, mock_gorelo, args, fragment):
    with pytest.raises(ToolError) as info:
        await call_list_directly(client_factory, **args)
    assert fragment in str(info.value)
    assert mock_gorelo.requests == []


async def test_a_zero_width_window_is_refused_but_one_second_is_not(full_server, mock_gorelo):
    mock_gorelo.on("GET", ALERTS, paged_envelope([]))
    ok = {"created_since": "2026-10-01T00:00:00Z", "created_before": "2026-10-01T00:00:01Z"}
    await call_tool(full_server, "list_alerts", ok)
    assert mock_gorelo.last.query["CreatedSince"] == "2026-10-01T00:00:00Z"
    assert mock_gorelo.last.query["CreatedBefore"] == "2026-10-01T00:00:01Z"


async def test_a_single_bound_is_enough(full_server, mock_gorelo):
    mock_gorelo.on("GET", ALERTS, paged_envelope([]))
    await call_tool(full_server, "list_alerts", {"created_before": "2026-10-01T00:00:00Z"})
    assert "CreatedSince" not in mock_gorelo.last.query and mock_gorelo.last.query["CreatedBefore"] == "2026-10-01T00:00:00Z"
    await call_tool(full_server, "list_alerts", {"created_since": "2026-10-01T00:00:00Z"})
    assert "CreatedBefore" not in mock_gorelo.last.query and mock_gorelo.last.query["CreatedSince"] == "2026-10-01T00:00:00Z"


@pytest.mark.parametrize(
    "args, param",
    [
        ({"sort_order": "up"}, "sort_order"),
        ({"sort_order": "DESC"}, "sort_order"),
        ({"sort_order": None}, "sort_order"),
        ({"status": ["new", 5]}, "status"),
        ({"type_ids": "4"}, "type_ids"),
        ({"client_ids": "5"}, "client_ids"),
        ({"device_ids": "abc"}, "device_ids"),
        ({"page_size": "many"}, "page_size"),
        ({"cursor": 5}, "cursor"),
        ({"created_since": 20261001}, "created_since"),
    ],
)
async def test_a_value_outside_the_schema_is_refused_with_the_param_name(full_server, mock_gorelo, args, param):
    text = await call_tool_error(full_server, "list_alerts", args)
    assert param in text
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("param", ["client_ids", "type_ids"])
@pytest.mark.parametrize("bad", [True, "5", 5.0], ids=["json-true", "text-5", "decimal-5"])
async def test_an_id_list_is_strict_true_and_text_are_refused_instead_of_becoming_an_id(full_server, mock_gorelo, param, bad):
    text = await call_tool_error(full_server, "list_alerts", {param: [bad]})
    assert param in text and "valid integer" in text
    assert mock_gorelo.requests == []


async def test_an_argument_the_tool_does_not_have_is_refused(full_server, mock_gorelo):
    for extra in ({"severity": 1}, {"title": "x"}, {"ClientIds": "5"}, {"status_ids": [1]}, {"query": "disk"}):
        text = await call_tool_error(full_server, "list_alerts", extra)
        assert next(iter(extra)) in text
    assert mock_gorelo.requests == []


# -- the post_alert advice and list_alerts agree ----------------------------------------------------------------------


async def test_the_advice_after_an_unconfirmed_post_names_a_tool_that_exists_and_a_filter_it_has(server, mock_gorelo):
    """post_alert tells the model to look with list_alerts: that tool must exist next to it, and every name the text
    uses (created_since, client_ids, newest first) must be a real part of it."""
    mock_gorelo.on("POST", ALERTS, httpx.ReadTimeout("slow"))
    text = await call_tool_error(server, "post_alert", GIVEN)
    names = {t.name: t for t in await list_tools(server)}
    assert "list_alerts" in names and "list_alerts" in text
    properties = names["list_alerts"].inputSchema["properties"]
    assert "created_since" in properties and "client_ids" in properties
    assert properties["sort_order"]["default"] == "desc"  # "newest first" is the default order
    assert "external alerts carry no client id" in text
    # the filter is not relied on for an external alert, and the parameter text agrees with the advice: an External alert
    # has no ClientId, so the filter may leave it out (it promises nothing either way, because nothing was observed)
    assert "no client_ids filter" in text
    description = properties["client_ids"]["description"]
    assert "Uptime and External alerts have no ClientId" in description and "may leave them out" in description
    assert "a Script alert without one matched through its device" in description
    assert "never match" not in description


async def test_the_post_alert_docstring_and_the_list_alerts_docstring_name_each_other():
    posting = " ".join(inspect.getdoc(alerts_module.post_alert).split())
    listing = " ".join(inspect.getdoc(alerts_module.list_alerts).split())
    assert "list_alerts" in posting and "post_alert" in listing
