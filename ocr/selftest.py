"""Offline checks: `python -m voice_logging.ocr.selftest`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..common.schemas import SpokenItem, build_dto
from .config import MIME_BY_SUFFIX, Settings, mime_for
from .extractor import OcrExtractor
from .schemas import OcrExtraction

_FAILURES: list[str] = []
_CHECKS = 0


def check(label: str, actual, expected) -> None:
    global _CHECKS
    _CHECKS += 1
    if actual != expected:
        _FAILURES.append(f"{label}\n      expected: {expected!r}\n      actual:   {actual!r}")


def _settings(**overrides) -> Settings:
    base = dict(
        api_key="test-key-not-used",
        contractor_id=7,
        client_id=12,
        dealer_id=None,
        order_type="MAT_ORDER",
        user_id="1",
        base_url="http://localhost:8092",
        min_confidence=0.55,
        post=False,
        dry_run=False,
        verbose=False,
        log_path=Path("voice_logging.ocr.log"),
        image_paths=[],
    )
    base.update(overrides)
    return Settings(**base)


def _item(name: str, qty: int, uom: str) -> SpokenItem:
    return SpokenItem(item_name=name, quantity=qty, uom=uom)


def test_mime_for() -> None:
    check("jpg", mime_for(Path("list.jpg")), "image/jpeg")
    check("png", mime_for(Path("list.PNG")), "image/png")
    check("known suffixes", set(MIME_BY_SUFFIX), {
        ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif",
    })


def test_schema_is_items_only() -> None:
    check(
        "OcrExtraction fields",
        sorted(OcrExtraction.model_fields),
        ["confidence", "entity", "items", "notes", "raw_text"],
    )
    smuggled = OcrExtraction.model_validate({
        "entity": "ITEMS",
        "confidence": 0.9,
        "items": [{"item_name": "cement", "quantity": 10, "uom": "bag"}],
        "amount": 12000,
        "dealer_name": "Sharma Traders",
    })
    check("invented fields dropped", smuggled.filled_fields(),
          {"items": [_item("cement", 10, "bag")]})

    body = OcrExtractor(_settings())._frame(
        "x.jpg", smuggled,
    ).dto.request_body()
    check("amount stays 0", body["amount"], 0.0)
    check("orderType from screen", body["orderType"], "MAT_ORDER")
    check("clientId from screen", body["clientId"], 12)
    check("no dealer invented", "dealerId" in body, False)


def test_incomplete_rows_dropped() -> None:
    result = OcrExtractor(_settings())._frame("x.jpg", OcrExtraction(
        entity="ITEMS", confidence=0.9,
        items=[
            _item("cement", 10, "bag"),
            _item("putty", 0, "kg"),
            _item("", 2, "tin"),
        ],
    ))
    check("only complete row", result.dto.request_body()["orderItems"],
          [{"itemName": "cement", "quantity": 10, "uom": "bag"}])
    check("drop is reported", any("dropped 2" in w for w in result.warnings), True)


def test_low_confidence_and_none() -> None:
    none = OcrExtractor(_settings())._frame(
        "x.jpg", OcrExtraction(entity="NONE", confidence=0.9, items=[_item("x", 1, "bag")]),
    )
    check("NONE has no dto", none.dto, None)

    gated = OcrExtractor(_settings())._frame(
        "x.jpg", OcrExtraction(
            entity="ITEMS", confidence=0.2, items=[_item("cement", 10, "bag")],
        ),
    )
    check("low confidence has no dto", gated.dto, None)
    check("low confidence keeps ignored", bool(gated.ignored_fields.get("items")), True)


def test_screen_ids() -> None:
    dto = build_dto(_settings(client_id=None, dealer_id=5), {
        "items": [{"itemName": "cement", "quantity": 10, "uom": "bag"}],
    })
    body = dto.request_body()
    check("dealer only", (body.get("dealerId"), "clientId" in body), (5, False))


TESTS = [
    ("mime map", test_mime_for),
    ("schema is items only", test_schema_is_items_only),
    ("incomplete rows dropped", test_incomplete_rows_dropped),
    ("NONE / low confidence", test_low_confidence_and_none),
    ("screen ids", test_screen_ids),
]


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m voice_logging.ocr.selftest")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    for label, fn in TESTS:
        before = len(_FAILURES)
        fn()
        ok = len(_FAILURES) == before
        if args.verbose or not ok:
            print(f"  {'ok  ' if ok else 'FAIL'} {label}")

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} of {_CHECKS} checks failed:\n")
        for failure in _FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"all {_CHECKS} checks passed ({len(TESTS)} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
