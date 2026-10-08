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
import stat
import types
from pathlib import Path

import httpx
import pytest
from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

import main as main_module
import oauth_guard
import oauth_store
import personal_auth
from scripts import oauth_state
from server import build_server as real_build_server

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
    (every provider main() built), `errors()` (the messages main() logged at ERROR), `messages()` (every message logged)
    and `main` (main_module.main)."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    monkeypatch.setattr(main_module, "APP_DIR", app_dir)
    monkeypatch.setattr(main_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(main_module.logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(main_module, "install_log_value_filter", lambda *a, **k: None)
    for name in ("GORELO_TOOLSETS", "GORELO_ENABLE_DESTRUCTIVE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GORELO_API_KEY", "key-not-real")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setenv("MCP_AUTH_PASSWORD", "a-test-password-not-real")

    providers: list = []
    served: list = []
    servers: list = []

    class Recording(personal_auth.PersonalAuthProvider):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            providers.append(self)

    def prepared_build_server(settings, **kwargs):
        server = real_build_server(settings, **kwargs)
        servers.append(server)

        class Prepared:
            def run(self, **run_kwargs):
                served.append(run_kwargs)
                server.http_app()  # what run() does before it binds anything: builds the app, routes of the provider included

        return Prepared()

    monkeypatch.setattr(main_module, "PersonalAuthProvider", Recording)
    monkeypatch.setattr(main_module, "build_server", prepared_build_server)
    caplog.set_level(logging.INFO)
    state = types.SimpleNamespace(
        app_dir=app_dir,
        state_dir=app_dir / ".oauth-state",
        served=served,
        servers=servers,
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
# The operator checks, made in process
# --------------------------------------------------------------------------


def client_secret_of(client_id: str) -> str:
    return version_1_document()["clients"][client_id]["client_secret"]


async def http_get_app(server, *, client=VISITOR):
    """An httpx client wired to the HTTP app of `server` (no sockets), with the app's lifespan running."""
    app = server.http_app(json_response=True)
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=client), base_url="https://mcp.example.test")


@pytest.mark.anyio
async def test_the_operator_checks_and_refresh_test_work_as_written_against_the_app_main_builds(launch, tmp_path):
    # The operator checks rehearsed offline, in order, on a version 1 state file with one live
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
            "/register", json={"redirect_uris": ["https://evil.example/callback"], "client_name": "operator-check"}
        )
        assert refused.status_code == 400 and refused.json()["error"] == "invalid_redirect_uri"
        assert list(launch.providers[-1].clients) == [owner]  # the refused registration created no client
    messages = launch.messages()
    assert any("authorize outcome=invalid_request" in m and "ip=203.0.113.9" in m for m in messages)
    assert any("register outcome=refused reason=redirect_not_allowed" in m and "ip=203.0.113.9" in m for m in messages)
    assert any(m.startswith("oauth state loaded format=v1 clients=1 access=1 refresh=1") for m in messages)  # the counts of the first load

    # check: stop the service, expire the session's access token with the admin script, start the service
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
    assert expired_at < refreshed_at  # the order an operator check expects
    assert not any(m.startswith("authorize outcome=") for m in after)  # no consent page was involved
    everything = " ".join(launch.messages())
    for secret in (access, refresh, fresh["access_token"], fresh["refresh_token"], client_secret_of(owner)):
        assert secret not in everything  # nothing of it reached the journal
    assert json.loads((launch.state_dir / STATE_FILE).read_text())["version"] == 2  # and the state is version 2 from here on


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


def test_the_provider_is_built_with_the_three_arguments_it_always_got(monkeypatch, tmp_path):
    seen = {}

    class Spy:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    class Server:
        def run(self, **kwargs):
            seen["run"] = kwargs

    monkeypatch.setattr(main_module, "APP_DIR", tmp_path)
    monkeypatch.setattr(main_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(main_module.logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(main_module, "install_log_value_filter", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "PersonalAuthProvider", Spy)
    monkeypatch.setattr(main_module, "build_server", lambda settings, **kwargs: Server())
    monkeypatch.setenv("GORELO_API_KEY", "key-not-real")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setenv("MCP_AUTH_PASSWORD", "a-test-password-not-real")
    main_module.main()
    assert set(seen) - {"run"} == {"base_url", "password", "state_dir"}  # the call the systemd unit has always made
    assert seen["state_dir"] == str(tmp_path / ".oauth-state")


def test_main_py_and_this_file_hold_no_em_or_en_dash():
    for path in (REPO_ROOT / "main.py", Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert EM_DASH not in text and EN_DASH not in text, path.name
