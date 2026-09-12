"""CLI: image -> Flash OCR -> console / starship."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Optional

from ..common.sink import StarshipClient, curl_for, render_body
from . import config
from .extractor import OcrExtractor, OcrResult

_COLOR = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


DIM = lambda s: _c(s, "2")          # noqa: E731
BOLD = lambda s: _c(s, "1")         # noqa: E731
GREEN = lambda s: _c(s, "32")       # noqa: E731
YELLOW = lambda s: _c(s, "33")      # noqa: E731
RED = lambda s: _c(s, "31")         # noqa: E731
CYAN = lambda s: _c(s, "36")        # noqa: E731


def say(text: str = "") -> None:
    print(text)


class Trace:
    def __init__(self, settings: config.Settings) -> None:
        self._enabled = settings.verbose
        self._path = settings.log_path
        if self._enabled:
            self._path.write_text("", encoding="utf-8")

    def write(self, stage: str, **payload) -> None:
        if not self._enabled:
            return
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "stage": stage, **payload}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _row_text(row: object) -> str:
    if isinstance(row, dict):
        name = row.get("itemName") or row.get("item_name")
        return f"{row.get('quantity')} {row.get('uom')}  {name}"
    if hasattr(row, "item_name"):
        return f"{row.quantity} {row.uom}  {row.item_name}"
    return str(row)


def banner(settings: config.Settings) -> None:
    mode = "POST enabled" if settings.post else "print only"
    if settings.post and settings.dry_run:
        mode = "dry-run (prints curl)"
    say(BOLD("starship ocr logging"))
    say(DIM(f"  extract    {settings.extract_model}"))
    say(DIM(f"  contractor {settings.contractor_id}   user {settings.user_id}   {mode}"))
    screen = [settings.order_type]
    if settings.client_id:
        screen.append(f"client {settings.client_id}")
    if settings.dealer_id:
        screen.append(f"dealer {settings.dealer_id}")
    say(DIM(f"  screen     {' · '.join(screen)}   (image fills items only)"))
    for path in settings.image_paths:
        say(DIM(f"  image      {path}"))
    say()


def show_result(result: OcrResult) -> None:
    for warning in result.warnings:
        say(f"  {YELLOW('!')} {warning}")

    if result.error:
        say(f"  {RED('x')} {result.error}")
        return

    extraction = result.extraction
    if extraction is None:
        return

    if extraction.raw_text:
        say(f"  {DIM('read:')}")
        for line in extraction.raw_text.splitlines():
            say(f"  {DIM('  ' + line)}")

    for row in result.ignored_fields.get("items") or []:
        say(f"  {DIM('seen but not kept: ' + _row_text(row))}")

    if extraction.entity == "NONE" or result.dto is None:
        if extraction.notes:
            say(f"  {DIM('· no items: ' + extraction.notes)}")
        else:
            say(f"  {DIM('· no items in that image')}")
        return

    rows = result.dto.orderItems
    say(f"  {CYAN('items')}")
    for row in rows:
        say(f"      {_row_text(row)}")
    say(f"  {DIM(f'{len(rows)} item(s) in the list')}")

    if extraction.notes:
        say(f"  {DIM('note: ' + extraction.notes)}")
    if result.validation_error:
        say(f"  {RED('invalid')} {result.validation_error}")
    elif result.missing:
        say(f"  {DIM('still need: ' + ', '.join(result.missing))}")


def show_ready(settings: config.Settings, dto) -> None:
    if dto is None:
        return
    say()
    say(BOLD(f"  ── {dto.entity_label} ready ── POST {settings.order_url}"))
    for line in render_body(dto).splitlines():
        say("  " + GREEN(line))


async def maybe_post(
    settings: config.Settings,
    dto,
    api: Optional[StarshipClient],
    trace: Trace,
) -> int:
    if settings.dry_run:
        say(f"  {DIM('--dry-run, not sending:')}")
        for line in curl_for(settings, dto).splitlines():
            say("  " + DIM(line))
        trace.write("dry_run", body=dto.request_body())
        return 0

    assert api is not None
    code, body = await api.create(dto)
    trace.write("post", status=code, body=dto.request_body(), response=body)
    if code is None:
        say(f"  {RED('x')} {body}")
        return 1
    colorize = GREEN if 200 <= code < 300 else RED
    say(f"  {colorize(f'HTTP {code}')}")
    for line in body.splitlines():
        say("  " + DIM(line))
    return 0 if 200 <= code < 300 else 1


async def run(settings: config.Settings) -> int:
    trace = Trace(settings)
    api = StarshipClient(settings) if settings.post and not settings.dry_run else None
    extractor = OcrExtractor(settings)
    try:
        result = await extractor.extract_paths()
        trace.write(
            "extract",
            source=result.source,
            extraction=result.extraction.model_dump() if result.extraction else None,
            missing=result.missing,
            warnings=result.warnings,
            error=result.error,
        )
        show_result(result)
        if result.error:
            return 1
        if not result.is_complete:
            return 1
        show_ready(settings, result.dto)
        if settings.post:
            return await maybe_post(settings, result.dto, api, trace)
        return 0
    finally:
        if api is not None:
            await api.aclose()


def main() -> int:
    parser = config.build_parser()
    args = parser.parse_args()
    settings = config.resolve(args)
    banner(settings)
    try:
        return asyncio.run(run(settings))
    except KeyboardInterrupt:
        say(DIM("stopped."))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
