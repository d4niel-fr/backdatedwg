Third-party runtimes for the in-browser converter (used when the page is
hosted without the conversion server, e.g. on Vercel).

- `pyodide/` — Pyodide 314.0.7 (Python compiled to WebAssembly, MPL-2.0),
  from https://github.com/pyodide/pyodide/releases/tag/314.0.7, plus the
  wheels the converter needs: numpy, fonttools, pyparsing, typing_extensions
  (from that release) and ezdxf 1.4.4 (MIT, from PyPI).
- `libredwg/` — LibreDWG compiled to WebAssembly, from the npm package
  `@mlightcad/libredwg-web` 0.7.14 (GPL-3.0). Source:
  https://github.com/mlightcad/libredwg-web and
  https://www.gnu.org/software/libredwg/
