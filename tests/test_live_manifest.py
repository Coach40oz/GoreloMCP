"""scripts/live: the run manifest, the env loader and pacer, and the cleanup command.

Offline: temporary files, httpx.MockTransport (through conftest.MockGorelo) and an in-process fastmcp client.
Nothing here reads the app's .env: every test passes its own temporary file or settings.
"""

import json
import os
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from conftest import TEST_API_KEY, call_tool_error, envelope, error_envelope, in_order, paged_envelope, uid
from site_helper import SITE_TOML, site_config_env  # noqa: F401  (autouse: points GORELO_SITE_CONFIG at invented values)

from scripts.live import _env, cleanup
from scripts.live import manifest as manifest_module
from scripts.live.guard import GuardViolation, LiveGuard
from scripts.live.manifest import ID_TYPES, KINDS, Manifest, Record, normalize_id
from settings import TOOLSETS

pytestmark = pytest.mark.anyio

RUN = "MCPTEST-20991002101500"
APP_ENV_FILE = _env.ENV_FILE  # the real constant, read before any fixture replaces it


@pytest.fixture(autouse=True)
def _never_read_the_real_env(monkeypatch, tmp_path):
    """Whatever a test does, the live service's .env is not the file that gets opened."""
    monkeypatch.setattr(_env, "ENV_FILE", tmp_path / "no-such-dir" / ".env")


@pytest.fixture
def manifest(tmp_path: Path) -> Manifest:
    return Manifest(RUN, tmp_path / "runs" / f"{RUN}.json")


def read_file(manifest: Manifest) -> dict:
    return json.loads(manifest.path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# run ids and labels
# --------------------------------------------------------------------------


def test_the_run_id_is_mcptest_and_the_utc_time():
    assert Manifest.new_run_id(datetime(2099, 10, 2, 10, 15, 0, tzinfo=timezone.utc)) == RUN
    # a naive time counts as UTC, another zone is converted
    assert Manifest.new_run_id(datetime(2099, 10, 2, 10, 15, 0)) == RUN
    assert Manifest.new_run_id(datetime(2099, 10, 2, 12, 15, 0, tzinfo=timezone(timedelta(hours=2)))) == RUN
    assert Manifest.new_run_id().startswith("MCPTEST-") and len(Manifest.new_run_id()) == len(RUN)


@pytest.mark.parametrize(
    "bad",
    ["", "MCPTEST-1", "mcptest-20991002101500", "MCPTEST-2099100210150a", "MCPTEST-209910021015000", "run", None, 5],
)
def test_a_malformed_run_id_is_refused(tmp_path, bad):
    with pytest.raises(ValueError, match="MCPTEST-YYYYMMDDHHMMSS"):
        Manifest(bad, tmp_path / "x.json")
    assert list(tmp_path.iterdir()) == []


def test_creating_a_manifest_writes_the_file_at_once(manifest):
    assert manifest.path.is_file()
    state = read_file(manifest)
    assert state["run_id"] == RUN and state["records"] == [] and state["intents"] == []
    assert manifest.all_created() == [] and manifest.leftovers() == [] and manifest.unresolved_intents() == []


def test_the_default_location_is_dot_live_runs_in_the_repo():
    assert manifest_module.RUNS_DIR == Path(__file__).resolve().parent.parent / ".live-runs"
    assert manifest_module.RUNS_DIR.name == ".live-runs"


def test_start_makes_a_fresh_run_and_refuses_to_reuse_a_file(tmp_path):
    moment = datetime(2099, 10, 2, 10, 15, 0, tzinfo=timezone.utc)
    first = Manifest.start(moment, directory=tmp_path)
    assert first.run_id == RUN and first.path == tmp_path / f"{RUN}.json" and first.path.is_file()
    with pytest.raises(FileExistsError):
        Manifest.start(moment, directory=tmp_path)


def test_a_label_starts_with_the_run_id(manifest):
    assert manifest.label() == RUN
    assert manifest.label("ticket one") == f"{RUN} ticket one"
    with pytest.raises(ValueError):
        manifest.label("bad\ntext")


@pytest.mark.parametrize("label", ["ticket", "MCPTEST-20991002101501 other run", f" {RUN}", "", None, 5])
def test_every_label_must_start_with_the_run_id(manifest, label):
    with pytest.raises(ValueError, match="must start with the run id"):
        manifest.intent("ticket", label)
    with pytest.raises(ValueError, match="must start with the run id"):
        manifest.created("ticket", uid(1), label)
    assert manifest.all_created() == [] and manifest.unresolved_intents() == []
    assert read_file(manifest)["intents"] == [] and read_file(manifest)["records"] == []


def test_a_label_with_a_control_character_is_refused(manifest):
    with pytest.raises(ValueError, match="control characters"):
        manifest.created("ticket", uid(1), f"{RUN}\nsecond line")


# --------------------------------------------------------------------------
# kinds and ids
# --------------------------------------------------------------------------

SAMPLE_IDS = {
    "client": 8200,
    "contact": 8301,
    "time_entry": 777,
    "ticket": uid(1),
    "comment": uid(2),
    "item": uid(3),
    "uptime": uid(4),
    "project": uid(5),
    "section": uid(6),
    "task": uid(7),
    "project_comment": uid(8),
    "attachment": "report.txt",
    "side_conversation": uid(9),
    "approval": "approval-1",
    "invoice": uid(11),
}


def test_the_kinds_are_the_fifteen_the_harness_creates():
    assert KINDS == (
        "client", "contact", "ticket", "comment", "time_entry", "item", "uptime", "project", "section", "task",
        "project_comment", "attachment", "side_conversation", "approval", "invoice",
    )
    assert set(ID_TYPES) == set(KINDS) and set(SAMPLE_IDS) == set(KINDS)
    assert ID_TYPES["invoice"] == "uuid"  # an invoice is named by a GUID, never by a number


def test_an_invoice_is_recorded_with_its_status_number_and_display_number(manifest):
    details = {"status_id": 1, "number": 1042, "display_number": "INV-1042"}
    manifest.intent("invoice", manifest.label("invoice"), {"client_id": 9501, "status_id": 1})
    record = manifest.created("invoice", uid(11).upper(), manifest.label("invoice"), details)
    assert record.id == uid(11) and record.details == details and manifest.details("invoice", uid(11)) == details
    assert manifest.unresolved_intents() == [] and manifest.ids("invoice") == {uid(11)}
    # an invoice that read back without a number records null, and the details survive a reload
    manifest.created("invoice", uid(12), manifest.label("numberless"), {"status_id": 1, "number": None, "display_number": None})
    again = Manifest.load(manifest.path)
    assert again.details("invoice", uid(12)) == {"status_id": 1, "number": None, "display_number": None}
    assert again.details("invoice", uid(11)) == details
    with pytest.raises(ValueError, match="a invoice id must be a UUID"):
        normalize_id("invoice", 1042)  # the invoice NUMBER is not an id
    with pytest.raises(ValueError, match="a invoice id must be a UUID"):
        normalize_id("invoice", "INV-1042")


def test_the_manifest_docstring_says_what_status_an_invoice_record_holds_and_what_a_void_leaves():
    text = " ".join(manifest_module.__doc__.split())
    for said in (
        "status_id (the status Gorelo STORED; REQUIRED for any DELETE of an invoice: 1 for a Draft, which the guard lets be "
        "deleted, and 5 for the one Approved invoice of the approved_invoice area, which the guard lets be voided only with "
        "allow_approved_invoice; 4 once it was voided, 3 when Gorelo recorded it as Paid",
        "when the answer has none the fallback is 1 for a Draft and 5 for the Approved invoice, never 1 for the latter",
        "voids an Approved invoice only after reading it back as Approved and only with --void-approved",
        "A voided invoice cannot be removed: it stays listed as Void, its outcome ends with \"(still listed as Void)\", and "
        "cleanup counts it as a known residue (neither cleaned nor a leftover), like an uploaded file",
    ):
        assert said in text, said
    assert "The harness only ever creates Drafts" not in text  # the old contract


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_can_be_recorded(manifest, kind):
    manifest.intent(kind, manifest.label(kind))
    record = manifest.created(kind, SAMPLE_IDS[kind], manifest.label(kind), {"note": kind})
    assert (record.kind, record.id, record.label, record.cleaned) == (kind, normalize_id(kind, SAMPLE_IDS[kind]), manifest.label(kind), False)
    assert manifest.ids(kind) == {record.id}
    assert manifest.unresolved_intents() == []


def test_an_unknown_kind_is_refused(manifest):
    with pytest.raises(ValueError, match="unknown record kind"):
        normalize_id("asset", 1)
    for call in (
        lambda: manifest.intent("asset", manifest.label()),
        lambda: manifest.created("asset", 1, manifest.label()),
        lambda: manifest.ids("asset"),
        lambda: manifest.cleaned("asset", 1, "x"),
    ):
        with pytest.raises(ValueError, match="unknown record kind"):
            call()


@pytest.mark.parametrize(
    "kind, given, expected",
    [
        ("client", 9501, 9501),
        ("client", "9501", 9501),
        ("client", " 0009501 ", 9501),
        ("ticket", uid(1).upper(), uid(1)),
        ("ticket", f" {uid(1)} ", uid(1)),
        ("time_entry", "42", 42),
        ("attachment", "report.txt", "report.txt"),
        ("attachment", 12, 12),
        ("approval", uid(3).upper(), uid(3)),
    ],
)
def test_ids_are_normalized_so_that_a_path_a_body_and_the_manifest_agree(kind, given, expected):
    assert normalize_id(kind, given) == expected


@pytest.mark.parametrize(
    "kind, bad",
    [
        ("client", True),
        ("client", False),
        ("client", 0),
        ("client", -5),
        ("client", 1.0),
        ("client", None),
        ("client", uid(1)),
        ("client", "abc"),
        ("client", ""),
        ("client", "12 3"),
        ("client", "\u0667\u0666\u0660\u0663"),  # Arabic-Indic digits are not an id
        ("ticket", 5),
        ("ticket", "abc"),
        ("ticket", uid(1).replace("-", "")),
        ("time_entry", uid(1)),
        ("attachment", True),
        ("attachment", ""),
        ("attachment", "x" * 301),
        ("attachment", "line\nbreak"),
    ],
)
def test_a_malformed_id_is_refused_and_never_guessed(kind, bad):
    with pytest.raises(ValueError, match=f"a {kind} id must be"):
        normalize_id(kind, bad)


def test_a_uuid_object_is_an_id_too(manifest):
    assert normalize_id("ticket", uuid.UUID(uid(1))) == uid(1)
    assert normalize_id("approval", uuid.UUID(uid(2))) == uid(2)
    with pytest.raises(ValueError):
        normalize_id("client", uuid.UUID(uid(1)))
    assert manifest.created("ticket", uuid.UUID(uid(3)), manifest.label("t")).id == uid(3)


def test_a_record_and_an_intent_can_be_read_by_name_too(manifest):
    manifest.intent("ticket", manifest.label("t"))
    manifest.created("contact", 5, manifest.label("c"), {"a": 1})
    record = manifest.all_created()[0]
    assert (record["kind"], record["id"], record["label"], record["details"], record["cleaned"]) == (
        "contact", 5, manifest.label("c"), {"a": 1}, False
    )
    intent = manifest.unresolved_intents()[0]
    assert (intent["kind"], intent["label"], intent["status"]) == ("ticket", manifest.label("t"), "open")
    for item in (record, intent):
        with pytest.raises(KeyError):
            item["nope"]


def test_the_repr_names_the_run_and_the_file_but_no_details(manifest):
    manifest.created("ticket", uid(1), manifest.label("t"), {"contact_id": 9600})
    text = repr(manifest)
    assert text.startswith("Manifest('MCPTEST-20991002101500', records=1, path=") and "9600" not in text


def test_creating_the_same_record_twice_is_refused(manifest):
    manifest.created("ticket", uid(1), manifest.label("one"))
    with pytest.raises(ValueError, match="already recorded"):
        manifest.created("ticket", uid(1).upper(), manifest.label("two"))
    assert len(manifest.all_created()) == 1


# --------------------------------------------------------------------------
# recording, reading, persistence
# --------------------------------------------------------------------------


def test_records_come_back_in_creation_order(manifest):
    order = [("client", 8200), ("contact", 8301), ("ticket", uid(1)), ("time_entry", 777), ("item", uid(3))]
    for kind, ident in order:
        manifest.created(kind, ident, manifest.label(kind))
    assert [(r.kind, r.id) for r in manifest.all_created()] == order
    assert [r.seq for r in manifest.all_created()] == sorted(r.seq for r in manifest.all_created())


def test_ids_by_kind_include_cleaned_records(manifest):
    manifest.created("ticket", uid(1), manifest.label("a"))
    manifest.created("ticket", uid(2), manifest.label("b"))
    manifest.created("contact", 5, manifest.label("c"))
    manifest.cleaned("ticket", uid(1), "Deleted")
    assert manifest.ids("ticket") == {uid(1), uid(2)}
    assert manifest.ids("contact") == {5}
    assert manifest.ids("client") == set()
    assert isinstance(manifest.ids("ticket"), set)
    manifest.ids("ticket").clear()  # a copy: the manifest does not change
    assert manifest.ids("ticket") == {uid(1), uid(2)}


def test_cleaned_marks_the_record_and_leftovers_shrink(manifest):
    manifest.created("ticket", uid(1), manifest.label("a"))
    manifest.created("contact", 5, manifest.label("b"))
    assert [r.id for r in manifest.leftovers()] == [uid(1), 5]
    manifest.cleaned("ticket", uid(1), "Deleted")
    assert [r.id for r in manifest.leftovers()] == [5]
    record = manifest.record("ticket", uid(1))
    assert record.cleaned and record.outcome == "Deleted"
    assert [a["outcome"] for a in record.attempts] == ["Deleted"] and record.attempts[0]["ok"] is True
    assert manifest.summary() == {"created": 2, "cleaned": 1, "leftovers": 1, "unresolved_intents": 0}


def test_a_failed_cleanup_attempt_keeps_the_record_as_a_leftover(manifest):
    manifest.created("contact", 5, manifest.label("b"))
    manifest.cleanup_failed("contact", 5, "HTTP 409: has open tickets")
    record = manifest.record("contact", 5)
    assert not record.cleaned and record.outcome == "HTTP 409: has open tickets"
    assert [r.id for r in manifest.leftovers()] == [5]
    manifest.cleaned("contact", 5, "Deleted")
    assert [r.id for r in manifest.leftovers()] == []
    assert [a["ok"] for a in manifest.record("contact", 5).attempts] == [False, True]


def test_cleaning_a_record_the_run_did_not_create_is_an_error(manifest):
    with pytest.raises(KeyError, match="did not create"):
        manifest.cleaned("ticket", uid(1), "Deleted")
    with pytest.raises(KeyError, match="did not create"):
        manifest.details("client", 9501)


@pytest.mark.parametrize("outcome", ["", "   ", None, 5])
def test_an_outcome_must_be_text(manifest, outcome):
    manifest.created("ticket", uid(1), manifest.label("a"))
    with pytest.raises(ValueError, match="outcome"):
        manifest.cleaned("ticket", uid(1), outcome)


def test_a_long_outcome_is_shortened_and_whitespace_collapsed(manifest):
    manifest.created("ticket", uid(1), manifest.label("a"))
    manifest.cleanup_failed("ticket", uid(1), "a\n\n  b " + "x" * 800)
    outcome = manifest.record("ticket", uid(1)).outcome
    assert outcome.startswith("a b xxx") and outcome.endswith("...") and len(outcome) == 500


def test_the_manifest_survives_a_reload_exactly(manifest):
    manifest.intent("ticket", manifest.label("t"), {"contact_id": None})
    manifest.created("ticket", uid(1), manifest.label("t"), {"contact_id": None, "cc_contact_ids": [1, 2]})
    manifest.created("comment", uid(2), manifest.label("c"), {"ticket_id": uid(1), "private": True})
    manifest.cleaned("comment", uid(2), "Deleted")
    manifest.intent("item", manifest.label("never answered"))
    manifest.leftover_outcome("client", 9801, "Deleted", deleted=True)
    reloaded = Manifest.load(manifest.path)
    assert reloaded.run_id == RUN and reloaded.path == manifest.path
    assert reloaded.all_created() == manifest.all_created()
    assert reloaded.leftovers() == manifest.leftovers()
    assert reloaded.unresolved_intents() == manifest.unresolved_intents()
    assert reloaded.approved_leftover_outcomes() == manifest.approved_leftover_outcomes()
    assert reloaded.details("ticket", uid(1)) == {"contact_id": None, "cc_contact_ids": [1, 2]}
    assert Manifest(RUN, manifest.path).ids("comment") == {uid(2)}
    # and the reloaded one keeps working
    reloaded.created("contact", 9, reloaded.label("later"))
    assert Manifest.load(manifest.path).ids("contact") == {9}


def test_every_change_is_on_disk_before_the_call_returns(manifest):
    manifest.intent("ticket", manifest.label("t"))
    assert [i["status"] for i in read_file(manifest)["intents"]] == ["open"]
    manifest.created("ticket", uid(1), manifest.label("t"))
    assert [r["id"] for r in read_file(manifest)["records"]] == [uid(1)]
    assert [i["status"] for i in read_file(manifest)["intents"]] == ["created"]
    manifest.update_details("ticket", uid(1), contact_id=9600)
    assert read_file(manifest)["records"][0]["details"] == {"contact_id": 9600}
    manifest.cleanup_failed("ticket", uid(1), "HTTP 409")
    assert read_file(manifest)["records"][0]["outcome"] == "HTTP 409"
    manifest.cleaned("ticket", uid(1), "Deleted")
    assert read_file(manifest)["records"][0]["cleaned"] is True


def test_the_file_is_private_to_its_owner(manifest):
    manifest.created("ticket", uid(1), manifest.label("t"))
    assert stat.S_IMODE(os.stat(manifest.path).st_mode) == 0o600


# --------------------------------------------------------------------------
# intents
# --------------------------------------------------------------------------


def test_an_intent_without_a_record_is_reported_as_unresolved(manifest):
    seq = manifest.intent("ticket", manifest.label("maybe created"), {"client_id": 9501})
    unresolved = manifest.unresolved_intents()
    assert [(i.seq, i.kind, i.label, i.status) for i in unresolved] == [(seq, "ticket", manifest.label("maybe created"), "open")]
    assert unresolved[0].details == {"client_id": 9501}
    assert manifest.summary()["unresolved_intents"] == 1


def test_created_settles_the_oldest_open_intent_with_the_same_kind_and_label(manifest):
    label = manifest.label("twin")
    first = manifest.intent("ticket", label)
    second = manifest.intent("ticket", label)
    manifest.intent("item", label)
    manifest.created("ticket", uid(1), label)
    assert [i.seq for i in manifest.unresolved_intents() if i.kind == "ticket"] == [second]
    manifest.created("ticket", uid(2), label)
    assert [i.seq for i in manifest.unresolved_intents()] == [i.seq for i in manifest.unresolved_intents() if i.kind == "item"]
    assert first < second


def test_a_refused_create_is_not_an_orphan(manifest):
    seq = manifest.intent("contact", manifest.label("refused"))
    manifest.intent_failed(seq, "Gorelo answered 400: bad phone")
    assert manifest.unresolved_intents() == []
    assert read_file(manifest)["intents"][0]["status"] == "failed"
    assert "bad phone" in read_file(manifest)["intents"][0]["reason"]


def test_a_failed_intent_cannot_be_declared_after_it_produced_a_record(manifest):
    seq = manifest.intent("contact", manifest.label("c"))
    manifest.created("contact", 5, manifest.label("c"))
    with pytest.raises(ValueError, match="already produced a record"):
        manifest.intent_failed(seq, "late")
    with pytest.raises(KeyError, match="no intent number"):
        manifest.intent_failed(999, "x")


# --------------------------------------------------------------------------
# details
# --------------------------------------------------------------------------


def test_details_are_copied_in_and_out(manifest):
    given = {"contact_id": 9600, "cc": [1, 2], "nested": {"a": 1}}
    manifest.created("ticket", uid(1), manifest.label("t"), given)
    given["contact_id"] = 5
    given["cc"].append(3)
    got = manifest.details("ticket", uid(1))
    assert got == {"contact_id": 9600, "cc": [1, 2], "nested": {"a": 1}}
    got["nested"]["a"] = 99
    got["cc"].clear()
    assert manifest.details("ticket", uid(1)) == {"contact_id": 9600, "cc": [1, 2], "nested": {"a": 1}}
    snapshot = manifest.record("ticket", uid(1))
    snapshot.details["contact_id"] = 7
    assert manifest.details("ticket", uid(1))["contact_id"] == 9600


def test_update_details_merges_top_level_keys(manifest):
    manifest.created("ticket", uid(1), manifest.label("t"), {"contact_id": None, "client_id": 9501})
    manifest.update_details("ticket", uid(1), contact_id=9600, extra="x")
    assert manifest.details("ticket", uid(1)) == {"contact_id": 9600, "client_id": 9501, "extra": "x"}
    with pytest.raises(ValueError, match="secret"):
        manifest.update_details("ticket", uid(1), token="x")


@pytest.mark.parametrize("details", [{"a": object()}, {"a": float("nan")}, {"a": {1, 2}}, {"a": b"bytes"}])
def test_details_must_be_json(manifest, details):
    with pytest.raises(ValueError, match="JSON"):
        manifest.created("ticket", uid(1), manifest.label("t"), details)
    assert manifest.all_created() == []


def test_details_must_be_a_mapping(manifest):
    with pytest.raises(TypeError, match="mapping"):
        manifest.created("ticket", uid(1), manifest.label("t"), ["not", "a", "mapping"])


@pytest.mark.parametrize(
    "key", ["api_key", "X-API-Key", "apikey", "GORELO_API_KEY", "Authorization", "password", "secret", "token", "access_token", "Bearer"]
)
def test_details_never_hold_a_secret_key(manifest, key):
    with pytest.raises(ValueError, match="looks like a secret"):
        manifest.created("ticket", uid(1), manifest.label("t"), {key: "x"})
    with pytest.raises(ValueError, match="looks like a secret"):
        manifest.created("ticket", uid(1), manifest.label("t"), {"nested": [{key: "x"}]})
    assert manifest.all_created() == []


# --------------------------------------------------------------------------
# listed leftovers are kept apart from the run's own records
# --------------------------------------------------------------------------


def test_an_approved_leftover_never_joins_the_ids_of_the_run(manifest):
    manifest.leftover_outcome("client", 9801, "Deleted", deleted=True)
    manifest.leftover_outcome("contact", 9900, "skipped: name does not match", deleted=False)
    assert manifest.ids("client") == set() and manifest.ids("contact") == set()
    assert manifest.all_created() == [] and manifest.leftovers() == []
    outcomes = manifest.approved_leftover_outcomes()
    assert [(o["kind"], o["id"], o["deleted"]) for o in outcomes] == [("client", 9801, True), ("contact", 9900, False)]
    with pytest.raises(ValueError, match="listed leftovers are"):
        manifest.leftover_outcome("ticket", uid(1), "x", deleted=False)


# --------------------------------------------------------------------------
# atomic writes
# --------------------------------------------------------------------------


def test_no_temp_file_is_left_behind(manifest):
    for index in range(1, 6):
        manifest.created("contact", index, manifest.label(f"c{index}"))
        manifest.cleaned("contact", index, "Deleted")
    assert sorted(p.name for p in manifest.path.parent.iterdir()) == [manifest.path.name]


def test_a_failed_write_leaves_the_previous_file_intact_and_keeps_the_change_in_memory(manifest, monkeypatch):
    manifest.created("ticket", uid(1), manifest.label("first"))
    before = manifest.path.read_text(encoding="utf-8")

    def broken_replace(source, target):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(manifest_module.os, "replace", broken_replace)
        with pytest.raises(OSError, match="disk full"):
            manifest.created("ticket", uid(2), manifest.label("second"))
    assert manifest.path.read_text(encoding="utf-8") == before  # the old file, byte for byte
    assert sorted(p.name for p in manifest.path.parent.iterdir()) == [manifest.path.name]  # no temp file
    assert uid(2) in manifest.ids("ticket")  # the in-memory state kept the change
    manifest.created("ticket", uid(3), manifest.label("third"))  # and the next successful write persists both
    assert {r["id"] for r in read_file(manifest)["records"]} == {uid(1), uid(2), uid(3)}


def test_an_interrupted_write_never_leaves_half_a_file(manifest, monkeypatch):
    manifest.created("ticket", uid(1), manifest.label("first"))
    before = manifest.path.read_text(encoding="utf-8")

    def interrupted(source, target):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(manifest_module.os, "replace", interrupted)
        with pytest.raises(KeyboardInterrupt):
            manifest.created("ticket", uid(2), manifest.label("second"))
    assert manifest.path.read_text(encoding="utf-8") == before
    assert json.loads(before)["records"][0]["id"] == uid(1)
    assert sorted(p.name for p in manifest.path.parent.iterdir()) == [manifest.path.name]


def test_the_temp_file_is_flushed_to_disk_before_it_replaces_the_manifest(manifest, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(descriptor):
        events.append("fsync")
        real_fsync(descriptor)

    def replace(source, target):
        events.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(manifest_module.os, "fsync", fsync)
    monkeypatch.setattr(manifest_module.os, "replace", replace)
    manifest.created("ticket", uid(1), manifest.label("first"))
    assert events[:2] == ["fsync", "replace"]  # the file first, the swap second (the directory is flushed after)
    assert events.count("replace") == 1 and "fsync" in events[2:]


def test_the_new_content_is_written_to_a_temp_file_in_the_same_directory(manifest, monkeypatch):
    seen = {}
    real_replace = os.replace

    def spy(source, target):
        seen["source"], seen["target"] = Path(source), Path(target)
        assert json.loads(Path(source).read_text(encoding="utf-8"))["records"][0]["id"] == uid(1)  # complete before the swap
        real_replace(source, target)

    monkeypatch.setattr(manifest_module.os, "replace", spy)
    manifest.created("ticket", uid(1), manifest.label("first"))
    assert seen["source"].parent == manifest.path.parent and seen["target"] == manifest.path
    assert seen["source"] != seen["target"]


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def test_load_of_a_missing_file_is_a_file_not_found_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        Manifest.load(tmp_path / "nope.json")


@pytest.mark.parametrize(
    "mutate, problem",
    [
        (lambda s: s.update(format=99), "format"),
        (lambda s: s.update(run_id="MCPTEST-1"), "run_id"),
        (lambda s: s.update(records="x"), "records"),
        (lambda s: s.update(next_seq="1"), "next_seq"),
        (lambda s: s["records"].append({"kind": "asset", "id": 1}), "invalid"),
        (lambda s: s["records"][0].update(label="other label"), "label"),
        (lambda s: s["records"].append(dict(s["records"][0])), "twice"),
        (lambda s: s["records"][0].update(id="not-a-uuid"), "invalid"),
        (lambda s: s["records"][0].update(cleaned="yes"), "cleaned"),
        (lambda s: s["records"].append("not an object"), "a record is not an object"),
        (lambda s: s["records"][0].update(details=[]), "no details or attempts"),
        (lambda s: s["records"][0].pop("attempts"), "no details or attempts"),
        (lambda s: s["intents"].append("not an object"), "an intent is invalid"),
        (lambda s: s["intents"].append({"kind": "asset"}), "an intent is invalid"),
        (lambda s: s["intents"].append({"kind": "ticket", "label": RUN, "status": "weird"}), "unknown status"),
        (lambda s: s["intents"].append({"kind": "ticket", "label": "other", "status": "open"}), "label of an intent"),
        (lambda s: s["approved_leftovers"].append({"kind": "ticket"}), "listed leftover"),
    ],
)
def test_load_refuses_a_file_that_is_not_a_valid_manifest(manifest, mutate, problem):
    manifest.created("ticket", uid(1), manifest.label("t"))
    state = read_file(manifest)
    mutate(state)
    manifest.path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match=problem):
        Manifest.load(manifest.path)


@pytest.mark.parametrize("content", ["", "not json", "[1, 2]", "null"])
def test_load_refuses_text_that_is_not_json_or_not_an_object(manifest, content):
    manifest.path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="not a valid run manifest"):
        Manifest.load(manifest.path)


def test_a_file_that_is_not_text_is_refused_by_load_and_by_the_constructor(manifest):
    manifest.path.write_bytes(b"\xff\xfe\x00{}")
    with pytest.raises(ValueError, match="not UTF-8 text"):
        Manifest.load(manifest.path)
    with pytest.raises(ValueError, match="not UTF-8 text"):
        Manifest(RUN, manifest.path)
    manifest.path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        Manifest(RUN, manifest.path)


def test_a_manifest_file_of_another_run_is_not_taken_over(manifest):
    with pytest.raises(ValueError, match="belongs to run"):
        Manifest("MCPTEST-20991002101501", manifest.path)


# --------------------------------------------------------------------------
# _env: the API key and the settings
# --------------------------------------------------------------------------

KEY = "k-test-0123456789"


def write_env(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_api_key_returns_the_value_of_that_one_line(tmp_path):
    path = write_env(tmp_path, f"OTHER_SECRET=do-not-read-me\nPUBLIC_BASE_URL=https://x.example\nGORELO_API_KEY={KEY}\nMCP_AUTH_PASSWORD=pw\n")
    assert _env.load_api_key(path) == KEY


@pytest.mark.parametrize(
    "line",
    [
        f"GORELO_API_KEY={KEY}",
        f"GORELO_API_KEY = {KEY}",
        f"  GORELO_API_KEY={KEY}  ",
        f"export GORELO_API_KEY={KEY}",
        f'GORELO_API_KEY="{KEY}"',
        f"GORELO_API_KEY='{KEY}'",
        f"GORELO_API_KEY={KEY} # the live key",
        f'GORELO_API_KEY="{KEY}" # the live key',
    ],
)
def test_load_api_key_understands_the_usual_dotenv_spellings(tmp_path, line):
    assert _env.load_api_key(write_env(tmp_path, f"# comment\n\n{line}\nOTHER=1\n")) == KEY


def test_a_value_with_a_hash_inside_quotes_is_kept_whole(tmp_path):
    assert _env.load_api_key(write_env(tmp_path, 'GORELO_API_KEY="ab #cd"\n')) == "ab #cd"


def test_the_last_assignment_wins_like_python_dotenv(tmp_path):
    assert _env.load_api_key(write_env(tmp_path, "GORELO_API_KEY=old\nGORELO_API_KEY=new\n")) == "new"


@pytest.mark.parametrize(
    "text",
    [
        "NOT_THE_KEY=1\n",
        "# GORELO_API_KEY=commented-out\n",
        "GORELO_API_KEY_OLD=1\n",
        "XGORELO_API_KEY=1\n",
        "GORELO_API_KEY\n",
        "",
    ],
)
def test_a_missing_key_is_reported_without_quoting_the_file(tmp_path, text):
    path = write_env(tmp_path, text + "SECRET_OTHER=never-shown\n")
    with pytest.raises(_env.EnvError, match="GORELO_API_KEY is not set") as error:
        _env.load_api_key(path)
    assert "never-shown" not in str(error.value) and str(path) in str(error.value)


@pytest.mark.parametrize("line", ["GORELO_API_KEY=", "GORELO_API_KEY=   ", 'GORELO_API_KEY=""'])
def test_a_blank_key_is_an_error(tmp_path, line):
    with pytest.raises(_env.EnvError, match="blank"):
        _env.load_api_key(write_env(tmp_path, line + "\n"))


def test_an_unreadable_file_is_an_error_that_names_the_file_only(tmp_path):
    with pytest.raises(_env.EnvError, match="cannot read the env file") as error:
        _env.load_api_key(tmp_path / "missing.env")
    assert "FileNotFoundError" in str(error.value)
    binary = tmp_path / "binary.env"
    binary.write_bytes(b"\xff\xfe\x00GORELO_API_KEY=x\n")
    with pytest.raises(_env.EnvError, match="not valid UTF-8"):
        _env.load_api_key(binary)


def test_the_default_file_is_the_live_services_env_but_a_given_path_never_touches_it(tmp_path, monkeypatch):
    assert APP_ENV_FILE == Path("/opt/gorelo-mcp/app/.env")
    monkeypatch.setattr(_env, "ENV_FILE", tmp_path / "does-not-exist")
    assert _env.load_api_key(write_env(tmp_path, f"GORELO_API_KEY={KEY}\n")) == KEY
    with pytest.raises(_env.EnvError):
        _env.load_api_key()  # the (patched) default is used when no path is given


def test_live_settings_are_everything_on_destructive_and_need_no_http_variables(tmp_path):
    settings = _env.live_settings(write_env(tmp_path, f"GORELO_API_KEY={KEY}\n"))
    assert settings.api_key == KEY
    assert settings.toolsets == frozenset(TOOLSETS)
    assert settings.destructive is True
    assert settings.public_base_url is None and settings.mcp_auth_password is None
    assert KEY not in repr(settings)


def test_scrub_removes_the_secret_from_text():
    assert _env.scrub(f"sent {KEY} twice {KEY}", KEY) == "sent *** twice ***"
    assert _env.scrub("nothing", KEY) == "nothing"
    assert _env.scrub("keep", None) == "keep" and _env.scrub("keep", "") == "keep"


class FakeTime:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


async def test_the_pacer_keeps_the_interval_between_request_starts():
    time = FakeTime()
    pacer = _env.Pacer(1.0, clock=time.clock, sleep=time.sleep)
    request = httpx.Request("GET", "https://api.usw.gorelo.io/v1/clients")
    await pacer(request)  # the first request never waits
    await pacer(request)
    time.now += 0.4  # work that took 0.4 s counts against the interval
    await pacer(request)
    time.now += 5.0  # a long pause: no wait
    await pacer(request)
    assert time.sleeps == [1.0, pytest.approx(0.6)]
    assert pacer.requests == 4


async def test_a_zero_interval_never_waits_and_a_negative_one_is_refused():
    time = FakeTime()
    pacer = _env.Pacer(0, clock=time.clock, sleep=time.sleep)
    for _ in range(3):
        await pacer(httpx.Request("GET", "https://api.usw.gorelo.io/v1/clients"))
    assert time.sleeps == []
    with pytest.raises(ValueError):
        _env.Pacer(-1)


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------

TEST_CLIENT_RECORD = 8200
CONTACT = 8301
TICKET = uid(1)
COMMENT = uid(2)
PUBLIC_COMMENT = uid(3)
TIME_ENTRY = 777
ITEM = uid(4)
UPTIME = uid(5)
PROJECT = uid(6)
SECTION = uid(7)
TASK = uid(8)
PROJECT_COMMENT = uid(9)
SIDE = uid(10)
UNDELETABLE = "soft-deleted with its ticket; the file itself cannot be deleted through the API"


def populate(manifest: Manifest) -> None:
    """One record of every kind, in this creation order."""
    label = manifest.label
    manifest.created("client", TEST_CLIENT_RECORD, label("client"))
    manifest.created("contact", CONTACT, label("contact"))
    manifest.created("ticket", TICKET, label("ticket"), {"contact_id": None})
    manifest.created("comment", COMMENT, label("private comment"), {"ticket_id": TICKET, "private": True})
    manifest.created("comment", PUBLIC_COMMENT, label("public comment"), {"ticket_id": TICKET, "private": False})
    manifest.created("time_entry", TIME_ENTRY, label("time entry"), {"ticket_id": TICKET})
    manifest.created("item", ITEM, label("item"))
    manifest.created("uptime", UPTIME, label("uptime"))
    manifest.created("project", PROJECT, label("project"))
    manifest.created("section", SECTION, label("section"), {"project_id": PROJECT})
    manifest.created("task", TASK, label("task"), {"project_id": PROJECT})
    manifest.created("project_comment", PROJECT_COMMENT, label("task comment"), {"project_id": PROJECT, "task_id": TASK})
    manifest.created("attachment", "report.txt", label("attachment"), {"ticket_id": TICKET})
    manifest.created("side_conversation", SIDE, label("side conversation"), {"ticket_id": TICKET})
    manifest.created("approval", "approval-1", label("approval"), {"ticket_id": TICKET})


DELETE_ROUTES = {
    "/v1/tickets/{ticketId}": envelope({"Id": "x"}),
    "/v1/tickets/{ticketId}/comments/{commentId}": envelope({"Id": "x"}),
    "/v1/time-entries/{timeEntryId}": envelope({"Id": TIME_ENTRY, "Outcome": "Deleted"}),
    "/v1/items/{itemId}": envelope({"Id": ITEM}),
    "/v1/uptime/{checkId}": envelope({"Id": UPTIME}),
    "/v1/projects/{projectId}": envelope({"Id": PROJECT}),
    "/v1/projects/{projectId}/tasks/{taskId}": envelope({"Id": TASK}),
    "/v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}": envelope({"Id": PROJECT_COMMENT}),
    "/v1/contacts/{contactId}": envelope({"Id": CONTACT}),
    "/v1/clients/{clientId}": envelope({"Id": TEST_CLIENT_RECORD}),
}


def route_all(mock, **overrides):
    for path, response in DELETE_ROUTES.items():
        mock.on("DELETE", path, overrides.get(path, response))


@pytest.fixture
def lines():
    return []


@pytest.fixture
def run_it(manifest, mock_gorelo, make_settings, lines):
    async def run(**options):
        options.setdefault("pace", 0)
        return await cleanup.run_cleanup(
            manifest,
            settings=make_settings(destructive=True),
            transport=mock_gorelo.transport,
            echo=lines.append,
            **options,
        )

    return run


def sent(mock):
    return [(r.method, r.path) for r in mock.requests]


async def test_cleanup_deletes_in_reverse_creation_order_with_tools_where_they_exist(manifest, mock_gorelo, run_it, lines):
    populate(manifest)
    route_all(mock_gorelo)
    report = await run_it()
    assert sent(mock_gorelo) == [
        ("DELETE", f"/v1/projects/{PROJECT}/tasks/{TASK}/comments/{PROJECT_COMMENT}"),  # delete_project_comment
        ("DELETE", f"/v1/projects/{PROJECT}/tasks/{TASK}"),  # delete_project_task
        ("DELETE", f"/v1/projects/{PROJECT}"),  # raw
        ("DELETE", f"/v1/uptime/{UPTIME}"),  # delete_uptime_check
        ("DELETE", f"/v1/items/{ITEM}"),  # delete_item
        ("DELETE", f"/v1/time-entries/{TIME_ENTRY}"),  # delete_time_entry
        ("DELETE", f"/v1/tickets/{TICKET}/comments/{COMMENT}"),  # delete_ticket_comment (the public one is not attempted)
        ("DELETE", f"/v1/tickets/{TICKET}"),  # raw
        ("DELETE", f"/v1/contacts/{CONTACT}"),  # raw
        ("DELETE", f"/v1/clients/{TEST_CLIENT_RECORD}"),  # raw
    ]
    assert report.ok and manifest.unresolved_intents() == []
    # the uploaded file is the one record that stays: the API cannot delete it (it is reported apart, never as cleaned)
    assert [(r.kind, r.id) for r in manifest.leftovers()] == [("attachment", "report.txt")]
    assert report.leftovers == [] and [r.id for r in report.undeletable] == ["report.txt"]
    outcomes = {(r.kind, r.id): r.outcome for r in manifest.all_created()}
    assert outcomes[("ticket", TICKET)] == "Deleted" and outcomes[("time_entry", TIME_ENTRY)] == "Deleted"
    assert outcomes[("section", SECTION)] == f"removed with project {PROJECT}"
    assert outcomes[("attachment", "report.txt")] == UNDELETABLE
    assert outcomes[("side_conversation", SIDE)] == f"removed with ticket {TICKET}"
    assert outcomes[("approval", "approval-1")] == f"removed with ticket {TICKET}"
    assert outcomes[("comment", PUBLIC_COMMENT)] == f"removed with ticket {TICKET}"
    assert lines[0] == "cleanup of MCPTEST-20991002101500: 14 cleaned, 0 left over, 1 known undeletable"
    assert f"  known undeletable attachment report.txt {manifest.label('attachment')}: {UNDELETABLE}" in lines
    assert "result: nothing left over" not in lines  # an uploaded file stays in Gorelo
    assert lines[-1] == "result: cleanup complete; 1 known undeletable record remains (see above)"


async def test_the_tools_send_the_ids_from_the_manifest_with_confirm(manifest, mock_gorelo, run_it):
    manifest.created("ticket", TICKET, manifest.label("t"), {"contact_id": None})
    manifest.created("comment", COMMENT, manifest.label("c"), {"ticket_id": TICKET})
    route_all(mock_gorelo)
    await run_it()
    # confirm=true reached the tool (a tool without it makes no request at all)
    assert sent(mock_gorelo)[0] == ("DELETE", f"/v1/tickets/{TICKET}/comments/{COMMENT}")
    assert all(r.json is None and r.query == {} for r in mock_gorelo.requests)
    assert all(r.headers["x-api-key"] == TEST_API_KEY for r in mock_gorelo.requests)


async def test_every_request_of_both_clients_passes_one_cleanup_guard(manifest, mock_gorelo, make_settings, monkeypatch):
    populate(manifest)
    route_all(mock_gorelo)
    made = []

    class Spy(LiveGuard):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.seen = []
            made.append((args, kwargs, self))

        def check(self, request):
            self.seen.append((request.method, request.url.path))
            super().check(request)

    monkeypatch.setattr(cleanup, "LiveGuard", Spy)
    await cleanup.run_cleanup(manifest, settings=make_settings(destructive=True), transport=mock_gorelo.transport, pace=0, echo=lambda line: None)
    assert len(made) == 1
    args, kwargs, guard = made[0]
    assert args[0] == "write" and kwargs == {"cleanup": True}
    assert guard.seen == sent(mock_gorelo) and len(guard.seen) == 10  # tool requests and raw requests alike
    assert not guard.tripped


async def test_cleanup_is_paced_across_both_clients(manifest, mock_gorelo, make_settings, monkeypatch):
    manifest.created("ticket", TICKET, manifest.label("t"), {"contact_id": None})
    manifest.created("comment", COMMENT, manifest.label("c"), {"ticket_id": TICKET})
    manifest.created("contact", CONTACT, manifest.label("k"))
    route_all(mock_gorelo)
    time = FakeTime()
    intervals = []

    def make_pacer(interval):
        intervals.append(interval)
        return _env.Pacer(interval, clock=time.clock, sleep=time.sleep)

    monkeypatch.setattr(cleanup, "Pacer", make_pacer)
    await cleanup.run_cleanup(manifest, settings=make_settings(destructive=True), transport=mock_gorelo.transport, echo=lambda line: None)
    assert intervals == [1.0] and cleanup.PACE_SECONDS == 1.0
    assert len(mock_gorelo.requests) == 3
    assert time.sleeps == [1.0, 1.0]  # a tool request, then raw ones, one second apart


# the Reopened loop


async def test_a_reopened_time_entry_is_deleted_again_until_it_is_deleted(manifest, mock_gorelo, run_it):
    manifest.created("time_entry", TIME_ENTRY, manifest.label("e"))
    mock_gorelo.on(
        "DELETE",
        "/v1/time-entries/{timeEntryId}",
        in_order(
            envelope({"Id": TIME_ENTRY, "Outcome": "Reopened"}),
            envelope({"Id": TIME_ENTRY, "Outcome": "Reopened"}),
            envelope({"Id": TIME_ENTRY, "Outcome": "Deleted"}),
        ),
    )
    report = await run_it()
    assert len(mock_gorelo.requests) == 3
    assert report.ok and manifest.record("time_entry", TIME_ENTRY).outcome == "Deleted after 3 calls (Reopened first)"


async def test_one_reopen_then_deleted_takes_two_calls(manifest, mock_gorelo, run_it):
    manifest.created("time_entry", TIME_ENTRY, manifest.label("e"))
    mock_gorelo.on(
        "DELETE",
        "/v1/time-entries/{timeEntryId}",
        in_order(envelope({"Id": TIME_ENTRY, "Outcome": "Reopened"}), envelope({"Id": TIME_ENTRY, "Outcome": "Deleted"})),
    )
    await run_it()
    assert len(mock_gorelo.requests) == 2
    assert manifest.record("time_entry", TIME_ENTRY).outcome == "Deleted after 2 calls (Reopened first)"


async def test_a_time_entry_that_stays_reopened_is_given_up_after_three_calls(manifest, mock_gorelo, run_it, lines):
    manifest.created("time_entry", TIME_ENTRY, manifest.label("e"))
    mock_gorelo.on("DELETE", "/v1/time-entries/{timeEntryId}", envelope({"Id": TIME_ENTRY, "Outcome": "Reopened"}))
    report = await run_it()
    assert cleanup.MAX_REOPEN_ATTEMPTS == 3 and len(mock_gorelo.requests) == 3
    assert not report.ok and [r.id for r in manifest.leftovers()] == [TIME_ENTRY]
    assert manifest.record("time_entry", TIME_ENTRY).outcome == "still Reopened after 3 delete calls"
    assert any(line.startswith("  LEFTOVER   time_entry 777") for line in lines) and lines[-1] == "result: SOMETHING IS LEFT OVER"


async def test_a_time_entry_deleted_on_the_first_call_is_not_repeated(manifest, mock_gorelo, run_it):
    manifest.created("time_entry", TIME_ENTRY, manifest.label("e"))
    route_all(mock_gorelo)
    await run_it()
    assert len(mock_gorelo.requests) == 1


async def test_an_unexpected_outcome_stops_the_loop(manifest, mock_gorelo, run_it):
    manifest.created("time_entry", TIME_ENTRY, manifest.label("e"))
    mock_gorelo.on("DELETE", "/v1/time-entries/{timeEntryId}", envelope({"Id": TIME_ENTRY, "Outcome": "Archived"}))
    report = await run_it()
    assert len(mock_gorelo.requests) == 1 and not report.ok
    assert "unexpected Outcome" in manifest.record("time_entry", TIME_ENTRY).outcome


# failures


async def test_a_refusal_is_recorded_and_the_rest_is_still_cleaned(manifest, mock_gorelo, run_it, lines):
    populate(manifest)
    route_all(
        mock_gorelo,
        **{"/v1/contacts/{contactId}": error_envelope(409, [("070901", "The contact has open tickets.")])},
    )
    report = await run_it()
    assert [r.id for r in report.leftovers] == [CONTACT] and not report.ok  # the uploaded file is reported apart
    assert [r.id for r in manifest.leftovers()] == [CONTACT, "report.txt"]
    assert manifest.record("contact", CONTACT).outcome == "HTTP 409: The contact has open tickets."
    assert ("DELETE", f"/v1/clients/{TEST_CLIENT_RECORD}") in sent(mock_gorelo)  # it went on after the refusal
    assert any(line.startswith(f"  LEFTOVER   contact {CONTACT} ") and "open tickets" in line for line in lines)


async def test_a_record_that_is_gone_counts_as_cleaned(manifest, mock_gorelo, run_it):
    manifest.created("ticket", TICKET, manifest.label("t"), {"contact_id": None})
    manifest.created("item", ITEM, manifest.label("i"))
    route_all(
        mock_gorelo,
        **{
            "/v1/tickets/{ticketId}": error_envelope(404, [("070404", "Not found.")]),
            "/v1/items/{itemId}": error_envelope(404, [("070404", "Item not found.", "itemId")]),
        },
    )
    report = await run_it()
    assert report.ok and manifest.leftovers() == []
    assert manifest.record("ticket", TICKET).outcome == "already gone (HTTP 404)"
    assert manifest.record("item", ITEM).outcome == "already gone (HTTP 404)"


async def test_a_comment_that_cannot_be_deleted_goes_with_its_ticket(manifest, mock_gorelo, run_it):
    manifest.created("ticket", TICKET, manifest.label("t"), {"contact_id": None})
    manifest.created("comment", COMMENT, manifest.label("c"), {"ticket_id": TICKET})
    route_all(
        mock_gorelo,
        **{"/v1/tickets/{ticketId}/comments/{commentId}": error_envelope(409, [("070901", "Only private comments can be deleted.")])},
    )
    report = await run_it()
    assert report.ok
    assert manifest.record("comment", COMMENT).outcome == f"removed with ticket {TICKET}"
    assert [a["ok"] for a in manifest.record("comment", COMMENT).attempts] == [False, True]


async def test_a_time_entry_that_cannot_be_deleted_is_not_covered_by_its_ticket(manifest, mock_gorelo, run_it):
    manifest.created("ticket", TICKET, manifest.label("t"), {"contact_id": None})
    manifest.created("time_entry", TIME_ENTRY, manifest.label("e"))
    route_all(mock_gorelo, **{"/v1/time-entries/{timeEntryId}": error_envelope(409, [("070901", "Only open time entries can be deleted.")])})
    report = await run_it()
    assert not report.ok and [r.id for r in manifest.leftovers()] == [TIME_ENTRY]
    assert manifest.record("ticket", TICKET).cleaned


async def test_a_child_whose_parent_was_not_cleaned_stays_a_leftover(manifest, mock_gorelo, run_it):
    manifest.created("ticket", TICKET, manifest.label("t"), {"contact_id": None})
    manifest.created("attachment", "report.txt", manifest.label("a"), {"ticket_id": TICKET})
    manifest.created("section", SECTION, manifest.label("s"), {"project_id": uid(77)})  # a project the run did not record
    route_all(mock_gorelo, **{"/v1/tickets/{ticketId}": error_envelope(409, [("070901", "Blocked.")])})
    report = await run_it()
    assert {r.kind for r in manifest.leftovers()} == {"ticket", "attachment", "section"} and not report.ok


async def test_a_transport_error_is_a_leftover_not_a_crash(manifest, mock_gorelo, run_it):
    manifest.created("contact", CONTACT, manifest.label("k"))
    manifest.created("item", ITEM, manifest.label("i"))
    route_all(
        mock_gorelo,
        **{"/v1/contacts/{contactId}": httpx.ConnectError("boom"), "/v1/items/{itemId}": httpx.ReadTimeout("slow")},
    )
    report = await run_it()
    assert not report.ok and {r.kind for r in manifest.leftovers()} == {"contact", "item"}
    assert "no answer from Gorelo (ConnectError)" in manifest.record("contact", CONTACT).outcome
    assert "did not confirm" in manifest.record("item", ITEM).outcome


async def test_a_non_envelope_answer_is_a_leftover(manifest, mock_gorelo, run_it):
    manifest.created("contact", CONTACT, manifest.label("k"))
    mock_gorelo.on("DELETE", "/v1/contacts/{contactId}", httpx.Response(502, text="<html>bad gateway</html>"))
    report = await run_it()
    assert not report.ok and manifest.record("contact", CONTACT).outcome == "HTTP 502"


async def test_the_guard_refusing_a_delete_is_recorded_and_nothing_is_sent(manifest, mock_gorelo, run_it):
    manifest.created("comment", COMMENT, manifest.label("c"), {"ticket_id": uid(99)})  # a ticket this run never created
    manifest.created("contact", CONTACT, manifest.label("k"))
    route_all(mock_gorelo)
    report = await run_it()
    assert sent(mock_gorelo) == [("DELETE", f"/v1/contacts/{CONTACT}")]
    assert not report.ok
    outcome = manifest.record("comment", COMMENT).outcome
    assert outcome.startswith("the guard refused it: blocked DELETE /tickets/") and "not a ticket created by this run" in outcome


async def test_a_record_without_the_parent_id_cannot_be_deleted_by_tool(manifest, mock_gorelo, run_it):
    manifest.created("comment", COMMENT, manifest.label("c"))
    manifest.created("task", TASK, manifest.label("t"))
    manifest.created("project_comment", PROJECT_COMMENT, manifest.label("pc"))
    report = await run_it()
    assert mock_gorelo.requests == [] and not report.ok
    assert "have no ticket_id" in manifest.record("comment", COMMENT).outcome
    assert "have no project_id" in manifest.record("task", TASK).outcome
    assert "have no project_id" in manifest.record("project_comment", PROJECT_COMMENT).outcome


async def test_a_project_comment_without_a_task_uses_the_project_path(manifest, mock_gorelo, run_it):
    manifest.created("project", PROJECT, manifest.label("p"))
    manifest.created("project_comment", PROJECT_COMMENT, manifest.label("pc"), {"project_id": PROJECT})
    mock_gorelo.on("DELETE", "/v1/projects/{projectId}/comments/{commentId}", envelope({"Id": PROJECT_COMMENT}))
    mock_gorelo.on("DELETE", "/v1/projects/{projectId}", envelope({"Id": PROJECT}))
    assert (await run_it()).ok
    assert sent(mock_gorelo) == [
        ("DELETE", f"/v1/projects/{PROJECT}/comments/{PROJECT_COMMENT}"),
        ("DELETE", f"/v1/projects/{PROJECT}"),
    ]


async def test_a_second_cleanup_skips_what_is_already_cleaned(manifest, mock_gorelo, run_it):
    populate(manifest)
    route_all(mock_gorelo)
    await run_it()
    first = len(mock_gorelo.requests)
    assert first == 10
    report = await run_it()
    assert len(mock_gorelo.requests) == first and report.ok


async def test_a_second_cleanup_adds_nothing_for_the_undeletable_file(manifest, mock_gorelo, run_it, lines):
    manifest.created("ticket", TICKET, manifest.label("t"), {"contact_id": None})
    manifest.created("attachment", "report.txt", manifest.label("a"), {"ticket_id": TICKET})
    route_all(mock_gorelo)
    await run_it()
    attempts = manifest.record("attachment", "report.txt").attempts
    assert [(a["ok"], a["outcome"]) for a in attempts] == [(False, UNDELETABLE)]
    requests = len(mock_gorelo.requests)
    lines.clear()
    report = await run_it()
    assert len(mock_gorelo.requests) == requests and report.ok
    assert manifest.record("attachment", "report.txt").attempts == attempts  # the same outcome is not recorded twice
    assert lines[0] == f"cleanup of {RUN}: 1 cleaned, 0 left over, 1 known undeletable"
    assert lines[-1] == "result: cleanup complete; 1 known undeletable record remains (see above)"


async def test_an_undeletable_file_alone_never_fails_the_run_but_a_real_leftover_does(manifest, mock_gorelo, run_it, lines):
    manifest.created("ticket", TICKET, manifest.label("t"), {"contact_id": None})
    manifest.created("attachment", "report.txt", manifest.label("a"), {"ticket_id": TICKET})
    manifest.created("contact", CONTACT, manifest.label("k"))
    route_all(mock_gorelo, **{"/v1/contacts/{contactId}": error_envelope(409, [("070901", "The contact has open tickets.")])})
    report = await run_it()
    assert not report.ok  # the contact could not be deleted
    assert [r.kind for r in report.leftovers] == ["contact"] and [r.kind for r in report.undeletable] == ["attachment"]
    assert lines[0] == "cleanup of MCPTEST-20991002101500: 1 cleaned, 1 left over, 1 known undeletable"
    assert any(line.startswith("  known undeletable attachment report.txt ") for line in lines)
    assert lines[-1] == "result: SOMETHING IS LEFT OVER"  # the file does not soften a real leftover


async def test_an_undeletable_file_names_the_kind_of_parent_that_was_deleted(manifest, mock_gorelo, run_it):
    manifest.created("project", PROJECT, manifest.label("p"))
    manifest.created("attachment", "plan.txt", manifest.label("a"), {"project_id": PROJECT})
    route_all(mock_gorelo)
    report = await run_it()
    assert report.ok
    assert manifest.record("attachment", "plan.txt").outcome == "soft-deleted with its project; " + cleanup.UNDELETABLE_REASON


async def test_a_file_whose_parent_could_not_be_deleted_is_a_plain_leftover_with_no_outcome(manifest, mock_gorelo, run_it, lines):
    manifest.created("ticket", TICKET, manifest.label("t"), {"contact_id": None})
    manifest.created("attachment", "report.txt", manifest.label("a"), {"ticket_id": TICKET})
    route_all(mock_gorelo, **{"/v1/tickets/{ticketId}": error_envelope(409, [("070901", "Blocked.")])})
    report = await run_it()
    assert not report.ok and report.undeletable == []
    assert {r.kind for r in report.leftovers} == {"ticket", "attachment"}
    assert manifest.record("attachment", "report.txt").outcome is None  # nothing was claimed about it
    assert any(line.startswith("  LEFTOVER   attachment report.txt ") and line.endswith("(not attempted)") for line in lines)
    assert lines[-1] == "result: SOMETHING IS LEFT OVER"


async def test_an_announced_create_without_an_id_is_reported_and_fails_the_run(manifest, mock_gorelo, run_it, lines):
    manifest.intent("ticket", manifest.label("maybe there"))
    report = await run_it()
    assert not report.ok and report.leftovers == [] and [i.label for i in report.unresolved] == [manifest.label("maybe there")]
    assert any(line.startswith("  UNRESOLVED ticket MCPTEST-20991002101500 maybe there") for line in lines)
    assert lines[-1] == "result: SOMETHING IS LEFT OVER"


async def test_the_summary_lists_ids_and_labels_and_never_the_api_key(manifest, mock_gorelo, run_it, lines):
    manifest.created("contact", CONTACT, manifest.label(f"oops {TEST_API_KEY}"))
    route_all(mock_gorelo)
    await run_it()
    text = "\n".join(lines)
    assert TEST_API_KEY not in text and "***" in text
    assert f"cleaned    contact {CONTACT} " in text


async def test_cleanup_never_prints_when_the_key_is_in_a_gorelo_message(manifest, mock_gorelo, run_it, lines):
    manifest.created("contact", CONTACT, manifest.label("k"))
    route_all(mock_gorelo, **{"/v1/contacts/{contactId}": error_envelope(409, [("1", f"key {TEST_API_KEY} rejected")])})
    await run_it()
    assert TEST_API_KEY not in "\n".join(lines)


def test_the_report_marks_an_approved_leftover_that_went_wrong():
    report = cleanup.CleanupReport(
        run_id=RUN,
        approved=[
            {"kind": "client", "id": 9801, "outcome": "delete failed: HTTP 409: busy", "deleted": False},
            {"kind": "client", "id": 9802, "outcome": "skipped: its name does not match the configured pattern, nothing was deleted", "deleted": False},
            {"kind": "contact", "id": 9900, "outcome": "Deleted", "deleted": True},
        ],
    )
    text = report.render()
    assert "  PROBLEM  listed leftover client 9801: delete failed: HTTP 409: busy" in text
    assert "  listed leftover client 9802: skipped" in text and "PROBLEM  listed leftover client 9802" not in text
    assert not report.ok and text.endswith("result: SOMETHING IS LEFT OVER")


def test_the_report_text_of_an_empty_run():
    clean = cleanup.CleanupReport(run_id=RUN)
    assert clean.ok and clean.render() == f"cleanup of {RUN}: 0 cleaned, 0 left over\nresult: nothing left over"


def undeletable_record(name="report.txt", outcome=UNDELETABLE):
    return Record(
        seq=1, kind="attachment", id=name, label=f"{RUN} attachment", details={"ticket_id": TICKET},
        created_at="2026-10-02T10:15:00Z", cleaned=False, outcome=outcome, attempts=(),
    )


def test_the_report_text_with_only_a_known_undeletable_file_never_says_nothing_is_left_over():
    report = cleanup.CleanupReport(run_id=RUN, undeletable=[undeletable_record()])
    assert report.ok  # it does not make the cleanup fail
    assert report.render().splitlines() == [
        f"cleanup of {RUN}: 0 cleaned, 0 left over, 1 known undeletable",
        f"  known undeletable attachment report.txt {RUN} attachment: {UNDELETABLE}",
        "result: cleanup complete; 1 known undeletable record remains (see above)",
    ]
    two = cleanup.CleanupReport(run_id=RUN, undeletable=[undeletable_record("a.txt"), undeletable_record("b.txt")])
    assert two.render().splitlines()[-1] == "result: cleanup complete; 2 known undeletable records remain (see above)"
    assert "nothing left over" not in report.render() and "nothing left over" not in two.render()


def test_a_report_with_a_leftover_and_an_undeletable_file_still_says_something_is_left_over():
    report = cleanup.CleanupReport(
        run_id=RUN, leftovers=[undeletable_record("plain.txt", outcome=None)], undeletable=[undeletable_record()]
    )
    assert not report.ok
    lines = report.render().splitlines()
    assert lines[0] == f"cleanup of {RUN}: 0 cleaned, 1 left over, 1 known undeletable"
    assert lines[-1] == "result: SOMETHING IS LEFT OVER"
    assert any(line.startswith("  LEFTOVER   attachment plain.txt ") for line in lines)
    assert any(line.startswith("  known undeletable attachment report.txt ") for line in lines)


def test_only_an_attachment_with_the_undeletable_outcome_counts_as_known_undeletable():
    assert cleanup.is_known_undeletable(undeletable_record())
    assert not cleanup.is_known_undeletable(undeletable_record(outcome=None))
    assert not cleanup.is_known_undeletable(undeletable_record(outcome="HTTP 409: blocked"))
    assert not cleanup.is_known_undeletable(
        Record(
            seq=1, kind="ticket", id=TICKET, label=f"{RUN} t", details={}, created_at="x", cleaned=False,
            outcome=UNDELETABLE, attempts=(),
        )
    )


async def test_a_tool_that_cannot_even_be_called_is_a_leftover(manifest, mock_gorelo, run_it, monkeypatch):
    manifest.created("item", ITEM, manifest.label("i"))

    async def down(self, *args, **kwargs):
        raise RuntimeError("connection to the in-process server was lost")

    monkeypatch.setattr(Client, "call_tool", down)
    report = await run_it()
    assert not report.ok and mock_gorelo.requests == []
    assert manifest.record("item", ITEM).outcome == "delete_item could not be called (RuntimeError)"


async def test_a_raw_delete_the_guard_refuses_is_recorded(manifest, mock_gorelo, run_it, monkeypatch):
    manifest.created("contact", CONTACT, manifest.label("k"))
    manifest.created("client", TEST_CLIENT_RECORD, manifest.label("c"))
    route_all(mock_gorelo)

    class Refusing(LiveGuard):
        def check(self, request):
            if "/contacts/" in request.url.path:
                raise GuardViolation("blocked by the test guard")
            super().check(request)

    monkeypatch.setattr(cleanup, "LiveGuard", Refusing)
    report = await run_it()
    assert not report.ok and sent(mock_gorelo) == [("DELETE", f"/v1/clients/{TEST_CLIENT_RECORD}")]
    assert manifest.record("contact", CONTACT).outcome == "the guard refused it: blocked by the test guard"


# invoices

INVOICE = uid(21)
INVOICE_NUMBER = 1042
TEST_CLIENT = 9501
DROP = object()  # a key the answer does not have
GONE = error_envelope(404, [("070404", "Invoice not found.", "invoiceId")])


def draft(**changes):
    """The invoice as GET /v1/invoices/{invoiceId} answers it; a DROP value removes the key."""
    record = {
        "Id": INVOICE, "ClientId": TEST_CLIENT, "Number": INVOICE_NUMBER, "DisplayNumber": "INV-1042", "Reference": f"{RUN} invoice",
        "Status": {"Id": 1, "Name": "Draft"}, **changes,
    }
    return {key: value for key, value in record.items() if value is not DROP}


def lookup_row(**changes):
    """A row of GET /v1/invoices, which delete_invoice reads to find the invoice by its Number."""
    return draft(**changes)


def record_invoice(manifest, **details):
    """The invoice as the matrix records it right after create_invoice answered: created as a Draft."""
    details = {"status_id": 1, "number": INVOICE_NUMBER, "display_number": "INV-1042", **details}
    return manifest.created("invoice", INVOICE, manifest.label("invoice"), details)


def route_draft(mock, *, read=None, rows=None, delete=None, ident=INVOICE, number=INVOICE_NUMBER):
    """The three routes a Draft's cleanup uses: the first read, delete_invoice's lookup by Number, and the DELETE."""
    mock.on("GET", f"/v1/invoices/{ident}", envelope(draft(Id=ident)) if read is None else read)
    mock.on(
        "GET",
        "/v1/invoices",
        paged_envelope([lookup_row(Id=ident)] if rows is None else rows),
        query={"Number": str(number)},
    )
    mock.on("DELETE", f"/v1/invoices/{ident}", envelope({"Id": ident, "StatusId": 6}) if delete is None else delete)


async def test_a_draft_invoice_is_read_first_and_deleted_with_delete_invoice(manifest, mock_gorelo, run_it, lines):
    record_invoice(manifest)
    route_draft(mock_gorelo)
    report = await run_it()
    assert sent(mock_gorelo) == [
        ("GET", f"/v1/invoices/{INVOICE}"),  # the first read: the status it reads decides what is sent next
        ("GET", "/v1/invoices"),  # delete_invoice finds the invoice by its Number
        ("DELETE", f"/v1/invoices/{INVOICE}"),
    ]
    assert mock_gorelo.requests[1].query["Number"] == "1042"
    assert report.ok and manifest.leftovers() == [] and manifest.record("invoice", INVOICE).outcome == "Deleted (Draft, delete_invoice)"
    assert lines[0] == f"cleanup of {RUN}: 1 cleaned, 0 left over"
    assert f"  cleaned    invoice {INVOICE} {manifest.label('invoice')} (Deleted (Draft, delete_invoice))" in lines
    assert lines[-1] == "result: nothing left over"


async def test_the_delete_of_a_draft_goes_through_the_cleanup_guard_with_the_invoice_recorded_as_a_draft(
    manifest, mock_gorelo, make_settings, monkeypatch
):
    record_invoice(manifest)
    route_draft(mock_gorelo)
    made = []

    class Spy(LiveGuard):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.seen = []
            made.append(self)

        def check(self, request):
            self.seen.append((request.method, request.url.path))
            super().check(request)

    monkeypatch.setattr(cleanup, "LiveGuard", Spy)
    await cleanup.run_cleanup(manifest, settings=make_settings(destructive=True), transport=mock_gorelo.transport, pace=0, echo=lambda line: None)
    assert len(made) == 1 and made[0].seen == sent(mock_gorelo) and not made[0].tripped
    assert manifest.details("invoice", INVOICE)["status_id"] == 1  # what the guard's DELETE rule reads


async def test_a_draft_invoice_without_a_number_is_deleted_by_the_raw_backstop(manifest, mock_gorelo, run_it):
    record_invoice(manifest, number=None)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Number=None)))
    mock_gorelo.on("DELETE", f"/v1/invoices/{INVOICE}", envelope({"Id": INVOICE, "StatusId": 6}))
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}"), ("DELETE", f"/v1/invoices/{INVOICE}")]  # no lookup by Number
    assert report.ok and manifest.record("invoice", INVOICE).outcome == "Deleted (Draft, raw DELETE)"


@pytest.mark.parametrize("number", [DROP, None, 0, -3, True, "1042", 1042.0])
async def test_an_invoice_whose_number_cannot_be_used_is_deleted_by_the_raw_backstop_too(manifest, mock_gorelo, run_it, number):
    record_invoice(manifest)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Number=number)))
    mock_gorelo.on("DELETE", f"/v1/invoices/{INVOICE}", envelope({"Id": INVOICE, "StatusId": 6}))
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}"), ("DELETE", f"/v1/invoices/{INVOICE}")]
    assert report.ok


GENERIC_TAIL = "this harness deletes only a Draft, and voids only an Approved invoice it created as Approved (with --void-approved)"


@pytest.mark.parametrize(
    "status, shown",
    [
        ({"Id": 5, "Name": "Approved"}, "Approved (id 5)"),  # the run recorded it as a Draft: it never voids that
        ({"Id": 99, "Name": "Mystery"}, "Mystery (id 99)"),
        ({"Id": 99}, "id 99"),
        ({"Id": True, "Name": "Draft"}, "Draft (id True)"),  # JSON true is not the integer 1
        ({"Id": "1", "Name": "Draft"}, "Draft (id '1')"),
        ({"Id": None}, "id None"),
        (None, "id None"),
        ("Draft", "id None"),
        (DROP, "id None"),
    ],
)
async def test_an_invoice_that_is_not_a_draft_is_never_deleted_or_voided_and_is_reported(
    manifest, mock_gorelo, run_it, lines, status, shown
):
    record_invoice(manifest)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Status=status)))
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}")]  # no lookup, no DELETE: nothing is voided or deleted
    assert not report.ok and [r.id for r in report.leftovers] == [INVOICE]
    outcome = f"left for the user: invoice INV-1042 has status {shown}; {GENERIC_TAIL}"
    assert manifest.record("invoice", INVOICE).outcome == outcome and not manifest.record("invoice", INVOICE).cleaned
    assert f"  LEFTOVER   invoice {INVOICE} {manifest.label('invoice')} ({outcome})" in lines
    assert lines[0] == f"cleanup of {RUN}: 0 cleaned, 1 left over" and lines[-1] == "result: SOMETHING IS LEFT OVER"


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("details", [{"status_id": 1}, {"status_id": 5}, {"status_id": 3}, {}])
async def test_a_paid_invoice_is_a_leftover_for_the_user_whatever_it_was_recorded_as_and_whatever_the_flag(
    manifest, mock_gorelo, run_it, lines, flag, details
):
    manifest.created("invoice", INVOICE, manifest.label("invoice"), {"number": INVOICE_NUMBER, "display_number": "INV-1042", **details})
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Status={"Id": 3, "Name": "Paid"})))
    report = await run_it(void_approved=flag)
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}")]  # Gorelo refuses to delete or void a Paid invoice: not asked
    outcome = (
        "left for the user: invoice INV-1042 has status Paid (id 3); Gorelo refuses to delete or void a Paid invoice, "
        "so it stays as it is"
    )
    assert manifest.record("invoice", INVOICE).outcome == outcome and not report.ok
    assert [r.id for r in report.leftovers] == [INVOICE] and report.undeletable == []
    assert f"  LEFTOVER   invoice {INVOICE} {manifest.label('invoice')} ({outcome})" in lines


@pytest.mark.parametrize(
    "changes, name",
    [
        ({}, "INV-1042"),
        ({"DisplayNumber": DROP}, "1042"),
        ({"DisplayNumber": "  ", "Number": DROP}, INVOICE),
        ({"DisplayNumber": DROP, "Number": DROP, "Id": DROP}, "(no number)"),
    ],
)
async def test_a_leftover_invoice_is_named_by_its_display_number_else_its_number_else_its_id(
    manifest, mock_gorelo, run_it, changes, name
):
    record_invoice(manifest)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Status={"Id": 5, "Name": "Approved"}, **changes)))
    await run_it()
    assert f"invoice {name} has status Approved" in manifest.record("invoice", INVOICE).outcome


async def test_an_invoice_that_is_gone_counts_as_cleaned_and_nothing_is_deleted(manifest, mock_gorelo, run_it):
    record_invoice(manifest)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", GONE)
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}")]
    assert report.ok and manifest.record("invoice", INVOICE).outcome == "already gone (HTTP 404)"


async def test_an_invoice_that_reads_back_as_deleted_counts_as_gone(manifest, mock_gorelo, run_it):
    record_invoice(manifest)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Status={"Id": 6, "Name": "Deleted"})))
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}")]
    assert report.ok and manifest.record("invoice", INVOICE).outcome == "already deleted (status 6, Deleted)"


async def test_a_second_cleanup_after_the_invoice_was_deleted_sends_nothing(manifest, mock_gorelo, run_it):
    record_invoice(manifest)
    route_draft(mock_gorelo)
    await run_it()
    first = len(mock_gorelo.requests)
    report = await run_it()
    assert len(mock_gorelo.requests) == first == 3 and report.ok


async def test_an_invoice_the_matrix_already_deleted_is_not_touched_again(manifest, mock_gorelo, run_it):
    record_invoice(manifest)
    manifest.cleaned("invoice", INVOICE, "deleted by the write matrix (delete_invoice, Draft)")
    report = await run_it()
    assert mock_gorelo.requests == [] and report.ok and report.cleaned[0].outcome.startswith("deleted by the write matrix")


@pytest.mark.parametrize(
    "read, said",
    [
        (error_envelope(500, [("070500", "boom")]), "could not be read first (HTTP 500: boom); nothing was deleted"),
        (envelope(["not", "an", "invoice"]), "Gorelo did not return an invoice when it was read first; nothing was deleted"),
        (envelope(None), "Gorelo did not return an invoice when it was read first; nothing was deleted"),
        (httpx.Response(502, text="<html>bad gateway</html>"), "could not be read first (HTTP 502); nothing was deleted"),
    ],
)
async def test_an_invoice_that_cannot_be_read_is_never_deleted_and_stays_a_leftover(manifest, mock_gorelo, run_it, read, said):
    record_invoice(manifest)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", read)
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}")]
    assert not report.ok and manifest.record("invoice", INVOICE).outcome == said


async def test_a_lost_connection_while_reading_the_invoice_deletes_nothing(manifest, mock_gorelo, run_it):
    record_invoice(manifest)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", httpx.ConnectError("boom"))
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}")] and not report.ok
    assert "could not be read first (no answer from Gorelo (ConnectError)" in manifest.record("invoice", INVOICE).outcome


async def test_a_delete_that_answers_void_is_a_leftover_for_the_user_not_a_cleaned_invoice(manifest, mock_gorelo, run_it):
    # delete_invoice passes a StatusId 4 on (the invoice was Approved by then and got voided): the cleanup must not call that deleted
    record_invoice(manifest)
    route_draft(mock_gorelo, delete=envelope({"Id": INVOICE, "StatusId": 4}))
    report = await run_it()
    assert not report.ok and not manifest.record("invoice", INVOICE).cleaned
    assert manifest.record("invoice", INVOICE).outcome == (
        "the delete of invoice INV-1042 answered status 4, not 6 (Deleted): check it in Gorelo"
    )


@pytest.mark.parametrize("status", [5, 3, 1, 99])
async def test_a_delete_that_answers_any_other_status_is_a_leftover_whoever_notices_it(manifest, mock_gorelo, run_it, status):
    # the tool may refuse such an answer itself (an unexpected response) or pass it on: either way nothing is called cleaned
    record_invoice(manifest)
    route_draft(mock_gorelo, delete=envelope({"Id": INVOICE, "StatusId": status}))
    report = await run_it()
    outcome = manifest.record("invoice", INVOICE).outcome
    assert not report.ok and not manifest.record("invoice", INVOICE).cleaned
    assert f"status {status}, not 6" in outcome or f"StatusId is {status}" in outcome, outcome


@pytest.mark.parametrize(
    "answer",
    [{"Id": INVOICE}, {"Id": INVOICE, "StatusId": True}, {"Id": INVOICE, "StatusId": "6"}, {"Id": INVOICE, "StatusId": None}],
)
async def test_a_delete_answer_without_a_real_status_6_is_not_trusted_by_the_cleanup(manifest, mock_gorelo, run_it, answer):
    # the raw backstop has no tool in front of it: the cleanup judges the answer itself
    record_invoice(manifest, number=None)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Number=None)))
    mock_gorelo.on("DELETE", f"/v1/invoices/{INVOICE}", envelope(answer))
    report = await run_it()
    assert not report.ok and not manifest.record("invoice", INVOICE).cleaned
    assert manifest.record("invoice", INVOICE).outcome == (
        f"the delete of invoice INV-1042 answered status {answer.get('StatusId')!r}, not 6 (Deleted): check it in Gorelo"
    )


@pytest.mark.parametrize(
    "answer",
    [{"Id": INVOICE}, {"Id": INVOICE, "StatusId": True}, {"Id": INVOICE, "StatusId": "6"}, {"Id": INVOICE, "StatusId": None}],
)
async def test_a_delete_answer_without_a_real_status_6_is_never_a_cleaned_invoice_through_the_tool_either(
    manifest, mock_gorelo, run_it, answer
):
    record_invoice(manifest)
    route_draft(mock_gorelo, delete=envelope(answer))
    report = await run_it()
    assert not report.ok and not manifest.record("invoice", INVOICE).cleaned


async def test_the_raw_backstop_checks_the_answer_of_the_delete_too(manifest, mock_gorelo, run_it):
    record_invoice(manifest, number=None)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Number=None)))
    mock_gorelo.on("DELETE", f"/v1/invoices/{INVOICE}", envelope({"Id": INVOICE, "StatusId": 4}))
    report = await run_it()
    assert not report.ok and "answered status 4, not 6 (Deleted)" in manifest.record("invoice", INVOICE).outcome


async def test_an_invoice_that_disappears_between_the_read_and_the_delete_counts_as_gone(manifest, mock_gorelo, run_it):
    record_invoice(manifest)
    route_draft(mock_gorelo, delete=GONE)
    report = await run_it()
    assert report.ok and manifest.record("invoice", INVOICE).outcome == "already gone (HTTP 404)"


async def test_a_delete_gorelo_refuses_is_a_leftover_with_its_message(manifest, mock_gorelo, run_it):
    record_invoice(manifest)
    route_draft(mock_gorelo, delete=error_envelope(409, [("070901", "The invoice is used elsewhere.")]))
    report = await run_it()
    assert not report.ok
    assert manifest.record("invoice", INVOICE).outcome.startswith("Gorelo rejected delete_invoice (HTTP 409, code 070901)")
    assert "The invoice is used elsewhere." in manifest.record("invoice", INVOICE).outcome


@pytest.mark.parametrize(
    "rows, said",
    [
        ([lookup_row(Status={"Id": 5, "Name": "Approved"})], "expected_status: invoice 1042 has status Approved"),
        ([lookup_row(), lookup_row(Id=uid(22))], "invoice_number: 1042 matches more than one invoice"),
    ],
)
async def test_when_delete_invoice_refuses_for_any_other_reason_nothing_is_deleted_and_the_invoice_stays_a_leftover(
    manifest, mock_gorelo, run_it, rows, said
):
    # only "the lookup found nothing" is answered with the raw DELETE (see below): a lookup that contradicts the first read
    # (another status, several invoices) is left for the user
    record_invoice(manifest)
    route_draft(mock_gorelo, rows=rows)
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}"), ("GET", "/v1/invoices")]  # no DELETE, no second try
    assert not report.ok and said in manifest.record("invoice", INVOICE).outcome


async def test_a_lookup_by_number_that_fails_leaves_the_invoice_a_leftover_without_a_second_delete(manifest, mock_gorelo, run_it):
    # the lookup did not answer: that is not "found nothing", so there is no raw DELETE either
    record_invoice(manifest)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft()))
    mock_gorelo.on("GET", "/v1/invoices", error_envelope(500, [("070500", "boom")]), query={"Number": str(INVOICE_NUMBER)})
    mock_gorelo.on("DELETE", f"/v1/invoices/{INVOICE}", envelope({"Id": INVOICE, "StatusId": 6}))  # would work: must not be asked
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}"), ("GET", "/v1/invoices")]
    outcome = manifest.record("invoice", INVOICE).outcome
    assert not report.ok and "HTTP 500" in outcome and "boom" in outcome
    assert "raw DELETE" not in outcome


# the lookup by Number finds nothing (the search lags behind the create): delete_invoice refuses BEFORE any DELETE, and the
# cleanup, which has just read the invoice by its id as a Draft, removes it with the guarded raw DELETE

FALLBACK = "Deleted (Draft, raw DELETE after the lookup by Number found nothing)"
FALLBACK_CONTEXT = "delete_invoice found no invoice by its Number, so the raw DELETE was tried: "


async def test_the_phrase_the_cleanup_looks_for_is_the_one_the_tool_refuses_with(mock_gorelo, server_factory):
    route_draft(mock_gorelo, rows=[])
    error = await call_tool_error(
        server_factory(destructive=True),
        "delete_invoice",
        {"invoice_number": INVOICE_NUMBER, "expected_status": "Draft", "confirm": True},
    )
    assert cleanup.NO_SUCH_NUMBER == "no invoice has the number" and cleanup.NO_SUCH_NUMBER in error
    assert "(HTTP" not in error  # a refusal of the tool itself: no status of Gorelo's, and no DELETE was sent
    assert [(r.method, r.path) for r in mock_gorelo.requests] == [("GET", "/v1/invoices")]


async def test_when_the_lookup_by_number_finds_nothing_the_draft_it_just_read_is_deleted_by_its_id(manifest, mock_gorelo, run_it, lines):
    record_invoice(manifest)
    route_draft(mock_gorelo, rows=[])
    report = await run_it()
    assert sent(mock_gorelo) == [
        ("GET", f"/v1/invoices/{INVOICE}"),  # the first read: a Draft
        ("GET", "/v1/invoices"),  # delete_invoice's lookup by Number finds nothing and refuses: nothing was sent
        ("DELETE", f"/v1/invoices/{INVOICE}"),  # the raw backstop, for the id that was just read
    ]
    assert mock_gorelo.requests[1].query["Number"] == "1042"
    assert report.ok and manifest.leftovers() == [] and manifest.record("invoice", INVOICE).outcome == FALLBACK
    assert lines[0] == f"cleanup of {RUN}: 1 cleaned, 0 left over"
    assert f"  cleaned    invoice {INVOICE} {manifest.label('invoice')} ({FALLBACK})" in lines
    assert lines[-1] == "result: nothing left over"


async def test_the_fallback_delete_goes_through_the_cleanup_guard_like_every_other_request(
    manifest, mock_gorelo, make_settings, monkeypatch
):
    record_invoice(manifest)
    route_draft(mock_gorelo, rows=[])
    made = []

    class Spy(LiveGuard):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.seen = []
            made.append(self)

        def check(self, request):
            self.seen.append((request.method, request.url.path))
            super().check(request)

    monkeypatch.setattr(cleanup, "LiveGuard", Spy)
    await cleanup.run_cleanup(manifest, settings=make_settings(destructive=True), transport=mock_gorelo.transport, pace=0, echo=lambda line: None)
    assert len(made) == 1 and made[0].seen == sent(mock_gorelo) and not made[0].tripped
    assert made[0].seen[-1] == ("DELETE", f"/v1/invoices/{INVOICE}") and made[0].cleanup is True


@pytest.mark.parametrize(
    "details, said",
    [
        ({"status_id": 5, "number": INVOICE_NUMBER, "display_number": "INV-1042"}, "status_id is 5, not 1"),  # recorded as Approved
        ({"status_id": None, "number": INVOICE_NUMBER, "display_number": "INV-1042"}, "status_id is null, not 1"),
        ({}, "status_id is missing, not 1"),
    ],
)
async def test_the_fallback_delete_is_refused_by_the_guard_for_an_invoice_the_manifest_does_not_know_as_a_draft(
    manifest, mock_gorelo, run_it, details, said
):
    manifest.created("invoice", INVOICE, manifest.label("invoice"), details)
    route_draft(mock_gorelo, rows=[])  # the read says Draft, but the record does not
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}"), ("GET", "/v1/invoices")]  # the DELETE never left
    outcome = manifest.record("invoice", INVOICE).outcome
    assert not report.ok and not manifest.record("invoice", INVOICE).cleaned
    assert outcome.startswith(FALLBACK_CONTEXT + "the guard refused it: blocked DELETE /invoices/") and said in outcome


@pytest.mark.parametrize(
    "answer",
    [
        {"Id": INVOICE, "StatusId": 4},  # voided: the invoice was Approved by then
        {"Id": INVOICE, "StatusId": 1},
        {"Id": INVOICE},
        {"Id": INVOICE, "StatusId": True},
        {"Id": INVOICE, "StatusId": "6"},
        {"Id": INVOICE, "StatusId": None},
    ],
)
async def test_the_fallback_delete_must_answer_status_6_like_every_other_delete(manifest, mock_gorelo, run_it, answer):
    record_invoice(manifest)
    route_draft(mock_gorelo, rows=[], delete=envelope(answer))
    report = await run_it()
    assert [m for m in sent(mock_gorelo) if m[0] == "DELETE"] == [("DELETE", f"/v1/invoices/{INVOICE}")]
    assert not report.ok and not manifest.record("invoice", INVOICE).cleaned
    assert manifest.record("invoice", INVOICE).outcome == (
        f"the delete of invoice INV-1042 answered status {answer.get('StatusId')!r}, not 6 (Deleted): check it in Gorelo"
    )


async def test_the_fallback_delete_that_finds_the_invoice_gone_counts_as_gone(manifest, mock_gorelo, run_it):
    record_invoice(manifest)
    route_draft(mock_gorelo, rows=[], delete=GONE)
    report = await run_it()
    assert report.ok and manifest.record("invoice", INVOICE).outcome == "already gone (HTTP 404)"


async def test_the_fallback_delete_that_gorelo_refuses_is_a_leftover_with_its_message_and_is_not_repeated(manifest, mock_gorelo, run_it):
    record_invoice(manifest)
    route_draft(mock_gorelo, rows=[], delete=error_envelope(409, [("070901", "The invoice is used elsewhere.")]))
    report = await run_it()
    assert [m for m in sent(mock_gorelo) if m[0] == "DELETE"] == [("DELETE", f"/v1/invoices/{INVOICE}")]  # once
    assert not report.ok and not manifest.record("invoice", INVOICE).cleaned
    assert manifest.record("invoice", INVOICE).outcome == FALLBACK_CONTEXT + "HTTP 409: The invoice is used elsewhere."


async def test_a_lost_connection_during_the_fallback_delete_is_a_leftover_to_run_again(manifest, mock_gorelo, run_it):
    record_invoice(manifest)
    route_draft(mock_gorelo, rows=[], delete=httpx.ConnectError("boom"))
    report = await run_it()
    assert not report.ok and not manifest.record("invoice", INVOICE).cleaned
    assert "no answer from Gorelo (ConnectError)" in manifest.record("invoice", INVOICE).outcome


async def test_an_error_that_carries_an_http_status_is_never_mistaken_for_the_refusal_before_any_delete(manifest, mock_gorelo, run_it):
    # Gorelo's own 409 to the tool's DELETE quotes the refusal's words: it has an HTTP status, so the DELETE WAS sent and the
    # cleanup must not send a second one
    record_invoice(manifest)
    route_draft(mock_gorelo, delete=error_envelope(409, [("070901", "no invoice has the number 1042")]))
    report = await run_it()
    assert [m for m in sent(mock_gorelo) if m[0] == "DELETE"] == [("DELETE", f"/v1/invoices/{INVOICE}")]
    assert not report.ok and "HTTP 409" in manifest.record("invoice", INVOICE).outcome
    assert not manifest.record("invoice", INVOICE).outcome.startswith(FALLBACK_CONTEXT)


async def test_an_invoice_found_by_the_label_search_whose_number_lookup_lags_is_deleted_by_the_fallback_too(manifest, mock_gorelo, run_it):
    label = announced(manifest)
    mock_gorelo.on("GET", "/v1/invoices", paged_envelope([found(label)]), query={"Query": label})
    route_draft(mock_gorelo, rows=[])
    report = await run_it()
    assert sent(mock_gorelo) == [
        ("GET", "/v1/invoices"),  # the search by label
        ("GET", f"/v1/invoices/{INVOICE}"),
        ("GET", "/v1/invoices"),  # the lookup by Number finds nothing
        ("DELETE", f"/v1/invoices/{INVOICE}"),
    ]
    assert report.ok and manifest.record("invoice", INVOICE).outcome == FALLBACK and manifest.unresolved_intents() == []


async def test_a_second_cleanup_after_the_fallback_delete_sends_nothing(manifest, mock_gorelo, run_it):
    record_invoice(manifest)
    route_draft(mock_gorelo, rows=[])
    await run_it()
    first = len(mock_gorelo.requests)
    report = await run_it()
    assert len(mock_gorelo.requests) == first == 3 and report.ok


async def test_an_invoice_the_manifest_does_not_know_as_a_draft_is_refused_by_the_guard_and_never_deleted(manifest, mock_gorelo, run_it):
    manifest.created("invoice", INVOICE, manifest.label("invoice"), {})  # no status_id: the guard's DELETE rule refuses
    route_draft(mock_gorelo)
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}"), ("GET", "/v1/invoices")]  # the DELETE never left
    assert not report.ok
    outcome = manifest.record("invoice", INVOICE).outcome
    assert outcome.startswith("the guard refused it: blocked DELETE /invoices/") and "is not recorded as a Draft" in outcome


async def test_the_raw_backstop_is_refused_by_the_guard_for_an_invoice_recorded_with_another_status(manifest, mock_gorelo, run_it):
    manifest.created("invoice", INVOICE, manifest.label("invoice"), {"status_id": 5, "number": None, "display_number": None})
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Number=None)))  # the read says Draft, the record says 5
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}")] and not report.ok
    assert "status_id is 5, not 1" in manifest.record("invoice", INVOICE).outcome


async def test_cleanup_never_sends_the_pdf_export_or_a_post_for_an_invoice(manifest, mock_gorelo, run_it):
    record_invoice(manifest)
    route_draft(mock_gorelo)
    await run_it()
    assert {r.method for r in mock_gorelo.requests} == {"GET", "DELETE"}
    assert not any(r.path.endswith("/pdf") for r in mock_gorelo.requests)


# an Approved invoice (the write matrix's approved_invoice area made it): voided ONLY with --void-approved

APPROVED = {"Id": 5, "Name": "Approved"}
VOID = {"Id": 4, "Name": "Void"}
VOIDED = "(still listed as Void)"
# Gorelo's void does not reach the accounting system (seen with Xero), so the
# cleanup says twice that its own void is in Gorelo only. Both texts are spelled out here, not read from the module, so that
# a change of their words fails these tests.
GORELO_ONLY = "this voids it in Gorelo only: void its copy in the accounting system by hand too"  # next to the command
REMINDER = (  # the line of the report for each invoice this cleanup voided
    "the void is in Gorelo only: check the accounting system and void the invoice there too "
    "(Gorelo's void is not pushed to the connected accounting system, seen with Xero)"
)


def record_approved(manifest, **details):
    """The invoice as the matrix records the approved invoice the moment create_approved_invoice answered: status_id 5."""
    details = {"status_id": 5, "number": INVOICE_NUMBER, "display_number": "INV-1042", **details}
    return manifest.created("invoice", INVOICE, manifest.label("approved invoice"), details)


def route_approved(mock, *, read=None, rows=None, delete=None, ident=INVOICE, number=INVOICE_NUMBER):
    """The three routes of a void: the first read (Approved), delete_invoice's lookup by Number and the DELETE (StatusId 4)."""
    mock.on("GET", f"/v1/invoices/{ident}", envelope(draft(Id=ident, Status=APPROVED)) if read is None else read)
    mock.on(
        "GET",
        "/v1/invoices",
        paged_envelope([lookup_row(Id=ident, Status=APPROVED)] if rows is None else rows),
        query={"Number": str(number)},
    )
    mock.on("DELETE", f"/v1/invoices/{ident}", envelope({"Id": ident, "StatusId": 4}) if delete is None else delete)


async def test_an_approved_invoice_is_a_leftover_for_the_user_without_the_flag_and_is_not_voided(
    manifest, mock_gorelo, run_it, lines
):
    record_approved(manifest)
    route_approved(mock_gorelo)
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}")]  # read, found Approved, left alone: no lookup, no DELETE
    # the command is followed by what it does to the accounting system (nothing: it voids in Gorelo only)
    outcome = (
        "left for the user: invoice INV-1042 has status Approved (id 5); it was approved on create, so Gorelo pushed it "
        "to the connected accounting system: check it there, then void it with: "
        f"python -m scripts.live.cleanup {manifest.path} --void-approved ({GORELO_ONLY})"
    )
    record = manifest.record("invoice", INVOICE)
    assert record.outcome == outcome and not record.cleaned and manifest.details("invoice", INVOICE)["status_id"] == 5
    assert not report.ok and [r.id for r in report.leftovers] == [INVOICE] and report.undeletable == []
    assert f"  LEFTOVER   invoice {INVOICE} {manifest.label('approved invoice')} ({outcome})" in lines
    assert lines[0] == f"cleanup of {RUN}: 0 cleaned, 1 left over" and lines[-1] == "result: SOMETHING IS LEFT OVER"
    assert report.voided == [] and not any(line.startswith("reminder:") for line in lines)  # nothing was voided: nothing to remind of


async def test_with_the_flag_an_approved_invoice_is_read_looked_up_by_number_and_voided(manifest, mock_gorelo, run_it, lines):
    record_approved(manifest)
    route_approved(mock_gorelo)
    report = await run_it(void_approved=True)
    assert sent(mock_gorelo) == [
        ("GET", f"/v1/invoices/{INVOICE}"),  # read first: it must still be Approved
        ("GET", "/v1/invoices"),  # delete_invoice finds the invoice by its Number (expected status Approved)
        ("DELETE", f"/v1/invoices/{INVOICE}"),
    ]
    assert mock_gorelo.requests[1].query["Number"] == "1042" and mock_gorelo.requests[2].json is None
    record = manifest.record("invoice", INVOICE)
    outcome = f"voided by the cleanup with delete_invoice {VOIDED}"
    # a voided invoice cannot be removed: it stays a record that is not cleaned, reported apart as a known residue
    assert record.outcome == outcome and not record.cleaned
    assert manifest.details("invoice", INVOICE) == {"status_id": 4, "number": 1042, "display_number": "INV-1042"}
    assert report.ok and report.cleaned == [] and report.leftovers == [] and [r.id for r in report.undeletable] == [INVOICE]
    assert lines[0] == f"cleanup of {RUN}: 0 cleaned, 0 left over, 1 known undeletable"
    assert f"  known undeletable invoice {INVOICE} {manifest.label('approved invoice')}: {outcome}" in lines
    assert "result: nothing left over" not in lines  # an invoice stays in Gorelo, as Void: never claimed to be gone
    assert lines[-1] == "result: cleanup complete; 1 known undeletable record remains (see above)"


async def test_the_void_goes_through_a_cleanup_guard_that_was_told_about_the_flag_and_only_then(
    manifest, mock_gorelo, make_settings, monkeypatch
):
    record_approved(manifest)
    route_approved(mock_gorelo)
    made = []

    class Spy(LiveGuard):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.seen = []
            made.append(self)

        def check(self, request):
            self.seen.append((request.method, request.url.path))
            super().check(request)

    monkeypatch.setattr(cleanup, "LiveGuard", Spy)
    settings = make_settings(destructive=True)
    await cleanup.run_cleanup(manifest, settings=settings, transport=mock_gorelo.transport, pace=0, echo=lambda line: None)
    assert len(made) == 1 and made[0].cleanup is True and made[0].allow_approved_invoice is False  # the old guard
    assert made[0].seen == [("GET", f"/v1/invoices/{INVOICE}")]  # nothing else left the process
    await cleanup.run_cleanup(
        manifest, void_approved=True, settings=settings, transport=mock_gorelo.transport, pace=0, echo=lambda line: None
    )
    assert len(made) == 2 and made[1].cleanup is True and made[1].allow_approved_invoice is True
    assert made[1].seen == sent(mock_gorelo)[1:] and not made[1].tripped
    assert made[1].seen[-1] == ("DELETE", f"/v1/invoices/{INVOICE}")


async def test_a_second_cleanup_after_a_void_sends_nothing_not_even_a_read(manifest, mock_gorelo, run_it):
    record_approved(manifest)
    route_approved(mock_gorelo)
    await run_it(void_approved=True)
    first = len(mock_gorelo.requests)
    for flag in (True, False):
        report = await run_it(void_approved=flag)
        assert report.ok and [r.id for r in report.undeletable] == [INVOICE]
    assert len(mock_gorelo.requests) == first == 3


@pytest.mark.parametrize("number", [DROP, None, 0, -3, True, "1042", 1042.0])
async def test_an_approved_invoice_without_a_usable_number_is_voided_by_the_raw_backstop_with_the_flag(
    manifest, mock_gorelo, run_it, number
):
    record_approved(manifest, number=None)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Number=number, Status=APPROVED)))
    mock_gorelo.on("DELETE", f"/v1/invoices/{INVOICE}", envelope({"Id": INVOICE, "StatusId": 4}))
    report = await run_it(void_approved=True)
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}"), ("DELETE", f"/v1/invoices/{INVOICE}")]  # no lookup by Number
    assert report.ok and manifest.record("invoice", INVOICE).outcome == f"voided by the cleanup with raw DELETE {VOIDED}"


async def test_when_the_lookup_by_number_finds_nothing_the_approved_invoice_it_just_read_is_voided_by_its_id(
    manifest, mock_gorelo, run_it
):
    record_approved(manifest)
    route_approved(mock_gorelo, rows=[])
    report = await run_it(void_approved=True)
    assert sent(mock_gorelo) == [
        ("GET", f"/v1/invoices/{INVOICE}"),
        ("GET", "/v1/invoices"),  # delete_invoice's lookup finds nothing and refuses: nothing was sent
        ("DELETE", f"/v1/invoices/{INVOICE}"),  # the raw backstop, for the id that was just read as Approved
    ]
    outcome = f"voided by the cleanup with raw DELETE after the lookup by Number found nothing {VOIDED}"
    assert report.ok and manifest.record("invoice", INVOICE).outcome == outcome


@pytest.mark.parametrize(
    "answer",
    [
        {"Id": INVOICE},
        {"Id": INVOICE, "StatusId": None},
        {"Id": INVOICE, "StatusId": True},
        {"Id": INVOICE, "StatusId": "4"},
        {"Id": INVOICE, "StatusId": 4.0},
        {"Id": INVOICE, "StatusId": 5},
        {"Id": INVOICE, "StatusId": 6},
        {"Id": INVOICE, "StatusId": 1},
    ],
)
async def test_a_void_answer_without_a_real_status_4_is_a_leftover_for_the_user(manifest, mock_gorelo, run_it, answer):
    # the raw backstop has no tool in front of it: the cleanup judges the answer itself
    record_approved(manifest, number=None)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Number=None, Status=APPROVED)))
    mock_gorelo.on("DELETE", f"/v1/invoices/{INVOICE}", envelope(answer))
    report = await run_it(void_approved=True)
    record = manifest.record("invoice", INVOICE)
    assert not report.ok and not record.cleaned and report.undeletable == [] and manifest.details("invoice", INVOICE)["status_id"] == 5
    assert record.outcome == (
        f"the void of invoice INV-1042 answered status {answer.get('StatusId')!r}, not 4 (Void): check it in Gorelo"
    )


async def test_a_void_that_delete_invoice_reports_as_a_deletion_is_not_trusted_either(manifest, mock_gorelo, run_it):
    # the tool passes a StatusId 6 on (a Draft was deleted): the invoice was not what the cleanup read, so it needs the user
    record_approved(manifest)
    route_approved(mock_gorelo, delete=envelope({"Id": INVOICE, "StatusId": 6}))
    report = await run_it(void_approved=True)
    assert not report.ok and not manifest.record("invoice", INVOICE).cleaned
    assert manifest.record("invoice", INVOICE).outcome == "the void of invoice INV-1042 answered status 6, not 4 (Void): check it in Gorelo"


@pytest.mark.parametrize(
    "rows, said",
    [
        ([lookup_row(Status={"Id": 1, "Name": "Draft"})], "expected_status: invoice 1042 has status Draft"),
        ([lookup_row(Status=APPROVED), lookup_row(Id=uid(22), Status=APPROVED)], "invoice_number: 1042 matches more than one invoice"),
    ],
)
async def test_when_delete_invoice_refuses_to_void_nothing_is_deleted_and_the_invoice_stays_a_leftover(
    manifest, mock_gorelo, run_it, rows, said
):
    record_approved(manifest)
    route_approved(mock_gorelo, rows=rows)
    report = await run_it(void_approved=True)
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}"), ("GET", "/v1/invoices")]  # no DELETE, no second try
    assert not report.ok and said in manifest.record("invoice", INVOICE).outcome and report.undeletable == []


async def test_a_lookup_by_number_that_fails_while_voiding_leaves_the_invoice_alone(manifest, mock_gorelo, run_it):
    record_approved(manifest)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Status=APPROVED)))
    mock_gorelo.on("GET", "/v1/invoices", error_envelope(500, [("070500", "boom")]), query={"Number": str(INVOICE_NUMBER)})
    mock_gorelo.on("DELETE", f"/v1/invoices/{INVOICE}", envelope({"Id": INVOICE, "StatusId": 4}))  # would work: must not be asked
    report = await run_it(void_approved=True)
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}"), ("GET", "/v1/invoices")]
    outcome = manifest.record("invoice", INVOICE).outcome
    assert not report.ok and "HTTP 500" in outcome and "boom" in outcome and "raw DELETE" not in outcome


async def test_a_void_gorelo_refuses_is_a_leftover_with_its_message_and_is_not_repeated(manifest, mock_gorelo, run_it):
    record_approved(manifest)
    route_approved(mock_gorelo, delete=error_envelope(409, [("070901", "The invoice is used elsewhere.")]))
    report = await run_it(void_approved=True)
    assert [m for m in sent(mock_gorelo) if m[0] == "DELETE"] == [("DELETE", f"/v1/invoices/{INVOICE}")]  # once
    assert not report.ok and not manifest.record("invoice", INVOICE).cleaned
    assert manifest.record("invoice", INVOICE).outcome.startswith("Gorelo rejected delete_invoice (HTTP 409, code 070901)")


async def test_an_approved_invoice_that_is_gone_is_cleaned_with_or_without_the_flag(manifest, mock_gorelo, run_it):
    record_approved(manifest)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", GONE)
    report = await run_it(void_approved=True)
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}")]
    assert report.ok and manifest.record("invoice", INVOICE).outcome == "already gone (HTTP 404)"


async def test_an_approved_invoice_that_disappears_between_the_read_and_the_void_counts_as_gone(manifest, mock_gorelo, run_it):
    record_approved(manifest)
    route_approved(mock_gorelo, delete=GONE)
    report = await run_it(void_approved=True)
    assert report.ok and manifest.record("invoice", INVOICE).outcome == "already gone (HTTP 404)"


@pytest.mark.parametrize("details", [{"status_id": 1}, {"status_id": None}, {}, {"status_id": "5"}, {"status_id": True}, {"status_id": 5.0}])
async def test_an_invoice_the_run_did_not_record_as_approved_is_never_voided_even_with_the_flag(
    manifest, mock_gorelo, run_it, lines, details
):
    # for example a Draft that somebody approved afterwards: the harness did not approve it, so it is not the harness's to void
    manifest.created("invoice", INVOICE, manifest.label("invoice"), {"number": INVOICE_NUMBER, "display_number": "INV-1042", **details})
    route_approved(mock_gorelo)
    report = await run_it(void_approved=True)
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}")]  # read only: nothing deleted or voided
    outcome = f"left for the user: invoice INV-1042 has status Approved (id 5); {GENERIC_TAIL}"
    assert manifest.record("invoice", INVOICE).outcome == outcome and not report.ok
    assert "--void-approved" in outcome and f"python -m scripts.live.cleanup {manifest.path}" not in outcome


async def test_the_guard_refuses_a_void_of_an_invoice_whose_record_changed_under_the_cleanup(manifest, mock_gorelo, run_it, monkeypatch):
    # defence in depth: the guard reads the manifest too, so a record that is not Approved cannot be voided even if the cleanup tried
    record_approved(manifest)
    route_approved(mock_gorelo)

    class Refusing(LiveGuard):
        def check(self, request):
            if request.method == "DELETE":
                manifest.update_details("invoice", INVOICE, status_id=3)  # the record stops saying Approved
            super().check(request)

    monkeypatch.setattr(cleanup, "LiveGuard", Refusing)
    report = await run_it(void_approved=True)
    assert [m for m in sent(mock_gorelo) if m[0] == "DELETE"] == []  # the DELETE never left
    outcome = manifest.record("invoice", INVOICE).outcome
    assert not report.ok and outcome.startswith("the guard refused it: blocked DELETE /invoices/") and "status_id is 3" in outcome


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("details", [{"status_id": 1}, {"status_id": 5}, {}])
async def test_a_void_invoice_is_a_known_residue_whatever_the_flag_and_nothing_more_is_sent(
    manifest, mock_gorelo, run_it, lines, flag, details
):
    manifest.created("invoice", INVOICE, manifest.label("invoice"), {"number": INVOICE_NUMBER, "display_number": "INV-1042", **details})
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Status=VOID)))
    report = await run_it(void_approved=flag)
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}")]  # already void: nothing is sent
    outcome = f"already void, a known residue: a voided invoice cannot be removed {VOIDED}"
    assert manifest.record("invoice", INVOICE).outcome == outcome and not manifest.record("invoice", INVOICE).cleaned
    assert manifest.details("invoice", INVOICE)["status_id"] == 4  # so that the guard never allows another DELETE of it
    assert report.ok and report.leftovers == [] and [r.id for r in report.undeletable] == [INVOICE]
    assert f"  known undeletable invoice {INVOICE} {manifest.label('invoice')}: {outcome}" in lines


async def test_an_invoice_the_matrix_voided_is_a_known_residue_and_the_cleanup_sends_nothing(manifest, mock_gorelo, run_it, lines):
    record_approved(manifest, status_id=4)
    manifest.cleanup_failed("invoice", INVOICE, f"voided by the write matrix {VOIDED}")
    report = await run_it()
    assert mock_gorelo.requests == []
    assert report.ok and report.cleaned == [] and report.leftovers == [] and [r.id for r in report.undeletable] == [INVOICE]
    assert f"  known undeletable invoice {INVOICE} {manifest.label('approved invoice')}: voided by the write matrix {VOIDED}" in lines
    assert lines[-1] == "result: cleanup complete; 1 known undeletable record remains (see above)"


async def test_an_approved_invoice_that_reads_back_as_deleted_counts_as_gone(manifest, mock_gorelo, run_it):
    record_approved(manifest)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Status={"Id": 6, "Name": "Deleted"})))
    report = await run_it(void_approved=True)
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}")]
    assert report.ok and manifest.record("invoice", INVOICE).outcome == "already deleted (status 6, Deleted)"


async def test_an_approved_invoice_that_cannot_be_read_is_never_voided(manifest, mock_gorelo, run_it):
    record_approved(manifest)
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", error_envelope(500, [("070500", "boom")]))
    report = await run_it(void_approved=True)
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}")] and not report.ok
    assert manifest.record("invoice", INVOICE).outcome == "could not be read first (HTTP 500: boom); nothing was deleted"


async def test_a_draft_is_still_deleted_with_the_flag_and_nothing_else_changes(manifest, mock_gorelo, run_it):
    record_invoice(manifest)
    route_draft(mock_gorelo)
    report = await run_it(void_approved=True)
    assert sent(mock_gorelo) == [("GET", f"/v1/invoices/{INVOICE}"), ("GET", "/v1/invoices"), ("DELETE", f"/v1/invoices/{INVOICE}")]
    assert report.ok and manifest.record("invoice", INVOICE).outcome == "Deleted (Draft, delete_invoice)"


async def test_cleanup_with_the_flag_never_sends_a_post_or_the_pdf_export(manifest, mock_gorelo, run_it):
    record_approved(manifest)
    route_approved(mock_gorelo)
    await run_it(void_approved=True)
    assert {r.method for r in mock_gorelo.requests} == {"GET", "DELETE"}
    assert not any(r.path.endswith("/pdf") for r in mock_gorelo.requests)


def test_only_an_invoice_with_the_void_outcome_counts_as_a_known_residue_besides_the_attachment():
    def invoice(outcome, kind="invoice"):
        return Record(
            seq=1, kind=kind, id=INVOICE, label=f"{RUN} i", details={}, created_at="x", cleaned=False, outcome=outcome, attempts=()
        )

    assert cleanup.VOID_RESIDUE == VOIDED
    for outcome in (f"voided by the write matrix {VOIDED}", f"voided by the cleanup with delete_invoice {VOIDED}", f"already void, a known residue: a voided invoice cannot be removed {VOIDED}"):
        assert cleanup.is_known_undeletable(invoice(outcome))
    for outcome in (None, "", "HTTP 409: blocked", "voided", f"{VOIDED} and then something else", UNDELETABLE):
        assert not cleanup.is_known_undeletable(invoice(outcome))
    assert not cleanup.is_known_undeletable(invoice(f"voided by the write matrix {VOIDED}", kind="ticket"))
    assert not cleanup.is_known_undeletable(invoice(f"voided by the write matrix {VOIDED}", kind="attachment"))


def test_the_report_text_with_a_voided_invoice_and_a_file_counts_both_as_known_undeletable():
    voided = Record(
        seq=2, kind="invoice", id=INVOICE, label=f"{RUN} approved invoice", details={"status_id": 4},
        created_at="x", cleaned=False, outcome=f"voided by the write matrix {VOIDED}", attempts=(),
    )
    report = cleanup.CleanupReport(run_id=RUN, undeletable=[undeletable_record(), voided])
    assert report.ok
    assert report.render().splitlines() == [
        f"cleanup of {RUN}: 0 cleaned, 0 left over, 2 known undeletable",
        f"  known undeletable attachment report.txt {RUN} attachment: {UNDELETABLE}",
        f"  known undeletable invoice {INVOICE} {RUN} approved invoice: voided by the write matrix {VOIDED}",
        "result: cleanup complete; 2 known undeletable records remain (see above)",
    ]


# the void of the cleanup is in Gorelo only, and the cleanup says so (its help and its leftover text are pinned above)


def test_the_two_void_sentences_are_defined_in_the_cleanup_with_the_agreed_words():
    assert cleanup.VOID_REMINDER == REMINDER
    assert cleanup.VOID_COMMAND_NOTE == GORELO_ONLY


def test_the_report_ends_with_a_reminder_for_each_invoice_this_cleanup_voided_just_before_the_result():
    outcome = f"voided by the cleanup with delete_invoice {VOIDED}"
    voided = Record(
        seq=2, kind="invoice", id=INVOICE, label=f"{RUN} approved invoice", details={"status_id": 4},
        created_at="x", cleaned=False, outcome=outcome, attempts=(),
    )
    report = cleanup.CleanupReport(run_id=RUN, undeletable=[voided], voided=["INV-1042", "1043"])
    assert report.ok
    assert report.render().splitlines() == [
        f"cleanup of {RUN}: 0 cleaned, 0 left over, 1 known undeletable",
        f"  known undeletable invoice {INVOICE} {RUN} approved invoice: {outcome}",
        f"reminder: approved invoice INV-1042: {REMINDER}",
        f"reminder: approved invoice 1043: {REMINDER}",
        "result: cleanup complete; 1 known undeletable record remains (see above)",
    ]
    # whatever else the cleanup found, the reminders are the last thing said before the result
    failing = cleanup.CleanupReport(run_id=RUN, leftovers=[undeletable_record("x", None)], voided=["INV-7"])
    assert failing.render().splitlines()[-2:] == [f"reminder: approved invoice INV-7: {REMINDER}", "result: SOMETHING IS LEFT OVER"]
    # a cleanup that voided nothing says nothing about a void
    assert not any(line.startswith("reminder:") for line in cleanup.CleanupReport(run_id=RUN).render().splitlines())


async def test_the_report_says_that_the_void_of_this_cleanup_is_in_gorelo_only(manifest, mock_gorelo, run_it, lines):
    record_approved(manifest)
    route_approved(mock_gorelo)
    report = await run_it(void_approved=True)
    assert report.ok and report.voided == ["INV-1042"]
    reminder = f"reminder: approved invoice INV-1042: {REMINDER}"
    assert lines.count(reminder) == 1
    # after the record of the invoice as a known residue, and the last thing said before the result
    assert lines[-3:] == [
        f"  known undeletable invoice {INVOICE} {manifest.label('approved invoice')}: "
        f"voided by the cleanup with delete_invoice {VOIDED}",
        reminder,
        "result: cleanup complete; 1 known undeletable record remains (see above)",
    ]


@pytest.mark.parametrize(
    "changes, name",
    [({}, "INV-1042"), ({"DisplayNumber": DROP}, "1042"), ({"DisplayNumber": "  ", "Number": DROP}, INVOICE)],
)
async def test_the_reminder_names_the_voided_invoice_by_its_display_number_else_its_number_else_its_id(
    manifest, mock_gorelo, run_it, lines, changes, name
):
    record_approved(manifest, number=None if "Number" in changes else INVOICE_NUMBER)  # no Number: the raw DELETE voids it
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Status=APPROVED, **changes)))
    rows = [lookup_row(Status=APPROVED, **changes)]
    mock_gorelo.on("GET", "/v1/invoices", paged_envelope(rows), query={"Number": str(INVOICE_NUMBER)})
    mock_gorelo.on("DELETE", f"/v1/invoices/{INVOICE}", envelope({"Id": INVOICE, "StatusId": 4}))
    report = await run_it(void_approved=True)
    assert report.ok and report.voided == [name]
    assert f"reminder: approved invoice {name}: {REMINDER}" in lines


async def test_the_report_reminds_about_every_invoice_this_cleanup_voided_in_the_order_it_voided_them(
    manifest, mock_gorelo, run_it, lines
):
    second = uid(22)
    record_approved(manifest)  # INV-1042
    manifest.created("invoice", second, manifest.label("second approved invoice"), {"status_id": 5, "number": 1043, "display_number": "INV-1043"})
    route_approved(mock_gorelo)
    route_approved(
        mock_gorelo,
        ident=second,
        number=1043,
        read=envelope(draft(Id=second, Number=1043, DisplayNumber="INV-1043", Status=APPROVED)),
        rows=[lookup_row(Id=second, Number=1043, DisplayNumber="INV-1043", Status=APPROVED)],
    )
    report = await run_it(void_approved=True)
    assert report.ok and report.voided == ["INV-1043", "INV-1042"]  # newest first, like the cleanup itself
    assert lines[-3:] == [
        f"reminder: approved invoice INV-1043: {REMINDER}",
        f"reminder: approved invoice INV-1042: {REMINDER}",
        "result: cleanup complete; 2 known undeletable records remain (see above)",
    ]


@pytest.mark.parametrize(
    "read, delete",
    [
        pytest.param(envelope(draft(Status=VOID)), None, id="found-void"),
        pytest.param(None, envelope({"Id": INVOICE, "StatusId": 6}), id="the-answer-says-deleted"),
        pytest.param(None, GONE, id="gone-between-the-read-and-the-void"),
        pytest.param(None, error_envelope(409, [("070901", "The invoice is used elsewhere.")]), id="gorelo-refuses"),
    ],
)
async def test_no_reminder_for_an_invoice_that_this_cleanup_did_not_void(manifest, mock_gorelo, run_it, lines, read, delete):
    record_approved(manifest)
    route_approved(mock_gorelo, read=read, delete=delete)
    report = await run_it(void_approved=True)
    assert report.voided == [] and not any(line.startswith("reminder:") for line in lines)


async def test_no_reminder_for_an_invoice_the_matrix_voided_or_an_earlier_cleanup_did(manifest, mock_gorelo, run_it, lines):
    # the matrix voided it (and said so itself, in its note and its summary): a known residue, sent nothing, no reminder here
    record_approved(manifest, status_id=4)
    manifest.cleanup_failed("invoice", INVOICE, f"voided by the write matrix {VOIDED}")
    report = await run_it(void_approved=True)
    assert mock_gorelo.requests == [] and report.voided == [] and [r.id for r in report.undeletable] == [INVOICE]
    assert not any(line.startswith("reminder:") for line in lines)


async def test_a_second_cleanup_after_a_void_has_no_reminder_to_give(manifest, mock_gorelo, run_it, lines):
    record_approved(manifest)
    route_approved(mock_gorelo)
    first = await run_it(void_approved=True)
    assert first.voided == ["INV-1042"] and f"reminder: approved invoice INV-1042: {REMINDER}" in lines
    lines.clear()
    second = await run_it(void_approved=True)  # the invoice is a known residue now: nothing is read, nothing is voided
    assert second.ok and second.voided == [] and not any(line.startswith("reminder:") for line in lines)


def test_the_texts_of_the_cleanup_say_that_its_void_is_in_gorelo_only():
    module = " ".join(cleanup.__doc__.split())
    assert (
        "its outcome says to check it there, gives this command with --void-approved and says that the command voids in "
        "Gorelo only (VOID_COMMAND_NOTE: its copy in the accounting system is voided by hand too)"
    ) in module
    assert (
        "Whatever this cleanup voids it voids in Gorelo only: the copy that Gorelo had pushed to the accounting system "
        "was not voided there (seen with Xero), so it is the operator's to void there."
    ) in module
    assert (
        "the report ends, just before its result line, with a reminder (VOID_REMINDER) for each invoice THIS cleanup voided"
    ) in module
    assert "gets no reminder from this one" in module
    assert (
        "the report names every invoice this call voided (`voided`) and says so for each (VOID_REMINDER)"
    ) in " ".join(cleanup.run_cleanup.__doc__.split())
    assert "`voided` names the invoices THIS cleanup voided" in " ".join(cleanup.CleanupReport.__doc__.split())


def test_the_texts_of_the_cleanup_say_that_it_voids_only_with_the_flag_and_name_both_create_tools():
    """The raw DELETE's comment, the label search and the module docstring once said that only a Draft is ever deleted
    and that only create_invoice is searched for. Gorelo's DELETE also voids, so the texts say what the cleanup does with
    each status: delete a Draft, void an Approved invoice recorded as 5 only with --void-approved, note a Void one as a known
    residue, and leave the rest for the user."""
    flat = " ".join(Path(cleanup.__file__).read_text(encoding="utf-8").split())
    assert "only a Draft is ever deleted" not in flat
    assert (
        "# Not in _RAW_PATHS: an invoice is read first. A Draft is deleted; an Approved invoice recorded as 5 is voided (the "
        "raw # DELETE voids it too) only with --void-approved; any other status is never deleted or voided."
    ) in flat
    searched = " ".join(cleanup._Cleaner._find_announced_invoices.__doc__.split())
    assert searched.startswith(
        "A create_invoice or create_approved_invoice that was announced and never recorded may still have made an invoice"
    )
    assert (
        "voids an Approved one (recorded as 5) only with --void-approved, notes a Void one as a known residue and leaves any "
        "other one (Paid, ...) for the user"
    ) in searched
    module = " ".join(cleanup.__doc__.split())
    assert "A create (create_invoice or create_approved_invoice) that was announced without an id" in module
    assert (
        "is voided by this cleanup ONLY with --void-approved (the area voids the invoice it approved itself, before any "
        "cleanup runs, and the cleanup the matrix runs afterwards never does)"
    ) in module
    assert "known undeletable record: an uploaded file or a voided invoice" in module
    assert "the write matrix never sets it" in " ".join(cleanup.run_cleanup.__doc__.split())  # void_approved: the user's flag


def test_the_package_docstring_names_the_void_flag_and_says_who_voids():
    import scripts.live as live

    text = " ".join(live.__doc__.split())
    assert (
        "cleanup.py deletes what a manifest says a run created (also the leftovers listed in site.local.toml [leftovers], with --leftovers; "
        "with --void-approved it also voids an Approved invoice the run recorded as Approved: without that flag a cleanup "
        "never voids, and only the write matrix's approved_invoice area voids the one invoice it approved itself)"
    ) in text
    assert "or: python -m scripts.live.cleanup <file> [--void-approved]" in text


# an invoice whose create was announced but never answered (a timeout, a lost answer)


def announced(manifest):
    label = manifest.label("invoice")
    manifest.intent("invoice", label, {"client_id": TEST_CLIENT, "status_id": 1})
    return label


def found(label, **changes):
    """A row of the label search: an invoice of the test client whose Reference is the run label (unless a change says otherwise)."""
    return lookup_row(**{"Reference": label, **changes})


async def test_an_announced_invoice_is_searched_by_the_run_label_and_a_draft_with_that_reference_is_deleted(
    manifest, mock_gorelo, run_it, lines
):
    label = announced(manifest)
    mock_gorelo.on("GET", "/v1/invoices", paged_envelope([found(label)]), query={"Query": label})
    route_draft(mock_gorelo)
    report = await run_it()
    search = mock_gorelo.requests[0]
    assert (search.method, search.path) == ("GET", "/v1/invoices")
    assert search.query["ClientIds"] == "9501" and search.query["Query"] == label and search.query["PageSize"] == "50"
    assert sent(mock_gorelo) == [
        ("GET", "/v1/invoices"),  # the search by label
        ("GET", f"/v1/invoices/{INVOICE}"),  # read first, like any invoice
        ("GET", "/v1/invoices"),  # delete_invoice's lookup by Number
        ("DELETE", f"/v1/invoices/{INVOICE}"),
    ]
    assert manifest.ids("invoice") == {INVOICE}
    assert manifest.details("invoice", INVOICE) == {"status_id": 1, "number": 1042, "display_number": "INV-1042"}
    assert manifest.record("invoice", INVOICE).label == label and manifest.record("invoice", INVOICE).cleaned
    assert manifest.unresolved_intents() == [] and report.ok and report.unresolved == []
    assert lines[-1] == "result: nothing left over"


async def test_an_announced_invoice_with_a_stranger_s_reference_or_client_is_never_touched(manifest, mock_gorelo, run_it, lines):
    label = announced(manifest)
    rows = [
        found(label, Id=uid(31), Reference="a customer's invoice"),
        found(label, Id=uid(32), Reference=f"{label} and more"),  # not exactly the label
        found(label, Id=uid(33), Reference=label.lower()),
        found(label, Id=uid(34), ClientId=9502),  # the label, but not on the test client
        found(label, Id=uid(35), ClientId=None),
        found(label, Id="not-a-guid"),
        found(label, Id=DROP),
        "not a row",
        found(label, Id=uid(36), Reference=DROP),
    ]
    mock_gorelo.on("GET", "/v1/invoices", paged_envelope(rows), query={"Query": label})
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", "/v1/invoices")]  # the search only: nothing is read, adopted or deleted
    assert manifest.ids("invoice") == set() and not report.ok
    assert [i.label for i in report.unresolved] == [label]
    assert any(line.startswith(f"  UNRESOLVED invoice {label}") for line in lines)
    assert lines[-1] == "result: SOMETHING IS LEFT OVER"


async def test_an_announced_invoice_that_is_not_a_draft_is_recorded_and_left_for_the_user(manifest, mock_gorelo, run_it, lines):
    label = announced(manifest)
    approved = found(label, Status={"Id": 5, "Name": "Approved"})
    mock_gorelo.on("GET", "/v1/invoices", paged_envelope([approved]), query={"Query": label})
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Reference=label, Status={"Id": 5, "Name": "Approved"})))
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", "/v1/invoices"), ("GET", f"/v1/invoices/{INVOICE}")]  # nothing deleted or voided
    assert manifest.details("invoice", INVOICE)["status_id"] == 5
    assert manifest.unresolved_intents() == [] and not report.ok  # recorded, but a leftover that needs the user
    assert [r.id for r in report.leftovers] == [INVOICE] and "invoice INV-1042 has status Approved (id 5)" in report.leftovers[0].outcome
    assert any(line.startswith(f"  LEFTOVER   invoice {INVOICE} {label} (left for the user: invoice INV-1042 has status Approved") for line in lines)


async def test_an_announced_invoice_that_is_not_a_draft_names_the_command_that_voids_it_when_it_is_approved(manifest, mock_gorelo, run_it):
    label = announced(manifest)
    mock_gorelo.on("GET", "/v1/invoices", paged_envelope([found(label, Status=APPROVED)]), query={"Query": label})
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Reference=label, Status=APPROVED)))
    report = await run_it()
    assert manifest.details("invoice", INVOICE)["status_id"] == 5  # the status the search found, whatever the intent said
    outcome = manifest.record("invoice", INVOICE).outcome
    assert outcome.endswith(f"python -m scripts.live.cleanup {manifest.path} --void-approved ({GORELO_ONLY})") and not report.ok
    assert sent(mock_gorelo) == [("GET", "/v1/invoices"), ("GET", f"/v1/invoices/{INVOICE}")]


async def test_an_announced_approved_invoice_is_found_by_its_label_and_voided_only_with_the_flag(manifest, mock_gorelo, run_it, lines):
    label = manifest.label("approved invoice")
    manifest.intent("invoice", label, {"status_id": 5})  # the create_approved_invoice whose answer was lost
    mock_gorelo.on("GET", "/v1/invoices", paged_envelope([found(label, Status=APPROVED)]), query={"Query": label})
    route_approved(mock_gorelo)
    report = await run_it(void_approved=True)
    assert sent(mock_gorelo) == [
        ("GET", "/v1/invoices"),  # the search by label
        ("GET", f"/v1/invoices/{INVOICE}"),
        ("GET", "/v1/invoices"),  # delete_invoice's lookup by Number
        ("DELETE", f"/v1/invoices/{INVOICE}"),
    ]
    assert manifest.record("invoice", INVOICE).label == label and manifest.unresolved_intents() == []
    assert manifest.record("invoice", INVOICE).outcome == f"voided by the cleanup with delete_invoice {VOIDED}"
    assert report.ok and [r.id for r in report.undeletable] == [INVOICE] and report.unresolved == []


@pytest.mark.parametrize(
    "status, outcome_start",
    [
        ({"Id": 3, "Name": "Paid"}, "left for the user: invoice INV-1042 has status Paid (id 3); Gorelo refuses to delete or void"),
        ({"Id": 4, "Name": "Void"}, "already void, a known residue"),
    ],
)
async def test_an_announced_invoice_found_as_paid_or_void_is_handled_by_its_status(manifest, mock_gorelo, run_it, status, outcome_start):
    label = announced(manifest)
    mock_gorelo.on("GET", "/v1/invoices", paged_envelope([found(label, Status=status)]), query={"Query": label})
    mock_gorelo.on("GET", f"/v1/invoices/{INVOICE}", envelope(draft(Reference=label, Status=status)))
    await run_it(void_approved=True)
    assert manifest.details("invoice", INVOICE)["status_id"] == status["Id"]  # what the search found (4 again once it is noted void)
    assert manifest.record("invoice", INVOICE).outcome.startswith(outcome_start)
    assert sent(mock_gorelo) == [("GET", "/v1/invoices"), ("GET", f"/v1/invoices/{INVOICE}")]  # nothing deleted or voided


async def test_an_announced_invoice_the_search_does_not_find_stays_announced_without_an_id(manifest, mock_gorelo, run_it, lines):
    label = announced(manifest)
    mock_gorelo.on("GET", "/v1/invoices", paged_envelope([]), query={"Query": label})
    report = await run_it()
    assert sent(mock_gorelo) == [("GET", "/v1/invoices")]
    assert not report.ok and [i.label for i in report.unresolved] == [label] and manifest.ids("invoice") == set()


@pytest.mark.parametrize(
    "answer",
    [
        error_envelope(500, [("070500", "boom")]),
        httpx.ConnectError("boom"),
        envelope({"not": "a list"}, {"HasMore": False}),
        paged_envelope([]),
    ],
)
async def test_a_search_that_fails_changes_nothing_and_the_create_stays_announced(manifest, mock_gorelo, run_it, answer):
    label = announced(manifest)
    mock_gorelo.on("GET", "/v1/invoices", answer, query={"Query": label})
    report = await run_it()
    assert not report.ok and [i.label for i in report.unresolved] == [label]
    assert [(m, p) for m, p in sent(mock_gorelo) if m != "GET"] == []


async def test_the_search_follows_the_cursor_to_a_second_page(manifest, mock_gorelo, run_it):
    label = announced(manifest)
    mock_gorelo.on(
        "GET",
        "/v1/invoices",
        in_order(
            paged_envelope([found(label, Id=uid(41), Reference="other")], next_cursor="c1", total_count=2),
            paged_envelope([found(label)], total_count=2),
        ),
        query={"Query": label},
    )
    route_draft(mock_gorelo)
    report = await run_it()
    searches = [r for r in mock_gorelo.requests if r.path == "/v1/invoices" and r.query.get("Query") == label]
    assert len(searches) == 2 and "Cursor" not in searches[0].query and searches[1].query["Cursor"] == "c1"
    assert report.ok and manifest.record("invoice", INVOICE).cleaned


async def test_the_search_stops_after_three_pages(manifest, mock_gorelo, run_it):
    label = announced(manifest)
    mock_gorelo.on(
        "GET",
        "/v1/invoices",
        lambda q: paged_envelope([found(label, Id=uid(50), Reference="other")], next_cursor="more", total_count=999),
        query={"Query": label},
    )
    report = await run_it()
    assert len(mock_gorelo.requests) == cleanup.INVOICE_SEARCH_PAGES == 3 and not report.ok


async def test_every_invoice_with_the_label_is_handled_a_draft_is_deleted_and_another_status_is_left(manifest, mock_gorelo, run_it):
    label = announced(manifest)
    second, third = uid(61), uid(62)
    rows = [found(label), found(label, Id=second, Number=1043, DisplayNumber="INV-1043", Status={"Id": 3, "Name": "Paid"}), found(label, Id=INVOICE)]
    mock_gorelo.on("GET", "/v1/invoices", paged_envelope(rows), query={"Query": label})
    route_draft(mock_gorelo)
    mock_gorelo.on("GET", f"/v1/invoices/{second}", envelope(draft(Id=second, Number=1043, DisplayNumber="INV-1043", Status={"Id": 3, "Name": "Paid"})))
    report = await run_it()
    assert manifest.ids("invoice") == {INVOICE, second}  # the duplicate row of the same invoice is recorded once
    assert manifest.record("invoice", INVOICE).cleaned and not manifest.record("invoice", second).cleaned
    assert [r.id for r in report.leftovers] == [second] and "status Paid (id 3)" in report.leftovers[0].outcome
    assert [(m, p) for m, p in sent(mock_gorelo) if m == "DELETE"] == [("DELETE", f"/v1/invoices/{INVOICE}")]
    assert manifest.unresolved_intents() == []


async def test_a_second_cleanup_does_not_search_again_once_the_invoice_is_recorded(manifest, mock_gorelo, run_it):
    label = announced(manifest)
    mock_gorelo.on("GET", "/v1/invoices", paged_envelope([found(label)]), query={"Query": label})
    route_draft(mock_gorelo)
    await run_it()
    first = len(mock_gorelo.requests)
    report = await run_it()
    assert len(mock_gorelo.requests) == first and report.ok


async def test_only_an_announced_invoice_is_searched_not_an_announced_ticket(manifest, mock_gorelo, run_it):
    manifest.intent("ticket", manifest.label("maybe there"))
    manifest.intent("invoice", manifest.label("settled"))
    manifest.intent_failed(manifest.unresolved_intents()[1].seq, "Gorelo answered 400")  # nothing was created
    report = await run_it()
    assert mock_gorelo.requests == [] and not report.ok  # the ticket is only reported; the settled invoice is no business


async def test_the_search_is_one_get_of_list_invoices_for_test_client(manifest, mock_gorelo, run_it):
    label = announced(manifest)
    mock_gorelo.on("GET", "/v1/invoices", paged_envelope([]), query={"Query": label})
    await run_it()
    (search,) = mock_gorelo.requests
    assert search.method == "GET" and search.json is None
    assert search.query["ClientIds"] == "9501" and search.query["Query"] == label and set(search.query) <= {"ClientIds", "Query", "PageSize", "SortOrder"}


# listed leftovers (--leftovers)

LEFTOVER_ROUTES = {
    "client9801": ("/v1/clients/9801", {"Id": 9801, "Name": "OLDTEST sample client"}),
    "client9802": ("/v1/clients/9802", {"Id": 9802, "Name": "Real Customer Ltd"}),
    "contact": ("/v1/contacts/9900", {"Id": 9900, "FirstName": "Sample", "LastName": "Leftover-0001"}),
}


def route_leftovers(mock, **overrides):
    for key, (path, data) in LEFTOVER_ROUTES.items():
        mock.on("GET", path, overrides.get(key, envelope(data)))
        mock.on("DELETE", path, envelope({"Id": data["Id"]}))


async def test_leftovers_are_not_touched_without_the_flag(manifest, mock_gorelo, run_it):
    route_leftovers(mock_gorelo)
    report = await run_it()
    assert mock_gorelo.requests == [] and report.approved == [] and manifest.approved_leftover_outcomes() == []


async def test_leftovers_are_read_first_and_deleted_only_when_the_name_matches(manifest, mock_gorelo, run_it, lines):
    manifest.created("contact", CONTACT, manifest.label("k"))
    route_leftovers(mock_gorelo)
    route_all(mock_gorelo)
    report = await run_it(leftovers=True)
    assert sent(mock_gorelo) == [
        ("GET", "/v1/clients/9801"),
        ("DELETE", "/v1/clients/9801"),
        ("GET", "/v1/clients/9802"),  # a real customer name: read, then left alone
        ("GET", "/v1/contacts/9900"),
        ("DELETE", "/v1/contacts/9900"),
        ("DELETE", f"/v1/contacts/{CONTACT}"),  # the run's own record comes after
    ]
    assert [(o["kind"], o["id"], o["deleted"]) for o in manifest.approved_leftover_outcomes()] == [
        ("client", 9801, True),
        ("client", 9802, False),
        ("contact", 9900, True),
    ]
    assert "does not match" in manifest.approved_leftover_outcomes()[1]["outcome"]
    assert report.ok  # a skipped leftover is reported, it does not fail the run
    text = "\n".join(lines)
    assert "listed leftover client 9801: Deleted" in text and "listed leftover client 9802: skipped" in text
    assert "OLDTEST" not in text and "Real Customer" not in text and "Leftover-" not in text  # names are never printed
    assert manifest.ids("client") == set() and manifest.ids("contact") == {CONTACT}  # they never join the run's ids


@pytest.mark.parametrize(
    "first, last, deleted",
    [
        ("Sample", "Leftover-1", True),
        ("Sample", "Leftover-", True),
        ("Sample", "leftover-1", False),
        ("Sample", "Smith", False),
        ("Sampler", "Leftover-1", False),
        ("sample", "Leftover-1", False),
        (None, "Leftover-1", False),
        ("Sample", None, False),
    ],
)
async def test_the_contact_leftover_needs_the_configured_names(manifest, mock_gorelo, run_it, first, last, deleted):
    route_leftovers(
        mock_gorelo,
        contact=envelope({"Id": 9900, "FirstName": first, "LastName": last}),
        client9801=error_envelope(404, [("1", "gone")]),
        client9802=error_envelope(404, [("1", "gone")]),
    )
    await run_it(leftovers=True)
    assert (("DELETE", "/v1/contacts/9900") in sent(mock_gorelo)) is deleted


@pytest.mark.parametrize(
    "name, deleted",
    [("OLDTEST", True), ("zz OLDTEST 2", True), ("OLDTEST-old", True), ("oldtest", False), ("OLD TEST", False), ("", False), (None, False)],
)
async def test_a_client_leftover_needs_the_configured_text_in_its_name(manifest, mock_gorelo, run_it, name, deleted):
    route_leftovers(
        mock_gorelo,
        client9801=envelope({"Id": 9801, "Name": name}),
        client9802=error_envelope(404, [("1", "gone")]),
        contact=error_envelope(404, [("1", "gone")]),
    )
    await run_it(leftovers=True)
    assert (("DELETE", "/v1/clients/9801") in sent(mock_gorelo)) is deleted


async def test_without_name_rules_every_listed_leftover_is_deleted(manifest, mock_gorelo, run_it, tmp_path, monkeypatch):
    path = tmp_path / "site.nonames.toml"
    path.write_text(SITE_TOML.replace("client_name_contains", "#a").replace("contact_first_name", "#b").replace("contact_last_name_prefix", "#c"), encoding="utf-8")
    monkeypatch.setenv("GORELO_SITE_CONFIG", str(path))
    route_leftovers(mock_gorelo)
    await run_it(leftovers=True)
    assert {("DELETE", "/v1/clients/9801"), ("DELETE", "/v1/clients/9802"), ("DELETE", "/v1/contacts/9900")} <= set(sent(mock_gorelo))


async def test_a_leftover_that_is_gone_or_unreadable_is_reported_and_skipped(manifest, mock_gorelo, run_it):
    route_leftovers(
        mock_gorelo,
        client9801=error_envelope(404, [("070404", "Client not found.")]),
        client9802=error_envelope(500, [("070001", "Internal error.")]),
        contact=envelope(["not", "an", "object"]),
    )
    report = await run_it(leftovers=True)
    assert [m for m in sent(mock_gorelo) if m[0] == "DELETE"] == []
    assert not report.ok and len(report.approved_problems) == 2  # the 500 and the non-record; the 404 is fine
    outcomes = {o["id"]: o["outcome"] for o in manifest.approved_leftover_outcomes()}
    assert outcomes[9801] == "already gone (HTTP 404)"
    assert outcomes[9802].startswith("skipped: it could not be read (HTTP 500")
    assert outcomes[9900] == "skipped: Gorelo did not return a record"


async def test_a_leftover_that_disappears_between_the_read_and_the_delete_is_reported_as_gone(manifest, mock_gorelo, run_it):
    mock_gorelo.on("GET", "/v1/clients/9801", envelope({"Id": 9801, "Name": "OLDTEST sample client"}))
    mock_gorelo.on("DELETE", "/v1/clients/9801", error_envelope(404, [("070404", "Client not found.")]))
    mock_gorelo.on("GET", "/v1/clients/9802", error_envelope(404, [("070404", "Client not found.")]))
    mock_gorelo.on("GET", "/v1/contacts/9900", error_envelope(404, [("070404", "Contact not found.")]))
    report = await run_it(leftovers=True)
    outcome = [o for o in manifest.approved_leftover_outcomes() if o["id"] == 9801][0]
    assert outcome["deleted"] is False and outcome["outcome"] == "already gone (HTTP 404)"
    assert report.ok


async def test_a_leftover_whose_delete_is_refused_is_reported(manifest, mock_gorelo, run_it):
    mock_gorelo.on("GET", "/v1/clients/9801", envelope({"Id": 9801, "Name": "OLDTEST sample client"}))
    mock_gorelo.on("DELETE", "/v1/clients/9801", error_envelope(409, [("070901", "The client has tickets.")]))
    mock_gorelo.on("GET", "/v1/clients/9802", error_envelope(404, [("070404", "Client not found.")]))
    mock_gorelo.on("GET", "/v1/contacts/9900", error_envelope(404, [("070404", "Contact not found.")]))
    report = await run_it(leftovers=True)
    outcome = [o for o in manifest.approved_leftover_outcomes() if o["id"] == 9801][0]
    assert outcome["deleted"] is False and outcome["outcome"] == "delete failed: HTTP 409: The client has tickets."
    assert not report.ok and [e["id"] for e in report.approved_problems] == [9801]


# command line


def make_run_file(tmp_path):
    path = tmp_path / "runs" / f"{RUN}.json"
    run = Manifest(RUN, path)
    run.created("contact", CONTACT, run.label("k"))
    return path


def test_the_command_line_reads_the_manifest_and_passes_the_flags(tmp_path, monkeypatch, capsys):
    path = make_run_file(tmp_path)
    calls = []

    async def fake_run_cleanup(manifest, *, leftovers=False, void_approved=False, **options):
        calls.append((manifest.run_id, leftovers, void_approved, options))
        return cleanup.CleanupReport(run_id=manifest.run_id)

    monkeypatch.setattr(cleanup, "run_cleanup", fake_run_cleanup)
    assert cleanup.main([str(path)]) == 0
    assert cleanup.main([str(path), "--leftovers"]) == 0
    assert cleanup.main([str(path), "--void-approved"]) == 0
    assert cleanup.main([str(path), "--leftovers", "--void-approved"]) == 0
    assert calls == [(RUN, False, False, {}), (RUN, True, False, {}), (RUN, False, True, {}), (RUN, True, True, {})]


def test_the_help_names_the_void_flag_and_what_it_does(capsys):
    with pytest.raises(SystemExit) as stop:
        cleanup.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())  # argparse wraps its lines
    assert stop.value.code == 0
    for text in ("--leftovers", "--void-approved", "void an Approved invoice that the run recorded as Approved",
                 "pushed to the connected accounting system, so check it there first", "stays listed as Void"):
        assert text in out
    # the help of the flag says that the void is in Gorelo only, right where it says to check the accounting system
    assert f"so check it there first ({GORELO_ONLY}); a voided invoice stays listed as Void" in out


def test_the_exit_status_is_zero_with_only_a_known_undeletable_file_and_one_with_a_leftover(tmp_path, monkeypatch):
    path = make_run_file(tmp_path)
    reports = []

    async def fake_run_cleanup(manifest, **options):
        return reports[-1]

    monkeypatch.setattr(cleanup, "run_cleanup", fake_run_cleanup)
    reports.append(cleanup.CleanupReport(run_id=RUN, undeletable=[undeletable_record()]))
    assert cleanup.main([str(path)]) == 0
    reports.append(cleanup.CleanupReport(run_id=RUN, undeletable=[undeletable_record()], leftovers=[undeletable_record("x", None)]))
    assert cleanup.main([str(path)]) == 1


def test_the_exit_status_is_one_when_something_is_left_over(tmp_path, monkeypatch):
    path = make_run_file(tmp_path)

    async def fake_run_cleanup(manifest, **options):
        return cleanup.CleanupReport(run_id=manifest.run_id, leftovers=manifest.leftovers())

    monkeypatch.setattr(cleanup, "run_cleanup", fake_run_cleanup)
    assert cleanup.main([str(path)]) == 1


def test_the_command_line_refuses_a_missing_or_invalid_manifest(tmp_path, capsys):
    assert cleanup.main([str(tmp_path / "missing.json")]) == 2
    assert "cannot open the manifest" in capsys.readouterr().err
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert cleanup.main([str(bad)]) == 2
    assert "not a valid run manifest" in capsys.readouterr().err


def test_the_command_line_reports_a_missing_api_key_without_a_traceback(tmp_path, monkeypatch, capsys):
    path = make_run_file(tmp_path)

    async def no_key(manifest, **options):
        raise _env.EnvError("GORELO_API_KEY is not set in somewhere")

    monkeypatch.setattr(cleanup, "run_cleanup", no_key)
    assert cleanup.main([str(path)]) == 2
    assert "cannot start: GORELO_API_KEY is not set" in capsys.readouterr().err


def test_the_command_line_needs_a_manifest_argument(capsys):
    with pytest.raises(SystemExit) as stop:
        cleanup.main([])
    assert stop.value.code == 2


async def test_without_settings_the_api_key_comes_from_live_settings(tmp_path, monkeypatch):
    # run_cleanup(settings=None) reads the key through _env.live_settings (replaced here: the real .env is never read)
    seen = []

    def fake_settings(*args, **kwargs):
        seen.append(True)
        raise _env.EnvError("stop here")

    monkeypatch.setattr(cleanup, "live_settings", fake_settings)
    run = Manifest(RUN, tmp_path / "r.json")
    with pytest.raises(_env.EnvError, match="stop here"):
        await cleanup.run_cleanup(run, pace=0)
    assert seen == [True]
