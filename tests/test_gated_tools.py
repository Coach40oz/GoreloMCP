"""GORELO_ENABLE_DESTRUCTIVE as a switch per tool: which gated tools a server registers.

settings.py parses the value (tests/test_settings.py) but does not know the tools. Here the registry decides:
Registry.select keeps exactly the listed gated tools, server.resolve_gated_tools refuses a list the registry cannot
honor with one error, build_server registers exactly the listed tools and tells the lifespan which ones, and the
health_check tool reports their names. A custom Registry stands in for the real one where the rule is the point; the real
registry is used for the list of five gated tools and for every real gated tool on its own.
"""

from __future__ import annotations

import logging
from typing import Annotated

import pytest
from conftest import call_tool, call_tool_error, envelope, list_tools
from fastmcp import Context
from pydantic import Field

from server import build_instructions, build_server, resolve_gated_tools
from settings import TOOLSETS, Settings, SettingsError
from tools._common import REGISTRY, Registry, StrictBool, client_of, require_confirm, server_info_of

pytestmark = pytest.mark.anyio

EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)

# The five gated tools that the tests below switch on; the other gated tools stay off.
FIVE_GATED = (
    "delete_ticket_comment,delete_invoice,delete_uptime_check,"
    "remove_document_related_item,delete_time_entry"
)
FIVE_GATED_TOOLS = frozenset(FIVE_GATED.split(","))
REAL_GATED = sorted(spec.name for spec in REGISTRY.specs if spec.kind == "destructive")


def demo_registry() -> Registry:
    """One read, one write and three gated tools in three toolsets (ops that exist in the spec index)."""
    registry = Registry()

    @registry.tool(toolset="core", kind="read", ops=["GET /v1/clients/{clientId}"])
    async def read_one(ctx: Context, client_id: Annotated[int, Field(description="Client id.")]) -> dict:
        """Fetch one client."""
        return await client_of(ctx).get_one("GET /v1/clients/{clientId}", path_params={"clientId": client_id}, tool="read_one")

    @registry.tool(toolset="core", kind="read", ops=["GET /v1/clients/{clientId}"])
    async def info_tool(ctx: Context) -> dict:
        """Report what the lifespan says about the server."""
        return server_info_of(ctx)

    @registry.tool(toolset="tickets", kind="write", ops=["POST /v1/clients"])
    async def write_one(ctx: Context) -> dict:
        """Create a client."""
        return {}

    @registry.tool(toolset="time", kind="destructive", ops=["DELETE /v1/time-entries/{timeEntryId}"])
    async def delete_time(
        ctx: Context,
        time_entry_id: Annotated[int, Field(description="Time entry id.")],
        confirm: Annotated[StrictBool, Field(description="Must be true to delete.")] = False,
    ) -> dict:
        """Delete a time entry."""
        require_confirm(confirm, action=f"delete time entry {time_entry_id}")
        return await client_of(ctx).delete(
            "DELETE /v1/time-entries/{timeEntryId}", path_params={"timeEntryId": time_entry_id}, tool="delete_time"
        )

    @registry.tool(toolset="billing", kind="destructive", ops=["DELETE /v1/items/{itemId}"])
    async def delete_thing(
        ctx: Context,
        item_id: Annotated[int, Field(description="Item id.")],
        confirm: Annotated[StrictBool, Field(description="Must be true to delete.")] = False,
    ) -> dict:
        """Delete an item."""
        require_confirm(confirm, action=f"delete item {item_id}")
        return {}

    @registry.tool(toolset="uptime", kind="destructive", ops=["DELETE /v1/uptime/{checkId}"])
    async def delete_check(
        ctx: Context,
        check_id: Annotated[str, Field(description="Check id.")],
        confirm: Annotated[StrictBool, Field(description="Must be true to delete.")] = False,
    ) -> dict:
        """Delete an uptime check."""
        require_confirm(confirm, action=f"delete check {check_id}")
        return {}

    return registry


ALL = frozenset(TOOLSETS)


def names_of(specs) -> list[str]:
    return [spec.name for spec in specs]


# --------------------------------------------------------------------------
# Registry.select: the gated tools of a name set
# --------------------------------------------------------------------------


def test_select_without_names_keeps_every_gated_tool_when_destructive_and_none_otherwise():
    registry = demo_registry()
    assert names_of(registry.select(ALL, True)) == ["read_one", "info_tool", "write_one", "delete_time", "delete_thing", "delete_check"]
    assert names_of(registry.select(ALL, False)) == ["read_one", "info_tool", "write_one"]
    assert names_of(registry.select(ALL, True, None)) == names_of(registry.select(ALL, True))


def test_select_with_names_keeps_every_regular_tool_and_exactly_the_named_gated_ones():
    registry = demo_registry()
    assert names_of(registry.select(ALL, True, {"delete_thing"})) == ["read_one", "info_tool", "write_one", "delete_thing"]
    assert names_of(registry.select(ALL, True, ["delete_check", "delete_time"])) == [
        "read_one", "info_tool", "write_one", "delete_time", "delete_check"
    ]
    assert names_of(registry.select(ALL, True, frozenset())) == ["read_one", "info_tool", "write_one"]


def test_select_names_never_switch_a_gated_tool_on_while_destructive_is_false():
    registry = demo_registry()
    assert names_of(registry.select(ALL, False, {"delete_thing", "delete_time"})) == ["read_one", "info_tool", "write_one"]


def test_select_still_filters_by_toolset_first():
    registry = demo_registry()
    assert names_of(registry.select({"core", "time"}, True, {"delete_thing", "delete_time"})) == ["read_one", "info_tool", "delete_time"]


def test_select_ignores_names_that_are_not_gated_tools_of_the_toolsets():
    registry = demo_registry()
    # regular tools and unknown names add nothing: resolve_gated_tools is what refuses them
    assert names_of(registry.select(ALL, True, {"read_one", "write_one", "nope"})) == ["read_one", "info_tool", "write_one"]


# --------------------------------------------------------------------------
# resolve_gated_tools: the names that are registered, or one refusal
# --------------------------------------------------------------------------


def settings_for(make_settings, *, toolsets=ALL, destructive=True, names=None) -> Settings:
    return make_settings(toolsets=toolsets, destructive=destructive, destructive_tools=None if names is None else frozenset(names))


def test_resolve_gives_nothing_while_the_switch_is_off_and_does_not_check_names_then(make_settings):
    registry = demo_registry()
    assert resolve_gated_tools(settings_for(make_settings, destructive=False), registry) == []
    assert resolve_gated_tools(settings_for(make_settings, destructive=False, names={"nope"}), registry) == []


def test_resolve_with_no_names_gives_every_gated_tool_of_the_enabled_toolsets_sorted(make_settings):
    registry = demo_registry()
    assert resolve_gated_tools(settings_for(make_settings), registry) == ["delete_check", "delete_thing", "delete_time"]
    assert resolve_gated_tools(settings_for(make_settings, toolsets={"core", "time"}), registry) == ["delete_time"]
    assert resolve_gated_tools(settings_for(make_settings, toolsets={"core", "tickets"}), registry) == []  # 1 with no gated tool in reach


def test_resolve_with_names_gives_exactly_those_sorted(make_settings):
    registry = demo_registry()
    assert resolve_gated_tools(settings_for(make_settings, names={"delete_time", "delete_check"}), registry) == ["delete_check", "delete_time"]
    assert resolve_gated_tools(settings_for(make_settings, names={"delete_thing"}), registry) == ["delete_thing"]


def refusal(make_settings, names, *, toolsets=ALL, registry=None) -> str:
    with pytest.raises(SettingsError) as info:
        resolve_gated_tools(settings_for(make_settings, toolsets=toolsets, names=names), registry or demo_registry())
    return str(info.value)


def test_an_unknown_name_is_refused_with_the_variable_the_name_and_the_valid_names(make_settings):
    message = refusal(make_settings, {"delete_itm"})
    assert message.startswith("GORELO_ENABLE_DESTRUCTIVE cannot be honored: 'delete_itm' (no tool has this name).")
    assert "delete_check, delete_thing, delete_time" in message  # the valid names, sorted
    assert "Write 1 for all of them, 0 or nothing for none" in message  # a mistyped switch word is told what to write


def test_a_tool_that_is_not_gated_is_refused_and_told_it_is_registered_anyway(make_settings):
    message = refusal(make_settings, {"read_one", "write_one"})
    assert "'read_one' (not a gated tool: it is registered whenever its toolset is on)" in message
    assert "'write_one' (not a gated tool: it is registered whenever its toolset is on)" in message


def test_a_gated_tool_of_a_toolset_that_is_not_enabled_is_refused_with_the_toolset(make_settings):
    message = refusal(make_settings, {"delete_thing"}, toolsets={"core", "time"})
    assert "'delete_thing' (a gated tool of the toolset 'billing', which GORELO_TOOLSETS does not enable)" in message
    assert "enabled toolsets (core, time): delete_time." in message  # the valid names are those of the enabled toolsets only


def test_every_bad_name_is_in_one_error_and_a_good_name_is_not_among_them(make_settings):
    message = refusal(make_settings, {"zzz", "read_one", "delete_thing", "delete_time"}, toolsets={"core", "tickets", "time"})
    assert message.count("GORELO_ENABLE_DESTRUCTIVE") == 1
    for fragment in ("'zzz' (no tool", "'read_one' (not a gated tool", "'delete_thing' (a gated tool of the toolset 'billing'"):
        assert fragment in message, fragment
    assert "'delete_time'" not in message  # it is valid; it shows only in the list of valid names, without quotes
    assert "enabled toolsets (core, tickets, time): delete_time." in message
    assert message.index("'read_one'") < message.index("'zzz'")  # the names are listed in sorted order


def test_with_no_gated_tool_in_the_enabled_toolsets_the_valid_list_says_none(make_settings):
    message = refusal(make_settings, {"delete_time"}, toolsets={"core", "tickets"})
    assert "enabled toolsets (core, tickets): none." in message


def test_a_very_long_unknown_name_is_cut_and_the_message_is_plain_ascii_without_dashes(make_settings):
    message = refusal(make_settings, {"x" * 300})
    assert "x" * 65 not in message and len(message) < 700
    assert message.isascii() and EM_DASH not in message and EN_DASH not in message


def test_the_message_never_says_a_name_is_valid_when_the_registry_has_no_such_tool(make_settings):
    message = refusal(make_settings, {"nope"}, registry=Registry())
    assert "no tool has this name" in message and "enabled toolsets (" in message and "): none." in message


# --------------------------------------------------------------------------
# build_server registers exactly the listed gated tools
# --------------------------------------------------------------------------


async def tool_names(server) -> list[str]:
    return sorted(tool.name for tool in await list_tools(server))


async def test_a_list_registers_every_regular_tool_and_exactly_the_listed_gated_ones(server_factory):
    server = server_factory(registry=demo_registry(), toolsets=ALL, destructive=True, destructive_tools=frozenset({"delete_time", "delete_check"}))
    assert await tool_names(server) == ["delete_check", "delete_time", "info_tool", "read_one", "write_one"]


async def test_no_list_still_registers_every_gated_tool_and_the_switch_off_none(server_factory):
    on = server_factory(registry=demo_registry(), toolsets=ALL, destructive=True)
    off = server_factory(registry=demo_registry(), toolsets=ALL, destructive=False)
    assert await tool_names(on) == ["delete_check", "delete_thing", "delete_time", "info_tool", "read_one", "write_one"]
    assert await tool_names(off) == ["info_tool", "read_one", "write_one"]


async def test_a_list_with_the_switch_off_registers_no_gated_tool(server_factory):
    # a Settings changed with dataclasses.replace(destructive=False) must switch everything off, as the live smoke run relies on
    server = server_factory(registry=demo_registry(), toolsets=ALL, destructive=False, destructive_tools=frozenset({"delete_time"}))
    assert await tool_names(server) == ["info_tool", "read_one", "write_one"]


async def test_a_gated_tool_that_is_not_listed_does_not_exist_and_cannot_be_called(server_factory, mock_gorelo):
    server = server_factory(registry=demo_registry(), toolsets=ALL, destructive=True, destructive_tools=frozenset({"delete_time"}))
    text = await call_tool_error(server, "delete_thing", {"item_id": 1, "confirm": True})
    assert "unknown tool" in text.lower()
    assert mock_gorelo.requests == []


async def test_a_listed_gated_tool_still_needs_confirm_and_sends_nothing_without_it(server_factory, mock_gorelo):
    server = server_factory(registry=demo_registry(), toolsets=ALL, destructive=True, destructive_tools=frozenset({"delete_time"}))
    text = await call_tool_error(server, "delete_time", {"time_entry_id": 55})
    assert text.startswith("confirm: refusing to delete time entry 55 without confirm=true.")
    assert mock_gorelo.requests == []
    mock_gorelo.on("DELETE", "/v1/time-entries/55", envelope({"Id": 55, "Outcome": "Deleted"}))
    assert await call_tool(server, "delete_time", {"time_entry_id": 55, "confirm": True}) == {"Id": 55, "Outcome": "Deleted"}
    assert [request.method for request in mock_gorelo.requests] == ["DELETE"]


async def test_the_lifespan_says_which_gated_tools_are_registered(server_factory):
    chosen = server_factory(registry=demo_registry(), toolsets=ALL, destructive=True, destructive_tools=frozenset({"delete_time", "delete_check"}))
    info = await call_tool(chosen, "info_tool")
    assert (info["toolsets"], info["destructive"], info["gated_tools"]) == (sorted(ALL), True, ["delete_check", "delete_time"])
    every = await call_tool(server_factory(registry=demo_registry(), toolsets=ALL, destructive=True), "info_tool")
    assert every["gated_tools"] == ["delete_check", "delete_thing", "delete_time"]
    none = await call_tool(server_factory(registry=demo_registry(), toolsets=ALL, destructive=False), "info_tool")
    assert none["gated_tools"] == [] and none["destructive"] is False
    short = await call_tool(server_factory(registry=demo_registry(), toolsets={"core", "time"}, destructive=True), "info_tool")
    assert short["gated_tools"] == ["delete_time"]  # only what is registered, not what the flag could add


async def test_a_list_the_registry_cannot_honor_stops_the_build_before_any_server_or_http_call(make_settings, spec_index, mock_gorelo):
    settings = settings_for(make_settings, toolsets={"core", "time"}, names={"delete_time", "delete_thing", "nope"})
    with pytest.raises(SettingsError) as info:
        build_server(settings, transport=mock_gorelo.transport, spec=spec_index, registry=demo_registry())
    assert "'delete_thing' (a gated tool of the toolset 'billing'" in str(info.value) and "'nope' (no tool" in str(info.value)
    assert mock_gorelo.requests == []


def test_the_ready_line_keeps_its_shape_with_a_list_and_counts_what_is_registered(make_settings, spec_index, mock_gorelo, caplog):
    caplog.set_level(logging.INFO, logger="gorelo-mcp")
    build_server(
        settings_for(make_settings, toolsets={"core", "tickets", "time", "uptime"}, names={"delete_time"}),
        transport=mock_gorelo.transport, spec=spec_index, registry=demo_registry(),
    )
    lines = [r.getMessage() for r in caplog.records if r.name == "gorelo-mcp"]
    # the gated tools are logged by main.py ("gorelo-mcp gated tools: ..."); the build adds no second line
    assert lines == ["gorelo-mcp ready: toolsets=core,tickets,time,uptime destructive=True tools=4"]


def test_the_server_instructions_do_not_depend_on_which_gated_tools_are_registered(make_settings, spec_index, mock_gorelo):
    registry = demo_registry()
    texts = {
        build_server(settings_for(make_settings, names=names, destructive=destructive), transport=mock_gorelo.transport, spec=spec_index, registry=registry).instructions
        for names, destructive in ((None, False), (None, True), ({"delete_time"}, True))
    }
    assert texts == {build_instructions(ALL)}
    assert "6. Delete, void and approved-invoice tools exist only when the operator has enabled them" in texts.pop()


# --------------------------------------------------------------------------
# The real registry: the five-name list and each real gated tool on its own
# --------------------------------------------------------------------------


def test_the_real_registry_has_the_gated_tools_the_five_list_is_made_of():
    assert FIVE_GATED_TOOLS <= set(REAL_GATED)
    assert set(REAL_GATED) - FIVE_GATED_TOOLS == {"create_approved_invoice", "delete_item", "delete_project_comment", "delete_project_task", "share_document"}


async def test_a_list_of_five_gated_tools_registers_exactly_those(server_factory):
    settings = Settings.from_env(
        {"GORELO_API_KEY": "k", "GORELO_TOOLSETS": "all", "GORELO_ENABLE_DESTRUCTIVE": FIVE_GATED}, require_http=False
    )
    names = {tool.name for tool in await list_tools(server_factory(settings=settings))}
    assert {spec.name for spec in REGISTRY.specs if spec.kind == "destructive"} & names == FIVE_GATED_TOOLS
    assert not names & (set(REAL_GATED) - FIVE_GATED_TOOLS)  # the other gated tools stay off
    regular = {spec.name for spec in REGISTRY.specs if spec.kind != "destructive"}
    assert names == regular | FIVE_GATED_TOOLS
    assert resolve_gated_tools(settings) == sorted(FIVE_GATED_TOOLS)


def test_a_list_of_five_gated_tools_without_the_documents_toolset_refuses_to_build_and_names_the_document_tool(server_factory):
    settings = Settings.from_env(
        {"GORELO_API_KEY": "k", "GORELO_TOOLSETS": "core,tickets,time,billing,uptime,projects,forms", "GORELO_ENABLE_DESTRUCTIVE": FIVE_GATED},
        require_http=False,
    )
    with pytest.raises(SettingsError) as info:
        server_factory(settings=settings)
    message = str(info.value)
    assert "'remove_document_related_item' (a gated tool of the toolset 'documents', which GORELO_TOOLSETS does not enable)" in message
    assert "'delete_time_entry'" not in message and "delete_invoice" in message  # both are valid here, so they are in the list of valid names


@pytest.mark.parametrize("name", REAL_GATED)
async def test_each_real_gated_tool_can_be_the_only_one_registered(server_factory, name):
    server = server_factory(toolsets=ALL, destructive=True, destructive_tools=frozenset({name}))
    names = {tool.name for tool in await list_tools(server)}
    assert names == {spec.name for spec in REGISTRY.specs if spec.kind != "destructive"} | {name}


async def test_one_switches_on_every_real_gated_tool_and_zero_none(server_factory):
    every = {tool.name for tool in await list_tools(server_factory(toolsets=ALL, destructive=True))}
    none = {tool.name for tool in await list_tools(server_factory(toolsets=ALL, destructive=False))}
    assert every - none == set(REAL_GATED) and not none & set(REAL_GATED)


@pytest.mark.parametrize("word", ["truee", "enabled", "y", "2", "delete_itm"])
def test_a_mistyped_switch_word_stops_the_build_and_tells_the_operator_what_to_write(server_factory, word):
    settings = Settings.from_env({"GORELO_API_KEY": "k", "GORELO_TOOLSETS": "all", "GORELO_ENABLE_DESTRUCTIVE": word}, require_http=False)
    with pytest.raises(SettingsError) as info:
        server_factory(settings=settings)
    message = str(info.value)
    assert message.startswith(f"GORELO_ENABLE_DESTRUCTIVE cannot be honored: '{word}' (no tool has this name).")
    assert "Write 1 for all of them" in message and "delete_item" in message


async def test_a_real_gated_tool_that_is_listed_still_refuses_without_confirm_and_sends_nothing(server_factory, mock_gorelo):
    server = server_factory(toolsets=ALL, destructive=True, destructive_tools=frozenset({"delete_time_entry"}))
    text = await call_tool_error(server, "delete_time_entry", {"time_entry_id": 5})
    assert "confirm" in text and "refusing" in text
    assert mock_gorelo.requests == []
    text = await call_tool_error(server, "delete_uptime_check", {"check_id": "x", "confirm": True})
    assert "unknown tool" in text.lower()
