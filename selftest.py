"""Shim: `python -m voice_logging.selftest` → audio.selftest."""

from .audio.selftest import main

if __name__ == "__main__":
    raise SystemExit(main())
