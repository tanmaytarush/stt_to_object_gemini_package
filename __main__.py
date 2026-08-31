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
from .schemas import ClientDto, ValidationFailure
from .sink import StarshipClient, curl_for, describe_match, render_body
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

_FIELD_LABELS = {
    "name": "name",
    "phone_number": "phoneNumber",
    "project_type": "projectType",
    "total_amount": "totalAmount",
}


def show_progress(result: TurnResult, extractor: Extractor) -> None:
    """Print what the turn changed and what the record still needs."""
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
    if result.ignored_fields:
        parts = [f"{_FIELD_LABELS.get(k, k)}={v!r}" for k, v in result.ignored_fields.items()]
        say(f"  {DIM('heard but not kept: ' + '  '.join(parts))}")

    # A second entity in the same turn is never captured, so it must always be
    # said out loud — otherwise the speaker believes it was recorded.
    if result.also_heard:
        say(f"  {YELLOW('!')} also heard {result.also_heard} — not captured, "
            f"say it again on its own")

    if extraction.entity == "NONE":
        # The note comes BEFORE the return: a bail that explains nothing is how
        # a turn full of real content used to vanish silently.
        if extraction.notes:
            say(f"  {DIM('· no record: ' + extraction.notes)}")
        else:
            say(f"  {DIM('· no record in that — still listening')}")
        return

    if not result.changed and not result.dto:
        if extraction.notes:
            say(f"  {DIM('note: ' + extraction.notes)}")
        return

    if result.changed:
        parts = [f"{_FIELD_LABELS.get(k, k)}={v!r}" for k, v in result.changed.items()]
        marker = "corrected" if extraction.is_correction else "captured"
        say(f"  {CYAN(marker)} [{extraction.entity}] " + "  ".join(parts))

    if extraction.notes:
        say(f"  {DIM('note: ' + extraction.notes)}")

    if result.validation_error:
        say(f"  {RED('invalid')} {result.validation_error}")
    elif result.missing:
        say(f"  {DIM('still need: ' + ', '.join(result.missing))}")


# --- interactive prompts ----------------------------------------------------


def show_ready(settings: config.Settings, dto) -> None:
    """Print a complete, valid record as the exact body that would be POSTed."""
    if dto is None:
        return
    endpoint = settings.client_url if isinstance(dto, ClientDto) else settings.dealer_url
    say()
    say(BOLD(f"  ── {dto.entity_label} ready ── POST {endpoint}"))
    for line in render_body(dto).splitlines():
        say("  " + GREEN(line))


async def ask(prompt: str) -> str:
    """Blocking stdin read moved off the event loop so audio keeps streaming."""
    clear_line()
    return (await asyncio.to_thread(input, prompt)).strip()


async def edit_record(extractor: Extractor) -> None:
    """Type a value for one field. The fastest fix when a field keeps mis-hearing."""
    editable = ["name", "phone_number"]
    if extractor.entity == "CLIENT":
        editable += ["project_type", "total_amount"]

    say("  fields: " + ", ".join(f"{i + 1}) {_FIELD_LABELS[f]}" for i, f in enumerate(editable)))
    choice = await ask("  edit which? (number, or blank to cancel) ")
    if not choice.isdigit() or not 1 <= int(choice) <= len(editable):
        say(f"  {DIM('cancelled')}")
        return

    key = editable[int(choice) - 1]
    current = extractor.fields.get(key)
    hint = {
        "project_type": " (MATERIAL_AND_LABOUR | LABOUR_ONLY)",
        "phone_number": " (10 digits)",
        "total_amount": " (rupees)",
    }.get(key, "")
    raw = await ask(f"  {_FIELD_LABELS[key]}{hint} [{current if current is not None else ''}] = ")

    if raw == "":
        say(f"  {DIM('unchanged')}")
        return
    if raw == "-":
        extractor.set_field(key, None)
        say(f"  {DIM(_FIELD_LABELS[key] + ' cleared')}")
        return

    if key == "total_amount":
        try:
            extractor.set_field(key, float(raw.replace(",", "")))
        except ValueError:
            say(f"  {RED('not a number')} — unchanged")
            return
    elif key == "project_type":
        value = raw.strip().upper()
        if value not in ("MATERIAL_AND_LABOUR", "LABOUR_ONLY"):
            say(f"  {RED('must be MATERIAL_AND_LABOUR or LABOUR_ONLY')} — unchanged")
            return
        extractor.set_field(key, value)
    else:
        extractor.set_field(key, raw)

    say(f"  {GREEN('set')} {_FIELD_LABELS[key]} = {extractor.fields.get(key)!r}")


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

        if settings.post and api is not None:
            wanted = (dto.clientName if isinstance(dto, ClientDto) else dto.dealerName) or ""
            matches, probe_error = await api.find_similar(dto)
            if probe_error:
                say(f"  {DIM(probe_error)}")
            elif matches:
                say(f"  {YELLOW(f'! {len(matches)} existing record(s) with a similar name:')}")
                for match in matches[:5]:
                    say(f"      {describe_match(match, wanted)}")
                if len(matches) > 5:
                    say(f"      {DIM(f'… and {len(matches) - 5} more')}")

        if settings.post:
            options = "[y] post  [e] edit  [k] keep refining  [d] discard  [q] quit"
        else:
            options = "[Enter] next record  [e] edit  [k] keep refining  [q] quit"
        choice = (await ask(f"  {options} > ")).lower()

        if choice == "e":
            await edit_record(extractor)
            continue
        if choice == "q":
            raise KeyboardInterrupt
        if choice == "k":
            say(f"  {DIM('record left open — keep speaking to amend it')}")
            return
        if choice == "d" or not settings.post:
            # Outside --post there is nothing else to do with a finished record,
            # so Enter clears it and the next utterance starts clean. Without
            # this, a second client's fields would merge into the first.
            extractor.reset_record()
            say(f"  {DIM('cleared — ready for the next record')}")
            return
        if choice != "y":
            say(f"  {DIM('record left open — keep speaking to amend it')}")
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
            say(f"  {DIM('record kept — press y to retry')}")
            continue
        colorize = GREEN if 200 <= code < 300 else RED
        say(f"  {colorize(f'HTTP {code}')}")
        for line in body.splitlines():
            say("  " + DIM(line))
        if 200 <= code < 300:
            extractor.reset_record()
            say(f"  {DIM('ready for the next record')}")
            return
        say(f"  {DIM('record kept — [e] to fix a field, then y to retry')}")


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
    if settings.text_mode:
        say(DIM("  one transcript per line · blank line clears the record · Ctrl+D to stop"))
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
        # the prompt. There is already an explicit record boundary here (a blank
        # line), so just show the body and leave the record open for follow-ups.
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
            say(f"  {DIM('cleared — ready for the next record')}")
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
