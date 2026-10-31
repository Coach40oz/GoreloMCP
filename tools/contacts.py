"""Contacts: list, read, create, update and the contact tags (toolset "core").

Tools: list_contacts, get_contact, create_contact, update_contact, list_contact_tags.

Ops owned by this module:

    GET /v1/contacts
    GET /v1/contacts/tags
    GET /v1/contacts/{contactId}
    POST /v1/contacts
    PATCH /v1/contacts/{contactId}

Contract notes (observed on 2026-10-01 and 2026-10-24, see docs/API-OBSERVED-BEHAVIOR.md):

* GET /v1/contacts has both a legacy ClientId and ClientIds. list_contacts only ever sends ClientIds. It also takes LocationIds
  (contract 6c30770dffc0: client location ids, up to 100, keeps contacts based at any of them).
* Tags (contract 6c30770dffc0): CreateContactCommand and UpdateContactCommand take TagIds (ids from GET /v1/contacts/tags, active
  tags only), at most 100, and every id must be an active contact tag or the whole request is a 400 on TagIds. On update the text
  says "Replaces the set; [] clears it; omit to leave it unchanged"; ContactResponse carries TagIds (ascending, [] when none).
  update_contact follows the omit text: it sends TagIds ONLY when the caller gives tag_ids and never re-sends the stored tags
  (a stored tag that has become inactive would make the whole request a 400 on TagIds). tag_ids=[] clears all tags (the same shape
  as secondary_email=[]). create_contact refuses [] (omit it). When tag_ids is given the answer's TagIds is compared with it as a
  set; a difference is a write-unconfirmed shape error.
* GET /v1/contacts StatusIds is 1 Active, 3 Temporary, 4 Archived (contract a7954b24844b, 2026-10-17; 2 Inactive is gone):
  the default leaves Archived out, a supplied list returns only those statuses, deleted contacts are never listed,
  up to 100 values, any other value is a 400. list_contacts checks every item against that scale, and the count,
  before any request. No other contacts tool takes or sets a status.
* POST /v1/contacts accepts the four required fields (ClientId, FirstName, LastName, PrimaryEmail) and
  leaves everything else null, so create_contact infers nothing and defaults nothing.
* The contact id travels in the PATH only. The published UpdateContactCommand (contract e15cb5a18ec2, 2026-10-02)
  has no ContactId field, so update_contact sends none. The update used to be published with the id in the body
  and no id in the path; that form answered 405 live on 2026-10-02 and a live override swapped the path form in
  until Gorelo published it (the history is in spec/live_overrides.json, section retired). The operation is still
  a full-body command (without PrimaryEmail or ClientId it is a 400).
* What PATCH /v1/contacts/{contactId} does with a field that is left out (live, on a test client, 2026-10-24; the
  2026-10-22 spec text says the same): MobilePhone, MobilePhoneCountryCode,
  OfficePhone, OfficePhoneCountryCode, JobTitle, Department, TimeZone and Description are CLEARED (the first probe, of
  2026-10-01 on the retired collection form, wiped the phones and the job title the same way). LocationId and
  SecondaryEmail are KEPT. "LocationId": null also KEEPS the location, so a contact's location cannot be cleared through
  the API at all (clear_fields refuses location_id; location_id only moves the contact). "SecondaryEmail": [] clears the
  secondary emails. The command still requires ClientId, FirstName, LastName and PrimaryEmail (without them it is a 400).
  update_contact therefore reads the contact (GET /v1/contacts/{contactId}), builds the UpdateContactCommand from that
  record plus the caller's changes, and sends it, so that what the caller did not mention is re-sent and not cleared.
  LocationId is still copied from the record (harmless, and it keeps the behaviour explicit). Only the command's own
  fields are copied, never the read-only ones (Id, Alias, Status, CreatedOn, ...). It refuses, before anything is
  written, a record that lacks a key it would have to copy (that value would be erased), that belongs to another
  contact, whose ClientId is null, or whose FirstName, LastName or PrimaryEmail is null and not given by the caller.
* The body never carries an explicit null. A copied value that is null is OMITTED: an omitted field is cleared anyway
  (or kept, for LocationId), and most of these fields are not nullable in the spec. clear_fields works the same way: a
  cleared field is simply not sent. FirstName, LastName, ClientId and PrimaryEmail are always sent with values.
* SecondaryEmail is part of the command but is NOT returned by any read: the API cannot show a contact's secondary
  emails, and only the user can see them (in the Gorelo app; a search by Query on an address finds the contact). Left
  out, they are KEPT (verified live 2026-10-24), so an update that does not pass secondary_email sends no
  SecondaryEmail and loses nothing. secondary_email REPLACES all existing ones and [] erases them; the tool texts say
  that the user sees the list in the Gorelo app, so the model asks the user for the complete list before it changes
  them. Until 2026-10-24 the tool assumed that every update erased them: it refused to run until the caller passed
  secondary_email or, after asking the user, clear_secondary_email_ok=true. Both are gone.
* Gorelo's write endpoints answer with the full contact record (ContactResponse). create_contact and
  update_contact return it as it is. An answer that is not that record (null, {}, false, a list, or on
  create a record without an Id) is refused through created_id / expect_object (kind "shape",
  write_unconfirmed): the write may have been applied, so it must be verified with a read, never repeated.
  There is no read-back fallback: re-reading after a `false` would show the OLD record and look like success.
* Gorelo refuses a region code without its phone number (HTTP 400, code 070101, PropertyName OfficePhone, "OfficePhone is
  required when OfficePhoneCountryCode is provided"; seen live on 2026-10-24, on create and on update), and the same for
  MobilePhone. The Gorelo app can store a region code on a contact that has no such phone, and the full command
  copies every stored field, so ANY update of such a contact (a first_name change too) failed. After the caller's changes
  are merged over the record, update_contact looks at each pair of PHONE_PAIRS whose phone is empty (null or blank) and
  whose region code is filled. A code the caller passed in this call is refused locally, with the one GET as the only
  request. A code that came from the stored record is left OUT of the command, so Gorelo stores none, which is the only
  state it accepts for a contact without that phone (a blank phone is left out with it), and the result carries a `note`
  that names each such parameter and its previous code. The note is built from Gorelo's ANSWER, not from what was sent:
  it says a code was cleared only when the answer shows that field null or blank, and says "left out of the update, but
  Gorelo's answer still shows it" (no error) when the answer still holds the code (a Gorelo answer that is not a usable
  record raises as before, with no note). create_contact refuses a region code passed without its phone, in the same
  words, before any request. A phone still needs its region code in both tools.

There is no delete tool: DELETE /v1/contacts/{contactId} is in FORBIDDEN_OPS.
"""

from typing import Annotated, Any, Literal, get_args

from fastmcp import Context
from pydantic import Field

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

LIST_OP = "GET /v1/contacts"
TAGS_OP = "GET /v1/contacts/tags"
GET_OP = "GET /v1/contacts/{contactId}"
CREATE_OP = "POST /v1/contacts"
UPDATE_OP = "PATCH /v1/contacts/{contactId}"

SINCE_HELP = "ISO 8601 with UTC offset, e.g. 2026-10-01T00:00:00Z."

# snake_case tool parameter -> Gorelo query name, used to name the parameter in Gorelo's errors.
# client_id and client_ids are both sent as ClientIds (never the legacy ClientId).
LIST_FIELD_MAP = {
    "client_id": "ClientIds",
    "client_ids": "ClientIds",
    "location_ids": "LocationIds",
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
    "tag_ids": "TagIds",
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
    "tag_ids": "TagIds",
}

# What update_contact declares as its field_map: the body fields plus the path placeholder, so that a Gorelo error
# about the id names contact_id (build_body is never given contact_id).
UPDATE_TOOL_FIELD_MAP = {**UPDATE_FIELD_MAP, "contact_id": "contactId"}

# Fields update_contact may clear through clear_fields (the contact ends up without them: they are left out of
# the PATCH, and Gorelo clears a phone, job title, department, time zone or description that is left out), and the
# three it can never blank (the command requires them). location_id is not here: Gorelo keeps a LocationId that is left
# out or sent as null (live, 2026-10-24), so a location cannot be cleared. The Literal puts the allowed names in the
# advertised schema, like update_item's clear_fields.
ClearableField = Literal[
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

# The contact status scale of the GET /v1/contacts StatusIds filter (contract a7954b24844b, 2026-10-17): "1 Active, 3
# Temporary, 4 Archived", any other value is a 400. 2 (Inactive) is gone. No other contacts tool takes or sets a status.
CONTACT_STATUS_LABELS = {1: "Active", 3: "Temporary", 4: "Archived"}
CONTACT_STATUS_SCALE = ", ".join(f"{status} {label}" for status, label in CONTACT_STATUS_LABELS.items())
STATUS_IDS_MAX = 100  # "Up to 100 values"
TAG_IDS_MAX = 100  # "At most 100" (the TagIds text of both commands)


def _status(param: str, value: Any) -> int:
    """One contact status: 1 Active, 3 Temporary or 4 Archived (same rules as tools/clients.py _status)."""
    if isinstance(value, int) and not isinstance(value, bool) and -MAX_ID <= value < 1:
        status = int(value)
    else:
        status = positive_id(param, value)
    if status not in CONTACT_STATUS_LABELS:
        raise ValueError(f"{param}: expected {CONTACT_STATUS_SCALE}, got {status}")
    return status


def _statuses(param: str, values: Any) -> list[int] | None:
    """The status_ids filter of list_contacts: at most STATUS_IDS_MAX items, each on the scale, a bad one named by its index."""
    if isinstance(values, (list, tuple)) and values:
        if len(values) > STATUS_IDS_MAX:
            raise ValueError(f"{param}: at most {STATUS_IDS_MAX} values, got {len(values)}")
        return [_status(f"{param}[{index}]", item) for index, item in enumerate(values)]
    return positive_ids(param, values)


def _tags(param: str, values: Any, *, allow_empty: bool) -> list[int] | None:
    """tag_ids: None stays None, at most TAG_IDS_MAX positive ids (a bad one named by its index), [] only if allowed
    (update_contact: it clears every tag; create_contact: omit the parameter)."""
    if values is None:
        return None
    if isinstance(values, (list, tuple)):
        if not values:
            if allow_empty:
                return []
            raise ValueError(f"{param}: must not be an empty list; omit it to create the contact without tags")
        if len(values) > TAG_IDS_MAX:
            raise ValueError(f"{param}: at most {TAG_IDS_MAX} values, got {len(values)}")
    return positive_ids(param, values)


def _tags_answered(answer: dict[str, Any], sent: list[int], op_key: str, tool: str) -> None:
    """Gorelo's answer must show the tags that were sent (as a set; the answer lists them ascending). A TagIds that
    differs is a write-unconfirmed shape error: the write was answered 2xx, so it may have been applied. An answer
    that has no TagIds list is not checked (nothing to compare)."""
    shown = answer.get("TagIds")
    if isinstance(shown, list) and {str(item) for item in shown} != {str(item) for item in sent}:
        raise GoreloAPIError(
            f"{op_key}: Gorelo answered the write but its TagIds differ from the tag_ids that were sent; the write "
            "may have been applied, so verify it with get_contact before repeating it",
            status=200, op_key=op_key, kind="shape", write_unconfirmed=True,
        )


def _filled(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _empty(value: Any) -> bool:
    """A phone number that is not there: null, or text that is blank."""
    return value is None or (isinstance(value, str) and not value.strip())


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


def _region_without_phone(phone_param: str, region_param: str, detail: str = "") -> ValueError:
    """The refusal of a region code that would reach Gorelo without its phone number. create_contact and update_contact
    use the same words; update_contact adds `detail`, which says why the contact would have no such phone."""
    return ValueError(
        f"{region_param}: Gorelo refuses a region code without its phone number; pass {phone_param} too, or leave "
        f"{region_param} out{detail}"
    )


def _region_needs_phone(phone_param: str, region_param: str, phone: str | None, region: str | None) -> None:
    """create_contact: a region code passed without its phone number is a local error naming the region."""
    if region is not None and phone is None:
        raise _region_without_phone(phone_param, region_param)


@gorelo_tool(toolset="core", kind="read", ops=[LIST_OP], field_map=LIST_FIELD_MAP)
async def list_contacts(
    ctx: Context,
    client_id: Annotated[
        StrictId | None, Field(description="Only this client's contacts (list_clients). Not with client_ids.")
    ] = None,
    client_ids: Annotated[
        list[StrictId] | None, Field(description="Only these clients' contacts (list_clients). Not with client_id.")
    ] = None,
    location_ids: Annotated[
        list[StrictId] | None,
        Field(description="Contacts based at any of these locations (list_client_locations), up to 100."),
    ] = None,
    status_ids: Annotated[
        list[StrictId] | None,
        Field(
            description="1 Active, 3 Temporary, 4 Archived, up to 100. Omit it and Archived contacts are left out; give it and only those are returned."
        ),
    ] = None,
    query: Annotated[str | None, Field(description="Matches email addresses, first name and last name.")] = None,
    created_since: Annotated[str | None, Field(description=f"Created at or after; {SINCE_HELP}")] = None,
    created_before: Annotated[str | None, Field(description="Created before.")] = None,
    updated_since: Annotated[str | None, Field(description="Updated at or after.")] = None,
    updated_before: Annotated[str | None, Field(description="Updated before.")] = None,
    page_size: Annotated[int, Field(description="Rows per page, 1 to 200.")] = 200,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous call.")] = None,
) -> dict:
    """List contacts one page per call; returns {items, has_more, next_cursor, total_count, ...}.
    Filter by client_id (one) or client_ids (several), not both; location_ids keeps contacts based at those locations. Archived contacts are left out unless status_ids asks for status 4; deleted contacts are never listed. Rows can be temporary contacts (IsTemporaryContact true, ClientId null) that belong to no client (Gorelo's release notes of 2026-10-09). Rows never include SecondaryEmail: Gorelo does not return it. Paging: pass next_cursor as cursor, same filters, until has_more is false.
    """
    if client_id is not None and client_ids is not None:
        raise ValueError(
            "client_id or client_ids: give only one of them (client_id for a single client, client_ids for several)"
        )
    if client_id is not None:
        clients = csv_ids("client_id", [positive_id("client_id", client_id)])
    else:
        clients = csv_ids("client_ids", positive_ids("client_ids", client_ids))
    locations = csv_ids("location_ids", positive_ids("location_ids", location_ids))
    ids = csv_ids("status_ids", _statuses("status_ids", status_ids))
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
            "LocationIds": locations,
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
            "location_ids": location_ids,
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
    tag_ids: Annotated[
        list[StrictId] | None,
        Field(description="Tag ids (list_contact_tags), at most 100, active tags only."),
    ] = None,
) -> dict:
    """Create a contact under a client; returns the created contact record.
    This tool always creates a contact for a client, never a client-less temporary one. Only client_id, first_name, last_name and primary_email are required; nothing else is inferred or defaulted. Check list_contacts first for a duplicate. Side effects: creates a real contact at once; this server cannot delete contacts, so a mistaken one is removed in the Gorelo app.
    """
    cid = positive_id("client_id", client_id)
    non_empty("first_name", first_name)
    non_empty("last_name", last_name)
    non_empty("primary_email", primary_email)
    lid = None if location_id is None else positive_id("location_id", location_id)
    emails = _addresses("secondary_email", secondary_email, allow_empty=False)
    tags = _tags("tag_ids", tag_ids, allow_empty=False)
    mobile_region = region_code("mobile_phone_country_code", mobile_phone_country_code)
    office_region = region_code("office_phone_country_code", office_phone_country_code)
    _phone_needs_region("mobile_phone", "mobile_phone_country_code", mobile_phone, mobile_region)
    _region_needs_phone("mobile_phone", "mobile_phone_country_code", mobile_phone, mobile_region)
    _phone_needs_region("office_phone", "office_phone_country_code", office_phone, office_region)
    _region_needs_phone("office_phone", "office_phone_country_code", office_phone, office_region)
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
            "tag_ids": tags,
        },
        CREATE_FIELD_MAP,
    )
    data = await client_of(ctx).post(CREATE_OP, json_body=body, tool="create_contact")
    created_id(data, CREATE_OP, tool="create_contact")  # raises (write_unconfirmed) unless Data carries an Id
    if tags is not None:
        _tags_answered(data, tags, CREATE_OP, "create_contact")
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
        if name == "location_id":
            raise ValueError(
                "clear_fields: Gorelo cannot clear a contact's location through the API (leaving it out or sending "
                "null keeps it; verified 2026-10-24); pass location_id to move the contact to another location of its "
                "client, or clear it in the Gorelo app"
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


def _cleared_note(cleared: list[tuple[str, str, str]], answer: dict[str, Any]) -> str:
    """The `note` of an update that left stored region codes out of the command, built from Gorelo's ANSWER and not from
    what was sent: each region parameter with the code it held. `cleared` holds (phone parameter, region parameter,
    previous code) in PHONE_PAIRS order. A code is called cleared only when the answer shows that field null or blank;
    when the answer still shows it (or does not show the field at all) the note says so and points to get_contact, and
    the tool does not raise: the PATCH was answered 2xx with a record.
    """
    gone: list[tuple[str, str, str]] = []
    kept: list[tuple[str, str, str]] = []
    unseen: list[tuple[str, str, str]] = []
    for item in cleared:
        key = UPDATE_FIELD_MAP[item[1]]
        if key not in answer:
            unseen.append(item)
        elif _empty(answer[key]):
            gone.append(item)
        else:
            kept.append(item)

    def names(group: list[tuple[str, str, str]]) -> str:
        return " and ".join(f"{region} (was {code})" for _phone, region, code in group)

    sentences: list[str] = []
    if gone:
        lacks = " and no ".join(phone.replace("_", " ") for phone, _region, _code in gone)
        sentences.append(
            f"{names(gone)} {'was' if len(gone) == 1 else 'were'} cleared because the contact has no {lacks}, and Gorelo "
            "refuses a region code without its phone number."
        )
    for group, shown in ((kept, "still shows"), (unseen, "does not show")):
        if group:
            single = len(group) == 1
            sentences.append(
                f"{names(group)} {'was' if single else 'were'} left out of the update, but Gorelo's answer {shown} "
                f"{'it' if single else 'them'}; check with get_contact."
            )
    return " ".join(sentences)


def _full_command(
    contact_id: int, record: Any, changes: dict[str, Any], addresses: list[str] | None
) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    """The complete UpdateContactCommand: the contact as Gorelo holds it, with the caller's changes laid over it, and
    the region codes it had to leave out.

    PATCH /v1/contacts/{contactId} clears the phones, region codes, job title, department, time zone and description
    it leaves out, so every value the contact has must be sent. A value it does not have (null in the record, or
    cleared through clear_fields) is OMITTED, never sent as an explicit null: an omitted field is cleared anyway. A
    LocationId and the SecondaryEmail list that are left out are KEPT (live, 2026-10-24); the record's LocationId is
    still copied, and SecondaryEmail is sent only when the caller gave it. FirstName, LastName, ClientId and
    PrimaryEmail are always sent with values. The command carries no contact id: the id is in the path. Refuses
    (before anything is written) a record that lacks a key it would have to copy: that value would be erased.

    Gorelo refuses a region code without its phone number, and its app can store one on a contact that has no such
    phone. Once the changes are merged over the record, a pair whose phone is empty (null or blank) and whose region
    code is filled is settled one of two ways. A code the caller passed in this call is a local error. A code that
    came from the stored record is left out of the command (with a blank phone), so that Gorelo stores none, and is
    returned as (phone parameter, region parameter, previous code) for the caller to report.
    """
    record = expect_object(record, GET_OP, tool="update_contact")
    missing = [key for key in ("Id", *COPIED_FIELDS) if key not in changes and key not in record]
    if missing:
        raise GoreloAPIError(
            f"{GET_OP}: the contact record has no {', '.join(missing)}; refusing to send an update built "
            "from it, because Gorelo clears the phone, region code, job title, department, time zone and description "
            "fields that an update leaves out, so a record that lacks one of its fields cannot be copied safely",
            status=200, op_key=GET_OP, kind="shape",
        )
    if str(record["Id"]) != str(contact_id):
        raise GoreloAPIError(
            f"{GET_OP}: asked for contact {contact_id} but Gorelo returned contact {record['Id']}; refusing to update",
            status=200, op_key=GET_OP, kind="shape",
        )
    merged = {key: changes[key] if key in changes else record[key] for key in COPIED_FIELDS}
    if "TagIds" in changes:  # sent only when the caller gave tag_ids; the stored tags are never re-sent (see the module note)
        merged["TagIds"] = changes["TagIds"]
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
    cleared: list[tuple[str, str, str]] = []
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
        if _empty(merged[phone_key]) and _filled(merged[region_key]):
            if changes.get(region_key) is not None:  # the caller passed this code in this very call
                raise _region_without_phone(
                    phone, region, f" (contact {contact_id} would have no {phone} after this update)"
                )
            cleared.append((phone, region, merged[region_key].strip()))
            merged[phone_key] = merged[region_key] = None  # left out below: Gorelo stores neither
    command: dict[str, Any] = {key: value for key, value in merged.items() if value is not None}
    if addresses is not None:
        command["SecondaryEmail"] = addresses
    return command, cleared


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
            description="COMPLETE list; replaces ALL secondary emails ([] erases them). The API cannot show them but the "
            "user sees them in the Gorelo app: to change them, ask the user for the complete list. Omit it to keep them."
        ),
    ] = None,
    tag_ids: Annotated[
        list[StrictId] | None,
        Field(
            description="COMPLETE tag list (list_contact_tags ids, at most 100); replaces ALL tags, [] removes them all. Omit it to keep them."
        ),
    ] = None,
) -> dict:
    """Update a contact; returns the updated contact record. Pass only the fields to change.
    Gorelo clears the phones, job title, department, time zone and description an update leaves out, so this tool reads the contact and re-sends them. Location and secondary emails you do not send are kept. The API cannot show the secondary emails, the user sees them in the Gorelo app: to change them ask the user for the complete list; secondary_email REPLACES all ([] erases). The location can be moved, not cleared. A stored region code with no phone is left out; the result's `note` names it. Side effects: overwrites the contact; old values are not kept; an app edit made between the read and the write is lost.
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
    tags = _tags("tag_ids", tag_ids, allow_empty=True)
    if tags is not None:
        changes["TagIds"] = tags
    if not changes and addresses is None:
        raise ValueError(
            "nothing to update: pass at least one of first_name, last_name, primary_email, location_id, "
            "mobile_phone, mobile_phone_country_code, office_phone, office_phone_country_code, job_title, "
            "department, time_zone, description, secondary_email, tag_ids or clear_fields"
        )
    client = client_of(ctx)
    record = await client.get_one(GET_OP, path_params={"contactId": cid}, tool="update_contact")
    command, cleared = _full_command(cid, record, changes, addresses)
    data = await client.patch(UPDATE_OP, path_params={"contactId": cid}, json_body=command, tool="update_contact")
    result = expect_object(data, UPDATE_OP, tool="update_contact")  # raises first when the answer is not a record: no note
    if tags is not None:
        _tags_answered(result, tags, UPDATE_OP, "update_contact")
    return {**result, "note": _cleared_note(cleared, result)} if cleared else result


@gorelo_tool(toolset="core", kind="read", ops=[TAGS_OP])
async def list_contact_tags(ctx: Context) -> dict:
    """List the tags a contact can be given (not paged; active tags only); returns {items, count}.
    A row's Id is a tag_ids value of create_contact and update_contact; a contact's TagIds (get_contact) lists the tags it has. Rows hold Id, Name and Description.
    Resolve ids first: none.
    Side effects: None (read-only).
    """
    items = await client_of(ctx).get_list(TAGS_OP, tool="list_contact_tags")
    return list_result(items)
