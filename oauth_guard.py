"""The login gate: everything that decides whether a request may reach the consent page or the registration endpoint.

`personal_auth.PersonalAuthProvider` builds the OAuth endpoints and the consent page; this module holds the rules it
applies, so that each one is small, has no framework state and is tested on its own (`tests/test_personal_auth_gate.py`).

* Failure limiter (`FailureLimiter`): wrong passwords are counted per client address, not per client_id, because a
  client_id is whatever the caller types. An IPv4 address counts as itself, an IPv6 address as its /64. Five wrong
  passwords per address in 15 minutes and thirty in an hour across all addresses end in HTTP 429, for the right password
  as well. At most 4096 addresses are remembered. `AttemptLimiter.block` says which of the two limits stopped a request
  (scope "ip" or "global") and for how long, so that the journal can tell a guesser at one address from the overall cap
  that locks every address out, the operator's included.
* Registration limiter (`RegistrationLimiter`): registration is open (no credential), so it is capped per address
  (10 an hour) and overall (50 a day), and the number of stored clients is capped (`MAX_CLIENTS`).
* Body cap (`BodyLimit`): a request to /authorize, /token, /register or /revoke with a body over 16 KiB is refused with
  413 before the handler reads any of it, whatever its HTTP method (the routes also answer OPTIONS, and the framework's
  handlers read the body of any request that reaches them). An OPTIONS request that carries a body at all is refused with
  400: a CORS preflight has none. A bodyless OPTIONS goes on, so a preflight is answered as it always was. It is never
  put in front of /mcp.
* Registration gate (`RegistrationGate`): every request that reaches the registration handler is counted against the
  registration limiter and is read and checked first, so that a body that is not JSON (any exception of the parser, a
  number of more than 4300 digits included) or metadata that no UTF-8 JSON writer can keep (a lone surrogate in any string,
  nesting deeper than `MAX_METADATA_DEPTH`) is answered with a clean 400 and never reaches the handler. Only a CORS
  preflight, which the framework's CORS layer answers by itself, is left uncounted (`is_cors_preflight`).
* Form guard (`FormGuard`): the token and revocation handlers read their parameters with the framework's form parser, which
  answers a multipart body it cannot read (a declared boundary and anything else) with an exception that nothing turns into
  an answer, so a multipart body is parsed first and one that does not parse is a clean 400 before the handler runs.
* Revocation mark (`RevocationMark`, `revoking`): while /revoke is served the provider knows it, because the framework's
  revocation handler calls the token loaders with whatever token the caller sent, and a loader must not take that for a
  refresh, an expired session or a replay.
* Redirect validator (`redirect_problem`): the one definition of an acceptable redirect address, applied to the
  pydantic-normalized string (the form that is stored and redirected to) at registration and again at /authorize.
* Safe log values (`safe`, `safe_host`): attacker-controlled text reaches the journal only as printable ASCII, escaped
  and shortened.
* Consent response headers and the fixed error pages (`consent_headers`, `INVALID_REQUEST_PAGE`, ...): the same bytes
  every time, whatever the request said.
* Fail closed (`check_framework_versions`, `require_password`, `approved`, `take_approval`): the gate refuses to start on
  a framework version it was not checked against or without a password, and the provider's authorize() only works inside
  a request that the consent endpoint approved after the password matched.

Nothing here reads a file, the environment or the network, and nothing logs a password, a token or a secret.
"""

from __future__ import annotations

import contextvars
import hmac
import importlib.metadata
import ipaddress
import json
import logging
import math
import re
import unicodedata
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from pydantic import AnyUrl
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("oauth-guard")

# --------------------------------------------------------------------------
# The numbers
# --------------------------------------------------------------------------

FAILURES_PER_IP = 5  # wrong passwords per client address ...
FAILURE_WINDOW_SECONDS = 15 * 60  # ... in this many seconds
FAILURES_OVERALL = 30  # wrong passwords from all addresses together ...
FAILURE_OVERALL_WINDOW_SECONDS = 60 * 60  # ... in this many seconds
MAX_LIMITER_KEYS = 4096  # addresses a limiter remembers

REGISTRATIONS_PER_IP = 10
REGISTRATION_IP_WINDOW_SECONDS = 60 * 60
REGISTRATIONS_OVERALL = 50
REGISTRATION_OVERALL_WINDOW_SECONDS = 24 * 60 * 60

MAX_CLIENTS = 50  # registered clients kept
EVICTION_MIN_AGE_SECONDS = 24 * 60 * 60  # a client must be older than this to be evicted for a new one

MAX_BODY_BYTES = 16 * 1024
BODY_LIMITED_PATHS = ("/authorize", "/token", "/register", "/revoke")
# How deeply the JSON of a client's registration may nest. Real client metadata nests five levels at most (a key set in
# "jwks": the body, jwks, keys, a key, its x5c list); the serializers fail somewhere above two hundred, and a client that
# one of them cannot write would end in a 500 or, worse, stay stored.
MAX_METADATA_DEPTH = 16

MAX_FIELD_CHARS = 4096  # one form or query value
MAX_REDIRECT_CHARS = 2048
LOG_VALUE_LIMIT = 80
MIN_PASSWORD_CHARS = 16

# What the gate was checked against: the series of each framework package (3.2 means 3.2 and 3.2.x).
SUPPORTED_VERSIONS = (("fastmcp", "3.2"), ("mcp", "1.27"))


class GateError(RuntimeError):
    """The login gate cannot be made safe: the service must refuse to start."""


class VersionGuardError(GateError):
    """The installed fastmcp or mcp is not a version the gate was checked against."""


# --------------------------------------------------------------------------
# Fail closed: framework versions and the password
# --------------------------------------------------------------------------


def installed_version(name: str) -> str | None:
    """The installed version of a package, or None when it is not installed or its metadata cannot be read (either way the
    guard refuses to start, with its own message instead of a traceback)."""
    try:
        return importlib.metadata.version(name)
    except Exception:
        return None


def check_framework_versions() -> None:
    """Raise VersionGuardError unless fastmcp is 3.2.x and mcp is 1.27.x. The gate swaps one route of the framework's
    OAuth server for its own and wraps three others, so a release of either package that changes how those routes are
    built could leave the password page out of the path without any error. Refusing to start is the safe answer."""
    problems = []
    for name, series in SUPPORTED_VERSIONS:
        found = installed_version(name)
        if found is None:
            problems.append(f"{name} is not installed (the gate was checked against {series}.x)")
        elif not (found == series or found.startswith(series + ".")):
            problems.append(f"{name} {found} is installed but the gate was checked only against {series}.x")
    if problems:
        raise VersionGuardError("; ".join(problems) + "; refusing to build the consent gate on an unchecked framework")


def require_password(password: object) -> str:
    """The password, or GateError when there is none that could be compared: not text, blank, or text that cannot be
    encoded as UTF-8. Surrounding spaces are part of a password and are kept."""
    if not isinstance(password, str) or not password.strip():
        raise GateError("the login password is required and must not be blank; the consent page cannot run without one")
    try:
        unicodedata.normalize("NFC", password).encode("utf-8")
    except UnicodeError:
        raise GateError("the login password cannot be encoded as UTF-8; the consent page cannot run with it") from None
    return password


def password_is_short(password: str) -> bool:
    """True for a password with fewer than MIN_PASSWORD_CHARS characters (counted after NFC normalization)."""
    return len(unicodedata.normalize("NFC", password)) < MIN_PASSWORD_CHARS


def password_matches(submitted: object, configured: object) -> bool:
    """Compare the submitted password with the configured one in constant time, as UTF-8 bytes of the NFC form of each,
    so that the same password typed with composed or decomposed accents is the same password. Never raises: text that
    cannot be encoded, or a missing or blank configured password, does not match."""
    if not isinstance(submitted, str) or not isinstance(configured, str) or not configured.strip():
        return False
    try:
        left = unicodedata.normalize("NFC", submitted).encode("utf-8")
        right = unicodedata.normalize("NFC", configured).encode("utf-8")
    except UnicodeError:
        return False
    return hmac.compare_digest(left, right)


# --------------------------------------------------------------------------
# Limiters
# --------------------------------------------------------------------------


def client_key(host: object) -> str:
    """The key a client address is counted under: an IPv4 address as itself, an IPv6 address as its /64 network (so
    that one machine cannot choose a fresh address inside its own range for every attempt), an IPv4-mapped IPv6 address
    as the IPv4 address. Anything that is not an address (no client in the ASGI scope, a test name) shares one key."""
    if not isinstance(host, str):
        return "?"
    try:
        address = ipaddress.ip_address(host.strip())
    except ValueError:
        return "?"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return f"{ipaddress.IPv6Address((int(address) >> 64) << 64).compressed}/64"
    return str(address)


SCOPE_IP = "ip"  # the address itself is over its own limit
SCOPE_GLOBAL = "global"  # the limit over all addresses together is reached, whoever asks


@dataclass(frozen=True)
class Blocked:
    """Why one more event from an address is refused. `scope` is SCOPE_GLOBAL when the overall limit is reached (then the
    cause is not this address: every address is refused until the oldest event leaves the window) and SCOPE_IP when only
    this address is over its own limit. `retry_in` is the number of seconds, at least 1, until the limiter lets this
    address through again: the longer of the waits for the limits that apply."""

    scope: str
    retry_in: int


class AttemptLimiter:
    """Counts events per client address and overall inside sliding windows, and remembers a bounded number of addresses.

    `block` says whether one more event from `host` is over a limit, and which one; `is_blocked` is the yes or no of it;
    `record` adds one. The caller passes the time (the provider's clock), so tests move it by hand. Nothing here awaits,
    so a check and the record that follows it cannot be interleaved with another request on the event loop."""

    def __init__(
        self,
        *,
        per_key: int,
        per_key_window: float,
        overall: int,
        overall_window: float,
        max_keys: int = MAX_LIMITER_KEYS,
    ) -> None:
        self.per_key = per_key
        self.per_key_window = per_key_window
        self.overall = overall
        self.overall_window = overall_window
        self.max_keys = max_keys
        # Ordered by the time of each address's newest event (oldest first), so that the address that has been quiet the
        # longest is always the first one: room is made by dropping from the front, in constant time.
        self._by_key: OrderedDict[str, deque[float]] = OrderedDict()
        self._overall: deque[float] = deque(maxlen=overall)

    @staticmethod
    def _live(events: deque[float], now: float, window: float) -> int:
        """Forget the events that left the window; how many are inside it."""
        while events and events[0] <= now - window:
            events.popleft()
        return len(events)

    def __len__(self) -> int:
        """The number of addresses remembered."""
        return len(self._by_key)

    def block(self, host: object, now: float) -> Blocked | None:
        """None when one more event from `host` is within both limits, else why it is not (see Blocked). When both limits
        apply the scope is "global": the overall cap is what every address, this one included, is waiting for."""
        overall_full = self._live(self._overall, now, self.overall_window) >= self.overall
        events = self._by_key.get(client_key(host))
        key_full = events is not None and self._live(events, now, self.per_key_window) >= self.per_key
        if not overall_full and not key_full:
            return None
        # An event leaves a window at oldest + window (see _live), and a full window has room again once one has left.
        waits = []
        if overall_full:
            waits.append(self._overall[0] + self.overall_window - now)
        if key_full and events is not None:
            waits.append(events[0] + self.per_key_window - now)
        return Blocked(SCOPE_GLOBAL if overall_full else SCOPE_IP, max(1, math.ceil(max(waits))))

    def is_blocked(self, host: object, now: float) -> bool:
        return self.block(host, now) is not None

    def record(self, host: object, now: float) -> None:
        self._live(self._overall, now, self.overall_window)
        self._overall.append(now)
        key = client_key(host)
        events = self._by_key.get(key)
        if events is None:
            while len(self._by_key) >= self.max_keys:  # full: forget the address that has been quiet the longest
                self._by_key.popitem(last=False)
            events = self._by_key[key] = deque(maxlen=self.per_key)
        else:
            self._by_key.move_to_end(key)
        self._live(events, now, self.per_key_window)
        events.append(now)

    def reset(self, host: object) -> None:
        """Forget one address (after it proved itself). The overall count is left alone."""
        self._by_key.pop(client_key(host), None)


class FailureLimiter(AttemptLimiter):
    """Wrong passwords: 5 per address in 15 minutes, 30 overall in an hour."""

    def __init__(self, *, max_keys: int = MAX_LIMITER_KEYS) -> None:
        super().__init__(
            per_key=FAILURES_PER_IP,
            per_key_window=FAILURE_WINDOW_SECONDS,
            overall=FAILURES_OVERALL,
            overall_window=FAILURE_OVERALL_WINDOW_SECONDS,
            max_keys=max_keys,
        )


class RegistrationLimiter(AttemptLimiter):
    """Registration requests: 10 per address in an hour, 50 overall in a day."""

    def __init__(self, *, max_keys: int = MAX_LIMITER_KEYS) -> None:
        super().__init__(
            per_key=REGISTRATIONS_PER_IP,
            per_key_window=REGISTRATION_IP_WINDOW_SECONDS,
            overall=REGISTRATIONS_OVERALL,
            overall_window=REGISTRATION_OVERALL_WINDOW_SECONDS,
            max_keys=max_keys,
        )


def idle_client_to_evict(
    clients: Mapping[str, Any], holders: Iterable[str], now: float, min_age: float = EVICTION_MIN_AGE_SECONDS
) -> str | None:
    """The client to drop to make room for a new one: among the clients that hold no token and were issued more than
    `min_age` seconds ago, the oldest. None when there is none. A client with no issue time counts as the oldest.
    `holders` are the ids of clients that hold an access or a refresh token (even an expired one): never chosen."""
    held = set(holders)
    best: tuple[float, str] | None = None
    for client_id, record in clients.items():
        if client_id in held:
            continue
        issued = getattr(record, "client_id_issued_at", None)
        issued_at = float(issued) if isinstance(issued, (int, float)) and not isinstance(issued, bool) else 0.0
        if now - issued_at <= min_age:
            continue
        if best is None or (issued_at, client_id) < best:
            best = (issued_at, client_id)
    return None if best is None else best[1]


# --------------------------------------------------------------------------
# Values that go into the journal
# --------------------------------------------------------------------------


def _escape(char: str) -> str:
    code = ord(char)
    if char == "\\":
        return "\\\\"
    if char == '"':
        return '\\"'
    if 0x20 <= code <= 0x7E:
        return char
    if code <= 0xFF:
        return f"\\x{code:02x}"
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def safe(value: object, limit: int = LOG_VALUE_LIMIT) -> str:
    """A value for a log line: printable ASCII only (a backslash, a double quote and every other character as an escape),
    at most `limit` characters (shortened with "..."), so that no caller can put a newline, a control code or markup into
    the journal. None and the empty string are "-"."""
    limit = max(limit, 4)
    try:
        text = "-" if value is None else str(value)
    except Exception:
        text = "?"
    if text == "":
        return "-"
    pieces: list[str] = []
    size = 0
    cut = False
    for char in text:
        piece = _escape(char)
        if size + len(piece) > limit:
            cut = True
            break
        pieces.append(piece)
        size += len(piece)
    if cut:
        while pieces and size + 3 > limit:
            size -= len(pieces.pop())
        return "".join(pieces) + "..."
    return "".join(pieces)


def safe_host(uri: object) -> str:
    """The host of a redirect address for a log line, and nothing else of it (the path and query can hold a secret).
    Read the way the redirect validator reads it, from the normalized form; "?" when it does not parse."""
    try:
        host = AnyUrl(str(uri)).host
    except ValueError:  # pydantic's ValidationError is a ValueError
        host = None
    return safe(host or "?")


# --------------------------------------------------------------------------
# Redirect addresses
# --------------------------------------------------------------------------

_CLEAN_TEXT = re.compile(r"[\x21-\x5b\x5d-\x7e]+")  # printable ASCII except space and backslash


def _allowed_domains(domains: Iterable[object] | None) -> list[str] | None:
    """The allowlist as clean lower-case names. None means every domain. An entry that is not a plain host name is left
    out, which can only make the list shorter."""
    if domains is None:
        return None
    cleaned = []
    for domain in domains:
        if isinstance(domain, str):
            name = domain.strip().lower().strip("[]")
            if re.fullmatch(r"[a-z0-9._:-]+", name):
                cleaned.append(name)
    return cleaned


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _on_allowlist(host: str, domains: list[str] | None) -> bool:
    if domains is None:
        return True
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def text_problem(text: str) -> str | None:
    """Why a redirect address is not even worth parsing: empty, too long, or holding a space, a control character, a
    backslash or anything outside printable ASCII (a parser may read those differently from the next one). None if fine."""
    if not text:
        return "empty"
    if len(text) > MAX_REDIRECT_CHARS:
        return "too_long"
    if not _CLEAN_TEXT.fullmatch(text):
        return "characters"
    return None


def redirect_problem(uri: object, allowed_domains: Iterable[object] | None) -> str | None:
    """None if `uri` is an acceptable redirect address, else a short reason (a fixed word, safe to log).

    Acceptable: https on a host of the allowlist (the host itself or a subdomain of an entry), or http to a loopback host
    (localhost or a loopback address) that the allowlist also admits. Never acceptable: userinfo, a fragment, a backslash,
    a space or control character, a scheme other than http and https, a host the standard library reads differently from
    pydantic. The check runs on the pydantic-normalized string (what is stored and what is redirected to), after a first
    look at the text as given: normalization quietly removes tabs and turns a backslash into a slash, and a validator
    that read the text before that and a redirect that used it after would disagree about the host.
    `allowed_domains` None admits every host (https still, and http only on loopback)."""
    text = uri if isinstance(uri, str) else str(uri) if uri is not None else ""
    problem = text_problem(text)
    if problem is not None:
        return problem
    try:
        url = AnyUrl(text)
    except ValueError:  # pydantic's ValidationError is a ValueError
        return "unparseable"
    normalized = str(url)
    if text_problem(normalized) is not None:
        return "characters"
    scheme = (url.scheme or "").lower()
    if scheme not in ("http", "https"):
        return "scheme"
    if url.username is not None or url.password is not None:
        return "userinfo"
    if "#" in normalized:
        return "fragment"
    host = (url.host or "").lower().strip("[]")
    if not host:
        return "host"
    try:
        split = urlsplit(normalized)
        _ = split.port  # raises for a port that is not a number in range
    except ValueError:
        return "mismatch"
    if (
        split.scheme != scheme
        or split.username is not None
        or split.password is not None
        or (split.hostname or "").lower() != host
    ):
        return "mismatch"
    if scheme == "http" and not _is_loopback(host):
        return "not_https"
    if not _on_allowlist(host, _allowed_domains(allowed_domains)):
        return "not_allowed"
    return None


def registration_problem(
    redirect_uris: Sequence[object] | None, allowed_domains: Iterable[object] | None
) -> tuple[str, str] | None:
    """For the redirect addresses of a client that wants to register: None if every one passes redirect_problem, else
    (reason, host of the first one that does not) for the log. No addresses at all is a problem."""
    if not redirect_uris:
        return "missing", "-"
    for uri in redirect_uris:
        problem = redirect_problem(uri, allowed_domains)
        if problem is not None:
            return problem, safe_host(uri)
    return None


# --------------------------------------------------------------------------
# Client metadata that can be kept
# --------------------------------------------------------------------------


def _is_utf8_text(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def json_text_problem(value: object) -> str | None:
    """None if a parsed JSON value (dictionaries, lists, text, numbers) can be written as UTF-8 JSON and nests no deeper
    than MAX_METADATA_DEPTH, else a fixed word for what is wrong: "surrogate" (a key or a text that holds a lone UTF-16
    surrogate, which json.loads lets through, written as an escape or as the bytes of one, and which no UTF-8 writer can
    encode) or "too_deep". The value is walked with a stack of its own: a recursive walk could be sent past the
    interpreter's limit by the very input it is looking at."""
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, str):
            if not _is_utf8_text(item):
                return "surrogate"
        elif isinstance(item, dict):
            if depth > MAX_METADATA_DEPTH:
                return "too_deep"
            for key, child in item.items():
                if isinstance(key, str) and not _is_utf8_text(key):
                    return "surrogate"
                stack.append((child, depth + 1))
        elif isinstance(item, (list, tuple)):
            if depth > MAX_METADATA_DEPTH:
                return "too_deep"
            stack.extend((child, depth + 1) for child in item)
    return None


def client_metadata_problem(info: Any) -> str | None:
    """None if a client that is about to be registered (a pydantic model) can be kept and answered, else a fixed word for
    why not: "surrogate" or "too_deep" as above, or "unserializable" for anything else that a serializer refuses. It tries
    both things that are done with a client before either is done: the dictionary and JSON text that the state file is made
    of, and the 201 answer of the registration endpoint, which the framework writes with the model's own serializer after
    the provider has stored the client. A client that either would choke on is therefore never stored."""
    try:
        dumped = info.model_dump(mode="json")
    except Exception:  # a serializer's refusal of this one client; there is no I/O here, so nothing else can be meant
        return "unserializable"
    problem = json_text_problem(dumped)
    if problem is not None:
        return problem
    try:
        json.dumps(dumped, indent=2, allow_nan=False).encode("utf-8")
        info.model_dump_json(exclude_none=True).encode("utf-8")
    except Exception:
        return "unserializable"
    return None


# --------------------------------------------------------------------------
# What a consent request may carry
# --------------------------------------------------------------------------

OAUTH_FIELDS = (
    "client_id",
    "redirect_uri",
    "response_type",
    "code_challenge",
    "code_challenge_method",
    "state",
    "scope",
    "resource",
)
CONSENT_FIELDS = (*OAUTH_FIELDS, "decision", "password")
FORM_CONTENT_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")

# RFC 7636: a code challenge is 43 to 128 characters of the unreserved set (an S256 challenge is exactly 43).
PKCE_CHALLENGE = re.compile(r"[A-Za-z0-9._~-]{43,128}")


def is_form_content_type(header: str | None) -> bool:
    """True when a Content-Type header names a form encoding (the two that Starlette parses as a form)."""
    if not header:
        return False
    return header.split(";", 1)[0].strip().lower() in FORM_CONTENT_TYPES


def read_fields(items: Iterable[tuple[str, object]]) -> tuple[dict[str, str], str | None]:
    """The consent fields of a query string or a form, and a problem word if the request cannot be taken at face value.

    Every value of every field must be text (a multipart file part is not) and short; where a name is repeated the last
    value wins, as it always did. Only the consent fields are returned."""
    fields: dict[str, str] = {}
    for key, value in items:
        if not isinstance(value, str):
            return {}, "field_type"
        if len(value) > MAX_FIELD_CHARS or len(key) > MAX_FIELD_CHARS:
            return {}, "field_size"
        if key in CONSENT_FIELDS:
            fields[key] = value
    return fields, None


# --------------------------------------------------------------------------
# Consent responses: the same headers and the same bytes every time
# --------------------------------------------------------------------------

# No form-action on purpose: browsers apply it to the redirect that follows the form post as well, which would block the
# redirect back to the client application.
CONTENT_SECURITY_POLICY = "frame-ancestors 'none'; base-uri 'none'; default-src 'none'; style-src 'unsafe-inline'"
_CONSENT_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def consent_headers() -> dict[str, str]:
    """The security headers of every response of the /authorize endpoint (a fresh dict each time)."""
    return dict(_CONSENT_HEADERS)


PAGE_STYLE = """body { background:#111; color:#eee; font-family:system-ui,-apple-system,sans-serif; margin:0; padding:2rem; display:flex; justify-content:center; }
.card { background:#1b1b1b; border:1px solid #333; border-radius:8px; padding:1.5rem 2rem; max-width:520px; width:100%; }
h1 { font-size:1.25rem; margin:0 0 1rem; }
p { margin:0; line-height:1.5; }
dl { margin:0 0 1rem; }
dt { font-size:0.75rem; color:#888; text-transform:uppercase; letter-spacing:0.05em; margin-top:0.75rem; }
dd { margin:0.25rem 0 0; word-break:break-all; }
label { display:block; margin-bottom:0.4rem; font-size:0.9rem; color:#bbb; }
input[type=password] { width:100%; padding:0.6rem; background:#000; color:#eee; border:1px solid #444; border-radius:4px; font-size:1rem; box-sizing:border-box; }
.buttons { display:flex; gap:0.5rem; margin-top:1rem; }
button { flex:1; padding:0.7rem; border-radius:4px; border:1px solid #444; font-size:1rem; cursor:pointer; font-family:inherit; }
button.approve { background:#2b6; color:#000; border-color:#2b6; font-weight:600; }
button.deny { background:#222; color:#eee; }
.err { background:#3a1414; color:#f99; border:1px solid #622; padding:0.5rem 0.75rem; border-radius:4px; margin-bottom:1rem; }"""


def notice_page(heading: str, message: str) -> str:
    """A page with a heading and one sentence. Both are constants of this module: a notice never repeats the request."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{heading}</title>
<style>
{PAGE_STYLE}
</style>
</head>
<body>
<div class="card">
<h1>{heading}</h1>
<p>{message}</p>
</div>
</body>
</html>
"""


INVALID_REQUEST_PAGE = notice_page(
    "Request not valid", "This authorization request is not valid. Go back to the application and start the sign-in again."
)
UNSUPPORTED_MEDIA_PAGE = notice_page("Request not supported", "This request cannot be processed.")
# The same page whichever limit stopped the request (the address's own or the overall one), so that it tells a guesser nothing
# about which it was; the sentence names both, because the second one can lock the operator out for something somebody else did.
TOO_MANY_ATTEMPTS_PAGE = notice_page(
    "Too many attempts",
    "Too many failed sign-in attempts, from this address or from all addresses together. "
    "Wait up to an hour, then start the sign-in again.",
)
TOO_LARGE_PAGE = notice_page("Request too large", "This request is too large to be processed.")
SERVER_ERROR_PAGE = notice_page("Something went wrong", "The request could not be processed. Try again later.")


def notice_response(status: int, page: str) -> HTMLResponse:
    return HTMLResponse(page, status_code=status, headers=consent_headers())


def invalid_request_response() -> HTMLResponse:
    """The one answer to every consent request that is not valid, whatever the password was."""
    return notice_response(400, INVALID_REQUEST_PAGE)


def unsupported_media_response() -> HTMLResponse:
    return notice_response(415, UNSUPPORTED_MEDIA_PAGE)


def too_many_attempts_response() -> HTMLResponse:
    """The one answer to every consent request from a limited address, or while the overall limit is reached."""
    return notice_response(429, TOO_MANY_ATTEMPTS_PAGE)


def server_error_response() -> HTMLResponse:
    return notice_response(500, SERVER_ERROR_PAGE)


def consent_refusal(status: int) -> Response:
    """What BodyLimit answers on the consent endpoint: a notice page with the consent headers."""
    return notice_response(status, TOO_LARGE_PAGE if status == 413 else INVALID_REQUEST_PAGE)


def json_refusal(status: int) -> Response:
    """What BodyLimit answers on the token, registration and revocation endpoints."""
    description = "The request body is too large." if status == 413 else "The request is not valid."
    return JSONResponse(
        {"error": "invalid_request", "error_description": description},
        status_code=status,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def registration_limited_response() -> Response:
    return JSONResponse(
        {"error": "temporarily_unavailable", "error_description": "Too many registration requests. Try again later."},
        status_code=429,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def registration_not_json_response() -> Response:
    return JSONResponse(
        {"error": "invalid_client_metadata", "error_description": "The request body is not valid JSON."},
        status_code=400,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# What a client is told when its metadata cannot be kept (the gate says it for the request body, the provider for the
# registered client). A fixed sentence: it never repeats anything the client sent.
METADATA_REFUSED_DESCRIPTION = "The client metadata cannot be stored."


def registration_metadata_response() -> Response:
    return JSONResponse(
        {"error": "invalid_client_metadata", "error_description": METADATA_REFUSED_DESCRIPTION},
        status_code=400,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# --------------------------------------------------------------------------
# ASGI wrappers: the body cap, the registration gate, the form guard and the revocation mark
# --------------------------------------------------------------------------


def client_host(scope: Scope) -> str:
    """The client address of an ASGI scope as text ("?" when the server gave none)."""
    client = scope.get("client")
    if client and isinstance(client[0], str):
        return client[0]
    return "?"


_BAD_LENGTH = -1


def _declared_length(scope: Scope) -> int | None:
    """The Content-Length of a request: None when absent, _BAD_LENGTH when it is not a plain number or is repeated with
    different values."""
    values = {value.strip() for name, value in scope.get("headers", []) if name.lower() == b"content-length"}
    if not values:
        return None
    if len(values) > 1:
        return _BAD_LENGTH
    (value,) = values
    return int(value) if re.fullmatch(rb"[0-9]{1,18}", value) else _BAD_LENGTH


def _has_transfer_encoding(scope: Scope) -> bool:
    """True when the request names a transfer encoding (chunked, in practice): it announces a body of unknown length."""
    return any(name.lower() == b"transfer-encoding" for name, _ in scope.get("headers", []))


def is_cors_preflight(scope: Scope) -> bool:
    """True for a request that the framework's CORS layer (Starlette's CORSMiddleware, which the framework puts in front of
    the token, registration and revocation handlers) answers by itself, without the handler ever running: an OPTIONS with
    an Origin header and an Access-Control-Request-Method header. Decided from the same Headers class and the same two
    headers that the middleware looks at, so that the two cannot disagree about which requests those are. An OPTIONS
    without them is not a preflight: the middleware hands it on to the handler."""
    if scope.get("type") != "http" or scope.get("method") != "OPTIONS":
        return False
    headers = Headers(raw=list(scope.get("headers") or []))
    return headers.get("origin") is not None and "access-control-request-method" in headers


class _BodyTooLarge(Exception):
    """The body went over its limit while it was being read."""


class _ClientGone(Exception):
    """The client disconnected before the whole body had arrived."""


async def _read_body(receive: Receive, limit: int) -> bytes:
    """The whole request body, read from `receive` until it says there is no more. Raises _BodyTooLarge as soon as more
    than `limit` bytes have arrived (nothing after that is read) and _ClientGone when the client leaves first. A message
    that is neither a body nor a disconnect is skipped."""
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise _ClientGone
        if message["type"] != "http.request":
            continue
        body = message.get("body", b"")
        total += len(body)
        if total > limit:
            raise _BodyTooLarge
        chunks.append(body)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _replaying(whole: bytes, receive: Receive) -> Receive:
    """A receive() that hands the body that was read to the wrapped app, whole, in one message, and after that whatever
    the server says next (the client leaving)."""
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": whole, "more_body": False}
        return await receive()

    return replay


class BodyLimit:
    """ASGI wrapper that refuses a request whose body is larger than `max_bytes`, before the wrapped app reads any of it.
    It does not look at the HTTP method: the routes it stands in front of answer OPTIONS as well as POST, and the handlers
    of the framework read the body of whatever reaches them.

    A declared Content-Length over the limit is refused without reading. A body without one (chunked), or one that lies,
    is read here up to the limit and refused when it goes over; a body that fits is handed on whole, so the wrapped app
    never sees more than `max_bytes` and never has to handle the failure itself (the framework's handlers catch broad
    exceptions, so an error raised from inside a read would come out as something else).

    An OPTIONS request may carry no body at all, so for it the limit is zero: a Content-Length above zero, any transfer
    encoding, or a byte that arrives anyway is refused with 400. A CORS preflight is an OPTIONS without a body, so it goes
    on exactly as before. Anything that is not HTTP passes through untouched. `refuse(status)` builds the response; the
    status is 413 (too large) or 400 (a Content-Length that is not a number, or a body on an OPTIONS)."""

    def __init__(
        self, app: ASGIApp, *, path: str, refuse: Callable[[int], Response], max_bytes: int = MAX_BODY_BYTES
    ) -> None:
        self.app = app
        self.path = path
        self.refuse = refuse
        self.max_bytes = max_bytes

    async def _refused(self, status: int, reason: str, scope: Scope, receive: Receive, send: Send) -> None:
        logger.warning("body outcome=%s path=%s ip=%s", reason, self.path, safe(client_host(scope)))
        await self.refuse(status)(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        options = str(scope.get("method", "")).upper() == "OPTIONS"
        declared = _declared_length(scope)
        if declared == _BAD_LENGTH:
            await self._refused(400, "bad_length", scope, receive, send)
            return
        if options and (declared or _has_transfer_encoding(scope)):
            await self._refused(400, "options_body", scope, receive, send)
            return
        if declared is not None and declared > self.max_bytes:
            await self._refused(413, "too_large", scope, receive, send)
            return
        try:
            whole = await _read_body(receive, 0 if options else self.max_bytes)
        except _ClientGone:
            return
        except _BodyTooLarge:
            if options:
                await self._refused(400, "options_body", scope, receive, send)
            else:
                await self._refused(413, "too_large", scope, receive, send)
            return
        await self.app(scope, _replaying(whole, receive), send)


# The address of the request being served, for the log lines that the provider writes below the wrappers.
REQUEST_IP: contextvars.ContextVar[str] = contextvars.ContextVar("oauth_guard_request_ip", default="-")


class RegistrationGate:
    """ASGI wrapper for the registration endpoint, in front of the framework's CORS layer and handler.

    Every request that reaches the handler counts against the registration limiter, whatever its HTTP method (the route
    answers OPTIONS as well as POST, and an OPTIONS that is not a CORS preflight is handed on to the handler like any
    other request); one over a limit gets HTTP 429 without the handler running. A CORS preflight is the only request that
    does not count: the CORS layer answers it itself, it registers nothing (`is_cors_preflight`).

    The body is read here, once, and checked before the handler sees it. A body that is not JSON gets HTTP 400, and so
    does one that is JSON but holds what no UTF-8 JSON writer can keep or nests too deeply (`json_text_problem`): the
    framework's handler lets the first escape as an exception (a 500 and a traceback in the journal for what any scanner
    can send, a number of more than 4300 digits included), and the second it stores before it fails to answer. Nothing is
    stored for a refused request. A request that is not a POST and still carries a body is refused too: only a POST
    registers. The body is read up to `max_bytes` (BodyLimit, which stands in front of this in the routes, has already
    held it to the same limit; the gate does not rely on that).

    The parser and the checks run here, not around the handler, so that an error that is a fault of ours still comes out as
    an error: only what the body itself causes is answered with a 400. `on_refused(host, reason)` lets the provider write
    the log line; the reason is "rate_limited", "too_large", "not_post_body", "not_json", "metadata_surrogate" or
    "metadata_too_deep"."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: AttemptLimiter,
        clock: Callable[[], float],
        on_refused: Callable[[str, str], None],
        max_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.clock = clock
        self.on_refused = on_refused
        self.max_bytes = max_bytes

    @staticmethod
    def _body_problem(scope: Scope, body: bytes) -> tuple[str, Response] | None:
        """(reason, the answer) for a body that must not reach the handler, or None."""
        if scope.get("method") != "POST" and body:
            return "not_post_body", json_refusal(400)
        try:
            data = json.loads(body)  # what the handler's request.json() does, with the same input
        except Exception:  # JSONDecodeError, UnicodeDecodeError, RecursionError, the 4300-digit ValueError: all are the body
            return "not_json", registration_not_json_response()
        problem = json_text_problem(data)
        if problem is not None:
            return f"metadata_{problem}", registration_metadata_response()
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or is_cors_preflight(scope):
            await self.app(scope, receive, send)
            return
        host = client_host(scope)
        now = self.clock()
        if self.limiter.is_blocked(host, now):
            self.on_refused(host, "rate_limited")
            await registration_limited_response()(scope, receive, send)
            return
        self.limiter.record(host, now)
        try:
            body = await _read_body(receive, self.max_bytes)
        except _ClientGone:
            return
        except _BodyTooLarge:
            self.on_refused(host, "too_large")
            await json_refusal(413)(scope, receive, send)
            return
        refusal = self._body_problem(scope, body)
        if refusal is not None:
            reason, response = refusal
            self.on_refused(host, reason)
            await response(scope, receive, send)
            return
        started = False

        async def watching(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        token = REQUEST_IP.set(host)
        try:
            await self.app(scope, _replaying(body, receive), watching)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            # The body parsed above, so the handler's own parse cannot fail on it; this stays for a parser that does not
            # behave the same twice (the interpreter's recursion limit depends on how deep the stack is).
            if started:
                raise
            self.on_refused(host, "not_json")
            await registration_not_json_response()(scope, receive, send)
        finally:
            REQUEST_IP.reset(token)


def _is_multipart_form(scope: Scope) -> bool:
    headers = Headers(raw=list(scope.get("headers") or []))
    return (headers.get("content-type") or "").split(";", 1)[0].strip().lower() == "multipart/form-data"


class FormGuard:
    """ASGI wrapper for the token and revocation endpoints, whose handlers read their parameters with `request.form()`.

    The framework's form parser lets a multipart body that it cannot read come out as an exception that nothing turns into
    an answer (python-multipart's MultipartParseError, for a declared boundary that the body does not follow: a 500 and a
    traceback in the journal for what any scanner can send; the consent endpoint, which parses its own form, has always
    answered it). A multipart body is therefore parsed here first, with the same parser, and one that does not parse gets
    HTTP 400 and never reaches the handler; one that does is handed on unchanged. Any exception of that parse is the
    body's, since the parse is a function of the body alone, and the wrapper stands outside the handler, so a fault of the
    handler's own still comes out as one. A body that is not multipart (an urlencoded form is always readable) passes
    through without being read here. The body was held to its limit by BodyLimit, which stands in front of this wrapper;
    this one holds it to the same limit itself. `path` is for the log line."""

    def __init__(self, app: ASGIApp, *, path: str, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.path = path
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_multipart_form(scope):
            await self.app(scope, receive, send)
            return
        try:
            whole = await _read_body(receive, self.max_bytes)
        except _ClientGone:
            return
        except _BodyTooLarge:
            logger.warning("body outcome=too_large path=%s ip=%s", self.path, safe(client_host(scope)))
            await json_refusal(413)(scope, receive, send)
            return
        try:
            form = await Request(scope, _replaying(whole, receive)).form()
        except Exception:  # the parser's own error, or the 400 that Starlette makes of its MultiPartException
            logger.warning("body outcome=bad_form path=%s ip=%s", self.path, safe(client_host(scope)))
            await json_refusal(400)(scope, receive, send)
            return
        await form.close()  # the file parts that were spooled while checking
        await self.app(scope, _replaying(whole, receive), send)


# True while the provider serves a request to /revoke.
REVOKING: contextvars.ContextVar[bool] = contextvars.ContextVar("oauth_guard_revoking", default=False)


def revoking() -> bool:
    """True inside a request to /revoke. The framework's revocation handler asks the provider to load the token it was
    given, as an access token and as a refresh token, to find out whose it is. That is a lookup, not a use: it must not be
    taken for a refresh (reuse detection of a rotated token, which under the "revoke" policy would end the caller's own
    session) or for an expired session being presented (the "access outcome=expired" line that the refresh check
    of docs/OPERATIONS.md reads as proof)."""
    return REVOKING.get()


class RevocationMark:
    """ASGI wrapper for /revoke: for the length of the request, `revoking()` is true in the provider's token loaders.
    Undone when the request ends, whatever happens in it; another request, which runs in its own context, never sees it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = REVOKING.set(True)
        try:
            await self.app(scope, receive, send)
        finally:
            REVOKING.reset(token)


# --------------------------------------------------------------------------
# Fail closed: authorize() only runs for a request the consent endpoint approved
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Approval:
    """What the consent endpoint approved: one client, one redirect address."""

    client_id: str
    redirect_uri: str


_APPROVAL: contextvars.ContextVar[Approval | None] = contextvars.ContextVar("oauth_guard_approval", default=None)


class approved:  # noqa: N801  (used like a function: `with approved(client_id, redirect_uri):`)
    """Mark the current request as approved for `client_id` and `redirect_uri` for the length of a `with` block. Only the
    consent endpoint does this, and only after the password matched; it is undone when the block ends, whatever happens
    inside.

    A class and not a generator function decorated with contextlib.contextmanager, on purpose: the framework's errors
    (AuthorizeError, TokenError, RegistrationError) are frozen dataclasses, and contextmanager assigns __traceback__ to the
    exception that passes through it, which a frozen dataclass refuses with FrozenInstanceError. The error of the block
    has to come out as itself."""

    def __init__(self, client_id: str, redirect_uri: str) -> None:
        self._approval = Approval(client_id, redirect_uri)
        self._token: contextvars.Token[Approval | None] | None = None

    def __enter__(self) -> None:
        self._token = _APPROVAL.set(self._approval)

    def __exit__(self, *exc_info: object) -> bool:
        if self._token is not None:
            _APPROVAL.reset(self._token)
            self._token = None
        return False


def take_approval(client_id: str, redirect_uri: str) -> bool:
    """True once for an approval made by `approved` for exactly this client and redirect address; false for anything
    else (no approval, another client, another address, a second use)."""
    current = _APPROVAL.get()
    if current is None or current.client_id != client_id or current.redirect_uri != redirect_uri:
        return False
    _APPROVAL.set(None)
    return True
