"""Command line for the AI editor's tools.

    python -m app.editor.cli health   plan.dwg
    python -m app.editor.cli explain  plan.dxf
    python -m app.editor.cli takeoff  plan.dxf --csv quantities.csv
    python -m app.editor.cli apply    plan.dwg --recipe office.json --out cleaned/
    python -m app.editor.cli apply    plan.dxf --command "purge unused layers" --command "delete duplicates" --out cleaned/
    python -m app.editor.cli batch    drawings/ --recipe office.json --out cleaned/
    python -m app.editor.cli audit    drawings/ [--email cad-manager@example.com]
    python -m app.editor.cli watch    hotfolder/ --recipe office.json
    python -m app.editor.cli compare  old.dxf new.dxf
    python -m app.editor.cli pdf      plan.dxf --out plan.pdf [--paper A1]

DWG files need ODA File Converter or LibreDWG, exactly as the server does.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

from ..engines import Engines
from . import analysis, batch, compare, exports
from .digest import build
from .geometry import extract
from .health import check
from .units import detect_units


def _open(path: Path, engines: Engines, tmp: Path):
    doc, det, notes = batch.load_drawing(path, engines, tmp)
    scene = extract(doc)
    ext = scene.extents
    units = detect_units(doc.header.get("$INSUNITS", 0), max(ext[2] - ext[0], ext[3] - ext[1]) if ext else None)
    return doc, scene, units, build(doc, scene, units, path.name)


def _recipe(args) -> dict:
    if args.recipe:
        return batch.validate_recipe(Path(args.recipe).read_text("utf-8"))
    if args.command:
        out = {"format": (args.format or "DXF").upper(), "target": args.target}
        return batch.validate_recipe({"name": "Command line", "commands": args.command, "output": out})
    raise batch.RecipeError("Give --recipe FILE or at least one --command.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.editor.cli", description="Backdate.dwg AI editor tools")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("health", "explain", "takeoff", "rooms", "aisles"):
        s = sub.add_parser(name)
        s.add_argument("file")
        s.add_argument("--json", action="store_true")
        if name == "takeoff":
            s.add_argument("--csv")
    s = sub.add_parser("apply")
    s.add_argument("file")
    s.add_argument("--recipe")
    s.add_argument("--command", action="append")
    s.add_argument("--format")
    s.add_argument("--target", type=int)
    s.add_argument("--out", required=True)
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("batch")
    s.add_argument("folder")
    s.add_argument("--recipe")
    s.add_argument("--command", action="append")
    s.add_argument("--format")
    s.add_argument("--target", type=int)
    s.add_argument("--out", required=True)
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("audit")
    s.add_argument("folder")
    s.add_argument("--csv")
    s.add_argument("--email")
    s = sub.add_parser("watch")
    s.add_argument("folder")
    s.add_argument("--recipe", required=True)
    s.add_argument("--interval", type=float, default=30)
    s.add_argument("--once", action="store_true")
    s = sub.add_parser("compare")
    s.add_argument("old")
    s.add_argument("new")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("pdf")
    s.add_argument("file")
    s.add_argument("--out", required=True)
    s.add_argument("--paper", default="A3")
    s.add_argument("--portrait", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("ezdxf").setLevel(logging.ERROR)
    engines = Engines.discover()
    tmp = Path(tempfile.mkdtemp(prefix="backdate-cli-"))
    try:
        return _run(args, engines, tmp)
    except (batch.RecipeError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(args, engines: Engines, tmp: Path) -> int:
    out = sys.stdout
    if args.cmd in ("health", "explain", "takeoff", "rooms", "aisles"):
        doc, scene, units, digest = _open(Path(args.file), engines, tmp)
        if args.cmd == "health":
            h = check(doc, scene, units, digest)
            if args.json:
                json.dump(h, out, indent=2)
            else:
                print(f"{args.file}: health {h['score']}/100, {h['issues']} issue(s)", file=out)
                for f in h["findings"]:
                    print(f"  [{f['severity']}] {f['title']} — {f['detail']}" + (f"  (fix: {f['fix']['label']})" if f["fix"] else ""), file=out)
            return 0 if h["score"] >= 80 else 1
        if args.cmd == "explain":
            h = check(doc, scene, units, digest)
            e = analysis.explain(doc, digest, units, {"score": h["score"], "issues": h["issues"]})
            print(json.dumps(e, indent=2) if args.json else "\n\n".join(e["paragraphs"]), file=out)
            return 0
        if args.cmd == "takeoff":
            t = analysis.takeoff(doc, units)
            if args.csv:
                Path(args.csv).write_text(analysis.takeoff_csv(t), "utf-8")
                print(f"wrote {args.csv}", file=out)
            print(json.dumps(t, indent=2) if args.json else analysis.takeoff_csv(t), file=out)
            return 0
        if args.cmd == "rooms":
            rows = analysis.rooms(doc, units)
            if args.json:
                json.dump(rows, out, indent=2)
            else:
                for r in rows:
                    print(f"{r['name']:<30} {r['squareMetres']:>10.2f} m²  ({r['layer']})", file=out)
            return 0
        w = analysis.warehouse(doc, units)
        print(json.dumps(w, indent=2) if args.json else w["message"], file=out)
        return 0 if not w.get("narrow") else 1

    if args.cmd in ("apply", "batch"):
        recipe = _recipe(args)
        files = [Path(args.file)] if args.cmd == "apply" else sorted(f for f in Path(args.folder).rglob("*") if f.suffix.lower() in batch.DRAWING_EXT)
        dest = Path(args.out)
        # `apply one.dxf --out edited.dxf` names the file; otherwise --out is a folder
        single = args.cmd == "apply" and dest.suffix.lower() in batch.DRAWING_EXT
        (dest.parent if single else dest).mkdir(parents=True, exist_ok=True)
        results = []
        for i, f in enumerate(files):
            work = tmp / str(i)
            r = batch.process_file(f, recipe, engines, work, dry_run=args.dry_run)
            if r.get("output"):
                shutil.copyfile(work / "out" / r["output"], dest if single else dest / r["output"])
            results.append(r)
        report = batch.report_text(recipe, results, args.dry_run)
        if not single:
            (dest / "report.txt").write_text(report, "utf-8")
        print(report, file=out)
        if single and results and results[0].get("output"):
            print(f"wrote {dest}", file=out)
        return 0 if all(r["status"] == "done" for r in results) else 1

    if args.cmd == "audit":
        rows, text, csv_text = batch.audit_folder(Path(args.folder), engines)
        print(text, file=out)
        if args.csv:
            Path(args.csv).write_text(csv_text, "utf-8")
        if args.email:
            batch.send_email(args.email, f"Drawing audit: {Path(args.folder).name} ({len(rows)} drawings)", text, {"audit.csv": csv_text.encode("utf-8")})
            print(f"emailed {args.email}", file=out)
        return 0

    if args.cmd == "watch":
        recipe = batch.validate_recipe(Path(args.recipe).read_text("utf-8"))
        if args.once:
            for r in batch.watch_once(Path(args.folder), recipe, engines):
                print(f"{r['file']}: {r['status']} {r.get('output', '')}", file=out)
            return 0
        w = batch.FolderWatcher(Path(args.folder), recipe, engines, args.interval)
        print(f"Watching {args.folder}/in every {args.interval:g}s. Ctrl+C to stop.", file=out)
        w.start()
        try:
            w.join()
        except KeyboardInterrupt:
            w.stop_event.set()
        return 0

    if args.cmd == "compare":
        a = _open(Path(args.old), engines, tmp / "a")[0]
        b = _open(Path(args.new), engines, tmp / "b")[0]
        r = compare.compare(a, b)
        if args.json:
            json.dump({k: v for k, v in r.items() if k != "overlay"}, out, indent=2)
        else:
            print(compare.report_text(r, args.old, args.new), file=out)
        return 0

    if args.cmd == "pdf":
        doc, scene, units, _d = _open(Path(args.file), engines, tmp)
        data, info = exports.drawing_pdf(scene, units_to_m=units.to_m, units_name=units.name, units_guessed=units.guessed,
                                         name=Path(args.file).name, paper=args.paper, orientation="portrait" if args.portrait else "landscape")
        Path(args.out).write_bytes(data)
        print(f"wrote {args.out} at 1:{info['scale']} on {info['paper']}", file=out)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
