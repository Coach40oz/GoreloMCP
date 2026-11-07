"""main.py: what the process does at start, and the reasons it refuses to serve.

main() refuses to start (one error line ending in "; refusing to start", exit status 1, nothing served) when the settings
are wrong (tests/test_server.py and tests/test_settings.py), when the OAuth state file or its directory cannot be trusted
(oauth_store.StateFileError, which includes a lock that another process holds) and when the login gate cannot be made safe
(oauth_guard.GateError, which includes the framework version guard). These tests run main() for real against a temporary
application directory. The process-wide side effects (dotenv, the logging setup, the log filter) are stubbed, and run() is
replaced by what it does first, building the HTTP app (which is where the gate checks its routes), so that nothing is
served and no socket is opened. Fake data only: the state file of the running service is never named.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import types
from pathlib import Path

import fastmcp
import fastmcp.server.http as fastmcp_http
import httpx
import pytest
from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull
from fastmcp import Client
from pydantic import AnyUrl

import main as main_module
import oauth_guard
import oauth_store
import personal_auth
from scripts import oauth_state
from server import build_server as real_build_server
import tools._common as common
from tools._common import REGISTRY

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = oauth_store.STATE_FILE_NAME
LOCK_FILE = oauth_store.LOCK_FILE_NAME
MARKER = "pat_THIS_MUST_NOT_REACH_THE_JOURNAL_0123456789abcdef"
NOW = 1_800_000_000
EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)
VISITOR = ("203.0.113.9", 4321)  # a documentation address: what the tunnel would pass on as the visitor
INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def version_1_document() -> dict:
    """What earlier versions wrote: one client, one live pair, no version key."""
    client = OAuthClientInformationFull(
        client_id="0a1b2c3d-1111-4222-8333-444444444444",
        client_secret="cs_" + "a" * 32,
        client_id_issued_at=NOW - 1000,
        redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name="Claude",
    )
    access = AccessToken(token="pat_" + "b" * 64, client_id=client.client_id, scopes=["mcp"], expires_at=int(4e9))
    refresh = RefreshToken(token="prt_" + "c" * 64, client_id=client.client_id, scopes=["mcp"], expires_at=None)
    return {
        "clients": {client.client_id: client.model_dump(mode="json")},
        "access_tokens": {access.token: access.model_dump(mode="json")},
        "refresh_tokens": {refresh.token: refresh.model_dump(mode="json")},
        "a2r": {access.token: refresh.token},
        "r2a": {refresh.token: access.token},
    }


@pytest.fixture
def launch(monkeypatch, tmp_path, caplog):
    """A temporary application directory and a way to run main() in it. Returns a namespace: `app_dir`, `state_dir`,
    `served` (the keyword arguments of every run() call), `servers` (every FastMCP server main() built), `providers`
    (every provider main() built), `built` (the Settings main() passed to every build_server call), `errors()` (the messages
    main() logged at ERROR), `messages()` (every message logged) and `main` (main_module.main)."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    monkeypatch.setattr(main_module, "APP_DIR", app_dir)
    monkeypatch.setattr(main_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(main_module.logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(main_module, "install_log_value_filter", lambda *a, **k: None)
    for name in ("GORELO_TOOLSETS", "GORELO_ENABLE_DESTRUCTIVE", "GORELO_BASE_URL", "MCP_EXTRA_REDIRECT_DOMAINS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GORELO_API_KEY", "key-not-real")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setenv("MCP_AUTH_PASSWORD", "a-test-password-not-real")

    providers: list = []
    served: list = []
    servers: list = []
    built: list = []

    class Recording(personal_auth.PersonalAuthProvider):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            providers.append(self)

    def prepared_build_server(settings, **kwargs):
        built.append(settings)
        server = real_build_server(settings, **kwargs)
        servers.append(server)

        class Prepared:
            def run(self, **run_kwargs):
                served.append(run_kwargs)
                server.http_app()  # what run() does before it binds anything: builds the app, routes of the provider included

        return Prepared()

    monkeypatch.setattr(main_module, "PersonalAuthProvider", Recording)
    monkeypatch.setattr(main_module, "build_server", prepared_build_server)
    # main() replaces the session manager class and a FastMCP setting for the whole process: put both back after each test
    monkeypatch.setattr(fastmcp_http, "StreamableHTTPSessionManager", fastmcp_http.StreamableHTTPSessionManager)
    monkeypatch.setattr(fastmcp.settings, "check_for_updates", fastmcp.settings.check_for_updates)
    caplog.set_level(logging.INFO)
    state = types.SimpleNamespace(
        app_dir=app_dir,
        state_dir=app_dir / ".oauth-state",
        served=served,
        servers=servers,
        built=built,
        providers=providers,
        main=main_module.main,
        errors=lambda: [r.getMessage() for r in caplog.records if r.name == "gorelo-mcp" and r.levelno >= logging.ERROR],
        messages=lambda: [r.getMessage() for r in caplog.records],
    )
    yield state
    for provider in providers:
        provider.close()


def put_state(state_dir: Path, content: bytes) -> bytes:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / STATE_FILE).write_bytes(content)
    os.chmod(state_dir / STATE_FILE, 0o600)
    return content


def snapshot(state_dir: Path) -> dict[str, tuple[bytes, int]]:
    return {entry.name: (entry.read_bytes(), entry.stat().st_mtime_ns) for entry in sorted(state_dir.iterdir())}


def assert_released(state_dir: Path) -> None:
    """The failed start let go of the lock (a process that refuses to start must not keep the operator out)."""
    oauth_store.StateLock(state_dir).acquire().release()


# --------------------------------------------------------------------------
# A start that works
# --------------------------------------------------------------------------


def test_a_first_start_makes_the_private_state_directory_builds_the_gate_and_serves_on_the_loopback(launch):
    launch.main()
    assert launch.served == [{"transport": "http", "host": "127.0.0.1", "port": 8765}]
    assert launch.errors() == []
    assert stat.S_IMODE(launch.state_dir.stat().st_mode) == 0o700
    assert os.listdir(launch.state_dir) == [LOCK_FILE]  # the lock; no state file until something is saved
    assert [type(p).__mro__[1] for p in launch.providers] == [personal_auth.PersonalAuthProvider]
    assert launch.providers[0]._state_lock.held  # the service keeps the lock for as long as it runs


def test_a_start_loads_a_version_1_file_and_does_not_rewrite_it(launch):
    before = put_state(launch.state_dir, json.dumps(version_1_document(), indent=2).encode())
    mtime = (launch.state_dir / STATE_FILE).stat().st_mtime_ns
    launch.main()
    assert launch.errors() == [] and launch.served
    assert (launch.state_dir / STATE_FILE).read_bytes() == before
    assert (launch.state_dir / STATE_FILE).stat().st_mtime_ns == mtime  # the first save is a token event, not the start
    assert any(m == "oauth state loaded format=v1 clients=1 access=1 refresh=1 pruned=0" for m in launch.messages())
    assert sorted(os.listdir(launch.state_dir)) == sorted([STATE_FILE, LOCK_FILE])


def test_a_start_that_follows_a_normal_stop_finds_the_lock_free(launch):
    launch.main()
    launch.providers.pop().close()  # the process ended
    launch.main()
    assert len(launch.served) == 2 and launch.errors() == []


# --------------------------------------------------------------------------
# A state file that cannot be trusted
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        b'{"clients": {"' + MARKER.encode() + b'": ',  # cut off in the middle of a write
        b"this is not json " + MARKER.encode(),
        b'["' + MARKER.encode() + b'"]',  # JSON, but not an object
        b'{"version": 7, "note": "' + MARKER.encode() + b'"}',  # written by a release this one does not know
        b'{"access_tokens": "' + MARKER.encode() + b'"}',  # a section of the wrong shape
        b"",
    ],
    ids=["truncated", "not json", "a list", "unknown version", "wrong section", "empty file"],
)
def test_main_refuses_to_start_on_a_state_file_it_cannot_trust_and_leaves_it_as_it_was(launch, content):
    put_state(launch.state_dir, content)
    before = snapshot(launch.state_dir)
    with pytest.raises(SystemExit) as info:
        launch.main()
    assert info.value.code == 1
    assert launch.served == [] and launch.providers == []  # nothing was built, nothing served
    (message,) = launch.errors()
    assert message.endswith("; refusing to start") and STATE_FILE in message
    assert MARKER not in " ".join(launch.messages())  # the journal never gets a value from the file
    after = snapshot(launch.state_dir)
    after.pop(LOCK_FILE, None)  # the lock file is the one thing a refused start may have created
    before.pop(LOCK_FILE, None)
    assert after == before  # the file is untouched, byte for byte and mtime for mtime
    assert not [name for name in os.listdir(launch.state_dir) if name.endswith(".tmp")]
    assert_released(launch.state_dir)


def test_main_refuses_to_start_when_another_process_holds_the_state_lock(launch, monkeypatch):
    before = put_state(launch.state_dir, json.dumps(version_1_document(), indent=2).encode())
    # no 5 second wait in the test: oauth_store sees a clock that jumps and a sleep that returns at once
    ticks = iter(range(0, 10_000, 10))
    fake_time = types.SimpleNamespace(monotonic=lambda: float(next(ticks)), sleep=lambda seconds: None, time=lambda: float(NOW))
    monkeypatch.setattr(oauth_store, "time", fake_time)
    with oauth_store.StateLock(launch.state_dir):
        with pytest.raises(SystemExit) as info:
            launch.main()
    assert info.value.code == 1 and launch.served == []
    (message,) = launch.errors()
    assert message.endswith("; refusing to start") and "lock" in message
    assert (launch.state_dir / STATE_FILE).read_bytes() == before


def test_main_refuses_to_start_when_the_state_directory_cannot_be_made(launch):
    launch.state_dir.write_text("a plain file where the directory should be")
    with pytest.raises(SystemExit) as info:
        launch.main()
    assert info.value.code == 1 and launch.served == []
    (message,) = launch.errors()
    assert message.endswith("; refusing to start") and "state directory" in message
    assert launch.state_dir.read_text() == "a plain file where the directory should be"


# --------------------------------------------------------------------------
# A login gate that cannot be made safe
# --------------------------------------------------------------------------


@pytest.mark.parametrize("package", ["fastmcp", "mcp"])
def test_main_refuses_to_start_on_a_framework_version_the_gate_was_not_checked_against(launch, monkeypatch, package):
    real = oauth_guard.installed_version
    monkeypatch.setattr(oauth_guard, "installed_version", lambda name: "9.9.9" if name == package else real(name))
    with pytest.raises(SystemExit) as info:
        launch.main()
    assert info.value.code == 1 and launch.served == []
    (message,) = launch.errors()
    assert message.endswith("; refusing to start") and f"{package} 9.9.9" in message


def test_main_refuses_to_start_when_a_framework_package_is_missing(launch, monkeypatch):
    real = oauth_guard.installed_version
    monkeypatch.setattr(oauth_guard, "installed_version", lambda name: None if name == "mcp" else real(name))
    with pytest.raises(SystemExit) as info:
        launch.main()
    assert info.value.code == 1 and launch.served == []
    assert "mcp is not installed" in launch.errors()[0]


def test_main_refuses_to_start_when_the_gate_fails_while_the_app_is_built(launch, monkeypatch):
    # run() builds the HTTP app first, and the provider checks its routes there: that error has to stop the start as well
    def refuse(self, mcp_path=None):
        raise oauth_guard.GateError("the consent route was not replaced")

    monkeypatch.setattr(personal_auth.PersonalAuthProvider, "get_routes", refuse)
    with pytest.raises(SystemExit) as info:
        launch.main()
    assert info.value.code == 1
    (message,) = launch.errors()
    assert message == "the consent route was not replaced; refusing to start"


def test_the_version_guard_error_is_a_gate_error_and_a_state_error_is_not(monkeypatch):
    # main() catches (StateFileError, GateError): the guard errors are subclasses of GateError, so none of them is missed
    assert issubclass(oauth_guard.VersionGuardError, oauth_guard.GateError)
    assert issubclass(oauth_store.StateLockedError, oauth_store.StateFileError)
    assert not issubclass(oauth_guard.GateError, oauth_store.StateFileError)


# --------------------------------------------------------------------------
# The restart and the refresh, made in process
# --------------------------------------------------------------------------


def client_secret_of(client_id: str) -> str:
    return version_1_document()["clients"][client_id]["client_secret"]


async def http_get_app(server, *, client=VISITOR):
    """An httpx client wired to the HTTP app of `server` (no sockets), with the app's lifespan running."""
    app = server.http_app(json_response=True)
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=client), base_url="https://mcp.example.test")


@pytest.mark.anyio
async def test_a_restart_keeps_the_session_and_a_refresh_needs_no_sign_in_against_the_app_main_builds(launch, tmp_path):
    # The restart and the refresh, run offline and in order on a version 1 state file with one live
    # pair, through the same provider and app main() builds and the same admin script.
    owner = "0a1b2c3d-1111-4222-8333-444444444444"
    document = version_1_document()
    access = next(iter(document["access_tokens"]))
    refresh = next(iter(document["refresh_tokens"]))
    put_state(launch.state_dir, json.dumps(document, indent=2).encode())
    launch.main()  # the service starts on the file the old release wrote

    # check: an existing session survives a restart (no sign-in): the old access token is accepted
    app, http = await http_get_app(launch.servers[-1])
    async with app.router.lifespan_context(app), http:
        assert (await http.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {access}"}, json=INITIALIZE)).status_code == 200
        assert (await http.post("/mcp", headers=MCP_HEADERS, json=INITIALIZE)).status_code == 401  # and nobody else is

        # check: the public login checks
        metadata = (await http.get("/.well-known/oauth-authorization-server")).json()
        assert metadata["revocation_endpoint"].endswith("/revoke") and metadata["registration_endpoint"].endswith("/register")
        page = await http.get("/authorize", params={"client_id": "x"})
        assert page.status_code == 400 and "location" not in page.headers
        assert page.headers["cache-control"] == "no-store" and page.headers["x-frame-options"] == "DENY"
        assert page.headers["referrer-policy"] == "no-referrer" and page.headers["x-content-type-options"] == "nosniff"
        policy = page.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in policy and "form-action" not in policy
        refused = await http.post(
            "/register", json={"redirect_uris": ["https://evil.example/callback"], "client_name": "redirect-refused"}
        )
        assert refused.status_code == 400 and refused.json()["error"] == "invalid_redirect_uri"
        assert list(launch.providers[-1].clients) == [owner]  # the refused registration created no client
    messages = launch.messages()
    assert any("authorize outcome=invalid_request" in m and "ip=203.0.113.9" in m for m in messages)
    assert any("register outcome=refused reason=redirect_not_allowed" in m and "ip=203.0.113.9" in m for m in messages)
    assert any(m.startswith("oauth state loaded format=v1 clients=1 access=1 refresh=1") for m in messages)  # the counts of the first load

    # stop the service, expire the session's access token with the admin script, start the service: the client refreshes
    launch.providers.pop().close()
    before = launch.messages().copy()
    run = oauth_state.main(["expire-access", "--client", "0a1b2c3d", "--apply", "--state-dir", str(launch.state_dir)])
    assert run == oauth_state.EXIT_OK
    launch.main()
    app, http = await http_get_app(launch.servers[-1])
    async with app.router.lifespan_context(app), http:
        expired = await http.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {access}"}, json=INITIALIZE)
        assert expired.status_code == 401  # the old access token is refused as expired ...
        reply = await http.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": owner, "client_secret": client_secret_of(owner)},
        )
        assert reply.status_code == 200, reply.text  # ... and the refresh token still works: no sign-in
        fresh = reply.json()
        assert fresh["access_token"] != access and fresh["refresh_token"] != refresh and fresh["expires_in"] == 3600
        assert (await http.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {fresh['access_token']}"}, json=INITIALIZE)).status_code == 200
    after = [m for m in launch.messages()[len(before):]]
    expired_at = next(i for i, m in enumerate(after) if m == f"access outcome=expired client=0a1b2c3d")
    refreshed_at = next(i for i, m in enumerate(after) if m.startswith("token outcome=refreshed client=0a1b2c3d family="))
    assert expired_at < refreshed_at  # the expiry is logged before the refresh
    assert not any(m.startswith("authorize outcome=") for m in after)  # no consent page was involved
    everything = " ".join(launch.messages())
    for secret in (access, refresh, fresh["access_token"], fresh["refresh_token"], client_secret_of(owner)):
        assert secret not in everything  # nothing of it reached the journal
    assert json.loads((launch.state_dir / STATE_FILE).read_text())["version"] == 2  # and the state is version 2 from here on


# --------------------------------------------------------------------------
# The region and the redirect allowlist
# --------------------------------------------------------------------------


def test_the_start_logs_the_default_api_host_and_no_extra_redirect_domains(launch):
    launch.main()
    assert [m for m in launch.messages() if m.startswith("gorelo-mcp settings: ")] == [
        "gorelo-mcp settings: api_host=api.usw.gorelo.io extra_redirect_domains=none"
    ]
    assert [s.base_url for s in launch.built] == ["https://api.usw.gorelo.io/v1"]
    assert launch.providers[0].allowed_redirect_domains == ["claude.ai", "claude.com", "localhost"]


def test_the_region_and_the_extra_redirect_domains_reach_the_server_the_provider_and_the_log(launch, monkeypatch):
    monkeypatch.setenv("GORELO_BASE_URL", "https://api.aue.gorelo.io/")
    monkeypatch.setenv("MCP_EXTRA_REDIRECT_DOMAINS", "Global.Consent.Azure-APIM.net, example.org")
    launch.main()
    assert launch.errors() == [] and launch.served
    assert [s.base_url for s in launch.built] == ["https://api.aue.gorelo.io/v1"]
    assert launch.providers[0].allowed_redirect_domains == [
        "claude.ai", "claude.com", "localhost", "global.consent.azure-apim.net", "example.org"
    ]
    assert "gorelo-mcp settings: api_host=api.aue.gorelo.io extra_redirect_domains=global.consent.azure-apim.net,example.org" in launch.messages()
    everything = " ".join(launch.messages())
    assert "key-not-real" not in everything and "a-test-password-not-real" not in everything


@pytest.mark.parametrize(
    "name, value, fragment",
    [
        ("GORELO_BASE_URL", "http://api.aue.gorelo.io", "GORELO_BASE_URL must start with https://"),
        ("GORELO_BASE_URL", "https://api.aue.gorelo.io.evil.example", "GORELO_BASE_URL must name a host under gorelo.io"),
        ("MCP_EXTRA_REDIRECT_DOMAINS", "https://global.consent.azure-apim.net/redirect/x", "MCP_EXTRA_REDIRECT_DOMAINS entry"),
        ("MCP_EXTRA_REDIRECT_DOMAINS", "localhost", "must not be localhost"),
    ],
)
def test_main_refuses_to_start_on_a_bad_region_or_redirect_domain_before_it_builds_anything(launch, monkeypatch, name, value, fragment):
    monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as info:
        launch.main()
    assert info.value.code == 1
    assert launch.served == [] and launch.providers == [] and launch.built == []
    (message,) = launch.errors()
    assert message.endswith("; refusing to start") and fragment in message
    assert not launch.state_dir.exists()  # the OAuth state directory was not even touched


@pytest.mark.parametrize(
    "value, fragment",
    [
        ("https://mcp.example.test/mcp", "PUBLIC_BASE_URL must not end in /mcp"),
        ("http://mcp.example.test", "PUBLIC_BASE_URL must start with https://"),
    ],
)
def test_main_refuses_a_public_base_url_that_would_break_the_sign_in_with_one_line_not_a_traceback(launch, monkeypatch, value, fragment):
    # /mcp: the OAuth endpoints would answer 404 and claude.ai could never sign in; http: the OAuth library raises ValueError
    # inside run(). Both are caught in the settings, before any provider or server is built.
    monkeypatch.setenv("PUBLIC_BASE_URL", value)
    with pytest.raises(SystemExit) as info:
        launch.main()
    assert info.value.code == 1
    assert launch.served == [] and launch.providers == [] and launch.built == []
    (message,) = launch.errors()
    assert message.endswith("; refusing to start") and fragment in message
    assert value not in message  # the address is never echoed (it may hold a password)
    assert not launch.state_dir.exists()


def registration(redirect):
    return {
        "redirect_uris": [redirect], "client_name": "Copilot Studio", "token_endpoint_auth_method": "client_secret_post",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
    }


@pytest.mark.anyio
async def test_a_registration_on_the_extra_domain_is_accepted_and_every_other_redirect_is_still_refused(launch, monkeypatch):
    monkeypatch.setenv("MCP_EXTRA_REDIRECT_DOMAINS", "global.consent.azure-apim.net")
    launch.main()
    app, http = await http_get_app(launch.servers[-1])
    async with app.router.lifespan_context(app), http:
        accepted = await http.post("/register", json=registration("https://global.consent.azure-apim.net/redirect/my-connector"))
        assert accepted.status_code == 201, accepted.text
        for refused in ("https://evil.example/callback", "http://global.consent.azure-apim.net/redirect/x", "https://azure-apim.net/redirect/x"):
            reply = await http.post("/register", json=registration(refused))
            assert reply.status_code == 400 and reply.json()["error"] == "invalid_redirect_uri", refused
    messages = launch.messages()
    assert any(m.startswith("register outcome=registered") for m in messages)
    assert any("register outcome=refused reason=redirect_not_allowed redirect=azure-apim.net" in m for m in messages)
    assert any("register outcome=refused reason=redirect_not_https redirect=global.consent.azure-apim.net" in m for m in messages)


@pytest.mark.anyio
async def test_without_the_variable_the_power_platform_redirect_is_refused_and_the_journal_names_the_host(launch):
    launch.main()
    app, http = await http_get_app(launch.servers[-1])
    async with app.router.lifespan_context(app), http:
        reply = await http.post("/register", json=registration("https://global.consent.azure-apim.net/redirect/my-connector"))
    assert reply.status_code == 400 and reply.json()["error"] == "invalid_redirect_uri"
    assert any("register outcome=refused reason=redirect_not_allowed redirect=global.consent.azure-apim.net" in m for m in launch.messages())


# --------------------------------------------------------------------------
# The gated tools: GORELO_ENABLE_DESTRUCTIVE as a switch per tool
# --------------------------------------------------------------------------

FIVE_GATED = (
    "delete_ticket_comment,delete_invoice,delete_uptime_check,"
    "remove_document_related_item,delete_time_entry"
)


def ready_lines(launch):
    return [m for m in launch.messages() if m.startswith("gorelo-mcp ready: ")]


def gated_lines(launch):
    return [m for m in launch.messages() if m.startswith("gorelo-mcp gated tools: ")]


def test_the_start_logs_that_no_gated_tool_is_registered_by_default(launch):
    launch.main()
    assert gated_lines(launch) == ["gorelo-mcp gated tools: none"]
    (ready,) = ready_lines(launch)
    assert " destructive=False " in ready


def test_the_start_logs_every_gated_tool_of_the_enabled_toolsets_for_the_word_one(launch, monkeypatch):
    monkeypatch.setenv("GORELO_ENABLE_DESTRUCTIVE", "1")
    monkeypatch.setenv("GORELO_TOOLSETS", "core,tickets,time")
    launch.main()
    # core gained three gated tools in 1.5.0 (delete_asset_type, delete_client_domain, delete_client_location) and one in 1.5.2 (delete_contact)
    assert gated_lines(launch) == [
        "gorelo-mcp gated tools: delete_asset_type,delete_client_domain,delete_client_location,delete_contact,delete_ticket_comment,delete_time_entry"
    ]


def test_the_start_logs_the_listed_gated_tools_sorted_and_keeps_the_ready_line_shape(launch, monkeypatch):
    monkeypatch.setenv("GORELO_ENABLE_DESTRUCTIVE", " Delete_Time_Entry , delete_item,delete_time_entry ")
    monkeypatch.setenv("GORELO_TOOLSETS", "core,tickets,time,billing,uptime")
    launch.main()
    assert launch.errors() == [] and launch.served
    assert gated_lines(launch) == ["gorelo-mcp gated tools: delete_item,delete_time_entry"]
    (ready,) = ready_lines(launch)
    assert re.fullmatch(r"gorelo-mcp ready: toolsets=billing,core,tickets,time,uptime destructive=True tools=\d+", ready)
    (built,) = launch.built
    assert built.destructive is True and built.destructive_tools == frozenset({"delete_item", "delete_time_entry"})


@pytest.mark.anyio
async def test_the_server_main_builds_has_exactly_the_listed_gated_tools(launch, monkeypatch):
    monkeypatch.setenv("GORELO_ENABLE_DESTRUCTIVE", "delete_item,delete_time_entry")
    monkeypatch.setenv("GORELO_TOOLSETS", "all")
    launch.main()
    async with Client(launch.servers[-1]) as session:
        names = {tool.name for tool in await session.list_tools()}
    gated = {spec.name for spec in REGISTRY.specs if spec.kind == "destructive"}
    assert names & gated == {"delete_item", "delete_time_entry"}


def test_a_list_of_five_gated_tools_starts_and_is_logged_in_full(launch, monkeypatch):
    monkeypatch.setenv("GORELO_ENABLE_DESTRUCTIVE", FIVE_GATED)
    monkeypatch.setenv("GORELO_TOOLSETS", "all")
    launch.main()
    assert launch.errors() == []
    assert gated_lines(launch) == ["gorelo-mcp gated tools: " + ",".join(sorted(FIVE_GATED.split(",")))]


HELD_PAIR = ("create_contract", "update_contract")
HELD_LINE = "gorelo-mcp held tools ignored: create_contract, update_contract (held back until Gorelo releases them)"


def _start_with_list(launch, monkeypatch, listed):
    monkeypatch.setenv("GORELO_ENABLE_DESTRUCTIVE", ",".join(listed))
    monkeypatch.setenv("GORELO_TOOLSETS", "all")
    launch.main()
    assert launch.errors() == [] and launch.served
    (ready,) = ready_lines(launch)
    return ready


def test_a_list_with_every_gated_name_starts_with_no_held_line_since_nothing_is_held_back(launch, monkeypatch):
    # 1.5.3 released the contract writes: HELD_TOOLS is empty, so the 18 declared gated names (the production list of that release) all register
    declared = sorted(spec.name for spec in REGISTRY.specs if spec.kind == "destructive")
    assert len(declared) == 18 and {"create_contract", "update_contract", "delete_contact"} <= set(declared)
    ready = _start_with_list(launch, monkeypatch, declared)
    assert ready.endswith(" destructive=True tools=162")  # 144 regular tools and the 18 gated ones
    assert gated_lines(launch) == ["gorelo-mcp gated tools: " + ",".join(declared)]
    assert [m for m in launch.messages() if "held" in m] == []  # an empty held set logs nothing


def test_the_gated_list_of_1_5_0_still_starts_cleanly_and_registers_17(launch, monkeypatch):
    # the production .env of 1.5.0 and 1.5.1 listed 17 names (no delete_contact, the two contract writes in)
    declared = sorted(spec.name for spec in REGISTRY.specs if spec.kind == "destructive")
    listed = [name for name in declared if name != "delete_contact"]
    assert len(listed) == 17
    ready = _start_with_list(launch, monkeypatch, listed)
    assert ready.endswith(" destructive=True tools=161")  # 144 regular tools and the 17 gated ones listed
    assert [m for m in launch.messages() if "held" in m] == []


def test_the_gated_list_of_1_5_2_starts_cleanly_registers_160_and_logs_no_held_line(launch, monkeypatch):
    # the production .env since the 1.5.2 deploy: 16 names, delete_contact in and the two contract writes out (the 1.5.3 code registers
    # the two only when the list names them)
    declared = sorted(spec.name for spec in REGISTRY.specs if spec.kind == "destructive")
    listed = [name for name in declared if name not in ("create_contract", "update_contract")]
    assert len(listed) == 16 and "delete_contact" in listed
    ready = _start_with_list(launch, monkeypatch, listed)
    assert ready.endswith(" destructive=True tools=160")  # 144 regular tools and the 16 gated ones listed
    assert gated_lines(launch) == ["gorelo-mcp gated tools: " + ",".join(listed)]
    assert [m for m in launch.messages() if "held" in m] == []


def _held_list_start(launch, monkeypatch, listed):
    monkeypatch.setattr(common, "HELD_TOOLS", frozenset(HELD_PAIR))  # the mechanism: put the two names into the held set again
    monkeypatch.setenv("GORELO_ENABLE_DESTRUCTIVE", ",".join(listed))
    monkeypatch.setenv("GORELO_TOOLSETS", "all")
    launch.main()
    assert launch.errors() == [] and launch.served
    messages = launch.messages()
    assert messages.count(HELD_LINE) == 1
    kept = [name for name in listed if name not in HELD_PAIR]
    assert gated_lines(launch) == ["gorelo-mcp gated tools: " + ",".join(kept)]
    (ready,) = ready_lines(launch)
    assert messages.index(gated_lines(launch)[0]) < messages.index(HELD_LINE) < messages.index(ready)
    return ready


def test_a_list_with_held_names_starts_with_one_warning_between_the_gated_and_ready_lines(launch, monkeypatch):
    declared = sorted(spec.name for spec in REGISTRY.specs if spec.kind == "destructive")
    listed = [name for name in declared if name != "delete_contact"]
    ready = _held_list_start(launch, monkeypatch, listed)
    assert ready.endswith(" destructive=True tools=159")  # 144 regular tools and the 15 gated ones the list names that are not held


def test_a_held_set_with_a_list_that_names_delete_contact_registers_it_next_to_the_other_gated_tools(launch, monkeypatch):
    declared = sorted(spec.name for spec in REGISTRY.specs if spec.kind == "destructive")
    ready = _held_list_start(launch, monkeypatch, declared)
    assert ready.endswith(" destructive=True tools=160")
    assert "delete_contact" in gated_lines(launch)[0].split(": ", 1)[1].split(",")


@pytest.mark.parametrize(
    "toolsets, destructive, fragments",
    [
        ("all", "delete_itm", ["GORELO_ENABLE_DESTRUCTIVE cannot be honored", "'delete_itm' (no tool has this name)", "delete_item"]),
        ("all", "truee", ["'truee' (no tool has this name)", "Write 1 for all of them"]),
        ("all", "update_client", ["'update_client' (not a gated tool"]),
        ("core,tickets", "delete_item", ["'delete_item' (a gated tool of the toolset 'billing', which GORELO_TOOLSETS does not enable)", "delete_ticket_comment"]),
        ("all", "1,delete_item", ["GORELO_ENABLE_DESTRUCTIVE mixes the word '1' with other items"]),
        ("all", "delete_item delete_time_entry", ["GORELO_ENABLE_DESTRUCTIVE has item(s) that cannot be tool names"]),
    ],
    ids=["unknown name", "mistyped word", "not a gated tool", "toolset not enabled", "word and names", "no comma"],
)
def test_main_refuses_to_start_on_a_gated_list_the_tools_cannot_honor_before_it_builds_anything(launch, monkeypatch, toolsets, destructive, fragments):
    monkeypatch.setenv("GORELO_TOOLSETS", toolsets)
    monkeypatch.setenv("GORELO_ENABLE_DESTRUCTIVE", destructive)
    with pytest.raises(SystemExit) as info:
        launch.main()
    assert info.value.code == 1
    assert launch.served == [] and launch.providers == [] and launch.built == []
    (message,) = launch.errors()
    assert message.endswith("; refusing to start")
    for fragment in fragments:
        assert fragment in message, fragment
    assert gated_lines(launch) == [] and ready_lines(launch) == []
    assert not launch.state_dir.exists()  # the OAuth state directory was not even touched


# --------------------------------------------------------------------------
# Only those refusals
# --------------------------------------------------------------------------


@pytest.mark.parametrize("error", [RuntimeError("boom"), ValueError("bad"), OSError(5, "disk")], ids=lambda e: type(e).__name__)
def test_any_other_failure_is_not_turned_into_a_refusal(launch, monkeypatch, error):
    def explode(self, mcp_path=None):
        raise error

    monkeypatch.setattr(personal_auth.PersonalAuthProvider, "get_routes", explode)
    with pytest.raises(type(error)):
        launch.main()
    assert not any("refusing to start" in m for m in launch.messages())  # a traceback, not a tidy refusal


def test_the_provider_is_built_with_the_four_arguments_main_passes(monkeypatch, tmp_path):
    seen = {}

    class Spy:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    class Server:
        def run(self, **kwargs):
            seen["run"] = kwargs

    monkeypatch.setattr(fastmcp_http, "StreamableHTTPSessionManager", fastmcp_http.StreamableHTTPSessionManager)  # main() swaps it
    monkeypatch.setattr(fastmcp.settings, "check_for_updates", fastmcp.settings.check_for_updates)
    monkeypatch.setattr(main_module, "APP_DIR", tmp_path)
    monkeypatch.setattr(main_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(main_module.logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(main_module, "install_log_value_filter", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "PersonalAuthProvider", Spy)
    monkeypatch.setattr(main_module, "build_server", lambda settings, **kwargs: Server())
    monkeypatch.setenv("GORELO_API_KEY", "key-not-real")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setenv("MCP_AUTH_PASSWORD", "a-test-password-not-real")
    for name in ("GORELO_BASE_URL", "MCP_EXTRA_REDIRECT_DOMAINS"):
        monkeypatch.delenv(name, raising=False)
    main_module.main()
    assert set(seen) - {"run"} == {"base_url", "password", "state_dir", "allowed_redirect_domains"}  # the four arguments main() passes
    assert seen["allowed_redirect_domains"] == ["claude.ai", "claude.com", "localhost"]
    assert seen["state_dir"] == str(tmp_path / ".oauth-state")


def test_main_py_and_this_file_hold_no_em_or_en_dash():
    for path in (REPO_ROOT / "main.py", Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert EM_DASH not in text and EN_DASH not in text, path.name


# --------------------------------------------------------------------------
# No request to pypi.org at start, and idle MCP sessions are freed
# --------------------------------------------------------------------------


def test_a_start_turns_the_fastmcp_update_check_off_so_no_request_goes_to_pypi(launch, monkeypatch):
    from fastmcp.utilities import version_check

    fetched = []
    monkeypatch.setattr(version_check, "_fetch_latest_version", lambda *a, **k: fetched.append(a) or "99.0.0")
    monkeypatch.setattr(version_check, "_read_cache", lambda *a, **k: (None, 0))
    monkeypatch.setattr(fastmcp.settings, "check_for_updates", "stable")  # what the process would have without main()
    assert version_check.check_for_newer_version() == "99.0.0" and fetched  # the check does reach out when it is on
    fetched.clear()
    launch.main()
    assert fastmcp.settings.check_for_updates == "off"
    assert version_check.check_for_newer_version() is None and fetched == []  # the banner's check now asks nobody


def test_the_session_manager_gets_an_idle_timeout_in_stateful_mode_only(launch):
    launch.main()
    manager = fastmcp_http.StreamableHTTPSessionManager
    app = types.SimpleNamespace()
    stateful = manager(app=app)
    assert stateful.session_idle_timeout == oauth_guard.SESSION_IDLE_TIMEOUT_SECONDS and stateful.stateless is False
    assert manager(app=app, session_idle_timeout=5).session_idle_timeout == 5  # an explicit value is not overridden
    assert manager(app=app, stateless=True).session_idle_timeout is None  # mcp refuses a timeout in stateless mode
    assert 0 < oauth_guard.SESSION_IDLE_TIMEOUT_SECONDS <= 24 * 3600
    oauth_guard.limit_idle_sessions(1)  # a second call sets a new limit and does not stack classes
    assert fastmcp_http.StreamableHTTPSessionManager(app=app).session_idle_timeout == 1
    assert fastmcp_http.StreamableHTTPSessionManager.__mro__[2] is manager.__mro__[2]


def test_a_start_refuses_when_the_installed_mcp_cannot_limit_idle_sessions(launch, monkeypatch):
    class Old:
        def __init__(self, app, event_store=None):
            pass

    monkeypatch.setattr(fastmcp_http, "StreamableHTTPSessionManager", Old)
    with pytest.raises(SystemExit) as info:
        launch.main()
    assert info.value.code == 1 and launch.served == []
    assert any("session_idle_timeout" in m and m.endswith("refusing to start") for m in launch.errors())


@pytest.mark.anyio
async def test_an_idle_session_is_freed_and_activity_keeps_it_alive(launch, monkeypatch):
    import anyio

    monkeypatch.setattr(oauth_guard, "SESSION_IDLE_TIMEOUT_SECONDS", 1.0)
    document = version_1_document()
    access = next(iter(document["access_tokens"]))
    put_state(launch.state_dir, json.dumps(document, indent=2).encode())
    launch.main()
    created = []
    base = fastmcp_http.StreamableHTTPSessionManager

    class Spy(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(fastmcp_http, "StreamableHTTPSessionManager", Spy)  # to see the manager the app builds
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {access}"}
    app, http = await http_get_app(launch.servers[-1])
    async with app.router.lifespan_context(app), http:
        opened = await http.post("/mcp", headers=headers, json=INITIALIZE)
        assert opened.status_code == 200
        session = {**headers, "mcp-session-id": opened.headers["mcp-session-id"]}
        ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        await http.post("/mcp", headers=session, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        manager = created[-1]
        assert manager.session_idle_timeout == 1.0 and len(manager._server_instances) == 1
        for _ in range(2):  # each request pushes the deadline back: the session outlives its first second
            await anyio.sleep(0.6)
            assert (await http.post("/mcp", headers=session, json=ping)).status_code == 200
        await anyio.sleep(2.0)  # no request for twice the timeout
        assert manager._server_instances == {}  # freed, not kept for the life of the process
        gone = await http.post("/mcp", headers=session, json=ping)
        assert gone.status_code == 404  # the client is told to start a new session, as after a restart
        again = await http.post("/mcp", headers=headers, json=INITIALIZE)
        assert again.status_code == 200 and again.headers["mcp-session-id"] != opened.headers["mcp-session-id"]
