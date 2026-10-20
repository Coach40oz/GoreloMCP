"""The cleanup of the documents a live run made (contract a7954b24844b), run offline against the stateful FakeGorelo.

A document cannot be deleted through the API, so the cleanup never deletes one: it reads the document first and ARCHIVES one a stopped
run left active (the cleanup guard allows exactly the body {"StatusId": 2} for a run-created document), then records the same outcome
as the write matrix does, which makes the document a known residue (neither cleaned nor a leftover). The matrix area itself is tested
in tests/test_live_write_matrix.py, and its attachment check at the end of this file; this file drives scripts/live/cleanup.py on
manifests that a stopped run could have left.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from conftest import envelope, error_envelope
from site_helper import site_config_env  # noqa: F401  (autouse: points GORELO_SITE_CONFIG at invented values)
from test_live_write_matrix import (  # noqa: F401  (fixtures and helpers of the matrix tests)
    RUN,
    TEST_CLIENT,
    FakeGorelo,
    _never_read_the_real_env,
    _the_gate_of_the_approved_invoice_is_open,
    lines,
)

from scripts.live import cleanup, write_matrix
from scripts.live.guard import GuardViolation, LiveGuard
from scripts.live.manifest import Manifest
from scripts.live.smoke import GuardTripped, ToolFailed

pytestmark = pytest.mark.anyio


def stopped_run(tmp_path, fake, *, count=2):
    """A manifest of a run that stopped after it made `count` documents (all Active) and uploaded one file to the first."""
    manifest = Manifest(RUN, tmp_path / "run.json")
    ids = []
    for n in range(count):
        label = manifest.label(f"document {n}")
        made = fake.add_document({"Title": label, "Body": f"<p>{label}</p>"}, parent=None, client=TEST_CLIENT)
        manifest.created("document", made["Id"], label, {"client_id": TEST_CLIENT, "parent_document_id": None})
        ids.append(made["Id"])
    manifest.created("attachment", f"{RUN}-document.txt", manifest.label("document attachment"), {"document_id": ids[0]})
    return manifest, ids


async def clean(manifest, fake, make_settings, lines, **options):
    return await cleanup.run_cleanup(
        manifest, settings=make_settings(destructive=True), transport=fake.mock.transport, pace=0, echo=lines.append,
        verify_clients=False, **options,
    )


async def test_a_document_a_stopped_run_left_active_is_read_then_archived_and_becomes_a_known_residue(tmp_path, make_settings, lines):
    fake = FakeGorelo()
    manifest, (first, second) = stopped_run(tmp_path, fake)
    report = await clean(manifest, fake, make_settings, lines)
    assert [(r.method, r.path) for r in fake.mock.requests] == [
        ("GET", f"/v1/documents/{second}"), ("PATCH", f"/v1/documents/{second}"),  # the manifest is read backwards
        ("GET", f"/v1/documents/{first}"), ("PATCH", f"/v1/documents/{first}"),
    ]
    assert [r.json for r in fake.mock.requests if r.method == "PATCH"] == [{"StatusId": 2}, {"StatusId": 2}]  # nothing but the archive
    assert all(d["StatusId"] == 2 for d in fake.documents.values()) and fake.leftovers() == []
    expected = cleanup.document_residue("archived by the cleanup (archive_document)")
    for ident in (first, second):
        record = manifest.record("document", ident)
        assert record.outcome == expected and not record.cleaned and cleanup.is_known_undeletable(record)
    assert report.ok and report.cleaned == [] and report.leftovers == [] and report.unresolved == []
    assert sorted(r.id for r in report.undeletable if r.kind == "document") == sorted([first, second])
    (attachment,) = [r for r in report.undeletable if r.kind == "attachment"]
    assert attachment.outcome == f"archived with its document; {cleanup.UNDELETABLE_REASON}"
    assert lines[-1] == "result: cleanup complete; 3 known undeletable records remain (see above)"


async def test_a_second_cleanup_finds_the_same_outcomes_and_sends_nothing(tmp_path, make_settings, lines):
    fake = FakeGorelo()
    manifest, ids = stopped_run(tmp_path, fake)
    await clean(manifest, fake, make_settings, lines)
    before = len(fake.mock.requests)
    attempts = {i: len(manifest.record("document", i).attempts) for i in ids}
    report = await clean(manifest, fake, make_settings, lines)
    assert len(fake.mock.requests) == before and report.ok
    assert {i: len(manifest.record("document", i).attempts) for i in ids} == attempts


async def test_a_document_that_is_already_archived_is_only_recorded_with_one_read(tmp_path, make_settings, lines):
    fake = FakeGorelo()
    manifest, (first, second) = stopped_run(tmp_path, fake)
    fake.documents[first]["StatusId"] = 2  # somebody archived it
    report = await clean(manifest, fake, make_settings, lines)
    assert [(r.method, r.path) for r in fake.mock.requests if first in r.path] == [("GET", f"/v1/documents/{first}")]
    assert manifest.record("document", first).outcome == cleanup.document_residue("already archived")
    assert manifest.record("document", second).outcome == cleanup.document_residue("archived by the cleanup (archive_document)")
    assert report.ok


async def test_a_document_gorelo_no_longer_finds_is_cleaned(tmp_path, make_settings, lines):
    fake = FakeGorelo()
    manifest, (first, second) = stopped_run(tmp_path, fake)
    del fake.documents[second]  # somebody removed it in the app
    report = await clean(manifest, fake, make_settings, lines)
    record = manifest.record("document", second)
    assert record.cleaned and record.outcome == "already gone (HTTP 404)" and report.ok
    assert [r.kind for r in report.cleaned] == ["document"]
    assert [r.method for r in fake.mock.requests if second in r.path] == ["GET"]  # nothing is archived that is not there


async def test_a_document_that_cannot_be_read_is_a_leftover_that_says_it_was_not_archived(tmp_path, make_settings, lines):
    fake = FakeGorelo()
    manifest, ids = stopped_run(tmp_path, fake, count=1)
    (first,) = ids
    fake.fail("GET /v1/documents/{documentId}", error_envelope(500, [("070500", "boom")]))
    report = await clean(manifest, fake, make_settings, lines)
    record = manifest.record("document", first)
    assert not report.ok and [r.kind for r in report.leftovers] == ["document", "attachment"]  # the file is a plain leftover too
    assert not cleanup.is_known_undeletable(record)
    assert "left over: document" in record.outcome and "could not be read first" in record.outcome and "it may still be Active" in record.outcome
    assert not any(r.method == "PATCH" for r in fake.mock.requests) and fake.documents[first]["StatusId"] == 1
    # the file of a document that was not archived is not a known residue yet: its parent is not gone
    assert cleanup.UNDELETABLE_REASON not in (manifest.record("attachment", f"{RUN}-document.txt").outcome or "")
    assert [r.kind for r in report.undeletable] == []


@pytest.mark.parametrize(
    "body",
    [{"Id": "x"}, {"Id": "x", "Status": None}, {"Id": "x", "Status": {"Name": "Active"}}, {"Id": "x", "Status": {"Id": "1"}}, {"Id": "x", "Status": {"Id": True}}],
)
async def test_a_record_without_a_readable_status_is_not_archived_blindly(tmp_path, make_settings, lines, body):
    fake = FakeGorelo()
    manifest, ids = stopped_run(tmp_path, fake, count=2)
    fake.handlers["GET /v1/documents/{documentId}"] = lambda q: envelope(body)
    report = await clean(manifest, fake, make_settings, lines)
    assert not report.ok and not any(r.method == "PATCH" for r in fake.mock.requests)
    for ident in ids:
        assert "the record has no readable Status" in manifest.record("document", ident).outcome


async def test_an_archive_gorelo_does_not_confirm_is_a_leftover_with_gorelos_words(tmp_path, make_settings, lines):
    fake = FakeGorelo()
    manifest, ids = stopped_run(tmp_path, fake, count=1)
    fake.fail("PATCH /v1/documents/{documentId}", error_envelope(500, [("070500", "boom")]))
    report = await clean(manifest, fake, make_settings, lines)
    outcome = manifest.record("document", ids[0]).outcome
    assert not report.ok and "is still Active, archive_document did not confirm" in outcome and "boom" in outcome
    assert [r.method for r in fake.mock.requests] == ["GET", "PATCH"]  # asked once, not repeated


async def test_the_cleanup_guard_allows_nothing_but_reads_and_the_archive_of_a_run_document(tmp_path, make_settings, lines):
    fake = FakeGorelo()
    foreign = fake.add_document({"Title": "A customer's runbook", "Body": "x"}, parent=None, client=TEST_CLIENT)["Id"]
    manifest, ids = stopped_run(tmp_path, fake)
    gone = "00000077-dddd-4eee-8fff-000000000077"
    manifest.created("document", gone, manifest.label("never made"), {})  # a record whose document is not there any more
    report = await clean(manifest, fake, make_settings, lines)
    assert fake.documents[foreign]["StatusId"] == 1 and fake.documents[foreign]["Title"] == "A customer's runbook"  # never touched
    assert {r.method for r in fake.mock.requests} == {"GET", "PATCH"} and not any(foreign in r.path for r in fake.mock.requests)
    assert manifest.record("document", gone).cleaned and report.ok


def test_the_residue_helpers_agree_with_what_the_cleanup_reads():
    text = cleanup.document_residue("archived by the write matrix (archive_document)")
    assert text == f"archived by the write matrix (archive_document), stays in Gorelo {cleanup.DOCUMENT_RESIDUE_TAIL}"
    assert text.endswith("until removed in the Gorelo app (a document cannot be deleted through the API)")
    assert cleanup.is_known_undeletable(type("R", (), {"kind": "document", "outcome": text})())
    for outcome in (None, "", "left over: document x is still Active", "archived by the cleanup"):
        assert not cleanup.is_known_undeletable(type("R", (), {"kind": "document", "outcome": outcome})())
    assert not cleanup.is_known_undeletable(type("R", (), {"kind": "ticket", "outcome": text})())  # only a document's outcome counts
    assert cleanup.DOCUMENT_ARCHIVED == 2


def test_the_cleanup_docstring_says_what_it_does_to_a_document():
    text = " ".join(cleanup.__doc__.split())
    for said in (
        "A document (kind document, made by write_matrix --with-documents) cannot be deleted through the API either",
        "reads it first (get_document) and archives it with the archive_document tool",
        "the cleanup guard allows exactly the body {\"StatusId\": 2} for a run-created document and nothing else",
        "It never restores, moves or shares anything.",
    ):
        assert said in text, said


# --------------------------------------------------------------------------
# The documents area's attachment check (write_matrix.refuse_dropping_the_attachment): update_document with a list that leaves the
# current file out must be refused LOCALLY after one GET. A scripted session sits in front of the real guard, as in
# tests/test_live_custom_assets.py.
# --------------------------------------------------------------------------

DOCUMENT = "00000002-aaaa-4bbb-8ccc-000000000002"
OTHER_FILE = f"{RUN}-other.txt"  # a file this run uploaded to the document as well: the guard lets a PATCH that names it through
REMOVAL = (  # the refusal that drops a current file (tools/documents.py), without any file name
    "attachments: Gorelo replaces the whole attachment list, so the current attachment(s) would be removed. Nothing was changed."
)
NOT_REFUSED = (
    "update_document with a list that leaves the current file out was not refused locally after one GET and before any PATCH, "
    "so the negative check did not pass"
)


class RefusalSession:
    """What update_document answers to a list that leaves the current file out, whatever mode says: "local" is the real tool (one GET,
    then the refusal that names the removal); the others are what the check must catch. Every request goes through the real guard."""

    def __init__(self, guard, pacer, *, mode):
        self.guard, self.pacer, self.mode = guard, pacer, mode

    def _send(self, method, path, body=None):
        request = httpx.Request(method, "https://api.usw.gorelo.io/v1" + path, json=body)
        try:
            self.guard.check(request)
        except GuardViolation as exc:
            raise GuardTripped(str(exc)) from None
        self.pacer.requests += 1

    async def call(self, name, arguments=None):
        arguments = dict(arguments or {})
        if name != "update_document":
            raise AssertionError(f"unexpected tool {name}")
        if self.mode == "no-read":  # refused before any request
            raise ToolFailed(name, REMOVAL, sent=False)
        self._send("GET", f"/documents/{DOCUMENT}")
        if self.mode == "other-text":
            raise ToolFailed(name, "name: not blank", sent=True)
        if self.mode == "http":
            raise ToolFailed(name, "Gorelo failed update_document (HTTP 500, code 1)", sent=True)
        if self.mode == "two-requests":
            self._send("GET", f"/documents/{DOCUMENT}")
            raise ToolFailed(name, REMOVAL, sent=True)
        if self.mode == "local":
            raise ToolFailed(name, REMOVAL, sent=True)
        body = {"Attachments": [{"Name": item["name"], "Url": item["url"]} for item in arguments["attachments"]]}
        self._send("PATCH", f"/documents/{DOCUMENT}", body)  # "writes" and "accepts": the guard let this write through
        if self.mode == "writes":
            raise ToolFailed(name, "Gorelo did not confirm update_document: the write may have applied", sent=True)
        return {"Id": DOCUMENT}  # "accepts": Gorelo answered the write and the tool returned it


def refusal_matrix(tmp_path, mode):
    manifest = Manifest(RUN, tmp_path / "run.json")
    manifest.created("document", DOCUMENT, manifest.label("document"), {"client_id": TEST_CLIENT, "parent_document_id": None})
    manifest.created("attachment", f"{RUN}-document.txt", manifest.label("document attachment"), {"document_id": DOCUMENT})
    manifest.created("attachment", OTHER_FILE, manifest.label("other document attachment"), {"document_id": DOCUMENT})
    pacer = SimpleNamespace(requests=0)
    guard = LiveGuard("write", manifest, require_intents=True, allow_documents=True)
    session = RefusalSession(guard, pacer, mode=mode)
    return write_matrix.Matrix(
        manifest=manifest, guard=guard, pacer=pacer, session=session, secret="sekrit-key-1",
        clock=lambda: datetime(2099, 10, 2, 10, 15, 0, tzinfo=timezone.utc), lookup_wait=0, scopes_missing=set(), with_documents=True,
    )


@pytest.mark.parametrize(
    "mode, said",
    [
        ("no-read", f"{NOT_REFUSED} (0 request(s), no HTTP answer)"),
        ("other-text", f"{NOT_REFUSED} (1 request(s), no HTTP answer)"),
        ("http", f"{NOT_REFUSED} (1 request(s), HTTP 500)"),
        ("two-requests", f"{NOT_REFUSED} (2 request(s), no HTTP answer)"),
        (
            "writes",
            f"update_document with a list that leaves the current file out let 1 write request(s) through to Gorelo; look at document "
            f"{DOCUMENT} in the Gorelo app: its attachments may have been replaced",
        ),
        (
            "accepts",
            "update_document ACCEPTED a list that leaves the current file out (a PATCH reached Gorelo): look at document "
            f"{DOCUMENT} in the Gorelo app, its attachments may have been replaced",
        ),
    ],
)
async def test_the_attachment_check_fails_with_its_exact_text_unless_the_list_is_refused_locally_after_one_get(tmp_path, mode, said):
    matrix = refusal_matrix(tmp_path, mode)
    with pytest.raises(write_matrix.CheckFailed) as failed:
        await write_matrix.refuse_dropping_the_attachment(matrix, DOCUMENT, OTHER_FILE)
    assert str(failed.value) == said


async def test_a_refusal_after_one_get_passes_the_attachment_check_and_sends_nothing_but_that_get(tmp_path):
    matrix = refusal_matrix(tmp_path, "local")
    await write_matrix.refuse_dropping_the_attachment(matrix, DOCUMENT, OTHER_FILE)
    assert matrix.pacer.requests == 1 and matrix.guard.writes_allowed == 0
    assert any("refused locally after its read of the document, nothing was sent but that GET" in note for note in matrix.notes)
