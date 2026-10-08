"""Watch Gorelo for API changes and post ONE Gorelo alert per new change.

Background: on 2026-07-24 Gorelo converted most collection endpoints to a cursor-paginated envelope. It
was a breaking change, announced as a changelog post; status.gorelo.io does not list the Public API as a component. A list tool could
silently return only the first page of records. This script closes that gap. gorelo-changelog-watch.timer runs it daily (deploy/gorelo-changelog-watch.service).

Two checks run on every invocation:

1. Changelog. Polls the changelog JSON, remembers which entries it has seen and flags the new ones. A new
   entry whose text looks API-related (API_PATTERN) is an API change; any other new entry is only news.
2. Spec. Fetches the live OpenAPI spec, computes its contract hash exactly like scripts/spec_snapshot.py
   (spec_snapshot.contract_sha256: the sha256 of the spec without descriptions, summaries and examples)
   and compares it with contract_sha256 in spec/spec_index.json. A different hash is an API change: the
   new spec and a spec_diff report are saved under .state/.

Every API change gets ONE alert through the Gorelo API (POST /v1/alerts) against the alert client of site.local.toml ([watcher] alert_client_id,
the only key of that file the watcher reads; the file is /opt/gorelo-mcp/app/site.local.toml, root:gorelo-mcp 0640),
Resource "gorelo-mcp", Severity 2, with a short summary and the path of its report. The key comes from
GORELO_API_KEY in the environment (the systemd unit loads /etc/gorelo-mcp/watcher.env) and is sent as
X-API-Key to the alert request only, never to the changelog host.

Exit codes (a systemd timer or cron acts on them; the unit treats 0 and 2 as success):
    0  nothing new
    1  new API change (the alert was posted; if it could not be, stderr says so and the change stays
       pending, so the next run tries again)
    2  new changelog entries, none API-related
    3  a fetch, parse, state or configuration failure, and no new API change
A new API change wins over a failure (the failure is still printed), and a failure wins over plain news.

Everything this script remembers or writes lives under .state/ (gitignored; the unit's only writable path):
    .state/changelog-seen.json            {"seen": [...entry keys...], "last_checked": "..."}
    .state/alerts-sent.json               {"alerts": {"<key>": {...}}}: what was alerted, so the same change
                                          never alerts twice. Keys: "changelog:<entry key>", "spec:<contract hash>"
    .state/spec/swagger-<hash16>.json     the live spec whose contract hash differed from the baseline
    .state/reports/spec-diff-<hash16>.md  the spec_diff report for it
    .state/reports/changelog-<slug>-<hash8>.md   one report per API-related changelog entry

The first run with no state records a baseline of the current feed and reports nothing, so it cannot spam
anybody with every historical entry. A changelog entry that looks API-related but could not be alerted is
not marked as seen, so it is found again (and its alert retried) by the next run.

Usage:
    runuser -u gorelo-mcp -- /opt/gorelo-mcp/app/.venv/bin/python scripts/watch_gorelo_changelog.py [--quiet] [--reset]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import traceback
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spec_diff  # noqa: E402
import site_config  # noqa: E402
import spec_snapshot  # noqa: E402

FEED = "https://feedback.gorelo.io/api/v1/changelog"
CHANGELOG_PAGE = "https://feedback.gorelo.io/changelog"
SPEC_URL = spec_snapshot.DEFAULT_SOURCE
ALERT_URL = "https://api.usw.gorelo.io/v1/alerts"
API_KEY_ENV = "GORELO_API_KEY"
ALERT_RESOURCE = "gorelo-mcp"
ALERT_SEVERITY = 2
TIMEOUT = 30.0
MAX_SEEN = 500

APP_DIR = Path(__file__).resolve().parent.parent

EXIT_NOTHING_NEW = 0
EXIT_API_CHANGE = 1
EXIT_NEWS_ONLY = 2
EXIT_FAILURE = 3

# An entry matching any of these is worth waking up for. Deliberately broad: a false positive costs you
# 30 seconds of reading, a false negative means silently wrong data. Plurals and
# variants count (the first version missed "APIs" and "endpoints"): api(s), endpoint(s), webhook(s),
# enum(s), schema(s), payload(s), cursor(s), paginat*, query parameter(s), rate limit(s)/limiting,
# breaking, deprecat*, renam*, removed, plus swagger, openapi, v1 and v2.
API_PATTERN = re.compile(
    r"""\b(?:
        apis? | endpoints? | webhooks? | enums? | schemas? | payloads? | cursors? | swagger | openapi | v1 | v2
      | paginat\w* | deprecat\w* | renam\w*
      | query[\s_-]+param(?:eter)?s?
      | rate[\s_-]*limit\w*
      | breaking | removed
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

_TEXT_FIELDS = ("title", "content", "body", "summary", "markdown")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class WatchError(Exception):
    """A failure the operator can act on: state, baseline or configuration."""


# Failures that mean "could not complete this check" (exit 3). Anything else is a bug and reaches main().
CHECK_ERRORS = (httpx.HTTPError, ValueError, OSError, WatchError, spec_snapshot.SnapshotError)


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------


def clean(value: Any, limit: int) -> str:
    """One line of printable text from a third-party string: whitespace collapsed, control and format
    characters dropped, cut to `limit` characters (ending in "...")."""
    text = " ".join(str(value).split())
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def clean_block(value: Any, limit: int) -> str:
    """Like clean() but keeps the line structure (for the text inside a report)."""
    lines = [
        "".join(ch for ch in line.replace("\t", " ") if unicodedata.category(ch) not in ("Cc", "Cf")).rstrip()
        for line in str(value).splitlines()
    ]
    text = "\n".join(lines).strip()
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


# --------------------------------------------------------------------------
# Changelog entries
# --------------------------------------------------------------------------


def text_of(entry: dict) -> str:
    return " ".join(str(entry[k]) for k in _TEXT_FIELDS if entry.get(k) not in (None, ""))


def api_terms(entry: dict) -> list[str]:
    """The distinct API-related words found in an entry, lower case, in order of appearance. Empty means
    the entry does not look API-related."""
    found: list[str] = []
    for match in API_PATTERN.finditer(text_of(entry)):
        term = " ".join(match.group(0).lower().split())
        if term not in found:
            found.append(term)
    return found


def is_api_related(entry: dict) -> bool:
    return bool(API_PATTERN.search(text_of(entry)))


def entry_key(entry: dict) -> str:
    for k in ("id", "_id", "slug", "uuid"):
        if entry.get(k):
            return str(entry[k])
    return f"{entry.get('title', '')}|{entry.get('createdAt', entry.get('date', ''))}"


def entry_title(entry: dict) -> str:
    return clean(entry.get("title") or "<untitled>", 110)


def entry_date(entry: dict) -> str:
    return str(entry.get("createdAt") or entry.get("date") or "")[:10]


def fetch_entries(http: httpx.Client) -> list[dict]:
    r = http.get(FEED, params={"limit": 25, "page": 1})
    r.raise_for_status()
    data = r.json()
    # The API has wrapped its list under different keys over time; accept any.
    entries: Any = None
    for key in ("results", "data", "items", "changelogs", "entries"):
        v = data.get(key) if isinstance(data, dict) else None
        if isinstance(v, list):
            entries = v
            break
    if entries is None and isinstance(data, list):
        entries = data
    if entries is None:
        shape = sorted(data)[:8] if isinstance(data, dict) else type(data).__name__
        raise ValueError(f"unrecognised changelog payload shape: {shape}")
    if not entries:
        raise ValueError("the changelog feed returned no entries")  # an empty feed is an outage, not news
    if not all(isinstance(e, dict) for e in entries):
        raise ValueError("the changelog feed holds an entry that is not a JSON object")
    return entries


# --------------------------------------------------------------------------
# State under .state/
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    """Where the watcher reads its baseline and keeps its state. Tests build one around a temp dir."""

    app_dir: Path
    index_path: Path

    @classmethod
    def default(cls) -> Paths:
        return cls(app_dir=APP_DIR, index_path=APP_DIR / "spec" / "spec_index.json")

    @property
    def state_dir(self) -> Path:
        return self.app_dir / ".state"

    @property
    def seen_file(self) -> Path:
        return self.state_dir / "changelog-seen.json"

    @property
    def alerts_file(self) -> Path:
        return self.state_dir / "alerts-sent.json"

    @property
    def spec_dir(self) -> Path:
        return self.state_dir / "spec"

    @property
    def reports_dir(self) -> Path:
        return self.state_dir / "reports"


def write_atomic(paths: Paths, target: Path, data: bytes) -> None:
    """Write `data` to `target` (tmp file, then rename: a crash cannot leave half a file). Refuses any
    target outside .state/, the only place the systemd unit may write."""
    root = paths.state_dir.resolve()
    if root not in target.resolve().parents:
        raise WatchError(f"refusing to write outside {root}: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def write_json(paths: Paths, target: Path, obj: Any) -> None:
    write_atomic(paths, target, (json.dumps(obj, indent=2) + "\n").encode("utf-8"))


def read_state(path: Path) -> dict | None:
    """The JSON object in a state file; None if the file does not exist. A file that exists but cannot be
    read as an object is an error, not an empty state: silently starting over would hide new entries or
    repeat alerts."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WatchError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise WatchError(f"{path} is not valid JSON ({exc}); fix it or delete it") from exc
    if not isinstance(data, dict):
        raise WatchError(f"{path} must hold a JSON object; fix it or delete it")
    return data


def load_seen(paths: Paths) -> list[str]:
    """The entry keys seen so far, oldest first. Empty when there is no state yet (the first run)."""
    data = read_state(paths.seen_file)
    seen = [] if data is None else data.get("seen", [])
    if not isinstance(seen, list) or not all(isinstance(k, str) for k in seen):
        raise WatchError(f"{paths.seen_file} has no valid 'seen' list; fix it or delete it (deleting re-baselines)")
    return seen


def load_alerts(paths: Paths) -> dict[str, Any]:
    """What was alerted, by dedupe key. Empty when nothing was alerted yet."""
    data = read_state(paths.alerts_file)
    alerts = {} if data is None else data.get("alerts", {})
    if not isinstance(alerts, dict):
        raise WatchError(f"{paths.alerts_file} has no valid 'alerts' object; fix it or delete it")
    return alerts


# --------------------------------------------------------------------------
# Spec check
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecDrift:
    live_hash: str
    baseline_hash: str
    saved: Path
    report: Path
    summary: str


def load_baseline_hash(index_path: Path) -> str:
    """contract_sha256 of the committed spec index: the contract this code was built against."""
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WatchError(f"cannot read the spec baseline {index_path}: {exc}") from exc
    except ValueError as exc:
        raise WatchError(f"the spec baseline {index_path} is not valid JSON: {exc}") from exc
    value = index.get("contract_sha256") if isinstance(index, dict) else None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise WatchError(
            f"the spec baseline {index_path} has no valid contract_sha256; regenerate it with scripts/spec_snapshot.py"
        )
    return value


def fetch_spec(http: httpx.Client) -> tuple[bytes, dict[str, Any]]:
    """The live spec: its raw bytes and the parsed document. No credentials: the spec is public."""
    r = http.get(SPEC_URL, headers={"Accept": "application/json", "User-Agent": spec_snapshot.USER_AGENT})
    r.raise_for_status()
    return r.content, spec_snapshot.parse_spec(r.content)


def diff_summary(result: Any) -> str:
    if not result.recorded_differences:
        return "No operation or schema that the spec index records differs: something outside the index changed."
    return (
        f"Operations: {len(result.ops_added)} added, {len(result.ops_removed)} removed, "
        f"{len(result.ops_changed)} changed. Schemas: {len(result.schemas_added)} added, "
        f"{len(result.schemas_removed)} removed, {len(result.schemas_changed)} changed."
    )


def spec_report(
    paths: Paths, raw: bytes, spec: dict[str, Any], live: str, baseline: str, saved: Path, detected: datetime
) -> tuple[str, str]:
    """(report text, one-line summary). The diff is the scripts/spec_diff.py report; if it cannot be built
    the report says so, because the alert must go out even then (the hash already proved a change)."""
    head = [
        "# Gorelo API contract changed",
        "",
        f"Detected by scripts/watch_gorelo_changelog.py at {detected.isoformat()}.",
        "",
        f"- Live contract sha256: `{live}`",
        f"- Baseline (spec/spec_index.json) contract sha256: `{baseline}`",
        f"- Saved live spec: `{saved}`",
        "",
        "Next: follow \"When the watcher reports an API change\" in docs/OPERATIONS.md "
        "(scripts/spec_snapshot.py, scripts/spec_diff.py, then the code).",
        "",
        "---",
        "",
    ]
    try:
        old = json.loads(paths.index_path.read_text(encoding="utf-8"))
        new, _warnings = spec_snapshot.build_index_with_warnings(spec, raw, SPEC_URL)
        result = spec_diff.diff_indexes(old, new)
        body = spec_diff.render_markdown(result, old, new, "spec/spec_index.json", str(saved))
        summary = diff_summary(result)
    except Exception as exc:  # noqa: BLE001 - any failure of the diff must not stop the alert
        body = (
            f"spec_diff could not be run ({type(exc).__name__}: {exc}).\n\n"
            f"Run it by hand: python scripts/spec_diff.py spec/spec_index.json {saved}\n"
        )
        summary = "The spec_diff report could not be built; the saved spec can be diffed by hand."
    return "\n".join(head) + "\n" + body, summary


def check_spec(http: httpx.Client, paths: Paths, detected: datetime) -> tuple[str, SpecDrift | None]:
    """(live contract hash, drift). Drift is None when the live hash equals the baseline; otherwise the new
    spec and its spec_diff report have been saved under .state/."""
    baseline = load_baseline_hash(paths.index_path)
    raw, spec = fetch_spec(http)
    live = spec_snapshot.contract_sha256(spec)
    if live == baseline:
        return live, None
    saved = paths.spec_dir / f"swagger-{live[:16]}.json"
    write_atomic(paths, saved, raw)
    report_text, summary = spec_report(paths, raw, spec, live, baseline, saved, detected)
    report = paths.reports_dir / f"spec-diff-{live[:16]}.md"
    write_atomic(paths, report, report_text.encode("utf-8"))
    return live, SpecDrift(live, baseline, saved, report, summary)


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------


@dataclass
class Change:
    """One API change that needs an alert."""

    key: str  # dedupe key in alerts-sent.json
    label: str  # one line for the console
    name: str  # the alert's Name
    description: str  # the alert's Description
    report: Path
    entry: str | None = None  # the changelog entry key, or None for a spec change
    sent: bool = False
    detail: str = ""  # why the alert was not sent


def changelog_report(entry: dict, terms: list[str], detected: datetime) -> str:
    lines = [
        "# Gorelo changelog entry flagged as API-related",
        "",
        f"- Detected: {detected.isoformat()}",
        f"- Title: {entry_title(entry)}",
        f"- Date: {entry_date(entry) or 'unknown'}",
        f"- Matched: {', '.join(terms)}",
        f"- Entry key: `{clean(entry_key(entry), 200)}`",
        f"- Changelog: {CHANGELOG_PAGE}",
        "",
        "## Entry text",
        "",
    ]
    shown: list[str] = []
    for field in _TEXT_FIELDS[1:]:
        text = clean_block(entry.get(field) or "", 6000)
        if text and text not in shown:
            shown.append(text)
    lines.append("\n\n".join(shown) if shown else "(the entry has no text besides its title)")
    lines += [
        "",
        "## Next",
        "",
        "API changes have appeared in the published spec before they appeared in the changelog; compare the live spec "
        "before relying on the changelog text: follow \"When the watcher reports an API change\" in docs/OPERATIONS.md "
        "(scripts/spec_snapshot.py, then scripts/spec_diff.py).",
    ]
    return "\n".join(lines) + "\n"


def changelog_change(paths: Paths, entry: dict, terms: list[str], detected: datetime) -> Change:
    key = entry_key(entry)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", key).strip("-")[:40] or "entry"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    report = paths.reports_dir / f"changelog-{slug}-{digest}.md"
    write_atomic(paths, report, changelog_report(entry, terms, detected).encode("utf-8"))
    title = entry_title(entry)
    when = entry_date(entry)
    description = "\n".join(
        [
            "A new entry in Gorelo's changelog looks API-related.",
            f"Title: {title}",
            *([f"Date: {when}"] if when else []),
            f"Matched: {', '.join(terms[:6])}",
            f"Report: {report}",
            f"Changelog: {CHANGELOG_PAGE}",
            "Next: compare the live spec (scripts/spec_snapshot.py, scripts/spec_diff.py) before trusting the changelog text.",
        ]
    )
    return Change(
        key=f"changelog:{key}",
        label=f"changelog entry \"{title}\" (matched: {', '.join(terms[:6])})",
        name=f"Gorelo changelog (API): {title}",
        description=description,
        report=report,
        entry=key,
    )


def spec_change(drift: SpecDrift) -> Change:
    description = "\n".join(
        [
            "The live Gorelo OpenAPI contract differs from spec/spec_index.json.",
            f"Live contract sha256: {drift.live_hash}",
            f"spec_index.json contract sha256: {drift.baseline_hash}",
            drift.summary,
            f"Report: {drift.report}",
            f"Saved spec: {drift.saved}",
            "Next: follow \"When the watcher reports an API change\" in docs/OPERATIONS.md.",
        ]
    )
    return Change(
        key=f"spec:{drift.live_hash}",
        label=f"live spec contract {drift.live_hash[:12]} differs from spec/spec_index.json ({drift.baseline_hash[:12]})",
        name=f"Gorelo API spec changed (contract {drift.live_hash[:12]})",
        description=description,
        report=drift.report,
    )


def post_alert(http: httpx.Client, api_key: str, alert_client_id: int, name: str, description: str) -> tuple[bool, str]:
    """POST one alert. (True, "") only when Gorelo answers 2xx with IsSuccess true and Data true; otherwise
    (False, why). Never raises for a network or Gorelo problem, and never repeats the request."""
    if not api_key:
        return False, f"{API_KEY_ENV} is not set in the environment"
    body = {
        "ClientId": alert_client_id,
        "Name": name,
        "Resource": ALERT_RESOURCE,
        "Severity": ALERT_SEVERITY,
        "Description": description,
    }
    try:
        # follow_redirects=False: a redirect must never carry the API key to another host
        response = http.post(
            ALERT_URL, json=body, headers={"X-API-Key": api_key, "Accept": "application/json"}, follow_redirects=False
        )
    except httpx.TimeoutException:
        return False, "the alert request timed out (the alert may or may not exist)"
    except httpx.HTTPError as exc:
        return False, f"the alert request failed ({type(exc).__name__})"
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not 200 <= response.status_code < 300:
        return False, f"Gorelo answered HTTP {response.status_code}{_notes(payload)}"
    if not (isinstance(payload, dict) and payload.get("IsSuccess") is True and payload.get("Data") is True):
        return False, f"Gorelo answered HTTP {response.status_code} but not with IsSuccess true and Data true{_notes(payload)}"
    return True, ""


def _notes(payload: Any) -> str:
    """Gorelo's own explanation (the first Notification messages and the trace id), if the body has any."""
    if not isinstance(payload, dict):
        return ""
    notes = payload.get("Notifications")
    messages = [
        clean(n["Message"], 120) for n in (notes if isinstance(notes, list) else [])[:3]
        if isinstance(n, dict) and n.get("Message")
    ]
    context = payload.get("DataContext")
    trace = context.get("TraceId") if isinstance(context, dict) else None
    parts = []
    if messages:
        parts.append("; ".join(messages))
    if trace:
        parts.append(f"trace {clean(trace, 80)}")
    return f" ({', '.join(parts)})" if parts else ""


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def run(
    *,
    quiet: bool = False,
    reset: bool = False,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    paths: Paths | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    """One full check. `env`, `transport`, `paths` and `now` exist for the tests; the CLI uses the real ones."""
    env = os.environ if env is None else env
    paths = paths or Paths.default()
    clock = now or (lambda: datetime.now(timezone.utc))

    def say(text: str) -> None:
        if not quiet:
            print(text)

    try:
        alert_client_id = site_config.watcher_alert_client()  # no site config, no run: the alert client must never be guessed
    except site_config.SiteConfigError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return EXIT_FAILURE

    try:
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        if not os.access(paths.state_dir, os.W_OK | os.X_OK):
            raise WatchError(f"{paths.state_dir} is not writable (the unit needs ReadWritePaths for it)")
        seen = load_seen(paths)
        alerts = load_alerts(paths)
    except (OSError, WatchError) as exc:
        print(f"ERROR cannot use the watcher state: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    failures: list[str] = []
    entries: list[dict] | None = None
    baseline = False
    new: list[tuple[dict, list[str]]] = []
    live_hash: str | None = None
    drift: SpecDrift | None = None
    changes: list[Change] = []

    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, transport=transport) as http:
        # 1. The changelog.
        try:
            entries = fetch_entries(http)
        except CHECK_ERRORS as exc:
            failures.append(f"fetching Gorelo changelog: {type(exc).__name__}: {exc}")
        else:
            known = set(seen)
            baseline = reset or not seen
            if not baseline:
                # An entry that was alerted before is not new, whatever the seen list says, and an entry
                # listed twice in one feed counts once.
                for e in entries:
                    key = entry_key(e)
                    if key not in known and f"changelog:{key}" not in alerts:
                        known.add(key)
                        new.append((e, api_terms(e)))

        # 2. The spec.
        try:
            live_hash, drift = check_spec(http, paths, clock())
        except CHECK_ERRORS as exc:
            failures.append(f"checking the Gorelo spec: {type(exc).__name__}: {exc}")

        # 3. What needs an alert. A change that was alerted before never does. A failure to write a report
        # is not caught here on purpose: the run crashes (exit 3) before it alerts or remembers anything,
        # so the next run starts over with nothing lost.
        spec_already_reported = False
        for entry, terms in new:
            if terms:
                changes.append(changelog_change(paths, entry, terms, clock()))
        if drift is not None:
            if f"spec:{drift.live_hash}" in alerts:
                spec_already_reported = True
            else:
                changes.append(spec_change(drift))

        # 4. The alerts: one per change, each recorded as soon as Gorelo confirms it.
        api_key = (env.get(API_KEY_ENV) or "").strip()
        for change in changes:
            change.sent, change.detail = post_alert(http, api_key, alert_client_id, change.name, change.description)
            if change.sent:
                alerts[change.key] = {"alerted_at": clock().isoformat(), "name": change.name, "report": str(change.report)}
                write_json(paths, paths.alerts_file, {"alerts": alerts})

    # 5. Remember the feed. An API-related entry whose alert failed is held back, so the next run finds it again.
    if entries is not None:
        held_back = {c.entry for c in changes if c.entry is not None and not c.sent}
        recorded = [k for k in dict.fromkeys(entry_key(e) for e in entries) if k not in held_back]
        keep = [] if reset else [k for k in seen if k not in set(recorded)]
        write_json(
            paths,
            paths.seen_file,
            {"seen": (keep + recorded)[-MAX_SEEN:], "last_checked": clock().isoformat()},
        )

    # 6. Say what happened.
    for failure in failures:
        print(f"ERROR {failure}", file=sys.stderr)
    if entries is not None and baseline:
        print(f"baseline recorded: {len(entries)} entries known, nothing reported")
    elif entries is not None and not new:
        say(f"no new changelog entries ({len(entries)} known)")
    if new:
        flagged = sum(1 for _, terms in new if terms)
        print(
            f"{len(new)} NEW Gorelo changelog entr{'y' if len(new) == 1 else 'ies'}"
            f"{f', {flagged} API-RELATED' if flagged else ''}"
        )
        for entry, terms in new:
            print(f"{'  [API]' if terms else '       '} {entry_date(entry)}  {entry_title(entry)}")
    for change in changes:
        print(f"API CHANGE: {change.label}")
        if change.sent:
            print(f"  alert posted to Gorelo (client {alert_client_id}, resource {ALERT_RESOURCE}, severity {ALERT_SEVERITY})")
        else:
            print(
                f"ALERT NOT SENT for {change.label}: {change.detail}; the change stays pending, the next run tries again",
                file=sys.stderr,
            )
        print(f"  report: {change.report}")
    if live_hash is not None and drift is None:
        say(f"spec contract unchanged ({live_hash[:12]})")
    elif spec_already_reported and drift is not None:
        say(f"spec contract {drift.live_hash[:12]} still differs from spec/spec_index.json; already reported, no new alert")
    if changes:
        print()
        print(
            "An API change was found. API changes have appeared in the published spec before they appeared in the "
            "changelog; compare the live spec before relying on the changelog text: follow \"When the watcher reports "
            "an API change\" in docs/OPERATIONS.md (scripts/spec_snapshot.py, then scripts/spec_diff.py)."
        )
        print(f"Changelog: {CHANGELOG_PAGE}")

    if changes:
        return EXIT_API_CHANGE
    if failures:
        return EXIT_FAILURE
    if new:
        return EXIT_NEWS_ONLY
    return EXIT_NOTHING_NEW


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Watch Gorelo's changelog and live OpenAPI spec; post a Gorelo alert for each new API change.",
        epilog="Exit codes: 0 nothing new, 1 new API change, 2 new non-API changelog entries, 3 failure.",
    )
    ap.add_argument("--quiet", action="store_true", help="only print when something is new")
    ap.add_argument(
        "--reset",
        action="store_true",
        help="re-baseline the changelog against the current feed (nothing is reported for it); the spec check still runs",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(quiet=args.quiet, reset=args.reset)
    except Exception:  # noqa: BLE001 - a crash must exit 3, never 1 (which means "API change")
        traceback.print_exc()
        print("ERROR the Gorelo watcher crashed (traceback above); this says nothing about the API", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")  # a title with an odd character must not crash the report
    sys.exit(main())
