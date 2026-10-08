#!/usr/bin/env python3
"""Snapshot Gorelo's public OpenAPI spec and build the compact spec index.

What it does:

1. Gets the swagger JSON: by default the live public URL (no auth, no API key),
   or a local file given with --source.
2. Unless --no-save-raw, saves the raw bytes as backups/swagger-YYYYMMDD-HHMMSS.json
   (UTC) and repoints the relative symlink backups/swagger-latest.json at it.
3. Builds spec/spec_index.json: a deterministic digest of the spec that code and
   tests validate against (every operation with its path and query params, body
   fields, required fields, response envelope and paging rule, plus every
   component schema). No descriptions or summaries are kept anywhere in it.
   Gorelo's descriptions contain non-ASCII dashes, which is one reason, and the
   facts code needs (the PageSize rule) are distilled into fields instead.

The index is a pure function of the spec bytes plus the source label: no
timestamps, sorted keys, stable list order. Same spec in, same file out.

Usage (run from the repo root):

    uv run python scripts/spec_snapshot.py
    uv run python scripts/spec_snapshot.py --source backups/swagger-20260726-184500.json \\
        --no-save-raw --index /tmp/july_index.json

The default --backups-dir and --index are resolved against the repository root
(the parent of scripts/), not the current directory, so running this from
another directory cannot create stray folders. Paths you pass explicitly are
resolved the normal way, against the current directory.

Nothing is written unless the whole spec parsed and indexed: the raw snapshot,
the symlink and the index are only touched after build_index succeeded.

Index format notes:

* Query and path param names, and body and schema field names, are kept exactly
  as the spec spells them (the July 2026 spec used camelCase, later ones use
  PascalCase). Schema names are kept as is too.
* path_params entries are {"type", "format"?}; query_params entries are
  {"type", "format"?, "required"}. "ref", "items" and "enum" appear on a param
  only if its schema has them (the current spec never does).
* "paged" is true if and only if the op has a Cursor query param. The match is
  case-insensitive (Gorelo treats query names case-insensitively, and the July
  spec spelled it "cursor"); every current op spells it "Cursor".
* "page_size_rule" comes from the PageSize param description (also matched
  case-insensitively): "clamp" if it says the value is clamped, "reject" if it
  says out-of-range is rejected or a 400, null if the op has no PageSize param,
  otherwise "unknown". A negated mention ("rejected, not clamped") does not
  count, and a description that positively says both reads as "unknown".
* "body.content_type" is the first content type the spec lists for the request
  body (application/json-patch+json for JSON ops, multipart/form-data for
  uploads). "body.fields" and "body.required" come from the body schema (a
  $ref or an inline object; allOf parts are merged). A multipart body is an
  inline schema, so its "schema" is null. A body that is not an object (an
  inline array, a primitive such as a binary string, a $ref to such a schema,
  or a schema that declares no type) has no fields. It is reported as a warning
  and records "type" (null if the schema declares none) plus "format" and
  "items" when present, with the same meaning as on an array "items" entry, so
  the index never reads as "an object with no fields". Those keys are absent
  from object bodies, so absent means object.
* "response" describes the 200 response (or the lowest 2xx if there is no 200):
  kind "envelope" (a BaseResponse with IsSuccess and Data), "binary" (a file
  download), or "other". "schema" is the response schema name (null if inline
  or absent). "data" is the payload type: the Data property of an envelope, the
  data property of a legacy paged shape, the schema itself for a bare model
  body, or the type of a bare inline body. It is a schema name, [SchemaName]
  for arrays, or a primitive type name, and null when there is nothing typed to
  report (a binary download, an untyped Data property, no response content).
* Schema entries are {"type", "required", "fields"} plus "enum" for enum
  schemas, "items" for array schemas and "format" when the schema declares one
  (int32 on an integer enum schema, for example). Field entries are
  {"type", "nullable"} plus "format", "ref", "enum" and "items" when present;
  "type" is null for a $ref. Array "items" entries use the same keys without
  "nullable" and "enum".
* Constraint keys appear on a param, field, "items" or schema entry only if the
  spec declares them, and carry the spec's value: "default", "min_length",
  "max_length", "pattern", "minimum", "maximum", "exclusive_minimum",
  "exclusive_maximum", "multiple_of", "min_items", "max_items". The flags
  "deprecated", "read_only", "write_only" and "unique_items" appear only when
  true (absent means false). A param entry also takes "style" and "explode"
  (as declared) and "allow_reserved", "allow_empty_value" and "deprecated"
  (only when true) from the parameter object. A schema, field or "items" entry
  carries "additional_properties": the declared boolean, or an "items" style
  entry for a map type. The live spec declares false on every object schema,
  which is how "an unknown body field is a 400" shows up in the index.
* A param, field or "items" entry that is a oneOf, anyOf or allOf, and a
  component schema that is a oneOf or anyOf, records "one_of", "any_of" or
  "all_of" when it has several real parts, or one real part next to a type or
  properties of its own (which would otherwise be lost): the parts as "items"
  style entries, sorted. Their own fields are not merged into the entry, so
  each one is also reported as a warning. A component schema that is an allOf
  is merged into its fields, as before (inheritance). A oneOf, anyOf or allOf
  with one real part (plus an optional null) and nothing else is just that
  part. A request body that is itself a oneOf or anyOf is not recorded, only
  reported as a warning (see the body notes above).
* A field or "items" entry that is an object defined inline, with properties of
  its own, carries "fields" and "required_fields" (the shape of a schema's
  "fields" and "required"); "type" is "object" if the spec declared none. A
  free-form object has neither key.
* An op carries "deprecated" (only when true) and "security" (its own
  requirement list, normalised) when it declares them. A body entry carries
  "body_required" (true) when requestBody.required is true; its "required" is
  still the list of required field names.
* Top level: "security_schemes" ({name: {"type", "in", "name", "scheme",
  "bearer_format", "open_id_connect_url"}, keeping the keys the scheme has}),
  "security" (the top-level requirement list, normalised) and "contract_sha256".
* "contract_sha256" is the sha256 of the whole spec after removing every
  "description", "summary", "example" and "examples" key (property names are
  kept, see contract_sha256()). scripts/spec_diff.py uses it to prove that two
  specs whose raw bytes differ differ only in documentation. Whatever the index
  does not record, such as response headers, extra media types or servers,
  still moves this hash, so a diff can never claim "only descriptions changed"
  unless that is true.
  scripts/spec_diff.py compares every attribute listed in these notes.

Constructs the format cannot represent in full (header or cookie parameters, a
oneOf, anyOf or multi-part allOf whose alternatives are recorded but not merged,
an unresolvable $ref, a request body that is not an object) are not silently
dropped: build_index_with_warnings returns them and the CLI prints them to
stderr.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_SOURCE = "https://api.usw.gorelo.io/swagger/v1/swagger.json"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKUPS_DIR = REPO_ROOT / "backups"
DEFAULT_INDEX = REPO_ROOT / "spec" / "spec_index.json"
LATEST_LINK_NAME = "swagger-latest.json"
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
FETCH_TIMEOUT_SECONDS = 60
USER_AGENT = "gorelo-mcp-spec-snapshot/1.0 (unauthenticated public spec fetch)"

_JSON_MEDIA_PREFERENCE = ("application/json", "text/json", "text/plain")
_BINARY_MEDIA_TYPES = ("application/pdf", "application/octet-stream")

# Constraint-like attributes copied onto param, field, items and schema entries
# when the spec declares them (spec key -> index key). Values are copied as the
# spec spells them. Booleans whose absence means false are only recorded when
# true, so "deprecated: false" and no key read the same.
_VALUE_FACETS = {
    "default": "default",
    "minLength": "min_length",
    "maxLength": "max_length",
    "pattern": "pattern",
    "minimum": "minimum",
    "maximum": "maximum",
    "exclusiveMinimum": "exclusive_minimum",
    "exclusiveMaximum": "exclusive_maximum",
    "multipleOf": "multiple_of",
    "minItems": "min_items",
    "maxItems": "max_items",
}
_FLAG_FACETS = {
    "deprecated": "deprecated",
    "readOnly": "read_only",
    "writeOnly": "write_only",
    "uniqueItems": "unique_items",
}
# The same, from the parameter object itself (how an array is serialised, and
# whether the parameter is deprecated) rather than from its schema.
_DEFAULT_STYLE = {"query": "form", "cookie": "form", "path": "simple", "header": "simple"}
_PARAM_VALUE_FACETS = {"style": "style", "explode": "explode"}
_PARAM_FLAG_FACETS = {
    "deprecated": "deprecated",
    "allowReserved": "allow_reserved",
    "allowEmptyValue": "allow_empty_value",
}
_COMPOSITION_KEYS = (("oneOf", "one_of"), ("anyOf", "any_of"), ("allOf", "all_of"))

# Every index key beyond type, format, ref, items, enum, nullable and required
# that scripts/spec_diff.py compares one by one on a param, field, items or
# schema entry. It lives here so that recording an attribute and diffing it
# cannot drift apart.
FACET_KEYS = tuple(
    dict.fromkeys(
        (
            *_VALUE_FACETS.values(),
            *_FLAG_FACETS.values(),
            *_PARAM_VALUE_FACETS.values(),
            *_PARAM_FLAG_FACETS.values(),
            "additional_properties",
            *(index_key for _, index_key in _COMPOSITION_KEYS),
        )
    )
)

# Documentation keys that contract_sha256() leaves out, and the keys whose
# value is a map from a name to an object (a property may be called
# "description", so the keys of such a map are names, never documentation).
# default, enum and const hold arbitrary data and are kept verbatim.
_DOC_KEYS = frozenset({"description", "summary", "example", "examples"})
_OPAQUE_KEYS = frozenset({"default", "enum", "const"})
_NAME_MAP_KEYS = frozenset(
    {
        "properties", "patternProperties", "schemas", "responses", "requestBodies",
        "headers", "securitySchemes", "links", "callbacks", "content", "encoding",
        "paths", "mapping", "variables", "definitions", "$defs", "dependentSchemas",
    }
)

# PageSize description vocabulary. A mention only counts when it is not negated
# ("not clamped", "never clamped", "no clamping"), so "Out of range is
# rejected, not clamped" reads as reject, not as both.
_CLAMP_WORD = re.compile(r"\bclamp(?:ed|s|ing)?\b", re.IGNORECASE)
_REJECT_WORD = re.compile(
    r"\breject(?:ed|s|ing)?\b|\bout of range is a 400\b", re.IGNORECASE
)
_NEGATION_BEFORE = re.compile(
    r"(?:\b(?:not|never|no|without)\b|n't)\W+(?:\w+\W+)?$", re.IGNORECASE
)


class SnapshotError(Exception):
    """A problem the user can act on (bad source, bad spec, unwritable path)."""


# --------------------------------------------------------------------------
# Spec access helpers
# --------------------------------------------------------------------------


class _Spec:
    """The parts of an OpenAPI document that $ref lookups need, plus warnings."""

    def __init__(self, spec: dict[str, Any]) -> None:
        components = spec.get("components") or {}
        self.schemas: dict[str, Any] = components.get("schemas") or {}
        self.parameters: dict[str, Any] = components.get("parameters") or {}
        self.request_bodies: dict[str, Any] = components.get("requestBodies") or {}
        self.warnings: list[str] = []

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _unwrap_wrapper(schema: dict[str, Any], ctx: _Spec | None = None, where: str = "") -> dict[str, Any]:
    """Collapse a one-schema wrapper into that schema.

    Swashbuckle writes a nullable $ref as {"allOf": [{"$ref": X}], "nullable": true};
    OpenAPI 3.1 writes {"anyOf": [{"$ref": X}, {"type": "null"}]}. Both mean "X,
    possibly null". A oneOf/anyOf with several real alternatives cannot be
    collapsed: it is left as is, and reported through ctx when a caller passes
    one (the callers that record the alternatives themselves, see
    _alternatives, pass None and report them there).
    """
    if "$ref" in schema or "properties" in schema or schema.get("type"):
        return schema
    for key in ("allOf", "oneOf", "anyOf"):
        parts = schema.get(key)
        if not isinstance(parts, list) or not parts:
            continue
        real = [p for p in parts if isinstance(p, dict) and p.get("type") != "null"]
        has_null = len(real) != len(parts)
        if len(real) == 1:
            merged = dict(real[0])
            for name, value in schema.items():
                if name != key:
                    merged.setdefault(name, value)
            if has_null:
                merged["nullable"] = True
            return merged
        if key != "allOf" and ctx is not None:
            ctx.warn(f"{where or 'schema'}: {key} with {len(real)} alternatives is not represented")
    return schema


def _members(
    schema: dict[str, Any] | None,
    ctx: _Spec,
    seen: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], list[str]]:
    """(properties, required) of an object schema, following $ref and allOf.

    Later allOf parts win on a name clash and the schema's own properties win
    over its parts. A cyclic $ref chain stops instead of recursing forever.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    if not isinstance(schema, dict):
        return properties, required
    if "$ref" in schema:
        name = _ref_name(schema["$ref"])
        if name in seen:
            return properties, required
        target = ctx.schemas.get(name)
        if not isinstance(target, dict):
            ctx.warn(f"unresolved $ref {schema['$ref']}")
            return properties, required
        return _members(target, ctx, seen | {name})
    for part in schema.get("allOf") or []:
        part_properties, part_required = _members(part, ctx, seen)
        properties.update(part_properties)
        required.extend(part_required)
    properties.update(schema.get("properties") or {})
    required.extend(schema.get("required") or [])
    return properties, required


def _type_of(schema: dict[str, Any]) -> tuple[str | None, bool]:
    """(type, nullable) tolerating OpenAPI 3.1 style type lists."""
    declared = schema.get("type")
    nullable = bool(schema.get("nullable", False))
    if isinstance(declared, list):
        non_null = [t for t in declared if t != "null"]
        nullable = nullable or ("null" in declared)
        declared = non_null[0] if non_null else None
    return declared, nullable


def _facets(source: dict[str, Any], values: dict[str, str], flags: dict[str, str]) -> dict[str, Any]:
    """The constraint keys source declares, under their index names."""
    found: dict[str, Any] = {}
    for spec_key, index_key in values.items():
        if spec_key in source:
            found[index_key] = copy.deepcopy(source[spec_key])
    for spec_key, index_key in flags.items():
        if source.get(spec_key) is True:
            found[index_key] = True
    return found


def _additional_properties(schema: dict[str, Any], ctx: _Spec, where: str) -> dict[str, Any]:
    """{"additional_properties": bool or items-style entry} if the schema declares it."""
    declared = schema.get("additionalProperties")
    if isinstance(declared, bool):
        return {"additional_properties": declared}
    if isinstance(declared, dict):
        return {"additional_properties": _items_entry(declared, ctx, where)}
    return {}


def _alternatives(
    schema: dict[str, Any],
    ctx: _Spec,
    where: str,
    keys: tuple[tuple[str, str], ...] = _COMPOSITION_KEYS,
) -> dict[str, list[dict[str, Any]]]:
    """one_of, any_of and all_of entries for a schema composed of real parts.

    Callers unwrap the schema first (_unwrap_wrapper), which collapses a
    composition of one real part (plus an optional {"type": "null"}) into that
    part, as in a nullable $ref. What is left is several real parts, or one part
    next to a type or properties of the schema's own, which would be lost. The
    parts are recorded, sorted so that their order in the spec does not matter,
    but their fields are not merged into the entry, and that is reported.
    """
    found: dict[str, list[dict[str, Any]]] = {}
    for spec_key, index_key in keys:
        parts = schema.get(spec_key)
        if not isinstance(parts, list):
            continue
        real = [part for part in parts if isinstance(part, dict) and part.get("type") != "null"]
        if not real:
            continue
        entries = [_items_entry(part, ctx, where) for part in real]
        found[index_key] = sorted(entries, key=lambda e: json.dumps(e, sort_keys=True))
        noun = "part" if spec_key == "allOf" else "alternative"
        ctx.warn(
            f"{where}: {spec_key} with {len(real)} {noun}{'' if len(real) == 1 else 's'} "
            f"is recorded as {index_key} only, its fields are not merged"
        )
    return found


def _inline_fields(schema: dict[str, Any], ctx: _Spec, where: str) -> dict[str, Any]:
    """{"fields", "required_fields"} for an object defined inline with properties of its own.

    Only the literal properties are read, so nothing is followed through a $ref
    and a recursive type cannot loop. A $ref has no properties here: its schema
    is in the components.
    """
    properties = schema.get("properties")
    if "$ref" in schema or not isinstance(properties, dict) or not properties:
        return {}
    return {
        "fields": {
            name: _field_entry(prop, ctx, f"{where}.{name}") for name, prop in properties.items()
        },
        "required_fields": sorted(set(schema.get("required") or [])),
    }


def _add_shape(entry: dict[str, Any], schema: dict[str, Any], ctx: _Spec, where: str) -> None:
    """Keys that field and items entries share beyond type, nullable and enum."""
    declared = entry["type"]
    if schema.get("format"):
        entry["format"] = schema["format"]
    if "$ref" in schema:
        entry["ref"] = _ref_name(schema["$ref"])
    if declared == "array" and isinstance(schema.get("items"), dict):
        entry["items"] = _items_entry(schema["items"], ctx, where)
    entry.update(_facets(schema, _VALUE_FACETS, _FLAG_FACETS))
    entry.update(_additional_properties(schema, ctx, where))
    entry.update(_alternatives(schema, ctx, where))
    entry.update(_inline_fields(schema, ctx, where))
    if declared is None and "fields" in entry:
        entry["type"] = "object"


def _items_entry(schema: dict[str, Any], ctx: _Spec, where: str) -> dict[str, Any]:
    schema = _unwrap_wrapper(schema, None, where)
    declared, _ = _type_of(schema)
    entry: dict[str, Any] = {"type": declared}
    _add_shape(entry, schema, ctx, where)
    return entry


def _field_entry(schema: dict[str, Any] | None, ctx: _Spec, where: str) -> dict[str, Any]:
    """Compact description of one object property (see the module docstring)."""
    schema = _unwrap_wrapper(schema or {}, None, where)
    declared, nullable = _type_of(schema)
    entry: dict[str, Any] = {"type": declared, "nullable": nullable}
    if "enum" in schema:
        entry["enum"] = list(schema["enum"])
    _add_shape(entry, schema, ctx, where)
    return entry


def _param_entry(schema: dict[str, Any] | None, ctx: _Spec, where: str) -> dict[str, Any]:
    """Compact description of a parameter's schema: type, format, ref, items, enum, constraints."""
    schema = _unwrap_wrapper(schema or {}, None, where)
    ref = None
    if "$ref" in schema:
        ref = _ref_name(schema["$ref"])
        target = ctx.schemas.get(ref)
        if not isinstance(target, dict):
            ctx.warn(f"{where}: unresolved $ref {schema['$ref']}")
            target = {}
        declared, _ = _type_of(target)
        fmt = target.get("format")
        enum = target.get("enum")
        # the referenced schema's constraints apply, overridden by any written next to the $ref
        effective = {**target, **{k: v for k, v in schema.items() if k != "$ref"}}
    else:
        declared, _ = _type_of(schema)
        fmt = schema.get("format")
        enum = schema.get("enum")
        effective = schema
    entry: dict[str, Any] = {"type": declared}
    if fmt:
        entry["format"] = fmt
    if ref:
        entry["ref"] = ref
    if enum is not None:
        entry["enum"] = list(enum)
    if declared == "array" and isinstance(schema.get("items"), dict):
        entry["items"] = _items_entry(schema["items"], ctx, where)
    entry.update(_facets(effective, _VALUE_FACETS, _FLAG_FACETS))
    entry.update(_alternatives(schema, ctx, where))
    return entry


def _type_str(schema: dict[str, Any] | None) -> str | None:
    """SchemaName, [SchemaName], or a primitive type name; None if untyped."""
    schema = _unwrap_wrapper(schema or {})
    if "$ref" in schema:
        return _ref_name(schema["$ref"])
    declared, _ = _type_of(schema)
    if declared == "array":
        items = schema.get("items")
        inner = _type_str(items) if isinstance(items, dict) else None
        return f"[{inner or 'any'}]"
    return declared


def _schema_entry(name: str, schema: dict[str, Any], ctx: _Spec) -> dict[str, Any]:
    where = f"schema {name}"
    # A oneOf, anyOf or allOf with one real alternative is that alternative (an
    # alias, possibly nullable); several alternatives are recorded below.
    schema = _unwrap_wrapper(schema, None, where)
    properties, required = _members(schema, ctx, frozenset({name}))
    declared, _ = _type_of(schema)
    if declared is None and properties:
        declared = "object"
    entry: dict[str, Any] = {
        "type": declared,
        "required": sorted(set(required)),
        "fields": {
            field: _field_entry(prop, ctx, f"{where}.{field}")
            for field, prop in properties.items()
        },
    }
    if "enum" in schema:
        entry["enum"] = list(schema["enum"])
    if declared == "array" and isinstance(schema.get("items"), dict):
        entry["items"] = _items_entry(schema["items"], ctx, where)
    if schema.get("format"):
        entry["format"] = schema["format"]
    entry.update(_facets(schema, _VALUE_FACETS, _FLAG_FACETS))
    entry.update(_additional_properties(schema, ctx, where))
    # an allOf on a component schema is inheritance: _members already merged it
    entry.update(_alternatives(schema, ctx, where, _COMPOSITION_KEYS[:2]))
    return entry


# --------------------------------------------------------------------------
# Per-operation pieces
# --------------------------------------------------------------------------


def _mentioned(pattern: re.Pattern[str], text: str) -> bool:
    """True if pattern occurs in text at least once without a negation before it."""
    for match in pattern.finditer(text):
        if not _NEGATION_BEFORE.search(text[: match.start()]):
            return True
    return False


def classify_page_size(description: str | None) -> str:
    """Map a PageSize param description to "clamp", "reject" or "unknown".

    Current Gorelo wordings, all handled: "Clamped to 1-200", "Out of range is
    rejected, not clamped", "out of range is a 400, never clamped", "Rejected
    with a 400 outside 1-200", and "Comments per page, 1-200. Defaults to 50"
    (which says nothing about out-of-range values, so "unknown").
    """
    text = description or ""
    clamps = _mentioned(_CLAMP_WORD, text)
    rejects = _mentioned(_REJECT_WORD, text)
    if rejects and not clamps:
        return "reject"
    if clamps and not rejects:
        return "clamp"
    return "unknown"


def _collect_params(
    path_item: dict[str, Any], op: dict[str, Any], ctx: _Spec, where: str
) -> list[dict[str, Any]]:
    """Path-level params overlaid by op-level params, $refs resolved."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for source in (path_item.get("parameters"), op.get("parameters")):
        for param in source or []:
            if isinstance(param, dict) and "$ref" in param:
                resolved = ctx.parameters.get(_ref_name(param["$ref"]))
                if not isinstance(resolved, dict):
                    ctx.warn(f"{where}: unresolved $ref {param['$ref']}")
                    continue
                param = resolved
            if isinstance(param, dict) and param.get("name") and param.get("in"):
                merged[(param["in"], param["name"])] = param
    return list(merged.values())


def _resolve_alias(schema: dict[str, Any], ctx: _Spec, where: str) -> dict[str, Any] | None:
    """Follow $ref aliases to the schema that declares the type.

    None if a $ref cannot be resolved (_members reports that, so this stays
    quiet) or the chain is circular (reported here).
    """
    seen: set[str] = set()
    schema = _unwrap_wrapper(schema)
    while "$ref" in schema:
        name = _ref_name(schema["$ref"])
        if name in seen:
            ctx.warn(f"{where}: circular $ref chain at {schema['$ref']}")
            return None
        target = ctx.schemas.get(name)
        if not isinstance(target, dict):
            return None
        seen.add(name)
        schema = _unwrap_wrapper(target)
    return schema


def _non_object_body(
    schema: dict[str, Any], name: str | None, ctx: _Spec, where: str
) -> dict[str, Any] | None:
    """Shape of a request body that has no fields because it is not an object.

    Called when the body schema yielded no properties. Returns None if it is an
    object (an empty one: nothing was dropped) or its $ref cannot be resolved
    (already reported). Otherwise it warns and returns the keys to add to the
    body entry: "type" (null if the schema declares none), plus "format" and
    "items" when present, so the index does not claim an object with no fields.
    """
    resolved = _resolve_alias(schema, ctx, where)
    if resolved is None:
        return None
    declared, _ = _type_of(resolved)
    if declared == "object":
        return None
    kind = declared or "untyped"
    if name is not None:
        ctx.warn(f"{where}: request body schema {name} is {kind}, fields not indexed")
    else:
        ctx.warn(f"{where}: request body is an inline {kind} schema, fields not indexed")
    # A oneOf, anyOf or allOf on the body itself is reported above (the body keeps no
    # one_of keys); one inside an array's items is recorded by the items entry.
    own = {key: value for key, value in resolved.items() if key not in ("oneOf", "anyOf", "allOf")}
    shape = _items_entry(own, ctx, where)
    return {key: shape[key] for key in ("type", "format", "items") if key in shape}


def _body_entry(op: dict[str, Any], ctx: _Spec, where: str) -> dict[str, Any] | None:
    body = op.get("requestBody")
    if isinstance(body, dict) and "$ref" in body:
        resolved = ctx.request_bodies.get(_ref_name(body["$ref"]))
        if not isinstance(resolved, dict):
            ctx.warn(f"{where}: unresolved $ref {body['$ref']}")
        body = resolved
    if not isinstance(body, dict):
        return None
    content = body.get("content") or {}
    if not content:
        ctx.warn(f"{where}: request body without content is not indexed")
        return None
    content_type = next(iter(content))
    schema = _unwrap_wrapper((content[content_type] or {}).get("schema") or {}, ctx, where)
    name = _ref_name(schema["$ref"]) if "$ref" in schema else None
    properties, required = _members(schema, ctx)
    entry: dict[str, Any] = {
        "content_type": content_type,
        "schema": name,
        "fields": {
            field: _field_entry(prop, ctx, f"{where} body.{field}")
            for field, prop in properties.items()
        },
        "required": sorted(set(required)),
    }
    if body.get("required") is True:
        entry["body_required"] = True
    if not properties:
        entry.update(_non_object_body(schema, name, ctx, where) or {})
    return entry


def _is_binary_media(content_type: str, schema: dict[str, Any]) -> bool:
    return (
        schema.get("format") == "binary"
        or content_type in _BINARY_MEDIA_TYPES
        or content_type.startswith(("image/", "audio/", "video/"))
    )


def _pick_json_media(content: dict[str, Any]) -> str | None:
    for candidate in _JSON_MEDIA_PREFERENCE:
        if candidate in content:
            return candidate
    for candidate in content:
        if candidate.endswith("+json"):
            return candidate
    return None


def _response_entry(op: dict[str, Any], ctx: _Spec, where: str) -> dict[str, Any]:
    responses = op.get("responses") or {}
    code = "200" if "200" in responses else next(
        (c for c in sorted(responses) if str(c).startswith("2")), None
    )
    other: dict[str, Any] = {"kind": "other", "schema": None, "data": None}
    if code is None:
        return other
    response = responses[code] or {}
    if "$ref" in response:
        ctx.warn(f"{where}: response {code} is a $ref ({response['$ref']}), which is not resolved")
    content = response.get("content") or {}
    if not content:
        return other

    media = _pick_json_media(content)
    if media is None:
        first = next(iter(content))
        schema = (content[first] or {}).get("schema") or {}
        if _is_binary_media(first, schema):
            return {"kind": "binary", "schema": None, "data": None}
        return other

    schema = _unwrap_wrapper((content[media] or {}).get("schema") or {})
    if "$ref" not in schema:
        # Inline body, for example a bare boolean. Report its own type.
        return {"kind": "other", "schema": None, "data": _type_str(schema)}

    name = _ref_name(schema["$ref"])
    props, _ = _members(schema, ctx)
    by_lower = {key.lower(): key for key in props}
    kind = "envelope" if ("issuccess" in by_lower and "data" in by_lower) else "other"
    data_prop = props.get(by_lower["data"]) if "data" in by_lower else None
    if isinstance(data_prop, dict):
        data = _type_str(data_prop)
    elif kind == "other":
        data = name  # a bare model body: the schema is the payload
    else:
        data = None
    return {"kind": kind, "schema": name, "data": data}


def _status_codes(op: dict[str, Any]) -> list[str]:
    codes = [str(c) for c in (op.get("responses") or {})]
    return sorted(codes, key=lambda c: (not c.isdigit(), c))


def _security_requirements(value: Any) -> list[dict[str, list[str]]]:
    """A security requirement list in canonical order: [{scheme: [scopes]}].

    The requirements are alternatives and the scopes of one scheme are a set,
    so both are sorted: reordering them in the spec is not a change.
    """
    requirements = []
    for requirement in value if isinstance(value, list) else []:
        if isinstance(requirement, dict):
            requirements.append(
                {
                    str(name): sorted(str(scope) for scope in scopes) if isinstance(scopes, list) else []
                    for name, scopes in requirement.items()
                }
            )
    return sorted(requirements, key=lambda r: json.dumps(r, sort_keys=True))


def _scheme_entry(scheme: dict[str, Any]) -> dict[str, Any]:
    """The parts of a security scheme a client has to match (no description, no OAuth flows)."""
    keys = {
        "type": "type",
        "in": "in",
        "name": "name",
        "scheme": "scheme",
        "bearerFormat": "bearer_format",
        "openIdConnectUrl": "open_id_connect_url",
    }
    return {index_key: scheme[spec_key] for spec_key, index_key in keys.items() if spec_key in scheme}


def _operation_entry(
    method: str, path: str, path_item: dict[str, Any], op: dict[str, Any], ctx: _Spec
) -> dict[str, Any]:
    where = f"{method.upper()} {path}"
    path_params: dict[str, Any] = {}
    query_params: dict[str, Any] = {}
    paged = False
    rule: str | None = None
    for param in _collect_params(path_item, op, ctx, where):
        location = param["in"]
        if location not in ("path", "query"):
            ctx.warn(f"{where}: {location} parameter {param['name']} is not represented in the index")
            continue
        entry = _param_entry(param.get("schema"), ctx, where)
        entry.update(_facets(param, _PARAM_VALUE_FACETS, _PARAM_FLAG_FACETS))
        if location == "path":
            path_params[param["name"]] = entry
        else:
            entry["required"] = bool(param.get("required", False))
            query_params[param["name"]] = entry
            lowered = param["name"].lower()
            if lowered == "cursor":
                paged = True
            elif lowered == "pagesize":
                rule = classify_page_size(param.get("description"))

    op_entry: dict[str, Any] = {
        "method": method.upper(),
        "path": path,
        "operation_id": op.get("operationId"),
        "path_params": path_params,
        "query_params": query_params,
        "paged": paged,
        "page_size_rule": rule,
        "body": _body_entry(op, ctx, where),
        "response": _response_entry(op, ctx, where),
        "status_codes": _status_codes(op),
    }
    if op.get("deprecated") is True:
        op_entry["deprecated"] = True
    if "security" in op:
        op_entry["security"] = _security_requirements(op["security"])
    return op_entry


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def _without_docs(node: Any, names: bool = False) -> Any:
    """Copy of a spec fragment without its documentation keys (see _DOC_KEYS).

    names is True while node is a map from names to objects (properties,
    schemas, responses, ...): its keys are names, so a property called
    "description" stays, although the same key inside a schema is dropped.
    """
    if isinstance(node, list):
        return [_without_docs(item) for item in node]
    if not isinstance(node, dict):
        return node
    kept: dict[str, Any] = {}
    for key, value in node.items():
        if names:
            kept[key] = _without_docs(value)
        elif key in _DOC_KEYS:
            continue
        elif key in _OPAQUE_KEYS:
            kept[key] = value
        else:
            # components.parameters is a map, an operation's parameters is a list
            is_map = key in _NAME_MAP_KEYS or (key == "parameters" and isinstance(value, dict))
            kept[key] = _without_docs(value, is_map)
    return kept


def _normalize_parameter(param: dict[str, Any]) -> dict[str, Any]:
    """Copy of a parameter object without style/explode when they equal the OpenAPI 3.0 default.

    Defaults: style "form" for query and cookie, "simple" for path and header; explode true
    when the effective style is "form", false otherwise. An explode value is judged against the
    effective style (the written one, else the location default).
    """
    default_style = _DEFAULT_STYLE[param["in"]]
    out = dict(param)
    effective = out.get("style", default_style)
    if out.get("style") == default_style:
        del out["style"]
    if "explode" in out and out["explode"] is (effective == "form"):
        del out["explode"]
    return out


def normalize_spec(spec: Any) -> Any:
    """Copy of the spec with every default parameter style/explode removed.

    Gorelo's published spec flaps between a rendering that writes style ("form" on query
    parameters, "simple" on path parameters) and one that omits it. Both mean the same, so the
    index and the contract hash are built from this normalised copy and the flap is invisible.
    A non-default style (deepObject, spaceDelimited, explode false on a query parameter...) stays.
    A parameter object is a dict whose "in" is a known location and whose "name" is a string
    (inline in an operation or path item, or under components.parameters).
    """
    if isinstance(spec, list):
        return [normalize_spec(item) for item in spec]
    if not isinstance(spec, dict):
        return spec
    out = {key: normalize_spec(value) for key, value in spec.items()}
    if isinstance(out.get("in"), str) and out["in"] in _DEFAULT_STYLE and isinstance(out.get("name"), str):
        out = _normalize_parameter(out)
    return out


def contract_sha256(spec: dict[str, Any]) -> str:
    """sha256 of the whole spec except descriptions, summaries and examples.

    Canonical JSON (sorted keys, no whitespace, ASCII) of the parsed spec with
    every "description", "summary", "example" and "examples" key removed, so
    formatting and key order do not matter and documentation edits do not move
    it. Everything else does, including what the index does not record
    (response headers, extra media types, servers, info). That makes it the
    proof behind "only documentation changed" in scripts/spec_diff.py.
    """
    canonical = json.dumps(_without_docs(normalize_spec(spec)), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def build_index_with_warnings(
    spec: dict[str, Any], raw: bytes, source: str
) -> tuple[dict[str, Any], list[str]]:
    """Build the spec index and the list of things it could not represent."""
    spec = normalize_spec(spec)
    ctx = _Spec(spec)
    ops: dict[str, Any] = {}
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            op = path_item.get(method)
            if isinstance(op, dict):
                key = f"{method.upper()} {path}"
                ops[key] = _operation_entry(method, path, path_item, op, ctx)
    schemas = {
        name: _schema_entry(name, schema, ctx)
        for name, schema in ctx.schemas.items()
        if isinstance(schema, dict)
    }
    index: dict[str, Any] = {
        "source": source,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "contract_sha256": contract_sha256(spec),
        "openapi": spec.get("openapi"),
        "op_count": len(ops),
        "ops": ops,
        "schemas": schemas,
    }
    schemes = (spec.get("components") or {}).get("securitySchemes")
    if isinstance(schemes, dict) and schemes:
        index["security_schemes"] = {
            name: _scheme_entry(scheme) for name, scheme in schemes.items() if isinstance(scheme, dict)
        }
    if "security" in spec:
        index["security"] = _security_requirements(spec["security"])
    return index, ctx.warnings


def build_index(spec: dict[str, Any], raw: bytes, source: str) -> dict[str, Any]:
    """Build the spec index dict from a parsed spec and its raw bytes.

    `raw` is hashed for the sha256 field; `source` is stored as given (a URL or
    a file path). The result contains no descriptions or summaries and no
    timestamps, so identical spec bytes always produce an identical index.
    """
    return build_index_with_warnings(spec, raw, source)[0]


def dumps_index(index: dict[str, Any]) -> str:
    """Canonical serialisation: sorted keys, indent 1, UTF-8 as is, final newline.

    The index copies a few strings from the spec (enum values, defaults, patterns).
    If one ever holds an em dash or an en dash, it is written as the JSON escape
    (\\u2014, \\u2013), which reads back as the same string, so that the file never
    contains either character (house rule, pinned by the tests).
    """
    text = json.dumps(index, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
    return text.replace(chr(0x2014), "\\u2014").replace(chr(0x2013), "\\u2013")


def summarize(index: dict[str, Any]) -> dict[str, Any]:
    ops = index["ops"].values()
    rules = Counter((op["page_size_rule"] or "none") for op in ops)
    return {
        "ops": len(index["ops"]),
        "paged": sum(1 for op in ops if op["paged"]),
        "rules": dict(sorted(rules.items())),
        "bodies": sum(1 for op in ops if op["body"] is not None),
        "schemas": len(index["schemas"]),
    }


# --------------------------------------------------------------------------
# Fetching and saving
# --------------------------------------------------------------------------


def fetch_spec_bytes(source: str) -> bytes:
    """GET the spec (http/https, no credentials) or read a local file."""
    if re.match(r"^https?://", source, re.IGNORECASE):
        request = urllib.request.Request(
            source, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise SnapshotError(f"GET {source} failed: HTTP {exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SnapshotError(f"GET {source} failed: {exc}") from exc
    try:
        return Path(source).read_bytes()
    except OSError as exc:
        raise SnapshotError(f"cannot read {source}: {exc}") from exc


def parse_spec(raw: bytes) -> dict[str, Any]:
    try:
        spec = json.loads(raw)
    except ValueError as exc:
        raise SnapshotError(f"source is not valid JSON: {exc}") from exc
    if not isinstance(spec, dict) or "openapi" not in spec or not isinstance(spec.get("paths"), dict):
        raise SnapshotError("source is not an OpenAPI 3 document (needs 'openapi' and 'paths')")
    return spec


def _repoint_latest(backups_dir: Path, target_name: str) -> Path:
    """Atomically point backups/swagger-latest.json at target_name (relative)."""
    link = backups_dir / LATEST_LINK_NAME
    if link.exists() and not link.is_symlink():
        raise SnapshotError(f"{link} exists and is not a symlink; refusing to replace it")
    tmp = backups_dir / f".{LATEST_LINK_NAME}.tmp-{os.getpid()}"
    try:
        os.symlink(target_name, tmp)
        os.replace(tmp, link)
    finally:
        if tmp.is_symlink():
            tmp.unlink()
    return link


def save_raw(raw: bytes, backups_dir: Path) -> Path:
    """Write raw bytes to backups/swagger-YYYYMMDD-HHMMSS.json (UTC), never overwriting."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = backups_dir / f"swagger-{stamp}.json"
        try:
            with open(target, "xb") as fh:
                fh.write(raw)
        except FileExistsError:
            time.sleep(1.1)  # same-second collision: wait for a fresh stamp
            continue
        if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(raw).digest():
            raise SnapshotError(f"{target} does not match the downloaded bytes after writing")
        _repoint_latest(backups_dir, target.name)
        return target
    raise SnapshotError(f"could not pick an unused swagger-<timestamp>.json name in {backups_dir}")


def write_index(index: dict[str, Any], path: Path) -> None:
    text = dumps_index(index)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _display(path: Path) -> str:
    """Repo-relative path for messages. absolute() first so a symlink keeps its own name."""
    for candidate in (path.absolute(), path.resolve()):
        try:
            return str(candidate.relative_to(REPO_ROOT))
        except ValueError:
            continue
    return str(path)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Snapshot the Gorelo OpenAPI spec and build spec/spec_index.json.",
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        help="spec URL or local file path (default: %(default)s)",
    )
    parser.add_argument(
        "--backups-dir",
        default=None,
        help="where raw snapshots go (default: backups under the repo root)",
    )
    parser.add_argument(
        "--index",
        default=None,
        help="spec index output path (default: spec/spec_index.json under the repo root)",
    )
    parser.add_argument(
        "--no-save-raw",
        action="store_true",
        help="do not save the raw spec or touch the swagger-latest.json symlink",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    backups_dir = Path(args.backups_dir) if args.backups_dir else DEFAULT_BACKUPS_DIR
    index_path = Path(args.index) if args.index else DEFAULT_INDEX
    try:
        raw = fetch_spec_bytes(args.source)
        spec = parse_spec(raw)
        index, warnings = build_index_with_warnings(spec, raw, args.source)
        saved = None if args.no_save_raw else save_raw(raw, backups_dir)
        write_index(index, index_path)
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for message in warnings:
        print(f"warning: {message}", file=sys.stderr)
    stats = summarize(index)
    rules = ", ".join(f"{name}={count}" for name, count in stats["rules"].items())
    print(f"source:                   {index['source']}")
    print(f"sha256:                   {index['sha256']}")
    print(f"contract sha256:          {index['contract_sha256']}")
    if saved is None:
        print("raw snapshot:             not saved (--no-save-raw)")
    else:
        print(f"raw snapshot:             {_display(saved)}")
        print(f"latest symlink:           {_display(backups_dir / LATEST_LINK_NAME)} -> {saved.name}")
    print(f"index:                    {_display(index_path)}")
    print(f"operations:               {stats['ops']}")
    print(f"paged operations:         {stats['paged']}")
    print(f"ops by page_size_rule:    {rules}")
    print(f"operations with bodies:   {stats['bodies']}")
    print(f"schemas:                  {stats['schemas']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
