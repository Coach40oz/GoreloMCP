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


def test_main_unit_may_write_only_the_two_state_directories():
    s = settings(MAIN)
    assert s["ReadWritePaths"] == ["/opt/gorelo-mcp/app/.oauth-state /opt/gorelo-mcp/app/.state"]
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


def test_watcher_env_example_holds_only_the_key_the_script_reads():
    lines = [x for x in (DEPLOY / "watcher.env.example").read_text(encoding="utf-8").splitlines() if x and not x.startswith("#")]
    assert [x.split("=")[0] for x in lines] == ["GORELO_API_KEY"]
    script = (REPO_ROOT / "scripts" / "watch_gorelo_changelog.py").read_text(encoding="utf-8")
    assert re.findall(r"""environ(?:\.get)?[\[(]\s*["'](\w+)""", script) == []  # it reads the key only through API_KEY_ENV
    assert 'API_KEY_ENV = "GORELO_API_KEY"' in script


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
