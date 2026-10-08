"""Fixtures and helpers shared by the whole offline suite. No test may touch the network.

Test modules import the helpers directly (pytest puts tests/ on sys.path):

    from conftest import MockGorelo, call_tool, call_tool_error, envelope, error_envelope, paged_envelope

Fixtures
    (autouse)        _block_network: no socket can connect. _quiet_fastmcp_logs: the "fastmcp" and "mcp" loggers
                     are raised to CRITICAL for the duration of each test and restored afterwards, because FastMCP
                     renders a Rich traceback for every ToolError (40 to 115 ms per error-path test). A test that
                     asserts on those loggers lowers the level itself inside the test (the fixture still restores
                     the previous level at teardown), or sits in a module listed in LOGGING_TEST_MODULES.
    anyio_backend    "asyncio" (mark async tests with `pytestmark = pytest.mark.anyio`).
    spec_index       the real spec/spec_index.json, loaded once.
    mock_gorelo      a fresh MockGorelo; at teardown it fails the test if a request had no route.
    make_settings    factory: make_settings(**overrides) -> Settings (every toolset on, destructive off).
    server_factory   factory: server_factory(mock=None, registry=None, event_hooks=None, **settings) ->
                     FastMCP built by server.build_server on top of the mock transport.
    client_factory   factory: client_factory(mock=None, **GoreloClient kwargs) -> GoreloClient (not started:
                     use `async with`). Its sleep is the recording fake_sleep, so 429 waits cost no time.
    fake_sleep       FakeSleep: the injected sleep; `.delays` lists every wait that was requested.

Helpers
    MockGorelo       route table keyed by (method, path) that records every request (RecordedRequest:
                     method, path, raw_path, query, query_multi, json, headers, files, form, content).
                     Routes use the full spec path including /v1 and may contain {placeholders}:
                     mock.on("GET", "/v1/clients/{clientId}", response, query=None, status=None, headers=None)
                     or mock.on_op("GET /v1/clients/{clientId}", response). Also mock.requests, mock.last,
                     mock.calls(method, path), mock.unmatched, mock.reset(), mock.transport.
    envelope, paged_envelope, pagination, notification, error_envelope
                     builders for Gorelo's PascalCase envelope; an envelope's StatusCode is the HTTP
                     status MockGorelo serves (error_envelope(400, ...) answers 400).
    paged_responder  a responder serving a list of pages through Cursor / NextCursor.
    in_order         a response spec that serves one response per request, in order.
    call_tool        call a tool through an in-process fastmcp Client; returns the structured result.
    call_tool_error  same, but asserts the tool failed and returns the error text.
    call_tool_outcome  same, but returns a ToolOutcome (is_error, data, error) whichever way it went.
    call_tool_raw    the raw fastmcp CallToolResult (for tools that return a ToolResult).
    list_tools       the mcp Tool objects a client sees (name, inputSchema, annotations, ...).
    make_ctx         a minimal ctx for calling a decorated tool function directly.
    uid              uid(n) is a valid UUID string, distinct per n: uid(7) == "00000007-aaaa-4bbb-8ccc-000000000007".
                     Path ids are validated against the spec before any HTTP call: a uuid placeholder (ticketId,
                     projectId, invoiceId, ...) refuses "t" or "abc", so tests must use uid(n) for them.
    path_params_for  path_params_for(op) gives a valid id for every placeholder of an OpSpec, by spec type
                     (uuid -> uid(n), integer -> n, untyped token -> "id-<name>").

Every helper that takes a server also accepts an open `fastmcp.Client` (use one `async with Client(server)`
to share a single lifespan across several calls).
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from fastmcp import Client, FastMCP

from gorelo_client import GoreloClient
from server import build_server
from settings import TOOLSETS, Settings
from spec import SpecIndex, load_spec_index
from tools._common import Registry

TEST_API_KEY = "test-api-key-not-real"
TEST_TRACE_ID = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"


# --------------------------------------------------------------------------
# Plumbing fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


class NetworkBlocked(RuntimeError):
    """Raised when something in the offline suite tries to open a network connection."""


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise NetworkBlocked("network access is blocked in the offline test suite")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# The logger trees FastMCP and the MCP SDK log through. FastMCP's handler renders a Rich traceback for
# every tool error it logs, which dominates the run time of the error-path tests.
QUIET_LOGGERS = ("fastmcp", "mcp")

# Test modules that assert on FastMCP's own log records: they need those loggers as FastMCP configured them.
LOGGING_TEST_MODULES = frozenset({"test_log_filter"})


@contextmanager
def quiet_loggers(names: Iterable[str] = QUIET_LOGGERS, level: int = logging.CRITICAL) -> Iterator[None]:
    """Raise the named loggers to `level` (default CRITICAL) and restore each one's previous level on exit,
    also when the body raises or changed the level itself. A logger that had no level of its own
    (NOTSET, inheriting) gets NOTSET back."""
    loggers = [logging.getLogger(name) for name in names]
    previous = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(level)
    try:
        yield
    finally:
        for logger, before in zip(loggers, previous):
            logger.setLevel(before)


@pytest.fixture(autouse=True)
def _quiet_fastmcp_logs(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.module.__name__ in LOGGING_TEST_MODULES:
        yield
        return
    with quiet_loggers():
        yield


@pytest.fixture(scope="session")
def spec_index() -> SpecIndex:
    return load_spec_index()


# --------------------------------------------------------------------------
# Envelope builders
# --------------------------------------------------------------------------


def pagination(
    next_cursor: str | None = None,
    total_count: int | None = None,
    *,
    has_more: bool | None = None,
    previous_cursor: str | None = None,
    has_previous: bool = False,
) -> dict[str, Any]:
    """DataContext.Pagination. HasMore defaults to "there is a next cursor"."""
    return {
        "NextCursor": next_cursor,
        "PreviousCursor": previous_cursor,
        "HasMore": (next_cursor is not None) if has_more is None else has_more,
        "HasPrevious": has_previous,
        "TotalCount": total_count,
    }


def notification(code: str, message: str, property: str | None = None) -> dict[str, Any]:
    """One entry of Notifications (Code, Message, PropertyName, ActionHint, DocUrl)."""
    return {"Code": code, "Message": message, "PropertyName": property, "ActionHint": None, "DocUrl": None}


def envelope(
    data: Any = None,
    pagination: Mapping[str, Any] | None = None,
    *,
    status: int = 200,
    notifications: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """A successful Gorelo envelope. Pass pagination=pagination(...) for a paged list."""
    return {
        "StatusCode": status,
        "IsSuccess": True,
        "Data": data,
        "DataContext": {"Pagination": dict(pagination)} if pagination is not None else None,
        "Notifications": [dict(n) for n in notifications or []],
    }


def paged_envelope(
    items: list[Any], *, next_cursor: str | None = None, total_count: int | None = None
) -> dict[str, Any]:
    """A paged list envelope; TotalCount defaults to len(items)."""
    total = len(items) if total_count is None else total_count
    return envelope(items, pagination(next_cursor, total))


def error_envelope(
    status: int,
    notifications: Iterable[Any] = (),
    trace_id: str | None = TEST_TRACE_ID,
) -> dict[str, Any]:
    """A failed envelope. Each notification is a notification(...) dict or a (code, message[, property])
    tuple. Served with HTTP status `status` unless MockGorelo.on(..., status=...) says otherwise."""
    built = [n if isinstance(n, Mapping) else notification(*n) for n in notifications]
    return {
        "StatusCode": status,
        "IsSuccess": False,
        "Data": None,
        "DataContext": {"TraceId": trace_id} if trace_id else None,
        "Notifications": built,
    }


def paged_responder(
    pages: list[list[Any]], *, total_count: int | None = None
) -> Callable[[RecordedRequest], dict[str, Any]]:
    """A responder that serves `pages` through cursors: no Cursor gets page 0 with NextCursor "c1",
    Cursor=c1 gets page 1, and so on. The last page has HasMore false."""
    total = sum(len(p) for p in pages) if total_count is None else total_count

    def respond(request: RecordedRequest) -> dict[str, Any]:
        cursor = request.query.get("Cursor")
        index = int(cursor[1:]) if cursor else 0
        last = index >= len(pages) - 1
        return envelope(pages[index], pagination(None if last else f"c{index + 1}", total, has_more=not last))

    return respond


# --------------------------------------------------------------------------
# MockGorelo: a route table on top of httpx.MockTransport that records every request
# --------------------------------------------------------------------------


@dataclass
class RecordedRequest:
    method: str
    path: str  # decoded, includes /v1, for example /v1/tickets/123
    raw_path: str  # as sent on the wire (percent-encoded), without the query string
    url: str
    query: dict[str, str]  # last value of each query name, exact casing as sent
    query_multi: dict[str, list[str]]
    headers: httpx.Headers  # case-insensitive
    content: bytes
    json: Any = None  # parsed JSON body, None if there was none
    form: dict[str, str] = field(default_factory=dict)  # multipart text fields
    files: dict[str, tuple[str | None, bytes, str | None]] = field(default_factory=dict)  # name -> (filename, bytes, content type)

    @property
    def query_names(self) -> list[str]:
        return sorted(self.query)


class UnexpectedRequest(AssertionError):
    """A request reached MockGorelo with no matching route."""


class _InOrder:
    def __init__(self, responses: tuple[Any, ...]):
        self._responses = list(responses)
        self._served = 0

    def next(self) -> Any:
        if self._served >= len(self._responses):
            raise UnexpectedRequest(f"more requests than the {len(self._responses)} responses in_order() provides")
        response = self._responses[self._served]
        self._served += 1
        return response


def in_order(*responses: Any) -> _InOrder:
    """A response spec that serves `responses` one per request, in order (more requests than
    responses fail the test). Each entry is anything MockGorelo.on accepts."""
    return _InOrder(responses)


@dataclass
class _Route:
    method: str
    segments: tuple[str, ...]
    response: Any
    query: dict[str, str] | None
    status: int | None
    headers: dict[str, str] | None
    order: int
    calls: int = 0


def _segments(path: str) -> tuple[str, ...]:
    return tuple(unquote(part) for part in path.strip("/").split("/"))


def _is_placeholder(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _parse_multipart(content_type: str, content: bytes) -> tuple[dict[str, str], dict[str, tuple[str | None, bytes, str | None]]]:
    header = b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("latin-1") + b"\r\n\r\n"
    message = BytesParser(policy=policy.default).parsebytes(header + content)
    form: dict[str, str] = {}
    files: dict[str, tuple[str | None, bytes, str | None]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is None:
            form[str(name)] = payload.decode("utf-8")
        else:
            part_type = part.get_content_type() if part["content-type"] else None
            files[str(name)] = (filename, payload, part_type)
    return form, files


class MockGorelo:
    """A fake Gorelo API. Register routes with on(), build the server or client on `transport`,
    then inspect `requests`.

    Response specs accepted by on():
        dict or list      JSON body. The HTTP status is the dict's StatusCode (200 if absent) unless
                          status= is given. Use envelope(), paged_envelope(), error_envelope().
        httpx.Response    served as is (copied per request): for non-JSON bodies, headers, 429s.
        callable          called with the RecordedRequest; may return any spec listed here.
        in_order(...)     one spec per request, in order.
        an exception      raised from the transport: httpx.ReadTimeout("...") simulates a timeout.
    A request with no matching route raises UnexpectedRequest (and is kept in `unmatched`).
    """

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.unmatched: list[RecordedRequest] = []
        self._routes: list[_Route] = []
        self.transport = httpx.MockTransport(self._handle)

    # -- registration ------------------------------------------------------

    def on(
        self,
        method: str,
        path: str,
        response: Any,
        *,
        query: Mapping[str, str] | None = None,
        status: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> _Route:
        """Route `method path` to `response`. `query` (optional) must be a subset of the request's
        query string for the route to match; routes with more constraints are tried first."""
        route = _Route(
            method=method.upper(),
            segments=_segments(path),
            response=response,
            query=dict(query) if query else None,
            status=status,
            headers=dict(headers) if headers else None,
            order=len(self._routes),
        )
        self._routes.append(route)
        return route

    def on_op(self, op_key: str, response: Any, **options: Any) -> _Route:
        """on() with an operation key such as "GET /v1/clients/{clientId}"."""
        method, path = op_key.split(" ", 1)
        return self.on(method, path, response, **options)

    # -- inspection --------------------------------------------------------

    @property
    def last(self) -> RecordedRequest:
        assert self.requests, "no request reached MockGorelo"
        return self.requests[-1]

    def calls(self, method: str | None = None, path: str | None = None) -> list[RecordedRequest]:
        """Recorded requests, optionally only one method and path (a path may use {placeholders})."""
        wanted = _segments(path) if path is not None else None
        found = []
        for request in self.requests:
            if method is not None and request.method != method.upper():
                continue
            if wanted is not None and not self._segments_match(wanted, _segments(request.path)):
                continue
            found.append(request)
        return found

    def reset(self) -> None:
        self.requests.clear()
        self.unmatched.clear()

    # -- the transport handler ---------------------------------------------

    @staticmethod
    def _segments_match(route: tuple[str, ...], actual: tuple[str, ...]) -> bool:
        if len(route) != len(actual):
            return False
        return all(_is_placeholder(r) and bool(a) or r == a for r, a in zip(route, actual))

    def _record(self, request: httpx.Request) -> RecordedRequest:
        multi: dict[str, list[str]] = {}
        for name, value in request.url.params.multi_items():
            multi.setdefault(name, []).append(value)
        content = request.content
        content_type = request.headers.get("content-type", "")
        body_json: Any = None
        form: dict[str, str] = {}
        files: dict[str, tuple[str | None, bytes, str | None]] = {}
        if content and "json" in content_type:
            try:
                body_json = json.loads(content)
            except ValueError:
                body_json = None
        elif content and content_type.startswith("multipart/form-data"):
            form, files = _parse_multipart(content_type, content)
        return RecordedRequest(
            method=request.method,
            path=request.url.path,
            raw_path=request.url.raw_path.decode("ascii").split("?", 1)[0],
            url=str(request.url),
            query={name: values[-1] for name, values in multi.items()},
            query_multi=multi,
            headers=httpx.Headers(request.headers),
            content=content,
            json=body_json,
            form=form,
            files=files,
        )

    def _find_route(self, recorded: RecordedRequest) -> _Route | None:
        actual = _segments(recorded.raw_path)
        candidates = []
        for route in self._routes:
            if route.method != recorded.method or not self._segments_match(route.segments, actual):
                continue
            if route.query and any(recorded.query.get(k) != v for k, v in route.query.items()):
                continue
            literal = sum(1 for s in route.segments if not _is_placeholder(s))
            candidates.append((-(len(route.query or {})), -literal, route.order, route))
        if not candidates:
            return None
        return min(candidates, key=lambda c: c[:3])[3]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        recorded = self._record(request)
        self.requests.append(recorded)
        route = self._find_route(recorded)
        if route is None:
            self.unmatched.append(recorded)
            known = ", ".join(sorted({f"{r.method} /{'/'.join(r.segments)}" for r in self._routes})) or "none"
            raise UnexpectedRequest(
                f"MockGorelo has no route for {recorded.method} {recorded.path}"
                f"{'?' + str(request.url.query, 'ascii') if request.url.query else ''}; routes: {known}"
            )
        route.calls += 1
        return self._to_response(route.response, request, recorded, route)

    def _to_response(self, spec: Any, request: httpx.Request, recorded: RecordedRequest, route: _Route) -> httpx.Response:
        if isinstance(spec, _InOrder):
            spec = spec.next()
        if isinstance(spec, BaseException) or (isinstance(spec, type) and issubclass(spec, BaseException)):
            error = spec() if isinstance(spec, type) else spec
            if isinstance(error, httpx.RequestError):
                error.request = request
            raise error
        if isinstance(spec, httpx.Response):
            try:
                content = spec.content
            except httpx.ResponseNotRead:
                return spec  # a streaming response: served as is, so it can be used once
            return httpx.Response(
                spec.status_code if route.status is None else route.status,
                headers={**dict(spec.headers), **(route.headers or {})},
                content=content,
            )
        if callable(spec):
            return self._to_response(spec(recorded), request, recorded, route)
        if isinstance(spec, (dict, list)):
            if route.status is not None:
                status = route.status
            elif isinstance(spec, dict) and isinstance(spec.get("StatusCode"), int):
                status = spec["StatusCode"]
            else:
                status = 200
            return httpx.Response(status, json=spec, headers=route.headers)
        raise TypeError(f"unsupported MockGorelo response spec: {spec!r}")


@pytest.fixture
def mock_gorelo() -> Iterable[MockGorelo]:
    mock = MockGorelo()
    yield mock
    assert not mock.unmatched, "requests with no mock route: " + ", ".join(
        f"{r.method} {r.path}" for r in mock.unmatched
    )


# --------------------------------------------------------------------------
# Settings, servers, clients
# --------------------------------------------------------------------------


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "api_key": TEST_API_KEY,
            "public_base_url": "https://mcp.example.test",
            "mcp_auth_password": "test-password-not-real",
            "toolsets": frozenset(TOOLSETS),
            "destructive": False,
        }
        values.update(overrides)
        if not isinstance(values["toolsets"], frozenset):
            values["toolsets"] = frozenset(values["toolsets"])
        return Settings(**values)

    return factory


@pytest.fixture
def server_factory(
    make_settings: Callable[..., Settings], spec_index: SpecIndex, mock_gorelo: MockGorelo
) -> Callable[..., FastMCP]:
    def factory(
        mock: MockGorelo | httpx.AsyncBaseTransport | None = None,
        *,
        registry: Registry | None = None,
        settings: Settings | None = None,
        event_hooks: dict[str, list[Any]] | None = None,
        **overrides: Any,
    ) -> FastMCP:
        chosen = mock if mock is not None else mock_gorelo
        transport = chosen.transport if isinstance(chosen, MockGorelo) else chosen
        return build_server(
            settings or make_settings(**overrides),
            transport=transport,
            event_hooks=event_hooks,
            spec=spec_index,
            registry=registry,
        )

    return factory


class FakeSleep:
    """A drop-in for asyncio.sleep that records the requested delays and returns at once."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def fake_sleep() -> FakeSleep:
    return FakeSleep()


@pytest.fixture
def client_factory(spec_index: SpecIndex, mock_gorelo: MockGorelo, fake_sleep: FakeSleep) -> Callable[..., GoreloClient]:
    def factory(
        mock: MockGorelo | httpx.AsyncBaseTransport | None = None, api_key: str = TEST_API_KEY, **kwargs: Any
    ) -> GoreloClient:
        chosen = mock if mock is not None else mock_gorelo
        transport = chosen.transport if isinstance(chosen, MockGorelo) else chosen
        kwargs.setdefault("sleep", fake_sleep)
        kwargs.setdefault("spec", spec_index)
        return GoreloClient(api_key, transport=transport, **kwargs)

    return factory


# --------------------------------------------------------------------------
# Calling tools in process
# --------------------------------------------------------------------------


@asynccontextmanager
async def _session(target: FastMCP | Client):
    if isinstance(target, Client):
        yield target
    else:
        async with Client(target) as client:
            yield client


async def call_tool_raw(target: FastMCP | Client, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
    """The fastmcp CallToolResult (is_error, content, structured_content, data) without raising.

    `target` is a FastMCP server (a fresh in-process session is opened for the call) or an open
    fastmcp Client (use it to share one lifespan across several calls)."""
    async with _session(target) as client:
        return await client.call_tool(name, dict(arguments or {}), raise_on_error=False)


def _error_text(result: Any) -> str:
    return "\n".join(getattr(block, "text", "") for block in result.content)


@dataclass
class ToolOutcome:
    """is_error False: `data` is the structured result. is_error True: `error` is the ToolError text."""

    is_error: bool
    data: Any = None
    error: str = ""


async def call_tool_outcome(target: FastMCP | Client, name: str, arguments: Mapping[str, Any] | None = None) -> ToolOutcome:
    """Call a tool and report what happened without asserting either way."""
    result = await call_tool_raw(target, name, arguments)
    if result.is_error:
        return ToolOutcome(is_error=True, error=_error_text(result))
    return ToolOutcome(is_error=False, data=result.structured_content)


async def call_tool(target: FastMCP | Client, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
    """Call a tool and return its structured result (a dict). A tool error fails the test."""
    result = await call_tool_raw(target, name, arguments)
    assert not result.is_error, f"tool {name} failed: {_error_text(result)}"
    return result.structured_content


async def call_tool_error(target: FastMCP | Client, name: str, arguments: Mapping[str, Any] | None = None) -> str:
    """Call a tool that must fail and return the error text the model would see."""
    result = await call_tool_raw(target, name, arguments)
    assert result.is_error, f"tool {name} was expected to fail but returned {result.structured_content!r}"
    return _error_text(result)


async def list_tools(target: FastMCP | Client) -> list[Any]:
    """The tools a client sees (mcp.types.Tool objects: name, inputSchema, annotations, ...)."""
    async with _session(target) as client:
        return await client.list_tools()


def uid(n: int = 1) -> str:
    """A valid UUID string for tests, distinct per `n` (uid(7) is 00000007-aaaa-4bbb-8ccc-000000000007)."""
    return f"{n:08d}-aaaa-4bbb-8ccc-{n:012d}"


def path_params_for(op: Any) -> dict[str, Any]:
    """Valid ids for every {placeholder} of `op` (an OpSpec), chosen by what the spec says it is."""
    params: dict[str, Any] = {}
    for position, name in enumerate(op.path_placeholders, start=1):
        entry = op.path_params.get(name) or {}
        if entry.get("format") == "uuid":
            params[name] = uid(position)
        elif entry.get("type") == "integer":
            params[name] = position
        else:
            params[name] = f"id-{name}"
    return params


def make_ctx(client: GoreloClient | None = None, **lifespan: Any) -> SimpleNamespace:
    """A minimal ctx for calling a decorated tool function directly, without a server:
    `await my_tool(make_ctx(started_client), ...)`."""
    state = dict(lifespan)
    if client is not None:
        state["gorelo"] = client
    return SimpleNamespace(lifespan_context=state)
