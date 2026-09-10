"""Shim: `python -m voice_logging.hardtest` → audio.hardtest."""

from .audio.hardtest import main

if __name__ == "__main__":
    raise SystemExit(main())
