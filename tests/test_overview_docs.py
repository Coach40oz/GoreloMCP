"""docs/OVERVIEW.md ("How the Gorelo MCP server works") says what the code does.

The overview is the one document an AI agent or a person reads instead of the code, so it must not rot. These
checks tie it to the code and to the rest of the repository:

* every file path it names exists (a path under a git-ignored runtime name such as .env or .state is not required
  to exist in a checkout), every operation key it quotes is in the spec index, and every code identifier it names
  (snake_case, UPPER_SNAKE or CamelCase) is still defined somewhere in the code;
* the tool counts it quotes (the counts table, the toolset table, the tool module table and any "N tools" in the
  prose) equal the registry, and the gated tools, forbidden operations and side-effect GETs it lists are the
  code's own sets;
* its module list covers every .py file of the repository root, tools/ and scripts/;
* the numbers and limits it quotes are the constants in the code, the login numbers included (the limiters, the caps, the
  lifetimes, the retention) and what the login section says the provider does is what the provider does; the body cap is for
  every method and an OPTIONS with a body is refused with 400 (a CORS preflight has none), which is run in process;
* what it says about the entry point and the admin script is what the code does: the login modules are loaded by the entry point
  and not by the server module, and `check` still prints a state file's mode, owner and lock when the store refuses the file;
* it is generic: no tenant hostnames, email addresses, ids or host paths (the one documented exception is the watcher's key file),
  and no hostname but the Gorelo API host and the AI client's own;
* it has its seven sections, every file it names exists, and it has no em or en dashes
  (tests/test_hygiene.py scans it too).

Reading the document is lazy (a missing file fails these tests, it does not break collection). Nothing here
touches the network or reads an environment file.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import io
import json
import re
import time
from collections import Counter
from pathlib import Path

import pytest
from mcp.server.auth.provider import AuthorizationCode
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

import main as main_module
import oauth_guard
import oauth_store
import personal_auth
import server as server_module
import tools  # noqa: F401  importing the package fills the registry
from gorelo_client import (
    FORBIDDEN_OPS,
    GORELO_BASE_URL,
    MAX_PAGES,
    PAGE_SIZE_MAX,
    PAGE_SIZE_MIN,
    SIDE_EFFECT_GETS,
    GoreloClient,
)
from scripts import oauth_state as oauth_state_script
from scripts.live.write_matrix import APPROVED_INVOICE_TOOLS
from settings import (
    DEFAULT_BASE_URL,
    DEFAULT_TOOLSETS,
    ENV_API_KEY,
    ENV_AUTH_PASSWORD,
    ENV_DESTRUCTIVE,
    ENV_PUBLIC_BASE_URL,
    ENV_TOOLSETS,
    TOOLSETS,
)
from spec import load_spec_index
from tools import attachments as attachments_module
from tools import invoices as invoices_module
from tools._common import REGISTRY

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERVIEW_PATH = REPO_ROOT / "docs" / "OVERVIEW.md"
GITIGNORE_PATH = REPO_ROOT / ".gitignore"

# Spelled with chr() so this file never contains the characters it forbids.
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

SECTIONS = (
    "1. What it is and the request path",
    "2. Module map",
    "3. Safety layers",
    "4. The spec index and live overrides",
    "5. Testing",
    "6. Operations",
    "7. Where to look for what",
)

# Names that exist only on a running host (git-ignored, see .gitignore): the document may name them although a
# checkout does not hold them. test_the_runtime_names_are_git_ignored keeps this list honest.
RUNTIME_ONLY = (".env", ".oauth-state", ".state", ".live-runs", ".venv")

# The only hostnames the document may spell out: the Gorelo API host, the AI client's own and the reserved test domain.
ALLOWED_HOSTS = frozenset({"api.usw.gorelo.io", "claude.ai", "claude.com", "example.invalid",
                           "site.local"})  # the start of the file name site.local.toml, not a host
HOST_TLDS = "com|io|tech|net|org|dev|ai|app|invalid|local|cloud|co|me|xyz"

HOST_PATH_PREFIXES = ("/opt/", "/etc/", "/root/", "/home/", "/var/", "/usr/", "/tmp/")
# Host paths the document may spell out: the watcher has its own key file and does not read .env.
ALLOWED_HOST_PATHS = ("/etc/gorelo-mcp/watcher.env",)

FILE_SUFFIXES = (".py", ".md", ".json", ".service", ".timer", ".toml", ".lock", ".ini", ".txt")
PATH_SHAPE = re.compile(r"[A-Za-z0-9_.\-<>*]+(?:/[A-Za-z0-9_.\-<>*]+)*/?")
MIME_TYPE = re.compile(r"(?:application|text|image|multipart)/[a-z0-9.+-]+")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CAMEL_CASE = re.compile(r"[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+")
OP_KEY = re.compile(r"\b(?:GET|POST|PATCH|PUT|DELETE) /v1/[A-Za-z0-9_/{}\-]*[A-Za-z0-9_}]")
FENCE = re.compile(r"^```.*?^```[ \t]*$", re.MULTILINE | re.DOTALL)


# --------------------------------------------------------------------------
# Reading the document
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def overview() -> str:
    assert OVERVIEW_PATH.is_file(), "docs/OVERVIEW.md does not exist"
    return OVERVIEW_PATH.read_text(encoding="utf-8")


def outside_fences(text: str) -> str:
    """The text without fenced code blocks (the commands in them name host paths and are not claims)."""
    return FENCE.sub("", text)


def code_spans(text: str) -> list[str]:
    """Every inline `code span` outside fenced blocks."""
    return re.findall(r"`([^`\n]+)`", outside_fences(text))


def squash(text: str) -> str:
    return " ".join(text.split())


def table_rows(text: str) -> list[list[str]]:
    """The cells of every markdown table row (separator rows excluded)."""
    rows = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        rows.append(cells)
    return rows


def section(title: str) -> str:
    """The text of the `## <title>` section."""
    text = overview()
    marker = f"\n## {title}\n"
    assert marker in text, f"docs/OVERVIEW.md has no section {title!r}"
    start = text.index(marker) + 1
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def unquote(cell: str) -> str:
    match = re.fullmatch(r"`([^`]+)`", cell)
    return match.group(1) if match else cell


# --------------------------------------------------------------------------
# The registry, as the document describes it
# --------------------------------------------------------------------------


def module_of(spec) -> str:
    return spec.fn.__module__.rsplit(".", 1)[-1]


def registry_counts() -> dict[str, int]:
    specs = REGISTRY.specs
    kinds = Counter(spec.kind for spec in specs)
    return {
        "Tools in the registry": len(specs),
        "Regular tools (kind `read` or `write`)": sum(1 for spec in specs if spec.kind != "destructive"),
        "Gated tools (kind `destructive`)": kinds["destructive"],
        "Tools of kind read": kinds["read"],
        "Tools of kind write": kinds["write"],
        "Tools of kind destructive": kinds["destructive"],
        "Registered with the default toolsets, gated tools off": len(REGISTRY.select(DEFAULT_TOOLSETS, False)),
        "Registered with the default toolsets, gated tools on": len(REGISTRY.select(DEFAULT_TOOLSETS, True)),
        "Registered with every toolset, gated tools off": len(REGISTRY.select(TOOLSETS, False)),
        "Registered with every toolset, gated tools on": len(REGISTRY.select(TOOLSETS, True)),
    }


def quoted_counts(text: str) -> dict[str, int]:
    """Label to number for every two-cell table row whose second cell is a number (the counts table)."""
    return {row[0]: int(row[1]) for row in table_rows(text) if len(row) == 2 and row[1].isdigit()}


# --------------------------------------------------------------------------
# Structure and hygiene
# --------------------------------------------------------------------------


def test_the_document_exists_and_has_its_seven_sections_in_order():
    headings = [line[3:] for line in overview().splitlines() if line.startswith("## ")]
    assert headings == list(SECTIONS)
    assert overview().startswith("# How the Gorelo MCP server works\n")


def test_no_em_or_en_dashes():
    offenders = [
        number
        for number, line in enumerate(overview().splitlines(), start=1)
        if EM_DASH in line or EN_DASH in line
    ]
    assert offenders == [], f"em or en dash in docs/OVERVIEW.md at lines {offenders}"


def test_the_runtime_names_are_git_ignored():
    # The exemption in the path check must not hide a tracked file: each name has to be in .gitignore.
    ignored = {
        line.strip().rstrip("/")
        for line in GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert set(RUNTIME_ONLY) <= ignored, sorted(set(RUNTIME_ONLY) - ignored)


def test_no_email_addresses_and_no_host_paths():
    text = overview()
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", text)
    assert emails == [], f"email addresses in docs/OVERVIEW.md: {emails}"
    for allowed in ALLOWED_HOST_PATHS:
        text = text.replace(allowed, "")
    paths = [prefix for prefix in HOST_PATH_PREFIXES if prefix in text]
    assert paths == [], f"host paths in docs/OVERVIEW.md: {paths}"


def test_no_hostname_but_the_allowed_ones():
    text = overview()
    found = set(re.findall(rf"\b(?:[a-z0-9-]+\.)+(?:{HOST_TLDS})\b", text, flags=re.IGNORECASE))
    unexpected = sorted(host for host in found if host.lower() not in ALLOWED_HOSTS)
    assert unexpected == [], f"hostnames in docs/OVERVIEW.md that the generic document may not name: {unexpected}"
    assert "api.usw.gorelo.io" in text


# --------------------------------------------------------------------------
# File paths, operation keys and identifiers
# --------------------------------------------------------------------------


def path_claims(text: str) -> list[str]:
    """The code spans that name a file or a directory of the repository (relative paths only)."""
    claims = []
    for span in code_spans(text):
        if "://" in span or span.startswith("/") or MIME_TYPE.fullmatch(span) or not PATH_SHAPE.fullmatch(span):
            continue
        bare = span.rstrip("/")
        if "/" in span or bare.endswith(FILE_SUFFIXES) or bare.startswith("."):
            claims.append(span)
    return claims


def path_exists(span: str) -> bool:
    bare = span.rstrip("/")
    if bare.split("/")[0] in RUNTIME_ONLY:
        return True
    if "*" in bare:
        return any(True for _ in REPO_ROOT.glob(bare))
    if "<" in bare:  # a placeholder segment such as tools/<domain>.py: the directory before it must exist
        before = bare.split("<", 1)[0]
        directory = before.rsplit("/", 1)[0] if "/" in before else ""
        return (REPO_ROOT / directory).is_dir()
    return (REPO_ROOT / bare).exists()


def test_it_names_files_at_all():
    claims = path_claims(overview())
    for must_name in (
        "docs/INSTALL.md", "docs/OPERATIONS.md", "docs/TROUBLESHOOTING.md", "docs/API-OBSERVED-BEHAVIOR.md",
        "CONTRIBUTING.md", "SECURITY.md", "docs/tool-catalog.md", "main.py", "tools/_common.py",
    ):
        assert must_name in claims, f"{must_name} is no longer named in docs/OVERVIEW.md"
    assert len(claims) >= 30


def test_every_file_path_it_names_exists():
    missing = sorted({span for span in path_claims(overview()) if span not in UNTRACKED_LOCAL_FILES and not path_exists(span)})
    assert missing == [], f"docs/OVERVIEW.md names paths that do not exist: {missing}"


UNTRACKED_LOCAL_FILES = frozenset({"site.local.toml"})  # gitignored, exists only on an installed host


def test_every_operation_key_it_quotes_is_in_the_spec_index():
    ops = load_spec_index().ops
    quoted = sorted(set(OP_KEY.findall(outside_fences(overview()))))
    assert quoted, "the overview quotes no operation key any more"
    unknown = [key for key in quoted if key not in ops]
    assert unknown == [], f"operation keys in docs/OVERVIEW.md that the spec index does not have: {unknown}"


def code_corpus_words() -> set[str]:
    """Every identifier-like word in the code the overview describes (the repository root, tools/, scripts/, the shared
    test fixtures, the spec files and .env.example)."""
    files = [*REPO_ROOT.glob("*.py"), *(REPO_ROOT / "tools").glob("*.py")]
    files += [p for p in (REPO_ROOT / "scripts").rglob("*.py") if "__pycache__" not in p.parts]
    files += [REPO_ROOT / "tests" / "conftest.py", REPO_ROOT / ".env.example"]
    files += sorted((REPO_ROOT / "spec").glob("*.json"))
    words: set[str] = set()
    for path in files:
        words.update(IDENTIFIER.findall(path.read_text(encoding="utf-8")))
    return words


def named_identifiers(text: str) -> set[str]:
    """Identifiers in code spans that are snake_case, UPPER_SNAKE or CamelCase: the ones that name code."""
    found: set[str] = set()
    for span in code_spans(text):
        if "://" in span or "*" in span or span.startswith("/") or MIME_TYPE.fullmatch(span):
            continue
        if "/" in span or span.rstrip("/").endswith(FILE_SUFFIXES):
            continue  # a path: test_every_file_path_it_names_exists covers it
        for token in IDENTIFIER.findall(span):
            if "_" in token.strip("_") or CAMEL_CASE.fullmatch(token):
                found.add(token)
    return found


def test_every_code_identifier_it_names_is_defined_in_the_code():
    names = named_identifiers(overview())
    assert len(names) >= 60, "the overview names far fewer identifiers than it used to"
    words = code_corpus_words()
    missing = sorted(name for name in names if name not in words)
    assert missing == [], f"docs/OVERVIEW.md names identifiers that no code defines or mentions: {missing}"


# --------------------------------------------------------------------------
# Counts, modules and toolsets against the registry
# --------------------------------------------------------------------------


def test_the_counts_table_equals_the_registry():
    assert quoted_counts(overview()) == registry_counts()


def test_the_toolset_table_equals_the_registry():
    expected = {}
    for toolset in TOOLSETS:
        specs = [spec for spec in REGISTRY.specs if spec.toolset == toolset]
        expected[toolset] = {
            "default": "yes" if toolset in DEFAULT_TOOLSETS else "no",
            "tools": len(specs),
            "gated": sum(1 for spec in specs if spec.kind == "destructive"),
            "modules": sorted({module_of(spec) for spec in specs}),
        }
    found = {}
    for row in table_rows(section("2. Module map")):
        if len(row) == 5 and unquote(row[0]) in TOOLSETS and row[2].isdigit():
            found[unquote(row[0])] = {
                "default": row[1],
                "tools": int(row[2]),
                "gated": int(row[3]),
                "modules": sorted(name.strip() for name in row[4].split(",")),
            }
    assert found == expected


def test_the_tool_module_table_equals_the_registry():
    by_module: dict[str, list] = {}
    for spec in REGISTRY.specs:
        by_module.setdefault(module_of(spec), []).append(spec)
    expected = {}
    for module, specs in by_module.items():
        toolsets = {spec.toolset for spec in specs}
        assert len(toolsets) == 1, f"tools/{module}.py holds tools of several toolsets: {sorted(toolsets)}"
        expected[f"tools/{module}.py"] = (len(specs), toolsets.pop())
    found = {}
    for row in table_rows(section("2. Module map")):
        if len(row) == 4 and re.fullmatch(r"`tools/[a-z_]+\.py`", row[0]) and row[1].isdigit():
            found[unquote(row[0])] = (int(row[1]), row[2])
    assert found == expected


def test_a_tool_count_in_the_prose_is_a_count_the_registry_has():
    allowed = set(registry_counts().values())
    for toolset in TOOLSETS:
        specs = [spec for spec in REGISTRY.specs if spec.toolset == toolset]
        allowed |= {len(specs), sum(1 for spec in specs if spec.kind == "destructive")}
    for module in {module_of(spec) for spec in REGISTRY.specs}:
        allowed.add(sum(1 for spec in REGISTRY.specs if module_of(spec) == module))
    quoted = re.findall(r"\b(\d+)\s+(?:regular\s+|gated\s+)?tools?\b", outside_fences(overview()))
    stray = sorted({int(number) for number in quoted if int(number) not in allowed})
    assert stray == [], f"the overview says {stray} tools somewhere; the registry has no such count"


def test_the_module_list_covers_every_python_file():
    text = overview()
    spans = set(code_spans(text))
    root = sorted(path.name for path in REPO_ROOT.glob("*.py"))
    in_tools = sorted(f"tools/{path.name}" for path in (REPO_ROOT / "tools").glob("*.py"))
    in_scripts = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "scripts").rglob("*.py")
        if "__pycache__" not in path.parts
    )
    assert root and in_tools and in_scripts
    missing = [name for name in (*root, *in_tools, *in_scripts) if name not in spans]
    assert missing == [], f"python files that docs/OVERVIEW.md does not name in its module map: {missing}"
    assert "tools/_common.py" in in_tools and "scripts/live/guard.py" in in_scripts


def test_the_registry_module_list_matches_the_tool_package():
    # tools/__init__.py imports every tool module: a module that registers tools but is not imported there would
    # never register, and the module table above would list tools that do not exist.
    imported = set(re.findall(r"^\s+(\w+),$", (REPO_ROOT / "tools" / "__init__.py").read_text(encoding="utf-8"), re.M))
    with_tools = {module_of(spec) for spec in REGISTRY.specs}
    assert with_tools <= imported


def test_the_gated_tools_it_lists_are_the_registry_destructive_tools():
    line = next((line for line in overview().splitlines() if line.startswith("Gated tools")), None)
    assert line is not None, "the overview no longer lists the gated tools"
    listed = set(re.findall(r"`([^`]+)`", line.split("): ", 1)[1]))
    assert listed == {spec.name for spec in REGISTRY.specs if spec.kind == "destructive"}


def test_the_forbidden_operations_and_side_effect_gets_it_lists_are_the_codes():
    lines = overview().splitlines()
    forbidden = next((line for line in lines if line.startswith("Forbidden operations")), None)
    assert forbidden is not None, "the overview no longer lists the forbidden operations"
    assert set(re.findall(r"`([^`]+)`", forbidden.split("): ", 1)[1])) == set(FORBIDDEN_OPS)
    side_effects = next((line for line in lines if line.startswith("Side-effect GETs")), None)
    assert side_effects is not None, "the overview no longer lists the side-effect GETs"
    assert set(re.findall(r"`([^`]+)`", side_effects.split("): ", 1)[1])) == set(SIDE_EFFECT_GETS)


def test_the_kinds_it_relies_on_are_the_registry_kinds():
    kinds = {spec.name: spec.kind for spec in REGISTRY.specs}
    relied_on = {
        "health_check": "read",
        "create_invoice": "write",
        "create_approved_invoice": "destructive",
        "export_invoice_pdf": "write",
        "delete_invoice": "destructive",
        "post_alert": "write",
    }
    for name, kind in relied_on.items():
        assert kinds.get(name) == kind, f"{name} is no longer kind {kind}, which docs/OVERVIEW.md says"
        assert f"`{name}`" in overview(), f"docs/OVERVIEW.md no longer names {name}"


# --------------------------------------------------------------------------
# Numbers and limits that the document quotes
# --------------------------------------------------------------------------


# How many characters of a client id the admin script (and the journal) show: read from its function, not typed here.
ID8_LENGTH = len(oauth_state_script.id8("a" * 40))


def window_words(seconds: int) -> str:
    """The words the document uses for a window of time; the number of seconds itself when it has none for it, so that a
    changed constant fails the test that quotes it (with a message that shows the new value) instead of the collection."""
    return {60 * 60: "an hour", 24 * 60 * 60: "a day"}.get(seconds, f"{seconds} seconds")


def quoted_facts() -> list[tuple[str, str]]:
    """(what, the exact words the document uses for it, built from the constant in the code)."""
    parameters = inspect.signature(GoreloClient.__init__).parameters
    spec = load_spec_index()
    return [
        ("concurrency", f"at most {parameters['max_concurrency'].default} requests in flight"),
        ("timeout", f"a {int(parameters['timeout'].default)} s timeout per request"),
        (
            "429 retries",
            f"up to {parameters['max_429_retries'].default} times within about {int(parameters['max_429_wait'].default)} s",
        ),
        ("page size", f"clamped to {PAGE_SIZE_MIN} to {PAGE_SIZE_MAX}"),
        ("page limit", f"after {MAX_PAGES} pages"),
        (
            "password attempts",
            f"{oauth_guard.FAILURES_PER_IP} wrong passwords per client address in {oauth_guard.FAILURE_WINDOW_SECONDS // 60} minutes",
        ),
        (
            "password attempts overall",
            f"{oauth_guard.FAILURES_OVERALL} wrong passwords in {window_words(oauth_guard.FAILURE_OVERALL_WINDOW_SECONDS)} "
            "from all addresses",
        ),
        ("remembered addresses", f"at most {oauth_guard.MAX_LIMITER_KEYS} addresses are remembered"),
        (
            "registrations per address",
            f"{oauth_guard.REGISTRATIONS_PER_IP} registrations {window_words(oauth_guard.REGISTRATION_IP_WINDOW_SECONDS)} per address",
        ),
        (
            "registrations overall",
            f"{oauth_guard.REGISTRATIONS_OVERALL} {window_words(oauth_guard.REGISTRATION_OVERALL_WINDOW_SECONDS)} overall",
        ),
        ("stored clients", f"at most {oauth_guard.MAX_CLIENTS} clients are stored"),
        ("idle client eviction", f"an idle one older than {window_words(oauth_guard.EVICTION_MIN_AGE_SECONDS)} makes room"),
        ("body cap", f"A body over {oauth_guard.MAX_BODY_BYTES // 1024} KiB is refused"),
        (
            "body capped paths",
            "whatever the method, on " + ", ".join(f"`{path}`" for path in oauth_guard.BODY_LIMITED_PATHS[:-1]) + f" and `{oauth_guard.BODY_LIMITED_PATHS[-1]}`",
        ),
        ("short password", f"shorter than {oauth_guard.MIN_PASSWORD_CHARS} characters"),
        ("access token", f"an access token valid for {personal_auth.DEFAULT_ACCESS_TOKEN_EXPIRY // 86400} days"),
        ("refresh access token", f"an access token valid for {personal_auth.DEFAULT_REFRESH_ACCESS_TOKEN_EXPIRY // 3600} hour"),
        ("reuse grace", f"INFO within {personal_auth.DEFAULT_REUSE_GRACE_SECONDS // 60} minutes"),
        (
            "retention",
            f"expired more than {oauth_store.EXPIRED_TOKEN_RETENTION_SECONDS // 86400} days ago and tombstones older than "
            f"{oauth_store.TOMBSTONE_RETENTION_SECONDS // 86400} days",
        ),
        ("client id shown", f"the first {ID8_LENGTH} characters of client ids"),
        ("listener", f"`{main_module.HOST}:{main_module.PORT}`"),
        ("pdf cap", f"read through a {invoices_module.PDF_MAX_BYTES // 2**20} MB cap"),
        ("upload cap", f"capped at {attachments_module.MAX_UPLOAD_BYTES // 2**20} MB"),
        ("operations", f"{len(spec.ops)} operations"),
        ("api base url", f"`{GORELO_BASE_URL}`"),
        ("default toolsets", f"`{','.join(DEFAULT_TOOLSETS)}`"),
    ]


@pytest.mark.parametrize("what, words", quoted_facts(), ids=[what for what, _ in quoted_facts()])
def test_a_quoted_number_is_the_constant_in_the_code(what, words):
    assert words in squash(overview()), f"docs/OVERVIEW.md no longer says {words!r} ({what}); the code says so"


def quantities_in_the_code() -> dict[str, set[int]]:
    """Every unit the document quotes, with the values the code allows for it."""
    parameters = inspect.signature(GoreloClient.__init__).parameters
    timer = (REPO_ROOT / "deploy" / "gorelo-changelog-watch.timer").read_text(encoding="utf-8")
    delay = re.search(r"RandomizedDelaySec=(\d+)m", timer)
    assert delay is not None, "the staged timer no longer has a RandomizedDelaySec in minutes"
    return {
        "MB": {invoices_module.PDF_MAX_BYTES // 2**20, attachments_module.MAX_UPLOAD_BYTES // 2**20},
        "days": {
            personal_auth.DEFAULT_ACCESS_TOKEN_EXPIRY // 86400,
            oauth_store.EXPIRED_TOKEN_RETENTION_SECONDS // 86400,
            oauth_store.TOMBSTONE_RETENTION_SECONDS // 86400,
        },
        "hour": {personal_auth.DEFAULT_REFRESH_ACCESS_TOKEN_EXPIRY // 3600},
        "minutes": {
            oauth_guard.FAILURE_WINDOW_SECONDS // 60,
            personal_auth.DEFAULT_REUSE_GRACE_SECONDS // 60,
            int(delay.group(1)),
        },
        "s": {int(parameters["timeout"].default), int(parameters["max_429_wait"].default)},
        "pages": {MAX_PAGES},
        "times": {parameters["max_429_retries"].default},
        "requests in flight": {parameters["max_concurrency"].default},
        "wrong passwords": {oauth_guard.FAILURES_PER_IP, oauth_guard.FAILURES_OVERALL},
        "KiB": {oauth_guard.MAX_BODY_BYTES // 1024},
        "addresses": {oauth_guard.MAX_LIMITER_KEYS},
        "clients": {oauth_guard.MAX_CLIENTS},
        "characters": {oauth_guard.MIN_PASSWORD_CHARS, ID8_LENGTH},
        "operations": {len(load_spec_index().ops)},
    }


def test_every_quantity_it_quotes_is_a_constant_of_the_code():
    # test_a_quoted_number_is_the_constant_in_the_code needs each phrase once; this one checks every mention, so a
    # number that was changed in one place and left alone in another cannot pass.
    text = outside_fences(overview())
    wrong = []
    for unit, allowed in quantities_in_the_code().items():
        for number in re.findall(rf"\b(\d+) {re.escape(unit)}\b", text):
            if int(number) not in allowed:
                wrong.append(f"{number} {unit} (the code has {sorted(allowed)})")
    assert wrong == [], f"docs/OVERVIEW.md quotes numbers the code does not have: {wrong}"


def test_every_mention_of_the_listener_is_the_address_in_main():
    mentions = re.findall(r"127\.0\.0\.1(?::(\d+))?", overview())
    assert mentions and main_module.HOST == "127.0.0.1"
    assert all(port in ("", str(main_module.PORT)) for port in mentions), f"ports mentioned: {sorted(set(mentions))}"


def test_the_token_lifetimes_and_the_expiry_behavior_it_describes_are_the_providers(tmp_path):
    # The overview says: a first sign-in gets an access token valid for 30 days and a refresh token that never expires; a
    # refresh gets an access token valid for 1 hour and a new refresh token, and the old one is retired; and presenting an
    # expired access token only refuses it: its refresh token stays valid, so the client refreshes instead of signing in
    # again. (The framework's own provider deletes the refresh token together with the expired access token, which is what
    # an earlier version did exactly that; the document must not go back to describing that.) Played through the real
    # provider with a state directory in tmp_path (nothing else is touched).
    provider = personal_auth.PersonalAuthProvider(
        base_url="https://example.invalid", password="a password", state_dir=str(tmp_path)
    )
    try:
        redirect = "https://claude.ai/callback"
        client = OAuthClientInformationFull(client_id="client-1", redirect_uris=[AnyUrl(redirect)])
        code = AuthorizationCode(
            code="code-1",
            scopes=[],
            expires_at=time.time() + 60,
            client_id="client-1",
            code_challenge="c" * 43,
            redirect_uri=AnyUrl(redirect),
            redirect_uri_provided_explicitly=True,
        )
        provider.auth_codes["code-1"] = code
        first = asyncio.run(provider.exchange_authorization_code(client, code))
        assert first.expires_in == 30 * 86400
        assert provider.refresh_tokens[first.refresh_token].expires_at is None
        refreshed = asyncio.run(provider.exchange_refresh_token(client, provider.refresh_tokens[first.refresh_token], []))
        assert refreshed.expires_in == 3600
        assert first.refresh_token not in provider.refresh_tokens and first.access_token not in provider.access_tokens  # retired
        assert asyncio.run(provider.load_refresh_token(client, first.refresh_token)) is None  # and it cannot be used again
        provider.access_tokens[refreshed.access_token].expires_at = int(time.time()) - 1
        assert asyncio.run(provider.load_access_token(refreshed.access_token)) is None  # refused ...
        assert refreshed.refresh_token in provider.refresh_tokens  # ... and nothing else changed: no deletion
        assert refreshed.access_token in provider.access_tokens
        assert asyncio.run(provider.load_refresh_token(client, refreshed.refresh_token)) is not None
        again = asyncio.run(provider.exchange_refresh_token(client, provider.refresh_tokens[refreshed.refresh_token], []))
        assert asyncio.run(provider.load_access_token(again.access_token)) is not None  # the client refreshed, no sign-in
    finally:
        provider.close()
    text = squash(overview())
    assert "an access token valid for 30 days and a refresh token that never expires" in text
    assert "a refresh gets an access token valid for 1 hour and a new refresh token" in text
    assert "the old refresh token is retired" in text
    assert (
        "An expired access token is refused and nothing else changes: its refresh token stays valid, so the client "
        "refreshes instead of signing in again."
    ) in text
    assert "deletes it together with its refresh token" not in text and "framework's default (1 hour)" not in text


def test_the_login_section_says_what_the_provider_does_at_start_and_in_its_routes(tmp_path):
    # "The password is required: without one ... the server refuses to start", and the four routes of the gate
    for bad in (None, "", "   "):
        with pytest.raises(oauth_guard.GateError):
            personal_auth.PersonalAuthProvider(
                base_url="https://example.invalid", password=bad, state_dir=str(tmp_path / "refused")
            )
    assert not (tmp_path / "refused").exists()  # refused before anything was created
    provider = personal_auth.PersonalAuthProvider(
        base_url="https://example.invalid", password="a password", state_dir=str(tmp_path / "state")
    )
    try:
        paths = [route.path for route in provider.get_routes("/mcp")]
    finally:
        provider.close()
    for path in oauth_guard.BODY_LIMITED_PATHS:
        assert paths.count(path) == 1, path
    assert "/mcp" not in oauth_guard.BODY_LIMITED_PATHS  # the endpoint of the tools is never capped
    text = overview()
    for path in (*oauth_guard.BODY_LIMITED_PATHS, "/mcp"):
        assert f"`{path}`" in text, path
    assert "without one, or on a `fastmcp` or `mcp` version the gate was not checked against, the server refuses to start" in squash(text)


def status_of_the_cap(method: str, body: bytes = b"", *, declare: bool = True) -> int:
    """The HTTP status that oauth_guard.BodyLimit gives a request to a capped path, or 204 when it handed the request on to the app
    behind it (a stand-in that answers 204). Driven in process, no socket."""
    statuses: list[int] = []

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    headers = [(b"content-length", str(len(body)).encode())] if declare else []
    scope = {"type": "http", "method": method, "path": "/token", "headers": headers, "client": ("203.0.113.9", 1), "query_string": b""}
    asyncio.run(oauth_guard.BodyLimit(app, path="/token", refuse=oauth_guard.json_refusal)(scope, receive, send))
    return statuses[0]


def test_the_body_cap_is_for_every_method_and_an_options_with_a_body_is_refused_with_400():
    """The 16 KiB cap applies to every method on the four paths, not only POST: the framework's handlers read an OPTIONS body
    without a cap. The code caps every method and refuses any body on an OPTIONS request with 400 (a CORS preflight has none);
    the sentence says so, and the behavior is checked here, in process."""
    paths = oauth_guard.BODY_LIMITED_PATHS
    text = squash(overview())
    assert (
        f"A body over {oauth_guard.MAX_BODY_BYTES // 1024} KiB is refused, whatever the method, on "
        + ", ".join(f"`{path}`" for path in paths[:-1])
        + f" and `{paths[-1]}`, and an OPTIONS request that carries a body is refused with 400 (a CORS preflight has none); "
        "the cap is never applied on `/mcp`."
    ) in text
    assert "refused on POST to" not in text  # an older wording that was only half the truth
    assert "/mcp" not in paths
    limit = oauth_guard.MAX_BODY_BYTES
    for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):  # whatever the method: over the cap is 413, at the cap goes on
        assert status_of_the_cap(method, b"x" * (limit + 1)) == 413, method
        assert status_of_the_cap(method, b"x" * limit) == 204, method
    assert status_of_the_cap("OPTIONS", b"x") == 400  # any body at all
    assert status_of_the_cap("OPTIONS", b"x" * (limit + 1)) == 400
    assert status_of_the_cap("OPTIONS") == 204  # a CORS preflight has no body and goes on
    assert status_of_the_cap("OPTIONS", declare=False) == 204


def test_the_admin_script_it_describes_has_the_commands_it_names():
    row = next((line for line in overview().splitlines() if line.startswith("| `scripts/oauth_state.py` |")), None)
    assert row is not None, "the scripts table no longer has a row for scripts/oauth_state.py"
    for command in oauth_state_script.COMMANDS:
        assert f"`{command}" in row, command
    for promise in ("metadata only", "never a token or a secret", "takes the state lock first", "never creates a state directory"):
        assert promise in squash(row), promise
    assert "`docs/OPERATIONS.md`" in row


def test_the_admin_script_row_says_check_still_prints_the_file_facts_for_a_file_the_store_refuses(tmp_path):
    """When the store refuses the state file (a copy made as root leaves it unreadable for the service user, or it is not JSON),
    check used to print only the error. It prints the facts that need no read of the file (mode, owner, the lock) first, so a file
    left to another user can be told from a damaged one. The row says so, and the script is run here on such a file."""
    row = next((line for line in overview().splitlines() if line.startswith("| `scripts/oauth_state.py` |")), None)
    assert row is not None
    assert (
        "When the store refuses the file, `check` still prints its mode, its owner and the lock before the error, because they need no "
        "read of the file, so a file left to another user can be told from a damaged one."
    ) in squash(row)
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    (state_dir / oauth_store.STATE_FILE_NAME).write_text("not json at all")
    (state_dir / oauth_store.STATE_FILE_NAME).chmod(0o600)
    out = io.StringIO()
    assert oauth_state_script.main(["check", "--state-dir", str(state_dir)], stdout=out, stderr=io.StringIO()) == 1
    kinds = [line.split(":", 1)[0] for line in out.getvalue().splitlines()]
    assert kinds.index("file") < kinds.index("lock") < kinds.index("error") < kinds.index("result")  # facts, lock, then the error


def test_the_login_modules_are_loaded_by_the_entry_point_and_not_by_the_server_module():
    """A login module that was never added to a release still passes every test that does not start the server, because the
    server and settings modules import none of them; only `main.py` loads them. The overview tells the reader that the start
    is what proves a release (the ready line and a 401), so this ties that to the imports."""
    for name, module in (("GateError", "oauth_guard"), ("StateFileError", "oauth_store"), ("PersonalAuthProvider", "personal_auth")):
        assert getattr(main_module, name).__module__ == module  # main.py imports each login module
    login_import = re.compile(r"^\s*(?:from|import)\s+(?:oauth_guard|oauth_store|personal_auth)\b", re.M)
    for name in ("server.py", "settings.py"):
        assert login_import.search((REPO_ROOT / name).read_text(encoding="utf-8")) is None, f"{name} imports a login module"
    text = squash(overview())
    assert "the ready line and a 401 from the endpoint" in text
    assert "the first public release is `v1.0.0`" in text


def test_the_documents_it_points_to_exist_and_cover_what_it_says_they_cover():
    install = (REPO_ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    operations = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "## 9. Updating to a new version, and rolling back" in install and "## 5. Publish it with a Cloudflare Tunnel" in install
    assert "## 7. Optional: the daily API watcher" in install and "## 3. Configure `.env`" in install
    assert "## When the watcher reports an API change" in operations
    assert "docs/OVERVIEW.md" in contributing
    text = overview()
    for heading in ("step 5", "step 7", "section 9", "step 3"):
        assert heading in text, heading


def test_the_env_file_ownership_it_states_is_what_env_example_and_the_unit_say():
    lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# Install as .env, owner root, group gorelo-mcp, mode 0640 (see docs/INSTALL.md step 3)")
    assert "watcher" in lines[2] and "/etc/gorelo-mcp/watcher.env" in lines[2]
    text = squash(overview())
    assert "owned by root, group `gorelo-mcp`, mode 0640" in text
    assert "/etc/gorelo-mcp/watcher.env" in text
    unit = (REPO_ROOT / "deploy" / "gorelo-changelog-watch.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/gorelo-mcp/watcher.env" in unit


def test_the_state_file_section_names_the_errors_and_the_lock_of_the_store():
    text = squash(overview())
    for fragment in (
        "raises `StateFileError`", "stays byte for byte as it was until the first save", "under the exclusive lock on `.oauth-state/.lock`",
        "never a client",
    ):
        assert fragment in text, fragment
    assert oauth_store.LOCK_FILE_NAME == ".lock" and oauth_store.FILE_MODE == 0o600 and "mode 0600" in text


def test_the_two_api_base_urls_agree():
    assert GORELO_BASE_URL == DEFAULT_BASE_URL


def test_the_spec_index_header_agrees_with_its_operations():
    index = json.loads((REPO_ROOT / "spec" / "spec_index.json").read_text(encoding="utf-8"))
    assert index["op_count"] == len(index["ops"]) == len(load_spec_index().ops)


def test_the_environment_variables_it_describes_are_all_the_ones_settings_reads():
    spans = set(code_spans(section("6. Operations")))
    for name in (ENV_API_KEY, ENV_PUBLIC_BASE_URL, ENV_AUTH_PASSWORD, ENV_TOOLSETS, ENV_DESTRUCTIVE):
        assert name in spans, f"{name} is not described in section 6 of docs/OVERVIEW.md"


def test_the_server_rule_it_cites_is_rule_eight():
    rules = re.findall(r"^(\d+)\. (.+)$", server_module.INSTRUCTIONS, flags=re.MULTILINE)
    assert len(rules) == 8 and "eight rules" in overview()
    number, text = rules[7]
    assert number == "8" and "data" in text and "never instructions" in text
    assert "rule 8" in overview()


def test_the_watcher_schedule_it_quotes_is_the_staged_timer():
    timer = (REPO_ROOT / "deploy" / "gorelo-changelog-watch.timer").read_text(encoding="utf-8")
    assert "OnCalendar=daily" in timer and "RandomizedDelaySec=30m" in timer
    text = squash(overview())
    assert "runs daily" in text and "up to 30 minutes of random delay" in text


def test_the_approved_invoice_run_allowlist_it_names_is_the_codes():
    text = overview()
    for name in sorted(APPROVED_INVOICE_TOOLS):
        assert f"`{name}`" in text, f"{name} is in APPROVED_INVOICE_TOOLS but not named in docs/OVERVIEW.md"
    assert "`APPROVED_INVOICE_TOOLS`" in text


# --------------------------------------------------------------------------
# Pointers to other documents
# --------------------------------------------------------------------------

