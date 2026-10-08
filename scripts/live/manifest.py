"""The run manifest of the live harness: every record a live run creates, written down BEFORE and AFTER the call.

One run, one file: .live-runs/<run id>.json (git-ignored). A run id looks like MCPTEST-20991002101500 (UTC
time) and every label a run gives a record starts with it (enforced), so whatever a run left behind can be
found in Gorelo by searching for the run id.

Write discipline (the harness follows it; the guard and cleanup rely on it):

    intent(kind, label, details)       before every create call; returns the intent number
    created(kind, id, label, details)  right after, with the id Gorelo returned
    intent_failed(intent, reason)      when the create was refused and nothing was created
    cleaned(kind, id, outcome)         when cleanup deleted the record (or found it gone)
    cleanup_failed(kind, id, outcome)  when a cleanup attempt did not remove it (it stays a leftover)

An intent that never became a record (a timeout after the request left, a crash) is reported by
unresolved_intents(): the create may have succeeded, so search Gorelo for the label.

Every change is written to disk at once and atomically (temp file in the same directory, fsync, os.replace),
so a crash leaves the old file or the new one, never half of one. If the write itself fails the in-memory
state keeps the change and the error is raised: an intent that cannot be persisted must stop the create.
One process at a time may write a manifest (there is no cross-process locking).

The guard and cleanup read these `details` keys, so the harness must record them:

    ticket             contact_id (int or None, REQUIRED: the guard's public-comment rule), cc_contact_ids (list);
                       ContactId and CcContactIds are read too. These are the audience the STORED ticket has
                       (the write matrix adds them with update_details after it read the ticket back), so a
                       ticket whose read-back was never verified has none and gets no public comment
    comment            ticket_id; private (bool, default True; a non-private comment cannot be deleted, it goes
                       with its ticket)
    project_comment    project_id; task_id when it is a task comment
    task, section      project_id
    side_conversation, approval
                       the record they belong to: ticket_id, or project_id and task_id for a task's. The guard
                       lets a side conversation (approval) comment go only into one whose ticket_id (task_id)
                       is the ticket (task) of the request path
    attachment         the parent: ticket_id, or project_id (and task_id); an uploaded file cannot be deleted
                       through the API, so cleanup reports it as known undeletable once its parent is deleted
    invoice            status_id (the status Gorelo STORED; REQUIRED for any DELETE of an invoice: 1 for a Draft, which
                       the guard lets be deleted, and 5 for the one Approved invoice of the approved_invoice area,
                       which the guard lets be voided only with allow_approved_invoice; 4 once it was voided, 3 when
                       Gorelo recorded it as Paid; for an invoice the cleanup found by its label search, the status the
                       search found; when the answer has none the fallback is 1 for a Draft and 5 for the Approved
                       invoice, never 1 for the latter), number (int or None: the invoice number it used up) and
                       display_number (str or None, for example INV-1042). Cleanup deletes a Draft only after reading
                       it back as a Draft, and voids an Approved invoice only after reading it back as Approved and
                       only with --void-approved. A voided invoice cannot be removed: it stays listed as Void, its
                       outcome ends with "(still listed as Void)", and cleanup counts it as a known residue (neither
                       cleaned nor a leftover), like an uploaded file

`id` is an int for client, contact and time_entry, a UUID for ticket, comment, item, uptime, project,
section, task, project_comment and invoice, and any short token for attachment, side_conversation and approval.
Never put secrets in `details` (the manifest refuses keys that look like one). An uploaded attachment's url
carries an access token: record its name, not its url.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KINDS: tuple[str, ...] = (
    "client",
    "contact",
    "ticket",
    "comment",
    "time_entry",
    "item",
    "uptime",
    "project",
    "section",
    "task",
    "project_comment",
    "attachment",
    "side_conversation",
    "approval",
    "invoice",
)
# "int": a positive integer. "uuid": a UUID (stored lowercase, hyphenated). "any": either of those or a short token.
ID_TYPES: dict[str, str] = {
    "client": "int",
    "contact": "int",
    "time_entry": "int",
    "ticket": "uuid",
    "comment": "uuid",
    "item": "uuid",
    "uptime": "uuid",
    "project": "uuid",
    "section": "uuid",
    "task": "uuid",
    "project_comment": "uuid",
    "attachment": "any",
    "side_conversation": "any",
    "approval": "any",
    "invoice": "uuid",
}
# the kinds of records that exist before a run (listed leftovers of earlier probes)
LEFTOVER_KINDS = ("client", "contact")

RUN_PREFIX = "MCPTEST-"
RUN_ID_PATTERN = re.compile(r"MCPTEST-[0-9]{14}")
FORMAT_VERSION = 1
RUNS_DIR = Path(__file__).resolve().parents[2] / ".live-runs"

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_DIGITS = re.compile(r"[0-9]{1,18}")
_SECRET_KEYS = frozenset(
    {"apikey", "xapikey", "authorization", "password", "secret", "token", "accesstoken", "bearer", "credentials"}
)
_MAX_TEXT = 500
_STATUS_OPEN, _STATUS_CREATED, _STATUS_FAILED = "open", "created", "failed"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_id(kind: str, value: Any) -> int | str:
    """The canonical form of an id of `kind`: an int, a lowercase hyphenated UUID, or a short token.

    Digit strings become ints and UUID text is lowercased, so ids from a path, a body and a manifest
    compare equal exactly when they name the same record. A bool, a float, None, a non-positive int or a
    value of the wrong shape for the kind raises ValueError (it never guesses).
    """
    id_type = ID_TYPES.get(kind)
    if id_type is None:
        raise ValueError(f"unknown record kind {kind!r}; the kinds are {', '.join(KINDS)}")
    number: int | None = None
    guid: str | None = None
    token: str | None = None
    if isinstance(value, bool):
        pass
    elif isinstance(value, int):
        number = value
    elif isinstance(value, uuid.UUID):
        guid = str(value)
    elif isinstance(value, str):
        text = value.strip()
        if _DIGITS.fullmatch(text):
            number = int(text)
        elif _UUID.fullmatch(text):
            guid = text.lower()
        elif text and len(text) <= 300 and text.isprintable():
            token = text
    if number is not None and number >= 1 and id_type in ("int", "any"):
        return number
    if guid is not None and id_type in ("uuid", "any"):
        return guid
    if token is not None and id_type == "any":
        return token
    wanted = {"int": "a positive integer", "uuid": "a UUID", "any": "an integer, a UUID or a short token"}[id_type]
    raise ValueError(f"a {kind} id must be {wanted}")


@dataclass(frozen=True)
class Record:
    """A record a run created, as a snapshot (changing it changes nothing in the manifest).

    Fields are read as attributes (record.kind) or by name (record["kind"])."""

    seq: int
    kind: str
    id: int | str
    label: str
    details: dict[str, Any]
    created_at: str
    cleaned: bool
    outcome: str | None
    attempts: tuple[dict[str, Any], ...]

    def __getitem__(self, name: str) -> Any:
        if name not in self.__dataclass_fields__:
            raise KeyError(name)
        return getattr(self, name)


@dataclass(frozen=True)
class Intent:
    """A create that was announced. status: open (no answer yet), created or failed. Read like a Record."""

    seq: int
    kind: str
    label: str
    details: dict[str, Any]
    at: str
    status: str
    reason: str | None

    def __getitem__(self, name: str) -> Any:
        if name not in self.__dataclass_fields__:
            raise KeyError(name)
        return getattr(self, name)


def _outcome_text(outcome: Any) -> str:
    if not isinstance(outcome, str) or not outcome.strip():
        raise ValueError("outcome must be a non-empty string")
    text = " ".join(outcome.split())
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 3] + "..."


def _reject_secret_keys(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            squashed = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if squashed in _SECRET_KEYS or squashed.endswith("apikey"):
                raise ValueError(f"details key {str(key)!r} looks like a secret; the manifest never stores secrets")
            _reject_secret_keys(value)
    elif isinstance(node, list):
        for item in node:
            _reject_secret_keys(item)


def _clean_details(details: Mapping[str, Any] | None) -> dict[str, Any]:
    """A JSON-safe deep copy of `details` (so the caller cannot change the manifest afterwards)."""
    if details is None:
        return {}
    if not isinstance(details, Mapping):
        raise TypeError("details must be a mapping of names to JSON values")
    try:
        text = json.dumps(details, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"details must be JSON serializable: {exc}") from None
    cleaned = json.loads(text)
    _reject_secret_keys(cleaned)
    return cleaned


def _check_kind(kind: Any) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown record kind {kind!r}; the kinds are {', '.join(KINDS)}")
    return kind


def _short(text: Any) -> str:
    shown = str(text)
    return shown if len(shown) <= 80 else shown[:77] + "..."


def _validate_state(state: Any, source: str) -> dict[str, Any]:
    """Check a state read from disk; raises ValueError naming the first problem."""

    def bad(problem: str) -> ValueError:
        return ValueError(f"{source} is not a valid run manifest: {problem}")

    if not isinstance(state, dict):
        raise bad("the top level is not an object")
    if state.get("format") != FORMAT_VERSION:
        raise bad(f"format is {state.get('format')!r}, expected {FORMAT_VERSION}")
    run_id = state.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise bad("run_id is missing or malformed")
    for section in ("intents", "records", "approved_leftovers"):
        if not isinstance(state.get(section), list):
            raise bad(f"{section} is missing or not a list")
    if not isinstance(state.get("next_seq"), int) or isinstance(state.get("next_seq"), bool):
        raise bad("next_seq is missing or not an integer")
    seen: set[tuple[str, int | str]] = set()
    for record in state["records"]:
        if not isinstance(record, dict):
            raise bad("a record is not an object")
        try:
            kind = _check_kind(record.get("kind"))
            key = normalize_id(kind, record.get("id"))
        except ValueError as exc:
            raise bad(f"a record is invalid ({exc})") from None
        if (kind, key) in seen:
            raise bad(f"{kind} {key} appears twice")
        seen.add((kind, key))
        label = record.get("label")
        if not isinstance(label, str) or not label.startswith(run_id):
            raise bad(f"the label of {kind} {key} does not start with the run id")
        if not isinstance(record.get("details"), dict) or not isinstance(record.get("attempts"), list):
            raise bad(f"{kind} {key} has no details or attempts")
        if not isinstance(record.get("cleaned"), bool) or not isinstance(record.get("seq"), int):
            raise bad(f"{kind} {key} has no valid cleaned flag or seq")
    for intent in state["intents"]:
        if not isinstance(intent, dict) or intent.get("kind") not in KINDS:
            raise bad("an intent is invalid")
        label = intent.get("label")
        if not isinstance(label, str) or not label.startswith(run_id):
            raise bad("the label of an intent does not start with the run id")
        if intent.get("status") not in (_STATUS_OPEN, _STATUS_CREATED, _STATUS_FAILED):
            raise bad("an intent has an unknown status")
    for entry in state["approved_leftovers"]:
        if not isinstance(entry, dict) or entry.get("kind") not in LEFTOVER_KINDS:
            raise bad("an listed leftover entry is invalid")
    return state


class Manifest:
    """What one live run created. See the module docstring for the write discipline.

    `Manifest(run_id, path)` creates the file (path defaults to .live-runs/<run id>.json) or, when it exists,
    loads it and checks that it belongs to `run_id`. `Manifest.start()` makes a NEW run (fresh id, refuses to
    reuse a file) and `Manifest.load(path)` opens an existing one (cleanup).
    """

    def __init__(self, run_id: str, path: str | os.PathLike[str] | None = None) -> None:
        if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError(f"run id {_short(run_id)!r} must look like MCPTEST-YYYYMMDDHHMMSS")
        self.run_id = run_id
        self.path = Path(path) if path is not None else RUNS_DIR / f"{run_id}.json"
        self._index: dict[tuple[str, int | str], dict[str, Any]] = {}
        if self.path.exists():
            state = _validate_state(self._read(), str(self.path))
            if state["run_id"] != run_id:
                raise ValueError(f"{self.path} belongs to run {state['run_id']}, not {run_id}")
            self._state = state
            for record in state["records"]:
                self._index[(record["kind"], normalize_id(record["kind"], record["id"]))] = record
        else:
            self._state = {
                "format": FORMAT_VERSION,
                "run_id": run_id,
                "started_at": _stamp(),
                "updated_at": _stamp(),
                "next_seq": 1,
                "intents": [],
                "records": [],
                "approved_leftovers": [],  # key name kept for compatibility with manifests already written
            }
            self._write()

    # -- construction ------------------------------------------------------

    @staticmethod
    def new_run_id(now: datetime | None = None) -> str:
        """MCPTEST-YYYYMMDDHHMMSS for `now` (default: the current UTC time; a naive time counts as UTC)."""
        moment = now if now is not None else _utc_now()
        if moment.tzinfo is not None:
            moment = moment.astimezone(timezone.utc)
        return RUN_PREFIX + moment.strftime("%Y%m%d%H%M%S")

    @classmethod
    def start(cls, now: datetime | None = None, *, directory: str | os.PathLike[str] | None = None) -> Manifest:
        """A NEW run: a fresh run id and an empty manifest file. Refuses to reuse an existing file."""
        run_id = cls.new_run_id(now)
        path = (Path(directory) if directory is not None else RUNS_DIR) / f"{run_id}.json"
        if path.exists():
            raise FileExistsError(f"{path} already exists; wait a second or resume it with Manifest.load()")
        return cls(run_id, path)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Manifest:
        """Open an existing manifest file (FileNotFoundError if it is missing, ValueError if it is not valid)."""
        target = Path(path)
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{target} is not a valid run manifest: not UTF-8 text") from None
        try:
            state = json.loads(text)
        except ValueError:
            raise ValueError(f"{target} is not a valid run manifest: invalid JSON") from None
        return cls(_validate_state(state, str(target))["run_id"], target)

    def label(self, text: str = "") -> str:
        """A label for a record of this run: the run id, then `text` after a space."""
        if not isinstance(text, str) or any(ord(ch) < 32 for ch in text):
            raise ValueError("label text must be a string without control characters")
        text = text.strip()
        return f"{self.run_id} {text}" if text else self.run_id

    # -- recording ---------------------------------------------------------

    def intent(self, kind: str, label: str, details: Mapping[str, Any] | None = None) -> int:
        """Announce a create BEFORE calling Gorelo; returns the intent number."""
        kind = _check_kind(kind)
        label = self._check_label(label)
        cleaned = _clean_details(details)
        seq = self._take_seq()
        self._state["intents"].append(
            {
                "seq": seq,
                "kind": kind,
                "label": label,
                "details": cleaned,
                "at": _stamp(),
                "status": _STATUS_OPEN,
                "reason": None,
                "record_seq": None,
            }
        )
        self._save()
        return seq

    def intent_failed(self, intent: int, reason: str) -> None:
        """The create announced by `intent` was refused: nothing was created, so it is not an orphan."""
        entry = self._find_intent(intent)
        if entry["status"] == _STATUS_CREATED:
            raise ValueError(f"intent {intent} already produced a record")
        entry["status"] = _STATUS_FAILED
        entry["reason"] = _outcome_text(reason)
        self._save()

    def created(
        self, kind: str, id: Any, label: str, details: Mapping[str, Any] | None = None
    ) -> Record:
        """Record a created record with the id Gorelo returned. Settles the oldest open intent of that kind and label."""
        kind = _check_kind(kind)
        key = normalize_id(kind, id)
        label = self._check_label(label)
        cleaned = _clean_details(details)
        if (kind, key) in self._index:
            raise ValueError(f"{kind} {key} is already recorded in this manifest")
        seq = self._take_seq()
        record = {
            "seq": seq,
            "kind": kind,
            "id": key,
            "label": label,
            "details": cleaned,
            "created_at": _stamp(),
            "cleaned": False,
            "outcome": None,
            "attempts": [],
        }
        self._state["records"].append(record)
        self._index[(kind, key)] = record
        for entry in self._state["intents"]:
            if entry["status"] == _STATUS_OPEN and entry["kind"] == kind and entry["label"] == label:
                entry["status"] = _STATUS_CREATED
                entry["record_seq"] = seq
                break
        self._save()
        return self._view(record)

    def update_details(self, kind: str, id: Any, **changes: Any) -> None:
        """Merge `changes` into the details of a record (for example a ticket whose contact was changed)."""
        record = self._lookup(kind, id)
        merged = {**record["details"], **_clean_details(changes)}
        record["details"] = _clean_details(merged)
        self._save()

    def cleaned(self, kind: str, id: Any, outcome: str) -> None:
        """The record was deleted (or was already gone). `outcome` says how, for example "Deleted"."""
        record = self._lookup(kind, id)
        text = _outcome_text(outcome)
        record["cleaned"] = True
        record["outcome"] = text
        record["attempts"].append({"at": _stamp(), "outcome": text, "ok": True})
        self._save()

    def cleanup_failed(self, kind: str, id: Any, outcome: str) -> None:
        """A cleanup attempt did not remove the record: it stays in leftovers() with this outcome."""
        record = self._lookup(kind, id)
        text = _outcome_text(outcome)
        record["outcome"] = text
        record["attempts"].append({"at": _stamp(), "outcome": text, "ok": False})
        self._save()

    def leftover_outcome(self, kind: str, id: Any, outcome: str, *, deleted: bool) -> None:
        """What cleanup did with an listed leftover of an EARLIER run (not a record of this run).

        These ids are kept apart from the records: they never join ids(kind), so the guard never treats
        them as created by this run."""
        if kind not in LEFTOVER_KINDS:
            raise ValueError(f"listed leftovers are {' or '.join(LEFTOVER_KINDS)} records, not {kind!r}")
        key = normalize_id(kind, id)
        self._state["approved_leftovers"].append(
            {"kind": kind, "id": key, "outcome": _outcome_text(outcome), "deleted": bool(deleted), "at": _stamp()}
        )
        self._save()

    # -- reading -----------------------------------------------------------

    def ids(self, kind: str) -> set[int | str]:
        """The canonical ids of every record of `kind` this run created (cleaned or not)."""
        kind = _check_kind(kind)
        return {key[1] for key in self._index if key[0] == kind}

    def details(self, kind: str, id: Any) -> dict[str, Any]:
        """A copy of the details recorded for a record; KeyError if the run did not create it."""
        return copy.deepcopy(self._lookup(kind, id)["details"])

    def record(self, kind: str, id: Any) -> Record:
        return self._view(self._lookup(kind, id))

    def all_created(self) -> list[Record]:
        """Every record in creation order."""
        return [self._view(record) for record in self._state["records"]]

    def leftovers(self) -> list[Record]:
        """The records that were created and not cleaned, in creation order."""
        return [self._view(record) for record in self._state["records"] if not record["cleaned"]]

    def unresolved_intents(self) -> list[Intent]:
        """Creates that were announced and neither recorded nor reported failed: they may exist in Gorelo."""
        return [self._intent_view(entry) for entry in self._state["intents"] if entry["status"] == _STATUS_OPEN]

    def approved_leftover_outcomes(self) -> list[dict[str, Any]]:
        """What cleanup did with configured earlier leftovers, oldest first."""
        return copy.deepcopy(self._state["approved_leftovers"])

    def summary(self) -> dict[str, int]:
        records = self._state["records"]
        return {
            "created": len(records),
            "cleaned": sum(1 for record in records if record["cleaned"]),
            "leftovers": sum(1 for record in records if not record["cleaned"]),
            "unresolved_intents": len(self.unresolved_intents()),
        }

    def __repr__(self) -> str:
        return f"Manifest({self.run_id!r}, records={len(self._state['records'])}, path={str(self.path)!r})"

    # -- internals ---------------------------------------------------------

    def _check_label(self, label: Any) -> str:
        if not isinstance(label, str) or not label.startswith(self.run_id):
            raise ValueError(f"label {_short(label)!r} must start with the run id {self.run_id}")
        if any(ord(ch) < 32 for ch in label):
            raise ValueError("label must not contain control characters")
        return label

    def _take_seq(self) -> int:
        seq = self._state["next_seq"]
        self._state["next_seq"] = seq + 1
        return seq

    def _lookup(self, kind: str, id: Any) -> dict[str, Any]:
        kind = _check_kind(kind)
        key = normalize_id(kind, id)
        try:
            return self._index[(kind, key)]
        except KeyError:
            raise KeyError(f"this run did not create {kind} {key}") from None

    def _find_intent(self, seq: int) -> dict[str, Any]:
        for entry in self._state["intents"]:
            if entry["seq"] == seq:
                return entry
        raise KeyError(f"no intent number {seq}")

    @staticmethod
    def _view(record: dict[str, Any]) -> Record:
        return Record(
            seq=record["seq"],
            kind=record["kind"],
            id=record["id"],
            label=record["label"],
            details=copy.deepcopy(record["details"]),
            created_at=record["created_at"],
            cleaned=record["cleaned"],
            outcome=record["outcome"],
            attempts=tuple(dict(attempt) for attempt in record["attempts"]),
        )

    @staticmethod
    def _intent_view(entry: dict[str, Any]) -> Intent:
        return Intent(
            seq=entry["seq"],
            kind=entry["kind"],
            label=entry["label"],
            details=copy.deepcopy(entry["details"]),
            at=entry["at"],
            status=entry["status"],
            reason=entry["reason"],
        )

    def _read(self) -> Any:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            raise ValueError(f"{self.path} is not a valid run manifest: not UTF-8 text") from None
        except ValueError:
            raise ValueError(f"{self.path} is not a valid run manifest: invalid JSON") from None

    def _save(self) -> None:
        self._state["updated_at"] = _stamp()
        self._write()

    def _write(self) -> None:
        """Write the whole state atomically: temp file next to the target, fsync, os.replace, fsync the directory."""
        text = json.dumps(self._state, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise
        with contextlib.suppress(OSError):
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
