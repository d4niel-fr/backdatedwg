"""Exports: a to-scale PDF with a title block, SVG, a change-log PDF, and the proof pack.

The PDF writer is a small hand-written one (vector lines and Helvetica text,
Flate-compressed), so the server needs no extra dependencies.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import time
import zipfile
import zlib
from datetime import datetime, timezone
from typing import Iterable, Optional
from xml.sax.saxutils import escape

from .geometry import Scene

MM = 72 / 25.4  # PDF points per millimetre

PAPERS = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594), "A0": (1189, 841),
          "LETTER": (279.4, 215.9), "TABLOID": (431.8, 279.4)}
SCALES = [1, 2, 5, 10, 20, 25, 50, 75, 100, 125, 150, 200, 250, 300, 400, 500, 750, 1000, 1250, 1500, 2000, 2500, 5000, 10000, 20000, 50000, 100000]


# ── minimal PDF writer ──────────────────────────────────────────────────────

_TRANSLATE = str.maketrans({"→": "->", "←": "<-", "✓": "v", "≥": ">=", "≤": "<=", "≈": "~", "△": "^", "\u2009": " ", "\u00a0": " "})


def _pdf_text(s: str) -> bytes:
    raw = str(s).translate(_TRANSLATE).encode("cp1252", "replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)").replace(b"\r", b"").replace(b"\n", b" ")


def _rgb(hex_color: Optional[str]) -> tuple[float, float, float]:
    if not hex_color:
        return (0.0, 0.0, 0.0)
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    if lum > 0.6:  # pale colours vanish on white paper: pull them toward black
        k = min(0.75, (lum - 0.5) * 1.6)
        r, g, b = (c * (1 - k) for c in (r, g, b))
    return (round(r, 3), round(g, 3), round(b, 3))


class Page:
    def __init__(self, width_pt: float, height_pt: float):
        self.w, self.h = width_pt, height_pt
        self.ops: list[bytes] = []

    def raw(self, s: str) -> None:
        self.ops.append(s.encode("ascii"))

    def stroke_rgb(self, rgb) -> None:
        self.raw("%.3f %.3f %.3f RG" % rgb)

    def fill_rgb(self, rgb) -> None:
        self.raw("%.3f %.3f %.3f rg" % rgb)

    def width(self, w: float) -> None:
        self.raw("%.3f w" % w)

    def path(self, pts: list[tuple[float, float]], closed: bool = False) -> None:
        if len(pts) < 2:
            return
        out = ["%.2f %.2f m" % pts[0]] + ["%.2f %.2f l" % p for p in pts[1:]]
        if closed:
            out.append("h")
        self.ops.append(" ".join(out).encode("ascii"))

    def stroke(self) -> None:
        self.raw("S")

    def rect(self, x: float, y: float, w: float, h: float, fill: bool = False) -> None:
        self.raw("%.2f %.2f %.2f %.2f re %s" % (x, y, w, h, "f" if fill else "S"))

    def line(self, x1, y1, x2, y2) -> None:
        self.raw("%.2f %.2f m %.2f %.2f l S" % (x1, y1, x2, y2))

    def text(self, x: float, y: float, size: float, s: str, bold: bool = False, angle: float = 0.0) -> None:
        font = "F2" if bold else "F1"
        if angle:
            c, si = math.cos(math.radians(angle)), math.sin(math.radians(angle))
            tm = "%.4f %.4f %.4f %.4f %.2f %.2f Tm" % (c, si, -si, c, x, y)
        else:
            tm = "1 0 0 1 %.2f %.2f Tm" % (x, y)
        self.ops.append(b"BT /" + font.encode() + b" %.2f Tf " % size + tm.encode() + b" (" + _pdf_text(s) + b") Tj ET")

    def clip(self, x: float, y: float, w: float, h: float) -> None:
        self.raw("q %.2f %.2f %.2f %.2f re W n" % (x, y, w, h))

    def unclip(self) -> None:
        self.raw("Q")


class PDF:
    def __init__(self, title: str = ""):
        self.pages: list[Page] = []
        self.title = title

    def page(self, w_pt: float, h_pt: float) -> Page:
        p = Page(w_pt, h_pt)
        self.pages.append(p)
        return p

    def render(self) -> bytes:
        objs: list[bytes] = []

        def add(body: bytes) -> int:
            objs.append(body)
            return len(objs)

        catalog = add(b"")  # filled later
        pages_id = add(b"")
        f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        kids = []
        for p in self.pages:
            content = zlib.compress(b"\n".join(p.ops), 6)
            cid = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(content) + content + b"\nendstream")
            pid = add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Contents %d 0 R /Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> >>"
                      % (pages_id, p.w, p.h, cid, f1, f2))
            kids.append(pid)
        objs[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id
        objs[pages_id - 1] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)
        info = add(b"<< /Producer (Backdate.dwg AI editor) /Title (" + _pdf_text(self.title) + b") /CreationDate (D:" + time.strftime("%Y%m%d%H%M%S").encode() + b") >>")
        out = io.BytesIO()
        out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objs, 1):
            offsets.append(out.tell())
            out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
        xref = out.tell()
        out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
        for off in offsets:
            out.write(b"%010d 00000 n \n" % off)
        out.write(b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, catalog, info, xref))
        return out.getvalue()


# ── drawing to PDF ──────────────────────────────────────────────────────────


def nice_scale(ratio: float) -> int:
    for n in SCALES:
        if n >= ratio:
            return n
    return int(math.ceil(ratio / 100000) * 100000)


def _visible(scene: Scene, hidden: set[str]) -> list[dict]:
    return [it for it in scene.items if it["l"] not in hidden]


def _extents(items: list[dict]) -> Optional[list[float]]:
    xs, ys = [], []
    for it in items:
        if it["k"] == "p":
            xs += it["p"][0::2]
            ys += it["p"][1::2]
        else:
            xs.append(it["x"])
            ys.append(it["y"])
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def drawing_pdf(scene: Scene, *, units_to_m: float, units_name: str, units_guessed: bool, name: str,
                hidden: Iterable[str] = (), paper: str = "A3", orientation: str = "landscape",
                fields: Optional[dict] = None) -> tuple[bytes, dict]:
    """The drawing on one sheet, to a standard scale, with a title block. Returns (pdf, info)."""
    hidden = set(hidden)
    paper = paper.upper() if paper.upper() in PAPERS else "A3"
    long, short = PAPERS[paper]
    pw, ph = (long, short) if orientation != "portrait" else (short, long)
    margin, tb_h = 10.0, 28.0
    area_w, area_h = pw - 2 * margin, ph - 2 * margin - tb_h
    items = _visible(scene, hidden)
    ext = _extents(items) or [0, 0, 1, 1]
    ew, eh = max(ext[2] - ext[0], 1e-9), max(ext[3] - ext[1], 1e-9)
    # paper millimetres per drawing unit at 1:1 is units_to_m * 1000
    ratio = max(ew * units_to_m * 1000 / area_w, eh * units_to_m * 1000 / area_h)
    n = nice_scale(ratio)
    k = units_to_m * 1000 / n * MM  # points per drawing unit
    ox = margin * MM + (area_w * MM - ew * k) / 2 - ext[0] * k
    oy = (margin + tb_h) * MM + (area_h * MM - eh * k) / 2 - ext[1] * k

    pdf = PDF(name)
    pg = pdf.page(pw * MM, ph * MM)
    pg.width(0.8)
    pg.stroke_rgb((0, 0, 0))
    pg.rect(margin * MM, margin * MM, (pw - 2 * margin) * MM, (ph - 2 * margin) * MM)
    pg.clip(margin * MM, (margin + tb_h) * MM, area_w * MM, area_h * MM)
    pg.width(0.25)
    by_color: dict = {}
    for it in items:
        if it["k"] == "p":
            by_color.setdefault(_rgb(it.get("c")), []).append(it)
    for rgb, group in by_color.items():
        pg.stroke_rgb(rgb)
        for it in group:
            p = it["p"]
            pts = [(ox + p[i] * k, oy + p[i + 1] * k) for i in range(0, len(p), 2)]
            pg.path(pts, bool(it["z"]))
            pg.stroke()
    for it in items:
        if it["k"] != "t":
            continue
        size = it["s"] * k
        if size < 1.0:
            continue
        pg.fill_rgb(_rgb(it.get("c")))
        for li, ln in enumerate(it["v"].split("\n")[:12]):
            dx = math.sin(math.radians(it["r"])) * li * size * 1.3
            dy = -math.cos(math.radians(it["r"])) * li * size * 1.3
            pg.text(ox + it["x"] * k + dx, oy + it["y"] * k + dy, size, ln[:200], angle=it["r"])
    pg.unclip()

    # title block
    f = fields or {}
    pg.fill_rgb((0, 0, 0))
    pg.width(0.6)
    y0 = margin * MM
    pg.line(margin * MM, (margin + tb_h) * MM, (pw - margin) * MM, (margin + tb_h) * MM)
    cols = [
        ("TITLE", f.get("title") or name, 0.34),
        ("PROJECT", f.get("project") or "", 0.2),
        ("DRAWN BY", f.get("drawn_by") or "", 0.12),
        ("DATE", f.get("date") or datetime.now().strftime("%Y-%m-%d"), 0.1),
        ("REV", f.get("rev") or "", 0.06),
        ("SCALE", f"1:{n} @ {paper}" + (" (units guessed)" if units_guessed else ""), 0.18),
    ]
    x = margin * MM
    total = (pw - 2 * margin) * MM
    for label, value, frac in cols:
        w = total * frac
        pg.text(x + 2 * MM, y0 + (tb_h - 6) * MM, 6, label, bold=True)
        pg.text(x + 2 * MM, y0 + (tb_h - 15) * MM, 11 if label == "TITLE" else 9, str(value)[:60])
        x += w
        if label != "SCALE":
            pg.line(x, y0, x, y0 + tb_h * MM)
    pg.text(margin * MM + 2 * MM, y0 + 3 * MM, 6, f"Units: {units_name}. Exported {datetime.now().strftime('%Y-%m-%d %H:%M')} by Backdate.dwg AI editor."
            + (f" Hidden layers: {', '.join(sorted(hidden))[:120]}" if hidden else ""))
    # scale bar: a round length near 1/5 of the sheet
    target_units = (area_w / 5) / (units_to_m * 1000 / n)
    step = 10 ** math.floor(math.log10(max(target_units, 1e-9)))
    for m in (5, 2, 1):
        if m * step <= target_units:
            step *= m
            break
    bar_pt = step * k
    bx, byy = (pw - margin) * MM - bar_pt - 6 * MM, y0 + 3 * MM
    pg.width(1.2)
    pg.line(bx, byy, bx + bar_pt, byy)
    pg.line(bx, byy - 2, bx, byy + 2)
    pg.line(bx + bar_pt, byy - 2, bx + bar_pt, byy + 2)
    label = f"{step * units_to_m:g} m" if units_to_m else f"{step:g}"
    pg.text(bx + bar_pt / 2 - 8, byy + 3, 6, label)
    return pdf.render(), {"scale": n, "paper": paper, "orientation": orientation, "items": len(items)}


def changelog_pdf(name: str, log: list[dict], summary_line: str) -> bytes:
    pdf = PDF(f"{name} – change log")
    w, h = 210 * MM, 297 * MM
    lines: list[tuple[str, float, bool]] = [(f"Change log: {name}", 16, True), (summary_line, 9, False), ("", 9, False)]
    if not log:
        lines.append(("No changes were accepted.", 10, False))
    for e in log:
        when = datetime.fromtimestamp(e.get("time", 0)).strftime("%Y-%m-%d %H:%M")
        lines.append((f"Change {e.get('rev')} · {when} · {e.get('source', '')}", 10, True))
        if e.get("prompt"):
            lines += [(chunk, 9, False) for chunk in _wrap(f'Asked: "{e["prompt"]}"', 100)]
        for s in e.get("summaries", []):
            lines += [(chunk, 9, False) for chunk in _wrap("• " + s, 100)]
        lines.append(("", 6, False))
    page = None
    y = 0.0
    for text, size, bold in lines:
        if page is None or y < 20 * MM:
            page = pdf.page(w, h)
            page.fill_rgb((0, 0, 0))
            y = h - 20 * MM
        if text:
            page.text(18 * MM, y, size, text, bold=bold)
        y -= size * 1.5
    return pdf.render()


def _wrap(text: str, width: int) -> list[str]:
    words = str(text).split()
    out, cur = [], ""
    for wd in words:
        if len(cur) + len(wd) + 1 > width and cur:
            out.append(cur)
            cur = "   " + wd
        else:
            cur = (cur + " " + wd).strip() if not cur.startswith("   ") else cur + " " + wd
    if cur:
        out.append(cur)
    return out or [""]


# ── SVG ─────────────────────────────────────────────────────────────────────


def drawing_svg(scene: Scene, hidden: Iterable[str] = (), title: str = "") -> str:
    hidden = set(hidden)
    items = _visible(scene, hidden)
    ext = _extents(items) or [0, 0, 1, 1]
    w, h = max(ext[2] - ext[0], 1e-9), max(ext[3] - ext[1], 1e-9)
    pad = max(w, h) * 0.02
    X = lambda x: x - ext[0] + pad  # noqa: E731
    Y = lambda y: ext[3] - y + pad  # noqa: E731
    layers: dict[str, list[str]] = {}
    for it in items:
        color = it.get("c") or "#000000"
        if it["k"] == "p":
            p = it["p"]
            pts = " ".join(f"{X(p[i]):.3f},{Y(p[i + 1]):.3f}" for i in range(0, len(p), 2))
            tag = "polygon" if it["z"] else "polyline"
            layers.setdefault(it["l"], []).append(f'<{tag} points="{pts}" stroke="{color}" vector-effect="non-scaling-stroke" data-handle="{escape(it["h"])}"/>')
        else:
            lines = escape(it["v"]).split("\n")
            tspans = "".join(f'<tspan x="0" dy="{0 if i == 0 else it["s"] * 1.3:.3f}">{ln}</tspan>' for i, ln in enumerate(lines[:12]))
            layers.setdefault(it["l"], []).append(
                f'<text transform="translate({X(it["x"]):.3f},{Y(it["y"]):.3f}) rotate({-it["r"]:.2f})" font-size="{it["s"]:.3f}" fill="{color}" data-handle="{escape(it["h"])}">{tspans}</text>')
    vw, vh = w + 2 * pad, h + 2 * pad
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vw:.3f} {vh:.3f}" width="1600" height="{1600 * vh / vw:.0f}">',
           f"<title>{escape(title)}</title>",
           f'<rect x="0" y="0" width="{vw:.3f}" height="{vh:.3f}" fill="#ffffff"/>',
           '<g fill="none" stroke-width="1" font-family="Helvetica, Arial, sans-serif">']
    for layer in sorted(layers):
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in layer)
        out.append(f'<g id="layer-{safe}" data-layer="{escape(layer)}">')
        out += layers[layer]
        out.append("</g>")
    out.append("</g></svg>")
    return "\n".join(out)


# ── proof pack ──────────────────────────────────────────────────────────────


def proof_pack(files: dict[str, bytes], meta: dict) -> bytes:
    """A zip of the files plus a manifest with SHA-256 checksums."""
    buf = io.BytesIO()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    manifest = ["Backdate.dwg AI editor – proof pack", f"Created: {stamp}"]
    manifest += [f"{k}: {v}" for k, v in meta.items()]
    manifest += ["", "Files (SHA-256):"]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, data in files.items():
            z.writestr(path, data)
            manifest.append(f"  {hashlib.sha256(data).hexdigest()}  {path}")
        z.writestr("MANIFEST.txt", "\n".join(manifest) + "\n")
        z.writestr("meta.json", json.dumps({**meta, "created": stamp}, indent=2))
    return buf.getvalue()
