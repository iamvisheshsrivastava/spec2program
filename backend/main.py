"""FastAPI application entry point.

Exposes the REST API and also serves the static single-page frontend, so the
whole product ships as one deployable service. Routes are intentionally thin -
all real work lives in the ``generator`` pipeline and its helpers.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .batch import run_batch
from .config import settings
from .duration_model import model_info as duration_model_info
from .generator import generate
from .models import (
    BatchRequest,
    BatchResponse,
    ChannelScheduleRequest,
    ChannelScheduleResult,
    ChannelSweepRequest,
    ChannelSweepResult,
    CommissioningProgram,
    GenerateRequest,
    GenerateResponse,
    RecoveryRequest,
    RecoveryResponse,
    VehicleSpec,
)
from .otx_export import to_otx_xml
from .recovery import generate_recovery
from .scheduler import channel_sweep, schedule_with_channels

# Resolve project paths relative to this file so the app works regardless of
# the current working directory (important inside Docker and on PaaS hosts).
BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DATA_DIR = BASE_DIR / "data"

app = FastAPI(
    title="spec2program",
    description=(
        "AI-assisted generation of vehicle commissioning programs from "
        "structured specifications (BOM, configuration, UDS diagnostics, "
        "software versions, process standards)."
    ),
    version="0.1.0",
)

# Allow the frontend (and any configured origins) to call the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    """Liveness probe. Also reports the active LLM provider for debugging."""
    if settings.llm_provider in ("openai", "openrouter") and settings.llm_api_key:
        provider = settings.llm_provider
        model = settings.llm_model
    else:
        provider, model = "mock", None
    return {
        "status": "ok",
        "provider": provider,
        "model": model,
        "duration_model": duration_model_info(),
    }


@app.get("/api/samples")
def list_samples() -> list[dict]:
    """List the bundled sample specifications (for the UI dropdown)."""
    samples = []
    for path in sorted(DATA_DIR.glob("sample_*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        samples.append({
            "file": path.name,
            "vehicle_id": spec.get("vehicle_id"),
            "model": spec.get("model"),
            "model_year": spec.get("model_year"),
        })
    return samples


@app.get("/api/samples/{filename}")
def get_sample(filename: str) -> VehicleSpec:
    """Return one bundled sample spec by filename.

    The filename is sanitised to its basename to prevent path traversal.
    """
    safe_name = Path(filename).name
    path = DATA_DIR / safe_name
    if not path.exists() or not safe_name.startswith("sample_"):
        raise HTTPException(status_code=404, detail="Sample not found.")
    return VehicleSpec.model_validate_json(path.read_text(encoding="utf-8"))


@app.post("/api/generate", response_model=GenerateResponse)
def generate_program(request: GenerateRequest) -> GenerateResponse:
    """Generate, validate, and analyse a commissioning program for a spec."""
    try:
        return generate(request.spec)
    except (json.JSONDecodeError, ValueError) as exc:
        # The generator returned something that could not be parsed/validated.
        raise HTTPException(
            status_code=502,
            detail=f"The generator returned an invalid program: {exc}",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surface upstream errors cleanly
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/batch", response_model=BatchResponse)
def generate_batch(request: BatchRequest) -> BatchResponse:
    """Run the pipeline for every spec in the batch and return a fleet rollup."""
    if not request.specs:
        raise HTTPException(status_code=400, detail="Batch request contained no specs.")
    try:
        return run_batch(request.specs)
    except Exception as exc:  # noqa: BLE001 - surface upstream errors cleanly
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/optimize/channels", response_model=ChannelScheduleResult)
def optimize_channels(request: ChannelScheduleRequest) -> ChannelScheduleResult:
    """Schedule an already-generated program under a finite tester-channel count."""
    return schedule_with_channels(request.program, request.channels)


@app.post("/api/optimize/channel-sweep", response_model=ChannelSweepResult)
def optimize_channel_sweep(request: ChannelSweepRequest) -> ChannelSweepResult:
    """Cycle time for channel counts 1..max_channels, to plot diminishing returns."""
    return channel_sweep(request.program, request.max_channels)


@app.post("/api/recover", response_model=RecoveryResponse)
def recover(request: RecoveryRequest) -> RecoveryResponse:
    """Generate a corrective sub-program for a step that failed at runtime."""
    try:
        return generate_recovery(request)
    except Exception as exc:  # noqa: BLE001 - surface upstream errors cleanly
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/export/otx")
def export_otx(program: CommissioningProgram) -> Response:
    """Export a generated program as an OTX-style XML procedure document."""
    xml = to_otx_xml(program)
    return Response(
        content=xml,
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{program.vehicle_id}_otx.xml"'
        },
    )


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
# Serve index.html at the root, and everything else under /static.
@app.get("/")
def index() -> FileResponse:
    """Serve the single-page frontend."""
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/about")
def about() -> FileResponse:
    """Serve the About / How it works page."""
    return FileResponse(FRONTEND_DIR / "about.html")


# Mount the rest of the frontend assets (styles.css, app.js) as static files.
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
