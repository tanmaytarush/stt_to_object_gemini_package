"""Image bytes → Flash structured OCR → MaterialOrderDto.
One image is the whole list. There is no turn buffer and no is_correction.
Screen ids still come from Settings, never from the photo.
"""
from __future__ import annotations
import asyncio
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from ..common.schemas import ValidationFailure, _normalize_item, build_dto
from . import config
from .schemas import OcrExtraction
EXTRACT_TIMEOUT_SECONDS = 60
SYSTEM_PROMPT = """\
You fill in the Items section of a New Order screen from a photo of an \
item list. The photo may be a printed challan, a handwritten note, a \
WhatsApp screenshot, or a dealer bill. Text is frequently Hindi, Gujarati, \
Marathi, Kannada or Indian English, and OCR errors are common; recover \
intent, do not trust glyphs literally.
The rest of the screen is already filled in by hand — the dealer, the client, \
and what kind of order this is. You do NOT extract any of that. Names of \
people, shops, firms, phone numbers, prices, totals, dates and letterheads \
are NOT your job: ignore them entirely. You produce ONE thing: rows of \
item name + quantity + unit.
WHEN TO RETURN NONE
Return entity NONE only when the image names no material at all — a blank \
page, an unrelated photo, or a document that is only about a dealer, a \
person, or a price. If any material is visible, return ITEMS.
items
- Each row: item_name, quantity (integer > 0), uom.
- item_name is the material. Keep Pidilite brand names intact and spelled the \
usual way (Fevicol SH, Fevicol Marine, Dr. Fixit, Fevikwik, M-Seal, Roff, \
Araldite). Drop filler around it.
- uom is the unit AS WRITTEN: bag, bori, katta, thaila, tin, dabba, peti, nag, \
kg, litre, piece, packet, roll, bundle, quintal. Do not convert between units \
and do not invent one.
- quantity is whole units. Digit words in any of the languages: ek/one=1, \
do/be/two=2, teen/tran/three=3, chaar/four=4, paanch/panch/five=5, chhe/six=6, \
saat/seven=7, aath/eight=8, nau/nine=9, das/ten=10; Kannada ondu=1, eradu=2, \
mooru=3, naalku=4, aidu=5, aaru=6, elu=7, entu=8, ombattu=9, hattu=10. \
Fractions (dedh, sawa, paune) are NOT whole units — drop that row and \
explain it in notes.
- One row per material. The image is the FULL list for this order.
- If a material is named with NO quantity or NO unit, drop that row and say so \
in notes. A half-row is worse than a missing one.
confidence
- 0.0–1.0 on the rows you filled. Below 0.5 only if you are guessing at a \
material name, a quantity, or a unit.
notes
- One short line when you dropped a row, or a unit was missing, or a quantity \
was unclear. Otherwise null. State the problem; do not narrate your process.
raw_text
- A short plain-text trace of the lines you actually read, or null. This is \
for debugging. It must never include a price, a phone, or a dealer name.
"""


