"""Turn assembly and structured extraction.

Finalized utterances accumulate into a "turn". After --silence seconds of
quiet, the whole turn goes to Flash with a response_schema, and the resulting
Extraction is merged into pending state. Merging across turns is what lets you
speak one field at a time and still build one object.
"""

from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass, field
from typing import Optional

from . import config
from .schemas import Extraction, ValidationFailure, _normalize_item, build_dto

# Measured on gemini-3.5-flash with this system prompt: 13.5–20s per turn,
# steady state, not a cold start. The old 20s ceiling sat right on top of that
# and turned ordinary slowness into "extraction call failed" on real speech.
EXTRACT_TIMEOUT_SECONDS = 60

SYSTEM_PROMPT = """\
You fill in the Items section of a New Order screen from live speech by an \
Indian construction contractor. The speech is transcribed from Hindi, Gujarati, \
Marathi, Kannada or Indian English and is frequently code-switched. \
Transcription errors are common; recover intent, do not trust the text literally.

The rest of the screen is already filled in by hand — the dealer, the client, \
and what kind of order this is. You do NOT extract any of that. Names of \
people, shops, firms, phone numbers, prices, totals and dates are NOT your \
job: ignore them entirely, even when they are said in the same breath. You \
produce ONE thing: rows of item name + quantity + unit.

WHEN TO RETURN NONE
Return entity NONE only when the turn names no material at all — greetings, \
thinking aloud, background talk, or a turn that was only about a dealer, a \
person, or a price. If they name any material, return ITEMS.

items
- Each row: item_name, quantity (integer > 0), uom.
- item_name is the material. Keep Pidilite brand names intact and spelled the \
usual way (Fevicol SH, Fevicol Marine, Dr. Fixit, Fevikwik, M-Seal, Roff, \
Araldite). Drop filler around it: "ek bag cement laao" -> "cement".
- uom is the unit AS SPOKEN: bag, bori, katta, thaila, tin, dabba, peti, nag, \
kg, litre, piece, packet, roll, bundle, quintal. Do not convert between units \
and do not invent one.
- quantity is whole units. Digit words in any of the languages: ek/one=1, \
do/be/two=2, teen/tran/three=3, chaar/four=4, paanch/panch/five=5, chhe/six=6, \
saat/seven=7, aath/eight=8, nau/nine=9, das/ten=10; Kannada ondu=1, eradu=2, \
mooru=3, naalku=4, aidu=5, aaru=6, elu=7, entu=8, ombattu=9, hattu=10. \
"dedh"=1.5 and "sawa"/"paune" fractions are NOT whole units — round is wrong, \
so drop that row and explain it in notes.
- One row per material. "das bag cement aur paanch kilo Fevicol" is two rows.
- If a material is named with NO quantity or NO unit, drop that row and say so \
in notes. A half-row is worse than a missing one: the user can see the note and \
say it again.
- Report only THIS TURN's rows. Do not repeat rows already in the list.

SELF-CORRECTION
When they take something back — nahi, nahin, no, not, sorry, actually, galat, \
illa, alla — set is_correction true and send the FULL replacement list, not \
just the fixed row. "nahi, aath bag cement" after "das bag cement, paanch kilo \
Fevicol" means the whole list is now [8 bag cement, 5 kg Fevicol].

CONTEXT CARRYING
You are given the rows already collected. New rows ADD to that list; only \
is_correction replaces it. An empty items list means this turn added nothing.

confidence
- 0.0–1.0 on the rows you filled. Below 0.5 only if you are guessing at a \
material name, a quantity, or a unit.

notes
- One short line when you dropped a row, or a unit was missing, or a quantity \
was unclear. Otherwise null. State the problem; do not narrate your process.
"""


@dataclass
class TurnResult:
    """What one silence-triggered extraction produced."""

    turn_text: str
    extraction: Optional[Extraction]
    entity: Optional[str]
    dto: object | None = None
    missing: list[str] = field(default_factory=list)
    changed: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None
    validation_error: Optional[str] = None
    # Fields the model did fill but that the confidence gate refused. Shown to
    # the user so a gated turn is never invisible.
    ignored_fields: dict[str, object] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return self.dto is not None and not self.missing and not self.validation_error


class Extractor:
    def __init__(self, settings: config.Settings) -> None:
        self._settings = settings
        self._client = None
        self._turn_parts: list[str] = []
        self.entity: Optional[str] = None
        self.fields: dict[str, object] = {}

    def _genai_client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self._settings.api_key)
        return self._client

    # -- turn buffer -------------------------------------------------------

    def add_final(self, text: str) -> None:
        self._turn_parts.append(text)

    @property
    def has_turn(self) -> bool:
        return bool(self._turn_parts)

    @property
    def has_pending_record(self) -> bool:
        return bool(self.fields)

    def reset_record(self) -> None:
        self.entity = None
        self.fields = {}

    def set_field(self, key: str, value: object) -> None:
        """Manual override from the edit prompt."""
        if value is None:
            self.fields.pop(key, None)
        else:
            self.fields[key] = value

    # -- extraction --------------------------------------------------------

    def _pending_context(self) -> str:
        rows = self.fields.get("items") or []
        if not rows:
            return "Item rows so far: none. This turn starts the list."
        lines = ["Item rows so far:"]
        for row in rows:
            lines.append(f"  {row['quantity']} {row['uom']} {row['itemName']}")
        lines.append(
            "Report only rows this turn adds. They append to the list above. "
            "Set is_correction and resend the whole list only if this turn "
            "takes something back."
        )
        return "\n".join(lines)

    async def flush(self) -> Optional[TurnResult]:
        """Extract the buffered turn, merge it, and report what happened."""
        turn_text = " ".join(part.strip() for part in self._turn_parts if part.strip()).strip()
        self._turn_parts.clear()
        if not turn_text:
            return None

        from google.genai import types

        contents = f"{self._pending_context()}\n\nWhat the contractor just said:\n{turn_text}"

        # Pass a JSON Schema dict, not the Pydantic class. The class is treated
        # as a tool and trips automatic function calling (AFC), which prints a
        # warning onto the status line and can hang generate_content.
        schema = Extraction.model_json_schema()
        gen_config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.0,
        )
        afc = getattr(types, "AutomaticFunctionCallingConfig", None)
        if afc is not None:
            gen_config.automatic_function_calling = afc(disable=True)

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
            return TurnResult(
                turn_text=turn_text, extraction=None, entity=self.entity,
                error=f"extraction call failed: {last_error}",
            )

        extraction = self._parse_extraction(response)
        if extraction is None:
            return TurnResult(turn_text=turn_text, extraction=None, entity=self.entity,
                              error="model returned no parseable extraction")

        return self._merge(turn_text, extraction)

    @staticmethod
    def _parse_extraction(response) -> Optional[Extraction]:
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, Extraction):
            return parsed
        if isinstance(parsed, dict):
            try:
                return Extraction.model_validate(parsed)
            except Exception:
                pass
        raw = (getattr(response, "text", None) or "").strip()
        if not raw:
            return None
        try:
            return Extraction.model_validate_json(raw)
        except Exception:
            return None

    def _merge(self, turn_text: str, extraction: Extraction) -> TurnResult:
        warnings: list[str] = []
        confidence = min(max(extraction.confidence, 0.0), 1.0)

        if extraction.entity == "NONE":
            return TurnResult(turn_text=turn_text, extraction=extraction,
                              entity=self.entity)

        if confidence < self._settings.min_confidence:
            return TurnResult(
                turn_text=turn_text, extraction=extraction, entity=self.entity,
                ignored_fields=extraction.filled_fields(),
                warnings=[f"ignored: confidence {confidence:.2f} < "
                          f"{self._settings.min_confidence:.2f} "
                          f"(--min-confidence to lower the bar)"],
            )

        self.entity = "ITEMS"

        normalized = [row for row in (_normalize_item(item)
                                      for item in extraction.items) if row]
        dropped = len(extraction.items) - len(normalized)
        if dropped:
            warnings.append(
                f"dropped {dropped} incomplete row(s) — a row needs a name, a "
                f"quantity above 0, and a unit"
            )

        changed: dict[str, object] = {}
        if normalized:
            existing = list(self.fields.get("items") or [])
            self.fields["items"] = normalized if extraction.is_correction \
                else existing + normalized
            changed["items"] = normalized

        dto = build_dto(self._settings, self.fields)
        missing = dto.missing_required()

        validation_error = None
        if not missing:
            try:
                dto.validate_for_api()
            except ValidationFailure as exc:
                validation_error = str(exc)

        return TurnResult(
            turn_text=turn_text, extraction=extraction, entity=self.entity,
            dto=dto, missing=missing, changed=changed, warnings=warnings,
            validation_error=validation_error,
        )

    def current_dto(self):
        """The DTO for the pending order as it stands, or None."""
        if not self.fields:
            return None
        return build_dto(self._settings, self.fields)
