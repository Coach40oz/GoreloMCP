"""docs/tool-catalog.md is generated from the tool registry by scripts/gen_tool_catalog.py.

The drift test regenerates the catalog in memory and fails if the committed file differs. After a change to any
tool (or after merging branches that touch tools/), run

    uv run --frozen python scripts/gen_tool_catalog.py

and commit the result. The other tests pin what the generator promises: every registered tool appears exactly
once in the right toolset with its kind, ops and required parameters, destructive tools are marked, and the
output is deterministic (no environment, clock or ordering dependence) and free of em and en dashes.
"""

import difflib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Context
from fastmcp.tools import Tool

import tools  # noqa: F401  importing the package fills the registry
from settings import DEFAULT_TOOLSETS, TOOLSETS
from tools import _common
from tools._common import REGISTRY, Registry

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
CATALOG = REPO_ROOT / "docs" / "tool-catalog.md"
sys.path.insert(0, str(SCRIPTS))
import gen_tool_catalog as gen  # noqa: E402

EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)
MARK = "only when GORELO_ENABLE_DESTRUCTIVE=1"


def parse_catalog(text):
    """{toolset: {tool name: {"kind", "ops", "required", "summary"}}} and the summary table rows."""
    sections, summary, current = {}, {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            continue
        if current is None or not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if current == "Toolsets":
            if cells[0] not in ("Toolset", "---"):
                summary[cells[0]] = cells[1:]
            continue
        match = re.fullmatch(r"`([^`]+)`", cells[0])
        if match:
            sections.setdefault(current, {})[match.group(1)] = {
                "kind": cells[1],
                "ops": re.findall(r"`([^`]+)`", cells[2]),
                "required": [] if cells[3] == "none" else re.findall(r"`([^`]+)`", cells[3]),
                "summary": cells[4],
            }
    return sections, summary


def required_of(spec):
    return list((Tool.from_function(spec.fn).parameters or {}).get("required") or [])


# --------------------------------------------------------------------------
# No drift
# --------------------------------------------------------------------------


def test_the_committed_catalog_is_what_the_generator_produces():
    committed = CATALOG.read_text(encoding="utf-8")
    fresh = gen.build_catalog()
    if committed != fresh:
        diff = list(difflib.unified_diff(
            committed.splitlines(), fresh.splitlines(), "docs/tool-catalog.md (committed)", "generated", lineterm="", n=0
        ))
        shown = "\n".join(diff[:40]) + (f"\n... and {len(diff) - 40} more diff lines" if len(diff) > 40 else "")
        pytest.fail(
            "docs/tool-catalog.md is out of date. Regenerate it with\n"
            "    uv run --frozen python scripts/gen_tool_catalog.py\n"
            f"and commit the result.\n{shown}"
        )


def test_the_generator_is_deterministic_and_ignores_the_environment(monkeypatch):
    first = gen.build_catalog()
    assert gen.build_catalog() == first
    monkeypatch.setenv("GORELO_TOOLSETS", "forms")
    monkeypatch.setenv("GORELO_ENABLE_DESTRUCTIVE", "1")
    monkeypatch.setenv("GORELO_API_KEY", "x")
    assert gen.build_catalog() == first


def test_the_catalog_is_plain_ascii_dash_free_text_with_one_final_newline():
    text = CATALOG.read_bytes().decode("utf-8")
    assert EM_DASH not in text and EN_DASH not in text
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert "\r" not in text
    assert all(line == line.rstrip() for line in text.splitlines()), "trailing whitespace"
    assert text.startswith("# Gorelo MCP tool catalog\n")
    assert "Do not edit by hand" in text
    assert "uv run --frozen python scripts/gen_tool_catalog.py" in text


# --------------------------------------------------------------------------
# The catalog says what the registry says
# --------------------------------------------------------------------------


def test_every_registered_tool_is_listed_once_in_its_own_toolset():
    sections, _ = parse_catalog(CATALOG.read_text(encoding="utf-8"))
    assert list(sections) == [name for name in TOOLSETS if any(s.toolset == name for s in REGISTRY.specs)]
    listed = [(toolset, name) for toolset, rows in sections.items() for name in rows]
    assert len(listed) == len(set(listed)) == len(REGISTRY.specs)
    assert sorted(listed) == sorted((spec.toolset, spec.name) for spec in REGISTRY.specs)
    assert len({name for _, name in listed}) == len(listed), "a tool name appears in two toolsets"


def test_every_row_has_the_tools_kind_ops_and_required_params():
    sections, _ = parse_catalog(CATALOG.read_text(encoding="utf-8"))
    for spec in REGISTRY.specs:
        row = sections[spec.toolset][spec.name]
        assert row["kind"].split(" ")[0] == spec.kind, spec.name
        assert row["ops"] == list(spec.ops), spec.name
        assert row["required"] == required_of(spec), spec.name
        assert row["summary"], spec.name


def test_overwriting_writes_are_marked_and_plain_writes_are_not():
    sections, _ = parse_catalog(CATALOG.read_text(encoding="utf-8"))
    for spec in REGISTRY.specs:
        kind = sections[spec.toolset][spec.name]["kind"]
        if spec.kind == "write":
            assert kind == ("write (overwrites data)" if spec.destructive_hint else "write"), spec.name
        elif spec.kind == "read":
            assert kind == "read", spec.name


def test_destructive_tools_say_when_they_exist_and_no_other_tool_does():
    sections, _ = parse_catalog(CATALOG.read_text(encoding="utf-8"))
    destructive = {spec.name for spec in REGISTRY.specs if spec.kind == "destructive"}
    assert destructive, "the registry has delete tools"
    for rows in sections.values():
        for name, row in rows.items():
            if name in destructive:
                assert row["kind"] == f"destructive ({MARK})", name
            else:
                assert MARK not in row["kind"], name


def test_the_intro_says_what_destructive_means_for_every_gated_tool_that_is_not_a_delete():
    # GORELO_ENABLE_DESTRUCTIVE also registers create_approved_invoice, which approves an invoice (Gorelo pushes
    # it to the connected accounting system and may email its recipients) instead of deleting anything, so "destructive
    # tools delete or void records" was no longer true. The fresh generation is checked: it is what the file will say.
    bullet = next(line for line in gen.build_catalog().splitlines() if line.startswith("- Kind `read`"))
    gated = [spec.name for spec in REGISTRY.specs if spec.kind == "destructive"]
    assert "create_approved_invoice" in gated
    for name in (name for name in gated if not name.startswith("delete_")):
        assert f"`{name}`" in bullet, name
    for word in ("delete", "void", "accounting system", "email", "confirm=true", MARK):
        assert word in bullet, word
    assert "delete or void records" not in bullet  # the text from before create_approved_invoice existed


def test_the_intro_sentence_about_the_default_server_names_the_flag_and_what_it_gates():
    # "deletes off" read as if GORELO_ENABLE_DESTRUCTIVE were a switch for deletes only, but the flag
    # also registers create_approved_invoice (it approves an invoice and pushes it to accounting). The sentence names
    # the flag and the kinds of tool it gates. The fresh generation is checked: it is what the file will say.
    sentence = next(line for line in gen.build_catalog().splitlines() if line.startswith("The registry holds"))
    gated = {spec.name for spec in REGISTRY.specs if spec.kind == "destructive"}
    assert "deletes off" not in sentence
    assert "GORELO_ENABLE_DESTRUCTIVE off, so no gated tool" in sentence
    assert "no delete or void tool" in sentence
    for name in (name for name in sorted(gated) if not name.startswith("delete_")):
        assert f"`{name}`" in sentence, name  # a gated tool that is not a delete is named, or it would look allowed
    default_registered = len(REGISTRY.select(DEFAULT_TOOLSETS, False))
    assert sentence.endswith(f") registers {default_registered} of them.")
    # what it says is true: a server with the default settings registers none of the gated tools
    assert not {spec.name for spec in REGISTRY.select(DEFAULT_TOOLSETS, False)} & gated
    assert gen.GATED_OFF in sentence  # the wording is one constant of the generator


def test_the_summary_table_counts_what_the_registry_holds():
    text = CATALOG.read_text(encoding="utf-8")
    _, summary = parse_catalog(text)
    for name in TOOLSETS:
        specs = [s for s in REGISTRY.specs if s.toolset == name]
        expected = [
            "yes" if name in DEFAULT_TOOLSETS else "no",
            str(len(specs)),
            *(str(sum(1 for s in specs if s.kind == kind)) for kind in ("read", "write", "destructive")),
        ]
        assert summary[name] == expected, name
    total = [
        "",
        str(len(REGISTRY.specs)),
        *(str(sum(1 for s in REGISTRY.specs if s.kind == kind)) for kind in ("read", "write", "destructive")),
    ]
    assert summary["Total"] == total
    default_registered = len(REGISTRY.select(DEFAULT_TOOLSETS, False))
    assert f"The registry holds {len(REGISTRY.specs)} tools in {len(TOOLSETS)} toolsets." in text
    assert f"registers {default_registered} of them." in text


def test_the_first_sentence_is_what_the_model_is_shown():
    sections, _ = parse_catalog(CATALOG.read_text(encoding="utf-8"))
    for spec in REGISTRY.specs:
        described = Tool.from_function(spec.fn).description
        listed = sections[spec.toolset][spec.name]["summary"].replace("\\|", "|")
        assert listed == gen.first_sentence(described), spec.name
        assert " ".join(described.split()).startswith(listed.removesuffix("...")), spec.name


def test_the_optional_toolsets_say_which_api_key_scope_they_need():
    text = CATALOG.read_text(encoding="utf-8")
    assert "Needs the `Project` scope" in text and "Needs the `Forms` scope" in text
    assert text.count("In the default set.") == len(DEFAULT_TOOLSETS)


# --------------------------------------------------------------------------
# first_sentence and the Markdown helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Create a client. Second sentence.", "Create a client."),
        ("Create a client\nover two lines. More.", "Create a client over two lines."),
        ("List things; returns {items, count}.\nSide effects: none.", "List things; returns {items, count}."),
        ("Use e.g. a name. Next.", "Use e.g. a name."),
        ("Use i.e. the id. Next.", "Use i.e. the id."),
        ("Cheaper vs. dearer items. Next.", "Cheaper vs. dearer items."),
        ("Tags, statuses, etc. and more. Next.", "Tags, statuses, etc. and more."),
        ("Tags, statuses, etc. Next sentence.", "Tags, statuses, etc."),
        ("Version 1.5 of the thing. Next.", "Version 1.5 of the thing."),
        ("Is it done? Yes.", "Is it done?"),
        ("Stop now! Then go.", "Stop now!"),
        ("No terminator at all", "No terminator at all"),
        ("First paragraph without a period\n\nSecond paragraph. Third.", "First paragraph without a period"),
        ("  \n  Leading whitespace is ignored. Next.", "Leading whitespace is ignored."),
        ("", ""),
        (None, ""),
    ],
)
def test_first_sentence(text, expected):
    assert gen.first_sentence(text) == expected


def test_first_sentence_cuts_a_very_long_sentence_at_a_word_boundary():
    long_text = "word " * 200 + "end."
    cut = gen.first_sentence(long_text)
    assert len(cut) <= gen.MAX_SENTENCE
    assert cut.endswith("...") and not cut.endswith(" ...")
    assert cut[:-3].split(" ")[-1] == "word"  # no half word
    assert gen.first_sentence(long_text, limit=30) == "word word word word word..."


def test_a_pipe_in_a_description_cannot_end_a_table_cell():
    assert gen.cell("a | b") == "a \\| b"
    assert gen.code_list(("GET /v1/a", "GET /v1/b"), "none") == "`GET /v1/a`, `GET /v1/b`"
    assert gen.code_list((), "none") == "none"


def test_no_dashes_replaces_em_and_en_dashes():
    assert gen.no_dashes(f"a {EM_DASH} b {EN_DASH} c") == "a - b - c"


# --------------------------------------------------------------------------
# The generator on a registry of its own
# --------------------------------------------------------------------------


def make_registry():
    registry = Registry()

    @registry.tool(toolset="core", kind="read", ops=["GET /v1/clients"])
    async def beta(ctx: Context, name: str, limit: int = 5) -> dict:
        """Beta first sentence | with a pipe. Second sentence."""
        return {}

    @registry.tool(toolset="core", kind="write", ops=["POST /v1/clients"], destructive_hint=True)
    async def alpha(ctx: Context, a: str) -> dict:
        """Alpha first sentence."""
        return {}

    return registry


def test_rows_come_out_sorted_by_name_with_escaped_pipes(monkeypatch):
    monkeypatch.setattr(_common, "REGISTRY", make_registry())
    text = gen.render(gen.registry_rows(), ("core", "tickets"), ("core",))
    core = text.split("\n## core\n")[1].split("\n## tickets\n")[0]
    rows = [line for line in core.splitlines() if line.startswith("| `")]
    assert [row.split("`")[1] for row in rows] == ["alpha", "beta"]
    assert "| `alpha` | write (overwrites data) | `POST /v1/clients` | `a` | Alpha first sentence. |" in rows
    assert "| `beta` | read | `GET /v1/clients` | `name` | Beta first sentence \\| with a pipe. |" in rows
    assert text.split("\n## tickets\n")[1].strip().endswith("No tools registered.")
    assert "The registry holds 2 tools in 2 toolsets." in text


def test_a_tool_without_a_description_stops_the_generator(monkeypatch):
    registry = Registry()

    @registry.tool(toolset="core", kind="read", ops=["GET /v1/clients"])
    async def mute(ctx: Context) -> dict:
        return {}

    monkeypatch.setattr(_common, "REGISTRY", registry)
    with pytest.raises(gen.CatalogError, match="tool 'mute' has no description"):
        gen.registry_rows()


def test_a_toolset_that_settings_does_not_list_stops_the_generator(monkeypatch):
    monkeypatch.setattr(_common, "REGISTRY", make_registry())
    with pytest.raises(gen.CatalogError, match="core"):
        gen.render(gen.registry_rows(), ("tickets",), ("tickets",))


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------


def test_main_writes_the_catalog_where_it_is_told_and_check_agrees(tmp_path, capsys):
    out = tmp_path / "nested" / "catalog.md"
    assert gen.main(["--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == gen.build_catalog() == CATALOG.read_text(encoding="utf-8")
    assert gen.main(["--out", str(out), "--check"]) == 0
    assert "is up to date" in capsys.readouterr().out
    assert sorted(p.name for p in out.parent.iterdir()) == ["catalog.md"]  # no temp file left behind
    before = out.stat().st_mtime_ns
    assert gen.main(["--out", str(out)]) == 0
    assert out.read_bytes() == gen.build_catalog().encode("utf-8")
    assert out.stat().st_mtime_ns >= before


def test_check_fails_when_the_file_is_stale_or_missing_and_writes_nothing(tmp_path, capsys):
    stale = tmp_path / "stale.md"
    stale.write_text("old text\n", encoding="utf-8")
    assert gen.main(["--out", str(stale), "--check"]) == 1
    assert "is out of date; run uv run --frozen python scripts/gen_tool_catalog.py" in capsys.readouterr().err
    assert stale.read_text(encoding="utf-8") == "old text\n"
    missing = tmp_path / "missing.md"
    assert gen.main(["--out", str(missing), "--check"]) == 1
    assert "cannot read" in capsys.readouterr().err
    assert not missing.exists()


def test_the_script_runs_as_documented_from_any_directory_and_does_not_depend_on_the_hash_seed(tmp_path):
    outputs = []
    for seed in ("1", "2"):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GORELO_")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONHASHSEED"] = seed
        target = tmp_path / f"catalog-{seed}.md"
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "gen_tool_catalog.py"), "--out", str(target)],
            cwd=tmp_path, capture_output=True, text=True, timeout=120, env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip() == f"wrote {target}"
        outputs.append(target.read_bytes())
    assert outputs[0] == outputs[1] == CATALOG.read_bytes()
