"""Image OCR → material-order object via the same Gemini Flash extract model.

Not implemented yet. Planned surface:

    image → gemini-3.5-flash-lite (OCR + structured extract) → MaterialOrderDto

Screen context (contractor, client, dealer, order type) comes from flags, same
as `voice_logging.audio`. This module never touches the microphone.
"""
