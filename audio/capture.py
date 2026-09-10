"""Microphone capture: float32 blocks from PortAudio -> 16-bit PCM on a queue.

The queue is bounded and drops the oldest chunk when full. An unbounded queue
means a network stall silently turns into transcribing minutes-old audio, which
looks like the model being wrong rather than the socket being slow.
"""

from __future__ import annotations

import asyncio
import math
import sys

import numpy as np

from ..common.exceptions import MicrophoneError
from . import config


def _import_sounddevice():
    try:
        import sounddevice as sd
    except OSError as exc:  # PortAudio shared library missing
        raise MicrophoneError(
            f"Could not load PortAudio ({exc}).\n"
            "  macOS: brew install portaudio && pip install --force-reinstall sounddevice"
        ) from exc
    return sd


def list_devices() -> str:
    sd = _import_sounddevice()
    lines = ["Input devices (use the index with --input-device):", ""]
    try:
        default_in = sd.default.device[0]
    except (TypeError, IndexError):
        default_in = None
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] < 1:
            continue
        marker = " (default)" if index == default_in else ""
        lines.append(
            f"  [{index}] {device['name']}{marker} — "
            f"{device['max_input_channels']} ch @ {int(device['default_samplerate'])} Hz"
        )
    if len(lines) == 2:
        lines.append("  none found — check System Settings > Privacy & Security > Microphone")
    return "\n".join(lines)


def _downsample(mono: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear resample. 48 kHz Mac mics opened at 16 kHz often deliver silence."""
    if src_rate == dst_rate or mono.size == 0:
        return mono
    n_out = max(1, int(round(mono.size * dst_rate / src_rate)))
    x_old = np.linspace(0.0, 1.0, mono.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
    return np.interp(x_new, x_old, mono).astype(mono.dtype, copy=False)


class MicStream:
    """Async context manager owning the PortAudio input stream.

    Lives across transcription reconnects: the socket may rotate every ~10
    minutes, but the mic stays open the whole time.
    """

    def __init__(self, settings: config.Settings) -> None:
        self._settings = settings
        self._sd = _import_sounddevice()
        self._stream = None
        self._capture_rate = config.SAMPLE_RATE
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=config.MAX_QUEUED_CHUNKS)
        self._loop: asyncio.AbstractEventLoop | None = None
        self.dropped_chunks = 0
        self.level = 0.0  # rolling RMS, 0.0..1.0
        self.callbacks = 0
        self.device_label = ""

    def _callback(self, indata, _frames, _time_info, status) -> None:
        # Runs on PortAudio's thread. Everything here must be non-blocking and
        # hop back to the loop via call_soon_threadsafe.
        if status:
            print(f"\n  audio status: {status}", file=sys.stderr)

        self.callbacks += 1
        mono = _downsample(indata[:, 0], self._capture_rate, config.SAMPLE_RATE)
        rms = float(np.sqrt(np.mean(np.square(mono)))) if mono.size else 0.0
        self.level = max(rms, self.level * 0.8)  # decay, so the meter isn't jumpy

        pcm = (np.clip(mono, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._offer, pcm)

    def _offer(self, pcm: bytes) -> None:
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            # Drop the oldest so what we send is always the most recent audio.
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                pass
            self.dropped_chunks += 1
            try:
                self._queue.put_nowait(pcm)
            except asyncio.QueueFull:
                pass

    def _input_device_info(self) -> tuple[int | None, dict]:
        device = self._settings.input_device
        if device is None:
            try:
                device = self._sd.default.device[0]
            except (TypeError, IndexError):
                device = None
        info = self._sd.query_devices(device, "input")
        return device, info

    async def __aenter__(self) -> "MicStream":
        self._loop = asyncio.get_running_loop()
        try:
            index, info = self._input_device_info()
            native = int(info.get("default_samplerate") or config.SAMPLE_RATE)
            # Capture at the hardware rate. Asking a 48 kHz Mac mic for 16 kHz
            # can open "successfully" and then deliver all-zero buffers.
            self._capture_rate = native if native > 0 else config.SAMPLE_RATE
            blocksize = max(1, int(self._capture_rate * config.CHUNK_MS / 1000))
            self.device_label = (
                f"[{index}] {info.get('name', 'unknown')} @ {self._capture_rate} Hz"
            )
            self._stream = self._sd.InputStream(
                samplerate=self._capture_rate,
                channels=config.CHANNELS,
                dtype="float32",
                blocksize=blocksize,
                device=index,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:  # sd.PortAudioError and friends
            raise MicrophoneError(
                f"Could not open the microphone ({exc}).\n"
                "  - grant mic access: System Settings > Privacy & Security > Microphone\n"
                "    (Cursor's terminal needs its own toggle, not Terminal.app's)\n"
                "  - pick a device explicitly: --list-devices, then --input-device 0"
            ) from exc
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def read(self) -> bytes:
        return await self._queue.get()

    def drain(self) -> int:
        """Discard everything queued. Called after a reconnect gap.

        Returns the number of milliseconds of audio thrown away, so the caller
        can say so out loud rather than losing it silently.
        """
        discarded = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            discarded += 1
        return discarded * config.CHUNK_MS

    def meter(self, width: int = 12) -> str:
        """A coarse level bar, so a dead mic is distinguishable from a dead socket."""
        # Gain-up: speech RMS on a laptop mic is often 0.01–0.05 and looks empty
        # on a linear 0..1 bar.
        scaled = min(1.0, self.level * 8.0)
        filled = min(width, int(math.sqrt(scaled) * width))
        return "█" * filled + "·" * (width - filled)

    def is_silent(self) -> bool:
        return self.level < 0.002
