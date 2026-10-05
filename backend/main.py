"""FastAPI application entry point.

Exposes the REST API and also serves the static single-page frontend, so the
whole product ships as one deployable service. Routes are intentionally thin -
all real work lives in the ``generator`` pipeline and its helpers.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import db
from .batch import run_batch
from .config import settings
from .diff import diff_programs
from .duration_model import model_info as duration_model_info
from .export_csv import to_csv
from .generator import generate
from .llm_service import close_http_client
from .models import (
    BatchRequest,
    BatchResponse,
    ChannelScheduleRequest,
    ChannelScheduleResult,
    ChannelSweepRequest,
    ChannelSweepResult,
    CompareRequest,
    GenerateRequest,
    GenerateResponse,
    HistoryItemDetail,
    OtxExportRequest,
    RecoveryRequest,
    RecoveryResponse,
    VehicleSpec,
)
from .otx_export import to_otx_xml
from .recovery import generate_recovery
from .scheduler import channel_sweep, schedule_with_channels
from .validator import is_valid, validate_program

# Per-IP rate limiting on the endpoints that spend LLM credits (issue #12).
# The public demo has no auth, so this is the only backstop against an
# anonymous caller draining a real API key with a burst of requests.
limiter = Limiter(key_func=get_remote_address)

# Resolve project paths relative to this file so the app works regardless of
# the current working directory (important inside Docker and on PaaS hosts).
logger = logging.getLogger(__name__)


def _internal_error(exc: Exception) -> HTTPException:
    """Log the real error server-side; return a generic message + correlation id."""
    error_id = uuid.uuid4().hex[:12]
    logger.error("Unhandled error %s: %r", error_id, exc, exc_info=exc)
    return HTTPException(
        status_code=500,
        detail=f"Internal error (reference {error_id}). See server logs.",
    )


_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename_component(value: str, *, fallback: str = "export") -> str:
    """Sanitise a user-controlled value for use inside a filename.

    ``value`` (e.g. a ``vehicle_id``) ultimately lands inside a
    ``Content-Disposition`` header, so it must never be able to inject CR/LF
    (header/response splitting) or break out of the quoted-string via an
    embedded ``"`` or ``\\``. Rather than trying to escape those characters,
    strip everything that is not a conservative, known-safe character
    (ASCII letters/digits, dot, dash, underscore) - this also sidesteps any
    header-encoding concerns for non-ASCII input.
    """
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", value).strip("._")
    return cleaned[:100] or fallback


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DATA_DIR = BASE_DIR / "data"

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: release pooled LLM connections on shutdown."""
    yield
    close_http_client()


app = FastAPI(
    lifespan=lifespan,
    title="spec2program",
    description=(
        "AI-assisted generation of vehicle commissioning programs from "
        "structured specifications (BOM, configuration, UDS diagnostics, "
        "software versions, process standards)."
    ),
    version="0.1.0",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

# Allow the frontend (and any configured origins) to call the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,  # no cookies/auth are used; '*' + credentials is invalid
    allow_methods=["*"],
    allow_headers=["*"],
)


def _persist_history(
    *, kind: str, vehicle_id: str | None, provider: str | None,
    is_valid_: bool | None, request_model, response_model,
) -> None:
    """Best-effort audit write (issue #16). Never let a DB problem fail the API call."""
    try:
        db.record_history(
            kind=kind,
            vehicle_id=vehicle_id,
            provider=provider,
            is_valid=is_valid_,
            request_json=request_model.model_dump_json(),
            response_json=response_model.model_dump_json(),
        )
    except Exception as exc:  # noqa: BLE001 - persistence must never break the response
        logger.warning("Failed to persist history row (kind=%s): %s", kind, exc)


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
@limiter.limit(lambda: settings.rate_limit_llm_endpoints)
def generate_program(request: Request, payload: GenerateRequest) -> GenerateResponse:
    """Generate, validate, and analyse a commissioning program for a spec.

    The parameter is named ``request`` (the Starlette request, required by
    @limiter.limit for per-IP rate limiting - issue #12) with the request
    *body* as ``payload``; FastAPI tells them apart by type, same as any
    other dependency.
    """
    try:
        result = generate(payload.spec)
    except (json.JSONDecodeError, ValueError) as exc:
        # The generator returned something that could not be parsed/validated.
        raise HTTPException(
            status_code=502,
            detail=f"The generator returned an invalid program: {exc}",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surface upstream errors cleanly
        raise _internal_error(exc) from exc
    _persist_history(
        kind="generate", vehicle_id=payload.spec.vehicle_id, provider=result.provider,
        is_valid_=result.is_valid, request_model=payload, response_model=result,
    )
    return result


@app.post("/api/batch", response_model=BatchResponse)
@limiter.limit(lambda: settings.rate_limit_llm_endpoints)
def generate_batch(request: Request, payload: BatchRequest) -> BatchResponse:
    """Run the pipeline for every spec in the batch and return a fleet rollup."""
    if not payload.specs:
        raise HTTPException(status_code=400, detail="Batch request contained no specs.")
    try:
        result = run_batch(payload.specs)
    except Exception as exc:  # noqa: BLE001 - surface upstream errors cleanly
        raise _internal_error(exc) from exc
    for spec, item in zip(payload.specs, result.results):
        _persist_history(
            kind="batch_item", vehicle_id=spec.vehicle_id, provider=item.provider,
            is_valid_=item.is_valid, request_model=spec, response_model=item,
        )
    return result


@app.post("/api/optimize/channels", response_model=ChannelScheduleResult)
def optimize_channels(request: ChannelScheduleRequest) -> ChannelScheduleResult:
    """Schedule an already-generated program under a finite tester-channel count."""
    try:
        return schedule_with_channels(request.program, request.channels)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/optimize/channel-sweep", response_model=ChannelSweepResult)
def optimize_channel_sweep(request: ChannelSweepRequest) -> ChannelSweepResult:
    """Cycle time for channel counts 1..max_channels, to plot diminishing returns."""
    try:
        return channel_sweep(request.program, request.max_channels)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/recover", response_model=RecoveryResponse)
@limiter.limit(lambda: settings.rate_limit_llm_endpoints)
def recover(request: Request, payload: RecoveryRequest) -> RecoveryResponse:
    """Generate a corrective sub-program for a step that failed at runtime."""
    try:
        result = generate_recovery(payload)
    except ValueError as exc:
        # generate_recovery() raises ValueError for bad client input (e.g. a
        # failed_step_order that does not exist in the given program) - a 400,
        # not a server fault. Matches how /api/optimize/channels and
        # /api/optimize/channel-sweep report their own ValueErrors.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface upstream errors cleanly
        raise _internal_error(exc) from exc
    _persist_history(
        kind="recover", vehicle_id=payload.spec.vehicle_id, provider=result.provider,
        is_valid_=result.is_valid, request_model=payload, response_model=result,
    )
    return result


@app.post("/api/export/otx")
def export_otx(request: OtxExportRequest) -> Response:
    """Export a generated program as an OTX-style XML procedure document.

    The request must include the ``VehicleSpec`` the program claims to be
    for, and the program is re-validated against it before export - a bare,
    client-fabricated ``CommissioningProgram`` that never went through
    ``validate_program`` (e.g. referencing nonexistent ECUs or skipping
    security access) is rejected with a 400 rather than exported as if it
    were a real, certified pipeline output.
    """
    spec, program = request.spec, request.program
    issues = validate_program(spec, program)
    if not is_valid(issues):
        messages = "; ".join(issue.message for issue in issues if issue.severity == "error")
        raise HTTPException(
            status_code=400,
            detail=f"Program failed validation against the given spec and cannot be exported: {messages}",
        )
    try:
        xml = to_otx_xml(program)
    except Exception as exc:  # noqa: BLE001 - surface export failures as a clean 400
        raise HTTPException(
            status_code=400,
            detail=f"Could not export program to OTX: {exc}",
        ) from exc
    safe_vehicle_id = _safe_filename_component(program.vehicle_id)
    return Response(
        content=xml,
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_vehicle_id}_otx.xml"'
        },
    )


@app.post("/api/export/csv")
def export_csv(request: OtxExportRequest) -> Response:
    """Export a generated program as a human-readable work-instruction CSV (issue #19).

    Same validation gate as ``/api/export/otx``: the program must pass
    ``validate_program`` against the given spec before it can be exported,
    so a client-fabricated program can't be handed out as if it were a real
    pipeline result.
    """
    spec, program = request.spec, request.program
    issues = validate_program(spec, program)
    if not is_valid(issues):
        messages = "; ".join(issue.message for issue in issues if issue.severity == "error")
        raise HTTPException(
            status_code=400,
            detail=f"Program failed validation against the given spec and cannot be exported: {messages}",
        )
    csv_text = to_csv(program)
    safe_vehicle_id = _safe_filename_component(program.vehicle_id)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_vehicle_id}_work_instructions.csv"'
        },
    )


@app.post("/api/compare")
def compare_programs(request: CompareRequest) -> dict:
    """Diff two commissioning programs by step order (issue #20).

    Unlike the export routes, comparison does not require either program to
    validate against a spec - a diff is useful precisely when one of the two
    is broken (e.g. comparing a first LLM attempt against the self-repaired
    result), and no spec is even supplied here.
    """
    return diff_programs(request.program_a, request.program_b)


@app.get("/api/history")
def get_history(
    kind: str | None = None, vehicle_id: str | None = None, limit: int = 50, offset: int = 0,
) -> list[dict]:
    """List audit-history rows (issue #16), newest first.

    ``kind`` filters to 'generate', 'batch_item', or 'recover'. Returns the
    summary fields only; fetch a single id via ``/api/history/{id}`` for the
    full stored request/response payload.
    """
    if kind is not None and kind not in ("generate", "batch_item", "recover"):
        raise HTTPException(status_code=400, detail="kind must be one of 'generate', 'batch_item', 'recover'.")
    try:
        return db.list_history(kind=kind, vehicle_id=vehicle_id, limit=limit, offset=offset)
    except Exception as exc:  # noqa: BLE001
        raise _internal_error(exc) from exc


@app.get("/api/history/{item_id}", response_model=HistoryItemDetail)
def get_history_item(item_id: int) -> HistoryItemDetail:
    """Return one audit-history row, including its full request/response payload."""
    try:
        record = db.get_history_item(item_id)
    except Exception as exc:  # noqa: BLE001
        raise _internal_error(exc) from exc
    if record is None:
        raise HTTPException(status_code=404, detail=f"No history item with id {item_id}.")
    return HistoryItemDetail.model_validate(record)


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
