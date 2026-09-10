"""Live extraction soak: `python -m voice_logging.hardtest` (or `.audio.hardtest`).

Feeds long contractor sessions through the same Flash extraction + merge path
as `--text` / the mic loop. Needs GEMINI_API_KEY. No microphone.

    python -m voice_logging.hardtest
    python -m voice_logging.hardtest --list
    python -m voice_logging.hardtest --only site-dump-oneshot,drop-minefield
    python -m voice_logging.hardtest --dump   # print turns, do not call the API

Calls are paced (--rpm, default 10) because the free tier allows 15
generate_content requests per minute per model. Unpaced, the run reports
extraction failures that are really 429s.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import EXTRACT_MODEL, Settings, _load_dotenv
from .extractor import Extractor
from .hard_scenarios import SCENARIOS, Scenario, by_id


UOM_CANON: dict[str, str] = {}
for _canon, _aliases in {
    "kg": ("kg", "kilo", "kilos", "kilogram", "kilograms"),
    "bag": ("bag", "bags"),
    "tin": ("tin", "tins"),
    "packet": ("packet", "packets"),
    "peti": ("peti", "carton", "cartons", "box", "boxes"),
    "nag": ("nag", "nags", "number", "numbers", "nos", "no"),
    "bori": ("bori", "boriya", "boris"),
    "roll": ("roll", "rolls"),
    "litre": ("litre", "liter", "litres", "liters", "ltr"),
    "dabba": ("dabba", "dabbas"),
    "katta": ("katta", "kattas"),
    "thaila": ("thaila", "thailas"),
    "quintal": ("quintal", "quintals"),
    "bundle": ("bundle", "bundles"),
    "piece": ("piece", "pieces", "pcs", "pc"),
}.items():
    for alias in _aliases:
        UOM_CANON[alias] = _canon


def _norm_name(text: str) -> str:
    text = text.casefold()
    text = text.replace("doctor", "dr")
    text = text.replace("em seal", "m-seal").replace("m seal", "m-seal")
    text = text.replace("fee vicol", "fevicol").replace("fevi col", "fevicol")
    text = text.replace("fevi quick", "fevikwik").replace("fevi kwik", "fevikwik")
    text = text.replace("ply wood", "plywood")
    text = re.sub(r"[^a-z0-9\u0900-\u097f\u0a80-\u0aff\u0c80-\u0cff+]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # "Fevicol S.H." / "T M T" / "L W plus" collapse to sh / tmt / lw.
    while True:
        collapsed = re.sub(r"\b([a-z0-9]) (?=[a-z0-9]\b)", r"\1", text)
        if collapsed == text:
            break
        text = collapsed
    return text


def _canon_uom(uom: str) -> str:
    key = re.sub(r"[^a-z]+", "", uom.casefold())
    return UOM_CANON.get(key, key)


def _settings(api_key: str, model: str) -> Settings:
    return Settings(
        api_key=api_key,
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
        text_mode=True,
        post=False,
        dry_run=False,
        verbose=False,
        log_path=Path("voice_logging.hardtest.log"),
        transcript_mode="SMART",
        extract_model=model,
    )


@dataclass
class RowMatch:
    expected: str
    ok: bool
    detail: str


@dataclass
class ScenarioReport:
    scenario: Scenario
    ok: bool
    rows: list[RowMatch]
    extras: list[str]
    forbidden_hits: list[str]
    turns_run: int
    errors: list[str]
    final_items: list[dict]
    notes: list[str]


def _items_from_extractor(extractor: Extractor) -> list[dict]:
    dto = extractor.current_dto()
    if dto is None:
        return []
    return list(dto.request_body().get("orderItems") or [])


def _row_matches(item: dict, expected) -> bool:
    name = _norm_name(str(item.get("itemName") or ""))
    qty = int(item.get("quantity") or 0)
    uom = _canon_uom(str(item.get("uom") or ""))
    if qty != expected.quantity or uom != _canon_uom(expected.uom):
        return False
    if not all(_norm_name(needle) in name for needle in expected.needles):
        return False
    # Bare "cement" must not steal "white cement".
    if expected.needles == ("cement",) and "white" in name:
        return False
    return True


def _score(scenario: Scenario, items: list[dict], errors: list[str],
           notes: list[str], turns_run: int) -> ScenarioReport:
    used: set[int] = set()
    rows: list[RowMatch] = []
    for expected in scenario.expect:
        hit = None
        for i, item in enumerate(items):
            if i in used:
                continue
            if _row_matches(item, expected):
                hit = i
                break
        if hit is None:
            rows.append(RowMatch(
                expected.label, False,
                "missing — final list: " + (
                    ", ".join(
                        f"{it.get('quantity')} {it.get('uom')} {it.get('itemName')}"
                        for it in items
                    ) or "(empty)"
                ),
            ))
        else:
            used.add(hit)
            got = items[hit]
            rows.append(RowMatch(
                expected.label, True,
                f"{got.get('quantity')} {got.get('uom')} {got.get('itemName')}",
            ))

    extras = [
        f"{item.get('quantity')} {item.get('uom')} {item.get('itemName')}"
        for i, item in enumerate(items) if i not in used
    ]
    if not scenario.exact_count:
        extras = []

    forbidden_hits: list[str] = []
    for item in items:
        name = _norm_name(str(item.get("itemName") or ""))
        for token in scenario.forbidden:
            if _norm_name(token) and _norm_name(token) in name:
                forbidden_hits.append(
                    f"{item.get('itemName')!r} contains forbidden {token!r}"
                )

    ok = (
        all(row.ok for row in rows)
        and not extras
        and not forbidden_hits
        and not errors
    )
    return ScenarioReport(
        scenario=scenario, ok=ok, rows=rows, extras=extras,
        forbidden_hits=forbidden_hits, turns_run=turns_run, errors=errors,
        final_items=items, notes=notes,
    )


class RateLimiter:
    """Spaces extraction calls so a free-tier RPM cap is never the failure.

    The free tier allows 15 generate_content requests per minute per model, and
    a scenario is dozens of turns fired as fast as the loop can go. Without
    spacing every run past the first minute fails on 429 and the scores say
    nothing about extraction quality.
    """

    def __init__(self, rpm: int) -> None:
        self._interval = 60.0 / rpm if rpm > 0 else 0.0
        self._next_at = 0.0

    async def acquire(self) -> None:
        if self._interval <= 0:
            return
        now = asyncio.get_running_loop().time()
        if now < self._next_at:
            await asyncio.sleep(self._next_at - now)
        self._next_at = asyncio.get_running_loop().time() + self._interval

    def back_off(self, seconds: float) -> None:
        self._next_at = asyncio.get_running_loop().time() + seconds


def _quota_retry_delay(error: str) -> float | None:
    """Seconds the server asked us to wait, or None if this is not a 429."""
    if "429" not in error and "RESOURCE_EXHAUSTED" not in error:
        return None
    match = re.search(r"[Pp]lease retry in ([0-9.]+)s", error)
    if not match:
        match = re.search(r"'retryDelay':\s*'([0-9.]+)s'", error)
    if match:
        return min(float(match.group(1)) + 1.0, 90.0)
    return 30.0


def _short_error(error: str) -> str:
    if "RESOURCE_EXHAUSTED" in error or "429" in error:
        return "429 quota exhausted (raise --rpm spacing or wait for the window)"
    return error.split("\n", 1)[0][:200]


async def _flush_with_retry(
    extractor: Extractor, turn: str, limiter: RateLimiter, max_retries: int,
):
    """One turn, retried while the failure is a quota refusal."""
    result = None
    for attempt in range(max_retries + 1):
        await limiter.acquire()
        extractor.add_final(turn)
        result = await extractor.flush()
        if result is None or not result.error:
            return result
        delay = _quota_retry_delay(result.error)
        if delay is None or attempt == max_retries:
            return result
        limiter.back_off(delay)
    return result


async def _run_scenario(
    scenario: Scenario, settings: Settings, limiter: RateLimiter, max_retries: int,
) -> ScenarioReport:
    extractor = Extractor(settings)
    errors: list[str] = []
    notes: list[str] = []
    turns_run = 0
    for turn in scenario.turns:
        result = await _flush_with_retry(extractor, turn, limiter, max_retries)
        turns_run += 1
        if result is None:
            errors.append(f"turn {turns_run}: flush returned nothing")
            continue
        if result.error:
            errors.append(f"turn {turns_run}: {_short_error(result.error)}")
        if result.extraction and result.extraction.notes:
            notes.append(f"t{turns_run}: {result.extraction.notes}")
        notes.extend(f"t{turns_run}: {w}" for w in result.warnings)
    return _score(scenario, _items_from_extractor(extractor), errors, notes, turns_run)


def _print_report(report: ScenarioReport, verbose: bool) -> None:
    mark = "ok  " if report.ok else "FAIL"
    print(f"  {mark} {report.scenario.id}  ({report.turns_run} turns)  {report.scenario.title}")
    if report.ok and not verbose:
        return
    for row in report.rows:
        print(f"         {'+' if row.ok else 'x'} {row.expected}")
        if not row.ok or verbose:
            print(f"           {row.detail}")
    for extra in report.extras:
        print(f"         x extra row: {extra}")
    for hit in report.forbidden_hits:
        print(f"         x {hit}")
    for err in report.errors:
        print(f"         x {err}")
    if verbose:
        for note in report.notes:
            print(f"         · {note}")
        body_items = report.final_items
        print("         final:", body_items)


def _dump_scenarios(chosen: list[Scenario]) -> None:
    for scenario in chosen:
        print(f"# {scenario.id} — {scenario.title}")
        if scenario.notes:
            print(f"# {scenario.notes}")
        for turn in scenario.turns:
            print(turn)
        print()
        print("# expect:")
        for row in scenario.expect:
            print(f"#   {row.label}")
        print()


def _pick(only: str | None) -> list[Scenario]:
    if not only:
        return list(SCENARIOS)
    wanted = [part.strip() for part in only.split(",") if part.strip()]
    chosen: list[Scenario] = []
    missing: list[str] = []
    for name in wanted:
        scenario = by_id(name)
        if scenario is None:
            missing.append(name)
        else:
            chosen.append(scenario)
    if missing:
        known = ", ".join(s.id for s in SCENARIOS)
        raise SystemExit(f"unknown scenario(s): {', '.join(missing)}\nknown: {known}")
    return chosen


async def _main_async(args: argparse.Namespace) -> int:
    chosen = _pick(args.only)
    if args.list:
        for scenario in chosen:
            print(f"{scenario.id:24}  {len(scenario.turns):2} turns  "
                  f"{len(scenario.expect):2} rows  {scenario.title}")
        return 0
    if args.dump:
        _dump_scenarios(chosen)
        return 0

    _load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print(
            "GEMINI_API_KEY is missing. Export it, or copy "
            ".env.example to .env.",
            file=sys.stderr,
        )
        return 2

    model = (args.model or os.environ.get("EXTRACT_MODEL") or EXTRACT_MODEL).strip()
    settings = _settings(api_key, model)
    total_turns = sum(len(s.turns) for s in chosen)
    eta = total_turns * 60.0 / args.rpm / 60.0 if args.rpm > 0 else 0.0
    print(f"hardtest  {len(chosen)} scenario(s)  {total_turns} turns  extract={model}")
    print(f"          paced at {args.rpm} req/min — about {eta:.1f} min\n")

    limiter = RateLimiter(args.rpm)
    reports: list[ScenarioReport] = []
    for scenario in chosen:
        reports.append(
            await _run_scenario(scenario, settings, limiter, args.max_retries)
        )
        _print_report(reports[-1], verbose=args.verbose)

    passed = sum(1 for r in reports if r.ok)
    failed = len(reports) - passed
    quota_hit = sum(1 for r in reports if any("429" in e for e in r.errors))
    print()
    summary = f"{passed} passed, {failed} failed  ({total_turns} API turns)"
    if quota_hit:
        summary += (
            f"\n{quota_hit} scenario(s) still hit the quota — rerun them with "
            f"--rpm {max(args.rpm - 4, 2)}"
        )
    print(summary)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m voice_logging.audio.hardtest")
    parser.add_argument("--only", help="Comma-separated scenario ids")
    parser.add_argument("--list", action="store_true", help="List scenarios and exit")
    parser.add_argument("--dump", action="store_true",
                        help="Print turns (for --text paste) and exit")
    parser.add_argument("--model", help="Override EXTRACT_MODEL")
    parser.add_argument("--rpm", type=int, default=10,
                        help="Extraction calls per minute. The free tier caps "
                             "gemini-3.5-flash-lite at 15.")
    parser.add_argument("--max-retries", type=int, default=2,
                        help="Retries per turn when the API refuses on quota.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
