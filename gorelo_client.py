"""All HTTP to the Gorelo Public API. One shared client, spec-validated requests, loud failures.

Contract (the observed API behavior behind every rule is in docs/API-OBSERVED-BEHAVIOR.md):

* ONE httpx.AsyncClient per GoreloClient, created by `async with GoreloClient(...)`. The server
  lifespan owns it (see server.py). Auth is the X-API-Key header only; there is no default
  Content-Type (httpx sets it per request: json= or multipart).
* Every request is named by an operation key from spec/spec_index.json ("GET /v1/tickets/{ticketId}").
  Unknown operations, forbidden operations, unknown query names (even with a None value), unknown
  body fields, mistyped body values, bad or missing path ids and blank form values are rejected
  BEFORE any HTTP call (GoreloAPIError kind "forbidden" or "spec", or spec.SpecViolation).
* Path ids are checked against the spec's type for the placeholder (uuid, integer, or a plain token,
  see spec.validate_path_param), sent in canonical form and percent-encoded, and the path that goes
  on the wire is compared with the checked one, so an id can never turn one operation into another
  (httpx would otherwise collapse DELETE .../comments/.. into DELETE /v1/tickets/T).
* While a @gorelo_tool function runs (tools._common sets CURRENT_TOOL), only the operations that
  tool declared in ops=[...] can be sent: anything else is refused with kind "spec" and no HTTP call.
  Calls outside any tool (tests, scripts) are not restricted.
* Every Gorelo response is an envelope {StatusCode, IsSuccess, Data, DataContext, Notifications}
  (the invoice PDF is the only raw body). `IsSuccess` is checked, not just the HTTP status. A body
  that is not a Gorelo envelope raises kind "shape" whatever the HTTP status (the status stays in
  err.status): this client never returns [] or {} to paper over a response it does not understand.
  A binary operation counts as a download only when the 2xx response carries one of the expected
  content types (default application/pdf); anything else is parsed as an envelope or refused. Every
  body of a binary request is read through the max_bytes cap, a download or not: a huge page of an
  unexpected content type is refused (kind "shape") without being buffered whole first.
* 429 (Gorelo did not process the request) is retried within a small budget for every method.
  Timeouts, connection errors, 5xx and unreadable 2xx responses are NEVER retried: for a write they
  are reported as `write_unconfirmed` so nobody repeats a write that may have applied.
* SIDE_EFFECT_GETS are GETs that still change state (the invoice PDF download is recorded as an
  export event). They are treated like writes for every failure above: write_unconfirmed is True and
  the message says the export may already be recorded and a retry records another export event. Like
  FORBIDDEN_OPS they are compared by SHAPE (is_side_effect_get: every {placeholder} counts the same, whatever
  Gorelo calls it), so a placeholder rename cannot turn the PDF export back into a harmless read.
* At most `max_concurrency` requests are in flight at once.
* Logging (logger "gorelo_client"): one INFO line per request with the tool, operation, status,
  latency, attempts, query parameter NAMES and body field NAMES (only names of fields the spec
  defines, as returned by spec.validate_body). Never values, never secrets. The httpx logger is
  raised to WARNING because it logs full URLs, and URLs carry query values.

Helpers (get_one, get_list, get_page, get_all, post, patch, delete, post_multipart, get_binary)
all take the calling tool's name as `tool=`; it only labels logs.
"""

from __future__ import annotations

import asyncio
import email.message
import email.utils
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx

from spec import (
    OpSpec,
    SpecIndex,
    SpecViolation,
    load_spec_index,
    normalize_query,
    validate_body,
    validate_path_param,
)

logger = logging.getLogger("gorelo_client")

GORELO_BASE_URL = "https://api.usw.gorelo.io/v1"
PAGE_SIZE_MIN, PAGE_SIZE_MAX = 1, 200

# Never callable, by any tool, whatever its kind. Deleting an agent asset UNINSTALLS the RMM agent
# from the host; the other deletes destroy records the operator wants to remove by hand in the app;
# POST /v1/api-keys would let a tool mint API credentials (a key with any scope, returned once).
# Keys are written the way the spec index spells them today, but they are COMPARED by shape
# (is_forbidden_op): see normalize_op_key.
FORBIDDEN_OPS = frozenset(
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

_PLACEHOLDER_SEGMENT = re.compile(r"\{[^{}]*\}")


def normalize_op_key(op_key: str) -> str:
    """An operation key with every {placeholder} written {}: the shape of the operation.

    Gorelo renamed the placeholders of thirteen operations on 2026-10-02 without changing their URLs (the
    clients delete ended in {id} and now ends in {clientId}). A forbidden operation must stay forbidden
    through such a rename, so forbidden operations are compared by this shape and never by the exact text
    of the key.
    """
    return _PLACEHOLDER_SEGMENT.sub("{}", op_key)


def is_forbidden_op(op_key: str) -> bool:
    """True when `op_key` is one of FORBIDDEN_OPS, whatever its placeholders are called.

    Every place that compares an operation to the forbidden ones goes through this function (the client
    when a call is made, server.build_server when tools are declared): the clients delete is as forbidden
    under a placeholder called id, or anything else, as under the one the spec uses today (clientId).
    """
    if not isinstance(op_key, str):
        return False
    shape = normalize_op_key(op_key)
    return any(shape == normalize_op_key(forbidden) for forbidden in FORBIDDEN_OPS)


# GETs that are not pure reads: Gorelo records the call as an event on the record (the invoice PDF
# download is logged on the invoice as an export event). A failed call may already have been
# recorded and a retry records another one, so they are handled like writes: write_unconfirmed is
# set for transport errors, timeouts, 5xx and unreadable 2xx, and a tool that uses one must not be
# declared kind="read". Keys are written the way the spec index spells them today, but they are
# COMPARED by shape (is_side_effect_get), exactly like FORBIDDEN_OPS: see normalize_op_key.
SIDE_EFFECT_GETS = frozenset({"GET /v1/invoices/{invoiceId}/pdf"})


def is_side_effect_get(op_key: str) -> bool:
    """True when `op_key` is one of SIDE_EFFECT_GETS, whatever its placeholders are called.

    The same rule as is_forbidden_op, for the same reason: Gorelo renamed thirteen placeholders on 2026-10-02
    without changing a URL, and an operation key compared by its exact text would silently stop matching. For the
    invoice PDF export that would mean a failed download reported as a harmless read ("nothing was changed",
    safe to retry) while Gorelo may already have recorded it. Every place that asks whether an operation is a
    side-effect GET goes through this function; a tool that wants the same answer should too.
    """
    if not isinstance(op_key, str):
        return False
    shape = normalize_op_key(op_key)
    return any(shape == normalize_op_key(known) for known in SIDE_EFFECT_GETS)


# Said to the model whenever a failed SIDE_EFFECT_GETS call may have been recorded anyway.
EXPORT_NOTE = (
    "the export may already be recorded on the invoice, and a retry records another export event"
)

DEFAULT_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # used only if a caller passes binary=True without max_bytes
DEFAULT_EXPECTED_CONTENT_TYPES = ("application/pdf",)  # what a binary operation's download may be
MAX_PAGES = 500  # get_all stops with an error rather than follow cursors forever
DEFAULT_RETRY_AFTER = 1.0

_ERROR_KINDS = ("http", "envelope", "shape", "timeout", "transport", "rate_limit", "forbidden", "spec")
_CONTENT_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*")


class DeclaredTool(Protocol):
    """What the client needs to know about the tool that is running: its name and declared ops.
    tools._common.ToolSpec satisfies it."""

    name: str
    ops: Sequence[str]


# The tool whose function is running right now, set by the @gorelo_tool wrapper of the OUTERMOST
# tool call and reset when it ends. A task started inside the tool (asyncio.gather, TaskGroup)
# inherits it. None means "not inside a tool" (tests, scripts), and then any operation may be sent.
CURRENT_TOOL: ContextVar[DeclaredTool | None] = ContextVar("gorelo_current_tool", default=None)


# --------------------------------------------------------------------------
# Errors and result types
# --------------------------------------------------------------------------


class GoreloAPIError(Exception):
    """A failed Gorelo call. str(err) is a concise one-liner; tools show format_gorelo_error().

    Attributes:
        status: HTTP status, or None if there was no response (timeout, transport, spec, forbidden).
        op_key: the operation key, for example "POST /v1/contacts".
        kind: "http" (non-2xx), "envelope" (2xx but IsSuccess false), "shape" (a response this client
            does not understand), "timeout", "transport", "rate_limit" (429 persisted),
            "forbidden" (operation blocked by design) or "spec" (unknown operation or helper misuse).
        notifications: Gorelo's Notifications as [{"code", "message", "property"}].
        trace_id: DataContext.TraceId, quote it when reporting a problem to Gorelo.
        write_unconfirmed: True when a write may or may not have been applied (timeout, connection
            error, 5xx, or an unreadable 2xx on a non-GET). Verify with a read before retrying. It is
            also True for the same failures of a SIDE_EFFECT_GETS call (the PDF download): the export
            may already be recorded and a retry records another export event.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        status: int | None = None,
        op_key: str | None = None,
        kind: str = "http",
        notifications: list[Mapping[str, Any]] | None = None,
        trace_id: str | None = None,
        write_unconfirmed: bool = False,
    ):
        self.status = status
        self.op_key = op_key
        self.kind = kind
        self.notifications: list[dict[str, Any]] = [_notification(n) for n in notifications or []]
        self.trace_id = trace_id
        self.write_unconfirmed = write_unconfirmed
        if message is not None and not isinstance(message, str):
            message = str(message)
        if not message:
            where = op_key or "Gorelo request"
            http = f", HTTP {status}" if status is not None else ""
            message = f"{where} failed ({kind}{http})"
        self.message: str = message
        super().__init__(message)

    def __str__(self) -> str:
        return " ".join(self.message.split())

    def __reduce__(self) -> tuple[Any, ...]:
        # keyword-only arguments would otherwise be lost by copy.copy() and pickle
        return (
            _restore_error,
            (self.message, self.status, self.op_key, self.kind, self.notifications, self.trace_id, self.write_unconfirmed),
        )


def _restore_error(
    message: str,
    status: int | None,
    op_key: str | None,
    kind: str,
    notifications: list[dict[str, Any]],
    trace_id: str | None,
    write_unconfirmed: bool,
) -> GoreloAPIError:
    return GoreloAPIError(
        message, status=status, op_key=op_key, kind=kind, notifications=notifications,
        trace_id=trace_id, write_unconfirmed=write_unconfirmed,
    )


@dataclass(frozen=True)
class Page:
    """One page of a paged list. page_size is the size actually requested (after clamping)."""

    items: list[Any]
    next_cursor: str | None
    has_more: bool
    total_count: int | None
    page_size: int


@dataclass(frozen=True)
class AllResult:
    """Every row of a paged list (get_all). `complete` is False if max_items cut the scan short.

    count_mismatch is True when the scan completed but len(items) differs from Gorelo's TotalCount.
    """

    items: list[Any]
    total_count: int | None
    complete: bool
    pages: int
    count_mismatch: bool


@dataclass(frozen=True)
class BinaryResult:
    """A file download (the invoice PDF)."""

    content: bytes
    filename: str | None
    content_type: str | None


@dataclass
class _Raw:
    status: int
    headers: httpx.Headers
    content: bytes
    is_download: bool = False
    expected: frozenset[str] | None = None  # the download types a binary request accepts


_MISSING: Any = object()


# --------------------------------------------------------------------------
# Small parsing helpers
# --------------------------------------------------------------------------


def _ci_get(mapping: Any, name: str) -> Any:
    """Case-insensitive dict lookup; None if absent or not a dict."""
    if not isinstance(mapping, Mapping):
        return None
    if name in mapping:
        return mapping[name]
    lowered = name.lower()
    for key, value in mapping.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value
    return None


def _notification(raw: Any) -> dict[str, Any]:
    """Normalize one notification to {"code", "message", "property"} (accepts either casing)."""
    if isinstance(raw, Mapping):
        prop = _ci_get(raw, "PropertyName")
        if prop is None:
            prop = _ci_get(raw, "property")
        return {
            "code": _ci_get(raw, "Code"),
            "message": _ci_get(raw, "Message"),
            "property": prop,
        }
    return {"code": None, "message": str(raw), "property": None}


def _main_type(content_type: Any) -> str:
    """The media type of a Content-Type header, lowercase, without parameters ("" if there is none)."""
    if not isinstance(content_type, str):
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def _normalize_content_types(values: Any) -> frozenset[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple, set, frozenset)) or not values:
        raise ValueError(
            "expected_content_types must be a non-empty list or tuple of content types, "
            "for example ('application/pdf',)"
        )
    found: set[str] = set()
    for item in values:
        main = _main_type(item)
        if not _CONTENT_TYPE.fullmatch(main):
            raise ValueError(f"expected_content_types: {item!r} is not a content type such as 'application/pdf'")
        found.add(main)
    return frozenset(found)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _finite_seconds(value: float) -> float | None:
    if isinstance(value, bool) or not math.isfinite(value):
        return None
    return max(0.0, float(value))


def _duration_seconds(value: Any) -> float | None:
    """Seconds from a number or a string such as 1, "1.5", "1s", "500ms", "00:00:02"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _finite_seconds(value)
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|msec|milliseconds?|s|sec|secs|seconds?)?", text)
    if match:
        number = float(match.group(1))
        unit = match.group(2) or "s"
        return _finite_seconds(number / 1000.0 if unit.startswith("m") else number)
    clock = re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", text)
    if clock:
        return _finite_seconds(int(clock.group(1)) * 3600 + int(clock.group(2)) * 60 + float(clock.group(3)))
    return None


def _retry_after_header(value: str | None) -> float | None:
    if not value or not value.strip():
        return None
    text = value.strip()
    try:
        return _finite_seconds(float(text))
    except ValueError:
        pass
    try:
        moment = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return _finite_seconds((moment - _now_utc()).total_seconds())


def _retry_after_body(content: bytes) -> float | None:
    try:
        data = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return None
    return _find_retry_after(data, 0)


def _find_retry_after(node: Any, depth: int) -> float | None:
    if not isinstance(node, Mapping):
        return None
    for key, value in node.items():
        if isinstance(key, str) and key.replace("_", "").replace("-", "").lower() == "retryafter":
            seconds = _duration_seconds(value)
            if seconds is not None:
                return seconds
    if depth < 2:
        for name in ("DataContext", "Data"):
            seconds = _find_retry_after(_ci_get(node, name), depth + 1)
            if seconds is not None:
                return seconds
    return None


def _retry_after_seconds(headers: httpx.Headers, content: bytes) -> float:
    """Retry-After header (seconds or HTTP date), else body retry_after / RetryAfter, else 1 s."""
    seconds = _retry_after_header(headers.get("retry-after"))
    if seconds is None:
        seconds = _retry_after_body(content)
    return DEFAULT_RETRY_AFTER if seconds is None else seconds


def _download_filename(headers: httpx.Headers) -> str | None:
    disposition = headers.get("content-disposition")
    if not disposition:
        return None
    holder = email.message.Message()
    holder["content-disposition"] = disposition
    name = holder.get_filename()
    if not name:
        return None
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or None


def _utc_z(moment: datetime) -> str:
    text = moment.astimezone(timezone.utc).isoformat(timespec="microseconds" if moment.microsecond else "seconds")
    return text.replace("+00:00", "Z")


def _quiet_http_library_logging() -> None:
    """httpx logs 'HTTP Request: GET <full url>' at INFO, and the URL carries query values."""
    for name in ("httpx", "httpcore"):
        library_logger = logging.getLogger(name)
        if library_logger.level == logging.NOTSET or library_logger.level < logging.WARNING:
            library_logger.setLevel(logging.WARNING)


def _clamp_page_size(value: Any) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"page_size must be an integer between {PAGE_SIZE_MIN} and {PAGE_SIZE_MAX}, got {value!r}") from None
    return max(PAGE_SIZE_MIN, min(PAGE_SIZE_MAX, size))


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------


class GoreloClient:
    """Async client for the Gorelo Public API. Use as `async with GoreloClient(key) as client`."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = GORELO_BASE_URL,
        spec: SpecIndex | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        event_hooks: dict[str, list[Callable[..., Any]]] | None = None,
        timeout: float = 30.0,
        max_concurrency: int = 4,
        max_429_retries: int = 3,
        max_429_wait: float = 20.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if not api_key or not str(api_key).strip():
            raise ValueError("api_key is required")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if max_429_retries < 0 or max_429_wait < 0:
            raise ValueError("max_429_retries and max_429_wait must not be negative")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.spec: SpecIndex = spec if spec is not None else load_spec_index()
        self.max_429_retries = max_429_retries
        self.max_429_wait = float(max_429_wait)
        self._api_key = api_key
        self._transport = transport
        self._event_hooks = event_hooks
        self._timeout = timeout
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._base_path = urlsplit(self.base_url).path.rstrip("/")
        self._http: httpx.AsyncClient | None = None
        _quiet_http_library_logging()

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> GoreloClient:
        if self._http is not None:
            raise RuntimeError("GoreloClient is already started")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-API-Key": self._api_key, "Accept": "application/json"},
            timeout=self._timeout,
            transport=self._transport,
            event_hooks=self._event_hooks,
        )
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    @property
    def is_closed(self) -> bool:
        return self._http is None or self._http.is_closed

    # -- the one low-level call -------------------------------------------

    async def request(
        self,
        op_key: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        json_body: Any = None,
        files: Mapping[str, Any] | None = None,
        form: Mapping[str, Any] | None = None,
        tool: str,
        binary: bool = False,
        max_bytes: int | None = None,
        expected_content_types: Sequence[str] = DEFAULT_EXPECTED_CONTENT_TYPES,
    ) -> Any:
        """Send one operation and return the parsed envelope dict.

        With binary=True and a 2xx response whose Content-Type is one of expected_content_types
        (default application/pdf) the result is a BinaryResult (the body is read in a streaming
        fashion and capped at max_bytes). Any other 2xx response to a binary request is parsed as an
        envelope or refused with kind "shape": nothing is guessed to be a file. That other body is
        read through the same max_bytes cap, so one that is larger is refused with kind "shape" without
        being buffered whole (the same goes for the body of a non-2xx answer). Raises GoreloAPIError,
        or SpecViolation for an invalid query name, body field, body value shape, path id or form value
        (always before any HTTP call), or GoreloAPIError kind "spec" for an operation the running tool
        did not declare.
        """
        _require_tool(tool)
        self._check_allowed(op_key, tool)
        op = self._lookup(op_key)
        path = self._fill_path(op, path_params)
        query_params = normalize_query(op, self._prepare_query(op, query))
        body_kwargs, body_names = self._prepare_body(op, json_body, files, form)
        limit = max_bytes if max_bytes is not None else DEFAULT_MAX_DOWNLOAD_BYTES
        if binary and limit < 1:
            raise ValueError("max_bytes must be at least 1")
        expected = _normalize_content_types(expected_content_types)

        started = time.perf_counter()
        attempts = 0
        waited = 0.0
        status: Any = "none"
        try:
            while True:
                attempts += 1
                raw = await self._send_once(
                    op, path, query_params, body_kwargs, binary=binary, max_bytes=limit, expected=expected
                )
                status = raw.status
                if raw.status != 429:
                    break
                wait = _retry_after_seconds(raw.headers, raw.content)
                if attempts <= self.max_429_retries and waited + wait <= self.max_429_wait:
                    logger.warning(
                        "tool=%s op=%s got HTTP 429 (Gorelo did not process it); waiting %.1fs, retry %d of %d",
                        tool, op.key, wait, attempts, self.max_429_retries,
                    )
                    waited += wait
                    await self._sleep(wait)
                    continue
                raise self._rate_limit_error(op, raw, attempts, waited)
            return self._interpret(op, raw)
        except GoreloAPIError as exc:
            if status == "none" and exc.kind in ("timeout", "transport"):
                status = exc.kind
            raise
        finally:
            logger.info(
                "tool=%s op=%s path=%s status=%s latency_ms=%d attempts=%d query=%s body=%s",
                tool,
                op.key,
                path,
                status,
                int((time.perf_counter() - started) * 1000),
                attempts,
                ",".join(sorted(query_params)) or "none",
                ",".join(sorted(body_names)) or "none",
            )

    # -- helpers: reads ----------------------------------------------------

    async def get_one(
        self,
        op_key: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        tool: str,
    ) -> Any:
        """GET one record: returns Data unchanged. Rejects paged ops (use get_page or get_all)."""
        self._op_for(op_key, tool, methods=("GET",), paged=False, binary=False)
        envelope = await self.request(op_key, path_params=path_params, query=query, tool=tool)
        data = envelope.get("Data")
        if data is None:
            raise GoreloAPIError(
                f"{op_key}: Gorelo reported success but Data is null; refusing to guess",
                status=200, op_key=op_key, kind="shape",
            )
        return data

    async def get_list(
        self,
        op_key: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        tool: str,
    ) -> list[Any]:
        """GET an UNPAGED list op (lookups). Data must be a list."""
        self._op_for(op_key, tool, methods=("GET",), paged=False, binary=False)
        envelope = await self.request(op_key, path_params=path_params, query=query, tool=tool)
        data = envelope.get("Data")
        if not isinstance(data, list):
            raise GoreloAPIError(
                f"{op_key}: expected Data to be a list but got {_describe(data)}; refusing to guess",
                status=200, op_key=op_key, kind="shape",
            )
        return data

    async def get_page(
        self,
        op_key: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        page_size: int = 50,
        cursor: str | None = None,
        tool: str,
    ) -> Page:
        """GET one page of a PAGED op. page_size is clamped to 1-200 and the size used is reported.

        Cursors are opaque and valid only with the SAME filters. A blank cursor means "first page".
        """
        op = self._op_for(op_key, tool, methods=("GET",), paged=True, binary=False)
        size = _clamp_page_size(page_size)
        merged = self._with_paging(op, query, size, cursor)
        envelope = await self.request(op_key, path_params=path_params, query=merged, tool=tool)
        return self._to_page(op, envelope, size)

    async def get_all(
        self,
        op_key: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        page_size: int = 200,
        max_items: int | None = None,
        tool: str,
    ) -> AllResult:
        """Follow NextCursor while HasMore and return every row.

        A repeated cursor raises kind "shape" (Gorelo re-serves page 1 for a cursor it does not
        recognise). max_items stops the scan early with complete=False. When the scan completes and
        the row count differs from TotalCount, count_mismatch is True and a WARNING is logged.
        """
        if max_items is not None and max_items < 1:
            raise ValueError("max_items must be at least 1 (or None for no cap)")
        items: list[Any] = []
        seen: set[str] = set()
        cursor: str | None = None
        pages = 0
        total: int | None = None
        complete = False
        while True:
            page = await self.get_page(
                op_key, path_params=path_params, query=query, page_size=page_size, cursor=cursor, tool=tool
            )
            pages += 1
            items.extend(page.items)
            total = page.total_count
            if max_items is not None and len(items) >= max_items:
                complete = len(items) == max_items and not page.has_more
                del items[max_items:]
                break
            if not page.has_more:
                complete = True
                break
            nxt = page.next_cursor
            if nxt is None or nxt in seen:
                raise GoreloAPIError(
                    f"{op_key}: Gorelo returned the cursor it had already served ({pages} pages read); "
                    "refusing to loop and return duplicate rows",
                    status=200, op_key=op_key, kind="shape",
                )
            if pages >= MAX_PAGES:
                raise GoreloAPIError(
                    f"{op_key}: paging did not finish after {MAX_PAGES} pages; refusing to continue",
                    status=200, op_key=op_key, kind="shape",
                )
            seen.add(nxt)
            cursor = nxt
        mismatch = complete and total is not None and len(items) != total
        if mismatch:
            logger.warning(
                "tool=%s op=%s read %d rows but Gorelo reports TotalCount=%s",
                tool, op_key, len(items), total,
            )
        return AllResult(items=items, total_count=total, complete=complete, pages=pages, count_mismatch=mismatch)

    async def get_binary(
        self,
        op_key: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        max_bytes: int,
        expected_content_types: Sequence[str] = DEFAULT_EXPECTED_CONTENT_TYPES,
        tool: str,
    ) -> BinaryResult:
        """GET a file download (the invoice PDF), read in a streaming fashion and capped at max_bytes.

        A 2xx response is a download only when its Content-Type is one of expected_content_types
        (media type compared case-insensitively, parameters such as charset ignored). Any other 2xx
        body is parsed as an envelope and refused with kind "shape" ("unexpected response shape;
        refusing to guess"): an HTML or plain text page is never handed back as a file. Such a body is
        read through the same max_bytes cap as a download, so a huge one is refused without being
        buffered whole. For the PDF export (SIDE_EFFECT_GETS) every such failure that Gorelo may have
        recorded (a 2xx or 5xx answer) is write_unconfirmed: the export may already be recorded and a
        retry records another export event.
        """
        self._op_for(op_key, tool, methods=("GET",), paged=False, binary=True)
        result = await self.request(
            op_key, path_params=path_params, query=query, tool=tool, binary=True, max_bytes=max_bytes,
            expected_content_types=expected_content_types,
        )
        if not isinstance(result, BinaryResult):
            raise GoreloAPIError(
                f"{op_key}: expected a file download but Gorelo returned a JSON envelope; refusing to guess"
                + (f" ({EXPORT_NOTE})" if is_side_effect_get(op_key) else ""),
                status=200, op_key=op_key, kind="shape", write_unconfirmed=is_side_effect_get(op_key),
            )
        return result

    # -- helpers: writes ---------------------------------------------------

    async def post(
        self,
        op_key: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        tool: str,
    ) -> Any:
        """POST a JSON body and return Data."""
        self._op_for(op_key, tool, methods=("POST",), binary=False)
        envelope = await self.request(op_key, path_params=path_params, json_body=json_body, tool=tool)
        return envelope.get("Data")

    async def patch(
        self,
        op_key: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        tool: str,
    ) -> Any:
        """PATCH a JSON body and return Data."""
        self._op_for(op_key, tool, methods=("PATCH",), binary=False)
        envelope = await self.request(op_key, path_params=path_params, json_body=json_body, tool=tool)
        return envelope.get("Data")

    async def delete(
        self,
        op_key: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        tool: str,
    ) -> Any:
        """DELETE and return Data. Forbidden operations raise kind "forbidden" without any HTTP call."""
        self._op_for(op_key, tool, methods=("DELETE",), binary=False)
        envelope = await self.request(op_key, path_params=path_params, tool=tool)
        return envelope.get("Data")

    async def post_multipart(
        self,
        op_key: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        files: Mapping[str, Any],
        form: Mapping[str, Any] | None = None,
        tool: str,
    ) -> Any:
        """POST multipart/form-data (the attachment upload) and return Data.

        `files` maps a form field name to what httpx accepts, for example
        {"file": (filename, content_bytes, content_type)}; `form` maps plain field names to strings.
        A form value that is None or blank raises SpecViolation naming the field: it is never dropped
        and never sent as an empty text part.
        """
        op = self._op_for(op_key, tool, methods=("POST",), binary=False)
        if not op.is_multipart:
            raise GoreloAPIError(
                f"{op_key} does not take multipart form data; use post()", op_key=op_key, kind="spec"
            )
        envelope = await self.request(
            op_key, path_params=path_params, files=files, form=form if form is not None else {}, tool=tool
        )
        return envelope.get("Data")

    # -- internals ---------------------------------------------------------

    def _check_not_forbidden(self, op_key: str, tool: str) -> None:
        if is_forbidden_op(op_key):
            logger.warning("tool=%s op=%s refused: this operation is forbidden by design", tool, op_key)
            raise GoreloAPIError(
                f"{op_key} is deliberately not available through this server",
                op_key=op_key, kind="forbidden",
            )

    def _check_declared(self, op_key: str, tool: str) -> None:
        """Inside a running @gorelo_tool, only the operations that tool declared may be sent."""
        running = CURRENT_TOOL.get()
        if running is None or op_key in running.ops:
            return
        logger.warning(
            "tool=%s op=%s refused: the running tool %s did not declare this operation", tool, op_key, running.name
        )
        declared = ", ".join(running.ops) or "none"
        raise GoreloAPIError(
            f"{op_key} is not one of the operations that tool '{running.name}' declares in ops=[...] "
            f"(declared: {declared}); add it to the declaration or do not call it",
            op_key=op_key, kind="spec",
        )

    def _check_allowed(self, op_key: str, tool: str) -> None:
        """Forbidden operations first, then the running tool's declared operations."""
        self._check_not_forbidden(op_key, tool)
        self._check_declared(op_key, tool)

    def _lookup(self, op_key: str) -> OpSpec:
        try:
            return self.spec.op(op_key)
        except KeyError as exc:
            raise GoreloAPIError(str(exc.args[0]), op_key=op_key, kind="spec") from None

    def _op_for(
        self,
        op_key: str,
        tool: str,
        *,
        methods: tuple[str, ...],
        paged: bool | None = None,
        binary: bool | None = None,
    ) -> OpSpec:
        """Forbidden and declared checks first, then the operation, then the helper's expectations about it."""
        _require_tool(tool)
        self._check_allowed(op_key, tool)
        op = self._lookup(op_key)
        if op.method not in methods:
            raise GoreloAPIError(
                f"{op_key} is a {op.method} operation; this helper is for {'/'.join(methods)}",
                op_key=op_key, kind="spec",
            )
        if paged is True and not op.paged:
            raise GoreloAPIError(
                f"{op_key} is not a paged operation; use get_list or get_one (it takes no Cursor or PageSize)",
                op_key=op_key, kind="spec",
            )
        if paged is False and op.paged:
            raise GoreloAPIError(
                f"{op_key} is a paged operation; use get_page or get_all so has_more and next_cursor are not lost",
                op_key=op_key, kind="spec",
            )
        if binary is True and not op.is_binary:
            raise GoreloAPIError(f"{op_key} is not a file download; use get_one", op_key=op_key, kind="spec")
        if binary is False and op.is_binary:
            raise GoreloAPIError(f"{op_key} is a file download; use get_binary", op_key=op_key, kind="spec")
        return op

    def _relative(self, op_path: str) -> str:
        """The op path as seen from base_url (httpx appends it): /v1/tickets becomes /tickets."""
        prefix = self._base_path
        if prefix and (op_path == prefix or op_path.startswith(prefix + "/")):
            return op_path[len(prefix):] or "/"
        return op_path

    def _fill_path(self, op: OpSpec, path_params: Mapping[str, Any] | None) -> str:
        """The op path with every id checked against the spec (spec.validate_path_param) and filled in.

        Unknown names and missing ids are reported first, then each id is validated: a uuid
        placeholder takes a UUID (sent in canonical form), an integer placeholder takes digits, any
        other placeholder takes one plain token. What survives is percent-encoded. Dot segments
        ("..") can therefore never reach the URL: httpx would collapse DELETE .../comments/.. into
        DELETE /v1/tickets/T, a forbidden operation (the wire path is compared with this one later).
        """
        placeholders = op.path_placeholders
        given = dict(path_params or {})
        for name in given:
            if name not in placeholders:
                expected = ", ".join(placeholders) or "none"
                raise SpecViolation(
                    op.key, str(name),
                    f"{op.key}: unknown path parameter '{name}'; this operation takes: {expected}",
                )
        for name in placeholders:
            value = given.get(name)
            if value is None or isinstance(value, bool) or not str(value).strip():
                raise SpecViolation(
                    op.key, name, f"{op.key}: path parameter '{name}' is required and must not be empty"
                )
        path = op.path
        for name in placeholders:
            text = validate_path_param(op, name, given[name])
            path = path.replace("{" + name + "}", quote(text, safe=""))
        return path

    def _prepare_query(self, op: OpSpec, query: Mapping[str, Any] | None) -> dict[str, Any]:
        """Join id lists with commas and write booleans as true/false.

        A None value stays in the result: normalize_query checks every NAME first and only then drops
        the None values, so a misspelled filter that is unset is still reported.
        """
        prepared: dict[str, Any] = {}
        for name, value in (query or {}).items():
            if value is None:
                prepared[name] = None
                continue
            if isinstance(value, bool):
                prepared[name] = "true" if value else "false"
            elif isinstance(value, (list, tuple)):
                prepared[name] = self._join_values(op, str(name), value)
            elif isinstance(value, datetime):
                if value.tzinfo is None or value.utcoffset() is None:
                    raise SpecViolation(
                        op.key, str(name),
                        f"{op.key}: query parameter '{name}' is a datetime without a UTC offset; "
                        "add Z or an offset such as -05:00",
                    )
                prepared[name] = _utc_z(value)
            else:
                prepared[name] = value
        return prepared

    @staticmethod
    def _join_values(op: OpSpec, name: str, values: list[Any] | tuple[Any, ...]) -> str:
        if not values:
            raise SpecViolation(
                op.key, name,
                f"{op.key}: query parameter '{name}' is an empty list; omit it to apply no filter",
            )
        parts: list[str] = []
        for item in values:
            if isinstance(item, bool) or not isinstance(item, (int, str)):
                raise SpecViolation(
                    op.key, name,
                    f"{op.key}: query parameter '{name}' takes integers or strings, got {_describe(item)}",
                )
            text = str(item).strip()
            if not text or "," in text:
                raise SpecViolation(
                    op.key, name,
                    f"{op.key}: query parameter '{name}' values must be non-empty and contain no comma",
                )
            parts.append(text)
        return ",".join(parts)

    def _prepare_body(
        self,
        op: OpSpec,
        json_body: Any,
        files: Mapping[str, Any] | None,
        form: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], set[str]]:
        if files is not None or form is not None:
            if json_body is not None:
                raise SpecViolation(op.key, "", f"{op.key}: pass json_body or files/form, not both")
            if not op.is_multipart:
                raise SpecViolation(
                    op.key, "", f"{op.key}: this operation takes a JSON body, not multipart form data"
                )
            files = dict(files or {})
            form = dict(form or {})
            if not files:
                raise SpecViolation(op.key, "", f"{op.key}: multipart upload needs at least one file part")
            both = sorted(set(files) & set(form))
            if both:
                raise SpecViolation(
                    op.key, both[0], f"{op.key}: form field '{both[0]}' is given both as a file and as text"
                )
            names = validate_body(op, {**form, **files})
            for field_name, value in form.items():
                if value is None or (isinstance(value, (str, bytes)) and not value.strip()):
                    raise SpecViolation(
                        op.key, str(field_name),
                        f"{op.key}: form field '{field_name}' must not be None or blank; "
                        "leave it out of form if there is nothing to send",
                    )
                if not isinstance(value, (str, bytes, int, float)):
                    raise SpecViolation(
                        op.key, str(field_name),
                        f"{op.key}: form field '{field_name}' takes text or a number, got {_describe(value)}",
                    )
            return {"files": files, "data": form or None}, set(names)
        if json_body is None:
            return {}, set()
        if op.is_multipart:
            raise SpecViolation(
                op.key, "", f"{op.key}: this operation takes multipart form data; use post_multipart()"
            )
        return {"json": json_body}, set(validate_body(op, json_body))

    def _with_paging(
        self, op: OpSpec, query: Mapping[str, Any] | None, size: int, cursor: str | None
    ) -> dict[str, Any]:
        # None values are kept on purpose: normalize_query checks every name (even an unset one)
        # before it drops them, and a paging name is refused here whatever its value.
        merged = dict(query or {})
        for name in merged:
            if str(name).lower() in ("pagesize", "cursor"):
                raise SpecViolation(
                    op.key, str(name),
                    f"{op.key}: do not put '{name}' in query; pass page_size= and cursor= to the helper",
                )
        names = {n.lower(): n for n in op.query_params}
        if "pagesize" in names:  # every paged op of the live spec has it; a future spec might not
            merged[names["pagesize"]] = size
        if isinstance(cursor, str) and cursor.strip():
            merged[names["cursor"]] = cursor.strip()
        elif cursor is not None and not isinstance(cursor, str):
            raise ValueError(f"cursor must be the next_cursor string from the previous page, got {cursor!r}")
        return merged

    def _to_page(self, op: OpSpec, envelope: dict[str, Any], size: int) -> Page:
        data = envelope.get("Data")
        context = envelope.get("DataContext")
        pagination = _ci_get(context, "Pagination")
        if pagination is not None and not isinstance(pagination, Mapping):
            raise _shape(op, f"Pagination is {_describe(pagination)}, expected an object")
        has_more = _ci_get(pagination, "HasMore")
        next_cursor = _ci_get(pagination, "NextCursor")
        total = _ci_get(pagination, "TotalCount")
        if pagination is not None:
            if not isinstance(has_more, bool):
                raise _shape(op, "Pagination.HasMore is missing or not a boolean")
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise _shape(op, "Pagination.NextCursor is not a string")
            if has_more and not next_cursor:
                raise _shape(op, "Gorelo says HasMore is true but sent no NextCursor")
            if total is not None and (isinstance(total, bool) or not isinstance(total, int)):
                raise _shape(op, "Pagination.TotalCount is not an integer")
        if data is None and pagination is not None and has_more is False and total == 0:
            data = []  # a nullable list with TotalCount 0 corroborates "no rows"
        if not isinstance(data, list):
            raise _shape(op, f"expected Data to be a list but got {_describe(data)}")
        if pagination is None:
            if data:
                raise _shape(op, "paged response without Pagination; cannot tell whether more rows exist")
            has_more, next_cursor, total = False, None, None
        return Page(
            items=data,
            next_cursor=next_cursor or None,
            has_more=bool(has_more),
            total_count=total,
            page_size=size,
        )

    async def _send_once(
        self,
        op: OpSpec,
        path: str,
        query_params: dict[str, Any],
        body_kwargs: dict[str, Any],
        *,
        binary: bool,
        max_bytes: int,
        expected: frozenset[str] = frozenset(DEFAULT_EXPECTED_CONTENT_TYPES),
    ) -> _Raw:
        if self._http is None or self._http.is_closed:
            raise RuntimeError(
                "GoreloClient is not started (or already closed): use 'async with GoreloClient(...) as client'"
            )
        headers = {"Accept": ", ".join([*sorted(expected), "application/json"])} if binary else None
        async with self._semaphore:
            try:
                request = self._http.build_request(
                    op.method,
                    self._relative(path),
                    params=query_params or None,
                    headers=headers,
                    **body_kwargs,
                )
                self._check_path_unchanged(op, request, path)
                response = await self._http.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise self._transport_failure(op, "timeout", exc) from None
            except httpx.RequestError as exc:
                raise self._transport_failure(op, "transport", exc) from None
            try:
                # A 2xx is a file only when it says it is one of the types the caller expects. Anything
                # else (an HTML gateway page, text/plain, JSON) is parsed as an envelope or refused. A
                # binary request reads EVERY body through the max_bytes cap, a download or not, so a
                # huge body of an unexpected type is never buffered whole just to be refused afterwards.
                is_download = (
                    binary
                    and 200 <= response.status_code < 300
                    and _main_type(response.headers.get("content-type")) in expected
                )
                if binary:
                    content = await self._read_capped(
                        op, response, max_bytes, is_download=is_download, expected=expected
                    )
                else:
                    content = await response.aread()
            except httpx.TimeoutException as exc:
                raise self._transport_failure(op, "timeout", exc) from None
            except httpx.RequestError as exc:
                raise self._transport_failure(op, "transport", exc) from None
            finally:
                await response.aclose()
        return _Raw(response.status_code, response.headers, content, is_download, expected if binary else None)

    def _check_path_unchanged(self, op: OpSpec, request: httpx.Request, path: str) -> None:
        """The path on the wire must be exactly the spec path with the ids filled in.

        Defence in depth for FORBIDDEN_OPS: if URL building ever rewrote the path (dot segments, for
        example), the request would no longer be the operation that was checked and logged.
        """
        expected = f"{self._base_path}{self._relative(path)}"
        actual = request.url.raw_path.split(b"?", 1)[0].decode("ascii", "replace")
        if actual != expected:
            raise GoreloAPIError(
                f"{op.key}: URL building changed the request path ({expected!r} became {actual!r}); refusing to send",
                op_key=op.key, kind="spec",
            )

    @staticmethod
    async def _read_capped(
        op: OpSpec,
        response: httpx.Response,
        max_bytes: int,
        *,
        is_download: bool = True,
        expected: frozenset[str] | None = None,
    ) -> bytes:
        """Read a body in a streaming fashion and stop as soon as it is larger than max_bytes.

        A declared Content-Length over the cap is refused before a byte is read. The refusal is kind
        "shape". For a download it says the download is too large. For any other body of a binary
        request (an unexpected content type, an error page) it is the usual "unexpected response
        shape; refusing to guess": that body would be refused anyway and must not be buffered first.
        """

        def too_big() -> GoreloAPIError:
            status = response.status_code
            if is_download:
                return GoreloAPIError(
                    f"{op.key}: the download is larger than the {max_bytes} byte cap (max_bytes); refusing to read it"
                    + _export_suffix(op),
                    status=status, op_key=op.key, kind="shape",
                    write_unconfirmed=is_side_effect_get(op.key),
                )
            ctype = _main_type(response.headers.get("content-type")) or "no content-type"
            unconfirmed = _may_have_applied(op, status)
            return GoreloAPIError(
                f"{op.key}: unexpected response shape; refusing to guess (HTTP {status}, content-type {ctype}, "
                f"the body is larger than the {max_bytes} byte cap (max_bytes) and was not read"
                f"{_wanted_hint(status, expected)})"
                + (_export_suffix(op) if unconfirmed else ""),
                status=status, op_key=op.key, kind="shape", write_unconfirmed=unconfirmed,
            )

        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise too_big()
        buffer = bytearray()
        async for chunk in response.aiter_bytes():
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                raise too_big()
        return bytes(buffer)

    def _transport_failure(self, op: OpSpec, kind: str, exc: Exception) -> GoreloAPIError:
        reason = "the request timed out" if kind == "timeout" else "the connection failed"
        detail = type(exc).__name__
        if is_side_effect_get(op.key):
            return GoreloAPIError(
                f"{op.key}: {reason} ({detail}) and Gorelo did not confirm the request; {EXPORT_NOTE}",
                op_key=op.key, kind=kind, write_unconfirmed=True,
            )
        if op.method == "GET":
            return GoreloAPIError(
                f"{op.key}: {reason} ({detail}); this was a read, nothing was changed",
                op_key=op.key, kind=kind,
            )
        return GoreloAPIError(
            f"{op.key}: {reason} ({detail}) and Gorelo did not confirm the write; it may or may not "
            "have been applied. Verify with a read before retrying",
            op_key=op.key, kind=kind, write_unconfirmed=True,
        )

    def _rate_limit_error(self, op: OpSpec, raw: _Raw, attempts: int, waited: float) -> GoreloAPIError:
        body = _parse_json(raw.content)
        envelope = body if isinstance(body, dict) else {}
        return GoreloAPIError(
            f"{op.key}: Gorelo is rate limiting (HTTP 429); gave up after {attempts} attempt(s) "
            f"and {waited:.1f}s of waiting",
            status=429, op_key=op.key, kind="rate_limit",
            notifications=_envelope_notifications(envelope), trace_id=_envelope_trace_id(envelope),
        )

    def _interpret(self, op: OpSpec, raw: _Raw) -> Any:
        status = raw.status
        success_status = 200 <= status < 300
        side_effect = is_side_effect_get(op.key)
        if raw.is_download:
            if not raw.content:
                raise _shape(
                    op, "the download is empty" + _export_suffix(op), status=status, write_unconfirmed=side_effect
                )
            return BinaryResult(
                content=raw.content,
                filename=_download_filename(raw.headers),
                content_type=raw.headers.get("content-type"),
            )
        parsed = _parse_json(raw.content)
        # A write may have been applied when Gorelo answered 2xx or 5xx without a usable answer. A
        # SIDE_EFFECT_GETS call (the PDF export) is recorded by Gorelo in the same situations.
        write_may_have_applied = _may_have_applied(op, status)
        if not (isinstance(parsed, dict) and isinstance(parsed.get("IsSuccess"), bool)):
            # Not a Gorelo envelope: a gateway page, an empty body, a bare list, ProblemDetails JSON...
            # Whatever the status, this client never guesses what it meant (never [] or {}).
            where = "" if success_status else f"HTTP {status}, "
            wanted = _wanted_hint(status, raw.expected)
            raise _shape(
                op,
                f"unexpected response shape; refusing to guess ({where}{_describe_body(parsed, raw)}{wanted})"
                + _problem_hint(parsed)
                + (_export_suffix(op) if write_may_have_applied else ""),
                status=status,
                write_unconfirmed=write_may_have_applied,
            )
        notifications = _envelope_notifications(parsed)
        trace_id = _envelope_trace_id(parsed)
        if not success_status:
            raise GoreloAPIError(
                _failure_text(op, status, notifications) + (_export_suffix(op) if write_may_have_applied else ""),
                status=status, op_key=op.key, kind="http", notifications=notifications,
                trace_id=trace_id, write_unconfirmed=write_may_have_applied,
            )
        if not parsed["IsSuccess"]:
            raise GoreloAPIError(
                _failure_text(op, status, notifications),
                status=status, op_key=op.key, kind="envelope", notifications=notifications, trace_id=trace_id,
            )
        return parsed


# --------------------------------------------------------------------------
# Module level helpers used by the client
# --------------------------------------------------------------------------


def _require_tool(tool: Any) -> None:
    if not isinstance(tool, str) or not tool.strip():
        raise ValueError("tool= must be the name of the calling tool (it labels the log line)")


def _parse_json(content: bytes) -> Any:
    if not content:
        return _MISSING
    try:
        return json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return _MISSING


def _describe(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    return type(value).__name__


def _describe_body(parsed: Any, raw: _Raw) -> str:
    """What the body was, without quoting any of it."""
    ctype = (raw.headers.get("content-type") or "no content-type").split(";", 1)[0].strip()
    if parsed is _MISSING:
        kind = "an empty body" if not raw.content else "a body that is not JSON"
        return f"{kind}, content-type {ctype}, {len(raw.content)} bytes"
    if isinstance(parsed, dict):
        return f"a JSON object without a boolean IsSuccess, content-type {ctype}"
    return f"JSON {_describe(parsed)} instead of an envelope, content-type {ctype}"


def _problem_hint(parsed: Any) -> str:
    """Names (never values) from an ASP.NET problem-details style error body."""
    if not isinstance(parsed, dict):
        return ""
    title = parsed.get("title")
    errors = parsed.get("errors")
    parts: list[str] = []
    if isinstance(title, str) and title.strip():
        parts.append(f"title: {title.strip()[:120]}")
    if isinstance(errors, dict) and errors:
        parts.append("fields: " + ", ".join(sorted(str(k) for k in errors)[:10]))
    return f" [{'; '.join(parts)}]" if parts else ""


def _envelope_notifications(envelope: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = _ci_get(envelope, "Notifications")
    return [_notification(item) for item in items] if isinstance(items, list) else []


def _envelope_trace_id(envelope: Mapping[str, Any]) -> str | None:
    trace = _ci_get(_ci_get(envelope, "DataContext"), "TraceId")
    return trace if isinstance(trace, str) and trace else None


def _failure_text(op: OpSpec, status: int, notifications: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for note in notifications[:3]:
        label = f"{note['property']}: " if note.get("property") else ""
        parts.append(f"{label}{note.get('message') or note.get('code') or 'no message'}")
    if len(notifications) > 3:
        parts.append(f"and {len(notifications) - 3} more")
    detail = "; ".join(parts) if parts else "no details returned"
    return f"{op.key}: HTTP {status}: {detail}"


def _export_suffix(op: OpSpec) -> str:
    """For a SIDE_EFFECT_GETS call that failed after Gorelo may have recorded it: say so. Else nothing."""
    return f" ({EXPORT_NOTE})" if is_side_effect_get(op.key) else ""


def _may_have_applied(op: OpSpec, status: int) -> bool:
    """True when Gorelo may have applied the call although its answer was unusable: a non-GET, or a
    SIDE_EFFECT_GETS call (the PDF export is recorded as an event), answered with 2xx or 5xx. A 4xx
    is a refusal: nothing was applied."""
    return (op.method != "GET" or is_side_effect_get(op.key)) and (200 <= status < 300 or status >= 500)


def _wanted_hint(status: int, expected: frozenset[str] | None) -> str:
    """For a 2xx answer to a binary request that was not one of the expected downloads: what was expected."""
    if expected and 200 <= status < 300:
        return f"; a download of type {', '.join(sorted(expected))} was expected"
    return ""


def _shape(
    op: OpSpec, detail: str, *, status: int | None = 200, write_unconfirmed: bool = False
) -> GoreloAPIError:
    return GoreloAPIError(
        f"{op.key}: {detail}",
        status=status, op_key=op.key, kind="shape", write_unconfirmed=write_unconfirmed,
    )
