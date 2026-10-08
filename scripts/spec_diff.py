#!/usr/bin/env python3
"""Compare two Gorelo OpenAPI snapshots and report the differences as markdown.

    spec_diff.py OLD NEW [--out FILE]

OLD and NEW may each be a raw swagger JSON file (for example
backups/swagger-20260726-184500.json) or a spec index (spec/spec_index.json);
a file is treated as an index when it has a top-level "ops" key. Raw specs are
normalised through spec_snapshot.build_index, so only API shape is compared:
descriptions, summaries and examples are ignored.

Exit status: 0 if the two are identical, or differ only in descriptions,
summaries, examples or formatting (proved by the contract hash, see below); 1 if
a recorded attribute differs; 2 on a usage or input error; 3 if every recorded
attribute is identical but the raw contract is not, or an input is an older
index that cannot prove it is. 3 means something the index does not record
changed (a response header, an extra media type, servers, ...): compare the raw
files. With --out the markdown goes to that file and a one-line status is
printed; without it the markdown is printed to stdout.

Report contents: summary counts; added and removed operations; per changed
operation the query params, path params, body, response, paging, deprecation,
security and status code changes; per changed schema the type, format, enum,
items, constraints, required list and fields added, removed and changed, with
"possible rename" hints when removed and added fields share a type.

Everything the spec index records is compared, so a change to any recorded
attribute shows up as a difference and never reads as "No differences":

* params and fields: type, format, ref, items, enum values (an enum-only change
  is listed as "enum changed" with the values gained and lost), nullable and
  required flags, every constraint key of the index (default, min_length,
  max_length, pattern, minimum, maximum, ..., deprecated, read_only, style,
  explode, additional_properties, one_of, any_of, all_of: see
  spec_snapshot.FACET_KEYS), and, for an object defined inline, its nested
  fields and required_fields;
* bodies: content type, schema name, fields, required list, whether the body
  itself is required, and for a body that is not an object (an array or
  primitive, inline or through a $ref) its type, format and items;
* schemas: type, format, enum, items (array schemas), required list, fields and
  the same constraint keys;
* operations: deprecated and their own security requirements;
* the spec: OpenAPI version, security schemes and the top-level security
  requirements.

Whatever the index does not record is still never missed. The index carries a
contract hash, the sha256 of the spec without its descriptions, summaries and
examples. When no recorded attribute differs, the report compares the two
hashes: equal means only documentation or formatting changed (exit 0, and the
report says so); different means something else changed (exit 3). An input that
is an index written before the hash existed cannot prove either, and exits 3
when its raw bytes differ.

How names are matched (so the July 2026 spec, which used camelCase and
different schema names, can be compared with later ones):

* Query params, body fields and schema fields are matched case-insensitively.
  A case-only change (cursor -> Cursor) is reported as such, never hidden:
  JSON keys are case-sensitive for anyone reading responses.
* Operations are matched on method plus path with a trailing slash ignored and
  path parameter names ignored (/v1/alerts/ matches /v1/alerts, {id} matches
  {agentId}); the path change itself is reported on the operation.
* Schema names are compared after dropping a "<word>-cluster_" prefix and
  "Public" name segments (public-cluster_PublicTicketListItemModel is
  TicketListItemModel).
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spec_snapshot import FACET_KEYS, build_index  # noqa: E402

_CLUSTER_PREFIX = re.compile(r"^[A-Za-z0-9]+-cluster_")
_PUBLIC_WORD = re.compile(r"Public(?=[A-Z])")
_TOKEN = re.compile(r"[A-Za-z0-9_\-]+")
_PATH_PARAM = re.compile(r"\{([^}]+)\}")
_PRIMITIVES = frozenset({"string", "integer", "number", "boolean", "object", "array", "any"})
_METHOD_ORDER = {"GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4}
_MAX_LISTED = 8
_CASE_LIST_LIMIT = 5
_AMBIGUOUS_HINT_LIMIT = 6
_MISSING = object()  # a facet the entry does not carry, as opposed to one that is null
# Facets whose value is an index entry (or a list of them), labelled like a type.
_ENTRY_FACETS = frozenset({"additional_properties", "one_of", "any_of", "all_of"})
_COMPOSITION_FACETS = ("one_of", "any_of", "all_of")


class DiffError(Exception):
    """An input problem the user can act on."""


# --------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------


def norm_schema_name(name: str | None) -> str | None:
    """Drop the July-style 'public-cluster_' prefix and 'Public' name segments."""
    if name is None:
        return None
    normalised = _PUBLIC_WORD.sub("", _CLUSTER_PREFIX.sub("", name))
    return normalised or name


def norm_data_type(text: str | None) -> str | None:
    """Normalise schema names inside a response data string like '[SchemaName]'."""
    if text is None:
        return None
    return _TOKEN.sub(
        lambda m: m.group(0) if m.group(0) in _PRIMITIVES else norm_schema_name(m.group(0)),
        text,
    )


def norm_path(path: str) -> str:
    return path.rstrip("/") or "/"


def _match_path(path: str) -> str:
    """Path used to pair operations: no trailing slash, parameter names erased."""
    return _PATH_PARAM.sub("{}", norm_path(path))


def _split_key(key: str) -> tuple[str, str]:
    method, _, path = key.partition(" ")
    return method, path


def _op_sort_key(key: str) -> tuple[str, int, str]:
    method, path = _split_key(key)
    return (path, _METHOD_ORDER.get(method, 9), method)


def _quote(name: str | None) -> str:
    return "none" if name is None else f"`{name}`"


# --------------------------------------------------------------------------
# Entry labels and signatures (params, fields)
# --------------------------------------------------------------------------


def _values_text(values: list[Any]) -> str:
    """Enum values as a compact JSON list: [1, 2, 3] or ["a", "b"]."""
    return "[" + ", ".join(json.dumps(value, ensure_ascii=False) for value in values) + "]"


def enum_change_text(old_enum: list[Any] | None, new_enum: list[Any] | None) -> str:
    """'old -> new' for an enum change, with the values gained and lost spelled out.

    None means no enum: `none -> [..]` is an enum that was added, `[..] -> none`
    one that was removed.
    """
    old_text = "none" if old_enum is None else _values_text(old_enum)
    new_text = "none" if new_enum is None else _values_text(new_enum)
    text = f"{old_text} -> {new_text}"
    if old_enum is None or new_enum is None:
        return text
    gained = [v for v in new_enum if v not in old_enum]
    lost = [v for v in old_enum if v not in new_enum]
    parts = []
    if gained:
        parts.append("values added: " + ", ".join(json.dumps(v, ensure_ascii=False) for v in gained))
    if lost:
        parts.append("values removed: " + ", ".join(json.dumps(v, ensure_ascii=False) for v in lost))
    return f"{text} ({'; '.join(parts) if parts else 'same values, order only'})"


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def facet_changes(
    old_entry: dict[str, Any], new_entry: dict[str, Any], prefix: str = ""
) -> list[tuple[str, Any, Any]]:
    """(facet, old, new) for every constraint-like index key that differs.

    The keys are spec_snapshot.FACET_KEYS: default, min_length, pattern,
    deprecated, additional_properties, one_of and the rest. A key the entry does
    not carry reads as _MISSING. Values compare as canonical JSON, so 1 and true
    are different values. The constraints of an array's element are compared too
    and named "items.<key>" (the entry's "items" entry, one level down each time).
    """
    changes = []
    for key in FACET_KEYS:
        old_value, new_value = old_entry.get(key, _MISSING), new_entry.get(key, _MISSING)
        if (old_value is _MISSING) != (new_value is _MISSING) or (
            old_value is not _MISSING and _canon(old_value) != _canon(new_value)
        ):
            changes.append((prefix + key, old_value, new_value))
    old_items, new_items = old_entry.get("items"), new_entry.get("items")
    if isinstance(old_items, dict) and isinstance(new_items, dict):
        changes += facet_changes(old_items, new_items, f"{prefix}items.")
    return changes


def facet_texts(facet: str, old_value: Any, new_value: Any) -> tuple[str, str]:
    """The old and new value of a facet as report text: none, JSON, or entry labels.

    Entries and lists of entries (a map type's value, the parts of a oneOf) read
    as type labels, unless the labels are equal although the entries differ (a
    constraint inside them changed): then both are shown as JSON.
    """

    def text(value: Any, as_json: bool) -> str:
        if value is _MISSING:
            return "none"
        if not as_json and facet.rsplit(".", 1)[-1] in _ENTRY_FACETS:
            if isinstance(value, dict):
                return entry_label(value)
            if isinstance(value, list):
                return "[" + ", ".join(entry_label(item) for item in value) + "]"
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    old_text, new_text = text(old_value, False), text(new_value, False)
    if old_text == new_text:
        old_text, new_text = text(old_value, True), text(new_value, True)
    return old_text, new_text


def entry_label(entry: dict[str, Any] | None) -> str:
    """Short human label: integer/int64, string/uuid, string, enum [..], array of X, SchemaName.

    A $ref entry is labelled by its schema name alone: the enum values of a
    referenced enum schema belong to that schema (the Schemas section lists them).
    The parts of a oneOf, anyOf or allOf are listed after the type.
    """
    if not entry:
        return "none"
    if entry.get("ref"):
        base = norm_schema_name(entry["ref"]) or entry["ref"]
    else:
        if entry.get("type") == "array":
            items = entry.get("items")
            base = f"array of {entry_label(items)}" if items else "array"
        else:
            base = entry.get("type") or "untyped"
            if entry.get("format"):
                base += f"/{entry['format']}"
        if entry.get("enum") is not None:
            base += f", enum {_values_text(entry['enum'])}"
        for key in _COMPOSITION_FACETS:
            if entry.get(key):
                base += f", {key} [" + ", ".join(entry_label(part) for part in entry[key]) + "]"
    if entry.get("nullable"):
        base += ", nullable"
    if entry.get("required"):
        base += ", required"
    return base


def _loose_sig(entry: dict[str, Any] | None) -> tuple[Any, ...]:
    """Type identity ignoring nullable, required and enum values.

    The enum is deliberately not part of the identity: an entry whose enum
    changed is still the same field or param, so it keeps its rename hints and
    is reported on its own ("enum changed"), see diff_maps.
    """
    entry = entry or {}
    items = entry.get("items")
    return (
        entry.get("type"),
        entry.get("format"),
        norm_schema_name(entry["ref"]) if entry.get("ref") else None,
        _loose_sig(items) if isinstance(items, dict) else None,
    )


def _sig(entry: dict[str, Any] | None) -> tuple[Any, ...]:
    entry = entry or {}
    return _loose_sig(entry) + (bool(entry.get("nullable")), bool(entry.get("required")))


def _loose_label(entry: dict[str, Any]) -> str:
    return entry_label({k: v for k, v in entry.items() if k not in ("nullable", "required")})


def _refs_in(entry: dict[str, Any] | None) -> Iterable[str]:
    entry = entry or {}
    if entry.get("ref"):
        yield entry["ref"]
    if isinstance(entry.get("items"), dict):
        yield from _refs_in(entry["items"])


def _deep_refs(entry: dict[str, Any] | None) -> Iterable[str]:
    """Every schema name an entry mentions: ref, items, parts of a oneOf, anyOf or
    allOf, a map type's value, and the fields of an inline object."""
    entry = entry or {}
    if entry.get("ref"):
        yield entry["ref"]
    if isinstance(entry.get("items"), dict):
        yield from _deep_refs(entry["items"])
    for key in _COMPOSITION_FACETS:
        for part in entry.get(key) or []:
            yield from _deep_refs(part)
    if isinstance(entry.get("additional_properties"), dict):
        yield from _deep_refs(entry["additional_properties"])
    for nested in (entry.get("fields") or {}).values():
        yield from _deep_refs(nested)


# --------------------------------------------------------------------------
# Keyed map diffs (params, fields)
# --------------------------------------------------------------------------


@dataclass
class MapDiff:
    """Differences between two {name: entry} maps.

    `changed` holds real type changes (type, format, ref, items); entries whose
    type is the same but whose enum values differ are in `enum_changed`; entries
    that differ only in nullable or required flags are listed in the four flag
    lists so a wave of "string, nullable -> string" does not bury real changes.
    `facets` holds (name, facet, old, new) for every constraint-like key that
    differs (default, min_length, deprecated, one_of, ...), whatever else
    changed on the entry; `nested` holds (name, diff of the inline object's
    fields, old and new required_fields) for an object defined inline.
    """

    added: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    removed: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    case_renamed: list[tuple[str, str]] = field(default_factory=list)
    changed: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    enum_changed: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    nullable_on: list[str] = field(default_factory=list)
    nullable_off: list[str] = field(default_factory=list)
    required_on: list[str] = field(default_factory=list)
    required_off: list[str] = field(default_factory=list)
    facets: list[tuple[str, str, Any, Any]] = field(default_factory=list)
    nested: list[tuple[str, "MapDiff", list[str], list[str]]] = field(default_factory=list)

    @property
    def structural(self) -> bool:
        """True if anything other than case renames and flag flips changed."""
        return bool(
            self.added
            or self.removed
            or self.changed
            or self.enum_changed
            or self.facets
            or self.nested
        )

    def __bool__(self) -> bool:
        return bool(
            self.structural
            or self.case_renamed
            or self.nullable_on
            or self.nullable_off
            or self.required_on
            or self.required_off
        )


def _case_keyer(*maps: Iterable[str]) -> Callable[[str], str]:
    """Lowercase match key, except names that collide on lowercase keep exact spelling."""
    collide: set[str] = set()
    for names in maps:
        counts = Counter(n.lower() for n in names)
        collide |= {low for low, count in counts.items() if count > 1}
    return lambda name: name if name.lower() in collide else name.lower()


def diff_maps(old: dict[str, Any], new: dict[str, Any]) -> MapDiff:
    """Diff two {name: entry} maps, matching names case-insensitively."""
    key = _case_keyer(old, new)
    old_by = {key(n): n for n in old}
    new_by = {key(n): n for n in new}
    result = MapDiff()
    for k in sorted(set(old_by) | set(new_by)):
        old_name, new_name = old_by.get(k), new_by.get(k)
        if old_name is None:
            result.added.append((new_name, new[new_name]))
        elif new_name is None:
            result.removed.append((old_name, old[old_name]))
        else:
            if old_name != new_name:
                result.case_renamed.append((old_name, new_name))
            old_entry, new_entry = old[old_name], new[new_name]
            # constraints and inline fields are reported whatever else changed on the entry
            for facet, old_value, new_value in facet_changes(old_entry, new_entry):
                result.facets.append((new_name, facet, old_value, new_value))
            _collect_nested(result, new_name, old_entry, new_entry)
            if _loose_sig(old_entry) != _loose_sig(new_entry):
                # entry_label shows the enum, so this line also carries an enum change
                result.changed.append((old_name, new_name, old_entry, new_entry))
                continue
            if old_entry.get("enum") != new_entry.get("enum"):
                result.enum_changed.append((old_name, new_name, old_entry, new_entry))
            if bool(old_entry.get("nullable")) != bool(new_entry.get("nullable")):
                (result.nullable_on if new_entry.get("nullable") else result.nullable_off).append(new_name)
            if bool(old_entry.get("required")) != bool(new_entry.get("required")):
                (result.required_on if new_entry.get("required") else result.required_off).append(new_name)
    return result


def _collect_nested(
    result: MapDiff, name: str, old_entry: dict[str, Any], new_entry: dict[str, Any]
) -> None:
    """Add the changes inside an inline object entry (its "fields" and "required_fields").

    An array's element is followed too, and named "<name>[]", so an array of
    inline objects reports the changes of the element's fields.
    """
    old_fields, new_fields = old_entry.get("fields"), new_entry.get("fields")
    if old_fields is not None or new_fields is not None:
        inner = diff_maps(old_fields or {}, new_fields or {})
        old_required = old_entry.get("required_fields", [])
        new_required = new_entry.get("required_fields", [])
        if inner or required_diff_lines("", old_required, new_required):
            result.nested.append((name, inner, old_required, new_required))
    old_items, new_items = old_entry.get("items"), new_entry.get("items")
    if isinstance(old_items, dict) and isinstance(new_items, dict):
        _collect_nested(result, f"{name}[]", old_items, new_items)


def rename_hints(removed: list[tuple[str, dict[str, Any]]], added: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """Hints for removed and added entries that share a type.

    Exactly one removed and one added of a type reads as a rename; a few of each
    reads as an ambiguous rename or split (WarrantyExpiryDate into two dates).
    """
    gone: dict[tuple[Any, ...], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    came: dict[tuple[Any, ...], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for name, entry in removed:
        gone[_loose_sig(entry)].append((name, entry))
    for name, entry in added:
        came[_loose_sig(entry)].append((name, entry))
    hints = []
    for sig in sorted(set(gone) & set(came), key=str):
        old_side, new_side = gone[sig], came[sig]
        label = _loose_label(old_side[0][1])
        if len(old_side) == 1 and len(new_side) == 1:
            hints.append(
                f"- possible rename: `{old_side[0][0]}` -> `{new_side[0][0]}` ({label})"
            )
        elif len(old_side) + len(new_side) <= _AMBIGUOUS_HINT_LIMIT:
            old_names = ", ".join(f"`{n}`" for n, _ in old_side)
            new_names = ", ".join(f"`{n}`" for n, _ in new_side)
            hints.append(
                f"- possible rename or split (ambiguous): {old_names} -> {new_names} ({label})"
            )
    return hints


def map_diff_lines(noun: str, diff: MapDiff, *, hints: bool = False) -> list[str]:
    lines: list[str] = []
    if diff.added:
        listed = ", ".join(f"`{n}` ({entry_label(e)})" for n, e in diff.added)
        lines.append(f"- {noun} added ({len(diff.added)}): {listed}")
    if diff.removed:
        listed = ", ".join(f"`{n}` ({entry_label(e)})" for n, e in diff.removed)
        lines.append(f"- {noun} removed ({len(diff.removed)}): {listed}")
    if hints:
        lines.extend(rename_hints(diff.removed, diff.added))
    if diff.case_renamed:
        if len(diff.case_renamed) <= _CASE_LIST_LIMIT:
            detail = ": " + ", ".join(f"`{o}` -> `{n}`" for o, n in diff.case_renamed)
        else:
            first_old, first_new = diff.case_renamed[0]
            detail = f", for example `{first_old}` -> `{first_new}`"
        lines.append(f"- {noun} renamed by case only ({len(diff.case_renamed)}){detail}")
    for old_name, new_name, old_entry, new_entry in diff.changed:
        shown = f"`{new_name}`" if old_name == new_name else f"`{old_name}` / `{new_name}`"
        lines.append(
            f"- {noun} type changed: {shown}: {entry_label(old_entry)} -> {entry_label(new_entry)}"
        )
    for old_name, new_name, old_entry, new_entry in diff.enum_changed:
        shown = f"`{new_name}`" if old_name == new_name else f"`{old_name}` / `{new_name}`"
        lines.append(
            f"- {noun} enum changed: {shown}: "
            f"{enum_change_text(old_entry.get('enum'), new_entry.get('enum'))}"
        )
    for label, names in (
        ("now nullable", diff.nullable_on),
        ("no longer nullable", diff.nullable_off),
        ("now required", diff.required_on),
        ("no longer required", diff.required_off),
    ):
        if names:
            lines.append(f"- {noun} {label} ({len(names)}): " + ", ".join(f"`{n}`" for n in names))
    for name, facet, old_value, new_value in diff.facets:
        old_text, new_text = facet_texts(facet, old_value, new_value)
        lines.append(f"- {noun} {facet} changed: `{name}`: {old_text} -> {new_text}")
    for name, inner, old_required, new_required in diff.nested:
        inner_noun = f"{noun} of `{name}`"
        lines += map_diff_lines(inner_noun, inner, hints=hints)
        lines += required_diff_lines(inner_noun, old_required, new_required)
    return lines


def required_diff_lines(noun: str, old_required: list[str], new_required: list[str]) -> list[str]:
    old_by = {r.lower(): r for r in old_required}
    new_by = {r.lower(): r for r in new_required}
    added = [new_by[k] for k in sorted(new_by) if k not in old_by]
    removed = [old_by[k] for k in sorted(old_by) if k not in new_by]
    prefix = f"{noun} " if noun else ""
    lines = []
    if added:
        lines.append(f"- {prefix}required added: " + ", ".join(f"`{n}`" for n in added))
    if removed:
        lines.append(f"- {prefix}required removed: " + ", ".join(f"`{n}`" for n in removed))
    return lines


# --------------------------------------------------------------------------
# Operation diffs
# --------------------------------------------------------------------------


def _body_label(body: dict[str, Any]) -> str:
    return _quote(body.get("schema")) if body.get("schema") else "inline"


def _body_shape(body: dict[str, Any]) -> dict[str, Any]:
    """What a body is: an object, unless the index recorded a type for it.

    The index adds "type" (plus "format" and "items" when present) only to a
    body that is not an object, for example an inline array. "type" is null for
    a body whose schema declares none.
    """
    if "type" not in body:
        return {"type": "object"}
    return {key: body[key] for key in ("type", "format", "items") if key in body}


def diff_body(old: dict[str, Any] | None, new: dict[str, Any] | None) -> list[str]:
    if old is None and new is None:
        return []
    if old is None:
        if "type" in new:
            return [
                f"- body added: {_body_label(new)} ({new['content_type']}), "
                f"{entry_label(_body_shape(new))}"
            ]
        required = ", ".join(f"`{n}`" for n in new["required"]) or "none"
        return [
            f"- body added: {_body_label(new)} ({new['content_type']}), "
            f"{len(new['fields'])} fields, required: {required}"
        ]
    if new is None:
        shape = f", {entry_label(_body_shape(old))}" if "type" in old else ""
        return [f"- body removed (was {_body_label(old)}, {old['content_type']}{shape})"]
    lines = []
    if old["content_type"] != new["content_type"]:
        lines.append(f"- body content type: `{old['content_type']}` -> `{new['content_type']}`")
    if norm_schema_name(old.get("schema")) != norm_schema_name(new.get("schema")):
        lines.append(f"- body schema: {_quote(old.get('schema'))} -> {_quote(new.get('schema'))}")
    old_shape, new_shape = _body_shape(old), _body_shape(new)
    if _loose_sig(old_shape) != _loose_sig(new_shape):
        lines.append(f"- body type: {entry_label(old_shape)} -> {entry_label(new_shape)}")
    # constraints and inline fields of the element of an array body
    for facet, old_value, new_value in facet_changes(old_shape, new_shape):
        old_text, new_text = facet_texts(facet, old_value, new_value)
        lines.append(f"- body {facet} changed: {old_text} -> {new_text}")
    old_items, new_items = old_shape.get("items"), new_shape.get("items")
    if isinstance(old_items, dict) and isinstance(new_items, dict):
        element = MapDiff()
        _collect_nested(element, "items", old_items, new_items)
        lines += map_diff_lines("body fields", element)
    if bool(old.get("body_required")) != bool(new.get("body_required")):
        lines.append(
            f"- request body: {'required' if old.get('body_required') else 'optional'} -> "
            f"{'required' if new.get('body_required') else 'optional'}"
        )
    lines += map_diff_lines("body fields", diff_maps(old["fields"], new["fields"]), hints=True)
    lines += required_diff_lines("body", old["required"], new["required"])
    return lines


def diff_response(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    parts = []
    if old.get("kind") != new.get("kind"):
        parts.append(f"kind `{old.get('kind')}` -> `{new.get('kind')}`")
    if norm_schema_name(old.get("schema")) != norm_schema_name(new.get("schema")):
        parts.append(f"schema {_quote(old.get('schema'))} -> {_quote(new.get('schema'))}")
    if norm_data_type(old.get("data")) != norm_data_type(new.get("data")):
        parts.append(f"data {_quote(old.get('data'))} -> {_quote(new.get('data'))}")
    return [f"- response: {'; '.join(parts)}"] if parts else []


def diff_path_params(old_op: dict[str, Any], new_op: dict[str, Any]) -> list[str]:
    """Compare path params by position (their names may legitimately change)."""
    old_names = _PATH_PARAM.findall(old_op["path"])
    new_names = _PATH_PARAM.findall(new_op["path"])
    lines = []
    for old_name, new_name in zip(old_names, new_names):
        old_entry = old_op.get("path_params", {}).get(old_name)
        new_entry = new_op.get("path_params", {}).get(new_name)
        if old_entry is None or new_entry is None:
            continue
        if _sig(old_entry) != _sig(new_entry):
            lines.append(
                f"- path param `{{{new_name}}}` type changed: "
                f"{entry_label(old_entry)} -> {entry_label(new_entry)}"
            )
        elif old_entry.get("enum") != new_entry.get("enum"):
            lines.append(
                f"- path param `{{{new_name}}}` enum changed: "
                f"{enum_change_text(old_entry.get('enum'), new_entry.get('enum'))}"
            )
        for facet, old_value, new_value in facet_changes(old_entry, new_entry):
            old_text, new_text = facet_texts(facet, old_value, new_value)
            lines.append(f"- path param `{{{new_name}}}` {facet} changed: {old_text} -> {new_text}")
    return lines


def diff_status_codes(old: list[str], new: list[str]) -> list[str]:
    added = [c for c in new if c not in old]
    removed = [c for c in old if c not in new]
    parts = []
    if added:
        parts.append("added " + ", ".join(added))
    if removed:
        parts.append("removed " + ", ".join(removed))
    return [f"- status codes: {'; '.join(parts)}"] if parts else []


def security_text(requirements: list[dict[str, list[str]]] | None) -> str:
    """Security requirements for the report.

    Several requirement objects are alternatives ("or"); the schemes inside one
    object are all needed ("+"). An empty list means the operation needs no
    authentication; None means it declares nothing and inherits the top level.
    """
    if requirements is None:
        return "none declared"
    if not requirements:
        return "no authentication"
    alternatives = []
    for requirement in requirements:
        if not isinstance(requirement, dict):  # a hand-edited index: show it rather than crash
            alternatives.append(json.dumps(requirement, ensure_ascii=False))
            continue
        if not requirement:
            alternatives.append("anonymous")
            continue
        alternatives.append(
            " + ".join(
                f"{name}({', '.join(scopes)})" if scopes else name
                for name, scopes in sorted(requirement.items())
            )
        )
    return " or ".join(alternatives)


def _scheme_text(scheme: dict[str, Any]) -> str:
    order = ("type", "in", "name", "scheme", "bearer_format", "open_id_connect_url")
    return ", ".join(f"{key} {scheme[key]}" for key in order if key in scheme) or "no details"


def diff_security(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Differences in the spec's security schemes and top-level security requirements."""
    lines = []
    old_schemes, new_schemes = old.get("security_schemes") or {}, new.get("security_schemes") or {}
    for name in sorted(set(old_schemes) | set(new_schemes)):
        old_scheme, new_scheme = old_schemes.get(name), new_schemes.get(name)
        if old_scheme is None:
            lines.append(f"Security scheme added: `{name}` ({_scheme_text(new_scheme)})")
        elif new_scheme is None:
            lines.append(f"Security scheme removed: `{name}` (was {_scheme_text(old_scheme)})")
        elif old_scheme != new_scheme:
            lines.append(
                f"Security scheme `{name}` changed: "
                f"{_scheme_text(old_scheme)} -> {_scheme_text(new_scheme)}"
            )
    if old.get("security") != new.get("security"):
        lines.append(
            f"Top-level security requirements: {security_text(old.get('security'))} -> "
            f"{security_text(new.get('security'))}"
        )
    return lines


@dataclass
class OpChange:
    old_key: str
    new_key: str
    lines: list[str]


def diff_op(old_op: dict[str, Any], new_op: dict[str, Any], counts: Counter) -> list[str]:
    lines: list[str] = []
    if old_op["path"] != new_op["path"]:
        if norm_path(old_op["path"]) == norm_path(new_op["path"]):
            why = " (trailing slash only)"
        elif _match_path(old_op["path"]) == _match_path(new_op["path"]):
            why = " (path parameter names only)"
        else:
            why = ""
        lines.append(f"- path: `{old_op['path']}` -> `{new_op['path']}`{why}")
    lines += diff_path_params(old_op, new_op)

    query = diff_maps(old_op.get("query_params", {}), new_op.get("query_params", {}))
    counts["query_case_renames"] += len(query.case_renamed)
    lines += map_diff_lines("query params", query)

    if old_op.get("paged") != new_op.get("paged"):
        lines.append(f"- paged: {old_op.get('paged')} -> {new_op.get('paged')}")
    if old_op.get("page_size_rule") != new_op.get("page_size_rule"):
        lines.append(
            f"- page_size_rule: {old_op.get('page_size_rule')} -> {new_op.get('page_size_rule')}"
        )
    if bool(old_op.get("deprecated")) != bool(new_op.get("deprecated")):
        lines.append(
            f"- deprecated: {str(bool(old_op.get('deprecated'))).lower()} -> "
            f"{str(bool(new_op.get('deprecated'))).lower()}"
        )
    if old_op.get("security") != new_op.get("security"):
        lines.append(
            f"- security: {security_text(old_op.get('security'))} -> "
            f"{security_text(new_op.get('security'))}"
        )
    lines += diff_body(old_op.get("body"), new_op.get("body"))
    lines += diff_response(old_op["response"], new_op["response"])
    lines += diff_status_codes(old_op.get("status_codes", []), new_op.get("status_codes", []))
    return lines


# --------------------------------------------------------------------------
# Schema diffs
# --------------------------------------------------------------------------


@dataclass
class SchemaChange:
    old_name: str
    new_name: str
    lines: list[str]
    names_only: bool


def diff_schema(old: dict[str, Any], new: dict[str, Any], counts: Counter) -> tuple[list[str], bool]:
    """Return (report lines, True if only field-name case and nullability changed)."""
    lines: list[str] = []
    other_changes = False
    if old.get("type") != new.get("type"):
        lines.append(f"- type: {_quote(old.get('type'))} -> {_quote(new.get('type'))}")
        other_changes = True
    old_items, new_items = old.get("items"), new.get("items")
    if _loose_sig(old_items) != _loose_sig(new_items):
        # Only array schemas carry items, so this is an element type change.
        lines.append(f"- items: {entry_label(old_items)} -> {entry_label(new_items)}")
        other_changes = True
    old_enum, new_enum = old.get("enum"), new.get("enum")
    if old_enum != new_enum:
        other_changes = True
        if old_enum is None or new_enum is None:
            lines.append(f"- enum {'added' if old_enum is None else 'removed'}: {new_enum or old_enum}")
        elif set(old_enum) == set(new_enum):
            lines.append("- enum order changed")
        else:
            gained = [v for v in new_enum if v not in old_enum]
            lost = [v for v in old_enum if v not in new_enum]
            if gained:
                lines.append(f"- enum values added: {gained}")
            if lost:
                lines.append(f"- enum values removed: {lost}")
    if old.get("format") != new.get("format"):
        lines.append(f"- format: {_quote(old.get('format'))} -> {_quote(new.get('format'))}")
        other_changes = True
    for facet, old_value, new_value in facet_changes(old, new):
        old_text, new_text = facet_texts(facet, old_value, new_value)
        lines.append(f"- {facet}: {old_text} -> {new_text}")
        other_changes = True
    if isinstance(old_items, dict) and isinstance(new_items, dict):
        # an array schema whose element is an inline object: its fields are nested in items
        element = MapDiff()
        _collect_nested(element, "items", old_items, new_items)
        element_lines = map_diff_lines("fields", element)
        lines += element_lines
        other_changes = other_changes or bool(element_lines)
    required_lines = required_diff_lines("", old.get("required", []), new.get("required", []))
    lines += required_lines
    other_changes = other_changes or bool(required_lines)

    fields = diff_maps(old.get("fields", {}), new.get("fields", {}))
    counts["schema_field_case_renames"] += len(fields.case_renamed)
    lines += map_diff_lines("fields", fields, hints=True)
    other_changes = other_changes or fields.structural
    return lines, bool(lines) and not other_changes


def _norm_name_map(names: Iterable[str]) -> dict[str, str]:
    """normalised name -> raw name; names that collide once normalised stay exact."""
    groups: dict[str, list[str]] = defaultdict(list)
    for name in names:
        groups[norm_schema_name(name) or name].append(name)
    result: dict[str, str] = {}
    for normalised, raws in groups.items():
        if len(raws) == 1:
            result[normalised] = raws[0]
        else:
            result.update({raw: raw for raw in raws})
    return result


# --------------------------------------------------------------------------
# Whole-index diff
# --------------------------------------------------------------------------


@dataclass
class DiffResult:
    ops_added: list[str] = field(default_factory=list)
    ops_removed: list[str] = field(default_factory=list)
    ops_changed: list[OpChange] = field(default_factory=list)
    ops_unchanged: int = 0
    operation_id_changes: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    schemas_added: list[str] = field(default_factory=list)
    schemas_removed: list[str] = field(default_factory=list)
    schemas_changed: list[SchemaChange] = field(default_factory=list)
    schemas_unchanged: int = 0
    top_level: list[str] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    # Set only when no recorded attribute differs: "changed" if the contract
    # hashes differ (something the index does not record changed), "unknown" if
    # the raw bytes differ and an input has no contract hash to prove otherwise.
    unrecorded: str | None = None

    @property
    def recorded_differences(self) -> bool:
        """True if an attribute the index records differs."""
        return bool(
            self.ops_added
            or self.ops_removed
            or self.ops_changed
            or self.operation_id_changes
            or self.schemas_added
            or self.schemas_removed
            or self.schemas_changed
            or self.top_level
        )

    @property
    def has_differences(self) -> bool:
        """True unless the two specs are identical or differ only in documentation."""
        return self.recorded_differences or self.unrecorded is not None


def _pair_ops(old_ops: dict[str, Any], new_ops: dict[str, Any]) -> dict[str, tuple[str | None, str | None]]:
    """match key -> (old op key, new op key); unmatched sides are None."""

    def match_key(op_key: str) -> str:
        method, path = _split_key(op_key)
        return f"{method} {_match_path(path)}"

    collisions: set[str] = set()
    for ops in (old_ops, new_ops):
        counts = Counter(match_key(k) for k in ops)
        collisions |= {k for k, count in counts.items() if count > 1}

    def keyed(ops: dict[str, Any]) -> dict[str, str]:
        out = {}
        for op_key in ops:
            mk = match_key(op_key)
            out[op_key if mk in collisions else mk] = op_key
        return out

    old_by, new_by = keyed(old_ops), keyed(new_ops)
    return {k: (old_by.get(k), new_by.get(k)) for k in set(old_by) | set(new_by)}


def unrecorded_state(old: dict[str, Any], new: dict[str, Any]) -> str | None:
    """Whether the two specs differ in something the index does not record.

    None: they do not (the contract hashes are equal, or the raw bytes are).
    "changed": the contract hashes differ, so something outside the recorded
    attributes changed. "unknown": the raw bytes differ and at least one input
    has no contract hash (an index written by an older spec_snapshot.py), so
    documentation-only cannot be proved.
    """
    old_hash, new_hash = old.get("contract_sha256"), new.get("contract_sha256")
    if old_hash and new_hash:
        return "changed" if old_hash != new_hash else None
    old_sha, new_sha = old.get("sha256"), new.get("sha256")
    if old_sha and new_sha and old_sha != new_sha:
        return "unknown"
    return None


def diff_indexes(old: dict[str, Any], new: dict[str, Any]) -> DiffResult:
    result = DiffResult()
    if old.get("openapi") != new.get("openapi"):
        result.top_level.append(
            f"OpenAPI version: `{old.get('openapi')}` -> `{new.get('openapi')}`"
        )
    result.top_level += diff_security(old, new)

    old_ops, new_ops = old.get("ops", {}), new.get("ops", {})
    for _, (old_key, new_key) in sorted(
        _pair_ops(old_ops, new_ops).items(), key=lambda kv: _op_sort_key(kv[1][1] or kv[1][0])
    ):
        if old_key is None:
            result.ops_added.append(new_key)
        elif new_key is None:
            result.ops_removed.append(old_key)
        else:
            old_op, new_op = old_ops[old_key], new_ops[new_key]
            if old_op.get("operation_id") != new_op.get("operation_id"):
                result.operation_id_changes.append(
                    (new_key, old_op.get("operation_id"), new_op.get("operation_id"))
                )
            lines = diff_op(old_op, new_op, result.counts)
            if lines:
                result.ops_changed.append(OpChange(old_key, new_key, lines))
            else:
                result.ops_unchanged += 1

    old_schemas, new_schemas = old.get("schemas", {}), new.get("schemas", {})
    old_map, new_map = _norm_name_map(old_schemas), _norm_name_map(new_schemas)
    result.schemas_added = sorted(new_map[k] for k in new_map if k not in old_map)
    result.schemas_removed = sorted(old_map[k] for k in old_map if k not in new_map)
    for norm_name in sorted(set(old_map) & set(new_map)):
        old_name, new_name = old_map[norm_name], new_map[norm_name]
        lines, names_only = diff_schema(old_schemas[old_name], new_schemas[new_name], result.counts)
        if lines:
            result.schemas_changed.append(SchemaChange(old_name, new_name, lines, names_only))
        else:
            result.schemas_unchanged += 1
    if not result.recorded_differences:
        result.unrecorded = unrecorded_state(old, new)
    return result


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------


def _usage(index: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """normalised schema name -> ops using it directly, and parent schemas."""
    users: dict[str, set[str]] = defaultdict(set)
    parents: dict[str, set[str]] = defaultdict(set)
    for op_key, op in index.get("ops", {}).items():
        body = op.get("body")
        if body and body.get("schema"):
            users[norm_schema_name(body["schema"])].add(f"`{op_key}` (body)")
        if body:
            # the element schema of an array body (it has no "schema" name of its own)
            for ref in _refs_in(body):
                users[norm_schema_name(ref)].add(f"`{op_key}` (body)")
        data = (op.get("response") or {}).get("data")
        for token in _TOKEN.findall(data or ""):
            if token not in _PRIMITIVES:
                users[norm_schema_name(token)].add(f"`{op_key}` (response)")
    for name, schema in index.get("schemas", {}).items():
        # field refs (inline objects, map types and oneOf parts included), plus
        # the element schema of an array schema and the parts of a oneOf or anyOf
        for ref in _deep_refs(schema):
            parents[norm_schema_name(ref)].add(f"`{norm_schema_name(name)}`")
    return (
        {k: sorted(v) for k, v in users.items()},
        {k: sorted(v) for k, v in parents.items()},
    )


def _limited(items: list[str]) -> str:
    shown = ", ".join(items[:_MAX_LISTED])
    extra = len(items) - _MAX_LISTED
    return shown + (f" (+{extra} more)" if extra > 0 else "")


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _op_row(key: str, op: dict[str, Any]) -> str:
    if op.get("paged"):
        paged = f"yes ({op.get('page_size_rule') or 'no PageSize'})"
    else:
        paged = "no"
    body = op.get("body")
    if body is None:
        body_text = "none"
    elif "type" in body:  # not an object: there are no fields to count
        body_text = f"{body['content_type']}: {entry_label(_body_shape(body))}"
        if body.get("schema"):
            body_text += f" ({_quote(body['schema'])})"
    elif body["content_type"].startswith("multipart/"):
        body_text = f"{body['content_type']}: {', '.join(sorted(body['fields']))}"
    else:
        body_text = f"{_body_label(body)} ({len(body['fields'])} fields, {len(body['required'])} required)"
    response = op.get("response") or {}
    kind = response.get("kind")
    data = response.get("data")
    response_text = kind if not data else f"{kind}: {data}"
    return f"| `{key}` | {paged} | {_cell(body_text)} | {_cell(str(response_text))} |"


def _meta_line(label: str, path: str, index: dict[str, Any]) -> str:
    sha = index.get("sha256") or ""
    contract = index.get("contract_sha256")
    contract_text = f"contract `{contract[:16]}`, " if contract else "no contract hash, "
    return (
        f"- {label}: `{path}` (source `{index.get('source')}`, sha256 `{sha[:16]}`, "
        f"{contract_text}OpenAPI {index.get('openapi')}, {len(index.get('ops', {}))} ops, "
        f"{len(index.get('schemas', {}))} schemas)"
    )


def _unrecorded_lines(result: DiffResult, old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """The Summary when no attribute the index records differs."""
    if result.unrecorded == "changed":
        return [
            "Every operation, schema and other attribute this report records is identical, "
            "but the raw contract is NOT: the contract hash differs "
            f"(`{old['contract_sha256'][:16]}` -> `{new['contract_sha256'][:16]}`).",
            "",
            "The hash covers the whole spec except descriptions, summaries and examples, so "
            "something the index does not record changed: for example a response header, an "
            "extra media type, the servers, the info block, or a constraint the index has no "
            "key for. Compare the two raw files to find it (exit status 3).",
        ]
    if result.unrecorded == "unknown":
        return [
            "Every operation, schema and other attribute this report records is identical, "
            "but the raw spec bytes differ (sha256 changed) and at least one input is a spec "
            "index written before contract hashes existed, so this report cannot tell whether "
            "only descriptions, summaries or examples changed.",
            "",
            "Regenerate that index with scripts/spec_snapshot.py (or pass the raw spec instead) "
            "and run the diff again, or compare the two raw files (exit status 3).",
        ]
    lines = ["No differences: operations and schemas are identical."]
    if old.get("sha256") and new.get("sha256") and old["sha256"] != new["sha256"]:
        lines += [
            "",
            "The raw spec bytes differ (sha256 changed) but the contract hash is equal, so "
            "only descriptions, summaries, examples or formatting changed.",
        ]
    return lines


def render_markdown(
    result: DiffResult,
    old: dict[str, Any],
    new: dict[str, Any],
    old_label: str,
    new_label: str,
) -> str:
    out: list[str] = ["# Gorelo API spec diff", ""]
    out.append(_meta_line("OLD", old_label, old))
    out.append(_meta_line("NEW", new_label, new))
    out += [
        "",
        "Only API shape is compared (paths, params with their defaults and constraints, "
        "bodies, required fields, responses, paging, schemas, deprecation, security); "
        "descriptions, summaries and examples are ignored. Names are matched "
        "case-insensitively and reported as case-only renames, operations ignore a trailing "
        "slash and path parameter names, and schema names ignore the `public-cluster_` prefix "
        "and `Public` segments. Attributes the index does not record (response headers, extra "
        "media types, servers) are not listed one by one, but a change to any of them moves "
        "the contract hash, and the report says so when nothing else differs.",
    ]
    if bool(old.get("contract_sha256")) != bool(new.get("contract_sha256")):
        out += [
            "",
            "Note: one input is a spec index written before contract hashes and constraint "
            "attributes were recorded (defaults, constraints, deprecation, security, "
            "composition). Differences in those attributes below may only reflect the older "
            "index format: regenerate that index from its raw snapshot to be sure.",
        ]
    out += ["", "## Summary", ""]

    if not result.recorded_differences:
        out += _unrecorded_lines(result, old, new)
        return _finish(out)

    out += [
        "| | OLD | NEW |",
        "|---|---|---|",
        f"| Operations | {len(old.get('ops', {}))} | {len(new.get('ops', {}))} |",
        f"| Schemas | {len(old.get('schemas', {}))} | {len(new.get('schemas', {}))} |",
        "",
    ]
    for line in result.top_level:
        out.append(f"- {line}")
    names_only_schemas = sum(1 for s in result.schemas_changed if s.names_only)
    out += [
        f"- Operations added: {len(result.ops_added)}",
        f"- Operations removed: {len(result.ops_removed)}",
        f"- Operations changed: {len(result.ops_changed)} (unchanged: {result.ops_unchanged})",
        f"- Schemas added: {len(result.schemas_added)}",
        f"- Schemas removed: {len(result.schemas_removed)}",
        f"- Schemas changed: {len(result.schemas_changed)} (of which only field-name case "
        f"or nullability differs: {names_only_schemas}; unchanged: {result.schemas_unchanged})",
        f"- Case-only renames: {result.counts['query_case_renames']} query params, "
        f"{result.counts['schema_field_case_renames']} schema fields",
        f"- operation_id changed on {len(result.operation_id_changes)} operations (last section)",
        "",
    ]

    out += [f"## Removed operations ({len(result.ops_removed)})", ""]
    out += [f"- `{key}`" for key in result.ops_removed] or ["None."]
    out.append("")

    out += [f"## Added operations ({len(result.ops_added)})", ""]
    if result.ops_added:
        out += ["| Operation | Paged (PageSize rule) | Body | Response |", "|---|---|---|---|"]
        out += [_op_row(key, new["ops"][key]) for key in result.ops_added]
    else:
        out.append("None.")
    out.append("")

    out += [f"## Changed operations ({len(result.ops_changed)})", ""]
    if not result.ops_changed:
        out += ["None.", ""]
    for change in result.ops_changed:
        title = f"`{change.new_key}`"
        if change.old_key != change.new_key:
            title += f" (was `{change.old_key}`)"
        out += [f"### {title}", ""]
        out += change.lines
        out.append("")

    users, parents = _usage(new)
    out += ["## Schemas", ""]
    out += [f"### Added ({len(result.schemas_added)})", ""]
    out.append(", ".join(f"`{n}`" for n in result.schemas_added) or "None.")
    out += ["", f"### Removed ({len(result.schemas_removed)})", ""]
    out.append(", ".join(f"`{n}`" for n in result.schemas_removed) or "None.")
    out += ["", f"### Changed ({len(result.schemas_changed)})", ""]
    if not result.schemas_changed:
        out.append("None.")
    for change in result.schemas_changed:
        title = f"`{change.new_name}`"
        if change.old_name != change.new_name:
            title += f" (was `{change.old_name}`)"
        out += [f"#### {title}", ""]
        normalised = norm_schema_name(change.new_name)
        if users.get(normalised):
            out.append(f"- used by: {_limited(users[normalised])}")
        if parents.get(normalised):
            out.append(f"- referenced by schemas: {_limited(parents[normalised])}")
        out += change.lines
        out.append("")

    changes = result.operation_id_changes
    out += [f"## operation_id changes ({len(changes)})", ""]
    if not changes:
        out.append("None.")
    elif len(changes) > 3 and len({old_id for _, old_id, _ in changes}) == 1:
        # The July spec gave every operation the same placeholder id, so a
        # column of identical OLD values says nothing.
        out += [
            f"Every changed operation had the same OLD operation_id {_quote(changes[0][1])}, "
            "so the OLD ids carry no information. The NEW ids are:",
            "",
            "| Operation | NEW |",
            "|---|---|",
        ]
        out += [f"| `{key}` | {_quote(new_id)} |" for key, _, new_id in changes]
    else:
        out += ["| Operation | OLD | NEW |", "|---|---|---|"]
        out += [
            f"| `{key}` | {_quote(old_id)} | {_quote(new_id)} |"
            for key, old_id, new_id in changes
        ]
    return _finish(out)


def _finish(lines: list[str]) -> str:
    text = "\n".join(lines).rstrip("\n") + "\n"
    # Spec-derived strings should never carry an em dash or an en dash, but
    # generated docs must not either. chr() keeps this source file free of both.
    return text.replace(chr(0x2014), "-").replace(chr(0x2013), "-")


# --------------------------------------------------------------------------
# Loading and CLI
# --------------------------------------------------------------------------


def load_index(path: str) -> dict[str, Any]:
    """Load OLD/NEW: a spec index as is, a raw swagger document via build_index."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise DiffError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise DiffError(f"{path} is not valid JSON: {exc}") from exc
    if isinstance(data, dict) and "ops" in data:
        return data
    if isinstance(data, dict) and isinstance(data.get("paths"), dict):
        return build_index(data, raw, path)
    raise DiffError(f"{path} is neither a spec index (has 'ops') nor a raw swagger file (has 'paths')")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare two Gorelo OpenAPI snapshots (raw swagger JSON or spec_index.json).",
        epilog=(
            "Exit status: 0 identical (or documentation-only changes, proved by the contract "
            "hash), 1 differences in recorded attributes, 2 error, 3 recorded attributes "
            "identical but the raw contract differs or cannot be proved identical "
            "(compare the raw files)."
        ),
    )
    parser.add_argument("old", help="OLD snapshot: raw swagger JSON or spec index")
    parser.add_argument("new", help="NEW snapshot: raw swagger JSON or spec index")
    parser.add_argument("--out", help="write the markdown report here instead of stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        old, new = load_index(args.old), load_index(args.new)
    except DiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = diff_indexes(old, new)
    report = render_markdown(result, old, new, args.old, args.new)
    if result.recorded_differences:
        code, status = 1, "differences found"
    elif result.unrecorded is not None:
        code, status = 3, "raw contract differs outside the recorded attributes (compare the raw files)"
    else:
        code, status = 0, "identical"
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8", newline="\n")
        print(
            f"{status}: wrote {args.out} "
            f"(ops +{len(result.ops_added)} -{len(result.ops_removed)} ~{len(result.ops_changed)}, "
            f"schemas +{len(result.schemas_added)} -{len(result.schemas_removed)} "
            f"~{len(result.schemas_changed)})"
        )
    else:
        sys.stdout.write(report)
    return code


if __name__ == "__main__":
    sys.exit(main())
