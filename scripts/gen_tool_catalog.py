#!/usr/bin/env python3
"""Generate docs/tool-catalog.md from the tool registry.

The catalog lists, per toolset, every tool with its kind, the Gorelo operations it may call, its
required parameters and the first sentence of its description. It is a pure function of the registry
(tools/*.py): no network, no environment variables, no clock, no file read. The output is the same on
every machine and on every run, and tests/test_tool_catalog.py fails when the committed file differs
from a fresh generation.

    uv run --frozen python scripts/gen_tool_catalog.py            # rewrite docs/tool-catalog.md
    uv run --frozen python scripts/gen_tool_catalog.py --check    # change nothing; exit 1 if the file is stale
    uv run --frozen python scripts/gen_tool_catalog.py --out /tmp/tool-catalog.md

Run it after adding, renaming or re-describing a tool, and again after merging branches that touch tools/.

Ordering is fixed so that a diff only shows real changes: toolsets in the order of settings.TOOLSETS,
tools by name inside a toolset, required parameters in signature order, ops in the order the tool
declares them.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "tool-catalog.md"
# `python scripts/gen_tool_catalog.py` puts scripts/ on sys.path, not the repo root: tools and settings live there.
sys.path.insert(0, str(REPO_ROOT))

DESTRUCTIVE_MARK = "only when GORELO_ENABLE_DESTRUCTIVE=1"
# What the default settings leave out, in the intro sentence. The flag registers more than the delete and void tools
# (it also registers create_approved_invoice, which approves an invoice), so the sentence names the flag and the tools
# it gates and never calls it a switch for deletes only.
GATED_OFF = (
    "GORELO_ENABLE_DESTRUCTIVE off, so no gated tool: no delete or void tool and no `create_approved_invoice`"
)
MAX_SENTENCE = 320  # characters; a longer first sentence is cut at a word boundary and ends with "..."
GENERATE_COMMAND = "uv run --frozen python scripts/gen_tool_catalog.py"

# What the operator must know about a toolset beyond its tools. Only documented facts about the API (see docs/API-OBSERVED-BEHAVIOR.md).
TOOLSET_NOTES = {
    "projects": "Needs the `Project` scope on the Gorelo API key (a 403 with code 080203 means it is missing).",
    "forms": "Needs the `Forms` scope on the Gorelo API key (a 403 with code 080203 means it is missing).",
}

_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_NEVER_ENDS_A_SENTENCE = frozenset({"e.g", "i.e", "vs", "cf", "approx", "incl"})


class CatalogError(RuntimeError):
    """The registry cannot be rendered (for example a tool without a description)."""


@dataclass(frozen=True)
class Row:
    name: str
    toolset: str
    kind: str
    overwrites: bool  # a write tool that replaces or clears existing data (MCP destructiveHint)
    ops: tuple[str, ...]
    required: tuple[str, ...]
    summary: str


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------


def first_sentence(text: str | None, limit: int = MAX_SENTENCE) -> str:
    """The first sentence of a description, on one line.

    Only the first paragraph is read (a docstring's lines are joined). A sentence ends at ".", "!" or "?"
    followed by whitespace or the end, except after e.g, i.e, vs, cf, approx and incl, and after "etc"
    when the next word starts in lower case. With no sentence end the whole first paragraph is used. A
    sentence longer than `limit` characters is cut at a word boundary and ends with "...".
    """
    paragraph = (text or "").strip().split("\n\n")[0]
    flat = " ".join(paragraph.split())
    sentence = flat
    for match in _SENTENCE_END.finditer(flat):
        if match.group() == "." and _abbreviation_before(flat, match.start()):
            continue
        sentence = flat[: match.end()]
        break
    if len(sentence) > limit:
        sentence = sentence[: limit - 3].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return sentence


def _abbreviation_before(flat: str, dot: int) -> bool:
    token = flat[:dot].rsplit(" ", 1)[-1].lower().lstrip("(\"'")
    if token in _NEVER_ENDS_A_SENTENCE:
        return True
    if token == "etc":
        following = flat[dot + 1 :].lstrip()[:1]
        return following.islower()
    return False


def cell(text: str) -> str:
    """Text for one Markdown table cell: a pipe would end the cell."""
    return text.replace("|", "\\|")


def no_dashes(text: str) -> str:
    """The house rule: no em dash and no en dash in anything generated. chr() keeps this file clean too."""
    return text.replace(chr(0x2014), "-").replace(chr(0x2013), "-")


def code_list(values: tuple[str, ...], empty: str) -> str:
    return ", ".join(f"`{value}`" for value in values) if values else empty


# --------------------------------------------------------------------------
# Registry to rows
# --------------------------------------------------------------------------


def registry_rows() -> list[Row]:
    """One Row per registered tool, in no particular order (render() sorts)."""
    import tools  # noqa: F401  importing the package imports every module, whose decorators fill the registry
    from fastmcp.tools import Tool

    from tools._common import REGISTRY

    rows: list[Row] = []
    for spec in REGISTRY.specs:
        described = Tool.from_function(spec.fn)  # what the model is shown: the docstring and the JSON schema
        summary = first_sentence(described.description)
        if not summary:
            raise CatalogError(f"tool {spec.name!r} has no description (its docstring is empty)")
        required = tuple((described.parameters or {}).get("required") or ())
        rows.append(
            Row(
                name=spec.name,
                toolset=spec.toolset,
                kind=spec.kind,
                overwrites=spec.kind == "write" and bool(spec.destructive_hint),
                ops=tuple(spec.ops),
                required=required,
                summary=summary,
            )
        )
    return rows


def kind_cell(row: Row) -> str:
    if row.kind == "destructive":
        return f"destructive ({DESTRUCTIVE_MARK})"
    if row.overwrites:
        return "write (overwrites data)"
    return row.kind


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render(rows: list[Row], toolsets: tuple[str, ...], default_toolsets: tuple[str, ...]) -> str:
    unknown = sorted({row.toolset for row in rows} - set(toolsets))
    if unknown:
        raise CatalogError(f"tools in toolsets that settings.TOOLSETS does not list: {', '.join(unknown)}")
    by_toolset = {name: sorted((r for r in rows if r.toolset == name), key=lambda r: r.name) for name in toolsets}
    default_registered = sum(1 for r in rows if r.toolset in default_toolsets and r.kind != "destructive")

    def count(selected: list[Row], kind: str) -> int:
        return sum(1 for r in selected if r.kind == kind)

    lines = [
        "# Gorelo MCP tool catalog",
        "",
        "<!-- Generated by scripts/gen_tool_catalog.py from the tool registry. Do not edit by hand. -->",
        "",
        "This file is generated. After any change to a tool (its name, toolset, kind, ops, parameters or the first",
        "sentence of its docstring), and after merging branches that touch `tools/`, regenerate it:",
        "",
        "```bash",
        GENERATE_COMMAND,
        "```",
        "",
        "`tests/test_tool_catalog.py` fails when the committed file differs from a fresh generation.",
        "",
        f"The registry holds {len(rows)} tools in {len(toolsets)} toolsets. A server started with the default settings "
        f"(toolsets {', '.join(default_toolsets)}; {GATED_OFF}) registers {default_registered} of them.",
        "",
        "How to read it:",
        "",
        "- Toolsets are chosen with `GORELO_TOOLSETS` (a comma list, or `all`). Default: "
        f"{', '.join(default_toolsets)}.",
        "- Kind `read` changes nothing in Gorelo. `write` creates or changes data. `write (overwrites data)` also replaces "
        "or clears data that already exists (the MCP destructiveHint is set). `destructive` tools are the delete and void "
        "tools and `create_approved_invoice` (it creates an invoice that is Approved at once, so Gorelo pushes it to the "
        f"connected accounting system and may email its recipients): they are registered {DESTRUCTIVE_MARK}, and every "
        "call also needs `confirm=true`.",
        "- Ops are the Gorelo operations (`METHOD /v1/path`) the tool may call. Any other operation is refused before "
        "any HTTP request.",
        "- Required params are the parameters a call must supply. Every other parameter is optional.",
        "- What it does is the first sentence of the tool description; the full text is what claude.ai reads.",
        "",
        "## Toolsets",
        "",
        "| Toolset | Default | Tools | read | write | destructive |",
        "|---|---|---|---|---|---|",
    ]
    for name in toolsets:
        selected = by_toolset[name]
        lines.append(
            f"| {name} | {'yes' if name in default_toolsets else 'no'} | {len(selected)} | "
            f"{count(selected, 'read')} | {count(selected, 'write')} | {count(selected, 'destructive')} |"
        )
    lines.append(
        f"| Total | | {len(rows)} | {count(rows, 'read')} | {count(rows, 'write')} | {count(rows, 'destructive')} |"
    )

    for name in toolsets:
        selected = by_toolset[name]
        lines += ["", f"## {name}", ""]
        scope = "In the default set." if name in default_toolsets else "Not in the default set: add it to `GORELO_TOOLSETS`."
        note = TOOLSET_NOTES.get(name)
        lines.append(" ".join(part for part in (scope, note) if part))
        lines.append("")
        if not selected:
            lines.append("No tools registered.")
            continue
        lines += [
            "| Tool | Kind | Ops | Required params | What it does |",
            "|---|---|---|---|---|",
        ]
        for row in selected:
            lines.append(
                f"| `{row.name}` | {kind_cell(row)} | {cell(code_list(row.ops, 'none'))} | "
                f"{cell(code_list(row.required, 'none'))} | {cell(row.summary)} |"
            )
    return no_dashes("\n".join(lines).rstrip("\n") + "\n")


def build_catalog() -> str:
    """The whole catalog as text. Pure: the same registry always gives the same text."""
    from settings import DEFAULT_TOOLSETS, TOOLSETS

    return render(registry_rows(), TOOLSETS, DEFAULT_TOOLSETS)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate docs/tool-catalog.md from the tool registry.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 if the catalog file is missing or differs from a fresh generation",
    )
    parser.add_argument("--out", default=None, help="catalog path (default: docs/tool-catalog.md under the repo root)")
    args = parser.parse_args(argv)
    out = Path(args.out) if args.out else DEFAULT_OUT

    try:
        text = build_catalog()
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.check:
        try:
            current = out.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read {out}: {exc}; run {GENERATE_COMMAND}", file=sys.stderr)
            return 1
        if current != text:
            print(f"error: {out} is out of date; run {GENERATE_COMMAND}", file=sys.stderr)
            return 1
        print(f"{out} is up to date")
        return 0

    write_atomic(out, text)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
