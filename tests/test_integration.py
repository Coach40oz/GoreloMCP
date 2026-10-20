"""Integration checks over the FULL tool surface (all tool modules merged).

These pin the tool surface: the exact tool names per toolset,
the default exposure, destructive gating, MCP hints, LLM-facing metadata, and that every one of the
117 spec operations (2026-10-17 contract) is either declared by a tool, forbidden, or deliberately
excluded. The 2026-10-12 contract added the custom asset create, update and hardware types (declared by tools) and
GET /v1/api-keys and DELETE /v1/api-keys/{apiKeyId} (deliberately not exposed: no tool reaches /v1/api-keys at all).
The 2026-10-17 contract (a7954b24844b) added fourteen operations and every one has a tool: the twelve document operations
(the documents toolset, off by default), GET /v1/deletions (list_deletions) and GET /v1/assets/custom/{customAssetId}
(get_custom_asset, and the read-back of the two custom asset writes).
The four operations that contract e15cb5a18ec2 added with a tool (GET /v1/alerts, POST /v1/invoices,
GET /v1/invoices/{invoiceId} and, forbidden, POST /v1/api-keys) are pinned one by one at the end of the coverage checks.
"""

from __future__ import annotations

import json

import pytest
from conftest import list_tools

import tools  # noqa: F401  (registers every tool module)
from gorelo_client import FORBIDDEN_OPS, is_forbidden_op, is_side_effect_get
from settings import DEFAULT_TOOLSETS, TOOLSETS
from tools._common import REGISTRY

EXPECTED = {
    "core": {
        "regular": {
            "health_check", "list_clients", "get_client", "create_client", "update_client",
            "list_client_locations", "list_contacts", "get_contact", "create_contact", "update_contact",
            "list_org_groups", "list_org_users", "list_agents", "get_agent", "list_custom_assets",
            "list_custom_asset_hardware_types", "create_custom_asset", "update_custom_asset",
            "post_alert", "list_alerts", "get_custom_asset", "list_deletions",
        },
        "gated": set(),
    },
    "tickets": {
        "regular": {
            "list_tickets", "search_tickets", "get_ticket", "create_ticket", "update_ticket",
            "list_ticket_statuses", "list_ticket_types", "list_ticket_tags", "list_ticket_priorities",
            "list_ticket_sources", "list_ticket_comments", "get_ticket_comment", "create_ticket_comment",
            "list_ticket_conversations", "create_ticket_side_conversation", "create_ticket_approval",
            "get_ticket_approval", "upload_attachment",
        },
        "gated": {"delete_ticket_comment"},
    },
    "time": {
        "regular": {
            "list_time_entries", "get_time_entry", "create_time_entry", "update_time_entry",
            "list_billing_roles", "list_work_types",
        },
        "gated": {"delete_time_entry"},
    },
    "billing": {
        "regular": {
            "list_contracts", "get_contract", "list_invoices", "get_invoice", "create_invoice",
            "export_invoice_pdf", "list_items", "get_item", "create_item", "update_item",
            "list_item_categories", "list_taxes",
        },
        "gated": {"delete_invoice", "delete_item", "create_approved_invoice"},
    },
    "uptime": {
        "regular": {
            "list_uptime_checks", "get_uptime_check", "create_uptime_check", "update_uptime_check",
            "set_uptime_maintenance",
        },
        "gated": {"delete_uptime_check"},
    },
    "projects": {
        "regular": {
            "list_projects", "get_project", "create_project", "update_project", "list_project_tags",
            "list_project_types", "list_project_sections", "create_project_section",
            "update_project_section", "list_project_comments", "get_project_comment",
            "create_project_comment", "list_project_tasks", "get_project_task", "create_project_task",
            "update_project_task", "list_task_conversations", "create_task_side_conversation",
            "create_task_approval", "get_task_approval",
        },
        "gated": {"delete_project_task", "delete_project_comment"},
    },
    "forms": {
        "regular": {"list_forms", "list_form_responses", "create_form_submission_link"},
        "gated": set(),
    },
    "documents": {
        "regular": {
            "list_documents", "get_document", "export_document_pdf", "list_document_versions", "get_document_version",
            "list_document_related_items", "create_document", "update_document", "move_document", "archive_document",
            "restore_document", "unshare_document", "duplicate_document", "restore_document_version",
            "add_document_related_items",
        },
        "gated": {"share_document", "remove_document_related_item"},
    },
}

LEGACY_NAMES = {
    "create_ticket", "list_tickets", "search_tickets", "get_ticket", "list_ticket_statuses",
    "list_ticket_types", "list_ticket_tags", "list_ticket_priorities", "list_ticket_sources",
    "create_client", "update_client", "list_clients", "get_client", "list_client_locations",
    "create_contact", "update_contact", "list_contacts", "get_contact", "list_agents", "get_agent",
    "list_org_groups", "list_org_users", "post_alert", "health_check",
}

# Spec operations deliberately not exposed by any tool (besides FORBIDDEN_OPS): they cascade.
EXCLUDED_OPS = {
    "DELETE /v1/projects/{projectId}",
    "DELETE /v1/projects/{projectId}/sections/{sectionId}",
    # Added by the 2026-10-08 spec (98 operations). Money movement: no tool is built on them.
    "POST /v1/payments",
    "DELETE /v1/payments/{paymentId}",
    # Added by the 2026-10-12 spec (103 operations). API credentials: no tool reaches /v1/api-keys at all (the list
    # shows who holds keys, the DELETE revokes one, and POST /v1/api-keys, which mints one, is forbidden).
    "GET /v1/api-keys",
    "DELETE /v1/api-keys/{apiKeyId}",
}

# The operations the 2026-10-12 contract added with a tool, and which tools declare each. Since the 2026-10-17
# contract the single custom asset GET is the read-back of both writes (the list was before): list_custom_assets is the only
# tool that declares the list.
CUSTOM_ASSET_OPS_AND_THEIR_TOOLS = {
    "GET /v1/assets/custom/hardware-types": {"list_custom_asset_hardware_types"},
    "POST /v1/assets/custom": {"create_custom_asset"},
    "PATCH /v1/assets/custom/{customAssetId}": {"update_custom_asset"},
    "GET /v1/assets/custom": {"list_custom_assets"},
    "GET /v1/assets/custom/{customAssetId}": {"get_custom_asset", "create_custom_asset", "update_custom_asset"},
}

# The operations contract e15cb5a18ec2 (2026-10-02) added, and which tools declare each: the alert list, the invoice
# create (a Draft by create_invoice, an Approved one by the gated create_approved_invoice) and the invoice read (which
# both create tools also use to read the new invoice back). POST /v1/api-keys is the fourth new operation: forbidden,
# so no tool may declare it.
NEW_OPS_AND_THEIR_TOOLS = {
    "GET /v1/alerts": {"list_alerts"},
    "POST /v1/invoices": {"create_invoice", "create_approved_invoice"},
    "GET /v1/invoices/{invoiceId}": {"get_invoice", "create_invoice", "create_approved_invoice"},
    "POST /v1/api-keys": set(),
}

# The twelve document operations the 2026-10-17 contract added, and which tools declare each. All of them are in the documents
# toolset. The documents themselves cannot be deleted through the API (there is no such operation); the only DELETE is the
# removal of a related-item link, declared by a gated tool, and the only gated tool that declares the PATCH is share_document.
DOCUMENT_OPS_AND_THEIR_TOOLS = {
    "GET /v1/documents": {"list_documents"},
    "POST /v1/documents": {"create_document"},
    "GET /v1/documents/{documentId}": {"get_document", "update_document", "restore_document"},
    "PATCH /v1/documents/{documentId}": {
        "update_document", "move_document", "archive_document", "restore_document", "unshare_document", "share_document",
    },
    "POST /v1/documents/{documentId}/duplicate": {"duplicate_document"},
    "GET /v1/documents/{documentId}/pdf": {"export_document_pdf"},
    "GET /v1/documents/{documentId}/related-items": {"add_document_related_items", "list_document_related_items"},
    "POST /v1/documents/{documentId}/related-items": {"add_document_related_items"},
    "DELETE /v1/documents/{documentId}/related-items/{relatedItemId}": {"remove_document_related_item"},
    "GET /v1/documents/{documentId}/versions": {"list_document_versions", "restore_document_version"},
    "GET /v1/documents/{documentId}/versions/{versionId}": {"get_document_version"},
    "POST /v1/documents/{documentId}/versions/{versionId}/restore": {"restore_document_version"},
}
# The thirteenth operation of that contract with a tool of its own (the fourteenth, the single custom asset GET, is pinned above).
DELETION_OPS_AND_THEIR_TOOLS = {"GET /v1/deletions": {"list_deletions"}}

DASHES = (chr(0x2014), chr(0x2013))


def _specs_by_name():
    return {s.name: s for s in REGISTRY.specs}


def test_registry_matches_the_configured_surface_exactly():
    specs = _specs_by_name()
    for toolset, groups in EXPECTED.items():
        got_regular = {n for n, s in specs.items() if s.toolset == toolset and s.kind != "destructive"}
        got_gated = {n for n, s in specs.items() if s.toolset == toolset and s.kind == "destructive"}
        assert got_regular == groups["regular"], (toolset, got_regular ^ groups["regular"])
        assert got_gated == groups["gated"], (toolset, got_gated ^ groups["gated"])
    assert len(specs) == 111  # 92 tools in 1.2.0; 1.3.0 added get_custom_asset, list_deletions and the 17 document tools
    regular = {n for n, s in specs.items() if s.kind != "destructive"}
    assert (len(regular), len(specs) - len(regular)) == (101, 10)  # the two new gated tools are share_document and remove_document_related_item


def test_all_24_legacy_tool_names_still_exist():
    assert LEGACY_NAMES <= set(_specs_by_name())
    assert len(LEGACY_NAMES) == 24


def test_every_spec_operation_is_covered_or_deliberately_excluded(spec_index):
    declared = {op for s in REGISTRY.specs for op in s.ops}
    # forbidden by SHAPE (placeholder names ignored): the spec's own keys must be exactly FORBIDDEN_OPS
    forbidden = {op for op in spec_index.ops if is_forbidden_op(op)}
    assert forbidden == set(FORBIDDEN_OPS) and "POST /v1/api-keys" in FORBIDDEN_OPS
    assert not {op for op in declared if is_forbidden_op(op)}
    assert not declared & EXCLUDED_OPS
    uncovered = set(spec_index.ops) - declared - forbidden - EXCLUDED_OPS
    assert uncovered == set(), sorted(uncovered)
    assert len(spec_index.ops) == 117
    assert not any("/v1/payments" in op for op in declared)  # payments are recorded money: no tool reaches them


def test_each_operation_the_2026_10_02_spec_added_is_declared_by_exactly_the_tools_meant_to_use_it(spec_index):
    for op, expected in NEW_OPS_AND_THEIR_TOOLS.items():
        assert op in spec_index.ops, op
        users = {s.name for s in REGISTRY.specs if op in s.ops}
        assert users == expected, (op, users)
    # the api-keys create can mint credentials: forbidden by shape, declared by nobody, and no tool can be built on it
    assert is_forbidden_op("POST /v1/api-keys") and not NEW_OPS_AND_THEIR_TOOLS["POST /v1/api-keys"]
    assert not any(is_forbidden_op(op) for op in NEW_OPS_AND_THEIR_TOOLS if op != "POST /v1/api-keys")
    # the invoice create is the only POST on /v1/invoices and only the two create tools send it
    posters = {s.name for s in REGISTRY.specs if "POST /v1/invoices" in s.ops}
    assert {s.kind for s in REGISTRY.specs if s.name in posters} == {"write", "destructive"}
    assert {s.name for s in REGISTRY.specs if s.name in posters and s.kind == "destructive"} == {"create_approved_invoice"}


def test_each_operation_the_2026_10_12_spec_added_is_declared_by_exactly_the_tools_meant_to_use_it(spec_index):
    for op, expected in CUSTOM_ASSET_OPS_AND_THEIR_TOOLS.items():
        assert op in spec_index.ops, op
        assert {s.name for s in REGISTRY.specs if op in s.ops} == expected, op
    # the DELETE of a custom asset stays forbidden: no tool declares it
    assert not any(is_forbidden_op(op) for op in CUSTOM_ASSET_OPS_AND_THEIR_TOOLS)
    assert not any(is_forbidden_op(op) for s in REGISTRY.specs for op in s.ops)
    # the API key operations are deliberately not exposed: no tool declares anything under /v1/api-keys
    for op in ("GET /v1/api-keys", "DELETE /v1/api-keys/{apiKeyId}", "POST /v1/api-keys"):
        assert not [s.name for s in REGISTRY.specs if op in s.ops], op
    assert not [s.name for s in REGISTRY.specs if any("/v1/api-keys" in op for op in s.ops)]
    assert not any("/v1/api-keys" in op for op in {op for s in REGISTRY.specs for op in s.ops})
    # creating and updating write; neither can delete
    kinds = {s.name: (s.kind, s.destructive_hint) for s in REGISTRY.specs}
    assert kinds["create_custom_asset"] == ("write", False) and kinds["update_custom_asset"] == ("write", True)
    assert kinds["list_custom_asset_hardware_types"][0] == "read"


@pytest.mark.anyio
async def test_default_toolsets_expose_63_tools_and_no_gated_ones(server_factory):
    server = server_factory(toolsets=frozenset(DEFAULT_TOOLSETS), destructive=False)
    names = {t.name for t in await list_tools(server)}
    expected = set().union(*(EXPECTED[ts]["regular"] for ts in DEFAULT_TOOLSETS))
    assert names == expected
    assert len(names) == 63  # 58 until the 2026-10-12 contract added the three custom asset tools, 61 until the 2026-10-17 one added get_custom_asset and list_deletions
    assert {"list_alerts", "get_invoice", "create_invoice", "get_custom_asset", "list_deletions"} <= names
    assert "create_approved_invoice" not in names  # it needs GORELO_ENABLE_DESTRUCTIVE as well
    assert not any("document" in name for name in names)  # the documents toolset is off by default


def test_documents_is_the_last_toolset_and_the_default_leaves_it_off():
    assert TOOLSETS[-1] == "documents" and "documents" not in DEFAULT_TOOLSETS
    assert TOOLSETS == (*DEFAULT_TOOLSETS, "projects", "forms", "documents")


def test_each_operation_the_2026_10_17_spec_added_is_declared_by_exactly_the_tools_meant_to_use_it(spec_index):
    added = {**DOCUMENT_OPS_AND_THEIR_TOOLS, **DELETION_OPS_AND_THEIR_TOOLS}
    assert len(DOCUMENT_OPS_AND_THEIR_TOOLS) == 12
    for op, expected in added.items():
        assert op in spec_index.ops, op
        assert {s.name for s in REGISTRY.specs if op in s.ops} == expected, op
    # the table holds every operation under /v1/documents, so the toolset covers the whole surface and no document can be deleted
    assert {op for op in spec_index.ops if "/v1/documents" in op} == set(DOCUMENT_OPS_AND_THEIR_TOOLS)
    specs = _specs_by_name()
    assert {specs[name].toolset for names in DOCUMENT_OPS_AND_THEIR_TOOLS.values() for name in names} == {"documents"}
    delete_op = "DELETE /v1/documents/{documentId}/related-items/{relatedItemId}"
    assert {specs[name].kind for name in DOCUMENT_OPS_AND_THEIR_TOOLS[delete_op]} == {"destructive"}
    patch_op = "PATCH /v1/documents/{documentId}"
    assert {name for name in DOCUMENT_OPS_AND_THEIR_TOOLS[patch_op] if specs[name].kind == "destructive"} == {"share_document"}
    # the spec names no export event for the document PDF: it is a plain read, not a side-effect GET like the invoice PDF
    assert not is_side_effect_get("GET /v1/documents/{documentId}/pdf") and specs["export_document_pdf"].kind == "read"
    assert specs["list_deletions"].toolset == "core" and specs["list_deletions"].kind == "read"
    assert not any(is_forbidden_op(op) for op in added)


@pytest.mark.anyio
async def test_destructive_flag_adds_exactly_the_gated_tools(server_factory):
    off = {t.name for t in await list_tools(server_factory(toolsets=frozenset(TOOLSETS), destructive=False))}
    on = {t.name for t in await list_tools(server_factory(toolsets=frozenset(TOOLSETS), destructive=True))}
    gated = set().union(*(EXPECTED[ts]["gated"] for ts in TOOLSETS))
    assert on - off == gated
    assert not off & gated


@pytest.mark.anyio
async def test_hints_follow_the_tool_kind(server_factory):
    specs = _specs_by_name()
    for tool in await list_tools(server_factory(toolsets=frozenset(TOOLSETS), destructive=True)):
        spec = specs[tool.name]
        ann = tool.annotations
        if spec.kind == "read":
            assert ann.readOnlyHint is True and ann.destructiveHint is False, tool.name
        elif spec.kind == "destructive":
            assert ann.destructiveHint is True and ann.readOnlyHint is False, tool.name
        else:
            assert ann.readOnlyHint is False, tool.name
            if tool.name.startswith(("update_", "set_")):
                assert ann.destructiveHint is True, f"{tool.name} overwrites data: destructive_hint=True"
            if tool.name.startswith("create_"):
                assert ann.destructiveHint is False, tool.name


@pytest.mark.anyio
async def test_every_tool_and_param_is_described_without_dashes(server_factory):
    for tool in await list_tools(server_factory(toolsets=frozenset(TOOLSETS), destructive=True)):
        assert tool.description and len(tool.description) > 40, tool.name
        assert not any(d in tool.description for d in DASHES), tool.name
        for pname, prop in (tool.inputSchema.get("properties") or {}).items():
            desc = prop.get("description")
            assert desc, f"{tool.name}.{pname} has no description"
            assert not any(d in desc for d in DASHES), f"{tool.name}.{pname}"
            assert pname == pname.lower(), f"{tool.name}.{pname} is not snake_case"


@pytest.mark.anyio
async def test_destructive_tools_require_confirm_defaulting_to_false(server_factory):
    specs = _specs_by_name()
    for tool in await list_tools(server_factory(toolsets=frozenset(TOOLSETS), destructive=True)):
        if specs[tool.name].kind == "destructive":
            confirm = tool.inputSchema["properties"]["confirm"]
            assert confirm.get("default") is False, tool.name


@pytest.mark.anyio
async def test_default_tools_list_stays_within_budget(server_factory):
    tools_list = await list_tools(server_factory(toolsets=frozenset(DEFAULT_TOOLSETS), destructive=False))
    size = len(json.dumps([t.model_dump(mode="json", exclude_none=True) for t in tools_list]))
    # The budget sits about 1 percent above the size measured for 1.3.0 (see the constant below). It
    # was raised once for the three default tools contract e15cb5a18ec2 added, once more for the custom properties of
    # contract 1b1c0cec0e9a and twice for release 1.3.0; growth beyond it needs a deliberate bump, because every byte here is
    # context claude.ai loads in every conversation that uses the connector.
    assert size < TOOLS_LIST_BUDGET_BYTES, size


# The default tools/list measured 95425 bytes for 1.3.0 (90209 for 1.2.0; the documents toolset and the gated tools are not in
# it): the deletions and custom asset tools, the strict status texts, the upload_attachment text, health_check's api_host and
# gated_tools; the budget is that plus about 1 percent. The budget leaves 975 bytes above that: the size plus 1 percent, rounded
# up to the next 100.
# At 1.2.0 the default tools/list was 90_209 bytes with all the texts of the custom properties (gated tools such as the delete
# tools and create_approved_invoice are not in it). Before them it was 89_083 bytes. They add 842 bytes for update_client,
# which gained custom_properties and clear_custom_property_ids (an object entry {id, value} and a list of ids, with their
# texts), and for the get_client and list_clients texts about CustomProperties (contract 1b1c0cec0e9a), 73 bytes for the
# same contract's sentence on the two device tools, 108 bytes for the update_client sentence that has the model compare
# the returned Values with what it sent, and 103 bytes for the one that says update_client reads the client first. At 1.2.0
# the budget left 891 bytes (about 1 percent) above that.
TOOLS_LIST_BUDGET_BYTES = 96400
MAX_TOOL_DESCRIPTION_CHARS = 900
MAX_PARAM_DESCRIPTION_CHARS = 220


@pytest.mark.anyio
async def test_every_description_stays_within_the_caps(server_factory):
    for tool in await list_tools(server_factory(toolsets=frozenset(TOOLSETS), destructive=True)):
        assert len(tool.description or "") <= MAX_TOOL_DESCRIPTION_CHARS, (tool.name, len(tool.description))
        for pname, prop in (tool.inputSchema.get("properties") or {}).items():
            assert len(prop.get("description", "")) <= MAX_PARAM_DESCRIPTION_CHARS, (tool.name, pname)
