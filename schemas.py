"""Two schema layers.

Layer 1 (`Extraction`) is what the LLM fills in: loose, flat, everything
optional. Layer 2 (`ClientDto` / `DealerDto`) is what actually goes on the
wire, constructed by Python with contractorId injected. The model never
authors a request body — it only reports what it heard.

The Layer 2 validators mirror Validator/ClientValidator.go and
Validator/DealerValidator.go one-for-one, including their exact message text,
so a local rejection tells you precisely what the server would have said.
Note that Go's `len()` counts BYTES, so the length checks here encode to UTF-8
first: 255 bytes is ~85 Devanagari characters, not 255.
"""

from __future__ import annotations

from typing import ClassVar, Literal, Optional

from pydantic import BaseModel, Field

PROJECT_TYPE_MATERIAL_AND_LABOUR = "MATERIAL_AND_LABOUR"
PROJECT_TYPE_LABOUR_ONLY = "LABOUR_ONLY"
PROJECT_TYPES = (PROJECT_TYPE_MATERIAL_AND_LABOUR, PROJECT_TYPE_LABOUR_ONLY)

MAX_NAME_BYTES = 255
MAX_PHONE_BYTES = 20


class ValidationFailure(Exception):
    """Mirrors a rejection the Go validator would have produced."""


# --- Layer 1: what the model fills ------------------------------------------


class Extraction(BaseModel):
    """The LLM's report of one spoken turn.

    Kept deliberately flat and free of dict/Any fields: google-genai's
    client-side schema validation rejects `additionalProperties`, which is what
    those types generate. `Optional[...]` renders as `anyOf`, which is fine.
    """

    entity: Literal["CLIENT", "DEALER", "NONE"] = Field(
        description="CLIENT if the speaker is describing a customer/site owner, "
                    "DEALER if a material supplier/shop, NONE if neither is "
                    "clearly being described."
    )
    # No ge/le here on purpose: numeric bounds are one of the JSON Schema
    # keywords google-genai has historically choked on client-side. The gate is
    # applied in Python instead (extractor clamps to 0..1).
    confidence: float = Field(
        description="How confident you are, from 0.0 to 1.0, in the entity "
                    "classification and the fields you filled. Below 0.5 if you "
                    "are guessing.",
    )
    name: Optional[str] = Field(
        default=None, description="The client's or dealer's name, or null if not stated."
    )
    phone_number: Optional[str] = Field(
        default=None,
        description="Exactly 10 digits, no spaces, no +91, no leading 0. "
                    "Null if not stated or not recoverable as 10 digits.",
    )
    project_type: Optional[Literal["MATERIAL_AND_LABOUR", "LABOUR_ONLY"]] = Field(
        default=None,
        description="Only if explicitly stated. Never inferred. Clients only.",
    )
    total_amount: Optional[float] = Field(
        default=None,
        description="Total project value in whole rupees. Null if not stated.",
    )
    is_correction: bool = Field(
        default=False,
        description="True if the speaker is correcting something said earlier "
                    "('no, not Ramesh — Rajesh', 'sorry, labour only').",
    )
    also_heard: Optional[str] = Field(
        default=None,
        description="If the turn described a SECOND client or dealer beyond the "
                    "one you extracted, name it here, e.g. "
                    "'DEALER Dermot Traders'. Null when the turn covered one "
                    "record. Never merge two entities into one record.",
    )
    notes: Optional[str] = Field(
        default=None,
        description="One short line on anything ambiguous or dropped — including "
                    "why you returned NONE, when the turn did contain a name or "
                    "digits. Null only when the turn was clean and unambiguous.",
    )

    def filled_fields(self) -> dict[str, object]:
        """The non-null payload fields, for merging into pending state."""
        out: dict[str, object] = {}
        for key in ("name", "phone_number", "project_type", "total_amount"):
            value = getattr(self, key)
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    continue
            if value is not None:
                out[key] = value
        return out


# --- Layer 2: what goes on the wire -----------------------------------------


def _check_name(value: Optional[str], field_name: str) -> str:
    trimmed = (value or "").strip()
    if not trimmed:
        if field_name == "clientName":
            raise ValidationFailure("clientName is required and cannot be empty")
        raise ValidationFailure("dealerName is required")
    if len(trimmed.encode("utf-8")) > MAX_NAME_BYTES:
        raise ValidationFailure(f"{field_name} must not exceed 255 characters")
    return trimmed


class ClientDto(BaseModel):
    """Mirrors RequestDtos.CreateClientRequestDto."""

    contractorId: int
    clientName: Optional[str] = None
    phoneNumber: Optional[str] = None
    projectType: Optional[str] = None
    totalAmount: Optional[float] = None

    entity_label: ClassVar[str] = "CLIENT"

    def missing_required(self) -> list[str]:
        missing = []
        if not (self.clientName or "").strip():
            missing.append("clientName")
        if not (self.phoneNumber or "").strip():
            missing.append("phoneNumber")
        if not self.projectType:
            missing.append("projectType")
        return missing

    def validate_for_api(self) -> None:
        """Raise ValidationFailure with the same text Validator/ClientValidator.go uses."""
        if self.contractorId == 0:
            raise ValidationFailure("contractorId is required and must be greater than 0")
        _check_name(self.clientName, "clientName")

        phone = (self.phoneNumber or "").strip()
        if not phone:
            raise ValidationFailure("phoneNumber is required and cannot be empty")
        if len(phone.encode("utf-8")) > MAX_PHONE_BYTES:
            raise ValidationFailure("phoneNumber must not exceed 20 characters")

        if self.projectType not in PROJECT_TYPES:
            raise ValidationFailure(
                "projectType must be one of: MATERIAL_AND_LABOUR, LABOUR_ONLY"
            )
        if self.totalAmount is not None and self.totalAmount < 0:
            raise ValidationFailure("totalAmount must be non-negative")

    def request_body(self) -> dict[str, object]:
        """The exact JSON body for POST /starship/v1/client.

        totalAmount is omitted rather than sent as null when absent — the Go
        field is a *float64, so an omitted key and a null are equivalent, and
        omitting keeps the printed body honest about what was actually heard.
        """
        body: dict[str, object] = {
            "contractorId": self.contractorId,
            "clientName": (self.clientName or "").strip(),
            "phoneNumber": (self.phoneNumber or "").strip(),
            "projectType": self.projectType,
        }
        if self.totalAmount is not None:
            body["totalAmount"] = self.totalAmount
        return body


class DealerDto(BaseModel):
    """Mirrors RequestDtos.CreateDealerRequestDto. No projectType, phone optional."""

    contractorId: int
    dealerName: Optional[str] = None
    phoneNumber: Optional[str] = None

    entity_label: ClassVar[str] = "DEALER"

    def missing_required(self) -> list[str]:
        return [] if (self.dealerName or "").strip() else ["dealerName"]

    def validate_for_api(self) -> None:
        """Mirrors Validator/DealerValidator.go ValidateCreateDealer."""
        if self.contractorId == 0:
            raise ValidationFailure("contractorId is required and must be greater than 0")
        _check_name(self.dealerName, "dealerName")
        # Go checks the raw pointer value here without trimming first.
        if self.phoneNumber is not None and len(self.phoneNumber.encode("utf-8")) > MAX_PHONE_BYTES:
            raise ValidationFailure("phoneNumber must not exceed 20 characters")

    def request_body(self) -> dict[str, object]:
        body: dict[str, object] = {
            "contractorId": self.contractorId,
            "dealerName": (self.dealerName or "").strip(),
        }
        phone = (self.phoneNumber or "").strip()
        if phone:
            body["phoneNumber"] = phone
        return body


def build_dto(entity: str, contractor_id: int, fields: dict[str, object]):
    """Project the merged extraction state onto the DTO for `entity`.

    Fields that do not exist on the target DTO (projectType and totalAmount on a
    dealer) are dropped here rather than silently sent.
    """
    if entity == "CLIENT":
        return ClientDto(
            contractorId=contractor_id,
            clientName=fields.get("name"),
            phoneNumber=fields.get("phone_number"),
            projectType=fields.get("project_type"),
            totalAmount=fields.get("total_amount"),
        )
    if entity == "DEALER":
        return DealerDto(
            contractorId=contractor_id,
            dealerName=fields.get("name"),
            phoneNumber=fields.get("phone_number"),
        )
    raise ValueError(f"no DTO for entity {entity!r}")


def dropped_for_dealer(fields: dict[str, object]) -> list[str]:
    """Client-only fields present in state that a dealer DTO cannot carry."""
    return [k for k in ("project_type", "total_amount") if k in fields]
