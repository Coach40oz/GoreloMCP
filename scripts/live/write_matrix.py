"""The live write matrix: the write tools against the test client and the second client, then clean up.

    python -m scripts.live.write_matrix [--skip-email] [--only AREA,...] [--leftovers] [--with-items] [--with-invoices]
                                        [--with-approved-invoice]

Run id MCPTEST-<UTC yyyymmddHHMMSS>. Every record the run creates is named with it, announced in the run manifest
(.live-runs/<run id>.json) BEFORE the create call and recorded with its id right after (the details contract of
scripts/live/manifest.py: tickets record contact_id and cc_contact_ids, comments ticket_id and private, tasks
and sections project_id, project comments project_id and task_id, attachments, side conversations and approvals
their ticket_id, invoices status_id, number and display_number). The server is built in process behind ONE write-mode LiveGuard with require_intents=True plus a
pacer (one request per second); everything the matrix sends is a tool call (there is no raw HTTP client). A guard
refusal stops the run at once (the remaining areas are "not run"); any other failure only fails its own area.
scripts.live.cleanup.run_cleanup ALWAYS runs afterwards, in a `finally`, also when an area raised or the run was
interrupted, and its leftovers decide the exit status together with the areas.

Areas, in this order (--only takes a comma separated list and keeps this order; "forms" is another name for
"projects"). Names are prefixed with the run id; the test client unless noted.

    clients    create_client of a temporary client with a location phone and region US (the region fix),
               update_client its alternate name (this is the only update_client call: the test client itself is never
               changed), read it back
    contacts   create_contact (mobile phone, region US); update_contact job_title with
               clear_secondary_email_ok=true, then check by a read that the mobile phone survived (PATCH replaces
               the contact); update_contact without a secondary-email choice must be refused locally (no request)
    tickets    status New, type Incident, group Everyone, source Api (6) and priority Normal are resolved by name;
               create_ticket plain; create_ticket backdated and Closed (offsets in the past); get_ticket by GUID,
               number and display number; update_ticket title and the operator user as watcher (WatcherIds must read back [operator user]),
               then clear_fields watcher_ids (WatcherIds must read back empty), and only then lead assignee the operator user in
               a later update (a technician cannot be lead and watcher at once: Gorelo answers 400 "Technician
               already exists", so the two never share a PATCH); no tenant tag is ever set; billing read-fill (role
               and work type, then only billable_status_id 2: the other two must survive); search_tickets
               (client_id=the test client) finds both run tickets
    comments   private comment, list it, get it, delete it (confirm=true); upload_attachment (text) and attach it
               to a new private comment
    email      the second client and the operator contact ONLY: create_ticket with send_created_email=true, a public comment,
               a status change, a side conversation to the operator address with a side_conversation comment, an
               approval for the operator contact with an approval comment (recorded as skipped when Gorelo rejects the
               approver, or when the stored approvers are not only the operator contact); --skip-email leaves this
               area out
    time       create_time_entry (the operator user, no service line, billable status 2, explicit times; the entry's User.Id
               must read back the operator user: the published User object, which production answers since 2026-10-03. The
               flat UserId (the operator user) that production answered on 2026-10-02, with no User object, still passes,
               with a note that it is drift; any other id, or neither field, fails the area), update_time_entry comment, delete_time_entry
               (confirm=true) again while Outcome is Reopened (at most 3 calls)
    items      create_item (product scoped to the test client), update_item, delete_item (confirm=true). SKIPPED unless
               --with-items is given (it creates, updates and deletes a catalog product on the test client), and also
               skipped, with the same reason, when --only items is given without it: the three item tools cannot
               even be called then. A live run passed it, and a deleted item answers GET /v1/items/{itemId}
               with a 404 (code 070202)
    invoices   list_items for the first active product that belongs to nobody or to the test client, create_invoice (always a
               Draft for the test client with one line and no recipients), get_invoice, list_invoices by the run label,
               export_invoice_pdf (a PDF file; the export event is recorded on the test invoice), then delete_invoice
               (expected status Draft, confirm=true; the answer says StatusId 6) and a last list_invoices that must no
               longer show it. A Draft uses up one invoice number. SKIPPED unless --with-invoices is given, and also
               skipped, with the same reason, when --only invoices is given without it: the invoice tools cannot even
               be called then. create_approved_invoice is never called here (it pushes the invoice to the accounting
               system): only the approved_invoice area below calls it
    approved_invoice
               ONE Approved invoice of $1 for the test client, created, waited for until Gorelo has pushed it to the connected
               accounting system, then voided at once. SKIPPED unless --with-approved-invoice is given, and then it is
               the ONLY area of the run and the run has no other flag (--only approved_invoice alone: any other area,
               and --with-items, --with-invoices, --leftovers or --skip-email, is a usage error, exit status 2, before a
               request is sent), with its own tool set (APPROVED_INVOICE_TOOLS) and a guard built with
               allow_approved_invoice. The create call alone (create_approved_invoice, which also reads the new invoice
               back) runs with the 429 retries of the run's Gorelo client switched off (max_429_retries=0, put back right
               after the call, also when it raises), so the one create is never sent twice: a 429 on it is a plain
               rejected create that settles its intent ("Gorelo is rate limiting requests (HTTP 429) ...; it did not
               process this request"), not a guard refusal. Every other request of the run keeps the client's normal
               retries.
               Preconditions, checked before any write (a skip with the reason, nothing sent, when one does not hold):
               every contact of the test client is Inactive (list_contacts), no location of the test client names a billing contact
               (list_client_locations: BillingContactIds empty, null or the text []), and an item as in the invoices area
               (list_items). Then the intent ("invoice", status_id 5, the label `<run> approved invoice`) and
               create_approved_invoice (the test client, ONE line: that item, quantity 1, unit price 1.0, the label as its
               text, no tax: TaxId is sent as an explicit null, which the guard insists on; the label as reference; no
               recipients and no dates, Gorelo's defaults; confirm=true), recorded in the manifest the moment it answers
               with the status Gorelo STORED (5 when the answer has none, never 1), its number and display number.
               Checks: Status.Id 5 (a Paid 3, which Gorelo created because the total was 0, fails the area and is left
               for the user: Gorelo refuses to delete or void a Paid invoice), the client, the reference, the one line,
               Total above 0, IsEmailSent false and EmailSentOn null (a note, written loudly, when not), and a note of
               the Number and DisplayNumber it used up. Then get_invoice every 5 s for at most 120 s until ExternalId is
               set (the push to the accounting system). A read that fails meanwhile (an HTTP error, a timeout or a 429
               that outlasts the client's own retries: a GET writes nothing) only means "not synced yet": it is noted
               (once per distinct text; the trace id that ends an error is left out when texts are compared) and asked
               again at the next poll, and the wait ends only on a status other than Approved or when the 120 s run
               out. If ExternalId never is set, nothing is voided and the area FAILS saying "approved invoice
               <DisplayNumber> not yet synced to accounting; check the accounting system, then void it with: python -m
               scripts.live.cleanup <manifest> --void-approved (this voids it in Gorelo only: void its copy in the
               accounting system by hand too)". Once ExternalId is set (noted, with whether PaymentLink is set):
               delete_invoice (the number, expected status Approved, confirm=true; the answer must say StatusId 4, Void;
               when Gorelo still answers 429 to its lookup or its DELETE after the client's own retries, the area asks
               again, up to 3 attempts in all, because Gorelo did not process that request), then a note that
               the void is in Gorelo only (check the accounting system and void the invoice there too: Gorelo's void is
               not pushed to the connected accounting system, seen with Xero), get_invoice (Status.Id 4) and list_invoices (the test client, the label
               as query), which must still list it. The read after the void also checks TotalTax 0 and Total 1.0 (the
               line asked for no tax and $1): when it reads otherwise, a loud note says the no-tax check FAILED. That is
               only a note, made after the void, so it can never stop or skip the void or its bookkeeping. A voided
               invoice cannot be removed, so it is recorded with the outcome "voided by the write matrix (still listed
               as Void)": a known residue, neither cleaned nor a failure (like the uploaded file). The summary of the run
               repeats the reminder about the void, for every approved invoice of the run that was voided, as its last
               line before the result
    uptime     create_uptime_check (http, the probe domain URL, seattle, every 60 minutes), set_uptime_maintenance
               starting now (start is the current UTC time written with an offset, 60 minutes, a reason: Gorelo
               refuses a window without a start), update_uptime_check description, delete_uptime_check
               (confirm=true)
    projects   list_projects and list_forms first: a 403 with the missing-scope message is recorded as "skipped:
               scope missing" and nothing else is sent; with the Project scope: list_project_types (the first
               type) and the Everyone group (both are required by create_project), create_project, a section, a task
               in it, a private task comment, then delete the comment and the task (the project itself is soft
               deleted by the cleanup)

Who a ticket would email is checked on what Gorelo STORED, before anything else is sent for that ticket. Right
after create_ticket's read-back (every run ticket, in Matrix.new_ticket) the stored ticket must have exactly the
audience the matrix asked for: the two test-client tickets ContactId null and CcContactIds empty, the second-client email ticket
ContactId the operator contact and CcContactIds only the operator contact. The STORED values are written into the manifest (ticket details
contact_id and cc_contact_ids, with manifest.update_details), so the guard's public-comment rule judges the truth;
until then the ticket has no contact_id in its details and the guard allows no public comment on it. A mismatch,
a read-back that failed and a ContactId or CcContactIds that is missing or of an odd shape all fail the area at
once: no comment, no status change, no approval follows. For the approval, create_ticket_approval's read-back
must list approvers and every one must be the operator contact before the approval comment is posted; otherwise the
comment is skipped and the reason recorded. The side conversation and the approval are recorded with their
ticket_id, and the guard lets a side conversation (approval) comment go only into the one this run created on
that ticket.

Live requests per area (method and spec path, in the order they are sent; the tests pin these lists):

    clients:
        POST /v1/clients  # create_client: temporary client, location phone and region US
        GET /v1/clients/{clientId}/locations  # the location it got
        PATCH /v1/clients/{clientId}  # update_client: alternate name of the temporary client
        GET /v1/clients/{clientId}  # read it back
    contacts:
        POST /v1/contacts  # create_contact
        GET /v1/contacts/{contactId}  # update_contact reads the contact first
        PATCH /v1/contacts/{contactId}  # and sends the complete contact
        GET /v1/contacts/{contactId}  # read back: the mobile phone survived
    tickets:
        GET /v1/tickets/statuses  # lookups by name, once per run (cached for the later areas)
        GET /v1/tickets/types
        GET /v1/organization/groups
        POST /v1/tickets  # plain ticket
        GET /v1/tickets/{ticketId}  # create_ticket reads the ticket back; its stored audience is verified here
        POST /v1/tickets  # backdated and Closed
        GET /v1/tickets/{ticketId}  # read back, audience verified again
        GET /v1/tickets/{ticketId}  # get_ticket by GUID
        GET /v1/tickets  # get_ticket by number (Query)
        GET /v1/tickets/{ticketId}
        GET /v1/tickets  # get_ticket by display number (Query)
        GET /v1/tickets/{ticketId}
        PATCH /v1/tickets/{ticketId}  # title and the operator user as watcher
        GET /v1/tickets/{ticketId}
        PATCH /v1/tickets/{ticketId}  # clear_fields watcher_ids
        GET /v1/tickets/{ticketId}  # WatcherIds is empty
        PATCH /v1/tickets/{ticketId}  # lead assignee the operator user, never in the same PATCH as a watcher
        GET /v1/tickets/{ticketId}
        GET /v1/billing-roles
        GET /v1/work-types
        GET /v1/tickets/{ticketId}  # update_ticket reads the current billing override
        PATCH /v1/tickets/{ticketId}  # billing role and work type
        GET /v1/tickets/{ticketId}
        GET /v1/tickets/{ticketId}  # billable status 2 alone: the other two parts are read and sent back
        PATCH /v1/tickets/{ticketId}
        GET /v1/tickets/{ticketId}
        GET /v1/tickets  # search_tickets for the test client
    comments:
        POST /v1/tickets/{ticketId}/comments  # private comment
        GET /v1/tickets/{ticketId}/comments/{commentId}
        GET /v1/tickets/{ticketId}/comments  # list_ticket_comments
        GET /v1/tickets/{ticketId}/comments/{commentId}  # get_ticket_comment
        DELETE /v1/tickets/{ticketId}/comments/{commentId}  # delete_ticket_comment
        POST /v1/attachments  # upload_attachment (text file)
        POST /v1/tickets/{ticketId}/comments  # private comment carrying the attachment
        GET /v1/tickets/{ticketId}/comments/{commentId}
    email:
        POST /v1/tickets  # the second client, the operator contact, send_created_email=true
        GET /v1/tickets/{ticketId}  # read back: ContactId the operator contact and CcContactIds only the operator contact, else the area stops here
        POST /v1/tickets/{ticketId}/comments  # public comment (emails the operator contact)
        GET /v1/tickets/{ticketId}/comments/{commentId}
        PATCH /v1/tickets/{ticketId}  # status change
        GET /v1/tickets/{ticketId}
        POST /v1/tickets/{ticketId}/conversations/side-conversation  # to the operator address
        POST /v1/tickets/{ticketId}/comments  # side_conversation comment, into the side conversation just created
        GET /v1/tickets/{ticketId}/comments/{commentId}
        POST /v1/tickets/{ticketId}/conversations/approval  # approver the operator contact
        GET /v1/tickets/{ticketId}/approvals/{approvalId}  # every stored approver must be the operator contact, else no comment
        POST /v1/tickets/{ticketId}/comments  # approval comment, into the approval just created
        GET /v1/tickets/{ticketId}/comments/{commentId}
    time:
        POST /v1/time-entries  # the operator user, no service line, billable status 2
        GET /v1/time-entries/{timeEntryId}
        PATCH /v1/time-entries/{timeEntryId}  # comment
        DELETE /v1/time-entries/{timeEntryId}  # again while Outcome is Reopened, at most 3 calls
        GET /v1/time-entries/{timeEntryId}  # recorded only: a deleted entry should be a 404
    items:
        POST /v1/items  # product scoped to the test client (only with --with-items)
        GET /v1/items/{itemId}
        PATCH /v1/items/{itemId}
        GET /v1/items/{itemId}
        DELETE /v1/items/{itemId}
    invoices:
        GET /v1/items  # list_items: the first active product of nobody or of the test client (only with --with-invoices)
        POST /v1/invoices  # create_invoice: a Draft for the test client, one line, no recipients
        GET /v1/invoices/{invoiceId}  # create_invoice reads the invoice back
        GET /v1/invoices/{invoiceId}  # get_invoice
        GET /v1/invoices  # list_invoices by the run label
        GET /v1/invoices/{invoiceId}/pdf  # export_invoice_pdf: an export event on the test invoice
        GET /v1/invoices/{invoiceId}  # get_invoice again: still a Draft, else the area stops and leaves it
        GET /v1/invoices  # delete_invoice finds the invoice by its Number
        DELETE /v1/invoices/{invoiceId}  # Draft: deleted (no longer listed); the answer says StatusId 6
        GET /v1/invoices  # list_invoices: no longer listed
    approved_invoice:
        GET /v1/contacts  # list_contacts for the test client: every contact must be Inactive (only with --with-approved-invoice)
        GET /v1/clients/{clientId}/locations  # list_client_locations: no location may name a billing contact
        GET /v1/items  # list_items: the first active product of nobody or of the test client
        POST /v1/invoices  # create_approved_invoice: StatusId 5, one line of $1, no recipient, no dates
        GET /v1/invoices/{invoiceId}  # create_approved_invoice reads the invoice back
        GET /v1/invoices/{invoiceId}  # get_invoice: ExternalId set (the push to accounting); repeated every 5 s until it is
        GET /v1/invoices  # delete_invoice finds the invoice by its Number
        DELETE /v1/invoices/{invoiceId}  # Approved: voided (stays listed); the answer says StatusId 4
        GET /v1/invoices/{invoiceId}  # get_invoice: Status.Id 4
        GET /v1/invoices  # list_invoices: still listed, as Void
    uptime:
        GET /v1/clients/{clientId}/locations  # the test client's location
        POST /v1/uptime  # http check, the probe domain URL, seattle, every 60 minutes
        GET /v1/uptime/{checkId}
        PATCH /v1/uptime/{checkId}  # maintenance window: start now, 60 minutes
        GET /v1/uptime/{checkId}
        PATCH /v1/uptime/{checkId}  # description
        GET /v1/uptime/{checkId}
        DELETE /v1/uptime/{checkId}
    projects:
        GET /v1/projects  # scope probe; a 403 with code 080203 ends the area here (skipped)
        GET /v1/forms  # scope probe (no forms write step)
        GET /v1/projects/types  # list_project_types: the first type, required by create_project
        POST /v1/projects  # with that type and the Everyone group (group_id)
        GET /v1/projects/{projectId}
        POST /v1/projects/{projectId}/sections
        POST /v1/projects/{projectId}/tasks  # in that section
        GET /v1/projects/{projectId}/tasks/{taskId}
        POST /v1/projects/{projectId}/tasks/{taskId}/comments  # private
        GET /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}
        DELETE /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}
        DELETE /v1/projects/{projectId}/tasks/{taskId}
    cleanup:
        DELETE /v1/projects/{projectId}  # then the rest, newest first; what each area already deleted is skipped
        DELETE /v1/tickets/{ticketId}  # the email ticket
        DELETE /v1/tickets/{ticketId}/comments/{commentId}  # the private comment that was not deleted
        DELETE /v1/tickets/{ticketId}  # the backdated ticket
        DELETE /v1/tickets/{ticketId}  # the plain ticket
        DELETE /v1/contacts/{contactId}
        DELETE /v1/clients/{clientId}  # the temporary client

The lists above are the full path (the approver is accepted and stored as asked, the Project scope is granted, the
time entry is deleted at once, --with-items, --with-invoices and --with-approved-invoice are given, the invoice has a
Number and, for the approved one, Gorelo has pushed it to the accounting system by the first get_invoice). What varies: the
three lookups come with the first area that needs them (--only comments or time starts with them and the plain
ticket; --only projects sends them, and the test client's location, right after its two scope probes); get_ticket by number
or display number is tried up to 3 times; delete_time_entry repeats while Outcome is Reopened (the cleanup tries
again up to 3 times if it never turns Deleted); search_tickets reads every page and adds a second search by run id
when a big client truncates it; a rejected approver ends the email area after its POST (no GET, no approval
comment), and so do stored approvers that are not only the operator contact after the approval's GET; a ticket stored with
an audience the matrix did not ask for (or a read-back that failed) ends its area right after that ticket's GET;
without the Project scope the projects area is its first two requests; the items, invoices and approved_invoice areas
send nothing at all unless --with-items, respectively --with-invoices or --with-approved-invoice, is given; with
--with-invoices and no usable catalog item the invoices area is its first request; the two list_invoices checks of that
area and delete_invoice's lookup by its
Number are tried up to 3 times, like the ticket lookups, because a search can lag behind a write; an invoice that
reads back with no Number is not deleted by the area (no delete_invoice, no lookup, no last list: the cleanup
removes it with its raw DELETE), a lookup by Number that still finds nothing after the 3 tries fails the area without
any DELETE (the cleanup then removes the invoice, which it reads as a Draft by its id, with the same raw DELETE), and
one that is no longer a Draft at the second get_invoice is not touched at all (the area fails there and the cleanup
leaves it for the user). The approved_invoice area ends after its first request when a contact of the test client is not
Inactive, after its second when a location names a billing contact and after its third when there is no usable item
(each a skip, with the reason); its get_invoice is repeated every 5 s for at most 120 s (24 waits) until ExternalId is
set (a read that fails is one of those polls, not a stop: the next one asks again), and an invoice that never gets one
is NOT voided (no lookup by Number, no DELETE: the area fails, and the cleanup, which never voids without
--void-approved, leaves it a leftover for the user); delete_invoice's lookup by Number is tried up to 3 times, like the
Draft's, and a voided invoice sends nothing in the cleanup (a known residue). An approved invoice that reads with no
Number is not voided by the area either (delete_invoice finds an invoice by its Number): the area fails with "approved
invoice <DisplayNumber> has no Number, so delete_invoice cannot find it; void it with: python -m scripts.live.cleanup
<manifest> --void-approved (this voids it in Gorelo only: void its copy in the accounting system by hand too)", and the
cleanup, which never voids without that flag, leaves it for the user. A 429 on the create is not retried (one POST, a
failed area, an intent settled as failed), and neither is one on its read-back, which belongs to the same call (the area
fails, and the invoice stays Approved, recorded as such, for the user to void with --void-approved); every other request
of the run keeps the client's own retries, and a delete_invoice whose lookup or DELETE is still answered 429 after those
is asked again by the area (the same up to 3 attempts as the lookup by Number: Gorelo did not process it). The cleanup
after the run has a client of its own and keeps the default retries.

Cleanup then reads the manifest backwards: gated delete tools for what has one (private comments, time entries,
items, uptime checks, project tasks and comments), raw soft deletes for tickets, contacts, the temporary client and
the project, and "removed with its parent" for sections, side conversations and approvals. An invoice is read first;
a Draft is deleted (delete_invoice, or the raw DELETE backstop when it has no Number or when delete_invoice refused
before any DELETE because its lookup by Number found nothing). The cleanup the matrix runs NEVER voids (it is not given
void_approved: only the approved_invoice area voids, the one invoice it approved itself, and the user's own command
voids a leftover): an Approved invoice recorded as 5 is reported as a leftover for the user with its display number and
the command that voids it (python -m scripts.live.cleanup <manifest> --void-approved, which voids it with delete_invoice
or, as for a Draft, the raw DELETE backstop) and the note that this voids it in Gorelo only (VOID_COMMAND_NOTE: its copy
in the accounting system is voided by hand too), a voided one is a known residue (sent nothing), and any other status
(Paid, ...) is never deleted or voided, it is reported as a leftover for the user with its display number. A create
that was announced without an id (create_invoice or create_approved_invoice) is searched by the run label
(list_invoices for the test client). The uploaded attachment has no delete in the API: it is reported as "soft-deleted with
its ticket; the file itself cannot be deleted through the API", counted as known undeletable (not as cleaned, not as a
leftover) and never alone fails the run; a voided invoice is counted the same way (it cannot be removed either).

Output: a line as each area finishes (the run takes a few minutes at one request per second), then the cleanup's own
report, then the summary: one line per area (pass, FAIL with the reason, skipped, not run), the probe answers and
notes, records created, cleaned, left over, known undeletable and announced without an id, a line per known
undeletable record, the scopes that answered 403, and the guard counts; then, for every approved invoice of the run that
was voided (by the area, or found void by the cleanup), a reminder line that the void is in Gorelo only: check the
accounting system and void the invoice there too (Gorelo's void is not pushed to the connected accounting system, seen with Xero). The two sentences
live in scripts/live/cleanup.py (VOID_REMINDER and VOID_COMMAND_NOTE), which says the first for each invoice it voids
itself and the second in its leftover text and in the help of --void-approved; every hint printed here that names that
command (the area's FAIL for an invoice that never synced or has no Number, and the summary's line for an Approved invoice
left over) ends with the second. Exit status 0 only when no area FAILED, nothing is left over (a known undeletable record,
an attachment file or a voided invoice, is not a leftover) and the guard refused nothing; a skipped area (scope missing,
--skip-email, items without --with-items, invoices without --with-invoices, approved_invoice without
--with-approved-invoice, a precondition of the approved invoice that does not hold) is not a failure; 1 otherwise; 2 for a
usage or setup error (an unknown area, --with-approved-invoice without --only approved_invoice or beside another flag, no
API key, a tool the matrix calls that the server would not offer); 130 when interrupted (the cleanup has run by then).
Output holds ids and Gorelo's messages only, never customer data and never the API key.

Safety nets besides the guard: the matrix may call only the tools in MATRIX_TOOLS (no post_alert, no
create_approved_invoice; the three item tools only with --with-items; list_items, list_invoices, get_invoice,
create_invoice, export_invoice_pdf and delete_invoice only with --with-invoices). With --with-approved-invoice the run
may call ONLY APPROVED_INVOICE_TOOLS (create_approved_invoice, get_invoice, list_invoices, delete_invoice, list_items,
list_contacts, list_client_locations) and its guard is built with allow_approved_invoice; create_approved_invoice is
named nowhere else in this module. That run also switches off the 429 retries of its Gorelo client around the create call
alone (no_429_retries, put back right after it), because the guard allows the create once and a retried POST would count as
a second one; the client is looked up before the first request (gorelo_client_behind), or the run does not start. The
matrix checks before it creates anything that the server offers every tool it may call.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import math
import os
import re
import sys
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastmcp import Client

from gorelo_client import GoreloClient
from scripts.live._env import EnvError, Pacer, live_settings, scrub
from scripts.live.cleanup import (
    MAX_REOPEN_ATTEMPTS,
    NO_SUCH_NUMBER,
    PACE_SECONDS,
    VOID_COMMAND_NOTE,
    VOID_REMINDER,
    VOID_RESIDUE,
    CleanupReport,
    run_cleanup,
)
from scripts.live.guard import LiveGuard
from scripts.live.manifest import Manifest
from scripts.live.smoke import GuardTripped, ToolFailed, ToolSession, emit, quiet_logs
from scripts.live import guard as live_guard
from scripts.site_config import SITE, SiteConfigError, site
from server import build_server
from settings import Settings
from tools._common import REGISTRY

LOOKUP_ATTEMPTS = 3  # get_ticket by number or display number may need a moment before the new ticket is searchable
LOOKUP_WAIT = 3.0  # (the invoice lists and delete_invoice's lookup by Number get the same patience)
# NO_SUCH_NUMBER (imported above from the cleanup, which reads the same refusal) is what delete_invoice says, before it
# sends any DELETE, when its lookup by Number finds nothing.
CLIENT_PHONE = "5555550142"  # national number of the temporary client's location (a fictional 555-01xx number)
CONTACT_PHONE = CLIENT_PHONE  # the one number the live probes already know Gorelo accepts with region US
REGION = "US"
NO_CHARGE = 2  # billable status id
APPROVER_HINT = re.compile(r"contact_ids|approvers?|eligible|\btags?\b", re.IGNORECASE)
APPROVER_STATUSES = (400, 409, 422)  # Gorelo refuses an approver with one of these
# The most an area's detail (or the cleanup error) shows. The hints that print the cleanup command and say what it does to the
# accounting system (VOID_COMMAND_NOTE) reach about 300 characters with the usual manifest path, and a clipped hint would lose
# the end of the command or of that sentence, so the limit leaves room for a longer path.
DETAIL_LIMIT = 400
FAILURE_LIMIT = 160  # characters of a failed read's text that go into a note
# What a tool says when Gorelo answered 429 to every try of a request (the client has retried within its own budget by then).
# Both parts together say that Gorelo did not process the request, so asking again is safe (see Matrix.remove_invoice). The words
# are those of tools/_common.format_gorelo_error; a test builds the real text and checks that rate_limited reads it.
RATE_LIMITED = "Gorelo is rate limiting requests (HTTP 429)"
NOT_PROCESSED = "it did not process this request"
# VOID_REMINDER (imported above from the cleanup, which says it too) is what is said after an approved invoice was voided:
# Gorelo's void does not reach the accounting system, so void the copy there yourself. VOID_COMMAND_NOTE (imported too)
# ends every hint below that sends the user to `cleanup --void-approved`: that command voids in Gorelo only as well.
# Why the items area does not run by default (it is also the area's detail in the summary).
ITEMS_SKIP_REASON = "not live-tested by default: creates and deletes a catalog product on the test client"
# Why the invoices area does not run by default (it is also the area's detail in the summary).
INVOICES_SKIP_REASON = (
    "not live-tested by default: creates and deletes a Draft invoice on the test client, which uses up one invoice number"
)
# Why the approved_invoice area does not run by default (it is also the area's detail in the summary).
APPROVED_INVOICE_SKIP_REASON = (
    "not live-tested by default: approves a $1 invoice on the test client, which pushes it to the connected accounting system, "
    "then voids it"
)
APPROVED_INVOICE_AREA = "approved_invoice"  # the key of the area, which --with-approved-invoice runs alone
INVOICE_DRAFT = 1  # Invoice StatusId of a Draft: the only status the invoices area creates
INVOICE_PAID = 3  # what Gorelo makes of an Approved invoice whose total is exactly 0 (it neither deletes nor voids it)
INVOICE_VOID = 4  # the StatusId Gorelo answers a DELETE of an Approved invoice with (it stays listed)
INVOICE_APPROVED = 5  # Invoice StatusId of an Approved invoice: only the approved_invoice area creates one
INVOICE_DELETED = 6  # the StatusId Gorelo answers a DELETE of a Draft invoice with
INVOICE_LINE_QUANTITY = 1
INVOICE_LINE_PRICE = 1.0
INVOICE_DUE_DAYS = 14
CATALOG_PAGE = 50  # catalog items read to find one to bill
# The approved invoice is polled every SYNC_INTERVAL seconds, SYNC_POLLS times after the first read: at most 120 s in all,
# for Gorelo to push it to the connected accounting system (ExternalId is set once it has).
SYNC_INTERVAL = 5.0
SYNC_POLLS = 24
CONTACT_PAGES = 5  # pages of 200 contacts read to check that every contact of the test client is Inactive
# The Status.Name of a contact that cannot be emailed (a test client whose contacts are all Inactive). The
# spec does not publish the status scale of contacts, so the name is what is matched; anything else is not Inactive.
INACTIVE_CONTACT = "inactive"
# What the matrix records for the invoice it voided itself (a voided invoice cannot be removed: a known residue).
VOIDED_BY_MATRIX = f"voided by the write matrix {VOID_RESIDUE}"
# Who the stored ticket may say it emails: nobody, or the operator contact (the matrix asks for nothing else).

# The only tools the matrix may call. Anything else (post_alert, create_approved_invoice, the smoke's many reads) is a
# RuntimeError before a request is made, on top of what the guard refuses. create_approved_invoice is deliberately not
# here: it pushes the invoice to the connected accounting system. Only the approved_invoice area may call it, with the
# separate tool set below.
MATRIX_TOOLS = frozenset(
    {
        "list_ticket_statuses", "list_ticket_types", "list_org_groups", "list_ticket_sources",
        "list_ticket_priorities", "list_billing_roles", "list_work_types",
        "list_client_locations", "list_projects", "list_forms", "list_project_types", "search_tickets",
        "list_ticket_comments",
        "get_client", "get_contact", "get_ticket", "get_ticket_comment", "get_time_entry",
        "create_client", "update_client", "create_contact", "update_contact", "create_ticket", "update_ticket",
        "create_ticket_comment", "upload_attachment", "create_ticket_side_conversation", "create_ticket_approval",
        "create_time_entry", "update_time_entry", "create_item", "update_item", "create_uptime_check",
        "set_uptime_maintenance", "update_uptime_check", "create_project", "create_project_section",
        "create_project_task", "create_project_comment",
        "list_items", "list_invoices", "get_invoice", "create_invoice", "export_invoice_pdf", "delete_invoice",
        "delete_ticket_comment", "delete_time_entry", "delete_item", "delete_uptime_check",
        "delete_project_comment", "delete_project_task",
    }
)
# The catalog tools: callable only with --with-items (it creates, updates and deletes a catalog product on the test client).
ITEM_TOOLS = frozenset({"create_item", "update_item", "delete_item"})
# The invoice tools: callable only with --with-invoices (a Draft invoice uses up an invoice number, and the PDF export
# is recorded on the invoice). list_items is only the lookup of the item to put on the invoice.
INVOICE_TOOLS = frozenset(
    {"list_items", "list_invoices", "get_invoice", "create_invoice", "export_invoice_pdf", "delete_invoice"}
)


# The tools of the approved_invoice run (--with-approved-invoice): the ONLY tools that run may call. This set is the one
# place besides the area itself where create_approved_invoice is named; it replaces MATRIX_TOOLS for that run.
APPROVED_INVOICE_TOOLS = frozenset(
    {
        "create_approved_invoice", "get_invoice", "list_invoices", "delete_invoice", "list_items", "list_contacts",
        "list_client_locations",
    }
)


def tools_for(with_items: bool, with_invoices: bool = False, with_approved_invoice: bool = False) -> frozenset[str]:
    """The tools a run may call: with --with-approved-invoice exactly APPROVED_INVOICE_TOOLS (that run has no other area);
    otherwise MATRIX_TOOLS, without the three item tools unless --with-items was given and without the six invoice tools
    unless --with-invoices was given."""
    if with_approved_invoice:
        return APPROVED_INVOICE_TOOLS
    tools = MATRIX_TOOLS
    if not with_items:
        tools = tools - ITEM_TOOLS
    if not with_invoices:
        tools = tools - INVOICE_TOOLS
    return tools


class CheckFailed(Exception):
    """A tool answered, but not with what the matrix expected."""


class Skipped(Exception):
    """The area cannot run (a missing scope, no run ticket); the text says why. It is not a failure."""


class UsageError(ValueError):
    """A bad command line (an unknown area, nothing left to run)."""


class SetupError(RuntimeError):
    """The server this run would build is not what the run needs: it does not offer every tool the matrix calls, or (for
    the approved invoice run) its Gorelo client cannot be reached to switch the 429 retries off around the one create.
    Found before anything is created."""


# --------------------------------------------------------------------------
# The 429 retries of the Gorelo client, switched off around the one approved create and nowhere else
# --------------------------------------------------------------------------


def gorelo_client_behind(server: Any) -> GoreloClient:
    """The GoreloClient the tools of `server` use, which the approved invoice run needs to hold before its first request.

    build_server has no option for the client's retries, so the client its lifespan built is reached the way a tool reaches
    it: through the lifespan state under "gorelo" (FastMCP keeps it on the started server). When that is not a GoreloClient,
    SetupError: the run must not go on without being able to switch the retries off, and nothing has been sent yet."""
    state = getattr(server, "_lifespan_result", None)
    client = state.get("gorelo") if isinstance(state, dict) else None
    if not isinstance(client, GoreloClient):
        raise SetupError(
            "cannot switch off the 429 retries of the Gorelo client (the started server has no GoreloClient in its "
            "lifespan state): the approved invoice must never be sent twice, so the run did not start"
        )
    return client


@contextlib.contextmanager
def no_429_retries(client: GoreloClient) -> Iterator[None]:
    """Inside the block `client` retries no 429 (max_429_retries is 0); after it, also when the block raised, the setting it
    had before is back.

    GoreloClient retries a 429 (Gorelo did not process the request) for every method, and every retry passes the guard
    again. The approved run's guard allows ONE create, so a retried POST would be refused as a second Approved invoice: a
    guard violation that stops the run and settles the intent as "nothing was sent", although a request was sent and
    answered. With no retry a 429 on the create comes back as a plain rejected create ("it did not process this request"),
    which settles its intent truthfully, and nothing that approves an invoice is ever sent twice behind the caller's back.
    Only that create needs it: the reads, the lookup by Number and the DELETE that voids may be retried safely, so the
    block is the one create call and nothing else. The matrix drives the client one request at a time, so nothing else
    can send while the block is open."""
    normal = client.max_429_retries
    client.max_429_retries = 0
    try:
        yield
    finally:
        client.max_429_retries = normal


# --------------------------------------------------------------------------
# Small checks
# --------------------------------------------------------------------------


def dig(record: Any, *path: str) -> Any:
    """record[path[0]][path[1]]..., or None when a step is missing or not an object."""
    for key in path:
        if not isinstance(record, dict):
            return None
        record = record.get(key)
    return record


def check(condition: Any, text: str) -> None:
    if not condition:
        raise CheckFailed(text)


def expect(what: str, actual: Any, wanted: Any) -> None:
    if actual != wanted:
        raise CheckFailed(f"{what}: expected {wanted!r}, got {actual!r}")


def same_guid(left: Any, right: Any) -> bool:
    return isinstance(left, str) and isinstance(right, str) and left.lower() == right.lower()


def digits(text: Any) -> str:
    return re.sub(r"\D", "", text) if isinstance(text, str) else ""


def full(record: dict[str, Any], tool: str) -> dict[str, Any]:
    """The record a write tool read back. {Id, warning} (the read-back failed after the write) is a failed check."""
    if "warning" in record:
        raise CheckFailed(f"{tool}: the write succeeded but reading it back failed: {str(record['warning'])[:160]}")
    return record


_MISSING = object()  # a key the record does not have (None is a value Gorelo can answer with)


def is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def kind_of(value: Any) -> str:
    """What a value is, for a message about a value that is not what was expected (never the value itself)."""
    return "missing" if value is _MISSING else type(value).__name__


def first_int_id(rows: Any) -> int | None:
    for row in rows if isinstance(rows, list) else []:
        value = row.get("Id") if isinstance(row, dict) else None
        if is_id(value):
            return value
    return None


_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def is_guid(value: Any) -> bool:
    return isinstance(value, str) and _GUID.fullmatch(value) is not None


def first_guid_id(rows: Any) -> str | None:
    """The Id (GUID text) of the first row that has one."""
    for row in rows if isinstance(rows, list) else []:
        value = row.get("Id") if isinstance(row, dict) else None
        if is_guid(value):
            return value
    return None


def billable_item(rows: Any) -> str | None:
    """The Id of the first catalog row that belongs to nobody (ClientId null) or to the test client, else None.

    A row with another client's id, without a ClientId or without a GUID Id is not used: the invoice is for the test client,
    and an item that belongs to another client must never end up on it."""
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or "ClientId" not in row or not is_guid(row.get("Id")):
            continue
        owner = row["ClientId"]
        if owner is None or (is_id(owner) and owner == SITE.test_client):
            return row["Id"]
    return None


def invoice_ids(listing: Any) -> set[str]:
    """The lower-cased Ids of the rows of a list_invoices answer."""
    rows = listing.get("items") if isinstance(listing, dict) else None
    return {
        row["Id"].lower()
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and is_guid(row.get("Id"))
    }


def invoice_record(record: dict[str, Any], fallback: int = INVOICE_DRAFT) -> dict[str, Any]:
    """The manifest details of a new invoice, from the create tool's answer (the manifest contract: status_id, number,
    display_number). status_id is the status Gorelo STORED when the answer says one, else `fallback`: 1 for a Draft
    (create_invoice, the default) and 5 for the Approved invoice (create_approved_invoice, never 1: an invoice that may
    be approved must not look like a Draft). The guard deletes an invoice recorded as 1, and voids one recorded as 5
    only with allow_approved_invoice."""
    stored = dig(record, "Status", "Id")
    number, display = record.get("Number"), record.get("DisplayNumber")
    return {
        "status_id": stored if is_id(stored) else fallback,
        "number": number if is_id(number) else None,
        "display_number": display if isinstance(display, str) and display else None,
    }


def date_text(value: Any) -> Any:
    """The calendar date of a date or date-time answer (its first 10 characters); anything else is returned as it is."""
    return value[:10] if isinstance(value, str) else value


def check_invoice_line(invoice: dict[str, Any], item_id: str, label: str, tool: str) -> dict[str, Any]:
    """The one line the matrix asked for: that item, quantity 1, unit price 1.0 and the run label as its text."""
    lines = invoice.get("LineItems")
    if not isinstance(lines, list) or len(lines) != 1:
        found = f"{len(lines)} line items" if isinstance(lines, list) else f"no list of line items ({kind_of(lines)})"
        raise CheckFailed(f"{tool}: the invoice has {found}, expected exactly 1")
    line = lines[0]
    check(isinstance(line, dict), f"{tool}: the invoice line is not an object")
    check(same_guid(line.get("ItemId"), item_id), f"{tool}: the invoice line is not for the catalog item that was asked for")
    expect(f"{tool} line Quantity", line.get("Quantity"), INVOICE_LINE_QUANTITY)
    expect(f"{tool} line UnitPrice", line.get("UnitPrice"), INVOICE_LINE_PRICE)
    expect(f"{tool} line Description", line.get("Description"), label)
    return line


def invoice_name(invoice: dict[str, Any]) -> str:
    """How an invoice is named in a message: its DisplayNumber (INV-1042), else its Number, else its Id."""
    for key in ("DisplayNumber", "Number", "Id"):
        value = invoice.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if is_id(value):
            return str(value)
    return "(no number)"


def rate_limited(failure: ToolFailed) -> bool:
    """Did the tool fail because Gorelo answered 429 to every try (it did not process the request)? Then asking again is safe."""
    return RATE_LIMITED in failure.text and NOT_PROCESSED in failure.text


_TRACE_TAIL = re.compile(r"\s*\[trace [^\]]*\]\s*$")


def without_trace(text: str) -> str:
    """`text` without the ' [trace <id>]' that ends the text of every enveloped Gorelo error.

    The id differs from one request to the next, so two failures that say the same thing are told apart by their words and
    never by the id (a text that is cut at FAILURE_LIMIT may have lost it already; the comparison is made on the whole text)."""
    return _TRACE_TAIL.sub("", text)


def is_amount(value: Any) -> bool:
    """A finite number that is not a boolean (an invoice total as Gorelo writes it)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def email_problem(invoice: dict[str, Any]) -> str | None:
    """What the invoice says about having been emailed, or None when it says it was not (IsEmailSent false, EmailSentOn null).

    The matrix names no recipient and every contact of the test client is Inactive, so an invoice that reads as emailed is news:
    the text is written to be noticed (it goes into the notes of the area). A value that cannot be read is a problem too."""
    sent, on = invoice.get("IsEmailSent", _MISSING), invoice.get("EmailSentOn", _MISSING)
    if sent is False and on is None:
        return None
    if sent is True or (isinstance(on, str) and on.strip()):
        return f"WARNING: invoice EMAILED (IsEmailSent {sent!r}, EmailSentOn {on!r}) although no recipient was named"
    return (
        "WARNING: whether the invoice was emailed cannot be read "
        f"(IsEmailSent is {field_kind(sent)}, EmailSentOn is {field_kind(on)})"
    )


def amount_text(value: Any) -> str:
    """An invoice amount for a message: the number itself, else what the field held (missing, null or its type)."""
    return repr(value) if is_amount(value) else field_kind(value)


def tax_problem(invoice: dict[str, Any]) -> str | None:
    """What the invoice says against the no-tax $1 that was approved, or None when TotalTax is 0 and Total is 1.0.

    The line asked for no tax (TaxId sent as an explicit null) and billed 1 x 1.0, so a TotalTax other than 0 or a Total
    other than 1.0 means Gorelo did not take the null as "no tax" (or changed the amount). The text is written to be
    noticed and holds amounts and kinds only; a TotalTax or Total that is missing or not a number is a problem too."""
    tax, total = invoice.get("TotalTax", _MISSING), invoice.get("Total", _MISSING)
    wanted = INVOICE_LINE_QUANTITY * INVOICE_LINE_PRICE
    if is_amount(tax) and tax == 0 and is_amount(total) and total == wanted:
        return None
    return (
        f"WARNING: no-tax check FAILED: TotalTax is {amount_text(tax)} (expected 0) and Total is {amount_text(total)} "
        f"(expected {wanted!r}), so Gorelo may not have taken the explicit TaxId null as no tax"
    )


def inactive_contact(row: Any) -> bool:
    """True when a row of list_contacts says the contact is Inactive (Status.Name, case ignored): anything else, and a row that
    is not an object, is not provably Inactive."""
    name = dig(row, "Status", "Name")
    return isinstance(name, str) and name.strip().casefold() == INACTIVE_CONTACT


def no_billing_contacts(row: Any) -> bool:
    """True when a row of list_client_locations names no billing contact: BillingContactIds is present and null, an empty
    list, text with nothing in it, or the text of an empty JSON list ("[]": production answers exactly that for a
    location that names nobody, although the spec calls the field comma-separated ids). A row without the key, or with anything else
    (ids, a non-empty list in any form, text that is not JSON or cannot be read as JSON), is not provably free of them.
    It never raises: json.loads of text nested too deeply is a RecursionError, which is not a ValueError."""
    if not isinstance(row, dict) or "BillingContactIds" not in row:
        return False
    ids = row["BillingContactIds"]
    if ids is None or ids == []:
        return True
    if not isinstance(ids, str):
        return False
    text = ids.strip()
    if not text:
        return True
    try:
        return json.loads(text) == []
    except (ValueError, RecursionError):
        return False


def row_ids(rows: list[Any], limit: int = 10) -> str:
    """The ids of some rows for a message: at most `limit`, then how many more; a row without an integer Id shows as '?'."""
    shown = [str(row["Id"]) if isinstance(row, dict) and is_id(row.get("Id")) else "?" for row in rows[:limit]]
    return ", ".join(shown) + (f" and {len(rows) - limit} more" if len(rows) > limit else "")


def pdf_size(result: Any) -> int:
    """The size in bytes of the PDF file export_invoice_pdf answered with (a text and an embedded file); CheckFailed when
    there is no file, it is empty or it is not application/pdf."""
    for block in getattr(result, "content", None) or []:
        resource = getattr(block, "resource", None)
        if getattr(block, "type", None) != "resource" or resource is None:
            continue
        mime = getattr(resource, "mimeType", None)
        check(mime == "application/pdf", f"export_invoice_pdf attached a file of type {mime!r}, expected application/pdf")
        try:
            data = base64.b64decode(getattr(resource, "blob", None) or "", validate=True)
        except (ValueError, TypeError):
            raise CheckFailed("export_invoice_pdf attached a file whose content is not valid base64") from None
        check(data, "export_invoice_pdf attached an empty file")
        return len(data)
    raise CheckFailed("export_invoice_pdf answered without an embedded file")


def approver_problem(approval: dict[str, Any]) -> str | None:
    """Why the approval comment must not be posted, or None when every stored approver is the operator contact.

    The approval read back lists its approvers (Approvers: [{ContactId, Status}]); the comment emails them all, so
    there must be at least one and each must be the operator contact. An approver list that cannot be read is a reason too."""
    approvers = approval.get("Approvers", _MISSING)
    if not isinstance(approvers, list) or not approvers:
        return f"the stored approval has no readable list of approvers (Approvers is {kind_of(approvers)} or empty)"
    found = [row.get("ContactId", _MISSING) if isinstance(row, dict) else _MISSING for row in approvers]
    if not all(is_id(contact) for contact in found):
        return "an approver of the stored approval has no readable ContactId"
    others = sorted({contact for contact in found if contact != SITE.operator_contact})
    if others:
        listed = ", ".join(str(contact) for contact in others)
        return f"the stored approvers include contact(s) {listed}, not only the operator contact {SITE.operator_contact}"
    return None


PUBLISHED_SHAPE, FLAT_SHAPE, BOTH_SHAPES = "published", "flat", "both"  # how a time entry names its user (entry_user_shape)


def field_kind(value: Any) -> str:
    """What a field held, for a message about it: missing, null or the type of the value (never the value itself)."""
    return "null" if value is None else kind_of(value)


def entry_user_shape(entry: dict[str, Any], wanted: int | None = None) -> str:
    """Check whose time entry `entry` (read back) is and say how it names its user: PUBLISHED_SHAPE or FLAT_SHAPE.

    The published TimeEntryModel has a `User` object {Id, Name}, which production answers since 2026-10-03. On
    2026-10-02 it answered a flat `UserId` and no User object instead: that passes as FLAT_SHAPE, so the caller can note
    the drift. Anything else raises CheckFailed with what came back (ids and kinds, never the user's name): a User
    object whose Id is not `wanted` or is missing, a User that is not an object, a flat UserId that is not `wanted`, or
    neither field. When both are there they must name the same user: BOTH_SHAPES, which the caller notes as drift;
    a flat UserId that disagrees with the User object fails."""
    if wanted is None:
        wanted = SITE.operator_user
    user, flat = entry.get("User", _MISSING), entry.get("UserId", _MISSING)
    if user is not _MISSING and user is not None:
        if not isinstance(user, dict):
            raise CheckFailed(f"time entry User: expected an object {{Id, Name}}, got {kind_of(user)}")
        expect("time entry User.Id", user.get("Id"), wanted)
        if flat is not _MISSING and flat is not None:
            expect("time entry UserId (next to the User object)", flat, wanted)
            return BOTH_SHAPES
        return PUBLISHED_SHAPE
    if flat is not _MISSING and flat is not None:
        expect("time entry UserId", flat, wanted)
        return FLAT_SHAPE
    raise CheckFailed(
        f"time entry user: expected the User object with Id {wanted} (or the flat UserId {wanted} of 2026-10-02), "
        f"got neither: User is {field_kind(user)} and UserId is {field_kind(flat)}"
    )


def named_id(rows: Any, name: str, noun: str) -> int:
    """The Id of the one row whose Name equals `name` (ignoring case); CheckFailed listing the names otherwise."""
    found = [
        row["Id"]
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict)
        and isinstance(row.get("Name"), str)
        and row["Name"].strip().casefold() == name.casefold()
        and isinstance(row.get("Id"), int)
        and not isinstance(row.get("Id"), bool)
    ]
    if len(found) != 1:
        names = ", ".join(sorted(str(row.get("Name")) for row in rows if isinstance(row, dict))) or "none"
        raise CheckFailed(f"cannot resolve the {noun} named {name!r} ({len(found)} matches); the tenant has: {names}")
    return found[0]


@dataclass(frozen=True)
class Lookups:
    """The tenant ids the ticket creates need, resolved by name once per run (they are tenant data, never constants)."""

    statuses: list[dict[str, Any]]
    incident: int
    everyone: int
    api: int
    normal: int

    def status(self, name: str) -> int:
        return named_id(self.statuses, name, "ticket status")


# --------------------------------------------------------------------------
# The run context
# --------------------------------------------------------------------------


class Matrix:
    """What the areas share: the tool session (the only way anything is sent), the manifest, the lookups, the run ticket.
    """

    def __init__(
        self,
        *,
        manifest: Manifest,
        guard: LiveGuard,
        pacer: Pacer,
        session: ToolSession,
        secret: str,
        clock: Callable[[], datetime],
        lookup_wait: float,
        scopes_missing: set[str],
        with_items: bool = False,
        with_invoices: bool = False,
        with_approved_invoice: bool = False,
        sync_interval: float = SYNC_INTERVAL,
        sync_polls: int = SYNC_POLLS,
        gorelo: GoreloClient | None = None,
    ) -> None:
        if with_approved_invoice and gorelo is None:  # the one create is sent with 429 retries off: the client must be reachable
            raise SetupError(
                "the approved invoice run needs the Gorelo client behind its server, to switch off the 429 retries of the one "
                "create: the approved invoice must never be sent twice, so the run did not start"
            )
        self.manifest = manifest
        self.guard = guard
        self.pacer = pacer
        self.session = session
        self.secret = secret
        self.clock = clock
        self.lookup_wait = lookup_wait
        self.scopes_missing = scopes_missing
        self.with_items = with_items
        self.with_invoices = with_invoices
        self.with_approved_invoice = with_approved_invoice
        self.sync_interval = sync_interval
        self.sync_polls = sync_polls
        self.gorelo = gorelo  # only the approved invoice run holds it (see without_429_retries)
        self.notes: list[str] = []
        self._lookups: Lookups | None = None
        self._plain: dict[str, Any] | None = None
        self._plain_error: str | None = None
        self._location: int | None = None

    # -- plumbing ----------------------------------------------------------

    def note(self, text: str) -> None:
        self.notes.append(scrub(" ".join(text.split()), self.secret))

    async def tool(self, name: str, /, **arguments: Any) -> dict[str, Any]:
        """Call a tool (the name is positional-only: create_client and friends take a `name` argument of their own)."""
        return await self.session.call(name, arguments)

    async def tool_result(self, name: str, /, **arguments: Any) -> Any:
        """Call a tool whose answer is a file and not a structured object (export_invoice_pdf): its raw result."""
        return await self.session.call_result(name, arguments)

    def without_429_retries(self) -> contextlib.AbstractContextManager[None]:
        """The window of the one approved create: `with matrix.without_429_retries():` around that call and nothing else (see
        no_429_retries). The run's own client retries no 429 inside it and has its normal retries again right after it, also
        when the call raised. SetupError when this Matrix was given no client (only the approved invoice run is)."""
        if self.gorelo is None:
            raise SetupError(
                "this run holds no Gorelo client whose 429 retries could be switched off: the approved invoice must never be "
                "sent twice, so it is not created"
            )
        return no_429_retries(self.gorelo)

    async def create(
        self,
        kind: str,
        label: str,
        details: dict[str, Any],
        tool: str,
        arguments: dict[str, Any],
        *,
        ident: Callable[[dict[str, Any]], Any] = lambda record: record["Id"],
        recorded: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """announce, create, record: manifest.intent BEFORE the call, manifest.created right after it.

        `details` go into the intent and, unless `recorded` is given, into the record. `recorded(answer)` builds the
        record's details from the answer itself (an invoice's number is only known after the create), so the record
        is written once, at once, before any check of the answer.

        A create that surely made nothing (the guard refused it, it never left the process, Gorelo answered 4xx)
        settles its intent as failed; anything else (a timeout, a 5xx, an answer without an Id) leaves the intent
        open, so the cleanup report lists it as announced but never recorded."""
        seq = self.manifest.intent(kind, label, details)
        sent = self.pacer.requests
        try:
            record = await self.tool(tool, **arguments)
        except GuardTripped:
            self.manifest.intent_failed(seq, "the guard refused the request; nothing was sent")
            raise
        except ToolFailed as exc:
            if not exc.sent or exc.rejected:
                self.manifest.intent_failed(seq, exc.text[:200] or "refused")
            raise
        except BaseException:
            if self.pacer.requests == sent:  # failed before anything left the process: nothing was created
                self.manifest.intent_failed(seq, "the call failed before a request was sent")
            raise
        try:
            identity = ident(record)
        except (KeyError, TypeError):
            raise CheckFailed(
                f"{tool} answered without an Id: the {kind} may exist in Gorelo, search for {label!r}"
            ) from None
        try:
            self.manifest.created(kind, identity, label, details if recorded is None else recorded(record))
        except ValueError as exc:
            raise CheckFailed(
                f"the new {kind} has an Id the manifest cannot record ({exc}); it exists in Gorelo, search for {label!r}"
            ) from None
        return record

    def mark_cleaned(self, kind: str, identity: Any, how: str) -> None:
        self.manifest.cleaned(kind, identity, how)

    def mark_voided(self, invoice_id: str) -> None:
        """The approved invoice was voided and stays listed as Void: a known residue, not a cleaned record (a voided invoice
        cannot be removed). Its stored status becomes 4 first, so the guard allows no further DELETE of it."""
        self.manifest.update_details("invoice", invoice_id, status_id=INVOICE_VOID)
        self.manifest.cleanup_failed("invoice", invoice_id, VOIDED_BY_MATRIX)

    def stamp(self, ago: timedelta, offset_hours: int = 0) -> str:
        """An ISO 8601 time `ago` before now, written with an explicit offset (Z for UTC)."""
        moment = (self.clock() - ago).astimezone(timezone(timedelta(hours=offset_hours))).replace(microsecond=0)
        return moment.isoformat().replace("+00:00", "Z")

    # -- shared lookups and records ------------------------------------------

    async def lookups(self) -> Lookups:
        if self._lookups is None:
            statuses = (await self.tool("list_ticket_statuses"))["items"]
            types = (await self.tool("list_ticket_types"))["items"]
            groups = (await self.tool("list_org_groups"))["items"]
            sources = (await self.tool("list_ticket_sources"))["items"]
            priorities = (await self.tool("list_ticket_priorities"))["items"]
            self._lookups = Lookups(
                statuses=statuses,
                incident=named_id(types, "Incident", "ticket type"),
                everyone=named_id(groups, "Everyone", "group"),
                api=named_id(sources, "Api", "ticket source"),
                normal=named_id(priorities, "Normal", "ticket priority"),
            )
        return self._lookups

    async def new_ticket(
        self,
        text: str,
        *,
        client_id: int | None = None,
        status: str = "New",
        contact_id: int | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """create_ticket named after the run, then the check of who the STORED ticket would email (stored_audience).

        `contact_id` is None or the operator contact. The ticket is announced and recorded with its client only: its
        contact_id and cc_contact_ids come from the read-back, so until stored_audience has passed the manifest
        leaves the audience unknown and the guard allows no public comment on it. Every run ticket is made here, so
        none of them can be used before the check: it raises CheckFailed and the caller's area ends on the spot."""
        if client_id is None:
            client_id = SITE.test_client
        if contact_id not in (None, SITE.operator_contact):
            raise ValueError(
                f"a run ticket gets no contact or the operator contact {SITE.operator_contact}, not {contact_id!r}"
            )
        lookups = await self.lookups()
        label = self.manifest.label(text)
        arguments: dict[str, Any] = {
            "title": label,
            "description": f"{label}: created by the live write matrix, safe to delete.",
            "client_id": client_id,
            "status_id": lookups.status(status),
            "type_id": lookups.incident,
            "priority_id": lookups.normal,
            "source_id": lookups.api,
            "group_id": lookups.everyone,
            **extra,
        }
        if contact_id is not None:
            arguments["contact_id"] = contact_id
        record = await self.create("ticket", label, {"client_id": client_id}, "create_ticket", arguments)
        self.stored_audience(text, record, contact_id)
        return record

    def stored_audience(self, text: str, record: dict[str, Any], wanted: int | None) -> None:
        """The STORED ticket must email exactly who the matrix asked for (nobody or `wanted`); the manifest must know too.

        `record` is create_ticket's answer, the ticket as Gorelo stored it (a tenant automation or Gorelo itself may
        have set a contact or CCs the matrix never sent). Called before anything else is sent for the ticket: a
        CheckFailed fails the area at once, so no comment, status change or approval follows. The stored ContactId
        and CcContactIds go into the manifest first, whatever they are, so the guard's public-comment rule judges the
        truth; a read-back that failed or values that cannot be read (a missing key, an odd type) leave the manifest
        without them, which the guard treats as an unknown audience, and fail the check as well."""
        full(record, "create_ticket")
        ticket = record["Id"]
        contact, copies = record.get("ContactId", _MISSING), record.get("CcContactIds", _MISSING)
        if (contact is not None and not is_id(contact)) or not isinstance(copies, list) or not all(map(is_id, copies)):
            raise CheckFailed(
                f"create_ticket: ticket {ticket} reads back with a ContactId ({kind_of(contact)}) or CcContactIds "
                f"({kind_of(copies)}) that cannot be read as contact ids, so who it would email is unknown; "
                "nothing more is sent for it"
            )
        self.manifest.update_details("ticket", ticket, contact_id=contact, cc_contact_ids=list(copies))
        asked = "none" if wanted is None else str(wanted)
        problems = []
        if contact != wanted:
            problems.append(f"ContactId is {'none' if contact is None else contact}, the matrix asked for {asked}")
        unexpected = [cc for cc in copies if cc != wanted]
        if unexpected:
            only = "none" if wanted is None else f"only {asked}"
            problems.append(f"CcContactIds holds {unexpected}, the matrix asked for {only}")
        if problems:
            raise CheckFailed(
                f"ticket {ticket} was stored with an audience the matrix did not ask for ({'; '.join(problems)}); "
                "the area stops here, before any comment, status change or approval"
            )
        self.note(f"{text}: stored audience verified (ContactId {contact}, CcContactIds {copies})")

    async def plain_ticket(self) -> dict[str, Any]:
        """The run's plain ticket on the test client (no contact), created on first use; the comments and time areas build on it."""
        if self._plain is not None:
            return self._plain
        if self._plain_error is not None:
            raise Skipped(f"no run ticket: {self._plain_error}")
        try:
            self._plain = await self.new_ticket("plain ticket")
        except (ToolFailed, CheckFailed) as exc:
            self._plain_error = str(exc)[:160]
            raise
        return self._plain

    async def location(self) -> int:
        """the test client's default location (list_client_locations), for the uptime check and the project."""
        if self._location is None:
            rows = (await self.tool("list_client_locations", client_id=SITE.test_client))["items"]
            defaults = [row for row in rows if isinstance(row, dict) and row.get("IsDefault") is True]
            found = first_int_id(defaults) or first_int_id(rows)
            check(found is not None, f"client {SITE.test_client} has no location")
            self._location = found
        return self._location  # type: ignore[return-value]

    async def comment(
        self,
        ticket: str,
        text: str,
        kind: str = "private",
        *,
        conversation_id: Any = None,
        attachments: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """A comment of the run's ticket, recorded as private or not (a non-private one can only go with its ticket)."""
        label = self.manifest.label(text)
        arguments: dict[str, Any] = {"ticket_id": ticket, "body": f"<p>{label}</p>", "conversation_type": kind}
        if conversation_id is not None:
            arguments["conversation_id"] = conversation_id
        if attachments is not None:
            arguments["attachments"] = attachments
        details = {"ticket_id": ticket, "private": kind == "private"}
        record = await self.create("comment", label, details, "create_ticket_comment", arguments)
        return full(record, "create_ticket_comment")

    async def get_ticket(self, reference: Any, how: str) -> dict[str, Any]:
        """get_ticket; a number or display number is retried a few times (the new ticket may not be searchable yet)."""
        attempts = 1 if how == "guid" else LOOKUP_ATTEMPTS
        for attempt in range(1, attempts + 1):
            try:
                record = await self.tool("get_ticket", ticket_id=reference)
            except ToolFailed as exc:
                if attempt < attempts and "no ticket has the number or display number" in exc.text:
                    await asyncio.sleep(self.lookup_wait)
                    continue
                raise
            if attempt > 1:
                self.note(f"get_ticket by {how} found the new ticket on attempt {attempt}")
            return record
        raise AssertionError("unreachable")

    async def invoice_listing(self, label: str, invoice_id: str, *, listed: bool, voided: bool = False) -> None:
        """list_invoices(client_ids=[the test client], query=label) must list the invoice (listed=True) or no longer list it.

        A search can lag behind a write (the ticket search does), and a failed run burns an invoice number, so it is
        read up to LOOKUP_ATTEMPTS times, LOOKUP_WAIT seconds apart, before the check fails. `voided` only words the
        failure for the approved invoice, which a void leaves listed (as Void)."""
        for attempt in range(1, LOOKUP_ATTEMPTS + 1):
            listing = await self.tool("list_invoices", client_ids=[SITE.test_client], query=label)
            if (invoice_id.lower() in invoice_ids(listing)) is listed:
                if attempt > 1:
                    what = (
                        "listed the voided invoice"
                        if listed and voided
                        else "listed the new invoice"
                        if listed
                        else "no longer listed the invoice"
                    )
                    self.note(f"list_invoices {what} on attempt {attempt}")
                return
            if attempt < LOOKUP_ATTEMPTS:
                await asyncio.sleep(self.lookup_wait)
        raise CheckFailed(
            "list_invoices did not list the voided invoice (a void leaves it listed, as Void)"
            if listed and voided
            else "list_invoices did not list the new invoice"
            if listed
            else "list_invoices still lists the deleted Draft invoice"
        )

    async def delete_draft(self, number: int) -> dict[str, Any]:
        """delete_invoice for a Draft, by its Number (see remove_invoice)."""
        return await self.remove_invoice(number, "Draft")

    async def remove_invoice(self, number: int, expected: str) -> dict[str, Any]:
        """delete_invoice by Number for an invoice of the status `expected` ("Draft": deleted, "Approved": voided).

        It is asked again, up to LOOKUP_ATTEMPTS times in all and LOOKUP_WAIT seconds apart, when the call changed nothing:
        its lookup found no invoice (the list may lag behind the create), or Gorelo answered 429 to its lookup or to its
        DELETE even after the client's own retries (rate_limited: Gorelo did not process the request, so nothing was deleted
        or voided and a second DELETE is safe; the guard has no once-only rule for it). Any other failure ends the call."""
        lagged = limited = 0
        for attempt in range(1, LOOKUP_ATTEMPTS + 1):
            try:
                answer = await self.tool("delete_invoice", invoice_number=number, expected_status=expected, confirm=True)
            except ToolFailed as exc:
                if attempt < LOOKUP_ATTEMPTS and NO_SUCH_NUMBER in exc.text:
                    lagged += 1
                elif attempt < LOOKUP_ATTEMPTS and rate_limited(exc):
                    limited += 1
                else:
                    raise
                await asyncio.sleep(self.lookup_wait)
                continue
            if lagged:
                self.note(f"delete_invoice found the invoice by its Number on attempt {attempt}")
            if limited:
                self.note(
                    f"delete_invoice was rate limited by Gorelo (HTTP 429) {limited} time{'' if limited == 1 else 's'} and "
                    f"went through on attempt {attempt}"
                )
            return answer
        raise AssertionError("unreachable")

    async def probe_scope(self, tool: str, **arguments: Any) -> str | None:
        """Call a read tool; the missing scope's name when Gorelo answers 403 code 080203, None when it answered."""
        try:
            await self.tool(tool, **arguments)
        except ToolFailed as exc:
            if exc.scope is None:
                raise
            self.scopes_missing.add(exc.scope)
            return exc.scope
        return None


# --------------------------------------------------------------------------
# The areas
# --------------------------------------------------------------------------


async def area_clients(m: Matrix) -> None:
    # update_client is exercised on the run's own temporary client only: the configured test client is never read for this, never
    # changed, and the guard refuses a PATCH of it.
    label = m.manifest.label("temporary client")
    made = await m.create(
        "client",
        label,
        {},
        "create_client",
        {
            "name": label,
            "location_name": m.manifest.label("location"),
            "location_phone": CLIENT_PHONE,
            "location_phone_country_code": REGION,
        },
    )
    client = made["Id"]
    expect("create_client Name", made.get("Name"), label)
    rows = (await m.tool("list_client_locations", client_id=client))["items"]
    check(rows, "the temporary client has no location")
    m.note(f"temporary client {client}: location phone region stored as {dig(rows[0], 'PhoneCountryCode')!r}")
    alternate = m.manifest.label("alternate name")
    updated = await m.tool("update_client", client_id=client, alternate_name=alternate)
    expect("update_client AlternateName on the temporary client", updated.get("AlternateName"), alternate)
    back = await m.tool("get_client", client_id=client)
    expect("get_client AlternateName on the temporary client", back.get("AlternateName"), alternate)


async def area_contacts(m: Matrix) -> None:
    run = m.manifest.run_id
    label = m.manifest.label("contact")
    made = await m.create(
        "contact",
        label,
        {"client_id": SITE.test_client},
        "create_contact",
        {
            "client_id": SITE.test_client,
            "first_name": run,
            "last_name": "Contact",
            "primary_email": f"{run.lower()}@example.invalid",
            "mobile_phone": CONTACT_PHONE,
            "mobile_phone_country_code": REGION,
        },
    )
    contact = made["Id"]
    check(
        digits(made.get("MobilePhone")).endswith(CONTACT_PHONE),
        f"create_contact MobilePhone is {made.get('MobilePhone')!r}, expected the number {CONTACT_PHONE}",
    )
    expect("create_contact MobilePhoneCountryCode", made.get("MobilePhoneCountryCode"), REGION)

    title = f"{run} title"
    updated = await m.tool("update_contact", contact_id=contact, job_title=title, clear_secondary_email_ok=True)
    expect("update_contact JobTitle", updated.get("JobTitle"), title)
    back = await m.tool("get_contact", contact_id=contact)
    expect("JobTitle read back", back.get("JobTitle"), title)
    expect("MobilePhone after the update (PATCH replaces the contact)", back.get("MobilePhone"), made.get("MobilePhone"))
    expect("MobilePhoneCountryCode after the update", back.get("MobilePhoneCountryCode"), REGION)
    expect("FirstName after the update", back.get("FirstName"), run)
    expect("LastName after the update", back.get("LastName"), "Contact")

    try:
        await m.tool("update_contact", contact_id=contact, job_title=f"{run} second title")
    except ToolFailed as exc:
        check(not exc.sent, "the refused update_contact still sent a request")
        check("secondary_email" in exc.text, "update_contact was refused, but not for the missing secondary-email choice")
    else:
        raise CheckFailed("update_contact without a secondary-email choice was not refused")


async def area_tickets(m: Matrix) -> None:
    run = m.manifest.run_id
    lookups = await m.lookups()
    plain = full(await m.plain_ticket(), "create_ticket")
    ticket = plain["Id"]
    expect("plain ticket Title", plain.get("Title"), m.manifest.label("plain ticket"))
    expect("plain ticket ClientId", plain.get("ClientId"), SITE.test_client)
    expect("plain ticket Status.Id", dig(plain, "Status", "Id"), lookups.status("New"))
    expect("plain ticket Type.Id", dig(plain, "Type", "Id"), lookups.incident)
    expect("plain ticket Priority.Id", dig(plain, "Priority", "Id"), lookups.normal)
    expect("plain ticket Source.Id", dig(plain, "Source", "Id"), lookups.api)
    check(lookups.everyone in (plain.get("GroupIds") or []), "the plain ticket is not in the Everyone group")

    created_on, closed_on = m.stamp(timedelta(days=3), -5), m.stamp(timedelta(days=2), -5)
    old = full(
        await m.new_ticket("backdated ticket", status="Closed", created_on=created_on, closed_on=closed_on),
        "create_ticket",
    )
    check(old.get("ClosedOn"), "the backdated ticket has no ClosedOn")
    try:
        stored = datetime.fromisoformat(str(old.get("CreatedOn")).replace("Z", "+00:00"))
    except ValueError:
        raise CheckFailed(f"the backdated ticket's CreatedOn is not a datetime: {old.get('CreatedOn')!r}") from None
    wanted = datetime.fromisoformat(created_on)
    check(
        abs((stored - wanted).total_seconds()) <= 120,
        f"the backdated ticket's CreatedOn is {old.get('CreatedOn')!r}, expected about {created_on}",
    )

    for how, reference in (("guid", ticket), ("number", plain.get("Number")), ("display number", plain.get("DisplayNumber"))):
        check(reference not in (None, ""), f"the plain ticket has no {how} to look it up with")
        got = await m.get_ticket(reference, how)
        check(same_guid(got.get("Id"), ticket), f"get_ticket by {how} returned another ticket")

    # One technician cannot be lead (or assisting) and watcher at once: Gorelo answers 400 "Technician already exists"
    # (live, 2026-10-02). So the watcher goes on first and is verified, it is cleared, and only then does the same
    # user become the lead, in a later PATCH: the two never share one.
    title = m.manifest.label("plain ticket (updated)")
    updated = full(
        await m.tool("update_ticket", ticket_id=ticket, title=title, watcher_ids=[SITE.operator_user]), "update_ticket"
    )
    expect("update_ticket Title", updated.get("Title"), title)
    expect("update_ticket WatcherIds", updated.get("WatcherIds"), [SITE.operator_user])
    cleared = full(await m.tool("update_ticket", ticket_id=ticket, clear_fields=["watcher_ids"]), "update_ticket")
    expect("WatcherIds after clear_fields=['watcher_ids']", cleared.get("WatcherIds"), [])
    m.note(f"watchers: the operator user {SITE.operator_user} set, then cleared with clear_fields (no tenant tag is touched)")
    led = full(await m.tool("update_ticket", ticket_id=ticket, lead_assignee_id=SITE.operator_user), "update_ticket")
    expect("update_ticket LeadAssigneeId", led.get("LeadAssigneeId"), SITE.operator_user)
    expect("WatcherIds after the lead was set", led.get("WatcherIds"), [])
    m.note(f"lead assignee: the operator user {SITE.operator_user} set in a later update, after the watcher was cleared")

    role = first_int_id((await m.tool("list_billing_roles"))["items"])
    work_type = first_int_id((await m.tool("list_work_types"))["items"])
    if role is None or work_type is None:
        m.note("billing read-fill skipped: the tenant has no billing role or no work type")
    else:
        first = full(
            await m.tool("update_ticket", ticket_id=ticket, billing_role_id=role, billing_work_type_id=work_type),
            "update_ticket",
        )
        expect("BillingOverride.BillingRole.Id", dig(first, "BillingOverride", "BillingRole", "Id"), role)
        expect("BillingOverride.WorkType.Id", dig(first, "BillingOverride", "WorkType", "Id"), work_type)
        second = full(await m.tool("update_ticket", ticket_id=ticket, billable_status_id=NO_CHARGE), "update_ticket")
        expect("BillingOverride.BillableStatus.Id", dig(second, "BillingOverride", "BillableStatus", "Id"), NO_CHARGE)
        expect("BillingRole kept by the read-fill", dig(second, "BillingOverride", "BillingRole", "Id"), role)
        expect("WorkType kept by the read-fill", dig(second, "BillingOverride", "WorkType", "Id"), work_type)

    def ids_of(found: dict[str, Any]) -> set[str]:
        return {
            row["Id"].lower() for row in found["items"] if isinstance(row, dict) and isinstance(row.get("Id"), str)
        }

    wanted_ids = {"plain": ticket, "backdated": old["Id"]}
    found = await m.tool("search_tickets", client_id=SITE.test_client, limit=500)
    seen = ids_of(found)
    missing = [name for name, value in wanted_ids.items() if value.lower() not in seen]
    if missing and found.get("truncated"):
        narrow = await m.tool("search_tickets", client_id=SITE.test_client, query=run, limit=50)
        seen = ids_of(narrow)
        missing = [name for name, value in wanted_ids.items() if value.lower() not in seen]
        m.note("search_tickets was truncated for the whole client: the run id was added as a query")
    check(not missing, f"search_tickets(client_id={SITE.test_client}) did not return the run's {' and '.join(missing)} ticket")
    m.note(f"search_tickets: matched={found.get('matched')} scanned={found.get('scanned')} truncated={found.get('truncated')}")


async def area_comments(m: Matrix) -> None:
    ticket = (await m.plain_ticket())["Id"]
    private = await m.comment(ticket, "private comment")
    comment = private["Id"]
    expect("private comment ConversationType.Id", dig(private, "ConversationType", "Id"), 2)
    listing = await m.tool("list_ticket_comments", ticket_id=ticket, conversation_types=["private"])
    listed = {row["Id"].lower() for row in listing["items"] if isinstance(row, dict) and isinstance(row.get("Id"), str)}
    check(comment.lower() in listed, "list_ticket_comments did not list the new private comment")
    got = await m.tool("get_ticket_comment", ticket_id=ticket, comment_id=comment)
    check(same_guid(got.get("Id"), comment), "get_ticket_comment returned another comment")
    gone = await m.tool("delete_ticket_comment", ticket_id=ticket, comment_id=comment, confirm=True)
    check(same_guid(gone.get("Id"), comment), "delete_ticket_comment answered with another comment's Id")
    m.mark_cleaned("comment", comment, "deleted by the write matrix (delete_ticket_comment)")

    name = f"{m.manifest.run_id}-attachment.txt"
    label = m.manifest.label("attachment")
    upload = await m.create(
        "attachment",
        label,
        {"ticket_id": ticket},
        "upload_attachment",
        {"item_type": "ticket", "item_id": ticket, "filename": name, "content_text": f"{label}: a small text file."},
        ident=lambda record: record["name"],
    )
    attached = await m.comment(
        ticket, "private comment with attachment", attachments=[{"name": upload["name"], "url": upload["url"]}]
    )
    names = [dig(item, "Name") for item in (attached.get("Attachments") or [])]
    check(upload["name"] in names, "the comment does not list the uploaded attachment")


async def area_email(m: Matrix) -> None:
    lookups = await m.lookups()
    target = lookups.status(SITE.updated_status)  # resolved first: a missing status must fail before anything is emailed
    # new_ticket has read the stored ticket back and verified who it emails (ContactId the operator contact, CcContactIds only the operator contact)
    # and written that into the manifest: a ticket stored any other way ended this area right there, before this line.
    record = await m.new_ticket(
        "email ticket", client_id=SITE.second_client, contact_id=SITE.operator_contact, send_created_email=True
    )
    ticket = record["Id"]
    expect("email ticket ClientId", record.get("ClientId"), SITE.second_client)
    expect("email ticket ContactId", record.get("ContactId"), SITE.operator_contact)
    m.note("email ticket created with send_created_email=true (one email, to the operator contact only)")

    public = await m.comment(ticket, "public comment", "public")
    expect("public comment ConversationType.Id", dig(public, "ConversationType", "Id"), 1)
    m.note(f"public comment email status: {dig(public, 'EmailInfo', 'Status', 'Name')!r}")

    moved = full(await m.tool("update_ticket", ticket_id=ticket, status_id=target), "update_ticket")
    expect("update_ticket Status.Id", dig(moved, "Status", "Id"), target)

    label = m.manifest.label("side conversation")
    side = await m.create(
        "side_conversation",
        label,
        {"ticket_id": ticket},
        "create_ticket_side_conversation",
        {"ticket_id": ticket, "name": label, "email": SITE.operator_email},
    )
    await m.comment(ticket, "side conversation comment", "side_conversation", conversation_id=side["Id"])

    label = m.manifest.label("approval")
    try:
        approval = await m.create(
            "approval",
            label,
            {"ticket_id": ticket},
            "create_ticket_approval",
            {"ticket_id": ticket, "name": label, "contact_ids": [SITE.operator_contact]},
        )
    except ToolFailed as exc:
        if not (exc.rejected and (exc.status in APPROVER_STATUSES or APPROVER_HINT.search(exc.detail))):
            raise
        m.note(f"approval: skipped: approver not eligible (Gorelo said: {exc.detail[:160]})")
        return
    full(approval, "create_ticket_approval")
    problem = approver_problem(approval)
    if problem is not None:  # the comment would email these approvers: not unless every one is the operator contact
        m.note(f"approval comment: skipped: {problem}; no approval comment was posted, so nobody was emailed")
        return
    await m.comment(ticket, "approval comment", "approval", conversation_id=approval["Id"])


async def area_time(m: Matrix) -> None:
    ticket = (await m.plain_ticket())["Id"]
    label = m.manifest.label("time entry")
    entry = full(
        await m.create(
            "time_entry",
            label,
            {"ticket_id": ticket},
            "create_time_entry",
            {
                "user_id": SITE.operator_user,
                "ticket_id": ticket,
                "started_on": m.stamp(timedelta(minutes=30)),
                "ended_on": m.stamp(timedelta(minutes=15)),
                "billable_status_id": NO_CHARGE,
                "no_service_line": True,
                "comment": label,
            },
        ),
        "create_time_entry",
    )
    entry_id = entry["Id"]
    expect("time entry BillableStatus.Id", dig(entry, "BillableStatus", "Id"), NO_CHARGE)
    shape = entry_user_shape(entry)
    if shape == FLAT_SHAPE:  # production answered like this on 2026-10-02: pass, but say so
        m.note(
            f"time entry user: live returned the flat UserId {SITE.operator_user} (the 2026-10-02 shape) instead of the "
            "published User object {Id, Name}; drift worth knowing"
        )
    elif shape == BOTH_SHAPES:  # the published object plus a flat id the model does not have: pass, but say so
        m.note(
            f"time entry user: live returned both the published User object and a flat UserId {SITE.operator_user}; "
            "drift worth knowing"
        )
    hours = entry.get("ActualHours")
    check(hours is None or abs(hours - 0.25) < 0.01, f"time entry ActualHours is {hours!r}, expected 0.25")
    m.note(f"time entry service line Id: {dig(entry, 'ServiceLine', 'Id')!r} (no_service_line=true)")

    comment = f"{label} (updated)"
    changed = await m.tool("update_time_entry", time_entry_id=entry_id, comment=comment)
    expect("update_time_entry Comment", changed.get("Comment"), comment)

    calls = 0
    while True:
        calls += 1
        done = await m.tool("delete_time_entry", time_entry_id=entry_id, confirm=True)
        outcome = done.get("Outcome")
        if outcome == "Deleted":
            break
        check(outcome == "Reopened", f"delete_time_entry answered an unexpected Outcome ({outcome!r})")
        check(calls < MAX_REOPEN_ATTEMPTS, f"the time entry was still Reopened after {MAX_REOPEN_ATTEMPTS} delete calls")
    m.mark_cleaned(
        "time_entry",
        entry_id,
        "deleted by the write matrix (delete_time_entry)" + (f" after {calls} calls (Reopened first)" if calls > 1 else ""),
    )
    try:
        await m.tool("get_time_entry", time_entry_id=entry_id)
    except ToolFailed as exc:
        m.note(f"get_time_entry after the delete: HTTP {exc.status}")
    else:
        m.note("get_time_entry after the delete: the entry is still readable (the docs say 404)")


async def area_items(m: Matrix) -> None:
    if not m.with_items:  # nothing is announced or sent: the item tools are not even callable without the flag
        raise Skipped(ITEMS_SKIP_REASON)
    label = m.manifest.label("item")
    item = full(
        await m.create(
            "item",
            label,
            {"client_id": SITE.test_client},
            "create_item",
            {
                "type": "product",
                "name": label,
                "client_id": SITE.test_client,
                "description": f"{label}: created by the live write matrix.",
                "unit_price": 1.0,
            },
        ),
        "create_item",
    )
    item_id = item["Id"]
    expect("create_item Name", item.get("Name"), label)
    expect("create_item ClientId", item.get("ClientId"), SITE.test_client)
    expect("create_item Type.Id", dig(item, "Type", "Id"), 1)
    description = f"{label}: updated"
    changed = full(
        await m.tool("update_item", item_id=item_id, description=description, unit_price=2.0), "update_item"
    )
    expect("update_item Description", changed.get("Description"), description)
    expect("update_item UnitPrice", changed.get("UnitPrice"), 2.0)
    gone = await m.tool("delete_item", item_id=item_id, confirm=True)
    check(same_guid(gone.get("Id"), item_id), "delete_item answered with another item's Id")
    m.mark_cleaned("item", item_id, "deleted by the write matrix (delete_item)")


async def area_invoices(m: Matrix) -> None:
    """A Draft invoice for the test client, created, read, listed, exported as a PDF and deleted again.

    Never an Approved one: create_approved_invoice is not a tool the matrix may call (MATRIX_TOOLS), and the guard
    refuses any StatusId but 1, any recipient and any other client."""
    if not m.with_invoices:  # nothing is announced or sent: the invoice tools are not even callable without the flag
        raise Skipped(INVOICES_SKIP_REASON)
    found = await m.tool("list_items", type="product", status="active", page_size=CATALOG_PAGE)
    item_id = billable_item(found.get("items"))
    if item_id is None:
        raise Skipped(
            f"no active product of nobody or of client {SITE.test_client} among the first {CATALOG_PAGE} catalog items "
            "to put on the invoice"
        )
    today = m.clock().astimezone(timezone.utc).date()
    invoice_date, due_date = today.isoformat(), (today + timedelta(days=INVOICE_DUE_DAYS)).isoformat()
    label = m.manifest.label("invoice")
    # The invoice is recorded (status_id, number, display_number) the moment create_invoice answers, before any check,
    # so the cleanup can find it even when a check fails. An answer without an Id leaves the intent open: the cleanup
    # then searches list_invoices (the test client) for the run label.
    made = await m.create(
        "invoice",
        label,
        {"client_id": SITE.test_client, "status_id": INVOICE_DRAFT},
        "create_invoice",
        {
            "client_id": SITE.test_client,
            "line_items": [
                {
                    "item_id": item_id,
                    "quantity": INVOICE_LINE_QUANTITY,
                    "description": label,
                    "unit_price": INVOICE_LINE_PRICE,
                }
            ],
            "invoice_date": invoice_date,
            "due_date": due_date,
            "reference": label,
        },
        recorded=invoice_record,
    )
    invoice_id = made["Id"]
    invoice = full(made, "create_invoice")
    expect("create_invoice Status.Id (Draft)", dig(invoice, "Status", "Id"), INVOICE_DRAFT)
    expect("create_invoice ClientId", invoice.get("ClientId"), SITE.test_client)
    expect("create_invoice Reference", invoice.get("Reference"), label)
    expect("create_invoice InvoiceDate", date_text(invoice.get("InvoiceDate")), invoice_date)
    expect("create_invoice DueDate", date_text(invoice.get("DueDate")), due_date)
    check_invoice_line(invoice, item_id, label, "create_invoice")
    m.note(
        f"invoice number consumed: Number {invoice.get('Number')!r}, DisplayNumber {invoice.get('DisplayNumber')!r} "
        "(a Draft uses up one invoice number)"
    )

    got = await m.tool("get_invoice", invoice_id=invoice_id)
    check(same_guid(got.get("Id"), invoice_id), "get_invoice returned another invoice")
    expect("get_invoice Number", got.get("Number"), invoice.get("Number"))
    check_invoice_line(got, item_id, label, "get_invoice")

    await m.invoice_listing(label, invoice_id, listed=True)

    exported = await m.tool_result("export_invoice_pdf", invoice_id=invoice_id)
    size = pdf_size(exported)
    m.note(
        f"export_invoice_pdf: {size} bytes of application/pdf; the export event is recorded on the test invoice, "
        "which is deleted next"
    )

    # Nothing is deleted unless the invoice is still a Draft. Gorelo's DELETE: Draft: deleted (no longer listed).
    # Approved: voided (status Void, still listed), and this area never voids: only the approved_invoice area does, for
    # the one invoice it approved itself. Whatever else this area finds is left for the user (the cleanup reads it again
    # and does the same: it voids an Approved invoice recorded as 5 only when the user runs it with --void-approved).
    again = await m.tool("get_invoice", invoice_id=invoice_id)
    check(same_guid(again.get("Id"), invoice_id), "get_invoice returned another invoice before the delete")
    status = dig(again, "Status", "Id")
    if status != INVOICE_DRAFT:
        raise CheckFailed(
            f"the test invoice has Status.Id {status!r} before the delete, not {INVOICE_DRAFT} (Draft): nothing was "
            "deleted or voided, it is left for the user"
        )
    number = again.get("Number")
    if not is_id(number):  # delete_invoice finds an invoice by its Number: without one the cleanup's raw DELETE does it
        m.note("the test invoice has no Number, so delete_invoice cannot find it: the cleanup removes it with its raw DELETE")
        return
    gone = await m.delete_draft(number)
    check(same_guid(gone.get("Id"), invoice_id), "delete_invoice answered with another invoice's Id")
    expect("delete_invoice StatusId (Deleted)", gone.get("StatusId"), INVOICE_DELETED)
    await m.invoice_listing(label, invoice_id, listed=False)
    m.mark_cleaned("invoice", invoice_id, "deleted by the write matrix (delete_invoice, Draft)")


async def check_nobody_can_be_emailed(m: Matrix) -> None:
    """Raise Skipped unless nobody could be emailed the Approved invoice: every contact of the test client is Inactive and no
    location of the test client names a billing contact. Two reads, nothing is written, and a failed read fails the area (it
    cannot be said that nobody could be emailed). The reasons name ids only, never a contact."""
    rows: list[Any] = []
    arguments: dict[str, Any] = {"client_id": SITE.test_client, "page_size": 200}
    for _ in range(CONTACT_PAGES):
        page = await m.tool("list_contacts", **arguments)
        found = page.get("items")
        if not isinstance(found, list):
            raise Skipped(
                f"precondition not met: list_contacts did not answer a list, so it cannot be said that every contact of "
                f"client {SITE.test_client} is Inactive; nothing was sent"
            )
        rows.extend(found)
        if page.get("has_more") is not True:
            break
        cursor = page.get("next_cursor")
        if not (isinstance(cursor, str) and cursor):
            raise Skipped(
                f"precondition not met: list_contacts has more contacts of client {SITE.test_client} but no cursor to read "
                "them, so it cannot be said that every one is Inactive; nothing was sent"
            )
        arguments["cursor"] = cursor
    else:
        raise Skipped(
            f"precondition not met: client {SITE.test_client} has more than {CONTACT_PAGES * 200} contacts, too many to "
            "check that every one is Inactive; nothing was sent"
        )
    awake = [row for row in rows if not inactive_contact(row)]
    if awake:
        raise Skipped(
            f"precondition not met: {len(awake)} of the {len(rows)} contacts of client {SITE.test_client} are not Inactive "
            f"(ids {row_ids(awake)}), so an Approved invoice might be emailed to one of them; nothing was sent"
        )
    locations = (await m.tool("list_client_locations", client_id=SITE.test_client)).get("items")
    if not isinstance(locations, list):
        raise Skipped(
            f"precondition not met: list_client_locations did not answer a list, so it cannot be said that no location of "
            f"client {SITE.test_client} names a billing contact; nothing was sent"
        )
    billed = [row for row in locations if not no_billing_contacts(row)]
    if billed:
        raise Skipped(
            f"precondition not met: {len(billed)} of the {len(locations)} locations of client {SITE.test_client} name "
            f"billing contacts or have no readable BillingContactIds (ids {row_ids(billed)}), so an Approved invoice "
            "might be emailed to them; nothing was sent"
        )
    m.note(
        f"nobody could be emailed: all {len(rows)} contacts of client {SITE.test_client} are Inactive and none of its "
        f"{len(locations)} locations names a billing contact"
    )


async def area_approved_invoice(m: Matrix) -> None:
    """ONE Approved invoice of $1 for the test client: created, read, waited for until Gorelo has pushed it to the connected
    accounting system, then voided at once and checked. The only area that calls create_approved_invoice, and it runs
    alone (--with-approved-invoice, --only approved_invoice), behind a guard built with allow_approved_invoice.

    Nothing is sent without the flag, and nothing is written unless the preconditions hold (nobody could be emailed, an
    item to bill). The invoice is recorded the moment it is created. It is never voided before ExternalId says it reached
    the accounting system: an invoice that never syncs fails the area and is left, Approved, for the user."""
    if not m.with_approved_invoice:  # nothing is announced or sent: the tools are not even callable without the flag
        raise Skipped(APPROVED_INVOICE_SKIP_REASON)
    await check_nobody_can_be_emailed(m)
    found = await m.tool("list_items", type="product", status="active", page_size=CATALOG_PAGE)
    item_id = billable_item(found.get("items"))
    if item_id is None:
        raise Skipped(
            f"no active product of nobody or of client {SITE.test_client} among the first {CATALOG_PAGE} catalog items "
            "to put on the invoice"
        )
    label = m.manifest.label("approved invoice")
    # Recorded (status_id, number, display_number) the moment create_approved_invoice answers, before any check; the status
    # is what Gorelo stored, and 5 (never 1) when the answer has none, so a failed read-back cannot make an Approved invoice
    # look like a Draft. An answer without an Id leaves the intent open: the cleanup then searches for the label.
    #
    # The client's 429 retries are off for this call and for nothing else (they are back right after it, also when it raises):
    # the guard allows ONE create, so a retried POST would be refused as a second one. The call includes the tool's read-back
    # GET, which is therefore not retried either: a 429 there is a failed read-back like any other (the invoice exists, is
    # recorded as Approved and is left for the user with the --void-approved command). Every later request, the polls, the
    # lookup by Number and the DELETE that voids, keeps the normal retries.
    with m.without_429_retries():
        made = await m.create(
            "invoice",
            label,
            {"status_id": INVOICE_APPROVED},
            "create_approved_invoice",
            {
                "client_id": SITE.test_client,
                "line_items": [
                    {
                        "item_id": item_id,
                        "quantity": INVOICE_LINE_QUANTITY,
                        "unit_price": INVOICE_LINE_PRICE,
                        "description": label,
                        "no_tax": True,
                    }
                ],
                "reference": label,
                "confirm": True,
            },
            recorded=lambda record: invoice_record(record, INVOICE_APPROVED),
        )
    invoice_id = made["Id"]
    invoice = full(made, "create_approved_invoice")
    name = invoice_name(invoice)
    warned: set[str] = set()

    def watch(record: dict[str, Any], when: str) -> None:
        """Note, once per distinct text, that the invoice reads as emailed (or unreadably): it must not have been."""
        problem = email_problem(record)
        if problem is not None and problem not in warned:
            warned.add(problem)
            m.note(f"{problem}; seen {when}; invoice {invoice_name(record)}")

    watch(invoice, "when it was created")  # first, so that a failing check below cannot hide it
    status = dig(invoice, "Status", "Id")
    if status == INVOICE_PAID:
        raise CheckFailed(
            f"create_approved_invoice Status.Id: expected {INVOICE_APPROVED} (Approved), got {INVOICE_PAID} (Paid): "
            f"Gorelo created invoice {name} as Paid, and it refuses to delete or void a Paid invoice, so it is left "
            "for the user"
        )
    expect("create_approved_invoice Status.Id (Approved)", status, INVOICE_APPROVED)
    expect("create_approved_invoice ClientId", invoice.get("ClientId"), SITE.test_client)
    expect("create_approved_invoice Reference", invoice.get("Reference"), label)
    check_invoice_line(invoice, item_id, label, "create_approved_invoice")
    total = invoice.get("Total")
    check(is_amount(total) and total > 0, f"create_approved_invoice Total is {total!r}, expected an amount above 0")
    m.note(
        f"approved invoice number consumed: Number {invoice.get('Number')!r}, DisplayNumber {invoice.get('DisplayNumber')!r} "
        "(an Approved invoice cannot be removed, only voided, and it stays listed as Void)"
    )

    # Wait for the push to the accounting system: ExternalId is the invoice's id there, null "when not yet synced". A read
    # that fails (a 5xx, a timeout, a 429) is a GET: it wrote nothing and says nothing about the invoice, so it only means
    # "not synced yet" and the next poll asks again. The wait ends when ExternalId is set, on a status other than
    # Approved, or when the polls run out. A guard refusal is no ToolFailed and still stops the run at once.
    got = invoice
    external: str | None = None
    failed, last_failure = 0, ""
    said_failed: set[str] = set()
    for attempt in range(m.sync_polls + 1):
        if attempt:
            await asyncio.sleep(m.sync_interval)
        try:
            read = await m.tool("get_invoice", invoice_id=invoice_id)
        except ToolFailed as exc:
            failed, last_failure = failed + 1, exc.text[:FAILURE_LIMIT]
            # Once per distinct text, so an outage does not fill the summary. The trace id that ends an enveloped error is
            # left out when the texts are compared (it differs per request: every failure would look new), and the note keeps
            # the first full text, trace included.
            same = without_trace(exc.text)
            if same not in said_failed:
                said_failed.add(same)
                then = "so it is asked again at the next poll" if attempt < m.sync_polls else "and no poll is left"
                m.note(
                    f"get_invoice failed at read {attempt + 1} while waiting for the accounting system, {then}: "
                    f"{last_failure}"
                )
            continue
        got = read
        check(same_guid(got.get("Id"), invoice_id), "get_invoice returned another invoice")
        watch(got, f"at read {attempt + 1} while waiting for the accounting system")
        now = dig(got, "Status", "Id")
        if now != INVOICE_APPROVED:
            raise CheckFailed(
                f"approved invoice {invoice_name(got)} has Status.Id {now!r} while waiting for the accounting system, not "
                f"{INVOICE_APPROVED} (Approved): nothing was voided, it is left for the user"
            )
        value = got.get("ExternalId")
        if isinstance(value, str) and value.strip():
            external = value
            break
    if external is None:
        waited = f"{m.sync_polls + 1} reads over {m.sync_polls * m.sync_interval:g} s"
        m.note(
            f"approved invoice {invoice_name(got)}: ExternalId was still null after {waited}, so it was not voided"
            if not failed
            else f"approved invoice {invoice_name(got)}: ExternalId was not seen in {waited}, {failed} of them failed "
            f"(the last: {last_failure}), so it was not voided"
        )
        raise CheckFailed(
            f"approved invoice {invoice_name(got)} not yet synced to accounting; check the accounting system, then void "
            f"it with: python -m scripts.live.cleanup {m.manifest.path} --void-approved ({VOID_COMMAND_NOTE})"
        )
    link = got.get("PaymentLink")
    m.note(
        f"approved invoice {invoice_name(got)}: pushed to the accounting system, ExternalId {external}, PaymentLink "
        + ("set" if isinstance(link, str) and link.strip() else "not set")
    )

    # Void it at once. Gorelo's DELETE of an Approved invoice voids it (StatusId 4) and leaves it listed.
    number = got.get("Number")
    if not is_id(number):  # delete_invoice finds an invoice by its Number: only the cleanup's raw DELETE can void this one
        raise CheckFailed(
            f"approved invoice {invoice_name(got)} has no Number, so delete_invoice cannot find it; void it with: "
            f"python -m scripts.live.cleanup {m.manifest.path} --void-approved ({VOID_COMMAND_NOTE})"
        )
    gone = await m.remove_invoice(number, "Approved")
    check(same_guid(gone.get("Id"), invoice_id), "delete_invoice answered with another invoice's Id")
    expect("delete_invoice StatusId (Void)", gone.get("StatusId"), INVOICE_VOID)
    m.note(VOID_REMINDER)  # as soon as Gorelo said Void: a failing read below must not leave it unsaid (the summary repeats it)
    after = await m.tool("get_invoice", invoice_id=invoice_id)
    check(same_guid(after.get("Id"), invoice_id), "get_invoice returned another invoice after the void")
    watch(after, "after the void")
    # The no-tax check (TaxId null, 1 x 1.0) is a note and nothing more: it comes after the void and never raises, so it can
    # neither stop nor skip the void or the bookkeeping below. A loud note is how an invoice that was taxed is noticed.
    problem = tax_problem(after)
    if problem is None:
        m.note("no-tax check passed: TotalTax is 0 and Total is 1.0 on the read after the void")
    else:
        m.note(
            f"{problem}; read after the void, invoice {invoice_name(after)}; at the last read before the void it was "
            f"TotalTax {amount_text(got.get('TotalTax', _MISSING))} and Total {amount_text(got.get('Total', _MISSING))}"
        )
    expect("get_invoice Status.Id after the void", dig(after, "Status", "Id"), INVOICE_VOID)
    m.mark_voided(invoice_id)  # recorded before the listing check: the void is a fact whatever the search says
    await m.invoice_listing(label, invoice_id, listed=True, voided=True)


async def area_uptime(m: Matrix) -> None:
    location = await m.location()
    label = m.manifest.label("uptime check")
    created = full(
        await m.create(
            "uptime",
            label,
            {"client_id": SITE.test_client},
            "create_uptime_check",
            {
                "check_type": "http",
                "frequency_minutes": 60,
                "region": "seattle",
                "url": SITE.probe_url,
                "client_id": SITE.test_client,
                "location_id": location,
                "description": label,
            },
        ),
        "create_uptime_check",
    )
    check_id = created["Id"]
    expect("uptime check Type.Id (http)", dig(created, "Type", "Id"), 2)
    expect("uptime check Target.Url", dig(created, "Target", "Url"), SITE.probe_url)
    expect("uptime check RegionId (seattle)", created.get("RegionId"), 1)
    expect("uptime check Frequency", created.get("Frequency"), 60)
    reason = f"{label}: maintenance during the live test"
    # Gorelo refuses a window without a start ("MaintenanceMode.StartDateTime is required when enabling maintenance
    # mode."), and the tool never reads the clock: the start is now, written with an explicit UTC offset.
    held = full(
        await m.tool(
            "set_uptime_maintenance",
            check_id=check_id,
            enabled=True,
            start=m.stamp(timedelta(0)),
            duration_minutes=60,
            reason=reason,
        ),
        "set_uptime_maintenance",
    )
    expect("MaintenanceMode.Enabled", dig(held, "MaintenanceMode", "Enabled"), True)
    description = f"{label} (updated)"
    changed = full(await m.tool("update_uptime_check", check_id=check_id, description=description), "update_uptime_check")
    expect("update_uptime_check Description", changed.get("Description"), description)
    m.note(
        f"maintenance window after update_uptime_check: Enabled={dig(changed, 'MaintenanceMode', 'Enabled')!r} "
        f"DurationInMinutes={dig(changed, 'MaintenanceMode', 'DurationInMinutes')!r}"
    )
    gone = await m.tool("delete_uptime_check", check_id=check_id, confirm=True)
    check(same_guid(gone.get("Id"), check_id), "delete_uptime_check answered with another check's Id")
    m.mark_cleaned("uptime", check_id, "deleted by the write matrix (delete_uptime_check)")


async def area_projects(m: Matrix) -> None:
    projects = await m.probe_scope("list_projects", page_size=1)
    forms = await m.probe_scope("list_forms", page_size=1)
    if forms is None:
        m.note("forms scope is available; the matrix has no forms write step")
    else:
        m.note(f"forms: scope missing ({forms})")
    if projects is not None:
        raise Skipped(f"scope missing: {projects}")
    location = await m.location()
    # create_project requires a project type and a group (contract e15cb5a18ec2): the first type the tenant has, and
    # the Everyone group the ticket lookups already resolve by name
    lookups = await m.lookups()
    type_id = first_guid_id((await m.tool("list_project_types"))["items"])
    if type_id is None:
        raise Skipped("the tenant has no project type to create a project with")
    label = m.manifest.label("project")
    project = full(
        await m.create(
            "project",
            label,
            {"client_id": SITE.test_client},
            "create_project",
            {
                "title": label,
                "client_id": SITE.test_client,
                "location_id": location,
                "type_id": type_id,
                "group_id": lookups.everyone,
            },
        ),
        "create_project",
    )
    project_id = project["Id"]
    expect("create_project Title", project.get("Title"), label)

    label = m.manifest.label("section")
    section = await m.create(
        "section",
        label,
        {"project_id": project_id},
        "create_project_section",
        {"project_id": project_id, "title": label},
    )
    label = m.manifest.label("task")
    task = full(
        await m.create(
            "task",
            label,
            {"project_id": project_id},
            "create_project_task",
            {"project_id": project_id, "title": label, "section_id": section["Id"]},
        ),
        "create_project_task",
    )
    task_id = task["Id"]
    check(same_guid(task.get("SectionId"), section["Id"]), "the new task is not in the section it was created in")

    label = m.manifest.label("task comment")
    posted = full(
        await m.create(
            "project_comment",
            label,
            {"project_id": project_id, "task_id": task_id},
            "create_project_comment",
            {
                "project_id": project_id,
                "task_id": task_id,
                "body": f"<p>{label}</p>",
                "conversation_type": "private",
            },
        ),
        "create_project_comment",
    )
    comment_id = posted["Id"]
    gone = await m.tool(
        "delete_project_comment", project_id=project_id, task_id=task_id, comment_id=comment_id, confirm=True
    )
    check(same_guid(gone.get("Id"), comment_id), "delete_project_comment answered with another comment's Id")
    m.mark_cleaned("project_comment", comment_id, "deleted by the write matrix (delete_project_comment)")
    gone = await m.tool("delete_project_task", project_id=project_id, task_id=task_id, confirm=True)
    check(same_guid(gone.get("Id"), task_id), "delete_project_task answered with another task's Id")
    m.mark_cleaned("task", task_id, "deleted by the write matrix (delete_project_task)")


# --------------------------------------------------------------------------
# Selecting and running areas
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Area:
    key: str
    run: Callable[[Matrix], Awaitable[None]]
    sends_email: bool = False


AREAS: tuple[Area, ...] = (
    Area("clients", area_clients),
    Area("contacts", area_contacts),
    Area("tickets", area_tickets),
    Area("comments", area_comments),
    Area("email", area_email, sends_email=True),
    Area("time", area_time),
    Area("items", area_items),
    Area("invoices", area_invoices),
    Area(APPROVED_INVOICE_AREA, area_approved_invoice),
    Area("uptime", area_uptime),
    Area("projects", area_projects),
)
ALIASES = {"forms": "projects"}


def select_areas(only: Sequence[str] | None, skip_email: bool, with_approved_invoice: bool = False) -> list[Area]:
    """The areas to run, always in AREAS order (the comments and time areas build on the tickets area).

    `only` is a list of area names (None: all). Raises UsageError for an unknown name, an empty selection, and for
    with_approved_invoice (--with-approved-invoice) unless the approved_invoice area is the ONLY area selected: that run
    approves a real invoice, so nothing else runs beside it."""
    known = [area.key for area in AREAS]
    wanted: list[str] | None = None
    if only is not None:
        wanted = []
        for raw in only:
            name = raw.strip().lower()
            name = ALIASES.get(name, name)
            if name not in known:
                raise UsageError(f"unknown area {raw!r}; the areas are {', '.join(known)}")
            if name not in wanted:
                wanted.append(name)
    chosen = [area for area in AREAS if wanted is None or area.key in wanted]
    if skip_email:
        chosen = [area for area in chosen if not area.sends_email]
    if not chosen:
        raise UsageError("no area left to run: --skip-email removed the only one selected")
    if with_approved_invoice and [area.key for area in chosen] != [APPROVED_INVOICE_AREA]:
        raise UsageError(
            f"--with-approved-invoice runs the {APPROVED_INVOICE_AREA} area alone: use --only {APPROVED_INVOICE_AREA} "
            "(it approves a real invoice, so no other area runs beside it)"
        )
    return chosen


@dataclass
class AreaResult:
    """status: pass, FAIL, skipped (scope missing, no run ticket, items without --with-items, invoices without
    --with-invoices, approved_invoice without --with-approved-invoice or with a precondition that does not hold, no
    catalog item or project type to use) or not run (the run stopped after a guard refusal)."""

    area: str
    status: str
    detail: str = ""
    notes: list[str] = field(default_factory=list)
    requests: int = 0
    tripped: bool = False


async def _run_area(matrix: Matrix, area: Area) -> AreaResult:
    matrix.notes = []
    requests = matrix.pacer.requests
    refused = len(matrix.guard.violations)
    status, detail, tripped = "pass", "", False
    try:
        await area.run(matrix)
    except Skipped as exc:
        status, detail = "skipped", str(exc)
    except GuardTripped as exc:
        status, detail, tripped = "FAIL", f"the guard refused a request, the run stops: {exc}", True
    except (ToolFailed, CheckFailed) as exc:
        status, detail = "FAIL", str(exc)
    except Exception as exc:
        status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
    if len(matrix.guard.violations) > refused and not tripped:
        status, tripped = "FAIL", True
        detail = f"the guard refused a request, the run stops: {matrix.guard.violations[-1]}"
    return AreaResult(
        area=area.key,
        status=status,
        detail=scrub(" ".join(detail.split()), matrix.secret)[:DETAIL_LIMIT],
        notes=list(matrix.notes),
        requests=matrix.pacer.requests - requests,
        tripped=tripped,
    )


@dataclass
class MatrixReport:
    """What a run did. `ok` is True when no area FAILED, nothing is left over and the guard refused nothing.

    `summary` counts the manifest's records: created, cleaned, leftovers (what needs a human), undeletable (known
    undeletable records, uploaded attachment files and voided invoices, which nobody can remove: neither cleaned nor
    leftovers, never alone a failure) and unresolved_intents.

    `voided` names the approved invoices of the run that were voided (voided_invoices): the summary says for each that the
    void is in Gorelo only (VOID_REMINDER), whatever else the run did."""

    run_id: str
    manifest_path: str
    results: list[AreaResult] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    cleanup: CleanupReport | None = None
    cleanup_error: str | None = None
    scopes_missing: list[str] = field(default_factory=list)
    requests: int = 0
    refused: int = 0
    interrupted: str | None = None
    voided: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.interrupted is None
            and not any(result.status == "FAIL" for result in self.results)
            and self.refused == 0
            and self.cleanup_error is None
            and self.cleanup is not None
            and self.cleanup.ok
        )

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def render(self) -> list[str]:
        width = max([len("area"), *(len(result.area) for result in self.results)]) + 2
        lines = [f"write matrix {self.run_id}: summary", f"{'area':<{width}}{'result':<9}{'requests':<10}detail"]
        for result in self.results:
            lines.append(f"{result.area:<{width}}{result.status:<9}{result.requests:<10}{result.detail}".rstrip())
        notes = [(result.area, note) for result in self.results for note in result.notes]
        if notes:
            lines.append("notes:")
            lines.extend(f"  {area}: {note}" for area, note in notes)
        lines.append(
            f"records: {self.summary.get('created', 0)} created, {self.summary.get('cleaned', 0)} cleaned, "
            f"{self.summary.get('leftovers', 0)} left over, {self.summary.get('undeletable', 0)} known undeletable, "
            f"{self.summary.get('unresolved_intents', 0)} announced without an id"
        )
        for record in self.cleanup.undeletable if self.cleanup is not None else []:
            lines.append(f"known undeletable: {record.kind} {record.id}: {record.outcome}")
        if self.cleanup_error:
            lines.append(f"cleanup could not finish: {self.cleanup_error}")
        lines.append("scopes missing: " + (", ".join(self.scopes_missing) or "none"))
        lines.append(f"requests: {self.requests} sent by the matrix, {self.refused} refused by the guard")
        if self.interrupted:
            lines.append(f"the run was interrupted ({self.interrupted}); the cleanup above ran anyway")
        if not self.ok:
            lines.append(f"if anything is left over: python -m scripts.live.cleanup {self.manifest_path}")
            leftovers = self.cleanup.leftovers if self.cleanup is not None else []
            if any(record.kind == "invoice" and record.details.get("status_id") == INVOICE_APPROVED for record in leftovers):
                lines.append(
                    "an Approved invoice is left over: check the accounting system, then void it with: "
                    f"python -m scripts.live.cleanup {self.manifest_path} --void-approved ({VOID_COMMAND_NOTE})"
                )
        for name in self.voided:  # the last thing said before the result: an invoice voided in Gorelo may still be open in Xero
            lines.append(f"reminder: approved invoice {name}: {VOID_REMINDER}")
        lines.append("result: " + ("PASSED" if self.ok else "FAILED"))
        return lines


def voided_invoices(manifest: Manifest) -> list[str]:
    """The names (DisplayNumber, else Number, else Id) of the invoices of this run that the manifest records as voided.

    An invoice is recorded with status_id 4 (INVOICE_VOID) when the area voided it (mark_voided) and when the cleanup found
    it void (a known residue); only an Approved invoice can be void, so each of these was pushed to the accounting system
    and its void is in Gorelo only (VOID_REMINDER). Read from the manifest, so it holds whichever of the two noticed it."""
    names = []
    for record in manifest.all_created():
        stored = record.details.get("status_id")
        if record.kind != "invoice" or isinstance(stored, bool) or stored != INVOICE_VOID:
            continue
        names.append(
            invoice_name(
                {"DisplayNumber": record.details.get("display_number"), "Number": record.details.get("number"), "Id": record.id}
            )
        )
    return names


async def run_matrix(
    *,
    areas: Sequence[str] | None = None,
    skip_email: bool = False,
    leftovers: bool = False,
    with_items: bool = False,
    with_invoices: bool = False,
    with_approved_invoice: bool = False,
    settings: Settings | None = None,
    transport: Any = None,
    pace: float = PACE_SECONDS,
    directory: str | os.PathLike[str] | None = None,
    started: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    lookup_wait: float = LOOKUP_WAIT,
    sync_interval: float = SYNC_INTERVAL,
    sync_polls: int = SYNC_POLLS,
    echo: Callable[[str], None] = emit,
) -> MatrixReport:
    """Run the selected areas, then ALWAYS the cleanup, and print the summary through `echo`.

    areas        area names for --only (None: every area); --skip-email is skip_email.
    leftovers    also delete the leftovers listed in site.local.toml [leftovers] (run_cleanup(leftovers=True)).
    with_items   run the items area (--with-items). Without it the area is skipped with ITEMS_SKIP_REASON, also when
                 it was selected with areas=["items"], and the three item tools cannot be called.
    with_invoices run the invoices area (--with-invoices). Without it the area is skipped with INVOICES_SKIP_REASON,
                 also when it was selected with areas=["invoices"], and the six invoice tools cannot be called.
    with_approved_invoice run the approved_invoice area (--with-approved-invoice), which approves one $1 invoice on
                 the test client and voids it. It must be the ONLY area selected (areas=["approved_invoice"]) and the only flag
                 (no with_items, with_invoices, leftovers or skip_email), else UsageError before anything is created; the
                 run may call only APPROVED_INVOICE_TOOLS, behind a guard built with allow_approved_invoice. Without it
                 the area is skipped with APPROVED_INVOICE_SKIP_REASON, also when it was selected, and
                 create_approved_invoice cannot be called. The create call alone runs with the 429 retries of the run's
                 Gorelo client switched off (see no_429_retries; the client is looked up before the first request), so a
                 429 on the one create is a plain rejected create. The cleanup that follows never voids (run_cleanup is
                 not given void_approved: that is the user's command, cleanup --void-approved). Whenever an approved
                 invoice of the run was voided, the summary ends with a reminder that the void is in Gorelo only.
    settings     default live_settings() (reads the API key from the app .env); tests pass their own.
    transport    an httpx transport for every client of the run (tests pass httpx.MockTransport).
    pace         seconds between two requests (0 in tests).
    directory    where the manifest goes (default .live-runs in the repository).
    started      the time the run id is made from (default now); clock times backdated records (default now, UTC).
    lookup_wait  seconds between the retries of get_ticket by number or display number.
    sync_interval, sync_polls  how the approved invoice is polled for its ExternalId: every sync_interval seconds (5),
                 sync_polls times after the first read (24, so at most 120 s); tests pass 0 and a small count.
    Raises UsageError (before anything is created) for a bad area selection, EnvError when there is no API key and
    SetupError when the settings leave out a tool the matrix calls (a toolset or the destructive tools).
    """
    site()  # refuse to start, before anything is created, when the site config is missing or incomplete
    selected = select_areas(areas, skip_email, with_approved_invoice)
    if with_approved_invoice:  # the run that approves a real invoice does that and nothing else, so no other flag opens anything
        alongside = [
            flag
            for flag, given in (
                ("--with-items", with_items), ("--with-invoices", with_invoices), ("--leftovers", leftovers),
                ("--skip-email", skip_email),
            )
            if given
        ]
        if alongside:
            raise UsageError(f"--with-approved-invoice runs alone: it cannot be combined with {', '.join(alongside)}")
    chosen = settings if settings is not None else live_settings()
    needed = tools_for(with_items, with_invoices, with_approved_invoice)
    offered = {spec.name for spec in REGISTRY.select(chosen.toolsets, chosen.destructive)}
    if needed - offered:  # a renamed or gated tool would otherwise show up half way through the run
        raise SetupError("the server does not offer the tools the matrix calls: " + ", ".join(sorted(needed - offered)))
    await live_guard.verify_site_clients(chosen.api_key, transport=transport)  # the ids must be the named clients, before any write
    manifest = Manifest.start(started, directory=directory)
    echo(f"write matrix {manifest.run_id}: manifest {manifest.path}")
    echo("areas: " + ", ".join(area.key for area in selected))
    guard = LiveGuard("write", manifest, require_intents=True, allow_approved_invoice=with_approved_invoice)
    pacer = Pacer(pace)
    hooks = {"request": [guard, pacer]}
    results: list[AreaResult] = []
    scopes: set[str] = set()
    interrupted: str | None = None
    cleanup_report: CleanupReport | None = None
    cleanup_error: str | None = None
    try:
        with quiet_logs():
            server = build_server(chosen, transport=transport, event_hooks=hooks)
            async with Client(server) as tools:
                # Before the first request: the one create must never be sent twice, so the client whose retries the area
                # switches off around that call has to be reachable, or the run does not start.
                gorelo = gorelo_client_behind(server) if with_approved_invoice else None
                matrix = Matrix(
                    manifest=manifest,
                    guard=guard,
                    pacer=pacer,
                    session=ToolSession(tools, guard, pacer, chosen.api_key, allowed=needed),
                    secret=chosen.api_key,
                    clock=clock if clock is not None else (lambda: datetime.now(timezone.utc)),
                    lookup_wait=lookup_wait,
                    scopes_missing=scopes,
                    with_items=with_items,
                    with_invoices=with_invoices,
                    with_approved_invoice=with_approved_invoice,
                    sync_interval=sync_interval,
                    sync_polls=sync_polls,
                    gorelo=gorelo,
                )
                stopped = False
                for area in selected:
                    if stopped:
                        results.append(AreaResult(area.key, "not run", "the run stopped after a guard refusal"))
                        continue
                    result = await _run_area(matrix, area)
                    results.append(result)
                    stopped = result.tripped
                    progress = f"area {result.area}: {result.status}, {result.requests} requests"
                    echo(scrub(progress + (f" ({result.detail})" if result.detail else ""), chosen.api_key))
    except BaseException as exc:
        interrupted = type(exc).__name__
        raise
    finally:
        try:
            cleanup_report = await run_cleanup(
                manifest, leftovers=leftovers, settings=chosen, transport=transport, pace=pace, echo=echo,
                verify_clients=False,  # checked above, before the first write
            )
        except Exception as exc:
            cleanup_error = scrub(" ".join(f"{type(exc).__name__}: {exc}".split()), chosen.api_key)[:DETAIL_LIMIT]
        counts = manifest.summary()
        undeletable = len(cleanup_report.undeletable) if cleanup_report is not None else 0
        counts["leftovers"] -= undeletable  # known undeletable files are not records anybody can still clean up
        counts["undeletable"] = undeletable
        report = MatrixReport(
            run_id=manifest.run_id,
            manifest_path=str(manifest.path),
            results=results,
            summary=counts,
            cleanup=cleanup_report,
            cleanup_error=cleanup_error,
            scopes_missing=sorted(scopes),
            requests=pacer.requests,
            refused=len(guard.violations),
            interrupted=interrupted,
            voided=voided_invoices(manifest),  # after the cleanup: it may have found an invoice void too
        )
        for line in report.render():
            echo(scrub(line, chosen.api_key))
    return report


def parse_only(text: str | None) -> list[str] | None:
    return None if text is None else text.split(",")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.live.write_matrix",
        description="Run the live write matrix against the test client and the second client, then clean up what it created.",
    )
    parser.add_argument("--skip-email", action="store_true", help="leave out the email area (the second client, the operator contact)")
    parser.add_argument(
        "--only",
        metavar="AREA,...",
        help="run only these areas, in the matrix order: " + ", ".join(area.key for area in AREAS),
    )
    parser.add_argument(
        "--leftovers",
        action="store_true",
        help="also delete the leftovers listed in site.local.toml [leftovers] (the leftover clients and contact) after checking their names",
    )
    parser.add_argument(
        "--with-items",
        action="store_true",
        help="also run the items area (creates, updates and deletes a catalog product on the test client); without it the "
        "area is skipped, also under --only items",
    )
    parser.add_argument(
        "--with-invoices",
        action="store_true",
        help="also run the invoices area (creates and deletes a Draft invoice on the test client, which uses up one invoice "
        "number); without it the area is skipped, also under --only invoices",
    )
    parser.add_argument(
        "--with-approved-invoice",
        action="store_true",
        help="run the approved_invoice area, ALONE (needs --only approved_invoice and no other flag): it approves a "
        "$1 invoice on the test client, which pushes it to the connected accounting system, then voids it (a voided invoice "
        "stays listed as Void); without the flag the area is skipped, also under --only approved_invoice",
    )
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(
            run_matrix(
                areas=parse_only(args.only),
                skip_email=args.skip_email,
                leftovers=args.leftovers,
                with_items=args.with_items,
                with_invoices=args.with_invoices,
                with_approved_invoice=args.with_approved_invoice,
            )
        )
    except UsageError as exc:
        print(f"usage: {exc}", file=sys.stderr)
        return 2
    except (EnvError, SetupError, SiteConfigError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # the cleanup and the summary already ran, in run_matrix's finally
        print("interrupted", file=sys.stderr)
        return 130
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
