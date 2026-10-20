"""Client compatibility: ChatGPT and Microsoft Copilot Studio sign in to this server the way claude.ai does.

Each test replays, offline and in process, the OAuth flow one of those clients runs against the real login app (the
FastMCP HTTP app built with the real PersonalAuthProvider, driven through httpx.ASGITransport with a chosen client
address: no socket is opened, no password or token is real). The provider is built the way main.py builds it: from
Settings.from_env with MCP_EXTRA_REDIRECT_DOMAINS, handing settings.redirect_domains to PersonalAuthProvider.

The flow of a client: the 401 of /mcp with its resource_metadata address, the two metadata documents, dynamic client
registration (DCR) with the client's own redirect address, /authorize with PKCE S256, state and the RFC 8707 resource
parameter (the page, then the password form a browser would post), the 302 back to the client's host carrying code and
state, the token exchange with client_secret_post and resource, one authenticated MCP request, the refresh with resource
after the access token expired (time moved by a fake clock), and the refusals: no extra domain configured, look-alike
hosts, http. A step of the flow that fails here is a client incompatibility to fix in the login code, not a test to
loosen.

The client facts (redirect addresses, PKCE S256, the resource parameter, client_secret_post) are the ones OpenAI documents
at developers.openai.com/apps-sdk/build/auth and Microsoft documents on Microsoft Learn for Copilot Studio, as read on
2026-10-17. Nothing here was tried against the real services.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import oauth_guard
import oauth_store
import personal_auth
from personal_auth import PersonalAuthProvider
from server import build_server
from settings import Settings

pytestmark = pytest.mark.anyio

BASE_URL = "https://mcp.example.test"
MCP_URL = f"{BASE_URL}/mcp"
PASSWORD = "a-test-password-not-real"  # 24 characters: no short-password warning
OPERATOR_IP = "203.0.113.7"
STATE_FILE = oauth_store.STATE_FILE_NAME
HOUR = 3600

CALLBACK_ID = "Cb7x_Q2m-9K4v1"  # the per connector id ChatGPT puts in its redirect path (case, _ and - kept)
CONNECTOR = "shared-5fgorelo-20mcp-5f7a3c91d2e84b60"  # the connector name Copilot Studio puts in its redirect path
CHATGPT_CALLBACK = f"https://chatgpt.com/connector/oauth/{CALLBACK_ID}"
CHATGPT_STABLE = "https://chatgpt.com/connector_platform_oauth_redirect"
COPILOT_REDIRECT = f"https://global.consent.azure-apim.net/redirect/{CONNECTOR}"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
POWER_PLATFORM = "global.consent.azure-apim.net"

INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@dataclass(frozen=True)
class Profile:
    """One client: what it registers, what it appends to /authorize and /token, and the domains the operator sets."""

    name: str  # client_name in the registration body
    redirect: str
    extra: str  # MCP_EXTRA_REDIRECT_DOMAINS for a server that serves this client ("" is not set)
    resource: bool = True  # the client appends resource=<the MCP URL> to /authorize and /token
    auth_method: str = "client_secret_post"  # token_endpoint_auth_method in the registration body ("none": no secret)


CHATGPT = Profile("ChatGPT", CHATGPT_CALLBACK, "chatgpt.com")
CHATGPT_PLATFORM = Profile("ChatGPT", CHATGPT_STABLE, "chatgpt.com")
CHATGPT_PUBLIC = Profile("ChatGPT", CHATGPT_CALLBACK, "chatgpt.com", auth_method="none")  # if it registers as a public client
COPILOT = Profile("Microsoft Copilot Studio", COPILOT_REDIRECT, POWER_PLATFORM)
COPILOT_NO_RESOURCE = Profile("Microsoft Copilot Studio", COPILOT_REDIRECT, POWER_PLATFORM, resource=False)
CLAUDE = Profile("Claude", CLAUDE_REDIRECT, "", resource=False)  # the control: what the server always served
PROFILES = [
    pytest.param(CHATGPT, id="chatgpt-callback-id"),
    pytest.param(CHATGPT_PLATFORM, id="chatgpt-stable-redirect"),
    pytest.param(CHATGPT_PUBLIC, id="chatgpt-public-client"),
    pytest.param(COPILOT, id="copilot-studio"),
    pytest.param(COPILOT_NO_RESOURCE, id="copilot-studio-without-resource"),
    pytest.param(CLAUDE, id="claude-ai-control"),
]


# --------------------------------------------------------------------------
# The rig: settings, provider, real HTTP app
# --------------------------------------------------------------------------


class FakeClock:
    """Seconds since the epoch, moved by hand. It starts at the real time because the framework checks an authorization
    code and a bearer token against the real clock."""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def settings_for(extra: str | None) -> Settings:
    """The settings of a server whose operator set MCP_EXTRA_REDIRECT_DOMAINS to `extra` (None: not set)."""
    env = {"GORELO_API_KEY": "key-not-real", "PUBLIC_BASE_URL": BASE_URL, "MCP_AUTH_PASSWORD": PASSWORD}
    if extra is not None:
        env["MCP_EXTRA_REDIRECT_DOMAINS"] = extra
    return Settings.from_env(env)


@dataclass
class Rig:
    settings: Settings
    provider: PersonalAuthProvider
    http: httpx.AsyncClient
    clock: FakeClock
    state_dir: Path


@pytest.fixture
def open_rig(tmp_path, mock_gorelo, spec_index):
    """open_rig(extra, state_dir=None) -> async context manager yielding a Rig. The provider is built with the arguments
    main.py passes (base_url, password, allowed_redirect_domains, state_dir) plus the fake clock, and closed at the end."""

    @asynccontextmanager
    async def opening(extra: str | None, *, state_dir: Path | None = None):
        settings = settings_for(extra)
        state_dir = state_dir or tmp_path / "oauth-state"
        clock = FakeClock()
        provider = PersonalAuthProvider(
            base_url=settings.public_base_url,
            password=settings.mcp_auth_password,
            allowed_redirect_domains=settings.redirect_domains,
            state_dir=str(state_dir),
            clock=clock,
        )
        try:
            server = build_server(settings, auth=provider, transport=mock_gorelo.transport, spec=spec_index)
            app = server.http_app(json_response=True)
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, client=(OPERATOR_IP, 40000))
                async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
                    yield Rig(settings, provider, http, clock, state_dir)
        finally:
            provider.close()

    return opening


def capture(caplog) -> None:
    caplog.set_level(logging.INFO, logger="personal-auth")
    caplog.set_level(logging.INFO, logger="oauth-guard")


def journal(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name in ("personal-auth", "oauth-guard")]


# --------------------------------------------------------------------------
# The steps of a client
# --------------------------------------------------------------------------


def path_of(url: str) -> str:
    """The path and query of an address on this server (the address itself must be on it)."""
    parts = urlparse(url)
    assert f"{parts.scheme}://{parts.netloc}" == BASE_URL, url
    return parts.path + (f"?{parts.query}" if parts.query else "")


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def hidden_inputs(page: str) -> dict[str, str]:
    """The hidden fields of the consent page, as a browser posts them back."""
    found = re.findall(r'<input type="hidden" name="([^"]*)" value="([^"]*)">', page)
    return {html.unescape(name): html.unescape(value) for name, value in found}


@dataclass
class Discovery:
    resource: str
    issuer: str
    registration: str
    authorization: str
    token: str
    metadata: dict


async def discover(http: httpx.AsyncClient) -> Discovery:
    """What a client reads before it registers: the 401 of /mcp, the protected resource document it points to, and the
    authorization server document that one names."""
    refused = await http.post("/mcp", headers=MCP_HEADERS, json=INITIALIZE)
    assert refused.status_code == 401
    pointer = re.search(r'resource_metadata="([^"]+)"', refused.headers["www-authenticate"])
    assert pointer is not None, refused.headers["www-authenticate"]
    resource_doc = await http.get(path_of(pointer.group(1)))
    assert resource_doc.status_code == 200
    resource = resource_doc.json()
    (issuer,) = resource["authorization_servers"]
    server_doc = await http.get(f"{path_of(issuer).rstrip('/')}/.well-known/oauth-authorization-server")
    assert server_doc.status_code == 200
    metadata = server_doc.json()
    return Discovery(
        resource=resource["resource"], issuer=issuer, registration=metadata["registration_endpoint"],
        authorization=metadata["authorization_endpoint"], token=metadata["token_endpoint"], metadata=metadata,
    )


def registration_body(name: str, redirect: str, auth_method: str = "client_secret_post") -> dict:
    return {
        "client_name": name, "redirect_uris": [redirect], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": auth_method,
    }


async def register(http: httpx.AsyncClient, found: Discovery, profile: Profile) -> dict:
    reply = await http.post(
        path_of(found.registration), json=registration_body(profile.name, profile.redirect, profile.auth_method)
    )
    assert reply.status_code == 201, reply.text
    return reply.json()


@dataclass
class Session:
    client_id: str
    client_secret: str | None
    access_token: str
    refresh_token: str
    expires_in: int


async def sign_in(http: httpx.AsyncClient, found: Discovery, profile: Profile) -> Session:
    """Register, open the consent page, approve with the password, exchange the code: every response is checked."""
    client = await register(http, found, profile)
    assert client["token_endpoint_auth_method"] == profile.auth_method and client["redirect_uris"] == [profile.redirect]
    assert client["client_id"] and bool(client.get("client_secret")) == (profile.auth_method != "none")

    verifier, challenge = pkce()
    state = secrets.token_urlsafe(16)
    query = {
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": profile.redirect,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    }
    if profile.resource:
        query["resource"] = found.resource
    page = await http.get(path_of(found.authorization), params=query)  # what the browser opens
    assert page.status_code == 200 and "Authorize MCP Access" in page.text
    assert html.escape(profile.name) in page.text and html.escape(profile.redirect) in page.text
    form = hidden_inputs(page.text)
    assert form["state"] == state and form["redirect_uri"] == profile.redirect and form.get("resource") == query.get("resource")

    approved = await http.post(path_of(found.authorization), data={**form, "password": PASSWORD, "decision": "approve"})
    assert approved.status_code == 302, approved.text
    assert approved.headers["referrer-policy"] == "no-referrer" and approved.headers["cache-control"] == "no-store"
    back = urlparse(approved.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == profile.redirect  # back to the client's own host and path
    answer = parse_qs(back.query)
    assert answer["state"] == [state] and len(answer["code"]) == 1
    assert set(answer) == {"code", "state"}  # no iss, no error: the server does not advertise the iss parameter

    exchange = {
        "grant_type": "authorization_code", "code": answer["code"][0], "redirect_uri": profile.redirect,
        "client_id": client["client_id"], "code_verifier": verifier,
    }
    if client.get("client_secret"):
        exchange["client_secret"] = client["client_secret"]
    if profile.resource:
        exchange["resource"] = found.resource
    reply = await http.post(path_of(found.token), data=exchange)
    assert reply.status_code == 200, reply.text
    assert reply.headers["cache-control"] == "no-store"
    body = reply.json()
    assert body["token_type"] == "Bearer" and body["access_token"].startswith("pat_") and body["refresh_token"].startswith("prt_")
    return Session(
        client["client_id"], client.get("client_secret"), body["access_token"], body["refresh_token"], body["expires_in"]
    )


async def refresh(http: httpx.AsyncClient, found: Discovery, session: Session, profile: Profile, token: str | None = None):
    form = {
        "grant_type": "refresh_token", "refresh_token": token or session.refresh_token,
        "client_id": session.client_id,
    }
    if session.client_secret:
        form["client_secret"] = session.client_secret
    if profile.resource:
        form["resource"] = found.resource
    return await http.post(path_of(found.token), data=form)


async def mcp_post(http: httpx.AsyncClient, access_token: str | None, payload: dict, session_id: str | None = None):
    headers = dict(MCP_HEADERS)
    if access_token is not None:
        headers["Authorization"] = f"Bearer {access_token}"
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    return await http.post("/mcp", headers=headers, json=payload)


async def mcp_initialize(http: httpx.AsyncClient, access_token: str | None) -> httpx.Response:
    return await mcp_post(http, access_token, INITIALIZE)


async def list_tool_names(http: httpx.AsyncClient, access_token: str) -> list[str]:
    """initialize, notifications/initialized, tools/list: what a connected client does first."""
    opened = await mcp_initialize(http, access_token)
    assert opened.status_code == 200, opened.text
    session_id = opened.headers.get("mcp-session-id")
    ready = await mcp_post(http, access_token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id)
    assert ready.status_code == 202, ready.text
    listed = await mcp_post(http, access_token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session_id)
    assert listed.status_code == 200, listed.text
    return [tool["name"] for tool in listed.json()["result"]["tools"]]


# --------------------------------------------------------------------------
# What the clients read before they register
# --------------------------------------------------------------------------


async def test_the_authorization_server_document_is_what_chatgpt_needs(open_rig):
    async with open_rig(None) as rig:
        reply = await rig.http.get("/.well-known/oauth-authorization-server")
        assert reply.status_code == 200 and reply.headers["content-type"].startswith("application/json")
        doc = reply.json()
    assert "S256" in doc["code_challenge_methods_supported"] and "plain" not in doc["code_challenge_methods_supported"]
    assert doc["registration_endpoint"] == f"{BASE_URL}/register"  # ChatGPT registers itself (DCR)
    # Advertising either of these would change what ChatGPT does: client_id_metadata_document_supported makes it prefer a
    # metadata document over DCR, authorization_response_iss_parameter_supported moves it to its stable redirect address
    # and makes it expect iss in the answer. Both stay off, so ChatGPT registers and uses connector/oauth/<id>.
    assert not doc.get("client_id_metadata_document_supported")
    assert not doc.get("authorization_response_iss_parameter_supported")
    assert doc["authorization_endpoint"] == f"{BASE_URL}/authorize" and doc["token_endpoint"] == f"{BASE_URL}/token"
    assert doc["response_types_supported"] == ["code"]
    assert {"authorization_code", "refresh_token"} <= set(doc["grant_types_supported"])
    assert "client_secret_post" in doc["token_endpoint_auth_methods_supported"]
    assert "none" not in doc["token_endpoint_auth_methods_supported"]  # the metadata advertises secrets only; a client that registers with none gets no secret and signs in with PKCE and the password


async def test_the_protected_resource_document_names_the_mcp_url_the_clients_send_as_resource(open_rig):
    async with open_rig(None) as rig:
        found = await discover(rig.http)
        direct = await rig.http.get("/.well-known/oauth-protected-resource/mcp")
    assert direct.status_code == 200 and direct.json()["resource"] == MCP_URL == found.resource
    assert found.metadata["issuer"] == found.issuer and path_of(found.issuer) == "/"
    assert direct.json()["authorization_servers"] == [found.issuer]


async def test_a_request_without_a_token_and_one_with_an_unknown_token_both_point_to_the_resource_document(open_rig):
    async with open_rig(None) as rig:
        for token in (None, "pat_" + "0" * 64):
            reply = await mcp_initialize(rig.http, token)
            assert reply.status_code == 401
            challenge = reply.headers["www-authenticate"]
            assert challenge.startswith("Bearer ")
            assert f'resource_metadata="{BASE_URL}/.well-known/oauth-protected-resource/mcp"' in challenge


# --------------------------------------------------------------------------
# The whole flow, per client
# --------------------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
async def test_a_client_registers_signs_in_calls_mcp_and_keeps_its_session_through_expiry_and_refresh(open_rig, profile):
    async with open_rig(profile.extra or None) as rig:
        found = await discover(rig.http)
        session = await sign_in(rig.http, found, profile)
        assert session.expires_in == personal_auth.DEFAULT_ACCESS_TOKEN_EXPIRY

        # One authenticated MCP request, then what a connected client does first.
        opened = await mcp_initialize(rig.http, session.access_token)
        assert opened.status_code == 200 and opened.json()["result"]["serverInfo"]["name"]
        assert "health_check" in await list_tool_names(rig.http, session.access_token)

        # The 30 day access token of the sign-in runs out: /mcp answers 401 and points to the resource document again.
        rig.clock.advance(personal_auth.DEFAULT_ACCESS_TOKEN_EXPIRY + 1)
        expired = await mcp_initialize(rig.http, session.access_token)
        assert expired.status_code == 401 and "resource_metadata=" in expired.headers["www-authenticate"]

        # The client refreshes (with resource, when it sends one): a new pair, the old refresh token is retired.
        first = await refresh(rig.http, found, session, profile)
        assert first.status_code == 200, first.text
        renewed = first.json()
        assert renewed["access_token"].startswith("pat_") and renewed["refresh_token"].startswith("prt_")
        assert renewed["access_token"] != session.access_token and renewed["refresh_token"] != session.refresh_token
        assert renewed["expires_in"] == personal_auth.DEFAULT_REFRESH_ACCESS_TOKEN_EXPIRY
        assert (await mcp_initialize(rig.http, renewed["access_token"])).status_code == 200
        assert (await mcp_initialize(rig.http, session.access_token)).status_code == 401  # the old pair is gone

        replay = await refresh(rig.http, found, session, profile)  # the retired refresh token again
        assert replay.status_code in (400, 401) and replay.json()["error"] == "invalid_grant"  # FastMCP makes it a 401

        # The hour passes, and the client refreshes with the NEW refresh token.
        rig.clock.advance(HOUR + 1)
        assert (await mcp_initialize(rig.http, renewed["access_token"])).status_code == 401
        second = await refresh(rig.http, found, session, profile, token=renewed["refresh_token"])
        assert second.status_code == 200, second.text
        assert (await mcp_initialize(rig.http, second.json()["access_token"])).status_code == 200


async def test_a_wrong_client_secret_or_code_verifier_gets_no_tokens(open_rig):
    async with open_rig("chatgpt.com") as rig:
        found = await discover(rig.http)
        client = await register(rig.http, found, CHATGPT)
        verifier, challenge = pkce()
        page = await rig.http.get(path_of(found.authorization), params={
            "response_type": "code", "client_id": client["client_id"], "redirect_uri": CHATGPT_CALLBACK,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "s", "resource": found.resource,
        })
        approved = await rig.http.post(path_of(found.authorization), data={
            **hidden_inputs(page.text), "password": PASSWORD, "decision": "approve",
        })
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        base = {
            "grant_type": "authorization_code", "code": code, "redirect_uri": CHATGPT_CALLBACK,
            "client_id": client["client_id"], "resource": found.resource,
        }
        wrong_secret = await rig.http.post(path_of(found.token), data={**base, "client_secret": "nope", "code_verifier": verifier})
        assert wrong_secret.status_code == 401 and wrong_secret.json()["error"] == "invalid_client"
        wrong_verifier = await rig.http.post(
            path_of(found.token), data={**base, "client_secret": client["client_secret"], "code_verifier": "x" * 64}
        )
        assert wrong_verifier.status_code in (400, 401) and wrong_verifier.json()["error"] == "invalid_grant"
        good = await rig.http.post(
            path_of(found.token), data={**base, "client_secret": client["client_secret"], "code_verifier": verifier}
        )
        assert good.status_code == 200  # the failed attempts did not burn the code


async def test_a_wrong_password_on_the_consent_page_issues_no_code_and_a_client_cannot_reuse_a_redirect_of_another(open_rig):
    async with open_rig("chatgpt.com") as rig:
        found = await discover(rig.http)
        client = await register(rig.http, found, CHATGPT)
        _, challenge = pkce()
        query = {
            "response_type": "code", "client_id": client["client_id"], "redirect_uri": CHATGPT_CALLBACK,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "s", "resource": found.resource,
        }
        form = hidden_inputs((await rig.http.get(path_of(found.authorization), params=query)).text)
        wrong = await rig.http.post(path_of(found.authorization), data={**form, "password": "not-the-password", "decision": "approve"})
        assert wrong.status_code == 200 and "Incorrect password" in wrong.text and "location" not in wrong.headers
        assert rig.provider.auth_codes == {}

        # Another callback id on the same host is not the address this client registered: the same refusal page for all.
        other = {**query, "redirect_uri": "https://chatgpt.com/connector/oauth/another-connector"}
        page = await rig.http.get(path_of(found.authorization), params=other)
        assert page.status_code == 400 and page.text == oauth_guard.INVALID_REQUEST_PAGE
        posted = await rig.http.post(path_of(found.authorization), data={**other, "password": PASSWORD, "decision": "approve"})
        assert posted.status_code == 400 and posted.text == oauth_guard.INVALID_REQUEST_PAGE
        assert rig.provider.auth_codes == {}


async def test_chatgpt_copilot_studio_and_claude_sign_in_on_one_server_with_both_domains_set(open_rig):
    async with open_rig("chatgpt.com, global.consent.azure-apim.net") as rig:
        assert set(rig.provider.allowed_redirect_domains) == {"claude.ai", "claude.com", "localhost", "chatgpt.com", POWER_PLATFORM}
        found = await discover(rig.http)
        sessions = [await sign_in(rig.http, found, profile) for profile in (CHATGPT, CHATGPT_PLATFORM, COPILOT, CLAUDE)]
        assert len({s.client_id for s in sessions}) == 4 and len({s.access_token for s in sessions}) == 4
        for session in sessions:
            assert (await mcp_initialize(rig.http, session.access_token)).status_code == 200
        assert len(rig.provider.clients) == 4


# --------------------------------------------------------------------------
# What the operator finds in the journal
# --------------------------------------------------------------------------


@pytest.mark.parametrize("profile", [pytest.param(CHATGPT, id="chatgpt"), pytest.param(COPILOT, id="copilot-studio")])
async def test_the_journal_names_each_step_and_holds_no_secret_and_no_redirect_path(open_rig, caplog, profile):
    capture(caplog)
    async with open_rig(profile.extra) as rig:
        found = await discover(rig.http)
        session = await sign_in(rig.http, found, profile)
        renewed = (await refresh(rig.http, found, session, profile)).json()
        lines = journal(caplog)
    short = session.client_id[:8]
    expected = [
        f'register outcome=registered client={short} name="{profile.name}" ip={OPERATOR_IP} evicted=0',
        f'authorize outcome=consent_shown client={short} name="{profile.name}" ip={OPERATOR_IP}',
        f'authorize outcome=approved client={short} name="{profile.name}" ip={OPERATOR_IP}',
    ]
    for line in expected:
        assert line in lines, (line, lines)
    assert any(line.startswith(f"token outcome=issued client={short} family=") for line in lines)
    assert any(line.startswith(f"token outcome=refreshed client={short} family=") for line in lines)
    everything = "\n".join(lines)
    # No secret, no full client id, no code, and not even the redirect host or the per connector part of its path.
    for hidden in (
        PASSWORD, session.client_secret, session.access_token, session.refresh_token, renewed["access_token"],
        renewed["refresh_token"], session.client_id, "code=", "code_verifier", CALLBACK_ID, CONNECTOR,
        urlparse(profile.redirect).hostname,
    ):
        assert hidden not in everything, hidden


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------

REFUSED_BODY = {
    "error": "invalid_redirect_uri",
    "error_description": "redirect_uris must be https addresses on an allowed domain, or http addresses on localhost",
}


async def post_registration(rig: Rig, redirect: str) -> httpx.Response:
    return await rig.http.post("/register", json=registration_body("Some client", redirect))


@pytest.mark.parametrize(
    "redirect, host",
    [
        pytest.param(CHATGPT_CALLBACK, "chatgpt.com", id="chatgpt-callback-id"),
        pytest.param(CHATGPT_STABLE, "chatgpt.com", id="chatgpt-stable-redirect"),
        pytest.param(COPILOT_REDIRECT, POWER_PLATFORM, id="copilot-studio"),
    ],
)
@pytest.mark.parametrize("extra", [None, "", " , "], ids=["not-set", "blank", "only-commas"])
async def test_without_the_extra_domain_the_registration_is_refused_and_the_journal_names_the_host(open_rig, caplog, redirect, host, extra):
    capture(caplog)
    async with open_rig(extra) as rig:
        reply = await post_registration(rig, redirect)
        assert reply.status_code == 400 and reply.json() == REFUSED_BODY
        assert rig.provider.clients == {} and not (rig.state_dir / STATE_FILE).exists()  # nothing stored, nothing saved
    assert f"register outcome=refused reason=redirect_not_allowed redirect={host} ip={OPERATOR_IP}" in journal(caplog)
    assert all(r.levelno == logging.WARNING for r in caplog.records if "register outcome=refused" in r.getMessage())


async def test_a_domain_for_one_client_does_not_admit_the_other(open_rig):
    async with open_rig("chatgpt.com") as rig:
        assert (await post_registration(rig, COPILOT_REDIRECT)).status_code == 400
        assert (await post_registration(rig, CHATGPT_CALLBACK)).status_code == 201
    async with open_rig(POWER_PLATFORM) as rig:
        assert (await post_registration(rig, CHATGPT_CALLBACK)).status_code == 400
        assert (await post_registration(rig, COPILOT_REDIRECT)).status_code == 201


@pytest.mark.parametrize(
    "redirect, reason, host",
    [
        ("https://chatgpt.com.evil.example/connector/oauth/x", "not_allowed", "chatgpt.com.evil.example"),
        ("https://evilchatgpt.com/connector/oauth/x", "not_allowed", "evilchatgpt.com"),
        ("https://notchatgpt.com/connector_platform_oauth_redirect", "not_allowed", "notchatgpt.com"),
        ("https://chatgpt-com.example/connector/oauth/x", "not_allowed", "chatgpt-com.example"),
        ("https://evil.example/connector/oauth/chatgpt.com", "not_allowed", "evil.example"),
        ("https://evil.example/?next=https://chatgpt.com/connector/oauth/x", "not_allowed", "evil.example"),
        ("https://chatgpt.com@evil.example/connector/oauth/x", "userinfo", "evil.example"),
        ("https://chatgpt.com:pw@chatgpt.com/connector/oauth/x", "userinfo", "chatgpt.com"),
        ("https://chatgpt.com#@evil.example/connector/oauth/x", "fragment", "chatgpt.com"),
        ("https://chatgpt.com%2eevil.example/connector/oauth/x", "not_allowed", "chatgpt.com.evil.example"),
        ("http://chatgpt.com/connector/oauth/x", "not_https", "chatgpt.com"),
        ("http://chatgpt.com/connector_platform_oauth_redirect", "not_https", "chatgpt.com"),
        ("http://global.consent.azure-apim.net/redirect/x", "not_https", POWER_PLATFORM),
        ("ftp://chatgpt.com/connector/oauth/x", "scheme", "chatgpt.com"),
        ("https://azure-apim.net/redirect/x", "not_allowed", "azure-apim.net"),
        ("https://consent.azure-apim.net/redirect/x", "not_allowed", "consent.azure-apim.net"),
        ("https://global.consent.azure-apim.net.evil.example/redirect/x", "not_allowed", "global.consent.azure-apim.net.evil.example"),
    ],
)
async def test_look_alike_hosts_and_unsafe_addresses_are_refused_even_with_both_domains_set(open_rig, caplog, redirect, reason, host):
    capture(caplog)
    async with open_rig(f"chatgpt.com,{POWER_PLATFORM}") as rig:
        reply = await post_registration(rig, redirect)
        assert reply.status_code == 400 and reply.json() == REFUSED_BODY
        assert rig.provider.clients == {} and not (rig.state_dir / STATE_FILE).exists()
    assert f"register outcome=refused reason=redirect_{reason} redirect={host} ip={OPERATOR_IP}" in journal(caplog)


async def test_one_bad_address_among_good_ones_refuses_the_whole_registration(open_rig):
    async with open_rig("chatgpt.com") as rig:
        body = registration_body("ChatGPT", CHATGPT_CALLBACK)
        body["redirect_uris"] = [CHATGPT_CALLBACK, "https://chatgpt.com.evil.example/cb"]
        reply = await rig.http.post("/register", json=body)
        assert reply.status_code == 400 and reply.json() == REFUSED_BODY and rig.provider.clients == {}


@pytest.mark.parametrize(
    "redirect",
    ["https://eu.chatgpt.com/connector/oauth/x", "https://a.b.chatgpt.com/connector/oauth/x"],
)
async def test_a_real_subdomain_of_an_entry_is_admitted_as_documented(open_rig, redirect):
    # An entry admits its subdomains (.env.example says so): the operator adds only hosts that they trust.
    async with open_rig("chatgpt.com") as rig:
        assert (await post_registration(rig, redirect)).status_code == 201


async def test_removing_the_domain_later_refuses_new_sign_ins_of_a_client_that_registered_while_it_was_set(open_rig, tmp_path, caplog):
    capture(caplog)
    state_dir = tmp_path / "shared-state"
    async with open_rig("chatgpt.com", state_dir=state_dir) as rig:
        found = await discover(rig.http)
        client = await register(rig.http, found, CHATGPT)
    query = {
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": CHATGPT_CALLBACK,
        "code_challenge": pkce()[1], "code_challenge_method": "S256", "state": "s", "resource": MCP_URL,
    }
    async with open_rig(None, state_dir=state_dir) as rig:  # the operator took MCP_EXTRA_REDIRECT_DOMAINS out and restarted
        assert client["client_id"] in rig.provider.clients  # the registration is still stored
        page = await rig.http.get("/authorize", params=query)
        assert page.status_code == 400 and page.text == oauth_guard.INVALID_REQUEST_PAGE
        posted = await rig.http.post("/authorize", data={**query, "password": PASSWORD, "decision": "approve"})
        assert posted.status_code == 400 and posted.text == oauth_guard.INVALID_REQUEST_PAGE and "location" not in posted.headers
        assert rig.provider.auth_codes == {}
    blocked = f"authorize outcome=blocked_redirect reason=not_allowed client={client['client_id'][:8]} redirect=chatgpt.com ip={OPERATOR_IP}"
    assert journal(caplog).count(blocked) == 2
    assert all(r.levelno == logging.WARNING for r in caplog.records if "blocked_redirect" in r.getMessage())


async def test_removing_the_domain_later_keeps_the_tokens_of_a_client_that_signed_in_while_it_was_set(open_rig, tmp_path):
    # Removing the domain revokes nothing: cutting a client off takes a session revoke, so its tokens still refresh.
    state_dir = tmp_path / "shared-state"
    async with open_rig("chatgpt.com", state_dir=state_dir) as rig:
        found = await discover(rig.http)
        session = await sign_in(rig.http, found, CHATGPT)
    async with open_rig(None, state_dir=state_dir) as rig:  # the operator took MCP_EXTRA_REDIRECT_DOMAINS out and restarted
        found = await discover(rig.http)
        assert session.client_id in rig.provider.clients  # the registration is still stored
        assert (await mcp_initialize(rig.http, session.access_token)).status_code == 200
        renewed = await refresh(rig.http, found, session, CHATGPT)
        assert renewed.status_code == 200, renewed.text
        assert (await mcp_initialize(rig.http, renewed.json()["access_token"])).status_code == 200
