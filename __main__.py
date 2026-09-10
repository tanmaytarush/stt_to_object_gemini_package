"""Package CLI: audio by default, or `ocr` / `audio` as the first argument.

    python -m voice_logging --contractor-id 1          # audio (unchanged)
    python -m voice_logging audio --contractor-id 1
    python -m voice_logging ocr
"""

from __future__ import annotations

import sys


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in {"ocr", "image"}:
        sys.argv = [sys.argv[0], *argv[1:]]
        from .ocr.__main__ import main as ocr_main
        return ocr_main()
    if argv and argv[0] in {"audio", "voice"}:
        sys.argv = [sys.argv[0], *argv[1:]]
    from .audio.cli import main as audio_main
    return audio_main()


if __name__ == "__main__":
    raise SystemExit(main())
