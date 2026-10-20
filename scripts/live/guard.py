"""The request guard of the live harness: refuses, BEFORE anything is sent, every request outside the allowlist.

LiveGuard is an httpx request event hook (`await guard(request)`). It is passed to the server the harness
builds (build_server(..., event_hooks={"request": [guard]})) and to every raw httpx client the harness uses,
so no request leaves the process unchecked. A request the allowlist does not name is refused with a
GuardViolation (a RuntimeError, so no tool wrapper turns it into a friendlier error). Every refusal is also
kept in `guard.violations`, so a harness can stop at once even when a tool call swallowed the exception:
`guard.assert_clean()` raises if anything was refused. Messages say what was blocked and why; they never
hold the API key (no header value is ever printed) or a customer's email address (a masked one is shown).

Modes:  "read"   only GET (and never the invoice PDF).
        "write"  GET plus the writes allowed below. `cleanup=True` additionally lets the leftovers listed in
                 site.local.toml [leftovers] be deleted (the leftover clients and contact), and nothing else.
                 `allow_approved_invoice=True` (off by default; only write_matrix --with-approved-invoice and
                 cleanup --void-approved set it) opens the two invoice rules named below, and nothing else.
                 `allow_custom_assets=True` (off by default; only write_matrix --with-custom-assets sets it) opens
                 the two custom asset rules named below, and nothing else.
                 `allow_custom_properties=True` (off by default; only write_matrix --with-custom-properties sets it, and
                 a cleanup of a manifest that holds a custom_property record) opens the one PATCH of the test client
                 named below ("The custom properties"), and nothing else.
                 `allow_custom_asset_move=True` (off by default; only write_matrix --with-asset-move sets it, and it needs
                 allow_custom_assets) lets the PATCH of a custom asset name the second client, which MOVES the asset
                 (see PATCH /assets/custom/{customAssetId} below), and nothing else.
                 `allow_documents=True` (off by default; only write_matrix --with-documents sets it, never a cleanup) opens
                 the document writes named below ("The documents"), and nothing else.

How a request is read: method; URL (https, the Gorelo API host, no credentials, base path /v1 stripped);
every path segment is checked (no empty, dot, percent-encoded or odd segment), then the request is matched
to an operation of spec/spec_index.json. A path that matches no operation is unknown and is blocked. JSON
bodies must be one object without duplicate keys (case-insensitive, because the server binds names
case-insensitively) and only fields the spec defines. Multipart bodies are parsed into fields and files.
Anything the guard cannot parse or inspect is blocked.

Allowlist (write mode). "Run-created" means the id is in the manifest of this run:

    GET        every GET operation of the spec except GET /invoices/{invoiceId}/pdf (it records an export event on the
               invoice): that one is never allowed in read mode and in write mode only for a run-created Draft invoice
    DELETE     never under /assets/ (deleting an agent uninstalls the RMM agent), never contracts,
               never a client that is the test client or the second client, and never the operator contact (even if a manifest wrongly lists them);
               otherwise only a run-created record of the matching kind (parents in the path too); with
               cleanup=True also the listed leftovers (the leftover clients and contact); an invoice only when
               it is run-created AND its manifest details say status_id 1, or 5 with allow_approved_invoice (see
               DELETE /invoices/{invoiceId})
    POST /clients                 Name starts with MCPTEST-; Domain, if present, is example.invalid (a real domain
                                  would route that company's inbound email to the temporary client)
    PATCH /clients/{clientId}     the path id is a run-created client; never the second client; the test client only with
                                  allow_custom_properties and only a body of CustomProperties and nothing else (see "The custom
                                  properties" below; no other field of the test client is ever changed); CustomProperties never go to
                                  a run-created client; the body has no Id (the spec defines none, so it is refused as an unknown field)
    POST /contacts                ClientId is the test client
    PATCH /contacts/{contactId}   the path id is a run-created contact; the body has no ContactId (refused as an unknown
                                  field); ClientId, if present, is the test client
    POST /tickets                 ClientId in {test client, second client}; ContactId and CcContactIds only the operator
                                  contact or run-created contacts; SendTicketCreatedEmail true only when ContactId
                                  is the operator contact (and the CCs are only the operator contact)
    PATCH /tickets/{ticketId}     run-created ticket; ClientId, if present, in {test client, second client}; contacts as above
    POST /tickets/{ticketId}/comments   run-created ticket; ConversationTypeId 1 (public) only when the ticket's contact
                                  and CCs are absent or the operator contact (from the manifest details contact_id and
                                  cc_contact_ids, or ContactId and CcContactIds, plus any contact a PATCH through
                                  this guard set); 2 (private) allowed; 3 (side conversation) and 4 (approval) only
                                  with a ConversationId that names a side conversation (3) or an approval (4) THIS
                                  RUN created whose manifest details ticket_id is the ticket of the path; types 1
                                  and 2 carry no ConversationId
    POST /tickets/{ticketId}/conversations/side-conversation   run-created ticket; Email and CcEmails allowed addresses
    POST /tickets/{ticketId}/conversations/approval            run-created ticket; ContactIds operator or run-created
    POST /attachments             multipart; itemType Ticket, Task or Project and itemId a run-created record of it; itemType
                                  Document only with allow_documents (never from a cleanup), itemId a run-created document
    POST /time-entries            TicketId (or TaskId) run-created; UserId is the operator user
    PATCH, DELETE /time-entries/{timeEntryId}, /items/{itemId}, /uptime/{checkId}   run-created record
    POST /items, /uptime, /projects    ClientId is the test client
    POST, PATCH /uptime           Target.Url, if present, is a http(s) URL on the probe domain or a subdomain; Target.Ip
                                  is not allowed (a check probes its target every minute)
    PATCH /items, /uptime, /projects   ClientId, if present, is the test client
    /projects/{projectId}/...     run-created project, and the run-created section, task or comment of the path;
                                  project comments only Private (no ConversationId); task comments Private, or a
                                  side conversation or approval with a ConversationId that this run created whose
                                  manifest details task_id is the task of the path
    POST /forms/{formId}/submission-links   TicketId or TaskId run-created
    POST /alerts                  never
    POST /api-keys                never (an API key is a credential: the harness never mints one)
    GET, DELETE /api-keys         never (an API key is a credential: the harness never lists or revokes one; this also
                                  holds in read mode, where GET /api-keys is the one GET that is refused besides the PDF)
    POST /assets/custom           only with allow_custom_assets=True (write_matrix --with-custom-assets) and never from
                                  a cleanup; ClientId is the test client; Name starts with the run id (MCPTEST-...)
    PATCH /assets/custom/{customAssetId}  only with allow_custom_assets=True (never from a cleanup); the path id is a
                                  custom asset this run created; ClientId is optional (the tool sends it only to name the
                                  asset's client or to move it): absent, or the test client, or, only with
                                  allow_custom_asset_move, the second client = a MOVE of the asset, which also needs a
                                  LocationId in the same body, no ContactId but null, and a manifest record of the asset whose
                                  details client_id already names the second client (the area announces the move first); any
                                  other client is refused; Name, if present, starts with the run id
    GET /assets/custom/hardware-types  a plain read
    GET /assets/custom/{customAssetId}  a plain read (contract a7954b24844b; the read-back of both custom asset writes)
    GET /deletions                a plain read (the feed of deleted records, list_deletions)
    DELETE /assets/custom/{customAssetId}  never: this harness never deletes a custom asset (the spec's soft DELETE is not used)
    POST /invoices                ClientId is the test client (a JSON integer); StatusId is present and exactly the JSON integer 1
                                  (a Draft: any other value, 5 (Approved, which pushes the invoice to the accounting
                                  system), "1", 1.0, true or no StatusId at all is refused); RecipientEmails absent,
                                  null or empty (the harness never emails an invoice, not even to the operator);
                                  LineItems a non-empty list of objects, each ItemId a UUID string. Only with
                                  allow_approved_invoice=True is StatusId exactly the JSON integer 5 allowed too, under
                                  the rules of "The approved invoice" below; without the option every refusal above
                                  stays exactly as it is
    GET /invoices/{invoiceId}/pdf   write mode only, and only a run-created invoice whose manifest details hold
                                  status_id 1, like DELETE below (never in read mode: the download is recorded on the
                                  invoice as an export event; the option does not open it: Draft only)
    DELETE /invoices/{invoiceId}  a run-created invoice whose manifest details hold status_id 1 (it was created as a
                                  Draft). Gorelo's DELETE: Draft: deleted (no longer listed). Approved: voided (status
                                  Void, still listed). The harness voids only with allow_approved_invoice=True, and then
                                  only an invoice recorded with status_id 5; any other invoice is refused (Paid 3, Void
                                  4 and an invoice with no recorded status are never deletable)
    POST /documents, PATCH /documents/{documentId}, POST /documents/{documentId}/duplicate, POST and DELETE under
    /documents/{documentId}/related-items, POST /documents/{documentId}/versions/{versionId}/restore
                                  the document writes, only with allow_documents, never from a cleanup (except the one archive
                                  described under "The documents" below); the GETs of a document, its PDF, its versions and its
                                  related items are plain reads
    everything else               blocked

Rules that apply to every JSON body: every string that looks like an email address must be an allowed
address (the operator address) or end with @example.invalid; contact fields (ContactId, CcContactIds,
ContactIds, SharedWithContactIds) hold only the operator contact or run-created contacts; user fields
(LeadAssigneeId, AssistingAssigneeIds, WatcherIds, UserId) hold only the operator user; AgentAssetIds and CustomAssetIds must be empty; UptimeIds, BlockedByTaskIds, BlockingTaskIds and
SectionId may name only run-created records; AdoptClientAssets must be absent or false. Id fields must be real
JSON integers or canonical UUID strings (no "9001", no 9001.0, no true, where 9001 stands for any id). An invoice body is a JSON body like any
other: these rules apply to it on top of the invoice rules above.

The invoices: the guard sees a create request but not its answer, so it cannot learn a new invoice's id or status.
The harness records `status_id`, `number` and `display_number` in the details of the invoice (kind "invoice") right
after the create answered. `status_id` is the status Gorelo stored (1 for every Draft create the POST rule allows, and 1
when the answer has none; for the approved invoice 5, and 5 when the answer has none, never 1), or, for an invoice found
by the cleanup's label search, the status the search found (any of 1, 3, 4 and 5, or None when the row has no readable
one), or 4 once the harness voided it. The PDF rule refuses any invoice whose details do not hold the integer 1, and the
DELETE rule any invoice whose details do not hold the integer 1 (or the integer 5 with allow_approved_invoice). A
Draft that somebody else approved afterwards is caught by the harness, not by the guard: the write matrix and the
cleanup read the invoice back and send a DELETE only for a Draft (or, with the option, for an invoice recorded as
Approved that still reads as Approved).

The approved invoice (allow_approved_invoice=True, set by write_matrix --with-approved-invoice, and for the void alone by
cleanup --void-approved): the one Approved invoice a run may create. Gorelo pushes it to the connected accounting system
at once and it cannot be removed afterwards (a void stays listed as Void). POST /invoices with StatusId exactly the JSON
integer 5 is allowed only when ALL of these hold: ClientId is the test client; the body has no RecipientEmails key at all (not even
null or empty); Reference is the label of an open manifest intent of kind "invoice" whose details hold status_id 5, and
that is the only such open intent; LineItems holds exactly one line, whose Quantity is above 0 and whose UnitPrice is given
explicitly and is above 0 (a line that falls back to the item's own price could total 0, and an Approved invoice whose
total is exactly 0 is created as Paid, which Gorelo neither deletes nor voids), with no DiscountPercent key and
BillableStatusId absent or exactly 1, and with a TaxId that is present and exactly null (the line's no_tax: Gorelo falls
back to the item's own tax when TaxId is omitted and a number picks a tax, but the harness limits the one Approved invoice to $1 with no tax);
that line bills no more than APPROVED_INVOICE_LIMIT (1.0, the $1 the harness allows) before tax; and no other approved
invoice was created in this run (this guard allowed none before, and the manifest holds no invoice record whose
status_id is not 1). A guard built with cleanup=True never allows the create: a cleanup creates nothing.
DELETE /invoices/{invoiceId} of a run-created invoice recorded with status_id 5 is allowed only with the option (it
voids the invoice: StatusId 4, still listed); an invoice recorded as Paid (3), Void (4) or with no status is never
deletable, and the PDF export stays Draft-only. Without the option a StatusId of 5 and a DELETE of an invoice recorded
as 5 are refused exactly as before.

The custom properties (allow_custom_properties=True, set by write_matrix --with-custom-properties, and by a cleanup whose
manifest holds a custom_property record): the one write the live tests make to the test client itself. PATCH
/clients/{clientId} of the test client is allowed only when ALL of these hold: the body is CustomProperties and nothing else
(no Name, StatusId, BillingName or AlternateName), a list with exactly ONE entry, an object with Id and Value and nothing
else; Id is a JSON integer that names a custom field the manifest records as a custom_property record of the test client
whose details say was_set is exactly false (the write matrix writes that record BEFORE its first write, so a field the run did
not record can never be written: this holds for CUSTOM_PROPERTY_UNKNOWN_ID, the Id of the area's negative check, too, which
update_client must refuse locally after its read of the client, so that no PATCH carrying it is ever sent); Value is null (a
clear: how Gorelo removes a saved value) or text that starts with the run id, never an empty string, and for a recorded field
exactly the text its record holds; once that record is cleaned (the area marks it cleaned after the clear), a guard that is not a
cleanup guard takes NO further write to the field, a null included. A cleanup guard (cleanup=True) allows the null only. The
second client is never changed, and a run-created client never takes CustomProperties. The granularity is the run, not the area: the option is on for the whole of a
--with-custom-properties run, but the manifest record is what unlocks a field, and only the custom_properties area writes one.
`writes_allowed` counts the requests the guard let through that are not a GET, so a harness can tell, by comparing it before and
after a call, that the call sent no write (the negative check does).

The documents (allow_documents=True, set only by write_matrix --with-documents, never by a cleanup): a document cannot be
deleted through the API, so every document a run makes stays in Gorelo (the run archives it, StatusId 2). Every write is for a
document THIS RUN created (kind "document" in the manifest) and for the test client alone, and nothing here ever turns sharing on:
    POST /documents                       Title starts with the run id; exactly one place: ClientId the test client (a JSON
                                          integer, a top-level document) or ParentDocumentId a run-created document (then no
                                          ClientId: the document takes the parent's client); an INTERNAL document (neither) is
                                          refused; Body embeds no data: image; with require_intents it needs an open "document" intent
    PATCH /documents/{documentId}         the keys Title (starts with the run id), Body (no data: image), AlternateName (empty
                                          or starts with the run id), IsStarred (a boolean), StatusId (exactly 1 or 2),
                                          Attachments (a list of {Name, Url}: every Name a file this run uploaded to THAT
                                          document, recorded in the manifest as an attachment with details document_id; [] removes
                                          them all), Move ({ParentDocumentId: a run-created document other than the document
                                          itself, no ClientId} or {ParentDocumentId: null, ClientId: the test client}; an
                                          internal move is refused) and PublicLinkEnabled and PublishToPortalEnabled, which may only
                                          be false: TRUE IS NEVER ALLOWED (a public link or the client portal makes the content
                                          readable outside the MSP; share_document is not a tool of the matrix)
    POST /documents/{id}/duplicate        a run-created source; Title, if present, starts with the run id; the body may be left out
                                          entirely (the spec allows it); with require_intents it needs an open "document" intent
    POST /documents/{id}/related-items    RelatedItems of 1 to 100 entries, each ItemTypeId exactly 1 (a Document) and an ItemId
                                          that is a run-created document: no asset, device, contact or password is ever linked
    DELETE /documents/{id}/related-items/{relatedItemId}   a run-created document, the link id a whole number
    POST /documents/{id}/versions/{versionId}/restore      a run-created document, the version id a UUID
A CLEANUP guard (cleanup=True) refuses all of these but one: PATCH /documents/{documentId} of a run-created document with the body
{"StatusId": 2} and nothing else (the archive of what a stopped run left; allow_documents is not needed for it).

The tickets' contacts: the guard sees a create request but not its answer, so it cannot learn a new ticket's
id or who is stored on it. The harness records `contact_id` (int or None) and `cc_contact_ids` in the details of
the ticket: the write matrix writes the STORED values it read back (manifest.update_details), not the ones it
asked for. A ticket without `contact_id` in its details is treated as unknown and gets no public comment.

The conversations: a side conversation or approval has an id the guard cannot see in a request either, so the
harness records each one with the parent it belongs to (manifest.created("side_conversation" or "approval", id,
label, {"ticket_id": ...}), and {"project_id": ..., "task_id": ...} for a task). A comment of type 3 or 4 goes
only into a conversation recorded that way, of the right kind, on the very ticket (task) of the path.

require_intents=True (opt-in) makes the manifest discipline mechanical: a POST that creates a record is refused unless the
manifest holds an open intent of the kind that goes with it (the kinds are the values of _CREATES), so a create the harness
forgot to announce can never become an orphan. It checks that an intent is open, it does not count requests. The POSTs the
guard lets through, with the intent kind each one needs (a POST with no kind needs none: it creates no record the harness
has to find again):
    POST /clients                                                             client
    POST /contacts                                                            contact
    POST /tickets                                                             ticket
    POST /tickets/{ticketId}/comments                                         comment
    POST /tickets/{ticketId}/conversations/side-conversation                  side_conversation
    POST /tickets/{ticketId}/conversations/approval                           approval
    POST /attachments                                                         attachment
    POST /time-entries                                                        time_entry
    POST /assets/custom                                                       custom_asset
    POST /items                                                               item
    POST /uptime                                                              uptime
    POST /invoices                                                            invoice
    POST /projects                                                            project
    POST /projects/{projectId}/sections                                       section
    POST /projects/{projectId}/tasks                                          task
    POST /projects/{projectId}/comments                                       project_comment
    POST /projects/{projectId}/tasks/{taskId}/comments                        project_comment
    POST /projects/{projectId}/tasks/{taskId}/conversations/approval          approval
    POST /projects/{projectId}/tasks/{taskId}/conversations/side-conversation side_conversation
    POST /documents                                                           document
    POST /documents/{documentId}/duplicate                                    document
    POST /forms/{formId}/submission-links
    POST /documents/{documentId}/related-items
    POST /documents/{documentId}/versions/{versionId}/restore
The options that open some of these (allow_custom_assets, allow_approved_invoice, allow_documents) and the rules of a
cleanup guard are in the allowlist above.
"""

from __future__ import annotations

import html
import json
import math
import re
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

import httpx

from gorelo_client import GORELO_BASE_URL, SIDE_EFFECT_GETS, normalize_op_key
from scripts.live.manifest import ID_TYPES, Manifest, normalize_id
from scripts.site_config import SiteConfig, SiteConfigError, site
from settings import SettingsError, parse_base_url
from spec import SpecIndex, load_spec_index

TEST_EMAIL_DOMAIN = "example.invalid"
TEMP_CLIENT_PREFIX = "MCPTEST-"
# The test clients, the operator contact, user and email, the listed leftovers and the probe domain come from the local
# site config (scripts/site_config.py, site.local.toml); the guard refuses to be built without it.
API_HOSTS = frozenset({"api.usw.gorelo.io"})
API_BASE_PATH = "/v1"


def _checked_base_url(base_url: str) -> str:
    """`base_url` as settings.parse_base_url normalizes it (https://<host>/v1). ValueError, naming GORELO_BASE_URL, when it is blank
    or refused: the guard must never be built for a host outside gorelo.io."""
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url must be the Gorelo base URL of the run (Settings.base_url), not blank")
    try:
        return parse_base_url(base_url)
    except SettingsError as exc:
        raise ValueError(str(exc)) from None


def api_hosts(base_url: str) -> frozenset[str]:
    """The one host a run may call: the host of `base_url` (the Settings.base_url of the run)."""
    return frozenset({str(urlsplit(_checked_base_url(base_url)).hostname)})


CONVERSATION_PUBLIC, CONVERSATION_PRIVATE, CONVERSATION_SIDE, CONVERSATION_APPROVAL = 1, 2, 3, 4
INVOICE_DRAFT, INVOICE_APPROVED = 1, 5  # Invoice StatusId: the harness creates (and deletes) Drafts; with
# allow_approved_invoice it also creates one Approved invoice and voids it (see "The approved invoice" above)
APPROVED_INVOICE_LIMIT = 1.0  # the most the one Approved invoice's line may bill before tax: the $1 the harness allows (the
# TaxId rule of the approved invoice keeps the tax at none, so before tax is also the total)
# The Id the negative check of the custom_properties area names: no custom field of the service provider has it. The guard accepts
# it nowhere (it is no recorded field, like any other Id the run did not record): update_client must refuse it locally, after its
# read of the client, so no PATCH that carries it is ever sent.
CUSTOM_PROPERTY_UNKNOWN_ID = 9_000_000_000_000
DOCUMENT_ACTIVE, DOCUMENT_ARCHIVED = 1, 2  # Document StatusId: the run archives what it creates, and restores one document
CLIENT_ACTIVE = 1  # Client StatusId 1 Active (3 Credit hold, 4 Archived): the only status a write run accepts for the test client
# An image embedded in a document body as a data: URL (Gorelo refuses it; the guard repeats the refusal so the rule table is true).
_DATA_IMAGE = re.compile(r"data\s*:\s*image\s*/", re.IGNORECASE)
_DIGITS_ONLY = re.compile(r"[0-9]{1,18}")

_METHODS = ("GET", "POST", "PATCH", "DELETE")
_METHOD_OVERRIDE_HEADERS = ("x-http-method-override", "x-http-method", "x-method-override")
_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")
_UNSAFE_URL = re.compile(r"[\\@\s]")
_PLAIN_NAME = re.compile(r"[A-Za-z0-9_]{1,40}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_EMAIL_SPLIT = re.compile(r"[^\w.%+@-]+")
_LOCAL_PART = re.compile(r"[a-z0-9._%+-]+")
_MAX_DEPTH = 64
# The details of a ticket record that name its contact and CCs (the harness may spell them either way).
_CONTACT_DETAIL_KEYS = ("contact_id", "ContactId")
_CC_DETAIL_KEYS = ("cc_contact_ids", "CcContactIds")

_KIND_NAMES = {
    "client": "client",
    "contact": "contact",
    "ticket": "ticket",
    "comment": "ticket comment",
    "time_entry": "time entry",
    "item": "item",
    "uptime": "uptime check",
    "project": "project",
    "section": "project section",
    "task": "project task",
    "project_comment": "project or task comment",
    "invoice": "test invoice",
    "custom_asset": "custom asset",
    "custom_property": "custom property",
    "document": "document",
}

# The manifest kind a comment's ConversationTypeId 3 or 4 may post into, with the words for it: (one, many).
_CONVERSATION_KINDS = {3: "side_conversation", 4: "approval"}
_CONVERSATION_WORDS = {"side_conversation": ("a side conversation", "side conversations"), "approval": ("an approval", "approvals")}

# Operations that are never allowed, with the reason that goes into the message.
_NEVER: dict[str, str] = {
    "POST /v1/alerts": "the live harness never posts alerts (an alert can open tickets or notify technicians "
    "and cannot be deleted)",
    "POST /v1/api-keys": "the live harness never creates API keys (an API key is a credential)",
    "GET /v1/api-keys": "the live harness never lists API keys (an API key is a credential)",
    "DELETE /v1/api-keys/{apiKeyId}": "the live harness never revokes API keys (an API key is a credential)",
    "DELETE /v1/assets/agents/{deviceId}": "deleting an agent asset UNINSTALLS the RMM agent from the host",
    "DELETE /v1/assets/custom/{customAssetId}": "this harness never deletes a custom asset (the spec's soft DELETE is not used)",
    "DELETE /v1/contracts/{contractId}": "the live harness never deletes contracts",
    "POST /v1/payments": "the live harness never records payments (a payment is accounting data that reaches the connected system)",
    "DELETE /v1/payments/{paymentId}": "the live harness never deletes payments (a payment is accounting data)",
}

# _NEVER is looked up by the SHAPE of the operation (placeholders written {}), so renaming a placeholder in a
# future spec cannot lose the reason a never-allowed operation is refused with. (An operation that is missing from
# the allowlist is blocked anyway; this keeps the message right.)
_NEVER_BY_SHAPE: dict[str, str] = {normalize_op_key(key): reason for key, reason in _NEVER.items()}


def _never_reason(op_key: str) -> str | None:
    """Why the live harness never sends this operation, or None when it is not one of the never-allowed ones."""
    return _NEVER_BY_SHAPE.get(normalize_op_key(op_key))


# The GETs that are not pure reads (the invoice PDF is logged on the invoice as an export event), compared by SHAPE
# like FORBIDDEN_OPS and _NEVER: a renamed placeholder must not turn the download back into a plain read.
_SIDE_EFFECT_SHAPES: frozenset[str] = frozenset(normalize_op_key(key) for key in SIDE_EFFECT_GETS)


def _is_side_effect_get(op_key: str) -> bool:
    """True when `op_key` is one of gorelo_client.SIDE_EFFECT_GETS, whatever its placeholder is called."""
    return normalize_op_key(op_key) in _SIDE_EFFECT_SHAPES


def _is_custom_asset_write(method: str, segments: tuple[str, ...]) -> bool:
    """True for the only two writes under /assets/ that are not refused outright: POST /assets/custom and PATCH
    /assets/custom/{id} (the rules of LiveGuard decide whether the run may send them). DELETE under /assets/ and every
    write to agents stay refused before any rule is read."""
    lowered = tuple(segment.lower() for segment in segments)
    if lowered[:2] != ("assets", "custom"):
        return False
    return (method == "POST" and len(lowered) == 2) or (method == "PATCH" and len(lowered) == 3)


# Writes whose path ids must ALL name records this run created: op key -> ((kind, placeholder), ...).
_TICKET = (("ticket", "ticketId"),)
_PROJECT = (("project", "projectId"),)
_PROJECT_TASK = (*_PROJECT, ("task", "taskId"))
_DOCUMENT = (("document", "documentId"),)
_PATH_KINDS: dict[str, tuple[tuple[str, str], ...]] = {
    "PATCH /v1/contacts/{contactId}": (("contact", "contactId"),),
    "PATCH /v1/tickets/{ticketId}": _TICKET,
    "DELETE /v1/tickets/{ticketId}": _TICKET,
    "POST /v1/tickets/{ticketId}/comments": _TICKET,
    "DELETE /v1/tickets/{ticketId}/comments/{commentId}": (*_TICKET, ("comment", "commentId")),
    "POST /v1/tickets/{ticketId}/conversations/side-conversation": _TICKET,
    "POST /v1/tickets/{ticketId}/conversations/approval": _TICKET,
    "PATCH /v1/time-entries/{timeEntryId}": (("time_entry", "timeEntryId"),),
    "DELETE /v1/time-entries/{timeEntryId}": (("time_entry", "timeEntryId"),),
    "PATCH /v1/assets/custom/{customAssetId}": (("custom_asset", "customAssetId"),),
    "PATCH /v1/items/{itemId}": (("item", "itemId"),),
    "DELETE /v1/items/{itemId}": (("item", "itemId"),),
    "PATCH /v1/uptime/{checkId}": (("uptime", "checkId"),),
    "DELETE /v1/uptime/{checkId}": (("uptime", "checkId"),),
    "DELETE /v1/invoices/{invoiceId}": (("invoice", "invoiceId"),),
    "PATCH /v1/projects/{projectId}": _PROJECT,
    "DELETE /v1/projects/{projectId}": _PROJECT,
    "POST /v1/projects/{projectId}/sections": _PROJECT,
    "PATCH /v1/projects/{projectId}/sections/{sectionId}": (*_PROJECT, ("section", "sectionId")),
    "DELETE /v1/projects/{projectId}/sections/{sectionId}": (*_PROJECT, ("section", "sectionId")),
    "POST /v1/projects/{projectId}/tasks": _PROJECT,
    "PATCH /v1/projects/{projectId}/tasks/{taskId}": _PROJECT_TASK,
    "DELETE /v1/projects/{projectId}/tasks/{taskId}": _PROJECT_TASK,
    "POST /v1/projects/{projectId}/comments": _PROJECT,
    "DELETE /v1/projects/{projectId}/comments/{commentId}": (*_PROJECT, ("project_comment", "commentId")),
    "POST /v1/projects/{projectId}/tasks/{taskId}/comments": _PROJECT_TASK,
    "DELETE /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}": (
        *_PROJECT_TASK,
        ("project_comment", "commentId"),
    ),
    "POST /v1/projects/{projectId}/tasks/{taskId}/conversations/approval": _PROJECT_TASK,
    "POST /v1/projects/{projectId}/tasks/{taskId}/conversations/side-conversation": _PROJECT_TASK,
    "PATCH /v1/documents/{documentId}": _DOCUMENT,
    "POST /v1/documents/{documentId}/duplicate": _DOCUMENT,
    "POST /v1/documents/{documentId}/related-items": _DOCUMENT,
    "DELETE /v1/documents/{documentId}/related-items/{relatedItemId}": _DOCUMENT,
    "POST /v1/documents/{documentId}/versions/{versionId}/restore": _DOCUMENT,
}

# The POST operations whose body the spec lets the caller leave out entirely (the tool sends none): an empty body is read as {}.
_OPTIONAL_BODY: dict[str, str] = {
    "POST /v1/documents/{documentId}/duplicate": "the spec says the request body may be omitted entirely (the copy then gets the default title)",
}

# The POST operations that create a record, and the manifest kind of what they create (for require_intents).
_CREATES: dict[str, str] = {
    "POST /v1/clients": "client",
    "POST /v1/contacts": "contact",
    "POST /v1/tickets": "ticket",
    "POST /v1/tickets/{ticketId}/comments": "comment",
    "POST /v1/tickets/{ticketId}/conversations/side-conversation": "side_conversation",
    "POST /v1/tickets/{ticketId}/conversations/approval": "approval",
    "POST /v1/attachments": "attachment",
    "POST /v1/time-entries": "time_entry",
    "POST /v1/assets/custom": "custom_asset",
    "POST /v1/items": "item",
    "POST /v1/uptime": "uptime",
    "POST /v1/invoices": "invoice",
    "POST /v1/projects": "project",
    "POST /v1/projects/{projectId}/sections": "section",
    "POST /v1/projects/{projectId}/tasks": "task",
    "POST /v1/projects/{projectId}/comments": "project_comment",
    "POST /v1/projects/{projectId}/tasks/{taskId}/comments": "project_comment",
    "POST /v1/projects/{projectId}/tasks/{taskId}/conversations/approval": "approval",
    "POST /v1/projects/{projectId}/tasks/{taskId}/conversations/side-conversation": "side_conversation",
    "POST /v1/documents": "document",
    "POST /v1/documents/{documentId}/duplicate": "document",
}
# The POST operations of the documents that are NOT creates, on purpose: a related-items POST adds links to a document and a version
# restore changes which version of a run-created document is current; neither makes a record the harness has to find again, so
# neither needs an intent (the rule table test names them, like the submission links).

# Clients and contacts are never deleted by tools: the run's own, and with cleanup=True the listed leftovers.
_SPECIAL_DELETES: dict[str, str] = {
    "DELETE /v1/clients/{clientId}": "run-created client (never the test client or the second client); with cleanup=True also the listed leftover clients",
    "DELETE /v1/contacts/{contactId}": "run-created contact (never the operator contact); with cleanup=True also the listed leftover contact",
}

# op key -> method name and one-line rule text, filled by the @_rule decorators of LiveGuard.
_HANDLER_NAMES: dict[str, str] = {}
_HANDLER_TEXT: dict[str, str] = {}
# handler method name -> what allow_approved_invoice adds to its rule text (LiveGuard.rule_table() of a guard with the option)
_APPROVED_TEXT_BY_HANDLER: dict[str, str] = {
    "_post_invoice": (
        "with allow_approved_invoice also StatusId exactly 5 (Approved) for the one announced approved invoice: "
        "no RecipientEmails key, one line with Quantity and an explicit UnitPrice above 0 (at most "
        f"{APPROVED_INVOICE_LIMIT:g} in all), TaxId present and null (no tax), no DiscountPercent, BillableStatusId "
        "absent or 1, Reference the label of the open invoice intent with status_id 5, and no other approved invoice in "
        "the run (never from a cleanup)"
    ),
    "_delete_invoice": (
        "with allow_approved_invoice also an invoice recorded with status_id 5, which is voided (StatusId 4, still "
        "listed); Paid (3), Void (4) and an invoice with no recorded status are never deleted"
    ),
}


def _rule(op_key: str, text: str) -> Callable[[Callable[..., None]], Callable[..., None]]:
    """Register a LiveGuard method as the body rule of one operation (it also feeds LiveGuard.rule_table())."""

    def register(function: Callable[..., None]) -> Callable[..., None]:
        _HANDLER_NAMES[op_key] = function.__name__
        _HANDLER_TEXT[op_key] = text
        return function

    return register


def _spelling_conflicts(*tables: Mapping[str, object]) -> dict[str, list[str]]:
    """The operations that these tables do not spell the same way: shape -> every spelling found, sorted.

    _PATH_KINDS, _CREATES, _SPECIAL_DELETES, _HANDLER_NAMES and _HANDLER_TEXT are looked up by the EXACT text of the
    operation key (LiveGuard._check_write), not by its shape. An operation spelled {invoiceID} in one table and
    {invoiceId} in another is therefore two different keys: the path rule would be found, the handler would not, and
    the request would be allowed after the path rule alone."""
    spellings: dict[str, set[str]] = {}
    for table in tables:
        for key in table:
            spellings.setdefault(normalize_op_key(key), set()).add(key)
    return {shape: sorted(keys) for shape, keys in sorted(spellings.items()) if len(keys) > 1}


def _check_one_spelling_per_operation(*tables: Mapping[str, object]) -> None:
    """Refuse to import this module when two of its rule tables spell one operation two ways (_spelling_conflicts)."""
    conflicts = _spelling_conflicts(*tables)
    if conflicts:
        shown = "; ".join(" and ".join(keys) for keys in conflicts.values())
        raise RuntimeError(
            "scripts/live/guard.py spells an operation two ways in its rule tables, so a rule that is looked up by "
            f"the exact text of the operation would not be found: {shown}"
        )


class GuardViolation(RuntimeError):
    """A request the live guard refused. Raised before anything is sent.

    `label` is "METHOD /path" of the refused request (path without the /v1 base, query never included) and
    `reason` says why; str(exc) is "blocked <label>: <reason>". It never holds the API key.
    """

    def __init__(self, message: str, *, label: str = "", reason: str = "") -> None:
        super().__init__(message)
        self.label = label
        self.reason = reason or message


class _BodyProblem(Exception):
    """The body could not be read the way the guard needs; turned into a GuardViolation by the caller."""


@dataclass
class _Req:
    """A request as the guard understands it."""

    method: str
    path: str  # what was sent, without the /v1 base, for example /tickets/<id>/comments
    params: dict[str, str]  # the path placeholders of the matched operation
    body: dict[str, Any] | None = None  # the JSON object body (operations with a JSON body)
    form: dict[str, str] | None = None  # the multipart text fields

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"

    def block(self, reason: str) -> GuardViolation:
        return GuardViolation(f"blocked {self.label}: {reason}", label=self.label, reason=reason)


@dataclass(frozen=True)
class _Route:
    key: str  # the operation key, for example "POST /v1/tickets/{ticketId}/comments"
    method: str
    pattern: tuple[str, ...]  # ("tickets", "{ticketId}", "comments")
    fields: frozenset[str] | None  # the top-level body fields of an operation with a body (None: no body)
    multipart: bool


def _short(text: Any, limit: int = 60) -> str:
    shown = ascii(str(text))
    return shown if len(shown) <= limit else shown[: limit - 3] + "..."


def _shown_path(path: str) -> str:
    """A request path for a message that is written before the path was checked: escaped, shortened, no /v1."""
    text = ascii(path)[1:-1]
    if text.startswith(API_BASE_PATH + "/"):
        text = text[len(API_BASE_PATH):]
    return text if len(text) <= 70 else text[:67] + "..."


def _name(text: Any) -> str:
    """A field name for a message: shown when it is a plain identifier, else hidden (a key can hold anything)."""
    return text if isinstance(text, str) and _PLAIN_NAME.fullmatch(text) else "<unusual name>"


def _json_kind(value: Any) -> str:
    """What a JSON value is, for a message about a value that is not allowed: a whole number is shown, anything else
    is only described (a string or a list can hold an address or other personal data)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return "a decimal number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "a list"
    return "an object"


def _is_status(value: Any, wanted: int) -> bool:
    """True when `value` is exactly the JSON integer `wanted`: never a bool, a decimal number or text."""
    return isinstance(value, int) and not isinstance(value, bool) and value == wanted


def _positive_number(value: Any) -> bool:
    """True for a finite number above 0 (an int or a float, never a bool)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _build_routes(spec: SpecIndex) -> dict[str, list[_Route]]:
    routes: dict[str, list[_Route]] = {}
    prefix = API_BASE_PATH + "/"
    for op in spec.ops.values():
        if not op.path.startswith(prefix):
            continue
        fields: frozenset[str] | None = None
        if op.body:
            fields = frozenset((op.body.get("fields") or {}).keys())
        routes.setdefault(op.method, []).append(
            _Route(
                key=op.key,
                method=op.method,
                pattern=tuple(op.path[len(prefix):].split("/")),
                fields=fields,
                multipart=op.is_multipart,
            )
        )
    return routes


def _decoded_forms(text: str) -> set[str]:
    """The text and what it becomes when HTML entities and percent escapes are decoded, up to three rounds in
    any order, so an address written as bob&#64;example.com or bob%2540example.com is still found."""
    forms = {text}
    frontier = [text]
    for _ in range(3):
        following = []
        for form in frontier:
            for decode in (html.unescape, unquote):
                decoded = decode(form)
                if decoded not in forms:
                    forms.add(decoded)
                    following.append(decoded)
        frontier = following
    return forms


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _value in pairs:
        folded = key.casefold()
        if folded in seen:
            raise _BodyProblem("a JSON object has the same key twice (names are matched case-insensitively)")
        seen.add(folded)
    return dict(pairs)


def _reject_constant(name: str) -> Any:
    raise _BodyProblem(f"the JSON body contains {name}, which is not valid JSON")


def _load_json_object(content: bytes) -> dict[str, Any]:
    if not content:
        raise _BodyProblem("the request has no JSON body")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise _BodyProblem("the body is not UTF-8 text") from None
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except _BodyProblem:
        raise
    except (ValueError, RecursionError):
        raise _BodyProblem("the body is not valid JSON") from None
    if not isinstance(data, dict):
        raise _BodyProblem("the body is not a JSON object")
    return data


def _walk_strings(node: Any, where: str, depth: int = 0) -> Iterator[tuple[str, str]]:
    """Every string of a JSON value, object keys included, with its location (Location.Name, SecondaryEmail[0])."""
    if depth > _MAX_DEPTH:
        raise _BodyProblem("the JSON body is nested too deeply to inspect")
    if isinstance(node, str):
        yield where, node
    elif isinstance(node, dict):
        for key, value in node.items():
            here = f"{where}.{_name(key)}" if where else _name(key)
            yield here, key
            yield from _walk_strings(value, here, depth + 1)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_strings(value, f"{where}[{index}]", depth + 1)


def _parse_multipart(content_type: str, content: bytes) -> tuple[dict[str, str], list[tuple[str, str, bytes]]]:
    """(text fields, [(field name, file name, content)]) of a multipart/form-data body; _BodyProblem if odd."""
    if not content_type.lower().startswith("multipart/form-data"):
        raise _BodyProblem("the body is not multipart/form-data")
    header = b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("latin-1", "replace") + b"\r\n\r\n"
    message = BytesParser(policy=policy.default).parsebytes(header + content)
    if not message.is_multipart():
        raise _BodyProblem("the multipart body has no parts")
    if (message.preamble or "").strip() or (message.epilogue or "").strip():
        raise _BodyProblem("the multipart body has text before the first part or after the last one")
    form: dict[str, str] = {}
    files: list[tuple[str, str, bytes]] = []
    seen: set[str] = set()
    for part in message.iter_parts():
        if part.is_multipart():
            raise _BodyProblem("the multipart body has a nested multipart part")
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str) or not name:
            raise _BodyProblem("a multipart part has no name")
        if name.casefold() in seen:
            raise _BodyProblem("the multipart body repeats a field name")
        seen.add(name.casefold())
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is None:
            try:
                form[name] = payload.decode("utf-8")
            except UnicodeDecodeError:
                raise _BodyProblem("a multipart text field is not UTF-8 text") from None
        else:
            files.append((name, filename, payload))
    return form, files


class LiveGuard:
    """Allow or refuse each request of a live run. See the module docstring for the allowlist.

    mode:      "read" (GET only) or "write".
    manifest:  the run manifest (required in write mode; the ids of run-created records come from it).
    cleanup:   write mode only: also allow deleting the listed leftovers.
    allow_approved_invoice: write mode only, off by default: also allow the one Approved invoice of a run (POST /invoices
               with StatusId 5 under the rules of "The approved invoice" in the module docstring) and the void of an
               invoice recorded with status_id 5 (DELETE /invoices/{invoiceId}). Only write_matrix --with-approved-invoice
               and cleanup --void-approved set it. With cleanup=True the create stays refused: a cleanup creates nothing.
    allow_custom_assets: write mode only, off by default: also allow POST /assets/custom and PATCH /assets/custom/{id}
               under the rules of the module docstring. Only write_matrix --with-custom-assets sets it. This harness never
               deletes a custom asset (the spec's soft DELETE is not used), so the run that creates one leaves it on the test
               client.
    allow_custom_properties: write mode only, off by default: also allow the one PATCH of the TEST CLIENT that sets or clears a
               custom field the manifest records (module docstring, "The custom properties"). Only write_matrix
               --with-custom-properties sets it, and cleanup.run_cleanup when the manifest holds a custom_property record
               (with cleanup=True the guard allows the clear, the null Value, and nothing else).
    allow_custom_asset_move: write mode only, off by default, and only together with allow_custom_assets: the PATCH of a custom asset
               this run created may name the second client (a MOVE of the asset, which has to be announced in the manifest first and
               carry a LocationId). Only write_matrix --with-asset-move sets it. Never from a cleanup.
    allow_documents: write mode only, off by default: the document writes of the module docstring ("The documents"), for documents this
               run created and for the test client. Only write_matrix --with-documents sets it. A cleanup guard (cleanup=True) never
               has it and still archives (StatusId 2) a run-created document.
    Counters: allowed_count is every request let through, writes_allowed the ones among them that are not a GET.
    The other keyword arguments tune the constants of the allowlist (the defaults are the configured values);
    `allowed_hosts` and `spec` exist for tests. base_url: the Settings.base_url of the run; the guard then allows that host only
    (the configured host only). Without it, and without allowed_hosts, the guard allows the US host api.usw.gorelo.io
    only: a run for a non-US host must pass base_url, and a forgotten one is blocked loudly, never allowed. probe_domains: the
    domains an uptime check may target.
    require_intents: refuse a create unless the manifest holds an open intent of that kind (see the module docstring).
    """

    def __init__(
        self,
        mode: Literal["read", "write"],
        manifest: Manifest | None,
        *,
        allowed_clients: frozenset[int] | set[int] | None = None,
        operator_contact: int | None = None,
        operator_user: int | None = None,
        allowed_emails: frozenset[str] | set[str] | None = None,
        test_email_domain: str = TEST_EMAIL_DOMAIN,
        approved_leftovers: Mapping[str, frozenset[int] | set[int]] | None = None,
        cleanup: bool = False,
        test_client: int | None = None,
        allowed_hosts: frozenset[str] | set[str] | None = None,
        base_url: str | None = None,
        probe_domains: frozenset[str] | set[str] | None = None,
        require_intents: bool = False,
        allow_approved_invoice: bool = False,
        allow_custom_assets: bool = False,
        allow_custom_properties: bool = False,
        allow_custom_asset_move: bool = False,
        allow_documents: bool = False,
        spec: SpecIndex | None = None,
    ) -> None:
        if mode not in ("read", "write"):
            raise ValueError(f"mode must be 'read' or 'write', not {mode!r}")
        if mode == "write" and manifest is None:
            raise ValueError("a write-mode guard needs the run manifest")
        if cleanup and mode != "write":
            raise ValueError("cleanup=True only makes sense in write mode")
        if allow_approved_invoice and mode != "write":
            raise ValueError("allow_approved_invoice=True only makes sense in write mode")
        if allow_custom_assets and mode != "write":
            raise ValueError("allow_custom_assets=True only makes sense in write mode")
        if allow_custom_properties and mode != "write":
            raise ValueError("allow_custom_properties=True only makes sense in write mode")
        if allow_custom_asset_move and mode != "write":
            raise ValueError("allow_custom_asset_move=True only makes sense in write mode")
        if allow_custom_asset_move and not allow_custom_assets:
            raise ValueError("allow_custom_asset_move=True needs allow_custom_assets=True: only a custom asset of this run can move")
        if allow_documents and mode != "write":
            raise ValueError("allow_documents=True only makes sense in write mode")
        if allowed_hosts is not None and base_url is not None:
            raise ValueError("pass allowed_hosts or base_url, not both")
        hosts = allowed_hosts if allowed_hosts is not None else api_hosts(base_url) if base_url is not None else API_HOSTS
        cfg = site()  # raises SiteConfigError (names the missing key and site.example.toml) when not configured
        if allowed_clients is None:
            allowed_clients = {cfg.test_client, cfg.second_client}
        if operator_contact is None:
            operator_contact = cfg.operator_contact
        if operator_user is None:
            operator_user = cfg.operator_user
        if allowed_emails is None:
            allowed_emails = {cfg.operator_email}
        if test_client is None:
            test_client = cfg.test_client
        if probe_domains is None:
            probe_domains = {cfg.probe_domain}
        if approved_leftovers is None:
            approved_leftovers = {"client": cfg.leftover_clients, "contact": cfg.leftover_contacts}
        self.mode = mode
        self.manifest = manifest
        self.cleanup = cleanup
        self.allow_approved_invoice = allow_approved_invoice
        self.allow_custom_assets = allow_custom_assets
        self.allow_custom_properties = allow_custom_properties
        self.allow_custom_asset_move = allow_custom_asset_move
        self.allow_documents = allow_documents
        self.allowed_clients = frozenset(allowed_clients)
        self.operator_contact = operator_contact
        self.operator_user = operator_user
        self.allowed_emails = frozenset(address.lower() for address in allowed_emails)
        self.test_email_domain = test_email_domain.lower()
        source = approved_leftovers
        self.approved_leftovers: dict[str, frozenset[int]] = {kind: frozenset(ids) for kind, ids in source.items()}
        self.test_client = test_client
        self.allowed_hosts = frozenset(host.lower() for host in hosts)
        self.probe_domains = frozenset(domain.lower() for domain in probe_domains)
        self.require_intents = require_intents
        self._routes = _build_routes(spec if spec is not None else load_spec_index())
        self._ticket_contacts: dict[str, set[int]] = {}
        self._approved_creates = 0  # Approved invoices this guard allowed (allow_approved_invoice permits one)
        self.violations: list[str] = []
        self.allowed_count = 0
        self.writes_allowed = 0  # allowed requests that are not a GET: a harness compares it before and after a call

    # -- the hook ----------------------------------------------------------

    async def __call__(self, request: httpx.Request) -> None:
        """httpx request event hook: reads the body (the request can still be sent afterwards), then checks."""
        try:
            await request.aread()
        except Exception as exc:
            violation = GuardViolation(
                f"blocked {_short(request.method)} request: its body could not be read ({type(exc).__name__})"
            )
            self.violations.append(str(violation))
            raise violation from None
        self.check(request)

    def check(self, request: httpx.Request) -> None:
        """Raise GuardViolation unless the request is allowed. Any error while inspecting also blocks it."""
        try:
            self._check(request)
        except GuardViolation as exc:
            self.violations.append(str(exc))
            raise
        except Exception as exc:
            violation = GuardViolation(
                f"blocked {_short(request.method)} request: the guard could not inspect it ({type(exc).__name__})"
            )
            self.violations.append(str(violation))
            raise violation from exc
        self.allowed_count += 1
        if request.method.upper() != "GET":
            self.writes_allowed += 1

    @property
    def tripped(self) -> bool:
        """True once any request was refused."""
        return bool(self.violations)

    def assert_clean(self) -> None:
        """Raise GuardViolation (the first refusal) if any request was refused: call it after each step."""
        if self.violations:
            raise GuardViolation(self.violations[0])

    def rule_table(self) -> dict[str, str]:
        """Operation key -> the rule applied to it (reads, writes and the never-allowed operations)."""
        return {route.key: self._rule_text(route.key) for routes in self._routes.values() for route in routes}

    # -- the check ---------------------------------------------------------

    def _check(self, request: httpx.Request) -> None:
        method = request.method.upper()
        url = request.url
        early = f"{method} {_shown_path(url.path)}"
        if method not in _METHODS:
            raise GuardViolation(
                f"blocked {early}: method {_short(method)} is not used with the Gorelo API", label=early
            )
        if self.mode == "read" and method != "GET":
            raise GuardViolation(f"blocked {early}: read mode allows only GET", label=early)
        if (
            url.scheme != "https"
            or url.host.lower() not in self.allowed_hosts
            or url.port not in (None, 443)
            or url.userinfo
        ):
            raise GuardViolation(
                f"blocked {early}: only https requests to {', '.join(sorted(self.allowed_hosts))} are allowed",
                label=early,
            )
        if request.headers.get("host", "").lower().split(":")[0] not in self.allowed_hosts:
            raise GuardViolation(f"blocked {early}: the Host header names another host", label=early)
        for header in _METHOD_OVERRIDE_HEADERS:
            if header in request.headers:
                raise GuardViolation(f"blocked {early}: a method override header is not allowed", label=early)
        raw_path = url.raw_path
        has_query = b"?" in raw_path
        path = raw_path.split(b"?", 1)[0].decode("ascii", "replace")  # a non-ASCII byte fails the segment check
        if not path.startswith(API_BASE_PATH + "/"):
            raise GuardViolation(f"blocked {early}: the path is outside {API_BASE_PATH}/", label=early)
        relative = path[len(API_BASE_PATH):]
        label = f"{method} {relative}"
        segments = tuple(relative[1:].split("/"))
        for segment in segments:
            if segment in ("", ".", "..") or not _SEGMENT.fullmatch(segment):
                raise GuardViolation(
                    f"blocked {label}: path segment {_short(segment)} is not allowed "
                    "(empty, dot, percent-encoded or unusual characters)",
                    label=label,
                )
        if method != "GET" and segments[0].lower() == "assets" and not _is_custom_asset_write(method, segments):
            raise GuardViolation(f"blocked {label}: writes under /assets/ are never allowed", label=label)
        match = self._match(method, segments)
        if match is None:
            raise GuardViolation(f"blocked {label}: this path is not a known Gorelo operation", label=label)
        route, params = match
        req = _Req(method=method, path=relative, params=params)
        content = self._content(request)
        if method == "GET":
            never = _never_reason(route.key)
            if never is not None:
                raise req.block(never)
            if _is_side_effect_get(route.key):
                self._check_export(req)
            if content:
                raise req.block("a GET request must not carry a body")
            return
        self._check_write(request, req, route, content, has_query)

    def _match(self, method: str, segments: tuple[str, ...]) -> tuple[_Route, dict[str, str]] | None:
        best: tuple[int, _Route, dict[str, str]] | None = None
        for route in self._routes.get(method, ()):
            if len(route.pattern) != len(segments):
                continue
            params: dict[str, str] = {}
            literals = 0
            for pattern, segment in zip(route.pattern, segments):
                if pattern.startswith("{"):
                    params[pattern[1:-1]] = segment
                elif pattern == segment:
                    literals += 1
                else:
                    break
            else:
                if best is None or literals > best[0]:
                    best = (literals, route, params)
        return None if best is None else (best[1], best[2])

    @staticmethod
    def _content(request: httpx.Request) -> bytes:
        try:
            return request.content
        except httpx.RequestNotRead:
            return request.read()

    def _check_export(self, req: _Req) -> None:
        """A GET that Gorelo records as an event on the record (the invoice PDF: an export event): never in read mode,
        in write mode only for an invoice this run created. The invoice id is the only placeholder of the path,
        whatever Gorelo calls it (the operation is recognized by its shape, see _is_side_effect_get)."""
        reason = "the invoice PDF download is recorded on the invoice as an export event"
        if self.mode == "read":
            raise req.block(reason + ", so read mode never sends it")
        if len(req.params) != 1:
            raise req.block(reason + ", and its path has no single invoice id the guard could check")
        ((placeholder, value),) = req.params.items()
        try:
            key = self._require_created(req, "invoice", value, f"path {placeholder}")
        except GuardViolation as refusal:
            raise req.block(f"{reason}, so only an invoice this run created may be exported: {refusal.reason}") from None
        problem = self._draft_problem(key)
        if problem is not None:
            raise req.block(f"{reason}, so only a Draft this run created may be exported: {problem}")

    def _draft_problem(self, key: int | str) -> str | None:
        """Why the manifest does not record this run-created invoice as a Draft (details status_id exactly the integer 1),
        or None when it does. An invoice that is not a Draft is never deleted (Gorelo would void it) or exported."""
        assert self.manifest is not None
        details = self.manifest.details("invoice", key)
        status = details.get("status_id")
        if isinstance(status, bool) or not isinstance(status, int) or status != INVOICE_DRAFT:
            shown = "missing" if "status_id" not in details else _json_kind(status)
            return f"invoice {key} is not recorded as a Draft (the manifest details status_id is {shown}, not 1)"
        return None

    # -- writes ------------------------------------------------------------

    def _check_write(self, request: httpx.Request, req: _Req, route: _Route, content: bytes, has_query: bool) -> None:
        if has_query:
            raise req.block("a write must not carry a query string")
        never = _never_reason(route.key)
        if never is not None:
            raise req.block(never)
        handler = _HANDLER_NAMES.get(route.key)
        path_kinds = _PATH_KINDS.get(route.key)
        special = route.key in _SPECIAL_DELETES
        if handler is None and path_kinds is None and not special:
            raise req.block("this operation is not on the live-test allowlist")
        assert self.manifest is not None  # a write-mode guard always has one
        if self.require_intents:
            created = _CREATES.get(route.key)
            if created is not None and not any(i.kind == created for i in self.manifest.unresolved_intents()):
                raise req.block(
                    f"no open intent of kind {created!r}: call manifest.intent({created!r}, label) before the create, "
                    "so a record that is created but never recorded can be found again"
                )
        texts: list[tuple[str, str]] = []
        try:
            if route.multipart:
                form, files = _parse_multipart(request.headers.get("content-type", ""), content)
                req.form = form
                allowed = route.fields or frozenset()
                for name in [*form, *(file_name for file_name, _filename, _payload in files)]:
                    if name not in allowed:
                        raise req.block(f"the form has a field the spec does not define: {_name(name)}")
                texts.extend(form.items())
                for name, filename, payload in files:
                    texts.append((f"{name} file name", filename))
                    texts.append((f"{name} file content", payload.decode("utf-8", "replace")))
            elif route.fields is not None:
                body = {} if (not content and route.key in _OPTIONAL_BODY) else _load_json_object(content)
                unknown = sorted(key for key in body if key not in route.fields)
                if unknown:
                    raise req.block(f"the body has a field the spec does not define for this operation: {_name(unknown[0])}")
                req.body = body
                texts.extend(_walk_strings(body, ""))
            elif content:
                raise req.block("this operation takes no body")
        except _BodyProblem as problem:
            raise req.block(str(problem)) from None
        for where, text in texts:
            self._check_text(req, where, text)
        for kind, placeholder in path_kinds or ():
            self._require_created(req, kind, req.params[placeholder], f"path {placeholder}")
        if req.body is not None:
            self._check_common_fields(req)
        if handler is not None:
            getattr(self, handler)(req)
        elif special:
            self._delete_client_or_contact(req, route)

    # -- emails ------------------------------------------------------------

    def _check_text(self, req: _Req, where: str, text: str) -> None:
        """Block text that holds an email address that is not allowed (also when HTML or percent encoded)."""
        if text.isascii() and "@" not in text and "%" not in text and "&" not in text:
            return
        for variant in _decoded_forms(text):
            for token in _EMAIL_SPLIT.split(unicodedata.normalize("NFKC", variant)):
                if "@" not in token:
                    continue
                candidate = token.strip(".")
                if not self._address_allowed(candidate):
                    raise req.block(
                        f"{where or 'the body'} holds an email address that is not allowed ({self._mask(candidate)}); "
                        f"only {', '.join(sorted(self.allowed_emails))} and addresses at @{self.test_email_domain} may appear"
                    )

    def _address_allowed(self, address: str) -> bool:
        if not address.isascii():
            return False
        lowered = address.lower()
        if lowered in self.allowed_emails:
            return True
        local, separator, domain = lowered.partition("@")
        return bool(separator) and domain == self.test_email_domain and bool(_LOCAL_PART.fullmatch(local))

    @staticmethod
    def _mask(address: str) -> str:
        """The address with its local part hidden after the first character (it can be a customer's address)."""
        local, _separator, domain = address.partition("@")
        return f"{ascii(local[:1])[1:-1]}***@{ascii(domain)[1:-1]}"

    def _require_address(self, req: _Req, value: Any, where: str) -> None:
        if not isinstance(value, str) or not self._address_allowed(value.strip()):
            raise req.block(
                f"{where} must be {', '.join(sorted(self.allowed_emails))} or an address at @{self.test_email_domain}"
            )

    # -- ids ---------------------------------------------------------------

    def _ids(self, kind: str) -> set[int | str]:
        assert self.manifest is not None
        return self.manifest.ids(kind)

    def _require_created(self, req: _Req, kind: str, raw: Any, where: str) -> int | str:
        """The canonical id, after checking that this run created that record."""
        try:
            key = normalize_id(kind, raw)
        except ValueError:
            wanted = "a positive integer" if ID_TYPES[kind] == "int" else "a UUID"
            raise req.block(f"{where} is not a valid {_KIND_NAMES[kind]} id (it must be {wanted})") from None
        if key not in self._ids(kind):
            assert self.manifest is not None
            raise req.block(
                f"{where} {key} is not a {_KIND_NAMES[kind]} created by this run ({self.manifest.run_id}); "
                "only records the run created may be changed"
            )
        return key

    def _body_created(self, req: _Req, kind: str, field: str, value: Any) -> int | str:
        """A body field that names a run-created record: a JSON integer or a UUID string, then the manifest."""
        if ID_TYPES[kind] == "int":
            self._json_int(req, field, value)
        elif not isinstance(value, str):
            raise req.block(f"{field} must be a UUID string")
        return self._require_created(req, kind, value, field)

    @staticmethod
    def _json_int(req: _Req, field: str, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise req.block(f"{field} must be a JSON integer")
        return value

    def _scalar_int(self, req: _Req, field: str) -> int | None:
        assert req.body is not None
        value = req.body.get(field)
        return None if value is None else self._json_int(req, field, value)

    def _int_list(self, req: _Req, field: str) -> list[int]:
        assert req.body is not None
        value = req.body.get(field)
        if value is None:
            return []
        if not isinstance(value, list):
            raise req.block(f"{field} must be a list of JSON integers")
        return [self._json_int(req, f"{field}[{index}]", item) for index, item in enumerate(value)]

    def _uuid_list(self, req: _Req, field: str) -> list[str]:
        assert req.body is not None
        value = req.body.get(field)
        if value is None:
            return []
        if not isinstance(value, list):
            raise req.block(f"{field} must be a list of UUID strings")
        found = []
        for index, item in enumerate(value):
            if not isinstance(item, str) or not _UUID.fullmatch(item):
                raise req.block(f"{field}[{index}] must be a UUID string")
            found.append(item.lower())
        return found

    def _require_client(self, req: _Req, client: int) -> None:
        if client not in self.allowed_clients:
            raise req.block(
                f"ClientId {client} is not allowed; live tests may only touch clients "
                f"{', '.join(str(c) for c in sorted(self.allowed_clients))}"
            )

    def _require_test_client(self, req: _Req, client: int | None, *, required: bool) -> None:
        if client is None:
            if required:
                raise req.block(f"ClientId is required and must be {self.test_client} (the test client)")
            return
        if client != self.test_client:
            raise req.block(f"ClientId must be {self.test_client} (the test client), not {client}")

    def _contact_allowed(self, contact: int) -> bool:
        return contact == self.operator_contact or contact in self._ids("contact")

    def _contact_message(self, field: str, contact: int) -> str:
        return (
            f"{field} holds contact {contact}, which is neither the operator contact {self.operator_contact} "
            "nor a contact created by this run"
        )

    # -- the rules every JSON body must pass --------------------------------

    def _check_common_fields(self, req: _Req) -> None:
        body = req.body
        assert body is not None
        contact = self._scalar_int(req, "ContactId")
        if contact is not None and not self._contact_allowed(contact):
            raise req.block(self._contact_message("ContactId", contact))
        for field in ("CcContactIds", "ContactIds", "SharedWithContactIds"):
            for contact in self._int_list(req, field):
                if not self._contact_allowed(contact):
                    raise req.block(self._contact_message(field, contact))
        lead = self._scalar_int(req, "LeadAssigneeId")
        if lead is not None and lead != self.operator_user:
            raise req.block(f"LeadAssigneeId {lead} is not allowed; only the operator user {self.operator_user}")
        for field in ("AssistingAssigneeIds", "WatcherIds"):
            for user in self._int_list(req, field):
                if user != self.operator_user:
                    raise req.block(f"{field} holds user {user}; only the operator user {self.operator_user} is allowed")
        user_id = self._scalar_int(req, "UserId")
        if user_id is not None and user_id != self.operator_user:
            raise req.block(f"UserId {user_id} is not allowed; only the operator user {self.operator_user}")
        for field in ("AgentAssetIds", "CustomAssetIds"):
            value = body.get(field)
            if value is not None and value != []:
                raise req.block(f"{field} must be empty: live tests never link customer assets")
        for field, kind in (("UptimeIds", "uptime"), ("BlockedByTaskIds", "task"), ("BlockingTaskIds", "task")):
            for item in self._uuid_list(req, field):
                self._require_created(req, kind, item, field)
        section = body.get("SectionId")
        if section is not None:
            self._body_created(req, "section", "SectionId", section)
        adopt = body.get("AdoptClientAssets")
        if adopt is not None and adopt is not False:  # strict: a server that coerces 1 or "true" must not be given them
            raise req.block("AdoptClientAssets must be absent or false: true moves devices between clients")

    # -- the rule table ------------------------------------------------------

    def _rule_text(self, key: str) -> str:
        never = _never_reason(key)
        if never is not None:
            return "never: " + never
        if _is_side_effect_get(key):
            return (
                "write mode only, and only a run-created invoice whose manifest details hold status_id 1; never in read "
                "mode: the PDF download records an export event on the invoice"
            )
        if key.startswith("GET "):
            return "allowed"
        parts = []
        kinds = _PATH_KINDS.get(key)
        if kinds:
            parts.append("path ids run-created: " + ", ".join(placeholder for _kind, placeholder in kinds))
        if key in _HANDLER_TEXT:
            parts.append(_HANDLER_TEXT[key])
            extra = _APPROVED_TEXT_BY_HANDLER.get(_HANDLER_NAMES[key]) if self.allow_approved_invoice else None
            if extra is not None:
                parts.append(extra)
        if key in _SPECIAL_DELETES:
            parts.append(_SPECIAL_DELETES[key])
        return "; ".join(parts) if parts else "blocked: not on the allowlist"

    # -- the rules of single operations ---------------------------------------

    def _delete_client_or_contact(self, req: _Req, route: _Route) -> None:
        kind = "client" if route.key == "DELETE /v1/clients/{clientId}" else "contact"
        try:
            key = normalize_id(kind, req.params["clientId" if kind == "client" else "contactId"])
        except ValueError:
            raise req.block(f"the path id is not a valid {kind} id") from None
        protected = self.allowed_clients if kind == "client" else frozenset({self.operator_contact})
        if key in protected:
            raise req.block(f"{kind} {key} is one of the records the live tests run on and is never deleted")
        if key in self._ids(kind):
            return
        approved = key in self.approved_leftovers.get(kind, frozenset())
        if approved and self.cleanup:
            return
        hint = f"; the listed leftover {kind}s are deleted only by the cleanup command (--leftovers)" if approved else ""
        assert self.manifest is not None
        raise req.block(f"{kind} {key} was not created by this run ({self.manifest.run_id}){hint}")

    @_rule("POST /v1/clients", "Name starts with MCPTEST- (the approved temporary client); Domain, if present, at example.invalid")
    def _post_client(self, req: _Req) -> None:
        assert req.body is not None
        name = req.body.get("Name")
        if not isinstance(name, str) or not name.startswith(TEMP_CLIENT_PREFIX):
            raise req.block(f"Name must start with {TEMP_CLIENT_PREFIX} (only the temporary test client may be created)")
        domain = req.body.get("Domain")
        if domain is not None and not self._is_test_domain(domain):
            raise req.block(
                f"Domain must be absent or at {self.test_email_domain}: a real domain would route that company's "
                "inbound email to the temporary client"
            )

    def _is_test_domain(self, domain: Any) -> bool:
        if not isinstance(domain, str) or not domain.isascii():
            return False
        lowered = domain.lower()
        return lowered == self.test_email_domain or lowered.endswith("." + self.test_email_domain)

    @_rule(
        "PATCH /v1/clients/{clientId}",
        "path id is a run-created client (never the second client); the test client only with allow_custom_properties and "
        "only a body of CustomProperties and nothing else: ONE entry for a custom field the manifest records as unset before "
        "the run, its Value the run id or null (a cleanup sends the null only); once the record is cleaned the matrix takes no "
        "further write to that field, a null included; CustomProperties never go to a run-created client; the body has no Id "
        "(the spec defines none)",
    )
    def _patch_client(self, req: _Req) -> None:
        try:
            client = normalize_id("client", req.params["clientId"])
        except ValueError:
            raise req.block("the path id is not a valid client id") from None
        if client == self.test_client:  # also when a manifest wrongly lists it as run-created
            self._patch_test_client(req, client)
            return
        if client in self.allowed_clients:  # the second client, also when a manifest wrongly lists it as run-created
            raise req.block(self._never_changed(client))
        if client not in self._ids("client"):
            raise req.block(f"path id {client} is not allowed; only a client created by this run may be changed")
        assert req.body is not None
        if "CustomProperties" in req.body:
            raise req.block(
                f"CustomProperties may go only to the test client {self.test_client} (the custom_properties area): a client "
                "the run created takes none"
            )
        # the record is named by the path alone: UpdateClientCommand has no Id, so a body that carries one is
        # already refused above as a field the spec does not define

    @staticmethod
    def _never_changed(client: int) -> str:
        return (
            f"path id {client} is not allowed; client {client} is one of the records the live tests run on and is never changed"
        )

    def _patch_test_client(self, req: _Req, client: int) -> None:
        """PATCH of the TEST CLIENT itself: never, except the one custom field write of the custom_properties area.

        All of these must hold (module docstring, "The custom properties"): the option allow_custom_properties; a body of
        CustomProperties and nothing else, with exactly one entry {Id, Value}; Id a JSON integer that the manifest records as
        a custom_property of this client with was_set exactly false (any other Id, CUSTOM_PROPERTY_UNKNOWN_ID included, is an
        Id the run did not record); Value null or text starting with the run id, and for a recorded field exactly the text the
        record holds, and no write at all once the record is cleaned (a cleanup guard still takes the null); a cleanup guard
        allows the null only. Anything else keeps the old message: the test client is never changed."""
        if not self.allow_custom_properties:
            raise req.block(self._never_changed(client))
        assert req.body is not None and self.manifest is not None
        body, run_id = req.body, self.manifest.run_id
        if set(body) != {"CustomProperties"}:
            raise req.block(
                self._never_changed(client)
                + ", except that a --with-custom-properties run may set or clear ONE custom field of it, with a body of "
                "CustomProperties and nothing else"
            )
        entries = body["CustomProperties"]
        if not isinstance(entries, list) or len(entries) != 1:
            raise req.block("CustomProperties must be a list with exactly one entry: one custom field at a time")
        entry = entries[0]
        if not isinstance(entry, dict) or set(entry) != {"Id", "Value"}:
            raise req.block("the CustomProperties entry must be an object with Id and Value and nothing else")
        field = self._json_int(req, "CustomProperties[0].Id", entry["Id"])
        value = entry["Value"]
        if value is not None and (not isinstance(value, str) or not value.startswith(run_id)):
            raise req.block(
                f"CustomProperties[0].Value must be null (a clear) or text that starts with the run id {run_id}"
            )
        if self.cleanup and value is not None:
            raise req.block("a cleanup only clears a custom field (Value null): it never saves a value")
        if field not in self._ids("custom_property"):  # manifest.ids() holds the cleaned records too
            raise req.block(
                f"custom field {field} is not one the run recorded in the manifest: a custom_property record is written "
                "before the first write, and only a recorded field may be written"
            )
        record = self.manifest.record("custom_property", field)
        details = record.details
        if details.get("was_set") is not False:
            raise req.block(
                f"custom field {field} is not recorded as unset before the run (the manifest details was_set is not "
                "false), so the run may not write it"
            )
        if details.get("client_id") != client:
            raise req.block(f"custom field {field} is recorded for another client than the test client {client}")
        if value is not None and value != details.get("value"):
            raise req.block(f"CustomProperties[0].Value must be the text the manifest records for custom field {field}")
        if record.cleaned and not self.cleanup:  # the matrix is done with a field it cleared: no write at all, a null included
            raise req.block(
                f"custom field {field} was already cleared by this run (its record is cleaned): this guard takes no further "
                "write to it, a clear included"
            )

    @_rule("POST /v1/contacts", "ClientId is the test client")
    def _post_contact(self, req: _Req) -> None:
        self._require_test_client(req, self._scalar_int(req, "ClientId"), required=True)

    @_rule(
        "PATCH /v1/contacts/{contactId}",
        "the body has no ContactId (the spec defines none); ClientId, if present, is the test client",
    )
    def _patch_contact(self, req: _Req) -> None:
        # the path id was already checked to be a contact this run created (_PATH_KINDS); the record is named by the
        # path alone, because UpdateContactCommand has no ContactId (a body that carries one is refused as unknown)
        self._require_test_client(req, self._scalar_int(req, "ClientId"), required=False)

    @_rule(
        "POST /v1/tickets",
        "ClientId in the allowed clients; contacts only operator or run-created; "
        "SendTicketCreatedEmail only with the operator contact",
    )
    def _post_ticket(self, req: _Req) -> None:
        assert req.body is not None
        client = self._scalar_int(req, "ClientId")
        if client is None:
            raise req.block("ClientId is required")
        self._require_client(req, client)
        contact = self._scalar_int(req, "ContactId")
        send = req.body.get("SendTicketCreatedEmail")
        if send is not None and not isinstance(send, bool):
            raise req.block("SendTicketCreatedEmail must be true or false")
        if send is True:
            if contact != self.operator_contact:
                raise req.block(
                    f"SendTicketCreatedEmail may be true only when ContactId is the operator contact {self.operator_contact}"
                )
            if any(cc != self.operator_contact for cc in self._int_list(req, "CcContactIds")):
                raise req.block("SendTicketCreatedEmail=true would email the CC contacts; only the operator may be emailed")

    @_rule("PATCH /v1/tickets/{ticketId}", "ClientId, if present, in the allowed clients; contacts as for POST /tickets")
    def _patch_ticket(self, req: _Req) -> None:
        client = self._scalar_int(req, "ClientId")
        if client is not None:
            self._require_client(req, client)
        contacts = set(self._int_list(req, "CcContactIds"))
        contact = self._scalar_int(req, "ContactId")
        if contact is not None:
            contacts.add(contact)
        if contacts:
            # remembered only now that every check passed: a later public comment must not email them
            ticket = normalize_id("ticket", req.params["ticketId"])
            self._ticket_contacts.setdefault(str(ticket), set()).update(contacts)

    @_rule(
        "POST /v1/tickets/{ticketId}/comments",
        "ConversationTypeId 1 (public) only when the ticket's contact and CCs are absent or the operator contact; "
        "2 (private); 3 (side conversation) and 4 (approval) only with a ConversationId of a side conversation or "
        "approval this run created on that ticket; 1 and 2 carry no ConversationId",
    )
    def _post_ticket_comment(self, req: _Req) -> None:
        kind = self._comment_type(req)
        if kind == CONVERSATION_PUBLIC:
            self._require_operator_audience(req)
        elif kind not in (CONVERSATION_PRIVATE, CONVERSATION_SIDE, CONVERSATION_APPROVAL):
            raise req.block(f"ConversationTypeId {kind} is not a known comment type (1 to 4)")
        self._check_conversation(req, kind, parent="ticket", placeholder="ticketId", parent_key="ticket_id")

    def _comment_type(self, req: _Req) -> int:
        assert req.body is not None
        kind = req.body.get("ConversationTypeId")
        if kind is None:
            raise req.block("ConversationTypeId is required (without it the comment type is not known to be private)")
        return self._json_int(req, "ConversationTypeId", kind)

    def _check_conversation(self, req: _Req, kind: int, *, parent: str, placeholder: str, parent_key: str) -> None:
        """The ConversationId of a comment, by its type (`parent` is the manifest kind of the path's record).

        A side conversation comment emails that conversation's recipients and an approval comment its approvers,
        and a ConversationId is a bare value the guard cannot otherwise place: it must name a side conversation
        (type 3) or an approval (type 4) that THIS RUN created, and the manifest details `parent_key` of that
        record must be the ticket (task) of the path, so a comment never goes into a conversation of another
        record. A public or private comment is the main thread and carries no ConversationId at all."""
        assert req.body is not None and self.manifest is not None
        value = req.body.get("ConversationId")
        record_kind = _CONVERSATION_KINDS.get(kind)
        if record_kind is None:
            if value is not None:
                raise req.block(
                    "ConversationId must be absent on a public or private comment (only a side conversation or "
                    "approval comment names a conversation)"
                )
            return
        one, many = _CONVERSATION_WORDS[record_kind]
        owner = _KIND_NAMES[parent]
        if value is None:
            raise req.block(f"ConversationId is required for this comment: the id of {one} this run created on the {owner}")
        if not isinstance(value, str):
            raise req.block("ConversationId must be a string")
        try:
            key = normalize_id(record_kind, value)
        except ValueError:
            raise req.block(f"ConversationId is not a usable id for {one}") from None
        if key not in self._ids(record_kind):
            raise req.block(
                f"ConversationId {_short(key)} is not one of the {many} this run created ({self.manifest.run_id}); "
                f"a comment of this type may only go into {one} the run created on the {owner} of the path"
            )
        try:
            belongs_to = normalize_id(parent, self.manifest.details(record_kind, key).get(parent_key))
        except ValueError:
            raise req.block(
                f"the manifest has no valid {parent_key} for {one} ({_short(key)}), so it cannot be tied to the "
                f"{owner} of the path (record it when the conversation is created)"
            ) from None
        wanted = normalize_id(parent, req.params[placeholder])
        if belongs_to != wanted:
            raise req.block(
                f"ConversationId {_short(key)} belongs to {owner} {_short(belongs_to)}, not to the {owner} of the "
                f"path ({_short(wanted)})"
            )

    def _require_operator_audience(self, req: _Req) -> None:
        """A public comment emails the ticket's contact and CCs: they may be nobody or the operator only."""
        assert self.manifest is not None
        ticket = normalize_id("ticket", req.params["ticketId"])
        details = self.manifest.details("ticket", ticket)
        contact_keys = [key for key in _CONTACT_DETAIL_KEYS if key in details]
        if not contact_keys:
            raise req.block(
                f"a public comment needs the ticket's contact: the manifest has no contact_id for ticket {ticket} "
                "(record contact_id, None when there is none)"
            )
        recorded: list[Any] = [details[key] for key in contact_keys]
        for key in _CC_DETAIL_KEYS:
            value = details.get(key)
            recorded.extend(value if isinstance(value, list) else [] if value is None else [value])
        audience: set[int] = set()
        for value in recorded:
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise req.block(f"the manifest holds an invalid contact for ticket {ticket}")
            audience.add(value)
        audience |= self._ticket_contacts.get(str(ticket), set())
        others = sorted(audience - {self.operator_contact})
        if others:
            raise req.block(
                f"a public comment would email contact(s) {', '.join(str(c) for c in others)} of ticket {ticket}; "
                f"public comments are allowed only when the ticket has no contact or only the operator contact "
                f"{self.operator_contact}"
            )

    @_rule("POST /v1/tickets/{ticketId}/conversations/side-conversation", "Email and CcEmails allowed addresses")
    def _post_ticket_side_conversation(self, req: _Req) -> None:
        self._check_side_conversation(req)

    @_rule(
        "POST /v1/projects/{projectId}/tasks/{taskId}/conversations/side-conversation",
        "Email and CcEmails allowed addresses",
    )
    def _post_task_side_conversation(self, req: _Req) -> None:
        self._check_side_conversation(req)

    def _check_side_conversation(self, req: _Req) -> None:
        assert req.body is not None
        self._require_address(req, req.body.get("Email"), "Email")
        copies = req.body.get("CcEmails")
        if copies is not None and not isinstance(copies, list):
            raise req.block("CcEmails must be a list of email addresses")
        for index, address in enumerate(copies or []):
            self._require_address(req, address, f"CcEmails[{index}]")

    @_rule(
        "POST /v1/attachments",
        "itemType Ticket, Task or Project, or Document only with allow_documents (never from a cleanup); itemId a run-created "
        "record of that type",
    )
    def _post_attachment(self, req: _Req) -> None:
        assert req.form is not None
        item_type = req.form.get("itemType")
        kinds = {"ticket": "ticket", "task": "task", "project": "project", "document": "document"}
        kind = kinds.get(item_type.strip().lower()) if isinstance(item_type, str) else None
        if kind is None:
            raise req.block("itemType must be Ticket, Task, Project or Document")
        if kind == "document":
            self._require_documents_option(req, "given a file")
        item_id = req.form.get("itemId")
        if item_id is None:
            raise req.block("itemId is required")
        self._require_created(req, kind, item_id, "itemId")

    @_rule("POST /v1/time-entries", "TicketId (or TaskId) run-created; UserId is the operator user")
    def _post_time_entry(self, req: _Req) -> None:
        assert req.body is not None
        ticket, task = req.body.get("TicketId"), req.body.get("TaskId")
        if ticket is None and task is None:
            raise req.block("TicketId or TaskId is required")
        if ticket is not None:
            self._body_created(req, "ticket", "TicketId", ticket)
        if task is not None:
            self._body_created(req, "task", "TaskId", task)
        if self._scalar_int(req, "UserId") != self.operator_user:
            raise req.block(f"UserId is required and must be the operator user {self.operator_user}")

    @_rule(
        "POST /v1/assets/custom",
        "only with allow_custom_assets (never from a cleanup); ClientId is the test client; Name starts with the run id",
    )
    def _post_custom_asset(self, req: _Req) -> None:
        if not self.allow_custom_assets or self.cleanup:
            raise req.block(
                "custom assets cannot be created unless the run enabled the custom_assets area (--with-custom-assets); "
                "this harness never deletes a custom asset (the spec's soft DELETE is not used), so it would stay on the test "
                "client"
            )
        self._require_test_client(req, self._scalar_int(req, "ClientId"), required=True)
        self._require_run_name(req)

    @_rule(
        "PATCH /v1/assets/custom/{customAssetId}",
        "only with allow_custom_assets (never from a cleanup); ClientId, if present, is the test client; the second client (a MOVE "
        "of the asset) only with allow_custom_asset_move, with a LocationId in the same body, no ContactId but null and the "
        "move announced in the manifest (details client_id names that client); Name, if present, starts with the run id",
    )
    def _patch_custom_asset(self, req: _Req) -> None:
        # the path id was already checked to be a custom asset this run created (_PATH_KINDS)
        if not self.allow_custom_assets or self.cleanup:
            raise req.block("custom assets cannot be changed unless the run enabled the custom_assets area (--with-custom-assets)")
        client = self._scalar_int(req, "ClientId")  # absent or null: the asset's client is not named (nothing moves)
        if client is not None and client != self.test_client:
            self._check_asset_move(req, client)
        self._require_run_name(req, required=False)

    def _check_asset_move(self, req: _Req, client: int) -> None:
        """A PATCH of a custom asset that names a client other than the test client = a MOVE of the asset (since contract
        a7954b24844b a new ClientId moves it). Only to the second client, only with allow_custom_asset_move, only with a LocationId
        of the new client in the same body (Gorelo needs it and the tool insists on it), no contact but a clear, and only for a
        move the area announced first: the manifest details of the asset must already name that client, so a move that was not
        written down before it was sent can never happen."""
        assert req.body is not None and self.manifest is not None
        if client not in self.allowed_clients:
            raise req.block(f"ClientId must be {self.test_client} (the test client), not {client}")
        if not self.allow_custom_asset_move:
            raise req.block(
                f"ClientId {client} would MOVE the custom asset to the second client; a move needs the option allow_custom_asset_move "
                "(write_matrix --with-asset-move). Only the test client may be named otherwise"
            )
        if self._scalar_int(req, "LocationId") is None:
            raise req.block(f"a move to client {client} needs a LocationId of that client in the same body (a JSON integer)")
        if req.body.get("ContactId") is not None:
            raise req.block("a move never assigns a contact: ContactId must be absent or null")
        asset = normalize_id("custom_asset", req.params["customAssetId"])
        recorded = self.manifest.details("custom_asset", asset).get("client_id")
        if recorded != client:
            raise req.block(
                f"the move of custom asset {asset} to client {client} was not announced: the manifest details client_id must name "
                "that client before the PATCH is sent (update_details), so a crash leaves the residue text true"
            )

    def _require_run_name(self, req: _Req, *, required: bool = True) -> None:
        assert req.body is not None and self.manifest is not None
        name = req.body.get("Name")
        if name is None:
            if required:
                raise req.block(f"Name is required and must start with the run id {self.manifest.run_id}")
            return
        if not isinstance(name, str) or not name.startswith(self.manifest.run_id):
            raise req.block(f"Name must start with the run id {self.manifest.run_id}")

    # -- documents -----------------------------------------------------------------

    def _require_documents_option(self, req: _Req, what: str) -> None:
        """Every document write but the archive of a cleanup needs allow_documents, and a cleanup never has it."""
        if not self.allow_documents or self.cleanup:
            raise req.block(
                f"a document cannot be {what} unless the run enabled the documents area (--with-documents), and never from a "
                "cleanup; a document cannot be deleted through the API, so what a run creates stays in Gorelo"
            )

    def _require_run_title(self, req: _Req, field: str, value: Any) -> None:
        assert self.manifest is not None
        if not isinstance(value, str) or not value.startswith(self.manifest.run_id):
            raise req.block(f"{field} must be text that starts with the run id {self.manifest.run_id}")

    @staticmethod
    def _require_no_data_image(req: _Req, field: str, value: Any) -> None:
        if not isinstance(value, str):
            raise req.block(f"{field} must be text")
        if _DATA_IMAGE.search(value):
            raise req.block(f"{field} embeds an image as a data: URL, which Gorelo does not accept")

    @_rule(
        "POST /v1/documents",
        "only with allow_documents (never from a cleanup); Title starts with the run id; exactly one place: ClientId the test "
        "client, or ParentDocumentId a run-created document with no ClientId; an internal document is refused; Body embeds no "
        "data: image",
    )
    def _post_document(self, req: _Req) -> None:
        assert req.body is not None
        body = req.body
        self._require_documents_option(req, "created")
        self._require_run_title(req, "Title", body.get("Title"))
        if "Body" in body and body["Body"] is not None:
            self._require_no_data_image(req, "Body", body["Body"])
        parent, client = body.get("ParentDocumentId"), body.get("ClientId")
        if parent is not None:
            self._body_created(req, "document", "ParentDocumentId", parent)
            if client is not None:
                raise req.block("ClientId must be absent under a parent: the document takes the parent's client")
            return
        if client is None:
            raise req.block(
                f"an INTERNAL document (no ClientId and no ParentDocumentId) is refused: ClientId must be {self.test_client} "
                "(the test client) or ParentDocumentId a document this run created"
            )
        self._require_test_client(req, self._json_int(req, "ClientId", client), required=True)

    @_rule(
        "PATCH /v1/documents/{documentId}",
        "only with allow_documents, but a cleanup may archive (the body exactly {StatusId: 2}); Title starts with the run id; "
        "StatusId 1 or 2; PublicLinkEnabled and PublishToPortalEnabled only false, never true; Attachments only files this run "
        "uploaded to that document; Move only under a run-created document or to the top level of the test client, never "
        "internal",
    )
    def _patch_document(self, req: _Req) -> None:
        assert req.body is not None
        body = req.body
        if self.cleanup:  # the one thing a cleanup does to a document: archive what a stopped run left
            if set(body) != {"StatusId"} or not _is_status(body["StatusId"], DOCUMENT_ARCHIVED):
                raise req.block("a cleanup only archives a document (the body {\"StatusId\": 2}); it changes nothing else")
            return
        self._require_documents_option(req, "changed")
        if not body:
            raise req.block("the body names nothing to change")
        for field in ("PublicLinkEnabled", "PublishToPortalEnabled"):
            if field in body and body[field] is not False:
                raise req.block(
                    f"{field} may only be false: the live harness never turns sharing on (a public link or the client portal makes "
                    "the document readable outside the MSP)"
                )
        if "Title" in body:
            self._require_run_title(req, "Title", body["Title"])
        if "Body" in body:
            self._require_no_data_image(req, "Body", body["Body"])
        if "AlternateName" in body:
            name = body["AlternateName"]
            if name != "":
                self._require_run_title(req, "AlternateName", name)
        if "IsStarred" in body and not isinstance(body["IsStarred"], bool):
            raise req.block("IsStarred must be true or false")
        if "StatusId" in body and not (
            _is_status(body["StatusId"], DOCUMENT_ACTIVE) or _is_status(body["StatusId"], DOCUMENT_ARCHIVED)
        ):
            raise req.block(f"StatusId must be exactly the JSON integer 1 (Active) or 2 (Archived), not {_json_kind(body['StatusId'])}")
        if "Attachments" in body:
            self._check_document_attachments(req, body["Attachments"])
        if "Move" in body:
            self._check_document_move(req, body["Move"])

    def _check_document_attachments(self, req: _Req, entries: Any) -> None:
        """Attachments replaces the whole list: every entry must be a file THIS RUN uploaded to THIS document (the manifest records each
        upload as an attachment with details document_id); [] removes them all."""
        assert self.manifest is not None
        if not isinstance(entries, list):
            raise req.block("Attachments must be a list")
        document = normalize_id("document", req.params["documentId"])
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) != {"Name", "Url"}:
                raise req.block(f"Attachments[{index}] must be an object with Name and Url and nothing else")
            name, url = entry["Name"], entry["Url"]
            if not isinstance(name, str) or not name.strip() or not isinstance(url, str) or not url.strip():
                raise req.block(f"Attachments[{index}] needs a Name and a Url, both text")
            try:
                key = normalize_id("attachment", name)
            except ValueError:
                raise req.block(f"Attachments[{index}].Name is not a name this run recorded for an upload") from None
            if key not in self._ids("attachment"):
                raise req.block(
                    f"Attachments[{index}].Name is not a file this run ({self.manifest.run_id}) uploaded: only files the run "
                    "uploaded to this document may be attached"
                )
            parent = self.manifest.details("attachment", key).get("document_id")
            try:
                same = normalize_id("document", parent) == document
            except ValueError:
                same = False
            if not same:
                raise req.block(f"Attachments[{index}].Name was uploaded for another record than this document")

    def _check_document_move(self, req: _Req, move: Any) -> None:
        """Move: under a run-created document (then no ClientId) or to the top level of the TEST client. Never internal, never a
        document under itself, never another client."""
        if not isinstance(move, dict) or not move or not set(move) <= {"ParentDocumentId", "ClientId"}:
            raise req.block("Move must be an object with ParentDocumentId and/or ClientId and nothing else")
        parent = move.get("ParentDocumentId")
        if parent is not None:
            key = self._body_created(req, "document", "Move.ParentDocumentId", parent)
            if key == normalize_id("document", req.params["documentId"]):
                raise req.block("a document cannot be moved under itself")
            if move.get("ClientId") is not None:
                raise req.block("Move.ClientId must be absent under a parent: the document takes the parent's client")
            return
        if "ParentDocumentId" not in move:
            raise req.block("Move to the top level needs ParentDocumentId: null next to the ClientId")
        client = move.get("ClientId")
        if client is None:
            raise req.block(f"an INTERNAL move (ClientId null) is refused: the top level of client {self.test_client} (the test client) only")
        self._require_test_client(req, self._json_int(req, "Move.ClientId", client), required=True)

    @_rule(
        "POST /v1/documents/{documentId}/duplicate",
        "only with allow_documents (never from a cleanup); the source is a run-created document; Title, if present, starts with the "
        "run id; the body may be left out",
    )
    def _post_document_duplicate(self, req: _Req) -> None:
        assert req.body is not None
        self._require_documents_option(req, "duplicated")
        if "Title" in req.body and req.body["Title"] is not None:
            self._require_run_title(req, "Title", req.body["Title"])

    @_rule(
        "POST /v1/documents/{documentId}/related-items",
        "only with allow_documents (never from a cleanup); RelatedItems of 1 to 100 entries, each ItemTypeId exactly 1 (Document) "
        "with an ItemId that is a run-created document",
    )
    def _post_document_related_items(self, req: _Req) -> None:
        assert req.body is not None
        self._require_documents_option(req, "linked to other records")
        items = req.body.get("RelatedItems")
        if not isinstance(items, list) or not items:
            raise req.block("RelatedItems must be a list of 1 to 100 entries")
        if len(items) > 100:
            raise req.block(f"RelatedItems holds {len(items)} entries; at most 100 per call")
        for index, entry in enumerate(items):
            if not isinstance(entry, dict) or set(entry) != {"ItemId", "ItemTypeId"}:
                raise req.block(f"RelatedItems[{index}] must be an object with ItemId and ItemTypeId and nothing else")
            if not _is_status(entry["ItemTypeId"], 1):
                raise req.block(
                    f"RelatedItems[{index}].ItemTypeId must be exactly 1 (a Document): the live harness never links a client's "
                    "asset, device, contact or password"
                )
            self._body_created(req, "document", f"RelatedItems[{index}].ItemId", entry["ItemId"])

    @_rule(
        "DELETE /v1/documents/{documentId}/related-items/{relatedItemId}",
        "only with allow_documents (never from a cleanup); a run-created document; relatedItemId a whole number",
    )
    def _delete_document_related_item(self, req: _Req) -> None:
        self._require_documents_option(req, "unlinked from another record")
        link = req.params["relatedItemId"]
        if not _DIGITS_ONLY.fullmatch(link) or int(link) < 1:
            raise req.block("relatedItemId must be a whole number (the Id of a related item)")

    @_rule(
        "POST /v1/documents/{documentId}/versions/{versionId}/restore",
        "only with allow_documents (never from a cleanup); a run-created document; versionId a UUID",
    )
    def _post_document_version_restore(self, req: _Req) -> None:
        self._require_documents_option(req, "restored to an earlier version")
        if not _UUID.fullmatch(req.params["versionId"]):
            raise req.block("versionId must be a UUID")

    @_rule("POST /v1/items", "ClientId is the test client")
    def _post_item(self, req: _Req) -> None:
        self._require_test_client(req, self._scalar_int(req, "ClientId"), required=True)

    @_rule("PATCH /v1/items/{itemId}", "ClientId, if present, is the test client")
    def _patch_item(self, req: _Req) -> None:
        self._require_test_client(req, self._scalar_int(req, "ClientId"), required=False)

    @_rule(
        "POST /v1/invoices",
        "ClientId is the test client; StatusId is exactly 1 (a Draft: 5, Approved, pushes the invoice to the accounting system); "
        "RecipientEmails absent, null or empty; LineItems a non-empty list of objects, each ItemId a UUID string",
    )
    def _post_invoice(self, req: _Req) -> None:
        assert req.body is not None
        body = req.body
        self._require_test_client(req, self._scalar_int(req, "ClientId"), required=True)
        status = body.get("StatusId")
        if status is None:  # absent or null: Gorelo would pick the status, which could be anything
            raise req.block(
                "StatusId is required and must be the JSON integer 1 (Draft); without it Gorelo would pick the status itself"
            )
        if _is_status(status, INVOICE_APPROVED):
            if not self.allow_approved_invoice:
                raise req.block(
                    "StatusId 5 (Approved) is not allowed: an Approved invoice is pushed to the connected accounting "
                    "system, so the live harness creates Drafts only (StatusId 1)"
                )
            self._check_approved_invoice(req)
            return
        if isinstance(status, bool) or not isinstance(status, int) or status != INVOICE_DRAFT:
            allowed = "the JSON integer 1 (Draft)" + (" or 5 (Approved)" if self.allow_approved_invoice else "")
            raise req.block(f"StatusId must be exactly {allowed}, not {_json_kind(status)}")
        recipients = body.get("RecipientEmails")
        if recipients is not None and not (isinstance(recipients, list) and not recipients):
            raise req.block(
                "RecipientEmails must be absent, null or empty: the live harness never emails an invoice, "
                "not even to the operator"
            )
        lines = body.get("LineItems")
        if not isinstance(lines, list) or not lines:
            raise req.block("LineItems must be a non-empty list of line objects")
        for index, line in enumerate(lines):
            if not isinstance(line, dict):
                raise req.block(f"LineItems[{index}] must be an object")
            item = line.get("ItemId")
            if not isinstance(item, str) or not _UUID.fullmatch(item):
                raise req.block(f"LineItems[{index}].ItemId must be a UUID string")

    def _check_approved_invoice(self, req: _Req) -> None:
        """The one Approved invoice a run may create (allow_approved_invoice), by the rules of "The approved invoice" in
        the module docstring. ClientId the test client and the rules every body passes were checked before this. Raises
        GuardViolation (via req.block) for the first rule that does not hold; when none is broken it counts the create,
        so a second one is refused."""
        assert req.body is not None and self.manifest is not None
        body = req.body
        if self.cleanup:
            raise req.block("a cleanup never creates an invoice, so it never creates an Approved one")
        if "RecipientEmails" in body:
            raise req.block(
                "RecipientEmails must be absent (no key at all, not null, not empty) from an Approved invoice: the live "
                "harness never emails an invoice, not even to the operator"
            )
        lines = body.get("LineItems")
        if not isinstance(lines, list) or len(lines) != 1:
            raise req.block("an Approved invoice must have exactly one line item (LineItems with a single line object)")
        line = lines[0]
        if not isinstance(line, dict):
            raise req.block("LineItems[0] must be an object")
        item = line.get("ItemId")
        if not isinstance(item, str) or not _UUID.fullmatch(item):
            raise req.block("LineItems[0].ItemId must be a UUID string")
        quantity, price = line.get("Quantity"), line.get("UnitPrice")
        if not _positive_number(quantity):
            raise req.block("LineItems[0].Quantity must be a number above 0")
        if not _positive_number(price):
            raise req.block(
                "LineItems[0].UnitPrice must be given explicitly and be a number above 0: a line that falls back to the "
                "item's own price, or bills nothing, could bring the total to exactly 0, which Gorelo records as Paid, "
                "and a Paid invoice can be neither deleted nor voided"
            )
        if "DiscountPercent" in line:
            raise req.block(
                "LineItems[0].DiscountPercent must be absent from an Approved invoice: a discount could bring the "
                "total to 0"
            )
        if "BillableStatusId" in line and not _is_status(line["BillableStatusId"], 1):
            raise req.block(
                "LineItems[0].BillableStatusId must be absent or exactly the JSON integer 1 (Billable): a line that is "
                "not billable could bring the total to 0"
            )
        if "TaxId" not in line or line["TaxId"] is not None:
            raise req.block(
                "LineItems[0].TaxId must be given explicitly as null (the line's no_tax): an omitted TaxId falls back to "
                "the item's own tax and a number picks a tax, but the harness limits the one Approved invoice to $1 with no tax"
            )
        if quantity * price > APPROVED_INVOICE_LIMIT:
            raise req.block(
                f"the line bills more than {APPROVED_INVOICE_LIMIT:g}: the harness limits the one Approved invoice to that "
                "size"
            )
        reference = body.get("Reference")
        announced = [
            intent
            for intent in self.manifest.unresolved_intents()
            if intent.kind == "invoice" and _is_status(intent.details.get("status_id"), INVOICE_APPROVED)
        ]
        if not announced:
            raise req.block(
                "no open intent of kind 'invoice' with details status_id 5: call manifest.intent('invoice', label, "
                "{'status_id': 5}) before creating the Approved invoice"
            )
        if len(announced) > 1:
            raise req.block(
                f"{len(announced)} open invoice intents hold status_id 5: the guard allows one Approved invoice, so "
                "settle the others first"
            )
        if not isinstance(reference, str) or reference != announced[0].label:
            raise req.block(
                "Reference must be exactly the label of the open invoice intent with details status_id 5 (the label the "
                "manifest announced), so the invoice can be found again by it"
            )
        if self._approved_creates:
            raise req.block(
                "this run already created an Approved invoice: the guard allows one (no other approved invoice may be "
                "created in this run)"
            )
        earlier = [
            record
            for record in self.manifest.all_created()
            if record.kind == "invoice" and not _is_status(record.details.get("status_id"), INVOICE_DRAFT)
        ]
        if earlier:
            raise req.block(
                f"the manifest already holds {len(earlier)} invoice record(s) that are not a Draft: no other approved "
                "invoice may be created in this run"
            )
        self._approved_creates += 1  # counted only now that every rule held: the request is allowed

    @_rule(
        "DELETE /v1/invoices/{invoiceId}",
        "its manifest details hold status_id 1 (created as a Draft). Gorelo's DELETE: Draft: deleted (no longer "
        "listed). Approved: voided (status Void, still listed), which the live harness does only with "
        "allow_approved_invoice",
    )
    def _delete_invoice(self, req: _Req) -> None:
        key = normalize_id("invoice", req.params["invoiceId"])  # _PATH_KINDS already required it to be run-created
        problem = self._draft_problem(key)
        if problem is None:
            return
        if self.allow_approved_invoice:
            assert self.manifest is not None
            if _is_status(self.manifest.details("invoice", key).get("status_id"), INVOICE_APPROVED):
                return  # recorded as Approved: the DELETE voids it (StatusId 4, still listed)
            raise req.block(
                f"{problem}; the live harness deletes only a Draft and voids only an invoice recorded as Approved "
                "(status_id 5), never one that is Paid (3), Void (4) or has no recorded status"
            )
        raise req.block(
            f"{problem}; the live harness deletes only a Draft, because Gorelo voids an Approved invoice instead "
            "of deleting it"
        )

    @_rule("POST /v1/uptime", "ClientId is the test client; Target.Url on the probe domain only, no Target.Ip")
    def _post_uptime(self, req: _Req) -> None:
        self._require_test_client(req, self._scalar_int(req, "ClientId"), required=True)
        self._check_probe_target(req)

    @_rule("PATCH /v1/uptime/{checkId}", "ClientId, if present, is the test client; Target.Url on the probe domain only, no Target.Ip")
    def _patch_uptime(self, req: _Req) -> None:
        self._require_test_client(req, self._scalar_int(req, "ClientId"), required=False)
        self._check_probe_target(req)

    def _check_probe_target(self, req: _Req) -> None:
        """An uptime check probes its target every minute: only hosts on the probe domains (the probe domain)."""
        assert req.body is not None
        target = req.body.get("Target")
        if target is None:
            return
        if not isinstance(target, dict):
            raise req.block("Target must be an object")
        if target.get("Ip") not in (None, ""):
            raise req.block("Target.Ip is not allowed: a check may only probe a URL on " + ", ".join(sorted(self.probe_domains)))
        url = target.get("Url")
        if url is None:
            return
        host = None
        if isinstance(url, str) and url.isascii() and not _UNSAFE_URL.search(url):
            try:
                parts = urlsplit(url)
                host = parts.hostname if parts.scheme in ("http", "https") else None
            except ValueError:
                host = None
        if host is None or not any(host == d or host.endswith("." + d) for d in self.probe_domains):
            raise req.block("Target.Url must be a http(s) URL on " + ", ".join(sorted(self.probe_domains)) + " or a subdomain")

    @_rule("POST /v1/projects", "ClientId is the test client")
    def _post_project(self, req: _Req) -> None:
        self._require_test_client(req, self._scalar_int(req, "ClientId"), required=True)

    @_rule("PATCH /v1/projects/{projectId}", "ClientId, if present, is the test client")
    def _patch_project(self, req: _Req) -> None:
        self._require_test_client(req, self._scalar_int(req, "ClientId"), required=False)

    @_rule("POST /v1/projects/{projectId}/comments", "ConversationTypeId 2 (Private), no ConversationId")
    def _post_project_comment(self, req: _Req) -> None:
        kind = self._comment_type(req)
        if kind != CONVERSATION_PRIVATE:
            raise req.block("a project comment must be Private (ConversationTypeId 2); anything else is outward-facing")
        self._check_conversation(req, kind, parent="project", placeholder="projectId", parent_key="project_id")

    @_rule(
        "POST /v1/projects/{projectId}/tasks/{taskId}/comments",
        "ConversationTypeId 2 (Private); 3 (side conversation) and 4 (approval) only with a ConversationId of a side "
        "conversation or approval this run created on that task",
    )
    def _post_task_comment(self, req: _Req) -> None:
        kind = self._comment_type(req)
        if kind not in (CONVERSATION_PRIVATE, CONVERSATION_SIDE, CONVERSATION_APPROVAL):
            raise req.block("a task comment must be Private, side conversation or approval (ConversationTypeId 2, 3 or 4)")
        self._check_conversation(req, kind, parent="task", placeholder="taskId", parent_key="task_id")

    @_rule("POST /v1/forms/{formId}/submission-links", "TicketId or TaskId run-created")
    def _post_submission_link(self, req: _Req) -> None:
        assert req.body is not None
        ticket, task = req.body.get("TicketId"), req.body.get("TaskId")
        if ticket is None and task is None:
            raise req.block("TicketId or TaskId is required")
        if ticket is not None:
            self._body_created(req, "ticket", "TicketId", ticket)
        if task is not None:
            self._body_created(req, "task", "TaskId", task)


# Import-time check (the offline tests also compare every key of these tables exactly with the spec): the tables that
# are looked up by the exact text of an operation must spell it alike, or a rule could silently protect nothing.
_check_one_spelling_per_operation(_PATH_KINDS, _HANDLER_NAMES, _HANDLER_TEXT, _SPECIAL_DELETES, _CREATES, _OPTIONAL_BODY)


# --------------------------------------------------------------------------
# The configured client ids must be the clients the site config names
# --------------------------------------------------------------------------
# Before any write (and before the read-only smoke run) a harness entry point reads GET /v1/clients/{id} for the test
# client and the second client and stops (SiteConfigError, which main() prints as "cannot start: ..." with exit 2) unless
# Gorelo's Name equals the configured name exactly. A config copied from another tenant therefore cannot write anywhere.


def configured_clients(cfg: SiteConfig) -> list[tuple[int, str]]:
    """(client id, expected name) of the test client and the second client."""
    return [(cfg.test_client, cfg.test_client_name), (cfg.second_client, cfg.second_client_name)]


@dataclass(frozen=True)
class ClientFacts:
    """What the check of the site clients learned about one client besides its name: its status (GET /v1/clients/{id}: Status
    {Id, Name}; 1 Active, 3 Credit hold, 4 Archived since contract a7954b24844b). Both are None when Gorelo's answer has no readable one."""

    status_id: int | None
    status_name: str | None

    @property
    def active(self) -> bool:
        """True only for status 1 (Active), matched by the id, never by the name."""
        return self.status_id == CLIENT_ACTIVE

    def shown(self) -> str:
        """The status for a message: its name and id when both are plain, else only what is known."""
        plain = isinstance(self.status_name, str) and re.fullmatch(r"[A-Za-z ]{1,30}", self.status_name) is not None
        if self.status_id is None:
            return "of a status that could not be read"
        return f"{self.status_name} (status {self.status_id})" if plain else f"in status {self.status_id}"


class ClientNotActive(SiteConfigError):
    """The test client is not Active (or its status could not be read), so a write run must not start. A SiteConfigError, so every
    entry point that prints "cannot start: ..." and exits 2 for a bad site setup does the same for this."""


def _client_facts(data: Any) -> ClientFacts:
    status = data.get("Status") if isinstance(data, dict) else None
    ident = status.get("Id") if isinstance(status, dict) else None
    name = status.get("Name") if isinstance(status, dict) else None
    return ClientFacts(
        status_id=ident if isinstance(ident, int) and not isinstance(ident, bool) else None,
        status_name=name if isinstance(name, str) else None,
    )


def require_active_test_client(facts: Mapping[int, ClientFacts] | None, cfg: SiteConfig | None = None) -> None:
    """Raise ClientNotActive unless the test client is Active (status 1). `facts` is what verify_site_clients returned.

    A write run (the write matrix) calls this right after the name check and before it creates a manifest, so an archived test client
    stops it with nothing sent but the two client reads. The status is never changed by the harness: the operator makes the client
    Active again in the Gorelo app.
    A status that was not read (facts missing or without the client) is a refusal too: fail closed. A cleanup never calls this, because
    it has to be able to clean up whatever a run left on a client in any status."""
    cfg = cfg if cfg is not None else site()
    found = facts.get(cfg.test_client) if isinstance(facts, Mapping) else None
    if found is None:
        raise ClientNotActive(
            f"the status of the test client {cfg.test_client} was not read, so a write run did not start and nothing but the two "
            "client reads was sent; the operator has to check in the Gorelo app that the test client is Active"
        )
    if found.active:
        return
    raise ClientNotActive(
        f"the test client {cfg.test_client} is {found.shown()}, so a write run did not start and nothing but the two client reads "
        "was sent: it needs the test client Active (status 1). The operator has to make the test client Active again in the Gorelo app; "
        "this harness never changes the status of a client itself"
    )


async def verify_site_clients(
    api_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    cfg: SiteConfig | None = None,
    base_url: str = GORELO_BASE_URL,
) -> dict[int, ClientFacts]:
    """Raise SiteConfigError unless every configured client exists in Gorelo under exactly its configured name; return each client's
    ClientFacts (its status), so a write run can refuse an archived test client (require_active_test_client) from the same read.
    The names are read from `base_url` (the host of the run; default the US host), after the same check as api_hosts (ValueError
    for a URL outside gorelo.io: the key must not go there)."""
    cfg = cfg if cfg is not None else site()
    facts: dict[int, ClientFacts] = {}
    async with httpx.AsyncClient(
        base_url=_checked_base_url(base_url),
        headers={"X-API-Key": api_key, "Accept": "application/json"},
        timeout=30.0,
        transport=transport,
    ) as http:
        for client_id, wanted in configured_clients(cfg):
            try:
                response = await http.get(f"/clients/{client_id}")
            except httpx.HTTPError as exc:
                raise SiteConfigError(f"cannot check client {client_id}: the request failed ({type(exc).__name__})") from None
            if response.status_code != 200:
                raise SiteConfigError(f"cannot check client {client_id}: Gorelo answered HTTP {response.status_code}")
            try:
                payload: Any = response.json()
            except ValueError:
                payload = None
            data = payload.get("Data") if isinstance(payload, dict) else None
            found = data.get("Name") if isinstance(data, dict) else None
            if found != wanted:
                shown = found if isinstance(found, str) else "(no name)"
                raise SiteConfigError(f"client {client_id} is named {shown}, site config says {wanted}")
            facts[client_id] = _client_facts(data)
    return facts
