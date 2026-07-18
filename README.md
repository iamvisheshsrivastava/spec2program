# spec2program

**AI-assisted generation of vehicle commissioning programs from structured specifications.**

`spec2program` takes the heterogeneous data sources that drive end-of-line
vehicle commissioning — the bill of materials (ECUs), the vehicle
configuration, the supported UDS diagnostic services, software/flash versions,
and process standards — and generates a correct, ordered **commissioning
program**: open a diagnostic session, unlock security access, flash software,
write configuration parameters, validate, and clear fault codes.

Every generated program is then **validated against its specification**,
**self-repaired** if the LLM's first attempt fails that validation, scheduled
with a **critical-path optimiser** to quantify real (not naive) cycle-time
savings, and **analysed** for optimisation potential. Step durations come from
a small regression model trained on run-log data rather than a hardcoded
table. The generator is pluggable: it runs fully offline with a deterministic
rule-based planner, or against any OpenAI-compatible LLM.

![status: prototype](https://img.shields.io/badge/status-prototype-black)
![python](https://img.shields.io/badge/python-3.11-black)
![license](https://img.shields.io/badge/license-MIT-black)

---

## Why this project exists

Today, programs for vehicle commissioning are created from specifications that
carry many organisational and technical interfaces. Each hand-off is a place
where errors creep in — and those errors directly affect production stability,
cycle time, commissioning quality, and cost.

This prototype explores the core idea of **using AI to systematically reduce
those process weaknesses**: turn the structured inputs into a commissioning
program automatically, check it against the spec so mistakes are caught before
they reach the line, and quantify where cycle time can be reduced.

It is intentionally domain-honest: it models real UDS services (ISO 14229),
real safety ordering (security access before flashing), and real process
standards — not a toy.

---

## What it does

| Stage | Description |
|-------|-------------|
| **Ingest** | Reads a structured `VehicleSpec`: BOM/ECUs, configuration, UDS services, software versions, process standards. |
| **Generate** | Produces an ordered `CommissioningProgram` (session → unlock → flash → write → validate → clear DTC). |
| **Validate** | Rule-based checks: unknown ECUs, unsupported UDS services, unsafe ordering, missing coverage, broken dependencies. |
| **Self-repair** | If validation fails, the validator's findings and the failed program are fed back to the LLM (up to 2 rounds) so it corrects its own mistakes. |
| **Estimate** | Step durations come from a regression model trained on run-log data (`backend/duration_model.py`), not a hardcoded table. |
| **Optimise** | A critical-path scheduler (`backend/scheduler.py`) computes the true minimum cycle time under parallel execution, vs. the naive sequential total. |
| **Analyse** | Cycle-time estimate, step-type breakdown, ECU coverage, and parallelisation headroom. |
| **Batch** | `/api/batch` runs the full pipeline over a fleet of specs and rolls results up into fleet-level bottleneck findings. |
| **Export** | `/api/export/otx` renders a program as an OTX-style (ISO 13209) XML procedure a real tester tool could load. |

The **validation** layer is deliberate: an LLM can produce a plausible program
that is subtly wrong. The system never trusts the generator blindly — it
verifies every program against the specification it came from, and gives the
model a bounded chance to fix what it got wrong before accepting the result.

---

## Architecture

```
                    ┌─────────────────────────────┐
   VehicleSpec ───► │  generator.py (orchestrator) │
   (structured      └──────────────┬──────────────┘
    JSON input)                    │
                     ┌─────────────┼──────────────┐
                     ▼             ▼              ▼
              llm_service.py   validator.py   analytics.py
              (mock | openai)  (rule checks)  (cycle time…)
                     │
                     ▼
              CommissioningProgram ──► validated + analysed ──► JSON API
                                                                   │
                                            frontend/ (static SPA) ┘
```

- **`backend/`** — FastAPI service. Thin routes; all logic in small, testable
  modules (`models`, `llm_service`, `generator`, `validator`, `analytics`,
  `scheduler`, `duration_model`, `batch`, `otx_export`).
- **`frontend/`** — dependency-free single-page UI (HTML/CSS/JS), served by the
  backend so the whole product is **one deployable container**.
- **`data/`** — realistic sample specs (a BEV `ID.4` and an ICE `Golf`), plus
  the trained `duration_model.json`.
- **`scripts/`** — `train_duration_model.py` (fits the duration model on a
  synthetic run-log stand-in) and `eval_harness.py` (measures LLM validity
  rate, with and without the self-repair loop, across randomised specs).
- **`tests/`** — pytest suite covering the generator, validator, scheduler,
  self-repair loop, batch mode, duration model, and OTX export.

The generator depends only on a small `LLMProvider` interface, so the model
backend is swapped through configuration — no code changes.

---

## Quick start

### Run locally (no API key needed)

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the app (defaults to offline mock mode)
uvicorn backend.main:app --reload
```

Open <http://localhost:8000>. Load a sample, press **Generate**, and you'll get
a full validated commissioning program — with zero configuration.

### Run with Docker

```bash
docker compose up --build
# open http://localhost:8000
```

### Use a real LLM

Copy `.env.example` to `.env` and set:

```env
LLM_PROVIDER=openai
LLM_API_KEY=sk-...                       # your key
LLM_BASE_URL=https://api.openai.com/v1   # or Together AI, Groq, Azure, Ollama…
LLM_MODEL=gpt-4o-mini
```

The API is OpenAI-compatible, so the same setting works with many providers.
If `openai` is selected but no key is present, the app safely falls back to
mock mode instead of crashing.

---

## API

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET`  | `/api/health` | Liveness + active provider. |
| `GET`  | `/api/samples` | List bundled sample specs. |
| `GET`  | `/api/samples/{file}` | Fetch one sample spec. |
| `POST` | `/api/generate` | Generate + self-repair + validate + optimise + analyse a program. |
| `POST` | `/api/batch` | Run the pipeline over a fleet of specs and return a rollup. |
| `POST` | `/api/export/otx` | Export a `CommissioningProgram` as OTX-style XML. |

Interactive API docs are available at `/docs` (Swagger UI, provided by FastAPI).

**Example:**

```bash
curl -s http://localhost:8000/api/samples/sample_id4_bev.json \
  | jq '{spec: .}' \
  | curl -s -X POST http://localhost:8000/api/generate \
      -H 'Content-Type: application/json' -d @- \
  | jq '{valid: .is_valid, steps: (.program.steps | length), cycle_time: .analytics.estimated_cycle_time_seconds}'
```

---

## Testing

```bash
pytest
```

The suite covers the generator (safe ordering, correct flashing, valid
end-to-end pipeline) and the validator (it *catches* unknown ECUs, unsupported
UDS services, and unsafe flashing order).

---

## Data model (summary)

**Input — `VehicleSpec`**
- `vehicle_id`, `model`, `model_year`
- `configuration`: option/feature codes
- `ecus[]`: `ecu_id`, `part_number`, `software_version`,
  `target_software_version`, `supported_uds_services`
- `process_standards[]`: rules the program must honour

**Output — `CommissioningProgram`**
- `steps[]`: `order`, `step_type`, `ecu_id`, `uds_service`,
  `estimated_seconds`, `depends_on`

UDS services referenced: `0x10` session control, `0x27` security access,
`0x34`/`0x36` request download / transfer data (flashing), `0x2E` write data,
`0x22` read data, `0x31` routine control, `0x14` clear diagnostic information.

---

## Evaluation

`scripts/eval_harness.py` generates randomised vehicle specs (seeded, so runs
are reproducible) and measures how often the pipeline produces a valid
program — for the mock planner, and for a real LLM with and without the
self-repair loop:

```bash
python scripts/eval_harness.py --n 20                 # mock only, offline
python scripts/eval_harness.py --n 5 --live            # also hits the real LLM (uses your configured key)
```

It writes a short report to `data/eval_report.md` with the validity rate
(first attempt vs. after repair) per provider, so "does self-repair actually
help" is an answered, reproducible question rather than an assumption.

---

## From UDS to SOVD

This prototype models diagnostics through classic **UDS** (Unified Diagnostic
Services, ISO 14229) — one physical/transport binding, one service ID space.
The JD's keyword list explicitly names **service-oriented vehicle
diagnostics**: the industry direction (SOVD, ASAM's newer specification) is to
expose the same diagnostic capabilities as a **RESTful, service-oriented API**
instead of a proprietary transport protocol, so any tool on the network can
address a vehicle the same way it addresses any other web service.

Nothing in this codebase is UDS-specific by accident of laziness — it's
UDS-specific because that's what the sample data models. The design already
separates *what* a step does (`StepType`, a domain concept) from *how* it's
addressed on the wire (`uds_service`, currently a UDS SID string). Extending
to SOVD would mean: (1) adding a `diagnostic_protocol` field to `Ecu`/`Step`
so a spec can declare UDS, SOVD, or both, (2) replacing the string SID with a
resource-style identifier (e.g. `/vehicles/{id}/components/{ecu}/data/{id}`),
and (3) teaching the validator's support-check to look up the right protocol's
capability list. The OTX-style export (`backend/otx_export.py`) is already
protocol-agnostic at the `diag-comm-action` level for exactly this reason.

---

## Deployment

The whole app ships as one Docker image (backend serves the static frontend),
so any container host works. Free option used for the live demo:

**Render** (Dockerfile-based web service, free tier):
1. Push this repo to GitHub (already done).
2. On [render.com](https://render.com), New → Web Service → connect the repo →
   Render detects the `Dockerfile` automatically.
3. Set environment variables: `LLM_PROVIDER=openrouter`, `LLM_API_KEY=...`,
   `LLM_MODEL=anthropic/claude-sonnet-5`.
4. Deploy. Free instances sleep after inactivity (first request after a while
   takes ~30s to wake up); everything after that is instant.

Other free/cheap options that work equally well since the app is just a
Dockerfile: **Fly.io** (`fly launch`, always-on free allowance) and
**Railway** (GitHub auto-deploy, trial-credit based). Vercel is not a good fit
here — it's built for serverless functions, not a persistent Python process.

---

## Roadmap

- Constraint-solver pass that also accounts for a *finite* number of physical
  tester channels (the current scheduler assumes unlimited parallelism).
- Retrain the duration model on real commissioning telemetry once available,
  replacing the synthetic stand-in dataset.
- Diff view: compare a generated program against a previously approved one.
- Richer process-standard DSL, checked automatically by the validator.
- SOVD support alongside UDS (see above).

---

## License

MIT — see [LICENSE](LICENSE).

Built by **Vishesh Srivastava** · [visheshsrivastava.com](https://visheshsrivastava.com)
