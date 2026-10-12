"""scripts/spec_diff.py, the "Description changes" section, on small synthetic raw specs. Offline.

The shape comparison ignores descriptions and summaries, so a behavior change stated only in text (for example "Ignored when
CreatedOn or ClosedOn is supplied") was missed by the 2026-10-12 refresh. The report now lists every changed text of an existing
operation, parameter, response, body or schema property when both inputs are raw swagger files.
"""

import copy
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import spec_diff  # noqa: E402

BASE = {
    "openapi": "3.0.1",
    "info": {"title": "t", "version": "1"},
    "paths": {
        "/v1/things": {
            "get": {
                "summary": "Lists things.",
                "description": "All things.",
                "parameters": [{"name": "q", "in": "query", "description": "search", "schema": {"type": "string"}}],
                "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}}}},
            }
        }
    },
    "components": {
        "schemas": {
            "Thing": {
                "type": "object",
                "properties": {
                    "Mail": {"type": "boolean", "description": "Send the email."},
                    "Name": {"type": "string"},
                },
            }
        }
    },
}


def write(tmp_path, name, spec):
    path = tmp_path / name
    path.write_text(json.dumps(spec))
    return str(path)


def run(tmp_path, old, new, capsys):
    out = tmp_path / "report.md"
    code = spec_diff.main([write(tmp_path, "old.json", old), write(tmp_path, "new.json", new), "--out", str(out)])
    capsys.readouterr()
    return code, out.read_text()


def test_a_description_only_change_exits_0_and_is_listed_with_old_and_new_text(tmp_path, capsys):
    new = copy.deepcopy(BASE)
    new["components"]["schemas"]["Thing"]["properties"]["Mail"]["description"] = "Send the email. Ignored when CreatedOn is supplied."
    new["paths"]["/v1/things"]["get"]["parameters"][0]["description"] = "search text"
    new["paths"]["/v1/things"]["get"]["summary"] = "Lists all things."
    code, report = run(tmp_path, BASE, new, capsys)
    assert code == 0
    assert "## Description changes (3)" in report
    assert "### `/components/schemas/Thing/properties/Mail/description`" in report
    assert "### `/paths//v1/things/get/parameters[0]/description`" in report
    assert "### `/paths//v1/things/get/summary`" in report
    assert "Send the email.\n```" in report and "Send the email. Ignored when CreatedOn is supplied.\n```" in report
    assert report.index("OLD:") < report.index("NEW:")


def test_the_section_says_none_when_no_text_changed(tmp_path, capsys):
    code, report = run(tmp_path, BASE, BASE, capsys)
    assert code == 0 and "## Description changes (0)" in report and report.rstrip().endswith("None.")


def test_texts_of_added_operations_and_schemas_are_skipped(tmp_path, capsys):
    new = copy.deepcopy(BASE)
    new["paths"]["/v1/other"] = {"post": {"summary": "Creates.", "description": "d", "responses": {"200": {"description": "OK"}}}}
    new["components"]["schemas"]["Other"] = {"type": "object", "properties": {"A": {"type": "string", "description": "a"}}}
    new["components"]["schemas"]["Thing"]["properties"]["Added"] = {"type": "string", "description": "a new property"}
    code, report = run(tmp_path, BASE, new, capsys)
    assert code == 1  # the shape differs (added operation, schema, property)
    assert "## Description changes (0)" in report
    assert "/v1/other" not in report.split("## Description changes")[1]
    assert "Added/description" not in report


def test_a_text_added_or_dropped_on_an_existing_node_is_listed(tmp_path, capsys):
    new = copy.deepcopy(BASE)
    new["components"]["schemas"]["Thing"]["properties"]["Name"]["description"] = "The name."
    del new["paths"]["/v1/things"]["get"]["description"]
    code, report = run(tmp_path, BASE, new, capsys)
    assert code == 0 and "## Description changes (2)" in report
    assert "### `/components/schemas/Thing/properties/Name/description`" in report
    assert "### `/paths//v1/things/get/description`" in report


def test_the_section_follows_the_shape_sections_when_the_shape_differs_too(tmp_path, capsys):
    new = copy.deepcopy(BASE)
    new["components"]["schemas"]["Thing"]["properties"]["Mail"]["description"] = "changed"
    new["components"]["schemas"]["Thing"]["properties"]["Name"]["type"] = "integer"
    code, report = run(tmp_path, BASE, new, capsys)
    assert code == 1
    assert report.index("## operation_id changes") < report.index("## Description changes (1)")


def test_an_index_input_says_descriptions_were_not_compared(tmp_path, capsys):
    index = spec_diff.build_index(BASE, json.dumps(BASE).encode(), "x")
    index_path = write(tmp_path, "index.json", index)
    out = tmp_path / "report.md"
    code = spec_diff.main([index_path, write(tmp_path, "new.json", BASE), "--out", str(out)])
    capsys.readouterr()
    text = out.read_text()
    assert code == 0
    assert "## Description changes\n" in text and "Not compared" in text and "spec index" in text


def test_render_markdown_without_descriptions_is_unchanged(tmp_path):
    old = spec_diff.build_index(BASE, json.dumps(BASE).encode(), "x")
    result = spec_diff.diff_indexes(old, old)
    assert "Description changes" not in spec_diff.render_markdown(result, old, old, "a", "b")
    assert "## Description changes (0)" in spec_diff.render_markdown(result, old, old, "a", "b", spec_diff.diff_descriptions(BASE, BASE))


def test_a_text_with_backticks_gets_a_longer_fence(tmp_path, capsys):
    new = copy.deepcopy(BASE)
    new["paths"]["/v1/things"]["get"]["description"] = "use ```code``` blocks"
    code, report = run(tmp_path, BASE, new, capsys)
    assert "````text\nuse ```code``` blocks\n````" in report


def test_the_watcher_still_renders_its_report_without_the_section():
    old = spec_diff.build_index(BASE, json.dumps(BASE).encode(), "x")
    result = spec_diff.diff_indexes(old, old)
    assert spec_diff.render_markdown(result, old, old, "spec/spec_index.json", "saved.json").startswith("# Gorelo API spec diff")
