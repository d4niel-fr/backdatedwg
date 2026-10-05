# Backdate.dwg

Upload an AutoCAD **DWG** or **DXF** file and save it as an older release, for example an AutoCAD 2026 drawing saved as an AutoCAD 2010 DWG. If something can't be converted, the app skips it and keeps going. Everything it skipped, changed or repaired is listed in a conversion report that you can also download as `.txt`.

Upload → Converting → Download, built from the design handoff in [`design/`](design/) (Organic design system).

## Run it

> **Not on Vercel.** Vercel runs Python as short-lived serverless functions. Those cap request bodies at 4.5 MB, can't keep a conversion running between requests, and can't install ODA File Converter. This app needs a normal server that runs the Docker image: Render, Railway, Fly.io, Google Cloud Run or any VPS.

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

## What each engine can do

| Setup | DWG in | DXF in | DWG out | DXF out |
| --- | --- | --- | --- | --- |
| ODA File Converter (Docker image) | ✓ | ✓ | ✓ 2000–2018 | ✓ 2000–2018 |
| LibreDWG + ezdxf only | ✓ (R13–2018) | ✓ | — | ✓ 2000–2018 |
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
  main.py       HTTP API + static files
  jobs.py       job queue, progress/ETA, cancel, 1-hour cleanup
  converter.py  read → convert → write → report pipeline
  engines.py    ODA File Converter and LibreDWG wrappers
  report.py     inventories, diffing, report text
  versions.py   AutoCAD format versions and file detection
static/         the front end (plain HTML/CSS/JS, no build step)
design/         the original design handoff
```
