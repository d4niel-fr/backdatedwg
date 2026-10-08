// Backdate.dwg AI editor: a drawing on the left, a conversation (and tools) on the right.
// Edits arrive as proposals. They are previewed on the drawing and only applied when accepted.
// This file is the core; editor-tools.js (the Tools tab) and editor-review.js (review links)
// build on the small interface it publishes as window.ED.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const API = String(window.BACKDATE_API || "").replace(/\/+$/, "");
  const INK = "#2e2b25";
  const ACCENT = "#c67139";
  const DEL = "#c2410c";
  const ADD = "#3d7a3a";
  const CHG = "#b7791f";
  const AREA = "#0f766e";
  const DOC_YEAR = { AC1032: 2018, AC1027: 2013, AC1024: 2010, AC1021: 2007, AC1018: 2004, AC1015: 2000 };
  const params = new URLSearchParams(location.search);

  const S = {
    config: null,
    session: null, // summary from the server
    scene: null, // { items, groups, texts, byHandle, hbox, extents }
    view: { cx: 0, cy: 0, scale: 1 },
    viewSet: false,
    hidden: new Set(), // layers hidden in the viewer
    selected: new Set(),
    proposal: null, // pending proposal view (drives the preview overlay)
    overlay: null, // { remove: Set, add: [items] }
    pending: false,
    boxMode: false,
    areaMode: false,
    area: null, // [x0, y0, x1, y1] in world units
    W: 0,
    H: 0,
    dpr: 1,
    dirty: false,
    marquee: null,
    people: [], // others watching this session
    cursors: {}, // client -> {x, y, name, color, t}
    pins: [], // review comments with positions
    readOnly: false,
  };
  const hooks = { draw: [], adopt: [], open: [], close: [], event: [] };

  const cv = $("cv");
  const ctx = cv.getContext("2d");

  // ── identity: workspace (private memory and search), name, access key ──
  function local(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch (e) { /* storage can be blocked; everything still works for this visit */ }
    return null;
  }
  function randomId(n) {
    const a = new Uint8Array(n);
    crypto.getRandomValues(a);
    return Array.from(a, (b) => "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"[b & 63]).join("");
  }
  let memWorkspace = null;
  function workspaceKey() {
    let ws = local("bd-workspace");
    if (!ws || !/^[A-Za-z0-9_-]{16,128}$/.test(ws)) {
      ws = memWorkspace || randomId(24);
      memWorkspace = ws;
      local("bd-workspace", ws);
    }
    return ws;
  }
  const CLIENT = "c" + randomId(12);
  function myName() { return local("bd-name") || ""; }
  function accessKey() { return local("bd-access-key") || ""; }
  function headers(extra) {
    const h = { "X-Workspace": workspaceKey(), "X-Client-Id": CLIENT, "X-Client-Name": myName() || "" };
    const k = accessKey();
    if (k) h["X-Access-Token"] = k;
    return Object.assign(h, extra || {});
  }

  // ── helpers ─────────────────────────────────────────────────────────────
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function btn(label, cls, onClick) {
    const b = el("button", cls || "btn btn-secondary", label);
    b.type = "button";
    if (onClick) b.addEventListener("click", onClick);
    return b;
  }
  function store(key, value) {
    try {
      if (value === undefined) return sessionStorage.getItem(key);
      if (value === null) sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, value);
    } catch (e) { /* optional */ }
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
    const o = Object.assign({}, opts || {});
    o.headers = headers(o.headers);
    let res;
    try { res = await fetch(API + path, o); } catch (e) { throw new Error("Can't reach the server. Check your connection and try again."); }
    if (res.status === 204) return null;
    let body = null;
    try { body = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) throw new Error((body && body.error && body.error.message) || (body && body.detail ? "That request wasn't valid." : `The server returned an error (${res.status}).`));
    return body;
  }
  const post = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });
  async function download(path, fallbackName) {
    // fetch (so headers such as an access key go along), then save the blob
    let res;
    try { res = await fetch(API + path, { headers: headers() }); } catch (e) { toast("Can't reach the server."); return; }
    if (!res.ok) {
      let msg = `Download failed (${res.status}).`;
      try { const b = await res.json(); msg = (b.error && b.error.message) || msg; } catch (e) { /* binary */ }
      toast(msg);
      return;
    }
    const cd = res.headers.get("content-disposition") || "";
    const m = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(cd);
    const name = m ? decodeURIComponent(m[1] || m[2]) : fallbackName || "download";
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
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

  function setScene(geo, fit) {
    S.scene = buildScene(geo);
    for (const h of [...S.selected]) if (!S.scene.byHandle.has(h)) S.selected.delete(h);
    if (fit || !S.viewSet) fitView();
    updateSelection();
    requestDraw();
  }

  async function loadGeometry({ fit }) {
    setScene(await api(`/api/editor/sessions/${sid()}/geometry`), fit);
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
    requestDraw();
  }
  function fitView() {
    const e = S.scene && S.scene.extents;
    if (e) fitTo(e); else { S.view = { cx: 0, cy: 0, scale: 1 }; S.viewSet = true; }
    requestDraw();
  }
  function viewport() { return [wx(0), wy(S.H), wx(S.W), wy(0)]; }
  const overlaps = (a, b) => !(a[2] < b[0] || a[0] > b[2] || a[3] < b[1] || a[1] > b[3]);

  function trace(it) {
    const p = it.p;
    ctx.moveTo(sx(p[0]), sy(p[1]));
    for (let i = 2; i < p.length; i += 2) ctx.lineTo(sx(p[i]), sy(p[i + 1]));
    if (it.z) ctx.closePath();
  }
  function visible(it, vp, minPx) {
    if (it.l && S.hidden.has(it.l)) return false;
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
  function strokeItems(items, color, width, dash, alpha) {
    const vp = viewport();
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.globalAlpha = alpha || 1; ctx.setLineDash(dash || []);
    ctx.beginPath();
    for (const it of items) if (it.k === "p" && visible(it, vp, 0.3)) trace(it);
    ctx.stroke();
    ctx.setLineDash([]); ctx.globalAlpha = 1;
    for (const it of items) if (it.k === "t" && visible(it, vp, 0)) drawText(it, color);
  }
  const itemsOf = (handles) => { const out = []; for (const h of handles) for (const it of (S.scene.byHandle.get(h) || [])) out.push(it); return out; };

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
      strokeItems(itemsOf(removed), DEL, 2, [6, 4], 0.55);
      strokeItems(S.overlay.add, ADD, 2);
    }
    for (const fn of hooks.draw) fn(ctx, { sx, sy, wx, wy, viewport, strokeItems, itemsOf, trace, drawText });

    if (S.selected.size) strokeItems(itemsOf(S.selected).filter((it) => !S.hidden.has(it.l)), ACCENT, 2.5);

    if (S.area) {
      const [x0, y0, x1, y1] = S.area;
      ctx.strokeStyle = AREA; ctx.lineWidth = 1.5; ctx.setLineDash([8, 5]);
      ctx.fillStyle = "rgba(15,118,110,0.07)";
      ctx.fillRect(sx(x0), sy(y1), sx(x1) - sx(x0), sy(y0) - sy(y1));
      ctx.strokeRect(sx(x0), sy(y1), sx(x1) - sx(x0), sy(y0) - sy(y1));
      ctx.setLineDash([]);
    }
    drawPins();
    drawCursors();

    if (S.marquee) {
      const m = S.marquee, crossing = m.x1 < m.x0;
      const x = Math.min(m.x0, m.x1), y = Math.min(m.y0, m.y1), w = Math.abs(m.x1 - m.x0), h = Math.abs(m.y1 - m.y0);
      ctx.lineWidth = 1.25;
      ctx.setLineDash(m.area ? [8, 5] : crossing ? [5, 4] : []);
      ctx.fillStyle = m.area ? "rgba(15,118,110,0.08)" : "rgba(198,113,57,0.10)";
      ctx.strokeStyle = m.area ? AREA : ACCENT;
      ctx.fillRect(x, y, w, h); ctx.strokeRect(x, y, w, h); ctx.setLineDash([]);
    }
  }

  function drawPins() {
    S.pins.forEach((p, i) => {
      if (p.x == null || p.y == null) return;
      const x = sx(p.x), y = sy(p.y);
      if (x < -20 || y < -20 || x > S.W + 20 || y > S.H + 20) return;
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.arc(x, y - 16, 11, Math.PI * 0.75, Math.PI * 2.25);
      ctx.closePath();
      ctx.fillStyle = p.resolved ? "#a19786" : (p.owner ? "#7a8a5e" : ACCENT);
      ctx.fill();
      ctx.fillStyle = "#fff";
      ctx.font = "600 11px Figtree, system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(String(p.n || i + 1), x, y - 12);
      ctx.textAlign = "start";
    });
  }
  function drawCursors() {
    const now = Date.now();
    for (const c of Object.values(S.cursors)) {
      if (c.x == null || now - c.t > 20000) continue;
      const x = sx(c.x), y = sy(c.y);
      ctx.fillStyle = c.color;
      ctx.beginPath();
      ctx.moveTo(x, y); ctx.lineTo(x + 3, y + 15); ctx.lineTo(x + 7, y + 10); ctx.lineTo(x + 13, y + 11); ctx.closePath();
      ctx.fill();
      ctx.font = "600 11px Figtree, system-ui, sans-serif";
      const label = c.name || "Guest";
      const w = ctx.measureText(label).width + 10;
      ctx.fillRect(x + 12, y + 12, w, 17);
      ctx.fillStyle = "#fff";
      ctx.fillText(label, x + 17, y + 25);
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
  function pickPin(px, py) {
    for (const p of S.pins) {
      if (p.x == null) continue;
      if (Math.hypot(sx(p.x) - px, sy(p.y) - 16 - py) < 12) return p;
    }
    return null;
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
    if (n && S.scene) $("selchip-text").textContent = `${fmt(n)} selected — ${describeHandles(S.selected)}`;
    $("input").placeholder = n ? "What should happen to the selection?  e.g. “move it 2 m north”, “what is this?”"
      : S.area ? "Ask about the marked area…  e.g. “what's in this area?”"
        : "Describe a change…  e.g. “move layer S-RACK 2 m east”";
    sendPresence();
  }
  function setSelection(handles, add) {
    if (!add) S.selected.clear();
    for (const h of handles) S.selected.add(h);
    updateSelection();
    requestDraw();
  }
  function selectAndShow(handles) {
    if (!S.scene || !handles || !handles.length) return;
    setSelection(handles.filter((h) => S.scene.byHandle.has(h)), false);
    const boxes = handles.map((h) => S.scene.hbox.get(h)).filter(Boolean);
    if (boxes.length) fitTo(boxes.reduce((a, b) => [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[2], b[2]), Math.max(a[3], b[3])]), 0.25);
  }
  function setArea(box) {
    S.area = box;
    $("areachip").hidden = !box;
    if (box) {
      const u = unitShort();
      $("areachip-text").textContent = `Area marked: ${fmt(box[2] - box[0])} × ${fmt(box[3] - box[1])} ${u} — your next questions are about this area`;
    }
    updateSelection();
    requestDraw();
  }

  // ── pointer interaction ────────────────────────────────────────────────
  let drag = null;
  let hoverQueued = false;
  function unitShort() {
    const u = S.session && S.session.digest && S.session.digest.units;
    return u ? u.short : "";
  }
  cv.addEventListener("pointerdown", (e) => {
    if (!S.scene || (e.button !== 0 && e.button !== 1)) return;
    cv.setPointerCapture(e.pointerId);
    cv.focus({ preventScroll: true });
    const r = cv.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    const area = e.button === 0 && (e.altKey || S.areaMode);
    drag = { x, y, lx: x, ly: y, moved: false, area, box: e.button === 0 && !area && (e.shiftKey || S.boxMode), shift: e.shiftKey || e.ctrlKey || e.metaKey };
    if (drag.box || drag.area) S.marquee = { x0: x, y0: y, x1: x, y1: y, area };
  });
  cv.addEventListener("pointermove", (e) => {
    const r = cv.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    $("st-coords").textContent = S.scene ? `X ${fmt(wx(x))}   Y ${fmt(wy(y))} ${unitShort()}` : "";
    S.mouse = { x: wx(x), y: wy(y) };
    sendPresence();
    if (drag) {
      if (!drag.moved && Math.hypot(x - drag.x, y - drag.y) > 4) drag.moved = true;
      if (drag.box || drag.area) { S.marquee.x1 = x; S.marquee.y1 = y; requestDraw(); }
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
        const pin = pickPin(x, y);
        if (pin) { $("st-hover").textContent = `Comment by ${pin.author}: ${pin.text}`; return; }
        const it = pick(wx(x), wy(y), 6);
        $("st-hover").textContent = it ? `${it.t} · layer ${it.l} · handle ${it.h}` : "";
      });
    }
  });
  function endDrag(e) {
    if (!drag) return;
    const d = drag;
    drag = null;
    cv.classList.remove("panning");
    if (d.box || d.area) {
      const m = S.marquee;
      S.marquee = null;
      if (d.moved) {
        const b = [Math.min(wx(m.x0), wx(m.x1)), Math.min(wy(m.y0), wy(m.y1)), Math.max(wx(m.x0), wx(m.x1)), Math.max(wy(m.y0), wy(m.y1))];
        if (d.area) {
          setArea(b);
          if (S.areaMode) toggleArea(false);
          $("input").focus();
          return;
        }
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
      const pin = pickPin(d.x, d.y);
      if (pin) { hooks.event.forEach((fn) => fn("pin-click", pin)); return; }
      if (S.pinMode && S.onPin) { S.onPin(wx(d.x), wy(d.y)); return; }
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
    else if (e.key === "a" || e.key === "A") toggleArea();
    else if (e.key === "Escape") { setSelection([], false); setArea(null); }
    else if ((e.key === "Delete" || e.key === "Backspace") && S.selected.size && !S.readOnly) { e.preventDefault(); send("delete the selection"); }
  });

  function toggleBox() {
    S.boxMode = !S.boxMode;
    if (S.boxMode && S.areaMode) toggleArea(false);
    $("box-btn").setAttribute("aria-pressed", String(S.boxMode));
  }
  function toggleArea(force) {
    S.areaMode = force === undefined ? !S.areaMode : force;
    if (S.areaMode && S.boxMode) toggleBox();
    $("area-btn").setAttribute("aria-pressed", String(S.areaMode));
    if (S.areaMode) toast("Drag a box around the area you want to ask about.");
  }
  $("box-btn").addEventListener("click", toggleBox);
  $("area-btn").addEventListener("click", () => toggleArea());
  $("fit-btn").addEventListener("click", fitView);
  $("areachip-clear").addEventListener("click", () => setArea(null));

  // ── find ───────────────────────────────────────────────────────────────
  $("find-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = $("find-input").value.trim();
    const box = $("find-results");
    if (!q || !sid()) { box.hidden = true; return; }
    try {
      const r = await api(`/api/editor/sessions/${sid()}/find?q=${encodeURIComponent(q)}`);
      box.replaceChildren();
      if (!r.results.length) box.append(el("li", "muted small", "Nothing found."));
      r.results.slice(0, 60).forEach((x) => {
        const li = el("li");
        const b = btn(`${x.label}`, "ed-find-hit");
        b.prepend(el("span", "tag tag-neutral", x.kind));
        b.append(el("span", "muted small", ` ${x.layer}`));
        b.addEventListener("click", () => { selectAndShow([x.handle]); box.hidden = true; });
        li.append(b);
        box.append(li);
      });
      if (r.count > 60) box.append(el("li", "muted small", `…and ${fmt(r.count - 60)} more.`));
      if (r.count > 1) {
        const li = el("li");
        li.append(btn(`Select all ${fmt(Math.min(r.count, 500))}`, "btn btn-ghost", () => { selectAndShow(r.results.map((x) => x.handle)); box.hidden = true; }));
        box.append(li);
      }
      box.hidden = false;
    } catch (err) { toast(err.message); }
  });
  document.addEventListener("click", (e) => { if (!e.target.closest("#find-results") && !e.target.closest("#find-form")) $("find-results").hidden = true; });

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
    $("mic-btn").disabled = !enabled;
  }
  function setPending(on) {
    S.pending = on;
    setComposer(!on && !!S.session && !S.readOnly);
    for (const b of document.querySelectorAll(".prop-actions button")) b.disabled = on;
    $("undo-btn").disabled = on || !(S.session && S.session.canUndo);
    $("redo-btn").disabled = on || !(S.session && S.session.canRedo);
  }

  function sourceLabel(r) {
    if (r.source === "model") return r.queries ? `AI assistant · looked things up ${r.queries}×` : "AI assistant";
    if (r.source === "local") return "Built-in";
    return "";
  }

  // Render a structured answer (tables, health report, inspection) under a message.
  function renderData(data) {
    if (!data) return;
    const box = el("div", "msg-data");
    if (data.kind === "table") {
      const head = el("div", "md-head");
      head.append(el("strong", null, data.title || "Table"));
      if (data.download) head.append(btn("Download CSV", "btn btn-ghost", () => download(data.download, "table.csv")));
      if (data.handles && data.handles.length) head.append(btn("Show on drawing", "btn btn-ghost", () => selectAndShow(data.handles)));
      box.append(head, tableEl(data.columns, data.rows, 25));
    } else if (data.kind === "health") {
      box.append(healthEl(data.report));
    } else if (data.kind === "inspect") {
      const it = data.items[0];
      if (it) {
        const dl = el("dl", "md-kv");
        for (const [k, v] of Object.entries(it)) {
          if (["sentence", "handle"].includes(k) || v == null || v === "") continue;
          dl.append(el("dt", null, k), el("dd", null, typeof v === "object" ? (Array.isArray(v) ? v.map((x) => (typeof x === "number" ? fmt(x) : x)).join(", ") : Object.entries(v).map(([a, b]) => `${a}=${b}`).join(", ")) : typeof v === "number" ? fmt(v) : String(v)));
        }
        box.append(dl);
      }
    } else if (data.kind === "area") {
      const s = data.summary;
      if (s.handles && s.handles.length) box.append(btn(`Select these ${fmt(s.count)}`, "btn btn-ghost", () => selectAndShow(s.handles)));
    } else return;
    log.append(box);
    scrollDown();
  }
  function tableEl(columns, rows, limit) {
    const wrap = el("div", "md-table");
    const t = el("table", "table");
    const thead = el("thead");
    const tr = el("tr");
    columns.forEach((c) => tr.append(el("th", null, c)));
    thead.append(tr);
    const tbody = el("tbody");
    const fill = (n) => {
      tbody.replaceChildren();
      rows.slice(0, n).forEach((r) => {
        const row = el("tr");
        r.forEach((c) => row.append(el("td", null, typeof c === "number" ? fmt(c) : String(c))));
        tbody.append(row);
      });
    };
    fill(limit);
    t.append(thead, tbody);
    wrap.append(t);
    if (rows.length > limit) {
      const more = btn(`Show all ${fmt(rows.length)} rows`, "btn btn-ghost", () => { fill(rows.length); more.remove(); });
      wrap.append(more);
    }
    return wrap;
  }
  function healthEl(report) {
    const box = el("div", "md-health");
    const head = el("div", "md-head");
    const score = el("span", "md-score " + (report.score >= 90 ? "ok" : report.score >= 70 ? "mid" : "bad"), String(report.score));
    head.append(score, el("strong", null, report.findings.length ? `${report.findings.length} thing${report.findings.length === 1 ? "" : "s"} to look at` : "Nothing to tidy"));
    if (report.fixAll && !S.readOnly) head.append(btn(report.fixAll.label, "btn btn-primary", () => stageOps(report.fixAll.ops, "Fix everything safe")));
    box.append(head);
    const ul = el("ul", "md-findings");
    for (const f of report.findings) {
      const li = el("li", "sev-" + f.severity);
      li.append(el("strong", null, f.title), el("p", "muted small", f.detail));
      const row = el("div", "row");
      if (f.handles && f.handles.length) row.append(btn("Show", "btn btn-ghost", () => selectAndShow(f.handles)));
      if (f.fix && !S.readOnly) row.append(btn(f.fix.label, "btn btn-secondary", () => stageOps(f.fix.ops, f.title)));
      if (row.children.length) li.append(row);
      ul.append(li);
    }
    box.append(ul);
    return box;
  }

  async function stageOps(ops, title) {
    if (!sid() || S.pending) return;
    selectTab("chat");
    setPending(true);
    try {
      const r = await post(`/api/editor/sessions/${sid()}/stage`, { ops, selection: [...S.selected].slice(0, 5000) });
      addMsg("sys", `Proposed: ${title}`);
      showProposal(r.proposal);
    } catch (err) { addMsg("bot err", err.message); } finally { setPending(false); }
  }

  function renderSuggestions(list) {
    const box = $("suggest");
    box.replaceChildren();
    for (const t of (list || []).slice(0, 5)) {
      const b = el("button", null, t);
      b.type = "button";
      b.addEventListener("click", () => send(t));
      box.append(b);
    }
  }

  function finishReply(r, bubble) {
    const text = r.reply || "";
    const kind = "bot" + (r.error && !r.proposal ? " err" : "");
    if (bubble) {
      bubble.className = "msg " + kind;
      bubble.replaceChildren(document.createTextNode(text));
      const src = sourceLabel(r);
      if (src) bubble.append(el("span", "src", src));
    } else addMsg(kind, text, sourceLabel(r));
    renderData(r.data);
    if (r.proposal) showProposal(r.proposal);
    renderSuggestions(r.suggestions);
    hooks.event.forEach((fn) => fn("reply", r));
  }

  async function send(text) {
    text = (text || "").trim();
    if (!text || !S.session || S.pending || S.readOnly) return;
    addMsg("user", text);
    $("suggest").replaceChildren();
    const think = el("div", "msg bot think");
    const status = el("span", null, "Thinking…");
    think.append(el("span", "spinner"), status);
    log.append(think);
    scrollDown();
    setPending(true);
    const body = { message: text, selection: [...S.selected].slice(0, 5000) };
    if (S.area) body.area = S.area;
    try {
      const done = await streamChat(body, think, status);
      if (!done) { // fallback: one request, one answer
        const r = await post(`/api/editor/sessions/${sid()}/chat`, body);
        finishReply(r, think);
      }
      updateAiPill();
    } catch (err) {
      think.className = "msg bot err";
      think.replaceChildren(document.createTextNode(err.message));
    } finally {
      setPending(false);
      focusInput();
    }
  }

  // Server-sent events over fetch: status lines, the reply as it is written, then the result.
  async function streamChat(body, think, status) {
    let res;
    try {
      res = await fetch(`${API}/api/editor/sessions/${sid()}/chat/stream`, { method: "POST", headers: headers({ "Content-Type": "application/json" }), body: JSON.stringify(body) });
    } catch (e) { return false; }
    if (!res.ok || !res.body || !res.body.getReader) return false;
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let live = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        let kind = null, data = null;
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) kind = line.slice(6).trim();
          else if (line.startsWith("data:")) { try { data = JSON.parse(line.slice(5)); } catch (e) { data = null; } }
        }
        if (!kind) continue;
        if (kind === "status" && typeof data === "string") status.textContent = data;
        else if (kind === "reply" && typeof data === "string") {
          if (!live) { live = el("span", "live"); think.className = "msg bot"; think.replaceChildren(live); }
          live.textContent = data;
          scrollDown();
        } else if (kind === "result") { finishReply(data, think); return true; }
        else if (kind === "error") { throw new Error((data && data.message) || "Something went wrong."); }
      }
    }
    return false;
  }

  // ── proposals ──────────────────────────────────────────────────────────
  const cards = new Map(); // proposal id -> card
  let activeCard = null;
  function showProposal(p, from) {
    if (activeCard) retire(activeCard, "Replaced by a newer suggestion", "no");
    S.proposal = p;
    S.overlay = { remove: new Set(p.preview.remove), add: p.preview.add.map(prep) };
    const card = el("div", "prop");
    const head = el("div", "prop-head");
    head.append(el("span", "tag tag-accent", from ? `Proposed by ${from}` : "Proposed change"));
    const st = p.stats, bits = [];
    if (st.changed) bits.push(`${fmt(st.changed)} changed`);
    if (st.removed) bits.push(`${fmt(st.removed)} removed`);
    if (st.added) bits.push(`${fmt(st.added)} added`);
    if (st.tables && !bits.length) bits.push("settings only");
    head.append(el("span", "prop-stats", bits.join(" · ")));
    card.append(head);
    const checks = [];
    if (p.steps && p.steps.length > 1) {
      const ol = el("ol", "prop-steps");
      p.steps.forEach((s, i) => {
        const li = el("li");
        const lab = el("label");
        const cb = el("input");
        cb.type = "checkbox";
        cb.checked = true;
        cb.addEventListener("change", () => { yes.textContent = checks.every((c) => c.checked) ? "Accept" : `Accept ${checks.filter((c) => c.checked).length} of ${checks.length}`; });
        checks.push(cb);
        lab.append(cb, el("strong", null, s.title || `Step ${i + 1}`));
        li.append(lab);
        const ul = el("ul");
        s.summaries.forEach((x) => ul.append(el("li", null, x)));
        li.append(ul);
        ol.append(li);
      });
      card.append(ol);
    } else {
      const ul = el("ul");
      p.summaries.forEach((s) => ul.append(el("li", null, s)));
      card.append(ul);
    }
    const warn = el("ul", "prop-warn");
    for (const w of p.warnings) warn.append(el("li", "warn", (w.startsWith("Skipped") ? "" : "Heads-up: ") + w));
    if (p.preview.truncated) warn.append(el("li", "warn", "The preview on the drawing is partial because the change is very large."));
    if (warn.children.length) card.append(warn);
    if (p.why) {
      const why = el("details", "prop-why");
      why.append(el("summary", null, "Why this?"), el("p", null, p.why));
      card.append(why);
    }
    const det = el("details");
    det.append(el("summary", null, "Show the exact operations"), el("pre", null, JSON.stringify(p.steps && p.steps.length > 1 ? p.steps.map((s) => ({ title: s.title, ops: s.ops })) : p.ops, null, 2)));
    card.append(det);
    const actions = el("div", "prop-actions");
    const yes = btn("Accept", "btn btn-primary", () => accept(card, p, checks));
    const no = btn("Reject", "btn btn-secondary", () => reject(card, p));
    const show = btn("Show on drawing", "btn btn-ghost", () => focusOverlay(true));
    actions.append(yes, no);
    if (S.overlay.remove.size || S.overlay.add.length) actions.append(show);
    if (S.readOnly) { yes.disabled = true; no.disabled = true; }
    card.append(actions);
    activeCard = card;
    cards.set(p.id, card);
    log.append(card);
    scrollDown();
    showLegend(S.overlay.remove.size || S.overlay.add.length ? "proposal" : null);
    focusOverlay(false);
    requestDraw();
  }
  function showLegend(kind) {
    const lg = $("legend");
    lg.hidden = !kind;
    if (!kind) return;
    const cmp = kind === "compare";
    $("legend-del").textContent = cmp ? "removed (in the older revision)" : "will be removed or moved";
    $("legend-add").textContent = cmp ? "added" : "new position / added";
    $("legend-chg-wrap").hidden = !cmp;
    $("legend-close").hidden = !cmp;
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
    if (!card || card.classList.contains("done")) return;
    card.classList.add("done");
    const actions = card.querySelector(".prop-actions");
    if (actions) actions.replaceWith(el("div", "prop-state " + kind, text));
    card.querySelectorAll(".prop-steps input").forEach((c) => { c.disabled = true; });
    if (activeCard === card) { activeCard = null; S.proposal = null; S.overlay = null; showLegend(null); }
    requestDraw();
  }
  async function accept(card, p, checks) {
    setPending(true);
    try {
      const chosen = checks && checks.length ? checks.map((c, i) => (c.checked ? i : -1)).filter((i) => i >= 0) : null;
      if (chosen && !chosen.length) { toast("Tick at least one step, or reject the proposal."); return; }
      const body = chosen && chosen.length < checks.length ? { steps: chosen } : {};
      const r = await post(`/api/editor/sessions/${sid()}/proposals/${p.id}/accept`, body);
      retire(card, `Applied (change ${r.summary.rev})` + (body.steps ? ` · ${body.steps.length} of ${checks.length} steps` : ""), "ok");
      await adopt(r.summary, { fit: false });
      renderSuggestions(r.suggestions);
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
    hooks.adopt.forEach((fn) => fn(summary));
  }
  async function refresh() {
    if (!sid()) return;
    try { await adopt(await api(`/api/editor/sessions/${sid()}`), { fit: false }); } catch (e) { /* the next action reports it */ }
  }

  function startSuggestions() {
    const s = ["What's in this drawing?", "Health check", "Clean up the drawing", "Explain this drawing"];
    if (S.config && S.config.ai.enabled) s.push("Find anything unusual or messy in this drawing");
    renderSuggestions(s);
  }

  async function openSummary(summary, fresh, joined) {
    show("drawing");
    store("ed-session", summary.id);
    S.hidden = new Set(summary.digest.layers.filter((l) => !l.on).map((l) => l.name));
    S.selected.clear();
    S.viewSet = false;
    S.area = null;
    $("areachip").hidden = true;
    $("find-input").value = "";
    $("find-results").hidden = true;
    log.replaceChildren();
    S.proposal = null; S.overlay = null; activeCard = null; cards.clear();
    showLegend(null);
    selectTab("chat");
    await adopt(summary, { fit: true });
    const d = summary.digest;
    const size = d.sizeMetres ? `about ${fmt(d.sizeMetres[0])} × ${fmt(d.sizeMetres[1])} m` : "size unknown";
    let intro = `${joined ? "Joined" : fresh ? "Opened" : "Back in"} ${summary.name}: ${fmt(d.entityCount)} entities on ${fmt(d.layerCount)} layers, ${size}. Units: ${d.units.name}${d.units.guessed ? " (guessed, the file doesn't say)" : ""}.`;
    const ns = Object.entries(d.notShown || {});
    if (ns.length) intro += ` Not drawn here: ${ns.map(([k, v]) => `${k} ×${fmt(v)}`).join(", ")}.`;
    if (d.truncated) intro += " Very large drawing: only part of it is shown.";
    addMsg("bot", intro);
    for (const n of summary.notes || []) addMsg("sys", n);
    const mem = summary.memory;
    if (mem && mem.changes && mem.changes.length) {
      const last = mem.changes[mem.changes.length - 1];
      addMsg("bot", `Welcome back: you've opened this drawing ${mem.visits} time${mem.visits === 1 ? "" : "s"} before. Last time: ${last.summaries.join("; ")}` + (last.prompt ? ` (you asked “${last.prompt}”).` : "."), "Remembered in your workspace");
    }
    addMsg("sys", "Click things on the drawing to select them, Alt-drag to mark an area, or just describe a change. I'll show you the result before anything is applied.");
    startSuggestions();
    setComposer(true);
    focusInput();
    connectEvents();
    hooks.open.forEach((fn) => fn(summary, { fresh, joined }));
  }

  const coarse = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;
  function focusInput() { if (!coarse) $("input").focus(); }
  function show(which) {
    const drawing = which === "drawing";
    document.body.classList.toggle("has-drawing", drawing);
    $("empty").hidden = drawing;
    $("canvas-wrap").hidden = !drawing;
    $("top-actions").hidden = !drawing || S.readOnly;
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
  let pendingImport = null;
  function openFile(file) {
    if (!file) return;
    dropError("");
    const name = file.name.toLowerCase();
    if (/\.(dwg|dxf|wdp|json)$/.test(name)) return upload(file, {});
    if (/\.pdf$/.test(name) || /\.(png|jpe?g|webp)$/.test(name)) {
      pendingImport = file;
      const pdf = /\.pdf$/.test(name);
      $("import-opts").hidden = false;
      $("import-pdf").hidden = !pdf;
      $("import-sketch").hidden = pdf;
      $("import-title").textContent = pdf ? `Import ${file.name} (vector PDF)` : `Trace ${file.name} with AI`;
      $("import-note").textContent = pdf
        ? "Lines, curves and text are imported. For a plan printed at 1:100, enter 100 to get real sizes; leave 1 for paper size."
        : (S.config && S.config.ai.vision ? "A vision model traces walls, doors and labels. Check the result against the photo." : "Tracing a sketch needs the AI assistant with a vision model; it isn't connected on this server.");
      $("import-go").focus();
      return;
    }
    dropError("Open a .dwg or .dxf drawing, or import a .pdf plan, a sketch image (.png/.jpg) or a .wdp project.");
  }
  $("import-go").addEventListener("click", () => {
    const f = pendingImport;
    if (!f) return;
    $("import-opts").hidden = true;
    pendingImport = null;
    const fields = /\.pdf$/i.test(f.name) ? { page: $("import-page").value || "1", scale: $("import-scale").value || "1" } : { width_m: $("import-width").value || "0" };
    upload(f, fields);
  });
  $("import-cancel").addEventListener("click", () => { $("import-opts").hidden = true; pendingImport = null; });
  async function upload(file, fields) {
    busy(`Opening ${file.name}…`);
    try {
      const fd = new FormData();
      fd.append("file", file, file.name);
      for (const [k, v] of Object.entries(fields || {})) fd.append(k, v);
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
    disconnectEvents();
    S.session = null; S.scene = null; S.proposal = null; S.overlay = null; activeCard = null; S.selected.clear(); S.area = null; S.pins = [];
    store("ed-session", null);
    setComposer(false);
    show("empty");
    $("suggest").replaceChildren();
    log.replaceChildren(el("div", "msg sys", "Open a drawing to start. Then describe a change, or click things on the drawing to select them."));
    updateSelection();
    hooks.close.forEach((fn) => fn());
    if (id) api(`/api/editor/sessions/${id}`, { method: "DELETE" }).catch(() => {});
  }

  // ── undo / redo / changes ──────────────────────────────────────────────
  async function history(kind) {
    if (!S.session || S.pending || S.readOnly) return;
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
    if (!(e.ctrlKey || e.metaKey) || !S.session || /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) return;
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

  // ── tabs, composer, voice, split ───────────────────────────────────────
  function selectTab(which) {
    for (const [tab, pane] of [["tab-chat", "pane-chat"], ["tab-tools", "pane-tools"], ["tab-log", "pane-log"]]) {
      const on = tab === "tab-" + which;
      $(tab).setAttribute("aria-selected", String(on));
      $(pane).hidden = !on;
    }
    hooks.event.forEach((fn) => fn("tab", which));
  }
  $("tab-chat").addEventListener("click", () => selectTab("chat"));
  $("tab-tools").addEventListener("click", () => selectTab("tools"));
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
    t.style.height = Math.min(t.scrollHeight, 160) + "px";
  });
  $("selchip-clear").addEventListener("click", () => setSelection([], false));

  (function voice() {
    const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Rec) return;
    const mic = $("mic-btn");
    mic.hidden = false;
    let rec = null;
    mic.addEventListener("click", () => {
      if (rec) { rec.stop(); return; }
      rec = new Rec();
      rec.lang = navigator.language || "en-GB";
      rec.interimResults = true;
      const before = $("input").value;
      rec.onresult = (ev) => {
        let text = "";
        for (const r of ev.results) text += r[0].transcript;
        $("input").value = (before ? before + " " : "") + text;
      };
      rec.onerror = (ev) => { toast(ev.error === "not-allowed" ? "Microphone access was refused." : "Voice input stopped."); };
      rec.onend = () => { rec = null; mic.setAttribute("aria-pressed", "false"); mic.classList.remove("rec"); $("input").focus(); };
      mic.setAttribute("aria-pressed", "true");
      mic.classList.add("rec");
      rec.start();
    });
  })();

  function updateAiPill() {
    const ai = S.config && S.config.ai;
    const pill = $("ai-pill");
    if (ai && ai.enabled) {
      const tail = String(ai.model || "on").split("/").pop().replace(/:free$/, "");
      pill.textContent = /nemotron/i.test(tail) ? "AI · Nemotron" : "AI · " + tail;
      pill.title = `Open-ended requests go to ${ai.provider || "an AI service"} (${ai.model}). Built-in commands stay on this server.`;
      pill.classList.add("on");
    } else {
      pill.textContent = "Built-in commands";
      pill.title = "The AI assistant isn't connected on this server (set OPENROUTER_API_KEY). Plain commands still work.";
      pill.classList.remove("on");
    }
  }

  (function split() {
    const bar = $("split");
    const root = document.documentElement;
    const clamp = (v) => Math.max(320, Math.min(v, Math.min(820, window.innerWidth * 0.7)));
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

  // ── live: other people in the same session ─────────────────────────────
  let evAbort = null;
  let evRetry = 0;
  let lastPresence = 0;
  let presenceTimer = 0;
  function sendPresence() {
    if (!sid() || S.people.length < 2 || S.readOnly) return;
    const now = Date.now();
    if (now - lastPresence < 120) {
      clearTimeout(presenceTimer);
      presenceTimer = setTimeout(sendPresence, 130);
      return;
    }
    lastPresence = now;
    const m = S.mouse || {};
    fetch(`${API}/api/editor/sessions/${sid()}/presence`, {
      method: "POST", headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({ client: CLIENT, name: myName() || "Guest", x: m.x, y: m.y, selection: [...S.selected].slice(0, 200) }),
    }).catch(() => {});
  }
  function renderPeople() {
    const box = $("people");
    box.replaceChildren();
    const others = S.people.filter((p) => p.client !== CLIENT);
    for (const p of others.slice(0, 6)) {
      const a = el("span", "ed-avatar", (p.name || "G").slice(0, 1).toUpperCase());
      a.style.background = p.color;
      a.title = `${p.name || "Guest"} is here`;
      box.append(a);
    }
    if (others.length) box.append(el("span", "muted small", others.length === 1 ? "1 other here" : `${others.length} others here`));
  }
  function disconnectEvents() {
    if (evAbort) { evAbort.abort(); evAbort = null; }
    S.people = []; S.cursors = {};
    renderPeople();
  }
  async function connectEvents() {
    disconnectEvents();
    if (!sid() || typeof AbortController === "undefined") return;
    const ac = new AbortController();
    evAbort = ac;
    const id = sid();
    try {
      const res = await fetch(`${API}/api/editor/sessions/${id}/events?client=${CLIENT}&name=${encodeURIComponent(myName() || "Guest")}`, { headers: headers(), signal: ac.signal });
      if (!res.ok || !res.body) return;
      evRetry = 0;
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          let kind = null, data = null;
          for (const line of block.split("\n")) {
            if (line.startsWith("event:")) kind = line.slice(6).trim();
            else if (line.startsWith("data:")) { try { data = JSON.parse(line.slice(5)); } catch (e) { data = null; } }
          }
          if (kind) onLiveEvent(kind, data || {});
        }
      }
    } catch (e) {
      if (ac.signal.aborted) return;
    }
    if (evAbort === ac && sid() === id) { // dropped: reconnect with backoff
      evRetry = Math.min(evRetry + 1, 6);
      setTimeout(() => { if (evAbort === ac && sid() === id) connectEvents(); }, 1000 * 2 ** evRetry);
    }
  }
  function onLiveEvent(kind, d) {
    const who = d.by || d.name || "Someone";
    if (kind === "hello") { S.people = d.people || []; renderPeople(); }
    else if (kind === "presence") {
      if (d.left) { S.people = S.people.filter((p) => p.client !== d.client); delete S.cursors[d.client]; }
      else if (d.joined) { S.people = S.people.filter((p) => p.client !== d.client).concat([d]); toast(`${d.name || "Someone"} joined.`); }
      renderPeople();
      requestDraw();
    } else if (kind === "cursor") {
      S.cursors[d.client] = { x: d.x, y: d.y, name: d.name, color: d.color, t: Date.now() };
      if (!S.people.some((p) => p.client === d.client)) { S.people.push({ client: d.client, name: d.name, color: d.color }); renderPeople(); }
      requestDraw();
    } else if (kind === "chat") {
      addMsg("user other", `${who}: ${d.message}`);
      if (d.reply) addMsg("bot", d.reply, "Answer to " + who);
      renderData(d.data);
      if (d.proposal) showProposal(d.proposal, who);
      if ($("pane-chat").hidden) toast(d.proposal ? `${who} proposed a change. It's in the Assistant tab.` : `${who} asked: ${String(d.message).slice(0, 80)}`);
    } else if (kind === "proposal") {
      if (d.proposal) showProposal(d.proposal, who);
      if (d.proposal && $("pane-chat").hidden) toast(`${who} proposed a change. It's in the Assistant tab.`);
    } else if (kind === "changed") {
      const card = d.proposal && cards.get(d.proposal);
      if (card) retire(card, `Applied by ${who}`, "ok");
      else if (activeCard) retire(activeCard, `Dropped: ${who} changed the drawing`, "no");
      addMsg("sys", `${who} ${d.what === "accepted" ? "applied" : d.what}: ${(d.summaries || [d.label || ""]).join("; ")}`);
      refresh();
    } else if (kind === "rejected") {
      const card = cards.get(d.proposal);
      if (card) retire(card, `Rejected by ${who}`, "no");
    }
    hooks.event.forEach((fn) => fn(kind, d));
  }

  // ── export dialog (older AutoCAD versions) ─────────────────────────────
  const dlg = $("export-dlg");
  let exportPoll = 0;
  let exportJob = null;
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
    const fail = (err) => { $("export-status").textContent = err.message; $("export-go").disabled = false; };
    try {
      let job = await post(`/api/editor/sessions/${sid()}/export`, { target, format });
      const tick = async () => {
        job = await api(`/api/jobs/${job.id}`);
        const pct = Math.round(job.progress || 0);
        $("export-fill").style.width = pct + "%";
        $("export-bar").setAttribute("aria-valuenow", String(pct));
        if (job.status === "done") {
          exportJob = job;
          const c = job.result.counts || {};
          $("export-status").textContent = `Ready: ${job.result.outputName}` + (c.skipped ? ` · ${c.skipped} item${c.skipped === 1 ? "" : "s"} skipped (see the report)` : " · nothing skipped");
          $("export-dl").hidden = false;
          $("export-report").hidden = false;
          $("export-go").disabled = false;
        } else if (job.status === "failed" || job.status === "cancelled") {
          fail(new Error((job.error && job.error.message) || "The conversion failed."));
        } else exportPoll = setTimeout(() => tick().catch(fail), 500);
      };
      await tick();
    } catch (err) { fail(err); }
  }
  $("export-btn").addEventListener("click", openExport);
  $("export-go").addEventListener("click", runExport);
  $("export-dl").addEventListener("click", () => exportJob && download(exportJob.result.downloadUrl, exportJob.result.outputName));
  $("export-report").addEventListener("click", () => exportJob && download(exportJob.result.reportUrl, "report.txt"));
  $("export-close").addEventListener("click", () => { clearTimeout(exportPoll); dlg.close ? dlg.close() : dlg.removeAttribute("open"); });
  dlg.addEventListener("close", () => clearTimeout(exportPoll));
  $("dl-btn").addEventListener("click", () => download(`/api/editor/sessions/${sid()}/download.dxf`, "drawing.dxf"));
  $("close-btn").addEventListener("click", closeDrawing);

  // ── open-file controls ─────────────────────────────────────────────────
  $("choose-btn").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", (e) => { openFile(e.target.files[0]); e.target.value = ""; });
  $("sample-btn").addEventListener("click", openSample);
  const viewPane = $("view");
  for (const ev of ["dragenter", "dragover"]) viewPane.addEventListener(ev, (e) => { if (S.readOnly) return; e.preventDefault(); $("drop").classList.add("is-over"); });
  for (const ev of ["dragleave", "drop"]) viewPane.addEventListener(ev, (e) => { e.preventDefault(); $("drop").classList.remove("is-over"); });
  viewPane.addEventListener("drop", (e) => { if (S.readOnly || S.session) return; const f = e.dataTransfer && e.dataTransfer.files[0]; if (f) openFile(f); });

  // ── public interface for editor-tools.js and editor-review.js ─────────
  window.ED = {
    S, API, $, el, btn, fmt, api, post, download, headers, toast, hooks, CLIENT,
    sid, myName, accessKey, workspaceKey, local, randomId,
    prep, buildScene, setScene, requestDraw, fitTo, fitView, viewport, overlaps, inkFor,
    setSelection, selectAndShow, setArea, addMsg, renderData, tableEl, healthEl, showProposal, showLegend, stageOps,
    send, selectTab, refresh, adopt, openSummary, show, busy, renderSuggestions, renderPeople, connectEvents, disconnectEvents,
    updateAiPill, resize, setComposer, unitShort,
  };

  // ── start ──────────────────────────────────────────────────────────────
  new ResizeObserver(resize).observe(cv);
  window.addEventListener("resize", resize);
  setComposer(false);
  log.replaceChildren(el("div", "msg sys", "Open a drawing to start. Then describe a change, or click things on the drawing to select them."));
  async function init() {
    try { S.config = await api("/api/editor/config"); } catch (err) {
      S.config = { ai: { enabled: false }, targets: [], formats: [] };
      dropError("The editing server isn't reachable. The editor needs the Backdate.dwg server (see the README).");
      $("choose-btn").disabled = true; $("sample-btn").disabled = true;
    }
    updateAiPill();
    $("privacy").textContent = S.config.ai.enabled
      ? `Your drawing stays on this server. For open-ended requests, your message and a summary of the drawing (layers, counts, text labels) are sent to ${S.config.ai.provider || "the AI service"}. Built-in commands never leave the server.`
      : "Your drawing stays on this server. The AI assistant isn't connected here, so built-in commands only (rename or purge layers, move, scale, replace text, health check, take-off, and so on).";
    if (params.get("share")) { if (window.EDReview) window.EDReview.start(params.get("share")); return; }
    if (params.get("join")) {
      busy("Joining…");
      try {
        const j = await api(`/api/editor/join/${encodeURIComponent(params.get("join"))}`);
        if (!myName()) { const n = window.prompt("Your name (shown to the others editing):", ""); if (n) local("bd-name", n.slice(0, 40)); }
        busy("");
        history_replace();
        await openSummary(await api(`/api/editor/sessions/${j.sessionId}`), false, true);
      } catch (err) { busy(""); dropError(err.message); }
      return;
    }
    const saved = store("ed-session");
    if (saved) {
      try { await openSummary(await api(`/api/editor/sessions/${saved}`), false); } catch (e) { store("ed-session", null); }
    }
  }
  function history_replace() {
    try { window.history.replaceState(null, "", location.pathname); } catch (e) { /* cosmetic */ }
  }
  document.addEventListener("DOMContentLoaded", init);
})();
