"""Audio or image → the Items section of a material order.

Two input modules share one output object (`MaterialOrderDto`):

- `voice_logging.audio` — microphone / transcript → Gemini Live STT → Flash
- `voice_logging.ocr`   — item-list image → Flash OCR (not implemented yet)

The order screen's dealer, client and order type are filled in by hand and
passed in as flags. Nothing in the Go service depends on this package.
"""

__version__ = "0.4.0"
