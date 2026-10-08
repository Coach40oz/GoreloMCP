"""Process settings, read once from the environment.

`Settings.from_env(os.environ)` is the only place environment variables are interpreted. It never
reads a file (main.py calls load_dotenv() first) and it reports EVERY problem in one
`SettingsError`, so an operator fixes the environment in one pass.

Variables:
    GORELO_API_KEY            required. The Gorelo API key (sent as X-API-Key).
    PUBLIC_BASE_URL           required when require_http is true (the OAuth issuer URL).
    MCP_AUTH_PASSWORD         required when require_http is true (the consent page password).
    GORELO_TOOLSETS           optional. Comma list of toolsets, case-insensitive, or "all".
                              Unset or blank means DEFAULT_TOOLSETS.
    GORELO_ENABLE_DESTRUCTIVE optional. 1/true/yes/on registers the gated tools: the delete and void tools and
                              create_approved_invoice (it creates an Approved invoice, which Gorelo pushes to the
                              connected accounting system at once and may email to its recipients). Every call
                              still needs confirm=true. 0/false/no/off/unset/blank registers none of them.
                              Anything else is an error.

The repr of Settings never shows the API key or the password.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

TOOLSETS: tuple[str, ...] = ("core", "tickets", "time", "billing", "uptime", "projects", "forms")
DEFAULT_TOOLSETS: tuple[str, ...] = ("core", "tickets", "time", "billing", "uptime")
DEFAULT_BASE_URL = "https://api.usw.gorelo.io/v1"

ENV_API_KEY = "GORELO_API_KEY"
ENV_PUBLIC_BASE_URL = "PUBLIC_BASE_URL"
ENV_AUTH_PASSWORD = "MCP_AUTH_PASSWORD"
ENV_TOOLSETS = "GORELO_TOOLSETS"
ENV_DESTRUCTIVE = "GORELO_ENABLE_DESTRUCTIVE"

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


class SettingsError(Exception):
    """The environment cannot produce valid settings. The message lists every problem."""


@dataclass(frozen=True)
class Settings:
    """Validated process settings. Build one with `Settings.from_env`."""

    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    public_base_url: str | None = None
    mcp_auth_password: str | None = field(default=None, repr=False)
    toolsets: frozenset[str] = frozenset(DEFAULT_TOOLSETS)
    destructive: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, require_http: bool = True) -> Settings:
        """Read settings from `env` (normally `os.environ`).

        With require_http true (the real server) PUBLIC_BASE_URL and MCP_AUTH_PASSWORD are
        required. The live harness and tests pass require_http=False: both stay optional.
        Raises SettingsError naming every missing or invalid variable.
        """
        problems: list[str] = []
        missing: list[str] = []

        api_key = _text(env.get(ENV_API_KEY))
        if not api_key:
            missing.append(ENV_API_KEY)

        public_base_url = _text(env.get(ENV_PUBLIC_BASE_URL))
        if require_http and not public_base_url:
            missing.append(ENV_PUBLIC_BASE_URL)

        # The password is kept exactly as written (surrounding spaces are part of a password).
        raw_password = env.get(ENV_AUTH_PASSWORD)
        password = raw_password if raw_password is not None and raw_password.strip() else None
        if require_http and password is None:
            missing.append(ENV_AUTH_PASSWORD)

        if missing:
            problems.append("missing or blank required environment variable(s): " + ", ".join(missing))

        toolsets = frozenset(DEFAULT_TOOLSETS)
        try:
            toolsets = _parse_toolsets(env.get(ENV_TOOLSETS))
        except SettingsError as exc:
            problems.append(str(exc))

        destructive = False
        try:
            destructive = _parse_flag(ENV_DESTRUCTIVE, env.get(ENV_DESTRUCTIVE))
        except SettingsError as exc:
            problems.append(str(exc))

        if problems:
            raise SettingsError("; ".join(problems))

        return cls(
            api_key=api_key,
            public_base_url=public_base_url or None,
            mcp_auth_password=password,
            toolsets=toolsets,
            destructive=destructive,
        )


def _text(value: str | None) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_toolsets(raw: str | None) -> frozenset[str]:
    typed: dict[str, str] = {}  # lowercase name -> the text as the operator wrote it
    for part in (raw or "").split(","):
        token = part.strip()
        if token and token.lower() not in typed:
            typed[token.lower()] = token
    names = list(typed)
    if not names:
        return frozenset(DEFAULT_TOOLSETS)
    unknown = [typed[name] for name in names if name != "all" and name not in TOOLSETS]
    if unknown:
        raise SettingsError(
            f"{ENV_TOOLSETS} names unknown toolset(s) {', '.join(repr(n) for n in unknown)}; "
            f"valid names: all, {', '.join(TOOLSETS)}"
        )
    if "all" in names:
        return frozenset(TOOLSETS)
    return frozenset(names)


def _parse_flag(name: str, raw: str | None) -> bool:
    word = (raw or "").strip().lower()
    if not word or word in _FALSE_WORDS:
        return False
    if word in _TRUE_WORDS:
        return True
    raise SettingsError(
        f"{name} must be one of 1, true, yes, on (enable) or 0, false, no, off (disable), got {raw!r}"
    )
