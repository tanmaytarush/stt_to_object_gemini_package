"""Turn assembly and structured extraction.

Finalized utterances accumulate into a "turn". After --silence seconds of
quiet, the whole turn goes to Flash with a response_schema, and the resulting
Extraction is merged into pending state. Merging across turns is what lets you
speak one field at a time and still build one object.
"""

from __future__ import annotations

import asyncio
import re
import warnings
from dataclasses import dataclass, field
from typing import Optional

from google import genai
from google.genai import types

from . import config
from .schemas import Extraction, ValidationFailure, build_dto, dropped_for_dealer

_DIGITS_ONLY = re.compile(r"\D")

# Measured on gemini-3.5-flash with this system prompt: 13.5–20s per turn,
# steady state, not a cold start. The old 20s ceiling sat right on top of that
# and turned ordinary slowness into "extraction call failed" on real speech.
EXTRACT_TIMEOUT_SECONDS = 60

SYSTEM_PROMPT = """\
You extract structured records from live speech by an Indian construction \
contractor. The speech is transcribed from Hindi, Gujarati, Marathi, Kannada or \
Indian English and is frequently code-switched mid-sentence. Transcription \
errors are common; your job is to recover intent, not to trust the text \
literally.

The contractor is registering one of two things:
- CLIENT — a customer or site owner they are doing work for.
  Cues: "client", "customer", "grahak", "graahaka", "party", "site", \
"naya kaam", "ghar", "hosa kelasa", "mane".
- DEALER — a shop or supplier they buy material from.
  Cues: "dealer", "supplier", "shop", "dukaan", "angadi", "traders", \
"hardware", "agency", "stores", "enterprises", "& sons", "vyapari".

WHEN TO RETURN NONE
Return entity NONE only when the turn has nothing to record: chatter, a \
greeting, thinking aloud, background conversation, or speech with no person or \
firm in it.

NONE is NOT the answer to a messy turn. If the turn names a person or a firm \
anywhere alongside a client-or-dealer cue, you MUST classify it. Pick the better \
fit and lower `confidence` — a low-confidence record the user can see and fix \
beats silence, because nothing is written anywhere until they approve it. A turn \
that is garbled, self-contradictory, or crammed with several facts still gets \
classified; you record the trouble in `notes`, you do not bail.

Whenever you do return NONE for a turn that contained a name or digits, `notes` \
must say why in one short line.

MULTIPLE ENTITIES IN ONE TURN
People say everything in one breath: "new client Ramesh Kumar phone … dealer \
Sharma Traders". Extract the FIRST/clearest entity as the record and name the \
other in `also_heard` (e.g. "DEALER Sharma Traders"). Never blend two entities \
into one record, and never let a trailing second entity turn the whole turn into \
NONE.

SELF-CORRECTION INSIDE ONE TURN — CHECK THIS FIRST
People correct themselves mid-sentence, and the transcript keeps both halves. If \
a second value for the same field is introduced by a word meaning "no" or "not \
that" — illa, alla, nahi, nahin, no, not, sorry, actually, galat, wait — then it \
is a CORRECTION and THE LATER VALUE WINS. Fill the field with it, set \
is_correction true, and do not call it a contradiction.
- "kooli matra, ottu eradu laksha ILLA samagri jothe" -> project_type = \
MATERIAL_AND_LABOUR. The speaker said labour-only, then took it back.
- "phone ondu eradu mooru, NAHI, ondu eradu naalku" -> keep the second number.
Word order settles it: whatever follows the negation is what they meant.

CONTRADICTIONS — only when there is no such correction word
If one field is genuinely stated two incompatible ways with nothing marking \
either as a retraction, leave THAT ONE FIELD null and say so in `notes`. Still \
classify the entity, and still fill every other field you heard. One muddled \
field must never cost the user the whole record.

CONTEXT CARRYING
You are given the fields already collected in this record. If the turn adds a \
field to an in-progress record (e.g. only "phone nau aath saat…" after a name \
was already given), return the SAME entity as the record in progress, and fill \
only the field that was actually spoken. Leave every other field null — null \
means "not stated in this turn", and never erases a value already collected.

FIELD RULES

name
- The person's or firm's name. Preserve business suffixes: "Sharma Traders", \
"Gupta Hardware & Sons".
- Strip trailing honorifics that are not part of the name: ji, bhai, bhaiya, \
saheb, seth, shri, sir, madam. Keep them when they are genuinely part of a firm \
name ("Seth Brothers").
- Title Case. {name_script_rule}

phone_number
- Output exactly 10 digits, nothing else: no spaces, no dashes, no +91, no \
leading 0.
- Digits are often dictated as words, in any of the languages, mixed together. \
Map: shunya/zero/sifar=0, ek/one=1, do/two/be=2, teen/three/tran=3, \
chaar/four/char=4, paanch/five/panch=5, chhe/six/chah=6, saat/seven/sat=7, \
aath/eight/aath=8, nau/nine/nav=9, das/ten=10 (as two digits 1 and 0 only when \
clearly part of a digit string).
- Kannada digits: sonne/sunne=0, ondu=1, eradu=2, mooru=3, naalku/naalu=4, \
aidu=5, aaru=6, elu=7, entu=8, ombattu=9, hattu=10.
- They may also be dictated in pairs or triples ("ninety-eight, seventy-six, \
five four…"). Expand those to their digits in order.
- "double 5" = 55, "triple 7" = 777.
- If, after all of that, you do not have exactly 10 digits, return null. Never \
return a partial, padded, or invented number. A missing phone is recoverable; a \
wrong one is not.

project_type — CLIENT ONLY, and only when explicitly stated
- MATERIAL_AND_LABOUR: "material and labour", "maal aur mazdoori", \
"saaman ke saath", "material sahit", "with material", "dono", "both", \
"samagri jothe", "samagri sahita", "eradu".
- LABOUR_ONLY: "labour only", "sirf labour", "only mazdoori", "fakt majuri", \
"sirf kaam", "material nahi", "without material", "kooli matra", \
"kelasa matra", "samagri illa".
- If the speaker did not say which, return null. Do NOT infer it from anything \
else in the sentence. It is a required field, so the user will be prompted.

total_amount — CLIENT ONLY. Whole rupees as a number, never a string.
- hazaar/hajaar/savira = 1,000 · lakh/laksha = 100,000 · crore/koti = 10,000,000.
- Fractional prefixes: sawa X = X + 0.25 · paune X = X − 0.25 · dedh = 1.5 · \
dhai/adhai = 2.5 · sadhe X = X + 0.5 · adha = 0.5.
- So: "dedh lakh" = 150000 · "sawa lakh" = 125000 · "paune do lakh" = 175000 · \
"sadhe teen lakh" = 350000 · "do lakh pachaas hazaar" = 250000.
- A bare number with no scale word is taken as rupees: "pachattar hazaar" = \
75000, "bees thousand" = 20000.
- The multiplier ALWAYS comes before the scale: "aidu savira" = 5 × 1000 = 5000, \
"do lakh" = 2 × 100,000. If you see the scale first with a number after it \
("savira aidu"), the speech was garbled or reversed — do NOT quietly read it as \
1000 + 5. Return your best reading and say in `notes` that the order looked \
reversed, or return null if you cannot tell. This is money; a plausible wrong \
number is worse than no number.
- If no amount was stated, null. Never estimate one.

is_correction
- True when the turn revises something already said: "nahi", "no, not…", \
"sorry", "galat", "actually", "change karo", or simply restating a field that \
is already filled with a different value.

confidence
- 0.0 to 1.0, covering the entity classification and THE VALUES YOU ACTUALLY \
FILLED — nothing else. Below 0.5 only when you are guessing at the entity, or \
at a value you still chose to return.
- Judge it on what survives, not on how untidy the turn was. A rambling turn \
with a contradiction in it is still high confidence when you nulled the \
contradicted field and the fields you kept are clear: nulling IS how you express \
doubt about one field. Do not also discount the whole turn for it — that would \
throw away the good fields alongside the bad one.
- Likewise a second entity in `also_heard`, a dropped short phone, or a name you \
had to romanize are all normal, and none of them lower confidence on their own.

also_heard
- Set only when a SECOND client or dealer appeared in the turn and you did not \
extract it. Format: "DEALER Sharma Traders" or "CLIENT Meena". Otherwise null.

notes
- One short line if something was ambiguous, contradictory, or deliberately \
dropped — and always when you returned NONE for a turn that had a name or \
digits in it. Otherwise null. State the problem, do not narrate your process.
"""

_NAME_SCRIPT_RULES = {
    "latin": (
        "Romanize names written in Devanagari, Gujarati or Kannada script into "
        "Latin letters using the most common English spelling (राजेश कुमार -> "
        "\"Rajesh Kumar\", ಸುರೇಶ್ -> \"Suresh\")."
    ),
    "native": (
        "Keep names in whatever script they were transcribed in; do not "
        "transliterate."
    ),
}


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
    # A second entity the turn mentioned but that no record captured. Report
    # only — it never touches pending state.
    also_heard: Optional[str] = None
    # Fields the model did fill but that the confidence gate refused. Shown to
    # the user so a gated turn is never invisible.
    ignored_fields: dict[str, object] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return self.dto is not None and not self.missing and not self.validation_error


class Extractor:
    def __init__(self, settings: config.Settings) -> None:
        self._settings = settings
        self._client = genai.Client(api_key=settings.api_key)
        self._turn_parts: list[str] = []
        self.entity: Optional[str] = None
        self.fields: dict[str, object] = {}

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
        if not self.fields:
            return "Record in progress: none. This turn starts a new record."
        lines = [f"Record in progress: {self.entity}"]
        for key, value in self.fields.items():
            lines.append(f"  {key} = {value!r}")
        lines.append(
            "Fill only fields this turn actually states. Return the same entity "
            "unless the speaker clearly switched to a different one."
        )
        return "\n".join(lines)

    async def flush(self) -> Optional[TurnResult]:
        """Extract the buffered turn, merge it, and report what happened."""
        turn_text = " ".join(part.strip() for part in self._turn_parts if part.strip()).strip()
        self._turn_parts.clear()
        if not turn_text:
            return None

        system_prompt = SYSTEM_PROMPT.format(
            name_script_rule=_NAME_SCRIPT_RULES[self._settings.name_script]
        )
        contents = f"{self._pending_context()}\n\nWhat the contractor just said:\n{turn_text}"

        # Pass a JSON Schema dict, not the Pydantic class. The class is treated
        # as a tool and trips automatic function calling (AFC), which prints a
        # warning onto the status line and can hang generate_content.
        schema = Extraction.model_json_schema()
        gen_config = types.GenerateContentConfig(
            system_instruction=system_prompt,
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
                        self._client.aio.models.generate_content(
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
        # Reported on every path, including the ones that record nothing: a turn
        # that mentioned a second entity should say so even when it was ignored.
        also_heard = (extraction.also_heard or "").strip() or None

        if extraction.entity == "NONE":
            return TurnResult(turn_text=turn_text, extraction=extraction,
                              entity=self.entity, also_heard=also_heard)

        if confidence < self._settings.min_confidence:
            return TurnResult(
                turn_text=turn_text, extraction=extraction, entity=self.entity,
                also_heard=also_heard,
                ignored_fields=extraction.filled_fields(),
                warnings=[f"ignored: confidence {confidence:.2f} < "
                          f"{self._settings.min_confidence:.2f} "
                          f"(--min-confidence to lower the bar)"],
            )

        incoming = extraction.filled_fields()

        # Normalize the phone defensively — the prompt asks for bare digits, but
        # a wrong phone number is the single worst failure mode here, so we do
        # not rely on the model having complied.
        if "phone_number" in incoming:
            digits = _DIGITS_ONLY.sub("", str(incoming["phone_number"]))
            digits = digits[2:] if len(digits) == 12 and digits.startswith("91") else digits
            digits = digits[1:] if len(digits) == 11 and digits.startswith("0") else digits
            if len(digits) == 10:
                incoming["phone_number"] = digits
            else:
                warnings.append(
                    f"dropped phone {incoming['phone_number']!r}: got {len(digits)} digits, need 10"
                )
                incoming.pop("phone_number")

        # Entity switch handling.
        if self.entity is not None and extraction.entity != self.entity and self.fields:
            if self._settings.strict_entity:
                return TurnResult(
                    turn_text=turn_text, extraction=extraction, entity=self.entity,
                    also_heard=also_heard,
                    warnings=[f"refused switch {self.entity} -> {extraction.entity} "
                              f"(--strict-entity); type 'r' to reset the record first"],
                )
            warnings.append(
                f"switched {self.entity} -> {extraction.entity}; started a new record"
            )
            self.fields = {}

        self.entity = extraction.entity

        changed: dict[str, object] = {}
        for key, value in incoming.items():
            if self.fields.get(key) != value:
                changed[key] = value
            self.fields[key] = value

        if self.entity == "DEALER":
            for key in dropped_for_dealer(self.fields):
                self.fields.pop(key, None)
                changed.pop(key, None)
                warnings.append(f"{key} dropped — dealers have no such field")

        dto = build_dto(self.entity, self._settings.contractor_id, self.fields)
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
            validation_error=validation_error, also_heard=also_heard,
        )

    def current_dto(self):
        """The DTO for the pending record as it stands, or None."""
        if not self.entity or not self.fields:
            return None
        return build_dto(self.entity, self._settings.contractor_id, self.fields)
