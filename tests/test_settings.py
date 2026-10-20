"""settings.py: environment parsing, defaults, and error messages that name every problem."""

import dataclasses

import pytest

import gorelo_client
import oauth_guard
import personal_auth
import settings as settings_module
from settings import (
    API_DOMAIN,
    BUILTIN_REDIRECT_DOMAINS,
    DEFAULT_BASE_URL,
    DEFAULT_TOOLSETS,
    ENV_BASE_URL,
    TOOLSETS,
    Settings,
    SettingsError,
    parse_base_url,
    parse_extra_redirect_domains,
)
from tools._common import REGISTRY

GOOD = {
    "GORELO_API_KEY": "key-123",
    "PUBLIC_BASE_URL": "https://mcp.example.test",
    "MCP_AUTH_PASSWORD": "pw-123",
}


def env(**extra):
    return {**GOOD, **extra}


def test_constants():
    assert TOOLSETS == ("core", "tickets", "time", "billing", "uptime", "projects", "forms", "documents")
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
    assert settings.extra_redirect_domains == () and settings.redirect_domains == ["claude.ai", "claude.com", "localhost"]
    assert settings.api_host == "api.usw.gorelo.io"


def test_settings_is_frozen():
    settings = Settings.from_env(GOOD)
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.destructive = True  # type: ignore[misc]


def test_field_order_and_defaults_of_the_dataclass():
    names = [f.name for f in dataclasses.fields(Settings)]
    assert names == ["api_key", "base_url", "public_base_url", "mcp_auth_password", "toolsets", "destructive", "extra_redirect_domains", "destructive_tools"]
    direct = Settings(api_key="k")
    assert direct.public_base_url is None and direct.mcp_auth_password is None
    assert direct.toolsets == frozenset(DEFAULT_TOOLSETS) and direct.destructive is False
    assert direct.destructive_tools is None  # None: every gated tool, once destructive is true
    assert direct.extra_redirect_domains == ()


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
    # the list form is described next to the words: what it is, that it registers exactly the names, who checks them
    for fragment in ("comma list of gated tool names", "delete_time_entry,delete_item", "exactly those", "server.resolve_gated_tools"):
        assert fragment in flag, fragment


def test_destructive_unset_is_false():
    assert Settings.from_env(GOOD).destructive is False


@pytest.mark.parametrize(
    "raw",
    ["1,delete_item", "delete_item,on", "yes,no", "off,delete_item", "delete_item delete_time_entry", "delete_item;delete_time_entry", "a b", "$x"],
    ids=["word first", "word last", "two words", "off and a name", "space list", "semicolon list", "two words in one item", "symbol"],
)
def test_destructive_a_word_mixed_with_names_or_an_item_that_cannot_be_a_tool_name_is_an_error(raw):
    with pytest.raises(SettingsError, match="GORELO_ENABLE_DESTRUCTIVE"):
        Settings.from_env(env(GORELO_ENABLE_DESTRUCTIVE=raw))


def test_every_problem_is_reported_together():
    with pytest.raises(SettingsError) as info:
        Settings.from_env({"GORELO_TOOLSETS": "nope", "GORELO_ENABLE_DESTRUCTIVE": "1,delete_item"})
    message = str(info.value)
    for fragment in ("GORELO_API_KEY", "PUBLIC_BASE_URL", "MCP_AUTH_PASSWORD", "nope", "GORELO_ENABLE_DESTRUCTIVE"):
        assert fragment in message


def test_from_env_reads_a_plain_mapping_and_ignores_unrelated_variables():
    settings = Settings.from_env(env(HOME="/root", PATH="/bin"))
    assert settings.api_key == "key-123"


# --------------------------------------------------------------------------
# GORELO_BASE_URL: the host of the region the Gorelo account is hosted in
# --------------------------------------------------------------------------

US = "https://api.usw.gorelo.io/v1"
AU = "https://api.aue.gorelo.io/v1"


def test_the_default_base_url_is_the_client_default_and_the_variable_is_named():
    assert DEFAULT_BASE_URL == gorelo_client.GORELO_BASE_URL == US
    assert (ENV_BASE_URL, API_DOMAIN) == ("GORELO_BASE_URL", "gorelo.io")


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_an_unset_or_blank_base_url_is_the_us_default(raw):
    extra = {} if raw is None else {"GORELO_BASE_URL": raw}
    assert Settings.from_env(env(**extra)).base_url == US
    assert parse_base_url(raw) == US


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://api.usw.gorelo.io", US), ("https://api.usw.gorelo.io/", US), ("https://api.usw.gorelo.io/v1", US),
        ("https://api.usw.gorelo.io/v1/", US),
        ("https://api.aue.gorelo.io", AU), ("https://api.aue.gorelo.io/", AU), ("https://api.aue.gorelo.io/v1", AU),
        ("https://api.aue.gorelo.io/v1/", AU),
        ("  https://API.AUE.Gorelo.io/v1/  ", AU), ("HTTPS://api.aue.gorelo.io", AU),
        ("https://gorelo.io", "https://gorelo.io/v1"),  # the exact rule: gorelo.io itself or a subdomain of it
    ],
)
def test_the_base_url_is_normalized_to_https_host_slash_v1(raw, expected):
    assert Settings.from_env(env(GORELO_BASE_URL=raw)).base_url == expected
    assert parse_base_url(raw) == expected
    assert parse_base_url(expected) == expected  # normalizing twice changes nothing
    assert Settings.from_env(env(GORELO_BASE_URL=raw), require_http=False).api_host == expected.split("/")[2]


CHARS = "without spaces, control characters, backslashes or non-ASCII characters"
HOST = "must name a host under gorelo.io"
BAD_BASE_URLS = [
    ("http://api.aue.gorelo.io", "must start with https://"),
    ("api.aue.gorelo.io", "must start with https://"),
    ("ftp://api.aue.gorelo.io", "must start with https://"),
    ("https://user:hunter2@api.aue.gorelo.io", "must not contain a user name or password"),
    ("https://api.aue.gorelo.io@evil.example", "must not contain a user name or password"),
    ("https://evil.example@api.aue.gorelo.io", "must not contain a user name or password"),
    ("https://evil.example", HOST),
    ("https://evilgorelo.io", HOST),
    ("https://gorelo.io.evil.example", HOST),
    ("https://api.aue.gorelo.io.evil.example/v1", HOST),
    ("https://api..gorelo.io", HOST),
    ("https://-api.gorelo.io", HOST),
    ("https://api.gorelo.io.", HOST),
    ("https://127.0.0.1", HOST),
    ("https://[::1]", HOST),
    ("https://", HOST),
    ("https:///v1", HOST),
    ("https://api.aue.gorelo.io:8443", "must not contain a port"),
    ("https://api.aue.gorelo.io:443/v1", "must not contain a port"),
    ("https://api.aue.gorelo.io:", "must not contain a port"),
    ("https://api.aue.gorelo.io/v1?x=1", "must not contain a query string"),
    ("https://api.aue.gorelo.io/v1?", "must not contain a query string"),
    ("https://api.aue.gorelo.io/v1#top", "must not contain a fragment"),
    ("https://api.aue.gorelo.io/v2", "must not contain a path other than /v1"),
    ("https://api.aue.gorelo.io/V1", "must not contain a path other than /v1"),
    ("https://api.aue.gorelo.io//v1", "must not contain a path other than /v1"),
    ("https://api.aue.gorelo.io/v1/clients", "must not contain a path other than /v1"),
    ("https://api.aue.gorelo.io/v1;x", "must not contain a path other than /v1"),
    ("https://api.aue .gorelo.io", CHARS),
    ("https://api.aue.gorelo.io\\@evil.example", CHARS),
    ("https://api.aue.gorelo.io\n.evil.example", CHARS),
    ("https://\u00e4.gorelo.io", CHARS),
    ("https://[api.aue.gorelo.io", "is not a valid address"),
]


@pytest.mark.parametrize("raw, fragment", BAD_BASE_URLS)
def test_a_refused_base_url_names_the_variable_and_the_reason_and_never_a_password(raw, fragment):
    with pytest.raises(SettingsError) as info:
        Settings.from_env(env(GORELO_BASE_URL=raw))
    message = str(info.value)
    assert message.startswith("GORELO_BASE_URL ") and fragment in message
    assert "hunter2" not in message and "api.aue.gorelo.io:8443" not in message
    with pytest.raises(SettingsError, match="GORELO_BASE_URL"):
        parse_base_url(raw)


def test_a_refused_foreign_host_is_shown_by_its_host_name_only():
    with pytest.raises(SettingsError) as info:
        parse_base_url("https://evil.example/v1/secret-path?token=abc")
    assert "'evil.example'" in str(info.value) and "secret-path" not in str(info.value) and "token" not in str(info.value)


def test_a_refused_base_url_is_reported_together_with_the_other_problems():
    with pytest.raises(SettingsError) as info:
        Settings.from_env({"GORELO_BASE_URL": "http://evil.example", "GORELO_TOOLSETS": "nope"})
    message = str(info.value)
    for fragment in ("GORELO_API_KEY", "PUBLIC_BASE_URL", "MCP_AUTH_PASSWORD", "GORELO_BASE_URL must start with https://", "nope"):
        assert fragment in message


def test_the_base_url_is_read_without_the_http_variables_too():
    assert Settings.from_env({"GORELO_API_KEY": "k", "GORELO_BASE_URL": "https://api.aue.gorelo.io"}, require_http=False).base_url == AU


# --------------------------------------------------------------------------
# MCP_EXTRA_REDIRECT_DOMAINS: more places an AI client may be sent back to
# --------------------------------------------------------------------------

POWER_PLATFORM = "global.consent.azure-apim.net"


def test_the_builtin_redirect_domains_are_what_the_provider_allows_by_default(tmp_path):
    provider = personal_auth.PersonalAuthProvider(
        base_url="https://mcp.example.test", password="a-test-password-not-real", state_dir=str(tmp_path / "state")
    )
    try:
        assert provider.allowed_redirect_domains == list(BUILTIN_REDIRECT_DOMAINS) == ["claude.ai", "claude.com", "localhost"]
    finally:
        provider.close()


@pytest.mark.parametrize("raw", [None, "", "   ", " , ,"])
def test_unset_or_blank_extra_redirect_domains_add_nothing(raw):
    extra = {} if raw is None else {"MCP_EXTRA_REDIRECT_DOMAINS": raw}
    settings = Settings.from_env(env(**extra))
    assert settings.extra_redirect_domains == () and settings.redirect_domains == ["claude.ai", "claude.com", "localhost"]
    assert parse_extra_redirect_domains(raw) == ()


def test_extra_redirect_domains_are_trimmed_lowercased_deduplicated_and_come_after_the_builtin_ones_in_order():
    raw = f" {POWER_PLATFORM.title()} , example.org,GLOBAL.consent.azure-apim.net,CLAUDE.AI, claude.com,,x1.example.org "
    settings = Settings.from_env(env(MCP_EXTRA_REDIRECT_DOMAINS=raw))
    assert settings.extra_redirect_domains == (POWER_PLATFORM, "example.org", "x1.example.org")
    assert settings.redirect_domains == ["claude.ai", "claude.com", "localhost", POWER_PLATFORM, "example.org", "x1.example.org"]
    settings.redirect_domains.append("evil.example")  # a copy: the setting itself cannot be changed through it
    assert "evil.example" not in settings.redirect_domains


def test_a_subdomain_of_a_builtin_entry_is_kept_as_an_extra():
    assert parse_extra_redirect_domains("mcp.claude.ai, xn--bcher-kva.example") == ("mcp.claude.ai", "xn--bcher-kva.example")


BARE = "must be a bare domain name"
NUMBER_LAST_PART = "(its last part must not be a number: digits, or 0x followed by hex digits, make a URL parser read the host as an IP address)"
BAD_REDIRECT_ENTRIES = [
    ("https://global.consent.azure-apim.net/redirect/x", BARE),
    ("global.consent.azure-apim.net/redirect", BARE),
    ("global.consent.azure-apim.net?x=1", BARE),
    ("global.consent.azure-apim.net#x", BARE),
    ("example.org\\evil.example", BARE),
    ("global.consent.azure-apim.net:443", "must not contain a port or an IP address"),
    ("[::1]", "must not contain a port or an IP address"),
    ("*.azure-apim.net", "must not contain a wildcard"),
    ("localhost", "must not be localhost"),
    ("LOCALHOST", "must not be localhost"),
    ("app.localhost", "must not be localhost"),
    ("com", "must contain at least one dot"),
    ("127.0.0.1", NUMBER_LAST_PART),  # decimal parts, as with the hex row below
    ("10.0.0.1", "not an IP address"),
    ("1.2.3.4", "not an IP address"),
    ("a.b.123", "not an IP address"),
    ("127.0.0.0x1", NUMBER_LAST_PART),  # hex parts: a URL parser reads these as 127.0.0.1
    ("0x7f.0x1", "not an IP address"),
    ("-bad.example.org", "is not a valid DNS name"),
    ("bad-.example.org", "is not a valid DNS name"),
    (".example.org", "is not a valid DNS name"),
    ("example.org.", "is not a valid DNS name"),
    ("a..example.org", "is not a valid DNS name"),
    ("a" * 64 + ".example.org", "is not a valid DNS name"),
    ("ex ample.org", "may contain only letters, digits, hyphens and dots"),
    ("ex\u00e4mple.org", "may contain only letters, digits, hyphens and dots"),
    ("ex_ample.org", "may contain only letters, digits, hyphens and dots"),
]


@pytest.mark.parametrize("entry, fragment", BAD_REDIRECT_ENTRIES)
def test_a_bad_redirect_entry_refuses_to_start_naming_the_variable_and_the_entry(entry, fragment):
    with pytest.raises(SettingsError) as info:
        Settings.from_env(env(MCP_EXTRA_REDIRECT_DOMAINS=f"good.example.org,{entry}"))
    message = str(info.value)
    assert message.startswith("MCP_EXTRA_REDIRECT_DOMAINS entry ") and fragment in message
    lowered = entry.lower()
    assert repr(lowered if len(lowered) <= 60 else lowered[:60] + "...") in message  # shown as a quoted text, cut at 60
    assert "good.example.org" not in message  # a good entry beside a bad one is not blamed (and does not rescue it)


def test_every_bad_entry_is_named_in_one_error():
    with pytest.raises(SettingsError) as info:
        parse_extra_redirect_domains("ok.example.org, localhost, 1.2.3.4, https://x.example.org")
    message = str(info.value)
    assert message.count("MCP_EXTRA_REDIRECT_DOMAINS") == 3
    for shown in ("'localhost'", "'1.2.3.4'", "'https://x.example.org'"):
        assert shown in message
    assert "ok.example.org" not in message


def test_an_entry_with_a_user_name_is_refused_and_never_echoed():
    with pytest.raises(SettingsError) as info:
        parse_extra_redirect_domains("user:hunter2@example.org")
    message = str(info.value)
    assert "hunter2" not in message and "example.org" not in message and "containing '@'" in message


def test_a_long_bad_entry_is_cut_in_the_message():
    entry = "a" * 64 + ".example.org"
    with pytest.raises(SettingsError) as info:
        parse_extra_redirect_domains(entry)
    assert "a" * 60 + "..." in str(info.value) and "a" * 61 not in str(info.value)


def test_redirect_problems_are_reported_with_the_region_and_the_other_problems():
    with pytest.raises(SettingsError) as info:
        Settings.from_env({"GORELO_BASE_URL": "http://x.test", "MCP_EXTRA_REDIRECT_DOMAINS": "localhost", "GORELO_ENABLE_DESTRUCTIVE": "1,delete_item"})
    message = str(info.value)
    for fragment in ("GORELO_API_KEY", "GORELO_BASE_URL must start with https://", "MCP_EXTRA_REDIRECT_DOMAINS entry 'localhost'", "GORELO_ENABLE_DESTRUCTIVE"):
        assert fragment in message


@pytest.mark.parametrize(
    "url, with_extra, by_default",
    [
        ("https://global.consent.azure-apim.net/redirect/my-connector", None, "not_allowed"),
        ("https://GLOBAL.consent.azure-apim.net/redirect/x", None, "not_allowed"),
        ("https://eu.global.consent.azure-apim.net/redirect/x", None, "not_allowed"),  # an entry admits its subdomains
        ("http://global.consent.azure-apim.net/redirect/x", "not_https", "not_https"),
        ("https://azure-apim.net/redirect/x", "not_allowed", "not_allowed"),  # the parent is not admitted
        ("https://consent.azure-apim.net/redirect/x", "not_allowed", "not_allowed"),  # nor a sibling
        ("https://global.consent.azure-apim.net.evil.example/redirect/x", "not_allowed", "not_allowed"),
        ("https://evil.example/redirect/x", "not_allowed", "not_allowed"),
        ("https://user@global.consent.azure-apim.net/x", "userinfo", "userinfo"),
        ("https://claude.ai/api/mcp/auth_callback", None, None),
        ("http://localhost:6274/cb", None, None),
    ],
)
def test_the_extra_domain_admits_the_power_platform_redirect_and_nothing_wider(url, with_extra, by_default):
    extended = Settings.from_env(env(MCP_EXTRA_REDIRECT_DOMAINS=POWER_PLATFORM)).redirect_domains
    assert oauth_guard.redirect_problem(url, extended) == with_extra
    assert oauth_guard.redirect_problem(url, Settings.from_env(GOOD).redirect_domains) == by_default


def test_the_module_docstring_names_the_region_and_redirect_variables():
    doc = " ".join(settings_module.__doc__.split())
    assert "GORELO_BASE_URL optional." in doc and "MCP_EXTRA_REDIRECT_DOMAINS optional." in doc
    assert "api.aue.gorelo.io" in doc and "global.consent.azure-apim.net" in doc
    assert "The start line `gorelo-mcp settings: api_host=...` shows the host in use." in doc


# --------------------------------------------------------------------------
# GORELO_ENABLE_DESTRUCTIVE: the words, or a comma list of gated tool names
# --------------------------------------------------------------------------


def parsed(raw):
    settings = Settings.from_env(env(GORELO_ENABLE_DESTRUCTIVE=raw))
    return settings.destructive, settings.destructive_tools


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", " on ", "On"])
def test_the_true_words_still_switch_on_every_gated_tool_and_name_none(raw):
    assert parsed(raw) == (True, None)  # None: every gated tool of the enabled toolsets


@pytest.mark.parametrize("raw", ["0", "false", "No", "OFF", "", "  ", ",", " , ,"])
def test_the_false_words_blank_and_only_commas_switch_every_gated_tool_off(raw):
    assert parsed(raw) == (False, None)


def test_unset_switches_every_gated_tool_off():
    settings = Settings.from_env(GOOD)
    assert (settings.destructive, settings.destructive_tools) == (False, None)


@pytest.mark.parametrize("raw, switched_on", [("1,", True), (",1", True), (" 1 , ", True), ("off,", False), (",no,", False)])
def test_a_word_with_stray_commas_is_still_the_word(raw, switched_on):
    assert parsed(raw) == (switched_on, None)


def test_a_comma_list_names_exactly_those_tools_and_turns_the_switch_on():
    assert parsed("delete_time_entry,delete_item") == (True, frozenset({"delete_time_entry", "delete_item"}))
    assert parsed("share_document") == (True, frozenset({"share_document"}))


def test_a_list_is_trimmed_lower_cased_and_free_of_empty_items_and_repeats():
    raw = "  Delete_Time_Entry , ,delete_item,, DELETE_ITEM ,delete_time_entry,"
    assert parsed(raw) == (True, frozenset({"delete_time_entry", "delete_item"}))
    assert isinstance(Settings.from_env(env(GORELO_ENABLE_DESTRUCTIVE=raw)).destructive_tools, frozenset)


FIVE_GATED = (
    "delete_ticket_comment,delete_invoice,delete_uptime_check,"
    "remove_document_related_item,delete_time_entry"
)


def test_a_list_of_five_gated_tools_parses_to_its_five_names():
    destructive, names = parsed(FIVE_GATED)
    assert destructive is True and names == frozenset(FIVE_GATED.split(",")) and len(names) == 5


@pytest.mark.parametrize("raw, word", [("1,delete_item", "1"), ("delete_item,on", "on"), ("yes,no", "no"), ("off,delete_item", "off")])
def test_a_word_next_to_other_items_is_refused_and_the_message_names_the_word(raw, word):
    with pytest.raises(SettingsError) as info:
        Settings.from_env(env(GORELO_ENABLE_DESTRUCTIVE=raw))
    message = str(info.value)
    assert message.startswith("GORELO_ENABLE_DESTRUCTIVE mixes the word ") and repr(word) in message
    assert "1, true, yes or on (every gated tool)" in message and "gated tool names separated by commas" in message


@pytest.mark.parametrize("raw, shown", [("delete_item delete_time_entry", "'delete_item delete_time_entry'"), ("a;b,delete_item", "'a;b'"), ("x=1", "'x=1'")])
def test_an_item_with_characters_a_tool_name_cannot_have_is_refused_and_shown(raw, shown):
    with pytest.raises(SettingsError) as info:
        Settings.from_env(env(GORELO_ENABLE_DESTRUCTIVE=raw))
    message = str(info.value)
    assert message.startswith("GORELO_ENABLE_DESTRUCTIVE has item(s) that cannot be tool names: ") and shown in message
    assert "separated by commas" in message


def test_a_very_long_item_is_cut_in_the_message():
    with pytest.raises(SettingsError) as info:
        Settings.from_env(env(GORELO_ENABLE_DESTRUCTIVE="x y" * 100))
    assert "..." in str(info.value) and len(str(info.value)) < 600


@pytest.mark.parametrize("raw", ["2", "enabled", "y", "truee", "-1", "delete_itm", "update_client"])
def test_a_single_item_that_is_not_a_word_is_a_name_and_the_registry_decides_later(raw):
    # settings.py does not know the tools: 'truee' is a name here, and server.resolve_gated_tools refuses it (tests/test_gated_tools.py)
    assert parsed(raw) == (True, frozenset({raw.lower()}))


def test_every_problem_is_still_reported_together_with_the_destructive_one():
    with pytest.raises(SettingsError) as info:
        Settings.from_env({"GORELO_ENABLE_DESTRUCTIVE": "1,delete_item", "GORELO_BASE_URL": "http://x"})
    message = str(info.value)
    for fragment in ("GORELO_API_KEY", "GORELO_ENABLE_DESTRUCTIVE mixes", "GORELO_BASE_URL"):
        assert fragment in message


def test_a_settings_made_by_hand_keeps_working_with_only_the_flag():
    assert Settings(api_key="k", destructive=True).destructive_tools is None  # as before: every gated tool
    assert Settings(api_key="k").destructive_tools is None and Settings(api_key="k").destructive is False
    named = Settings(api_key="k", destructive=True, destructive_tools=frozenset({"delete_item"}))
    assert named.destructive_tools == frozenset({"delete_item"})
    assert dataclasses.replace(named, destructive=False).destructive is False  # replace() still switches them all off


def test_settings_does_not_import_the_tools_so_the_watcher_and_the_harness_can_use_it():
    import os
    import subprocess
    import sys

    root = os.path.dirname(os.path.abspath(settings_module.__file__))
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import settings; "
        "sys.exit(1 if any(name == 'tools' or name.startswith('tools.') for name in sys.modules) else 0)"
    )
    done = subprocess.run([sys.executable, "-I", "-B", "-c", code, root], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
