"""Catalog items, categories and taxes (toolset "billing").

Tools:

    list_item_categories  read         GET /v1/items/categories
    list_taxes            read         GET /v1/taxes
    list_items            read         GET /v1/items
    get_item              read         GET /v1/items/{itemId}
    create_item           write        POST /v1/items, GET /v1/items/{itemId}
    update_item           write        PATCH /v1/items/{itemId}, GET /v1/items/{itemId}
    delete_item           destructive  DELETE /v1/items/{itemId}

Ops owned by this module:

    GET /v1/items
    GET /v1/items/{itemId}
    POST /v1/items
    PATCH /v1/items/{itemId}
    DELETE /v1/items/{itemId}
    GET /v1/items/categories
    GET /v1/taxes

An item is a product (type 1) or a bundle (type 2, made of products). The type is fixed when the item
is created: update_item never sends TypeId, because Gorelo answers 400 if that field is present at all.
POST and PATCH answer with only the item Id, so create_item and update_item read the record back with
reread_after_write. update_item is the one tool here that can clear stored data: free text and ids only
through clear_fields (an empty string or 0 on the wire), a bundle's sub-items only through clear_fields
too, and name, number, unit_cost, unit_price and status never.

Money (contract e15cb5a18ec2): a unit price may be negative, for a discount or credit line, on create and on update,
so unit_price accepts any finite number. A unit cost may not: the update text says a negative UnitCost is a 400 and the
create text says nothing to the contrary, so unit_cost must be 0 or more on both, and a negative one is refused here,
naming unit_cost, before anything is sent.
"""

import math
from collections.abc import Mapping
from typing import Annotated, Any, Literal, get_args

from fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field

from tools._common import (
    StrictBool,
    StrictId,
    build_body,
    clamp_page_size,
    client_of,
    created_id,
    csv_ids,
    expect_object,
    gorelo_tool,
    guid,
    list_result,
    non_empty,
    paged_result,
    positive_id,
    positive_ids,
    reread_after_write,
    require_confirm,
    utc_iso,
)

LIST_OP = "GET /v1/items"
GET_OP = "GET /v1/items/{itemId}"
CREATE_OP = "POST /v1/items"
UPDATE_OP = "PATCH /v1/items/{itemId}"
DELETE_OP = "DELETE /v1/items/{itemId}"
CATEGORIES_OP = "GET /v1/items/categories"
TAXES_OP = "GET /v1/taxes"

ITEM_TYPE_IDS = {"product": 1, "bundle": 2}
ITEM_STATUS_IDS = {"active": 1, "archived": 2}
QUERY_MAX_CHARS = 200

# {tool param: Gorelo query name} for every filter of GET /v1/items. Like the body maps below it also
# turns the PropertyName of a Gorelo error back into the snake_case param in error messages.
FILTER_FIELDS = {
    "type": "TypeIds",
    "category_ids": "CategoryIds",
    "subcategory_ids": "SubcategoryIds",
    "client_ids": "ClientIds",
    "skus": "Skus",
    "vendors": "Vendors",
    "part_numbers": "PartNumbers",
    "status": "StatusIds",
    "query": "Query",
    "created_since": "CreatedSince",
    "created_before": "CreatedBefore",
    "updated_since": "UpdatedSince",
    "updated_before": "UpdatedBefore",
}
LIST_FIELD_MAP = {**FILTER_FIELDS, "page_size": "PageSize", "cursor": "Cursor"}

# Body fields that CreateItemCommand and UpdateItemCommand share.
ITEM_BODY = {
    "name": "Name",
    "number": "Number",
    "description": "Description",
    "category_id": "CategoryId",
    "subcategory_id": "SubcategoryId",
    "client_id": "ClientId",
    "location_id": "LocationId",
    "vendor": "Vendor",
    "unit_price": "UnitPrice",
    "tax_id": "TaxId",
    "external_product_id": "ExternalProductId",
    "sku": "Sku",
    "part_number": "PartNumber",
    "manufacturer": "Manufacturer",
    "unit_cost": "UnitCost",
    "sub_items": "SubItems",
    "show_sub_items_on_invoice": "ShowSubItemsOnInvoice",
    "show_sub_item_descriptions_on_invoice": "ShowSubItemDescriptionsOnInvoice",
}
CREATE_FIELDS = {"type": "TypeId", **ITEM_BODY}
# There is deliberately no TypeId here: a PATCH that carries it, even as null, is a 400.
UPDATE_FIELDS = {**ITEM_BODY, "status": "StatusId"}

PRODUCT_ONLY = ("sku", "part_number", "manufacturer", "unit_cost")
BUNDLE_ONLY = ("sub_items", "show_sub_items_on_invoice", "show_sub_item_descriptions_on_invoice")

# What update_item may clear, and what Gorelo expects on the wire for each: "" for free text (the
# build_body default), 0 for ids, [] for the sub-item list. Everything else is not clearable.
ClearableField = Literal[
    "description",
    "sku",
    "part_number",
    "manufacturer",
    "vendor",
    "external_product_id",
    "category_id",
    "subcategory_id",
    "client_id",
    "location_id",
    "tax_id",
    "sub_items",
]
CLEARABLE = get_args(ClearableField)
CLEAR_VALUES = {
    "category_id": 0,
    "subcategory_id": 0,
    "client_id": 0,
    "location_id": 0,
    "tax_id": 0,
    "sub_items": [],
}


class SubItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    quantity: Annotated[float, Field(gt=0)]


# --------------------------------------------------------------------------
# Local validation helpers (each raises ValueError naming the snake_case param)
# --------------------------------------------------------------------------


def _pick(param: str, value: str | None, table: Mapping[str, int]) -> int | None:
    """The Gorelo id for a named choice. An unknown name is an error, never a silently dropped filter."""
    if value is None:
        return None
    if value not in table:
        raise ValueError(f"{param}: must be one of {', '.join(repr(name) for name in table)}, got {value!r}")
    return table[value]


def _search_text(param: str, value: str | None) -> str | None:
    value = non_empty(param, value)
    if value is not None and len(value) > QUERY_MAX_CHARS:
        raise ValueError(f"{param}: at most {QUERY_MAX_CHARS} characters (Gorelo rejects longer keywords)")
    return value


def _to_query(filters: dict[str, Any]) -> dict[str, Any]:
    """Gorelo's query names; a list becomes one comma separated value (csv_ids names the param on error).

    Unset filters stay in the dict as None on purpose: the client checks every NAME before it drops them.
    """
    return {
        FILTER_FIELDS[param]: csv_ids(param, value) if isinstance(value, (list, tuple)) else value
        for param, value in filters.items()
    }


def _amount(param: str, value: Any, *, may_be_negative: bool = False) -> float | int | None:
    """A unit price or a unit cost: a finite number, never a bool. None stays None (not given).

    A unit PRICE may be negative, for a discount or credit line (UnitPrice of POST /v1/items and PATCH
    /v1/items/{itemId}, contract e15cb5a18ec2), so the price call sites pass may_be_negative=True. A unit COST may
    not: Gorelo answers 400 for a negative UnitCost on update (the create text is silent, so it is refused there
    too), so the default (0 or more) is the cost rule and a negative cost is refused here, naming the param, before
    anything is sent.
    """
    if value is None:
        return None
    problem = (
        f"{param}: must be a finite number (it may be negative, for a discount or credit line)"
        if may_be_negative
        else f"{param}: must be a number of 0 or more"
    )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(problem)
    try:
        finite = math.isfinite(value)
    except OverflowError:  # an int too large for a float
        finite = False
    if not finite or (value < 0 and not may_be_negative):
        raise ValueError(problem)
    return value


def _opt_id(param: str, value: Any) -> int | None:
    """An id that may be left out: None stays None, anything else must be a positive whole number."""
    return None if value is None else positive_id(param, value)


def _changed_id(param: str, value: Any) -> int | None:
    """A Gorelo id on update. 0 is how Gorelo clears an id, which only clear_fields may ask for."""
    if value == 0 and not isinstance(value, bool):
        raise ValueError(f"{param}: 0 is not an id; to remove it pass clear_fields=[{param!r}]")
    return _opt_id(param, value)


def _required_text(param: str, value: Any) -> str:
    """Text that must be given and must not be blank."""
    if value is None:
        raise ValueError(f"{param}: is required")
    return non_empty(param, value)


def _fixed_text(param: str, value: Any) -> Any:
    """Name and number: Gorelo cannot clear them, so a blank is an error whatever the tool offers."""
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{param}: must not be blank; Gorelo cannot clear it, omit it to keep the current value")
    return value


def _clearable_text(param: str, value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{param}: a blank value is not accepted; to clear it pass clear_fields=[{param!r}]")
    return value


def _sub_items(param: str, value: Any, *, if_empty: str) -> list[dict[str, Any]] | None:
    """The SubItems body array [{ItemId, Quantity}] from the tool's sub_items, or None when not given."""
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{param}: expected a list of objects with item_id and quantity")
    if not value:
        raise ValueError(f"{param}: {if_empty}")
    payload: list[dict[str, Any]] = []
    for position, entry in enumerate(value):
        where = f"{param}[{position}]"
        data = entry.model_dump() if isinstance(entry, BaseModel) else entry
        if not isinstance(data, Mapping):
            raise ValueError(f"{where}: must be an object with item_id and quantity")
        unknown = sorted(str(key) for key in data if key not in ("item_id", "quantity"))
        if unknown:
            raise ValueError(f"{where}: unknown field(s) {', '.join(unknown)}; allowed: item_id, quantity")
        missing = [key for key in ("item_id", "quantity") if data.get(key) is None]
        if missing:
            raise ValueError(f"{where}: missing {', '.join(missing)}")
        quantity = data["quantity"]
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, (int, float))
            or not math.isfinite(quantity)
            or quantity <= 0
        ):
            raise ValueError(f"{where}.quantity: must be a number greater than 0")
        payload.append({"ItemId": guid(f"{where}.item_id", data["item_id"]), "Quantity": float(quantity)})
    return payload


def _refuse_wrong_type(item_type: str, given: dict[str, Any], names: tuple[str, ...], only: str) -> None:
    """Fields that belong to the other item type are local errors naming every offending param."""
    offending = [name for name in names if given.get(name) is not None]
    if offending:
        raise ValueError(
            f"{', '.join(offending)}: only valid for {only}, but type is {item_type!r}; "
            "Gorelo rejects it with a 400, so nothing was sent. Omit it."
        )


# --------------------------------------------------------------------------
# Lookups: categories and taxes
# --------------------------------------------------------------------------


@gorelo_tool(toolset="billing", kind="read", ops=[CATEGORIES_OP])
async def list_item_categories(ctx: Context) -> dict:
    """List item categories with their Subcategories; resolves category_id and subcategory_id.
    A subcategory id can equal a category id: read a subcategory with its category.
    """
    items = await client_of(ctx).get_list(CATEGORIES_OP, tool="list_item_categories")
    return list_result(items)


@gorelo_tool(toolset="billing", kind="read", ops=[TAXES_OP])
async def list_taxes(ctx: Context) -> dict:
    """List tax rates (unpaged); resolves tax_id."""
    items = await client_of(ctx).get_list(TAXES_OP, tool="list_taxes")
    return list_result(items)


# --------------------------------------------------------------------------
# list_items and get_item
# --------------------------------------------------------------------------


@gorelo_tool(toolset="billing", kind="read", ops=[LIST_OP], field_map=LIST_FIELD_MAP)
async def list_items(
    ctx: Context,
    type: Annotated[Literal["product", "bundle"] | None, Field(description="Omit for both.")] = None,
    category_ids: Annotated[list[StrictId] | None, Field(description="From list_item_categories.")] = None,
    subcategory_ids: Annotated[list[StrictId] | None, Field(description="From list_item_categories.")] = None,
    client_ids: Annotated[list[StrictId] | None, Field(description="From list_clients.")] = None,
    skus: Annotated[list[str] | None, Field(description="Exact; no commas in a value.")] = None,
    vendors: Annotated[list[str] | None, Field(description="Exact; no commas in a value.")] = None,
    part_numbers: Annotated[list[str] | None, Field(description="Exact; no commas in a value.")] = None,
    status: Annotated[Literal["active", "archived"] | None, Field(description="Omit for both.")] = None,
    query: Annotated[str | None, Field(description="Name or description keyword.")] = None,
    created_since: Annotated[str | None, Field(description="At or after.")] = None,
    created_before: Annotated[str | None, Field(description="At or before.")] = None,
    updated_since: Annotated[str | None, Field(description="At or after.")] = None,
    updated_before: Annotated[str | None, Field(description="At or before.")] = None,
    page_size: Annotated[int, Field(description="1-200, clamped.")] = 50,
    cursor: Annotated[
        str | None, Field(description="next_cursor from the previous call; same filters; repeat until has_more is false.")
    ] = None,
) -> dict:
    """List catalog items (products and bundles), one page at a time. Only get_item returns a bundle's
    SubItems; labor items never appear.
    """
    filters = {
        "type": type,
        "category_ids": positive_ids("category_ids", category_ids),
        "subcategory_ids": positive_ids("subcategory_ids", subcategory_ids),
        "client_ids": positive_ids("client_ids", client_ids),
        "skus": skus,
        "vendors": vendors,
        "part_numbers": part_numbers,
        "status": status,
        "query": _search_text("query", query),
        "created_since": utc_iso("created_since", created_since),
        "created_before": utc_iso("created_before", created_before),
        "updated_since": utc_iso("updated_since", updated_since),
        "updated_before": utc_iso("updated_before", updated_before),
    }
    wire = _to_query(
        {
            **filters,
            "type": _pick("type", type, ITEM_TYPE_IDS),
            "status": _pick("status", status, ITEM_STATUS_IDS),
        }
    )
    page = await client_of(ctx).get_page(
        LIST_OP,
        query=wire,
        page_size=clamp_page_size(page_size),
        cursor=non_empty("cursor", cursor),
        tool="list_items",
    )
    return paged_result(page, filters)


@gorelo_tool(toolset="billing", kind="read", ops=[GET_OP], field_map={"item_id": "ItemId"})
async def get_item(
    ctx: Context,
    item_id: Annotated[str, Field(description="GUID (list_items).")],
) -> dict:
    """Get one catalog item by Id, with a bundle's SubItems (null for a product)."""
    data = await client_of(ctx).get_one(
        GET_OP, path_params={"itemId": guid("item_id", item_id)}, tool="get_item"
    )
    return expect_object(data, GET_OP, tool="get_item")


# --------------------------------------------------------------------------
# create_item
# --------------------------------------------------------------------------


@gorelo_tool(toolset="billing", kind="write", ops=[CREATE_OP, GET_OP], field_map=CREATE_FIELDS)
async def create_item(
    ctx: Context,
    type: Annotated[Literal["product", "bundle"], Field(description="Cannot be changed later.")],
    name: Annotated[str, Field(description="Display name.")],
    number: Annotated[str | None, Field(description="Product number.")] = None,
    description: Annotated[str | None, Field(description="Free text.")] = None,
    category_id: Annotated[StrictId | None, Field(description="From list_item_categories.")] = None,
    subcategory_id: Annotated[
        StrictId | None, Field(description="From list_item_categories; send category_id too.")
    ] = None,
    client_id: Annotated[StrictId | None, Field(description="From list_clients; omit for catalog-wide.")] = None,
    location_id: Annotated[
        StrictId | None, Field(description="From list_client_locations; send client_id too.")
    ] = None,
    vendor: Annotated[str | None, Field(description="Vendor name.")] = None,
    unit_price: Annotated[float | None, Field(description="May be negative, for a discount or credit line.")] = None,
    tax_id: Annotated[StrictId | None, Field(description="From list_taxes.")] = None,
    external_product_id: Annotated[str | None, Field(description="Accounting system id.")] = None,
    sku: Annotated[str | None, Field(description="Products only.")] = None,
    part_number: Annotated[str | None, Field(description="Products only.")] = None,
    manufacturer: Annotated[str | None, Field(description="Products only.")] = None,
    unit_cost: Annotated[float | None, Field(description="Products only, 0 or more.")] = None,
    sub_items: Annotated[
        list[SubItem] | None,
        Field(
            description="Bundles only, required: [{item_id (a product from list_items), quantity}]. "
            "A bundle's cost comes from its sub-items."
        ),
    ] = None,
    show_sub_items_on_invoice: Annotated[bool | None, Field(description="Bundles only.")] = None,
    show_sub_item_descriptions_on_invoice: Annotated[bool | None, Field(description="Bundles only.")] = None,
) -> dict:
    """Create a product or a bundle and return it.
    Side effects: creates a real catalog item that contracts and invoices can use. To undo, archive it with
    update_item (status=archived, always possible), or delete_item, when deletes are enabled.
    A failed re-read returns {Id, warning}: the item exists, do not create it again.
    """
    if type not in ITEM_TYPE_IDS:
        raise ValueError(f"type: must be one of {', '.join(repr(name) for name in ITEM_TYPE_IDS)}, got {type!r}")
    given = {
        "sku": sku,
        "part_number": part_number,
        "manufacturer": manufacturer,
        "unit_cost": unit_cost,
        "sub_items": sub_items,
        "show_sub_items_on_invoice": show_sub_items_on_invoice,
        "show_sub_item_descriptions_on_invoice": show_sub_item_descriptions_on_invoice,
    }
    if type == "product":
        _refuse_wrong_type(type, given, BUNDLE_ONLY, "bundles (type='bundle')")
    else:
        _refuse_wrong_type(type, given, PRODUCT_ONLY, "products (type='product')")
    body = build_body(
        {
            "type": ITEM_TYPE_IDS[type],
            "name": _required_text("name", name),
            "number": non_empty("number", number),
            "description": non_empty("description", description),
            "category_id": _opt_id("category_id", category_id),
            "subcategory_id": _opt_id("subcategory_id", subcategory_id),
            "client_id": _opt_id("client_id", client_id),
            "location_id": _opt_id("location_id", location_id),
            "vendor": non_empty("vendor", vendor),
            "unit_price": _amount("unit_price", unit_price, may_be_negative=True),
            "tax_id": _opt_id("tax_id", tax_id),
            "external_product_id": non_empty("external_product_id", external_product_id),
            "sku": non_empty("sku", sku),
            "part_number": non_empty("part_number", part_number),
            "manufacturer": non_empty("manufacturer", manufacturer),
            "unit_cost": _amount("unit_cost", unit_cost),
            "sub_items": _sub_items(
                "sub_items",
                sub_items,
                if_empty="a bundle needs at least one sub-item, each {item_id, quantity}",
            ),
            "show_sub_items_on_invoice": show_sub_items_on_invoice,
            "show_sub_item_descriptions_on_invoice": show_sub_item_descriptions_on_invoice,
        },
        CREATE_FIELDS,
    )
    if type == "bundle" and "SubItems" not in body:
        raise ValueError(
            "sub_items: a bundle needs at least one sub-item (the products it is made of), each {item_id, quantity}"
        )
    written = await client_of(ctx).post(CREATE_OP, json_body=body, tool="create_item")
    item_id = created_id(written, CREATE_OP, tool="create_item")
    return await reread_after_write(
        ctx, GET_OP, path_params={"itemId": item_id}, tool="create_item", written_id=item_id
    )


# --------------------------------------------------------------------------
# update_item
# --------------------------------------------------------------------------


@gorelo_tool(
    toolset="billing",
    kind="write",
    ops=[UPDATE_OP, GET_OP],
    field_map={**UPDATE_FIELDS, "item_id": "ItemId"},
    destructive_hint=True,
)
async def update_item(
    ctx: Context,
    item_id: Annotated[str, Field(description="GUID (list_items).")],
    name: Annotated[str | None, Field(description="New name.")] = None,
    number: Annotated[str | None, Field(description="New product number.")] = None,
    description: Annotated[str | None, Field(description="New description.")] = None,
    sku: Annotated[str | None, Field(description="Products only.")] = None,
    part_number: Annotated[str | None, Field(description="Products only.")] = None,
    manufacturer: Annotated[str | None, Field(description="Products only.")] = None,
    vendor: Annotated[str | None, Field(description="New vendor.")] = None,
    external_product_id: Annotated[str | None, Field(description="Accounting system id.")] = None,
    category_id: Annotated[StrictId | None, Field(description="From list_item_categories.")] = None,
    subcategory_id: Annotated[
        StrictId | None, Field(description="From list_item_categories; of the item's category.")
    ] = None,
    client_id: Annotated[StrictId | None, Field(description="From list_clients.")] = None,
    location_id: Annotated[
        StrictId | None, Field(description="From list_client_locations; of the item's client.")
    ] = None,
    tax_id: Annotated[StrictId | None, Field(description="From list_taxes.")] = None,
    unit_cost: Annotated[float | None, Field(description="Products only, 0 or more.")] = None,
    unit_price: Annotated[float | None, Field(description="May be negative, for a discount or credit line.")] = None,
    status: Annotated[Literal["active", "archived"] | None, Field(description="Archive or reactivate.")] = None,
    sub_items: Annotated[
        list[SubItem] | None,
        Field(
            description="Bundles only: the COMPLETE new list [{item_id (a product from list_items), quantity}]; "
            "replaces the list and Gorelo recomputes the bundle's UnitCost."
        ),
    ] = None,
    show_sub_items_on_invoice: Annotated[bool | None, Field(description="Bundles only.")] = None,
    show_sub_item_descriptions_on_invoice: Annotated[bool | None, Field(description="Bundles only.")] = None,
    clear_fields: Annotated[
        list[ClearableField] | None,
        Field(description="Text emptied, ids unset, sub_items empties a bundle."),
    ] = None,
) -> dict:
    """Change fields of a catalog item (partial update) and return it. The type never changes; values are
    removed only through clear_fields. At least one change.
    Side effects: overwrites stored values, changing what future contract and invoice lines pick up.
    A failed re-read returns {Id, warning}: the change WAS applied, do not repeat it.
    """
    item_guid = guid("item_id", item_id)
    clear: list[str] | None = non_empty("clear_fields", clear_fields)
    for field in clear or []:
        if field not in CLEARABLE:
            raise ValueError(
                f"clear_fields: {field!r} cannot be cleared; allowed: {', '.join(CLEARABLE)}"
            )
    values = {
        "name": _fixed_text("name", name),
        "number": _fixed_text("number", number),
        "unit_cost": _amount("unit_cost", unit_cost),
        "unit_price": _amount("unit_price", unit_price, may_be_negative=True),
        "status": _pick("status", status, ITEM_STATUS_IDS),
        "sub_items": _sub_items(
            "sub_items",
            sub_items,
            if_empty="an empty list is not accepted here; to remove every sub-item pass "
            "clear_fields=['sub_items'], otherwise give the complete new list",
        ),
        "show_sub_items_on_invoice": show_sub_items_on_invoice,
        "show_sub_item_descriptions_on_invoice": show_sub_item_descriptions_on_invoice,
    }
    for param, text in (
        ("description", description),
        ("sku", sku),
        ("part_number", part_number),
        ("manufacturer", manufacturer),
        ("vendor", vendor),
        ("external_product_id", external_product_id),
    ):
        values[param] = _clearable_text(param, text)
    for param, identifier in (
        ("category_id", category_id),
        ("subcategory_id", subcategory_id),
        ("client_id", client_id),
        ("location_id", location_id),
        ("tax_id", tax_id),
    ):
        values[param] = _changed_id(param, identifier)
    body = build_body(values, UPDATE_FIELDS, clear=clear, clear_values=CLEAR_VALUES)
    if not body:
        raise ValueError(
            "no change requested: give at least one field to change, or name fields to remove in clear_fields"
        )
    written = await client_of(ctx).patch(UPDATE_OP, path_params={"itemId": item_guid}, json_body=body, tool="update_item")
    expect_object(written, UPDATE_OP, tool="update_item")
    return await reread_after_write(
        ctx, GET_OP, path_params={"itemId": item_guid}, tool="update_item", written_id=item_guid
    )


# --------------------------------------------------------------------------
# delete_item
# --------------------------------------------------------------------------


@gorelo_tool(
    toolset="billing", kind="destructive", ops=[DELETE_OP], field_map={"item_id": "ItemId"}
)
async def delete_item(
    ctx: Context,
    item_id: Annotated[str, Field(description="GUID (list_items).")],
    confirm: Annotated[StrictBool, Field(description="Must be true; only after the user approves.")] = False,
) -> dict:
    """Delete one catalog item by Id. Gorelo refuses (409) while it is a sub-item of a
    bundle or billed on a contract (the answer lists every blocker): remove it there first. An archived item can be
    deleted. Deleting a bundle keeps its sub-items.
    Ask the user first; needs confirm=true.
    """
    item_guid = guid("item_id", item_id)
    require_confirm(
        confirm,
        action=f"delete catalog item {item_guid}",
        effect=(
            "The item is removed from the catalog; Gorelo refuses (409) while it is a sub-item of a bundle or "
            "billed on a contract. Nothing has been sent to Gorelo."
        ),
    )
    data = await client_of(ctx).delete(DELETE_OP, path_params={"itemId": item_guid}, tool="delete_item")
    return expect_object(data, DELETE_OP, tool="delete_item")
