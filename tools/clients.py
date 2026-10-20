"""Clients and their locations: list, read, create, update (toolset "core").

Tools: list_clients, get_client, create_client, update_client, list_client_locations.

Ops owned by this module:

    GET /v1/clients
    GET /v1/clients/{clientId}                  (also read by update_client before and after it writes a custom field)
    GET /v1/clients/{clientId}/locations
    POST /v1/clients
    PATCH /v1/clients/{clientId}

Gorelo's client write endpoints answer with the full client record (ClientResponse), so create_client
and update_client return it as it is (update_client with a custom field is the one exception: it reads the client
again after the write and returns that read, see below). A write whose answer is not that record is
refused through created_id / expect_object (kind "shape", write_unconfirmed): the write may have been
applied, so the model must verify it with a read instead of repeating it. PATCH /v1/clients/{clientId} is a
true partial update that treats null as "leave unchanged" for its four plain fields; probe 2026-10-01 showed that
"" and null both leave a value unchanged, so name, status, billing name and alternate name cannot be cleared
through the API and update_client has no clear option for them (it rejects "" and says why).

Status scale (contract a7954b24844b, 2026-10-17): UpdateClientCommand.StatusId and the GET /v1/clients StatusIds filter
take 1 Active, 3 Credit hold, 4 Archived; any other value is a 400 and 2 (Inactive) is gone. The default list leaves
Archived out, a supplied list returns only those statuses (up to 100 values), deleted clients are never listed,
GET /v1/clients/{clientId} answers 404 for a deleted client like an unknown id and returns an Archived one.
update_client and list_clients check the status against that scale before any request (CLIENT_STATUS_LABELS).
DELETE /v1/clients/{clientId}, which the spec names for deleting, stays in FORBIDDEN_OPS.

The client id travels in the PATH only. The published UpdateClientCommand (contract e15cb5a18ec2, 2026-10-02) has
no Id field, so update_client sends none. The update used to be published with the id in the body and no id in
the path; that form answered 405 live on 2026-10-02 and a live override swapped the path form in until Gorelo
published it (the history is in spec/live_overrides.json, section retired). A missing client is a 404.

The list rows are ClientListItemResponse records (Id, Name, AlternateName, BillingName, Status, Domains,
IsDefault, CreatedOn, UpdatedOn). The single record (ClientResponse: what get_client returns and what create_client
and update_client answer) is that list shape PLUS CustomProperties, which the list deliberately leaves out
(contract 1b1c0cec0e9a, 2026-10-14): every custom field the service provider has defined for clients, each a
CustomPropertyValue {Id, Variable, Name, Type, Value, IsSet, IsHidden}. Value is the effective text (the saved value, else the
field's default, else null; a string whatever the Type) and IsSet is true when a value is saved on this client. IsHidden
(contract a7954b24844b, 2026-10-17) is true when the service provider has hidden the field's value from the API: Value is then
always null whether or not a value is saved, IsSet is still reported, and the value can still be written through the API.

Custom fields are what update_client CAN clear. UpdateClientCommand.CustomProperties (contract 1b1c0cec0e9a) is an
upsert by Id: an entry sent changes that field, one left out keeps its value, and a null Value clears the saved value
so the field falls back to its default (an empty string would SAVE an empty value; Gorelo refuses either on a required
field when it would leave it with no value). The tool sends custom_properties as {Id, Value: text} entries and, after
them, clear_custom_property_ids as {Id, Value: null} entries, in the one array. It never sends an empty string: a blank
value is refused locally and the message names clear_custom_property_ids. An id is given once across both lists.
Gorelo refuses a repeated id with a 400 as well (UpdateClientCommand.CustomProperties text of contract a7954b24844b),
so the local refusal is not stricter than Gorelo.

The reads around the write. The published text says CustomProperties is an upsert, but no live call has shown that,
and if production applied it as a replace, an update of one field would silently wipe every other custom field of a
real client. So, only when custom_properties or clear_custom_property_ids is given, update_client reads the client
(GET /v1/clients/{clientId}, declared in its ops) before the write and again after it: GET, PATCH, GET. A plain update
sends the PATCH only. A failed first read raises its own error and nothing is written. A record whose CustomProperties
is not a list of objects (each with an integer Id, a boolean IsSet and a Value that is text or null, each Id once) is a
shape error that is NOT write-unconfirmed: nothing was sent. Every sent id must be listed by that read, else a
ValueError names the parameter, the item and the id and no PATCH is sent, so an unknown id never reaches Gorelo.

After ANY 2xx PATCH the second read and its checks run, whatever the PATCH answer looks like. That answer is another
rendering of the client and was never compared with a read, so it takes no part in the checks; it is not required to
be a record to get them. An answer that cannot be used (a Data that is not a non-empty object, or a 2xx body with no
Gorelo envelope in it) is one more problem in the text, not a stop: the first read holds values that would be lost
for good if the update was applied as a replace and the tool gave up at the answer. The checks run on the second read,
which has the rendering of the first, and that second read is the record update_client returns.

The second read must show each sent field: an entry for every sent Id, with IsSet true and a Value that is non-blank
text for a saved value (this tool never sends an empty one) and IsSet false for a cleared one. A saved field that was
set before and still shows its previous Value although a different text was sent is not confirmed either (an ignored
update, or the same value written another way, which the message allows for). A non-blank Value is otherwise NOT
compared with what was sent (Gorelo shows display text and may normalise numbers or dates), so the update_client text
also tells the model to compare each returned Value with what it sent. Anything else is a write-unconfirmed shape error
that names the ids and says the update may have been applied; a PATCH answer that cannot be used is added to it, and
is the whole text when the second read shows no problem.

Every field of the first read that was NOT sent must come back in the second with the same IsSet and the same Value. A
difference or a missing field means that custom fields that were not in this call changed (another user, an
automation, or an update applied as a replace): that is one loud error, listed first, with each field's Id, Name and
previous state. It tells the model to tell the user at once, to ask before putting anything back, and gives the ONE
update_client call that puts the fields back together with what this call meant to set; a field that cannot be put
back that way (an empty previous value, a required field that was unset) is fixed in the Gorelo app.

Hidden custom fields (IsHidden true). The value of such a field is never readable, so no check can depend on it. IsHidden
is optional in a read (absent means not hidden) and must be a boolean when present, else the read is unusable; the Value of a hidden
field is dropped when a read is taken apart, so it is never compared, restored or quoted in an error (the record update_client
returns is Gorelo's read, unchanged). A saved hidden field is confirmed by IsSet alone, and the record update_client returns then
carries one extra key, warning, that says its value was not read back and could not be checked (never that it was checked); a
cleared hidden field is confirmed by IsSet false like any other. A field that was not sent is compared on IsSet and presence only,
and every error that lists fields says that a change of a hidden value cannot be seen. A restore call never offers a value it
could not read: a hidden field that held a value is fixed in the Gorelo app or written again with a value the user gives, and
an unset one may be cleared like any unset field.

If the second read fails or cannot be used, the update was sent and answered with success but nothing can be said
about what it did. The tool still holds what every custom field held before, so the error lists all of them (Id, Name,
previous value or unset), tells the model to compare that list with get_client, and, if a field that was not in the
call has changed, to tell the user, ask, and use the one call written out from the first read (plus what the call meant
to set). Without it the wiped values would be lost for good.

If the PATCH itself ends in a timeout, a connection error, a 5xx or a 3xx (the failures Gorelo marks write_unconfirmed), the
update may have been applied and the failure says nothing about the custom fields, so no second read is tried. The
error is the same kind of error, with the same list of what every custom field held before, the order to compare it
with get_client and the one call to put the fields back, built from the first read. Its text keeps the standard meaning
(the update may or may not have been applied: verify it with a read before repeating it) and the labels that the tool
gave Gorelo's notifications, and it keeps the status, the notifications and the trace id of the failure, which is its
__cause__. A 4xx refusal and a failure envelope in a success status are Gorelo saying that nothing was applied, and stay
as they are.

These errors are CollateralChangeError: the previous values are shown on purpose (they are the user's own data and are
needed to restore them), so the error also carries log_summary, and server.LogValueFilter logs a record whose
exception chain has it as that summary alone, without the traceback or any value, so that a previous value (possibly a
password or a licence key) does not reach the server journal. It has a __reduce__ of its own, because the one of
GoreloAPIError would rebuild a plain error and a copy would lose log_summary. The values the caller sent are never
echoed in an error. FastMCP's own DEBUG line with the arguments of a call is not ours and is left as it is: it shows
the sent values, at DEBUG only.

Not probed (not tested live yet): what Gorelo answers for an Id that is not a client custom field, the PropertyName of
its custom property errors, and the format each Type needs. Gorelo's release notes of 2026-10-16 say it now checks
Number, Checkbox and Dropdown values against the field Type; the published text of UpdateClientCommand.CustomProperties
says only that a repeated id is a 400, so no local format rule exists and the tool texts only advise. Gorelo's item number
in an error is assumed to count the one array that was sent, the saved entries first, so update_client relabels such an error
itself (_relabel_custom_property_errors): "CustomProperties[1].Value" becomes "custom_properties (item 2, custom
field 9, Value; Gorelo: CustomProperties 1 Value)", or "clear_custom_property_ids (item 1, custom field 7, Value;
Gorelo: ...)" past the saved entries. Because that reading of the index is an assumption, every label ends with
Gorelo's own name without brackets, so that a reader can see Gorelo's own PropertyName and check the tool's reading
of the index against it. Any name that starts with CustomProperties is placed when its index fits; an index past
the entries keeps Gorelo's own number under the parameters that were sent ("item past the end" beyond 18 digits,
which is never converted); a name with no usable index (a bare Value, CustomProperties, "[]", a malformed one)
lists the sent ids when several entries were sent, and is the one entry when only one was; a bare Id is never
pinned on a custom field, because the command also has an Id of its own, bound from the route (the client's). The
error keeps its kind, status, trace id and write_unconfirmed flag. There are no custom properties on create_client
(CreateClientCommand has none).

There is no delete tool: DELETE /v1/clients/{clientId} is in FORBIDDEN_OPS.
"""

import json
import re
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, NamedTuple

from fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field, Strict

from gorelo_client import GoreloAPIError
from tools._common import (
    MAX_ID,
    StrictId,
    build_body,
    clamp_page_size,
    client_of,
    created_id,
    csv_ids,
    describe_value,
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

# The client status scale of UpdateClientCommand.StatusId and of the GET /v1/clients StatusIds filter (contract
# a7954b24844b, 2026-10-17): "1 Active, 3 Credit hold, 4 Archived", any other value is a 400. 2 (Inactive) is gone.
CLIENT_STATUS_LABELS = {1: "Active", 3: "Credit hold", 4: "Archived"}
CLIENT_STATUS_SCALE = ", ".join(f"{status} {label}" for status, label in CLIENT_STATUS_LABELS.items())
STATUS_IDS_MAX = 100  # "Up to 100 values" (the StatusIds text)

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
# the command has no Id. custom_properties is the whole CustomProperties array (the entries to save, then the ones to
# clear): clear_custom_property_ids has no field of its own, its ids are entries of the same array.
UPDATE_FIELD_MAP = {
    "name": "Name",
    "status_id": "StatusId",
    "billing_name": "BillingName",
    "alternate_name": "AlternateName",
    "custom_properties": "CustomProperties",
}

UPDATE_TEXT_PARAMS = ("name", "billing_name", "alternate_name")

# What update_client declares as its field_map: the body fields plus the path placeholder, so that a Gorelo error
# about the id names client_id (build_body is never given client_id). An error about the CustomProperties array is
# relabelled by the tool itself before it gets here (_relabel_custom_property_errors): Gorelo counts the items of the
# one array, so only the tool knows whether item 3 is a saved or a cleared entry.
UPDATE_TOOL_FIELD_MAP = {**UPDATE_FIELD_MAP, "client_id": "clientId"}

# The keys of one custom_properties entry, in the order the tool lists them.
CUSTOM_PROPERTY_KEYS = ("id", "value")

# The path placeholders, so that a Gorelo error about the id names client_id (the parameter the model
# has to fix) and not "clientId".
GET_FIELD_MAP = {"client_id": "clientId"}
LOCATIONS_FIELD_MAP = {"client_id": "clientId"}


def _status(param: str, value: Any) -> int:
    """One client status: 1 Active, 3 Credit hold or 4 Archived. A whole number outside the scale (0 and the negatives
    included, so positive_id's "such as 123" does not suggest that any id will do) is answered with the scale and quoted
    back; whatever else is not an id (a boolean, a decimal, text, a number beyond int64) keeps the message of positive_id."""
    if isinstance(value, int) and not isinstance(value, bool) and -MAX_ID <= value < 1:
        status = int(value)
    else:
        status = positive_id(param, value)
    if status not in CLIENT_STATUS_LABELS:
        raise ValueError(f"{param}: expected {CLIENT_STATUS_SCALE}, got {status}")
    return status


def _statuses(param: str, values: Any) -> list[int] | None:
    """The status_ids filter of list_clients: at most STATUS_IDS_MAX items, each on the scale, a bad one named by its
    index. None, an empty list and a value that is not a list keep the messages of positive_ids."""
    if isinstance(values, (list, tuple)) and values:
        if len(values) > STATUS_IDS_MAX:
            raise ValueError(f"{param}: at most {STATUS_IDS_MAX} values, got {len(values)}")
        return [_status(f"{param}[{index}]", item) for index, item in enumerate(values)]
    return positive_ids(param, values)


class CustomPropertySet(BaseModel):
    # One custom field to save on a client: an entry of UpdateClientCommand.CustomProperties (CustomPropertyUpdate) in
    # snake_case. value is typed `str | None` and defaults to None only so that a missing or null value reaches
    # _custom_property_changes, which says to use clear_custom_property_ids, instead of a bare schema error; the
    # advertised schema still shows a string (the schema compaction drops the null branch and the default) and lists
    # only id as required, and the custom_properties parameter says the value is non-blank text. No docstring and no
    # field texts on purpose: pydantic would copy them into the advertised schema, which claude.ai pays for in every
    # conversation, and the custom_properties parameter already says what an entry is.

    model_config = ConfigDict(extra="forbid")

    id: StrictId
    value: Annotated[str | None, Strict()] = None


@gorelo_tool(toolset="core", kind="read", ops=[LIST_OP], field_map=LIST_FIELD_MAP)
async def list_clients(
    ctx: Context,
    query: Annotated[str | None, Field(description="Matches name, alternate name, billing name and domains.")] = None,
    status_ids: Annotated[
        list[StrictId] | None,
        Field(
            description="1 Active, 3 Credit hold, 4 Archived, up to 100. Omit it and Archived clients are left out; "
            "give it and only those are returned."
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
    Archived clients are left out unless status_ids asks for status 4; deleted clients are never listed. Rows leave out CustomProperties: get_client has them. Paging: pass next_cursor as cursor, same filters, until has_more is false.
    """
    keyword = non_empty("query", query)
    ids = csv_ids("status_ids", _statuses("status_ids", status_ids))
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
    """Get one client by id; returns Gorelo's client record.
    The record includes CustomProperties (each client custom field: Id, Name, Type, Value, IsSet true when a value is saved, IsHidden true when the Value is hidden from the API and always null). A deleted client is a 404 like an unknown id; an Archived client is returned.
    """
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


# --------------------------------------------------------------------------
# update_client: custom properties (local validation, the reads around the write and the checks on them)
# --------------------------------------------------------------------------


def _where(position: int, field: str | None = None) -> str:
    """Where a problem in custom_properties is: "custom_properties (item 2)" or "custom_properties (item 2, id)"."""
    return f"custom_properties (item {position}{f', {field}' if field else ''})"


def _custom_property_changes(
    custom_properties: Any, clear_custom_property_ids: Any
) -> tuple[list[tuple[int, str]], list[int]]:
    """(the custom fields to save as (id, text) pairs, the ids whose saved value is cleared), each in the caller's
    order and each empty when its parameter was not given.

    Pure: no HTTP. A problem raises ValueError naming the snake_case param; an entry of custom_properties is located
    by its place counted from 1 ("custom_properties (item 2, id)"), an id of the other list by its index like every
    id list ("clear_custom_property_ids[1]"). Ids are strict positive whole numbers (a bool or a digit string is no
    id), an empty list is refused for both params, and an id may appear once across both lists. A value must be text
    that is not blank: this tool never sends an empty string or a null through custom_properties (a null Value is how
    Gorelo clears a field, and that is clear_custom_property_ids). An entry is a CustomPropertySet or a plain object
    with the keys id and value.
    """
    saved: list[tuple[int, str]] = []
    cleared: list[int] = []
    item_of: dict[int, int] = {}  # id -> the item of custom_properties that holds it
    if custom_properties is not None:
        if isinstance(custom_properties, (str, bytes, Mapping)) or not isinstance(custom_properties, (list, tuple)):
            raise ValueError(
                "custom_properties: expected a list of objects with id and value, "
                f"got {describe_value(custom_properties)}"
            )
        if not custom_properties:
            raise ValueError(
                "custom_properties: expected at least one object with id and value, got an empty list "
                "(omit custom_properties if you save no custom field)"
            )
        for position, entry in enumerate(custom_properties, start=1):
            data = entry.model_dump() if isinstance(entry, BaseModel) else entry
            if not isinstance(data, Mapping):
                raise ValueError(
                    f"{_where(position)}: must be an object with id and value, got {describe_value(entry)}"
                )
            unknown = sorted(str(key) for key in data if key not in CUSTOM_PROPERTY_KEYS)
            if unknown:
                raise ValueError(
                    f"{_where(position)}: unknown field(s) {', '.join(unknown)}; "
                    f"allowed: {', '.join(CUSTOM_PROPERTY_KEYS)}"
                )
            identifier = positive_id(_where(position, "id"), data.get("id"))
            if identifier in item_of:
                raise ValueError(
                    f"{_where(position, 'id')}: custom field {identifier} is already in item {item_of[identifier]}; "
                    "give each id once (Gorelo refuses a repeated id with a 400 too)"
                )
            text = data.get("value")
            if text is None or (isinstance(text, str) and not text.strip()):
                raise ValueError(
                    f"{_where(position, 'value')}: must be text that is not empty or blank. To remove a saved value "
                    "pass its id in clear_custom_property_ids (custom_properties never sends an empty or null value)"
                )
            if not isinstance(text, str):
                raise ValueError(
                    f"{_where(position, 'value')}: must be text, got {describe_value(text)} "
                    "(a number or a date is written as text)"
                )
            item_of[identifier] = position
            saved.append((identifier, text))
    for index, identifier in enumerate(positive_ids("clear_custom_property_ids", clear_custom_property_ids) or []):
        if identifier in item_of:
            raise ValueError(
                f"clear_custom_property_ids[{index}]: custom field {identifier} is also in custom_properties "
                f"(item {item_of[identifier]}); an id is either saved or cleared in one call, not both "
                "(Gorelo refuses a repeated id with a 400 too)"
            )
        if identifier in cleared:
            raise ValueError(
                f"clear_custom_property_ids[{index}]: custom field {identifier} is already at "
                f"[{cleared.index(identifier)}]; give each id once (Gorelo refuses a repeated id with a 400 too)"
            )
        cleared.append(identifier)
    return saved, cleared


def _ids(values: list[int]) -> str:
    return ", ".join(str(value) for value in values)


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


class _CustomField(NamedTuple):
    """One custom field of the client as a read of it shows it: the state that two reads are compared by.

    `hidden` is IsHidden (contract a7954b24844b): the provider hides the field's value from the API. The value of a hidden
    field is always None here (Gorelo's Value is null then, and whatever else arrives is dropped), so two reads of it differ
    only in is_set."""

    name: str | None
    is_set: bool
    value: str | None
    hidden: bool = False


class _UnusableRead(Exception):
    """A read of the client cannot serve as the state of its custom fields. The message says why and holds no value."""


def _custom_fields_of(data: Any) -> dict[int, _CustomField]:
    """The custom fields in what GET /v1/clients/{clientId} answered, by Id.

    The record must be a non-empty object whose CustomProperties is a list of objects, each with an integer Id (once), a
    boolean IsSet and a Value that is text or null; anything else raises _UnusableRead. IsHidden, when present, must be a
    boolean (absent means not hidden); the Value of a hidden field is dropped (None). Both reads around a write are
    taken apart by this one function, because the checks compare the two and only a rendering that can be read proves
    anything.
    """
    if not isinstance(data, Mapping) or not data:
        raise _UnusableRead(f"expected Data to be a non-empty object but got {describe_value(data)}")
    listed = data.get("CustomProperties")
    if not isinstance(listed, list):
        if "CustomProperties" not in data:
            raise _UnusableRead("the client record has no CustomProperties")
        raise _UnusableRead(f"the client's CustomProperties is {describe_value(listed)}, not a list")
    fields: dict[int, _CustomField] = {}
    for position, entry in enumerate(listed, start=1):
        if not isinstance(entry, Mapping):
            raise _UnusableRead(f"CustomProperties item {position} is {describe_value(entry)}, not an object")
        identifier = entry.get("Id")
        if isinstance(identifier, bool) or not isinstance(identifier, int):
            raise _UnusableRead(f"CustomProperties item {position} has no integer Id")
        if identifier in fields:
            raise _UnusableRead(f"custom field {identifier} is listed more than once")
        name, is_set, value = entry.get("Name"), entry.get("IsSet"), entry.get("Value")
        hidden = entry.get("IsHidden", False)  # absent (an older rendering) means not hidden
        if not isinstance(is_set, bool):
            raise _UnusableRead(f"custom field {identifier} has no boolean IsSet")
        if not isinstance(hidden, bool):
            raise _UnusableRead(f"custom field {identifier} has an IsHidden that is neither true nor false")
        # a hidden field's Value is never read: whatever arrives (the spec says null) is dropped below, never refused
        if not hidden and value is not None and not isinstance(value, str):
            raise _UnusableRead(f"custom field {identifier} has a Value that is neither text nor null")
        fields[identifier] = _CustomField(name if _has_text(name) else None, is_set, None if hidden else value, hidden)
    return fields


async def _read_custom_fields_before(ctx: Context, client_id: int) -> dict[int, _CustomField]:
    """The client's custom fields as they are now, read BEFORE any custom field is written.

    A failed read raises its own error and nothing has been written. A record that cannot be used raises a shape error
    that is not write-unconfirmed: the state before the write cannot be established, so nothing is sent.
    """
    data = await client_of(ctx).get_one(GET_OP, path_params={"clientId": client_id}, tool="update_client")
    try:
        return _custom_fields_of(data)
    except _UnusableRead as problem:
        raise GoreloAPIError(
            f"{GET_OP}: {problem}; nothing was written, because the state of the custom fields before the update is "
            "unknown",
            status=200,
            op_key=GET_OP,
            kind="shape",
        ) from None


class _AfterReadFailed(Exception):
    """The read of the client after the update failed or cannot be used.

    `why` says so in words that carry nothing of what Gorelo answered; the failure itself, if there is one, is the
    __cause__. update_client turns it into the error that lists what the custom fields held before (_unchecked_error).
    """

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def _unusable_patch_answer(data: Any) -> str | None:
    """Why the Data of the PATCH answer cannot be the updated client record, or None if it can (a non-empty object)."""
    if isinstance(data, Mapping) and data:
        return None
    return f"expected Data to be a non-empty object but got {describe_value(data)}"


def _unreadable_patch_answer(err: GoreloAPIError) -> str | None:
    """Why the PATCH answer cannot be read, when Gorelo answered with a 2xx status and the client found no Gorelo
    envelope in the body (an empty body, a page, a bare list); None for every other failure.

    That is an answer that cannot be used, not a refusal: the update may have been applied, so it is judged like an
    unusable Data and the client is read again. The client words it without quoting the body.
    """
    if err.kind != "shape" or err.op_key != UPDATE_OP or err.status is None or err.status // 100 != 2:
        return None
    lead = f"{UPDATE_OP}: "
    text = str(err)
    return text[len(lead) :] if text.startswith(lead) else text


def _read_failure(err: GoreloAPIError) -> str:
    """What went wrong with the read after the update, in words that carry nothing of the answer's body."""
    if err.kind == "timeout":
        return "the read of the client after it timed out"
    if err.kind == "transport":
        return "the connection failed during the read of the client after it"
    if err.kind == "rate_limit":
        return "the read of the client after it was rate limited (HTTP 429)"
    if err.kind == "shape":
        return "the read of the client after it returned an answer that cannot be used"
    if err.kind == "envelope":
        return "the read of the client after it reported a failure"
    if err.status is not None:
        return f"the read of the client after it answered HTTP {err.status}"
    return "the read of the client after it failed"


async def _read_custom_fields_after(ctx: Context, client_id: int) -> tuple[dict[str, Any], dict[int, _CustomField]]:
    """The client record and its custom fields as they are after the write.

    A second GET, the same operation and the same rendering as the read before, so that the two can be compared; the
    PATCH answer is another rendering that was never compared with a read, and is not used for this. The update was
    sent and answered with success by now, so a read that fails or cannot be used raises _AfterReadFailed (the failure
    is its cause).
    """
    try:
        data = await client_of(ctx).get_one(GET_OP, path_params={"clientId": client_id}, tool="update_client")
        fields = _custom_fields_of(data)
    except GoreloAPIError as err:
        raise _AfterReadFailed(_read_failure(err)) from err
    except _UnusableRead as problem:
        raise _AfterReadFailed(f"the read of the client after it cannot be used: {problem}") from None
    return dict(data), fields


def _refuse_unknown_custom_fields(
    fields: Mapping[int, _CustomField], saved: list[tuple[int, str]], cleared: list[int], client_id: int
) -> None:
    """Raise a ValueError, before any PATCH, for every sent id that the client does not have as a custom field.

    The message names the parameter, the item and the id; an id Gorelo does not list never reaches Gorelo.
    """
    unknown = [
        f"{_where(position, 'id')}: custom field {identifier} is not a custom field of client {client_id} "
        "(get_client lists them)"
        for position, (identifier, _) in enumerate(saved, start=1)
        if identifier not in fields
    ]
    unknown += [
        f"clear_custom_property_ids[{index}]: custom field {identifier} is not a custom field of client {client_id} "
        "(get_client lists them)"
        for index, identifier in enumerate(cleared)
        if identifier not in fields
    ]
    if unknown:
        raise ValueError("; ".join(unknown))


def _collateral_changes(
    before: Mapping[int, _CustomField], after: Mapping[int, _CustomField], sent: set[int]
) -> list[tuple[int, _CustomField, str]]:
    """The fields of the read before that were NOT sent and that the read after does not show as they were: (id, state
    before, how the read after differs). A field missing from it, with another IsSet or with another Value counts; a
    field only the read after lists does not. In the order of the read before. A field hidden in either read is compared
    on IsSet and presence only (its Value is always null)."""
    changes: list[tuple[int, _CustomField, str]] = []
    for identifier, field in before.items():
        if identifier in sent:
            continue
        shown = after.get(identifier)
        if shown is None:
            now = "now missing from that read"
        elif shown.is_set != field.is_set:
            now = "now reads set" if shown.is_set else "now reads unset"
        elif shown.value != field.value and not (field.hidden or shown.hidden):
            now = "now shows a different value"
        else:
            continue
        changes.append((identifier, field, now))
    return changes


def _quoted(text: str) -> str:
    """The text as a JSON string, to be copied into a call as it is. GoreloAPIError squeezes every run of whitespace in
    its message to one space, which would silently change a value with two spaces, a tab or a non-breaking space; json
    already writes a tab or a newline as an escape, so the rest (a run of spaces after its first, any other whitespace
    character) is written as a \\u escape, which a JSON string reads back as the same character."""
    quoted = json.dumps(text, ensure_ascii=False)
    quoted = re.sub(r"[^\S ]", lambda found: f"\\u{ord(found.group()):04x}", quoted)
    return re.sub(r" {2,}", lambda found: " " + "\\u0020" * (len(found.group()) - 1), quoted)


_ASK_FIRST = "ask before putting anything back: another user or an automation may have made the change. "


def _closing(client_id: int) -> str:
    """The sentence that ends every error for an update that was sent and answered with success (a 2xx status)."""
    return (
        "Gorelo answered with success; the update may have been applied, so verify it with a read before repeating it "
        f"(get_client(client_id={client_id}))"
    )


def _answer_also(answer_problem: str) -> str:
    return f"The answer to the update also cannot be used as the client record: {answer_problem}. "


def _named(field: _CustomField) -> str:
    return f" ({_quoted(field.name)})" if field.name else ""


def _hidden_ids(*reads: Mapping[int, _CustomField]) -> list[int]:
    """The ids, ascending, of the custom fields that any of the reads shows as hidden."""
    return sorted({identifier for read in reads for identifier, field in read.items() if field.hidden})


def _hidden_note(ids: Iterable[int]) -> str:
    """The sentence that tells the model a change of a hidden value cannot be seen (only IsSet can be compared)."""
    return (
        f"Custom field(s) {_ids(list(ids))} are hidden from the API (IsHidden true): only whether they hold a value (IsSet) "
        "can be read, so a change of their value cannot be seen; check them in the Gorelo app if it matters. "
    )


def _unread_warning(ids: Iterable[int]) -> str:
    """The `warning` of an answer in which a saved custom field is hidden: its value was never read back."""
    return (
        f"custom field(s) {_ids(list(ids))} are hidden from the API (IsHidden true): Gorelo confirms that a value is saved "
        "(IsSet true) but never shows it, so the value you sent was not read back and could not be checked. Check it in the "
        "Gorelo app if it matters."
    )


def _held(field: _CustomField) -> tuple[str, str]:
    """What a custom field held, as words ("was set to "x"") and as a kind: "unset", "text", "empty" or "hidden"."""
    if not field.is_set:
        return "was unset", "unset"
    if field.hidden:
        return "was set (its value is hidden from the API and could not be read)", "hidden"
    if _has_text(field.value):
        return f"was set to {_quoted(field.value)}", "text"  # type: ignore[arg-type]
    return "was set to an empty value", "empty"


class _Restore(NamedTuple):
    """What a call that puts custom fields back is made of, from what each one held."""

    put_back: list[str]  # the entries of custom_properties, for the fields that held text
    unset: list[int]  # the fields that were unset: they go to clear_custom_property_ids
    empty: list[int]  # the fields that held an empty value, which this tool cannot send
    hidden: list[int]  # the fields that held a value Gorelo hides from the API: fixed in the Gorelo app or written again with a value the user gives


def _restore_of(fields: Iterable[tuple[int, _CustomField]]) -> _Restore:
    restore = _Restore([], [], [], [])
    for identifier, field in fields:
        kind = _held(field)[1]
        if kind == "unset":
            restore.unset.append(identifier)
        elif kind == "text":
            quoted = _quoted(field.value)  # type: ignore[arg-type]
            restore.put_back.append(f'{{"id": {identifier}, "value": {quoted}}}')
        elif kind == "hidden":
            restore.hidden.append(identifier)
        else:
            restore.empty.append(identifier)
    return restore


def _restore_advice(restore: _Restore, client_id: int, *, known_to_differ: bool) -> str:
    """The advice that follows "ask before putting anything back": the ONE update_client call that puts the fields back
    together with what this call meant to set (ids and previous values filled in), and the fields that the Gorelo app
    fixes (a hidden field that held a value is fixed there or written again with a value the user gives). `known_to_differ`
    is false when the fields come from the read before the update alone (the read after it failed): then some of them may
    not have changed, and the advice says to keep only what now differs."""
    text = ""
    if restore.put_back or restore.unset:
        arguments = []
        if restore.put_back:
            arguments.append(f"custom_properties=[{', '.join(restore.put_back)}]")
        if restore.unset:
            arguments.append(f"clear_custom_property_ids=[{_ids(restore.unset)}]")
        text += (
            "To put the fields back, make ONE update_client call that lists every field to put back together with the "
            "values you meant to set in this call (a call for one field alone could wipe the others again): "
            f"update_client(client_id={client_id}, {', '.join(arguments)})"
        )
        if restore.unset and not known_to_differ:
            text += " (in clear_custom_property_ids keep only the fields that hold a value now)"
        text += (
            ", to which you add the custom_properties and clear_custom_property_ids you meant to send in this call. "
        )
    if restore.empty:
        fix = "fix them" if known_to_differ else "if they differ, fix them"
        text += (
            f"Custom field(s) {_ids(restore.empty)} were set to an empty value, which this tool cannot send: {fix} in "
            "the Gorelo app. "
        )
    if restore.hidden:
        fix = "fix them" if known_to_differ else "if they differ, fix them"
        text += (
            f"Custom field(s) {_ids(restore.hidden)} were set to a value that Gorelo hides from the API, which this tool "
            f"could not read, so it cannot offer it back: {fix} in the Gorelo app, or ask the user for the value and "
            "write it with update_client. "
        )
    if restore.unset:
        text += (
            "If Gorelo refuses to clear one of the unset fields (a required field), fix that one in the Gorelo app. "
        )
    return text


class CollateralChangeError(GoreloAPIError):
    """The write-unconfirmed shape error whose text for the model lists what custom fields held before the update.

    That is the user's own data, needed to put the fields back, and possibly a password or a licence key. It is raised
    when custom fields changed although they were not in the call, when the read after the update fails or cannot be
    used, and when the PATCH itself ends in a timeout, a connection error, a 5xx or a 3xx (the tool holds what each field held
    in every case). So it also carries `log_summary`, the text for the server log: the operation, the client and the
    field ids, with no value. server.LogValueFilter logs a record whose exception, or any exception in its cause or
    context chain, has that attribute with that text and without the traceback (the attribute is
    server.PRIVATE_LOG_ATTRIBUTE, and a test pins that the two agree). For a failed PATCH it also keeps the status,
    the notifications and the trace id of that failure.
    """

    def __init__(
        self,
        message: str,
        log_summary: str,
        *,
        status: int | None = 200,
        notifications: list[Mapping[str, Any]] | None = None,
        trace_id: str | None = None,
    ) -> None:
        super().__init__(
            message,
            status=status,
            op_key=UPDATE_OP,
            kind="shape",
            notifications=notifications,
            trace_id=trace_id,
            write_unconfirmed=True,
        )
        self.log_summary = log_summary

    def __reduce__(self) -> tuple[Any, ...]:
        # GoreloAPIError.__reduce__ rebuilds a plain GoreloAPIError: a copy or a pickle would keep the message, with its
        # previous values, and lose log_summary, so the filter would log the copy with its traceback.
        return (type(self), (self.message, self.log_summary), dict(self.__dict__))


def _collateral_error(
    changes: list[tuple[int, _CustomField, str]],
    problems: list[str],
    client_id: int,
    answer_problem: str | None = None,
    *,
    hidden_ids: Iterable[int] = (),
) -> CollateralChangeError:
    """The error for fields that changed without being sent.

    It says what the read after shows and what each field was (the previous value is shown on purpose), then the order
    to tell the user at once and to ask before putting anything back, the ONE call that puts the fields back together
    with what this call meant to set, the fields that the Gorelo app has to fix (a hidden field that held a value is
    fixed there or written again with a value the user gives), the note on the hidden fields (`hidden_ids`), the problems
    with the fields that were sent, and the answer to the update if that could not be used either. The values the caller
    sent are not in it."""
    parts = [
        f"custom field {identifier}{_named(field)} {_held(field)[0]}, {now}" for identifier, field, now in changes
    ]
    text = (
        f"{UPDATE_OP}: the update was sent, but a read of the client after it does not show custom fields that were "
        f"not in this call as they were before it ({'; '.join(parts)}). Tell the user at once, and {_ASK_FIRST}"
    )
    restore = _restore_of((identifier, field) for identifier, field, _ in changes)
    text += _restore_advice(restore, client_id, known_to_differ=True)
    hidden = list(hidden_ids)
    if hidden:
        text += _hidden_note(hidden)
    if problems:
        text += f"The read after the update also does not confirm the fields that were sent ({'; '.join(problems)}). "
    if answer_problem:
        text += _answer_also(answer_problem)
    ids = _ids([identifier for identifier, _, _ in changes])
    return CollateralChangeError(
        text + _closing(client_id),
        f"{UPDATE_OP} on client {client_id}: custom field(s) {ids} changed without being sent; "
        "previous values withheld from the log",
    )


def _unchecked_error(
    before: Mapping[int, _CustomField], why: str, sent: Iterable[int], answer_problem: str | None, client_id: int
) -> CollateralChangeError:
    """The error for an update that was sent and answered with success when the read after it failed or cannot be used.

    Nothing can be said about what the update did, but the tool still holds what every custom field held before it
    (the first read): it lists them all (Id, Name, previous value or unset) for the model to compare with get_client.
    If a field that was not in the call has changed, the model tells the user, asks, and puts the fields back with the
    ONE call written out here from the first read; under an update applied as a replace, those values would be lost for
    good otherwise. Like the alarm it holds previous values, so it carries a log_summary with none."""
    text = f"{UPDATE_OP}: the update was sent, but the custom fields cannot be checked: {why}. "
    if answer_problem:
        text += _answer_also(answer_problem)
    return CollateralChangeError(
        text + _states_and_advice(before, sent, client_id) + _closing(client_id),
        f"{UPDATE_OP} on client {client_id}: the custom fields could not be checked after the update; "
        f"previous states of custom field(s) {_ids(list(before))} withheld from the log",
    )


def _states_and_advice(before: Mapping[int, _CustomField], sent: Iterable[int], client_id: int) -> str:
    """What every custom field held before the update (Id, Name, previous value or unset), the order to compare that
    list with get_client, and, for the fields that were not in the call, the order to tell the user and ask if one has
    changed, then the ONE call that puts them back (built from this first read, to which the model adds what the call
    meant to set). Shared by the errors for an update whose effect is not known."""
    states = [f"custom field {identifier}{_named(field)} {_held(field)[0]}" for identifier, field in before.items()]
    text = (
        f"Before the update this tool read the client's custom fields: {'; '.join(states)}. "
        f"Compare that list with get_client(client_id={client_id}). "
    )
    hidden = _hidden_ids(before)
    if hidden:
        text += _hidden_note(hidden)
    sent_ids = set(sent)
    unsent = [(identifier, field) for identifier, field in before.items() if identifier not in sent_ids]
    if unsent:
        text += f"If a custom field that was not in this call has changed, tell the user at once, and {_ASK_FIRST}"
        text += _restore_advice(_restore_of(unsent), client_id, known_to_differ=False)
    return text


def _patch_failure(err: GoreloAPIError) -> str:
    """What went wrong with the PATCH, in words that carry nothing of a body: the status and, when Gorelo sent
    notifications, their codes and texts under the names the tool gave them (see _relabel_custom_property_errors)."""
    if err.kind == "timeout":
        return "the request timed out"
    if err.kind == "transport":
        return "the connection failed"
    lead = f"{UPDATE_OP}: "
    if err.kind == "shape" and str(err).startswith(lead):  # a page or an empty body that the client refused to read
        return str(err)[len(lead) :]
    http = f"Gorelo answered HTTP {err.status}" if err.status is not None else "Gorelo answered with an error"
    codes = list(dict.fromkeys(str(note["code"]) for note in err.notifications if note.get("code")))
    texts = [
        f"{note['property']}: {note['message']}" if note.get("property") else str(note.get("message") or "no message")
        for note in err.notifications[:3]
    ]
    if len(err.notifications) > 3:
        texts.append(f"and {len(err.notifications) - 3} more")
    return http + (f" (code {'/'.join(codes)})" if codes else "") + (f": {'; '.join(texts)}" if texts else "")


def _unconfirmed_patch_error(
    before: Mapping[int, _CustomField], err: GoreloAPIError, sent: Iterable[int], client_id: int
) -> CollateralChangeError:
    """The error for a PATCH that ended in a timeout, a connection error, a 5xx or a 3xx: Gorelo may have applied the update.

    Its answer, if there is one, says nothing about the custom fields, but the tool still holds what every one of them
    held before (the first read), and under an update applied as a replace those values would be lost for good. So the
    text keeps the standard meaning (the update may or may not have been applied: verify it with a read before
    repeating it) and adds what _unchecked_error adds. `err` is the failure with the labels of Gorelo's notifications
    already renamed, and its status, notifications and trace id are kept. The log text names the kind of failure and
    nothing Gorelo said, because a notification may echo a value."""
    if err.kind == "timeout":
        how = "timeout"
    elif err.kind == "transport":
        how = "connection error"
    else:
        how = f"HTTP {err.status}" if err.status is not None else "no HTTP status"
    text = (
        f"{UPDATE_OP}: the update was sent, but Gorelo did not confirm it: {_patch_failure(err).rstrip('. ')}. "
        + _states_and_advice(before, sent, client_id)
        + "Gorelo did not confirm the update; it may or may not have been applied, so verify it with a read before "
        f"repeating it (get_client(client_id={client_id}))"
    )
    return CollateralChangeError(
        text,
        f"{UPDATE_OP} on client {client_id}: the update was not confirmed ({how}); "
        f"previous states of custom field(s) {_ids(list(before))} withheld from the log",
        status=err.status,
        notifications=err.notifications,
        trace_id=err.trace_id,
    )


def _confirm_custom_properties(
    after: Mapping[int, _CustomField],
    before: Mapping[int, _CustomField],
    saved: list[tuple[int, str]],
    cleared: list[int],
    client_id: int,
    answer_problem: str | None = None,
) -> list[int]:
    """Raise the write-unconfirmed shape error unless the read after the write shows the custom fields as sent.

    `before` and `after` are two reads of the client with the same rendering (_custom_fields_of); the PATCH answer
    takes no part, except that `answer_problem` (why that answer could not be used, or None) is one more problem in the
    text. For the fields that were sent, `after` must have an entry for every sent Id. A field that was saved
    reads IsSet true (a saved value that equals the default still does) and a Value that is non-blank text, since this
    tool never sends an empty value, and it must not still show its previous Value when it was set before and a
    different text was sent (an ignored update, or the same value written another way); a field that was cleared
    reads IsSet false. A non-blank Value is otherwise NOT compared with what was sent: Gorelo shows display text and
    may normalise a number or a date. A saved field that the second read shows as hidden is confirmed by IsSet alone (no
    text rule, no previous-value rule); the function returns the ids of those fields, in the order of `saved`, so the
    caller can say that their value was not read back. A hidden field is compared on IsSet only in the collateral check.

    Every field in `before` that was not sent must have the same IsSet and the same Value in `after`: a difference, or
    a missing field, is its own error, listed first (see _collateral_error), and the problems with the sent fields
    follow in the same error. The messages name ids and fields, never a value that the caller sent, and say to check
    with get_client before repeating.
    """
    saved_ids = [identifier for identifier, _ in saved]
    sent_ids = [*saved_ids, *cleared]
    absent = [identifier for identifier in sent_ids if identifier not in after]
    unsaved = [identifier for identifier in saved_ids if identifier in after and not after[identifier].is_set]
    valueless = [
        identifier
        for identifier in saved_ids
        if identifier in after
        and after[identifier].is_set
        and not after[identifier].hidden
        and not _has_text(after[identifier].value)
    ]
    unchanged = [
        identifier
        for identifier, text in saved
        if identifier in before
        and identifier in after
        and identifier not in unsaved
        and identifier not in valueless
        and not before[identifier].hidden
        and not after[identifier].hidden
        and before[identifier].is_set
        and text != before[identifier].value
        and after[identifier].value == before[identifier].value
    ]
    still_saved = [identifier for identifier in cleared if identifier in after and after[identifier].is_set]
    problems = []
    if absent:
        problems.append(f"no entry for custom field(s) {_ids(absent)}")
    if unsaved:
        problems.append(f"IsSet is not true for custom field(s) {_ids(unsaved)}, which were sent with a value")
    if valueless:
        problems.append(f"custom field(s) {_ids(valueless)} show no text value, although they were sent with one")
    if unchanged:
        problems.append(
            f"custom field(s) {_ids(unchanged)} still show their previous value (if you wrote the same value another "
            "way, nothing is wrong; otherwise Gorelo ignored it)"
        )
    if still_saved:
        problems.append(f"IsSet is not false for custom field(s) {_ids(still_saved)}, which were sent to be cleared")
    changes = _collateral_changes(before, after, set(sent_ids))
    if changes:
        raise _collateral_error(changes, problems, client_id, answer_problem, hidden_ids=_hidden_ids(before, after))
    if problems:
        text = (
            f"{UPDATE_OP}: the update was sent, but a read of the client after it does not confirm the custom fields "
            f"that were sent ({'; '.join(problems)}). "
        )
        if answer_problem:
            text += _answer_also(answer_problem)
    elif answer_problem:
        text = (
            f"{UPDATE_OP}: the update was sent, and a read of the client after it shows no problem with the custom "
            f"fields, but the answer to the update cannot be used as the client record: {answer_problem}. "
        )
    else:
        # every saved id is in `after` here (an absent one would have been a problem): the saved hidden fields are
        # confirmed by IsSet alone, and the caller says so in a warning
        return [identifier for identifier in saved_ids if after[identifier].hidden]
    raise GoreloAPIError(
        text + _closing(client_id), status=200, op_key=UPDATE_OP, kind="shape", write_unconfirmed=True
    )


# Gorelo's name for a problem inside the CustomProperties array starts with CustomProperties (any case), then has an
# optional index and a tail that goes on with a dot or a bracket: "CustomProperties[1].Value", "CustomProperties[1].",
# "CustomProperties[2].Value[1]", "CustomProperties[2][0]", "CustomProperties.Value", "CustomProperties". A bare nested
# name ("Value", "Id") is the way Gorelo reports a bad Location.Phone as "Phone". ASCII only: \d is 0-9 and case is
# folded plainly.
_ARRAY_PROPERTY = re.compile(r"CustomProperties(?:\[(\d*)\])?([.\[].*)?", re.IGNORECASE | re.ASCII | re.DOTALL)
_BARE_PROPERTY = re.compile(r"Value|Id", re.IGNORECASE | re.ASCII)
_PLAIN_TAIL = re.compile(r"(?:\.\w+)+", re.ASCII)
# An index of more than this many digits is no item number (no array has so many items) and is never converted: int()
# refuses thousands of digits, and the shared error text applies the same cap.
_INDEX_DIGITS_MAX = 18
_RAW_NAME_MAX = 80


def _gorelo_name(name: str) -> str:
    """Gorelo's own name with no bracket and no dot ("CustomProperties[1].Value" becomes "CustomProperties 1 Value"),
    cut at _RAW_NAME_MAX characters: it is shown beside the label so that nobody has to trust the tool's reading of
    the index."""
    text = " ".join(re.sub(r"[\[\].]", " ", name).split())
    return text if len(text) <= _RAW_NAME_MAX else text[: _RAW_NAME_MAX - 3] + "..."


def _label(param: str, places: list[str | None], raw: str) -> str:
    """"param (place, place; Gorelo: raw name)": no bracket in it, so the generic error text prints it as it is."""
    return f"{param} ({', '.join(place for place in places if place)}; Gorelo: {_gorelo_name(raw)})"


def _custom_property_label(prop: Any, saved: list[int], cleared: list[int]) -> str | None:
    """What to call a Gorelo PropertyName that is about the CustomProperties array, or None for any other name.

    Gorelo counts the items of the one array that was sent: the saved entries first, then the cleared ones. So item k
    (from 0) is custom_properties item k+1 when k is below the number of saved entries, and otherwise
    clear_custom_property_ids item k-saved+1, and the custom field id of that entry is named (an id is safe to name, a
    value never is): "custom_properties (item 2, custom field 9, Value; Gorelo: CustomProperties 1 Value)". That
    placement assumes Gorelo's item number is the 0-based position in the array that was sent, which was never probed,
    so Gorelo's own name always follows the label, without brackets.

    Any name that starts with CustomProperties is placed this way when its index fits. An index past the entries keeps
    Gorelo's own number under the parameter(s) that were sent ("item past the end" for one of more than 18 digits, which
    is never converted). A name with no usable index (none, an empty "[]", a malformed one) cannot be placed when
    several entries were sent, so the sent ids are listed under the parameter(s) that were sent; with one entry sent it
    can only be that one. A bare Value is treated like that; a bare Id may also be the client's own (the command has
    an Id too, bound from the route, as its published description says), so it is never pinned on a custom field.
    """
    if not isinstance(prop, str):
        return None
    raw = prop.strip()
    sent = _ids([*saved, *cleared])
    array = _ARRAY_PROPERTY.fullmatch(raw)
    if array:
        digits, tail = array.group(1), array.group(2) or ""
        nested = tail.lstrip(".") if _PLAIN_TAIL.fullmatch(tail) and len(tail) <= _RAW_NAME_MAX else None
    elif _BARE_PROPERTY.fullmatch(raw):
        digits, nested = None, raw
        if raw.lower() == "id":
            return f"{raw} (client_id, or one of custom fields {sent}; Gorelo: {_gorelo_name(raw)})"
    else:
        return None
    total = len(saved) + len(cleared)
    index: int | None = None
    if digits and len(digits) <= _INDEX_DIGITS_MAX:
        index = int(digits)
    elif not digits and total == 1:
        index = 0  # no item number, with one entry sent, can only be that entry
    if index is not None and index < len(saved):
        return _label("custom_properties", [f"item {index + 1}", f"custom field {saved[index]}", nested], raw)
    if index is not None and index < total:
        place = index - len(saved)
        return _label("clear_custom_property_ids", [f"item {place + 1}", f"custom field {cleared[place]}", nested], raw)
    params = " or ".join(
        param for param, ids in (("custom_properties", saved), ("clear_custom_property_ids", cleared)) if ids
    )
    if digits:  # an item number past the entries that were sent: Gorelo's own number, nothing guessed
        return _label(params, [f"item {index + 1}" if index is not None else "item past the end", nested], raw)
    return _label(params, [f"{nested + ' of ' if nested else ''}one of custom fields {sent}"], raw)


def _relabel_custom_property_errors(err: GoreloAPIError, saved: list[int], cleared: list[int]) -> GoreloAPIError:
    """The same error with each notification about the CustomProperties array renamed by _custom_property_label.

    The new error has the same message, kind, status, operation, trace id and write_unconfirmed flag; only the
    PropertyName of the notifications that were relabelled differs, and the generic error text prints a name it does
    not know as it is. Returns err itself when nothing needs a new name (another kind of failure has no notifications).
    """
    if not saved and not cleared:
        return err
    notifications = []
    changed = False
    for note in err.notifications:
        label = _custom_property_label(note.get("property"), saved, cleared)
        changed = changed or label is not None
        notifications.append(note if label is None else {**note, "property": label})
    if not changed:
        return err
    return GoreloAPIError(
        err.message,
        status=err.status,
        op_key=err.op_key,
        kind=err.kind,
        notifications=notifications,
        trace_id=err.trace_id,
        write_unconfirmed=err.write_unconfirmed,
    )


@gorelo_tool(
    toolset="core", kind="write", ops=[GET_OP, UPDATE_OP], field_map=UPDATE_TOOL_FIELD_MAP, destructive_hint=True
)
async def update_client(
    ctx: Context,
    client_id: Annotated[StrictId, Field(description="Client id (list_clients).")],
    name: Annotated[str | None, Field(description="New display name.")] = None,
    status_id: Annotated[
        StrictId | None,
        Field(
            description="1 Active, 3 Credit hold, 4 Archived; any other value is refused. Archived hides the client from default lists."
        ),
    ] = None,
    billing_name: Annotated[str | None, Field(description="New name printed on invoices.")] = None,
    alternate_name: Annotated[str | None, Field(description="New alternate or trading name.")] = None,
    custom_properties: Annotated[
        list[CustomPropertySet] | None,
        Field(
            description="Fields to save: [{id, value}], non-blank text, each id once. Gorelo checks Number, Checkbox, Dropdown values. To remove a value use clear_custom_property_ids."
        ),
    ] = None,
    clear_custom_property_ids: Annotated[
        list[StrictId] | None,
        Field(
            description="Custom field ids to clear: the saved value goes and the field shows its default again. "
            "A required field cannot be cleared to nothing."
        ),
    ] = None,
) -> dict:
    """Update a client's name, status, billing name, alternate name or custom fields; returns the updated client record.
    Only the fields you pass change (at least one); only custom fields can be cleared. Custom fields: ids from get_client's CustomProperties, values are text whatever the Type. Compare each returned CustomProperties Value with what you sent; tell the user if one differs. A hidden field (IsHidden) shows no Value: only IsSet is checked (see warning). It reads the client first: ids must be listed there, and other custom fields must come back unchanged. Side effects: overwrites current values (old values are not kept); status_id 4 (Archived) hides the client from default lists.
    """
    cid = positive_id("client_id", client_id)
    sid = None if status_id is None else _status("status_id", status_id)
    texts = {"name": name, "billing_name": billing_name, "alternate_name": alternate_name}
    for param in UPDATE_TEXT_PARAMS:
        value = texts[param]
        if value is not None and not value.strip():
            raise ValueError(
                f"{param}: must not be empty. Gorelo cannot clear client fields through the API (an empty "
                f"string or null leaves the value unchanged), so omit {param} to keep the current value"
            )
    saved, cleared = _custom_property_changes(custom_properties, clear_custom_property_ids)
    if sid is None and not saved and not cleared and all(value is None for value in texts.values()):
        raise ValueError(
            "nothing to update: pass at least one of name, status_id, billing_name, alternate_name, custom_properties "
            "or clear_custom_property_ids (only the fields you pass are changed)"
        )
    # the saved entries first, then the cleared ones with a real JSON null (build_body leaves what is inside the list
    # alone); the client id is in the path, never in the body
    entries = [{"Id": identifier, "Value": text} for identifier, text in saved]
    entries += [{"Id": identifier, "Value": None} for identifier in cleared]
    body = build_body({"status_id": sid, **texts, "custom_properties": entries or None}, UPDATE_FIELD_MAP)
    saved_ids = [identifier for identifier, _ in saved]
    before: dict[int, _CustomField] = {}
    if entries:  # a plain update sends no GET; a custom field is only written after the client was read
        before = await _read_custom_fields_before(ctx, cid)
        _refuse_unknown_custom_fields(before, saved, cleared, cid)
    answer_problem = None
    try:
        data = await client_of(ctx).patch(
            UPDATE_OP, path_params={"clientId": cid}, json_body=body, tool="update_client"
        )
    except GoreloAPIError as err:
        # A 2xx answer with no Gorelo envelope in it is no refusal: the custom fields are still read and checked below
        answer_problem = _unreadable_patch_answer(err) if entries else None
        if answer_problem is None:
            relabelled = _relabel_custom_property_errors(err, saved_ids, cleared)
            if entries and relabelled.write_unconfirmed:
                # a timeout, a connection error, a 5xx or a 3xx: the update may have been applied and the failure says nothing
                # about the custom fields, so the error lists what they held before (a 4xx or a failure envelope is
                # Gorelo saying that nothing was applied, and stays as it is)
                raise _unconfirmed_patch_error(before, relabelled, [*saved_ids, *cleared], cid) from err
            if relabelled is err:
                raise
            raise relabelled from err
        data = None
    if not entries:
        return expect_object(data, UPDATE_OP, tool="update_client")
    # After any 2xx PATCH the custom fields are checked on a second read of the client, which has the rendering of the
    # first one, whatever the PATCH answer looks like: that answer is another rendering, and one that cannot be used
    # is one more problem in the error, not a stop (the read before holds values that would otherwise be lost). The
    # record that is returned is the second read.
    answer_problem = answer_problem or _unusable_patch_answer(data)
    try:
        after_record, after = await _read_custom_fields_after(ctx, cid)
    except _AfterReadFailed as failed:
        raise _unchecked_error(before, failed.why, [*saved_ids, *cleared], answer_problem, cid) from failed.__cause__
    unread = _confirm_custom_properties(after, before, saved, cleared, cid, answer_problem)
    if unread:
        after_record["warning"] = _unread_warning(unread)
    return after_record


@gorelo_tool(toolset="core", kind="read", ops=[LOCATIONS_OP], field_map=LOCATIONS_FIELD_MAP)
async def list_client_locations(
    ctx: Context,
    client_id: Annotated[StrictId, Field(description="Client id (list_clients).")],
) -> dict:
    """List every location of one client (not paged); returns {items, count}. A row's Id is the location_id for create_contact and update_contact."""
    cid = positive_id("client_id", client_id)
    items = await client_of(ctx).get_list(LOCATIONS_OP, path_params={"clientId": cid}, tool="list_client_locations")
    return list_result(items)
