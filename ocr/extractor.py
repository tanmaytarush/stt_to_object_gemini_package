"""Placeholder: image bytes → item rows → MaterialOrderDto.

Will call the same EXTRACT_MODEL as `voice_logging.audio.extractor.Extractor`,
with a response schema framed as SpokenItem rows. Merge/validate/POST stay in
`voice_logging.common`.
"""

from __future__ import annotations

from typing import Optional

from ..common.config import BaseSettings
from ..common.schemas import MaterialOrderDto


class OcrExtractor:
    """Structured OCR of an item-list image. Not implemented yet."""

    def __init__(self, settings: BaseSettings) -> None:
        self._settings = settings

    async def extract(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> MaterialOrderDto:
        raise NotImplementedError(
            "OCR extraction is not implemented yet. "
            "It will use EXTRACT_MODEL from voice_logging.common.config."
        )

    def current_dto(self) -> Optional[MaterialOrderDto]:
        return None
