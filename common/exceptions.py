"""Domain errors for the voice-logging CLI."""


class MicrophoneError(RuntimeError):
    """Raised with an actionable message instead of a PortAudio traceback."""
