#!/usr/bin/env python3
"""Look at and tidy the OAuth state file of the personal login, and never print a credential.

The state file (`.oauth-state/oauth_tokens.json`) holds live credentials: client secrets, access tokens and refresh
tokens. Do not open or edit it directly. This script is the one way to see what it holds and the one way to change it
by hand. It is meant for the operator, and it runs as the service user (`gorelo-mcp`) on the host that
holds the state:

    oauth_state.py check        [--state-dir DIR]
    oauth_state.py inventory    [--state-dir DIR]
    oauth_state.py purge --stale [--keep-client ID8 ...] (--plan | --apply)  [--state-dir DIR]
    oauth_state.py expire-access --client ID8 --apply                        [--state-dir DIR]
    oauth_state.py revoke-all --apply                                        [--state-dir DIR]

`--state-dir` defaults to `.oauth-state` next to the repository root of this script. To look at the state of another
installation, name its directory: `--state-dir /opt/gorelo-mcp/app/.oauth-state`.

What each command does:

* check: reads the file the way the service does at start and says what the service would log (`format`, the counts,
  how many tokens its load would prune), whether the service holds the state lock (`held` after a start of this release,
  `absent` before it), the mode and owner of the file, and whether this user can write to the directory (run as the service
  user, that is what the service needs to lock and to save). A file that other users can read is a problem (its mode should be
  0600), and `check` follows that problem with a `hint:` line that holds the one repair this script suggests for it (see
  docs/TROUBLESHOOTING.md, "Service will not start"). When the store refuses the file (not valid JSON, an unknown format, a file this user cannot
  read), `check` still prints the file facts and the lock line and the problems they show, because stat needs only search
  permission on the directory: a file owned by another user than its directory is the one the owner repair of
  docs/TROUBLESHOOTING.md is for, and without these lines it could not be told from a damaged file. They are followed by an `error:` line with the type
  and one fixed sentence, and by `result: problems` (exit status 1). Writes nothing, creates nothing.
* inventory: one line per client and per token. Writes nothing.
* purge --stale: removes what is stale and nothing else. A token pair (a refresh token with the access tokens paired with
  it) is stale when none of its access tokens is still valid AND its refresh token has not been issued within the last
  STALE_AFTER_SECONDS (30 days, taken from the family metadata the store keeps; an old file gets an estimate), or the
  refresh token itself has expired. An access token with no refresh token goes after the week the store keeps expired
  tokens. A client that holds no token is stale when it was registered more than NEW_CLIENT_GRACE_SECONDS ago (24 hours)
  or lost its last token in this purge. A live pair is never removed, and neither is anything of a client named with
  `--keep-client` (the first 8 characters of its id, as `inventory` shows them). `--plan` shows what `--apply` would
  remove and changes nothing.
* expire-access: marks the valid access tokens of one client as expired a second ago, so that its next request is
  refused as expired and has to refresh. Refresh tokens are not touched. It refuses a client that has no usable refresh
  token, because it could not refresh and would have to sign in again. This is how to test that a session refreshes.
* revoke-all: removes every access token and every refresh token. Client registrations and the tombstones of rotated
  tokens stay. It replaces deleting the state file ("every client must authorize again"), and keeps the file valid.

What the script promises, each of them pinned by tests/test_oauth_state_script.py:

* Output is metadata only: the type prefix of a token (pat_, prt_, ...), the first 8 characters of a client id, client
  names (printable ASCII, cut to 40 characters), dates (UTC) and counts. Never a token, a secret or a full client id: every
  line is checked against the credentials of the state that was loaded before it is printed, and a line that holds one is
  not printed (LeakError).
* Text that a client chose (its name, the host of a redirect address) cannot stop a run. Registration is open, so anyone can
  name a client after another client's id or give it a redirect host that holds one. A protected value inside such text is
  replaced by [withheld] before the line is built, and the rest of the line is printed. A value that is split by characters
  this script cannot show (an invisible one, a quote, a backslash or a question mark: each is printed as "?") would come out
  as the value with a "?" in it, which a reader can put together again, so the whole text is withheld instead. A line built
  from a token or a secret field has no such way out: it stays under the LeakError check.
* An error prints its type and a fixed sentence, never the message of the exception (a message may hold a value).
* A command line that does not fit prints the usage and a fixed sentence, exit status 2, and never echoes what was typed: a
  token or a client id pasted by mistake would otherwise land in the terminal and in any transcript (the stock argparse
  messages, "unrecognized arguments: ..." and "invalid choice: ...", repeat the offending text).
* Writing (--apply) takes the exclusive lock on `.lock` in the state directory first and refuses at once, with exit status
  3 and nothing changed, while the service (or any other process) holds it: stop the service first. check, inventory and
  --plan take no lock; the service writes atomically, so a read never sees half a file.
* It never creates a state directory and never creates a state file: a missing directory is refused (exit status 2), a
  missing file is an empty state that --apply has nothing to change in.
* A write is atomic (oauth_store.write_state), rewrites the file as version 2 (the same tokens, expiries, client ids and
  secrets, plus the family metadata), and is read back and compared before the script says it is done.
* --apply also refuses (exit status 2, nothing changed) a file that holds records that cannot be read (rewriting it would
  drop them), except revoke-all, which drops every token anyway.

Exit status: 0 done (or nothing to do); 1 the state file cannot be used, `check` found problems, or something unexpected
failed; 2 refused: a bad command line, a directory or an id that does not fit, or a precondition that is not met (nothing
was changed); 3 refused because the state lock is held (nothing was changed).
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import pwd
import re
import shlex
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn, TextIO
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
# `python scripts/oauth_state.py` puts scripts/ on sys.path, not the repository root, where oauth_store.py lives.
sys.path.insert(0, str(REPO_ROOT))

import oauth_store  # noqa: E402

DEFAULT_STATE_DIR = REPO_ROOT / ".oauth-state"

# purge --stale: a pair with no valid access token whose refresh token was issued longer ago than this is stale.
STALE_AFTER_SECONDS = 30 * 24 * 60 * 60
# purge --stale: a client that holds no token is left alone for this long after it registered (a sign-in in progress).
NEW_CLIENT_GRACE_SECONDS = 24 * 60 * 60
# expire-access: the access tokens it marks expire this many seconds before "now".
EXPIRED_SECONDS_AGO = 1

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_LOCKED = 3

ID8_PATTERN = re.compile(r"[A-Za-z0-9_-]{8}")
ID8_RULE = "must be the first 8 characters of a client id (letters, digits, - and _)"
# The only token shapes the script names. Anything else is printed as "other": no character of a token is ever shown.
TOKEN_TYPES = ("test_access_token_", "test_refresh_token_", "pat_", "prt_")
NAME_LIMIT = 40
HOSTS_SHOWN = 3
# The user the service runs as. The one repair of the file mode that `check` hints at is run as this user, so that the file
# keeps its owner (docs/TROUBLESHOOTING.md, "Service will not start").
SERVICE_USER = "gorelo-mcp"
# A credential shorter than this is not guarded in the output (no real one is: tokens are 64 hex digits and more).
MIN_PROTECTED_LENGTH = 12
# What stands in for a protected value inside text that a client chose (a name, a redirect host). It holds no quote, no
# backslash and no comma, so it cannot change the shape of the line it ends up in.
MASKED = "[withheld]"
# The one sentence a refused command line gets. It names nothing that was typed.
COMMAND_LINE_REFUSED = (
    "the command line was refused and nothing was changed. What was typed is not shown, because a mistyped value may be a "
    "token or a client id. Compare it with the usage above, or run with --help."
)
# The one sentence that follows the type of a StateFileError, wherever the script prints one (main, and `check` for a file
# the store refuses). The message of the exception itself is never printed: it names the path and a fault, and a message
# may hold a value.
STATE_FILE_UNUSABLE = (
    "the state file or directory cannot be used (not valid JSON, an unknown format, not readable by this user, or not "
    "writable); nothing was changed by this run. The service journal has the details."
)


class Refused(Exception):
    """The command cannot be carried out as asked and nothing was changed. The text is fixed wording (plus an id the caller
    typed), never a value from the state file."""


class LeakError(RuntimeError):
    """A line that was about to be printed holds a credential of the loaded state. It is not printed and not kept."""


class VerificationError(RuntimeError):
    """The file that was just written does not read back as what was written."""


# --------------------------------------------------------------------------
# Printing nothing that could be a credential
# --------------------------------------------------------------------------


def id8(value: object) -> str:
    """The first 8 characters of an id, as the journal lines show them: letters, digits, "-" and "_" as they are, anything
    else as "?". It is also what --client and --keep-client are compared with."""
    text = "" if value is None else str(value)[:8]
    return "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "?" for ch in text) or "-"


def as_printed(text: str) -> str:
    """`text` as safe_text prints it, before any cut: printable ASCII as it is, except the quote and the backslash, and
    anything outside printable ASCII (an invisible character, a control character, a letter that is not ASCII), each of which
    becomes "?"."""
    return "".join(ch if " " <= ch <= "~" and ch not in '"\\' else "?" for ch in text)


def safe_text(value: object, limit: int = NAME_LIMIT, *, guard: Guard | None = None) -> str:
    """Printable ASCII only (anything else becomes "?"), no quote and no backslash, at most `limit` characters; "..." marks a
    cut. A client chooses its own name at registration, so a name can hold anything.

    Pass the `guard` for text a client chose (a name, a redirect host): every protected value inside it is replaced by
    MASKED first, before the cut, so that no piece of one is left at the edge. A protected value that is split by characters
    which this function prints as "?" (a zero-width space in the middle of another client's id) is not found by that
    replacement, and the text would come out as the id with a "?" in it, which a reader puts together again: the text as it
    is printed is checked once more with its question marks taken out, and a text that hides a protected value that way is
    withheld whole (MASKED for the entire text, not only for the value), before the cut as well. Without a guard the text is
    not masked, and a protected value in it is caught later by Output.line (LeakError): that is what is wanted for text the
    script built itself."""
    text = "" if value is None else str(value)
    if guard is not None:
        text = guard.mask(text)
        if guard.hides_secret(as_printed(text)):
            text = MASKED
    shown = as_printed(text[:limit])
    return shown + ("..." if len(text) > limit else "")


def when(epoch: object, *, none: str = "unknown", seconds: bool = False) -> str:
    """A UTC date and time to the minute (or the second); `none` for no value, "invalid" for one that is not a time."""
    if epoch is None:
        return none
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        return "invalid"
    try:
        moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "invalid"
    return moment.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")


def token_type(token: str) -> str:
    for prefix in TOKEN_TYPES:
        if token.startswith(prefix):
            return prefix
    return "other"


class Guard:
    """The credentials of the state that was loaded, kept only to check that no printed line holds one."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def protect(self, state: oauth_store.OAuthState) -> None:
        for table in (state.access_tokens, state.refresh_tokens):
            for key, record in table.items():
                for value in (key, getattr(record, "token", None)):
                    if isinstance(value, str) and len(value) >= MIN_PROTECTED_LENGTH:
                        self._secrets.add(value)
        for client_id, client in state.clients.items():
            secret = getattr(client, "client_secret", None)
            if isinstance(secret, str) and len(secret) >= MIN_PROTECTED_LENGTH:
                self._secrets.add(secret)
            if len(client_id) > 8:  # only the first 8 characters of an id may be shown
                self._secrets.add(client_id)
        for digest in state.retired:
            self._secrets.add(digest)

    def holds_secret(self, text: str) -> bool:
        return any(secret in text for secret in self._secrets)

    def mask(self, text: str) -> str:
        """`text` with every protected value inside it replaced by MASKED, for text that a CLIENT chose (its name, the host of
        a redirect address). Registration is open, so anyone can register a client named after another client's id, or with
        a redirect host that holds one (`https://<that id>.claude.ai/cb` passes the validator), and the guard would then
        stop every listing with LeakError. Text like that is masked instead; a line built from a token or a secret field is
        not, and still stops the run.

        The match ignores case (a host is lower-cased on its way here) and takes the longest value first. The result is
        checked again: if a value is still in it (a replacement that joined two pieces into one), the whole text is
        withheld."""
        lowered = text.lower()
        # a state can hold thousands of tombstones: only the values that occur in the text get a regular expression
        present = [secret for secret in self._secrets if secret.lower() in lowered]
        for secret in sorted(present, key=len, reverse=True):
            text = re.sub(re.escape(secret), lambda _match: MASKED, text, flags=re.IGNORECASE | re.ASCII)
        return MASKED if self.holds_secret(text) else text

    def hides_secret(self, printed: str) -> bool:
        """True when `printed`, a text in the form safe_text prints it (see as_printed), holds a protected value once its
        question marks are taken out: the value was split by characters that print as "?", such as a zero-width space, and
        mask() did not find it. A reader sees the value with "?" marks in it and can put it together again. A "?" that was
        typed is taken out as well, because it prints exactly like one that was made (no protected value holds a question
        mark, so this cannot hide a real match). The match ignores case, like mask(). It is only a check, nothing is replaced
        here: the caller withholds the whole text.

        Only a text that has a "?" can hide one: without it the printed form is the text mask() has just looked at, so there
        is nothing more to find, and the scan over every protected value (thousands, with the tombstones) is skipped."""
        rebuilt = printed.replace("?", "")
        if rebuilt == printed:
            return False
        lowered = rebuilt.lower()
        return any(secret.lower() in lowered for secret in self._secrets)


class Output:
    """A text stream whose lines are checked against the Guard before they are written."""

    def __init__(self, stream: TextIO, guard: Guard) -> None:
        self._stream = stream
        self._guard = guard

    def line(self, text: str = "") -> None:
        if self._guard.holds_secret(text):
            raise LeakError()
        self._stream.write(text + "\n")


# --------------------------------------------------------------------------
# Reading the state
# --------------------------------------------------------------------------


def usable(expires_at: int | None, now: float) -> bool:
    """The service treats a token as expired when expires_at < now, so it is usable when it has no expiry or expires_at >= now."""
    return expires_at is None or expires_at >= now


def client_ids(state: oauth_store.OAuthState) -> set[str]:
    """Every client id the state knows, as a registration or as the owner of a token."""
    return (
        set(state.clients)
        | {record.client_id for record in state.access_tokens.values()}
        | {record.client_id for record in state.refresh_tokens.values()}
    )


def match_clients(state: oauth_store.OAuthState, given: str) -> list[str]:
    return sorted(client_id for client_id in client_ids(state) if id8(client_id) == given)


def access_status(expires_at: int | None, now: float) -> str:
    if expires_at is None or expires_at >= now:
        return "live"
    if expires_at >= now - oauth_store.EXPIRED_TOKEN_RETENTION_SECONDS:
        return "expired"
    return "prunable"  # expired for more than the retention: the service drops it at its next load


def issued_at(state: oauth_store.OAuthState, refresh_token: str) -> int | None:
    meta = state.refresh_meta.get(refresh_token)
    value = meta.get("issued_at") if isinstance(meta, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def family8(state: oauth_store.OAuthState, refresh_token: str) -> str:
    meta = state.refresh_meta.get(refresh_token)
    return id8(meta.get("family") if isinstance(meta, dict) else None)


def stored_family8(state: oauth_store.OAuthState, refresh_token: str, file_format: str) -> str:
    """The family of a refresh token as the FILE has it. A version 1 file stores none (a load invents one for each token, a
    new one every time), so it shows "-" until the first save writes version 2."""
    return family8(state, refresh_token) if file_format == "v2" else "-"


def redirect_hosts(client: object, guard: Guard | None = None) -> str:
    """The hosts of the client's redirect addresses (never the whole address), at most HOSTS_SHOWN of them. A host is text the
    client chose: with a `guard`, a protected value inside it (a host such as `<another client's id>.claude.ai`) is masked."""
    hosts: set[str] = set()
    for uri in getattr(client, "redirect_uris", None) or []:
        try:
            host = urlsplit(str(uri)).hostname
        except ValueError:
            host = None
        hosts.add(safe_text(host or "-", 40, guard=guard))
    ordered = sorted(hosts)
    shown = ",".join(ordered[:HOSTS_SHOWN]) or "-"
    return shown + (f",+{len(ordered) - HOSTS_SHOWN}" if len(ordered) > HOSTS_SHOWN else "")


def _owner(uid: int) -> str:
    try:
        return safe_text(pwd.getpwuid(uid).pw_name)
    except KeyError:
        return str(uid)


@dataclass(frozen=True)
class Problem:
    """Something `check` found wrong. `hint` is the one repair this script suggests for it, when it has one: it is printed
    on a line of its own after the problem, and it is a command for the operator to run as root."""

    text: str
    hint: str | None = None


def mode_repair_command(state_dir: Path) -> str:
    """The command that gives the state file in `state_dir` its mode 0600 back. It is run as root, and
    `runuser` runs the chmod as the service user, who owns the file (when root owns it, the owner repair comes first). The path is
    absolute (the command may be run from anywhere) and shown the way every printed path is (safe_text)."""
    path = os.path.abspath(state_dir / oauth_store.STATE_FILE_NAME)
    return f"runuser -u {SERVICE_USER} -- chmod 600 {shlex.quote(safe_text(path, 400))}"


def file_facts(state_dir: Path) -> tuple[str, list[Problem]]:
    """("mode=0600 owner=gorelo-mcp dir_owner=gorelo-mcp", problems) for the state file; ("", []) when there is none."""
    try:
        info = (state_dir / oauth_store.STATE_FILE_NAME).stat()
        parent = state_dir.stat()
    except OSError:
        return "", []
    problems: list[Problem] = []
    if info.st_mode & 0o077:
        problems.append(
            Problem(
                "the state file can be read by other users (its mode should be 0600)",
                hint=f"to repair it, run as root (docs/TROUBLESHOOTING.md, \"Service will not start\"): {mode_repair_command(state_dir)}",
            )
        )
    if info.st_uid != parent.st_uid:
        problems.append(
            Problem("the state file is owned by another user than its directory (the service may not be able to read it)")
        )
    mode = info.st_mode & 0o777
    return f"mode={mode:04o} owner={_owner(info.st_uid)} dir_owner={_owner(parent.st_uid)}", problems


def lock_status(state_dir: Path) -> str:
    """held (another process holds the state lock: the service is up), free, absent (no lock file: the service has not
    started with this release) or unknown. Creates nothing; a shared lock is taken for an instant to ask."""
    path = state_dir / oauth_store.LOCK_FILE_NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            return "held" if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK) else "unknown"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "free"
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# What purge --stale would remove
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PairPlan:
    """A stale refresh token with the access tokens paired with it. The token strings are only used to remove them."""

    refresh: str
    access: tuple[str, ...]
    client_id: str
    issued_at: int | None
    last_access_expiry: int | None
    why: str


@dataclass(frozen=True)
class OrphanPlan:
    """A stale access token that has no refresh token."""

    token: str
    client_id: str
    expires_at: int | None


@dataclass
class StalePlan:
    pairs: list[PairPlan] = field(default_factory=list)
    orphans: list[OrphanPlan] = field(default_factory=list)
    clients: list[str] = field(default_factory=list)  # ids of the clients that would go
    kept: dict[str, str] = field(default_factory=dict)  # client id -> why it stays

    @property
    def access_count(self) -> int:
        return sum(len(pair.access) for pair in self.pairs) + len(self.orphans)

    @property
    def empty(self) -> bool:
        return not (self.pairs or self.orphans or self.clients)


def plan_stale(state: oauth_store.OAuthState, now: float, keep: Sequence[str] = ()) -> StalePlan:
    """What `purge --stale` removes from `state` at time `now`, with `keep` the first 8 characters of the client ids to leave
    alone. Pure: changes nothing. See the module docstring for the rules; the two that matter most are written as code
    here: a client in `keep` is skipped before anything else, and a pair with a valid access token is skipped before its
    refresh token is looked at."""
    plan = StalePlan()
    keep_set = set(keep)
    kept_ids = {client_id for client_id in client_ids(state) if id8(client_id) in keep_set}
    paired_access: set[str] = set()
    for refresh, record in state.refresh_tokens.items():
        accesses = oauth_store.paired_access_tokens(state, refresh)
        paired_access.update(accesses)
        if record.client_id in kept_ids:
            continue
        if any(usable(state.access_tokens[token].expires_at, now) for token in accesses):
            continue  # a live pair
        issued = issued_at(state, refresh)
        if usable(record.expires_at, now):
            if issued is None or now - issued <= STALE_AFTER_SECONDS:
                continue  # it can still be used, and its client was active lately (or no one can tell when)
            why = f"no valid access token and issued {int((now - issued) // 86400)} days ago"
        else:
            why = "the refresh token itself has expired"
        expiries = [state.access_tokens[token].expires_at for token in accesses if state.access_tokens[token].expires_at]
        plan.pairs.append(
            PairPlan(
                refresh=refresh,
                access=tuple(accesses),
                client_id=record.client_id,
                issued_at=issued,
                last_access_expiry=max(expiries) if expiries else None,
                why=why,
            )
        )
    for token, record in state.access_tokens.items():
        if token in paired_access or record.client_id in kept_ids:
            continue
        if usable(record.expires_at, now) or access_status(record.expires_at, now) != "prunable":
            continue
        plan.orphans.append(OrphanPlan(token=token, client_id=record.client_id, expires_at=record.expires_at))

    gone_refresh = {pair.refresh for pair in plan.pairs}
    gone_access = {token for pair in plan.pairs for token in pair.access} | {orphan.token for orphan in plan.orphans}
    holders_left = {record.client_id for token, record in state.refresh_tokens.items() if token not in gone_refresh} | {
        record.client_id for token, record in state.access_tokens.items() if token not in gone_access
    }
    lost_tokens = {pair.client_id for pair in plan.pairs} | {orphan.client_id for orphan in plan.orphans}
    for client_id in sorted(state.clients, key=lambda cid: (state.clients[cid].client_id_issued_at or 0, cid)):
        client = state.clients[client_id]
        if client_id in kept_ids:
            plan.kept[client_id] = "named with --keep-client"
        elif client_id in holders_left:
            live = any(
                usable(record.expires_at, now)
                for record in state.access_tokens.values()
                if record.client_id == client_id and record.token not in gone_access
            )
            plan.kept[client_id] = "has a valid access token" if live else "has tokens that are not stale"
        elif client_id not in lost_tokens and client.client_id_issued_at is not None and (
            now - client.client_id_issued_at <= NEW_CLIENT_GRACE_SECONDS
        ):
            plan.kept[client_id] = "no token yet, registered less than 24 hours ago"
        else:
            plan.clients.append(client_id)
    return plan


def apply_stale(state: oauth_store.OAuthState, plan: StalePlan) -> tuple[int, int, int]:
    """Remove what `plan` lists. Returns (access tokens, refresh tokens, clients) removed. No tombstone is kept: this is an
    administrative removal, not a rotation, so a later presentation of one of these tokens is simply unknown. A client that
    still holds a token is never removed, whatever the plan says (oauth_store.remove_tokenless_clients)."""
    access = refresh = 0
    for pair in plan.pairs:
        gone_access, gone_refresh = oauth_store.remove_refresh_token(state, pair.refresh)
        access += gone_access
        refresh += gone_refresh
    for orphan in plan.orphans:
        access += oauth_store.remove_access_token(state, orphan.token)
    clients = oauth_store.remove_tokenless_clients(state, plan.clients)
    return access, refresh, len(clients)


# --------------------------------------------------------------------------
# The other two changes
# --------------------------------------------------------------------------


def expire_access(state: oauth_store.OAuthState, client_id: str, now: float) -> int:
    """Mark the valid access tokens of `client_id` as expired EXPIRED_SECONDS_AGO seconds ago. Returns how many were marked.
    Nothing else changes (a refresh token, a token of another client, an access token that had already expired)."""
    expired_at = int(now) - EXPIRED_SECONDS_AGO
    marked = 0
    for record in state.access_tokens.values():
        if record.client_id == client_id and usable(record.expires_at, now):
            record.expires_at = expired_at
            marked += 1
    return marked


def revoke_everything(state: oauth_store.OAuthState) -> tuple[int, int]:
    """Remove every refresh token with its pair, then every access token that is left. Returns (access, refresh) counts.
    Clients and tombstones stay."""
    access = refresh = 0
    for token in list(state.refresh_tokens):
        gone_access, gone_refresh = oauth_store.remove_refresh_token(state, token)
        access += gone_access
        refresh += gone_refresh
    for token in list(state.access_tokens):
        access += oauth_store.remove_access_token(state, token)
    return access, refresh


# --------------------------------------------------------------------------
# The commands
# --------------------------------------------------------------------------


@dataclass
class Context:
    args: argparse.Namespace
    now: float
    out: Output
    err: Output
    guard: Guard

    @property
    def state_dir(self) -> Path:
        return Path(self.args.state_dir)


def _require_directory(ctx: Context) -> None:
    if not ctx.state_dir.is_dir():
        raise Refused("the state directory does not exist or is not a directory; nothing was created")


@contextmanager
def _exclusive(ctx: Context) -> Iterator[None]:
    """Hold the state lock for the whole of a change, taken before the file is read. Raises StateLockedError at once when
    the service, or another process, has it. With no state file there is nothing to change, so that is refused before the
    lock is taken (taking it would create the lock file in an otherwise empty directory)."""
    if not (ctx.state_dir / oauth_store.STATE_FILE_NAME).exists():
        raise Refused("there is no state file in that directory; nothing to change")
    lock = oauth_store.StateLock(ctx.state_dir)  # never creates the directory
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _load_for_change(ctx: Context, *, allow_unreadable: bool = False) -> oauth_store.LoadResult:
    """Read the file as it is (nothing pruned), for a change. Refuses an empty state and, unless asked, a file that holds
    records which cannot be read."""
    result = oauth_store.load_state(ctx.state_dir, now=ctx.now, prune_expired=False)
    ctx.guard.protect(result.state)
    if result.format == "none":
        raise Refused("there is no state file in that directory; nothing to change")
    if sum(result.skipped.values()) and not allow_unreadable:
        raise Refused(
            "the state file holds records that cannot be read, and rewriting it would drop them; run check, and restore a "
            "backup (or use revoke-all, which drops every token anyway)"
        )
    return result


def _write_and_verify(ctx: Context, state: oauth_store.OAuthState) -> None:
    expected = (len(state.clients), len(state.access_tokens), len(state.refresh_tokens))
    oauth_store.write_state(ctx.state_dir, state)
    try:
        again = oauth_store.load_state(ctx.state_dir, now=ctx.now, prune_expired=False)
    except oauth_store.StateFileError:
        raise VerificationError() from None
    found = (again.clients, again.access_tokens, again.refresh_tokens)
    if again.format != "v2" or found != expected or sum(again.skipped.values()):
        raise VerificationError()
    facts, _ = file_facts(ctx.state_dir)
    ctx.out.line(
        f"written: format=v2 clients={again.clients} access={again.access_tokens} refresh={again.refresh_tokens} "
        "verified=yes (the file was read back)"
    )
    if facts:
        ctx.out.line(f"file: {facts}")


def cmd_check(ctx: Context) -> int:
    _require_directory(ctx)
    result: oauth_store.LoadResult | None = None
    refused: oauth_store.StateFileError | None = None
    try:
        result = oauth_store.load_state(ctx.state_dir, now=ctx.now)
    except oauth_store.StateLockedError:
        raise  # not a fault of the file (load_state takes no lock): main() reports it
    except oauth_store.StateFileError as exc:
        # The store refuses the file (not valid JSON, an unknown format, a file this user cannot read). The facts below need
        # no read of it: stat needs only search permission on the directory. They are still printed, because a file left
        # owned by another user than its directory is the cause that the owner repair of docs/TROUBLESHOOTING.md is for, and without these
        # lines it could not be told from a damaged file. The refusal is reported after them, and the result is "problems".
        refused = exc
    if result is not None:
        ctx.guard.protect(result.state)
    skipped = sum(result.skipped.values()) if result is not None else 0
    facts, problems = file_facts(ctx.state_dir)
    if skipped:
        problems.append(
            Problem(f"{skipped} records in the file cannot be read; the service skips them and drops them at its next save")
        )
    # Run as the service user (as the docs say) this is a pre-flight of what the service needs: to write the lock file
    # and to replace the state file, it must be able to write to the directory.
    writable = os.access(ctx.state_dir, os.W_OK | os.X_OK)
    if not writable:
        problems.append(
            Problem("this user cannot write to the state directory, so the service could not lock or save if it runs as this user")
        )
    out = ctx.out
    out.line("oauth state check")
    out.line(f"dir: {safe_text(ctx.state_dir, 200)}")
    out.line(f"dir_writable: {'yes' if writable else 'no'}")
    if result is None:
        out.line("state: not read (the store refused the file)")
    else:
        out.line(
            f"state: format={result.format} clients={result.clients} access={result.access_tokens} "
            f"refresh={result.refresh_tokens} tombstones={len(result.state.retired)} skipped={skipped} "
            f"migrated={result.migrated} pruned_on_load={result.pruned.total}"
        )
        if result.format == "none":
            out.line("note: there is no state file; the service starts with an empty state")
    if facts:
        out.line(f"file: {facts}")
    out.line(f"lock: {lock_status(ctx.state_dir)}")
    for problem in problems:
        out.line(f"problem: {problem.text}")
        if problem.hint:
            out.line(f"hint: {problem.hint}")
    if refused is not None:
        out.line(f"error: {type(refused).__name__}: {STATE_FILE_UNUSABLE}")
    ok = not problems and refused is None
    out.line("result: ok" if ok else "result: problems")
    return EXIT_OK if ok else EXIT_FAILED


def cmd_inventory(ctx: Context) -> int:
    _require_directory(ctx)
    result = oauth_store.load_state(ctx.state_dir, now=ctx.now, prune_expired=False)
    state, now, out = result.state, ctx.now, ctx.out
    ctx.guard.protect(state)
    out.line(f"oauth state inventory (times are UTC, now={when(now, seconds=True)})")
    out.line(
        f"state: format={result.format} clients={len(state.clients)} access={len(state.access_tokens)} "
        f"refresh={len(state.refresh_tokens)} tombstones={len(state.retired)} skipped={sum(result.skipped.values())}"
    )

    out.line(f"clients ({len(state.clients)}):")
    for client_id in sorted(state.clients, key=lambda cid: (state.clients[cid].client_id_issued_at or 0, cid)):
        client = state.clients[client_id]
        access = [r for r in state.access_tokens.values() if r.client_id == client_id]
        refresh = [t for t, r in state.refresh_tokens.items() if r.client_id == client_id]
        newest = max((i for i in (issued_at(state, t) for t in refresh) if i is not None), default=None)
        if any(usable(r.expires_at, now) for r in access):
            session = "live"
        elif refresh:
            session = "refresh-only"
        else:
            session = "tokenless"
        # the name and the redirect hosts are text the client chose: masked, not fatal (see Guard.mask)
        out.line(
            f'  client={id8(client_id)} name="{safe_text(client.client_name, guard=ctx.guard)}" '
            f"registered={when(client.client_id_issued_at)} redirect_hosts={redirect_hosts(client, ctx.guard)} "
            f"access={len(access)} refresh={len(refresh)} newest_issue={when(newest, none='none')} session={session}"
        )

    out.line(f"access tokens ({len(state.access_tokens)}):")
    for token, record in sorted(
        state.access_tokens.items(), key=lambda item: (id8(item[1].client_id), item[1].expires_at or float("inf"))
    ):
        out.line(
            f"  type={token_type(token)} client={id8(record.client_id)} expires={when(record.expires_at, none='never')} "
            f"status={access_status(record.expires_at, now)} "
            f"paired={'yes' if oauth_store.paired_refresh_tokens(state, token) else 'no'}"
        )

    out.line(f"refresh tokens ({len(state.refresh_tokens)}):")
    for token, record in sorted(
        state.refresh_tokens.items(), key=lambda item: (id8(item[1].client_id), issued_at(state, item[0]) or 0)
    ):
        meta = state.refresh_meta.get(token) or {}
        out.line(
            f"  type={token_type(token)} client={id8(record.client_id)} "
            f"family={stored_family8(state, token, result.format)} "
            f"origin={safe_text(meta.get('origin'), 10)} issued={when(issued_at(state, token))} "
            f"expires={when(record.expires_at, none='never')} "
            f"access={len(oauth_store.paired_access_tokens(state, token))}"
        )

    retired = sorted(entry["retired_at"] for entry in state.retired.values())
    if retired:
        out.line(f"tombstones: {len(retired)} (oldest={when(retired[0])} newest={when(retired[-1])})")
    else:
        out.line("tombstones: 0")
    return EXIT_OK


def _print_plan(ctx: Context, state: oauth_store.OAuthState, plan: StalePlan) -> None:
    out = ctx.out
    out.line(
        "purge plan: a pair is stale when it has no valid access token and its refresh token was issued more than "
        f"{STALE_AFTER_SECONDS // 86400} days ago (or has expired); a client with no token is stale after "
        f"{NEW_CLIENT_GRACE_SECONDS // 3600} hours"
    )
    # a client's name and redirect hosts are text the client chose: masked, not fatal (see Guard.mask)
    for pair in plan.pairs:
        client = state.clients.get(pair.client_id)
        name = safe_text(client.client_name if client else None, guard=ctx.guard)
        types = "/".join(sorted({token_type(token) for token in pair.access}) or ["-"]) + "+" + token_type(pair.refresh)
        out.line(
            f'  remove pair: client={id8(pair.client_id)} name="{name}" types={types} issued={when(pair.issued_at)} '
            f"last_access_expiry={when(pair.last_access_expiry, none='none')} why={pair.why}"
        )
    for orphan in plan.orphans:
        out.line(
            f"  remove access token without a refresh token: type={token_type(orphan.token)} "
            f"client={id8(orphan.client_id)} expired={when(orphan.expires_at)}"
        )
    for client_id in plan.clients:
        client = state.clients[client_id]
        out.line(
            f'  remove client: client={id8(client_id)} name="{safe_text(client.client_name, guard=ctx.guard)}" '
            f"registered={when(client.client_id_issued_at)} redirect_hosts={redirect_hosts(client, ctx.guard)}"
        )
    for client_id, why in plan.kept.items():
        client = state.clients[client_id]
        out.line(f'  keep client: client={id8(client_id)} name="{safe_text(client.client_name, guard=ctx.guard)}" why={why}')
    out.line(
        f"totals: pairs={len(plan.pairs)} access_tokens={plan.access_count} refresh_tokens={len(plan.pairs)} "
        f"clients={len(plan.clients)} kept_clients={len(plan.kept)}"
    )


def _known_keep_list(state: oauth_store.OAuthState, keep: Sequence[str]) -> None:
    """A typo in the keep list must not let the client it was meant for be purged: every id has to match a client."""
    for given in keep:
        if not match_clients(state, given):
            raise Refused(f"--keep-client {given} matches no client; nothing was changed")


def cmd_purge(ctx: Context) -> int:
    _require_directory(ctx)
    keep: list[str] = list(ctx.args.keep_client)
    if ctx.args.plan:
        result = oauth_store.load_state(ctx.state_dir, now=ctx.now, prune_expired=False)
        ctx.guard.protect(result.state)
        _known_keep_list(result.state, keep)
        plan = plan_stale(result.state, ctx.now, keep)
        _print_plan(ctx, result.state, plan)
        ctx.out.line("plan only: nothing was changed")
        return EXIT_OK
    with _exclusive(ctx):
        result = _load_for_change(ctx)
        _known_keep_list(result.state, keep)
        plan = plan_stale(result.state, ctx.now, keep)
        _print_plan(ctx, result.state, plan)
        if plan.empty:
            ctx.out.line("nothing to purge: nothing was changed")
            return EXIT_OK
        access, refresh, clients = apply_stale(result.state, plan)
        ctx.out.line(f"removed: access_tokens={access} refresh_tokens={refresh} clients={clients}")
        _write_and_verify(ctx, result.state)
    return EXIT_OK


def cmd_expire_access(ctx: Context) -> int:
    _require_directory(ctx)
    given = ctx.args.client
    with _exclusive(ctx):
        result = _load_for_change(ctx)
        state = result.state
        matches = match_clients(state, given)
        if not matches:
            raise Refused(f"--client {given} matches no client; nothing was changed")
        if len(matches) > 1:
            raise Refused(f"--client {given} matches more than one client; nothing was changed")
        client_id = matches[0]
        if not any(usable(r.expires_at, ctx.now) for r in state.refresh_tokens.values() if r.client_id == client_id):
            raise Refused(
                f"client {given} has no usable refresh token, so once its access token had expired it could not refresh and "
                "would have to sign in again; nothing was changed"
            )
        marked = expire_access(state, client_id, ctx.now)
        if not marked:
            ctx.out.line(f"client {given} has no valid access token (it has already expired): nothing was changed")
            return EXIT_OK
        ctx.out.line(
            f"expired: client={given} access_tokens={marked} expired_at={when(int(ctx.now) - EXPIRED_SECONDS_AGO, seconds=True)}; "
            "refresh tokens were not touched"
        )
        _write_and_verify(ctx, state)
    return EXIT_OK


def cmd_revoke_all(ctx: Context) -> int:
    _require_directory(ctx)
    with _exclusive(ctx):
        result = _load_for_change(ctx, allow_unreadable=True)
        state = result.state
        clients = len(state.clients)
        access, refresh = revoke_everything(state)
        if not (access or refresh):
            ctx.out.line("there is no token to revoke: nothing was changed")
            return EXIT_OK
        ctx.out.line(
            f"revoked: access_tokens={access} refresh_tokens={refresh}; client registrations stay (clients={clients}) "
            "and every client has to sign in again"
        )
        if sum(result.skipped.values()):
            ctx.out.line(f"dropped: {sum(result.skipped.values())} records that could not be read")
        _write_and_verify(ctx, state)
    return EXIT_OK


COMMANDS: dict[str, Callable[[Context], int]] = {
    "check": cmd_check,
    "inventory": cmd_inventory,
    "purge": cmd_purge,
    "expire-access": cmd_expire_access,
    "revoke-all": cmd_revoke_all,
}


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------


def _id8_argument(value: str) -> str:
    if not ID8_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(ID8_RULE)  # a fixed sentence: it does not repeat the value
    return value


class QuietParser(argparse.ArgumentParser):
    """An ArgumentParser whose errors never echo what was typed.

    The stock `error()` prints the offending text straight to stderr, past the Guard: `unrecognized arguments: <everything
    stray>` and `invalid choice: '<the command>'` repeat it. A token or a full client id pasted into the wrong place would
    land in the terminal and in any transcript that keeps it. This one prints the usage (built from the definitions of the
    parser, so it holds nothing that was typed) and one fixed sentence, then exits with EXIT_REFUSED. The sub-parsers are of
    this class too (`add_subparsers` builds them from the class of their parent)."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)  # not the default: print_usage writes to stdout unless it is told otherwise
        # `message` is argparse's text and may hold what was typed, so it is never printed. The one use made of it: a bad id
        # is recognized by the end of the message (_id8_argument raises ID8_RULE, our own fixed sentence) and the hint below,
        # also fixed, is added. Whatever else the message holds is dropped.
        hint = ""
        if message.endswith(ID8_RULE):
            hint = f" An id given to --client or --keep-client {ID8_RULE}, as inventory shows it."
        self.exit(EXIT_REFUSED, f"{self.prog}: error: {COMMAND_LINE_REFUSED}{hint}\n")


def build_parser() -> argparse.ArgumentParser:
    common = QuietParser(add_help=False)
    common.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        metavar="DIR",
        help="the OAuth state directory (default: .oauth-state next to this repository's root); it is never created",
    )
    parser = QuietParser(
        prog="oauth_state.py",
        description="Look at and tidy the OAuth state file without ever printing a token or a secret. Run it as the "
        "service user. --apply refuses while the service holds the state lock.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("check", parents=[common], help="what the service would load, the lock and the file mode; writes nothing")
    commands.add_parser("inventory", parents=[common], help="one line per client and per token; writes nothing")

    purge = commands.add_parser("purge", parents=[common], help="remove stale token pairs and old tokenless clients")
    purge.add_argument("--stale", action="store_true", required=True, help="the only kind of purge there is")
    purge.add_argument(
        "--keep-client",
        action="extend",
        nargs="+",
        type=_id8_argument,
        default=[],
        metavar="ID8",
        help="leave this client (and every token of it) alone; the first 8 characters of its id, as inventory shows them",
    )
    mode = purge.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="show what --apply would remove and change nothing")
    mode.add_argument("--apply", action="store_true", help="remove it (the service must be stopped)")

    expire = commands.add_parser(
        "expire-access", parents=[common], help="mark the valid access tokens of one client as expired (the refresh test)"
    )
    expire.add_argument("--client", required=True, type=_id8_argument, metavar="ID8", help="the first 8 characters of the client id")
    expire.add_argument("--apply", action="store_true", required=True, help="do it (the service must be stopped)")

    revoke = commands.add_parser(
        "revoke-all", parents=[common], help="remove every access and refresh token; clients stay registered"
    )
    revoke.add_argument("--apply", action="store_true", required=True, help="do it (the service must be stopped)")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Callable[[], float] = time.time,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    guard = Guard()
    out = Output(stdout if stdout is not None else sys.stdout, guard)
    err = Output(stderr if stderr is not None else sys.stderr, guard)
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse: 0 after --help, 2 for a command line that does not fit
        return exc.code if isinstance(exc.code, int) else EXIT_REFUSED
    ctx = Context(args=args, now=clock(), out=out, err=err, guard=guard)
    try:
        return COMMANDS[args.command](ctx)
    except Refused as exc:
        err.line(f"refused: {exc}")
        return EXIT_REFUSED
    except oauth_store.StateLockedError:
        err.line(
            "refused: another process holds the OAuth state lock (the service is running, or another run of this script); "
            "stop the service first. Nothing was changed. (StateLockedError)"
        )
        return EXIT_LOCKED
    except oauth_store.StateFileError as exc:
        err.line(f"error: {type(exc).__name__}: {STATE_FILE_UNUSABLE}")
        return EXIT_FAILED
    except LeakError as exc:
        err.line(f"error: {type(exc).__name__}: a line held a credential and was not printed")
        return EXIT_FAILED
    except VerificationError as exc:
        err.line(
            f"error: {type(exc).__name__}: the file was written but does not read back as written; "
            "restore the backup taken before this run"
        )
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001  the type only: the message of an exception may hold a value
        err.line(f"error: {type(exc).__name__}")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
