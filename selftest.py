"""Offline checks: `python -m voice_logging.selftest`.

Exercises everything except the two network calls — validator parity with the
Go layer, item merge across turns, and DTO shaping. Needs no API key and no
microphone.

The validator cases assert that a local rejection carries the exact message
text Validator/MaterialOrderValidator.go would have produced.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings
from .extractor import Extractor
from .schemas import (
    Extraction,
    MaterialOrderDto,
    SpokenItem,
    ValidationFailure,
    build_dto,
)
from .sink import curl_for

_FAILURES: list[str] = []
_CHECKS = 0


def check(label: str, actual, expected) -> None:
    global _CHECKS
    _CHECKS += 1
    if actual != expected:
        _FAILURES.append(f"{label}\n      expected: {expected!r}\n      actual:   {actual!r}")


def check_raises(label: str, fn, expected_message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    try:
        fn()
    except ValidationFailure as exc:
        if str(exc) != expected_message:
            _FAILURES.append(
                f"{label}\n      expected: {expected_message!r}\n      actual:   {str(exc)!r}"
            )
        return
    _FAILURES.append(f"{label}\n      expected ValidationFailure({expected_message!r}), none raised")


def check_ok(label: str, fn) -> None:
    global _CHECKS
    _CHECKS += 1
    try:
        fn()
    except ValidationFailure as exc:
        _FAILURES.append(f"{label}\n      unexpected ValidationFailure: {exc}")


def _settings(**overrides) -> Settings:
    base = dict(
        api_key="test-key-not-used",
        contractor_id=7,
        client_id=12,
        dealer_id=None,
        order_type="MAT_ORDER",
        user_id="1",
        base_url="http://localhost:8092",
        languages=["hi-IN"],
        silence_seconds=1.5,
        min_confidence=0.55,
        input_device=None,
        text_mode=False,
        post=False,
        dry_run=False,
        verbose=False,
        log_path=Path("voice_logging.log"),
        transcript_mode="SMART",
    )
    base.update(overrides)
    return Settings(**base)


def _item(name: str, qty: int, uom: str) -> SpokenItem:
    return SpokenItem(item_name=name, quantity=qty, uom=uom)


def _extraction(**overrides) -> Extraction:
    base = dict(entity="ITEMS", confidence=0.9)
    base.update(overrides)
    return Extraction(**base)


def _good_items() -> list[dict]:
    return [{"itemName": "cement", "quantity": 10, "uom": "bag"}]


# --- validator parity with the Go layer -------------------------------------


def test_order_validator_parity() -> None:
    good = MaterialOrderDto(
        contractorId=7, clientId=12, orderType="MAT_LIST",
        orderItems=_good_items(),
    )
    check_ok("order: fully valid list passes", good.validate_for_api)

    check_raises(
        "order: contractorId 0",
        lambda: MaterialOrderDto(
            contractorId=0, orderType="MAT_LIST", orderItems=_good_items(),
        ).validate_for_api(),
        "contractorId is required and must be greater than 0",
    )
    check_raises(
        "order: bogus orderType",
        lambda: MaterialOrderDto(
            contractorId=7, orderType="SHOPPING", orderItems=_good_items(),
        ).validate_for_api(),
        "orderType must be one of: MAT_ORDER, MAT_LIST",
    )
    check_raises(
        "order: no items",
        lambda: MaterialOrderDto(
            contractorId=7, orderType="MAT_LIST", orderItems=[],
        ).validate_for_api(),
        "either orderItems or orderUrls is required",
    )
    check_raises(
        "order: negative amount",
        lambda: MaterialOrderDto(
            contractorId=7, orderType="MAT_LIST", amount=-1,
            orderItems=_good_items(),
        ).validate_for_api(),
        "amount must be non-negative",
    )
    check_raises(
        "order: blank itemName",
        lambda: MaterialOrderDto(
            contractorId=7, orderType="MAT_LIST",
            orderItems=[{"itemName": "  ", "quantity": 1, "uom": "bag"}],
        ).validate_for_api(),
        "orderItems[0].itemName is required",
    )
    check_raises(
        "order: itemName over 255 bytes",
        lambda: MaterialOrderDto(
            contractorId=7, orderType="MAT_LIST",
            orderItems=[{"itemName": "x" * 256, "quantity": 1, "uom": "bag"}],
        ).validate_for_api(),
        "orderItems[0].itemName must not exceed 255 characters",
    )
    check_raises(
        "order: quantity 0",
        lambda: MaterialOrderDto(
            contractorId=7, orderType="MAT_LIST",
            orderItems=[{"itemName": "cement", "quantity": 0, "uom": "bag"}],
        ).validate_for_api(),
        "orderItems[0].quantity is required and must be greater than 0",
    )
    check_raises(
        "order: blank uom",
        lambda: MaterialOrderDto(
            contractorId=7, orderType="MAT_LIST",
            orderItems=[{"itemName": "cement", "quantity": 1, "uom": ""}],
        ).validate_for_api(),
        "orderItems[0].uom is required",
    )
    check_raises(
        "order: uom over 30 bytes",
        lambda: MaterialOrderDto(
            contractorId=7, orderType="MAT_LIST",
            orderItems=[{"itemName": "cement", "quantity": 1, "uom": "u" * 31}],
        ).validate_for_api(),
        "orderItems[0].uom must not exceed 30 characters",
    )
    check_ok(
        "order: 85 Devanagari chars (255 bytes) itemName passes",
        lambda: MaterialOrderDto(
            contractorId=7, orderType="MAT_LIST",
            orderItems=[{"itemName": "अ" * 85, "quantity": 1, "uom": "bag"}],
        ).validate_for_api(),
    )
    check_raises(
        "order: 86 Devanagari chars (258 bytes) itemName rejected",
        lambda: MaterialOrderDto(
            contractorId=7, orderType="MAT_LIST",
            orderItems=[{"itemName": "अ" * 86, "quantity": 1, "uom": "bag"}],
        ).validate_for_api(),
        "orderItems[0].itemName must not exceed 255 characters",
    )


def test_request_bodies() -> None:
    body = MaterialOrderDto(
        contractorId=7, clientId=12, orderType="MAT_LIST",
        amount=0.0, orderItems=_good_items(),
    ).request_body()
    check(
        "client flow: clientId in, dealerId out, empty orderUrls",
        body,
        {
            "contractorId": 7,
            "orderType": "MAT_LIST",
            "orderUrls": [],
            "amount": 0.0,
            "orderItems": [{"itemName": "cement", "quantity": 10, "uom": "bag"}],
            "clientId": 12,
        },
    )
    dealer_body = MaterialOrderDto(
        contractorId=7, dealerId=5, orderType="MAT_ORDER",
        amount=12000.0, itemSummary="cement",
        orderItems=_good_items(),
    ).request_body()
    check("dealer flow: dealerId in, clientId out",
          "dealerId" in dealer_body and "clientId" not in dealer_body, True)
    check("itemSummary included when set", dealer_body.get("itemSummary"), "cement")
    check("amount included", dealer_body.get("amount"), 12000.0)


def test_missing_required() -> None:
    check(
        "no items -> orderItems missing",
        build_dto(_settings(), {}).missing_required(),
        ["orderItems"],
    )
    check(
        "items present -> nothing missing (orderType comes from the screen)",
        build_dto(_settings(), {"items": _good_items()}).missing_required(),
        [],
    )
    check(
        "screen's orderType is used verbatim",
        build_dto(_settings(order_type="MAT_LIST"), {"items": _good_items()}).orderType,
        "MAT_LIST",
    )


def test_speech_cannot_reach_beyond_items() -> None:
    """The Items section is the whole contract — nothing else is extractable."""
    check(
        "Extraction exposes items only",
        sorted(Extraction.model_fields),
        ["confidence", "entity", "is_correction", "items", "notes"],
    )

    # Pydantic drops unknown keys, so a model that invents an amount or a dealer
    # name cannot smuggle it into the record.
    smuggled = Extraction.model_validate({
        "entity": "ITEMS",
        "confidence": 0.9,
        "items": [{"item_name": "cement", "quantity": 10, "uom": "bag"}],
        "amount": 12000,
        "order_type": "MAT_LIST",
        "dealer_name": "Sharma Traders",
    })
    check("invented fields are not kept", smuggled.filled_fields(),
          {"items": [_item("cement", 10, "bag")]})

    ex = Extractor(_settings())
    body = ex._merge("t", smuggled).dto.request_body()
    check("invented amount never reaches the body", body["amount"], 0.0)
    check("orderType still the screen's", body["orderType"], "MAT_ORDER")
    check("no itemSummary key", "itemSummary" in body, False)


# --- merge semantics --------------------------------------------------------


def test_accumulation_across_turns() -> None:
    ex = Extractor(_settings())
    ex._merge("10 bag cement", _extraction(items=[_item("cement", 10, "bag")]))
    final = ex._merge("5 kg Fevicol SH", _extraction(items=[_item("Fevicol SH", 5, "kg")]))

    check("accumulate: record is complete", final.is_complete, True)
    check(
        "accumulate: items appended",
        final.dto.request_body()["orderItems"],
        [
            {"itemName": "cement", "quantity": 10, "uom": "bag"},
            {"itemName": "Fevicol SH", "quantity": 5, "uom": "kg"},
        ],
    )
    check("accumulate: orderType from the screen", final.dto.orderType, "MAT_ORDER")
    check("accumulate: clientId from screen, not speech",
          final.dto.request_body()["clientId"], 12)


def test_empty_turn_never_clears() -> None:
    ex = Extractor(_settings())
    ex._merge("t", _extraction(items=[_item("cement", 10, "bag")]))
    ex._merge("haan theek hai", _extraction(items=[]))
    check("a turn that adds nothing leaves the list alone",
          ex.fields.get("items"),
          [{"itemName": "cement", "quantity": 10, "uom": "bag"}])


def test_incomplete_rows_are_dropped() -> None:
    """A half-row is worse than a missing one — the user can re-say a missing one."""
    ex = Extractor(_settings())
    result = ex._merge("t", _extraction(items=[
        _item("cement", 10, "bag"),
        _item("putty", 0, "kg"),
        _item("", 2, "tin"),
        _item("Fevicol", 3, "  "),
    ]))
    check("only the complete row is kept",
          ex.fields.get("items"),
          [{"itemName": "cement", "quantity": 10, "uom": "bag"}])
    check("the drop is reported",
          any("dropped 3" in w for w in result.warnings), True)


def test_correction_replaces_items() -> None:
    ex = Extractor(_settings())
    ex._merge("t", _extraction(items=[_item("cement", 10, "bag"), _item("Fevicol", 5, "kg")]))
    result = ex._merge(
        "nahi, 8 bag cement only",
        _extraction(items=[_item("cement", 8, "bag")], is_correction=True),
    )
    check("correction: list replaced",
          ex.fields["items"],
          [{"itemName": "cement", "quantity": 8, "uom": "bag"}])
    check("correction: reported as changed",
          result.changed.get("items"),
          [{"itemName": "cement", "quantity": 8, "uom": "bag"}])


def test_none_and_low_confidence_are_ignored() -> None:
    ex = Extractor(_settings())
    ex._merge("t", _extraction(items=[_item("cement", 10, "bag")]))

    ex._merge("weather chatter", _extraction(entity="NONE", confidence=0.9,
                                            items=[_item("nonsense", 1, "bag")]))
    check("NONE leaves state untouched",
          ex.fields["items"],
          [{"itemName": "cement", "quantity": 10, "uom": "bag"}])

    result = ex._merge("mumble", _extraction(items=[_item("garbled", 1, "bag")],
                                            confidence=0.2))
    check("low confidence leaves state untouched",
          ex.fields["items"],
          [{"itemName": "cement", "quantity": 10, "uom": "bag"}])
    check("low confidence warns", len(result.warnings), 1)


def test_screen_ids_are_optional() -> None:
    """Both dropdowns are optional on the order screen, so both are here."""
    dealer = Extractor(_settings(client_id=None, dealer_id=5))
    body = dealer._merge("t", _extraction(items=[_item("cement", 10, "bag")])).dto.request_body()
    check("dealer only: dealerId", body.get("dealerId"), 5)
    check("dealer only: no clientId", "clientId" in body, False)

    both = Extractor(_settings(client_id=12, dealer_id=5))
    body = both._merge("t", _extraction(items=[_item("cement", 10, "bag")])).dto.request_body()
    check("both: dealerId and clientId",
          (body.get("dealerId"), body.get("clientId")), (5, 12))

    neither = Extractor(_settings(client_id=None, dealer_id=None))
    result = neither._merge("t", _extraction(items=[_item("cement", 10, "bag")]))
    check("neither: still a valid order",
          ("clientId" in result.dto.request_body(),
           "dealerId" in result.dto.request_body(),
           result.is_complete),
          (False, False, True))


def test_no_digit_words_in_vocabulary() -> None:
    from .config import CUSTOM_VOCABULARY

    banned = {
        "shunya", "ek", "do", "teen", "chaar", "paanch", "chhe", "saat",
        "aath", "nau", "das",
        "sonne", "sunne", "ondu", "eradu", "mooru", "naalku", "naalu", "aidu",
        "aaru", "elu", "entu", "ombattu", "hattu",
    }
    present = sorted(banned.intersection(term.lower() for term in CUSTOM_VOCABULARY))
    check("vocabulary: no romanized digit words", present, [])
    lowered = {term.lower() for term in CUSTOM_VOCABULARY}
    check("vocabulary: amount scales retained",
          {"lakh", "savira", "laksha", "koti"}.issubset(lowered), True)


def test_transcriber_reaps_children_on_cancel() -> None:
    import asyncio

    from .transcriber import _shutdown

    async def scenario() -> tuple[bool, bool]:
        async def explodes():
            await asyncio.sleep(0.01)
            raise RuntimeError("APIError 1000: socket closed underneath us")

        async def forever():
            await asyncio.sleep(3600)

        failed = asyncio.create_task(explodes())
        idle = asyncio.create_task(forever())
        await asyncio.sleep(0.05)

        await _shutdown((failed, idle, None))

        retrieved = failed.done() and failed.exception() is not None
        return retrieved, idle.cancelled()

    retrieved, idle_cancelled = asyncio.run(scenario())
    check("shutdown: failed child's exception is consumed", retrieved, True)
    check("shutdown: pending child is cancelled", idle_cancelled, True)


def test_manual_edit() -> None:
    ex = Extractor(_settings())
    ex._merge("t", _extraction(items=[_item("cement", 10, "bag")]))
    ex.set_field("items", [{"itemName": "white cement", "quantity": 8, "uom": "bag"}])
    check("edit: row overwrite applied",
          ex.current_dto().request_body()["orderItems"],
          [{"itemName": "white cement", "quantity": 8, "uom": "bag"}])
    ex.set_field("items", None)
    check("edit: deleting the last row leaves nothing to post",
          ex.current_dto(), None)


def test_curl_rendering() -> None:
    settings = _settings(user_id="42")
    dto = MaterialOrderDto(
        contractorId=7, clientId=12, orderType="MAT_LIST",
        orderItems=_good_items(),
    )
    rendered = curl_for(settings, dto)
    check("curl: targets material-order",
          "http://localhost:8092/starship/v1/material-order" in rendered, True)
    check("curl: carries the audit header", "X-USER-ID: 42" in rendered, True)
    check("curl: not the old client endpoint",
          "/starship/v1/client" in rendered, False)


def test_turn_buffer() -> None:
    ex = Extractor(_settings())
    check("buffer: empty at rest", ex.has_turn, False)
    ex.add_final("10 bag cement")
    check("buffer: fills on a final", ex.has_turn, True)
    ex.reset_record()
    check("reset: clears entity and fields", (ex.entity, ex.fields), (None, {}))


# --- runner -----------------------------------------------------------------

TESTS = [
    ("order validator parity", test_order_validator_parity),
    ("request bodies", test_request_bodies),
    ("missing required fields", test_missing_required),
    ("speech reaches items only", test_speech_cannot_reach_beyond_items),
    ("accumulation across turns", test_accumulation_across_turns),
    ("empty turn never clears", test_empty_turn_never_clears),
    ("incomplete rows dropped", test_incomplete_rows_are_dropped),
    ("corrections replace items", test_correction_replaces_items),
    ("NONE / low confidence ignored", test_none_and_low_confidence_are_ignored),
    ("screen ids optional", test_screen_ids_are_optional),
    ("no digit words in vocabulary", test_no_digit_words_in_vocabulary),
    ("transcriber reaps children", test_transcriber_reaps_children_on_cancel),
    ("manual edit", test_manual_edit),
    ("curl rendering", test_curl_rendering),
    ("turn buffer", test_turn_buffer),
]


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m voice_logging.selftest")
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
