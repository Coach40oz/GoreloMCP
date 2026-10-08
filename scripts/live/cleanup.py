"""Clean up after a live run: delete what its manifest says it created, in reverse creation order.

    python -m scripts.live.cleanup .live-runs/MCPTEST-20991002101500.json [--leftovers] [--void-approved]

Two paths, both behind ONE write-mode LiveGuard with cleanup=True (and allow_approved_invoice=True only with
--void-approved) and both paced at about one request per second:

* the gated delete TOOLS, called through an in-process fastmcp Client on
  build_server(live_settings(), event_hooks={"request": [guard, pacer]}): delete_ticket_comment,
  delete_time_entry (repeated while Outcome is "Reopened", at most 3 calls), delete_item, delete_uptime_check,
  delete_project_task, delete_project_comment and delete_invoice;
* raw soft deletes through a minimal httpx.AsyncClient (X-API-Key, https://api.usw.gorelo.io/v1) for the
  records that deliberately have no tool: tickets, contacts, the temporary client and projects (and, as the
  backstop for an invoice that has no Number or that delete_invoice cannot find by it, the invoice itself).

Sections, side conversations and approvals have no delete of their own; they go with their parent. Comments,
project comments and tasks that could not be deleted on their own are also counted as removed when their ticket
or project was deleted (details ticket_id, project_id and task_id name the parent). A non-private comment
(details private=False) cannot be deleted, so it is never attempted.

An invoice (kind "invoice") is read first, GET /v1/invoices/{id}, and what happens next depends on the status it
reads as. A 404 means it is gone, and so does status 6 (Deleted). A Draft (Status.Id 1) is deleted: with delete_invoice
(its Number, expected status "Draft", confirm=true), or with the raw DELETE backstop, both behind the guard. The backstop
is used when the invoice has no Number, and when delete_invoice refuses BEFORE any DELETE because its lookup by Number
found no invoice ("no invoice has the number": that search can lag behind a write, like the matrix's own lookup, and
nothing was sent): the invoice is the very one this cleanup just read as a Draft by its id. The guard still allows that
DELETE only for an invoice the run created and recorded as a Draft (status_id 1), and the answer must say StatusId 6
(Deleted); anything else is a leftover. Every other refusal of delete_invoice (several matches, another status, a lookup
that failed) and every error that carries an HTTP status leaves the invoice a leftover without a second DELETE.

An Approved invoice (Status.Id 5) that the run recorded with status_id 5 (the write matrix's approved_invoice area made
it Approved, or the label search found it Approved) is voided by this cleanup ONLY with --void-approved (the area voids
the invoice it approved itself, before any cleanup runs, and the cleanup the matrix runs afterwards never does). Without
the flag it is a leftover that needs the user, named by its DisplayNumber: Gorelo pushed it to the connected accounting
system when it was approved, and its outcome says to check it there, gives this command with --void-approved and says
that the command voids in Gorelo only (VOID_COMMAND_NOTE: its copy in the accounting system is voided by hand too). With
the flag it is voided like a Draft is deleted, with delete_invoice (expected status "Approved") or the same raw DELETE
backstop, behind a guard that allows a DELETE of a run-created invoice recorded with status_id 5 and nothing else new,
and the answer must say StatusId 4 (Void); anything else is a leftover. An Approved invoice that was NOT recorded as 5
(a Draft that somebody approved afterwards) is never voided: it is a leftover for the user. A Void invoice (Status.Id 4)
is already void: nothing is sent. A voided invoice cannot be removed, it stays listed as Void, so it is a KNOWN RESIDUE
like the uploaded file below: its outcome ends with "(still listed as Void)", it is reported apart as known undeletable
(neither cleaned nor a leftover) and it never alone turns the exit status to 1. A Paid invoice (Status.Id 3, which
Gorelo neither deletes nor voids) and any other status are never deleted or voided: each stays a leftover that needs the
user, and its outcome names the DisplayNumber and the status. A create (create_invoice or create_approved_invoice) that
was announced without an id (the answer was lost) is searched by the run label, with list_invoices for the test client: an
invoice whose Reference is exactly that label is a run-created record, so it is recorded in the manifest (with whatever
status it has) and then handled by the rules above like any other invoice; nothing else is touched.

Whatever this cleanup voids it voids in Gorelo only: the copy that Gorelo had pushed to the accounting system was not
voided there (seen with Xero), so it is the operator's to void there. That is why the leftover text above
and the help of --void-approved say that the command voids in Gorelo only (VOID_COMMAND_NOTE), and why the report ends,
just before its result line, with a reminder (VOID_REMINDER) for each invoice THIS cleanup voided. An invoice that was found
void, or that the matrix or an earlier cleanup voided, is a known residue and gets no reminder from this one.

An uploaded attachment cannot be deleted through the API at all. Once its parent is deleted it is reported as
"soft-deleted with its ticket; the file itself cannot be deleted through the API" and listed apart, as KNOWN
UNDELETABLE: it is not counted as cleaned, it is not a leftover, the result line does not say "nothing left
over" while one exists, and it never alone turns the exit status to 1 (an attachment whose parent could not be
deleted is a plain leftover).

With --leftovers the leftovers listed in site.local.toml [leftovers] are handled FIRST: each is read (GET) and deleted only if its
name matches (the optional name rules of [leftovers]: client_name_contains for clients, contact_first_name and
contact_last_name_prefix for the contact; a rule left out does not apply); otherwise it is reported and skipped. Names are never printed.

Every outcome is written to the manifest at once, so a cleanup that stops half way can simply be run again.
A record counts as cleaned when the delete succeeded or Gorelo says it is gone (HTTP 404). The summary lists
ids, labels and Gorelo's messages only, never customer data and never the API key. The exit status is 0 when
nothing is left over (no leftover record, no create announced without an id, no listed leftover that failed to
delete; one skipped because its name does not match is fine, and so is a known undeletable record: an uploaded file
or a voided invoice), 1 otherwise, 2 for a usage or setup error.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastmcp import Client

from gorelo_client import GORELO_BASE_URL
from scripts.live._env import EnvError, Pacer, live_settings, scrub
from scripts.live.guard import GuardViolation, LiveGuard
from scripts.live.manifest import Intent, Manifest, Record
from scripts.live import guard as live_guard
from scripts.site_config import SITE, SiteConfigError, site
from server import build_server
from settings import Settings

MAX_REOPEN_ATTEMPTS = 3
PACE_SECONDS = 1.0
LOG_NAMES = ("fastmcp", "mcp", "httpx", "httpcore", "gorelo_client")

# Records without a delete of their own: they go with their parent.
COVERED_KINDS = frozenset({"comment", "project_comment", "task", "section", "side_conversation", "approval"})
# An uploaded file has no delete in the API: it is reported (not cleaned) once its parent is deleted, and apart from
# the leftovers, because nobody can do anything about it. The outcome text ends with UNDELETABLE_REASON.
UNDELETABLE_KINDS = frozenset({"attachment"})
UNDELETABLE_REASON = "the file itself cannot be deleted through the API"
_PARENT_KEYS = (("ticket_id", "ticket"), ("task_id", "task"), ("project_id", "project"))
_RAW_PATHS = {"ticket": "/tickets/{}", "contact": "/contacts/{}", "client": "/clients/{}", "project": "/projects/{}"}
# Not in _RAW_PATHS: an invoice is read first. A Draft is deleted; an Approved invoice recorded as 5 is voided (the raw
# DELETE voids it too) only with --void-approved; any other status is never deleted or voided.
_INVOICE_PATH = "/invoices/{}"
INVOICE_DRAFT, INVOICE_DELETED = 1, 6  # Invoice StatusId of a Draft, and what Gorelo answers a delete of a Draft with
INVOICE_PAID, INVOICE_VOID, INVOICE_APPROVED = 3, 4, 5  # Paid; Void (the answer to a delete of an Approved invoice); Approved
# Every outcome of a voided invoice ends like this. A voided invoice cannot be removed (it stays listed as Void): it is a
# known residue, like an uploaded file, and write_matrix.py builds its own outcome from this suffix.
VOID_RESIDUE = "(still listed as Void)"
# Gorelo's void does not reach the accounting system: the invoice that the approved run had pushed stayed
# open in the accounting system (seen with Xero) after the void in Gorelo, so whoever voids an Approved invoice says, right after,
# that its copy there is the operator's to void. The write matrix says VOID_REMINDER after its own void (a note and a line
# of its summary) and CleanupReport.render says it for each invoice this cleanup voided; write_matrix.py imports it.
VOID_REMINDER = (
    "the void is in Gorelo only: check the accounting system and void the invoice there too "
    "(Gorelo's void is not pushed to the connected accounting system, seen with Xero)"
)
# Said wherever the user is sent to `cleanup --void-approved` (the help of the flag, the leftover text of an Approved
# invoice and the hints of the write matrix that print the command): that command voids in Gorelo only too. The write
# matrix imports it as well.
VOID_COMMAND_NOTE = "this voids it in Gorelo only: void its copy in the accounting system by hand too"
NO_SUCH_NUMBER = "no invoice has the number"  # what delete_invoice says, before any DELETE, when its lookup finds nothing
INVOICE_SEARCH_PAGES = 3  # pages of list_invoices read when an announced create is searched by its label
INVOICE_SEARCH_PAGE_SIZE = 50
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_STATUS = re.compile(r"\(HTTP (\d{3})")
# Outcomes of an listed leftover that mean something went wrong (a deliberate skip or "already gone" is not one).
_LEFTOVER_PROBLEMS = ("delete failed", "skipped: it could not be read", "skipped: Gorelo did not return a record")
_MESSAGE_LIMIT = 160


@dataclass
class _Outcome:
    ok: bool
    text: str
    status: int | None = None
    data: Any = None

    @property
    def gone(self) -> bool:
        return not self.ok and self.status == 404


def is_known_undeletable(record: Record) -> bool:
    """Is this record a known residue that nobody can remove: an uploaded file that the cleanup found its parent deleted
    for (see _note_undeletable), or an invoice that was voided and stays listed as Void (see _note_void)?"""
    outcome = record.outcome or ""
    if record.kind in UNDELETABLE_KINDS:
        return outcome.endswith(UNDELETABLE_REASON)
    return record.kind == "invoice" and outcome.endswith(VOID_RESIDUE)


@dataclass
class CleanupReport:
    """What a cleanup did. `ok` is True when nothing needs a human: no leftover record, no create without an id and
    no listed leftover that failed to delete or could not be read (one skipped because its name does not match,
    or already gone, is fine).

    `undeletable` holds the known undeletable records (an uploaded file whose parent was deleted, a voided invoice that
    stays listed as Void): they are neither cleaned nor leftovers, and they do not make `ok` False. A report is built
    from the manifest by run_cleanup, which keeps them out of `leftovers`; the matrix prints them on a line of their own.

    `voided` names the invoices THIS cleanup voided (DisplayNumber, else Number, else Id, in the order it voided them): the
    report says for each, as its last line before the result, that the void is in Gorelo only (VOID_REMINDER). An invoice
    that was found void, or that the matrix or an earlier cleanup voided, is in `undeletable` but not in `voided`."""

    run_id: str
    cleaned: list[Record] = field(default_factory=list)
    leftovers: list[Record] = field(default_factory=list)
    unresolved: list[Intent] = field(default_factory=list)
    approved: list[dict[str, Any]] = field(default_factory=list)
    undeletable: list[Record] = field(default_factory=list)
    voided: list[str] = field(default_factory=list)

    @property
    def approved_problems(self) -> list[dict[str, Any]]:
        """Configured leftovers that should have been handled but could not be (not the ones skipped on purpose)."""
        return [entry for entry in self.approved if entry["outcome"].startswith(_LEFTOVER_PROBLEMS)]

    @property
    def ok(self) -> bool:
        return not self.leftovers and not self.unresolved and not self.approved_problems

    def render(self) -> str:
        header = f"cleanup of {self.run_id}: {len(self.cleaned)} cleaned, {len(self.leftovers)} left over"
        if self.undeletable:
            header += f", {len(self.undeletable)} known undeletable"
        lines = [header]
        for record in self.cleaned:
            lines.append(f"  cleaned    {record.kind} {record.id} {record.label} ({record.outcome})")
        for record in self.leftovers:
            lines.append(f"  LEFTOVER   {record.kind} {record.id} {record.label} ({record.outcome or 'not attempted'})")
        for intent in self.unresolved:
            lines.append(
                f"  UNRESOLVED {intent.kind} {intent.label}: the create was announced but never recorded; "
                "it may exist in Gorelo, search for the label"
            )
        for record in self.undeletable:
            lines.append(f"  known undeletable {record.kind} {record.id} {record.label}: {record.outcome}")
        for entry in self.approved:
            flag = "PROBLEM  " if entry in self.approved_problems else ""
            lines.append(f"  {flag}listed leftover {entry['kind']} {entry['id']}: {entry['outcome']}")
        for name in self.voided:  # the last thing said before the result: Xero may still hold the invoice open
            lines.append(f"reminder: approved invoice {name}: {VOID_REMINDER}")
        if not self.ok:
            result = "SOMETHING IS LEFT OVER"
        elif self.undeletable:
            count = len(self.undeletable)
            result = f"cleanup complete; {count} known undeletable record{' remains' if count == 1 else 's remain'} (see above)"
        else:
            result = "nothing left over"
        lines.append("result: " + result)
        return "\n".join(lines)


@contextlib.contextmanager
def _quiet_logs() -> Iterator[None]:
    """FastMCP logs every refused tool call with a traceback; the summary already says what failed."""
    loggers = [logging.getLogger(name) for name in LOG_NAMES]
    previous = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        for logger, level in zip(loggers, previous):
            logger.setLevel(level)


def _notes(body: Any) -> str:
    """Gorelo's Notification messages (at most three), or an empty string."""
    items = body.get("Notifications") if isinstance(body, dict) else None
    messages = []
    for item in (items if isinstance(items, list) else [])[:3]:
        message = item.get("Message") if isinstance(item, dict) else None
        if isinstance(message, str) and message.strip():
            text = " ".join(message.split())
            messages.append(text if len(text) <= _MESSAGE_LIMIT else text[: _MESSAGE_LIMIT - 3] + "...")
    return "; ".join(messages)


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


class _Cleaner:
    def __init__(
        self,
        manifest: Manifest,
        guard: LiveGuard,
        tools: Client,
        raw: httpx.AsyncClient,
        secret: str,
        *,
        void_approved: bool = False,
    ) -> None:
        self.manifest = manifest
        self.guard = guard
        self.tools = tools
        self.raw = raw
        self.secret = secret
        self.void_approved = void_approved
        self.voided: list[str] = []  # names of the invoices THIS cleanup voided: the report reminds about each

    def _safe(self, text: str) -> str:
        """Text that goes into the manifest: never the API key, even if Gorelo were to echo it."""
        return scrub(text, self.secret)

    # -- the two ways of talking to Gorelo -----------------------------------

    async def _tool(self, name: str, arguments: dict[str, Any]) -> _Outcome:
        before = len(self.guard.violations)
        try:
            result = await self.tools.call_tool(name, arguments, raise_on_error=False)
        except Exception as exc:
            return _Outcome(False, f"{name} could not be called ({type(exc).__name__})")
        if len(self.guard.violations) > before:
            return _Outcome(False, "the guard refused it: " + self.guard.violations[-1])
        if result.is_error:
            text = " ".join(getattr(block, "text", "") for block in result.content).strip() or f"{name} failed"
            match = _STATUS.search(text)
            return _Outcome(False, " ".join(text.split()), status=int(match.group(1)) if match else None)
        return _Outcome(True, "Deleted", data=result.structured_content)

    async def _raw(self, method: str, path: str) -> _Outcome:
        try:
            response = await self.raw.request(method, path)
        except GuardViolation as exc:
            return _Outcome(False, "the guard refused it: " + str(exc))
        except httpx.HTTPError as exc:
            return _Outcome(
                False,
                f"no answer from Gorelo ({type(exc).__name__}); it may or may not have been applied, run cleanup again",
            )
        body = _json(response)
        status = response.status_code
        if 200 <= status < 300 and isinstance(body, dict) and body.get("IsSuccess") is True:
            return _Outcome(True, "Deleted" if method == "DELETE" else "ok", status=status, data=body.get("Data"))
        notes = _notes(body)
        return _Outcome(False, f"HTTP {status}" + (f": {notes}" if notes else ""), status=status)

    # -- records ---------------------------------------------------------------

    async def run(self, *, leftovers: bool) -> None:
        if leftovers:
            await self._approved_leftovers()
        await self._find_announced_invoices()
        for record in reversed(self.manifest.all_created()):
            if not record.cleaned:
                await self._clean(record)
        self._cover_children()
        self._note_undeletable()

    async def _clean(self, record: Record) -> None:
        kind, ident = record.kind, record.id
        if kind == "invoice":
            await self._clean_invoice(record)
            return
        if kind in _RAW_PATHS:
            outcome = await self._raw("DELETE", _RAW_PATHS[kind].format(ident))
        elif kind == "time_entry":
            outcome = await self._delete_time_entry(ident)
        elif kind == "comment" and record.details.get("private") is False:
            return  # a non-private comment cannot be deleted; it goes with its ticket
        elif kind in ("comment", "project_comment", "task", "item", "uptime"):
            outcome = await self._delete_with_tool(record)
        else:
            return  # section, side_conversation, approval: covered by the parent; attachment: see _note_undeletable
        if outcome.ok:
            self.manifest.cleaned(kind, ident, self._safe(outcome.text))
        elif outcome.gone:
            self.manifest.cleaned(kind, ident, "already gone (HTTP 404)")
        else:
            self.manifest.cleanup_failed(kind, ident, self._safe(outcome.text))

    # -- invoices ----------------------------------------------------------------

    async def _clean_invoice(self, record: Record) -> None:
        """Read the invoice, then act on the status it has (see the module docstring). Only a Draft is deleted, and an
        Approved invoice that the run recorded as Approved is voided only with --void-approved; nothing else is ever
        deleted or voided."""
        ident = record.id
        if is_known_undeletable(record):  # voided already (by the matrix or an earlier cleanup): nothing to read or send
            return
        read = await self._raw("GET", _INVOICE_PATH.format(ident))
        if read.gone:
            self.manifest.cleaned("invoice", ident, "already gone (HTTP 404)")
            return
        if not read.ok:
            self.manifest.cleanup_failed(
                "invoice", ident, self._safe(f"could not be read first ({read.text}); nothing was deleted")
            )
            return
        invoice = read.data
        if not isinstance(invoice, dict):
            self.manifest.cleanup_failed(
                "invoice", ident, "Gorelo did not return an invoice when it was read first; nothing was deleted"
            )
            return
        status = invoice.get("Status")
        raw_id = status.get("Id") if isinstance(status, dict) else None
        status_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None  # true is not the integer 1
        name = _invoice_name(invoice)
        if status_id == INVOICE_DELETED:
            self.manifest.cleaned("invoice", ident, f"already deleted (status {INVOICE_DELETED}, Deleted)")
            return
        if status_id == INVOICE_VOID:  # nothing to send: a voided invoice stays listed and nobody can remove it
            self._note_void(ident, f"already void, a known residue: a voided invoice cannot be removed {VOID_RESIDUE}")
            return
        if status_id == INVOICE_APPROVED:
            if _recorded_status(record) == INVOICE_APPROVED:  # the run itself made it Approved (or found it Approved)
                if self.void_approved:
                    await self._void_approved(ident, invoice, name)
                else:
                    self.manifest.cleanup_failed(
                        "invoice",
                        ident,
                        self._safe(
                            f"left for the user: invoice {name} has status {_shown_status(status, raw_id)}; it was approved "
                            "on create, so Gorelo pushed it to the connected accounting system: check it there, then void "
                            f"it with: python -m scripts.live.cleanup {self.manifest.path} --void-approved "
                            f"({VOID_COMMAND_NOTE})"
                        ),
                    )
                return
        if status_id != INVOICE_DRAFT:
            shown = _shown_status(status, raw_id)
            if status_id == INVOICE_PAID:
                why = "Gorelo refuses to delete or void a Paid invoice, so it stays as it is"
            else:
                why = (
                    "this harness deletes only a Draft, and voids only an Approved invoice it created as Approved "
                    "(with --void-approved)"
                )
            self.manifest.cleanup_failed(
                "invoice", ident, self._safe(f"left for the user: invoice {name} has status {shown}; {why}")
            )
            return
        outcome, how, context = await self._remove_invoice(ident, invoice.get("Number"), "Draft")
        if outcome.ok:
            answered = outcome.data.get("StatusId") if isinstance(outcome.data, dict) else None
            if answered == INVOICE_DELETED:
                self.manifest.cleaned("invoice", ident, f"Deleted (Draft, {how})")
            else:  # a 4 would mean the invoice was Approved by then and got voided: that needs the user
                self.manifest.cleanup_failed(
                    "invoice",
                    ident,
                    f"the delete of invoice {name} answered status {answered!r}, not {INVOICE_DELETED} (Deleted): "
                    "check it in Gorelo",
                )
        elif outcome.gone:
            self.manifest.cleaned("invoice", ident, "already gone (HTTP 404)")
        else:
            self.manifest.cleanup_failed("invoice", ident, self._safe(context + outcome.text))

    async def _void_approved(self, ident: str, invoice: dict[str, Any], name: str) -> None:
        """Void an invoice that was just read as Approved and that the run recorded as Approved (--void-approved only)."""
        outcome, how, context = await self._remove_invoice(ident, invoice.get("Number"), "Approved")
        if outcome.ok:
            answered = outcome.data.get("StatusId") if isinstance(outcome.data, dict) else None
            if isinstance(answered, int) and not isinstance(answered, bool) and answered == INVOICE_VOID:
                self._note_void(ident, f"voided by the cleanup with {how} {VOID_RESIDUE}")
                self.voided.append(name)  # in Gorelo only: the report says the accounting system keeps its copy open
            else:  # a 6 would mean Gorelo called it a Draft by then: it is not what was asked, so it needs the user
                self.manifest.cleanup_failed(
                    "invoice",
                    ident,
                    f"the void of invoice {name} answered status {answered!r}, not {INVOICE_VOID} (Void): "
                    "check it in Gorelo",
                )
        elif outcome.gone:
            self.manifest.cleaned("invoice", ident, "already gone (HTTP 404)")
        else:
            self.manifest.cleanup_failed("invoice", ident, self._safe(context + outcome.text))

    async def _remove_invoice(self, ident: str, number: Any, expected: str) -> tuple[_Outcome, str, str]:
        """Remove an invoice that was just read as `expected` ("Draft": deleted, "Approved": voided): (outcome, how, context).

        delete_invoice (its Number, the expected status, confirm=true) when the invoice has a usable Number; the raw DELETE
        backstop when it has none, and when delete_invoice refuses BEFORE any DELETE because its lookup by Number found
        nothing (the invoice is the very one just read by its id, and the guard still decides what may be deleted). `how`
        names the way for the outcome text; `context` is said before Gorelo's own text when the fallback had to be used."""
        if isinstance(number, int) and not isinstance(number, bool) and number >= 1:
            how = "delete_invoice"
            outcome = await self._tool(
                "delete_invoice", {"invoice_number": number, "expected_status": expected, "confirm": True}
            )
            if _lookup_found_nothing(outcome):
                # delete_invoice refused before it sent any DELETE: its lookup by Number found no invoice (that search can lag
                # behind a write, like the matrix's own lookup). The invoice is the one just read by its id, so the raw
                # backstop removes it. The guard still allows that DELETE only for a run-created invoice recorded as a Draft
                # (or, with --void-approved, as Approved), and the answer is judged by the caller like any other.
                return (
                    await self._raw("DELETE", _INVOICE_PATH.format(ident)),
                    "raw DELETE after the lookup by Number found nothing",
                    "delete_invoice found no invoice by its Number, so the raw DELETE was tried: ",
                )
            return outcome, how, ""
        # delete_invoice looks an invoice up by its Number: without one only the raw DELETE can remove it
        return await self._raw("DELETE", _INVOICE_PATH.format(ident)), "raw DELETE", ""

    def _note_void(self, ident: str, outcome: str) -> None:
        """The invoice is void and stays listed as Void: record it as a known residue, not as cleaned (it cannot be removed).

        The manifest keeps the record as a leftover with an outcome that ends with VOID_RESIDUE, which is_known_undeletable
        reads, and its status_id becomes 4 so that the guard never allows another DELETE of it."""
        self.manifest.update_details("invoice", ident, status_id=INVOICE_VOID)
        self.manifest.cleanup_failed("invoice", ident, outcome)

    async def _find_announced_invoices(self) -> None:
        """A create_invoice or create_approved_invoice that was announced and never recorded may still have made an
        invoice (the answer was lost): search list_invoices (the test client) for the run label and record what is found, so
        it is handled below.

        Only an invoice whose Reference is exactly the announced label of this run counts, whatever its status: it is
        recorded with the status it has, and _clean_invoice then deletes a Draft, voids an Approved one (recorded as 5)
        only with --void-approved, notes a Void one as a known residue and leaves any other one (Paid, ...) for the
        user. A search that fails or finds nothing changes nothing: the create stays announced without an id."""
        for intent in self.manifest.unresolved_intents():
            if intent.kind != "invoice":
                continue
            for row in await self._search_invoices(intent.label):
                status = row.get("Status")
                status_id = status.get("Id") if isinstance(status, dict) else None
                number, display = row.get("Number"), row.get("DisplayNumber")
                details = {
                    "status_id": status_id if isinstance(status_id, int) and not isinstance(status_id, bool) else None,
                    "number": number if isinstance(number, int) and not isinstance(number, bool) else None,
                    "display_number": display if isinstance(display, str) and display else None,
                }
                try:
                    self.manifest.created("invoice", row["Id"], intent.label, details)
                except ValueError:  # already recorded
                    continue

    async def _search_invoices(self, label: str) -> list[dict[str, Any]]:
        """The invoices of the test client whose Reference is exactly `label` (a few pages of list_invoices at most)."""
        found: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(INVOICE_SEARCH_PAGES):
            arguments: dict[str, Any] = {
                "client_ids": [SITE.test_client],
                "query": label,
                "page_size": INVOICE_SEARCH_PAGE_SIZE,
            }
            if cursor is not None:
                arguments["cursor"] = cursor
            outcome = await self._tool("list_invoices", arguments)
            if not outcome.ok or not isinstance(outcome.data, dict) or not isinstance(outcome.data.get("items"), list):
                break
            page = outcome.data
            found.extend(
                row
                for row in page["items"]
                if isinstance(row, dict)
                and row.get("Reference") == label
                and row.get("ClientId") == SITE.test_client
                and isinstance(row.get("Id"), str)
                and _GUID.fullmatch(row["Id"])
            )
            cursor = page.get("next_cursor") if page.get("has_more") is True else None
            if not isinstance(cursor, str) or not cursor:
                break
        return found

    async def _delete_time_entry(self, entry_id: Any) -> _Outcome:
        outcome = _Outcome(False, "not attempted")
        for attempt in range(1, MAX_REOPEN_ATTEMPTS + 1):
            outcome = await self._tool("delete_time_entry", {"time_entry_id": entry_id, "confirm": True})
            if not outcome.ok:
                return outcome
            word = outcome.data.get("Outcome") if isinstance(outcome.data, dict) else None
            if word == "Deleted":
                return _Outcome(True, "Deleted" if attempt == 1 else f"Deleted after {attempt} calls (Reopened first)")
            if word != "Reopened":
                return _Outcome(False, f"delete_time_entry answered an unexpected Outcome ({str(word)[:40]!r})")
        return _Outcome(False, f"still Reopened after {MAX_REOPEN_ATTEMPTS} delete calls")

    async def _delete_with_tool(self, record: Record) -> _Outcome:
        details = record.details
        kind = record.kind
        if kind == "item":
            return await self._tool("delete_item", {"item_id": record.id, "confirm": True})
        if kind == "uptime":
            return await self._tool("delete_uptime_check", {"check_id": record.id, "confirm": True})
        needed = {"comment": "ticket_id", "project_comment": "project_id", "task": "project_id"}[kind]
        parent = details.get(needed)
        if not isinstance(parent, str) or not parent:
            return _Outcome(False, f"the manifest details of this {kind} have no {needed}, so it cannot be deleted")
        if kind == "comment":
            return await self._tool(
                "delete_ticket_comment", {"ticket_id": parent, "comment_id": record.id, "confirm": True}
            )
        if kind == "task":
            return await self._tool("delete_project_task", {"project_id": parent, "task_id": record.id, "confirm": True})
        arguments: dict[str, Any] = {"project_id": parent, "comment_id": record.id, "confirm": True}
        if details.get("task_id"):
            arguments["task_id"] = details["task_id"]
        return await self._tool("delete_project_comment", arguments)

    def _cover_children(self) -> None:
        """A record without a delete of its own (or one whose own delete failed) is gone when its parent was deleted."""
        for record in self.manifest.leftovers():
            if record.kind not in COVERED_KINDS:
                continue
            parent = self._cleaned_parent(record)
            if parent is not None:
                self.manifest.cleaned(record.kind, record.id, f"removed with {parent.kind} {parent.id}")

    def _note_undeletable(self) -> None:
        """An uploaded file has no delete in the API: once its parent was deleted, say what happened to it.

        It stays a record that is NOT cleaned (the outcome is recorded as a failed attempt, once: a second cleanup
        finds the same text and adds nothing); the report lists it as known undeletable. One whose parent was not
        deleted is not noted and stays a plain leftover."""
        for record in self.manifest.leftovers():
            if record.kind not in UNDELETABLE_KINDS:
                continue
            parent = self._cleaned_parent(record)
            if parent is None:
                continue
            outcome = f"soft-deleted with its {parent.kind}; {UNDELETABLE_REASON}"
            if record.outcome != outcome:
                self.manifest.cleanup_failed(record.kind, record.id, outcome)

    def _cleaned_parent(self, record: Record) -> Record | None:
        for key, kind in _PARENT_KEYS:
            reference = record.details.get(key)
            if not isinstance(reference, str) or not reference:
                continue
            try:
                parent = self.manifest.record(kind, reference)
            except (KeyError, ValueError):
                continue
            if parent.cleaned:
                return parent
        return None

    # -- the listed leftovers of earlier runs -----------------------------

    async def _approved_leftovers(self) -> None:
        for kind in ("client", "contact"):
            for ident in sorted(self.guard.approved_leftovers.get(kind, ())):
                await self._one_leftover(kind, ident)

    async def _one_leftover(self, kind: str, ident: int) -> None:
        path = _RAW_PATHS[kind].format(ident)
        read = await self._raw("GET", path)
        if read.gone:
            self.manifest.leftover_outcome(kind, ident, "already gone (HTTP 404)", deleted=False)
        elif not read.ok:
            self.manifest.leftover_outcome(
                kind, ident, self._safe(f"skipped: it could not be read ({read.text})"), deleted=False
            )
        elif not isinstance(read.data, dict):
            self.manifest.leftover_outcome(kind, ident, "skipped: Gorelo did not return a record", deleted=False)
        elif not _looks_like_the_probe(kind, read.data):
            self.manifest.leftover_outcome(
                kind, ident, "skipped: its name does not match the configured pattern, nothing was deleted", deleted=False
            )
        else:
            outcome = await self._raw("DELETE", path)
            if outcome.ok:
                self.manifest.leftover_outcome(kind, ident, "Deleted", deleted=True)
            elif outcome.gone:
                self.manifest.leftover_outcome(kind, ident, "already gone (HTTP 404)", deleted=False)
            else:
                self.manifest.leftover_outcome(kind, ident, self._safe(f"delete failed: {outcome.text}"), deleted=False)


def _lookup_found_nothing(outcome: _Outcome) -> bool:
    """Did delete_invoice refuse BEFORE any DELETE because its lookup by Number found no invoice?

    That refusal is a local error of the tool, so its text carries no HTTP status. An error that does carry one came
    from Gorelo, possibly for the DELETE itself, and is never read as "nothing was sent"."""
    return not outcome.ok and outcome.status is None and NO_SUCH_NUMBER in outcome.text


def _recorded_status(record: Record) -> int | None:
    """The status_id the manifest recorded for an invoice (the status Gorelo stored), None when it is not an integer."""
    recorded = record.details.get("status_id")
    return recorded if isinstance(recorded, int) and not isinstance(recorded, bool) else None


def _shown_status(status: Any, raw_id: Any) -> str:
    """A status for a message: its name and id ("Approved (id 5)"), or only the id when it has no readable name."""
    name = status.get("Name") if isinstance(status, dict) else None
    return f"{name} (id {raw_id!r})" if isinstance(name, str) and name else f"id {raw_id!r}"


def _invoice_name(invoice: dict[str, Any]) -> str:
    """How an invoice is named in a message: its DisplayNumber (INV-1042), else its Number, else its Id."""
    for key in ("DisplayNumber", "Number", "Id"):
        value = invoice.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return "(no number)"


def _looks_like_the_probe(kind: str, data: dict[str, Any]) -> bool:
    """Is this record the listed leftover (by the optional name rules of site.local.toml [leftovers])?

    Clients: Name contains one of client_name_contains. Contact: FirstName equals contact_first_name and LastName starts
    with contact_last_name_prefix. A rule that is not configured does not apply."""
    cfg = site()
    if kind == "client":
        wanted = cfg.leftover_client_name_contains
        name = data.get("Name")
        return not wanted or (isinstance(name, str) and any(part in name for part in wanted))
    first, last = data.get("FirstName"), data.get("LastName")
    if cfg.leftover_contact_first_name is not None and first != cfg.leftover_contact_first_name:
        return False
    prefix = cfg.leftover_contact_last_name_prefix
    return prefix is None or (isinstance(last, str) and last.startswith(prefix))


async def run_cleanup(
    manifest: Manifest,
    *,
    leftovers: bool = False,
    void_approved: bool = False,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    pace: float = PACE_SECONDS,
    echo: Callable[[str], None] = print,
    verify_clients: bool = True,
) -> CleanupReport:
    """Delete everything `manifest` says its run created (see the module docstring) and report what is left.

    leftovers:  also delete the leftovers listed in site.local.toml [leftovers], after checking their names.
    void_approved: also void an Approved invoice that the run recorded with status_id 5 (it is read first and must still
                read as Approved). Without it such an invoice is a leftover for the user; the write matrix never sets it.
                The void is in Gorelo only: the report names every invoice this call voided (`voided`) and says so for each
                (VOID_REMINDER), because its copy in the accounting system stays open until the user voids it there.
    settings:   default live_settings() (reads the API key from the app .env); tests pass their own.
    transport:  an httpx transport for BOTH clients (tests pass httpx.MockTransport); default is the network.
    pace:       seconds between two requests (0 in tests).
    echo:       where the summary goes (one call per line). The API key is scrubbed from it.
    verify_clients: before the first request that can write, read each configured client and refuse (SiteConfigError)
                unless its Name is the one the site config names (scripts/live/guard.py). The write matrix has done
                that already and passes False.
    """
    site()  # no site config, no run: refuse before the API key is read or anything is sent
    chosen = settings if settings is not None else live_settings()
    if verify_clients:
        await live_guard.verify_site_clients(chosen.api_key, transport=transport)
    guard_options: dict[str, Any] = {"cleanup": True}
    if void_approved:  # the one extra thing the guard learns: a run-created invoice recorded as Approved may be voided
        guard_options["allow_approved_invoice"] = True
    guard = LiveGuard("write", manifest, **guard_options)
    pacer = Pacer(pace)
    hooks = {"request": [guard, pacer]}
    with _quiet_logs():
        server = build_server(chosen, transport=transport, event_hooks=hooks)
        async with (
            Client(server) as tools,
            httpx.AsyncClient(
                base_url=GORELO_BASE_URL,
                headers={"X-API-Key": chosen.api_key, "Accept": "application/json"},
                timeout=30.0,
                transport=transport,
                event_hooks=hooks,
            ) as raw,
        ):
            cleaner = _Cleaner(manifest, guard, tools, raw, chosen.api_key, void_approved=void_approved)
            await cleaner.run(leftovers=leftovers)
    remaining = manifest.leftovers()
    report = CleanupReport(
        run_id=manifest.run_id,
        cleaned=[record for record in manifest.all_created() if record.cleaned],
        leftovers=[record for record in remaining if not is_known_undeletable(record)],
        unresolved=manifest.unresolved_intents(),
        approved=manifest.approved_leftover_outcomes() if leftovers else [],
        undeletable=[record for record in remaining if is_known_undeletable(record)],
        voided=list(cleaner.voided),
    )
    for line in report.render().splitlines():
        echo(scrub(line, chosen.api_key))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.live.cleanup",
        description="Delete what a live run created, as recorded in its manifest. Safe to run again.",
    )
    parser.add_argument("manifest", help="the run manifest, for example .live-runs/MCPTEST-20991002101500.json")
    parser.add_argument(
        "--leftovers",
        action="store_true",
        help="also delete the leftovers listed in site.local.toml [leftovers] (the leftover clients and contact) after "
        "checking their names",
    )
    parser.add_argument(
        "--void-approved",
        action="store_true",
        help="also void an Approved invoice that the run recorded as Approved (the write matrix's approved_invoice area): "
        f"it was pushed to the connected accounting system, so check it there first ({VOID_COMMAND_NOTE}); "
        "a voided invoice stays listed as Void",
    )
    args = parser.parse_args(argv)
    try:
        manifest = Manifest.load(args.manifest)
    except (OSError, ValueError) as exc:
        print(f"cannot open the manifest: {exc}", file=sys.stderr)
        return 2
    try:
        report = asyncio.run(run_cleanup(manifest, leftovers=args.leftovers, void_approved=args.void_approved))
    except (EnvError, SiteConfigError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
