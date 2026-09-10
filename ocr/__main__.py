"""CLI stub: `python -m voice_logging.ocr`."""

from __future__ import annotations


def main() -> int:
    print(
        "OCR is not implemented yet.\n"
        "This module will read an item-list image, run the same Gemini Flash "
        "extract model as voice_logging.audio, and produce a MaterialOrderDto.\n"
        "\n"
        "For now, use the audio path:\n"
        "  python -m voice_logging.audio --contractor-id N"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
