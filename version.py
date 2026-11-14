"""The version of this server, read once from the `version` of the pyproject.toml beside this file.

pyproject.toml is the one place the version is written; everything else (the FastMCP server, the ready line,
health_check) reads VERSION from here. A pyproject.toml that is missing, is not valid TOML, or has no usable
`[project] version` raises at import: a server that cannot say which version it is must not start.
"""

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent / "pyproject.toml"


def read_version(path: Path = PYPROJECT) -> str:
    """The `[project] version` of the pyproject.toml at `path`. Raises RuntimeError that names the file and the problem."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise RuntimeError(f"cannot read the server version from {path}: {err}") from err
    project = data.get("project")
    value = project.get("version") if isinstance(project, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"cannot read the server version from {path}: [project] has no version text")
    return value.strip()


VERSION = read_version()
