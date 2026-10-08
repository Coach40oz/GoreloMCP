# Contributing

Thanks for helping. This server talks to a live PSA that holds real client data, so changes are
checked carefully. Please read `docs/OVERVIEW.md` (how it works) and `docs/why/README.txt` (why
it is built this way) first.

## Run the offline tests

The tests use no network and need no Gorelo account. From the repository root:

    UV_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 \
      uv run --frozen -q --with pytest --with anyio python -m pytest -q -p no:cacheprovider

(`uv` must be on your PATH; if it is not, use its full path, for example `/root/.local/bin/uv`. `UV_OFFLINE=1` needs the dependencies to be in uv's cache already; run once without it to fetch
them. Set `UV_PYTHON` to a Python 3.13 if uv picks the wrong one.) Please run the full suite
before you open a pull request.

## Rules

- No em dashes (U+2014) or en dashes (U+2013) in code, comments, docstrings, the guides under
  `docs/` or the notes under `docs/why/`. `tests/test_hygiene.py` scans them and fails on one.
- Verify behavior against the live Gorelo spec (`scripts/spec_snapshot.py`, then
  `spec/spec_index.json`), not against changelog prose, which drifts. If you learn that
  production differs from the spec, add it to `docs/API-OBSERVED-BEHAVIOR.md`.
- Fail loudly: no silent empty results, no invented defaults, and error messages that name the
  snake_case tool parameter.
- Never commit `.env`, `site.local.toml`, `.oauth-state/`, a tunnel token, an API key, or real
  tenant data or ids. Use `site.example.toml` as the template for your own `site.local.toml`.
- Do not edit `pyproject.toml` or `uv.lock` in a feature change, and do not use `uv add` or `uv lock`,
  and do not run `uv sync` without `--frozen`; dependency changes are separate and deliberate.
- Add tests with every change: method, path, query names, body, result shape, a Gorelo error
  mapped to the parameter, local validation (with zero HTTP calls), and for a gated tool the
  refusal without `confirm`.
- Keep the docs true. Tests pin counts and file lists in `docs/OVERVIEW.md` and
  `docs/tool-catalog.md`; regenerate the catalog with `uv run --frozen python scripts/gen_tool_catalog.py`.
- Live tests write to a dedicated test client that you configure in site.local.toml. Do not run
  `scripts/live/` against a tenant where test records would bother anyone.

## Propose a new tool

Open an issue first, naming the Gorelo operation and why it is useful. A tool is a decorated async
function in `tools/<domain>.py`:

    @gorelo_tool(toolset="core", kind="read", ops=["GET /v1/clients"])
    async def list_clients(
        ctx: Context,
        query: Annotated[str | None, Field(description="Keyword matched against client names.")] = None,
    ) -> dict:
        """One sentence: what it does, which ids to resolve first, side effects, paging rule."""

- `toolset` is one of core, tickets, time, billing, uptime, projects, forms.
- `kind` is `read`, `write` or `destructive`. Destructive tools are registered only with
  `GORELO_ENABLE_DESTRUCTIVE` and need a `confirm` parameter. A tool that pushes data outside
  Gorelo (for example to accounting) should be its own gated tool.
- `ops` lists every operation the tool may call, including the GET it uses to read a record
  back. Each must exist in `spec/spec_index.json`. Forbidden operations (see
  `docs/why/03-forbidden-and-gated-operations.txt`) can never be declared.
- Use the helpers in `tools/_common.py` for ids, datetimes, bodies, results and errors. The
  docstring is the prompt the AI model sees, so state who is emailed and what starts running.

## Add a tool: checklist

1. Verify the operation in `spec/spec_index.json`, refreshing the spec first with
   `scripts/spec_snapshot.py` if Gorelo changed it. Never code from changelog prose, and record
   anything production does differently in `docs/API-OBSERVED-BEHAVIOR.md`.
2. Write the tool in `tools/<domain>.py` with `@gorelo_tool(toolset=..., kind=..., ops=[...])`,
   typed snake_case parameters with `Field(description=...)`, the docstring pattern above and one of
   the existing result shapes. A destructive tool takes `confirm` and refuses without it. Declare
   every operation it calls, including the GET it uses to read a record back.
3. Write its offline tests in `tests/test_tools_<domain>.py`: method, path, query names, PascalCase
   body, result shape, a Gorelo error mapped to the parameter, local validation with zero HTTP
   calls, and for a destructive tool the refusal without `confirm`.
4. Regenerate the catalog with `uv run --frozen python scripts/gen_tool_catalog.py`. A test fails
   while `docs/tool-catalog.md` is stale. Update the counts in `docs/OVERVIEW.md` (the test that
   pins them tells you which).
5. Run the full offline suite and keep it green before you open a pull request.
