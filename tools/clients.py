"""Clients and their locations: list, read, create, update (toolset "core").

Tools: list_clients, get_client, create_client, update_client, list_client_locations.

Ops owned by this module:

    GET /v1/clients
    GET /v1/clients/{clientId}
    GET /v1/clients/{clientId}/locations
    POST /v1/clients
    PATCH /v1/clients/{clientId}

Gorelo's client write endpoints answer with the full client record (ClientResponse), so create_client
and update_client return it as it is and make no re-read. A write whose answer is not that record is
refused through created_id / expect_object (kind "shape", write_unconfirmed): the write may have been
applied, so the model must verify it with a read instead of repeating it. PATCH /v1/clients/{clientId} is a
true partial update that treats null as "leave unchanged"; probe 2026-10-01 showed that "" and null both leave
a value unchanged, so a client field cannot be cleared through the API and update_client has no clear option
(it rejects "" and says why).

The client id travels in the PATH only. The published UpdateClientCommand (contract e15cb5a18ec2, 2026-10-02) has
no Id field, so update_client sends none. The update used to be published with the id in the body and no id in
the path; that form answered 405 live on 2026-10-02 and a live override swapped the path form in until Gorelo
published it (the history is in spec/live_overrides.json, section retired). A missing client is a 404.

The list rows are ClientListItemResponse records (Id, Name, AlternateName, BillingName, Status, Domains,
IsDefault, CreatedOn, UpdatedOn): the same fields as the single record that get_client returns.

There is no delete tool: DELETE /v1/clients/{clientId} is in FORBIDDEN_OPS.
"""

from typing import Annotated

from fastmcp import Context
from pydantic import Field

from tools._common import (
    StrictId,
    build_body,
    clamp_page_size,
    client_of,
    created_id,
    csv_ids,
    expect_object,
    gorelo_tool,
    list_result,
    non_empty,
    paged_result,
    positive_id,
    positive_ids,
    region_code,
    utc_iso,
)

LIST_OP = "GET /v1/clients"
GET_OP = "GET /v1/clients/{clientId}"
LOCATIONS_OP = "GET /v1/clients/{clientId}/locations"
CREATE_OP = "POST /v1/clients"
UPDATE_OP = "PATCH /v1/clients/{clientId}"

SINCE_HELP = "ISO 8601 with UTC offset, e.g. 2026-10-01T00:00:00Z."

# snake_case tool parameter -> Gorelo query name. Only used to name the parameter in Gorelo's errors
# (a 400 for PageSize or StatusIds comes back with that PropertyName).
LIST_FIELD_MAP = {
    "query": "Query",
    "status_ids": "StatusIds",
    "created_since": "CreatedSince",
    "created_before": "CreatedBefore",
    "updated_since": "UpdatedSince",
    "updated_before": "UpdatedBefore",
    "page_size": "PageSize",
    "cursor": "Cursor",
}

# snake_case tool parameter -> CreateClientCommand field; the location fields nest under Location
# (ClientLocationRequest).
CREATE_FIELD_MAP = {
    "name": "Name",
    "billing_name": "BillingName",
    "alternate_name": "AlternateName",
    "domain": "Domain",
    "location_name": "Location.Name",
    "location_phone_country_code": "Location.PhoneCountryCode",
    "location_phone": "Location.Phone",
    "location_phone_ext": "Location.PhoneExt",
    "location_address1": "Location.Address1",
    "location_address2": "Location.Address2",
    "location_city": "Location.City",
    "location_state": "Location.State",
    "location_country": "Location.Country",
    "location_postal_code": "Location.PostalCode",
    "location_time_zone": "Location.TimeZone",
}

# snake_case tool parameter -> UpdateClientCommand field. Body fields only: the client id travels in the path and
# the command has no Id.
UPDATE_FIELD_MAP = {
    "name": "Name",
    "status_id": "StatusId",
    "billing_name": "BillingName",
    "alternate_name": "AlternateName",
}

UPDATE_TEXT_PARAMS = ("name", "billing_name", "alternate_name")

# What update_client declares as its field_map: the body fields plus the path placeholder, so that a Gorelo error
# about the id names client_id (build_body is never given client_id).
UPDATE_TOOL_FIELD_MAP = {**UPDATE_FIELD_MAP, "client_id": "clientId"}

# The path placeholders, so that a Gorelo error about the id names client_id (the parameter the model
# has to fix) and not "clientId".
GET_FIELD_MAP = {"client_id": "clientId"}
LOCATIONS_FIELD_MAP = {"client_id": "clientId"}


@gorelo_tool(toolset="core", kind="read", ops=[LIST_OP], field_map=LIST_FIELD_MAP)
async def list_clients(
    ctx: Context,
    query: Annotated[str | None, Field(description="Matches name, alternate name, billing name and domains.")] = None,
    status_ids: Annotated[
        list[StrictId] | None,
        Field(
            description="Status.Id values from client records (no lookup tool). Omit it and inactive clients (status 2) "
            "are left out; give it and only those statuses are returned."
        ),
    ] = None,
    created_since: Annotated[str | None, Field(description=f"Created at or after; {SINCE_HELP}")] = None,
    created_before: Annotated[str | None, Field(description="Created before.")] = None,
    updated_since: Annotated[str | None, Field(description="Updated at or after.")] = None,
    updated_before: Annotated[str | None, Field(description="Updated before.")] = None,
    page_size: Annotated[int, Field(description="Rows per page, 1 to 200.")] = 200,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous call.")] = None,
) -> dict:
    """List clients one page per call; returns {items, has_more, next_cursor, total_count, ...}.
    Inactive clients are left out unless status_ids asks for the inactive status. Paging: pass next_cursor as cursor, same filters, until has_more is false.
    """
    keyword = non_empty("query", query)
    ids = csv_ids("status_ids", positive_ids("status_ids", status_ids))
    created_after = utc_iso("created_since", created_since)
    created_until = utc_iso("created_before", created_before)
    updated_after = utc_iso("updated_since", updated_since)
    updated_until = utc_iso("updated_before", updated_before)
    token = non_empty("cursor", cursor)
    size = clamp_page_size(page_size)
    page = await client_of(ctx).get_page(
        LIST_OP,
        query={
            "Query": keyword,
            "StatusIds": ids,
            "CreatedSince": created_after,
            "CreatedBefore": created_until,
            "UpdatedSince": updated_after,
            "UpdatedBefore": updated_until,
        },
        page_size=size,
        cursor=token,
        tool="list_clients",
    )
    return paged_result(
        page,
        {
            "query": keyword,
            "status_ids": status_ids,
            "created_since": created_after,
            "created_before": created_until,
            "updated_since": updated_after,
            "updated_before": updated_until,
        },
    )


@gorelo_tool(toolset="core", kind="read", ops=[GET_OP], field_map=GET_FIELD_MAP)
async def get_client(
    ctx: Context,
    client_id: Annotated[StrictId, Field(description="Client id (list_clients).")],
) -> dict:
    """Get one client by id; returns Gorelo's client record."""
    cid = positive_id("client_id", client_id)
    record = await client_of(ctx).get_one(GET_OP, path_params={"clientId": cid}, tool="get_client")
    return expect_object(record, GET_OP, tool="get_client")


@gorelo_tool(toolset="core", kind="write", ops=[CREATE_OP], field_map=CREATE_FIELD_MAP)
async def create_client(
    ctx: Context,
    name: Annotated[str, Field(description="Client display name.")],
    location_name: Annotated[str, Field(description="Name of the default location, e.g. Head Office.")],
    billing_name: Annotated[str | None, Field(description="Name printed on invoices.")] = None,
    alternate_name: Annotated[str | None, Field(description="Alternate or trading name.")] = None,
    domain: Annotated[str | None, Field(description="Primary web domain.")] = None,
    location_phone_country_code: Annotated[
        str | None,
        Field(
            description="ISO region code in capitals (US, CA), not a dial code. Required with location_phone."
        ),
    ] = None,
    location_phone: Annotated[str | None, Field(description="National number, no country code.")] = None,
    location_phone_ext: Annotated[str | None, Field(description="Phone extension.")] = None,
    location_address1: Annotated[str | None, Field(description="Address line 1.")] = None,
    location_address2: Annotated[str | None, Field(description="Address line 2.")] = None,
    location_city: Annotated[str | None, Field(description="City.")] = None,
    location_state: Annotated[str | None, Field(description="State or province.")] = None,
    location_country: Annotated[str | None, Field(description="Country.")] = None,
    location_postal_code: Annotated[str | None, Field(description="Postal or ZIP code.")] = None,
    location_time_zone: Annotated[str | None, Field(description="IANA time zone, e.g. America/New_York.")] = None,
) -> dict:
    """Create a client together with its default location; returns the created client record.
    Only name and location_name are required; nothing is guessed. Check list_clients (query) first for a duplicate. Side effects: creates a real client at once; this server cannot delete clients, so a mistaken one is removed in the Gorelo app.
    """
    non_empty("name", name)
    non_empty("location_name", location_name)
    region = region_code("location_phone_country_code", location_phone_country_code)
    if location_phone is not None and region is None:
        raise ValueError(
            "location_phone_country_code: required when location_phone is given. Gorelo needs the ISO "
            "region code of the number (for example US or CA) and this tool assumes none"
        )
    body = build_body(
        {
            "name": name,
            "billing_name": billing_name,
            "alternate_name": alternate_name,
            "domain": domain,
            "location_name": location_name,
            "location_phone_country_code": region,
            "location_phone": location_phone,
            "location_phone_ext": location_phone_ext,
            "location_address1": location_address1,
            "location_address2": location_address2,
            "location_city": location_city,
            "location_state": location_state,
            "location_country": location_country,
            "location_postal_code": location_postal_code,
            "location_time_zone": location_time_zone,
        },
        CREATE_FIELD_MAP,
    )
    data = await client_of(ctx).post(CREATE_OP, json_body=body, tool="create_client")
    created_id(data, CREATE_OP, tool="create_client")  # raises (write_unconfirmed) unless Data carries an Id
    return data


@gorelo_tool(toolset="core", kind="write", ops=[UPDATE_OP], field_map=UPDATE_TOOL_FIELD_MAP, destructive_hint=True)
async def update_client(
    ctx: Context,
    client_id: Annotated[StrictId, Field(description="Client id (list_clients).")],
    name: Annotated[str | None, Field(description="New display name.")] = None,
    status_id: Annotated[
        StrictId | None,
        Field(
            description="Status.Id from a client record (no lookup tool). An inactive status hides the client "
            "from default lists like a delete does; its devices drop out of list_agents."
        ),
    ] = None,
    billing_name: Annotated[str | None, Field(description="New name printed on invoices.")] = None,
    alternate_name: Annotated[str | None, Field(description="New alternate or trading name.")] = None,
) -> dict:
    """Update a client's name, status, billing name or alternate name; returns the updated client record.
    Only the fields you pass change (at least one); Gorelo cannot clear a client field through the API. Side effects: overwrites current values (visible in the app, on tickets and invoices; old values are not kept); an inactive status_id also hides the client like a delete does.
    """
    cid = positive_id("client_id", client_id)
    sid = None if status_id is None else positive_id("status_id", status_id)
    texts = {"name": name, "billing_name": billing_name, "alternate_name": alternate_name}
    for param in UPDATE_TEXT_PARAMS:
        value = texts[param]
        if value is not None and not value.strip():
            raise ValueError(
                f"{param}: must not be empty. Gorelo cannot clear client fields through the API (an empty "
                f"string or null leaves the value unchanged), so omit {param} to keep the current value"
            )
    if sid is None and all(value is None for value in texts.values()):
        raise ValueError(
            "nothing to update: pass at least one of name, status_id, billing_name or alternate_name "
            "(only the fields you pass are changed)"
        )
    body = build_body({"status_id": sid, **texts}, UPDATE_FIELD_MAP)  # the id is in the path, never in the body
    data = await client_of(ctx).patch(UPDATE_OP, path_params={"clientId": cid}, json_body=body, tool="update_client")
    return expect_object(data, UPDATE_OP, tool="update_client")


@gorelo_tool(toolset="core", kind="read", ops=[LOCATIONS_OP], field_map=LOCATIONS_FIELD_MAP)
async def list_client_locations(
    ctx: Context,
    client_id: Annotated[StrictId, Field(description="Client id (list_clients).")],
) -> dict:
    """List every location of one client (not paged); returns {items, count}. A row's Id is the location_id for create_contact and update_contact."""
    cid = positive_id("client_id", client_id)
    items = await client_of(ctx).get_list(LOCATIONS_OP, path_params={"clientId": cid}, tool="list_client_locations")
    return list_result(items)
