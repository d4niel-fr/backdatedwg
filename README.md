# Backdate.dwg

Upload an AutoCAD **DWG** or **DXF** file and save it as an older release, for example an AutoCAD 2026 drawing saved as an AutoCAD 2010 DWG. If something can't be converted, the app skips it and keeps going. Everything it skipped, changed or repaired is listed in a conversion report that you can also download as `.txt`.

Upload → Converting → Download, built from the design handoff in [`design/`](design/) (Organic design system).

## Run it

### Vercel (static, converts in the browser)

Import the repo on Vercel. `vercel.json` builds a static site (`scripts/build-static.sh` → `dist/`), with no Python server. With no API available, the page converts files **on the visitor's own device**:

- **DWG → DXF:** LibreDWG compiled to WebAssembly.
- **Older version:** the same Python code as the server's fallback engine (`app/browser.py`), running in Pyodide inside a Web Worker.

Files are never uploaded, so there's no size cap from Vercel.

What it can't do: **save as DWG**. Writing DWG needs ODA File Converter, which only runs on a server. On Vercel, files are saved as DXF in the chosen version, which every AutoCAD release opens directly. For DWG output, deploy the Docker image instead (below). The first conversion downloads about 30 MB of converter files, which the browser caches after that.

### Render (easiest)

On [render.com](https://render.com) choose **New + → Blueprint** and pick this repo. It uses `render.yaml` and builds the Dockerfile. The first build takes a few minutes because it compiles LibreDWG.

### Docker (recommended: includes the DWG engines)

```bash
docker build -t backdate .
docker run -p 8000:8000 backdate
# open http://localhost:8000
```

or `docker compose up --build`.

**DWG output needs ODA File Converter.** This is the free (proprietary) converter from the Open Design Alliance. The build downloads its Linux `.deb` from `ODA_DEB_URL`. ODA renames the file with each release, so if the build prints `couldn't download ODA File Converter`, get the current link for **Linux · Qt6 · x64 .deb** from <https://www.opendesign.com/guestfiles/oda_file_converter>. Then either:

```bash
docker build --build-arg ODA_DEB_URL="https://www.opendesign.com/guestfiles/get?filename=ODAFileConverter_QT6_lnxX64_..." -t backdate .
```

or put the downloaded `.deb` into `vendor/` and build again. Add `--build-arg REQUIRE_ODA=1` to make the build fail rather than ship without it. You can check what's active at `GET /healthz`.

### Without Docker

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

To convert DWG files, install ODA File Converter (it's found on `PATH`, in its standard install folders, or through `ODA_CONVERTER_PATH`). On a headless Linux server you also need `xvfb` (`xvfb-run`). LibreDWG's `dwg2dxf` on `PATH` is used as a fallback DWG reader.

## AI editor (beta)

`/editor.html` opens a drawing next to a chat, so you can edit it by asking. It is a separate feature: the converter above is unchanged, and the editor only reuses its file reader and its export queue.

1. **Open** a DWG or DXF (or try the built-in sample warehouse). DWG needs ODA File Converter or LibreDWG on the server, like the converter.
2. **Ask**: "move layer S-RACK 2 m east", "rename layer TEMP to NOTES", "replace "REV A" with "REV B"", "purge unused layers", or, with the AI connected, open-ended requests such as "tidy this up". Click things on the drawing first (click, Shift-click, or drag a box: left-to-right must enclose, right-to-left touches) and say what to do with them.
3. **Review.** Nothing is applied yet. The drawing shows what would be removed or moved (dashed orange) and where it ends up (green), and the chat lists the changes in words, with the exact operations one click away.
4. **Accept or reject.** Accepted changes go on an undo/redo history and a change log.
5. **Save** as DXF, or as an older release (DWG with ODA), through the same conversion and report as the converter.

How the AI is kept safe: the model never edits the file. It sees a summary of the drawing (units, size, layers, block names, text labels) and can ask questions through read-only queries. To change anything it must answer with operations from a fixed list (`move`, `copy`, `array`, `rotate`, `scale`, `delete`, `set_layer`, `set_color`, `create_layer`, `layer_props`, `rename_layer`, `purge_unused_layers`, `add_line`/`add_polyline`/`add_rect`/`add_circle`/`add_text`, `replace_text`). Each is validated and run on a *copy* of the drawing, and only a person pressing Accept commits it. Text inside the drawing is treated as data, never as instructions. A plain-command parser answers first, so common requests are instant, free and work without any key.

### Connecting the AI (NVIDIA Nemotron)

| Variable | Default | |
| --- | --- | --- |
| `NVIDIA_API_KEY` | none | Key from build.nvidia.com. **Without it the editor still works with the built-in commands.** Keep it in the host's secret store, never in the repo. |
| `NEMOTRON_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b` | Copy the exact id from the model card if it differs. |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | Any OpenAI-compatible server works, for example a self-hosted vLLM, so drawings never leave your network. |
| `NVIDIA_EXTRA_BODY` | none | JSON merged into each request, for model-specific switches. |
| `NVIDIA_TIMEOUT` | `120` | Seconds to wait for one model call. |
| `BACKDATE_EDITOR_AI_LIMIT` | `100` | Model calls per editing session. |
| `BACKDATE_EDITOR_MAX_SESSIONS` | `20` | Open drawings kept in memory. |

With the AI on, a message and a summary of the drawing (layers, counts, text labels, never the file) go to the AI service. The editor page says so.

Limits for now: sessions live in memory for an hour (single server process, like the converter's jobs); model space only; hatches, images and points are not drawn in the viewer (they are kept in the file); very large drawings preview partially; replies arrive whole, not streamed. It needs the server, so it does not work on the static in-browser build.

### Editor API

| Method | Path | |
| --- | --- | --- |
| `GET` | `/api/editor/config` | Whether the AI is connected, formats, targets |
| `POST` | `/api/editor/sessions` | Multipart `file`. Opens a drawing; returns the session summary |
| `POST` | `/api/editor/sessions/sample` | Opens the sample warehouse |
| `GET` / `DELETE` | `/api/editor/sessions/{id}` | Summary (digest, history, change log) / close |
| `GET` | `/api/editor/sessions/{id}/geometry` | The drawing as polylines and text for the viewer |
| `POST` | `/api/editor/sessions/{id}/chat` | `{message, selection[]}`. Returns a reply and optionally a proposal |
| `POST` | `/api/editor/sessions/{id}/stage` | `{ops[]}`. Propose operations directly, without a model |
| `POST` | `/api/editor/sessions/{id}/proposals/{pid}/accept` or `/reject` | Decide |
| `POST` | `/api/editor/sessions/{id}/undo` or `/redo` | History |
| `GET` | `/api/editor/sessions/{id}/download.dxf` | The edited drawing as DXF |
| `POST` | `/api/editor/sessions/{id}/export` | `{target, format}`. Starts a normal conversion job (poll `/api/jobs/{id}`) |

## What each engine can do

| Setup | DWG in | DXF in | DWG out | DXF out |
| --- | --- | --- | --- | --- |
| ODA File Converter (Docker image) | ✓ | ✓ | ✓ 2000–2018 | ✓ 2000–2018 |
| LibreDWG + ezdxf only | ✓ (R13–2018) | ✓ | — | ✓ 2000–2018 |
| In the browser (Vercel / any static host) | ✓ (R13–2018) | ✓ | — | ✓ 2000–2018 |
| ezdxf only | — | ✓ | — | ✓ 2000–2018 |

The UI reads `/api/config` and only offers what the server can actually do. For example, the DWG chip is disabled and explained when ODA isn't installed.

> **About "2026" files.** AutoCAD hasn't changed its file format since 2018. Files saved by AutoCAD 2018 through 2026 all use the same `AC1032` format, so the app labels them "AutoCAD 2018–2026". Target versions work the same way: "2010" means the 2010–2012 format.

## How a conversion works

1. **Read.** The file is identified from its header bytes, never its extension alone. It's then loaded with `ezdxf.recover`, which tolerates damaged files. A DWG is first turned into DXF by ODA, or by LibreDWG as the fallback.
2. **Convert.**
   - *With ODA:* the original file is converted directly to the target version and format, with ODA's audit switched on.
   - *Fallback:* entity types that don't exist in the target version (Mesh before 2010, Multileader/Helix/Section/Surfaces before 2007, Tables and arc dimensions before 2004, …) and add-on proxy objects are removed and recorded. Transparency, which needs 2010+, and true color, which needs 2004+, are stripped where the target can't hold them.
3. **Verify.** The written file is read back. If it can't be read, the job fails. You never get a file that wasn't checked.
4. **Report.** The app compares the source and output entity by entity, per layer and per block. Anything that disappeared is listed as **skipped**. Anything that turned into something else (for example Mesh → Polyface) is listed as **changed**. Damaged data that was fixed on the way is listed as **repaired**.

Skipped entities never fail a job. Only problems with the whole file do (unreadable or corrupt, wrong type, over the size limit, no engine), and the upload screen shows those as a message in the drop zone.

Uploads and results are deleted after 1 hour, or as soon as you press Cancel.

## API

| Method | Path | |
| --- | --- | --- |
| `GET` | `/api/config` | Limits, target versions, available output formats and engines |
| `POST` | `/api/detect` | Multipart `file` (the first 64 KB is enough). Returns kind and version |
| `POST` | `/api/jobs` | Multipart `file`, `target` (2000/2004/2007/2010/2013/2018), `format` (DWG/DXF). Returns 201 with the job |
| `GET` | `/api/jobs/{id}` | Status, progress, current step, ETA, skipped so far, and the result once done |
| `DELETE` | `/api/jobs/{id}` | Cancel: stops the engine and deletes the files |
| `GET` | `/api/jobs/{id}/download` | The converted file |
| `GET` | `/api/jobs/{id}/report.txt` | The conversion report |

Errors come back as `{"error": {"code", "message"}}` with codes `unsupported_type`, `too_large`, `empty`, `already_older`, `no_engine`, `corrupt`, `engine_failed`, `write_failed` and `not_found`.

## Settings (environment variables)

| Variable | Default | |
| --- | --- | --- |
| `BACKDATE_MAX_UPLOAD_MB` | `200` | Upload limit |
| `BACKDATE_RETENTION_SECONDS` | `3600` | How long results are kept |
| `BACKDATE_WORKERS` | `2` | Conversions that run at the same time (the rest queue) |
| `BACKDATE_ENGINE_TIMEOUT` | `900` | Seconds before a single engine run is stopped |
| `BACKDATE_DATA_DIR` | system temp | Where uploads and results are stored |
| `BACKDATE_ENGINE` | `auto` | `auto`, `oda` (fail if missing) or `fallback` (never use ODA) |
| `ODA_CONVERTER_PATH` | auto-detected | Path to the `ODAFileConverter` executable |

Jobs are kept in memory, so run a single server process (`--workers 1`, as the Dockerfile does).

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests cover version detection, the fallback downgrade, every error path, cancelling a running engine, cleanup, and the ODA code path. The ODA path runs against `tests/fake_oda.py`, a stand-in that speaks the same command line. To also run a real DWG through LibreDWG, set `BACKDATE_TEST_DWG=/path/to/file.dwg`.

```
app/
  editor/       AI editor: geometry, operations, sessions, agent, NVIDIA client, routes
  main.py       HTTP API + static files
  jobs.py       job queue, progress/ETA, cancel, 1-hour cleanup
  converter.py  read → convert → write → report pipeline
  engines.py    ODA File Converter and LibreDWG wrappers
  report.py     inventories, diffing, report text
  versions.py   AutoCAD format versions and file detection
  browser.py    entry point for in-browser conversion (Pyodide)
static/         the front end (plain HTML/CSS/JS, no build step)
  browser/      Web Worker for in-browser conversion
  vendor/       Pyodide + LibreDWG WebAssembly (see vendor/README.md)
scripts/        build-static.sh: the static build for Vercel
design/         the original design handoff
```
