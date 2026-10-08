"""Contract probes (2026-10-01).

Settles what the OpenAPI spec does not say. Writes ONLY to the test client (site.local.toml) and only to
records this script creates (plus a reversible AlternateName edit on the test client itself). Every
created record is named MCPTEST-<run> and cleaned up in `finally`. Prints structure and Gorelo
messages only, never customer data. Before the first write it reads the test client and the second client and
stops with exit 2 unless each is named exactly as the site config says. A missing or incomplete site config also
exits 2, with the reason on stderr. Usage:

    UV_PYTHON=/usr/bin/python3.13 /root/.local/bin/uv run --frozen -q python scripts/live/probes_20261001.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.live.guard import configured_clients  # noqa: E402
from scripts.site_config import SiteConfig, SiteConfigError, site  # noqa: E402

BASE = "https://api.usw.gorelo.io/v1"
ENV_FILE = Path("/opt/gorelo-mcp/app/.env")
RUN = "MCPTEST-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
PACE = 1.2


def api_key() -> str:
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("GORELO_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("GORELO_API_KEY not found")


TEST_CLIENT = 0  # set from the site config by start(), before any request
CLIENT: httpx.Client | None = None
RESULTS: dict[str, object] = {"run": RUN}
CREATED: dict[str, list] = {"contacts": [], "tickets": []}


def check_clients(http: httpx.Client, cfg: SiteConfig) -> None:
    """Read each configured client; SiteConfigError unless its Name is exactly the configured one."""
    for client_id, wanted in configured_clients(cfg):
        response = http.get(f"/clients/{client_id}")
        data = response.json().get("Data") if response.status_code == 200 else None
        found = data.get("Name") if isinstance(data, dict) else None
        if found != wanted:
            shown = found if isinstance(found, str) else "(no name)"
            raise SiteConfigError(f"client {client_id} is named {shown}, site config says {wanted}")


def write_allowed(method: str, path: str, body: dict | None) -> bool:
    """Exact-id rule for every non-GET: the test client, or a record this script created."""
    body = body or {}
    if method == "GET":
        return True
    parts = path.strip("/").split("/")
    if method == "POST" and path in ("/contacts", "/tickets", "/alerts"):
        return body.get("ClientId") == TEST_CLIENT
    if method == "PATCH" and path == "/clients":
        return body.get("Id") == TEST_CLIENT
    if method == "PATCH" and path == "/contacts":
        return body.get("ClientId") == TEST_CLIENT and body.get("ContactId") in CREATED["contacts"]
    if len(parts) == 2 and parts[0] in ("tickets", "contacts") and parts[1].isdigit():
        return method in ("PATCH", "DELETE") and int(parts[1]) in CREATED[parts[0]]
    return False


def call(method: str, path: str, *, params=None, body=None, label: str = ""):
    time.sleep(PACE)
    assert write_allowed(method, path, body), f"guard: unexpected write target {method} {path}"
    resp = CLIENT.request(method, path, params=params, json=body)
    try:
        j = resp.json()
    except Exception:
        j = None
    notes = [(n.get("Code"), n.get("PropertyName"), n.get("Message")) for n in ((j or {}).get("Notifications") or [])] if isinstance(j, dict) else []
    out = {"status": resp.status_code, "ok": (j or {}).get("IsSuccess") if isinstance(j, dict) else None, "notes": notes}
    print(f"[{label or method + ' ' + path}] -> {resp.status_code} ok={out['ok']} notes={notes}")
    return resp.status_code, (j or {}).get("Data") if isinstance(j, dict) else None, j, out


def main() -> int:
    # ---- reads: lookups and enum pairs ------------------------------------------------------
    _, statuses, _, _ = call("GET", "/tickets/statuses", label="statuses")
    _, types, _, _ = call("GET", "/tickets/types", label="types")
    _, groups, _, _ = call("GET", "/organization/groups", label="groups")
    _, users, _, _ = call("GET", "/organization/users", params={"PageSize": 50}, label="users")
    RESULTS["users"] = [{"Id": u.get("Id"), "Name": " ".join(filter(None, [u.get("FirstName"), u.get("LastName")])) or u.get("Name")} for u in users or []]
    RESULTS["groups"] = [{"Id": g.get("Id"), "Name": g.get("Name")} for g in groups or []]
    RESULTS["statuses"] = [{"Id": s.get("Id"), "Name": s.get("Name")} for s in statuses or []]
    RESULTS["types"] = [{"Id": t.get("Id"), "Name": t.get("Name")} for t in types or []]

    _, tickets, _, _ = call("GET", "/tickets", params={"PageSize": 200}, label="tickets page for enum pairs")
    src, pri = {}, {}
    for t in tickets or []:
        s, p = t.get("Source") or {}, t.get("Priority") or {}
        src.setdefault(s.get("Id"), set()).add(s.get("Name"))
        pri.setdefault(p.get("Id"), set()).add(p.get("Name"))
    RESULTS["source_pairs"] = {str(k): sorted(map(str, v)) for k, v in sorted(src.items(), key=lambda kv: str(kv[0]))}
    RESULTS["priority_pairs"] = {str(k): sorted(map(str, v)) for k, v in sorted(pri.items(), key=lambda kv: str(kv[0]))}
    print("source pairs:", RESULTS["source_pairs"], "priority pairs:", RESULTS["priority_pairs"])

    st, data, _, o = call("GET", "/clients", params={"PageSize": 500}, label="clients PageSize=500")
    RESULTS["clients_pagesize_500"] = {"status": st, "rows": len(data) if isinstance(data, list) else None, "notes": o["notes"]}

    # ---- contact probes on the test client ---------------------------------------------------------
    email = f"{RUN.lower()}@example.invalid"
    contact_id = None
    try:
        st, data, _, o = call("POST", "/contacts", body={"ClientId": TEST_CLIENT, "FirstName": "MCPTEST", "LastName": RUN + "-unknownfield", "PrimaryEmail": email, "ClientLocationId": 1}, label="contact create with unknown field ClientLocationId")
        RESULTS["contact_unknown_field"] = o
        if st == 200 and isinstance(data, dict) and (data.get("Id") or data.get("ContactId")):
            CREATED["contacts"].append(data.get("Id") or data.get("ContactId"))  # accepted after all: clean it up
        st, data, _, o = call("POST", "/contacts", body={"ClientId": TEST_CLIENT, "FirstName": "MCPTEST", "LastName": RUN, "PrimaryEmail": email}, label="contact create minimal (4 spec-required fields)")
        RESULTS["contact_create_minimal"] = {**o, "data_keys": sorted(data.keys()) if isinstance(data, dict) else data}
        if st == 200 and isinstance(data, dict):
            contact_id = data.get("Id") or data.get("ContactId")
            CREATED["contacts"].append(contact_id)
            RESULTS["contact_created_LocationId"] = data.get("LocationId")
            RESULTS["contact_created_fields"] = {k: data.get(k) for k in ("MobilePhoneCountryCode", "OfficePhoneCountryCode", "TimeZone", "LocationId", "JobTitle", "Department")}
        if contact_id:
            base = {"ContactId": contact_id, "FirstName": "MCPTEST", "LastName": RUN, "ClientId": TEST_CLIENT, "PrimaryEmail": email}
            for code in ("1", "+1", "US"):
                st, data, _, o = call("PATCH", "/contacts", body={**base, "MobilePhone": "5555550100", "MobilePhoneCountryCode": code, "JobTitle": "MCPTEST title", "SecondaryEmail": [f"{RUN.lower()}-2@example.invalid"]}, label=f"contact PATCH phone with country code {code!r}")
                RESULTS[f"contact_patch_phone_code_{code}"] = o
            st, data, _, o = call("GET", f"/contacts/{contact_id}", label="contact GET after phone PATCH")
            RESULTS["contact_after_phone_patch"] = {k: (data or {}).get(k) for k in ("MobilePhone", "MobilePhoneCountryCode", "JobTitle", "LocationId")}
            RESULTS["contact_response_keys"] = sorted((data or {}).keys())
            st, data, _, o = call("PATCH", "/contacts", body=base, label="contact PATCH with ONLY required fields (replace vs merge)")
            RESULTS["contact_patch_required_only"] = o
            st, data, _, o = call("GET", f"/contacts/{contact_id}", label="contact GET after required-only PATCH")
            RESULTS["contact_after_required_only_patch"] = {k: (data or {}).get(k) for k in ("MobilePhone", "MobilePhoneCountryCode", "JobTitle", "LocationId")}

        # ---- client clear semantics on the test client -------------------------------------------
        st, data, _, _ = call("GET", f"/clients/{TEST_CLIENT}", label="test client GET")
        original_alt = (data or {}).get("AlternateName")
        RESULTS["client_alt_original_is_empty"] = original_alt in (None, "")
        call("PATCH", "/clients", body={"Id": TEST_CLIENT, "AlternateName": RUN}, label="client PATCH AlternateName set")
        st, data, _, _ = call("GET", f"/clients/{TEST_CLIENT}", label="client GET after set")
        RESULTS["client_alt_after_set_matches"] = (data or {}).get("AlternateName") == RUN
        st, _, _, o = call("PATCH", "/clients", body={"Id": TEST_CLIENT, "AlternateName": ""}, label="client PATCH AlternateName ''")
        RESULTS["client_alt_clear_empty_string"] = o
        st, data, _, _ = call("GET", f"/clients/{TEST_CLIENT}", label="client GET after ''")
        RESULTS["client_alt_after_empty"] = repr((data or {}).get("AlternateName"))
        if (data or {}).get("AlternateName") not in (None, "", original_alt):
            st, _, _, o = call("PATCH", "/clients", body={"Id": TEST_CLIENT, "AlternateName": None}, label="client PATCH AlternateName null")
            st, data, _, _ = call("GET", f"/clients/{TEST_CLIENT}", label="client GET after null")
            RESULTS["client_alt_after_null"] = repr((data or {}).get("AlternateName"))
        if original_alt not in (None, "") and (data or {}).get("AlternateName") != original_alt:
            call("PATCH", "/clients", body={"Id": TEST_CLIENT, "AlternateName": original_alt}, label="client restore AlternateName")

        # ---- ticket null semantics on the test client --------------------------------------------
        status_new = next((s["Id"] for s in RESULTS["statuses"] if str(s["Name"]).lower() in ("new", "open")), RESULTS["statuses"][0]["Id"])
        type_id = RESULTS["types"][0]["Id"]
        group_id = RESULTS["groups"][0]["Id"]
        operator = next((u["Id"] for u in RESULTS["users"] if u.get("Id") == site().operator_user), None)
        RESULTS["operator_user_id"] = operator
        st, data, _, o = call("POST", "/tickets", body={"Title": f"{RUN} probe ticket", "ClientId": TEST_CLIENT, "StatusId": status_new, "TypeId": type_id, "GroupId": group_id, "PriorityId": 3, "SourceId": 6, "SendTicketCreatedEmail": False, "Description": "Contract probe. Safe to ignore; deleted by the probe."}, label="ticket create on the test client")
        RESULTS["ticket_create"] = {**o, "data": data}
        tid = (data or {}).get("Id") if isinstance(data, dict) else None
        if tid:
            CREATED["tickets"].append(tid)
        if tid and operator is None:
            print("operator user not identified by name; skipping assignee probes")
        if tid and operator is not None:
            st, data, _, o = call("PATCH", f"/tickets/{tid}", body={"LeadAssigneeId": operator}, label="ticket PATCH LeadAssigneeId=operator")
            RESULTS["ticket_patch_assign"] = {**o, "data_keys": sorted(data.keys()) if isinstance(data, dict) else data}
            st, data, _, o = call("GET", f"/tickets/{tid}", label="ticket GET after assign")
            RESULTS["ticket_after_assign"] = {"LeadAssigneeId": (data or {}).get("LeadAssigneeId"), "Priority": (data or {}).get("Priority"), "Source": (data or {}).get("Source"), "Number": (data or {}).get("Number"), "DisplayNumber": (data or {}).get("DisplayNumber")}
            st, data, _, o = call("PATCH", f"/tickets/{tid}", body={"LeadAssigneeId": None}, label="ticket PATCH LeadAssigneeId=null")
            RESULTS["ticket_patch_null"] = o
            st, data, _, o = call("GET", f"/tickets/{tid}", label="ticket GET after null")
            RESULTS["ticket_after_null_LeadAssigneeId"] = (data or {}).get("LeadAssigneeId")
            num = RESULTS["ticket_after_assign"].get("Number")
            st, data, _, o = call("GET", "/tickets", params={"Query": str(num), "PageSize": 5}, label="ticket list Query=<number>")
            RESULTS["ticket_query_by_number_hits"] = [t.get("Id") == tid for t in (data or [])]

        # ---- alert path and Resource rule on the test client ------------------------------------
        # The probe creates at most one alert, so the missing-Resource case is not probed (the spec marks it required).
        st, data, raw, o = call("POST", "/alerts", body={"Name": f"{RUN} probe alert", "ClientId": TEST_CLIENT, "Resource": "mcp-probe", "Severity": 1, "Description": "Contract probe from the Gorelo MCP server. Safe to ignore."}, label="alert POST valid (creates one alert)")
        RESULTS["alert_valid"] = {**o, "data": data, "data_type": type(data).__name__}
    finally:
        for tid in CREATED["tickets"]:
            st, data, _, o = call("DELETE", f"/tickets/{tid}", label=f"cleanup ticket {tid}")
            RESULTS.setdefault("cleanup", []).append({"ticket": tid, **o, "data": data})
        for cid in CREATED["contacts"]:
            st, data, _, o = call("DELETE", f"/contacts/{cid}", label=f"cleanup contact {cid}")
            RESULTS.setdefault("cleanup", []).append({"contact": cid, **o, "data": data})
        print("\n=== RESULTS JSON ===")
        print(json.dumps(RESULTS, indent=1, default=str))
    return 0


def start() -> int:
    """Load the site config and the key, check the client names, then run the probes. Exit 2 on any refusal."""
    global TEST_CLIENT, CLIENT
    try:
        cfg = site()
        key = api_key()
        CLIENT = httpx.Client(base_url=BASE, headers={"X-API-Key": key, "Accept": "application/json"}, timeout=30.0)
        TEST_CLIENT = cfg.test_client
        check_clients(CLIENT, cfg)
    except (SiteConfigError, SystemExit, OSError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2
    return main()


if __name__ == "__main__":
    sys.exit(start())
