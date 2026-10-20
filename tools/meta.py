"""Server diagnostics: health_check (toolset "core").

Tools: health_check.

Ops owned by this module:

    GET /v1/clients

health_check makes ONE get_page(page_size=1) call on that op and reports what it found, together with
what this server knows about itself (the enabled toolsets, whether the gated tools are on, the names of the gated
tools that are registered, and the sha256 of the API spec it was built from; all of it comes from the lifespan context
through server_info_of). The gated tool names are tool names, never values: GORELO_ENABLE_DESTRUCTIVE may switch on only
some of the gated tools, and this is how the model and the operator see which.

It is the only tool that turns a Gorelo failure into a result ({"ok": false, "error": ..., "status":
...}) instead of raising: its job is diagnosis, so the reason must reach the model as data. A response
that parses but does not have the shape this server relies on (a paged list of client objects with an
Id and a TotalCount) is a failure too. The legacy check looked at the HTTP status only, so it reported
ok for a body it could not read.

It also reports api_host, the Gorelo host the shared client calls (read from the client's base URL, not from the
lifespan context).

When Gorelo answers an unauthorized status (UNAUTHORIZED), the error text also carries REGION_HINT, which names the
host that was called: GORELO_BASE_URL chooses that host, and it must be the host of the region the Gorelo account is
hosted in (US api.usw.gorelo.io, Australia api.aue.gorelo.io).
"""

from urllib.parse import urlsplit

from fastmcp import Context

from gorelo_client import GoreloAPIError, GoreloClient, Page
from tools._common import client_of, format_gorelo_error, gorelo_tool, server_info_of

CLIENTS_OP = "GET /v1/clients"
INFO_KEYS = ("toolsets", "destructive", "gated_tools", "spec_sha256")
UNAUTHORIZED = 401  # the HTTP status that gets the region hint
# A 401 names the host that was called and the two settings to check: the key and the host.
REGION_HINT = (
    "Hint: Gorelo answered {status} from {host}; check GORELO_API_KEY (the raw key, no Bearer prefix) and "
    "GORELO_BASE_URL: it must be the host of the region your Gorelo account is hosted in (US api.usw.gorelo.io, "
    "Australia api.aue.gorelo.io)."
)


def _shape_problem(page: Page) -> str | None:
    """Why a page the client could parse is still not the list of clients this server expects.

    None means the page looks right. The client has already refused a body that is not an envelope, a
    Data that is not a list and a broken Pagination block, so what is left to check is the content: a
    TotalCount to report, rows that are objects, and an Id on the first row (the canary that would
    have caught the legacy client reading a PascalCase body as zero rows).
    """
    if page.total_count is None:
        return f"{CLIENTS_OP} answered, but its pagination block has no TotalCount, so the number of clients is unknown"
    not_objects = sorted({type(item).__name__ for item in page.items if not isinstance(item, dict)})
    if not_objects:
        return f"{CLIENTS_OP} answered, but its rows are not client objects (found {', '.join(not_objects)})"
    if page.total_count > 0 and not page.items:
        return f"{CLIENTS_OP} reports TotalCount={page.total_count} but returned no rows"
    if page.items and "Id" not in page.items[0]:
        return f"{CLIENTS_OP} answered, but the first client row has no Id field, so the row shape is not the one this server expects"
    return None


def _api_host(client: GoreloClient) -> str | None:
    """The host the shared client calls, from its base URL; None when it cannot be read."""
    return urlsplit(client.base_url).hostname or None


@gorelo_tool(toolset="core", kind="read", ops=[CLIENTS_OP])
async def health_check(ctx: Context) -> dict:
    """Check the Gorelo API answers and this server understands it; returns {ok, api, api_host, total_clients, toolsets, destructive, gated_tools, spec_sha256}.
    Call it first when other tools fail. total_clients is the number of clients Gorelo lists by default (Archived clients are not counted). gated_tools lists the delete, void, approved-invoice and sharing tools switched on ([] means none).
    api_host is the Gorelo host this server calls (GORELO_BASE_URL). A Gorelo failure or unexpected response returns {ok: false, error, status} instead of a tool error.
    """
    client = client_of(ctx)
    host = _api_host(client)
    try:
        page = await client.get_page(CLIENTS_OP, page_size=1, tool="health_check")
    except GoreloAPIError as err:
        error = format_gorelo_error(err, "health_check")
        if err.status == UNAUTHORIZED:  # the body may be an envelope or gateway JSON: the status alone decides the hint
            error = f"{error} {REGION_HINT.format(status=UNAUTHORIZED, host=host or 'an address it could not read')}"
        return {"ok": False, "error": error, "status": err.status}
    problem = _shape_problem(page)
    if problem is not None:
        return {"ok": False, "error": problem, "status": 200}
    info = server_info_of(ctx)
    result = {
        "ok": True,
        "api": "reachable",
        "api_host": host,
        "total_clients": page.total_count,
        "toolsets": info["toolsets"],
        "destructive": info["destructive"],
        "gated_tools": info["gated_tools"],
        "spec_sha256": info["spec_sha256"],
    }
    unavailable = [key for key in INFO_KEYS if info[key] is None]
    if unavailable:
        result["note"] = (
            f"{', '.join(unavailable)} could not be read from the server's lifespan context, so they "
            "are null here; the Gorelo API itself answered correctly"
        )
    return result
