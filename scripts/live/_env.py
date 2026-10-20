"""Environment plumbing for the live harness: the API key, the Settings, the request pacer.

The API key lives in the live service's .env file (ENV_FILE). load_api_key() reads ONLY the
GORELO_API_KEY line of that file when it is called and returns the value. load_base_url() reads ONLY the
GORELO_BASE_URL line of that same file, the same way, and returns the normalized Gorelo base URL (the US host
when the line is absent or blank). live_settings() carries both into the Settings. Nothing in this module prints,
logs or stores the key, no error message contains it, and nothing runs at import time (the offline tests
always pass a temporary file, never ENV_FILE).
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from settings import TOOLSETS, Settings, SettingsError, parse_base_url

ENV_FILE = Path("/opt/gorelo-mcp/app/.env")
KEY_NAME = "GORELO_API_KEY"
BASE_URL_NAME = "GORELO_BASE_URL"


class EnvError(RuntimeError):
    """The API key could not be loaded. The message never holds the key or any other value of the file."""


def _line_value(line: str, name: str) -> str | None:
    """The value on a `<name>=...` line; None for every other line (whose content is not kept)."""
    text = line.strip()
    if text.startswith("export "):
        text = text[len("export "):].lstrip()
    key, separator, rest = text.partition("=")
    if not separator or key.strip() != name:
        return None
    rest = rest.strip()
    if rest[:1] in ("'", '"'):
        quote = rest[0]
        end = rest.find(quote, 1)
        return rest[1:end] if end != -1 else rest[1:]
    return rest.split(" #", 1)[0].strip()


def _key_value(line: str) -> str | None:
    """The value on a `GORELO_API_KEY=...` line (kept under its old name)."""
    return _line_value(line, KEY_NAME)


def _last_value(target: Path, name: str) -> str | None:
    """The value of the last `<name>=` line of the env file `target`, or None when there is none. Raises EnvError, naming the file
    but never a value, when the file cannot be read."""
    found: str | None = None
    try:
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                value = _line_value(line, name)
                if value is not None:
                    found = value
    except OSError as exc:
        raise EnvError(f"cannot read the env file {target}: {type(exc).__name__}") from None
    except UnicodeDecodeError:
        raise EnvError(f"the env file {target} is not valid UTF-8") from None
    return found


def load_api_key(path: str | os.PathLike[str] | None = None) -> str:
    """The Gorelo API key: the GORELO_API_KEY line of `path` (default ENV_FILE).

    Only that line is interpreted. The last assignment wins (as in python-dotenv), single or double quotes
    around the value are removed, and an unquoted value ends at " #". Raises EnvError, naming the file but
    never a value, when the file cannot be read, has no such line or the value is blank.
    """
    target = Path(path) if path is not None else ENV_FILE
    found = _last_value(target, KEY_NAME)
    if found is None:
        raise EnvError(f"{KEY_NAME} is not set in {target}")
    if not found.strip():
        raise EnvError(f"{KEY_NAME} is blank in {target}")
    return found


def load_base_url(path: str | os.PathLike[str] | None = None) -> str:
    """The Gorelo base URL of the live service: the GORELO_BASE_URL line of `path` (default ENV_FILE), read like the key line and
    normalized by settings.parse_base_url (https://<host>/v1). No such line, or a blank one, is the US default. Raises EnvError,
    naming the file and the variable but never the key, when the file cannot be read or the value is refused."""
    target = Path(path) if path is not None else ENV_FILE
    try:
        return parse_base_url(_last_value(target, BASE_URL_NAME))
    except SettingsError as exc:
        raise EnvError(f"{exc} (in {target})") from None


def live_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Settings for the live harness: the API key and the base URL from `path` (default ENV_FILE), every toolset, destructive
    tools on. PUBLIC_BASE_URL and MCP_AUTH_PASSWORD are not needed: the harness never serves HTTP."""
    return Settings(api_key=load_api_key(path), base_url=load_base_url(path), toolsets=frozenset(TOOLSETS), destructive=True)


def scrub(text: str, secret: str | None) -> str:
    """`text` with every occurrence of `secret` replaced by ***: apply it to anything printed after talking to Gorelo."""
    if not secret:
        return text
    return text.replace(secret, "***")


_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')


def without_quotes(text: str) -> str:
    """`text` with every double-quoted string cut out ("..."), escapes inside a string included.

    update_client quotes the name and the previous value of a custom field in the error it raises for fields that changed
    without being sent (the user's own data, which it shows to the model on purpose). The live harness prints and records
    update_client error texts, which can quote that data, so it applies this to every update_client error text it prints or
    records."""
    return _QUOTED.sub('"..."', text)


class Pacer:
    """An httpx request hook that keeps at least `interval` seconds between the starts of two requests.

    Install it AFTER the guard (event_hooks={"request": [guard, pacer]}) so a blocked request never waits.
    One Pacer shared by several clients paces them together. `requests` counts the requests it let through.
    """

    def __init__(
        self,
        interval: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if interval < 0:
            raise ValueError("interval must not be negative")
        self.interval = float(interval)
        self.requests = 0
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self._lock = asyncio.Lock()

    async def __call__(self, request: httpx.Request) -> None:
        async with self._lock:
            if self._last is not None and self.interval > 0:
                wait = self._last + self.interval - self._clock()
                if wait > 0:
                    await self._sleep(wait)
            self._last = self._clock()
            self.requests += 1
