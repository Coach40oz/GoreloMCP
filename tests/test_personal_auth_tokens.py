"""personal_auth.py, the token lifecycle: one issuer, rotation, tombstones, reuse, /revoke (which only looks a token up:
no reuse detection and no expiry line inside a request to it), expired tokens, pruning, the state file at start and at
every save, and compatibility with the code that production ran before.

Everything is offline: temporary state directories, fake data, a fake clock where time matters, and the real HTTP app
driven in process through httpx.ASGITransport (no sockets). The authorization code is put into provider.auth_codes
directly for the token tests (the consent page is not what they are about); the compatibility tests and one end to end
test go through the real register, authorize and token endpoints.

The legacy provider is tests/legacy/personal_auth_previous.py, a frozen copy of what the previous release ran.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import logging
import os
import secrets
import stat
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from legacy import personal_auth_previous
from mcp.server.auth.provider import AccessToken, AuthorizationCode, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

import oauth_guard
import oauth_store
import personal_auth
from personal_auth import PersonalAuthProvider
from server import build_server
from tools._common import Registry

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "https://mcp.example.test"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
PASSWORD = "a-test-password-not-real"
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


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class FakeClock:
    """Seconds since the epoch, moved by hand. It starts at the real time so that what the framework checks against the
    real clock (token expiry at /mcp, the authorization code) stays consistent."""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def make_provider():
    """make_provider(state_dir, **kwargs) -> PersonalAuthProvider; every provider is closed at the end of the test."""
    created: list = []

    def factory(state_dir, *, legacy: bool = False, **kwargs):
        cls = personal_auth_previous.PersonalAuthProvider if legacy else PersonalAuthProvider
        provider = cls(base_url=BASE_URL, password=PASSWORD, state_dir=str(state_dir), **kwargs)
        created.append(provider)
        return provider

    yield factory
    for provider in created:
        if hasattr(provider, "close"):
            provider.close()


@pytest.fixture
def state_dir(tmp_path) -> Path:
    return tmp_path / "oauth-state"


@pytest.fixture
def serve(make_settings, mock_gorelo, spec_index):
    """serve(provider) -> async context manager yielding an httpx client wired to the real HTTP app (no sockets)."""

    @asynccontextmanager
    async def serving(provider):
        server = build_server(
            make_settings(), auth=provider, transport=mock_gorelo.transport, spec=spec_index, registry=Registry()
        )
        app = server.http_app(json_response=True)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 40000))
            async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as http:
                yield http

    return serving


async def register(provider, n: int = 1) -> OAuthClientInformationFull:
    """A registered client with a secret, as the registration endpoint would store one."""
    client = OAuthClientInformationFull(
        client_id=f"{n:08d}-aaaa-4bbb-8ccc-{n:012d}",
        client_secret=f"client-secret-{n}-" + "x" * 24,
        client_id_issued_at=int(time.time()),
        redirect_uris=[AnyUrl(REDIRECT)],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name=f"Test client {n}",
    )
    await provider.register_client(client)
    return client


def mint_code(provider, client, code: str = "code-1", scopes=()) -> AuthorizationCode:
    record = AuthorizationCode(
        code=code,
        scopes=list(scopes),
        expires_at=time.time() + 300,
        client_id=client.client_id,
        code_challenge=CHALLENGE,
        redirect_uri=AnyUrl(REDIRECT),
        redirect_uri_provided_explicitly=True,
    )
    provider.auth_codes[code] = record
    return record


async def sign_in(provider, client, *, code: str = "code-1", scopes=()):
    """The tokens of a first sign-in (code exchange), issued by the provider itself."""
    return await provider.exchange_authorization_code(client, mint_code(provider, client, code, scopes))


async def refresh(provider, client, token: str, scopes=None):
    """A refresh as the SDK's token handler performs it: load, then exchange. Returns None when the load refuses."""
    loaded = await provider.load_refresh_token(client, token)
    if loaded is None:
        return None
    return await provider.exchange_refresh_token(client, loaded, list(loaded.scopes if scopes is None else scopes))


def on_disk(state_dir: Path) -> dict:
    return json.loads((state_dir / STATE_FILE).read_text())


def state_fingerprint(provider) -> str:
    """Everything the provider keeps about tokens, as one string, to prove that a call changed nothing."""
    return json.dumps(oauth_store.dump_state(provider._state_view()), sort_keys=True) + json.dumps(
        sorted(provider.auth_codes)
    )


def records(caplog, *, level: int | None = None, name: str | None = None) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if (level is None or r.levelno == level) and (name is None or r.name == name)
    ]


def messages(caplog, **kwargs) -> list[str]:
    return [r.getMessage() for r in records(caplog, **kwargs)]


def capture(caplog) -> None:
    caplog.set_level(logging.INFO, logger="personal-auth")
    caplog.set_level(logging.INFO, logger="oauth-store")


def fail_saves(monkeypatch) -> None:
    """Make every write of the state file fail at the last step (the rename)."""
    real = os.replace

    def refuse(src, dst, *args, **kwargs):
        if str(dst).endswith(STATE_FILE):
            raise OSError(5, "Input/output error")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", refuse)


# --- the HTTP side -----------------------------------------------------------------------------


@dataclass
class Session:
    client_id: str
    client_secret: str
    access_token: str
    refresh_token: str
    expires_in: int


def refresh_form(client_id: str, client_secret: str, refresh_token: str) -> dict:
    return {
        "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id, "client_secret": client_secret,
    }


async def http_register(http) -> dict:
    reply = await http.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT], "client_name": "Claude", "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        },
    )
    assert reply.status_code == 201, reply.text
    return reply.json()


async def http_sign_in(http) -> Session:
    """The real flow: register, approve on the consent page with the password, exchange the code."""
    client = await http_register(http)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    approved = await http.post(
        "/authorize",
        data={
            "client_id": client["client_id"], "redirect_uri": REDIRECT, "response_type": "code", "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "abc", "decision": "approve", "password": PASSWORD,
        },
    )
    assert approved.status_code == 302, approved.text
    code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
    reply = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": client["client_id"],
            "client_secret": client["client_secret"], "code_verifier": verifier,
        },
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()
    return Session(client["client_id"], client["client_secret"], body["access_token"], body["refresh_token"], body["expires_in"])


async def http_refresh(http, session: Session, token: str | None = None) -> httpx.Response:
    return await http.post("/token", data=refresh_form(session.client_id, session.client_secret, token or session.refresh_token))


async def mcp_status(http, access_token: str | None) -> int:
    headers = dict(MCP_HEADERS)
    if access_token is not None:
        headers["Authorization"] = f"Bearer {access_token}"
    return (await http.post("/mcp", headers=headers, json=INITIALIZE)).status_code


# --------------------------------------------------------------------------
# Defaults and the constructor
# --------------------------------------------------------------------------


async def test_the_defaults_are_the_lifetimes_and_settings_of_today(state_dir, make_provider):
    provider = make_provider(state_dir)
    assert personal_auth.DEFAULT_ACCESS_TOKEN_EXPIRY == 30 * DAY == provider.access_token_expiry_seconds
    assert personal_auth.DEFAULT_REFRESH_ACCESS_TOKEN_EXPIRY == 3600 == provider.refresh_access_token_expiry_seconds
    assert personal_auth.DEFAULT_REUSE_POLICY == "log" == provider.reuse_policy
    assert personal_auth.DEFAULT_REUSE_GRACE_SECONDS == 300 == provider.reuse_grace_seconds
    assert personal_auth.REUSE_POLICIES == ("log", "revoke")
    assert personal_auth.DEFAULT_STATE_DIR == ".oauth-state"
    assert provider.revocation_options is not None and provider.revocation_options.enabled is True
    assert provider.client_registration_options is not None and provider.client_registration_options.enabled is True


async def test_the_constructor_call_of_main_py_still_works_with_no_new_argument(state_dir):
    # main.py passes exactly these three keyword arguments
    provider = PersonalAuthProvider(base_url=BASE_URL, password="pw-not-real", state_dir=str(state_dir))
    try:
        assert provider.reuse_policy == "log" and (state_dir / oauth_store.LOCK_FILE_NAME).exists()
    finally:
        provider.close()


async def test_an_unknown_reuse_policy_is_refused_before_anything_is_created(tmp_path):
    with pytest.raises(ValueError, match="reuse_policy must be one of log, revoke"):
        PersonalAuthProvider(base_url=BASE_URL, password="pw", state_dir=str(tmp_path / "s"), reuse_policy="ignore")
    assert not (tmp_path / "s").exists()


async def test_the_clock_that_is_passed_in_is_the_one_the_provider_uses(state_dir, make_provider):
    clock = FakeClock()
    clock.now = 1_900_000_000
    provider = make_provider(state_dir, clock=clock)
    client = await register(provider)
    tokens = await sign_in(provider, client)
    assert provider.access_tokens[tokens.access_token].expires_at == 1_900_000_000 + 30 * DAY
    assert provider._refresh_meta[tokens.refresh_token]["issued_at"] == 1_900_000_000


async def test_construction_creates_the_state_directory_0700_and_the_lock_file_0600(tmp_path, make_provider):
    target = tmp_path / "x" / "oauth-state"
    old = os.umask(0)
    try:
        make_provider(target)
    finally:
        os.umask(old)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE((target / oauth_store.LOCK_FILE_NAME).stat().st_mode) == 0o600
    assert sorted(os.listdir(target)) == [oauth_store.LOCK_FILE_NAME]  # no state file until something is saved


async def test_the_state_file_and_every_save_are_mode_0600(state_dir, make_provider):
    old = os.umask(0)
    try:
        provider = make_provider(state_dir)
        await sign_in(provider, await register(provider))
    finally:
        os.umask(old)
    assert stat.S_IMODE((state_dir / STATE_FILE).stat().st_mode) == 0o600


async def test_a_second_provider_on_the_same_directory_is_refused_until_the_first_lets_go(state_dir, make_provider):
    first = make_provider(state_dir)
    with pytest.raises(oauth_store.StateLockedError):
        PersonalAuthProvider(base_url=BASE_URL, password="pw", state_dir=str(state_dir), state_lock_wait_seconds=0)
    first.close()
    second = make_provider(state_dir)  # fine now
    assert second._state_lock.held


async def test_construction_removes_leftover_temp_files_of_a_killed_save(state_dir, make_provider):
    state_dir.mkdir()
    (state_dir / f".{STATE_FILE}.999.cafebabe.tmp").write_text("a full copy of the credentials")
    make_provider(state_dir)
    assert sorted(os.listdir(state_dir)) == [oauth_store.LOCK_FILE_NAME]


# --------------------------------------------------------------------------
# The load line and what construction does to the file
# --------------------------------------------------------------------------


async def test_a_fresh_start_logs_the_load_line_with_format_none(state_dir, make_provider, caplog):
    capture(caplog)
    make_provider(state_dir)
    assert messages(caplog, name="personal-auth") == ["oauth state loaded format=none clients=0 access=0 refresh=0 pruned=0"]


async def test_construction_never_writes_the_state_file(state_dir, make_provider):
    builder = LegacyFile()
    document = builder.document()
    state_dir.mkdir()
    (state_dir / STATE_FILE).write_text(json.dumps(document, indent=2))
    before = (state_dir / STATE_FILE).read_bytes()
    mtime = (state_dir / STATE_FILE).stat().st_mtime_ns
    make_provider(state_dir)
    assert (state_dir / STATE_FILE).read_bytes() == before
    assert (state_dir / STATE_FILE).stat().st_mtime_ns == mtime
    assert sorted(os.listdir(state_dir)) == sorted([STATE_FILE, oauth_store.LOCK_FILE_NAME])


async def test_a_state_file_that_cannot_be_trusted_stops_construction_and_stays_exactly_as_it_was(state_dir, make_provider):
    state_dir.mkdir()
    leftover = state_dir / f".{STATE_FILE}.999.cafebabe.tmp"
    leftover.write_text("evidence for the operator")  # a refused start leaves everything as it found it, this too
    for content in (b"{", b"", b"[]", b'{"clients": []}', b'{"version": 9}'):
        (state_dir / STATE_FILE).write_bytes(content)
        with pytest.raises(oauth_store.StateFileError):
            PersonalAuthProvider(base_url=BASE_URL, password="pw", state_dir=str(state_dir))
        assert (state_dir / STATE_FILE).read_bytes() == content
        assert leftover.read_text() == "evidence for the operator"
        assert sorted(os.listdir(state_dir)) == sorted([STATE_FILE, oauth_store.LOCK_FILE_NAME, leftover.name])
    leftover.unlink()
    # the failed constructions let go of the lock: a good file can be used straight away
    (state_dir / STATE_FILE).write_bytes(b"{}")
    assert make_provider(state_dir)._state_lock.held


async def test_a_lock_held_by_someone_else_stops_construction_without_touching_the_file(state_dir):
    state_dir.mkdir()
    (state_dir / STATE_FILE).write_bytes(b"{}")
    with oauth_store.StateLock(state_dir):
        with pytest.raises(oauth_store.StateLockedError):
            PersonalAuthProvider(base_url=BASE_URL, password="pw", state_dir=str(state_dir), state_lock_wait_seconds=0)
    assert (state_dir / STATE_FILE).read_bytes() == b"{}"


# --------------------------------------------------------------------------
# One issuer: code exchange
# --------------------------------------------------------------------------


async def test_a_code_exchange_issues_pat_and_prt_lasting_30_days_and_never_expiring(state_dir, make_provider, caplog):
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)
    client = await register(provider)

    tokens = await sign_in(provider, client, scopes=["mcp"])

    assert tokens.token_type == "Bearer" and tokens.expires_in == 30 * DAY and tokens.scope == "mcp"
    assert tokens.access_token.startswith("pat_") and len(tokens.access_token) == 4 + 64
    assert tokens.refresh_token.startswith("prt_") and len(tokens.refresh_token) == 4 + 64
    access = provider.access_tokens[tokens.access_token]
    refresh_token = provider.refresh_tokens[tokens.refresh_token]
    assert access.expires_at == int(clock.now + 30 * DAY) and access.client_id == client.client_id and access.scopes == ["mcp"]
    assert refresh_token.expires_at is None and refresh_token.client_id == client.client_id and refresh_token.scopes == ["mcp"]
    assert provider._access_to_refresh_map == {tokens.access_token: tokens.refresh_token}
    assert provider._refresh_to_access_map == {tokens.refresh_token: tokens.access_token}
    meta = provider._refresh_meta[tokens.refresh_token]
    assert set(meta) == {"family", "issued_at", "origin"} and meta["origin"] == "code" and meta["issued_at"] == int(clock.now)
    assert len(meta["family"]) == 16
    assert provider.auth_codes == {}  # the code is used up
    line = [m for m in messages(caplog, name="personal-auth") if m.startswith("token outcome=issued")]
    assert line == [f"token outcome=issued client={client.client_id[:8]} family={meta['family'][:8]}"]


async def test_the_first_sign_in_is_saved_in_the_version_2_format(state_dir, make_provider):
    provider = make_provider(state_dir)
    client = await register(provider)
    tokens = await sign_in(provider, client)
    document = on_disk(state_dir)
    assert set(document) == {"version", "clients", "access_tokens", "refresh_tokens", "a2r", "r2a", "refresh_meta", "retired"}
    assert document["version"] == 2
    assert document["access_tokens"][tokens.access_token]["client_id"] == client.client_id
    assert document["refresh_tokens"][tokens.refresh_token]["expires_at"] is None
    assert document["a2r"] == {tokens.access_token: tokens.refresh_token} and document["r2a"] == {tokens.refresh_token: tokens.access_token}
    assert document["refresh_meta"][tokens.refresh_token]["origin"] == "code" and document["retired"] == {}
    assert document["clients"][client.client_id]["client_secret"] == client.client_secret


async def test_an_authorization_code_works_once(state_dir, make_provider):
    provider = make_provider(state_dir)
    client = await register(provider)
    code = mint_code(provider, client)
    await provider.exchange_authorization_code(client, code)
    before = state_fingerprint(provider)
    with pytest.raises(TokenError) as info:
        await provider.exchange_authorization_code(client, code)
    assert info.value.error == "invalid_grant"
    assert state_fingerprint(provider) == before


async def test_a_client_without_an_id_is_refused_and_the_code_is_not_used_up(state_dir, make_provider):
    provider = make_provider(state_dir)
    client = await register(provider)
    code = mint_code(provider, client)
    with pytest.raises(TokenError) as info:
        await provider.exchange_authorization_code(OAuthClientInformationFull(redirect_uris=[AnyUrl(REDIRECT)]), code)
    assert info.value.error == "invalid_client" and "code-1" in provider.auth_codes


async def test_each_sign_in_is_its_own_family(state_dir, make_provider):
    provider = make_provider(state_dir)
    client = await register(provider)
    first = await sign_in(provider, client, code="c1")
    second = await sign_in(provider, client, code="c2")
    families = {provider._refresh_meta[t.refresh_token]["family"] for t in (first, second)}
    assert len(families) == 2


# --------------------------------------------------------------------------
# One issuer: refresh, rotation, tombstones
# --------------------------------------------------------------------------


async def test_a_refresh_rotates_the_pair_and_issues_a_one_hour_access_token(state_dir, make_provider, caplog):
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)
    client = await register(provider)
    first = await sign_in(provider, client, scopes=["mcp"])
    family = provider._refresh_meta[first.refresh_token]["family"]
    clock.advance(1000)

    second = await refresh(provider, client, first.refresh_token)

    assert second.expires_in == 3600 and second.scope == "mcp" and second.token_type == "Bearer"
    assert second.access_token.startswith("pat_") and second.refresh_token.startswith("prt_")
    assert provider.access_tokens[second.access_token].expires_at == int(clock.now + 3600)
    assert provider.refresh_tokens[second.refresh_token].expires_at is None
    assert set(provider.access_tokens) == {second.access_token} and set(provider.refresh_tokens) == {second.refresh_token}
    assert provider._access_to_refresh_map == {second.access_token: second.refresh_token}
    assert provider._refresh_to_access_map == {second.refresh_token: second.access_token}
    meta = provider._refresh_meta[second.refresh_token]
    assert meta == {"family": family, "issued_at": int(clock.now), "origin": "refresh"}
    assert list(provider._refresh_meta) == [second.refresh_token]
    digest = hashlib.sha256(first.refresh_token.encode()).hexdigest()
    assert provider._retired == {digest: {"family": family, "retired_at": int(clock.now)}}
    assert [m for m in messages(caplog, name="personal-auth") if "refreshed" in m] == [
        f"token outcome=refreshed client={client.client_id[:8]} family={family[:8]}"
    ]


async def test_the_refresh_is_saved_the_old_tokens_are_gone_from_the_file_and_only_a_digest_remains(state_dir, make_provider):
    provider = make_provider(state_dir)
    client = await register(provider)
    first = await sign_in(provider, client)
    second = await refresh(provider, client, first.refresh_token)
    text = (state_dir / STATE_FILE).read_text()
    document = json.loads(text)
    assert first.access_token not in text and first.refresh_token not in text
    assert set(document["access_tokens"]) == {second.access_token} and set(document["refresh_tokens"]) == {second.refresh_token}
    assert list(document["retired"]) == [hashlib.sha256(first.refresh_token.encode()).hexdigest()]
    assert document["refresh_meta"][second.refresh_token]["origin"] == "refresh"


async def test_a_chain_of_refreshes_keeps_one_family_and_one_live_pair(state_dir, make_provider):
    provider = make_provider(state_dir)
    client = await register(provider)
    current = await sign_in(provider, client)
    family = provider._refresh_meta[current.refresh_token]["family"]
    retired = []
    for step in range(1, 5):
        retired.append(current.refresh_token)
        current = await refresh(provider, client, current.refresh_token)
        assert len(provider.access_tokens) == 1 and len(provider.refresh_tokens) == 1
        assert provider._refresh_meta[current.refresh_token]["family"] == family
        assert len(provider._retired) == step
    for old in retired:
        assert await provider.load_refresh_token(client, old) is None
    assert (await provider.load_refresh_token(client, current.refresh_token)).token == current.refresh_token


async def test_a_refresh_may_ask_for_fewer_scopes_but_not_more(state_dir, make_provider):
    provider = make_provider(state_dir)
    client = await register(provider)
    first = await sign_in(provider, client, scopes=["a", "b"])
    loaded = await provider.load_refresh_token(client, first.refresh_token)
    before = state_fingerprint(provider)
    with pytest.raises(TokenError) as info:
        await provider.exchange_refresh_token(client, loaded, ["a", "c"])
    assert info.value.error == "invalid_scope" and state_fingerprint(provider) == before
    narrowed = await provider.exchange_refresh_token(client, loaded, ["a"])
    assert narrowed.scope == "a" and provider.refresh_tokens[narrowed.refresh_token].scopes == ["a"]


async def test_exchange_refresh_token_works_when_called_with_the_stored_object_as_the_overview_test_does(state_dir, make_provider):
    provider = make_provider(state_dir)
    client = await register(provider)
    first = await sign_in(provider, client)
    refreshed = await provider.exchange_refresh_token(client, provider.refresh_tokens[first.refresh_token], [])
    assert refreshed.expires_in == 3600 and first.refresh_token not in provider.refresh_tokens


async def test_exchange_refuses_a_token_that_is_not_live_and_changes_nothing(state_dir, make_provider, caplog):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await register(provider)
    first = await sign_in(provider, client)
    stale = provider.refresh_tokens[first.refresh_token].model_copy()
    await refresh(provider, client, first.refresh_token)
    before = state_fingerprint(provider)
    with pytest.raises(TokenError) as info:
        await provider.exchange_refresh_token(client, stale, [])
    assert info.value.error == "invalid_grant" and state_fingerprint(provider) == before
    assert any("refresh_reuse_in_grace" in m for m in messages(caplog, name="personal-auth"))  # it was a rotated token


async def test_exchange_checks_the_stored_token_not_the_copy_the_caller_holds(state_dir, make_provider):
    provider = make_provider(state_dir)
    client = await register(provider)
    first = await sign_in(provider, client, scopes=["a"])
    broader = provider.refresh_tokens[first.refresh_token].model_copy(update={"scopes": ["a", "b"]})
    before = state_fingerprint(provider)
    with pytest.raises(TokenError) as info:
        await provider.exchange_refresh_token(client, broader, ["b"])  # the stored token only has scope a
    assert info.value.error == "invalid_scope" and state_fingerprint(provider) == before
    expired_copy = provider.refresh_tokens[first.refresh_token]
    expired_copy.expires_at = int(time.time()) - 5  # an expired token is not exchanged, whoever calls
    with pytest.raises(TokenError, match="expired") as info:
        await provider.exchange_refresh_token(client, expired_copy, [])
    assert info.value.error == "invalid_grant"
    assert first.refresh_token in provider.refresh_tokens and provider._retired == {}


async def test_a_refresh_token_of_another_client_is_not_found_and_is_not_a_reuse(state_dir, make_provider, caplog):
    capture(caplog)
    provider = make_provider(state_dir)
    one, two = await register(provider, 1), await register(provider, 2)
    first = await sign_in(provider, one)
    before = state_fingerprint(provider)
    assert await provider.load_refresh_token(two, first.refresh_token) is None
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(two, provider.refresh_tokens[first.refresh_token], [])
    assert state_fingerprint(provider) == before
    assert not [m for m in messages(caplog, name="personal-auth") if "reuse" in m]


# --------------------------------------------------------------------------
# an expired access token must not take anything else with it
# --------------------------------------------------------------------------


async def test_an_expired_access_token_is_refused_and_nothing_else_changes(state_dir, make_provider, caplog):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await register(provider)
    tokens = await sign_in(provider, client)
    provider.access_tokens[tokens.access_token].expires_at = int(time.time()) - 5
    fingerprint = state_fingerprint(provider)
    file_before = (state_dir / STATE_FILE).read_bytes()
    caplog.clear()

    assert await provider.load_access_token(tokens.access_token) is None
    assert await provider.verify_token(tokens.access_token) is None  # the route FastMCP's middleware takes

    assert state_fingerprint(provider) == fingerprint  # the access token, the refresh token, the maps, the metadata
    assert tokens.refresh_token in provider.refresh_tokens and tokens.access_token in provider.access_tokens
    assert (state_dir / STATE_FILE).read_bytes() == file_before  # and no write happened
    assert messages(caplog, name="personal-auth") == [f"access outcome=expired client={client.client_id[:8]}"] * 2
    assert tokens.access_token not in caplog.text


async def test_the_expiry_edge_a_token_is_valid_up_to_the_second_it_expires(state_dir, make_provider):
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)
    client = await register(provider)
    tokens = await sign_in(provider, client)
    expires_at = provider.access_tokens[tokens.access_token].expires_at
    clock.now = expires_at
    assert await provider.load_access_token(tokens.access_token) is not None
    clock.now = expires_at + 1
    assert await provider.load_access_token(tokens.access_token) is None


async def test_an_access_token_without_an_expiry_never_expires(state_dir, make_provider):
    provider = make_provider(state_dir)
    client = await register(provider)
    tokens = await sign_in(provider, client)
    provider.access_tokens[tokens.access_token].expires_at = None
    assert await provider.load_access_token(tokens.access_token) is not None


async def test_an_expired_refresh_token_is_refused_and_nothing_is_deleted(state_dir, make_provider):
    provider = make_provider(state_dir)
    client = await register(provider)
    tokens = await sign_in(provider, client)
    provider.refresh_tokens[tokens.refresh_token].expires_at = int(time.time()) - 5
    fingerprint = state_fingerprint(provider)
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None
    assert state_fingerprint(provider) == fingerprint
    assert tokens.access_token in provider.access_tokens and tokens.refresh_token in provider.refresh_tokens


async def test_an_unknown_token_is_simply_refused_and_nothing_is_logged(state_dir, make_provider, caplog):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await register(provider)
    caplog.clear()
    assert await provider.load_access_token("pat_" + "0" * 64) is None
    assert await provider.load_refresh_token(client, "prt_" + "0" * 64) is None
    assert await provider.load_access_token("") is None
    assert messages(caplog, name="personal-auth") == []


async def test_regression_over_http_an_expired_access_token_gets_401_the_refresh_token_survives_and_the_refresh_works(
    state_dir, make_provider, serve, caplog
):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await register(provider)
    tokens = await sign_in(provider, client)
    async with serve(provider) as http:
        assert await mcp_status(http, tokens.access_token) == 200  # the token works while it is valid
        provider.access_tokens[tokens.access_token].expires_at = int(time.time()) - 5  # the clock passes its expiry
        assert await mcp_status(http, tokens.access_token) == 401
        assert await mcp_status(http, None) == 401

        # the framework would have deleted the refresh token here; it must be untouched
        assert tokens.refresh_token in provider.refresh_tokens
        assert provider._refresh_to_access_map[tokens.refresh_token] == tokens.access_token

        reply = await http_refresh(http, Session(client.client_id, client.client_secret, "", tokens.refresh_token, 0))
        assert reply.status_code == 200, reply.text
        body = reply.json()
        assert body["expires_in"] == 3600 and body["access_token"].startswith("pat_") and body["refresh_token"].startswith("prt_")
        assert await mcp_status(http, body["access_token"]) == 200  # the new access token works
        assert await mcp_status(http, tokens.access_token) == 401  # and the old one stays refused

        again = await http_refresh(http, Session(client.client_id, client.client_secret, "", body["refresh_token"], 0))
        assert again.status_code == 200  # the rotated token refreshes too: the session lives on
    document = on_disk(state_dir)
    assert tokens.refresh_token not in json.dumps(document) and body["refresh_token"] not in json.dumps(document)
    assert [m for m in messages(caplog, name="personal-auth") if m.startswith("access outcome=expired")]


async def test_an_expired_refresh_token_gets_401_invalid_grant_over_http(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await register(provider)
    tokens = await sign_in(provider, client)
    provider.refresh_tokens[tokens.refresh_token].expires_at = int(time.time()) - 5
    async with serve(provider) as http:
        reply = await http.post("/token", data=refresh_form(client.client_id, client.client_secret, tokens.refresh_token))
    assert reply.status_code == 401 and reply.json()["error"] == "invalid_grant"
    assert tokens.refresh_token in provider.refresh_tokens


# --------------------------------------------------------------------------
# Reuse of a rotated refresh token
# --------------------------------------------------------------------------


async def rotated(provider, client, clock, *, steps: int = 1):
    """Sign in and refresh `steps` times. Returns (the retired tokens, in order, the live tokens)."""
    current = await sign_in(provider, client)
    retired = []
    for _ in range(steps):
        retired.append(current.refresh_token)
        current = await refresh(provider, client, current.refresh_token)
    return retired, current


async def test_reuse_inside_the_grace_window_is_refused_and_logged_at_info_only(state_dir, make_provider, caplog):
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)
    client = await register(provider)
    (old,), live = await rotated(provider, client, clock)
    family = provider._refresh_meta[live.refresh_token]["family"]
    clock.advance(10)
    caplog.clear()

    assert await provider.load_refresh_token(client, old) is None

    assert [(r.levelno, r.getMessage()) for r in records(caplog, name="personal-auth")] == [
        (logging.INFO, f"token outcome=refresh_reuse_in_grace client={client.client_id[:8]} family={family[:8]} age=10s")
    ]
    assert (await provider.load_refresh_token(client, live.refresh_token)) is not None  # nothing was revoked
    assert live.access_token in provider.access_tokens


@pytest.mark.parametrize("policy", ["log", "revoke"])
async def test_the_grace_window_is_300_seconds_and_inclusive(state_dir, make_provider, caplog, policy):
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock, reuse_policy=policy)
    client = await register(provider)
    (old,), live = await rotated(provider, client, clock)
    clock.advance(300)
    caplog.clear()
    assert await provider.load_refresh_token(client, old) is None
    assert [r.levelno for r in records(caplog, name="personal-auth")] == [logging.INFO]
    assert live.refresh_token in provider.refresh_tokens  # in grace nothing is revoked under either policy
    clock.advance(1)
    caplog.clear()
    assert await provider.load_refresh_token(client, old) is None
    assert [r.levelno for r in records(caplog, name="personal-auth") if "reuse_detected" in r.getMessage()] == [logging.WARNING]


async def test_late_reuse_under_the_log_policy_is_a_warning_and_changes_nothing_else(state_dir, make_provider, caplog):
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)  # the default policy
    assert provider.reuse_policy == "log"
    client = await register(provider)
    (old,), live = await rotated(provider, client, clock)
    family = provider._refresh_meta[live.refresh_token]["family"]
    clock.advance(301)
    fingerprint = state_fingerprint(provider)
    file_before = (state_dir / STATE_FILE).read_bytes()
    caplog.clear()

    assert await provider.load_refresh_token(client, old) is None

    assert [(r.levelno, r.getMessage()) for r in records(caplog, name="personal-auth")] == [
        (
            logging.WARNING,
            f"token outcome=refresh_reuse_detected client={client.client_id[:8]} family={family[:8]} age=301s policy=log",
        )
    ]
    assert state_fingerprint(provider) == fingerprint and (state_dir / STATE_FILE).read_bytes() == file_before
    refreshed = await refresh(provider, client, live.refresh_token)  # the legitimate session carries on
    assert refreshed is not None and refreshed.expires_in == 3600


async def test_reuse_in_grace_under_the_revoke_policy_is_only_logged_and_late_reuse_revokes_the_family(
    state_dir, make_provider, caplog
):
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock, reuse_policy="revoke")
    client = await register(provider)
    other = await sign_in(provider, client, code="other")  # another family of the same client: must survive
    (old,), live = await rotated(provider, client, clock)
    family = provider._refresh_meta[live.refresh_token]["family"]

    clock.advance(60)
    assert await provider.load_refresh_token(client, old) is None
    assert live.refresh_token in provider.refresh_tokens and live.access_token in provider.access_tokens  # grace: kept

    clock.advance(300)
    caplog.clear()
    assert await provider.load_refresh_token(client, old) is None

    lines = [(r.levelno, r.getMessage()) for r in records(caplog, name="personal-auth")]
    assert lines == [
        (logging.WARNING, f"token outcome=refresh_reuse_detected client={client.client_id[:8]} family={family[:8]} age=360s policy=revoke"),
        (logging.WARNING, f"revoke outcome=revoked client={client.client_id[:8]} family={family[:8]} reason=reuse access=1 refresh=1"),
    ]
    assert live.refresh_token not in provider.refresh_tokens and live.access_token not in provider.access_tokens
    assert await provider.load_refresh_token(client, live.refresh_token) is None
    assert await provider.load_access_token(live.access_token) is None
    assert other.refresh_token in provider.refresh_tokens and other.access_token in provider.access_tokens  # untouched
    document = on_disk(state_dir)  # and it was saved
    assert live.refresh_token not in json.dumps(document) and live.access_token not in json.dumps(document)
    assert other.refresh_token in document["refresh_tokens"]
    assert hashlib.sha256(old.encode()).hexdigest() in document["retired"]  # the tombstone stays


async def test_a_second_late_reuse_after_the_revoke_is_still_a_warning_but_revokes_nothing_more(state_dir, make_provider, caplog):
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock, reuse_policy="revoke")
    client = await register(provider)
    (old,), live = await rotated(provider, client, clock)
    clock.advance(400)
    await provider.load_refresh_token(client, old)  # the first late reuse revokes the family
    assert live.refresh_token not in provider.refresh_tokens
    caplog.clear()
    assert await provider.load_refresh_token(client, old) is None
    (line,) = messages(caplog, name="personal-auth")  # one warning, and no second revoke line: nothing is left to revoke
    assert line.startswith("token outcome=refresh_reuse_detected") and "age=400s policy=revoke" in line


async def test_reuse_is_recognized_after_a_restart_because_the_tombstones_are_saved(state_dir, make_provider, caplog):
    capture(caplog)
    clock = FakeClock()
    first = make_provider(state_dir, clock=clock, reuse_policy="revoke")
    client = await register(first)
    (old,), live = await rotated(first, client, clock)
    first.close()
    clock.advance(1000)
    caplog.clear()

    second = make_provider(state_dir, clock=clock, reuse_policy="revoke")
    assert messages(caplog, name="personal-auth") == ["oauth state loaded format=v2 clients=1 access=1 refresh=1 pruned=0"]
    assert await second.load_refresh_token(client, old) is None
    assert [m for m in messages(caplog, name="personal-auth") if "refresh_reuse_detected" in m]
    assert live.refresh_token not in second.refresh_tokens  # revoked by the policy


async def test_a_token_that_was_never_issued_is_not_a_reuse(state_dir, make_provider, caplog):
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock, reuse_policy="revoke")
    client = await register(provider)
    await rotated(provider, client, clock)
    clock.advance(10_000)
    caplog.clear()
    assert await provider.load_refresh_token(client, "prt_" + "f" * 64) is None
    assert messages(caplog, name="personal-auth") == []


async def test_a_retired_token_presented_by_another_client_is_still_a_reuse(state_dir, make_provider, caplog):
    # The client that presents it is only known to be a registered one. What matters is that a token that was rotated
    # away is back, so it is logged with the family it belonged to and, under the revoke policy, that family is revoked.
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock, reuse_policy="revoke")
    owner, thief = await register(provider, 1), await register(provider, 2)
    (old,), live = await rotated(provider, owner, clock)
    family = provider._refresh_meta[live.refresh_token]["family"]
    clock.advance(1000)
    caplog.clear()
    assert await provider.load_refresh_token(thief, old) is None
    detected = [m for m in messages(caplog, name="personal-auth") if "refresh_reuse_detected" in m]
    assert detected == [f"token outcome=refresh_reuse_detected client={thief.client_id[:8]} family={family[:8]} age=1000s policy=revoke"]
    assert live.refresh_token not in provider.refresh_tokens


@pytest.mark.parametrize("policy, family_survives", [("log", True), ("revoke", False)])
async def test_reuse_over_http_is_refused_with_401_invalid_grant_under_both_policies(
    state_dir, make_provider, serve, policy, family_survives
):
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock, reuse_policy=policy)
    client = await register(provider)
    first = await sign_in(provider, client)
    async with serve(provider) as http:
        session = Session(client.client_id, client.client_secret, first.access_token, first.refresh_token, 0)
        rotated_reply = await http_refresh(http, session)
        assert rotated_reply.status_code == 200
        live = rotated_reply.json()

        in_grace = await http_refresh(http, session)  # the old token again, at once
        assert in_grace.status_code == 401 and in_grace.json()["error"] == "invalid_grant"
        assert await mcp_status(http, live["access_token"]) == 200  # in grace nothing is revoked

        clock.advance(301)
        late = await http_refresh(http, session)
        assert late.status_code == 401 and late.json()["error"] == "invalid_grant"
        assert (await mcp_status(http, live["access_token"]) == 200) is family_survives
        live_refresh = await http_refresh(http, session, live["refresh_token"])
        assert (live_refresh.status_code == 200) is family_survives


async def test_two_refreshes_of_the_same_token_at_the_same_time_one_wins_and_the_other_is_a_reuse_in_grace(
    state_dir, make_provider, serve, caplog
):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await register(provider)
    first = await sign_in(provider, client)
    session = Session(client.client_id, client.client_secret, first.access_token, first.refresh_token, 0)
    async with serve(provider) as http:
        replies = await asyncio.gather(http_refresh(http, session), http_refresh(http, session))
    assert sorted(r.status_code for r in replies) == [200, 401]
    assert [m for m in messages(caplog, name="personal-auth") if "refresh_reuse_in_grace" in m]
    assert len(provider.refresh_tokens) == 1 and len(provider.access_tokens) == 1  # one live pair, not two


# --------------------------------------------------------------------------
# /revoke
# --------------------------------------------------------------------------


async def test_the_metadata_document_advertises_the_revocation_endpoint(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    async with serve(provider) as http:
        reply = await http.get("/.well-known/oauth-authorization-server")
    assert reply.status_code == 200
    metadata = reply.json()
    assert metadata["revocation_endpoint"] == f"{BASE_URL}/revoke"
    assert metadata["revocation_endpoint_auth_methods_supported"] == ["client_secret_post", "client_secret_basic"]
    assert metadata["registration_endpoint"] == f"{BASE_URL}/register"  # registration is unchanged


async def test_the_revoke_route_is_mounted(state_dir, make_provider):
    provider = make_provider(state_dir)
    routes = {route.path: route for route in provider.get_routes("/mcp")}
    assert "/revoke" in routes and set(routes["/revoke"].methods) >= {"POST", "OPTIONS"}


@pytest.mark.parametrize("which, hint", [("access", None), ("access", "access_token"), ("refresh", None), ("refresh", "refresh_token"), ("access", "refresh_token")])
async def test_revoking_a_token_over_http_removes_its_pair_persists_it_and_logs_it(
    state_dir, make_provider, serve, caplog, which, hint
):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await register(provider)
    survivor = await sign_in(provider, client, code="survivor")  # another session of the same client
    tokens = await sign_in(provider, client, code="victim")
    family = provider._refresh_meta[tokens.refresh_token]["family"]
    presented = tokens.access_token if which == "access" else tokens.refresh_token
    form = {"token": presented, "client_id": client.client_id, "client_secret": client.client_secret}
    if hint:
        form["token_type_hint"] = hint
    caplog.clear()

    async with serve(provider) as http:
        reply = await http.post("/revoke", data=form)
        assert reply.status_code == 200 and reply.content == b""
        assert await mcp_status(http, tokens.access_token) == 401
        assert await mcp_status(http, survivor.access_token) == 200
        refused = await http.post("/token", data=refresh_form(client.client_id, client.client_secret, tokens.refresh_token))
        assert refused.status_code == 401

    assert tokens.access_token not in provider.access_tokens and tokens.refresh_token not in provider.refresh_tokens
    assert tokens.refresh_token not in provider._refresh_meta and tokens.refresh_token not in provider._refresh_to_access_map
    assert survivor.refresh_token in provider.refresh_tokens
    document = on_disk(state_dir)  # persisted
    assert tokens.access_token not in document["access_tokens"] and tokens.refresh_token not in document["refresh_tokens"]
    assert survivor.refresh_token in document["refresh_tokens"]
    assert tokens.refresh_token not in json.dumps(document) and tokens.access_token not in json.dumps(document)
    revoked = [m for m in messages(caplog, name="personal-auth") if m.startswith("revoke outcome=revoked")]
    assert revoked == [f"revoke outcome=revoked client={client.client_id[:8]} family={family[:8]} access=1 refresh=1"]
    assert presented not in caplog.text


async def test_revoking_an_unknown_or_already_revoked_token_answers_200_and_changes_nothing(state_dir, make_provider, serve, caplog):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await register(provider)
    tokens = await sign_in(provider, client)
    async with serve(provider) as http:
        first = {"token": tokens.refresh_token, "client_id": client.client_id, "client_secret": client.client_secret}
        assert (await http.post("/revoke", data=first)).status_code == 200
        before = state_fingerprint(provider)
        file_before = (state_dir / STATE_FILE).read_bytes()
        caplog.clear()
        for token in (tokens.refresh_token, tokens.access_token, "pat_" + "1" * 64, "garbage", " "):
            reply = await http.post("/revoke", data={**first, "token": token})
            assert reply.status_code == 200, token
    assert state_fingerprint(provider) == before and (state_dir / STATE_FILE).read_bytes() == file_before
    assert [m for m in messages(caplog, name="personal-auth") if m.startswith("revoke")] == []


async def test_a_client_cannot_revoke_the_token_of_another_client(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    owner, other = await register(provider, 1), await register(provider, 2)
    tokens = await sign_in(provider, owner)
    async with serve(provider) as http:
        for presented in (tokens.refresh_token, tokens.access_token):
            reply = await http.post(
                "/revoke", data={"token": presented, "client_id": other.client_id, "client_secret": other.client_secret}
            )
            assert reply.status_code == 200
        assert await mcp_status(http, tokens.access_token) == 200
    assert tokens.refresh_token in provider.refresh_tokens and tokens.access_token in provider.access_tokens


async def test_revoke_needs_the_client_secret(state_dir, make_provider, serve):
    provider = make_provider(state_dir)
    client = await register(provider)
    tokens = await sign_in(provider, client)
    async with serve(provider) as http:
        wrong = await http.post("/revoke", data={"token": tokens.refresh_token, "client_id": client.client_id, "client_secret": "wrong"})
        unknown = await http.post("/revoke", data={"token": tokens.refresh_token, "client_id": "nobody", "client_secret": "x"})
    assert wrong.status_code == 401 and unknown.status_code == 401
    assert tokens.refresh_token in provider.refresh_tokens


async def test_revoking_a_legacy_token_with_no_family_works_and_gives_it_one_first(state_dir, make_provider):
    provider = make_provider(state_dir)
    client = await register(provider)
    tokens = await sign_in(provider, client)
    del provider._refresh_meta[tokens.refresh_token]  # as if loaded from a file with no metadata and never touched
    await provider.revoke_token(provider.refresh_tokens[tokens.refresh_token])
    assert provider.refresh_tokens == {} and provider.access_tokens == {}
    assert provider._access_to_refresh_map == {} and provider._refresh_to_access_map == {}


async def test_revoking_an_access_token_that_has_no_refresh_token_removes_just_that_token(state_dir, make_provider):
    provider = make_provider(state_dir)
    client = await register(provider)
    keep = await sign_in(provider, client, code="keep")
    provider.access_tokens["pat_orphan"] = AccessToken(token="pat_orphan", client_id=client.client_id, scopes=[], expires_at=int(time.time()) + 100)
    await provider.revoke_token(provider.access_tokens["pat_orphan"])
    assert "pat_orphan" not in provider.access_tokens
    assert keep.access_token in provider.access_tokens and keep.refresh_token in provider.refresh_tokens


async def test_revoke_token_ignores_what_is_not_a_token(state_dir, make_provider):
    provider = make_provider(state_dir)
    before = state_fingerprint(provider)
    await provider.revoke_token(object())
    assert state_fingerprint(provider) == before


async def test_a_revocation_stands_in_memory_even_if_it_cannot_be_saved_and_the_failure_is_raised(state_dir, make_provider, monkeypatch):
    provider = make_provider(state_dir)
    client = await register(provider)
    tokens = await sign_in(provider, client)
    before = (state_dir / STATE_FILE).read_bytes()
    fail_saves(monkeypatch)
    with pytest.raises(oauth_store.StateFileError):
        await provider.revoke_token(provider.access_tokens[tokens.access_token])
    assert await provider.load_access_token(tokens.access_token) is None  # revoked in memory: the safe direction
    assert tokens.refresh_token not in provider.refresh_tokens
    monkeypatch.undo()
    assert (state_dir / STATE_FILE).read_bytes() == before  # the file still holds the old state
    await register(provider, 2)  # the next save writes the revocation
    assert tokens.refresh_token not in on_disk(state_dir)["refresh_tokens"]


# --------------------------------------------------------------------------
# /revoke only looks a token up: it is not a refresh, not a replay, not a session coming back
# --------------------------------------------------------------------------
#
# The SDK's revocation handler calls load_access_token and load_refresh_token with whatever token the caller sent, to find
# out whose it is. Reuse detection and the expiry line belong to /token and /mcp: on /revoke a client that cleans up a token
# it has already rotated away (or let expire) would otherwise be logged as a replay, and under the "revoke" policy lose its
# own live session.


def revoke_form(client, token: str, hint: str | None = None) -> dict:
    form = {"token": token, "client_id": client.client_id, "client_secret": client.client_secret}
    if hint:
        form["token_type_hint"] = hint
    return form


@pytest.mark.parametrize("policy", ["log", "revoke"])
async def test_revoking_a_refresh_token_that_was_rotated_away_logs_no_reuse_and_revokes_nothing_else(
    state_dir, make_provider, serve, caplog, policy
):
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock, reuse_policy=policy)
    client = await register(provider)
    other = await sign_in(provider, client, code="other")  # another family of the same client
    (old,), live = await rotated(provider, client, clock)
    clock.advance(400)  # past the grace window: for /token this would be a replay, and under "revoke" the end of the family
    caplog.clear()

    async with serve(provider) as http:
        for hint in (None, "refresh_token", "access_token"):  # the handler loads it as a refresh token whatever the hint says
            reply = await http.post("/revoke", data=revoke_form(client, old, hint))
            assert reply.status_code == 200 and reply.content == b"", hint
        assert await mcp_status(http, live.access_token) == 200  # nothing of the live family was revoked
        assert await mcp_status(http, other.access_token) == 200  # nor of the other one
        after_the_revocations = messages(caplog, name="personal-auth")
        again = await http_refresh(http, Session(client.client_id, client.client_secret, "", live.refresh_token, 0))
        assert again.status_code == 200  # and the live refresh token still refreshes

    assert after_the_revocations == []  # no reuse line (INFO or WARNING), no revoke line
    assert other.refresh_token in provider.refresh_tokens and other.access_token in provider.access_tokens


@pytest.mark.parametrize("policy", ["log", "revoke"])
async def test_a_revocation_of_a_rotated_token_changes_nothing_logs_nothing_and_writes_nothing(
    state_dir, make_provider, serve, caplog, policy
):
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock, reuse_policy=policy)
    client = await register(provider)
    (old,), live = await rotated(provider, client, clock)
    clock.advance(400)
    fingerprint = state_fingerprint(provider)
    file_before = (state_dir / STATE_FILE).read_bytes()
    caplog.clear()
    async with serve(provider) as http:
        reply = await http.post("/revoke", data=revoke_form(client, old, "refresh_token"))
    assert reply.status_code == 200
    assert state_fingerprint(provider) == fingerprint and (state_dir / STATE_FILE).read_bytes() == file_before
    assert messages(caplog, name="personal-auth") == []  # no reuse line, no revoke line, nothing
    assert live.refresh_token in provider.refresh_tokens and live.access_token in provider.access_tokens


@pytest.mark.parametrize("policy, family_survives", [("log", True), ("revoke", False)])
async def test_the_same_token_at_token_is_still_a_reuse_so_the_mark_is_for_revoke_only_and_ends_with_the_request(
    state_dir, make_provider, serve, caplog, policy, family_survives
):
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock, reuse_policy=policy)
    client = await register(provider)
    (old,), live = await rotated(provider, client, clock)
    family = provider._refresh_meta[live.refresh_token]["family"]
    clock.advance(400)
    caplog.clear()
    async with serve(provider) as http:
        assert (await http.post("/revoke", data=revoke_form(client, old))).status_code == 200
        assert messages(caplog, name="personal-auth") == []
        assert oauth_guard.revoking() is False  # the mark ended with that request
        late = await http_refresh(http, Session(client.client_id, client.client_secret, "", old, 0))
        assert late.status_code == 401 and late.json()["error"] == "invalid_grant"
        assert (await mcp_status(http, live.access_token) == 200) is family_survives
    expected = [
        (logging.WARNING, f"token outcome=refresh_reuse_detected client={client.client_id[:8]} family={family[:8]} age=400s policy={policy}")
    ]
    if not family_survives:
        expected.append((logging.WARNING, f"revoke outcome=revoked client={client.client_id[:8]} family={family[:8]} reason=reuse access=1 refresh=1"))
    assert [(r.levelno, r.getMessage()) for r in records(caplog, name="personal-auth")] == expected


@pytest.mark.parametrize("policy", ["log", "revoke"])
async def test_revoking_an_expired_access_token_logs_no_expiry_and_changes_nothing_while_presenting_it_to_mcp_still_does(
    state_dir, make_provider, serve, caplog, policy
):
    capture(caplog)
    provider = make_provider(state_dir, reuse_policy=policy)
    client = await register(provider)
    tokens = await sign_in(provider, client)
    provider.access_tokens[tokens.access_token].expires_at = int(time.time()) - 5
    fingerprint = state_fingerprint(provider)
    file_before = (state_dir / STATE_FILE).read_bytes()
    caplog.clear()
    async with serve(provider) as http:
        for hint in (None, "access_token", "refresh_token"):
            reply = await http.post("/revoke", data=revoke_form(client, tokens.access_token, hint))
            assert reply.status_code == 200, hint
        assert messages(caplog, name="personal-auth") == []  # no "access outcome=expired": nobody presented a session
        assert state_fingerprint(provider) == fingerprint and (state_dir / STATE_FILE).read_bytes() == file_before
        assert tokens.refresh_token in provider.refresh_tokens  # the refresh token of the session was not touched
        # the line is still the proof that the refresh check reads it as: the session came back with an expired token
        assert await mcp_status(http, tokens.access_token) == 401
        expiry = messages(caplog, name="personal-auth")
        assert expiry and set(expiry) == {f"access outcome=expired client={client.client_id[:8]}"}
        refreshed = await http_refresh(http, Session(client.client_id, client.client_secret, "", tokens.refresh_token, 0))
        assert refreshed.status_code == 200


@pytest.mark.parametrize("policy", ["log", "revoke"])
async def test_the_token_loaders_do_not_note_reuse_or_log_expiry_while_the_mark_is_on_and_do_again_when_it_is_off(
    state_dir, make_provider, caplog, policy
):
    # The provider's side of the mark, without HTTP: what the loaders do is decided by oauth_guard.revoking() alone.
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock, reuse_policy=policy)
    client = await register(provider)
    (old,), live = await rotated(provider, client, clock)
    expiring = await sign_in(provider, client, code="expiring")
    provider.access_tokens[expiring.access_token].expires_at = int(clock.now) - 5
    clock.advance(400)
    fingerprint = state_fingerprint(provider)
    caplog.clear()

    mark = oauth_guard.REVOKING.set(True)
    try:
        assert await provider.load_refresh_token(client, old) is None
        assert await provider.load_access_token(expiring.access_token) is None
        provider._note_reuse(client, old)  # the one place reuse is decided: it does nothing at all while the mark is on
    finally:
        oauth_guard.REVOKING.reset(mark)
    assert messages(caplog, name="personal-auth") == [] and state_fingerprint(provider) == fingerprint

    assert await provider.load_access_token(expiring.access_token) is None  # mark off: the expiry is logged again ...
    assert await provider.load_refresh_token(client, old) is None  # ... and so is the replay
    logged = messages(caplog, name="personal-auth")
    assert [m.split()[0:2] for m in logged[:2]] == [["access", "outcome=expired"], ["token", "outcome=refresh_reuse_detected"]]
    assert (live.refresh_token in provider.refresh_tokens) is (policy == "log")  # under "revoke" the family is gone now


# --------------------------------------------------------------------------
# A save that fails must not leave memory and file telling different stories
# --------------------------------------------------------------------------


async def test_a_failed_save_during_a_sign_in_undoes_it_and_the_code_can_be_used_again(
    state_dir, make_provider, monkeypatch, caplog
):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await register(provider)
    code = mint_code(provider, client)
    before_file = (state_dir / STATE_FILE).read_bytes()
    before = state_fingerprint(provider)
    fail_saves(monkeypatch)

    with pytest.raises(oauth_store.StateFileError):
        await provider.exchange_authorization_code(client, code)

    assert state_fingerprint(provider) == before  # no token, and the code is still there
    assert (state_dir / STATE_FILE).read_bytes() == before_file
    assert sorted(os.listdir(state_dir)) == sorted([STATE_FILE, oauth_store.LOCK_FILE_NAME])
    assert not [m for m in messages(caplog, name="personal-auth") if m.startswith("token outcome=issued")]
    monkeypatch.undo()
    tokens = await provider.exchange_authorization_code(client, code)  # the client retries with the same code
    assert tokens.access_token in on_disk(state_dir)["access_tokens"]


async def test_a_failed_save_during_a_refresh_keeps_the_old_refresh_token_usable(state_dir, make_provider, monkeypatch, caplog):
    capture(caplog)
    provider = make_provider(state_dir)
    client = await register(provider)
    first = await sign_in(provider, client)
    before_file = (state_dir / STATE_FILE).read_bytes()
    before = state_fingerprint(provider)
    fail_saves(monkeypatch)

    with pytest.raises(oauth_store.StateFileError):
        await refresh(provider, client, first.refresh_token)

    assert state_fingerprint(provider) == before and provider._retired == {}
    assert (state_dir / STATE_FILE).read_bytes() == before_file
    assert not [m for m in messages(caplog, name="personal-auth") if "refreshed" in m]
    monkeypatch.undo()
    assert (await refresh(provider, client, first.refresh_token)) is not None  # no false reuse: it was never rotated


# --------------------------------------------------------------------------
# Pruning at every save
# --------------------------------------------------------------------------


async def test_every_save_prunes_tombstones_older_than_90_days_and_tokens_expired_for_over_a_week(state_dir, make_provider):
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)
    client = await register(provider)
    idle = await sign_in(provider, client, code="idle")  # a session that is never used again
    (old,), live = await rotated(provider, client, clock)
    old_digest = hashlib.sha256(old.encode()).hexdigest()
    assert old_digest in on_disk(state_dir)["retired"]

    clock.advance(89 * DAY)
    await sign_in(provider, client, code="c89")  # any save
    assert old_digest in on_disk(state_dir)["retired"]  # 89 days: kept
    assert idle.access_token not in provider.access_tokens  # its 30 days ended 59 days ago: long past the week
    assert idle.refresh_token in provider.refresh_tokens  # a refresh token never expires in this release

    clock.advance(1 * DAY + 1)
    await sign_in(provider, client, code="c90")
    assert old_digest not in on_disk(state_dir)["retired"]  # more than 90 days: gone


async def test_a_token_that_expired_less_than_a_week_ago_is_kept_so_the_expiry_can_still_be_logged(state_dir, make_provider, caplog):
    capture(caplog)
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)
    client = await register(provider)
    tokens = await sign_in(provider, client)
    clock.advance(30 * DAY + 3 * DAY)  # the access token expired three days ago
    await sign_in(provider, client, code="another")  # a save, which prunes
    assert tokens.access_token in provider.access_tokens and tokens.access_token in on_disk(state_dir)["access_tokens"]
    caplog.clear()
    assert await provider.load_access_token(tokens.access_token) is None
    assert messages(caplog, name="personal-auth") == [f"access outcome=expired client={client.client_id[:8]}"]
    assert tokens.refresh_token in provider.refresh_tokens  # and the session can still be refreshed
    assert (await refresh(provider, client, tokens.refresh_token)) is not None


async def test_a_client_that_holds_no_token_is_never_pruned_by_a_save(state_dir, make_provider):
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock)
    registered = await register(provider, 1)
    other = await register(provider, 2)
    clock.advance(400 * DAY)
    await sign_in(provider, other)
    assert registered.client_id in provider.clients and registered.client_id in on_disk(state_dir)["clients"]


async def test_the_prune_at_load_drops_old_expired_access_tokens_from_memory_but_not_from_the_file(state_dir, make_provider, caplog):
    capture(caplog)
    state_dir.mkdir()
    builder = LegacyFile()
    (state_dir / STATE_FILE).write_text(json.dumps(builder.document(), indent=2))
    before = (state_dir / STATE_FILE).read_bytes()
    provider = make_provider(state_dir)
    assert messages(caplog, name="personal-auth") == ["oauth state loaded format=v1 clients=4 access=3 refresh=3 pruned=2"]
    assert set(provider.access_tokens) == {builder.live_access}  # the two expired access tokens are long expired
    assert len(provider.refresh_tokens) == 3  # their refresh tokens are not
    assert (state_dir / STATE_FILE).read_bytes() == before  # the file waits for the next save


# --------------------------------------------------------------------------
# The compatibility tests: the state file of the previous release in, the state file of this release out, and back
# --------------------------------------------------------------------------


class LegacyFile:
    """A realistic version 1 file: a live session (client "Claude"), old pairs whose access tokens expired
    long ago and whose refresh tokens never expire, and clients that never got a token. Built relative to the real clock
    because the framework checks expiry against it."""

    def __init__(self, now: float | None = None) -> None:
        self.now = int(time.time() if now is None else now)
        self.live_client = "0a1b2c3d-0000-4000-8000-000000000001"
        self.live_access = "pat_" + "a" * 64
        self.live_refresh = "prt_" + "b" * 64
        self.old: list[tuple[str, str, str]] = []
        clients, access, refresh, a2r, r2a = {}, {}, {}, {}, {}

        def add_client(client_id: str, name: str) -> None:
            clients[client_id] = {
                "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "scope": None,
                "client_name": name, "client_uri": None, "logo_uri": None, "contacts": None, "tos_uri": None,
                "policy_uri": None, "jwks_uri": None, "jwks": None, "software_id": None, "software_version": None,
                "client_id": client_id, "client_secret": f"secret-of-{client_id[:8]}", "client_id_issued_at": self.now - 17 * DAY,
                "client_secret_expires_at": None,
            }

        add_client(self.live_client, "Claude")
        access[self.live_access] = {
            "token": self.live_access, "client_id": self.live_client, "scopes": [], "expires_at": self.now + 13 * DAY, "resource": None,
        }
        refresh[self.live_refresh] = {"token": self.live_refresh, "client_id": self.live_client, "scopes": [], "expires_at": None}
        a2r[self.live_access], r2a[self.live_refresh] = self.live_refresh, self.live_access
        for n in (2, 3):  # the two old sessions of a smaller version of the file
            client_id = f"0000000{n}-0000-4000-8000-00000000000{n}"
            add_client(client_id, "Claude")
            pat, prt = f"pat_{n}" + "c" * 62, f"prt_{n}" + "d" * 62
            access[pat] = {"token": pat, "client_id": client_id, "scopes": [], "expires_at": self.now - 150 * DAY, "resource": None}
            refresh[prt] = {"token": prt, "client_id": client_id, "scopes": [], "expires_at": None}
            a2r[pat], r2a[prt] = prt, pat
            self.old.append((client_id, pat, prt))
        add_client("00000004-0000-4000-8000-000000000004", "test")  # a client that never got a token
        self.parts = {"clients": clients, "access_tokens": access, "refresh_tokens": refresh, "a2r": a2r, "r2a": r2a}

    def document(self) -> dict:
        return copy.deepcopy(self.parts)


def the_file_of_previous_release(builder: "LegacyFile", state_dir: Path) -> bytes:
    state_dir.mkdir(parents=True, exist_ok=True)
    data = json.dumps(builder.document(), indent=2).encode()
    (state_dir / STATE_FILE).write_bytes(data)
    return data


async def test_the_legacy_provider_is_what_the_previous_release_runs_and_has_the_old_behavior(state_dir, make_provider):
    # Without this the compatibility tests below would prove nothing: the frozen copy must still be the old code.
    legacy = make_provider(state_dir, legacy=True)
    client = await register(legacy)
    tokens = await sign_in(legacy, client)
    legacy.access_tokens[tokens.access_token].expires_at = int(time.time()) - 5
    assert await legacy.load_access_token(tokens.access_token) is None
    assert tokens.refresh_token not in legacy.refresh_tokens  # the framework deleted the refresh token
    assert not hasattr(legacy, "close") and not hasattr(legacy, "reuse_policy")


async def test_the_legacy_provider_writes_a_version_1_file_through_the_real_flows(state_dir, make_provider, serve):
    legacy = make_provider(state_dir, legacy=True)
    async with serve(legacy) as http:
        first = await http_sign_in(http)  # register, consent page with the password, code exchange
        assert first.access_token.startswith("pat_") and first.refresh_token.startswith("prt_")
        assert first.expires_in == 30 * DAY
        second = await http_sign_in(http)
        refreshed = await http_refresh(http, second)  # the framework's refresh in the legacy code
        assert refreshed.status_code == 200
    document = on_disk(state_dir)
    assert set(document) == {"clients", "access_tokens", "refresh_tokens", "a2r", "r2a"}  # version 1: five keys
    assert len(document["clients"]) == 2
    assert first.access_token in document["access_tokens"] and first.refresh_token in document["refresh_tokens"]
    assert any(token.startswith("test_access_token_") for token in document["access_tokens"])
    assert any(token.startswith("test_refresh_token_") for token in document["refresh_tokens"])
    assert document["a2r"][first.access_token] == first.refresh_token and document["r2a"][first.refresh_token] == first.access_token


async def test_this_release_loads_the_file_the_legacy_code_wrote_verifies_its_pat_and_refreshes_its_prt_and_its_test_tokens(
    state_dir, make_provider, serve, caplog
):
    capture(caplog)
    legacy = make_provider(state_dir, legacy=True)
    async with serve(legacy) as http:
        signed_in = await http_sign_in(http)  # pat_ and prt_ from a sign-in
        other = await http_sign_in(http)
        framework = await http_refresh(http, other)  # test_access_token_ and test_refresh_token_ from the framework
        assert framework.status_code == 200
        framework_tokens = framework.json()
    del legacy
    file_before = (state_dir / STATE_FILE).read_bytes()
    assert set(json.loads(file_before)) == {"clients", "access_tokens", "refresh_tokens", "a2r", "r2a"}
    caplog.clear()

    current = make_provider(state_dir)
    assert messages(caplog, name="personal-auth") == ["oauth state loaded format=v1 clients=2 access=2 refresh=2 pruned=0"]
    assert (state_dir / STATE_FILE).read_bytes() == file_before  # loading did not touch it
    assert framework_tokens["access_token"].startswith("test_access_token_")

    async with serve(current) as http:
        assert await mcp_status(http, signed_in.access_token) == 200  # the pat_ token verifies
        assert await mcp_status(http, framework_tokens["access_token"]) == 200  # so does a framework token
        rotated_reply = await http_refresh(http, signed_in)  # the prt_ token refreshes
        assert rotated_reply.status_code == 200, rotated_reply.text
        new = rotated_reply.json()
        assert new["expires_in"] == 3600 and new["access_token"].startswith("pat_") and new["refresh_token"].startswith("prt_")
        assert await mcp_status(http, new["access_token"]) == 200
        assert await mcp_status(http, signed_in.access_token) == 401  # rotated away with its pair
        framework_session = Session(other.client_id, other.client_secret, "", framework_tokens["refresh_token"], 0)
        again = await http_refresh(http, framework_session)  # a test_refresh_token_ refreshes too
        assert again.status_code == 200 and again.json()["access_token"].startswith("pat_")
        old_prt_again = await http_refresh(http, signed_in)
        assert old_prt_again.status_code == 401  # the rotated prt_ is refused

    document = on_disk(state_dir)  # the first write turned the file into version 2
    assert document["version"] == 2 and set(document) == {"version", "clients", "access_tokens", "refresh_tokens", "a2r", "r2a", "refresh_meta", "retired"}
    assert len(document["retired"]) == 2 and len(document["clients"]) == 2
    assert {meta["origin"] for meta in document["refresh_meta"].values()} == {"refresh"}  # both rotated tokens are this release's
    assert signed_in.refresh_token not in json.dumps(document)


async def test_the_legacy_code_loads_the_version_2_file_with_the_same_clients_tokens_and_maps(state_dir, make_provider, serve):
    current = make_provider(state_dir)
    async with serve(current) as http:
        one = await http_sign_in(http)
        two = await http_sign_in(http)
        refreshed = await http_refresh(http, two)  # rotation: a tombstone, a family of origin "refresh"
        assert refreshed.status_code == 200
        live_two = refreshed.json()
    document = on_disk(state_dir)
    assert document["version"] == 2 and len(document["retired"]) == 1
    current.close()

    legacy = make_provider(state_dir, legacy=True)  # a rollback to the previous release

    assert set(legacy.clients) == set(current.clients) and len(legacy.clients) == 2
    for client_id, record in current.clients.items():
        assert legacy.clients[client_id].model_dump(mode="json") == record.model_dump(mode="json")
        assert legacy.clients[client_id].client_secret == record.client_secret
    assert {k: v.model_dump(mode="json") for k, v in legacy.access_tokens.items()} == {
        k: v.model_dump(mode="json") for k, v in current.access_tokens.items()
    }
    assert {k: v.model_dump(mode="json") for k, v in legacy.refresh_tokens.items()} == {
        k: v.model_dump(mode="json") for k, v in current.refresh_tokens.items()
    }
    assert legacy._access_to_refresh_map == current._access_to_refresh_map
    assert legacy._refresh_to_access_map == current._refresh_to_access_map
    assert set(legacy.access_tokens) == {one.access_token, live_two["access_token"]}

    # and the old code can serve the sessions the new code issued
    async with serve(legacy) as http:
        assert await mcp_status(http, one.access_token) == 200
        assert await mcp_status(http, live_two["access_token"]) == 200
        again = await http_refresh(http, two, live_two["refresh_token"])  # the legacy refresh with a prt_ token
        assert again.status_code == 200 and again.json()["access_token"].startswith("test_access_token_")


async def test_a_rollback_and_a_roll_forward_lose_nothing(state_dir, make_provider, serve):
    # this release writes version 2, the previous release loads it and saves version 1 (dropping the tombstones), this release loads that: still one session.
    current = make_provider(state_dir)
    async with serve(current) as http:
        session = await http_sign_in(http)
        rotated_reply = await http_refresh(http, session)
        live = rotated_reply.json()
    current.close()

    legacy = make_provider(state_dir, legacy=True)
    legacy._save_state()
    assert set(on_disk(state_dir)) == {"clients", "access_tokens", "refresh_tokens", "a2r", "r2a"}

    del legacy
    forward = make_provider(state_dir)
    assert set(forward.refresh_tokens) == {live["refresh_token"]} and set(forward.access_tokens) == {live["access_token"]}
    assert forward._refresh_meta[live["refresh_token"]]["origin"] == "legacy"  # the family was forgotten, the session was not
    async with serve(forward) as http:
        assert await mcp_status(http, live["access_token"]) == 200
        assert (await http_refresh(http, session, live["refresh_token"])).status_code == 200


async def test_the_owners_session_survives_the_upgrade_a_file_shaped_like_the_live_one(state_dir, make_provider, serve, caplog):
    capture(caplog)
    builder = LegacyFile()
    original = the_file_of_previous_release(builder, state_dir)
    caplog.clear()

    provider = make_provider(state_dir)

    assert messages(caplog, name="personal-auth") == ["oauth state loaded format=v1 clients=4 access=3 refresh=3 pruned=2"]
    assert (state_dir / STATE_FILE).read_bytes() == original
    async with serve(provider) as http:
        assert await mcp_status(http, builder.live_access) == 200  # the live session verifies at once, with no sign-in
        for _, pat, prt in builder.old:
            assert await mcp_status(http, pat) == 401  # long expired
        session = Session(builder.live_client, f"secret-of-{builder.live_client[:8]}", builder.live_access, builder.live_refresh, 0)

        # the refresh test: the access token expires, the user opens a new conversation
        provider.access_tokens[builder.live_access].expires_at = int(time.time()) - 60
        caplog.clear()
        assert await mcp_status(http, builder.live_access) == 401
        assert messages(caplog, name="personal-auth") == [f"access outcome=expired client={builder.live_client[:8]}"]
        reply = await http_refresh(http, session)
        assert reply.status_code == 200, reply.text
        assert [m for m in messages(caplog, name="personal-auth") if m.startswith("token outcome=refreshed")]
        assert await mcp_status(http, reply.json()["access_token"]) == 200

    document = on_disk(state_dir)
    assert document["version"] == 2 and len(document["clients"]) == 4  # every client is still there
    assert len(document["refresh_tokens"]) == 3  # the old sessions' refresh tokens are untouched
    assert all(prt in document["refresh_tokens"] for _, _, prt in builder.old)
    assert len(document["access_tokens"]) == 1  # the two expired access tokens were pruned by the save
    assert set(document["refresh_meta"]) == set(document["refresh_tokens"])
    legacy_meta = {t: m for t, m in document["refresh_meta"].items() if m["origin"] == "legacy"}
    assert set(legacy_meta) == {prt for _, _, prt in builder.old}  # the migration was saved with the first write
    # an old session can still refresh with its refresh token: nothing about it changed in this release
    old_client, _, old_prt = builder.old[0]
    async with serve(provider) as http:
        old = await http_refresh(http, Session(old_client, f"secret-of-{old_client[:8]}", "", old_prt, 0))
        assert old.status_code == 200


async def test_the_pair_the_legacy_code_issued_earlier_gets_an_issue_time_of_about_30_days_before_its_access_token_expired(
    state_dir, make_provider
):
    builder = LegacyFile()
    the_file_of_previous_release(builder, state_dir)
    provider = make_provider(state_dir)
    for _, _, prt in builder.old:
        assert provider._refresh_meta[prt]["issued_at"] == builder.now - 150 * DAY - 30 * DAY
        assert provider._refresh_meta[prt]["origin"] == "legacy"
    assert provider._refresh_meta[builder.live_refresh]["issued_at"] == builder.now + 13 * DAY - 30 * DAY  # 17 days ago


async def test_the_whole_real_flow_works_end_to_end_on_this_release(state_dir, make_provider, serve):
    # one smoke test of register, consent, code exchange, refresh, expiry and revoke through the real endpoints
    provider = make_provider(state_dir)
    async with serve(provider) as http:
        session = await http_sign_in(http)
        assert await mcp_status(http, session.access_token) == 200
        rotated_reply = await http_refresh(http, session)
        assert rotated_reply.status_code == 200
        live = rotated_reply.json()
        revoked = await http.post(
            "/revoke", data={"token": live["refresh_token"], "client_id": session.client_id, "client_secret": session.client_secret}
        )
        assert revoked.status_code == 200
        assert await mcp_status(http, live["access_token"]) == 401
        assert (await http_refresh(http, session, live["refresh_token"])).status_code == 401
    assert on_disk(state_dir)["access_tokens"] == {} and on_disk(state_dir)["refresh_tokens"] == {}


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------


def test_the_log_id_helper_shows_at_most_8_safe_characters():
    assert personal_auth._id8("0a1b2c3d-0000-4000") == "0a1b2c3d"
    assert personal_auth._id8("abc") == "abc"
    assert personal_auth._id8("") == "-" and personal_auth._id8(None) == "-"
    assert personal_auth._id8("evil\nforged") == "evil?for"
    assert personal_auth._id8("a b<c>d&e") == "a?b?c?d?"
    assert personal_auth._id8("éè́ab") == "???ab"
    assert personal_auth._id8("x" * 100) == "xxxxxxxx"
    assert personal_auth._id8(12345678901) == "12345678"


async def test_the_logs_of_a_whole_session_carry_no_token_secret_password_or_newline(state_dir, make_provider, serve, caplog):
    caplog.set_level(logging.INFO)  # everything, whatever the logger
    clock = FakeClock()
    provider = make_provider(state_dir, clock=clock, reuse_policy="revoke")
    async with serve(provider) as http:
        session = await http_sign_in(http)  # register, consent page, code exchange
        issued = [session.access_token, session.refresh_token]
        step = await http_refresh(http, session)  # rotation
        assert step.status_code == 200
        issued += [step.json()["access_token"], step.json()["refresh_token"]]
        provider.access_tokens[issued[2]].expires_at = int(time.time()) - 5
        assert await mcp_status(http, issued[2]) == 401  # an expired access token
        await http_refresh(http, session)  # the rotated token again, at once: reuse in grace
        clock.advance(400)
        await http_refresh(http, session)  # and later: reuse detected, and the family is revoked
        other = await http_sign_in(http)  # a second session, revoked on purpose
        issued += [other.access_token, other.refresh_token]
        revoked = await http.post(
            "/revoke", data={"token": other.refresh_token, "client_id": other.client_id, "client_secret": other.client_secret}
        )
        assert revoked.status_code == 200
        assert await mcp_status(http, "pat_" + "9" * 64) == 401  # an unknown token
    secrets_in_play = [
        *issued, PASSWORD, VERIFIER, hashlib.sha256(issued[1].encode()).hexdigest(),
        provider.clients[session.client_id].client_secret, provider.clients[other.client_id].client_secret,
    ]
    texts = [record.getMessage() for record in caplog.records]
    for expected in (
        "oauth state loaded", "token outcome=issued", "token outcome=refreshed", "access outcome=expired",
        "token outcome=refresh_reuse_in_grace", "token outcome=refresh_reuse_detected", "revoke outcome=revoked",
    ):
        assert any(expected in text for text in texts), f"the scenario no longer produces {expected!r}"
    for text in texts:
        assert "\n" not in text and "\r" not in text, text
        for secret in secrets_in_play:
            assert secret not in text, text


async def test_a_hostile_client_id_in_the_state_cannot_forge_a_log_line(state_dir, make_provider, caplog):
    capture(caplog)
    hostile = "evil\nINFO forged line=1\x1b[31m"
    state_dir.mkdir()
    builder = LegacyFile()
    document = builder.document()
    token = "pat_" + "e" * 64
    document["access_tokens"][token] = {"token": token, "client_id": hostile, "scopes": [], "expires_at": int(time.time()) - 5, "resource": None}
    (state_dir / STATE_FILE).write_text(json.dumps(document, indent=2))
    provider = make_provider(state_dir)
    caplog.clear()
    assert await provider.load_access_token(token) is None
    (line,) = messages(caplog, name="personal-auth")
    assert line == "access outcome=expired client=evil?INF"
    assert "\n" not in line and "\x1b" not in line


# --------------------------------------------------------------------------
# Hygiene of the sign-in files
# --------------------------------------------------------------------------


def test_personal_auth_py_holds_no_em_or_en_dash_and_no_non_ascii_text():
    text = (REPO_ROOT / "personal_auth.py").read_text(encoding="utf-8")
    assert EM_DASH not in text and EN_DASH not in text
    assert text.isascii()


def test_the_frozen_copy_is_r2_with_only_the_four_dashes_replaced():
    path = REPO_ROOT / "tests" / "legacy" / "personal_auth_previous.py"
    copy_text = path.read_text(encoding="utf-8")
    assert EM_DASH not in copy_text and EN_DASH not in copy_text and copy_text.isascii()
    # the digest of the file as it is pinned here: nobody edits the frozen copy by accident
    assert hashlib.sha256(copy_text.encode()).hexdigest() == "f094d339d4d57b4dcdb7d3aad15c9b32c273b2010e5fcde6059fbcb5b893f940"
    # putting the four dashes back gives exactly the previous release's personal_auth.py
    restored = copy_text
    for flattened in (
        "Claude Code - no external identity",
        "(interactive consent page) - by the time this method runs, the",
        "# POST - form submission from the consent page.",
        "Approved and password valid - clear counter and let the SDK",
    ):
        assert restored.count(flattened) == 1, flattened
        restored = restored.replace(flattened, flattened.replace(" - ", f" {EM_DASH} "))
    assert hashlib.sha256(restored.encode()).hexdigest() == "7f0fcf2c89820de48df1077472fcb91dc0d244840f678d69585ee9aae2123ea4"


def test_the_frozen_copy_is_a_separate_class_from_the_current_provider():
    assert personal_auth_previous.PersonalAuthProvider is not PersonalAuthProvider
    assert personal_auth_previous.__file__.endswith(os.path.join("tests", "legacy", "personal_auth_previous.py"))
