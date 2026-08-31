# voice_logging

Speak Hindi / Gujarati / Marathi / Kannada / Indian-English into the mic; get a
validated `CreateClientRequestDto` or `CreateDealerRequestDto` out.

Standalone Python. Nothing in the Go service depends on it, and by default it
never writes anything — it prints the JSON body and stops there.

```
mic ──16 kHz PCM──▶ gemini-3.5-transcribe-live ──finals──▶ turn buffer
                     SMART mode · custom vocab                 │ 1.5 s of quiet
                     hi/gu/mr/kn/en-IN · auto-reconnect        ▼
                                            gemini-3.6-flash + response_schema
                                                               │ Extraction
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

# The main loop: talk, watch the JSON build up. Writes nothing.
python -m voice_logging --contractor-id 1

# Extraction only — type transcripts instead of speaking. No mic, no STT quota.
python -m voice_logging --contractor-id 1 --text
```

Once the transcription and extraction look right, wire it to the API:

```bash
# Prints the equivalent curl instead of sending.
python -m voice_logging --contractor-id 1 --post --dry-run

# Actually POSTs — after showing you the body and asking.
python -m voice_logging --contractor-id 1 --user-id 1 --post
```

## How a session goes

Speak one field at a time; they merge into one record.

```
  · naya client Ramesh Kumar
  captured [CLIENT] name='Ramesh Kumar'
  still need: phoneNumber, projectType

  · phone nau aath saat chhe paanch chaar teen do ek shunya
  captured [CLIENT] phoneNumber='9876543210'
  still need: projectType

  · material aur labour dono, total do lakh pachaas hazaar
  captured [CLIENT] projectType='MATERIAL_AND_LABOUR'  totalAmount=250000.0

  ── CLIENT ready ── POST http://localhost:8092/starship/v1/client
  {
    "contractorId": 1,
    "clientName": "Ramesh Kumar",
    "phoneNumber": "9876543210",
    "projectType": "MATERIAL_AND_LABOUR",
    "totalAmount": 250000.0
  }
  [Enter] next record  [e] edit  [k] keep refining  [q] quit >
```

Say `nahi, sirf labour` and only `projectType` changes — the rest of the record
survives. Say `dealer Sharma Traders` and it switches to the dealer schema and
drops the client-only fields.

`e` lets you type a value for one field, which is the fastest fix when a name or
a digit keeps coming through wrong.

## Flags

| Flag | Default | Notes |
|---|---|---|
| `--contractor-id` | — | Required. Never inferred from speech. Env `CONTRACTOR_ID`. |
| `--user-id` | `1` | Sent as `X-USER-ID`; drives `created_by`. Env `STARSHIP_USER_ID`. |
| `--base-url` | `http://localhost:8092` | Env `STARSHIP_BASE_URL`. |
| `--languages` | auto-detect | One code locks that language. Two or more are a note only — see below. Env `STT_LANGUAGES`. |
| `--lock-languages` | off | Force the full `--languages` list through as hints anyway. |
| `--silence` | `1.5` | Seconds of quiet that end a turn. |
| `--min-confidence` | `0.55` | Extractions below this are dropped, but still printed so you can see what was heard. |
| `--name-script` | `latin` | `native` keeps Devanagari/Gujarati as spoken. |
| `--transcript-mode` | `SMART` | `VERBATIM` for a literal transcript with fillers. |
| `--text` | off | Read transcripts from stdin. No mic, no STT. |
| `--post` | off | Offer to POST each completed record. |
| `--dry-run` | off | With `--post`, print curl instead of sending. |
| `--strict-entity` | off | Refuse to switch CLIENT↔DEALER mid-record. |
| `--verbose` | off | JSONL trace of every stage to `--log-file`. |
| `--input-device` / `--list-devices` | — | Pick the mic explicitly. |

## Things worth knowing

**Sessions rotate every ~9m30s.** The service caps a live transcription session
at 10 minutes. The connection is replaced before that, and a half-built record
survives the boundary. You will see `rotating session` and then `reconnected`.
Audio arriving during the gap is dropped and the amount is reported, not
swallowed.

**The language is auto-detected, and sending no hint is deliberate.** By default
nothing is sent to the ASR at all — `language_codes` is documented as *"hints
about the languages present in the audio. If omitted or empty, defaults to
automatic language detection"*, and the two fields that look like they should
control this, `language_auto` and `language_hints`, are both deprecated in the
SDK. Empty is the auto-detect setting.

A multi-language hint list actively hurts: `hi-IN` and `kn-IN` together makes
Hindi win and transcribes spoken Kannada into Devanagari. So `--languages` with
two or more codes is kept as a note only. One code locks that language, and
`--lock-languages` forces the whole list through if you really want it:

```bash
python -m voice_logging --contractor-id 1                      # auto-detect
python -m voice_logging --contractor-id 1 --languages kn-IN    # lock Kannada
```

Other supported Indic codes: `ta-IN` `te-IN` `ml-IN` `bn-IN` `pa-IN` `as-IN`
`or-IN`. Auto-detect covers them already; what a new language needs is
*extraction* support — add its digit words and its "labour only" / "with
material" phrasings to `SYSTEM_PROMPT` in `extractor.py`, the way Kannada is
wired in now.

**Speech biasing is the cheapest accuracy lever.** `CUSTOM_VOCABULARY` in
`config.py` seeds the ASR with Pidilite brands, materials, UOMs as actually
spoken (`bori`, `katta`, `nag`) and amount scales. If a term keeps coming
through wrong, add it there first — up to 1000 terms.

It deliberately holds **no romanized digit words**. Under auto-detect the Hindi
and Kannada sets compete with each other, and SMART mode already emits spoken
digits as numerals unaided — a live Kannada run produced `phone number 92665243`
on its own, and `ombattu entu elu…` still resolves to `9876543210` because the
*extraction prompt* maps digit words. Biasing the ASR was the wrong layer.

**A turn that contained something never disappears.** The extractor returns
`NONE` only for speech with nothing in it. If a name and a client-or-dealer cue
are present it must classify, however messy the turn — a contradiction nulls
just that one field and says so, rather than costing you the record. When it
does return `NONE` on a turn that had a name or digits in it, it has to say why,
and that reason is printed. Two entities in one breath yields the first as the
record plus `also heard DEALER … — say it again on its own`; a turn dropped by
`--min-confidence` still prints what it heard, so you know which part to repeat.

**Phone numbers fail closed.** They are the one field where a wrong value is
worse than a missing one — `phone_number` is `NOT NULL` and indexed on `client`.
Anything that does not resolve to exactly 10 digits after stripping `+91` and a
leading `0` is dropped with a warning rather than truncated or padded.

**`projectType` is never guessed.** If the speaker did not say which, it stays
null and the record shows as incomplete. It is a required field, so guessing it
would silently mislabel the job.

**Validation mirrors the Go layer.** `schemas.py` reproduces
`Validator/ClientValidator.go` and `Validator/DealerValidator.go` — including
their exact message text and the fact that Go's `len()` counts bytes, so the
255-character cap is about 85 Devanagari characters. `selftest.py` asserts that
parity; if the Go validators change, that suite is where the drift surfaces.

**The duplicate probe is a similarity probe.** Both repositories filter with
`LIKE '%name%'`, so it surfaces near-matches too, and marks exact ones. There is
no uniqueness constraint on client or dealer names — nothing but this prompt
stops you creating the same contact three times.

## Scope

Client and dealer creation only. Material orders reference client and dealer by
ID and error if they do not exist (`MaterialOrderService.go`), so extracting one
from speech would need a name→ID resolution step against the API first.
