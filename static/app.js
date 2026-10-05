// Backdate.dwg front end: upload -> converting -> download, one screen at a time.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const UPLOAD_SHARE = 20; // % of the bar used by the upload itself
  const POLL_MS = 500;

  const state = {
    config: null,
    step: "upload", // upload | converting | done | error
    file: null, // File
    info: null, // {kind, code, label, short}
    target: 2010,
    format: "DWG",
    job: null,
    xhr: null,
    pollTimer: null,
    pollFailures: 0,
    uploadStarted: 0,
    showAllReport: false,
  };

  // ── helpers ─────────────────────────────────────────────────────────────
  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function formatEta(seconds) {
    if (seconds == null) return "";
    if (seconds < 5) return "A few seconds left";
    if (seconds < 60) return `About ${Math.ceil(seconds / 5) * 5} seconds left`;
    const m = Math.round(seconds / 60);
    return `About ${m} minute${m === 1 ? "" : "s"} left`;
  }

  function plural(n, word) {
    return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
  }

  function codeNum(code) {
    const m = /^AC(\d{4})$/.exec(code || "");
    return m ? Number(m[1]) : Infinity;
  }

  function show(step) {
    state.step = step;
    const screen = step === "error" ? "upload" : step;
    for (const s of ["upload", "converting", "done"]) $(`screen-${s}`).hidden = s !== screen;
    window.scrollTo({ top: 0 });
  }

  async function api(path, options = {}) {
    const res = await fetch(path, options);
    let body = null;
    if (res.status !== 204) {
      try {
        body = await res.json();
      } catch {
        body = null;
      }
    }
    if (!res.ok) {
      const err = new Error((body && body.error && body.error.message) || `Server error (${res.status})`);
      err.code = body && body.error && body.error.code;
      err.status = res.status;
      throw err;
    }
    return body;
  }

  // ── upload screen ───────────────────────────────────────────────────────
  function renderTargets() {
    const box = $("target-chips");
    box.textContent = "";
    for (const t of state.config.targets) {
      const label = document.createElement("label");
      label.className = "chip";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "target";
      input.value = t.year;
      input.checked = t.year === state.target;
      const tooOld = state.info && codeNum(t.code) > codeNum(state.info.code);
      input.disabled = !!tooOld;
      label.title = tooOld ? `Your file is already older (${state.info.label})` : t.label;
      const span = document.createElement("span");
      span.textContent = t.year;
      label.append(input, span);
      box.append(label);
    }
  }

  function syncOptions() {
    const cfg = state.config;
    // Keep the chosen target valid for the selected file.
    if (state.info) {
      const allowed = cfg.targets.filter((t) => codeNum(t.code) <= codeNum(state.info.code));
      if (!allowed.some((t) => t.year === state.target) && allowed.length) {
        state.target = allowed[allowed.length - 1].year;
      }
    }
    renderTargets();
    const hint = $("target-hint");
    if (state.info) {
      hint.hidden = false;
      hint.textContent = `Your file: ${state.info.label} format.`;
    } else {
      hint.hidden = true;
    }

    const dwgOk = cfg.formats.includes("DWG");
    const dwgInput = document.querySelector('#format-chips input[value="DWG"]');
    dwgInput.disabled = !dwgOk;
    if (!dwgOk && state.format === "DWG") state.format = "DXF";
    for (const input of document.querySelectorAll('#format-chips input')) input.checked = input.value === state.format;
    const fhint = $("format-hint");
    fhint.hidden = dwgOk;
    fhint.textContent = dwgOk ? "" : "DWG output needs ODA File Converter on the server, so files are saved as DXF.";

    $("convert-btn").textContent = `Convert to ${state.target}`;
  }

  function setDropError(message, tag = "Can't use this file") {
    const box = $("drop-error");
    if (!message) {
      box.hidden = true;
      return;
    }
    $("drop-error-tag").textContent = tag;
    $("drop-error-msg").textContent = message;
    box.hidden = false;
  }

  function renderPicked() {
    const drop = $("drop");
    const hasFile = !!(state.file && state.info);
    drop.dataset.state = hasFile ? "file" : "empty";
    $("choose-btn").hidden = hasFile;
    $("convert-btn").hidden = !hasFile;
    $("rechoose-btn").hidden = !hasFile;
    if (hasFile) {
      $("pick-badge").textContent = state.info.kind;
      $("pick-name").textContent = state.file.name;
      $("pick-meta").textContent = `${state.info.label} · ${formatSize(state.file.size)}`;
      const allowed = state.config.targets.some((t) => codeNum(t.code) <= codeNum(state.info.code));
      $("convert-btn").disabled = !allowed;
      if (!allowed) setDropError("This file is already older than AutoCAD 2000, the oldest version offered.", "Nothing to convert");
    }
    syncOptions();
  }

  async function pickFile(file) {
    setDropError(null);
    state.file = null;
    state.info = null;
    if (!file) return renderPicked();
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    const maxMB = state.config.maxUploadMB;
    if (ext !== "dwg" && ext !== "dxf") {
      renderPicked();
      return setDropError("Only .dwg and .dxf files can be converted.", "Unsupported file type");
    }
    if (file.size > maxMB * 1024 * 1024) {
      renderPicked();
      return setDropError(`That file is ${formatSize(file.size)}. The limit is ${maxMB} MB.`, "File too large");
    }
    if (file.size === 0) {
      renderPicked();
      return setDropError("That file is empty.");
    }
    try {
      const form = new FormData();
      form.append("file", file.slice(0, 64 * 1024), file.name);
      const info = await api("/api/detect", { method: "POST", body: form });
      if (info.kind === "DWG" && !state.config.canReadDwg) {
        renderPicked();
        return setDropError("This server can't read DWG files yet. Upload a DXF instead.", "DWG not available");
      }
      if (!info.code || info.label === "Unknown version") {
        renderPicked();
        return setDropError("This file's AutoCAD version isn't recognised.", "Unknown version");
      }
      state.file = file;
      state.info = info;
    } catch (err) {
      renderPicked();
      return setDropError(err.status ? err.message : "Couldn't reach the server. Check your connection and try again.");
    }
    renderPicked();
    $("convert-btn").focus();
  }

  function wireUpload() {
    const input = $("file-input");
    const drop = $("drop");
    $("choose-btn").addEventListener("click", () => input.click());
    $("rechoose-btn").addEventListener("click", () => input.click());
    input.addEventListener("change", () => {
      pickFile(input.files[0]);
      input.value = "";
    });

    // Drop anywhere on the upload screen; the drop zone lights up.
    let depth = 0;
    const screen = $("screen-upload");
    screen.addEventListener("dragenter", (e) => {
      if (!e.dataTransfer || !Array.from(e.dataTransfer.types).includes("Files")) return;
      e.preventDefault();
      depth++;
      drop.classList.add("is-over");
    });
    screen.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    });
    screen.addEventListener("dragleave", () => {
      depth = Math.max(0, depth - 1);
      if (!depth) drop.classList.remove("is-over");
    });
    screen.addEventListener("drop", (e) => {
      e.preventDefault();
      depth = 0;
      drop.classList.remove("is-over");
      const file = e.dataTransfer.files[0];
      if (file) pickFile(file);
    });
    // Don't let a missed drop navigate away from the app.
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("drop", (e) => e.preventDefault());

    $("target-chips").addEventListener("change", (e) => {
      state.target = Number(e.target.value);
      $("convert-btn").textContent = `Convert to ${state.target}`;
    });
    $("format-chips").addEventListener("change", (e) => {
      state.format = e.target.value;
    });
    $("convert-btn").addEventListener("click", startConversion);
  }

  // ── converting screen ───────────────────────────────────────────────────
  function stepLabels(current, job) {
    const n = job && job.layersAndBlocks;
    const skipped = job ? job.skippedSoFar : 0;
    return [
      current > 0 ? "Uploaded" : "Uploading file",
      "Reading file structure",
      n != null ? `Converting ${n.toLocaleString()} layers and blocks` : "Converting layers and blocks",
      `Writing ${state.target} file` + (skipped ? ` · ${plural(skipped, "item")} skipped so far` : ""),
      "Building report",
    ];
  }

  const CHECK =
    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';

  function renderSteps(current, job) {
    const list = $("steps");
    const labels = stepLabels(current, job);
    list.textContent = "";
    labels.forEach((text, i) => {
      const li = document.createElement("li");
      li.className = i < current ? "done" : i === current ? "current" : "pending";
      const mark = document.createElement("span");
      mark.className = "mark";
      mark.setAttribute("aria-hidden", "true");
      if (i < current) mark.innerHTML = CHECK;
      const label = document.createElement("span");
      label.textContent = text;
      if (i === current) li.setAttribute("aria-current", "step");
      li.append(mark, label);
      list.append(li);
    });
  }

  function setProgress(pct, etaText) {
    const p = Math.max(0, Math.min(100, pct));
    $("bar-fill").style.width = `${p}%`;
    $("bar").setAttribute("aria-valuenow", String(Math.round(p)));
    $("bar-pct").textContent = `${Math.floor(p)}%`;
    $("bar-eta").textContent = etaText;
  }

  function renderConvertingHeader() {
    const { file, info, target } = state;
    $("conv-badge").textContent = info.kind;
    $("conv-name").textContent = file.name;
    $("conv-meta").textContent = `${info.label} · ${formatSize(file.size)}`;
    $("conv-target").textContent = `${target} ${state.format}`;
    $("converting-title").textContent = `Converting to ${target}…`;
    $("conv-lede").textContent =
      file.size > 50 * 1024 * 1024
        ? "Rewriting blocks and layers. Big drawings can take a few minutes."
        : "Rewriting blocks and layers. This usually takes under a minute.";
  }

  function startConversion() {
    if (!state.file || !state.info) return;
    setDropError(null);
    state.job = null;
    state.pollFailures = 0;
    renderConvertingHeader();
    renderSteps(0, null);
    setProgress(0, "Uploading…");
    show("converting");
    $("cancel-btn").focus();

    const form = new FormData();
    form.append("file", state.file, state.file.name);
    form.append("target", String(state.target));
    form.append("format", state.format);

    const xhr = new XMLHttpRequest();
    state.xhr = xhr;
    state.uploadStarted = performance.now();
    xhr.open("POST", "/api/jobs");
    xhr.responseType = "json";
    xhr.upload.addEventListener("progress", (e) => {
      if (!e.lengthComputable) return;
      const frac = e.loaded / e.total;
      const elapsed = (performance.now() - state.uploadStarted) / 1000;
      const eta = frac > 0.02 ? (elapsed * (1 - frac)) / frac : null;
      setProgress(frac * UPLOAD_SHARE, eta != null ? `Uploading · ${formatEta(eta).toLowerCase()}` : "Uploading…");
    });
    xhr.addEventListener("load", () => {
      state.xhr = null;
      const body = xhr.response;
      if (xhr.status === 201 && body) {
        state.job = body;
        setProgress(UPLOAD_SHARE, "Starting…");
        renderSteps(1, body);
        poll();
      } else {
        const msg = (body && body.error && body.error.message) || `The server rejected the upload (${xhr.status}).`;
        fail(msg, errorTag(body && body.error && body.error.code));
      }
    });
    xhr.addEventListener("error", () => {
      state.xhr = null;
      fail("The upload was interrupted. Check your connection and try again.", "Network problem");
    });
    xhr.send(form);
  }

  function errorTag(code) {
    return (
      {
        unsupported_type: "Unsupported file type",
        too_large: "File too large",
        corrupt: "File is damaged",
        no_engine: "Not available",
        already_older: "Nothing to convert",
        engine_failed: "Conversion failed",
        write_failed: "Conversion failed",
      }[code] || "Conversion failed"
    );
  }

  function poll() {
    clearTimeout(state.pollTimer);
    if (!state.job || state.step !== "converting") return;
    state.pollTimer = setTimeout(async () => {
      let job;
      try {
        job = await api(`/api/jobs/${state.job.id}`);
        state.pollFailures = 0;
      } catch (err) {
        if (err.status === 404) return fail("This conversion expired. Please try again.");
        if (++state.pollFailures >= 6) {
          return fail("Lost connection to the server. Check your connection and try again.", "Network problem");
        }
        return poll();
      }
      if (state.step !== "converting") return;
      state.job = job;
      if (job.status === "done") return finish(job);
      if (job.status === "failed") return fail(job.error ? job.error.message : "The conversion failed.", errorTag(job.error && job.error.code));
      if (job.status === "cancelled") return backToUpload();
      const pct = UPLOAD_SHARE + (job.progress * (100 - UPLOAD_SHARE)) / 100;
      const eta = job.status === "queued" ? "Waiting for a free converter…" : formatEta(job.etaSeconds) || "Working…";
      setProgress(pct, eta);
      renderSteps(job.status === "queued" ? 1 : job.step, job);
      poll();
    }, POLL_MS);
  }

  function stopWork() {
    clearTimeout(state.pollTimer);
    if (state.xhr) {
      state.xhr.abort();
      state.xhr = null;
    }
  }

  function cancel() {
    stopWork();
    if (state.job && state.job.id) {
      // Fire and forget: the server stops the job and deletes the files.
      fetch(`/api/jobs/${state.job.id}`, { method: "DELETE" }).catch(() => {});
    }
    state.job = null;
    backToUpload();
  }

  function backToUpload() {
    show("upload");
    renderPicked();
  }

  function fail(message, tag) {
    stopWork();
    state.job = null;
    show("error");
    renderPicked();
    setDropError(message, tag || "Conversion failed");
    $("drop").scrollIntoView({ block: "center" });
  }

  // ── done screen ─────────────────────────────────────────────────────────
  function finish(job) {
    const r = job.result;
    setProgress(100, "Done");
    renderSteps(5, job);
    show("done");
    $("done-title").textContent = `Your ${job.target.year} file is ready.`;
    $("done-meta").textContent = `${r.outputName} · ${job.file.short} → ${job.target.year} · ${formatSize(r.outputSize)}`;
    const dl = $("download-btn");
    dl.href = r.downloadUrl;
    dl.setAttribute("download", r.outputName);
    dl.textContent = `Download ${job.format}`;
    $("report-dl").href = r.reportUrl;
    const minutes = state.config.retentionMinutes;
    $("expiry-note").textContent =
      minutes >= 60 && minutes % 60 === 0
        ? `Files are deleted after ${plural(minutes / 60, "hour")}.`
        : `Files are deleted after ${plural(minutes, "minute")}.`;
    state.showAllReport = false;
    renderReport(job);
    dl.focus();
  }

  const REPORT_LIMIT = 6;

  function renderReport(job) {
    const r = job.result;
    const { skipped, changed, repaired } = r.counts;
    const tag = $("report-tag");
    const intro = $("report-intro");
    if (skipped) {
      tag.className = "tag tag-accent";
      tag.textContent = `${skipped.toLocaleString()} skipped`;
      intro.textContent = "Everything else converted without issues. Skipped items were ignored and are listed here.";
    } else if (r.items.length) {
      tag.className = "tag tag-accent-2";
      tag.textContent = "Nothing skipped";
      intro.textContent = "Every object made it across. A few things were adjusted to fit the older format.";
    } else {
      tag.className = "tag tag-accent-2";
      tag.textContent = "No issues";
      intro.textContent = "Everything converted as-is. There was nothing to skip or repair.";
    }
    if (repaired && !skipped && !changed) {
      intro.textContent = "Every object made it across. Some errors in the original drawing were repaired on the way.";
    }

    const list = $("report-list");
    list.textContent = "";
    const items = state.showAllReport ? r.items : r.items.slice(0, REPORT_LIMIT);
    for (const it of items) {
      const li = document.createElement("li");
      li.className = it.kind;
      const b = document.createElement("b");
      b.textContent = it.entity;
      li.append(b);
      if (it.count > 1 && it.entity !== "Transparency" && it.entity !== "True color") {
        const c = document.createElement("span");
        c.className = "count";
        c.textContent = ` ×${it.count.toLocaleString()}`;
        li.append(c);
      }
      li.append(document.createTextNode(` · ${it.where}`));
      const reason = document.createElement("div");
      reason.className = "reason";
      reason.textContent = it.reason;
      li.append(reason);
      list.append(li);
    }
    const more = $("report-more");
    const hiddenCount = r.items.length - items.length;
    more.hidden = hiddenCount <= 0;
    more.textContent = `Show ${hiddenCount} more`;
    $("report-dl").hidden = false;
  }

  // ── boot ────────────────────────────────────────────────────────────────
  async function boot() {
    wireUpload();
    $("cancel-btn").addEventListener("click", cancel);
    $("again-btn").addEventListener("click", () => {
      state.file = null;
      state.info = null;
      state.job = null;
      setDropError(null);
      backToUpload();
      $("choose-btn").focus();
    });
    $("report-more").addEventListener("click", () => {
      state.showAllReport = true;
      if (state.job) renderReport(state.job);
    });

    try {
      state.config = await api("/api/config");
    } catch {
      state.config = { targets: [], formats: [], maxUploadMB: 200, retentionMinutes: 60, canReadDwg: false, defaultTarget: 2010 };
      setDropError("Couldn't reach the conversion server. Refresh the page to try again.", "Server offline");
      $("choose-btn").disabled = true;
    }
    state.target = state.config.defaultTarget;
    $("drop-hint").textContent = `.dwg or .dxf, up to ${state.config.maxUploadMB} MB`;
    renderPicked();
  }

  boot();
})();
