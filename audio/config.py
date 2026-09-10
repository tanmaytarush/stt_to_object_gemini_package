"""Audio-module settings: STT, mic, languages, plus the shared screen context."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field

from ..common.config import (
    EXTRACT_MODEL,
    BaseSettings,
    add_shared_arguments,
    load_dotenv,
    resolve_shared,
)

# Live streaming speech-to-text. Sessions are capped at 10 minutes by the
# service, which is why transcriber.py rotates the connection proactively.
STT_MODEL = "gemini-3.5-transcribe-live"

# Re-export so `from .config import EXTRACT_MODEL` keeps working inside audio/.
# The value itself lives in common — OCR will use the same id.

# --- Audio ------------------------------------------------------------------

SAMPLE_RATE = 16_000  # the Live API requires 16 kHz mono little-endian PCM
CHANNELS = 1
CHUNK_MS = 100
BLOCK_FRAMES = SAMPLE_RATE * CHUNK_MS // 1000  # 1600 frames

# Bounded so a network stall drops audio instead of queueing minutes of it.
MAX_QUEUED_CHUNKS = 50  # 5 seconds

# Reconnect a little before the server's 10-minute cap so rotation never lands
# mid-word.
SESSION_ROTATE_SECONDS = 9 * 60 + 30

# --- Speech biasing ---------------------------------------------------------

# Passed to the ASR as custom_vocabulary (max 1000 terms). This is the cheapest
# accuracy lever in the pipeline — extend it freely with names and materials
# that come up in your own recordings.
#
# Deliberately NO romanized digit words (nau/aath, ombattu/entu, …). Under
# automatic language detection the Hindi and Kannada sets compete with each
# other, and SMART mode already emits spoken digits as numerals without help —
# a live Kannada run produced "phone number 92665243" on its own. The extraction
# prompt still maps digit words if any ever do arrive as text; biasing the ASR
# was the wrong layer for it.
CUSTOM_VOCABULARY: list[str] = [
    # Pidilite brands
    "Fevicol", "Fevicol SH", "Fevicol Marine", "Dr. Fixit", "Fevikwik",
    "M-Seal", "Fevistik", "Roff", "Araldite", "Fevigum", "Fevibond",
    "Dr. Fixit LW+", "Dr. Fixit Pidiproof", "Roff Rainbow Tile Mate",
    # Generic construction materials
    "cement", "sariya", "rebar", "TMT", "plywood", "laminate", "putty",
    "primer", "distemper", "emulsion", "POP", "gypsum", "tile", "grout",
    "waterproofing", "adhesive", "sealant", "thinner", "turpentine",
    # Units of measure as actually spoken
    "bori", "boriya", "katta", "thaila", "nag", "peti", "dabba", "tin",
    "kg", "kilo", "litre", "liter", "gram", "quintal", "ton", "bag",
    "square feet", "running feet", "piece", "packet", "bundle", "roll",
    # Domain vocabulary
    "contractor", "thekedar", "mistri", "dealer", "client", "labour",
    "mazdoori", "majuri", "material", "maal", "saaman", "site", "advance",
    "MATERIAL_AND_LABOUR", "LABOUR_ONLY",
    # Kannada domain vocabulary
    "graahaka", "angadi", "vyapari", "kelasa", "kooli", "samagri", "matra",
    # Amount scales — content words SMART mode will not normalize on its own,
    # and far less collision-prone across languages than bare digits.
    "hazaar", "hajaar", "lakh", "lakhs", "crore", "sawa", "dedh", "paune",
    "adhaa", "sadhe", "savira", "laksha", "koti",
]

DEFAULT_LANGUAGES = ["hi-IN", "gu-IN", "mr-IN", "kn-IN", "en-IN"]

# Older imports (`from .config import _load_dotenv`) still resolve.
_load_dotenv = load_dotenv


@dataclass
class Settings(BaseSettings):
    languages: list[str] = field(default_factory=list)
    silence_seconds: float = 1.5
    input_device: int | None = None
    text_mode: bool = False
    transcript_mode: str = "SMART"
    lock_languages: bool = False
    custom_vocabulary: list[str] = field(default_factory=lambda: list(CUSTOM_VOCABULARY))

    @property
    def stt_language_codes(self) -> list[str]:
        """Codes actually sent to Gemini Live.

        Sending nothing is the correct way to auto-detect, not an omission.
        The SDK documents `language_codes` as "hints about the languages present
        in the audio. If omitted or empty, defaults to automatic language
        detection" — they are hints, never a lock. The sibling fields that look
        like they belong here, `language_auto` and `language_hints`, are both
        marked Deprecated; there is no better knob to reach for.

        A multi-language hint list actively hurts: hi-IN + kn-IN together makes
        Hindi win and transcribes spoken Kannada into Devanagari. So unless the
        user locked a single language or passed --lock-languages, send nothing
        and let the model follow whatever is being spoken.
        """
        if not self.languages:
            return []
        if self.lock_languages or len(self.languages) == 1:
            return list(self.languages)
        return []


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m voice_logging",
        description=(
            "Speak the Items section of a New Order screen: item, quantity, "
            "unit. The dealer, the client and the order type come from the "
            "screen — voice never sets them."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_shared_arguments(p)
    p.add_argument(
        "--languages", default=None,
        help="Comma-separated BCP-47 codes, or 'auto'. A single code locks "
             "that language. Two or more are kept as a note only — Gemini "
             "auto-detects the language being spoken (Hindi+Kannada together "
             "used to force Devanagari). Env: STT_LANGUAGES.",
    )
    p.add_argument(
        "--lock-languages", action="store_true",
        help="Send the full --languages / STT_LANGUAGES list to Gemini as "
             "hints. Off by default so spoken language wins.",
    )
    p.add_argument(
        "--silence", type=float, default=1.5, dest="silence_seconds",
        help="Seconds of quiet that end a turn and trigger extraction.",
    )
    p.add_argument(
        "--transcript-mode", choices=("SMART", "VERBATIM"), default="SMART",
        help="SMART strips filler words and formats alphanumerics.",
    )
    p.add_argument(
        "--input-device", type=int, default=None,
        help="Input device index (see --list-devices).",
    )
    p.add_argument(
        "--list-devices", action="store_true",
        help="Print available audio input devices and exit.",
    )
    p.add_argument(
        "--text", action="store_true",
        help="Skip the microphone and read transcripts from stdin, one turn "
             "per line. Exercises the extraction leg alone — useful for "
             "iterating on the prompt without talking.",
    )
    return p


def resolve(args: argparse.Namespace) -> Settings:
    """Turn parsed args + environment into a validated Settings."""
    shared = resolve_shared(args)

    if args.languages is not None:
        raw_languages = args.languages
    else:
        raw_languages = os.environ.get("STT_LANGUAGES", ",".join(DEFAULT_LANGUAGES))
    if raw_languages.strip().lower() in ("auto", ""):
        languages: list[str] = []  # empty list = automatic detection
    else:
        languages = [code.strip() for code in raw_languages.split(",") if code.strip()]

    if args.silence_seconds <= 0:
        raise SystemExit("--silence must be greater than 0")

    return Settings(
        **shared,
        languages=languages,
        silence_seconds=args.silence_seconds,
        input_device=args.input_device,
        text_mode=args.text,
        transcript_mode=args.transcript_mode,
        lock_languages=args.lock_languages,
    )
