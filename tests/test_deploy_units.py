"""Lint of the systemd units in deploy/ and of the dependency pins. Offline: it only reads files."""

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = REPO_ROOT / "deploy"
VENV_PYTHON = "/opt/gorelo-mcp/app/.venv/bin/python"
APP_ENV = "/opt/gorelo-mcp/app/.env"

MAIN = DEPLOY / "gorelo-mcp.service"
WATCH = DEPLOY / "gorelo-changelog-watch.service"
DROPINS = [DEPLOY / "gorelo-mcp.service.d" / "10-sandbox.conf", DEPLOY / "gorelo-changelog-watch.service.d" / "10-sandbox.conf"]
SANDBOX_KEYS = {
    "PrivateDevices", "ProtectKernelLogs", "ProtectClock", "ProtectHostname", "ProtectProc", "ProcSubset",
    "RestrictAddressFamilies", "RestrictNamespaces", "RestrictRealtime", "SystemCallArchitectures", "SystemCallFilter",
    "SystemCallErrorNumber", "CapabilityBoundingSet", "AmbientCapabilities", "RemoveIPC", "MemoryDenyWriteExecute",
}


def settings(path: Path) -> dict[str, list[str]]:
    """The key=value lines of a unit file (comments and section headers skipped), values listed in order."""
    found: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[")):
            continue
        key, _, value = line.partition("=")
        found.setdefault(key, []).append(value)
    return found


@pytest.mark.parametrize("unit", [MAIN, WATCH], ids=["main", "watcher"])
def test_units_run_the_venv_python_directly_with_a_private_umask(unit):
    s = settings(unit)
    assert len(s["ExecStart"]) == 1
    assert s["ExecStart"][0].split()[0] == VENV_PYTHON
    assert not re.search(r"\buv\b", s["ExecStart"][0])
    assert s["UMask"] == ["0077"]
    assert s["NoNewPrivileges"] == ["true"] and s["ProtectSystem"] == ["strict"] and s["ProtectHome"] == ["true"]


def test_main_unit_may_write_only_the_oauth_state_directory():
    s = settings(MAIN)
    # .state is the API watcher's directory: no server code writes there, and a writable .state would let a hijacked service
    # process plant files (symbolic links) for a watcher that may be run as root
    assert s["ReadWritePaths"] == ["/opt/gorelo-mcp/app/.oauth-state"]
    assert s["ExecStart"] == [f"{VENV_PYTHON} main.py"]
    assert s["EnvironmentFile"] == [APP_ENV]
    assert s["Environment"] == ["PYTHONDONTWRITEBYTECODE=1"]


def test_watcher_unit_gets_only_the_watcher_env_file_and_writes_only_state():
    s = settings(WATCH)
    assert s["EnvironmentFile"] == ["/etc/gorelo-mcp/watcher.env"]
    assert s["ReadWritePaths"] == ["/opt/gorelo-mcp/app/.state"]
    text = WATCH.read_text(encoding="utf-8")
    assert not any(line.startswith("EnvironmentFile") and APP_ENV in line for line in text.splitlines())
    assert s["ExecStart"] == [f"{VENV_PYTHON} scripts/watch_gorelo_changelog.py --quiet"]


def test_watcher_env_example_holds_exactly_the_variables_the_script_reads():
    """The names come from the script's own constants (it reads the environment through them, `env.get(API_KEY_ENV)`), so
    the example must list each one, the optional ones commented out, and nothing else; the unit's comment names them too."""
    script = (REPO_ROOT / "scripts" / "watch_gorelo_changelog.py").read_text(encoding="utf-8")
    read = set(re.findall(r'^(\w+_ENV) = "(\w+)"$', script, re.M))
    assert {name for const, name in read} == {"GORELO_API_KEY", "GORELO_BASE_URL"}, read
    for const, name in read:  # each constant is the only way the script names a variable: used with env.get / environ
        assert len(re.findall(rf"\b{const}\b", script)) >= 2, const
    # the script reads nothing from the environment by a literal name or in another way than through those constants
    assert re.findall(r"""(?:environ|getenv|env\.get|env\[)\(?\s*["'](\w+)""", script) == []
    assert re.findall(r"os\.getenv|os\.environ\b", script) == ["os.environ"]  # only `env = os.environ if env is None ...`
    example = (DEPLOY / "watcher.env.example").read_text(encoding="utf-8").splitlines()
    names = {m.group(1) for line in example if (m := re.match(r"#?\s*([A-Z][A-Z0-9_]*)=", line))}
    assert names == {name for const, name in read}
    active = [x.split("=")[0] for x in example if x and not x.startswith("#")]
    assert active == ["GORELO_API_KEY"]  # the key is set; GORELO_BASE_URL is the commented, optional line
    assert any(line.startswith("# GORELO_BASE_URL=") for line in example)
    unit = WATCH.read_text(encoding="utf-8")
    assert "GORELO_API_KEY" in unit and "GORELO_BASE_URL" in unit  # the unit's comment names both


@pytest.mark.parametrize("dropin", DROPINS, ids=["main", "watcher"])
def test_sandbox_dropins_have_one_setting_per_line_and_a_comment_before_each(dropin):
    lines = dropin.read_text(encoding="utf-8").splitlines()
    keys = set()
    for number, line in enumerate(lines):
        if line.startswith(("#", "[")) or not line:
            continue
        assert re.fullmatch(r"\w+=[^=]*", line), line
        assert lines[number - 1].startswith("#"), f"no comment above {line}"
        keys.add(line.split("=")[0])
    assert keys == SANDBOX_KEYS
    assert "CapabilityBoundingSet=" in lines and "AmbientCapabilities=" in lines


def test_dependencies_are_pinned_with_double_equals():
    deps = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    assert deps, "no dependencies"
    for dep in deps:
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+(\[[a-z,]+\])?==\d+(\.\d+)*", dep), dep
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    for dep in deps:
        name, version = re.match(r"([A-Za-z0-9_.\-]+)", dep).group(1), dep.split("==")[1]
        assert re.search(rf'^name = "{re.escape(name)}"\nversion = "{re.escape(version)}"$', lock, re.M), dep


def test_the_locked_certifi_no_longer_trusts_the_compromised_emsign_ecc_root_g3():
    """GHSA-6q48-6c7w-9gw2: certifi before 2026.10.19 still trusts the emSign ECC Root CA - G3, whose intermediates' keys were
    compromised. httpx takes its trust store from certifi, and the Gorelo API key travels over that connection."""
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    match = re.search(r'^name = "certifi"\nversion = "([\d.]+)"$', lock, re.M)
    assert match, "certifi is not in the lock"
    assert tuple(int(x) for x in match.group(1).split(".")) >= (2026, 10, 19), match.group(1)
    import certifi

    assert "emSign ECC Root CA - G3" not in Path(certifi.where()).read_text(encoding="utf-8")
