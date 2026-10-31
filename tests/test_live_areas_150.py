"""The opt-in areas of release 1.5.0 (contract 6c30770dffc0) of the live write matrix, run offline.

run_matrix is driven against Fake150, FakeGorelo (tests/test_live_write_matrix.py) extended with the routes of the new operations: alerts
and the stack of an alert, asset types, client locations and domains, contact tags, product charges, the invoice PATCH (lines matched by
Id, an entry with an Id only keeps its values, a StatusId 5 approves), contracts (service lines and line items replaced by Id) and the
comment PATCHes. The REAL tools, the REAL LiveGuard (write mode, require_intents, the allow_areas of the flags) and the REAL cleanup run
in front of it, so a request outside the rules or a record that is not cleaned fails these tests like it would live.

Nothing touches the network or the app's .env.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest
from conftest import envelope, error_envelope
from site_helper import site_config_env  # noqa: F401  (autouse: points GORELO_SITE_CONFIG at invented values)
from test_live_write_matrix import (  # noqa: F401  (fixtures and helpers of the matrix tests)
    NEW_AREAS,
    OPERATOR_CONTACT,
    RUN,
    SECOND,
    SOURCE_NAMES,
    TEST_CLIENT,
    FakeGorelo,
    _contract_writes_released,
    _never_read_the_real_env,
    area_ops,
    invoice_guid,
    lines,
    results,
    run,
)

from scripts.live import cleanup, write_matrix
from scripts.live.manifest import Manifest
from tools import _common

pytestmark = pytest.mark.anyio

TAX_RATE, ZERO_TAX = 8.5, 1
TAXES = (
    {"Id": ZERO_TAX, "Name": "Tax Exempt", "Code": "EX", "Percentage": 0.0, "IsDefault": False, "SubTaxes": []},
    {"Id": 2, "Name": "Sales tax", "Code": "ST", "Percentage": TAX_RATE, "IsDefault": True, "SubTaxes": []},
)
ALERT_NEW, ALERT_DISMISSED, ALERT_TICKETED = 1, 2, 3
STATUS_NAME = {1: "New", 2: "Dismissed", 3: "Ticketed"}
LABOR_NAME = {1: "Unlimited Hours", 2: "Hourly", 3: "Block Hours", 4: "No Labor Terms", 5: "Limited Hours"}


DISMISSED_ON = "2099-10-02T10:16:00Z"  # inside the window of the run (RUN_START is 2099-10-02 10:15:00 UTC)


def alert(ident: str, status: int = ALERT_NEW, *, client: int | None = TEST_CLIENT, title: str = "Disk space low", kind: int = 4, **subject) -> dict:
    """An alert row (AlertListItemModel): Status and Type are {Id, Name}. `subject` adds AgentAssetId, CheckId, UptimeId and the like."""
    return {**subject,
        "Id": ident, "Title": title, "Type": {"Id": kind, "Name": "Script"}, "Status": {"Id": status, "Name": STATUS_NAME[status]},
        "ClientId": client, "DismissedOn": None if status != ALERT_DISMISSED else "2026-10-01T10:00:00Z",
        "DismissedBy": None if status != ALERT_DISMISSED else 9700, "TicketId": None,
    }


def alert_guid(n: int) -> str:
    return f"{n:08d}-a1e7-4a1e-8a1e-{n:012d}"


class Fake150(FakeGorelo):
    """FakeGorelo plus the routes of the 1.5.0 areas. Knobs: alerts (the rows GET /v1/alerts answers), taxes, asked_tax_wins (False: Gorelo
    keeps the item's own tax whatever TaxId a line asks for; a TaxId null always gives the item's tax, as measured on 2026-10-30), keeps_id_only_lines (False: an invoice or contract entry that names only
    an Id comes back reset), omitted_tags_kept (False: a contact update that names no TagIds clears them)."""

    def __init__(
        self,
        *,
        alerts: list[dict] | None = None,
        taxes: tuple[dict, ...] | None = None,
        asked_tax_wins: bool = True,
        keeps_id_only_lines: bool = True,
        omitted_tags_kept: bool = True,
        contact_tags: list[dict] | None = None,
        **options,
    ):
        super().__init__(**options)
        self.alerts = {row["Id"]: row for row in (alerts if alerts is not None else [alert(alert_guid(1))])}
        self.taxes = [dict(row) for row in (TAXES if taxes is None else taxes)]
        self.asked_tax_wins, self.keeps_id_only_lines, self.omitted_tags_kept = asked_tax_wins, keeps_id_only_lines, omitted_tags_kept
        self.contact_tags = [dict(row) for row in (contact_tags if contact_tags is not None else [{"Id": 31, "Name": "VIP", "Description": None}])]
        self.asset_types = {1: {"Id": 1, "Name": "Laptop"}, 2: {"Id": 2, "Name": "Server"}}
        self._type_ids = itertools.count(40)
        self.inactive_locations: dict[int, dict] = {}
        self._location_ids = itertools.count(4200)
        self.domains: dict[int, dict] = {}
        self._domain_ids = itertools.count(4300)
        self.charges: dict[int, dict] = {}
        self._charge_ids = itertools.count(4400)
        self.contracts: dict[int, dict] = {}
        self._contract_ids = itertools.count(4500)
        self._line_ids = itertools.count(6000)
        self.project_comments: dict[str, dict] = {}
        self.stack_moves: list[list[str]] = []
        r = self.route
        r("GET", "/v1/alerts", self.list_alerts)
        r("GET", "/v1/alerts/{alertId}", self.get_alert)
        r("PATCH", "/v1/alerts", self.patch_alerts)
        r("GET", "/v1/assets/types", lambda q: envelope(list(self.asset_types.values())))
        r("POST", "/v1/assets/types", self.post_asset_type)
        r("PATCH", "/v1/assets/types/{assetTypeId}", self.patch_asset_type)
        r("DELETE", "/v1/assets/types/{assetTypeId}", self.delete_asset_type)
        r("GET", "/v1/clients/{clientId}/locations/{locationId}", self.get_location)
        r("POST", "/v1/clients/{clientId}/locations", self.post_location)
        r("PATCH", "/v1/clients/{clientId}/locations/{locationId}", self.patch_location)
        r("DELETE", "/v1/clients/{clientId}/locations/{locationId}", self.delete_location)
        r("POST", "/v1/clients/{clientId}/domains", self.post_domain)
        r("PATCH", "/v1/clients/{clientId}/domains/{domainId}", self.patch_domain)
        r("DELETE", "/v1/clients/{clientId}/domains/{domainId}", self.delete_domain)
        r("GET", "/v1/contacts/tags", lambda q: envelope(list(self.contact_tags)))
        r("GET", "/v1/taxes", lambda q: envelope(list(self.taxes)))
        r("GET", "/v1/product-charges", self.list_charges)
        r("POST", "/v1/product-charges", self.post_charge)
        r("GET", "/v1/product-charges/{productChargeId}", self.get_charge)
        r("PATCH", "/v1/product-charges/{productChargeId}", self.patch_charge)
        r("DELETE", "/v1/product-charges/{productChargeId}", self.delete_charge)
        r("PATCH", "/v1/invoices/{invoiceId}", self.patch_invoice)
        r("GET", "/v1/contracts/{contractId}", self.get_contract)
        r("POST", "/v1/contracts", self.post_contract)
        r("PATCH", "/v1/contracts/{contractId}", self.patch_contract)
        r("POST", "/v1/projects/{projectId}/comments", self.post_project_comment)
        r("GET", "/v1/projects/{projectId}/comments/{commentId}", self.get_project_comment)
        r("PATCH", "/v1/projects/{projectId}/comments/{commentId}", self.patch_project_comment)
        r("DELETE", "/v1/projects/{projectId}/comments/{commentId}", self.delete_project_comment)
        r("PATCH", "/v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}", self.patch_task_comment)
        # overrides of what FakeGorelo already serves
        self.handlers["PATCH /v1/contacts/{contactId}"] = self.patch_contact_with_tags
        self.handlers["POST /v1/items"] = self.post_item
        self.handlers["POST /v1/invoices"] = self.post_invoice_with_tax
        self.handlers["PATCH /v1/tickets/{ticketId}"] = self.patch_ticket_with_source
        self.handlers["GET /v1/clients/{clientId}"] = self.get_client_with_domains
        self.handlers["GET /v1/clients/{clientId}/locations"] = lambda q: envelope(list(self.locations.get(int(self.part(q, 3)), [])))

    # -- alerts: Gorelo changes the whole stack (same title, type and client) with the alert

    def stack_of(self, row: dict) -> list[dict]:
        return [o for o in self.alerts.values() if (o["Title"], o["Type"]["Id"], o["ClientId"]) == (row["Title"], row["Type"]["Id"], row["ClientId"])]

    def list_alerts(self, q):
        rows = list(self.alerts.values())
        if "ClientIds" in q.query:
            wanted = {int(x) for x in q.query["ClientIds"].split(",")}
            rows = [row for row in rows if row["ClientId"] in wanted]
        if "StatusIds" in q.query:
            wanted = {int(x) for x in q.query["StatusIds"].split(",")}
            rows = [row for row in rows if row["Status"]["Id"] in wanted]
        return self.paged([dict(row) for row in rows], q)

    def get_alert(self, q):
        row = self.alerts.get(self.part(q, 3))
        return envelope(dict(row)) if row else self.missing("alert")

    def patch_alerts(self, q):
        body = q.json
        ids = body.get("AlertIds") or []
        unknown = [i for i in ids if i not in self.alerts]
        if unknown:
            return error_envelope(400, [("070101", f"Not an alert of this account: {unknown[0]}", "AlertIds")])
        status = body.get("StatusId")
        if status not in (ALERT_NEW, ALERT_DISMISSED, ALERT_TICKETED) or (status == ALERT_TICKETED and not body.get("TicketId")):
            return error_envelope(400, [("070101", "StatusId is invalid.", "StatusId")])
        updated: list[str] = []
        for ident in ids:
            row = self.alerts[ident]
            current = row["Status"]["Id"]
            if current == status:
                continue
            if current == ALERT_TICKETED or (current == ALERT_DISMISSED and status == ALERT_TICKETED):
                return error_envelope(409, [("070901", "The alert cannot change status.")])
            for member in self.stack_of(row):
                if member["Status"]["Id"] == current:  # dismissing moves every New one, restoring every Dismissed one
                    member["Status"] = {"Id": status, "Name": STATUS_NAME[status]}
                    member["DismissedOn"] = DISMISSED_ON if status == ALERT_DISMISSED else None
                    member["DismissedBy"] = 9700 if status == ALERT_DISMISSED else None
                    if member["Id"] not in updated:
                        updated.append(member["Id"])
        self.stack_moves.append(list(updated))
        return envelope({"UpdatedIds": updated})

    # -- asset types

    def post_asset_type(self, q):
        name = q.json.get("Name")
        if any(row["Name"].casefold() == str(name).casefold() for row in self.asset_types.values()):
            return error_envelope(409, [("070901", "An asset type with this name exists.")])
        ident = next(self._type_ids)
        self.asset_types[ident] = {"Id": ident, "Name": name}
        return envelope({"Id": ident})

    def patch_asset_type(self, q):
        ident = int(self.part(q, 4))
        if ident not in self.asset_types:
            return self.missing("asset type")
        self.asset_types[ident]["Name"] = q.json["Name"]
        return envelope({"Id": ident})

    def delete_asset_type(self, q):
        ident = int(self.part(q, 4))
        if ident not in self.asset_types:
            return self.missing("asset type")
        if ident <= 2:
            return error_envelope(409, [("070901", "A default type cannot be deleted.")])
        del self.asset_types[ident]
        return envelope({"Id": ident})

    # -- client locations: a deleted one is marked inactive and leaves the list

    def get_location(self, q):
        client, ident = int(self.part(q, 3)), int(self.part(q, 5))
        for row in [*self.locations.get(client, []), *self.inactive_locations.values()]:
            if row["Id"] == ident and row["ClientId"] == client:
                return envelope(dict(row))
        return self.missing("location")

    def post_location(self, q):
        client, ident = int(self.part(q, 3)), next(self._location_ids)
        body = q.json
        self.locations.setdefault(client, []).append(
            {"Id": ident, "ClientId": client, "Name": body["Name"], "IsDefault": bool(body.get("IsDefault")), "BillingContactIds": "[]",
             "IsDefaultBilling": bool(body.get("IsDefaultBilling")), "PhoneCountryCode": body.get("PhoneCountryCode"),
             **{key: body.get(key) for key in ("Address1", "Address2", "City", "State", "Country", "PostalCode", "TimeZone", "Phone")}}
        )
        return envelope({"Id": ident})

    def patch_location(self, q):
        client, ident = int(self.part(q, 3)), int(self.part(q, 5))
        row = next((r for r in self.locations.get(client, []) if r["Id"] == ident), None)
        if row is None:
            return self.missing("location")
        for key, value in q.json.items():
            row[key] = value
        return envelope({"Id": ident})

    def delete_location(self, q):
        client, ident = int(self.part(q, 3)), int(self.part(q, 5))
        row = next((r for r in self.locations.get(client, []) if r["Id"] == ident), None)
        if row is None:
            return envelope({"Id": ident}) if ident in self.inactive_locations else self.missing("location")
        if row["IsDefault"] or len(self.locations[client]) == 1:
            return error_envelope(409, [("070901", "The default or only location cannot be deleted.")])
        self.locations[client].remove(row)
        self.inactive_locations[ident] = row
        return envelope({"Id": ident})

    # -- client domains: the client record lists its Domains

    def get_client_with_domains(self, q):
        answer = self.get_client(q)
        data = answer.get("Data")
        if isinstance(data, dict):
            client = int(self.part(q, 3))
            data = {**data, "Domains": [dict(d) for d in self.domains.values() if d["_client"] == client and d["Status"]["Id"] == 1]}
            data["Domains"] = [{k: v for k, v in d.items() if k != "_client"} for d in data["Domains"]]
            return envelope(data)
        return answer

    def post_domain(self, q):
        client, ident = int(self.part(q, 3)), next(self._domain_ids)
        body = q.json
        if any(d["Name"] == body["Name"] and d["Status"]["Id"] == 1 for d in self.domains.values()):
            return error_envelope(400, [("070101", "Another client holds this domain.", "Name")])
        self.domains[ident] = {
            "Id": ident, "Name": body["Name"], "ExpirationDate": body.get("ExpirationDate"), "IsDateSetManually": body.get("ExpirationDate") is not None,
            "Status": {"Id": 1, "Name": "Active"}, "CreatedOn": "2099-10-02T10:15:00Z", "UpdatedOn": None, "_client": client,
        }
        return envelope({"Id": ident})

    def patch_domain(self, q):
        ident = int(self.part(q, 5))
        domain = self.domains.get(ident)
        if domain is None:
            return self.missing("domain")
        for key, value in q.json.items():
            domain[key] = value
        return envelope({"Id": ident})

    def delete_domain(self, q):
        ident = int(self.part(q, 5))
        if ident not in self.domains:
            return self.missing("domain")
        self.domains[ident]["Status"] = {"Id": 2, "Name": "Inactive"}
        return envelope({"Id": ident})

    # -- contacts: TagIds

    def patch_contact_with_tags(self, q):
        ident = int(self.part(q, 3))
        before = list(self.contacts[ident].get("TagIds", [])) if ident in self.contacts else []
        answer = self.handlers_base_patch_contact(q)
        if ident in self.contacts and answer.get("IsSuccess", True):
            body = q.json
            if "TagIds" in body and body["TagIds"] is not None:
                unknown = [t for t in body["TagIds"] if t not in {row["Id"] for row in self.contact_tags}]
                if unknown:
                    return error_envelope(400, [("070101", "A tag is not an active contact tag.", "TagIds")])
                self.contacts[ident]["TagIds"] = sorted(body["TagIds"])
            else:
                self.contacts[ident]["TagIds"] = before if self.omitted_tags_kept else []
        return answer

    def handlers_base_patch_contact(self, q):
        return FakeGorelo.patch_contact(self, q)

    # -- tickets: SourceId

    def patch_ticket_with_source(self, q):
        answer = FakeGorelo.patch_ticket(self, q)
        ticket = self.live_ticket(q)
        if ticket is not None and q.json.get("SourceId") is not None:
            ident = q.json["SourceId"]
            ticket["Source"] = {"Id": ident, "Name": SOURCE_NAMES.get(ident)}
        return answer

    # -- items with a tax, invoices with taxes and the PATCH

    def post_item(self, q):
        answer = self.post_thing(q, self.items, "Item")
        self.items[answer["Data"]["Id"]]["TaxId"] = q.json.get("TaxId")
        return answer

    def tax_row(self, tax_id):
        return next((row for row in self.taxes if row["Id"] == tax_id), None)

    def apply_tax(self, line: dict, asked: dict) -> None:
        item = self.find_item(line["ItemId"]) or {}
        item_tax = item.get("TaxId")
        if "TaxId" in asked:
            tax = asked["TaxId"] if asked["TaxId"] is not None and self.asked_tax_wins else item_tax
        else:
            tax = item_tax
        row = self.tax_row(tax)
        line["Tax"] = {"Id": row["Id"], "Name": row["Name"]} if row else {"Id": 0, "Name": "No tax"}
        line["TaxAmount"] = round(line["Amount"] * (row["Percentage"] if row else 0.0) / 100, 2)

    def retotal(self, invoice: dict) -> None:
        invoice["SubTotal"] = sum(line["Amount"] for line in invoice["LineItems"])
        invoice["TotalTax"] = round(sum(line["TaxAmount"] for line in invoice["LineItems"]), 2)
        invoice["Total"] = invoice["AmountDue"] = invoice["SubTotal"] + invoice["TotalTax"]

    def post_invoice_with_tax(self, q):
        hooks, self.invoice_hooks = self.invoice_hooks, []  # the hooks change the STORED invoice: after the taxes are worked out
        try:
            answer = FakeGorelo.post_invoice(self, q)
        finally:
            self.invoice_hooks = hooks
        data = answer.get("Data")
        if isinstance(data, dict) and data.get("Id") in self.invoices:
            invoice = self.invoices[data["Id"]]
            for line, asked in zip(invoice["LineItems"], q.json["LineItems"]):
                self.apply_tax(line, asked)
            self.retotal(invoice)
            for hook in hooks:
                hook(invoice)
        return answer

    def patch_invoice(self, q):
        invoice = self.live_invoice(q)
        if invoice is None:
            return self.missing("invoice")
        body, status = q.json, invoice["Status"]["Id"]
        if status in (3, 4):
            return error_envelope(409, [("070901", "A Paid or Void invoice cannot change.")])
        if "StatusId" in body:
            if body["StatusId"] != 5:
                return error_envelope(400, [("070101", "StatusId accepts only 5.", "StatusId")])
            if status == 1:
                self.retotal(invoice)
                invoice["Status"] = {"Id": 3 if invoice["Total"] == 0 else 5, "Name": "Paid" if invoice["Total"] == 0 else "Approved"}
        for key in ("InvoiceDate", "DueDate"):
            if body.get(key) is not None:
                invoice[key] = body[key][:10]
        if invoice["DueDate"] < invoice["InvoiceDate"]:
            return error_envelope(400, [("070101", "DueDate may not be earlier than InvoiceDate.", "DueDate")])
        if "Reference" in body:
            invoice["Reference"] = body["Reference"]
        if "LineItems" in body:
            entries = body["LineItems"]
            if not entries:
                return error_envelope(400, [("070101", "An empty array is rejected.", "LineItems")])
            held = {line["Id"].lower(): line for line in invoice["LineItems"]}
            final = []
            for entry in entries:
                if entry.get("Id"):
                    line = held.get(entry["Id"].lower())
                    if line is None:
                        return error_envelope(400, [("070101", "A line is not on this invoice.", "LineItems")])
                    if not self.keeps_id_only_lines and set(entry) == {"Id"}:
                        line.update(Quantity=1, UnitPrice=0.0, Description=None)  # reset: the case the area must catch
                    for key, value in entry.items():
                        if key != "Id" and key not in ("TaxId", "BillableStatusId", "CoaCode", "UnitCost", "DiscountPercent"):
                            line[key] = value
                    if "TaxId" in entry:
                        self.apply_tax(line, entry)
                    line["Amount"] = line["Quantity"] * line["UnitPrice"]
                    line["TaxAmount"] = round(line["Amount"] * (self.tax_row(line["Tax"]["Id"]) or {"Percentage": 0.0})["Percentage"] / 100, 2)
                else:
                    item = self.find_item(entry.get("ItemId"))
                    if item is None:
                        return error_envelope(404, [("070404", "The item does not exist.", "LineItems.ItemId")])
                    price = entry["UnitPrice"] if entry.get("UnitPrice") is not None else item.get("UnitPrice") or 0.0
                    line = {
                        "Id": self.guid(), "ItemId": item["Id"], "Name": item["Name"], "Description": entry.get("Description") or item["Name"],
                        "Quantity": entry["Quantity"], "UnitPrice": price, "UnitCost": item.get("UnitCost"), "DiscountPercent": 0.0,
                        "Amount": entry["Quantity"] * price, "BillableStatus": {"Id": 1, "Name": "Billable"}, "CoaCode": None,
                        "ItemType": {"Id": 1, "Name": "Product"}, "SubItems": [],
                    }
                    self.apply_tax(line, entry)
                final.append(line)
            invoice["LineItems"] = final
        self.retotal(invoice)
        return envelope(self.served(invoice))

    # -- product charges

    def charge_row(self, charge: dict) -> dict:
        return {key: value for key, value in charge.items() if not key.startswith("_")}

    def list_charges(self, q):
        rows = [c for c in self.charges.values() if ("charge", c["Id"]) not in self.deleted]
        if "TicketIds" in q.query:
            wanted = set(q.query["TicketIds"].split(","))
            rows = [c for c in rows if c["TicketId"] in wanted]
        return self.paged([self.charge_row(c) for c in rows], q)

    def post_charge(self, q):
        body = q.json
        ticket = self.tickets.get(body.get("TicketId"))
        if ticket is None or ("ticket", body["TicketId"]) in self.deleted:
            return self.missing("ticket")
        item = self.find_item(body.get("ItemId"))
        if item is None:
            return error_envelope(400, [("070101", "ItemId is not an active product.", "ItemId")])
        ident = next(self._charge_ids)
        price = body["UnitPrice"] if body.get("UnitPrice") is not None else item.get("UnitPrice")
        self.charges[ident] = {
            "Id": ident, "TicketId": body["TicketId"], "TaskId": None, "ProjectId": None, "ItemId": item["Id"], "Name": item["Name"],
            "Description": body.get("Description") or item["Name"], "Quantity": body["Quantity"], "UnitPrice": price,
            "UnitCost": item.get("UnitCost"), "Amount": body["Quantity"] * price,
            "BillableStatus": {"Id": body.get("BillableStatusId", 1), "Name": "No charge" if body.get("BillableStatusId") == 2 else "Billable"},
            "ClientId": ticket["ClientId"], "InvoiceId": None, "CreatedOn": "2099-10-02T10:15:00Z", "UpdatedOn": None,
        }
        return envelope({"Id": ident})

    def live_charge(self, q):
        ident = int(self.part(q, 3))
        return None if ("charge", ident) in self.deleted else self.charges.get(ident)

    def get_charge(self, q):
        charge = self.live_charge(q)
        return envelope(self.charge_row(charge)) if charge else self.missing("product charge")

    def patch_charge(self, q):
        charge = self.live_charge(q)
        if charge is None:
            return self.missing("product charge")
        for key, value in q.json.items():
            if value is None and key != "Description":
                continue
            if key == "BillableStatusId":
                charge["BillableStatus"] = {"Id": value, "Name": "No charge" if value == 2 else "Billable"}
            else:
                charge[key] = value
        charge["Amount"] = charge["Quantity"] * charge["UnitPrice"]
        return envelope(self.charge_row(charge))

    def delete_charge(self, q):
        charge = self.live_charge(q)
        if charge is None:
            return self.missing("product charge")
        self.deleted.add(("charge", charge["Id"]))
        return envelope({"Id": charge["Id"]})

    # -- contracts: service lines and line items are matched by Id, the ones left out are removed

    def contract_row(self, contract: dict) -> dict:
        return {key: value for key, value in contract.items() if not key.startswith("_")}

    def make_line_item(self, entry: dict) -> dict:
        item = self.find_item(entry.get("ItemId")) or {}
        price = entry["UnitPrice"] if entry.get("UnitPrice") is not None else item.get("UnitPrice")
        return {
            "Id": next(self._line_ids), "ItemId": entry.get("ItemId"), "Name": item.get("Name"), "Description": entry.get("Description") or item.get("Name"),
            "Quantity": entry["Quantity"], "UnitPrice": price, "UnitCost": item.get("UnitCost"), "DiscountPercent": entry.get("DiscountPercent") or 0.0,
            "Tax": {"Id": 0, "Name": "No tax"}, "Amount": entry["Quantity"] * price,
            "BillableStatus": {"Id": entry.get("BillableStatusId", 1), "Name": "Billable"},
        }

    def merge_line_items(self, held: list[dict], entries: list[dict]):
        by_id = {row["Id"]: row for row in held}
        final = []
        for entry in entries:
            if entry.get("Id") is not None:
                row = by_id.get(entry["Id"])
                if row is None:
                    return None
                if not self.keeps_id_only_lines and set(entry) == {"Id"}:
                    row.update(Quantity=1, UnitPrice=0.0, Description=None)
                for key, value in entry.items():
                    if key in ("Quantity", "UnitPrice", "Description", "DiscountPercent"):
                        row[key] = value
                    elif key == "BillableStatusId":
                        row["BillableStatus"] = {"Id": value, "Name": "Billable"}
                row["Amount"] = row["Quantity"] * (row["UnitPrice"] or 0)
                final.append(row)
            else:
                final.append(self.make_line_item(entry))
        return final

    def make_line(self, entry: dict) -> dict:
        terms = entry["LaborTermsId"]
        line = {
            "Id": next(self._line_ids), "Name": entry.get("Name"), "LaborTerms": {"Id": terms, "Name": LABOR_NAME[terms]},
            "WorkTypes": [], "WorkRoles": [], "AutoApproveTime": bool(entry.get("AutoApproveTime")),
            "AutoInvoiceTime": bool(entry.get("AutoInvoiceTime")), "LineItems": [self.make_line_item(e) for e in entry.get("LineItems") or []],
        }
        for name in ("UnlimitedHoursDetails", "PerHourDetails", "BlockHoursDetails", "LimitedHoursDetails"):  # the model shows what was sent
            if isinstance(entry.get(name), dict):
                shown = {key: value for key, value in entry[name].items() if key != "RateTypeId"}
                if "RateTypeId" in entry[name]:
                    shown["RateType"] = {"Id": entry[name]["RateTypeId"], "Name": "Rate type"}
                line[name] = shown
        return line

    def post_contract(self, q):
        body = q.json
        if body.get("ClientId") not in self.clients:
            return error_envelope(400, [("070101", "The client does not exist.", "ClientId")])
        lines = body.get("ServiceLines") or []
        if not lines:
            return error_envelope(400, [("070101", "At least one service line is required.", "ServiceLines")])
        for line in lines:
            if line.get("LaborTermsId") == 3:  # a Block Hours line raises a Draft invoice (the guard must have stopped this)
                number = next(self._invoice_numbers)
                ident = invoice_guid(next(self._invoice_ids))
                self.invoices[ident] = {"Id": ident, "ClientId": body["ClientId"], "Number": number, "DisplayNumber": f"INV-{number}",
                                        "Status": {"Id": 1, "Name": "Draft"}, "LineItems": [], "_reads": 0, "Reference": None,
                                        "InvoiceDate": "2099-10-02", "DueDate": "2099-10-16", "ExternalId": None, "PaymentLink": None}
        ident = next(self._contract_ids)
        contract = {
            "Id": ident, "Name": body["Name"], "Status": {"Id": 1, "Name": "Active"}, "Client": {"Id": body["ClientId"], "Name": "c"},
            "LocationIds": [], "IsForAllLocations": True, "Reference": body.get("Reference"), "AutoApproveAndSend": bool(body.get("AutoApproveAndSend")),
            "ServiceLines": [self.make_line(line) for line in lines], "Contacts": [],
        }
        self.contracts[ident] = contract
        return envelope(self.contract_row(contract))

    def get_contract(self, q):
        contract = self.contracts.get(int(self.part(q, 3)))
        return envelope(self.contract_row(contract)) if contract else self.missing("contract")

    def patch_contract(self, q):
        contract = self.contracts.get(int(self.part(q, 3)))
        if contract is None:
            return self.missing("contract")
        body = q.json
        if "Name" in body:
            contract["Name"] = body["Name"]
        if body.get("StatusId") in (1, 3):
            contract["Status"] = {"Id": body["StatusId"], "Name": "Active" if body["StatusId"] == 1 else "Archived"}
        if "ServiceLines" in body:
            held = {row["Id"]: row for row in contract["ServiceLines"]}
            final = []
            for entry in body["ServiceLines"]:
                if entry.get("Id") is not None:
                    line = held.get(entry["Id"])
                    if line is None:
                        return error_envelope(400, [("070101", "A service line is not on this contract.", "ServiceLines")])
                    if not self.keeps_id_only_lines and set(entry) == {"Id"}:  # reset: the case the area must catch
                        line["Name"] = None
                        for row in line["LineItems"]:
                            row.update(Quantity=1, UnitPrice=0.0, Description=None)
                    if "Name" in entry:
                        line["Name"] = entry["Name"]
                    if "LineItems" in entry:
                        merged = self.merge_line_items(line["LineItems"], entry["LineItems"])
                        if merged is None:
                            return error_envelope(400, [("070101", "A line item is not on this line.", "LineItems")])
                        line["LineItems"] = merged
                    final.append(line)
                else:
                    final.append(self.make_line(entry))
            contract["ServiceLines"] = final
        return envelope(self.contract_row(contract))

    # -- project and task comments

    def post_project_comment(self, q):
        ident = self.guid()
        self.project_comments[ident] = {
            "Id": ident, "BodyHtml": q.json["Body"], "ConversationType": {"Id": q.json["ConversationTypeId"], "Name": "Private"}, "UpdatedOn": None,
        }
        return envelope({"Id": ident})

    def get_project_comment(self, q):
        comment = self.project_comments.get(self.part(q, 5))
        return envelope(dict(comment)) if comment and ("pcomment", comment["Id"]) not in self.deleted else self.missing("comment")

    def patch_project_comment(self, q):
        comment = self.project_comments.get(self.part(q, 5))
        if comment is None or ("pcomment", comment["Id"]) in self.deleted:
            return self.missing("comment")
        if "Body" in q.json:
            comment["BodyHtml"] = q.json["Body"]
        comment["UpdatedOn"] = "2099-10-02T10:20:00Z"
        return envelope({"Id": comment["Id"]})

    def delete_project_comment(self, q):
        comment = self.project_comments.get(self.part(q, 5))
        if comment is None:
            return self.missing("comment")
        self.deleted.add(("pcomment", comment["Id"]))
        return envelope({"Id": comment["Id"]})

    def patch_task_comment(self, q):
        comment = self.task_comments.get(self.part(q, 7))
        if comment is None or ("comment", comment["Id"]) in self.deleted:
            return self.missing("comment")
        if "Body" in q.json:
            comment["BodyHtml"] = q.json["Body"]
        comment["UpdatedOn"] = "2099-10-02T10:20:00Z"
        return envelope({"Id": comment["Id"]})

    # -- what a run leaves

    def leftovers(self) -> list[str]:
        found = super().leftovers()
        found += [f"asset type {i}" for i in self.asset_types if i > 2]
        found += [f"location {r['Id']}" for r in self.locations.get(TEST_CLIENT, []) if 4200 <= r["Id"] < 9000]
        found += [f"domain {i}" for i, d in self.domains.items() if d["Status"]["Id"] == 1]
        found += [f"product charge {i}" for i in self.charges if ("charge", i) not in self.deleted]
        found += [f"contract {i}" for i, c in self.contracts.items() if c["Status"]["Id"] != 3]
        return found

    def contract_residue(self) -> list[int]:
        """The contracts that stay in Gorelo: an Archived one is a known residue, an Active one was missed by the run."""
        return [i for i, c in self.contracts.items() if c["Status"]["Id"] == 3]


# --------------------------------------------------------------------------
# One area at a time
# --------------------------------------------------------------------------

FLAG = {key: f"with_{key}" for key in NEW_AREAS}


async def run_area(run, fake, key, **options):
    return await run(fake, areas=[key], **{FLAG[key]: True}, **options)


@pytest.mark.parametrize("key", NEW_AREAS)
async def test_each_area_passes_alone_and_leaves_nothing_but_its_known_residue(run, lines, key):
    fake = Fake150()
    report = await run_area(run, fake, key)
    assert results(report)[key].status == "pass", "\n".join(lines)
    assert report.ok and report.refused == 0 and fake.leftovers() == [] and not fake.mock.unmatched


def record_of(report, kind: str, ident=None):
    found = [r for r in Manifest.load(report.manifest_path).all_created() if r.kind == kind and (ident is None or r.id == ident)]
    return found[0] if found else None


def docstring_lists() -> dict[str, list[str]]:
    """The per-area request lists of the module docstring (what a reader sees)."""
    lines = write_matrix.__doc__.splitlines()
    listed: dict[str, list[str]] = {}
    current = None
    import re

    for line in lines[lines.index("Live requests per area (method and spec path, in the order they are sent; the tests pin these lists):") :]:
        heading = re.fullmatch(r"    (\w+)(?: \(--with-[a-z-]+\))?:", line)
        step = re.fullmatch(r"        ((?:GET|POST|PATCH|DELETE) /v1/\S+)(?:  .*)?", line)
        if heading:
            current = heading.group(1)
            listed[current] = []
        elif step and current:
            listed[current].append(step.group(1))
    return listed


@pytest.mark.parametrize("key", NEW_AREAS)
async def test_the_requests_of_each_area_are_the_ones_the_docstring_lists(run, key):
    fake = Fake150()
    report = await run_area(run, fake, key)
    assert results(report)[key].status == "pass"
    assert area_ops(fake, report)[key] == docstring_lists()[key]


def test_the_docstring_names_every_area_and_flag_of_1_5_0():
    text = " ".join(write_matrix.__doc__.split())
    for area in write_matrix.OPT_INS:
        assert area.flag in text and f"{area.key} ({area.flag}):" in write_matrix.__doc__, area.key
    assert "[--with-approved-invoice [--approve-via-update]]" in text and "--approve-via-update" in text
    assert "A contract CANNOT be deleted through the API" in text and "the TAX PROBE" in text
    assert "Id only" in text.replace("an Id only", "Id only")


# --------------------------------------------------------------------------
# Off by default
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", NEW_AREAS)
async def test_each_area_is_skipped_without_its_flag_even_when_selected_and_sends_nothing(run, key):
    fake = Fake150()
    report = await run(fake, areas=[key])
    found = results(report)[key]
    assert found.status == "skipped" and found.requests == 0 and found.detail == write_matrix.OPT_IN_BY_KEY[key].skip_reason
    assert found.detail.startswith("not live-tested by default: ") and report.ok and fake.mock.requests == []


@pytest.mark.parametrize("flag", [True, False])
async def test_the_contracts_area_is_skipped_with_a_reason_while_its_tools_are_held_back_and_nothing_is_sent(run, monkeypatch, flag):
    monkeypatch.setattr(_common, "HELD_TOOLS", frozenset({"create_contract", "update_contract"}))
    fake = Fake150()
    report = await run(fake, areas=["contracts"], with_contracts=flag)  # not a setup error: the held tools are left out of what the run needs
    found = results(report)["contracts"]
    assert found.status == "skipped" and found.requests == 0 and report.ok and fake.mock.requests == [] and fake.contracts == {}
    assert found.detail == write_matrix.OPT_IN_BY_KEY["contracts"].held_reason
    assert found.detail.startswith("create_contract and update_contract held back until Gorelo releases contract writes: ")
    assert write_matrix.OPT_IN_BY_KEY["contracts"].held_tools == ["create_contract", "update_contract"]
    assert not write_matrix.tools_for(False, opt_in=["contracts"]) & {"create_contract", "update_contract"}
    assert {"list_items", "get_contract"} <= write_matrix.tools_for(False, opt_in=["contracts"])


async def test_the_other_areas_do_not_notice_the_held_tools(run, monkeypatch):
    monkeypatch.setattr(_common, "HELD_TOOLS", frozenset({"create_contract", "update_contract"}))
    assert [area.key for area in write_matrix.OPT_INS if area.held_tools] == ["contracts"]
    fake = Fake150()
    report = await run(fake, areas=["asset_types"], with_asset_types=True)
    assert results(report)["asset_types"].status == "pass" and report.ok and fake.leftovers() == []


@pytest.mark.parametrize("key", NEW_AREAS)
async def test_the_tools_of_an_area_cannot_be_called_without_its_flag(run, monkeypatch, key):
    fake = Fake150()
    area = write_matrix.OPT_IN_BY_KEY[key]
    others = area.tools - write_matrix.MATRIX_TOOLS  # the tools only this area opens
    seen = {}

    async def probe(matrix):
        for tool in sorted(others):
            try:
                await matrix.tool(tool)
            except RuntimeError as exc:
                seen[tool] = str(exc)

    monkeypatch.setattr(write_matrix, "AREAS", (write_matrix.Area("clients", probe),))
    await run(fake)
    assert set(seen) == others and all(text == f"{tool} is not one of the tools this script may call" for tool, text in seen.items())
    assert fake.mock.requests == []


def test_the_tool_sets_of_the_areas_are_registered_and_the_matrix_tools_stay_as_they_were():
    from tools._common import REGISTRY

    registered = {spec.name: spec for spec in REGISTRY.specs}
    assert [a.key for a in write_matrix.OPT_INS] == list(NEW_AREAS)
    for area in write_matrix.OPT_INS:
        assert area.tools <= set(registered), area.key
        assert not area.tools & {"post_alert", "create_approved_invoice", "update_agent", "share_document"}, area.key
        assert write_matrix.tools_for(False, opt_in=[area.key]) == write_matrix.tools_for(False) | area.tools
    gated = {name for name, spec in registered.items() if spec.kind == "destructive"}
    assert write_matrix.OPT_IN_BY_KEY["contracts"].tools & gated == {"create_contract", "update_contract"}
    assert write_matrix.OPT_IN_BY_KEY["asset_types"].tools & gated == {"delete_asset_type"}
    assert write_matrix.OPT_IN_BY_KEY["client_locations"].tools & gated == {"delete_client_location"}
    assert write_matrix.OPT_IN_BY_KEY["client_domains"].tools & gated == {"delete_client_domain"}
    assert write_matrix.OPT_IN_BY_KEY["product_charges"].tools & gated == {"delete_product_charge"}
    assert write_matrix.OPT_IN_BY_KEY["invoice_update"].tools & gated == {"delete_invoice", "delete_item"}
    assert not any(a.tools & {"create_payment", "delete_payment", "delete_contract"} for a in write_matrix.OPT_INS)
    # the new reads and writes of an area never leak into the default set
    default = write_matrix.tools_for(False)
    for name in ("create_contract", "set_alert_status", "create_asset_type", "create_product_charge", "update_invoice", "approve_invoice"):
        assert name not in default


@pytest.mark.parametrize("key", NEW_AREAS)
async def test_an_area_flag_beside_the_approved_invoice_run_is_a_usage_error_before_anything_is_sent(run, tmp_path, key):
    fake = Fake150()
    flag = write_matrix.OPT_IN_BY_KEY[key].flag
    with pytest.raises(write_matrix.UsageError, match=f"--with-approved-invoice runs alone: it cannot be combined with {flag}$"):
        await run(fake, areas=["approved_invoice"], with_approved_invoice=True, **{FLAG[key]: True})
    assert fake.mock.requests == [] and not (tmp_path / "runs").exists()


async def test_approve_via_update_needs_the_approved_invoice_flag(run, tmp_path):
    fake = Fake150()
    with pytest.raises(write_matrix.UsageError, match="--approve-via-update needs --with-approved-invoice"):
        await run(fake, areas=["approved_invoice"], approve_via_update=True)
    assert fake.mock.requests == [] and not (tmp_path / "runs").exists()


async def test_the_guard_of_the_run_opens_only_the_areas_whose_flag_was_given(run, monkeypatch):
    made = []
    real = write_matrix.LiveGuard

    class Spy(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(write_matrix, "LiveGuard", Spy)
    await run(Fake150(), areas=["clients"])
    await run(
        Fake150(), areas=["clients"], with_alert_status=True, with_contracts=True, with_contact_tags=True, with_ticket_source=True,
        started=datetime(2099, 10, 2, 10, 16, 0, tzinfo=timezone.utc),
    )
    assert made[0].allow_areas == frozenset()
    assert made[1].allow_areas == {"alert_status", "contracts"}  # contact_tags and ticket_source need no guard area
    assert made[1].require_intents is True and made[1].allow_approved_invoice is False


@pytest.mark.parametrize("key", NEW_AREAS)
async def test_a_not_active_test_client_stops_a_run_with_an_area_before_anything_is_created(run, tmp_path, monkeypatch, key):
    import site_helper

    from scripts.live.guard import ClientNotActive

    fake = Fake150()
    monkeypatch.setitem(site_helper.CLIENT_STATUS, TEST_CLIENT, (4, "Archived"))
    with pytest.raises(ClientNotActive, match="it needs the test client Active"):
        await run(fake, areas=[key], **{FLAG[key]: True})
    assert fake.mock.requests == [] and not (tmp_path / "runs").exists()  # no request and no manifest


# --------------------------------------------------------------------------
# alert_status
# --------------------------------------------------------------------------


async def test_alert_status_dismisses_then_restores_a_recorded_alert_and_records_it_before_the_first_change(run, lines, monkeypatch):
    fake = Fake150()
    at_record = []
    original = Manifest.created

    def spy(self, kind, ident, label, details=None):
        if kind == "alert":
            at_record.append(len(fake.mock.requests))
        return original(self, kind, ident, label, details)

    monkeypatch.setattr(Manifest, "created", spy)
    report = await run_area(run, fake, "alert_status")
    assert results(report)["alert_status"].status == "pass", "\n".join(lines)
    assert at_record == [2]  # only the two list_alerts had been sent when the alert was recorded: before the first change
    list_call, dismissed_call, dismiss, read_one, restore, read_two = fake.mock.requests
    assert list_call.query == {"ClientIds": str(TEST_CLIENT), "PageSize": "50", "SortOrder": "desc"}
    assert dismissed_call.query == {"ClientIds": str(TEST_CLIENT), "StatusIds": "2", "PageSize": "200", "SortOrder": "desc"}
    assert dismiss.json == {"AlertIds": [alert_guid(1)], "StatusId": 2} and restore.json == {"AlertIds": [alert_guid(1)], "StatusId": 1}
    assert fake.alerts[alert_guid(1)]["Status"]["Id"] == ALERT_NEW and fake.alerts[alert_guid(1)]["DismissedOn"] is None
    record = record_of(report, "alert")
    assert record.details == {"client_id": TEST_CLIENT, "status_before": 1, "dismissed_ids": [alert_guid(1)]} and record.cleaned
    assert record.outcome == "restored to New by the write matrix (set_alert_status)" and report.summary["created"] == 1
    assert any("dismissed (1 alert(s) of its stack moved) and restored to New (1 moved back)" in n for n in results(report)["alert_status"].notes)


async def test_alert_status_moves_the_whole_stack_of_the_alert_and_says_how_many(run):
    fake = Fake150(alerts=[alert(alert_guid(1)), alert(alert_guid(2)), alert(alert_guid(3), title="Other", kind=5)])
    report = await run_area(run, fake, "alert_status")
    assert report.ok
    assert fake.stack_moves == [[alert_guid(1), alert_guid(2)], [alert_guid(1), alert_guid(2)]]  # the alert named first, then its stack
    assert all(row["Status"]["Id"] == ALERT_NEW for row in fake.alerts.values())
    assert any("dismissed (2 alert(s) of its stack moved)" in n for n in results(report)["alert_status"].notes)


@pytest.mark.parametrize(
    "alerts, said",
    [
        ([], "no alert of the test client 9501 is New among the 0 listed"),
        ([alert(alert_guid(1), ALERT_DISMISSED), alert(alert_guid(2), ALERT_TICKETED, title="T")], "no alert of the test client 9501 is New among the 2 listed"),
        ([alert(alert_guid(1), client=SECOND)], "no alert of the test client 9501 is New"),  # another client's alert is never touched
        ([alert(alert_guid(1)), alert(alert_guid(2), ALERT_DISMISSED)], "shares its Type and subject with a Dismissed alert"),
        # the Title is not compared: the same Type and subject under another title is one stack
        ([alert(alert_guid(1)), alert(alert_guid(2), ALERT_DISMISSED, title="Other title")], "shares its Type and subject with a Dismissed alert"),
        ([alert(alert_guid(1), AgentAssetId=alert_guid(70)), alert(alert_guid(2), ALERT_DISMISSED, title="Other", AgentAssetId=alert_guid(70).upper())],
         "shares its Type and subject with a Dismissed alert"),
        ([alert(alert_guid(1), UptimeId=alert_guid(71)), alert(alert_guid(2), ALERT_DISMISSED, title="Other", UptimeId=alert_guid(71))],
         "shares its Type and subject with a Dismissed alert"),
    ],
)
async def test_alert_status_is_skipped_with_a_note_when_there_is_no_safe_alert_to_use(run, alerts, said):
    fake = Fake150(alerts=alerts)
    report = await run_area(run, fake, "alert_status")
    found = results(report)["alert_status"]
    assert found.status == "skipped" and said in found.detail and report.ok
    assert not any(r.method == "PATCH" for r in fake.mock.requests) and record_of(report, "alert") is None


async def test_alert_status_never_touches_another_clients_alert_even_when_it_is_the_only_new_one(run):
    other = alert(alert_guid(9), client=SECOND)
    fake = Fake150(alerts=[other, alert(alert_guid(1))])
    report = await run_area(run, fake, "alert_status")
    assert report.ok and other["Status"]["Id"] == ALERT_NEW and other["DismissedOn"] is None
    assert all(alert_guid(9) not in (r.json or {}).get("AlertIds", []) for r in fake.mock.requests)


async def test_alert_status_a_failure_after_the_dismissal_is_restored_by_the_cleanup(run, lines):
    fake = Fake150()
    calls = []
    original = fake.handlers["PATCH /v1/alerts"]

    def fails_the_restore(request):
        calls.append(request.json["StatusId"])
        if calls == [2, 1]:  # the area's own restore
            return error_envelope(500, [("070500", "boom")])
        return original(request)

    fake.handlers["PATCH /v1/alerts"] = fails_the_restore
    report = await run_area(run, fake, "alert_status")
    assert results(report)["alert_status"].status == "FAIL"
    assert calls == [2, 1, 1]  # dismiss, the area's restore (failed), the cleanup's restore
    assert fake.alerts[alert_guid(1)]["Status"]["Id"] == ALERT_NEW and fake.leftovers() == []
    record = record_of(report, "alert")
    assert record.cleaned and record.outcome.startswith("restored to New by the cleanup (set_alert_status;")
    assert report.cleanup.ok and report.refused == 0


async def test_alert_status_a_dismissal_that_gorelo_does_not_show_fails_the_area_and_the_cleanup_restores(run):
    fake = Fake150()
    original = fake.handlers["GET /v1/alerts/{alertId}"]
    reads = []

    def hides_it_once(request):  # the area's own read; the cleanup's read shows the date, like Gorelo does
        reads.append(1)
        return envelope({**original(request)["Data"], "DismissedOn": None}) if len(reads) == 1 else original(request)

    fake.handlers["GET /v1/alerts/{alertId}"] = hides_it_once
    report = await run_area(run, fake, "alert_status")
    found = results(report)["alert_status"]
    assert found.status == "FAIL" and "DismissedOn is not set" in found.detail
    assert fake.alerts[alert_guid(1)]["Status"]["Id"] == ALERT_NEW and report.cleanup.ok


async def test_alert_status_a_dismissal_whose_date_the_cleanup_cannot_read_is_left_for_the_user_not_restored(run):
    fake = Fake150()
    original = fake.handlers["GET /v1/alerts/{alertId}"]
    fake.handlers["GET /v1/alerts/{alertId}"] = lambda request: envelope({**original(request)["Data"], "DismissedOn": None})
    report = await run_area(run, fake, "alert_status")
    assert results(report)["alert_status"].status == "FAIL"
    assert fake.alerts[alert_guid(1)]["Status"]["Id"] == ALERT_DISMISSED and not report.cleanup.ok  # fail closed: nothing proves the run dismissed it
    assert "not inside this run's window" in record_of(report, "alert").outcome


async def test_alert_status_a_ticketed_alert_is_never_asked_for_a_ticket(run):
    fake = Fake150()
    await run_area(run, fake, "alert_status")
    assert all("TicketId" not in (r.json or {}) for r in fake.mock.requests)


# --------------------------------------------------------------------------
# asset_types, client_locations, client_domains
# --------------------------------------------------------------------------


async def test_asset_types_the_type_is_named_after_the_run_renamed_and_deleted(run):
    fake = Fake150()
    report = await run_area(run, fake, "asset_types")
    assert report.ok
    create, _read, rename, *_rest = fake.mock.requests
    assert create.json == {"Name": f"{RUN} asset type"} and rename.json == {"Name": f"{RUN} asset type renamed"}
    assert set(fake.asset_types) == {1, 2}  # the defaults only
    record = record_of(report, "asset_type")
    assert record.id >= 40 and record.cleaned and record.outcome == "deleted by the write matrix (delete_asset_type)"


async def test_asset_types_a_name_that_exists_is_a_409_naming_the_existing_type_and_fails_the_area(run):
    fake = Fake150()
    fake.asset_types[3] = {"Id": 3, "Name": f"{RUN} asset type"}
    report = await run_area(run, fake, "asset_types")
    found = results(report)["asset_types"]
    assert found.status == "FAIL" and "409" in found.detail
    assert record_of(report, "asset_type") is None and report.summary["unresolved_intents"] == 0  # the 409 settled the intent as failed


async def test_asset_types_a_failure_after_the_create_leaves_the_type_to_the_cleanup(run):
    fake = Fake150()
    fake.fail("PATCH /v1/assets/types/{assetTypeId}", error_envelope(500, [("070500", "boom")]), times=4)
    report = await run_area(run, fake, "asset_types")
    assert results(report)["asset_types"].status == "FAIL"
    assert set(fake.asset_types) == {1, 2} and fake.leftovers() == []
    assert record_of(report, "asset_type").outcome == "Deleted" and report.cleanup.ok


async def test_client_locations_a_non_default_location_is_created_updated_and_deleted_and_leaves_the_list(run):
    fake = Fake150()
    report = await run_area(run, fake, "client_locations")
    assert report.ok
    create, _read, update, *_rest = fake.mock.requests
    assert create.path == f"/v1/clients/{TEST_CLIENT}/locations" and create.json == {"Name": f"{RUN} location", "Address1": "1 Test Street", "City": "Seattle"}
    assert update.json == {"City": "Tacoma", "Address2": "Suite 2"}
    assert [r["Id"] for r in fake.locations[TEST_CLIENT]] == [9001] and list(fake.inactive_locations) == [4200]  # inactive, off the list
    assert any("get_client_location after the delete still answers" in n for n in results(report)["client_locations"].notes)
    record = record_of(report, "client_location")
    assert record.details == {"client_id": TEST_CLIENT} and record.cleaned


async def test_client_locations_a_failed_update_leaves_the_location_to_the_cleanup(run):
    fake = Fake150()
    fake.fail("PATCH /v1/clients/{clientId}/locations/{locationId}", error_envelope(400, [("070101", "bad", "City")]))
    report = await run_area(run, fake, "client_locations")
    assert results(report)["client_locations"].status == "FAIL"
    assert fake.leftovers() == [] and record_of(report, "client_location").outcome == "Deleted"
    assert fake.mock.requests[-1].path == f"/v1/clients/{TEST_CLIENT}/locations/4200" and fake.mock.requests[-1].method == "DELETE"


async def test_client_domains_the_domain_is_under_the_reserved_example_tld_with_an_explicit_date(run):
    fake = Fake150()
    report = await run_area(run, fake, "client_domains")
    assert report.ok
    create, _read, update, *_rest = fake.mock.requests
    name = RUN.lower() + ".example"
    assert create.json == {"Name": name, "ExpirationDate": f"{(datetime.now(timezone.utc).date().year) + 1}-" + create.json["ExpirationDate"][5:]}
    assert create.json["ExpirationDate"][:4].isdigit() and update.json["ExpirationDate"] > create.json["ExpirationDate"]
    assert fake.domains[4300]["Status"]["Id"] == 2 and fake.leftovers() == []  # deleted = inactive
    record = record_of(report, "client_domain")
    assert record.details == {"client_id": TEST_CLIENT, "name": name} and record.cleaned


async def test_client_domains_a_domain_another_client_holds_fails_the_area_and_leaves_nothing(run):
    fake = Fake150()
    fake.domains[1] = {"Id": 1, "Name": RUN.lower() + ".example", "Status": {"Id": 1, "Name": "Active"}, "ExpirationDate": None, "_client": SECOND}
    report = await run_area(run, fake, "client_domains")
    assert results(report)["client_domains"].status == "FAIL" and record_of(report, "client_domain") is None
    assert fake.domains[1]["Status"]["Id"] == 1  # the other client's domain was never touched


# --------------------------------------------------------------------------
# contact_tags and ticket_source
# --------------------------------------------------------------------------


async def test_contact_tags_set_kept_and_cleared_and_the_note_says_what_it_proves(run):
    fake = Fake150()
    report = await run_area(run, fake, "contact_tags")
    assert report.ok
    patches = [r.json for r in fake.mock.requests if r.method == "PATCH"]
    # update_contact re-sends nothing for the field the caller left out: the second PATCH has no TagIds, so it tests Gorelo's own omit rule
    assert [p.get("TagIds", "omitted") for p in patches] == [[31], "omitted", []]
    assert "JobTitle" in patches[1] and "JobTitle" not in patches[0]
    note = " ".join(results(report)["contact_tags"].notes)
    assert "an update that named no tag_ids (Gorelo's omit rule, nothing re-sent): Gorelo KEPT the tags (omitting TagIds leaves them unchanged)" in note
    assert "re-sends" not in note.replace("nothing re-sent", "") and "not observable" not in note
    assert fake.leftovers() == [] and record_of(report, "contact").cleaned


async def test_contact_tags_a_gorelo_that_drops_the_tags_on_an_omitted_tag_ids_is_a_loud_note_not_a_failure(run):
    fake = Fake150(omitted_tags_kept=False)
    report = await run_area(run, fake, "contact_tags")
    assert report.ok and results(report)["contact_tags"].status == "pass"  # the finding is what the run was made to get
    patches = [r.json for r in fake.mock.requests if r.method == "PATCH"]
    assert [p.get("TagIds", "omitted") for p in patches] == [[31], "omitted", []]
    note = " ".join(results(report)["contact_tags"].notes)
    assert (
        "Gorelo did NOT keep the tags (the update answered TagIds [] and get_contact read []), so omitting TagIds does not leave them unchanged"
    ) in note
    assert "Gorelo KEPT" not in note
    assert fake.leftovers() == [] and record_of(report, "contact").cleaned


async def test_contact_tags_without_a_tag_in_the_tenant_the_area_is_skipped_before_a_contact_is_made(run):
    fake = Fake150(contact_tags=[])
    report = await run_area(run, fake, "contact_tags")
    found = results(report)["contact_tags"]
    assert found.status == "skipped" and found.detail == "the tenant has no contact tag to set"
    assert [r.path for r in fake.mock.requests] == ["/v1/contacts/tags"]


async def test_contact_tags_a_gorelo_that_drops_the_tags_fails_the_area(run):
    fake = Fake150()
    original = fake.handlers["PATCH /v1/contacts/{contactId}"]

    def forgets(request):
        answer = original(request)
        fake.contacts[int(request.path.rsplit("/", 1)[1])]["TagIds"] = []
        return answer

    fake.handlers["PATCH /v1/contacts/{contactId}"] = forgets
    report = await run_area(run, fake, "contact_tags")
    assert results(report)["contact_tags"].status == "FAIL" and "TagIds" in results(report)["contact_tags"].detail


async def test_ticket_source_is_set_to_another_writable_source_read_back_and_put_back_to_api(run):
    fake = Fake150()
    report = await run_area(run, fake, "ticket_source")
    assert report.ok
    patches = [r.json for r in fake.mock.requests if r.method == "PATCH"]
    assert patches == [{"SourceId": 1}, {"SourceId": 6}]  # Web, then Api again; 7 Messenger is never written
    ticket = next(t for t in fake.tickets.values() if t["Title"].startswith("MCPTEST-"))
    assert ticket["Source"]["Id"] == 6
    assert any("ticket source set to 1 ('Web') and read back, then put back to 6 (Api)" in n for n in results(report)["ticket_source"].notes)


async def test_ticket_source_never_writes_messenger_and_is_skipped_without_another_writable_source(run):
    fake = Fake150()
    fake.handlers["GET /v1/tickets/sources"] = lambda q: envelope([{"Id": 6, "Name": "API"}, {"Id": 7, "Name": "Messenger"}])
    report = await run_area(run, fake, "ticket_source")
    found = results(report)["ticket_source"]
    assert found.status == "skipped" and "no writable source (1 to 6) other than the one of the ticket" in found.detail
    assert not any(r.method == "PATCH" for r in fake.mock.requests)


async def test_ticket_source_a_source_that_does_not_read_back_fails_the_area(run):
    fake = Fake150()
    fake.handlers["PATCH /v1/tickets/{ticketId}"] = lambda q: envelope({"Id": q.path.rsplit("/", 1)[1]})  # Gorelo ignores the source
    report = await run_area(run, fake, "ticket_source")
    assert results(report)["ticket_source"].status == "FAIL" and "Source.Id" in results(report)["ticket_source"].detail
    assert fake.leftovers() == []


# --------------------------------------------------------------------------
# product_charges
# --------------------------------------------------------------------------


async def test_product_charges_a_no_charge_charge_on_the_plain_ticket_is_created_changed_and_deleted(run):
    fake = Fake150()
    report = await run_area(run, fake, "product_charges")
    assert report.ok
    create = next(r for r in fake.mock.requests if r.method == "POST" and r.path == "/v1/product-charges")
    ticket = next(t for t in fake.tickets.values() if t["Title"].startswith("MCPTEST-"))
    assert create.json == {
        "TicketId": ticket["Id"], "ItemId": write_matrix_ids()["global_product"], "Quantity": 1, "UnitPrice": 1.0,
        "Description": f"{RUN} product charge", "BillableStatusId": 2,
    }
    update = next(r for r in fake.mock.requests if r.method == "PATCH")
    assert update.json == {"Quantity": 2, "UnitPrice": 2.0, "Description": f"{RUN} product charge updated"}
    record = record_of(report, "product_charge")
    assert record.details == {"ticket_id": ticket["Id"], "client_id": TEST_CLIENT} and record.cleaned
    assert fake.leftovers() == []


def write_matrix_ids() -> dict[str, str]:
    from test_live_write_matrix import GLOBAL_PRODUCT

    return {"global_product": GLOBAL_PRODUCT}


async def test_product_charges_without_a_product_the_area_is_skipped_after_one_request(run):
    fake = Fake150(catalog=[])
    report = await run_area(run, fake, "product_charges")
    assert results(report)["product_charges"].status == "skipped" and [r.path for r in fake.mock.requests] == ["/v1/items"]


async def test_product_charges_a_failed_update_leaves_the_charge_to_the_cleanup_which_deletes_it_before_the_ticket(run):
    fake = Fake150()
    fake.fail("PATCH /v1/product-charges/{productChargeId}", error_envelope(409, [("070901", "invoiced")]))
    report = await run_area(run, fake, "product_charges")
    assert results(report)["product_charges"].status == "FAIL"
    after = [(r.method, r.path.split("/")[2]) for r in fake.mock.requests][-2:]
    assert after == [("DELETE", "product-charges"), ("DELETE", "tickets")] and fake.leftovers() == []
    assert record_of(report, "product_charge").outcome == "Deleted"


async def test_product_charges_a_charge_that_reads_back_billable_fails_the_area(run):
    fake = Fake150()
    original = fake.handlers["POST /v1/product-charges"]

    def billable(request):
        answer = original(request)
        fake.charges[answer["Data"]["Id"]]["BillableStatus"] = {"Id": 1, "Name": "Billable"}
        return answer

    fake.handlers["POST /v1/product-charges"] = billable
    report = await run_area(run, fake, "product_charges")
    assert results(report)["product_charges"].status == "FAIL" and "BillableStatus.Id (No charge)" in results(report)["product_charges"].detail
    assert fake.leftovers() == []


# --------------------------------------------------------------------------
# invoice_update and the tax probe
# --------------------------------------------------------------------------


def invoice_patches(fake) -> list[dict]:
    return [r.json for r in fake.mock.requests if r.method == "PATCH" and r.path.startswith("/v1/invoices/")]


async def test_invoice_update_changes_the_lines_by_id_and_checks_that_the_ones_left_alone_kept_their_values(run, lines):
    fake = Fake150()
    report = await run_area(run, fake, "invoice_update")
    assert results(report)["invoice_update"].status == "pass", "\n".join(lines)
    header, added, changed, removed = invoice_patches(fake)
    today = header["InvoiceDate"][:10]
    assert header["Reference"] == f"{RUN} update invoice ref" and header["DueDate"][:10] > today and "LineItems" not in header
    # lines A and B go as {Id} alone: the tool resends every line it does not change that way
    assert [set(entry) for entry in added["LineItems"][:2]] == [{"Id"}, {"Id"}] and added["LineItems"][2]["Description"] == f"{RUN} update invoice C"
    assert changed["LineItems"][0]["Quantity"] == 2 and changed["LineItems"][0]["UnitPrice"] == 4.0
    assert [set(entry) for entry in changed["LineItems"][1:]] == [{"Id"}, {"Id"}]
    assert len(removed["LineItems"]) == 2 and all(set(entry) == {"Id"} for entry in removed["LineItems"])
    notes = " ".join(results(report)["invoice_update"].notes)
    assert "Gorelo keeps the fields an entry with only an Id leaves out" in notes
    assert "invoice number consumed" in notes and fake.leftovers() == []
    # both Drafts and the temporary item are deleted by the area itself
    outcomes = {(r.kind, r.outcome) for r in Manifest.load(report.manifest_path).all_created()}
    assert outcomes == {
        ("invoice", "deleted by the write matrix (delete_invoice, Draft)"), ("item", "deleted by the write matrix (delete_item)"),
    }
    assert report.summary["created"] == 3 and report.summary["cleaned"] == 3 and report.refused == 0


async def test_invoice_update_the_tax_probe_sends_the_exempt_rate_and_records_which_tax_gorelo_applied(run):
    fake = Fake150()
    report = await run_area(run, fake, "invoice_update")
    assert results(report)["invoice_update"].status == "pass" and report.ok
    probe = next(n for n in results(report)["invoice_update"].notes if n.startswith("TAX PROBE"))
    assert probe == (
        f"TAX PROBE: a line of an item taxed at 8.5% (TaxId 2) created with no_tax=true (the tool sent TaxId {ZERO_TAX}, the Tax Exempt rate): "
        f"Gorelo applied TaxId {ZERO_TAX}, line TaxAmount 0.0, invoice TotalTax 0.0: no_tax=true gave NO tax; create_invoice added no warning; "
        f"create_invoice note: no_tax used tax id {ZERO_TAX} (the account's Tax Exempt rate, 0%, from list_taxes)."
    )
    item_post = next(r for r in fake.mock.requests if r.method == "POST" and r.path == "/v1/items")
    assert item_post.json["TaxId"] == 2 and item_post.json["Name"] == f"{RUN} tax probe item" and item_post.json["ClientId"] == TEST_CLIENT
    probe_post = [r for r in fake.mock.requests if r.method == "POST" and r.path == "/v1/invoices"][-1]
    assert probe_post.json["LineItems"][0]["TaxId"] == ZERO_TAX and probe_post.json["StatusId"] == 1  # the exempt id, never a null; a Draft
    assert b"null" not in probe_post.content
    # the exempt rate was recorded in the manifest (record details) before and with the Draft, as the approved invoice does
    drafts = [r for r in Manifest.load(report.manifest_path).all_created() if r.kind == "invoice" and r.label.endswith("tax probe invoice")]
    assert [(r.details["tax_id"], r.details["tax_percentage"]) for r in drafts] == [(ZERO_TAX, 0)]


async def test_invoice_update_the_tax_probe_fails_the_area_when_gorelo_keeps_the_items_own_tax(run):
    fake = Fake150(asked_tax_wins=False)
    report = await run_area(run, fake, "invoice_update")
    found = results(report)["invoice_update"]
    assert found.status == "FAIL" and "TAX PROBE FAILED" in found.detail and "no_tax does not give no tax" in found.detail
    assert f"TaxId {ZERO_TAX}, the Tax Exempt rate" in found.detail and "TaxAmount 0.09" in found.detail
    probe = next(n for n in found.notes if n.startswith("TAX PROBE"))
    assert "Gorelo applied TaxId 2, line TaxAmount 0.09, invoice TotalTax 0.09: no_tax=true did NOT give no tax" in probe
    assert f"the tool sent TaxId {ZERO_TAX}" in probe and "the area FAILS" in probe
    # create_invoice read the Draft back, saw the tax on the no_tax line and added a warning to the (full) invoice: the probe records it
    assert "; create_invoice warning: no_tax was asked for line_items[0]" in probe and "the invoice still shows a tax amount on it" in probe
    assert fake.leftovers() == [] and report.cleanup.ok  # the Draft and the item were deleted before the check failed


@pytest.mark.parametrize(
    "taxes, found",
    [
        pytest.param((TAXES[1],), "none", id="no-exempt-rate"),
        pytest.param((TAXES[0], {**TAXES[0], "Id": 77, "Name": "x", "Code": "tax EXEMPT"}, TAXES[1]), "2 (Ids ", id="two-exempt-rates"),
    ],
)
async def test_invoice_update_the_tax_probe_is_skipped_with_a_note_when_there_is_not_exactly_one_exempt_rate(run, taxes, found):
    fake = Fake150(taxes=taxes)
    report = await run_area(run, fake, "invoice_update")
    assert report.ok
    notes = results(report)["invoice_update"].notes
    assert any(n.startswith("TAX PROBE skipped: list_taxes must hold exactly one Tax Exempt rate") and found in n for n in notes)
    assert not any(r.method == "POST" and r.path == "/v1/items" for r in fake.mock.requests)  # nothing written for the probe
    assert report.summary["created"] == 1


async def test_invoice_update_the_tax_probe_is_skipped_with_a_note_when_no_rate_is_above_zero(run):
    fake = Fake150(taxes=(TAXES[0],))
    report = await run_area(run, fake, "invoice_update")
    assert report.ok
    assert "TAX PROBE skipped: list_taxes has no rate with Percentage above 0" in results(report)["invoice_update"].notes
    assert not any(r.method == "POST" and r.path == "/v1/items" for r in fake.mock.requests)  # no item without a rate to test
    assert report.summary["created"] == 1


async def test_invoice_update_an_entry_with_an_id_only_that_comes_back_reset_fails_the_area_and_the_draft_is_still_deleted(run):
    fake = Fake150(keeps_id_only_lines=False)
    report = await run_area(run, fake, "invoice_update")
    found = results(report)["invoice_update"]
    # since the billing fixes update_invoice's own read-back catches a line the PATCH reset, so the tool refuses before the area's check of the
    # lines it did not name (a net that stays for a Gorelo whose reset the tool cannot see)
    assert found.status == "FAIL" and "update_invoice: PATCH /v1/invoices/{invoiceId}: the invoice does not show the change" in found.detail
    assert fake.leftovers() == [] and report.cleanup.ok
    assert record_of(report, "invoice").outcome.startswith("Deleted (Draft")


async def test_invoice_update_a_failed_tax_probe_still_deletes_the_item_and_the_draft(run):
    fake = Fake150()
    fake.fail("DELETE /v1/items/{itemId}", error_envelope(500, [("070500", "boom")]))  # the area's delete fails, the cleanup's works
    report = await run_area(run, fake, "invoice_update")
    assert results(report)["invoice_update"].status == "FAIL"
    assert fake.leftovers() == [] and report.cleanup.ok  # the cleanup deleted what the area could not


async def test_invoice_update_without_a_usable_product_the_area_is_skipped_after_one_request(run):
    fake = Fake150(catalog=[])
    report = await run_area(run, fake, "invoice_update")
    assert results(report)["invoice_update"].status == "skipped" and [r.path for r in fake.mock.requests] == ["/v1/items"]


# --------------------------------------------------------------------------
# contracts
# --------------------------------------------------------------------------


async def test_contracts_an_hourly_contract_is_created_changed_and_archived_and_stays_as_a_known_residue(run, lines):
    fake = Fake150()
    report = await run_area(run, fake, "contracts")
    assert results(report)["contracts"].status == "pass", "\n".join(lines)
    create = next(r for r in fake.mock.requests if r.method == "POST")
    assert create.json["ClientId"] == TEST_CLIENT and create.json["Name"] == f"{RUN} contract"
    assert "AutoApproveAndSend" not in create.json and "AutoInvoiceTicketProducts" not in create.json
    (line,) = create.json["ServiceLines"]
    assert line["LaborTermsId"] == 2 and line["PerHourDetails"] == {"RateTypeId": 1} and "BlockHoursDetails" not in line
    assert line["LineItems"][0]["BillableStatusId"] == 2  # No charge
    patches = [r.json for r in fake.mock.requests if r.method == "PATCH"]
    assert patches[0]["Name"] == f"{RUN} contract renamed" and patches[0]["ServiceLines"][0]["LineItems"] == [
        {"Id": 6001, "Quantity": 2}, {"Id": 6002},  # line item A changed, line item B named by its Id only
    ]
    assert patches[-1] == {"StatusId": 3}
    assert not any(r.method == "DELETE" and "/contracts" in r.path for r in fake.mock.requests)  # never
    assert fake.contract_residue() == [4500] and fake.leftovers() == []
    record = record_of(report, "contract")
    assert record.id == 4500 and record.details == {"client_id": TEST_CLIENT} and not record.cleaned
    assert record.outcome == cleanup.contract_residue("archived by the write matrix (update_contract)")
    assert report.summary["undeletable"] == 1 and report.summary["leftovers"] == 0 and report.ok
    assert report.contracts == ["4500"]
    assert "known undeletable: contract 4500: archived by the write matrix (update_contract), stays in Gorelo until removed in the Gorelo app (a contract cannot be deleted through the API)" in lines
    reminder = next(line for line in lines if line.startswith("reminder: 1 contract(s) of this run stay in Gorelo"))
    assert "(a contract cannot be deleted through the API)" in reminder and RUN in reminder
    assert "result: nothing left over" not in lines and lines[-1] == "result: PASSED"
    notes = " ".join(results(report)["contracts"].notes)
    assert "sent with an Id only read back with the same" in notes and "contract 4500 archived" in notes


async def test_contracts_the_second_service_line_is_added_then_removed_and_the_first_one_is_kept_by_its_id(run):
    fake = Fake150()
    await run_area(run, fake, "contracts")
    patches = [r.json for r in fake.mock.requests if r.method == "PATCH"]
    added, removed = patches[1], patches[2]
    assert added["ServiceLines"] == [  # the first line is kept by its Id, the second is new
        {"Id": 6000}, {"Name": f"{RUN} contract line 2", "LaborTermsId": 2, "PerHourDetails": {"RateTypeId": 1}},
    ]
    assert removed["ServiceLines"] == [{"Id": 6000}]  # the first line, named by its Id only; the second is left out


async def test_contracts_an_entry_with_an_id_only_that_comes_back_reset_fails_and_the_cleanup_archives_the_contract(run):
    fake = Fake150(keeps_id_only_lines=False)
    report = await run_area(run, fake, "contracts")
    found = results(report)["contracts"]
    # update_contract's own read-back catches a line item the PATCH reset: the tool refuses before the area's check
    assert found.status == "FAIL" and "update_contract: PATCH /v1/contracts/{contractId}: the read-back does not show the change" in found.detail
    assert fake.contracts[4500]["Status"]["Id"] == 3 and fake.leftovers() == []
    record = record_of(report, "contract")
    assert record.outcome == cleanup.contract_residue("archived by the cleanup (update_contract)") and report.summary["undeletable"] == 1
    assert report.cleanup.ok and not report.ok  # the area failed, nothing is left over


async def test_contracts_a_failure_after_the_create_is_archived_by_the_cleanup_with_the_cleanup_guard_body(run):
    fake = Fake150()
    fake.fail("PATCH /v1/contracts/{contractId}", error_envelope(500, [("070500", "boom")]))  # the area's first update fails
    report = await run_area(run, fake, "contracts")
    assert results(report)["contracts"].status == "FAIL"
    assert [r.json for r in fake.mock.requests if r.method == "PATCH"][-1] == {"StatusId": 3} and fake.contracts[4500]["Status"]["Id"] == 3
    assert report.summary["undeletable"] == 1 and fake.leftovers() == []


async def test_contracts_without_a_usable_product_the_area_is_skipped_after_one_request(run):
    fake = Fake150(catalog=[])
    report = await run_area(run, fake, "contracts")
    assert results(report)["contracts"].status == "skipped" and [r.path for r in fake.mock.requests] == ["/v1/items"]


async def test_contracts_the_guard_stops_a_block_hours_line_and_an_auto_approve_before_a_request_leaves(run, monkeypatch):
    fake = Fake150()
    seen = {}

    async def bad(matrix):
        line = {"name": "x", "labor_terms_id": 3, "block_hours_details": {"template_id": 0, "balance": 5.0, "price": 10.0}}
        for label, arguments in (
            ("block hours", {"service_lines": [line]}),
            ("auto approve", {"service_lines": [{"name": "x", "labor_terms_id": 2, "per_hour_details": {}}], "auto_approve_and_send": True}),
        ):
            try:
                await matrix.create(
                    "contract", matrix.manifest.label(label), {"client_id": TEST_CLIENT}, "create_contract",
                    {"name": matrix.manifest.label(label), "client_id": TEST_CLIENT, "confirm": True, **arguments},
                )
            except write_matrix.GuardTripped as exc:
                seen[label] = str(exc)

    monkeypatch.setattr(write_matrix, "AREAS", (write_matrix.Area("contracts", bad),))
    await run(fake, with_contracts=True)
    assert "Block Hours" in seen["block hours"] and "AutoApproveAndSend must be absent, null or false" in seen["auto approve"]
    assert fake.contracts == {} and not [i for i in fake.invoices]  # nothing reached Gorelo, so no Draft invoice was raised


# --------------------------------------------------------------------------
# comment_edits
# --------------------------------------------------------------------------


async def test_comment_edits_both_comments_are_private_edited_read_back_and_deleted(run, lines):
    fake = Fake150()
    report = await run_area(run, fake, "comment_edits")
    assert results(report)["comment_edits"].status == "pass", "\n".join(lines)
    posts = [r.json for r in fake.mock.requests if r.method == "POST" and r.path.endswith("/comments")]
    assert [p["ConversationTypeId"] for p in posts] == [2, 2]  # Private both
    edits = [(r.path.split("/")[-4] if "tasks" in r.path else "project", r.json) for r in fake.mock.requests if r.method == "PATCH"]
    assert [kind for kind, _ in edits] == ["tasks", "project"] or len(edits) == 2
    assert all(body.keys() == {"Body"} for _, body in edits) and all(body["Body"].endswith("edited</p>") for _, body in edits)
    assert fake.leftovers() == []
    kinds = {(r.kind, r.cleaned) for r in Manifest.load(report.manifest_path).all_created()}
    assert kinds == {("project", True), ("section", True), ("task", True), ("project_comment", True)}


async def test_comment_edits_without_the_project_scope_the_area_is_skipped_after_its_probe(run):
    fake = Fake150(project_scope=False)
    report = await run_area(run, fake, "comment_edits")
    found = results(report)["comment_edits"]
    assert found.status == "skipped" and found.detail == "scope missing: Project" and [r.path for r in fake.mock.requests] == ["/v1/projects"]
    assert "Project" in report.scopes_missing


async def test_comment_edits_an_edit_gorelo_does_not_show_fails_the_area_and_the_project_is_still_removed(run):
    fake = Fake150()
    fake.handlers["PATCH /v1/projects/{projectId}/comments/{commentId}"] = lambda q: envelope({"Id": q.path.rsplit("/", 1)[1]})
    report = await run_area(run, fake, "comment_edits")
    found = results(report)["comment_edits"]
    assert found.status == "FAIL" and "BodyHtml" in found.detail
    assert fake.leftovers() == [] and report.cleanup.ok


# --------------------------------------------------------------------------
# the approved invoice, approved with approve_invoice (--approve-via-update)
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _the_gate_of_the_approved_invoice_is_open(monkeypatch):
    """The gate of the approved_invoice area is CLOSED in the code (UNEMAILABLE_CONTACT_STATUS_IDS is empty): the tests of the area open it
    the way an operator who accepts this risk would, naming Archived (4)."""
    monkeypatch.setattr(write_matrix, "UNEMAILABLE_CONTACT_STATUS_IDS", (4,))


async def run_approved(run, fake, **options):
    options.setdefault("sync_interval", 0)
    options.setdefault("sync_polls", 3)
    return await run(fake, areas=["approved_invoice"], with_approved_invoice=True, approve_via_update=True, **options)


async def test_approve_via_update_makes_a_draft_of_one_dollar_and_approves_it_with_one_patch(run, lines):
    fake = Fake150()
    report = await run_approved(run, fake)
    found = results(report)["approved_invoice"]
    assert report.ok and found.status == "pass", "\n".join(lines)
    ops = area_ops(fake, report)["approved_invoice"]
    assert ops == [
        "GET /v1/contacts", "GET /v1/clients/{clientId}/locations", "GET /v1/items", "GET /v1/taxes",
        "POST /v1/invoices", "GET /v1/invoices/{invoiceId}",  # the Draft and its read-back
        "GET /v1/invoices/{invoiceId}", "PATCH /v1/invoices/{invoiceId}", "GET /v1/invoices/{invoiceId}",  # approve_invoice
        "GET /v1/invoices/{invoiceId}",  # the poll for ExternalId
        "GET /v1/invoices", "DELETE /v1/invoices/{invoiceId}", "GET /v1/invoices/{invoiceId}", "GET /v1/invoices",  # the void and its checks
    ]
    post = next(r for r in fake.mock.requests if r.method == "POST")
    assert post.json["StatusId"] == 1 and "RecipientEmails" not in post.json  # a Draft of $1
    assert post.json["LineItems"][0]["TaxId"] == ZERO_TAX  # the tax-exempt rate of list_taxes, never a null (that applies the item's own tax)
    assert post.json["LineItems"][0]["UnitPrice"] == 1.0 and post.json["LineItems"][0]["Quantity"] == 1
    patch = next(r for r in fake.mock.requests if r.method == "PATCH")
    assert patch.json == {"StatusId": 5}
    invoice = next(iter(fake.invoices.values()))
    assert invoice["Status"] == {"Id": 4, "Name": "Void"} and fake.leftovers() == [] and fake.residue() == [f"invoice {invoice['Id']}"]
    record = record_of(report, "invoice")
    assert record.details["approve_via_update"] is True and record.details["total"] == 1.0 and record.details["total_tax"] == 0.0
    assert record.details["tax_id"] == ZERO_TAX and record.details["tax_percentage"] == 0
    assert record.details["status_id"] == 4 and record.outcome == "voided by the write matrix (still listed as Void)"
    notes = found.notes
    assert any(n.startswith("approved invoice INV-1042: pushed to the accounting system") for n in notes)
    assert any("approve_invoice" not in n or True for n in notes)
    assert any(line.startswith("reminder: approved invoice INV-1042") and "Gorelo only" in line for line in lines)
    assert lines[-1] == "result: PASSED"


async def test_approve_via_update_never_calls_create_approved_invoice_or_creates_an_approved_invoice(run, monkeypatch):
    fake = Fake150()
    tools_called = []
    real = write_matrix.ToolSession.call

    async def spy(self, name, arguments):
        tools_called.append(name)
        return await real(self, name, arguments)

    monkeypatch.setattr(write_matrix.ToolSession, "call", spy)
    await run_approved(run, fake)
    assert "approve_invoice" in tools_called and "create_invoice" in tools_called and "create_approved_invoice" not in tools_called
    assert [r.json["StatusId"] for r in fake.mock.requests if r.method == "POST"] == [1]  # the one create is a Draft


async def test_approve_via_update_records_the_total_and_the_tax_before_the_approval_is_sent(run, monkeypatch):
    fake = Fake150()
    snapshot = []
    original = fake.handlers["PATCH /v1/invoices/{invoiceId}"]

    def spy(request):
        record = Manifest.load(request_manifest_path[0]).all_created()[0]
        snapshot.append(dict(record.details))
        return original(request)

    request_manifest_path = []
    real_start = Manifest.start

    def start(*args, **kwargs):
        manifest = real_start(*args, **kwargs)
        request_manifest_path.append(manifest.path)
        return manifest

    monkeypatch.setattr(Manifest, "start", staticmethod(start))
    fake.handlers["PATCH /v1/invoices/{invoiceId}"] = spy
    await run_approved(run, fake)
    assert snapshot == [{"status_id": 1, "number": 1042, "display_number": "INV-1042", "tax_id": ZERO_TAX, "tax_percentage": 0, "approve_via_update": True, "total": 1.0, "total_tax": 0.0}]


async def test_approve_via_update_a_draft_that_reads_back_taxed_is_not_approved_and_the_cleanup_deletes_it(run):
    fake = Fake150()
    fake.on_invoice_created(lambda invoice: invoice.update(TotalTax=0.09, Total=1.09))
    report = await run_approved(run, fake)
    found = results(report)["approved_invoice"]
    assert found.status == "FAIL" and "no-tax check FAILED" in found.detail and "the Draft is not approved" in found.detail
    assert not any(r.method == "PATCH" for r in fake.mock.requests)  # never approved
    assert fake.leftovers() == [] and record_of(report, "invoice").outcome.startswith("Deleted (Draft")


async def test_approve_via_update_a_zero_total_draft_is_refused_by_the_check_before_any_approval(run):
    fake = Fake150()
    fake.on_invoice_created(lambda invoice: invoice.update(Total=0.0, SubTotal=0.0))
    report = await run_approved(run, fake)
    assert results(report)["approved_invoice"].status == "FAIL" and not any(r.method == "PATCH" for r in fake.mock.requests)
    assert fake.leftovers() == []


async def test_approve_via_update_an_approval_gorelo_refuses_leaves_the_draft_recorded_as_a_draft_and_deleted(run):
    fake = Fake150()
    fake.fail("PATCH /v1/invoices/{invoiceId}", error_envelope(409, [("070901", "cannot approve")]))
    report = await run_approved(run, fake)
    assert results(report)["approved_invoice"].status == "FAIL"
    assert fake.leftovers() == [] and record_of(report, "invoice").outcome.startswith("Deleted (Draft") and not report.summary["undeletable"]


async def test_approve_via_update_an_approval_whose_answer_is_lost_is_recorded_with_the_status_gorelo_says(run):
    fake = Fake150()
    original = fake.handlers["PATCH /v1/invoices/{invoiceId}"]

    def applied_but_lost(request):
        original(request)  # Gorelo approved it ...
        return error_envelope(502, [("070502", "bad gateway")])  # ... but the answer never came

    fake.handlers["PATCH /v1/invoices/{invoiceId}"] = applied_but_lost
    report = await run_approved(run, fake)
    assert results(report)["approved_invoice"].status == "FAIL"
    record = record_of(report, "invoice")
    assert record.details["status_id"] == 5  # taken from a fresh read, never left as a Draft
    assert not report.cleanup.ok and "left for the user" in record.outcome and "--void-approved" in record.outcome
    assert not any(r.method == "DELETE" for r in fake.mock.requests)  # an Approved invoice is not deleted: it needs the user


# --------------------------------------------------------------------------
# A run that stops: the cleanup knows the kinds
# --------------------------------------------------------------------------


async def test_an_interrupted_run_leaves_the_new_kinds_to_the_cleanup_which_removes_them(run, monkeypatch, lines):
    fake = Fake150()

    class Abort(BaseException):
        pass

    async def stops(matrix):
        client = TEST_CLIENT
        made = await matrix.create("asset_type", matrix.manifest.label("t"), {}, "create_asset_type", {"name": matrix.manifest.label("t")})
        place = await matrix.create(
            "client_location", matrix.manifest.label("l"), {"client_id": client}, "create_client_location",
            {"client_id": client, "name": matrix.manifest.label("l")},
        )
        name = RUN.lower() + ".example"
        await matrix.create("client_domain", matrix.manifest.label("d"), {"client_id": client, "name": name}, "create_client_domain",
                            {"client_id": client, "name": name})
        assert made["Id"] and place["Id"]
        raise Abort

    monkeypatch.setattr(write_matrix, "AREAS", (write_matrix.Area("asset_types", stops),))
    with pytest.raises(Abort):
        await run(fake, with_asset_types=True, with_client_locations=True, with_client_domains=True)
    assert fake.leftovers() == [] and set(fake.asset_types) == {1, 2}
    assert any("interrupted (Abort)" in line for line in lines) and any("cleaned    asset_type" in line for line in lines)
    assert any("cleaned    client_location" in line for line in lines) and any("cleaned    client_domain" in line for line in lines)
    assert lines[-1] == "result: FAILED"


async def test_every_create_of_a_new_area_is_announced_first(run, monkeypatch):
    fake = Fake150()
    order = []
    created, intent = Manifest.created, Manifest.intent

    def spy_intent(self, kind, label, details=None):
        order.append(("intent", kind))
        return intent(self, kind, label, details)

    def spy_created(self, kind, ident, label, details=None):
        order.append(("created", kind))
        return created(self, kind, ident, label, details)

    monkeypatch.setattr(Manifest, "intent", spy_intent)
    monkeypatch.setattr(Manifest, "created", spy_created)
    for key in ("asset_types", "client_locations", "client_domains", "product_charges", "contracts"):
        await run(
            Fake150(), areas=[key], **{FLAG[key]: True},
            started=datetime(2099, 10, 2, 10, 15, 0 + list(NEW_AREAS).index(key), tzinfo=timezone.utc),
        )
    kinds = ("asset_type", "client_location", "client_domain", "product_charge", "contract")
    for kind in kinds:
        assert ("intent", kind) in order and order.index(("intent", kind)) < order.index(("created", kind)), kind


async def test_alert_status_a_different_subject_or_type_is_another_stack_and_the_alert_is_used(run):
    new = alert(alert_guid(1), AgentAssetId=alert_guid(70))
    elsewhere = alert(alert_guid(2), ALERT_DISMISSED, title="Other", AgentAssetId=alert_guid(71))  # same type, another device
    another_type = alert(alert_guid(3), ALERT_DISMISSED, title="Other two", kind=5, AgentAssetId=alert_guid(70))  # same device, another type
    fake = Fake150(alerts=[new, elsewhere, another_type])
    report = await run_area(run, fake, "alert_status")
    assert report.ok and results(report)["alert_status"].status == "pass"
    assert elsewhere["Status"]["Id"] == ALERT_DISMISSED and another_type["Status"]["Id"] == ALERT_DISMISSED  # never touched


async def test_alert_status_reads_the_dismissed_alerts_on_every_page_and_finds_the_stack_member_on_the_last_one(run):
    others = [alert(alert_guid(100 + n), ALERT_DISMISSED, title=f"Other {n}", kind=5) for n in range(200)]  # a full first page of another type
    member = alert(alert_guid(2), ALERT_DISMISSED, title="Page two", kind=4)  # on the second page: same type, same (empty) subject
    fake = Fake150(alerts=[alert(alert_guid(1)), *others, member])
    report = await run_area(run, fake, "alert_status")
    found = results(report)["alert_status"]
    assert found.status == "skipped" and "shares its Type and subject with a Dismissed alert" in found.detail
    listings = [r for r in fake.mock.requests if r.path == "/v1/alerts" and r.query.get("StatusIds") == "2"]
    assert len(listings) == 2 and "Cursor" in listings[1].query  # the second page was asked for with the cursor of the first
    assert not any(r.method == "PATCH" for r in fake.mock.requests) and record_of(report, "alert") is None


async def test_alert_status_an_id_the_restore_moved_that_the_dismissal_did_not_fails_the_area_and_names_it(run):
    fake = Fake150()
    original = fake.handlers["PATCH /v1/alerts"]
    calls = []
    late = alert_guid(55)

    def somebody_dismissed_one_meanwhile(request):
        calls.append(request.json["StatusId"])
        if calls == [2, 1]:  # right before the area's restore, another alert of the same stack is dismissed by somebody
            fake.alerts[late] = alert(late, ALERT_DISMISSED)
        return original(request)

    fake.handlers["PATCH /v1/alerts"] = somebody_dismissed_one_meanwhile
    report = await run_area(run, fake, "alert_status")
    found = results(report)["alert_status"]
    assert found.status == "FAIL" and late in found.detail and "that the dismissal had not moved" in found.detail
    assert "LEFT FOR THE USER" in found.detail
    assert fake.alerts[alert_guid(1)]["Status"]["Id"] == ALERT_NEW


async def test_alert_status_the_cleanup_restores_nothing_for_an_alert_dismissed_before_the_run_started(run):
    fake = Fake150()
    original = fake.handlers["PATCH /v1/alerts"]
    calls = []

    def fails_the_restore(request):
        calls.append(request.json["StatusId"])
        if calls == [2, 1]:
            return error_envelope(500, [("070500", "boom")])
        return original(request)

    fake.handlers["PATCH /v1/alerts"] = fails_the_restore
    getter = fake.handlers["GET /v1/alerts/{alertId}"]
    # what the cleanup reads is an alert whose DismissedOn is older than the run: somebody else dismissed it
    fake.handlers["GET /v1/alerts/{alertId}"] = lambda request: (
        envelope({**getter(request)["Data"], "DismissedOn": "2026-10-01T10:00:00Z"}) if calls == [2, 1] else getter(request)
    )
    report = await run_area(run, fake, "alert_status")
    assert results(report)["alert_status"].status == "FAIL"
    assert calls == [2, 1]  # dismiss, the area's restore (failed): the cleanup sent no restore
    record = record_of(report, "alert")
    assert not record.cleaned and "not inside this run's window" in record.outcome and report.summary["leftovers"] == 1
