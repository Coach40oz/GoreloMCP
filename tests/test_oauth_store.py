"""oauth_store.py: reading, checking, migrating, pruning and atomically writing the OAuth state file, and its lock.

Everything runs in temporary directories with fake data and a fixed clock. The state file of the running service is
never touched: these tests only know the format.
"""

from __future__ import annotations

import copy
import errno
import gc
import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

import oauth_store
from oauth_store import (
    ORIGIN_CODE,
    ORIGIN_LEGACY,
    ORIGIN_REFRESH,
    OAuthState,
    StateFileError,
    StateLock,
    StateLockedError,
)

DAY = 24 * 60 * 60
NOW = 1_800_000_000  # a fixed "now" (January 2027); every function that takes a clock is given this one
STATE_FILE = oauth_store.STATE_FILE_NAME
V1_KEYS = {"clients", "access_tokens", "refresh_tokens", "a2r", "r2a"}
V2_KEYS = V1_KEYS | {"version", "refresh_meta", "retired"}
EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)


# --------------------------------------------------------------------------
# Building states and files
# --------------------------------------------------------------------------


def client_id_of(n: int) -> str:
    return f"{n:08d}-aaaa-4bbb-8ccc-{n:012d}"


def make_client(n: int = 1) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id_of(n),
        client_secret=f"client-secret-{n}-" + "x" * 20,
        client_id_issued_at=NOW - 100 * DAY,
        redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name=f"Test client {n}",
    )


class Builder:
    """A state in memory, plus the version 1 document that earlier versions would have written for it."""

    def __init__(self) -> None:
        self.state = OAuthState()

    def client(self, n: int = 1) -> str:
        record = make_client(n)
        self.state.clients[record.client_id] = record
        return record.client_id

    def pair(
        self,
        client_id: str,
        name: str,
        *,
        access_expires: int | None,
        refresh_expires: int | None = None,
        prefixes: tuple[str, str] = ("pat_", "prt_"),
    ) -> tuple[str, str]:
        access, refresh = f"{prefixes[0]}{name}-access", f"{prefixes[1]}{name}-refresh"
        self.state.access_tokens[access] = AccessToken(
            token=access, client_id=client_id, scopes=["mcp"], expires_at=access_expires
        )
        self.state.refresh_tokens[refresh] = RefreshToken(
            token=refresh, client_id=client_id, scopes=["mcp"], expires_at=refresh_expires
        )
        self.state.access_to_refresh[access] = refresh
        self.state.refresh_to_access[refresh] = access
        return access, refresh

    def v1_document(self) -> dict:
        """Exactly the five keys that PersonalAuthProvider._save_state wrote before version 2."""
        state = self.state
        return {
            "clients": {k: v.model_dump(mode="json") for k, v in state.clients.items()},
            "access_tokens": {k: v.model_dump(mode="json") for k, v in state.access_tokens.items()},
            "refresh_tokens": {k: v.model_dump(mode="json") for k, v in state.refresh_tokens.items()},
            "a2r": dict(state.access_to_refresh),
            "r2a": dict(state.refresh_to_access),
        }


def put(state_dir: Path, document: object) -> bytes:
    """Write `document` the way the old code did (json.dumps(indent=2)) and return the bytes."""
    state_dir.mkdir(parents=True, exist_ok=True)
    data = json.dumps(document, indent=2).encode()
    (state_dir / STATE_FILE).write_bytes(data)
    return data


def listing(state_dir: Path) -> list[str]:
    return sorted(os.listdir(state_dir))


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    path = tmp_path / "oauth-state"
    path.mkdir()
    return path


def a_state_with_everything() -> OAuthState:
    """Two clients, a live pair, a pair that expired two days ago, family metadata and two tombstones."""
    builder = Builder()
    one, two = builder.client(1), builder.client(2)
    access_1, refresh_1 = builder.pair(one, "one", access_expires=NOW + 30 * DAY)
    access_2, refresh_2 = builder.pair(two, "two", access_expires=NOW - 2 * DAY)
    state = builder.state
    state.refresh_meta[refresh_1] = {"family": "aaaaaaaaaaaaaaaa", "issued_at": NOW - DAY, "origin": ORIGIN_CODE}
    state.refresh_meta[refresh_2] = {"family": "bbbbbbbbbbbbbbbb", "issued_at": NOW - 3 * DAY, "origin": ORIGIN_REFRESH}
    state.retired[oauth_store.token_digest("prt_old-one")] = {"family": "aaaaaaaaaaaaaaaa", "retired_at": NOW - DAY}
    state.retired[oauth_store.token_digest("prt_old-two")] = {"family": "bbbbbbbbbbbbbbbb", "retired_at": NOW - 5 * DAY}
    return state


# --------------------------------------------------------------------------
# Constants the documents and the other modules rely on
# --------------------------------------------------------------------------


def test_the_constants_are_the_ones_the_design_names():
    assert oauth_store.FORMAT_VERSION == 2 and oauth_store.KNOWN_VERSIONS == (1, 2)
    assert oauth_store.STATE_FILE_NAME == "oauth_tokens.json" and oauth_store.LOCK_FILE_NAME == ".lock"
    assert oauth_store.TOMBSTONE_RETENTION_SECONDS == 90 * DAY
    assert oauth_store.EXPIRED_TOKEN_RETENTION_SECONDS == 7 * DAY
    assert oauth_store.FILE_MODE == 0o600 and oauth_store.DIR_MODE == 0o700
    assert oauth_store.ORIGINS == (ORIGIN_CODE, ORIGIN_REFRESH, ORIGIN_LEGACY)
    assert issubclass(StateLockedError, StateFileError) and issubclass(StateFileError, RuntimeError)


def test_the_module_and_its_tests_hold_no_em_or_en_dash():
    for path in (Path(oauth_store.__file__), Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert EM_DASH not in text and EN_DASH not in text, path.name
        assert text.isascii(), path.name


def test_token_digest_is_the_sha256_hex_and_a_family_id_is_16_hex_characters():
    assert oauth_store.token_digest("prt_abc") == hashlib.sha256(b"prt_abc").hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", oauth_store.token_digest("prt_abc"))
    assert oauth_store.token_digest("a lone surrogate \ud800") == oauth_store.token_digest("a lone surrogate ?")
    ids = {oauth_store.new_family_id() for _ in range(50)}
    assert len(ids) == 50 and all(re.fullmatch(r"[0-9a-f]{16}", value) for value in ids)


# --------------------------------------------------------------------------
# Loading: no file, version 1, version 2
# --------------------------------------------------------------------------


def test_no_file_is_a_first_start_and_nothing_is_created(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = oauth_store.load_state(empty, now=NOW)
    assert result.format == "none"
    assert (result.clients, result.access_tokens, result.refresh_tokens, result.migrated) == (0, 0, 0, 0)
    assert result.pruned.total == 0 and sum(result.skipped.values()) == 0
    assert result.state.clients == {} and result.state.retired == {}
    assert listing(empty) == []


def test_a_missing_directory_is_a_first_start_too_and_is_not_created(tmp_path):
    result = oauth_store.load_state(tmp_path / "does-not-exist", now=NOW)
    assert result.format == "none"
    assert not (tmp_path / "does-not-exist").exists()


def test_an_empty_json_object_is_an_empty_version_1_state(state_dir):
    put(state_dir, {})
    result = oauth_store.load_state(state_dir, now=NOW)
    assert result.format == "v1" and (result.clients, result.access_tokens, result.refresh_tokens) == (0, 0, 0)


def test_a_version_1_file_loads_and_is_migrated_in_memory_without_writing_the_file(state_dir):
    builder = Builder()
    one, two = builder.client(1), builder.client(2)
    builder.client(3)  # a client with no token at all
    builder.pair(one, "live", access_expires=NOW + 20 * DAY)
    builder.pair(two, "old", access_expires=NOW - 200 * DAY)
    before = put(state_dir, builder.v1_document())
    mtime = (state_dir / STATE_FILE).stat().st_mtime_ns

    result = oauth_store.load_state(state_dir, now=NOW)

    assert result.format == "v1"
    assert (result.clients, result.access_tokens, result.refresh_tokens) == (3, 2, 2)  # counts as read, before pruning
    assert result.migrated == 2
    assert result.pruned.access_tokens == 1 and result.pruned.total == 1  # the expired access token is long expired
    assert len(result.state.access_tokens) == 1 and len(result.state.refresh_tokens) == 2
    assert (state_dir / STATE_FILE).read_bytes() == before and (state_dir / STATE_FILE).stat().st_mtime_ns == mtime
    assert listing(state_dir) == [STATE_FILE]
    families = set()
    for token, meta in result.state.refresh_meta.items():
        assert token in result.state.refresh_tokens
        assert set(meta) == {"family", "issued_at", "origin"} and meta["origin"] == ORIGIN_LEGACY
        assert re.fullmatch(r"[0-9a-f]{16}", meta["family"])
        families.add(meta["family"])
    assert len(families) == 2  # one family each
    assert result.state.retired == {}


def test_migrating_version_1_to_2_only_adds_keys_and_changes_no_value(state_dir):
    builder = Builder()
    one, two = builder.client(1), builder.client(2)
    builder.pair(one, "a", access_expires=NOW + 5 * DAY)
    builder.pair(two, "b", access_expires=NOW + 10 * DAY, refresh_expires=None)
    original = builder.v1_document()
    assert set(original) == V1_KEYS
    put(state_dir, original)

    document = oauth_store.dump_state(oauth_store.load_state(state_dir, now=NOW).state)

    assert set(document) == V2_KEYS
    assert document["version"] == 2
    for key in V1_KEYS:  # every old key holds exactly what it held: tokens, expiries, client ids, secrets, maps
        assert document[key] == original[key], key
    assert set(document["refresh_meta"]) == set(original["refresh_tokens"])
    assert document["retired"] == {}
    # and it is valid JSON that survives a round trip unchanged
    assert json.loads(json.dumps(document)) == document


def test_a_legacy_refresh_token_gets_an_issue_time_estimated_from_its_access_token(state_dir):
    builder = Builder()
    cid = builder.client(1)
    _, from_signin = builder.pair(cid, "signin", access_expires=NOW - 10 * DAY + 30 * DAY)  # issued 10 days ago
    _, from_refresh = builder.pair(
        cid, "framework", access_expires=NOW - DAY + 3600, prefixes=("test_access_token_", "test_refresh_token_")
    )
    _, odd_prefix = builder.pair(cid, "odd", access_expires=NOW + DAY, prefixes=("zzz_", "yyy_"))
    _, no_expiry = builder.pair(cid, "forever", access_expires=None)
    _, in_future = builder.pair(cid, "future", access_expires=NOW + 90 * DAY)  # would be "issued" 60 days from now
    builder.state.refresh_tokens["prt_alone"] = RefreshToken(token="prt_alone", client_id=cid, scopes=[], expires_at=None)
    put(state_dir, builder.v1_document())

    meta = oauth_store.load_state(state_dir, now=NOW, prune_expired=False).state.refresh_meta

    assert meta[from_signin]["issued_at"] == NOW - 10 * DAY
    assert meta[from_refresh]["issued_at"] == NOW - DAY
    for token in (odd_prefix, no_expiry, in_future, "prt_alone"):
        assert meta[token]["issued_at"] == NOW, token  # no honest estimate: the time of the first load
    assert all(entry["origin"] == ORIGIN_LEGACY for entry in meta.values())


def test_a_version_2_file_keeps_its_families_and_tombstones_and_migrates_nothing(state_dir):
    state = a_state_with_everything()
    put(state_dir, oauth_store.dump_state(state))

    result = oauth_store.load_state(state_dir, now=NOW)

    assert result.format == "v2" and result.migrated == 0
    assert result.state.refresh_meta == state.refresh_meta
    assert result.state.retired == state.retired
    assert oauth_store.dump_state(result.state) == oauth_store.dump_state(state)


def test_a_refresh_token_without_metadata_in_a_version_2_file_gets_a_legacy_family(state_dir):
    state = a_state_with_everything()
    document = oauth_store.dump_state(state)
    victim = next(iter(document["refresh_meta"]))
    del document["refresh_meta"][victim]
    put(state_dir, document)

    result = oauth_store.load_state(state_dir, now=NOW)

    assert result.migrated == 1 and result.state.refresh_meta[victim]["origin"] == ORIGIN_LEGACY
    assert sum(result.skipped.values()) == 0  # a missing entry is not an invalid one


def test_unknown_top_level_keys_are_ignored(state_dir):
    document = Builder().v1_document()
    document["something_new"] = {"a": 1}
    put(state_dir, document)
    assert oauth_store.load_state(state_dir, now=NOW).format == "v1"


def test_the_file_is_only_read_never_written_by_a_load(state_dir):
    put(state_dir, oauth_store.dump_state(a_state_with_everything()))
    before = (state_dir / STATE_FILE).read_bytes()
    oauth_store.load_state(state_dir, now=NOW)
    oauth_store.load_state(state_dir, now=NOW, prune_expired=False)
    assert (state_dir / STATE_FILE).read_bytes() == before and listing(state_dir) == [STATE_FILE]


# --------------------------------------------------------------------------
# A file that cannot be trusted is refused and left alone
# --------------------------------------------------------------------------

UNTRUSTWORTHY = [
    pytest.param(b"", id="empty-file"),
    pytest.param(b"{", id="truncated"),
    pytest.param(b'{"clients": {"a": ', id="truncated-inside"),
    pytest.param(b"not json at all", id="text"),
    pytest.param(b"\xff\xfe\x00garbage\x80", id="binary"),
    pytest.param(b"[]", id="list"),
    pytest.param(b"null", id="null"),
    pytest.param(b'"a string"', id="string"),
    pytest.param(b"12", id="number"),
    pytest.param(b"true", id="boolean"),
    pytest.param(b'{"clients": []}', id="clients-list"),
    pytest.param(b'{"access_tokens": "x"}', id="access-tokens-string"),
    pytest.param(b'{"refresh_tokens": 1}', id="refresh-tokens-number"),
    pytest.param(b'{"a2r": null}', id="a2r-null"),
    pytest.param(b'{"r2a": []}', id="r2a-list"),
    pytest.param(b'{"refresh_meta": []}', id="meta-list"),
    pytest.param(b'{"retired": "no"}', id="retired-string"),
    pytest.param(b'{"version": 3}', id="future-version"),
    pytest.param(b'{"version": 0}', id="version-zero"),
    pytest.param(b'{"version": "2"}', id="version-string"),
    pytest.param(b'{"version": true}', id="version-bool"),
    pytest.param(b'{"version": null}', id="version-null"),
    pytest.param(b'{"version": 2.0}', id="version-float"),
    pytest.param(b"[" * 100_000, id="deeply-nested"),
]


@pytest.mark.parametrize("content", UNTRUSTWORTHY)
def test_an_unreadable_or_misshapen_file_raises_and_is_left_byte_for_byte_alone(state_dir, content):
    (state_dir / STATE_FILE).write_bytes(content)
    mtime = (state_dir / STATE_FILE).stat().st_mtime_ns
    with pytest.raises(StateFileError) as info:
        oauth_store.load_state(state_dir, now=NOW)
    assert "was not changed" in str(info.value)
    assert (state_dir / STATE_FILE).read_bytes() == content and (state_dir / STATE_FILE).stat().st_mtime_ns == mtime
    assert listing(state_dir) == [STATE_FILE]  # no backup, no temp file, nothing


def test_a_state_path_that_is_a_directory_raises(state_dir):
    (state_dir / STATE_FILE).mkdir()
    with pytest.raises(StateFileError, match="cannot read"):
        oauth_store.load_state(state_dir, now=NOW)


def test_an_error_never_carries_a_value_from_the_file(state_dir):
    marker = "SECRETMARKER-pat_0123456789abcdef"
    for content in (
        ('{"access_tokens": {"%s": {"token": "%s"' % (marker, marker)).encode(),  # truncated JSON that holds a secret
        ('{"clients": "%s"}' % marker).encode(),  # a section of the wrong type
        ('{"version": "%s"}' % marker).encode(),  # a version of the wrong type
        ('["%s"]' % marker).encode(),  # a wrong top level
    ):
        (state_dir / STATE_FILE).write_bytes(content)
        with pytest.raises(StateFileError) as info:
            oauth_store.load_state(state_dir, now=NOW)
        assert marker not in str(info.value) and marker not in repr(info.value)
        # a JSONDecodeError holds the whole document, so it must not ride along as the cause or the context
        assert info.value.__cause__ is None
        assert info.value.__context__ is None or info.value.__suppress_context__


def test_a_version_the_release_does_not_know_is_named_by_number(state_dir):
    (state_dir / STATE_FILE).write_bytes(b'{"version": 7}')
    with pytest.raises(StateFileError, match=r"format version 7"):
        oauth_store.load_state(state_dir, now=NOW)


# --------------------------------------------------------------------------
# One bad record is skipped, with one warning that carries counts only
# --------------------------------------------------------------------------


def a_document_with_bad_records() -> dict:
    builder = Builder()
    good_client = builder.client(1)
    builder.pair(good_client, "good", access_expires=NOW + DAY)
    document = oauth_store.dump_state(builder.state)
    document["version"] = 2
    sec = "SECRETMARKER"
    document["clients"]["BADCLIENTID-1"] = {"client_id": "BADCLIENTID-1", "client_secret": sec}  # no redirect_uris
    document["clients"]["BADCLIENTID-2"] = dict(document["clients"][good_client], client_id="BADCLIENTID-other")
    document["access_tokens"]["pat_" + sec + "-1"] = {"token": "pat_" + sec + "-1", "client_id": good_client, "scopes": [], "expires_at": "soon"}
    document["access_tokens"]["pat_" + sec + "-2"] = {"token": "pat_" + sec + "-different", "client_id": good_client, "scopes": [], "expires_at": 1}
    document["access_tokens"]["pat_" + sec + "-3"] = {"token": "pat_" + sec + "-3", "client_id": "", "scopes": [], "expires_at": 1}
    document["refresh_tokens"]["prt_" + sec + "-1"] = {"token": "prt_" + sec + "-1", "client_id": good_client, "scopes": "mcp"}
    document["refresh_tokens"]["prt_" + sec + "-2"] = "not even an object"
    document["a2r"]["pat_" + sec + "-gone"] = "prt_" + sec + "-gone"  # points at nothing
    document["r2a"]["prt_" + sec + "-gone"] = 12345
    document["refresh_meta"]["prt_" + sec + "-orphan"] = {"family": "abc", "issued_at": 1, "origin": "code"}
    good_refresh = next(iter(document["refresh_tokens"]))
    document["refresh_meta"][good_refresh] = {"family": "abc", "issued_at": 1, "origin": "somewhere"}  # a bad origin
    document["retired"]["not-a-digest"] = {"family": "abc", "retired_at": 1}
    document["retired"][oauth_store.token_digest("prt_x")] = "no"
    document["retired"][oauth_store.token_digest("prt_y")] = {"family": "bad family!", "retired_at": 1}
    document["retired"][oauth_store.token_digest("prt_z")] = {"family": "abc", "retired_at": -5}
    return document


def test_bad_records_are_skipped_the_good_ones_load_and_one_warning_counts_them(state_dir, caplog):
    caplog.set_level(logging.INFO, logger="oauth-store")
    put(state_dir, a_document_with_bad_records())

    result = oauth_store.load_state(state_dir, now=NOW)

    assert (result.clients, result.access_tokens, result.refresh_tokens) == (1, 1, 1)
    assert result.skipped == {
        "clients": 2, "access_tokens": 3, "refresh_tokens": 2, "maps": 2, "refresh_meta": 2, "retired": 4,
    }
    warnings = [r for r in caplog.records if r.name == "oauth-store" and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "clients=2" in text and "access_tokens=3" in text and "refresh_tokens=2" in text and "retired=4" in text
    for forbidden in ("SECRETMARKER", "BADCLIENTID", "client-secret", "prt_", "pat_"):
        assert forbidden not in caplog.text, forbidden
    # the good refresh token lost its bad metadata and was given a legacy family
    assert result.migrated == 1


def test_a_clean_file_logs_no_warning(state_dir, caplog):
    caplog.set_level(logging.DEBUG, logger="oauth-store")
    put(state_dir, oauth_store.dump_state(a_state_with_everything()))
    oauth_store.load_state(state_dir, now=NOW)
    assert [r for r in caplog.records if r.name == "oauth-store"] == []


def test_a_bad_record_does_not_stop_the_load_of_the_records_after_it(state_dir):
    builder = Builder()
    for n in range(1, 6):
        builder.client(n)
    document = builder.v1_document()
    first = next(iter(document["clients"]))
    document["clients"][first] = {"client_id": first}  # invalid: redirect_uris missing
    put(state_dir, document)
    result = oauth_store.load_state(state_dir, now=NOW)
    assert result.clients == 4 and first not in result.state.clients and result.skipped["clients"] == 1


# --------------------------------------------------------------------------
# Pruning
# --------------------------------------------------------------------------


def test_an_access_token_is_kept_for_a_week_after_it_expired_then_pruned_and_its_refresh_token_stays():
    builder = Builder()
    cid = builder.client(1)
    recent, recent_refresh = builder.pair(cid, "recent", access_expires=NOW - 7 * DAY + 1)
    boundary, boundary_refresh = builder.pair(cid, "boundary", access_expires=NOW - 7 * DAY)
    old, old_refresh = builder.pair(cid, "old", access_expires=NOW - 7 * DAY - 1)
    forever, forever_refresh = builder.pair(cid, "forever", access_expires=None)
    state = builder.state

    counts = oauth_store.prune(state, NOW)

    assert (counts.access_tokens, counts.refresh_tokens, counts.tombstones, counts.total) == (1, 0, 0, 1)
    assert set(state.access_tokens) == {recent, boundary, forever}
    assert set(state.refresh_tokens) == {recent_refresh, boundary_refresh, old_refresh, forever_refresh}
    assert old not in state.access_to_refresh and old_refresh not in state.refresh_to_access  # no dangling entry
    assert state.access_to_refresh[recent] == recent_refresh and state.refresh_to_access[recent_refresh] == recent


def test_an_expired_refresh_token_takes_its_pair_with_it_even_if_the_access_token_is_still_valid():
    builder = Builder()
    cid = builder.client(1)
    access, refresh = builder.pair(cid, "x", access_expires=NOW + 20 * DAY, refresh_expires=NOW - 8 * DAY)
    kept_access, kept_refresh = builder.pair(cid, "y", access_expires=NOW + DAY, refresh_expires=NOW - 6 * DAY)
    state = builder.state
    state.refresh_meta[refresh] = {"family": "f1", "issued_at": 1, "origin": ORIGIN_CODE}
    state.refresh_meta[kept_refresh] = {"family": "f2", "issued_at": 1, "origin": ORIGIN_CODE}

    counts = oauth_store.prune(state, NOW)

    assert (counts.access_tokens, counts.refresh_tokens) == (1, 1)
    assert access not in state.access_tokens and refresh not in state.refresh_tokens
    assert refresh not in state.refresh_meta and refresh not in state.refresh_to_access and access not in state.access_to_refresh
    assert kept_access in state.access_tokens and kept_refresh in state.refresh_tokens and kept_refresh in state.refresh_meta


def test_tombstones_older_than_90_days_go_and_younger_ones_stay():
    state = OAuthState()
    keep = oauth_store.token_digest("keep")
    edge = oauth_store.token_digest("edge")
    gone = oauth_store.token_digest("gone")
    state.retired[keep] = {"family": "f", "retired_at": NOW - 89 * DAY}
    state.retired[edge] = {"family": "f", "retired_at": NOW - 90 * DAY}
    state.retired[gone] = {"family": "f", "retired_at": NOW - 90 * DAY - 1}

    counts = oauth_store.prune(state, NOW)

    assert counts.tombstones == 1 and counts.total == 1
    assert set(state.retired) == {keep, edge}


def test_the_tombstone_cap_drops_the_oldest_first():
    state = OAuthState()
    digests = [oauth_store.token_digest(str(n)) for n in range(6)]
    for age, digest in enumerate(digests):  # digests[0] is the newest
        state.retired[digest] = {"family": "f", "retired_at": NOW - age * DAY}

    counts = oauth_store.prune(state, NOW, max_tombstones=4)

    assert counts.tombstones == 2 and set(state.retired) == set(digests[:4])
    assert oauth_store.MAX_TOMBSTONES == 10_000


def test_prune_never_removes_a_client_even_one_with_no_token_at_all():
    builder = Builder()
    for n in (1, 2, 3):
        builder.client(n)
    builder.pair(client_id_of(1), "old", access_expires=NOW - 400 * DAY, refresh_expires=NOW - 300 * DAY)
    state = builder.state

    oauth_store.prune(state, NOW)

    assert state.access_tokens == {} and state.refresh_tokens == {}
    assert set(state.clients) == {client_id_of(1), client_id_of(2), client_id_of(3)}


def test_prune_sweeps_map_and_metadata_entries_that_point_at_nothing():
    builder = Builder()
    cid = builder.client(1)
    access, refresh = builder.pair(cid, "live", access_expires=NOW + DAY)
    state = builder.state
    state.access_to_refresh["pat_phantom"] = refresh
    state.access_to_refresh[access] = "prt_phantom"
    state.refresh_to_access["prt_phantom"] = access
    state.refresh_meta["prt_phantom"] = {"family": "f", "issued_at": 1, "origin": ORIGIN_CODE}

    counts = oauth_store.prune(state, NOW)

    assert counts.total == 0  # quiet clean-up, not counted as pruned tokens
    assert state.access_to_refresh == {}  # both of its entries had a missing end
    assert state.refresh_to_access == {refresh: access}  # the real pair is untouched
    assert "prt_phantom" not in state.refresh_meta


def test_prune_with_a_shorter_retention_removes_what_expired_after_it():
    builder = Builder()
    cid = builder.client(1)
    access, _ = builder.pair(cid, "recent", access_expires=NOW - 3600)
    state = builder.state
    assert oauth_store.prune(copy.deepcopy(state), NOW).total == 0
    assert oauth_store.prune(state, NOW, retention=0).access_tokens == 1
    assert access not in state.access_tokens


def test_load_reports_what_the_file_held_and_what_the_prune_after_it_dropped(state_dir):
    builder = Builder()
    cid = builder.client(1)
    builder.pair(cid, "live", access_expires=NOW + DAY)
    builder.pair(cid, "dead", access_expires=NOW - 30 * DAY)
    put(state_dir, builder.v1_document())

    pruned = oauth_store.load_state(state_dir, now=NOW)
    kept = oauth_store.load_state(state_dir, now=NOW, prune_expired=False)

    assert (pruned.access_tokens, pruned.refresh_tokens, pruned.pruned.total) == (2, 2, 1)
    assert len(pruned.state.access_tokens) == 1
    assert (kept.access_tokens, kept.pruned.total) == (2, 0) and len(kept.state.access_tokens) == 2


# --------------------------------------------------------------------------
# Pairs, families, tombstones, clients
# --------------------------------------------------------------------------


def test_pairs_are_found_through_either_map():
    builder = Builder()
    cid = builder.client(1)
    access, refresh = builder.pair(cid, "p", access_expires=NOW + DAY)
    state = builder.state
    assert oauth_store.paired_access_tokens(state, refresh) == [access]
    assert oauth_store.paired_refresh_tokens(state, access) == [refresh]
    del state.refresh_to_access[refresh]  # only a2r left
    assert oauth_store.paired_access_tokens(state, refresh) == [access]
    state.refresh_to_access[refresh] = access
    del state.access_to_refresh[access]  # only r2a left
    assert oauth_store.paired_refresh_tokens(state, access) == [refresh]
    assert oauth_store.paired_access_tokens(state, "prt_unknown") == []


def test_removing_a_refresh_token_removes_its_pair_metadata_and_maps_unless_asked_not_to():
    builder = Builder()
    cid = builder.client(1)
    access, refresh = builder.pair(cid, "p", access_expires=NOW + DAY)
    other_access, other_refresh = builder.pair(cid, "q", access_expires=NOW + DAY)
    state = builder.state
    state.refresh_meta[refresh] = {"family": "f", "issued_at": 1, "origin": ORIGIN_CODE}
    snapshot = copy.deepcopy(state)

    assert oauth_store.remove_refresh_token(state, refresh, with_pair=False) == (0, 1)
    assert access in state.access_tokens and refresh not in state.refresh_tokens and refresh not in state.refresh_meta
    assert access not in state.access_to_refresh and refresh not in state.refresh_to_access

    state = snapshot
    assert oauth_store.remove_refresh_token(state, refresh) == (1, 1)
    assert access not in state.access_tokens and set(state.access_tokens) == {other_access}
    assert state.access_to_refresh == {other_access: other_refresh} and state.refresh_to_access == {other_refresh: other_access}
    assert oauth_store.remove_refresh_token(state, refresh) == (0, 0)  # already gone: nothing happens


def test_retiring_a_refresh_token_keeps_a_sha256_tombstone_and_never_the_token():
    builder = Builder()
    cid = builder.client(1)
    access, refresh = builder.pair(cid, "p", access_expires=NOW + DAY)
    state = builder.state
    state.refresh_meta[refresh] = {"family": "fam1", "issued_at": 5, "origin": ORIGIN_CODE}

    family = oauth_store.retire_refresh_token(state, refresh, NOW + 0.7)

    assert family == "fam1"
    assert state.retired == {oauth_store.token_digest(refresh): {"family": "fam1", "retired_at": NOW}}
    assert refresh not in state.refresh_tokens and access not in state.access_tokens and refresh not in state.refresh_meta
    assert refresh not in json.dumps(oauth_store.dump_state(state)) and access not in json.dumps(oauth_store.dump_state(state))


def test_retiring_a_legacy_token_gives_it_a_family_first():
    builder = Builder()
    access, refresh = builder.pair(builder.client(1), "legacy", access_expires=NOW + 30 * DAY)
    state = builder.state
    family = oauth_store.retire_refresh_token(state, refresh, NOW)
    assert re.fullmatch(r"[0-9a-f]{16}", family)
    assert state.retired[oauth_store.token_digest(refresh)]["family"] == family


def test_revoking_a_family_removes_its_live_tokens_and_keeps_every_tombstone():
    builder = Builder()
    cid = builder.client(1)
    access, refresh = builder.pair(cid, "mine", access_expires=NOW + DAY)
    other_access, other_refresh = builder.pair(cid, "theirs", access_expires=NOW + DAY)
    state = builder.state
    state.refresh_meta[refresh] = {"family": "mine", "issued_at": 1, "origin": ORIGIN_REFRESH}
    state.refresh_meta[other_refresh] = {"family": "theirs", "issued_at": 1, "origin": ORIGIN_REFRESH}
    state.retired["a" * 64] = {"family": "mine", "retired_at": NOW}

    assert oauth_store.revoke_family(state, "mine") == (1, 1)

    assert set(state.access_tokens) == {other_access} and set(state.refresh_tokens) == {other_refresh}
    assert "a" * 64 in state.retired  # a later presentation of a rotated token is still recognized
    assert oauth_store.revoke_family(state, "mine") == (0, 0) and oauth_store.revoke_family(state, "nobody") == (0, 0)


def test_a_client_that_still_has_a_token_is_never_removed():
    builder = Builder()
    holds_access = builder.client(1)
    holds_refresh = builder.client(2)
    holds_expired = builder.client(3)
    nothing = builder.client(4)
    also_nothing = builder.client(5)
    builder.state.access_tokens["pat_only"] = AccessToken(token="pat_only", client_id=holds_access, scopes=[], expires_at=NOW + 1)
    builder.state.refresh_tokens["prt_only"] = RefreshToken(token="prt_only", client_id=holds_refresh, scopes=[])
    builder.state.access_tokens["pat_expired"] = AccessToken(token="pat_expired", client_id=holds_expired, scopes=[], expires_at=NOW - 3 * DAY)
    state = builder.state

    assert oauth_store.clients_with_tokens(state) == {holds_access, holds_refresh, holds_expired}
    removed = oauth_store.remove_tokenless_clients(
        state, [holds_access, holds_refresh, holds_expired, nothing, "no-such-client", nothing]
    )

    assert removed == [nothing]
    assert set(state.clients) == {holds_access, holds_refresh, holds_expired, also_nothing}


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def write(state_dir: Path, state: OAuthState) -> bytes:
    oauth_store.write_state(state_dir, state)
    return (state_dir / STATE_FILE).read_bytes()


def test_a_state_survives_a_write_and_a_load_unchanged(state_dir):
    state = a_state_with_everything()
    write(state_dir, state)
    loaded = oauth_store.load_state(state_dir, now=NOW, prune_expired=False)
    assert loaded.format == "v2" and loaded.migrated == 0 and sum(loaded.skipped.values()) == 0
    assert oauth_store.dump_state(loaded.state) == oauth_store.dump_state(state)
    assert loaded.state.clients[client_id_of(1)].client_secret == state.clients[client_id_of(1)].client_secret
    assert listing(state_dir) == [STATE_FILE]


def test_a_write_drops_entries_that_point_at_nothing_so_the_file_always_loads_cleanly(state_dir, caplog):
    caplog.set_level(logging.DEBUG, logger="oauth-store")
    state = a_state_with_everything()
    state.access_to_refresh["pat_phantom"] = "prt_phantom"
    state.refresh_to_access["prt_phantom"] = "pat_phantom"
    state.refresh_meta["prt_phantom"] = {"family": "zzz", "issued_at": 1, "origin": ORIGIN_CODE}
    write(state_dir, state)
    document = json.loads((state_dir / STATE_FILE).read_text())
    assert "pat_phantom" not in document["a2r"] and "prt_phantom" not in document["r2a"] and "prt_phantom" not in document["refresh_meta"]
    result = oauth_store.load_state(state_dir, now=NOW, prune_expired=False)
    assert sum(result.skipped.values()) == 0 and [r for r in caplog.records if r.name == "oauth-store"] == []


def test_the_written_file_is_version_2_with_the_two_new_sections_and_the_five_old_ones(state_dir):
    write(state_dir, a_state_with_everything())
    document = json.loads((state_dir / STATE_FILE).read_text())
    assert set(document) == V2_KEYS and document["version"] == 2
    assert set(document["refresh_meta"].values().__iter__().__next__()) == {"family", "issued_at", "origin"}
    assert all(set(entry) == {"family", "retired_at"} for entry in document["retired"].values())
    assert all(re.fullmatch(r"[0-9a-f]{64}", key) for key in document["retired"])


def test_a_write_replaces_the_old_file_atomically_with_a_complete_new_one(state_dir):
    put(state_dir, Builder().v1_document())
    inode_before = (state_dir / STATE_FILE).stat().st_ino
    write(state_dir, a_state_with_everything())
    assert (state_dir / STATE_FILE).stat().st_ino != inode_before  # a new file was renamed over it
    assert json.loads((state_dir / STATE_FILE).read_text())["version"] == 2


@pytest.mark.parametrize("umask", [0o000, 0o022, 0o077, 0o277])
def test_the_file_is_mode_0600_whatever_the_umask(state_dir, umask):
    old = os.umask(umask)
    try:
        write(state_dir, a_state_with_everything())
        lock = StateLock(state_dir).acquire()
        lock.release()
    finally:
        os.umask(old)
    assert stat.S_IMODE((state_dir / STATE_FILE).stat().st_mode) == 0o600
    if umask in (0o000, 0o022, 0o077):
        assert stat.S_IMODE((state_dir / oauth_store.LOCK_FILE_NAME).stat().st_mode) == 0o600


@pytest.mark.skipif(os.geteuid() != 0, reason="only root can give a file to another user")
def test_files_that_root_creates_in_another_users_directory_are_given_to_that_user(state_dir):
    # The service runs as the user that owns the state directory. A lock file or a state file that a script started as root by
    # mistake leaves behind, owned by root with mode 0600, would stop the service from starting.
    try:
        os.chown(state_dir, 54321, 54322)
    except OSError:
        pytest.skip("this environment does not allow chown")
    oauth_store.write_state(state_dir, a_state_with_everything())
    with StateLock(state_dir):
        pass
    for name in (STATE_FILE, oauth_store.LOCK_FILE_NAME):
        info = (state_dir / name).stat()
        assert (info.st_uid, info.st_gid) == (54321, 54322), name
        assert stat.S_IMODE(info.st_mode) == 0o600, name
    assert listing(state_dir) == sorted([STATE_FILE, oauth_store.LOCK_FILE_NAME])


@pytest.mark.skipif(os.geteuid() != 0, reason="only root can see the difference")
def test_files_in_a_directory_that_root_owns_stay_with_root(state_dir):
    oauth_store.write_state(state_dir, a_state_with_everything())
    assert (state_dir / STATE_FILE).stat().st_uid == 0


def failing_at(monkeypatch, name: str, error: int = errno.EIO):
    """Make os.<name> fail for the state file's temp file or target only (everything else is left alone)."""
    real = getattr(os, name)

    def fail(*args, **kwargs):
        if name == "replace" and not str(args[1]).endswith(STATE_FILE):
            return real(*args, **kwargs)
        if name == "open" and not (Path(str(args[0])).name.startswith(".") and str(args[0]).endswith(".tmp")):
            return real(*args, **kwargs)
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(os, name, fail)


@pytest.mark.parametrize(
    "stage, error",
    [
        ("open", errno.EACCES),  # the temp file cannot be created
        ("fchmod", errno.EPERM),
        ("write", errno.ENOSPC),  # the disk is full
        ("fsync", errno.EIO),
        ("replace", errno.EIO),
    ],
)
def test_a_failed_write_leaves_the_old_file_byte_for_byte_and_no_temp_file(state_dir, monkeypatch, stage, error):
    before = put(state_dir, oauth_store.dump_state(a_state_with_everything()))
    mtime = (state_dir / STATE_FILE).stat().st_mtime_ns
    listing_before = listing(state_dir)
    changed = a_state_with_everything()
    changed.retired["f" * 64] = {"family": "zzzz", "retired_at": NOW}
    failing_at(monkeypatch, stage, error)

    with pytest.raises(StateFileError, match="cannot write the OAuth state file"):
        oauth_store.write_state(state_dir, changed)

    monkeypatch.undo()
    assert (state_dir / STATE_FILE).read_bytes() == before
    assert (state_dir / STATE_FILE).stat().st_mtime_ns == mtime
    assert listing(state_dir) == listing_before


def test_a_failed_first_write_leaves_no_file_at_all(state_dir, monkeypatch):
    failing_at(monkeypatch, "fsync")
    with pytest.raises(StateFileError):
        oauth_store.write_state(state_dir, a_state_with_everything())
    monkeypatch.undo()
    assert listing(state_dir) == []


def test_an_interrupt_in_the_middle_of_a_write_still_removes_the_temp_file(state_dir, monkeypatch):
    before = put(state_dir, Builder().v1_document())

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", interrupt)
    with pytest.raises(KeyboardInterrupt):
        oauth_store.write_state(state_dir, a_state_with_everything())
    monkeypatch.undo()
    assert (state_dir / STATE_FILE).read_bytes() == before and listing(state_dir) == [STATE_FILE]


def test_a_failure_to_fsync_the_directory_after_the_rename_is_a_warning_and_the_save_counts(state_dir, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="oauth-store")

    def refuse(directory):
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(oauth_store, "_fsync_directory", refuse)
    oauth_store.write_state(state_dir, a_state_with_everything())  # does not raise: the new file is already in place
    assert json.loads((state_dir / STATE_FILE).read_text())["version"] == 2
    assert any("could not fsync the state directory" in r.getMessage() for r in caplog.records)
    assert listing(state_dir) == [STATE_FILE]


def test_the_directory_is_fsynced_after_the_rename(state_dir, monkeypatch):
    order = []
    real_replace, real_fsync_dir = os.replace, oauth_store._fsync_directory

    def replace(src, dst):
        order.append("replace")
        return real_replace(src, dst)

    def fsync_dir(directory):
        order.append("fsync-dir")
        return real_fsync_dir(directory)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(oauth_store, "_fsync_directory", fsync_dir)
    oauth_store.write_state(state_dir, a_state_with_everything())
    assert order == ["replace", "fsync-dir"]


def test_the_temp_file_is_created_exclusively_with_mode_0600_and_is_fsynced_before_the_rename(state_dir, monkeypatch):
    seen = {}
    real_open, real_fsync, real_replace = os.open, os.fsync, os.replace

    def spy_open(path, flags, mode=0o777, **kwargs):
        if str(path).endswith(".tmp"):
            seen["flags"], seen["mode"], seen["name"] = flags, mode, Path(str(path)).name
        return real_open(path, flags, mode, **kwargs)

    def spy_fsync(fd):
        seen.setdefault("fsynced_before_replace", "replace" not in seen)
        return real_fsync(fd)

    def spy_replace(src, dst):
        seen["replace"] = (Path(str(src)).name, Path(str(dst)).name)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    oauth_store.write_state(state_dir, a_state_with_everything())
    assert seen["flags"] & os.O_EXCL and seen["flags"] & os.O_CREAT and seen["flags"] & os.O_WRONLY and seen["mode"] == 0o600
    assert re.fullmatch(rf"\.{re.escape(STATE_FILE)}\.\d+\.[0-9a-f]{{8}}\.tmp", seen["name"])
    assert seen["fsynced_before_replace"] is True
    assert seen["replace"] == (seen["name"], STATE_FILE)


def test_state_that_cannot_be_written_as_json_is_refused_before_the_disk_is_touched(state_dir):
    before = put(state_dir, Builder().v1_document())
    state = a_state_with_everything()
    next(iter(state.refresh_meta.values()))["issued_at"] = float("nan")  # json.dumps(allow_nan=False) refuses it
    with pytest.raises(StateFileError, match="cannot be written as JSON"):
        oauth_store.write_state(state_dir, state)
    assert (state_dir / STATE_FILE).read_bytes() == before and listing(state_dir) == [STATE_FILE]


def test_writing_into_a_directory_that_does_not_exist_fails_and_creates_nothing(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(StateFileError, match="cannot write"):
        oauth_store.write_state(missing, a_state_with_everything())
    assert not missing.exists() and list(tmp_path.iterdir()) == []


def test_a_rotated_token_is_in_the_file_only_as_its_digest(state_dir):
    builder = Builder()
    access, refresh = builder.pair(builder.client(1), "rot", access_expires=NOW + DAY)
    state = builder.state
    oauth_store.retire_refresh_token(state, refresh, NOW)
    text = write(state_dir, state).decode()
    assert refresh not in text and access not in text
    assert oauth_store.token_digest(refresh) in text


def test_leftover_temp_files_are_removed_and_nothing_else(state_dir):
    keep = put(state_dir, Builder().v1_document())
    (state_dir / f".{STATE_FILE}.4242.deadbeef.tmp").write_text("a full copy of the credentials")
    (state_dir / f".{STATE_FILE}.1.00000000.tmp").write_text("another")
    (state_dir / "notes.tmp").write_text("not ours")
    (state_dir / STATE_FILE.replace(".json", ".json.bak")).write_text("not ours either")

    assert oauth_store.remove_stale_temp_files(state_dir) == 2

    assert listing(state_dir) == sorted([STATE_FILE, "notes.tmp", STATE_FILE.replace(".json", ".json.bak")])
    assert (state_dir / STATE_FILE).read_bytes() == keep
    assert oauth_store.remove_stale_temp_files(state_dir) == 0
    assert oauth_store.remove_stale_temp_files(state_dir / "missing") == 0


# --------------------------------------------------------------------------
# The lock
# --------------------------------------------------------------------------


def test_the_lock_is_exclusive_and_can_be_taken_again_after_release(state_dir):
    first = StateLock(state_dir).acquire()
    assert first.held and (state_dir / oauth_store.LOCK_FILE_NAME).exists()
    second = StateLock(state_dir)
    with pytest.raises(StateLockedError, match="another process holds the OAuth state lock"):
        second.acquire()
    assert not second.held
    assert first.acquire() is first  # taking a lock one already holds is a no-op
    first.release()
    first.release()  # releasing twice is fine
    assert not first.held
    second.acquire()
    assert second.held
    second.release()


def test_the_lock_waits_up_to_its_timeout_for_the_holder(state_dir):
    holder = StateLock(state_dir).acquire()
    started = time.monotonic()
    with pytest.raises(StateLockedError):
        StateLock(state_dir).acquire(timeout=0.2)
    assert 0.15 <= time.monotonic() - started < 3
    holder.release()
    StateLock(state_dir).acquire(timeout=0.2).release()


def test_the_lock_is_a_context_manager(state_dir):
    with StateLock(state_dir) as lock:
        assert lock.held
        with pytest.raises(StateLockedError):
            StateLock(state_dir).acquire()
    assert not lock.held
    StateLock(state_dir).acquire().release()


def test_the_lock_excludes_another_process_and_is_dropped_when_released(state_dir):
    code = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    print('free')\n"
        "except OSError:\n"
        "    print('locked')\n"
    )
    lock_path = str(state_dir / oauth_store.LOCK_FILE_NAME)

    def probe() -> str:
        return subprocess.run([sys.executable, "-c", code, lock_path], capture_output=True, text=True, timeout=60).stdout.strip()

    with StateLock(state_dir):
        assert probe() == "locked"
    assert probe() == "free"


def test_a_lock_that_is_garbage_collected_lets_go(state_dir):
    lock = StateLock(state_dir).acquire()
    fd = lock._fd
    assert fd is not None and os.get_inheritable(fd) is False  # never passed on to a child process
    del lock
    gc.collect()
    StateLock(state_dir).acquire().release()


def test_the_lock_never_creates_the_directory_unless_told_to(tmp_path):
    missing = tmp_path / "no-state-dir"
    with pytest.raises(StateFileError, match="does not exist"):
        StateLock(missing).acquire()
    assert not missing.exists() and list(tmp_path.iterdir()) == []


def test_the_lock_creates_a_missing_directory_with_mode_0700_and_the_lock_file_with_0600(tmp_path):
    target = tmp_path / "a" / "b" / "state"
    old = os.umask(0)
    try:
        with StateLock(target, create_dir=True):
            pass
    finally:
        os.umask(old)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE((target / oauth_store.LOCK_FILE_NAME).stat().st_mode) == 0o600


def test_an_existing_directory_keeps_its_mode(tmp_path):
    target = tmp_path / "state"
    target.mkdir(mode=0o750)
    os.chmod(target, 0o750)
    with StateLock(target, create_dir=True):
        pass
    assert stat.S_IMODE(target.stat().st_mode) == 0o750


def test_a_lock_file_that_cannot_be_opened_is_a_state_file_error(state_dir, monkeypatch):
    real = os.open

    def deny(path, *args, **kwargs):
        if str(path).endswith(oauth_store.LOCK_FILE_NAME):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny)
    with pytest.raises(StateFileError, match="cannot open the OAuth state lock file") as info:
        StateLock(state_dir).acquire()
    assert not isinstance(info.value, StateLockedError)
