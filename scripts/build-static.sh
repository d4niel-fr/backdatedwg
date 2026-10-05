#!/bin/sh
# Builds the static site (dist/) for hosts without the Python server, such as
# Vercel. The page converts files in the browser: it loads the same Python
# conversion code (app/*.py) into Pyodide.
set -eu
cd "$(dirname "$0")/.."
rm -rf dist
mkdir -p dist/py/app
cp -R static/. dist/
for f in __init__ versions report engines converter browser; do
  cp "app/$f.py" "dist/py/app/$f.py"
done
# The DWG converter runs as a separate server (the Docker image, e.g. on
# Render); the page sends jobs there and falls back to in-browser DXF
# conversion while it's unreachable.
api="${BACKDATE_API_URL-https://backdate-dwg.onrender.com}"
printf 'window.BACKDATE_API = "%s";\n' "$api" > dist/config.js
echo "Conversion server: ${api:-none (in-browser only)}"
echo "Built dist/ ($(du -sh dist | cut -f1))"
