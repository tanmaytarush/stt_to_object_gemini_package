"""Image bytes → Flash structured OCR → MaterialOrderDto.

One image is the whole list. There is no turn buffer and no is_correction.
Screen ids still come from Settings, never from the photo.
"""

from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..common.schemas import ValidationFailure, _normalize_item, build_dto
from . import config
from .schemas import OcrExtraction

EXTRACT_TIMEOUT_SECONDS = 60

SYSTEM_PROMPT = """\
You fill in the Items section of a New Order screen from a photo of an \
item list. The photo may be a printed challan, a handwritten note, a \
WhatsApp screenshot, or a dealer bill. Text is frequently Hindi, Gujarati, \
Marathi, Kannada or Indian English, and OCR errors are common; recover \
intent, do not trust glyphs literally.

The rest of the screen is already filled in by hand — the dealer, the client, \
and what kind of order this is. You do NOT extract any of that. Names of \
people, shops, firms, phone numbers, prices, totals, dates and letterheads \
are NOT your job: ignore them entirely. You produce ONE thing: rows of \
item name + quantity + unit.

WHEN TO RETURN NONE
Return entity NONE only when the image names no material at all — a blank \
page, an unrelated photo, or a document that is only about a dealer, a \
person, or a price. If any material is visible, return ITEMS.

items
- Each row: item_name, quantity (integer > 0), uom.
- item_name is the material. Keep Pidilite brand names intact and spelled the \
usual way (Fevicol SH, Fevicol Marine, Dr. Fixit, Fevikwik, M-Seal, Roff, \
Araldite). Drop filler around it.
- uom is the unit AS WRITTEN: bag, bori, katta, thaila, tin, dabba, peti, nag, \
kg, litre, piece, packet, roll, bundle, quintal. Do not convert between units \
and do not invent one.
- quantity is whole units. Digit words in any of the languages: ek/one=1, \
do/be/two=2, teen/tran/three=3, chaar/four=4, paanch/panch/five=5, chhe/six=6, \
saat/seven=7, aath/eight=8, nau/nine=9, das/ten=10; Kannada ondu=1, eradu=2, \
mooru=3, naalku=4, aidu=5, aaru=6, elu=7, entu=8, ombattu=9, hattu=10. \
Fractions (dedh, sawa, paune) are NOT whole units — drop that row and \
explain it in notes.
- One row per material. The image is the FULL list for this order.
- If a material is named with NO quantity or NO unit, drop that row and say so \
in notes. A half-row is worse than a missing one.

confidence
- 0.0–1.0 on the rows you filled. Below 0.5 only if you are guessing at a \
material name, a quantity, or a unit.

notes
- One short line when you dropped a row, or a unit was missing, or a quantity \
was unclear. Otherwise null. State the problem; do not narrate your process.

raw_text
- A short plain-text trace of the lines you actually read, or null. This is \
for debugging. It must never include a price, a phone, or a dealer name.
"""


@dataclass
class OcrResult:
    """What one image (or a concatenated set of images) produced."""

    source: str
    extraction: Optional[OcrExtraction]
    dto: object | None = None
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None
    validation_error: Optional[str] = None
    ignored_fields: dict[str, object] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return self.dto is not None and not self.missing and not self.validation_error


class OcrExtractor:
    def __init__(self, settings: config.Settings) -> None:
        self._settings = settings
        self._client = None

    def _genai_client(self):
        if self._client is None:
            try:
                from google import genai
            except ModuleNotFoundError as exc:
                raise SystemExit(
                    "google-genai is not installed in this Python.\n"
                    "  python -m pip install -r voice_logging/requirements.txt\n"
                    "  or use the project venv: voice_logging/venv/bin/python -m voice_logging ocr …"
                ) from exc
            self._client = genai.Client(api_key=self._settings.api_key)
        return self._client

    async def extract_paths(self, paths: list[Path] | None = None) -> OcrResult:
        """Read each image, extract, concatenate rows in file order."""
        chosen = paths or self._settings.image_paths
        combined: list = []
        notes: list[str] = []
        raw_parts: list[str] = []
        warnings: list[str] = []
        last_extraction: OcrExtraction | None = None
        label = ", ".join(path.name for path in chosen) or "(no image)"

        for path in chosen:
            data, mime = config.read_image(path)
            result = await self.extract(data, mime, source=str(path))
            if result.error:
                return result
            if result.warnings:
                warnings.extend(f"{path.name}: {w}" for w in result.warnings)
            extraction = result.extraction
            if extraction is None:
                continue
            last_extraction = extraction
            if extraction.entity == "NONE":
                if extraction.notes:
                    notes.append(f"{path.name}: {extraction.notes}")
                continue
            if result.ignored_fields:
                return result
            rows = result.dto.orderItems if result.dto is not None else []
            combined.extend(rows)
            if extraction.notes:
                notes.append(f"{path.name}: {extraction.notes}")
            if extraction.raw_text:
                raw_parts.append(extraction.raw_text)

        if not combined:
            merged = last_extraction or OcrExtraction(
                entity="NONE", confidence=1.0, items=[],
                notes="; ".join(notes) or None,
            )
            return OcrResult(
                source=label, extraction=merged, warnings=warnings,
            )

        fields = {"items": combined}
        dto = build_dto(self._settings, fields)
        missing = dto.missing_required()
        validation_error = None
        if not missing:
            try:
                dto.validate_for_api()
            except ValidationFailure as exc:
                validation_error = str(exc)

        merged = OcrExtraction(
            entity="ITEMS",
            confidence=last_extraction.confidence if last_extraction else 1.0,
            items=[],
            notes="; ".join(notes) or None,
            raw_text="\n".join(raw_parts) or None,
        )
        return OcrResult(
            source=label, extraction=merged, dto=dto, missing=missing,
            warnings=warnings, validation_error=validation_error,
        )

    async def extract(
        self,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        source: str = "image",
    ) -> OcrResult:
        from google.genai import types

        schema = OcrExtraction.model_json_schema()
        gen_config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.0,
        )
        afc = getattr(types, "AutomaticFunctionCallingConfig", None)
        if afc is not None:
            gen_config.automatic_function_calling = afc(disable=True)

        contents = [
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            types.Part.from_text(
                text="Extract every complete item row from this image."
            ),
        ]

        last_error: Exception | None = None
        response = None
        for attempt in range(3):
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message=".*automatic function calling.*")
                    warnings.filterwarnings("ignore", message=".*AFC.*")
                    response = await asyncio.wait_for(
                        self._genai_client().aio.models.generate_content(
                            model=self._settings.extract_model,
                            contents=contents,
                            config=gen_config,
                        ),
                        timeout=EXTRACT_TIMEOUT_SECONDS,
                    )
                break
            except asyncio.TimeoutError:
                last_error = TimeoutError(
                    f"extraction timed out after {EXTRACT_TIMEOUT_SECONDS}s"
                )
                break
            except Exception as exc:
                last_error = exc
                overloaded = "503" in str(exc) or "UNAVAILABLE" in str(exc)
                if not overloaded or attempt == 2:
                    break
                await asyncio.sleep(1.5 * (attempt + 1))

        if response is None:
            return OcrResult(
                source=source, extraction=None,
                error=f"extraction call failed: {last_error}",
            )

        extraction = self._parse_extraction(response)
        if extraction is None:
            return OcrResult(
                source=source, extraction=None,
                error="model returned no parseable extraction",
            )
        return self._frame(source, extraction)

    @staticmethod
    def _parse_extraction(response) -> Optional[OcrExtraction]:
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, OcrExtraction):
            return parsed
        if isinstance(parsed, dict):
            try:
                return OcrExtraction.model_validate(parsed)
            except Exception:
                pass
        raw = (getattr(response, "text", None) or "").strip()
        if not raw:
            return None
        try:
            return OcrExtraction.model_validate_json(raw)
        except Exception:
            return None

    def _frame(self, source: str, extraction: OcrExtraction) -> OcrResult:
        warnings: list[str] = []
        confidence = min(max(extraction.confidence, 0.0), 1.0)

        if extraction.entity == "NONE":
            return OcrResult(source=source, extraction=extraction)

        if confidence < self._settings.min_confidence:
            return OcrResult(
                source=source, extraction=extraction,
                ignored_fields=extraction.filled_fields(),
                warnings=[
                    f"ignored: confidence {confidence:.2f} < "
                    f"{self._settings.min_confidence:.2f} "
                    f"(--min-confidence to lower the bar)"
                ],
            )

        normalized = [row for row in (_normalize_item(item)
                                      for item in extraction.items) if row]
        dropped = len(extraction.items) - len(normalized)
        if dropped:
            warnings.append(
                f"dropped {dropped} incomplete row(s) — a row needs a name, a "
                f"quantity above 0, and a unit"
            )

        if not normalized:
            return OcrResult(
                source=source, extraction=extraction, warnings=warnings,
            )

        dto = build_dto(self._settings, {"items": normalized})
        missing = dto.missing_required()
        validation_error = None
        if not missing:
            try:
                dto.validate_for_api()
            except ValidationFailure as exc:
                validation_error = str(exc)

        return OcrResult(
            source=source, extraction=extraction, dto=dto, missing=missing,
            warnings=warnings, validation_error=validation_error,
        )
