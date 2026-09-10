"""Shared settings: Gemini extract model, screen context, dotenv.

CLI flag > environment variable > default. Audio- and OCR-specific knobs
live in those modules; this file is the overlap — API key, the Flash model
both extractors call, and the order-screen identifiers that never come from
speech or an image.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

# Text model that turns a finished input (spoken turn or image) into a typed
# item list.
#
# Measured on a real Kannada turn with the audio system prompt:
#   gemini-3.5-flash-lite   1.4– 1.8s
#   gemini-3.5-flash        6.9–15.2s (and flaky: intermittent 503)
#   gemini-3.6-flash        free-tier quota is 20 requests/day, per model
# Both 3.5 models produced identical, correct extractions, so lite wins on the
# only axis that separated them. Override with EXTRACT_MODEL in .env.
EXTRACT_MODEL = "gemini-3.5-flash-lite"

# Package root (the directory that holds .env), not this file's parent.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class BaseSettings:
    """Screen context + Gemini credentials shared by every input module."""

    api_key: str
    contractor_id: int
    client_id: int | None
    dealer_id: int | None
    order_type: str
    user_id: str
    base_url: str
    min_confidence: float
    post: bool
    dry_run: bool
    verbose: bool
    log_path: Path
    extract_model: str = EXTRACT_MODEL

    @property
    def order_url(self) -> str:
        return f"{self.base_url}/starship/v1/material-order"


def load_dotenv() -> None:
    """Populate os.environ from the package .env if it exists.

    Deliberately hand-rolled: the repo has no python-dotenv dependency and the
    format we need is trivial. Existing environment variables always win.
    """
    env_file = PACKAGE_ROOT / ".env"
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


def add_shared_arguments(parser: argparse.ArgumentParser) -> None:
    """Flags that both the audio and OCR CLIs accept."""
    parser.add_argument(
        "--contractor-id", type=int, default=None,
        help="Owning contractor. Env: CONTRACTOR_ID. Never inferred from input.",
    )
    parser.add_argument(
        "--client-id", type=int, default=None,
        help="Existing client picked on the order screen. Optional, "
             "like the screen's dropdown. Env: CLIENT_ID.",
    )
    parser.add_argument(
        "--dealer-id", type=int, default=None,
        help="Existing dealer picked on the order screen. Optional, "
             "like the screen's dropdown. Env: DEALER_ID.",
    )
    parser.add_argument(
        "--order-type", choices=("MAT_ORDER", "MAT_LIST"), default=None,
        help="Set by the screen, never by audio or OCR. Env: ORDER_TYPE. "
             "(default: MAT_ORDER)",
    )
    parser.add_argument(
        "--user-id", default=None,
        help="Sent as X-USER-ID; drives created_by. Env: STARSHIP_USER_ID.",
    )
    parser.add_argument(
        "--base-url", default=None,
        help="starship base URL. Env: STARSHIP_BASE_URL.",
    )
    parser.add_argument(
        "--min-confidence", type=float, default=0.55,
        help="Discard extractions below this confidence.",
    )
    parser.add_argument(
        "--post", action="store_true",
        help="Offer to POST each completed object to starship.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the equivalent curl instead of sending. Implies --post.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Append a JSONL trace of every stage to --log-file.",
    )
    parser.add_argument(
        "--log-file", default="voice_logging.log",
        help="Where --verbose writes its JSONL trace.",
    )


def optional_positive_id(cli_value: int | None, env_name: str, label: str) -> int | None:
    raw = cli_value
    if raw is None:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            try:
                raw = int(env_value)
            except ValueError:
                raise SystemExit(f"{env_name} must be an integer, got {env_value!r}")
    if raw is None:
        return None
    if raw <= 0:
        raise SystemExit(f"{label} must be greater than 0")
    return raw


def resolve_shared(args: argparse.Namespace) -> dict:
    """Validate the shared CLI/env surface into BaseSettings kwargs.

    Raises SystemExit with an actionable message rather than a traceback, since
    every failure here is a setup problem the user can fix directly.
    """
    load_dotenv()

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "GEMINI_API_KEY is not set.\n"
            "  export GEMINI_API_KEY=...   (get one at https://aistudio.google.com/apikey)\n"
            "  or copy .env.example to .env and fill it in."
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
            "contractorId is required and cannot be inferred from input.\n"
            "  pass --contractor-id N, or set CONTRACTOR_ID in the environment."
        )
    if raw_contractor <= 0:
        raise SystemExit("contractorId must be greater than 0 (the API rejects 0).")

    client_id = optional_positive_id(args.client_id, "CLIENT_ID", "clientId")
    dealer_id = optional_positive_id(args.dealer_id, "DEALER_ID", "dealerId")

    order_type = (args.order_type or os.environ.get("ORDER_TYPE") or "MAT_ORDER").strip().upper()
    if order_type not in ("MAT_ORDER", "MAT_LIST"):
        raise SystemExit(f"orderType must be MAT_ORDER or MAT_LIST, got {order_type!r}")

    if not 0.0 <= args.min_confidence <= 1.0:
        raise SystemExit("--min-confidence must be between 0.0 and 1.0")

    base_url = (args.base_url or os.environ.get("STARSHIP_BASE_URL")
                or "http://localhost:8092").rstrip("/")

    # --dry-run only means anything in the POST flow, so let it imply --post
    # rather than silently doing nothing on its own.
    post = args.post or args.dry_run

    return dict(
        api_key=api_key,
        contractor_id=raw_contractor,
        client_id=client_id,
        dealer_id=dealer_id,
        order_type=order_type,
        user_id=args.user_id or os.environ.get("STARSHIP_USER_ID") or "1",
        base_url=base_url,
        min_confidence=args.min_confidence,
        post=post,
        dry_run=args.dry_run,
        verbose=args.verbose,
        log_path=Path(args.log_file),
        extract_model=(os.environ.get("EXTRACT_MODEL") or EXTRACT_MODEL).strip(),
    )
