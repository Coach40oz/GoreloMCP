"""tools/meta.py: health_check (toolset "core")."""

import inspect
import json
import typing
from typing import Annotated

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
    pagination,
)

import tools.alerts as alerts_module
import tools.assets as assets_module
import tools.clients as clients_module
import tools.contacts as contacts_module
import tools.meta as meta_module
import tools.org as org_module
from gorelo_client import is_forbidden_op
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio

CLIENTS = "/v1/clients"
SHAPE_PREFIX = "Gorelo returned an unexpected response for health_check"

# Every tool of the core module group (meta, clients, contacts, org, assets, alerts), all in toolset "core".
CORE_TOOLS = {
    "health_check",
    "list_clients", "get_client", "create_client", "update_client", "list_client_locations",
    "list_contacts", "get_contact", "create_contact", "update_contact",
    "list_org_groups", "list_org_users",
    "list_agents", "get_agent", "list_custom_assets",
    "post_alert", "list_alerts",
}


@pytest.fixture
def server(server_factory):
    return server_factory(toolsets={"core"})


def flat(text):
    return " ".join(text.split())


def a_client(client_id=9101, name="Example Co"):
    return {"Id": client_id, "Name": name, "Status": {"Id": 1, "Name": "Active"}, "Domains": []}


# --------------------------------------------------------------------------
# Declaration
# --------------------------------------------------------------------------


def test_health_check_is_declared_as_a_core_read_tool_over_get_clients():
    spec = next(s for s in REGISTRY.specs if s.name == "health_check")
    assert (spec.toolset, spec.kind, spec.ops, spec.destructive_hint) == ("core", "read", ["GET /v1/clients"], False)
    assert not any(is_forbidden_op(op) for op in spec.ops)  # by shape, never by the exact text of the key


def test_the_docstring_says_what_it_returns_and_that_a_failure_is_data_not_an_error():
    doc = flat(meta_module.health_check.__doc__)
    assert "returns {ok, api, total_clients, toolsets, destructive, spec_sha256}" in doc
    assert "{ok: false, error, status} instead of a tool error" in doc
    assert "Side effects:" not in doc  # read only


def test_the_module_text_says_destructive_means_whether_the_gated_tools_are_on_not_only_the_delete_tools():
    # GORELO_ENABLE_DESTRUCTIVE (the `destructive` field of the result) registers the delete and void tools
    # and also create_approved_invoice, so "whether delete tools are on" described only part of what it switches
    module_text = flat(meta_module.__doc__)
    assert "whether the gated tools are on" in module_text
    assert "whether delete tools are on" not in module_text
    gated = {spec.name for spec in REGISTRY.specs if spec.kind == "destructive"}
    assert "create_approved_invoice" in gated and any(name.startswith("delete_") for name in gated)


async def test_the_tool_has_no_parameters_and_is_read_only(server):
    tool = next(t for t in await list_tools(server) if t.name == "health_check")
    assert tool.inputSchema.get("properties", {}) == {}
    assert not tool.inputSchema.get("required")
    assert (tool.annotations.readOnlyHint, tool.annotations.destructiveHint) == (True, False)


async def test_extra_arguments_are_refused_before_any_http_call(server, mock_gorelo):
    text = await call_tool_error(server, "health_check", {"page_size": 5})
    assert "page_size" in text
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# The core modules as a whole
# --------------------------------------------------------------------------


def core_specs():
    return [spec for spec in REGISTRY.specs if spec.name in CORE_TOOLS]


def test_every_core_tool_is_registered_once_in_the_core_toolset():
    specs = core_specs()
    assert sorted(spec.name for spec in specs) == sorted(CORE_TOOLS) and len(CORE_TOOLS) == 17  # 16 until list_alerts joined
    assert {spec.toolset for spec in specs} == {"core"}


def test_no_core_tool_is_destructive_and_none_can_reach_a_delete(spec_index):
    for spec in core_specs():
        assert spec.kind in ("read", "write"), spec.name
        assert not [op for op in spec.ops if op.startswith("DELETE")], spec.name
        assert not any(is_forbidden_op(op) for op in spec.ops), spec.name
        assert all(op in spec_index.ops for op in spec.ops), spec.name


def test_only_the_two_update_tools_overwrite_data():
    overwriting = {spec.name for spec in core_specs() if spec.destructive_hint}
    assert overwriting == {"update_client", "update_contact"}


def test_every_core_tool_has_an_ascii_docstring_without_dashes():
    em_dash, en_dash = chr(0x2014), chr(0x2013)
    for spec in core_specs():
        doc = spec.fn.__doc__
        assert doc and doc.isascii() and em_dash not in doc and en_dash not in doc, spec.name


# --------------------------------------------------------------------------
# shared helpers, strict ids, concision and size
# --------------------------------------------------------------------------

CORE_MODULES = (meta_module, clients_module, contacts_module, org_module, assets_module, alerts_module)

# Words a module must not define itself any more: tools/_common.py has positive_id, positive_ids, guid, guids,
# created_id, expect_object and describe_value (tools/_common.py), and a private copy could drift.
PRIVATE_COPIES = (
    "_positive_id", "_positive_ids", "_describe", "_describe_value", "_guid", "_UUID", "_written_client",
    "_written_contact", "_alert_result",
)


def test_no_core_module_keeps_a_private_copy_of_a_shared_helper():
    for module in CORE_MODULES:
        for name in PRIVATE_COPIES:
            assert not hasattr(module, name), f"{module.__name__} still defines {name}"


def test_the_core_modules_declare_plain_optional_types_and_no_schema_skipping_aliases():
    # server.compact_input_schema now drops the null branch and "default": null from every advertised schema, so
    # the core modules write plain `X | None` and no longer hide the null branch with SkipJsonSchema aliases.
    for module in CORE_MODULES:
        assert "SkipJsonSchema" not in inspect.getsource(module), module.__name__
        for alias in ("OptStr", "OptStrs", "OptId", "OptIds"):
            assert not hasattr(module, alias), f"{module.__name__} still defines {alias}"
    for spec in core_specs():
        for name, parameter in inspect.signature(spec.fn).parameters.items():
            if parameter.default is not None:
                continue
            annotation = parameter.annotation
            base = typing.get_args(annotation)[0] if typing.get_origin(annotation) is Annotated else annotation
            assert type(None) in typing.get_args(base), f"{spec.name}.{name} is declared as {base!r}, not `X | None`"


def test_the_core_modules_use_the_shared_helpers_for_ids_and_write_answers():
    source = {module.__name__: inspect.getsource(module) for module in CORE_MODULES}
    assert "positive_id(" in source["tools.clients"] and "positive_ids(" in source["tools.clients"]
    assert "created_id(" in source["tools.clients"] and "expect_object(" in source["tools.clients"]
    assert "positive_id(" in source["tools.contacts"] and "positive_ids(" in source["tools.contacts"]
    assert "created_id(" in source["tools.contacts"] and "expect_object(" in source["tools.contacts"]
    assert "guid(" in source["tools.assets"] and "positive_ids(" in source["tools.assets"]
    assert "positive_id(" in source["tools.alerts"] and "describe_value(" in source["tools.alerts"]


# Every core tool with its id parameters, so that StrictId is checked on all of them in one place.
ID_PARAMS = {
    "list_clients": ["status_ids"],
    "get_client": ["client_id"],
    "update_client": ["client_id", "status_id"],
    "list_client_locations": ["client_id"],
    "list_contacts": ["client_id", "client_ids", "status_ids"],
    "get_contact": ["contact_id"],
    "create_contact": ["client_id", "location_id"],
    "update_contact": ["contact_id", "location_id"],
    "list_agents": ["status_ids", "client_ids"],
    "list_custom_assets": ["client_ids"],
    "post_alert": ["client_id"],
    "list_alerts": ["client_ids", "type_ids"],
}


BASE_ARGS = {
    "list_clients": {},
    "get_client": {"client_id": 1},
    "update_client": {"client_id": 1, "name": "N"},
    "list_client_locations": {"client_id": 1},
    "list_contacts": {},
    "get_contact": {"contact_id": 1},
    "create_contact": {"client_id": 1, "first_name": "a", "last_name": "b", "primary_email": "c"},
    "update_contact": {"contact_id": 1, "job_title": "X", "secondary_email": []},
    "list_agents": {},
    "list_custom_assets": {},
    "post_alert": {"name": "n", "client_id": 1, "resource": "r", "severity": 1},
    "list_alerts": {},
}


def is_integer_id(name, prop):
    """An integer id or a list of them, whether or not the schema also spells out a null branch (anyOf)."""
    if not (name.endswith("_id") or name.endswith("_ids")):
        return False
    return any(
        branch.get("type") == "integer" or (branch.get("type") == "array" and branch["items"].get("type") == "integer")
        for branch in prop.get("anyOf", [prop])
    )


async def test_the_table_of_id_parameters_covers_every_integer_id_parameter_of_the_package(server):
    found = {}
    for tool in await core_tools(server):
        ids = sorted(name for name, prop in tool.inputSchema.get("properties", {}).items() if is_integer_id(name, prop))
        if ids:
            found[tool.name] = ids
    assert found == {name: sorted(params) for name, params in ID_PARAMS.items()}
    assert set(BASE_ARGS) == set(ID_PARAMS)


@pytest.mark.parametrize("name", sorted(ID_PARAMS))
async def test_every_integer_id_parameter_is_strict_so_true_and_text_and_decimals_are_refused(server, mock_gorelo, name):
    for param in ID_PARAMS[name]:
        plural = param.endswith("s")
        for bad in ([True], ["5"], [5.0]) if plural else (True, "5", 5.0):
            text = await call_tool_error(server, name, {**BASE_ARGS[name], param: bad})
            assert param in text and "valid integer" in text, (name, param, bad, text)
    assert mock_gorelo.requests == []


async def test_the_only_core_tools_with_a_strict_flag_or_level_refuse_text_and_numbers(server, mock_gorelo):
    for name, arguments, param in (
        ("update_contact", {"contact_id": 5, "job_title": "X", "clear_secondary_email_ok": "true"}, "clear_secondary_email_ok"),
        ("post_alert", {"name": "n", "client_id": 1, "resource": "r", "severity": True}, "severity"),
    ):
        text = await call_tool_error(server, name, arguments)
        assert param in text
    assert mock_gorelo.requests == []


MAX_DESCRIPTION = 900  # never more than 900 characters per tool
MAX_PARAMETER = 220  # and never more than 220 per parameter
# Typical limits: most descriptions 700 or less, parameters 160 or less. Pinned for the whole
# core module group so that a later edit cannot quietly grow the tool list again.
TYPICAL_DESCRIPTION = 700
TYPICAL_PARAMETER = 160


async def core_tools(server):
    return [tool for tool in await list_tools(server) if tool.name in CORE_TOOLS]


async def test_every_core_tool_and_parameter_description_stays_within_the_size_limits(server):
    tools = await core_tools(server)
    assert len(tools) == len(CORE_TOOLS)
    for tool in tools:
        assert 40 < len(tool.description) <= MAX_DESCRIPTION, tool.name
        assert len(tool.description) <= TYPICAL_DESCRIPTION, tool.name
        for pname, prop in tool.inputSchema.get("properties", {}).items():
            assert 0 < len(prop["description"]) <= TYPICAL_PARAMETER, f"{tool.name}.{pname}"
            assert len(prop["description"]) <= MAX_PARAMETER, f"{tool.name}.{pname}"


# The size budget of the core module group: measured as (json.dumps of each tool
# as the client sees it, exclude_none) with every toolset on and destructive tools on. The advertised schemas are
# compacted centrally (server.compact_input_schema), so what the modules still control is their descriptions.
# Measured 19115 bytes for the first 16 tools (descriptions are 7593 of them), 19659 at the start of the
# API update and 21794 with list_alerts (about 2050 bytes, 275 characters of them the alert type names the spec
# publishes and the model needs to filter by type) and the longer post_alert text; the limit is that plus 10 percent,
# rounded up to the next 100.
CORE_BUDGET_BYTES = 24000


async def test_the_core_tool_list_stays_within_its_size_budget(server_factory):
    from settings import TOOLSETS

    server = server_factory(toolsets=frozenset(TOOLSETS), destructive=True)
    sizes = {tool.name: len(json.dumps(tool.model_dump(mode="json", exclude_none=True))) for tool in await core_tools(server)}
    assert set(sizes) == CORE_TOOLS
    assert sum(sizes.values()) <= CORE_BUDGET_BYTES, sizes


async def test_core_optional_parameters_do_not_advertise_a_null_branch(server):
    # The modules declare plain `X | None`; what keeps the null branch and "default": null out of the advertised
    # schema is server.compact_input_schema (see test_schema_compaction.py). Validation still accepts an explicit
    # null (see the per-tool tests); the schema just does not spend bytes on it.
    for tool in await core_tools(server):
        advertised = json.dumps(tool.inputSchema)
        assert '"type": "null"' not in advertised and '"default": null' not in advertised, tool.name
        for pname, prop in tool.inputSchema.get("properties", {}).items():
            assert "anyOf" not in prop, f"{tool.name}.{pname}"


@pytest.mark.parametrize("destructive", [False, True])
async def test_the_whole_package_is_visible_whether_or_not_deletes_are_enabled(server_factory, destructive):
    server = server_factory(toolsets={"core"}, destructive=destructive)
    assert CORE_TOOLS <= {tool.name for tool in await list_tools(server)}


async def test_no_core_tool_is_registered_when_the_core_toolset_is_off(server_factory):
    server = server_factory(toolsets={"tickets", "time", "billing", "uptime", "projects", "forms"})
    assert not CORE_TOOLS & {tool.name for tool in await list_tools(server)}


async def test_every_core_tool_returns_a_json_object(server_factory, mock_gorelo):
    # one call per tool shape: a list result, a single record, a boolean answer and a diagnosis
    mock_gorelo.on("GET", "/v1/organization/groups", envelope([]))
    mock_gorelo.on("GET", "/v1/clients/9101", envelope(a_client()))
    mock_gorelo.on("POST", "/v1/alerts", envelope(True))
    mock_gorelo.on("GET", CLIENTS, paged_envelope([a_client()], total_count=1))
    server = server_factory(toolsets={"core"})
    calls = [
        ("list_org_groups", {}),
        ("get_client", {"client_id": 9101}),
        ("post_alert", {"name": "n", "client_id": 9101, "resource": "r", "severity": 1}),
        ("list_clients", {}),
        ("health_check", {}),
    ]
    for name, arguments in calls:
        assert isinstance(await call_tool(server, name, arguments), dict), name


# --------------------------------------------------------------------------
# The healthy path
# --------------------------------------------------------------------------


async def test_a_healthy_api_reports_the_client_count_and_the_server_configuration(
    server_factory, mock_gorelo, spec_index
):
    mock_gorelo.on("GET", CLIENTS, paged_envelope([a_client()], next_cursor="c1", total_count=40))
    server = server_factory(toolsets={"core", "tickets"}, destructive=True)
    result = await call_tool(server, "health_check")
    assert result == {
        "ok": True,
        "api": "reachable",
        "total_clients": 40,
        "toolsets": ["core", "tickets"],
        "destructive": True,
        "spec_sha256": spec_index.sha256,
    }


async def test_destructive_off_and_a_single_toolset_are_reported_as_they_are(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, paged_envelope([a_client()], total_count=1))
    result = await call_tool(server, "health_check")
    assert result["toolsets"] == ["core"] and result["destructive"] is False and "note" not in result


async def test_exactly_one_get_with_page_size_one_is_sent(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, paged_envelope([a_client()], next_cursor="c1", total_count=40))
    await call_tool(server, "health_check")
    assert len(mock_gorelo.requests) == 1
    request = mock_gorelo.last
    assert (request.method, request.path) == ("GET", CLIENTS)
    assert request.query == {"PageSize": "1"}
    assert request.content == b"" and request.headers["x-api-key"]


async def test_a_tenant_with_no_clients_is_healthy_with_a_count_of_zero(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, envelope([], pagination(None, 0, has_more=False)))
    result = await call_tool(server, "health_check")
    assert result["ok"] is True and result["total_clients"] == 0


async def test_a_null_data_with_a_zero_total_is_an_empty_tenant_not_a_failure(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, envelope(None, pagination(None, 0, has_more=False)))
    assert (await call_tool(server, "health_check"))["total_clients"] == 0


async def test_called_without_the_lifespan_data_it_still_answers_and_says_what_is_missing(client_factory, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, paged_envelope([a_client()], total_count=40))
    async with client_factory() as client:
        result = await meta_module.health_check(make_ctx(client))
    assert result["ok"] is True and result["total_clients"] == 40
    assert (result["toolsets"], result["destructive"], result["spec_sha256"]) == (None, None, None)
    assert "toolsets, destructive, spec_sha256 could not be read" in result["note"]


async def test_a_partly_missing_lifespan_names_only_what_is_missing(client_factory, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, paged_envelope([a_client()], total_count=40))
    async with client_factory() as client:
        result = await meta_module.health_check(make_ctx(client, toolsets=["core"], destructive=False))
    assert result["toolsets"] == ["core"] and result["destructive"] is False and result["spec_sha256"] is None
    assert result["note"].startswith("spec_sha256 could not be read")


async def test_without_a_gorelo_client_the_tool_raises_instead_of_pretending_to_diagnose():
    with pytest.raises(RuntimeError, match="the Gorelo client is not available"):
        await meta_module.health_check(make_ctx())


# --------------------------------------------------------------------------
# Gorelo failures come back as data
# --------------------------------------------------------------------------


async def test_an_unauthorized_key_is_reported_not_raised(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, error_envelope(401, [("070401", "Invalid API key.")]))
    result = await call_tool(server, "health_check")
    assert result == {
        "ok": False,
        "error": f"Gorelo rejected health_check (HTTP 401, code 070401): Invalid API key. [trace {TEST_TRACE_ID}]",
        "status": 401,
    }


async def test_a_missing_scope_is_reported_with_the_scope_name(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, error_envelope(403, [("080203", "API key does not have 'Clients' scope")]))
    result = await call_tool(server, "health_check")
    assert result["ok"] is False and result["status"] == 403
    assert "the API key does not have the 'Clients' scope" in result["error"]


async def test_a_server_error_with_a_gateway_page_is_reported_with_its_status(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, httpx.Response(502, text="Bad gateway", headers={"content-type": "text/plain"}))
    result = await call_tool(server, "health_check")
    assert result["ok"] is False and result["status"] == 502
    assert result["error"].startswith(SHAPE_PREFIX) and "HTTP 502" in result["error"]


async def test_a_timeout_is_reported_with_no_status_and_says_retrying_is_safe(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, httpx.ReadTimeout("slow"))
    result = await call_tool(server, "health_check")
    assert result["ok"] is False and result["status"] is None
    assert result["error"].startswith("Gorelo did not answer health_check (the request timed out)")
    assert "retrying is safe" in result["error"]
    assert len(mock_gorelo.requests) == 1  # a read that timed out is not retried by the client


async def test_a_connection_failure_is_reported(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, httpx.ConnectError("refused"))
    result = await call_tool(server, "health_check")
    assert result["ok"] is False and result["status"] is None
    assert "the connection failed" in result["error"]


async def test_a_persistent_rate_limit_is_reported_with_status_429(server, mock_gorelo):
    mock_gorelo.on(
        "GET",
        CLIENTS,
        httpx.Response(429, json={"error": "rate_limited", "retry_after": "0s"}),
        headers={"Retry-After": "0"},
    )
    result = await call_tool(server, "health_check")
    assert result["ok"] is False and result["status"] == 429
    assert "rate limiting requests (HTTP 429) for health_check" in result["error"]


async def test_a_failed_envelope_with_http_200_is_a_failure(server, mock_gorelo):
    body = error_envelope(200, [("070101", "Something went wrong.")])
    mock_gorelo.on("GET", CLIENTS, body)
    result = await call_tool(server, "health_check")
    assert result["ok"] is False and result["status"] == 200
    assert "Something went wrong." in result["error"]


# --------------------------------------------------------------------------
# An unexpected shape is a failure (the legacy check only looked at the HTTP status)
# --------------------------------------------------------------------------


async def test_the_legacy_lowercase_body_is_not_healthy(server, mock_gorelo):
    # Status 200 and valid JSON, but not the PascalCase envelope: the old check said ok, and the old
    # tools read it as zero rows.
    mock_gorelo.on("GET", CLIENTS, {"data": [{"id": 1, "name": "Acme"}], "nextCursor": None, "hasMore": False})
    result = await call_tool(server, "health_check")
    assert result["ok"] is False and result["status"] == 200
    assert result["error"].startswith(SHAPE_PREFIX) and "refusing to guess" in result["error"]


async def test_an_html_page_with_http_200_is_not_healthy(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, httpx.Response(200, text="<html>maintenance</html>", headers={"content-type": "text/html"}))
    result = await call_tool(server, "health_check")
    assert result["ok"] is False and result["status"] == 200
    assert "refusing to guess" in result["error"]


@pytest.mark.parametrize(
    "body, fragment",
    [
        pytest.param(envelope({"Id": 1}, pagination(None, 1)), "expected Data to be a list but got an object", id="data-is-an-object"),
        pytest.param(envelope([a_client()]), "paged response without Pagination", id="no-pagination"),
        pytest.param(
            envelope([a_client()], {"NextCursor": None, "HasMore": False}),
            "has no TotalCount",
            id="no-total-count",
        ),
        pytest.param(paged_envelope(["not an object"], total_count=1), "rows are not client objects (found str)", id="rows-are-strings"),
        pytest.param(
            paged_envelope([{"id": 9101, "name": "Example Co"}], total_count=1),
            "first client row has no Id field",
            id="row-has-no-id",
        ),
        pytest.param(envelope([], pagination(None, 5, has_more=False)), "reports TotalCount=5 but returned no rows", id="total-without-rows"),
        pytest.param(
            envelope([a_client()], {"NextCursor": None, "HasMore": "no", "TotalCount": 1}),
            "HasMore is missing or not a boolean",
            id="has-more-not-boolean",
        ),
    ],
)
async def test_a_response_that_is_not_a_page_of_clients_is_not_healthy(server, mock_gorelo, body, fragment):
    mock_gorelo.on("GET", CLIENTS, body)
    result = await call_tool(server, "health_check")
    assert result["ok"] is False and result["status"] == 200
    assert fragment in result["error"]
    assert set(result) == {"ok", "error", "status"}


async def test_a_failure_never_claims_the_api_is_reachable(server, mock_gorelo):
    mock_gorelo.on("GET", CLIENTS, error_envelope(500, [("070500", "Internal error")]))
    result = await call_tool(server, "health_check")
    assert result["ok"] is False and "api" not in result and "total_clients" not in result
