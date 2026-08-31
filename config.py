"""Settings resolution: CLI flag > environment variable > default."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path

# --- Models -----------------------------------------------------------------

# Live streaming speech-to-text. Sessions are capped at 10 minutes by the
# service, which is why transcriber.py rotates the connection proactively.
STT_MODEL = "gemini-3.5-transcribe-live"

# Text model that turns a finished spoken turn into a typed Extraction.
#
# Measured on a real Kannada turn with this system prompt:
#   gemini-3.5-flash-lite   1.4– 1.8s
#   gemini-3.5-flash        6.9–15.2s (and flaky: intermittent 503)
#   gemini-3.6-flash        free-tier quota is 20 requests/day, per model
# Both 3.5 models produced identical, correct extractions, so lite wins on the
# only axis that separated them. At ~15s a turn the loop stops feeling live;
# at ~1.5s it keeps up with speech.
#
# Override with EXTRACT_MODEL in .env or the environment to try another id.
EXTRACT_MODEL = "gemini-3.5-flash-lite"

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

# --- Settings ---------------------------------------------------------------


@dataclass
class Settings:
    api_key: str
    contractor_id: int
    user_id: str
    base_url: str
    languages: list[str]
    silence_seconds: float
    min_confidence: float
    name_script: str
    input_device: int | None
    text_mode: bool
    post: bool
    dry_run: bool
    strict_entity: bool
    verbose: bool
    log_path: Path
    transcript_mode: str
    extract_model: str = EXTRACT_MODEL
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

    @property
    def client_url(self) -> str:
        return f"{self.base_url}/starship/v1/client"

    @property
    def dealer_url(self) -> str:
        return f"{self.base_url}/starship/v1/material-dealer"


def _load_dotenv() -> None:
    """Populate os.environ from scripts/voice_logging/.env if it exists.

    Deliberately hand-rolled: the repo has no python-dotenv dependency and the
    format we need is trivial. Existing environment variables always win.
    """
    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m voice_logging",
        description=(
            "Speak Hindi/Gujarati/Marathi/Indian-English into the mic; get a "
            "validated starship CreateClient/CreateDealer request body out."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--contractor-id", type=int, default=None,
                   help="Owning contractor. Env: CONTRACTOR_ID. Never inferred from speech.")
    p.add_argument("--user-id", default=None,
                   help="Sent as X-USER-ID; drives created_by. Env: STARSHIP_USER_ID.")
    p.add_argument("--base-url", default=None,
                   help="starship base URL. Env: STARSHIP_BASE_URL.")
    p.add_argument("--languages", default=None,
                   help="Comma-separated BCP-47 codes, or 'auto'. A single code locks "
                        "that language. Two or more are kept as a note only — Gemini "
                        "auto-detects the language being spoken (Hindi+Kannada together "
                        "used to force Devanagari). Env: STT_LANGUAGES.")
    p.add_argument("--lock-languages", action="store_true",
                   help="Send the full --languages / STT_LANGUAGES list to Gemini as "
                        "hints. Off by default so spoken language wins.")
    p.add_argument("--silence", type=float, default=1.5, dest="silence_seconds",
                   help="Seconds of quiet that end a turn and trigger extraction.")
    p.add_argument("--min-confidence", type=float, default=0.55,
                   help="Discard extractions below this confidence.")
    p.add_argument("--name-script", choices=("latin", "native"), default="latin",
                   help="Romanize names, or keep them in the spoken script.")
    p.add_argument("--transcript-mode", choices=("SMART", "VERBATIM"), default="SMART",
                   help="SMART strips filler words and formats alphanumerics.")
    p.add_argument("--input-device", type=int, default=None,
                   help="Input device index (see --list-devices).")
    p.add_argument("--list-devices", action="store_true",
                   help="Print available audio input devices and exit.")
    p.add_argument("--text", action="store_true",
                   help="Skip the microphone and read transcripts from stdin, one turn "
                        "per line. Exercises the extraction leg alone — useful for "
                        "iterating on the prompt without talking.")
    p.add_argument("--post", action="store_true",
                   help="Offer to POST each completed object to starship.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the equivalent curl instead of sending. Implies --post.")
    p.add_argument("--strict-entity", action="store_true",
                   help="Refuse to switch between CLIENT and DEALER mid-object.")
    p.add_argument("--verbose", action="store_true",
                   help="Append a JSONL trace of every stage to --log-file.")
    p.add_argument("--log-file", default="voice_logging.log",
                   help="Where --verbose writes its JSONL trace.")
    return p


def resolve(args: argparse.Namespace) -> Settings:
    """Turn parsed args + environment into a validated Settings.

    Raises SystemExit with an actionable message rather than a traceback, since
    every failure here is a setup problem the user can fix directly.
    """
    _load_dotenv()

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "GEMINI_API_KEY is not set.\n"
            "  export GEMINI_API_KEY=...   (get one at https://aistudio.google.com/apikey)\n"
            "  or copy scripts/voice_logging/.env.example to .env and fill it in."
        )

    raw_contractor = args.contractor_id
    if raw_contractor is None:
        env_value = os.environ.get("CONTRACTOR_ID", "").strip()
        if env_value:
            try:
                raw_contractor = int(env_value)
            except ValueError:
                raise SystemExit(f"CONTRACTOR_ID must be an integer, got {env_value!r}")
    if raw_contractor is None:
        raise SystemExit(
            "contractorId is required and cannot be inferred from speech.\n"
            "  pass --contractor-id N, or set CONTRACTOR_ID in the environment."
        )
    if raw_contractor <= 0:
        raise SystemExit("contractorId must be greater than 0 (the API rejects 0).")

    if args.languages is not None:
        raw_languages = args.languages
    else:
        raw_languages = os.environ.get("STT_LANGUAGES", ",".join(DEFAULT_LANGUAGES))
    if raw_languages.strip().lower() in ("auto", ""):
        languages: list[str] = []  # empty list = automatic detection
    else:
        languages = [code.strip() for code in raw_languages.split(",") if code.strip()]

    if not 0.0 <= args.min_confidence <= 1.0:
        raise SystemExit("--min-confidence must be between 0.0 and 1.0")
    if args.silence_seconds <= 0:
        raise SystemExit("--silence must be greater than 0")

    base_url = (args.base_url or os.environ.get("STARSHIP_BASE_URL")
                or "http://localhost:8092").rstrip("/")

    # --dry-run only means anything in the POST flow, so let it imply --post
    # rather than silently doing nothing on its own.
    post = args.post or args.dry_run

    return Settings(
        api_key=api_key,
        contractor_id=raw_contractor,
        user_id=args.user_id or os.environ.get("STARSHIP_USER_ID") or "1",
        base_url=base_url,
        languages=languages,
        silence_seconds=args.silence_seconds,
        min_confidence=args.min_confidence,
        name_script=args.name_script,
        input_device=args.input_device,
        text_mode=args.text,
        post=post,
        dry_run=args.dry_run,
        strict_entity=args.strict_entity,
        verbose=args.verbose,
        log_path=Path(args.log_file),
        transcript_mode=args.transcript_mode,
        extract_model=(os.environ.get("EXTRACT_MODEL") or EXTRACT_MODEL).strip(),
        lock_languages=args.lock_languages,
    )
