/* ==========================================================================
   spec2program - frontend logic
   Vanilla JS, no build step. Talks to the FastAPI backend over fetch().
   Responsibilities:
     - probe /api/health and show provider status
     - load the list of bundled sample specs into the dropdown
     - let the user edit the spec JSON and POST it to /api/generate
     - render the returned program, validation findings, and analytics
   ========================================================================== */

"use strict";

// Cache DOM references once.
const el = {
  statusPill: document.getElementById("statusPill"),
  statusDot: document.getElementById("statusDot"),
  statusText: document.getElementById("statusText"),
  sampleSelect: document.getElementById("sampleSelect"),
  specInput: document.getElementById("specInput"),
  generateBtn: document.getElementById("generateBtn"),
  formatBtn: document.getElementById("formatBtn"),
  inlineError: document.getElementById("inlineError"),
  providerTag: document.getElementById("providerTag"),
  emptyState: document.getElementById("emptyState"),
  loadingState: document.getElementById("loadingState"),
  resultState: document.getElementById("resultState"),
  programNotes: document.getElementById("programNotes"),
  metrics: document.getElementById("metrics"),
  validationBanner: document.getElementById("validationBanner"),
  issuesBlock: document.getElementById("issuesBlock"),
  issuesList: document.getElementById("issuesList"),
  stepsBody: document.getElementById("stepsBody"),
  exportOtxBtn: document.getElementById("exportOtxBtn"),
  batchBtn: document.getElementById("batchBtn"),
  batchResult: document.getElementById("batchResult"),
};

// The most recently generated program, kept around so "Export OTX" can send
// it without re-running generation. Cleared whenever the spec changes.
let lastProgram = null;

// Human-friendly labels for the machine step-type enum.
const STEP_LABELS = {
  diagnostic_session: "Session",
  security_access: "Unlock",
  flash_software: "Flash",
  write_parameter: "Write",
  validation: "Validate",
  fault_clear: "Clear DTC",
};

/** Small helper: escape text before injecting into innerHTML. */
function esc(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

/** Toggle which of the three output states is visible. */
function showState(name) {
  el.emptyState.hidden = name !== "empty";
  el.loadingState.hidden = name !== "loading";
  el.resultState.hidden = name !== "result";
}

/* --------------------------- Health / status ---------------------------- */
async function probeHealth() {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    const isLive = data.provider === "openai" || data.provider === "openrouter";
    el.statusPill.classList.remove("down");
    el.statusPill.classList.add(isLive ? "ok" : "mock");
    el.statusText.textContent = isLive
      ? `LLM: ${data.model || data.provider}`
      : "LLM: mock (offline)";
  } catch (err) {
    el.statusPill.classList.add("down");
    el.statusText.textContent = "offline";
  }
}

/* ----------------------------- Sample specs ----------------------------- */
// True once the user has typed into the spec editor themselves. Guards
// against a real race condition: on page load we asynchronously fetch the
// sample list and auto-populate the editor with the first one. Those fetches
// take a moment over the network - if the user starts typing their own spec
// before that auto-load resolves, it must NOT silently overwrite what they
// just wrote. Any programmatic load the user did not explicitly ask for
// (via the dropdown) checks this flag before touching the editor.
let userHasEditedSpec = false;

async function loadSampleList() {
  try {
    const res = await fetch("/api/samples");
    const samples = await res.json();

    el.sampleSelect.innerHTML = "";
    samples.forEach((s, index) => {
      const opt = document.createElement("option");
      opt.value = s.file;
      opt.textContent = `${s.model} ${s.model_year} — ${s.vehicle_id}`;
      if (index === 0) opt.selected = true;
      el.sampleSelect.appendChild(opt);
    });

    // Auto-populate the editor with the first sample - but only if the user
    // has not already started typing their own spec in the meantime.
    if (samples.length) await loadSample(samples[0].file, { userInitiated: false });
  } catch (err) {
    setError("Could not load sample specifications.");
  }
}

async function loadSample(filename, { userInitiated = true } = {}) {
  const res = await fetch(`/api/samples/${encodeURIComponent(filename)}`);
  if (!res.ok) return setError("Sample not found.");
  const spec = await res.json();

  // This call was the initial silent auto-load, but the user has since
  // started editing the spec themselves - discard this load entirely rather
  // than clobbering their work.
  if (!userInitiated && userHasEditedSpec) return;

  el.specInput.value = JSON.stringify(spec, null, 2);
  clearError();

  // A previously generated program corresponds to whatever spec was loaded
  // at the time - it does not describe this newly loaded spec. Clear it so
  // the two panels never show mismatched data.
  showState("empty");
  el.providerTag.hidden = true;
  el.exportOtxBtn.hidden = true;
  lastProgram = null;
}

/* ------------------------------- Errors --------------------------------- */
function setError(message) {
  el.inlineError.textContent = message;
}
function clearError() {
  el.inlineError.textContent = "";
}

/**
 * Turn a FastAPI error response body into a readable message.
 *
 * FastAPI's `detail` field is NOT always a string:
 *   - App-raised HTTPException(detail="...")   -> detail is a string
 *   - Pydantic request-validation failures      -> detail is an ARRAY of
 *     {loc, msg, type} objects (one per invalid field), auto-generated
 *     before our code even runs (e.g. a required field like vehicle_id
 *     is missing from the spec, or a field has the wrong type).
 * Blindly doing `new Error(detail)` on the array case stringifies it to
 * something like "[object Object],[object Object]" - unreadable. This
 * formats each validation error as "field.path: message" instead.
 */
function formatApiError(payload, status) {
  const detail = payload && payload.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail
      .map((e) => {
        const path = Array.isArray(e.loc)
          ? e.loc.filter((p) => p !== "body" && p !== "spec").join(".")
          : "";
        return path ? `${path}: ${e.msg}` : e.msg;
      })
      .join("; ");
  }
  return `Request failed (HTTP ${status}).`;
}

/* ------------------------------ Generate -------------------------------- */
async function generate() {
  clearError();

  // Parse the editor content so we fail fast on malformed JSON.
  let spec;
  try {
    spec = JSON.parse(el.specInput.value);
  } catch (err) {
    return setError(`Spec is not valid JSON: ${err.message}`);
  }

  el.generateBtn.disabled = true;
  showState("loading");

  try {
    const res = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec }),
    });

    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      throw new Error(formatApiError(payload, res.status));
    }

    const data = await res.json();
    renderResult(data);
    showState("result");
  } catch (err) {
    setError(err.message);
    showState("empty");
  } finally {
    el.generateBtn.disabled = false;
  }
}

/* ------------------------------- Render --------------------------------- */
function renderResult(data) {
  const { program, validation, analytics, optimization, is_valid, provider, repair_attempts } = data;

  lastProgram = program;
  el.exportOtxBtn.hidden = false;

  // Provider tag (mock / openai / openrouter / mock-fallback), plus a note
  // if the self-repair loop had to run.
  el.providerTag.hidden = false;
  el.providerTag.textContent = repair_attempts
    ? `provider: ${provider} · repaired ×${repair_attempts}`
    : `provider: ${provider}`;
  el.providerTag.classList.toggle("provider-tag-fallback", provider === "mock-fallback");

  // Metrics strip. Cycle time now shows both the naive sequential total and
  // the critical-path (parallelised) minimum, with the speedup factor - the
  // concrete optimisation number the project is meant to surface.
  const flashCount = analytics.steps_by_type.flash_software || 0;
  el.metrics.innerHTML = `
    ${metric(analytics.total_steps, "Steps")}
    ${metric(formatTime(analytics.estimated_cycle_time_seconds), "Sequential time")}
    ${metric(formatTime(optimization.critical_path_seconds), "Critical-path time")}
    ${metric(`${optimization.speedup_factor}×`, "Speedup potential")}
    ${metric(`${analytics.ecus_covered}/${analytics.ecus_total}`, "ECUs covered")}
    ${metric(flashCount, "Flash ops")}
  `;

  // Validation banner.
  el.validationBanner.className = "banner " + (is_valid ? "valid" : "invalid");
  el.validationBanner.textContent = is_valid
    ? "Program passed all structural, UDS, and safety checks."
    : "Program has validation errors — see findings below.";

  // Issues list.
  if (validation.length) {
    el.issuesBlock.hidden = false;
    el.issuesList.innerHTML = validation
      .map(
        (issue) => `
          <li>
            <span class="sev ${esc(issue.severity)}">${esc(issue.severity)}</span>
            <span>${esc(issue.message)}</span>
          </li>`
      )
      .join("");
  } else {
    el.issuesBlock.hidden = true;
    el.issuesList.innerHTML = "";
  }

  // Steps table.
  el.stepsBody.innerHTML = program.steps
    .map(
      (step) => `
        <tr>
          <td class="num">${esc(step.order)}</td>
          <td><span class="type-tag">${esc(STEP_LABELS[step.step_type] || step.step_type)}</span></td>
          <td class="ecu">${esc(step.ecu_id)}</td>
          <td class="uds">${esc(step.uds_service || "—")}</td>
          <td>${esc(step.description)}</td>
          <td class="num">${esc(step.estimated_seconds)}</td>
        </tr>`
    )
    .join("");

  // Generator notes (rationale, or a transparent fallback explanation).
  if (program.notes) {
    el.programNotes.hidden = false;
    el.programNotes.textContent = program.notes;
  } else {
    el.programNotes.hidden = true;
    el.programNotes.textContent = "";
  }
}

function metric(value, label) {
  return `
    <div class="metric">
      <div class="metric-value">${esc(value)}</div>
      <div class="metric-label">${esc(label)}</div>
    </div>`;
}

/** Format seconds as "m:ss" when >= 60s, else "Ns". */
function formatTime(seconds) {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

/* ------------------------------- Wiring --------------------------------- */
el.sampleSelect.addEventListener("change", (e) => loadSample(e.target.value));
el.generateBtn.addEventListener("click", generate);
el.formatBtn.addEventListener("click", () => {
  try {
    const parsed = JSON.parse(el.specInput.value);
    el.specInput.value = JSON.stringify(parsed, null, 2);
    clearError();
  } catch (err) {
    // Surface the browser's actual parse error (e.g. "Unexpected token G in
    // JSON at position 1") instead of a generic message, so the user can
    // actually find and fix the problem rather than guessing.
    setError(`Cannot format: ${err.message}`);
  }
});

// If the user hand-edits the spec after already generating a program, the
// visible result no longer describes what's in the editor. Rather than
// leaving a stale, mismatched program on screen, invalidate it as soon as
// they start typing - it comes back the instant they press Generate again.
el.specInput.addEventListener("input", () => {
  // Mark the spec as user-owned so the initial auto-load (if still in
  // flight) knows not to overwrite it. See loadSampleList()/loadSample().
  userHasEditedSpec = true;

  if (!el.resultState.hidden) {
    showState("empty");
    el.providerTag.hidden = true;
    el.exportOtxBtn.hidden = true;
    lastProgram = null;
  }
});

/* ------------------------------ OTX export ------------------------------- */
el.exportOtxBtn.addEventListener("click", async () => {
  if (!lastProgram) return;
  try {
    const res = await fetch("/api/export/otx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(lastProgram),
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      throw new Error(formatApiError(payload, res.status));
    }
    const xml = await res.text();
    const blob = new Blob([xml], { type: "application/xml" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${lastProgram.vehicle_id || "program"}_otx.xml`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    setError(err.message);
  }
});

/* ------------------------------ Batch mode ------------------------------- */
el.batchBtn.addEventListener("click", async () => {
  el.batchBtn.disabled = true;
  el.batchResult.hidden = false;
  el.batchResult.innerHTML = `<p class="hint">Running batch…</p>`;
  try {
    const sampleList = await (await fetch("/api/samples")).json();
    const specs = await Promise.all(
      sampleList.map((s) =>
        fetch(`/api/samples/${encodeURIComponent(s.file)}`).then((r) => r.json())
      )
    );
    const res = await fetch("/api/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ specs }),
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      throw new Error(formatApiError(payload, res.status));
    }
    const data = await res.json();
    renderBatch(data);
  } catch (err) {
    el.batchResult.innerHTML = `<p class="hint">Batch failed: ${esc(err.message)}</p>`;
  } finally {
    el.batchBtn.disabled = false;
  }
});

function renderBatch(data) {
  const a = data.aggregate;
  el.batchResult.innerHTML = `
    <div class="metrics">
      ${metric(a.vehicles, "Vehicles")}
      ${metric(`${Math.round(a.validity_rate * 100)}%`, "Validity rate")}
      ${metric(formatTime(a.avg_cycle_time_seconds), "Avg sequential time")}
      ${metric(formatTime(a.avg_critical_path_seconds), "Avg critical-path time")}
      ${metric(`${a.avg_speedup_factor}×`, "Avg speedup")}
    </div>
    ${
      a.bottleneck_ecus.length
        ? `<p class="hint">Recurring bottleneck ECUs: ${a.bottleneck_ecus.map(esc).join(", ")}</p>`
        : ""
    }
    ${
      a.most_common_issue
        ? `<p class="hint">Most common validation finding: ${esc(a.most_common_issue)}</p>`
        : ""
    }
  `;
}

// Boot.
probeHealth();
loadSampleList();
showState("empty");
