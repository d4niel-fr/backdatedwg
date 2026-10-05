"""End-to-end check of the Docker image: real DWG in, older DWG out.

    python server_check.py http://localhost:8000 sample.dwg
"""

import json
import sys
import time
import urllib.request
import uuid

base, sample = sys.argv[1].rstrip("/"), sys.argv[2]


def get(path):
    with urllib.request.urlopen(base + path, timeout=60) as r:
        return r.read()


def post_job(path, target, fmt):
    boundary = uuid.uuid4().hex
    data = open(path, "rb").read()
    body = b"".join(
        [
            f'--{boundary}\r\nContent-Disposition: form-data; name="target"\r\n\r\n{target}\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="format"\r\n\r\n{fmt}\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="Sample_2018.dwg"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode(),
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    req = urllib.request.Request(base + "/api/jobs", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


config = json.loads(get("/api/config"))
print("engines:", config["engines"], "formats:", config["formats"])
assert config["engines"]["oda"], "ODA File Converter is not installed in the image"

failures = 0
for target, fmt, magic in [(2010, "DWG", b"AC1024"), (2000, "DWG", b"AC1015"), (2004, "DXF", b"AC1018")]:
    job = post_job(sample, target, fmt)
    while job["status"] in ("queued", "running"):
        time.sleep(1)
        job = json.loads(get(f"/api/jobs/{job['id']}"))
    print(f"\n== {fmt} {target}: {job['status']}")
    if job["status"] != "done":
        print("   error:", job.get("error"))
        failures += 1
        continue
    res = job["result"]
    print("  ", res["outputName"], res["outputSize"], "bytes, engine:", res["engine"], res["counts"])
    for item in res["items"][:15]:
        print(f"   - [{item['kind']}] {item['entity']} x{item['count']} · {item['where']} — {item['reason']}")
    out = get(res["downloadUrl"])
    head = out[:6] if fmt == "DWG" else out[:4000]
    ok = (head == magic) if fmt == "DWG" else (magic in head)
    print("   output version check:", "OK" if ok else f"FAILED (starts {out[:12]!r})")
    failures += 0 if ok else 1

print("\nSERVER CHECK", "OK" if not failures else f"FAILED ({failures})")
sys.exit(1 if failures else 0)
