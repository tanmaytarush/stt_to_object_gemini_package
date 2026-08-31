"""Gemini Live API session management.

The service caps a live transcription session at 10 minutes. This module
rotates the connection proactively at ~9m30s and reconnects with backoff on
unexpected drops, so a long dictation session never requires restarting the
process. The turn buffer lives above this layer (in extractor.py) so a
half-built object survives a rotation intact.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from google import genai
from google.genai import types

from . import config
from .audio import MicStream


@dataclass(frozen=True)
class Interim:
    """A partial hypothesis. Overwritten by the next one; never buffered."""
    text: str


@dataclass(frozen=True)
class Final:
    """A finalized utterance. This is what accumulates into a turn."""
    text: str


@dataclass(frozen=True)
class Notice:
    """Connection lifecycle news worth showing the user."""
    text: str
    is_error: bool = False


Event = Interim | Final | Notice

_GRACE_SECONDS = 2.0  # how long to wait for trailing finals after audio_stream_end
_BACKOFF_START = 0.5
_BACKOFF_CAP = 8.0


async def _shutdown(tasks) -> None:
    """Cancel child tasks and consume whatever they finished with.

    Consuming is the whole point. A task that ended with an exception nobody
    looked at makes asyncio log "Task exception was never retrieved" when it is
    garbage collected — which is exactly what Ctrl+C used to produce here, via
    _pump_text dying on the socket the closing session pulled out from under it.
    """
    live = [task for task in tasks if task is not None]
    for task in live:
        if not task.done():
            task.cancel()
    for task in live:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        # Belt and braces: if the await above was itself interrupted by a fresh
        # cancellation, the task may still be holding an unread exception.
        if task.done() and not task.cancelled():
            task.exception()


class Transcriber:
    def __init__(self, settings: config.Settings, mic: MicStream) -> None:
        self._settings = settings
        self._mic = mic
        self._client = genai.Client(api_key=settings.api_key)
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._stop = asyncio.Event()
        self.epoch = 0

    # -- public ------------------------------------------------------------

    async def events(self):
        """Yield transcription events until stop() is called."""
        while True:
            event = await self._events.get()
            if event is None:  # sentinel from run()
                return
            yield event

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Supervisor: keep a live session up for as long as we're not stopped."""
        backoff = _BACKOFF_START
        try:
            while not self._stop.is_set():
                self.epoch += 1
                try:
                    await self._one_session()
                    backoff = _BACKOFF_START  # a clean rotation resets the penalty
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._emit(Notice(f"transcription session dropped: {exc}", is_error=True))
                    if self._stop.is_set():
                        break
                    self._emit(Notice(f"reconnecting in {backoff:.1f}s…"))
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                        break  # stop() won the race
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(backoff * 2, _BACKOFF_CAP)
        finally:
            await self._events.put(None)

    # -- internals ---------------------------------------------------------

    def _emit(self, event: Event) -> None:
        self._events.put_nowait(event)

    def _live_config(self) -> types.LiveConnectConfig:
        transcription = types.AudioTranscriptionConfig(
            # Empty = auto-detect the language being spoken. A 5-way hint
            # list (hi+kn+…) makes Hindi steal Kannada into Devanagari.
            language_codes=self._settings.stt_language_codes,
            # SMART strips filler words, stutters and false starts, and formats
            # alphanumerics by intent — a large win for dictated Indic speech.
            mode=self._settings.transcript_mode,
            custom_vocabulary=self._settings.custom_vocabulary,
        )
        return types.LiveConnectConfig(
            response_modalities=["TEXT"],
            input_audio_transcription=transcription,
        )

    async def _one_session(self) -> None:
        first = self.epoch == 1
        async with self._client.aio.live.connect(
            model=config.STT_MODEL, config=self._live_config()
        ) as session:
            if first:
                self._emit(Notice("connected — listening"))
            else:
                dropped_ms = self._mic.drain()
                suffix = f" ({dropped_ms} ms of audio dropped)" if dropped_ms else ""
                self._emit(Notice(f"reconnected (session {self.epoch}){suffix}"))

            send_task = recv_task = rotate_task = stop_task = None
            children: tuple = ()
            try:
                send_task = asyncio.create_task(self._pump_audio(session), name="stt-send")
                recv_task = asyncio.create_task(self._pump_text(session), name="stt-recv")
                # Rotate before the server's 10-minute cap so it never lands mid-word.
                rotate_task = asyncio.create_task(
                    asyncio.sleep(config.SESSION_ROTATE_SECONDS), name="stt-rotate"
                )
                stop_task = asyncio.create_task(self._stop.wait(), name="stt-stop")
                children = (send_task, recv_task, rotate_task, stop_task)

                done, _ = await asyncio.wait(
                    set(children), return_when=asyncio.FIRST_COMPLETED
                )

                # Stop feeding audio before closing, so the trailing utterance is
                # finalized instead of cut off.
                send_task.cancel()
                rotate_task.cancel()
                stop_task.cancel()

                try:
                    await session.send_realtime_input(audio_stream_end=True)
                except Exception:
                    pass  # socket already gone; the recv drain below just no-ops

                if not recv_task.done():
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(recv_task), timeout=_GRACE_SECONDS
                        )
                    except (asyncio.TimeoutError, Exception):
                        pass

                # Surface a genuine failure so the supervisor backs off; a
                # rotation or a stop is a normal exit and should not.
                if rotate_task in done and not self._stop.is_set():
                    self._emit(Notice("rotating session (10-minute service cap)"))
                for task in done:
                    if task in (rotate_task, stop_task):
                        continue
                    exc = task.exception() if not task.cancelled() else None
                    if exc is not None:
                        raise exc
            finally:
                # Must run even when _one_session is itself cancelled — that is
                # what Ctrl+C does, and it used to skip every line above, leaving
                # _pump_text to die on the closing socket with its APIError never
                # retrieved ("Task exception was never retrieved").
                await _shutdown(children)

    async def _pump_audio(self, session) -> None:
        while True:
            chunk = await self._mic.read()
            await session.send_realtime_input(
                audio=types.Blob(
                    data=chunk,
                    mime_type=f"audio/pcm;rate={config.SAMPLE_RATE}",
                )
            )

    async def _pump_text(self, session) -> None:
        async for message in session.receive():
            content = getattr(message, "server_content", None)
            if content is None:
                continue

            interim = getattr(content, "interim_input_transcription", None)
            if interim is not None and getattr(interim, "text", None):
                self._emit(Interim(interim.text))

            final = getattr(content, "input_transcription", None)
            if final is not None and getattr(final, "text", None):
                text = final.text.strip()
                if text:
                    self._emit(Final(text))
