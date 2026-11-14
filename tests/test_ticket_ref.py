"""tools/_ticket_ref.py: resolve_ticket_id, the ticket reference resolver shared by the tools that take one ticket.

The lookup behind get_ticket is tested in depth in tests/test_tools_tickets.py; this file tests the shared function through a
tool of its own: the GUID that costs no request, the `tool` and `param` arguments, and the op declaration it demands.
"""

import logging
from typing import Annotated

import pytest
from conftest import call_tool, call_tool_error, envelope, paged_responder, pagination, uid
from fastmcp import Context
from pydantic import Field

import tools._ticket_ref as ticket_ref
from tools._common import GUID_EXAMPLE, Registry, StrictId, client_of
from tools._ticket_ref import TICKET_LIST_OP, check_ticket_reference, looks_like_a_guid, resolve_ticket_id

pytestmark = pytest.mark.anyio

LIST = "/v1/tickets"
T1, T2 = uid(1), uid(2)


def row(n, **over):
    return {"Id": uid(n), "Number": 100 + n, "DisplayNumber": f"TCK-{100 + n}", **over}


def resolver_registry(*, declare=(TICKET_LIST_OP,), tool="demo_resolver"):
    registry = Registry()

    @registry.tool(toolset="core", kind="read", ops=list(declare), name=tool)
    async def demo_resolver(
        ctx: Context,
        ticket_id: Annotated[str | StrictId, Field(description="Ticket GUID, display number or number.")],
    ) -> dict:
        """Resolve a ticket reference."""
        return {"id": await resolve_ticket_id(client_of(ctx), ticket_id, tool=tool)}

    @registry.tool(toolset="core", kind="read", ops=list(declare), name=f"{tool}_named")
    async def demo_named(
        ctx: Context,
        parent_ticket: Annotated[str | StrictId, Field(description="Ticket GUID, display number or number.")],
    ) -> dict:
        """Resolve a ticket reference under another parameter name."""
        return {"id": await resolve_ticket_id(client_of(ctx), parent_ticket, tool=f"{tool}_named", param="parent_ticket")}

    return registry


@pytest.fixture
def server(server_factory):
    return server_factory(registry=resolver_registry(), toolsets={"core"})


def test_the_exported_op_is_the_ticket_list():
    assert TICKET_LIST_OP == "GET /v1/tickets"
    assert ticket_ref.LOOKUP_PAGE_SIZE == 50 and 10 <= ticket_ref.LOOKUP_MAX_PAGES <= 100 and ticket_ref.QUERY_MAX_CHARS == 200


def test_the_module_has_no_tools_and_is_not_imported_by_the_package():
    import tools

    assert "_ticket_ref" not in vars(tools).get("__all__", ()) and not hasattr(tools, "resolve_ticket_id")
    assert not any(spec.fn.__module__ == "tools._ticket_ref" for spec in tools.REGISTRY.specs)


@pytest.mark.parametrize("written", [T1, T1.upper(), T1.replace("-", "")])
async def test_a_guid_passes_through_with_zero_requests(server, mock_gorelo, written):
    assert (await call_tool(server, "demo_resolver", {"ticket_id": written})) == {"id": T1}
    assert mock_gorelo.requests == []


async def test_a_display_number_is_looked_up_with_query_and_the_tool_is_logged(server, mock_gorelo, caplog):
    caplog.set_level(logging.INFO, logger="gorelo_client")
    mock_gorelo.on("GET", LIST, envelope([row(1), row(2)], pagination(None, 2)))
    assert (await call_tool(server, "demo_resolver", {"ticket_id": "tck-102"})) == {"id": T2}
    request = mock_gorelo.last
    assert request.query == {"Query": "tck-102", "PageSize": "50"}
    assert any("tool=demo_resolver op=GET /v1/tickets" in record.getMessage() for record in caplog.records)


async def test_a_number_as_text_or_as_an_integer_is_looked_up(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, envelope([row(1)], pagination(None, 1)))
    assert (await call_tool(server, "demo_resolver", {"ticket_id": "101"})) == {"id": T1}
    assert (await call_tool(server, "demo_resolver", {"ticket_id": 101})) == {"id": T1}
    assert [r.query["Query"] for r in mock_gorelo.requests] == ["101", "101"]


async def test_the_messages_name_the_parameter_it_was_given(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, envelope([row(1)], pagination(None, 1)))
    expected = (
        "{p}: no ticket has the number or display number '999' (Gorelo returned 1 ticket(s) matching that text, none with that "
        "exact number). Pass a ticket GUID, a number such as 1234 or a display number such as TCK-1234; to search titles use "
        "list_tickets with query"
    )
    assert await call_tool_error(server, "demo_resolver", {"ticket_id": "999"}) == expected.format(p="ticket_id")
    assert await call_tool_error(server, "demo_resolver_named", {"parent_ticket": "999"}) == expected.format(p="parent_ticket")
    blank = await call_tool_error(server, "demo_resolver_named", {"parent_ticket": "  "})
    assert blank.startswith("parent_ticket: must not be empty; give a ticket GUID")
    long = await call_tool_error(server, "demo_resolver_named", {"parent_ticket": "x" * 201})
    assert long == "parent_ticket: at most 200 characters, got 201"
    zero = await call_tool_error(server, "demo_resolver_named", {"parent_ticket": 0})
    assert zero == "parent_ticket: expected a positive whole number such as 123, got zero or a negative number"


async def test_several_matches_and_an_inconclusive_walk_name_the_parameter(server, mock_gorelo, monkeypatch):
    mock_gorelo.on("GET", LIST, envelope([row(1, Number=7), row(2, Number=7)], pagination(None, 2)))
    text = await call_tool_error(server, "demo_resolver_named", {"parent_ticket": "7"})
    assert text.startswith("parent_ticket: '7' matches several tickets: TCK-101 (Id ") and "Pass the ticket GUID of the one you mean" in text
    mock_gorelo._routes.clear()
    monkeypatch.setattr(ticket_ref, "LOOKUP_MAX_PAGES", 2)
    mock_gorelo.on("GET", LIST, paged_responder([[row(n)] for n in range(1, 5)]))
    text = await call_tool_error(server, "demo_resolver_named", {"parent_ticket": "9"})
    assert text.startswith("parent_ticket: '9' was not found in the first 2 of 4 tickets") and "so it is inconclusive" in text


async def test_a_tool_that_does_not_declare_the_ticket_list_cannot_use_the_resolver(server_factory, mock_gorelo):
    server = server_factory(registry=resolver_registry(declare=("GET /v1/tickets/{ticketId}",)), toolsets={"core"})
    assert (await call_tool(server, "demo_resolver", {"ticket_id": T1})) == {"id": T1}  # a GUID needs no request
    text = await call_tool_error(server, "demo_resolver", {"ticket_id": "TCK-101"})
    assert "GET /v1/tickets is not one of the operations that tool 'demo_resolver' declares" in text
    assert mock_gorelo.requests == []


async def test_a_row_without_an_id_or_a_row_that_is_not_an_object_is_a_shape_error(server, mock_gorelo):
    mock_gorelo.on("GET", LIST, envelope([{"Number": 101, "DisplayNumber": "TCK-101"}], pagination(None, 1)))
    assert "a ticket row has no Id; refusing to guess" in await call_tool_error(server, "demo_resolver", {"ticket_id": "101"})
    mock_gorelo._routes.clear()
    mock_gorelo.on("GET", LIST, envelope(["x"], pagination(None, 1)))
    assert "a ticket row is not an object; refusing to guess" in await call_tool_error(server, "demo_resolver", {"ticket_id": "101"})


# --------------------------------------------------------------------------
# Text that is plainly an attempt at a GUID but is not one: refused locally, never searched as a number
# --------------------------------------------------------------------------

BARE = T1.replace("-", "")
MALFORMED = {
    "one character short": T1[:-1],
    "one character short, no hyphens": BARE[:-1],
    "one character long": T1 + "0",
    "two characters short": T1[:-2],
    "braces": "{" + T1 + "}",
    "braces and spaces": "  { " + T1 + " }  ",
    "urn prefix": "urn:uuid:" + T1,
    "urn prefix, upper case": "URN:UUID:" + T1.upper(),
    "urn prefix, short": "urn:uuid:123",
    "a hyphen too many": T1[:8] + "-" + T1[8:],
    "letters of a GUID, non-canonical hyphens": "-".join([BARE[:6], BARE[6:14], BARE[14:20], BARE[20:]]),
}


@pytest.mark.parametrize("text", MALFORMED.values(), ids=MALFORMED.keys())
async def test_a_malformed_guid_is_refused_with_zero_requests_and_named_as_such(server, mock_gorelo, text):
    message = await call_tool_error(server, "demo_resolver_named", {"parent_ticket": text})
    assert message == (
        f"parent_ticket: the ticket GUID is malformed (it must look like {GUID_EXAMPLE}, 32 hex digits with or without "
        "hyphens, no braces and no urn: prefix). For a ticket number pass 1234 or a display number such as TCK-1234"
    )
    assert "no ticket has the number" not in message and text.strip() not in message  # the value is never echoed
    assert mock_gorelo.requests == []


async def test_a_malformed_guid_names_the_default_parameter_through_a_tool(server, mock_gorelo):
    message = await call_tool_error(server, "demo_resolver", {"ticket_id": T1[:-1]})
    assert message.startswith("ticket_id: the ticket GUID is malformed")
    assert mock_gorelo.requests == []


@pytest.mark.parametrize(
    "text",
    ["1234", "7", "TCK-1234", "tck-1234", "MSP-77", "INC-1", "ABC-123", "DEAD-BEEF-1", "FACADE-1234567", "A-1", "{TCK-1234}", "x" * 31],
)
def test_a_number_a_display_number_or_other_short_text_is_never_a_guid_attempt(text):
    assert looks_like_a_guid(text) is False
    assert check_ticket_reference("ticket_id", text) == text


@pytest.mark.parametrize("text", [1234, "1234", 2**63 - 1])
def test_a_real_number_passes_the_local_check(text):
    assert check_ticket_reference("ticket_id", text) == str(text)


def test_a_display_number_shaped_text_is_never_refused_however_long_or_hex_like_it_is():
    long_prefix = "ABCDEF0123456789" * 2  # 32 hex digits as a tenant prefix: still PREFIX-DIGITS, so a display number
    assert looks_like_a_guid(f"{long_prefix}-1234") is False
    assert looks_like_a_guid(long_prefix) is True  # the same digits without the number are a bare GUID


def test_the_threshold_is_30_characters_of_hex_and_hyphens_or_a_urn_prefix():
    assert ticket_ref.GUID_ATTEMPT_MIN_CHARS == 30
    assert looks_like_a_guid("a" * 30) and not looks_like_a_guid("a" * 29)
    assert looks_like_a_guid("urn:x") and looks_like_a_guid("  URN:uuid:x")
    assert not looks_like_a_guid("g" * 40)  # not hex: plain text, searched like any other reference


async def test_whitespace_around_a_reference_is_trimmed_on_purpose(server, mock_gorelo):
    assert (await call_tool(server, "demo_resolver", {"ticket_id": f"  {T1}\n"})) == {"id": T1}  # a GUID with spaces is a GUID
    assert mock_gorelo.requests == []
    mock_gorelo.on("GET", LIST, envelope([row(1)], pagination(None, 1)))
    assert (await call_tool(server, "demo_resolver", {"ticket_id": "  tck-101 "})) == {"id": T1}
    assert mock_gorelo.last.query["Query"] == "tck-101"
    assert "Whitespace around the reference is trimmed on purpose" in ticket_ref.__doc__


async def test_a_valid_guid_is_still_resolved_with_zero_requests_after_the_check(server, mock_gorelo):
    for written in (T1, T1.upper(), BARE, f" {T1} "):
        assert (await call_tool(server, "demo_resolver", {"ticket_id": written})) == {"id": T1}
    assert mock_gorelo.requests == []


# --------------------------------------------------------------------------
# Ambiguity is detected within the page of the first match only
# --------------------------------------------------------------------------


async def test_a_second_exact_match_on_a_later_page_is_never_seen_and_no_further_page_is_read(server, mock_gorelo):
    # pinned on purpose (requests are scarce): the walk stops at the page of the first exact match
    pages = [
        [row(1, Number=7, DisplayNumber="TCK-7"), row(2, Number=900, DisplayNumber="TCK-900")],
        [row(3, Number=7, DisplayNumber="OTHER-7")],
        [row(4, Number=7, DisplayNumber="THIRD-7")],
    ]
    mock_gorelo.on("GET", LIST, paged_responder(pages))
    assert (await call_tool(server, "demo_resolver", {"ticket_id": "7"})) == {"id": T1}
    assert [r.query for r in mock_gorelo.requests] == [{"Query": "7", "PageSize": "50"}]  # page 2 and 3 were not requested


async def test_two_exact_matches_on_the_same_page_are_still_ambiguous(server, mock_gorelo):
    pages = [[row(1, Number=7), row(2, Number=7, DisplayNumber="OTHER-7")], [row(3, Number=7, DisplayNumber="THIRD-7")]]
    mock_gorelo.on("GET", LIST, paged_responder(pages))
    text = await call_tool_error(server, "demo_resolver", {"ticket_id": "7"})
    assert "matches several tickets" in text and "THIRD-7" not in text
    assert len(mock_gorelo.requests) == 1


def test_both_docstrings_say_where_ambiguity_is_detected_and_why_that_is_enough():
    module_doc = " ".join(ticket_ref.__doc__.split())
    function_doc = " ".join(ticket_ref._find_ticket_id.__doc__.split())
    assert "Ambiguity is detected WITHIN THE PAGE where the first exact match is found" in module_doc
    assert "reads no further one" in module_doc
    assert "Ambiguity is detected only within the page where the first match is found" in function_doc
    assert "a second exact match on a later page is never seen" in function_doc
    assert "tenant prefix plus the ticket number" in module_doc and "do not share a reference" in module_doc
    assert "tenant prefix plus the number" in function_doc and "do not share a reference" in function_doc
