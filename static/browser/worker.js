// In-browser converter, used when the page is hosted without the conversion
// server (e.g. on Vercel). Runs in a Web Worker so the page stays responsive
// and Cancel can stop it instantly with worker.terminate().
//
//   DWG --(LibreDWG, WebAssembly)--> DXF --(ezdxf in Pyodide)--> older DXF
import { loadPyodide } from "../vendor/pyodide/pyodide.mjs";

const PY_FILES = ["__init__.py", "versions.py", "report.py", "engines.py", "converter.py", "browser.py"];
const here = (path) => new URL(path, import.meta.url).href;
const post = (type, data = {}, transfer = []) => self.postMessage({ type, ...data }, transfer);

let pyodideReady = null;
let dwgReady = null;
let dwgWarnings = 0;

function getPyodide() {
  pyodideReady ??= (async () => {
    const base = here("../vendor/pyodide/");
    const py = await loadPyodide({ indexURL: base, stdout: () => {}, stderr: () => {} });
    post("load", { pct: 45 });
    await py.loadPackage(["numpy", "fonttools", "pyparsing", "typing-extensions", base + "ezdxf-1.4.4-py3-none-any.whl"], {
      messageCallback: () => {},
    });
    post("load", { pct: 80 });
    py.FS.mkdirTree("/home/pyodide/app");
    for (const f of PY_FILES) {
      const res = await fetch(here(`../py/app/${f}`));
      if (!res.ok) throw new Error(`Couldn't load the converter (${f}: ${res.status})`);
      py.FS.writeFile(`/home/pyodide/app/${f}`, await res.text());
    }
    py.runPython(`
import sys, logging
sys.path.insert(0, "/home/pyodide")
logging.getLogger("ezdxf").setLevel(logging.ERROR)
import app.browser
`);
    return py;
  })();
  pyodideReady.catch(() => (pyodideReady = null));
  return pyodideReady;
}

function getDwgReader() {
  dwgReady ??= import("../vendor/libredwg/libredwg-web.js").then((m) =>
    m.default({
      print: () => {},
      printErr: (line) => {
        if (/^(ERROR|Warning)/.test(line)) dwgWarnings++;
      },
    }),
  );
  dwgReady.catch(() => (dwgReady = null));
  return dwgReady;
}

function isDwg(head) {
  // "AC10xx" magic
  return head[0] === 0x41 && head[1] === 0x43 && head[2] >= 0x30 && head[2] <= 0x39;
}

function dwgToDxf(reader, bytes) {
  dwgWarnings = 0;
  reader.FS.writeFile("in.dwg", bytes);
  let failed = null;
  try {
    const code = reader.dwg_write_dxf("in.dwg", "out.dxf");
    // Codes >= 128 are critical; smaller ones are recoverable warnings.
    if (code >= 128) failed = `error code ${code}`;
  } catch (e) {
    failed = e.message;
    dwgReady = null; // the module may be in a bad state; start fresh next time
  }
  const exists = !failed && reader.FS.analyzePath("out.dxf", false).exists;
  const out = exists ? reader.FS.readFile("out.dxf") : null;
  try { reader.FS.unlink("in.dwg"); } catch {}
  try { reader.FS.unlink("out.dxf"); } catch {}
  if (!out || !out.length) {
    const err = new Error(`The DWG file couldn't be read. It may be damaged${failed ? ` (${failed})` : ""}.`);
    err.code = "corrupt";
    throw err;
  }
  return out;
}

self.onmessage = async (event) => {
  const { buffer, name, target } = event.data;
  try {
    const bytes = new Uint8Array(buffer);
    const head = bytes.subarray(0, 65536);
    const dwg = isDwg(head);
    post("load", { pct: 5 });
    const [py, reader] = await Promise.all([getPyodide(), dwg ? getDwgReader() : null]);
    post("load", { pct: 100 });

    let dxf = bytes;
    if (dwg) {
      post("py", { kind: "stage", value: { index: 1, lo: 0, hi: 30, expected: 1 + bytes.length / 1e6 } });
      dxf = dwgToDxf(reader, bytes);
    }
    py.FS.writeFile("/tmp/in.dxf", dxf);
    py.FS.writeFile("/tmp/head.bin", head);
    const emit = (kind, value) => post("py", { kind, value: JSON.parse(value) });
    const run = py.pyimport("app.browser").run_files;
    const json = run("/tmp/in.dxf", "/tmp/head.bin", "/tmp/out.dxf", name, target, emit, dwgWarnings);
    run.destroy();
    py.FS.unlink("/tmp/in.dxf");
    const result = JSON.parse(json);
    if (result.error) return post("error", { error: result.error });
    const output = py.FS.readFile("/tmp/out.dxf");
    py.FS.unlink("/tmp/out.dxf");
    post("done", { result, output }, [output.buffer]);
  } catch (e) {
    post("error", { error: { code: e.code || "internal", message: e.message || String(e) } });
  }
};
