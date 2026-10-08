"""Typed access to spec/spec_index.json and local validation of requests against it.

spec/spec_index.json is generated from Gorelo's live OpenAPI spec by scripts/spec_snapshot.py (see
its docstring for the format). This module never hand-edits it and never touches the network.

Live overrides. The published spec is sometimes wrong about the live API (on 2026-10-02 it listed the
collection forms of the client and contact updates, which answered 405 once the id moved into the path; Gorelo
published the path forms the same day, so that override is retired). A file spec/live_overrides.json next to
the index lists the operations to swap, with the evidence for each: load_spec_index() applies it, so every
consumer (the client, the tools, the live guard, the tests) sees the live operation and never the published
one. replace_ops may be empty (nothing is swapped), and a `retired` section holds the history of overrides that
were dropped: the loader ignores it. The raw index stays a mirror of the published spec and is never edited.
SpecIndex.override_ops says which loaded operations came from the overrides and which published operation
each replaced. When the published spec catches up (the index itself contains a live key) loading fails with a
message that says to drop the override (move the entry to the `retired` section with `retired_on`, `reason`,
`published_as`, `replaced` and its old `evidence`: the five keys of every retired entry), because only a person can
re-verify the live API; a hash check of the published spec cannot see this kind of drift. See apply_live_overrides()
for the rules.

Gorelo rejects unknown query parameters and unknown body fields with a 400, and for a body field
the error cannot even say which field it was (see docs/API-OBSERVED-BEHAVIOR.md). So every query name, body
field and path id is checked HERE, before any request is sent:

    op = load_spec_index().op("POST /v1/clients")
    validate_body(op, {"Name": "Acme", "Location": {"Name": "HQ", "Phonee": "x"}})
    # SpecViolation: field "Location.Phonee", message lists the allowed names

Query names are case-insensitive on Gorelo's side, so normalize_query() returns each name with the
spec's own casing; a name is checked even when its value is None. Body field names are
case-sensitive (exact PascalCase) and the SHAPE of a value is checked too: an array field takes a
list, an object field takes an object. validate_body() returns the names of the schema fields that
were present: they are the only body names that may be logged. validate_path_param() checks one path
id against the spec's type for it (uuid, integer or a plain token) and returns the text to put in
the URL.
"""

from __future__ import annotations

import copy
import difflib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

DEFAULT_INDEX_PATH = Path(__file__).resolve().parent / "spec" / "spec_index.json"
# The live overrides of an index are the file of this name in the same directory, when there is one.
OVERRIDES_FILENAME = "live_overrides.json"
DEFAULT_OVERRIDES_PATH = DEFAULT_INDEX_PATH.with_name(OVERRIDES_FILENAME)

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_MAX_NAMES_IN_MESSAGE = 60
_PRIMITIVE_TYPES = frozenset({"string", "integer", "number", "boolean"})
_LIST_INDEX = re.compile(r"\[\d+\]")
# ASCII only on purpose: \d and str.isdigit() accept other scripts' digits.
_DIGITS = re.compile(r"[0-9]+")
_UUID_HYPHENATED = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_UUID_BARE = re.compile(r"[0-9a-fA-F]{32}")
_INT_LIMITS = {"int32": 2**31 - 1}
_INT_LIMIT_DEFAULT = 2**63 - 1


class SpecViolation(ValueError):
    """A request does not match the spec index (unknown, misspelled or misplaced name).

    Attributes:
        op_key: the operation, for example "POST /v1/clients".
        field: the offending name. Dotted for nested body fields ("Location.Phonee"), with an
            element index for list items ("SubItems[0].Quantityy"). Empty if no single name applies.
    """

    def __init__(self, op_key: str, field: str, message: str):
        super().__init__(message)
        self.op_key = op_key
        self.field = field

    def __reduce__(self) -> tuple[Any, ...]:
        return (SpecViolation, (self.op_key, self.field, str(self)))


@dataclass(frozen=True, eq=False)
class OpSpec:
    """One operation of the spec index. Names keep the spec's exact casing."""

    key: str
    method: str
    path: str
    query_params: dict[str, Any]
    path_params: dict[str, Any]
    paged: bool
    page_size_rule: str | None
    body: dict[str, Any] | None
    response: dict[str, Any]
    # The index's component schemas, so validate_body can follow a field's "ref".
    schemas: Mapping[str, Any] = field(default_factory=dict, repr=False)
    # For an operation that came from the live overrides: the published operation key it replaced.
    overridden_from: str | None = None

    @property
    def is_live_override(self) -> bool:
        """True when this operation is a live override and not what the published spec says."""
        return self.overridden_from is not None

    @property
    def path_placeholders(self) -> tuple[str, ...]:
        """The {placeholder} names of the path, in order."""
        return tuple(_PLACEHOLDER.findall(self.path))

    @property
    def is_multipart(self) -> bool:
        return bool(self.body) and str(self.body.get("content_type", "")).lower().startswith("multipart/")

    @property
    def is_binary(self) -> bool:
        return self.response.get("kind") == "binary"


class SpecIndex:
    """The parsed spec index: `ops` (key to OpSpec) and component `schemas`.

    `overrides` is a parsed live overrides document (see apply_live_overrides): the operations it replaces are
    swapped before anything is read, `override_ops` maps each live operation key to the published key it
    replaced, and `overrides_path` is the file it came from (None when there were no overrides). load_spec_index
    passes the overrides that sit next to the index; SpecIndex(data) alone applies none. `sha256` and
    `contract_sha256` always describe the published spec.
    """

    def __init__(
        self,
        data: Mapping[str, Any],
        *,
        path: Path | None = None,
        overrides: Any = None,
        overrides_path: Path | None = None,
    ):
        replaced: dict[str, str] = {}
        if overrides is not None:
            source = str(overrides_path) if overrides_path is not None else "live overrides"
            data, replaced = apply_live_overrides(data, overrides, source=source)
        raw_ops = data.get("ops")
        if not isinstance(raw_ops, Mapping) or not raw_ops:
            raise ValueError("not a Gorelo spec index: the 'ops' object is missing or empty")
        raw_schemas = data.get("schemas")
        self._schemas: dict[str, Any] = dict(raw_schemas) if isinstance(raw_schemas, Mapping) else {}
        self.path = path
        self.overrides_path = overrides_path if overrides is not None else None
        self.source: str | None = data.get("source")
        self.openapi: str | None = data.get("openapi")
        self.sha256: str | None = data.get("sha256")
        self.contract_sha256: str | None = data.get("contract_sha256")
        ops: dict[str, OpSpec] = {}
        for key, entry in raw_ops.items():
            ops[key] = OpSpec(
                key=key,
                method=entry["method"],
                path=entry["path"],
                query_params=dict(entry.get("query_params") or {}),
                path_params=dict(entry.get("path_params") or {}),
                paged=bool(entry.get("paged")),
                page_size_rule=entry.get("page_size_rule"),
                body=entry.get("body"),
                response=dict(entry.get("response") or {}),
                schemas=self._schemas,
                overridden_from=replaced.get(key),
            )
        self.ops: Mapping[str, OpSpec] = MappingProxyType(ops)
        self.override_ops: Mapping[str, str] = MappingProxyType(dict(replaced))

    def op(self, key: str) -> OpSpec:
        """The operation for a key such as "GET /v1/tickets/{ticketId}". KeyError if unknown."""
        found = self.ops.get(key)
        if found is not None:
            return found
        close = difflib.get_close_matches(str(key), list(self.ops), n=3, cutoff=0.6)
        hint = f" Closest keys: {', '.join(close)}." if close else ""
        raise KeyError(
            f"unknown Gorelo operation {key!r}: it is not in the spec index "
            f"({len(self.ops)} operations; keys look like 'GET /v1/tickets/{{ticketId}}').{hint}"
        )

    def schema(self, name: str) -> dict[str, Any]:
        """A component schema entry ({"type", "required", "fields", ...}). KeyError if unknown."""
        try:
            return self._schemas[name]
        except KeyError:
            raise KeyError(f"unknown Gorelo schema {name!r}: it is not in the spec index") from None

    @property
    def schemas(self) -> Mapping[str, Any]:
        return MappingProxyType(self._schemas)


_CACHE: dict[tuple[Any, ...], SpecIndex] = {}


def _stamp(path: Path) -> tuple[int, int]:
    """What changes when a file is rewritten: its mtime and size."""
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def _overrides_file(target: Path, overrides: bool | Path | str | None) -> Path | None:
    """The live overrides file for an index: True is the one next to it (None if there is none), False or None is
    no overrides, and a path is that file (which must exist)."""
    if overrides is None or overrides is False:
        return None
    if overrides is True:
        sibling = target.with_name(OVERRIDES_FILENAME)
        return sibling if sibling.is_file() else None
    return Path(overrides).resolve()


def load_spec_index(path: Path | None = None, *, overrides: bool | Path | str | None = True) -> SpecIndex:
    """Load the spec index (default: spec/spec_index.json next to this file) with its live overrides, cached.

    overrides=True (the default) applies the file live_overrides.json that sits next to the index, if there is
    one; False or None applies none (the published spec as it is); a path applies that file. A test or script
    that wants the published operations asks for overrides=False. See apply_live_overrides for what an
    override does and when loading fails.

    The cache is keyed by the index path and the overrides file, and an entry is dropped when either file
    changes on disk (mtime or size), so a regenerated index or an edited override is picked up without a
    restart of the test process.
    """
    target = (Path(path) if path is not None else DEFAULT_INDEX_PATH).resolve()
    index_stamp = _stamp(target)
    overlay = _overrides_file(target, overrides)
    overlay_stamp = _stamp(overlay) if overlay is not None else None
    key = (str(target), index_stamp, None if overlay is None else str(overlay), overlay_stamp)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"spec index {target} is not valid JSON: {exc}") from exc
    live: Any = None
    if overlay is not None:
        try:
            live = json.loads(overlay.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"live overrides {overlay} is not valid JSON: {exc}") from exc
    index = SpecIndex(data, path=target, overrides=live, overrides_path=overlay)
    for stale in [k for k in _CACHE if k[0] == key[0] and k[2] == key[2]]:
        del _CACHE[stale]
    _CACHE[key] = index
    return index


# --------------------------------------------------------------------------
# Live overrides
# --------------------------------------------------------------------------

# `retired` is the history of dropped overrides: it is allowed in the file and never read.
_OVERRIDE_KEYS = ("description", "replace_ops", "evidence", "retired")
# What a person writes under `retired` for one dropped override, by hand: the five keys every retired entry of the shipped
# file has (tests/test_live_overrides.py checks them). Every message below that sends a person to the `retired` section
# lists all five, so that following the message leaves a file that those tests accept.
RETIRED_ENTRY_FIELDS = (
    "retired_on (YYYY-MM-DD), reason (text), published_as (the operation key the published index has for it now), "
    "replaced (the published key the override stood in for) and evidence (the old evidence, unchanged)"
)
_OP_KEY = re.compile(r"(GET|POST|PUT|PATCH|DELETE) (/\S+)")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_evidence(source: str, live_key: str, evidence: Any) -> None:
    """Every override says when and how it was verified: a date and at least one dated probe with its status."""
    where = f"{source}: evidence for {live_key!r}"
    if not isinstance(evidence, Mapping):
        raise ValueError(f"{where} must be an object with verified_on and probes")
    if not (isinstance(evidence.get("verified_on"), str) and _DATE.fullmatch(evidence["verified_on"])):
        raise ValueError(f"{where} needs verified_on, a date written YYYY-MM-DD")
    probes = evidence.get("probes")
    if not isinstance(probes, list) or not probes:
        raise ValueError(f"{where} needs probes, a non-empty list of what was observed")
    for number, probe in enumerate(probes, start=1):
        status = probe.get("status") if isinstance(probe, Mapping) else None
        if not (
            isinstance(probe, Mapping)
            and isinstance(probe.get("date"), str)
            and _DATE.fullmatch(probe["date"])
            and _is_text(probe.get("request"))
            and isinstance(status, int)
            and not isinstance(status, bool)
        ):
            raise ValueError(
                f"{where}: probe {number} needs date (YYYY-MM-DD), request (text) and status (the HTTP status, "
                "a number)"
            )


def parse_live_overrides(overrides: Any, *, source: str = OVERRIDES_FILENAME) -> dict[str, str]:
    """Check a live overrides document and return its replace_ops table {published key: live key}.

    The document is a JSON object with replace_ops (published operation key to live operation key, both written
    "METHOD /v1/path"; it may be empty), evidence (one entry per live key: verified_on and probes, see
    _check_evidence; empty when replace_ops is) and an optional description (text or a list of text). An optional
    `retired` section is the history of dropped overrides: it is accepted and ignored, whatever it holds. Any
    other key is an error, so a typo cannot disable an override quietly. A live key is listed once, is never
    also a published key, and has evidence; evidence for a key that replace_ops does not list is an error too,
    so an override that moves to `retired` takes its evidence with it. Raises ValueError naming `source`.
    """
    if not isinstance(overrides, Mapping):
        raise ValueError(f"{source}: must be a JSON object with replace_ops and evidence")
    unknown = sorted(str(name) for name in overrides if name not in _OVERRIDE_KEYS)
    if unknown:
        raise ValueError(f"{source}: unknown key(s) {', '.join(unknown)}; allowed: {', '.join(_OVERRIDE_KEYS)}")
    description = overrides.get("description")
    if description is not None and not (
        _is_text(description) or (isinstance(description, list) and all(_is_text(line) for line in description))
    ):
        raise ValueError(f"{source}: description must be text or a list of text")
    table = overrides.get("replace_ops")
    evidence = overrides.get("evidence")
    if not isinstance(table, Mapping):
        raise ValueError(f"{source}: replace_ops must be an object of published operation key to live operation key")
    if not isinstance(evidence, Mapping):
        raise ValueError(f"{source}: evidence must be an object with one entry per live operation key")
    result: dict[str, str] = {}
    for old_key, new_key in table.items():
        for label, value in (("published", old_key), ("live", new_key)):
            if not (isinstance(value, str) and _OP_KEY.fullmatch(value)):
                raise ValueError(
                    f"{source}: replace_ops has a {label} operation key that is not 'METHOD /v1/path' "
                    "(for example 'PATCH /v1/things/{thingId}')"
                )
        if old_key == new_key:
            raise ValueError(f"{source}: replace_ops replaces {old_key!r} with itself")
        result[old_key] = new_key
    live_keys = list(result.values())
    repeated = sorted({key for key in live_keys if live_keys.count(key) > 1})
    if repeated:
        raise ValueError(f"{source}: more than one published operation is replaced by {', '.join(repeated)}")
    chained = sorted(set(result) & set(live_keys))
    if chained:
        raise ValueError(f"{source}: {', '.join(chained)} is both replaced and a replacement")
    for new_key in live_keys:
        if new_key not in evidence:
            raise ValueError(f"{source}: no evidence for {new_key!r}: say when and how the live operation was verified")
        _check_evidence(source, new_key, evidence[new_key])
    stray = sorted(str(key) for key in evidence if key not in live_keys)
    if stray:
        raise ValueError(f"{source}: evidence for {', '.join(stray)}, which replace_ops does not list")
    return result


def _typed_placeholders(source: str, live_key: str, path: str, raw_ops: Mapping[str, Any]) -> dict[str, Any]:
    """The path parameters of a live operation, typed like the same placeholders on the same path elsewhere.

    The published spec has no entry for the live operation, but it types the placeholders of the same path on
    its other operations (a published GET /v1/things/{thingId} types thingId as an int64). Those types are
    copied; a placeholder that no published operation types, or that two of them type differently, is an error
    (nothing is guessed).
    """
    typed: dict[str, Any] = {}
    for name in _PLACEHOLDER.findall(path):
        kinds = {
            json.dumps(other["path_params"][name], sort_keys=True)
            for other in raw_ops.values()
            if isinstance(other, Mapping)
            and other.get("path") == path
            and isinstance(other.get("path_params"), Mapping)
            and name in other["path_params"]
        }
        if not kinds:
            raise ValueError(
                f"{source}: cannot type the path parameter {{{name}}} of {live_key!r}: no operation of the published "
                f"spec has the path {path!r} to copy its type from"
            )
        if len(kinds) > 1:
            raise ValueError(
                f"{source}: the published operations on {path!r} type the path parameter {{{name}}} of {live_key!r} "
                "in different ways; decide which is right before overriding"
            )
        typed[name] = json.loads(next(iter(kinds)))
    return typed


def apply_live_overrides(
    data: Mapping[str, Any], overrides: Any, *, source: str = OVERRIDES_FILENAME
) -> tuple[dict[str, Any], dict[str, str]]:
    """Swap the operations an overrides document replaces. Returns (new index data, {live key: published key}).

    Each live operation is a copy of the published one it replaces, with the live method and path and the path
    parameters typed like the same placeholders on the same path elsewhere in the published spec (_typed_placeholders):
    its body, query parameters and response are the published ones, and the published operation id and status codes
    are dropped because they describe another operation. The live operation takes the published one's place in the
    operation order and the published key is gone. `data` is not modified.

    Fails loudly (ValueError naming `source`), because only a person can re-verify the live API:
      * the index already has the live key: the published spec caught up, so DROP THE OVERRIDE: move the entry
        (as an entry with `retired_on`, `reason`, `published_as`, `replaced` and its old `evidence`, see
        RETIRED_ENTRY_FIELDS) to the `retired` section of the overrides file, which the loader ignores; the entry is
        not deleted and the file stays, because an empty replace_ops is valid;
      * the index no longer has the published key: the published spec changed under the override;
      * the overrides document is malformed (parse_live_overrides) or a placeholder cannot be typed.
    """
    table = parse_live_overrides(overrides, source=source)
    raw_ops = data.get("ops")
    if not isinstance(raw_ops, Mapping) or not raw_ops:
        raise ValueError("not a Gorelo spec index: the 'ops' object is missing or empty")
    for old_key, new_key in table.items():
        if new_key in raw_ops:
            raise ValueError(
                f"{source}: the published spec index now has {new_key!r}, so the published spec has caught up with "
                f"the override of {old_key!r}. DROP THE OVERRIDE: re-verify the live API, then MOVE the entry to the "
                f"retired section of {source}: take {old_key!r} out of replace_ops and {new_key!r} out of evidence, "
                f"and record it under retired, keyed by {new_key!r}, with these fields: {RETIRED_ENTRY_FIELDS}. An "
                "empty replace_ops is valid, so the file stays. Then check that the tools and tests use the published "
                "operations"
            )
        if old_key not in raw_ops:
            raise ValueError(
                f"{source}: replaces {old_key!r}, which is not in the spec index, so the published spec changed under "
                "the override. Verify the live API again, then update the entry or move it to the retired section, "
                f"keyed by {new_key!r}, with these fields: {RETIRED_ENTRY_FIELDS}"
            )
    ops: dict[str, Any] = {}
    replaced: dict[str, str] = {}
    for key, entry in raw_ops.items():
        new_key = table.get(key)
        if new_key is None:
            ops[key] = entry
            continue
        method, path = new_key.split(" ", 1)
        live = copy.deepcopy(dict(entry))
        live.update(method=method, path=path, path_params=_typed_placeholders(source, new_key, path, raw_ops))
        live.pop("operation_id", None)
        live.pop("status_codes", None)
        ops[new_key] = live
        replaced[new_key] = key
    return {**data, "ops": ops}, replaced


# --------------------------------------------------------------------------
# Query names
# --------------------------------------------------------------------------


def normalize_query(op: OpSpec, params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop None values and return every name with the spec's casing.

    Matching is case-insensitive (Gorelo treats query names that way). EVERY given name is checked
    first, even one whose value is None: an unknown name raises SpecViolation listing the allowed
    names, so a misspelled filter that happens to be unset is still a bug and never passes silently.
    Values are not converted and lists are not allowed here: GoreloClient joins id lists into comma
    separated strings before calling this.
    """
    allowed = op.query_params
    by_lower = {name.lower(): name for name in allowed}
    result: dict[str, Any] = {}
    for given, value in (params or {}).items():
        spec_name = by_lower.get(str(given).lower())
        if spec_name is None:
            raise SpecViolation(
                op.key,
                str(given),
                _unknown_name_message(op.key, "query parameter", str(given), list(allowed), None),
            )
        if value is None:
            continue
        if isinstance(value, (list, tuple, set, frozenset, dict)):
            raise SpecViolation(
                op.key,
                spec_name,
                f"{op.key}: query parameter '{spec_name}' takes a single value; "
                "join a list into one comma separated string before sending",
            )
        if spec_name in result:
            raise SpecViolation(
                op.key,
                spec_name,
                f"{op.key}: query parameter '{spec_name}' was given twice "
                "(names are case-insensitive)",
            )
        result[spec_name] = value
    for name, entry in allowed.items():
        if entry.get("required") and name not in result:
            raise SpecViolation(op.key, name, f"{op.key}: required query parameter '{name}' is missing")
    return result


# --------------------------------------------------------------------------
# Body fields
# --------------------------------------------------------------------------


def validate_body(op: OpSpec, body: Any) -> frozenset[str]:
    """Check a request body against the spec. Raises SpecViolation.

    JSON bodies are checked recursively: every key must exist (exact case) and every value must have
    the shape its field declares (an array field takes a list, an object or ref field takes an object,
    a scalar field takes a single value); a dict value is checked against the field's "ref" schema (or
    its inline fields) and a list value is checked element by element. Multipart bodies (pass the form
    field names and file field names merged in one dict) are checked against the multipart fields.
    None means "no body" and always passes; a body sent to an operation without one is a violation.

    Returns the dotted names of the schema fields that are present ("Location", "Location.Name",
    "Parts.Quantity": list indices removed). They are the only body names that may be logged: a key
    that is not a schema field (the content of a free-form object, for example) never appears in it.
    """
    if body is None:
        return frozenset()
    if op.body is None:
        raise SpecViolation(op.key, "", f"{op.key}: this operation takes no request body")
    if not isinstance(body, Mapping):
        raise SpecViolation(
            op.key, "", f"{op.key}: the request body must be a JSON object (a dict of field names to values)"
        )
    fields = op.body.get("fields") or {}
    if op.is_multipart:
        for key in body:
            if key not in fields:
                raise SpecViolation(
                    op.key,
                    str(key),
                    _unknown_name_message(op.key, "multipart form field", str(key), list(fields), None),
                )
        return frozenset(str(key) for key in body)
    names: set[str] = set()
    _check_object(op, body, fields, "", names)
    return frozenset(names)


def _check_object(
    op: OpSpec, value: Mapping[str, Any], fields: Mapping[str, Any], prefix: str, names: set[str]
) -> None:
    for key, item in value.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if key not in fields:
            raise SpecViolation(
                op.key,
                dotted,
                _unknown_name_message(op.key, "body field", dotted, list(fields), prefix or None),
            )
        names.add(_LIST_INDEX.sub("", dotted))
        _check_value(op, item, fields[key], dotted, names)


def _violation(op: OpSpec, dotted: str, problem: str) -> SpecViolation:
    return SpecViolation(op.key, dotted, f"{op.key}: body field '{dotted}' {problem}")


def _check_value(op: OpSpec, item: Any, entry: Mapping[str, Any], dotted: str, names: set[str]) -> None:
    if item is None:
        return
    shape = _shape_of(op, entry)
    if isinstance(item, Mapping):
        if shape == "array":
            raise _violation(op, dotted, "takes a list (a JSON array), not an object")
        if shape == "scalar":
            raise _violation(op, dotted, "takes a single value, not an object")
        nested = _object_fields(op, entry)
        if nested is not None:
            _check_object(op, item, nested, dotted, names)
        return
    if isinstance(item, (list, tuple)):
        if shape in ("object", "scalar"):
            raise _violation(op, dotted, "does not take a list")
        _check_elements(op, item, entry, dotted, names)
        return
    if shape == "array":
        raise _violation(op, dotted, f"takes a list (a JSON array), got {_kind_of(item)}")
    if shape == "object":
        raise _violation(op, dotted, f"takes an object (a JSON object with named fields), got {_kind_of(item)}")


def _check_elements(
    op: OpSpec, elements: list[Any] | tuple[Any, ...], entry: Mapping[str, Any], dotted: str, names: set[str]
) -> None:
    items_entry = entry.get("items")
    if not isinstance(items_entry, Mapping):
        return
    wants_objects = _shape_of(op, items_entry) == "object"
    for position, element in enumerate(elements):
        if element is None:
            continue
        where = f"{dotted}[{position}]"
        if wants_objects and not isinstance(element, Mapping):
            raise _violation(op, where, "must be an object")
        _check_value(op, element, items_entry, where, names)


def _object_fields(op: OpSpec, entry: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The field table an object value is checked against, or None if the entry is not an object."""
    if not isinstance(entry, Mapping):
        return None
    inline = entry.get("fields")
    if isinstance(inline, Mapping) and inline:
        return inline
    ref = entry.get("ref")
    if ref:
        schema = op.schemas.get(ref)
        if isinstance(schema, Mapping) and "enum" not in schema:
            fields = schema.get("fields")
            if isinstance(fields, Mapping) and fields:
                return fields
    return None


def _shape_of(op: OpSpec, entry: Mapping[str, Any] | None) -> str | None:
    """What kind of JSON value a field takes: "array", "object", "scalar", or None when the index
    does not say (a oneOf/anyOf/allOf composition, an untyped field, a ref that is not in the index).
    A None shape is never checked: this module only rejects what the spec positively contradicts."""
    if not isinstance(entry, Mapping) or any(k in entry for k in ("one_of", "any_of", "all_of")):
        return None
    kind = entry.get("type")
    if kind == "array":
        return "array"
    if kind == "object":
        return "object"
    ref = entry.get("ref")
    schema = op.schemas.get(ref) if ref else None
    if isinstance(schema, Mapping):
        if "enum" in schema:
            return "scalar"
        schema_type = schema.get("type")
        if schema_type in ("array", "object"):
            return schema_type
        if schema_type in _PRIMITIVE_TYPES:
            return "scalar"
    if kind in _PRIMITIVE_TYPES:
        return "scalar"
    inline = entry.get("fields")
    if isinstance(inline, Mapping) and inline:
        return "object"
    return None


def _kind_of(value: Any) -> str:
    """A value's JSON kind in words, for messages. Never the value itself."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, Mapping):
        return "an object"
    if isinstance(value, (list, tuple)):
        return "a list"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    return f"a {type(value).__name__}"


# --------------------------------------------------------------------------
# Path ids
# --------------------------------------------------------------------------


def validate_path_param(op: OpSpec, name: str, value: Any) -> str:
    """Check one path id against what the spec says the placeholder is, and return the text for the URL.

    * format uuid (tickets, projects, invoices, items...): a uuid.UUID, or text that is exactly a UUID
      (8-4-4-4-12 hex digits, or 32 hex digits, any case). It is returned in canonical lowercase
      hyphenated form, so what goes on the wire is never the caller's spelling.
    * type integer (clients, contacts, time entries...): a non-negative int that fits the spec's integer
      width, or a string of ASCII digits only (returned without leading zeros). Never a bool or a float.
    * anything else (an untyped string such as a form id): one plain token. It must not contain '/',
      a backslash, '%', '?', '#', whitespace or a control character, must not be made only of dots, and
      must match the spec's pattern when the spec gives one.

    Raises SpecViolation naming the placeholder. The message never repeats the value. This runs before
    any HTTP call; the caller still percent-encodes the result and compares the wire path with the
    checked one.
    """
    entry = op.path_params.get(name)
    entry = entry if isinstance(entry, Mapping) else {}
    if value is None or isinstance(value, bool):
        raise SpecViolation(
            op.key, name, f"{op.key}: path parameter '{name}' is required and must not be {_kind_of(value)}"
        )
    if entry.get("format") == "uuid":
        return _uuid_text(op, name, value)
    if entry.get("type") == "integer":
        return _integer_text(op, name, value, entry.get("format"))
    return _token_text(op, name, value, entry.get("pattern"))


def _uuid_text(op: OpSpec, name: str, value: Any) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str) and (_UUID_HYPHENATED.fullmatch(value) or _UUID_BARE.fullmatch(value)):
        return str(uuid.UUID(value))
    raise SpecViolation(
        op.key,
        name,
        f"{op.key}: path parameter '{name}' must be a UUID such as 3f2b8c1e-0d4a-4b7e-9a51-2c6d8e9f0a1b "
        f"({_kind_of(value)} was given)",
    )


def _integer_text(op: OpSpec, name: str, value: Any, fmt: Any) -> str:
    limit = _INT_LIMITS.get(fmt, _INT_LIMIT_DEFAULT)
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _DIGITS.fullmatch(value):
        number = int(value) if len(value) <= 24 else limit + 1
    else:
        raise SpecViolation(
            op.key,
            name,
            f"{op.key}: path parameter '{name}' must be a whole number such as 42, "
            f"written in digits only ({_kind_of(value)} was given)",
        )
    if number < 0 or number > limit:
        raise SpecViolation(
            op.key, name, f"{op.key}: path parameter '{name}' must be a whole number from 0 to {limit}"
        )
    return str(int(number))


def _token_text(op: OpSpec, name: str, value: Any, pattern: Any) -> str:
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        raise SpecViolation(
            op.key, name, f"{op.key}: path parameter '{name}' must be text ({_kind_of(value)} was given)"
        )
    if not text.strip():
        raise SpecViolation(op.key, name, f"{op.key}: path parameter '{name}' is required and must not be empty")
    if (
        text.strip(".") == ""
        or any(ch in "/\\%?#" for ch in text)
        or any(ch.isspace() or not ch.isprintable() for ch in text)
    ):
        raise SpecViolation(
            op.key,
            name,
            f"{op.key}: path parameter '{name}' must be one plain token: no '/', backslash, '%', '?', '#', "
            "whitespace or control characters, and not made only of dots",
        )
    if isinstance(pattern, str) and pattern:
        try:
            matches = re.fullmatch(pattern, text) is not None
        except re.error:  # a pattern Python cannot read is not a reason to refuse a safe token
            matches = True
        if not matches:
            raise SpecViolation(
                op.key, name, f"{op.key}: path parameter '{name}' must match the pattern {pattern}"
            )
    return text


def _unknown_name_message(
    op_key: str, kind: str, name: str, allowed: list[str], parent: str | None
) -> str:
    leaf = name.rsplit(".", 1)[-1]
    suggestion = _suggest(leaf, allowed)
    hint = f" (did you mean '{suggestion}'?)" if suggestion else ""
    where = f" in '{parent}'" if parent else ""
    if not allowed:
        return f"{op_key}: unknown {kind} '{name}'{hint}; this operation has no {kind}s"
    shown = sorted(allowed)
    extra = ""
    if len(shown) > _MAX_NAMES_IN_MESSAGE:
        extra = f", ... ({len(shown) - _MAX_NAMES_IN_MESSAGE} more)"
        shown = shown[:_MAX_NAMES_IN_MESSAGE]
    return f"{op_key}: unknown {kind} '{name}'{hint}. Allowed{where}: {', '.join(shown)}{extra}"


def _suggest(name: str, allowed: list[str]) -> str | None:
    lowered = {candidate.lower(): candidate for candidate in allowed}
    if name.lower() in lowered:
        return lowered[name.lower()]
    close = difflib.get_close_matches(name, allowed, n=1, cutoff=0.6)
    return close[0] if close else None
