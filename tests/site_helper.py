"""A neutral site config for the live harness and watcher tests (invented ids and example hosts, no real tenant).

A test module does `from site_helper import site_config_env  # noqa: F401`; the autouse fixture then points
GORELO_SITE_CONFIG at a temporary copy of SITE_TOML for every test of that module. The same fixture replaces the live
check of the client names (scripts.live.guard.verify_site_clients) with a no-op that counts its calls in
`CLIENT_CHECKS`, so a harness test sends no extra read; tests of the check itself use `REAL_VERIFY`.
"""

from __future__ import annotations

import pytest

from scripts.live import guard as live_guard

REAL_VERIFY = live_guard.verify_site_clients
CLIENT_CHECKS: list[str] = []

SITE_TOML = """\
[clients]
test_client = 9501
test_client_name = "Sandbox Alpha"
second_client = 9502
second_client_name = "Sandbox Beta"

[operator]
contact_id = 9600
user_id = 9700
email = "ops@example.com"

[leftovers]
client_ids = [9801, 9802]
contact_ids = [9900]
client_name_contains = ["OLDTEST"]
contact_first_name = "Sample"
contact_last_name_prefix = "Leftover-"

[hosts]
public_host = "mcp.example.net"
probe_domain = "example.net"

[watcher]
alert_client_id = 9503
"""


@pytest.fixture(autouse=True)
def site_config_env(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.getbasetemp() / "site.test.toml"
    if not path.exists():
        path.write_text(SITE_TOML, encoding="utf-8")
    monkeypatch.setenv("GORELO_SITE_CONFIG", str(path))

    async def no_check(api_key, *, transport=None, cfg=None):
        CLIENT_CHECKS.append("checked")

    monkeypatch.setattr(live_guard, "verify_site_clients", no_check)
    CLIENT_CHECKS.clear()
    return path


def drop_key(monkeypatch, tmp_path, section: str, key: str):
    """Point GORELO_SITE_CONFIG at a copy of SITE_TOML without `key` in `section`; returns the path."""
    out, current = [], None
    for line in SITE_TOML.splitlines():
        if line.startswith("["):
            current = line.strip("[]")
        if current == section and line.startswith(key + " "):
            continue
        out.append(line)
    path = tmp_path / "site.partial.toml"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    monkeypatch.setenv("GORELO_SITE_CONFIG", str(path))
    return path
