"""scripts/live/guard.py: every rule of the live request guard, tested with real httpx.Request objects.

Offline. The guard is an httpx request hook: these tests build the requests the harness would send (JSON and
multipart), call guard.check() (the synchronous core of the hook) and also drive the real async hook through an
httpx client on a mock transport. "Run-created" ids come from a manifest in a temporary directory.
"""

import json
import re
from pathlib import Path

import httpx
import pytest
from conftest import TEST_API_KEY, call_tool, call_tool_error, call_tool_raw, envelope, paged_envelope, path_params_for, uid
from site_helper import site_config_env  # noqa: F401  (autouse: points GORELO_SITE_CONFIG at invented values)

from gorelo_client import normalize_op_key
from scripts.live import guard as guard_module
from scripts.live.guard import _CREATES, APPROVED_INVOICE_LIMIT, GuardViolation, LiveGuard
from scripts.live.manifest import Manifest
from spec import SpecIndex, load_spec_index

pytestmark = pytest.mark.anyio

RUN = "MCPTEST-20991002101500"
BASE = "https://api.usw.gorelo.io/v1"
TEST_CLIENT, SECOND, OPERATOR_CONTACT, OPERATOR_USER = 9501, 9502, 9600, 9700
OTHER_USER = 9202  # another technician: never assigned, never a time entry user

RUN_CLIENT = 8200
RUN_CONTACT = 8301
TICKET_OPERATOR = uid(11)  # created for Second with the operator contact
TICKET_NONE = uid(12)  # created without a contact
TICKET_RUN_CONTACT = uid(13)  # created with a run-created contact
TICKET_CC = uid(14)  # created without a contact but with a run-created CC
TICKET_UNKNOWN = uid(15)  # the manifest has no contact_id for it
COMMENT = uid(21)
TIME_ENTRY = 777
ITEM = uid(31)
UPTIME = uid(41)
PROJECT = uid(51)
SECTION = uid(52)
TASK = uid(53)
PROJECT_COMMENT = uid(54)
OTHER_TASK = uid(55)  # a second task of the run's project
INVOICE = uid(91)  # an invoice the run created as a Draft (manifest details status_id 1)
INVOICE_APPROVED = uid(92)  # run-created, but recorded with status_id 5
INVOICE_NO_STATUS = uid(93)  # run-created, recorded without a status_id
INVOICE_ITEM = uid(94)  # a catalog item: it belongs to no run
# The conversations this run created: a side conversation (a number) and an approval (a UUID) on every run ticket,
# each recorded with the ticket it belongs to, and the same for the run's task (recorded with its task_id).
SIDE_OF = {TICKET_OPERATOR: 701, TICKET_NONE: 702, TICKET_RUN_CONTACT: 703, TICKET_CC: 704, TICKET_UNKNOWN: 705}
APPROVAL_OF = {
    TICKET_OPERATOR: uid(81), TICKET_NONE: uid(82), TICKET_RUN_CONTACT: uid(83), TICKET_CC: uid(84), TICKET_UNKNOWN: uid(85),
}
TASK_SIDE, TASK_APPROVAL = 711, uid(86)
OTHER_TASK_SIDE, OTHER_TASK_APPROVAL = 712, uid(87)
STRANGER_SIDE, STRANGER_APPROVAL = 799, uid(98)  # conversations of a customer's ticket: this run never created them
FOREIGN = uid(99)  # a customer's record: this run never created it
APPROVED_LABEL = f"{RUN} approved invoice"  # the label of the one Approved invoice a run announces
DROP = object()


@pytest.fixture
def manifest(tmp_path: Path) -> Manifest:
    run = Manifest(RUN, tmp_path / "run.json")
    label = run.label
    run.created("client", RUN_CLIENT, label("temporary client"))
    run.created("contact", RUN_CONTACT, label("contact"))
    run.created("ticket", TICKET_OPERATOR, label("ticket"), {"client_id": SECOND, "contact_id": OPERATOR_CONTACT})
    run.created("ticket", TICKET_NONE, label("ticket"), {"client_id": TEST_CLIENT, "contact_id": None})
    run.created("ticket", TICKET_RUN_CONTACT, label("ticket"), {"client_id": TEST_CLIENT, "contact_id": RUN_CONTACT})
    run.created("ticket", TICKET_CC, label("ticket"), {"client_id": TEST_CLIENT, "contact_id": None, "cc_contact_ids": [RUN_CONTACT]})
    run.created("ticket", TICKET_UNKNOWN, label("ticket"), {"client_id": TEST_CLIENT})
    run.created("comment", COMMENT, label("comment"), {"ticket_id": TICKET_NONE})
    run.created("time_entry", TIME_ENTRY, label("time entry"))
    run.created("item", ITEM, label("item"))
    run.created("uptime", UPTIME, label("uptime check"))
    run.created("project", PROJECT, label("project"))
    run.created("section", SECTION, label("section"), {"project_id": PROJECT})
    run.created("task", TASK, label("task"), {"project_id": PROJECT})
    run.created("project_comment", PROJECT_COMMENT, label("project comment"), {"project_id": PROJECT})
    run.created("task", OTHER_TASK, label("other task"), {"project_id": PROJECT})
    for ticket, side in SIDE_OF.items():
        run.created("side_conversation", side, label("side conversation"), {"ticket_id": ticket})
    for ticket, approval in APPROVAL_OF.items():
        run.created("approval", approval, label("approval"), {"ticket_id": ticket})
    run.created("side_conversation", TASK_SIDE, label("task side conversation"), {"project_id": PROJECT, "task_id": TASK})
    run.created("approval", TASK_APPROVAL, label("task approval"), {"project_id": PROJECT, "task_id": TASK})
    run.created("side_conversation", OTHER_TASK_SIDE, label("side conversation"), {"project_id": PROJECT, "task_id": OTHER_TASK})
    run.created("approval", OTHER_TASK_APPROVAL, label("approval"), {"project_id": PROJECT, "task_id": OTHER_TASK})
    run.created("invoice", INVOICE, label("invoice"), {"status_id": 1, "number": 1042, "display_number": "INV-1042"})
    run.created("invoice", INVOICE_APPROVED, label("approved invoice"), {"status_id": 5, "number": 1043, "display_number": "INV-1043"})
    run.created("invoice", INVOICE_NO_STATUS, label("invoice without a status"), {"number": 1044})
    return run


@pytest.fixture
def guard(manifest) -> LiveGuard:
    return LiveGuard("write", manifest)


@pytest.fixture
def cleanup_guard(manifest) -> LiveGuard:
    return LiveGuard("write", manifest, cleanup=True)


@pytest.fixture
def option_guard(manifest) -> LiveGuard:
    """A write guard with allow_approved_invoice, on the manifest that holds the Draft, the Approved and the status-less invoice."""
    return LiveGuard("write", manifest, allow_approved_invoice=True)


@pytest.fixture
def option_cleanup_guard(manifest) -> LiveGuard:
    """What cleanup --void-approved builds: a cleanup guard that also allows the void of an invoice recorded as Approved."""
    return LiveGuard("write", manifest, cleanup=True, allow_approved_invoice=True)


@pytest.fixture
def approved_manifest(tmp_path: Path) -> Manifest:
    """The manifest of a run that announced its one Approved invoice and has created nothing yet."""
    run = Manifest(RUN, tmp_path / "approved.json")
    run.intent("invoice", APPROVED_LABEL, {"status_id": 5})
    return run


@pytest.fixture
def approved_guard(approved_manifest) -> LiveGuard:
    return LiveGuard("write", approved_manifest, allow_approved_invoice=True)


@pytest.fixture
def read_guard() -> LiveGuard:
    return LiveGuard("read", None)


def build(method, path, body=None, **kwargs):
    """A real httpx.Request for https://api.usw.gorelo.io/v1<path>; `body` goes out as a JSON body."""
    return httpx.Request(method, BASE + path, json=body, **kwargs)


def raw(method, path, content, content_type="application/json"):
    """A request with exactly these body bytes (for bodies json= could never produce)."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    return httpx.Request(method, BASE + path, content=data, headers={"content-type": content_type})


def upload(item_type, item_id, *, filename="note.txt", content=b"hello", **extra):
    """A multipart attachment upload like the upload_attachment tool sends."""
    data = {"itemType": item_type, "itemId": item_id, **extra}
    return httpx.Request("POST", BASE + "/attachments", files={"file": (filename, content, "text/plain")}, data=data)


def make(base, **overrides):
    body = dict(base)
    for key, value in overrides.items():
        if value is DROP:
            body.pop(key, None)
        else:
            body[key] = value
    return body


def refused(guard, request, match):
    with pytest.raises(GuardViolation, match=match) as caught:
        guard.check(request)
    return caught.value


# --------------------------------------------------------------------------
# read mode and GET
# --------------------------------------------------------------------------

GET_PATHS = [
    "/clients",
    "/clients/9501",
    "/clients/9501/locations",
    "/contacts",
    "/contacts/9600",
    f"/tickets/{TICKET_NONE}",
    "/tickets/statuses",
    "/tickets/types",
    "/tickets/tags",
    f"/tickets/{TICKET_NONE}/comments",
    f"/tickets/{TICKET_NONE}/comments/{COMMENT}",
    f"/tickets/{TICKET_NONE}/conversations",
    "/organization/users",
    "/organization/groups",
    "/invoices",
    f"/invoices/{INVOICE}",
    f"/invoices/{FOREIGN}",
    "/items",
    "/items/categories",
    f"/items/{ITEM}",
    "/uptime",
    f"/uptime/{UPTIME}",
    "/time-entries",
    "/time-entries/777",
    "/projects",
    "/projects/tags",
    f"/projects/{PROJECT}",
    f"/projects/{PROJECT}/tasks/{TASK}/comments",
    "/forms",
    "/forms/a-form-id/responses",
    "/assets/agents",
    f"/assets/agents/{uid(5)}",
    "/assets/custom",
    "/contracts",
    "/contracts/5",
    "/taxes",
    "/work-types",
    "/billing-roles",
    "/tickets?Query=2029&PageSize=5&StatusIds=1,2",
    "/clients?PageSize=200",
]


@pytest.mark.parametrize("path", GET_PATHS)
def test_read_mode_allows_get(read_guard, path):
    read_guard.check(build("GET", path))
    assert not read_guard.tripped and read_guard.allowed_count == 1


@pytest.mark.parametrize("path", GET_PATHS)
def test_write_mode_allows_get_too(guard, path):
    guard.check(build("GET", path))


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("POST", "/clients", {"Name": "MCPTEST-x", "Location": {"Name": "HQ"}}),
        ("PATCH", f"/clients/{TEST_CLIENT}", {"AlternateName": "x"}),
        ("PATCH", f"/contacts/{RUN_CONTACT}", {"ClientId": TEST_CLIENT}),
        ("POST", f"/tickets/{TICKET_NONE}/comments", {"Body": "x", "ConversationTypeId": 2}),
        ("DELETE", f"/tickets/{TICKET_NONE}", None),
        ("DELETE", "/assets/agents/" + uid(5), None),
        ("POST", "/alerts", {"ClientId": TEST_CLIENT, "Name": "x", "Resource": "y"}),
        ("POST", "/invoices", {"ClientId": TEST_CLIENT, "StatusId": 1, "LineItems": [{"ItemId": INVOICE_ITEM, "Quantity": 1}]}),
        ("DELETE", f"/invoices/{INVOICE}", None),
    ],
)
def test_read_mode_blocks_every_write(read_guard, method, path, body):
    refused(read_guard, build(method, path, body), "read mode allows only GET")


@pytest.mark.parametrize("method", ["PUT", "HEAD", "OPTIONS", "TRACE", "CONNECT", "get "])
def test_methods_the_api_client_never_uses_are_blocked_in_both_modes(read_guard, guard, method):
    for subject in (read_guard, guard):
        with pytest.raises(GuardViolation, match="read mode allows only GET|is not used with the Gorelo API"):
            subject.check(httpx.Request(method, BASE + "/clients"))


def test_the_method_is_compared_case_insensitively(read_guard):
    read_guard.check(httpx.Request("get", BASE + "/clients"))
    refused(read_guard, httpx.Request("post", BASE + "/clients"), "read mode allows only GET")


def test_the_invoice_pdf_is_never_allowed_in_read_mode_not_even_for_a_run_created_invoice(read_guard):
    for invoice in (uid(5), INVOICE):
        message = refused(read_guard, build("GET", f"/invoices/{invoice}/pdf"), "export event")
        assert message.label == f"GET /invoices/{invoice}/pdf" and "read mode never sends it" in str(message)


def test_the_invoice_pdf_is_allowed_in_write_mode_only_for_a_run_created_draft(guard, cleanup_guard):
    for subject in (guard, cleanup_guard):
        subject.check(build("GET", f"/invoices/{INVOICE}/pdf"))
        subject.check(build("GET", f"/invoices/{INVOICE.upper()}/pdf"))  # a UUID is not case sensitive
        for invoice in (uid(5), FOREIGN, ITEM, TICKET_NONE):  # a stranger's invoice, or another kind's id
            message = refused(subject, build("GET", f"/invoices/{invoice}/pdf"), "export event")
            assert message.label == f"GET /invoices/{invoice}/pdf"
            assert "only an invoice this run created may be exported" in str(message)
            assert "is not a test invoice created by this run" in str(message)
    assert guard.allowed_count == 2 and len(guard.violations) == 4


@pytest.mark.parametrize(
    "invoice, shown",
    [(INVOICE_APPROVED, "5"), (INVOICE_NO_STATUS, "missing")],
)
def test_the_invoice_pdf_of_a_run_created_invoice_that_is_not_recorded_as_a_draft_is_blocked(guard, cleanup_guard, invoice, shown):
    for subject in (guard, cleanup_guard):
        message = refused(subject, build("GET", f"/invoices/{invoice}/pdf"), "export event")
        assert "only a Draft this run created may be exported" in str(message)
        assert f"is not recorded as a Draft (the manifest details status_id is {shown}, not 1)" in str(message)


@pytest.mark.parametrize("path", ["/invoices/abc/pdf", "/invoices/5/pdf", f"/invoices/{INVOICE}x/pdf", "/invoices/pdf/pdf"])
def test_the_invoice_pdf_of_an_id_that_is_not_a_uuid_is_blocked(guard, path):
    refused(guard, build("GET", path), "is not a valid test invoice id")


def test_the_invoice_pdf_is_recognized_by_its_shape_so_a_renamed_placeholder_is_not_a_plain_read(manifest):
    data = json.loads((Path(__file__).resolve().parent.parent / "spec" / "spec_index.json").read_text(encoding="utf-8"))
    entry = data["ops"].pop("GET /v1/invoices/{invoiceId}/pdf")
    renamed = "GET /v1/invoices/{id}/pdf"
    data["ops"][renamed] = {**entry, "path": "/v1/invoices/{id}/pdf", "path_params": {"id": entry["path_params"]["invoiceId"]}}
    spec = SpecIndex(data)
    read = LiveGuard("read", None, spec=spec)
    write = LiveGuard("write", manifest, spec=spec)
    refused(read, build("GET", f"/invoices/{INVOICE}/pdf"), "read mode never sends it")
    write.check(build("GET", f"/invoices/{INVOICE}/pdf"))
    refused(write, build("GET", f"/invoices/{FOREIGN}/pdf"), "path id .* is not a test invoice created by this run")
    assert write.rule_table()[renamed].startswith("write mode only, and only a run-created invoice")


def test_a_get_with_a_body_is_blocked(read_guard, guard):
    for subject in (read_guard, guard):
        refused(subject, build("GET", "/clients", {"a": 1}), "must not carry a body")


@pytest.mark.parametrize(
    "path",
    ["/widgets", "/clients/9501/secrets", "/tickets/x/y/z", f"/tickets/{TICKET_NONE}/comments/{COMMENT}/extra", "/organization", "/", "/assets"],
)
def test_a_get_path_that_is_not_a_gorelo_operation_is_blocked(read_guard, guard, path):
    for subject in (read_guard, guard):
        with pytest.raises(GuardViolation, match="not a known Gorelo operation|path segment"):
            subject.check(build("GET", path))


def test_the_modes_and_their_constructor_arguments_are_checked(manifest):
    with pytest.raises(ValueError, match="mode must be"):
        LiveGuard("delete", manifest)
    with pytest.raises(ValueError, match="needs the run manifest"):
        LiveGuard("write", None)
    with pytest.raises(ValueError, match="only makes sense in write mode"):
        LiveGuard("read", manifest, cleanup=True)
    assert LiveGuard("read", None).mode == "read" and LiveGuard("read", manifest).manifest is manifest


def test_a_read_guard_needs_no_manifest_and_never_reaches_a_rule_that_does():
    guard = LiveGuard("read", None)
    for method in ("POST", "PATCH", "DELETE"):
        refused(guard, build(method, f"/tickets/{TICKET_NONE}", {"Title": "x"}), "read mode")


# --------------------------------------------------------------------------
# where the request goes, and what the path looks like
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/v1/clients",
        "https://api.usw.gorelo.io.evil.example/v1/clients",
        "http://api.usw.gorelo.io/v1/clients",
        "https://api.usw.gorelo.io:8443/v1/clients",
        "https://user:pass@api.usw.gorelo.io/v1/clients",
        "https://api.eu.gorelo.io/v1/clients",
    ],
)
def test_only_https_requests_to_the_gorelo_api_host_are_allowed(read_guard, guard, url):
    for subject in (read_guard, guard):
        refused(subject, httpx.Request("GET", url), "only https requests to api.usw.gorelo.io are allowed")


def test_the_allowed_hosts_can_be_changed_for_tests(manifest):
    other = LiveGuard("read", None, allowed_hosts={"gorelo.test"})
    other.check(httpx.Request("GET", "https://gorelo.test/v1/clients"))
    refused(other, httpx.Request("GET", BASE + "/clients"), "only https requests to gorelo.test")
    default_port = LiveGuard("read", None)
    default_port.check(httpx.Request("GET", "https://api.usw.gorelo.io:443/v1/clients"))


@pytest.mark.parametrize("url", ["https://api.usw.gorelo.io/v2/clients", "https://api.usw.gorelo.io/clients", "https://api.usw.gorelo.io/v1", "https://api.usw.gorelo.io/"])
def test_a_path_outside_the_v1_base_is_blocked(read_guard, url):
    refused(read_guard, httpx.Request("GET", url), "outside /v1/")


@pytest.mark.parametrize(
    "method, path",
    [
        ("DELETE", "/tickets/a%2Fb"),  # an encoded slash
        ("DELETE", "/tickets/%2e%2e/assets"),  # encoded dots: a decoding hop could collapse them
        ("DELETE", "/tickets/%2E%2E%2Fassets%2Fagents%2F" + uid(5)),
        ("DELETE", "/tickets//x"),  # an empty segment
        ("DELETE", f"/tickets/{TICKET_NONE}/"),  # a trailing slash
        ("DELETE", "/tickets/a\\b"),
        ("DELETE", "/tickets/a b"),
        ("DELETE", "/tickets/x;y"),
        ("DELETE", "/tickets/x:y"),
        ("DELETE", "/tickets/\u00e9"),
        ("GET", "/tickets/%2e%2e"),
        ("GET", "/clients//9501"),
        ("GET", "/clients/9501/"),
        ("POST", "/tickets/%00/comments"),
    ],
)
def test_an_odd_path_segment_is_blocked_before_it_is_looked_up(guard, method, path):
    refused(guard, build(method, path, {"x": 1} if method == "POST" else None), "path segment .* is not allowed")


def test_the_guard_judges_the_path_httpx_actually_sends(guard):
    # httpx collapses dot segments before the hook runs: this one reaches the wire as DELETE /v1/tickets/<foreign>
    request = build("DELETE", f"/tickets/{FOREIGN}/comments/..")
    assert request.url.path == f"/v1/tickets/{FOREIGN}"
    refused(guard, request, "is not a ticket created by this run")
    # and this one as DELETE /v1/assets/agents/<id>
    request = build("DELETE", f"/tickets/../assets/agents/{uid(5)}")
    assert request.url.path == f"/v1/assets/agents/{uid(5)}"
    refused(guard, request, "writes under /assets/ are never allowed")


@pytest.mark.parametrize("name", ["X-HTTP-Method-Override", "x-http-method", "X-Method-Override"])
def test_a_method_override_header_is_blocked(guard, read_guard, name):
    for subject, method in ((read_guard, "GET"), (guard, "POST")):
        refused(subject, httpx.Request(method, BASE + "/clients", headers={name: "DELETE"}), "method override header")


def test_a_host_header_for_another_host_is_blocked(guard, read_guard):
    for subject in (read_guard, guard):
        refused(subject, httpx.Request("GET", BASE + "/clients", headers={"Host": "evil.example"}), "Host header names another host")
        refused(subject, httpx.Request("GET", BASE + "/clients", headers={"Host": "api.usw.gorelo.io.evil.example"}), "Host header")
        subject.check(httpx.Request("GET", BASE + "/clients", headers={"Host": "API.usw.gorelo.io:443"}))
        subject.check(httpx.Request("GET", BASE + "/clients"))


@pytest.mark.parametrize("path", [f"/Assets/agents/{uid(5)}", f"/ASSETS/custom/{uid(5)}", "/aSsEtS/agents/1"])
def test_the_assets_rule_does_not_depend_on_letter_case(guard, cleanup_guard, path):
    for subject in (guard, cleanup_guard):
        refused(subject, build("DELETE", path), "writes under /assets/ are never allowed")


@pytest.mark.parametrize(
    "method, path",
    [
        ("DELETE", f"/TICKETS/{TICKET_NONE}"),
        ("GET", "/Clients"),
        ("PATCH", f"/Clients/{RUN_CLIENT}"),
        ("PATCH", f"/Contacts/{RUN_CONTACT}"),
    ],
)
def test_a_path_in_another_letter_case_is_not_a_known_operation(guard, read_guard, method, path):
    refused(guard, build(method, path, {"AlternateName": "x"} if method == "PATCH" else None), "not a known Gorelo operation")


def test_a_write_with_a_query_string_is_blocked(guard):
    refused(guard, build("POST", "/clients?x=1", {"Name": "MCPTEST-x", "Location": {"Name": "HQ"}}), "must not carry a query string")
    refused(guard, build("DELETE", f"/tickets/{TICKET_NONE}?force=true"), "must not carry a query string")
    refused(guard, build("DELETE", f"/tickets/{TICKET_NONE}?"), "must not carry a query string")


def test_a_delete_with_a_body_is_blocked(guard):
    refused(guard, build("DELETE", f"/tickets/{TICKET_NONE}", {"force": True}), "takes no body")


def test_an_operation_the_spec_does_not_list_is_blocked_and_one_the_allowlist_does_not_name_too(manifest):
    data = json.loads((Path(__file__).resolve().parent.parent / "spec" / "spec_index.json").read_text(encoding="utf-8"))
    body = {"content_type": "application/json", "fields": {"Name": {"type": "string"}}, "required": []}
    data["ops"]["POST /v1/widgets"] = {"method": "POST", "path": "/v1/widgets", "body": body}
    data["ops"]["GET /v1/widgets"] = {"method": "GET", "path": "/v1/widgets"}
    guard = LiveGuard("write", manifest, spec=SpecIndex(data))
    refused(guard, build("POST", "/widgets", {"Name": "x"}), "not on the live-test allowlist")
    guard.check(build("GET", "/widgets"))  # a new GET operation is a read: allowed
    refused(LiveGuard("write", manifest), build("GET", "/widgets"), "not a known Gorelo operation")


def test_an_operation_outside_the_v1_base_is_ignored_by_the_routes(manifest):
    data = json.loads((Path(__file__).resolve().parent.parent / "spec" / "spec_index.json").read_text(encoding="utf-8"))
    data["ops"]["GET /v2/things"] = {"method": "GET", "path": "/v2/things"}
    guard = LiveGuard("write", manifest, spec=SpecIndex(data))
    assert "GET /v2/things" not in guard.rule_table() or guard.rule_table()["GET /v2/things"] == "allowed"
    refused(guard, httpx.Request("GET", "https://api.usw.gorelo.io/v2/things"), "outside /v1/")


# --------------------------------------------------------------------------
# DELETE
# --------------------------------------------------------------------------

RUN_DELETES = [
    f"/tickets/{TICKET_NONE}",
    f"/tickets/{TICKET_NONE}/comments/{COMMENT}",
    f"/time-entries/{TIME_ENTRY}",
    f"/items/{ITEM}",
    f"/uptime/{UPTIME}",
    f"/projects/{PROJECT}",
    f"/projects/{PROJECT}/sections/{SECTION}",
    f"/projects/{PROJECT}/tasks/{TASK}",
    f"/projects/{PROJECT}/comments/{PROJECT_COMMENT}",
    f"/projects/{PROJECT}/tasks/{TASK}/comments/{PROJECT_COMMENT}",
    f"/clients/{RUN_CLIENT}",
    f"/contacts/{RUN_CONTACT}",
]


@pytest.mark.parametrize("path", RUN_DELETES)
def test_delete_of_a_run_created_record_is_allowed(guard, cleanup_guard, path):
    guard.check(build("DELETE", path))
    cleanup_guard.check(build("DELETE", path))


@pytest.mark.parametrize(
    "path",
    [
        f"/tickets/{FOREIGN}",
        f"/tickets/{TICKET_NONE}/comments/{FOREIGN}",
        f"/tickets/{FOREIGN}/comments/{COMMENT}",  # the comment is the run's, its ticket is not
        "/time-entries/778",
        f"/items/{FOREIGN}",
        f"/uptime/{FOREIGN}",
        f"/projects/{FOREIGN}",
        f"/projects/{PROJECT}/sections/{FOREIGN}",
        f"/projects/{FOREIGN}/sections/{SECTION}",
        f"/projects/{PROJECT}/tasks/{FOREIGN}",
        f"/projects/{FOREIGN}/tasks/{TASK}",
        f"/projects/{PROJECT}/comments/{FOREIGN}",
        f"/projects/{PROJECT}/tasks/{TASK}/comments/{FOREIGN}",
        f"/projects/{PROJECT}/tasks/{FOREIGN}/comments/{PROJECT_COMMENT}",
        "/clients/9501",
        "/clients/9502",
        "/clients/9801",
        "/contacts/9600",
        "/contacts/9900",
        "/contacts/150002",
        f"/tickets/{ITEM}",  # another kind's id in a ticket path
        f"/time-entries/{TICKET_NONE}",
        f"/tickets/{TICKET_NONE}/comments/{ITEM}",
    ],
)
def test_delete_of_anything_the_run_did_not_create_is_blocked(guard, path):
    refused(guard, build("DELETE", path), "not a .* created by this run|is not a valid .* id|was not created by this run|never deleted")


@pytest.mark.parametrize(
    "path",
    [f"/assets/agents/{uid(5)}", f"/assets/agents/{TICKET_NONE}", f"/assets/custom/{uid(6)}", "/assets/agents/5", "/assets/other/x", "/assets"],
)
def test_delete_under_assets_is_never_allowed(guard, cleanup_guard, path):
    for subject in (guard, cleanup_guard):
        refused(subject, build("DELETE", path), "writes under /assets/ are never allowed")


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_no_write_at_all_under_assets_is_allowed(guard, method):
    refused(guard, build(method, f"/assets/agents/{uid(5)}", {"x": 1}), "writes under /assets/ are never allowed")


def test_contracts_are_never_deleted(guard, cleanup_guard):
    for subject in (guard, cleanup_guard):
        refused(subject, build("DELETE", "/contracts/5"), "never deletes contracts")


@pytest.mark.parametrize("body", [{"Name": "MCPTEST-key", "Scopes": ["Project"]}, {}, {"Name": "x", "Description": "y", "Scopes": []}])
def test_an_api_key_is_never_created_in_any_mode(guard, cleanup_guard, read_guard, body):
    for subject in (guard, cleanup_guard):
        message = refused(subject, build("POST", "/api-keys", body), "never creates API keys")
        assert message.label == "POST /api-keys" and "an API key is a credential" in str(message)
    refused(read_guard, build("POST", "/api-keys", body), "read mode allows only GET")


def test_an_api_key_is_refused_even_when_an_intent_is_open_and_through_the_real_hooks(manifest):
    manifest.intent("invoice", manifest.label("something else"))
    for strict in (LiveGuard("write", manifest, require_intents=True), LiveGuard("write", manifest, cleanup=True, require_intents=True)):
        refused(strict, build("POST", "/api-keys", {"Name": "MCPTEST-key", "Scopes": ["Project"]}), "never creates API keys")


def test_the_never_rules_are_looked_up_by_shape_so_a_renamed_placeholder_keeps_its_reason(manifest):
    data = json.loads((Path(__file__).resolve().parent.parent / "spec" / "spec_index.json").read_text(encoding="utf-8"))
    entry = data["ops"].pop("DELETE /v1/contracts/{contractId}")
    renamed_key = "DELETE /v1/contracts/{contractNumber}"
    data["ops"][renamed_key] = {**entry, "path": "/v1/contracts/{contractNumber}", "path_params": {"contractNumber": entry["path_params"]["contractId"]}}
    guard = LiveGuard("write", manifest, spec=SpecIndex(data))
    refused(guard, build("DELETE", "/contracts/5"), "never deletes contracts")
    assert guard.rule_table()[renamed_key].startswith("never: the live harness never deletes contracts")


@pytest.mark.parametrize("path", ["/clients/9501", "/clients/9502", "/contacts/9600"])
def test_the_test_clients_and_the_operator_contact_are_never_deleted_even_if_a_manifest_lists_them(tmp_path, path):
    kind, ident = path[1:-1].split("s/")  # "/clients/9501" -> client, 9501
    run = Manifest(RUN, tmp_path / "wrong.json")
    run.created(kind, int(ident), run.label("recorded by mistake"))
    for subject in (LiveGuard("write", run), LiveGuard("write", run, cleanup=True)):
        refused(subject, build("DELETE", path), "is one of the records the live tests run on and is never deleted")


def test_the_approved_leftovers_are_deleted_only_in_cleanup_mode(guard, cleanup_guard):
    for path in ("/clients/9801", "/clients/9802", "/contacts/9900"):
        cleanup_guard.check(build("DELETE", path))
        refused(guard, build("DELETE", path), "was not created by this run.*cleanup command")


@pytest.mark.parametrize(
    "path",
    ["/clients/9803", "/clients/9800", "/clients/9501", "/clients/9502", "/contacts/9901", "/contacts/9600", "/contacts/9801", "/clients/9900"],
)
def test_cleanup_mode_allows_exactly_the_approved_leftovers(cleanup_guard, path):
    refused(cleanup_guard, build("DELETE", path), "was not created by this run|never deleted")


def test_the_approved_leftovers_can_be_changed(manifest):
    guard = LiveGuard("write", manifest, cleanup=True, approved_leftovers={"client": {31000}, "contact": set()})
    guard.check(build("DELETE", "/clients/31000"))
    refused(guard, build("DELETE", "/clients/9801"), "was not created by this run")
    refused(guard, build("DELETE", "/contacts/9900"), "was not created by this run")


def test_cleanup_mode_allows_nothing_but_deletes_of_leftovers(cleanup_guard):
    refused(cleanup_guard, build("PATCH", "/clients/9801", {"AlternateName": "x"}), "path id 9801 is not allowed")
    refused(
        cleanup_guard,
        build("PATCH", "/contacts/9900", {"ClientId": TEST_CLIENT, "FirstName": "a", "LastName": "b", "PrimaryEmail": "a@example.invalid"}),
        "path contactId 9900 is not a contact created by this run",
    )
    refused(cleanup_guard, build("POST", "/alerts", {"ClientId": TEST_CLIENT, "Name": "x", "Resource": "y"}), "never posts alerts")
    refused(cleanup_guard, build("POST", "/clients", {"Name": "Acme", "Location": {"Name": "x"}}), "Name must start with")


def test_a_delete_with_an_id_of_the_wrong_shape_is_blocked(guard, cleanup_guard):
    refused(guard, build("DELETE", "/clients/abc"), "not a valid client id")
    refused(cleanup_guard, build("DELETE", "/contacts/0"), "not a valid contact id")
    refused(guard, build("DELETE", f"/clients/{uid(3)}"), "not a valid client id")


# --------------------------------------------------------------------------
# clients
# --------------------------------------------------------------------------

CLIENT_BODY = {"Name": f"{RUN} temporary client", "Location": {"Name": "HQ", "Phone": "5555550142", "PhoneCountryCode": "US"}}


def test_post_clients_allows_a_name_that_starts_with_mcptest(guard):
    guard.check(build("POST", "/clients", CLIENT_BODY))
    guard.check(build("POST", "/clients", make(CLIENT_BODY, Name="MCPTEST-any suffix")))


@pytest.mark.parametrize(
    "overrides",
    [
        {"Name": "Acme"},
        {"Name": "mcptest-lowercase"},
        {"Name": " MCPTEST-leading space"},
        {"Name": "MCPTEST"},
        {"Name": 123},
        {"Name": None},
        {"Name": DROP},
    ],
)
def test_post_clients_blocks_any_other_name(guard, overrides):
    refused(guard, build("POST", "/clients", make(CLIENT_BODY, **overrides)), "Name must start with MCPTEST-")


@pytest.mark.parametrize("domain", ["example.invalid", "EXAMPLE.INVALID", "mail.example.invalid", None])
def test_post_clients_allows_only_the_test_domain_or_none(guard, domain):
    guard.check(build("POST", "/clients", make(CLIENT_BODY, Domain=domain)))


@pytest.mark.parametrize(
    "domain",
    ["gmail.com", "example.com", "example.invalid.com", "notexample.invalid", "example.invalid ", " example.invalid", "", 5, ["example.invalid"], "\u0435xample.invalid"],
)
def test_post_clients_blocks_a_real_domain(guard, domain):
    refused(guard, build("POST", "/clients", make(CLIENT_BODY, Domain=domain)), "Domain must be absent or at example.invalid")


RUN_CLIENT_PATH = f"/clients/{RUN_CLIENT}"  # the id is in the path only: UpdateClientCommand has no Id


@pytest.mark.parametrize(
    "body",
    [
        {"AlternateName": "MCPTEST alt"},
        {"Name": f"{RUN} renamed", "BillingName": "b", "StatusId": 2, "AlternateName": "a"},
        {},  # nothing to change is not the guard's business
    ],
)
def test_patch_clients_allows_the_run_created_client_in_every_way(guard, body):
    guard.check(build("PATCH", RUN_CLIENT_PATH, body))


@pytest.mark.parametrize("value", [RUN_CLIENT, TEST_CLIENT, SECOND, RUN_CLIENT + 1, None, "9501", 9501.0, True, [TEST_CLIENT]])
def test_patch_clients_refuses_an_id_in_the_body_because_the_spec_has_none(guard, cleanup_guard, value):
    # the record is named by the path alone (contract e15cb5a18ec2): a body Id, whatever it holds, is not a field
    # of UpdateClientCommand, so the guard refuses it as an unknown field before it looks at the path
    for subject in (guard, cleanup_guard):
        refused(
            subject,
            build("PATCH", RUN_CLIENT_PATH, {"Id": value, "AlternateName": "x"}),
            "the body has a field the spec does not define for this operation: Id",
        )
        refused(
            subject,
            build("PATCH", f"/clients/{TEST_CLIENT}", {"Id": value}),
            "the body has a field the spec does not define for this operation: Id",
        )


def test_patch_clients_in_the_collection_form_is_gone_it_is_not_an_operation_of_the_spec(guard, cleanup_guard):
    for subject in (guard, cleanup_guard):
        refused(subject, build("PATCH", "/clients", {"AlternateName": "x"}), "not a known Gorelo operation")


@pytest.mark.parametrize(
    "client, body",
    [
        (TEST_CLIENT, {"AlternateName": "MCPTEST alt"}),
        (TEST_CLIENT, {}),
        (TEST_CLIENT, {"Name": "Renamed"}),
        (TEST_CLIENT, {"BillingName": "b"}),
        (TEST_CLIENT, {"StatusId": 2}),
        (SECOND, {"AlternateName": "x"}),
        (SECOND, {}),
    ],
)
def test_patch_clients_never_changes_test_client_or_second_not_even_its_alternate_name(guard, cleanup_guard, client, body):
    for subject in (guard, cleanup_guard):
        refused(
            subject,
            build("PATCH", f"/clients/{client}", body),
            f"path id {client} is not allowed; client {client} is one of the records the live tests run on and is never changed",
        )


@pytest.mark.parametrize("client", [TEST_CLIENT, SECOND])
def test_patch_clients_refuses_the_test_clients_even_if_a_manifest_lists_them_as_run_created(tmp_path, client):
    run = Manifest(RUN, tmp_path / "wrong.json")
    run.created("client", client, run.label("recorded by mistake"))
    for subject in (LiveGuard("write", run), LiveGuard("write", run, cleanup=True)):
        refused(subject, build("PATCH", f"/clients/{client}", {"AlternateName": "x"}), "is never changed")


@pytest.mark.parametrize(
    "path, body, match",
    [
        ("/clients/9801", {"AlternateName": "x"}, "path id 9801 is not allowed; only a client created by this run may be changed"),
        ("/clients/5", {"Name": "x"}, "path id 5 is not allowed; only a client created by this run may be changed"),
        (f"/clients/{RUN_CLIENT + 1}", {"AlternateName": "x"}, "only a client created by this run may be changed"),
        ("/clients/abc", {"AlternateName": "x"}, "the path id is not a valid client id"),
        ("/clients/0", {"AlternateName": "x"}, "the path id is not a valid client id"),
        (f"/clients/{uid(3)}", {"AlternateName": "x"}, "the path id is not a valid client id"),
        (f"/clients/{RUN_CLIENT}.0", {"AlternateName": "x"}, "the path id is not a valid client id"),
        (f"/clients/{RUN_CLIENT}", {"AlternateNme": "x"}, "the body has a field the spec does not define"),
        (f"/clients/{RUN_CLIENT}", {"ClientId": TEST_CLIENT}, "the body has a field the spec does not define"),
    ],
)
def test_patch_clients_blocks_every_other_client(guard, path, body, match):
    refused(guard, build("PATCH", path, body), match)


def test_patch_clients_leaves_the_path_id_in_the_message_not_the_body(guard):
    error = refused(guard, build("PATCH", "/clients/9801", {"AlternateName": "x"}), "path id 9801 is not allowed")
    assert error.label == "PATCH /clients/9801"


# --------------------------------------------------------------------------
# contacts
# --------------------------------------------------------------------------

CONTACT_BODY = {
    "ClientId": TEST_CLIENT,
    "FirstName": "MCPTEST",
    "LastName": "Contact",
    "PrimaryEmail": "mcptest-20991002101500@example.invalid",
    "MobilePhone": "5555550142",
    "MobilePhoneCountryCode": "US",
}
CONTACT_UPDATE = make(CONTACT_BODY, JobTitle="Tester", SecondaryEmail=[])  # no ContactId: the id is in the path


def test_post_contacts_allows_test_client(guard):
    guard.check(build("POST", "/contacts", CONTACT_BODY))
    guard.check(build("POST", "/contacts", make(CONTACT_BODY, SecondaryEmail=["second@example.invalid"])))


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"ClientId": SECOND}, "ClientId must be 9501"),
        ({"ClientId": 5555}, "ClientId must be 9501"),
        ({"ClientId": RUN_CLIENT}, "ClientId must be 9501"),
        ({"ClientId": DROP}, "ClientId is required"),
        ({"ClientId": None}, "ClientId is required"),
        ({"ClientId": "9501"}, "ClientId must be a JSON integer"),
        ({"PrimaryEmail": "bob@example.com"}, "email address that is not allowed"),
    ],
)
def test_post_contacts_blocks_any_other_client_or_a_real_address(guard, overrides, match):
    refused(guard, build("POST", "/contacts", make(CONTACT_BODY, **overrides)), match)


RUN_CONTACT_PATH = f"/contacts/{RUN_CONTACT}"  # the id is in the path only: UpdateContactCommand has no ContactId


def test_patch_contacts_allows_a_run_created_contact_on_test_client(guard):
    guard.check(build("PATCH", RUN_CONTACT_PATH, CONTACT_UPDATE))
    guard.check(build("PATCH", RUN_CONTACT_PATH, make(CONTACT_UPDATE, ClientId=DROP)))


@pytest.mark.parametrize("value", [RUN_CONTACT, OPERATOR_CONTACT, 9900, 5, None, str(RUN_CONTACT), RUN_CONTACT + 0.0, True])
def test_patch_contacts_refuses_a_contact_id_in_the_body_because_the_spec_has_none(guard, cleanup_guard, value):
    # the record is named by the path alone (contract e15cb5a18ec2): a body ContactId, whatever it holds, is not a field
    # of UpdateContactCommand, so the guard refuses it as an unknown field before it looks at the path
    for subject in (guard, cleanup_guard):
        refused(
            subject,
            build("PATCH", RUN_CONTACT_PATH, make(CONTACT_UPDATE, ContactId=value)),
            "the body has a field the spec does not define for this operation: ContactId",
        )
        refused(
            subject,
            build("PATCH", f"/contacts/{OPERATOR_CONTACT}", make(CONTACT_UPDATE, ContactId=value)),
            "the body has a field the spec does not define for this operation: ContactId",
        )


def test_patch_contacts_in_the_collection_form_is_gone_it_is_not_an_operation_of_the_spec(guard, cleanup_guard):
    for subject in (guard, cleanup_guard):
        refused(subject, build("PATCH", "/contacts", CONTACT_UPDATE), "not a known Gorelo operation")


@pytest.mark.parametrize(
    "path, overrides, match",
    [
        (f"/contacts/{OPERATOR_CONTACT}", {}, "path contactId 9600 is not a contact created by this run"),
        ("/contacts/9900", {}, "path contactId 9900 is not a contact created by this run"),
        ("/contacts/5", {}, "path contactId 5 is not a contact created by this run"),
        (f"/contacts/{RUN_CONTACT + 1}", {}, f"path contactId {RUN_CONTACT + 1} is not a contact created by this run"),
        ("/contacts/abc", {}, "path contactId is not a valid contact id"),
        ("/contacts/0", {}, "path contactId is not a valid contact id"),
        (f"/contacts/{uid(3)}", {}, "path contactId is not a valid contact id"),
        (RUN_CONTACT_PATH, {"ClientId": SECOND}, "ClientId must be 9501"),
        (RUN_CONTACT_PATH, {"ClientId": RUN_CLIENT}, "ClientId must be 9501"),
        (RUN_CONTACT_PATH, {"ClientId": "9501"}, "ClientId must be a JSON integer"),
        (RUN_CONTACT_PATH, {"PrimaryEmail": "bob@example.com"}, "email address that is not allowed"),
    ],
)
def test_patch_contacts_blocks_every_contact_the_run_did_not_create(guard, path, overrides, match):
    refused(guard, build("PATCH", path, make(CONTACT_UPDATE, **overrides)), match)


# --------------------------------------------------------------------------
# tickets
# --------------------------------------------------------------------------

TICKET_BODY = {"ClientId": TEST_CLIENT, "Title": "MCPTEST ticket", "GroupId": 7201, "StatusId": 1, "TypeId": 7101, "PriorityId": 3, "SourceId": 6}


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"ClientId": SECOND},
        {"ContactId": None},
        {"ContactId": RUN_CONTACT},
        {"ContactId": OPERATOR_CONTACT},
        {"ClientId": SECOND, "ContactId": OPERATOR_CONTACT, "SendTicketCreatedEmail": True},
        {"ClientId": SECOND, "ContactId": OPERATOR_CONTACT, "SendTicketCreatedEmail": True, "CcContactIds": [OPERATOR_CONTACT]},
        {"ContactId": RUN_CONTACT, "SendTicketCreatedEmail": False},
        {"ContactId": RUN_CONTACT, "SendTicketCreatedEmail": None},
        {"CcContactIds": [OPERATOR_CONTACT, RUN_CONTACT]},
        {"CcContactIds": []},
        {"LeadAssigneeId": OPERATOR_USER, "WatcherIds": [OPERATOR_USER], "AssistingAssigneeIds": [OPERATOR_USER]},
        {"TagIds": [1, 2], "CreatedOn": "2026-09-01T10:00:00Z", "ClosedOn": "2026-09-01T11:00:00Z", "StatusId": 4},
        {"AgentAssetIds": [], "CustomAssetIds": None},
        {"UptimeIds": [UPTIME]},
        {"Description": "Write to ops@example.com or mcptest@example.invalid"},
    ],
)
def test_post_tickets_allows_the_approved_clients_and_contacts(guard, overrides):
    guard.check(build("POST", "/tickets", make(TICKET_BODY, **overrides)))


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"ClientId": 5555}, "ClientId 5555 is not allowed"),
        ({"ClientId": RUN_CLIENT}, "ClientId 8200 is not allowed"),
        ({"ClientId": DROP}, "ClientId is required"),
        ({"ClientId": None}, "ClientId is required"),
        ({"ClientId": "9501"}, "ClientId must be a JSON integer"),
        ({"ClientId": True}, "ClientId must be a JSON integer"),
        ({"ContactId": 5}, "ContactId holds contact 5"),
        ({"ContactId": 9900}, "ContactId holds contact 9900"),
        ({"ContactId": str(OPERATOR_CONTACT)}, "ContactId must be a JSON integer"),
        ({"ContactId": True}, "ContactId must be a JSON integer"),
        ({"CcContactIds": [5]}, "CcContactIds holds contact 5"),
        ({"CcContactIds": [OPERATOR_CONTACT, 5]}, "CcContactIds holds contact 5"),
        ({"CcContactIds": OPERATOR_CONTACT}, "CcContactIds must be a list"),
        ({"SendTicketCreatedEmail": True}, "may be true only when ContactId is the operator contact"),
        ({"SendTicketCreatedEmail": True, "ContactId": RUN_CONTACT}, "may be true only when ContactId is the operator contact"),
        ({"SendTicketCreatedEmail": True, "ContactId": None}, "may be true only when ContactId is the operator contact"),
        (
            {"SendTicketCreatedEmail": True, "ContactId": OPERATOR_CONTACT, "CcContactIds": [RUN_CONTACT]},
            "would email the CC contacts",
        ),
        ({"SendTicketCreatedEmail": "true", "ContactId": OPERATOR_CONTACT}, "SendTicketCreatedEmail must be true or false"),
        ({"SendTicketCreatedEmail": 1}, "SendTicketCreatedEmail must be true or false"),
        ({"LeadAssigneeId": OTHER_USER}, f"LeadAssigneeId {OTHER_USER} is not allowed"),
        ({"WatcherIds": [OTHER_USER]}, f"WatcherIds holds user {OTHER_USER}"),
        ({"AssistingAssigneeIds": [OPERATOR_USER, OTHER_USER]}, f"AssistingAssigneeIds holds user {OTHER_USER}"),
        ({"AgentAssetIds": [uid(70)]}, "AgentAssetIds must be empty"),
        ({"CustomAssetIds": [uid(71)]}, "CustomAssetIds must be empty"),
        ({"UptimeIds": [FOREIGN]}, "UptimeIds .* not a uptime check created by this run"),
        ({"UptimeIds": ["not-a-uuid"]}, "UptimeIds\\[0\\] must be a UUID string"),
        ({"UptimeIds": UPTIME}, "UptimeIds must be a list of UUID strings"),
        ({"Description": "Send it to bob@example.com"}, "email address that is not allowed"),
        ({"Unknown": 1}, "field the spec does not define"),
        ({"ClientId": DROP, "clientId": SECOND}, "field the spec does not define"),
        ({"clientId": SECOND}, "same key twice"),
    ],
)
def test_post_tickets_blocks_other_clients_contacts_and_emails(guard, overrides, match):
    refused(guard, build("POST", "/tickets", make(TICKET_BODY, **overrides)), match)


@pytest.mark.parametrize(
    "body",
    [
        {"Title": "renamed", "TagIds": [1, 2], "LeadAssigneeId": OPERATOR_USER},
        {"StatusId": 2},
        {"ClientId": SECOND},
        {"ClientId": TEST_CLIENT, "ContactId": RUN_CONTACT},
        {"ContactId": OPERATOR_CONTACT, "CcContactIds": [OPERATOR_CONTACT, RUN_CONTACT]},
        {"ClosedOn": "2026-10-02T10:00:00Z", "UpdatedOn": "2026-10-02T10:00:00Z"},
    ],
)
def test_patch_tickets_allows_a_run_ticket_and_approved_values(guard, body):
    guard.check(build("PATCH", f"/tickets/{TICKET_NONE}", body))


@pytest.mark.parametrize(
    "path, body, match",
    [
        (f"/tickets/{FOREIGN}", {"Title": "x"}, "path ticketId .* is not a ticket created by this run"),
        (f"/tickets/{ITEM}", {"Title": "x"}, "is not a ticket created by this run"),
        ("/tickets/statuses", {"Title": "x"}, "not a valid ticket id"),
        (f"/tickets/{TICKET_NONE}", {"ClientId": 5555}, "ClientId 5555 is not allowed"),
        (f"/tickets/{TICKET_NONE}", {"ContactId": 5}, "ContactId holds contact 5"),
        (f"/tickets/{TICKET_NONE}", {"CcContactIds": [5]}, "CcContactIds holds contact 5"),
        (f"/tickets/{TICKET_NONE}", {"LeadAssigneeId": OTHER_USER}, "LeadAssigneeId"),
        (f"/tickets/{TICKET_NONE}", {"AgentAssetIds": [uid(70)]}, "AgentAssetIds must be empty"),
        (f"/tickets/{TICKET_NONE}", {"Title": "mail bob@example.com"}, "email address that is not allowed"),
        (f"/tickets/{TICKET_NONE}", {"SendTicketCreatedEmail": True}, "field the spec does not define"),
        (f"/tickets/{TICKET_NONE}", None, "no JSON body"),
    ],
)
def test_patch_tickets_blocks_other_tickets_clients_and_contacts(guard, path, body, match):
    refused(guard, build("PATCH", path, body), match)


# --------------------------------------------------------------------------
# ticket comments, side conversations, approvals
# --------------------------------------------------------------------------


def comment(kind, **extra):
    return {"Body": "<p>MCPTEST</p>", "ConversationTypeId": kind, **extra}


RUN_TICKETS = [TICKET_OPERATOR, TICKET_NONE, TICKET_RUN_CONTACT, TICKET_CC, TICKET_UNKNOWN]


def conversation_of(ticket, kind):
    """The ConversationId text the matrix sends for the side conversation (3) or approval (4) it made on `ticket`."""
    return str(SIDE_OF[ticket]) if kind == 3 else APPROVAL_OF[ticket]


@pytest.mark.parametrize("ticket", RUN_TICKETS)
@pytest.mark.parametrize("kind", [2, 3, 4])
def test_private_comments_and_comments_into_the_ticket_s_own_conversations_are_allowed_on_any_run_ticket(guard, ticket, kind):
    extra = {"ConversationId": conversation_of(ticket, kind)} if kind != 2 else {}
    guard.check(build("POST", f"/tickets/{ticket}/comments", comment(kind, **extra)))


def test_a_conversation_id_is_matched_by_what_it_names_not_by_how_it_is_spelled(guard):
    path = f"/tickets/{TICKET_OPERATOR}/comments"
    guard.check(build("POST", path, comment(4, ConversationId=APPROVAL_OF[TICKET_OPERATOR].upper())))  # a UUID is not case sensitive
    guard.check(build("POST", path, comment(3, ConversationId=str(SIDE_OF[TICKET_OPERATOR]))))  # the tool sends the number as text


@pytest.mark.parametrize("kind", [3, 4])
def test_a_side_conversation_or_approval_comment_must_name_its_conversation(guard, kind):
    for body in (comment(kind), comment(kind, ConversationId=None)):
        error = refused(guard, build("POST", f"/tickets/{TICKET_OPERATOR}/comments", body), "ConversationId is required for this comment")
        assert "this run created on the ticket" in str(error)


@pytest.mark.parametrize(
    "kind, conversation, match",
    [
        (3, uid(80), "is not one of the side conversations this run created"),  # an unknown id was allowed before
        (4, uid(80), "is not one of the approvals this run created"),
        (3, "9999", "is not one of the side conversations this run created"),
        (3, str(STRANGER_SIDE), "is not one of the side conversations this run created"),  # a customer's conversation
        (4, STRANGER_APPROVAL, "is not one of the approvals this run created"),
        (3, APPROVAL_OF[TICKET_OPERATOR], "is not one of the side conversations this run created"),  # the wrong kind
        (4, str(SIDE_OF[TICKET_OPERATOR]), "is not one of the approvals this run created"),
        (4, "not a uuid at all", "is not one of the approvals this run created"),
        (3, "", "ConversationId is not a usable id for a side conversation"),
        (4, "   ", "ConversationId is not a usable id for an approval"),
    ],
)
def test_a_comment_into_a_conversation_this_run_did_not_create_is_blocked(guard, kind, conversation, match):
    refused(guard, build("POST", f"/tickets/{TICKET_OPERATOR}/comments", comment(kind, ConversationId=conversation)), match)


@pytest.mark.parametrize("value", [701, 701.0, True, ["701"], {"Id": "701"}])
def test_a_conversation_id_that_is_not_text_is_blocked(guard, value):
    refused(guard, build("POST", f"/tickets/{TICKET_OPERATOR}/comments", comment(3, ConversationId=value)), "ConversationId must be a string")


@pytest.mark.parametrize("kind", [3, 4])
def test_a_comment_into_a_conversation_of_another_run_ticket_is_blocked(guard, kind):
    error = refused(
        guard,
        build("POST", f"/tickets/{TICKET_OPERATOR}/comments", comment(kind, ConversationId=conversation_of(TICKET_NONE, kind))),
        "belongs to ticket",
    )
    assert TICKET_NONE in str(error) and TICKET_OPERATOR in str(error)  # both tickets are named


@pytest.mark.parametrize("details", [{}, {"ticket_id": None}, {"ticket_id": "not-a-uuid"}, {"ticket_id": 5}, {"project_id": PROJECT}])
def test_a_conversation_recorded_without_a_usable_ticket_id_takes_no_comment(guard, manifest, details):
    manifest.created("side_conversation", 721, manifest.label("odd side conversation"), details)
    manifest.created("approval", uid(88), manifest.label("odd approval"), details)
    path = f"/tickets/{TICKET_OPERATOR}/comments"
    refused(guard, build("POST", path, comment(3, ConversationId="721")), "the manifest has no valid ticket_id")
    refused(guard, build("POST", path, comment(4, ConversationId=uid(88))), "the manifest has no valid ticket_id")


def test_a_conversation_recorded_for_a_task_takes_no_ticket_comment(guard):
    path = f"/tickets/{TICKET_OPERATOR}/comments"
    refused(guard, build("POST", path, comment(3, ConversationId=str(TASK_SIDE))), "the manifest has no valid ticket_id")
    refused(guard, build("POST", path, comment(4, ConversationId=TASK_APPROVAL)), "the manifest has no valid ticket_id")


@pytest.mark.parametrize("kind", [1, 2])
@pytest.mark.parametrize("conversation", [str(SIDE_OF[TICKET_OPERATOR]), APPROVAL_OF[TICKET_OPERATOR], uid(80), "x", ""])
def test_a_public_or_private_comment_carries_no_conversation_id(guard, kind, conversation):
    refused(
        guard,
        build("POST", f"/tickets/{TICKET_OPERATOR}/comments", comment(kind, ConversationId=conversation)),
        "ConversationId must be absent on a public or private comment",
    )


@pytest.mark.parametrize("kind", [1, 2])
def test_a_null_conversation_id_on_a_public_or_private_comment_is_the_same_as_none(guard, kind):
    guard.check(build("POST", f"/tickets/{TICKET_OPERATOR}/comments", comment(kind, ConversationId=None)))


def test_the_rule_texts_say_what_a_comment_may_name(guard):
    table = guard.rule_table()
    assert "ConversationId of a side conversation or approval this run created on that ticket" in table["POST /v1/tickets/{ticketId}/comments"]
    assert "ConversationId of a side conversation or approval this run created on that task" in table["POST /v1/projects/{projectId}/tasks/{taskId}/comments"]
    assert table["POST /v1/projects/{projectId}/comments"].endswith("no ConversationId")


@pytest.mark.parametrize("ticket", [TICKET_OPERATOR, TICKET_NONE])
def test_a_public_comment_is_allowed_when_the_contact_is_the_operator_or_absent(guard, ticket):
    guard.check(build("POST", f"/tickets/{ticket}/comments", comment(1)))


@pytest.mark.parametrize(
    "ticket, who",
    [(TICKET_RUN_CONTACT, str(RUN_CONTACT)), (TICKET_CC, str(RUN_CONTACT))],
)
def test_a_public_comment_is_blocked_when_the_ticket_has_another_contact_or_cc(guard, ticket, who):
    error = refused(guard, build("POST", f"/tickets/{ticket}/comments", comment(1)), "a public comment would email contact")
    assert who in str(error)


@pytest.mark.parametrize("details", [{"contact_id": str(OPERATOR_CONTACT)}, {"contact_id": True}, {"contact_id": None, "cc_contact_ids": [True]}, {"contact_id": None, "cc_contact_ids": ["1"]}])
def test_a_public_comment_is_blocked_when_the_manifest_holds_a_malformed_contact(guard, manifest, details):
    manifest.created("ticket", uid(61), manifest.label("odd ticket"), details)
    refused(guard, build("POST", f"/tickets/{uid(61)}/comments", comment(1)), "the manifest holds an invalid contact")


@pytest.mark.parametrize(
    "details, allowed",
    [
        ({"ContactId": OPERATOR_CONTACT}, True),
        ({"ContactId": None}, True),
        ({"ContactId": None, "CcContactIds": [OPERATOR_CONTACT]}, True),
        ({"ContactId": RUN_CONTACT}, False),
        ({"ContactId": None, "CcContactIds": [RUN_CONTACT]}, False),
        ({"contact_id": OPERATOR_CONTACT, "CcContactIds": [RUN_CONTACT]}, False),
        ({"contact_id": None, "ContactId": RUN_CONTACT}, False),  # both spellings: every contact counts
        ({"contact_id": None, "cc_contact_ids": RUN_CONTACT}, False),  # a lone CC instead of a list
        ({"client_id": TEST_CLIENT}, None),  # neither spelling: unknown
    ],
)
def test_the_ticket_contact_may_be_recorded_in_snake_or_pascal_case(guard, manifest, details, allowed):
    manifest.created("ticket", uid(62), manifest.label("spelled ticket"), details)
    request = build("POST", f"/tickets/{uid(62)}/comments", comment(1))
    if allowed:
        guard.check(request)
    else:
        refused(guard, request, "manifest has no contact_id" if allowed is None else "a public comment would email contact")


def test_a_public_comment_needs_the_contact_to_be_recorded(guard):
    refused(guard, build("POST", f"/tickets/{TICKET_UNKNOWN}/comments", comment(1)), "manifest has no contact_id")


def test_a_contact_set_by_a_patch_through_the_guard_stops_public_comments(guard):
    path = f"/tickets/{TICKET_NONE}/comments"
    guard.check(build("POST", path, comment(1)))
    guard.check(build("PATCH", f"/tickets/{TICKET_NONE}", {"ContactId": RUN_CONTACT}))
    refused(guard, build("POST", path, comment(1)), "a public comment would email contact")
    guard.check(build("POST", path, comment(2)))  # private is still fine
    # the operator contact on top does not undo it: the earlier contact may still be on the ticket
    guard.check(build("PATCH", f"/tickets/{TICKET_NONE}", {"ContactId": OPERATOR_CONTACT}))
    refused(guard, build("POST", path, comment(1)), "a public comment would email contact")


def test_a_cc_set_by_a_patch_through_the_guard_stops_public_comments(guard):
    guard.check(build("PATCH", f"/tickets/{TICKET_OPERATOR}", {"CcContactIds": [OPERATOR_CONTACT]}))
    guard.check(build("POST", f"/tickets/{TICKET_OPERATOR}/comments", comment(1)))
    guard.check(build("PATCH", f"/tickets/{TICKET_OPERATOR}", {"CcContactIds": [RUN_CONTACT]}))
    refused(guard, build("POST", f"/tickets/{TICKET_OPERATOR}/comments", comment(1)), "a public comment would email contact")


def test_a_refused_patch_does_not_change_what_the_guard_remembers(guard):
    refused(guard, build("PATCH", f"/tickets/{TICKET_NONE}", {"ContactId": RUN_CONTACT, "ClientId": 5555}), "ClientId 5555")
    guard.check(build("POST", f"/tickets/{TICKET_NONE}/comments", comment(1)))


@pytest.mark.parametrize(
    "body, match",
    [
        (comment(5), "ConversationTypeId 5 is not a known comment type"),
        (comment(0), "ConversationTypeId 0 is not a known comment type"),
        (comment(None), "ConversationTypeId is required"),
        ({"Body": "x"}, "ConversationTypeId is required"),
        (comment("1"), "ConversationTypeId must be a JSON integer"),
        (comment(True), "ConversationTypeId must be a JSON integer"),
        (comment(2.0), "ConversationTypeId must be a JSON integer"),
        (comment(2, Body="mail bob@example.com"), "email address that is not allowed"),
        (comment(2, Attachments=[{"Name": "bob@example.com.txt", "Url": "https://files.example/x"}]), "Attachments\\[0\\]\\.Name holds an email"),
    ],
)
def test_ticket_comments_with_an_unknown_type_or_a_stranger_address_are_blocked(guard, body, match):
    refused(guard, build("POST", f"/tickets/{TICKET_NONE}/comments", body), match)


def test_ticket_comments_on_a_ticket_the_run_did_not_create_are_blocked(guard):
    for kind in (1, 2, 3, 4):
        refused(guard, build("POST", f"/tickets/{FOREIGN}/comments", comment(kind)), "is not a ticket created by this run")


def test_a_comment_may_name_the_operator_address(guard):
    guard.check(build("POST", f"/tickets/{TICKET_NONE}/comments", comment(2, Body="ask ops@example.com or a@example.invalid")))


SIDE = {"Name": "MCPTEST side conversation", "Email": "ops@example.com"}


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"CcEmails": ["ops@example.com", "copy@example.invalid"]},
        {"Email": "OPS@EXAMPLE.COM"},
        {"Email": "mcptest@example.invalid", "AttachPublicConversation": False},
        {"CcEmails": []},
        {"CcEmails": None},
    ],
)
def test_a_side_conversation_is_allowed_to_the_operator_or_an_example_invalid_address(guard, overrides):
    guard.check(build("POST", f"/tickets/{TICKET_OPERATOR}/conversations/side-conversation", make(SIDE, **overrides)))
    guard.check(
        build("POST", f"/projects/{PROJECT}/tasks/{TASK}/conversations/side-conversation", make({"Name": "n", "Email": "ops@example.com"}, **{k: v for k, v in overrides.items() if k != "AttachPublicConversation"}))
    )


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"Email": "bob@example.com"}, "email address that is not allowed"),
        ({"Email": "bob@example.invalid.com"}, "email address that is not allowed"),
        ({"Email": "not an address"}, "Email must be ops@example.com or an address at @example.invalid"),
        ({"Email": ""}, "Email must be"),
        ({"Email": None}, "Email must be"),
        ({"Email": DROP}, "Email must be"),
        ({"Email": ["ops@example.com"]}, "Email must be"),
        ({"CcEmails": ["ops@example.com", "bob@example.com"]}, "email address that is not allowed"),
        ({"CcEmails": ["not an address"]}, "CcEmails\\[0\\] must be"),
        ({"CcEmails": "ops@example.com"}, "CcEmails must be a list"),
        ({"Name": "mail bob@example.com"}, "email address that is not allowed"),
    ],
)
def test_a_side_conversation_to_anyone_else_is_blocked(guard, overrides, match):
    refused(guard, build("POST", f"/tickets/{TICKET_OPERATOR}/conversations/side-conversation", make(SIDE, **overrides)), match)


def test_a_side_conversation_on_a_foreign_ticket_or_task_is_blocked(guard):
    refused(guard, build("POST", f"/tickets/{FOREIGN}/conversations/side-conversation", SIDE), "is not a ticket created by this run")
    refused(
        guard,
        build("POST", f"/projects/{PROJECT}/tasks/{FOREIGN}/conversations/side-conversation", SIDE),
        "is not a project task created by this run",
    )
    refused(
        guard,
        build("POST", f"/projects/{PROJECT}/tasks/{TASK}/conversations/side-conversation", make(SIDE, Email="bob@example.com")),
        "email address that is not allowed",
    )


@pytest.mark.parametrize("contacts", [[OPERATOR_CONTACT], [RUN_CONTACT], [OPERATOR_CONTACT, RUN_CONTACT], []])
def test_an_approval_may_name_only_the_operator_and_run_contacts(guard, contacts):
    guard.check(build("POST", f"/tickets/{TICKET_OPERATOR}/conversations/approval", {"Name": "MCPTEST approval", "ContactIds": contacts}))
    guard.check(build("POST", f"/projects/{PROJECT}/tasks/{TASK}/conversations/approval", {"Name": "MCPTEST approval", "ContactIds": contacts}))


@pytest.mark.parametrize(
    "contacts, match",
    [
        ([5], "ContactIds holds contact 5"),
        ([OPERATOR_CONTACT, 5], "ContactIds holds contact 5"),
        ([9900], "ContactIds holds contact 9900"),
        ([str(OPERATOR_CONTACT)], "ContactIds\\[0\\] must be a JSON integer"),
        (OPERATOR_CONTACT, "ContactIds must be a list"),
    ],
)
def test_an_approval_naming_anyone_else_is_blocked(guard, contacts, match):
    refused(guard, build("POST", f"/tickets/{TICKET_OPERATOR}/conversations/approval", {"Name": "x", "ContactIds": contacts}), match)
    refused(guard, build("POST", f"/projects/{PROJECT}/tasks/{TASK}/conversations/approval", {"Name": "x", "ContactIds": contacts}), match)


def test_an_approval_on_a_foreign_ticket_is_blocked(guard):
    refused(guard, build("POST", f"/tickets/{FOREIGN}/conversations/approval", {"Name": "x", "ContactIds": [OPERATOR_CONTACT]}), "is not a ticket created by this run")


# --------------------------------------------------------------------------
# attachments (multipart)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item_type, item_id",
    [("Ticket", TICKET_NONE), ("ticket", TICKET_NONE.upper()), ("Task", TASK), ("Project", PROJECT), (" Ticket ", TICKET_OPERATOR)],
)
def test_an_attachment_upload_to_a_run_ticket_task_or_project_is_allowed(guard, item_type, item_id):
    guard.check(upload(item_type, item_id))


@pytest.mark.parametrize(
    "item_type, item_id, match",
    [
        ("Ticket", FOREIGN, "itemId .* is not a ticket created by this run"),
        ("Task", FOREIGN, "is not a project task created by this run"),
        ("Project", FOREIGN, "is not a project created by this run"),
        ("Ticket", TASK, "is not a ticket created by this run"),  # the id is a run task, the type says ticket
        ("Task", PROJECT, "is not a project task created by this run"),
        ("Project", TICKET_NONE, "is not a project created by this run"),
        ("Asset", TICKET_NONE, "itemType must be Ticket, Task or Project"),
        ("Contact", TICKET_NONE, "itemType must be Ticket, Task or Project"),
        ("", TICKET_NONE, "itemType must be Ticket, Task or Project"),
        ("Ticket", "not-a-uuid", "not a valid ticket id"),
        ("Ticket", "9501", "not a valid ticket id"),
        ("Ticket", TICKET_NONE.replace("-", ""), "not a valid ticket id"),
        ("Ticket", "{" + TICKET_NONE + "}", "not a valid ticket id"),
    ],
)
def test_an_attachment_upload_to_anything_else_is_blocked(guard, item_type, item_id, match):
    refused(guard, upload(item_type, item_id), match)


def multipart(parts):
    """parts: [(name, value)] text fields and [(name, (filename, bytes, type))] files, in this order."""
    return httpx.Request("POST", BASE + "/attachments", files=[(name, value if isinstance(value, tuple) else (None, value)) for name, value in parts])


def test_an_upload_without_the_type_or_the_id_is_blocked(guard):
    refused(guard, multipart([("file", ("a.txt", b"x", "text/plain")), ("itemId", TICKET_NONE)]), "itemType must be")
    refused(guard, multipart([("file", ("a.txt", b"x", "text/plain")), ("itemType", "Ticket")]), "itemId is required")


def test_a_repeated_or_unknown_form_field_is_blocked(guard):
    twice = multipart([("file", ("a.txt", b"x", "text/plain")), ("itemType", "Ticket"), ("itemId", FOREIGN), ("itemId", TICKET_NONE)])
    refused(guard, twice, "repeats a field name")
    twice = multipart([("file", ("a.txt", b"x", "text/plain")), ("itemType", "Ticket"), ("ITEMID", TICKET_NONE), ("itemId", FOREIGN)])
    refused(guard, twice, "repeats a field name")
    refused(guard, upload("Ticket", TICKET_NONE, extra="1"), "form has a field the spec does not define: extra")
    refused(guard, upload("Ticket", TICKET_NONE, **{"x@example.com": "1"}), "form has a field the spec does not define: <unusual name>")


def test_an_upload_must_really_be_multipart_form_data(guard):
    refused(guard, build("POST", "/attachments", {"itemType": "Ticket", "itemId": TICKET_NONE}), "not multipart/form-data")
    refused(guard, httpx.Request("POST", BASE + "/attachments", data={"itemType": "Ticket", "itemId": TICKET_NONE}), "not multipart/form-data")
    refused(guard, raw("POST", "/attachments", b"", "multipart/form-data; boundary=x"), "no parts|text before")


def test_text_around_the_parts_of_a_multipart_body_is_blocked(guard):
    boundary = "xyz"
    parts = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="itemType"\r\n\r\nTicket\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="itemId"\r\n\r\n{TICKET_NONE}\r\n'
        f'--{boundary}--\r\n'
    )
    guard.check(raw("POST", "/attachments", parts, f"multipart/form-data; boundary={boundary}"))
    refused(guard, raw("POST", "/attachments", "hidden preamble\r\n" + parts, f"multipart/form-data; boundary={boundary}"), "text before")
    refused(guard, raw("POST", "/attachments", parts + "hidden epilogue", f"multipart/form-data; boundary={boundary}"), "text before|after")


@pytest.mark.parametrize(
    "body, match",
    [
        (b'--b\r\nContent-Disposition: form-data\r\n\r\nTicket\r\n--b--\r\n', "a multipart part has no name"),
        (b'--b\r\nContent-Disposition: form-data; name="itemType"\r\n\r\n\xff\xfe\r\n--b--\r\n', "not UTF-8 text"),
        (
            b'--b\r\nContent-Disposition: form-data; name="file"\r\nContent-Type: multipart/mixed; boundary=inner\r\n\r\n'
            b'--inner\r\nContent-Disposition: attachment; filename="a.txt"\r\n\r\nx\r\n--inner--\r\n\r\n--b--\r\n',
            "nested multipart part",
        ),
    ],
)
def test_a_multipart_body_with_an_odd_part_is_blocked(guard, body, match):
    refused(guard, raw("POST", "/attachments", body, "multipart/form-data; boundary=b"), match)


def test_an_email_in_an_uploaded_file_or_field_is_blocked(guard):
    guard.check(upload("Ticket", TICKET_NONE, content=b"mail ops@example.com or a@example.invalid"))
    refused(guard, upload("Ticket", TICKET_NONE, content=b"mail bob@example.com"), "file content holds an email address that is not allowed")
    refused(guard, upload("Ticket", TICKET_NONE, filename="bob@example.com.txt"), "file file name holds an email address")
    refused(guard, upload("Ticket", "bob@example.com"), "itemId holds an email address")
    refused(guard, upload("Ticket", TICKET_NONE, content="caf\u00e9 bob@example.com".encode("utf-8")), "file content holds an email")


# --------------------------------------------------------------------------
# time entries
# --------------------------------------------------------------------------


def entry(**overrides):
    return make({"TicketId": TICKET_NONE, "UserId": OPERATOR_USER, "BillableStatusId": 2, "Comment": "MCPTEST work"}, **overrides)


@pytest.mark.parametrize(
    "overrides",
    [{}, {"TicketId": TICKET_OPERATOR}, {"TicketId": DROP, "TaskId": TASK}, {"TaskId": TASK}, {"TicketId": TICKET_NONE.upper()}],
)
def test_post_time_entries_allows_a_run_ticket_or_task_and_the_operator_user(guard, overrides):
    guard.check(build("POST", "/time-entries", entry(**overrides)))


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"TicketId": FOREIGN}, "TicketId .* is not a ticket created by this run"),
        ({"TicketId": DROP}, "TicketId or TaskId is required"),
        ({"TicketId": None}, "TicketId or TaskId is required"),
        ({"TicketId": DROP, "TaskId": FOREIGN}, "TaskId .* is not a project task created by this run"),
        ({"TaskId": FOREIGN}, "TaskId .* is not a project task created by this run"),
        ({"TicketId": 5}, "TicketId must be a UUID string"),
        ({"TicketId": "not-a-uuid"}, "not a valid ticket id"),
        ({"TicketId": "{" + TICKET_NONE + "}"}, "not a valid ticket id"),
        ({"UserId": OTHER_USER}, f"UserId {OTHER_USER} is not allowed"),
        ({"UserId": DROP}, "UserId is required"),
        ({"UserId": None}, "UserId is required"),
        ({"UserId": str(OPERATOR_USER)}, "UserId must be a JSON integer"),
        ({"UserId": float(OPERATOR_USER)}, "UserId must be a JSON integer"),
        ({"Comment": "mail bob@example.com"}, "email address that is not allowed"),
    ],
)
def test_post_time_entries_blocks_other_tickets_and_users(guard, overrides, match):
    refused(guard, build("POST", "/time-entries", entry(**overrides)), match)


@pytest.mark.parametrize("body", [{"Comment": "updated"}, {"UserId": OPERATOR_USER}, {"ActualHours": 1.5, "StartedOn": "2026-10-02T10:00:00Z"}])
def test_patch_time_entries_allows_a_run_entry(guard, body):
    guard.check(build("PATCH", f"/time-entries/{TIME_ENTRY}", body))


@pytest.mark.parametrize(
    "path, body, match",
    [
        ("/time-entries/778", {"Comment": "x"}, "path timeEntryId 778 is not a time entry created by this run"),
        (f"/time-entries/{TICKET_NONE}", {"Comment": "x"}, "not a valid time entry id"),
        (f"/time-entries/{TIME_ENTRY}", {"UserId": OTHER_USER}, f"UserId {OTHER_USER} is not allowed"),
        (f"/time-entries/{TIME_ENTRY}", {"Comment": "mail bob@example.com"}, "email address that is not allowed"),
        (f"/time-entries/{TIME_ENTRY}", {"TicketId": TICKET_NONE}, "field the spec does not define"),
    ],
)
def test_patch_time_entries_blocks_other_entries_and_users(guard, path, body, match):
    refused(guard, build("PATCH", path, body), match)


# --------------------------------------------------------------------------
# items, uptime checks, projects
# --------------------------------------------------------------------------

ITEM_BODY = {"ClientId": TEST_CLIENT, "Name": "MCPTEST item", "TypeId": 1, "UnitPrice": 1.5}
UPTIME_BODY = {
    "ClientId": TEST_CLIENT,
    "TypeId": 2,
    "Frequency": 60,
    "RegionId": 1,
    "Target": {"Url": "https://mcp.example.net/.well-known/oauth-authorization-server"},
}
PROJECT_BODY = {"ClientId": TEST_CLIENT, "Title": "MCPTEST project"}


@pytest.mark.parametrize(
    "path, body",
    [("/items", ITEM_BODY), ("/uptime", UPTIME_BODY), ("/projects", PROJECT_BODY)],
    ids=["items", "uptime", "projects"],
)
class TestCreateOnAllowedClientOnly:
    def test_post_allows_test_client(self, guard, path, body):
        guard.check(build("POST", path, body))

    @pytest.mark.parametrize(
        "client, match",
        [
            (SECOND, "ClientId must be 9501"),
            (5555, "ClientId must be 9501"),
            (0, "ClientId must be 9501"),
            (RUN_CLIENT, "ClientId must be 9501"),
            (DROP, "ClientId is required"),
            (None, "ClientId is required"),
            ("9501", "ClientId must be a JSON integer"),
        ],
    )
    def test_post_blocks_every_other_client(self, guard, path, body, client, match):
        refused(guard, build("POST", path, make(body, ClientId=client)), match)

    def test_post_blocks_an_address_that_is_not_allowed(self, guard, path, body):
        key = "Name" if "Name" in body else "Description" if path == "/uptime" else "Title"
        refused(guard, build("POST", path, make(body, **{key: "x bob@example.com"})), "email address that is not allowed")


@pytest.mark.parametrize(
    "path, record, body",
    [
        ("/items", ITEM, {"Description": "new"}),
        ("/uptime", UPTIME, {"Description": "new"}),
        ("/projects", PROJECT, {"Title": "new"}),
    ],
    ids=["items", "uptime", "projects"],
)
class TestChangeRunRecordsOnly:
    def test_patch_allows_a_run_record_and_test_client(self, guard, path, record, body):
        guard.check(build("PATCH", f"{path}/{record}", body))
        guard.check(build("PATCH", f"{path}/{record}", make(body, ClientId=TEST_CLIENT)))

    @pytest.mark.parametrize("client", [SECOND, 5555, 0])
    def test_patch_blocks_moving_the_record_to_another_client(self, guard, path, record, body, client):
        refused(guard, build("PATCH", f"{path}/{record}", make(body, ClientId=client)), "ClientId must be 9501")

    def test_patch_blocks_a_record_of_a_customer(self, guard, path, record, body):
        refused(guard, build("PATCH", f"{path}/{FOREIGN}", body), "is not a .* created by this run")
        refused(guard, build("PATCH", f"{path}/not-an-id", body), "not a valid")


@pytest.mark.parametrize(
    "target",
    [
        {"Url": "https://mcp.example.net/.well-known/oauth-authorization-server"},
        {"Url": "http://example.net/"},
        {"Url": "https://a.b.EXAMPLE.NET:8443/x?y=1"},
        {"Url": "https://mcp.example.net/", "Ip": None, "Port": 443},
        {"Url": "https://mcp.example.net/", "Ip": ""},
        {"Port": 443},
        {},
    ],
)
def test_an_uptime_check_may_probe_only_the_probe_domain(guard, target):
    guard.check(build("POST", "/uptime", make(UPTIME_BODY, Target=target)))
    guard.check(build("PATCH", f"/uptime/{UPTIME}", {"Target": target}))


@pytest.mark.parametrize(
    "target, match",
    [
        ({"Url": "https://example.com/"}, "Target.Url must be a http\\(s\\) URL on example.net"),
        ({"Url": "https://example.net.evil.example/"}, "Target.Url must be"),
        ({"Url": "https://evilexample.net/"}, "Target.Url must be"),
        ({"Url": "https://mcp.example.net@evil.example/"}, "email address that is not allowed|Target.Url must be"),
        ({"Url": "https://evil.example\\@mcp.example.net/"}, "email address that is not allowed|Target.Url must be"),
        ({"Url": "https://evil.example\\.mcp.example.net/"}, "Target.Url must be"),
        ({"Url": "ftp://mcp.example.net/"}, "Target.Url must be"),
        ({"Url": "mcp.example.net"}, "Target.Url must be"),
        ({"Url": "https://mcp.example.net/ with space"}, "Target.Url must be"),
        ({"Url": "https://mcp.example.n\u0435t/"}, "Target.Url must be"),
        ({"Url": "https://[::1/"}, "Target.Url must be"),
        ({"Url": 5}, "Target.Url must be"),
        ({"Url": ""}, "Target.Url must be"),
        ({"Ip": "10.0.0.5"}, "Target.Ip is not allowed"),
        ({"Ip": "10.0.0.5", "Port": 22}, "Target.Ip is not allowed"),
        ({"Url": "https://mcp.example.net/", "Ip": "10.0.0.5"}, "Target.Ip is not allowed"),
        ("https://mcp.example.net/", "Target must be an object"),
        (["https://mcp.example.net/"], "Target must be an object"),
    ],
)
def test_an_uptime_check_that_would_probe_anything_else_is_blocked(guard, target, match):
    refused(guard, build("POST", "/uptime", make(UPTIME_BODY, Target=target)), match)
    refused(guard, build("PATCH", f"/uptime/{UPTIME}", {"Target": target}), match)


def test_the_probe_domains_can_be_changed(manifest):
    guard = LiveGuard("write", manifest, probe_domains={"Client.Example"})
    guard.check(build("POST", "/uptime", make(UPTIME_BODY, Target={"Url": "https://x.client.example/"})))
    refused(guard, build("POST", "/uptime", make(UPTIME_BODY)), "Target.Url must be a http\\(s\\) URL on client.example")


@pytest.mark.parametrize("value", [True, 1, "true", "True", 2, [True], {"x": 1}])
def test_an_uptime_check_may_not_adopt_client_assets(guard, value):
    refused(guard, build("POST", "/uptime", make(UPTIME_BODY, AdoptClientAssets=value)), "AdoptClientAssets must be absent or false")
    refused(guard, build("PATCH", f"/uptime/{UPTIME}", {"AdoptClientAssets": value}), "AdoptClientAssets must be absent or false")


@pytest.mark.parametrize("value", [False, None])
def test_an_uptime_check_may_say_it_adopts_nothing(guard, value):
    guard.check(build("POST", "/uptime", make(UPTIME_BODY, AdoptClientAssets=value)))
    guard.check(build("PATCH", f"/uptime/{UPTIME}", {"AdoptClientAssets": value}))


def test_an_uptime_maintenance_window_is_a_run_check_patch(guard):
    guard.check(build("PATCH", f"/uptime/{UPTIME}", {"MaintenanceMode": {"Enabled": True}}))
    refused(guard, build("PATCH", f"/uptime/{FOREIGN}", {"MaintenanceMode": {"Enabled": True}}), "not a uptime check created by this run")


# --------------------------------------------------------------------------
# the project tree
# --------------------------------------------------------------------------

PRIVATE = {"Body": "<p>MCPTEST</p>", "ConversationTypeId": 2}


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("POST", f"/projects/{PROJECT}/sections", {"Title": "MCPTEST section"}),
        ("PATCH", f"/projects/{PROJECT}/sections/{SECTION}", {"Title": "renamed"}),
        ("POST", f"/projects/{PROJECT}/tasks", {"Title": "MCPTEST task", "SectionId": SECTION, "LeadAssigneeId": OPERATOR_USER}),
        ("PATCH", f"/projects/{PROJECT}/tasks/{TASK}", {"BlockedByTaskIds": [TASK], "BlockingTaskIds": [TASK]}),
        ("PATCH", f"/projects/{PROJECT}/tasks/{TASK}", {"StatusId": 2, "WatcherIds": [OPERATOR_USER]}),
        ("POST", f"/projects/{PROJECT}/comments", PRIVATE),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", PRIVATE),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 3, "ConversationId": str(TASK_SIDE)}),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 4, "ConversationId": TASK_APPROVAL}),
        ("POST", f"/projects/{PROJECT}/tasks/{OTHER_TASK}/comments", {**PRIVATE, "ConversationTypeId": 3, "ConversationId": str(OTHER_TASK_SIDE)}),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 4, "ConversationId": TASK_APPROVAL.upper()}),
        ("PATCH", f"/projects/{PROJECT}", {"Title": "renamed", "SharedWithContactIds": [OPERATOR_CONTACT, RUN_CONTACT]}),
    ],
)
def test_the_project_tree_allows_run_created_ids_and_private_comments(guard, method, path, body):
    guard.check(build(method, path, body))


@pytest.mark.parametrize(
    "method, path, body, match",
    [
        ("POST", f"/projects/{FOREIGN}/sections", {"Title": "x"}, "is not a project created by this run"),
        ("PATCH", f"/projects/{PROJECT}/sections/{FOREIGN}", {"Title": "x"}, "is not a project section created by this run"),
        ("PATCH", f"/projects/{FOREIGN}/sections/{SECTION}", {"Title": "x"}, "is not a project created by this run"),
        ("POST", f"/projects/{FOREIGN}/tasks", {"Title": "x"}, "is not a project created by this run"),
        ("POST", f"/projects/{PROJECT}/tasks", {"Title": "x", "SectionId": FOREIGN}, "SectionId .* is not a project section created"),
        ("POST", f"/projects/{PROJECT}/tasks", {"Title": "x", "SectionId": 5}, "SectionId must be a UUID string"),
        ("PATCH", f"/projects/{PROJECT}/tasks/{TASK}", {"BlockedByTaskIds": [FOREIGN]}, "BlockedByTaskIds .* not a project task created"),
        ("PATCH", f"/projects/{PROJECT}/tasks/{TASK}", {"BlockingTaskIds": [TASK, FOREIGN]}, "BlockingTaskIds .* not a project task created"),
        ("POST", f"/projects/{PROJECT}/tasks", {"Title": "x", "LeadAssigneeId": OTHER_USER}, f"LeadAssigneeId {OTHER_USER} is not allowed"),
        ("POST", f"/projects/{PROJECT}/tasks", {"Title": "x", "WatcherIds": [OTHER_USER]}, "WatcherIds holds user"),
        ("POST", f"/projects/{PROJECT}/tasks", {"Title": "x", "AssistingAssigneeIds": [OTHER_USER]}, "AssistingAssigneeIds holds user"),
        ("PATCH", f"/projects/{PROJECT}/tasks/{FOREIGN}", {"StatusId": 2}, "is not a project task created by this run"),
        ("PATCH", f"/projects/{PROJECT}/tasks/{TASK}", {"AgentAssetIds": [uid(70)]}, "AgentAssetIds must be empty"),
        ("PATCH", f"/projects/{PROJECT}", {"SharedWithContactIds": [5]}, "SharedWithContactIds holds contact 5"),
        ("PATCH", f"/projects/{PROJECT}", {"ClientId": SECOND}, "ClientId must be 9501"),
        ("PATCH", f"/projects/{FOREIGN}", {"Title": "x"}, "is not a project created by this run"),
        ("POST", f"/projects/{FOREIGN}/comments", PRIVATE, "is not a project created by this run"),
        ("POST", f"/projects/{PROJECT}/tasks/{FOREIGN}/comments", PRIVATE, "is not a project task created by this run"),
        ("POST", f"/projects/{FOREIGN}/tasks/{TASK}/comments", PRIVATE, "is not a project created by this run"),
        ("POST", f"/projects/{PROJECT}/comments", {**PRIVATE, "ConversationTypeId": 1}, "project comment must be Private"),
        ("POST", f"/projects/{PROJECT}/comments", {**PRIVATE, "ConversationTypeId": 3}, "project comment must be Private"),
        ("POST", f"/projects/{PROJECT}/comments", {"Body": "x"}, "ConversationTypeId is required"),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 1}, "task comment must be Private"),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {"Body": "x"}, "ConversationTypeId is required"),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "Body": "bob@example.com"}, "email address that is not allowed"),
        # a side conversation or approval comment on a task goes only into one this run created on THAT task
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 3}, "ConversationId is required for this comment"),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 4, "ConversationId": None}, "ConversationId is required for this comment"),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 3, "ConversationId": uid(80)}, "is not one of the side conversations this run created"),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 4, "ConversationId": uid(80)}, "is not one of the approvals this run created"),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 3, "ConversationId": str(STRANGER_SIDE)}, "is not one of the side conversations this run created"),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 3, "ConversationId": TASK_APPROVAL}, "is not one of the side conversations this run created"),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 4, "ConversationId": str(TASK_SIDE)}, "is not one of the approvals this run created"),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 3, "ConversationId": str(OTHER_TASK_SIDE)}, "belongs to project task"),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 4, "ConversationId": OTHER_TASK_APPROVAL}, "belongs to project task"),
        ("POST", f"/projects/{PROJECT}/tasks/{OTHER_TASK}/comments", {**PRIVATE, "ConversationTypeId": 3, "ConversationId": str(TASK_SIDE)}, "belongs to project task"),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 3, "ConversationId": str(SIDE_OF[TICKET_OPERATOR])}, "the manifest has no valid task_id"),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 4, "ConversationId": APPROVAL_OF[TICKET_OPERATOR]}, "the manifest has no valid task_id"),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationTypeId": 3, "ConversationId": 711}, "ConversationId must be a string"),
        ("POST", f"/projects/{PROJECT}/tasks/{TASK}/comments", {**PRIVATE, "ConversationId": str(TASK_SIDE)}, "ConversationId must be absent on a public or private comment"),
        ("POST", f"/projects/{PROJECT}/comments", {**PRIVATE, "ConversationId": str(TASK_SIDE)}, "ConversationId must be absent on a public or private comment"),
        ("POST", f"/projects/{PROJECT}/comments", {**PRIVATE, "ConversationId": uid(80)}, "ConversationId must be absent on a public or private comment"),
    ],
)
def test_the_project_tree_blocks_foreign_ids_non_private_comments_and_other_people(guard, method, path, body, match):
    refused(guard, build(method, path, body), match)


# --------------------------------------------------------------------------
# form submission links and alerts
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [{"TicketId": TICKET_NONE}, {"TaskId": TASK}, {"TicketId": TICKET_OPERATOR, "TaskId": TASK}],
)
def test_a_submission_link_for_a_run_ticket_or_task_is_allowed(guard, body):
    guard.check(build("POST", "/forms/some-form/submission-links", body))


@pytest.mark.parametrize(
    "body, match",
    [
        ({}, "TicketId or TaskId is required"),
        ({"TicketId": None}, "TicketId or TaskId is required"),
        ({"TicketId": FOREIGN}, "TicketId .* is not a ticket created by this run"),
        ({"TaskId": FOREIGN}, "TaskId .* is not a project task created by this run"),
        ({"TicketId": TICKET_NONE, "TaskId": FOREIGN}, "TaskId .* is not a project task created"),
        ({"TicketId": 5}, "TicketId must be a UUID string"),
    ],
)
def test_a_submission_link_for_anything_else_is_blocked(guard, body, match):
    refused(guard, build("POST", "/forms/some-form/submission-links", body), match)


@pytest.mark.parametrize("client", [TEST_CLIENT, SECOND, RUN_CLIENT])
def test_alerts_are_never_posted(guard, cleanup_guard, client):
    body = {"ClientId": client, "Name": "MCPTEST alert", "Resource": "mcptest", "Severity": 1}
    for subject in (guard, cleanup_guard):
        refused(subject, build("POST", "/alerts", body), "never posts alerts")


# --------------------------------------------------------------------------
# invoices: only a Draft for the test client, never emailed, and only a run-created Draft is deleted or exported
# --------------------------------------------------------------------------

INVOICE_LINE = {"ItemId": INVOICE_ITEM, "Quantity": 1, "Description": f"{RUN} invoice", "UnitPrice": 1.0}
# exactly the body create_invoice sends for the matrix's invoice (a test below compares it with the real tool)
INVOICE_BODY = {
    "ClientId": TEST_CLIENT,
    "StatusId": 1,
    "InvoiceDate": "2026-10-02T00:00:00Z",
    "DueDate": "2026-10-16T00:00:00Z",
    "Reference": f"{RUN} invoice",
    "LineItems": [INVOICE_LINE],
}


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"RecipientEmails": None},
        {"RecipientEmails": []},
        {"Reference": DROP},
        {"InvoiceDate": DROP, "DueDate": DROP},
        {"InvoiceDate": None, "DueDate": None, "Reference": None},
        {"LineItems": [INVOICE_LINE, {"ItemId": uid(95).upper(), "Quantity": 2.5, "UnitCost": 0, "DiscountPercent": 10, "TaxId": None}]},
        {"LineItems": [{"ItemId": INVOICE_ITEM, "Quantity": 1}]},
        {"Reference": "ask ops@example.com or a@example.invalid"},
    ],
)
def test_post_invoices_allows_a_draft_for_test_client_with_no_recipients(guard, cleanup_guard, overrides):
    for subject in (guard, cleanup_guard):
        subject.check(build("POST", "/invoices", make(INVOICE_BODY, **overrides)))


@pytest.mark.parametrize(
    "status, match",
    [
        (5, "StatusId 5 \\(Approved\\) is not allowed"),
        (DROP, "StatusId is required and must be the JSON integer 1"),
        (None, "StatusId is required and must be the JSON integer 1"),
        ("1", "StatusId must be exactly the JSON integer 1 \\(Draft\\), not text"),
        ("5", "StatusId must be exactly the JSON integer 1 \\(Draft\\), not text"),
        (1.0, "StatusId must be exactly the JSON integer 1 \\(Draft\\), not a decimal number"),
        (5.0, "StatusId must be exactly the JSON integer 1 \\(Draft\\), not a decimal number"),
        (True, "StatusId must be exactly the JSON integer 1 \\(Draft\\), not a boolean"),
        (False, "StatusId must be exactly the JSON integer 1 \\(Draft\\), not a boolean"),
        (0, "StatusId must be exactly the JSON integer 1 \\(Draft\\), not 0"),
        (2, "StatusId must be exactly the JSON integer 1 \\(Draft\\), not 2"),
        (3, "StatusId must be exactly the JSON integer 1 \\(Draft\\), not 3"),
        (4, "StatusId must be exactly the JSON integer 1 \\(Draft\\), not 4"),
        (6, "StatusId must be exactly the JSON integer 1 \\(Draft\\), not 6"),
        (-1, "StatusId must be exactly the JSON integer 1 \\(Draft\\), not -1"),
        ([1], "StatusId must be exactly the JSON integer 1 \\(Draft\\), not a list"),
        ({"Id": 1}, "StatusId must be exactly the JSON integer 1 \\(Draft\\), not an object"),
    ],
)
def test_post_invoices_blocks_every_status_but_the_integer_one(guard, cleanup_guard, status, match):
    for subject in (guard, cleanup_guard):
        error = refused(subject, build("POST", "/invoices", make(INVOICE_BODY, StatusId=status)), match)
        assert error.label == "POST /invoices"


def test_the_approved_status_is_refused_with_the_reason_it_would_reach_the_accounting_system(guard):
    error = refused(guard, build("POST", "/invoices", make(INVOICE_BODY, StatusId=5)), "pushed to the connected accounting system")
    assert "creates Drafts only" in str(error)


def test_a_status_id_smuggled_in_a_second_spelling_is_refused(guard):
    # json.loads keeps the LAST of two keys that differ only in case: the guard refuses the whole body instead
    body = json.dumps({**INVOICE_BODY, "statusId": 5})
    refused(guard, raw("POST", "/invoices", body), "same key twice")
    twice = '{"ClientId": 9501, "StatusId": 1, "StatusId": 5, "LineItems": [{"ItemId": "' + INVOICE_ITEM + '", "Quantity": 1}]}'
    refused(guard, raw("POST", "/invoices", twice), "same key twice")
    nested = json.dumps({**INVOICE_BODY, "LineItems": [{**INVOICE_LINE, "itemid": INVOICE_ITEM}]})
    refused(guard, raw("POST", "/invoices", nested), "same key twice")


@pytest.mark.parametrize(
    "client, match",
    [
        (SECOND, "ClientId must be 9501 \\(the test client\\), not 9502"),
        (5555, "ClientId must be 9501 \\(the test client\\), not 5555"),
        (RUN_CLIENT, "ClientId must be 9501"),
        (0, "ClientId must be 9501"),
        ("9501", "ClientId must be a JSON integer"),
        (9501.0, "ClientId must be a JSON integer"),
        (True, "ClientId must be a JSON integer"),
        ([TEST_CLIENT], "ClientId must be a JSON integer"),
        (None, "ClientId is required and must be 9501"),
        (DROP, "ClientId is required and must be 9501"),
    ],
)
def test_post_invoices_blocks_any_client_but_test_client(guard, cleanup_guard, client, match):
    for subject in (guard, cleanup_guard):
        refused(subject, build("POST", "/invoices", make(INVOICE_BODY, ClientId=client)), match)


@pytest.mark.parametrize(
    "recipients",
    [
        ["ops@example.com"],  # not even the operator
        ["OPS@EXAMPLE.COM"],
        ["mcptest@example.invalid"],  # not even a test address
        ["ops@example.com", "mcptest@example.invalid"],
        "ops@example.com",
        [""],
        [None],
        "",
        0,
        False,
        True,
        {},
        {"a": "ops@example.com"},
    ],
)
def test_post_invoices_never_names_a_recipient_not_even_the_operator(guard, cleanup_guard, recipients):
    for subject in (guard, cleanup_guard):
        error = refused(
            subject,
            build("POST", "/invoices", make(INVOICE_BODY, RecipientEmails=recipients)),
            "RecipientEmails must be absent, null or empty",
        )
        assert "never emails an invoice, not even to the operator" in str(error)


@pytest.mark.parametrize("recipients", [["bob@example.com"], ["ops@example.com", "bob@example.com"], ["bob&#64;example.com"]])
def test_post_invoices_with_a_stranger_as_recipient_is_refused_by_the_address_rule(guard, recipients):
    error = refused(guard, build("POST", "/invoices", make(INVOICE_BODY, RecipientEmails=recipients)), "email address that is not allowed")
    assert "bob@" not in str(error)


@pytest.mark.parametrize(
    "lines, match",
    [
        (DROP, "LineItems must be a non-empty list of line objects"),
        (None, "LineItems must be a non-empty list of line objects"),
        ([], "LineItems must be a non-empty list of line objects"),
        ("x", "LineItems must be a non-empty list of line objects"),
        ({}, "LineItems must be a non-empty list of line objects"),
        ({"ItemId": INVOICE_ITEM}, "LineItems must be a non-empty list of line objects"),
        ([None], "LineItems\\[0\\] must be an object"),
        (["x"], "LineItems\\[0\\] must be an object"),
        ([[INVOICE_ITEM]], "LineItems\\[0\\] must be an object"),
        ([{}], "LineItems\\[0\\].ItemId must be a UUID string"),
        ([{"Quantity": 1}], "LineItems\\[0\\].ItemId must be a UUID string"),
        ([{"ItemId": None}], "LineItems\\[0\\].ItemId must be a UUID string"),
        ([{"ItemId": 5}], "LineItems\\[0\\].ItemId must be a UUID string"),
        ([{"ItemId": True}], "LineItems\\[0\\].ItemId must be a UUID string"),
        ([{"ItemId": [INVOICE_ITEM]}], "LineItems\\[0\\].ItemId must be a UUID string"),
        ([{"ItemId": "not-a-uuid"}], "LineItems\\[0\\].ItemId must be a UUID string"),
        ([{"ItemId": ""}], "LineItems\\[0\\].ItemId must be a UUID string"),
        ([{"ItemId": INVOICE_ITEM.replace("-", "")}], "LineItems\\[0\\].ItemId must be a UUID string"),
        ([{"ItemId": "{" + INVOICE_ITEM + "}"}], "LineItems\\[0\\].ItemId must be a UUID string"),
        ([{"ItemId": " " + INVOICE_ITEM}], "LineItems\\[0\\].ItemId must be a UUID string"),
        ([{"ItemId": INVOICE_ITEM + "\n"}], "LineItems\\[0\\].ItemId must be a UUID string"),
        ([INVOICE_LINE, {"ItemId": "x"}], "LineItems\\[1\\].ItemId must be a UUID string"),
        ([INVOICE_LINE, None], "LineItems\\[1\\] must be an object"),
    ],
)
def test_post_invoices_needs_lines_that_each_name_an_item_by_uuid(guard, lines, match):
    refused(guard, build("POST", "/invoices", make(INVOICE_BODY, LineItems=lines)), match)


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"Unknown": 1}, "field the spec does not define"),
        ({"ContactId": OPERATOR_CONTACT}, "field the spec does not define"),
        ({"LeadAssigneeId": OPERATOR_USER}, "field the spec does not define"),
        ({"clientId": SECOND}, "same key twice"),
        ({"Reference": "send it to bob@example.com"}, "email address that is not allowed"),
        ({"Reference": "bob\uff20example.com"}, "email address that is not allowed"),
        ({"LineItems": [make(INVOICE_LINE, Description="mail bob@example.com")]}, "LineItems\\[0\\]\\.Description holds an email address"),
        ({"LineItems": [make(INVOICE_LINE, **{"bob@example.com": 1})]}, "email address that is not allowed"),
    ],
)
def test_the_rules_for_every_json_body_still_apply_to_an_invoice_body(guard, overrides, match):
    refused(guard, build("POST", "/invoices", make(INVOICE_BODY, **overrides)), match)


def test_post_invoices_refuses_anything_that_is_not_one_json_object(guard):
    refused(guard, build("POST", "/invoices", None), "no JSON body")
    refused(guard, raw("POST", "/invoices", "[]"), "the body is not a JSON object")
    refused(guard, raw("POST", "/invoices", "not json"), "the body is not valid JSON")
    refused(guard, build("POST", "/invoices?StatusId=1", INVOICE_BODY), "must not carry a query string")


def test_post_invoices_needs_an_open_invoice_intent_when_the_guard_requires_intents(manifest):
    strict = LiveGuard("write", manifest, require_intents=True)
    request = build("POST", "/invoices", INVOICE_BODY)
    refused(strict, request, "no open intent of kind 'invoice': call manifest.intent\\('invoice', label\\)")
    manifest.intent("item", manifest.label("another kind"))
    refused(strict, request, "no open intent of kind 'invoice'")  # an intent of another kind does not count
    seq = manifest.intent("invoice", manifest.label("invoice"))
    strict.check(request)
    manifest.intent_failed(seq, "Gorelo answered 400")
    refused(strict, request, "no open intent of kind 'invoice'")  # a settled intent is no longer open


def test_a_bad_invoice_body_is_still_refused_when_an_intent_is_open(manifest):
    manifest.intent("invoice", manifest.label("invoice"))
    strict = LiveGuard("write", manifest, require_intents=True)
    refused(strict, build("POST", "/invoices", make(INVOICE_BODY, StatusId=5)), "StatusId 5 \\(Approved\\) is not allowed")
    refused(strict, build("POST", "/invoices", make(INVOICE_BODY, ClientId=SECOND)), "ClientId must be 9501")
    strict.check(build("POST", "/invoices", INVOICE_BODY))


def test_a_renamed_placeholder_of_the_invoice_delete_leaves_it_blocked_never_allowed(manifest):
    # the allowlist rules are keyed by the exact operation (only the never rules and the PDF are matched by shape): a spec that
    # renames {invoiceId} must make the delete fail closed until the rules are updated, never fall back to a rule-less allow
    data = json.loads((Path(__file__).resolve().parent.parent / "spec" / "spec_index.json").read_text(encoding="utf-8"))
    entry = data["ops"].pop("DELETE /v1/invoices/{invoiceId}")
    renamed = "DELETE /v1/invoices/{id}"
    data["ops"][renamed] = {**entry, "path": "/v1/invoices/{id}", "path_params": {"id": entry["path_params"]["invoiceId"]}}
    for cleanup in (False, True):
        subject = LiveGuard("write", manifest, cleanup=cleanup, spec=SpecIndex(data))
        refused(subject, build("DELETE", f"/invoices/{INVOICE}"), "not on the live-test allowlist")
        assert subject.rule_table()[renamed] == "blocked: not on the allowlist"


@pytest.mark.parametrize("invoice", [INVOICE, INVOICE.upper()])
def test_delete_of_an_invoice_the_run_created_as_a_draft_is_allowed(guard, cleanup_guard, invoice):
    for subject in (guard, cleanup_guard):
        subject.check(build("DELETE", f"/invoices/{invoice}"))


@pytest.mark.parametrize(
    "invoice, match",
    [
        (FOREIGN, "path invoiceId .* is not a test invoice created by this run"),  # a customer's invoice
        (uid(5), "is not a test invoice created by this run"),
        (ITEM, "is not a test invoice created by this run"),  # another kind's id
        (TICKET_NONE, "is not a test invoice created by this run"),
        ("abc", "is not a valid test invoice id"),
        ("5", "is not a valid test invoice id"),
        (INVOICE.replace("-", ""), "is not a valid test invoice id"),
        (INVOICE_APPROVED, "invoice .* is not recorded as a Draft \\(the manifest details status_id is 5, not 1\\)"),
        (INVOICE_NO_STATUS, "invoice .* is not recorded as a Draft \\(the manifest details status_id is missing, not 1\\)"),
    ],
)
def test_delete_of_any_other_invoice_is_blocked(guard, cleanup_guard, invoice, match):
    for subject in (guard, cleanup_guard):
        error = refused(subject, build("DELETE", f"/invoices/{invoice}"), match)
        assert error.label == f"DELETE /invoices/{invoice}"


@pytest.mark.parametrize(
    "status, shown",
    [
        ("1", "text"),
        (True, "a boolean"),
        (False, "a boolean"),
        (1.0, "a decimal number"),
        (None, "null"),
        (0, "0"),
        (3, "3"),
        (4, "4"),
        (5, "5"),
        (6, "6"),
        ([1], "a list"),
        ({"Id": 1}, "an object"),
    ],
)
def test_delete_of_an_invoice_recorded_with_anything_but_the_integer_one_is_blocked(manifest, status, shown):
    manifest.created("invoice", uid(96), manifest.label("odd invoice"), {"status_id": status})
    for subject in (LiveGuard("write", manifest), LiveGuard("write", manifest, cleanup=True)):
        refused(subject, build("DELETE", f"/invoices/{uid(96)}"), f"manifest details status_id is {shown}, not 1")
        # the export is judged by the same record: only a Draft the run created is ever exported
        refused(subject, build("GET", f"/invoices/{uid(96)}/pdf"), f"manifest details status_id is {shown}, not 1")


def test_delete_of_an_invoice_with_a_body_or_a_query_is_blocked(guard):
    refused(guard, build("DELETE", f"/invoices/{INVOICE}", {"force": True}), "takes no body")
    refused(guard, build("DELETE", f"/invoices/{INVOICE}?force=true"), "must not carry a query string")


def test_a_draft_that_was_approved_afterwards_is_not_the_guards_business_the_harness_reads_it_first():
    # documented in the module docstring: the guard only knows what the manifest says; the matrix and the cleanup read the
    # invoice back before they send a DELETE (tests/test_live_write_matrix.py and tests/test_live_manifest.py)
    assert "caught by the harness, not by the guard" in guard_module.__doc__


def test_invoice_ids_are_matched_by_what_they_name_not_by_how_they_are_spelled(manifest):
    manifest.created("invoice", uid(97).upper(), manifest.label("upper case"), {"status_id": 1})
    for subject in (LiveGuard("write", manifest), LiveGuard("write", manifest, cleanup=True)):
        subject.check(build("DELETE", f"/invoices/{uid(97)}"))
        subject.check(build("GET", f"/invoices/{uid(97)}/pdf"))


# --------------------------------------------------------------------------
# the one Approved invoice (allow_approved_invoice): off by default, and then only under strict rules
# --------------------------------------------------------------------------

APPROVED_LINE = {"ItemId": INVOICE_ITEM, "Quantity": 1, "Description": APPROVED_LABEL, "UnitPrice": 1.0, "TaxId": None}
# exactly the body create_approved_invoice sends for the matrix's approved invoice (a test below compares it with the real tool)
APPROVED_BODY = {"ClientId": TEST_CLIENT, "StatusId": 5, "Reference": APPROVED_LABEL, "LineItems": [APPROVED_LINE]}
DRAFT_OR_APPROVED = "StatusId must be exactly the JSON integer 1 \\(Draft\\) or 5 \\(Approved\\), not "


def approved_request(**overrides):
    return build("POST", "/invoices", make(APPROVED_BODY, **overrides))


def test_the_approved_status_is_refused_without_the_option_exactly_as_before(approved_manifest):
    # an announced intent and a perfect body do not matter: only the option opens the rule
    for subject in (
        LiveGuard("write", approved_manifest),
        LiveGuard("write", approved_manifest, allow_approved_invoice=False),
        LiveGuard("write", approved_manifest, cleanup=True),
        LiveGuard("write", approved_manifest, require_intents=True),
    ):
        error = refused(subject, approved_request(), "StatusId 5 \\(Approved\\) is not allowed")
        assert error.label == "POST /invoices"
        assert "pushed to the connected accounting system" in str(error) and "creates Drafts only" in str(error)
    assert len(approved_manifest.unresolved_intents()) == 1  # nothing consumed the intent


def test_the_option_is_off_by_default_and_needs_write_mode(manifest):
    assert LiveGuard("write", manifest).allow_approved_invoice is False
    assert LiveGuard("write", manifest, cleanup=True).allow_approved_invoice is False
    assert LiveGuard("write", manifest, allow_approved_invoice=True).allow_approved_invoice is True
    with pytest.raises(ValueError, match="allow_approved_invoice=True only makes sense in write mode"):
        LiveGuard("read", None, allow_approved_invoice=True)
    assert APPROVED_INVOICE_LIMIT == 1.0  # the $1 the harness allows


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"InvoiceDate": "2026-10-02T00:00:00Z", "DueDate": "2026-10-16T00:00:00Z"},  # Gorelo's defaults, or ours: no rule
        {"LineItems": [make(APPROVED_LINE, Quantity=0.5)]},
        {"LineItems": [make(APPROVED_LINE, Quantity=4, UnitPrice=0.25)]},  # exactly the approved $1
        {"LineItems": [make(APPROVED_LINE, Quantity=1.0, UnitPrice=1)]},
        {"LineItems": [make(APPROVED_LINE, Description=DROP)]},
        {"LineItems": [make(APPROVED_LINE, UnitCost=0)]},
        {"LineItems": [make(APPROVED_LINE, BillableStatusId=1)]},
        {"LineItems": [make(APPROVED_LINE, ItemId=uid(95).upper())]},
    ],
)
def test_the_approved_invoice_is_allowed_for_the_announced_label_when_every_rule_holds(approved_manifest, overrides):
    guard = LiveGuard("write", approved_manifest, allow_approved_invoice=True)
    guard.check(approved_request(**overrides))
    assert guard.allowed_count == 1 and not guard.tripped


def test_a_draft_is_still_allowed_with_the_option_under_the_old_rules(approved_manifest):
    guard = LiveGuard("write", approved_manifest, allow_approved_invoice=True)
    guard.check(build("POST", "/invoices", make(INVOICE_BODY, LineItems=[make(APPROVED_LINE, Description=DROP)])))
    refused(guard, build("POST", "/invoices", make(INVOICE_BODY, RecipientEmails=["ops@example.com"])), "RecipientEmails must be absent, null or empty")
    refused(guard, build("POST", "/invoices", make(INVOICE_BODY, StatusId=3)), DRAFT_OR_APPROVED + "3")


@pytest.mark.parametrize(
    "overrides, match",
    [
        # a status that is not exactly the integer 5 is the Draft rule's business: only 1 passes there
        ({"StatusId": "5"}, DRAFT_OR_APPROVED + "text"),
        ({"StatusId": 5.0}, DRAFT_OR_APPROVED + "a decimal number"),
        ({"StatusId": True}, DRAFT_OR_APPROVED + "a boolean"),
        ({"StatusId": [5]}, DRAFT_OR_APPROVED + "a list"),
        ({"StatusId": 3}, DRAFT_OR_APPROVED + "3"),
        ({"StatusId": 4}, DRAFT_OR_APPROVED + "4"),
        ({"StatusId": 6}, DRAFT_OR_APPROVED + "6"),
        ({"StatusId": DROP}, "StatusId is required"),
        ({"StatusId": None}, "StatusId is required"),
        # the client
        ({"ClientId": SECOND}, "ClientId must be 9501 \\(the test client\\), not 9502"),
        ({"ClientId": RUN_CLIENT}, "ClientId must be 9501"),
        ({"ClientId": "9501"}, "ClientId must be a JSON integer"),
        ({"ClientId": 9501.0}, "ClientId must be a JSON integer"),
        ({"ClientId": DROP}, "ClientId is required and must be 9501"),
        # nobody is emailed: not one RecipientEmails key, whatever it holds
        ({"RecipientEmails": ["ops@example.com"]}, "RecipientEmails must be absent"),
        ({"RecipientEmails": ["mcptest@example.invalid"]}, "RecipientEmails must be absent"),
        ({"RecipientEmails": []}, "RecipientEmails must be absent"),
        ({"RecipientEmails": None}, "RecipientEmails must be absent"),
        ({"RecipientEmails": ""}, "RecipientEmails must be absent"),
        ({"RecipientEmails": [None]}, "RecipientEmails must be absent"),
        ({"RecipientEmails": ["bob@example.com"]}, "email address that is not allowed"),
        # exactly one line
        ({"LineItems": DROP}, "must have exactly one line item"),
        ({"LineItems": None}, "must have exactly one line item"),
        ({"LineItems": []}, "must have exactly one line item"),
        ({"LineItems": "x"}, "must have exactly one line item"),
        ({"LineItems": {}}, "must have exactly one line item"),
        ({"LineItems": [APPROVED_LINE, APPROVED_LINE]}, "must have exactly one line item"),
        ({"LineItems": [None]}, "LineItems\\[0\\] must be an object"),
        ({"LineItems": ["x"]}, "LineItems\\[0\\] must be an object"),
        ({"LineItems": [[INVOICE_ITEM]]}, "LineItems\\[0\\] must be an object"),
        ({"LineItems": [make(APPROVED_LINE, ItemId=DROP)]}, "LineItems\\[0\\].ItemId must be a UUID string"),
        ({"LineItems": [make(APPROVED_LINE, ItemId=None)]}, "LineItems\\[0\\].ItemId must be a UUID string"),
        ({"LineItems": [make(APPROVED_LINE, ItemId=5)]}, "LineItems\\[0\\].ItemId must be a UUID string"),
        ({"LineItems": [make(APPROVED_LINE, ItemId="not-a-uuid")]}, "LineItems\\[0\\].ItemId must be a UUID string"),
        # a quantity above 0 and an explicit price above 0, so the total is never exactly 0 (that would be created as Paid)
        ({"LineItems": [make(APPROVED_LINE, Quantity=DROP)]}, "LineItems\\[0\\].Quantity must be a number above 0"),
        ({"LineItems": [make(APPROVED_LINE, Quantity=None)]}, "LineItems\\[0\\].Quantity must be a number above 0"),
        ({"LineItems": [make(APPROVED_LINE, Quantity=0)]}, "LineItems\\[0\\].Quantity must be a number above 0"),
        ({"LineItems": [make(APPROVED_LINE, Quantity=-1)]}, "LineItems\\[0\\].Quantity must be a number above 0"),
        ({"LineItems": [make(APPROVED_LINE, Quantity=True)]}, "LineItems\\[0\\].Quantity must be a number above 0"),
        ({"LineItems": [make(APPROVED_LINE, Quantity="1")]}, "LineItems\\[0\\].Quantity must be a number above 0"),
        ({"LineItems": [make(APPROVED_LINE, UnitPrice=DROP)]}, "UnitPrice must be given explicitly and be a number above 0"),
        ({"LineItems": [make(APPROVED_LINE, UnitPrice=None)]}, "UnitPrice must be given explicitly and be a number above 0"),
        ({"LineItems": [make(APPROVED_LINE, UnitPrice=0)]}, "UnitPrice must be given explicitly and be a number above 0"),
        ({"LineItems": [make(APPROVED_LINE, UnitPrice=0.0)]}, "UnitPrice must be given explicitly and be a number above 0"),
        ({"LineItems": [make(APPROVED_LINE, UnitPrice=-1.0)]}, "UnitPrice must be given explicitly and be a number above 0"),
        ({"LineItems": [make(APPROVED_LINE, UnitPrice=True)]}, "UnitPrice must be given explicitly and be a number above 0"),
        ({"LineItems": [make(APPROVED_LINE, UnitPrice="1.0")]}, "UnitPrice must be given explicitly and be a number above 0"),
        # no discount, and a billable line
        ({"LineItems": [make(APPROVED_LINE, DiscountPercent=0)]}, "DiscountPercent must be absent"),
        ({"LineItems": [make(APPROVED_LINE, DiscountPercent=None)]}, "DiscountPercent must be absent"),
        ({"LineItems": [make(APPROVED_LINE, DiscountPercent=10)]}, "DiscountPercent must be absent"),
        ({"LineItems": [make(APPROVED_LINE, BillableStatusId=2)]}, "BillableStatusId must be absent or exactly the JSON integer 1"),
        ({"LineItems": [make(APPROVED_LINE, BillableStatusId=3)]}, "BillableStatusId must be absent or exactly the JSON integer 1"),
        ({"LineItems": [make(APPROVED_LINE, BillableStatusId=None)]}, "BillableStatusId must be absent or exactly the JSON integer 1"),
        ({"LineItems": [make(APPROVED_LINE, BillableStatusId=True)]}, "BillableStatusId must be absent or exactly the JSON integer 1"),
        ({"LineItems": [make(APPROVED_LINE, BillableStatusId="1")]}, "BillableStatusId must be absent or exactly the JSON integer 1"),
        ({"LineItems": [make(APPROVED_LINE, BillableStatusId=1.0)]}, "BillableStatusId must be absent or exactly the JSON integer 1"),
        # no tax: TaxId present and exactly null, so Gorelo can neither fall back to the item's own tax nor pick one
        ({"LineItems": [make(APPROVED_LINE, TaxId=DROP)]}, "LineItems\\[0\\].TaxId must be given explicitly as null"),
        ({"LineItems": [make(APPROVED_LINE, TaxId=5)]}, "LineItems\\[0\\].TaxId must be given explicitly as null"),
        ({"LineItems": [make(APPROVED_LINE, TaxId=1)]}, "LineItems\\[0\\].TaxId must be given explicitly as null"),
        ({"LineItems": [make(APPROVED_LINE, TaxId=0)]}, "LineItems\\[0\\].TaxId must be given explicitly as null"),
        ({"LineItems": [make(APPROVED_LINE, TaxId=False)]}, "LineItems\\[0\\].TaxId must be given explicitly as null"),
        ({"LineItems": [make(APPROVED_LINE, TaxId="")]}, "LineItems\\[0\\].TaxId must be given explicitly as null"),
        ({"LineItems": [make(APPROVED_LINE, TaxId="5")]}, "LineItems\\[0\\].TaxId must be given explicitly as null"),
        ({"LineItems": [make(APPROVED_LINE, TaxId=[])]}, "LineItems\\[0\\].TaxId must be given explicitly as null"),
        ({"LineItems": [make(APPROVED_LINE, TaxId={})]}, "LineItems\\[0\\].TaxId must be given explicitly as null"),
        # the $1 the harness allows, and no more
        ({"LineItems": [make(APPROVED_LINE, Quantity=2)]}, "the line bills more than 1"),
        ({"LineItems": [make(APPROVED_LINE, UnitPrice=1.01)]}, "the line bills more than 1"),
        ({"LineItems": [make(APPROVED_LINE, UnitPrice=5)]}, "the line bills more than 1"),
        ({"LineItems": [make(APPROVED_LINE, Quantity=100, UnitPrice=0.02)]}, "the line bills more than 1"),
        # the reference is the announced label, exactly, so the invoice can be found by it
        ({"Reference": DROP}, "Reference must be exactly the label of the open invoice intent"),
        ({"Reference": None}, "Reference must be exactly the label of the open invoice intent"),
        ({"Reference": ""}, "Reference must be exactly the label of the open invoice intent"),
        ({"Reference": 5}, "Reference must be exactly the label of the open invoice intent"),
        ({"Reference": f"{RUN} invoice"}, "Reference must be exactly the label of the open invoice intent"),
        ({"Reference": APPROVED_LABEL.lower()}, "Reference must be exactly the label of the open invoice intent"),
        ({"Reference": APPROVED_LABEL + " "}, "Reference must be exactly the label of the open invoice intent"),
        ({"Reference": "MCPTEST-20991002101501 approved invoice"}, "Reference must be exactly the label of the open invoice intent"),
        # the rules every body passes still apply
        ({"Unknown": 1}, "field the spec does not define"),
        ({"ContactId": OPERATOR_CONTACT}, "field the spec does not define"),
        ({"Description": "x"}, "field the spec does not define"),
        ({"clientId": SECOND}, "same key twice"),
        ({"statusId": 1}, "same key twice"),
    ],
)
def test_the_approved_invoice_is_refused_when_any_rule_is_broken(approved_manifest, overrides, match):
    guard = LiveGuard("write", approved_manifest, allow_approved_invoice=True)
    error = refused(guard, approved_request(**overrides), match)
    assert error.label == "POST /invoices" and guard.allowed_count == 0
    assert len(approved_manifest.unresolved_intents()) == 1  # a refusal consumes nothing


def test_a_refused_attempt_does_not_use_up_the_one_approved_invoice(approved_guard):
    refused(approved_guard, approved_request(ClientId=SECOND), "ClientId must be 9501")
    refused(approved_guard, approved_request(RecipientEmails=[]), "RecipientEmails must be absent")
    approved_guard.check(approved_request())
    assert approved_guard.allowed_count == 1


def test_an_approved_invoice_needs_no_tax_asked_for_explicitly_and_a_refusal_for_it_keeps_the_one_create(approved_guard):
    # the cap on Quantity x UnitPrice is before tax, so the line must say TaxId null itself: an omitted TaxId
    # falls back to the item's own tax (Gorelo's text) and a number picks a tax, and either would bill more than $1
    for taxed in (make(APPROVED_LINE, TaxId=DROP), make(APPROVED_LINE, TaxId=5)):
        error = refused(approved_guard, approved_request(LineItems=[taxed]), "TaxId must be given explicitly as null")
        assert error.label == "POST /invoices" and "the harness limits the one Approved invoice to $1 with no tax" in str(error)
        assert "omitted TaxId falls back to the item's own tax" in str(error)
    assert approved_guard.allowed_count == 0 and len(approved_guard.manifest.unresolved_intents()) == 1
    approved_guard.check(approved_request())  # TaxId null, the line the matrix sends: the one create is still there
    assert approved_guard.allowed_count == 1


def test_the_tax_rule_is_for_the_approved_invoice_only_a_draft_may_leave_its_tax_to_the_item(guard, cleanup_guard):
    for subject in (guard, cleanup_guard):
        subject.check(build("POST", "/invoices", make(INVOICE_BODY, LineItems=[make(APPROVED_LINE, TaxId=DROP)])))
        subject.check(build("POST", "/invoices", make(INVOICE_BODY, LineItems=[make(APPROVED_LINE, TaxId=5)])))


def test_a_second_approved_invoice_is_refused_by_the_guard_that_allowed_the_first(approved_guard):
    approved_guard.check(approved_request())
    error = refused(approved_guard, approved_request(), "this run already created an Approved invoice")
    assert "no other approved invoice may be created in this run" in str(error)
    assert approved_guard.allowed_count == 1 and len(approved_guard.violations) == 1


def test_the_approved_invoice_needs_an_open_intent_of_kind_invoice_with_status_id_5(tmp_path):
    manifest = Manifest(RUN, tmp_path / "m.json")
    guard = LiveGuard("write", manifest, allow_approved_invoice=True)
    request = approved_request()
    none = "no open intent of kind 'invoice' with details status_id 5: call manifest.intent\\('invoice', label, "
    refused(guard, request, none)
    manifest.intent("item", APPROVED_LABEL, {"status_id": 5})  # another kind does not count
    refused(guard, request, none)
    manifest.intent("invoice", manifest.label("invoice"), {"client_id": TEST_CLIENT, "status_id": 1})  # the Draft's intent
    refused(guard, request, none)
    for odd in ("5", True, 5.0, None, [5], 3):  # nothing but exactly the integer 5 announces an Approved invoice
        manifest.intent("invoice", APPROVED_LABEL, {"status_id": odd})
        refused(guard, request, none)
    manifest.intent("invoice", APPROVED_LABEL)  # no details at all
    refused(guard, request, none)
    seq = manifest.intent("invoice", APPROVED_LABEL, {"status_id": 5})
    guard.check(request)
    other = LiveGuard("write", manifest, allow_approved_invoice=True)
    manifest.intent_failed(seq, "Gorelo answered 400")  # a settled intent is no longer open
    refused(other, request, none)


def test_two_open_approved_intents_are_refused_because_one_of_them_may_already_exist(approved_manifest):
    approved_manifest.intent("invoice", approved_manifest.label("approved invoice 2"), {"status_id": 5})
    guard = LiveGuard("write", approved_manifest, allow_approved_invoice=True)
    refused(guard, approved_request(), "2 open invoice intents hold status_id 5")


@pytest.mark.parametrize("status", [5, 3, 4, None, "1", True, 0])
def test_an_invoice_record_that_is_not_a_draft_stops_a_second_approved_invoice(approved_manifest, status):
    # the first approved invoice was created and recorded (its intent is settled); a second one is announced
    approved_manifest.created("invoice", uid(70), APPROVED_LABEL, {"status_id": status, "number": 1043})
    second = approved_manifest.label("second approved invoice")
    approved_manifest.intent("invoice", second, {"status_id": 5})
    guard = LiveGuard("write", approved_manifest, allow_approved_invoice=True)
    error = refused(guard, approved_request(Reference=second), "no other approved invoice may be created in this run")
    assert "already holds 1 invoice record(s) that are not a Draft" in str(error)


def test_a_draft_record_does_not_stop_the_approved_invoice(approved_manifest):
    approved_manifest.created("invoice", uid(70), approved_manifest.label("invoice"), {"status_id": 1, "number": 1042})
    LiveGuard("write", approved_manifest, allow_approved_invoice=True).check(approved_request())


def test_a_cleanup_never_creates_an_approved_invoice_even_with_the_option(approved_manifest):
    guard = LiveGuard("write", approved_manifest, cleanup=True, allow_approved_invoice=True)
    refused(guard, approved_request(), "a cleanup never creates an invoice, so it never creates an Approved one")
    # a Draft stays a Draft for it, as before
    guard.check(build("POST", "/invoices", INVOICE_BODY))


def test_the_approved_invoice_needs_the_intent_even_when_the_guard_does_not_require_intents(approved_manifest, tmp_path):
    bare = Manifest(RUN, tmp_path / "bare.json")
    refused(LiveGuard("write", bare, allow_approved_invoice=True), approved_request(), "no open intent of kind 'invoice' with details status_id 5")
    strict = LiveGuard("write", approved_manifest, require_intents=True, allow_approved_invoice=True)
    strict.check(approved_request())
    # with require_intents the generic rule asks first: no open intent of kind invoice at all
    refused(LiveGuard("write", bare, require_intents=True, allow_approved_invoice=True), approved_request(), "no open intent of kind 'invoice': call")


def test_a_body_that_is_not_one_json_object_or_has_a_query_is_refused_for_an_approved_invoice_too(approved_guard):
    refused(approved_guard, build("POST", "/invoices", None), "no JSON body")
    refused(approved_guard, raw("POST", "/invoices", "[]"), "the body is not a JSON object")
    refused(approved_guard, build("POST", "/invoices?StatusId=5", APPROVED_BODY), "must not carry a query string")
    twice = '{"ClientId": 9501, "StatusId": 5, "StatusId": 1, "Reference": "' + APPROVED_LABEL + '"}'
    refused(approved_guard, raw("POST", "/invoices", twice), "same key twice")


@pytest.mark.parametrize("invoice", [INVOICE_APPROVED, INVOICE_APPROVED.upper()])
def test_delete_of_an_invoice_recorded_as_approved_is_allowed_only_with_the_option(
    guard, cleanup_guard, option_guard, option_cleanup_guard, invoice
):
    for subject in (option_guard, option_cleanup_guard):
        subject.check(build("DELETE", f"/invoices/{invoice}"))
    for subject in (guard, cleanup_guard):  # without it the refusal is exactly the old one
        error = refused(
            subject,
            build("DELETE", f"/invoices/{invoice}"),
            "invoice .* is not recorded as a Draft \\(the manifest details status_id is 5, not 1\\)",
        )
        assert "the live harness deletes only a Draft, because Gorelo voids an Approved invoice instead of deleting it" in str(error)


def test_a_draft_is_still_deleted_with_the_option(option_guard, option_cleanup_guard):
    for subject in (option_guard, option_cleanup_guard):
        subject.check(build("DELETE", f"/invoices/{INVOICE}"))


def test_a_delete_of_a_run_invoice_may_be_sent_again_because_the_guard_counts_nothing(
    guard, cleanup_guard, option_guard, option_cleanup_guard
):
    # the write matrix asks delete_invoice again when Gorelo answers it 429 after the client's own retries (Gorelo did
    # not process that request), and the client itself retries a 429 too. Both send the same DELETE more than once, so the
    # guard must judge it by the manifest record alone and count nothing: a once-only rule here would turn a rate limit into a
    # stopped run, with the Approved invoice left un-voided.
    for subject in (option_guard, option_cleanup_guard):
        for _ in range(4):
            subject.check(build("DELETE", f"/invoices/{INVOICE_APPROVED}"))  # the void of the Approved invoice
            subject.check(build("DELETE", f"/invoices/{INVOICE}"))  # and a Draft's delete
    for subject in (guard, cleanup_guard):
        for _ in range(4):
            subject.check(build("DELETE", f"/invoices/{INVOICE}"))
    assert not option_guard.violations and not guard.violations


@pytest.mark.parametrize(
    "invoice, match",
    [
        (FOREIGN, "path invoiceId .* is not a test invoice created by this run"),  # a customer's invoice
        (uid(5), "is not a test invoice created by this run"),
        (ITEM, "is not a test invoice created by this run"),
        ("abc", "is not a valid test invoice id"),
        ("5", "is not a valid test invoice id"),
        (INVOICE_NO_STATUS, "the manifest details status_id is missing, not 1"),
    ],
)
def test_with_the_option_any_other_invoice_is_still_refused(option_guard, option_cleanup_guard, invoice, match):
    for subject in (option_guard, option_cleanup_guard):
        error = refused(subject, build("DELETE", f"/invoices/{invoice}"), match)
        assert error.label == f"DELETE /invoices/{invoice}"


@pytest.mark.parametrize(
    "status, shown",
    [
        (3, "3"),
        (4, "4"),
        (6, "6"),
        (0, "0"),
        (None, "null"),
        ("5", "text"),
        (True, "a boolean"),
        (5.0, "a decimal number"),
        ([5], "a list"),
        ({"Id": 5}, "an object"),
    ],
)
def test_with_the_option_an_invoice_recorded_as_anything_but_1_or_5_is_never_deleted(manifest, status, shown):
    manifest.created("invoice", uid(96), manifest.label("odd invoice"), {"status_id": status})
    for subject in (
        LiveGuard("write", manifest, allow_approved_invoice=True),
        LiveGuard("write", manifest, cleanup=True, allow_approved_invoice=True),
    ):
        error = refused(subject, build("DELETE", f"/invoices/{uid(96)}"), f"manifest details status_id is {shown}, not 1")
        assert "voids only an invoice recorded as Approved (status_id 5), never one that is Paid (3), Void (4)" in str(error)
        # the export is judged by the same record: Draft only, option or not
        refused(subject, build("GET", f"/invoices/{uid(96)}/pdf"), "only a Draft this run created may be exported")


def test_the_pdf_export_stays_draft_only_with_the_option(option_guard, option_cleanup_guard):
    for subject in (option_guard, option_cleanup_guard):
        subject.check(build("GET", f"/invoices/{INVOICE}/pdf"))
        error = refused(subject, build("GET", f"/invoices/{INVOICE_APPROVED}/pdf"), "only a Draft this run created may be exported")
        assert "the manifest details status_id is 5, not 1" in str(error)
        refused(subject, build("GET", f"/invoices/{FOREIGN}/pdf"), "only an invoice this run created may be exported")


def test_the_option_opens_nothing_else(option_guard, option_cleanup_guard):
    # a Draft body still needs no recipient, a stranger's invoice is still not deleted, the other approvals stay shut
    for subject in (option_guard, option_cleanup_guard):
        refused(subject, build("DELETE", f"/invoices/{FOREIGN}"), "is not a test invoice created by this run")
        refused(subject, build("POST", "/alerts", {"ClientId": TEST_CLIENT, "Name": "x", "Resource": "y"}), "never posts alerts")
        refused(subject, build("POST", "/api-keys", {"Name": "x", "Scopes": ["Tickets:read"]}), "never creates API keys")
        refused(subject, build("DELETE", "/assets/agents/" + uid(5)), "writes under /assets/ are never allowed|UNINSTALLS")
        refused(subject, build("PATCH", f"/clients/{TEST_CLIENT}", {"AlternateName": "x"}), "is not allowed")


def test_the_rule_table_of_a_guard_with_the_option_says_what_the_option_adds(guard, option_guard):
    plain, extended = guard.rule_table(), option_guard.rule_table()
    changed = sorted(key for key in plain if plain[key] != extended[key])
    assert changed == ["DELETE /v1/invoices/{invoiceId}", "POST /v1/invoices"]
    for key in changed:  # the text of the default guard is untouched; the option adds one clause
        assert extended[key].startswith(plain[key] + "; with allow_approved_invoice also "), key
    assert "StatusId exactly 5 (Approved) for the one announced approved invoice" in extended["POST /v1/invoices"]
    assert "at most 1 in all" in extended["POST /v1/invoices"] and "never from a cleanup" in extended["POST /v1/invoices"]
    assert "TaxId present and null (no tax)" in extended["POST /v1/invoices"]  # the line must ask for no tax itself
    assert "invoice recorded with status_id 5, which is voided (StatusId 4, still listed)" in extended["DELETE /v1/invoices/{invoiceId}"]
    assert "Paid (3), Void (4) and an invoice with no recorded status are never deleted" in extended["DELETE /v1/invoices/{invoiceId}"]
    assert "does only with allow_approved_invoice" in plain["DELETE /v1/invoices/{invoiceId}"]
    assert extended["GET /v1/invoices/{invoiceId}/pdf"] == plain["GET /v1/invoices/{invoiceId}/pdf"]  # the export is Draft only


def test_every_write_operation_of_the_spec_is_blocked_for_a_stranger_with_the_option_too(option_guard, option_cleanup_guard):
    for key in WRITE_OPS:
        request = stranger_request(SPEC.op(key))
        for subject in (option_guard, option_cleanup_guard):
            with pytest.raises(GuardViolation):
                subject.check(request)


def test_the_guard_docstring_describes_the_approved_invoice_rules():
    text = " ".join(guard_module.__doc__.split())
    # the paragraph names both callers, and the older wording that only the matrix sets the option is gone
    assert (
        "The approved invoice (allow_approved_invoice=True, set by write_matrix --with-approved-invoice, and for the void "
        "alone by cleanup --void-approved)"
    ) in text
    assert "set only by write_matrix --with-approved-invoice" not in text
    for said in (
        "`allow_approved_invoice=True` (off by default; only write_matrix --with-approved-invoice and cleanup --void-approved set it)",
        "with a TaxId that is present and exactly null (the line's no_tax: Gorelo falls back to the item's own tax when TaxId is omitted",
        "Only with allow_approved_invoice=True is StatusId exactly the JSON integer 5 allowed too",
        "the body has no RecipientEmails key at all (not even null or empty)",
        "Reference is the label of an open manifest intent of kind \"invoice\" whose details hold status_id 5",
        "LineItems holds exactly one line, whose Quantity is above 0 and whose UnitPrice is given explicitly and is above 0",
        "no DiscountPercent key and BillableStatusId absent or exactly 1",
        "APPROVED_INVOICE_LIMIT (1.0, the $1 the harness allows)",
        "no other approved invoice was created in this run",
        "A guard built with cleanup=True never allows the create",
        "recorded with status_id 5 is allowed only with the option (it voids the invoice: StatusId 4, still listed)",
        "the PDF export stays Draft-only",
        "Without the option a StatusId of 5 and a DELETE of an invoice recorded as 5 are refused exactly as before",
    ):
        assert said in text, said


# --------------------------------------------------------------------------
# email addresses anywhere in a body
# --------------------------------------------------------------------------

ALLOWED_TEXT = [
    "ops@example.com",
    "OPS@EXAMPLE.COM",
    "Ops@Example.com.",
    "<ops@example.com>",
    "mailto:ops@example.com",
    "mcptest@example.invalid",
    "a.b+c_d%e-f@example.invalid",
    "A@EXAMPLE.INVALID",
    "write to ops@example.com, x@example.invalid; thanks",
    "(see ops@example.com)",
    "no address here at all",
    "100% sure & done, 50% off",
]


@pytest.mark.parametrize("text", ALLOWED_TEXT)
def test_allowed_addresses_and_text_without_addresses_pass(guard, text):
    guard.check(build("POST", f"/tickets/{TICKET_NONE}/comments", comment(2, Body=text)))


BLOCKED_TEXT = [
    ("bob@example.com", "b***@example.com"),
    ("bob@example.invalid.com", "b***@example.invalid.com"),
    ("bob@sub.example.invalid", "b***@sub.example.invalid"),
    ("bob@example.invalid@gmail.com", "b***@example.invalid@gmail.com"),
    ("ops@example.com.evil.example", "o***@example.com.evil.example"),
    ("ops@example.co", "o***@example.co"),
    ("x@example.com", "x***@example.com"),
    ("ops@example.com@evil.example", "o***@example.com@evil.example"),
    ("hello bob@example.com goodbye", "b***@example.com"),
    ("a@b", "a***@b"),
    ("@example.invalid", "***"),
    ("@ops", "***"),
    ("bob\uff20example.com", "b***@example.com"),  # a fullwidth at sign
    ("bob\ufe6bexample.com", "b***@example.com"),  # a small commercial at
    ("bob&#64;example.com", "b***@example.com"),  # an HTML entity
    ("bob&commat;example.com", "b***@example.com"),
    ("bob%40example.com", "b***@example.com"),  # percent encoding
    ("bob%2540example.com", "b***@example.com"),
    ("bob\u200b@example.com", "***"),  # a zero width space splits the token
    ("b\u00f6b@example.invalid", "***"),  # a non-ASCII local part
    ("bob@exampl\u0435.invalid", "***"),  # a Cyrillic letter in the domain
    ("<a href=\"mailto:bob@example.com\">x</a>", "b***@example.com"),
]


@pytest.mark.parametrize("text, shown", BLOCKED_TEXT)
def test_any_other_address_is_blocked_and_shown_masked(guard, text, shown):
    error = refused(guard, build("POST", f"/tickets/{TICKET_NONE}/comments", comment(2, Body=text)), "email address that is not allowed")
    assert shown in str(error)
    assert "bob@example.com" not in str(error) and "bob@" not in str(error)


def test_an_address_is_found_wherever_it_hides_in_a_body(guard):
    path = "/clients"
    ok = {"Name": f"{RUN} c", "Location": {"Name": "HQ", "Address1": "mail ops@example.com"}}
    guard.check(build("POST", path, ok))
    deep = {"Name": f"{RUN} c", "Location": {"Name": "HQ", "Address1": "write to bob@example.com"}}
    assert "Location.Address1 holds an email" in str(refused(guard, build("POST", path, deep), "email address that is not allowed"))
    as_key = {"Name": f"{RUN} c", "Location": {"Name": "HQ", "bob@example.com": "x"}}
    assert "<unusual name> holds an email" in str(refused(guard, build("POST", path, as_key), "email address that is not allowed"))
    in_list = make(CONTACT_BODY, SecondaryEmail=["fine@example.invalid", "bob@example.com"])
    assert "SecondaryEmail[1] holds an email" in str(refused(guard, build("POST", "/contacts", in_list), "email address that is not allowed"))
    nested_list = comment(2, Attachments=[{"Name": "a.txt", "Url": "https://files.example/a"}, {"Name": "b.txt", "Url": "https://x.example/?to=bob@example.com"}])
    assert "Attachments[1].Url" in str(refused(guard, build("POST", f"/tickets/{TICKET_NONE}/comments", nested_list), "email address that is not allowed"))


def test_the_allowed_addresses_and_the_test_domain_can_be_changed(manifest):
    guard = LiveGuard("write", manifest, allowed_emails={"Boss@Example.org"}, test_email_domain="Test.Invalid")
    guard.check(build("POST", f"/tickets/{TICKET_NONE}/comments", comment(2, Body="boss@example.org and x@test.invalid")))
    refused(guard, build("POST", f"/tickets/{TICKET_NONE}/comments", comment(2, Body="ops@example.com")), "email address that is not allowed")
    refused(guard, build("POST", f"/tickets/{TICKET_NONE}/comments", comment(2, Body="x@example.invalid")), "email address that is not allowed")


# --------------------------------------------------------------------------
# bodies the guard cannot trust
# --------------------------------------------------------------------------

GOOD_CLIENT = json.dumps(CLIENT_BODY)


@pytest.mark.parametrize(
    "content, match",
    [
        ('{"Name": "MCPTEST-a", "Name": "Acme", "Location": {"Name": "x"}}', "same key twice"),
        ('{"Name": "MCPTEST-a", "name": "Acme", "Location": {"Name": "x"}}', "same key twice"),
        ('{"Name": "MCPTEST-a", "Location": {"Name": "x", "NAME": "bob@example.com"}}', "same key twice"),
        ('{"name": "MCPTEST-a", "Location": {"Name": "x"}}', "field the spec does not define"),
        ('{"Name": "MCPTEST-a", "Location": {"Name": "x"}, "Extra": 1}', "field the spec does not define"),
        ("{", "not valid JSON"),
        ("not json", "not valid JSON"),
        ("[1, 2]", "not a JSON object"),
        ("5", "not a JSON object"),
        ('"text"', "not a JSON object"),
        ("null", "not a JSON object"),
        ("", "no JSON body"),
        ('{"Name": NaN}', "NaN, which is not valid JSON"),
        ('{"Name": Infinity}', "Infinity, which is not valid JSON"),
        (b"\xff\xfe{}", "not UTF-8"),
        (b"\xef\xbb\xbf" + GOOD_CLIENT.encode(), "not valid JSON"),  # a byte order mark
        ('{"Name": "MCPTEST-a", "Location": ' + "[" * 100 + "]" * 100 + "}", "nested too deeply"),
    ],
)
def test_a_body_that_cannot_be_read_exactly_is_blocked(guard, content, match):
    refused(guard, raw("POST", "/clients", content), match)


def test_the_same_well_formed_body_passes(guard):
    guard.check(raw("POST", "/clients", GOOD_CLIENT))


@pytest.mark.parametrize(
    "body, match",
    [
        ({"ClientId": "9501"}, "ClientId must be a JSON integer"),
        ({"ClientId": 9501.5}, "ClientId must be a JSON integer"),
        ({"ClientId": True}, "ClientId must be a JSON integer"),
    ],
)
def test_ids_must_be_real_json_integers(guard, body, match):
    refused(guard, build("PATCH", RUN_CONTACT_PATH, make(CONTACT_UPDATE, **body)), match)


@pytest.mark.parametrize("text", ["9501.0", "9.501e3", "95010e-1"])  # the test client's id, spelled as a float
def test_a_number_written_with_a_fraction_or_exponent_is_not_an_id(guard, text):
    spelled = json.dumps(CONTACT_UPDATE).replace(f'"ClientId": {TEST_CLIENT}', '"ClientId": ' + text)
    assert text in spelled
    refused(guard, raw("PATCH", RUN_CONTACT_PATH, spelled), "ClientId must be a JSON integer")
    guard.check(raw("PATCH", RUN_CONTACT_PATH, json.dumps(CONTACT_UPDATE)))


# --------------------------------------------------------------------------
# violations are kept, and the real async hook
# --------------------------------------------------------------------------


def test_every_refusal_is_kept_and_assert_clean_raises_the_first(guard):
    guard.check(build("GET", "/clients"))
    assert not guard.tripped and guard.violations == [] and guard.allowed_count == 1
    guard.assert_clean()
    refused(guard, build("POST", "/alerts", {"ClientId": TEST_CLIENT, "Name": "x", "Resource": "y"}), "never posts alerts")
    refused(guard, build("DELETE", "/clients/9501"), "never deleted")
    assert guard.tripped and len(guard.violations) == 2 and guard.allowed_count == 1
    assert guard.violations[0].startswith("blocked POST /alerts: ")
    with pytest.raises(GuardViolation, match="blocked POST /alerts"):
        guard.assert_clean()


def test_a_guard_violation_is_a_runtime_error_with_a_label_and_a_reason(guard):
    error = refused(guard, build("DELETE", "/clients/9801"), "was not created by this run")
    assert isinstance(error, RuntimeError)
    assert error.label == "DELETE /clients/9801"
    assert error.reason.startswith("client 9801 was not created by this run")
    assert str(error) == f"blocked {error.label}: {error.reason}"


def test_an_error_while_inspecting_blocks_the_request(guard, monkeypatch):
    def broken(*args, **kwargs):
        raise KeyError("boom")

    monkeypatch.setattr(guard, "_check_common_fields", broken)
    error = refused(guard, build("PATCH", f"/tickets/{TICKET_NONE}", {"Title": "x"}), "the guard could not inspect it \\(KeyError\\)")
    assert guard.tripped and "boom" not in str(error)


def test_messages_never_hold_the_api_key_or_the_whole_address(guard, read_guard):
    secret = TEST_API_KEY
    headers = {"X-API-Key": secret, "Authorization": f"Bearer {secret}"}
    requests = [
        build("POST", "/alerts", {"ClientId": TEST_CLIENT, "Name": "x", "Resource": "y"}, headers=headers),
        build("DELETE", "/clients/9501", headers=headers),
        build("POST", "/clients", {"Name": "Acme", "Location": {"Name": "x"}}, headers=headers),
        build("POST", f"/tickets/{TICKET_NONE}/comments", comment(2, Body="bob@example.com"), headers=headers),
        httpx.Request("GET", "https://evil.example/v1/clients", headers=headers),
        httpx.Request("GET", BASE + f"/invoices/{uid(5)}/pdf", headers=headers),
    ]
    for request in requests:
        for subject in (guard, read_guard):
            with pytest.raises(GuardViolation) as caught:
                subject.check(request)
            assert secret not in str(caught.value)
    assert all(secret not in text for text in guard.violations + read_guard.violations)
    assert guard.violations and read_guard.violations


async def test_the_async_hook_stops_a_request_before_the_transport_sees_it(guard):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=envelope({"Id": 1}))

    hooks = {"request": [guard]}
    async with httpx.AsyncClient(
        base_url=BASE, transport=httpx.MockTransport(handler), event_hooks=hooks, headers={"X-API-Key": TEST_API_KEY}
    ) as client:
        assert (await client.get("/clients")).status_code == 200
        assert [r.method for r in seen] == ["GET"]
        with pytest.raises(GuardViolation, match="never posts alerts"):
            await client.post("/alerts", json={"ClientId": TEST_CLIENT, "Name": "x", "Resource": "y"})
        with pytest.raises(GuardViolation, match="writes under /assets/"):
            await client.delete(f"/assets/agents/{uid(5)}")
        with pytest.raises(GuardViolation, match="never deleted"):
            await client.delete("/clients/9501")
        assert [r.method for r in seen] == ["GET"]  # nothing else was sent
        assert (await client.delete(f"/tickets/{TICKET_NONE}")).status_code == 200
        assert [r.method for r in seen] == ["GET", "DELETE"]
    assert guard.tripped and len(guard.violations) == 3


async def test_the_hook_reads_a_multipart_body_without_consuming_it(guard):
    seen = []

    def handler(request):
        seen.append(request.content)
        return httpx.Response(200, json=envelope({"Name": "note.txt", "Url": "https://files.example/x"}))

    async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler), event_hooks={"request": [guard]}) as client:
        response = await client.post(
            "/attachments",
            files={"file": ("note.txt", b"hello world", "text/plain")},
            data={"itemType": "Ticket", "itemId": TICKET_NONE},
        )
        assert response.status_code == 200
        with pytest.raises(GuardViolation, match="is not a ticket created by this run"):
            await client.post(
                "/attachments",
                files={"file": ("note.txt", b"hello world", "text/plain")},
                data={"itemType": "Ticket", "itemId": FOREIGN},
            )
    assert len(seen) == 1 and b"hello world" in seen[0] and TICKET_NONE.encode() in seen[0] and b'filename="note.txt"' in seen[0]


async def test_the_hook_blocks_a_body_it_cannot_read(guard):
    async def endless():
        yield b"{"
        raise RuntimeError("stream broke")

    request = httpx.Request("POST", BASE + "/clients", content=endless())
    with pytest.raises(GuardViolation, match="its body could not be read"):
        await guard(request)
    assert guard.tripped


async def test_the_hook_reads_a_streamed_body_and_the_request_can_still_be_sent(guard):
    async def chunks():
        yield json.dumps(CLIENT_BODY).encode("utf-8")

    sent = []
    transport = httpx.MockTransport(lambda request: sent.append(request.content) or httpx.Response(200, json=envelope({"Id": 5})))
    async with httpx.AsyncClient(base_url=BASE, transport=transport, event_hooks={"request": [guard]}) as client:
        assert (await client.post("/clients", content=chunks())).status_code == 200
    assert json.loads(sent[0])["Name"] == CLIENT_BODY["Name"]


# --------------------------------------------------------------------------
# through the real server and its tools
# --------------------------------------------------------------------------


async def test_a_tool_call_is_stopped_by_the_guard_before_a_request_leaves(server_factory, mock_gorelo, guard):
    server = server_factory(event_hooks={"request": [guard]}, destructive=True)
    mock_gorelo.on("PATCH", f"/v1/clients/{SECOND}", envelope({"Id": SECOND}))
    text = await call_tool_error(server, "update_client", {"client_id": SECOND, "alternate_name": "x"})
    assert "blocked PATCH /clients/9502: path id 9502 is not allowed" in text
    assert mock_gorelo.requests == []
    assert guard.tripped and guard.violations[0].startswith("blocked PATCH /clients/9502")


async def test_a_tool_call_for_the_run_client_goes_through_the_guard(server_factory, mock_gorelo, guard):
    server = server_factory(event_hooks={"request": [guard]}, destructive=True)
    record = {"Id": RUN_CLIENT, "Name": f"{RUN} temporary client", "AlternateName": "MCPTEST alt"}
    mock_gorelo.on("PATCH", f"/v1/clients/{RUN_CLIENT}", envelope(record))
    result = await call_tool(server, "update_client", {"client_id": RUN_CLIENT, "alternate_name": "MCPTEST alt"})
    assert result["Id"] == RUN_CLIENT
    # the id is in the path and not in the body (UpdateClientCommand has no Id): the guard accepts exactly that
    assert [(r.method, r.path, r.json) for r in mock_gorelo.requests] == [
        ("PATCH", f"/v1/clients/{RUN_CLIENT}", {"AlternateName": "MCPTEST alt"})
    ]
    assert not guard.tripped and guard.allowed_count == 1


async def test_a_tool_call_that_would_change_test_client_is_stopped_before_a_request_leaves(server_factory, mock_gorelo, guard):
    server = server_factory(event_hooks={"request": [guard]}, destructive=True)
    mock_gorelo.on("PATCH", f"/v1/clients/{TEST_CLIENT}", envelope({"Id": TEST_CLIENT}))
    text = await call_tool_error(server, "update_client", {"client_id": TEST_CLIENT, "alternate_name": "MCPTEST alt"})
    assert "blocked PATCH /clients/9501: path id 9501 is not allowed" in text and "is never changed" in text
    assert mock_gorelo.requests == [] and guard.tripped


def contact_record(ident, **extra):
    """A contact as GET /v1/contacts/{contactId} returns it: every field update_contact copies, plus the read-only ones."""
    record = {
        "Id": ident, "FirstName": "MCPTEST", "LastName": "Contact", "ClientId": TEST_CLIENT, "LocationId": None,
        "PrimaryEmail": "mcptest-20991002101500@example.invalid", "MobilePhone": "5555550142", "MobilePhoneCountryCode": "US",
        "OfficePhone": None, "OfficePhoneCountryCode": None, "JobTitle": None, "Department": None, "TimeZone": None,
        "Description": None, "Alias": None, "Status": {"Id": 1, "Name": "Active"}, "IsTemporaryContact": False,
    }
    record.update(extra)
    return record


async def test_a_tool_call_for_the_run_contact_goes_through_the_guard(server_factory, mock_gorelo, guard):
    server = server_factory(event_hooks={"request": [guard]}, destructive=True)
    mock_gorelo.on("GET", f"/v1/contacts/{RUN_CONTACT}", envelope(contact_record(RUN_CONTACT)))
    mock_gorelo.on("PATCH", f"/v1/contacts/{RUN_CONTACT}", envelope(contact_record(RUN_CONTACT, JobTitle="Tester")))
    arguments = {"contact_id": RUN_CONTACT, "job_title": "Tester", "clear_secondary_email_ok": True}
    result = await call_tool(server, "update_contact", arguments)
    assert result["JobTitle"] == "Tester"
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [
        ("GET", f"/v1/contacts/{RUN_CONTACT}"), ("PATCH", f"/v1/contacts/{RUN_CONTACT}")
    ]
    sent = mock_gorelo.requests[1].json  # the id is in the path only; the body is the whole contact on the test client
    assert "ContactId" not in sent and sent["ClientId"] == TEST_CLIENT and sent["JobTitle"] == "Tester"
    assert not guard.tripped and guard.allowed_count == 2


async def test_a_tool_call_that_would_change_the_operator_contact_is_stopped_before_the_write_leaves(
    server_factory, mock_gorelo, guard
):
    server = server_factory(event_hooks={"request": [guard]}, destructive=True)
    mock_gorelo.on("GET", f"/v1/contacts/{OPERATOR_CONTACT}", envelope(contact_record(OPERATOR_CONTACT, ClientId=SECOND)))
    mock_gorelo.on("PATCH", f"/v1/contacts/{OPERATOR_CONTACT}", envelope(contact_record(OPERATOR_CONTACT)))
    arguments = {"contact_id": OPERATOR_CONTACT, "job_title": "Boss", "clear_secondary_email_ok": True}
    text = await call_tool_error(server, "update_contact", arguments)
    assert "blocked PATCH /contacts/9600: path contactId 9600 is not a contact created by this run" in text
    assert [r.method for r in mock_gorelo.requests] == ["GET"]  # the read is allowed, the write never left
    assert guard.tripped and guard.violations[0].startswith("blocked PATCH /contacts/9600")


async def test_a_read_mode_guard_stops_write_tools_of_the_real_server(server_factory, mock_gorelo, read_guard):
    server = server_factory(event_hooks={"request": [read_guard]}, destructive=True)
    mock_gorelo.on("PATCH", f"/v1/clients/{TEST_CLIENT}", envelope({"Id": TEST_CLIENT}))
    text = await call_tool_error(server, "update_client", {"client_id": TEST_CLIENT, "alternate_name": "x"})
    assert "blocked PATCH /clients/9501: read mode allows only GET" in text
    assert mock_gorelo.requests == [] and read_guard.tripped


INVOICE_ARGUMENTS = {
    "client_id": TEST_CLIENT,
    "line_items": [{"item_id": INVOICE_ITEM, "quantity": 1, "description": f"{RUN} invoice", "unit_price": 1.0}],
    "invoice_date": "2026-10-02",
    "due_date": "2026-10-16",
    "reference": f"{RUN} invoice",
}


def invoice_record(ident, **extra):
    """An invoice as GET /v1/invoices/{invoiceId} returns it (the fields the harness reads)."""
    return {
        "Id": ident, "ClientId": TEST_CLIENT, "Number": 1042, "DisplayNumber": "INV-1042", "Reference": f"{RUN} invoice",
        "Status": {"Id": 1, "Name": "Draft"}, "InvoiceDate": "2026-10-02", "DueDate": "2026-10-16", "LineItems": [],
        **extra,
    }


async def test_the_create_invoice_tool_sends_exactly_the_body_the_guard_allows(server_factory, mock_gorelo, manifest):
    guard = LiveGuard("write", manifest, require_intents=True)
    server = server_factory(event_hooks={"request": [guard]}, destructive=True)
    new = uid(70)
    mock_gorelo.on("POST", "/v1/invoices", envelope({"Id": new}))
    mock_gorelo.on("GET", f"/v1/invoices/{new}", envelope(invoice_record(new)))
    manifest.intent("invoice", manifest.label("invoice"))
    result = await call_tool(server, "create_invoice", INVOICE_ARGUMENTS)
    assert result["Id"] == new and not guard.tripped
    post = mock_gorelo.requests[0]
    assert (post.method, post.path) == ("POST", "/v1/invoices")
    assert post.json == INVOICE_BODY  # the guard tests above use the very body the tool builds
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("POST", "/v1/invoices"), ("GET", f"/v1/invoices/{new}")]
    assert guard.allowed_count == 2


async def test_a_create_invoice_tool_call_without_an_intent_is_stopped_before_a_request_leaves(server_factory, mock_gorelo, manifest):
    guard = LiveGuard("write", manifest, require_intents=True)
    server = server_factory(event_hooks={"request": [guard]}, destructive=True)
    mock_gorelo.on("POST", "/v1/invoices", envelope({"Id": uid(70)}))
    text = await call_tool_error(server, "create_invoice", INVOICE_ARGUMENTS)
    assert "no open intent of kind 'invoice'" in text
    assert mock_gorelo.requests == [] and guard.tripped


async def test_create_invoice_for_any_other_client_is_stopped_before_a_request_leaves(server_factory, mock_gorelo, guard):
    server = server_factory(event_hooks={"request": [guard]}, destructive=True)
    mock_gorelo.on("POST", "/v1/invoices", envelope({"Id": uid(70)}))
    text = await call_tool_error(server, "create_invoice", {**INVOICE_ARGUMENTS, "client_id": SECOND})
    assert "blocked POST /invoices: ClientId must be 9501 (the test client), not 9502" in text
    assert mock_gorelo.requests == [] and guard.tripped


async def test_create_approved_invoice_is_stopped_by_the_guard_before_a_request_leaves(server_factory, mock_gorelo, manifest):
    guard = LiveGuard("write", manifest, require_intents=True)
    server = server_factory(event_hooks={"request": [guard]}, destructive=True)
    mock_gorelo.on("POST", "/v1/invoices", envelope({"Id": uid(70)}))
    manifest.intent("invoice", manifest.label("invoice"))
    text = await call_tool_error(server, "create_approved_invoice", {**INVOICE_ARGUMENTS, "confirm": True})
    assert "blocked POST /invoices: StatusId 5 (Approved) is not allowed" in text
    assert "pushed to the connected accounting system" in text
    assert mock_gorelo.requests == [] and guard.tripped
    # with a recipient too: the status is judged first, and nothing is ever emailed
    emailed = {**INVOICE_ARGUMENTS, "recipient_emails": ["ops@example.com"], "confirm": True}
    assert "StatusId 5 (Approved) is not allowed" in await call_tool_error(server, "create_approved_invoice", emailed)
    assert mock_gorelo.requests == []


async def test_delete_invoice_goes_through_the_guard_for_a_run_created_draft_only(server_factory, mock_gorelo, guard):
    server = server_factory(event_hooks={"request": [guard]}, destructive=True)

    def row(ident, number, status=1):
        return {"Id": ident, "Number": number, "DisplayNumber": f"INV-{number}", "Status": {"Id": status, "Name": "x"}}

    mock_gorelo.on("GET", "/v1/invoices", paged_envelope([row(INVOICE, 1042)]), query={"Number": "1042"})
    mock_gorelo.on("GET", "/v1/invoices", paged_envelope([row(FOREIGN, 1500)]), query={"Number": "1500"})
    mock_gorelo.on("GET", "/v1/invoices", paged_envelope([row(INVOICE_APPROVED, 1043, 5)]), query={"Number": "1043"})
    mock_gorelo.on("DELETE", f"/v1/invoices/{INVOICE}", envelope({"Id": INVOICE, "StatusId": 6}))
    mock_gorelo.on("DELETE", f"/v1/invoices/{FOREIGN}", envelope({"Id": FOREIGN, "StatusId": 6}))
    mock_gorelo.on("DELETE", f"/v1/invoices/{INVOICE_APPROVED}", envelope({"Id": INVOICE_APPROVED, "StatusId": 4}))
    done = await call_tool(server, "delete_invoice", {"invoice_number": 1042, "expected_status": "Draft", "confirm": True})
    assert done["StatusId"] == 6 and not guard.tripped
    stranger = await call_tool_error(server, "delete_invoice", {"invoice_number": 1500, "expected_status": "Draft", "confirm": True})
    assert "blocked DELETE /invoices/" in stranger and "is not a test invoice created by this run" in stranger
    # an Approved invoice would be VOIDED by the same call: refused even when the run created it
    voided = await call_tool_error(server, "delete_invoice", {"invoice_number": 1043, "expected_status": "Approved", "confirm": True})
    assert "is not recorded as a Draft (the manifest details status_id is 5, not 1)" in voided
    assert [(r.method, r.path) for r in mock_gorelo.requests if r.method == "DELETE"] == [("DELETE", f"/v1/invoices/{INVOICE}")]
    assert len(guard.violations) == 2


async def test_export_invoice_pdf_goes_through_the_guard_for_a_run_created_invoice_only(server_factory, mock_gorelo, guard, read_guard):
    pdf = httpx.Response(200, content=b"%PDF-1.7 test", headers={"content-type": "application/pdf"})
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}/pdf", pdf)
    mock_gorelo.on("GET", f"/v1/invoices/{FOREIGN}/pdf", pdf)
    server = server_factory(event_hooks={"request": [guard]}, destructive=True)
    result = await call_tool_raw(server, "export_invoice_pdf", {"invoice_id": INVOICE})
    assert not result.is_error and not guard.tripped
    text = await call_tool_error(server, "export_invoice_pdf", {"invoice_id": FOREIGN})
    assert "only an invoice this run created may be exported" in text
    assert [r.path for r in mock_gorelo.requests] == [f"/v1/invoices/{INVOICE}/pdf"]  # the stranger's never left
    read_server = server_factory(event_hooks={"request": [read_guard]}, destructive=True)
    text = await call_tool_error(read_server, "export_invoice_pdf", {"invoice_id": INVOICE})
    assert "read mode never sends it" in text and len(mock_gorelo.requests) == 1


APPROVED_ARGUMENTS = {
    "client_id": TEST_CLIENT,
    "line_items": [{"item_id": INVOICE_ITEM, "quantity": 1, "unit_price": 1.0, "description": APPROVED_LABEL, "no_tax": True}],
    "reference": APPROVED_LABEL,
    "confirm": True,
}


async def test_the_create_approved_invoice_tool_sends_exactly_the_body_the_approved_rules_allow(
    server_factory, mock_gorelo, approved_manifest
):
    guard = LiveGuard("write", approved_manifest, require_intents=True, allow_approved_invoice=True)
    server = server_factory(event_hooks={"request": [guard]}, destructive=True)
    new = uid(70)
    mock_gorelo.on("POST", "/v1/invoices", envelope({"Id": new}))
    mock_gorelo.on("GET", f"/v1/invoices/{new}", envelope(invoice_record(new, Status={"Id": 5, "Name": "Approved"}, Reference=APPROVED_LABEL)))
    result = await call_tool(server, "create_approved_invoice", APPROVED_ARGUMENTS)
    assert result["Id"] == new and not guard.tripped
    post = mock_gorelo.requests[0]
    assert (post.method, post.path) == ("POST", "/v1/invoices")
    assert post.json == APPROVED_BODY  # the body the guard tests above use is the very body the tool builds
    assert not {"RecipientEmails", "InvoiceDate", "DueDate"} & set(post.json)  # no recipient, no dates: Gorelo's defaults
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("POST", "/v1/invoices"), ("GET", f"/v1/invoices/{new}")]
    # a second one is stopped before a request leaves: the guard allows one
    text = await call_tool_error(server, "create_approved_invoice", APPROVED_ARGUMENTS)
    assert "this run already created an Approved invoice" in text and len(mock_gorelo.requests) == 2


@pytest.mark.parametrize(
    "tax",
    [pytest.param({}, id="no-tax-choice"), pytest.param({"tax_id": 5}, id="a-tax-picked"), pytest.param({"no_tax": False}, id="no-tax-false")],
)
async def test_an_approved_invoice_line_that_does_not_ask_for_no_tax_is_stopped_before_a_request_leaves(
    server_factory, mock_gorelo, approved_manifest, tax
):
    guard = LiveGuard("write", approved_manifest, require_intents=True, allow_approved_invoice=True)
    server = server_factory(event_hooks={"request": [guard]}, destructive=True)
    mock_gorelo.on("POST", "/v1/invoices", envelope({"Id": uid(70)}))
    line = {"item_id": INVOICE_ITEM, "quantity": 1, "unit_price": 1.0, "description": APPROVED_LABEL, **tax}
    text = await call_tool_error(server, "create_approved_invoice", {**APPROVED_ARGUMENTS, "line_items": [line]})
    assert "blocked POST /invoices: LineItems[0].TaxId must be given explicitly as null" in text
    assert mock_gorelo.requests == [] and guard.tripped and guard.allowed_count == 0
    assert len(approved_manifest.unresolved_intents()) == 1  # nothing was used up: the matrix's own line still goes through


async def test_a_recipient_makes_the_tool_call_stop_before_a_request_leaves_even_with_the_option(
    server_factory, mock_gorelo, approved_manifest
):
    guard = LiveGuard("write", approved_manifest, require_intents=True, allow_approved_invoice=True)
    server = server_factory(event_hooks={"request": [guard]}, destructive=True)
    mock_gorelo.on("POST", "/v1/invoices", envelope({"Id": uid(70)}))
    for emails in (["ops@example.com"], ["bob@example.com"]):
        text = await call_tool_error(server, "create_approved_invoice", {**APPROVED_ARGUMENTS, "recipient_emails": emails})
        assert "blocked POST /invoices:" in text and "bob@" not in text
    assert mock_gorelo.requests == [] and guard.tripped and guard.allowed_count == 0


async def test_create_approved_invoice_without_the_option_is_still_stopped_when_everything_else_is_right(
    server_factory, mock_gorelo, approved_manifest
):
    guard = LiveGuard("write", approved_manifest, require_intents=True)
    server = server_factory(event_hooks={"request": [guard]}, destructive=True)
    mock_gorelo.on("POST", "/v1/invoices", envelope({"Id": uid(70)}))
    text = await call_tool_error(server, "create_approved_invoice", APPROVED_ARGUMENTS)
    assert "blocked POST /invoices: StatusId 5 (Approved) is not allowed" in text and mock_gorelo.requests == []


async def test_delete_invoice_voids_an_invoice_recorded_as_approved_only_with_the_option(server_factory, mock_gorelo, manifest):
    def row(ident, number, status):
        return {"Id": ident, "Number": number, "DisplayNumber": f"INV-{number}", "Status": {"Id": status, "Name": "x"}}

    mock_gorelo.on("GET", "/v1/invoices", paged_envelope([row(INVOICE_APPROVED, 1043, 5)]), query={"Number": "1043"})
    mock_gorelo.on("DELETE", f"/v1/invoices/{INVOICE_APPROVED}", envelope({"Id": INVOICE_APPROVED, "StatusId": 4}))
    arguments = {"invoice_number": 1043, "expected_status": "Approved", "confirm": True}
    plain = server_factory(event_hooks={"request": [LiveGuard("write", manifest)]}, destructive=True)
    text = await call_tool_error(plain, "delete_invoice", arguments)
    assert "is not recorded as a Draft (the manifest details status_id is 5, not 1)" in text
    assert [r.method for r in mock_gorelo.requests] == ["GET"]  # the lookup went out, the DELETE never did
    guard = LiveGuard("write", manifest, allow_approved_invoice=True)
    voided = await call_tool(server_factory(event_hooks={"request": [guard]}, destructive=True), "delete_invoice", arguments)
    assert voided["StatusId"] == 4 and voided["previous_status"] == "Approved" and not guard.tripped
    assert [(r.method, r.path) for r in mock_gorelo.requests if r.method == "DELETE"] == [("DELETE", f"/v1/invoices/{INVOICE_APPROVED}")]


# --------------------------------------------------------------------------
# require_intents: a create needs an open intent of its kind
# --------------------------------------------------------------------------


def test_require_intents_refuses_a_create_nobody_announced(manifest):
    guard = LiveGuard("write", manifest, require_intents=True)
    request = build("POST", "/tickets", TICKET_BODY)
    refused(guard, request, "no open intent of kind 'ticket': call manifest.intent\\('ticket', label\\)")
    manifest.intent("item", manifest.label("another kind"))
    refused(guard, request, "no open intent of kind 'ticket'")  # an intent of another kind does not count
    manifest.intent("ticket", manifest.label("new ticket"))
    guard.check(request)
    manifest.created("ticket", uid(60), manifest.label("new ticket"), {"contact_id": None})  # settles the intent
    refused(guard, request, "no open intent of kind 'ticket'")


def test_a_refused_create_settles_nothing_and_a_failed_intent_is_no_longer_open(manifest):
    guard = LiveGuard("write", manifest, require_intents=True)
    seq = manifest.intent("contact", manifest.label("c"))
    guard.check(build("POST", "/contacts", CONTACT_BODY))
    manifest.intent_failed(seq, "Gorelo answered 400")
    refused(guard, build("POST", "/contacts", CONTACT_BODY), "no open intent of kind 'contact'")


def test_require_intents_leaves_everything_but_creates_alone(manifest):
    guard = LiveGuard("write", manifest, require_intents=True)
    guard.check(build("GET", "/clients"))
    guard.check(build("PATCH", f"/tickets/{TICKET_NONE}", {"Title": "x"}))
    guard.check(build("DELETE", f"/tickets/{TICKET_NONE}"))
    guard.check(build("POST", "/forms/some-form/submission-links", {"TicketId": TICKET_NONE}))  # a link is not a record


def test_a_guard_without_require_intents_ignores_intents(guard):
    guard.check(build("POST", "/tickets", TICKET_BODY))
    assert LiveGuard("write", guard.manifest).require_intents is False


def test_every_create_operation_is_known_to_require_intents():
    posts = {key for key in WRITE_OPS if key.startswith("POST ")}
    assert posts - set(_CREATES) == {
        "POST /v1/alerts", "POST /v1/api-keys", "POST /v1/forms/{formId}/submission-links", "POST /v1/payments",
    }
    assert set(_CREATES) <= posts
    assert _CREATES["POST /v1/invoices"] == "invoice"


@pytest.mark.parametrize("key, kind", sorted(_CREATES.items()))
def test_require_intents_asks_for_the_right_kind_on_every_create(manifest, key, kind):
    guard = LiveGuard("write", manifest, require_intents=True)
    request = stranger_request(SPEC.op(key))
    with pytest.raises(GuardViolation, match=f"no open intent of kind '{kind}':"):
        guard.check(request)
    manifest.intent(kind, manifest.label("announced"))
    with pytest.raises(GuardViolation) as caught:  # a stranger's request is still refused, for another reason
        guard.check(stranger_request(SPEC.op(key)))
    assert "no open intent" not in str(caught.value)


# --------------------------------------------------------------------------
# the rule table and the spec
# --------------------------------------------------------------------------

SPEC = load_spec_index()
NEVER_OPS = {
    "POST /v1/alerts",
    "POST /v1/api-keys",
    "DELETE /v1/assets/agents/{deviceId}",
    "DELETE /v1/assets/custom/{customAssetId}",
    "DELETE /v1/contracts/{contractId}",
    "POST /v1/payments",
    "DELETE /v1/payments/{paymentId}",
}
# a GET that is not a plain read: write mode lets it through only for a run-created invoice
CONDITIONAL_GETS = {"GET /v1/invoices/{invoiceId}/pdf"}


def stranger_request(op):
    """A request for `op` with ids that belong to no run, the emptiest body, and a stranger's upload for the multipart op."""
    path = "https://api.usw.gorelo.io" + op.path.format(**path_params_for(op))  # op.path already starts with /v1
    if op.is_multipart:
        return httpx.Request(
            op.method, path, files={"file": ("a.txt", b"x", "text/plain")}, data={"itemType": "Ticket", "itemId": FOREIGN}
        )
    if op.body:
        return httpx.Request(op.method, path, json={})
    return httpx.Request(op.method, path)


WRITE_OPS = sorted(op.key for op in SPEC.ops.values() if op.method != "GET")
GET_OPS = sorted(op.key for op in SPEC.ops.values() if op.method == "GET")


@pytest.mark.parametrize("key", WRITE_OPS)
def test_every_write_operation_of_the_spec_is_blocked_for_a_stranger(guard, cleanup_guard, read_guard, key):
    request = stranger_request(SPEC.op(key))
    for subject in (guard, cleanup_guard, read_guard):
        with pytest.raises(GuardViolation):
            subject.check(request)


@pytest.mark.parametrize("key", [k for k in GET_OPS if k not in CONDITIONAL_GETS])
def test_every_get_operation_of_the_spec_is_allowed_in_both_modes(guard, read_guard, key):
    request = stranger_request(SPEC.op(key))
    for subject in (guard, read_guard):
        subject.check(request)


def test_the_rule_table_names_a_rule_for_every_operation_of_the_spec(guard):
    table = guard.rule_table()
    assert set(table) == set(SPEC.ops)
    assert {key for key, text in table.items() if text.startswith("never")} == NEVER_OPS
    # a write operation that is neither allowed with a rule nor never allowed: the spec grew, decide what the harness may do with it
    undecided = sorted(key for key, text in table.items() if text == "blocked: not on the allowlist")
    assert undecided == [], f"classify these new write operations in scripts/live/guard.py: {undecided}"
    assert all(text for text in table.values())
    assert table["POST /v1/clients"].startswith("Name starts with MCPTEST-")
    assert table["GET /v1/clients"] == "allowed"
    assert table["DELETE /v1/clients/{clientId}"] == "run-created client (never the test client or the second client); with cleanup=True also the listed leftover clients"
    assert table["DELETE /v1/contacts/{contactId}"].startswith("run-created contact (never the operator contact); with cleanup=True")
    assert table["PATCH /v1/tickets/{ticketId}"].startswith("path ids run-created: ticketId; ClientId")
    assert table["PATCH /v1/clients/{clientId}"] == (
        "path id is a run-created client (never the test client or the second client); the body has no Id (the spec defines none)"
    )
    assert table["PATCH /v1/contacts/{contactId}"] == (
        "path ids run-created: contactId; the body has no ContactId (the spec defines none); ClientId, if present, is the test client"
    )
    # the collection forms are gone from the published spec: they are not operations of the loaded spec, so no rule
    assert "PATCH /v1/clients" not in table and "PATCH /v1/contacts" not in table
    # an API key is a credential: it is never created
    assert table["POST /v1/api-keys"].startswith("never: the live harness never creates API keys")
    # an invoice is created as a Draft for the test client only, and deleted or exported only when the run created it
    assert table["POST /v1/invoices"] == (
        "ClientId is the test client; StatusId is exactly 1 (a Draft: 5, Approved, pushes the invoice to the accounting system); "
        "RecipientEmails absent, null or empty; LineItems a non-empty list of objects, each ItemId a UUID string"
    )
    assert table["DELETE /v1/invoices/{invoiceId}"].startswith("path ids run-created: invoiceId; its manifest details hold status_id 1")
    assert table["GET /v1/invoices/{invoiceId}/pdf"] == (
        "write mode only, and only a run-created invoice whose manifest details hold status_id 1; never in read "
        "mode: the PDF download records an export event on the invoice"
    )
    assert not {"POST /v1/invoices", "DELETE /v1/invoices/{invoiceId}", "GET /v1/invoices/{invoiceId}/pdf"} & NEVER_OPS


# The rule tables that LiveGuard looks up by the EXACT text of route.key (_check_write and _rule_text). A key spelled
# differently from the spec (an invoice id written {invoiceID} where the spec says {invoiceId}) is simply not found: its
# entry protects nothing, and the tables that do match apply alone (the invoice DELETE would then pass on the run-created
# check only, and an Approved invoice would be voided). So these tables are compared with the spec EXACTLY. Only what the
# guard itself looks up by shape (_NEVER and SIDE_EFFECT_GETS) is compared by shape.
EXACT_KEYED_TABLES = ("_PATH_KINDS", "_HANDLER_NAMES", "_HANDLER_TEXT", "_SPECIAL_DELETES", "_CREATES")


def exact_keyed_tables() -> dict[str, dict]:
    return {name: dict(getattr(guard_module, name)) for name in EXACT_KEYED_TABLES}


def keys_the_spec_does_not_have(tables: dict[str, dict]) -> dict[str, list[str]]:
    """Per table, the keys that are not an operation of the spec spelled exactly like that."""
    stale = {name: sorted(key for key in table if key not in SPEC.ops) for name, table in tables.items()}
    return {name: keys for name, keys in stale.items() if keys}


def test_every_key_of_the_exact_keyed_rule_tables_is_an_operation_of_the_spec_spelled_exactly():
    """A typo, a renamed placeholder or an operation Gorelo dropped must not leave a rule that silently protects nothing."""
    tables = exact_keyed_tables()
    assert all(tables.values())  # none of them is empty
    assert keys_the_spec_does_not_have(tables) == {}
    # every placeholder a path rule reads is a placeholder of that operation's path (else req.params has no such key)
    for key, kinds in guard_module._PATH_KINDS.items():
        placeholders = set(re.findall(r"\{(\w+)\}", SPEC.ops[key].path))
        assert {placeholder for _kind, placeholder in kinds} <= placeholders, key
    # the handlers the @_rule decorators registered really are LiveGuard methods
    assert all(callable(getattr(LiveGuard, name, None)) for name in guard_module._HANDLER_NAMES.values())
    assert set(guard_module._HANDLER_TEXT) == set(guard_module._HANDLER_NAMES)


def test_every_table_of_operation_keys_in_the_guard_is_classified_as_looked_up_by_exact_text_or_by_shape():
    """A table added later must say how it is looked up, or it would escape the comparison with the spec: add it to
    EXACT_KEYED_TABLES (looked up by route.key) or to the by-shape set (looked up through normalize_op_key)."""
    operation = re.compile(r"(?:GET|POST|PATCH|DELETE) /v1/\S+")
    by_shape = {"_NEVER", "_NEVER_BY_SHAPE", "SIDE_EFFECT_GETS", "_SIDE_EFFECT_SHAPES"}
    found = set()
    for name, value in vars(guard_module).items():
        keys = value.keys() if isinstance(value, dict) else value if isinstance(value, (set, frozenset)) else None
        if keys and all(isinstance(key, str) and operation.fullmatch(key) for key in keys):
            found.add(name)
    assert found == set(EXACT_KEYED_TABLES) | by_shape


def test_the_exact_comparison_sees_a_misspelled_placeholder_that_a_comparison_by_shape_cannot():
    misspelled = "DELETE /v1/invoices/{invoiceID}"
    shapes = {normalize_op_key(key) for key in SPEC.ops}
    assert normalize_op_key(misspelled) in shapes  # what the old comparison by shape could not tell apart
    assert misspelled not in SPEC.ops and "DELETE /v1/invoices/{invoiceId}" in SPEC.ops
    tables = exact_keyed_tables()
    assert keys_the_spec_does_not_have(tables) == {}
    tables["_HANDLER_NAMES"][misspelled] = tables["_HANDLER_NAMES"].pop("DELETE /v1/invoices/{invoiceId}")
    assert keys_the_spec_does_not_have(tables) == {"_HANDLER_NAMES": [misspelled]}
    tables = exact_keyed_tables()
    tables["_PATH_KINDS"]["DELETE /v1/invoicez/{invoiceId}"] = (("invoice", "invoiceId"),)  # names nothing at all
    assert keys_the_spec_does_not_have(tables) == {"_PATH_KINDS": ["DELETE /v1/invoicez/{invoiceId}"]}


def test_what_the_guard_looks_up_by_shape_is_compared_with_the_spec_by_shape():
    """_NEVER (_never_reason) and SIDE_EFFECT_GETS (_is_side_effect_get) are matched by shape, so a renamed placeholder
    still finds them; that is how they are compared with the spec."""
    shapes = {normalize_op_key(key) for key in SPEC.ops}
    assert guard_module._NEVER and {normalize_op_key(key) for key in guard_module._NEVER} <= shapes
    assert {normalize_op_key(key) for key in guard_module.SIDE_EFFECT_GETS} <= shapes
    assert guard_module._SIDE_EFFECT_SHAPES == {"GET /v1/invoices/{}/pdf"}
    assert guard_module._never_reason("DELETE /v1/assets/agents/{id}") == guard_module._NEVER["DELETE /v1/assets/agents/{deviceId}"]
    assert guard_module._is_side_effect_get("GET /v1/invoices/{id}/pdf")
    # and a key that names nothing is caught by the same comparison
    assert normalize_op_key("DELETE /v1/invoices/{typo}") in shapes
    assert normalize_op_key("DELETE /v1/invoicez/{id}") not in shapes


def test_no_operation_is_spelled_two_ways_across_the_exact_keyed_tables():
    """A handler under {invoiceID} next to a path rule under {invoiceId} would be two keys: the guard finds the path
    rule, not the handler, and the request passes after the run-created check alone."""
    tables = exact_keyed_tables()
    assert guard_module._spelling_conflicts(*tables.values()) == {}
    guard_module._check_one_spelling_per_operation(*tables.values())  # the check that runs when the module is imported
    renamed = {key.replace("{invoiceId}", "{invoiceID}"): name for key, name in guard_module._HANDLER_NAMES.items()}
    assert "DELETE /v1/invoices/{invoiceID}" in renamed
    conflicts = guard_module._spelling_conflicts(guard_module._PATH_KINDS, renamed)
    assert conflicts == {"DELETE /v1/invoices/{}": ["DELETE /v1/invoices/{invoiceID}", "DELETE /v1/invoices/{invoiceId}"]}
    with pytest.raises(RuntimeError, match="spells an operation two ways") as caught:
        guard_module._check_one_spelling_per_operation(guard_module._PATH_KINDS, renamed)
    assert "DELETE /v1/invoices/{invoiceID} and DELETE /v1/invoices/{invoiceId}" in str(caught.value)
    # two tables that agree, or one table alone, are fine
    guard_module._check_one_spelling_per_operation(guard_module._PATH_KINDS, guard_module._HANDLER_NAMES)
    guard_module._check_one_spelling_per_operation(renamed)


def test_an_operation_in_both_the_path_rules_and_the_handlers_has_one_spelling():
    both = set(guard_module._PATH_KINDS) & set(guard_module._HANDLER_NAMES)
    assert "DELETE /v1/invoices/{invoiceId}" in both and "PATCH /v1/tickets/{ticketId}" in both
    by_shape = {}
    for key in [*guard_module._PATH_KINDS, *guard_module._HANDLER_NAMES]:
        by_shape.setdefault(normalize_op_key(key), set()).add(key)
    assert {shape: keys for shape, keys in by_shape.items() if len(keys) > 1} == {}


def test_the_guard_does_not_promise_that_an_invoice_delete_is_permanent(guard):
    """The 2026-10-02 spec says only that a Draft is Deleted (StatusId 6) and no longer listed, and an Approved invoice
    is voided: nothing about permanence, so no text of the guard may say it."""
    rule = guard.rule_table()["DELETE /v1/invoices/{invoiceId}"]
    source = Path(guard_module.__file__).read_text(encoding="utf-8")
    for where, text in (("the module docstring", guard_module.__doc__), ("the rule text", rule), ("the source", source)):
        assert "permanent" not in text.lower(), where
    # the documented wording of what a DELETE does, in the docstring and in the rule table
    said = "Draft: deleted (no longer listed). Approved: voided (status Void, still listed)"
    assert said in rule and said in " ".join(guard_module.__doc__.split())


def test_the_guard_docstring_says_what_status_the_manifest_holds_for_an_invoice():
    text = " ".join(guard_module.__doc__.split())
    assert (
        "`status_id` is the status Gorelo stored (1 for every Draft create the POST rule allows, and 1 when the answer has "
        "none; for the approved invoice 5, and 5 when the answer has none, never 1), or, for an invoice found by the "
        "cleanup's label search, the status the search found"
    ) in text
    assert "or 4 once the harness voided it" in text
    assert (
        "The PDF rule refuses any invoice whose details do not hold the integer 1, and the DELETE rule any invoice whose "
        "details do not hold the integer 1 (or the integer 5 with allow_approved_invoice)"
    ) in text
    assert "The harness records `status_id` 1 (a Draft" not in text  # the old wording said every record holds 1


def test_the_never_table_holds_exactly_the_operations_the_harness_never_sends():
    assert {normalize_op_key(key) for key in guard_module._NEVER} == {normalize_op_key(key) for key in NEVER_OPS}
    # the invoice operations moved out of it: they have a rule of their own
    for key in ("POST /v1/invoices", "DELETE /v1/invoices/{invoiceId}"):
        assert key not in guard_module._NEVER and key in guard_module._HANDLER_NAMES
    assert guard_module._PATH_KINDS["DELETE /v1/invoices/{invoiceId}"] == (("invoice", "invoiceId"),)


def test_the_constants_of_the_allowlist_are_the_approved_ones(manifest):
    guard = LiveGuard("write", manifest)
    assert guard.allowed_clients == {9501, 9502}
    assert (guard.operator_contact, guard.operator_user, guard.test_client) == (9600, 9700, 9501)
    assert guard.allowed_emails == {"ops@example.com"} and guard.test_email_domain == "example.invalid"
    assert guard.approved_leftovers == {"client": {9801, 9802}, "contact": {9900}}
    assert guard.cleanup is False and LiveGuard("write", manifest, cleanup=True).cleanup is True
    assert guard.allow_approved_invoice is False  # the one Approved invoice of a run is opt-in, never a default


@pytest.mark.parametrize("path", ["/v1/tickets"])
def test_lead_assignee_zero_is_refused_now_that_the_unassign_probe_is_gone(guard, path):
    import httpx
    req = httpx.Request("POST", "https://api.usw.gorelo.io" + path, json={
        "Title": f"{guard.manifest.run_id} t", "ClientId": 9501, "StatusId": 1, "TypeId": 7101, "GroupId": 7201, "LeadAssigneeId": 0})
    with pytest.raises(Exception) as caught:
        guard.check(req)
    assert "LeadAssigneeId" in str(caught.value)


# --------------------------------------------------------------------------
# the site config: no file, no key, no guard
# --------------------------------------------------------------------------


def test_the_guard_comes_from_the_site_config_and_takes_nothing_from_the_code(manifest):
    built = LiveGuard("write", manifest)
    assert built.allowed_clients == {9501, 9502} and built.test_client == 9501
    assert (built.operator_contact, built.operator_user) == (9600, 9700)
    assert built.allowed_emails == {"ops@example.com"} and built.probe_domains == {"example.net"}
    assert built.approved_leftovers == {"client": {9801, 9802}, "contact": {9900}}


def test_a_guard_refuses_to_be_built_without_the_site_config_file(manifest, monkeypatch, tmp_path):
    from scripts.site_config import SiteConfigError

    monkeypatch.setenv("GORELO_SITE_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(SiteConfigError, match=r"file not found.*site\.example\.toml"):
        LiveGuard("write", manifest)
    with pytest.raises(SiteConfigError, match="site.example.toml"):
        LiveGuard("read", None)


@pytest.mark.parametrize("section, key", [("clients", "test_client"), ("operator", "contact_id"), ("hosts", "probe_domain"), ("leftovers", "client_ids")])
def test_a_guard_refuses_to_be_built_when_a_key_is_missing_and_names_it(manifest, monkeypatch, tmp_path, section, key):
    from scripts.site_config import SiteConfigError
    from site_helper import drop_key

    drop_key(monkeypatch, tmp_path, section, key)
    with pytest.raises(SiteConfigError, match=rf"missing key \[{section}\] {key}.*site\.example\.toml"):
        LiveGuard("write", manifest)
