"""LLM schema for one spoken turn. Item rows only, nothing else."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from ..common.schemas import SpokenItem


class Extraction(BaseModel):
    """The LLM's report of one spoken turn."""

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
