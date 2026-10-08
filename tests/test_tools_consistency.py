"""Cross-module consistency of the text the model reads in the tool list (offline, no HTTP).

A safety rule that one tool module states can be missing from another.
These checks look at ALL tools at once (every toolset on, delete tools enabled):

* every destructive tool tells the model to ask the user first and that it needs confirm=true;
* every write that can email says who is emailed (or that nobody is);
* every paged list says how to read on: pass next_cursor back as cursor, same filters, until has_more is false;
* no tool text names a destructive tool without saying that it exists only when the operator enabled the destructive
  tools (", when deletes are enabled" for a delete or void tool, ", when enabled" for create_approved_invoice, the one
  gated tool that is not a delete: it approves an invoice, which pushes it to accounting);
* every paged list refuses a blank cursor, every clear_fields lists its names in the schema, a tool outside the
  projects toolset says so when it points at a project tool, the attachments of every tool that takes them are the
  name and url upload_attachment returned, and no tool text carries a number that belongs to one tenant;
* the two tools that approve or void an invoice say that a void happens in Gorelo ONLY and that the copy already pushed
  to the accounting system stays open (observed with Xero), before the instruction to ask the user, and no
  other tool text talks about voiding.

A new tool that trips one of these rules fails here until its text says what the rule asks for.
"""

import re

import pytest
from conftest import call_tool_error, list_tools, uid

from settings import TOOLSETS
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio


def flat(text):
    return " ".join((text or "").split())


async def all_tools(server_factory):
    """{name: the tool as a client lists it}, every toolset on and delete tools enabled."""
    server = server_factory(toolsets=frozenset(TOOLSETS), destructive=True)
    return {tool.name: tool for tool in await list_tools(server)}


def kind_of(name):
    return next(spec.kind for spec in REGISTRY.specs if spec.name == name)


def toolset_of(name):
    return next(spec.toolset for spec in REGISTRY.specs if spec.name == name)


def texts_of(tool):
    """Every piece of text the model reads for one tool: {where: text}."""
    found = {"description": flat(tool.description)}
    for param, definition in tool.inputSchema.get("properties", {}).items():
        found[f"parameter {param}"] = flat(definition.get("description"))
    return found


# --------------------------------------------------------------------------
# The listing is the registry (so the rules below cover every tool)
# --------------------------------------------------------------------------


async def test_the_checked_listing_holds_every_registered_tool(server_factory):
    tools = await all_tools(server_factory)
    assert set(tools) == {spec.name for spec in REGISTRY.specs}
    assert {name for name in tools if kind_of(name) == "destructive"} == {
        "create_approved_invoice",
        "delete_invoice",
        "delete_item",
        "delete_project_comment",
        "delete_project_task",
        "delete_ticket_comment",
        "delete_time_entry",
        "delete_uptime_check",
    }


# --------------------------------------------------------------------------
# Destructive tools: ask the user first, confirm=true
# --------------------------------------------------------------------------


async def test_every_destructive_tool_says_to_ask_the_user_first_and_that_it_needs_confirm_true(server_factory):
    tools = await all_tools(server_factory)
    destructive = sorted(name for name in tools if kind_of(name) == "destructive")
    assert destructive
    for name in destructive:
        description = flat(tools[name].description)
        assert "ask the user first" in description.lower(), f"{name} does not say to ask the user first: {description}"
        assert "needs confirm=true" in description, f"{name} does not say it needs confirm=true: {description}"
        confirm = tools[name].inputSchema["properties"]["confirm"]
        assert confirm["default"] is False and confirm["type"] == "boolean", name
        # the parameter repeats the rule next to where the model fills it in
        assert re.search(r"ask the user first|user approves", confirm["description"], re.I), (name, confirm)
        assert "Must be true" in confirm["description"], (name, confirm)


# --------------------------------------------------------------------------
# Writes that can email say who
# --------------------------------------------------------------------------

# A sentence about sending or announcing something to people. "secondary emails" is contact data, not a send.
EMAIL_ACTION = re.compile(r"\b(?<!secondary )(?:emails?|emailed|notify|notif\w+)\b", re.IGNORECASE)

# Every non-read tool whose text talks about emailing or notifying, with what it must say about who. A tool that
# does not email says so ("emails nobody", "Nothing is emailed"); one that does names the recipients; one where
# Gorelo documents a notification but no recipient says that Gorelo does not say who.
EMAIL_WHO = {
    "create_ticket": ("send_created_email=true also emails the contact", "may still notify contacts"),
    "update_ticket": ("a status change may email the contact",),
    "create_ticket_comment": (
        "private (default) nobody",
        "public the ticket contact and CCs",
        "side_conversation that conversation's recipients",
        "approval the approvers",
        "Tell the user who before posting anything not private",
    ),
    "create_ticket_side_conversation": (
        "Nothing is emailed until you post into it",
        "emails the address and CCs",
    ),
    "create_ticket_approval": ("Nothing is emailed until you post into it", "emails the approvers"),
    "create_project": ("project-created notification the app sends (Gorelo does not say who receives it)",),
    "create_project_comment": (
        "emails nobody",
        "a side_conversation comment emails that conversation's recipients",
        "an approval comment the approvers",
        "Tell the user who will be emailed before posting anything that is not private",
    ),
    "create_project_section": ("nothing is emailed",),
    "create_task_approval": ("Nothing is emailed until a comment is posted into the approval",),
    "create_task_side_conversation": ("emails nobody", "Nothing is emailed until a comment is posted into it"),
    "post_alert": ("notify technicians",),
    # create_invoice (a Draft) and get/list tools never talk about email: a draft has no recipients parameter at all,
    # and it says it is "not pushed to accounting or sent to anyone"
    "create_approved_invoice": (
        "recipient_emails, when given, are the addresses Gorelo sends the invoice to",
        "it does not say whether creating the invoice already sends it",
        "tell the user who",
    ),
    "upload_attachment": ("Nothing is emailed",),
    "delete_project_comment": ("nothing is emailed",),
    "delete_project_task": ("nothing is emailed",),
    "delete_ticket_comment": ("Emails nobody",),
}


async def test_every_write_that_talks_about_email_or_notifications_says_who_gets_them(server_factory):
    tools = await all_tools(server_factory)
    talking = {
        name
        for name, tool in tools.items()
        if kind_of(name) != "read" and EMAIL_ACTION.search(flat(tool.description))
    }
    assert talking == set(EMAIL_WHO), (
        f"classify these in EMAIL_WHO: {sorted(talking - set(EMAIL_WHO))}; "
        f"no longer talking about email: {sorted(set(EMAIL_WHO) - talking)}"
    )
    for name, fragments in EMAIL_WHO.items():
        description = flat(tools[name].description)
        for fragment in fragments:
            assert fragment in description, f"{name} must say {fragment!r}: {description}"


# --------------------------------------------------------------------------
# Paged lists say how to read on
# --------------------------------------------------------------------------

PAGED_LISTS = {
    "list_tickets",
    "list_ticket_comments",
    "list_time_entries",
    "list_clients",
    "list_contacts",
    "list_agents",
    "list_custom_assets",
    "list_forms",
    "list_form_responses",
    "list_projects",
    "list_project_comments",
    "list_project_tasks",
    "list_invoices",
    "list_items",
    "list_uptime_checks",
    "list_contracts",
    "list_alerts",
}


async def test_the_paged_lists_are_exactly_the_tools_with_a_cursor(server_factory):
    tools = await all_tools(server_factory)
    assert {name for name, tool in tools.items() if "cursor" in tool.inputSchema["properties"]} == PAGED_LISTS
    assert all(kind_of(name) == "read" for name in PAGED_LISTS)


async def test_every_paged_list_says_to_pass_next_cursor_back_until_has_more_is_false(server_factory):
    tools = await all_tools(server_factory)
    for name in sorted(PAGED_LISTS):
        tool = tools[name]
        said = flat(tool.description) + " " + flat(tool.inputSchema["properties"]["cursor"]["description"])
        assert "next_cursor" in said, f"{name} never mentions next_cursor"
        assert "until has_more is false" in said, f"{name} does not say to repeat until has_more is false: {said}"
        assert re.search(r"same (?:filters|form_id)", said, re.IGNORECASE), f"{name} does not say to keep the filters"


# --------------------------------------------------------------------------
# Destructive tools are named only on the condition that deletes are enabled
# --------------------------------------------------------------------------


# The words that must follow the name of a destructive tool in a text that is not itself destructive. Every delete or
# void tool is "when deletes are enabled". create_approved_invoice does not delete anything: the flag that registers it
# is the same one, but the model is told "when enabled", which is true of it and does not promise a delete.
GATE_CONDITIONS = {"create_approved_invoice": ", when enabled"}
DELETE_CONDITION = ", when deletes are enabled"


def condition_for(target):
    return GATE_CONDITIONS.get(target, DELETE_CONDITION)


async def test_no_tool_text_names_a_destructive_tool_without_saying_deletes_must_be_enabled(server_factory):
    tools = await all_tools(server_factory)
    destructive = sorted(name for name in tools if kind_of(name) == "destructive")
    mentions = []
    for name, tool in tools.items():
        if name in destructive:  # a destructive tool and the ones it names exist together
            continue
        for where, text in texts_of(tool).items():
            for target in destructive:
                if re.search(rf"\b{target}\b", text):
                    mentions.append((name, where, target))
                    assert condition_for(target).lstrip(", ") in text, f"{name} ({where}) names {target} without the condition: {text}"
    # the tools that name a destructive tool today, so a removed or renamed mention is noticed
    assert {(name, target) for name, _, target in mentions} == {
        ("create_invoice", "create_approved_invoice"),
        ("create_invoice", "delete_invoice"),
        ("create_item", "delete_item"),
        ("create_ticket_comment", "delete_ticket_comment"),
        ("update_time_entry", "delete_time_entry"),
    }


async def test_a_tool_text_that_names_a_delete_tool_never_offers_it_unconditionally(server_factory):
    # the condition has to sit right next to the name: "delete_item, when deletes are enabled" or, for the one gated
    # tool that approves instead of deleting, "create_approved_invoice, when enabled"
    tools = await all_tools(server_factory)
    destructive = sorted(name for name in tools if kind_of(name) == "destructive")
    for name, tool in tools.items():
        if name in destructive:
            continue
        for where, text in texts_of(tool).items():
            for target in destructive:
                for match in re.finditer(rf"\b{target}\b", text):
                    tail = text[match.end() : match.end() + 40]
                    assert tail.startswith(condition_for(target)), f"{name} ({where}): {text[match.start():match.end() + 40]!r}"


async def test_every_destructive_tool_has_a_condition_phrase_a_delete_tool_by_default_and_a_listed_one_otherwise(server_factory):
    tools = await all_tools(server_factory)
    destructive = {name for name in tools if kind_of(name) == "destructive"}
    assert set(GATE_CONDITIONS) <= destructive
    # a delete or void tool never gets the shorter phrase, and a gated tool that does something else never gets the
    # delete phrase: a new destructive tool that is not a delete has to be listed in GATE_CONDITIONS to pass
    assert not any(name.startswith("delete_") for name in GATE_CONDITIONS)
    assert destructive - set(GATE_CONDITIONS) == {name for name in destructive if name.startswith("delete_")}
    assert {condition_for(name) for name in destructive if name.startswith("delete_")} == {DELETE_CONDITION}
    assert condition_for("create_approved_invoice") == ", when enabled" != DELETE_CONDITION


async def test_create_invoice_points_at_the_gated_approval_tool_with_the_gate_phrase_and_nowhere_else(server_factory):
    tools = await all_tools(server_factory)
    text = flat(tools["create_invoice"].description)
    assert "for that use create_approved_invoice, when enabled." in text
    assert "delete_invoice, when deletes are enabled" in text
    # the other invoice tools (list, get, export) never advertise a gated tool: a model that cannot see it must not be told to use it
    for name in ("list_invoices", "get_invoice", "export_invoice_pdf"):
        assert not re.search(r"create_approved_invoice|delete_invoice", " ".join(texts_of(tools[name]).values())), name


# --------------------------------------------------------------------------
# A void happens in Gorelo only: the accounting system keeps its copy open (observed with Xero)
# --------------------------------------------------------------------------

# Every spelling of voiding in a tool text. The status name ("4 Void" in the status filter of list_invoices) is a label,
# not an action, so the list below names it apart.
VOID_WORD = re.compile(r"\bvoid(?:s|ed|ing)?\b", re.IGNORECASE)
TOOLS_THAT_APPROVE_OR_VOID = ("create_approved_invoice", "delete_invoice")


async def test_the_two_tools_that_approve_or_void_an_invoice_say_the_accounting_copy_stays_open(server_factory):
    """Gorelo pushes an Approved invoice to Xero, and the
    DELETE that voids it (StatusId 4, get_invoice shows Void) leaves the Xero copy open. A model
    that approves or voids an invoice has to tell the user to void the copy there, so both tools say so in the text it
    reads before it acts (said before the instruction to ask the user), and no tool text claims that voiding takes the
    approval back."""
    tools = await all_tools(server_factory)
    for name in TOOLS_THAT_APPROVE_OR_VOID:
        text = flat(tools[name].description)
        assert "Gorelo ONLY" in text, f"{name} does not say that a void happens in Gorelo only: {text}"
        assert "accounting system stays open" in text, f"{name} does not say the accounting copy stays open: {text}"
        assert "who must void it there too" in text, f"{name} does not say the user must void it there too: {text}"
        assert "observed with Xero" in text, f"{name} does not say that this was seen live: {text}"
        assert text.index("Gorelo ONLY") < text.index("Ask the user first"), f"{name} says it only after asking: {text}"
    for name, tool in tools.items():
        for where, text in texts_of(tool).items():
            assert "except by voiding" not in text, f"{name} ({where}) says that voiding takes the approval back: {text}"
    # a Void invoice can still show AmountDue (seen live), so the void tool says to read Status first (and so do the readers)
    assert "A Void invoice can still show AmountDue: read Status first." in flat(tools["delete_invoice"].description)


async def test_no_other_tool_text_talks_about_voiding_so_none_can_leave_the_accounting_copy_out(server_factory):
    # A tool text that starts talking about voiding (a new invoice tool, a reworded filter) must be classified here, and a
    # text that voids or approves must carry the Gorelo-only warning of the two tools above.
    # (the billing toolset: the time tools talk about "void entries", a status of a time entry that has nothing to do with
    # an invoice or an accounting system)
    tools = {name: tool for name, tool in (await all_tools(server_factory)).items() if toolset_of(name) == "billing"}
    assert {"list_invoices", "delete_invoice", "create_approved_invoice", "create_invoice"} <= set(tools)
    talking = {name for name, tool in tools.items() if any(VOID_WORD.search(text) for text in texts_of(tool).values())}
    # the two read tools only name the status ("4 Void", "A Void invoice can still show AmountDue"): neither voids anything
    assert talking == {*TOOLS_THAT_APPROVE_OR_VOID, "list_invoices", "get_invoice"}
    for name in ("list_invoices", "get_invoice"):
        mentions = [text for text in texts_of(tools[name]).values() if VOID_WORD.search(text)]
        assert all(re.search(r"\bVoid\b", text) and "voided" not in text and "voids" not in text for text in mentions), name
    assert [text for text in texts_of(tools["list_invoices"]).values() if "4 Void" in text] == ["1 Draft, 3 Paid, 4 Void, 5 Approved."]
    for name in ("list_invoices", "get_invoice"):
        assert "A Void invoice can still show AmountDue: read Status first." in flat(tools[name].description), name


# --------------------------------------------------------------------------
# a blank cursor is refused by every paged list, locally
# --------------------------------------------------------------------------

# The arguments each paged list needs besides the cursor (every other paged list needs none).
PAGED_BASE_ARGS = {
    "list_ticket_comments": {"ticket_id": uid(1)},
    "list_form_responses": {"form_id": "form-1"},
    "list_project_comments": {"project_id": uid(1)},
    "list_project_tasks": {"project_id": uid(1)},
}


@pytest.mark.parametrize("name", sorted(PAGED_LISTS))
@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
async def test_every_paged_list_refuses_a_blank_cursor_before_any_http_call(server_factory, mock_gorelo, name, blank):
    # a blank cursor is a caller mistake, not "the first page" (that is no cursor at all): it is refused naming the param
    server = server_factory(toolsets=frozenset(TOOLSETS), destructive=True)
    text = await call_tool_error(server, name, {**PAGED_BASE_ARGS.get(name, {}), "cursor": blank})
    assert "cursor: must not be empty or whitespace only" in text, (name, text)
    assert mock_gorelo.requests == [], name


# --------------------------------------------------------------------------
# every clear_fields lists the names it accepts in the schema
# --------------------------------------------------------------------------


async def test_every_clear_fields_parameter_lists_its_accepted_names_in_the_schema(server_factory):
    tools = await all_tools(server_factory)
    with_clear = {name for name, tool in tools.items() if "clear_fields" in tool.inputSchema["properties"]}
    assert with_clear == {"update_contact", "update_item", "update_project", "update_project_task", "update_ticket"}
    for name in sorted(with_clear):
        clear = tools[name].inputSchema["properties"]["clear_fields"]
        assert clear["type"] == "array", name
        names = clear["items"].get("enum")
        assert names and len(names) == len(set(names)) and all(isinstance(n, str) for n in names), name
        # every offered name is a parameter of the same tool (a name the tool does not have could never be cleared)
        assert set(names) <= set(tools[name].inputSchema["properties"]) - {"clear_fields"}, name


# --------------------------------------------------------------------------
# pointing at a project tool outside the projects toolset says which toolset it needs
# --------------------------------------------------------------------------


async def test_a_tool_outside_the_projects_toolset_that_points_at_a_project_tool_names_the_toolset(server_factory):
    tools = await all_tools(server_factory)
    project_tools = sorted(spec.name for spec in REGISTRY.specs if spec.toolset == "projects")
    assert "list_project_tasks" in project_tools and "list_projects" in project_tools
    mentions = set()
    for name, tool in tools.items():
        if toolset_of(name) == "projects":
            continue
        for where, text in texts_of(tool).items():
            for target in project_tools:
                if re.search(rf"\b{target}\b", text):
                    mentions.add((name, where, target))
                    assert "projects toolset" in text, f"{name} ({where}) points at {target} without the toolset: {text}"
    # the places that point at a project tool today, so a removed or new pointer is noticed
    assert mentions == {
        ("create_form_submission_link", "parameter task_id", "list_project_tasks"),
        ("create_time_entry", "parameter task_id", "list_project_tasks"),
        ("list_time_entries", "parameter task_ids", "list_project_tasks"),
        ("upload_attachment", "parameter item_id", "list_project_tasks"),
        ("upload_attachment", "parameter item_id", "list_projects"),
    }


async def test_the_task_id_pointers_use_the_same_phrase(server_factory):
    tools = await all_tools(server_factory)
    pointers = (("create_time_entry", "task_id"), ("list_time_entries", "task_ids"), ("create_form_submission_link", "task_id"))
    for name, param in pointers:
        text = flat(tools[name].inputSchema["properties"][param]["description"])
        assert "(list_project_tasks, projects toolset)" in text, (name, text)


# --------------------------------------------------------------------------
# attachments are the name and url upload_attachment returned, nothing else
# --------------------------------------------------------------------------


async def test_every_attachments_parameter_says_to_pass_only_the_name_and_url_from_upload_attachment(server_factory):
    tools = await all_tools(server_factory)
    with_attachments = {name for name, tool in tools.items() if "attachments" in tool.inputSchema["properties"]}
    assert with_attachments == {"create_project_comment", "create_ticket_comment", "create_time_entry"}
    for name in sorted(with_attachments):
        text = flat(tools[name].inputSchema["properties"]["attachments"]["description"])
        assert re.search(r"Pass only (?:the )?name and url", text), (name, text)
        assert "upload_attachment" in text, (name, text)


# --------------------------------------------------------------------------
# a delete of a comment can be repeated, and a read cannot verify it
# --------------------------------------------------------------------------


async def test_both_comment_delete_tools_say_repeating_is_safe_and_not_to_verify_with_a_read(server_factory):
    tools = await all_tools(server_factory)
    for name in ("delete_ticket_comment", "delete_project_comment"):
        description = flat(tools[name].description)
        assert re.search(r"repeating (?:a delete |it )?is safe", description, re.IGNORECASE), (name, description)
        assert re.search(r"do not (?:verify a delete with one|re-read to verify)", description, re.IGNORECASE), (name, description)
        assert "may still return deleted comments" in description or "may still be returned by reads" in description, name


async def test_upload_attachment_says_a_failed_upload_may_be_stored_and_must_not_be_repeated_without_the_user(server_factory):
    tools = await all_tools(server_factory)
    description = flat(tools["upload_attachment"].description)
    assert "the file may already be stored: do not upload again without asking the user" in description


# --------------------------------------------------------------------------
# no tool text carries a number that belongs to one tenant
# --------------------------------------------------------------------------

# A count of "all" or "only" some kind of record belongs to one tenant, not to the tool. Limits that Gorelo or this server sets (5000 scanned tickets, 20 pages of 50, 200 rows) are fine.
A_COUNT_OF_RECORDS = re.compile(
    r"\b(?:all|only|about|around|currently|there are)\s+\d+\s+"
    r"(?:clients|contacts|agents|assets|tickets|users|technicians|entries|invoices|items|projects|checks|groups)\b",
    re.IGNORECASE,
)


async def test_no_tool_text_states_a_count_that_is_only_true_of_one_tenant(server_factory):
    tools = await all_tools(server_factory)
    for name, tool in tools.items():
        for where, text in texts_of(tool).items():
            assert not A_COUNT_OF_RECORDS.search(text), f"{name} ({where}) states a count of records: {text}"
