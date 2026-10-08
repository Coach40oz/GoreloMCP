"""scripts/live/smoke.py: the live read-only smoke test, run offline against a static fake Gorelo.

The fake answers a list and a record for every read tool. The REAL LiveGuard (read mode) and the real tool code run in
front of it, so a request that is not a GET, or a tool that is not a read tool, fails these tests like it would live.
Nothing touches the network or the app's .env.
"""

from __future__ import annotations

import collections

import pytest
from conftest import TEST_API_KEY, MockGorelo, envelope, error_envelope, paged_envelope, uid
from site_helper import site_config_env  # noqa: F401  (autouse: points GORELO_SITE_CONFIG at invented values)

from scripts.live import _env, smoke
from scripts.live.guard import GuardViolation, LiveGuard
from scripts.live.smoke import Case, ShapeError
from tools._common import REGISTRY

pytestmark = pytest.mark.anyio

SECRET = "SECRET-CUSTOMER-DATA"  # sits in every row the fake serves: it must never reach the printed table
CLIENT_ID = 101
TICKET_QUIET, TICKET_COMMENTED = uid(1), uid(2)
TICKETS = [
    {"Id": TICKET_QUIET, "Number": 3001, "DisplayNumber": "TCK-3001", "ClientId": CLIENT_ID, "Title": SECRET,
     "LastUpdate": {"On": "2026-10-01T10:00:00Z", "Summary": SECRET, "UpdateType": "StatusChange"}},
    {"Id": TICKET_COMMENTED, "Number": 3002, "DisplayNumber": "TCK-3002", "ClientId": CLIENT_ID, "Title": SECRET,
     "LastUpdate": {"On": "2026-10-01T09:00:00Z", "Summary": SECRET, "UpdateType": "NewComment"}},
]
COMMENT_ID, APPROVAL_ID, PROJECT_ID, TASK_ID = uid(11), uid(12), uid(21), uid(22)


@pytest.fixture(autouse=True)
def _never_read_the_real_env(monkeypatch, tmp_path):
    """Whatever a test does, the live service's .env is not the file that gets opened."""
    monkeypatch.setattr(_env, "ENV_FILE", tmp_path / "no-such-dir" / ".env")


def row(ident, **more):
    return {"Id": ident, "Name": SECRET, **more}


class Pages(list):
    """Rows of a paged list."""


class Items(list):
    """Rows of an unpaged list."""


def last(request, index):
    return request.path.split("/")[index]


class ReadFake:
    """Serves GET routes for every read tool. `empty` names routes that answer an empty list, `fail` maps a route to a
    response, project_scope and forms_scope False answer the 403 with code 080203 on those routes."""

    def __init__(self, *, project_scope=True, forms_scope=True, empty=(), fail=None):
        self.mock = MockGorelo()
        self.scopes = {"Project": project_scope, "Forms": forms_scope}
        self.empty, self.fail = set(empty), dict(fail or {})
        on = self._on
        on("/v1/clients", Pages([row(CLIENT_ID), row(102)]))
        on("/v1/clients/{clientId}", lambda q: envelope(row(int(last(q, 3)))))
        on("/v1/clients/{clientId}/locations", Items([row(901, ClientId=CLIENT_ID)]))
        on("/v1/contacts", self.list_contacts)
        on("/v1/contacts/{contactId}", lambda q: envelope(row(int(last(q, 3)))))
        on("/v1/organization/groups", Items([row(7201)]))
        on("/v1/organization/users", Pages([row(9700), row(9202)]))
        on("/v1/assets/agents", Pages([row(uid(31))]))
        on("/v1/assets/agents/{deviceId}", lambda q: envelope(row(last(q, 4))))
        on("/v1/assets/custom", Pages([row(uid(32))]))
        on("/v1/alerts", Pages([row(uid(33))]))
        on("/v1/tickets/statuses", Items([row(1), row(4)]))
        on("/v1/tickets/types", Items([row(7101)]))
        on("/v1/tickets/tags", Items([row(700)]))
        on("/v1/tickets", self.list_tickets)
        on("/v1/tickets/{ticketId}", lambda q: envelope(next(t for t in TICKETS if t["Id"] == last(q, 3))))
        on("/v1/tickets/{ticketId}/comments", Pages([row(COMMENT_ID)]))
        on("/v1/tickets/{ticketId}/comments/{commentId}", lambda q: envelope(row(last(q, 5))))
        on(
            "/v1/tickets/{ticketId}/conversations",
            Items(
                [
                    {"Id": None, "Name": "Main thread", "Type": {"Id": 1, "Name": "Public"}},
                    {"Id": 55, "Name": SECRET, "Type": {"Id": 3, "Name": "Side Conversation"}},
                    {"Id": APPROVAL_ID, "Name": SECRET, "Type": {"Id": 4, "Name": "Approval"}},
                ]
            ),
        )
        on("/v1/tickets/{ticketId}/approvals/{approvalId}", lambda q: envelope(row(last(q, 5))))
        on("/v1/billing-roles", Items([row(11)]))
        on("/v1/work-types", Items([row(21)]))
        on("/v1/time-entries", Pages([row(5001)]))
        on("/v1/time-entries/{timeEntryId}", lambda q: envelope(row(int(last(q, 3)))))
        on("/v1/contracts", Pages([row(61)]))
        on("/v1/contracts/{contractId}", lambda q: envelope(row(int(last(q, 3)))))
        on("/v1/invoices", Pages([row(uid(41))]))
        on("/v1/invoices/{invoiceId}", lambda q: envelope(row(last(q, 3))))
        on("/v1/items", Pages([row(uid(42))]))
        on("/v1/items/{itemId}", lambda q: envelope(row(last(q, 3))))
        on("/v1/items/categories", Items([row(71)]))
        on("/v1/taxes", Items([row(81)]))
        on("/v1/uptime", Pages([row(uid(43))]))
        on("/v1/uptime/{checkId}", lambda q: envelope(row(last(q, 3))))
        on("/v1/projects", Pages([row(PROJECT_ID)]), scope="Project")
        on("/v1/projects/{projectId}", lambda q: envelope(row(last(q, 3))), scope="Project")
        on("/v1/projects/tags", Items([row(uid(51))]), scope="Project")
        on("/v1/projects/types", Items([row(uid(52))]), scope="Project")
        on("/v1/projects/{projectId}/sections", Items([row(uid(53))]), scope="Project")
        on("/v1/projects/{projectId}/tasks", Pages([row(TASK_ID)]), scope="Project")
        on("/v1/projects/{projectId}/tasks/{taskId}", lambda q: envelope(row(last(q, 5))), scope="Project")
        on(
            "/v1/projects/{projectId}/tasks/{taskId}/conversations",
            Items([{"Id": uid(54), "Name": SECRET, "Type": {"Id": 4, "Name": "Approval"}}]),
            scope="Project",
        )
        on("/v1/projects/{projectId}/tasks/{taskId}/approvals/{approvalId}", lambda q: envelope(row(last(q, 7))), scope="Project")
        on("/v1/projects/{projectId}/comments", Pages([row(uid(55))]), scope="Project")
        on("/v1/projects/{projectId}/comments/{commentId}", lambda q: envelope(row(last(q, 5))), scope="Project")
        on("/v1/forms", Pages([{"Id": "form-abc_1", "Title": SECRET}]), scope="Forms")
        on("/v1/forms/{formId}/responses", Pages([row(uid(56))]), scope="Forms")

    def _on(self, template, data, *, scope=None):
        def serve(request):
            if template in self.fail:
                return self.fail[template]
            if scope is not None and not self.scopes[scope]:
                return error_envelope(403, [("080203", f"API key does not have '{scope}' scope")])
            if isinstance(data, Pages):
                return paged_envelope([] if template in self.empty else list(data))
            if isinstance(data, Items):
                return envelope([] if template in self.empty else list(data))
            return data(request)

        self.mock.on("GET", template, serve)

    def list_contacts(self, q):
        if "/v1/contacts" in self.empty:
            return paged_envelope([])
        return paged_envelope([row(201), row(202)], next_cursor="c2", total_count=5)  # more pages than this one

    def list_tickets(self, q):
        rows = TICKETS
        if "ClientIds" in q.query:
            rows = [t for t in rows if str(t["ClientId"]) in q.query["ClientIds"].split(",")]
        if "Query" in q.query:
            rows = [t for t in rows if q.query["Query"].casefold() in (str(t["Number"]), t["DisplayNumber"].casefold())]
        return paged_envelope(rows)


@pytest.fixture
def lines():
    return []


@pytest.fixture
def run(make_settings, lines):
    async def go(fake: ReadFake, **options):
        options.setdefault("pace", 0)
        options.setdefault("echo", lines.append)
        return await smoke.run_smoke(settings=make_settings(destructive=True), transport=fake.mock.transport, **options)

    return go


def outcomes(report) -> dict[str, smoke.Row]:
    return {row_.label: row_ for row_ in report.rows}


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


def test_the_plan_covers_exactly_the_registered_read_tools_once_and_get_ticket_three_times():
    reads = smoke.read_tools()
    assert len(reads) == 51 and "export_invoice_pdf" not in reads
    assert {"list_alerts", "get_invoice"} <= set(reads)
    assert {spec.name for spec in REGISTRY.specs if spec.kind == "read"} == set(reads)
    assert smoke.plan_problems(smoke.CASES, reads) == ([], [])
    tools = [case.tool for case in smoke.CASES]
    assert {name: count for name, count in collections.Counter(tools).items() if count > 1} == {"get_ticket": 3}
    labels = [case.row for case in smoke.CASES]
    assert len(labels) == len(set(labels)) == 53
    assert [case.row for case in smoke.CASES if case.tool == "get_ticket"] == [
        "get_ticket (guid)", "get_ticket (number)", "get_ticket (display number)",
    ]


def test_no_write_or_destructive_tool_is_in_the_plan():
    kinds = {spec.name: spec.kind for spec in REGISTRY.specs}
    assert {kinds[case.tool] for case in smoke.CASES} == {"read"}


def test_plan_problems_names_foreign_and_uncovered_tools():
    reads = smoke.read_tools()
    plan = [Case("create_client", "record"), *[c for c in smoke.CASES if c.tool != "list_taxes"]]
    assert smoke.plan_problems(plan, reads) == (["create_client"], ["list_taxes"])


# --------------------------------------------------------------------------
# A full run
# --------------------------------------------------------------------------


async def test_a_full_run_calls_every_read_tool_once_sends_only_gets_and_passes(run, lines):
    fake = ReadFake()
    report = await run(fake)
    assert report.ok, "\n".join(lines)
    assert len(report.rows) == 53 and all(r.outcome == smoke.OK for r in report.rows), [
        (r.label, r.outcome, r.note) for r in report.rows if r.outcome != smoke.OK
    ]
    assert {r.method for r in fake.mock.requests} == {"GET"}
    # 51 calls with HTTP (two local tables make none) plus the search behind get_ticket by number and by display number
    assert report.requests == len(fake.mock.requests) == 53 and report.refused == 0
    assert report.counts() == {smoke.OK: 53, smoke.SCOPE_MISSING: 0, smoke.SKIPPED: 0, smoke.FAILED: 0}
    assert lines[-1] == "result: PASSED" and "53 checks: 53 ok, 0 scope missing, 0 skipped, 0 FAILED; 53 requests sent" in lines[-2]
    assert not fake.mock.unmatched


async def test_the_rows_are_printed_as_they_complete_and_make_up_the_rendered_table(run, lines):
    fake = ReadFake()
    seen = []
    report = await run(fake, echo=lambda line: seen.append((line, len(fake.mock.requests))))
    assert [line for line, _ in seen] == report.render()
    by_start = {line.split()[0]: made for line, made in seen if line.split()[0] in ("tool", "health_check", "list_clients", "list_form_responses")}
    assert by_start["tool"] == 0  # the header is out before the first request
    assert by_start["health_check"] == 1 and by_start["list_clients"] == 2  # each row appears when its call is done
    assert by_start["list_form_responses"] == 53  # the last row only after the last request


async def test_get_ticket_is_called_with_the_guid_the_number_and_the_display_number_of_a_listed_ticket(run):
    fake = ReadFake()
    report = await run(fake)
    assert report.ok
    paths = [(r.path, r.query.get("Query")) for r in fake.mock.requests if r.path.startswith("/v1/tickets")]
    # the ticket whose last update was a comment is the one explored
    assert (f"/v1/tickets/{TICKET_COMMENTED}", None) in paths
    assert ("/v1/tickets", "3002") in paths and ("/v1/tickets", "TCK-3002") in paths
    assert f"/v1/tickets/{TICKET_COMMENTED}/comments" in [p for p, _ in paths]
    assert not any(f"/v1/tickets/{TICKET_QUIET}" in p for p, _ in paths)
    found = outcomes(report)
    assert all(found[name].note == "Id matches" for name in ("get_ticket (guid)", "get_ticket (number)", "get_ticket (display number)"))


async def test_ids_are_discovered_from_the_lists_and_used_by_the_gets(run):
    fake = ReadFake()
    report = await run(fake)
    assert report.ok
    sent = {r.path for r in fake.mock.requests}
    for path in (
        f"/v1/clients/{CLIENT_ID}", f"/v1/clients/{CLIENT_ID}/locations", "/v1/contacts/201", f"/v1/assets/agents/{uid(31)}",
        f"/v1/tickets/{TICKET_COMMENTED}/comments/{COMMENT_ID}", f"/v1/tickets/{TICKET_COMMENTED}/approvals/{APPROVAL_ID}",
        "/v1/time-entries/5001", "/v1/contracts/61", f"/v1/invoices/{uid(41)}", f"/v1/items/{uid(42)}", f"/v1/uptime/{uid(43)}",
        f"/v1/projects/{PROJECT_ID}", f"/v1/projects/{PROJECT_ID}/tasks/{TASK_ID}",
        f"/v1/projects/{PROJECT_ID}/tasks/{TASK_ID}/approvals/{uid(54)}", f"/v1/projects/{PROJECT_ID}/comments/{uid(55)}",
        "/v1/forms/form-abc_1/responses",
    ):
        assert path in sent, path
    search = next(r for r in fake.mock.requests if r.path == "/v1/tickets" and "ClientIds" in r.query)
    assert search.query["ClientIds"] == str(CLIENT_ID)
    assert next(r for r in fake.mock.requests if r.path == "/v1/contacts").query["PageSize"] == "20"
    assert outcomes(report)["list_contacts"].note == "count=2 total_count=5 (more pages)"


async def test_list_alerts_is_read_as_a_paged_list_of_20_and_get_invoice_reads_the_first_listed_invoice(run):
    fake = ReadFake()
    report = await run(fake)
    assert report.ok
    found = outcomes(report)
    assert found["list_alerts"].outcome == smoke.OK and found["list_alerts"].note == "count=1 total_count=1"
    alerts = next(r for r in fake.mock.requests if r.path == "/v1/alerts")
    assert alerts.method == "GET" and alerts.query["PageSize"] == "20" and "Cursor" not in alerts.query
    assert found["get_invoice"].outcome == smoke.OK and found["get_invoice"].note == "Id matches"
    listed = next(r for r in fake.mock.requests if r.path == "/v1/invoices")
    assert listed.query == {"PageSize": "20"}
    one = next(r for r in fake.mock.requests if r.path == f"/v1/invoices/{uid(41)}")  # the id of the first listed row
    assert one.method == "GET" and one.query == {}
    names = [row_.label for row_ in report.rows]
    assert names.index("list_invoices") < names.index("get_invoice") < names.index("list_items")
    assert names.index("list_custom_assets") < names.index("list_alerts") < names.index("list_ticket_statuses")


async def test_the_smoke_stays_read_only_it_never_exports_an_invoice_pdf(run):
    fake = ReadFake()
    report = await run(fake)
    assert report.ok and {r.method for r in fake.mock.requests} == {"GET"}
    assert not any(r.path.endswith("/pdf") for r in fake.mock.requests)  # the export records an event on the invoice
    assert "export_invoice_pdf" not in {case.tool for case in smoke.CASES}


async def test_get_invoice_is_failed_when_the_invoice_it_reads_is_not_the_one_listed(run):
    fake = ReadFake(fail={"/v1/invoices/{invoiceId}": envelope(row(uid(77)))})  # answers another invoice's record
    report = await run(fake)
    found = outcomes(report)
    assert not report.ok and found["get_invoice"].outcome == smoke.FAILED
    assert found["get_invoice"].note == "the record's Id is not the one that was asked for"


async def test_the_table_has_counts_only_never_record_contents_and_never_the_key(run, lines):
    fake = ReadFake(fail={"/v1/taxes": error_envelope(500, [("070500", f"{SECRET} broke, key {TEST_API_KEY}")])})
    report = await run(fake)
    assert not report.ok
    text = "\n".join(lines)
    taxes = next(line for line in lines if line.startswith("list_taxes"))
    assert "FAILED" in taxes and "***" in taxes and TEST_API_KEY not in text
    # a failure shows Gorelo's message (the one place text from the API appears); no other row holds any
    assert text.count(SECRET) == 1
    assert lines[0].startswith("tool") and lines[-1] == "result: FAILED"


# --------------------------------------------------------------------------
# Scope, empty lists, errors
# --------------------------------------------------------------------------


async def test_a_scope_403_from_projects_and_forms_is_scope_missing_and_not_a_failure(run, lines):
    fake = ReadFake(project_scope=False, forms_scope=False)
    report = await run(fake)
    assert report.ok
    found = outcomes(report)
    for name in ("list_projects", "list_project_tags", "list_project_types", "list_forms"):
        assert found[name].outcome == smoke.SCOPE_MISSING, name
    assert found["list_projects"].note == "Project scope" and found["list_forms"].note == "Forms scope"
    for name in ("get_project", "list_project_sections", "list_project_tasks", "get_project_task", "list_task_conversations",
                 "get_task_approval", "list_project_comments", "get_project_comment", "list_form_responses"):
        assert found[name].outcome == smoke.SKIPPED, name
    assert found["get_project"].note == "needs a project: list_projects: scope missing"
    assert found["get_project_task"].note == "needs a project: list_projects: scope missing"
    assert found["list_form_responses"].note == "needs a form: list_forms: scope missing"
    assert report.counts()[smoke.SCOPE_MISSING] == 4 and report.counts()[smoke.FAILED] == 0
    # the missing-scope answers are all the requests those tools made: nothing else was tried
    assert [r.path for r in fake.mock.requests if "project" in r.path or "forms" in r.path] == [
        "/v1/projects", "/v1/projects/tags", "/v1/projects/types", "/v1/forms",
    ]
    assert lines[-1] == "result: PASSED"


async def test_a_scope_403_from_any_other_tool_is_a_failure(run):
    fake = ReadFake(fail={"/v1/contracts": error_envelope(403, [("080203", "API key does not have 'Contracts' scope")])})
    report = await run(fake)
    found = outcomes(report)
    assert not report.ok
    assert found["list_contracts"].outcome == smoke.FAILED
    assert "the API key does not have the 'Contracts' scope" in found["list_contracts"].note
    assert found["get_contract"].outcome == smoke.SKIPPED and "list_contracts: FAILED" in found["get_contract"].note


async def test_a_get_whose_list_is_empty_is_skipped_and_says_which_list(run):
    empty = {"/v1/contacts", "/v1/assets/agents", "/v1/time-entries", "/v1/items", "/v1/uptime", "/v1/contracts", "/v1/invoices"}
    fake = ReadFake(empty=empty)
    report = await run(fake)
    found = outcomes(report)
    assert report.ok
    for get, parent, what in (
        ("get_contact", "list_contacts", "a contact"), ("get_agent", "list_agents", "an agent"),
        ("get_time_entry", "list_time_entries", "a time entry"), ("get_item", "list_items", "a catalog item"),
        ("get_uptime_check", "list_uptime_checks", "an uptime check"), ("get_contract", "list_contracts", "a contract"),
        ("get_invoice", "list_invoices", "an invoice"),
    ):
        assert found[parent].outcome == smoke.OK and found[parent].note.startswith("count=0")
        assert found[get].outcome == smoke.SKIPPED and found[get].note == f"needs {what}: {parent} returned no rows", get
    assert not any(r.path in ("/v1/contacts/201", "/v1/contracts/61") for r in fake.mock.requests)
    assert not any(r.path.startswith("/v1/invoices/") for r in fake.mock.requests)  # no invoice to read: no get_invoice request


async def test_a_list_that_is_never_empty_in_a_working_tenant_fails_when_it_is(run):
    fake = ReadFake(empty={"/v1/tickets/statuses", "/v1/clients"})
    report = await run(fake)
    found = outcomes(report)
    assert not report.ok
    assert found["list_ticket_statuses"].outcome == smoke.FAILED and "no rows" in found["list_ticket_statuses"].note
    assert found["list_clients"].outcome == smoke.FAILED and "no rows" in found["list_clients"].note
    assert found["get_client"].outcome == smoke.SKIPPED and found["get_client"].note == "needs a client: list_clients: FAILED"


async def test_a_gorelo_error_is_a_failed_row_with_the_message_and_the_dependents_are_skipped(run, lines):
    fake = ReadFake(fail={"/v1/tickets": error_envelope(500, [("070500", "boom")])})
    report = await run(fake)
    found = outcomes(report)
    assert not report.ok
    assert found["list_tickets"].outcome == smoke.FAILED
    assert "Gorelo rejected list_tickets (HTTP 500, code 070500): boom" in found["list_tickets"].note
    assert found["get_ticket (guid)"].outcome == smoke.SKIPPED and "list_tickets: FAILED" in found["get_ticket (guid)"].note
    assert found["search_tickets"].outcome == smoke.SKIPPED
    assert lines[-1] == "result: FAILED"


async def test_health_check_that_reports_not_ok_is_a_failed_row(run):
    fake = ReadFake(fail={"/v1/clients": error_envelope(500, [("070500", "down")])})
    report = await run(fake)
    found = outcomes(report)
    assert found["health_check"].outcome == smoke.FAILED and "ok is not true" in found["health_check"].note
    assert found["list_clients"].outcome == smoke.FAILED


async def test_a_read_tool_the_plan_forgot_is_a_failed_row(run, monkeypatch):
    monkeypatch.setattr(smoke, "CASES", tuple(c for c in smoke.CASES if c.tool != "list_taxes"))
    report = await run(ReadFake())
    found = outcomes(report)
    assert not report.ok
    assert found["list_taxes"].outcome == smoke.FAILED and "no smoke case" in found["list_taxes"].note


async def test_a_plan_with_a_tool_that_is_not_a_read_tool_is_refused_before_any_request(run):
    fake = ReadFake()
    with pytest.raises(smoke.PlanError, match="create_client"):
        await run(fake, cases=[Case("health_check", "health"), Case("create_client", "record", args=lambda f: {"name": "x"})])
    with pytest.raises(smoke.PlanError, match="export_invoice_pdf"):
        await run(fake, cases=[Case("export_invoice_pdf", "record")])
    assert fake.mock.requests == []


# --------------------------------------------------------------------------
# The guard and the session
# --------------------------------------------------------------------------


async def test_the_guard_is_in_read_mode_so_a_write_never_leaves_and_no_delete_tool_exists(make_settings):
    fake = ReadFake()
    async with smoke.open_session(make_settings(destructive=True), transport=fake.mock.transport, pace=0) as session:
        assert session.guard.mode == "read" and session.guard.manifest is None
        names = {tool.name for tool in await session.tools.list_tools()}
        assert "create_client" in names and "export_invoice_pdf" in names  # registered, but the guard stops them
        assert not {name for name in names if name.startswith("delete_")}  # destructive tools are not registered
        with pytest.raises(RuntimeError, match="not one of the tools this script may call"):
            await session.call("create_client", {"name": "x", "location_name": "y"})
        # past the allowlist the guard still refuses a write, before anything is sent
        unrestricted = smoke.ToolSession(session.tools, session.guard, session.pacer, TEST_API_KEY)
        with pytest.raises(smoke.GuardTripped, match="blocked PATCH /clients/9501: read mode allows only GET"):
            await unrestricted.call("update_client", {"client_id": 9501, "alternate_name": "x"})
        with pytest.raises(smoke.GuardTripped, match="the invoice PDF download is recorded on the invoice as an export event"):
            await unrestricted.call("export_invoice_pdf", {"invoice_id": uid(9)})
        assert len(session.guard.violations) == 2
    assert fake.mock.requests == []


async def test_call_result_returns_the_raw_result_and_keeps_the_checks_of_call(make_settings):
    fake = ReadFake()
    async with smoke.open_session(make_settings(destructive=True), transport=fake.mock.transport, pace=0) as session:
        result = await session.call_result("list_ticket_types", {})
        assert not result.is_error and result.structured_content["count"] == 1  # the raw CallToolResult
        assert (await session.call("list_ticket_types", {}))["count"] == 1  # call() is call_result() plus the dict check
        with pytest.raises(RuntimeError, match="create_client is not one of the tools this script may call"):
            await session.call_result("create_client", {"name": "x", "location_name": "y"})
        unrestricted = smoke.ToolSession(session.tools, session.guard, session.pacer, TEST_API_KEY)
        with pytest.raises(smoke.GuardTripped, match="read mode never sends it"):
            await unrestricted.call_result("export_invoice_pdf", {"invoice_id": uid(9)})
        with pytest.raises(smoke.ToolFailed, match="invoice_id: expected a GUID") as failed:
            await unrestricted.call_result("export_invoice_pdf", {"invoice_id": "INV-1"})  # refused locally: nothing sent
        assert not failed.value.sent
    assert [r.path for r in fake.mock.requests] == ["/v1/tickets/types", "/v1/tickets/types"]


async def test_a_guard_refusal_stops_the_run_and_the_rest_is_not_run(run, monkeypatch):
    class Strict(LiveGuard):
        def check(self, request):
            if request.url.path.endswith("/tickets/statuses"):
                message = "blocked GET /tickets/statuses: test"
                self.violations.append(message)
                raise GuardViolation(message)
            super().check(request)

    monkeypatch.setattr(smoke, "LiveGuard", Strict)
    fake = ReadFake()
    report = await run(fake)
    found = outcomes(report)
    assert not report.ok and report.refused == 1
    assert found["list_ticket_statuses"].outcome == smoke.FAILED
    assert "the guard refused the request" in found["list_ticket_statuses"].note
    assert found["list_ticket_types"].outcome == smoke.SKIPPED and "not run" in found["list_ticket_types"].note
    assert found["health_check"].outcome == smoke.OK  # what ran before the refusal stays
    assert not any(r.path == "/v1/tickets/types" for r in fake.mock.requests)


async def test_the_requests_are_paced_one_second_apart(run, monkeypatch):
    class Time:
        def __init__(self):
            self.now, self.sleeps = 100.0, []

        def clock(self):
            return self.now

        async def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds

    time = Time()
    monkeypatch.setattr(smoke, "Pacer", lambda interval: _env.Pacer(interval, clock=time.clock, sleep=time.sleep))
    assert smoke.PACE_SECONDS == 1.0
    report = await run(ReadFake(), pace=smoke.PACE_SECONDS)
    assert report.ok and report.requests == 53
    assert time.sleeps == [1.0] * 52  # one second between two requests, none before the first


# --------------------------------------------------------------------------
# The shape checks
# --------------------------------------------------------------------------

GOOD_PAGE = {"items": [{"Id": 1}], "count": 1, "total_count": 1, "has_more": False, "next_cursor": None, "page_size": 50, "filters": {}}


@pytest.mark.parametrize(
    "change, problem",
    [
        ({"items": "x"}, "items is not a list"),
        ({"count": 2}, "count is not len"),
        ({"count": True}, "count is not len"),
        ({"total_count": "1"}, "total_count is not an integer"),
        ({"total_count": None}, "total_count is not an integer"),
        ({"total_count": 7}, "only page but count=1 differs from total_count=7"),
        ({"has_more": "no"}, "has_more is not a boolean"),
        ({"has_more": True, "total_count": 9}, "has_more is true but there is no next_cursor"),
        ({"page_size": 0}, "page_size is not a positive integer"),
    ],
)
def test_check_paged_names_what_is_wrong(change, problem):
    with pytest.raises(ShapeError, match=problem):
        smoke.check_paged({**GOOD_PAGE, **change})


def test_check_paged_accepts_a_page_with_more_to_come_and_an_empty_single_page():
    more = {**GOOD_PAGE, "has_more": True, "next_cursor": "c1", "total_count": 9}
    assert smoke.check_paged(more) == "count=1 total_count=9 (more pages)"
    empty = {**GOOD_PAGE, "items": [], "count": 0, "total_count": 0}
    assert smoke.check_paged(empty) == "count=0 total_count=0"
    with pytest.raises(ShapeError, match="no rows"):
        smoke.check_paged(empty, nonempty=True)


def test_check_list_all_search_record_and_health():
    assert smoke.check_list({"items": [1, 2], "count": 2}) == "count=2"
    with pytest.raises(ShapeError, match="count is not len"):
        smoke.check_list({"items": [1], "count": 2})
    everything = {"items": [{"Id": 1}], "count": 1, "total_count": 1, "truncated": False, "complete_scan": True, "count_mismatch": False}
    assert smoke.check_all(everything, complete=True) == "count=1 total_count=1"
    for change, problem in (
        ({"truncated": 0}, "not a boolean"),
        ({"count_mismatch": True}, "count_mismatch"),
        ({"truncated": True}, "not complete"),
        ({"total_count": "1"}, "total_count is not an integer"),
    ):
        with pytest.raises(ShapeError, match=problem):
            smoke.check_all({**everything, **change}, complete=True)
    assert smoke.check_search({**everything, "matched": 1, "scanned": 4}) == "count=1 total_count=1 matched=1 scanned=4"
    with pytest.raises(ShapeError, match="not in order"):
        smoke.check_search({**everything, "matched": 0, "scanned": 4})
    assert smoke.check_record({"Id": 5}, 5) == "Id matches" and smoke.check_record({"Id": "ABC"}, "abc") == "Id matches"
    assert smoke.check_record({"Id": 5}) == "Id present"
    with pytest.raises(ShapeError, match="no Id"):
        smoke.check_record({"Name": "x"})
    with pytest.raises(ShapeError, match="not the one that was asked for"):
        smoke.check_record({"Id": 6}, 5)
    assert smoke.check_health({"ok": True, "total_clients": 40}) == "total_clients=40"
    with pytest.raises(ShapeError, match="health_check says ok is not true: down"):
        smoke.check_health({"ok": False, "error": "down"})


# --------------------------------------------------------------------------
# Scope detection and the command line
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, scope",
    [
        ("Gorelo rejected list_projects (HTTP 403, code 080203): the API key does not have the 'Project' scope. Grant it", "Project"),
        ("Gorelo rejected list_forms (HTTP 403, code 080203): the API key does not have a scope this tool needs. Grant", "unknown"),
        ("Gorelo rejected list_forms (HTTP 403, code 070101): forbidden", None),
        ("Gorelo rejected list_clients (HTTP 400, code 070101): PageSize must be between 1 and 200.", None),
        ("page_size: expected an integer", None),
    ],
)
def test_scope_name_reads_the_missing_scope_from_gorelos_403(text, scope):
    assert smoke.scope_name(text) == scope


def test_tool_failed_knows_its_status_and_whether_gorelo_refused_without_applying():
    refused = smoke.ToolFailed("create_ticket", "Gorelo rejected create_ticket (HTTP 400, code 070101): Title: bad", sent=True)
    assert refused.status == 400 and refused.rejected and refused.detail == "Title: bad" and refused.scope is None
    unsure = smoke.ToolFailed("create_ticket", "Gorelo did not confirm create_ticket (the request timed out). The change may or may not", sent=True)
    assert unsure.status is None and not unsure.rejected and unsure.detail == unsure.text
    server = smoke.ToolFailed("create_ticket", "Gorelo rejected create_ticket (HTTP 500, code 070500): x. Gorelo may have applied the change", sent=True)
    assert server.status == 500 and not server.rejected
    local = smoke.ToolFailed("create_ticket", "title: must not be empty", sent=False)
    assert local.status is None and not local.rejected and not local.sent


def test_the_command_line_runs_the_smoke_and_maps_the_report_to_an_exit_status(monkeypatch):
    async def passed(**options):
        assert options == {}
        return smoke.SmokeReport()

    async def failed(**options):
        return smoke.SmokeReport(rows=[smoke.Row("x", smoke.FAILED, "no")])

    monkeypatch.setattr(smoke, "run_smoke", passed)
    assert smoke.main([]) == 0
    monkeypatch.setattr(smoke, "run_smoke", failed)
    assert smoke.main([]) == 1
    with pytest.raises(SystemExit) as stop:
        smoke.main(["--pace", "0"])  # there is deliberately no way to change the pace from the command line
    assert stop.value.code == 2


def test_the_default_output_is_flushed_so_progress_shows_through_a_pipe(capsys):
    assert smoke.run_smoke.__kwdefaults__["echo"] is smoke.emit
    smoke.emit("a line")
    assert capsys.readouterr().out == "a line\n"


def test_main_runs_the_smoke_end_to_end_and_its_exit_status_follows_the_result(monkeypatch, capsys, make_settings):
    import functools

    real = smoke.run_smoke
    for fake, status, last in (
        (ReadFake(), 0, "result: PASSED"),
        (ReadFake(fail={"/v1/taxes": error_envelope(500, [("070500", "boom")])}), 1, "result: FAILED"),
    ):
        monkeypatch.setattr(
            smoke, "run_smoke", functools.partial(real, settings=make_settings(), transport=fake.mock.transport, pace=0)
        )
        assert smoke.main([]) == status
        out = capsys.readouterr().out.splitlines()
        assert out[0].startswith("tool") and out[-1] == last


def test_the_help_says_what_the_smoke_does_and_offers_no_flags(capsys):
    with pytest.raises(SystemExit) as stop:
        smoke.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())  # argparse wraps its lines
    assert stop.value.code == 0 and "every read tool" in out and "GET only" in out and "--pace" not in out


def test_an_interrupt_ends_the_command_with_status_130(monkeypatch, capsys):
    async def stopped(**options):
        raise KeyboardInterrupt

    monkeypatch.setattr(smoke, "run_smoke", stopped)
    assert smoke.main([]) == 130
    assert capsys.readouterr().err == "interrupted\n"


def test_the_command_line_reports_a_missing_api_key_without_a_traceback(monkeypatch, capsys):
    def no_key(*args, **kwargs):
        raise _env.EnvError("GORELO_API_KEY is not set in somewhere")

    monkeypatch.setattr(smoke, "live_settings", no_key)
    assert smoke.main([]) == 2
    assert "cannot start: GORELO_API_KEY is not set" in capsys.readouterr().err
