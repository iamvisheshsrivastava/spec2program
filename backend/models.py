"""Domain models for spec2program.

These pydantic models define the two central data structures of the project:

1. ``VehicleSpec`` - the structured *input*. It bundles the heterogeneous data
   sources that, in a real plant, feed the commissioning process: the bill of
   materials (ECUs / components), the vehicle configuration, the diagnostic
   services (UDS) each ECU supports, software/flash versions, and the process
   standards that must be honoured.

2. ``CommissioningProgram`` - the structured *output*. A commissioning program
   is an ordered sequence of steps (open a diagnostic session, flash software,
   write parameters, run a validation routine, ...) that brings the vehicle's
   electronics into a defined, tested state.

Keeping both sides strongly typed is what makes the rest of the pipeline
possible: the LLM is asked to emit JSON matching ``CommissioningProgram``, and
the validator then checks that output against the ``VehicleSpec`` it came from.
"""

from __future__ import annotations

from enum import Enum
from typing import Union

from pydantic import BaseModel, ConfigDict, Field

# A single configuration value. Real vehicle-configuration data is not
# uniformly stringly-typed: booleans (e.g. left_hand_drive), numbers (e.g.
# battery capacity), and strings (e.g. drivetrain) all show up naturally.
# Restricting this to `str` rejects perfectly valid real-world specs with a
# confusing type error, so we accept the common JSON scalar types instead.
ConfigValue = Union[str, bool, int, float]


# ---------------------------------------------------------------------------
# INPUT SIDE - the vehicle specification
# ---------------------------------------------------------------------------
class Ecu(BaseModel):
    """A single Electronic Control Unit in the vehicle (a line in the BOM)."""

    # Real BOM exports carry extra columns (e.g. flash_required, coding_required
    # flags a PLM tool adds); accept and ignore anything we don't model.
    model_config = ConfigDict(extra="ignore")

    ecu_id: str = Field(..., description="Stable identifier, e.g. 'BCM'.")
    name: str = Field(..., description="Human-readable name.")
    part_number: str = Field(..., description="Part number from the bill of materials.")
    supplier: str | None = Field(None, description="Component supplier.")
    software_version: str | None = Field(
        None, description="Currently installed software / flash version."
    )
    target_software_version: str | None = Field(
        None, description="Software version the vehicle should end up with."
    )
    # UDS = Unified Diagnostic Services (ISO 14229). We reference services by
    # their common request SIDs, e.g. '0x10' (DiagnosticSessionControl),
    # '0x27' (SecurityAccess), '0x2E' (WriteDataByIdentifier), '0x34'/'0x36'
    # (RequestDownload / TransferData) used for flashing.
    supported_uds_services: list[str] = Field(
        default_factory=list,
        description="UDS service IDs this ECU supports, e.g. ['0x10','0x27'].",
    )


class VehicleSpec(BaseModel):
    """The complete, structured description of one vehicle to commission."""

    # Extra fields (e.g. "plant", "vin", or any other metadata a real BOM/PLM
    # export includes) are accepted and ignored rather than rejected. This
    # schema only enforces the fields the pipeline actually depends on.
    model_config = ConfigDict(extra="ignore")

    vehicle_id: str = Field(..., description="Order / VIN-like identifier.")
    model: str = Field(..., description="Vehicle model, e.g. 'ID.4'.")
    model_year: int = Field(..., description="Model year.")

    # Vehicle configuration: the option codes / features that determine which
    # ECUs are present and how they must be parameterised. Values may be
    # strings, booleans, or numbers - see ConfigValue above.
    configuration: dict[str, ConfigValue] = Field(
        default_factory=dict,
        description="Option/feature codes, e.g. {'drivetrain':'BEV','left_hand_drive':true}.",
    )

    # Bill of materials expressed as the list of ECUs to be commissioned.
    ecus: list[Ecu] = Field(default_factory=list, description="ECUs / components.")

    # Process standards the generated program must respect (safety gates,
    # ordering rules, mandatory validation steps, ...).
    process_standards: list[str] = Field(
        default_factory=list,
        description="Named process rules the program must honour.",
    )


# ---------------------------------------------------------------------------
# OUTPUT SIDE - the commissioning program
# ---------------------------------------------------------------------------
class StepType(str, Enum):
    """The categories of commissioning step this system can plan."""

    DIAGNOSTIC_SESSION = "diagnostic_session"  # open a UDS session on an ECU
    SECURITY_ACCESS = "security_access"        # unlock the ECU (UDS 0x27)
    FLASH_SOFTWARE = "flash_software"          # download new software (0x34/0x36)
    WRITE_PARAMETER = "write_parameter"        # configure the ECU (0x2E)
    VALIDATION = "validation"                  # read back / self-test (0x22/0x31)
    FAULT_CLEAR = "fault_clear"                # clear diagnostic trouble codes (0x14)


class CommissioningStep(BaseModel):
    """One ordered action in a commissioning program."""

    order: int = Field(..., description="1-based position of the step.")
    step_type: StepType = Field(..., description="What kind of action this is.")
    ecu_id: str = Field(..., description="Which ECU this step targets.")
    description: str = Field(..., description="Human-readable summary of the step.")
    uds_service: str | None = Field(
        None, description="UDS service ID used, if applicable, e.g. '0x2E'."
    )
    estimated_seconds: float = Field(
        0.0, description="Estimated cycle time contribution of this step."
    )
    depends_on: list[int] = Field(
        default_factory=list,
        description="Orders of steps that must complete before this one.",
    )


class CommissioningProgram(BaseModel):
    """The full generated program for a single vehicle."""

    vehicle_id: str = Field(..., description="Vehicle this program is for.")
    steps: list[CommissioningStep] = Field(
        default_factory=list, description="Ordered commissioning steps."
    )
    notes: str | None = Field(
        None, description="Optional free-text notes / rationale from the generator."
    )


# ---------------------------------------------------------------------------
# API request / response envelopes
# ---------------------------------------------------------------------------
class GenerateRequest(BaseModel):
    """Request body for POST /api/generate."""

    spec: VehicleSpec


class ValidationIssue(BaseModel):
    """A single problem found while checking a program against its spec."""

    severity: str = Field(..., description="'error' or 'warning'.")
    message: str = Field(..., description="What is wrong.")
    step_order: int | None = Field(None, description="Related step, if any.")


class ProgramAnalytics(BaseModel):
    """Quantitative summary used to surface optimisation potential."""

    total_steps: int
    estimated_cycle_time_seconds: float
    steps_by_type: dict[str, int]
    ecus_covered: int
    ecus_total: int
    parallelisable_steps: int = Field(
        0, description="Steps with no dependencies that could run concurrently."
    )


class ScheduledStep(BaseModel):
    """A step placed on the earliest-start-time critical-path schedule."""

    order: int
    start: float = Field(..., description="Earliest possible start time (seconds).")
    end: float = Field(..., description="Earliest possible finish time (seconds).")


class OptimizationResult(BaseModel):
    """Critical-path scheduling analysis: how much cycle time could be saved.

    ``sequential_seconds`` is the naive total (every step run back-to-back, the
    number already reported in ``ProgramAnalytics``). ``critical_path_seconds``
    is the minimum possible cycle time if independent steps ran in parallel on
    multi-channel tester hardware, honouring every declared dependency. The gap
    between the two is the concrete optimisation lever this project targets.
    """

    sequential_seconds: float
    critical_path_seconds: float
    speedup_factor: float = Field(
        ..., description="sequential_seconds / critical_path_seconds."
    )
    critical_path_steps: list[int] = Field(
        default_factory=list,
        description="Orders of the steps that sit on the longest dependency chain.",
    )
    schedule: list[ScheduledStep] = Field(
        default_factory=list,
        description="Earliest-start-time schedule for every step under unlimited parallelism.",
    )


class ChannelScheduledStep(BaseModel):
    """A step placed on a channel-constrained schedule (finite tester channels)."""

    order: int
    channel: int = Field(..., description="0-based tester channel this step runs on.")
    start: float
    end: float


class ChannelScheduleResult(BaseModel):
    """Result of scheduling a program under a finite number of tester channels."""

    channels: int = Field(..., description="Number of parallel tester channels assumed.")
    cycle_time_seconds: float
    schedule: list[ChannelScheduledStep] = Field(default_factory=list)


class ChannelSweepPoint(BaseModel):
    """One point on the cycle-time-vs-channel-count curve."""

    channels: int
    cycle_time_seconds: float


class ChannelSweepResult(BaseModel):
    """Cycle time for channel counts 1..max, to show diminishing returns."""

    points: list[ChannelSweepPoint] = Field(default_factory=list)


class ChannelScheduleRequest(BaseModel):
    """Request body for POST /api/optimize/channels."""

    program: CommissioningProgram
    channels: int = Field(..., ge=1, description="Number of parallel tester channels.")


class ChannelSweepRequest(BaseModel):
    """Request body for POST /api/optimize/channel-sweep."""

    program: CommissioningProgram
    max_channels: int = Field(8, ge=1, le=32, description="Sweep channel counts from 1 to this.")


class RecoveryRequest(BaseModel):
    """Request body for POST /api/recover.

    Represents a runtime failure: step ``failed_step_order`` of ``program``
    (generated for ``spec``) failed on the line, for the reason described in
    free text by ``failure_reason`` (e.g. "communication timeout",
    "security access denied", "flash verification failed").
    """

    spec: VehicleSpec
    program: CommissioningProgram
    failed_step_order: int
    failure_reason: str


class RecoveryResponse(BaseModel):
    """Response body for POST /api/recover: a validated corrective sub-program."""

    recovery_steps: list[CommissioningStep]
    notes: str | None = None
    provider: str
    is_valid: bool
    validation: list[ValidationIssue] = Field(default_factory=list)


class GenerateResponse(BaseModel):
    """Response body for POST /api/generate."""

    program: CommissioningProgram
    validation: list[ValidationIssue]
    analytics: ProgramAnalytics
    optimization: OptimizationResult
    is_valid: bool = Field(..., description="True if no 'error'-severity issues.")
    provider: str = Field(..., description="Which LLM provider produced the program.")
    repair_attempts: int = Field(
        0, description="How many self-repair rounds the LLM needed to reach this result."
    )


# ---------------------------------------------------------------------------
# Batch mode - run several specs at once and aggregate optimisation findings
# ---------------------------------------------------------------------------
class BatchRequest(BaseModel):
    """Request body for POST /api/batch."""

    specs: list[VehicleSpec] = Field(..., description="One or more vehicle specs.")


class BatchAggregate(BaseModel):
    """Fleet-level rollup across every spec in a batch run."""

    vehicles: int
    valid_count: int
    validity_rate: float
    avg_cycle_time_seconds: float
    avg_critical_path_seconds: float
    avg_speedup_factor: float
    bottleneck_ecus: list[str] = Field(
        default_factory=list,
        description="ECU ids most frequently flagged with the highest per-vehicle time share.",
    )
    most_common_issue: str | None = Field(
        None, description="The most frequently recurring validation message across the fleet."
    )


class BatchResponse(BaseModel):
    """Response body for POST /api/batch."""

    results: list[GenerateResponse]
    aggregate: BatchAggregate
