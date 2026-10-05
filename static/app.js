// Backdate.dwg front end: upload -> converting -> download, one screen at a time.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const UPLOAD_SHARE = 20; // % of the bar used by the upload itself
  const POLL_MS = 500;
  // Where the conversion server lives. Empty = same origin (Docker deploy).
  // The static (Vercel) build sets it in config.js to a separately hosted server.
  const API = String(window.BACKDATE_API || "").replace(/\/+$/, "");
  const SERVER_WAIT_MS = 4 * 60 * 1000; // free hosts can take a minute or two to wake up

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
    mode: "server", // "server" (API available) | "browser" (static hosting, convert locally)
    worker: null,
    blobUrls: [],
    anim: null,
    serverWaking: false,
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

  // AutoCAD format versions (mirrors app/versions.py).
  const VERSIONS = {
    AC1009: ["AutoCAD R11/R12", "R12"],
    AC1012: ["AutoCAD R13", "R13"],
    AC1014: ["AutoCAD R14", "R14"],
    AC1015: ["AutoCAD 2000–2002", "2000"],
    AC1018: ["AutoCAD 2004–2006", "2004"],
    AC1021: ["AutoCAD 2007–2009", "2007"],
    AC1024: ["AutoCAD 2010–2012", "2010"],
    AC1027: ["AutoCAD 2013–2017", "2013"],
    AC1032: ["AutoCAD 2018–2026", "2018+"],
  };
  const BROWSER_TARGETS = [
    [2000, "AC1015"], [2004, "AC1018"], [2007, "AC1021"],
    [2010, "AC1024"], [2013, "AC1027"], [2018, "AC1032"],
  ].map(([year, code]) => ({ year, code, label: VERSIONS[code][0] }));

  // Identify a DWG/DXF from its first bytes (mirrors app/versions.detect).
  function detectHead(bytes, name) {
    const ascii = (a, b) => String.fromCharCode(...bytes.subarray(a, b));
    const info = (kind, code) => {
      const v = VERSIONS[code];
      return { kind, code: v ? code : null, label: v ? v[0] : "Unknown version", short: v ? v[1] : "?" };
    };
    if (/^AC\d{4}$/.test(ascii(0, 6))) return info("DWG", ascii(0, 6));
    if (/^AC[12]\./.test(ascii(0, 4))) return info("DWG", null);
    const text = new TextDecoder("latin1").decode(bytes);
    if (text.startsWith("AutoCAD Binary DXF")) {
      const m = /\$ACADVER[\s\S]{0,8}?(AC\d{4})/.exec(text);
      return info("DXF", m ? m[1] : null);
    }
    const m = /\$ACADVER\s*\r?\n\s*1\s*\r?\n\s*(AC\d{4})/.exec(text);
    if (m) return info("DXF", m[1]);
    if (/\.dxf$/i.test(name) && /^\s*0\s*\r?\n\s*SECTION/m.test(text)) return info("DXF", "AC1009");
    return null;
  }

  function show(step) {
    state.step = step;
    const screen = step === "error" ? "upload" : step;
    for (const s of ["upload", "converting", "done"]) $(`screen-${s}`).hidden = s !== screen;
    window.scrollTo({ top: 0 });
  }

  async function api(path, options = {}) {
    const res = await fetch(API + path, options);
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
    fhint.textContent = dwgOk
      ? ""
      : state.mode === "browser" && state.serverWaking
        ? "Waking up the DWG converter… this takes up to a minute. You can convert to DXF right away."
        : state.mode === "browser"
        ? "The DWG converter isn't reachable right now, so files are saved as DXF, which opens in any AutoCAD."
        : "DWG output needs ODA File Converter on the server, so files are saved as DXF.";

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
      const head = new Uint8Array(await file.slice(0, 64 * 1024).arrayBuffer());
      const info = detectHead(head, file.name);
      if (!info) {
        renderPicked();
        return setDropError("This doesn't look like a DWG or DXF file.", "Unsupported file type");
      }
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
      return setDropError(`Couldn't read that file (${err.message}).`);
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
      state.mode === "browser"
        ? current > 0 ? "Converter ready" : "Loading converter (first time only)"
        : current > 0 ? "Uploaded" : "Uploading file",
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
    if (state.mode === "browser") return startInBrowser();
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
    xhr.open("POST", API + "/api/jobs");
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

  // ── in-browser conversion (static hosting, e.g. Vercel) ──────────────────
  function startInBrowser() {
    setDropError(null);
    state.job = {
      id: null,
      file: { name: state.file.name, size: state.file.size, ...state.info },
      target: { year: state.target },
      format: "DXF",
      layersAndBlocks: null,
      skippedSoFar: 0,
    };
    renderConvertingHeader();
    renderSteps(0, state.job);
    setProgress(0, "Loading the converter…");
    show("converting");
    $("cancel-btn").focus();

    if (!state.worker) {
      state.worker = new Worker("browser/worker.js", { type: "module" });
    }
    const worker = state.worker;
    const job = state.job;
    const started = performance.now();
    let stage = { index: 0, lo: 0, hi: 0, expected: 1, at: started };
    let loadPct = 0;

    const tick = () => {
      if (state.step !== "converting" || state.job !== job) return;
      let pct;
      if (stage.index === 0) {
        // Loading happens in a few big steps; creep between them so the bar keeps moving.
        const creep = 90 * (1 - Math.exp(-(performance.now() - started) / 8000));
        pct = (Math.max(loadPct, creep) / 100) * UPLOAD_SHARE;
      } else {
        const t = (performance.now() - stage.at) / 1000;
        const inner = stage.lo + (stage.hi - stage.lo) * 0.95 * (1 - Math.exp(-t / stage.expected));
        pct = UPLOAD_SHARE + (inner * (100 - UPLOAD_SHARE)) / 100;
      }
      const elapsed = (performance.now() - started) / 1000;
      const eta = stage.index === 0 ? "Loading the converter…" : pct > 25 ? formatEta((elapsed * (100 - pct)) / pct) : "Working…";
      setProgress(pct, eta);
    };
    clearInterval(state.anim);
    state.anim = setInterval(tick, 250);

    worker.onmessage = (e) => {
      const msg = e.data;
      if (state.job !== job) return;
      if (msg.type === "load") {
        loadPct = msg.pct;
      } else if (msg.type === "py") {
        if (msg.kind === "stage") {
          stage = { ...msg.value, at: performance.now() };
          renderSteps(stage.index, job);
        } else if (msg.kind === "layers") {
          job.layersAndBlocks = msg.value;
          renderSteps(stage.index, job);
        } else if (msg.kind === "skipped") {
          job.skippedSoFar = msg.value;
          renderSteps(stage.index, job);
        }
      } else if (msg.type === "done") {
        clearInterval(state.anim);
        const out = new Blob([msg.output], { type: "application/dxf" });
        const txt = new Blob([msg.result.reportText], { type: "text/plain" });
        const result = { ...msg.result, downloadUrl: URL.createObjectURL(out), reportUrl: URL.createObjectURL(txt) };
        state.blobUrls.push(result.downloadUrl, result.reportUrl);
        finish({ ...job, status: "done", result });
      } else if (msg.type === "error") {
        fail(msg.error.message, errorTag(msg.error.code));
      }
    };
    worker.onerror = (e) => {
      e.preventDefault();
      state.worker = null;
      worker.terminate();
      fail("The in-browser converter couldn't start. Try a recent version of Chrome, Edge, Firefox or Safari.");
    };

    state.file.arrayBuffer().then(
      (buffer) => worker.postMessage({ buffer, name: state.file.name, target: state.target }, [buffer]),
      () => fail("Couldn't read that file from your device."),
    );
  }

  function stopWork() {
    clearTimeout(state.pollTimer);
    clearInterval(state.anim);
    if (state.xhr) {
      state.xhr.abort();
      state.xhr = null;
    }
  }

  function cancel() {
    stopWork();
    if (state.mode === "browser" && state.worker) {
      // Stop the conversion mid-flight; the next one starts a fresh worker.
      state.worker.terminate();
      state.worker = null;
    }
    if (state.job && state.job.id) {
      // Fire and forget: the server stops the job and deletes the files.
      fetch(`${API}/api/jobs/${state.job.id}`, { method: "DELETE" }).catch(() => {});
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
    dl.href = r.downloadUrl.startsWith("blob:") ? r.downloadUrl : API + r.downloadUrl;
    dl.setAttribute("download", r.outputName);
    dl.textContent = `Download ${job.format}`;
    $("report-dl").href = r.reportUrl.startsWith("blob:") ? r.reportUrl : API + r.reportUrl;
    const minutes = state.config.retentionMinutes;
    $("expiry-note").textContent = state.mode === "browser"
      ? "Converted on your device. Your file was never uploaded."
      : minutes >= 60 && minutes % 60 === 0
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
      for (const url of state.blobUrls) URL.revokeObjectURL(url);
      state.blobUrls = [];
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
      state.config = await fetchConfig(API ? 8000 : 4000);
    } catch {
      // No conversion server reachable (yet): convert in the browser for now.
      useBrowserMode();
      // A separately hosted server may just be asleep: keep trying, and switch
      // to it (with DWG output) as soon as it answers.
      if (API) waitForServer();
    }
    state.target = state.config.defaultTarget;
    $("drop-hint").textContent = `.dwg or .dxf, up to ${state.config.maxUploadMB} MB`;
    renderPicked();
  }

  async function fetchConfig(timeoutMs) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const cfg = await api("/api/config", { signal: ctrl.signal });
      if (!cfg || !Array.isArray(cfg.targets)) throw new Error("not a Backdate server");
      return cfg;
    } finally {
      clearTimeout(timer);
    }
  }

  function useBrowserMode() {
    state.mode = "browser";
    state.config = {
      targets: BROWSER_TARGETS,
      formats: ["DXF"],
      maxUploadMB: 200,
      retentionMinutes: 0,
      canReadDwg: typeof WebAssembly === "object",
      defaultTarget: 2010,
    };
    state.serverWaking = !!API;
    $("free-tag").textContent = "Free · no sign-up";
  }

  async function waitForServer() {
    const until = Date.now() + SERVER_WAIT_MS;
    while (Date.now() < until) {
      try {
        const cfg = await fetchConfig(20000);
        if (state.step === "converting") {
          // Don't switch engines mid-conversion; try again shortly.
          await new Promise((r) => setTimeout(r, 3000));
          continue;
        }
        state.mode = "server";
        state.serverWaking = false;
        state.config = cfg;
        if (cfg.formats.includes("DWG")) state.format = "DWG";
        $("drop-hint").textContent = `.dwg or .dxf, up to ${cfg.maxUploadMB} MB`;
        if (state.step === "upload" || state.step === "error") renderPicked();
        return;
      } catch {
        await new Promise((r) => setTimeout(r, 5000));
      }
    }
    state.serverWaking = false;
    if (state.step === "upload" || state.step === "error") syncOptions();
  }

  boot();
})();
