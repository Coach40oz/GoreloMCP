"""settings.py: environment parsing, defaults, and error messages that name every problem."""

import dataclasses

import pytest

import settings as settings_module
from settings import DEFAULT_TOOLSETS, TOOLSETS, Settings, SettingsError
from tools._common import REGISTRY

GOOD = {
    "GORELO_API_KEY": "key-123",
    "PUBLIC_BASE_URL": "https://mcp.example.test",
    "MCP_AUTH_PASSWORD": "pw-123",
}


def env(**extra):
    return {**GOOD, **extra}


def test_constants():
    assert TOOLSETS == ("core", "tickets", "time", "billing", "uptime", "projects", "forms")
    assert DEFAULT_TOOLSETS == ("core", "tickets", "time", "billing", "uptime")


def test_minimal_valid_environment_uses_defaults():
    settings = Settings.from_env(GOOD)
    assert settings.api_key == "key-123"
    assert settings.base_url == "https://api.usw.gorelo.io/v1"
    assert settings.public_base_url == "https://mcp.example.test"
    assert settings.mcp_auth_password == "pw-123"
    assert settings.toolsets == frozenset(DEFAULT_TOOLSETS)
    assert isinstance(settings.toolsets, frozenset)
    assert settings.destructive is False


def test_settings_is_frozen():
    settings = Settings.from_env(GOOD)
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.destructive = True  # type: ignore[misc]


def test_field_order_and_defaults_of_the_dataclass():
    names = [f.name for f in dataclasses.fields(Settings)]
    assert names == ["api_key", "base_url", "public_base_url", "mcp_auth_password", "toolsets", "destructive"]
    direct = Settings(api_key="k")
    assert direct.public_base_url is None and direct.mcp_auth_password is None
    assert direct.toolsets == frozenset(DEFAULT_TOOLSETS) and direct.destructive is False


def test_repr_never_shows_the_api_key_or_the_password():
    text = repr(Settings.from_env(env(GORELO_API_KEY="SECRET-KEY-XYZ", MCP_AUTH_PASSWORD="SECRET-PW-XYZ")))
    assert "SECRET-KEY-XYZ" not in text and "SECRET-PW-XYZ" not in text


def test_missing_variables_are_all_named_in_one_error():
    with pytest.raises(SettingsError) as info:
        Settings.from_env({})
    message = str(info.value)
    for name in ("GORELO_API_KEY", "PUBLIC_BASE_URL", "MCP_AUTH_PASSWORD"):
        assert name in message


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_values_count_as_missing(blank):
    with pytest.raises(SettingsError) as info:
        Settings.from_env({"GORELO_API_KEY": blank, "PUBLIC_BASE_URL": blank, "MCP_AUTH_PASSWORD": blank})
    for name in ("GORELO_API_KEY", "PUBLIC_BASE_URL", "MCP_AUTH_PASSWORD"):
        assert name in str(info.value)


def test_only_the_missing_variable_is_named():
    with pytest.raises(SettingsError) as info:
        Settings.from_env({"GORELO_API_KEY": "k", "PUBLIC_BASE_URL": "https://x.test"})
    assert "MCP_AUTH_PASSWORD" in str(info.value)
    assert "GORELO_API_KEY" not in str(info.value)
    assert "PUBLIC_BASE_URL" not in str(info.value)


def test_http_variables_are_optional_without_require_http():
    settings = Settings.from_env({"GORELO_API_KEY": "k"}, require_http=False)
    assert settings.api_key == "k"
    assert settings.public_base_url is None and settings.mcp_auth_password is None


def test_the_api_key_is_always_required():
    with pytest.raises(SettingsError, match="GORELO_API_KEY"):
        Settings.from_env({}, require_http=False)


def test_http_variables_are_read_when_present_even_if_not_required():
    settings = Settings.from_env(GOOD, require_http=False)
    assert settings.public_base_url == "https://mcp.example.test"
    assert settings.mcp_auth_password == "pw-123"


def test_the_password_is_kept_exactly_but_the_key_and_url_are_trimmed():
    settings = Settings.from_env(
        {"GORELO_API_KEY": "  key  ", "PUBLIC_BASE_URL": " https://x.test ", "MCP_AUTH_PASSWORD": " pw with spaces "}
    )
    assert settings.api_key == "key"
    assert settings.public_base_url == "https://x.test"
    assert settings.mcp_auth_password == " pw with spaces "


@pytest.mark.parametrize("raw", [None, "", "   ", " , ,"])
def test_unset_or_blank_toolsets_mean_the_default(raw):
    extra = {} if raw is None else {"GORELO_TOOLSETS": raw}
    assert Settings.from_env(env(**extra)).toolsets == frozenset(DEFAULT_TOOLSETS)


def test_toolsets_are_case_insensitive_and_trimmed():
    settings = Settings.from_env(env(GORELO_TOOLSETS=" Core , TICKETS,time "))
    assert settings.toolsets == frozenset({"core", "tickets", "time"})


def test_all_means_every_toolset():
    assert Settings.from_env(env(GORELO_TOOLSETS="all")).toolsets == frozenset(TOOLSETS)
    assert Settings.from_env(env(GORELO_TOOLSETS="ALL")).toolsets == frozenset(TOOLSETS)


def test_projects_and_forms_are_opt_in():
    settings = Settings.from_env(env(GORELO_TOOLSETS="core,projects,forms"))
    assert settings.toolsets == frozenset({"core", "projects", "forms"})
    assert "projects" not in Settings.from_env(GOOD).toolsets


def test_unknown_toolsets_are_named_and_the_valid_ones_listed():
    with pytest.raises(SettingsError) as info:
        Settings.from_env(env(GORELO_TOOLSETS="core,ticketz,Bogus"))
    message = str(info.value)
    assert "'ticketz'" in message and "'Bogus'" in message  # as the operator typed them
    for valid in TOOLSETS:
        assert valid in message


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", " on "])
def test_destructive_truthy_words(raw):
    assert Settings.from_env(env(GORELO_ENABLE_DESTRUCTIVE=raw)).destructive is True


@pytest.mark.parametrize("raw", ["0", "false", "No", "OFF", "", "  "])
def test_destructive_falsy_words_and_blank(raw):
    assert Settings.from_env(env(GORELO_ENABLE_DESTRUCTIVE=raw)).destructive is False


def test_the_module_docstring_says_everything_the_destructive_flag_registers():
    # the flag also registers create_approved_invoice (it approves an invoice, which pushes it to accounting and
    # may email its recipients), so the text that describes the flag must name it next to the delete and void tools
    doc = " ".join(settings_module.__doc__.split())
    flag = doc[doc.index("GORELO_ENABLE_DESTRUCTIVE optional.") :]
    gated = [spec.name for spec in REGISTRY.specs if spec.kind == "destructive"]
    assert "create_approved_invoice" in gated
    for name in (name for name in gated if not name.startswith("delete_")):
        assert name in flag, name
    for word in ("delete", "void", "accounting system", "email", "confirm=true"):
        assert word in flag, word
    assert "enables delete and void tools" not in doc  # the text from before create_approved_invoice existed


def test_destructive_unset_is_false():
    assert Settings.from_env(GOOD).destructive is False


@pytest.mark.parametrize("raw", ["2", "enabled", "y", "truee", "-1"])
def test_destructive_anything_else_is_an_error(raw):
    with pytest.raises(SettingsError, match="GORELO_ENABLE_DESTRUCTIVE"):
        Settings.from_env(env(GORELO_ENABLE_DESTRUCTIVE=raw))


def test_every_problem_is_reported_together():
    with pytest.raises(SettingsError) as info:
        Settings.from_env({"GORELO_TOOLSETS": "nope", "GORELO_ENABLE_DESTRUCTIVE": "maybe"})
    message = str(info.value)
    for fragment in ("GORELO_API_KEY", "PUBLIC_BASE_URL", "MCP_AUTH_PASSWORD", "nope", "GORELO_ENABLE_DESTRUCTIVE"):
        assert fragment in message


def test_from_env_reads_a_plain_mapping_and_ignores_unrelated_variables():
    settings = Settings.from_env(env(HOME="/root", PATH="/bin"))
    assert settings.api_key == "key-123"
