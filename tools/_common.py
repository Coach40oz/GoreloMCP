"""Shared plumbing for every tool module: the @gorelo_tool decorator, the registry, parameter
helpers, result shapes and error formatting.

Tool modules import from here and must not add shared helpers to it.

The tool pattern (see CONTRIBUTING.md for the full conventions):

    FIELD_MAP = {"name": "Name", "location_phone": "Location.Phone"}

    @gorelo_tool(toolset="core", kind="write", ops=["POST /v1/clients"], field_map=FIELD_MAP)
    async def create_client(
        ctx: Context,
        name: Annotated[str, Field(description="Client name.")],
        location_phone: Annotated[str | None, Field(description="Digits only.")] = None,
    ) -> dict:
        '''Create a client ... (docstring template: what it does, ids to resolve, side effects).'''
        non_empty("name", name)
        body = build_body({"name": name, "location_phone": location_phone}, FIELD_MAP)
        return await client_of(ctx).post("POST /v1/clients", json_body=body, tool="create_client")

Typing a tool signature (ids and confirm are STRICT, so JSON true, "5" or 5.0 never turn into an id):

    client_id: Annotated[StrictId, Field(description="Client id (list_clients).")],
    status_ids: Annotated[list[StrictId] | None, Field(description="Status ids (list_ticket_statuses).")] = None,
    confirm: Annotated[StrictBool, Field(description="Must be true to delete. Ask the user first.")] = False,

then call positive_id("client_id", client_id) / positive_ids(...) / guid(...) / guids(...) in the body for
the range checks and the error text that names the snake_case parameter (see "Id validators" below).
StrictId and StrictBool are pydantic's Annotated[int, Strict()] and Annotated[bool, Strict()]: the JSON
schema is the same as for int and bool, only the validation is stricter.

What the decorator does:

* Registers the tool in REGISTRY (or in a Registry you build for a test) under its name.
* field_map maps each snake_case parameter to its Gorelo name. For a write tool those are the body field
  paths ("Location.Phone") that build_body() uses. The same map turns a Gorelo PropertyName back into the
  parameter to fix when an error is shown, and for that purpose it may ALSO map query parameter names
  ("StatusIds", "PageSize") and path placeholders ("ticketId", "id"): read tools rely on this, so an
  error that names a query parameter or a placeholder reads "status_ids: ..." and not "StatusIds: ...".
  Entries that build_body() is not asked about cost nothing, so one map can serve both purposes.
* Attaches FastMCP metadata: name, title, tags {toolset, kind} and ToolAnnotations (readOnlyHint,
  destructiveHint, idempotentHint, openWorldHint). destructive_hint decides destructiveHint: a read
  tool is never destructive, a destructive tool always is, and a write tool is not unless it passes
  destructive_hint=True (update_* and set_* tools that overwrite or clear existing data do). Registration
  with a FastMCP server is done by server.build_server, which also gates destructive tools and checks
  every op against the spec.
* Wraps the function so a GoreloAPIError, a SpecViolation or a helper ValueError reaches the model as a
  fastmcp ToolError with a useful message (Gorelo errors name the snake_case parameter to fix). The
  same goes for an ExceptionGroup (asyncio.TaskGroup) whose leaves include one of those: a leaf that is
  an unconfirmed write (GoreloAPIError.write_unconfirmed) is translated in preference to the others,
  otherwise the first such leaf, and the number of other errors is said in the message ("fix this one
  and call again", or, when ANY leaf is an unconfirmed write, "at least one write may have been
  applied, so verify with a read before calling again"). The wrapper copies the
  signature, annotations and docstring (functools.wraps), so the JSON schema FastMCP generates is
  identical to the undecorated function's: same parameter names, types, Field descriptions and
  defaults, and the `ctx: Context` parameter never appears in it.
* Translates errors ONLY at the outermost tool. While the function runs, the wrapper puts the tool's
  ToolSpec in gorelo_client.CURRENT_TOOL. A decorated tool called while another tool is running does
  not translate anything: its exception reaches the outer tool unchanged, and the outer tool turns it
  into the one ToolError the model sees (with the outer tool's name and field map). NEVER call a
  decorated tool from another tool: use client_of(ctx) for HTTP and reread_after_write() to read a
  record back after a write.
* Restricts the operations a running tool can send to the ones in its ops=[...]: GoreloClient refuses
  any other operation (GoreloAPIError kind "spec", no HTTP call). Code that runs outside a tool
  (tests, scripts) is not restricted.
* For kind="destructive" it requires a boolean `confirm` parameter whose generated schema default is
  False (so Field(default=True), a required confirm or a non-boolean confirm are refused at decoration)
  and that is STRICT: declare it as `confirm: Annotated[StrictBool, Field(description=...)] = False`
  (or Annotated[bool, Strict(), Field(...)]). A lax bool would let the text "true" or the number 1 count
  as confirmation, so the decorator checks that pydantic rejects both and refuses the tool otherwise.
  A kind="read" tool may not declare an operation that gorelo_client.is_side_effect_get recognises (the
  invoice PDF export is recorded as an event, so it is a write). It compares by SHAPE, like FORBIDDEN_OPS:
  every {placeholder} counts the same, whatever Gorelo calls it.
"""

from __future__ import annotations

import functools
import inspect
import logging
import re
import typing
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextvars import Token
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypeVar

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from fastmcp.tools import tool as _fastmcp_tool
from mcp.types import ToolAnnotations
from pydantic import Strict, TypeAdapter, ValidationError

from gorelo_client import (
    CURRENT_TOOL,
    EXPORT_NOTE,
    PAGE_SIZE_MAX,
    PAGE_SIZE_MIN,
    AllResult,
    GoreloAPIError,
    GoreloClient,
    Page,
    is_side_effect_get,
)
from settings import TOOLSETS
from spec import SpecViolation

logger = logging.getLogger("gorelo-mcp.tools")

ToolKind = Literal["read", "write", "destructive"]
KINDS: tuple[str, ...] = ("read", "write", "destructive")

# claude.ai accepts tool names matching [a-zA-Z0-9_-]{1,64}
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_REGION = re.compile(r"[A-Z]{2}")
_UUID_HYPHENATED = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_UUID_BARE = re.compile(r"[0-9a-fA-F]{32}")
SCOPE_CODE = "080203"

# Gorelo numeric ids are int64 and start at 1.
MAX_ID = 2**63 - 1
GUID_EXAMPLE = "3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b"

# Strict types for tool signatures. pydantic's Strict() makes FastMCP refuse what the default lax mode
# coerces: JSON true for an id (lax int turns it into 1), the string "5" or the number 5.0 for an id, and
# the text "true" or the number 1 for confirm. The JSON schema is unchanged (integer / boolean), so
# Annotated[StrictId, Field(description=...)] and list[StrictId] behave like int and list[int] for the
# model, and only the validation is stricter. Usage: see the module docstring.
StrictId = Annotated[int, Strict()]
StrictBool = Annotated[bool, Strict()]

# Said by created_id() and expect_object() when a write succeeded but its answer cannot be used. The
# shape branch of format_gorelo_error() does not repeat its own "verify" sentence when this one is there.
_VERIFY_NOTE = "verify it with a read before repeating it"

F = TypeVar("F", bound=Callable[..., Any])


# --------------------------------------------------------------------------
# Registry and the @gorelo_tool decorator
# --------------------------------------------------------------------------


class RegistryError(ValueError, RuntimeError):
    """A tool was declared wrongly (bad toolset or kind, missing confirm, duplicate name...)."""


@dataclass(frozen=True)
class ToolSpec:
    """One registered tool. `fn` is the wrapped function carrying the FastMCP metadata.

    While a tool runs its ToolSpec is gorelo_client.CURRENT_TOOL: the client reads `name` and `ops`
    from it to refuse operations the tool did not declare. `destructive_hint` is the value of the
    destructiveHint annotation.
    """

    name: str
    fn: Callable[..., Any]
    toolset: str
    kind: str
    ops: list[str]
    field_map: dict[str, str]
    destructive_hint: bool = False


class Registry:
    """The set of declared tools. REGISTRY is the real one; tests build their own."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    @property
    def specs(self) -> list[ToolSpec]:
        """Every registered tool, in registration order."""
        return list(self._specs.values())

    def select(self, toolsets: Iterable[str], destructive: bool) -> list[ToolSpec]:
        """The tools to register on a server: in one of `toolsets`, and kind != destructive
        unless `destructive` is true."""
        wanted = set(toolsets)
        return [
            spec
            for spec in self._specs.values()
            if spec.toolset in wanted and (destructive or spec.kind != "destructive")
        ]

    def tool(
        self,
        *,
        toolset: str,
        kind: ToolKind,
        ops: list[str],
        field_map: dict[str, str] | None = None,
        name: str | None = None,
        title: str | None = None,
        destructive_hint: bool | None = None,
    ) -> Callable[[F], F]:
        """Decorator factory. See the module docstring. Raises RegistryError on a bad declaration.

        field_map maps snake_case parameter names to Gorelo names: body field paths for a write tool
        ("Location.Phone", used by build_body) and, for any tool, query parameter names ("StatusIds")
        and path placeholders ("ticketId", "id"). Error messages use it to show a Gorelo PropertyName
        as the snake_case parameter to fix, and read tools rely on that for their query and path
        parameters. An entry that build_body is never asked about is harmless.

        destructive_hint sets the MCP destructiveHint annotation. None means the default for the
        kind: False for "write", True for "destructive". A "write" tool that overwrites or clears
        data that already exists (update_*, set_*) passes True. A "read" tool can only be False and
        a "destructive" tool can only be True.

        A "destructive" tool must declare `confirm: Annotated[StrictBool, Field(description=...)] = False`
        (StrictBool is defined in this module): confirm must be a strict boolean that defaults to False.
        """
        if toolset not in TOOLSETS:
            raise RegistryError(f"toolset must be one of {', '.join(TOOLSETS)}, got {toolset!r}")
        if kind not in KINDS:
            raise RegistryError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
        if isinstance(ops, str) or not isinstance(ops, (list, tuple)) or not ops:
            raise RegistryError("ops must be a non-empty list of operation keys such as 'GET /v1/clients'")
        if not all(isinstance(op, str) and op.strip() for op in ops):
            raise RegistryError(f"ops must be non-empty strings, got {ops!r}")
        mapping = dict(field_map or {})
        if not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in mapping.items()):
            raise RegistryError(
                "field_map must map snake_case parameter names to non-empty Gorelo names "
                "(body field paths, query parameter names or path placeholders)"
            )
        op_list = list(ops)
        hint = _resolve_destructive_hint(kind, destructive_hint)
        if kind == "read":
            side_effects = [op for op in op_list if is_side_effect_get(op)]
            if side_effects:
                raise RegistryError(
                    f"a read tool cannot declare {', '.join(side_effects)}: Gorelo records that call as an "
                    "event, so the tool is not read-only; declare it with kind='write'"
                )

        def decorator(fn: F) -> F:
            tool_name = name or getattr(fn, "__name__", "")
            if not _TOOL_NAME.fullmatch(tool_name):
                raise RegistryError(
                    f"tool name {tool_name!r} must match [A-Za-z0-9_-] and be 1 to 64 characters long"
                )
            if tool_name in self._specs:
                raise RegistryError(f"duplicate tool name {tool_name!r}: it is already registered")
            cell = _SpecCell()
            wrapper = _translate_errors(fn, tool_name, mapping, cell)
            if kind == "destructive":
                _require_confirm_parameter(wrapper, tool_name)
            annotations = ToolAnnotations(
                readOnlyHint=kind == "read",
                destructiveHint=hint,
                idempotentHint=kind == "read",
                openWorldHint=True,
            )
            decorated = _fastmcp_tool(
                name=tool_name, title=title, tags={toolset, kind}, annotations=annotations
            )(wrapper)
            if not hasattr(decorated, "__fastmcp__"):
                raise RegistryError(
                    "FastMCP returned a tool object instead of the function "
                    "(FASTMCP_DECORATOR_MODE=object is not supported); unset it"
                )
            cell.spec = self._specs[tool_name] = ToolSpec(
                name=tool_name, fn=decorated, toolset=toolset, kind=kind, ops=op_list, field_map=mapping,
                destructive_hint=hint,
            )
            return decorated  # type: ignore[return-value]

        return decorator


REGISTRY = Registry()
gorelo_tool = REGISTRY.tool


def _resolve_destructive_hint(kind: str, requested: bool | None) -> bool:
    if requested is not None and not isinstance(requested, bool):
        raise RegistryError(f"destructive_hint must be True, False or None, got {requested!r}")
    if kind == "read":
        if requested:
            raise RegistryError("a read tool cannot be destructive: destructive_hint must be None or False")
        return False
    if kind == "destructive":
        if requested is False:
            raise RegistryError("a destructive tool always has destructive_hint True: pass True or leave it out")
        return True
    return bool(requested)


_CONFIRM_FORM = "confirm: Annotated[StrictBool, Field(description=...)] = False"

# What a lax boolean turns into True although it is not one: the text and the number a model might send.
_LAX_CONFIRM_SAMPLES: tuple[Any, ...] = ("true", "yes", 1)


def _require_confirm_parameter(fn: Callable[..., Any], tool_name: str) -> None:
    """A destructive tool must expose `confirm` as a STRICT boolean that defaults to False.

    Checked on the parameter schema FastMCP generates, not on the Python default, because
    `confirm: bool = Field(default=True)` and `confirm: Annotated[bool, Field(default=True)]` have no
    Python default of True (or none at all) yet default to True for the model. A confirm without a
    default is refused too: the model could not tell that omitting it is safe.

    Strict means pydantic refuses the text "true" and the number 1 for it (a lax bool accepts both, so a
    model that sends confirm="true" would confirm a delete). That is checked by validating those samples
    against the very annotation FastMCP will use, so every spelling counts: StrictBool,
    Annotated[bool, Strict(), ...], Field(strict=True), or a default of Field(default=False, strict=True).
    """
    missing = (
        f"destructive tool {tool_name!r} must declare a strict boolean 'confirm' parameter that defaults to "
        f"False, written as '{_CONFIRM_FORM}' (StrictBool comes from tools._common), "
        "and call require_confirm() before any HTTP call"
    )
    if "confirm" not in inspect.signature(fn).parameters:
        raise RegistryError(missing)
    try:
        properties = Tool.from_function(fn).parameters.get("properties") or {}
    except Exception as exc:  # FastMCP could not build a schema: the real registration would fail too
        raise RegistryError(
            f"destructive tool {tool_name!r}: FastMCP cannot build its parameter schema: {exc}"
        ) from exc
    schema = properties.get("confirm")
    if not isinstance(schema, dict):
        raise RegistryError(missing)
    if schema.get("type") != "boolean":
        raise RegistryError(
            f"destructive tool {tool_name!r}: 'confirm' must be a boolean declared as '{_CONFIRM_FORM}' "
            f"(its schema is {schema})"
        )
    if schema.get("default") is not False:
        found = schema["default"] if "default" in schema else "no default"
        raise RegistryError(
            f"destructive tool {tool_name!r}: 'confirm' must default to False (its schema default is "
            f"{found!r}); declare it as '{_CONFIRM_FORM}'"
        )
    lax = _lax_confirm_samples(fn, tool_name)
    if lax:
        raise RegistryError(
            f"destructive tool {tool_name!r}: 'confirm' must be a strict boolean, because a lax bool also "
            f"accepts the text \"true\" or the number 1 as confirmation (pydantic accepted "
            f"{', '.join(repr(sample) for sample in lax)} for it). Declare it as '{_CONFIRM_FORM}' "
            "(StrictBool comes from tools._common; Annotated[bool, Strict(), Field(...)] and "
            "Field(strict=True) work too)"
        )


def _lax_confirm_samples(fn: Callable[..., Any], tool_name: str) -> list[Any]:
    """The samples in _LAX_CONFIRM_SAMPLES that pydantic accepts for fn's `confirm` parameter (empty: strict).

    A throwaway function with the same annotation and default is validated through TypeAdapter, which
    is how FastMCP validates a tool call (a TypeAdapter on the function), so the answer is the one a real
    call would get. The tool itself is never called.
    """
    parameter = inspect.signature(fn).parameters["confirm"]
    try:
        annotation = typing.get_type_hints(fn, include_extras=True).get("confirm", parameter.annotation)
    except Exception:  # an annotation that cannot be resolved here: let pydantic judge what is written
        annotation = parameter.annotation

    def probe(confirm: Any) -> Any:
        return confirm

    probe.__annotations__ = {"confirm": annotation}
    probe.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(
                "confirm", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=parameter.default, annotation=annotation
            )
        ]
    )
    try:
        adapter = TypeAdapter(probe)
    except Exception as exc:
        raise RegistryError(
            f"destructive tool {tool_name!r}: cannot check that 'confirm' is strict: {exc}; "
            f"declare it as '{_CONFIRM_FORM}'"
        ) from exc
    accepted: list[Any] = []
    for sample in _LAX_CONFIRM_SAMPLES:
        try:
            adapter.validate_python({"confirm": sample})
        except ValidationError:
            continue
        accepted.append(sample)
    return accepted


class _SpecCell:
    """Holds the ToolSpec the wrapper publishes in CURRENT_TOOL. Filled once the tool is registered."""

    __slots__ = ("spec",)

    def __init__(self) -> None:
        self.spec: ToolSpec | None = None


def _enter_tool(cell: _SpecCell) -> Token[Any] | None:
    """Mark this tool as the running one, unless a tool is already running (then None: nested call)."""
    if CURRENT_TOOL.get() is not None:
        return None
    if cell.spec is None:  # cannot happen: the cell is filled before the decorator returns
        raise RuntimeError("a tool was called before its registration finished")
    return CURRENT_TOOL.set(cell.spec)


def _translate_errors(
    fn: Callable[..., Any], tool_name: str, field_map: dict[str, str], cell: _SpecCell
) -> Callable[..., Any]:
    """Wrap fn so Gorelo, spec and validation errors become ToolError, at the outermost tool only.

    The signature is preserved. A tool called while another tool is running re-raises whatever
    happened untouched: the outer tool owns the message (its name, its field map) and the declared
    operations (CURRENT_TOOL stays the outer tool's).
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            token = _enter_tool(cell)
            if token is None:
                return await fn(*args, **kwargs)
            try:
                return await fn(*args, **kwargs)
            except (GoreloAPIError, SpecViolation, ValueError) as err:
                raise _tool_error(err, tool_name, field_map) from err
            except BaseExceptionGroup as group:
                translated = _tool_error_from_group(group, tool_name, field_map)
                if translated is None:
                    raise
                raise translated from group
            finally:
                CURRENT_TOOL.reset(token)

        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        token = _enter_tool(cell)
        if token is None:
            return fn(*args, **kwargs)
        try:
            return fn(*args, **kwargs)
        except (GoreloAPIError, SpecViolation, ValueError) as err:
            raise _tool_error(err, tool_name, field_map) from err
        except BaseExceptionGroup as group:
            translated = _tool_error_from_group(group, tool_name, field_map)
            if translated is None:
                raise
            raise translated from group
        finally:
            CURRENT_TOOL.reset(token)

    return sync_wrapper


def _error_text(err: Exception, tool_name: str, field_map: Mapping[str, str]) -> str:
    if isinstance(err, GoreloAPIError):
        return format_gorelo_error(err, tool_name, field_map)
    return str(err)


def _tool_error(err: Exception, tool_name: str, field_map: Mapping[str, str]) -> ToolError:
    return ToolError(_error_text(err, tool_name, field_map))


def _leaves(group: BaseExceptionGroup) -> list[BaseException]:
    found: list[BaseException] = []
    for item in group.exceptions:
        if isinstance(item, BaseExceptionGroup):
            found.extend(_leaves(item))
        else:
            found.append(item)
    return found


def _tool_error_from_group(
    group: BaseExceptionGroup, tool_name: str, field_map: Mapping[str, str]
) -> ToolError | None:
    """The ToolError for an ExceptionGroup (asyncio.TaskGroup) that holds a Gorelo, spec or value
    error, or None to let the group propagate untouched.

    Which leaf is translated: the first GoreloAPIError that is an unconfirmed write
    (write_unconfirmed=True, depth first) if there is one, otherwise the first Gorelo, spec or value
    error. The unconfirmed write wins whatever its position, because it is the one the model must not
    lose: showing a plain validation error first would invite "fix this one and call again", which
    repeats a write that may already have been applied. The number of other errors in the group is
    appended; when ANY leaf is an unconfirmed write the advice is to verify with a read before calling
    again, otherwise to fix the one shown and call again.

    A group that holds a leaf that is not an Exception (a cancellation, KeyboardInterrupt) is never
    translated: control flow must not be swallowed.
    """
    leaves = _leaves(group)
    if not all(isinstance(leaf, Exception) for leaf in leaves):
        return None
    unconfirmed = [leaf for leaf in leaves if isinstance(leaf, GoreloAPIError) and leaf.write_unconfirmed]
    translatable = [leaf for leaf in leaves if isinstance(leaf, (GoreloAPIError, SpecViolation, ValueError))]
    chosen = unconfirmed[0] if unconfirmed else translatable[0] if translatable else None
    if chosen is None:
        return None
    text = _error_text(chosen, tool_name, field_map)
    others = len(leaves) - 1
    if others:
        noun = "other errors were" if others > 1 else "other error was"
        advice = (
            "at least one write may have been applied, so verify with a read before calling again"
            if unconfirmed
            else "fix this one and call again"
        )
        text += f" ({others} {noun} raised at the same time; {advice})"
    return ToolError(text)


# --------------------------------------------------------------------------
# Lifespan access
# --------------------------------------------------------------------------


def _lifespan_dict(ctx: Context) -> Mapping[str, Any]:
    state = getattr(ctx, "lifespan_context", None)
    return state if isinstance(state, Mapping) else {}


def client_of(ctx: Context) -> GoreloClient:
    """The shared GoreloClient the server lifespan created (one per server process)."""
    client = _lifespan_dict(ctx).get("gorelo")
    if client is None:
        raise RuntimeError(
            "the Gorelo client is not available: the server lifespan did not provide ctx.lifespan_context"
            "['gorelo']. Build the server with server.build_server()."
        )
    return client


def server_info_of(ctx: Context) -> dict[str, Any]:
    """What the lifespan exposes besides the client, for diagnostics such as health_check:
    {"toolsets": sorted list or None, "destructive": bool or None, "spec_sha256": str or None}."""
    state = _lifespan_dict(ctx)
    spec = state.get("spec")
    return {
        "toolsets": state.get("toolsets"),
        "destructive": state.get("destructive"),
        "spec_sha256": getattr(spec, "sha256", None),
    }


# --------------------------------------------------------------------------
# Reading a record back after a write
# --------------------------------------------------------------------------


async def reread_after_write(
    ctx: Context,
    op_key: str,
    *,
    path_params: Mapping[str, Any],
    tool: str,
    written_id: Any,
) -> dict[str, Any]:
    """GET the record a write just created or changed and return it. A failed GET is returned, not raised.

    Many writes answer with only {"Id": ...}. The tool then reads the record back with `op_key` (a
    GET that the tool must also declare in ops=[...]) and returns it. If that read fails AFTER the
    write succeeded, raising would tell the model the write failed and it would repeat it. So the
    failure is returned instead, as {"Id": written_id, "warning": "the write succeeded; re-reading it
    failed: <why the read failed>. Do not repeat the write; read it again later."}. Any GoreloAPIError
    or SpecViolation of the read counts, including a bad `op_key` or a malformed id.

    <why the read failed> describes the READ and never the tool: it names `op_key` and says what
    Gorelo answered (HTTP status, code, message, trace id when there is one), or that the read timed
    out or the connection failed. It is deliberately not format_gorelo_error() text: that text speaks
    about the tool ("Gorelo rejected create_ticket") and, for a read that timed out, says "retrying is
    safe". Both would mislead the model about a tool whose write has already been applied. `tool` only
    labels the log line of the read.

    Call it from a tool as `await reread_after_write(ctx, "GET /v1/tickets/{ticketId}",
    path_params={"ticketId": new_id}, tool="create_ticket", written_id=new_id)`. NEVER call a
    decorated tool from another tool to do the read: use this helper (or client_of(ctx)) instead, so
    the outer tool stays the only place that translates errors.
    """
    client = client_of(ctx)
    try:
        record = await client.get_one(op_key, path_params=path_params, tool=tool)
    except GoreloAPIError as err:
        return _reread_warning(written_id, _reread_failure(err, op_key))
    except SpecViolation as err:
        return _reread_warning(written_id, str(err))
    if not isinstance(record, dict):
        return _reread_warning(
            written_id, f"{op_key} returned {_data_kind(record)} instead of the record"
        )
    return record


def _reread_warning(written_id: Any, reason: str) -> dict[str, Any]:
    return {
        "Id": written_id,
        "warning": (
            f"the write succeeded; re-reading it failed: {reason.rstrip('. ')}. "
            "Do not repeat the write; read it again later."
        ),
    }


def _reread_failure(err: GoreloAPIError, op_key: str) -> str:
    """Why a re-read failed, worded about the READ (see reread_after_write).

    Never goes through format_gorelo_error() and never receives the tool's name, so no variant can say
    "Gorelo rejected <tool>" or "retrying is safe". It names the operation that was read, says what
    happened to it, and gives no advice about retrying: the warning around it already says what to do.
    A write_unconfirmed flag is ignored on purpose: it describes a write, and this was a read.
    """
    where = err.op_key or op_key
    kind = err.kind
    trace = f" [trace {err.trace_id}]" if err.trace_id else ""
    if kind == "timeout":
        return f"{where} timed out"
    if kind == "transport":
        return f"the connection to Gorelo failed during {where}"
    if kind == "rate_limit":
        return f"{where} was rate limited by Gorelo (HTTP 429){trace}"
    if kind in ("shape", "spec", "forbidden"):
        # The client's own one-line message. It names the operation and what was wrong with the
        # answer; for an operation the tool did not declare it says which declaration to fix.
        return f"{err}{trace}"
    # "http" and "envelope": Gorelo answered with an error. No field map: a read has no body to map.
    answer = f"answered HTTP {err.status}" if err.status is not None else "failed"
    codes = _distinct_codes(err)
    head = f"{where} {answer}" + (f" (code {'/'.join(codes)})" if codes else "")
    return f"{head}: {_notification_detail(err, {})}{trace}"


def _data_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    return type(value).__name__


# --------------------------------------------------------------------------
# Parameter helpers (each raises ValueError naming the snake_case parameter)
# --------------------------------------------------------------------------


def require_confirm(confirm: bool, *, action: str, effect: str | None = None) -> None:
    """Refuse a destructive action unless confirm is exactly True. Call before any HTTP.

    `action` completes "refusing to <action>" (for example "delete time entry 55"); `effect` is an
    optional sentence saying what will happen (what is removed, what cannot be undone).
    """
    if confirm is True:
        return
    consequence = f" {effect.strip()}" if effect and effect.strip() else ""
    raise ValueError(
        f"confirm: refusing to {action} without confirm=true.{consequence} "
        f"Call again with confirm=true if you really want to {action}."
    )


def csv_ids(param: str, values: Any) -> str | None:
    """A list of ids (ints or strings) as one comma separated string. None stays None; [] is an error."""
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError(f"{param}: expected a list of ids such as [1, 2, 3], got {type(values).__name__}")
    if not values:
        raise ValueError(f"{param}: the list must contain at least one id (omit {param} to apply no filter)")
    parts: list[str] = []
    for item in values:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise ValueError(f"{param}: ids must be integers or strings, got {item!r}")
        text = str(item).strip()
        if not text:
            raise ValueError(f"{param}: ids must not be blank")
        if "," in text:
            raise ValueError(f"{param}: a value must not contain a comma (got {item!r}); the list is sent comma separated")
        parts.append(text)
    return ",".join(parts)


def utc_iso(param: str, value: str | datetime | None) -> str | None:
    """An ISO 8601 datetime WITH an offset, converted to UTC and written as ...Z. Naive is an error."""
    if value is None:
        return None
    example = "2026-10-01T14:30:00Z or 2026-10-01T09:30:00-05:00"
    if isinstance(value, datetime):
        moment = value
    else:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{param}: expected an ISO 8601 datetime with a UTC offset such as {example}")
        try:
            moment = datetime.fromisoformat(value.strip())
        except ValueError:
            raise ValueError(
                f"{param}: {value!r} is not an ISO 8601 datetime; use a UTC offset, for example {example}"
            ) from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(
            f"{param}: {value!r} has no UTC offset. Add Z for UTC or an offset such as -05:00, "
            f"for example {example}; naive datetimes are rejected so nothing is guessed"
        )
    utc = moment.astimezone(timezone.utc)
    text = utc.isoformat(timespec="microseconds" if utc.microsecond else "seconds")
    return text.replace("+00:00", "Z")


def region_code(param: str, value: str | None) -> str | None:
    """A two letter ISO region code in capitals (US, CA). Dial codes (1, +1) are rejected."""
    if value is None:
        return None
    if not isinstance(value, str) or not _REGION.fullmatch(value):
        raise ValueError(
            f"{param}: expected a 2 letter ISO region code in capitals, got {value!r}; "
            "dial codes like 1 or +1 are not accepted (one dial code covers many regions), use e.g. US or CA"
        )
    return value


def non_empty(param: str, value: Any) -> Any:
    """Return value unchanged unless it is "", whitespace only or an empty list. None passes (not given)."""
    if value is None:
        return value
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{param}: must not be empty or whitespace only")
    if isinstance(value, (list, tuple, set, frozenset, dict)) and len(value) == 0:
        raise ValueError(f"{param}: must not be an empty list; omit it to leave the field unchanged")
    return value


def clamp_page_size(value: int) -> int:
    """Clamp a page size into 1..200 (Gorelo answers 400 outside that range, on every paged op)."""
    try:
        size = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"page_size: expected an integer from {PAGE_SIZE_MIN} to {PAGE_SIZE_MAX}, got {value!r}"
        ) from None
    return max(PAGE_SIZE_MIN, min(PAGE_SIZE_MAX, size))


# --------------------------------------------------------------------------
# Id validators (each raises ValueError naming the snake_case parameter, never quoting the value)
#
# Pair them with the strict signature types: a StrictId parameter already refuses JSON true, "5" and 5.0
# before the tool runs, and positive_id() then adds the range check and the message that names the
# parameter. Both are needed: Strict() cannot say "1 or more", and a direct call (a test, another
# helper) skips FastMCP's validation altogether.
# --------------------------------------------------------------------------


def describe_value(value: Any) -> str:
    """What a value is, in a few words, WITHOUT quoting it. Use it in messages instead of repr(value).

    "null", "a boolean", "a number", "a string" ("an empty string" for ""), "an object" ("an empty
    object"), "a list of 3 items" ("a list of 1 item", "an empty list"), "binary data", or
    "a value of type X". A caller's data can be a name, an email or a secret, so it is never echoed.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "an empty string" if not value else "a string"
    if isinstance(value, (bytes, bytearray)):
        return "binary data"
    if isinstance(value, Mapping):
        return "an empty object" if not value else "an object"
    if isinstance(value, (list, tuple)):
        count = len(value)
        if count == 0:
            return "an empty list"
        return f"a list of {count} item{'' if count == 1 else 's'}"
    return f"a value of type {type(value).__name__}"


def _id_problem(value: Any) -> str | None:
    """Why `value` is not a Gorelo numeric id (a "got ..." phrase that never quotes it), or None if it is one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return "got a decimal number" if isinstance(value, float) else f"got {describe_value(value)}"
    if value < 1:
        return "got zero or a negative number"
    if value > MAX_ID:
        return f"got a number above {MAX_ID}, the largest Gorelo id"
    return None


def positive_id(param: str, value: Any) -> int:
    """A Gorelo numeric id: a whole number from 1 to 2**63-1 (int64). Returns it as an int.

    Rejects None, a bool (True is an int to Python, and an id of 1 to a lax pydantic int), a float, a
    string such as "5", zero, a negative number and anything above int64. The message names `param`
    and describes what was given without quoting it. None is NOT passed through: for an optional
    parameter test `if value is not None` first.
    """
    problem = _id_problem(value)
    if problem is not None:
        raise ValueError(f"{param}: expected a positive whole number such as 123, {problem}")
    return int(value)


def positive_ids(param: str, values: Any) -> list[int] | None:
    """A list of Gorelo numeric ids, each checked like positive_id. None stays None; [] is an error.

    A bad item is reported as "param[index]: ..." (index counts from 0, like the JSON array the model
    sent). Order and duplicates are kept. A list or a tuple is accepted; a string, a number or an
    object is not. Returns a new list.
    """
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError(f"{param}: expected a list of ids such as [123, 456], got {describe_value(values)}")
    if not values:
        raise ValueError(
            f"{param}: expected at least one id, got an empty list (omit {param} if you have no ids to give)"
        )
    return [positive_id(f"{param}[{index}]", item) for index, item in enumerate(values)]


def guid(param: str, value: Any) -> str:
    """A GUID (UUID) as canonical lowercase hyphenated text, whatever case or form it was written in.

    Accepts the 8-4-4-4-12 hyphenated form or 32 hex digits without hyphens, in any case, and a
    uuid.UUID. Rejects braces, a urn:uuid: prefix, surrounding whitespace (nothing is trimmed or guessed),
    a number, None, a bool and every other text. The message names `param` and never quotes the value.
    None is NOT passed through: for an optional parameter test `if value is not None` first.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str) and (_UUID_HYPHENATED.fullmatch(value) or _UUID_BARE.fullmatch(value)):
        return str(uuid.UUID(value))
    what = "text that is not a GUID" if isinstance(value, str) and value else describe_value(value)
    raise ValueError(f"{param}: expected a GUID such as {GUID_EXAMPLE}, got {what}")


def guids(param: str, values: Any) -> list[str] | None:
    """A list of GUIDs, each checked like guid() and returned canonical. None stays None; [] is an error.

    A bad item is reported as "param[index]: ..." (index counts from 0). Order and duplicates are kept.
    Returns a new list.
    """
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError(f"{param}: expected a list of GUIDs, got {describe_value(values)}")
    if not values:
        raise ValueError(
            f"{param}: expected at least one GUID, got an empty list (omit {param} if you have none to give)"
        )
    return [guid(f"{param}[{index}]", item) for index, item in enumerate(values)]


# --------------------------------------------------------------------------
# Checking the Data of a successful answer
# --------------------------------------------------------------------------


def _usable_id(value: Any) -> bool:
    """An Id a write can answer with: a whole number of 1 or more (never a bool) or a non-blank string."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 1
    return isinstance(value, str) and bool(value.strip())


def _unusable_id_text(value: Any) -> str:
    """Why an Id that _usable_id refused is unusable, in words that never quote it."""
    if isinstance(value, str):
        return "blank"
    if isinstance(value, int) and not isinstance(value, bool):
        return "zero or negative"
    return "a decimal number" if isinstance(value, float) else describe_value(value)


def _is_write(op_key: str) -> bool:
    """True for every operation that may change something: not a GET, or a GET Gorelo records (the PDF export)."""
    method = op_key.split(" ", 1)[0].upper() if isinstance(op_key, str) else ""
    return method != "GET" or is_side_effect_get(op_key)


def _shape_error(op_key: str, detail: str, *, tool: str, write: bool) -> GoreloAPIError:
    """A 2xx answer whose Data is not what the operation promises. A write is flagged as unconfirmed."""
    logger.warning("tool=%s op=%s unusable Data in a successful answer: %s", tool, op_key, detail)
    suffix = f"; the write may have been applied, so {_VERIFY_NOTE}" if write else "; refusing to guess"
    return GoreloAPIError(
        f"{op_key}: {detail}{suffix}", status=200, op_key=op_key, kind="shape", write_unconfirmed=write
    )


def created_id(data: Any, op_key: str, *, tool: str) -> Any:
    """The Id that a successful write answered with, as Gorelo sent it (an int or a string).

    `data` is the Data of the answer. It must be an object with an "Id" that is a whole number of 1 or
    more or a non-blank string. Anything else (null, a list, an object without Id, a null, blank, zero
    or boolean Id) raises GoreloAPIError(kind="shape", write_unconfirmed=True): the write succeeded as
    far as Gorelo says but the record cannot be found or read back, so the message says it may have been
    applied and must be verified with a read before it is repeated (a repeat could create a duplicate).
    The message describes what came back without quoting it. `tool` labels the log line.
    """
    if isinstance(data, Mapping):
        if "Id" not in data:
            problem = "Data is an object without an Id"
        elif _usable_id(data["Id"]):
            return data["Id"]
        else:
            problem = f"Data.Id is {_unusable_id_text(data['Id'])}"
    else:
        problem = f"Data is {describe_value(data)}, not an object with an Id"
    raise _shape_error(
        op_key, f"Gorelo reported success but the answer carries no usable Id for the record ({problem})",
        tool=tool, write=True,
    )


def expect_object(data: Any, op_key: str, *, tool: str, allow_empty: bool = False) -> dict[str, Any]:
    """Data as a dict: it must be a non-empty object (an empty one too when allow_empty is true).

    Anything else raises GoreloAPIError(kind="shape") whose message says what came back (never quoting it)
    and "refusing to guess". For a write (any operation that is not a GET, and the PDF export) the error
    is also write_unconfirmed=True and says the write may have been applied and must be verified with a
    read before it is repeated: an answer that cannot be used does not prove the write did not happen.
    `tool` labels the log line.
    """
    if isinstance(data, Mapping) and (data or allow_empty):
        return data if isinstance(data, dict) else dict(data)
    wanted = "an object" if allow_empty else "a non-empty object"
    raise _shape_error(
        op_key, f"expected Data to be {wanted} but got {describe_value(data)}", tool=tool, write=_is_write(op_key)
    )


def _is_empty(value: Any) -> bool:
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return len(value) == 0
    return False


def build_body(
    values: Mapping[str, Any],
    field_map: Mapping[str, str],
    *,
    clear: Iterable[str] | None = (),
    clear_values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a request body from snake_case tool parameters.

    values:   {param: value}. None is omitted ("not given", leave unchanged). "", whitespace only and
              [] raise ValueError unless the param is listed in `clear`.
    field_map:{param: "PascalField"}; "Location.Phone" creates {"Location": {"Phone": ...}}.
    clear:    params the caller asked to clear: None (same as empty) or an iterable of param names.
              Each is sent as clear_values.get(param, ""), so use clear_values={"tax_id": 0} for
              fields Gorelo clears with 0, or {"x": []} for lists. A param in `clear` must not also
              carry a non-empty value. A single string is refused (it would clear one letter at a time).
    clear_values: {param: value sent when that param is cleared}. Every key must be in field_map (a
              misspelled key is a programming error and raises), but a key does not have to be in
              `clear`: a tool can keep ONE static map for all its clearable fields and pass it on every
              call. An entry for a param that is not being cleared in this call is simply not used.
    A param that is not in field_map raises ValueError (a programming error in the tool).
    """
    if clear is None:
        clear = ()
    if isinstance(clear, (str, bytes)):
        raise ValueError(
            "build_body: clear must be a list of parameter names such as ['alternate_name'], not a string"
        )
    clear_order = list(dict.fromkeys(clear))
    if not all(isinstance(param, str) for param in clear_order):
        raise ValueError("build_body: clear must contain parameter names (strings)")
    clear_set = set(clear_order)
    unknown = sorted(p for p in clear_set if p not in field_map)
    if unknown:
        raise ValueError(f"cannot clear unknown field(s) {', '.join(unknown)}; known: {', '.join(sorted(field_map))}")
    stray = sorted(str(k) for k in (clear_values or {}) if k not in field_map)
    if stray:
        raise ValueError(
            f"build_body: clear_values names {', '.join(stray)}, which is not in field_map "
            f"(known: {', '.join(sorted(field_map))}); programming error in the tool"
        )
    overrides = clear_values or {}
    body: dict[str, Any] = {}
    created: set[int] = set()

    def put(param: str, value: Any) -> None:
        parts = field_map[param].split(".")
        node = body
        for part in parts[:-1]:
            child = node.get(part)
            if child is None:
                child = node[part] = {}
                created.add(id(child))
            elif id(child) not in created:
                raise ValueError(f"build_body: field_map paths overlap at '{part}' (parameter {param})")
            node = child
        leaf = parts[-1]
        if leaf in node:
            if id(node[leaf]) in created:
                raise ValueError(f"build_body: field_map paths overlap at '{leaf}' (parameter {param})")
            raise ValueError(f"build_body: '{field_map[param]}' is set twice (parameter {param})")
        node[leaf] = value

    for param, value in values.items():
        if param not in field_map:
            raise ValueError(f"build_body: parameter {param!r} has no entry in field_map (programming error)")
        if param in clear_set:
            if value is not None and not _is_empty(value):
                raise ValueError(f"{param}: cannot be given a value and cleared in the same call")
            put(param, overrides.get(param, ""))
            continue
        if value is None:
            continue
        if _is_empty(value):
            # Worded for any tool, create or update: build_body does not know which HTTP method follows, nor
            # whether the field is required.
            raise ValueError(
                f"{param}: must not be empty or whitespace only; give it a real value, or leave it out if it is "
                "optional (where the tool offers an explicit option to clear the field, use that option instead)"
            )
        put(param, value)
    for param in clear_order:
        if param not in values:
            put(param, overrides.get(param, ""))
    return body


# --------------------------------------------------------------------------
# Result shapes (always a JSON object)
# --------------------------------------------------------------------------


def _present(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    return {key: value for key, value in (filters or {}).items() if value is not None}


def paged_result(page: Page, filters: Mapping[str, Any] | None) -> dict[str, Any]:
    """One page of a paged list; pass next_cursor back as cursor with the SAME filters."""
    return {
        "items": page.items,
        "count": len(page.items),
        "total_count": page.total_count,
        "has_more": page.has_more,
        "next_cursor": page.next_cursor,
        "page_size": page.page_size,
        "filters": _present(filters),
    }


def list_result(items: list[Any]) -> dict[str, Any]:
    """An unpaged list."""
    return {"items": items, "count": len(items)}


def all_result(
    res: AllResult, filters: Mapping[str, Any] | None, *, truncated: bool | None = None
) -> dict[str, Any]:
    """Every row of an auto-paged scan, with the honesty flags (truncated, complete_scan, count_mismatch).

    `truncated` is computed from the scan (true when the scan stopped early) unless the caller passes it:
    an explicit True or False replaces the computed value. A tool that cuts the rows itself (a limit
    applied after a client-side filter, say) passes truncated=True so the model knows rows are missing.
    complete_scan always reports the scan itself (res.complete): it can be true while truncated is true,
    which means "everything was read, but not everything is shown".
    """
    if truncated is not None and not isinstance(truncated, bool):
        raise ValueError(f"all_result: truncated must be True, False or None, got {describe_value(truncated)}")
    return {
        "items": res.items,
        "count": len(res.items),
        "total_count": res.total_count,
        "truncated": (not res.complete) if truncated is None else truncated,
        "complete_scan": res.complete,
        "count_mismatch": res.count_mismatch,
        "filters": _present(filters),
    }


def ok_result(value: Any) -> dict[str, Any]:
    """{"ok": bool} for a boolean, a dict unchanged, anything else as {"result": value}."""
    if isinstance(value, bool):
        return {"ok": value}
    if isinstance(value, dict):
        return value
    return {"result": value}


# --------------------------------------------------------------------------
# Error text for the model
# --------------------------------------------------------------------------


def _strip_indexes(name: str) -> str:
    return re.sub(r"\[\d*\]", "", name).strip(".")


def _param_for_property(prop: str | None, field_map: Mapping[str, str]) -> str | None:
    """The snake_case param for a Gorelo PropertyName, or None to keep Gorelo's own name.

    Order: exact dotted match, then case-insensitive dotted match. A bare name (no dot) is special,
    because Gorelo reports a bad Location.Phone as just "Phone": it can also be the last segment of
    nested paths. If it matches a top-level path AND the last segment of nested paths, every
    candidate is named ("name or location_name"), because Gorelo does not say which one it meant. A
    bare name that matches only nested paths is mapped only when exactly one nested path ends in it."""
    if not prop or not field_map:
        return None
    wanted = _strip_indexes(prop)
    paths = {param: _strip_indexes(path) for param, path in field_map.items()}
    direct = [param for param, path in paths.items() if path == wanted]
    if not direct:
        direct = [param for param, path in paths.items() if path.lower() == wanted.lower()]
    if "." in wanted:
        return direct[0] if direct else None
    nested = [
        param for param, path in paths.items()
        if "." in path and path.rsplit(".", 1)[-1].lower() == wanted.lower()
    ]
    if direct:
        return " or ".join(direct + [param for param in nested if param not in direct])
    return nested[0] if len(nested) == 1 else None


_PATH_SEGMENT = re.compile(r"([^\[\].]+)((?:\[\d*\])*)")
_PATH_INDEX = re.compile(r"\[(\d*)\]")


def _split_property(prop: str) -> list[tuple[str, list[int]]] | None:
    """Gorelo's PropertyName as [(name, [list indexes])], or None if it is not a dotted path with well formed
    indexes. "Attachments[0].Url" -> [("Attachments", [0]), ("Url", [])]; an empty index "[]" carries no number."""
    segments: list[tuple[str, list[int]]] = []
    for part in prop.split("."):
        match = _PATH_SEGMENT.fullmatch(part)
        if match is None:
            return None
        indexes = [int(number) for number in _PATH_INDEX.findall(match.group(2)) if number]
        segments.append((match.group(1), indexes))
    return segments


def _indexed_label(prop: str | None, field_map: Mapping[str, str]) -> str | None:
    """A label for a PropertyName that points INTO a list and has no exact mapping, or None.

    Gorelo reports an error inside a list item as "Attachments[0].Url" or "SubItems[1].ItemId". The tool
    has one parameter for the whole list (attachments, sub_items), so the nearest ancestor of the path
    that field_map knows is named, then where in it the problem is, with item numbers counted from 1
    (the way a person counts):

        Attachments[0].Url      -> "attachments (item 1, Url)"
        SubItems[1].ItemId      -> "sub_items (item 2, ItemId)"
        Location.Phones[2].Number with only "Location" mapped -> "location (Phones item 3, Number)"

    The caller tries the exact mapping first (indexes ignored), so this only runs when there is none.
    A property with no list index, or with no mapped ancestor, gets None and keeps Gorelo's own name.
    """
    if not prop or "[" not in prop or not field_map:
        return None
    segments = _split_property(prop)
    if not segments:
        return None
    for cut in range(len(segments) - 1, 0, -1):  # the nearest ancestor first
        param = _param_for_property(".".join(name for name, _ in segments[:cut]), field_map)
        if param is None:
            continue
        where = [f"item {index + 1}" for index in segments[cut - 1][1]]
        for name, indexes in segments[cut:]:
            where.append(" ".join([name, *(f"item {index + 1}" for index in indexes)]))
        return f"{param} ({', '.join(where)})"
    return None


_UNCONFIRMED = "Verify with a read before retrying."
_EXPORT_ADVICE = "Retry only if one more export event is acceptable."


def _export_tail(text: str) -> str:
    """The sentences for a failed SIDE_EFFECT_GETS call (the PDF export): it may be recorded already."""
    note = "" if EXPORT_NOTE in text else f" Note: {EXPORT_NOTE}."
    return f".{note} {_EXPORT_ADVICE}"


def format_gorelo_error(
    err: GoreloAPIError, tool_name: str, field_map: Mapping[str, str] | None = None
) -> str:
    """The message a tool error carries, for example:

    Gorelo rejected create_client (HTTP 400, code 070101): location_phone: Mobile phone validation
    failed [trace 00-abc]

    Every Notification is listed, with Gorelo's PropertyName mapped to the snake_case parameter through
    the inverted field_map where possible (a bare name that fits a top-level and a nested field names
    all of them: "name or location_name"). field_map may hold body field paths, query parameter names
    and path placeholders alike. A PropertyName that points into a list ("Attachments[0].Url") and has
    no exact mapping is shown against the nearest mapped ancestor with 1-based item numbers:
    "attachments (item 1, Url): message". Scope, rate limit, unconfirmed write and forbidden
    operations have their own wording. An unconfirmed SIDE_EFFECT_GETS call (the invoice PDF export)
    says the export may already be recorded and that a retry records another export event.
    """
    field_map = field_map or {}
    kind = err.kind
    trace = f" [trace {err.trace_id}]" if err.trace_id else ""
    export = err.write_unconfirmed and is_side_effect_get(err.op_key)

    if kind == "forbidden":
        return (
            f"{tool_name} cannot run: {err.op_key or 'this operation'} is deliberately not available "
            "through this server (it deletes or uninstalls production data, or creates API keys). Do it in the "
            "Gorelo app if it is really intended."
        )
    if kind in ("timeout", "transport"):
        what = "the request timed out" if kind == "timeout" else "the connection failed"
        if export:
            text = f"Gorelo did not confirm {tool_name} ({what}); {EXPORT_NOTE}"
            return text + _export_tail(text)
        if err.write_unconfirmed:
            return (
                f"Gorelo did not confirm {tool_name} ({what}). The change may or may not have been "
                f"applied. {_UNCONFIRMED}"
            )
        return f"Gorelo did not answer {tool_name} ({what}). This was a read, so retrying is safe."
    if kind == "rate_limit":
        return (
            f"Gorelo is rate limiting requests (HTTP 429) for {tool_name}; it did not process this "
            "request. Retry later."
        )
    if kind == "spec":
        return f"{tool_name} cannot run: {err}"
    if kind == "shape":
        text = f"Gorelo returned an unexpected response for {tool_name}: {err}"
        if export:
            text += _export_tail(text)
        elif err.write_unconfirmed and _VERIFY_NOTE not in str(err):
            # created_id() and expect_object() already say it in their own words: not twice
            text += f". The change may or may not have been applied. {_UNCONFIRMED}"
        return text + trace

    codes = _distinct_codes(err)
    http = f"HTTP {err.status}" if err.status is not None else "no HTTP status"
    head = f"Gorelo rejected {tool_name} ({http}" + (f", code {'/'.join(codes)}" if codes else "") + ")"

    detail = _scope_text(err) or _notification_detail(err, field_map)
    text = f"{head}: {detail}"
    if export:
        text += _export_tail(text)
    elif err.write_unconfirmed:
        text += f". Gorelo may have applied the change before failing. {_UNCONFIRMED}"
    return text + trace


def _distinct_codes(err: GoreloAPIError) -> list[str]:
    """The Notification codes of an error, each once, in the order Gorelo sent them."""
    codes: list[str] = []
    for note in err.notifications:
        code = note.get("code")
        if code and code not in codes:
            codes.append(str(code))
    return codes


def _notification_detail(err: GoreloAPIError, field_map: Mapping[str, str]) -> str:
    """Every Notification as "param: message", joined with "; ". Gorelo's PropertyName is mapped to the
    snake_case param through field_map where possible (see _param_for_property and _indexed_label) and
    kept as Gorelo wrote it otherwise."""
    parts: list[str] = []
    for note in err.notifications:
        message = note.get("message") or note.get("code") or "no message"
        prop = note.get("property")
        label = _param_for_property(prop, field_map) or _indexed_label(prop, field_map) or prop
        parts.append(f"{label}: {message}" if label else str(message))
    return "; ".join(parts) if parts else "no details returned"


def _scope_text(err: GoreloAPIError) -> str | None:
    for note in err.notifications:
        if note.get("code") == SCOPE_CODE:
            quoted = re.search(r"'([^']+)'", str(note.get("message") or ""))
            scope = f"the '{quoted.group(1)}' scope" if quoted else "a scope this tool needs"
            return (
                f"the API key does not have {scope}. Grant it on the API key in Gorelo, then retry"
            )
    return None
