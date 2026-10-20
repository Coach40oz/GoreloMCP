"""Process settings, read once from the environment.

`Settings.from_env(os.environ)` is the only place environment variables are interpreted. It never
reads a file (main.py calls load_dotenv() first) and it reports EVERY problem in one
`SettingsError`, so an operator fixes the environment in one pass (the names of a GORELO_ENABLE_DESTRUCTIVE list are
checked against the tools after the other variables are valid).

Variables:
    GORELO_API_KEY            required. The Gorelo API key (sent as X-API-Key).
    PUBLIC_BASE_URL           required when require_http is true (the OAuth issuer URL).
    MCP_AUTH_PASSWORD         required when require_http is true (the consent page password).
    GORELO_TOOLSETS           optional. Comma list of toolsets, case-insensitive, or "all".
                              Unset or blank means DEFAULT_TOOLSETS.
    GORELO_ENABLE_DESTRUCTIVE optional. Switches on the gated tools: the delete and void tools,
                              remove_document_related_item (it removes the link between a document and another
                              record), create_approved_invoice (it creates an Approved invoice, which Gorelo pushes to
                              the connected accounting system at once and may email to its recipients) and
                              share_document (it turns a document's public link on, so anyone who has the link can
                              read it without a login, or publishes it to the client portal). Every call still needs
                              confirm=true.
                              1/true/yes/on registers every gated tool of the enabled toolsets.
                              0/false/no/off/unset/blank registers none of them.
                              A comma list of gated tool names (case-insensitive; spaces around a name, empty items and
                              repeats are ignored; a space inside an item is an error), for example
                              delete_time_entry,delete_item, registers exactly those and no other gated tool. This module
                              cannot know the tools, so server.resolve_gated_tools checks the names against the registry:
                              a name that is not a gated tool, or a gated tool of a toolset that GORELO_TOOLSETS does not
                              enable, stops the start with one error that names this variable, the bad names and the valid
                              ones. A word next to a tool name is an error (a word with stray commas, such as 1, is the
                              word). Anything else is an error.
    GORELO_BASE_URL           optional. The Gorelo API address: the host of the region your Gorelo account is
                              hosted in: US https://api.usw.gorelo.io (the default when unset or blank) or Australia
                              https://api.aue.gorelo.io. "/v1" and a trailing slash are optional; the value is
                              normalized to https://<host>/v1. It must be https, on gorelo.io or a subdomain of it,
                              with no user name, port, query, fragment or other path. The start line
                              `gorelo-mcp settings: api_host=...` shows the host in use. The changelog watcher and the
                              live harness read the same variable.
    MCP_EXTRA_REDIRECT_DOMAINS optional. Comma list of extra domains an OAuth client may be redirected to after
                              sign-in, added after claude.ai, claude.com and localhost (Microsoft Copilot Studio:
                              global.consent.azure-apim.net). An entry also admits its subdomains. Each entry is a
                              bare DNS name with at least one dot: no scheme, path, port, wildcard, IP address or
                              localhost. Unset, blank or only commas adds nothing. Anything else is an error.

The repr of Settings never shows the API key or the password.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

TOOLSETS: tuple[str, ...] = ("core", "tickets", "time", "billing", "uptime", "projects", "forms", "documents")
DEFAULT_TOOLSETS: tuple[str, ...] = ("core", "tickets", "time", "billing", "uptime")
DEFAULT_BASE_URL = "https://api.usw.gorelo.io/v1"
API_DOMAIN = "gorelo.io"  # the API host must be this or a subdomain of it
BUILTIN_REDIRECT_DOMAINS: tuple[str, ...] = ("claude.ai", "claude.com", "localhost")  # personal_auth's own default

ENV_API_KEY = "GORELO_API_KEY"
ENV_BASE_URL = "GORELO_BASE_URL"
ENV_PUBLIC_BASE_URL = "PUBLIC_BASE_URL"
ENV_AUTH_PASSWORD = "MCP_AUTH_PASSWORD"
ENV_TOOLSETS = "GORELO_TOOLSETS"
ENV_DESTRUCTIVE = "GORELO_ENABLE_DESTRUCTIVE"
ENV_EXTRA_REDIRECT_DOMAINS = "MCP_EXTRA_REDIRECT_DOMAINS"

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


class SettingsError(Exception):
    """The environment cannot produce valid settings. The message lists every problem."""


@dataclass(frozen=True)
class Settings:
    """Validated process settings. Build one with `Settings.from_env`.

    `destructive` is the master switch for the gated tools (kind destructive): false registers none of them, whatever
    `destructive_tools` holds. When it is true, `destructive_tools` chooses which: None (the default, and what the words
    1/true/yes/on give) means every gated tool of the enabled toolsets, a frozenset of tool names means exactly those
    (from_env gives such a set only with at least one name, and then also sets `destructive`). A Settings made by hand
    with only `destructive=True` or `destructive=False` therefore works as it always did, and `dataclasses.replace(s,
    destructive=False)` switches every gated tool off. The names are not checked here, because this module does not
    know the tools: `server.resolve_gated_tools` checks them against the registry, and `build_server` calls it.
    """

    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    public_base_url: str | None = None
    mcp_auth_password: str | None = field(default=None, repr=False)
    toolsets: frozenset[str] = frozenset(DEFAULT_TOOLSETS)
    destructive: bool = False
    extra_redirect_domains: tuple[str, ...] = ()
    destructive_tools: frozenset[str] | None = None

    @property
    def redirect_domains(self) -> list[str]:
        """The OAuth redirect allowlist for PersonalAuthProvider: the built-in domains, then the extra ones (a new list each call)."""
        return [*BUILTIN_REDIRECT_DOMAINS, *self.extra_redirect_domains]

    @property
    def api_host(self) -> str:
        """The host of base_url, for log lines and diagnostics ('' when it has none)."""
        return urlsplit(self.base_url).hostname or ""

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, require_http: bool = True) -> Settings:
        """Read settings from `env` (normally `os.environ`).

        With require_http true (the real server) PUBLIC_BASE_URL and MCP_AUTH_PASSWORD are
        required. The live harness and tests pass require_http=False: both stay optional.
        Raises SettingsError naming every missing or invalid variable.
        base_url and the extra redirect domains are validated here and nowhere else (a Settings built by hand is not).
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

        destructive, destructive_tools = False, None
        try:
            destructive, destructive_tools = parse_destructive(env.get(ENV_DESTRUCTIVE))
        except SettingsError as exc:
            problems.append(str(exc))

        base_url = DEFAULT_BASE_URL
        try:
            base_url = parse_base_url(env.get(ENV_BASE_URL))
        except SettingsError as exc:
            problems.append(str(exc))

        extra_redirect_domains: tuple[str, ...] = ()
        try:
            extra_redirect_domains = parse_extra_redirect_domains(env.get(ENV_EXTRA_REDIRECT_DOMAINS))
        except SettingsError as exc:
            problems.append(str(exc))

        if problems:
            raise SettingsError("; ".join(problems))

        return cls(
            api_key=api_key,
            base_url=base_url,
            public_base_url=public_base_url or None,
            mcp_auth_password=password,
            toolsets=toolsets,
            destructive=destructive,
            extra_redirect_domains=extra_redirect_domains,
            destructive_tools=destructive_tools,
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


_DESTRUCTIVE_USE = (
    "write 1, true, yes or on (every gated tool), 0, false, no or off (none), or gated tool names separated by commas"
)
_TOOL_NAME_CHARS = re.compile(r"[a-z0-9_-]{1,64}")  # the alphabet and length of a registered tool name, in lower case


def _shown(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def parse_destructive(raw: str | None) -> tuple[bool, frozenset[str] | None]:
    """GORELO_ENABLE_DESTRUCTIVE as (destructive, destructive_tools).

    Unset, blank, only commas and spaces, 0, false, no or off: (False, None), no gated tool. 1, true, yes or on, in any case:
    (True, None), every gated tool of the enabled toolsets (a word with a stray comma, "1,", is the word). Anything else is
    a comma list of gated tool names: each item is trimmed and lower-cased, empty items and repeats are dropped, and the
    result is (True, frozenset of the names). A list that holds one of the words next to other items ("1,delete_item") or an
    item with characters a tool name cannot have (a space between two names, a semicolon) raises SettingsError naming the
    variable. Whether a name is a gated tool of an enabled toolset is not decided here (this module does not know the
    tools): server.resolve_gated_tools decides.
    """
    text = (raw or "").strip().lower()
    if not text or text in _FALSE_WORDS:
        return False, None
    if text in _TRUE_WORDS:
        return True, None
    names = {token for token in (part.strip() for part in text.split(",")) if token}
    if not names:
        return False, None
    if len(names) == 1 and (word := next(iter(names))) in _TRUE_WORDS | _FALSE_WORDS:  # "1," is the word 1
        return word in _TRUE_WORDS, None
    words = sorted(names & (_TRUE_WORDS | _FALSE_WORDS))
    if words:
        raise SettingsError(
            f"{ENV_DESTRUCTIVE} mixes the word {words[0]!r} with other items; {_DESTRUCTIVE_USE}, never both in one value"
        )
    bad = sorted(name for name in names if not _TOOL_NAME_CHARS.fullmatch(name))
    if bad:
        raise SettingsError(
            f"{ENV_DESTRUCTIVE} has item(s) that cannot be tool names: {', '.join(repr(_shown(name)) for name in bad)} "
            f"(a tool name has only letters, digits, underscores and hyphens, and names are separated by commas); {_DESTRUCTIVE_USE}"
        )
    return True, frozenset(names)


_REGION_HELP = "US: https://api.usw.gorelo.io, Australia: https://api.aue.gorelo.io; unset or blank means the US address"
_CLEAN_URL = re.compile(r"[\x21-\x5b\x5d-\x7e]+")  # printable ASCII except space and backslash
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_REDIRECT_CHARS = re.compile(r"[a-z0-9.-]+")
# A last part a URL parser reads as a number (decimal, or 0x hex) makes the host an IPv4 address, as in 127.0.0.0x1.
_IPV4_LAST_PART = re.compile(r"[0-9]+|0x[0-9a-f]*")


def _is_dns_name(name: str) -> bool:
    """A lower-case DNS name: 1 to 253 characters, dot separated labels of letters, digits and inner hyphens (1 to 63 each)."""
    return 0 < len(name) <= 253 and all(_DNS_LABEL.fullmatch(label) for label in name.split("."))


def _base_url_error(why: str) -> SettingsError:
    return SettingsError(f"{ENV_BASE_URL} {why} ({_REGION_HELP})")


def parse_base_url(raw: str | None) -> str:
    """GORELO_BASE_URL as the process uses it: "https://<host>/v1".

    None, empty or blank is DEFAULT_BASE_URL. Otherwise the value must be an https address whose host is gorelo.io or ends with
    ".gorelo.io", with no user name or password, port, query, fragment or path other than "", "/", "/v1" or "/v1/". The host is
    lower-cased. Raises SettingsError naming GORELO_BASE_URL and the reason. The message never repeats the value (it may hold a
    password); only a refused host name is shown. The watcher and the live harness call this too, so they refuse what the server
    refuses.
    """
    text = _text(raw)
    if not text:
        return DEFAULT_BASE_URL
    if not _CLEAN_URL.fullmatch(text):
        raise _base_url_error("must be one address without spaces, control characters, backslashes or non-ASCII characters")
    try:
        parts = urlsplit(text)
    except ValueError:
        raise _base_url_error("is not a valid address") from None
    if parts.scheme.lower() != "https":
        raise _base_url_error("must start with https://")
    if "@" in parts.netloc:
        raise _base_url_error("must not contain a user name or password")
    host = parts.hostname or ""
    if not host:
        raise _base_url_error("must name a host under gorelo.io")
    if not _is_dns_name(host) or not (host == API_DOMAIN or host.endswith("." + API_DOMAIN)):
        raise _base_url_error(f"must name a host under gorelo.io, not {host[:80]!r}")
    if ":" in parts.netloc:
        raise _base_url_error("must not contain a port")
    if "?" in text:
        raise _base_url_error("must not contain a query string")
    if "#" in text:
        raise _base_url_error("must not contain a fragment")
    if parts.path not in ("", "/", "/v1", "/v1/"):
        raise _base_url_error("must not contain a path other than /v1")
    return f"https://{host}/v1"


def _redirect_domain_problem(entry: str) -> str | None:
    """Why a trimmed, lower-cased entry (without '@') is not an acceptable redirect domain, or None. The first problem wins."""
    if "://" in entry or any(char in entry for char in "/?#\\"):
        return "must be a bare domain name, without a scheme, path, query or fragment"
    if ":" in entry:
        return "must not contain a port or an IP address"
    if "*" in entry:
        return "must not contain a wildcard (an entry already admits its subdomains)"
    if not _REDIRECT_CHARS.fullmatch(entry):
        return "may contain only letters, digits, hyphens and dots"
    if entry == "localhost" or entry.endswith(".localhost"):
        return "must not be localhost (it is already allowed)"
    if "." not in entry:
        return "must contain at least one dot (a single name such as 'com' would admit a whole top-level domain)"
    if not _is_dns_name(entry):
        return "is not a valid DNS name (an empty part, a part that starts or ends with a hyphen, or a part longer than 63 characters)"
    if _IPV4_LAST_PART.fullmatch(entry.rsplit(".", 1)[1]):
        return (
            "must be a domain name, not an IP address (its last part must not be a number: digits, or 0x followed by hex digits, "
            "make a URL parser read the host as an IP address)"
        )
    return None


def parse_extra_redirect_domains(raw: str | None) -> tuple[str, ...]:
    """MCP_EXTRA_REDIRECT_DOMAINS as a tuple of lower-case domains: trimmed, blank pieces skipped, repeats and the built-in
    domains dropped, order kept. Raises one SettingsError that names the variable and every bad entry (an entry with '@' is
    named without its text, it may hold a password; a long entry is cut at 60 characters)."""
    taken = set(BUILTIN_REDIRECT_DOMAINS)
    extra: list[str] = []
    problems: list[str] = []
    for token in (raw or "").split(","):
        entry = token.strip().lower()
        if not entry:
            continue
        if "@" in entry:
            problems.append(
                f"{ENV_EXTRA_REDIRECT_DOMAINS} has an entry containing '@', which is not allowed (a domain name has no user name or password)"
            )
            continue
        problem = _redirect_domain_problem(entry)
        if problem is not None:
            shown = entry if len(entry) <= 60 else entry[:60] + "..."
            problems.append(f"{ENV_EXTRA_REDIRECT_DOMAINS} entry {shown!r} {problem}")
        elif entry not in taken:
            taken.add(entry)
            extra.append(entry)
    if problems:
        raise SettingsError("; ".join(problems))
    return tuple(extra)
