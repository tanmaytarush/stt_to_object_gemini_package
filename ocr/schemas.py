"""LLM schema for one item-list image. Item rows only nothing else."""

from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field
from ..common.schemas import SpokenItem

class OcrExtraction(BaseModel):
    """The LLM report of one image. The image is the full list"""
    entity: Literal["ITEMS", "NONE"] = Field(
        description="ITEMS of any material row is visible. "
        "NONE for a blank, unrelated, or unreadable image."
    )
    confidence: float = Field(
        description="0.0 to 0.1 for the item rows you filled."
    )
    items: list[SpokenItem] = Field(
        default_factory=list,
        description="Every complete item row is visible on this image."
        "Empty if None.",
    )
    notes: Optional[str] = Field(
        default=None,
        description="One short line on dropped or unreadable rows. "
        "Null if clean.",
    )
    raw_text: Optional[str] = Field(
        default=None,
        description="Plain text trace of what was read. Null if nothing useful."
        "Never a field on the order body.",
    )

    def filled_fields(self) -> dict[str, object]:
        return {"items": list(self.items)} if self.items else {}