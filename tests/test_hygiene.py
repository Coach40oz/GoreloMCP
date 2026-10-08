"""Repository hygiene: no em or en dashes in what we write, lockfiles untouched, legacy code gone."""

import hashlib
import re
from pathlib import Path

import pytest

import gorelo_client

REPO_ROOT = Path(__file__).resolve().parent.parent
# Spelled with chr() so this file never contains the characters it forbids.
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

# sha256 of the dependency files of the released lockfile, taken with
#     sha256sum pyproject.toml uv.lock
# They are pinned here so the guard needs no git (a checkout without tags, a release tree and a sandbox without git all still
# check). If a dependency change is made on purpose, replace them in the same commit.
PINNED_SHA256 = {
    "pyproject.toml": "66fc82ab2b4ea1767551a50facfd4b89b471e44010ec7060ac70b4f138c3f533",
    "uv.lock": "14a40580beb13556c45b528c66aeea01241ee9c03701688c730389d42266bcac",
}


# The operator-facing files: the README, the example environment file, the changelog watcher, the catalog generator and the
# catalog it writes. Besides these, the scan covers deploy/*, the guides under docs/ and the notes under docs/why/.
OPERATOR_FILES = (
    "README.md",
    ".env.example",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "scripts/watch_gorelo_changelog.py",
    "scripts/gen_tool_catalog.py",
    "docs/tool-catalog.md",
)


# The living docs: the guides and the overview. They are rewritten with every release, so they are scanned too.
LIVING_DOCS = (
    "docs/OVERVIEW.md",
    "docs/INSTALL.md",
    "docs/OPERATIONS.md",
    "docs/TROUBLESHOOTING.md",
    "docs/API-OBSERVED-BEHAVIOR.md",
)


# The login code: the provider and the three modules around it, and the admin script for the OAuth state file. They are
# scanned like everything else, nothing is excluded. The first three are root modules (the glob below finds them), the script is
# under scripts/ and is named here so that a rename or a deletion fails the expected-list test.
AUTH_FILES = (
    "personal_auth.py",
    "oauth_store.py",
    "oauth_guard.py",
    "scripts/oauth_state.py",
)


def checked_files() -> list[Path]:
    files = list(REPO_ROOT.glob("*.py"))
    for pattern in (
        "tools/**/*.py",
        "tests/**/*.py",
        "scripts/spec_*.py",
        "scripts/oauth_state.py",
        "scripts/live/**/*.py",
        "spec/*.json",
        "deploy/*",
        "docs/why/*.txt",
    ):
        files.extend(REPO_ROOT.glob(pattern))
    files.extend(REPO_ROOT / name for name in OPERATOR_FILES)
    files.extend(REPO_ROOT / name for name in LIVING_DOCS)
    return sorted({p for p in files if p.is_file() and "__pycache__" not in p.parts})


def test_the_file_list_covers_the_public_files():
    names = {p.relative_to(REPO_ROOT).as_posix() for p in checked_files()}
    for expected in (
        "gorelo_client.py", "main.py", "server.py", "settings.py", "spec.py",
        "tools/__init__.py", "tools/_common.py", "tools/meta.py", "tools/tickets.py",
        "tests/conftest.py", "tests/test_hygiene.py", "tests/test_spec_index.py",
        "scripts/spec_snapshot.py", "scripts/spec_diff.py", "spec/spec_index.json",
        # the operator-facing files
        "README.md", ".env.example", "SECURITY.md", "CONTRIBUTING.md", "scripts/watch_gorelo_changelog.py",
        "scripts/gen_tool_catalog.py", "docs/tool-catalog.md",
        "deploy/gorelo-changelog-watch.service", "deploy/gorelo-changelog-watch.timer", "deploy/gorelo-mcp.service",
        "docs/why/README.txt", "docs/why/08-server-hardening.txt",
        # the living docs
        *LIVING_DOCS,
        # the login code
        *AUTH_FILES,
        "tests/legacy/personal_auth_previous.py",
    ):
        assert expected in names, expected
    assert len(names) >= 30


def test_every_why_note_is_scanned_for_dashes():
    names = {p.relative_to(REPO_ROOT).as_posix() for p in checked_files()}
    notes = {p.relative_to(REPO_ROOT).as_posix() for p in (REPO_ROOT / "docs" / "why").glob("*.txt")}
    assert len(notes) >= 10 and notes <= names


def test_the_login_code_is_scanned_for_dashes():
    names = {p.relative_to(REPO_ROOT).as_posix() for p in checked_files()}
    assert set(AUTH_FILES) <= names
    for name in AUTH_FILES:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert EM_DASH not in text and EN_DASH not in text, name
    # every module of the repository root is scanned, so a new one cannot slip past
    assert {p.name for p in REPO_ROOT.glob("*.py")} <= {Path(n).name for n in names if "/" not in n}


def test_no_em_or_en_dashes_in_code_docs_and_the_spec_index():
    offenders = []
    for path in checked_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if EM_DASH in line or EN_DASH in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert offenders == [], "em or en dash found at: " + ", ".join(offenders)


def test_the_pinned_digests_are_well_formed():
    assert set(PINNED_SHA256) == {"pyproject.toml", "uv.lock"}
    for name, digest in PINNED_SHA256.items():
        assert re.fullmatch(r"[0-9a-f]{64}", digest), name


@pytest.mark.parametrize("name", sorted(PINNED_SHA256))
def test_dependency_files_match_the_digests_of_the_released_lockfile(name):
    # A dependency change should be deliberate: this check reads no git data, so it can never be skipped.
    digest = hashlib.sha256((REPO_ROOT / name).read_bytes()).hexdigest()
    assert digest == PINNED_SHA256[name], f"{name} differs from the released lockfile: change dependencies only on purpose, and update the digests"


def test_the_obsolete_scripts_are_gone():
    # Scripts that imported the old entry point were removed and must not come back.
    assert not sorted(p.name for p in (REPO_ROOT / "scripts").glob("test_*.py"))


def test_the_legacy_client_helpers_are_gone():
    for name in ("merge_update_body", "unwrap_list", "_build_body", "CONTACT_REQUIRED_DEFAULTS", "MAX_PAGE_SIZE", "AUTO_PAGE_LIMIT"):
        assert not hasattr(gorelo_client, name), name
    source = (REPO_ROOT / "gorelo_client.py").read_text(encoding="utf-8")
    assert "Bearer" not in source and '"Authorization"' not in source  # X-API-Key is the only auth header


def test_the_dev_tree_has_no_oauth_state():
    # Nothing in a source checkout may run main.py: that would create the service's state directory here.
    assert not (REPO_ROOT / ".oauth-state").exists()
