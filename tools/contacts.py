"""Contacts: list, read, create, update (toolset "core").

Tools: list_contacts, get_contact, create_contact, update_contact.

Ops owned by this module:

    GET /v1/contacts
    GET /v1/contacts/{contactId}
    POST /v1/contacts
    PATCH /v1/contacts/{contactId}

Contract notes (observed on 2026-10-01, see docs/API-OBSERVED-BEHAVIOR.md):

* GET /v1/contacts has both a legacy ClientId and ClientIds. list_contacts only ever sends ClientIds.
* POST /v1/contacts accepts the four required fields (ClientId, FirstName, LastName, PrimaryEmail) and
  leaves everything else null, so create_contact infers nothing and defaults nothing.
* The contact id travels in the PATH only. The published UpdateContactCommand (contract e15cb5a18ec2, 2026-10-02)
  has no ContactId field, so update_contact sends none. The update used to be published with the id in the body
  and no id in the path; that form answered 405 live on 2026-10-02 and a live override swapped the path form in
  until Gorelo published it (the history is in spec/live_overrides.json, section retired). The operation is still
  a full-body command (without PrimaryEmail or ClientId it is a 400).
* PATCH /v1/contacts/{contactId} REPLACES the whole contact: a request with only the required fields wiped the
  phones and the job title (probe of 2026-10-01, on the retired collection form, with the same body; the live
  operation is still a full-body command). update_contact therefore reads the contact (GET /v1/contacts/{contactId}),
  builds the complete UpdateContactCommand from that record plus the caller's changes, and sends it. Only the
  command's own fields are copied, never the read-only ones (Id, Alias, Status, CreatedOn, ...).
  It refuses, before anything is written, a record that lacks a key it would have to copy (that value
  would be erased), that belongs to another contact, whose ClientId is null, or whose FirstName,
  LastName or PrimaryEmail is null and not given by the caller (the command requires them).
* The body never carries an explicit null. A copied value that is null is OMITTED: PATCH replaces
  the contact, so an omitted field is stored as null anyway (the probe showed it), and most of these
  fields are not nullable in the spec. clear_fields works the same way: a cleared field is simply not
  sent. FirstName, LastName, ClientId and PrimaryEmail are always sent with values.
* SecondaryEmail is part of the command but is NOT returned by any read, and only the user can see a
  contact's secondary emails (in the Gorelo app). Every update would erase them, so the choice goes
  through the user: update_contact refuses to run until the caller passes the complete list the
  user wants in secondary_email (it replaces ALL existing ones, [] erases them) or, after asking the user,
  clear_secondary_email_ok=true. The refusal says to ask the user first and does not read as an invitation
  to retry with the flag. SecondaryEmail is only sent when the caller gave secondary_email.
* Gorelo's write endpoints answer with the full contact record (ContactResponse). create_contact and
  update_contact return it as it is. An answer that is not that record (null, {}, false, a list, or on
  create a record without an Id) is refused through created_id / expect_object (kind "shape",
  write_unconfirmed): the write may have been applied, so it must be verified with a read, never repeated.
  There is no read-back fallback: re-reading after a `false` would show the OLD record and look like success.

There is no delete tool: DELETE /v1/contacts/{contactId} is in FORBIDDEN_OPS.
"""

from typing import Annotated, Any, Literal, get_args

from fastmcp import Context
from pydantic import Field

from gorelo_client import GoreloAPIError
from tools._common import (
    StrictBool,
    StrictId,
    build_body,
    clamp_page_size,
    client_of,
    created_id,
    csv_ids,
    describe_value,
    expect_object,
    gorelo_tool,
    non_empty,
    paged_result,
    positive_id,
    positive_ids,
    region_code,
    utc_iso,
)

LIST_OP = "GET /v1/contacts"
GET_OP = "GET /v1/contacts/{contactId}"
CREATE_OP = "POST /v1/contacts"
UPDATE_OP = "PATCH /v1/contacts/{contactId}"

SINCE_HELP = "ISO 8601 with UTC offset, e.g. 2026-10-01T00:00:00Z."

# snake_case tool parameter -> Gorelo query name, used to name the parameter in Gorelo's errors.
# client_id and client_ids are both sent as ClientIds (never the legacy ClientId).
LIST_FIELD_MAP = {
    "client_id": "ClientIds",
    "client_ids": "ClientIds",
    "status_ids": "StatusIds",
    "query": "Query",
    "created_since": "CreatedSince",
    "created_before": "CreatedBefore",
    "updated_since": "UpdatedSince",
    "updated_before": "UpdatedBefore",
    "page_size": "PageSize",
    "cursor": "Cursor",
}

# The path placeholder of get_contact, so that a Gorelo error about the id names contact_id.
GET_FIELD_MAP = {"contact_id": "contactId"}

# snake_case tool parameter -> CreateContactCommand field.
CREATE_FIELD_MAP = {
    "client_id": "ClientId",
    "first_name": "FirstName",
    "last_name": "LastName",
    "primary_email": "PrimaryEmail",
    "location_id": "LocationId",
    "secondary_email": "SecondaryEmail",
    "mobile_phone": "MobilePhone",
    "mobile_phone_country_code": "MobilePhoneCountryCode",
    "office_phone": "OfficePhone",
    "office_phone_country_code": "OfficePhoneCountryCode",
    "job_title": "JobTitle",
    "department": "Department",
    "time_zone": "TimeZone",
    "description": "Description",
}

# snake_case tool parameter -> UpdateContactCommand field. Body fields only: the contact id travels in the path and
# the command has no ContactId. There is deliberately no client_id: the client of a contact cannot be changed
# through this tool (ClientId is copied from the record).
UPDATE_FIELD_MAP = {
    "first_name": "FirstName",
    "last_name": "LastName",
    "primary_email": "PrimaryEmail",
    "location_id": "LocationId",
    "secondary_email": "SecondaryEmail",
    "mobile_phone": "MobilePhone",
    "mobile_phone_country_code": "MobilePhoneCountryCode",
    "office_phone": "OfficePhone",
    "office_phone_country_code": "OfficePhoneCountryCode",
    "job_title": "JobTitle",
    "department": "Department",
    "time_zone": "TimeZone",
    "description": "Description",
}

# What update_contact declares as its field_map: the body fields plus the path placeholder, so that a Gorelo error
# about the id names contact_id (build_body is never given contact_id).
UPDATE_TOOL_FIELD_MAP = {**UPDATE_FIELD_MAP, "contact_id": "contactId"}

# Fields update_contact may clear through clear_fields (the contact ends up without them: they are left out of
# the PATCH, which replaces the whole contact), and the three it can never blank (the command requires them).
# The Literal puts the allowed names in the advertised schema, like update_item's clear_fields.
ClearableField = Literal[
    "location_id",
    "mobile_phone",
    "mobile_phone_country_code",
    "office_phone",
    "office_phone_country_code",
    "job_title",
    "department",
    "time_zone",
    "description",
]
CLEARABLE_FIELDS = get_args(ClearableField)
REQUIRED_FIELDS = ("first_name", "last_name", "primary_email")

# What update_contact copies from the contact record into the command: the UpdateContactCommand
# fields the API also returns. SecondaryEmail comes from the caller (no read returns it); the contact id is in
# the path and in no body field; Id, Alias, Status, CreatedOn and the other response-only fields are
# never copied (an unknown body field is a 400).
COPIED_FIELDS = (
    "FirstName",
    "LastName",
    "ClientId",
    "LocationId",
    "PrimaryEmail",
    "MobilePhone",
    "MobilePhoneCountryCode",
    "OfficePhone",
    "OfficePhoneCountryCode",
    "JobTitle",
    "Department",
    "TimeZone",
    "Description",
)

PHONE_PAIRS = (
    ("mobile_phone", "mobile_phone_country_code"),
    ("office_phone", "office_phone_country_code"),
)


def _filled(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _addresses(param: str, values: list[str] | None, *, allow_empty: bool) -> list[str] | None:
    """A list of email addresses: None stays None, items must be non-blank text, [] only if allowed."""
    if values is None:
        return None
    if not values:
        if allow_empty:
            return []
        raise ValueError(f"{param}: must not be an empty list; omit it to create the contact without secondary emails")
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{param}: every address must be non-empty text, got {describe_value(item)}")
    return list(values)


def _phone_needs_region(phone_param: str, region_param: str, phone: str | None, region: str | None) -> None:
    """create_contact: a phone number without its ISO region code is a local error naming the region."""
    if phone is not None and region is None:
        raise ValueError(
            f"{region_param}: required when {phone_param} is given. Gorelo needs the ISO region code of "
            "the number (for example US or CA) and this tool assumes none"
        )


@gorelo_tool(toolset="core", kind="read", ops=[LIST_OP], field_map=LIST_FIELD_MAP)
async def list_contacts(
    ctx: Context,
    client_id: Annotated[
        StrictId | None, Field(description="Only this client's contacts (list_clients). Not with client_ids.")
    ] = None,
    client_ids: Annotated[
        list[StrictId] | None, Field(description="Only these clients' contacts (list_clients). Not with client_id.")
    ] = None,
    status_ids: Annotated[list[StrictId] | None, Field(description="Status.Id values from contact records.")] = None,
    query: Annotated[str | None, Field(description="Matches email addresses, first name and last name.")] = None,
    created_since: Annotated[str | None, Field(description=f"Created at or after; {SINCE_HELP}")] = None,
    created_before: Annotated[str | None, Field(description="Created before.")] = None,
    updated_since: Annotated[str | None, Field(description="Updated at or after.")] = None,
    updated_before: Annotated[str | None, Field(description="Updated before.")] = None,
    page_size: Annotated[int, Field(description="Rows per page, 1 to 200.")] = 200,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous call.")] = None,
) -> dict:
    """List contacts one page per call; returns {items, has_more, next_cursor, total_count, ...}.
    Filter by client_id (one) or client_ids (several), not both. Rows never include SecondaryEmail: Gorelo does not return it. Paging: pass next_cursor as cursor, same filters, until has_more is false.
    """
    if client_id is not None and client_ids is not None:
        raise ValueError(
            "client_id or client_ids: give only one of them (client_id for a single client, client_ids for several)"
        )
    if client_id is not None:
        clients = csv_ids("client_id", [positive_id("client_id", client_id)])
    else:
        clients = csv_ids("client_ids", positive_ids("client_ids", client_ids))
    ids = csv_ids("status_ids", positive_ids("status_ids", status_ids))
    keyword = non_empty("query", query)
    created_after = utc_iso("created_since", created_since)
    created_until = utc_iso("created_before", created_before)
    updated_after = utc_iso("updated_since", updated_since)
    updated_until = utc_iso("updated_before", updated_before)
    token = non_empty("cursor", cursor)
    size = clamp_page_size(page_size)
    page = await client_of(ctx).get_page(
        LIST_OP,
        query={
            "ClientIds": clients,
            "StatusIds": ids,
            "Query": keyword,
            "CreatedSince": created_after,
            "CreatedBefore": created_until,
            "UpdatedSince": updated_after,
            "UpdatedBefore": updated_until,
        },
        page_size=size,
        cursor=token,
        tool="list_contacts",
    )
    return paged_result(
        page,
        {
            "client_id": client_id,
            "client_ids": client_ids,
            "status_ids": status_ids,
            "query": keyword,
            "created_since": created_after,
            "created_before": created_until,
            "updated_since": updated_after,
            "updated_before": updated_until,
        },
    )


@gorelo_tool(toolset="core", kind="read", ops=[GET_OP], field_map=GET_FIELD_MAP)
async def get_contact(
    ctx: Context,
    contact_id: Annotated[StrictId, Field(description="Contact id (list_contacts).")],
) -> dict:
    """Get one contact by id; returns Gorelo's contact record (SecondaryEmail is never included)."""
    cid = positive_id("contact_id", contact_id)
    record = await client_of(ctx).get_one(GET_OP, path_params={"contactId": cid}, tool="get_contact")
    return expect_object(record, GET_OP, tool="get_contact")


@gorelo_tool(toolset="core", kind="write", ops=[CREATE_OP], field_map=CREATE_FIELD_MAP)
async def create_contact(
    ctx: Context,
    client_id: Annotated[StrictId, Field(description="Client id (list_clients).")],
    first_name: Annotated[str, Field(description="First name.")],
    last_name: Annotated[str, Field(description="Last name.")],
    primary_email: Annotated[str, Field(description="Primary email address.")],
    location_id: Annotated[StrictId | None, Field(description="A location of that client (list_client_locations).")] = None,
    secondary_email: Annotated[list[str] | None, Field(description="Additional email addresses.")] = None,
    mobile_phone: Annotated[str | None, Field(description="National number, no country code.")] = None,
    mobile_phone_country_code: Annotated[
        str | None,
        Field(
            description="ISO region code in capitals (US, CA), not a dial code. Required with mobile_phone."
        ),
    ] = None,
    office_phone: Annotated[str | None, Field(description="National number, no country code.")] = None,
    office_phone_country_code: Annotated[
        str | None,
        Field(
            description="ISO region code in capitals (US, CA), not a dial code. Required with office_phone."
        ),
    ] = None,
    job_title: Annotated[str | None, Field(description="Job title.")] = None,
    department: Annotated[str | None, Field(description="Department.")] = None,
    time_zone: Annotated[str | None, Field(description="IANA time zone, e.g. America/New_York.")] = None,
    description: Annotated[str | None, Field(description="Free-text notes.")] = None,
) -> dict:
    """Create a contact under a client; returns the created contact record.
    Only client_id, first_name, last_name and primary_email are required; nothing else is inferred or defaulted. Check list_contacts first for a duplicate. Side effects: creates a real contact at once; this server cannot delete contacts, so a mistaken one is removed in the Gorelo app.
    """
    cid = positive_id("client_id", client_id)
    non_empty("first_name", first_name)
    non_empty("last_name", last_name)
    non_empty("primary_email", primary_email)
    lid = None if location_id is None else positive_id("location_id", location_id)
    emails = _addresses("secondary_email", secondary_email, allow_empty=False)
    mobile_region = region_code("mobile_phone_country_code", mobile_phone_country_code)
    office_region = region_code("office_phone_country_code", office_phone_country_code)
    _phone_needs_region("mobile_phone", "mobile_phone_country_code", mobile_phone, mobile_region)
    _phone_needs_region("office_phone", "office_phone_country_code", office_phone, office_region)
    body = build_body(
        {
            "client_id": cid,
            "first_name": first_name,
            "last_name": last_name,
            "primary_email": primary_email,
            "location_id": lid,
            "secondary_email": emails,
            "mobile_phone": mobile_phone,
            "mobile_phone_country_code": mobile_region,
            "office_phone": office_phone,
            "office_phone_country_code": office_region,
            "job_title": job_title,
            "department": department,
            "time_zone": time_zone,
            "description": description,
        },
        CREATE_FIELD_MAP,
    )
    data = await client_of(ctx).post(CREATE_OP, json_body=body, tool="create_contact")
    created_id(data, CREATE_OP, tool="create_contact")  # raises (write_unconfirmed) unless Data carries an Id
    return data


def _clear_list(clear_fields: list[str] | None) -> list[str]:
    """clear_fields as a list of distinct, accepted parameter names (a local error otherwise)."""
    if clear_fields is None:
        return []
    if not clear_fields:
        raise ValueError("clear_fields: must not be an empty list; omit it when nothing should be cleared")
    names: list[str] = []
    for name in clear_fields:
        if name in REQUIRED_FIELDS:
            raise ValueError(
                f"clear_fields: {name} cannot be cleared because Gorelo requires it; pass a new value in {name} instead"
            )
        if name == "secondary_email":
            raise ValueError(
                "clear_fields: secondary_email is not cleared through clear_fields; pass secondary_email=[] "
                "to erase the secondary emails"
            )
        if name not in CLEARABLE_FIELDS:
            raise ValueError(f"clear_fields: unknown field {name!r}; accepted: {', '.join(CLEARABLE_FIELDS)}")
        if name not in names:
            names.append(name)
    return names


def _changes(values: dict[str, Any], clear: list[str]) -> dict[str, Any]:
    """The caller's changes as {PascalField: value}; a cleared field is None here (and left out of the body by
    _full_command: nothing sends a null). Local errors name the parameter."""
    checked = dict(values)
    for param, value in values.items():
        if value is None:
            continue
        if param in clear:
            if isinstance(value, str) and not value.strip():
                continue  # a blank value next to clear_fields is only a second way of saying "clear it"
            raise ValueError(f"{param}: cannot be given a value and listed in clear_fields in the same call")
        if param == "location_id":
            checked[param] = positive_id(param, value)
        elif param.endswith("_country_code"):
            checked[param] = region_code(param, value)
        elif isinstance(value, str) and not value.strip():
            if param in REQUIRED_FIELDS:
                raise ValueError(f"{param}: must not be empty; Gorelo requires it and it cannot be cleared")
            raise ValueError(
                f"{param}: must not be empty or whitespace only; to remove the stored value pass "
                f'clear_fields=["{param}"]'
            )
    return build_body(checked, UPDATE_FIELD_MAP, clear=clear, clear_values={param: None for param in CLEARABLE_FIELDS})


# What to say when a required text field of the record is null and the caller gave no value: its parameter and label.
_REQUIRED_LABELS = {"first_name": "first name", "last_name": "last name", "primary_email": "primary email address"}


def _full_command(
    contact_id: int, record: Any, changes: dict[str, Any], addresses: list[str] | None
) -> dict[str, Any]:
    """The complete UpdateContactCommand: the contact as Gorelo holds it, with the caller's changes laid over it.

    PATCH /v1/contacts/{contactId} replaces the whole contact, so every value the contact has must be sent. A value
    it does not have (null in the record, or cleared through clear_fields) is OMITTED, never sent as an explicit
    null: an omitted field is stored as null anyway. FirstName, LastName, ClientId and PrimaryEmail are always sent
    with values. The command carries no contact id: the id is in the path. Refuses (before anything is written) a
    record that lacks a key it would have to copy: that value would be erased.
    """
    record = expect_object(record, GET_OP, tool="update_contact")
    missing = [key for key in ("Id", *COPIED_FIELDS) if key not in changes and key not in record]
    if missing:
        raise GoreloAPIError(
            f"{GET_OP}: the contact record has no {', '.join(missing)}; refusing to send an update built "
            "from it, because PATCH /v1/contacts/{contactId} replaces the whole contact and would erase every value "
            "that could not be copied",
            status=200, op_key=GET_OP, kind="shape",
        )
    if str(record["Id"]) != str(contact_id):
        raise GoreloAPIError(
            f"{GET_OP}: asked for contact {contact_id} but Gorelo returned contact {record['Id']}; refusing to update",
            status=200, op_key=GET_OP, kind="shape",
        )
    merged = {key: changes[key] if key in changes else record[key] for key in COPIED_FIELDS}
    if merged["ClientId"] is None:
        raise ValueError(
            f"contact_id: contact {contact_id} has no client in Gorelo (ClientId is null) and Gorelo's update "
            "requires one. This tool cannot attach a contact to a client; fix it in the Gorelo app"
        )
    for param in REQUIRED_FIELDS:
        if merged[UPDATE_FIELD_MAP[param]] is None:
            raise ValueError(
                f"{param}: contact {contact_id} has no {_REQUIRED_LABELS[param]} in Gorelo and Gorelo's update "
                f"requires one; pass {param} with the value it should have"
            )
    for phone, region in PHONE_PAIRS:
        phone_key, region_key = UPDATE_FIELD_MAP[phone], UPDATE_FIELD_MAP[region]
        touched = phone_key in changes or region_key in changes
        if touched and _filled(merged[phone_key]) and not _filled(merged[region_key]):
            way_out = (
                f"clear {phone} as well, or keep {region}"
                if region_key in changes and changes[region_key] is None
                else f'pass {region}, for example "US" (no default is assumed)'
            )
            raise ValueError(
                f"{region}: {phone} needs an ISO region code (for example US or CA) and contact "
                f"{contact_id} would have none after this update; {way_out}"
            )
    command: dict[str, Any] = {key: value for key, value in merged.items() if value is not None}
    if addresses is not None:
        command["SecondaryEmail"] = addresses
    return command


@gorelo_tool(toolset="core", kind="write", ops=[GET_OP, UPDATE_OP], field_map=UPDATE_TOOL_FIELD_MAP, destructive_hint=True)
async def update_contact(
    ctx: Context,
    contact_id: Annotated[StrictId, Field(description="Contact id (list_contacts).")],
    first_name: Annotated[str | None, Field(description="New first name.")] = None,
    last_name: Annotated[str | None, Field(description="New last name.")] = None,
    primary_email: Annotated[str | None, Field(description="New primary email address.")] = None,
    location_id: Annotated[
        StrictId | None, Field(description="New location of the contact's own client (list_client_locations).")
    ] = None,
    mobile_phone: Annotated[str | None, Field(description="New national number, no country code.")] = None,
    mobile_phone_country_code: Annotated[
        str | None,
        Field(
            description="New ISO region code in capitals (US, CA), not a dial code. Needed with mobile_phone "
            "when none is stored."
        ),
    ] = None,
    office_phone: Annotated[str | None, Field(description="New national number, no country code.")] = None,
    office_phone_country_code: Annotated[
        str | None,
        Field(
            description="New ISO region code in capitals (US, CA), not a dial code. Needed with office_phone "
            "when none is stored."
        ),
    ] = None,
    job_title: Annotated[str | None, Field(description="New job title.")] = None,
    department: Annotated[str | None, Field(description="New department.")] = None,
    time_zone: Annotated[str | None, Field(description="New IANA time zone, e.g. America/New_York.")] = None,
    description: Annotated[str | None, Field(description="New free-text notes.")] = None,
    clear_fields: Annotated[
        list[ClearableField] | None,
        Field(description="Fields to clear: the contact ends up without them. Not with a value for the same field."),
    ] = None,
    secondary_email: Annotated[
        list[str] | None,
        Field(
            description="COMPLETE list after this update; replaces ALL existing ones ([] erases). The API cannot show "
            "them, only the user can see them in the Gorelo app: ask the user."
        ),
    ] = None,
    clear_secondary_email_ok: Annotated[
        StrictBool,
        Field(
            description="True erases ALL secondary emails when secondary_email is omitted. Ask the user first: the API "
            "cannot show them, only the user can see them in the Gorelo app."
        ),
    ] = False,
) -> dict:
    """Update a contact; returns the updated contact record. Pass only the fields to change.
    Gorelo's PATCH replaces the whole contact, so this tool reads it first and keeps what you did not mention (the client cannot change). The API cannot show a contact's secondary emails (only the user can see them in the Gorelo app): ask the user for the complete list, pass it as secondary_email (replaces ALL existing ones; [] erases), or ask the user before passing clear_secondary_email_ok=true. Without either the call is refused before anything is sent. Side effects: overwrites the contact (visible in the app and on tickets); old values are not kept; an app edit made between the read and the write is lost.
    """
    cid = positive_id("contact_id", contact_id)
    clear = _clear_list(clear_fields)
    changes = _changes(
        {
            "first_name": first_name,
            "last_name": last_name,
            "primary_email": primary_email,
            "location_id": location_id,
            "mobile_phone": mobile_phone,
            "mobile_phone_country_code": mobile_phone_country_code,
            "office_phone": office_phone,
            "office_phone_country_code": office_phone_country_code,
            "job_title": job_title,
            "department": department,
            "time_zone": time_zone,
            "description": description,
        },
        clear,
    )
    addresses = _addresses("secondary_email", secondary_email, allow_empty=True)
    if not changes and addresses is None:
        raise ValueError(
            "nothing to update: pass at least one of first_name, last_name, primary_email, location_id, "
            "mobile_phone, mobile_phone_country_code, office_phone, office_phone_country_code, job_title, "
            "department, time_zone, description, secondary_email or clear_fields"
        )
    if addresses is None and clear_secondary_email_ok is not True:
        raise ValueError(
            f"secondary_email: refusing to update contact {cid} until the user has decided what happens to its "
            "secondary emails. Gorelo's PATCH /v1/contacts/{contactId} replaces the whole contact and the API cannot show a "
            "contact's secondary emails (only the user can see them in the Gorelo app), so this update would "
            "erase them. Ask the user first, do not retry on your own. Either ask which complete list of "
            "secondary emails the contact should have afterwards and pass it as secondary_email (it replaces ALL "
            "existing ones; [] erases them), or ask whether erasing all of them is acceptable and only if the "
            "user says yes pass clear_secondary_email_ok=true. Nothing has been sent to Gorelo."
        )
    client = client_of(ctx)
    record = await client.get_one(GET_OP, path_params={"contactId": cid}, tool="update_contact")
    command = _full_command(cid, record, changes, addresses)
    data = await client.patch(UPDATE_OP, path_params={"contactId": cid}, json_body=command, tool="update_contact")
    return expect_object(data, UPDATE_OP, tool="update_contact")
