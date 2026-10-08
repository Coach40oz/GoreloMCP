"""scripts/watch_gorelo_changelog.py, offline: every path of the changelog check, the spec check, the alerts and
the exit codes, plus the systemd units in deploy/ that run it.

The watcher talks to three things: the changelog feed (feedback.gorelo.io), the live OpenAPI spec and Gorelo's
alert endpoint (api.usw.gorelo.io). Here all three are one httpx.MockTransport (the `world` fixture), so nothing
touches a socket (conftest also blocks them) and no alert is ever really posted. Each test runs the watcher
against a temp directory standing in for /opt/gorelo-mcp/app: its own spec/spec_index.json baseline and its own
.state/.

Decision table (what triggers an alert, and the exit code):

    nothing new                                           no alert  0
    first run / --reset: changelog baseline recorded      no alert  0
    new changelog entries, none API-related               no alert  2
    new API-related changelog entry                       1 alert per entry  1
    live contract hash != spec_index.json contract hash   1 alert, once per hash  1
    the same hash again, an entry alerted before          no alert  0
    an alert that Gorelo did not confirm                  not recorded, retried next run  1
    a fetch, parse, state or baseline failure             no alert  3 (an API change in the same run wins: 1)
"""

import configparser
import copy
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from conftest import envelope, error_envelope
from site_helper import site_config_env  # noqa: F401  (autouse: points GORELO_SITE_CONFIG at invented values)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import spec_snapshot  # noqa: E402
import watch_gorelo_changelog as watch  # noqa: E402

API_KEY = "test-api-key-must-never-be-printed-0123456789"
NOW = datetime(2026, 10, 9, 6, 30, tzinfo=timezone.utc)
CHANGELOG_PAGE = "https://feedback.gorelo.io/changelog"
ALERT_URL = "https://api.usw.gorelo.io/v1/alerts"


# --------------------------------------------------------------------------
# The outside world
# --------------------------------------------------------------------------


def small_spec():
    """A tiny OpenAPI document: enough for the contract hash, the index and the diff."""
    ok = {"200": {"description": "ok", "content": {"application/json": {"schema": {"type": "object"}}}}}
    return {
        "openapi": "3.0.1",
        "info": {"title": "Public API", "description": "docs", "version": "1.0.0"},
        "paths": {
            "/v1/clients": {
                "get": {
                    "summary": "List clients",
                    "parameters": [{"name": "Cursor", "in": "query", "description": "paging", "schema": {"type": "string"}}],
                    "responses": ok,
                }
            },
            "/v1/ping": {"get": {"summary": "Ping", "responses": ok}},
        },
        "components": {"schemas": {}},
    }


def dumps(document):
    return json.dumps(document).encode("utf-8")


def entry(key, title, content, created="2026-09-01T10:00:00Z"):
    return {"id": key, "title": title, "content": content, "createdAt": created}


def reply_with(spec, default):
    """What the world answers: the default, a canned httpx.Response, a function of the request, or an
    exception to raise (a network failure)."""
    if spec is None:
        return default()
    if isinstance(spec, BaseException):
        raise spec
    if callable(spec):
        return spec()
    return spec


class World:
    """The changelog feed, the live spec and Gorelo's alert endpoint, behind one mock transport."""

    def __init__(self, spec):
        self.entries = [
            entry("old-1", "Dashboard refresh", "Nicer colors on the home screen."),
            entry("old-2", "Mobile app update", "Bug fixes and speed improvements.", "2026-09-08T10:00:00Z"),
        ]
        self.spec_bytes = dumps(spec)
        self.feed_reply = None  # None: the entries; else a Response, an exception or a function
        self.spec_reply = None
        self.alert_reply = None  # None: Gorelo confirms (200, IsSuccess true, Data true)
        self.requests = []
        self.unmatched = []

    def handler(self, request):
        self.requests.append(request)
        route = (request.method, request.url.host, request.url.path)
        if route == ("GET", "feedback.gorelo.io", "/api/v1/changelog"):
            return reply_with(self.feed_reply, lambda: httpx.Response(200, json={"results": self.entries}))
        if route == ("GET", "api.usw.gorelo.io", "/swagger/v1/swagger.json"):
            return reply_with(self.spec_reply, lambda: httpx.Response(200, content=self.spec_bytes))
        if route == ("POST", "api.usw.gorelo.io", "/v1/alerts"):
            return reply_with(self.alert_reply, lambda: httpx.Response(200, json=envelope(True)))
        self.unmatched.append(route)
        return httpx.Response(404, json={"error": "no such route in the test world"})

    @property
    def alert_posts(self):
        return [r for r in self.requests if r.method == "POST" and r.url.path == "/v1/alerts"]

    @property
    def alerts(self):
        return [json.loads(r.content) for r in self.alert_posts]

    def set_spec(self, document):
        self.spec_bytes = dumps(document)


@pytest.fixture
def world():
    w = World(small_spec())
    yield w
    assert w.unmatched == [], f"the watcher called routes the test world does not know: {w.unmatched}"


@pytest.fixture
def app(tmp_path):
    """A stand-in for /opt/gorelo-mcp/app: spec/spec_index.json is the baseline built from small_spec()."""
    (tmp_path / "spec").mkdir()
    index = spec_snapshot.build_index(small_spec(), dumps(small_spec()), "test")
    (tmp_path / "spec" / "spec_index.json").write_text(spec_snapshot.dumps_index(index), encoding="utf-8")
    return watch.Paths(app_dir=tmp_path, index_path=tmp_path / "spec" / "spec_index.json")


def run(world, app, *, env=None, **kwargs):
    env = {"GORELO_API_KEY": API_KEY} if env is None else env
    return watch.run(env=env, transport=httpx.MockTransport(world.handler), paths=app, now=lambda: NOW, **kwargs)


def baselined(world, app):
    """Run once so the current feed counts as known: the watcher reports nothing the first time."""
    assert run(world, app) == 0


def state(app, name):
    return json.loads((app.state_dir / name).read_text(encoding="utf-8"))


def seen(app):
    return state(app, "changelog-seen.json")["seen"]


def alerted(app):
    path = app.alerts_file
    return json.loads(path.read_text(encoding="utf-8"))["alerts"] if path.exists() else {}


def report_name(key):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", key).strip("-")[:40] or "entry"
    return f"changelog-{slug}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}.md"


API_ENTRY = entry(
    "e-100",
    "Ticket list endpoints are now paginated",
    "The APIs return a cursor.",
    "2026-10-09T08:00:00Z",
)
NEWS_ENTRY = entry("e-101", "New dashboard widgets", "Pick the colors you like.", "2026-10-10T08:00:00Z")


def contract_changed(document):
    document = copy.deepcopy(document)
    document["paths"]["/v1/clients"]["get"]["parameters"].append(
        {"name": "Status", "in": "query", "schema": {"type": "string"}}
    )
    return document


# --------------------------------------------------------------------------
# The classifier
# --------------------------------------------------------------------------

API_WORDS = [
    "API", "APIs", "api", "endpoint", "endpoints", "webhook", "webhooks", "enum", "enums", "schema", "schemas",
    "payload", "payloads", "pagination", "paginated", "paginate", "paginating", "query parameter",
    "query parameters", "breaking", "deprecated", "deprecation", "deprecates", "rename", "renamed", "renames",
    "renaming", "removed", "cursor", "cursors", "rate limit", "rate limits", "rate limiting", "swagger",
    "OpenAPI", "v1", "v2",
]


@pytest.mark.parametrize("word", API_WORDS)
def test_the_classifier_matches_every_form_of_an_api_word(word):
    assert watch.is_api_related({"title": f"Changes to the {word} this week"})
    assert watch.is_api_related({"content": f"We changed {word}."})
    assert watch.is_api_related({"title": word.upper()})


@pytest.mark.parametrize(
    "text",
    [
        "New dashboard widgets",
        "Dark mode for the mobile app",
        "Bug fixes and performance improvements",
        "Rapid onboarding for new technicians",
        "Capital letters in client names",
        "Therapist notes template",
        "The apiary report",
        "",
    ],
)
def test_the_classifier_ignores_ordinary_news(text):
    assert not watch.is_api_related({"title": text})
    assert watch.api_terms({"title": text}) == []


def test_the_classifier_reads_every_text_field_and_ignores_missing_ones():
    for field in ("title", "content", "body", "summary", "markdown"):
        assert watch.is_api_related({field: "payload change"}), field
    assert not watch.is_api_related({"title": None, "content": None, "id": "api"})  # an id is not text


def test_api_terms_lists_each_word_once_in_order_of_appearance():
    found = watch.api_terms({"title": "APIs and Endpoints", "content": "The API renamed 2 endpoints (breaking)."})
    assert found == ["apis", "endpoints", "api", "renamed", "breaking"]


# --------------------------------------------------------------------------
# Changelog: baseline, nothing new, news
# --------------------------------------------------------------------------


def test_the_first_run_records_a_baseline_and_reports_nothing(world, app, capsys):
    assert run(world, app) == 0
    out = capsys.readouterr().out
    assert "baseline recorded: 2 entries known, nothing reported" in out
    assert sorted(seen(app)) == ["old-1", "old-2"]
    assert state(app, "changelog-seen.json")["last_checked"] == NOW.isoformat()
    assert world.alert_posts == []
    assert not app.alerts_file.exists()


def test_a_baseline_never_alerts_even_for_api_looking_history(world, app):
    world.entries.insert(0, API_ENTRY)  # already in the feed on the very first run
    assert run(world, app) == 0
    assert world.alert_posts == []
    assert "e-100" in seen(app)


def test_a_run_with_nothing_new_exits_0_and_says_so(world, app, capsys):
    baselined(world, app)
    capsys.readouterr()
    assert run(world, app) == 0
    out = capsys.readouterr().out
    assert "no new changelog entries (2 known)" in out
    assert "spec contract unchanged" in out
    assert world.alert_posts == []


def test_quiet_prints_nothing_when_nothing_is_new(world, app, capsys):
    baselined(world, app)
    capsys.readouterr()
    assert run(world, app, quiet=True) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_quiet_still_prints_what_is_new(world, app, capsys):
    baselined(world, app)
    capsys.readouterr()
    world.entries.insert(0, API_ENTRY)
    assert run(world, app, quiet=True) == 1
    out = capsys.readouterr().out
    assert "1 NEW Gorelo changelog entry, 1 API-RELATED" in out
    assert "[API] 2026-10-09  Ticket list endpoints are now paginated" in out


def test_a_new_entry_that_is_not_about_the_api_exits_2_and_never_alerts(world, app, capsys):
    baselined(world, app)
    capsys.readouterr()
    world.entries.insert(0, NEWS_ENTRY)
    assert run(world, app) == 2
    out = capsys.readouterr().out
    assert "1 NEW Gorelo changelog entry\n" in out
    assert "2026-10-10  New dashboard widgets" in out
    assert "[API]" not in out
    assert world.alert_posts == []
    assert not app.alerts_file.exists()
    assert "e-101" in seen(app)
    assert run(world, app) == 0  # news is reported once


def test_the_feed_may_wrap_its_list_under_any_known_key(world, app):
    for wrapper in ("results", "data", "items", "changelogs", "entries"):
        world.feed_reply = lambda wrapper=wrapper: httpx.Response(200, json={wrapper: world.entries})
        state_file = app.seen_file
        if state_file.exists():
            state_file.unlink()
        assert run(world, app) == 0
        assert sorted(seen(app)) == ["old-1", "old-2"], wrapper
    world.feed_reply = lambda: httpx.Response(200, json=world.entries)  # a bare list too
    app.seen_file.unlink()
    assert run(world, app) == 0
    assert sorted(seen(app)) == ["old-1", "old-2"]


def test_an_entry_without_an_id_is_keyed_by_title_and_date(world, app):
    baselined(world, app)
    anonymous = {"title": "Webhook payload changes", "createdAt": "2026-10-11T00:00:00Z", "content": ""}
    world.entries.insert(0, anonymous)
    assert run(world, app) == 1
    assert "Webhook payload changes|2026-10-11T00:00:00Z" in seen(app)
    assert run(world, app) == 0
    assert len(world.alert_posts) == 1


# --------------------------------------------------------------------------
# Changelog: the alert
# --------------------------------------------------------------------------


def test_a_new_api_entry_posts_one_alert_with_the_exact_body(world, app, capsys):
    baselined(world, app)
    capsys.readouterr()
    world.entries.insert(0, API_ENTRY)
    assert run(world, app) == 1

    assert len(world.alert_posts) == 1
    post = world.alert_posts[0]
    assert str(post.url) == ALERT_URL and post.method == "POST"
    assert post.headers["X-API-Key"] == API_KEY
    assert post.headers["Content-Type"] == "application/json"
    report = app.reports_dir / report_name("e-100")
    assert json.loads(post.content) == {
        "ClientId": 9503,
        "Name": "Gorelo changelog (API): Ticket list endpoints are now paginated",
        "Resource": "gorelo-mcp",
        "Severity": 2,
        "Description": "\n".join(
            [
                "A new entry in Gorelo's changelog looks API-related.",
                "Title: Ticket list endpoints are now paginated",
                "Date: 2026-10-09",
                "Matched: endpoints, paginated, apis, cursor",
                f"Report: {report}",
                f"Changelog: {CHANGELOG_PAGE}",
                "Next: compare the live spec (scripts/spec_snapshot.py, scripts/spec_diff.py) "
                "before trusting the changelog text.",
            ]
        ),
    }
    text = report.read_text(encoding="utf-8")
    assert "Ticket list endpoints are now paginated" in text and "The APIs return a cursor." in text
    assert alerted(app)["changelog:e-100"]["report"] == str(report)
    assert alerted(app)["changelog:e-100"]["alerted_at"] == NOW.isoformat()
    out = capsys.readouterr().out
    assert "API CHANGE: changelog entry" in out
    assert "alert posted to Gorelo (client 9503, resource gorelo-mcp, severity 2)" in out
    assert f"report: {report}" in out
    assert "e-100" in seen(app)


def test_a_repeated_api_entry_does_not_alert_twice(world, app):
    baselined(world, app)
    world.entries.insert(0, API_ENTRY)
    assert run(world, app) == 1
    assert run(world, app) == 0
    assert run(world, app) == 0
    assert len(world.alert_posts) == 1


def test_an_entry_alerted_before_is_never_alerted_again_even_if_the_seen_list_forgot_it(world, app):
    baselined(world, app)
    world.entries.insert(0, API_ENTRY)
    assert run(world, app) == 1
    app.seen_file.write_text(json.dumps({"seen": ["old-1", "old-2"]}), encoding="utf-8")  # a crash lost "e-100"
    assert run(world, app) == 0
    assert len(world.alert_posts) == 1
    assert "e-100" in seen(app)


def test_every_new_api_entry_gets_its_own_alert(world, app):
    baselined(world, app)
    second = entry("e-102", "Webhooks: new payload fields", "Enums were renamed.", "2026-10-12T08:00:00Z")
    world.entries[:0] = [second, API_ENTRY]
    assert run(world, app) == 1
    assert [a["Name"] for a in world.alerts] == [
        "Gorelo changelog (API): Webhooks: new payload fields",
        "Gorelo changelog (API): Ticket list endpoints are now paginated",
    ]
    assert set(alerted(app)) == {"changelog:e-100", "changelog:e-102"}


def test_an_entry_listed_twice_in_the_feed_alerts_once(world, app):
    baselined(world, app)
    world.entries[:0] = [API_ENTRY, copy.deepcopy(API_ENTRY)]
    assert run(world, app) == 1
    assert len(world.alert_posts) == 1


def test_a_changelog_entry_and_a_contract_change_in_one_run_alert_once_each(world, app, capsys):
    baselined(world, app)
    capsys.readouterr()
    world.entries.insert(0, API_ENTRY)
    world.set_spec(contract_changed(small_spec()))
    assert run(world, app) == 1
    first, second = world.alerts
    assert first["Name"].startswith("Gorelo changelog (API): ")  # the changelog entry first, then the spec
    assert second["Name"].startswith("Gorelo API spec changed (contract ")
    assert [a["ClientId"] for a in world.alerts] == [9503, 9503]
    assert [a["Resource"] for a in world.alerts] == ["gorelo-mcp", "gorelo-mcp"]
    assert [a["Severity"] for a in world.alerts] == [2, 2]
    assert len(alerted(app)) == 2 and "e-100" in seen(app)
    out = capsys.readouterr().out
    assert out.count("API CHANGE:") == 2 and out.count("alert posted to Gorelo") == 2
    assert run(world, app) == 0  # neither one alerts again
    assert len(world.alert_posts) == 2


def test_api_and_non_api_entries_together_exit_1_and_alert_only_the_api_one(world, app, capsys):
    baselined(world, app)
    capsys.readouterr()
    world.entries[:0] = [NEWS_ENTRY, API_ENTRY]
    assert run(world, app) == 1
    assert len(world.alert_posts) == 1
    assert world.alerts[0]["Name"].endswith("Ticket list endpoints are now paginated")
    out = capsys.readouterr().out
    assert "2 NEW Gorelo changelog entries, 1 API-RELATED" in out
    assert "New dashboard widgets" in out
    assert {"e-100", "e-101"} <= set(seen(app))


def test_the_alert_title_is_one_clean_line_of_bounded_length(world, app):
    baselined(world, app)
    nasty = "API \x1b[31mred\x1b[0m\n" + "very long title " * 20 + chr(0x202E) + "evil"
    world.entries.insert(0, entry("e-200", nasty, "api"))
    assert run(world, app) == 1
    name = world.alerts[0]["Name"]
    assert "\n" not in name and "\x1b" not in name and chr(0x202E) not in name
    assert len(name) <= len("Gorelo changelog (API): ") + 110
    assert name.endswith("...")


def test_the_watch_keeps_at_most_500_seen_keys_and_always_the_current_feed(world, app):
    app.state_dir.mkdir()
    known = [f"ancient-{n}" for n in range(600)] + ["old-1", "old-2"]
    app.seen_file.write_text(json.dumps({"seen": known}), encoding="utf-8")
    assert run(world, app) == 0
    kept = seen(app)
    assert len(kept) == 500
    assert kept[-2:] == ["old-1", "old-2"]  # the oldest keys go first, the current feed stays
    assert kept[0] == "ancient-102"


# --------------------------------------------------------------------------
# Reset
# --------------------------------------------------------------------------


def test_reset_rebaselines_the_changelog_and_alerts_for_nothing_in_it(world, app, capsys):
    baselined(world, app)
    world.entries.insert(0, API_ENTRY)
    capsys.readouterr()
    assert run(world, app, reset=True) == 0
    assert "baseline recorded: 3 entries known, nothing reported" in capsys.readouterr().out
    assert world.alert_posts == []
    assert run(world, app) == 0  # and e-100 is now known


def test_reset_replaces_the_seen_list_with_the_current_feed(world, app):
    app.state_dir.mkdir()
    app.seen_file.write_text(json.dumps({"seen": ["gone-1", "gone-2", "old-1"]}), encoding="utf-8")
    assert run(world, app, reset=True) == 0
    assert sorted(seen(app)) == ["old-1", "old-2"]


def test_reset_does_not_hide_a_spec_change(world, app):
    baselined(world, app)
    world.set_spec(contract_changed(small_spec()))
    assert run(world, app, reset=True) == 1
    assert len(world.alert_posts) == 1


# --------------------------------------------------------------------------
# Spec check
# --------------------------------------------------------------------------


def test_an_unchanged_spec_changes_nothing_and_writes_no_spec_files(world, app):
    baselined(world, app)
    assert run(world, app) == 0
    assert not app.spec_dir.exists() and not app.reports_dir.exists()
    spec_requests = [r for r in world.requests if r.url.path == "/swagger/v1/swagger.json"]
    assert len(spec_requests) == 2  # fetched on every run
    assert "gorelo-mcp-spec-snapshot" in spec_requests[0].headers["User-Agent"]


def test_a_documentation_only_change_of_the_live_spec_is_not_a_change(world, app):
    baselined(world, app)
    docs = copy.deepcopy(small_spec())
    docs["info"]["description"] = "Rewritten documentation."
    docs["paths"]["/v1/clients"]["get"]["summary"] = "Retitled"
    docs["paths"]["/v1/clients"]["get"]["parameters"][0]["description"] = "Reworded"
    docs["paths"]["/v1/ping"]["get"]["responses"]["200"]["description"] = "fine"
    world.set_spec(docs)
    assert spec_snapshot.contract_sha256(docs) == spec_snapshot.contract_sha256(small_spec())
    assert run(world, app) == 0
    assert world.alert_posts == []
    assert not app.spec_dir.exists()


def test_the_watcher_hashes_the_spec_the_way_spec_snapshot_does():
    raw = (REPO_ROOT / "backups" / "swagger-latest.json").read_bytes()
    committed = json.loads((REPO_ROOT / "spec" / "spec_index.json").read_text(encoding="utf-8"))
    assert spec_snapshot.contract_sha256(spec_snapshot.parse_spec(raw)) == committed["contract_sha256"]
    assert watch.load_baseline_hash(REPO_ROOT / "spec" / "spec_index.json") == committed["contract_sha256"]


def test_the_committed_spec_is_not_drift_against_the_committed_index(tmp_path, capsys):
    """The real baseline and the real spec snapshot it came from: the watcher must stay quiet."""
    w = World(small_spec())
    w.spec_bytes = (REPO_ROOT / "backups" / "swagger-latest.json").read_bytes()
    paths = watch.Paths(app_dir=tmp_path, index_path=REPO_ROOT / "spec" / "spec_index.json")
    assert run(w, paths) == 0
    assert run(w, paths) == 0
    assert w.alert_posts == [] and w.unmatched == []
    assert "spec contract unchanged" in capsys.readouterr().out
    assert not paths.spec_dir.exists()


def test_a_contract_change_saves_the_spec_and_a_report_and_alerts_once(world, app, capsys):
    baselined(world, app)
    capsys.readouterr()
    changed = contract_changed(small_spec())
    world.set_spec(changed)
    live = spec_snapshot.contract_sha256(changed)
    baseline = json.loads(app.index_path.read_text(encoding="utf-8"))["contract_sha256"]
    assert live != baseline

    assert run(world, app) == 1

    saved = app.spec_dir / f"swagger-{live[:16]}.json"
    report = app.reports_dir / f"spec-diff-{live[:16]}.md"
    assert saved.read_bytes() == world.spec_bytes  # exactly what the live spec served
    text = report.read_text(encoding="utf-8")
    assert "# Gorelo API contract changed" in text
    assert f"`{live}`" in text and f"`{baseline}`" in text
    assert "# Gorelo API spec diff" in text and "`GET /v1/clients`" in text and "`Status`" in text

    assert len(world.alert_posts) == 1
    body = world.alerts[0]
    assert set(body) == {"ClientId", "Name", "Resource", "Severity", "Description"}
    assert (body["ClientId"], body["Resource"], body["Severity"]) == (9503, "gorelo-mcp", 2)
    assert body["Name"] == f"Gorelo API spec changed (contract {live[:12]})"
    assert body["Description"].splitlines() == [
        "The live Gorelo OpenAPI contract differs from spec/spec_index.json.",
        f"Live contract sha256: {live}",
        f"spec_index.json contract sha256: {baseline}",
        "Operations: 0 added, 0 removed, 1 changed. Schemas: 0 added, 0 removed, 0 changed.",
        f"Report: {report}",
        f"Saved spec: {saved}",
        "Next: follow \"When the watcher reports an API change\" in docs/OPERATIONS.md.",
    ]
    assert world.alert_posts[0].headers["X-API-Key"] == API_KEY
    assert alerted(app)[f"spec:{live}"]["report"] == str(report)
    out = capsys.readouterr().out
    assert f"API CHANGE: live spec contract {live[:12]} differs from spec/spec_index.json ({baseline[:12]})" in out
    assert f"report: {report}" in out


def test_the_same_contract_change_does_not_alert_again(world, app, capsys):
    baselined(world, app)
    world.set_spec(contract_changed(small_spec()))
    assert run(world, app) == 1
    capsys.readouterr()
    assert run(world, app) == 0
    assert "already reported, no new alert" in capsys.readouterr().out
    assert run(world, app, quiet=True) == 0
    assert len(world.alert_posts) == 1


def test_a_further_contract_change_alerts_again(world, app):
    baselined(world, app)
    first = contract_changed(small_spec())
    world.set_spec(first)
    assert run(world, app) == 1
    second = contract_changed(first)
    second["paths"]["/v1/clients"]["get"]["parameters"][-1]["name"] = "Region"
    world.set_spec(second)
    assert run(world, app) == 1
    assert len(world.alert_posts) == 2
    assert world.alerts[0]["Name"] != world.alerts[1]["Name"]
    assert len(alerted(app)) == 2


def test_a_removed_operation_is_counted_in_the_alert_and_named_in_the_report(world, app):
    baselined(world, app)
    gone = copy.deepcopy(small_spec())
    del gone["paths"]["/v1/ping"]
    world.set_spec(gone)
    assert run(world, app) == 1
    assert "Operations: 0 added, 1 removed, 0 changed." in world.alerts[0]["Description"]
    report = next(app.reports_dir.glob("spec-diff-*.md")).read_text(encoding="utf-8")
    assert "`GET /v1/ping`" in report


def test_the_real_spec_with_one_operation_removed_is_reported(tmp_path):
    """The real spec and baseline end to end: the diff report must work on the full-size document."""
    document = json.loads((REPO_ROOT / "backups" / "swagger-latest.json").read_text(encoding="utf-8"))
    del document["paths"]["/v1/alerts"]["post"]
    w = World(small_spec())
    w.set_spec(document)
    paths = watch.Paths(app_dir=tmp_path, index_path=REPO_ROOT / "spec" / "spec_index.json")
    assert run(w, paths) == 1  # the changelog is only baselined; the spec check found the change in the same pass
    assert len(w.alert_posts) == 1
    assert "Operations: 0 added, 1 removed, 0 changed." in w.alerts[0]["Description"]
    report = next(paths.reports_dir.glob("spec-diff-*.md")).read_text(encoding="utf-8")
    assert "`POST /v1/alerts`" in report


def test_a_change_the_index_does_not_record_still_alerts(world, app):
    baselined(world, app)
    changed = copy.deepcopy(small_spec())
    changed["servers"] = [{"url": "https://example.invalid"}]  # the index has no key for servers
    world.set_spec(changed)
    assert run(world, app) == 1
    assert "something outside the index changed" in world.alerts[0]["Description"]


def test_a_diff_that_crashes_does_not_stop_the_alert(world, app, monkeypatch):
    baselined(world, app)

    def broken(*args, **kwargs):
        raise RuntimeError("diff exploded")

    monkeypatch.setattr(watch.spec_diff, "diff_indexes", broken)
    world.set_spec(contract_changed(small_spec()))
    assert run(world, app) == 1
    assert len(world.alert_posts) == 1
    assert "could not be built" in world.alerts[0]["Description"]
    report = next(app.reports_dir.glob("spec-diff-*.md")).read_text(encoding="utf-8")
    assert "diff exploded" in report and "spec_diff.py spec/spec_index.json" in report
    assert next(app.spec_dir.glob("swagger-*.json")).exists()


@pytest.mark.parametrize(
    "baseline_text",
    [None, "not json", "[]", json.dumps({"ops": {}}), json.dumps({"contract_sha256": "short"})],
    ids=["missing", "not-json", "not-an-object", "no-hash", "bad-hash"],
)
def test_an_unusable_baseline_is_a_failure_not_an_alert(world, app, capsys, baseline_text):
    if baseline_text is None:
        app.index_path.unlink()
    else:
        app.index_path.write_text(baseline_text, encoding="utf-8")
    assert run(world, app) == 3
    err = capsys.readouterr().err
    assert "ERROR checking the Gorelo spec" in err and "spec baseline" in err
    assert world.alert_posts == []
    assert sorted(seen(app)) == ["old-1", "old-2"]  # the changelog part still ran


# --------------------------------------------------------------------------
# Fetch failures
# --------------------------------------------------------------------------

FEED_FAILURES = {
    "http-500": lambda: httpx.Response(500, text="boom"),
    "html": lambda: httpx.Response(200, text="<html>maintenance</html>"),
    "unknown-shape": lambda: httpx.Response(200, json={"unexpected": 1}),
    "not-a-container": lambda: httpx.Response(200, json="nope"),
    "empty-feed": lambda: httpx.Response(200, json={"results": []}),
    "entry-not-an-object": lambda: httpx.Response(200, json={"results": ["x"]}),
    "connect-error": httpx.ConnectError("no route to host"),
    "timeout": httpx.ReadTimeout("too slow"),
}


@pytest.mark.parametrize("failure", sorted(FEED_FAILURES))
def test_a_changelog_fetch_failure_exits_3_and_leaves_the_state_alone(world, app, capsys, failure):
    baselined(world, app)
    before = app.seen_file.read_bytes()
    capsys.readouterr()
    world.feed_reply = FEED_FAILURES[failure]
    assert run(world, app) == 3
    assert "ERROR fetching Gorelo changelog" in capsys.readouterr().err
    assert app.seen_file.read_bytes() == before
    assert world.alert_posts == []


SPEC_FAILURES = {
    "http-503": lambda: httpx.Response(503, text="down"),
    "html": lambda: httpx.Response(200, text="<html>maintenance</html>"),
    "not-openapi": lambda: httpx.Response(200, json={"hello": "world"}),
    "connect-error": httpx.ConnectError("no route to host"),
    "timeout": httpx.ReadTimeout("too slow"),
}


@pytest.mark.parametrize("failure", sorted(SPEC_FAILURES))
def test_a_spec_fetch_failure_exits_3_without_an_alert(world, app, capsys, failure):
    baselined(world, app)
    capsys.readouterr()
    world.spec_reply = SPEC_FAILURES[failure]
    assert run(world, app) == 3
    assert "ERROR checking the Gorelo spec" in capsys.readouterr().err
    assert world.alert_posts == [] and not app.spec_dir.exists()


def test_both_sources_failing_exits_3(world, app, capsys):
    baselined(world, app)
    capsys.readouterr()
    world.feed_reply = FEED_FAILURES["http-500"]
    world.spec_reply = SPEC_FAILURES["http-503"]
    assert run(world, app) == 3
    err = capsys.readouterr().err
    assert "fetching Gorelo changelog" in err and "checking the Gorelo spec" in err


def test_an_api_change_wins_over_a_failure_of_the_other_check(world, app, capsys):
    baselined(world, app)
    capsys.readouterr()
    world.entries.insert(0, API_ENTRY)
    world.spec_reply = SPEC_FAILURES["http-503"]
    assert run(world, app) == 1
    assert len(world.alert_posts) == 1
    assert "ERROR checking the Gorelo spec" in capsys.readouterr().err

    world.spec_reply = None
    world.feed_reply = FEED_FAILURES["http-500"]
    world.set_spec(contract_changed(small_spec()))
    assert run(world, app) == 1
    assert len(world.alert_posts) == 2


def test_a_failure_wins_over_plain_news(world, app):
    baselined(world, app)
    world.entries.insert(0, NEWS_ENTRY)
    world.spec_reply = SPEC_FAILURES["timeout"]
    assert run(world, app) == 3
    assert "e-101" in seen(app)  # the news was still recorded, so it will not be reported twice
    world.spec_reply = None
    assert run(world, app) == 0


# --------------------------------------------------------------------------
# Alerts that Gorelo did not confirm
# --------------------------------------------------------------------------

ALERT_FAILURES = {
    "http-500": lambda: httpx.Response(500, json=error_envelope(500, [("010101", "Something broke")])),
    "http-401": lambda: httpx.Response(401, json=error_envelope(401, [("080101", "Invalid API key")])),
    "http-400": lambda: httpx.Response(400, json=error_envelope(400, [("070101", "Name is required", "Name")])),
    "http-429": lambda: httpx.Response(429, json={"error": "rate_limited", "retry_after": "1s"}),
    "success-false": lambda: httpx.Response(
        200, json={"StatusCode": 200, "IsSuccess": False, "Data": True, "DataContext": None, "Notifications": []}
    ),
    "data-false": lambda: httpx.Response(200, json=envelope(False)),
    "data-missing": lambda: httpx.Response(200, json=envelope(None)),
    "not-json": lambda: httpx.Response(200, text="OK"),
    "redirect": lambda: httpx.Response(302, headers={"Location": "https://evil.example/alerts"}),
    "timeout": httpx.ReadTimeout("too slow"),
    "connect-error": httpx.ConnectError("no route to host"),
}


@pytest.mark.parametrize("failure", sorted(ALERT_FAILURES))
def test_an_alert_gorelo_did_not_confirm_is_not_recorded_and_is_retried(world, app, capsys, failure):
    baselined(world, app)
    capsys.readouterr()
    world.entries.insert(0, API_ENTRY)
    world.alert_reply = ALERT_FAILURES[failure]

    assert run(world, app) == 1  # the change is real, whether or not the alert went out
    captured = capsys.readouterr()
    assert "ALERT NOT SENT" in captured.err and "the next run tries again" in captured.err
    assert API_KEY not in captured.out + captured.err
    assert len(world.alert_posts) == 1  # one attempt per run, never an immediate retry
    assert alerted(app) == {}
    assert "e-100" not in seen(app)  # held back, so the next run finds the entry again
    assert all(r.url.host != "evil.example" for r in world.requests)  # the key never follows a redirect

    world.alert_reply = None  # Gorelo is back
    assert run(world, app) == 1
    assert len(world.alert_posts) == 2
    assert "changelog:e-100" in alerted(app) and "e-100" in seen(app)
    assert run(world, app) == 0
    assert len(world.alert_posts) == 2


def test_a_failed_spec_alert_is_retried_too(world, app):
    baselined(world, app)
    world.set_spec(contract_changed(small_spec()))
    world.alert_reply = ALERT_FAILURES["http-500"]
    assert run(world, app) == 1
    assert alerted(app) == {}
    world.alert_reply = None
    assert run(world, app) == 1
    assert len(alerted(app)) == 1
    assert run(world, app) == 0
    assert len(world.alert_posts) == 2


def test_gorelos_explanation_and_trace_id_reach_the_operator(world, app, capsys):
    baselined(world, app)
    capsys.readouterr()
    world.entries.insert(0, API_ENTRY)
    world.alert_reply = lambda: httpx.Response(
        400, json=error_envelope(400, [("070101", "Name is required", "Name")], trace_id="00-abc-def-01")
    )
    assert run(world, app) == 1
    err = capsys.readouterr().err
    assert "Gorelo answered HTTP 400 (Name is required, trace 00-abc-def-01)" in err


@pytest.mark.parametrize("env", [{}, {"GORELO_API_KEY": ""}, {"GORELO_API_KEY": "   "}], ids=["unset", "empty", "blank"])
def test_without_an_api_key_nothing_is_posted_but_the_change_is_still_reported(world, app, capsys, env):
    baselined(world, app)
    capsys.readouterr()
    world.entries.insert(0, API_ENTRY)
    assert run(world, app, env=env) == 1
    assert world.alert_posts == []
    err = capsys.readouterr().err
    assert "ALERT NOT SENT" in err and "GORELO_API_KEY is not set" in err
    assert "e-100" not in seen(app)
    assert run(world, app) == 1  # with the key back, the pending entry alerts
    assert len(world.alert_posts) == 1


def test_the_api_key_goes_only_to_the_alert_request_and_never_into_output(world, app, capsys):
    baselined(world, app)
    world.entries.insert(0, API_ENTRY)
    world.set_spec(contract_changed(small_spec()))
    assert run(world, app) == 1
    for request in world.requests:
        if request.method == "POST":
            assert request.headers["X-API-Key"] == API_KEY
            assert request.url.host == "api.usw.gorelo.io"
        else:
            assert "X-API-Key" not in request.headers, request.url
            assert "Authorization" not in request.headers, request.url
    captured = capsys.readouterr()
    assert API_KEY not in captured.out + captured.err
    for path in app.state_dir.rglob("*"):
        if path.is_file():
            assert API_KEY.encode() not in path.read_bytes(), path


# --------------------------------------------------------------------------
# State: corrupt, unwritable, contained
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["changelog-seen.json", "alerts-sent.json"])
@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"seen": "x", "alerts": []}'])
def test_unreadable_state_is_a_failure_and_nothing_is_fetched(world, app, capsys, name, content):
    app.state_dir.mkdir()
    (app.state_dir / name).write_text(content, encoding="utf-8")
    assert run(world, app) == 3
    assert "ERROR cannot use the watcher state" in capsys.readouterr().err
    assert world.requests == []


def test_a_state_directory_that_is_not_writable_is_a_failure(world, app, capsys, monkeypatch):
    monkeypatch.setattr(watch.os, "access", lambda *args, **kwargs: False)
    assert run(world, app) == 3
    assert "is not writable" in capsys.readouterr().err
    assert world.requests == []


def test_a_state_path_that_is_a_file_is_a_failure(world, app, capsys):
    app.state_dir.write_text("in the way", encoding="utf-8")
    assert run(world, app) == 3
    assert "ERROR cannot use the watcher state" in capsys.readouterr().err


def test_an_old_format_state_file_from_the_previous_watcher_is_read_as_it_is(world, app):
    app.state_dir.mkdir()
    old = {"seen": ["old-2", "old-1"], "last_checked": "2026-09-30T04:00:00+00:00"}  # sorted, as it wrote them
    app.seen_file.write_text(json.dumps(old), encoding="utf-8")
    world.entries.insert(0, NEWS_ENTRY)
    assert run(world, app) == 2  # not a baseline: the state already knew the feed
    assert set(seen(app)) == {"old-1", "old-2", "e-101"}


def test_nothing_is_written_outside_the_state_directory(world, app, tmp_path):
    before = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")}
    baselined(world, app)
    hostile = entry("../../../../tmp/pwned", "Webhook payload change", "api", "2026-10-09")
    world.entries.insert(0, hostile)
    world.set_spec(contract_changed(small_spec()))
    assert run(world, app) == 1
    after = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")}
    created = after - before
    assert created and all(name == ".state" or name.startswith(".state/") for name in created), sorted(created)
    reports = sorted(p.name for p in app.reports_dir.iterdir())
    assert any(name.startswith("changelog-tmp-pwned-") for name in reports)  # the key became a harmless file name


def test_write_atomic_refuses_a_target_outside_the_state_directory(app, tmp_path, tmp_path_factory):
    app.state_dir.mkdir()
    with pytest.raises(watch.WatchError, match="refusing to write outside"):
        watch.write_atomic(app, tmp_path / "elsewhere.txt", b"x")
    with pytest.raises(watch.WatchError, match="refusing to write outside"):
        watch.write_atomic(app, app.state_dir / ".." / "elsewhere.txt", b"x")
    outside = tmp_path_factory.mktemp("outside")
    (app.state_dir / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(watch.WatchError, match="refusing to write outside"):
        watch.write_atomic(app, app.state_dir / "link" / "x.txt", b"x")
    assert not (outside / "x.txt").exists()
    watch.write_atomic(app, app.state_dir / "sub" / "ok.txt", b"fine")
    assert (app.state_dir / "sub" / "ok.txt").read_bytes() == b"fine"
    assert not list(app.state_dir.rglob("*.tmp"))


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------


def test_main_passes_the_flags_to_run(monkeypatch):
    calls = []
    monkeypatch.setattr(watch, "run", lambda **kwargs: calls.append(kwargs) or 0)
    assert watch.main([]) == 0
    assert watch.main(["--quiet", "--reset"]) == 0
    assert calls == [{"quiet": False, "reset": False}, {"quiet": True, "reset": True}]


def test_a_crash_exits_3_never_1(monkeypatch, capsys):
    def explode(**kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(watch, "run", explode)
    assert watch.main([]) == 3  # exit 1 would read as "API change"
    err = capsys.readouterr().err
    assert "Traceback" in err and "RuntimeError: bug" in err and "this says nothing about the API" in err


def test_the_script_runs_on_its_own_from_another_directory(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "watch_gorelo_changelog.py"), "--help"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "--quiet" in result.stdout and "--reset" in result.stdout and "Exit codes" in result.stdout


# --------------------------------------------------------------------------
# The systemd units in deploy/
# --------------------------------------------------------------------------

SERVICE = REPO_ROOT / "deploy" / "gorelo-changelog-watch.service"
TIMER = REPO_ROOT / "deploy" / "gorelo-changelog-watch.timer"

# The reference timer unit: the staged copy must stay byte for byte the same.
INSTALLED_TIMER = """\
[Unit]
Description=Daily check of Gorelo's changelog for API changes

[Timer]
OnCalendar=daily
# A random delay of up to 30 minutes spreads the daily request.
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
"""


def unit(path):
    parser = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))
    parser.optionxform = str
    parser.read_string(path.read_text(encoding="utf-8"))
    return parser


def test_the_staged_timer_is_an_unchanged_copy_of_the_installed_one():
    assert TIMER.read_text(encoding="utf-8") == INSTALLED_TIMER


def test_the_staged_service_loads_the_env_file_and_only_exit_2_is_a_success():
    service = unit(SERVICE)["Service"]
    assert service["Type"] == "oneshot"
    assert service["User"] == "gorelo-mcp" and service["Group"] == "gorelo-mcp"
    assert service["WorkingDirectory"] == "/opt/gorelo-mcp/app"
    # least privilege: the watcher gets its own file with GORELO_API_KEY only, never the app .env; no "-": a missing
    # file must fail loudly
    assert service["EnvironmentFile"] == "/etc/gorelo-mcp/watcher.env"
    assert service["SuccessExitStatus"] == "2"  # 0 is implicit; 1 (API change) and 3 (failure) fail the unit
    assert service["ReadWritePaths"] == "/opt/gorelo-mcp/app/.state"


def test_the_staged_service_runs_the_watcher_from_the_venv_quietly():
    exec_start = unit(SERVICE)["Service"]["ExecStart"].split()
    assert exec_start == [
        "/opt/gorelo-mcp/app/.venv/bin/python",
        "scripts/watch_gorelo_changelog.py",
        "--quiet",
    ]
    assert (REPO_ROOT / exec_start[1]).is_file()


def test_the_staged_service_keeps_the_hardening_of_the_installed_one():
    service = unit(SERVICE)["Service"]
    for key, value in {
        "NoNewPrivileges": "true",
        "PrivateTmp": "true",
        "ProtectSystem": "strict",
        "ProtectHome": "true",
        "ProtectKernelTunables": "true",
        "ProtectKernelModules": "true",
        "RestrictSUIDSGID": "true",
        "LockPersonality": "true",
        "StandardOutput": "journal",
        "StandardError": "journal",
    }.items():
        assert service[key] == value, key
    assert "Install" not in unit(SERVICE).sections()  # started by the timer only


def test_the_unit_exit_codes_match_the_watchers_exit_codes():
    assert (watch.EXIT_NOTHING_NEW, watch.EXIT_API_CHANGE, watch.EXIT_NEWS_ONLY, watch.EXIT_FAILURE) == (0, 1, 2, 3)
    codes = unit(SERVICE)["Service"]["SuccessExitStatus"].split()
    assert str(watch.EXIT_NEWS_ONLY) in codes
    assert str(watch.EXIT_API_CHANGE) not in codes and str(watch.EXIT_FAILURE) not in codes


def test_without_a_site_config_the_watcher_exits_3_before_touching_anything(world, app, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("GORELO_SITE_CONFIG", str(tmp_path / "missing.toml"))
    assert run(world, app) == 3
    err = capsys.readouterr().err
    assert "site config" in err and "site.example.toml" in err
    assert not (app.app_dir / ".state").exists() and world.requests == []


def test_a_missing_alert_client_id_exits_3_and_names_the_key(world, app, capsys, monkeypatch, tmp_path):
    from site_helper import drop_key

    drop_key(monkeypatch, tmp_path, "watcher", "alert_client_id")
    assert run(world, app) == 3
    assert "missing key [watcher] alert_client_id" in capsys.readouterr().err


def test_a_watcher_only_site_file_with_just_the_watcher_section_is_enough(world, app, monkeypatch, tmp_path):
    path = tmp_path / "site.local.toml"
    path.write_text("[watcher]\nalert_client_id = 9503\n", encoding="utf-8")
    monkeypatch.setenv("GORELO_SITE_CONFIG", str(path))
    baselined(world, app)
    world.entries.insert(0, API_ENTRY)
    assert run(world, app) == 1
    assert [a["ClientId"] for a in world.alerts] == [9503]


def test_the_unedited_example_file_is_refused_by_the_watcher(world, app, capsys, monkeypatch):
    import site_config

    monkeypatch.setenv("GORELO_SITE_CONFIG", str(site_config.REPO_ROOT / "site.example.toml"))
    assert run(world, app) == 3
    assert "site.example.toml" in capsys.readouterr().err and world.requests == []
