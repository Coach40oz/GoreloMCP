"""scripts/site_config.py: the one place the site-specific values of the live harness and the watcher come from."""

from __future__ import annotations

import pytest
from site_helper import SITE_TOML

from scripts import site_config
from scripts.site_config import SiteConfigError, load


def write(tmp_path, text):
    path = tmp_path / "site.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_complete_file_loads(tmp_path):
    cfg = load(write(tmp_path, SITE_TOML))
    assert (cfg.test_client, cfg.second_client, cfg.operator_contact, cfg.operator_user) == (9501, 9502, 9600, 9700)
    assert cfg.operator_email == "ops@example.com" and site_config.watcher_alert_client(write(tmp_path, SITE_TOML)) == 9503
    assert (cfg.test_client_name, cfg.second_client_name) == ("Sandbox Alpha", "Sandbox Beta")
    assert cfg.leftover_clients == {9801, 9802} and cfg.leftover_contacts == {9900}
    assert cfg.probe_url == "https://mcp.example.net/.well-known/oauth-authorization-server"


def test_the_example_file_documents_every_key_and_is_refused():
    example = site_config.REPO_ROOT / "site.example.toml"
    text = example.read_text(encoding="utf-8")
    for section, key in site_config.KEYS:
        assert f"[{section}]" in text and f"\n{key} " in text
    with pytest.raises(SiteConfigError, match=r"example file.*site\.example\.toml"):
        load(example)
    with pytest.raises(SiteConfigError, match="example file"):
        site_config.watcher_alert_client(example)


def test_a_copy_of_the_example_under_another_name_is_refused_on_its_placeholders(tmp_path):
    copy = tmp_path / "site.local.toml"
    copy.write_text((site_config.REPO_ROOT / "site.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(SiteConfigError, match=r"\[clients\] test_client still holds the placeholder of site\.example\.toml"):
        load(copy)
    with pytest.raises(SiteConfigError, match=r"\[watcher\] alert_client_id still holds the placeholder"):
        site_config.watcher_alert_client(copy)


@pytest.mark.parametrize(
    "old, new, key",
    [("test_client = 9501", "test_client = 999999001", "test_client"),
     ('second_client_name = "Sandbox Beta"', 'second_client_name = "Example Second Client"', "second_client_name"),
     ('email = "ops@example.com"', 'email = "you@example.com"', "email"),
     ('public_host = "mcp.example.net"', 'public_host = "mcp.example.com"', "public_host")],
)
def test_one_placeholder_value_is_enough_to_refuse(tmp_path, old, new, key):
    with pytest.raises(SiteConfigError, match=rf"{key} still holds the placeholder"):
        load(write(tmp_path, SITE_TOML.replace(old, new)))


def test_empty_leftover_lists_are_not_placeholders(tmp_path):
    cfg = load(write(tmp_path, SITE_TOML.replace("[9801, 9802]", "[]").replace("[9900]", "[]")))
    assert cfg.leftover_clients == frozenset() and cfg.leftover_contacts == frozenset()


def test_the_leftover_name_rules_are_optional(tmp_path):
    cfg = load(write(tmp_path, SITE_TOML))
    assert cfg.leftover_client_name_contains == ("OLDTEST",)
    assert (cfg.leftover_contact_first_name, cfg.leftover_contact_last_name_prefix) == ("Sample", "Leftover-")
    bare = "\n".join(line for line in SITE_TOML.splitlines() if not line.startswith(("client_name_contains", "contact_first_name", "contact_last_name_prefix")))
    cfg = load(write(tmp_path, bare))
    assert cfg.leftover_client_name_contains == () and cfg.leftover_contact_first_name is None and cfg.leftover_contact_last_name_prefix is None


@pytest.mark.parametrize(
    "old, new, key",
    [('client_name_contains = ["OLDTEST"]', 'client_name_contains = "OLDTEST"', "client_name_contains"),
     ('client_name_contains = ["OLDTEST"]', 'client_name_contains = [""]', "client_name_contains"),
     ('contact_first_name = "Sample"', "contact_first_name = 5", "contact_first_name"),
     ('contact_last_name_prefix = "Leftover-"', 'contact_last_name_prefix = ""', "contact_last_name_prefix")],
)
def test_a_wrong_leftover_name_rule_is_refused(tmp_path, old, new, key):
    with pytest.raises(SiteConfigError, match=rf"{key} must be"):
        load(write(tmp_path, SITE_TOML.replace(old, new)))


@pytest.mark.parametrize("key", ["client_name_contains", "contact_first_name", "contact_last_name_prefix"])
def test_the_example_values_of_the_name_rules_are_refused(tmp_path, key):
    example = (site_config.REPO_ROOT / "site.example.toml").read_text(encoding="utf-8")
    line = next(l.split("#")[0].strip() for l in example.splitlines() if l.startswith(key + " "))
    text = "\n".join(line if l.startswith(key + " ") else l for l in SITE_TOML.splitlines())
    with pytest.raises(SiteConfigError, match=rf"{key} still holds the placeholder"):
        load(write(tmp_path, text))


def test_the_watcher_needs_only_its_own_key(tmp_path):
    only = write(tmp_path, "[watcher]\nalert_client_id = 9503\n")
    assert site_config.watcher_alert_client(only) == 9503
    with pytest.raises(SiteConfigError, match=r"missing key \[clients\] test_client.*site\.example\.toml"):
        load(only)


def test_the_harness_does_not_need_the_watcher_key(tmp_path, monkeypatch):
    from site_helper import drop_key

    assert load(drop_key(monkeypatch, tmp_path, "watcher", "alert_client_id")).test_client == 9501


def test_the_watcher_names_a_missing_alert_key_and_the_example(tmp_path):
    with pytest.raises(SiteConfigError, match=r"missing key \[watcher\] alert_client_id.*site\.example\.toml"):
        site_config.watcher_alert_client(write(tmp_path, "[clients]\ntest_client = 9501\n"))


def test_a_missing_file_is_a_clear_refusal_naming_the_example(tmp_path):
    with pytest.raises(SiteConfigError, match=r"file not found.*site\.example\.toml"):
        load(tmp_path / "nope.toml")


@pytest.mark.parametrize("section, key", list(site_config.HARNESS_KEYS))
def test_every_key_is_required(tmp_path, monkeypatch, section, key):
    from site_helper import drop_key

    path = drop_key(monkeypatch, tmp_path, section, key)
    with pytest.raises(SiteConfigError, match=rf"missing key \[{section}\] {key}"):
        load(path)


@pytest.mark.parametrize(
    "old, new, key",
    [("test_client = 9501", 'test_client = "9501"', "test_client"), ("user_id = 9700", "user_id = true", "user_id"),
     ("client_ids = [9801, 9802]", "client_ids = [0]", "client_ids"), ('email = "ops@example.com"', 'email = ""', "email")],
)
def test_a_value_of_the_wrong_type_is_refused(tmp_path, old, new, key):
    with pytest.raises(SiteConfigError, match=rf"{key} must be"):
        load(write(tmp_path, SITE_TOML.replace(old, new)))


def test_invalid_toml_is_refused(tmp_path):
    with pytest.raises(SiteConfigError, match="not valid TOML"):
        load(write(tmp_path, "[clients\n"))


def test_the_environment_variable_picks_the_file_and_the_default_is_the_repo_file(monkeypatch, tmp_path):
    monkeypatch.setenv(site_config.ENV_VAR, str(tmp_path / "x.toml"))
    assert site_config.config_path() == tmp_path / "x.toml"
    monkeypatch.delenv(site_config.ENV_VAR)
    assert site_config.config_path() == site_config.REPO_ROOT / "site.local.toml"


def test_the_lazy_proxy_does_not_read_the_file_at_import(monkeypatch, tmp_path):
    monkeypatch.setenv(site_config.ENV_VAR, str(tmp_path / "missing.toml"))
    with pytest.raises(SiteConfigError):
        site_config.SITE.test_client


def test_a_real_looking_id_such_as_9002_is_not_a_placeholder(tmp_path):
    assert load(write(tmp_path, SITE_TOML.replace("test_client = 9501", "test_client = 9001"))).test_client == 9001


def test_the_email_area_status_defaults_to_in_progress_and_can_be_set(tmp_path):
    assert load(write(tmp_path, SITE_TOML)).updated_status == "In Progress"
    text = SITE_TOML + '\n[harness]\nupdated_status = "Investigating"\n'
    assert load(write(tmp_path, text)).updated_status == "Investigating"
