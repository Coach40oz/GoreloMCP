"""version.py: VERSION is the `version` of the pyproject.toml beside it, and a pyproject without a usable one raises."""

import re
import tomllib
from pathlib import Path

import pytest

import version
from version import PYPROJECT, VERSION, read_version

REPO_ROOT = Path(__file__).resolve().parent.parent


def write_pyproject(tmp_path, text):
    path = tmp_path / "pyproject.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_version_is_the_version_of_the_pyproject_beside_the_module():
    assert Path(version.__file__).resolve().parent == REPO_ROOT and PYPROJECT == REPO_ROOT / "pyproject.toml"
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert VERSION == declared == read_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION)


def test_this_release_is_1_7_0():
    assert VERSION == "1.7.0"


def test_the_version_is_read_from_the_given_file(tmp_path):
    assert read_version(write_pyproject(tmp_path, '[project]\nname = "x"\nversion = " 9.8.7 "\n')) == "9.8.7"


@pytest.mark.parametrize(
    "text",
    [
        '[project]\nname = "x"\n',  # no version
        'name = "x"\nversion = "1.0.0"\n',  # a version outside [project]
        '[project]\nversion = ""\n',
        '[project]\nversion = "   "\n',
        "[project]\nversion = 1.7\n",  # a number
        '[project]\nversion = ["1.7.0"]\n',
        'project = "1.7.0"\n',
    ],
)
def test_a_pyproject_without_a_usable_version_raises(tmp_path, text):
    path = write_pyproject(tmp_path, text)
    with pytest.raises(RuntimeError, match="cannot read the server version from .*pyproject.toml"):
        read_version(path)


def test_a_missing_or_broken_pyproject_raises(tmp_path):
    with pytest.raises(RuntimeError, match="cannot read the server version"):
        read_version(tmp_path / "pyproject.toml")
    with pytest.raises(RuntimeError, match="cannot read the server version"):
        read_version(write_pyproject(tmp_path, "[project\nversion ="))


def test_uv_lock_records_the_same_project_version():
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    own = [package for package in lock["package"] if package["name"] == "gorelo-mcp"]
    assert len(own) == 1 and own[0]["version"] == VERSION
