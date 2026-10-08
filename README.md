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

`/editor.html` opens a drawing next to a chat, so you can edit it by asking. It is a separate feature: the converter above is unchanged, and the editor only reuses its file reader and its export queue. It needs the Python server (Docker, Render), so it does not work on the static in-browser build.

1. **Open** a DWG or DXF, or try the built-in sample warehouse. You can also open a **vector PDF** plan (at a scale you give), a **photo of a sketch** (traced by a vision model to a width you give), or a **Warehouse Designer Pro** project (`.wdp`). DWG needs ODA File Converter or LibreDWG on the server, like the converter.
2. **Ask.** For example "move layer S-RACK 2 m east", "rename layer TEMP to NOTES", "replace "REV A" with "REV B"", "purge unused layers", or, with the AI connected, open-ended requests such as "tidy the annotation layer". Click things on the drawing first (click, Shift-click, or drag a box: left-to-right must enclose, right-to-left touches), or Alt-drag to mark an area, and say what to do with them. Several commands, one per line, run as a script. There's a microphone button where the browser supports speech input.
3. **Review.** Nothing is applied yet. The drawing shows what would be removed or moved (dashed orange) and where it ends up (green). The chat lists the changes in words, with the AI's "why" and the exact operations one click away. Plans come as named steps you can untick.
4. **Accept or reject.** Accepted changes go on an undo/redo history and a change log.
5. **Save** as DXF, or as an older release (DWG with ODA), through the same conversion and report as the converter.

### What it can do

| | |
| --- | --- |
| **Ask about the drawing** | "explain this drawing", "what is this?" (selection), "what's in this area?", quantity take-off (CSV), rooms and areas, door/window schedules, block schedules and bills of materials, rack rows and aisle widths against a minimum, find any text, block or layer |
| **Check and clean** | A health score with one-click fixes (unused layers and blocks, duplicates, zero-length lines, empty text, stray objects far from the rest, typos, dimensions that don't match what they measure), "clean up the drawing" as a step-by-step plan, layer names to the NCS/AIA standard or your own mapping, spelling |
| **Edit** | 35 operations: move, copy, array, rotate, scale, delete, layers (create, rename, merge, colour, on/off, freeze, lock), add lines, polylines, rectangles, circles, text and tables, find and replace text, text style and heights, fonts, fill title-block attributes, renumber, revision clouds, explode, flatten, purge, delete duplicates, set units, rename blocks, re-path or detach xrefs |
| **Compare** | What changed since you opened it, or against any older revision: overlay on the drawing, per-layer counts, a text report, and revision clouds with a revision-table row in one click |
| **Tables** | A CSV or Excel sheet drawn as a table, or a row of it filled into a title block |
| **Export** | PDF to a standard scale with a title block and scale bar, SVG, the change log as text or PDF, and a **proof pack** (.zip with the original, the edited DXF, the operations, a comparison, health before/after, PDFs and SHA-256 checksums) |
| **Recipes and batch** | Record a session's changes as a recipe (or write one), replay it on another drawing, or run it on up to 200 drawings at once with a report and a zip of results. A webhook can be called when a batch finishes; a hot folder can run a recipe on every drawing dropped into it |
| **Share and review** | Links for a frozen revision: viewers comment, approvers approve or request changes; comments can be pinned to a spot and show up as numbered pins on your drawing, with replies. You get a live notice when someone comments or decides |
| **Edit together** | An editor link lets someone join the same drawing live: you see each other's cursors and names, each other's requests and proposals, and either of you can accept |
| **Memory and search** | With a workspace key (made for you in this browser, or shared by your team), the editor remembers each drawing: "welcome back", what you changed last time, a search over all your drawings, and drawings similar to this one. The files themselves are never kept |

How the AI is kept safe: the model never edits the file. It sees a summary of the drawing (units, size, layers, block names, text labels) and can ask questions through read-only queries. To change anything it must answer with operations from the fixed list above. Each is validated and run on a *copy* of the drawing, and only a person pressing Accept commits it. Text inside the drawing is treated as data, never as instructions. Built-in commands answer first, so common requests are instant, free and work without any key.

### Connecting the AI (OpenRouter, Nemotron 3 Ultra)

Set **one** secret on the server that runs the app (on Render: the service → **Environment** → add a secret). Not in the repo, not in Vercel or Supabase: the key is only read by the Python server.

| Variable | Default | |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | none | Key from openrouter.ai (starts `sk-or-`). **Without it the editor still works with the built-in commands.** |
| `AI_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b:free` | Any model id on your provider |
| `AI_VISION_MODEL` | `nvidia/nemotron-nano-12b-v2-vl:free` | For tracing sketch photos |
| `AI_PROVIDER` | from the key | `openrouter`, `nvidia`, `custom`, or a local server: `ollama`, `lmstudio`, `vllm` (no key needed, drawings never leave your network) |
| `AI_BASE_URL` | the provider's | Any OpenAI-compatible endpoint |
| `AI_REASONING` | `low` (`off` for local) | Reasoning effort sent to the model: `off`, `low`, `medium`, `high` |
| `AI_MAX_TOKENS` / `AI_TIMEOUT` | `4096` / `120` | Per model call |
| `AI_EXTRA_BODY` | none | JSON merged into each request |
| `AI_APP_URL` | this site | Sent to OpenRouter as the app's address |

`NVIDIA_API_KEY` (with `NEMOTRON_MODEL`, `NVIDIA_BASE_URL`) still works for NVIDIA's own endpoint. Replies stream into the chat as they're written.

With the AI on, a message and a summary of the drawing (layers, counts, text labels, never the file) go to the AI service. The editor page says so.

### Limits, keys and storage

| Variable | Default | |
| --- | --- | --- |
| `BACKDATE_AI_DAILY_PER_IP` | `100` | AI requests per visitor per day, so one person can't spend a free model's shared allowance. Built-in commands are unlimited |
| `BACKDATE_ACCESS_TOKENS` | none | JSON (or a path to a JSON file) of access keys with their own daily allowance: `{"key": {"name": "Acme", "daily": 500}}`. People enter theirs under Tools → Settings |
| `BACKDATE_REQUIRE_TOKEN` | off | `1` closes the editor to anyone without an access key (review links stay open) |
| `BACKDATE_TRUST_PROXY` | off | `1` reads the visitor's address from `X-Forwarded-For` (Render and most hosts set it) |
| `BACKDATE_EDITOR_AI_LIMIT` | `100` | AI requests per open drawing |
| `BACKDATE_EDITOR_MAX_SESSIONS` | `20` | Open drawings kept in memory |
| `BACKDATE_EDITOR_MEMORY` / `_DAYS` | `on` / `30` | Workspace memory and search, and how long it's kept |
| `BACKDATE_SHARE_DAYS` | `14` | How long review links work |
| `BACKDATE_BATCH_MAX_FILES` / `BACKDATE_BATCH_AI_LIMIT` | `200` / `20` | Per batch |
| `BACKDATE_WATCH_DIR`, `BACKDATE_WATCH_RECIPE`, `BACKDATE_WATCH_INTERVAL` | none, none, `30` | Hot folder: run a recipe file on every drawing put in the folder; results go to `done/` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | none | Email a batch report when it finishes |
| `BACKDATE_ALLOW_HTTP_CALLBACKS` | off | Batch webhooks must be public `https://` addresses unless this is `1` |

Open drawings live in memory for an hour (single server process, like the converter's jobs); memory, review links and comments are kept on disk under `BACKDATE_DATA_DIR`. Model space only; hatches, images and points aren't drawn in the viewer (they are kept in the file); very large drawings preview partially.

### Command line

The same tools run without the web page, for scripts and CI:

```bash
python -m app.editor.cli health plan.dxf                       # score and findings (--json for machines)
python -m app.editor.cli explain plan.dxf
python -m app.editor.cli takeoff plan.dxf --csv takeoff.csv
python -m app.editor.cli apply plan.dxf --recipe tidy.json --out plan_clean.dxf
python -m app.editor.cli apply plan.dxf --command "purge unused layers" --out plan_clean.dxf --target 2010
python -m app.editor.cli batch drawings/ --recipe tidy.json --out results/
python -m app.editor.cli compare old.dxf new.dxf
python -m app.editor.cli pdf plan.dxf --out plan.pdf --paper A3
```

Also `rooms`, `aisles`, `audit FOLDER` (a health report for every drawing in a folder, `--csv`, `--email`) and `watch FOLDER --recipe R` (a hot folder). `--help` on each.

### Editor API

All under `/api/editor`. Send `X-Workspace` for memory and search, and `X-Access-Token` when the server uses access keys.

| Method | Path | |
| --- | --- | --- |
| `GET` | `/config`, `/usage` | AI connection, formats, targets / today's AI allowance |
| `POST` | `/sessions` | Multipart `file` (DWG, DXF, PDF, WDP, PNG/JPG/WebP) with `page`, `scale`, `width_m` where they apply |
| `POST` | `/sessions/sample` | Opens the sample warehouse |
| `GET` / `DELETE` | `/sessions/{id}` | Summary (digest, history, change log) / close |
| `GET` | `/sessions/{id}/geometry` | The drawing as polylines and text for the viewer |
| `POST` | `/sessions/{id}/chat`, `/chat/stream` | `{message, selection[], area}`. A reply, maybe a proposal and data; the stream sends `status`, `reply` and `result` events |
| `POST` | `/sessions/{id}/stage` | `{ops[]}` or `{steps[]}`. Propose operations directly |
| `POST` | `/sessions/{id}/proposals/{pid}/accept` or `/reject` | Decide; accept takes `{steps: [...]}` to apply only some |
| `POST` | `/sessions/{id}/undo`, `/redo` | History |
| `GET` | `/sessions/{id}/health`, `/takeoff`, `/rooms`, `/schedule`, `/explain`, `/warehouse`, `/standards`, `/find` | Analysis |
| `POST` | `/sessions/{id}/inspect`, `/area` | What's selected / in an area |
| `POST` | `/sessions/{id}/compare/original`, `/compare`, `/compare/clouds` | Compare (upload for `/compare`), then propose revision clouds |
| `GET` | `/sessions/{id}/export.pdf`, `/export.svg`, `/changelog.txt`, `/changelog.pdf`, `/proof-pack.zip`, `/download.dxf` | Outputs |
| `POST` | `/sessions/{id}/export` | `{target, format}`. A normal conversion job (poll `/api/jobs/{id}`) |
| `GET` / `POST` | `/sessions/{id}/recipe`, `/recipes/validate`, `/sessions/{id}/recipe/apply`, `/batch`, `/batch/{id}` | Recipes and batch |
| `POST` / `GET` | `/sessions/{id}/shares`, `/shares/{token}`, `/shares/{token}/comments`, `/shares/{token}/decision`, `/join/{token}` | Review links and live editing |
| `GET` | `/sessions/{id}/events` | Server-sent events: people, cursors, chat, proposals, changes, comments, decisions |
| `GET` | `/search`, `/sessions/{id}/similar`, `/sessions/{id}/memory` | Workspace search and memory |

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
  editor/       AI editor: geometry, operations, sessions, agent, AI client (OpenRouter/NVIDIA/local),
                analysis, health, compare, exports, imports, batch, sharing, memory, quotas, CLI, routes
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
