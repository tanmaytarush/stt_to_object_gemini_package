"""Offline checks: `python -m voice_logging.selftest`.

Exercises everything except the two network calls — validator parity with the
Go layer, phone normalization, merge semantics across turns, and DTO shaping.
Needs no API key and no microphone.

The validator cases are the important ones: they assert that a local rejection
carries the exact message text Validator/ClientValidator.go and
Validator/DealerValidator.go would have produced, so drift between the Python
mirror and the Go source shows up here rather than as a surprise 400.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings
from .extractor import Extractor
from .schemas import (
    ClientDto,
    DealerDto,
    Extraction,
    ValidationFailure,
    build_dto,
)
from .sink import curl_for, describe_match, parse_filter_rows

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
        user_id="1",
        base_url="http://localhost:8092",
        languages=["hi-IN"],
        silence_seconds=1.5,
        min_confidence=0.55,
        name_script="latin",
        input_device=None,
        text_mode=False,
        post=False,
        dry_run=False,
        strict_entity=False,
        verbose=False,
        log_path=Path("voice_logging.log"),
        transcript_mode="SMART",
    )
    base.update(overrides)
    return Settings(**base)


def _extraction(**overrides) -> Extraction:
    base = dict(entity="CLIENT", confidence=0.9)
    base.update(overrides)
    return Extraction(**base)


# --- validator parity with the Go layer -------------------------------------


def test_client_validator_parity() -> None:
    good = ClientDto(contractorId=7, clientName="Ramesh Kumar", phoneNumber="9876543210",
                     projectType="MATERIAL_AND_LABOUR", totalAmount=250000)
    check_ok("client: fully valid record passes", good.validate_for_api)

    check_raises(
        "client: contractorId 0",
        lambda: ClientDto(contractorId=0, clientName="A", phoneNumber="1",
                          projectType="LABOUR_ONLY").validate_for_api(),
        "contractorId is required and must be greater than 0",
    )
    check_raises(
        "client: blank name after trim",
        lambda: ClientDto(contractorId=7, clientName="   ", phoneNumber="1",
                          projectType="LABOUR_ONLY").validate_for_api(),
        "clientName is required and cannot be empty",
    )
    check_raises(
        "client: name over 255 bytes",
        lambda: ClientDto(contractorId=7, clientName="x" * 256, phoneNumber="1",
                          projectType="LABOUR_ONLY").validate_for_api(),
        "clientName must not exceed 255 characters",
    )
    check_raises(
        "client: blank phone",
        lambda: ClientDto(contractorId=7, clientName="A", phoneNumber="",
                          projectType="LABOUR_ONLY").validate_for_api(),
        "phoneNumber is required and cannot be empty",
    )
    check_raises(
        "client: phone over 20 bytes",
        lambda: ClientDto(contractorId=7, clientName="A", phoneNumber="1" * 21,
                          projectType="LABOUR_ONLY").validate_for_api(),
        "phoneNumber must not exceed 20 characters",
    )
    check_raises(
        "client: bogus projectType",
        lambda: ClientDto(contractorId=7, clientName="A", phoneNumber="1",
                          projectType="MATERIAL").validate_for_api(),
        "projectType must be one of: MATERIAL_AND_LABOUR, LABOUR_ONLY",
    )
    check_raises(
        "client: negative totalAmount",
        lambda: ClientDto(contractorId=7, clientName="A", phoneNumber="1",
                          projectType="LABOUR_ONLY", totalAmount=-1).validate_for_api(),
        "totalAmount must be non-negative",
    )

    # Go's len() counts bytes, so the 255 cap is ~85 Devanagari characters.
    check_ok(
        "client: 85 Devanagari chars (255 bytes) passes",
        lambda: ClientDto(contractorId=7, clientName="अ" * 85, phoneNumber="1",
                          projectType="LABOUR_ONLY").validate_for_api(),
    )
    check_raises(
        "client: 86 Devanagari chars (258 bytes) rejected",
        lambda: ClientDto(contractorId=7, clientName="अ" * 86, phoneNumber="1",
                          projectType="LABOUR_ONLY").validate_for_api(),
        "clientName must not exceed 255 characters",
    )


def test_dealer_validator_parity() -> None:
    check_ok(
        "dealer: name only is valid (phone optional)",
        lambda: DealerDto(contractorId=7, dealerName="Sharma Traders").validate_for_api(),
    )
    check_raises(
        "dealer: contractorId 0",
        lambda: DealerDto(contractorId=0, dealerName="A").validate_for_api(),
        "contractorId is required and must be greater than 0",
    )
    check_raises(
        "dealer: blank name uses the dealer wording",
        lambda: DealerDto(contractorId=7, dealerName=" ").validate_for_api(),
        "dealerName is required",
    )
    check_raises(
        "dealer: phone over 20 bytes",
        lambda: DealerDto(contractorId=7, dealerName="A",
                          phoneNumber="9" * 21).validate_for_api(),
        "phoneNumber must not exceed 20 characters",
    )


def test_request_bodies() -> None:
    check(
        "client body: totalAmount omitted when unset",
        ClientDto(contractorId=7, clientName=" Ramesh ", phoneNumber=" 9876543210 ",
                  projectType="LABOUR_ONLY").request_body(),
        {"contractorId": 7, "clientName": "Ramesh", "phoneNumber": "9876543210",
         "projectType": "LABOUR_ONLY"},
    )
    check(
        "client body: totalAmount included when set",
        ClientDto(contractorId=7, clientName="R", phoneNumber="1",
                  projectType="LABOUR_ONLY", totalAmount=150000.0).request_body(),
        {"contractorId": 7, "clientName": "R", "phoneNumber": "1",
         "projectType": "LABOUR_ONLY", "totalAmount": 150000.0},
    )
    check(
        "dealer body: phoneNumber omitted when unset",
        DealerDto(contractorId=7, dealerName="Sharma Traders").request_body(),
        {"contractorId": 7, "dealerName": "Sharma Traders"},
    )
    check(
        "dealer body: no projectType/totalAmount keys ever",
        sorted(DealerDto(contractorId=7, dealerName="X", phoneNumber="9876543210")
               .request_body().keys()),
        ["contractorId", "dealerName", "phoneNumber"],
    )


def test_missing_required() -> None:
    check(
        "client: name only -> two fields missing",
        build_dto("CLIENT", 7, {"name": "Ramesh"}).missing_required(),
        ["phoneNumber", "projectType"],
    )
    check(
        "client: all three present -> nothing missing",
        build_dto("CLIENT", 7, {"name": "R", "phone_number": "9876543210",
                                "project_type": "LABOUR_ONLY"}).missing_required(),
        [],
    )
    check(
        "dealer: name alone is complete",
        build_dto("DEALER", 7, {"name": "Sharma Traders"}).missing_required(),
        [],
    )
    check(
        "client: totalAmount is not required",
        build_dto("CLIENT", 7, {"name": "R", "phone_number": "1",
                                "project_type": "LABOUR_ONLY"}).missing_required(),
        [],
    )


# --- merge semantics --------------------------------------------------------


def test_phone_normalization() -> None:
    cases = [
        ("bare 10 digits", "9876543210", "9876543210"),
        ("spaces and dashes", "98765 43210", "9876543210"),
        ("+91 prefix", "+919876543210", "9876543210"),
        ("91 prefix, no plus", "919876543210", "9876543210"),
        ("leading 0", "09876543210", "9876543210"),
        ("formatted", "+91-98765-43210", "9876543210"),
    ]
    for label, spoken, expected in cases:
        ex = Extractor(_settings())
        result = ex._merge("t", _extraction(phone_number=spoken))
        check(f"phone: {label}", ex.fields.get("phone_number"), expected)
        check(f"phone: {label} produced no warning", result.warnings, [])

    for label, spoken in [("9 digits", "987654321"), ("7 digits", "9876543"),
                          ("13 digits", "9876543210123")]:
        ex = Extractor(_settings())
        result = ex._merge("t", _extraction(phone_number=spoken))
        check(f"phone: {label} is dropped, not truncated",
              ex.fields.get("phone_number"), None)
        check(f"phone: {label} warns", len(result.warnings), 1)


def test_accumulation_across_turns() -> None:
    ex = Extractor(_settings())
    ex._merge("naya client Ramesh Kumar", _extraction(name="Ramesh Kumar"))
    ex._merge("phone nau aath...", _extraction(phone_number="9876543210"))
    ex._merge("material aur labour", _extraction(project_type="MATERIAL_AND_LABOUR"))
    final = ex._merge("do lakh pachaas hazaar", _extraction(total_amount=250000))

    check("accumulate: record is complete", final.is_complete, True)
    check(
        "accumulate: body matches the four spoken turns",
        final.dto.request_body(),
        {"contractorId": 7, "clientName": "Ramesh Kumar", "phoneNumber": "9876543210",
         "projectType": "MATERIAL_AND_LABOUR", "totalAmount": 250000.0},
    )


def test_nulls_never_clear() -> None:
    ex = Extractor(_settings())
    ex._merge("t", _extraction(name="Ramesh", phone_number="9876543210"))
    ex._merge("t", _extraction(project_type="LABOUR_ONLY"))  # name/phone null here
    check("null fields do not erase collected values",
          (ex.fields.get("name"), ex.fields.get("phone_number")),
          ("Ramesh", "9876543210"))


def test_correction_overwrites() -> None:
    ex = Extractor(_settings())
    ex._merge("t", _extraction(name="Ramesh", project_type="MATERIAL_AND_LABOUR"))
    result = ex._merge("nahi, sirf labour",
                       _extraction(project_type="LABOUR_ONLY", is_correction=True))
    check("correction: projectType flipped", ex.fields["project_type"], "LABOUR_ONLY")
    check("correction: name survived", ex.fields["name"], "Ramesh")
    check("correction: reported as changed", result.changed, {"project_type": "LABOUR_ONLY"})


def test_none_and_low_confidence_are_ignored() -> None:
    ex = Extractor(_settings())
    ex._merge("t", _extraction(name="Ramesh"))

    ex._merge("weather chatter", _extraction(entity="NONE", confidence=0.9, name="Nonsense"))
    check("NONE leaves state untouched", ex.fields, {"name": "Ramesh"})

    result = ex._merge("mumble", _extraction(name="Garbled", confidence=0.2))
    check("low confidence leaves state untouched", ex.fields, {"name": "Ramesh"})
    check("low confidence warns", len(result.warnings), 1)


def test_also_heard_is_reported_on_every_path() -> None:
    """A second entity in a turn is never captured, so it must always be said."""
    ex = Extractor(_settings())
    result = ex._merge(
        "naya client Ramesh Kumar ... dealer Dermot Traders",
        _extraction(name="Ramesh Kumar", also_heard="DEALER Dermot Traders"),
    )
    check("also_heard: surfaced on a captured turn",
          result.also_heard, "DEALER Dermot Traders")
    check("also_heard: does not leak into the record",
          ex.fields, {"name": "Ramesh Kumar"})

    # It must survive the paths that record nothing, or the speaker believes the
    # second entity was saved.
    none_result = ex._merge("chatter", _extraction(
        entity="NONE", confidence=0.9, also_heard="DEALER Dermot Traders"))
    check("also_heard: surfaced on a NONE turn",
          none_result.also_heard, "DEALER Dermot Traders")

    low_result = ex._merge("mumble", _extraction(
        confidence=0.1, also_heard="CLIENT Meena"))
    check("also_heard: surfaced on a low-confidence turn",
          low_result.also_heard, "CLIENT Meena")

    strict = Extractor(_settings(strict_entity=True))
    strict._merge("t", _extraction(name="Ramesh"))
    refused = strict._merge("dealer Sharma", _extraction(
        entity="DEALER", name="Sharma", also_heard="CLIENT Meena"))
    check("also_heard: surfaced on a refused switch",
          refused.also_heard, "CLIENT Meena")

    blank = ex._merge("t", _extraction(name="X", also_heard="   "))
    check("also_heard: blank string normalizes to None", blank.also_heard, None)


def test_contradiction_keeps_the_record() -> None:
    """One muddled field must not cost the whole record (the live Kannada bug)."""
    ex = Extractor(_settings())
    # What the model should now return for "Hosa grahaka Ramesh Kumar ...
    # kooli matra samagri jothe ... dealer Dermot Traders": entity classified,
    # project_type null because it was stated both ways, the rest kept.
    result = ex._merge(
        "Hosa grahaka Ramesh Kumar phone 9266524312. Kooli matra samagri jothe "
        "dealer Dermot Traders.",
        _extraction(
            name="Ramesh Kumar", phone_number="9266524312", project_type=None,
            also_heard="DEALER Dermot Traders",
            notes="projectType stated both ways (kooli matra / samagri jothe)",
        ),
    )
    check("contradiction: entity still classified", result.entity, "CLIENT")
    check("contradiction: other fields survive",
          (ex.fields.get("name"), ex.fields.get("phone_number")),
          ("Ramesh Kumar", "9266524312"))
    check("contradiction: the muddled field alone is missing",
          result.missing, ["projectType"])
    check("contradiction: second entity reported",
          result.also_heard, "DEALER Dermot Traders")
    check("contradiction: not silently complete", result.is_complete, False)


def test_no_digit_words_in_vocabulary() -> None:
    """Romanized digit words compete across languages under auto-detect.

    SMART mode already emits spoken digits as numerals, so these belong in the
    extraction prompt (which still maps them) and not in the ASR bias list.
    """
    from .config import CUSTOM_VOCABULARY

    banned = {
        # Hindi / Urdu
        "shunya", "ek", "do", "teen", "chaar", "paanch", "chhe", "saat",
        "aath", "nau", "das",
        # Kannada
        "sonne", "sunne", "ondu", "eradu", "mooru", "naalku", "naalu", "aidu",
        "aaru", "elu", "entu", "ombattu", "hattu",
    }
    present = sorted(banned.intersection(term.lower() for term in CUSTOM_VOCABULARY))
    check("vocabulary: no romanized digit words", present, [])
    # The scales are content words and stay.
    lowered = {term.lower() for term in CUSTOM_VOCABULARY}
    check("vocabulary: amount scales retained",
          {"lakh", "savira", "laksha", "koti"}.issubset(lowered), True)


def test_transcriber_reaps_children_on_cancel() -> None:
    """Ctrl+C must not leave an unretrieved exception behind.

    Reproduces the live traceback: cancelling _one_session while it waits used
    to skip every line of cleanup, so _pump_text died on the closing socket and
    asyncio logged "Task exception was never retrieved".
    """
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
        await asyncio.sleep(0.05)  # let `failed` finish with its exception

        await _shutdown((failed, idle, None))

        # exception() returning without raising means it was retrieved; asyncio
        # only warns about tasks whose exception nobody ever read.
        retrieved = failed.done() and failed.exception() is not None
        return retrieved, idle.cancelled()

    retrieved, idle_cancelled = asyncio.run(scenario())
    check("shutdown: failed child's exception is consumed", retrieved, True)
    check("shutdown: pending child is cancelled", idle_cancelled, True)


def test_entity_switch_drops_client_only_fields() -> None:
    ex = Extractor(_settings())
    ex._merge("t", _extraction(name="Ramesh", project_type="LABOUR_ONLY", total_amount=5000))
    result = ex._merge("dealer Sharma Traders", _extraction(entity="DEALER", name="Sharma Traders"))

    check("switch: entity is now DEALER", ex.entity, "DEALER")
    check("switch: client-only fields are gone",
          sorted(ex.fields.keys()), ["name"])
    check("switch: body is a clean dealer body",
          result.dto.request_body(),
          {"contractorId": 7, "dealerName": "Sharma Traders"})
    check("switch: warned about it", any("switched" in w for w in result.warnings), True)


def test_strict_entity_refuses_switch() -> None:
    ex = Extractor(_settings(strict_entity=True))
    ex._merge("t", _extraction(name="Ramesh"))
    result = ex._merge("dealer Sharma", _extraction(entity="DEALER", name="Sharma"))
    check("strict: entity unchanged", ex.entity, "CLIENT")
    check("strict: state unchanged", ex.fields, {"name": "Ramesh"})
    check("strict: refusal warned", any("refused switch" in w for w in result.warnings), True)


def test_manual_edit() -> None:
    ex = Extractor(_settings())
    ex._merge("t", _extraction(name="Ramesh", phone_number="9876543210",
                               project_type="LABOUR_ONLY"))
    ex.set_field("name", "Rajesh Kumar")
    check("edit: overwrite applied", ex.current_dto().request_body()["clientName"], "Rajesh Kumar")
    ex.set_field("project_type", None)
    check("edit: clearing marks the field missing",
          ex.current_dto().missing_required(), ["projectType"])


def test_filter_envelope_parsing() -> None:
    """The duplicate probe must read Utils.ApiPaginatedResult correctly."""
    rows = [{"id": 11, "clientName": "Ramesh Kumar", "phoneNumber": "9999988888",
             "projectType": "LABOUR_ONLY", "isActive": True}]
    populated = {"result": {"response": {"status": "SUCCESS"}, "data": rows,
                            "offset": 0, "pageSize": 10, "totalPages": 1, "totalRecords": 1}}
    check("envelope: rows extracted", parse_filter_rows(populated), (rows, None))

    # An empty page serializes data as null — a valid "no matches", not a failure.
    empty = {"result": {"response": {"status": "SUCCESS"}, "data": None,
                        "offset": 0, "pageSize": 10, "totalPages": 0, "totalRecords": 0}}
    check("envelope: data null means no matches", parse_filter_rows(empty), ([], None))
    check("envelope: empty list means no matches",
          parse_filter_rows({"result": {"data": []}}), ([], None))

    check("envelope: flat/legacy shape reports a parse error",
          parse_filter_rows({"statusCode": 200, "data": rows})[1],
          "duplicate check: unrecognised response envelope")
    check("envelope: error body reports a parse error",
          parse_filter_rows({"result": {"response": {"status": "ERROR"}}})[1],
          "duplicate check: unrecognised response envelope")


def test_describe_match() -> None:
    check(
        "describe: exact match is flagged",
        describe_match({"id": 11, "clientName": "Ramesh Kumar", "phoneNumber": "9999988888",
                        "projectType": "LABOUR_ONLY", "isActive": True}, "ramesh kumar"),
        "id=11  Ramesh Kumar  9999988888  LABOUR_ONLY  ← exact match",
    )
    check(
        "describe: substring match is not flagged, inactive is",
        describe_match({"id": 12, "clientName": "Ramesh Kumar Sons", "phoneNumber": None,
                        "isActive": False}, "Ramesh Kumar"),
        "id=12  Ramesh Kumar Sons  —  inactive",
    )
    check(
        "describe: dealer row",
        describe_match({"id": 3, "dealerName": "Sharma Traders", "phoneNumber": "9876543210"}),
        "id=3  Sharma Traders  9876543210",
    )


def test_curl_rendering() -> None:
    settings = _settings(user_id="42")
    dto = ClientDto(contractorId=7, clientName="Ramesh Kumar", phoneNumber="9876543210",
                    projectType="LABOUR_ONLY")
    rendered = curl_for(settings, dto)
    check("curl: targets the client endpoint",
          "http://localhost:8092/starship/v1/client" in rendered, True)
    check("curl: carries the audit header", "X-USER-ID: 42" in rendered, True)
    check("curl: dealer goes to material-dealer",
          "/starship/v1/material-dealer" in curl_for(
              settings, DealerDto(contractorId=7, dealerName="Sharma Traders")),
          True)


def test_turn_buffer() -> None:
    ex = Extractor(_settings())
    check("buffer: empty at rest", ex.has_turn, False)
    ex.add_final("naya client")
    check("buffer: fills on a final", ex.has_turn, True)
    ex.reset_record()
    check("reset: clears entity and fields", (ex.entity, ex.fields), (None, {}))


# --- runner -----------------------------------------------------------------

TESTS = [
    ("client validator parity", test_client_validator_parity),
    ("dealer validator parity", test_dealer_validator_parity),
    ("request bodies", test_request_bodies),
    ("missing required fields", test_missing_required),
    ("phone normalization", test_phone_normalization),
    ("accumulation across turns", test_accumulation_across_turns),
    ("nulls never clear", test_nulls_never_clear),
    ("corrections overwrite", test_correction_overwrites),
    ("NONE / low confidence ignored", test_none_and_low_confidence_are_ignored),
    ("also_heard reporting", test_also_heard_is_reported_on_every_path),
    ("contradiction keeps record", test_contradiction_keeps_the_record),
    ("no digit words in vocabulary", test_no_digit_words_in_vocabulary),
    ("transcriber reaps children", test_transcriber_reaps_children_on_cancel),
    ("entity switch", test_entity_switch_drops_client_only_fields),
    ("strict entity", test_strict_entity_refuses_switch),
    ("manual edit", test_manual_edit),
    ("filter envelope parsing", test_filter_envelope_parsing),
    ("match descriptions", test_describe_match),
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
