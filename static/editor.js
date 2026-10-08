// Backdate.dwg AI editor: a drawing on the left, a conversation on the right.
// Edits arrive as proposals. They are previewed on the drawing and only applied when accepted.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const API = String(window.BACKDATE_API || "").replace(/\/+$/, "");
  const INK = "#2e2b25";
  const ACCENT = "#c67139";
  const DEL = "#c2410c";
  const ADD = "#3d7a3a";
  const DOC_YEAR = { AC1032: 2018, AC1027: 2013, AC1024: 2010, AC1021: 2007, AC1018: 2004, AC1015: 2000 };

  const S = {
    config: null,
    session: null, // summary from the server
    scene: null, // { items, groups, texts, byHandle, hbox, extents }
    view: { cx: 0, cy: 0, scale: 1 },
    hidden: new Set(), // layers hidden in the viewer
    selected: new Set(),
    proposal: null, // pending proposal view (drives the preview overlay)
    overlay: null, // { remove: Set, add: [items], b: bbox }
    pending: false,
    hover: null,
    boxMode: false,
    W: 0,
    H: 0,
    dpr: 1,
    dirty: false,
    marquee: null,
  };

  const cv = $("cv");
  const ctx = cv.getContext("2d");

  // ── helpers ─────────────────────────────────────────────────────────────
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function store(key, value) {
    try {
      if (value === undefined) return sessionStorage.getItem(key);
      if (value === null) sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, value);
    } catch (e) { /* storage can be blocked; the editor works without it */ }
    return null;
  }
  let toastTimer = 0;
  function toast(text) {
    const t = $("toast");
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 4500);
  }
  async function api(path, opts) {
    let res;
    try { res = await fetch(API + path, opts); } catch (e) { throw new Error("Can't reach the server. Check your connection and try again."); }
    if (res.status === 204) return null;
    let body = null;
    try { body = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) throw new Error((body && body.error && body.error.message) || (body && body.detail ? "That request wasn't valid." : `The server returned an error (${res.status}).`));
    return body;
  }
  const post = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });
  const sid = () => S.session && S.session.id;
  const fmt = (n) => Number(n).toLocaleString(undefined, { maximumFractionDigits: 1 });

  // ── scene ───────────────────────────────────────────────────────────────
  const inkCache = new Map();
  function inkFor(c) {
    if (!c) return INK;
    let v = inkCache.get(c);
    if (v) return v;
    const r = parseInt(c.slice(1, 3), 16), g = parseInt(c.slice(3, 5), 16), b = parseInt(c.slice(5, 7), 16);
    const l = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
    if (l > 0.6) { // pale colours vanish on paper: pull them toward the ink
      const k = Math.min(0.75, (l - 0.5) * 1.6);
      const mix = (a, i) => Math.round(a * (1 - k) + i * k);
      v = `rgb(${mix(r, 0x2e)},${mix(g, 0x2b)},${mix(b, 0x25)})`;
    } else v = c;
    inkCache.set(c, v);
    return v;
  }

  function prep(it) {
    if (it.k === "p") {
      const p = it.p;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (let i = 0; i < p.length; i += 2) {
        const x = p[i], y = p[i + 1];
        if (x < x0) x0 = x; if (x > x1) x1 = x;
        if (y < y0) y0 = y; if (y > y1) y1 = y;
      }
      it.b = [x0, y0, x1, y1];
    } else {
      const w = Math.min(it.v.length, 60) * it.s * 0.7 + it.s * 2;
      it.b = [it.x - w, it.y - w, it.x + w, it.y + w];
    }
    return it;
  }

  function buildScene(geo) {
    const groups = new Map();
    const texts = [];
    const byHandle = new Map();
    const hbox = new Map();
    for (const it of geo.items) {
      prep(it);
      if (it.k === "p") {
        const key = inkFor(it.c);
        let g = groups.get(key);
        if (!g) groups.set(key, (g = []));
        g.push(it);
      } else texts.push(it);
      let list = byHandle.get(it.h);
      if (!list) { byHandle.set(it.h, (list = [])); hbox.set(it.h, it.b.slice()); }
      else { const hb = hbox.get(it.h); hb[0] = Math.min(hb[0], it.b[0]); hb[1] = Math.min(hb[1], it.b[1]); hb[2] = Math.max(hb[2], it.b[2]); hb[3] = Math.max(hb[3], it.b[3]); }
      list.push(it);
    }
    return { items: geo.items, groups, texts, byHandle, hbox, extents: geo.extents, truncated: geo.truncated, notShown: geo.notShown || {} };
  }

  async function loadGeometry({ fit }) {
    const geo = await api(`/api/editor/sessions/${sid()}/geometry`);
    S.scene = buildScene(geo);
    for (const h of [...S.selected]) if (!S.scene.byHandle.has(h)) S.selected.delete(h);
    if (fit || !S.viewSet) fitView();
    updateSelection();
    requestDraw();
  }

  // ── drawing ─────────────────────────────────────────────────────────────
  function resize() {
    const r = cv.getBoundingClientRect();
    S.dpr = window.devicePixelRatio || 1;
    S.W = Math.max(1, Math.round(r.width));
    S.H = Math.max(1, Math.round(r.height));
    cv.width = Math.round(S.W * S.dpr);
    cv.height = Math.round(S.H * S.dpr);
    requestDraw();
  }
  function requestDraw() {
    if (S.dirty) return;
    S.dirty = true;
    requestAnimationFrame(() => { S.dirty = false; draw(); });
  }
  const sx = (x) => (x - S.view.cx) * S.view.scale + S.W / 2;
  const sy = (y) => S.H / 2 - (y - S.view.cy) * S.view.scale;
  const wx = (px) => (px - S.W / 2) / S.view.scale + S.view.cx;
  const wy = (py) => S.view.cy - (py - S.H / 2) / S.view.scale;

  function fitTo(b, pad) {
    const w = Math.max(b[2] - b[0], 1e-6), h = Math.max(b[3] - b[1], 1e-6);
    const k = 1 + 2 * (pad == null ? 0.06 : pad);
    S.view.scale = Math.min(S.W / (w * k), S.H / (h * k));
    if (!isFinite(S.view.scale) || S.view.scale <= 0) S.view.scale = 1;
    S.view.cx = (b[0] + b[2]) / 2;
    S.view.cy = (b[1] + b[3]) / 2;
    S.viewSet = true;
  }
  function fitView() {
    const e = S.scene && S.scene.extents;
    if (e) fitTo(e); else { S.view = { cx: 0, cy: 0, scale: 1 }; S.viewSet = true; }
    requestDraw();
  }
  function viewport() {
    return [wx(0), wy(S.H), wx(S.W), wy(0)];
  }
  const overlaps = (a, b) => !(a[2] < b[0] || a[0] > b[2] || a[3] < b[1] || a[1] > b[3]);

  function trace(it) {
    const p = it.p;
    ctx.moveTo(sx(p[0]), sy(p[1]));
    for (let i = 2; i < p.length; i += 2) ctx.lineTo(sx(p[i]), sy(p[i + 1]));
    if (it.z) ctx.closePath();
  }
  function visible(it, vp, minPx) {
    if (S.hidden.has(it.l)) return false;
    const b = it.b;
    if (b[2] < vp[0] || b[0] > vp[2] || b[3] < vp[1] || b[1] > vp[3]) return false;
    return it.k !== "p" || (b[2] - b[0]) * S.view.scale >= minPx || (b[3] - b[1]) * S.view.scale >= minPx;
  }
  function drawText(it, color) {
    const px = it.s * S.view.scale;
    if (px < 4) return;
    ctx.save();
    ctx.translate(sx(it.x), sy(it.y));
    if (it.r) ctx.rotate((-it.r * Math.PI) / 180);
    ctx.fillStyle = color;
    ctx.font = `${Math.min(px, 400)}px Figtree, system-ui, sans-serif`;
    const lines = it.v.split("\n").slice(0, 12);
    lines.forEach((ln, i) => ctx.fillText(ln.slice(0, 120), 0, i * px * 1.3));
    ctx.restore();
  }

  function draw() {
    ctx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
    ctx.fillStyle = "#fffaf0";
    ctx.fillRect(0, 0, S.W, S.H);
    const sc = S.scene;
    if (!sc) return;
    const vp = viewport();
    const removed = S.overlay ? S.overlay.remove : null;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.lineWidth = 1;

    for (const [color, list] of sc.groups) {
      ctx.strokeStyle = color;
      ctx.beginPath();
      for (const it of list) {
        if (S.selected.has(it.h) || (removed && removed.has(it.h)) || !visible(it, vp, 0.6)) continue;
        trace(it);
      }
      ctx.stroke();
    }
    ctx.textBaseline = "alphabetic";
    for (const it of sc.texts) {
      if (S.selected.has(it.h) || (removed && removed.has(it.h)) || !visible(it, vp, 0)) continue;
      drawText(it, inkFor(it.c));
    }

    if (removed) {
      ctx.strokeStyle = DEL; ctx.globalAlpha = 0.55; ctx.lineWidth = 2; ctx.setLineDash([6, 4]);
      ctx.beginPath();
      for (const h of removed) for (const it of sc.byHandle.get(h) || []) if (it.k === "p" && visible(it, vp, 0.3)) trace(it);
      ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha = 1;
      for (const h of removed) for (const it of sc.byHandle.get(h) || []) if (it.k === "t" && visible(it, vp, 0)) drawText(it, DEL);
      ctx.strokeStyle = ADD; ctx.lineWidth = 2;
      ctx.beginPath();
      for (const it of S.overlay.add) if (it.k === "p" && visible({ ...it, l: "" }, vp, 0.3)) trace(it);
      ctx.stroke();
      for (const it of S.overlay.add) if (it.k === "t") drawText(it, ADD);
    }

    if (S.selected.size) {
      ctx.strokeStyle = ACCENT; ctx.lineWidth = 2.5;
      ctx.beginPath();
      for (const h of S.selected) for (const it of sc.byHandle.get(h) || []) if (it.k === "p" && !S.hidden.has(it.l)) trace(it);
      ctx.stroke();
      for (const h of S.selected) for (const it of sc.byHandle.get(h) || []) if (it.k === "t" && !S.hidden.has(it.l)) drawText(it, ACCENT);
    }

    if (S.marquee) {
      const m = S.marquee, crossing = m.x1 < m.x0;
      const x = Math.min(m.x0, m.x1), y = Math.min(m.y0, m.y1), w = Math.abs(m.x1 - m.x0), h = Math.abs(m.y1 - m.y0);
      ctx.lineWidth = 1.25; ctx.setLineDash(crossing ? [5, 4] : []);
      ctx.fillStyle = "rgba(198,113,57,0.10)"; ctx.strokeStyle = ACCENT;
      ctx.fillRect(x, y, w, h); ctx.strokeRect(x, y, w, h); ctx.setLineDash([]);
    }
  }

  // ── picking & selection ────────────────────────────────────────────────
  function distSeg(px, py, ax, ay, bx, by) {
    const dx = bx - ax, dy = by - ay;
    const l2 = dx * dx + dy * dy;
    let t = l2 ? ((px - ax) * dx + (py - ay) * dy) / l2 : 0;
    t = t < 0 ? 0 : t > 1 ? 1 : t;
    return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
  }
  function pick(x, y, tolPx) {
    if (!S.scene) return null;
    const tol = (tolPx || 7) / S.view.scale;
    let best = null, bd = Infinity;
    for (const it of S.scene.items) {
      if (S.hidden.has(it.l)) continue;
      const b = it.b;
      if (x < b[0] - tol || x > b[2] + tol || y < b[1] - tol || y > b[3] + tol) continue;
      let d = Infinity;
      if (it.k === "p") {
        const p = it.p;
        for (let i = 0; i + 3 < p.length; i += 2) {
          d = Math.min(d, distSeg(x, y, p[i], p[i + 1], p[i + 2], p[i + 3]));
          if (d < 1e-9) break;
        }
        if (it.z && p.length >= 6) d = Math.min(d, distSeg(x, y, p[p.length - 2], p[p.length - 1], p[0], p[1]));
      } else {
        const w = Math.min(it.v.length, 60) * it.s * 0.6;
        if (x >= it.x - tol && x <= it.x + w + tol && y >= it.y - tol && y <= it.y + it.s + tol) d = 0;
      }
      if (d <= tol && d < bd) { bd = d; best = it; }
    }
    return best;
  }

  function describeHandles(handles) {
    const types = new Map(), layers = new Set();
    for (const h of handles) {
      const it = (S.scene.byHandle.get(h) || [])[0];
      if (!it) continue;
      types.set(it.t, (types.get(it.t) || 0) + 1);
      layers.add(it.l);
    }
    const ty = [...types].sort((a, b) => b[1] - a[1]).slice(0, 3).map(([t, n]) => `${t} ×${n}`).join(", ");
    const ly = layers.size === 1 ? `layer ${[...layers][0]}` : `${layers.size} layers`;
    return `${ty} · ${ly}`;
  }
  function updateSelection() {
    const n = S.selected.size;
    $("selchip").hidden = n === 0;
    $("st-sel").textContent = n ? `${fmt(n)} selected` : "";
    if (n) $("selchip-text").textContent = `${fmt(n)} selected — ${describeHandles(S.selected)}`;
    $("input").placeholder = n ? "What should happen to the selection?  e.g. “move it 2 m north”" : "Describe a change…  e.g. “move layer S-RACK 2 m east”";
  }
  function setSelection(handles, add) {
    if (!add) S.selected.clear();
    for (const h of handles) S.selected.add(h);
    updateSelection();
    requestDraw();
  }

  // ── pointer interaction ────────────────────────────────────────────────
  let drag = null;
  cv.addEventListener("pointerdown", (e) => {
    if (!S.scene || (e.button !== 0 && e.button !== 1)) return;
    cv.setPointerCapture(e.pointerId);
    cv.focus({ preventScroll: true });
    const r = cv.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    drag = { x, y, lx: x, ly: y, moved: false, box: e.button === 0 && (e.shiftKey || S.boxMode), shift: e.shiftKey || e.ctrlKey || e.metaKey };
    if (drag.box) S.marquee = { x0: x, y0: y, x1: x, y1: y };
  });
  cv.addEventListener("pointermove", (e) => {
    const r = cv.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    $("st-coords").textContent = S.scene ? `X ${fmt(wx(x))}   Y ${fmt(wy(y))}` + unitSuffix() : "";
    if (drag) {
      if (!drag.moved && Math.hypot(x - drag.x, y - drag.y) > 4) drag.moved = true;
      if (drag.box) { S.marquee.x1 = x; S.marquee.y1 = y; requestDraw(); }
      else if (drag.moved) {
        S.view.cx -= (x - drag.lx) / S.view.scale;
        S.view.cy += (y - drag.ly) / S.view.scale;
        cv.classList.add("panning");
        requestDraw();
      }
      drag.lx = x; drag.ly = y;
      return;
    }
    if (!hoverQueued) {
      hoverQueued = true;
      requestAnimationFrame(() => {
        hoverQueued = false;
        const it = pick(wx(x), wy(y), 6);
        $("st-hover").textContent = it ? `${it.t} · layer ${it.l} · handle ${it.h}` : "";
      });
    }
  });
  let hoverQueued = false;
  function unitSuffix() {
    const u = S.session && S.session.digest && S.session.digest.units;
    return u ? " " + u.short : "";
  }
  function endDrag(e) {
    if (!drag) return;
    const d = drag;
    drag = null;
    cv.classList.remove("panning");
    if (d.box) {
      const m = S.marquee;
      S.marquee = null;
      if (d.moved) {
        const b = [Math.min(wx(m.x0), wx(m.x1)), Math.min(wy(m.y0), wy(m.y1)), Math.max(wx(m.x0), wx(m.x1)), Math.max(wy(m.y0), wy(m.y1))];
        const crossing = m.x1 < m.x0; // right-to-left touches; left-to-right must enclose (like CAD)
        const hits = [];
        for (const [h, hb] of S.scene.hbox) {
          const first = S.scene.byHandle.get(h)[0];
          if (S.hidden.has(first.l)) continue;
          if (crossing ? overlaps(hb, b) : hb[0] >= b[0] && hb[2] <= b[2] && hb[1] >= b[1] && hb[3] <= b[3]) hits.push(h);
        }
        setSelection(hits, d.shift);
        return;
      }
    }
    if (!d.moved && e && e.type === "pointerup") {
      const it = pick(wx(d.x), wy(d.y), 7);
      if (it) {
        if (d.shift && S.selected.has(it.h)) { S.selected.delete(it.h); updateSelection(); requestDraw(); }
        else setSelection([it.h], d.shift);
      } else if (!d.shift) setSelection([], false);
    }
    requestDraw();
  }
  cv.addEventListener("pointerup", endDrag);
  cv.addEventListener("pointercancel", () => { drag = null; S.marquee = null; cv.classList.remove("panning"); requestDraw(); });
  cv.addEventListener("dblclick", () => fitView());
  cv.addEventListener("wheel", (e) => {
    if (!S.scene) return;
    e.preventDefault();
    const r = cv.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    const bx = wx(x), by = wy(y);
    const k = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0016));
    S.view.scale = Math.min(Math.max(S.view.scale * k, 1e-6), 1e7);
    S.view.cx = bx - (x - S.W / 2) / S.view.scale;
    S.view.cy = by + (y - S.H / 2) / S.view.scale;
    requestDraw();
  }, { passive: false });
  cv.addEventListener("keydown", (e) => {
    if (e.key === "f" || e.key === "F") fitView();
    else if (e.key === "b" || e.key === "B") toggleBox();
    else if (e.key === "Escape") { setSelection([], false); }
    else if ((e.key === "Delete" || e.key === "Backspace") && S.selected.size) { e.preventDefault(); send("delete the selection"); }
  });

  function toggleBox() {
    S.boxMode = !S.boxMode;
    $("box-btn").setAttribute("aria-pressed", String(S.boxMode));
  }
  $("box-btn").addEventListener("click", toggleBox);
  $("fit-btn").addEventListener("click", fitView);

  // ── layers popover ─────────────────────────────────────────────────────
  function renderLayers() {
    const ul = $("layers-list");
    ul.replaceChildren();
    const layers = (S.session && S.session.digest.layers) || [];
    for (const l of layers) {
      if (!l.count) continue;
      const li = el("li");
      const lab = el("label");
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = !S.hidden.has(l.name);
      cb.addEventListener("change", () => { if (cb.checked) S.hidden.delete(l.name); else S.hidden.add(l.name); requestDraw(); });
      const sw = el("span", "sw");
      sw.style.background = l.color;
      lab.append(cb, sw, el("span", "nm", l.name), el("span", "ct", fmt(l.count)));
      lab.title = l.on ? l.name : `${l.name} (hidden in the file)`;
      li.append(lab);
      ul.append(li);
    }
  }
  $("layers-btn").addEventListener("click", () => {
    const pop = $("layers-pop");
    pop.hidden = !pop.hidden;
    $("layers-btn").setAttribute("aria-expanded", String(!pop.hidden));
  });
  const setAllLayers = (show) => {
    S.hidden.clear();
    if (!show) for (const l of S.session.digest.layers) S.hidden.add(l.name);
    renderLayers(); requestDraw();
  };
  $("layers-all").addEventListener("click", () => setAllLayers(true));
  $("layers-none").addEventListener("click", () => setAllLayers(false));

  // ── chat ───────────────────────────────────────────────────────────────
  const log = $("messages");
  function scrollDown() { log.scrollTop = log.scrollHeight; }
  function addMsg(kind, text, src) {
    const m = el("div", "msg " + kind, text);
    if (src) m.append(el("span", "src", src));
    log.append(m);
    scrollDown();
    return m;
  }
  function setComposer(enabled) {
    $("input").disabled = !enabled;
    $("send-btn").disabled = !enabled;
  }
  function setPending(on) {
    S.pending = on;
    setComposer(!on && !!S.session);
    for (const b of document.querySelectorAll(".prop-actions button")) b.disabled = on;
    $("undo-btn").disabled = on || !(S.session && S.session.canUndo);
    $("redo-btn").disabled = on || !(S.session && S.session.canRedo);
  }

  async function send(text) {
    text = (text || "").trim();
    if (!text || !S.session || S.pending) return;
    addMsg("user", text);
    $("suggest").replaceChildren();
    const think = el("div", "msg bot think");
    think.append(el("span", "spinner"), el("span", null, "Thinking…"));
    log.append(think);
    scrollDown();
    setPending(true);
    try {
      const r = await post(`/api/editor/sessions/${sid()}/chat`, { message: text, selection: [...S.selected].slice(0, 5000) });
      think.remove();
      const src = r.source === "model" ? (r.queries ? `AI assistant · looked things up ${r.queries}×` : "AI assistant") : r.source === "local" ? "Built-in command" : "";
      addMsg("bot" + (r.error && !r.proposal ? " err" : ""), r.reply, src);
      if (r.proposal) showProposal(r.proposal);
      updateAiPill(r.aiCallsLeft);
    } catch (err) {
      think.remove();
      addMsg("bot err", err.message);
    } finally {
      setPending(false);
      $("input").focus();
    }
  }

  // ── proposals ──────────────────────────────────────────────────────────
  let activeCard = null;
  function showProposal(p) {
    if (activeCard) retire(activeCard, "Replaced by a newer suggestion", "no");
    S.proposal = p;
    S.overlay = { remove: new Set(p.preview.remove), add: p.preview.add.map(prep) };
    const card = el("div", "prop");
    const head = el("div", "prop-head");
    head.append(el("span", "tag tag-accent", "Proposed change"));
    const st = p.stats, bits = [];
    if (st.changed) bits.push(`${fmt(st.changed)} changed`);
    if (st.removed) bits.push(`${fmt(st.removed)} removed`);
    if (st.added) bits.push(`${fmt(st.added)} added`);
    if (st.tables && !bits.length) bits.push("layer settings");
    head.append(el("span", "prop-stats", bits.join(" · ")));
    card.append(head);
    const ul = el("ul");
    p.summaries.forEach((s) => ul.append(el("li", null, s)));
    for (const w of p.warnings) ul.append(el("li", "warn", "Heads-up: " + w));
    if (p.preview.truncated) ul.append(el("li", "warn", "The preview on the drawing is partial because the change is very large."));
    card.append(ul);
    const det = el("details");
    det.append(el("summary", null, "Show the exact operations"), el("pre", null, JSON.stringify(p.ops, null, 2)));
    card.append(det);
    const actions = el("div", "prop-actions");
    const yes = el("button", "btn btn-primary", "Accept");
    const no = el("button", "btn btn-secondary", "Reject");
    const show = el("button", "btn btn-ghost", "Show on drawing");
    yes.type = no.type = show.type = "button";
    yes.addEventListener("click", () => accept(card, p));
    no.addEventListener("click", () => reject(card, p));
    show.addEventListener("click", () => focusOverlay(true));
    actions.append(yes, no);
    if (S.overlay.remove.size || S.overlay.add.length) actions.append(show);
    card.append(actions);
    card._p = p;
    activeCard = card;
    log.append(card);
    scrollDown();
    $("legend").hidden = !(S.overlay.remove.size || S.overlay.add.length);
    focusOverlay(false);
    requestDraw();
  }
  function overlayBox() {
    const sc = S.scene, boxes = [];
    for (const h of S.overlay.remove) { const b = sc.hbox.get(h); if (b) boxes.push(b); }
    for (const it of S.overlay.add) boxes.push(it.b);
    if (!boxes.length) return null;
    return boxes.reduce((a, b) => [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[2], b[2]), Math.max(a[3], b[3])]);
  }
  function focusOverlay(force) {
    const b = S.overlay && overlayBox();
    if (!b) return;
    if (force || !overlaps(viewport(), b)) fitTo(b, 0.25);
    requestDraw();
  }
  function retire(card, text, kind) {
    card.classList.add("done");
    const actions = card.querySelector(".prop-actions");
    if (actions) actions.replaceWith(el("div", "prop-state " + kind, text));
    if (activeCard === card) { activeCard = null; S.proposal = null; S.overlay = null; $("legend").hidden = true; }
    requestDraw();
  }
  async function accept(card, p) {
    setPending(true);
    try {
      const r = await post(`/api/editor/sessions/${sid()}/proposals/${p.id}/accept`);
      retire(card, `Applied (change ${r.summary.rev})`, "ok");
      await adopt(r.summary, { fit: false });
      toast("Change applied. You can undo it.");
    } catch (err) {
      addMsg("bot err", err.message);
      retire(card, "Couldn't be applied", "no");
    } finally { setPending(false); }
  }
  async function reject(card, p) {
    setPending(true);
    try { await post(`/api/editor/sessions/${sid()}/proposals/${p.id}/reject`); } catch (e) { /* the preview is dropped either way */ }
    retire(card, "Rejected — nothing changed", "no");
    setPending(false);
  }

  // ── session lifecycle ──────────────────────────────────────────────────
  async function adopt(summary, { fit }) {
    S.session = summary;
    $("file-name").textContent = summary.name;
    $("file-name").title = summary.name;
    renderLayers();
    renderChanges();
    setPending(S.pending);
    await loadGeometry({ fit });
  }

  function suggestions() {
    const s = ["What's in this drawing?", "List layers", "Purge unused layers"];
    if (S.config && S.config.ai.enabled) s.push("Find anything unusual or messy in this drawing");
    return s;
  }
  function renderSuggestions() {
    const box = $("suggest");
    box.replaceChildren();
    for (const t of suggestions()) {
      const b = el("button", null, t);
      b.type = "button";
      b.addEventListener("click", () => send(t));
      box.append(b);
    }
  }

  async function openSummary(summary, fresh) {
    show("drawing");
    store("ed-session", summary.id);
    S.hidden = new Set(summary.digest.layers.filter((l) => !l.on).map((l) => l.name));
    S.selected.clear();
    S.viewSet = false;
    log.replaceChildren();
    S.proposal = null; S.overlay = null; activeCard = null;
    $("legend").hidden = true;
    await adopt(summary, { fit: true });
    const d = summary.digest;
    const size = d.sizeMetres ? `about ${fmt(d.sizeMetres[0])} × ${fmt(d.sizeMetres[1])} m` : "size unknown";
    let intro = `${fresh ? "Opened" : "Back in"} ${summary.name}: ${fmt(d.entityCount)} entities on ${fmt(d.layerCount)} layers, ${size}. Units: ${d.units.name}${d.units.guessed ? " (guessed, the file doesn't say)" : ""}.`;
    const ns = Object.entries(d.notShown || {});
    if (ns.length) intro += ` Not drawn here: ${ns.map(([k, v]) => `${k} ×${fmt(v)}`).join(", ")}.`;
    if (d.truncated) intro += " Very large drawing: only part of it is shown.";
    addMsg("bot", intro);
    for (const n of summary.notes || []) addMsg("sys", n);
    addMsg("sys", "Click things on the drawing to select them, or just describe a change. I'll show you the result before anything is applied.");
    renderSuggestions();
    setComposer(true);
    $("input").focus();
  }

  function show(which) {
    const drawing = which === "drawing";
    $("empty").hidden = drawing;
    $("canvas-wrap").hidden = !drawing;
    $("top-actions").hidden = !drawing;
    $("file-name").hidden = !drawing;
    if (drawing) resize();
  }
  function busy(text) {
    $("busy").hidden = !text;
    if (text) $("busy-text").textContent = text;
  }

  function dropError(msg) {
    $("drop-error").hidden = !msg;
    $("drop-error-msg").textContent = msg || "";
    $("drop").dataset.state = msg ? "error" : "empty";
  }
  async function openFile(file) {
    if (!file) return;
    dropError("");
    if (!/\.(dwg|dxf)$/i.test(file.name)) return dropError("Only .dwg and .dxf files can be opened.");
    busy(`Opening ${file.name}…`);
    try {
      const fd = new FormData();
      fd.append("file", file, file.name);
      const summary = await api("/api/editor/sessions", { method: "POST", body: fd });
      busy("");
      await openSummary(summary, true);
    } catch (err) {
      busy("");
      show("empty");
      dropError(err.message);
    }
  }
  async function openSample() {
    dropError("");
    busy("Opening the sample warehouse…");
    try {
      const summary = await post("/api/editor/sessions/sample");
      busy("");
      await openSummary(summary, true);
    } catch (err) { busy(""); dropError(err.message); }
  }
  async function closeDrawing() {
    const id = sid();
    S.session = null; S.scene = null; S.proposal = null; S.overlay = null; activeCard = null; S.selected.clear();
    store("ed-session", null);
    $("suggest").replaceChildren();
    log.replaceChildren(el("div", "msg sys", "Open a drawing to start. Then describe a change, or click things on the drawing to select them."));
    updateSelection();
    setComposer(false);
    show("empty");
    if (id) api(`/api/editor/sessions/${id}`, { method: "DELETE" }).catch(() => {});
  }

  // ── undo / redo / changes ──────────────────────────────────────────────
  async function history(kind) {
    if (!S.session || S.pending) return;
    setPending(true);
    try {
      if (activeCard) retire(activeCard, "Dropped (the drawing changed)", "no");
      const r = await post(`/api/editor/sessions/${sid()}/${kind}`);
      await adopt(r.summary, { fit: false });
      addMsg("sys", (kind === "undo" ? "Undid: " : "Redid: ") + r.label);
    } catch (err) { toast(err.message); } finally { setPending(false); }
  }
  $("undo-btn").addEventListener("click", () => history("undo"));
  $("redo-btn").addEventListener("click", () => history("redo"));
  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey) || !S.session || /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
    const k = e.key.toLowerCase();
    if (k === "z" && !e.shiftKey) { e.preventDefault(); history("undo"); }
    else if (k === "y" || (k === "z" && e.shiftKey)) { e.preventDefault(); history("redo"); }
  });

  function renderChanges() {
    const ol = $("changes");
    ol.replaceChildren();
    const entries = (S.session && S.session.log) || [];
    $("log-count").textContent = String(entries.length);
    if (!entries.length) { ol.append(el("li", "empty", "Nothing has changed yet. Accepted changes are listed here, newest first.")); return; }
    for (const e of [...entries].reverse()) {
      const li = el("li");
      const when = el("div", "when");
      when.append(el("span", null, `Change ${e.rev} · ${e.source}`), el("span", null, new Date(e.time * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })));
      li.append(when);
      if (e.prompt) li.append(el("div", "prompt", `“${e.prompt}”`));
      const ul = el("ul");
      e.summaries.forEach((s) => ul.append(el("li", null, s)));
      li.append(ul);
      ol.append(li);
    }
  }

  // ── tabs, composer, split ──────────────────────────────────────────────
  function selectTab(which) {
    for (const [tab, pane] of [["tab-chat", "pane-chat"], ["tab-log", "pane-log"]]) {
      const on = (which === "chat") === (tab === "tab-chat");
      $(tab).setAttribute("aria-selected", String(on));
      $(pane).hidden = !on;
    }
  }
  $("tab-chat").addEventListener("click", () => selectTab("chat"));
  $("tab-log").addEventListener("click", () => selectTab("log"));

  $("compose").addEventListener("submit", (e) => {
    e.preventDefault();
    const t = $("input").value;
    $("input").value = "";
    $("input").style.height = "";
    send(t);
  });
  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("compose").requestSubmit(); }
  });
  $("input").addEventListener("input", () => {
    const t = $("input");
    t.style.height = "auto";
    t.style.height = Math.min(t.scrollHeight, 140) + "px";
  });
  $("selchip-clear").addEventListener("click", () => setSelection([], false));

  function updateAiPill() {
    const ai = S.config && S.config.ai;
    const pill = $("ai-pill");
    if (ai && ai.enabled) {
      const tail = String(ai.model || "on").split("/").pop();
      pill.textContent = /nemotron/i.test(tail) ? "AI · Nemotron" : "AI · " + tail;
      pill.title = `Open-ended requests go to ${ai.provider || "an AI service"} (${ai.model}). Built-in commands stay on this server.`;
      pill.classList.add("on");
    } else {
      pill.textContent = "Built-in commands";
      pill.title = "The AI assistant isn't connected on this server (set NVIDIA_API_KEY). Plain commands still work.";
      pill.classList.remove("on");
    }
  }

  (function split() {
    const bar = $("split");
    const root = document.documentElement;
    const clamp = (v) => Math.max(320, Math.min(v, Math.min(760, window.innerWidth * 0.7)));
    const set = (v) => { root.style.setProperty("--chat-w", clamp(v) + "px"); resize(); };
    try { const saved = Number(localStorage.getItem("ed-chat-w")); if (saved) root.style.setProperty("--chat-w", clamp(saved) + "px"); } catch (e) { /* optional */ }
    bar.addEventListener("pointerdown", (e) => {
      bar.setPointerCapture(e.pointerId);
      bar.classList.add("drag");
      const move = (ev) => set(window.innerWidth - ev.clientX - 5);
      const up = () => {
        bar.classList.remove("drag");
        bar.removeEventListener("pointermove", move);
        bar.removeEventListener("pointerup", up);
        try { localStorage.setItem("ed-chat-w", String(parseInt(getComputedStyle(root).getPropertyValue("--chat-w"), 10))); } catch (e2) { /* optional */ }
      };
      bar.addEventListener("pointermove", move);
      bar.addEventListener("pointerup", up);
    });
    bar.addEventListener("keydown", (e) => {
      const cur = parseInt(getComputedStyle(root).getPropertyValue("--chat-w"), 10) || 440;
      if (e.key === "ArrowLeft") { e.preventDefault(); set(cur + 24); }
      if (e.key === "ArrowRight") { e.preventDefault(); set(cur - 24); }
    });
  })();

  // ── export ─────────────────────────────────────────────────────────────
  const dlg = $("export-dlg");
  let exportPoll = 0;
  function chip(group, name, value, label, checked, disabled) {
    const l = el("label", "chip");
    const i = el("input");
    i.type = "radio"; i.name = name; i.value = value; i.checked = checked; i.disabled = !!disabled;
    l.append(i, el("span", null, label));
    group.append(l);
  }
  function openExport() {
    const year = DOC_YEAR[S.session.docVersion] || 0;
    const cfg = S.config;
    const targets = cfg.targets.filter((t) => t.year <= year);
    $("export-lede").textContent = year
      ? `Your edited drawing is in the ${year >= 2018 ? "2018+" : year} format. Choose an older release to save it as.`
      : "This drawing is already in a very old format. Use “Download DXF” to keep it as is.";
    const tg = $("export-targets"), fm = $("export-formats");
    tg.replaceChildren(); fm.replaceChildren();
    const def = (targets.find((t) => t.year === 2010) || targets[targets.length - 1] || {}).year;
    targets.forEach((t) => chip(tg, "xt", t.year, t.year === 2018 ? "2018+" : String(t.year), t.year === def));
    const canDwg = cfg.formats.includes("DWG");
    chip(fm, "xf", "DWG", "DWG", canDwg, !canDwg);
    chip(fm, "xf", "DXF", "DXF", !canDwg);
    $("export-hint").textContent = canDwg ? "" : "Saving as DWG needs ODA File Converter on the server. Saving as DXF works in every AutoCAD release.";
    $("export-bar").hidden = true; $("export-status").textContent = "";
    for (const id of ["export-dl", "export-report"]) $(id).hidden = true;
    $("export-go").disabled = !targets.length;
    if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
  }
  async function runExport() {
    const target = Number((dlg.querySelector('input[name="xt"]:checked') || {}).value);
    const format = (dlg.querySelector('input[name="xf"]:checked') || {}).value;
    if (!target || !format) return;
    $("export-go").disabled = true;
    $("export-bar").hidden = false;
    $("export-status").textContent = "Converting…";
    try {
      let job = await post(`/api/editor/sessions/${sid()}/export`, { target, format });
      const tick = async () => {
        job = await api(`/api/jobs/${job.id}`);
        const pct = Math.round(job.progress || 0);
        $("export-fill").style.width = pct + "%";
        $("export-bar").setAttribute("aria-valuenow", String(pct));
        if (job.status === "done") {
          const c = job.result.counts || {};
          $("export-status").textContent = `Ready: ${job.result.outputName}` + (c.skipped ? ` · ${c.skipped} item${c.skipped === 1 ? "" : "s"} skipped (see the report)` : " · nothing skipped");
          const dl = $("export-dl");
          dl.href = API + job.result.downloadUrl; dl.hidden = false;
          const rp = $("export-report");
          rp.href = API + job.result.reportUrl; rp.hidden = false;
          $("export-go").disabled = false;
        } else if (job.status === "failed" || job.status === "cancelled") {
          $("export-status").textContent = (job.error && job.error.message) || "The conversion failed.";
          $("export-go").disabled = false;
        } else exportPoll = setTimeout(() => tick().catch(fail), 500);
      };
      const fail = (err) => { $("export-status").textContent = err.message; $("export-go").disabled = false; };
      await tick();
    } catch (err) {
      $("export-status").textContent = err.message;
      $("export-go").disabled = false;
    }
  }
  $("export-btn").addEventListener("click", openExport);
  $("export-go").addEventListener("click", runExport);
  $("export-close").addEventListener("click", () => { clearTimeout(exportPoll); dlg.close ? dlg.close() : dlg.removeAttribute("open"); });
  dlg.addEventListener("close", () => clearTimeout(exportPoll));
  $("dl-btn").addEventListener("click", () => {
    const a = document.createElement("a");
    a.href = `${API}/api/editor/sessions/${sid()}/download.dxf`;
    a.download = "";
    document.body.append(a); a.click(); a.remove();
  });
  $("close-btn").addEventListener("click", closeDrawing);

  // ── open-file controls ─────────────────────────────────────────────────
  $("choose-btn").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", (e) => { openFile(e.target.files[0]); e.target.value = ""; });
  $("sample-btn").addEventListener("click", openSample);
  const view = $("view");
  for (const ev of ["dragenter", "dragover"]) view.addEventListener(ev, (e) => { e.preventDefault(); $("drop").classList.add("is-over"); });
  for (const ev of ["dragleave", "drop"]) view.addEventListener(ev, (e) => { e.preventDefault(); $("drop").classList.remove("is-over"); });
  view.addEventListener("drop", (e) => { const f = e.dataTransfer && e.dataTransfer.files[0]; if (f) openFile(f); });

  // ── start ──────────────────────────────────────────────────────────────
  new ResizeObserver(resize).observe(cv);
  window.addEventListener("resize", resize);
  setComposer(false);
  log.replaceChildren(el("div", "msg sys", "Open a drawing to start. Then describe a change, or click things on the drawing to select them."));
  (async function init() {
    try { S.config = await api("/api/editor/config"); } catch (err) {
      S.config = { ai: { enabled: false }, targets: [], formats: [] };
      dropError("The editing server isn't reachable. The editor needs the Backdate.dwg server (see the README).");
      $("choose-btn").disabled = true; $("sample-btn").disabled = true;
    }
    updateAiPill();
    $("privacy").textContent = S.config.ai.enabled
      ? "Your drawing stays on this server. For open-ended requests, your message and a summary of the drawing (layers, counts, text labels) are sent to NVIDIA's AI service. Built-in commands never leave the server."
      : "Your drawing stays on this server. The AI assistant isn't connected here, so built-in commands only (rename or purge layers, move, scale, replace text, and so on).";
    const saved = store("ed-session");
    if (saved) {
      try { await openSummary(await api(`/api/editor/sessions/${saved}`), false); } catch (e) { store("ed-session", null); }
    }
  })();
})();
