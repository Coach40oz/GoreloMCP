"""scripts/live/write_matrix.py: the live write matrix, run offline against a stateful fake Gorelo.

The fake (FakeGorelo) sits behind httpx.MockTransport and answers the envelopes the tools expect for the whole
flow: ids are handed out and remembered, PATCH /v1/clients/{clientId} is a partial update and PATCH /v1/contacts/{contactId}
replaces the contact (the id is in the path: the collection forms answer 405 live), a ticket's BillingOverride resets
the parts a PATCH leaves out, a time entry can answer Reopened, projects and forms can answer the missing-scope 403.
It also behaves like production where live runs (2026-10-02) found the published spec wrong: a technician
cannot be lead or assisting and watcher of one ticket ("Technician already exists"), a maintenance window needs its
StartDateTime and an approver needs the approver contact tag. Time entries are served in the PUBLISHED shape by default
(User {Id, Name}, Ticket {Id, Number, Title}, Task null for a ticket entry), which production answers again since the
probe of 2026-10-03; on 2026-10-02 it answered the flat ids UserId, TicketId and TaskId instead, which a test serves by
reshaping the entry reads (make_flat).
A test can also make the fake STORE a ticket with a contact or CCs the request never carried, or an approval with
other approvers: the cases where the matrix must stop before it emails anybody.
For invoices the fake keeps a catalog (products of another client, of nobody and of the test client), numbers the invoices,
answers the dates as plain calendar dates like production, deletes a Draft (answer StatusId 6) and voids an Approved
invoice (StatusId 4; deleting an invoice that is already void is a success, as the spec says), counts the PDF export
events, and lets a test change the STORED invoice (a Reference, a status, no Number) or make the create answer get lost
after the invoice was made. An Approved invoice whose total is exactly 0 is created as Paid (3, which cannot be deleted
or voided: a 409), and an Approved invoice reaches the accounting system: its ExternalId (and a payment link) appear
after as many reads as the test asks for (none by default: it is set at once), or never.
For the approved invoice area it also lists the contacts of the test client (all Inactive by default, a test may
add an Active one) and gives every location a BillingContactIds, the text "[]" by default (production's answer for none). An invoice carries the published totals
(SubTotal, TotalDiscount, TotalTax 0.0 and Total), and a test may put a tax on the STORED invoice with on_invoice_created.
The REAL LiveGuard (write mode, require_intents=True) and the real cleanup run in front of it, so a create without
an intent, a request outside the allowlist or a record that is not cleaned up fails these tests like it would live.
Nothing touches the network or the app's .env.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import inspect
import itertools
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import TEST_API_KEY, MockGorelo, envelope, error_envelope, paged_envelope
from site_helper import site_config_env  # noqa: F401  (autouse: points GORELO_SITE_CONFIG at invented values)

from gorelo_client import GoreloAPIError, GoreloClient
from scripts.live import _env, cleanup, write_matrix
from scripts.live.guard import _CREATES, GuardViolation, LiveGuard
from scripts.live.manifest import Manifest, Record
from spec import load_spec_index
from tools._common import format_gorelo_error

pytestmark = pytest.mark.anyio

RUN_START = datetime(2099, 10, 2, 10, 15, 0, tzinfo=timezone.utc)
OPERATOR_EMAIL = "ops@example.com"  # [operator] email of the invented test config (tests/site_helper.py)
PROBE_URL = "https://mcp.example.net/.well-known/oauth-authorization-server"
RUN = "MCPTEST-20991002101500"
TEST_CLIENT, SECOND, OPERATOR_CONTACT, OPERATOR_USER = 9501, 9502, 9600, 9700
SPEC = load_spec_index()

STATUS_NAMES = {1: "New", 2: "In Progress", 3: "Solved", 4: "Closed", 6: "On Hold"}
TYPE_NAMES = {7101: "Incident", 7102: "Request"}
CONTACT_FIELDS = (
    "FirstName", "LastName", "ClientId", "LocationId", "PrimaryEmail", "MobilePhone", "MobilePhoneCountryCode",
    "OfficePhone", "OfficePhoneCountryCode", "JobTitle", "Department", "TimeZone", "Description",
)


@pytest.fixture(autouse=True)
def _never_read_the_real_env(monkeypatch, tmp_path):
    """Whatever a test does, the live service's .env is not the file that gets opened."""
    monkeypatch.setattr(_env, "ENV_FILE", tmp_path / "no-such-dir" / ".env")


def now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def op_key(method: str, path: str) -> str:
    """The spec operation key of a request, for example 'PATCH /v1/tickets/{ticketId}'."""
    parts = path.strip("/").split("/")
    best = None
    for key, op in SPEC.ops.items():
        if op.method != method:
            continue
        pattern = op.path.strip("/").split("/")
        if len(pattern) != len(parts):
            continue
        literals = 0
        for expected, actual in zip(pattern, parts):
            if expected.startswith("{"):
                continue
            if expected != actual:
                break
            literals += 1
        else:
            if best is None or literals > best[0]:
                best = (literals, key)
    return best[1] if best else f"{method} {path}"


def catalog_guid(n: int) -> str:
    return f"{n:08d}-cccc-4ddd-8eee-{n:012d}"


def invoice_guid(n: int) -> str:
    return f"{n:08d}-bbbb-4ccc-8ddd-{n:012d}"


def project_type_guid(n: int) -> str:
    return f"{n:08d}-dddd-4eee-8fff-{n:012d}"


def catalog_item(n: int, name: str, client, **extra) -> dict:
    """A row of GET /v1/items: an active product that belongs to `client` (None: to nobody)."""
    return {
        "Id": catalog_guid(n), "Name": name, "ClientId": client, "Type": {"Id": 1, "Name": "Product"},
        "Status": {"Id": 1, "Name": "Active"}, "UnitPrice": 50.0, "UnitCost": 20.0, **extra,
    }


def make_test_client_contact(n: int, status: dict | None, **extra) -> dict:
    """A row of GET /v1/contacts for the test client: an MCPTEST contact at an example.invalid address with this status."""
    row = {
        "Id": 9100 + n, "FirstName": f"MCPTEST-2099100{n}", "LastName": "Contact", "ClientId": TEST_CLIENT,
        "PrimaryEmail": f"mcptest-{n}@example.invalid", "Status": status, **extra,
    }
    return {key: value for key, value in row.items() if value is not DROP}


def inactive_test_client_contacts(count: int = 4) -> list[dict]:
    """Contacts of a test client whose contacts are all Inactive (so nobody can be emailed)."""
    return [make_test_client_contact(n, {"Id": 2, "Name": "Inactive"}) for n in range(1, count + 1)]


DROP = object()  # a key a row does not have

# the catalog of a tenant: a product of another client first, then one of nobody, then one of the test client
OTHER_CLIENTS_PRODUCT, GLOBAL_PRODUCT, TEST_PRODUCT = catalog_guid(901), catalog_guid(902), catalog_guid(903)
DEFAULT_CATALOG = (
    catalog_item(901, "Another client's product", SECOND),
    catalog_item(902, "Managed service", None),
    catalog_item(903, "the test client product", TEST_CLIENT),
)
DEFAULT_PROJECT_TYPES = (
    {"Id": project_type_guid(1), "Name": "Implementation", "Description": "x"},
    {"Id": project_type_guid(2), "Name": "Migration", "Description": "y"},
)
PDF_BYTES = b"%PDF-1.7 fake invoice"


# --------------------------------------------------------------------------
# The fake
# --------------------------------------------------------------------------


class FakeGorelo:
    """A stateful fake of the parts of the Gorelo API the write matrix talks to.

    Knobs: project_scope and forms_scope (False answers 403 code 080203), approver (False rejects every approver),
    stored_approvers (the approvers an approval is STORED with, whatever contacts were asked for), reopens (how many
    delete_time_entry calls answer Reopened first), other_tickets (tickets of the test client that exist before the run),
    oldest_first (list tickets oldest first, so a big client hides the run's tickets behind the first 500),
    lookup_lag (searches by text that come back empty before the new ticket is found), status_names (the tenant's
    ticket statuses), catalog (the rows of GET /v1/items), project_types (the rows of GET /v1/projects/types),
    sync_after (how many reads of an Approved invoice answer without ExternalId before it appears: 0, the default, sets it
    at once; None never sets it), payment_link (False: the synced invoice has no PaymentLink), test_client_contacts (the contact rows
    GET /v1/contacts answers for the test client besides the ones a run created: Inactive contacts by default).
    on_ticket_created(hook) lets a test change the STORED ticket after a POST /v1/tickets (a tenant automation that
    sets a contact or CCs the request never carried), and on_invoice_created(hook) does the same for the STORED invoice.
    There is deliberately no route for GET /v1/tickets/tags: the matrix applies no tenant tag, so such a request would
    be unmatched (and raise)."""

    def __init__(
        self,
        *,
        project_scope: bool = True,
        forms_scope: bool = True,
        approver: bool = True,
        stored_approvers: list | None = None,
        reopens: int = 0,
        other_tickets: int = 3,
        oldest_first: bool = False,
        lookup_lag: int = 0,
        status_names: dict[int, str] | None = None,
        catalog: list | None = None,
        project_types: list | None = None,
        sync_after: int | None = 0,
        payment_link: bool = True,
        test_client_contacts: list | None = None,
    ) -> None:
        self.status_names = dict(STATUS_NAMES if status_names is None else status_names)
        self.catalog = [dict(row) if isinstance(row, dict) else row for row in (DEFAULT_CATALOG if catalog is None else catalog)]
        self.project_types = [
            dict(row) if isinstance(row, dict) else row
            for row in (DEFAULT_PROJECT_TYPES if project_types is None else project_types)
        ]
        self.mock = MockGorelo()
        self.events: list[tuple] = []
        self.forced: dict[str, list] = {}
        self.handlers: dict[str, object] = {}  # "METHOD /v1/tickets/{ticketId}" -> handler; a test may replace one
        self.ticket_hooks: list = []
        self.invoice_hooks: list = []
        self.project_scope, self.forms_scope = project_scope, forms_scope
        self.approver, self.reopens, self.stored_approvers = approver, reopens, stored_approvers
        self.oldest_first, self.lookup_lag = oldest_first, lookup_lag
        self.sync_after, self.payment_link = sync_after, payment_link
        self.test_client_contacts = [
            dict(row) if isinstance(row, dict) else row for row in (inactive_test_client_contacts() if test_client_contacts is None else test_client_contacts)
        ]
        self._ids = itertools.count(1)
        self._numbers = itertools.count(3000)
        self._conversation_ids = itertools.count(700)
        self._entry_ids = itertools.count(5000)
        self._client_ids = itertools.count(8200)
        self._contact_ids = itertools.count(8301)
        self._invoice_ids = itertools.count(1)
        self._invoice_numbers = itertools.count(1042)
        self.deleted: set[tuple[str, object]] = set()
        self.clients = {
            TEST_CLIENT: {"Id": TEST_CLIENT, "Name": "the test client", "AlternateName": "the test client alt", "BillingName": "the test client"},
            SECOND: {"Id": SECOND, "Name": "Second", "AlternateName": "Second alt", "BillingName": "Second"},
        }
        self.locations = {
            TEST_CLIENT: [
                {
                    "Id": 9001, "ClientId": TEST_CLIENT, "Name": "the test client HQ", "IsDefault": True, "PhoneCountryCode": "US",
                    # the spec calls it comma separated ids, but production answers the TEXT of an empty JSON list when there
                    # are none (seen on a live location)
                    "BillingContactIds": "[]",
                }
            ],
            SECOND: [
                {
                    "Id": 9002, "ClientId": SECOND, "Name": "Second HQ", "IsDefault": True, "PhoneCountryCode": "US",
                    "BillingContactIds": "[]",
                }
            ],
        }
        self.contacts = {
            OPERATOR_CONTACT: self.contact_record(OPERATOR_CONTACT, {"ClientId": SECOND, "FirstName": "Operator", "LastName": "Person"})
        }
        self.tickets: dict[str, dict] = {}
        self.comments: dict[str, dict[str, dict]] = {}
        self.conversations: dict[str, list] = {}
        self.approvals: dict[str, dict] = {}
        self.time_entries: dict[int, dict] = {}
        self.items: dict[str, dict] = {}
        self.uptime: dict[str, dict] = {}
        self.projects: dict[str, dict] = {}
        self.sections: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.task_comments: dict[str, dict] = {}
        self.attachments: list[dict] = []
        self.invoices: dict[str, dict] = {}
        self.exports: list[str] = []  # the invoice of every PDF export event
        self.emailed: list = []  # every RecipientEmails entry a create body carried (the matrix sends none)
        for n in range(other_tickets):
            self.add_ticket({"Title": f"older ticket {n}", "ClientId": TEST_CLIENT, "StatusId": 1, "TypeId": 7101, "GroupId": 7201})
        self._register()

    # -- plumbing ----------------------------------------------------------

    def guid(self) -> str:
        n = next(self._ids)
        return f"{n:08d}-aaaa-4bbb-8ccc-{n:012d}"

    def part(self, request, index: int) -> str:
        return request.path.split("/")[index]

    def missing(self, what: str) -> dict:
        return error_envelope(404, [("070404", f"{what} not found")])

    def paged(self, rows: list, request) -> dict:
        size = int(request.query.get("PageSize", 50))
        cursor = request.query.get("Cursor")
        start = int(cursor[1:]) if cursor else 0
        following = f"c{start + size}" if start + size < len(rows) else None
        return paged_envelope(rows[start : start + size], next_cursor=following, total_count=len(rows))

    def fail(self, key: str, spec, times: int = 1) -> None:
        """Answer the next `times` requests for `key` ('PATCH /v1/tickets/{ticketId}') with `spec` instead."""
        self.forced.setdefault(key, []).extend([spec] * times)

    def route(self, method: str, template: str, handler) -> None:
        key = f"{method} {template}"
        self.handlers[key] = handler

        def serve(request):
            self.events.append(("request", request.method, request.path))
            queue = self.forced.get(key)
            if queue:
                return queue.pop(0)
            return self.handlers[key](request)

        self.mock.on(method, template, serve)

    def scoped(self, flag: str, handler):
        def serve(request):
            if not getattr(self, flag):
                scope = "Project" if flag == "project_scope" else "Forms"
                return error_envelope(403, [("080203", f"API key does not have '{scope}' scope")])
            return handler(request)

        return serve

    def requests(self) -> list[tuple[str, str]]:
        return [(r.method, op_key(r.method, r.path)) for r in self.mock.requests]

    def keys(self) -> list[str]:
        return [f"{r.method} {op_key(r.method, r.path).split(' ', 1)[1]}" for r in self.mock.requests]

    # -- records -----------------------------------------------------------

    def contact_record(self, ident: int, body: dict) -> dict:
        record = {key: body.get(key) for key in CONTACT_FIELDS}
        record.update({"Id": ident, "Alias": None, "IsTemporaryContact": False, "Status": {"Id": 1, "Name": "Active"}})
        return record

    def add_ticket(self, body: dict) -> dict:
        number = next(self._numbers)
        ident = self.guid()
        status = body["StatusId"]
        ticket = {
            "Id": ident,
            "Number": number,
            "DisplayNumber": f"TCK-{number}",
            "Title": body["Title"],
            "Description": body.get("Description"),
            "ClientId": body.get("ClientId"),
            "ContactId": body.get("ContactId"),
            "CcContactIds": list(body.get("CcContactIds") or []),
            "LocationId": body.get("LocationId"),
            "Status": {"Id": status, "Name": self.status_names.get(status)},
            "Type": {"Id": body["TypeId"], "Name": TYPE_NAMES.get(body["TypeId"])},
            "Priority": {"Id": body.get("PriorityId", 3), "Name": "Normal"},
            "Source": {"Id": body.get("SourceId", 6), "Name": "Api"},
            "GroupIds": [body["GroupId"]],
            "PrimaryGroupId": body["GroupId"],
            "LeadAssigneeId": body.get("LeadAssigneeId"),
            "AssistingAssigneeIds": [],
            "WatcherIds": [],
            "TagIds": list(body.get("TagIds") or []),
            "CreatedOn": body.get("CreatedOn") or now_text(),
            "UpdatedOn": now_text(),
            "ClosedOn": body.get("ClosedOn"),
            "BillingOverride": {"BillableStatus": None, "BillingRole": None, "ServiceLine": None, "WorkType": None},
        }
        self.tickets[ident] = ticket
        self.comments[ident] = {}
        self.conversations[ident] = []
        return ticket

    def live_ticket(self, request, index: int = 3):
        ident = self.part(request, index)
        if ("ticket", ident) in self.deleted:
            return None
        return self.tickets.get(ident)

    # -- routes ------------------------------------------------------------

    def _register(self) -> None:
        r = self.route
        r("GET", "/v1/clients/{clientId}", self.get_client)
        r("PATCH", "/v1/clients/{clientId}", self.patch_client)
        r("POST", "/v1/clients", self.post_client)
        r("DELETE", "/v1/clients/{clientId}", self.delete_client)
        r("GET", "/v1/clients/{clientId}/locations", lambda q: envelope(self.locations.get(int(self.part(q, 3)), [])))
        r("GET", "/v1/contacts", self.list_contacts)
        r("GET", "/v1/contacts/{contactId}", self.get_contact)
        r("POST", "/v1/contacts", self.post_contact)
        r("PATCH", "/v1/contacts/{contactId}", self.patch_contact)
        r("DELETE", "/v1/contacts/{contactId}", self.delete_contact)
        r("GET", "/v1/tickets/statuses", lambda q: envelope([{"Id": i, "Name": n} for i, n in self.status_names.items()]))
        r("GET", "/v1/tickets/types", lambda q: envelope([{"Id": i, "Name": n} for i, n in TYPE_NAMES.items()]))
        r("GET", "/v1/organization/groups", lambda q: envelope([{"Id": 7201, "Name": "Everyone"}, {"Id": 7202, "Name": "Admins"}]))
        r("GET", "/v1/billing-roles", lambda q: envelope([{"Id": 11, "Name": "Technician", "HourlyRate": 100.0}]))
        r("GET", "/v1/work-types", lambda q: envelope([{"Id": 21, "Name": "Standard", "HourlyMultiplier": 1.0}]))
        r("GET", "/v1/tickets", self.list_tickets)
        r("POST", "/v1/tickets", self.post_ticket)
        r("GET", "/v1/tickets/{ticketId}", self.get_ticket)
        r("PATCH", "/v1/tickets/{ticketId}", self.patch_ticket)
        r("DELETE", "/v1/tickets/{ticketId}", self.delete_ticket)
        r("GET", "/v1/tickets/{ticketId}/comments", self.list_comments)
        r("POST", "/v1/tickets/{ticketId}/comments", self.post_comment)
        r("GET", "/v1/tickets/{ticketId}/comments/{commentId}", self.get_comment)
        r("DELETE", "/v1/tickets/{ticketId}/comments/{commentId}", self.delete_comment)
        r("POST", "/v1/attachments", self.post_attachment)
        r("POST", "/v1/tickets/{ticketId}/conversations/side-conversation", self.post_side)
        r("POST", "/v1/tickets/{ticketId}/conversations/approval", self.post_approval)
        r("GET", "/v1/tickets/{ticketId}/approvals/{approvalId}", self.get_approval)
        r("POST", "/v1/time-entries", self.post_entry)
        r("GET", "/v1/time-entries/{timeEntryId}", self.get_entry)
        r("PATCH", "/v1/time-entries/{timeEntryId}", self.patch_entry)
        r("DELETE", "/v1/time-entries/{timeEntryId}", self.delete_entry)
        r("GET", "/v1/items", self.list_items)
        r("POST", "/v1/items", lambda q: self.post_thing(q, self.items, "Item"))
        r("GET", "/v1/items/{itemId}", lambda q: self.get_thing(q, self.items, "item"))
        r("PATCH", "/v1/items/{itemId}", lambda q: self.patch_thing(q, self.items, "item"))
        r("DELETE", "/v1/items/{itemId}", lambda q: self.delete_thing(q, self.items, "item"))
        r("GET", "/v1/invoices", self.list_invoices)
        r("POST", "/v1/invoices", self.post_invoice)
        r("GET", "/v1/invoices/{invoiceId}", self.get_invoice)
        r("GET", "/v1/invoices/{invoiceId}/pdf", self.get_invoice_pdf)
        r("DELETE", "/v1/invoices/{invoiceId}", self.delete_invoice)
        r("POST", "/v1/uptime", self.post_uptime)
        r("GET", "/v1/uptime/{checkId}", lambda q: self.get_thing(q, self.uptime, "uptime check"))
        r("PATCH", "/v1/uptime/{checkId}", self.patch_uptime)
        r("DELETE", "/v1/uptime/{checkId}", lambda q: self.delete_thing(q, self.uptime, "uptime check"))
        r("GET", "/v1/projects", self.scoped("project_scope", lambda q: self.paged(list(self.projects.values()), q)))
        r("GET", "/v1/forms", self.scoped("forms_scope", lambda q: self.paged([], q)))
        r("GET", "/v1/projects/types", self.scoped("project_scope", lambda q: envelope(self.project_types)))
        r("POST", "/v1/projects", self.scoped("project_scope", self.post_project))
        r("GET", "/v1/projects/{projectId}", lambda q: self.get_thing(q, self.projects, "project"))
        r("DELETE", "/v1/projects/{projectId}", lambda q: self.delete_thing(q, self.projects, "project"))
        r("POST", "/v1/projects/{projectId}/sections", self.post_section)
        r("POST", "/v1/projects/{projectId}/tasks", self.post_task)
        r("GET", "/v1/projects/{projectId}/tasks/{taskId}", lambda q: self.get_thing(q, self.tasks, "task", index=5))
        r("DELETE", "/v1/projects/{projectId}/tasks/{taskId}", lambda q: self.delete_thing(q, self.tasks, "task", index=5))
        r("POST", "/v1/projects/{projectId}/tasks/{taskId}/comments", self.post_task_comment)
        r("GET", "/v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}", lambda q: self.get_thing(q, self.task_comments, "comment", index=7))
        r("DELETE", "/v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}", lambda q: self.delete_thing(q, self.task_comments, "comment", index=7))

    # clients and contacts

    def get_client(self, q):
        client = self.clients.get(int(self.part(q, 3)))
        return envelope(client) if client else self.missing("client")

    def patch_client(self, q):
        """A partial update by the id in the path; the body has no Id (UpdateClientCommand, contract e15cb5a18ec2)."""
        client = self.clients.get(int(self.part(q, 3)))
        if client is None:
            return self.missing("client")
        for key in ("Name", "AlternateName", "BillingName"):
            if q.json.get(key):
                client[key] = q.json[key]
        return envelope(client)

    def post_client(self, q):
        location = q.json.get("Location", {})
        if location.get("Phone") and not re.fullmatch(r"[A-Z]{2}", location.get("PhoneCountryCode") or ""):
            return error_envelope(400, [("070101", "Missing or invalid default region", "Phone")])
        ident = next(self._client_ids)
        self.clients[ident] = {
            "Id": ident, "Name": q.json["Name"], "AlternateName": q.json.get("AlternateName") or "", "BillingName": q.json["Name"],
        }
        self.locations[ident] = [
            {
                "Id": 9500 + ident, "ClientId": ident, "Name": location.get("Name"), "IsDefault": True,
                "Phone": location.get("Phone"), "PhoneCountryCode": location.get("PhoneCountryCode"),
            }
        ]
        return envelope(self.clients[ident])

    def delete_client(self, q):
        ident = int(self.part(q, 3))
        self.deleted.add(("client", ident))
        return envelope({"Id": ident})

    def list_contacts(self, q):
        """GET /v1/contacts: the contacts of the run and the operator plus the rows a test put in `test_client_contacts`, filtered by
        ClientIds (a contact that was soft-deleted is not listed by the real API, and neither is one here)."""
        rows = [row for row in self.test_client_contacts]
        rows += [row for ident, row in self.contacts.items() if ("contact", ident) not in self.deleted]
        if "ClientIds" in q.query:
            wanted = {int(x) for x in q.query["ClientIds"].split(",")}
            rows = [row for row in rows if not isinstance(row, dict) or row.get("ClientId") in wanted]
        return self.paged(rows, q)

    def get_contact(self, q):
        contact = self.contacts.get(int(self.part(q, 3)))
        return envelope(contact) if contact else self.missing("contact")

    def post_contact(self, q):
        ident = next(self._contact_ids)
        self.contacts[ident] = self.contact_record(ident, q.json)
        return envelope(self.contacts[ident])

    def patch_contact(self, q):
        """A full-body command by the id in the path: without PrimaryEmail or ClientId it is a 400, and the body has no
        ContactId (UpdateContactCommand, contract e15cb5a18ec2)."""
        ident = int(self.part(q, 3))
        if ident not in self.contacts:
            return self.missing("contact")
        for required in ("PrimaryEmail", "ClientId"):
            if not q.json.get(required):
                return error_envelope(400, [("070101", f"{required} is required.", required)])
        self.contacts[ident] = self.contact_record(ident, q.json)  # PATCH REPLACES: whatever the body leaves out is gone
        return envelope(self.contacts[ident])

    def delete_contact(self, q):
        ident = int(self.part(q, 3))
        self.deleted.add(("contact", ident))
        return envelope({"Id": ident})

    # tickets

    def list_tickets(self, q):
        rows = list(self.tickets.values())
        rows = [t for t in rows if ("ticket", t["Id"]) not in self.deleted]
        if not self.oldest_first:
            rows.reverse()
        if "ClientIds" in q.query:
            wanted = {int(x) for x in q.query["ClientIds"].split(",")}
            rows = [t for t in rows if t["ClientId"] in wanted]
        text = q.query.get("Query")
        if text:
            if self.lookup_lag > 0:
                self.lookup_lag -= 1
                rows = []
            else:
                folded = text.casefold()
                rows = [
                    t for t in rows
                    if folded in t["Title"].casefold() or folded in str(t["Number"]) or folded in t["DisplayNumber"].casefold()
                ]
        return self.paged(rows, q)

    def on_ticket_created(self, hook) -> None:
        """hook(ticket) runs on the STORED ticket right after every POST /v1/tickets (it may change it in place)."""
        self.ticket_hooks.append(hook)

    def post_ticket(self, q):
        ticket = self.add_ticket(q.json)
        for hook in self.ticket_hooks:
            hook(ticket)
        return envelope({"Id": ticket["Id"]})

    def get_ticket(self, q):
        ticket = self.live_ticket(q)
        return envelope(ticket) if ticket else self.missing("ticket")

    def patch_ticket(self, q):
        ticket = self.live_ticket(q)
        if ticket is None:
            return self.missing("ticket")
        body = q.json
        if not body:
            return error_envelope(400, [("070101", "The request must contain at least one field to update.")])
        # live, 2026-10-02: one technician cannot be lead (or assisting) and watcher of a ticket at once
        lead = body["LeadAssigneeId"] if body.get("LeadAssigneeId") is not None else ticket["LeadAssigneeId"]
        assisting = body["AssistingAssigneeIds"] if body.get("AssistingAssigneeIds") is not None else ticket["AssistingAssigneeIds"]
        watchers = body["WatcherIds"] if body.get("WatcherIds") is not None else ticket["WatcherIds"]
        if {lead, *assisting} & set(watchers):
            return error_envelope(400, [("070101", "Technician already exists", "WatcherIds")])
        for key, value in body.items():
            if value is None:
                continue
            if key == "StatusId":
                ticket["Status"] = {"Id": value, "Name": self.status_names.get(value)}
            elif key == "PriorityId":
                ticket["Priority"] = {"Id": value, "Name": "Normal"}
            elif key == "TypeId":
                ticket["Type"] = {"Id": value, "Name": TYPE_NAMES.get(value)}
            elif key == "BillingOverride":
                parts = {"BillableStatus": "BillableStatusId", "BillingRole": "BillingRoleId", "ServiceLine": "ServiceLineId", "WorkType": "WorkTypeId"}
                # Gorelo's trap: the parts a PATCH leaves out are reset
                ticket["BillingOverride"] = {
                    name: ({"Id": value[field], "Name": f"{name} {value[field]}"} if value.get(field) else None)
                    for name, field in parts.items()
                }
            elif isinstance(value, list):
                ticket[key] = list(value)
            else:
                ticket[key] = value
        ticket["UpdatedOn"] = now_text()
        return envelope({"Id": ticket["Id"]})

    def delete_ticket(self, q):
        ident = self.part(q, 3)
        self.deleted.add(("ticket", ident))
        return envelope({"Id": ident})

    # comments, attachments, conversations

    def list_comments(self, q):
        ticket = self.live_ticket(q)
        if ticket is None:
            return self.missing("ticket")
        rows = list(self.comments[ticket["Id"]].values())
        if "ConversationType" in q.query:
            wanted = {int(x) for x in q.query["ConversationType"].split(",")}
            rows = [c for c in rows if c["ConversationType"]["Id"] in wanted]
        return self.paged(rows, q)

    def post_comment(self, q):
        ticket = self.live_ticket(q)
        if ticket is None:
            return self.missing("ticket")
        kind = q.json["ConversationTypeId"]
        names = {1: "Public", 2: "Private", 3: "Side Conversation", 4: "Approval"}
        ident = self.guid()
        self.comments[ticket["Id"]][ident] = {
            "Id": ident,
            "BodyHtml": q.json["Body"],
            "BodyText": q.json["Body"],
            "ConversationType": {"Id": kind, "Name": names[kind]},
            "ConversationId": q.json.get("ConversationId"),
            "Attachments": [{"Name": a["Name"], "Url": a["Url"]} for a in q.json.get("Attachments", [])],
            "EmailInfo": {"Status": {"Id": 1, "Name": "Sent"}, "ErrorDetail": None} if kind != 2 else {"Status": {"Id": 0, "Name": "None"}, "ErrorDetail": None},
            "CreatedOn": now_text(),
        }
        return envelope({"Id": ident})

    def get_comment(self, q):
        comment = self.comments.get(self.part(q, 3), {}).get(self.part(q, 5))
        return envelope(comment) if comment else self.missing("comment")

    def delete_comment(self, q):
        comment = self.comments.get(self.part(q, 3), {}).get(self.part(q, 5))
        if comment is None:
            return self.missing("comment")
        if comment["ConversationType"]["Id"] != 2:
            return error_envelope(409, [("070901", "ResourceNotDeletable: only private comments can be deleted")])
        self.deleted.add(("comment", comment["Id"]))
        return envelope({"Id": comment["Id"]})

    def post_attachment(self, q):
        name, content, _ = q.files["file"]
        self.attachments.append({"name": name, "item": q.form["itemId"], "type": q.form["itemType"], "size": len(content)})
        return envelope({"Name": name, "Url": f"https://files.example.invalid/{name}?token=abc123"})

    def post_side(self, q):
        ticket = self.live_ticket(q)
        ident = next(self._conversation_ids)
        self.conversations[ticket["Id"]].append({"Id": ident, "Name": q.json["Name"], "Email": q.json["Email"]})
        return envelope({"Id": ident})

    def post_approval(self, q):
        if not self.approver:
            return error_envelope(
                400,
                [(
                    "070101",
                    "An approver must be active, belong to the ticket's client and carry a contact tag marked as approver",
                    "ContactIds",
                )],
            )
        ident = self.guid()
        stored = q.json["ContactIds"] if self.stored_approvers is None else self.stored_approvers
        self.approvals[ident] = {
            "Id": ident, "Name": q.json["Name"], "Status": {"Id": 1, "Name": "Pending"}, "Type": {"Id": 4, "Name": "Approval"},
            "Approvers": [{"ContactId": c, "Status": {"Id": 1, "Name": "Pending"}} for c in stored],
            "CreatedOn": now_text(),
        }
        return envelope({"Id": ident})

    def get_approval(self, q):
        approval = self.approvals.get(self.part(q, 5))
        return envelope(approval) if approval else self.missing("approval")

    # time entries

    def post_entry(self, q):
        body = q.json
        ident = next(self._entry_ids)
        started, ended = body.get("StartedOn"), body.get("EndedOn")
        hours = body.get("ActualHours")
        if hours is None and started and ended:
            hours = (datetime.fromisoformat(ended.replace("Z", "+00:00")) - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds() / 3600
        no_service_line = "ServiceLineId" in body and body["ServiceLineId"] is None
        self.time_entries[ident] = {
            "Id": ident,
            # the PUBLISHED TimeEntryModel (production answers it since 2026-10-03): the user is {Id, Name}, the
            # ticket and the task are {Id, Number, Title} and the one the entry was not logged against is null.
            # On 2026-10-02 production answered the flat UserId, TicketId and TaskId instead (see make_flat).
            "Ticket": self.reference(body.get("TicketId"), self.tickets),
            "Task": self.reference(body.get("TaskId"), self.tasks),
            "User": {"Id": body["UserId"], "Name": "Operator"},
            "StartedOn": started, "EndedOn": ended, "ActualHours": hours, "AdjustedHours": hours,
            "BillableStatus": {"Id": body.get("BillableStatusId", 1), "Name": "x"},
            "BillingRole": {"Id": 11, "Name": "Technician"}, "WorkType": {"Id": 21, "Name": "Standard"},
            "ServiceLine": {"Id": None, "Name": None} if no_service_line else {"Id": 31, "Name": "Line"},
            "Comment": body.get("Comment"), "_deletes": 0,
        }
        return envelope({"Id": ident})

    def reference(self, ident, store: dict[str, dict]) -> dict | None:
        """The published NumberedReferenceModel {Id, Number, Title} of a ticket or task of the fake (None: there is none)."""
        if ident is None:
            return None
        record = store.get(ident) or {}
        return {"Id": ident, "Number": str(record.get("Number", "")), "Title": record.get("Title", "")}

    def entry(self, q):
        ident = int(self.part(q, 3))
        if ("time_entry", ident) in self.deleted:
            return None
        return self.time_entries.get(ident)

    def get_entry(self, q):
        entry = self.entry(q)
        return envelope({k: v for k, v in entry.items() if not k.startswith("_")}) if entry else self.missing("time entry")

    def patch_entry(self, q):
        entry = self.entry(q)
        if entry is None:
            return self.missing("time entry")
        if "Comment" in q.json:
            entry["Comment"] = q.json["Comment"]
        return envelope({k: v for k, v in entry.items() if not k.startswith("_")})

    def delete_entry(self, q):
        entry = self.entry(q)
        if entry is None:
            return envelope({"Id": int(self.part(q, 3)), "Outcome": "Deleted"})
        entry["_deletes"] += 1
        if entry["_deletes"] <= self.reopens:
            return envelope({"Id": entry["Id"], "Outcome": "Reopened"})
        self.deleted.add(("time_entry", entry["Id"]))
        return envelope({"Id": entry["Id"], "Outcome": "Deleted"})

    # catalog and invoices

    def find_item(self, ident):
        for row in [*self.catalog, *self.items.values()]:
            if isinstance(row, dict) and isinstance(ident, str) and isinstance(row.get("Id"), str) and row["Id"].lower() == ident.lower():
                return row
        return None

    def list_items(self, q):
        """The catalog (a test may put odd rows in it: they are served as they are) and the items the run made."""
        rows = [dict(row) if isinstance(row, dict) else row for row in self.catalog]
        rows += [
            {"Status": {"Id": 1, "Name": "Active"}, **item}
            for item in self.items.values()
            if ("item", item["Id"]) not in self.deleted
        ]
        for name, key in (("TypeIds", "Type"), ("StatusIds", "Status")):
            if name in q.query:
                wanted = {int(x) for x in q.query[name].split(",")}
                rows = [row for row in rows if not isinstance(row, dict) or (row.get(key) or {}).get("Id") in wanted]
        if "ClientIds" in q.query:
            wanted = {int(x) for x in q.query["ClientIds"].split(",")}
            rows = [row for row in rows if not isinstance(row, dict) or row.get("ClientId") in wanted]
        return self.paged(rows, q)

    def on_invoice_created(self, hook) -> None:
        """hook(invoice) runs on the STORED invoice right after every POST /v1/invoices (it may change it in place)."""
        self.invoice_hooks.append(hook)

    def post_invoice(self, q):
        body = q.json
        if body.get("ClientId") not in self.clients:
            return error_envelope(400, [("070101", "The client does not exist.", "ClientId")])
        lines = body.get("LineItems") or []
        if not lines:
            return error_envelope(400, [("070101", "At least one line item is required.", "LineItems")])
        stored = []
        for position, line in enumerate(lines):
            item = self.find_item(line.get("ItemId"))
            if item is None:
                return error_envelope(404, [("070404", "The item does not exist.", f"LineItems[{position}].ItemId")])
            price = line["UnitPrice"] if line.get("UnitPrice") is not None else item.get("UnitPrice") or 0.0
            stored.append(
                {
                    "Id": self.guid(), "ItemId": item["Id"], "Name": item["Name"], "Description": line.get("Description") or item["Name"],
                    "Quantity": line["Quantity"], "UnitPrice": price, "UnitCost": line.get("UnitCost", item.get("UnitCost")),
                    "DiscountPercent": line.get("DiscountPercent") or 0.0, "Tax": {"Id": 0, "Name": "No tax"}, "TaxAmount": 0.0,
                    "Amount": line["Quantity"] * price, "BillableStatus": {"Id": line.get("BillableStatusId", 1), "Name": "Billable"},
                    "CoaCode": line.get("CoaCode"), "ItemType": {"Id": 1, "Name": "Product"}, "SubItems": [],
                }
            )
        status = body.get("StatusId", 1)
        number = next(self._invoice_numbers)
        ident = invoice_guid(next(self._invoice_ids))
        total = sum(row["Amount"] for row in stored)
        if status == 5 and total == 0:  # the spec: an Approved invoice whose total is exactly 0 is created as Paid
            status = 3
        self.invoices[ident] = {
            "Id": ident, "ClientId": body["ClientId"], "Number": number, "DisplayNumber": f"INV-{number}",
            "Reference": body.get("Reference"),
            "Status": {"Id": status, "Name": {1: "Draft", 3: "Paid", 4: "Void", 5: "Approved"}.get(status, "Other")},
            # live, 2026-10-02: the dates come back as plain calendar dates
            "InvoiceDate": (body.get("InvoiceDate") or now_text())[:10], "DueDate": (body.get("DueDate") or now_text())[:10],
            "SubTotal": total, "TotalDiscount": 0.0, "TotalTax": sum(row["TaxAmount"] for row in stored), "Total": total,
            "AmountDue": total, "AmountPaid": 0.0,
            "ExternalId": None, "PaymentLink": None,  # set when the invoice reaches the accounting system (see served)
            "IsEmailSent": False, "EmailSentOn": None, "LineItems": stored, "Attachments": [], "_reads": 0,
        }
        self.emailed.extend(body.get("RecipientEmails") or [])
        for hook in self.invoice_hooks:
            hook(self.invoices[ident])
        return envelope({"Id": ident})

    def live_invoice(self, request):
        ident = self.part(request, 3)
        if ("invoice", ident) in self.deleted:
            return None
        return self.invoices.get(ident)

    def served(self, invoice: dict, *, read: bool = False) -> dict:
        """The invoice as the API answers it (no private bookkeeping). A read of an Approved invoice may be the one at which
        Gorelo has pushed it to the accounting system: its ExternalId, a Xero GUID, and a payment link are set then."""
        if read:
            invoice["_reads"] += 1
            if (
                invoice["Status"]["Id"] == 5 and invoice["ExternalId"] is None
                and self.sync_after is not None and invoice["_reads"] > self.sync_after
            ):
                ident = invoice["Id"]
                invoice["ExternalId"] = f"{ident[:8]}-5e1e-4a7b-9c3d-{ident[-12:]}"  # a Xero GUID, in the spec's words
                invoice["PaymentLink"] = f"https://in.xero.com/{ident}" if self.payment_link else None
        return {key: value for key, value in invoice.items() if not key.startswith("_")}

    def get_invoice(self, q):
        invoice = self.live_invoice(q)
        return envelope(self.served(invoice, read=True)) if invoice else self.missing("invoice")

    def list_invoices(self, q):
        rows = [invoice for invoice in self.invoices.values() if ("invoice", invoice["Id"]) not in self.deleted]
        rows.reverse()  # newest first, like the default sort
        if "ClientIds" in q.query:
            wanted = {int(x) for x in q.query["ClientIds"].split(",")}
            rows = [row for row in rows if row["ClientId"] in wanted]
        if "Number" in q.query:
            rows = [row for row in rows if str(row["Number"]) == q.query["Number"]]
        text = q.query.get("Query")
        if text:
            folded = text.casefold()
            rows = [
                row for row in rows
                if folded in (row["Reference"] or "").casefold() or folded in row["DisplayNumber"].casefold()
                or folded in str(row["Number"])
            ]
        return self.paged([{k: v for k, v in self.served(row).items() if k not in ("LineItems", "Attachments")} for row in rows], q)

    def get_invoice_pdf(self, q):
        invoice = self.live_invoice(q)
        if invoice is None:
            return httpx.Response(404, json=self.missing("invoice"))
        self.exports.append(invoice["Id"])  # every download is an export event on the invoice
        return httpx.Response(
            200, content=PDF_BYTES,
            headers={"content-type": "application/pdf", "content-disposition": f'attachment; filename="{invoice["DisplayNumber"]}.pdf"'},
        )

    def delete_invoice(self, q):
        """A Draft is deleted (StatusId 6), an Approved invoice is voided (StatusId 4), any other status is a 409, an
        invoice that is already deleted is a success and an unknown id is a 404 (the live answers of 2026-10-02)."""
        ident = self.part(q, 3)
        invoice = self.invoices.get(ident)
        if invoice is None:
            return self.missing("invoice")
        if ("invoice", ident) in self.deleted:
            return envelope({"Id": ident, "StatusId": 6})
        status = invoice["Status"]["Id"]
        if status == 1:
            self.deleted.add(("invoice", ident))
            return envelope({"Id": ident, "StatusId": 6})
        if status == 5:
            invoice["Status"] = {"Id": 4, "Name": "Void"}
            return envelope({"Id": ident, "StatusId": 4})
        if status == 4:  # the spec: deleting an invoice that is already void is treated as success
            return envelope({"Id": ident, "StatusId": 4})
        return error_envelope(409, [("070901", "Only a Draft can be deleted and only an Approved invoice can be voided.")])

    # items, uptime, projects

    def post_thing(self, q, store: dict, kind: str):
        ident = self.guid()
        body = q.json
        store[ident] = {
            "Id": ident, "Name": body.get("Name"), "ClientId": body.get("ClientId"), "Description": body.get("Description"),
            "UnitPrice": body.get("UnitPrice"), "Type": {"Id": body.get("TypeId"), "Name": "Product"},
        }
        return envelope({"Id": ident})

    def get_thing(self, q, store: dict, what: str, index: int = 3):
        ident = self.part(q, index)
        if (what, ident) in self.deleted:
            return self.missing(what)
        thing = store.get(ident)
        return envelope(thing) if thing else self.missing(what)

    def patch_thing(self, q, store: dict, what: str):
        thing = store.get(self.part(q, 3))
        if thing is None:
            return self.missing(what)
        for key, value in q.json.items():
            thing[key] = value
        return envelope({"Id": thing["Id"]})

    def delete_thing(self, q, store: dict, what: str, index: int = 3):
        ident = self.part(q, index)
        if ident not in store:
            return self.missing(what)
        self.deleted.add((what, ident))
        return envelope({"Id": ident})

    def post_uptime(self, q):
        body = q.json
        ident = self.guid()
        target = body.get("Target", {})
        self.uptime[ident] = {
            "Id": ident, "Type": {"Id": body["TypeId"], "Name": "HTTP"}, "Frequency": body["Frequency"], "RegionId": body["RegionId"],
            "Target": {"Ip": target.get("Ip"), "Port": target.get("Port"), "Url": target.get("Url")},
            "ClientId": body.get("ClientId"), "LocationId": body.get("LocationId"), "Description": body.get("Description"),
            "MaintenanceMode": {"Enabled": False, "DurationInMinutes": None, "Reason": None, "StartDateTime": None},
            "TagIds": [], "Status": {"Id": 1, "Name": "Up"},
        }
        return envelope({"Id": ident})

    def patch_uptime(self, q):
        check = self.uptime.get(self.part(q, 3))
        if check is None:
            return self.missing("uptime check")
        window = q.json.get("MaintenanceMode") or {}
        if window.get("Enabled") is True and not window.get("StartDateTime"):  # live, 2026-10-02
            return error_envelope(
                400,
                [(
                    "070101",
                    "MaintenanceMode.StartDateTime is required when enabling maintenance mode.",
                    "MaintenanceMode.StartDateTime",
                )],
            )
        for key, value in q.json.items():
            check[key] = {**check[key], **value} if key == "MaintenanceMode" else value
        return envelope({"Id": check["Id"]})

    def post_project(self, q):
        """Contract e15cb5a18ec2: ClientId, GroupId, LocationId, Title and TypeId are required."""
        body = q.json
        if body.get("TypeId") not in {t.get("Id") for t in self.project_types if isinstance(t, dict)}:
            return error_envelope(400, [("070101", "TypeId is required and must be a project type.", "TypeId")])
        if body.get("GroupId") not in (7201, 7202):
            return error_envelope(400, [("070101", "GroupId is required and must be a group.", "GroupId")])
        if not body.get("LocationId"):
            return error_envelope(400, [("070101", "LocationId is required.", "LocationId")])
        ident = self.guid()
        self.projects[ident] = {
            "Id": ident, "Title": body["Title"], "ClientId": body.get("ClientId"), "TypeId": body["TypeId"],
            "GroupId": body["GroupId"], "LocationId": body["LocationId"], "Status": {"Id": 1, "Name": "NotStarted"},
        }
        return envelope({"Id": ident})

    def post_section(self, q):
        ident = self.guid()
        self.sections[ident] = {"Id": ident, "Title": q.json["Title"]}
        return envelope({"Id": ident})

    def post_task(self, q):
        ident = self.guid()
        self.tasks[ident] = {
            "Id": ident, "Title": q.json["Title"], "ProjectId": self.part(q, 3), "SectionId": q.json.get("SectionId"),
            "Status": {"Id": 1, "Name": "NotStarted"},
        }
        return envelope({"Id": ident})

    def post_task_comment(self, q):
        ident = self.guid()
        self.task_comments[ident] = {
            "Id": ident, "BodyHtml": q.json["Body"], "ConversationType": {"Id": q.json["ConversationTypeId"], "Name": "Private"},
        }
        return envelope({"Id": ident})

    # -- inspection ----------------------------------------------------------

    def leftovers(self) -> list[str]:
        """What the run created and nobody deleted (comments and children go with their ticket)."""
        stores = (
            ("client", [c for c in self.clients if c not in (TEST_CLIENT, SECOND)]),
            ("contact", [c for c in self.contacts if c != OPERATOR_CONTACT]),
            ("ticket", [t["Id"] for t in self.tickets.values() if t["Title"].startswith("MCPTEST-")]),
            ("time_entry", list(self.time_entries)),
            ("item", list(self.items)),
            ("uptime check", list(self.uptime)),
            ("project", list(self.projects)),
            ("task", list(self.tasks)),
            # a voided invoice cannot be removed: it stays listed as Void, a known residue and not a leftover (see residue())
            ("invoice", [i for i, row in self.invoices.items() if row["Status"]["Id"] != 4]),
        )
        return [f"{kind} {ident}" for kind, idents in stores for ident in idents if (kind, ident) not in self.deleted]

    def residue(self) -> list[str]:
        """The invoices that were voided: they stay in Gorelo, listed as Void, and nobody can remove them."""
        return [f"invoice {i}" for i, row in self.invoices.items() if row["Status"]["Id"] == 4 and ("invoice", i) not in self.deleted]


# --------------------------------------------------------------------------
# Running the matrix
# --------------------------------------------------------------------------


@pytest.fixture
def lines():
    return []


@pytest.fixture
def run(tmp_path, make_settings, lines):
    async def go(fake: FakeGorelo, **options):
        options.setdefault("pace", 0)
        options.setdefault("lookup_wait", 0)
        options.setdefault("started", RUN_START)
        return await write_matrix.run_matrix(
            settings=make_settings(destructive=True),
            transport=fake.mock.transport,
            directory=tmp_path / "runs",
            echo=lines.append,
            **options,
        )

    return go


def results(report) -> dict:
    return {result.area: result for result in report.results}


def area_ops(fake: FakeGorelo, report) -> dict[str, list[str]]:
    """The spec operation of every request, by area (the area's own requests, cut by its request count)."""
    out, start = {}, 0
    for result in report.results:
        chunk = fake.mock.requests[start : start + result.requests]
        out[result.area] = [f"{r.method} {op_key(r.method, r.path).split(' ', 1)[1]}" for r in chunk]
        start += result.requests
    out["cleanup"] = [f"{r.method} {op_key(r.method, r.path).split(' ', 1)[1]}" for r in fake.mock.requests[start:]]
    return out


def make_flat(record: dict) -> None:
    """In place: the time entry as production answered on 2026-10-02, with the flat UserId, TicketId and TaskId (the Id
    of the User, Ticket and Task object, None where there is no object) and no objects."""
    user, ticket, task = (record.pop(key, None) for key in ("User", "Ticket", "Task"))
    record.update(UserId=(user or {}).get("Id"), TicketId=(ticket or {}).get("Id"), TaskId=(task or {}).get("Id"))


def reshape_entry_reads(fake: FakeGorelo, reshape) -> None:
    """Every successful GET of a time entry answers `reshape(copy of the stored entry)`; a 404 is passed on as it is."""
    original = fake.handlers["GET /v1/time-entries/{timeEntryId}"]

    def reshaped(request):
        answer = original(request)
        if not isinstance(answer.get("Data"), dict):
            return answer
        record = dict(answer["Data"])
        reshape(record)
        return envelope(record)

    fake.handlers["GET /v1/time-entries/{timeEntryId}"] = reshaped


GOLDEN = {
    "clients": [
        "POST /v1/clients",
        "GET /v1/clients/{clientId}/locations",
        "PATCH /v1/clients/{clientId}",
        "GET /v1/clients/{clientId}",
    ],
    "contacts": [
        "POST /v1/contacts",
        "GET /v1/contacts/{contactId}",
        "PATCH /v1/contacts/{contactId}",
        "GET /v1/contacts/{contactId}",
    ],
    "tickets": [
        "GET /v1/tickets/statuses",
        "GET /v1/tickets/types",
        "GET /v1/organization/groups",
        "POST /v1/tickets",
        "GET /v1/tickets/{ticketId}",
        "POST /v1/tickets",
        "GET /v1/tickets/{ticketId}",
        "GET /v1/tickets/{ticketId}",
        "GET /v1/tickets",
        "GET /v1/tickets/{ticketId}",
        "GET /v1/tickets",
        "GET /v1/tickets/{ticketId}",
        "PATCH /v1/tickets/{ticketId}",
        "GET /v1/tickets/{ticketId}",
        "PATCH /v1/tickets/{ticketId}",
        "GET /v1/tickets/{ticketId}",
        "PATCH /v1/tickets/{ticketId}",
        "GET /v1/tickets/{ticketId}",
        "GET /v1/billing-roles",
        "GET /v1/work-types",
        "GET /v1/tickets/{ticketId}",
        "PATCH /v1/tickets/{ticketId}",
        "GET /v1/tickets/{ticketId}",
        "GET /v1/tickets/{ticketId}",
        "PATCH /v1/tickets/{ticketId}",
        "GET /v1/tickets/{ticketId}",
        "GET /v1/tickets",
    ],
    "comments": [
        "POST /v1/tickets/{ticketId}/comments",
        "GET /v1/tickets/{ticketId}/comments/{commentId}",
        "GET /v1/tickets/{ticketId}/comments",
        "GET /v1/tickets/{ticketId}/comments/{commentId}",
        "DELETE /v1/tickets/{ticketId}/comments/{commentId}",
        "POST /v1/attachments",
        "POST /v1/tickets/{ticketId}/comments",
        "GET /v1/tickets/{ticketId}/comments/{commentId}",
    ],
    "email": [
        "POST /v1/tickets",
        "GET /v1/tickets/{ticketId}",
        "POST /v1/tickets/{ticketId}/comments",
        "GET /v1/tickets/{ticketId}/comments/{commentId}",
        "PATCH /v1/tickets/{ticketId}",
        "GET /v1/tickets/{ticketId}",
        "POST /v1/tickets/{ticketId}/conversations/side-conversation",
        "POST /v1/tickets/{ticketId}/comments",
        "GET /v1/tickets/{ticketId}/comments/{commentId}",
        "POST /v1/tickets/{ticketId}/conversations/approval",
        "GET /v1/tickets/{ticketId}/approvals/{approvalId}",
        "POST /v1/tickets/{ticketId}/comments",
        "GET /v1/tickets/{ticketId}/comments/{commentId}",
    ],
    "time": [
        "POST /v1/time-entries",
        "GET /v1/time-entries/{timeEntryId}",
        "PATCH /v1/time-entries/{timeEntryId}",
        "DELETE /v1/time-entries/{timeEntryId}",
        "GET /v1/time-entries/{timeEntryId}",
    ],
    "items": [
        "POST /v1/items",
        "GET /v1/items/{itemId}",
        "PATCH /v1/items/{itemId}",
        "GET /v1/items/{itemId}",
        "DELETE /v1/items/{itemId}",
    ],
    "invoices": [
        "GET /v1/items",
        "POST /v1/invoices",
        "GET /v1/invoices/{invoiceId}",
        "GET /v1/invoices/{invoiceId}",
        "GET /v1/invoices",
        "GET /v1/invoices/{invoiceId}/pdf",
        "GET /v1/invoices/{invoiceId}",
        "GET /v1/invoices",
        "DELETE /v1/invoices/{invoiceId}",
        "GET /v1/invoices",
    ],
    "uptime": [
        "GET /v1/clients/{clientId}/locations",
        "POST /v1/uptime",
        "GET /v1/uptime/{checkId}",
        "PATCH /v1/uptime/{checkId}",
        "GET /v1/uptime/{checkId}",
        "PATCH /v1/uptime/{checkId}",
        "GET /v1/uptime/{checkId}",
        "DELETE /v1/uptime/{checkId}",
    ],
    "projects": [
        "GET /v1/projects",
        "GET /v1/forms",
        "GET /v1/projects/types",
        "POST /v1/projects",
        "GET /v1/projects/{projectId}",
        "POST /v1/projects/{projectId}/sections",
        "POST /v1/projects/{projectId}/tasks",
        "GET /v1/projects/{projectId}/tasks/{taskId}",
        "POST /v1/projects/{projectId}/tasks/{taskId}/comments",
        "GET /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}",
        "DELETE /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}",
        "DELETE /v1/projects/{projectId}/tasks/{taskId}",
    ],
    "cleanup": [
        "DELETE /v1/projects/{projectId}",
        "DELETE /v1/tickets/{ticketId}",
        "DELETE /v1/tickets/{ticketId}/comments/{commentId}",
        "DELETE /v1/tickets/{ticketId}",
        "DELETE /v1/tickets/{ticketId}",
        "DELETE /v1/contacts/{contactId}",
        "DELETE /v1/clients/{clientId}",
    ],
}
# The approved_invoice area (--with-approved-invoice, which runs it alone) in the happy path: Gorelo has pushed the invoice to
# the accounting system by the first get_invoice (the fake sets ExternalId at once), delete_invoice finds it by its Number
# at the first try, and the cleanup after it sends nothing (a voided invoice is a known residue).
GOLDEN_APPROVED = [
    "GET /v1/contacts",
    "GET /v1/clients/{clientId}/locations",
    "GET /v1/items",
    "POST /v1/invoices",
    "GET /v1/invoices/{invoiceId}",
    "GET /v1/invoices/{invoiceId}",
    "GET /v1/invoices",
    "DELETE /v1/invoices/{invoiceId}",
    "GET /v1/invoices/{invoiceId}",
    "GET /v1/invoices",
]


# --------------------------------------------------------------------------
# The whole run
# --------------------------------------------------------------------------


async def test_a_full_run_passes_and_leaves_nothing_behind(run, lines):
    fake = FakeGorelo()
    report = await run(fake, with_items=True, with_invoices=True)
    assert report.ok and report.exit_code == 0, "\n".join(lines)
    assert [r.area for r in report.results] == [a.key for a in write_matrix.AREAS]
    assert [r.area for r in report.results][6:9] == ["items", "invoices", "approved_invoice"]  # the invoices follow the items
    # every area passes but the approved invoice, which only its own flag (and then it runs alone) opens
    assert all(r.status == "pass" for r in report.results if r.area != "approved_invoice"), [
        (r.area, r.status, r.detail) for r in report.results
    ]
    assert results(report)["approved_invoice"].status == "skipped" and results(report)["approved_invoice"].requests == 0
    assert fake.leftovers() == []
    assert report.refused == 0 and report.cleanup.ok and report.scopes_missing == []
    assert report.summary["leftovers"] == 0 and report.summary["unresolved_intents"] == 0
    # 21 records: everything is cleaned but the uploaded file, which the API cannot delete (known undeletable)
    assert report.summary["created"] == 21 and report.summary["cleaned"] == 20 and report.summary["undeletable"] == 1
    assert not fake.mock.unmatched


async def test_the_requests_of_every_area_are_exactly_the_documented_ones(run):
    fake = FakeGorelo()
    report = await run(fake, with_items=True, with_invoices=True)
    assert report.ok
    assert area_ops(fake, report) == {**GOLDEN, "approved_invoice": []}  # skipped: not a request


async def test_the_module_docstring_lists_exactly_these_requests():
    """The per-area request lists in the docstring (what a reader sees) are the ones the golden test pins."""
    lines = write_matrix.__doc__.splitlines()
    listed: dict[str, list[str]] = {}
    current = None
    for line in lines[lines.index("Live requests per area (method and spec path, in the order they are sent; the tests pin these lists):") :]:
        heading = re.fullmatch(r"    (\w+):", line)
        step = re.fullmatch(r"        ((?:GET|POST|PATCH|DELETE) /v1/\S+)(?:  .*)?", line)
        if heading:
            current = heading.group(1)
            listed[current] = []
        elif step and current:
            listed[current].append(step.group(1))
    assert listed == {**GOLDEN, "approved_invoice": GOLDEN_APPROVED}


async def test_every_create_is_announced_first_and_recorded_after(run, monkeypatch):
    fake = FakeGorelo()
    kinds: dict[int, str] = {}
    originals = (Manifest.intent, Manifest.created, Manifest.intent_failed)

    def intent(self, kind, label, details=None):
        seq = originals[0](self, kind, label, details)
        kinds[seq] = kind
        fake.events.append(("intent", kind, label))
        return seq

    def created(self, kind, ident, label, details=None):
        record = originals[1](self, kind, ident, label, details)
        fake.events.append(("created", kind, label))
        return record

    def failed(self, seq, reason):
        originals[2](self, seq, reason)
        fake.events.append(("failed", kinds[seq], reason))

    monkeypatch.setattr(Manifest, "intent", intent)
    monkeypatch.setattr(Manifest, "created", created)
    monkeypatch.setattr(Manifest, "intent_failed", failed)
    report = await run(fake, with_items=True, with_invoices=True)
    assert report.ok
    open_intents: collections.Counter = collections.Counter()
    creates = 0
    for event in fake.events:
        if event[0] == "intent":
            open_intents[event[1]] += 1
        elif event[0] in ("created", "failed"):
            open_intents[event[1]] -= 1
            assert open_intents[event[1]] >= 0, event
        elif event[1] == "POST":
            kind = _CREATES.get(op_key("POST", event[2]))
            if kind:
                creates += 1
                assert open_intents[kind] > 0, f"{event[2]} was sent without an open intent of kind {kind}"
    assert creates == 21 and all(count == 0 for count in open_intents.values())


async def test_everything_it_creates_is_named_with_the_run_id_and_only_allowed_addresses_are_used(run):
    fake = FakeGorelo()
    report = await run(fake, with_items=True, with_invoices=True)
    assert report.ok and report.run_id == RUN
    for ident, ticket in fake.tickets.items():
        if ticket["Title"].startswith("older ticket"):
            continue
        assert ticket["Title"].startswith(RUN), ticket["Title"]
    names = (
        [c["Name"] for i, c in fake.clients.items() if i not in (TEST_CLIENT, SECOND)]
        + [c["FirstName"] for i, c in fake.contacts.items() if i != OPERATOR_CONTACT]
        + [i["Name"] for i in fake.items.values()]
        + [u["Description"] for u in fake.uptime.values()]
        + [p["Title"] for p in fake.projects.values()]
        + [s["Title"] for s in fake.sections.values()]
        + [t["Title"] for t in fake.tasks.values()]
        + [a["name"] for a in fake.attachments]
        + [e["Comment"] for e in fake.time_entries.values()]
        + [i["Reference"] for i in fake.invoices.values()]
        + [line["Description"] for i in fake.invoices.values() for line in i["LineItems"]]
    )
    assert names and all(name.startswith(RUN) for name in names), names
    assert len([n for n in names if n == f"{RUN} invoice"]) == 2  # the Reference and the line text of the one invoice
    assert fake.emailed == []  # no invoice recipient, not even the operator
    for comments in fake.comments.values():
        assert all(RUN in comment["BodyHtml"] for comment in comments.values())
    allowed = re.compile(rf"^(?:{re.escape(OPERATOR_EMAIL)}|[a-z0-9._%+-]+@example\.invalid)$", re.IGNORECASE)
    for request in fake.mock.requests:
        for token in re.findall(r"[\w.%+-]+@[\w.-]+", request.content.decode("utf-8", "replace")):
            assert allowed.match(token), token


async def test_nothing_outside_test_client_second_the_operator_and_the_run_is_ever_touched(run):
    fake = FakeGorelo()
    report = await run(fake, with_items=True)
    assert report.ok
    made_contacts = {c for c in fake.contacts if c != OPERATOR_CONTACT}
    made_clients = {c for c in fake.clients if c not in (TEST_CLIENT, SECOND)}
    assert made_contacts == {8301} and made_clients == {8200}
    for request in fake.mock.requests:
        assert request.method in ("GET", "POST", "PATCH", "DELETE")
        assert not any(part in request.path for part in ("/assets/", "/alerts", "/invoices", "/contracts"))
        body = request.json if isinstance(request.json, dict) else {}
        assert body.get("ClientId") in (None, TEST_CLIENT, SECOND, *made_clients), request.path
        assert body.get("ContactId") in (None, OPERATOR_CONTACT, *made_contacts), request.path
        assert body.get("LeadAssigneeId") in (None, OPERATOR_USER) and body.get("UserId") in (None, OPERATOR_USER)
        assert set(body.get("WatcherIds") or []) <= {OPERATOR_USER} and "TagIds" not in body
        assert set(body.get("CcContactIds") or []) <= {OPERATOR_CONTACT, *made_contacts}
        assert set(body.get("ContactIds") or []) <= {OPERATOR_CONTACT, *made_contacts}
        assert body.get("AdoptClientAssets") in (None, False) and not body.get("AgentAssetIds") and not body.get("CustomAssetIds")
        if request.method == "PATCH" and request.path.startswith("/v1/clients/"):  # only the run's temporary client
            assert int(request.path.rsplit("/", 1)[1]) in made_clients and body.get("Id") in (None, *made_clients)
        if request.method == "PATCH" and request.path.startswith("/v1/contacts/"):  # only the run's own contact
            assert int(request.path.rsplit("/", 1)[1]) in made_contacts and body.get("ContactId") in (None, *made_contacts)
        assert request.path not in ("/v1/clients", "/v1/contacts") or request.method in ("GET", "POST")  # no collection PATCH
        if request.method == "DELETE":  # only what the run made, never the test client, Second or the operator
            assert request.path not in (f"/v1/clients/{TEST_CLIENT}", f"/v1/clients/{SECOND}", f"/v1/contacts/{OPERATOR_CONTACT}")
    # the test client itself is neither read nor changed: not even a GET of the client, and no PATCH of it
    assert not any(r.path == f"/v1/clients/{TEST_CLIENT}" for r in fake.mock.requests)
    assert fake.clients[TEST_CLIENT] == {"Id": TEST_CLIENT, "Name": "the test client", "AlternateName": "the test client alt", "BillingName": "the test client"}
    assert not any(r.path == "/v1/tickets/tags" for r in fake.mock.requests)  # no tenant tag is looked up or applied
    deleted_tickets = {t for kind, t in fake.deleted if kind == "ticket"}
    assert deleted_tickets == {t["Id"] for t in fake.tickets.values() if t["Title"].startswith(RUN)}


def test_the_matrix_sends_everything_through_tools_there_is_no_raw_client_and_no_unassign_probe():
    assert "raw" not in inspect.signature(write_matrix.Matrix).parameters
    assert not hasattr(write_matrix.Matrix, "probe_unassign") and not hasattr(write_matrix, "httpx")
    assert "AsyncClient" not in inspect.getsource(write_matrix)


async def test_the_matrix_may_only_call_its_own_tools():
    from tools._common import REGISTRY

    registered = {spec.name for spec in REGISTRY.specs}
    assert write_matrix.MATRIX_TOOLS <= registered
    assert not write_matrix.MATRIX_TOOLS & {"post_alert", "create_approved_invoice", "create_form_submission_link"}
    assert "list_ticket_tags" not in write_matrix.MATRIX_TOOLS  # no tenant tag is ever looked up
    gated = {spec.name for spec in REGISTRY.specs if spec.kind == "destructive"}
    assert write_matrix.MATRIX_TOOLS & gated == {
        "delete_ticket_comment", "delete_time_entry", "delete_item", "delete_uptime_check",
        "delete_project_comment", "delete_project_task", "delete_invoice",
    }
    assert "list_project_types" in write_matrix.MATRIX_TOOLS  # create_project needs a project type
    assert write_matrix.ITEM_TOOLS == {"create_item", "update_item", "delete_item"}
    assert write_matrix.INVOICE_TOOLS == {
        "list_items", "list_invoices", "get_invoice", "create_invoice", "export_invoice_pdf", "delete_invoice",
    }
    assert write_matrix.INVOICE_TOOLS <= write_matrix.MATRIX_TOOLS
    assert write_matrix.tools_for(True, True) == write_matrix.MATRIX_TOOLS
    assert write_matrix.tools_for(False) == write_matrix.MATRIX_TOOLS - write_matrix.ITEM_TOOLS - write_matrix.INVOICE_TOOLS
    assert write_matrix.tools_for(True) == write_matrix.MATRIX_TOOLS - write_matrix.INVOICE_TOOLS  # items alone
    assert write_matrix.tools_for(False, True) == write_matrix.MATRIX_TOOLS - write_matrix.ITEM_TOOLS  # invoices alone
    assert write_matrix.tools_for(False, False) == write_matrix.tools_for(False)
    # the approved invoice run has a tool set of its own: seven tools, two of them gated, none of the matrix's creates
    assert write_matrix.APPROVED_INVOICE_TOOLS == {
        "create_approved_invoice", "get_invoice", "list_invoices", "delete_invoice", "list_items", "list_contacts",
        "list_client_locations",
    }
    assert write_matrix.APPROVED_INVOICE_TOOLS <= registered
    assert write_matrix.APPROVED_INVOICE_TOOLS & gated == {"create_approved_invoice", "delete_invoice"}
    assert not write_matrix.APPROVED_INVOICE_TOOLS & {"create_invoice", "export_invoice_pdf", "post_alert", "create_item"}
    assert write_matrix.tools_for(False, False, True) == write_matrix.APPROVED_INVOICE_TOOLS


def test_create_approved_invoice_is_named_only_in_the_approved_run_s_own_tool_set_and_in_its_area():
    import ast

    from tools._common import REGISTRY

    kinds = {spec.name: spec.kind for spec in REGISTRY.specs}
    assert kinds["create_approved_invoice"] == "destructive"  # live_settings() registers it: the allowlist is what stops it
    # it is in no tool set of the ordinary matrix, whatever the flags ...
    assert "create_approved_invoice" not in write_matrix.MATRIX_TOOLS
    for with_items in (False, True):
        for with_invoices in (False, True):
            assert "create_approved_invoice" not in write_matrix.tools_for(with_items, with_invoices)
            assert "create_approved_invoice" not in write_matrix.tools_for(with_items, with_invoices, False)
            # ... and the run that has the approved flag may call nothing but its own seven tools
            assert write_matrix.tools_for(with_items, with_invoices, True) == write_matrix.APPROVED_INVOICE_TOOLS
    # nowhere in the module is the tool NAMED as a value except in that set and in the area that calls it (a call, a set
    # member, an argument): everywhere else only prose mentions it
    tree = ast.parse(inspect.getsource(write_matrix))
    homes = set()
    for top in tree.body:
        for node in ast.walk(top):
            if isinstance(node, ast.Constant) and node.value == "create_approved_invoice":
                homes.add(top.targets[0].id if isinstance(top, ast.Assign) else getattr(top, "name", type(top).__name__))
    assert homes == {"APPROVED_INVOICE_TOOLS", "area_approved_invoice"}


# --------------------------------------------------------------------------
# Failures, the guard and the cleanup in finally
# --------------------------------------------------------------------------


async def test_a_failing_area_fails_alone_the_rest_runs_and_the_cleanup_still_happens(run, lines):
    fake = FakeGorelo()
    fake.fail("PATCH /v1/tickets/{ticketId}", error_envelope(400, [("070101", "Title is too long", "Title")]))
    report = await run(fake, with_items=True, with_invoices=True)
    found = results(report)
    assert found["tickets"].status == "FAIL" and "update_ticket" in found["tickets"].detail
    others = [r for r in report.results if r.area != "tickets"]
    assert [r.status for r in others] == ["pass"] * 7 + ["skipped"] + ["pass"] * 2  # only the approved invoice is off
    assert [r.area for r in others if r.status == "skipped"] == ["approved_invoice"]
    assert not report.ok and report.exit_code == 1
    assert fake.leftovers() == [] and report.cleanup.ok  # everything it had created is gone
    assert lines[-1] == "result: FAILED"


async def test_cleanup_runs_even_when_the_run_is_interrupted(run, monkeypatch, lines):
    fake = FakeGorelo()

    class Abort(BaseException):
        pass

    async def boom(matrix):
        await matrix.plain_ticket()
        raise Abort("stop")

    monkeypatch.setattr(write_matrix, "AREAS", (write_matrix.Area("clients", write_matrix.area_clients), write_matrix.Area("tickets", boom)))
    with pytest.raises(Abort):
        await run(fake)
    assert fake.leftovers() == []  # the client and the ticket were created before the interruption: both are gone
    assert any(line.startswith("cleanup of MCPTEST-") for line in lines)
    assert any("interrupted (Abort)" in line for line in lines) and lines[-1] == "result: FAILED"


async def test_a_crashing_cleanup_is_reported_and_the_run_still_ends_with_a_summary(run, monkeypatch, lines):
    fake = FakeGorelo()

    async def broken(manifest, **options):
        raise RuntimeError(f"cleanup broke near {TEST_API_KEY}")

    monkeypatch.setattr(write_matrix, "run_cleanup", broken)
    report = await run(fake, areas=["items"], with_items=True)
    assert not report.ok and report.cleanup is None
    assert "cleanup could not finish: RuntimeError: cleanup broke near ***" in lines
    assert any("python -m scripts.live.cleanup" in line for line in lines)
    assert not any(TEST_API_KEY in line for line in lines)


@pytest.mark.parametrize("client", [SECOND, TEST_CLIENT])
async def test_the_guard_is_installed_and_a_bad_request_stops_the_run(run, monkeypatch, client):
    fake = FakeGorelo()

    async def bad(matrix):
        await matrix.tool("update_client", client_id=client, alternate_name="x")  # no client but the run's own may change

    monkeypatch.setattr(
        write_matrix,
        "AREAS",
        (write_matrix.Area("clients", bad), write_matrix.Area("uptime", write_matrix.area_uptime)),
    )
    report = await run(fake)
    assert [(r.area, r.status) for r in report.results] == [("clients", "FAIL"), ("uptime", "not run")]
    detail = results(report)["clients"].detail
    assert "the guard refused" in detail and f"path id {client} is not allowed" in detail
    assert fake.mock.requests == []  # nothing left the process, not even for the cleanup (nothing was created)
    assert report.refused == 1 and not report.ok


async def test_a_guard_refusal_on_a_ticket_update_stops_the_run_too(run, monkeypatch):
    fake = FakeGorelo()

    async def bad(matrix):
        ticket = (await matrix.plain_ticket())["Id"]
        await matrix.tool("update_ticket", ticket_id=ticket, lead_assignee_id=9202)  # not the operator user

    monkeypatch.setattr(write_matrix, "AREAS", (write_matrix.Area("tickets", bad),))
    report = await run(fake)
    assert report.results[0].status == "FAIL" and report.refused == 1
    assert "LeadAssigneeId 9202 is not allowed" in report.results[0].detail
    assert not any(r.method == "PATCH" for r in fake.mock.requests)
    assert fake.leftovers() == []


async def test_a_create_refused_locally_never_leaves_an_open_intent(run, monkeypatch):
    fake = FakeGorelo()

    async def blank(matrix):
        label = matrix.manifest.label("blank item")
        await matrix.create("item", label, {"client_id": TEST_CLIENT}, "create_item", {"type": "product", "name": " ", "client_id": TEST_CLIENT})

    monkeypatch.setattr(write_matrix, "AREAS", (write_matrix.Area("items", blank),))
    report = await run(fake, with_items=True)
    assert results(report)["items"].status == "FAIL" and "name: must not be empty" in results(report)["items"].detail
    assert fake.mock.requests == [] and report.summary["unresolved_intents"] == 0 and report.cleanup.ok


async def test_a_create_that_gorelo_refuses_settles_its_intent_and_one_that_timed_out_stays_open(run, tmp_path, lines):
    fake = FakeGorelo()
    fake.fail("POST /v1/items", error_envelope(400, [("070101", "Name is required", "Name")]))
    report = await run(fake, areas=["items"], with_items=True)
    assert results(report)["items"].status == "FAIL"
    assert report.summary["unresolved_intents"] == 0 and report.cleanup.ok  # a 4xx made nothing

    fake = FakeGorelo()
    fake.fail("POST /v1/items", error_envelope(500, [("070500", "boom")]))
    report = await run(
        fake, areas=["items"], with_items=True, started=datetime(2099, 10, 2, 10, 16, 0, tzinfo=timezone.utc)
    )
    assert results(report)["items"].status == "FAIL"
    assert report.summary["unresolved_intents"] == 1 and not report.cleanup.ok  # a 5xx may have made it
    assert any("announced without an id" in line for line in lines)


# --------------------------------------------------------------------------
# --only, --skip-email and the summary
# --------------------------------------------------------------------------


async def test_skip_email_leaves_the_email_area_out_and_never_touches_second(run):
    fake = FakeGorelo()
    report = await run(fake, skip_email=True)
    assert report.ok and "email" not in results(report)
    assert len(report.results) == 10  # items, invoices and approved_invoice are skipped by default, and email is left out
    for request in fake.mock.requests:
        body = request.json or {}
        assert body.get("ClientId") != SECOND and body.get("ContactId") != OPERATOR_CONTACT
        assert "/conversations/" not in request.path


async def test_only_runs_the_named_areas_in_matrix_order_and_a_dependent_area_makes_its_own_ticket(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["time", "comments", "forms"])
    assert [r.area for r in report.results] == ["comments", "time", "projects"]
    assert report.ok
    tickets = [t for t in fake.tickets.values() if t["Title"].startswith(RUN)]
    assert len(tickets) == 1 and tickets[0]["Title"] == f"{RUN} plain ticket"  # made once, shared by comments and time
    ops = area_ops(fake, report)
    assert ops["comments"][:5] == [
        "GET /v1/tickets/statuses", "GET /v1/tickets/types", "GET /v1/organization/groups",
        "POST /v1/tickets", "GET /v1/tickets/{ticketId}",
    ]


def test_unknown_areas_and_empty_selections_are_usage_errors():
    with pytest.raises(write_matrix.UsageError, match="unknown area 'bogus'; the areas are clients, contacts"):
        write_matrix.select_areas(["bogus"], False)
    with pytest.raises(write_matrix.UsageError, match="no area left to run"):
        write_matrix.select_areas(["email"], True)
    assert [a.key for a in write_matrix.select_areas(None, True)] == [a.key for a in write_matrix.AREAS if a.key != "email"]
    assert [a.key for a in write_matrix.select_areas(["Forms", "projects", " items"], False)] == ["items", "projects"]
    assert [a.key for a in write_matrix.select_areas(["projects", "Invoices", "items", "uptime"], False)] == [
        "items", "invoices", "uptime", "projects",
    ]  # the invoices come right after the items, whatever order --only names them in
    assert [a.key for a in write_matrix.select_areas(["uptime", "approved_invoice", "invoices"], False)] == [
        "invoices", "approved_invoice", "uptime",
    ]  # and the approved invoice right after the invoices
    assert [a.key for a in write_matrix.AREAS] == [
        "clients", "contacts", "tickets", "comments", "email", "time", "items", "invoices", "approved_invoice", "uptime",
        "projects",
    ]
    areas = "clients, contacts, tickets, comments, email, time, items, invoices, approved_invoice, uptime, projects"
    with pytest.raises(write_matrix.UsageError, match=f"the areas are {areas}"):
        write_matrix.select_areas(["bogus"], False)


def test_the_approved_invoice_flag_runs_its_area_alone_and_anything_else_is_a_usage_error():
    select = write_matrix.select_areas
    assert [a.key for a in select(["approved_invoice"], False, True)] == ["approved_invoice"]
    assert [a.key for a in select([" Approved_Invoice ", "approved_invoice"], True, True)] == ["approved_invoice"]
    for only in (None, ["approved_invoice", "invoices"], ["invoices"], ["approved_invoice", "uptime"], ["items", "approved_invoice"]):
        with pytest.raises(write_matrix.UsageError, match="--with-approved-invoice runs the approved_invoice area alone: use --only approved_invoice"):
            select(only, False, True)
    # without the flag the area is an ordinary selectable area (it is skipped when it runs)
    assert "approved_invoice" in [a.key for a in select(None, False)]
    assert [a.key for a in select(["approved_invoice", "invoices"], False)] == ["invoices", "approved_invoice"]


async def test_settings_that_leave_out_a_tool_the_matrix_calls_are_refused_before_anything_is_created(
    tmp_path, make_settings
):
    fake = FakeGorelo()
    for settings, missing in (
        (make_settings(destructive=False), "delete_ticket_comment"),  # the gated deletes are not registered
        (make_settings(destructive=True, toolsets=frozenset({"core", "tickets", "time", "billing", "uptime"})), "create_project"),
    ):
        with pytest.raises(write_matrix.SetupError, match=f"does not offer the tools the matrix calls: .*{missing}"):
            await write_matrix.run_matrix(
                settings=settings, transport=fake.mock.transport, directory=tmp_path / "runs", pace=0, echo=lambda line: None
            )
    assert fake.mock.requests == [] and not (tmp_path / "runs").exists()


async def test_a_bad_selection_is_refused_before_anything_is_created(run, tmp_path):
    fake = FakeGorelo()
    with pytest.raises(write_matrix.UsageError):
        await run(fake, areas=["nope"])
    assert fake.mock.requests == [] and not (tmp_path / "runs").exists()


async def test_the_summary_names_every_area_the_notes_the_counts_and_the_scopes(run, lines):
    fake = FakeGorelo(project_scope=False, forms_scope=False)
    report = await run(fake)
    assert report.ok  # a skipped area (scope missing, items without --with-items) is not a failure
    assert report.scopes_missing == ["Forms", "Project"]
    text = "\n".join(lines)
    assert f"write matrix {RUN}: summary" in text
    for area in write_matrix.AREAS:
        assert re.search(rf"^{area.key}\s+(pass|skipped)\s+\d+", text, re.MULTILINE), area.key
    assert re.search(r"^projects\s+skipped\s+2\s+scope missing: Project$", text, re.MULTILINE)
    assert re.search(rf"^items\s+skipped\s+0\s+{re.escape(write_matrix.ITEMS_SKIP_REASON)}$", text, re.MULTILINE)
    assert re.search(rf"^invoices\s+skipped\s+0\s+{re.escape(write_matrix.INVOICES_SKIP_REASON)}$", text, re.MULTILINE)
    assert "area projects: skipped, 2 requests (scope missing: Project)" in lines  # progress, while the run goes on
    assert lines.index("area clients: pass, 4 requests") < lines.index("area contacts: pass, 4 requests")
    # 15 records (no items, no project): all cleaned but the uploaded file, which is known undeletable and not a leftover
    assert "records: 15 created, 14 cleaned, 0 left over, 1 known undeletable, 0 announced without an id" in text
    assert (
        f"known undeletable: attachment {RUN}-attachment.txt: soft-deleted with its ticket; "
        "the file itself cannot be deleted through the API"
    ) in lines
    assert "scopes missing: Forms, Project" in text
    assert "watchers: the operator user 9700 set, then cleared with clear_fields (no tenant tag is touched)" in text
    assert "unassign probe" not in text
    assert re.search(r"^requests: \d+ sent by the matrix, 0 refused by the guard$", text, re.MULTILINE)
    assert "result: nothing left over" not in lines  # an uploaded file stays in Gorelo: never claimed to be gone
    assert lines[-1] == "result: PASSED"


async def test_a_leftover_fails_the_run_even_when_every_area_passed(run, lines):
    fake = FakeGorelo()
    fake.fail("DELETE /v1/contacts/{contactId}", error_envelope(409, [("070901", "The contact has open tickets.")]))
    report = await run(fake, with_items=True, with_invoices=True)
    assert all(r.status == "pass" for r in report.results if r.area != "approved_invoice")
    assert not report.ok and report.exit_code == 1 and report.summary["leftovers"] == 1
    assert any("LEFTOVER   contact" in line for line in lines) and lines[-1] == "result: FAILED"
    assert not any(line.startswith("an Approved invoice is left over") for line in lines)  # only for an Approved invoice


async def test_the_api_key_never_appears_in_the_output(run, lines):
    fake = FakeGorelo()
    fake.fail("PATCH /v1/tickets/{ticketId}", error_envelope(400, [("070101", f"bad request, key {TEST_API_KEY}", "Title")]))
    report = await run(fake)
    assert results(report)["tickets"].status == "FAIL"
    assert not any(TEST_API_KEY in line for line in lines) and any("***" in line for line in lines)


# --------------------------------------------------------------------------
# One area at a time
# --------------------------------------------------------------------------


async def test_clients_only_the_temporary_client_is_created_with_the_region_and_updated_and_test_client_is_never_touched(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["clients"])
    assert report.ok
    assert area_ops(fake, report)["clients"] == GOLDEN["clients"]  # no GET of 9501 and no PATCH pair around it
    patches = [r.json for r in fake.mock.requests if r.method == "PATCH"]
    assert len(patches) == 1 and set(patches[0]) == {"AlternateName"} and patches[0]["AlternateName"].startswith(RUN)
    # the id is in the path only: UpdateClientCommand has no Id (contract e15cb5a18ec2)
    assert [r.path for r in fake.mock.requests if r.method == "PATCH"] == ["/v1/clients/8200"]
    assert fake.clients[TEST_CLIENT] == {"Id": TEST_CLIENT, "Name": "the test client", "AlternateName": "the test client alt", "BillingName": "the test client"}
    assert not any(r.path == f"/v1/clients/{TEST_CLIENT}" for r in fake.mock.requests)
    post = next(r.json for r in fake.mock.requests if r.method == "POST")
    assert post["Name"] == f"{RUN} temporary client"
    assert post["Location"]["Phone"] == "5555550142" and post["Location"]["PhoneCountryCode"] == "US"
    assert fake.clients[8200]["AlternateName"] == patches[0]["AlternateName"]
    assert any("location phone region stored as 'US'" in note for note in results(report)["clients"].notes)


async def test_clients_an_update_that_did_not_stick_on_the_temporary_client_is_caught(run):
    fake = FakeGorelo()
    fake.fail("PATCH /v1/clients/{clientId}", envelope({"Id": 8200, "Name": "x", "AlternateName": "unchanged"}))
    report = await run(fake, areas=["clients"])
    assert results(report)["clients"].status == "FAIL"
    assert "update_client AlternateName on the temporary client" in results(report)["clients"].detail
    assert not any(r.path == f"/v1/clients/{TEST_CLIENT}" for r in fake.mock.requests if r.method == "PATCH")
    assert fake.leftovers() == []  # the temporary client is still deleted by the cleanup


async def test_contacts_the_phone_survives_the_update_and_a_missing_secondary_email_choice_is_refused_locally(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["contacts"])
    assert report.ok
    contact = fake.contacts[8301]
    assert contact["JobTitle"] == f"{RUN} title" and contact["MobilePhone"] == "5555550142"
    assert contact["MobilePhoneCountryCode"] == "US"
    posts = [r.json for r in fake.mock.requests if r.method == "POST"]
    assert posts == [
        {
            "ClientId": TEST_CLIENT, "FirstName": RUN, "LastName": "Contact", "PrimaryEmail": f"{RUN.lower()}@example.invalid",
            "MobilePhone": "5555550142", "MobilePhoneCountryCode": "US",
        }
    ]
    assert results(report)["contacts"].requests == 4  # the refused update_contact sent nothing
    # the id is in the path only (UpdateContactCommand has no ContactId) and the body is the whole contact
    (patch,) = [r for r in fake.mock.requests if r.method == "PATCH"]
    assert patch.path == "/v1/contacts/8301" and "ContactId" not in patch.json and patch.json["ClientId"] == TEST_CLIENT
    assert {"FirstName", "LastName", "PrimaryEmail"} <= set(patch.json)


async def test_contacts_an_update_that_wipes_the_phone_is_caught(run):
    fake = FakeGorelo()
    original = fake.handlers["PATCH /v1/contacts/{contactId}"]

    def wipe(request):
        request.json = {key: value for key, value in request.json.items() if not key.startswith("Mobile")}
        return original(request)

    fake.handlers["PATCH /v1/contacts/{contactId}"] = wipe
    report = await run(fake, areas=["contacts"])
    assert results(report)["contacts"].status == "FAIL"
    assert "MobilePhone after the update" in results(report)["contacts"].detail


async def test_tickets_billing_read_fill_keeps_the_parts_the_second_update_did_not_name(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["tickets"])
    assert report.ok
    plain = next(t for t in fake.tickets.values() if t["Title"].startswith(RUN) and "plain" in t["Title"])
    override = plain["BillingOverride"]
    assert override["BillingRole"]["Id"] == 11 and override["WorkType"]["Id"] == 21 and override["BillableStatus"]["Id"] == 2
    patches = [r.json for r in fake.mock.requests if r.method == "PATCH"]
    assert patches[-2] == {"BillingOverride": {"BillingRoleId": 11, "WorkTypeId": 21}}
    assert patches[-1] == {"BillingOverride": {"BillableStatusId": 2, "BillingRoleId": 11, "WorkTypeId": 21}}


async def test_tickets_a_billing_update_that_loses_the_other_parts_is_caught(run, monkeypatch):
    from tools import tickets

    async def forgets_the_current_billing(client, ticket):
        return {}

    monkeypatch.setattr(tickets, "_current_billing", forgets_the_current_billing)
    report = await run(FakeGorelo(), areas=["tickets"])
    assert results(report)["tickets"].status == "FAIL"
    assert "BillingRole kept by the read-fill: expected 11, got None" in results(report)["tickets"].detail


async def test_tickets_create_bodies_resolve_names_and_the_backdated_ticket_is_closed_in_the_past(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["tickets"])
    assert report.ok
    plain, old = (r.json for r in fake.mock.requests if r.method == "POST")
    for body in (plain, old):
        assert body["ClientId"] == TEST_CLIENT and body["TypeId"] == 7101 and body["GroupId"] == 7201
        assert body["SourceId"] == 6 and body["PriorityId"] == 3 and body["SendTicketCreatedEmail"] is False
        assert "ContactId" not in body
    assert plain["StatusId"] == 1 and "CreatedOn" not in plain
    assert old["StatusId"] == 4
    created, closed = (datetime.fromisoformat(old[key].replace("Z", "+00:00")) for key in ("CreatedOn", "ClosedOn"))
    now = datetime.now(timezone.utc)
    assert created < closed < now and (now - created).days >= 2


async def test_tickets_the_watcher_is_set_then_cleared_with_clear_fields_and_only_then_the_lead_is_set(run):
    # live, 2026-10-02: lead_assignee_id 9700 and watcher_ids [9700] in one PATCH is a 400 "Technician already exists"
    fake = FakeGorelo()
    report = await run(fake, areas=["tickets"])
    assert report.ok, results(report)["tickets"].detail
    patches = [r.json for r in fake.mock.requests if r.method == "PATCH"]
    assert patches[0] == {"Title": f"{RUN} plain ticket (updated)", "WatcherIds": [OPERATOR_USER]}
    assert patches[1] == {"WatcherIds": []}
    assert patches[2] == {"LeadAssigneeId": OPERATOR_USER}
    assert not [body for body in patches if "LeadAssigneeId" in body and "WatcherIds" in body]  # never both in one PATCH
    plain = next(t for t in fake.tickets.values() if t["Title"].startswith(RUN) and "plain" in t["Title"])
    assert plain["WatcherIds"] == [] and plain["LeadAssigneeId"] == OPERATOR_USER
    notes = results(report)["tickets"].notes
    assert any("the operator user 9700 set, then cleared with clear_fields" in note for note in notes)
    assert any("lead assignee: the operator user 9700 set in a later update, after the watcher was cleared" in n for n in notes)


async def test_tickets_the_fake_refuses_a_technician_who_is_lead_and_watcher_like_production_does(run):
    # the fake models the live rule, so the passing runs above prove the matrix keeps the two roles apart
    fake = FakeGorelo()
    ticket = fake.add_ticket({"Title": "t", "ClientId": TEST_CLIENT, "StatusId": 1, "TypeId": 7101, "GroupId": 7201})
    path = f"/v1/tickets/{ticket['Id']}"
    both = httpx.Request("PATCH", f"https://api.usw.gorelo.io{path}", json={"LeadAssigneeId": 9700, "WatcherIds": [9700]})
    response = httpx.Client(transport=fake.mock.transport).send(both)
    assert response.status_code == 400
    assert response.json()["Notifications"][0]["Message"] == "Technician already exists"
    assert ticket["LeadAssigneeId"] is None and ticket["WatcherIds"] == []  # nothing was applied
    for body in ({"WatcherIds": [9700]}, {"WatcherIds": []}, {"LeadAssigneeId": 9700}):
        sent = httpx.Request("PATCH", f"https://api.usw.gorelo.io{path}", json=body)
        assert httpx.Client(transport=fake.mock.transport).send(sent).status_code == 200
    again = httpx.Request("PATCH", f"https://api.usw.gorelo.io{path}", json={"WatcherIds": [9700]})  # the lead is 9700 now
    assert httpx.Client(transport=fake.mock.transport).send(again).status_code == 400


async def test_tickets_a_technician_conflict_from_gorelo_fails_the_area_after_the_watcher_was_cleared(run):
    fake = FakeGorelo()
    original = fake.handlers["PATCH /v1/tickets/{ticketId}"]

    def refuses_the_lead(request):
        if "LeadAssigneeId" in request.json:
            return error_envelope(400, [("070101", "Technician already exists", "LeadAssigneeId")])
        return original(request)

    fake.handlers["PATCH /v1/tickets/{ticketId}"] = refuses_the_lead
    report = await run(fake, areas=["tickets"])
    found = results(report)["tickets"]
    assert found.status == "FAIL" and "update_ticket" in found.detail and "lead_assignee_id: Technician already exists" in found.detail
    assert [r.json for r in fake.mock.requests if r.method == "PATCH"] == [
        {"Title": f"{RUN} plain ticket (updated)", "WatcherIds": [OPERATOR_USER]}, {"WatcherIds": []}, {"LeadAssigneeId": OPERATOR_USER},
    ]
    assert fake.leftovers() == []  # the cleanup still ran


async def test_tickets_no_tenant_tag_and_no_unassign_probe_are_ever_sent(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["tickets"])
    assert report.ok
    assert not any(r.path == "/v1/tickets/tags" for r in fake.mock.requests)  # not even looked up (the fake has no route)
    assert all("TagIds" not in (r.json or {}) for r in fake.mock.requests)
    assert all((r.json or {}).get("LeadAssigneeId") in (None, OPERATOR_USER) for r in fake.mock.requests)
    assert not fake.mock.unmatched
    notes = " | ".join(results(report)["tickets"].notes)
    assert "unassign probe" not in notes and "tags:" not in notes


async def test_tickets_a_watcher_that_did_not_stick_is_caught_before_the_clear(run):
    fake = FakeGorelo()
    original = fake.handlers["PATCH /v1/tickets/{ticketId}"]

    def ignores_watchers(request):
        request.json = {key: value for key, value in request.json.items() if key != "WatcherIds"}
        return original(request)

    fake.handlers["PATCH /v1/tickets/{ticketId}"] = ignores_watchers
    report = await run(fake, areas=["tickets"])
    assert results(report)["tickets"].status == "FAIL"
    assert "update_ticket WatcherIds: expected [9700], got []" in results(report)["tickets"].detail
    assert len([r for r in fake.mock.requests if r.method == "PATCH"]) == 1  # the area stopped before the clear


async def test_tickets_a_clear_that_leaves_the_watcher_on_the_ticket_is_caught(run):
    fake = FakeGorelo()
    original = fake.handlers["PATCH /v1/tickets/{ticketId}"]

    def keeps_the_watcher(request):
        if request.json == {"WatcherIds": []}:
            return envelope({"Id": fake.live_ticket(request)["Id"]})  # answers 200 and changes nothing
        return original(request)

    fake.handlers["PATCH /v1/tickets/{ticketId}"] = keeps_the_watcher
    report = await run(fake, areas=["tickets"])
    assert results(report)["tickets"].status == "FAIL"
    assert "WatcherIds after clear_fields=['watcher_ids']: expected [], got [9700]" in results(report)["tickets"].detail
    assert len([r for r in fake.mock.requests if r.method == "PATCH"]) == 2  # the billing steps never started


async def test_tickets_a_number_lookup_that_is_too_early_is_retried(run):
    fake = FakeGorelo(lookup_lag=1)
    report = await run(fake, areas=["tickets"])
    assert report.ok
    assert any("get_ticket by number found the new ticket on attempt 2" in note for note in results(report)["tickets"].notes)
    queries = [r.query["Query"] for r in fake.mock.requests if r.method == "GET" and r.path == "/v1/tickets" and "Query" in r.query]
    assert queries[0] == queries[1] == "3003" and queries[2] == "TCK-3003"


async def test_tickets_a_lookup_that_never_finds_the_ticket_fails_after_three_attempts(run):
    fake = FakeGorelo(lookup_lag=99)
    report = await run(fake, areas=["tickets"])
    assert results(report)["tickets"].status == "FAIL" and "no ticket has the number" in results(report)["tickets"].detail
    assert len([r for r in fake.mock.requests if r.path == "/v1/tickets" and "Query" in r.query]) == 3


async def test_tickets_search_falls_back_to_the_run_id_when_a_big_client_truncates_the_scan(run):
    fake = FakeGorelo(other_tickets=600, oldest_first=True)
    report = await run(fake, areas=["tickets"])
    assert report.ok, results(report)["tickets"].detail
    searches = [r for r in fake.mock.requests if r.method == "GET" and r.path == "/v1/tickets" and "ClientIds" in r.query]
    assert searches[-1].query["Query"] == RUN and searches[0].query["ClientIds"] == "9501"
    assert any("run id was added as a query" in note for note in results(report)["tickets"].notes)


async def test_comments_private_comment_is_listed_read_and_deleted_and_the_attachment_is_attached(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["comments"])
    assert report.ok
    ticket = next(t["Id"] for t in fake.tickets.values() if t["Title"] == f"{RUN} plain ticket")
    comments = list(fake.comments[ticket].values())
    assert [c["ConversationType"]["Id"] for c in comments] == [2, 2]
    assert ("comment", comments[0]["Id"]) in fake.deleted
    assert comments[1]["Attachments"] == [{"Name": f"{RUN}-attachment.txt", "Url": f"https://files.example.invalid/{RUN}-attachment.txt?token=abc123"}]
    assert fake.attachments == [{"name": f"{RUN}-attachment.txt", "item": ticket, "type": "Ticket", "size": len(f"{RUN} attachment: a small text file.")}]
    listing = next(r for r in fake.mock.requests if r.method == "GET" and r.path.endswith("/comments"))
    assert listing.query["ConversationType"] == "2"


async def test_the_uploaded_attachment_is_known_undeletable_not_cleaned_and_never_alone_a_failure(run, lines):
    fake = FakeGorelo()
    report = await run(fake, areas=["comments"])
    assert report.ok and report.exit_code == 0
    # the plain ticket, two private comments and the upload: three are cleaned, the file cannot be
    assert report.summary == {"created": 4, "cleaned": 3, "leftovers": 0, "undeletable": 1, "unresolved_intents": 0}
    outcome = "soft-deleted with its ticket; the file itself cannot be deleted through the API"
    assert [(r.kind, r.id, r.outcome) for r in report.cleanup.undeletable] == [("attachment", f"{RUN}-attachment.txt", outcome)]
    assert report.cleanup.leftovers == [] and report.cleanup.ok
    assert f"known undeletable: attachment {RUN}-attachment.txt: {outcome}" in lines  # the matrix summary
    assert any(line.startswith("  known undeletable attachment ") and line.endswith(outcome) for line in lines)  # the cleanup
    assert "result: nothing left over" not in lines  # a file stays in Gorelo: the cleanup never claims otherwise
    assert "result: cleanup complete; 1 known undeletable record remains (see above)" in lines
    assert lines[-1] == "result: PASSED"


async def test_an_attachment_whose_ticket_could_not_be_deleted_is_a_plain_leftover(run, lines):
    fake = FakeGorelo()
    fake.handlers["DELETE /v1/tickets/{ticketId}"] = lambda request: error_envelope(409, [("070901", "The ticket is locked.")])
    report = await run(fake, areas=["comments"])
    assert not report.ok and report.exit_code == 1
    assert sorted(r.kind for r in report.cleanup.leftovers) == ["attachment", "ticket"]
    assert report.cleanup.undeletable == [] and report.summary["undeletable"] == 0 and report.summary["leftovers"] == 2
    assert "result: SOMETHING IS LEFT OVER" in lines and lines[-1] == "result: FAILED"


async def test_email_runs_on_second_with_the_operator_contact_only(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["email"])
    assert report.ok, results(report)["email"].detail
    ticket = next(t for t in fake.tickets.values() if t["Title"] == f"{RUN} email ticket")
    assert ticket["ClientId"] == SECOND and ticket["ContactId"] == OPERATOR_CONTACT and ticket["Status"]["Id"] == 2
    create = next(r.json for r in fake.mock.requests if r.method == "POST" and r.path == "/v1/tickets")
    assert create["SendTicketCreatedEmail"] is True and create["ContactId"] == OPERATOR_CONTACT and create["ClientId"] == SECOND
    comments = list(fake.comments[ticket["Id"]].values())
    assert [c["ConversationType"]["Id"] for c in comments] == [1, 3, 4]
    assert comments[1]["ConversationId"] == "700"  # the tool sends conversation ids as text
    assert comments[2]["ConversationId"] in fake.approvals
    side = next(r.json for r in fake.mock.requests if r.path.endswith("/side-conversation"))
    assert side == {"Name": f"{RUN} side conversation", "Email": OPERATOR_EMAIL}
    approval = next(r.json for r in fake.mock.requests if r.path.endswith("/approval"))
    assert approval == {"Name": f"{RUN} approval", "ContactIds": [OPERATOR_CONTACT]}
    assert any("public comment email status: 'Sent'" in note for note in results(report)["email"].notes)


async def test_email_an_approver_gorelo_rejects_is_a_recorded_skip_not_a_failure(run, lines):
    fake = FakeGorelo(approver=False)
    report = await run(fake, areas=["email"])
    assert report.ok
    assert any("approval: skipped: approver not eligible (Gorelo said:" in note for note in results(report)["email"].notes)
    # live, 2026-10-02: ticket approvals refuse a contact without the approver contact tag, in these words
    assert any("carry a contact tag marked as approver" in note for note in results(report)["email"].notes)
    assert not any(r.path.endswith("/approval") and r.method == "GET" for r in fake.mock.requests)
    ticket = next(t["Id"] for t in fake.tickets.values() if t["Title"] == f"{RUN} email ticket")
    assert [c["ConversationType"]["Id"] for c in fake.comments[ticket].values()] == [1, 3]  # no approval comment
    assert report.summary["unresolved_intents"] == 0


async def test_email_a_rejection_that_is_not_about_the_approver_still_fails_the_area(run):
    fake = FakeGorelo()
    fake.fail("POST /v1/tickets/{ticketId}/conversations/approval", error_envelope(404, [("070404", "ticket gone")]))
    report = await run(fake, areas=["email"])
    assert results(report)["email"].status == "FAIL" and "create_ticket_approval" in results(report)["email"].detail


# --------------------------------------------------------------------------
# Who a ticket would email is checked on what Gorelo STORED, before anything else is sent for it
# --------------------------------------------------------------------------

LOOKUPS = ["GET /v1/tickets/statuses", "GET /v1/tickets/types", "GET /v1/organization/groups"]
CREATE_AND_READ_BACK = ["POST /v1/tickets", "GET /v1/tickets/{ticketId}"]


def stored(client: int, **changes):
    """A ticket hook: the tickets of `client` are STORED with these values, whatever the create asked for."""

    def hook(ticket):
        if ticket["ClientId"] == client:
            ticket.update(changes)

    return hook


def public_comment(ticket: str) -> httpx.Request:
    """The request a public comment on `ticket` would send, for asking the real guard about it."""
    return httpx.Request(
        "POST",
        f"https://api.usw.gorelo.io/v1/tickets/{ticket}/comments",
        json={"Body": "<p>x</p>", "ConversationTypeId": 1},
    )


def run_tickets(manifest: Manifest) -> dict:
    return {record.label: record for record in manifest.all_created() if record.kind == "ticket"}


@pytest.mark.parametrize(
    "changes, said",
    [
        ({"ContactId": 777}, "ContactId is 777, the matrix asked for none"),
        ({"ContactId": OPERATOR_CONTACT}, "ContactId is 9600, the matrix asked for none"),
        ({"CcContactIds": [777]}, "CcContactIds holds [777], the matrix asked for none"),
        ({"CcContactIds": [OPERATOR_CONTACT]}, "CcContactIds holds [9600], the matrix asked for none"),
        (
            {"ContactId": 777, "CcContactIds": [888]},
            "ContactId is 777, the matrix asked for none; CcContactIds holds [888], the matrix asked for none",
        ),
    ],
)
async def test_tickets_a_plain_ticket_stored_with_an_audience_ends_the_area_before_anything_else(run, changes, said):
    fake = FakeGorelo()
    fake.on_ticket_created(stored(TEST_CLIENT, **changes))
    report = await run(fake, with_items=True)
    found = results(report)
    assert found["tickets"].status == "FAIL" and said in found["tickets"].detail
    assert "before any comment, status change or approval" in found["tickets"].detail
    assert area_ops(fake, report)["tickets"] == LOOKUPS + CREATE_AND_READ_BACK  # not one request after the read-back
    for area in ("comments", "time"):  # they build on that ticket: they never start
        assert found[area].status == "skipped" and found[area].detail.startswith("no run ticket: ")
        assert found[area].requests == 0
    assert all(found[area].status == "pass" for area in ("clients", "contacts", "email", "items", "uptime", "projects"))
    assert not report.ok and report.exit_code == 1
    assert fake.leftovers() == [] and report.summary["unresolved_intents"] == 0  # the ticket is still cleaned up
    (plain,) = (record for label, record in run_tickets(Manifest.load(report.manifest_path)).items() if "plain" in label)
    assert plain.details == {
        "client_id": TEST_CLIENT,
        "contact_id": changes.get("ContactId"),
        "cc_contact_ids": changes.get("CcContactIds", []),
    }  # the manifest holds what is STORED, wrong as it is


async def test_tickets_the_stored_audience_reaches_the_manifest_so_the_guard_refuses_a_public_comment(run):
    fake = FakeGorelo()
    fake.on_ticket_created(stored(TEST_CLIENT, ContactId=777))
    report = await run(fake, areas=["tickets"])
    manifest = Manifest.load(report.manifest_path)
    (plain,) = run_tickets(manifest).values()
    assert plain.details["contact_id"] == 777
    with pytest.raises(GuardViolation, match=r"a public comment would email contact\(s\) 777"):
        LiveGuard("write", manifest).check(public_comment(plain.id))


async def test_tickets_a_backdated_ticket_stored_with_a_contact_ends_the_area_right_after_its_read_back(run):
    fake = FakeGorelo()
    fake.on_ticket_created(lambda ticket: ticket.update(ContactId=777) if ticket["ClosedOn"] else None)
    report = await run(fake, areas=["tickets"])
    found = results(report)["tickets"]
    assert found.status == "FAIL" and "ContactId is 777, the matrix asked for none" in found.detail
    # the plain ticket passed its check; the backdated one stopped the area after its own read-back
    assert area_ops(fake, report)["tickets"] == LOOKUPS + CREATE_AND_READ_BACK * 2
    assert not any(r.method == "PATCH" for r in fake.mock.requests)
    assert fake.leftovers() == []


@pytest.mark.parametrize(
    "changes, said",
    [
        ({"ContactId": None}, "ContactId is none, the matrix asked for 9600"),
        ({"ContactId": 777}, "ContactId is 777, the matrix asked for 9600"),
        ({"CcContactIds": [777]}, "CcContactIds holds [777], the matrix asked for only 9600"),
        ({"CcContactIds": [OPERATOR_CONTACT, 777]}, "CcContactIds holds [777], the matrix asked for only 9600"),
    ],
)
async def test_email_a_ticket_stored_with_another_audience_ends_the_area_before_any_comment_status_or_approval(
    run, changes, said
):
    fake = FakeGorelo()
    fake.on_ticket_created(stored(SECOND, **changes))
    report = await run(fake, areas=["email"])
    found = results(report)["email"]
    assert found.status == "FAIL" and said in found.detail and not report.ok
    assert area_ops(fake, report)["email"] == LOOKUPS + CREATE_AND_READ_BACK
    ticket = next(t for t in fake.tickets.values() if t["Title"] == f"{RUN} email ticket")
    assert fake.comments[ticket["Id"]] == {} and fake.conversations[ticket["Id"]] == [] and fake.approvals == {}
    assert ticket["Status"]["Id"] == 1  # still New: no status change either
    assert not any(r.method == "PATCH" or "/comments" in r.path or "/conversations/" in r.path for r in fake.mock.requests)
    assert fake.leftovers() == [] and report.summary["unresolved_intents"] == 0
    (email,) = run_tickets(Manifest.load(report.manifest_path)).values()
    assert email.details["contact_id"] == changes.get("ContactId", OPERATOR_CONTACT)  # what is stored, not what was asked


async def test_email_a_stored_cc_of_the_operator_contact_alone_is_fine_and_goes_into_the_manifest(run):
    fake = FakeGorelo()
    fake.on_ticket_created(stored(SECOND, CcContactIds=[OPERATOR_CONTACT]))
    report = await run(fake, areas=["email"])
    assert report.ok, results(report)["email"].detail
    (email,) = run_tickets(Manifest.load(report.manifest_path)).values()
    assert email.details == {"client_id": SECOND, "contact_id": OPERATOR_CONTACT, "cc_contact_ids": [OPERATOR_CONTACT]}


UNREADABLE = {
    "no ContactId": lambda ticket: ticket.pop("ContactId"),
    "no CcContactIds": lambda ticket: ticket.pop("CcContactIds"),
    "ContactId as text": lambda ticket: ticket.update(ContactId="9600"),
    "ContactId true": lambda ticket: ticket.update(ContactId=True),
    "CcContactIds null": lambda ticket: ticket.update(CcContactIds=None),
    "CcContactIds not ids": lambda ticket: ticket.update(CcContactIds=[True]),
}


@pytest.mark.parametrize("how", sorted(UNREADABLE))
@pytest.mark.parametrize("area, client", [("tickets", TEST_CLIENT), ("email", SECOND)])
async def test_a_stored_audience_that_cannot_be_read_ends_the_area_and_leaves_the_manifest_unknown(run, area, client, how):
    fake = FakeGorelo()
    damage = UNREADABLE[how]
    fake.on_ticket_created(lambda ticket: damage(ticket) if ticket["ClientId"] == client else None)
    report = await run(fake, areas=[area])
    found = results(report)[area]
    assert found.status == "FAIL" and "cannot be read as contact ids" in found.detail
    assert area_ops(fake, report)[area] == LOOKUPS + CREATE_AND_READ_BACK
    manifest = Manifest.load(report.manifest_path)
    (ticket,) = run_tickets(manifest).values()
    assert ticket.details == {"client_id": client}  # nothing was claimed about who the ticket emails
    with pytest.raises(GuardViolation, match="manifest has no contact_id"):
        LiveGuard("write", manifest).check(public_comment(ticket.id))
    assert fake.leftovers() == []


@pytest.mark.parametrize("area, client", [("tickets", TEST_CLIENT), ("email", SECOND)])
async def test_a_ticket_whose_read_back_failed_is_not_used_for_anything(run, area, client):
    fake = FakeGorelo()
    fake.fail("GET /v1/tickets/{ticketId}", error_envelope(500, [("070500", "boom")]))
    report = await run(fake, areas=[area])
    found = results(report)[area]
    assert found.status == "FAIL" and "reading it back failed" in found.detail
    assert area_ops(fake, report)[area] == LOOKUPS + CREATE_AND_READ_BACK
    manifest = Manifest.load(report.manifest_path)
    (ticket,) = run_tickets(manifest).values()
    assert ticket.details == {"client_id": client}  # created and recorded, audience unknown
    assert fake.leftovers() == [] and report.summary["unresolved_intents"] == 0


async def test_the_stored_audience_is_written_to_the_manifest_right_after_each_read_back(run, monkeypatch):
    fake = FakeGorelo()
    real = Manifest.update_details

    def spy(self, kind, ident, **changes):
        fake.events.append(("details", kind, changes))
        return real(self, kind, ident, **changes)

    monkeypatch.setattr(Manifest, "update_details", spy)
    report = await run(fake, areas=["tickets", "email"])
    assert report.ok
    updates = [i for i, event in enumerate(fake.events) if event[0] == "details"]
    assert len(updates) == 3  # the plain, the backdated and the email ticket
    for index, wanted in zip(updates, [None, None, OPERATOR_CONTACT]):
        before = fake.events[index - 1]
        assert before[:2] == ("request", "GET") and before[2].startswith("/v1/tickets/")  # the create's read-back
        assert fake.events[index][1:] == ("ticket", {"contact_id": wanted, "cc_contact_ids": []})
    notes = [note for result in report.results for note in result.notes]
    assert "plain ticket: stored audience verified (ContactId None, CcContactIds [])" in notes
    assert f"email ticket: stored audience verified (ContactId {OPERATOR_CONTACT}, CcContactIds [])" in notes


async def test_a_healthy_run_leaves_the_guard_able_to_see_who_each_ticket_emails(run):
    fake = FakeGorelo()
    report = await run(fake)
    manifest = Manifest.load(report.manifest_path)
    guard = LiveGuard("write", manifest)
    for record in run_tickets(manifest).values():  # nobody, nobody and the operator contact: all fine to comment on
        guard.check(public_comment(record.id))
    fake2 = FakeGorelo()
    fake2.on_ticket_created(stored(TEST_CLIENT, ContactId=777, CcContactIds=[OPERATOR_CONTACT]))
    report2 = await run(fake2, areas=["tickets"], started=datetime(2099, 10, 2, 10, 18, 0, tzinfo=timezone.utc))
    (plain,) = run_tickets(Manifest.load(report2.manifest_path)).values()
    with pytest.raises(GuardViolation, match=r"would email contact\(s\) 777"):
        LiveGuard("write", Manifest.load(report2.manifest_path)).check(public_comment(plain.id))


# --------------------------------------------------------------------------
# the approval comment, and the conversations the guard lets a comment go into
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored_approvers, why",
    [
        ([777], "the stored approvers include contact(s) 777, not only the operator contact 9600"),
        ([OPERATOR_CONTACT, 888, 777], "the stored approvers include contact(s) 777, 888, not only the operator contact 9600"),
        ([], "the stored approval has no readable list of approvers"),
    ],
)
async def test_email_an_approval_stored_with_other_approvers_gets_no_comment_and_the_reason_is_recorded(
    run, stored_approvers, why
):
    fake = FakeGorelo(stored_approvers=stored_approvers)
    report = await run(fake, areas=["email"])
    assert report.ok  # a recorded skip, like a rejected approver
    notes = results(report)["email"].notes
    assert any(f"approval comment: skipped: {why}" in note and "nobody was emailed" in note for note in notes)
    # the approval's read-back is the last request of the area: no approval comment follows it
    assert area_ops(fake, report)["email"] == LOOKUPS + GOLDEN["email"][:-2]
    assert area_ops(fake, report)["email"][-1] == "GET /v1/tickets/{ticketId}/approvals/{approvalId}"
    ticket = next(t["Id"] for t in fake.tickets.values() if t["Title"] == f"{RUN} email ticket")
    assert [c["ConversationType"]["Id"] for c in fake.comments[ticket].values()] == [1, 3]
    manifest = Manifest.load(report.manifest_path)
    assert [r.details["private"] for r in manifest.all_created() if r.kind == "comment"] == [False, False]
    assert report.summary["unresolved_intents"] == 0 and fake.leftovers() == []


@pytest.mark.parametrize(
    "answer, why",
    [
        (lambda approval: approval.pop("Approvers"), "the stored approval has no readable list of approvers (Approvers is missing"),
        (lambda approval: approval.update(Approvers=None), "the stored approval has no readable list of approvers (Approvers is NoneType"),
        (lambda approval: approval.update(Approvers="9600"), "the stored approval has no readable list of approvers (Approvers is str"),
        (lambda approval: approval.update(Approvers=[{"Status": {"Id": 1}}]), "an approver of the stored approval has no readable ContactId"),
        (lambda approval: approval.update(Approvers=["9600"]), "an approver of the stored approval has no readable ContactId"),
        (lambda approval: approval.update(Approvers=[{"ContactId": "9600"}]), "an approver of the stored approval has no readable ContactId"),
    ],
)
async def test_email_an_approval_whose_approvers_cannot_be_read_gets_no_comment_either(run, answer, why):
    fake = FakeGorelo()
    original = fake.handlers["GET /v1/tickets/{ticketId}/approvals/{approvalId}"]

    def damaged(request):
        record = dict(fake.approvals[fake.part(request, 5)])
        answer(record)
        return envelope(record)

    fake.handlers["GET /v1/tickets/{ticketId}/approvals/{approvalId}"] = damaged
    assert original is not damaged
    report = await run(fake, areas=["email"])
    assert report.ok
    assert any(f"approval comment: skipped: {why}" in note for note in results(report)["email"].notes)
    assert area_ops(fake, report)["email"][-1] == "GET /v1/tickets/{ticketId}/approvals/{approvalId}"
    assert not any(r.method == "POST" and r.json and r.json.get("ConversationTypeId") == 4 for r in fake.mock.requests)


async def test_email_an_approval_read_back_that_failed_fails_the_area_and_posts_no_comment(run):
    fake = FakeGorelo()
    fake.fail("GET /v1/tickets/{ticketId}/approvals/{approvalId}", error_envelope(500, [("070500", "boom")]))
    report = await run(fake, areas=["email"])
    assert results(report)["email"].status == "FAIL" and "reading it back failed" in results(report)["email"].detail
    assert area_ops(fake, report)["email"] == LOOKUPS + GOLDEN["email"][:-2]
    assert not any(r.method == "POST" and r.json and r.json.get("ConversationTypeId") == 4 for r in fake.mock.requests)


async def test_email_the_conversations_are_recorded_so_the_guard_lets_only_their_own_comments_in(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["email"])
    assert report.ok
    manifest = Manifest.load(report.manifest_path)
    (email,) = run_tickets(manifest).values()
    (side,) = (r for r in manifest.all_created() if r.kind == "side_conversation")
    (approval,) = (r for r in manifest.all_created() if r.kind == "approval")
    assert side.details == approval.details == {"ticket_id": email.id}
    guard = LiveGuard("write", manifest)

    def comment_into(kind, conversation):
        body = {"Body": "<p>x</p>", "ConversationTypeId": kind, "ConversationId": conversation}
        return httpx.Request("POST", f"https://api.usw.gorelo.io/v1/tickets/{email.id}/comments", json=body)

    guard.check(comment_into(3, str(side.id)))  # the tool sends a side conversation's number as text
    guard.check(comment_into(4, approval.id))
    for kind, conversation, match in (
        (3, "999", "is not one of the side conversations this run created"),
        (3, approval.id, "is not one of the side conversations this run created"),
        (4, str(side.id), "is not one of the approvals this run created"),
        (4, "00000000-0000-4000-8000-000000000000", "is not one of the approvals this run created"),
    ):
        with pytest.raises(GuardViolation, match=match):
            guard.check(comment_into(kind, conversation))


@pytest.mark.parametrize("reopens, calls", [(0, 1), (1, 2), (2, 3)])
async def test_time_delete_is_repeated_while_the_entry_comes_back_reopened(run, reopens, calls):
    fake = FakeGorelo(reopens=reopens)
    report = await run(fake, areas=["time"])
    assert report.ok
    assert len([r for r in fake.mock.requests if r.method == "DELETE" and "/time-entries/" in r.path]) == calls
    assert report.summary["unresolved_intents"] == 0 and fake.leftovers() == []


async def test_time_a_delete_that_stays_reopened_is_given_up_and_the_cleanup_finds_it_still_open(run, lines):
    fake = FakeGorelo(reopens=99)
    report = await run(fake, areas=["time"])
    assert results(report)["time"].status == "FAIL" and "still Reopened after 3 delete calls" in results(report)["time"].detail
    deletes = [r for r in fake.mock.requests if r.method == "DELETE" and "/time-entries/" in r.path]
    assert len(deletes) == 6  # three from the area, three from the cleanup
    assert not report.ok and report.summary["leftovers"] == 1


async def test_time_the_entry_is_created_for_the_operator_without_a_service_line(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["time"])
    assert report.ok
    body = next(r.json for r in fake.mock.requests if r.method == "POST" and r.path == "/v1/time-entries")
    ticket = next(t["Id"] for t in fake.tickets.values() if t["Title"] == f"{RUN} plain ticket")
    assert body["UserId"] == OPERATOR_USER and body["TicketId"] == ticket and body["BillableStatusId"] == 2
    assert "ServiceLineId" in body and body["ServiceLineId"] is None
    assert body["StartedOn"].endswith("Z") and body["EndedOn"].endswith("Z") and body["StartedOn"] < body["EndedOn"]
    assert any("get_time_entry after the delete: HTTP 404" in note for note in results(report)["time"].notes)


async def test_time_the_published_shape_passes_with_no_note_about_drift(run):
    # production answers the published TimeEntryModel since 2026-10-03, and the fake serves it by default: the
    # user is {Id, Name}, the ticket {Id, Number, Title} (the run ticket), the task null for a ticket entry, and there
    # is no flat UserId, TicketId or TaskId
    fake = FakeGorelo()
    report = await run(fake, areas=["time"])
    found = results(report)["time"]
    assert report.ok and found.status == "pass" and fake.leftovers() == []
    ticket = next(t for t in fake.tickets.values() if t["Title"] == f"{RUN} plain ticket")
    (entry,) = fake.time_entries.values()
    assert entry["User"] == {"Id": OPERATOR_USER, "Name": "Operator"}
    assert write_matrix.same_guid(entry["Ticket"]["Id"], ticket["Id"])
    assert set(entry["Ticket"]) == {"Id", "Number", "Title"} and entry["Ticket"]["Title"] == ticket["Title"]
    assert entry["Task"] is None
    assert not {"UserId", "TicketId", "TaskId"} & set(entry)
    model = SPEC.schema("TimeEntryModel")["fields"]  # the fake serves what the published model has, nothing else
    assert {key for key in entry if not key.startswith("_")} <= set(model) and {"User", "Ticket", "Task"} <= set(entry)
    for name, kind in (("User", "CodeModel"), ("Ticket", "NumberedReferenceModel"), ("Task", "NumberedReferenceModel")):
        assert model[name]["ref"] == kind
    assert set(entry["User"]) == set(SPEC.schema("CodeModel")["fields"])
    assert set(entry["Ticket"]) == set(SPEC.schema("NumberedReferenceModel")["fields"])
    assert not any("flat" in note or "drift" in note for note in found.notes)


@pytest.mark.parametrize(
    "reshape",
    [
        pytest.param(make_flat, id="no-user-object"),
        pytest.param(lambda record: (make_flat(record), record.update(User=None)), id="user-object-null"),
    ],
)
async def test_time_the_flat_user_id_of_2026_10_02_passes_with_a_note_that_it_is_drift(run, reshape):
    fake = FakeGorelo()
    reshape_entry_reads(fake, reshape)
    report = await run(fake, areas=["time"])
    found = results(report)["time"]
    assert report.ok and found.status == "pass" and fake.leftovers() == []
    assert [note for note in found.notes if note.startswith("time entry user:")] == [
        "time entry user: live returned the flat UserId 9700 (the 2026-10-02 shape) instead of the published User "
        "object {Id, Name}; drift worth knowing"
    ]


@pytest.mark.parametrize(
    "damage, fragment",
    [
        pytest.param(
            lambda entry: entry.update(User={"Id": 9202, "Name": "Sam Sample"}),
            "time entry User.Id: expected 9700, got 9202",
            id="another-user-in-the-user-object",
        ),
        pytest.param(
            lambda entry: entry.update(User={"Name": "Operator"}),
            "time entry User.Id: expected 9700, got None",
            id="a-user-object-without-an-id",
        ),
        pytest.param(
            lambda entry: entry.update(User="Operator"),
            "time entry User: expected an object {Id, Name}, got str",
            id="a-user-that-is-not-an-object",
        ),
        pytest.param(
            lambda entry: (make_flat(entry), entry.update(UserId=9202)),
            "time entry UserId: expected 9700, got 9202",
            id="another-flat-user-id",
        ),
        pytest.param(
            lambda entry: entry.pop("User"),
            "got neither: User is missing and UserId is missing",
            id="neither-field",
        ),
        pytest.param(
            lambda entry: entry.update(User=None),
            "got neither: User is null and UserId is missing",
            id="a-null-user-object-and-no-flat-id",
        ),
        pytest.param(
            lambda entry: (make_flat(entry), entry.update(UserId=None)),
            "got neither: User is missing and UserId is null",
            id="a-null-flat-id",
        ),
    ],
)
async def test_time_an_entry_that_does_not_read_back_as_the_operators_fails_the_area(run, damage, fragment):
    fake = FakeGorelo()
    reshape_entry_reads(fake, damage)
    report = await run(fake, areas=["time"])
    found = results(report)["time"]
    assert found.status == "FAIL" and fragment in found.detail
    assert "Sam" not in found.detail and "Operator" not in found.detail  # ids and kinds only, never a user's name
    assert not any(note.startswith("time entry user:") for note in found.notes)
    assert fake.leftovers() == []  # the entry made before the check is still deleted by the cleanup


@pytest.mark.parametrize(
    "record, shape",
    [
        pytest.param({"User": {"Id": OPERATOR_USER, "Name": "x"}}, write_matrix.PUBLISHED_SHAPE, id="published"),
        pytest.param(
            {"User": {"Id": OPERATOR_USER, "Name": "x"}, "UserId": OPERATOR_USER},
            write_matrix.BOTH_SHAPES,
            id="both-agree",
        ),
        pytest.param({"UserId": OPERATOR_USER}, write_matrix.FLAT_SHAPE, id="flat"),
        pytest.param({"User": None, "UserId": OPERATOR_USER}, write_matrix.FLAT_SHAPE, id="null-object-and-a-flat-id"),
    ],
)
def test_entry_user_shape_says_how_the_operators_entry_names_its_user(record, shape):
    assert write_matrix.entry_user_shape(record) == shape


def test_a_flat_user_id_that_disagrees_with_the_user_object_fails_and_names_both():
    with pytest.raises(write_matrix.CheckFailed) as caught:
        write_matrix.entry_user_shape({"User": {"Id": OPERATOR_USER, "Name": "Zelda Quorum"}, "UserId": 9202})
    assert "UserId (next to the User object)" in str(caught.value) and "9202" in str(caught.value)
    assert "Zelda" not in str(caught.value)  # never the user's name


async def test_items_with_the_flag_a_product_scoped_to_test_client_is_created_updated_and_deleted(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["items"], with_items=True)
    assert report.ok
    post = next(r.json for r in fake.mock.requests if r.method == "POST")
    assert post["TypeId"] == 1 and post["ClientId"] == TEST_CLIENT and post["Name"] == f"{RUN} item"
    assert [r.method for r in fake.mock.requests if r.method != "GET"] == ["POST", "PATCH", "DELETE"]
    assert report.summary["cleaned"] == 1 and fake.leftovers() == []


async def test_items_are_skipped_by_default_with_the_reason_and_nothing_is_sent_for_them(run, lines):
    fake = FakeGorelo()
    report = await run(fake, areas=["items"])  # --only items without --with-items
    result = results(report)["items"]
    assert result.status == "skipped" and result.requests == 0
    # the spec does not say that items sync to the accounting system, so the reason does not claim it
    assert result.detail == "not live-tested by default: creates and deletes a catalog product on the test client"
    assert result.detail == write_matrix.ITEMS_SKIP_REASON and "sync" not in result.detail
    assert fake.mock.requests == [] and fake.items == {}  # not even a lookup
    assert report.ok and report.exit_code == 0 and report.summary["created"] == 0
    assert "area items: skipped, 0 requests (" + write_matrix.ITEMS_SKIP_REASON + ")" in lines
    assert "result: PASSED" in lines[-1]


async def test_a_default_full_run_skips_only_the_items_invoices_and_approved_invoice_areas_and_never_touches_them(run):
    fake = FakeGorelo()
    report = await run(fake)
    found = results(report)
    assert found["items"].status == "skipped" and found["items"].detail == write_matrix.ITEMS_SKIP_REASON
    assert found["invoices"].status == "skipped" and found["invoices"].detail == write_matrix.INVOICES_SKIP_REASON
    assert found["approved_invoice"].status == "skipped"
    assert found["approved_invoice"].detail == write_matrix.APPROVED_INVOICE_SKIP_REASON
    assert all(
        result.status == "pass" for area, result in found.items() if area not in ("items", "invoices", "approved_invoice")
    )
    assert report.ok and fake.items == {} and fake.invoices == {}
    assert not any("/items" in r.path or "/invoices" in r.path for r in fake.mock.requests)
    assert not any(r.method == "GET" and r.path == "/v1/contacts" for r in fake.mock.requests)  # not the precondition read
    assert area_ops(fake, report) == {**GOLDEN, "items": [], "invoices": [], "approved_invoice": []}
    assert report.summary["created"] == 19  # the 21 records of a run with both flags, less the product and the invoice


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("create_item", {"type": "product", "name": f"{RUN} sneaky", "client_id": TEST_CLIENT}),
        ("update_item", {"item_id": "x", "description": "y"}),
        ("delete_item", {"item_id": "x", "confirm": True}),
    ],
)
async def test_the_item_tools_cannot_even_be_called_without_the_flag(run, monkeypatch, tool, arguments):
    fake = FakeGorelo()

    async def sneaky(matrix):
        await matrix.tool(tool, **arguments)

    monkeypatch.setattr(write_matrix, "AREAS", (write_matrix.Area("items", sneaky),))
    report = await run(fake)
    assert results(report)["items"].status == "FAIL"
    assert f"{tool} is not one of the tools this script may call" in results(report)["items"].detail
    assert fake.mock.requests == [] and report.refused == 0


# --------------------------------------------------------------------------
# Invoices: a Draft for the test client, created, read, listed, exported and deleted (only with --with-invoices)
# --------------------------------------------------------------------------

# 2026-10-02 23:30 at UTC-5 is 2026-10-03 04:30 UTC: the invoice dates must be the UTC date, which is the next day here
CLOCK = datetime(2026, 10, 2, 23, 30, 0, tzinfo=timezone(timedelta(hours=-5)))
INVOICE_DATE, DUE_DATE = "2026-10-03", "2026-10-17"
LABEL = f"{RUN} invoice"
INVOICE_CREATE_BODY = {
    "ClientId": TEST_CLIENT,
    "StatusId": 1,
    "Reference": LABEL,
    "InvoiceDate": f"{INVOICE_DATE}T00:00:00Z",
    "DueDate": f"{DUE_DATE}T00:00:00Z",
    "LineItems": [{"ItemId": GLOBAL_PRODUCT, "Quantity": 1, "Description": LABEL, "UnitPrice": 1.0}],
}


def the_invoice(fake: FakeGorelo) -> dict:
    (invoice,) = fake.invoices.values()
    return invoice


def invoice_record_of(report) -> object:
    (record,) = [r for r in Manifest.load(report.manifest_path).all_created() if r.kind == "invoice"]
    return record


async def test_invoices_with_the_flag_a_draft_is_created_read_listed_exported_and_deleted(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["invoices"], with_invoices=True, clock=lambda: CLOCK)
    assert report.ok, results(report)["invoices"].detail
    assert area_ops(fake, report)["invoices"] == GOLDEN["invoices"] and results(report)["invoices"].requests == 10
    invoice = the_invoice(fake)
    items, post, readback, got, listing, pdf, again, lookup, delete, after = fake.mock.requests
    # the item: the first active product of nobody or of the test client (the one before it belongs to Second)
    assert items.query["TypeIds"] == "1" and items.query["StatusIds"] == "1" and items.query["PageSize"] == "50"
    # exactly the request the guard test pins: a Draft for the test client, one line, no recipient, the UTC date and 14 days later
    assert post.json == INVOICE_CREATE_BODY and "RecipientEmails" not in post.json
    assert readback.path == got.path == again.path == f"/v1/invoices/{invoice['Id']}"
    assert listing.query["ClientIds"] == "9501" and listing.query["Query"] == LABEL
    assert after.query["ClientIds"] == "9501" and after.query["Query"] == LABEL
    assert pdf.path == f"/v1/invoices/{invoice['Id']}/pdf" and fake.exports == [invoice["Id"]]  # one export event
    assert lookup.query["Number"] == "1042" and delete.path == f"/v1/invoices/{invoice['Id']}"
    assert ("invoice", invoice["Id"]) in fake.deleted and fake.leftovers() == [] and fake.emailed == []
    # recorded when the create answered: a Draft, with the number it used up
    record = invoice_record_of(report)
    assert (record.id, record.label) == (invoice["Id"], LABEL)
    assert record.details == {"status_id": 1, "number": 1042, "display_number": "INV-1042"}
    assert record.cleaned and record.outcome == "deleted by the write matrix (delete_invoice, Draft)"
    assert report.summary == {"created": 1, "cleaned": 1, "leftovers": 0, "undeletable": 0, "unresolved_intents": 0}
    notes = results(report)["invoices"].notes
    assert "invoice number consumed: Number 1042, DisplayNumber 'INV-1042' (a Draft uses up one invoice number)" in notes
    assert (
        f"export_invoice_pdf: {len(PDF_BYTES)} bytes of application/pdf; the export event is recorded on the test invoice, "
        "which is deleted next"
    ) in notes
    assert invoice["Status"] == {"Id": 1, "Name": "Draft"} and ("invoice", invoice["Id"]) in fake.deleted


async def test_invoices_are_skipped_by_default_with_the_reason_and_nothing_is_sent_for_them(run, lines):
    fake = FakeGorelo()
    report = await run(fake, areas=["invoices"])  # --only invoices without --with-invoices
    result = results(report)["invoices"]
    assert result.status == "skipped" and result.requests == 0
    assert result.detail == (
        "not live-tested by default: creates and deletes a Draft invoice on the test client, which uses up one invoice number"
    )
    assert result.detail == write_matrix.INVOICES_SKIP_REASON
    assert fake.mock.requests == [] and fake.invoices == {} and fake.exports == []  # not even a catalog lookup
    assert report.ok and report.exit_code == 0 and report.summary["created"] == 0
    assert "area invoices: skipped, 0 requests (" + write_matrix.INVOICES_SKIP_REASON + ")" in lines
    assert lines[-1] == "result: PASSED"


async def test_the_items_flag_does_not_open_the_invoices_area(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["items", "invoices"], with_items=True)
    found = results(report)
    assert found["items"].status == "pass" and found["invoices"].status == "skipped"
    assert not any("/invoices" in r.path for r in fake.mock.requests) and fake.invoices == {}
    fake = FakeGorelo()
    report = await run(fake, areas=["items", "invoices"], with_invoices=True, started=datetime(2099, 10, 2, 10, 16, 0, tzinfo=timezone.utc))
    found = results(report)
    assert found["items"].status == "skipped" and found["invoices"].status == "pass"
    assert not any(r.method in ("POST", "PATCH") and "/items" in r.path for r in fake.mock.requests)


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("list_items", {"type": "product"}),
        ("list_invoices", {"client_ids": [TEST_CLIENT]}),
        ("get_invoice", {"invoice_id": invoice_guid(1)}),
        ("create_invoice", {"client_id": TEST_CLIENT, "line_items": [{"item_id": GLOBAL_PRODUCT, "quantity": 1}]}),
        ("export_invoice_pdf", {"invoice_id": invoice_guid(1)}),
        ("delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True}),
    ],
)
async def test_the_invoice_tools_cannot_even_be_called_without_the_flag(run, monkeypatch, tool, arguments):
    fake = FakeGorelo()

    async def sneaky(matrix):
        await matrix.tool(tool, **arguments)

    monkeypatch.setattr(write_matrix, "AREAS", (write_matrix.Area("invoices", sneaky),))
    report = await run(fake, with_items=True)  # the items flag does not open them
    assert results(report)["invoices"].status == "FAIL"
    assert f"{tool} is not one of the tools this script may call" in results(report)["invoices"].detail
    assert fake.mock.requests == [] and report.refused == 0


async def test_the_pdf_export_cannot_be_reached_through_the_file_call_without_the_flag(run, monkeypatch):
    fake = FakeGorelo()

    async def sneaky(matrix):
        await matrix.tool_result("export_invoice_pdf", invoice_id=invoice_guid(1))

    monkeypatch.setattr(write_matrix, "AREAS", (write_matrix.Area("invoices", sneaky),))
    report = await run(fake)
    assert "export_invoice_pdf is not one of the tools this script may call" in results(report)["invoices"].detail
    assert fake.mock.requests == [] and fake.exports == []


async def test_create_approved_invoice_is_not_callable_by_the_matrix_even_with_the_flag(run, monkeypatch):
    fake = FakeGorelo()

    async def sneaky(matrix):
        await matrix.tool(
            "create_approved_invoice", client_id=TEST_CLIENT, line_items=[{"item_id": GLOBAL_PRODUCT, "quantity": 1}], confirm=True
        )

    monkeypatch.setattr(write_matrix, "AREAS", (write_matrix.Area("invoices", sneaky),))
    report = await run(fake, with_items=True, with_invoices=True)
    assert results(report)["invoices"].status == "FAIL"
    assert "create_approved_invoice is not one of the tools this script may call" in results(report)["invoices"].detail
    assert fake.mock.requests == [] and report.refused == 0 and fake.invoices == {}


async def test_the_guard_still_stops_an_approved_invoice_if_the_tool_allowlist_were_bypassed(run, monkeypatch):
    fake = FakeGorelo()

    async def bypass(matrix):
        seq = matrix.manifest.intent("invoice", matrix.manifest.label("sneaky invoice"))
        arguments = {"client_id": TEST_CLIENT, "line_items": [{"item_id": GLOBAL_PRODUCT, "quantity": 1}], "confirm": True}
        await matrix.session.tools.call_tool("create_approved_invoice", arguments, raise_on_error=False)  # past the allowlist
        matrix.manifest.intent_failed(seq, "the guard refused it")

    monkeypatch.setattr(write_matrix, "AREAS", (write_matrix.Area("invoices", bypass), write_matrix.Area("uptime", write_matrix.area_uptime)))
    report = await run(fake, with_invoices=True)
    assert [(r.area, r.status) for r in report.results] == [("invoices", "FAIL"), ("uptime", "not run")]
    assert "StatusId 5 (Approved) is not allowed" in results(report)["invoices"].detail
    assert fake.mock.requests == [] and fake.invoices == {} and report.refused == 1


@pytest.mark.parametrize(
    "catalog, chosen",
    [
        ([catalog_item(901, "a", SECOND), catalog_item(903, "c", TEST_CLIENT)], catalog_guid(903)),  # only the test client's product
        ([catalog_item(902, "b", None), catalog_item(903, "c", TEST_CLIENT)], catalog_guid(902)),  # the first of two
        ([catalog_item(901, "a", 5555), catalog_item(902, "b", None)], catalog_guid(902)),
        ([{**catalog_item(901, "a", None), "Id": "not-a-guid"}, catalog_item(902, "b", TEST_CLIENT)], catalog_guid(902)),
        ([{k: v for k, v in catalog_item(901, "a", None).items() if k != "ClientId"}, catalog_item(902, "b", TEST_CLIENT)], catalog_guid(902)),
        ([catalog_item(901, "a", True), catalog_item(902, "b", None)], catalog_guid(902)),  # JSON true is no client
        ([catalog_item(901, "a", 9501.0), catalog_item(902, "b", None)], catalog_guid(902)),  # nor is a decimal number
        ([catalog_item(901, "a", 9502), catalog_item(902, "b", TEST_CLIENT), catalog_item(903, "c", None)], catalog_guid(902)),
    ],
)
async def test_invoices_the_item_is_the_first_active_product_of_nobody_or_of_test_client(run, catalog, chosen):
    fake = FakeGorelo(catalog=catalog)
    report = await run(fake, areas=["invoices"], with_invoices=True)
    assert report.ok, results(report)["invoices"].detail
    post = next(r.json for r in fake.mock.requests if r.method == "POST")
    assert post["LineItems"][0]["ItemId"] == chosen


@pytest.mark.parametrize(
    "catalog",
    [
        [],
        [catalog_item(901, "a", SECOND)],
        [catalog_item(901, "a", 5555), catalog_item(902, "b", 1)],
        [{"Id": catalog_guid(901), "Name": "no client key"}],
        [{**catalog_item(901, "a", None), "Id": "not-a-guid"}, {**catalog_item(902, "b", None), "Id": None}, "not a row"],
    ],
)
async def test_invoices_without_a_usable_item_the_area_is_skipped_after_its_first_request(run, catalog):
    fake = FakeGorelo(catalog=catalog)
    report = await run(fake, areas=["invoices"], with_invoices=True)
    result = results(report)["invoices"]
    assert result.status == "skipped" and result.requests == 1
    assert result.detail == (
        "no active product of nobody or of client 9501 among the first 50 catalog items to put on the invoice"
    )
    assert [(r.method, r.path) for r in fake.mock.requests] == [("GET", "/v1/items")]
    assert fake.invoices == {} and report.ok and report.summary["created"] == 0


@pytest.mark.parametrize(
    "damage, said",
    [
        pytest.param(lambda inv: inv.update(Reference="something else"), "create_invoice Reference: expected", id="reference"),
        pytest.param(lambda inv: inv.update(Reference=None), "create_invoice Reference: expected", id="no-reference"),
        pytest.param(lambda inv: inv.update(ClientId=SECOND), "create_invoice ClientId: expected 9501, got 9502", id="client"),
        pytest.param(lambda inv: inv.update(InvoiceDate="2020-01-01"), "create_invoice InvoiceDate: expected '2026-10-03', got '2020-01-01'", id="invoice-date"),
        pytest.param(lambda inv: inv.update(InvoiceDate=None), "create_invoice InvoiceDate: expected '2026-10-03', got None", id="no-invoice-date"),
        pytest.param(lambda inv: inv.update(DueDate="2026-10-18"), "create_invoice DueDate: expected '2026-10-17', got '2026-10-18'", id="due-date"),
        pytest.param(lambda inv: inv["LineItems"].append(dict(inv["LineItems"][0])), "has 2 line items, expected exactly 1", id="two-lines"),
        pytest.param(lambda inv: inv.update(LineItems=[]), "has 0 line items, expected exactly 1", id="no-lines"),
        pytest.param(lambda inv: inv.update(LineItems=None), "no list of line items (NoneType), expected exactly 1", id="lines-not-a-list"),
        pytest.param(lambda inv: inv.update(LineItems=["x"]), "the invoice line is not an object", id="line-not-an-object"),
        pytest.param(lambda inv: inv["LineItems"][0].update(ItemId=TEST_PRODUCT), "not for the catalog item that was asked for", id="another-item"),
        pytest.param(lambda inv: inv["LineItems"][0].update(ItemId=None), "not for the catalog item that was asked for", id="no-item"),
        pytest.param(lambda inv: inv["LineItems"][0].update(Quantity=2), "line Quantity: expected 1, got 2", id="quantity"),
        pytest.param(lambda inv: inv["LineItems"][0].update(UnitPrice=50.0), "line UnitPrice: expected 1.0, got 50.0", id="price"),
        pytest.param(lambda inv: inv["LineItems"][0].update(Description="x"), "line Description: expected", id="description"),
    ],
)
async def test_invoices_a_failing_check_after_the_create_still_cleans_the_invoice_up(run, damage, said):
    fake = FakeGorelo()
    fake.on_invoice_created(damage)
    report = await run(fake, areas=["invoices"], with_invoices=True, clock=lambda: CLOCK)
    found = results(report)["invoices"]
    assert found.status == "FAIL" and said in found.detail and found.requests == 3  # items, POST, the read-back
    assert not report.ok and report.exit_code == 1  # the area failed ...
    assert report.cleanup.ok and fake.leftovers() == [] and report.summary["unresolved_intents"] == 0  # ... but nothing is left
    # the cleanup found the invoice in the manifest (recorded before any check) and deleted it: read first, then delete_invoice
    assert area_ops(fake, report)["cleanup"] == ["GET /v1/invoices/{invoiceId}", "GET /v1/invoices", "DELETE /v1/invoices/{invoiceId}"]
    record = invoice_record_of(report)
    assert record.details == {"status_id": 1, "number": 1042, "display_number": "INV-1042"}
    assert record.cleaned and record.outcome == "Deleted (Draft, delete_invoice)"


async def test_invoices_a_check_that_fails_later_still_deletes_the_invoice_in_the_cleanup(run):
    fake = FakeGorelo()
    original = fake.handlers["GET /v1/invoices"]

    def forgets_the_invoice(request):
        if request.query.get("Query"):
            return paged_envelope([])  # list_invoices by the run label does not list it
        return original(request)

    fake.handlers["GET /v1/invoices"] = forgets_the_invoice
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "FAIL" and "list_invoices did not list the new invoice" in found.detail
    assert fake.leftovers() == [] and report.cleanup.ok and not any(r.path.endswith("/pdf") for r in fake.mock.requests)


async def test_invoices_a_stored_status_that_is_not_a_draft_is_left_for_the_user_and_never_deleted_or_voided(run, lines):
    fake = FakeGorelo()
    fake.on_invoice_created(lambda inv: inv.update(Status={"Id": 5, "Name": "Approved"}))
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "FAIL" and "create_invoice Status.Id (Draft): expected 1, got 5" in found.detail
    invoice = the_invoice(fake)
    assert not any(r.method == "DELETE" for r in fake.mock.requests)  # neither the area nor the cleanup deleted or voided it
    assert invoice["Status"] == {"Id": 5, "Name": "Approved"} and ("invoice", invoice["Id"]) not in fake.deleted
    assert area_ops(fake, report)["cleanup"] == ["GET /v1/invoices/{invoiceId}"]  # read, found Approved, left alone
    assert not report.ok and report.summary["leftovers"] == 1 and fake.leftovers() == [f"invoice {invoice['Id']}"]
    assert invoice_record_of(report).details["status_id"] == 5  # what Gorelo STORED: the guard would refuse a delete too
    assert any(
        line.startswith("  LEFTOVER   invoice ") and "invoice INV-1042 has status Approved (id 5)" in line for line in lines
    )
    assert "result: SOMETHING IS LEFT OVER" in lines and lines[-1] == "result: FAILED"


async def test_invoices_an_invoice_approved_before_the_delete_is_not_deleted_or_voided(run, lines):
    fake = FakeGorelo()
    original = fake.handlers["GET /v1/invoices/{invoiceId}"]
    reads = []

    def approved_by_somebody_else(request):
        reads.append(request.path)
        if len(reads) == 3:  # the create's read-back, get_invoice, and now the check right before the delete
            fake.invoices[request.path.rsplit("/", 1)[1]]["Status"] = {"Id": 5, "Name": "Approved"}
        return original(request)

    fake.handlers["GET /v1/invoices/{invoiceId}"] = approved_by_somebody_else
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "FAIL"
    assert "has Status.Id 5 before the delete, not 1 (Draft): nothing was deleted or voided, it is left for the user" in found.detail
    ops = area_ops(fake, report)
    assert ops["invoices"] == GOLDEN["invoices"][:7]  # up to the second get_invoice: no lookup by Number, no DELETE
    assert ops["cleanup"] == ["GET /v1/invoices/{invoiceId}"]  # read again by the cleanup: Approved, left alone
    assert not any(r.method == "DELETE" for r in fake.mock.requests)
    assert the_invoice(fake)["Status"]["Id"] == 5 and not report.ok and report.summary["leftovers"] == 1
    assert any("invoice INV-1042 has status Approved (id 5)" in line for line in lines)


async def test_invoices_one_that_reads_back_with_no_number_is_removed_by_the_cleanup_with_its_raw_delete(run):
    fake = FakeGorelo()
    fake.on_invoice_created(lambda inv: inv.update(Number=None))
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "pass" and report.ok, found.detail
    ops = area_ops(fake, report)
    assert ops["invoices"] == GOLDEN["invoices"][:7]  # no delete_invoice, no lookup by Number, no last list
    assert ops["cleanup"] == ["GET /v1/invoices/{invoiceId}", "DELETE /v1/invoices/{invoiceId}"]
    assert any("has no Number" in note and "the cleanup removes it with its raw DELETE" in note for note in found.notes)
    record = invoice_record_of(report)
    assert record.details == {"status_id": 1, "number": None, "display_number": "INV-1042"}
    assert record.cleaned and record.outcome == "Deleted (Draft, raw DELETE)"
    assert fake.leftovers() == []


async def test_invoices_a_delete_that_does_not_answer_deleted_fails_the_area(run):
    fake = FakeGorelo()
    original = fake.handlers["DELETE /v1/invoices/{invoiceId}"]

    def answers_void(request):
        original(request)
        return envelope({"Id": fake.part(request, 3), "StatusId": 4})  # Gorelo says Void, not Deleted

    fake.handlers["DELETE /v1/invoices/{invoiceId}"] = answers_void
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "FAIL" and "delete_invoice StatusId (Deleted): expected 6, got 4" in found.detail
    assert not invoice_record_of(report).outcome.startswith("deleted by the write matrix")  # it was not claimed as deleted


async def test_invoices_a_delete_that_answers_another_invoice_fails_the_area(run):
    fake = FakeGorelo()
    original = fake.handlers["DELETE /v1/invoices/{invoiceId}"]

    def answers_another(request):
        original(request)
        return envelope({"Id": invoice_guid(777), "StatusId": 6})

    fake.handlers["DELETE /v1/invoices/{invoiceId}"] = answers_another
    report = await run(fake, areas=["invoices"], with_invoices=True)
    assert "delete_invoice answered with another invoice's Id" in results(report)["invoices"].detail


async def test_invoices_a_deleted_draft_that_is_still_listed_is_not_claimed_as_cleaned_by_the_area(run):
    fake = FakeGorelo()
    original = fake.handlers["GET /v1/invoices"]

    def stale_listing(request):
        if request.query.get("Query"):  # the list by the run label still shows the deleted Draft
            return fake.paged([{k: v for k, v in fake.served(i).items() if k != "LineItems"} for i in fake.invoices.values()], request)
        return original(request)

    fake.handlers["GET /v1/invoices"] = stale_listing
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "FAIL" and "list_invoices still lists the deleted Draft invoice" in found.detail
    record = invoice_record_of(report)
    # the area did not mark it cleaned: the cleanup read it again (a deleted invoice is a 404) and settled it
    assert record.cleaned and record.outcome == "already gone (HTTP 404)"
    assert fake.leftovers() == [] and not report.ok


def lagging_label_searches(fake: FakeGorelo, times: int) -> None:
    """The first `times` free-text searches of GET /v1/invoices (the run label) come back empty: the search lags."""
    original = fake.handlers["GET /v1/invoices"]
    left = [times]

    def serve(request):
        if request.query.get("Query") and left[0] > 0:
            left[0] -= 1
            return paged_envelope([])
        return original(request)

    fake.handlers["GET /v1/invoices"] = serve


def stale_label_searches_after_the_delete(fake: FakeGorelo, times: int) -> None:
    """After the Draft was deleted the first `times` free-text searches still list it."""
    original = fake.handlers["GET /v1/invoices"]
    left = [times]

    def serve(request):
        if request.query.get("Query") and fake.deleted and left[0] > 0:
            left[0] -= 1
            rows = [{k: v for k, v in fake.served(i).items() if k != "LineItems"} for i in fake.invoices.values()]
            return fake.paged(rows, request)
        return original(request)

    fake.handlers["GET /v1/invoices"] = serve


def lagging_number_lookups(fake: FakeGorelo, times: int) -> None:
    """The first `times` lookups of an invoice by its Number (what delete_invoice does) find nothing."""
    original = fake.handlers["GET /v1/invoices"]
    left = [times]

    def serve(request):
        if request.query.get("Number") and left[0] > 0:
            left[0] -= 1
            return paged_envelope([])
        return original(request)

    fake.handlers["GET /v1/invoices"] = serve


async def test_invoices_a_list_that_lags_behind_the_create_is_asked_again(run):
    fake = FakeGorelo()
    lagging_label_searches(fake, 1)
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "pass" and report.ok, found.detail
    ops = area_ops(fake, report)["invoices"]
    assert ops == [*GOLDEN["invoices"][:5], "GET /v1/invoices", *GOLDEN["invoices"][5:]]  # one more list, right after the first
    assert "list_invoices listed the new invoice on attempt 2" in found.notes


async def test_invoices_a_list_that_never_shows_the_invoice_fails_after_three_attempts_and_the_cleanup_deletes_it(run):
    fake = FakeGorelo()
    lagging_label_searches(fake, 99)
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "FAIL" and "list_invoices did not list the new invoice" in found.detail
    searches = [r for r in fake.mock.requests if r.path == "/v1/invoices" and "Query" in r.query]
    assert len(searches) == 3 and not any(r.path.endswith("/pdf") for r in fake.mock.requests)  # nothing after the failed check
    assert fake.leftovers() == [] and report.cleanup.ok


async def test_invoices_a_list_that_still_shows_the_deleted_invoice_for_a_moment_is_asked_again(run):
    fake = FakeGorelo()
    stale_label_searches_after_the_delete(fake, 1)
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "pass" and report.ok, found.detail
    assert area_ops(fake, report)["invoices"] == [*GOLDEN["invoices"], "GET /v1/invoices"]
    assert "list_invoices no longer listed the invoice on attempt 2" in found.notes
    assert invoice_record_of(report).outcome == "deleted by the write matrix (delete_invoice, Draft)"


async def test_invoices_a_lookup_by_number_that_lags_is_asked_again_before_anything_is_deleted(run):
    fake = FakeGorelo()
    lagging_number_lookups(fake, 1)
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "pass" and report.ok, found.detail
    ops = area_ops(fake, report)["invoices"]
    assert ops == [*GOLDEN["invoices"][:7], "GET /v1/invoices", *GOLDEN["invoices"][7:]]  # a second lookup, then the DELETE
    assert ops.count("DELETE /v1/invoices/{invoiceId}") == 1
    assert "delete_invoice found the invoice by its Number on attempt 2" in found.notes


async def test_invoices_a_lookup_by_number_that_never_finds_the_invoice_fails_the_area_without_deleting_and_the_cleanup_removes_it_by_id(run):
    fake = FakeGorelo()
    lagging_number_lookups(fake, 99)
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "FAIL" and "no invoice has the number 1042" in found.detail
    area_requests = fake.mock.requests[: found.requests]  # what the area itself sent, before the cleanup
    assert len([r for r in area_requests if r.path == "/v1/invoices" and "Number" in r.query]) == 3
    assert not any(r.method == "DELETE" for r in area_requests)  # the area deleted nothing: its lookup found nothing
    # The cleanup read the invoice by its id (a Draft) and asked delete_invoice once more: its lookup lags too, so the tool
    # refused before any DELETE. The cleanup then sent the one DELETE there is, raw, for that same id (still behind the guard).
    assert area_ops(fake, report)["cleanup"] == ["GET /v1/invoices/{invoiceId}", "GET /v1/invoices", "DELETE /v1/invoices/{invoiceId}"]
    invoice = the_invoice(fake)
    assert [r.path for r in fake.mock.requests if r.method == "DELETE"] == [f"/v1/invoices/{invoice['Id']}"]
    assert ("invoice", invoice["Id"]) in fake.deleted and fake.leftovers() == [] and fake.emailed == []
    assert invoice_record_of(report).outcome == "Deleted (Draft, raw DELETE after the lookup by Number found nothing)"
    assert report.cleanup.ok and report.summary["leftovers"] == 0 and report.refused == 0
    assert not report.ok and report.exit_code == 1  # nothing is left over, but the area itself failed: the run says so


async def test_invoices_a_lookup_that_lags_for_exactly_the_areas_three_tries_is_found_by_the_cleanup_which_uses_delete_invoice(run):
    fake = FakeGorelo()
    lagging_number_lookups(fake, 3)  # the area's three attempts, not the cleanup's
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "FAIL" and "no invoice has the number 1042" in found.detail
    assert area_ops(fake, report)["cleanup"] == ["GET /v1/invoices/{invoiceId}", "GET /v1/invoices", "DELETE /v1/invoices/{invoiceId}"]
    assert invoice_record_of(report).outcome == "Deleted (Draft, delete_invoice)"  # the tool did it: no fallback needed
    assert report.cleanup.ok and fake.leftovers() == []


def test_the_matrix_and_the_cleanup_read_the_same_refusal_of_delete_invoice():
    assert write_matrix.NO_SUCH_NUMBER is cleanup.NO_SUCH_NUMBER and cleanup.NO_SUCH_NUMBER == "no invoice has the number"


def test_the_harness_does_not_promise_that_an_invoice_delete_is_permanent():
    """The 2026-10-02 spec says a Draft is Deleted (StatusId 6) and no longer listed, nothing about permanence."""
    for module in (write_matrix, cleanup):
        assert "permanent" not in Path(module.__file__).read_text(encoding="utf-8").lower(), module.__name__
    assert "Draft: deleted (no longer listed); the answer says StatusId 6" in write_matrix.__doc__
    source = Path(write_matrix.__file__).read_text(encoding="utf-8")
    assert "Draft: deleted (no longer listed).\n    # Approved: voided (status Void, still listed)" in source  # the documented wording


async def test_invoices_a_delete_gorelo_refuses_is_not_asked_again_by_the_area(run):
    fake = FakeGorelo()
    fake.fail("DELETE /v1/invoices/{invoiceId}", error_envelope(409, [("070901", "The invoice is locked.")]))
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "FAIL" and "delete_invoice" in found.detail and "The invoice is locked." in found.detail
    assert area_ops(fake, report)["invoices"].count("DELETE /v1/invoices/{invoiceId}") == 1  # no retry for a refusal
    assert fake.leftovers() == [] and report.cleanup.ok  # the cleanup deleted it afterwards


async def test_invoices_a_create_whose_answer_was_lost_is_found_by_the_run_label_and_deleted(run, lines):
    fake = FakeGorelo()
    original = fake.handlers["POST /v1/invoices"]

    def lost_answer(request):
        original(request)  # the invoice is made ...
        return error_envelope(500, [("070500", "boom")])  # ... but the answer never arrives

    fake.handlers["POST /v1/invoices"] = lost_answer
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "FAIL" and "create_invoice" in found.detail and found.requests == 2
    ops = area_ops(fake, report)
    assert ops["cleanup"] == [
        "GET /v1/invoices",  # the search by the run label (client 9501)
        "GET /v1/invoices/{invoiceId}",  # the invoice found is read first, like any other
        "GET /v1/invoices",  # delete_invoice's lookup by Number
        "DELETE /v1/invoices/{invoiceId}",
    ]
    search = fake.mock.requests[2]
    assert search.query["ClientIds"] == "9501" and search.query["Query"] == LABEL
    assert fake.leftovers() == [] and report.cleanup.ok and report.summary["unresolved_intents"] == 0
    record = invoice_record_of(report)
    assert record.label == LABEL and record.cleaned and record.details["status_id"] == 1
    assert not report.ok  # the area itself failed


async def test_invoices_a_create_that_made_nothing_but_was_not_confirmed_stays_reported(run, lines):
    fake = FakeGorelo()
    fake.fail("POST /v1/invoices", error_envelope(500, [("070500", "boom")]))
    report = await run(fake, areas=["invoices"], with_invoices=True)
    assert results(report)["invoices"].status == "FAIL"
    assert area_ops(fake, report)["cleanup"] == ["GET /v1/invoices"]  # searched by label: nothing found
    assert report.summary["unresolved_intents"] == 1 and not report.cleanup.ok
    assert any("announced without an id" in line for line in lines) and any(line.startswith("  UNRESOLVED invoice ") for line in lines)


async def test_invoices_a_create_that_gorelo_refused_settles_its_intent_and_nothing_is_searched(run):
    fake = FakeGorelo()
    fake.fail("POST /v1/invoices", error_envelope(400, [("070101", "Reference is too long", "Reference")]))
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "FAIL" and "create_invoice" in found.detail and "reference: Reference is too long" in found.detail
    assert report.summary["unresolved_intents"] == 0 and report.cleanup.ok and area_ops(fake, report)["cleanup"] == []


async def test_invoices_a_read_back_that_failed_still_leaves_the_invoice_recorded_as_a_draft_and_cleaned(run):
    fake = FakeGorelo()
    fake.fail("GET /v1/invoices/{invoiceId}", error_envelope(500, [("070500", "boom")]))
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "FAIL" and "the write succeeded but reading it back failed" in found.detail
    assert area_ops(fake, report)["invoices"] == ["GET /v1/items", "POST /v1/invoices", "GET /v1/invoices/{invoiceId}"]
    # the answer was only {Id, warning}: no number to record, and the status is the Draft the create asked for
    assert invoice_record_of(report).details == {"status_id": 1, "number": None, "display_number": None}
    assert fake.leftovers() == [] and report.summary["unresolved_intents"] == 0


async def test_invoices_an_answer_without_an_id_fails_the_area_and_leaves_the_create_announced(run):
    fake = FakeGorelo()
    fake.fail("POST /v1/invoices", envelope({}))
    report = await run(fake, areas=["invoices"], with_invoices=True)
    assert results(report)["invoices"].status == "FAIL"
    assert report.summary["unresolved_intents"] == 1 and area_ops(fake, report)["cleanup"] == ["GET /v1/invoices"]


async def test_invoices_a_pdf_that_is_not_a_pdf_fails_the_area_and_the_draft_is_still_deleted(run):
    fake = FakeGorelo()
    fake.handlers["GET /v1/invoices/{invoiceId}/pdf"] = lambda q: httpx.Response(
        200, text="<html>nope</html>", headers={"content-type": "text/html"}
    )
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "FAIL" and "export_invoice_pdf" in found.detail
    assert fake.leftovers() == [] and report.cleanup.ok


async def test_invoices_cleanup_runs_even_when_the_run_is_interrupted_after_the_invoice_was_created(run, monkeypatch, lines):
    fake = FakeGorelo()

    class Abort(BaseException):
        pass

    def interrupted(result):
        raise Abort("stop")

    monkeypatch.setattr(write_matrix, "pdf_size", interrupted)
    with pytest.raises(Abort):
        await run(fake, areas=["invoices"], with_invoices=True)
    assert fake.leftovers() == [] and len(fake.exports) == 1  # the Draft made before the interruption is gone
    assert any(line.startswith("cleanup of MCPTEST-") for line in lines) and lines[-1] == "result: FAILED"


async def test_invoices_only_the_runs_own_invoice_is_ever_written_exported_or_deleted(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["invoices"], with_invoices=True)
    assert report.ok
    (ident,) = fake.invoices
    for request in fake.mock.requests:
        if request.method == "POST":
            assert request.path == "/v1/invoices" and request.json["ClientId"] == TEST_CLIENT and request.json["StatusId"] == 1
            assert not request.json.get("RecipientEmails")
        if request.method == "DELETE":
            assert request.path == f"/v1/invoices/{ident}"
        if request.path.endswith("/pdf"):
            assert request.path == f"/v1/invoices/{ident}/pdf"
        assert request.path not in ("/v1/alerts", "/v1/api-keys") and request.method in ("GET", "POST", "DELETE")
    assert len(fake.exports) == 1 and fake.emailed == []
    assert report.refused == 0


async def test_the_invoice_tools_must_be_offered_when_the_flag_is_given(tmp_path, make_settings):
    fake = FakeGorelo()
    no_billing = make_settings(destructive=True, toolsets=frozenset({"core", "tickets", "time", "uptime", "projects", "forms"}))
    with pytest.raises(write_matrix.SetupError, match="does not offer the tools the matrix calls: .*create_invoice"):
        await write_matrix.run_matrix(
            settings=no_billing, transport=fake.mock.transport, directory=tmp_path / "runs", pace=0,
            echo=lambda line: None, areas=["clients"], with_invoices=True,
        )
    assert fake.mock.requests == [] and not (tmp_path / "runs").exists()
    # without the flag the matrix never needs a billing tool
    report = await write_matrix.run_matrix(
        settings=no_billing, transport=fake.mock.transport, directory=tmp_path / "runs", pace=0,
        echo=lambda line: None, areas=["clients"], started=RUN_START,
    )
    assert report.ok


def test_invoice_record_takes_the_stored_status_number_and_display_number_from_the_answer():
    record = write_matrix.invoice_record
    assert record({"Id": "x", "Status": {"Id": 1, "Name": "Draft"}, "Number": 1042, "DisplayNumber": "INV-1042"}) == {
        "status_id": 1, "number": 1042, "display_number": "INV-1042",
    }
    # an answer without them ({Id, warning}): a Draft was asked for and the guard allows nothing else; no number to note
    assert record({"Id": "x", "warning": "read-back failed"}) == {"status_id": 1, "number": None, "display_number": None}
    # what Gorelo STORED wins: an invoice that is not a Draft is recorded as it is, so the guard refuses to delete it
    assert record({"Id": "x", "Status": {"Id": 5, "Name": "Approved"}})["status_id"] == 5
    for status in (None, "1", True, 0, -1, [], {}):
        assert record({"Status": status})["status_id"] == 1
    for number in (None, "1042", True, 0, -3, 1042.0):
        assert record({"Number": number})["number"] is None
    for display in (None, "", 1042, ["INV-1"]):
        assert record({"DisplayNumber": display})["display_number"] is None


def test_billable_item_picks_the_first_row_of_nobody_or_of_test_client():
    pick = write_matrix.billable_item
    assert pick([catalog_item(1, "a", None)]) == catalog_guid(1)
    assert pick([catalog_item(1, "a", SECOND), catalog_item(2, "b", TEST_CLIENT)]) == catalog_guid(2)
    assert pick([]) is None and pick(None) is None and pick("x") is None and pick({"Id": catalog_guid(1)}) is None
    assert pick([catalog_item(1, "a", SECOND), catalog_item(2, "b", 1), catalog_item(3, "c", "9501")]) is None


def test_date_text_is_the_calendar_date_of_a_date_or_a_date_time():
    assert write_matrix.date_text("2026-10-03") == "2026-10-03"
    assert write_matrix.date_text("2026-10-03T00:00:00Z") == "2026-10-03"
    assert write_matrix.date_text("2026-10-03T23:30:00-05:00") == "2026-10-03"
    assert write_matrix.date_text(None) is None and write_matrix.date_text(20261003) == 20261003


def test_pdf_size_reads_the_embedded_file_and_refuses_anything_else():
    def result(*blocks):
        return SimpleNamespace(content=list(blocks))

    def pdf(mime="application/pdf", blob=base64.b64encode(PDF_BYTES).decode()):
        return SimpleNamespace(type="resource", resource=SimpleNamespace(mimeType=mime, blob=blob))

    summary = SimpleNamespace(type="text", text="Exported invoice PDF INV-1042.pdf")
    assert write_matrix.pdf_size(result(summary, pdf())) == len(PDF_BYTES)
    for answer, said in (
        (result(summary), "answered without an embedded file"),
        (result(), "answered without an embedded file"),
        (SimpleNamespace(content=None), "answered without an embedded file"),
        (result(summary, pdf(mime="text/plain")), "attached a file of type 'text/plain', expected application/pdf"),
        (result(summary, pdf(mime=None)), "attached a file of type None, expected application/pdf"),
        (result(summary, pdf(blob="%%%")), "attached a file whose content is not valid base64"),
        (result(summary, pdf(blob="")), "attached an empty file"),
        (result(summary, pdf(blob=None)), "attached an empty file"),
    ):
        with pytest.raises(write_matrix.CheckFailed, match=said):
            write_matrix.pdf_size(answer)


def test_check_invoice_line_wants_exactly_the_one_line_the_matrix_asked_for():
    line = {"ItemId": GLOBAL_PRODUCT.upper(), "Quantity": 1.0, "UnitPrice": 1, "Description": LABEL}  # GUID case and 1 == 1.0 do not matter
    assert write_matrix.check_invoice_line({"LineItems": [line]}, GLOBAL_PRODUCT, LABEL, "get_invoice") is line
    for invoice, said in (
        ({}, "no list of line items \\(NoneType\\)"),
        ({"LineItems": [line, line]}, "2 line items"),
        ({"LineItems": [{**line, "Quantity": 2}]}, "get_invoice line Quantity: expected 1, got 2"),
    ):
        with pytest.raises(write_matrix.CheckFailed, match=said):
            write_matrix.check_invoice_line(invoice, GLOBAL_PRODUCT, LABEL, "get_invoice")


# --------------------------------------------------------------------------
# The approved invoice: ONE $1 Approved invoice for the test client, waited for until Gorelo has pushed it to the accounting
# system, then voided at once (only with --with-approved-invoice, which runs the area alone)
# --------------------------------------------------------------------------

APPROVED_LABEL = f"{RUN} approved invoice"
APPROVED_CREATE_BODY = {
    "ClientId": TEST_CLIENT,
    "StatusId": 5,
    "Reference": APPROVED_LABEL,
    "LineItems": [{"ItemId": GLOBAL_PRODUCT, "Quantity": 1, "Description": APPROVED_LABEL, "UnitPrice": 1.0, "TaxId": None}],
}
VOIDED_OUTCOME = "voided by the write matrix (still listed as Void)"
READ = "GET /v1/invoices/{invoiceId}"
NOT_YET_SYNCED = "approved invoice INV-1042 not yet synced to accounting; check the accounting system, then void it with: "
# Gorelo's void does not reach the accounting system, so every hint that sends the user to `cleanup --void-approved`
# says that this command voids in Gorelo only. Spelled out here, not read from the module: a change of the words fails a test.
GORELO_ONLY = "this voids it in Gorelo only: void its copy in the accounting system by hand too"


def void_command(report) -> str:
    """The command that the area and the summary print for an Approved invoice left for the user, with what it does."""
    return f"python -m scripts.live.cleanup {report.manifest_path} --void-approved ({GORELO_ONLY})"


@pytest.fixture
def approved(run):
    """run_matrix for the approved invoice area alone (--with-approved-invoice --only approved_invoice), without waiting."""

    async def go(fake: FakeGorelo, **options):
        options.setdefault("sync_interval", 0)
        options.setdefault("sync_polls", 3)
        return await run(fake, areas=["approved_invoice"], with_approved_invoice=True, **options)

    return go


def cleanup_with_the_flag(fake, report, make_settings, lines):
    """What the user runs afterwards: python -m scripts.live.cleanup <manifest> --void-approved, against the same fake."""
    return cleanup.run_cleanup(
        Manifest.load(report.manifest_path),
        void_approved=True,
        settings=make_settings(destructive=True),
        transport=fake.mock.transport,
        pace=0,
        echo=lines.append,
    )


async def test_approved_invoice_the_happy_path_creates_waits_voids_and_leaves_one_known_residue(approved, lines):
    fake = FakeGorelo()
    report = await approved(fake, clock=lambda: CLOCK)
    found = results(report)["approved_invoice"]
    assert report.ok and report.exit_code == 0 and found.status == "pass", "\n".join(lines)
    assert [r.area for r in report.results] == ["approved_invoice"]
    # exactly the documented requests, and the cleanup that follows sends nothing: a voided invoice is a known residue
    assert area_ops(fake, report) == {"approved_invoice": GOLDEN_APPROVED, "cleanup": []} and found.requests == 10
    contacts, locations, items, post, readback, poll, lookup, delete, after, listing = fake.mock.requests
    invoice = the_invoice(fake)
    assert contacts.query["ClientIds"] == "9501" and contacts.query["PageSize"] == "200"
    assert locations.path == "/v1/clients/9501/locations"
    assert items.query["TypeIds"] == "1" and items.query["StatusIds"] == "1" and items.query["PageSize"] == "50"
    # one line: that item, quantity 1, unit price 1.0, no tax, the label as its text and as the reference; StatusId 5; no
    # recipient and no dates (Gorelo's defaults)
    assert post.json == APPROVED_CREATE_BODY
    assert not {"RecipientEmails", "InvoiceDate", "DueDate"} & set(post.json) and fake.emailed == []
    assert readback.path == poll.path == after.path == f"/v1/invoices/{invoice['Id']}"
    assert lookup.query["Number"] == "1042" and delete.path == f"/v1/invoices/{invoice['Id']}" and delete.json is None
    assert listing.query["ClientIds"] == "9501" and listing.query["Query"] == APPROVED_LABEL
    assert fake.exports == [] and not any(r.path.endswith("/pdf") for r in fake.mock.requests)  # never exported
    # voided: Gorelo keeps it, listed as Void, and nobody can remove it
    assert invoice["Status"] == {"Id": 4, "Name": "Void"} and ("invoice", invoice["Id"]) not in fake.deleted
    assert fake.leftovers() == [] and fake.residue() == [f"invoice {invoice['Id']}"]
    record = invoice_record_of(report)
    assert (record.id, record.label) == (invoice["Id"], APPROVED_LABEL)
    assert record.details == {"status_id": 4, "number": 1042, "display_number": "INV-1042"}  # 4 once it was voided
    assert record.outcome == VOIDED_OUTCOME and not record.cleaned
    assert report.summary == {"created": 1, "cleaned": 0, "leftovers": 0, "undeletable": 1, "unresolved_intents": 0}
    assert [(r.kind, r.id, r.outcome) for r in report.cleanup.undeletable] == [("invoice", invoice["Id"], VOIDED_OUTCOME)]
    assert report.cleanup.cleaned == [] and report.cleanup.leftovers == [] and report.cleanup.ok and report.refused == 0
    notes = found.notes
    assert "nobody could be emailed: all 4 contacts of client 9501 are Inactive and none of its 1 locations names a billing contact" in notes
    assert (
        "approved invoice number consumed: Number 1042, DisplayNumber 'INV-1042' (an Approved invoice cannot be removed, "
        "only voided, and it stays listed as Void)"
    ) in notes
    external = f"{invoice['Id'][:8]}-5e1e-4a7b-9c3d-{invoice['Id'][-12:]}"
    assert f"approved invoice INV-1042: pushed to the accounting system, ExternalId {external}, PaymentLink set" in notes
    assert "no-tax check passed: TotalTax is 0 and Total is 1.0 on the read after the void" in notes
    assert not any("WARNING" in note for note in notes) and not any(external in line and "PaymentLink" in line and "https" in line for line in lines)
    assert "area approved_invoice: pass, 10 requests" in lines
    assert f"known undeletable: invoice {invoice['Id']}: {VOIDED_OUTCOME}" in lines
    assert "records: 1 created, 0 cleaned, 0 left over, 1 known undeletable, 0 announced without an id" in lines
    assert "result: nothing left over" not in lines  # an invoice stays in Gorelo, as Void: never claimed to be gone
    assert "result: cleanup complete; 1 known undeletable record remains (see above)" in lines
    assert lines[-1] == "result: PASSED"
    assert not fake.mock.unmatched


async def test_approved_invoice_is_skipped_by_default_with_the_reason_and_not_one_request(run, lines):
    fake = FakeGorelo()
    report = await run(fake, areas=["approved_invoice"])  # --only approved_invoice without --with-approved-invoice
    found = results(report)["approved_invoice"]
    assert found.status == "skipped" and found.requests == 0
    assert found.detail == (
        "not live-tested by default: approves a $1 invoice on the test client, which pushes it to the connected accounting "
        "system, then voids it"
    )
    assert found.detail == write_matrix.APPROVED_INVOICE_SKIP_REASON
    assert fake.mock.requests == [] and fake.invoices == {}  # not even a precondition read
    assert report.ok and report.exit_code == 0 and report.summary["created"] == 0 and report.cleanup.ok
    assert "area approved_invoice: skipped, 0 requests (" + write_matrix.APPROVED_INVOICE_SKIP_REASON + ")" in lines
    assert lines[-1] == "result: PASSED"
    # the other flags do not open it either
    other = await run(
        FakeGorelo(), areas=["approved_invoice"], with_items=True, with_invoices=True,
        started=datetime(2099, 10, 2, 10, 16, 0, tzinfo=timezone.utc),
    )
    assert results(other)["approved_invoice"].status == "skipped" and results(other)["approved_invoice"].requests == 0


@pytest.mark.parametrize(
    "only", [None, ["approved_invoice", "invoices"], ["invoices"], ["approved_invoice", "uptime"], ["items", "approved_invoice"]]
)
async def test_approved_invoice_flag_with_any_other_area_is_a_usage_error_before_anything_is_sent(run, tmp_path, only):
    fake = FakeGorelo()
    with pytest.raises(write_matrix.UsageError, match="--with-approved-invoice runs the approved_invoice area alone"):
        await run(fake, areas=only, with_approved_invoice=True)
    assert fake.mock.requests == [] and not (tmp_path / "runs").exists()  # no request, no manifest


def test_approved_invoice_the_command_line_makes_that_usage_error_an_exit_status_of_two(capsys):
    assert write_matrix.main(["--with-approved-invoice"]) == 2
    assert "usage: --with-approved-invoice runs the approved_invoice area alone: use --only approved_invoice" in capsys.readouterr().err
    assert write_matrix.main(["--with-approved-invoice", "--only", "approved_invoice,uptime"]) == 2
    assert "usage: --with-approved-invoice runs the approved_invoice area alone" in capsys.readouterr().err
    for other in ("--with-items", "--with-invoices", "--leftovers", "--skip-email"):
        assert write_matrix.main(["--with-approved-invoice", "--only", "approved_invoice", other]) == 2
        assert f"usage: --with-approved-invoice runs alone: it cannot be combined with {other}" in capsys.readouterr().err


def test_approved_invoice_the_command_line_runs_the_area_alone_with_its_flag(tmp_path, monkeypatch, capsys, make_settings):
    import functools

    real = write_matrix.run_matrix
    fake = FakeGorelo()
    monkeypatch.setattr(
        write_matrix, "run_matrix",
        functools.partial(
            real, settings=make_settings(destructive=True), transport=fake.mock.transport, directory=tmp_path / "runs", pace=0,
            lookup_wait=0, started=RUN_START, sync_interval=0, sync_polls=3,
        ),
    )
    assert write_matrix.main(["--with-approved-invoice", "--only", "approved_invoice"]) == 0
    out = capsys.readouterr().out
    assert "areas: approved_invoice" in out and "area approved_invoice: pass, 10 requests" in out
    assert out.rstrip().endswith("result: PASSED") and fake.residue() == [f"invoice {the_invoice(fake)['Id']}"]


@pytest.mark.parametrize(
    "flags, named",
    [
        ({"with_items": True}, "--with-items"),
        ({"with_invoices": True}, "--with-invoices"),
        ({"leftovers": True}, "--leftovers"),
        ({"skip_email": True}, "--skip-email"),
        ({"with_items": True, "with_invoices": True, "leftovers": True}, "--with-items, --with-invoices, --leftovers"),
    ],
)
async def test_approved_invoice_flag_beside_any_other_flag_is_a_usage_error_before_anything_is_sent(run, tmp_path, flags, named):
    # the run that approves a real invoice does that and nothing else: no other flag opens anything beside it
    fake = FakeGorelo()
    with pytest.raises(write_matrix.UsageError, match=f"--with-approved-invoice runs alone: it cannot be combined with {named}$"):
        await run(fake, areas=["approved_invoice"], with_approved_invoice=True, **flags)
    assert fake.mock.requests == [] and not (tmp_path / "runs").exists()  # no request, no manifest


async def test_approved_invoice_is_announced_first_and_recorded_with_the_status_gorelo_stored(approved, monkeypatch):
    fake = FakeGorelo()
    seen = []
    real = Manifest.intent

    def intent(self, kind, label, details=None):
        seen.append((kind, label, details))
        fake.events.append(("intent", kind, label))
        return real(self, kind, label, details)

    monkeypatch.setattr(Manifest, "intent", intent)
    report = await approved(fake)
    assert report.ok
    assert seen == [("invoice", APPROVED_LABEL, {"status_id": 5})]  # announced as an invoice of status 5, nothing more
    kinds = [event[:2] for event in fake.events]
    assert kinds.index(("intent", "invoice")) < kinds.index(("request", "POST"))  # the intent comes before the create


# the preconditions: nobody could be emailed (checked before any write; a skip with the reason when one does not hold)


async def test_approved_invoice_an_active_contact_skips_the_area_after_one_read_and_nothing_is_written(approved):
    fake = FakeGorelo(test_client_contacts=[*inactive_test_client_contacts(), make_test_client_contact(9, {"Id": 1, "Name": "Active"})])
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "skipped" and found.requests == 1
    assert found.detail == (
        "precondition not met: 1 of the 5 contacts of client 9501 are not Inactive (ids 9109), so an Approved invoice "
        "might be emailed to one of them; nothing was sent"
    )
    assert [(r.method, r.path) for r in fake.mock.requests] == [("GET", "/v1/contacts")]
    assert fake.invoices == {} and report.ok and report.summary["created"] == 0 and report.summary["unresolved_intents"] == 0


@pytest.mark.parametrize(
    "status",
    [
        {"Id": 1, "Name": "Active"},
        {"Id": 2},  # the status is matched by its name: the spec publishes no scale for contacts
        {"Name": ""},
        {"Name": 5},
        None,
        "Inactive",
        DROP,
    ],
)
async def test_approved_invoice_a_contact_that_is_not_provably_inactive_stops_the_area(approved, status):
    fake = FakeGorelo(test_client_contacts=[*inactive_test_client_contacts(2), make_test_client_contact(9, status)])
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "skipped" and found.requests == 1 and "1 of the 3 contacts of client 9501 are not Inactive (ids 9109)" in found.detail
    assert fake.invoices == {} and all(r.method == "GET" for r in fake.mock.requests)


async def test_approved_invoice_a_row_that_is_not_a_contact_stops_the_area_too(approved):
    fake = FakeGorelo(test_client_contacts=[*inactive_test_client_contacts(2), "not a row", {"FirstName": "no id", "ClientId": TEST_CLIENT}])
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "skipped" and "2 of the 4 contacts of client 9501 are not Inactive (ids ?, ?)" in found.detail


async def test_approved_invoice_the_inactive_status_is_read_ignoring_case_and_no_contact_at_all_is_fine(approved):
    fake = FakeGorelo(test_client_contacts=[make_test_client_contact(1, {"Id": 2, "Name": "INACTIVE"}), make_test_client_contact(2, {"Id": 2, "Name": " inactive "})])
    assert results(await approved(fake))["approved_invoice"].status == "pass"
    nobody = FakeGorelo(test_client_contacts=[])
    report = await approved(nobody, started=datetime(2099, 10, 2, 10, 16, 0, tzinfo=timezone.utc))
    assert results(report)["approved_invoice"].status == "pass"


async def test_approved_invoice_only_the_contacts_of_test_client_are_asked_for(approved):
    # a contact of another client is no business of the check (and the run's own contacts are listed too, if any exist)
    other = make_test_client_contact(1, {"Id": 1, "Name": "Active"}, ClientId=SECOND)
    fake = FakeGorelo(test_client_contacts=[*inactive_test_client_contacts(2), other])
    report = await approved(fake)
    assert results(report)["approved_invoice"].status == "pass"
    assert fake.mock.requests[0].query["ClientIds"] == "9501"


async def test_approved_invoice_a_second_page_of_contacts_is_read_and_an_active_contact_on_it_stops_the_area(approved):
    rows = [make_test_client_contact(n, {"Id": 2, "Name": "Inactive"}, Id=200000 + n) for n in range(250)]
    fake = FakeGorelo(test_client_contacts=rows)
    report = await approved(fake)
    assert results(report)["approved_invoice"].status == "pass"
    contact_reads = [r for r in fake.mock.requests if r.method == "GET" and r.path == "/v1/contacts"]
    assert len(contact_reads) == 2 and "Cursor" not in contact_reads[0].query and contact_reads[1].query["Cursor"] == "c200"
    assert area_ops(fake, report)["approved_invoice"] == ["GET /v1/contacts", *GOLDEN_APPROVED]
    rows[240] = make_test_client_contact(240, {"Id": 1, "Name": "Active"}, Id=200240)
    other = FakeGorelo(test_client_contacts=rows)
    skipped = await approved(other, started=datetime(2099, 10, 2, 10, 16, 0, tzinfo=timezone.utc))
    found = results(skipped)["approved_invoice"]
    assert found.status == "skipped" and found.requests == 2 and "(ids 200240)" in found.detail and other.invoices == {}


async def test_approved_invoice_too_many_contacts_to_check_is_a_skip(approved, monkeypatch):
    monkeypatch.setattr(write_matrix, "CONTACT_PAGES", 1)
    fake = FakeGorelo(test_client_contacts=[make_test_client_contact(n, {"Id": 2, "Name": "Inactive"}, Id=200000 + n) for n in range(250)])
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "skipped" and found.requests == 1
    assert found.detail == (
        "precondition not met: client 9501 has more than 200 contacts, too many to check that every one is Inactive; "
        "nothing was sent"
    )


async def test_approved_invoice_a_contact_list_that_cannot_be_read_fails_the_area_without_writing(approved):
    fake = FakeGorelo()
    fake.fail("GET /v1/contacts", error_envelope(500, [("070500", "boom")]))
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "list_contacts" in found.detail and found.requests == 1  # it cannot be said nobody is emailed
    assert fake.invoices == {} and not report.ok and report.summary["created"] == 0


@pytest.mark.parametrize(
    "billing",
    # ids in any form (the spec's comma separated text, production's JSON list text, a real list, a number), text that is not
    # an empty list however it is written, and no key at all: none of these proves that nobody is named
    [
        "9600", "1,2", " 5 ", ["5"], 5, True, "[9600]", "[1, 2]", " [ 5 ] ", "[[]]", "[]x", "{}", "null",
        pytest.param("[" * 100000 + "]" * 100000, id="nested-too-deep-to-read"),
        DROP,
    ],
)
async def test_approved_invoice_a_location_with_billing_contacts_stops_the_area_after_two_reads(approved, billing):
    fake = FakeGorelo()
    if billing is DROP:  # no key: it cannot be said that nobody is named
        fake.locations[TEST_CLIENT][0].pop("BillingContactIds")
    else:
        fake.locations[TEST_CLIENT][0]["BillingContactIds"] = billing
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "skipped" and found.requests == 2
    assert found.detail == (
        "precondition not met: 1 of the 1 locations of client 9501 name billing contacts or have no readable "
        "BillingContactIds (ids 9001), so an Approved invoice might be emailed to them; nothing was sent"
    )
    assert [(r.method, r.path) for r in fake.mock.requests] == [("GET", "/v1/contacts"), ("GET", "/v1/clients/9501/locations")]
    assert fake.invoices == {} and report.ok


@pytest.mark.parametrize("billing", ["", "  ", None, [], "[]", " [ ] ", "[]\n", "[\t]"])
async def test_approved_invoice_an_empty_or_null_billing_contact_list_is_fine(approved, billing):
    # "[]" is what production answers for a location that names nobody; the other spellings are the published
    # text (blank), null, a real empty list, and the same JSON list written with spaces
    fake = FakeGorelo()
    fake.locations[TEST_CLIENT][0]["BillingContactIds"] = billing
    assert results(await approved(fake))["approved_invoice"].status == "pass"


async def test_approved_invoice_only_the_locations_of_test_client_matter_and_one_bad_location_stops_it(approved):
    fake = FakeGorelo()
    fake.locations[SECOND][0]["BillingContactIds"] = "9600"  # another client's: not looked at
    assert results(await approved(fake))["approved_invoice"].status == "pass"
    two = FakeGorelo()
    two.locations[TEST_CLIENT].append({"Id": 9003, "ClientId": TEST_CLIENT, "Name": "Annex", "BillingContactIds": "77"})
    report = await approved(two, started=datetime(2099, 10, 2, 10, 16, 0, tzinfo=timezone.utc))
    assert "1 of the 2 locations of client 9501" in results(report)["approved_invoice"].detail and "(ids 9003)" in results(report)["approved_invoice"].detail


@pytest.mark.parametrize("catalog", [[], [catalog_item(901, "a", SECOND)], [catalog_item(901, "a", 5555)]])
async def test_approved_invoice_without_a_usable_item_the_area_is_skipped_after_three_reads(approved, catalog):
    fake = FakeGorelo(catalog=catalog)
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "skipped" and found.requests == 3
    assert found.detail == "no active product of nobody or of client 9501 among the first 50 catalog items to put on the invoice"
    assert [r.method for r in fake.mock.requests] == ["GET", "GET", "GET"] and fake.invoices == {} and report.ok


async def test_approved_invoice_the_item_is_the_first_active_product_of_nobody_or_of_test_client(approved):
    fake = FakeGorelo()  # the default catalog: another client's product first, then one of nobody
    await approved(fake)
    assert next(r.json for r in fake.mock.requests if r.method == "POST")["LineItems"][0]["ItemId"] == GLOBAL_PRODUCT


# waiting for the push to the accounting system


async def test_approved_invoice_a_push_that_takes_a_few_reads_is_waited_for(approved):
    fake = FakeGorelo(sync_after=2)  # the create's read-back and the first read still say ExternalId is null
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert report.ok and found.status == "pass"
    assert area_ops(fake, report)["approved_invoice"] == [*GOLDEN_APPROVED[:5], READ, *GOLDEN_APPROVED[5:]]
    assert the_invoice(fake)["ExternalId"] is not None and fake.residue()


def flaky_reads(fake: FakeGorelo, failing: set[int] | dict[int, object], answer=None) -> list[str]:
    """The reads of an invoice (GET /v1/invoices/{invoiceId}) whose 1-based position is in `failing` get `answer` instead (an
    error envelope, an httpx.Response or an exception such as a timeout); `failing` may also map positions to answers. Every
    other read is served as usual. Returns the paths of the reads so far."""
    original = fake.handlers[READ]
    answers = failing if isinstance(failing, dict) else {position: answer for position in failing}
    reads: list[str] = []

    def serve(request):
        reads.append(request.path)
        return answers[len(reads)] if len(reads) in answers else original(request)

    fake.handlers[READ] = serve
    return reads


TOO_MANY = httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate_limited", "message": "slow down", "retry_after": "0s"})
FAILED_READ = "get_invoice failed at read 1 while waiting for the accounting system, so it is asked again at the next poll: "


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(error_envelope(500, [("070500", "boom")]), id="500"),
        pytest.param(httpx.Response(502, text="Bad Gateway"), id="502-without-an-envelope"),
        pytest.param(httpx.Response(504, text="Gateway Timeout"), id="504"),
        pytest.param(httpx.ReadTimeout("slow"), id="timeout"),
    ],
)
async def test_approved_invoice_a_poll_read_that_fails_means_not_synced_yet_and_the_invoice_is_still_voided(approved, answer):
    # the create's read-back is read 1 and the first poll is read 2; a GET writes nothing, so a failed one says nothing
    # about the invoice and must not end the wait before the void (the invoice may already be in the accounting system).
    # A 429 is no case of its own here: the client asks again by itself (the retries are off only for the create), and only a
    # 429 that outlasts them fails the poll (see test_approved_invoice_a_429_that_outlasts_the_clients_retries_on_a_poll_read...)
    fake = FakeGorelo()
    flaky_reads(fake, {2}, answer)
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok, found.detail
    ops = area_ops(fake, report)["approved_invoice"]
    assert ops == [*GOLDEN_APPROVED[:5], READ, *GOLDEN_APPROVED[5:]]  # the failed poll is one more read, then the usual void
    assert ops.count("DELETE /v1/invoices/{invoiceId}") == 1 and area_ops(fake, report)["cleanup"] == []
    assert any(note.startswith(FAILED_READ) for note in found.notes)  # it is said, not hidden
    assert the_invoice(fake)["Status"] == {"Id": 4, "Name": "Void"} and fake.leftovers() == [] and fake.residue()
    record = invoice_record_of(report)
    assert record.outcome == VOIDED_OUTCOME and record.details["status_id"] == 4 and report.refused == 0


async def test_approved_invoice_several_failed_polls_in_a_row_are_waited_out_and_each_text_is_noted_once(approved):
    fake = FakeGorelo()
    flaky_reads(fake, {2, 3}, error_envelope(500, [("070500", "boom")]))
    report = await approved(fake)  # three polls after the first: reads 2 and 3 fail, read 4 sees ExternalId
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok, found.detail
    assert area_ops(fake, report)["approved_invoice"] == [*GOLDEN_APPROVED[:5], READ, READ, *GOLDEN_APPROVED[5:]]
    assert len([note for note in found.notes if note.startswith("get_invoice failed at read")]) == 1  # one text, one note


def traced(n: int) -> str:
    """A trace id of Gorelo's shape that is different for every n: Gorelo gives every request its own."""
    return f"00-{n:032x}-{n:016x}-01"


async def test_approved_invoice_failed_polls_that_say_the_same_with_different_trace_ids_are_noted_once(approved):
    # every enveloped error ends with " [trace <id>]" and the id differs per request, so the notes must compare the
    # texts without it, or an outage would fill the summary with one note per failed poll
    fake = FakeGorelo()
    flaky_reads(fake, {n: error_envelope(500, [("070500", "boom")], trace_id=traced(n)) for n in (2, 3)})
    report = await approved(fake)  # reads 2 and 3 fail, each with a trace id of its own, read 4 sees ExternalId
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok, found.detail
    (note,) = [note for note in found.notes if note.startswith("get_invoice failed at read")]
    # the note keeps the FIRST full text, its trace id included, and the repeat adds nothing
    assert note == (
        "get_invoice failed at read 1 while waiting for the accounting system, so it is asked again at the next poll: "
        f"Gorelo rejected get_invoice (HTTP 500, code 070500): boom [trace {traced(2)}]"
    )
    assert traced(3) not in " ".join(found.notes)


async def test_approved_invoice_failed_polls_with_different_words_are_each_noted_whatever_their_trace_ids(approved):
    fake = FakeGorelo()
    flaky_reads(
        fake,
        {
            2: error_envelope(500, [("070500", "boom")], trace_id=traced(2)),
            3: error_envelope(502, [("070502", "bad gateway")], trace_id=traced(3)),
            4: error_envelope(500, [("070500", "boom")], trace_id=traced(4)),
        },
    )
    report = await approved(fake)  # reads 2, 3 and 4 fail, read 5 sees ExternalId
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok, found.detail
    first, second = [note for note in found.notes if note.startswith("get_invoice failed at read")]
    assert first.startswith("get_invoice failed at read 1 ") and f"(HTTP 500, code 070500): boom [trace {traced(2)}]" in first
    assert second.startswith("get_invoice failed at read 2 ") and f"(HTTP 502, code 070502): bad gateway [trace {traced(3)}]" in second
    assert traced(4) not in " ".join(found.notes)  # the third failure says what the first one said


async def test_approved_invoice_when_every_poll_fails_the_summary_keeps_the_last_full_text_with_its_own_trace_id(approved):
    fake = FakeGorelo()
    flaky_reads(fake, {n: error_envelope(502, [("070502", "bad gateway")], trace_id=traced(n)) for n in range(2, 100)})
    report = await approved(fake)  # the test's wait: reads 2 to 5, every one of them fails with a trace id of its own
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and found.detail.startswith(NOT_YET_SYNCED)
    failed = [note for note in found.notes if note.startswith("get_invoice failed at read")]
    assert len(failed) == 1 and traced(2) in failed[0]  # one note, with the first full text
    (summary,) = [note for note in found.notes if "was not seen" in note]
    assert f"4 of them failed (the last: Gorelo rejected get_invoice (HTTP 502, code 070502): bad gateway [trace {traced(5)}])" in summary
    assert traced(3) not in " ".join(found.notes) and traced(4) not in " ".join(found.notes)


def test_without_trace_drops_only_the_trace_that_ends_the_text():
    cut = write_matrix.without_trace
    rejected = format_gorelo_error(
        GoreloAPIError(
            "x", status=500, op_key="GET /v1/invoices/{invoiceId}", kind="envelope",
            notifications=[{"Code": "070500", "Message": "boom"}], trace_id=traced(7),
        ),
        "get_invoice",
    )
    assert rejected == f"Gorelo rejected get_invoice (HTTP 500, code 070500): boom [trace {traced(7)}]"  # the shape tools/_common.py gives
    assert cut(rejected) == "Gorelo rejected get_invoice (HTTP 500, code 070500): boom"
    assert cut("boom [trace 1]") == "boom" and cut("boom  [trace 1] ") == "boom" and cut("boom [trace ]") == "boom"
    for untouched in (
        "", "boom", "boom [trace 1] and more", "[trace 1] boom", "boom [trace 1", "boom [trace]", "boom [traces 1]", "boom (trace 1)",
    ):
        assert cut(untouched) == untouched
    # two failures with the same words and different trace ids are the same failure
    assert cut(rejected) == cut(rejected.replace(traced(7), traced(8)))
    # a text that was cut at FAILURE_LIMIT may have lost its trace already: the comparison is made on the whole text
    assert cut(rejected)[: write_matrix.FAILURE_LIMIT] == cut(rejected)


async def test_approved_invoice_when_every_poll_read_fails_nothing_is_voided_and_the_area_fails_with_the_usual_hint(approved, lines):
    fake = FakeGorelo()
    flaky_reads(fake, set(range(2, 100)), error_envelope(502, [("070502", "bad gateway")]))
    report = await approved(fake)  # the test's wait: 1 + 3 polls, all of them fail
    found = results(report)["approved_invoice"]
    invoice = the_invoice(fake)
    assert found.status == "FAIL"
    assert found.detail == f"{NOT_YET_SYNCED}{void_command(report)}"
    ops = area_ops(fake, report)
    assert ops["approved_invoice"] == [*GOLDEN_APPROVED[:5], *[READ] * 4]  # the read-back, then 4 polls, no lookup, no DELETE
    assert not any(r.method == "DELETE" for r in fake.mock.requests)
    assert ops["cleanup"] == [READ] and invoice["Status"]["Id"] == 5 and report.summary["leftovers"] == 1 and not report.ok
    # the area says that it could not see ExternalId at all, not that it was null
    assert any(note.startswith(FAILED_READ) for note in found.notes)
    assert not any("ExternalId was still null" in note for note in found.notes)
    summary = [note for note in found.notes if note.startswith("approved invoice ") and "was not seen" in note]
    assert len(summary) == 1 and "ExternalId was not seen in 4 reads over 0 s, 4 of them failed (the last: " in summary[0]
    assert summary[0].endswith("), so it was not voided") and "bad gateway" in summary[0]
    assert any("--void-approved" in line for line in lines) and lines[-1] == "result: FAILED"


async def test_approved_invoice_the_note_of_a_failed_last_poll_says_that_no_poll_is_left(approved):
    fake = FakeGorelo(sync_after=None)
    flaky_reads(fake, {2: error_envelope(500, [("070500", "boom")]), 5: error_envelope(502, [("070502", "bad gateway")])})
    report = await approved(fake)  # reads: the read-back, then the polls 2 to 5 (the last one is read 5)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and found.detail.startswith(NOT_YET_SYNCED)
    assert area_ops(fake, report)["approved_invoice"] == [*GOLDEN_APPROVED[:5], *[READ] * 4]
    first, last = [note for note in found.notes if note.startswith("get_invoice failed at read")]
    assert first.startswith("get_invoice failed at read 1 while waiting for the accounting system, so it is asked again at the next poll: ")
    assert last.startswith("get_invoice failed at read 4 while waiting for the accounting system, and no poll is left: ")
    assert "ExternalId was not seen in 4 reads over 0 s, 2 of them failed (the last: " in " ".join(found.notes)
    assert not any(r.method == "DELETE" for r in fake.mock.requests)


async def test_approved_invoice_a_failed_poll_does_not_hide_a_status_that_changed_in_the_meantime(approved):
    fake = FakeGorelo(sync_after=None)
    original = fake.handlers[READ]
    reads = []

    def serve(request):
        reads.append(request.path)
        if len(reads) == 2:
            return error_envelope(500, [("070500", "boom")])
        if len(reads) == 3:
            fake.invoices[request.path.rsplit("/", 1)[1]]["Status"] = {"Id": 3, "Name": "Paid"}
        return original(request)

    fake.handlers[READ] = serve
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "has Status.Id 3 while waiting for the accounting system, not 5 (Approved)" in found.detail
    assert not any(r.method == "DELETE" for r in fake.mock.requests)
    # the read-back, the poll that failed, the poll that saw Paid: it stopped at the status, not at the failed read and not later
    assert area_ops(fake, report)["approved_invoice"] == [*GOLDEN_APPROVED[:5], READ, READ]


async def test_approved_invoice_a_guard_refusal_while_waiting_is_never_read_as_not_synced_yet(approved, monkeypatch):
    fake = FakeGorelo(sync_after=None)
    real = write_matrix.Matrix.tool
    polls = []

    async def tool(self, name, /, **arguments):
        if name == "get_invoice":
            polls.append(name)
            if len(polls) == 2:  # the first poll
                raise write_matrix.GuardTripped("blocked GET /invoices/x: refused by the test")
        return await real(self, name, **arguments)

    monkeypatch.setattr(write_matrix.Matrix, "tool", tool)
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and found.detail.startswith("the guard refused a request, the run stops: ")
    assert [r.tripped for r in report.results] == [True] and len(polls) == 2  # not one more poll after the refusal
    assert not any(r.method == "DELETE" for r in fake.mock.requests)


async def test_approved_invoice_a_read_that_answers_another_invoice_is_a_failed_check_not_a_failed_read(approved):
    fake = FakeGorelo(sync_after=None)
    original = fake.handlers[READ]
    reads = []

    def serve(request):
        reads.append(request.path)
        answer = original(request)
        if len(reads) == 2:
            answer = envelope({**answer["Data"], "Id": invoice_guid(777)})
        return answer

    fake.handlers[READ] = serve
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and found.detail == "get_invoice returned another invoice"
    assert area_ops(fake, report)["approved_invoice"] == [*GOLDEN_APPROVED[:5], READ]  # the first poll was the last request
    assert not any(r.method == "DELETE" for r in fake.mock.requests)


async def test_approved_invoice_is_polled_every_five_seconds_for_at_most_120_and_never_voided_when_it_never_syncs(run, monkeypatch):
    delays = []
    real_sleep = asyncio.sleep

    async def recorded(delay, *args, **kwargs):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(write_matrix.asyncio, "sleep", recorded)
    fake = FakeGorelo(sync_after=None)
    report = await run(fake, areas=["approved_invoice"], with_approved_invoice=True)  # the real defaults: 5 s, 24 waits
    invoice = the_invoice(fake)
    own = fake.mock.requests[: results(report)["approved_invoice"].requests]  # the area's requests, not the cleanup's read
    reads = [r for r in own if r.method == "GET" and r.path == f"/v1/invoices/{invoice['Id']}"]
    assert len(reads) == 1 + 25  # the create's read-back, the first read, and 24 more
    assert [delay for delay in delays if delay == 5.0] == [5.0] * 24  # 24 waits of 5 s: 120 s in all
    assert not any(r.method == "DELETE" for r in fake.mock.requests)
    assert results(report)["approved_invoice"].status == "FAIL"
    assert write_matrix.SYNC_INTERVAL == 5.0 and write_matrix.SYNC_POLLS == 24


async def test_approved_invoice_that_never_syncs_is_not_voided_the_area_fails_with_the_command_and_the_cleanup_leaves_it(
    approved, lines, make_settings
):
    fake = FakeGorelo(sync_after=None)
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    invoice = the_invoice(fake)
    assert found.status == "FAIL"
    assert found.detail == f"{NOT_YET_SYNCED}{void_command(report)}"
    ops = area_ops(fake, report)
    assert ops["approved_invoice"] == [*GOLDEN_APPROVED[:5], *[READ] * 4]  # the read-back, then 1 + 3 reads (the test's wait)
    assert not any(r.method == "DELETE" for r in fake.mock.requests)  # no lookup by Number and no DELETE: not voided
    assert ops["cleanup"] == [READ]  # read, found Approved, left for the user: the cleanup never voids without the flag
    assert invoice["Status"]["Id"] == 5 and invoice["ExternalId"] is None
    assert not report.ok and report.exit_code == 1
    assert report.summary == {"created": 1, "cleaned": 0, "leftovers": 1, "undeletable": 0, "unresolved_intents": 0}
    assert fake.leftovers() == [f"invoice {invoice['Id']}"]
    assert "approved invoice INV-1042: ExternalId was still null after 4 reads over 0 s, so it was not voided" in found.notes
    leftover = f"  LEFTOVER   invoice {invoice['Id']} {APPROVED_LABEL} (left for the user: invoice INV-1042 has status Approved (id 5)"
    assert any(line.startswith(leftover) and line.endswith(f"--void-approved ({GORELO_ONLY}))") for line in lines)
    assert "result: SOMETHING IS LEFT OVER" in lines and lines[-1] == "result: FAILED"
    # the summary ends by saying how to void the Approved invoice that is left over, next to the general cleanup hint
    hint = f"if anything is left over: python -m scripts.live.cleanup {report.manifest_path}"
    void = f"an Approved invoice is left over: check the accounting system, then void it with: {void_command(report)}"
    assert lines.index(void) == lines.index(hint) + 1 and lines[-2] == void
    # the user checks the accounting system, then voids it with the command the area printed
    again = await cleanup_with_the_flag(fake, report, make_settings, lines)
    assert again.ok and invoice["Status"]["Id"] == 4 and fake.leftovers() == [] and fake.residue() == [f"invoice {invoice['Id']}"]
    record = invoice_record_of(report)
    assert record.outcome == "voided by the cleanup with delete_invoice (still listed as Void)" and not record.cleaned
    assert record.details["status_id"] == 4
    # that cleanup says what the area's hint only said in brackets: its void is in Gorelo only, the Xero copy stays open
    assert again.voided == ["INV-1042"]
    assert lines[-2:] == [
        f"reminder: approved invoice INV-1042: {REMINDER}",
        "result: cleanup complete; 1 known undeletable record remains (see above)",
    ]


# what Gorelo made of it


async def test_approved_invoice_created_as_paid_fails_the_area_and_is_left_for_the_user(approved, lines):
    fake = FakeGorelo()
    fake.on_invoice_created(lambda invoice: invoice.update(Status={"Id": 3, "Name": "Paid"}))
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL"
    assert "create_approved_invoice Status.Id: expected 5 (Approved), got 3 (Paid)" in found.detail
    assert "refuses to delete or void a Paid invoice, so it is left for the user" in found.detail
    ops = area_ops(fake, report)
    assert ops["approved_invoice"] == GOLDEN_APPROVED[:5]  # not one request after the read-back
    assert ops["cleanup"] == [READ]  # the cleanup reads it, finds it Paid and leaves it
    assert not any(r.method == "DELETE" for r in fake.mock.requests)
    invoice = the_invoice(fake)
    assert invoice["Status"]["Id"] == 3 and fake.leftovers() == [f"invoice {invoice['Id']}"]
    assert invoice_record_of(report).details["status_id"] == 3  # what Gorelo stored, so the guard would refuse any DELETE
    outcome = (
        "left for the user: invoice INV-1042 has status Paid (id 3); Gorelo refuses to delete or void a Paid invoice, "
        "so it stays as it is"
    )
    assert invoice_record_of(report).outcome == outcome
    assert f"  LEFTOVER   invoice {invoice['Id']} {APPROVED_LABEL} ({outcome})" in lines
    assert not report.ok and report.summary["leftovers"] == 1 and lines[-1] == "result: FAILED"
    assert not any(line.startswith("an Approved invoice is left over") for line in lines)  # a Paid one cannot be voided


async def test_approved_invoice_created_as_a_draft_is_deleted_by_the_cleanup_and_the_area_fails(approved):
    fake = FakeGorelo()
    fake.on_invoice_created(lambda invoice: invoice.update(Status={"Id": 1, "Name": "Draft"}))
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "create_approved_invoice Status.Id (Approved): expected 5, got 1" in found.detail
    assert invoice_record_of(report).details["status_id"] == 1  # what Gorelo stored: a Draft is deleted like any Draft
    assert area_ops(fake, report)["cleanup"] == [READ, "GET /v1/invoices", "DELETE /v1/invoices/{invoiceId}"]
    assert fake.leftovers() == [] and report.cleanup.ok and not report.ok


@pytest.mark.parametrize(
    "damage, said",
    [
        pytest.param(lambda inv: inv.update(Reference="something else"), "create_approved_invoice Reference: expected", id="reference"),
        pytest.param(lambda inv: inv.update(ClientId=SECOND), "create_approved_invoice ClientId: expected 9501, got 9502", id="client"),
        pytest.param(lambda inv: inv["LineItems"].append(dict(inv["LineItems"][0])), "has 2 line items, expected exactly 1", id="two-lines"),
        pytest.param(lambda inv: inv["LineItems"][0].update(UnitPrice=50.0), "line UnitPrice: expected 1.0, got 50.0", id="price"),
        pytest.param(lambda inv: inv["LineItems"][0].update(Quantity=2), "line Quantity: expected 1, got 2", id="quantity"),
        pytest.param(lambda inv: inv.update(Total=0), "create_approved_invoice Total is 0, expected an amount above 0", id="total-zero"),
        pytest.param(lambda inv: inv.update(Total=None), "create_approved_invoice Total is None, expected an amount above 0", id="no-total"),
        pytest.param(lambda inv: inv.update(Total=True), "create_approved_invoice Total is True, expected an amount above 0", id="total-true"),
        pytest.param(lambda inv: inv.update(Total=-1.0), "create_approved_invoice Total is -1.0, expected an amount above 0", id="total-negative"),
    ],
)
async def test_approved_invoice_a_failing_check_after_the_create_stops_the_area_and_leaves_the_invoice_for_the_user(approved, damage, said):
    fake = FakeGorelo()
    fake.on_invoice_created(damage)
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and said in found.detail
    assert area_ops(fake, report)["approved_invoice"] == GOLDEN_APPROVED[:5]  # nothing after the read-back: no wait, no void
    assert not any(r.method == "DELETE" for r in fake.mock.requests)
    invoice = the_invoice(fake)
    assert invoice["Status"]["Id"] == 5 and not report.ok and report.summary["leftovers"] == 1
    assert invoice_record_of(report).details["status_id"] == 5 and report.summary["unresolved_intents"] == 0


async def test_approved_invoice_a_status_that_changes_while_waiting_is_never_voided(approved):
    fake = FakeGorelo(sync_after=None)
    original = fake.handlers[f"GET /v1/invoices/{{invoiceId}}"]
    reads = []

    def paid_in_the_meantime(request):
        reads.append(request.path)
        if len(reads) == 3:  # the create's read-back, the first read, and now the second
            fake.invoices[request.path.rsplit("/", 1)[1]]["Status"] = {"Id": 3, "Name": "Paid"}
        return original(request)

    fake.handlers["GET /v1/invoices/{invoiceId}"] = paid_in_the_meantime
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL"
    assert "approved invoice INV-1042 has Status.Id 3 while waiting for the accounting system, not 5 (Approved): nothing was voided" in found.detail
    assert not any(r.method == "DELETE" for r in fake.mock.requests) and report.summary["leftovers"] == 1


async def test_approved_invoice_with_no_number_cannot_be_found_by_delete_invoice_and_is_left_for_the_cleanup_flag(approved, lines, make_settings):
    fake = FakeGorelo()
    fake.on_invoice_created(lambda invoice: invoice.update(Number=None))
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    invoice = the_invoice(fake)
    assert found.status == "FAIL"
    assert found.detail == (
        f"approved invoice INV-1042 has no Number, so delete_invoice cannot find it; void it with: {void_command(report)}"
    )
    assert area_ops(fake, report)["approved_invoice"] == GOLDEN_APPROVED[:6]  # up to the first read: no lookup, no DELETE
    assert invoice["Status"]["Id"] == 5 and not report.ok
    assert invoice_record_of(report).details == {"status_id": 5, "number": None, "display_number": "INV-1042"}
    # the cleanup with the flag has the raw DELETE backstop for exactly this case
    again = await cleanup_with_the_flag(fake, report, make_settings, lines)
    assert again.ok and invoice["Status"]["Id"] == 4
    assert invoice_record_of(report).outcome == "voided by the cleanup with raw DELETE (still listed as Void)"
    assert again.voided == ["INV-1042"] and f"reminder: approved invoice INV-1042: {REMINDER}" in lines  # in Gorelo only


# the void


async def test_approved_invoice_a_lookup_by_number_that_lags_is_asked_again_before_anything_is_voided(approved):
    fake = FakeGorelo()
    lagging_number_lookups(fake, 1)
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok, found.detail
    ops = area_ops(fake, report)["approved_invoice"]
    assert ops == [*GOLDEN_APPROVED[:6], "GET /v1/invoices", *GOLDEN_APPROVED[6:]] and ops.count("DELETE /v1/invoices/{invoiceId}") == 1
    assert "delete_invoice found the invoice by its Number on attempt 2" in found.notes


async def test_approved_invoice_a_lookup_that_never_finds_it_fails_the_area_without_a_delete_and_the_cleanup_does_not_void(approved):
    fake = FakeGorelo()
    lagging_number_lookups(fake, 99)
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "no invoice has the number 1042" in found.detail
    assert len([r for r in fake.mock.requests if r.path == "/v1/invoices" and "Number" in r.query]) == 3
    assert not any(r.method == "DELETE" for r in fake.mock.requests)  # not even the cleanup's raw backstop: it never voids
    assert the_invoice(fake)["Status"]["Id"] == 5 and report.summary["leftovers"] == 1 and not report.ok


async def test_approved_invoice_a_void_answer_that_is_not_status_4_fails_the_area(approved):
    fake = FakeGorelo()
    original = fake.handlers["DELETE /v1/invoices/{invoiceId}"]

    def says_deleted(request):
        original(request)  # Gorelo did void it ...
        return envelope({"Id": fake.part(request, 3), "StatusId": 6})  # ... but the answer says Deleted

    fake.handlers["DELETE /v1/invoices/{invoiceId}"] = says_deleted
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "delete_invoice StatusId (Void): expected 4, got 6" in found.detail
    ops = area_ops(fake, report)
    assert ops["approved_invoice"] == GOLDEN_APPROVED[:8]  # no get_invoice and no listing after a wrong answer
    # the matrix recorded no outcome; the cleanup read the invoice, found it void and noted the residue
    assert ops["cleanup"] == [READ]
    record = invoice_record_of(report)
    assert record.outcome == "already void, a known residue: a voided invoice cannot be removed (still listed as Void)"
    assert report.cleanup.ok and not report.ok and report.summary["leftovers"] == 0 and report.summary["undeletable"] == 1


async def test_approved_invoice_a_wrong_void_answer_for_an_invoice_that_is_still_approved_leaves_it_for_the_user(approved, lines):
    fake = FakeGorelo()
    fake.handlers["DELETE /v1/invoices/{invoiceId}"] = lambda request: envelope({"Id": fake.part(request, 3), "StatusId": 6})
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "delete_invoice StatusId (Void): expected 4, got 6" in found.detail
    assert the_invoice(fake)["Status"]["Id"] == 5 and report.summary["leftovers"] == 1 and not report.cleanup.ok
    assert any(line.startswith("  LEFTOVER   invoice ") and "--void-approved" in line for line in lines)
    assert invoice_record_of(report).details["status_id"] == 5 and invoice_record_of(report).outcome.startswith("left for the user")


async def test_approved_invoice_a_void_that_answers_another_invoices_id_fails_the_area(approved):
    fake = FakeGorelo()
    original = fake.handlers["DELETE /v1/invoices/{invoiceId}"]

    def answers_another(request):
        original(request)
        return envelope({"Id": invoice_guid(777), "StatusId": 4})

    fake.handlers["DELETE /v1/invoices/{invoiceId}"] = answers_another
    report = await approved(fake)
    assert "delete_invoice answered with another invoice's Id" in results(report)["approved_invoice"].detail


async def test_approved_invoice_that_still_reads_as_approved_after_the_void_is_not_recorded_as_voided(approved):
    fake = FakeGorelo()
    fake.handlers["DELETE /v1/invoices/{invoiceId}"] = lambda request: envelope({"Id": fake.part(request, 3), "StatusId": 4})
    report = await approved(fake)  # the answer says Void, the invoice stays Approved
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "get_invoice Status.Id after the void: expected 4, got 5" in found.detail
    assert area_ops(fake, report)["approved_invoice"] == GOLDEN_APPROVED[:9]
    record = invoice_record_of(report)
    assert record.details["status_id"] == 5 and record.outcome.startswith("left for the user: invoice INV-1042 has status Approved")
    assert report.summary["leftovers"] == 1 and report.summary["undeletable"] == 0


async def test_approved_invoice_a_void_gorelo_refuses_fails_the_area_and_is_not_asked_again(approved):
    fake = FakeGorelo()
    fake.fail("DELETE /v1/invoices/{invoiceId}", error_envelope(409, [("070901", "The invoice is locked.")]))
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "delete_invoice" in found.detail and "The invoice is locked." in found.detail
    assert area_ops(fake, report)["approved_invoice"].count("DELETE /v1/invoices/{invoiceId}") == 1
    assert the_invoice(fake)["Status"]["Id"] == 5 and report.summary["leftovers"] == 1


async def test_approved_invoice_a_listing_that_lags_after_the_void_is_asked_again(approved):
    fake = FakeGorelo()
    lagging_label_searches(fake, 1)
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok, found.detail
    assert area_ops(fake, report)["approved_invoice"] == [*GOLDEN_APPROVED, "GET /v1/invoices"]
    assert "list_invoices listed the voided invoice on attempt 2" in found.notes


async def test_approved_invoice_a_list_that_never_shows_the_voided_invoice_fails_the_area_but_the_void_stays_recorded(approved):
    fake = FakeGorelo()
    lagging_label_searches(fake, 99)
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "list_invoices did not list the voided invoice (a void leaves it listed, as Void)" in found.detail
    record = invoice_record_of(report)
    assert record.outcome == VOIDED_OUTCOME and record.details["status_id"] == 4  # the void is a fact whatever the search says
    assert fake.leftovers() == [] and area_ops(fake, report)["cleanup"] == [] and not report.ok


# what is recorded, announced and sent when the create itself goes wrong


async def test_approved_invoice_a_failed_read_back_still_records_the_invoice_as_approved_never_as_a_draft(approved, lines):
    fake = FakeGorelo()
    fake.fail(READ, error_envelope(500, [("070500", "boom")]))
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "the write succeeded but reading it back failed" in found.detail
    ops = area_ops(fake, report)
    assert ops["approved_invoice"] == GOLDEN_APPROVED[:5]
    # the answer was only {Id, warning}: no number to record, and the status is 5, never the 1 of a Draft
    assert invoice_record_of(report).details == {"status_id": 5, "number": None, "display_number": None}
    assert ops["cleanup"] == [READ]  # the cleanup reads it: Approved, so a leftover that needs the user
    assert the_invoice(fake)["Status"]["Id"] == 5 and report.summary["leftovers"] == 1 and report.summary["unresolved_intents"] == 0
    assert invoice_record_of(report).outcome.startswith("left for the user: invoice INV-1042 has status Approved (id 5)")
    assert not report.ok and lines[-1] == "result: FAILED"


async def test_approved_invoice_a_create_whose_answer_was_lost_is_found_by_the_run_label_and_left_for_the_user(approved, lines, make_settings):
    fake = FakeGorelo()
    original = fake.handlers["POST /v1/invoices"]

    def lost_answer(request):
        original(request)  # the invoice is made ...
        return error_envelope(500, [("070500", "boom")])  # ... but the answer never arrives

    fake.handlers["POST /v1/invoices"] = lost_answer
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "create_approved_invoice" in found.detail and found.requests == 4
    ops = area_ops(fake, report)
    assert ops["cleanup"] == ["GET /v1/invoices", READ]  # the search by the run label, then the invoice it found is read
    search = fake.mock.requests[4]
    assert search.query["ClientIds"] == "9501" and search.query["Query"] == APPROVED_LABEL
    record = invoice_record_of(report)
    assert record.label == APPROVED_LABEL and record.details == {"status_id": 5, "number": 1042, "display_number": "INV-1042"}
    assert record.outcome.startswith("left for the user: invoice INV-1042 has status Approved (id 5)")
    assert report.summary["unresolved_intents"] == 0 and report.summary["leftovers"] == 1 and not report.ok
    assert not any(r.method == "DELETE" for r in fake.mock.requests)  # never voided without the flag
    again = await cleanup_with_the_flag(fake, report, make_settings, lines)
    assert again.ok and the_invoice(fake)["Status"]["Id"] == 4


async def test_approved_invoice_a_create_gorelo_refuses_settles_its_intent_and_nothing_else_is_sent(approved):
    fake = FakeGorelo()
    fake.fail("POST /v1/invoices", error_envelope(400, [("070101", "Reference is too long", "Reference")]))
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "create_approved_invoice" in found.detail and "reference: Reference is too long" in found.detail
    assert report.summary["unresolved_intents"] == 0 and fake.invoices == {} and area_ops(fake, report)["cleanup"] == []
    assert area_ops(fake, report)["approved_invoice"] == GOLDEN_APPROVED[:4]


async def test_approved_invoice_a_create_that_made_nothing_but_was_not_confirmed_stays_reported(approved, lines):
    fake = FakeGorelo()
    fake.fail("POST /v1/invoices", error_envelope(500, [("070500", "boom")]))
    report = await approved(fake)
    assert results(report)["approved_invoice"].status == "FAIL"
    assert area_ops(fake, report)["cleanup"] == ["GET /v1/invoices"]  # searched by label: nothing found
    assert report.summary["unresolved_intents"] == 1 and not report.cleanup.ok
    assert any(line.startswith("  UNRESOLVED invoice ") for line in lines)


# a 429 on the one create: never retried, so the guard is never asked for a second Approved invoice. The client's
# 429 retries are off for that one call and for nothing else: the polls, the lookup by Number and the DELETE that
# voids keep them


async def test_approved_invoice_a_429_on_the_create_is_not_retried_and_settles_the_intent_truthfully(approved, lines):
    fake = FakeGorelo()
    fake.fail("POST /v1/invoices", TOO_MANY, times=5)  # more than the client's three retries would ever use
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    rate_limited = (
        "Gorelo is rate limiting requests (HTTP 429) for create_approved_invoice; it did not process this request. Retry later."
    )
    assert found.status == "FAIL" and found.detail == rate_limited
    # ONE POST was sent and answered: the client retried nothing, so the guard never saw a second Approved invoice
    assert [(r.method, r.path) for r in fake.mock.requests] == [
        ("GET", "/v1/contacts"), ("GET", "/v1/clients/9501/locations"), ("GET", "/v1/items"), ("POST", "/v1/invoices"),
    ]
    assert fake.invoices == {} and report.refused == 0 and [r.tripped for r in report.results] == [False]
    # a plain rejected create: its intent is settled as failed with what Gorelo said, not as "nothing was sent"
    (intent,) = json.loads(Path(report.manifest_path).read_text(encoding="utf-8"))["intents"]
    assert intent["status"] == "failed" and intent["reason"] == rate_limited and "nothing was sent" not in intent["reason"]
    assert report.summary == {"created": 0, "cleaned": 0, "leftovers": 0, "undeletable": 0, "unresolved_intents": 0}
    assert area_ops(fake, report)["cleanup"] == [] and report.cleanup.ok and not report.ok  # nothing to clean; the area failed
    assert not any("the guard refused" in line for line in lines) and lines[-1] == "result: FAILED"


async def test_approved_invoice_without_the_switch_a_429_would_be_retried_into_the_guard_which_is_why_it_exists(approved, monkeypatch):
    # the premise of the switch: GoreloClient retries a 429 for every method, the retry passes the guard again, and the guard
    # has already allowed its one create. The run then stops as a guard violation and the intent says "nothing was sent".
    monkeypatch.setattr(write_matrix, "no_429_retries", lambda client: contextlib.nullcontext())
    fake = FakeGorelo()
    fake.fail("POST /v1/invoices", TOO_MANY, times=5)
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and found.detail.startswith("the guard refused a request, the run stops: ")
    assert "this run already created an Approved invoice" in found.detail
    assert report.refused == 1 and len([r for r in fake.mock.requests if r.method == "POST"]) == 1
    (intent,) = json.loads(Path(report.manifest_path).read_text(encoding="utf-8"))["intents"]
    assert intent["reason"] == "the guard refused the request; nothing was sent"  # the misleading text the switch avoids


CLIENT_RETRIES = 3  # what the GoreloClient does with a 429: it asks again 3 times before it gives up
GIVES_UP = 1 + CLIENT_RETRIES  # requests of one call that is answered 429 every time
INVOICE_LIST, INVOICE_DELETE = "GET /v1/invoices", "DELETE /v1/invoices/{invoiceId}"
RATE_LIMITED_TEXT = "Gorelo is rate limiting requests (HTTP 429) for delete_invoice; it did not process this request. Retry later."


def watch_retries(fake: FakeGorelo, monkeypatch):
    """Spy on the 429 retries: (the GoreloClients built so far, in order; one (method, path, max_429_retries) for every request
    that reached the fake, with the setting that the sending client had at that moment).

    A run builds the matrix's server first (its client is the first one) and the cleanup's own after it, and the two never send
    at the same time, so a request belongs to the newest client."""
    clients: list[GoreloClient] = []
    real_init = GoreloClient.__init__

    def spy(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        clients.append(self)

    monkeypatch.setattr(GoreloClient, "__init__", spy)
    seen: list[tuple[str, str, int]] = []
    served = fake.mock.transport.handler  # what the fake's own MockTransport calls

    def handle(request):
        seen.append((request.method, request.url.path, clients[-1].max_429_retries))
        return served(request)

    fake.mock.transport = httpx.MockTransport(handle)  # the run fixture reads it when the run starts
    return clients, seen


async def test_approved_invoice_the_429_retries_are_off_for_the_create_call_alone(approved, monkeypatch):
    fake = FakeGorelo()
    clients, seen = watch_retries(fake, monkeypatch)
    report = await approved(fake)
    assert report.ok and area_ops(fake, report)["approved_invoice"] == GOLDEN_APPROVED
    # The create's POST and its read-back GET (one tool call) were sent with no retries. The preconditions before it, and the
    # first poll, the lookup by Number, the DELETE that voids, the read after it and the listing after the call, had the
    # client's normal three. This is also the proof that the client the matrix switches is the one the tools send with.
    assert [(method, retries) for method, path, retries in seen] == [
        ("GET", 3), ("GET", 3), ("GET", 3), ("POST", 0), ("GET", 0), ("GET", 3), ("GET", 3), ("DELETE", 3), ("GET", 3), ("GET", 3),
    ]
    assert [client.max_429_retries for client in clients] == [3, 3]  # the matrix's client and the cleanup's own: both normal


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(error_envelope(400, [("070101", "Reference is too long", "Reference")]), id="refused"),
        pytest.param(TOO_MANY, id="429"),
        pytest.param(httpx.ReadTimeout("slow"), id="timeout"),
    ],
)
async def test_approved_invoice_the_429_retries_are_back_right_after_a_create_that_failed(approved, monkeypatch, answer):
    fake = FakeGorelo()
    clients, seen = watch_retries(fake, monkeypatch)
    fake.fail("POST /v1/invoices", answer, times=5)
    report = await approved(fake)
    assert results(report)["approved_invoice"].status == "FAIL"
    assert [retries for method, path, retries in seen if method == "POST"] == [0]  # ONE POST, sent with no retry
    assert clients[0].max_429_retries == 3  # the client's normal setting is back, although the create raised


async def test_approved_invoice_the_429_retries_are_back_even_when_the_create_call_is_interrupted(approved, monkeypatch):
    fake = FakeGorelo()
    clients, seen = watch_retries(fake, monkeypatch)

    class Abort(BaseException):
        pass

    real = write_matrix.Matrix.tool

    async def tool(self, name, /, **arguments):
        if name == "create_approved_invoice":
            assert clients[0].max_429_retries == 0  # the window is open while the create call runs
            raise Abort("stop")
        return await real(self, name, **arguments)

    monkeypatch.setattr(write_matrix.Matrix, "tool", tool)
    with pytest.raises(Abort):
        await approved(fake)
    assert clients[0].max_429_retries == 3


def test_no_429_retries_is_a_window_that_puts_back_the_setting_it_found():
    client = GoreloClient("key-for-the-test", transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    assert client.max_429_retries == 3  # the default of GoreloClient
    with write_matrix.no_429_retries(client):
        assert client.max_429_retries == 0
    assert client.max_429_retries == 3

    class Abort(BaseException):
        pass

    for error in (KeyError("x"), Abort("stop")):  # also when the block raises, whatever it raises
        with pytest.raises(type(error)):
            with write_matrix.no_429_retries(client):
                assert client.max_429_retries == 0
                raise error
        assert client.max_429_retries == 3
    client.max_429_retries = 7  # what was there is what comes back, not a constant
    with write_matrix.no_429_retries(client):
        assert client.max_429_retries == 0
        with write_matrix.no_429_retries(client):
            assert client.max_429_retries == 0
        assert client.max_429_retries == 0  # the inner window found 0 and puts back 0
    assert client.max_429_retries == 7


def test_gorelo_client_behind_finds_the_client_the_tools_use_and_refuses_what_is_not_one():
    client = GoreloClient("key-for-the-test", transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    assert write_matrix.gorelo_client_behind(SimpleNamespace(_lifespan_result={"gorelo": client, "toolsets": []})) is client
    assert client.max_429_retries == 3  # finding it changes nothing
    for server in (
        SimpleNamespace(),  # not started: no lifespan state
        SimpleNamespace(_lifespan_result=None),
        SimpleNamespace(_lifespan_result={}),
        SimpleNamespace(_lifespan_result={"gorelo": object()}),
        SimpleNamespace(_lifespan_result=["gorelo"]),
    ):
        with pytest.raises(write_matrix.SetupError, match="cannot switch off the 429 retries of the Gorelo client"):
            write_matrix.gorelo_client_behind(server)


def test_a_matrix_for_the_approved_run_must_hold_the_client_whose_retries_it_switches_off():
    def matrix(**options):
        return write_matrix.Matrix(
            manifest=None, guard=None, pacer=None, session=None, secret="s", clock=lambda: RUN_START, lookup_wait=0,
            scopes_missing=set(), **options,
        )

    with pytest.raises(write_matrix.SetupError, match="the approved invoice must never be sent twice, so the run did not start"):
        matrix(with_approved_invoice=True)
    plain = matrix()  # every other run holds none, and cannot open the window
    assert plain.gorelo is None
    with pytest.raises(write_matrix.SetupError, match="holds no Gorelo client whose 429 retries could be switched off"):
        plain.without_429_retries()
    client = GoreloClient("key-for-the-test", transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    with matrix(with_approved_invoice=True, gorelo=client).without_429_retries():
        assert client.max_429_retries == 0
    assert client.max_429_retries == 3


async def test_approved_invoice_the_run_does_not_start_when_the_client_cannot_be_reached(run, monkeypatch):
    class NotTheClient:  # the module's idea of a GoreloClient: no lifespan state holds one, so the switch cannot be made
        pass

    monkeypatch.setattr(write_matrix, "GoreloClient", NotTheClient)
    fake = FakeGorelo()
    with pytest.raises(write_matrix.SetupError, match="the approved invoice must never be sent twice, so the run did not start"):
        await run(fake, areas=["approved_invoice"], with_approved_invoice=True)
    assert fake.mock.requests == []  # not even a precondition read: nothing was sent


async def test_the_other_runs_never_touch_the_clients_retries(run, monkeypatch):
    monkeypatch.setattr(write_matrix, "no_429_retries", lambda client: pytest.fail("only the approved run switches retries off"))
    monkeypatch.setattr(write_matrix, "gorelo_client_behind", lambda server: pytest.fail("only the approved run holds the client"))
    fake = FakeGorelo()
    clients, seen = watch_retries(fake, monkeypatch)
    report = await run(fake, areas=["clients"])
    assert report.ok
    assert seen and {retries for method, path, retries in seen} == {3}  # every request of the run, the cleanup's included


# 429 on the reads of the wait, the lookup by Number and the DELETE that voids: the client asks again, and when it has
# given up the area asks again (delete_invoice only), because Gorelo did not process the request


async def test_approved_invoice_one_429_on_a_poll_read_is_retried_by_the_client_and_the_area_never_hears_of_it(approved):
    fake = FakeGorelo()
    flaky_reads(fake, {2}, TOO_MANY)  # the first poll is answered 429 once: the client's own retry (the next read) gets the answer
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok, found.detail
    assert area_ops(fake, report)["approved_invoice"] == [*GOLDEN_APPROVED[:5], READ, *GOLDEN_APPROVED[5:]]  # the retry is one read
    assert not any(note.startswith("get_invoice failed") for note in found.notes)  # no poll failed: nothing to note


async def test_approved_invoice_a_429_that_outlasts_the_clients_retries_on_a_poll_read_is_one_failed_poll(approved):
    fake = FakeGorelo()
    flaky_reads(fake, set(range(2, 2 + GIVES_UP)), TOO_MANY)  # the whole first poll: 1 + 3 requests, every one of them 429
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok, found.detail
    assert area_ops(fake, report)["approved_invoice"] == [*GOLDEN_APPROVED[:5], *[READ] * GIVES_UP, *GOLDEN_APPROVED[5:]]
    assert any(note.startswith(FAILED_READ + "Gorelo is rate limiting requests (HTTP 429) for get_invoice") for note in found.notes)


def throttled_number_lookups(fake: FakeGorelo, times: int) -> None:
    """The first `times` lookups of an invoice by its Number (what delete_invoice does) are answered 429, one request each."""
    original = fake.handlers[INVOICE_LIST]
    left = [times]

    def serve(request):
        if request.query.get("Number") and left[0] > 0:
            left[0] -= 1
            return TOO_MANY
        return original(request)

    fake.handlers[INVOICE_LIST] = serve


@pytest.mark.parametrize("where", ["lookup", "delete"])
async def test_approved_invoice_one_429_on_the_lookup_or_the_delete_is_retried_by_the_client_itself(approved, where):
    # the client's retries are back after the create, so the void is protected by them again (they were off run-wide)
    fake = FakeGorelo()
    if where == "lookup":
        throttled_number_lookups(fake, 1)
        at, extra = 6, INVOICE_LIST
    else:
        fake.fail(INVOICE_DELETE, TOO_MANY)
        at, extra = 7, INVOICE_DELETE
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok, found.detail
    assert area_ops(fake, report)["approved_invoice"] == [*GOLDEN_APPROVED[:at], extra, *GOLDEN_APPROVED[at:]]
    assert not any("rate limited" in note for note in found.notes)  # the area never saw a failure: the client asked again
    assert the_invoice(fake)["Status"] == {"Id": 4, "Name": "Void"} and fake.residue() and report.refused == 0


async def test_approved_invoice_a_429_on_the_lookup_that_outlasts_the_clients_retries_is_asked_again_by_the_area(approved):
    fake = FakeGorelo()
    throttled_number_lookups(fake, GIVES_UP)  # one whole attempt of delete_invoice: its lookup is answered 429 four times
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok, found.detail
    ops = area_ops(fake, report)["approved_invoice"]
    assert ops == [*GOLDEN_APPROVED[:6], *[INVOICE_LIST] * GIVES_UP, *GOLDEN_APPROVED[6:]]  # then the second attempt: lookup, DELETE
    assert ops.count(INVOICE_DELETE) == 1 and area_ops(fake, report)["cleanup"] == []
    assert "delete_invoice was rate limited by Gorelo (HTTP 429) 1 time and went through on attempt 2" in found.notes
    assert not any("found the invoice by its Number" in note for note in found.notes)  # the lookup did not lag: it was throttled
    assert the_invoice(fake)["Status"] == {"Id": 4, "Name": "Void"} and fake.residue() and report.refused == 0
    assert invoice_record_of(report).outcome == VOIDED_OUTCOME and found.notes.count(write_matrix.VOID_REMINDER) == 1


async def test_approved_invoice_a_429_on_the_delete_that_outlasts_the_clients_retries_is_asked_again_by_the_area(approved):
    fake = FakeGorelo()
    fake.fail(INVOICE_DELETE, TOO_MANY, times=GIVES_UP)  # one whole attempt of delete_invoice: its DELETE is answered 429 four times
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok, found.detail
    ops = area_ops(fake, report)["approved_invoice"]
    # the first attempt: the lookup, then the DELETE four times (Gorelo processed none of them); the second: lookup, DELETE
    assert ops == [*GOLDEN_APPROVED[:7], *[INVOICE_DELETE] * GIVES_UP, INVOICE_LIST, *GOLDEN_APPROVED[7:]]
    assert "delete_invoice was rate limited by Gorelo (HTTP 429) 1 time and went through on attempt 2" in found.notes
    invoice = the_invoice(fake)
    assert invoice["Status"] == {"Id": 4, "Name": "Void"} and fake.residue()
    assert report.refused == 0  # the guard lets the second DELETE of the invoice through: it has no once-only rule for it
    assert invoice_record_of(report).outcome == VOIDED_OUTCOME and area_ops(fake, report)["cleanup"] == []


async def test_approved_invoice_a_lagging_lookup_and_a_429_share_the_three_attempts(approved):
    fake = FakeGorelo()
    lagging_number_lookups(fake, 1)
    throttled_number_lookups(fake, GIVES_UP)  # wraps the lagging one: attempt 1 is 429 four times, 2 finds nothing, 3 goes through
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok, found.detail
    assert area_ops(fake, report)["approved_invoice"] == [
        *GOLDEN_APPROVED[:6], *[INVOICE_LIST] * GIVES_UP, INVOICE_LIST, *GOLDEN_APPROVED[6:]
    ]
    assert "delete_invoice found the invoice by its Number on attempt 3" in found.notes
    assert "delete_invoice was rate limited by Gorelo (HTTP 429) 1 time and went through on attempt 3" in found.notes


async def test_approved_invoice_when_a_lagging_lookup_has_used_two_attempts_a_429_after_it_ends_the_area(approved):
    fake = FakeGorelo()
    lagging_number_lookups(fake, 2)  # attempts 1 and 2 find nothing ...
    original = fake.handlers[INVOICE_LIST]
    seen = []

    def throttle_the_third(request):
        if request.query.get("Number"):
            seen.append(request.path)
            if len(seen) > 2:  # ... and every request of the third attempt is 429
                return TOO_MANY
        return original(request)

    fake.handlers[INVOICE_LIST] = throttle_the_third
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and found.detail == RATE_LIMITED_TEXT  # out of attempts: the last failure is the answer
    assert not any(r.method == "DELETE" for r in fake.mock.requests) and the_invoice(fake)["Status"]["Id"] == 5
    assert report.summary["leftovers"] == 1 and not report.ok


async def test_approved_invoice_a_429_that_never_stops_on_the_lookup_is_given_up_after_three_attempts_and_voids_nothing(approved, lines):
    fake = FakeGorelo()
    throttled_number_lookups(fake, 99)
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and found.detail == RATE_LIMITED_TEXT
    ops = area_ops(fake, report)
    assert ops["approved_invoice"] == [*GOLDEN_APPROVED[:6], *[INVOICE_LIST] * (3 * GIVES_UP)]  # 3 attempts, no DELETE
    assert ops["cleanup"] == [READ]  # the cleanup reads it and leaves it: it never voids without --void-approved
    assert not any(r.method == "DELETE" for r in fake.mock.requests)
    assert the_invoice(fake)["Status"]["Id"] == 5 and report.summary["leftovers"] == 1 and not report.ok
    # nothing was voided, so no reminder about a void: the user is told how to void it instead
    assert write_matrix.VOID_REMINDER not in found.notes and report.voided == []
    assert any(line.startswith("an Approved invoice is left over: check the accounting system, then void it with: ") for line in lines)


async def test_approved_invoice_a_429_that_never_stops_on_the_delete_is_given_up_after_three_attempts_and_voids_nothing(approved, lines):
    fake = FakeGorelo()
    fake.fail(INVOICE_DELETE, TOO_MANY, times=99)
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and found.detail == RATE_LIMITED_TEXT
    ops = area_ops(fake, report)
    assert ops["approved_invoice"] == [*GOLDEN_APPROVED[:6], *([INVOICE_LIST, *[INVOICE_DELETE] * GIVES_UP] * 3)]
    assert ops["cleanup"] == [READ]
    assert the_invoice(fake)["Status"]["Id"] == 5 and report.summary["leftovers"] == 1 and not report.ok
    assert write_matrix.VOID_REMINDER not in found.notes and report.voided == []
    assert any(line.startswith("an Approved invoice is left over: check the accounting system, then void it with: ") for line in lines)


async def test_a_429_on_the_delete_of_a_draft_is_asked_again_by_the_invoices_area_too(run):
    # remove_invoice is shared with the invoices area: a Draft's delete is asked again the same way
    fake = FakeGorelo()
    fake.fail(INVOICE_DELETE, TOO_MANY, times=GIVES_UP)
    report = await run(fake, areas=["invoices"], with_invoices=True)
    found = results(report)["invoices"]
    assert found.status == "pass" and report.ok, found.detail
    expected = GOLDEN["invoices"]  # the lookup is index 7, the DELETE 8
    assert area_ops(fake, report)["invoices"] == [*expected[:8], *[INVOICE_DELETE] * GIVES_UP, INVOICE_LIST, *expected[8:]]
    assert "delete_invoice was rate limited by Gorelo (HTTP 429) 1 time and went through on attempt 2" in found.notes
    assert fake.leftovers() == [] and report.refused == 0


async def test_approved_invoice_a_429_on_the_creates_read_back_is_not_retried_and_leaves_the_invoice_for_the_user(
    approved, lines, monkeypatch
):
    fake = FakeGorelo()
    clients, seen = watch_retries(fake, monkeypatch)
    fake.fail(READ, TOO_MANY)  # the first read of the invoice is the create's read-back: answered 429 once
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL"
    assert "the write succeeded but reading it back failed" in found.detail and "was rate limited by Gorelo (HTTP 429)" in found.detail
    ops = area_ops(fake, report)
    assert ops["approved_invoice"] == GOLDEN_APPROVED[:5]  # the POST and ONE read-back: not asked again, nothing polled or voided
    assert [(method, retries) for method, path, retries in seen[3:5]] == [("POST", 0), ("GET", 0)]  # no retry: it is part of the call
    assert clients[0].max_429_retries == 3  # and the window is closed again
    assert not any(r.method == "DELETE" for r in fake.mock.requests)
    # recorded as Approved (never as a Draft) and left for the user, with the command that voids it
    assert invoice_record_of(report).details == {"status_id": 5, "number": None, "display_number": None}
    assert ops["cleanup"] == [READ]
    assert the_invoice(fake)["Status"]["Id"] == 5 and report.summary["leftovers"] == 1 and report.summary["unresolved_intents"] == 0
    assert any(line.startswith("an Approved invoice is left over: check the accounting system, then void it with: ") for line in lines)
    assert write_matrix.VOID_REMINDER not in found.notes and report.voided == []
    assert not report.ok and lines[-1] == "result: FAILED"


def test_the_harness_reads_the_clients_own_words_for_a_429():
    limited = format_gorelo_error(
        GoreloAPIError("gave up", status=429, op_key="DELETE /v1/invoices/{invoiceId}", kind="rate_limit"), "delete_invoice"
    )
    assert limited == RATE_LIMITED_TEXT  # the text the tests above serve comes from tools/_common.py, not an invention
    assert write_matrix.RATE_LIMITED in limited and write_matrix.NOT_PROCESSED in limited
    assert write_matrix.rate_limited(write_matrix.ToolFailed("delete_invoice", limited, sent=True))
    others = [
        format_gorelo_error(
            GoreloAPIError(
                "locked", status=409, op_key="DELETE /v1/invoices/{invoiceId}", kind="http",
                notifications=[{"Code": "070901", "Message": "The invoice is locked."}], trace_id="00-abc-def-01",
            ),
            "delete_invoice",
        ),
        format_gorelo_error(
            GoreloAPIError("t", op_key="DELETE /v1/invoices/{invoiceId}", kind="timeout", write_unconfirmed=True), "delete_invoice"
        ),
        "invoice_number: no invoice has the number 1042 (deleted invoices are not listed); nothing was deleted or voided.",
        "Gorelo rejected delete_invoice (HTTP 429, code 070101): something else that says 429",  # 429, but not "did not process"
        # the words of a 429 without the promise that makes asking again safe: never read as one
        "Gorelo is rate limiting requests (HTTP 429) for delete_invoice; it may have processed this request.",
        "it did not process this request",  # not 429
        "",
    ]
    for text in others:
        assert not write_matrix.rate_limited(write_matrix.ToolFailed("delete_invoice", text, sent=True)), text


# who was emailed, and the payment link


async def test_approved_invoice_that_reads_as_emailed_is_noted_loudly_once_and_still_voided(approved, lines):
    fake = FakeGorelo()
    fake.on_invoice_created(lambda invoice: invoice.update(IsEmailSent=True, EmailSentOn="2026-10-02T10:20:00Z"))
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok and fake.residue()  # a note, not a failure: it is voided all the same
    warnings = [note for note in found.notes if note.startswith("WARNING: invoice EMAILED")]
    assert len(warnings) == 1  # once, whatever the number of reads
    assert "IsEmailSent True, EmailSentOn '2026-10-02T10:20:00Z'" in warnings[0] and "although no recipient was named" in warnings[0]
    assert "seen when it was created; invoice INV-1042" in warnings[0]
    assert any("WARNING: invoice EMAILED" in line for line in lines)  # it reaches the printed summary


async def test_approved_invoice_that_is_emailed_after_the_create_is_noted_when_it_is_seen(approved):
    fake = FakeGorelo()
    original = fake.handlers["GET /v1/invoices/{invoiceId}"]
    reads = []

    def emailed_later(request):
        reads.append(request.path)
        if len(reads) == 2:  # the first read after the create's read-back
            fake.invoices[request.path.rsplit("/", 1)[1]].update(IsEmailSent=True, EmailSentOn="2026-10-02T10:22:00Z")
        return original(request)

    fake.handlers["GET /v1/invoices/{invoiceId}"] = emailed_later
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass"
    warnings = [note for note in found.notes if "EMAILED" in note]
    assert len(warnings) == 1 and "seen at read 1 while waiting for the accounting system" in warnings[0]


@pytest.mark.parametrize(
    "damage, said",
    [
        pytest.param(lambda inv: inv.pop("IsEmailSent"), "(IsEmailSent is missing, EmailSentOn is null)", id="no-flag"),
        pytest.param(lambda inv: inv.update(IsEmailSent=None), "(IsEmailSent is null, EmailSentOn is null)", id="null-flag"),
        pytest.param(lambda inv: inv.pop("EmailSentOn"), "(IsEmailSent is bool, EmailSentOn is missing)", id="no-date"),
        pytest.param(lambda inv: inv.update(IsEmailSent="false"), "(IsEmailSent is str, EmailSentOn is null)", id="text-flag"),
    ],
)
async def test_approved_invoice_that_cannot_say_whether_it_was_emailed_is_noted_too(approved, damage, said):
    fake = FakeGorelo()
    fake.on_invoice_created(damage)
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass"
    assert any(note.startswith("WARNING: whether the invoice was emailed cannot be read ") and said in note for note in found.notes)


async def test_approved_invoice_the_payment_link_is_only_said_to_be_set_or_not_never_printed(approved, lines):
    fake = FakeGorelo(payment_link=False)
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and any(note.endswith("PaymentLink not set") for note in found.notes)
    other = FakeGorelo()
    report = await approved(other, started=datetime(2099, 10, 2, 10, 16, 0, tzinfo=timezone.utc))
    notes = results(report)["approved_invoice"].notes
    assert any(note.endswith("PaymentLink set") for note in notes)
    assert not any("in.xero.com" in line for line in lines) and not any("in.xero.com" in note for note in notes)


# the no-tax check on the read after the void: a loud note, never a failure, and never in front of the void


async def test_approved_invoice_the_read_after_the_void_confirms_no_tax_and_one_dollar_with_a_note_and_no_extra_request(approved):
    fake = FakeGorelo()
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok
    assert "no-tax check passed: TotalTax is 0 and Total is 1.0 on the read after the void" in found.notes
    assert not any("no-tax check FAILED" in note for note in found.notes)
    assert area_ops(fake, report)["approved_invoice"] == GOLDEN_APPROVED  # it reads the get_invoice that is already there


@pytest.mark.parametrize(
    "damage, said",
    [
        pytest.param(lambda inv: inv.update(TotalTax=0.08, Total=1.08), "TotalTax is 0.08 (expected 0) and Total is 1.08 (expected 1.0)", id="taxed"),
        pytest.param(lambda inv: inv.update(TotalTax=0.08), "TotalTax is 0.08 (expected 0) and Total is 1.0 (expected 1.0)", id="tax-only"),
        pytest.param(lambda inv: inv.update(Total=1.5), "TotalTax is 0.0 (expected 0) and Total is 1.5 (expected 1.0)", id="another-total"),
        pytest.param(lambda inv: inv.pop("TotalTax"), "TotalTax is missing (expected 0) and Total is 1.0", id="no-tax-field"),
        pytest.param(lambda inv: inv.update(TotalTax=None), "TotalTax is null (expected 0) and Total is 1.0", id="null-tax"),
        pytest.param(lambda inv: inv.update(TotalTax="0"), "TotalTax is str (expected 0) and Total is 1.0", id="text-tax"),
        pytest.param(lambda inv: inv.update(TotalTax=True), "TotalTax is bool (expected 0) and Total is 1.0", id="true-tax"),
    ],
)
async def test_approved_invoice_that_reads_with_tax_after_the_void_is_noted_loudly_and_the_void_is_untouched(approved, lines, damage, said):
    fake = FakeGorelo()
    fake.on_invoice_created(damage)  # the stored invoice, so every read of it says so
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok, found.detail  # a note, not a failure
    warnings = [note for note in found.notes if note.startswith("WARNING: no-tax check FAILED: ")]
    assert len(warnings) == 1 and said in warnings[0]
    assert (
        "so Gorelo may not have taken the explicit TaxId null as no tax; read after the void, invoice INV-1042; at the last "
        "read before the void it was TotalTax "
    ) in warnings[0]
    assert not any(note.startswith("no-tax check passed") for note in found.notes)
    assert any("no-tax check FAILED" in line for line in lines)  # it reaches the printed summary
    # the void was neither stopped nor skipped, and its bookkeeping is complete
    assert area_ops(fake, report) == {"approved_invoice": GOLDEN_APPROVED, "cleanup": []}
    record = invoice_record_of(report)
    assert record.outcome == VOIDED_OUTCOME and record.details["status_id"] == 4 and not record.cleaned
    assert the_invoice(fake)["Status"] == {"Id": 4, "Name": "Void"} and fake.leftovers() == [] and fake.residue()


async def test_approved_invoice_a_tax_that_shows_only_after_the_void_is_noted_with_the_amounts_read_before_it(approved):
    fake = FakeGorelo()
    original = fake.handlers[READ]

    def taxed_once_void(request):
        stored = fake.invoices[request.path.rsplit("/", 1)[1]]
        if stored["Status"]["Id"] == 4:  # only the read after the void sees a tax
            stored.update(TotalTax=0.08, Total=1.08)
        return original(request)

    fake.handlers[READ] = taxed_once_void
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok
    (warning,) = [note for note in found.notes if note.startswith("WARNING: no-tax check FAILED")]
    assert "TotalTax is 0.08 (expected 0) and Total is 1.08 (expected 1.0)" in warning
    assert warning.endswith("at the last read before the void it was TotalTax 0.0 and Total 1.0")


async def test_approved_invoice_the_no_tax_note_does_not_hide_a_void_that_did_not_happen(approved):
    # the check is made before the status after the void is judged, so a failing status check cannot swallow it either
    fake = FakeGorelo()
    fake.on_invoice_created(lambda inv: inv.update(TotalTax=0.08, Total=1.08))
    fake.handlers["DELETE /v1/invoices/{invoiceId}"] = lambda request: envelope({"Id": fake.part(request, 3), "StatusId": 4})
    report = await approved(fake)  # the answer says Void, the invoice stays Approved
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "get_invoice Status.Id after the void: expected 4, got 5" in found.detail
    assert any(note.startswith("WARNING: no-tax check FAILED") for note in found.notes)
    assert report.summary["leftovers"] == 1 and invoice_record_of(report).details["status_id"] == 5


def test_tax_problem_is_none_only_for_no_tax_and_exactly_one_dollar():
    problem = write_matrix.tax_problem
    for record in ({"TotalTax": 0, "Total": 1}, {"TotalTax": 0.0, "Total": 1.0}, {"TotalTax": -0.0, "Total": 1.0}):
        assert problem(record) is None
    for record in (
        {},
        {"TotalTax": 0},
        {"Total": 1.0},
        {"TotalTax": 0.01, "Total": 1.0},
        {"TotalTax": 0, "Total": 1.01},
        {"TotalTax": 0, "Total": 0.99},
        {"TotalTax": 0, "Total": 0},
        {"TotalTax": 0, "Total": True},  # True == 1, but it is no amount
        {"TotalTax": False, "Total": 1.0},
        {"TotalTax": "0", "Total": "1.0"},
        {"TotalTax": None, "Total": None},
        {"TotalTax": float("nan"), "Total": 1.0},
        {"TotalTax": 0, "Total": float("inf")},
        {"TotalTax": 0, "Total": 10**400},
    ):
        text = problem(record)
        assert text is not None and text.startswith("WARNING: no-tax check FAILED: TotalTax is "), record
        assert "(expected 0)" in text and "(expected 1.0)" in text and "TaxId null" in text
    assert write_matrix.amount_text(0.08) == "0.08" and write_matrix.amount_text(1) == "1"
    assert [write_matrix.amount_text(v) for v in (None, "1", True, [], float("nan"))] == ["null", "str", "bool", "list", "float"]
    assert write_matrix.amount_text(write_matrix._MISSING) == "missing"


# the void is in Gorelo only: the area says so right after the void, and the summary says it again (the invoice that the approved
# run pushed stayed open in Xero after Gorelo voided it)

REMINDER = (
    "the void is in Gorelo only: check the accounting system and void the invoice there too "
    "(Gorelo's void is not pushed to the connected accounting system, seen with Xero)"
)
SUMMARY_REMINDER = f"reminder: approved invoice INV-1042: {REMINDER}"


def test_the_void_reminder_is_the_agreed_sentence():
    assert write_matrix.VOID_REMINDER == REMINDER


def test_the_void_sentences_are_defined_once_in_the_cleanup_and_the_matrix_imports_them():
    # the cleanup says the same words for the invoices it voids itself, so the two cannot drift apart
    assert cleanup.VOID_REMINDER == REMINDER and cleanup.VOID_COMMAND_NOTE == GORELO_ONLY
    assert write_matrix.VOID_REMINDER is cleanup.VOID_REMINDER
    assert write_matrix.VOID_COMMAND_NOTE is cleanup.VOID_COMMAND_NOTE
    source = Path(write_matrix.__file__).read_text(encoding="utf-8")
    assert "VOID_REMINDER = " not in source and "VOID_COMMAND_NOTE = " not in source  # imported, never defined here


def test_the_summary_line_for_an_approved_invoice_left_over_ends_with_what_the_command_does():
    leftover = Record(
        seq=1, kind="invoice", id=invoice_guid(1), label=f"{RUN} approved invoice", details={"status_id": 5, "number": 1042},
        created_at="x", cleaned=False, outcome="left for the user: invoice INV-1042 has status Approved (id 5)", attempts=(),
    )
    paid = Record(
        seq=2, kind="invoice", id=invoice_guid(2), label=f"{RUN} invoice", details={"status_id": 3}, created_at="x",
        cleaned=False, outcome="left for the user: invoice INV-1043 has status Paid (id 3)", attempts=(),
    )
    command = f"python -m scripts.live.cleanup m.json --void-approved ({GORELO_ONLY})"
    hint = f"an Approved invoice is left over: check the accounting system, then void it with: {command}"

    def summary(*leftovers) -> list[str]:
        report = write_matrix.MatrixReport(
            run_id=RUN, manifest_path="m.json", cleanup=cleanup.CleanupReport(run_id=RUN, leftovers=list(leftovers))
        )
        return report.render()

    for leftovers, said in (([leftover], True), ([paid], False), ([leftover, paid], True)):
        lines = summary(*leftovers)
        assert "if anything is left over: python -m scripts.live.cleanup m.json" in lines  # the plain command, which never voids
        assert (hint in lines) is said and sum(line.startswith("an Approved invoice is left over") for line in lines) == int(said)
    assert not any(line.startswith("an Approved invoice") for line in summary())  # nothing is left over: nothing is said


@pytest.mark.parametrize("scenario", ["never synced", "no number"])
async def test_approved_invoice_the_hint_that_names_the_command_is_whole_even_for_a_long_manifest_path(
    tmp_path, make_settings, scenario
):
    # the area's detail is clipped at DETAIL_LIMIT: with the sentence about Gorelo only the hint outgrows the old 300, and a
    # clipped hint would lose the end of the sentence, or of the command that the user copies
    fake = FakeGorelo(sync_after=None) if scenario == "never synced" else FakeGorelo()
    if scenario == "no number":
        fake.on_invoice_created(lambda invoice: invoice.update(Number=None))
    runs = tmp_path / "runs"
    padding = max(0, 130 - len(str(runs / f"{RUN}.json")))  # a manifest path of 130 characters, however deep tmp_path is
    directory = runs / ("d" * padding) if padding else runs
    report = await write_matrix.run_matrix(
        settings=make_settings(destructive=True), transport=fake.mock.transport, directory=directory, echo=lambda line: None,
        pace=0, lookup_wait=0, started=RUN_START, sync_interval=0, sync_polls=3,
        areas=["approved_invoice"], with_approved_invoice=True,
    )
    found = results(report)["approved_invoice"]
    assert len(report.manifest_path) >= 130 and found.status == "FAIL"
    assert found.detail.endswith(f"python -m scripts.live.cleanup {report.manifest_path} --void-approved ({GORELO_ONLY})")
    assert 300 < len(found.detail) < write_matrix.DETAIL_LIMIT
    assert found.detail.startswith(NOT_YET_SYNCED if scenario == "never synced" else "approved invoice INV-1042 has no Number")


async def test_approved_invoice_a_void_is_said_to_be_in_gorelo_only_by_the_area_and_by_the_summary(approved, lines):
    fake = FakeGorelo()
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "pass" and report.ok
    assert found.notes.count(REMINDER) == 1  # the area's note: exactly that sentence, once
    assert report.voided == ["INV-1042"]
    # right after the void, before the reads that follow it: after the push was noted, before the no-tax check
    pushed = next(i for i, note in enumerate(found.notes) if "pushed to the accounting system" in note)
    taxed = found.notes.index("no-tax check passed: TotalTax is 0 and Total is 1.0 on the read after the void")
    assert pushed < found.notes.index(REMINDER) < taxed
    assert f"  approved_invoice: {REMINDER}" in lines  # it is in the printed summary as a note ...
    assert lines.count(SUMMARY_REMINDER) == 1 and lines[-2:] == [SUMMARY_REMINDER, "result: PASSED"]  # ... and as its last line


async def test_approved_invoice_the_void_note_is_there_even_when_the_read_after_the_void_fails(approved, lines):
    fake = FakeGorelo()
    flaky_reads(fake, {3: error_envelope(500, [("070500", "boom")])})  # reads: 1 the read-back, 2 the poll, 3 after the void
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "get_invoice" in found.detail
    assert REMINDER in found.notes  # Gorelo's DELETE answer said Void: that is said before anything else is read
    # the area never recorded the void, the cleanup found the invoice void and noted the residue: the summary says it all the same
    assert invoice_record_of(report).outcome == "already void, a known residue: a voided invoice cannot be removed (still listed as Void)"
    assert report.voided == ["INV-1042"] and lines[-2:] == [SUMMARY_REMINDER, "result: FAILED"]


async def test_approved_invoice_the_summary_reminds_about_a_void_the_area_never_saw_through(approved, lines):
    fake = FakeGorelo()
    original = fake.handlers[INVOICE_DELETE]

    def says_deleted(request):
        original(request)  # Gorelo did void it ...
        return envelope({"Id": fake.part(request, 3), "StatusId": 6})  # ... but the answer says Deleted

    fake.handlers[INVOICE_DELETE] = says_deleted
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "delete_invoice StatusId (Void): expected 4, got 6" in found.detail
    assert REMINDER not in found.notes  # the area was not told Void, so it said nothing about a void ...
    assert report.voided == ["INV-1042"] and lines[-2:] == [SUMMARY_REMINDER, "result: FAILED"]  # ... but the cleanup found it void


async def test_approved_invoice_the_void_is_said_to_be_in_gorelo_only_when_the_area_fails_after_it_too(approved, lines):
    fake = FakeGorelo()
    lagging_label_searches(fake, 99)  # the void happened; the listing never shows the invoice
    report = await approved(fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "list_invoices did not list the voided invoice" in found.detail
    assert REMINDER in found.notes and report.voided == ["INV-1042"] and SUMMARY_REMINDER in lines


async def test_no_reminder_about_a_void_when_nothing_was_voided(run, approved, lines):
    never = await approved(FakeGorelo(sync_after=None))  # never synced: left Approved, not voided
    assert report_has_no_void_reminder(never, lines)
    paid = FakeGorelo()
    paid.on_invoice_created(lambda invoice: invoice.update(Status={"Id": 3, "Name": "Paid"}))
    lines.clear()
    assert report_has_no_void_reminder(await approved(paid, started=datetime(2099, 10, 2, 10, 16, 0, tzinfo=timezone.utc)), lines)
    draft = FakeGorelo()
    draft.on_invoice_created(lambda invoice: invoice.update(Status={"Id": 1, "Name": "Draft"}))  # a Draft is deleted, not voided
    lines.clear()
    assert report_has_no_void_reminder(await approved(draft, started=datetime(2099, 10, 2, 10, 17, 0, tzinfo=timezone.utc)), lines)
    lines.clear()  # a full run with the invoices area: its Draft is deleted
    full = await run(FakeGorelo(), with_items=True, with_invoices=True, started=datetime(2099, 10, 2, 10, 18, 0, tzinfo=timezone.utc))
    assert full.ok and report_has_no_void_reminder(full, lines)
    lines.clear()  # and the approved area skipped without its flag
    skipped = await run(FakeGorelo(), areas=["approved_invoice"], started=datetime(2099, 10, 2, 10, 19, 0, tzinfo=timezone.utc))
    assert report_has_no_void_reminder(skipped, lines)


def report_has_no_void_reminder(report, lines) -> bool:
    notes = [note for result in report.results for note in result.notes]
    return report.voided == [] and REMINDER not in notes and not any(line.startswith("reminder:") for line in lines)


def test_the_summary_ends_with_one_reminder_per_voided_invoice_just_before_the_result():
    plain = write_matrix.MatrixReport(run_id=RUN, manifest_path="m.json", cleanup=None).render()
    assert not any(line.startswith("reminder:") for line in plain)
    report = write_matrix.MatrixReport(run_id=RUN, manifest_path="m.json", cleanup=None, voided=["INV-7", "1043"])
    assert report.render()[-3:] == [
        f"reminder: approved invoice INV-7: {REMINDER}", f"reminder: approved invoice 1043: {REMINDER}", "result: FAILED",
    ]


def test_voided_invoices_names_the_invoices_the_manifest_records_as_void(tmp_path):
    manifest = Manifest.start(RUN_START, directory=tmp_path)

    def invoice(n: int, **details) -> None:
        manifest.created("invoice", invoice_guid(n), manifest.label(f"invoice {n}"), details)

    invoice(1, status_id=4, number=1042, display_number="INV-1042")  # voided: named by its DisplayNumber
    invoice(2, status_id=4, number=1043)  # no display number: its Number
    invoice(3, status_id=4)  # neither: its Id
    invoice(4, status_id=5, number=1044, display_number="INV-1044")  # still Approved
    invoice(5, status_id=1, number=1045, display_number="INV-1045")  # a Draft
    invoice(6, status_id=3, number=1046, display_number="INV-1046")  # Paid
    invoice(7, status_id=True, number=1047, display_number="INV-1047")  # true is not the number 4
    invoice(8, number=1048, display_number="INV-1048")  # no status recorded
    manifest.created("contact", 9101, manifest.label("contact"), {"client_id": TEST_CLIENT, "status_id": 4})  # not an invoice
    assert write_matrix.voided_invoices(manifest) == ["INV-1042", "1043", invoice_guid(3)]
    assert write_matrix.voided_invoices(Manifest.start(datetime(2099, 10, 2, 10, 16, 0, tzinfo=timezone.utc), directory=tmp_path)) == []


# the run around it: the tools, the guard, the cleanup


async def test_approved_invoice_run_may_call_only_its_seven_tools(run, monkeypatch):
    fake = FakeGorelo()
    refused = []

    async def sneaky(matrix):
        for tool, arguments in (
            ("create_invoice", {"client_id": TEST_CLIENT, "line_items": [{"item_id": GLOBAL_PRODUCT, "quantity": 1}]}),
            ("export_invoice_pdf", {"invoice_id": invoice_guid(1)}),
            ("create_item", {"type": "product", "name": f"{RUN} sneaky", "client_id": TEST_CLIENT}),
            ("post_alert", {"client_id": TEST_CLIENT, "name": "x", "resource": "y", "severity": 1}),
            ("list_clients", {}),
            ("update_client", {"client_id": 8200, "alternate_name": "x"}),
        ):
            with pytest.raises(RuntimeError, match=f"{tool} is not one of the tools this script may call"):
                await matrix.tool(tool, **arguments)
            refused.append(tool)

    monkeypatch.setattr(write_matrix, "AREAS", (write_matrix.Area("approved_invoice", sneaky),))
    report = await run(fake, areas=["approved_invoice"], with_approved_invoice=True)
    assert len(refused) == 6 and fake.mock.requests == [] and report.refused == 0 and report.ok


async def test_approved_invoice_without_an_intent_the_guard_stops_the_run_before_a_request_leaves(run, monkeypatch):
    fake = FakeGorelo()

    async def forgetful(matrix):
        await matrix.tool("create_approved_invoice", client_id=TEST_CLIENT, line_items=[{"item_id": GLOBAL_PRODUCT, "quantity": 1, "unit_price": 1.0}], confirm=True)

    monkeypatch.setattr(write_matrix, "AREAS", (write_matrix.Area("approved_invoice", forgetful),))
    report = await run(fake, areas=["approved_invoice"], with_approved_invoice=True)
    assert [(r.area, r.status) for r in report.results] == [("approved_invoice", "FAIL")]
    assert "no open intent of kind 'invoice'" in results(report)["approved_invoice"].detail
    assert fake.mock.requests == [] and report.refused == 1 and fake.invoices == {}


async def test_approved_invoice_a_second_approved_invoice_in_the_same_run_is_stopped_by_the_guard(run, monkeypatch):
    fake = FakeGorelo()

    async def twice(matrix):
        for text in ("approved invoice", "approved invoice again"):
            label = matrix.manifest.label(text)
            await matrix.create(
                "invoice", label, {"status_id": 5}, "create_approved_invoice",
                {
                    "client_id": TEST_CLIENT, "reference": label, "confirm": True,
                    "line_items": [{"item_id": GLOBAL_PRODUCT, "quantity": 1, "unit_price": 1.0, "description": label, "no_tax": True}],
                },
                recorded=lambda record: write_matrix.invoice_record(record, 5),
            )

    monkeypatch.setattr(write_matrix, "AREAS", (write_matrix.Area("approved_invoice", twice),))
    report = await run(fake, areas=["approved_invoice"], with_approved_invoice=True)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "this run already created an Approved invoice" in found.detail and report.refused == 1
    assert len(fake.invoices) == 1 and len([r for r in fake.mock.requests if r.method == "POST"]) == 1
    assert report.summary["leftovers"] == 1 and report.summary["unresolved_intents"] == 0  # the first one stays, Approved


async def test_approved_invoice_the_guard_of_the_run_has_the_option_only_for_this_run_and_the_cleanup_never_has_it(run, approved, monkeypatch):
    guards = []

    class Spy(write_matrix.LiveGuard):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            guards.append(self)

    cleanups = []
    real_cleanup = write_matrix.run_cleanup

    async def spy_cleanup(manifest, **options):
        cleanups.append(options)
        return await real_cleanup(manifest, **options)

    monkeypatch.setattr(write_matrix, "LiveGuard", Spy)
    monkeypatch.setattr(write_matrix, "run_cleanup", spy_cleanup)
    await approved(FakeGorelo())
    await run(FakeGorelo(), areas=["clients"], started=datetime(2099, 10, 2, 10, 16, 0, tzinfo=timezone.utc))
    assert [g.allow_approved_invoice for g in guards] == [True, False] and all(g.require_intents and not g.cleanup for g in guards)
    # the matrix's own cleanup never voids: it is told nothing about it, and --void-approved is the user's command
    assert len(cleanups) == 2 and all("void_approved" not in options for options in cleanups)


async def test_approved_invoice_cleanup_runs_and_never_voids_when_the_run_is_interrupted_after_the_create(approved, monkeypatch, lines):
    fake = FakeGorelo()

    class Abort(BaseException):
        pass

    async def interrupted(self, number, expected):
        raise Abort("stop")

    monkeypatch.setattr(write_matrix.Matrix, "remove_invoice", interrupted)
    with pytest.raises(Abort):
        await approved(fake)
    invoice = the_invoice(fake)
    assert invoice["Status"]["Id"] == 5 and not any(r.method == "DELETE" for r in fake.mock.requests)  # approved, not voided
    assert any(line.startswith("cleanup of MCPTEST-") for line in lines)
    assert any(line.startswith("  LEFTOVER   invoice ") and "--void-approved" in line for line in lines)
    assert any("interrupted (Abort)" in line for line in lines) and lines[-1] == "result: FAILED"


async def test_approved_invoice_the_server_must_offer_the_gated_tools_when_the_flag_is_given(tmp_path, make_settings):
    fake = FakeGorelo()
    with pytest.raises(write_matrix.SetupError, match="does not offer the tools the matrix calls: create_approved_invoice, delete_invoice"):
        await write_matrix.run_matrix(
            settings=make_settings(destructive=False), transport=fake.mock.transport, directory=tmp_path / "runs", pace=0,
            echo=lambda line: None, areas=["approved_invoice"], with_approved_invoice=True,
        )
    assert fake.mock.requests == [] and not (tmp_path / "runs").exists()
    # without the flag the matrix never needs create_approved_invoice, whatever the settings
    report = await write_matrix.run_matrix(
        settings=make_settings(destructive=True), transport=fake.mock.transport, directory=tmp_path / "runs", pace=0,
        echo=lambda line: None, areas=["approved_invoice"], started=RUN_START,
    )
    assert report.ok and results(report)["approved_invoice"].status == "skipped"


async def test_approved_invoice_the_api_key_never_appears_in_the_output_even_when_gorelo_echoes_it(approved, lines):
    fake = FakeGorelo()
    fake.fail(READ, error_envelope(500, [("070500", f"boom, key {TEST_API_KEY}")]))  # the create's read-back fails
    fake.fail("GET /v1/contacts", error_envelope(500, [("070500", f"boom, key {TEST_API_KEY}")]))
    first = await approved(fake)
    assert results(first)["approved_invoice"].status == "FAIL" and "***" in results(first)["approved_invoice"].detail
    other = FakeGorelo()
    other.fail(READ, error_envelope(500, [("070500", f"boom, key {TEST_API_KEY}")]))
    second = await approved(other, started=datetime(2099, 10, 2, 10, 16, 0, tzinfo=timezone.utc))
    assert "the write succeeded but reading it back failed" in results(second)["approved_invoice"].detail
    assert not any(TEST_API_KEY in line for line in lines) and any("***" in line for line in lines)
    manifest_text = Path(first.manifest_path).read_text(encoding="utf-8") + Path(second.manifest_path).read_text(encoding="utf-8")
    assert TEST_API_KEY not in manifest_text


async def test_approved_invoice_nothing_but_the_runs_own_invoice_and_the_reads_are_ever_sent(approved):
    fake = FakeGorelo()
    report = await approved(fake)
    assert report.ok
    (ident,) = fake.invoices
    for request in fake.mock.requests:
        if request.method == "POST":
            assert request.path == "/v1/invoices" and request.json["ClientId"] == TEST_CLIENT and request.json["StatusId"] == 5
            assert "RecipientEmails" not in request.json
        if request.method == "DELETE":
            assert request.path == f"/v1/invoices/{ident}"
        assert request.method in ("GET", "POST", "DELETE") and request.path not in ("/v1/alerts", "/v1/api-keys")
        assert not request.path.endswith("/pdf") and "/assets/" not in request.path
    assert len([r for r in fake.mock.requests if r.method == "POST"]) == 1 and len(fake.exports) == 0
    assert report.refused == 0


# the fake itself follows the spec (so that the tests above prove something)


def send(fake: FakeGorelo, method: str, path: str, body=None):
    request = httpx.Request(method, f"https://api.usw.gorelo.io{path}", json=body)
    response = httpx.Client(transport=fake.mock.transport).send(request)
    return response.status_code, response.json()


def test_the_fake_follows_the_spec_for_approved_invoices():
    fake = FakeGorelo(sync_after=2)
    line = {"ItemId": GLOBAL_PRODUCT, "Quantity": 1, "UnitPrice": 1.0}
    # an Approved invoice whose total is exactly 0 is created as Paid, which can be neither deleted nor voided (409)
    code, zero = send(fake, "POST", "/v1/invoices", {"ClientId": TEST_CLIENT, "StatusId": 5, "LineItems": [{**line, "UnitPrice": 0.0}]})
    paid = zero["Data"]["Id"]
    assert code == 200 and send(fake, "GET", f"/v1/invoices/{paid}")[1]["Data"]["Status"] == {"Id": 3, "Name": "Paid"}
    assert send(fake, "DELETE", f"/v1/invoices/{paid}")[0] == 409
    # a Draft with a total of 0 stays a Draft
    draft = send(fake, "POST", "/v1/invoices", {"ClientId": TEST_CLIENT, "StatusId": 1, "LineItems": [{**line, "UnitPrice": 0.0}]})[1]["Data"]["Id"]
    assert send(fake, "GET", f"/v1/invoices/{draft}")[1]["Data"]["Status"]["Id"] == 1
    # approving sets ExternalId and a payment link, after as many reads as the fake was told to wait
    approved_id = send(fake, "POST", "/v1/invoices", {"ClientId": TEST_CLIENT, "StatusId": 5, "LineItems": [line]})[1]["Data"]["Id"]
    first = send(fake, "GET", f"/v1/invoices/{approved_id}")[1]["Data"]
    second = send(fake, "GET", f"/v1/invoices/{approved_id}")[1]["Data"]
    third = send(fake, "GET", f"/v1/invoices/{approved_id}")[1]["Data"]
    assert first["Status"]["Id"] == 5 and first["ExternalId"] is None and second["ExternalId"] is None and first["PaymentLink"] is None
    assert third["ExternalId"] is not None and third["PaymentLink"].startswith("https://in.xero.com/")
    assert third["IsEmailSent"] is False and third["EmailSentOn"] is None and "_reads" not in third and third["Total"] == 1.0
    # a Draft is deleted (6, and again 6), an Approved invoice is voided (4), and deleting a void invoice is a success (4)
    assert send(fake, "DELETE", f"/v1/invoices/{draft}")[1]["Data"] == {"Id": draft, "StatusId": 6}
    assert send(fake, "DELETE", f"/v1/invoices/{draft}")[1]["Data"] == {"Id": draft, "StatusId": 6}
    assert send(fake, "DELETE", f"/v1/invoices/{approved_id}")[1]["Data"] == {"Id": approved_id, "StatusId": 4}
    code, again = send(fake, "DELETE", f"/v1/invoices/{approved_id}")
    assert code == 200 and again["Data"] == {"Id": approved_id, "StatusId": 4}
    assert send(fake, "GET", f"/v1/invoices/{approved_id}")[1]["Data"]["Status"] == {"Id": 4, "Name": "Void"}  # still readable, listed
    assert fake.leftovers() == [f"invoice {paid}"] and fake.residue() == [f"invoice {approved_id}"]


def test_the_fake_never_syncs_when_it_is_told_not_to_and_serves_no_private_bookkeeping():
    fake = FakeGorelo(sync_after=None, payment_link=False)
    line = {"ItemId": GLOBAL_PRODUCT, "Quantity": 1, "UnitPrice": 1.0}
    ident = send(fake, "POST", "/v1/invoices", {"ClientId": TEST_CLIENT, "StatusId": 5, "LineItems": [line]})[1]["Data"]["Id"]
    for _ in range(5):
        record = send(fake, "GET", f"/v1/invoices/{ident}")[1]["Data"]
        assert record["ExternalId"] is None and record["PaymentLink"] is None
    rows = send(fake, "GET", "/v1/invoices")[1]["Data"]
    assert not any(key.startswith("_") for row in rows for key in row) and "LineItems" not in rows[0]
    assert FakeGorelo(payment_link=False).payment_link is False


def test_the_fake_lists_the_contacts_of_a_client_like_the_api_does():
    fake = FakeGorelo()
    code, listed = send(fake, "GET", "/v1/contacts?ClientIds=9501&PageSize=200")
    rows = listed["Data"]
    assert code == 200 and len(rows) == 4 and all(row["ClientId"] == TEST_CLIENT and row["Status"]["Name"] == "Inactive" for row in rows)
    assert all(row["PrimaryEmail"].endswith("@example.invalid") for row in rows)
    assert send(fake, "GET", "/v1/contacts?ClientIds=9502")[1]["Data"][0]["Id"] == OPERATOR_CONTACT
    assert len(send(fake, "GET", "/v1/contacts")[1]["Data"]) == 5  # every client
    # every location answers BillingContactIds as production does when it names nobody: the text of an empty JSON list
    assert fake.locations[TEST_CLIENT][0]["BillingContactIds"] == "[]" and fake.locations[SECOND][0]["BillingContactIds"] == "[]"
    assert fake.leftovers() == []  # the contacts that existed before the run are nobody's leftover


# helpers


def test_invoice_record_falls_back_to_the_status_it_is_told_not_to_a_draft():
    record = write_matrix.invoice_record
    assert record({"Id": "x", "warning": "read-back failed"}, 5) == {"status_id": 5, "number": None, "display_number": None}
    assert record({"Id": "x", "warning": "read-back failed"}) == {"status_id": 1, "number": None, "display_number": None}
    for status in (None, "5", True, 0, -1, [], {}):
        assert record({"Status": status}, 5)["status_id"] == 5
    assert record({"Status": {"Id": 3, "Name": "Paid"}}, 5)["status_id"] == 3  # what Gorelo stored wins over the fallback
    assert record({"Status": {"Id": 1, "Name": "Draft"}}, 5)["status_id"] == 1


def test_email_problem_is_none_only_for_an_invoice_that_says_it_was_not_emailed():
    problem = write_matrix.email_problem
    assert problem({"IsEmailSent": False, "EmailSentOn": None}) is None
    assert problem({"IsEmailSent": True, "EmailSentOn": None}).startswith("WARNING: invoice EMAILED (IsEmailSent True, EmailSentOn None)")
    assert problem({"IsEmailSent": False, "EmailSentOn": "2026-10-02T10:20:00Z"}).startswith("WARNING: invoice EMAILED")
    for record in ({}, {"IsEmailSent": False}, {"EmailSentOn": None}, {"IsEmailSent": 0, "EmailSentOn": None}, {"IsEmailSent": False, "EmailSentOn": ""}):
        assert problem(record).startswith("WARNING: whether the invoice was emailed cannot be read")


def test_the_precondition_helpers_read_only_what_they_can_prove():
    inactive, free = write_matrix.inactive_contact, write_matrix.no_billing_contacts
    assert inactive({"Status": {"Id": 2, "Name": "Inactive"}}) and inactive({"Status": {"Name": " INACTIVE "}})
    for row in ({"Status": {"Id": 1, "Name": "Active"}}, {"Status": {"Id": 2}}, {"Status": None}, {}, "x", None, {"Status": "Inactive"}):
        assert not inactive(row)
    for ids in ("", "  ", None, [], "[]", " [ ] ", "[]\n"):  # "[]" is what production answers
        assert free({"Id": 1, "BillingContactIds": ids})
    for row in ({"Id": 1}, {"BillingContactIds": "1"}, {"BillingContactIds": ["1"]}, {"BillingContactIds": 1}, {"BillingContactIds": 0}, "x", None):
        assert not free(row)
    for ids in (
        "[123]", "[\"123\"]", "[0]", "123,456", "[]x", "[", "{}", "null x", "[[]]", "\"[]\"",
        "null", "[null]", "[],[]", "[] []", "\ufeff[]", "[]\x00", "[ \u00a0]",
        "[" * 100000 + "]" * 100000,  # json.loads raises RecursionError here, which is no ValueError: it must still say "no"
    ):
        assert not free({"Id": 1, "BillingContactIds": ids}), ids[:40]
    assert write_matrix.row_ids([{"Id": 5}, {"Id": True}, {"Id": "7"}, "x", {"Id": 9}]) == "5, ?, ?, ?, 9"
    assert write_matrix.row_ids([{"Id": n} for n in range(1, 13)]) == "1, 2, 3, 4, 5, 6, 7, 8, 9, 10 and 2 more"


def test_invoice_name_and_amount_helpers():
    name, amount = write_matrix.invoice_name, write_matrix.is_amount
    assert name({"DisplayNumber": " INV-5 ", "Number": 5, "Id": "x"}) == "INV-5" and name({"DisplayNumber": "", "Number": 5}) == "5"
    assert name({"Id": invoice_guid(1)}) == invoice_guid(1) and name({}) == "(no number)" and name({"Number": True}) == "(no number)"
    assert amount(1) and amount(0.5) and amount(0) and amount(-3.5)
    for value in (True, None, "1", [], float("inf"), float("nan"), 10**400):
        assert not amount(value)


def test_the_module_docstring_tells_the_rules_of_the_approved_run_as_the_code_has_them():
    text = " ".join(write_matrix.__doc__.split())
    for said in (
        # the 429 retries are off for the one create call and for nothing else
        "The create call alone (create_approved_invoice, which also reads the new invoice back) runs with the 429 retries of "
        "the run's Gorelo client switched off (max_429_retries=0, put back right after the call, also when it raises), so the "
        "one create is never sent twice",
        "a 429 on it is a plain rejected create that settles its intent",
        "Every other request of the run keeps the client's normal retries",
        "That run also switches off the 429 retries of its Gorelo client around the create call alone (no_429_retries, put "
        "back right after it)",
        "the client is looked up before the first request (gorelo_client_behind), or the run does not start",
        "A 429 on the create is not retried (one POST, a failed area, an intent settled as failed), and neither is one on its "
        "read-back, which belongs to the same call",
        "a delete_invoice whose lookup or DELETE is still answered 429 after those is asked again by the area",
        "when Gorelo still answers 429 to its lookup or its DELETE after the client's own retries, the area asks again, up to "
        "3 attempts in all, because Gorelo did not process that request",
        # a failed poll read is not a stop, and it is noted once per distinct text, trace ids aside
        "A read that fails meanwhile (an HTTP error, a timeout or a 429 that outlasts the client's own retries: a GET writes "
        "nothing) only means \"not synced yet\"",
        "(once per distinct text; the trace id that ends an error is left out when texts are compared)",
        "the wait ends only on a status other than Approved or when the 120 s run out",
        "(a read that fails is one of those polls, not a stop: the next one asks again)",
        # the void is in Gorelo only
        "then a note that the void is in Gorelo only (check the accounting system and void the invoice there too: Gorelo's void is not "
        "pushed to the connected accounting system, seen with Xero)",
        # the hints that name the cleanup command say that it voids in Gorelo only too
        "not yet synced to accounting; check the accounting system, then void it with: python -m scripts.live.cleanup "
        "<manifest> --void-approved (this voids it in Gorelo only: void its copy in the accounting system by hand too)\"",
        "approved invoice <DisplayNumber> has no Number, so delete_invoice cannot find it; void it with: python -m "
        "scripts.live.cleanup <manifest> --void-approved (this voids it in Gorelo only: void its copy in the accounting "
        "system by hand too)\"",
        "and the note that this voids it in Gorelo only (VOID_COMMAND_NOTE: its copy in the accounting system is voided by "
        "hand too)",
        "The two sentences live in scripts/live/cleanup.py (VOID_REMINDER and VOID_COMMAND_NOTE), which says the first for "
        "each invoice it voids itself and the second in its leftover text and in the help of --void-approved; every hint "
        "printed here that names that command (the area's FAIL for an invoice that never synced or has no Number, and the "
        "summary's line for an Approved invoice left over) ends with the second.",
        "The summary of the run repeats the reminder about the void, for every approved invoice of the run that was voided, "
        "as its last line before the result",
        "a reminder line that the void is in Gorelo only: check the accounting system and void the invoice there too (Gorelo's void "
        "is not pushed to the connected accounting system, seen with Xero)",
        # TaxId null is sent, and the totals are checked after the void, as a note
        "no tax: TaxId is sent as an explicit null, which the guard insists on",
        "The read after the void also checks TotalTax 0 and Total 1.0",
        "when it reads otherwise, a loud note says the no-tax check FAILED",
        "never stop or skip the void or its bookkeeping",
    ):
        assert said in text, said


def test_the_texts_about_voiding_and_about_undeletable_records_say_what_the_code_does():
    """The matrix and the cleanup void in two places (the approved_invoice area, and the cleanup with
    --void-approved), and a voided invoice is a known undeletable record like an uploaded file: no text may say that the
    harness never voids, or that only attachment files are undeletable."""
    source = Path(write_matrix.__file__).read_text(encoding="utf-8")
    flat = " ".join(source.split())
    assert "this matrix never voids anything" not in flat and "never voids anything" not in flat
    assert (
        "# Approved: voided (status Void, still listed), and this area never voids: only the approved_invoice area does, for "
        "# the one invoice it approved itself."
    ) in flat
    report = " ".join(write_matrix.MatrixReport.__doc__.split())
    assert "undeletable (known undeletable records, uploaded attachment files and voided invoices, which nobody can remove" in report
    assert "known undeletable attachment files" not in report
    doc = " ".join(write_matrix.__doc__.split())
    assert "The cleanup the matrix runs NEVER voids (it is not given void_approved: only the approved_invoice area voids" in doc
    assert "which voids it with delete_invoice or, as for a Draft, the raw DELETE backstop" in doc
    assert "A create that was announced without an id (create_invoice or create_approved_invoice) is searched by the run label" in doc
    assert "a voided invoice is counted the same way (it cannot be removed either)" in doc
    assert (
        "The cleanup that follows never voids (run_cleanup is not given void_approved: that is the user's command, cleanup "
        "--void-approved)"
    ) in " ".join(write_matrix.run_matrix.__doc__.split())


async def test_uptime_check_targets_the_second_url_and_the_maintenance_window_starts_now_with_an_offset(run):
    fake = FakeGorelo()
    now = datetime(2099, 10, 2, 10, 20, 0, tzinfo=timezone.utc)
    report = await run(fake, areas=["uptime"], clock=lambda: now)
    assert report.ok, results(report)["uptime"].detail
    bodies = [r.json for r in fake.mock.requests if r.method in ("POST", "PATCH")]
    assert bodies[0] == {
        "TypeId": 2, "Frequency": 60, "RegionId": 1, "Target": {"Url": PROBE_URL},
        "ClientId": TEST_CLIENT, "LocationId": 9001, "Description": f"{RUN} uptime check",
    }
    # live, 2026-10-02: Gorelo refuses a window without a start, and the tool never reads the clock: the start is now (UTC, with an offset)
    assert bodies[1] == {
        "MaintenanceMode": {
            "Enabled": True,
            "StartDateTime": "2099-10-02T10:20:00Z",
            "DurationInMinutes": 60,
            "Reason": f"{RUN} uptime check: maintenance during the live test",
        }
    }
    assert bodies[2] == {"Description": f"{RUN} uptime check (updated)"}
    assert PROBE_URL == "https://mcp.example.net/.well-known/oauth-authorization-server" == write_matrix.SITE.probe_url
    assert any("maintenance window after update_uptime_check: Enabled=True DurationInMinutes=60" in n for n in results(report)["uptime"].notes)


async def test_projects_a_missing_scope_is_a_skip_with_two_reads_and_nothing_else(run):
    fake = FakeGorelo(project_scope=False, forms_scope=True)
    report = await run(fake, areas=["projects"])
    result = results(report)["projects"]
    assert report.ok and result.status == "skipped" and result.detail == "scope missing: Project" and result.requests == 2
    assert [(r.method, r.path) for r in fake.mock.requests] == [("GET", "/v1/projects"), ("GET", "/v1/forms")]
    assert any("forms scope is available" in note for note in result.notes)
    assert report.scopes_missing == ["Project"] and report.summary["created"] == 0


async def test_projects_with_the_scope_everything_goes_through_the_gated_tools_and_the_project_is_left_to_the_cleanup(run):
    fake = FakeGorelo(forms_scope=False)
    report = await run(fake, areas=["projects"])
    assert report.ok, results(report)["projects"].detail
    assert report.scopes_missing == ["Forms"]
    assert [r.method for r in fake.mock.requests if r.method != "GET"] == ["POST", "POST", "POST", "POST", "DELETE", "DELETE", "DELETE"]
    section = next(iter(fake.sections))
    task_body = next(r.json for r in fake.mock.requests if r.method == "POST" and r.path.endswith("/tasks"))
    assert task_body["SectionId"] == section and task_body["Title"] == f"{RUN} task"
    comment_body = next(r.json for r in fake.mock.requests if r.path.endswith("/comments") and r.method == "POST")
    assert comment_body["ConversationTypeId"] == 2
    assert fake.leftovers() == [] and ("project", next(iter(fake.projects))) in fake.deleted
    assert report.summary["created"] == 4 and report.summary["cleaned"] == 4


async def test_projects_create_project_gets_the_first_project_type_and_the_everyone_group(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["projects"])
    assert report.ok, results(report)["projects"].detail
    # run alone, the area needs the test client's location and the ticket lookups (the Everyone group) first, after its scope probes
    assert area_ops(fake, report)["projects"] == [
        "GET /v1/projects",
        "GET /v1/forms",
        "GET /v1/clients/{clientId}/locations",
        "GET /v1/tickets/statuses",
        "GET /v1/tickets/types",
        "GET /v1/organization/groups",
        "GET /v1/projects/types",
        *GOLDEN["projects"][3:],
    ]
    post = next(r.json for r in fake.mock.requests if r.method == "POST" and r.path == "/v1/projects")
    assert post == {
        "Title": f"{RUN} project", "ClientId": TEST_CLIENT, "LocationId": 9001,
        "TypeId": project_type_guid(1),  # the first type list_project_types answered
        "GroupId": 7201,  # the Everyone group, resolved by name
    }
    assert fake.leftovers() == []


async def test_projects_in_a_full_run_the_lookups_are_reused_not_asked_again(run):
    fake = FakeGorelo()
    report = await run(fake, areas=["tickets", "uptime", "projects"])
    assert report.ok
    assert area_ops(fake, report)["projects"] == GOLDEN["projects"]  # the location and the three lookups are cached already
    assert len([r for r in fake.mock.requests if r.path == "/v1/organization/groups"]) == 1


async def test_projects_a_tenant_without_project_types_skips_the_area_before_creating_anything(run):
    fake = FakeGorelo(project_types=[])
    report = await run(fake, areas=["projects"])
    result = results(report)["projects"]
    assert report.ok and result.status == "skipped"
    assert result.detail == "the tenant has no project type to create a project with"
    assert not any(r.method == "POST" for r in fake.mock.requests) and fake.projects == {}


async def test_projects_the_type_is_the_first_row_with_a_guid_id(run):
    types = [{"Id": None, "Name": "broken"}, {"Id": "not-a-guid"}, "x", {"Id": project_type_guid(7), "Name": "Real"}]
    fake = FakeGorelo(project_types=types)
    report = await run(fake, areas=["projects"])
    assert report.ok, results(report)["projects"].detail
    assert next(r.json for r in fake.mock.requests if r.method == "POST" and r.path == "/v1/projects")["TypeId"] == project_type_guid(7)


async def test_projects_the_fake_refuses_a_project_without_a_type_or_a_group_like_production_does(run):
    fake = FakeGorelo()
    base = {"Title": "t", "ClientId": TEST_CLIENT, "LocationId": 9001}

    def post(body):
        sent = httpx.Request("POST", "https://api.usw.gorelo.io/v1/projects", json=body)
        return httpx.Client(transport=fake.mock.transport).send(sent)

    assert post(base).status_code == 400
    assert post({**base, "TypeId": project_type_guid(1)}).status_code == 400  # a group is required too
    assert post({**base, "GroupId": 7201}).status_code == 400  # and a type
    assert post({**base, "TypeId": project_type_guid(99), "GroupId": 7201}).status_code == 400  # a type that exists
    assert post({**base, "TypeId": project_type_guid(1), "GroupId": 7201}).status_code == 200


async def test_the_manifest_records_the_details_the_guard_and_the_cleanup_read(run, tmp_path):
    fake = FakeGorelo()
    report = await run(fake, with_items=True)
    assert report.ok
    manifest = Manifest.load(report.manifest_path)
    by_kind: dict[str, list] = collections.defaultdict(list)
    for record in manifest.all_created():
        assert record.label.startswith(RUN)
        by_kind[record.kind].append(record)
    plain, backdated, email = by_kind["ticket"]
    assert plain.details == {"client_id": TEST_CLIENT, "contact_id": None, "cc_contact_ids": []}
    assert backdated.details == {"client_id": TEST_CLIENT, "contact_id": None, "cc_contact_ids": []}
    assert email.details == {"client_id": SECOND, "contact_id": OPERATOR_CONTACT, "cc_contact_ids": []}
    private, with_attachment, public, side_comment, approval_comment = by_kind["comment"]
    assert [c.details["private"] for c in by_kind["comment"]] == [True, True, False, False, False]
    assert private.details["ticket_id"] == with_attachment.details["ticket_id"] == plain.id
    assert public.details["ticket_id"] == side_comment.details["ticket_id"] == approval_comment.details["ticket_id"] == email.id
    (attachment,) = by_kind["attachment"]
    assert attachment.id == f"{RUN}-attachment.txt" and attachment.details == {"ticket_id": plain.id}
    (side,), (approval,) = by_kind["side_conversation"], by_kind["approval"]
    assert side.id == 700 and side.details == {"ticket_id": email.id} and approval.details == {"ticket_id": email.id}
    assert by_kind["client"][0].details == {} and by_kind["contact"][0].details == {"client_id": TEST_CLIENT}
    assert by_kind["time_entry"][0].details == {"ticket_id": plain.id}
    assert by_kind["item"][0].details == {"client_id": TEST_CLIENT} and by_kind["uptime"][0].details == {"client_id": TEST_CLIENT}
    (project,) = by_kind["project"]
    assert by_kind["section"][0].details == {"project_id": project.id} and by_kind["task"][0].details == {"project_id": project.id}
    assert by_kind["project_comment"][0].details == {"project_id": project.id, "task_id": by_kind["task"][0].id}
    # what the matrix deleted itself says so, and everything else was cleaned by the cleanup
    by_matrix = {r.kind for r in manifest.all_created() if (r.outcome or "").startswith("deleted by the write matrix")}
    assert by_matrix == {"time_entry", "item", "uptime", "task", "project_comment"} | {"comment"}
    assert manifest.unresolved_intents() == []
    # the one record that is not cleaned is the uploaded file: the API cannot delete it, and the cleanup says so
    assert [(r.kind, r.id, r.outcome) for r in manifest.leftovers()] == [
        (
            "attachment",
            f"{RUN}-attachment.txt",
            "soft-deleted with its ticket; the file itself cannot be deleted through the API",
        )
    ]
    # an uploaded file's url carries an access token: only its name is ever written down
    assert "token=abc123" not in manifest.path.read_text(encoding="utf-8")


async def test_a_create_without_an_intent_is_refused_by_the_guard_and_stops_the_run(run, monkeypatch):
    fake = FakeGorelo()

    async def forgetful(matrix):
        await matrix.tool("create_item", type="product", name=f"{RUN} sneaky", client_id=TEST_CLIENT)  # no manifest.intent first

    monkeypatch.setattr(
        write_matrix, "AREAS", (write_matrix.Area("items", forgetful), write_matrix.Area("uptime", write_matrix.area_uptime))
    )
    report = await run(fake, with_items=True)
    assert [(r.area, r.status) for r in report.results] == [("items", "FAIL"), ("uptime", "not run")]
    assert "no open intent of kind 'item'" in results(report)["items"].detail
    assert fake.mock.requests == [] and report.refused == 1


async def test_a_tool_outside_the_matrix_allowlist_is_refused_before_any_request(run, monkeypatch):
    fake = FakeGorelo()

    async def alarm(matrix):
        await matrix.tool("post_alert", client_id=TEST_CLIENT, name="x", resource="y", severity=1)

    monkeypatch.setattr(write_matrix, "AREAS", (write_matrix.Area("clients", alarm),))
    report = await run(fake)
    assert results(report)["clients"].status == "FAIL"
    assert "post_alert is not one of the tools this script may call" in results(report)["clients"].detail
    assert fake.mock.requests == [] and report.refused == 0


async def test_a_failed_plain_ticket_skips_the_areas_that_need_it_and_the_others_still_run(run):
    fake = FakeGorelo()
    fake.fail("POST /v1/tickets", error_envelope(400, [("070101", "Title is too long", "Title")]))
    report = await run(fake, with_items=True)
    found = results(report)
    assert found["tickets"].status == "FAIL" and "create_ticket" in found["tickets"].detail
    for area in ("comments", "time"):
        assert found[area].status == "skipped" and found[area].detail.startswith("no run ticket: ")
    assert found["email"].status == "pass" and found["items"].status == "pass"  # the email ticket is its own
    assert not report.ok and report.summary["unresolved_intents"] == 0 and fake.leftovers() == []


async def test_a_missing_email_status_fails_before_anything_is_emailed(run):
    fake = FakeGorelo(status_names={1: "New", 4: "Closed"})
    report = await run(fake, areas=["email"])
    assert results(report)["email"].status == "FAIL"
    assert "cannot resolve the ticket status named 'In Progress'" in results(report)["email"].detail
    assert not any(r.method == "POST" for r in fake.mock.requests)  # no ticket, no email


async def test_the_leftovers_flag_reaches_the_cleanup(run, monkeypatch):
    seen = []
    real = write_matrix.run_cleanup

    async def spy(manifest, **options):
        seen.append(options["leftovers"])
        return await real(manifest, **options)

    monkeypatch.setattr(write_matrix, "run_cleanup", spy)
    await run(FakeGorelo(), areas=["items"], leftovers=True)
    await run(FakeGorelo(), areas=["items"], started=datetime(2099, 10, 2, 10, 17, 0, tzinfo=timezone.utc))
    assert seen == [True, False]


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------


def test_the_command_line_passes_its_flags_and_turns_the_report_into_an_exit_status(monkeypatch, capsys):
    calls = []

    async def fake_run(**options):
        calls.append(options)
        return write_matrix.MatrixReport(run_id=RUN, manifest_path="m.json", cleanup=None)

    monkeypatch.setattr(write_matrix, "run_matrix", fake_run)
    assert write_matrix.main(["--skip-email", "--only", "items, uptime", "--leftovers"]) == 1  # cleanup None: not ok
    assert write_matrix.main(["--with-items"]) == 1
    assert write_matrix.main(["--with-invoices"]) == 1
    assert write_matrix.main(["--with-items", "--with-invoices", "--only", "invoices"]) == 1
    assert write_matrix.main([]) == 1
    assert write_matrix.main(["--with-approved-invoice", "--only", "approved_invoice"]) == 1
    flags = {"with_items": False, "with_invoices": False, "with_approved_invoice": False}
    assert calls == [
        {"areas": ["items", " uptime"], "skip_email": True, "leftovers": True, **flags},
        {"areas": None, "skip_email": False, "leftovers": False, **{**flags, "with_items": True}},
        {"areas": None, "skip_email": False, "leftovers": False, **{**flags, "with_invoices": True}},
        {"areas": ["invoices"], "skip_email": False, "leftovers": False, **{**flags, "with_items": True, "with_invoices": True}},
        {"areas": None, "skip_email": False, "leftovers": False, **flags},
        {"areas": ["approved_invoice"], "skip_email": False, "leftovers": False, **{**flags, "with_approved_invoice": True}},
    ]

    async def passed(**options):
        return write_matrix.MatrixReport(
            run_id=RUN, manifest_path="m.json", cleanup=write_matrix.CleanupReport(run_id=RUN)
        )

    monkeypatch.setattr(write_matrix, "run_matrix", passed)
    assert write_matrix.main(["--only", "items"]) == 0


def test_main_runs_the_matrix_end_to_end_and_its_exit_status_follows_the_result(monkeypatch, capsys, tmp_path, make_settings):
    import functools

    real = write_matrix.run_matrix

    def partial(fake, started):
        return functools.partial(
            real, settings=make_settings(destructive=True), transport=fake.mock.transport,
            directory=tmp_path / "runs", pace=0, lookup_wait=0, started=started,
        )

    monkeypatch.setattr(write_matrix, "run_matrix", partial(FakeGorelo(), RUN_START))
    assert write_matrix.main(["--only", "items,uptime", "--with-items"]) == 0
    out = capsys.readouterr().out
    assert f"write matrix {RUN}: manifest " in out and "areas: items, uptime" in out
    assert "area items: pass, 5 requests" in out and out.rstrip().endswith("result: PASSED")

    # without the flag the area is skipped, which is not a failure
    monkeypatch.setattr(write_matrix, "run_matrix", partial(FakeGorelo(), datetime(2099, 10, 2, 10, 19, 0, tzinfo=timezone.utc)))
    assert write_matrix.main(["--only", "items"]) == 0
    out = capsys.readouterr().out
    assert f"area items: skipped, 0 requests ({write_matrix.ITEMS_SKIP_REASON})" in out and out.rstrip().endswith("result: PASSED")

    broken = FakeGorelo()
    broken.fail("PATCH /v1/items/{itemId}", error_envelope(400, [("070101", "no", "Description")]))
    monkeypatch.setattr(write_matrix, "run_matrix", partial(broken, datetime(2099, 10, 2, 10, 20, 0, tzinfo=timezone.utc)))
    assert write_matrix.main(["--only", "items", "--with-items"]) == 1
    out = capsys.readouterr().out
    assert "area items: FAIL" in out and out.rstrip().endswith("result: FAILED")
    assert broken.leftovers() == []  # the item the failed area had made was cleaned up all the same


def test_the_help_names_the_flags_and_the_areas(capsys):
    with pytest.raises(SystemExit) as stop:
        write_matrix.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())  # argparse wraps its lines
    assert stop.value.code == 0
    for text in ("--skip-email", "--only AREA,...", "--leftovers", "--with-items", "creates, updates and deletes a catalog "
                 "product on the test client", "--with-invoices", "uses up one invoice number", "also under --only invoices",
                 "--with-approved-invoice", "ALONE (needs --only approved_invoice and no other flag)", "approves a $1 invoice on the test client, "
                 "which pushes it to the connected accounting system, then voids it", "also under --only approved_invoice",
                 "clients, contacts, tickets, comments, email, time, items, invoices, approved_invoice, uptime, projects"):
        assert text in out
    assert "may sync" not in out  # the spec does not say that a catalog product syncs to the accounting system


def test_an_interrupt_ends_the_command_with_status_130(monkeypatch, capsys):
    async def stopped(**options):
        raise KeyboardInterrupt

    monkeypatch.setattr(write_matrix, "run_matrix", stopped)
    assert write_matrix.main(["--only", "items"]) == 130
    assert capsys.readouterr().err == "interrupted\n"


def test_the_default_output_goes_through_the_flushing_emit():
    assert write_matrix.run_matrix.__kwdefaults__["echo"] is write_matrix.emit


def test_the_command_line_reports_usage_and_setup_errors_with_status_two(monkeypatch, capsys):
    assert write_matrix.main(["--only", "bogus"]) == 2
    assert "usage: unknown area 'bogus'" in capsys.readouterr().err

    def no_key(*args, **kwargs):
        raise _env.EnvError("GORELO_API_KEY is not set in somewhere")

    monkeypatch.setattr(write_matrix, "live_settings", no_key)
    assert write_matrix.main(["--only", "items"]) == 2
    assert "cannot start: GORELO_API_KEY is not set" in capsys.readouterr().err

    async def incomplete(**options):
        raise write_matrix.SetupError("the server does not offer the tools the matrix calls: create_project")

    monkeypatch.setattr(write_matrix, "run_matrix", incomplete)
    assert write_matrix.main([]) == 2
    assert "cannot start: the server does not offer" in capsys.readouterr().err
    with pytest.raises(SystemExit) as stop:
        write_matrix.main(["--bogus-flag"])
    assert stop.value.code == 2


async def test_the_run_is_paced_and_cleaned_up_at_the_same_pace(run, monkeypatch):
    seen = []
    real = write_matrix.Pacer

    def spy(interval, **kwargs):
        seen.append(interval)
        return real(interval, **kwargs)

    monkeypatch.setattr(write_matrix, "Pacer", spy)
    paced = []
    real_cleanup = write_matrix.run_cleanup

    async def spy_cleanup(manifest, **options):
        paced.append(options["pace"])
        return await real_cleanup(manifest, **options)

    monkeypatch.setattr(write_matrix, "run_cleanup", spy_cleanup)
    assert write_matrix.PACE_SECONDS == 1.0
    report = await run(FakeGorelo(), areas=["items"], pace=0)
    assert report.ok and seen == [0] and paced == [0]
    sig = write_matrix.run_matrix.__kwdefaults__
    assert sig["pace"] == 1.0 and sig["lookup_wait"] == write_matrix.LOOKUP_WAIT and sig["leftovers"] is False
    assert sig["with_items"] is False  # the catalog is never touched unless asked for
    assert sig["with_invoices"] is False  # and no invoice is ever created unless asked for
    assert sig["with_approved_invoice"] is False  # and no invoice is ever approved unless asked for, in a run of its own
    assert sig["sync_interval"] == write_matrix.SYNC_INTERVAL == 5.0 and sig["sync_polls"] == write_matrix.SYNC_POLLS == 24
    assert write_matrix.SYNC_INTERVAL * write_matrix.SYNC_POLLS == 120.0  # polled every 5 s for at most 120 s


def test_without_a_site_config_the_write_matrix_refuses_to_start_and_creates_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GORELO_SITE_CONFIG", str(tmp_path / "missing.toml"))
    assert write_matrix.main(["--only", "tickets"]) == 2
    err = capsys.readouterr().err
    assert "cannot start" in err and "site.example.toml" in err


def test_the_site_values_of_the_write_matrix_come_from_the_config():
    assert (write_matrix.SITE.test_client, write_matrix.SITE.second_client) == (9501, 9502)
    assert (write_matrix.SITE.operator_contact, write_matrix.SITE.operator_email) == (9600, "ops@example.com")
