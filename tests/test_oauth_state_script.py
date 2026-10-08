"""scripts/oauth_state.py: the operator's way to look at and tidy the OAuth state file without ever printing a credential.

Everything runs against temporary state directories with fake data and a fixed clock. The state file of the running service
is never touched: nothing here names it, and the script's default directory is pointed at a temporary one for every test,
so a test that forgets `--state-dir` still cannot reach a real state. No sockets are opened.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shlex
import stat
import subprocess
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

import oauth_store
from personal_auth import PersonalAuthProvider
from scripts import oauth_state

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "oauth_state.py"
REAL_DEFAULT_STATE_DIR = oauth_state.DEFAULT_STATE_DIR  # read before any test patches it
EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)

DAY = 24 * 60 * 60
NOW = int(datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp())
STATE_FILE = oauth_store.STATE_FILE_NAME
LOCK_FILE = oauth_store.LOCK_FILE_NAME
BASE_URL = "https://mcp.example.test"
PASSWORD = "a-test-password-not-real"
LIVE_ID = "0a1b2c3d-1111-4222-8333-444444444444"
HEX_RUN = re.compile(r"[0-9a-fA-F]{12,}")


# --------------------------------------------------------------------------
# Fake state
# --------------------------------------------------------------------------


def client_id_of(n: int) -> str:
    return f"{n:08x}-aaaa-4bbb-8ccc-{n:012x}"


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class Builder:
    """Fake clients and token pairs, written as the version 1 document that older releases wrote, or as version 2."""

    def __init__(self) -> None:
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.access: dict[str, AccessToken] = {}
        self.refresh: dict[str, RefreshToken] = {}
        self.a2r: dict[str, str] = {}
        self.r2a: dict[str, str] = {}
        self.meta: dict[str, dict] = {}
        self.retired: dict[str, dict] = {}

    def client(
        self,
        n: int,
        name: str | None = "Claude",
        *,
        registered: int | None = NOW - 100 * DAY,
        uris: tuple[str, ...] = ("https://claude.ai/api/mcp/auth_callback",),
        cid: str | None = None,
    ) -> str:
        cid = cid or client_id_of(n)
        self.clients[cid] = OAuthClientInformationFull(
            client_id=cid,
            client_secret="cs_" + digest(f"secret-{cid}")[:32],
            client_id_issued_at=registered,
            redirect_uris=[AnyUrl(uri) for uri in uris],
            token_endpoint_auth_method="client_secret_post",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            client_name=name,
        )
        return cid

    def pair(
        self,
        cid: str,
        label: str,
        *,
        access_exp: int | None,
        refresh_exp: int | None = None,
        issued: int | None = None,
        access_prefix: str = "pat_",
        refresh_prefix: str = "prt_",
    ) -> tuple[str, str]:
        access, refresh = f"{access_prefix}{digest(label + '-access')}", f"{refresh_prefix}{digest(label + '-refresh')}"
        self.access[access] = AccessToken(token=access, client_id=cid, scopes=["mcp"], expires_at=access_exp)
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=cid, scopes=["mcp"], expires_at=refresh_exp)
        self.a2r[access], self.r2a[refresh] = refresh, access
        if issued is not None:
            self.meta[refresh] = {"family": digest(label)[:16], "issued_at": issued, "origin": "code"}
        return access, refresh

    def lone_access(self, cid: str, label: str, *, expires: int | None) -> str:
        access = f"pat_{digest(label + '-access')}"
        self.access[access] = AccessToken(token=access, client_id=cid, scopes=["mcp"], expires_at=expires)
        return access

    def lone_refresh(self, cid: str, label: str, *, expires: int | None = None, issued: int | None = None) -> str:
        refresh = f"prt_{digest(label + '-refresh')}"
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=cid, scopes=["mcp"], expires_at=expires)
        if issued is not None:
            self.meta[refresh] = {"family": digest(label)[:16], "issued_at": issued, "origin": "code"}
        return refresh

    def document(self, version: int = 1) -> dict:
        document = {
            "clients": {k: v.model_dump(mode="json") for k, v in self.clients.items()},
            "access_tokens": {k: v.model_dump(mode="json") for k, v in self.access.items()},
            "refresh_tokens": {k: v.model_dump(mode="json") for k, v in self.refresh.items()},
            "a2r": dict(self.a2r),
            "r2a": dict(self.r2a),
        }
        if version == 2:
            document = {"version": 2, **document, "refresh_meta": dict(self.meta), "retired": dict(self.retired)}
        return document

    def write(self, state_dir: Path, version: int = 1, *, mode: int = 0o600) -> bytes:
        """Write the document into `state_dir` (mode 0700). `mode` is the mode of the file: 0600 as the new code writes it, or
        0644 as an older release writes a new one (Path.write_text under the service's umask 0022)."""
        state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(state_dir, 0o700)
        data = json.dumps(self.document(version), indent=2).encode()
        path = state_dir / STATE_FILE
        path.write_bytes(data)
        os.chmod(path, mode)
        return data

    def secrets(self) -> list[str]:
        """Every string that must never be printed: tokens, client secrets and full client ids."""
        return [*self.access, *self.refresh, *(c.client_secret for c in self.clients.values()), *self.clients]


OLD_SESSIONS = [client_id_of(0x10 + i) for i in range(6)]
TOKENLESS = [client_id_of(0x20 + i) for i in range(3)]


def typical_state() -> Builder:
    """A synthetic scenario of a long-running install: one live session, six old sessions whose access tokens
    expired long ago and whose refresh tokens never expire, and three clients that never got a token."""
    builder = Builder()
    live = builder.client(0, "Claude", registered=NOW - 17 * DAY, cid=LIVE_ID)
    builder.pair(live, "live", access_exp=NOW + 13 * DAY)
    for i, cid in enumerate(OLD_SESSIONS):
        builder.client(0x10 + i, "Claude", registered=NOW - (180 - i) * DAY)
        builder.pair(cid, f"old-{i}", access_exp=NOW - (150 - i) * DAY)
    for i, name in enumerate(("test", "old-test-client", "Claude")):
        builder.client(0x20 + i, name, registered=NOW - 185 * DAY)
    return builder


# --------------------------------------------------------------------------
# Running the script
# --------------------------------------------------------------------------


@dataclass
class Run:
    code: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + self.err


def invoke(*argv: str, state_dir: Path | None = None, now: float = NOW) -> Run:
    out, err = io.StringIO(), io.StringIO()
    args = list(argv)
    if state_dir is not None:
        args += ["--state-dir", str(state_dir)]
    code = oauth_state.main(args, clock=lambda: now, stdout=out, stderr=err)
    return Run(code, out.getvalue(), err.getvalue())


def snapshot(state_dir: Path) -> dict[str, tuple[bytes, int, int]]:
    """Name, content, modification time and mode of everything in the directory."""
    found = {}
    for entry in sorted(state_dir.iterdir()):
        info = entry.stat()
        found[entry.name] = (entry.read_bytes() if entry.is_file() else b"", info.st_mtime_ns, stat.S_IMODE(info.st_mode))
    return found


def read_document(state_dir: Path) -> dict:
    return json.loads((state_dir / STATE_FILE).read_text(encoding="utf-8"))


def loaded(state_dir: Path) -> oauth_store.LoadResult:
    return oauth_store.load_state(state_dir, now=NOW, prune_expired=False)


def line_of(text: str, start: str) -> str:
    matches = [line for line in text.splitlines() if line.startswith(start)]
    assert len(matches) == 1, f"expected one line starting with {start!r}, found {matches}"
    return matches[0]


def assert_no_credential(run: Run, builder: Builder) -> None:
    for secret in builder.secrets():
        assert secret not in run.text
    shown = "\n".join(line for line in run.text.splitlines() if not line.startswith("dir:"))
    assert HEX_RUN.search(shown) is None, "a run of 12 or more hex digits was printed"
    for cid in builder.clients:
        assert cid not in run.text and cid[:9] not in run.text  # the first 8 characters only


@pytest.fixture(autouse=True)
def never_the_service_state(monkeypatch, tmp_path_factory):
    """Whatever a test forgets to name, it must not be the service's state directory."""
    monkeypatch.setattr(oauth_state, "DEFAULT_STATE_DIR", tmp_path_factory.mktemp("default") / "never-created")


@pytest.fixture
def state_dir(tmp_path) -> Path:
    return tmp_path / "oauth-state"


@pytest.fixture
def production(state_dir) -> Builder:
    builder = typical_state()
    builder.write(state_dir)
    return builder


def hold_lock(state_dir: Path) -> oauth_store.StateLock:
    return oauth_store.StateLock(state_dir).acquire()


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["bogus"],
        ["check", "--unknown"],
        ["purge"],
        ["purge", "--stale"],
        ["purge", "--plan"],
        ["purge", "--apply"],
        ["purge", "--stale", "--plan", "--apply"],
        ["expire-access", "--apply"],
        ["expire-access", "--client", "0a1b2c3d"],
        ["revoke-all"],
    ],
    ids=lambda argv: " ".join(argv) or "no arguments",
)
def test_a_command_line_that_is_not_deliberate_is_refused_and_changes_nothing(argv, production, state_dir, capsys):
    before = snapshot(state_dir)
    assert oauth_state.main([*argv, "--state-dir", str(state_dir)], clock=lambda: NOW) == oauth_state.EXIT_REFUSED
    assert snapshot(state_dir) == before
    captured = capsys.readouterr()
    assert captured.err.startswith("usage: oauth_state.py")  # the usage first ...
    assert oauth_state.COMMAND_LINE_REFUSED in captured.err  # ... then the one fixed sentence that says it was refused


@pytest.mark.parametrize(
    "value", ["short", "toolongvalue", "0a1b2c3!", LIVE_ID, "", "0a1b 2c3", "0a1b2c3\n"], ids=lambda v: repr(v)[:20]
)
@pytest.mark.parametrize("argv", [["expire-access", "--client", None, "--apply"], ["purge", "--stale", "--keep-client", None, "--plan"]])
def test_an_id_that_is_not_the_first_8_characters_of_an_id_is_refused_without_echoing_it(argv, value, production, state_dir, capsys):
    argv = [value if part is None else part for part in argv]
    before = snapshot(state_dir)
    assert oauth_state.main([*argv, "--state-dir", str(state_dir)], clock=lambda: NOW) == oauth_state.EXIT_REFUSED
    assert snapshot(state_dir) == before
    captured = capsys.readouterr()
    assert "first 8 characters" in captured.err  # the fixed hint for a bad id
    assert value.strip() == "" or (value not in captured.err and value not in captured.out)  # and never the value itself


# --------------------------------------------------------------------------
# A command line that does not fit is refused without echoing what was typed
# --------------------------------------------------------------------------

# Fake values of the kinds an operator could paste into the wrong place by mistake: a token and a full client id.
STRAY_TOKEN = "prt_" + "c" * 64
NEVER_ECHOED = (STRAY_TOKEN, STRAY_TOKEN[:12], "c" * 20, LIVE_ID, LIVE_ID[:12], "0a1b2c3d")
STOCK_MESSAGES = ("unrecognized arguments", "invalid choice", "expected one argument", "the following arguments are required", "ambiguous")
TYPED_BY_MISTAKE = [
    ["check", STRAY_TOKEN],  # a token-shaped stray argument (first case)
    ["inventory", LIVE_ID],  # a full client id where nothing is expected
    ["purge", "--stale", "--plan", STRAY_TOKEN],
    ["expire-access", "--client", "0a1b2c3d", "--apply", LIVE_ID],  # the real arguments, then a full id after them
    ["revoke-all", "--apply", STRAY_TOKEN],
    ["check", "--" + STRAY_TOKEN],  # an option that does not exist
    ["check", "--token=" + STRAY_TOKEN],
    [STRAY_TOKEN],  # an unknown command, token-shaped (second case)
    ["x" + STRAY_TOKEN, "check"],
    ["expire-access", "--client", STRAY_TOKEN, "--apply"],  # not the first 8 characters of an id
    ["purge", "--stale", "--keep-client", LIVE_ID, "--plan"],
    ["purge", "--stale", "--plan", "--keep-client", "0a1b2c3d", LIVE_ID],  # a good id, then a full one
]


@pytest.mark.parametrize("argv", TYPED_BY_MISTAKE, ids=lambda argv: " ".join(part[:12] for part in argv))
def test_a_command_line_error_prints_the_usage_and_one_fixed_sentence_and_echoes_nothing(argv, capsys):
    assert oauth_state.main(argv, clock=lambda: NOW) == oauth_state.EXIT_REFUSED == 2
    captured = capsys.readouterr()
    assert captured.out == ""  # the usage goes to stderr, with the sentence
    lines = captured.err.splitlines()
    assert len(lines) >= 2 and lines[0].startswith("usage: oauth_state.py")
    assert "error: the command line was refused and nothing was changed. What was typed is not shown" in lines[-1]
    assert oauth_state.COMMAND_LINE_REFUSED in lines[-1]
    for typed in NEVER_ECHOED:
        assert typed not in captured.err, f"the error printed what was typed: {typed[:12]}"
    for stock in STOCK_MESSAGES:  # argparse's own wording is the one that repeats the offending text
        assert stock not in captured.err, stock


def test_an_error_in_a_subcommand_shows_the_usage_of_that_subcommand_and_the_hint_for_a_bad_id(capsys):
    assert oauth_state.main(["expire-access", "--client", LIVE_ID, "--apply"], clock=lambda: NOW) == oauth_state.EXIT_REFUSED
    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith("usage: oauth_state.py expire-access ")
    assert lines[-1].startswith("oauth_state.py expire-access: error: the command line was refused")
    assert "An id given to --client or --keep-client must be the first 8 characters of a client id" in lines[-1]
    assert LIVE_ID not in "\n".join(lines)


def test_only_a_bad_id_gets_the_hint_about_ids(capsys):
    for argv, wants_hint in ((["check", STRAY_TOKEN], False), (["revoke-all"], False), (["expire-access", "--client", "no", "--apply"], True)):
        assert oauth_state.main(argv, clock=lambda: NOW) == oauth_state.EXIT_REFUSED
        assert ("first 8 characters" in capsys.readouterr().err) is wants_hint, argv


def test_every_parser_the_script_builds_is_the_quiet_one():
    parser = oauth_state.build_parser()
    assert isinstance(parser, oauth_state.QuietParser)
    (subparsers,) = [action for action in parser._actions if isinstance(action.choices, dict)]  # the COMMAND choices
    assert sorted(subparsers.choices) == sorted(oauth_state.COMMANDS)
    assert all(isinstance(sub, oauth_state.QuietParser) for sub in subparsers.choices.values())


def test_a_token_pasted_into_the_wrong_place_is_not_echoed_when_the_script_runs_as_a_program(tmp_path):
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(tmp_path / "home")}
    for argv in (["check", STRAY_TOKEN], [STRAY_TOKEN], ["expire-access", "--client", "0a1b2c3d", "--apply", LIVE_ID]):
        done = subprocess.run(
            [sys.executable, str(SCRIPT), *argv], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180
        )
        assert done.returncode == 2 and done.stdout == "", done.stderr[-2000:]
        assert done.stderr.startswith("usage: oauth_state.py") and "What was typed is not shown" in done.stderr
        for typed in NEVER_ECHOED:
            assert typed not in done.stderr, typed[:12]
    assert list(tmp_path.iterdir()) == []  # and it created nothing


def test_help_names_every_command_and_exits_0(capsys):
    assert oauth_state.main(["--help"]) == oauth_state.EXIT_OK
    shown = capsys.readouterr().out
    for command in ("check", "inventory", "purge", "expire-access", "revoke-all"):
        assert command in shown


def test_the_default_state_directory_is_the_one_next_to_the_repository_root_and_the_dev_tree_has_none():
    assert REAL_DEFAULT_STATE_DIR == REPO_ROOT / ".oauth-state"
    assert not REAL_DEFAULT_STATE_DIR.exists()  # nothing in the source tree may hold real state (see test_hygiene)


def test_forgetting_the_state_directory_reaches_nothing_and_creates_nothing(tmp_path):
    missing = oauth_state.DEFAULT_STATE_DIR  # the patched default of this test: a path that does not exist
    assert not missing.exists()
    for argv in (["check"], ["inventory"], ["purge", "--stale", "--plan"], ["revoke-all", "--apply"]):
        run = invoke(*argv)
        assert run.code == oauth_state.EXIT_REFUSED and "does not exist" in run.err
    assert not missing.exists() and not missing.parent.joinpath(STATE_FILE).exists()


def test_it_runs_as_a_program_from_any_directory_and_prints_the_same_check(production, state_dir, tmp_path):
    # an operator runs `python scripts/oauth_state.py ...`: the script has to find oauth_store.py itself (sys.path gets scripts/)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(tmp_path / "home")}
    before = snapshot(state_dir)
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "check", "--state-dir", str(state_dir)],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=180,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "state: format=v1 clients=10 access=7 refresh=7" in done.stdout and "result: ok" in done.stdout
    assert snapshot(state_dir) == before and list(cwd.iterdir()) == []
    help_run = subprocess.run([sys.executable, str(SCRIPT), "--help"], cwd=cwd, env=env, capture_output=True, text=True, timeout=180)
    assert help_run.returncode == 0 and "revoke-all" in help_run.stdout
    refused = subprocess.run(
        [sys.executable, str(SCRIPT), "revoke-all", "--state-dir", str(state_dir)],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=180,
    )
    assert refused.returncode == 2 and snapshot(state_dir) == before  # no --apply: refused, nothing changed


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


def test_check_of_the_typical_state_file_gives_the_counts_the_service_will_log(production, state_dir):
    before = snapshot(state_dir)
    run = invoke("check", state_dir=state_dir)
    assert run.code == 0 and run.err == ""
    assert "state: format=v1 clients=10 access=7 refresh=7 tombstones=0 skipped=0 migrated=7 pruned_on_load=6" in run.out
    assert "lock: absent" in run.out and "result: ok" in run.out
    assert "file: mode=0600" in run.out and "hint:" not in run.out and "problem:" not in run.out
    assert snapshot(state_dir) == before  # it read, and wrote and created nothing: no .lock, no temp file, same bytes, same mtime
    assert LOCK_FILE not in os.listdir(state_dir)
    assert_no_credential(run, production)


def test_check_says_what_the_service_loads_for_every_kind_of_file(state_dir):
    builder = typical_state()
    builder.write(state_dir, version=2)
    assert "format=v2 clients=10 access=7 refresh=7" in invoke("check", state_dir=state_dir).out
    empty = state_dir.parent / "empty"
    empty.mkdir()
    run = invoke("check", state_dir=empty)
    assert run.code == 0 and "format=none clients=0 access=0 refresh=0" in run.out and "no state file" in run.out
    assert os.listdir(empty) == []


def test_check_of_a_missing_directory_or_a_file_is_refused_and_creates_nothing(tmp_path):
    for target in (tmp_path / "missing", tmp_path / "missing" / "deeper"):
        run = invoke("check", state_dir=target)
        assert run.code == oauth_state.EXIT_REFUSED and "does not exist" in run.err and "nothing was created" in run.err
        assert not target.exists()
    plain_file = tmp_path / "a-file"
    plain_file.write_text("x")
    assert invoke("check", state_dir=plain_file).code == oauth_state.EXIT_REFUSED
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["a-file"]


# Files that the store refuses. Each holds something that looks like a token, which must never be printed.
UNTRUSTED_CONTENT = [
    b'{"clients": {"pat_THIS_MUST_NOT_BE_PRINTED_0123456789abcdef": ',  # cut off
    b"not json at all pat_THIS_MUST_NOT_BE_PRINTED_0123456789abcdef",
    b'["pat_THIS_MUST_NOT_BE_PRINTED_0123456789abcdef"]',  # not an object
    b'{"version": 3, "note": "pat_THIS_MUST_NOT_BE_PRINTED_0123456789abcdef"}',  # a format this version does not know
    b'{"clients": "pat_THIS_MUST_NOT_BE_PRINTED_0123456789abcdef"}',  # a section of the wrong shape
]
UNTRUSTED_IDS = ["truncated", "not json", "a list", "unknown version", "wrong section"]


@pytest.mark.parametrize("content", UNTRUSTED_CONTENT, ids=UNTRUSTED_IDS)
@pytest.mark.parametrize("argv", [["inventory"], ["purge", "--stale", "--plan"], ["purge", "--stale", "--apply"], ["revoke-all", "--apply"]])
def test_a_file_that_cannot_be_trusted_stops_every_other_command_with_the_error_type_only(argv, content, state_dir):
    # (check is the exception: it still prints the file facts first, see the unreadable-file tests below)
    state_dir.mkdir()
    (state_dir / STATE_FILE).write_bytes(content)
    before = snapshot(state_dir)
    run = invoke(*argv, state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED
    assert "StateFileError" in run.err and run.out == ""
    assert "THIS_MUST_NOT_BE_PRINTED" not in run.text
    assert "(line" not in run.text and "it was not changed" not in run.text and str(state_dir) not in run.err  # not the store's own message
    after = snapshot(state_dir)
    after.pop(LOCK_FILE, None)  # a write command takes the lock first, which creates the (empty) lock file
    before.pop(LOCK_FILE, None)
    assert after == before  # the file itself is exactly as it was


OTHER_UID = 54321  # a user that does not own the test files (and, on most hosts, does not exist)


def make_unreadable(patched, path: Path) -> None:
    """Make reading `path` fail with PermissionError, as the operating system does for a user who may not read it. Root can read
    any file, so a test that must behave the same for every user patches the read instead of the mode. `patched` is a MonkeyPatch
    (use it inside `with monkeypatch.context() as patched:`, so that the patch is gone before the test looks at the directory)."""
    real = Path.read_bytes

    def read_bytes(self):
        if self == path:
            raise PermissionError(13, "Permission denied")
        return real(self)

    patched.setattr(Path, "read_bytes", read_bytes)


def make_owned_by(patched, path: Path, uid: int) -> None:
    """Make `path` report another owner to stat: what a copy made as root leaves, seen by the service user, without needing root."""
    real = Path.stat

    def stat_of(self, *args, **kwargs):
        info = real(self, *args, **kwargs)
        if self == path:
            fields = list(info)[:10]
            fields[stat.ST_UID] = uid
            return os.stat_result(fields)
        return info

    patched.setattr(Path, "stat", stat_of)


def refused_file(state_dir: Path, content: bytes = b"not json at all", mode: int = 0o600) -> Path:
    state_dir.mkdir(mode=0o700)
    path = state_dir / STATE_FILE
    path.write_bytes(content)
    os.chmod(path, mode)
    return path


@pytest.mark.parametrize("content", UNTRUSTED_CONTENT, ids=UNTRUSTED_IDS)
def test_check_of_a_file_the_store_refuses_still_prints_the_file_facts_and_the_lock_then_the_error_and_the_result(content, state_dir):
    """check stopped at the first StateFileError and printed only the generic error line: no `file:` facts, no lock line,
    no owner problem. The facts need no read of the file, so check now prints them, then the error (one stream, so the order holds
    when the output is piped), then `result: problems`. The message of the exception is still never printed."""
    path = refused_file(state_dir, content)
    before = snapshot(state_dir)
    run = invoke("check", state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED and run.err == ""
    owner = oauth_state._owner(path.stat().st_uid)
    assert run.out.splitlines() == [
        "oauth state check",
        f"dir: {state_dir}",
        "dir_writable: yes",
        "state: not read (the store refused the file)",
        f"file: mode=0600 owner={owner} dir_owner={owner}",
        "lock: absent",
        f"error: StateFileError: {oauth_state.STATE_FILE_UNUSABLE}",
        "result: problems",
    ]
    assert "THIS_MUST_NOT_BE_PRINTED" not in run.text and "(line" not in run.text and "it was not changed" not in run.text
    assert snapshot(state_dir) == before  # it read, and wrote and created nothing: no .lock, no temp file, same bytes, same mtime


def test_check_of_a_file_the_service_user_cannot_read_names_the_owner_problem_before_the_error(state_dir, monkeypatch):
    """A copy made as root leaves the state file owned by root, mode 0600, in the directory of
    gorelo-mcp, which then cannot read it. The owner repair of docs/TROUBLESHOOTING.md is for exactly that, and `problem: the state file is owned by
    another user than its directory` is the line that tells the operator to use it. check once printed none of it, only the generic error, so
    an unreadable file could not be told from a damaged one."""
    typical_state().write(state_dir)
    path = state_dir / STATE_FILE
    before = snapshot(state_dir)
    with monkeypatch.context() as patched:
        make_unreadable(patched, path)
        make_owned_by(patched, path, OTHER_UID)
        run = invoke("check", state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED and run.err == ""
    me = oauth_state._owner(state_dir.stat().st_uid)
    assert run.out.splitlines() == [
        "oauth state check",
        f"dir: {state_dir}",
        "dir_writable: yes",
        "state: not read (the store refused the file)",
        f"file: mode=0600 owner={oauth_state._owner(OTHER_UID)} dir_owner={me}",
        "lock: absent",
        "problem: the state file is owned by another user than its directory (the service may not be able to read it)",
        f"error: StateFileError: {oauth_state.STATE_FILE_UNUSABLE}",
        "result: problems",
    ]
    assert "hint:" not in run.out  # only the mode problem has a hint (the owner repair is in docs/TROUBLESHOOTING.md)
    assert snapshot(state_dir) == before


@pytest.mark.skipif(os.geteuid() != 0, reason="only root can give a file to another user")
def test_check_of_a_file_really_owned_by_another_user_that_cannot_be_read_names_the_owner_problem(state_dir, monkeypatch):
    # the same again, with a real chown (the owner is not simulated; only the refusal to read is, because root reads everything)
    typical_state().write(state_dir)
    path = state_dir / STATE_FILE
    try:
        os.chown(path, OTHER_UID, OTHER_UID)
    except OSError:
        pytest.skip("this environment does not allow chown")
    before = snapshot(state_dir)
    with monkeypatch.context() as patched:
        make_unreadable(patched, path)
        run = invoke("check", state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED
    assert line_of(run.out, "file: ") == f"file: mode=0600 owner={oauth_state._owner(OTHER_UID)} dir_owner={oauth_state._owner(0)}"
    lines = run.out.splitlines()
    problem = line_of(run.out, "problem: the state file is owned by another user than its directory")
    error = line_of(run.out, "error: ")
    assert lines.index(problem) < lines.index(error) == len(lines) - 2 and lines[-1] == "result: problems"
    assert snapshot(state_dir) == before


def test_check_of_a_refused_file_that_other_users_can_read_names_the_mode_problem_and_its_hint_before_the_error(state_dir):
    refused_file(state_dir, mode=0o644)
    before = snapshot(state_dir)
    run = invoke("check", state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED and run.err == ""
    lines = run.out.splitlines()
    problem = line_of(run.out, "problem: the state file can be read by other users")
    hint = line_of(run.out, "hint: ")
    error = line_of(run.out, "error: ")
    assert lines.index(hint) == lines.index(problem) + 1 and lines.index(error) == lines.index(hint) + 1  # problem, its hint, the error
    assert lines[-2:] == [error, "result: problems"] and line_of(run.out, "file: ").startswith("file: mode=0644")
    assert repair_in(hint) == ["runuser", "-u", "gorelo-mcp", "--", "chmod", "600", str(state_dir / STATE_FILE)]
    assert snapshot(state_dir) == before  # check repairs nothing itself


def test_check_of_a_refused_file_says_whether_the_service_holds_the_lock_and_whether_the_directory_is_writable(state_dir, monkeypatch):
    refused_file(state_dir)
    assert "lock: absent" in invoke("check", state_dir=state_dir).out
    lock = hold_lock(state_dir)
    try:
        run = invoke("check", state_dir=state_dir)
        assert run.code == oauth_state.EXIT_FAILED and "lock: held" in run.out  # the service is up, and its file is refused
    finally:
        lock.release()
    assert "lock: free" in invoke("check", state_dir=state_dir).out
    with monkeypatch.context() as patched:
        patched.setattr(os, "access", lambda *args, **kwargs: False)
        run = invoke("check", state_dir=state_dir)
    lines = run.out.splitlines()
    assert "dir_writable: no" in lines
    problem = line_of(run.out, "problem: this user cannot write to the state directory")
    assert lines.index(problem) < lines.index(line_of(run.out, "error: ")) and lines[-1] == "result: problems"


def test_a_held_lock_reported_by_the_store_still_ends_in_exit_status_3_for_check(production, state_dir, monkeypatch):
    # a held lock is not a fault of the file, so check does not handle it itself: StateLockedError still ends in main()'s exit status 3
    def locked(*args, **kwargs):
        raise oauth_store.StateLockedError("pat_THIS_MUST_NOT_BE_PRINTED_0123456789abcdef is held")

    monkeypatch.setattr(oauth_store, "load_state", locked)
    run = invoke("check", state_dir=state_dir)
    assert run.code == oauth_state.EXIT_LOCKED and run.out == "" and "StateLockedError" in run.err
    assert "THIS_MUST_NOT_BE_PRINTED" not in run.text


def test_check_reports_records_that_cannot_be_read_as_a_problem(state_dir):
    builder = typical_state()
    builder.write(state_dir)
    document = builder.document()
    document["clients"]["broken"] = {"client_id": "broken", "not": "a client"}
    state_dir.joinpath(STATE_FILE).write_text(json.dumps(document))
    run = invoke("check", state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED
    assert "skipped=1" in run.out and "problem: 1 records in the file cannot be read" in run.out and "result: problems" in run.out


def test_check_reports_a_state_file_that_other_users_can_read_as_a_problem(production, state_dir):
    os.chmod(state_dir / STATE_FILE, 0o644)
    run = invoke("check", state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED and "mode=0644" in run.out and "problem: the state file can be read by other users" in run.out


def repair_in(hint_line: str) -> list[str]:
    """The words of the command a `hint:` line ends with (the part after the heading in parentheses)."""
    prefix = 'hint: to repair it, run as root (docs/TROUBLESHOOTING.md, "Service will not start"): '
    assert hint_line.startswith(prefix), hint_line
    return shlex.split(hint_line[len(prefix):])


def test_check_of_the_typical_state_file_as_an_older_release_wrote_it_is_a_problem_with_the_repair_in_its_hint(state_dir):
    """Older releases write the state file with Path.write_text, which gives a NEW file the service's umask (0022):
    mode 0644. check still calls that a problem, because the file holds live credentials, it exits 1 (so an operator who runs check stops
    there), and the line after the problem is a hint with the one repair this script suggests. (A file at 0600 gets
    no hint; this is the file that is not.) check repairs nothing itself."""
    typical_state().write(state_dir, mode=0o644)
    before = snapshot(state_dir)
    run = invoke("check", state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED and run.err == ""
    assert "state: format=v1 clients=10 access=7 refresh=7" in run.out and "file: mode=0644" in run.out
    lines = run.out.splitlines()
    problem = line_of(run.out, "problem: the state file can be read by other users")
    assert "(its mode should be 0600)" in problem
    hint = line_of(run.out, "hint: ")
    assert lines.index(hint) == lines.index(problem) + 1  # the hint follows the problem it is for
    assert lines[-1] == "result: problems"
    assert repair_in(hint) == ["runuser", "-u", "gorelo-mcp", "--", "chmod", "600", str(state_dir / STATE_FILE)]
    assert hint.endswith(oauth_state.mode_repair_command(state_dir))
    assert oauth_state.SERVICE_USER == "gorelo-mcp"
    assert snapshot(state_dir) == before  # the file is still 0644: check only says what to do


def test_the_repair_in_the_hint_is_a_command_that_works_and_the_next_check_is_ok(state_dir):
    typical_state().write(state_dir, mode=0o644)
    command = repair_in(line_of(invoke("check", state_dir=state_dir).out, "hint: "))
    assert command[:4] == ["runuser", "-u", "gorelo-mcp", "--"]
    subprocess.run(command[4:], check=True, capture_output=True, timeout=60)  # the chmod itself: runuser needs root and the user
    assert stat.S_IMODE((state_dir / STATE_FILE).stat().st_mode) == 0o600
    run = invoke("check", state_dir=state_dir)
    assert run.code == 0 and "file: mode=0600" in run.out and "result: ok" in run.out
    assert "hint:" not in run.out and "problem:" not in run.out


def test_the_repair_names_an_absolute_path_and_quotes_it_when_the_shell_would_split_it(tmp_path, monkeypatch):
    spaced = tmp_path / "a directory with spaces" / "oauth-state"
    typical_state().write(spaced, mode=0o644)
    command = repair_in(line_of(invoke("check", state_dir=spaced).out, "hint: "))
    assert command[-1] == str(spaced / STATE_FILE)  # one word again after the shell has read it
    monkeypatch.chdir(spaced.parent)
    relative = repair_in(line_of(invoke("check", "--state-dir", "oauth-state").out, "hint: "))
    assert relative[-1] == str(spaced / STATE_FILE)  # the operator may run the command from anywhere


def test_only_the_mode_problem_has_a_hint(state_dir):
    builder = typical_state()
    builder.write(state_dir)
    document = builder.document()
    document["clients"]["broken"] = {"client_id": "broken", "not": "a client"}
    state_dir.joinpath(STATE_FILE).write_text(json.dumps(document))
    run = invoke("check", state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED and "problem: 1 records in the file cannot be read" in run.out
    assert "hint:" not in run.out  # a repair is named only where the script has one


def test_check_says_whether_this_user_can_write_to_the_directory_which_the_service_needs(production, state_dir, monkeypatch):
    run = invoke("check", state_dir=state_dir)
    assert run.code == 0 and "dir_writable: yes" in run.out
    seen = []

    def read_only(path, mode, *args, **kwargs):
        seen.append((Path(path), mode))
        return False

    with monkeypatch.context() as patched:
        patched.setattr(os, "access", read_only)
        run = invoke("check", state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED and "dir_writable: no" in run.out and "result: problems" in run.out
    assert "problem: this user cannot write to the state directory" in run.out
    assert (state_dir, os.W_OK | os.X_OK) in seen  # it asked about the directory, for writing and for entering it


def test_check_says_whether_the_service_holds_the_lock_and_creates_no_lock_file(production, state_dir):
    assert "lock: absent" in invoke("check", state_dir=state_dir).out
    assert LOCK_FILE not in os.listdir(state_dir)
    lock = hold_lock(state_dir)
    try:
        assert "lock: held" in invoke("check", state_dir=state_dir).out  # a running service: reading is still fine
    finally:
        lock.release()
    assert "lock: free" in invoke("check", state_dir=state_dir).out
    assert invoke("check", state_dir=state_dir).code == 0


def test_check_while_the_real_provider_runs_sees_the_lock_held_and_the_same_counts(production, state_dir):
    provider = PersonalAuthProvider(base_url=BASE_URL, password=PASSWORD, state_dir=str(state_dir), clock=lambda: float(NOW))
    try:
        run = invoke("check", state_dir=state_dir)
        assert run.code == 0 and "lock: held" in run.out
        assert "format=v1 clients=10 access=7 refresh=7" in run.out
    finally:
        provider.close()


# --------------------------------------------------------------------------
# inventory
# --------------------------------------------------------------------------


def test_inventory_lists_every_client_and_token_and_changes_nothing(production, state_dir):
    before = snapshot(state_dir)
    run = invoke("inventory", state_dir=state_dir)
    assert run.code == 0 and run.err == ""
    assert snapshot(state_dir) == before
    assert "clients (10):" in run.out and "access tokens (7):" in run.out and "refresh tokens (7):" in run.out
    live = line_of(run.out, "  client=0a1b2c3d ")
    assert 'name="Claude"' in live and "redirect_hosts=claude.ai" in live and "session=live" in live and "access=1 refresh=1" in live
    old = line_of(run.out, "  client=00000010 ")
    assert "session=refresh-only" in old
    tokenless = [line for line in run.out.splitlines() if line.startswith("  client=0000002")]
    assert len(tokenless) == 3 and all("session=tokenless" in line and "access=0 refresh=0" in line for line in tokenless)
    assert 'name="test"' in tokenless[0] and 'name="old-test-client"' in tokenless[1]
    access = [line for line in run.out.splitlines() if line.startswith("  type=pat_")]
    assert len(access) == 7 and sum("status=live" in line for line in access) == 1 and sum("status=prunable" in line for line in access) == 6
    assert any("client=0a1b2c3d" in line and "expires=" in line and f"{datetime.fromtimestamp(NOW + 13 * DAY, timezone.utc):%Y-%m-%d %H:%M}" in line for line in access)
    refresh = [line for line in run.out.splitlines() if line.startswith("  type=prt_")]
    assert len(refresh) == 7 and all("expires=never" in line and "origin=legacy" in line and "access=1" in line for line in refresh)


def test_inventory_is_metadata_only(production, state_dir):
    run = invoke("inventory", state_dir=state_dir)
    assert_no_credential(run, production)
    kinds = set(re.findall(r"type=(\S+)", run.out))
    assert kinds == {"pat_", "prt_"}
    # every line is one of a few known shapes: nothing else is ever printed
    shapes = (
        "oauth state inventory", "state: ", "clients (", "access tokens (", "refresh tokens (", "tombstones: ",
        "  client=", "  type=",
    )
    assert all(line.startswith(shapes) for line in run.out.splitlines())


def test_inventory_shows_the_family_only_when_the_file_stores_it(state_dir):
    builder = Builder()
    cid = builder.client(1)
    _, refresh = builder.pair(cid, "one", access_exp=NOW + DAY, issued=NOW - DAY)
    builder.write(state_dir, version=1)
    assert "family=-" in invoke("inventory", state_dir=state_dir).out  # a version 1 file has none: a load invents a new one each time
    builder.write(state_dir, version=2)
    shown = line_of(invoke("inventory", state_dir=state_dir).out, "  type=prt_")
    assert f"family={digest('one')[:8]}" in shown and "origin=code" in shown


def test_inventory_never_prints_a_token_whatever_its_shape(state_dir):
    builder = Builder()
    cid = builder.client(1)
    for label, access_prefix, refresh_prefix in (
        ("framework", "test_access_token_", "test_refresh_token_"),
        ("odd", "zzz_", "yyy_"),
        ("bare", "", ""),
    ):
        builder.pair(cid, label, access_exp=NOW + DAY, access_prefix=access_prefix, refresh_prefix=refresh_prefix)
    builder.write(state_dir)
    run = invoke("inventory", state_dir=state_dir)
    assert_no_credential(run, builder)
    types = re.findall(r"type=(\S+)", run.out)
    assert sorted(types) == sorted(["test_access_token_", "test_refresh_token_", "other", "other", "other", "other"])


@pytest.mark.parametrize(
    "name",
    [
        "line one\nline two",
        "\x1b[31mred\x1b[0m",
        "right to left \u202e override",
        "caf\u00e9 \u4e2d\u6587 \U0001f600",
        'quote " and backslash \\ and \'single\'',
        "x" * 500,
        "tab\there\rcarriage",
        "",
        None,
    ],
    ids=["newline", "ansi", "bidi", "unicode", "quotes", "long", "controls", "empty", "none"],
)
def test_a_client_name_cannot_put_anything_but_plain_text_in_the_output(name, state_dir):
    builder = Builder()
    builder.client(1, name)
    builder.write(state_dir)
    for argv in (["inventory"], ["purge", "--stale", "--plan"]):
        run = invoke(*argv, state_dir=state_dir)
        assert run.code == 0
        assert all(ch == "\n" or " " <= ch <= "~" for ch in run.out), "a character outside printable ASCII was printed"
        line = next(line for line in run.out.splitlines() if "client=00000001" in line)
        assert len(line) < 400 and line.count('"') == 2  # the name is one quoted field: no quote of its own got through
        if name and len(name) > 40:
            assert "x" * 41 not in line and "..." in line


# --------------------------------------------------------------------------
# Text that a client chose cannot stop a run
# --------------------------------------------------------------------------


def impostor_state() -> tuple[Builder, str]:
    """Registration is open, so anyone can register a client, read its id in the answer and register more clients that carry
    that id in the fields a client chooses: the name, and a redirect host (`https://<id>.claude.ai/cb` passes the redirect
    validator). Returns the state and the id that is copied, with a stale pair on it so that the purge has work to do."""
    builder = Builder()
    victim = builder.client(1, "Claude", registered=NOW - 100 * DAY)
    builder.pair(victim, "victim", access_exp=NOW - 50 * DAY, issued=NOW - 80 * DAY)
    builder.client(2, victim, registered=NOW - 90 * DAY)  # its name is the id of the first client
    builder.client(3, "Host", registered=NOW - 90 * DAY, uris=(f"https://{victim}.claude.ai/cb",))  # a redirect host holds it
    builder.client(4, builder.clients[victim].client_secret, registered=NOW - 90 * DAY)  # its name is that client's secret
    return builder, victim


def test_a_client_named_after_another_clients_id_does_not_stop_inventory_or_the_purge_plan(state_dir):
    builder, victim = impostor_state()
    builder.write(state_dir)
    before = snapshot(state_dir)
    inventory = invoke("inventory", state_dir=state_dir)
    assert inventory.code == 0 and inventory.err == ""  # it used to stop at the second client with "error: LeakError"
    assert 'client=00000002 name="[withheld]" ' in inventory.out and 'client=00000004 name="[withheld]" ' in inventory.out
    assert 'client=00000003 name="Host" registered=' in inventory.out and "redirect_hosts=[withheld].claude.ai " in inventory.out
    assert 'client=00000001 name="Claude" ' in inventory.out and "redirect_hosts=claude.ai " in inventory.out  # the others are as before
    # the lines after the impostors, which the attack was meant to hide, are all there
    assert "access tokens (1):" in inventory.out and "refresh tokens (1):" in inventory.out and "tombstones: 0" in inventory.out
    assert any(line.startswith("  type=pat_ ") for line in inventory.out.splitlines())
    assert any(line.startswith("  type=prt_ ") for line in inventory.out.splitlines())
    assert_no_credential(inventory, builder)
    assert victim not in inventory.text
    plan = invoke("purge", "--stale", "--plan", state_dir=state_dir)
    assert plan.code == 0 and plan.err == "" and "plan only: nothing was changed" in plan.out
    assert "totals: pairs=1 access_tokens=1 refresh_tokens=1 clients=4 kept_clients=0" in plan.out
    assert 'remove client: client=00000002 name="[withheld]" ' in plan.out
    assert 'remove client: client=00000003 name="Host" registered=' in plan.out and "redirect_hosts=[withheld].claude.ai" in plan.out
    assert_no_credential(plan, builder)
    assert snapshot(state_dir) == before


def test_the_purge_can_be_applied_over_clients_that_carry_another_clients_id(state_dir):
    builder, _ = impostor_state()
    builder.write(state_dir)
    run = invoke("purge", "--stale", "--apply", state_dir=state_dir)
    assert run.code == 0 and run.err == ""  # it used to exit 1 before removing anything
    assert "removed: access_tokens=1 refresh_tokens=1 clients=4" in run.out
    assert "written: format=v2 clients=0 access=0 refresh=0 verified=yes" in run.out
    assert_no_credential(run, builder)
    assert read_document(state_dir)["clients"] == {} and read_document(state_dir)["access_tokens"] == {}


def test_a_kept_client_whose_name_is_masked_is_listed_with_the_mask(state_dir):
    builder, _ = impostor_state()
    builder.write(state_dir)
    run = invoke("purge", "--stale", "--keep-client", "00000002", "--plan", state_dir=state_dir)
    assert run.code == 0 and run.err == ""
    assert 'keep client: client=00000002 name="[withheld]" why=named with --keep-client' in run.out
    assert "kept_clients=1" in run.out
    assert_no_credential(run, builder)


@pytest.mark.parametrize("kind", ["client id", "upper-case client id", "client secret", "access token", "refresh token", "tombstone"])
def test_every_kind_of_protected_value_inside_a_client_name_is_masked_where_it_stands(kind, state_dir):
    builder = Builder()
    victim = builder.client(1, "Victim")
    access, refresh = builder.pair(victim, "p", access_exp=NOW + DAY, issued=NOW - DAY)
    builder.retired[digest("rotated")] = {"family": "a" * 16, "retired_at": NOW - DAY}
    protected = {
        "client id": victim,
        "upper-case client id": victim.upper(),  # the match ignores case
        "client secret": builder.clients[victim].client_secret,
        "access token": access,
        "refresh token": refresh,
        "tombstone": digest("rotated"),
    }[kind]
    builder.client(2, f"before {protected} after")
    builder.write(state_dir, version=2)
    for argv in (["inventory"], ["purge", "--stale", "--plan"]):
        run = invoke(*argv, state_dir=state_dir)
        assert run.code == 0 and run.err == "", argv
        line = next(line for line in run.out.splitlines() if "client=00000002" in line)
        assert 'name="before [withheld] after"' in line
        assert protected not in run.text and protected.lower() not in run.text
        assert_no_credential(run, builder)
        assert digest("rotated") not in run.text


def test_a_redirect_host_that_holds_a_protected_value_is_masked_and_the_rest_of_the_host_is_shown(state_dir):
    builder = Builder()
    victim = builder.client(1, "Victim")
    builder.pair(victim, "p", access_exp=NOW + DAY, issued=NOW - DAY)
    token = next(iter(builder.access))
    builder.client(
        2,
        "Hosts",
        uris=(f"https://{victim}.claude.ai/cb", f"https://{victim.upper()}.example.invalid/x", "https://claude.ai/ok"),
    )
    # a host is cut at 40 characters like any other text; the id sits at its start, so a cut before the mask would keep all of it
    builder.client(3, "Long", uris=(f"https://{victim}.{'x' * 40}.claude.ai/cb",))
    builder.write(state_dir)
    run = invoke("inventory", state_dir=state_dir)
    assert run.code == 0 and run.err == ""
    line = line_of(run.out, "  client=00000002 ")
    assert "redirect_hosts=[withheld].claude.ai,[withheld].example.invalid,claude.ai " in line
    assert f"redirect_hosts=[withheld].{'x' * 29}... " in line_of(run.out, "  client=00000003 ")
    assert victim not in run.text and token not in run.text
    assert_no_credential(run, builder)


def test_masking_is_for_text_a_client_chose_a_line_built_from_a_secret_field_still_stops_the_run(state_dir, monkeypatch):
    builder = Builder()
    victim = builder.client(1, "Victim")
    builder.client(2, victim)  # a name that is masked, and a line that must still fail for another reason
    builder.write(state_dir)
    ok = invoke("inventory", state_dir=state_dir)
    assert ok.code == 0 and 'name="[withheld]"' in ok.out
    monkeypatch.setattr(oauth_state, "id8", lambda value: str(value))  # an edit that prints a whole id in the client= field
    run = invoke("inventory", state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED and "LeakError" in run.err
    assert victim not in run.text


def test_the_mask_replaces_every_protected_value_in_any_case_and_leaves_the_rest_alone():
    builder = Builder()
    cid = builder.client(1, cid="abcdef12-0000-4000-8000-000000000001")
    access, refresh = builder.pair(cid, "p", access_exp=NOW + DAY)
    builder.client(2, cid="short")  # an id of 8 characters or fewer is shown anyway, so it is not protected
    state = oauth_store.OAuthState(clients=builder.clients, access_tokens=builder.access, refresh_tokens=builder.refresh)
    guard = oauth_state.Guard()
    guard.protect(state)
    secret = builder.clients[cid].client_secret
    text = f"a {cid} b {cid.upper()} c {secret} d {access}{refresh} e short f"
    assert guard.mask(text) == "a [withheld] b [withheld] c [withheld] d [withheld][withheld] e short f"
    assert guard.mask("nothing to hide") == "nothing to hide" and guard.mask("") == ""
    assert not guard.holds_secret(guard.mask(text))
    assert oauth_state.safe_text(f"x {cid} y", guard=guard) == "x [withheld] y"
    assert oauth_state.safe_text(f"x {cid} y") == f"x {cid} y"  # without the guard nothing is masked: Output.line is what stops it


def test_a_text_that_a_replacement_would_turn_into_a_protected_value_is_withheld_whole():
    # the safety net of Guard.mask: after the replacements the text is checked again. Two values are made so that masking
    # the shorter one produces the longer one (nothing real looks like this; the point is that the check exists).
    longer = oauth_state.MASKED + "yyyyyy"
    shorter = "x" * 12
    state = oauth_store.OAuthState(
        access_tokens={
            token: AccessToken(token=token, client_id="c", scopes=[], expires_at=None) for token in (longer, shorter)
        }
    )
    guard = oauth_state.Guard()
    guard.protect(state)
    assert guard.mask(shorter + "yyyyyy") == oauth_state.MASKED
    assert guard.mask("a " + shorter + " b") == "a [withheld] b"


# --------------------------------------------------------------------------
# A protected value split by a character that prints as "?"
# --------------------------------------------------------------------------

# What safe_text prints as "?": an invisible character, a control character, a letter that is not ASCII; the quote and the
# backslash, which it never prints; and a "?" that was typed, which prints exactly like one that was made.
ZWSP = chr(0x200B)  # a zero-width space: nothing is shown, and safe_text prints it as "?"
SPLITTERS = {
    "zero-width space": ZWSP,
    "zero-width joiner": chr(0x200D),
    "soft hyphen": chr(0x00AD),
    "byte order mark": chr(0xFEFF),
    "right-to-left override": chr(0x202E),
    "NUL": "\x00",
    "escape": "\x1b",
    "newline": "\n",
    "tab": "\t",
    "letter that is not ASCII": chr(0x00E9),
    "quote": '"',
    "backslash": "\\",
    "typed question mark": "?",
}
PROTECTED_KINDS = ["client id", "upper-case client id", "client secret", "access token", "refresh token", "tombstone"]
SPLIT_ID = "abcdef12-0000-4000-8000-000000000001"


def victim_guard() -> tuple[oauth_state.Guard, str]:
    """A guard that protects the full id (36 characters), the secret and the two tokens of one client; returns it and the id."""
    builder = Builder()
    victim = builder.client(1, cid=SPLIT_ID)
    builder.pair(victim, "p", access_exp=NOW + DAY)
    guard = oauth_state.Guard()
    guard.protect(oauth_store.OAuthState(clients=builder.clients, access_tokens=builder.access, refresh_tokens=builder.refresh))
    return guard, victim


@pytest.mark.parametrize("kind", PROTECTED_KINDS)
@pytest.mark.parametrize("splitter", list(SPLITTERS.values()), ids=list(SPLITTERS))
def test_a_protected_value_split_by_a_character_that_prints_as_a_question_mark_is_withheld_with_the_whole_name(
    kind, splitter, state_dir
):
    """Anyone can register a client named after another client's id with a zero-width space in the middle of it. The
    mask looks for the exact id and finds none, and the printer turns the space into "?": a terminal got the other
    client's full id with one "?" in it. Now the printed text is checked once more with its question marks taken out, and the
    whole name is withheld. Every kind of protected value, and every kind of character that prints as "?"."""
    builder = Builder()
    victim = builder.client(1, "Victim")
    access, refresh = builder.pair(victim, "p", access_exp=NOW + DAY, issued=NOW - DAY)
    builder.retired[digest("rotated")] = {"family": "a" * 16, "retired_at": NOW - DAY}
    protected = {
        "client id": victim,
        "upper-case client id": victim.upper(),
        "client secret": builder.clients[victim].client_secret,
        "access token": access,
        "refresh token": refresh,
        "tombstone": digest("rotated"),
    }[kind]
    middle = len(protected) // 2
    builder.client(2, f"before {protected[:middle]}{splitter}{protected[middle:]} after")
    builder.write(state_dir, version=2)
    for argv in (["inventory"], ["purge", "--stale", "--plan"]):
        run = invoke(*argv, state_dir=state_dir)
        assert run.code == 0 and run.err == "", argv
        line = next(line for line in run.out.splitlines() if "client=00000002" in line)
        assert 'name="[withheld]"' in line, line  # the whole name, "before" and "after" included
        shown = run.text.lower()
        assert protected[:middle].lower() not in shown and protected[middle:].lower() not in shown  # no half of it either
        assert_no_credential(run, builder)


def test_a_zero_width_space_inside_another_clients_id_withholds_the_whole_name():
    guard, victim = victim_guard()
    split = victim[:20] + ZWSP + victim[20:]
    assert guard.mask(split) == split  # the exact-match replacement does not see it: that was the gap
    assert oauth_state.safe_text(split) == victim[:20] + "?" + victim[20:]  # with no guard a near-full id is what comes out
    assert oauth_state.safe_text(split, guard=guard) == oauth_state.MASKED
    assert oauth_state.safe_text(f"before {split} after", guard=guard) == oauth_state.MASKED  # the whole name, not only the value
    assert oauth_state.safe_text(split.upper(), guard=guard) == oauth_state.MASKED  # the check ignores case, like the mask
    assert oauth_state.safe_text(f"{victim} and {split}", guard=guard) == oauth_state.MASKED  # one plain, one split
    long_name = "x" * 10 + split  # 46 characters: the cut at 40 would leave 30 characters of the id on the screen
    assert oauth_state.safe_text(long_name, guard=guard) == oauth_state.MASKED  # looked at before the cut, like the mask


def test_text_that_hides_no_protected_value_is_printed_as_before_even_with_question_marks_and_invisible_characters():
    guard, victim = victim_guard()
    assert oauth_state.safe_text("Cl" + ZWSP + "aude", guard=guard) == "Cl?aude"
    assert oauth_state.safe_text("what? really?", guard=guard) == "what? really?"
    assert oauth_state.safe_text("right to left " + chr(0x202E) + " override", guard=guard) == "right to left ? override"
    assert oauth_state.safe_text(f"x {victim} y", guard=guard) == "x [withheld] y"  # a plain value is replaced where it stands
    assert oauth_state.safe_text(victim[:20] + "?", guard=guard) == victim[:20] + "?"  # a piece of an id is not the id
    assert oauth_state.safe_text(None, guard=guard) == ""


def test_hides_secret_looks_only_when_a_question_mark_is_there_and_ignores_case():
    guard, victim = victim_guard()
    split = victim[:20] + "?" + victim[20:]
    assert guard.hides_secret(split) and guard.hides_secret(split.upper()) and guard.hides_secret("??" + split + "??")
    assert not guard.hides_secret("no question mark in this one") and not guard.hides_secret("what? why?")
    assert not guard.hides_secret(victim)  # no "?" to take out: a plain value is for mask() to replace, and it has by then
    assert not oauth_state.Guard().hides_secret(split)  # a guard that protects nothing hides nothing


def test_a_redirect_host_with_a_value_split_by_an_invisible_character_is_withheld_whole():
    # a redirect address is normalized when it is loaded, so this cannot come from a state file today; the host goes through
    # the same function as the name, and it is defended the same way
    guard, victim = victim_guard()
    client = types.SimpleNamespace(redirect_uris=[f"https://{victim[:20]}{ZWSP}{victim[20:]}.claude.ai/cb", "https://claude.ai/ok"])
    assert oauth_state.redirect_hosts(client, guard) == "[withheld],claude.ai"


def test_the_purge_can_be_applied_over_a_client_named_with_a_split_id(state_dir):
    builder = Builder()
    victim = builder.client(1, "Claude", registered=NOW - 100 * DAY)
    builder.pair(victim, "victim", access_exp=NOW - 50 * DAY, issued=NOW - 80 * DAY)
    builder.client(2, victim[:20] + ZWSP + victim[20:], registered=NOW - 90 * DAY)
    builder.write(state_dir)
    run = invoke("purge", "--stale", "--apply", state_dir=state_dir)
    assert run.code == 0 and run.err == ""
    assert 'remove client: client=00000002 name="[withheld]" ' in run.out
    assert "removed: access_tokens=1 refresh_tokens=1 clients=2" in run.out
    assert victim[:20] not in run.text and victim[20:] not in run.text
    assert_no_credential(run, builder)


def test_only_the_host_of_a_redirect_address_is_shown(state_dir):
    builder = Builder()
    builder.client(1, uris=("https://user:hunter2@evil.example:8443/cb?code=SECRETCODE&state=abc#frag",))
    builder.client(2, uris=("https://claude.ai/a", "https://claude.com/b", "http://localhost:6274/c", "http://127.0.0.1:9/d", "https://example.invalid/e"))
    builder.write(state_dir)
    run = invoke("inventory", state_dir=state_dir)
    assert "redirect_hosts=evil.example " in run.out
    for leaked in ("hunter2", "user", "SECRETCODE", "8443", "frag", "/cb", "state=abc"):
        assert leaked not in run.out, leaked
    assert "redirect_hosts=127.0.0.1,claude.ai,claude.com,+2 " in run.out  # at most three, the rest counted


def test_an_expiry_that_is_not_a_time_is_shown_as_invalid_not_as_a_crash(state_dir):
    builder = Builder()
    cid = builder.client(1)
    builder.pair(cid, "far", access_exp=10**30)
    builder.write(state_dir)
    run = invoke("inventory", state_dir=state_dir)
    assert run.code == 0 and "expires=invalid" in run.out


def test_inventory_counts_the_tombstones_and_shows_their_dates_only(state_dir):
    builder = Builder()
    builder.client(1)
    builder.retired[digest("old-one")] = {"family": "a" * 16, "retired_at": NOW - 10 * DAY}
    builder.retired[digest("old-two")] = {"family": "b" * 16, "retired_at": NOW - 2 * DAY}
    builder.write(state_dir, version=2)
    run = invoke("inventory", state_dir=state_dir)
    assert "tombstones: 2 (oldest=" in run.out and digest("old-one") not in run.text and digest("old-two") not in run.text


# --------------------------------------------------------------------------
# purge --stale: the plan
# --------------------------------------------------------------------------


def test_the_plan_names_the_six_old_pairs_and_nine_clients_and_changes_nothing(production, state_dir):
    before = snapshot(state_dir)
    run = invoke("purge", "--stale", "--plan", state_dir=state_dir)
    assert run.code == 0 and run.err == ""
    assert snapshot(state_dir) == before
    assert "totals: pairs=6 access_tokens=6 refresh_tokens=6 clients=9 kept_clients=1" in run.out
    assert "plan only: nothing was changed" in run.out
    removed_pairs = re.findall(r"  remove pair: client=(\w+) ", run.out)
    assert sorted(removed_pairs) == sorted(oauth_state.id8(cid) for cid in OLD_SESSIONS)
    removed_clients = re.findall(r"  remove client: client=(\w+) ", run.out)
    assert sorted(removed_clients) == sorted(oauth_state.id8(cid) for cid in [*OLD_SESSIONS, *TOKENLESS])
    assert "  keep client: client=0a1b2c3d " in run.out and "why=has a valid access token" in run.out
    assert "0a1b2c3d" not in "".join(line for line in run.out.splitlines() if "remove" in line)
    assert_no_credential(run, production)


def test_the_plan_needs_no_lock_and_works_while_the_service_holds_it(production, state_dir):
    lock = hold_lock(state_dir)
    try:
        before = snapshot(state_dir)
        run = invoke("purge", "--stale", "--plan", state_dir=state_dir)
        assert run.code == 0 and "pairs=6" in run.out
        assert snapshot(state_dir) == before
    finally:
        lock.release()


def test_a_live_pair_is_never_planned_whatever_its_age_or_its_refresh_token(state_dir):
    builder = Builder()
    ancient = builder.client(1, registered=NOW - 900 * DAY)
    builder.pair(ancient, "ancient-but-live", access_exp=NOW + 1, issued=NOW - 800 * DAY)  # valid for one more second
    forever = builder.client(2, registered=NOW - 900 * DAY)
    builder.pair(forever, "no-expiry", access_exp=None, issued=NOW - 800 * DAY)  # an access token that never expires
    odd = builder.client(3, registered=NOW - 900 * DAY)
    builder.pair(odd, "live-access-dead-refresh", access_exp=NOW + DAY, refresh_exp=NOW - DAY, issued=NOW - 800 * DAY)
    alone = builder.client(4, registered=NOW - 900 * DAY)
    builder.lone_access(alone, "alone", expires=NOW + DAY)  # a valid access token with no refresh token at all
    builder.write(state_dir, version=2)
    plan = oauth_state.plan_stale(loaded(state_dir).state, NOW)
    assert plan.pairs == [] and plan.orphans == [] and plan.clients == [] and plan.empty
    assert set(plan.kept) == set(builder.clients)
    run = invoke("purge", "--stale", "--plan", state_dir=state_dir)
    assert "totals: pairs=0 access_tokens=0 refresh_tokens=0 clients=0 kept_clients=4" in run.out


def test_a_token_that_expires_exactly_now_is_still_live(state_dir):
    builder = Builder()
    cid = builder.client(1, registered=NOW - 900 * DAY)
    builder.pair(cid, "edge", access_exp=NOW, issued=NOW - 800 * DAY)  # the service refuses it only when expires_at < now
    builder.write(state_dir, version=2)
    assert oauth_state.plan_stale(loaded(state_dir).state, NOW).empty
    assert [p.client_id for p in oauth_state.plan_stale(loaded(state_dir).state, NOW + 1).pairs] == [cid]


def test_the_age_of_the_refresh_token_decides_for_a_pair_without_a_valid_access_token(state_dir):
    limit = oauth_state.STALE_AFTER_SECONDS
    builder = Builder()
    edge = builder.client(1, registered=NOW - 900 * DAY)
    builder.pair(edge, "exactly-at-the-limit", access_exp=NOW - 5 * DAY, issued=NOW - limit)
    over = builder.client(2, registered=NOW - 900 * DAY)
    builder.pair(over, "one-second-over", access_exp=NOW - 5 * DAY, issued=NOW - limit - 1)
    recent = builder.client(3, registered=NOW - 900 * DAY)
    builder.pair(recent, "recent", access_exp=NOW - 1 * DAY, issued=NOW - 2 * DAY)
    future = builder.client(4, registered=NOW - 900 * DAY)
    builder.pair(future, "clock-skew", access_exp=NOW - 1 * DAY, issued=NOW + 5 * DAY)
    builder.write(state_dir, version=2)
    plan = oauth_state.plan_stale(loaded(state_dir).state, NOW)
    assert [pair.client_id for pair in plan.pairs] == [over]
    assert plan.clients == [over]
    assert {edge, recent, future} <= set(plan.kept) and plan.kept[recent] == "has tokens that are not stale"


def test_a_pair_whose_refresh_token_has_expired_is_stale_even_when_it_is_recent(state_dir):
    builder = Builder()
    cid = builder.client(1, registered=NOW - 3 * DAY)
    builder.pair(cid, "dead", access_exp=NOW - 2 * DAY, refresh_exp=NOW - 1 * DAY, issued=NOW - 3 * DAY)
    builder.write(state_dir, version=2)
    plan = oauth_state.plan_stale(loaded(state_dir).state, NOW)
    assert [pair.client_id for pair in plan.pairs] == [cid] and "expired" in plan.pairs[0].why


def test_a_legacy_pair_is_dated_from_its_access_token_as_the_store_estimates_it(state_dir):
    # a version 1 file has no family metadata: pat_ lived 30 days, so a pair whose pat_ expired 40 days ago was issued 70 days ago
    builder = Builder()
    old = builder.client(1, registered=NOW - 900 * DAY)
    builder.pair(old, "old", access_exp=NOW - 40 * DAY)
    framework = builder.client(2, registered=NOW - 900 * DAY)
    builder.pair(framework, "framework", access_exp=NOW - 10 * DAY, access_prefix="test_access_token_", refresh_prefix="test_refresh_token_")
    unknown = builder.client(3, registered=NOW - 900 * DAY)
    builder.pair(unknown, "unknown-shape", access_exp=NOW - 400 * DAY, access_prefix="zzz_", refresh_prefix="yyy_")
    builder.write(state_dir, version=1)
    plan = oauth_state.plan_stale(loaded(state_dir).state, NOW)
    assert [pair.client_id for pair in plan.pairs] == [old]  # the other two cannot be shown to be old: they stay
    assert "issued 70 days ago" in plan.pairs[0].why


def test_a_client_named_to_keep_loses_nothing_and_a_keep_list_typo_is_refused(production, state_dir):
    keep = oauth_state.id8(OLD_SESSIONS[2])
    plan = oauth_state.plan_stale(loaded(state_dir).state, NOW, [keep])
    assert OLD_SESSIONS[2] not in plan.clients and OLD_SESSIONS[2] not in {pair.client_id for pair in plan.pairs}
    assert plan.kept[OLD_SESSIONS[2]] == "named with --keep-client" and len(plan.pairs) == 5 and len(plan.clients) == 8
    before = snapshot(state_dir)
    for mode in ("--plan", "--apply"):
        run = invoke("purge", "--stale", "--keep-client", keep, "--keep-client", "deadbeef", mode, state_dir=state_dir)
        assert run.code == oauth_state.EXIT_REFUSED and "--keep-client deadbeef matches no client" in run.err
        assert run.out == ""
    after = snapshot(state_dir)
    after.pop(LOCK_FILE, None)  # --apply took the lock before it looked at the list
    assert after == before


def test_keep_client_takes_several_ids_in_either_form(production, state_dir):
    a, b = oauth_state.id8(OLD_SESSIONS[0]), oauth_state.id8(OLD_SESSIONS[1])
    for argv in (["--keep-client", a, b], ["--keep-client", a, "--keep-client", b]):
        run = invoke("purge", "--stale", *argv, "--plan", state_dir=state_dir)
        assert run.code == 0 and "pairs=4 " in run.out and "kept_clients=3" in run.out


def test_a_kept_id_covers_every_client_that_shares_it(state_dir):
    builder = Builder()
    first = builder.client(1, cid="abcd1234-0000-4000-8000-000000000001", registered=NOW - 900 * DAY)
    second = builder.client(2, cid="abcd1234-0000-4000-8000-000000000002", registered=NOW - 900 * DAY)
    builder.write(state_dir)
    plan = oauth_state.plan_stale(loaded(state_dir).state, NOW, ["abcd1234"])
    assert plan.clients == [] and set(plan.kept) == {first, second}


def test_a_client_without_a_token_is_stale_after_the_grace_and_not_before(state_dir):
    grace = oauth_state.NEW_CLIENT_GRACE_SECONDS
    builder = Builder()
    old = builder.client(1, "old", registered=NOW - grace - 1)
    edge = builder.client(2, "edge", registered=NOW - grace)
    new = builder.client(3, "new", registered=NOW - 2 * 3600)
    unknown = builder.client(4, "unknown age", registered=None)
    builder.write(state_dir)
    plan = oauth_state.plan_stale(loaded(state_dir).state, NOW)
    assert sorted(plan.clients) == sorted([old, unknown])
    assert plan.kept[edge] == plan.kept[new] == "no token yet, registered less than 24 hours ago"


def test_a_client_with_a_live_and_a_stale_pair_loses_only_the_stale_pair(state_dir):
    builder = Builder()
    cid = builder.client(1, registered=NOW - 300 * DAY)
    builder.pair(cid, "old", access_exp=NOW - 200 * DAY, issued=NOW - 230 * DAY)
    live_access, live_refresh = builder.pair(cid, "new", access_exp=NOW + 5 * DAY, issued=NOW - 25 * DAY)
    builder.write(state_dir, version=2)
    before = loaded(state_dir).state
    plan = oauth_state.plan_stale(before, NOW)
    assert len(plan.pairs) == 1 and plan.clients == [] and plan.kept[cid] == "has a valid access token"
    oauth_state.apply_stale(before, plan)
    assert set(before.access_tokens) == {live_access} and set(before.refresh_tokens) == {live_refresh} and cid in before.clients


def test_an_access_token_without_a_refresh_token_goes_only_after_the_week_the_store_keeps_expired_tokens(state_dir):
    retention = oauth_store.EXPIRED_TOKEN_RETENTION_SECONDS
    builder = Builder()
    cid = builder.client(1, registered=NOW - 300 * DAY)
    old = builder.lone_access(cid, "old", expires=NOW - retention - 1)
    recent = builder.lone_access(cid, "recent", expires=NOW - retention)
    valid = builder.lone_access(cid, "valid", expires=NOW + DAY)
    kept = builder.client(2, registered=NOW - 300 * DAY)
    builder.lone_access(kept, "kept-old", expires=NOW - 100 * DAY)
    builder.write(state_dir)
    plan = oauth_state.plan_stale(loaded(state_dir).state, NOW, [oauth_state.id8(kept)])
    assert [orphan.token for orphan in plan.orphans] == [old]
    assert recent not in {o.token for o in plan.orphans} and valid not in {o.token for o in plan.orphans}
    assert plan.clients == [] and plan.kept[cid] == "has a valid access token"


def test_apply_never_removes_a_client_that_holds_a_token_even_when_the_plan_lists_it(state_dir):
    builder = Builder()
    cid = builder.client(1)
    access, refresh = builder.pair(cid, "live", access_exp=NOW + DAY, issued=NOW - DAY)
    builder.write(state_dir, version=2)
    state = loaded(state_dir).state
    wrong = oauth_state.StalePlan(clients=[cid])  # a plan that is wrong about this client
    assert oauth_state.apply_stale(state, wrong) == (0, 0, 0)
    assert cid in state.clients and access in state.access_tokens and refresh in state.refresh_tokens


def test_the_plan_is_pure(production, state_dir):
    state = loaded(state_dir).state
    dumped = oauth_store.dump_state(state)
    oauth_state.plan_stale(state, NOW, [oauth_state.id8(OLD_SESSIONS[0])])
    assert oauth_store.dump_state(state) == dumped


# --------------------------------------------------------------------------
# purge --stale --apply
# --------------------------------------------------------------------------


def test_apply_removes_the_planned_sessions_and_keeps_the_live_one_value_for_value(production, state_dir):
    original = production.document()
    run = invoke("purge", "--stale", "--apply", state_dir=state_dir)
    assert run.code == 0 and run.err == ""
    assert "removed: access_tokens=6 refresh_tokens=6 clients=9" in run.out
    assert "written: format=v2 clients=1 access=1 refresh=1 verified=yes" in run.out
    document = read_document(state_dir)
    assert document["version"] == 2
    assert set(document["clients"]) == {LIVE_ID} and len(document["access_tokens"]) == 1 and len(document["refresh_tokens"]) == 1
    for section in ("clients", "access_tokens", "refresh_tokens", "a2r", "r2a"):  # the survivor is exactly what it was
        for key, value in document[section].items():
            assert original[section][key] == value, section
    (refresh_token,) = document["refresh_tokens"]
    assert document["refresh_meta"][refresh_token]["origin"] == "legacy" and document["retired"] == {}
    assert_no_credential(run, production)


def test_apply_leaves_a_clean_directory_a_private_file_and_no_lock_held(production, state_dir):
    assert invoke("purge", "--stale", "--apply", state_dir=state_dir).code == 0
    assert sorted(os.listdir(state_dir)) == sorted([STATE_FILE, LOCK_FILE])  # no temporary file
    assert stat.S_IMODE((state_dir / STATE_FILE).stat().st_mode) == 0o600
    assert stat.S_IMODE((state_dir / LOCK_FILE).stat().st_mode) == 0o600
    again = oauth_store.StateLock(state_dir).acquire()  # the script let go of the lock
    again.release()


def test_a_second_apply_has_nothing_to_do_and_writes_nothing(production, state_dir):
    assert invoke("purge", "--stale", "--apply", state_dir=state_dir).code == 0
    before = snapshot(state_dir)
    run = invoke("purge", "--stale", "--apply", state_dir=state_dir)
    assert run.code == 0 and "nothing to purge: nothing was changed" in run.out and "written:" not in run.out
    assert snapshot(state_dir) == before


def test_apply_with_a_keep_list_keeps_that_client_with_its_pair(production, state_dir):
    keep_id = OLD_SESSIONS[3]
    keep_access = next(t for t, r in production.access.items() if r.client_id == keep_id)
    keep_refresh = next(t for t, r in production.refresh.items() if r.client_id == keep_id)
    run = invoke("purge", "--stale", "--keep-client", oauth_state.id8(keep_id), "--apply", state_dir=state_dir)
    assert run.code == 0 and "removed: access_tokens=5 refresh_tokens=5 clients=8" in run.out
    after = loaded(state_dir).state
    assert set(after.clients) == {LIVE_ID, keep_id}
    assert keep_access in after.access_tokens and keep_refresh in after.refresh_tokens
    assert after.refresh_tokens[keep_refresh] == production.refresh[keep_refresh]  # unchanged, token for token


def test_apply_while_the_service_holds_the_lock_is_refused_and_changes_nothing(production, state_dir):
    lock = hold_lock(state_dir)
    try:
        before = snapshot(state_dir)
        for argv in (["purge", "--stale", "--apply"], ["expire-access", "--client", "0a1b2c3d", "--apply"], ["revoke-all", "--apply"]):
            run = invoke(*argv, state_dir=state_dir)
            assert run.code == oauth_state.EXIT_LOCKED and run.out == ""
            assert "holds the OAuth state lock" in run.err and "stop the service" in run.err and "Nothing was changed" in run.err
            assert snapshot(state_dir) == before
    finally:
        lock.release()
    assert invoke("purge", "--stale", "--apply", state_dir=state_dir).code == 0  # and once it lets go, it works


def test_apply_while_the_real_provider_is_up_is_refused(production, state_dir):
    provider = PersonalAuthProvider(base_url=BASE_URL, password=PASSWORD, state_dir=str(state_dir), clock=lambda: float(NOW))
    try:
        before = snapshot(state_dir)
        run = invoke("revoke-all", "--apply", state_dir=state_dir)
        assert run.code == oauth_state.EXIT_LOCKED and snapshot(state_dir) == before
    finally:
        provider.close()
    assert invoke("revoke-all", "--apply", state_dir=state_dir).code == 0


def test_apply_refuses_a_file_that_holds_records_it_could_not_read_and_changes_nothing(production, state_dir):
    document = production.document()
    document["access_tokens"]["junk"] = {"token": "junk"}
    (state_dir / STATE_FILE).write_text(json.dumps(document))
    before = snapshot(state_dir)
    for argv in (["purge", "--stale", "--apply"], ["expire-access", "--client", "0a1b2c3d", "--apply"]):
        run = invoke(*argv, state_dir=state_dir)
        assert run.code == oauth_state.EXIT_REFUSED and "cannot be read" in run.err
    after = snapshot(state_dir)
    after.pop(LOCK_FILE, None)
    assert after == before
    assert invoke("purge", "--stale", "--plan", state_dir=state_dir).code == 0  # looking is still fine


def test_apply_on_a_directory_without_a_state_file_creates_none(state_dir):
    state_dir.mkdir()
    for argv in (["purge", "--stale", "--apply"], ["expire-access", "--client", "0a1b2c3d", "--apply"], ["revoke-all", "--apply"]):
        run = invoke(*argv, state_dir=state_dir)
        assert run.code == oauth_state.EXIT_REFUSED and "no state file" in run.err
        assert STATE_FILE not in os.listdir(state_dir)
    plan = invoke("purge", "--stale", "--plan", state_dir=state_dir)
    assert plan.code == 0 and "totals: pairs=0" in plan.out and os.listdir(state_dir) == []


def test_after_a_purge_the_real_provider_serves_the_live_session_and_does_not_know_the_rest(production, state_dir):
    assert invoke("purge", "--stale", "--apply", state_dir=state_dir).code == 0
    live_access = next(t for t, r in production.access.items() if r.client_id == LIVE_ID)
    live_refresh = next(t for t, r in production.refresh.items() if r.client_id == LIVE_ID)
    gone_access = next(t for t, r in production.access.items() if r.client_id == OLD_SESSIONS[0])
    gone_refresh = next(t for t, r in production.refresh.items() if r.client_id == OLD_SESSIONS[0])
    provider = PersonalAuthProvider(base_url=BASE_URL, password=PASSWORD, state_dir=str(state_dir), clock=lambda: float(NOW))
    try:
        assert set(provider.clients) == {LIVE_ID}
        assert asyncio.run(provider.load_access_token(live_access)) is not None
        assert asyncio.run(provider.load_access_token(gone_access)) is None
        live_client = provider.clients[LIVE_ID]
        assert asyncio.run(provider.load_refresh_token(live_client, live_refresh)) is not None
        assert asyncio.run(provider.load_refresh_token(live_client, gone_refresh)) is None
    finally:
        provider.close()


# --------------------------------------------------------------------------
# expire-access: the refresh test
# --------------------------------------------------------------------------


def test_expire_access_marks_only_that_clients_valid_access_tokens(state_dir):
    builder = typical_state()
    other = builder.client(0x30, "Other", registered=NOW - 3 * DAY)
    other_access, other_refresh = builder.pair(other, "other", access_exp=NOW + 2 * DAY, issued=NOW - 3 * DAY)
    second_access, second_refresh = builder.pair(LIVE_ID, "second", access_exp=NOW + 9 * DAY, issued=NOW - DAY)
    already = builder.lone_access(LIVE_ID, "already-expired", expires=NOW - 3 * DAY)
    live_access = next(t for t, r in builder.access.items() if r.client_id == LIVE_ID and t.endswith(digest("live-access")))
    builder.write(state_dir)
    run = invoke("expire-access", "--client", "0a1b2c3d", "--apply", state_dir=state_dir)
    assert run.code == 0 and run.err == ""
    assert "expired: client=0a1b2c3d access_tokens=2 expired_at=2026-10-05 11:59:59; refresh tokens were not touched" in run.out
    assert "written: format=v2 clients=11 access=" in run.out
    after = loaded(state_dir).state
    assert after.access_tokens[live_access].expires_at == NOW - 1
    assert after.access_tokens[second_access].expires_at == NOW - 1
    assert after.access_tokens[already].expires_at == NOW - 3 * DAY  # an expiry in the past is left as it was
    assert after.access_tokens[other_access].expires_at == NOW + 2 * DAY  # another client's token
    for token, record in typical_state().access.items():
        if record.client_id != LIVE_ID:
            assert after.access_tokens[token].expires_at == record.expires_at
    for token, record in builder.refresh.items():
        assert after.refresh_tokens[token] == record  # no refresh token changed, and none was removed
    assert other_refresh in after.refresh_tokens and second_refresh in after.refresh_tokens
    assert_no_credential(run, builder)


def test_the_refresh_test_end_to_end_through_the_real_provider(production, state_dir, caplog):
    # the operator procedure: stop the service, expire the live session's access token, start the service, the client refreshes
    access = next(t for t, r in production.access.items() if r.client_id == LIVE_ID)
    refresh = next(t for t, r in production.refresh.items() if r.client_id == LIVE_ID)
    run = invoke("expire-access", "--client", "0a1b2c3d", "--apply", state_dir=state_dir)
    assert run.code == 0
    caplog.set_level(logging.INFO)
    provider = PersonalAuthProvider(base_url=BASE_URL, password=PASSWORD, state_dir=str(state_dir), clock=lambda: float(NOW))
    try:
        client = provider.clients[LIVE_ID]
        assert asyncio.run(provider.load_access_token(access)) is None  # the request is refused as expired ...
        stored = asyncio.run(provider.load_refresh_token(client, refresh))
        assert stored is not None  # ... and the refresh token is still there
        token = asyncio.run(provider.exchange_refresh_token(client, stored, stored.scopes))
        assert asyncio.run(provider.load_access_token(token.access_token)) is not None  # the new access token works
        assert token.access_token != access and token.refresh_token != refresh  # rotated
    finally:
        provider.close()
    messages = [record.getMessage() for record in caplog.records]
    assert any("access outcome=expired" in message for message in messages)
    assert any("token outcome=refreshed" in message for message in messages)
    assert not any("consent" in message for message in messages)  # no sign-in page was involved
    assert not any(secret in message for secret in production.secrets() for message in messages)


def test_expire_access_refuses_a_client_that_could_not_refresh(state_dir):
    builder = Builder()
    no_refresh = builder.client(1)
    builder.lone_access(no_refresh, "alone", expires=NOW + DAY)
    dead_refresh = builder.client(2)
    builder.pair(dead_refresh, "dead", access_exp=NOW + DAY, refresh_exp=NOW - DAY)
    builder.write(state_dir)
    before = snapshot(state_dir)
    for n in (1, 2):
        run = invoke("expire-access", "--client", oauth_state.id8(client_id_of(n)), "--apply", state_dir=state_dir)
        assert run.code == oauth_state.EXIT_REFUSED and "no usable refresh token" in run.err and "sign in again" in run.err
    after = snapshot(state_dir)
    after.pop(LOCK_FILE, None)
    assert after == before


def test_expire_access_refuses_an_unknown_and_an_ambiguous_client(state_dir):
    builder = Builder()
    first = builder.client(1, cid="abcd1234-0000-4000-8000-000000000001")
    second = builder.client(2, cid="abcd1234-0000-4000-8000-000000000002")
    builder.pair(first, "one", access_exp=NOW + DAY)
    builder.pair(second, "two", access_exp=NOW + DAY)
    builder.write(state_dir)
    before = snapshot(state_dir)
    unknown = invoke("expire-access", "--client", "ffffffff", "--apply", state_dir=state_dir)
    assert unknown.code == oauth_state.EXIT_REFUSED and "matches no client" in unknown.err
    ambiguous = invoke("expire-access", "--client", "abcd1234", "--apply", state_dir=state_dir)
    assert ambiguous.code == oauth_state.EXIT_REFUSED and "matches more than one client" in ambiguous.err
    after = snapshot(state_dir)
    after.pop(LOCK_FILE, None)
    assert after == before


def test_expire_access_on_a_client_that_has_nothing_valid_changes_nothing(state_dir):
    builder = Builder()
    cid = builder.client(1)
    builder.pair(cid, "expired", access_exp=NOW - DAY, issued=NOW - 2 * DAY)
    builder.write(state_dir, version=2)
    before = snapshot(state_dir)
    run = invoke("expire-access", "--client", oauth_state.id8(cid), "--apply", state_dir=state_dir)
    assert run.code == 0 and "has no valid access token" in run.out and "written:" not in run.out
    after = snapshot(state_dir)
    after.pop(LOCK_FILE, None)
    assert after == before


def test_a_token_expired_by_the_script_is_recent_enough_to_be_recognized_as_expired(production, state_dir):
    assert invoke("expire-access", "--client", "0a1b2c3d", "--apply", state_dir=state_dir).code == 0
    result = oauth_store.load_state(state_dir, now=NOW)  # a normal load, with the prune
    assert result.pruned.total == 6 and len(result.state.access_tokens) == 1  # only the six old tokens go, not this one
    assert result.state.access_tokens and next(iter(result.state.access_tokens.values())).expires_at == NOW - 1


# --------------------------------------------------------------------------
# revoke-all
# --------------------------------------------------------------------------


def test_revoke_all_removes_every_token_and_keeps_clients_and_tombstones(state_dir):
    builder = typical_state()
    builder.retired[digest("rotated-away")] = {"family": "a" * 16, "retired_at": NOW - 3 * DAY}
    builder.write(state_dir, version=2)
    original = builder.document(2)
    run = invoke("revoke-all", "--apply", state_dir=state_dir)
    assert run.code == 0 and run.err == ""
    assert "revoked: access_tokens=7 refresh_tokens=7; client registrations stay (clients=10)" in run.out
    assert "written: format=v2 clients=10 access=0 refresh=0 verified=yes" in run.out
    document = read_document(state_dir)
    assert document["access_tokens"] == {} and document["refresh_tokens"] == {}
    assert document["a2r"] == {} and document["r2a"] == {} and document["refresh_meta"] == {}
    assert document["clients"] == original["clients"]  # registrations, secrets included, exactly as they were
    assert document["retired"] == original["retired"]  # and no tombstone was added or dropped
    assert_no_credential(run, builder)


def test_revoke_all_of_a_version_1_file_writes_version_2_and_leaves_the_clients_alone(production, state_dir):
    original = production.document()
    assert invoke("revoke-all", "--apply", state_dir=state_dir).code == 0
    document = read_document(state_dir)
    assert document["version"] == 2 and document["clients"] == original["clients"]


def test_after_revoke_all_the_real_provider_knows_no_token_and_logs_no_reuse(production, state_dir, caplog):
    access = next(t for t, r in production.access.items() if r.client_id == LIVE_ID)
    refresh = next(t for t, r in production.refresh.items() if r.client_id == LIVE_ID)
    assert invoke("revoke-all", "--apply", state_dir=state_dir).code == 0
    caplog.set_level(logging.INFO)
    provider = PersonalAuthProvider(base_url=BASE_URL, password=PASSWORD, state_dir=str(state_dir), clock=lambda: float(NOW))
    try:
        assert len(provider.clients) == 10 and provider.access_tokens == {} and provider.refresh_tokens == {}
        assert asyncio.run(provider.load_access_token(access)) is None
        assert asyncio.run(provider.load_refresh_token(provider.clients[LIVE_ID], refresh)) is None
    finally:
        provider.close()
    assert not any("refresh_reuse" in record.getMessage() for record in caplog.records)  # revoked is not "rotated away"


def test_revoke_all_with_no_token_changes_nothing(state_dir):
    builder = Builder()
    builder.client(1)
    builder.write(state_dir, version=2)
    before = snapshot(state_dir)
    run = invoke("revoke-all", "--apply", state_dir=state_dir)
    assert run.code == 0 and "no token to revoke: nothing was changed" in run.out
    after = snapshot(state_dir)
    after.pop(LOCK_FILE, None)
    assert after == before


def test_revoke_all_goes_ahead_when_the_file_holds_records_it_cannot_read_and_says_so(production, state_dir):
    document = production.document()
    document["refresh_tokens"]["junk"] = {"token": "junk"}
    (state_dir / STATE_FILE).write_text(json.dumps(document))
    run = invoke("revoke-all", "--apply", state_dir=state_dir)
    assert run.code == 0 and "dropped: 1 records that could not be read" in run.out
    assert read_document(state_dir)["refresh_tokens"] == {}


# --------------------------------------------------------------------------
# What every command promises
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["check"],
        ["inventory"],
        ["purge", "--stale", "--plan"],
        ["purge", "--stale", "--apply"],
        ["expire-access", "--client", "0a1b2c3d", "--apply"],
        ["revoke-all", "--apply"],
    ],
    ids=lambda argv: " ".join(argv),
)
def test_no_command_ever_creates_a_state_directory(argv, tmp_path):
    target = tmp_path / "parent" / "oauth-state"
    run = invoke(*argv, state_dir=target)
    assert run.code == oauth_state.EXIT_REFUSED and "nothing was created" in run.err
    assert not (tmp_path / "parent").exists()
    assert os.listdir(tmp_path) == []


@pytest.mark.parametrize(
    "argv",
    [["check"], ["inventory"], ["purge", "--stale", "--plan"]],
    ids=lambda argv: " ".join(argv),
)
def test_the_read_only_commands_leave_the_directory_exactly_as_it_was(argv, production, state_dir):
    before = snapshot(state_dir)
    assert invoke(*argv, state_dir=state_dir).code == 0
    assert snapshot(state_dir) == before


@pytest.mark.parametrize("argv", [["check"], ["inventory"], ["purge", "--stale", "--apply"], ["revoke-all", "--apply"]])
def test_an_unexpected_failure_prints_its_type_only(argv, production, state_dir, monkeypatch):
    def explode(*args, **kwargs):
        raise ValueError("pat_THIS_MUST_NOT_BE_PRINTED_0123456789abcdef " + production.secrets()[0])

    monkeypatch.setattr(oauth_store, "load_state", explode)
    run = invoke(*argv, state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED
    assert run.err.strip() == "error: ValueError"
    assert "THIS_MUST_NOT_BE_PRINTED" not in run.text and production.secrets()[0] not in run.text


@pytest.mark.parametrize("argv", [["check"], ["inventory"], ["purge", "--stale", "--plan"]], ids=lambda argv: " ".join(argv))
def test_a_state_error_prints_its_type_and_a_fixed_sentence_never_its_message(argv, production, state_dir, monkeypatch):
    def refuse(*args, **kwargs):
        raise oauth_store.StateFileError("the file holds pat_THIS_MUST_NOT_BE_PRINTED_0123456789abcdef at line 3")

    monkeypatch.setattr(oauth_store, "load_state", refuse)
    run = invoke(*argv, state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED
    sentence = f"error: StateFileError: {oauth_state.STATE_FILE_UNUSABLE}"
    if argv == ["check"]:  # check prints it in line, on stdout, before its result
        assert run.err == "" and sentence in run.out.splitlines() and run.out.splitlines()[-1] == "result: problems"
    else:
        assert run.err.strip() == sentence and run.out == ""
    assert "THIS_MUST_NOT_BE_PRINTED" not in run.text and "line 3" not in run.text


def test_a_line_that_holds_a_credential_is_never_printed(production, state_dir, monkeypatch):
    # if a future edit ever formats a token into a line, the guard stops the line: it is not printed and the run fails
    leaked = next(iter(production.access))
    monkeypatch.setattr(oauth_state, "token_type", lambda token: token)
    run = invoke("inventory", state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED and "LeakError" in run.err
    assert leaked not in run.text and not any(secret in run.text for secret in production.secrets())


def test_the_guard_covers_tokens_secrets_full_client_ids_and_tombstones():
    builder = Builder()
    cid = builder.client(1, cid="abcdef12-0000-4000-8000-000000000001")
    access, refresh = builder.pair(cid, "p", access_exp=NOW + DAY)
    builder.client(2, cid="short")
    builder.retired[digest("old")] = {"family": "a" * 16, "retired_at": NOW}
    state = oauth_store.OAuthState(
        clients=builder.clients, access_tokens=builder.access, refresh_tokens=builder.refresh, retired=builder.retired
    )
    guard = oauth_state.Guard()
    guard.protect(state)
    secret = builder.clients[cid].client_secret
    for text in (access, refresh, secret, cid, digest("old"), f"prefix {access} suffix"):
        assert guard.holds_secret(text), text[:12]
    for text in ("client=abcdef12", "type=pat_", "type=prt_", "short", "", "name=\"Claude\"", cid[:8]):
        assert not guard.holds_secret(text), text
    stream = io.StringIO()
    output = oauth_state.Output(stream, guard)
    output.line("client=abcdef12 type=pat_")
    with pytest.raises(oauth_state.LeakError):
        output.line(f"oops {refresh}")
    assert stream.getvalue() == "client=abcdef12 type=pat_\n"


def test_a_failed_write_leaves_the_old_file_byte_for_byte_and_the_lock_free(production, state_dir, monkeypatch):
    before = snapshot(state_dir)

    def refuse(src, dst):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patched:
        patched.setattr(os, "replace", refuse)
        run = invoke("purge", "--stale", "--apply", state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED and "StateFileError" in run.err and "nothing was changed" in run.err
    after = snapshot(state_dir)
    after.pop(LOCK_FILE, None)
    assert after == before  # the old file is intact and no temporary file is left
    oauth_store.StateLock(state_dir).acquire().release()  # and the lock was let go


def test_a_file_that_does_not_read_back_as_written_is_reported_and_not_called_done(production, state_dir, monkeypatch):
    real_write = oauth_store.write_state

    def write_something_else(directory, state):
        smaller = oauth_store.OAuthState(  # what ends up on disk is not what the script holds
            clients={}, access_tokens=state.access_tokens, refresh_tokens=state.refresh_tokens,
            access_to_refresh=state.access_to_refresh, refresh_to_access=state.refresh_to_access,
            refresh_meta=state.refresh_meta, retired=state.retired,
        )
        real_write(directory, smaller)

    monkeypatch.setattr(oauth_store, "write_state", write_something_else)
    run = invoke("purge", "--stale", "--apply", state_dir=state_dir)
    assert run.code == oauth_state.EXIT_FAILED and "VerificationError" in run.err and "restore the backup" in run.err
    assert "verified=yes" not in run.out


def test_a_pair_that_the_maps_do_not_link_is_still_removed_as_a_pair_or_an_orphan(state_dir):
    # a version 1 file written by the old code can lack one direction of the map; the script reads either direction
    builder = Builder()
    cid = builder.client(1, registered=NOW - 300 * DAY)
    access, refresh = builder.pair(cid, "half", access_exp=NOW - 200 * DAY)
    builder.r2a.clear()  # only a2r is left
    builder.write(state_dir)
    state = loaded(state_dir).state
    plan = oauth_state.plan_stale(state, NOW)
    assert len(plan.pairs) == 1 and plan.pairs[0].access == (access,) and plan.orphans == []
    oauth_state.apply_stale(state, plan)
    assert state.access_tokens == {} and state.refresh_tokens == {} and state.clients == {}


# --------------------------------------------------------------------------
# The script and its tests
# --------------------------------------------------------------------------


def test_the_script_and_its_tests_hold_no_em_or_en_dash_and_only_ascii():
    for path in (SCRIPT, Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert EM_DASH not in text and EN_DASH not in text, path.name
        assert text.isascii(), path.name


def test_the_module_docstring_names_every_command_and_the_promises():
    doc = " ".join((oauth_state.__doc__ or "").split()).lower()
    for command in oauth_state.COMMANDS:
        assert command in doc
    for promise in (
        "output is metadata only", "never a token, a secret or a full client id", "exit status 3",
        "it never creates a state directory", "refuses at once", "stop the service first",
        "text that a client chose", "cannot stop a run", "replaced by [withheld]", "it stays under the leakerror check",
        "prints the usage and a fixed sentence", "never echoes what was typed",
        "a value that is split by characters this script cannot show", "so the whole text is withheld instead",
        "follows that problem with a `hint:` line", "docs/troubleshooting.md",
        "when the store refuses the file", "`check` still prints the file facts and the lock line",
        "stat needs only search permission on the directory", "without these lines it could not be told from a damaged file",
        "followed by an `error:` line with the type and one fixed sentence", "by `result: problems` (exit status 1)",
    ):
        assert promise in doc, promise
    assert set(oauth_state.COMMANDS) == {"check", "inventory", "purge", "expire-access", "revoke-all"}


def test_the_thresholds_are_the_ones_the_docs_say():
    assert oauth_state.STALE_AFTER_SECONDS == 30 * DAY
    assert oauth_state.NEW_CLIENT_GRACE_SECONDS == DAY
    assert oauth_state.EXPIRED_SECONDS_AGO == 1
    assert (oauth_state.EXIT_OK, oauth_state.EXIT_FAILED, oauth_state.EXIT_REFUSED, oauth_state.EXIT_LOCKED) == (0, 1, 2, 3)
