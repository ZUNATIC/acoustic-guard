# Acoustic Guard Engine

ExfilGuard's optional microphone module. It detects **spoken disclosure of sensitive
information** (passwords, project codenames, CNIC / card numbers, financial and personal data,
talk of taking data out) in **English, Urdu, Roman Urdu, Hindi, Arabic, Persian** and the other
languages Whisper recognises.

Everything runs locally. **No audio is ever written to disk or sent anywhere.**

![architecture](docs/architecture.png)

```
mic → Silero VAD → segment ─┬─ fast path: Whisper tiny, rolling window → early warning
                            └─ Whisper small (one encoder pass: language + transcript + English)
                                   → keyword · phonetic · spoken-number · semantic intent → severity
                                   → second opinion (large-v3-turbo) on risky segments
                                   → SQLite hash-chained audit · backend (HTTPS + HMAC) · live dashboard
```

## Quick start

Setup is one command; it creates the Python environment, installs everything, downloads the
models (one time) and writes the configuration. At the end it prints the command to start.

**Linux**

```bash
./setup.sh            # standard (~2.3 GB of models)
./setup.sh --light    # machines with 8 GB RAM or less (~0.7 GB, no second-opinion model)
./setup.sh --lan      # also reachable from other computers (HTTPS + access token)

./run.sh              # start, any time after setup
```

**Windows (PowerShell)**

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1           # or .\setup.ps1 -Light, .\setup.ps1 -Lan
.\run.ps1             # start
```

Open the address that setup printed (normally **http://127.0.0.1:8001**), choose the microphone source, click **Start monitoring**, enter
your name and confirm consent. API docs are at `/docs`.

- **This device**: the browser asks for microphone permission and uses the microphone of the
  computer the page is open on (USB headsets included; newly plugged-in microphones are picked
  up automatically).
- **Endpoint microphone**: the service listens through a microphone attached to the machine it
  runs on.

**From another computer on the network**: run setup with `--lan` (`-Lan` on Windows). It creates
a self-signed HTTPS certificate (browsers only allow microphones on https pages) and an access
token, and prints both the address and the token. On the other computer open that address,
accept the certificate warning and enter the token.

Manual steps (any OS): create a venv, `pip install -r requirements.txt`, `python scripts/fetch_models.py`, `python scripts/configure.py`, `python -m app`.

## What you get

- **Multilingual detection.** Automatic language identification, native-script transcripts
  (Urdu shown in Urdu script) and English translation, with a keyword registry of 330 phrases
  in 6 languages.
- **Four detectors, fused.** Exact keywords, phonetic matching for misspelled transcripts,
  spoken-number patterns (CNIC, Luhn-valid cards, IBAN, phone, IP) and semantic intent for
  paraphrases.
- **Early warning** while the speaker is still talking, and a **second opinion** from a larger
  model on anything suspicious.
- **Environment analysis.** Sudden sounds, raised voices, ambient shifts and muted-microphone
  detection.
- **Hardware aware.** Disables itself cleanly without a microphone and re-enables on hot-plug.
- **Privacy by design.** Consent gate with the operator's name, hash-only mode, masked numbers,
  public keyword list, retention purge, and a tamper-evident SHA-256 audit chain
  (`/api/v1/audit/verify`).
- **Backend-ready.** Signed events to the ExfilGuard server with a retrying outbox.
- **Operator console.** Monitor, Alerts, Environment, Policy, Audit & privacy and Test pages;
  live level meter, per-sentence verdicts with what was detected and how, CSV export, erasure,
  text / recorded clip / WAV testing. Light and dark themes, right-to-left text, works on a
  phone screen, no external requests.

## Measured on an Intel Core i5-8350U laptop (no GPU)

| | |
|---|---|
| Detection on the 20-clip, 5-language test set | 20 / 20, no false alarms on harmless speech |
| Full live QA (21 clips through the dashboard) | 21 / 21 correct |
| Alert latency (end of speech → dashboard) | 2.7 – 4.7 s in the live QA run |
| CPU while monitoring a quiet room | 0.7 % |
| RAM | 3.25 GB standard · 1.6 GB light |

## Tests

```bash
pytest                                      # full suite: 177 tests, no microphone needed
ACOUSTIC_TEST_VERIFIER=1 pytest -m verifier # second-opinion model tests (slow)
```

## Layout

```
app/          service code (FastAPI app, audio engine, models, threat engine, storage)
config/       threats.yaml - detection policy, editable live via PUT /api/v1/policy
static/       operator console + browser capture worklet (no build step, no CDN)
scripts/      fetch_models.py, configure.py (writes .env, HTTPS certificate, token), make_test_speech.py
tests/        pytest suite + synthetic multilingual speech fixtures
deploy/       systemd user unit
docs/         MODULE_DOCUMENT.md (full design, API, compliance, results, Q&A), architecture diagram
```

Configuration: copy `.env.example` to `.env`. Every option is documented in
`docs/MODULE_DOCUMENT.md` §10.
