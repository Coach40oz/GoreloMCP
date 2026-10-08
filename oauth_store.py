"""The OAuth state file of the personal login: read it, check it, migrate it, prune it, write it.

`.oauth-state/oauth_tokens.json` holds live credentials: client secrets, access tokens and refresh tokens. This module is
the one place that reads, checks, migrates, prunes and writes that file, so every rule about it lives here and is tested
here (`tests/test_oauth_store.py`). The server (`personal_auth.PersonalAuthProvider`) goes through it, and so will the
operator script. Nothing here prints or logs a token, a secret or the content of a record: a warning carries counts, an
error names the kind of problem and at most a position in the file.

File format, version 2. Version 1, which earlier versions wrote, is the same file without the keys marked "new":

    {
      "version": 2,                                                    (new)
      "clients":        {client_id: OAuthClientInformationFull as JSON},
      "access_tokens":  {token: {token, client_id, scopes, expires_at, resource}},
      "refresh_tokens": {token: {token, client_id, scopes, expires_at}},
      "a2r":            {access token: its refresh token},
      "r2a":            {refresh token: its access token},
      "refresh_meta":   {refresh token: {"family", "issued_at", "origin"}},   (new)
      "retired":        {sha256 hex of a rotated refresh token: {"family", "retired_at"}}   (new)
    }

Migrating version 1 to version 2 only ADDS those three keys. A token string, an expiry, a client id or a client secret is
never changed, and the older code still loads a version 2 file because it ignores the keys it does not know
(`tests/test_personal_auth_tokens.py` proves both directions with a frozen copy of that code). A refresh token that has
no entry in `refresh_meta` (every one in a version 1 file) gets a family of its own with origin "legacy"; its `issued_at`
is then an estimate taken from its paired access token, or the time of the first load when that is not possible.

Rules, each pinned by a test:

* No file is a first start: an empty state. A file that is not JSON, or whose top level or sections are not objects, or
  whose `version` this release does not know, raises StateFileError and is left exactly as it was; the service refuses to
  start rather than silently forget every session.
* One bad record (it does not validate, or its key is not its own id) is skipped, as is a map entry that points at a
  token that is not there; the load logs ONE warning with counts only.
* Writing is atomic: a temporary file in the same directory (created with O_EXCL, mode 0600), written, fsynced, renamed
  over the real file with os.replace, then the directory is fsynced. On any error the temporary file is removed and the
  old file is untouched, byte for byte.
* The process that writes holds an exclusive flock on `.lock` in the state directory for as long as it runs (StateLock),
  so there is never a second writer. Files this module creates are mode 0600, and when root creates one in a directory
  that another user owns it is given to that user, so a script started as root by mistake cannot lock the service out.
* Pruning removes access and refresh tokens that expired more than EXPIRED_TOKEN_RETENTION_SECONDS ago (an expired
  refresh token takes its paired access token with it), rotated-token tombstones older than
  TOMBSTONE_RETENTION_SECONDS, and map or metadata entries that point at nothing. It never removes a client. A token
  that expired recently stays for a week on purpose: while it is still known, presenting it can be recognized and logged
  as an expired token instead of an unknown one, and a refresh token that belongs to it is still usable.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import ValidationError

logger = logging.getLogger("oauth-store")

STATE_FILE_NAME = "oauth_tokens.json"
LOCK_FILE_NAME = ".lock"
FORMAT_VERSION = 2
KNOWN_VERSIONS = (1, 2)

DIR_MODE = 0o700
FILE_MODE = 0o600

# An expired token is kept this long after it expired (see the module docstring).
EXPIRED_TOKEN_RETENTION_SECONDS = 7 * 24 * 60 * 60
# A tombstone (the sha256 of a refresh token that was rotated away) is kept this long.
TOMBSTONE_RETENTION_SECONDS = 90 * 24 * 60 * 60
# Safety limit, not a policy: a client that refreshed in a loop could otherwise grow the file without bound, and the
# whole file is rewritten on every save. The oldest tombstones go first. One refresh an hour is about 2200 per 90 days.
MAX_TOMBSTONES = 10_000

ORIGIN_CODE = "code"  # the pair came from an authorization code exchange (a sign-in)
ORIGIN_REFRESH = "refresh"  # the pair came from a refresh token exchange (a rotation)
ORIGIN_LEGACY = "legacy"  # the token was already in a version 1 file when this release first loaded it
ORIGINS = (ORIGIN_CODE, ORIGIN_REFRESH, ORIGIN_LEGACY)

# How long the access tokens that older releases issued lived, by prefix. Used only to estimate when a legacy refresh
# token was issued: "pat_" came from a sign-in (30 days), "test_access_token_" from the framework's refresh (1 hour).
LEGACY_ACCESS_LIFETIMES = (("pat_", 30 * 24 * 60 * 60), ("test_access_token_", 60 * 60))

TEMP_FILE_GLOB = f".{STATE_FILE_NAME}.*.tmp"

# Where a load reports what it skipped.
SKIP_SECTIONS = ("clients", "access_tokens", "refresh_tokens", "maps", "refresh_meta", "retired")

_OBJECT_SECTIONS = ("clients", "access_tokens", "refresh_tokens", "a2r", "r2a", "refresh_meta", "retired")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class StateFileError(RuntimeError):
    """The OAuth state file or its directory cannot be used safely: the service must refuse to start (or to write)."""


class StateLockedError(StateFileError):
    """Another process holds the exclusive lock on the state directory (the server, or the operator script)."""


# --------------------------------------------------------------------------
# The state in memory
# --------------------------------------------------------------------------


@dataclass
class OAuthState:
    """Everything the file holds. The provider builds one over its own dictionaries (PersonalAuthProvider._state_view),
    so the functions below change the live state when they are given that view."""

    clients: dict[str, OAuthClientInformationFull] = field(default_factory=dict)
    access_tokens: dict[str, AccessToken] = field(default_factory=dict)
    refresh_tokens: dict[str, RefreshToken] = field(default_factory=dict)
    access_to_refresh: dict[str, str] = field(default_factory=dict)
    refresh_to_access: dict[str, str] = field(default_factory=dict)
    refresh_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    retired: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class PruneCounts:
    """What one prune removed: tokens and tombstones. (Map and metadata entries that pointed at nothing go quietly.)"""

    access_tokens: int = 0
    refresh_tokens: int = 0
    tombstones: int = 0

    @property
    def total(self) -> int:
        return self.access_tokens + self.refresh_tokens + self.tombstones


@dataclass
class LoadResult:
    state: OAuthState
    format: str  # what was on disk: "none" (no file), "v1" or "v2"
    clients: int  # records the file held, valid ones only, before pruning
    access_tokens: int
    refresh_tokens: int
    skipped: dict[str, int]  # invalid records and dangling map entries that were skipped, by SKIP_SECTIONS
    migrated: int  # refresh tokens that had no metadata and were given a "legacy" family
    pruned: PruneCounts  # what the prune that follows a load removed (nothing when prune_expired is False)


def token_digest(token: str) -> str:
    """sha256 of a token as lower-case hex: all a tombstone keeps of a rotated refresh token."""
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


def new_family_id() -> str:
    """A fresh family id: 16 hex characters. Not a secret; the first 8 appear in log lines."""
    return secrets.token_hex(8)


# --------------------------------------------------------------------------
# Pairs, families and tombstones: operations on an OAuthState
# --------------------------------------------------------------------------


def paired_access_tokens(state: OAuthState, refresh_token: str) -> list[str]:
    """The access tokens that the maps pair with a refresh token, found through either map."""
    found: list[str] = []
    direct = state.refresh_to_access.get(refresh_token)
    if direct is not None and direct in state.access_tokens:
        found.append(direct)
    for access, refresh in state.access_to_refresh.items():
        if refresh == refresh_token and access in state.access_tokens and access not in found:
            found.append(access)
    return found


def paired_refresh_tokens(state: OAuthState, access_token: str) -> list[str]:
    """The refresh tokens that the maps pair with an access token, found through either map."""
    found: list[str] = []
    direct = state.access_to_refresh.get(access_token)
    if direct is not None and direct in state.refresh_tokens:
        found.append(direct)
    for refresh, access in state.refresh_to_access.items():
        if access == access_token and refresh in state.refresh_tokens and refresh not in found:
            found.append(refresh)
    return found


def remove_access_token(state: OAuthState, token: str) -> int:
    """Remove an access token and every map entry that names it. Returns 1 if the token was there, else 0."""
    removed = 1 if state.access_tokens.pop(token, None) is not None else 0
    state.access_to_refresh.pop(token, None)
    for refresh in [r for r, a in state.refresh_to_access.items() if a == token]:
        del state.refresh_to_access[refresh]
    return removed


def remove_refresh_token(state: OAuthState, token: str, *, with_pair: bool = True) -> tuple[int, int]:
    """Remove a refresh token, its metadata, every map entry that names it and (by default) its paired access tokens.
    Returns (access tokens removed, refresh tokens removed)."""
    access_removed = 0
    if with_pair:
        for access in paired_access_tokens(state, token):
            access_removed += remove_access_token(state, access)
    refresh_removed = 1 if state.refresh_tokens.pop(token, None) is not None else 0
    state.refresh_to_access.pop(token, None)
    for access in [a for a, r in state.access_to_refresh.items() if r == token]:
        del state.access_to_refresh[access]
    state.refresh_meta.pop(token, None)
    return access_removed, refresh_removed


def _estimated_issue_time(state: OAuthState, refresh_token: str, now: float) -> int:
    for access in paired_access_tokens(state, refresh_token):
        record = state.access_tokens[access]
        if record.expires_at is None:
            continue
        for prefix, lifetime in LEGACY_ACCESS_LIFETIMES:
            if record.token.startswith(prefix):
                estimate = record.expires_at - lifetime
                if 0 < estimate <= now:
                    return int(estimate)
    return int(now)


def ensure_family(state: OAuthState, refresh_token: str, now: float) -> str:
    """The family id of a refresh token. A token with no metadata (a legacy token) gets a family of its own."""
    meta = state.refresh_meta.get(refresh_token)
    if meta is None:
        meta = {
            "family": new_family_id(),
            "issued_at": _estimated_issue_time(state, refresh_token, now),
            "origin": ORIGIN_LEGACY,
        }
        state.refresh_meta[refresh_token] = meta
    return str(meta["family"])


def retire_refresh_token(state: OAuthState, refresh_token: str, now: float) -> str:
    """Rotate a refresh token away: keep a tombstone (the sha256 of the token, its family and the time), remove the token
    with its pair. Returns the family id."""
    family = ensure_family(state, refresh_token, now)
    state.retired[token_digest(refresh_token)] = {"family": family, "retired_at": int(now)}
    remove_refresh_token(state, refresh_token)
    return family


def revoke_family(state: OAuthState, family: str) -> tuple[int, int]:
    """Remove every live refresh token of a family with its paired access tokens. Tombstones stay, so that a later
    presentation of a rotated token is still recognized. Returns (access tokens removed, refresh tokens removed)."""
    access_removed = refresh_removed = 0
    for token in [t for t, meta in state.refresh_meta.items() if meta.get("family") == family]:
        access, refresh = remove_refresh_token(state, token)
        access_removed += access
        refresh_removed += refresh
    return access_removed, refresh_removed


def clients_with_tokens(state: OAuthState) -> set[str]:
    """The ids of the clients that hold an access token or a refresh token (an expired one still counts)."""
    return {record.client_id for record in state.access_tokens.values()} | {
        record.client_id for record in state.refresh_tokens.values()
    }


def remove_tokenless_clients(state: OAuthState, candidates: Iterable[str]) -> list[str]:
    """Remove the candidate clients that hold no token. A client that still has a token is never removed, whatever the
    caller asked. Returns the ids that were removed."""
    holders = clients_with_tokens(state)
    removed: list[str] = []
    for client_id in candidates:
        if client_id in state.clients and client_id not in holders:
            del state.clients[client_id]
            removed.append(client_id)
    return removed


def _sweep(state: OAuthState) -> None:
    """Drop map entries with a missing end and metadata of refresh tokens that are gone."""
    dangling = [a for a, r in state.access_to_refresh.items() if a not in state.access_tokens or r not in state.refresh_tokens]
    for access in dangling:
        del state.access_to_refresh[access]
    dangling = [r for r, a in state.refresh_to_access.items() if r not in state.refresh_tokens or a not in state.access_tokens]
    for refresh in dangling:
        del state.refresh_to_access[refresh]
    for refresh in [r for r in state.refresh_meta if r not in state.refresh_tokens]:
        del state.refresh_meta[refresh]


def prune(
    state: OAuthState,
    now: float,
    *,
    retention: float = EXPIRED_TOKEN_RETENTION_SECONDS,
    tombstone_retention: float = TOMBSTONE_RETENTION_SECONDS,
    max_tombstones: int = MAX_TOMBSTONES,
) -> PruneCounts:
    """Remove what the module docstring lists under "Pruning". Changes `state` in place. Never removes a client."""
    counts = PruneCounts()
    cutoff = now - retention
    for token in [t for t, r in state.refresh_tokens.items() if r.expires_at is not None and r.expires_at < cutoff]:
        access, refresh = remove_refresh_token(state, token)
        counts.access_tokens += access
        counts.refresh_tokens += refresh
    for token in [t for t, r in state.access_tokens.items() if r.expires_at is not None and r.expires_at < cutoff]:
        counts.access_tokens += remove_access_token(state, token)
    _sweep(state)
    old = [digest for digest, entry in state.retired.items() if entry["retired_at"] < now - tombstone_retention]
    for digest in old:
        del state.retired[digest]
    counts.tombstones += len(old)
    excess = len(state.retired) - max_tombstones
    if excess > 0:
        for digest in sorted(state.retired, key=lambda d: state.retired[d]["retired_at"])[:excess]:
            del state.retired[digest]
        counts.tombstones += excess
    return counts


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _describe(value: Any) -> str:
    return type(value).__name__


def _read_document(path: Path) -> dict[str, Any] | None:
    """The parsed file, or None when there is no file. Anything else that is wrong raises StateFileError, and the file is
    not touched. The messages carry no value from the file (a JSONDecodeError holds the whole document: it is dropped)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateFileError(f"cannot read the OAuth state file {path}: {exc.strerror or _describe(exc)}") from None
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateFileError(
            f"the OAuth state file {path} is not valid JSON (line {exc.lineno}, column {exc.colno}); it was not changed"
        ) from None
    except (ValueError, RecursionError) as exc:
        raise StateFileError(f"the OAuth state file {path} is not valid JSON ({_describe(exc)}); it was not changed") from None
    if not isinstance(document, dict):
        raise StateFileError(
            f"the OAuth state file {path} holds a JSON {_describe(document)} at its top level, not an object; it was not changed"
        )
    for section in _OBJECT_SECTIONS:
        if section in document and not isinstance(document[section], dict):
            raise StateFileError(
                f"the OAuth state file {path} has a JSON {_describe(document[section])} for {section!r}, "
                "not an object; it was not changed"
            )
    return document


def _version_of(document: dict[str, Any], path: Path) -> int:
    if "version" not in document:
        return 1
    version = document["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version not in KNOWN_VERSIONS:
        shown = version if isinstance(version, int) and not isinstance(version, bool) else _describe(version)
        raise StateFileError(
            f"the OAuth state file {path} has format version {shown}, which this release does not know "
            f"(it knows {', '.join(str(v) for v in KNOWN_VERSIONS)}); it was not changed"
        )
    return version


def _timestamp(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(value) if value >= 0 else None


def _valid_family(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 64 and value.isascii() and value.isalnum()


def _records(name: str, raw: dict[str, Any], model: Any, id_attr: str, skipped: dict[str, int]) -> dict[str, Any]:
    records: dict[str, Any] = {}
    for key, value in raw.items():
        try:
            record = model.model_validate(value)
        except ValidationError:
            skipped[name] += 1
            continue
        identifier = getattr(record, id_attr, None)
        owner = getattr(record, "client_id", None)
        if not isinstance(identifier, str) or not identifier or identifier != key or not isinstance(owner, str) or not owner:
            skipped[name] += 1
            continue
        records[key] = record
    return records


def _pairs(raw: dict[str, Any], left: dict[str, Any], right: dict[str, Any], skipped: dict[str, int]) -> dict[str, str]:
    kept: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, str) and key in left and value in right:
            kept[key] = value
        else:
            skipped["maps"] += 1
    return kept


def _meta_entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    family, issued_at, origin = value.get("family"), _timestamp(value.get("issued_at")), value.get("origin")
    if not _valid_family(family) or issued_at is None or origin not in ORIGINS:
        return None
    return {"family": family, "issued_at": issued_at, "origin": origin}


def _tombstone_entry(key: str, value: Any) -> dict[str, Any] | None:
    if not _DIGEST.fullmatch(key) or not isinstance(value, dict):
        return None
    family, retired_at = value.get("family"), _timestamp(value.get("retired_at"))
    if not _valid_family(family) or retired_at is None:
        return None
    return {"family": family, "retired_at": retired_at}


def _parse(document: dict[str, Any], skipped: dict[str, int]) -> OAuthState:
    clients = _records("clients", document.get("clients", {}), OAuthClientInformationFull, "client_id", skipped)
    access = _records("access_tokens", document.get("access_tokens", {}), AccessToken, "token", skipped)
    refresh = _records("refresh_tokens", document.get("refresh_tokens", {}), RefreshToken, "token", skipped)
    meta: dict[str, dict[str, Any]] = {}
    for token, value in document.get("refresh_meta", {}).items():
        entry = _meta_entry(value)
        if token in refresh and entry is not None:
            meta[token] = entry
        else:
            skipped["refresh_meta"] += 1
    retired: dict[str, dict[str, Any]] = {}
    for digest, value in document.get("retired", {}).items():
        entry = _tombstone_entry(digest, value)
        if entry is not None:
            retired[digest] = entry
        else:
            skipped["retired"] += 1
    return OAuthState(
        clients=clients,
        access_tokens=access,
        refresh_tokens=refresh,
        access_to_refresh=_pairs(document.get("a2r", {}), access, refresh, skipped),
        refresh_to_access=_pairs(document.get("r2a", {}), refresh, access, skipped),
        refresh_meta=meta,
        retired=retired,
    )


def load_state(state_dir: str | os.PathLike[str], *, now: float | None = None, prune_expired: bool = True) -> LoadResult:
    """Read, check and migrate the state file in `state_dir`, then (unless prune_expired is False) prune it.

    Never writes: the file stays as it was until the provider next saves, so a first start of a new release leaves a
    version 1 file byte for byte as it found it. No file is an empty state (format "none"). Raises StateFileError for a
    file that cannot be trusted (see the module docstring)."""
    now = time.time() if now is None else float(now)
    path = Path(state_dir) / STATE_FILE_NAME
    document = _read_document(path)
    if document is None:
        return LoadResult(OAuthState(), "none", 0, 0, 0, dict.fromkeys(SKIP_SECTIONS, 0), 0, PruneCounts())
    version = _version_of(document, path)
    skipped = dict.fromkeys(SKIP_SECTIONS, 0)
    state = _parse(document, skipped)
    loaded = (len(state.clients), len(state.access_tokens), len(state.refresh_tokens))
    migrated = 0
    for token in state.refresh_tokens:
        if token not in state.refresh_meta:
            ensure_family(state, token, now)
            migrated += 1
    if sum(skipped.values()):
        logger.warning(
            "oauth state: skipped invalid records (%s)", " ".join(f"{name}={skipped[name]}" for name in SKIP_SECTIONS)
        )
    pruned = prune(state, now) if prune_expired else PruneCounts()
    return LoadResult(state, f"v{version}", *loaded, skipped, migrated, pruned)


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def dump_state(state: OAuthState) -> dict[str, Any]:
    """The JSON document for a state: version 2, the five version 1 sections in the form version 1 wrote them, plus the
    two new sections."""
    return {
        "version": FORMAT_VERSION,
        "clients": {key: record.model_dump(mode="json") for key, record in state.clients.items()},
        "access_tokens": {key: record.model_dump(mode="json") for key, record in state.access_tokens.items()},
        "refresh_tokens": {key: record.model_dump(mode="json") for key, record in state.refresh_tokens.items()},
        "a2r": dict(state.access_to_refresh),
        "r2a": dict(state.refresh_to_access),
        "refresh_meta": {key: dict(entry) for key, entry in state.refresh_meta.items()},
        "retired": {key: dict(entry) for key, entry in state.retired.items()},
    }


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def _adopt_directory_owner(fd: int, directory: Path) -> None:
    """When root creates a file in a directory that another user owns, give the file to that user. The service runs as that
    user, and a root-owned file with mode 0600 in its state directory (a lock file, or a state file written by a script
    that was started as root by mistake) would stop it from starting. Does nothing for any other user. Best effort."""
    if os.geteuid() != 0:
        return
    try:
        owner = os.stat(directory)
        if owner.st_uid != 0:
            os.fchown(fd, owner.st_uid, owner.st_gid)
    except OSError:
        pass


def _atomic_write(path: Path, payload: bytes) -> None:
    """Replace `path` with `payload` or leave it exactly as it was (see the module docstring for the steps). The
    directory must exist: nothing here creates one."""
    temp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, FILE_MODE)
    except OSError as exc:
        raise StateFileError(f"cannot write the OAuth state file {path}: {exc.strerror or _describe(exc)}") from None
    try:
        try:
            os.fchmod(fd, FILE_MODE)
            _adopt_directory_owner(fd, path.parent)
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, path)
    except OSError as exc:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise StateFileError(f"cannot write the OAuth state file {path}: {exc.strerror or _describe(exc)}") from None
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    try:
        _fsync_directory(path.parent)
    except OSError as exc:
        # The new file is already in place; only its durability is in doubt. Treating this as a failed save would make
        # the caller undo a change that the file already holds, so it is reported and the save counts as done.
        logger.warning("oauth state: could not fsync the state directory (%s)", exc.strerror or _describe(exc))


def write_state(state_dir: str | os.PathLike[str], state: OAuthState) -> None:
    """Write `state` to `state_dir` atomically. Raises StateFileError if it cannot be written; the old file is then
    untouched and no temporary file is left. Does not prune and does not create the directory. Map and metadata entries
    that point at nothing are dropped first, so that what is written always loads without a warning."""
    _sweep(state)
    try:
        payload = json.dumps(dump_state(state), indent=2, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise StateFileError(f"the OAuth state cannot be written as JSON ({_describe(exc)})") from None
    _atomic_write(Path(state_dir) / STATE_FILE_NAME, payload)


def remove_stale_temp_files(state_dir: str | os.PathLike[str]) -> int:
    """Delete temporary files that a process killed in the middle of a save left behind (they hold a full copy of the
    credentials). Only call this while holding the StateLock: then no save can be running. Returns how many were removed."""
    removed = 0
    directory = Path(state_dir)
    if not directory.is_dir():
        return 0
    for leftover in directory.glob(TEMP_FILE_GLOB):
        try:
            leftover.unlink()
            removed += 1
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------
# The lock
# --------------------------------------------------------------------------


class StateLock:
    """An exclusive flock on `.lock` in the state directory, held until release() (or until the process ends: the
    operating system drops it then, so a crash never leaves a stale lock).

    The server takes it when the provider is built and keeps it, so the operator script (which asks for it before it
    writes) refuses to run while the service is up. `create_dir=True` creates a missing state directory with mode 0700
    (the server does); the default never creates one (the script must not). An existing directory keeps its mode."""

    def __init__(self, state_dir: str | os.PathLike[str], *, create_dir: bool = False) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / LOCK_FILE_NAME
        self._create_dir = create_dir
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, timeout: float = 0.0) -> StateLock:
        """Take the lock, waiting up to `timeout` seconds for another holder. Raises StateLockedError if it stays taken,
        and StateFileError if the directory or the lock file cannot be used."""
        if self._fd is not None:
            return self
        if self._create_dir:
            try:
                self.state_dir.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
            except OSError as exc:
                raise StateFileError(
                    f"cannot create the OAuth state directory {self.state_dir}: {exc.strerror or _describe(exc)}"
                ) from None
        elif not self.state_dir.is_dir():
            raise StateFileError(f"the OAuth state directory {self.state_dir} does not exist")
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, FILE_MODE)
        except OSError as exc:
            raise StateFileError(f"cannot open the OAuth state lock file {self.path}: {exc.strerror or _describe(exc)}") from None
        _adopt_directory_owner(fd, self.state_dir)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    os.close(fd)
                    raise StateFileError(f"cannot lock {self.path}: {exc.strerror or _describe(exc)}") from None
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise StateLockedError(
                        f"another process holds the OAuth state lock {self.path} (the server, or the operator script)"
                    ) from None
                time.sleep(0.05)
        self._fd = fd
        return self

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> StateLock:
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass
