"""tools/_common.py: the registry, @gorelo_tool (strict confirm), parameter and id helpers, the strict id and
bool types, answer shape checks, result shapes, error text; plus the test infrastructure that keeps FastMCP's
loggers quiet."""

import asyncio
import enum
import functools
import inspect
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

import httpx
import pytest
from conftest import (
    LOGGING_TEST_MODULES,
    QUIET_LOGGERS,
    call_tool,
    call_tool_error,
    call_tool_raw,
    envelope,
    error_envelope,
    list_tools,
    make_ctx,
    quiet_loggers,
    uid,
)
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from pydantic import Field, Strict

import tools
from gorelo_client import (
    CURRENT_TOOL,
    EXPORT_NOTE,
    SIDE_EFFECT_GETS,
    AllResult,
    GoreloAPIError,
    Page,
    is_forbidden_op,
    is_side_effect_get,
)
from settings import TOOLSETS
from spec import SpecViolation
from tools._common import (
    GUID_EXAMPLE,
    MAX_ID,
    REGISTRY,
    Registry,
    RegistryError,
    StrictBool,
    StrictId,
    ToolSpec,
    all_result,
    build_body,
    clamp_page_size,
    client_of,
    created_id,
    csv_ids,
    describe_value,
    expect_object,
    format_gorelo_error,
    gorelo_tool,
    guid,
    guids,
    list_result,
    non_empty,
    ok_result,
    paged_result,
    positive_id,
    positive_ids,
    region_code,
    reread_after_write,
    require_confirm,
    server_info_of,
    utc_iso,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.anyio


def declare(registry, **kwargs):
    kwargs.setdefault("toolset", "core")
    kwargs.setdefault("kind", "read")
    kwargs.setdefault("ops", ["GET /v1/clients"])
    return registry.tool(**kwargs)


# --------------------------------------------------------------------------
# Declaration checks
# --------------------------------------------------------------------------


def test_gorelo_tool_is_the_tool_decorator_of_the_real_registry():
    assert gorelo_tool == REGISTRY.tool
    assert isinstance(REGISTRY, Registry)


def test_an_unknown_toolset_or_kind_is_refused_at_declaration():
    registry = Registry()
    with pytest.raises(RegistryError, match="toolset must be one of"):
        declare(registry, toolset="billings")
    with pytest.raises(RegistryError, match="kind must be one of"):
        declare(registry, kind="delete")
    assert registry.specs == []


def test_registry_errors_are_value_errors_and_runtime_errors():
    assert issubclass(RegistryError, ValueError) and issubclass(RegistryError, RuntimeError)


@pytest.mark.parametrize("ops", [[], (), "GET /v1/clients", None, [""], ["GET /v1/clients", "  "], [5]])
def test_ops_must_be_a_non_empty_list_of_strings(ops):
    with pytest.raises(RegistryError, match="ops"):
        declare(Registry(), ops=ops)


def test_field_map_must_map_names_to_paths():
    with pytest.raises(RegistryError, match="field_map"):
        declare(Registry(), field_map={"name": ""})
    with pytest.raises(RegistryError, match="field_map"):
        declare(Registry(), field_map={"name": 5})


@pytest.mark.parametrize("name", [" ", "has space", "dot.name", "x" * 65, "caf\u00e9"])
def test_tool_names_must_be_valid_for_claude_ai(name):
    registry = Registry()

    async def fn(ctx: Context) -> dict:
        return {}

    with pytest.raises(RegistryError, match="tool name"):
        declare(registry, name=name)(fn)
    assert registry.specs == []


def test_duplicate_tool_names_are_an_error():
    registry = Registry()

    @declare(registry)
    async def same(ctx: Context) -> dict:
        return {}

    with pytest.raises(RegistryError, match="duplicate tool name 'same'"):

        @declare(registry, name="same")
        async def other(ctx: Context) -> dict:
            return {}

    assert [spec.name for spec in registry.specs] == ["same"]


def test_a_destructive_tool_must_declare_confirm():
    registry = Registry()

    async def delete_thing(ctx: Context, thing_id: int) -> dict:
        return {}

    with pytest.raises(RegistryError, match="strict boolean 'confirm' parameter") as info:
        declare(registry, kind="destructive", ops=["DELETE /v1/items/{itemId}"])(delete_thing)
    assert "confirm: Annotated[StrictBool, Field(description=...)] = False" in str(info.value)
    assert registry.specs == []


def test_a_destructive_confirm_must_not_default_to_true():
    registry = Registry()

    async def delete_thing(ctx: Context, confirm: StrictBool = True) -> dict:
        return {}

    with pytest.raises(RegistryError, match="must default to False"):
        declare(registry, kind="destructive", ops=["DELETE /v1/items/{itemId}"])(delete_thing)


# The four defaults below are strict on purpose: the default is then the only thing wrong with them.
async def _confirm_true_via_field(ctx: Context, confirm: StrictBool = Field(default=True)) -> dict:
    return {}


async def _confirm_true_via_annotated_field(ctx: Context, confirm: Annotated[StrictBool, Field(default=True)]) -> dict:
    return {}


async def _confirm_true_annotated_with_python_default(
    ctx: Context, confirm: Annotated[StrictBool, Field(description="Must be true.")] = True
) -> dict:
    return {}


async def _confirm_with_no_default(
    ctx: Context, confirm: Annotated[StrictBool, Field(description="Must be true.")]
) -> dict:
    return {}


async def _confirm_that_may_be_null(ctx: Context, confirm: bool | None = False) -> dict:
    return {}


async def _confirm_that_is_a_number(ctx: Context, confirm: int = 0) -> dict:
    return {}


async def _confirm_that_is_text(ctx: Context, confirm: str = "false") -> dict:
    return {}


@pytest.mark.parametrize(
    "fn, fragment",
    [
        pytest.param(_confirm_true_via_field, "must default to False", id="StrictBool-Field(default=True)"),
        pytest.param(_confirm_true_via_annotated_field, "must default to False", id="Annotated-StrictBool-Field(default=True)"),
        pytest.param(_confirm_true_annotated_with_python_default, "must default to False", id="Annotated-StrictBool-then-=-True"),
        pytest.param(_confirm_with_no_default, "must default to False", id="required-confirm"),
        pytest.param(_confirm_that_may_be_null, "must be a boolean", id="bool-or-None"),
        pytest.param(_confirm_that_is_a_number, "must be a boolean", id="int"),
        pytest.param(_confirm_that_is_text, "must be a boolean", id="str"),
    ],
)
def test_confirm_is_checked_on_the_generated_schema_not_on_the_python_default(fn, fragment):
    registry = Registry()
    with pytest.raises(RegistryError, match=fragment) as info:
        declare(registry, kind="destructive", ops=["DELETE /v1/items/{itemId}"])(fn)
    assert fn.__name__ in str(info.value) and "confirm" in str(info.value)
    assert registry.specs == []


async def _confirm_plain(ctx: Context, confirm: StrictBool = False) -> dict:
    return {}


async def _confirm_annotated(
    ctx: Context, confirm: Annotated[StrictBool, Field(description="Must be true.")] = False
) -> dict:
    return {}


async def _confirm_field(ctx: Context, confirm: StrictBool = Field(default=False, description="Must be true.")) -> dict:
    return {}


async def _confirm_annotated_field_default(ctx: Context, confirm: Annotated[StrictBool, Field(default=False)]) -> dict:
    return {}


# The other spellings of "strict": Strict() written out, and pydantic's strict=True on the Field.
async def _confirm_strict_written_out(
    ctx: Context, confirm: Annotated[bool, Strict(), Field(description="Must be true.")] = False
) -> dict:
    return {}


async def _confirm_field_strict_true(
    ctx: Context, confirm: Annotated[bool, Field(description="Must be true.", strict=True)] = False
) -> dict:
    return {}


async def _confirm_field_default_strict_true(
    ctx: Context, confirm: bool = Field(default=False, description="Must be true.", strict=True)
) -> dict:
    return {}


def _confirm_strict_in_a_sync_tool(ctx: Context, confirm: StrictBool = False) -> dict:
    return {}


STRICT_CONFIRM_FUNCTIONS = [
    pytest.param(_confirm_plain, id="StrictBool"),
    pytest.param(_confirm_annotated, id="Annotated-StrictBool-Field"),
    pytest.param(_confirm_field, id="StrictBool-Field(default=False)"),
    pytest.param(_confirm_annotated_field_default, id="Annotated-StrictBool-Field(default=False)"),
    pytest.param(_confirm_strict_written_out, id="Annotated-bool-Strict()-Field"),
    pytest.param(_confirm_field_strict_true, id="Annotated-bool-Field(strict=True)"),
    pytest.param(_confirm_field_default_strict_true, id="bool-Field(default=False-strict=True)"),
]
SYNC_STRICT_CONFIRM_FUNCTIONS = [pytest.param(_confirm_strict_in_a_sync_tool, id="sync-StrictBool")]


@pytest.mark.parametrize("fn", [*STRICT_CONFIRM_FUNCTIONS, *SYNC_STRICT_CONFIRM_FUNCTIONS])
async def test_every_way_of_declaring_a_strict_false_default_registers(fn):
    registry = Registry()
    declare(registry, name="drop", kind="destructive", ops=["DELETE /v1/items/{itemId}"])(fn)
    server = FastMCP("t")
    server.add_tool(registry.specs[0].fn)
    schema = (await server.get_tool("drop")).parameters["properties"]["confirm"]
    assert schema["type"] == "boolean" and schema["default"] is False


async def _lax_plain(ctx: Context, confirm: bool = False) -> dict:
    return {}


async def _lax_annotated(ctx: Context, confirm: Annotated[bool, Field(description="Must be true.")] = False) -> dict:
    return {}


async def _lax_field(ctx: Context, confirm: bool = Field(default=False, description="Must be true.")) -> dict:
    return {}


async def _lax_annotated_field_default(ctx: Context, confirm: Annotated[bool, Field(default=False)]) -> dict:
    return {}


async def _lax_explicitly_not_strict(
    ctx: Context, confirm: Annotated[bool, Field(description="Must be true.", strict=False)] = False
) -> dict:
    return {}


async def _lax_strict_switched_off(
    ctx: Context, confirm: Annotated[bool, Strict(False), Field(description="Must be true.")] = False
) -> dict:
    return {}


def _lax_sync(ctx: Context, confirm: bool = False) -> dict:
    return {}


LAX_CONFIRM_FUNCTIONS = [
    pytest.param(_lax_plain, id="bool"),
    pytest.param(_lax_annotated, id="Annotated-bool-Field"),
    pytest.param(_lax_field, id="bool-Field(default=False)"),
    pytest.param(_lax_annotated_field_default, id="Annotated-bool-Field(default=False)"),
    pytest.param(_lax_explicitly_not_strict, id="Field(strict=False)"),
    pytest.param(_lax_strict_switched_off, id="Strict(False)"),  # carries a Strict marker that does not make it strict
    pytest.param(_lax_sync, id="sync-bool"),
]


@pytest.mark.parametrize("fn", LAX_CONFIRM_FUNCTIONS)
def test_a_lax_confirm_is_refused_and_the_message_gives_the_form_to_use(fn):
    registry = Registry()
    with pytest.raises(RegistryError, match="must be a strict boolean") as info:
        declare(registry, kind="destructive", ops=["DELETE /v1/items/{itemId}"])(fn)
    message = str(info.value)
    assert fn.__name__ in message and "confirm" in message
    assert "'true'" in message  # what pydantic accepted, so the reason is visible
    assert "confirm: Annotated[StrictBool, Field(description=...)] = False" in message
    assert "Annotated[bool, Strict(), Field(...)]" in message and "tools._common" in message
    assert registry.specs == []


def test_a_postponed_annotation_is_judged_by_what_it_resolves_to(tmp_path):
    import importlib.util

    source = """
from __future__ import annotations

from typing import Annotated

from fastmcp import Context
from pydantic import Field

from tools._common import Registry, StrictBool

REGISTRY = Registry()


@REGISTRY.tool(toolset="core", kind="destructive", ops=["DELETE /v1/items/{itemId}"])
async def strict_one(ctx: Context, confirm: Annotated[StrictBool, Field(description="Must be true.")] = False) -> dict:
    return {}


try:
    @REGISTRY.tool(toolset="core", kind="destructive", ops=["DELETE /v1/items/{itemId}"])
    async def lax_one(ctx: Context, confirm: Annotated[bool, Field(description="Must be true.")] = False) -> dict:
        return {}
except Exception as exc:
    LAX_ERROR = exc
"""
    path = tmp_path / "postponed_confirm.py"
    path.write_text(source, encoding="utf-8")
    module_spec = importlib.util.spec_from_file_location("postponed_confirm_for_test", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    assert [spec.name for spec in module.REGISTRY.specs] == ["strict_one"]
    assert isinstance(module.LAX_ERROR, RegistryError) and "must be a strict boolean" in str(module.LAX_ERROR)


def _counting(fn, calls):
    """fn's twin that records each `confirm` it is called with (functools.wraps keeps signature and annotations)."""

    @functools.wraps(fn)
    async def inner(*args, **kwargs):
        calls.append(kwargs.get("confirm"))
        return {"ran": True}

    return inner


@pytest.mark.parametrize("fn", STRICT_CONFIRM_FUNCTIONS)
async def test_a_strict_confirm_refuses_text_and_numbers_but_takes_a_real_boolean(fn):
    calls = []
    registry = Registry()
    declare(registry, name="drop", kind="destructive", ops=["DELETE /v1/items/{itemId}"])(_counting(fn, calls))
    server = FastMCP("t")
    server.add_tool(registry.specs[0].fn)
    for value in ("true", "True", "yes", "1", "on", 1, 0, 1.0, None, [True]):
        text = await call_tool_error(server, "drop", {"confirm": value})
        assert "confirm" in text and "valid boolean" in text, (value, text)
    assert calls == []  # the tool body never ran
    assert await call_tool(server, "drop", {"confirm": True}) == {"ran": True}
    assert await call_tool(server, "drop", {"confirm": False}) == {"ran": True}
    assert await call_tool(server, "drop", {}) == {"ran": True}
    assert calls == [True, False, False]  # omitted: the declared default, False


def test_a_destructive_tool_whose_annotations_cannot_be_resolved_is_refused_not_waved_through():
    registry = Registry()

    async def delete_thing(ctx: Context, confirm: "NotDefinedAnywhere" = False) -> dict:  # noqa: F821
        return {}

    with pytest.raises(RegistryError, match="cannot build its parameter schema"):
        declare(registry, kind="destructive", ops=["DELETE /v1/items/{itemId}"])(delete_thing)
    assert registry.specs == []


def test_a_destructive_tool_with_confirm_registers():
    registry = Registry()

    @declare(registry, kind="destructive", ops=["DELETE /v1/items/{itemId}"])
    async def delete_item(
        ctx: Context, item_id: str, confirm: Annotated[StrictBool, Field(description="Must be true.")] = False
    ) -> dict:
        return {}

    assert registry.specs[0].kind == "destructive"


def test_other_kinds_do_not_need_confirm():
    registry = Registry()

    @declare(registry, kind="write", ops=["POST /v1/clients"])
    async def make_client(ctx: Context) -> dict:
        return {}

    assert registry.specs[0].kind == "write"


def test_the_strict_confirm_rule_is_for_destructive_tools_only():
    # a write tool may take any `confirm` it likes: only a destructive tool is gated on it
    registry = Registry()

    @declare(registry, kind="write", ops=["POST /v1/clients"])
    async def make_client(ctx: Context, confirm: bool = False) -> dict:
        return {}

    assert registry.specs[0].kind == "write"


def _tool_for(kind, **options):
    async def thing(ctx: Context, confirm: StrictBool = False) -> dict:
        """Do a thing."""
        return {}

    registry = Registry()
    ops = {"read": ["GET /v1/clients"], "write": ["POST /v1/clients"], "destructive": ["DELETE /v1/items/{itemId}"]}[kind]
    declare(registry, kind=kind, ops=ops, **options)(thing)
    return registry


@pytest.mark.parametrize(
    "kind, hint, expected",
    [
        ("read", None, False), ("read", False, False),
        ("write", None, False), ("write", False, False), ("write", True, True),
        ("destructive", None, True), ("destructive", True, True),
    ],
)
async def test_destructive_hint_decides_the_destructive_annotation(kind, hint, expected):
    registry = _tool_for(kind, destructive_hint=hint)
    (spec,) = registry.specs
    assert spec.destructive_hint is expected
    server = FastMCP("t")
    server.add_tool(spec.fn)
    annotations = (await server.get_tool("thing")).annotations
    assert annotations.destructiveHint is expected
    assert annotations.readOnlyHint is (kind == "read") and annotations.openWorldHint is True
    listed = (await list_tools(server))[0]
    assert listed.annotations.destructiveHint is expected


def test_the_default_destructive_hint_depends_on_the_kind():
    assert [_tool_for(kind).specs[0].destructive_hint for kind in ("read", "write", "destructive")] == [False, False, True]


@pytest.mark.parametrize(
    "kind, hint, fragment",
    [
        ("read", True, "a read tool cannot be destructive"),
        ("destructive", False, "always has destructive_hint True"),
        ("write", "yes", "must be True, False or None"),
        ("write", 1, "must be True, False or None"),
        ("read", 0, "must be True, False or None"),
    ],
)
def test_a_contradictory_destructive_hint_is_refused(kind, hint, fragment):
    with pytest.raises(RegistryError, match=fragment):
        _tool_for(kind, destructive_hint=hint)


def test_a_read_tool_cannot_declare_an_operation_that_gorelo_records():
    registry = Registry()

    async def export_pdf(ctx: Context) -> dict:
        return {}

    for op in sorted(SIDE_EFFECT_GETS):
        with pytest.raises(RegistryError, match="read tool cannot declare") as info:
            declare(registry, kind="read", ops=["GET /v1/invoices", op])(export_pdf)
        assert op in str(info.value) and "kind='write'" in str(info.value)
        declare(registry, kind="write", ops=[op])(export_pdf)  # as a write tool it is fine
    assert [spec.kind for spec in registry.specs] == ["write"]


@pytest.mark.parametrize("name", ["id", "documentId", "ID"])
def test_a_read_tool_cannot_declare_the_pdf_export_under_any_placeholder_name(name):
    # compared by shape, like FORBIDDEN_OPS: a Gorelo rename of the placeholder must not let a "read" tool declare it
    registry = Registry()

    async def export_pdf(ctx: Context) -> dict:
        return {}

    (pdf,) = SIDE_EFFECT_GETS
    variant = re.sub(r"\{[^{}]*\}", "{" + name + "}", pdf)
    with pytest.raises(RegistryError, match="read tool cannot declare") as info:
        declare(registry, kind="read", ops=["GET /v1/invoices", variant])(export_pdf)
    assert variant in str(info.value) and "kind='write'" in str(info.value)
    declare(registry, kind="write", ops=[variant])(export_pdf)  # as a write tool it is fine
    assert [spec.kind for spec in registry.specs] == ["write"]


# --------------------------------------------------------------------------
# What registration records
# --------------------------------------------------------------------------


def test_toolspec_records_the_declaration():
    registry = Registry()
    field_map = {"name": "Name"}
    ops = ["POST /v1/clients", "GET /v1/clients/{clientId}"]

    @declare(registry, toolset="core", kind="write", ops=ops, field_map=field_map, title="Create a client")
    async def create_client(ctx: Context, name: str) -> dict:
        return {}

    (spec,) = registry.specs
    assert isinstance(spec, ToolSpec)
    assert (spec.name, spec.toolset, spec.kind) == ("create_client", "core", "write")
    assert spec.ops == ops and spec.ops is not ops
    assert spec.field_map == field_map and spec.field_map is not field_map
    assert spec.fn is create_client and callable(spec.fn)


def test_the_name_can_be_overridden_and_field_map_defaults_to_empty():
    registry = Registry()

    @declare(registry, name="list_things")
    async def whatever(ctx: Context) -> dict:
        return {}

    assert registry.specs[0].name == "list_things" and registry.specs[0].field_map == {}


def test_registration_order_is_kept():
    registry = Registry()
    for name in ("b_tool", "a_tool", "c_tool"):
        declare(registry, name=name)(_noop())
    assert [s.name for s in registry.specs] == ["b_tool", "a_tool", "c_tool"]
    registry.specs.clear()  # a copy: the registry itself is untouched
    assert len(registry.specs) == 3


def _noop():
    async def fn(ctx: Context) -> dict:
        return {}

    return fn


def _toolset_registry():
    registry = Registry()
    declare(registry, name="core_read", toolset="core", kind="read")(_noop())
    declare(registry, name="core_write", toolset="core", kind="write", ops=["POST /v1/clients"])(_noop())

    async def core_delete(ctx: Context, confirm: StrictBool = False) -> dict:
        return {}

    declare(registry, name="core_delete", toolset="core", kind="destructive", ops=["DELETE /v1/items/{itemId}"])(core_delete)
    declare(registry, name="tickets_read", toolset="tickets", kind="read", ops=["GET /v1/tickets"])(_noop())
    declare(registry, name="projects_read", toolset="projects", kind="read", ops=["GET /v1/projects"])(_noop())
    return registry


def test_select_filters_by_toolset_and_gates_destructive_tools():
    registry = _toolset_registry()

    def names(toolsets, destructive):
        return [s.name for s in registry.select(toolsets, destructive)]

    assert names({"core"}, False) == ["core_read", "core_write"]
    assert names({"core"}, True) == ["core_read", "core_write", "core_delete"]
    assert names({"core", "tickets"}, False) == ["core_read", "core_write", "tickets_read"]
    assert names(frozenset({"tickets"}), True) == ["tickets_read"]
    assert names(["projects"], False) == ["projects_read"]
    assert names(set(), True) == []
    assert names(TOOLSETS, True) == [s.name for s in registry.specs]


# --------------------------------------------------------------------------
# FastMCP metadata and schema identity
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, read_only, destructive, idempotent",
    [("read", True, False, True), ("write", False, False, False), ("destructive", False, True, False)],
)
async def test_fastmcp_metadata_name_title_tags_annotations(kind, read_only, destructive, idempotent):
    registry = Registry()

    async def thing(ctx: Context, confirm: StrictBool = False) -> dict:
        """Do a thing."""
        return {}

    ops = {"read": ["GET /v1/clients"], "write": ["POST /v1/clients"], "destructive": ["DELETE /v1/items/{itemId}"]}[kind]
    declare(registry, toolset="billing", kind=kind, ops=ops, title="Thing title")(thing)
    server = FastMCP("t")
    for spec in registry.specs:
        server.add_tool(spec.fn)
    tool = await server.get_tool("thing")
    assert tool.name == "thing" and tool.title == "Thing title"
    assert tool.tags == {"billing", kind}
    ann = tool.annotations
    assert (ann.readOnlyHint, ann.destructiveHint, ann.idempotentHint, ann.openWorldHint) == (
        read_only, destructive, idempotent, True,
    )
    listed = (await list_tools(server))[0]
    assert listed.annotations.readOnlyHint is read_only and listed.annotations.openWorldHint is True


def _example_function():
    async def search_things(
        ctx: Context,
        query: Annotated[str | None, Field(description="Keyword matched against names.")] = None,
        status_ids: Annotated[list[int] | None, Field(description="Status ids to include.")] = None,
        page_size: Annotated[int, Field(description="Rows per page, 1-200.", ge=1, le=200)] = 200,
        include_closed: Annotated[bool, Field(description="Also return closed things.")] = False,
        sort: Annotated[str, Field(description="Sort order.")] = "asc",
        cursor: Annotated[str | None, Field(description="next_cursor from the previous call.")] = None,
    ) -> dict:
        """List things.

        Resolve ids first: status_id -> list_statuses.
        Side effects: None (read-only).
        Paging: pass next_cursor back as cursor with the SAME filters until has_more is false.
        """
        return {"query": query, "ids": status_ids, "page_size": page_size, "closed": include_closed, "sort": sort, "cursor": cursor}

    return search_things


def test_the_wrapper_keeps_the_signature_annotations_and_docstring():
    raw = _example_function()
    registry = Registry()
    decorated = declare(registry)(raw)
    assert decorated is not raw and decorated.__wrapped__ is raw
    assert inspect.signature(decorated) == inspect.signature(raw)
    assert decorated.__name__ == "search_things" and decorated.__doc__ == raw.__doc__
    assert decorated.__annotations__ == raw.__annotations__
    assert inspect.iscoroutinefunction(decorated)


async def test_the_generated_schema_is_identical_to_the_undecorated_functions():
    raw = _example_function()
    reference = Tool.from_function(raw)
    registry = Registry()
    decorated = declare(registry)(_example_function())
    wrapped = Tool.from_function(decorated)
    assert wrapped.parameters == reference.parameters
    assert wrapped.output_schema == reference.output_schema
    assert wrapped.description == reference.description

    properties = reference.parameters["properties"]
    assert list(properties) == ["query", "status_ids", "page_size", "include_closed", "sort", "cursor"]
    assert "ctx" not in properties and "ctx" not in reference.parameters.get("required", [])
    assert properties["query"]["description"] == "Keyword matched against names."
    assert properties["status_ids"]["description"] == "Status ids to include."
    assert properties["page_size"]["default"] == 200 and properties["page_size"]["maximum"] == 200
    assert properties["include_closed"]["default"] is False and properties["sort"]["default"] == "asc"

    # and through a real server and an MCP client
    server = FastMCP("t")
    for spec in registry.specs:
        server.add_tool(spec.fn)
    (listed,) = await list_tools(server)
    assert listed.inputSchema == reference.parameters
    assert "ctx" not in listed.inputSchema["properties"]
    assert (listed.description or "").startswith("List things.")


async def test_the_ctx_parameter_is_injected_and_never_part_of_the_arguments():
    registry = Registry()

    @declare(registry)
    async def whoami(ctx: Context, label: Annotated[str, Field(description="A label.")] = "x") -> dict:
        """Return the label."""
        return {"label": label, "has_ctx": hasattr(ctx, "lifespan_context")}

    server = FastMCP("t")
    server.add_tool(registry.specs[0].fn)
    assert await call_tool(server, "whoami", {"label": "y"}) == {"label": "y", "has_ctx": True}


FUTURE_MODULE = '''
from __future__ import annotations

from typing import Annotated, Literal

from fastmcp import Context
from pydantic import Field

from tools._common import Registry

REGISTRY = Registry()


@REGISTRY.tool(toolset="core", kind="read", ops=["GET /v1/clients"])
async def future_tool(
    ctx: Context,
    mode: Annotated[Literal["a", "b"], Field(description="Mode.")] = "a",
    ids: Annotated[list[int] | None, Field(description="Ids.")] = None,
) -> dict:
    """A tool in a module that uses postponed annotations."""
    return {"mode": mode, "ids": ids}
'''


async def test_a_tool_module_with_postponed_annotations_keeps_its_schema(tmp_path):
    import importlib.util

    path = tmp_path / "future_tools.py"
    path.write_text(FUTURE_MODULE, encoding="utf-8")
    module_spec = importlib.util.spec_from_file_location("future_tools_for_test", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    (spec,) = module.REGISTRY.specs
    reference = Tool.from_function(spec.fn.__wrapped__)
    wrapped = Tool.from_function(spec.fn)
    assert wrapped.parameters == reference.parameters
    assert wrapped.parameters["properties"]["mode"] == {
        "default": "a", "description": "Mode.", "enum": ["a", "b"], "type": "string",
    }
    server = FastMCP("t")
    server.add_tool(spec.fn)
    assert await call_tool(server, "future_tool", {"mode": "b", "ids": [1]}) == {"mode": "b", "ids": [1]}
    assert "'a' or 'b'" in await call_tool_error(server, "future_tool", {"mode": "zzz"})


async def test_a_tool_may_return_a_toolresult_and_may_omit_ctx():
    from fastmcp.tools import ToolResult

    registry = Registry()

    @declare(registry, kind="write", ops=["GET /v1/invoices/{invoiceId}/pdf"])
    async def export_like(ctx: Context) -> ToolResult:
        """Return a ToolResult."""
        return ToolResult(content="summary text", structured_content={"name": "INV-1", "size": 3})

    @declare(registry)
    async def no_context(value: Annotated[int, Field(description="A number.")] = 2) -> dict:
        """A local table tool: it never touches Gorelo."""
        return {"double": value * 2}

    server = FastMCP("t")
    for spec in registry.specs:
        server.add_tool(spec.fn)
    assert await call_tool(server, "export_like") == {"name": "INV-1", "size": 3}
    assert await call_tool(server, "no_context", {"value": 21}) == {"double": 42}
    tools_by_name = {tool.name: tool for tool in await list_tools(server)}
    assert tools_by_name["export_like"].outputSchema is None
    assert "ctx" not in tools_by_name["no_context"].inputSchema["properties"]


# --------------------------------------------------------------------------
# Error translation
# --------------------------------------------------------------------------


def _error_registry():
    registry = Registry()
    field_map = {"location_phone": "Location.Phone", "name": "Name"}

    @declare(registry, toolset="core", kind="write", ops=["POST /v1/clients"], field_map=field_map)
    async def raises(ctx: Context, what: str) -> dict:
        """Raise whatever is asked."""
        if what == "gorelo":
            raise GoreloAPIError(
                "x", status=400, op_key="POST /v1/clients", kind="http", trace_id="00-trace-1",
                notifications=[{"code": "070101", "message": "Mobile phone validation failed", "property": "Phone"}],
            )
        if what == "spec":
            raise SpecViolation("POST /v1/clients", "Location.Phonee", "POST /v1/clients: unknown body field 'Location.Phonee'")
        if what == "value":
            raise ValueError("name: must not be empty")
        if what == "tool":
            raise ToolError("tool says no")
        if what == "key":
            raise KeyError("a bug")
        return {"ok": True}

    @declare(registry, toolset="core", kind="read", ops=["GET /v1/clients"])
    def sync_raises(ctx: Context) -> dict:
        """A synchronous tool."""
        raise ValueError("sync: bad value")

    return registry


async def test_gorelo_errors_become_tool_errors_naming_the_snake_case_param():
    server = FastMCP("t")
    for spec in _error_registry().specs:
        server.add_tool(spec.fn)
    text = await call_tool_error(server, "raises", {"what": "gorelo"})
    assert text == "Gorelo rejected raises (HTTP 400, code 070101): location_phone: Mobile phone validation failed [trace 00-trace-1]"


async def test_spec_violations_and_value_errors_keep_their_own_message():
    server = FastMCP("t")
    for spec in _error_registry().specs:
        server.add_tool(spec.fn)
    assert await call_tool_error(server, "raises", {"what": "spec"}) == "POST /v1/clients: unknown body field 'Location.Phonee'"
    assert await call_tool_error(server, "raises", {"what": "value"}) == "name: must not be empty"
    assert await call_tool_error(server, "raises", {"what": "tool"}) == "tool says no"
    assert await call_tool_error(server, "sync_raises", {}) == "sync: bad value"
    assert await call_tool(server, "raises", {"what": "fine"}) == {"ok": True}


async def test_other_exceptions_are_not_swallowed_or_rewritten():
    registry = _error_registry()
    raises = registry.specs[0].fn
    with pytest.raises(KeyError):
        await raises(make_ctx(), what="key")
    with pytest.raises(ToolError, match="tool says no"):
        await raises(make_ctx(), what="tool")


async def test_a_decorated_function_can_be_called_directly_in_a_unit_test():
    registry = _error_registry()
    raises = registry.specs[0].fn
    assert await raises(make_ctx(), what="fine") == {"ok": True}
    with pytest.raises(ToolError) as info:
        await raises(make_ctx(), what="gorelo")
    assert "location_phone: Mobile phone validation failed" in str(info.value)
    assert isinstance(info.value.__cause__, GoreloAPIError)


def _gorelo_error(prop="Phone", message="bad value", code="070101"):
    return GoreloAPIError(
        "x", status=400, op_key="POST /v1/clients", kind="http", trace_id="00-trace-1",
        notifications=[{"code": code, "message": message, "property": prop}],
    )


def _unconfirmed_write(kind="timeout"):
    """A write Gorelo never confirmed: it may or may not have been applied."""
    return GoreloAPIError(
        f"POST /v1/clients: {kind} and Gorelo did not confirm the write",
        op_key="POST /v1/clients", kind=kind, write_unconfirmed=True,
    )


def _nesting_registry():
    registry = Registry()

    @declare(registry, toolset="core", kind="write", ops=["POST /v1/clients"], field_map={"inner_phone": "Phone"})
    async def inner(ctx: Context, what: str = "gorelo") -> dict:
        """The inner tool."""
        if what == "gorelo":
            raise _gorelo_error()
        if what == "value":
            raise ValueError("inner says no")
        if what == "spec":
            raise SpecViolation("POST /v1/clients", "Phonee", "POST /v1/clients: unknown body field 'Phonee'")
        return {"inner": True, "running": CURRENT_TOOL.get().name}

    @declare(registry, toolset="core", kind="write", ops=["POST /v1/clients"], field_map={"outer_phone": "Phone"})
    async def outer(ctx: Context, what: str = "gorelo") -> dict:
        """The outer tool calls the inner one (which the docs forbid; tools/_common.py still behaves)."""
        return await inner(ctx, what)

    @declare(registry, toolset="core", kind="write", ops=["POST /v1/clients"], field_map={"outer_phone": "Phone"})
    async def outer_catching(ctx: Context, what: str = "gorelo") -> dict:
        """Shows what the inner tool raised."""
        try:
            await inner(ctx, what)
        except Exception as err:  # noqa: BLE001
            return {"raised": type(err).__name__, "same_text": str(err)}
        return {"raised": None}

    return registry


async def test_only_the_outermost_tool_translates_errors():
    registry = _nesting_registry()
    outer, inner = registry.specs[1].fn, registry.specs[0].fn
    # called on its own, the inner tool is the outermost one and translates with its own field map
    with pytest.raises(ToolError) as info:
        await inner(make_ctx())
    assert str(info.value) == "Gorelo rejected inner (HTTP 400, code 070101): inner_phone: bad value [trace 00-trace-1]"
    # called from the outer tool, the inner tool re-raises the ORIGINAL exception and the outer tool translates it
    with pytest.raises(ToolError) as info:
        await outer(make_ctx())
    assert str(info.value) == "Gorelo rejected outer (HTTP 400, code 070101): outer_phone: bad value [trace 00-trace-1]"
    assert type(info.value.__cause__) is GoreloAPIError  # not a ToolError from the inner wrapper
    for what, text in (("value", "inner says no"), ("spec", "POST /v1/clients: unknown body field 'Phonee'")):
        with pytest.raises(ToolError) as info:
            await outer(make_ctx(), what)
        assert str(info.value) == text and type(info.value.__cause__) in (ValueError, SpecViolation)


async def test_the_inner_tool_raises_the_original_exception_untouched():
    registry = _nesting_registry()
    catching = registry.specs[2].fn
    assert (await catching(make_ctx(), "gorelo"))["raised"] == "GoreloAPIError"
    assert (await catching(make_ctx(), "value"))["raised"] == "ValueError"
    assert (await catching(make_ctx(), "spec"))["raised"] == "SpecViolation"
    assert (await catching(make_ctx(), "fine"))["raised"] is None


async def test_the_outer_tool_stays_the_running_tool_for_a_nested_call():
    registry = _nesting_registry()
    outer = registry.specs[1].fn
    assert await outer(make_ctx(), "fine") == {"inner": True, "running": "outer"}


async def test_nested_tool_errors_reach_the_model_once_through_a_real_server():
    server = FastMCP("t")
    for spec in _nesting_registry().specs:
        server.add_tool(spec.fn)
    assert await call_tool_error(server, "outer", {"what": "gorelo"}) == (
        "Gorelo rejected outer (HTTP 400, code 070101): outer_phone: bad value [trace 00-trace-1]"
    )
    assert await call_tool_error(server, "outer", {"what": "value"}) == "inner says no"
    assert await call_tool_error(server, "inner", {"what": "gorelo"}) == (
        "Gorelo rejected inner (HTTP 400, code 070101): inner_phone: bad value [trace 00-trace-1]"
    )


async def test_the_running_tool_is_published_while_it_runs_and_cleared_after():
    registry = Registry()
    seen = {}

    @declare(registry, toolset="core", kind="write", ops=["POST /v1/clients", "GET /v1/clients/{clientId}"])
    async def probe(ctx: Context, fail: bool = False) -> dict:
        """Probe."""
        seen["async"] = CURRENT_TOOL.get()
        if fail:
            raise ValueError("boom")
        return {}

    @declare(registry, toolset="core", kind="read", ops=["GET /v1/clients"])
    def sync_probe(ctx: Context) -> dict:
        """Probe."""
        seen["sync"] = CURRENT_TOOL.get()
        return {}

    assert CURRENT_TOOL.get() is None
    await probe(make_ctx())
    assert CURRENT_TOOL.get() is None
    with pytest.raises(ToolError):
        await probe(make_ctx(), True)
    assert CURRENT_TOOL.get() is None  # also after a failure
    sync_probe(make_ctx())
    assert CURRENT_TOOL.get() is None
    first, second = registry.specs
    assert seen["async"] is first and seen["async"].ops == ["POST /v1/clients", "GET /v1/clients/{clientId}"]
    assert seen["sync"] is second and isinstance(seen["async"], ToolSpec)


async def test_sync_tools_translate_errors_only_at_the_outermost_tool_too():
    registry = Registry()

    @declare(registry, toolset="core", kind="read", ops=["GET /v1/clients"])
    def inner_sync(ctx: Context) -> dict:
        """Inner."""
        raise ValueError("sync inner")

    @declare(registry, toolset="core", kind="read", ops=["GET /v1/clients"])
    def outer_sync(ctx: Context) -> dict:
        """Outer."""
        try:
            return inner_sync(ctx)
        except Exception as err:  # noqa: BLE001
            return {"raised": type(err).__name__}

    assert outer_sync(make_ctx()) == {"raised": "ValueError"}
    with pytest.raises(ToolError, match="sync inner"):
        inner_sync(make_ctx())


def _declared_ops_registry():
    registry = Registry()

    @declare(registry, toolset="core", kind="read", ops=["GET /v1/clients/{clientId}"])
    async def sneaky(ctx: Context, which: str) -> dict:
        """Calls what it declared, or something it did not."""
        client = client_of(ctx)
        if which == "declared":
            return await client.get_one("GET /v1/clients/{clientId}", path_params={"clientId": 1}, tool="sneaky")
        if which == "undeclared":
            return {"items": await client.get_list("GET /v1/organization/groups", tool="sneaky")}
        if which == "write":
            return {"data": await client.post("POST /v1/clients", json_body={"Name": "x"}, tool="sneaky")}
        return {}

    return registry


async def test_a_running_tool_can_only_send_the_operations_it_declared(server_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients/1", envelope({"Id": 1}))
    mock_gorelo.on("GET", "/v1/organization/groups", envelope([{"Id": 7201}]))
    mock_gorelo.on("POST", "/v1/clients", envelope({"Id": 2}))
    server = server_factory(registry=_declared_ops_registry())
    assert await call_tool(server, "sneaky", {"which": "declared"}) == {"Id": 1}
    for which, op in (("undeclared", "GET /v1/organization/groups"), ("write", "POST /v1/clients")):
        text = await call_tool_error(server, "sneaky", {"which": which})
        assert text.startswith(f"sneaky cannot run: {op} is not one of the operations that tool 'sneaky' declares")
        assert "declared: GET /v1/clients/{clientId}" in text
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", "/v1/clients/1")]  # nothing else went out


async def test_outside_a_tool_the_same_client_is_not_restricted(client_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/organization/groups", envelope([{"Id": 7201}]))
    async with client_factory() as client:  # a test or a script: no tool is running
        assert await client.get_list("GET /v1/organization/groups", tool="script") == [{"Id": 7201}]
    # and the same call from inside the tool that did not declare it is refused (the test above)
    assert len(mock_gorelo.requests) == 1


async def test_a_nested_call_is_checked_against_the_outer_tools_declaration(server_factory, mock_gorelo):
    registry = Registry()

    @declare(registry, toolset="core", kind="read", ops=["GET /v1/organization/groups"])
    async def inner_reader(ctx: Context) -> dict:
        """Declares and reads groups."""
        return {"items": await client_of(ctx).get_list("GET /v1/organization/groups", tool="inner_reader")}

    @declare(registry, toolset="core", kind="read", ops=["GET /v1/clients/{clientId}"])
    async def outer_reader(ctx: Context) -> dict:
        """Declares only clients, yet calls the groups reader."""
        return await inner_reader(ctx)

    mock_gorelo.on("GET", "/v1/organization/groups", envelope([{"Id": 7201}]))
    server = server_factory(registry=registry)
    assert await call_tool(server, "inner_reader") == {"items": [{"Id": 7201}]}
    text = await call_tool_error(server, "outer_reader")
    assert text.startswith("outer_reader cannot run: GET /v1/organization/groups is not one of the operations that tool 'outer_reader' declares")
    assert len(mock_gorelo.requests) == 1  # only the first call reached Gorelo


async def test_two_concurrent_tools_in_one_session_each_run_as_themselves(server_factory, mock_gorelo):
    # ONE in-process session, two tools with disjoint ops in flight at the same time. The first is
    # suspended on an Event that only the second sets, so the second starts while the first is running.
    # CURRENT_TOOL must not leak between them: each sees its own name and is checked against its OWN ops.
    mock_gorelo.on("GET", "/v1/clients/1", envelope({"Id": 1}))
    mock_gorelo.on("GET", "/v1/organization/groups", envelope([{"Id": 7201}]))
    registry = Registry()
    first_started, release_first = asyncio.Event(), asyncio.Event()
    seen = {}

    @declare(registry, toolset="core", kind="read", ops=["GET /v1/clients/{clientId}"])
    async def first(ctx: Context) -> dict:
        """Waits until the second tool has started, then reads a client."""
        seen["first_before"] = CURRENT_TOOL.get().name
        first_started.set()
        await asyncio.wait_for(release_first.wait(), timeout=5)  # only the second tool sets it
        seen["first_after"] = CURRENT_TOOL.get().name
        return {"client": await client_of(ctx).get_one("GET /v1/clients/{clientId}", path_params={"clientId": 1}, tool="first")}

    @declare(registry, toolset="core", kind="read", ops=["GET /v1/organization/groups"])
    async def second(ctx: Context) -> dict:
        """Starts while the first is running, releases it, then lists groups."""
        await asyncio.wait_for(first_started.wait(), timeout=5)
        seen["second_before"] = CURRENT_TOOL.get().name
        release_first.set()
        client = client_of(ctx)
        # the first tool is still running, yet its operation is not the second tool's: refused, no HTTP call
        # (recorded instead of asserted here, so a regression shows up as a readable diff of `seen` below)
        try:
            await client.get_one("GET /v1/clients/{clientId}", path_params={"clientId": 1}, tool="second")
        except GoreloAPIError as refused:
            seen["second_vs_the_first_tools_op"] = (refused.kind, "tool 'second' declares" in str(refused))
        else:
            seen["second_vs_the_first_tools_op"] = "not refused"
        items = await client.get_list("GET /v1/organization/groups", tool="second")
        seen["second_after"] = CURRENT_TOOL.get().name
        return {"items": items}

    server = server_factory(registry=registry)
    async with Client(server) as session:
        results = await asyncio.wait_for(
            asyncio.gather(call_tool(session, "first"), call_tool(session, "second")), timeout=15
        )
    # both succeeded: neither was refused by the declared-ops check for its own operation
    assert results == [{"client": {"Id": 1}}, {"items": [{"Id": 7201}]}]
    assert seen == {
        "first_before": "first",
        "first_after": "first",
        "second_before": "second",
        "second_after": "second",
        "second_vs_the_first_tools_op": ("spec", True),  # refused by the declared-ops check, with no HTTP call
    }
    assert sorted((r.method, r.path) for r in mock_gorelo.requests) == [
        ("GET", "/v1/clients/1"),
        ("GET", "/v1/organization/groups"),
    ]
    assert CURRENT_TOOL.get() is None  # nothing leaked into the caller's context either


# --------------------------------------------------------------------------
# Exception groups (asyncio.TaskGroup)
# --------------------------------------------------------------------------


def _group_registry():
    registry = Registry()

    async def fail_together(errors):
        gate = asyncio.Barrier(len(errors))

        async def fail(error):
            await gate.wait()
            raise error

        async with asyncio.TaskGroup() as group:
            for error in errors:
                group.create_task(fail(error))

    @declare(registry, toolset="core", kind="write", ops=["POST /v1/clients"], field_map={"location_phone": "Location.Phone"})
    async def fan_out(ctx: Context, mode: str) -> dict:
        """Runs several concurrent calls that fail."""
        gorelo = _gorelo_error("Location.Phone", "Mobile phone validation failed")
        scenarios = {
            "two": [gorelo, ValueError("second problem")],
            "one": [gorelo],
            "three": [ValueError("first problem"), gorelo, SpecViolation("POST /v1/clients", "X", "POST /v1/clients: unknown body field 'X'")],
            "bugs-only": [KeyError("a"), RuntimeError("b")],
            "bug-and-value": [KeyError("a"), ValueError("the real problem")],
            "value-then-unconfirmed": [ValueError("local problem"), _unconfirmed_write()],
            "unconfirmed-then-value": [_unconfirmed_write(), ValueError("local problem")],
        }
        await fail_together(scenarios[mode])
        return {}

    @declare(registry, toolset="core", kind="write", ops=["POST /v1/clients"])
    async def nested_groups(ctx: Context) -> dict:
        """A group inside a group."""

        async def inner_group():
            await fail_together([ValueError("deep problem"), ValueError("deep problem 2")])

        async with asyncio.TaskGroup() as outer_group:
            outer_group.create_task(inner_group())
        return {}

    @declare(registry, toolset="core", kind="write", ops=["POST /v1/clients"])
    async def cancelled_with_a_problem(ctx: Context) -> dict:
        """A group that also holds a cancellation."""
        raise BaseExceptionGroup("mixed", [asyncio.CancelledError(), ValueError("hidden")])

    @declare(registry, toolset="core", kind="write", ops=["POST /v1/clients"])
    async def outer_of_a_group(ctx: Context) -> dict:
        """Calls a tool whose function raises a group."""
        return await fan_out(ctx, "two")

    return registry


def _first_leaf_text(group, tool, field_map):
    leaf = group.exceptions[0]
    return format_gorelo_error(leaf, tool, field_map) if isinstance(leaf, GoreloAPIError) else str(leaf)


async def test_an_exception_group_is_translated_to_its_first_leaf_and_counts_the_others():
    registry = _group_registry()
    fan_out = registry.specs[0].fn
    field_map = {"location_phone": "Location.Phone"}
    with pytest.raises(ToolError) as info:
        await fan_out(make_ctx(), "two")
    group = info.value.__cause__
    assert isinstance(group, ExceptionGroup) and len(group.exceptions) == 2
    expected = _first_leaf_text(group, "fan_out", field_map)
    assert str(info.value) == f"{expected} (1 other error was raised at the same time; fix this one and call again)"
    assert "Mobile phone validation failed" in str(info.value) or "second problem" in str(info.value)

    with pytest.raises(ToolError) as info:
        await fan_out(make_ctx(), "three")
    group = info.value.__cause__
    expected = _first_leaf_text(group, "fan_out", field_map)
    assert str(info.value) == f"{expected} (2 other errors were raised at the same time; fix this one and call again)"

    with pytest.raises(ToolError) as info:
        await fan_out(make_ctx(), "one")  # a group of one: nothing to count
    assert str(info.value) == (
        "Gorelo rejected fan_out (HTTP 400, code 070101): location_phone: Mobile phone validation failed [trace 00-trace-1]"
    )


async def test_a_group_with_a_real_problem_among_bugs_is_translated_and_counts_the_bugs():
    fan_out = _group_registry().specs[0].fn
    with pytest.raises(ToolError) as info:
        await fan_out(make_ctx(), "bug-and-value")
    assert str(info.value) == "the real problem (1 other error was raised at the same time; fix this one and call again)"


async def test_a_group_of_nothing_but_bugs_propagates_untouched():
    fan_out = _group_registry().specs[0].fn
    with pytest.raises(ExceptionGroup) as info:
        await fan_out(make_ctx(), "bugs-only")
    assert {type(e) for e in info.value.exceptions} == {KeyError, RuntimeError}


async def test_nested_exception_groups_are_flattened():
    nested = _group_registry().specs[1].fn
    with pytest.raises(ToolError) as info:
        await nested(make_ctx())
    assert str(info.value) in (
        "deep problem (1 other error was raised at the same time; fix this one and call again)",
        "deep problem 2 (1 other error was raised at the same time; fix this one and call again)",
    )


async def test_a_group_holding_a_cancellation_is_never_swallowed():
    cancelled = _group_registry().specs[2].fn
    with pytest.raises(BaseExceptionGroup) as info:
        await cancelled(make_ctx())
    assert any(isinstance(e, asyncio.CancelledError) for e in info.value.exceptions)


async def test_a_group_raised_inside_a_nested_tool_is_translated_once_by_the_outer_tool():
    registry = _group_registry()
    outer = registry.specs[3].fn
    with pytest.raises(ToolError) as info:
        await outer(make_ctx())
    assert str(info.value).startswith("Gorelo rejected outer_of_a_group") or "second problem" in str(info.value)
    assert isinstance(info.value.__cause__, ExceptionGroup)  # the inner tool did not translate it


async def test_a_group_reaches_the_model_as_one_tool_error_through_a_real_server():
    server = FastMCP("t")
    for spec in _group_registry().specs:
        server.add_tool(spec.fn)
    text = await call_tool_error(server, "fan_out", {"mode": "two"})
    assert "(1 other error was raised at the same time" in text and "unhandled errors" not in text


# An unconfirmed write inside a group : the leaf the model must not lose is shown, and the advice
# is to verify with a read, never "fix this one and call again" (that would repeat a write that may have applied).

VERIFY_ADVICE = "at least one write may have been applied, so verify with a read before calling again"
FIX_ADVICE = "fix this one and call again"
GROUP_FIELD_MAP = {"location_phone": "Location.Phone"}


def _ordered_group_registry():
    """One tool that raises an ExceptionGroup whose leaves come in a FIXED order (no TaskGroup scheduling)."""
    registry = Registry()

    @declare(registry, toolset="core", kind="write", ops=["POST /v1/clients"], field_map=GROUP_FIELD_MAP)
    async def raise_group(ctx: Context, mode: str) -> dict:
        """Raises an ExceptionGroup with its leaves in a fixed order."""
        confirmed = _gorelo_error("Location.Phone", "Mobile phone validation failed")
        value = ValueError("local problem")
        scenarios = {
            "unconfirmed-first": [_unconfirmed_write(), value],
            "unconfirmed-last": [value, _unconfirmed_write()],
            "confirmed-then-unconfirmed": [confirmed, _unconfirmed_write("transport")],
            "unconfirmed-then-confirmed": [_unconfirmed_write("transport"), confirmed],
            "two-unconfirmed": [_unconfirmed_write("timeout"), _unconfirmed_write("transport")],
            "unconfirmed-among-bugs": [KeyError("a"), _unconfirmed_write(), RuntimeError("b")],
            "nested-last": [value, ExceptionGroup("inner", [ValueError("deep"), _unconfirmed_write()])],
            "nothing-unconfirmed": [value, confirmed],
        }
        raise ExceptionGroup("boom", scenarios[mode])

    return registry


@pytest.mark.parametrize("mode", ["unconfirmed-first", "unconfirmed-last"])
async def test_an_unconfirmed_write_is_the_leaf_that_is_shown_whatever_its_position(mode):
    raise_group = _ordered_group_registry().specs[0].fn
    shown = format_gorelo_error(_unconfirmed_write(), "raise_group", GROUP_FIELD_MAP)
    assert shown.startswith("Gorelo did not confirm raise_group") and "Verify with a read before retrying" in shown
    with pytest.raises(ToolError) as info:
        await raise_group(make_ctx(), mode)
    assert str(info.value) == f"{shown} (1 other error was raised at the same time; {VERIFY_ADVICE})"
    assert "local problem" not in str(info.value) and FIX_ADVICE not in str(info.value)
    assert isinstance(info.value.__cause__, ExceptionGroup)


@pytest.mark.parametrize("mode", ["confirmed-then-unconfirmed", "unconfirmed-then-confirmed"])
async def test_an_unconfirmed_write_wins_over_a_gorelo_error_that_was_refused_in_either_order(mode):
    raise_group = _ordered_group_registry().specs[0].fn
    with pytest.raises(ToolError) as info:
        await raise_group(make_ctx(), mode)
    text = str(info.value)
    assert text.startswith("Gorelo did not confirm raise_group (the connection failed)")
    assert text.endswith(f"(1 other error was raised at the same time; {VERIFY_ADVICE})")
    assert "Mobile phone validation failed" not in text and FIX_ADVICE not in text


async def test_with_two_unconfirmed_writes_the_first_is_shown_and_the_other_is_counted():
    raise_group = _ordered_group_registry().specs[0].fn
    with pytest.raises(ToolError) as info:
        await raise_group(make_ctx(), "two-unconfirmed")
    text = str(info.value)
    assert text.startswith("Gorelo did not confirm raise_group (the request timed out)")
    assert text.endswith(f"(1 other error was raised at the same time; {VERIFY_ADVICE})")


@pytest.mark.parametrize("mode", ["unconfirmed-among-bugs", "nested-last"])
async def test_an_unconfirmed_write_is_found_among_bugs_and_inside_a_nested_group(mode):
    raise_group = _ordered_group_registry().specs[0].fn
    with pytest.raises(ToolError) as info:
        await raise_group(make_ctx(), mode)
    text = str(info.value)
    assert text.startswith("Gorelo did not confirm raise_group (the request timed out)")
    assert text.endswith(f"(2 other errors were raised at the same time; {VERIFY_ADVICE})")  # the others are counted


async def test_without_an_unconfirmed_write_the_group_keeps_the_fix_and_call_again_advice():
    raise_group = _ordered_group_registry().specs[0].fn
    with pytest.raises(ToolError) as info:
        await raise_group(make_ctx(), "nothing-unconfirmed")
    assert str(info.value) == f"local problem (1 other error was raised at the same time; {FIX_ADVICE})"
    assert "verify with a read" not in str(info.value)


@pytest.mark.parametrize("mode", ["value-then-unconfirmed", "unconfirmed-then-value"])
async def test_a_real_taskgroup_translates_the_unconfirmed_write_whichever_task_failed_first(mode):
    fan_out = _group_registry().specs[0].fn
    shown = format_gorelo_error(_unconfirmed_write(), "fan_out", GROUP_FIELD_MAP)
    with pytest.raises(ToolError) as info:
        await fan_out(make_ctx(), mode)
    assert str(info.value) == f"{shown} (1 other error was raised at the same time; {VERIFY_ADVICE})"


async def test_the_verify_advice_reaches_the_model_through_a_real_server():
    server = FastMCP("t")
    for spec in _ordered_group_registry().specs:
        server.add_tool(spec.fn)
    text = await call_tool_error(server, "raise_group", {"mode": "unconfirmed-last"})
    assert VERIFY_ADVICE in text and "Verify with a read before retrying" in text and FIX_ADVICE not in text


# --------------------------------------------------------------------------
# client_of, server_info_of
# --------------------------------------------------------------------------


def test_client_of_reads_the_lifespan_context():
    sentinel = object()
    assert client_of(make_ctx(sentinel)) is sentinel  # type: ignore[arg-type]


def test_client_of_fails_clearly_when_the_client_is_missing():
    for ctx in (make_ctx(), make_ctx(other=1), object()):
        with pytest.raises(RuntimeError, match="Gorelo client is not available"):
            client_of(ctx)  # type: ignore[arg-type]


def test_server_info_of():
    class Spec:
        sha256 = "abc"

    info = server_info_of(make_ctx(toolsets=["core"], destructive=True, spec=Spec()))
    assert info == {"toolsets": ["core"], "destructive": True, "spec_sha256": "abc"}
    assert server_info_of(make_ctx()) == {"toolsets": None, "destructive": None, "spec_sha256": None}


# --------------------------------------------------------------------------
# reread_after_write
# --------------------------------------------------------------------------


def _write_tool_registry(read_op="GET /v1/clients/{clientId}", read_params=None):
    registry = Registry()
    params = read_params or (lambda written: {"clientId": written})

    @declare(
        registry, toolset="core", kind="write", ops=["POST /v1/clients", "GET /v1/clients/{clientId}"],
        field_map={"name": "Name", "location_phone": "Location.Phone"},
    )
    async def create_it(ctx: Context, name: Annotated[str, Field(description="Name.")]) -> dict:
        """Create a client, then read it back."""
        written = await client_of(ctx).post(
            "POST /v1/clients", json_body={"Name": name, "Location": {"Name": "HQ"}}, tool="create_it"
        )
        return await reread_after_write(
            ctx, read_op, path_params=params(written["Id"]), tool="create_it", written_id=written["Id"]
        )

    return registry


async def test_reread_after_write_returns_the_record(server_factory, mock_gorelo):
    mock_gorelo.on("POST", "/v1/clients", envelope({"Id": 7}))
    mock_gorelo.on("GET", "/v1/clients/7", envelope({"Id": 7, "Name": "Acme", "Status": "Active"}))
    server = server_factory(registry=_write_tool_registry())
    assert await call_tool(server, "create_it", {"name": "Acme"}) == {"Id": 7, "Name": "Acme", "Status": "Active"}
    assert [r.method for r in mock_gorelo.requests] == ["POST", "GET"]


REREAD = "GET /v1/clients/{clientId}"
SAYS_RETRY_IS_SAFE = ("retrying is safe", "safe to retry", "This was a read")  # format_gorelo_error's wording for a read


def _assert_the_warning_describes_the_read(warning, write_tool="create_it"):
    """The warning is about the failed RE-READ: the write tool is never named, never called "rejected" or
    "did not answer", and a retry is never called safe (the write already happened: it must not be repeated)."""
    for forbidden in (*SAYS_RETRY_IS_SAFE, f"rejected {write_tool}", "Gorelo rejected", "Gorelo did not", write_tool):
        assert forbidden not in warning, f"{forbidden!r} must not appear in: {warning}"


@pytest.mark.parametrize(
    "response, reason",
    [
        pytest.param(httpx.ReadTimeout("slow"), f"{REREAD} timed out", id="timeout"),
        pytest.param(error_envelope(404, [("070401", "Client not found")], trace_id="00-t-1"),
                     f"{REREAD} answered HTTP 404 (code 070401): Client not found [trace 00-t-1]", id="404"),
        pytest.param(httpx.Response(502, text="<html>Bad Gateway</html>"), f"{REREAD}: unexpected response shape; refusing to guess (HTTP 502", id="gateway-page"),
        pytest.param(envelope(None), f"{REREAD}: Gorelo reported success but Data is null; refusing to guess", id="data-null"),
        pytest.param(envelope([{"Id": 7}]), f"{REREAD} returned a list instead of the record", id="data-is-a-list"),
        pytest.param(envelope(True), f"{REREAD} returned a boolean instead of the record", id="data-is-a-boolean"),
    ],
)
async def test_a_failed_reread_after_a_successful_write_returns_the_warning_and_never_repeats_the_write(
    server_factory, mock_gorelo, response, reason
):
    mock_gorelo.on("POST", "/v1/clients", envelope({"Id": 7}))
    mock_gorelo.on("GET", "/v1/clients/7", response)
    server = server_factory(registry=_write_tool_registry())
    result = await call_tool(server, "create_it", {"name": "Acme"})
    assert set(result) == {"Id", "warning"} and result["Id"] == 7
    warning = result["warning"]
    assert warning.startswith(f"the write succeeded; re-reading it failed: {reason}") and reason in warning
    assert warning.endswith(". Do not repeat the write; read it again later.") and ".." not in warning
    _assert_the_warning_describes_the_read(warning)
    assert len(mock_gorelo.calls("POST")) == 1  # the write happened exactly once
    assert len(mock_gorelo.calls("GET")) == 1  # and the read was not retried either


@pytest.mark.parametrize(
    "response, reason",
    [
        pytest.param(httpx.ReadTimeout("slow"), f"{REREAD} timed out", id="timeout"),
        pytest.param(httpx.ConnectError("refused"), f"the connection to Gorelo failed during {REREAD}", id="connection-failed"),
        pytest.param(error_envelope(404, [("070401", "Client not found")], trace_id="00-t-1"),
                     f"{REREAD} answered HTTP 404 (code 070401): Client not found [trace 00-t-1]", id="404"),
        pytest.param(error_envelope(500, [("070001", "Internal error")], trace_id="00-t-2"),
                     f"{REREAD} answered HTTP 500 (code 070001): Internal error [trace 00-t-2]", id="500"),
        pytest.param(error_envelope(503, [("070001", "Busy"), ("070002", "Try later", "Client")], trace_id="00-t-3"),
                     f"{REREAD} answered HTTP 503 (code 070001/070002): Busy; Client: Try later [trace 00-t-3]", id="503-two-notifications"),
        pytest.param(error_envelope(500, [], trace_id=None), f"{REREAD} answered HTTP 500: no details returned", id="500-no-details"),
        pytest.param(error_envelope(403, [("080203", "API key does not have 'Project' scope")], trace_id="00-t-4"),
                     f"{REREAD} answered HTTP 403 (code 080203): API key does not have 'Project' scope [trace 00-t-4]", id="403-scope"),
        pytest.param(httpx.Response(429, headers={"Retry-After": "100000"}, json={"error": "slow"}),
                     f"{REREAD} was rate limited by Gorelo (HTTP 429)", id="429"),
    ],
)
async def test_the_warning_of_a_failed_reread_describes_the_read_never_the_write_tool(
    server_factory, mock_gorelo, response, reason
):
    # for a timeout, a 404 and a 5xx (and the other ways a read fails) the warning is the exact text
    # below. It never says "Gorelo rejected create_it", never says retrying is safe, never names the tool.
    mock_gorelo.on("POST", "/v1/clients", envelope({"Id": 7}))
    mock_gorelo.on("GET", "/v1/clients/7", response)
    server = server_factory(registry=_write_tool_registry())
    result = await call_tool(server, "create_it", {"name": "Acme"})
    assert result == {
        "Id": 7,
        "warning": f"the write succeeded; re-reading it failed: {reason}. Do not repeat the write; read it again later.",
    }
    _assert_the_warning_describes_the_read(result["warning"])
    assert len(mock_gorelo.calls("POST")) == 1 and len(mock_gorelo.calls("GET")) == 1


class _FailingReader:
    """A stand-in client whose GET fails with the given error (no HTTP at all)."""

    def __init__(self, error):
        self.error = error

    async def get_one(self, op_key, *, path_params=None, query=None, tool):
        raise self.error


def _every_way_a_reread_can_fail():
    """(id, error, the reason the warning must carry) for every kind of GoreloAPIError a read can raise,
    including hand-built ones that carry a write_unconfirmed flag the re-read must ignore."""
    notes = [{"code": "070401", "message": "Client not found", "property": None}]
    shape = f"{REREAD}: unexpected response shape; refusing to guess (HTTP 502, a body that is not JSON, content-type text/html, 20 bytes)"
    connection = f"the connection to Gorelo failed during {REREAD}"
    cases = [
        ("timeout", GoreloAPIError("x", op_key=REREAD, kind="timeout"), f"{REREAD} timed out"),
        ("timeout-flagged-unconfirmed", GoreloAPIError("x", op_key=REREAD, kind="timeout", write_unconfirmed=True), f"{REREAD} timed out"),
        ("transport", GoreloAPIError("x", op_key=REREAD, kind="transport"), connection),
        ("transport-flagged-unconfirmed", GoreloAPIError("x", op_key=REREAD, kind="transport", write_unconfirmed=True), connection),
        ("http-404", GoreloAPIError("x", status=404, op_key=REREAD, kind="http", notifications=notes, trace_id="t1"),
         f"{REREAD} answered HTTP 404 (code 070401): Client not found [trace t1]"),
        ("http-500-flagged-unconfirmed", GoreloAPIError("x", status=500, op_key=REREAD, kind="http", notifications=notes, write_unconfirmed=True),
         f"{REREAD} answered HTTP 500 (code 070401): Client not found"),
        ("http-without-status-or-op-key", GoreloAPIError("x", kind="http"), f"{REREAD} failed: no details returned"),
        ("envelope", GoreloAPIError("x", status=200, op_key=REREAD, kind="envelope", notifications=notes, trace_id="t2"),
         f"{REREAD} answered HTTP 200 (code 070401): Client not found [trace t2]"),
        ("shape", GoreloAPIError(shape, status=502, op_key=REREAD, kind="shape", trace_id="t3"), f"{shape} [trace t3]"),
        ("shape-flagged-unconfirmed", GoreloAPIError(shape, status=502, op_key=REREAD, kind="shape", write_unconfirmed=True), shape),
        ("rate-limit", GoreloAPIError("x", status=429, op_key=REREAD, kind="rate_limit", trace_id="t4"),
         f"{REREAD} was rate limited by Gorelo (HTTP 429) [trace t4]"),
        ("forbidden", GoreloAPIError(f"{REREAD} is deliberately not available through this server", op_key=REREAD, kind="forbidden"),
         f"{REREAD} is deliberately not available through this server"),
        ("spec", GoreloAPIError(f"{REREAD} is a paged operation; use get_page or get_all", op_key=REREAD, kind="spec"),
         f"{REREAD} is a paged operation; use get_page or get_all"),
    ]
    return [pytest.param(error, reason, id=name) for name, error, reason in cases]


@pytest.mark.parametrize("error, reason", _every_way_a_reread_can_fail())
async def test_no_kind_of_failed_reread_can_blame_the_write_tool_or_call_a_retry_safe(error, reason):
    ctx = make_ctx(_FailingReader(error))
    result = await reread_after_write(ctx, REREAD, path_params={"clientId": 7}, tool="create_it", written_id=7)
    assert result == {
        "Id": 7,
        "warning": f"the write succeeded; re-reading it failed: {reason}. Do not repeat the write; read it again later.",
    }
    warning = result["warning"]
    _assert_the_warning_describes_the_read(warning)
    assert REREAD in warning  # the neutral label: the operation that was read
    assert "Verify with a read before retrying" not in warning  # that sentence is about a write, and this was a read
    assert "may or may not have been applied" not in warning  # nothing is unclear about the write: it succeeded


async def test_a_reread_that_the_tool_did_not_declare_is_a_warning_not_a_failed_write(server_factory, mock_gorelo):
    # a programming error must not make the model think the write failed (it would write again)
    mock_gorelo.on("POST", "/v1/clients", envelope({"Id": 7}))
    mock_gorelo.on("GET", "/v1/organization/groups", envelope([]))
    server = server_factory(registry=_write_tool_registry(read_op="GET /v1/organization/groups", read_params=lambda written: {}))
    result = await call_tool(server, "create_it", {"name": "Acme"})
    warning = result["warning"]
    assert result["Id"] == 7
    # the declaration is the developer's to fix, so the message says whose it is; it is not "create_it cannot run"
    assert "GET /v1/organization/groups is not one of the operations that tool 'create_it' declares in ops=[...]" in warning
    assert "cannot run" not in warning and "rejected" not in warning
    assert [r.method for r in mock_gorelo.requests] == ["POST"]


async def test_a_malformed_id_in_the_reread_is_a_warning_too(server_factory, mock_gorelo):
    mock_gorelo.on("POST", "/v1/clients", envelope({"Id": "not/a-uuid"}))
    registry = Registry()

    @declare(registry, toolset="core", kind="write", ops=["POST /v1/clients", "GET /v1/tickets/{ticketId}"])
    async def make_ticket(ctx: Context) -> dict:
        """Create, then read a ticket."""
        written = await client_of(ctx).post("POST /v1/clients", json_body={"Name": "x"}, tool="make_ticket")
        return await reread_after_write(
            ctx, "GET /v1/tickets/{ticketId}", path_params={"ticketId": written["Id"]}, tool="make_ticket", written_id=written["Id"]
        )

    result = await call_tool(server_factory(registry=registry), "make_ticket")
    assert result["Id"] == "not/a-uuid" and "'ticketId' must be a UUID" in result["warning"]
    assert [r.method for r in mock_gorelo.requests] == ["POST"]


async def test_reread_after_write_works_outside_a_tool_and_with_a_uuid_op(client_factory, mock_gorelo):
    mock_gorelo.on("GET", f"/v1/tickets/{uid(3)}", envelope({"Id": uid(3), "Title": "T"}))
    async with client_factory() as client:
        ctx = make_ctx(client)
        record = await reread_after_write(
            ctx, "GET /v1/tickets/{ticketId}", path_params={"ticketId": uid(3)}, tool="create_ticket", written_id=uid(3)
        )
    assert record == {"Id": uid(3), "Title": "T"}


async def test_reread_after_write_needs_a_client():
    with pytest.raises(RuntimeError, match="Gorelo client is not available"):
        await reread_after_write(make_ctx(), "GET /v1/clients/{clientId}", path_params={"clientId": 1}, tool="t", written_id=1)


# --------------------------------------------------------------------------
# Parameter helpers
# --------------------------------------------------------------------------


def test_require_confirm_passes_only_for_true():
    assert require_confirm(True, action="delete item 5") is None
    for value in (False, None, 1, "true", "yes", [True]):
        with pytest.raises(ValueError) as info:
            require_confirm(value, action="delete item 5")  # type: ignore[arg-type]
        message = str(info.value)
        assert message.startswith("confirm: refusing to delete item 5 without confirm=true.")
        assert "Call again with confirm=true" in message


def test_require_confirm_can_say_what_will_happen():
    with pytest.raises(ValueError) as info:
        require_confirm(False, action="void invoice 12", effect="The invoice becomes Void and cannot be reopened.")
    assert "The invoice becomes Void and cannot be reopened." in str(info.value)


def test_csv_ids():
    assert csv_ids("status_ids", None) is None
    assert csv_ids("status_ids", [1, 2, 3]) == "1,2,3"
    assert csv_ids("skus", ["A-1", "B 2", " c "]) == "A-1,B 2,c"
    assert csv_ids("ids", (7,)) == "7"
    assert csv_ids("ids", [1, "2"]) == "1,2"


@pytest.mark.parametrize(
    "value, fragment",
    [
        ([], "at least one id"),
        (5, "expected a list"),
        ("1,2", "expected a list"),
        ([True], "integers or strings"),
        ([1.5], "integers or strings"),
        ([None], "integers or strings"),
        ([""], "must not be blank"),
        (["   "], "must not be blank"),
        (["a,b"], "comma"),
    ],
)
def test_csv_ids_errors_name_the_param(value, fragment):
    with pytest.raises(ValueError) as info:
        csv_ids("status_ids", value)
    assert str(info.value).startswith("status_ids:") and fragment in str(info.value)


@pytest.mark.parametrize(
    "given, expected",
    [
        ("2026-10-01T14:30:00Z", "2026-10-01T14:30:00Z"),
        ("2026-10-01T09:30:00-05:00", "2026-10-01T14:30:00Z"),
        ("2026-10-01T16:30:00+02:00", "2026-10-01T14:30:00Z"),
        ("2026-10-01T14:30:00+00:00", "2026-10-01T14:30:00Z"),
        ("2026-10-01T14:30:00.123456+02:00", "2026-10-01T12:30:00.123456Z"),
        ("2026-10-01 14:30:00Z", "2026-10-01T14:30:00Z"),
        ("2026-12-31T23:30:00-05:00", "2027-01-01T04:30:00Z"),
        (datetime(2026, 10, 1, 9, 30, tzinfo=timezone(timedelta(hours=-5))), "2026-10-01T14:30:00Z"),
    ],
)
def test_utc_iso_converts_to_utc_z(given, expected):
    assert utc_iso("started_on", given) == expected


def test_utc_iso_none_stays_none():
    assert utc_iso("started_on", None) is None


@pytest.mark.parametrize(
    "bad", ["2026-10-01T14:30:00", "2026-10-01", "2026-10-01T14:30", datetime(2026, 10, 1, 14, 30), "not a date", "", "   ", "10/01/2026 2pm"]
)
def test_utc_iso_rejects_naive_and_malformed_values_naming_the_param(bad):
    with pytest.raises(ValueError) as info:
        utc_iso("started_on", bad)
    message = str(info.value)
    assert message.startswith("started_on:") and ("UTC offset" in message or "ISO 8601" in message)


@pytest.mark.parametrize("value", ["US", "CA", "GB", "DE"])
def test_region_code_accepts_two_capital_letters(value):
    assert region_code("mobile_phone_country_code", value) == value
    assert region_code("mobile_phone_country_code", None) is None


@pytest.mark.parametrize("value", ["1", "+1", "us", "Us", "USA", "U", "", " US", "US ", "U1", 1])
def test_region_code_rejects_everything_else_and_explains_dial_codes(value):
    with pytest.raises(ValueError) as info:
        region_code("mobile_phone_country_code", value)
    message = str(info.value)
    assert message.startswith("mobile_phone_country_code:")
    assert "dial codes like 1 or +1 are not accepted" in message and "use e.g. US or CA" in message


def test_non_empty():
    assert non_empty("title", "x") == "x"
    assert non_empty("title", " x ") == " x "
    assert non_empty("ids", [1]) == [1]
    assert non_empty("title", None) is None
    assert non_empty("count", 0) == 0 and non_empty("flag", False) is False and non_empty("ids", [0]) == [0]


@pytest.mark.parametrize("value", ["", "   ", "\n\t", [], (), set(), {}])
def test_non_empty_rejects_blank_and_empty_values(value):
    with pytest.raises(ValueError) as info:
        non_empty("title", value)
    assert str(info.value).startswith("title:")


@pytest.mark.parametrize("given, expected", [(0, 1), (-9, 1), (1, 1), (37, 37), (200, 200), (201, 200), (10_000, 200), ("12", 12), (7.9, 7)])
def test_clamp_page_size(given, expected):
    assert clamp_page_size(given) == expected


def test_clamp_page_size_rejects_non_numbers():
    with pytest.raises(ValueError, match="page_size"):
        clamp_page_size("lots")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# describe_value
# --------------------------------------------------------------------------

SECRET = "s3cr3t-token-do-not-echo"


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "null"),
        (True, "a boolean"),
        (False, "a boolean"),
        (0, "a number"),
        (7, "a number"),
        (2**70, "a number"),
        (1.5, "a number"),
        (float("nan"), "a number"),
        ("", "an empty string"),
        ("x", "a string"),
        (SECRET, "a string"),
        ("   ", "a string"),
        ({}, "an empty object"),
        ({"a": 1}, "an object"),
        ({SECRET: SECRET}, "an object"),
        ([], "an empty list"),
        ((), "an empty list"),
        ([1], "a list of 1 item"),
        ([1, 2, 3], "a list of 3 items"),
        ((SECRET, SECRET), "a list of 2 items"),
        (b"abc", "binary data"),
        (bytearray(b"x"), "binary data"),
        (object(), "a value of type object"),
        ({1, 2}, "a value of type set"),
    ],
)
def test_describe_value_says_what_a_value_is_without_quoting_it(value, expected):
    described = describe_value(value)
    assert described == expected
    assert SECRET not in described


# --------------------------------------------------------------------------
# positive_id, positive_ids, guid, guids
# --------------------------------------------------------------------------


class _Level(enum.IntEnum):
    LOW = 1


@pytest.mark.parametrize("value", [1, 2, 123, 9103, 2**31, MAX_ID])
def test_positive_id_returns_a_valid_id_as_an_int(value):
    assert positive_id("client_id", value) == value
    assert type(positive_id("client_id", value)) is int


def test_positive_id_hands_back_a_plain_int_for_an_int_subclass():
    assert type(positive_id("level_id", _Level.LOW)) is int and positive_id("level_id", _Level.LOW) == 1


def test_the_largest_id_is_the_int64_maximum():
    assert MAX_ID == 2**63 - 1 == 9223372036854775807


@pytest.mark.parametrize(
    "value, what",
    [
        (True, "got a boolean"),
        (False, "got a boolean"),
        (None, "got null"),
        ("5", "got a string"),
        ("", "got an empty string"),
        (SECRET, "got a string"),
        (5.0, "got a decimal number"),
        (0.5, "got a decimal number"),
        ([5], "got a list of 1 item"),
        ([], "got an empty list"),
        ({"id": 5}, "got an object"),
        (0, "got zero or a negative number"),
        (-1, "got zero or a negative number"),
        (-(2**70), "got zero or a negative number"),
        (MAX_ID + 1, f"got a number above {MAX_ID}, the largest Gorelo id"),
        (2**70, f"got a number above {MAX_ID}, the largest Gorelo id"),
    ],
)
def test_positive_id_rejects_everything_else_naming_the_param_and_never_the_value(value, what):
    with pytest.raises(ValueError) as info:
        positive_id("client_id", value)
    message = str(info.value)
    assert message == f"client_id: expected a positive whole number such as 123, {what}"
    assert SECRET not in message


def test_positive_id_does_not_pass_none_through():
    # optional parameters are the caller's business: `if value is not None` first
    with pytest.raises(ValueError, match="client_id: expected a positive whole number"):
        positive_id("client_id", None)


def test_positive_ids():
    assert positive_ids("status_ids", None) is None
    assert positive_ids("status_ids", [3, 1, 2, 1]) == [3, 1, 2, 1]  # order and duplicates kept
    assert positive_ids("status_ids", (7, MAX_ID)) == [7, MAX_ID]
    source = [1, 2]
    result = positive_ids("status_ids", source)
    assert result == source and result is not source  # a new list
    assert all(type(item) is int for item in positive_ids("status_ids", [_Level.LOW]))


EMPTY_IDS = "status_ids: expected at least one id, got an empty list (omit status_ids if you have no ids to give)"
NOT_A_LIST = "status_ids: expected a list of ids such as [123, 456], got "
BAD_ITEM = "expected a positive whole number such as 123, "


@pytest.mark.parametrize(
    "values, message",
    [
        ([], EMPTY_IDS),
        ((), EMPTY_IDS),
        (5, NOT_A_LIST + "a number"),
        ("1,2", NOT_A_LIST + "a string"),
        (b"12", NOT_A_LIST + "binary data"),
        ({"a": 1}, NOT_A_LIST + "an object"),
        (True, NOT_A_LIST + "a boolean"),
        ([True], "status_ids[0]: " + BAD_ITEM + "got a boolean"),
        ([1, "2"], "status_ids[1]: " + BAD_ITEM + "got a string"),
        ([1, 2, 0], "status_ids[2]: " + BAD_ITEM + "got zero or a negative number"),
        ([None], "status_ids[0]: " + BAD_ITEM + "got null"),
        ([1.0], "status_ids[0]: " + BAD_ITEM + "got a decimal number"),
        ([[1]], "status_ids[0]: " + BAD_ITEM + "got a list of 1 item"),
        ([MAX_ID + 1], f"status_ids[0]: {BAD_ITEM}got a number above {MAX_ID}, the largest Gorelo id"),
    ],
)
def test_positive_ids_errors_name_the_param_and_the_item_and_never_the_value(values, message):
    with pytest.raises(ValueError) as info:
        positive_ids("status_ids", values)
    assert str(info.value) == message


def test_positive_ids_never_echo_a_secret_item():
    with pytest.raises(ValueError) as info:
        positive_ids("status_ids", [1, SECRET])
    assert SECRET not in str(info.value) and str(info.value).startswith("status_ids[1]:")


GUID = "3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b"


@pytest.mark.parametrize(
    "value",
    [
        GUID,
        GUID.upper(),
        "3F2B8c1E-0d4A-4b7e-9A51-2c6D8e9F0a1B",  # mixed case
        GUID.replace("-", ""),  # 32 hex digits
        GUID.replace("-", "").upper(),
        uuid.UUID(GUID),
        uid(7),
    ],
)
def test_guid_returns_the_canonical_lowercase_hyphenated_form(value):
    result = guid("ticket_id", value)
    assert result == str(uuid.UUID(str(value))) and result == result.lower() and result.count("-") == 4
    assert type(result) is str


def test_guid_known_forms():
    assert guid("ticket_id", GUID.upper()) == GUID
    assert guid("ticket_id", GUID.replace("-", "")) == GUID
    assert GUID_EXAMPLE == GUID


NOT_TEXT_GUID = "text that is not a GUID"


@pytest.mark.parametrize(
    "value, what",
    [
        (f"{{{GUID}}}", NOT_TEXT_GUID),  # braces
        (f"urn:uuid:{GUID}", NOT_TEXT_GUID),
        (f" {GUID}", NOT_TEXT_GUID),  # nothing is trimmed
        (f"{GUID} ", NOT_TEXT_GUID),
        (f"{GUID}\n", NOT_TEXT_GUID),
        (GUID[:-1], NOT_TEXT_GUID),
        (GUID + "0", NOT_TEXT_GUID),
        (GUID.replace("a", "g"), NOT_TEXT_GUID),
        (GUID.replace("-", "_"), NOT_TEXT_GUID),
        (GUID.replace("-", "", 1), NOT_TEXT_GUID),  # one hyphen missing
        ("3f2b8c1e-0d4a4b7e-9a51-2c6d8e9f0a1b", NOT_TEXT_GUID),  # hyphens in the wrong places
        ("TCK-2029", NOT_TEXT_GUID),
        ("12345", NOT_TEXT_GUID),
        ("３f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b", NOT_TEXT_GUID),  # a full-width digit
        ("", "an empty string"),
        (12345, "a number"),
        (None, "null"),
        (True, "a boolean"),
        (1.5, "a number"),
        ([GUID], "a list of 1 item"),
        ({"Id": GUID}, "an object"),
        (GUID.encode(), "binary data"),
    ],
)
def test_guid_rejects_everything_else_naming_the_param_and_never_the_value(value, what):
    with pytest.raises(ValueError) as info:
        guid("ticket_id", value)
    assert str(info.value) == f"ticket_id: expected a GUID such as {GUID_EXAMPLE}, got {what}"


def test_guid_never_echoes_a_secret():
    with pytest.raises(ValueError) as info:
        guid("ticket_id", SECRET)
    assert SECRET not in str(info.value)


def test_guids():
    assert guids("ticket_ids", None) is None
    assert guids("ticket_ids", [GUID.upper(), uid(2), GUID.replace("-", "")]) == [GUID, uid(2), GUID]
    assert guids("ticket_ids", (uid(1), uid(1))) == [uid(1), uid(1)]  # order and duplicates kept
    source = [uid(1)]
    result = guids("ticket_ids", source)
    assert result == source and result is not source


BAD_GUID_ITEM = f"expected a GUID such as {GUID_EXAMPLE}, got "


@pytest.mark.parametrize(
    "values, message",
    [
        ([], "ticket_ids: expected at least one GUID, got an empty list (omit ticket_ids if you have none to give)"),
        (GUID, "ticket_ids: expected a list of GUIDs, got a string"),
        (5, "ticket_ids: expected a list of GUIDs, got a number"),
        ({"a": 1}, "ticket_ids: expected a list of GUIDs, got an object"),
        ([GUID, "nope"], f"ticket_ids[1]: {BAD_GUID_ITEM}{NOT_TEXT_GUID}"),
        ([5], f"ticket_ids[0]: {BAD_GUID_ITEM}a number"),
        ([None], f"ticket_ids[0]: {BAD_GUID_ITEM}null"),
        ([GUID, GUID, ""], f"ticket_ids[2]: {BAD_GUID_ITEM}an empty string"),
    ],
)
def test_guids_errors_name_the_param_and_the_item(values, message):
    with pytest.raises(ValueError) as info:
        guids("ticket_ids", values)
    assert str(info.value) == message


async def test_a_validator_error_reaches_the_model_as_the_tool_error():
    registry = Registry()

    @declare(registry, kind="write", ops=["POST /v1/clients"])
    async def take(ctx: Context, client_id: int, ticket_id: str) -> dict:
        """Validate."""
        return {"client_id": positive_id("client_id", client_id), "ticket_id": guid("ticket_id", ticket_id)}

    take_fn = registry.specs[0].fn
    assert await take_fn(make_ctx(), 7, GUID.upper()) == {"client_id": 7, "ticket_id": GUID}
    with pytest.raises(ToolError) as info:
        await take_fn(make_ctx(), 0, GUID)
    assert str(info.value) == "client_id: expected a positive whole number such as 123, got zero or a negative number"
    with pytest.raises(ToolError) as info:
        await take_fn(make_ctx(), 7, "nope")
    assert str(info.value).startswith("ticket_id: expected a GUID such as ")


# --------------------------------------------------------------------------
# created_id and expect_object
# --------------------------------------------------------------------------

POST_OP = "POST /v1/items"


@pytest.mark.parametrize(
    "data, expected",
    [
        ({"Id": 7}, 7),
        ({"Id": MAX_ID}, MAX_ID),
        ({"Id": uid(3)}, uid(3)),
        ({"Id": "abc-123"}, "abc-123"),
        ({"Id": 7, "Name": "Widget", "Extra": [1, 2]}, 7),  # anything else in Data is ignored
        ({"Id": " 7 "}, " 7 "),  # returned as Gorelo sent it, not trimmed
    ],
)
def test_created_id_returns_the_id_as_gorelo_sent_it(data, expected):
    assert created_id(data, POST_OP, tool="create_item") == expected


@pytest.mark.parametrize(
    "data, problem",
    [
        (None, "Data is null, not an object with an Id"),
        (True, "Data is a boolean, not an object with an Id"),
        (7, "Data is a number, not an object with an Id"),
        ("7", "Data is a string, not an object with an Id"),
        ([], "Data is an empty list, not an object with an Id"),
        ([{"Id": 7}], "Data is a list of 1 item, not an object with an Id"),
        ({}, "Data is an object without an Id"),
        ({"Name": "Widget"}, "Data is an object without an Id"),
        ({"id": 7}, "Data is an object without an Id"),  # Gorelo's keys are PascalCase
        ({"ItemId": 7}, "Data is an object without an Id"),
        ({"Id": None}, "Data.Id is null"),
        ({"Id": ""}, "Data.Id is blank"),
        ({"Id": "   "}, "Data.Id is blank"),
        ({"Id": 0}, "Data.Id is zero or negative"),
        ({"Id": -4}, "Data.Id is zero or negative"),
        ({"Id": True}, "Data.Id is a boolean"),
        ({"Id": False}, "Data.Id is a boolean"),
        ({"Id": 7.5}, "Data.Id is a decimal number"),
        ({"Id": [7]}, "Data.Id is a list of 1 item"),
        ({"Id": {"Value": 7}}, "Data.Id is an object"),
    ],
)
def test_created_id_raises_an_unconfirmed_write_when_the_answer_has_no_usable_id(data, problem):
    with pytest.raises(GoreloAPIError) as info:
        created_id(data, POST_OP, tool="create_item")
    err = info.value
    assert (err.kind, err.op_key, err.write_unconfirmed, err.status) == ("shape", POST_OP, True, 200)
    assert str(err) == (
        f"{POST_OP}: Gorelo reported success but the answer carries no usable Id for the record ({problem}); "
        "the write may have been applied, so verify it with a read before repeating it"
    )


def test_created_id_never_echoes_what_gorelo_sent():
    for data in (SECRET, [SECRET], {"Id": "", "Name": SECRET}, {"Name": SECRET}, {SECRET: 1}, {"Id": [SECRET]}):
        with pytest.raises(GoreloAPIError) as info:
            created_id(data, POST_OP, tool="create_item")
        assert SECRET not in str(info.value) and SECRET not in info.value.message


def test_created_id_logs_the_tool_and_operation_but_no_values(caplog):
    caplog.set_level(logging.WARNING, logger="gorelo-mcp.tools")
    with pytest.raises(GoreloAPIError):
        created_id({"Name": SECRET}, POST_OP, tool="create_item")
    (line,) = [r.getMessage() for r in caplog.records if r.name == "gorelo-mcp.tools"]
    assert line.startswith("tool=create_item op=POST /v1/items ") and SECRET not in line
    assert "Data is an object without an Id" in line


EXPECT_GET = "GET /v1/clients/{clientId}"
EXPECT_PATCH = "PATCH /v1/clients/{clientId}"


@pytest.mark.parametrize("op_key", [EXPECT_GET, EXPECT_PATCH, "POST /v1/items", "DELETE /v1/items/{itemId}"])
def test_expect_object_returns_a_non_empty_object(op_key):
    data = {"Id": 7, "Name": "Acme"}
    assert expect_object(data, op_key, tool="t") is data
    assert expect_object({"a": 0}, op_key, tool="t") == {"a": 0}


def test_expect_object_turns_any_mapping_into_a_dict():
    import types

    result = expect_object(types.MappingProxyType({"Id": 7}), EXPECT_GET, tool="t")
    assert type(result) is dict and result == {"Id": 7}


@pytest.mark.parametrize(
    "data, got",
    [
        (None, "null"),
        ([], "an empty list"),
        ([{"Id": 7}, {"Id": 8}], "a list of 2 items"),
        ("text", "a string"),
        ("", "an empty string"),
        (7, "a number"),
        (True, "a boolean"),
        ({}, "an empty object"),
    ],
)
def test_expect_object_refuses_other_data_and_a_read_is_not_an_unconfirmed_write(data, got):
    with pytest.raises(GoreloAPIError) as info:
        expect_object(data, EXPECT_GET, tool="get_client")
    err = info.value
    assert (err.kind, err.op_key, err.write_unconfirmed, err.status) == ("shape", EXPECT_GET, False, 200)
    assert str(err) == f"{EXPECT_GET}: expected Data to be a non-empty object but got {got}; refusing to guess"


@pytest.mark.parametrize(
    "op_key", [EXPECT_PATCH, "POST /v1/items", "DELETE /v1/items/{itemId}", "PUT /v1/x", "post /v1/x"]
)
@pytest.mark.parametrize("data, got", [(None, "null"), ([], "an empty list"), ({}, "an empty object"), ("x", "a string")])
def test_expect_object_marks_a_write_as_unconfirmed_and_says_to_verify(op_key, data, got):
    with pytest.raises(GoreloAPIError) as info:
        expect_object(data, op_key, tool="update_client")
    err = info.value
    assert (err.kind, err.op_key, err.write_unconfirmed) == ("shape", op_key, True)
    assert str(err) == (
        f"{op_key}: expected Data to be a non-empty object but got {got}; "
        "the write may have been applied, so verify it with a read before repeating it"
    )


def test_expect_object_treats_the_pdf_export_as_a_write_because_gorelo_records_it():
    (pdf,) = SIDE_EFFECT_GETS
    with pytest.raises(GoreloAPIError) as info:
        expect_object(None, pdf, tool="export_invoice_pdf")
    assert info.value.write_unconfirmed is True


def test_expect_object_treats_a_renamed_pdf_export_as_a_write_too():
    (pdf,) = SIDE_EFFECT_GETS
    for variant in (pdf, re.sub(r"\{[^{}]*\}", "{id}", pdf)):
        with pytest.raises(GoreloAPIError) as info:
            expect_object(None, variant, tool="export_invoice_pdf")
        assert info.value.write_unconfirmed is True, variant


def test_expect_object_allow_empty_accepts_an_empty_object_and_nothing_else_new():
    assert expect_object({}, EXPECT_PATCH, tool="t", allow_empty=True) == {}
    assert expect_object({"a": 1}, EXPECT_PATCH, tool="t", allow_empty=True) == {"a": 1}
    for data, got in ((None, "null"), ([], "an empty list"), ("x", "a string")):
        with pytest.raises(GoreloAPIError) as info:
            expect_object(data, EXPECT_PATCH, tool="t", allow_empty=True)
        assert f"expected Data to be an object but got {got}" in str(info.value) and "non-empty" not in str(info.value)
        assert info.value.write_unconfirmed is True


def test_expect_object_does_not_treat_an_unknown_operation_text_as_a_safe_read():
    for odd in ("", "nonsense", "/v1/clients"):
        with pytest.raises(GoreloAPIError) as info:
            expect_object(None, odd, tool="t")
        assert info.value.write_unconfirmed is True, odd


def test_expect_object_never_echoes_what_gorelo_sent():
    for data in (SECRET, [SECRET], {}, None):
        for op_key in (EXPECT_GET, EXPECT_PATCH):
            with pytest.raises(GoreloAPIError) as info:
                expect_object(data, op_key, tool="t")
            assert SECRET not in str(info.value)


def _shape_registry():
    registry = Registry()

    @declare(
        registry, toolset="core", kind="write", ops=["POST /v1/clients", "GET /v1/clients/{clientId}", "PATCH /v1/clients/{clientId}"]
    )
    async def shaped(ctx: Context, what: str) -> dict:
        """Check the shape of what Gorelo answered."""
        client = client_of(ctx)
        if what == "create":
            data = await client.post("POST /v1/clients", json_body={"Name": "x"}, tool="shaped")
            return {"Id": created_id(data, "POST /v1/clients", tool="shaped")}
        if what == "read":
            data = await client.get_one("GET /v1/clients/{clientId}", path_params={"clientId": 1}, tool="shaped")
            return expect_object(data, "GET /v1/clients/{clientId}", tool="shaped")
        data = await client.patch(
            "PATCH /v1/clients/{clientId}", path_params={"clientId": 1}, json_body={"Name": "x"}, tool="shaped"
        )
        return expect_object(data, "PATCH /v1/clients/{clientId}", tool="shaped")

    return registry


async def test_a_created_id_failure_reaches_the_model_once_with_one_verify_sentence(server_factory, mock_gorelo):
    mock_gorelo.on("POST", "/v1/clients", envelope(None))
    server = server_factory(registry=_shape_registry())
    text = await call_tool_error(server, "shaped", {"what": "create"})
    assert text == (
        "Gorelo returned an unexpected response for shaped: POST /v1/clients: Gorelo reported success but the "
        "answer carries no usable Id for the record (Data is null, not an object with an Id); the write may have "
        "been applied, so verify it with a read before repeating it"
    )
    assert text.lower().count("verify") == 1 and "Verify with a read before retrying" not in text
    assert len(mock_gorelo.requests) == 1  # never repeated


async def test_a_created_id_is_returned_when_the_answer_has_one(server_factory, mock_gorelo):
    mock_gorelo.on("POST", "/v1/clients", envelope({"Id": 9101}))
    assert await call_tool(server_factory(registry=_shape_registry()), "shaped", {"what": "create"}) == {"Id": 9101}


async def test_an_object_failure_on_a_write_and_on_a_read_reach_the_model_with_their_own_advice(
    server_factory, mock_gorelo
):
    mock_gorelo.on("PATCH", "/v1/clients/1", envelope({}))
    mock_gorelo.on("GET", "/v1/clients/1", envelope([{"Id": 1}, {"Id": 2}]))
    server = server_factory(registry=_shape_registry())
    write = await call_tool_error(server, "shaped", {"what": "patch"})
    assert write == (
        "Gorelo returned an unexpected response for shaped: PATCH /v1/clients/{clientId}: expected Data to be a non-empty "
        "object but got an empty object; the write may have been applied, so verify it with a read before repeating it"
    )
    assert write.lower().count("verify") == 1
    read = await call_tool_error(server, "shaped", {"what": "read"})
    assert read == (
        "Gorelo returned an unexpected response for shaped: GET /v1/clients/{clientId}: expected Data to be a non-empty "
        "object but got a list of 2 items; refusing to guess"
    )
    assert "verify" not in read.lower()


def test_format_gorelo_error_still_adds_its_own_verify_sentence_to_other_unconfirmed_shape_errors():
    plain = GoreloAPIError(
        "POST /v1/x: unexpected response shape", status=200, op_key="POST /v1/x", kind="shape", write_unconfirmed=True
    )
    text = format_gorelo_error(plain, "make_x", {})
    assert text.endswith("The change may or may not have been applied. Verify with a read before retrying.")


# --------------------------------------------------------------------------
# Strict types for tool signatures: StrictId and StrictBool
# --------------------------------------------------------------------------


def _strict_and_plain_functions():
    async def plain(
        ctx: Context,
        a: Annotated[int, Field(description="An id.")],
        b: Annotated[list[int], Field(description="Ids.")],
        c: Annotated[int | None, Field(description="An optional id.")] = None,
        d: Annotated[list[int] | None, Field(description="Optional ids.")] = None,
        e: Annotated[bool, Field(description="A flag.")] = False,
    ) -> dict:
        """Plain int and bool."""
        return {}

    async def strict(
        ctx: Context,
        a: Annotated[StrictId, Field(description="An id.")],
        b: Annotated[list[StrictId], Field(description="Ids.")],
        c: Annotated[StrictId | None, Field(description="An optional id.")] = None,
        d: Annotated[list[StrictId] | None, Field(description="Optional ids.")] = None,
        e: Annotated[StrictBool, Field(description="A flag.")] = False,
    ) -> dict:
        """Plain int and bool."""
        return {}

    return plain, strict


def test_the_strict_types_are_pydantics_strict_annotations():
    assert StrictId == Annotated[int, Strict()] and StrictBool == Annotated[bool, Strict()]


def test_a_nested_annotated_strict_id_has_the_same_json_schema_as_a_plain_int_with_the_description():
    plain, strict = _strict_and_plain_functions()
    plain_schema = Tool.from_function(plain).parameters
    strict_schema = Tool.from_function(strict).parameters
    assert strict_schema == plain_schema  # strictness is invisible in the JSON schema
    properties = strict_schema["properties"]
    assert properties["a"] == {"description": "An id.", "type": "integer"}
    assert properties["b"] == {"description": "Ids.", "items": {"type": "integer"}, "type": "array"}
    assert properties["c"] == {
        "anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "description": "An optional id.",
    }
    assert properties["d"]["anyOf"][0] == {"items": {"type": "integer"}, "type": "array"}
    assert properties["e"] == {"default": False, "description": "A flag.", "type": "boolean"}
    assert strict_schema["required"] == ["a", "b"]


def _strict_tool_server(calls):
    registry = Registry()

    @declare(registry, kind="write", ops=["POST /v1/clients"])
    async def strict_tool(
        ctx: Context,
        client_id: Annotated[StrictId, Field(description="An id.")],
        status_ids: Annotated[list[StrictId] | None, Field(description="Ids.")] = None,
        flag: Annotated[StrictBool, Field(description="A flag.")] = False,
    ) -> dict:
        """Take strict values."""
        calls.append((client_id, status_ids, flag))
        return {"client_id": client_id, "status_ids": status_ids, "flag": flag}

    server = FastMCP("t")
    server.add_tool(registry.specs[0].fn)
    return server


@pytest.mark.parametrize("bad", [True, False, "5", " 5", "five", 5.0, 5.5, None, [5], {"id": 5}])
async def test_fastmcp_refuses_json_true_a_string_or_a_float_for_a_strict_id(bad):
    calls = []
    server = _strict_tool_server(calls)
    text = await call_tool_error(server, "strict_tool", {"client_id": bad})
    assert "client_id" in text and "valid integer" in text
    assert calls == []  # the tool never ran


@pytest.mark.parametrize("good", [5, 1, 123, 2**40, MAX_ID])
async def test_fastmcp_accepts_a_json_integer_for_a_strict_id(good):
    calls = []
    server = _strict_tool_server(calls)
    assert (await call_tool(server, "strict_tool", {"client_id": good}))["client_id"] == good
    assert calls == [(good, None, False)]


@pytest.mark.parametrize("bad", [[True], [1, True], ["1"], [1.0], [None], [[1]], [{"id": 1}]])
async def test_fastmcp_refuses_json_true_a_string_or_a_float_inside_a_strict_id_list(bad):
    calls = []
    server = _strict_tool_server(calls)
    text = await call_tool_error(server, "strict_tool", {"client_id": 1, "status_ids": bad})
    assert "status_ids" in text and "valid integer" in text
    assert calls == []


@pytest.mark.parametrize("bad", ["1", 5, True, {"a": 1}])
async def test_fastmcp_refuses_a_strict_id_list_that_is_not_a_list(bad):
    calls = []
    text = await call_tool_error(_strict_tool_server(calls), "strict_tool", {"client_id": 1, "status_ids": bad})
    assert "status_ids" in text and calls == []


async def test_fastmcp_accepts_a_strict_id_list_and_an_omitted_or_null_optional():
    calls = []
    server = _strict_tool_server(calls)
    assert (await call_tool(server, "strict_tool", {"client_id": 1, "status_ids": [1, 2, 3]}))["status_ids"] == [1, 2, 3]
    assert (await call_tool(server, "strict_tool", {"client_id": 1, "status_ids": []}))["status_ids"] == []
    assert (await call_tool(server, "strict_tool", {"client_id": 1, "status_ids": None}))["status_ids"] is None
    assert (await call_tool(server, "strict_tool", {"client_id": 1}))["status_ids"] is None
    assert [c[1] for c in calls] == [[1, 2, 3], [], None, None]


@pytest.mark.parametrize("bad", ["true", "false", "yes", 1, 0, None, [True]])
async def test_fastmcp_refuses_text_and_numbers_for_a_strict_bool(bad):
    calls = []
    text = await call_tool_error(_strict_tool_server(calls), "strict_tool", {"client_id": 1, "flag": bad})
    assert "flag" in text and "valid boolean" in text and calls == []


async def test_fastmcp_accepts_real_booleans_for_a_strict_bool():
    calls = []
    server = _strict_tool_server(calls)
    assert (await call_tool(server, "strict_tool", {"client_id": 1, "flag": True}))["flag"] is True
    assert (await call_tool(server, "strict_tool", {"client_id": 1, "flag": False}))["flag"] is False


async def test_the_lax_int_is_what_the_strict_types_replace():
    # the control: a plain int takes JSON true as 1 (which is a real client id), "5" as 5 and 5.0 as 5
    registry = Registry()

    @declare(registry, kind="write", ops=["POST /v1/clients"])
    async def lax_tool(ctx: Context, client_id: Annotated[int, Field(description="An id.")]) -> dict:
        """Take a lax int."""
        return {"client_id": client_id}

    server = FastMCP("t")
    server.add_tool(registry.specs[0].fn)
    assert await call_tool(server, "lax_tool", {"client_id": True}) == {"client_id": 1}
    assert await call_tool(server, "lax_tool", {"client_id": "5"}) == {"client_id": 5}
    assert await call_tool(server, "lax_tool", {"client_id": 5.0}) == {"client_id": 5}


async def test_the_listed_input_schema_of_a_strict_tool_is_what_the_model_reads():
    server = _strict_tool_server([])
    (listed,) = await list_tools(server)
    properties = listed.inputSchema["properties"]
    assert properties["client_id"] == {"description": "An id.", "type": "integer"}
    assert properties["status_ids"]["description"] == "Ids." and properties["flag"]["type"] == "boolean"
    assert listed.inputSchema["required"] == ["client_id"]


# --------------------------------------------------------------------------
# build_body
# --------------------------------------------------------------------------

FIELD_MAP = {
    "name": "Name",
    "billing_name": "BillingName",
    "alternate_name": "AlternateName",
    "location_name": "Location.Name",
    "location_phone": "Location.Phone",
    "tag_ids": "TagIds",
    "tax_id": "TaxId",
    "enabled": "Enabled",
    "count": "Count",
}


def test_build_body_maps_names_and_omits_none():
    body = build_body({"name": "Acme", "billing_name": None, "alternate_name": None}, FIELD_MAP)
    assert body == {"Name": "Acme"}


def test_build_body_nests_dotted_paths_and_merges_siblings():
    body = build_body({"name": "Acme", "location_name": "HQ", "location_phone": "555", "billing_name": None}, FIELD_MAP)
    assert body == {"Name": "Acme", "Location": {"Name": "HQ", "Phone": "555"}}
    assert build_body({"location_phone": "555"}, FIELD_MAP) == {"Location": {"Phone": "555"}}


def test_build_body_keeps_falsy_values_that_are_not_empty_strings_or_lists():
    assert build_body({"enabled": False, "count": 0}, FIELD_MAP) == {"Enabled": False, "Count": 0}


def test_build_body_returns_a_new_dict_and_never_mutates_values():
    nested = {"a": 1}
    values = {"name": "x"}
    body = build_body(values, {"name": "Name", "extra": "Extra"} | {})
    assert body is not values and values == {"name": "x"}
    body = build_body({"extra": nested}, {"extra": "Extra"})
    assert body["Extra"] is nested and nested == {"a": 1}


@pytest.mark.parametrize("empty", ["", "   ", [], (), {}])
def test_build_body_rejects_empty_values_and_names_the_param(empty):
    with pytest.raises(ValueError) as info:
        build_body({"alternate_name": empty}, FIELD_MAP)
    assert str(info.value).startswith("alternate_name: must not be empty or whitespace only")


@pytest.mark.parametrize("empty", ["", "   ", [], (), {}])
def test_build_body_empty_value_message_does_not_assume_a_patch(empty):
    # build_body serves creates and updates alike, so it must not talk about "leaving the field unchanged"
    with pytest.raises(ValueError) as info:
        build_body({"tag_ids": empty}, FIELD_MAP)
    message = str(info.value)
    assert message == (
        "tag_ids: must not be empty or whitespace only; give it a real value, or leave it out if it is optional "
        "(where the tool offers an explicit option to clear the field, use that option instead)"
    )
    for patch_wording in ("unchanged", "PATCH", "update", "leave the field"):
        assert patch_wording not in message


def test_build_body_empty_value_message_names_the_param_it_is_about():
    for param in ("name", "billing_name", "location_phone"):
        with pytest.raises(ValueError) as info:
            build_body({"enabled": True, param: " "}, FIELD_MAP)
        assert str(info.value).startswith(f"{param}: must not be empty or whitespace only; give it a real value")


def test_build_body_still_omits_none_and_sends_a_cleared_param_without_the_message():
    assert build_body({"name": None, "alternate_name": ""}, FIELD_MAP, clear=["alternate_name"]) == {"AlternateName": ""}


def test_build_body_clear_sends_the_clear_value():
    assert build_body({"alternate_name": None}, FIELD_MAP, clear=["alternate_name"]) == {"AlternateName": ""}
    assert build_body({"alternate_name": ""}, FIELD_MAP, clear=["alternate_name"]) == {"AlternateName": ""}
    body = build_body(
        {"alternate_name": None, "tax_id": None, "tag_ids": None, "name": "x"},
        FIELD_MAP,
        clear=["alternate_name", "tax_id", "tag_ids"],
        clear_values={"tax_id": 0, "tag_ids": []},
    )
    assert body == {"AlternateName": "", "TaxId": 0, "TagIds": [], "Name": "x"}


def test_build_body_clear_can_send_an_explicit_null():
    assert build_body({"tax_id": None}, FIELD_MAP, clear=["tax_id"], clear_values={"tax_id": None}) == {"TaxId": None}


def test_build_body_clears_nested_fields_and_params_missing_from_values():
    assert build_body({}, FIELD_MAP, clear=["location_phone"]) == {"Location": {"Phone": ""}}
    assert build_body({"location_name": "HQ"}, FIELD_MAP, clear=iter(["location_phone"])) == {
        "Location": {"Name": "HQ", "Phone": ""}
    }


def test_build_body_refuses_to_set_and_clear_the_same_field():
    with pytest.raises(ValueError, match="alternate_name: cannot be given a value and cleared"):
        build_body({"alternate_name": "x"}, FIELD_MAP, clear=["alternate_name"])


def test_build_body_programming_errors():
    with pytest.raises(ValueError, match="'nope' has no entry in field_map"):
        build_body({"nope": 1}, FIELD_MAP)
    with pytest.raises(ValueError, match="nope"):
        build_body({}, FIELD_MAP, clear=["nope"])
    with pytest.raises(ValueError, match="overlap"):
        build_body({"a": {"x": 1}, "b": 2}, {"a": "Loc", "b": "Loc.Name"})
    with pytest.raises(ValueError, match="overlap"):
        build_body({"b": 2, "a": {"x": 1}}, {"a": "Loc", "b": "Loc.Name"})
    with pytest.raises(ValueError, match="set twice"):
        build_body({"a": 1, "b": 2}, {"a": "Same", "b": "Same"})


def test_build_body_refuses_clear_values_for_a_name_that_is_not_in_field_map():
    with pytest.raises(ValueError, match="clear_values names nope, which is not in field_map") as info:
        build_body({}, FIELD_MAP, clear=["tax_id"], clear_values={"tax_id": 0, "nope": 1})
    assert "programming error" in str(info.value)
    with pytest.raises(ValueError, match="clear_values names nope"):
        build_body({"name": "x"}, FIELD_MAP, clear_values={"nope": 1})
    with pytest.raises(ValueError, match="clear_values names a, b"):
        build_body({}, FIELD_MAP, clear_values={"b": 1, "a": 2})


STATIC_CLEAR_VALUES = {"alternate_name": "", "tax_id": 0, "tag_ids": [], "enabled": None}


def test_build_body_accepts_a_static_clear_values_map_with_entries_for_params_that_are_not_cleared():
    # a tool keeps ONE map for all its clearable fields and passes it on every call; an entry
    # is used only for a param that is cleared in this call and is otherwise ignored (it is not an error)
    assert build_body({"name": "x"}, FIELD_MAP, clear_values=STATIC_CLEAR_VALUES) == {"Name": "x"}
    assert build_body({}, FIELD_MAP, clear_values={"tax_id": 0}) == {}
    assert build_body({"tax_id": None}, FIELD_MAP, clear=["alternate_name"], clear_values={"tax_id": 0}) == {
        "AlternateName": ""
    }
    assert build_body({"tax_id": None, "name": "x"}, FIELD_MAP, clear_values=STATIC_CLEAR_VALUES) == {"Name": "x"}
    # a value the caller gave is sent as it is: the entry is for clearing only
    assert build_body({"tax_id": 5}, FIELD_MAP, clear_values={"tax_id": 0}) == {"TaxId": 5}
    # and the same map clears exactly the params named in `clear`, each with its own value
    assert build_body({"tax_id": None, "tag_ids": None}, FIELD_MAP, clear=["tax_id"], clear_values=STATIC_CLEAR_VALUES) == {
        "TaxId": 0
    }
    assert build_body({}, FIELD_MAP, clear=["tag_ids", "alternate_name", "enabled"], clear_values=STATIC_CLEAR_VALUES) == {
        "TagIds": [], "AlternateName": "", "Enabled": None,
    }
    assert STATIC_CLEAR_VALUES == {"alternate_name": "", "tax_id": 0, "tag_ids": [], "enabled": None}  # never mutated


def test_build_body_still_refuses_a_clear_values_key_that_is_not_in_field_map_even_when_nothing_is_cleared():
    # typos still raise: the membership check in field_map stays
    for clear in ((), ["tax_id"]):
        with pytest.raises(ValueError, match="clear_values names tax_idd, which is not in field_map") as info:
            build_body({"name": "x"}, FIELD_MAP, clear=clear, clear_values={"tax_id": 0, "tax_idd": 0})
        assert "programming error" in str(info.value)
    with pytest.raises(ValueError, match="clear_values names Name, which is not in field_map"):
        build_body({}, FIELD_MAP, clear_values={"Name": ""})  # the PascalCase field is not a param name


def test_build_body_takes_clear_none_as_empty():
    assert build_body({"name": "x", "tax_id": None}, FIELD_MAP, clear=None) == {"Name": "x"}
    assert build_body({}, FIELD_MAP, clear=None, clear_values=None) == {}
    assert build_body({}, FIELD_MAP, clear=None, clear_values={"tax_id": 0}) == {}  # an unused entry is fine
    with pytest.raises(ValueError, match="clear_values names nope"):
        build_body({}, FIELD_MAP, clear=None, clear_values={"nope": 0})
    assert build_body({"name": "x"}, FIELD_MAP, clear_values={}) == {"Name": "x"}
    assert build_body({"name": "x"}, FIELD_MAP, clear_values=None) == {"Name": "x"}


@pytest.mark.parametrize("clear", ["alternate_name", b"alternate_name", "tax_id"])
def test_build_body_refuses_a_string_for_clear(clear):
    # "alternate_name" iterates as letters: the tool meant ["alternate_name"]
    with pytest.raises(ValueError, match="clear must be a list of parameter names"):
        build_body({"alternate_name": None}, FIELD_MAP, clear=clear)  # type: ignore[arg-type]


def test_build_body_refuses_clear_entries_that_are_not_names():
    with pytest.raises(ValueError, match="clear must contain parameter names"):
        build_body({}, FIELD_MAP, clear=[5])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="clear must contain parameter names"):
        build_body({}, FIELD_MAP, clear=["tax_id", None])  # type: ignore[list-item]


# --------------------------------------------------------------------------
# Result shapes
# --------------------------------------------------------------------------


def test_paged_result():
    page = Page(items=[{"Id": 1}, {"Id": 2}], next_cursor="c2", has_more=True, total_count=40, page_size=2)
    result = paged_result(page, {"query": "acme", "status_ids": None, "client_ids": [1, 2], "flag": False, "empty": ""})
    assert result == {
        "items": [{"Id": 1}, {"Id": 2}],
        "count": 2,
        "total_count": 40,
        "has_more": True,
        "next_cursor": "c2",
        "page_size": 2,
        "filters": {"query": "acme", "client_ids": [1, 2], "flag": False, "empty": ""},
    }
    assert list(result) == ["items", "count", "total_count", "has_more", "next_cursor", "page_size", "filters"]


def test_paged_result_of_a_last_page():
    page = Page(items=[], next_cursor=None, has_more=False, total_count=None, page_size=50)
    result = paged_result(page, {})
    assert result["count"] == 0 and result["has_more"] is False and result["next_cursor"] is None
    assert result["total_count"] is None and result["filters"] == {}


def test_list_result():
    assert list_result([{"Id": 1}]) == {"items": [{"Id": 1}], "count": 1}
    assert list_result([]) == {"items": [], "count": 0}


def test_all_result():
    done = AllResult(items=[{"Id": 1}, {"Id": 2}], total_count=2, complete=True, pages=1, count_mismatch=False)
    assert all_result(done, {"client_id": 7, "query": None}) == {
        "items": [{"Id": 1}, {"Id": 2}],
        "count": 2,
        "total_count": 2,
        "truncated": False,
        "complete_scan": True,
        "count_mismatch": False,
        "filters": {"client_id": 7},
    }
    cut = AllResult(items=[{"Id": 1}], total_count=9, complete=False, pages=1, count_mismatch=False)
    result = all_result(cut, None)
    assert result["truncated"] is True and result["complete_scan"] is False and result["filters"] == {}
    odd = AllResult(items=[], total_count=3, complete=True, pages=1, count_mismatch=True)
    assert all_result(odd, {})["count_mismatch"] is True


DONE_SCAN = AllResult(items=[{"Id": 1}], total_count=1, complete=True, pages=1, count_mismatch=False)
CUT_SCAN = AllResult(items=[{"Id": 1}], total_count=9, complete=False, pages=1, count_mismatch=False)


def test_all_result_computes_truncated_from_the_scan_unless_it_is_given():
    assert all_result(DONE_SCAN, None)["truncated"] is False
    assert all_result(CUT_SCAN, None)["truncated"] is True
    assert all_result(DONE_SCAN, None, truncated=None) == all_result(DONE_SCAN, None)
    assert all_result(CUT_SCAN, None, truncated=None) == all_result(CUT_SCAN, None)


def test_an_explicit_truncated_overrides_the_computed_value_either_way():
    assert all_result(DONE_SCAN, None, truncated=True)["truncated"] is True  # a tool that cut the rows itself
    assert all_result(CUT_SCAN, None, truncated=False)["truncated"] is False


def test_an_explicit_truncated_changes_nothing_else_and_complete_scan_keeps_describing_the_scan():
    shown = all_result(DONE_SCAN, {"q": "x", "none": None}, truncated=True)
    assert shown == {
        "items": [{"Id": 1}],
        "count": 1,
        "total_count": 1,
        "truncated": True,
        "complete_scan": True,  # everything was read, but not everything is shown
        "count_mismatch": False,
        "filters": {"q": "x"},
    }
    assert list(shown) == ["items", "count", "total_count", "truncated", "complete_scan", "count_mismatch", "filters"]
    assert all_result(CUT_SCAN, {}, truncated=False)["complete_scan"] is False


@pytest.mark.parametrize("bad", [1, 0, "yes", "", [], {}])
def test_all_result_refuses_a_truncated_that_is_not_a_boolean(bad):
    with pytest.raises(ValueError, match="all_result: truncated must be True, False or None"):
        all_result(DONE_SCAN, None, truncated=bad)  # type: ignore[arg-type]


def test_all_result_truncated_is_keyword_only():
    with pytest.raises(TypeError):
        all_result(DONE_SCAN, None, True)  # type: ignore[misc]


def test_ok_result():
    assert ok_result(True) == {"ok": True}
    assert ok_result(False) == {"ok": False}
    record = {"Id": 5, "Outcome": "Deleted"}
    assert ok_result(record) is record
    assert ok_result([1, 2]) == {"result": [1, 2]}
    assert ok_result(None) == {"result": None}
    assert ok_result("text") == {"result": "text"}
    assert ok_result(0) == {"result": 0}


# --------------------------------------------------------------------------
# format_gorelo_error
# --------------------------------------------------------------------------

MAP = {
    "name": "Name",
    "location_phone": "Location.Phone",
    "billing_phone": "Billing.Phone",
    "location_city": "Location.City",
    "item_name": "Items.Name",
    "page_size": "PageSize",
}


def gerr(*notes, status=400, kind="http", trace="00-abc-01", **extra):
    return GoreloAPIError(
        "x", status=status, op_key="POST /v1/clients", kind=kind, trace_id=trace,
        notifications=[{"code": c, "message": m, "property": p} for c, m, p in notes], **extra,
    )


def test_the_documented_format():
    err = gerr(("070101", "Mobile phone validation failed", "Location.Phone"))
    assert format_gorelo_error(err, "create_client", MAP) == (
        "Gorelo rejected create_client (HTTP 400, code 070101): location_phone: "
        "Mobile phone validation failed [trace 00-abc-01]"
    )


def test_every_notification_is_shown_and_distinct_codes_are_listed():
    err = gerr(
        ("070101", "Mobile phone validation failed", "Location.Phone"),
        ("070101", "City is too long", "Location.City"),
        ("070201", "Invalid or malformed request body.", None),
    )
    text = format_gorelo_error(err, "create_client", MAP)
    assert text == (
        "Gorelo rejected create_client (HTTP 400, code 070101/070201): location_phone: Mobile phone validation failed; "
        "location_city: City is too long; Invalid or malformed request body. [trace 00-abc-01]"
    )


def test_property_mapping_exact_then_case_insensitive_then_unique_last_segment():
    def mapped(prop, field_map=MAP):
        text = format_gorelo_error(gerr(("070101", "msg", prop)), "t", field_map)
        return text.split("): ", 1)[1].split(": msg")[0]

    assert mapped("Location.Phone") == "location_phone"  # exact dotted match
    assert mapped("location.phone") == "location_phone"  # case-insensitive dotted match
    assert mapped("Name", {"name": "Name", "location_phone": "Location.Phone"}) == "name"  # exact, nothing else fits
    assert mapped("name", {"name": "Name"}) == "name"  # case-insensitive, nothing else fits
    assert mapped("City") == "location_city"  # bare last segment, unique
    assert mapped("city") == "location_city"
    assert mapped("PageSize") == "page_size"
    assert mapped("Items[0].Name") == "item_name"  # list index ignored


def test_an_ambiguous_or_unknown_property_keeps_gorelos_name():
    def mapped(prop, field_map=MAP):
        text = format_gorelo_error(gerr(("070101", "msg", prop)), "t", field_map)
        return text.split("): ", 1)[1].split(": msg")[0]

    assert mapped("Phone") == "Phone"  # Location.Phone and Billing.Phone both end in Phone
    assert mapped("Mystery") == "Mystery"
    assert mapped("Billing.Fax") == "Billing.Fax"  # a dotted name is never matched by its last segment only
    assert mapped("Location.Fax") == "Location.Fax"
    assert mapped("Name", {}) == "Name" and mapped("Name", None) == "Name"


def test_a_notification_without_a_property_shows_only_its_message_or_code():
    err = gerr(("070201", "Invalid or malformed request body.", None), ("070999", None, None))
    text = format_gorelo_error(err, "t", MAP)
    assert "): Invalid or malformed request body.; 070999 [trace" in text


def test_no_notifications_and_no_trace():
    err = GoreloAPIError("x", status=502, op_key="GET /v1/clients", kind="http")
    assert format_gorelo_error(err, "list_clients", {}) == "Gorelo rejected list_clients (HTTP 502): no details returned"


def test_the_missing_scope_message_names_the_scope_and_says_how_to_fix_it():
    err = gerr(("080203", "API key does not have 'Project' scope", None), status=403)
    text = format_gorelo_error(err, "list_projects", {})
    assert text == (
        "Gorelo rejected list_projects (HTTP 403, code 080203): the API key does not have the 'Project' scope. "
        "Grant it on the API key in Gorelo, then retry [trace 00-abc-01]"
    )
    unnamed = gerr(("080203", "forbidden", None), status=403)
    assert "does not have a scope this tool needs" in format_gorelo_error(unnamed, "t", {})


def test_rate_limit_message():
    err = GoreloAPIError("x", status=429, op_key="GET /v1/tickets", kind="rate_limit")
    text = format_gorelo_error(err, "list_tickets", {})
    assert "rate limiting" in text and "list_tickets" in text and "Retry later" in text and "did not process" in text


def test_unconfirmed_write_message_says_to_verify_before_retrying():
    for kind, what in (("timeout", "timed out"), ("transport", "connection failed")):
        err = GoreloAPIError("x", op_key="POST /v1/tickets", kind=kind, write_unconfirmed=True)
        text = format_gorelo_error(err, "create_ticket", {})
        assert text.startswith("Gorelo did not confirm create_ticket") and what in text
        assert "may or may not have been applied" in text and "Verify with a read before retrying" in text


def test_a_read_timeout_is_safe_to_retry():
    err = GoreloAPIError("x", op_key="GET /v1/tickets", kind="timeout")
    text = format_gorelo_error(err, "list_tickets", {})
    assert "did not answer list_tickets" in text and "safe" in text and "Verify" not in text


def test_a_server_error_on_a_write_adds_the_verify_message_after_the_notifications():
    err = gerr(("070001", "Internal error", None), status=500, write_unconfirmed=True)
    text = format_gorelo_error(err, "create_client", {})
    assert text.startswith("Gorelo rejected create_client (HTTP 500, code 070001): Internal error")
    assert "may have applied the change" in text and text.endswith("[trace 00-abc-01]")


PDF = "GET /v1/invoices/{invoiceId}/pdf"


def test_an_unconfirmed_pdf_export_says_it_may_be_recorded_and_a_retry_records_another():
    for kind, what in (("timeout", "the request timed out"), ("transport", "the connection failed")):
        err = GoreloAPIError("x", op_key=PDF, kind=kind, write_unconfirmed=True)
        text = format_gorelo_error(err, "export_invoice_pdf", {})
        assert text == (
            f"Gorelo did not confirm export_invoice_pdf ({what}); {EXPORT_NOTE}. "
            "Retry only if one more export event is acceptable."
        )
        assert "Verify with a read" not in text and "change may or may not" not in text
    shape = GoreloAPIError("GET /v1/x: unexpected response shape; refusing to guess", status=200, op_key=PDF, kind="shape", write_unconfirmed=True, trace_id="t9")
    text = format_gorelo_error(shape, "export_invoice_pdf", {})
    assert text.count(EXPORT_NOTE) == 1 and text.endswith("Retry only if one more export event is acceptable. [trace t9]")
    already = GoreloAPIError(f"{PDF}: unexpected response shape ({EXPORT_NOTE})", status=200, op_key=PDF, kind="shape", write_unconfirmed=True)
    assert format_gorelo_error(already, "export_invoice_pdf", {}).count(EXPORT_NOTE) == 1  # never said twice
    server_error = GoreloAPIError("x", status=503, op_key=PDF, kind="http", write_unconfirmed=True, trace_id="t8",
                                  notifications=[{"code": "070001", "message": "busy", "property": None}])
    text = format_gorelo_error(server_error, "export_invoice_pdf", {})
    assert text == (
        f"Gorelo rejected export_invoice_pdf (HTTP 503, code 070001): busy. Note: {EXPORT_NOTE}. "
        "Retry only if one more export event is acceptable. [trace t8]"
    )


def test_an_unconfirmed_pdf_export_under_a_renamed_placeholder_says_it_may_be_recorded():
    (pdf,) = SIDE_EFFECT_GETS
    variant = re.sub(r"\{[^{}]*\}", "{documentId}", pdf)
    err = GoreloAPIError("x", op_key=variant, kind="timeout", write_unconfirmed=True)
    assert format_gorelo_error(err, "export_invoice_pdf", {}) == (
        f"Gorelo did not confirm export_invoice_pdf (the request timed out); {EXPORT_NOTE}. "
        "Retry only if one more export event is acceptable."
    )


def test_a_pdf_export_that_gorelo_refused_or_that_never_reached_it_is_worded_as_usual():
    refused = GoreloAPIError("x", status=404, op_key=PDF, kind="http", notifications=[{"code": "070401", "message": "Invoice not found", "property": None}])
    text = format_gorelo_error(refused, "export_invoice_pdf", {})
    assert text == "Gorelo rejected export_invoice_pdf (HTTP 404, code 070401): Invoice not found" and "export" not in text.split(":", 1)[1]
    other = GoreloAPIError("x", op_key="GET /v1/clients", kind="timeout")  # an ordinary read
    assert "retrying is safe" in format_gorelo_error(other, "list_clients", {})
    not_unconfirmed = GoreloAPIError("x", op_key=PDF, kind="timeout")  # a hand-built error without the flag
    assert "retrying is safe" in format_gorelo_error(not_unconfirmed, "export_invoice_pdf", {})


def test_forbidden_operations_are_described_as_deliberately_unavailable():
    err = GoreloAPIError("x", op_key="DELETE /v1/clients/{clientId}", kind="forbidden")
    text = format_gorelo_error(err, "rogue", {})
    assert "deliberately not available" in text and "DELETE /v1/clients/{clientId}" in text
    assert "deletes or uninstalls production data" in text


def test_the_api_key_creation_is_described_as_deliberately_unavailable_too():
    err = GoreloAPIError("x", op_key="POST /v1/api-keys", kind="forbidden")
    text = format_gorelo_error(err, "rogue", {})
    assert text.startswith("rogue cannot run: POST /v1/api-keys is deliberately not available through this server")
    assert "creates API keys" in text and "Do it in the Gorelo app if it is really intended." in text


def test_shape_and_spec_errors():
    shape = GoreloAPIError("GET /v1/x: unexpected response shape; refusing to guess", status=200, op_key="GET /v1/x", kind="shape", trace_id="t1")
    text = format_gorelo_error(shape, "get_x", {})
    assert text.startswith("Gorelo returned an unexpected response for get_x") and "refusing to guess" in text and text.endswith("[trace t1]")
    unsure = GoreloAPIError("POST /v1/x: unexpected response shape", status=200, op_key="POST /v1/x", kind="shape", write_unconfirmed=True)
    assert "Verify with a read before retrying" in format_gorelo_error(unsure, "make_x", {})
    spec = GoreloAPIError("unknown Gorelo operation 'GET /v1/nope'", op_key="GET /v1/nope", kind="spec")
    assert format_gorelo_error(spec, "t", {}) == "t cannot run: unknown Gorelo operation 'GET /v1/nope'"


def test_envelope_errors_read_like_http_errors():
    err = gerr(("070101", "Bad value", "PageSize"), status=200, kind="envelope")
    assert format_gorelo_error(err, "create_client", MAP).startswith("Gorelo rejected create_client (HTTP 200, code 070101): page_size: Bad value")


NAME_MAP = {
    "name": "Name",
    "location_name": "Location.Name",
    "billing_name": "Billing.Name",
    "phone": "Phone",
    "location_phone": "Location.Phone",
    "city": "Location.City",
}


def _named(prop, field_map=NAME_MAP):
    text = format_gorelo_error(gerr(("070101", "msg", prop)), "t", field_map)
    return text.split("): ", 1)[1].split(": msg")[0]


def test_a_bare_name_that_fits_a_top_level_and_nested_fields_names_every_candidate():
    # Gorelo reports a bad Location.Name as just "Name", which is also the top-level Name
    assert _named("Name") == "name or location_name or billing_name"
    assert _named("name") == "name or location_name or billing_name"  # case-insensitive
    assert _named("Phone") == "phone or location_phone"
    err = gerr(("070101", "Name is required", "Name"))
    assert format_gorelo_error(err, "create_client", NAME_MAP) == (
        "Gorelo rejected create_client (HTTP 400, code 070101): name or location_name or billing_name: "
        "Name is required [trace 00-abc-01]"
    )
    two = {"name": "Name", "location_name": "Location.Name"}
    assert _named("Name", two) == "name or location_name"
    assert _named("Items[2].Name", {"name": "Name", "item_name": "Items.Name"}) == "item_name"  # dotted: never ambiguous


def test_a_dotted_name_and_a_unique_nested_name_stay_a_single_candidate():
    assert _named("Location.Name") == "location_name"
    assert _named("location.name") == "location_name"
    assert _named("Billing.Name") == "billing_name"
    assert _named("City") == "city"  # only one nested path ends in City and there is no top-level City
    assert _named("Location.City") == "city"
    assert _named("Location.Fax") == "Location.Fax"  # unknown dotted names keep Gorelo's own
    # nested paths only: still ambiguous, so Gorelo's own name stays (nothing top-level to anchor on)
    assert _named("Name", {"location_name": "Location.Name", "billing_name": "Billing.Name"}) == "Name"


def test_every_ambiguous_notification_in_one_error_is_expanded():
    err = gerr(("070101", "a", "Name"), ("070101", "b", "Phone"), ("070101", "c", "Location.City"))
    text = format_gorelo_error(err, "create_client", NAME_MAP)
    assert "name or location_name or billing_name: a; phone or location_phone: b; city: c" in text


# --------------------------------------------------------------------------
# Index aware error names ("Attachments[0].Url" -> "attachments (item 1, Url)")
# --------------------------------------------------------------------------

INDEX_MAP = {
    "attachments": "Attachments",
    "sub_items": "SubItems",
    "location": "Location",
    "title": "Title",
    "item_name": "Items.Name",
}


@pytest.mark.parametrize(
    "prop, label",
    [
        ("Attachments[0].Url", "attachments (item 1, Url)"),
        ("Attachments[1].Name", "attachments (item 2, Name)"),
        ("Attachments[11].Url", "attachments (item 12, Url)"),
        ("SubItems[1].ItemId", "sub_items (item 2, ItemId)"),
        ("SubItems[0].Quantity", "sub_items (item 1, Quantity)"),
        ("attachments[0].url", "attachments (item 1, url)"),  # the ancestor matches in any case; the rest is Gorelo's spelling
        ("Location.Phones[2].Number", "location (Phones item 3, Number)"),  # the list is below the mapped ancestor
        ("Attachments[0].Files[1].Path", "attachments (item 1, Files item 2, Path)"),  # the nearest MAPPED ancestor is Attachments
        ("Attachments[].Url", "attachments (Url)"),  # an empty index carries no item number
        ("Attachments[0][1].Url", "attachments (item 1, item 2, Url)"),
    ],
)
def test_a_property_inside_a_list_is_shown_against_the_nearest_mapped_ancestor_with_one_based_items(prop, label):
    err = gerr(("070101", "The value is not valid", prop))
    assert format_gorelo_error(err, "create_thing", INDEX_MAP) == (
        f"Gorelo rejected create_thing (HTTP 400, code 070101): {label}: The value is not valid [trace 00-abc-01]"
    )


def test_the_nearest_ancestor_wins_over_a_more_distant_one():
    field_map = {"location": "Location", "phones": "Location.Phones"}
    assert _named("Location.Phones[1].Number", field_map) == "phones (item 2, Number)"
    assert _named("Location.Phones[1].Number", {"location": "Location"}) == "location (Phones item 2, Number)"


def test_an_exact_mapping_still_wins_over_the_ancestor_and_ignores_the_index():
    assert _named("Attachments[0].Url", {"attachments": "Attachments", "attachment_url": "Attachments.Url"}) == "attachment_url"
    assert _named("Items[2].Name", INDEX_MAP) == "item_name"
    assert _named("Attachments[3]", INDEX_MAP) == "attachments"  # the item itself: exact after the index is dropped
    assert _named("Location.Phone", {"location_phone": "Location.Phone", "location": "Location"}) == "location_phone"


@pytest.mark.parametrize(
    "prop",
    ["Nope[0].X", "Attachments[0", "[0].Url", "Attachments[x].Url", "Attachments[0]x.Url", "Attachments[0]..Url", "Attachments[-1].Url"],
)
def test_a_list_property_without_a_mapped_ancestor_or_with_a_malformed_index_keeps_gorelos_name(prop):
    assert _named(prop, INDEX_MAP) == prop


def test_a_sibling_path_is_not_an_ancestor_and_an_empty_map_maps_nothing():
    assert _named("Items[0].Quantity", {"item_name": "Items.Name"}) == "Items[0].Quantity"
    assert _named("Attachments[0].Url", {}) == "Attachments[0].Url"
    assert _named("Attachments[0].Url", None) == "Attachments[0].Url"


def test_each_notification_in_one_error_is_labelled_on_its_own():
    err = gerr(
        ("070101", "bad url", "Attachments[0].Url"),
        ("070101", "bad quantity", "SubItems[2].Quantity"),
        ("070101", "too long", "Title"),
        ("070201", "Invalid body", None),
        ("070101", "odd", "Mystery[0].X"),
    )
    assert format_gorelo_error(err, "create_thing", INDEX_MAP) == (
        "Gorelo rejected create_thing (HTTP 400, code 070101/070201): attachments (item 1, Url): bad url; "
        "sub_items (item 3, Quantity): bad quantity; title: too long; Invalid body; Mystery[0].X: odd [trace 00-abc-01]"
    )


async def test_a_list_item_error_reaches_the_model_against_the_list_parameter(server_factory, mock_gorelo):
    registry = Registry()

    @declare(registry, toolset="core", kind="write", ops=["POST /v1/clients"], field_map={"attachments": "Attachments"})
    async def attach(ctx: Context) -> dict:
        """Post something."""
        return await client_of(ctx).post("POST /v1/clients", json_body={"Name": "x"}, tool="attach")

    mock_gorelo.on("POST", "/v1/clients", error_envelope(400, [("070101", "The url is not allowed", "Attachments[1].Url")], trace_id="00-t-1"))
    text = await call_tool_error(server_factory(registry=registry), "attach")
    assert text == "Gorelo rejected attach (HTTP 400, code 070101): attachments (item 2, Url): The url is not allowed [trace 00-t-1]"


async def test_the_field_map_may_also_name_query_parameters_and_path_placeholders(server_factory, mock_gorelo):
    # the decorator documents this: read tools put their query names and placeholders in field_map for error naming
    registry = Registry()
    field_map = {"status_ids": "StatusIds", "page_size": "PageSize", "client_id": "clientId", "ticket_id": "ticketId"}

    @declare(registry, toolset="core", kind="read", ops=["GET /v1/clients"], field_map=field_map)
    async def list_things(ctx: Context) -> dict:
        """List things."""
        return {"items": (await client_of(ctx).get_page("GET /v1/clients", tool="list_things")).items}

    notes = [
        ("070101", "bad ids", "StatusIds"),
        ("070101", "bad size", "pagesize"),
        ("070101", "bad client", "clientId"),
        ("070101", "bad ticket", "TicketId"),
    ]
    mock_gorelo.on("GET", "/v1/clients", error_envelope(400, notes, trace_id="00-t-2"))
    text = await call_tool_error(server_factory(registry=registry), "list_things")
    assert text == (
        "Gorelo rejected list_things (HTTP 400, code 070101): status_ids: bad ids; page_size: bad size; "
        "client_id: bad client; ticket_id: bad ticket [trace 00-t-2]"
    )


# --------------------------------------------------------------------------
# Conventions that every registered tool must follow (vacuous until tool modules add tools)
# --------------------------------------------------------------------------

STUB_OP = re.compile(r"\b(?:GET|POST|PATCH|PUT|DELETE) /v1/[^\s,;)]+")


def test_every_op_named_in_a_tool_module_docstring_exists_in_the_spec(spec_index):
    seen = 0
    # a docstring may also name a published operation that spec/live_overrides.json replaced (to say why it is not used)
    known = set(spec_index.ops) | set(spec_index.override_ops.values())
    for path in sorted((REPO_ROOT / "tools").glob("*.py")):
        if path.name.startswith("_"):
            continue
        doc = __import__(f"tools.{path.stem}", fromlist=["x"]).__doc__ or ""
        named = [op.rstrip(".") for op in STUB_OP.findall(doc)]
        for op in named:
            assert op in known, f"{path.name} names {op}, which is not in the spec index"
        if any(is_forbidden_op(op) for op in named):
            assert "FORBIDDEN_OPS" in doc, f"{path.name} names a forbidden op without saying it is forbidden"
        seen += len(named)
    assert seen > 0, "no tool module docstring names an operation"


def test_all_tool_modules_are_imported_by_the_package():
    names = {p.stem for p in (REPO_ROOT / "tools").glob("*.py") if not p.name.startswith("_")}
    assert names <= set(dir(tools)), names - set(dir(tools))


def _registered_tools():
    return REGISTRY.specs or [None]  # None: nothing is registered yet, the test then has nothing to check


@pytest.mark.parametrize("spec", _registered_tools(), ids=lambda s: "no-tools-registered-yet" if s is None else s.name)
def test_registered_tool_follows_the_house_conventions(spec, spec_index):
    if spec is None:
        return
    where = f"tool {spec.name} ({spec.fn.__module__})"
    assert re.fullmatch(r"[a-z][a-z0-9_]*", spec.name), f"{where}: tool names are snake_case"
    assert spec.toolset in TOOLSETS and spec.kind in ("read", "write", "destructive")
    for op in spec.ops:
        assert op in spec_index.ops, f"{where}: {op} is not in the spec index"
        assert not is_forbidden_op(op), f"{where}: {op} is forbidden"
        method = op.split(" ", 1)[0]
        if spec.kind == "read":
            assert method == "GET", f"{where} is a read tool but declares {op}"
            assert not is_side_effect_get(op), f"{where} is a read tool but {op} is recorded by Gorelo"  # by shape
        if method == "DELETE":
            assert spec.kind == "destructive", f"{where} declares {op} but is not destructive"
    if spec.kind == "destructive":
        assert spec.destructive_hint is True, where
    if spec.kind == "read":
        assert spec.destructive_hint is False, where
    doc = inspect.getdoc(spec.fn) or ""
    assert doc.strip(), f"{where} needs a docstring: it is the prompt claude.ai sees"
    schema = Tool.from_function(spec.fn).parameters
    for param, definition in schema.get("properties", {}).items():
        assert param != "ctx" and re.fullmatch(r"[a-z][a-z0-9_]*", param), f"{where}: parameter {param} must be snake_case"
        assert definition.get("description"), f"{where}: parameter {param} needs a Field(description=...)"
    if spec.kind == "destructive":
        assert "confirm" in schema["properties"] and schema["properties"]["confirm"].get("default") is False, where


# --------------------------------------------------------------------------
# The real destructive tools: confirm is strict
# --------------------------------------------------------------------------

REAL_DESTRUCTIVE_ARGUMENTS = {
    "delete_ticket_comment": {"ticket_id": uid(1), "comment_id": uid(2)},
    "delete_time_entry": {"time_entry_id": 55},
    "delete_invoice": {"invoice_number": 1042, "expected_status": "Draft"},
    # not a delete, but gated by the same flag: approving on create pushes the invoice to accounting (contract e15cb5a18ec2)
    "create_approved_invoice": {"client_id": 9102, "line_items": [{"item_id": uid(8), "quantity": 1}]},
    "delete_item": {"item_id": uid(3)},
    "delete_uptime_check": {"check_id": uid(4)},
    "delete_project_comment": {"project_id": uid(5), "comment_id": uid(6)},
    "delete_project_task": {"project_id": uid(5), "task_id": uid(7)},
}


def test_the_table_of_real_destructive_tools_is_complete():
    # a new destructive tool must be added here so that its confirm is exercised too
    assert {spec.name for spec in REGISTRY.specs if spec.kind == "destructive"} == set(REAL_DESTRUCTIVE_ARGUMENTS)


@pytest.mark.parametrize("name", sorted(REAL_DESTRUCTIVE_ARGUMENTS))
async def test_a_real_destructive_tool_refuses_text_and_numbers_for_confirm_and_sends_nothing(
    server_factory, mock_gorelo, name
):
    server = server_factory(toolsets=frozenset(TOOLSETS), destructive=True)
    async with Client(server) as session:
        for confirm in ("true", "True", "yes", "on", "1", 1, 0, 1.0, None, "false", [True]):
            text = await call_tool_error(session, name, {**REAL_DESTRUCTIVE_ARGUMENTS[name], "confirm": confirm})
            assert "confirm" in text and "valid boolean" in text, (name, confirm, text)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("name", sorted(REAL_DESTRUCTIVE_ARGUMENTS))
async def test_a_real_destructive_tool_still_refuses_a_missing_or_false_confirm_with_its_own_message(
    server_factory, mock_gorelo, name
):
    server = server_factory(toolsets=frozenset(TOOLSETS), destructive=True)
    async with Client(server) as session:
        for extra in ({}, {"confirm": False}):
            text = await call_tool_error(session, name, {**REAL_DESTRUCTIVE_ARGUMENTS[name], **extra})
            assert text.startswith("confirm: refusing to ") and "without confirm=true" in text, (name, text)
    assert mock_gorelo.requests == []


@pytest.mark.parametrize("name", sorted(REAL_DESTRUCTIVE_ARGUMENTS))
def test_every_real_destructive_tool_declares_a_strict_false_default_confirm(name):
    (spec,) = [s for s in REGISTRY.specs if s.name == name]
    schema = Tool.from_function(spec.fn).parameters["properties"]["confirm"]
    assert schema["type"] == "boolean" and schema["default"] is False and schema["description"]
    parameter = inspect.signature(spec.fn).parameters["confirm"]
    assert parameter.default is False
    assert "StrictBool" in repr(parameter.annotation) or "Strict(" in repr(parameter.annotation)


# --------------------------------------------------------------------------
# Test infrastructure: the FastMCP loggers are quiet during a test
# --------------------------------------------------------------------------


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_the_fastmcp_and_mcp_loggers_are_at_critical_during_a_test():
    for name in (*QUIET_LOGGERS, "fastmcp.server.server", "fastmcp.tools.tool", "mcp.server.lowlevel.server"):
        logger = logging.getLogger(name)
        assert logger.getEffectiveLevel() == logging.CRITICAL, name
        assert not logger.isEnabledFor(logging.ERROR), name
    assert QUIET_LOGGERS == ("fastmcp", "mcp")


async def test_a_rejected_tool_call_logs_nothing_through_fastmcp_while_the_loggers_are_quiet():
    logger = logging.getLogger("fastmcp")
    handler = _Collect()
    logger.addHandler(handler)
    try:
        server = FastMCP("t")

        @server.tool
        async def needs_a_number(value: int) -> dict:
            """Needs a number."""
            return {}

        assert (await call_tool_raw(server, "needs_a_number", {"value": "x"})).is_error
        assert handler.records == []  # no Rich traceback was rendered for it
        logger.setLevel(logging.ERROR)  # the control: the same call does log once the test lowers the level
        assert (await call_tool_raw(server, "needs_a_number", {"value": "x"})).is_error
        assert handler.records, "FastMCP logged nothing about a rejected call: this test no longer proves anything"
    finally:
        logger.removeHandler(handler)


def test_a_test_can_lower_the_level_itself_and_the_records_then_flow():
    logger = logging.getLogger("fastmcp")
    handler = _Collect()
    logger.addHandler(handler)
    try:
        logging.getLogger("fastmcp.server.server").error("dropped while quiet")
        assert handler.records == []
        logger.setLevel(logging.ERROR)  # the fixture still restores the previous level at teardown
        logging.getLogger("fastmcp.server.server").error("visible once lowered")
        assert [r.getMessage() for r in handler.records] == ["visible once lowered"]
    finally:
        logger.removeHandler(handler)


def test_quiet_loggers_restores_every_previous_level_also_after_an_error_or_a_change_inside():
    names = ["gtest_quiet_a", "gtest_quiet_b", "gtest_quiet_b.child"]
    first, second, child = (logging.getLogger(name) for name in names)
    first.setLevel(logging.INFO)
    second.setLevel(logging.NOTSET)
    child.setLevel(logging.DEBUG)
    try:
        with quiet_loggers(names):
            assert (first.level, second.level, child.level) == (logging.CRITICAL,) * 3
            first.setLevel(logging.ERROR)  # a body that changes the level itself
        assert (first.level, second.level, child.level) == (logging.INFO, logging.NOTSET, logging.DEBUG)
        with pytest.raises(RuntimeError, match="boom"):
            with quiet_loggers(names):
                raise RuntimeError("boom")
        assert (first.level, second.level, child.level) == (logging.INFO, logging.NOTSET, logging.DEBUG)
        with quiet_loggers(names, level=logging.ERROR):
            assert first.level == logging.ERROR
        assert first.level == logging.INFO
    finally:
        for logger in (first, second, child):
            logger.setLevel(logging.NOTSET)


def test_nested_quiet_loggers_unwind_in_order():
    logger = logging.getLogger("gtest_quiet_nested")
    logger.setLevel(logging.WARNING)
    try:
        with quiet_loggers(["gtest_quiet_nested"]):
            with quiet_loggers(["gtest_quiet_nested"], level=logging.ERROR):
                assert logger.level == logging.ERROR
            assert logger.level == logging.CRITICAL
        assert logger.level == logging.WARNING
    finally:
        logger.setLevel(logging.NOTSET)


def test_the_modules_that_assert_on_fastmcps_own_log_records_are_exempt_and_exist():
    assert "test_log_filter" in LOGGING_TEST_MODULES
    for name in LOGGING_TEST_MODULES:
        assert (REPO_ROOT / "tests" / f"{name}.py").is_file(), name
