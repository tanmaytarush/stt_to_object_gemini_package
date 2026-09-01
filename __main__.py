"""CLI entry point: wires mic -> transcriber -> extractor -> console/starship."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Optional

from . import config
from .audio import MicStream, list_devices
from .exceptions import MicrophoneError
from .extractor import Extractor, TurnResult
from .schemas import ValidationFailure
from .sink import StarshipClient, curl_for, render_body
from .transcriber import Final, Interim, Notice, Transcriber

# --- console ----------------------------------------------------------------

_COLOR = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


DIM = lambda s: _c(s, "2")          # noqa: E731
BOLD = lambda s: _c(s, "1")         # noqa: E731
GREEN = lambda s: _c(s, "32")       # noqa: E731
YELLOW = lambda s: _c(s, "33")      # noqa: E731
RED = lambda s: _c(s, "31")         # noqa: E731
CYAN = lambda s: _c(s, "36")        # noqa: E731

_line_dirty = False


def clear_line() -> None:
    """Erase the in-place interim line before printing anything permanent."""
    global _line_dirty
    if _line_dirty:
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
        _line_dirty = False


def status(text: str) -> None:
    """Write to the single rewriting status line."""
    global _line_dirty
    sys.stdout.write("\r\033[K" + text)
    sys.stdout.flush()
    _line_dirty = True


def say(text: str = "") -> None:
    clear_line()
    print(text)


# --- verbose trace ----------------------------------------------------------


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


# --- record presentation ----------------------------------------------------

def _row_text(row: object) -> str:
    """One item as the order screen shows it: item · qty · uom."""
    if isinstance(row, dict):
        name = row.get("itemName") or row.get("item_name")
        return f"{row.get('quantity')} {row.get('uom')}  {name}"
    if hasattr(row, "item_name"):
        return f"{row.quantity} {row.uom}  {row.item_name}"
    return str(row)


def show_progress(result: TurnResult, extractor: Extractor) -> None:
    """Print the rows this turn added and the list as it now stands."""
    for warning in result.warnings:
        say(f"  {YELLOW('!')} {warning}")

    if result.error:
        say(f"  {RED('x')} {result.error}")
        return

    extraction = result.extraction
    if extraction is None:
        return

    # A gated turn is not an empty one. Show what was heard and thrown away, or
    # the speaker has no idea which part to repeat.
    for row in result.ignored_fields.get("items") or []:
        say(f"  {DIM('heard but not kept: ' + _row_text(row))}")

    if extraction.entity == "NONE":
        # The note comes BEFORE the return: a bail that explains nothing is how
        # a turn full of real content used to vanish silently.
        if extraction.notes:
            say(f"  {DIM('· no items: ' + extraction.notes)}")
        else:
            say(f"  {DIM('· no items in that — still listening')}")
        return

    added = result.changed.get("items") or []
    if added:
        marker = "replaced with" if extraction.is_correction else "added"
        say(f"  {CYAN(marker)}")
        for row in added:
            say(f"      {_row_text(row)}")
        total = len(extractor.fields.get("items") or [])
        say(f"  {DIM(f'{total} item(s) in the list')}")
    elif not result.warnings:
        # Classified as ITEMS but nothing usable came out of it — often a turn
        # that was really about the dealer or the price. Say so, or the speaker
        # is left thinking it landed.
        say(f"  {DIM('· no items in that — still listening')}")

    if extraction.notes:
        say(f"  {DIM('note: ' + extraction.notes)}")

    if result.validation_error:
        say(f"  {RED('invalid')} {result.validation_error}")
    elif result.missing:
        say(f"  {DIM('still need: ' + ', '.join(result.missing))}")


# --- interactive prompts ----------------------------------------------------


def show_ready(settings: config.Settings, dto) -> None:
    """Print a complete, valid order as the exact body that would be POSTed."""
    if dto is None:
        return
    endpoint = settings.order_url
    say()
    say(BOLD(f"  ── {dto.entity_label} ready ── POST {endpoint}"))
    for line in render_body(dto).splitlines():
        say("  " + GREEN(line))


async def ask(prompt: str) -> str:
    """Blocking stdin read moved off the event loop so audio keeps streaming."""
    clear_line()
    return (await asyncio.to_thread(input, prompt)).strip()


def _parse_row(raw: str) -> Optional[dict]:
    """`cement, 10, bag` -> one order item. None if it is not three parts."""
    bits = [bit.strip() for bit in raw.split(",")]
    if len(bits) != 3:
        return None
    name, qty_raw, uom = bits
    try:
        qty = int(qty_raw)
    except ValueError:
        return None
    if not name or qty <= 0 or not uom:
        return None
    return {"itemName": name, "quantity": qty, "uom": uom}


async def edit_record(extractor: Extractor) -> None:
    """Fix one item row by hand — the fastest cure for a mis-heard material."""
    rows = list(extractor.fields.get("items") or [])
    for i, row in enumerate(rows):
        say(f"  {i + 1}) {_row_text(row)}")

    choice = await ask("  row number to fix, 'a' to add one, blank to cancel > ")
    if choice.lower() == "a":
        raw = await ask("  new row (item, qty, uom) = ")
        row = _parse_row(raw)
        if row is None:
            say(f"  {RED('expected item, qty, uom')} — nothing added")
            return
        extractor.set_field("items", rows + [row])
        say(f"  {GREEN('added')} {_row_text(row)}")
        return

    if not choice.isdigit() or not 1 <= int(choice) <= len(rows):
        say(f"  {DIM('cancelled')}")
        return

    index = int(choice) - 1
    raw = await ask(f"  row {choice} (item, qty, uom · '-' deletes) "
                    f"[{_row_text(rows[index])}] = ")
    if raw == "":
        say(f"  {DIM('unchanged')}")
        return
    if raw == "-":
        removed = rows.pop(index)
        extractor.set_field("items", rows or None)
        say(f"  {DIM('deleted ' + _row_text(removed))}")
        return

    row = _parse_row(raw)
    if row is None:
        say(f"  {RED('expected item, qty, uom')} — unchanged")
        return
    rows[index] = row
    extractor.set_field("items", rows)
    say(f"  {GREEN('set')} {_row_text(row)}")


async def confirm_and_maybe_post(
    settings: config.Settings,
    extractor: Extractor,
    api: Optional[StarshipClient],
    trace: Trace,
) -> None:
    """Present a complete record and let the user decide what happens to it."""
    while True:
        dto = extractor.current_dto()
        if dto is None:
            return
        try:
            dto.validate_for_api()
        except ValidationFailure as exc:
            say(f"  {RED('invalid')} {exc}")
            return

        show_ready(settings, dto)

        if settings.post:
            options = "[y] post  [e] edit an item  [k] keep adding  [d] discard  [q] quit"
        else:
            options = "[Enter] next order  [e] edit an item  [k] keep adding  [q] quit"
        choice = (await ask(f"  {options} > ")).lower()

        if choice == "e":
            await edit_record(extractor)
            continue
        if choice == "q":
            raise KeyboardInterrupt
        if choice == "k":
            say(f"  {DIM('list left open — keep speaking to add items')}")
            return
        if choice == "d" or not settings.post:
            # Outside --post there is nothing else to do with a finished order,
            # so Enter clears it and the next utterance starts clean. Without
            # this, the next order's items would append to this one.
            extractor.reset_record()
            say(f"  {DIM('cleared — ready for the next order')}")
            return
        if choice != "y":
            say(f"  {DIM('list left open — keep speaking to add items')}")
            return

        if settings.dry_run:
            say(f"  {DIM('--dry-run, not sending:')}")
            for line in curl_for(settings, dto).splitlines():
                say("  " + DIM(line))
            trace.write("dry_run", body=dto.request_body())
            extractor.reset_record()
            return

        assert api is not None
        code, body = await api.create(dto)
        trace.write("post", status=code, body=dto.request_body(), response=body)
        if code is None:
            say(f"  {RED('x')} {body}")
            say(f"  {DIM('list kept — press y to retry')}")
            continue
        colorize = GREEN if 200 <= code < 300 else RED
        say(f"  {colorize(f'HTTP {code}')}")
        for line in body.splitlines():
            say("  " + DIM(line))
        if 200 <= code < 300:
            extractor.reset_record()
            say(f"  {DIM('ready for the next order')}")
            return
        say(f"  {DIM('list kept — [e] to fix an item, then y to retry')}")


# --- main loop --------------------------------------------------------------


def banner(settings: config.Settings) -> None:
    sent = settings.stt_language_codes
    if sent:
        languages = ", ".join(sent) + " (locked)"
    else:
        languages = "auto-detect — follows whatever the user is speaking"
    mode = "POST enabled" if settings.post else "print only"
    if settings.post and settings.dry_run:
        mode = "dry-run (prints curl)"
    say(BOLD("starship voice logging"))
    if settings.text_mode:
        say(DIM("  input      stdin (--text; microphone and STT are skipped)"))
    else:
        say(DIM(f"  stt        {config.STT_MODEL} ({settings.transcript_mode}, {languages})"))
    say(DIM(f"  extract    {settings.extract_model}"))
    say(DIM(f"  contractor {settings.contractor_id}   user {settings.user_id}   {mode}"))
    screen = [settings.order_type]
    if settings.client_id:
        screen.append(f"client {settings.client_id}")
    if settings.dealer_id:
        screen.append(f"dealer {settings.dealer_id}")
    say(DIM(f"  screen     {' · '.join(screen)}   (voice fills items only)"))
    if settings.text_mode:
        say(DIM("  one transcript per line · blank line clears the list · Ctrl+D to stop"))
    else:
        say(DIM(f"  turn ends after {settings.silence_seconds:g}s of quiet · Ctrl+C to stop"))
    say()


async def handle_turn(
    settings: config.Settings,
    extractor: Extractor,
    api: Optional[StarshipClient],
    trace: Trace,
) -> None:
    """Extract the buffered turn, show what changed, and offer a complete record."""
    result = await extractor.flush()
    clear_line()
    if result is None:
        return
    trace.write(
        "extract",
        turn=result.turn_text,
        extraction=result.extraction.model_dump() if result.extraction else None,
        state=extractor.fields,
        missing=result.missing,
        warnings=result.warnings,
    )
    show_progress(result, extractor)
    if not result.is_complete:
        return

    if settings.text_mode and not settings.post:
        # In --text mode the confirm prompt would read from the same stdin the
        # transcripts come from, so a piped script would feed its next line to
        # the prompt. There is already an explicit order boundary here (a blank
        # line), so just show the body and leave the list open for follow-ups.
        show_ready(settings, extractor.current_dto())
        return

    await confirm_and_maybe_post(settings, extractor, api, trace)


async def run_text_mode(
    settings: config.Settings,
    extractor: Extractor,
    api: Optional[StarshipClient],
    trace: Trace,
) -> None:
    """Read transcripts from stdin, one turn per line.

    The extraction leg on its own: no microphone, no STT quota, and the same
    merge/validate/present path the live loop uses.
    """
    while True:
        try:
            line = (await asyncio.to_thread(input, "> ")).strip()
        except EOFError:
            return
        if not line:
            extractor.reset_record()
            say(f"  {DIM('cleared — ready for the next order')}")
            continue
        extractor.add_final(line)
        trace.write("final", text=line)
        status(f"  {DIM('extracting…')}")
        await handle_turn(settings, extractor, api, trace)


async def run(settings: config.Settings) -> None:
    trace = Trace(settings)
    api = StarshipClient(settings) if settings.post and not settings.dry_run else None
    extractor = Extractor(settings)

    try:
        if settings.text_mode:
            await run_text_mode(settings, extractor, api, trace)
            return

        async with MicStream(settings) as mic:
            transcriber = Transcriber(settings, mic)
            supervisor = asyncio.create_task(transcriber.run(), name="stt-supervisor")
            events = transcriber.events()

            last_final_at: float | None = None
            pending_event: asyncio.Task | None = None
            silent_since = time.monotonic()
            hinted_dead_mic = False
            heard_speech = False
            if mic.device_label:
                say(DIM(f"  mic        {mic.device_label}"))

            try:
                while True:
                    if pending_event is None:
                        pending_event = asyncio.create_task(anext(events, None))

                    if last_final_at is not None:
                        timeout = max(
                            0.05,
                            settings.silence_seconds - (time.monotonic() - last_final_at),
                        )
                    else:
                        timeout = 0.25  # keep the level meter alive while idle

                    done, _ = await asyncio.wait({pending_event}, timeout=timeout)

                    if pending_event in done:
                        event = pending_event.result()
                        pending_event = None
                        if event is None:  # transcriber shut down
                            break

                        if isinstance(event, Interim):
                            heard_speech = True
                            status(f"  {DIM('~ ' + event.text)}  {DIM(mic.meter())}")
                        elif isinstance(event, Final):
                            heard_speech = True
                            say(f"  {DIM('·')} {event.text}")
                            extractor.add_final(event.text)
                            trace.write("final", text=event.text)
                            last_final_at = time.monotonic()
                        elif isinstance(event, Notice):
                            paint = RED if event.is_error else DIM
                            say(f"  {paint(event.text)}")
                        continue

                    # Timed out. Either a turn went quiet, or we're just idling.
                    if extractor.has_turn and last_final_at is not None:
                        if time.monotonic() - last_final_at >= settings.silence_seconds:
                            last_final_at = None
                            status(f"  {DIM('extracting…')}")
                            await handle_turn(settings, extractor, api, trace)
                    elif not extractor.has_turn:
                        if mic.is_silent() and not heard_speech:
                            if (
                                not hinted_dead_mic
                                and time.monotonic() - silent_since >= 4
                            ):
                                hinted_dead_mic = True
                                say(
                                    YELLOW(
                                        "  no audio yet — grant Cursor (not just Terminal) "
                                        "mic access in System Settings → Privacy & Security "
                                        "→ Microphone, then re-run with --input-device 0"
                                    )
                                )
                        else:
                            silent_since = time.monotonic()
                        status(f"  {DIM('listening')}  {DIM(mic.meter())}")
            finally:
                if pending_event is not None:
                    pending_event.cancel()
                transcriber.stop()
                supervisor.cancel()
                try:
                    await supervisor
                except (asyncio.CancelledError, Exception):
                    pass
    finally:
        if api is not None:
            await api.aclose()


def main() -> int:
    parser = config.build_parser()
    args = parser.parse_args()

    if args.list_devices:
        try:
            print(list_devices())
        except MicrophoneError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0

    settings = config.resolve(args)
    banner(settings)

    try:
        asyncio.run(run(settings))
    except MicrophoneError as exc:
        clear_line()
        print(RED(str(exc)), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass

    clear_line()
    print(DIM("stopped."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
