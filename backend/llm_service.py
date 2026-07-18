"""Pluggable LLM service.

The generation pipeline depends only on the small ``LLMProvider`` interface,
so the concrete backend can be swapped through configuration without touching
any other code. Two providers ship with the project:

* ``MockProvider``   - a deterministic, offline planner. It builds a sensible
  commissioning program directly from the spec using domain rules. This lets
  the whole application run and be demoed with **no API key**, and it also acts
  as a reference/fallback so the product never hard-fails.

* ``OpenAIProvider`` - calls any OpenAI-compatible Chat Completions endpoint
  (OpenAI, Azure OpenAI gateways, Together AI, Groq, Ollama, ...). It asks the
  model to return strict JSON matching the ``CommissioningProgram`` schema.

* ``OpenRouterProvider`` - calls OpenRouter.ai, a router that exposes many
  underlying model providers (Anthropic, OpenAI, Google, ...) behind one
  OpenAI-compatible API. Defaults to a strong, current model
  (``anthropic/claude-sonnet-5``) for high-quality structured output.

Both real providers return a raw ``dict`` (parsed JSON). Turning that dict
into a validated ``CommissioningProgram`` happens in ``generator.py``, which
also applies a resilient fallback to the mock provider if a real call fails,
so the product never hard-fails in front of a reviewer.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

import httpx

from .config import settings
from .models import CommissioningProgram, CommissioningStep, ValidationIssue, VehicleSpec


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------
class LLMProvider(Protocol):
    """Minimal interface every provider must implement."""

    name: str

    def generate_program(self, spec: VehicleSpec) -> dict:
        """Return a raw dict shaped like a ``CommissioningProgram``."""
        ...

    def repair_program(
        self, spec: VehicleSpec, previous_program: dict, issues: list[ValidationIssue]
    ) -> dict:
        """Return a corrected program dict, given the failed attempt and why it failed.

        Optional: only real (LLM-backed) providers implement this. The
        generation pipeline checks with ``hasattr`` before calling it, so the
        mock provider (which never needs a self-repair round) can simply omit
        this method.
        """
        ...


# The instruction we give any real LLM. Kept here so it is versioned with code.
SYSTEM_PROMPT = """\
You are an expert manufacturing engineer specialising in vehicle commissioning
(end-of-line electronics commissioning). Given a structured vehicle
specification, you produce a correct, safe, ordered commissioning program.

Rules you MUST follow:
- Only reference ECUs that appear in the specification's ECU list.
- Only use a UDS service on an ECU if that ECU lists it as supported.
- A security-access (0x27) step must come before any flash (0x34/0x36) or
  write-parameter (0x2E) step on the same ECU.
- Every ECU that needs a software update (target_software_version differs from
  software_version) must be flashed, then validated.
- Honour every rule listed in the specification's process_standards.
- End the program by clearing diagnostic trouble codes and running a final
  validation.

Return ONLY valid JSON, no prose, matching exactly this schema:
{
  "vehicle_id": "<string>",
  "steps": [
    {
      "order": <int, 1-based>,
      "step_type": "diagnostic_session|security_access|flash_software|write_parameter|validation|fault_clear",
      "ecu_id": "<must be an ECU id from the spec>",
      "description": "<short human-readable summary>",
      "uds_service": "<e.g. 0x2E or null>",
      "estimated_seconds": <number>,
      "depends_on": [<orders of prerequisite steps>]
    }
  ],
  "notes": "<optional rationale>"
}
"""


# ---------------------------------------------------------------------------
# Mock provider - deterministic, offline, no API key required
# ---------------------------------------------------------------------------
class MockProvider:
    """Rule-based planner that mimics what a good LLM would produce.

    It encodes the same domain rules stated in ``SYSTEM_PROMPT`` so the demo is
    realistic and always schema-valid. Being deterministic also makes it
    perfect for unit tests.
    """

    name = "mock"

    # Rough per-action time budgets (seconds). Centralised so they are easy to
    # tune and so analytics and the planner agree on the numbers.
    TIME_BUDGET = {
        "diagnostic_session": 3.0,
        "security_access": 4.0,
        "flash_software": 45.0,
        "write_parameter": 6.0,
        "validation": 8.0,
        "fault_clear": 5.0,
    }

    def generate_program(self, spec: VehicleSpec) -> dict:
        steps: list[dict] = []
        order = 0

        def add(step_type: str, ecu_id: str, description: str,
                uds: str | None, depends_on: list[int]) -> int:
            """Append a step, auto-numbering it, and return its order."""
            nonlocal order
            order += 1
            steps.append({
                "order": order,
                "step_type": step_type,
                "ecu_id": ecu_id,
                "description": description,
                "uds_service": uds,
                "estimated_seconds": self.TIME_BUDGET[step_type],
                "depends_on": depends_on,
            })
            return order

        # Plan each ECU in turn. Ordering within an ECU respects the safety
        # rule: session -> security access -> (flash) -> (write) -> validation.
        for ecu in spec.ecus:
            supports = set(ecu.supported_uds_services)

            # 1) Open a diagnostic session (UDS 0x10) if the ECU supports it.
            session_order = None
            if "0x10" in supports:
                session_order = add(
                    "diagnostic_session", ecu.ecu_id,
                    f"Open extended diagnostic session on {ecu.name}.",
                    "0x10", [],
                )

            prereq = [session_order] if session_order else []

            # 2) Security access (UDS 0x27) - required before flashing/writing.
            needs_flash = (
                ecu.target_software_version is not None
                and ecu.target_software_version != ecu.software_version
            )
            sec_order = None
            if "0x27" in supports and (needs_flash or "0x2E" in supports):
                sec_order = add(
                    "security_access", ecu.ecu_id,
                    f"Unlock {ecu.name} via security access.",
                    "0x27", prereq,
                )

            flash_prereq = [o for o in [sec_order or session_order] if o]

            # 3) Flash software if a newer target version is specified. Use
            #    whichever of 0x34 (RequestDownload) / 0x36 (TransferData)
            #    the ECU actually advertises - some ECUs only list one.
            last_write_order = None
            if needs_flash and {"0x34", "0x36"} & supports:
                flash_uds = "0x34" if "0x34" in supports else "0x36"
                flash_order = add(
                    "flash_software", ecu.ecu_id,
                    (f"Flash {ecu.name} "
                     f"{ecu.software_version or '?'} -> {ecu.target_software_version}."),
                    flash_uds, flash_prereq,
                )
                last_write_order = flash_order

            # 4) Write configuration parameters (UDS 0x2E).
            if "0x2E" in supports:
                write_prereq = [o for o in [last_write_order or sec_order or session_order] if o]
                last_write_order = add(
                    "write_parameter", ecu.ecu_id,
                    f"Write vehicle configuration parameters to {ecu.name}.",
                    "0x2E", write_prereq,
                )

            # 5) Validate the ECU (read data / routine). Prefer 0x22 (read
            #    data by identifier), fall back to 0x31 (routine control), or
            #    omit the service id entirely if the ECU supports neither -
            #    never assert a service the spec doesn't list as supported.
            if last_write_order or session_order:
                val_prereq = [o for o in [last_write_order, session_order] if o]
                if "0x22" in supports:
                    val_uds = "0x22"
                elif "0x31" in supports:
                    val_uds = "0x31"
                else:
                    val_uds = None
                add(
                    "validation", ecu.ecu_id,
                    f"Validate {ecu.name}: read back configuration and self-test.",
                    val_uds, val_prereq,
                )

        # Global closing steps across the whole vehicle: clear fault memory and
        # run one final validation. These depend on everything before them.
        # We pick ECUs/services that are actually supported so the closing
        # steps never violate the "UDS must be supported" rule.
        all_orders = [s["order"] for s in steps]
        if steps:
            # Choose an ECU that supports fault-clear (0x14); fall back to the
            # first ECU if none advertise it.
            clear_ecu = next(
                (e for e in spec.ecus if "0x14" in e.supported_uds_services),
                spec.ecus[0],
            )
            clear_uds = "0x14" if "0x14" in clear_ecu.supported_uds_services else None
            clear_order = add(
                "fault_clear", clear_ecu.ecu_id,
                "Clear diagnostic trouble codes across all ECUs.",
                clear_uds, all_orders,
            )

            # Choose an ECU/service for the final validation. Prefer routine
            # control (0x31), then read-data (0x22), else no explicit service.
            val_ecu = next(
                (e for e in spec.ecus if "0x31" in e.supported_uds_services), None
            )
            val_uds = "0x31"
            if val_ecu is None:
                val_ecu = next(
                    (e for e in spec.ecus if "0x22" in e.supported_uds_services),
                    spec.ecus[0],
                )
                val_uds = "0x22" if "0x22" in val_ecu.supported_uds_services else None
            add(
                "validation", val_ecu.ecu_id,
                "Final vehicle-level validation of the electronics network.",
                val_uds, [clear_order],
            )

        return {
            "vehicle_id": spec.vehicle_id,
            "steps": steps,
            "notes": ("Generated offline by the deterministic rule-based planner "
                      "(mock provider). Set LLM_PROVIDER=openai to use a real model."),
        }


# ---------------------------------------------------------------------------
# Shared helpers for any OpenAI-compatible Chat Completions endpoint
# ---------------------------------------------------------------------------
def _extract_json(content: str) -> dict:
    """Parse a model's JSON answer, tolerating minor formatting noise.

    Even when asked for strict JSON, some models wrap the answer in a
    ```json ... ``` markdown fence or add a short preamble. Rather than
    failing the whole generation on that cosmetic issue, we first try a
    direct parse, then fall back to extracting the outermost {...} block.
    """
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Strip a markdown code fence if present.
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))

    # Fall back to the first '{' .. last '}' span in the text.
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(content[start : end + 1])

    raise json.JSONDecodeError("No JSON object found in model output.", content, 0)


def _call_chat_completions(
    *, base_url: str, api_key: str, model: str, extra_headers: dict | None,
    spec: VehicleSpec,
) -> dict:
    """POST a Chat Completions request and return the parsed JSON program.

    Shared by every OpenAI-compatible provider (OpenAI itself, OpenRouter,
    Azure gateways, Together AI, Groq, Ollama, ...) so each provider class
    only needs to supply its endpoint, credentials, and any extra headers.
    """
    user_content = (
        "Vehicle specification (JSON):\n"
        + spec.model_dump_json(indent=2)
        + "\n\nProduce the commissioning program as JSON now."
    )
    payload = {
        "model": model,
        "temperature": settings.llm_temperature,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        # Ask providers/models that support it to return strict JSON.
        # If the specific model ignores this, _extract_json() below still
        # recovers the JSON from a prose-wrapped answer.
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    if extra_headers:
        headers.update(extra_headers)

    # Synchronous call - simple and robust. FastAPI runs this in a
    # threadpool because the route handler is declared `def`, not `async`.
    with httpx.Client(timeout=settings.llm_timeout) as client:
        resp = client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    content = data["choices"][0]["message"]["content"]
    return _extract_json(content)


def _call_repair(
    *, base_url: str, api_key: str, model: str, extra_headers: dict | None,
    spec: VehicleSpec, previous_program: dict, issues: list[ValidationIssue],
) -> dict:
    """POST a follow-up request asking the model to fix its own failed output.

    This is the self-repair loop: rather than discarding a program the moment
    the rule-based validator finds a problem, we show the model exactly what
    it got wrong (in the validator's own words) and its own prior answer, and
    ask it to correct just that. This mirrors how "AI-based code analysis /
    AI-supported software development" is meant to work - detect, explain,
    fix - rather than a single unchecked generation pass.
    """
    issues_text = "\n".join(
        f"- [{issue.severity}] {issue.message}"
        + (f" (step {issue.step_order})" if issue.step_order is not None else "")
        for issue in issues
    )
    user_content = (
        "The commissioning program below, which you previously generated for "
        "this vehicle specification, failed rule-based validation.\n\n"
        f"Vehicle specification (JSON):\n{spec.model_dump_json(indent=2)}\n\n"
        f"Your previous program (JSON):\n{json.dumps(previous_program, indent=2)}\n\n"
        f"Validation issues found:\n{issues_text}\n\n"
        "Return a corrected program that resolves every issue above while "
        "still following all the rules in the system prompt. Return ONLY the "
        "corrected JSON, matching the exact same schema as before."
    )
    payload = {
        "model": model,
        "temperature": settings.llm_temperature,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    if extra_headers:
        headers.update(extra_headers)

    with httpx.Client(timeout=settings.llm_timeout) as client:
        resp = client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    content = data["choices"][0]["message"]["content"]
    return _extract_json(content)


# ---------------------------------------------------------------------------
# OpenAI-compatible provider - used when an API key is configured
# ---------------------------------------------------------------------------
class OpenAIProvider:
    """Calls any plain OpenAI-compatible Chat Completions endpoint."""

    name = "openai"

    def generate_program(self, spec: VehicleSpec) -> dict:
        return _call_chat_completions(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            extra_headers=None,
            spec=spec,
        )

    def repair_program(
        self, spec: VehicleSpec, previous_program: dict, issues: list[ValidationIssue]
    ) -> dict:
        return _call_repair(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            extra_headers=None,
            spec=spec,
            previous_program=previous_program,
            issues=issues,
        )


# ---------------------------------------------------------------------------
# OpenRouter provider - a router in front of many model providers
# ---------------------------------------------------------------------------
class OpenRouterProvider:
    """Calls OpenRouter.ai using its OpenAI-compatible API.

    OpenRouter recommends (not requires) two extra headers identifying the
    calling app, used for their public leaderboard and rate-limit handling.
    Neither header is secret.
    """

    name = "openrouter"

    def generate_program(self, spec: VehicleSpec) -> dict:
        return _call_chat_completions(
            base_url="https://openrouter.ai/api/v1",
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            extra_headers={
                "HTTP-Referer": settings.openrouter_site_url,
                "X-Title": settings.openrouter_site_name,
            },
            spec=spec,
        )

    def repair_program(
        self, spec: VehicleSpec, previous_program: dict, issues: list[ValidationIssue]
    ) -> dict:
        return _call_repair(
            base_url="https://openrouter.ai/api/v1",
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            extra_headers={
                "HTTP-Referer": settings.openrouter_site_url,
                "X-Title": settings.openrouter_site_name,
            },
            spec=spec,
            previous_program=previous_program,
            issues=issues,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def get_provider() -> LLMProvider:
    """Return the configured provider instance.

    Falls back to the mock provider if a real provider is selected but no API
    key is present, so a misconfigured deployment degrades gracefully instead
    of crashing. (Runtime failures of a *configured* real provider are handled
    separately, in ``generator.py``, with a transparent fallback.)
    """
    if settings.llm_provider == "openrouter" and settings.llm_api_key:
        return OpenRouterProvider()
    if settings.llm_provider == "openai" and settings.llm_api_key:
        return OpenAIProvider()
    return MockProvider()


# ---------------------------------------------------------------------------
# Runtime corrective actions - "step failed on the line, now what?"
# ---------------------------------------------------------------------------
# This is a different problem from generation/repair above: those happen
# *before* the vehicle reaches the line (planning time). This is a runtime
# event - a step that was executed actually failed - and the corrective
# sub-program has to work with the world as it now is: some steps already
# ran, some ECUs may already be unlocked, and the fix has to be a short,
# targeted retry sequence, not a whole new program.
RECOVERY_SYSTEM_PROMPT = """\
You are an expert manufacturing engineer specialising in vehicle commissioning.
A commissioning step just FAILED at runtime on the production line. Given the
vehicle specification, the program that was running, which step failed, and
why, produce a short corrective sub-program (1-4 steps) that resolves the
issue and retries the failed action.

Rules you MUST follow:
- Only reference ECUs that appear in the specification's ECU list.
- Only use a UDS service on an ECU if that ECU lists it as supported.
- If the failure reason suggests security access was lost or denied, restore
  it (0x27) before retrying any flash/write step on that ECU.
- If the failure reason suggests a communication/timeout/session problem,
  re-open a diagnostic session (0x10) before retrying.
- The final step should retry the action that failed (same step_type/ecu,
  reasonable uds_service).
- Number new steps starting at the given next_order value. depends_on may
  reference either these new steps or the given list of already-completed
  step orders.

Return ONLY valid JSON, no prose, matching exactly this schema:
{
  "steps": [
    {
      "order": <int, starting at next_order>,
      "step_type": "diagnostic_session|security_access|flash_software|write_parameter|validation|fault_clear",
      "ecu_id": "<must be an ECU id from the spec>",
      "description": "<short human-readable summary>",
      "uds_service": "<e.g. 0x2E or null>",
      "estimated_seconds": <number>,
      "depends_on": [<orders of prerequisite steps>]
    }
  ],
  "notes": "<optional short rationale for the recovery strategy>"
}
"""


def _call_recovery(
    *, base_url: str, api_key: str, model: str, extra_headers: dict | None,
    spec: VehicleSpec, program: CommissioningProgram, failed_step: CommissioningStep,
    failure_reason: str,
) -> dict:
    """POST a recovery request and return the parsed JSON sub-program."""
    completed_orders = [s.order for s in program.steps if s.order <= failed_step.order]
    next_order = len(program.steps) + 1
    user_content = (
        "Vehicle specification (JSON):\n"
        + spec.model_dump_json(indent=2)
        + "\n\nProgram that was running (JSON):\n"
        + program.model_dump_json(indent=2)
        + f"\n\nFailed step (order {failed_step.order}):\n"
        + failed_step.model_dump_json(indent=2)
        + f"\n\nFailure reason: {failure_reason}"
        + f"\n\nAlready-completed step orders you may depend on: {completed_orders}"
        + f"\nNumber new recovery steps starting at order {next_order}."
        + "\n\nProduce the corrective sub-program as JSON now."
    )
    payload = {
        "model": model,
        "temperature": settings.llm_temperature,
        "messages": [
            {"role": "system", "content": RECOVERY_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    if extra_headers:
        headers.update(extra_headers)

    with httpx.Client(timeout=settings.llm_timeout) as client:
        resp = client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    content = data["choices"][0]["message"]["content"]
    return _extract_json(content)


def generate_recovery_program(
    spec: VehicleSpec, program: CommissioningProgram, failed_step: CommissioningStep,
    failure_reason: str,
) -> tuple[dict, str]:
    """Ask the configured real LLM provider for a corrective sub-program.

    Raises if no real provider is configured (mock has no opinion on runtime
    failures - it never fails by construction) or if the call itself fails;
    the caller (``recovery.py``) is responsible for the same
    generate-then-fallback pattern used everywhere else in this project.
    Returns (raw_dict, provider_name).
    """
    if settings.llm_provider == "openrouter" and settings.llm_api_key:
        raw = _call_recovery(
            base_url="https://openrouter.ai/api/v1",
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            extra_headers={
                "HTTP-Referer": settings.openrouter_site_url,
                "X-Title": settings.openrouter_site_name,
            },
            spec=spec, program=program, failed_step=failed_step, failure_reason=failure_reason,
        )
        return raw, "openrouter"
    if settings.llm_provider == "openai" and settings.llm_api_key:
        raw = _call_recovery(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            extra_headers=None,
            spec=spec, program=program, failed_step=failed_step, failure_reason=failure_reason,
        )
        return raw, "openai"
    raise RuntimeError("No real LLM provider configured; use the deterministic recovery policy instead.")
