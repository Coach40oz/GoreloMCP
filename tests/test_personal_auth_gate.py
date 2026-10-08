"""personal_auth.py and oauth_guard.py, the consent gate: who may reach the password page, what the page does with a
request, how often a password may be tried, who may register, how big a body may be, and that the gate fails closed.

Everything is offline: temporary state directories, fake data, a fake clock where time matters, and the real HTTP app
(built by FastMCP from the real provider) driven in process through httpx.ASGITransport, with the client address chosen
per request (no sockets). Clients are put into the provider directly where the registration endpoint is not what a test
is about. The token lifecycle has its own tests (test_personal_auth_tokens.py), the state file too (test_oauth_store.py).

What is pinned here, by section: the limiters and the address key (and which limit stopped a request, and for how long),
the redirect validator and the registrations that try to get an open redirect, the consent page (GET and POST: the order
of the checks, one identical page for every invalid request, the compare never run for one, deny, approve and the
limits), requests that must never end in a 500, the security headers, the body cap (for every HTTP method: an OPTIONS
that carries a body is refused, a CORS preflight is answered as the framework answers it and is not a registration),
registration limits, a registration body that cannot be parsed or kept (a clean 400 that stores nothing), the form guard
of the token and revocation endpoints, the revocation mark, the client cap, the fail-closed checks, and the logs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastmcp.server.auth.auth import OAuthProvider
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.provider import AccessToken, AuthorizationParams, AuthorizeError, RefreshToken, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.routing import Route

import oauth_guard
import oauth_store
import personal_auth
from oauth_guard import GateError, VersionGuardError
from personal_auth import PersonalAuthProvider
from server import build_server
from tools._common import Registry

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "https://mcp.example.test"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
PASSWORD = "a-test-password-not-real"  # 24 characters: no short-password warning
OWNER_IP = "203.0.113.7"
OTHER_IP = "198.51.100.9"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
STATE_FILE = oauth_store.STATE_FILE_NAME
DAY = 86400
EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)

INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

CONSENT_HEADERS = {
    "cache-control": "no-store",
    "pragma": "no-cache",
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'; base-uri 'none'; default-src 'none'; style-src 'unsafe-inline'",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class FakeClock:
    """Seconds since the epoch, moved by hand. It starts at the real time because the registration endpoint stamps a
    client with the real clock."""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def state_dir(tmp_path) -> Path:
    return tmp_path / "oauth-state"


@pytest.fixture
def make_provider():
    """make_provider(state_dir, **kwargs) -> PersonalAuthProvider with the test password; closed at the end of the test."""
    created: list[PersonalAuthProvider] = []

    def factory(state_dir, **kwargs):
        kwargs.setdefault("password", PASSWORD)
        provider = PersonalAuthProvider(base_url=BASE_URL, state_dir=str(state_dir), **kwargs)
        created.append(provider)
        return provider

    yield factory
    for provider in created:
        provider.close()


class Gate:
    """The real HTTP app of a provider, and one httpx client per client address (the address the server sees)."""

    def __init__(self, app) -> None:
        self.app = app
        self._clients: dict[str, httpx.AsyncClient] = {}

    def at(self, ip: str = OWNER_IP) -> httpx.AsyncClient:
        if ip not in self._clients:
            transport = httpx.ASGITransport(app=self.app, client=(ip, 40000))
            self._clients[ip] = httpx.AsyncClient(transport=transport, base_url="http://mcp.test")
        return self._clients[ip]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()


@pytest.fixture
def serve(make_settings, mock_gorelo, spec_index):
    """serve(provider) -> async context manager yielding a Gate."""

    @asynccontextmanager
    async def serving(provider):
        server = build_server(
            make_settings(), auth=provider, transport=mock_gorelo.transport, spec=spec_index, registry=Registry()
        )
        app = server.http_app(json_response=True)
        async with app.router.lifespan_context(app):
            gate = Gate(app)
            try:
                yield gate
            finally:
                await gate.aclose()

    return serving


async def make_client(
    provider,
    n: int = 1,
    *,
    redirect: str = REDIRECT,
    name: str | None = None,
    scope: str | None = None,
    issued_at: int | None = None,
) -> OAuthClientInformationFull:
    """A registered client with a secret, registered through the provider (so the redirect validator ran)."""
    client = OAuthClientInformationFull(
        client_id=f"{n:08d}-aaaa-4bbb-8ccc-{n:012d}",
        client_secret=f"client-secret-{n}-" + "x" * 24,
        client_id_issued_at=int(provider._now()) if issued_at is None else issued_at,
        redirect_uris=[AnyUrl(redirect)],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name=name or f"Test client {n}",
        scope=scope,
    )
    await provider.register_client(client)
    return client


def params(client, **override) -> dict[str, str]:
    """The query or form of a valid authorization request for `client`; a value of None leaves the field out."""
    base = {
        "client_id": client.client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "state-1",
    }
    base.update(override)
    return {key: value for key, value in base.items() if value is not None}


async def post_consent(http, client, *, password: str | None = PASSWORD, decision: str | None = "approve", **override):
    data = params(client, **override)
    if decision is not None:
        data["decision"] = decision
    if password is not None:
        data["password"] = password
    return await http.post("/authorize", data=data)


def signature(response: httpx.Response) -> tuple:
    """Everything a caller can see of a response, to prove that two responses are the same."""
    return (response.status_code, response.content, tuple(sorted((k.lower(), v) for k, v in response.headers.items())))


def location_params(response: httpx.Response) -> dict[str, list[str]]:
    return parse_qs(urlparse(response.headers["location"]).query)


@pytest.fixture
def compare_spy(monkeypatch) -> list[tuple]:
    """Every call of hmac.compare_digest while the test runs (the real function still does the comparing)."""
    calls: list[tuple] = []
    real = hmac.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    return calls


def counters(provider) -> tuple[int, int]:
    """(addresses remembered, failures remembered overall): what a request must not change when it is not an attempt."""
    return len(provider._failure_limiter), len(provider._failure_limiter._overall)


def capture(caplog) -> None:
    caplog.set_level(logging.INFO, logger="personal-auth")
    caplog.set_level(logging.INFO, logger="oauth-guard")


def lines(caplog, name: str = "personal-auth") -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == name]


def fail_saves(monkeypatch) -> None:
    """Make every write of the state file fail at the last step (the rename)."""
    real = os.replace

    def refuse(src, dst, *args, **kwargs):
        if str(dst).endswith(STATE_FILE):
            raise OSError(5, "Input/output error")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", refuse)


def hidden_inputs(page: str) -> dict[str, str]:
    """The hidden fields of a consent page, as a browser would send them back."""
    found = re.findall(r'<input type="hidden" name="([^"]*)" value="([^"]*)">', page)
    return {html.unescape(name): html.unescape(value) for name, value in found}


_DEFAULT = object()


async def register_over_http(http, *, redirect_uris=_DEFAULT, **extra) -> httpx.Response:
    """POST /register. redirect_uris is the claude.ai address unless given; None sends a JSON null."""
    body = {
        "redirect_uris": [REDIRECT] if redirect_uris is _DEFAULT else redirect_uris,
        "client_name": "Claude",
        "token_endpoint_auth_method": "client_secret_post",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    body.update(extra)
    return await http.post("/register", json=body)


def v1_file(clients: dict) -> dict:
    """A version 1 state file (the shape earlier versions wrote) holding these clients and no tokens."""
    return {"clients": clients, "access_tokens": {}, "refresh_tokens": {}, "a2r": {}, "r2a": {}}


def legacy_client_record(client_id: str, redirect_uris: list[str], name: str = "Claude") -> dict:
    return {
        "redirect_uris": redirect_uris, "token_endpoint_auth_method": "client_secret_post",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "scope": None,
        "client_name": name, "client_uri": None, "logo_uri": None, "contacts": None, "tos_uri": None, "policy_uri": None,
        "jwks_uri": None, "jwks": None, "software_id": None, "software_version": None, "client_id": client_id,
        "client_secret": f"secret-of-{client_id[:8]}", "client_id_issued_at": int(time.time()) - 30 * DAY,
        "client_secret_expires_at": None,
    }


# --------------------------------------------------------------------------
# The numbers of the release
# --------------------------------------------------------------------------


def test_the_numbers_are_the_ones_the_release_was_designed_with():
    assert (oauth_guard.FAILURES_PER_IP, oauth_guard.FAILURE_WINDOW_SECONDS) == (5, 15 * 60)
    assert (oauth_guard.FAILURES_OVERALL, oauth_guard.FAILURE_OVERALL_WINDOW_SECONDS) == (30, 60 * 60)
    assert oauth_guard.MAX_LIMITER_KEYS == 4096
    assert (oauth_guard.REGISTRATIONS_PER_IP, oauth_guard.REGISTRATION_IP_WINDOW_SECONDS) == (10, 60 * 60)
    assert (oauth_guard.REGISTRATIONS_OVERALL, oauth_guard.REGISTRATION_OVERALL_WINDOW_SECONDS) == (50, DAY)
    assert (oauth_guard.MAX_CLIENTS, oauth_guard.EVICTION_MIN_AGE_SECONDS) == (50, DAY)
    assert oauth_guard.MAX_BODY_BYTES == 16 * 1024
    assert oauth_guard.BODY_LIMITED_PATHS == ("/authorize", "/token", "/register", "/revoke")
    assert oauth_guard.MAX_METADATA_DEPTH == 16
    assert oauth_guard.LOG_VALUE_LIMIT == 80 and oauth_guard.MIN_PASSWORD_CHARS == 16
    assert oauth_guard.SUPPORTED_VERSIONS == (("fastmcp", "3.2"), ("mcp", "1.27"))


async def test_a_new_provider_has_the_limiters_with_those_numbers(state_dir, make_provider):
    provider = make_provider(state_dir)
    failures, registrations = provider._failure_limiter, provider._registration_limiter
    assert isinstance(failures, oauth_guard.FailureLimiter) and isinstance(registrations, oauth_guard.RegistrationLimiter)
    assert (failures.per_key, failures.per_key_window, failures.overall, failures.overall_window) == (5, 900, 30, 3600)
    assert (registrations.per_key, registrations.per_key_window, registrations.overall, registrations.overall_window) == (10, 3600, 50, DAY)
    assert failures.max_keys == registrations.max_keys == 4096


# --------------------------------------------------------------------------
# The address a limiter counts under
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host, key",
    [
        ("203.0.113.7", "203.0.113.7"),
        (" 203.0.113.7 ", "203.0.113.7"),
        ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
        ("2001:0DB8:0001:0002:0000:0000:0000:0042", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
        ("fe80::1%eth0", "fe80::/64"),
        ("::1", "::/64"),
        ("::ffff:203.0.113.7", "203.0.113.7"),  # an IPv4 address that arrived in IPv6 clothes is the IPv4 address
        ("::ffff:cb00:7107", "203.0.113.7"),
    ],
)
def test_an_ipv4_address_counts_as_itself_and_an_ipv6_address_as_its_slash_64(host, key):
    assert oauth_guard.client_key(host) == key


@pytest.mark.parametrize("host", [None, "", "testclient", "not an address", "999.1.1.1", 12345, b"203.0.113.7", "x" * 5000])
def test_anything_that_is_not_an_address_shares_one_key(host):
    assert oauth_guard.client_key(host) == "?"


# --------------------------------------------------------------------------
# The limiters, on their own
# --------------------------------------------------------------------------


def limiter_for_failures() -> oauth_guard.FailureLimiter:
    return oauth_guard.FailureLimiter()


def test_five_failures_from_an_address_block_the_sixth_and_the_window_slides():
    limiter, t0 = limiter_for_failures(), 1_000_000.0
    for attempt in range(5):
        assert not limiter.is_blocked("203.0.113.7", t0 + attempt)
        limiter.record("203.0.113.7", t0 + attempt)
    assert limiter.is_blocked("203.0.113.7", t0 + 5)
    assert not limiter.is_blocked("203.0.113.8", t0 + 5)  # another address is fine
    # the first failure leaves the window 900 seconds after it happened, and then the address has room for one more
    assert limiter.is_blocked("203.0.113.7", t0 + 899)
    assert not limiter.is_blocked("203.0.113.7", t0 + 900)
    limiter.record("203.0.113.7", t0 + 900)
    assert limiter.is_blocked("203.0.113.7", t0 + 900)  # four old ones and the new one: five again
    assert not limiter.is_blocked("203.0.113.7", t0 + 901)  # the second failure has left the window too


def test_a_blocked_address_that_keeps_trying_is_not_blocked_for_longer_because_only_failures_count():
    limiter, t0 = limiter_for_failures(), 5_000.0
    for _ in range(5):
        limiter.record("203.0.113.7", t0)
    for step in range(100):  # asking is not recording
        assert limiter.is_blocked("203.0.113.7", t0 + step)
    assert not limiter.is_blocked("203.0.113.7", t0 + 900)


def test_thirty_failures_in_an_hour_from_any_addresses_block_everyone():
    limiter, t0 = limiter_for_failures(), 2_000_000.0
    for n in range(29):
        limiter.record(f"198.51.100.{n}", t0 + n)
    assert not limiter.is_blocked("192.0.2.1", t0 + 40)  # 29 failures: one more is allowed
    limiter.record("198.51.100.99", t0 + 40)
    assert limiter.is_blocked("192.0.2.1", t0 + 41) and limiter.is_blocked("192.0.2.200", t0 + 41)
    assert limiter.is_blocked("198.51.100.0", t0 + 41)
    # the oldest of the thirty leaves the window 3600 seconds after it happened
    assert limiter.is_blocked("192.0.2.1", t0 + 3599)
    assert not limiter.is_blocked("192.0.2.1", t0 + 3600)


def test_reset_forgets_one_address_but_not_the_overall_count():
    limiter, t0 = limiter_for_failures(), 10.0
    for _ in range(4):
        limiter.record("203.0.113.7", t0)
    limiter.reset("203.0.113.7")
    assert len(limiter) == 0 and len(limiter._overall) == 4
    for _ in range(5):
        assert not limiter.is_blocked("203.0.113.7", t0 + 1)
        limiter.record("203.0.113.7", t0 + 1)
    assert limiter.is_blocked("203.0.113.7", t0 + 2)


def test_addresses_of_one_ipv6_slash_64_are_one_address_and_slash_64s_are_separate():
    limiter, t0 = limiter_for_failures(), 77.0
    for n in range(5):
        limiter.record(f"2001:db8:1:2::{n + 1:x}", t0)  # five different machine addresses of one network
    assert len(limiter) == 1
    assert limiter.is_blocked("2001:db8:1:2:aaaa:bbbb:cccc:dddd", t0)
    assert not limiter.is_blocked("2001:db8:1:3::1", t0)
    assert not limiter.is_blocked("203.0.113.7", t0)


def test_ten_thousand_addresses_leave_at_most_4096_remembered_and_the_newest_are_the_ones_kept():
    limiter = limiter_for_failures()
    t0 = 1_700_000_000.0
    for n in range(10_000):
        limiter.record(f"10.{n // 65536}.{(n // 256) % 256}.{n % 256}", t0 + n * 0.001)  # all inside one window
        assert len(limiter) <= oauth_guard.MAX_LIMITER_KEYS
    assert len(limiter) == 4096
    assert limiter.max_keys == 4096
    assert len(limiter._overall) == 30  # the overall record is bounded too
    newest = "10.0.39.15"  # n = 9999
    assert oauth_guard.client_key(newest) in limiter._by_key
    assert oauth_guard.client_key("10.0.0.0") not in limiter._by_key  # the first one was forgotten to make room


def test_a_full_table_forgets_the_address_that_has_been_quiet_the_longest():
    limiter = oauth_guard.AttemptLimiter(per_key=2, per_key_window=100, overall=1000, overall_window=100_000, max_keys=3)
    limiter.record("192.0.2.1", 0)
    limiter.record("192.0.2.2", 50)
    limiter.record("192.0.2.3", 120)
    assert len(limiter) == 3
    limiter.record("192.0.2.1", 125)  # the first address fails again: it is the most recent now, not the quietest
    limiter.record("192.0.2.4", 130)  # no room: the quietest is the second address
    assert set(limiter._by_key) == {"192.0.2.1", "192.0.2.3", "192.0.2.4"}
    limiter.record("192.0.2.5", 131)
    assert set(limiter._by_key) == {"192.0.2.1", "192.0.2.4", "192.0.2.5"}
    assert len(limiter) == 3


def test_a_registration_limiter_counts_ten_per_address_an_hour_and_fifty_overall_a_day():
    limiter, t0 = oauth_guard.RegistrationLimiter(), 9_000_000.0
    for n in range(10):
        assert not limiter.is_blocked("203.0.113.7", t0 + n)
        limiter.record("203.0.113.7", t0 + n)
    assert limiter.is_blocked("203.0.113.7", t0 + 10) and not limiter.is_blocked("203.0.113.8", t0 + 10)
    assert limiter.is_blocked("203.0.113.7", t0 + 3599) and not limiter.is_blocked("203.0.113.7", t0 + 3600)
    for host in range(4):  # four more addresses with ten each: fifty in all
        for n in range(10):
            limiter.record(f"198.51.100.{host}", t0 + 100 + n)
    assert limiter.is_blocked("192.0.2.1", t0 + 200)
    assert limiter.is_blocked("192.0.2.1", t0 + DAY - 1) and not limiter.is_blocked("192.0.2.1", t0 + DAY)


def test_block_says_which_limit_stopped_an_address_and_for_how_long():
    limiter, t0 = limiter_for_failures(), 1_000_000.0
    assert limiter.block("203.0.113.7", t0) is None
    for attempt in range(5):  # five wrong passwords, one a second: this address is over its own limit
        limiter.record("203.0.113.7", t0 + attempt)
    assert limiter.block("203.0.113.7", t0 + 4) == oauth_guard.Blocked("ip", 896)  # the first leaves at t0 + 900
    assert limiter.block("203.0.113.7", t0 + 4.5) == oauth_guard.Blocked("ip", 896)  # a part of a second counts as a second
    assert limiter.block("203.0.113.7", t0 + 899) == oauth_guard.Blocked("ip", 1)
    assert limiter.block("203.0.113.7", t0 + 900) is None
    assert limiter.block("203.0.113.8", t0 + 4) is None  # the other addresses are not affected
    assert oauth_guard.SCOPE_IP == "ip" and oauth_guard.SCOPE_GLOBAL == "global"


def test_block_says_global_when_the_overall_limit_is_what_stopped_a_request_whoever_asks():
    limiter, t0 = limiter_for_failures(), 2_000_000.0
    for n in range(30):  # thirty addresses, one wrong password each, one a minute
        limiter.record(f"198.51.100.{n}", t0 + 60 * n)
    now = t0 + 60 * 29
    for host in ("192.0.2.1", "198.51.100.0", "198.51.100.29", "2001:db8::1"):  # a stranger, two guessers, an IPv6 address
        assert limiter.block(host, now) == oauth_guard.Blocked("global", 3600 - 60 * 29), host
    assert limiter.is_blocked("192.0.2.1", now)
    now = t0 + 3600  # the first of the thirty is an hour old: room for one more
    assert limiter.block("192.0.2.1", now) is None and not limiter.is_blocked("192.0.2.1", now)


def test_block_waits_for_the_longer_of_two_limits_and_says_global_when_both_apply():
    limiter, t0 = limiter_for_failures(), 3_000_000.0
    for n in range(25):  # twenty-five failures by other addresses, long ago: the overall limit will be the first to lapse
        limiter.record(f"198.51.100.{n}", t0 + n)
    for attempt in range(5):  # and five by this one, much later: its own limit lapses last
        limiter.record("203.0.113.7", t0 + 3000 + attempt)
    now = t0 + 3004
    assert limiter.block("203.0.113.7", now) == oauth_guard.Blocked("global", 896)  # not the 596 s the overall limit needs
    assert limiter.block("192.0.2.1", now) == oauth_guard.Blocked("global", 596)  # a stranger waits for the overall limit only
    now = t0 + 3600  # the oldest overall failure has left; this address is still over its own limit
    assert limiter.block("203.0.113.7", now) == oauth_guard.Blocked("ip", 300)
    assert limiter.block("192.0.2.1", now) is None


def test_is_blocked_is_block_as_a_yes_or_no_and_the_registration_limiter_says_the_same_things():
    limiter, t0 = oauth_guard.RegistrationLimiter(), 9_000_000.0
    for n in range(10):
        limiter.record("203.0.113.7", t0 + n)
    assert limiter.is_blocked("203.0.113.7", t0 + 10) and limiter.block("203.0.113.7", t0 + 10) == oauth_guard.Blocked("ip", 3590)
    assert not limiter.is_blocked("203.0.113.8", t0 + 10) and limiter.block("203.0.113.8", t0 + 10) is None
    for host in range(4):
        for n in range(10):
            limiter.record(f"198.51.100.{host}", t0 + 100 + n)
    assert limiter.block("192.0.2.1", t0 + 200) == oauth_guard.Blocked("global", DAY - 200)


# --------------------------------------------------------------------------
# What goes into a log line
# --------------------------------------------------------------------------


def test_safe_keeps_printable_ascii_and_escapes_everything_else():
    assert oauth_guard.safe("plain text 123") == "plain text 123"
    assert oauth_guard.safe("two\nlines\r\n") == "two\\x0alines\\x0d\\x0a"
    assert oauth_guard.safe("\x1b[31mred\x00\x7f") == "\\x1b[31mred\\x00\\x7f"
    assert oauth_guard.safe('say "hi" \\ bye') == 'say \\"hi\\" \\\\ bye'
    assert oauth_guard.safe("caf\N{LATIN SMALL LETTER E WITH ACUTE} \N{CJK UNIFIED IDEOGRAPH-4E2D} \U0001f600") == "caf\\xe9 \\u4e2d \\U0001f600"
    assert oauth_guard.safe("\N{RIGHT-TO-LEFT OVERRIDE}\N{ZERO WIDTH SPACE}") == "\\u202e\\u200b"  # bidirectional override, zero-width space
    for hostile in ("a\nb", "\x1b]0;title\x07", "\N{LINE SEPARATOR}", "x\x85y", "tab\there"):
        text = oauth_guard.safe(hostile)
        assert text.isascii() and text.isprintable() and "\n" not in text


def test_safe_is_at_most_80_characters_and_says_when_it_cut():
    assert oauth_guard.safe("x" * 80) == "x" * 80
    cut = oauth_guard.safe("x" * 81)
    assert len(cut) == 80 and cut == "x" * 77 + "..."
    assert oauth_guard.safe("\n" * 100) == "\\x0a" * 19 + "..." and len(oauth_guard.safe("\n" * 100)) <= 80
    assert len(oauth_guard.safe("\U0001f600" * 50)) <= 80  # ten characters each
    assert len(oauth_guard.safe("y" * 10_000_000)) == 80  # and it does not walk the whole text to find that out
    assert oauth_guard.safe("x" * 30, limit=10) == "x" * 7 + "..."


def test_safe_has_a_word_for_nothing_and_never_raises():
    assert oauth_guard.safe(None) == "-" and oauth_guard.safe("") == "-"
    assert oauth_guard.safe(12345) == "12345"

    class Hostile:
        def __str__(self):
            raise RuntimeError("no")

    assert oauth_guard.safe(Hostile()) == "?"


def test_safe_host_shows_the_host_and_nothing_else_of_an_address():
    assert oauth_guard.safe_host("https://evil.example/path?token=secret&x=1#frag") == "evil.example"
    assert oauth_guard.safe_host(AnyUrl("https://claude.ai/api/mcp/auth_callback?code=abc")) == "claude.ai"
    # the host that a redirect would go to (a backslash is a slash), not the one a naive reading of the text finds
    assert oauth_guard.safe_host("https://evil.example\\@claude.ai/cb") == "evil.example"
    assert oauth_guard.safe_host("https://claude.ai\\@evil.example/cb") == "claude.ai"
    assert oauth_guard.safe_host("not a url\nINFO forged") == "?"
    assert oauth_guard.safe_host("http://[::1]:8080/cb") == "[::1]"
    assert oauth_guard.safe_host(None) == "?"
    assert oauth_guard.safe_host("https://" + "a" * 200 + ".example/x").endswith("...")


# --------------------------------------------------------------------------
# The password compare
# --------------------------------------------------------------------------


def test_the_compare_is_hmac_compare_digest_on_nfc_utf8_bytes(compare_spy):
    configured = "caf\N{LATIN SMALL LETTER E WITH ACUTE}-" + "x" * 12  # composed e acute
    for submitted in (configured, unicodedata.normalize("NFD", configured)):
        assert oauth_guard.password_matches(submitted, configured) is True
    assert oauth_guard.password_matches("caf\N{LATIN SMALL LETTER E WITH ACUTE}-" + "y" * 12, configured) is False
    nfc = unicodedata.normalize("NFC", configured).encode("utf-8")
    assert compare_spy[0] == (nfc, nfc) and compare_spy[1] == (nfc, nfc)  # bytes, composed, both sides
    assert all(isinstance(left, bytes) and isinstance(right, bytes) for left, right in compare_spy)
    assert oauth_guard.password_matches(configured, unicodedata.normalize("NFD", configured)) is True  # stored decomposed


@pytest.mark.parametrize(
    "submitted, configured",
    [
        ("\ud800", "secret"),  # a lone surrogate cannot be encoded as UTF-8
        ("secret", "\udfff"),
        (None, "secret"),
        (b"secret", "secret"),
        ("secret", None),
        ("secret", ""),
        ("", ""),  # nothing configured is never a match, not even for nothing
        ("   ", "   "),
        ("anything", "   "),
        (5, 5),
    ],
)
def test_the_compare_never_raises_and_never_matches_when_it_cannot_be_trusted(submitted, configured):
    assert oauth_guard.password_matches(submitted, configured) is False


def test_surrounding_spaces_are_part_of_a_password():
    assert oauth_guard.password_matches("  pw with spaces  ", "  pw with spaces  ") is True
    assert oauth_guard.password_matches("pw with spaces", "  pw with spaces  ") is False
    assert oauth_guard.require_password("  pw  ") == "  pw  "


@pytest.mark.parametrize("password", [None, "", "   ", "\t\n", 12345, b"bytes", "\ud800"])
def test_a_password_that_cannot_be_compared_is_refused(password):
    with pytest.raises(GateError):
        oauth_guard.require_password(password)


def test_a_short_password_is_one_with_fewer_than_16_characters_counted_after_nfc():
    assert oauth_guard.password_is_short("x" * 15) and not oauth_guard.password_is_short("x" * 16)
    assert not oauth_guard.password_is_short("e\N{COMBINING ACUTE ACCENT}" * 16)  # sixteen letters, written as 32 code points
    assert oauth_guard.password_is_short("e\N{COMBINING ACUTE ACCENT}" * 15)


# --------------------------------------------------------------------------
# The redirect validator
# --------------------------------------------------------------------------

ALLOWED = ["claude.ai", "claude.com", "localhost"]

ACCEPTABLE_REDIRECTS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://CLAUDE.AI/api/mcp/auth_callback",  # the host is read in lower case
    "https://www.claude.ai/cb",  # a subdomain of an entry
    "https://a.b.claude.ai/cb?x=1",
    "https://claude.ai:443/cb",
    "https://claude.ai:8443/cb",
    "https://claude.ai/cb?next=https://evil.example",  # in the query it is just text
    "http://localhost:6274/oauth/callback",  # http is for loopback only
    "http://localhost/cb",
    "https://localhost:8080/cb",
]

# (address, why the validator refuses it)
UNACCEPTABLE_REDIRECTS = [
    ("https://evil.example/cb", "not_allowed"),
    ("https://evilclaude.ai/cb", "not_allowed"),  # the suffix trick
    ("https://claude.ai.evil.example/cb", "not_allowed"),
    ("https://notclaude.ai/cb", "not_allowed"),
    ("https://claude.ai./cb", "not_allowed"),
    ("https://claude.ai%2eevil.example/cb", "not_allowed"),  # an escaped dot is a dot
    ("https://203.0.113.4/cb", "not_allowed"),
    ("https://[::1]/cb", "not_allowed"),
    ("https://claude.ai@evil.example/cb", "userinfo"),  # the userinfo trick
    ("https://user:pw@claude.ai/cb", "userinfo"),
    ("https://claude.ai:@evil.example/cb", "userinfo"),
    ("https://:pw@claude.ai/cb", "userinfo"),  # a password and no user name is still userinfo
    ("https://claude.ai\\@evil.example/cb", "characters"),  # the backslash trick, both ways round
    ("https://evil.example\\@claude.ai/cb", "characters"),
    ("https://claude.ai/cb\\x", "characters"),
    ("https://claude.ai\t.evil.example/cb", "characters"),  # normalization would remove the tab
    ("https://claude.ai/cb\n", "characters"),
    (" https://claude.ai/cb", "characters"),
    ("https://exa mple.com/cb", "characters"),
    ("https://claude.ai\x00/cb", "characters"),
    ("https://claude.ai/caf\N{LATIN SMALL LETTER E WITH ACUTE}", "characters"),
    ("https://\N{CYRILLIC SMALL LETTER ES}laude.ai/cb", "characters"),  # a Cyrillic s
    ("https://claude.ai/cb#frag", "fragment"),
    ("https://claude.ai/cb#", "fragment"),
    ("http://claude.ai/cb", "not_https"),  # http on an allowed domain that is not loopback
    ("http://evil.example/cb", "not_https"),
    ("http://localhost.evil.example/cb", "not_https"),
    ("http://127.0.0.1:8080/cb", "not_allowed"),  # loopback, but the allowlist does not name it
    ("http://127.1:80/cb", "not_allowed"),
    ("http://[::1]:8080/cb", "not_allowed"),
    ("javascript:alert(1)", "scheme"),
    ("data:text/html,hi", "scheme"),
    ("ftp://claude.ai/cb", "scheme"),
    ("myapp://callback", "scheme"),
    ("", "empty"),
    ("claude.ai/cb", "unparseable"),
    ("//claude.ai/cb", "unparseable"),
    ("https://", "unparseable"),
    ("https://claude.ai:99999/cb", "unparseable"),
    ("https://claude.ai%40evil.example/cb", "unparseable"),
    ("https://claude.ai/" + "a" * 2100, "too_long"),
]


@pytest.mark.parametrize("uri", ACCEPTABLE_REDIRECTS)
def test_an_acceptable_redirect_address_passes(uri):
    assert oauth_guard.redirect_problem(uri, ALLOWED) is None


@pytest.mark.parametrize("uri, reason", UNACCEPTABLE_REDIRECTS, ids=[repr(uri)[:60] for uri, _ in UNACCEPTABLE_REDIRECTS])
def test_an_unacceptable_redirect_address_is_refused_and_says_why(uri, reason):
    assert oauth_guard.redirect_problem(uri, ALLOWED) == reason


def test_the_validator_reads_the_normalized_address_so_what_it_approves_is_what_is_redirected_to():
    # pydantic turns a backslash into a slash and drops tabs: the host a browser would go to is what is judged
    for raw in ("https://evil.example\\@claude.ai/cb", "https://claude.ai\t.evil.example/cb"):
        assert oauth_guard.redirect_problem(AnyUrl(raw), ALLOWED) == "not_allowed", raw
    # and an address object is judged by its normalized text, which is what gets stored and redirected to
    assert oauth_guard.redirect_problem(AnyUrl("https://CLAUDE.AI:443/cb"), ALLOWED) is None
    assert str(AnyUrl("https://CLAUDE.AI:443/cb")) == "https://claude.ai/cb"


def test_the_validator_refuses_an_address_that_the_standard_library_would_read_differently(monkeypatch):
    # the redirect is built from the normalized text with urllib, so the validator asks urllib too and wants the same answer
    from urllib.parse import urlsplit as real_split

    def reading(**replacement):
        return lambda text: real_split(text)._replace(**replacement)

    assert oauth_guard.redirect_problem(REDIRECT, ALLOWED) is None
    for replacement in (
        {"netloc": "evil.example"},  # another host
        {"netloc": "claude.ai:99999"},  # a port that is not a port
        {"netloc": "user@claude.ai"},  # userinfo that pydantic did not see
        {"netloc": ":pw@claude.ai"},
        {"scheme": "http"},  # another scheme
    ):
        monkeypatch.setattr(oauth_guard, "urlsplit", reading(**replacement))
        assert oauth_guard.redirect_problem(REDIRECT, ALLOWED) == "mismatch", replacement
    monkeypatch.setattr(oauth_guard, "urlsplit", reading(netloc="CLAUDE.AI"))  # the case of a host is not a difference
    assert oauth_guard.redirect_problem(REDIRECT, ALLOWED) is None


def test_the_allowlist_can_be_changed_and_http_still_means_loopback():
    assert oauth_guard.redirect_problem("http://localhost:1/cb", ["claude.ai"]) == "not_allowed"  # not named
    assert oauth_guard.redirect_problem("http://127.0.0.1:8080/cb", ["claude.ai", "127.0.0.1"]) is None
    assert oauth_guard.redirect_problem("http://[::1]:1/cb", ["::1"]) is None
    assert oauth_guard.redirect_problem("http://[::1]:1/cb", ["[::1]"]) is None
    assert oauth_guard.redirect_problem("http://claude.ai/cb", ["claude.ai"]) == "not_https"
    # None admits every host, but still not http away from loopback, and still no userinfo or fragment
    assert oauth_guard.redirect_problem("https://anything.example/cb", None) is None
    assert oauth_guard.redirect_problem("http://anything.example/cb", None) == "not_https"
    assert oauth_guard.redirect_problem("http://localhost:1/cb", None) is None
    assert oauth_guard.redirect_problem("https://u@anything.example/cb", None) == "userinfo"
    assert oauth_guard.redirect_problem("https://anything.example/cb#x", None) == "fragment"


def test_a_loopback_address_in_ipv6_clothes_is_loopback_and_another_one_is_not():
    assert oauth_guard.redirect_problem("http://[::ffff:127.0.0.1]:8080/cb", ["::ffff:7f00:1"]) is None
    assert oauth_guard.redirect_problem("http://[::ffff:127.0.0.1]:8080/cb", ALLOWED) == "not_allowed"
    assert oauth_guard.redirect_problem("http://[::ffff:203.0.113.7]:8080/cb", ["::ffff:cb00:7107"]) == "not_https"


def test_the_validator_does_not_trust_a_normalizer_that_hands_back_something_unexpected(monkeypatch):
    class Odd:
        def __init__(self, text, host="claude.ai", scheme="https"):
            self.text, self.host, self.scheme, self.username, self.password = text, host, scheme, None, None

        def __str__(self):
            return self.text

    monkeypatch.setattr(oauth_guard, "AnyUrl", lambda text: Odd("https://claude.ai/a b"))  # a space after normalizing
    assert oauth_guard.redirect_problem(REDIRECT, ALLOWED) == "characters"
    monkeypatch.setattr(oauth_guard, "AnyUrl", lambda text: Odd("https://claude.ai/cb", host=None))  # no host
    assert oauth_guard.redirect_problem(REDIRECT, ALLOWED) == "host"
    monkeypatch.setattr(oauth_guard, "AnyUrl", lambda text: Odd("https://claude.ai/cb", host=""))
    assert oauth_guard.redirect_problem(REDIRECT, ALLOWED) == "host"


def test_an_allowlist_entry_that_is_not_a_plain_host_name_can_only_make_the_list_shorter():
    odd = ["", None, 5, "bad domain", "a/b", "claude.ai"]
    assert oauth_guard.redirect_problem("https://claude.ai/cb", odd) is None
    assert oauth_guard.redirect_problem("https://evil.example/cb", odd) == "not_allowed"
    assert oauth_guard.redirect_problem("https://evil.example/cb", [""]) == "not_allowed"
    assert oauth_guard.redirect_problem("https://evil.example/cb", []) == "not_allowed"


def test_a_registration_without_redirect_addresses_is_a_problem_and_so_is_one_bad_address_among_good_ones():
    assert oauth_guard.registration_problem(None, ALLOWED) == ("missing", "-")
    assert oauth_guard.registration_problem([], ALLOWED) == ("missing", "-")
    assert oauth_guard.registration_problem([AnyUrl(REDIRECT)], ALLOWED) is None
    both = [AnyUrl(REDIRECT), AnyUrl("https://evil.example/cb?secret=1")]
    assert oauth_guard.registration_problem(both, ALLOWED) == ("not_allowed", "evil.example")


async def test_the_old_name_of_the_check_still_exists_and_uses_the_validator(state_dir, make_provider):
    provider = make_provider(state_dir)
    assert provider._is_redirect_allowed(REDIRECT) is True
    assert provider._is_redirect_allowed("https://claude.ai\\@evil.example/cb") is False
    assert provider._is_redirect_allowed("https://claude.ai@evil.example/cb") is False
    assert provider._is_redirect_allowed("http://claude.ai/cb") is False
    # the constructor has no setting that allows every domain: None means the default list
    assert make_provider(state_dir.parent / "none", allowed_redirect_domains=None).allowed_redirect_domains == ALLOWED
    narrower = make_provider(state_dir.parent / "narrow", allowed_redirect_domains=["claude.ai"])
    assert narrower._is_redirect_allowed(REDIRECT) is True and narrower._is_redirect_allowed("http://localhost:1/cb") is False
    # the validator's own meaning of "no list": every https host, still never http away from loopback
    provider.allowed_redirect_domains = None
    assert provider._is_redirect_allowed("https://anything.example/cb") is True
    assert provider._is_redirect_allowed("http://anything.example/cb") is False


# --------------------------------------------------------------------------
# Registrations that try to get an open redirect
# --------------------------------------------------------------------------

EVIL_REGISTRATIONS = [
    ["https://evil.example/cb"],
    ["https://claude.ai@evil.example/cb"],  # userinfo trick
    ["https://claude.ai:443@evil.example/cb"],
    ["https://evil.example\\@claude.ai/cb"],  # backslash trick: a browser goes to evil.example
    ["https://claude.ai\t.evil.example/cb"],  # a tab that normalization removes
    ["https://evilclaude.ai/cb"],  # suffix tricks
    ["https://claude.ai.evil.example/cb"],
    ["http://claude.ai/cb"],  # http away from loopback
    ["http://evil.example/cb"],
    ["http://127.0.0.1:8080/cb"],  # loopback, but the allowlist does not name it
    ["javascript:alert(1)"],
    ["data:text/html,hi"],
    ["ftp://claude.ai/cb"],
    ["myapp://callback"],
    ["https://claude.ai/cb#fragment"],
    ["https://claude.ai./cb"],
    [REDIRECT, "https://evil.example/cb"],  # one good address does not excuse a bad one
    ["https://evil.example/cb", REDIRECT],
    None,
]


@pytest.mark.parametrize("redirect_uris", EVIL_REGISTRATIONS, ids=[repr(uris)[:70] for uris in EVIL_REGISTRATIONS])
async def test_a_registration_with_an_unacceptable_redirect_address_is_refused_and_stores_nothing(
    state_dir, make_provider, serve, caplog, redirect_uris
):
    capture(caplog)
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        reply = await register_over_http(gate.at(), redirect_uris=redirect_uris)
    assert reply.status_code == 400
    body = reply.json()
    assert body["error"] == "invalid_redirect_uri" and "client_id" not in body and "client_secret" not in body
    assert provider.clients == {}
    assert not (state_dir / STATE_FILE).exists()  # nothing was saved either
    (line,) = [m for m in lines(caplog) if m.startswith("register outcome=")]
    assert line.startswith("register outcome=refused reason=redirect_") and f"ip={OWNER_IP}" in line


async def test_an_empty_list_of_redirect_addresses_is_refused_by_the_framework_before_the_provider_sees_it(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        reply = await register_over_http(gate.at(), redirect_uris=[])
    assert reply.status_code == 400 and reply.json()["error"] == "invalid_client_metadata"
    assert provider.clients == {}


@pytest.mark.parametrize(
    "redirect",
    [
        REDIRECT,
        "https://claude.com/api/mcp/auth_callback",
        "https://www.claude.ai/cb",
        "http://localhost:6274/oauth/callback",
        "https://CLAUDE.AI:443/cb",
    ],
)
async def test_a_registration_with_acceptable_redirect_addresses_works_and_is_saved_normalized(state_dir, make_provider, serve, redirect):
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        reply = await register_over_http(gate.at(), redirect_uris=[redirect])
    assert reply.status_code == 201
    body = reply.json()
    assert body["client_id"] in provider.clients and body["client_secret"]
    stored = json.loads((state_dir / STATE_FILE).read_text())["clients"][body["client_id"]]["redirect_uris"]
    assert stored == [str(AnyUrl(redirect))]


async def test_an_address_that_only_looks_like_a_trick_but_goes_to_an_allowed_host_is_stored_as_the_browser_reads_it(
    state_dir, make_provider, serve
):
    # "https://claude.ai\\@evil.example/cb" is, for a browser and for pydantic, host claude.ai with the path /@evil.example/cb
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        reply = await register_over_http(gate.at(), redirect_uris=["https://claude.ai\\@evil.example/cb"])
    assert reply.status_code == 201
    assert reply.json()["redirect_uris"] == ["https://claude.ai/@evil.example/cb"]


async def test_a_registration_with_several_acceptable_addresses_works(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        reply = await register_over_http(gate.at(), redirect_uris=[REDIRECT, "https://claude.com/api/mcp/auth_callback", "http://localhost:3000/cb"])
    assert reply.status_code == 201 and len(reply.json()["redirect_uris"]) == 3


async def test_registering_with_the_provider_directly_runs_the_same_check(state_dir, make_provider):
    provider = make_provider(state_dir)
    evil = OAuthClientInformationFull(client_id="evil-1", redirect_uris=[AnyUrl("https://evil.example/cb")])
    with pytest.raises(RegistrationError) as info:
        await provider.register_client(evil)
    assert info.value.error == "invalid_redirect_uri" and provider.clients == {}
    nothing = OAuthClientInformationFull(client_id="none-1", redirect_uris=None)
    with pytest.raises(RegistrationError) as info:
        await provider.register_client(nothing)
    assert info.value.error == "invalid_redirect_uri" and provider.clients == {}
    good = OAuthClientInformationFull(client_id="good-1", redirect_uris=[AnyUrl(REDIRECT)])
    await provider.register_client(good)
    assert set(provider.clients) == {"good-1"}


# --------------------------------------------------------------------------
# The consent page (GET)
# --------------------------------------------------------------------------


async def test_a_valid_request_gets_the_consent_page_with_the_headers_and_one_log_line(state_dir, make_provider, serve, caplog):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await make_client(provider, name="Claude")
    caplog.clear()
    async with serve(provider) as gate:
        reply = await gate.at().get("/authorize", params=params(client))
    assert reply.status_code == 200 and reply.headers["content-type"].startswith("text/html")
    page = reply.text
    assert "<h1>Authorize MCP Access</h1>" in page and "<dd>Claude</dd>" in page
    assert f"<dd>{client.client_id}</dd>" in page and f"<dd>{REDIRECT}</dd>" in page and "<dd>(none)</dd>" in page
    assert 'type="password"' in page and 'name="decision" value="approve"' in page and 'name="decision" value="deny"' in page
    assert '<form method="POST" action="/authorize"' in page
    assert hidden_inputs(page) == params(client)  # the five fields that were sent come back, nothing else
    assert "<script" not in page.lower() and "javascript:" not in page.lower() and "Incorrect password" not in page
    assert [m for m in lines(caplog) if m.startswith("authorize")] == [
        f'authorize outcome=consent_shown client={client.client_id[:8]} name="Claude" ip={OWNER_IP}'
    ]


async def test_optional_fields_that_are_empty_count_as_absent_the_way_the_form_always_carried_them(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    async with serve(provider) as gate:
        reply = await gate.at().get(
            "/authorize", params=params(client, state="", scope="", resource="", code_challenge_method=None)
        )
        assert reply.status_code == 200
        assert hidden_inputs(reply.text) == params(client, state=None, code_challenge_method=None)
        # and they are what the framework would call "not sent" at the end: the code carries no scope and no state
        approved = await post_consent(gate.at(), client, state="", scope="", resource="")
    assert approved.status_code == 302
    assert "state" not in location_params(approved) and "code" in location_params(approved)


async def test_the_scope_the_client_registered_is_shown_and_carried(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await make_client(provider, scope="read write")
    async with serve(provider) as gate:
        reply = await gate.at().get("/authorize", params=params(client, scope="read", resource="https://mcp.example.test/mcp"))
        assert reply.status_code == 200 and "<dd>read</dd>" in reply.text
        assert hidden_inputs(reply.text)["scope"] == "read" and hidden_inputs(reply.text)["resource"] == "https://mcp.example.test/mcp"
        approved = await post_consent(gate.at(), client, scope="read")
    code = provider.auth_codes[location_params(approved)["code"][0]]
    assert code.scopes == ["read"]


async def test_the_page_can_be_sent_back_as_a_browser_would_and_approves(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    async with serve(provider) as gate:
        page = await gate.at().get("/authorize", params=params(client, resource="https://mcp.example.test/mcp"))
        form = {**hidden_inputs(page.text), "password": PASSWORD, "decision": "approve"}
        approved = await gate.at().post("/authorize", data=form)
    assert approved.status_code == 302
    assert approved.headers["location"].startswith(REDIRECT + "?") and location_params(approved)["state"] == ["state-1"]


async def test_a_redirect_address_that_is_left_out_is_the_clients_only_registered_one_and_a_second_one_makes_that_ambiguous(
    state_dir, make_provider, serve
):
    provider = make_provider(state_dir)
    single = await make_client(provider, 1)
    async with serve(provider) as gate:
        reply = await gate.at().get("/authorize", params=params(single, redirect_uri=None))
        assert reply.status_code == 200 and f"<dd>{REDIRECT}</dd>" in reply.text
        approved = await post_consent(gate.at(), single, redirect_uri=None)
        assert approved.status_code == 302 and approved.headers["location"].startswith(REDIRECT + "?")
        code = provider.auth_codes[location_params(approved)["code"][0]]
        assert code.redirect_uri_provided_explicitly is False

        two = OAuthClientInformationFull(
            client_id="two-uris", redirect_uris=[AnyUrl(REDIRECT), AnyUrl("https://claude.com/api/mcp/auth_callback")]
        )
        await provider.register_client(two)
        assert (await gate.at().get("/authorize", params=params(two, redirect_uri=None))).status_code == 400  # which one?
        chosen = await gate.at().get("/authorize", params=params(two, redirect_uri="https://claude.com/api/mcp/auth_callback"))
        assert chosen.status_code == 200 and "<dd>https://claude.com/api/mcp/auth_callback</dd>" in chosen.text


async def test_everything_printed_on_the_page_is_escaped_and_a_hostile_name_cannot_pretend_to_be_something_else(
    state_dir, make_provider, serve
):
    provider = make_provider(state_dir)
    nasty = '<script>alert(1)</script>"\'&<img src=x onerror=alert(2)>'
    client = await make_client(provider, name=nasty, scope='read"x <b>')
    spoof = await make_client(provider, 2, name="Cla\N{RIGHT-TO-LEFT OVERRIDE}ude\N{ZERO WIDTH SPACE} Admin\x00\x1b[31m" + "N" * 300)
    async with serve(provider) as gate:
        page = (await gate.at().get("/authorize", params=params(client, scope='read"x', state='"><script>x</script>'))).text
        spoofed = (await gate.at().get("/authorize", params=params(spoof))).text
    assert "<script" not in page and "<img" not in page and "onerror=alert(2)>" not in page
    assert html.escape(nasty, quote=True) in page
    assert hidden_inputs(page)["state"] == '"><script>x</script>' and 'value=""><script>' not in page
    assert hidden_inputs(page)["scope"] == 'read"x'
    # control, bidirectional and zero-width characters are not shown, and a long name is cut
    shown = re.search(r"<dt>Client</dt><dd>(.*?)</dd>", spoofed).group(1)
    assert "\N{RIGHT-TO-LEFT OVERRIDE}" not in spoofed and "\N{ZERO WIDTH SPACE}" not in spoofed and "\x00" not in spoofed and "\x1b" not in spoofed
    assert shown.startswith("Claude Admin[31m") and len(shown) <= personal_auth.CLIENT_NAME_DISPLAY_CHARS


async def test_a_client_without_a_name_is_shown_as_unnamed(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    provider.clients[client.client_id].client_name = None
    async with serve(provider) as gate:
        page = (await gate.at().get("/authorize", params=params(client))).text
    assert "<dd>(unnamed client)</dd>" in page


# --------------------------------------------------------------------------
# One page for every invalid request, and the password never looked at for it
# --------------------------------------------------------------------------

INVALID_REQUESTS = {
    "blank client_id": {"client_id": ""},
    "missing client_id": {"client_id": None},
    "client_id of spaces": {"client_id": "   "},
    "unknown client": {"client_id": "ffffffff-aaaa-4bbb-8ccc-ffffffffffff"},
    "client_id with a newline and escape codes": {"client_id": "x\nINFO forged\x1b[31m"},
    "client_id over the field limit": {"client_id": "c" * 5000},
    "non-ASCII client_id": {"client_id": "caf\N{LATIN SMALL LETTER E WITH ACUTE}-\N{CJK UNIFIED IDEOGRAPH-4E2D}-\U0001f600"},
    "response_type token": {"response_type": "token"},
    "response_type missing": {"response_type": None},
    "no code_challenge": {"code_challenge": None},
    "short code_challenge": {"code_challenge": "abc"},
    "code_challenge outside the unreserved set": {"code_challenge": "!" * 43},
    "code_challenge_method plain": {"code_challenge_method": "plain"},
    "code_challenge_method S512": {"code_challenge_method": "S512"},
    "redirect_uri that is not registered": {"redirect_uri": "https://claude.ai/other"},
    "evil redirect_uri": {"redirect_uri": "https://evil.example/cb"},
    "backslash redirect_uri": {"redirect_uri": "https://evil.example\\@claude.ai/api/mcp/auth_callback"},
    "redirect_uri with a newline": {"redirect_uri": REDIRECT + "\nX"},
    "the registered address written with backslashes": {"redirect_uri": "https://claude.ai\\api\\mcp\\auth_callback"},
    "the registered address with a tab in it": {"redirect_uri": "https://claude.ai/api/mcp/auth_\tcallback"},
    "the registered address with a space after it": {"redirect_uri": REDIRECT + " "},
    "javascript redirect_uri": {"redirect_uri": "javascript:alert(1)"},
    "redirect_uri that is not an address": {"redirect_uri": "claude.ai/cb"},
    "redirect_uri of two slashes": {"redirect_uri": "//"},
    "scope the client does not have": {"scope": "admin"},
    "state over the field limit": {"state": "s" * 5000},
}


def assert_the_generic_page(response: httpx.Response) -> None:
    assert response.status_code == 400
    assert response.content == oauth_guard.INVALID_REQUEST_PAGE.encode()
    assert "location" not in response.headers
    for name, value in CONSENT_HEADERS.items():
        assert response.headers[name] == value


@pytest.mark.parametrize("override", INVALID_REQUESTS.values(), ids=INVALID_REQUESTS.keys())
async def test_every_invalid_request_gets_the_same_400_page_whatever_the_password_and_the_password_is_not_looked_at(
    state_dir, make_provider, serve, compare_spy, caplog, override
):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await make_client(provider)
    caplog.clear()
    async with serve(provider) as gate:
        http = gate.at()
        seen = [
            await http.get("/authorize", params=params(client, **override)),
            await post_consent(http, client, password=PASSWORD, **override),  # the right password
            await post_consent(http, client, password="not the password", **override),
            await post_consent(http, client, password=None, **override),
            await post_consent(http, client, password=PASSWORD, decision="deny", **override),  # not even Deny gets through
            await post_consent(http, client, password=PASSWORD, decision=None, **override),
        ]
        reference = await http.get("/authorize", params={"client_id": ""})
    assert_the_generic_page(reference)
    for response in seen:
        assert signature(response) == signature(reference)
    # not one thing of the request is repeated back
    for value in override.values():
        if value:
            assert value not in reference.text
    assert compare_spy == []  # the password was never compared
    assert counters(provider) == (0, 0)  # nothing was counted against the address
    assert provider.auth_codes == {}
    assert not [m for m in lines(caplog) if "approved" in m or "wrong_password" in m or "denied_by_user" in m]
    assert len([m for m in lines(caplog) if m.startswith("authorize outcome=invalid_request")]) == 7


async def test_the_generic_page_is_a_fixed_page_with_no_data_of_the_request_in_it(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        reply = await gate.at().get("/authorize")
    assert_the_generic_page(reply)
    visible = reply.text.split("</style>", 1)[1]  # after the style sheet, which is shared with the consent page
    assert "<form" not in visible and "password" not in visible.lower() and "<input" not in visible
    assert reply.text == oauth_guard.notice_page(
        "Request not valid", "This authorization request is not valid. Go back to the application and start the sign-in again."
    )


async def test_a_valid_request_with_a_wrong_password_does_reach_the_compare_and_is_counted(
    state_dir, make_provider, serve, compare_spy, caplog
):
    # the other half of the test above: it is the check of the request that keeps the compare away, not something else
    capture(caplog)
    provider = make_provider(state_dir)
    client = await make_client(provider)
    caplog.clear()
    async with serve(provider) as gate:
        reply = await post_consent(gate.at(), client, password="not the password")
    assert reply.status_code == 200 and "Incorrect password. Please try again." in reply.text
    assert len(compare_spy) == 1 and counters(provider) == (1, 1)
    assert hidden_inputs(reply.text) == params(client)  # the form is shown again with the request in it
    assert "not the password" not in reply.text
    for name, value in CONSENT_HEADERS.items():
        assert reply.headers[name] == value
    assert [m for m in lines(caplog) if m.startswith("authorize")] == [
        f'authorize outcome=wrong_password client={client.client_id[:8]} name="Test client 1" ip={OWNER_IP}'
    ]


async def test_the_checks_come_in_the_order_content_type_fields_request_deny_limiter_password(
    state_dir, make_provider, serve, compare_spy
):
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)
    client = await make_client(provider)
    good = {**params(client), "decision": "approve", "password": PASSWORD}
    async with serve(provider) as gate:
        http = gate.at()
        # 1. the content type: a JSON body is not read at all
        as_json = await http.post("/authorize", json=good)
        assert as_json.status_code == 415 and as_json.content == oauth_guard.UNSUPPORTED_MEDIA_PAGE.encode()
        # 2. the fields: a file part where text belongs is refused before the request is looked at
        files = {key: (None, value) for key, value in good.items() if key != "password"}
        files["password"] = ("password.txt", PASSWORD.encode(), "text/plain")
        assert_the_generic_page(await http.post("/authorize", files=files))
        # 3. the request: an invalid one is refused before Deny is looked at (no redirect to anywhere)
        assert_the_generic_page(await post_consent(http, client, decision="deny", response_type="token"))
        assert compare_spy == [] and counters(provider) == (0, 0)
        # 4. Deny comes before the limiter: an address that is over the limit can still say no
        for _ in range(5):
            await post_consent(http, client, password="wrong")
        assert (await post_consent(http, client)).status_code == 429  # over the limit, even with the right password
        denied = await post_consent(http, client, decision="deny")
        assert denied.status_code == 302 and location_params(denied)["error"] == ["access_denied"]
        # 5. the limiter comes before the compare: the 429 above did not compare anything
        assert len(compare_spy) == 5
        # 6. and only then the password
        clock.advance(900)
        approved = await post_consent(http, client)
        assert approved.status_code == 302 and "code" in location_params(approved) and len(compare_spy) == 6


async def test_if_authorize_refuses_after_the_password_matched_the_answer_is_the_generic_page_and_no_redirect(
    state_dir, make_provider, serve, monkeypatch, caplog
):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await make_client(provider)

    async def refusing(self, client_, params_):
        raise AuthorizeError(error="server_error", error_description="something the caller must not see")

    monkeypatch.setattr(PersonalAuthProvider, "authorize", refusing)
    caplog.clear()
    async with serve(provider) as gate:
        await post_consent(gate.at(), client, password="wrong")
        reply = await post_consent(gate.at(), client)
    assert_the_generic_page(reply)
    assert "something the caller must not see" not in reply.text + caplog.text
    assert counters(provider) == (1, 1)  # a refused sign-in is not a proof of anything: the wrong guess is still on record
    assert [m for m in lines(caplog) if "authorize_server_error" in m] == [
        f"authorize outcome=invalid_request reason=authorize_server_error client={client.client_id[:8]} ip={OWNER_IP}"
    ]


async def test_the_framework_handler_for_authorize_is_never_called(state_dir, make_provider, serve, monkeypatch):
    async def forbidden(self, request):
        raise AssertionError("the framework's /authorize handler was used")

    monkeypatch.setattr(AuthorizationHandler, "handle", forbidden)
    provider = make_provider(state_dir)
    client = await make_client(provider)
    async with serve(provider) as gate:
        http = gate.at()
        assert (await http.get("/authorize", params=params(client))).status_code == 200
        assert (await http.get("/authorize", params=params(client, response_type="token"))).status_code == 400
        assert (await post_consent(http, client, password="wrong")).status_code == 200
        assert (await post_consent(http, client, decision="deny")).status_code == 302
        assert (await post_consent(http, client)).status_code == 302


# --------------------------------------------------------------------------
# A client that was registered before the validator existed
# --------------------------------------------------------------------------

LEGACY_EVIL = "00000000-0000-4000-8000-0000000000e1"
LEGACY_GOOD = "00000000-0000-4000-8000-0000000000a1"
LEGACY_MIXED = "00000000-0000-4000-8000-0000000000b1"
LEGACY_TRICK = "00000000-0000-4000-8000-0000000000c1"


@pytest.fixture
def legacy_provider(state_dir, make_provider):
    """A provider that loaded a version 1 file in which one client has an evil address, one has a good one, one has both,
    and one has what the backslash trick stores (the normalized form). Registration used to take any of them."""
    state_dir.mkdir()
    clients = {
        LEGACY_EVIL: legacy_client_record(LEGACY_EVIL, ["https://evil.example/cb"], "Evil"),
        LEGACY_GOOD: legacy_client_record(LEGACY_GOOD, [REDIRECT], "Claude"),
        LEGACY_MIXED: legacy_client_record(LEGACY_MIXED, ["https://evil.example/cb", REDIRECT], "Mixed"),
        LEGACY_TRICK: legacy_client_record(LEGACY_TRICK, ["https://evil.example\\@claude.ai/api/mcp/auth_callback"], "Trick"),
    }
    (state_dir / STATE_FILE).write_text(json.dumps(v1_file(clients), indent=2))
    provider = make_provider(state_dir)
    assert provider.clients[LEGACY_TRICK].redirect_uris == [AnyUrl("https://evil.example/@claude.ai/api/mcp/auth_callback")]
    return provider


async def test_a_legacy_client_with_an_evil_address_gets_the_400_page_on_get_deny_and_approve_and_is_never_redirected(
    legacy_provider, serve, compare_spy, caplog
):
    capture(caplog)
    provider = legacy_provider
    caplog.clear()
    async with serve(provider) as gate:
        http = gate.at()
        reference = await http.get("/authorize", params={"client_id": ""})
        for client_id, redirect in (
            (LEGACY_EVIL, "https://evil.example/cb"),
            (LEGACY_EVIL, None),  # the address left out: the only registered one is the evil one
            (LEGACY_TRICK, "https://evil.example/@claude.ai/api/mcp/auth_callback"),
        ):
            query = {"client_id": client_id, "response_type": "code", "code_challenge": CHALLENGE, "state": "s"}
            if redirect:
                query["redirect_uri"] = redirect
            seen = [
                await http.get("/authorize", params=query),
                await http.post("/authorize", data={**query, "decision": "approve", "password": PASSWORD}),
                await http.post("/authorize", data={**query, "decision": "approve", "password": "wrong"}),
                await http.post("/authorize", data={**query, "decision": "deny"}),
            ]
            for response in seen:
                assert signature(response) == signature(reference), (client_id, redirect)
                assert "location" not in response.headers
    assert_the_generic_page(reference)
    assert compare_spy == [] and counters(provider) == (0, 0) and provider.auth_codes == {}
    blocked = [m for m in lines(caplog) if m.startswith("authorize outcome=blocked_redirect")]
    assert len(blocked) == 12
    assert blocked[0] == (
        f"authorize outcome=blocked_redirect reason=not_allowed client={LEGACY_EVIL[:8]} redirect=evil.example ip={OWNER_IP}"
    )
    assert all("redirect=evil.example" in m for m in blocked)
    assert all(record.levelno == logging.WARNING for record in caplog.records if "blocked_redirect" in record.getMessage())


async def test_a_legacy_client_with_one_good_and_one_evil_address_can_use_the_good_one_only(legacy_provider, serve, compare_spy):
    provider = legacy_provider
    mixed = provider.clients[LEGACY_MIXED]
    async with serve(provider) as gate:
        http = gate.at()
        query = {"client_id": LEGACY_MIXED, "response_type": "code", "code_challenge": CHALLENGE}
        good = await http.post("/authorize", data={**query, "redirect_uri": REDIRECT, "decision": "approve", "password": PASSWORD})
        assert good.status_code == 302 and good.headers["location"].startswith(REDIRECT + "?code=")
        evil = await http.post(
            "/authorize", data={**query, "redirect_uri": "https://evil.example/cb", "decision": "approve", "password": PASSWORD}
        )
        assert_the_generic_page(evil)
        unclear = await http.post("/authorize", data={**query, "decision": "approve", "password": PASSWORD})
        assert_the_generic_page(unclear)  # two addresses and none named
    assert len(compare_spy) == 1 and len(mixed.redirect_uris) == 2


async def test_a_legacy_client_with_a_good_address_works_as_before(legacy_provider, serve):
    provider = legacy_provider
    async with serve(provider) as gate:
        http = gate.at()
        query = {"client_id": LEGACY_GOOD, "redirect_uri": REDIRECT, "response_type": "code", "code_challenge": CHALLENGE}
        assert (await http.get("/authorize", params=query)).status_code == 200
        approved = await http.post("/authorize", data={**query, "decision": "approve", "password": PASSWORD, "state": "s"})
    assert approved.status_code == 302 and location_params(approved)["state"] == ["s"]


# --------------------------------------------------------------------------
# Trying passwords: five per address in 15 minutes, thirty an hour overall
# --------------------------------------------------------------------------


async def test_the_sixth_attempt_from_an_address_is_refused_even_with_the_right_password_whichever_client_ids_it_uses(
    state_dir, make_provider, serve, compare_spy, caplog
):
    capture(caplog)
    clock = FakeClock()  # the five wrong passwords and the refusals are one instant, so that retry_in is exact
    provider = make_provider(state_dir, clock=clock)
    clients = [await make_client(provider, n) for n in range(1, 8)]  # seven registered clients: a fresh one every time
    caplog.clear()
    async with serve(provider) as gate:
        http = gate.at()
        for client in clients[:5]:
            reply = await post_consent(http, client, password="guess")
            assert reply.status_code == 200 and "Incorrect password" in reply.text
        sixth = await post_consent(http, clients[5])  # the right password
        seventh = await post_consent(http, clients[6], password="guess")
        other = await post_consent(gate.at(OTHER_IP), clients[0])  # another address is not affected
    assert sixth.status_code == 429 and seventh.status_code == 429
    assert sixth.content == oauth_guard.TOO_MANY_ATTEMPTS_PAGE.encode() and signature(sixth) == signature(seventh)
    for name, value in CONSENT_HEADERS.items():
        assert sixth.headers[name] == value
    assert "location" not in sixth.headers and "Incorrect password" not in sixth.text
    assert other.status_code == 302
    assert len(compare_spy) == 6  # five guesses and the right password of the other address: nothing else was compared
    limited = [m for m in lines(caplog) if m.startswith("authorize outcome=rate_limited")]
    assert limited == [  # scope=ip: this address had its five wrong passwords; retry_in: its first one is 900 seconds from leaving
        f'authorize outcome=rate_limited scope=ip retry_in=900s client={clients[5].client_id[:8]} name="Test client 6" ip={OWNER_IP}',
        f'authorize outcome=rate_limited scope=ip retry_in=900s client={clients[6].client_id[:8]} name="Test client 7" ip={OWNER_IP}',
    ]


async def test_requests_that_are_not_valid_do_not_use_up_the_attempts_however_many_client_ids_are_tried(
    state_dir, make_provider, serve, compare_spy
):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    async with serve(provider) as gate:
        http = gate.at()
        for _ in range(60):
            guess = await post_consent(http, client, password="guess", client_id=str(uuid_like()))
            assert_the_generic_page(guess)
        assert counters(provider) == (0, 0) and compare_spy == []
        approved = await post_consent(http, client)  # the operator is not locked out by any of that
    assert approved.status_code == 302


def uuid_like() -> str:
    return "-".join(secrets.token_hex(n) for n in (4, 2, 2, 2, 6))


async def test_thirty_wrong_passwords_in_an_hour_from_any_addresses_stop_everyone_until_the_oldest_ages_out(
    state_dir, make_provider, serve, compare_spy
):
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)
    client = await make_client(provider)
    async with serve(provider) as gate:
        for host in range(1, 7):  # six addresses, five wrong passwords each: thirty
            for _ in range(5):
                assert (await post_consent(gate.at(f"198.51.100.{host}"), client, password="guess")).status_code == 200
        assert len(compare_spy) == 30
        for address in ("192.0.2.10", "198.51.100.1", "2001:db8::1"):  # a new address, an old one, an IPv6 one
            for password in (PASSWORD, "guess"):
                reply = await post_consent(gate.at(address), client, password=password)
                assert reply.status_code == 429 and reply.content == oauth_guard.TOO_MANY_ATTEMPTS_PAGE.encode(), (address, password)
        assert len(compare_spy) == 30  # no compare while the overall limit holds
        clock.advance(3599)
        assert (await post_consent(gate.at("192.0.2.10"), client)).status_code == 429
        clock.advance(1)
        approved = await post_consent(gate.at("192.0.2.10"), client)
    assert approved.status_code == 302 and len(compare_spy) == 31


async def test_the_journal_says_whether_the_address_or_the_overall_limit_refused_a_sign_in_and_for_how_long_and_the_page_does_not(
    state_dir, make_provider, serve, caplog
):
    # The global cap is by design: thirty wrong passwords in an hour, from anywhere, refuse everyone, the operator's new sign-in
    # included. The operator has to be able to tell that from a guesser at one address, so the line carries the scope and the
    # wait; the page is one fixed page for both, so it gives a guesser nothing.
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)
    client = await make_client(provider)
    name = f'client={client.client_id[:8]} name="Test client 1"'
    async with serve(provider) as gate:
        guesser = gate.at("198.51.100.1")
        for _ in range(5):  # one address uses its five wrong passwords, one a second
            assert (await post_consent(guesser, client, password="guess")).status_code == 200
            clock.advance(1)
        caplog.clear()
        refused_alone = await post_consent(guesser, client)
        assert (await post_consent(gate.at("192.0.2.10"), client)).status_code == 302  # a stranger is not refused: not global
        for host in range(2, 7):  # five more addresses, five wrong passwords each: thirty in all
            for _ in range(5):
                assert (await post_consent(gate.at(f"198.51.100.{host}"), client, password="guess")).status_code == 200
        owner = await post_consent(gate.at(OWNER_IP), client)  # the operator, the right password, an address with no failures
        guesser_again = await post_consent(guesser, client)  # over its own limit too: the overall one is what it waits for
        clock.advance(3595)  # an hour since the first wrong password
        let_in = await post_consent(gate.at(OWNER_IP), client)
    limited = [m for m in lines(caplog) if "outcome=rate_limited" in m]
    assert limited == [
        f'authorize outcome=rate_limited scope=ip retry_in=895s {name} ip=198.51.100.1',
        f'authorize outcome=rate_limited scope=global retry_in=3595s {name} ip={OWNER_IP}',
        f'authorize outcome=rate_limited scope=global retry_in=3595s {name} ip=198.51.100.1',
    ]
    assert signature(refused_alone) == signature(owner) == signature(guesser_again)  # the same page, scope or no scope
    assert refused_alone.status_code == 429 and refused_alone.content == oauth_guard.TOO_MANY_ATTEMPTS_PAGE.encode()
    assert let_in.status_code == 302  # the oldest wrong password is an hour old: the cap lets the operator in again
    assert not [m for m in lines(caplog) if "scope=" in m and "outcome=rate_limited" not in m]


def test_the_429_page_names_both_causes_and_the_wait_and_is_one_fixed_page():
    page = oauth_guard.TOO_MANY_ATTEMPTS_PAGE
    assert "from this address or from all addresses together" in page and "Wait up to an hour" in page
    assert oauth_guard.too_many_attempts_response().body == page.encode()
    assert oauth_guard.too_many_attempts_response().status_code == 429


async def test_twenty_nine_wrong_passwords_leave_room_for_one_more(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    async with serve(provider) as gate:
        for host in range(1, 7):
            for _ in range(5 if host < 6 else 4):
                await post_consent(gate.at(f"198.51.100.{host}"), client, password="guess")
        assert (await post_consent(gate.at("192.0.2.10"), client, password="guess")).status_code == 200  # the thirtieth
        assert (await post_consent(gate.at("192.0.2.11"), client)).status_code == 429


async def test_addresses_in_one_ipv6_slash_64_share_the_attempts_and_another_slash_64_has_its_own(
    state_dir, make_provider, serve, caplog
):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await make_client(provider)
    caplog.clear()
    async with serve(provider) as gate:
        for n in range(1, 6):  # five different machine addresses of one network
            await post_consent(gate.at(f"2001:db8:1:2::{n:x}"), client, password="guess")
        same_network = await post_consent(gate.at("2001:db8:1:2:aaaa:bbbb:cccc:dddd"), client)
        other_network = await post_consent(gate.at("2001:db8:1:3::1"), client)
        ipv4 = await post_consent(gate.at(OWNER_IP), client)
    assert same_network.status_code == 429 and other_network.status_code == 302 and ipv4.status_code == 302
    assert [m for m in lines(caplog) if "rate_limited" in m][0].endswith("ip=2001:db8:1:2:aaaa:bbbb:cccc:dddd")


async def test_an_ipv4_address_and_the_same_address_in_ipv6_notation_are_one_address(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    async with serve(provider) as gate:
        for _ in range(3):
            await post_consent(gate.at("::ffff:203.0.113.7"), client, password="guess")
        for _ in range(2):
            await post_consent(gate.at("203.0.113.7"), client, password="guess")
        assert (await post_consent(gate.at("203.0.113.7"), client)).status_code == 429
        assert (await post_consent(gate.at("::ffff:203.0.113.7"), client)).status_code == 429
        assert (await post_consent(gate.at("203.0.113.8"), client)).status_code == 302


async def test_the_window_of_an_address_is_15_minutes_and_slides(state_dir, make_provider, serve):
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)
    client = await make_client(provider)
    async with serve(provider) as gate:
        http = gate.at()
        for _ in range(5):
            await post_consent(http, client, password="guess")
        clock.advance(899)
        assert (await post_consent(http, client)).status_code == 429  # 899 seconds on: still inside the window
        clock.advance(1)  # all five failures were made at the same moment: 900 seconds later they are out of the window
        for _ in range(5):  # a whole new budget of five
            reply = await post_consent(http, client, password="guess")
            assert reply.status_code == 200 and "Incorrect password" in reply.text
        assert (await post_consent(http, client)).status_code == 429
        clock.advance(900)
        assert (await post_consent(http, client)).status_code == 302


async def test_a_right_password_forgets_the_failures_of_that_address_but_not_the_overall_count(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    async with serve(provider) as gate:
        http = gate.at()
        for _ in range(4):
            await post_consent(http, client, password="guess")
        assert (await post_consent(http, client)).status_code == 302
        assert counters(provider) == (0, 4)
        for _ in range(5):
            reply = await post_consent(http, client, password="guess")
            assert reply.status_code == 200 and "Incorrect password" in reply.text
        assert (await post_consent(http, client)).status_code == 429
    assert counters(provider) == (1, 9)


async def test_the_endpoint_counts_through_the_bounded_table(state_dir, make_provider, serve):
    # thirty wrong passwords an hour is all the overall limit lets in, so the table is filled hour after hour here, from a
    # fresh set of addresses each time; the table is made small so that the bound is reached
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)
    provider._failure_limiter.max_keys = 8
    client = await make_client(provider)
    async with serve(provider) as gate:
        for hour in range(4):
            for n in range(30):
                reply = await post_consent(gate.at(f"10.{hour}.0.{n}"), client, password="guess")
                assert reply.status_code == 200
                assert len(provider._failure_limiter) <= 8
            clock.advance(3601)
    assert len(provider._failure_limiter) == 8


async def test_a_burst_of_simultaneous_wrong_passwords_gets_exactly_five_tries(state_dir, make_provider, serve, compare_spy):
    # the check and the count are one step: a hundred requests that arrive together cannot all be let through
    provider = make_provider(state_dir)
    client = await make_client(provider)
    async with serve(provider) as gate:
        replies = await asyncio.gather(*(post_consent(gate.at(), client, password=f"guess-{n}") for n in range(100)))
        right = await post_consent(gate.at(), client)
    statuses = sorted(reply.status_code for reply in replies)
    assert statuses == [200] * 5 + [429] * 95
    assert len(compare_spy) == 5 and right.status_code == 429


async def test_a_burst_of_simultaneous_registrations_from_one_address_gets_exactly_ten(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        replies = await asyncio.gather(*(register_over_http(gate.at()) for _ in range(40)))
    assert sorted(reply.status_code for reply in replies) == [201] * 10 + [429] * 30
    assert len(provider.clients) == 10


async def test_attempts_that_are_refused_or_denied_or_wrong_never_write_the_state_file(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    before = (state_dir / STATE_FILE).read_bytes()
    mtime = (state_dir / STATE_FILE).stat().st_mtime_ns
    async with serve(provider) as gate:
        http = gate.at()
        await http.get("/authorize", params=params(client))
        await http.get("/authorize", params={"client_id": "nobody"})
        await post_consent(http, client, decision="deny")
        for _ in range(5):
            await post_consent(http, client, password="wrong")
        assert (await post_consent(http, client)).status_code == 429
        await http.post("/authorize", json={})
        await http.post("/authorize", content=b"x" * (CAP + 1), headers={"content-type": FORM})
        await register_over_http(http, redirect_uris=["https://evil.example/cb"])
        await http.post("/register", content=b"not json", headers={"content-type": "application/json"})
    assert (state_dir / STATE_FILE).read_bytes() == before and (state_dir / STATE_FILE).stat().st_mtime_ns == mtime
    assert sorted(os.listdir(state_dir)) == sorted([STATE_FILE, oauth_store.LOCK_FILE_NAME])


async def test_nothing_about_an_address_is_kept_when_it_does_nothing_wrong(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    async with serve(provider) as gate:
        for host in range(1, 40):
            assert (await gate.at(f"198.51.100.{host}").get("/authorize", params=params(client))).status_code == 200
            assert (await post_consent(gate.at(f"198.51.100.{host}"), client)).status_code == 302
    assert counters(provider) == (0, 0)


# --------------------------------------------------------------------------
# Deny and approve
# --------------------------------------------------------------------------


async def test_deny_goes_back_to_the_registered_address_with_access_denied_and_needs_no_password(
    state_dir, make_provider, serve, compare_spy, caplog
):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await make_client(provider, name="Claude")
    caplog.clear()
    async with serve(provider) as gate:
        denied = await post_consent(gate.at(), client, password=None, decision="deny")
        no_state = await post_consent(gate.at(), client, password=None, decision="deny", state=None)
    assert denied.status_code == 302
    target = urlparse(denied.headers["location"])
    assert (target.scheme, target.netloc, target.path) == ("https", "claude.ai", "/api/mcp/auth_callback")
    assert location_params(denied) == {
        "error": ["access_denied"], "error_description": ["User denied authorization"], "state": ["state-1"],
    }
    assert "state" not in location_params(no_state) and location_params(no_state)["error"] == ["access_denied"]
    for name, value in CONSENT_HEADERS.items():
        assert denied.headers[name] == value
    assert compare_spy == [] and counters(provider) == (0, 0) and provider.auth_codes == {}
    assert [m for m in lines(caplog) if m.startswith("authorize")] == [
        f'authorize outcome=denied_by_user client={client.client_id[:8]} name="Claude" ip={OWNER_IP}'
    ] * 2


async def test_approve_with_the_right_password_issues_a_code_to_the_registered_address_only(
    state_dir, make_provider, serve, caplog
):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await make_client(provider, name="Claude")
    caplog.clear()
    async with serve(provider) as gate:
        approved = await post_consent(gate.at(), client)
    assert approved.status_code == 302 and approved.content == b""
    target = urlparse(approved.headers["location"])
    assert (target.scheme, target.netloc, target.path) == ("https", "claude.ai", "/api/mcp/auth_callback")
    query = location_params(approved)
    assert set(query) == {"code", "state"} and query["state"] == ["state-1"]
    for name, value in CONSENT_HEADERS.items():
        assert approved.headers[name] == value
    (code,) = provider.auth_codes.values()
    assert code.code == query["code"][0] and code.client_id == client.client_id and code.code_challenge == CHALLENGE
    assert str(code.redirect_uri) == REDIRECT and code.redirect_uri_provided_explicitly is True and code.scopes == []
    assert [m for m in lines(caplog) if m.startswith("authorize")] == [
        f'authorize outcome=approved client={client.client_id[:8]} name="Claude" ip={OWNER_IP}'
    ]
    assert PASSWORD not in caplog.text and query["code"][0] not in caplog.text


async def test_the_state_goes_back_exactly_as_sent_even_when_it_is_odd_and_cannot_split_the_response(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    odd = "a b&c=d/e?f#g\N{LATIN SMALL LETTER E WITH ACUTE}\N{SNOWMAN}%20+\r\nSet-Cookie: stolen=1\r\n\r\n<script>x</script>"
    async with serve(provider) as gate:
        approved = await post_consent(gate.at(), client, state=odd)
        denied = await post_consent(gate.at(), client, state=odd, decision="deny")
    for reply in (approved, denied):
        assert reply.status_code == 302 and location_params(reply)["state"] == [odd]
        assert "set-cookie" not in reply.headers and reply.content == b""
        assert "\r" not in reply.headers["location"] and "\n" not in reply.headers["location"]
        assert reply.headers["location"].isascii()


async def test_a_non_ascii_password_works_in_either_form_of_the_same_letters(state_dir, make_provider, serve, compare_spy):
    password = "p\N{LATIN SMALL LETTER A WITH DIAERESIS}ssw\N{LATIN SMALL LETTER O WITH DIAERESIS}rd-\N{CJK UNIFIED IDEOGRAPH-4E2D}\N{CJK UNIFIED IDEOGRAPH-6587}-" + "x" * 8  # composed letters
    provider = make_provider(state_dir, password=password)
    client = await make_client(provider)
    async with serve(provider) as gate:
        http = gate.at()
        assert (await post_consent(http, client, password=password)).status_code == 302
        assert (await post_consent(http, client, password=unicodedata.normalize("NFD", password))).status_code == 302
        wrong = await post_consent(http, client, password="p\N{LATIN SMALL LETTER A WITH DIAERESIS}ssw\N{LATIN SMALL LETTER O WITH DIAERESIS}rd")
        assert wrong.status_code == 200 and "Incorrect password" in wrong.text
    nfc = unicodedata.normalize("NFC", password).encode("utf-8")
    assert compare_spy[0] == (nfc, nfc) and compare_spy[1] == (nfc, nfc)


async def test_the_whole_sign_in_through_the_page_works_from_registration_to_a_tool_session(
    state_dir, make_provider, serve, caplog
):
    capture(caplog)
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        http = gate.at()
        registered = (await register_over_http(http)).json()
        query = {
            "client_id": registered["client_id"], "redirect_uri": REDIRECT, "response_type": "code", "code_challenge": CHALLENGE,
            "code_challenge_method": "S256", "state": "xyz", "resource": f"{BASE_URL}/mcp",
        }
        page = await http.get("/authorize", params=query)
        assert page.status_code == 200
        approved = await http.post(
            "/authorize", data={**hidden_inputs(page.text), "password": PASSWORD, "decision": "approve"}
        )
        assert approved.status_code == 302 and location_params(approved)["state"] == ["xyz"]
        token = await http.post(
            "/token",
            data={
                "grant_type": "authorization_code", "code": location_params(approved)["code"][0], "redirect_uri": REDIRECT,
                "client_id": registered["client_id"], "client_secret": registered["client_secret"], "code_verifier": VERIFIER,
            },
        )
        assert token.status_code == 200, token.text
        tokens = token.json()
        assert tokens["access_token"].startswith("pat_") and tokens["refresh_token"].startswith("prt_")
        session = await http.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"}, json=INITIALIZE)
        assert session.status_code == 200
        assert (await http.post("/mcp", headers=MCP_HEADERS, json=INITIALIZE)).status_code == 401
    kinds = [m.split()[0:2] for m in lines(caplog) if not m.startswith("oauth state")]
    assert kinds == [
        ["register", "outcome=registered"], ["authorize", "outcome=consent_shown"], ["authorize", "outcome=approved"],
        ["token", "outcome=issued"],
    ]
    everything = caplog.text
    for secret in (PASSWORD, VERIFIER, tokens["access_token"], tokens["refresh_token"], registered["client_secret"]):
        assert secret not in everything


# --------------------------------------------------------------------------
# Requests that must never end in a 500
# --------------------------------------------------------------------------

FORM = "application/x-www-form-urlencoded"
OK_STATUSES = {200, 302, 400, 413, 415, 429}


def valid_fields(client, **override) -> dict[str, str]:
    return {**params(client, **override), "decision": "approve", "password": PASSWORD}


def encode(fields: dict[str, str]) -> bytes:
    from urllib.parse import urlencode

    return urlencode(fields).encode()


async def test_odd_requests_are_answered_with_a_page_and_never_a_500(state_dir, make_provider, serve, compare_spy):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    good = valid_fields(client)
    boundary = "xyzBOUNDARYxyz"

    def multipart(parts: list[tuple[str, str | None, bytes]], *, end: bool = True) -> tuple[bytes, dict[str, str]]:
        body = b""
        for name, filename, data in parts:
            disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
            body += f"--{boundary}\r\nContent-Disposition: {disposition}\r\n".encode()
            body += (b"Content-Type: text/plain\r\n" if filename else b"") + b"\r\n" + data + b"\r\n"
        if end:
            body += f"--{boundary}--\r\n".encode()
        return body, {"content-type": f"multipart/form-data; boundary={boundary}"}

    text_parts = [(key, None, value.encode()) for key, value in good.items()]
    with_file = lambda field: [(key, "f.txt" if key == field else None, value.encode()) for key, value in good.items()]  # noqa: E731
    cases = {
        # (headers, body, the status it must get)
        "a form that is fine, as multipart text parts": (*reversed(multipart(text_parts)), 302),
        "a file part for the password": (*reversed(multipart(with_file("password"))), 400),
        "a file part for the client_id": (*reversed(multipart(with_file("client_id"))), 400),
        "a file part for a field nobody reads": (*reversed(multipart([*text_parts, ("extra", "e.bin", b"\x00\x01")])), 400),
        "two file parts": (*reversed(multipart([*text_parts, ("a", "a.bin", b"1"), ("b", "b.bin", b"2")])), 400),
        "multipart with no boundary": ({"content-type": "multipart/form-data"}, b"--x\r\n\r\nhello", 400),
        "multipart cut off in the middle": (*reversed(multipart(text_parts, end=False)), None),
        "multipart that is not multipart": ({"content-type": f"multipart/form-data; boundary={boundary}"}, b"hello", None),
        "a form with the charset named": ({"content-type": f"{FORM}; charset=utf-8"}, encode(good), 302),
        "a form with the type in capitals": ({"content-type": FORM.upper()}, encode(good), 302),
        "JSON": ({"content-type": "application/json"}, json.dumps(good).encode(), 415),
        "plain text": ({"content-type": "text/plain"}, encode(good), 415),
        "no content type": ({}, encode(good), 415),
        "multipart/mixed": ({"content-type": "multipart/mixed; boundary=x"}, b"", 415),
        "a type that only starts like a form": ({"content-type": FORM + "-extra"}, encode(good), 415),
        "an empty body": ({"content-type": FORM}, b"", 400),
        "percent signs that mean nothing": ({"content-type": FORM}, b"client_id=%&password=%zz&state=%4", 400),
        "bytes that are not UTF-8 in the client_id": ({"content-type": FORM}, b"client_id=%FF%FE%C3&decision=approve&password=x", 400),
        "bytes that are not UTF-8 in the password": (
            {"content-type": FORM},
            encode({k: v for k, v in good.items() if k != "password"}) + b"&password=%FF%FE%C3%28",
            200,
        ),
        "a NUL in the client_id": ({"content-type": FORM}, encode({**good, "client_id": "a\x00b"}), 400),
        "non-ASCII in every field": (
            {"content-type": FORM},
            encode({**good, "client_id": "\N{LATIN SMALL LETTER E WITH ACUTE}\N{CJK UNIFIED IDEOGRAPH-4E2D}", "redirect_uri": "https://\N{LATIN SMALL LETTER E WITH ACUTE}.example/\N{SNOWMAN}", "state": "\N{SNOWMAN}", "scope": "\N{LATIN SMALL LETTER E WITH ACUTE}"}),
            400,
        ),
        "a very long field name": ({"content-type": FORM}, b"a" * 5000 + b"=1&" + encode(good), 400),
        "a very long value in a field nobody reads": ({"content-type": FORM}, encode({**good, "extra": "z" * 5000}), 400),
        "fifteen hundred small fields": (
            {"content-type": FORM}, "&".join(f"f{n}=1" for n in range(1500)).encode(), 400,
        ),
        "so many small fields that the body is over the cap": (
            {"content-type": FORM}, "&".join(f"f{n}=1" for n in range(3000)).encode(), 413,
        ),
        "a lone ampersand and equals": ({"content-type": FORM}, b"&&&===&=", 400),
    }
    async with serve(provider) as gate:
        http = gate.at()
        results = {}
        for name, (headers, body, expected) in cases.items():
            reply = await http.post("/authorize", content=body, headers=headers)
            results[name] = reply.status_code
            assert reply.status_code in OK_STATUSES, (name, reply.status_code, reply.text[:200])
            assert reply.status_code != 500, name
            if expected is not None:
                assert reply.status_code == expected, (name, reply.status_code)
            for header, value in CONSENT_HEADERS.items():
                assert reply.headers[header] == value, (name, header)
            if reply.status_code == 302:
                provider.auth_codes.clear()  # the next approval starts from nothing
    assert results["multipart cut off in the middle"] in {200, 302, 400}


async def test_odd_queries_are_answered_with_a_page_and_never_a_500(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    queries = [
        "client_id=%FF",
        "%00=1",
        "client_id=" + "%E2%98%83" * 3,
        "client_id=" + client.client_id + "&redirect_uri=%FF%FE",
        "&&&",
        "",
        "client_id",
        "client_id=a&client_id=b&client_id=c",
        "x" * 8000 + "=1",
    ]
    async with serve(provider) as gate:
        for query in queries:
            reply = await gate.at().get("/authorize?" + query)
            assert reply.status_code == 400, query
            assert reply.content == oauth_guard.INVALID_REQUEST_PAGE.encode()
        head = await gate.at().request("HEAD", "/authorize", params=params(client))
        assert head.status_code == 200 and head.content == b""  # HEAD is served like GET, without the body


async def test_a_repeated_parameter_counts_with_its_last_value_in_the_check_and_in_the_redirect(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client, other = await make_client(provider, 1), await make_client(provider, 2)

    def form(**lists):
        base = {key: [value] for key, value in valid_fields(client).items()}
        base.update(lists)
        return base

    async with serve(provider) as gate:
        http = gate.at()
        # the last redirect_uri is the one that counts: here the good one is last, so the code goes there
        last_good = await http.post("/authorize", data=form(redirect_uri=["https://evil.example/cb", REDIRECT]))
        assert last_good.status_code == 302 and last_good.headers["location"].startswith(REDIRECT + "?code=")
        # and here the evil one is last: refused, not redirected (a check on the first value would have let it through)
        assert_the_generic_page(await http.post("/authorize", data=form(redirect_uri=[REDIRECT, "https://evil.example/cb"])))
        assert_the_generic_page(await http.post("/authorize", data=form(client_id=[client.client_id, "unknown-client"])))
        by_last = await http.post("/authorize", data=form(client_id=["unknown-client", other.client_id]))
        assert by_last.status_code == 302
        (code,) = [c for c in provider.auth_codes.values() if c.client_id == other.client_id]
        assert code.client_id == other.client_id
        # the last password, the last decision
        assert (await http.post("/authorize", data=form(password=[PASSWORD, "wrong"]))).status_code == 200
        assert (await http.post("/authorize", data=form(password=["wrong", PASSWORD]))).status_code == 302
        denied = await http.post("/authorize", data=form(decision=["approve", "deny"]))
        assert denied.status_code == 302 and location_params(denied)["error"] == ["access_denied"]


# --------------------------------------------------------------------------
# Security headers on every consent response
# --------------------------------------------------------------------------


async def test_every_kind_of_consent_response_carries_the_security_headers_and_no_form_action(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    responses: dict[str, httpx.Response] = {}
    async with serve(provider) as gate:
        http, limited = gate.at(), gate.at(OTHER_IP)
        responses["page"] = await http.get("/authorize", params=params(client))
        responses["invalid get"] = await http.get("/authorize", params={"client_id": "nobody"})
        responses["invalid post"] = await post_consent(http, client, response_type="token")
        responses["wrong password"] = await post_consent(http, client, password="wrong")
        responses["unsupported media"] = await http.post("/authorize", json={"a": 1})
        responses["too large"] = await http.post("/authorize", content=b"a=" + b"x" * 20_000, headers={"content-type": FORM})
        responses["approved"] = await post_consent(http, client)
        responses["denied"] = await post_consent(http, client, decision="deny")
        for _ in range(5):
            await post_consent(limited, client, password="wrong")
        responses["too many attempts"] = await post_consent(limited, client)
    assert {name: r.status_code for name, r in responses.items()} == {
        "page": 200, "invalid get": 400, "invalid post": 400, "wrong password": 200, "unsupported media": 415,
        "too large": 413, "approved": 302, "denied": 302, "too many attempts": 429,
    }
    for name, reply in responses.items():
        for header, value in CONSENT_HEADERS.items():
            assert reply.headers[header] == value, (name, header)
        assert "form-action" not in reply.headers["content-security-policy"], name
        assert "script-src" not in reply.headers["content-security-policy"]


def test_the_policy_names_what_the_page_needs_and_leaves_form_action_out():
    policy = oauth_guard.CONTENT_SECURITY_POLICY
    assert policy == CONSENT_HEADERS["content-security-policy"]
    directives = {part.split()[0]: part.split()[1:] for part in policy.split("; ")}
    assert directives == {
        "frame-ancestors": ["'none'"], "base-uri": ["'none'"], "default-src": ["'none'"], "style-src": ["'unsafe-inline'"],
    }
    assert "form-action" not in policy  # a browser applies it to the redirect after the post, which would block it
    assert oauth_guard.consent_headers() == {
        "Cache-Control": "no-store", "Pragma": "no-cache", "X-Frame-Options": "DENY", "Content-Security-Policy": policy,
        "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
    }
    assert oauth_guard.consent_headers() is not oauth_guard.consent_headers()  # a fresh copy each time


# --------------------------------------------------------------------------
# The body cap, on its own
# --------------------------------------------------------------------------

CAP = oauth_guard.MAX_BODY_BYTES


async def run_asgi(app, *, method="POST", headers=(), chunks=(b"",), client=(OWNER_IP, 40000), disconnect_after=None, scope_type="http"):
    """Call an ASGI app directly. Returns (status or None, response body, how many times the app called receive, messages sent)."""
    queue = [{"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1} for index, chunk in enumerate(chunks)]
    if disconnect_after is not None:
        queue = queue[:disconnect_after] + [{"type": "http.disconnect"}]
    reads = {"calls": 0}
    sent: list[dict] = []

    async def receive():
        reads["calls"] += 1
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": scope_type, "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
        "path": "/x", "raw_path": b"/x", "query_string": b"", "root_path": "", "server": ("mcp.test", 80), "client": client,
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
    }
    await app(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, reads["calls"], sent


class Recorder:
    """An ASGI app that reads the whole body, notes it, notes what receive() says after it, and answers 200."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        self.afterwards: list[dict] = []
        self.calls = 0

    async def __call__(self, scope, receive, send):
        from starlette.responses import Response

        self.calls += 1
        body, more = b"", True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        self.bodies.append(body)
        self.afterwards.append(await receive())
        await Response("ok")(scope, receive, send)


def capped(inner, **kwargs):
    return oauth_guard.BodyLimit(inner, path="/x", refuse=oauth_guard.json_refusal, **kwargs)


async def test_a_declared_length_over_the_cap_is_refused_without_reading_a_byte(caplog):
    caplog.set_level(logging.INFO, logger="oauth-guard")
    inner = Recorder()
    status, body, reads, _ = await run_asgi(capped(inner), headers=[("content-length", str(CAP + 1))], chunks=(b"x" * (CAP + 1),))
    assert status == 413 and inner.calls == 0 and reads == 0
    assert json.loads(body) == {"error": "invalid_request", "error_description": "The request body is too large."}
    assert lines(caplog, "oauth-guard") == [f"body outcome=too_large path=/x ip={OWNER_IP}"]


async def test_a_body_that_just_fits_is_passed_on_whole_and_what_the_app_reads_after_it_is_the_servers_next_message(caplog):
    inner = Recorder()
    status, _, _, _ = await run_asgi(
        capped(inner), headers=[("content-length", str(CAP))], chunks=(b"a" * 4096, b"b" * 4096, b"c" * 4096, b"d" * 4096)
    )
    assert status == 200 and inner.bodies == [b"a" * 4096 + b"b" * 4096 + b"c" * 4096 + b"d" * 4096]
    assert inner.afterwards == [{"type": "http.disconnect"}]  # the original receive answers once the body is delivered


async def test_a_body_with_no_declared_length_is_counted_as_it_arrives():
    inner = Recorder()
    ok, _, _, _ = await run_asgi(capped(inner), chunks=(b"x" * 8192, b"y" * 8192))  # exactly the cap
    assert ok == 200 and len(inner.bodies[0]) == CAP
    over, _, _, _ = await run_asgi(capped(inner), chunks=(b"x" * 8192, b"y" * 8192, b"z"))
    assert over == 413 and inner.calls == 1  # the second one never reached the app


async def test_a_declared_length_that_lies_does_not_help():
    inner = Recorder()
    status, _, _, _ = await run_asgi(capped(inner), headers=[("content-length", "10")], chunks=(b"x" * 20_000,))
    assert status == 413 and inner.calls == 0


@pytest.mark.parametrize("value", ["abc", "-1", "-5", "1e3", " ", "", "0x10", "12 34", "1_000", "+5", "99999999999999999999"])
async def test_a_content_length_that_is_not_a_plain_number_is_a_400(value):
    inner = Recorder()
    status, body, reads, _ = await run_asgi(capped(inner), headers=[("content-length", value)], chunks=(b"x",))
    assert status == 400 and inner.calls == 0 and reads == 0
    assert json.loads(body)["error"] == "invalid_request"


async def test_two_content_lengths_that_disagree_are_a_400_and_two_that_agree_are_one():
    inner = Recorder()
    status, _, _, _ = await run_asgi(capped(inner), headers=[("content-length", "3"), ("content-length", "4")], chunks=(b"abc",))
    assert status == 400 and inner.calls == 0
    status, _, _, _ = await run_asgi(capped(inner), headers=[("content-length", "3"), ("content-length", "3")], chunks=(b"abc",))
    assert status == 200 and inner.bodies == [b"abc"]


async def test_a_request_with_no_client_is_logged_as_an_unknown_address(caplog):
    caplog.set_level(logging.INFO, logger="oauth-guard")
    status, _, _, _ = await run_asgi(capped(Recorder()), headers=[("content-length", str(CAP + 1))], client=None)
    assert status == 413 and lines(caplog, "oauth-guard") == ["body outcome=too_large path=/x ip=?"]


async def test_a_message_that_is_neither_body_nor_disconnect_is_skipped_while_the_body_is_read():
    inner = Recorder()
    queue = [{"type": "http.something.else"}, {"type": "http.request", "body": b"abc", "more_body": False}]

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "headers": [], "client": (OWNER_IP, 1)}
    await capped(inner)(scope, receive, send)
    assert inner.bodies == [b"abc"]


async def test_a_client_that_leaves_in_the_middle_gets_no_response_and_the_app_is_not_called():
    inner = Recorder()
    status, _, _, sent = await run_asgi(capped(inner), chunks=(b"x" * 100, b"y" * 100, b"z" * 100), disconnect_after=1)
    assert status is None and sent == [] and inner.calls == 0


METHODS = ("POST", "PUT", "PATCH", "DELETE", "GET", "HEAD", "post", "TRACE")


@pytest.mark.parametrize("method", METHODS)
async def test_the_cap_holds_for_every_http_method_and_not_only_for_post(method):
    # The routes answer OPTIONS as well as POST and a handler of the framework reads the body of whatever reaches it, so a
    # cap that looks at the method is a cap that an attacker steps around by choosing another one.
    inner = Recorder()
    status, _, reads, _ = await run_asgi(capped(inner), method=method, headers=[("content-length", str(CAP + 1))])
    assert status == 413 and reads == 0 and inner.calls == 0  # declared: refused without reading a byte
    status, _, _, _ = await run_asgi(capped(inner), method=method, chunks=(b"x" * 9000, b"y" * 9000))
    assert status == 413 and inner.calls == 0  # not declared (chunked) or a lie: counted as it arrives
    status, _, _, _ = await run_asgi(capped(inner), method=method, headers=[("content-length", "10")], chunks=(b"x" * 20_000,))
    assert status == 413 and inner.calls == 0
    status, _, _, _ = await run_asgi(capped(inner), method=method, chunks=(b"x" * 8192, b"y" * 8192))  # exactly the cap
    assert status == 200 and len(inner.bodies[-1]) == CAP and inner.calls == 1
    status, _, _, _ = await run_asgi(capped(inner), method=method)  # no body at all
    assert status == 200 and inner.bodies[-1] == b""


async def test_only_http_is_left_alone():
    seen = []

    async def lifespan_app(scope, receive, send):
        seen.append(scope["type"])

    await run_asgi(capped(lifespan_app), scope_type="lifespan")
    await run_asgi(capped(lifespan_app), scope_type="websocket", method="OPTIONS", headers=[("content-length", str(10 * CAP))])
    assert seen == ["lifespan", "websocket"]


OPTIONS_WITH_A_BODY = {
    "a small declared body": {"headers": [("content-length", "2")], "chunks": (b"{}",)},
    "a declared body over the cap": {"headers": [("content-length", str(CAP + 1))], "chunks": (b"x" * (CAP + 1),)},
    "a huge declared body that is never read": {"headers": [("content-length", str(10_000 * CAP))]},
    "a chunked body": {"headers": [("transfer-encoding", "chunked")], "chunks": (b"x" * 100, b"y" * 100)},
    "a chunked body with nothing in it": {"headers": [("transfer-encoding", "chunked")], "chunks": (b"",)},
    "a transfer encoding and a length": {"headers": [("transfer-encoding", "chunked"), ("content-length", "0")]},
    "a body of a registration": {
        "headers": [("content-type", "application/json"), ("content-length", "49")],
        "chunks": (b'{"redirect_uris": ["https://claude.ai/cb"], "x": 1}',),
    },
}
PREFLIGHT_HEADERS = [("origin", "https://inspector.example"), ("access-control-request-method", "POST")]


@pytest.mark.parametrize("preflight", [False, True], ids=["bare", "with the preflight headers"])
@pytest.mark.parametrize("method", ["OPTIONS", "options"])
@pytest.mark.parametrize("case", OPTIONS_WITH_A_BODY.values(), ids=OPTIONS_WITH_A_BODY.keys())
async def test_an_options_request_that_carries_a_body_is_refused_with_400_before_the_app_reads_anything(caplog, case, method, preflight):
    caplog.set_level(logging.INFO, logger="oauth-guard")
    inner = Recorder()
    extra = PREFLIGHT_HEADERS if preflight else []
    status, body, reads, _ = await run_asgi(capped(inner), method=method, headers=[*case["headers"], *extra], chunks=case.get("chunks", (b"",)))
    assert status == 400 and reads == 0 and inner.calls == 0
    assert json.loads(body) == {"error": "invalid_request", "error_description": "The request is not valid."}
    assert lines(caplog, "oauth-guard") == [f"body outcome=options_body path=/x ip={OWNER_IP}"]


async def test_an_options_request_with_a_body_that_its_headers_do_not_announce_is_refused_when_the_body_shows_up():
    inner = Recorder()
    status, body, reads, _ = await run_asgi(capped(inner), method="OPTIONS", chunks=(b"{}",))  # no content-length, no encoding
    assert status == 400 and inner.calls == 0 and reads == 1
    status, _, _, _ = await run_asgi(capped(inner), method="OPTIONS", chunks=(b"", b"", b"x"))  # it arrives late
    assert status == 400 and inner.calls == 0


@pytest.mark.parametrize("headers", [[], [("content-length", "0")], PREFLIGHT_HEADERS, [("content-length", "0"), *PREFLIGHT_HEADERS]])
async def test_an_options_request_without_a_body_goes_on_exactly_as_before_and_the_app_sees_an_empty_body(headers):
    inner = Recorder()
    status, _, _, _ = await run_asgi(capped(inner), method="OPTIONS", headers=headers)
    assert status == 200 and inner.bodies == [b""] and inner.afterwards == [{"type": "http.disconnect"}]


async def test_an_options_request_with_a_bad_content_length_is_a_bad_length_like_any_other(caplog):
    caplog.set_level(logging.INFO, logger="oauth-guard")
    inner = Recorder()
    for value in ("abc", "-1", "1e3"):
        status, _, reads, _ = await run_asgi(capped(inner), method="OPTIONS", headers=[("content-length", value)])
        assert status == 400 and reads == 0 and inner.calls == 0
    status, _, _, _ = await run_asgi(capped(inner), method="OPTIONS", headers=[("content-length", "1"), ("content-length", "2")])
    assert status == 400 and inner.calls == 0
    assert lines(caplog, "oauth-guard") == [f"body outcome=bad_length path=/x ip={OWNER_IP}"] * 4


async def test_the_refusal_is_whatever_the_wrapper_was_given_and_the_log_line_carries_no_body(caplog):
    caplog.set_level(logging.INFO, logger="oauth-guard")
    inner = Recorder()
    page = oauth_guard.BodyLimit(inner, path="/authorize", refuse=oauth_guard.consent_refusal)
    status, body, _, sent = await run_asgi(page, chunks=(b"password=hunter2-" + b"x" * CAP,), client=("2001:db8::7", 1))
    assert status == 413 and body == oauth_guard.TOO_LARGE_PAGE.encode()
    headers = {k.decode(): v.decode() for k, v in next(m for m in sent if m["type"] == "http.response.start")["headers"]}
    assert headers["content-security-policy"] == CONSENT_HEADERS["content-security-policy"]
    status, body, _, _ = await run_asgi(page, headers=[("content-length", "x")])
    assert status == 400 and body == oauth_guard.INVALID_REQUEST_PAGE.encode()
    assert lines(caplog, "oauth-guard") == [
        "body outcome=too_large path=/authorize ip=2001:db8::7", f"body outcome=bad_length path=/authorize ip={OWNER_IP}",
    ]
    assert "hunter2" not in caplog.text


# --------------------------------------------------------------------------
# The body cap on the real routes
# --------------------------------------------------------------------------


def body_of(path: str, size: int) -> tuple[bytes, str]:
    """A body of exactly `size` bytes that the handler of `path` can read, and its content type."""
    if path == "/register":
        head, tail = b'{"redirect_uris":["https://claude.ai/cb"],"client_name":"', b'"}'
        return head + b"n" * (size - len(head) - len(tail)) + tail, "application/json"
    head = b"client_id=nobody&junk="
    return head + b"x" * (size - len(head)), FORM


async def chunked(total: int, piece: int = 3000):
    sent = 0
    while sent < total:
        count = min(piece, total - sent)
        yield b"x" * count
        sent += count


@pytest.mark.parametrize("path", ["/authorize", "/token", "/revoke", "/register"])
async def test_a_body_over_16_kib_is_refused_with_413_before_the_handler_and_one_at_the_cap_gets_through(
    state_dir, make_provider, serve, monkeypatch, path
):
    provider = make_provider(state_dir)
    asked: list[str] = []
    real = provider.get_client

    async def spy(client_id):
        asked.append(client_id)
        return await real(client_id)

    monkeypatch.setattr(provider, "get_client", spy)
    async with serve(provider) as gate:
        http = gate.at()
        too_big, content_type = body_of(path, CAP + 1)
        refused = await http.post(path, content=too_big, headers={"content-type": content_type})
        assert refused.status_code == 413
        assert asked == [] and provider.clients == {} and len(provider._registration_limiter) == 0  # the handler did not run
        if path == "/authorize":
            assert refused.content == oauth_guard.TOO_LARGE_PAGE.encode()
            for name, value in CONSENT_HEADERS.items():
                assert refused.headers[name] == value
        else:
            assert refused.json() == {"error": "invalid_request", "error_description": "The request body is too large."}
            assert refused.headers["cache-control"] == "no-store"
        at_cap, content_type = body_of(path, CAP)
        let_through = await http.post(path, content=at_cap, headers={"content-type": content_type})
        assert let_through.status_code != 413
        assert {"/authorize": 400, "/token": 401, "/revoke": 401, "/register": 201}[path] == let_through.status_code
        assert asked == ([] if path in ("/authorize", "/register") else ["nobody"])  # the handler of the framework ran


@pytest.mark.parametrize("path", ["/authorize", "/token", "/revoke", "/register"])
async def test_a_chunked_body_is_capped_too(state_dir, make_provider, serve, path):
    provider = make_provider(state_dir)
    seen_headers: list[dict] = []
    async with serve(provider) as gate:

        async def tap(scope, receive, send):
            if scope["type"] == "http" and scope["path"] == path:
                seen_headers.append(dict(scope["headers"]))
            await gate.app(scope, receive, send)

        http = Gate(tap).at()
        content_type = "application/json" if path == "/register" else FORM
        over = await http.post(path, content=chunked(CAP + 1), headers={"content-type": content_type})
        assert over.status_code == 413
        assert b"content-length" not in seen_headers[0] and seen_headers[0][b"transfer-encoding"] == b"chunked"
        within = await http.post(path, content=chunked(CAP), headers={"content-type": content_type})
        assert within.status_code != 413  # whatever the handler makes of 16 KiB of x
    assert provider.clients == {}


async def test_the_mcp_endpoint_is_not_capped(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        http = gate.at()
        big = json.dumps({**INITIALIZE, "padding": "p" * 100_000}).encode()
        reply = await http.post("/mcp", headers=MCP_HEADERS, content=big)
        assert reply.status_code == 401  # the answer of the bearer check, not 413
        assert (await http.post("/mcp", headers=MCP_HEADERS, content=chunked(300_000))).status_code == 401


async def test_the_consent_page_is_capped_for_get_and_head_as_well_and_a_normal_one_is_not_touched(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    async with serve(provider) as gate:
        http = gate.at()
        page = await http.get("/authorize", params=params(client))
        assert page.status_code == 200 and "Authorize MCP Access" in page.text
        assert (await http.head("/authorize", params=params(client))).status_code == 200
        for method in ("GET", "HEAD"):  # a body on a GET is odd, and it is held to the same cap as every other
            refused = await http.request(method, "/authorize", params=params(client), content=b"x" * (CAP + 1))
            assert refused.status_code == 413, method
            fits = await http.request(method, "/authorize", params=params(client), content=b"x" * CAP)
            assert fits.status_code == 200, method
        assert (await http.request("GET", "/authorize", params=params(client), content=chunked(CAP + 1))).status_code == 413


async def test_other_methods_and_other_routes_are_left_alone(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        http = gate.at()
        preflight = await http.options(
            "/token", headers={"Origin": "https://inspector.example", "Access-Control-Request-Method": "POST"}
        )
        assert preflight.status_code == 200 and preflight.headers["access-control-allow-origin"] == "*"
        registration_preflight = await http.options(
            "/register", headers={"Origin": "https://inspector.example", "Access-Control-Request-Method": "POST"}
        )
        assert registration_preflight.status_code == 200 and len(provider._registration_limiter) == 0  # not a registration
        assert (await http.get("/register")).status_code == 405 and len(provider._registration_limiter) == 0
        assert (await http.get("/.well-known/oauth-authorization-server")).status_code == 200
        assert (await http.get("/token")).status_code == 405


# --------------------------------------------------------------------------
# OPTIONS on the real routes: the routes answer it, so it must not be a way around the cap or the limiter
# --------------------------------------------------------------------------


async def counted_chunks(total: int, counter: dict, piece: int = 3000):
    """A chunked body of `total` bytes that counts how many of its chunks were taken from it."""
    sent = 0
    while sent < total:
        counter["taken"] += 1
        count = min(piece, total - sent)
        yield b"x" * count
        sent += count


def handler_spies(provider, monkeypatch) -> list[str]:
    """What the framework's handlers ask of the provider: client authentication (/token and /revoke) and registration."""
    ran: list[str] = []
    real_get_client, real_register = provider.get_client, provider.register_client

    async def get_client(client_id):
        ran.append(f"get_client {client_id}")
        return await real_get_client(client_id)

    async def register_client(client_info):
        ran.append("register_client")
        return await real_register(client_info)

    monkeypatch.setattr(provider, "get_client", get_client)
    monkeypatch.setattr(provider, "register_client", register_client)
    return ran


PREFLIGHTS = {
    "the least a browser sends": {"Origin": "https://inspector.example", "Access-Control-Request-Method": "POST"},
    "the way claude.ai would ask": {
        "Origin": "https://claude.ai", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,mcp-protocol-version",
    },
    "a method that is not allowed": {"Origin": "https://inspector.example", "Access-Control-Request-Method": "DELETE"},
    "a header that is not allowed": {
        "Origin": "https://inspector.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-evil",
    },
}
NOT_PREFLIGHTS = {
    "no CORS headers at all": {},
    "an origin and no requested method": {"Origin": "https://inspector.example"},
    "a requested method and no origin": {"Access-Control-Request-Method": "POST"},
}


@pytest.mark.parametrize("cors", [False, True], ids=["bare", "with the preflight headers"])
@pytest.mark.parametrize("kind", ["declared", "chunked"])
@pytest.mark.parametrize("path", ["/register", "/token", "/revoke"])
async def test_an_options_request_with_a_large_body_is_refused_before_any_handler_and_counts_for_nothing(
    state_dir, make_provider, serve, monkeypatch, path, kind, cors
):
    # OPTIONS with a body was the way around the 16 KiB cap and the registration limiter: the routes serve it and the
    # handlers read the body of whatever reaches them (a 1 MiB registration was stored, an 8 MiB form was read whole).
    provider = make_provider(state_dir)
    ran = handler_spies(provider, monkeypatch)
    headers = {"content-type": "application/json" if path == "/register" else FORM}
    if cors:
        headers.update(PREFLIGHTS["the least a browser sends"])
    taken = {"taken": 0}
    size = 10_000 * CAP if kind == "chunked" else 10 * CAP
    async with serve(provider) as gate:
        http = gate.at()
        if kind == "declared":
            content, _ = body_of(path, size)  # for /register a whole registration, padded: it would be stored if it got in
        else:
            content = counted_chunks(size, taken)
        refused = await http.request("OPTIONS", path, content=content, headers=headers)
        assert refused.status_code == 400 and refused.headers["cache-control"] == "no-store"
        assert refused.json() == {"error": "invalid_request", "error_description": "The request is not valid."}
        again = await http.request("OPTIONS", path, content=b"{}", headers=headers)  # even a tiny one: no body, whatever its size
        assert again.status_code == 400
    assert taken["taken"] == 0  # not one chunk was taken from the sender
    assert ran == [] and provider.clients == {} and len(provider._registration_limiter) == 0  # no handler ran, nothing counted
    assert not (state_dir / STATE_FILE).exists()


async def test_a_registration_cannot_be_made_with_an_options_request_and_the_same_body_as_a_post_registers(
    state_dir, make_provider, serve, monkeypatch, caplog
):
    capture(caplog)
    provider = make_provider(state_dir)
    ran = handler_spies(provider, monkeypatch)
    body = json.dumps({"redirect_uris": [REDIRECT], "client_name": "Claude", "grant_types": ["authorization_code", "refresh_token"]}).encode()
    headers = {"content-type": "application/json"}
    taken = {"taken": 0}

    async def as_chunks():
        taken["taken"] += 1
        yield body

    async with serve(provider) as gate:
        http = gate.at()
        for extra in ({}, PREFLIGHTS["the least a browser sends"]):
            assert (await http.request("OPTIONS", "/register", content=body, headers={**headers, **extra})).status_code == 400
            assert (await http.request("OPTIONS", "/register", content=as_chunks(), headers={**headers, **extra})).status_code == 400
        assert ran == [] and provider.clients == {} and taken["taken"] == 0
        assert (await http.post("/register", content=body, headers=headers)).status_code == 201  # the very same bytes, as a POST
    assert ran == ["register_client"] and len(provider.clients) == 1
    assert [m for m in lines(caplog, "oauth-guard")] == [f"body outcome=options_body path=/register ip={OWNER_IP}"] * 4


async def test_an_options_request_that_is_not_a_cors_preflight_counts_as_a_registration_attempt_and_is_limited_like_one(
    state_dir, make_provider, serve, caplog
):
    # Such a request is handed on by the CORS layer to the registration handler, so it is a request to register (an empty
    # one: the handler would have answered it with a 500). It is counted, and the budget is the one that POST uses.
    capture(caplog)
    provider = make_provider(state_dir)
    shapes = list(NOT_PREFLIGHTS.values())
    async with serve(provider) as gate:
        mine, other = gate.at(), gate.at(OTHER_IP)
        for n in range(10):
            reply = await mine.request("OPTIONS", "/register", headers=shapes[n % 3])
            assert reply.status_code == 400 and reply.json()["error"] == "invalid_client_metadata", n
        assert len(provider._registration_limiter) == 1 and len(provider._registration_limiter._overall) == 10
        caplog.clear()
        refused = await mine.request("OPTIONS", "/register", headers=shapes[0])
        assert refused.status_code == 429 and refused.json() == RATE_LIMITED and len(provider.clients) == 0
        assert lines(caplog) == [f"register outcome=refused reason=rate_limited ip={OWNER_IP}"]
        assert (await register_over_http(mine)).status_code == 429  # a POST from that address: the same budget
        assert (await register_over_http(other)).status_code == 201  # another address has its own
        assert (await other.request("OPTIONS", "/register", headers=shapes[1])).status_code == 400  # and its OPTIONS count in it
        assert len(provider._registration_limiter) == 2
    assert len(provider.clients) == 1


async def test_a_cors_preflight_is_answered_even_to_an_address_that_has_used_up_its_registrations_and_is_never_counted(
    state_dir, make_provider, serve
):
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        http = gate.at()
        for _ in range(10):
            assert (await register_over_http(http)).status_code == 201
        assert (await register_over_http(http)).status_code == 429
        before = len(provider._registration_limiter._overall)
        for headers in PREFLIGHTS.values():
            assert (await http.request("OPTIONS", "/register", headers=headers)).status_code in (200, 400)
        assert (await http.request("OPTIONS", "/register", headers=PREFLIGHTS["the least a browser sends"])).status_code == 200
        assert len(provider._registration_limiter._overall) == before  # a preflight registers nothing
        assert (await register_over_http(http)).status_code == 429


@pytest.mark.parametrize("path", ["/token", "/register", "/revoke"])
@pytest.mark.parametrize("name", PREFLIGHTS)
async def test_a_cors_preflight_is_answered_exactly_as_the_framework_answers_it(state_dir, make_provider, name, path):
    # What claude.ai or a browser based client sends before a POST. The guarded route must answer it byte for byte as the
    # framework's own route does (the headers, the status, the body), and must not count it as a registration.
    provider = make_provider(state_dir)
    guarded = {route.path: route for route in provider.get_routes("/mcp")}[path]
    framework = {route.path: route for route in real_routes(provider)}[path]
    headers = list(PREFLIGHTS[name].items())
    ours = await run_asgi(guarded.endpoint, method="OPTIONS", headers=headers)
    theirs = await run_asgi(framework.endpoint, method="OPTIONS", headers=headers)
    assert ours[0] == theirs[0] and ours[1] == theirs[1] and ours[3] == theirs[3]  # status, body, every message that was sent
    assert ours[0] == (200 if name in ("the least a browser sends", "the way claude.ai would ask") else 400)
    assert len(provider._registration_limiter) == 0 and provider.clients == {}


async def test_a_preflight_is_answered_200_with_what_a_browser_needs_to_go_on_to_post(state_dir, make_provider, serve):
    # The facts a browser reads, over the whole stack, so that a change in the CORS layer is seen here and not first by a user.
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        for path in ("/token", "/register", "/revoke"):
            reply = await gate.at().request("OPTIONS", path, headers=PREFLIGHTS["the way claude.ai would ask"])
            assert reply.status_code == 200 and reply.text == "OK", path
            assert reply.headers["access-control-allow-origin"] == "*"
            assert "POST" in reply.headers["access-control-allow-methods"]
            assert "mcp-protocol-version" in reply.headers["access-control-allow-headers"].lower()
            assert reply.headers["access-control-max-age"].isdigit()


async def test_a_bodyless_options_request_that_is_not_a_preflight_still_reaches_the_token_and_revocation_handlers(
    state_dir, make_provider, serve
):
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        http = gate.at()
        for headers in NOT_PREFLIGHTS.values():
            token = await http.request("OPTIONS", "/token", headers=headers)
            assert token.status_code == 401 and token.json()["error"] == "invalid_client"  # the framework's own answer
            revoke = await http.request("OPTIONS", "/revoke", headers=headers)
            assert revoke.status_code == 401 and revoke.json()["error"] == "unauthorized_client"


@pytest.mark.parametrize("origin", [None, "", "https://inspector.example"], ids=["no origin", "empty origin", "an origin"])
@pytest.mark.parametrize("requested", [None, "", "POST", "DELETE"], ids=["no method", "empty method", "POST", "DELETE"])
@pytest.mark.parametrize("method", ["OPTIONS", "POST", "GET", "options"])
async def test_the_gate_and_the_cors_layer_agree_on_which_requests_are_preflights(method, origin, requested):
    # The registration gate does not count a preflight because the CORS layer behind it answers one itself. If the two ever
    # disagreed, an OPTIONS that reaches the handler would be a free registration attempt.
    from starlette.middleware.cors import CORSMiddleware
    from starlette.responses import Response

    reached: list[bool] = []

    async def handler(scope, receive, send):
        reached.append(True)
        await Response("handler")(scope, receive, send)

    cors = CORSMiddleware(handler, allow_origins="*", allow_methods=["POST", "OPTIONS"], allow_headers=["mcp-protocol-version"])
    headers = []
    if origin is not None:
        headers.append(("origin", origin))
    if requested is not None:
        headers.append(("access-control-request-method", requested))
    await run_asgi(cors, method=method, headers=headers)
    scope = {"type": "http", "method": method, "headers": [(name.encode(), value.encode()) for name, value in headers]}
    assert oauth_guard.is_cors_preflight(scope) is (not reached), (method, origin, requested)


def test_is_cors_preflight_is_only_about_http_requests_and_never_raises():
    assert oauth_guard.is_cors_preflight({"type": "http", "method": "OPTIONS"}) is False  # no headers in the scope at all
    assert oauth_guard.is_cors_preflight({"type": "http", "method": "OPTIONS", "headers": None}) is False
    both = [(b"origin", b"https://x.example"), (b"access-control-request-method", b"POST")]
    assert oauth_guard.is_cors_preflight({"type": "http", "method": "OPTIONS", "headers": both}) is True
    assert oauth_guard.is_cors_preflight({"type": "websocket", "method": "OPTIONS", "headers": both}) is False
    assert oauth_guard.is_cors_preflight({"type": "http", "headers": both}) is False


# --------------------------------------------------------------------------
# The revocation mark: /revoke only looks a token up
# --------------------------------------------------------------------------


async def test_the_revocation_mark_is_on_for_the_length_of_a_request_and_never_longer():
    from starlette.responses import Response

    seen: list = []

    async def app(scope, receive, send):
        seen.append(oauth_guard.revoking())
        await Response("ok")(scope, receive, send)

    assert oauth_guard.revoking() is False
    status, _, _, _ = await run_asgi(oauth_guard.RevocationMark(app))
    assert status == 200 and seen == [True] and oauth_guard.revoking() is False

    async def failing(scope, receive, send):
        seen.append(oauth_guard.revoking())
        raise RuntimeError("the handler failed")

    with pytest.raises(RuntimeError):
        await run_asgi(oauth_guard.RevocationMark(failing))
    assert seen == [True, True] and oauth_guard.revoking() is False  # undone whatever happens inside

    async def lifespan(scope, receive, send):
        seen.append((scope["type"], oauth_guard.revoking()))

    await run_asgi(oauth_guard.RevocationMark(lifespan), scope_type="lifespan")
    assert seen[-1] == ("lifespan", False)  # only an HTTP request is a revocation


async def test_the_mark_is_seen_only_by_the_request_it_was_set_for():
    inside, release = asyncio.Event(), asyncio.Event()
    seen: dict[str, bool] = {}

    async def revoking_request(scope, receive, send):
        seen["revoke"] = oauth_guard.revoking()
        inside.set()
        await release.wait()  # another request runs while this one is still inside
        seen["revoke, later"] = oauth_guard.revoking()

    async def other_request():
        await inside.wait()
        seen["other"] = oauth_guard.revoking()
        release.set()

    await asyncio.gather(run_asgi(oauth_guard.RevocationMark(revoking_request)), other_request())
    assert seen == {"revoke": True, "revoke, later": True, "other": False}
    assert oauth_guard.revoking() is False


# --------------------------------------------------------------------------
# Registration is open, so it is limited: per address, overall, and in the number of clients kept
# --------------------------------------------------------------------------

RATE_LIMITED = {"error": "temporarily_unavailable", "error_description": "Too many registration requests. Try again later."}


async def test_ten_registrations_an_hour_from_one_address_then_429_while_another_address_is_not_affected(
    state_dir, make_provider, serve, caplog
):
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)
    async with serve(provider) as gate:
        mine, other = gate.at(), gate.at(OTHER_IP)
        start = clock.now
        for _ in range(10):  # one a second
            assert (await register_over_http(mine)).status_code == 201
            clock.advance(1)
        caplog.clear()
        refused = await register_over_http(mine)
        assert refused.status_code == 429 and refused.json() == RATE_LIMITED
        assert refused.headers["cache-control"] == "no-store" and len(provider.clients) == 10  # the handler did not run
        assert lines(caplog) == [f"register outcome=refused reason=rate_limited ip={OWNER_IP}"]
        assert (await register_over_http(other)).status_code == 201
        clock.now = start + 3599
        assert (await register_over_http(mine)).status_code == 429
        clock.now = start + 3600  # the first of the ten is an hour old, the other nine are not
        assert (await register_over_http(mine)).status_code == 201
        assert (await register_over_http(mine)).status_code == 429


async def test_fifty_registrations_a_day_from_all_addresses_together_then_429_for_everyone(state_dir, make_provider, serve, caplog):
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)
    async with serve(provider) as gate:
        for host in range(1, 6):  # five addresses, ten each
            for _ in range(10):
                assert (await register_over_http(gate.at(f"198.51.100.{host}"))).status_code == 201
        assert len(provider.clients) == 50
        for address in ("192.0.2.1", "198.51.100.1", "2001:db8::5"):
            reply = await register_over_http(gate.at(address))
            assert reply.status_code == 429 and reply.json() == RATE_LIMITED, address
        clock.advance(DAY - 1)
        assert (await register_over_http(gate.at("192.0.2.1"))).status_code == 429
        clock.advance(121)  # more than a day since the first registrations: the limiter lets one through
        caplog.clear()
        reply = await register_over_http(gate.at("192.0.2.1"))
        assert reply.status_code == 201  # and the oldest idle client makes room for it
        assert len(provider.clients) == 50
        assert lines(caplog)[0].endswith(f"ip=192.0.2.1 evicted=1")


async def test_every_request_to_register_counts_whatever_it_asked_for_except_one_refused_for_its_size(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        http = gate.at()
        for _ in range(4):
            assert (await register_over_http(http, redirect_uris=["https://evil.example/cb"])).status_code == 400
        for _ in range(3):
            assert (await http.post("/register", content=b"not json", headers={"content-type": "application/json"})).status_code == 400
        for _ in range(20):  # too large: refused before it is a registration at all
            assert (await http.post("/register", content=b"x" * (CAP + 1), headers={"content-type": "application/json"})).status_code == 413
        for _ in range(3):
            assert (await register_over_http(http)).status_code == 201
        assert (await register_over_http(http)).status_code == 429  # four bad, three garbage and three good: ten
    assert len(provider.clients) == 3


async def test_addresses_of_one_ipv6_slash_64_share_the_registration_budget(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        for n in range(1, 11):
            assert (await register_over_http(gate.at(f"2001:db8:1:2::{n:x}"))).status_code == 201
        assert (await register_over_http(gate.at("2001:db8:1:2:ffff::1"))).status_code == 429
        assert (await register_over_http(gate.at("2001:db8:1:3::1"))).status_code == 201


BODIES_THAT_ARE_NOT_JSON = {
    "text": b"hello",
    "a cut-off object": b'{"redirect_uris": ["https://claude.ai/cb"',
    "an empty body": b"",
    "bytes that are not UTF-8": b'{"client_name": "\xff\xfe\xfd"}',
}


@pytest.mark.parametrize("body", BODIES_THAT_ARE_NOT_JSON.values(), ids=BODIES_THAT_ARE_NOT_JSON.keys())
async def test_a_registration_body_that_is_not_json_is_a_400_and_not_the_frameworks_unhandled_exception(
    state_dir, make_provider, serve, caplog, body
):
    capture(caplog)
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        reply = await gate.at().post("/register", content=body, headers={"content-type": "application/json"})
    assert reply.status_code == 400 and reply.headers["cache-control"] == "no-store"
    assert reply.json() == {"error": "invalid_client_metadata", "error_description": "The request body is not valid JSON."}
    assert provider.clients == {} and lines(caplog)[-1] == f"register outcome=refused reason=not_json ip={OWNER_IP}"


@pytest.mark.parametrize(
    "body",
    [b"[]", b"5", b'"text"', b"null", b"{}", b'{"redirect_uris": "https://claude.ai/cb"}', b"[" * 8000 + b"]" * 8000],
    ids=["array", "number", "string", "null", "empty object", "redirect_uris not a list", "arrays nested 8000 deep"],
)
async def test_json_that_is_not_client_metadata_is_the_frameworks_400(state_dir, make_provider, serve, body):
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        reply = await gate.at().post("/register", content=body, headers={"content-type": "application/json"})
    assert reply.status_code == 400 and reply.json()["error"] == "invalid_client_metadata" and provider.clients == {}


@pytest.mark.parametrize("error", [json.JSONDecodeError("x", "", 0), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "x"), RecursionError()])
async def test_the_registration_gate_answers_a_parse_error_of_the_handler_that_comes_before_any_response_with_400_and_never_hides_a_later_one(
    error, caplog
):
    # The gate parses the body before the handler does, so the handler's own parser cannot fail on a body that got in; this
    # is the second line, for a parser that does not behave the same twice.
    caplog.set_level(logging.INFO, logger="personal-auth")
    refused: list[tuple[str, str]] = []

    async def before_any_response(scope, receive, send):
        raise error

    async def after_the_response_started(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise error

    def gate(inner):
        return oauth_guard.RegistrationGate(
            inner, limiter=oauth_guard.RegistrationLimiter(), clock=time.time, on_refused=lambda host, why: refused.append((host, why))
        )

    status, body, _, _ = await run_asgi(gate(before_any_response), chunks=(b"{}",))  # a body that parses, so the handler runs
    assert status == 400 and json.loads(body)["error"] == "invalid_client_metadata" and refused == [(OWNER_IP, "not_json")]
    with pytest.raises(type(error)):
        await run_asgi(gate(after_the_response_started), chunks=(b"{}",))
    assert refused == [(OWNER_IP, "not_json")]  # no second answer was invented


@pytest.mark.parametrize("error", [ValueError("a bug of ours, not a bad body"), KeyError("x"), OSError(5, "disk"), RuntimeError("x"), TypeError("x")])
async def test_the_registration_gate_does_not_hide_the_errors_of_the_handler_that_are_not_the_bodys(error):
    # Only what the body causes is answered with a 400, and the body is judged before the handler runs. A ValueError that
    # comes out of the handler itself is a fault of ours and has to stay one (a 500 and a traceback), not become a
    # "not valid JSON" answer that sends the operator looking in the wrong place.
    async def broken(scope, receive, send):
        raise error

    gate = oauth_guard.RegistrationGate(
        broken, limiter=oauth_guard.RegistrationLimiter(), clock=time.time, on_refused=lambda host, why: pytest.fail(why)
    )
    with pytest.raises(type(error)):
        await run_asgi(gate, chunks=(b'{"redirect_uris": ["https://claude.ai/cb"]}',))


# --------------------------------------------------------------------------
# A registration that cannot be parsed or kept is a clean 400 and stores nothing
# --------------------------------------------------------------------------


def a_gate(inner, *, limiter=None, **kwargs):
    """(a RegistrationGate over `inner`, the list of what it reported through on_refused)."""
    refused: list[tuple[str, str]] = []
    gate = oauth_guard.RegistrationGate(
        inner,
        limiter=oauth_guard.RegistrationLimiter() if limiter is None else limiter,  # not `or`: an empty limiter is falsy
        clock=time.time,
        on_refused=lambda host, why: refused.append((host, why)),
        **kwargs,
    )
    return gate, refused


def registration_json(**raw_members: str) -> bytes:
    """A registration body for the claude.ai address. The members are JSON text, written as they are, so that a test can send
    what json.dumps would never write (an escape of a lone surrogate, a number of 4301 digits)."""
    members = [f'"redirect_uris": ["{REDIRECT}"]'] + [f'"{name}": {value}' for name, value in raw_members.items()]
    return ("{" + ", ".join(members) + "}").encode()


def nested(levels: int) -> str:
    return "[" * levels + "]" * levels


DIGITS_ONE_TOO_MANY = "1" * 4301  # Python reads at most 4300 digits of a whole number
NOT_JSON_SAYS = "The request body is not valid JSON."
BODIES_THAT_CANNOT_BE_KEPT = {
    # the parser refuses them: a ValueError that is not a JSONDecodeError
    "a number of 4301 digits where text is expected": (registration_json(software_version=DIGITS_ONE_TOO_MANY), "not_json"),
    "a number of 4301 digits in jwks": (registration_json(jwks='{"n": ' + DIGITS_ONE_TOO_MANY + "}"), "not_json"),
    "a number of 4301 digits in a list": (registration_json(contacts="[" + DIGITS_ONE_TOO_MANY + "]"), "not_json"),
    "a number of 4301 digits on its own": (DIGITS_ONE_TOO_MANY.encode(), "not_json"),
    "a number of 16000 digits": (registration_json(software_version="2" * 16_000), "not_json"),
    # a lone surrogate in a text that the model takes as it is (it used to be stored first and a 500 followed when the answer was written)
    "a lone surrogate in client_name": (registration_json(client_name='"a\\ud800b"'), "metadata_surrogate"),
    "a lone surrogate in scope": (registration_json(scope='"a\\ud800"'), "metadata_surrogate"),
    "a lone surrogate in software_id": (registration_json(software_id='"\\ud800"'), "metadata_surrogate"),
    "a lone surrogate in software_version": (registration_json(software_version='"1.\\udfff"'), "metadata_surrogate"),
    "a lone surrogate in a contact": (registration_json(contacts='["a@example.test", "\\ud800"]'), "metadata_surrogate"),
    "a lone surrogate in a grant type": (
        registration_json(grant_types='["authorization_code", "refresh_token", "\\ud800"]'), "metadata_surrogate",
    ),
    "a lone surrogate in a response type": (registration_json(response_types='["code", "\\udc00"]'), "metadata_surrogate"),
    "a lone surrogate in a value of jwks": (registration_json(jwks='{"keys": [{"kty": "\\ud800"}]}'), "metadata_surrogate"),
    "a lone surrogate in a key of jwks": (registration_json(jwks='{"\\ud800": 1}'), "metadata_surrogate"),
    "the two halves of a pair in the wrong order": (registration_json(client_name='"\\ude00\\ud83d"'), "metadata_surrogate"),
    "the bytes of a surrogate, which the parser decodes": (
        b'{"redirect_uris": ["' + REDIRECT.encode() + b'"], "client_name": "a\xed\xa0\x80b"}', "metadata_surrogate",
    ),
    # nested beyond what any writer is asked to handle (the body is then 17 levels deep: the object, then sixteen lists)
    "jwks nested too deeply": (registration_json(jwks=nested(16)), "metadata_too_deep"),
    "objects nested too deeply": (registration_json(jwks='{"a":' * 16 + "1" + "}" * 16), "metadata_too_deep"),
    "arrays nested thousands deep": (registration_json(jwks=nested(3000)), "metadata_too_deep"),
}
BODIES_THAT_ARE_KEPT = {
    "text outside ASCII": registration_json(client_name='"Cl\\u00e4ude \\u4e2d\\u6587"'),
    "a character outside the first plane as a pair of escapes": registration_json(client_name='"\\ud83d\\ude00 Claude"'),
    "the same character as its UTF-8 bytes": b'{"redirect_uris": ["' + REDIRECT.encode() + b'"], "client_name": "\xf0\x9f\x98\x80"}',
    "NaN, Infinity and a float too big to hold in jwks": registration_json(jwks='{"a": NaN, "b": -Infinity, "c": 1e999}'),
    "a real key set": registration_json(jwks='{"keys": [{"kty": "RSA", "n": "abc", "e": "AQAB", "x5c": ["MIIB"]}]}'),
    "nesting as deep as it may be": registration_json(jwks=nested(15)),  # the object and fifteen lists: sixteen levels
    "a number of 4300 digits, the most Python reads": registration_json(jwks='{"n": ' + "1" * 4300 + "}"),
}


@pytest.mark.parametrize("case", BODIES_THAT_CANNOT_BE_KEPT.values(), ids=BODIES_THAT_CANNOT_BE_KEPT.keys())
async def test_a_registration_that_cannot_be_parsed_or_kept_is_a_clean_400_and_nothing_is_stored(
    state_dir, make_provider, serve, caplog, case
):
    body, reason = case
    capture(caplog)
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        # an exception in the app would come out of this call (the transport re-raises it): a 500 and a traceback
        reply = await gate.at().post("/register", content=body, headers={"content-type": "application/json"})
        assert reply.status_code == 400 and reply.headers["cache-control"] == "no-store"
        expected = NOT_JSON_SAYS if reason == "not_json" else oauth_guard.METADATA_REFUSED_DESCRIPTION
        assert reply.json() == {"error": "invalid_client_metadata", "error_description": expected}
        assert provider.clients == {} and not (state_dir / STATE_FILE).exists()  # nothing stored, in memory or on disk
        assert len(provider._registration_limiter._overall) == 1  # it counted as a registration
        # and the same address can register as soon as it sends something that can be kept
        assert (await register_over_http(gate.at())).status_code == 201
    assert [m for m in lines(caplog) if m.startswith("register")][0] == f"register outcome=refused reason={reason} ip={OWNER_IP}"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR or r.exc_info]  # no traceback, no error line
    assert len(provider.clients) == 1


@pytest.mark.parametrize("body", BODIES_THAT_ARE_KEPT.values(), ids=BODIES_THAT_ARE_KEPT.keys())
async def test_what_the_checks_must_not_refuse_is_still_registered_and_the_file_loads_again(state_dir, make_provider, serve, body):
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        reply = await gate.at().post("/register", content=body, headers={"content-type": "application/json"})
    assert reply.status_code == 201, reply.text
    assert reply.json()["client_id"] in provider.clients
    reloaded = oauth_store.load_state(state_dir)  # the file that was written is one that loads
    assert set(reloaded.state.clients) == set(provider.clients) and sum(reloaded.skipped.values()) == 0  # none skipped


async def test_the_depth_limit_is_sixteen_levels_exactly(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        http = gate.at()
        allowed = await http.post("/register", content=registration_json(jwks=nested(15)), headers={"content-type": "application/json"})
        refused = await http.post("/register", content=registration_json(jwks=nested(16)), headers={"content-type": "application/json"})
    assert (allowed.status_code, refused.status_code) == (201, 400) and len(provider.clients) == 1


def test_json_text_problem_finds_a_lone_surrogate_and_too_deep_a_nesting_wherever_they_are():
    problem = oauth_guard.json_text_problem
    assert problem({"a": [1, 2.5, True, None, {"b": "text \N{LATIN SMALL LETTER A WITH DIAERESIS}\N{CJK UNIFIED IDEOGRAPH-4E2D}\U0001f600"}]}) is None
    assert problem("\ud800") == "surrogate" and problem(["x", ["\udfff"]]) == "surrogate" and problem({"\ud800": 1}) == "surrogate"
    assert problem({"a": {"b": [{"c": "ok"}, {"d": ["\ud800"]}]}}) == "surrogate"
    assert problem(chr(0xD83D) + chr(0xDE00)) == "surrogate"  # a pair that the parser did not combine is two lone halves
    assert problem(7) is None and problem(None) is None and problem([]) is None and problem({}) is None
    depth_16 = "x"
    for _ in range(16):
        depth_16 = [depth_16]
    assert problem(depth_16) is None  # sixteen containers, the string inside them is not a level
    assert problem([depth_16]) == "too_deep" and problem({"a": depth_16}) == "too_deep"
    deep = "x"
    for _ in range(50_000):  # far past the interpreter's own limit: the walk has no recursion to run out of
        deep = [deep]
    assert problem(deep) == "too_deep"
    wide = [["x"] for _ in range(50_000)]
    assert problem(wide) is None


async def test_a_registration_client_with_a_lone_surrogate_never_reaches_the_table_even_when_the_provider_is_called_directly(
    state_dir, make_provider, caplog
):
    # The gate refuses these bodies before the handler. This is the second line: whatever reaches register_client is judged
    # as the thing that is stored and answered, and refused before the table or the file is touched.
    capture(caplog)
    provider = make_provider(state_dir)
    keep = await make_client(provider, 1)
    file_before, table_before = (state_dir / STATE_FILE).read_bytes(), dict(provider.clients)
    deep = "x"
    for _ in range(400):  # no serializer takes this
        deep = [deep]
    shallow_but_too_deep = "x"
    for _ in range(17):
        shallow_but_too_deep = [shallow_but_too_deep]
    bad = {
        "client_name": ("client_name", "a\ud800b", "surrogate"),
        "scope": ("scope", "a\ud800", "surrogate"),
        "software_id": ("software_id", "\udfff", "surrogate"),
        "contacts": ("contacts", ["\ud800"], "surrogate"),
        "jwks": ("jwks", {"keys": [{"kty": "\ud800"}]}, "surrogate"),
        "a jwks key": ("jwks", {"\ud800": 1}, "unserializable"),  # pydantic cannot even dump a key that is not text
        "a jwks that is too deep": ("jwks", shallow_but_too_deep, "too_deep"),
        "a jwks that no serializer takes": ("jwks", deep, "unserializable"),
    }
    for n, (name, (field, value, reason)) in enumerate(bad.items(), start=2):
        caplog.clear()
        client = OAuthClientInformationFull(
            client_id=f"{n:08d}-aaaa-4bbb-8ccc-{n:012d}", client_secret="s", client_id_issued_at=int(provider._now()),
            redirect_uris=[AnyUrl(REDIRECT)], **{field: value},
        )
        with pytest.raises(RegistrationError) as info:
            await provider.register_client(client)
        assert info.value.error == "invalid_client_metadata" and info.value.error_description == oauth_guard.METADATA_REFUSED_DESCRIPTION, name
        assert lines(caplog) == [f"register outcome=refused reason=metadata_{reason} ip=-"], name
    assert provider.clients == table_before and set(provider.clients) == {keep.client_id}
    assert (state_dir / STATE_FILE).read_bytes() == file_before  # nothing was saved either


async def test_the_second_line_alone_gives_the_frameworks_handler_a_clean_400_and_stores_nothing(state_dir, make_provider):
    # The framework's own registration route, with no gate in front of it: the provider's check is what answers.
    provider = make_provider(state_dir)
    app = Starlette(routes=[route for route in real_routes(provider) if route.path == "/register"])
    transport = httpx.ASGITransport(app=app, client=(OWNER_IP, 40000))
    deep = '{"a":' * 16 + "1" + "}" * 16
    bodies = [
        registration_json(client_name='"a\\ud800b"'), registration_json(scope='"\\udfff"'),
        registration_json(jwks='{"keys": [{"kty": "\\ud800"}]}'), registration_json(jwks=deep),
    ]
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as http:
        for body in bodies:
            reply = await http.post("/register", content=body, headers={"content-type": "application/json"})
            assert reply.status_code == 400, body
            assert reply.json() == {"error": "invalid_client_metadata", "error_description": oauth_guard.METADATA_REFUSED_DESCRIPTION}
        ok = await http.post("/register", content=registration_json(client_name='"Claude"'), headers={"content-type": "application/json"})
        assert ok.status_code == 201
    assert len(provider.clients) == 1  # only the one that could be kept


NAME_OUTSIDE_ASCII ="Cl\N{LATIN SMALL LETTER A WITH DIAERESIS}ude \N{CJK UNIFIED IDEOGRAPH-4E2D}\N{CJK UNIFIED IDEOGRAPH-6587} \U0001f600"


async def test_register_client_takes_a_client_with_text_outside_ascii_and_every_pair_that_is_whole(state_dir, make_provider):
    provider = make_provider(state_dir)
    client = await make_client(provider, 1, name=NAME_OUTSIDE_ASCII, scope="a b")
    assert provider.clients[client.client_id].client_name == NAME_OUTSIDE_ASCII
    assert oauth_store.load_state(state_dir).state.clients[client.client_id].client_name == client.client_name


def test_client_metadata_problem_looks_at_what_is_stored_and_answered_and_never_raises():
    good = OAuthClientInformationFull(client_id="c", client_secret="s", redirect_uris=[AnyUrl(REDIRECT)], client_name="Claude")
    assert oauth_guard.client_metadata_problem(good) is None

    class Broken:
        def model_dump(self, **kwargs):
            raise RuntimeError("a serializer that fails")

    assert oauth_guard.client_metadata_problem(Broken()) == "unserializable"

    class Fails:  # a dump that is fine and an answer that cannot be written
        def model_dump(self, **kwargs):
            return {"a": 1}

        def model_dump_json(self, **kwargs):
            raise ValueError("cannot be written")

    assert oauth_guard.client_metadata_problem(Fails()) == "unserializable"

    class NotAJsonNumber:
        def model_dump(self, **kwargs):
            return {"a": float("nan")}

        def model_dump_json(self, **kwargs):
            return "{}"

    assert oauth_guard.client_metadata_problem(NotAJsonNumber()) == "unserializable"  # the state file never holds NaN


async def test_the_gate_reads_the_body_once_parses_it_before_the_handler_and_hands_the_handler_the_same_bytes():
    inner = Recorder()
    gate, refused = a_gate(inner)
    pieces = (b'{"redirect_uris": ', b'["https://claude.ai/cb"], ', b'"client_name": "n"}')
    status, _, _, _ = await run_asgi(gate, chunks=pieces)
    assert status == 200 and inner.bodies == [b"".join(pieces)] and refused == []
    assert inner.afterwards == [{"type": "http.disconnect"}]  # after the body: what the server says next


@pytest.mark.parametrize(
    "body",
    [b"", b"hello", b"{", b'{"a": 1,}', b'{"a": ' + b"1" * 4301 + b"}", b"1" * 4301, b"\xff\xfe\xfd", b"\x00\x00", b"[" * 5 + b"]" * 4],
    ids=["nothing", "text", "cut", "trailing comma", "4301 digits", "4301 digits alone", "not UTF-8", "NULs", "unbalanced"],
)
async def test_a_body_that_does_not_parse_never_reaches_the_handler(body):
    inner = Recorder()
    gate, refused = a_gate(inner)
    status, answer, _, _ = await run_asgi(gate, chunks=(body,))
    assert status == 400 and inner.calls == 0 and refused == [(OWNER_IP, "not_json")]
    assert json.loads(answer) == {"error": "invalid_client_metadata", "error_description": NOT_JSON_SAYS}


@pytest.mark.parametrize("name", [name for name, (_, reason) in BODIES_THAT_CANNOT_BE_KEPT.items() if reason.startswith("metadata")])
async def test_metadata_that_cannot_be_kept_never_reaches_the_handler(name):
    body, reason = BODIES_THAT_CANNOT_BE_KEPT[name]
    inner = Recorder()
    gate, refused = a_gate(inner)
    status, answer, _, _ = await run_asgi(gate, chunks=(body,))
    assert status == 400 and inner.calls == 0 and refused == [(OWNER_IP, reason)]
    assert json.loads(answer) == {"error": "invalid_client_metadata", "error_description": oauth_guard.METADATA_REFUSED_DESCRIPTION}


async def test_a_request_that_is_not_a_post_and_still_carries_a_body_is_refused_by_the_gate_itself_and_only_a_post_registers():
    body = b'{"redirect_uris": ["https://claude.ai/cb"]}'
    inner = Recorder()
    gate, refused = a_gate(inner)
    for method in ("OPTIONS", "PUT", "GET"):  # BodyLimit stands in front of the gate and refuses an OPTIONS body; the gate does not rely on it
        status, answer, _, _ = await run_asgi(gate, method=method, chunks=(body,))
        assert status == 400 and json.loads(answer) == {"error": "invalid_request", "error_description": "The request is not valid."}
    assert inner.calls == 0 and refused == [(OWNER_IP, "not_post_body")] * 3
    status, _, _, _ = await run_asgi(gate, method="POST", chunks=(body,))
    assert status == 200 and inner.bodies == [body]
    status, answer, _, _ = await run_asgi(gate, method="OPTIONS")  # no body: an attempt that is counted and answered like an empty POST
    assert status == 400 and json.loads(answer)["error_description"] == NOT_JSON_SAYS and refused[-1] == (OWNER_IP, "not_json")


async def test_a_cors_preflight_goes_straight_through_the_gate_and_is_not_counted():
    inner = Recorder()
    limiter = oauth_guard.RegistrationLimiter()
    gate, refused = a_gate(inner, limiter=limiter)
    status, _, _, _ = await run_asgi(gate, method="OPTIONS", headers=PREFLIGHT_HEADERS)
    assert status == 200 and inner.calls == 1 and len(limiter) == 0 and refused == []
    status, _, _, _ = await run_asgi(gate, method="OPTIONS", headers=[("origin", "https://x.example")])  # not a preflight
    assert status == 400 and inner.calls == 1 and len(limiter) == 1 and refused == [(OWNER_IP, "not_json")]


async def test_a_body_over_the_limit_that_reaches_the_gate_without_the_cap_in_front_is_a_413_and_no_handler_runs():
    inner = Recorder()
    gate, refused = a_gate(inner, max_bytes=100)
    status, answer, _, _ = await run_asgi(gate, chunks=(b"x" * 60, b"y" * 41))
    assert status == 413 and inner.calls == 0 and refused == [(OWNER_IP, "too_large")]
    assert json.loads(answer) == {"error": "invalid_request", "error_description": "The request body is too large."}
    status, _, _, _ = await run_asgi(gate, chunks=(b'{"a": "' + b"x" * 91 + b'"}',))  # 100 bytes: fits, and is then a body like any other
    assert status == 200 and inner.calls == 1


async def test_a_client_that_leaves_while_the_gate_reads_gets_no_answer_and_no_handler_runs():
    inner = Recorder()
    gate, refused = a_gate(inner)
    status, _, _, sent = await run_asgi(gate, chunks=(b'{"a":', b" 1}"), disconnect_after=1)
    assert status is None and sent == [] and inner.calls == 0 and refused == []


async def test_a_limited_address_is_refused_before_its_body_is_read():
    inner = Recorder()
    limiter = oauth_guard.RegistrationLimiter()
    for _ in range(10):
        limiter.record(OWNER_IP, time.time())
    gate, refused = a_gate(inner, limiter=limiter)
    status, answer, reads, _ = await run_asgi(gate, chunks=(b'{"a": 1}',))
    assert status == 429 and reads == 0 and inner.calls == 0 and refused == [(OWNER_IP, "rate_limited")]
    assert json.loads(answer)["error"] == "temporarily_unavailable"


@pytest.mark.parametrize("path", ["/token", "/revoke"])
async def test_garbage_sent_to_the_token_and_revoke_endpoints_is_a_4xx_and_never_a_500(state_dir, make_provider, serve, path):
    provider = make_provider(state_dir)
    await make_client(provider)
    boundary = "bnd"
    bodies = [
        ({"content-type": FORM}, b""),
        ({"content-type": FORM}, b"\xff\xfe=%FF&client_id=%FF"),
        ({"content-type": FORM}, b"client_id=nobody&client_secret=x&token=y&grant_type=nope"),
        ({"content-type": "application/json"}, b'{"client_id": "nobody"}'),
        ({"content-type": "multipart/form-data"}, b"--x\r\n\r\nhello"),
        (
            {"content-type": f"multipart/form-data; boundary={boundary}"},
            f'--{boundary}\r\nContent-Disposition: form-data; name="client_id"; filename="f"\r\n\r\ndata\r\n--{boundary}--\r\n'.encode(),
        ),
        ({}, b"client_id=nobody"),
        # a declared boundary that the body does not follow: python-multipart raises, and nothing used to turn it into an answer
        ({"content-type": f"multipart/form-data; boundary={boundary}"}, b"\xc0\x80garbage"),
        ({"content-type": f"multipart/form-data; boundary={boundary}"}, b"hello world"),
        ({"content-type": f"multipart/form-data; boundary={boundary}"}, b"--bnd\r\nno colon header\r\n\r\ndata\r\n--bnd--\r\n"),
    ]
    async with serve(provider) as gate:
        for headers, body in bodies:
            reply = await gate.at().post(path, content=body, headers=headers)
            assert 400 <= reply.status_code < 500, (path, headers, body[:40], reply.status_code)


# --------------------------------------------------------------------------
# A multipart body that does not parse is a 400 on the token and revocation endpoints, not an exception (found by fuzzing)
# --------------------------------------------------------------------------

MULTIPART = "multipart/form-data; boundary=x"
MULTIPART_THAT_DOES_NOT_PARSE = {
    "bytes that are not a boundary": b"\xc0\x80garbage",
    "text": b"hello world",
    "a wrong boundary character": b"-Xx\r\n",
    "a header that is not one": b"--x\r\nno colon header\r\n\r\ndata\r\n--x--\r\n",
}
MULTIPART_THAT_PARSES = {
    "nothing": b"",
    "a form field": b'--x\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\r\n--x--\r\n',
    "a file part": b'--x\r\nContent-Disposition: form-data; name="a"; filename="f"\r\n\r\nv\r\n--x--\r\n',
    "a part that stops": b'--x\r\nContent-Disposition: form-data; name="a"\r\n\r\nbo',
}
WITHOUT_CREDENTIALS = {"/token": "invalid_client", "/revoke": "unauthorized_client"}  # the handlers' own 401 for no client_id


@pytest.mark.parametrize("path", ["/token", "/revoke"])
@pytest.mark.parametrize("name", MULTIPART_THAT_DOES_NOT_PARSE)
async def test_a_multipart_body_that_does_not_parse_is_a_clean_400_and_no_handler_runs(
    state_dir, make_provider, serve, monkeypatch, caplog, path, name
):
    capture(caplog)
    provider = make_provider(state_dir)
    ran = handler_spies(provider, monkeypatch)
    async with serve(provider) as gate:
        reply = await gate.at().post(path, content=MULTIPART_THAT_DOES_NOT_PARSE[name], headers={"content-type": MULTIPART})
    assert reply.status_code == 400 and reply.headers["cache-control"] == "no-store"
    assert reply.json() == {"error": "invalid_request", "error_description": "The request is not valid."}
    assert ran == [] and lines(caplog, "oauth-guard") == [f"body outcome=bad_form path={path} ip={OWNER_IP}"]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR or r.exc_info]  # no traceback


@pytest.mark.parametrize("path", ["/token", "/revoke"])
@pytest.mark.parametrize("name", MULTIPART_THAT_PARSES)
async def test_a_multipart_body_that_parses_still_reaches_the_handler_as_it_always_did(state_dir, make_provider, serve, path, name):
    provider = make_provider(state_dir)
    async with serve(provider) as gate:
        reply = await gate.at().post(path, content=MULTIPART_THAT_PARSES[name], headers={"content-type": MULTIPART})
    assert reply.status_code == 401 and reply.json()["error"] == WITHOUT_CREDENTIALS[path]  # the handler's answer, not the guard's


@pytest.mark.parametrize("path", ["/token", "/revoke"])
async def test_a_multipart_request_with_credentials_is_read_by_the_handler_through_the_guard(state_dir, make_provider, serve, path):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    fields = {"client_id": client.client_id, "client_secret": client.client_secret, "token": "nothing"}
    if path == "/token":
        fields.update({"grant_type": "refresh_token", "refresh_token": "prt_nothing"})
    body = "".join(f'--x\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields.items()) + "--x--\r\n"
    async with serve(provider) as gate:
        reply = await gate.at().post(path, content=body.encode(), headers={"content-type": MULTIPART})
    if path == "/token":  # the credentials were read, so the refresh token is what is wrong
        assert reply.status_code == 401 and reply.json() == {"error": "invalid_grant", "error_description": "refresh token does not exist"}
    else:  # RFC 7009: a token that is not known is a success
        assert reply.status_code == 200 and reply.content == b""


async def test_the_form_guard_hands_a_body_that_parses_on_unchanged_and_passes_other_bodies_by_without_reading_them():
    inner = Recorder()
    guard = oauth_guard.FormGuard(inner, path="/x")
    body = MULTIPART_THAT_PARSES["a file part"]
    status, _, _, _ = await run_asgi(guard, headers=[("content-type", "Multipart/Form-Data; boundary=x")], chunks=(body[:20], body[20:]))
    assert status == 200 and inner.bodies == [body] and inner.afterwards == [{"type": "http.disconnect"}]  # any case of the type

    calls = []

    async def silent(scope, receive, send):
        from starlette.responses import Response

        calls.append(True)
        await Response("ok")(scope, receive, send)

    passes_by = oauth_guard.FormGuard(silent, path="/x")
    for headers in ([("content-type", FORM)], [("content-type", "application/json")], [], [("content-type", "multipart/mixed; boundary=x")]):
        status, _, reads, _ = await run_asgi(passes_by, headers=headers, chunks=(b"\xc0\x80garbage",))
        assert status == 200 and reads == 0, headers  # not a multipart form: not read, not parsed, not refused
    assert len(calls) == 4


async def test_the_form_guard_refuses_what_does_not_parse_holds_its_own_limit_and_lets_the_handlers_own_errors_through(caplog):
    caplog.set_level(logging.INFO, logger="oauth-guard")
    inner = Recorder()
    guard = oauth_guard.FormGuard(inner, path="/token", max_bytes=100)
    status, body, _, _ = await run_asgi(guard, headers=[("content-type", MULTIPART)], chunks=(b"\xc0\x80garbage",))
    assert status == 400 and inner.calls == 0 and json.loads(body)["error"] == "invalid_request"
    status, body, _, _ = await run_asgi(guard, headers=[("content-type", MULTIPART)], chunks=(b"x" * 60, b"y" * 41))
    assert status == 413 and inner.calls == 0 and json.loads(body)["error_description"] == "The request body is too large."
    status, _, _, sent = await run_asgi(guard, headers=[("content-type", MULTIPART)], chunks=(b"--x", b"--"), disconnect_after=1)
    assert status is None and sent == [] and inner.calls == 0  # the client left in the middle
    assert lines(caplog, "oauth-guard") == [
        f"body outcome=bad_form path=/token ip={OWNER_IP}", f"body outcome=too_large path=/token ip={OWNER_IP}",
    ]

    async def broken(scope, receive, send):
        raise RuntimeError("a fault of the handler, not of the body")

    fine = MULTIPART_THAT_PARSES["a form field"]
    with pytest.raises(RuntimeError, match="a fault of the handler"):
        await run_asgi(oauth_guard.FormGuard(broken, path="/token"), headers=[("content-type", MULTIPART)], chunks=(fine,))


# --------------------------------------------------------------------------
# The number of clients kept, and who makes room
# --------------------------------------------------------------------------


def seed_clients(provider, count: int, *, age: float, start: int = 1) -> list[str]:
    """`count` clients that were registered `age` seconds ago, put straight into the provider (no registration, no save)."""
    ids = []
    for n in range(start, start + count):
        client_id = f"seed{n:04d}-aaaa-4bbb-8ccc-{n:012d}"
        provider.clients[client_id] = OAuthClientInformationFull(
            client_id=client_id, client_secret=f"secret-{n}", client_id_issued_at=int(provider._now() - age),
            redirect_uris=[AnyUrl(REDIRECT)], token_endpoint_auth_method="client_secret_post",
            grant_types=["authorization_code", "refresh_token"], response_types=["code"], client_name=f"Seed {n}",
        )
        ids.append(client_id)
    return ids


def give_token(provider, client_id: str, kind: str) -> None:
    """kind: "access" (valid), "expired access" (expired a day ago: still in the table) or "refresh"."""
    token = secrets.token_hex(8)
    if kind == "refresh":
        provider.refresh_tokens["prt_" + token] = RefreshToken(token="prt_" + token, client_id=client_id, scopes=[])
        return
    expires = int(provider._now()) + (3600 if kind == "access" else -DAY)
    provider.access_tokens["pat_" + token] = AccessToken(token="pat_" + token, client_id=client_id, scopes=[], expires_at=expires)


def newcomer(n: int = 900, **kwargs) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=f"new{n:05d}-aaaa-4bbb-8ccc-{n:012d}", client_secret="s", client_id_issued_at=int(time.time()),
        redirect_uris=[AnyUrl(REDIRECT)], client_name="Newcomer", **kwargs,
    )


async def test_with_room_a_registration_evicts_nobody(state_dir, make_provider, caplog):
    capture(caplog)
    provider = make_provider(state_dir)
    seed_clients(provider, 49, age=10 * DAY)  # old, idle, and one place free
    await provider.register_client(newcomer())
    assert len(provider.clients) == 50
    assert lines(caplog)[-1].endswith("evicted=0")


async def test_at_the_cap_young_clients_are_never_evicted_and_the_registration_is_refused(state_dir, make_provider, caplog):
    capture(caplog)
    provider = make_provider(state_dir)
    ids = seed_clients(provider, 50, age=DAY - 60)  # all less than a day old
    before = (state_dir / STATE_FILE).exists()
    caplog.clear()
    with pytest.raises(RegistrationError) as info:
        await provider.register_client(newcomer())
    assert info.value.error == "invalid_client_metadata" and "Too many clients" in info.value.error_description
    assert set(provider.clients) == set(ids) and (state_dir / STATE_FILE).exists() == before  # nothing changed, nothing saved
    assert lines(caplog) == ["register outcome=refused reason=client_cap ip=-"]


async def test_at_the_cap_the_oldest_idle_client_over_a_day_old_makes_room(state_dir, make_provider, caplog):
    capture(caplog)
    provider = make_provider(state_dir)
    young = seed_clients(provider, 47, age=3600)
    oldest = seed_clients(provider, 1, age=30 * DAY, start=100)[0]
    older = seed_clients(provider, 1, age=5 * DAY, start=101)[0]
    old = seed_clients(provider, 1, age=2 * DAY, start=102)[0]
    assert len(provider.clients) == 50
    caplog.clear()
    await provider.register_client(newcomer(1))
    assert oldest not in provider.clients and older in provider.clients and old in provider.clients
    assert len(provider.clients) == 50 and newcomer(1).client_id in provider.clients
    assert lines(caplog)[-1].endswith("evicted=1")
    on_disk = json.loads((state_dir / STATE_FILE).read_text())["clients"]
    assert oldest not in on_disk and newcomer(1).client_id in on_disk  # the file says the same
    await provider.register_client(newcomer(2))  # the next one takes the next oldest
    assert older not in provider.clients and old in provider.clients
    await provider.register_client(newcomer(3))
    assert old not in provider.clients and set(young) <= set(provider.clients)
    with pytest.raises(RegistrationError):  # nothing idle and old is left: the three newcomers are young
        await provider.register_client(newcomer(4))


@pytest.mark.parametrize("kind", ["access", "expired access", "refresh"])
async def test_a_client_that_holds_a_token_of_any_kind_is_never_evicted_however_old(state_dir, make_provider, kind):
    provider = make_provider(state_dir)
    ids = seed_clients(provider, 50, age=90 * DAY)
    for client_id in ids:
        give_token(provider, client_id, kind)
    with pytest.raises(RegistrationError):  # every client holds a token: nobody can make room
        await provider.register_client(newcomer())
    assert set(provider.clients) == set(ids)
    victim = ids[17]  # take one client's tokens away: that one, and only that one, can now be evicted
    for table in (provider.access_tokens, provider.refresh_tokens):
        for token in [token for token, record in table.items() if record.client_id == victim]:
            del table[token]
    await provider.register_client(newcomer())
    assert victim not in provider.clients and len(provider.clients) == 50 and set(ids) - {victim} <= set(provider.clients)


async def test_the_victim_is_chosen_among_the_idle_clients_not_among_all_of_them(state_dir, make_provider):
    provider = make_provider(state_dir)
    ids = seed_clients(provider, 50, age=10 * DAY)
    ids_oldest_first = sorted(ids, key=lambda c: (provider.clients[c].client_id_issued_at, c))
    for client_id in ids_oldest_first[:10]:  # the ten oldest hold tokens
        give_token(provider, client_id, "refresh")
    await provider.register_client(newcomer())
    assert all(client_id in provider.clients for client_id in ids_oldest_first[:10])
    assert ids_oldest_first[10] not in provider.clients  # the oldest idle one went


async def test_a_table_that_was_loaded_over_the_cap_is_brought_back_to_it_by_the_next_registration(state_dir, make_provider):
    provider = make_provider(state_dir)
    seed_clients(provider, 60, age=10 * DAY)
    await provider.register_client(newcomer())
    assert len(provider.clients) == 50  # eleven went: the ten that were over, and one for the newcomer
    assert newcomer().client_id in provider.clients


async def test_a_table_over_the_cap_with_too_few_idle_clients_is_refused_and_nothing_is_taken_away(state_dir, make_provider):
    provider = make_provider(state_dir)
    ids = seed_clients(provider, 55, age=10 * DAY)
    for client_id in ids[3:]:
        give_token(provider, client_id, "refresh")  # only three are idle, and six would have to go
    with pytest.raises(RegistrationError):
        await provider.register_client(newcomer())
    assert set(provider.clients) == set(ids)  # the three that were evicted on the way are back


async def test_a_victim_that_cannot_be_removed_ends_the_registration_instead_of_looping(state_dir, make_provider, monkeypatch):
    provider = make_provider(state_dir)
    ids = seed_clients(provider, 50, age=10 * DAY)
    calls = []
    real = oauth_guard.idle_client_to_evict

    def counting(*args, **kwargs):
        calls.append(1)
        if len(calls) > 3:
            raise AssertionError("the eviction loop does not end")
        return real(*args, **kwargs)

    monkeypatch.setattr(oauth_guard, "idle_client_to_evict", counting)
    monkeypatch.setattr(oauth_store, "remove_tokenless_clients", lambda state, candidates: [])
    with pytest.raises(RegistrationError):
        await provider.register_client(newcomer())
    assert len(calls) == 1 and set(provider.clients) == set(ids)


async def test_a_client_has_to_be_more_than_a_day_old_not_exactly_a_day(state_dir, make_provider):
    clock = FakeClock()
    clock.now = float(int(clock.now))  # whole seconds: an issue time is a whole number of seconds
    provider = make_provider(state_dir, clock=clock)
    ids = seed_clients(provider, 50, age=DAY)  # exactly 24 hours
    with pytest.raises(RegistrationError):
        await provider.register_client(newcomer())
    clock.advance(1)
    await provider.register_client(newcomer())
    assert len(provider.clients) == 50 and sum(1 for c in ids if c not in provider.clients) == 1


async def test_a_client_with_no_issue_time_counts_as_the_oldest(state_dir, make_provider):
    provider = make_provider(state_dir)
    ids = seed_clients(provider, 50, age=40 * DAY)
    provider.clients[ids[7]].client_id_issued_at = None
    await provider.register_client(newcomer())
    assert ids[7] not in provider.clients and len(provider.clients) == 50


async def test_registering_a_client_id_that_is_already_there_needs_no_room(state_dir, make_provider):
    provider = make_provider(state_dir)
    ids = seed_clients(provider, 50, age=DAY - 60)
    again = provider.clients[ids[0]].model_copy(update={"client_name": "Renamed"})
    await provider.register_client(again)
    assert len(provider.clients) == 50 and provider.clients[ids[0]].client_name == "Renamed"


async def test_a_registration_that_cannot_be_saved_leaves_the_clients_as_they_were_including_the_one_it_would_have_evicted(
    state_dir, make_provider, monkeypatch
):
    provider = make_provider(state_dir)
    ids = seed_clients(provider, 50, age=10 * DAY)
    before = {c: r.model_dump(mode="json") for c, r in provider.clients.items()}
    same_dict = provider.clients
    fail_saves(monkeypatch)
    with pytest.raises(oauth_store.StateFileError):
        await provider.register_client(newcomer())
    assert {c: r.model_dump(mode="json") for c, r in provider.clients.items()} == before and set(provider.clients) == set(ids)
    assert provider.clients is same_dict  # the table the state view shares is still the same object
    monkeypatch.undo()
    await provider.register_client(newcomer())  # and it works once the disk does
    assert len(provider.clients) == 50


def test_the_idle_client_picker_in_isolation():
    class Record:
        def __init__(self, issued_at):
            self.client_id_issued_at = issued_at

    clients = {"a": Record(100), "b": Record(50), "c": Record(None), "d": Record(10_000), "e": Record(50)}
    pick = oauth_guard.idle_client_to_evict
    assert pick(clients, [], 100_000) == "c"  # no issue time: oldest
    assert pick(clients, ["c"], 100_000) == "b"  # tie between b and e on 50: the smaller id
    assert pick(clients, ["c", "b", "e"], 100_000) == "a"
    assert pick(clients, ["c", "b", "e", "a"], 100_000) == "d"
    assert pick(clients, ["c", "b", "e", "a", "d"], 100_000) is None
    assert pick(clients, [], 10_000 + DAY) == "c"  # d (issued at 10_000) is exactly a day old there: not yet, and c has no time
    assert pick(clients, ["c", "b", "e", "a"], 10_000 + DAY) is None and pick(clients, ["c", "b", "e", "a"], 10_001 + DAY) == "d"
    assert pick({"x": Record(1000)}, [], 1000 + DAY) is None and pick({"x": Record(1000)}, [], 1000 + DAY + 1) == "x"
    assert pick({"x": Record(True)}, [], 5 * DAY) == "x"  # a boolean is not a time: treated as none


async def test_a_registration_over_http_at_the_cap_is_a_400_and_the_oldest_idle_client_makes_room_when_there_is_one(
    state_dir, make_provider, serve
):
    provider = make_provider(state_dir)
    ids = seed_clients(provider, 50, age=DAY - 600)
    async with serve(provider) as gate:
        full = await register_over_http(gate.at())
        assert full.status_code == 400 and full.json()["error"] == "invalid_client_metadata"
        provider.clients[ids[3]].client_id_issued_at = int(provider._now() - 3 * DAY)
        made_room = await register_over_http(gate.at())
        assert made_room.status_code == 201
    assert ids[3] not in provider.clients and made_room.json()["client_id"] in provider.clients and len(provider.clients) == 50


# --------------------------------------------------------------------------
# Fail closed: no password, an unchecked framework, a route that is not there, authorize() on its own
# --------------------------------------------------------------------------


@pytest.mark.parametrize("password", [None, "", "   ", "\t\n", 12345, b"bytes", "\ud800"], ids=repr)
def test_a_provider_cannot_be_built_without_a_usable_password_and_nothing_is_created(tmp_path, password):
    target = tmp_path / "oauth-state"
    with pytest.raises(GateError) as info:
        PersonalAuthProvider(base_url=BASE_URL, password=password, state_dir=str(target))
    assert not target.exists()  # no directory, no lock file, nothing
    assert "password" in str(info.value) and "\ud800" not in str(info.value)


def test_the_password_argument_is_still_the_third_positional_one_and_main_pys_call_still_works(tmp_path):
    provider = PersonalAuthProvider(base_url=BASE_URL, password=PASSWORD, state_dir=str(tmp_path / "a"))
    try:
        assert provider.password == PASSWORD
    finally:
        provider.close()
    import inspect

    names = list(inspect.signature(PersonalAuthProvider.__init__).parameters)
    assert names[:6] == ["self", "base_url", "password", "allowed_redirect_domains", "access_token_expiry_seconds", "state_dir"]


async def test_a_password_that_is_blanked_after_the_provider_was_built_stops_get_routes_and_never_approves(
    state_dir, make_provider, serve
):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    async with serve(provider) as gate:  # the routes exist, built with a good password
        provider.password = None
        refused = await post_consent(gate.at(), client, password="")
        assert refused.status_code == 200 and "Incorrect password" in refused.text  # no password, no approval, whatever is sent
        refused = await post_consent(gate.at(), client, password="None")
        assert refused.status_code == 200
        assert provider.auth_codes == {}
    with pytest.raises(GateError, match="password"):
        provider.get_routes("/mcp")
    provider.password = "   "
    with pytest.raises(GateError, match="password"):
        provider.get_routes("/mcp")


VERSION_CASES = [
    ({"fastmcp": "3.2.4", "mcp": "1.27.0"}, True),
    ({"fastmcp": "3.2.0", "mcp": "1.27.9"}, True),
    ({"fastmcp": "3.2", "mcp": "1.27"}, True),
    ({"fastmcp": "3.2.9.dev1", "mcp": "1.27.1"}, True),
    ({"fastmcp": "3.3.0", "mcp": "1.27.0"}, False),
    ({"fastmcp": "3.1.9", "mcp": "1.27.0"}, False),
    ({"fastmcp": "4.0.0", "mcp": "1.27.0"}, False),
    ({"fastmcp": "2.14.0", "mcp": "1.27.0"}, False),
    ({"fastmcp": "3.20.1", "mcp": "1.27.0"}, False),  # starts like 3.2 and is 3.20
    ({"fastmcp": "3.2.4", "mcp": "1.28.0"}, False),
    ({"fastmcp": "3.2.4", "mcp": "1.2.7"}, False),
    ({"fastmcp": "3.2.4", "mcp": "1.270.0"}, False),
    ({"fastmcp": "3.2.4", "mcp": "2.0.0"}, False),
    ({"fastmcp": "3.2.4", "mcp": None}, False),  # not installed
    ({"fastmcp": None, "mcp": "1.27.0"}, False),
    ({"fastmcp": "", "mcp": "1.27.0"}, False),
]


@pytest.mark.parametrize("versions, fine", VERSION_CASES, ids=[f"fastmcp {v['fastmcp']} mcp {v['mcp']}" for v, _ in VERSION_CASES])
def test_the_gate_starts_only_on_fastmcp_3_2_and_mcp_1_27(tmp_path, monkeypatch, versions, fine):
    monkeypatch.setattr(oauth_guard, "installed_version", versions.get)
    target = tmp_path / "oauth-state"
    if fine:
        oauth_guard.check_framework_versions()
        PersonalAuthProvider(base_url=BASE_URL, password=PASSWORD, state_dir=str(target)).close()
        return
    with pytest.raises(VersionGuardError) as info:
        oauth_guard.check_framework_versions()
    shown = str(info.value)
    bad = [(name, version) for name, version in versions.items() if version is None or version == "" or not oauth_guard_ok(name, version)]
    assert all(name in shown for name, _ in bad) and "refusing" in shown
    assert all(version in shown for _, version in bad if version)
    with pytest.raises(VersionGuardError):  # and the provider is not built, with nothing created
        PersonalAuthProvider(base_url=BASE_URL, password=PASSWORD, state_dir=str(target))
    assert not target.exists()


def oauth_guard_ok(name: str, version: str) -> bool:
    series = dict(oauth_guard.SUPPORTED_VERSIONS)[name]
    return version == series or version.startswith(series + ".")


def test_the_versions_installed_here_are_the_ones_the_gate_was_checked_against():
    assert oauth_guard.installed_version("fastmcp").startswith("3.2.") and oauth_guard.installed_version("mcp").startswith("1.27.")
    assert oauth_guard.installed_version("a-package-that-does-not-exist") is None
    oauth_guard.check_framework_versions()  # a package upgrade that moves either series fails here, on purpose


def test_a_package_whose_metadata_cannot_be_read_counts_as_not_installed_and_stops_the_gate_cleanly(monkeypatch):
    import importlib.metadata

    def unreadable(name):
        raise ValueError("bad metadata")

    monkeypatch.setattr(importlib.metadata, "version", unreadable)
    assert oauth_guard.installed_version("fastmcp") is None
    with pytest.raises(VersionGuardError, match="fastmcp is not installed"):
        oauth_guard.check_framework_versions()


async def test_a_framework_that_changes_after_the_provider_was_built_stops_get_routes(state_dir, make_provider, monkeypatch):
    provider = make_provider(state_dir)
    assert provider.get_routes("/mcp")
    monkeypatch.setattr(oauth_guard, "installed_version", {"fastmcp": "3.3.0", "mcp": "1.27.0"}.get)
    with pytest.raises(VersionGuardError, match="fastmcp 3.3.0"):
        provider.get_routes("/mcp")


def real_routes(provider, mcp_path="/mcp"):
    return OAuthProvider.get_routes(provider, mcp_path)


@pytest.mark.parametrize("path", oauth_guard.BODY_LIMITED_PATHS)
@pytest.mark.parametrize("fault", ["missing", "twice"])
async def test_get_routes_refuses_unless_the_framework_gives_exactly_one_route_for_each_guarded_path(
    state_dir, make_provider, monkeypatch, path, fault
):
    provider = make_provider(state_dir)
    original = OAuthProvider.get_routes

    def faulty(self, mcp_path=None):
        routes = original(self, mcp_path)
        wanted = [route for route in routes if getattr(route, "path", None) == path]
        assert len(wanted) == 1
        return [r for r in routes if r is not wanted[0]] if fault == "missing" else [*routes, wanted[0]]

    monkeypatch.setattr(OAuthProvider, "get_routes", faulty)
    with pytest.raises(GateError) as info:
        provider.get_routes("/mcp")
    assert path in str(info.value) and "exactly one" in str(info.value)


async def test_an_authorize_route_of_another_kind_or_a_second_one_that_is_not_a_route_stops_get_routes(state_dir, make_provider, monkeypatch):
    from starlette.routing import Mount

    provider = make_provider(state_dir)
    original = OAuthProvider.get_routes

    def extra_mount(self, mcp_path=None):
        return [*original(self, mcp_path), Mount("/authorize", app=lambda *a: None)]

    monkeypatch.setattr(OAuthProvider, "get_routes", extra_mount)
    with pytest.raises(GateError, match="the one /authorize route"):
        provider.get_routes("/mcp")

    def only_a_mount(self, mcp_path=None):
        return [r for r in original(self, mcp_path) if getattr(r, "path", None) != "/authorize"] + [Mount("/authorize", app=lambda *a: None)]

    monkeypatch.setattr(OAuthProvider, "get_routes", only_a_mount)
    with pytest.raises(GateError, match="exactly one /authorize"):
        provider.get_routes("/mcp")


async def test_get_routes_replaces_authorize_and_guards_the_other_three_and_leaves_the_rest_alone(state_dir, make_provider):
    provider = make_provider(state_dir)
    routes = provider.get_routes("/mcp")
    by_path = {}
    for route in routes:
        by_path.setdefault(route.path, []).append(route)
    assert all(len(found) == 1 for found in by_path.values())
    for path in oauth_guard.BODY_LIMITED_PATHS:
        assert isinstance(by_path[path][0].endpoint, oauth_guard.BodyLimit), path
        assert by_path[path][0].endpoint.max_bytes == 16 * 1024
    assert set(by_path["/authorize"][0].methods) >= {"GET", "POST"} and "OPTIONS" not in by_path["/authorize"][0].methods
    assert set(by_path["/token"][0].methods) == {"POST", "OPTIONS"} and set(by_path["/revoke"][0].methods) == {"POST", "OPTIONS"}
    assert isinstance(by_path["/register"][0].endpoint.app, oauth_guard.RegistrationGate)
    assert not isinstance(by_path["/token"][0].endpoint.app, oauth_guard.RegistrationGate)
    assert isinstance(by_path["/revoke"][0].endpoint.app, oauth_guard.RevocationMark)
    assert not isinstance(by_path["/token"][0].endpoint.app, oauth_guard.RevocationMark)
    assert not isinstance(by_path["/register"][0].endpoint.app, oauth_guard.RevocationMark)
    assert isinstance(by_path["/token"][0].endpoint.app, oauth_guard.FormGuard)
    assert isinstance(by_path["/revoke"][0].endpoint.app.app, oauth_guard.FormGuard)
    assert not isinstance(by_path["/register"][0].endpoint.app, oauth_guard.FormGuard)  # a JSON endpoint: no form
    # The gate, the mark and the form guard stand in front of the framework's CORS layer, which is what is_cors_preflight
    # assumes: a request that the CORS layer answers by itself never reaches the handler behind it.
    from starlette.middleware.cors import CORSMiddleware

    assert isinstance(by_path["/register"][0].endpoint.app.app, CORSMiddleware)
    assert isinstance(by_path["/token"][0].endpoint.app.app, CORSMiddleware)
    assert isinstance(by_path["/revoke"][0].endpoint.app.app.app, CORSMiddleware)
    assert "/mcp" not in by_path  # the endpoint of the tools is not one of these routes and is never capped
    untouched = [path for path in by_path if path not in oauth_guard.BODY_LIMITED_PATHS]
    assert "/.well-known/oauth-authorization-server" in untouched and untouched
    assert not any(isinstance(by_path[path][0].endpoint, oauth_guard.BodyLimit) for path in untouched)
    # the limiters the routes use are the provider's, so a second call of get_routes shares them
    again = {route.path: route for route in provider.get_routes("/mcp")}
    assert again["/register"].endpoint.app.limiter is by_path["/register"][0].endpoint.app.limiter is provider._registration_limiter


def an_authorization_for(client, redirect=REDIRECT):
    return AuthorizationParams(
        state="s", scopes=None, code_challenge=CHALLENGE, redirect_uri=AnyUrl(redirect), redirect_uri_provided_explicitly=True
    )


async def test_authorize_called_on_its_own_raises_access_denied_and_issues_nothing(state_dir, make_provider, caplog):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await make_client(provider)
    caplog.clear()
    with pytest.raises(AuthorizeError) as info:
        await provider.authorize(client, an_authorization_for(client))
    assert info.value.error == "access_denied" and provider.auth_codes == {}
    assert lines(caplog) == [f"authorize outcome=invalid_request reason=no_consent client={client.client_id[:8]}"]
    assert [r.levelno for r in caplog.records if r.name == "personal-auth"] == [logging.ERROR]


async def test_an_approval_is_for_one_client_one_address_and_one_code(state_dir, make_provider):
    provider = make_provider(state_dir)
    client, other = await make_client(provider, 1), await make_client(provider, 2)
    params_ = an_authorization_for(client)

    with oauth_guard.approved(other.client_id, REDIRECT):  # approved for another client
        with pytest.raises(AuthorizeError):
            await provider.authorize(client, params_)
    with oauth_guard.approved(client.client_id, "https://claude.ai/other"):  # approved for another address
        with pytest.raises(AuthorizeError):
            await provider.authorize(client, params_)
    assert provider.auth_codes == {}

    with oauth_guard.approved(client.client_id, REDIRECT):
        url = await provider.authorize(client, params_)
        assert "code=" in url and len(provider.auth_codes) == 1
        with pytest.raises(AuthorizeError):  # a second code needs a second approval
            await provider.authorize(client, params_)
    assert len(provider.auth_codes) == 1
    with pytest.raises(AuthorizeError):  # and the approval ended with the block
        await provider.authorize(client, params_)


async def test_authorize_checks_the_redirect_address_again_even_for_an_approved_request(state_dir, make_provider, caplog):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await make_client(provider)
    evil = an_authorization_for(client, "https://evil.example/cb")
    caplog.clear()
    with oauth_guard.approved(client.client_id, "https://evil.example/cb"):
        with pytest.raises(AuthorizeError) as info:
            await provider.authorize(client, evil)
    assert info.value.error == "access_denied" and provider.auth_codes == {}
    assert lines(caplog) == [
        f"authorize outcome=blocked_redirect reason=not_allowed client={client.client_id[:8]} redirect=evil.example"
    ]


async def test_the_frameworks_own_refusal_inside_authorize_comes_out_as_what_it_is(state_dir, make_provider):
    # AuthorizeError is a frozen dataclass: through a contextlib.contextmanager it would come out as FrozenInstanceError
    provider = make_provider(state_dir)
    stranger = OAuthClientInformationFull(client_id="stranger", redirect_uris=[AnyUrl(REDIRECT)])  # not registered
    with oauth_guard.approved("stranger", REDIRECT):
        with pytest.raises(AuthorizeError) as info:
            await provider.authorize(stranger, an_authorization_for(stranger))
    assert info.value.error == "unauthorized_client" and provider.auth_codes == {}
    with pytest.raises(AuthorizeError) as info:  # and an error of the block passes through `approved` unchanged
        with oauth_guard.approved("c", "r"):
            raise AuthorizeError(error="invalid_scope", error_description="x")
    assert info.value.error == "invalid_scope"


async def test_an_approval_does_not_leak_out_of_the_request_that_made_it(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await make_client(provider)
    async with serve(provider) as gate:
        assert (await post_consent(gate.at(), client)).status_code == 302
        provider.auth_codes.clear()
        with pytest.raises(AuthorizeError):  # the next caller, in the same process and the same task, is not approved
            await provider.authorize(client, an_authorization_for(client))
        assert oauth_guard.take_approval(client.client_id, REDIRECT) is False


def test_an_approval_ends_with_its_block_whether_or_not_anything_used_it_and_whether_or_not_the_block_failed():
    with oauth_guard.approved("c", "https://claude.ai/cb"):
        pass
    assert oauth_guard.take_approval("c", "https://claude.ai/cb") is False
    with pytest.raises(RuntimeError):
        with oauth_guard.approved("c", "https://claude.ai/cb"):
            raise RuntimeError("the block failed")
    assert oauth_guard.take_approval("c", "https://claude.ai/cb") is False
    with oauth_guard.approved("c", "https://claude.ai/cb"):
        assert oauth_guard.take_approval("c", "https://claude.ai/cb") is True
        assert oauth_guard.take_approval("c", "https://claude.ai/cb") is False  # once


async def test_requests_that_run_at_the_same_time_do_not_see_each_others_approvals():
    async def request(mine: str, other: str):
        with oauth_guard.approved(mine, "https://claude.ai/cb"):
            await asyncio.sleep(0)  # the other request runs now
            assert oauth_guard.take_approval(other, "https://claude.ai/cb") is False
            await asyncio.sleep(0)
            assert oauth_guard.take_approval(mine, "https://claude.ai/cb") is True

    await asyncio.gather(request("a", "b"), request("b", "a"))
    assert oauth_guard.take_approval("a", "https://claude.ai/cb") is False


async def test_approving_calls_authorize_inside_an_approval_for_exactly_that_client_and_address(state_dir, make_provider, serve, monkeypatch):
    seen = []
    real = PersonalAuthProvider.authorize

    async def spy(self, client, params_):
        seen.append((client.client_id, str(params_.redirect_uri), oauth_guard._APPROVAL.get()))
        return await real(self, client, params_)

    monkeypatch.setattr(PersonalAuthProvider, "authorize", spy)
    provider = make_provider(state_dir)
    client = await make_client(provider)
    async with serve(provider) as gate:
        assert (await post_consent(gate.at(), client)).status_code == 302
        assert (await post_consent(gate.at(), client, password="wrong")).status_code == 200
        assert (await post_consent(gate.at(), client, decision="deny")).status_code == 302
    assert seen == [(client.client_id, REDIRECT, oauth_guard.Approval(client.client_id, REDIRECT))]  # once, for the approval only


async def test_a_code_that_cannot_be_saved_is_taken_back_and_the_next_try_works(state_dir, make_provider, serve, monkeypatch, caplog):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await make_client(provider)
    caplog.clear()
    async with serve(provider) as gate:
        fail_saves(monkeypatch)
        failed = await post_consent(gate.at(), client)
        assert failed.status_code == 500 and failed.content == oauth_guard.SERVER_ERROR_PAGE.encode()
        assert "location" not in failed.headers and provider.auth_codes == {}  # nothing was issued, nothing is left over
        monkeypatch.undo()
        assert (await post_consent(gate.at(), client)).status_code == 302
    assert [m for m in lines(caplog) if m.startswith("authorize outcome=error")] == [
        f"authorize outcome=error ip={OWNER_IP} error=StateFileError"
    ]


async def test_an_unexpected_error_in_the_consent_endpoint_is_a_plain_500_page_and_a_log_line_without_the_message(
    state_dir, make_provider, serve, monkeypatch, caplog
):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await make_client(provider)

    def broken(self, *args, **kwargs):
        raise RuntimeError("detail that must stay out of the page and the journal: " + PASSWORD)

    async def broken_async(self, *args, **kwargs):
        raise RuntimeError("detail that must stay out of the page and the journal: " + PASSWORD)

    monkeypatch.setattr(PersonalAuthProvider, "_consent_get", broken)
    monkeypatch.setattr(PersonalAuthProvider, "_decide", broken_async)
    caplog.clear()
    hostile = "198.51.100.9\nINFO forged"
    async with serve(provider) as gate:
        got = await gate.at().get("/authorize", params=params(client))
        posted = await post_consent(Gate(gate.app).at(hostile), client)
    for reply in (got, posted):
        assert reply.status_code == 500 and reply.content == oauth_guard.SERVER_ERROR_PAGE.encode()
        for name, value in CONSENT_HEADERS.items():
            assert reply.headers[name] == value
    assert [m for m in lines(caplog) if m.startswith("authorize")] == [
        f"authorize outcome=error ip={OWNER_IP} error=RuntimeError",
        f"authorize outcome=error ip={oauth_guard.safe(hostile)} error=RuntimeError",  # escaped, like every other line
    ]
    assert "detail that must stay out" not in caplog.text and PASSWORD not in caplog.text


async def test_the_password_is_compared_with_its_spaces_and_the_page_does_not_trim_what_is_typed(state_dir, make_provider, serve):
    password = "  a pass phrase with spaces at both ends  "
    provider = make_provider(state_dir, password=password)
    client = await make_client(provider)
    async with serve(provider) as gate:
        http = gate.at()
        assert (await post_consent(http, client, password=password.strip())).status_code == 200  # trimmed: not the password
        assert (await post_consent(http, client, password=password + " ")).status_code == 200
        assert (await post_consent(http, client, password=password)).status_code == 302


async def test_the_frameworks_own_authorize_handler_cannot_issue_a_code_if_it_ever_got_into_the_path(state_dir, make_provider):
    # what the replacement of the route protects, and what authorize() protects when the replacement fails: here the
    # framework's handler is mounted as it ships, in front of the same provider, and it can only be refused
    provider = make_provider(state_dir)
    client = await make_client(provider)
    app = Starlette(routes=[Route("/authorize", AuthorizationHandler(provider=provider).handle, methods=["GET", "POST"])])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test") as http:
        reply = await http.get("/authorize", params=params(client), follow_redirects=False)
    assert provider.auth_codes == {}
    assert reply.status_code == 302 and "code" not in location_params(reply)
    assert location_params(reply)["error"] == ["access_denied"]


# --------------------------------------------------------------------------
# A short password starts, with a warning that does not say how short
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "password, warns",
    [("x", True), ("x" * 11, True), ("x" * 15, True), ("x" * 16, False), ("x" * 40, False), ("e\N{COMBINING ACUTE ACCENT}" * 15, True), ("e\N{COMBINING ACUTE ACCENT}" * 16, False)],
    ids=["1", "11", "15", "16", "40", "15 accented", "16 accented"],
)
def test_a_password_shorter_than_16_characters_gets_one_startup_warning_that_does_not_give_its_length(tmp_path, caplog, password, warns):
    caplog.set_level(logging.INFO, logger="personal-auth")
    provider = PersonalAuthProvider(base_url=BASE_URL, password=password, state_dir=str(tmp_path / "s"))
    try:
        warnings = [r for r in caplog.records if r.name == "personal-auth" and r.levelno == logging.WARNING]
        assert len(warnings) == (1 if warns else 0)
        if warns:
            message = warnings[0].getMessage()
            assert "shorter than 16 characters" in message
            assert re.findall(r"\d+", message) == ["16"]  # the limit, not the length
            assert password not in message and message.isascii()
        assert provider.password == password  # it still starts
    finally:
        provider.close()


# --------------------------------------------------------------------------
# The journal
# --------------------------------------------------------------------------


async def test_the_log_outcomes_are_the_stable_tags_and_nothing_in_the_journal_is_forged_or_secret(state_dir, make_provider, serve, caplog):
    caplog.set_level(logging.INFO)  # every logger
    clock = FakeClock()
    state_dir.mkdir()
    legacy_id = "00000000-0000-4000-8000-0000000000e1"
    (state_dir / STATE_FILE).write_text(json.dumps(v1_file({legacy_id: legacy_client_record(legacy_id, ["https://evil.example/cb"], "Evil")})))
    provider = make_provider(state_dir, clock=clock)
    hostile_name = 'Evil\nINFO forged outcome=approved\x1b[31m "quoted" \\ \N{RIGHT-TO-LEFT OVERRIDE} end'
    wrong_passwords = ["wrong-guess-1\nINFO forged", "wrong-guess-2", "wrong-guess-3", "wrong-guess-4", "wrong-guess-5"]
    async with serve(provider) as gate:
        http = gate.at()
        registered = (await register_over_http(http, client_name=hostile_name)).json()
        refused = await register_over_http(http, redirect_uris=["https://evil.example/cb?secret=abc\nINFO forged"])
        assert refused.status_code == 400
        client = OAuthClientInformationFull.model_validate(registered)
        query = {
            "client_id": client.client_id, "redirect_uri": REDIRECT, "response_type": "code", "code_challenge": CHALLENGE,
            "state": "state\nINFO forged state",
        }
        await http.get("/authorize", params=query)  # consent_shown
        await http.get("/authorize", params={**query, "client_id": "id\nINFO forged id\x1b[0m"})  # invalid_request
        await http.get("/authorize", params={"client_id": legacy_id, "response_type": "code", "code_challenge": CHALLENGE})  # blocked_redirect
        await http.post("/authorize", data={**query, "decision": "deny"})  # denied_by_user
        for guess in wrong_passwords:  # wrong_password x5
            await http.post("/authorize", data={**query, "decision": "approve", "password": guess})
        await http.post("/authorize", data={**query, "decision": "approve", "password": PASSWORD})  # rate_limited
        clock.advance(900)
        approved = await http.post("/authorize", data={**query, "decision": "approve", "password": PASSWORD})  # approved
        assert approved.status_code == 302
        code = location_params(approved)["code"][0]
        await http.post("/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": client.client_id,
            "client_secret": registered["client_secret"], "code_verifier": VERIFIER,
        })
        await http.post("/register", content=b"not json", headers={"content-type": "application/json"})
        await http.post("/register", content=b"x" * (CAP + 1), headers={"content-type": "application/json"})
        await http.request("OPTIONS", "/register", content=b"{}", headers={"content-type": "application/json"})  # options_body
    texts = [record.getMessage() for record in caplog.records]
    tags = {
        kind: set(re.findall(rf"^{kind} outcome=(\w+)", "\n".join(texts), flags=re.MULTILINE))
        for kind in ("authorize", "register", "token", "body")
    }
    assert tags["authorize"] == {
        "consent_shown", "invalid_request", "blocked_redirect", "denied_by_user", "wrong_password", "rate_limited", "approved",
    }
    assert tags["register"] == {"registered", "refused"} and tags["token"] == {"issued"}
    assert tags["body"] == {"too_large", "options_body"}
    secrets_in_play = [PASSWORD, VERIFIER, code, registered["client_secret"], *wrong_passwords, "abc", "forged state"]
    for text in texts:
        assert text.isascii() and text.isprintable(), text  # no newline, no control code, nothing outside printable ASCII
        for secret in secrets_in_play:
            assert secret not in text, (secret, text)
    assert not [t for t in texts if t.startswith("INFO") or "forged" in t.replace("\\x0aINFO forged", "")], texts
    forged = [t for t in texts if "forged" in t]
    assert forged and all(t.split()[0] in ("authorize", "register") for t in forged)  # only inside the escaped values
    assert any('name="Evil\\x0aINFO forged outcome=approved\\x1b[31m \\"quoted\\" \\\\ \\u202e end"' in t for t in texts)
    for text in texts:
        if text.startswith("authorize") or text.startswith("register"):
            assert re.search(r"\bip=203\.0\.113\.7\b", text) or "ip=" not in text, text  # the address of the client, not the proxy's


async def test_the_address_in_a_log_line_is_the_one_the_server_gave_and_a_hostile_one_cannot_forge_a_line(state_dir, make_provider, serve, caplog):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await make_client(provider)
    caplog.clear()
    async with serve(provider) as gate:
        await gate.at("2001:db8::1234").get("/authorize", params=params(client))
        hostile = Gate(gate.app).at("203.0.113.4\nINFO forged ip=198.51.100.9")  # not an address, but a server could pass anything
        await hostile.get("/authorize", params=params(client))
        await hostile.aclose()
    assert lines(caplog) == [
        f'authorize outcome=consent_shown client={client.client_id[:8]} name="Test client 1" ip=2001:db8::1234',
        f'authorize outcome=consent_shown client={client.client_id[:8]} name="Test client 1" ip=203.0.113.4\\x0aINFO forged ip=198.51.100.9',
    ]


async def test_a_hostile_address_cannot_forge_a_line_in_any_kind_of_log_line(state_dir, make_provider, serve, caplog):
    caplog.set_level(logging.INFO)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)
    provider.clients[LEGACY_EVIL] = OAuthClientInformationFull.model_validate(
        legacy_client_record(LEGACY_EVIL, ["https://evil.example/cb"])
    )
    client = await make_client(provider)
    hostile = "203.0.113.4\nINFO forged ip=198.51.100.9\x1b[31m"  # not an address, but a server could pass anything
    shown = oauth_guard.safe(hostile)
    assert "\n" not in shown and "\x1b" not in shown
    caplog.clear()
    async with serve(provider) as gate:
        http = Gate(gate.app).at(hostile)
        await register_over_http(http)  # registered
        await register_over_http(http, redirect_uris=["https://evil.example/cb"])  # refused
        await http.post("/register", content=b"not json", headers={"content-type": "application/json"})  # refused
        await http.post("/register", content=b"x" * (CAP + 1), headers={"content-type": "application/json"})  # too_large
        await http.request("OPTIONS", "/token", content=b"{}", headers={"content-type": "application/json"})  # options_body
        await http.post("/register", content=b'{"client_name": "\\ud800"}', headers={"content-type": "application/json"})  # refused
        await http.get("/authorize", params=params(client))  # consent_shown
        await http.get("/authorize", params={"client_id": "nobody"})  # invalid_request
        await http.get("/authorize", params={"client_id": LEGACY_EVIL, "response_type": "code", "code_challenge": CHALLENGE})  # blocked
        await http.post("/authorize", json={})  # invalid_request (the content type)
        await post_consent(http, client, decision="deny")  # denied_by_user
        for _ in range(5):
            await post_consent(http, client, password="wrong")  # wrong_password
        await post_consent(http, client)  # rate_limited
        clock.advance(900)
        await post_consent(http, client)  # approved
        await http.aclose()
    messages = [r.getMessage() for r in caplog.records if r.name in ("personal-auth", "oauth-guard")]
    outcome_lines = [m for m in messages if m.split()[0] in ("authorize", "register", "body")]
    assert {m.split()[1] for m in outcome_lines} == {
        "outcome=consent_shown", "outcome=invalid_request", "outcome=blocked_redirect", "outcome=denied_by_user",
        "outcome=wrong_password", "outcome=rate_limited", "outcome=approved", "outcome=registered", "outcome=refused",
        "outcome=too_large", "outcome=options_body",
    }
    for message in messages:
        assert message.isascii() and message.isprintable(), message  # one line each, no control code
    for message in outcome_lines:
        assert f"ip={shown}" in message, message  # every one of them names the address, escaped, and nothing leaks out of it
    assert not [m for m in messages if m.startswith(("INFO", "forged"))]


async def test_a_request_with_no_client_address_is_served_and_counted_under_one_shared_key(state_dir, make_provider, caplog):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await make_client(provider)
    (route,) = [r for r in provider.get_routes("/mcp") if r.path == "/authorize"]
    body = encode({**params(client), "decision": "approve", "password": "wrong"})
    headers = [("content-type", FORM), ("content-length", str(len(body)))]
    for _ in range(5):
        status, _, _, _ = await run_asgi(route.endpoint, headers=headers, chunks=(body,), client=None)
        assert status == 200
    status, page, _, _ = await run_asgi(route.endpoint, headers=headers, chunks=(body,), client=None)
    assert status == 429 and page == oauth_guard.TOO_MANY_ATTEMPTS_PAGE.encode()
    assert any(m.endswith("ip=?") for m in lines(caplog))
    assert (await post_consent_with_ip_unknown(provider, client)) is False


async def post_consent_with_ip_unknown(provider, client) -> bool:
    """True if an address that does exist is limited too, by the key of those that do not (it must not be)."""
    (route,) = [r for r in provider.get_routes("/mcp") if r.path == "/authorize"]
    body = encode({**params(client), "decision": "approve", "password": PASSWORD})
    status, _, _, _ = await run_asgi(route.endpoint, headers=[("content-type", FORM), ("content-length", str(len(body)))], chunks=(body,))
    return status == 429


# --------------------------------------------------------------------------
# Hygiene of the sign-in files
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["oauth_guard.py", "personal_auth.py", "tests/test_personal_auth_gate.py"])
def test_the_files_hold_no_em_or_en_dash_and_no_text_outside_ascii(name):
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    assert EM_DASH not in text and EN_DASH not in text
    assert text.isascii()


def test_the_guard_module_imports_nothing_of_the_provider_and_touches_no_file_or_environment():
    import ast

    tree = ast.parse((REPO_ROOT / "oauth_guard.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not imported & {"personal_auth", "oauth_store", "server", "main", "settings", "os", "pathlib", "socket", "httpx"}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not names & {"open", "environ", "getenv", "read_text", "write_text", "unlink"}
