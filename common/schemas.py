"""Two schema layers for material-order logging.

Layer 1 (`Extraction`) is what the LLM fills in from speech, and it covers the
Items section of the New Order screen and nothing else: itemName, quantity, uom.
Layer 2 (`MaterialOrderDto`) is the POST /starship/v1/material-order body.
contractorId, clientId, dealerId and orderType come from the screen the user is
already on — never from speech.

Validators mirror Validator/MaterialOrderValidator.go, including message text.
Go's `len()` counts BYTES.
"""

from __future__ import annotations

from typing import ClassVar, Literal, Optional

from pydantic import BaseModel, Field

ORDER_TYPE_MAT_ORDER = "MAT_ORDER"
ORDER_TYPE_MAT_LIST = "MAT_LIST"
ORDER_TYPES = (ORDER_TYPE_MAT_ORDER, ORDER_TYPE_MAT_LIST)

MAX_ITEM_NAME_BYTES = 255
MAX_UOM_BYTES = 30


class ValidationFailure(Exception):
    """Mirrors a rejection the Go validator would have produced."""


class SpokenItem(BaseModel):
    """One line item heard in a turn. Nested objects are allowed; dict/Any are not."""

    item_name: str = Field(description="Material name, e.g. Fevicol SH, cement.")
    quantity: int = Field(description="Whole units only, greater than 0.")
    uom: str = Field(
        description="Unit of measure as spoken: bag, bori, tin, kg, litre, piece, …"
    )


class Extraction(BaseModel):
    """The LLM's report of one spoken turn. Item rows only, nothing else."""

    entity: Literal["ITEMS", "NONE"] = Field(
        description="ITEMS if the speaker named any material. "
                    "NONE for chatter with no items."
    )
    confidence: float = Field(
        description="0.0 to 1.0 for the item rows you filled."
    )
    items: list[SpokenItem] = Field(
        default_factory=list,
        description="Line items heard THIS turn. Empty if none. Do not repeat "
                    "items already in the in-progress list unless correcting.",
    )
    is_correction: bool = Field(
        default=False,
        description="True if this turn replaces the item list rather than "
                    "adding to it.",
    )
    notes: Optional[str] = Field(
        default=None,
        description="One short line on ambiguity, e.g. a missing uom. "
                    "Null if clean.",
    )

    def filled_fields(self) -> dict[str, object]:
        return {"items": list(self.items)} if self.items else {}


class MaterialOrderDto(BaseModel):
    """Mirrors RequestDtos.CreateMaterialOrderRequestDto.

    Only `orderItems` is ever filled from speech. `amount` and `itemSummary`
    exist because the Go DTO has them, and stay at their defaults here.
    """

    contractorId: int
    dealerId: Optional[int] = None
    clientId: Optional[int] = None
    orderType: Optional[str] = None
    amount: float = 0.0
    itemSummary: Optional[str] = None
    orderItems: list[dict] = Field(default_factory=list)

    entity_label: ClassVar[str] = "MATERIAL_ORDER"

    def missing_required(self) -> list[str]:
        missing = []
        if self.orderType not in ORDER_TYPES:
            missing.append("orderType")
        if not self.orderItems:
            missing.append("orderItems")
        else:
            for i, item in enumerate(self.orderItems):
                if not str(item.get("itemName") or "").strip():
                    missing.append(f"orderItems[{i}].itemName")
                if int(item.get("quantity") or 0) <= 0:
                    missing.append(f"orderItems[{i}].quantity")
                if not str(item.get("uom") or "").strip():
                    missing.append(f"orderItems[{i}].uom")
        return missing

    def validate_for_api(self) -> None:
        if self.contractorId == 0:
            raise ValidationFailure("contractorId is required and must be greater than 0")
        if self.orderType not in ORDER_TYPES:
            raise ValidationFailure("orderType must be one of: MAT_ORDER, MAT_LIST")
        if not self.orderItems:
            raise ValidationFailure("either orderItems or orderUrls is required")
        if self.amount < 0:
            raise ValidationFailure("amount must be non-negative")
        for i, item in enumerate(self.orderItems):
            name = str(item.get("itemName") or "").strip()
            if not name:
                raise ValidationFailure(f"orderItems[{i}].itemName is required")
            if len(name.encode("utf-8")) > MAX_ITEM_NAME_BYTES:
                raise ValidationFailure(f"orderItems[{i}].itemName must not exceed 255 characters")
            qty = int(item.get("quantity") or 0)
            if qty <= 0:
                raise ValidationFailure(
                    f"orderItems[{i}].quantity is required and must be greater than 0"
                )
            uom = str(item.get("uom") or "").strip()
            if not uom:
                raise ValidationFailure(f"orderItems[{i}].uom is required")
            if len(uom.encode("utf-8")) > MAX_UOM_BYTES:
                raise ValidationFailure(f"orderItems[{i}].uom must not exceed 30 characters")

    def request_body(self) -> dict[str, object]:
        body: dict[str, object] = {
            "contractorId": self.contractorId,
            "orderType": self.orderType,
            "orderUrls": [],
            "amount": self.amount,
            "orderItems": [
                {
                    "itemName": str(item["itemName"]).strip(),
                    "quantity": int(item["quantity"]),
                    "uom": str(item["uom"]).strip(),
                }
                for item in self.orderItems
            ],
        }
        if self.clientId:
            body["clientId"] = self.clientId
        if self.dealerId:
            body["dealerId"] = self.dealerId
        if self.itemSummary:
            body["itemSummary"] = self.itemSummary
        return body


def _normalize_item(item: SpokenItem | dict) -> Optional[dict]:
    if isinstance(item, SpokenItem):
        name, qty, uom = item.item_name, item.quantity, item.uom
    else:
        name = item.get("item_name") or item.get("itemName")
        qty = item.get("quantity")
        uom = item.get("uom")
    name = str(name or "").strip()
    uom = str(uom or "").strip()
    try:
        qty_int = int(qty)
    except (TypeError, ValueError):
        return None
    if not name or qty_int <= 0 or not uom:
        return None
    return {"itemName": name, "quantity": qty_int, "uom": uom}


def build_dto(settings, fields: dict[str, object]) -> MaterialOrderDto:
    """Wrap the spoken item rows in the surrounding screen's identifiers."""
    normalized = [row for row in (_normalize_item(item)
                                  for item in fields.get("items") or []) if row]
    return MaterialOrderDto(
        contractorId=settings.contractor_id,
        clientId=settings.client_id,
        dealerId=settings.dealer_id,
        orderType=settings.order_type,
        orderItems=normalized,
    )
