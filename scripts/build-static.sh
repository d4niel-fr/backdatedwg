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
echo "Built dist/ ($(du -sh dist | cut -f1))"
