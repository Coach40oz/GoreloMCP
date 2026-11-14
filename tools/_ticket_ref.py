"""Ticket references, shared by every tool that takes one ticket: a GUID, a number or a display number.

This is a helper module with no tools (like tools/_common.py); tools/__init__.py does not import it, the tool modules
that need it do.

    reference = check_ticket_reference(param, ticket_id)            # local only, zero requests
    ticket = await resolve_ticket_id(client, ticket_id, tool="get_ticket")

resolve_ticket_id returns the ticket GUID. A GUID passes through guid() with ZERO requests. Anything else (1234, TCK-1234)
is looked up with GET /v1/tickets and the Query filter: the rows whose Number or DisplayNumber equals the text
(case-insensitive) are kept, the walk stops at the first page that holds a match, and it never reads more than
LOOKUP_MAX_PAGES pages of LOOKUP_PAGE_SIZE tickets. No match, several matches and an inconclusive walk are ValueErrors that
name `param` and say which ticket GUID, number or display number to pass instead.

Whitespace around the reference is trimmed on purpose (a pasted " TCK-1234 " or a GUID copied with a trailing newline is
the same reference), so a GUID with spaces around it is a GUID. The shared guid() helper trims nothing; this module does
it first.

Text that is plainly an attempt at a GUID but is not a valid one (a GUID one character short, "{guid}" in braces, a
"urn:uuid:" prefix) is refused locally, with zero requests, by a message that says the ticket GUID is malformed and names
the parameter. It is never searched as a ticket number: Gorelo would find nothing and the model would be told that no ticket
has that number, which hides the real mistake. The exact rule is in looks_like_a_guid. A real number (1234) and a display
number (TCK-1234, any tenant prefix) are never refused by it.

Ambiguity is detected WITHIN THE PAGE where the first exact match is found, not across the whole result: the walk stops at
that page and reads no further one, because requests are scarce (Gorelo limits the whole API key, about one request per
second, shared by every tool). That is enough in practice: a display
number is the tenant prefix plus the ticket number, and a ticket number is unique in the tenant, so two different tickets
do not share a reference; the exact match is at most one ticket, and the search puts the tickets that merely contain the
text around it. A second exact match on a LATER page is therefore not expected and is not seen: the lookup returns the first
match. A caller who needs certainty passes the ticket GUID.

A tool that uses the resolver must declare TICKET_LIST_OP in its ops=[...] (the client refuses an operation the calling tool
did not declare) and must name itself in `tool`, so the request log and that check name the calling tool. Its field_map
should map the parameter to "Query" (the lookup filter) so a Gorelo 400 on it reads as the parameter. A tool that has local
checks of its own calls check_ticket_reference first and resolve_ticket_id only when it needs the GUID, so a call that is
refused locally never spends a lookup request.
"""

import re
from typing import Any

from gorelo_client import GoreloAPIError, GoreloClient
from tools._common import GUID_EXAMPLE, guid, positive_id

TICKET_LIST_OP = "GET /v1/tickets"

QUERY_MAX_CHARS = 200  # GET /v1/tickets Query: "Up to 200 characters"

# A number or display number is looked up with Query (50 per page) and the walk stops at the first page that holds an exact
# match; this bounds the walk for a short number that matches many tickets.
LOOKUP_PAGE_SIZE = 50
LOOKUP_MAX_PAGES = 20


def _shape_error(detail: str) -> GoreloAPIError:
    """A successful READ whose answer cannot be used (nothing was written, so it is not write_unconfirmed)."""
    return GoreloAPIError(f"{TICKET_LIST_OP}: {detail}", status=200, op_key=TICKET_LIST_OP, kind="shape")


def _ticket_reference(param: str, value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        positive_id(param, value)
    text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if not text:
        raise ValueError(
            f"{param}: must not be empty; give a ticket GUID, a number such as 1234 or a display number such as "
            "TCK-1234"
        )
    if len(text) > QUERY_MAX_CHARS:
        raise ValueError(f"{param}: at most {QUERY_MAX_CHARS} characters, got {len(text)}")
    return text


# A ticket reference is "plainly an attempt at a GUID" when, after the surrounding whitespace is trimmed and one pair of braces
# is taken off, it either starts with "urn:" (any case: "urn:uuid:...") or is made ONLY of hex digits and hyphens and has at
# least GUID_ATTEMPT_MIN_CHARS characters, and is not shaped like a display number (see below). Why 30: a GUID is 32 hex digits
# (36 characters with hyphens), so a GUID that lost one or two characters, or has a hyphen too few, still reaches 30, while a
# ticket number is at most 19 digits (int64) and a display number is a tenant prefix, a hyphen and that number (the
# usual shape is TCK-1234; the prefix length is not documented, but a prefix of about 25 characters would be needed to
# reach the threshold), so a display number is far below 30 characters. As a second guard a text shaped PREFIX-DIGITS (one hyphen, then only digits) is never
# a GUID attempt, whatever its length or letters, so a display number can never be refused by this rule.
GUID_ATTEMPT_MIN_CHARS = 30
_HEX_AND_HYPHENS = re.compile(r"[0-9A-Fa-f-]+")
_DISPLAY_NUMBER_SHAPE = re.compile(r"[^-\s]+-[0-9]+")


def looks_like_a_guid(text: str) -> bool:
    """True for text that is plainly an attempt at a ticket GUID (see the rule above); only meant for text that guid()
    already refused. Pure and local."""
    body = text.strip()
    if body.casefold().startswith("urn:"):
        return True
    if _DISPLAY_NUMBER_SHAPE.fullmatch(body):
        return False
    if body.startswith("{"):
        body = body[1:]
    if body.endswith("}"):
        body = body[:-1]
    body = body.strip()
    return len(body) >= GUID_ATTEMPT_MIN_CHARS and _HEX_AND_HYPHENS.fullmatch(body) is not None


def check_ticket_reference(param: str, value: Any) -> str:
    """Every local check of a ticket reference, with ZERO requests: returns the trimmed text (a GUID in any accepted form, a
    number or a display number). Raises ValueError naming `param` for a blank or too long reference, for a number below 1 and
    for a malformed GUID (see looks_like_a_guid). resolve_ticket_id runs it first; a tool with its own local checks runs it up
    front too, so that a refusal never follows a lookup."""
    reference = _ticket_reference(param, value)
    try:
        guid(param, reference)
    except ValueError:
        if looks_like_a_guid(reference):
            raise ValueError(
                f"{param}: the ticket GUID is malformed (it must look like {GUID_EXAMPLE}, 32 hex digits with or without "
                "hyphens, no braces and no urn: prefix). For a ticket number pass 1234 or a display number such as TCK-1234"
            ) from None
    return reference


def _is_reference_match(row: dict[str, Any], wanted: str) -> bool:
    number = row.get("Number")
    if number is not None and not isinstance(number, bool) and str(number).casefold() == wanted:
        return True
    display = row.get("DisplayNumber")
    return isinstance(display, str) and display.strip().casefold() == wanted


def _row_id(row: dict[str, Any]) -> str:
    row_id = row.get("Id")
    if not isinstance(row_id, str) or not row_id.strip():
        raise _shape_error("a ticket row has no Id; refusing to guess")
    return row_id.strip()


def _label(row: dict[str, Any]) -> str:
    return f"{row.get('DisplayNumber') or row.get('Number')} (Id {row.get('Id')})"


async def _find_ticket_id(client: GoreloClient, reference: str, *, tool: str, param: str) -> str:
    """Resolve a ticket number or display number to the ticket GUID: search with Query, keep the rows whose
    Number or DisplayNumber equals the text (case-insensitive). Stops at the first page with a match, and
    never reads more than LOOKUP_MAX_PAGES pages.

    Ambiguity is detected only within the page where the first match is found: a second exact match on a later page is
    never seen, because no further page is read (requests are scarce). That is enough in practice, because a display
    number is the tenant prefix plus the number, so two different tickets do not share a reference (module docstring)."""
    wanted = reference.casefold()
    matches: dict[str, dict[str, Any]] = {}
    seen_cursors: set[str] = set()
    cursor: str | None = None
    rows_read = 0
    total: int | None = None
    more = False
    for _ in range(LOOKUP_MAX_PAGES):
        page = await client.get_page(
            TICKET_LIST_OP, query={"Query": reference}, page_size=LOOKUP_PAGE_SIZE, cursor=cursor, tool=tool
        )
        rows_read += len(page.items)
        total = page.total_count
        for row in page.items:
            if not isinstance(row, dict):
                raise _shape_error("a ticket row is not an object; refusing to guess")
            if _is_reference_match(row, wanted):
                matches.setdefault(_row_id(row), row)
        more = page.has_more
        if matches or not more:
            break
        cursor = page.next_cursor
        if cursor is None or cursor in seen_cursors:
            raise _shape_error("Gorelo returned a cursor it had already served; refusing to loop")
        seen_cursors.add(cursor)
    if len(matches) == 1:
        return next(iter(matches))
    if matches:
        listing = "; ".join(_label(row) for row in matches.values())
        raise ValueError(
            f"{param}: {reference!r} matches several tickets: {listing}. Pass the ticket GUID of the one you mean"
        )
    if more:
        shown_total = f" of {total}" if total is not None else ""
        raise ValueError(
            f"{param}: {reference!r} was not found in the first {rows_read}{shown_total} tickets Gorelo returned for "
            "that text (the lookup reads at most "
            f"{LOOKUP_MAX_PAGES} pages), so it is inconclusive. Pass the ticket GUID, or a longer display number "
            "such as TCK-1234"
        )
    found = (
        f"Gorelo returned {rows_read} ticket(s) matching that text, none with that exact number"
        if rows_read
        else "Gorelo found no ticket matching that text"
    )
    raise ValueError(
        f"{param}: no ticket has the number or display number {reference!r} ({found}). Pass a ticket GUID, "
        "a number such as 1234 or a display number such as TCK-1234; to search titles use list_tickets with query"
    )


async def resolve_ticket_id(client: GoreloClient, value: Any, *, tool: str, param: str = "ticket_id") -> str:
    """The ticket GUID for a ticket reference: a GUID (any case, hyphens optional; no request is made), a number (1234 or
    the int 1234) or a display number (TCK-1234). `tool` is the calling tool (it must declare TICKET_LIST_OP); `param` is
    the parameter name the messages use. Surrounding whitespace is trimmed on purpose. Raises ValueError for a bad, malformed
    (GUID-like but invalid), unknown, ambiguous or inconclusive reference, and GoreloAPIError when Gorelo fails or answers a
    shape the lookup cannot use. Ambiguity is only detected within the page where the first match is found (see
    _find_ticket_id)."""
    reference = check_ticket_reference(param, value)
    try:
        return guid(param, reference)
    except ValueError:  # not a GUID (check_ticket_reference refused a malformed one): a number or a display number, found with one search
        return await _find_ticket_id(client, reference, tool=tool, param=param)
