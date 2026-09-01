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
  programNotesWrap: document.getElementById("programNotesWrap"),
  metrics: document.getElementById("metrics"),
  validationBanner: document.getElementById("validationBanner"),
  issuesBlock: document.getElementById("issuesBlock"),
  issuesList: document.getElementById("issuesList"),
  stepsBody: document.getElementById("stepsBody"),
  exportOtxBtn: document.getElementById("exportOtxBtn"),
  batchBtn: document.getElementById("batchBtn"),
  batchResult: document.getElementById("batchResult"),
  ganttUnlimited: document.getElementById("ganttUnlimited"),
  channelInput: document.getElementById("channelInput"),
  channelScheduleBtn: document.getElementById("channelScheduleBtn"),
  channelSweepBtn: document.getElementById("channelSweepBtn"),
  channelCycleTime: document.getElementById("channelCycleTime"),
  ganttChannels: document.getElementById("ganttChannels"),
  channelSweepChart: document.getElementById("channelSweepChart"),
  failedStepSelect: document.getElementById("failedStepSelect"),
  failureReasonSelect: document.getElementById("failureReasonSelect"),
  recoverBtn: document.getElementById("recoverBtn"),
  recoveryResult: document.getElementById("recoveryResult"),
};

// The most recently generated program (+ the spec it came from), kept
// around so Export OTX / channel scheduling / recovery simulation can call
// their own endpoints without re-running generation. Cleared whenever the
// spec changes.
let lastProgram = null;
let lastSpec = null;

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
  if (name !== "loading") stopProgress();
}

/* ------------------------------ Progress -------------------------------- */
// Generation is a single, slow LLM call - typically ~20s, occasionally much
// longer, and slower still if the free-tier host has gone to sleep and has
// to wake up first. With no feedback that reads as "the page is broken", so
// we show elapsed time plus a phase label.
//
// The bar is deliberately honest: it advances against a *typical* duration,
// and once it passes that it switches to an indeterminate pulse instead of
// creeping toward 100% and stalling there. A progress bar that lies about
// how far along it is, is worse than one that admits it doesn't know.
let progressTimer = null;
let progressStart = 0;

const PROGRESS_PHASES = [
  { at: 0,  text: "Sending specification to the model…" },
  { at: 3,  text: "Model is generating the program…" },
  { at: 12, text: "Still generating - reasoning about UDS ordering…" },
  { at: 25, text: "Taking longer than usual. Validating once it returns…" },
  { at: 45, text: "Still working. A self-repair round may have been triggered…" },
  { at: 75, text: "Unusually slow - the host may be waking from sleep…" },
];

function startProgress({ typicalSeconds = 20, firstCall = false } = {}) {
  stopProgress();
  progressStart = performance.now();

  const track = document.getElementById("progressTrack");
  const fill = document.getElementById("progressFill");
  const phase = document.getElementById("progressPhase");
  const elapsedEl = document.getElementById("progressElapsed");
  const hintEl = document.getElementById("progressHint");
  if (!track || !fill) return;

  track.classList.remove("indeterminate");
  fill.style.width = "0%";
  if (hintEl) {
    hintEl.textContent = firstCall
      ? "first request may take ~30s extra to wake the server"
      : `typically ~${typicalSeconds}s`;
  }

  const tick = () => {
    const secs = (performance.now() - progressStart) / 1000;
    if (elapsedEl) elapsedEl.textContent = `${secs.toFixed(1)}s`;

    // Advance to at most 90% over the typical duration, then hand over to
    // the indeterminate animation rather than faking further progress.
    if (secs < typicalSeconds) {
      fill.style.width = `${Math.min(90, (secs / typicalSeconds) * 90)}%`;
    } else if (!track.classList.contains("indeterminate")) {
      track.classList.add("indeterminate");
    }

    if (phase) {
      let text = PROGRESS_PHASES[0].text;
      for (const p of PROGRESS_PHASES) if (secs >= p.at) text = p.text;
      if (phase.textContent !== text) phase.textContent = text;
    }
    const meta = elapsedEl && elapsedEl.parentElement;
    if (meta) meta.classList.toggle("slow", secs > 45);
  };

  tick();
  progressTimer = setInterval(tick, 100);
}

function stopProgress() {
  if (progressTimer) {
    clearInterval(progressTimer);
    progressTimer = null;
  }
  const track = document.getElementById("progressTrack");
  const fill = document.getElementById("progressFill");
  if (track) track.classList.remove("indeterminate");
  if (fill) fill.style.width = "100%";
}

/** Whether any request has completed yet this page-load (cold-start hint). */
let hasCompletedARequest = false;

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
  lastSpec = null;
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
  startProgress({ typicalSeconds: 20, firstCall: !hasCompletedARequest });

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
    hasCompletedARequest = true;
    lastSpec = spec;
    renderResult(data);
    showState("result");
  } catch (err) {
    setError(err.message);
    showState("empty");
  } finally {
    stopProgress();
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
  // Free text authored by the LLM - never checked by structural validation,
  // so it is shown clearly labelled and separate from the validation banner
  // above (see issue #14: a spec's process_standards can try to get the
  // model to write false certification/compliance claims into notes).
  if (program.notes) {
    el.programNotesWrap.hidden = false;
    el.programNotes.textContent = program.notes;
  } else {
    el.programNotesWrap.hidden = true;
    el.programNotes.textContent = "";
  }

  // Unlimited-parallelism Gantt: lanes = ECUs (each ECU's own steps are
  // strictly sequential; different ECUs can run concurrently under this
  // scheduler, so grouping by ECU is exactly the right visual mapping).
  renderGantt(el.ganttUnlimited, program.steps, optimization.schedule, {
    laneKeyFn: (step) => step.ecu_id,
    criticalOrders: new Set(optimization.critical_path_steps),
  });

  // Reset the channel/recovery panels - they describe the previous program.
  el.channelCycleTime.textContent = "";
  el.ganttChannels.innerHTML = "";
  el.channelSweepChart.hidden = true;
  el.channelSweepChart.innerHTML = "";
  el.recoveryResult.hidden = true;
  el.recoveryResult.innerHTML = "";

  // Populate the recovery simulator's step picker.
  el.failedStepSelect.innerHTML = program.steps
    .map(
      (s) =>
        `<option value="${s.order}">#${s.order} ${esc(STEP_LABELS[s.step_type] || s.step_type)} on ${esc(s.ecu_id)}</option>`
    )
    .join("");
}

/**
 * Render a Gantt chart into `container` from a list of steps and a matching
 * start/end schedule (either the unlimited-parallelism schedule or a
 * channel-constrained one). `opts.laneKeyFn(step)` decides which row a step
 * lands on; `opts.criticalOrders` (optional) highlights bars on the
 * critical path in red.
 */
function renderGantt(container, steps, schedule, opts = {}) {
  const { laneKeyFn, criticalOrders } = opts;
  const byOrder = new Map(schedule.map((s) => [s.order, s]));
  const maxEnd = Math.max(1, ...schedule.map((s) => s.end));

  // Group steps into lanes, preserving first-seen lane order.
  const lanes = new Map(); // laneKey -> [steps]
  steps.forEach((step) => {
    const key = laneKeyFn(step);
    if (!lanes.has(key)) lanes.set(key, []);
    lanes.get(key).push(step);
  });

  container.innerHTML = Array.from(lanes.entries())
    .map(([laneKey, laneSteps]) => {
      const bars = laneSteps
        .map((step) => {
          const timing = byOrder.get(step.order);
          if (!timing) return "";
          const left = (timing.start / maxEnd) * 100;
          const width = Math.max(0.6, ((timing.end - timing.start) / maxEnd) * 100);
          const critical = criticalOrders && criticalOrders.has(step.order) ? "critical" : "";
          const label = STEP_LABELS[step.step_type] || step.step_type;
          return `
            <div class="gantt-bar ${critical}" style="left:${left}%;width:${width}%"
                 title="#${esc(step.order)} ${esc(label)} (${esc(timing.start)}s - ${esc(timing.end)}s)${step.channel !== undefined ? ` · channel ${esc(step.channel)}` : ""}">
              ${esc(label)}
            </div>`;
        })
        .join("");
      return `
        <div class="gantt-row">
          <div class="gantt-label">${esc(laneKey)}</div>
          <div class="gantt-track">${bars}</div>
        </div>`;
    })
    .join("");
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
    lastSpec = null;
  }
});

/* ------------------------------ OTX export ------------------------------- */
el.exportOtxBtn.addEventListener("click", async () => {
  if (!lastProgram || !lastSpec) return;
  try {
    const res = await fetch("/api/export/otx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec: lastSpec, program: lastProgram }),
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

  // The batch runs its specs concurrently server-side, so it finishes in
  // roughly the time of a single generation rather than N times longer -
  // but that is still tens of seconds, so show a live elapsed counter here
  // too rather than a static "Running…".
  const started = performance.now();
  const render = (note) => {
    const secs = ((performance.now() - started) / 1000).toFixed(1);
    el.batchResult.innerHTML =
      `<p class="hint">Running batch across all sample vehicles… ` +
      `<strong>${secs}s</strong><br><span class="progress-meta">${esc(note)}</span></p>`;
  };
  render("specs run concurrently, so this takes about as long as one vehicle");
  const ticker = setInterval(() => {
    const secs = (performance.now() - started) / 1000;
    render(
      secs > 60
        ? "unusually slow - the host may be waking from sleep"
        : "specs run concurrently, so this takes about as long as one vehicle"
    );
  }, 200);

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
    clearInterval(ticker);
    hasCompletedARequest = true;
    renderBatch(data, (performance.now() - started) / 1000);
  } catch (err) {
    clearInterval(ticker);
    el.batchResult.innerHTML = `<p class="hint">Batch failed: ${esc(err.message)}</p>`;
  } finally {
    clearInterval(ticker);
    el.batchBtn.disabled = false;
  }
});

function renderBatch(data, wallSeconds) {
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
      wallSeconds
        ? `<p class="hint">Generated ${a.vehicles} vehicles in ${wallSeconds.toFixed(1)}s wall-clock ` +
          `(the pipeline runs them concurrently).</p>`
        : ""
    }
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

/* --------------------------- Channel scheduling -------------------------- */
el.channelScheduleBtn.addEventListener("click", async () => {
  if (!lastProgram) return setError("Generate a program first.");
  const channels = Math.max(1, parseInt(el.channelInput.value, 10) || 1);
  try {
    const res = await fetch("/api/optimize/channels", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ program: lastProgram, channels }),
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      throw new Error(formatApiError(payload, res.status));
    }
    const data = await res.json();
    el.channelCycleTime.textContent =
      `With ${data.channels} channel(s): cycle time ${formatTime(data.cycle_time_seconds)}.`;
    renderGantt(el.ganttChannels, lastProgram.steps, data.schedule, {
      laneKeyFn: (step) => {
        const timing = data.schedule.find((s) => s.order === step.order);
        return timing ? `Channel ${timing.channel}` : "?";
      },
    });
  } catch (err) {
    setError(err.message);
  }
});

el.channelSweepBtn.addEventListener("click", async () => {
  if (!lastProgram) return setError("Generate a program first.");
  try {
    const res = await fetch("/api/optimize/channel-sweep", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ program: lastProgram, max_channels: 16 }),
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      throw new Error(formatApiError(payload, res.status));
    }
    const data = await res.json();
    const maxTime = Math.max(1, ...data.points.map((p) => p.cycle_time_seconds));
    el.channelSweepChart.hidden = false;
    el.channelSweepChart.innerHTML = data.points
      .map((p) => {
        const heightPct = Math.max(2, (p.cycle_time_seconds / maxTime) * 100);
        return `
          <div class="sweep-bar-wrap" title="${esc(p.channels)} channel(s): ${esc(p.cycle_time_seconds)}s">
            <div class="sweep-bar" style="height:${heightPct}%"></div>
            <div class="sweep-bar-label">${esc(p.channels)}</div>
          </div>`;
      })
      .join("");
  } catch (err) {
    setError(err.message);
  }
});

/* ------------------------------- Recovery -------------------------------- */
el.recoverBtn.addEventListener("click", async () => {
  if (!lastProgram || !lastSpec) return setError("Generate a program first.");
  const failedStepOrder = parseInt(el.failedStepSelect.value, 10);
  const failureReason = el.failureReasonSelect.value;
  el.recoveryResult.hidden = false;
  el.recoveryResult.innerHTML = `<p class="hint">Generating recovery…</p>`;
  try {
    const res = await fetch("/api/recover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        spec: lastSpec,
        program: lastProgram,
        failed_step_order: failedStepOrder,
        failure_reason: failureReason,
      }),
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      throw new Error(formatApiError(payload, res.status));
    }
    const data = await res.json();
    renderRecovery(data);
  } catch (err) {
    el.recoveryResult.innerHTML = `<p class="hint">Recovery failed: ${esc(err.message)}</p>`;
  }
});

function renderRecovery(data) {
  const bannerClass = data.is_valid ? "valid" : "invalid";
  const bannerText = data.is_valid
    ? `Recovery generated by provider "${data.provider}" and passed validation.`
    : `Recovery generated by provider "${data.provider}" but has validation issues.`;

  const stepsRows = data.recovery_steps
    .map(
      (step) => `
        <tr>
          <td class="num">${esc(step.order)}</td>
          <td><span class="type-tag">${esc(STEP_LABELS[step.step_type] || step.step_type)}</span></td>
          <td class="ecu">${esc(step.ecu_id)}</td>
          <td class="uds">${esc(step.uds_service || "—")}</td>
          <td>${esc(step.description)}</td>
        </tr>`
    )
    .join("");

  const issuesHtml = data.validation.length
    ? `<ul>${data.validation.map((i) => `<li><span class="sev ${esc(i.severity)}">${esc(i.severity)}</span> ${esc(i.message)}</li>`).join("")}</ul>`
    : "";

  el.recoveryResult.innerHTML = `
    <div class="banner ${bannerClass}">${esc(bannerText)}</div>
    <table class="steps-table">
      <thead><tr><th>#</th><th>Type</th><th>ECU</th><th>UDS</th><th>Description</th></tr></thead>
      <tbody>${stepsRows}</tbody>
    </table>
    ${data.notes ? `
    <div class="program-notes-wrap" style="margin-top:12px;">
      <div class="program-notes-label">AI-generated explanation — not structurally validated</div>
      <p class="program-notes" style="display:block;">${esc(data.notes)}</p>
    </div>` : ""}
    ${issuesHtml}
  `;
}

// Boot.
probeHealth();
loadSampleList();
showState("empty");
