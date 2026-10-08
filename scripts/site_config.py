"""Site-specific values for the live harness and the changelog watcher, read from one local TOML file.

Nothing about a real tenant (client ids, names, contact, email address, host names) is kept in tracked code. The file is
`site.local.toml` at the repo root (git-ignored); `site.example.toml` documents every key with neutral placeholders.
`GORELO_SITE_CONFIG=<path>` points at another file. A missing file, a missing key or a value of the wrong type raises
SiteConfigError: there is no default, so nothing can silently point at a real tenant. An unedited copy of the example
is refused too: a path whose basename is site.example.toml, and any value equal to the placeholder of the same key
(an empty list is not a placeholder).

Each consumer needs only its own keys: the live harness needs HARNESS_KEYS (`site()`, or the lazy `SITE` proxy,
`SITE.test_client`, which reads the file on first attribute access instead of at import time); the changelog watcher
needs WATCHER_KEYS only (`watcher_alert_client()`), so a file holding just [watcher] is enough for a watcher-only install.
The harness also checks the client NAMES against Gorelo before it writes (scripts/live/guard.py).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENV_VAR = "GORELO_SITE_CONFIG"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = REPO_ROOT / "site.local.toml"
EXAMPLE_NAME = "site.example.toml"
DEFAULT_UPDATED_STATUS = "In Progress"

# (section, key) -> kind. "int", "str", "int_list"
HARNESS_KEYS: dict[tuple[str, str], str] = {
    ("clients", "test_client"): "int",
    ("clients", "test_client_name"): "str",
    ("clients", "second_client"): "int",
    ("clients", "second_client_name"): "str",
    ("operator", "contact_id"): "int",
    ("operator", "user_id"): "int",
    ("operator", "email"): "str",
    ("leftovers", "client_ids"): "int_list",
    ("leftovers", "contact_ids"): "int_list",
    ("hosts", "public_host"): "str",
    ("hosts", "probe_domain"): "str",
}
# Optional name rules for the listed leftovers. When a key is absent, that rule does not apply (only the listed ids count).
OPTIONAL_KEYS: dict[tuple[str, str], str] = {
    ("leftovers", "client_name_contains"): "str_list",
    ("leftovers", "contact_first_name"): "str",
    ("leftovers", "contact_last_name_prefix"): "str",
    ("harness", "updated_status"): "str",
}
WATCHER_KEYS: dict[tuple[str, str], str] = {("watcher", "alert_client_id"): "int"}
KEYS: dict[tuple[str, str], str] = {**HARNESS_KEYS, **WATCHER_KEYS}


class SiteConfigError(Exception):
    """The site config is missing, unreadable or incomplete. The message names the file, the key and the example."""


@dataclass(frozen=True)
class SiteConfig:
    path: Path
    test_client: int
    test_client_name: str
    second_client: int
    second_client_name: str
    operator_contact: int
    operator_user: int
    operator_email: str
    leftover_clients: frozenset[int]
    leftover_contacts: frozenset[int]
    public_host: str
    probe_domain: str
    # Optional name rules for the listed leftovers; empty / None means "no such rule".
    leftover_client_name_contains: tuple[str, ...] = ()
    leftover_contact_first_name: str | None = None
    leftover_contact_last_name_prefix: str | None = None
    # The ticket status the email area moves its ticket to; the tenant must have a status of exactly this name.
    updated_status: str = DEFAULT_UPDATED_STATUS

    @property
    def probe_url(self) -> str:
        return f"https://{self.public_host}/.well-known/oauth-authorization-server"


def config_path() -> Path:
    override = os.environ.get(ENV_VAR)
    return Path(override).expanduser() if override else DEFAULT_PATH


def _fail(path: Path, what: str) -> SiteConfigError:
    return SiteConfigError(
        f"site config {path}: {what}. Copy {REPO_ROOT / EXAMPLE_NAME} to {DEFAULT_PATH.name} and fill in your own "
        f"values (or point {ENV_VAR} at your file)."
    )


def _check(path: Path, data: dict[str, Any], section: str, key: str, kind: str) -> Any:
    table = data.get(section)
    if not isinstance(table, dict) or key not in table:
        raise _fail(path, f"missing key [{section}] {key}")
    value = table[key]
    ok: bool
    if kind == "int":
        ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
    elif kind == "str":
        ok = isinstance(value, str) and bool(value.strip())
    elif kind == "str_list":
        ok = isinstance(value, list) and all(isinstance(v, str) and bool(v.strip()) for v in value)
    else:
        ok = isinstance(value, list) and all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in value)
    if not ok:
        want = {"int": "a positive integer", "str": "a non-empty string", "int_list": "a list of positive integers", "str_list": "a list of non-empty strings"}[kind]
        raise _fail(path, f"[{section}] {key} must be {want}")
    return value


def _placeholders() -> dict[tuple[str, str], Any]:
    """The values of site.example.toml, key by key ({} when the example cannot be read)."""
    try:
        data = tomllib.loads((REPO_ROOT / EXAMPLE_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    return {(s, k): v for s, table in data.items() if isinstance(table, dict) for k, v in table.items()}


def _read(path: Path | str | None, keys: dict[tuple[str, str], str]) -> tuple[Path, dict[tuple[str, str], Any]]:
    target = Path(path) if path is not None else config_path()
    if target.name == EXAMPLE_NAME:
        raise _fail(target, f"this is the example file, not a site config; copy it to {DEFAULT_PATH.name} and edit it")
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        raise _fail(target, "file not found") from None
    except OSError as exc:
        raise _fail(target, f"cannot read it ({exc.__class__.__name__})") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise _fail(target, f"not valid TOML ({exc})") from None
    placeholders = _placeholders()
    values: dict[tuple[str, str], Any] = {}
    for (section, key), kind in keys.items():
        value = _check(target, data, section, key, kind)
        if value == placeholders.get((section, key)) and value != []:
            raise _fail(target, f"[{section}] {key} still holds the placeholder of {EXAMPLE_NAME}")
        values[section, key] = value
    for (section, key), kind in OPTIONAL_KEYS.items():
        table = data.get(section)
        if keys is HARNESS_KEYS and isinstance(table, dict) and key in table:
            value = _check(target, data, section, key, kind)
            if value == placeholders.get((section, key)) and value != []:
                raise _fail(target, f"[{section}] {key} still holds the placeholder of {EXAMPLE_NAME}")
            values[section, key] = value
    return target, values


def load(path: Path | str | None = None) -> SiteConfig:
    """Read and validate the harness keys. Raises SiteConfigError; never falls back to a default."""
    target, v = _read(path, HARNESS_KEYS)
    return SiteConfig(
        path=target,
        test_client=v["clients", "test_client"],
        test_client_name=v["clients", "test_client_name"].strip(),
        second_client=v["clients", "second_client"],
        second_client_name=v["clients", "second_client_name"].strip(),
        operator_contact=v["operator", "contact_id"],
        operator_user=v["operator", "user_id"],
        operator_email=v["operator", "email"].strip().lower(),
        leftover_clients=frozenset(v["leftovers", "client_ids"]),
        leftover_contacts=frozenset(v["leftovers", "contact_ids"]),
        public_host=v["hosts", "public_host"].strip().lower(),
        probe_domain=v["hosts", "probe_domain"].strip().lower(),
        leftover_client_name_contains=tuple(v.get(("leftovers", "client_name_contains"), ())),
        leftover_contact_first_name=v.get(("leftovers", "contact_first_name")),
        leftover_contact_last_name_prefix=v.get(("leftovers", "contact_last_name_prefix")),
        updated_status=v.get(("harness", "updated_status"), DEFAULT_UPDATED_STATUS).strip(),
    )


def watcher_alert_client(path: Path | str | None = None) -> int:
    """The client the changelog watcher files its alert against: the only key the watcher needs."""
    return _read(path, WATCHER_KEYS)[1]["watcher", "alert_client_id"]


_cache: dict[tuple[Path, int, int], SiteConfig] = {}


def site() -> SiteConfig:
    """The current SiteConfig (cached per path and modification time, so a test can point at another file)."""
    path = config_path()
    try:
        stat = path.stat()
        key = (path, stat.st_mtime_ns, stat.st_size)
    except OSError:
        return load(path)  # raises the clear error
    if key not in _cache:
        _cache.clear()
        _cache[key] = load(path)
    return _cache[key]


class _LazySite:
    """`SITE.test_client`: loads the config on first use, not at import."""

    def __getattr__(self, name: str) -> Any:
        return getattr(site(), name)


SITE = _LazySite()
