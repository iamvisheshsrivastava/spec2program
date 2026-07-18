# spec2program

**AI-assisted generation of vehicle commissioning programs from structured specifications.**

`spec2program` takes the heterogeneous data sources that drive end-of-line
vehicle commissioning — the bill of materials (ECUs), the vehicle
configuration, the supported UDS diagnostic services, software/flash versions,
and process standards — and generates a correct, ordered **commissioning
program**: open a diagnostic session, unlock security access, flash software,
write configuration parameters, validate, and clear fault codes.

Every generated program is then **validated against its specification** and
**analysed** for optimisation potential (cycle time, program structure,
parallelisable steps). The generator is pluggable: it runs fully offline with a
deterministic rule-based planner, or against any OpenAI-compatible LLM.

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
| **Analyse** | Cycle-time estimate, step-type breakdown, ECU coverage, and parallelisation headroom. |

The **validation** layer is deliberate: an LLM can produce a plausible program
that is subtly wrong. The system never trusts the generator blindly — it
verifies every program against the specification it came from.

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
  modules (`models`, `llm_service`, `generator`, `validator`, `analytics`).
- **`frontend/`** — dependency-free single-page UI (HTML/CSS/JS), served by the
  backend so the whole product is **one deployable container**.
- **`data/`** — realistic sample specs (a BEV `ID.4` and an ICE `Golf`).
- **`tests/`** — pytest suite for the generator and validator.

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
| `POST` | `/api/generate` | Generate + validate + analyse a program. |

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

## Roadmap

- Constraint-solver pass to compute the true minimum-cycle-time ordering given
  the dependency graph and parallel tester channels.
- Learn per-step time budgets from historical commissioning logs instead of
  fixed estimates.
- Diff view: compare a generated program against a previously approved one.
- Richer process-standard DSL, checked automatically by the validator.

---

## License

MIT — see [LICENSE](LICENSE).

Built by **Vishesh Srivastava** · [visheshsrivastava.com](https://visheshsrivastava.com)
