"""The advertised input schemas are compacted (no null branches, no "default": null) without
changing what the tools accept."""

from __future__ import annotations

import json

import pytest
from conftest import call_tool, list_tools, paged_envelope

from server import compact_input_schema
from settings import TOOLSETS


def test_optional_scalar_loses_null_branch_and_null_default():
    schema = {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "description": "Id."}
    assert compact_input_schema(schema) == {"type": "integer", "description": "Id."}


def test_optional_list_and_enum_keep_their_real_shape():
    lst = {"anyOf": [{"type": "array", "items": {"type": "integer"}}, {"type": "null"}], "default": None}
    assert compact_input_schema(lst) == {"type": "array", "items": {"type": "integer"}}
    enum = {"anyOf": [{"enum": ["asc", "desc"], "type": "string"}, {"type": "null"}], "default": None, "description": "Order."}
    assert compact_input_schema(enum) == {"enum": ["asc", "desc"], "type": "string", "description": "Order."}


def test_non_null_defaults_and_required_types_are_untouched():
    assert compact_input_schema({"type": "integer", "default": 50}) == {"type": "integer", "default": 50}
    assert compact_input_schema({"type": "boolean", "default": False}) == {"type": "boolean", "default": False}
    assert compact_input_schema({"type": "string", "description": "x"}) == {"type": "string", "description": "x"}


def test_unions_with_more_than_one_real_branch_keep_anyof_without_null():
    schema = {"anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}], "default": None}
    assert compact_input_schema(schema) == {"anyOf": [{"type": "integer"}, {"type": "string"}]}


def test_nested_defs_and_items_are_compacted_and_input_is_not_mutated():
    schema = {
        "type": "object",
        "properties": {"attachments": {"anyOf": [{"type": "array", "items": {"$ref": "#/$defs/A"}}, {"type": "null"}], "default": None}},
        "$defs": {"A": {"type": "object", "properties": {"url": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}}}},
    }
    before = json.dumps(schema, sort_keys=True)
    out = compact_input_schema(schema)
    assert json.dumps(schema, sort_keys=True) == before
    assert out["properties"]["attachments"] == {"type": "array", "items": {"$ref": "#/$defs/A"}}
    assert out["$defs"]["A"]["properties"]["url"] == {"type": "string"}


@pytest.mark.anyio
async def test_no_tool_advertises_null_branches_or_null_defaults(server_factory):
    server = server_factory(toolsets=frozenset(TOOLSETS), destructive=True)
    for tool in await list_tools(server):
        text = json.dumps(tool.inputSchema)
        assert '{"type": "null"}' not in text, tool.name
        assert '"default": null' not in text, tool.name


@pytest.mark.anyio
async def test_explicit_null_is_still_accepted_for_an_optional_param(server_factory, mock_gorelo):
    mock_gorelo.on("GET", "/v1/clients", paged_envelope([{"Id": 1, "Name": "A"}], total_count=1))
    server = server_factory(toolsets=frozenset(TOOLSETS))
    result = await call_tool(server, "list_clients", {"query": None, "page_size": 10})
    assert result["count"] == 1
