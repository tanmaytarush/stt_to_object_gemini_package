# voice_logging

Speak the **Items** section of the New Order screen — item, quantity, unit —
in Hindi / Gujarati / Marathi / Kannada / Indian-English, and get a validated
`CreateMaterialOrderRequestDto` out.

Everything else on that screen is filled in by hand and passed in as a flag:

| On the screen | Where it comes from |
|---|---|
| Dealer (optional) | `--dealer-id` |
| Client (optional) | `--client-id` |
| Order type | `--order-type` (default `MAT_ORDER`) |
| **Items — item / qty / UOM** | **voice** |

Speech never sets an id, a name, a phone number, a price or a date. If the
model invents one it is dropped before it can reach the request body, and
there is a test for that.

Standalone Python. Nothing in the Go service depends on it, and by default it
never writes anything — it prints the JSON body and stops there.

```
mic ──16 kHz PCM──▶ gemini-3.5-transcribe-live ──finals──▶ turn buffer
                     SMART mode · custom vocab                 │ 1.5 s of quiet
                                                               ▼
                                       gemini-3.5-flash-lite + response_schema
                                                               │ item rows
                                                               ▼
                                          merge ▶ validate ▶ print ▶ [--post]
```

## Setup

```bash
cd /path/to/starship
python3 -m venv .venv
source .venv/bin/activate
pip install -r scripts/voice_logging/requirements.txt

export GEMINI_API_KEY=...        # https://aistudio.google.com/apikey
```

Or copy `.env.example` to `scripts/voice_logging/.env` and fill it in — it is
loaded automatically, and real environment variables always win over it.

On macOS, grant mic access to your terminal in
**System Settings → Privacy & Security → Microphone**.

All commands below run from the `scripts/` directory.

## Running

```bash
cd scripts

# Check the plumbing first — no API key, no mic, ~1 second.
python -m voice_logging.selftest

# Which mic will it use?
python -m voice_logging --list-devices

# Items for an order against a client. Writes nothing.
python -m voice_logging --contractor-id 1 --client-id 12

# Against a dealer instead, or both, or neither — same as the screen.
python -m voice_logging --contractor-id 1 --dealer-id 5

# Extraction only — type transcripts instead of speaking. No mic, no STT quota.
python -m voice_logging --contractor-id 1 --client-id 12 --text
```

Once the transcription looks right, wire it to the API:

```bash
python -m voice_logging --contractor-id 1 --client-id 12 --post --dry-run
python -m voice_logging --contractor-id 1 --client-id 12 --user-id 1 --post
```

## How a session goes

Rows accumulate. Each turn adds to the list; a correction replaces it.

```
  screen     MAT_ORDER · client 12   (voice fills items only)

  · das bag cement aur paanch kilo Fevicol SH
  added
      10 bag  cement
      5 kg  Fevicol SH
  2 item(s) in the list

  · do tin Dr. Fixit, aur thoda putty
  ! dropped 1 incomplete row(s) — a row needs a name, a quantity above 0, and a unit
  added
      2 tin  Dr. Fixit
  3 item(s) in the list
  note: putty had no quantity or unit

  · nahi, aath bag cement aur paanch kilo Fevicol SH
  replaced with
      8 bag  cement
      5 kg  Fevicol SH
  2 item(s) in the list

  ── MATERIAL_ORDER ready ── POST http://localhost:8092/starship/v1/material-order
  {
    "contractorId": 1,
    "orderType": "MAT_ORDER",
    "orderUrls": [],
    "amount": 0.0,
    "orderItems": [
      {"itemName": "cement", "quantity": 8, "uom": "bag"},
      {"itemName": "Fevicol SH", "quantity": 5, "uom": "kg"}
    ],
    "clientId": 12
  }
  [Enter] next order  [e] edit an item  [k] keep adding  [q] quit >
```

`e` lists the rows and lets you retype one, delete it with `-`, or add one —
the fastest fix when a material name keeps coming through wrong.

Voice never fills `orderUrls`; that is the screen's "Upload Item List" button.
The Go validator requires items XOR urls, so the body always sends
`"orderUrls": []`.

## Flags

| Flag | Default | Notes |
|---|---|---|
| `--contractor-id` | — | Required. Never inferred from speech. Env `CONTRACTOR_ID`. |
| `--client-id` | — | Optional, like the screen's dropdown. Env `CLIENT_ID`. |
| `--dealer-id` | — | Optional, like the screen's dropdown. Env `DEALER_ID`. |
| `--order-type` | `MAT_ORDER` | `MAT_LIST` for a list that is not an order yet. Env `ORDER_TYPE`. |
| `--user-id` | `1` | Sent as `X-USER-ID`; drives `created_by`. Env `STARSHIP_USER_ID`. |
| `--base-url` | `http://localhost:8092` | Env `STARSHIP_BASE_URL`. |
| `--languages` | auto-detect | One code locks that language. Two or more are a note only. Env `STT_LANGUAGES`. |
| `--lock-languages` | off | Force the full `--languages` list through as hints anyway. |
| `--silence` | `1.5` | Seconds of quiet that end a turn. |
| `--min-confidence` | `0.55` | Rows below this are dropped, but still printed so you can see what was heard. |
| `--transcript-mode` | `SMART` | `VERBATIM` for a literal transcript with fillers. |
| `--text` | off | Read transcripts from stdin. No mic, no STT. |
| `--post` | off | Offer to POST each completed order. |
| `--dry-run` | off | With `--post`, print curl instead of sending. |
| `--verbose` | off | JSONL trace of every stage to `--log-file`. |
| `--input-device` / `--list-devices` | — | Pick the mic explicitly. |

## Things worth knowing

**A row is all three or nothing.** `itemName`, `quantity > 0` and `uom` — a
material named with no quantity, or a quantity with no unit, is dropped with a
note rather than guessed at. Half a row that looks captured is worse than a
missing one, because the user only re-says what they can see is missing. Units
are never converted, and fractional quantities (`dedh bag`, `sawa tin`) are
dropped rather than rounded.

**Corrections replace the whole list.** After a `nahi` / `illa` / `no, not
that`, the model resends every row, not just the changed one. That is why
`is_correction` swaps the list instead of appending.

**Sessions rotate every ~9m30s.** The service caps a live transcription session
at 10 minutes. The connection is replaced before that, and a half-built item
list survives the boundary.

**The language is auto-detected, and sending no hint is deliberate.** A
multi-language hint list (`hi-IN` + `kn-IN`) makes Hindi win and transcribes
spoken Kannada into Devanagari. One code locks that language:

```bash
python -m voice_logging --contractor-id 1 --client-id 12
python -m voice_logging --contractor-id 1 --client-id 12 --languages kn-IN
```

**Speech biasing is the cheapest accuracy lever.** `CUSTOM_VOCABULARY` in
`config.py` seeds the ASR with Pidilite brands, materials, and UOMs as spoken
(`bori`, `katta`, `nag`). If a term keeps coming through wrong, add it there
first — up to 1000 terms. Romanized digit words stay out of that list; the
extraction prompt still maps them.

**Validation mirrors the Go layer.** `schemas.py` reproduces
`Validator/MaterialOrderValidator.go` — including exact message text and the
fact that Go's `len()` counts bytes, so the 255-character `itemName` cap is
about 85 Devanagari characters. `selftest.py` asserts that parity; if the Go
validator changes, that suite is where the drift surfaces.

## Scope

The Items section of one order screen. Clients, dealers, order type, amounts
and image uploads are all handled by the screen itself.
