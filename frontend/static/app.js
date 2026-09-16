/* Audio Transcription frontend.
   Vanilla JS single-page app; talks to the backend via same-origin
   /api + /ws routes (proxied by nginx in Docker, served directly on macOS). */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  system: null,
  settings: null,
  presets: [],
  jobs: new Map(),          // id -> job
  selectedFiles: [],        // {file, duration}
  eventsWs: null,
  live: null,               // active live session controller
  liveResultsJob: null,
  modelDownloads: new Map(),// "engine/model" -> {done,total,status}
};

/* ---------------- utilities ---------------- */

function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  const units = ["KB", "MB", "GB"];
  let u = -1;
  do { n /= 1024; u++; } while (n >= 1024 && u < units.length - 1);
  return n.toFixed(n >= 10 ? 0 : 1) + " " + units[u];
}
function fmtDuration(s) {
  if (s == null || isNaN(s)) return "—";
  s = Math.round(s);
  const m = Math.floor(s / 60), sec = s % 60;
  const h = Math.floor(m / 60);
  if (h) return `${h}:${String(m % 60).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
}
function fmtClock(ms) { return fmtDuration(ms / 1000); }
function fmtDate(t) { return t ? new Date(t * 1000).toLocaleString() : "—"; }
function esc(s) {
  const d = document.createElement("span");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}
function announce(msg) { $("aria-announcer").textContent = msg; }

async function api(path, opts = {}) {
  if (opts.json !== undefined) {
    opts.body = JSON.stringify(opts.json);
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers);
    delete opts.json;
  }
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) {
    const msg = body && (body.detail || body.message) || `HTTP ${res.status}`;
    const err = new Error(msg);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

/* ---------------- init ---------------- */

async function init() {
  wireStaticHandlers();
  try {
    const [system, settings, presets, jobs] = await Promise.all([
      api("/api/system"), api("/api/settings"), api("/api/presets"), api("/api/jobs"),
    ]);
    state.system = system;
    state.settings = settings;
    state.presets = presets;
    jobs.forEach((j) => state.jobs.set(j.id, j));
  } catch (e) {
    $("start-error").textContent = "Cannot reach the backend: " + e.message;
    return;
  }
  populateLanguageSelects();
  populateDeviceSelects();
  populatePresetSelects();
  applyDefaults();
  renderJobs();
  renderHistory();
  renderSettingsPanes();
  updateSummaries();
  connectEvents();
  listMicrophones();
  updateSourceHint();
  updateLimitsText();
}

function updateLimitsText() {
  const lim = state.system.limits;
  $("upload-limits").textContent =
    `or — limits: ${lim.max_upload_mb} MB, ${lim.max_duration_min} min per file. ` +
    `Formats: ${lim.supported_extensions.join(", ")} (decoder-dependent)`;
}

function connectEvents() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/events`);
  state.eventsWs = ws;
  ws.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    if (data.type === "job.updated") {
      const prev = state.jobs.get(data.job.id) || {};
      state.jobs.set(data.job.id, Object.assign({}, prev, data.job));
      renderJobs();
      renderHistory();
    } else if (data.type === "job.deleted") {
      state.jobs.delete(data.job_id);
      renderJobs();
      renderHistory();
    } else if (data.type === "model.download") {
      const key = `${data.engine}/${data.model}`;
      state.modelDownloads.set(key, data);
      renderModelsTable();
      if (data.status === "done") refreshSystem();
    }
  };
  ws.onclose = () => setTimeout(connectEvents, 3000);
}

async function refreshSystem() {
  try {
    state.system = await api("/api/system");
    populateDeviceSelects();
    populateModelSelects();
    renderHwOptions();
    renderModelsTable();
    updateSourceHint();
  } catch (e) { /* transient */ }
}

/* ---------------- collapsibles / tabs ---------------- */

function wireStaticHandlers() {
  document.querySelectorAll(".collapsible-header").forEach((h) => {
    h.addEventListener("click", () => {
      const body = $(h.dataset.target);
      const expanded = h.getAttribute("aria-expanded") === "true";
      h.setAttribute("aria-expanded", String(!expanded));
      body.hidden = expanded;
    });
  });
  $("tab-files").addEventListener("click", () => switchMode("files"));
  $("tab-live").addEventListener("click", () => switchMode("live"));
  $("tab-history").addEventListener("click", () => switchMode("history"));
  $("btn-settings").addEventListener("click", openSettings);
  $("btn-settings-close").addEventListener("click", () => $("settings-modal").close());
  $("btn-review-close").addEventListener("click", () => $("review-modal").close());
  document.querySelectorAll(".settings-tab").forEach((t) => {
    t.addEventListener("click", () => {
      document.querySelectorAll(".settings-tab").forEach((x) => {
        x.classList.toggle("active", x === t);
        x.setAttribute("aria-selected", String(x === t));
      });
      document.querySelectorAll(".settings-pane").forEach((p) => {
        p.hidden = p.id !== t.dataset.pane;
      });
    });
  });
  wireUpload();
  wireStartProcessing();
  wireLiveControls();
  wireSettingsControls();
  const syncSummaries = ["f-language", "f-model", "f-quality", "f-device", "f-translate",
    "f-target", "f-preset", "l-language", "l-model", "l-quality", "l-device",
    "l-translate", "l-target", "l-preset"];
  syncSummaries.forEach((id) => $(id).addEventListener("change", updateSummaries));
  $("f-device").addEventListener("change", populateModelSelects);
  $("l-device").addEventListener("change", populateModelSelects);
  window.addEventListener("beforeunload", (e) => {
    if (state.live) { e.preventDefault(); e.returnValue = ""; }
  });
}

function switchMode(mode) {
  // Switching modes never stops a session or cancels jobs; the global
  // recording chip keeps Stop reachable from everywhere.
  for (const [m, view, tab] of [["files", "view-files", "tab-files"],
                                ["live", "view-live", "tab-live"],
                                ["history", "view-history", "tab-history"]]) {
    const active = mode === m;
    $(view).hidden = !active;
    $(tab).classList.toggle("active", active);
    $(tab).setAttribute("aria-selected", String(active));
  }
  $("global-rec").hidden = !(state.live && mode !== "live");
}

/* ---------------- selects ---------------- */

function populateLanguageSelects() {
  const langs = state.system.languages;
  const build = (sel, withAuto) => {
    const el = $(sel);
    el.innerHTML = withAuto ? '<option value="">Auto-detect</option>' : "";
    const codes = Object.keys(langs).sort((a, b) => langs[a].localeCompare(langs[b]));
    for (const c of codes) {
      const o = document.createElement("option");
      o.value = c; o.textContent = `${langs[c]} (${c})`;
      el.appendChild(o);
    }
  };
  build("f-language", true);
  build("l-language", true);
  build("s-default-lang", true);
  const buildTargets = (sel) => {
    const el = $(sel);
    el.innerHTML = "";
    const common = state.system.common_targets;
    for (const c of common) {
      const o = document.createElement("option");
      o.value = c; o.textContent = `${langs[c]} (${c})`;
      el.appendChild(o);
    }
    const rest = Object.keys(langs).filter((c) => !common.includes(c))
      .sort((a, b) => langs[a].localeCompare(langs[b]));
    const grp = document.createElement("optgroup");
    grp.label = "All languages";
    for (const c of rest) {
      const o = document.createElement("option");
      o.value = c; o.textContent = `${langs[c]} (${c})`;
      grp.appendChild(o);
    }
    el.appendChild(grp);
  };
  buildTargets("f-target");
  buildTargets("l-target");
}

function activeEngineFor(deviceSel) {
  const device = $(deviceSel).value || "auto";
  const eng = state.system.engines;
  if (device === "apple") return "whispercpp";
  if (device === "nvidia") return "fasterwhisper";
  if (eng.whispercpp.available) return "whispercpp";
  if (eng.fasterwhisper.available) return "fasterwhisper";
  return "whispercpp";
}

function populateDeviceSelects() {
  for (const sel of ["f-device", "l-device"]) {
    const el = $(sel);
    const current = el.value;
    el.innerHTML = "";
    for (const d of state.system.devices) {
      const o = document.createElement("option");
      o.value = d.id;
      o.textContent = d.label + (d.available ? "" : " — unavailable");
      o.disabled = !d.available;
      el.appendChild(o);
    }
    el.value = current && [...el.options].some((o) => o.value === current && !o.disabled)
      ? current : "auto";
  }
  const hint = state.system.devices.find((d) => d.id === $("f-device").value);
  $("f-device-hint").textContent = hint ? hint.detail : "";
  populateModelSelects();
  populateComputeTypes();
}

function populateModelSelects() {
  for (const [sel, deviceSel, hintId] of [["f-model", "f-device", "f-model-hint"],
                                          ["l-model", "l-device", "l-model-hint"]]) {
    const engine = activeEngineFor(deviceSel);
    const el = $(sel);
    const current = el.value;
    el.innerHTML = "";
    for (const m of state.system.models[engine]) {
      const o = document.createElement("option");
      o.value = m.id;
      o.textContent = `${m.id} (${fmtBytes(m.bytes)})` +
        (m.installed ? "" : " — not downloaded") +
        (m.multilingual ? "" : " — English only");
      o.disabled = !m.installed;
      el.appendChild(o);
    }
    const usable = [...el.options].filter((o) => !o.disabled).map((o) => o.value);
    if (usable.includes(current)) el.value = current;
    else if (usable.length) el.value = usable.includes("base") ? "base" : usable[0];
    const hintEl = $(hintId);
    if (!usable.length) {
      hintEl.textContent = "No models downloaded for the active engine yet — " +
        "download one in Settings → Transcription → Models.";
    } else if (hintId === "f-model-hint") {
      hintEl.textContent = "Larger models are more accurate but slower.";
    }
  }
  populateComputeTypes();
}

function populateComputeTypes() {
  const engine = activeEngineFor("f-device");
  const caps = state.system.capabilities[engine];
  const wrap = $("f-compute-wrap");
  if (!caps.compute_types.length) {
    wrap.hidden = true;
  } else {
    wrap.hidden = false;
    const el = $("f-compute");
    el.innerHTML = "";
    for (const c of caps.compute_types) {
      const o = document.createElement("option");
      o.value = c; o.textContent = c;
      el.appendChild(o);
    }
  }
  $("f-vad-wrap").hidden = !caps.vad;
  $("f-words-wrap").hidden = !caps.word_timestamps;
  const extra = $("f-extra");
  if (caps.extra_mode === "kwargs") {
    extra.placeholder = "repetition_penalty=1.15\nno_repeat_ngram_size=3";
    $("f-extra-hint").textContent =
      "One key=value per line — any faster-whisper transcribe() parameter (e.g. " +
      "repetition_penalty, no_repeat_ngram_size, hallucination_silence_threshold, " +
      "patience). Values are parsed as JSON where possible; a bare key means true. " +
      "Unknown names are rejected with the full supported list.";
  } else {
    extra.placeholder = "-et 2.8 --suppress-nst";
    $("f-extra-hint").textContent =
      "Raw whisper-cli flags appended to the command (run `whisper-cli -h` for the " +
      "full list). Managed input/model/JSON-output flags cannot be overridden.";
  }
}

function populatePresetSelects() {
  for (const sel of ["f-preset", "l-preset", "s-default-preset"]) {
    const el = $(sel);
    const current = el.value;
    el.innerHTML = sel === "s-default-preset" ? '<option value="">None</option>' : "";
    for (const p of state.presets) {
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = p.name + (p.configured ? "" : " — awaiting configuration (TBA)");
      if (sel !== "s-default-preset") o.disabled = !p.configured;
      el.appendChild(o);
    }
    if ([...el.options].some((o) => o.value === current)) el.value = current;
  }
  updateTranslationStatusLines();
}

function applyDefaults() {
  const d = state.settings.defaults || {};
  if (d.language !== undefined) { $("f-language").value = d.language; $("l-language").value = d.language; }
  const dp = state.settings.default_preset_id;
  if (dp) { ["f-preset", "l-preset"].forEach((s) => { if ([...$(s).options].some((o) => o.value === dp && !o.disabled)) $(s).value = dp; }); }
  $("s-default-lang").value = d.language || "";
  $("s-default-preset").value = dp || "";
  $("retention-days").value = state.settings.retention_days;
}

function updateTranslationStatusLines() {
  for (const [statusId, translateId, presetId] of [
    ["f-translation-status", "f-translate", "f-preset"],
    ["l-translation-status", "l-translate", "l-preset"]]) {
    const on = $(translateId).checked;
    const el = $(statusId);
    if (!on) {
      el.textContent = "Translation is off. No transcript content is sent to any LLM.";
      continue;
    }
    const preset = state.presets.find((p) => p.id === $(presetId).value);
    if (!preset) {
      el.textContent = "Awaiting configuration: no usable LLM preset. Create one under Settings → LLM presets (TBA presets cannot be activated).";
    } else {
      el.textContent = `Segments will be sent to “${preset.name}” (${preset.base_url}). Provider-side retention is independent of local automatic deletion.`;
    }
  }
}

function updateSummaries() {
  const langName = (v) => v ? (state.system.languages[v] || v) : "Auto-detect";
  $("files-transcription-summary").textContent =
    `${langName($("f-language").value)} · ${$("f-model").value || "no model"} · ${$("f-quality").value} · ${$("f-device").value}`;
  $("live-transcription-summary").textContent =
    `${langName($("l-language").value)} · ${$("l-model").value || "no model"} · ${$("l-quality").value} · ${$("l-device").value}`;
  const tsum = (t, target, preset) => {
    if (!$(t).checked) return "Off";
    const p = state.presets.find((x) => x.id === $(preset).value);
    return `On → ${langName($(target).value)}${p ? " · " + p.name : " · no preset"}`;
  };
  $("files-translation-summary").textContent = tsum("f-translate", "f-target", "f-preset");
  $("live-translation-summary").textContent = tsum("l-translate", "l-target", "l-preset");
  updateTranslationStatusLines();
}

/* ---------------- From Files: upload ---------------- */

function wireUpload() {
  const dz = $("dropzone");
  const input = $("file-input");
  $("btn-select-files").addEventListener("click", (e) => { e.stopPropagation(); input.click(); });
  dz.addEventListener("click", () => input.click());
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  input.addEventListener("change", () => { addFiles([...input.files]); input.value = ""; });
  ["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => {
    e.preventDefault(); dz.classList.add("dragover");
  }));
  ["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => {
    e.preventDefault(); dz.classList.remove("dragover");
  }));
  dz.addEventListener("drop", (e) => addFiles([...e.dataTransfer.files]));
}

function addFiles(files) {
  for (const f of files) {
    if (state.selectedFiles.some((x) => x.file.name === f.name && x.file.size === f.size)) continue;
    const entry = { file: f, duration: null };
    state.selectedFiles.push(entry);
    probeDuration(entry);
  }
  renderSelectedFiles();
}

function probeDuration(entry) {
  // Best-effort duration via the browser decoder; the backend probes
  // authoritatively with its own decoder.
  const url = URL.createObjectURL(entry.file);
  const el = document.createElement(entry.file.type.startsWith("video") ? "video" : "audio");
  el.preload = "metadata";
  el.onloadedmetadata = () => {
    entry.duration = isFinite(el.duration) ? el.duration : null;
    URL.revokeObjectURL(url);
    renderSelectedFiles();
  };
  el.onerror = () => URL.revokeObjectURL(url);
  el.src = url;
}

function renderSelectedFiles() {
  const ul = $("selected-files");
  ul.innerHTML = "";
  $("files-empty").hidden = state.selectedFiles.length > 0;
  state.selectedFiles.forEach((entry, i) => {
    const li = document.createElement("li");
    li.innerHTML =
      `<span class="file-name" title="${esc(entry.file.name)}">${esc(entry.file.name)}</span>` +
      `<span class="file-meta">${esc(entry.file.type || "unknown type")}</span>` +
      `<span class="file-meta">${fmtBytes(entry.file.size)}</span>` +
      `<span class="file-meta">${entry.duration != null ? fmtDuration(entry.duration) : "duration —"}</span>` +
      `<button class="btn btn-ghost btn-sm file-remove" aria-label="Remove ${esc(entry.file.name)}">Remove</button>`;
    li.querySelector(".file-remove").addEventListener("click", () => {
      state.selectedFiles.splice(i, 1);
      renderSelectedFiles();
    });
    ul.appendChild(li);
  });
}

/* ---------------- From Files: processing ---------------- */

function collectSettings(prefix) {
  const v = (id) => $(prefix + id).value;
  const num = (id) => { const x = $(prefix + id).value; return x === "" ? undefined : Number(x); };
  const settings = {
    device: v("-device"),
    model: v("-model"),
    language: v("-language") || null,
    quality_preset: v("-quality"),
    translate: $(prefix + "-translate").checked,
    target_language: v("-target"),
    llm_preset_id: v("-preset") || null,
    advanced: {},
  };
  if (prefix === "f") {
    const adv = settings.advanced;
    if (num("-beam") !== undefined) adv.beam_size = num("-beam");
    if (num("-temp") !== undefined) adv.temperature = num("-temp");
    if (!$("f-compute-wrap").hidden) adv.compute_type = v("-compute");
    if (!$("f-vad-wrap").hidden) adv.vad = $("f-vad").checked;
    if (!$("f-words-wrap").hidden) adv.word_timestamps = $("f-words").checked;
    if ($("f-prompt").value.trim()) adv.initial_prompt = $("f-prompt").value.trim();
    const extraRaw = $("f-extra").value.trim();
    if (extraRaw) {
      const caps = state.system.capabilities[activeEngineFor("f-device")];
      if (caps.extra_mode === "kwargs") {
        const extra = {};
        for (const line of extraRaw.split("\n")) {
          const t = line.trim();
          if (!t || t.startsWith("#")) continue;
          const i = t.indexOf("=");
          const key = (i < 0 ? t : t.slice(0, i)).trim();
          if (!key) continue;
          if (i < 0) { extra[key] = true; continue; }  // bare key = boolean flag
          const raw = t.slice(i + 1).trim();
          try { extra[key] = JSON.parse(raw); } catch { extra[key] = raw; }
        }
        if (Object.keys(extra).length) adv.extra = extra;
      } else {
        adv.extra_args = extraRaw;
      }
    }
    if (num("-track") !== undefined) settings.audio_track = num("-track");
  } else {
    const adv = settings.advanced;
    if (num("-maxchunk") !== undefined) adv.max_chunk_s = num("-maxchunk");
    if (num("-silence") !== undefined) adv.silence_ms = num("-silence");
    if (num("-partial") !== undefined) adv.partial_interval_s = num("-partial");
    settings.source = $("l-source").value;
  }
  return settings;
}

function validateTranslation(settings, errEl) {
  if (!settings.translate) return true;
  if (!settings.target_language) {
    errEl.textContent = "Translation is enabled: choose a target language.";
    return false;
  }
  const preset = state.presets.find((p) => p.id === settings.llm_preset_id);
  if (!preset || !preset.configured) {
    errEl.textContent = "Translation is enabled but no configured LLM preset is selected. " +
      "Presets with TBA values cannot be activated.";
    return false;
  }
  return true;
}

function wireStartProcessing() {
  $("btn-start").addEventListener("click", async () => {
    const err = $("start-error");
    err.textContent = "";
    if (!state.selectedFiles.length) {
      err.textContent = "Add at least one file first.";
      return;
    }
    const settings = collectSettings("f");
    if (!settings.model) {
      err.textContent = "No model is available. Download one in Settings → Transcription → Models.";
      return;
    }
    if (!validateTranslation(settings, err)) return;
    const fd = new FormData();
    for (const e of state.selectedFiles) fd.append("files", e.file, e.file.name);
    fd.append("settings", JSON.stringify(settings));
    $("btn-start").disabled = true;
    $("btn-start").textContent = "Uploading…";
    try {
      const results = await api("/api/jobs", { method: "POST", body: fd });
      for (const r of results) state.jobs.set(r.job.id, r.job);
      state.selectedFiles = [];
      renderSelectedFiles();
      renderJobs();
    } catch (e) {
      err.textContent = "Upload failed: " + e.message;
    } finally {
      $("btn-start").disabled = false;
      $("btn-start").textContent = "Start Processing";
    }
  });
}

const STAGE_LABEL = {
  queued: "Queued", preparing: "Preparing Audio", transcribing: "Transcribing",
  translating: "Translating", exporting: "Exporting", done: "Completed",
};
const STATUS_CHIP = {
  queued: ["run", "Queued"], running: ["run", "Processing"],
  recording: ["rec", "● Recording"],
  completed: ["ok", "✓ Completed"],
  completed_with_translation_errors: ["warn", "✓ Completed — translation errors"],
  failed: ["err", "✕ Failed"], canceled: ["", "Canceled"],
  interrupted: ["warn", "⚠ Interrupted"],
};

function chipFor(job) {
  const [cls, label] = STATUS_CHIP[job.status] || ["", job.status];
  return `<span class="chip ${cls}">${esc(label)}</span>`;
}

function renderJobs() {
  const active = [], done = [];
  for (const job of [...state.jobs.values()].sort((a, b) => (b.created_at || 0) - (a.created_at || 0))) {
    if (job.kind !== "file") continue;
    if (["queued", "running"].includes(job.status)) active.push(job);
    else done.push(job);
  }
  $("progress-panel").hidden = active.length === 0;
  const pl = $("progress-list");
  pl.innerHTML = "";
  for (const job of active) pl.appendChild(renderProgressRow(job));
  $("results-panel").hidden = done.length === 0;
  const rl = $("results-list");
  rl.innerHTML = "";
  for (const job of done) rl.appendChild(renderResultRow(job));
}

function renderProgressRow(job) {
  const li = document.createElement("li");
  const stage = STAGE_LABEL[job.stage] || job.stage;
  const indeterminate = job.stage === "preparing" || job.stage === "exporting" ||
    (job.stage === "queued");
  const pct = Math.round((job.progress || 0) * 100);
  li.innerHTML =
    `<div class="job-row-top">${chipFor(job)}<span class="job-name">${esc(job.display_name)}</span>` +
    `<span class="job-stage">${esc(stage)}${indeterminate ? "" : ` — ${pct}%`}</span>` +
    `<span class="job-actions"><button class="btn btn-sm" data-act="cancel">Cancel</button></span></div>` +
    `<div class="progress-track"><div class="progress-fill ${indeterminate ? "indeterminate" : ""}" style="width:${pct}%"></div></div>` +
    (job.progress_label ? `<div class="job-note">${esc(job.progress_label)}</div>` : "");
  li.querySelector('[data-act="cancel"]').addEventListener("click", () =>
    api(`/api/jobs/${job.id}/cancel`, { method: "POST" }).catch((e) => alert(e.message)));
  return li;
}

function artifactButtons(job, kinds) {
  const btns = [];
  for (const a of job.artifacts || []) {
    if (kinds && !kinds.includes(a.kind)) continue;
    btns.push(`<a class="btn btn-sm" href="/api/jobs/${job.id}/download/${encodeURIComponent(a.kind === "input" ? a.file : a.kind)}" download>${esc(a.label)}</a>`);
  }
  return btns.join(" ");
}

function renderResultRow(job) {
  const li = document.createElement("li");
  const hasTranslated = (job.artifacts || []).some((a) => a.kind === "translated_txt");
  const canRetryT = ["completed", "completed_with_translation_errors"].includes(job.status);
  const canRetry = ["failed", "canceled", "interrupted"].includes(job.status);
  li.innerHTML =
    `<div class="job-row-top">${chipFor(job)}<span class="job-name">${esc(job.display_name)}</span>` +
    `<span class="job-actions">` +
    ((job.artifacts || []).some((a) => a.kind === "original_txt")
      ? `<button class="btn btn-sm" data-act="review">Review</button>` : "") +
    `<button class="btn btn-sm btn-ghost" data-act="delete">Delete</button></span></div>` +
    `<div class="artifact-row">` +
    artifactButtons(job, ["original_txt", "original_srt", "recording"]) +
    (hasTranslated ? " " + artifactButtons(job, ["translated_txt", "translated_srt"]) : "") +
    `</div>` +
    (job.error ? `<div class="job-error">${esc(job.error)}</div>` : "") +
    (((job.media || {}).audio_tracks || []).length > 1
      ? `<div class="job-note">${job.media.audio_tracks.length} audio tracks detected — track ${(job.settings || {}).audio_track || 0}${((job.settings || {}).audio_track || 0) === 0 ? " (default)" : ""} was used. Tracks: ${job.media.audio_tracks.map((t) => `#${t.index} ${esc(t.codec)} ${esc(t.language || "")}`).join(", ")}. To transcribe another track, upload again with Advanced → Audio track set.</div>` : "") +
    ((job.status === "completed_with_translation_errors" && canRetryT)
      ? `<div class="job-note">Some segments were not translated. <button class="btn btn-sm" data-act="retry-t">Retry translation</button> (does not retranscribe)</div>` : "") +
    (canRetry ? `<div class="job-note"><button class="btn btn-sm" data-act="retry">Retry</button></div>` : "") +
    (job.expires_at ? `<div class="job-note">Scheduled deletion: ${fmtDate(job.expires_at)}</div>` : "");
  const on = (act, fn) => {
    const b = li.querySelector(`[data-act="${act}"]`);
    if (b) b.addEventListener("click", fn);
  };
  on("review", () => openReview(job.id));
  on("delete", () => deleteJob(job));
  on("retry", () => api(`/api/jobs/${job.id}/retry`, { method: "POST" }).catch((e) => alert(e.message)));
  on("retry-t", () => api(`/api/jobs/${job.id}/retry-translation`, { method: "POST" }).catch((e) => alert(e.message)));
  return li;
}

async function deleteJob(job) {
  if (!confirm(`Delete “${job.display_name}” and all of its stored media, transcripts, and exports? This cannot be undone.`)) return;
  try {
    await api(`/api/jobs/${job.id}`, { method: "DELETE" });
    state.jobs.delete(job.id);
    renderJobs(); renderHistory();
  } catch (e) { alert(e.message); }
}

/* ---------------- review modal ---------------- */

async function openReview(jobId) {
  const job = state.jobs.get(jobId) || await api(`/api/jobs/${jobId}`);
  const segments = await api(`/api/jobs/${jobId}/segments`);
  $("review-title").textContent = job.display_name;
  const s = job.settings || {};
  const langs = state.system.languages;
  $("review-meta").textContent =
    `${fmtDate(job.created_at)} · ${s.effective_engine || ""}` +
    (s.detected_language ? ` · source: ${langs[s.detected_language] || s.detected_language}` : "") +
    (s.translate ? ` · translated to ${langs[s.target_language] || s.target_language}` : "");
  $("review-downloads").innerHTML = artifactButtons(job);
  const wrap = $("review-rows");
  wrap.innerHTML = "";
  const showT = !!s.translate;
  for (const seg of segments) {
    wrap.appendChild(segmentRow(seg, showT));
  }
  if (!segments.length) wrap.innerHTML = '<p class="empty-state">No transcript segments were produced.</p>';
  $("review-modal").showModal();
}

function segmentRow(seg, showTranslation) {
  const row = document.createElement("div");
  row.className = "seg-row" + (showTranslation ? "" : " no-translation");
  let t = "";
  if (showTranslation) {
    if (seg.translation_status === "done") {
      t = `<div class="seg-cell"><div class="seg-label">Translated</div><div class="seg-text">${esc(seg.translation)}</div></div>`;
    } else if (seg.translation_status === "pending") {
      t = `<div class="seg-cell"><div class="seg-label">Translated</div><div class="seg-tstatus">Translating…</div></div>`;
    } else if (seg.translation_status === "error") {
      t = `<div class="seg-cell"><div class="seg-label">Translated</div><div class="seg-tstatus err">Not translated — ${esc(seg.translation_error || "error")}</div></div>`;
    } else {
      t = `<div class="seg-cell"></div>`;
    }
  }
  row.innerHTML =
    `<div class="seg-time">${fmtClock(seg.start_ms)}</div>` +
    `<div class="seg-cell"><div class="seg-label">Original</div><div class="seg-text">${esc(seg.text)}</div></div>` + t;
  return row;
}

/* ---------------- Real Time mode ---------------- */

function updateSourceHint() {
  const src = $("l-source").value;
  $("l-mic-wrap").hidden = src !== "microphone";
  const hint = $("l-source-hint");
  if (src === "system") {
    const cap = state.system.capture.computer_audio;
    if (cap.mode === "native") {
      hint.textContent = "Computer Audio captures what this machine plays (meetings, videos, other apps) via the native capture helper. The first session asks for the Screen & System Audio Recording permission.";
    } else if (cap.mode === "companion") {
      hint.textContent = "Computer Audio uses the Windows capture companion: " + cap.reason;
    } else {
      hint.textContent = "Computer Audio is unavailable: " + cap.reason;
    }
    $("btn-live-start").disabled = !cap.available && !state.live;
  } else {
    hint.textContent = "Microphone capture uses this browser. Grant microphone permission when prompted.";
    $("btn-live-start").disabled = false;
  }
}

async function listMicrophones() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const mics = devices.filter((d) => d.kind === "audioinput");
    const el = $("l-mic");
    el.innerHTML = "";
    mics.forEach((m, i) => {
      const o = document.createElement("option");
      o.value = m.deviceId;
      o.textContent = m.label || `Microphone ${i + 1}`;
      el.appendChild(o);
    });
    if (!mics.length) el.innerHTML = '<option value="">No microphone found</option>';
  } catch (e) { /* enumeration denied */ }
}

function wireLiveControls() {
  $("l-source").addEventListener("change", updateSourceHint);
  $("btn-live-start").addEventListener("click", startLive);
  $("btn-live-stop").addEventListener("click", stopLive);
  $("global-stop").addEventListener("click", stopLive);
  const rows = $("live-rows");
  rows.addEventListener("scroll", () => {
    const away = rows.scrollTop > 40;
    $("btn-return-latest").hidden = !away;
    if (state.live) state.live.scrolledAway = away;
  });
  $("btn-return-latest").addEventListener("click", () => {
    rows.scrollTop = 0;
    $("btn-return-latest").hidden = true;
    if (state.live) state.live.scrolledAway = false;
  });
}

function setLiveControlsLocked(locked) {
  ["l-source", "l-mic", "l-language", "l-model", "l-quality", "l-device",
   "l-maxchunk", "l-silence", "l-partial", "l-translate", "l-target", "l-preset"]
    .forEach((id) => { $(id).disabled = locked; });
}

async function startLive() {
  $("l-error").textContent = "";
  const settings = collectSettings("l");
  if (!settings.model) {
    $("l-error").textContent = "No model is available for the selected hardware. Download one in Settings → Transcription → Models.";
    return;
  }
  if (!validateTranslation(settings, $("l-error"))) return;

  const source = settings.source;
  let mic = null;
  if (source === "microphone") {
    // Validate permission and device before opening the session.
    try {
      const constraints = { audio: $("l-mic").value ? { deviceId: { exact: $("l-mic").value } } : true };
      mic = await navigator.mediaDevices.getUserMedia(constraints);
    } catch (e) {
      $("l-error").textContent = e.name === "NotAllowedError"
        ? "Microphone permission was denied. Allow microphone access for this site in the browser settings, then try again."
        : `Could not open the microphone: ${e.message}`;
      return;
    }
    listMicrophones(); // labels become visible after permission
  }

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/live`);
  ws.binaryType = "arraybuffer";
  const live = {
    ws, mic, source, paused: false, scrolledAway: false, segments: new Map(),
    startedAt: Date.now(), timerInterval: null, audioCtx: null, stopping: false,
    showTranslation: settings.translate,
  };
  state.live = live;

  ws.onopen = () => ws.send(JSON.stringify({ type: "start", settings }));
  ws.onmessage = (ev) => handleLiveMessage(live, ev);
  ws.onclose = () => {
    if (state.live === live && !live.stopping) {
      $("l-error").textContent = "Connection to the backend was lost. The session was preserved as interrupted; see Previous jobs.";
      teardownLive(live);
    }
  };
  $("live-empty").hidden = true;
  $("live-rows").innerHTML = "";
  $("live-results-panel").hidden = true;
}

function handleLiveMessage(live, ev) {
  let msg;
  try { msg = JSON.parse(ev.data); } catch (e) { return; }
  switch (msg.type) {
    case "status":
      if (msg.state === "recording" && !live.recording) {
        live.recording = true;
        live.jobId = msg.job_id;
        onRecordingStarted(live, msg.engine);
      } else if (msg.detail === "waiting_companion") {
        setChip("warn", msg.message);
      } else if (msg.detail === "silence") {
        setChip("warn", "Silence — no speech detected");
      } else if (msg.state === "stopping") {
        setChip("run", "Finishing — flushing buffered speech");
      }
      break;
    case "level": {
      const pct = Math.min(100, Math.round(msg.rms * 400));
      $("l-level").style.width = pct + "%";
      $("l-timer").textContent = fmtClock(msg.elapsed_ms);
      $("global-rec-time").textContent = fmtClock(msg.elapsed_ms);
      if (msg.backlog_s) setChip("warn", `Processing delay: ${msg.backlog_s}s of audio buffered`);
      else if (live.paused) { live.paused = false; setChip("rec", "Recording"); }
      else if (live.chipState === "warn-backlog") setChip("rec", "Recording");
      break;
    }
    case "partial":
      upsertLiveRow(live, msg.segment, true);
      break;
    case "final":
      upsertLiveRow(live, msg.segment, false);
      break;
    case "retract": {
      const row = live.segments.get(msg.index);
      if (row) { row.remove(); live.segments.delete(msg.index); }
      break;
    }
    case "translation":
      applyLiveTranslation(live, msg);
      break;
    case "warning":
      setChip("warn", msg.message);
      break;
    case "error":
      $("l-error").textContent = msg.message;
      announce("Error: " + msg.message);
      if (msg.code === "overload") { live.paused = true; setChip("err", "Paused — processing backlog"); }
      if (!msg.recoverable) stopLive();
      break;
    case "stopped":
      live.stopping = true;
      onSessionStopped(live, msg);
      break;
  }
}

function setChip(cls, text) {
  const chip = $("live-status-chip");
  chip.hidden = false;
  chip.className = "chip " + cls;
  chip.textContent = text;
  if (state.live) state.live.chipState = cls === "warn" ? "warn-backlog" : cls;
}

function onRecordingStarted(live, engine) {
  $("btn-live-start").hidden = true;
  $("btn-live-stop").hidden = false;
  $("l-recstate").textContent = "Recording";
  $("l-recstate").classList.add("recording");
  setChip("rec", "Recording");
  setLiveControlsLocked(true);
  announce("Recording started");
  $("l-source-hint").textContent = `Session running with ${engine}.`;
  if (live.source === "microphone") startMicPipeline(live);
  live.timerInterval = setInterval(() => {
    if (!$("l-timer").textContent || $("l-timer").textContent === "0:00") {
      $("l-timer").textContent = fmtDuration((Date.now() - live.startedAt) / 1000);
    }
  }, 1000);
}

function startMicPipeline(live) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  live.audioCtx = ctx;
  const src = ctx.createMediaStreamSource(live.mic);
  const proc = ctx.createScriptProcessor(4096, 1, 1);
  live.proc = proc;
  const inRate = ctx.sampleRate;
  let acc = [];
  let accLen = 0;
  const targetChunk = Math.round(16000 * 0.25); // 250 ms at 16 kHz
  proc.onaudioprocess = (e) => {
    if (!live.recording || live.paused || live.stopping) return;
    const input = e.inputBuffer.getChannelData(0);
    // linear-interpolation downsample to 16 kHz
    const ratio = inRate / 16000;
    const outLen = Math.floor(input.length / ratio);
    const out = new Int16Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const pos = i * ratio;
      const i0 = Math.floor(pos), i1 = Math.min(i0 + 1, input.length - 1);
      const frac = pos - i0;
      let v = input[i0] * (1 - frac) + input[i1] * frac;
      v = Math.max(-1, Math.min(1, v));
      out[i] = v < 0 ? v * 32768 : v * 32767;
    }
    acc.push(out);
    accLen += out.length;
    if (accLen >= targetChunk) {
      const merged = new Int16Array(accLen);
      let off = 0;
      for (const a of acc) { merged.set(a, off); off += a.length; }
      acc = []; accLen = 0;
      if (live.ws.readyState === WebSocket.OPEN) live.ws.send(merged.buffer);
    }
  };
  src.connect(proc);
  proc.connect(ctx.destination);
  live.mic.getAudioTracks().forEach((t) => {
    t.onended = () => {
      if (live.ws.readyState === WebSocket.OPEN) {
        live.ws.send(JSON.stringify({ type: "device_lost", message: "The microphone was disconnected. Recording of received audio is preserved; stop the session or reconnect the device." }));
      }
      $("l-error").textContent = "Microphone disconnected.";
      announce("Microphone disconnected");
    };
  });
}

function upsertLiveRow(live, seg, isPartial) {
  const rows = $("live-rows");
  let row = live.segments.get(seg.index);
  const showT = live.showTranslation;
  if (!row) {
    row = document.createElement("div");
    row.dataset.index = seg.index;
    live.segments.set(seg.index, row);
    const prevHeight = rows.scrollHeight;
    rows.prepend(row); // newest first
    if (live.scrolledAway) {
      // keep the reading position stable as content is prepended
      rows.scrollTop += rows.scrollHeight - prevHeight;
    }
  }
  row.className = "seg-row" + (showT ? "" : " no-translation") + (isPartial ? " partial" : "");
  let t = "";
  if (showT) {
    if (isPartial) {
      t = `<div class="seg-cell"><div class="seg-label">Translated</div><div class="seg-tstatus">—</div></div>`;
    } else if (seg.translation_status === "pending") {
      t = `<div class="seg-cell"><div class="seg-label">Translated</div><div class="seg-tstatus">Translating…</div></div>`;
    } else if (seg.translation_status === "error") {
      t = `<div class="seg-cell"><div class="seg-label">Translated</div><div class="seg-tstatus err">Not translated</div></div>`;
    } else {
      t = `<div class="seg-cell"><div class="seg-label">Translated</div><div class="seg-tstatus">—</div></div>`;
    }
  }
  row.innerHTML =
    `<div class="seg-time">${fmtClock(seg.start_ms)}</div>` +
    `<div class="seg-cell"><div class="seg-label">${isPartial ? "Provisional" : "Original"}</div>` +
    `<div class="seg-text">${esc(seg.text)}</div></div>` + t;
}

function applyLiveTranslation(live, msg) {
  const row = live.segments.get(msg.seg_index);
  if (!row) return;
  const cell = row.children[2];
  if (!cell) return;
  if (msg.status === "done") {
    cell.innerHTML = `<div class="seg-label">Translated</div><div class="seg-text">${esc(msg.text)}</div>`;
  } else {
    cell.innerHTML = `<div class="seg-label">Translated</div><div class="seg-tstatus err">Not translated — ${esc(msg.error || "error")}</div>`;
  }
}

function stopLive() {
  const live = state.live;
  if (!live || live.stopRequested) return;
  live.stopRequested = true;
  // Stop capture immediately; the backend flushes buffered speech, then
  // reports the final session result.
  stopMicPipeline(live);
  setChip("run", "Finishing — flushing buffered speech and pending translation");
  if (live.ws.readyState === WebSocket.OPEN) {
    live.ws.send(JSON.stringify({ type: "stop" }));
  } else {
    teardownLive(live);
  }
}

function stopMicPipeline(live) {
  if (live.proc) { try { live.proc.disconnect(); } catch (e) {} }
  if (live.audioCtx) { try { live.audioCtx.close(); } catch (e) {} }
  if (live.mic) live.mic.getTracks().forEach((t) => t.stop());
}

function onSessionStopped(live, msg) {
  teardownLive(live);
  announce("Recording stopped");
  const job = state.jobs.get(msg.job_id);
  const results = $("live-results");
  $("live-results-panel").hidden = false;
  const fake = { id: msg.job_id, artifacts: msg.artifacts };
  results.innerHTML = artifactButtons(fake) ||
    '<p class="empty-state">No speech was recognized in this session.</p>';
  if (msg.status === "completed_with_translation_errors") {
    results.insertAdjacentHTML("beforeend",
      `<p class="status-line">Some segments were not translated. Open the Previous Jobs tab to retry translation.</p>`);
  }
}

function teardownLive(live) {
  live.stopping = true;
  stopMicPipeline(live);
  if (live.timerInterval) clearInterval(live.timerInterval);
  if (live.ws.readyState === WebSocket.OPEN) live.ws.close();
  state.live = null;
  $("btn-live-start").hidden = false;
  $("btn-live-stop").hidden = true;
  $("l-recstate").textContent = "Not recording";
  $("l-recstate").classList.remove("recording");
  $("live-status-chip").hidden = true;
  $("l-level").style.width = "0%";
  $("global-rec").hidden = true;
  setLiveControlsLocked(false);
  updateSourceHint();
}

/* ---------------- settings modal ---------------- */

function openSettings() {
  renderSettingsPanes();
  $("settings-modal").showModal();
}

function renderSettingsPanes() {
  renderPresetList();
  renderHwOptions();
  renderModelsTable();
}

function wireSettingsControls() {
  $("btn-preset-new").addEventListener("click", () => showPresetForm(null));
  $("btn-preset-cancel").addEventListener("click", () => { $("preset-form").hidden = true; });
  $("preset-form").addEventListener("submit", savePreset);
  $("btn-preset-test").addEventListener("click", testPresetFromForm);
  $("btn-save-defaults").addEventListener("click", saveDefaults);
  $("btn-save-retention").addEventListener("click", () => saveRetention(false));
  $("btn-retention-confirm").addEventListener("click", () => saveRetention(true));
  $("btn-retention-cancel").addEventListener("click", () => { $("retention-confirm").hidden = true; });
}

function renderPresetList() {
  const ul = $("preset-list");
  ul.innerHTML = "";
  if (!state.presets.length) {
    ul.innerHTML = '<li class="empty-state">No LLM presets yet. Translation stays unavailable until a preset with real values is created.</li>';
  }
  for (const p of state.presets) {
    const li = document.createElement("li");
    li.innerHTML =
      `<strong>${esc(p.name)}</strong>` +
      `<span class="chip ${p.configured ? "ok" : "warn"}">${p.configured ? "configured" : "awaiting configuration (TBA)"}</span>` +
      `<span class="muted">${esc(p.base_url)} · ${esc(p.model_id)} · key ${p.has_key ? "set" : "not set"}</span>` +
      `<span class="job-actions">` +
      `<button class="btn btn-sm" data-act="edit">Edit</button>` +
      `<button class="btn btn-sm" data-act="test">Test Connection</button>` +
      `<button class="btn btn-sm btn-ghost" data-act="del">Delete</button></span>` +
      `<span class="status-line" data-role="result" style="flex-basis:100%"></span>`;
    li.querySelector('[data-act="edit"]').addEventListener("click", () => showPresetForm(p));
    li.querySelector('[data-act="del"]').addEventListener("click", async () => {
      if (!confirm(`Delete preset “${p.name}”?`)) return;
      await api(`/api/presets/${p.id}`, { method: "DELETE" });
      await reloadPresets();
    });
    li.querySelector('[data-act="test"]').addEventListener("click", async (e) => {
      const out = li.querySelector('[data-role="result"]');
      out.textContent = "Testing…";
      try {
        const r = await api(`/api/presets/${p.id}/test`, { method: "POST" });
        out.textContent = (r.ok ? "✓ " : `✕ [${r.category}] `) + r.message;
      } catch (err) { out.textContent = "✕ " + err.message; }
    });
    ul.appendChild(li);
  }
}

function showPresetForm(p) {
  $("preset-form").hidden = false;
  $("preset-test-result").textContent = "";
  $("p-id").value = p ? p.id : "";
  $("p-name").value = p ? p.name : "";
  $("p-base").value = p ? p.base_url : "";
  $("p-model").value = p ? p.model_id : "";
  $("p-timeout").value = p ? p.timeout_s : 60;
  $("p-key").value = "";
  $("p-temp").value = p && p.params.temperature != null ? p.params.temperature : "";
  $("p-maxtok").value = p && p.params.max_tokens != null ? p.params.max_tokens : "";
  $("p-name").focus();
}

async function savePreset(e) {
  e.preventDefault();
  const id = $("p-id").value;
  const params = {};
  if ($("p-temp").value !== "") params.temperature = Number($("p-temp").value);
  if ($("p-maxtok").value !== "") params.max_tokens = Number($("p-maxtok").value);
  const body = {
    name: $("p-name").value.trim(),
    base_url: $("p-base").value.trim() || "TBA",
    model_id: $("p-model").value.trim() || "TBA",
    timeout_s: Number($("p-timeout").value) || 60,
    params,
  };
  if ($("p-key").value) body.api_key = $("p-key").value;
  try {
    if (id) await api(`/api/presets/${id}`, { method: "PUT", json: body });
    else await api("/api/presets", { method: "POST", json: body });
    $("preset-form").hidden = true;
    await reloadPresets();
  } catch (err) {
    $("preset-test-result").textContent = "✕ " + err.message;
  }
}

async function testPresetFromForm() {
  const id = $("p-id").value;
  const out = $("preset-test-result");
  if (!id) { out.textContent = "Save the preset first, then test it."; return; }
  out.textContent = "Testing…";
  try {
    const r = await api(`/api/presets/${id}/test`, { method: "POST" });
    out.textContent = (r.ok ? "✓ " : `✕ [${r.category}] `) + r.message;
  } catch (err) { out.textContent = "✕ " + err.message; }
}

async function reloadPresets() {
  state.presets = await api("/api/presets");
  renderPresetList();
  populatePresetSelects();
  updateSummaries();
}

function renderHwOptions() {
  const wrap = $("hw-options");
  wrap.innerHTML = "";
  const current = (state.settings.defaults || {}).device || "auto";
  for (const d of state.system.devices) {
    const label = document.createElement("label");
    label.className = "hw-option" + (d.available ? "" : " disabled");
    label.innerHTML =
      `<input type="radio" name="hw" value="${d.id}" ${d.available ? "" : "disabled"} ${d.id === current ? "checked" : ""}>` +
      `<span><strong>${esc(d.label)}</strong> ${d.available ? "" : '<span class="chip warn">unavailable</span>'}` +
      `<div class="hw-detail">${esc(d.detail)}${d.available ? "" : " — " + esc(d.how_to_enable)}</div></span>`;
    wrap.appendChild(label);
  }
  wrap.querySelectorAll('input[name="hw"]').forEach((r) => r.addEventListener("change", async () => {
    const defaults = Object.assign({}, state.settings.defaults, { device: r.value });
    state.settings = await api("/api/settings", { method: "PUT", json: { defaults } });
    $("f-device").value = r.value;
    $("l-device").value = r.value;
    populateModelSelects();
    updateSummaries();
  }));
  const note = $("models-engine-note");
  const eng = state.system.engines;
  note.textContent =
    `whisper.cpp: ${eng.whispercpp.available ? "installed" : "not installed — " + eng.whispercpp.install_hint} · ` +
    `faster-whisper: ${eng.fasterwhisper.available ? "installed" : "not installed — " + eng.fasterwhisper.install_hint}`;
}

function renderModelsTable() {
  const tbody = $("models-tbody");
  if (!state.system) return;
  tbody.innerHTML = "";
  for (const engine of ["whispercpp", "fasterwhisper"]) {
    if (!state.system.engines[engine].available) continue;
    for (const m of state.system.models[engine]) {
      const key = `${engine}/${m.id}`;
      const dl = state.modelDownloads.get(key);
      let status, action = "";
      if (m.installed) {
        status = '<span class="chip ok">downloaded</span>';
        action = `<button class="btn btn-sm btn-ghost" data-key="${key}" data-act="delmodel">Delete</button>`;
      } else if (dl && dl.status === "error") {
        status = `<span class="chip err">failed</span> <span class="muted">${esc(dl.error)}</span>`;
        action = `<button class="btn btn-sm" data-key="${key}" data-act="dl">Retry</button>`;
      } else if (dl && !dl.status) {
        const pct = dl.total ? Math.round(dl.done / dl.total * 100) : 0;
        status = `<span class="chip run">downloading ${pct}%</span>`;
      } else {
        status = '<span class="chip">not downloaded</span>';
        action = `<button class="btn btn-sm" data-key="${key}" data-act="dl">Download</button>`;
      }
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${esc(m.id)} <span class="muted">(${engine})</span></td>` +
        `<td>${fmtBytes(m.bytes)}</td><td>${status}</td><td>${action}</td>`;
      tbody.appendChild(tr);
    }
  }
  tbody.querySelectorAll("[data-act='dl']").forEach((b) => b.addEventListener("click", () => {
    const [engine, model] = b.dataset.key.split("/");
    state.modelDownloads.set(b.dataset.key, { done: 0, total: 1 });
    renderModelsTable();
    api("/api/models/download", { method: "POST", json: { engine, model } })
      .catch((e) => alert(e.message));
  }));
  tbody.querySelectorAll("[data-act='delmodel']").forEach((b) => b.addEventListener("click", async () => {
    const [engine, model] = b.dataset.key.split("/");
    if (!confirm(`Delete the downloaded model “${model}”?`)) return;
    await api(`/api/models/${engine}/${model}`, { method: "DELETE" });
    state.modelDownloads.delete(b.dataset.key);
    refreshSystem();
  }));
}

async function saveDefaults() {
  const defaults = Object.assign({}, state.settings.defaults, {
    language: $("s-default-lang").value,
  });
  state.settings = await api("/api/settings", {
    method: "PUT",
    json: { defaults, default_preset_id: $("s-default-preset").value || null },
  });
  $("defaults-saved").textContent = "✓ Defaults saved. They apply to new work, not existing jobs.";
  applyDefaults();
}

function renderHistory() {
  const ul = $("history-list");
  if (!ul) return;
  ul.innerHTML = "";
  const jobs = [...state.jobs.values()].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  let bytes = 0;
  jobs.forEach((j) => { bytes += j.storage_bytes || 0; });
  $("history-summary").textContent = jobs.length
    ? `${jobs.length} job(s) · ${fmtBytes(bytes)} of managed storage. Deleting a job removes its uploads, recordings, transcripts, and exports.`
    : "No previous jobs.";
  for (const job of jobs) {
    const li = document.createElement("li");
    const s = job.settings || {};
    const langs = state.system ? state.system.languages : {};
    const langInfo = [
      s.detected_language ? (langs[s.detected_language] || s.detected_language) : (s.language ? (langs[s.language] || s.language) : "auto"),
      s.translate ? "→ " + (langs[s.target_language] || s.target_language) : null,
    ].filter(Boolean).join(" ");
    const active = ["queued", "running", "recording"].includes(job.status);
    li.innerHTML =
      `<div class="job-row-top">${chipFor(job)}` +
      `<span class="job-name">${esc(job.display_name)}</span>` +
      `<span class="job-stage">${job.kind === "live" ? "Live session" : "File"} · ${fmtDate(job.created_at)} · ${esc(langInfo)} · ${fmtBytes(job.storage_bytes)}</span>` +
      `<span class="job-actions">` +
      ((job.artifacts || []).length ? `<button class="btn btn-sm" data-act="review">Review</button>` : "") +
      (["completed", "completed_with_translation_errors"].includes(job.status) && s.translate
        ? `<button class="btn btn-sm" data-act="retry-t">Retry translation</button>` : "") +
      `<button class="btn btn-sm btn-ghost" data-act="delete">${active ? "Cancel & delete" : "Delete"}</button></span></div>` +
      `<div class="artifact-row">${artifactButtons(job)}</div>` +
      (job.expires_at ? `<div class="job-note">Scheduled deletion: ${fmtDate(job.expires_at)}</div>` : "") +
      (job.error ? `<div class="job-error">${esc(job.error)}</div>` : "");
    const on = (act, fn) => { const b = li.querySelector(`[data-act="${act}"]`); if (b) b.addEventListener("click", fn); };
    on("review", () => openReview(job.id));
    on("delete", () => deleteJob(job));
    on("retry-t", () => api(`/api/jobs/${job.id}/retry-translation`, { method: "POST" }).catch((e) => alert(e.message)));
    ul.appendChild(li);
  }
}

async function saveRetention(confirmed) {
  const days = Number($("retention-days").value);
  $("retention-saved").textContent = "";
  try {
    const res = await fetch("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ retention_days: days, confirm_retention: confirmed }),
    });
    const body = await res.json();
    if (res.status === 409 && body.needs_confirmation) {
      $("retention-confirm").hidden = false;
      $("retention-confirm-msg").textContent = body.message;
      $("retention-affected").innerHTML = body.affected
        .map((j) => `<li>${esc(j.display_name)} (finished ${fmtDate(j.terminal_at)})</li>`).join("");
      return;
    }
    if (!res.ok) throw new Error(body.detail || "Failed to save.");
    state.settings = body;
    $("retention-confirm").hidden = true;
    $("retention-saved").textContent = `✓ Retention set to ${days} day(s).`;
    const jobs = await api("/api/jobs");
    state.jobs = new Map(jobs.map((j) => [j.id, j]));
    renderJobs(); renderHistory();
  } catch (e) {
    $("retention-saved").textContent = "✕ " + e.message;
  }
}

init();
