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
};

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

    // Load the first sample into the editor immediately.
    if (samples.length) await loadSample(samples[0].file);
  } catch (err) {
    setError("Could not load sample specifications.");
  }
}

async function loadSample(filename) {
  const res = await fetch(`/api/samples/${encodeURIComponent(filename)}`);
  if (!res.ok) return setError("Sample not found.");
  const spec = await res.json();
  el.specInput.value = JSON.stringify(spec, null, 2);
  clearError();

  // A previously generated program corresponds to whatever spec was loaded
  // at the time - it does not describe this newly loaded spec. Clear it so
  // the two panels never show mismatched data.
  showState("empty");
  el.providerTag.hidden = true;
}

/* ------------------------------- Errors --------------------------------- */
function setError(message) {
  el.inlineError.textContent = message;
}
function clearError() {
  el.inlineError.textContent = "";
}

/* ------------------------------ Generate -------------------------------- */
async function generate() {
  clearError();

  // Parse the editor content so we fail fast on malformed JSON.
  let spec;
  try {
    spec = JSON.parse(el.specInput.value);
  } catch (err) {
    return setError("Spec is not valid JSON.");
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
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `Request failed (${res.status}).`);
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
  const { program, validation, analytics, is_valid, provider } = data;

  // Provider tag (mock / openai / openrouter / mock-fallback).
  el.providerTag.hidden = false;
  el.providerTag.textContent = `provider: ${provider}`;
  el.providerTag.classList.toggle("provider-tag-fallback", provider === "mock-fallback");

  // Metrics strip.
  const flashCount = analytics.steps_by_type.flash_software || 0;
  el.metrics.innerHTML = `
    ${metric(analytics.total_steps, "Steps")}
    ${metric(formatTime(analytics.estimated_cycle_time_seconds), "Est. cycle time")}
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
    setError("Cannot format: invalid JSON.");
  }
});

// If the user hand-edits the spec after already generating a program, the
// visible result no longer describes what's in the editor. Rather than
// leaving a stale, mismatched program on screen, invalidate it as soon as
// they start typing - it comes back the instant they press Generate again.
el.specInput.addEventListener("input", () => {
  if (!el.resultState.hidden) {
    showState("empty");
    el.providerTag.hidden = true;
  }
});

// Boot.
probeHealth();
loadSampleList();
showState("empty");
