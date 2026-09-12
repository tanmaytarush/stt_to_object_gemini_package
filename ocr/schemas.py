"""LLM schema for one item-list image. Item rows only, nothing else."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from ..common.schemas import SpokenItem


class OcrExtraction(BaseModel):
    """The LLM's report of one image. The image IS the full list."""

    entity: Literal["ITEMS", "NONE"] = Field(
        description="ITEMS if any material row is visible. "
                    "NONE for a blank, unrelated, or unreadable image."
    )
    confidence: float = Field(
        description="0.0 to 1.0 for the item rows you filled."
    )
    items: list[SpokenItem] = Field(
        default_factory=list,
        description="Every complete item row visible in THIS image. "
                    "Empty if none.",
    )
    notes: Optional[str] = Field(
        default=None,
        description="One short line on dropped or unreadable rows. "
                    "Null if clean.",
    )
    raw_text: Optional[str] = Field(
        default=None,
        description="Plain-text trace of what was read. Null if nothing useful. "
                    "Never a field on the order body.",
    )

    def filled_fields(self) -> dict[str, object]:
        return {"items": list(self.items)} if self.items else {}
