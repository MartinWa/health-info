"""Liveness probe for the container HEALTHCHECK."""

import json
import sys
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=10) as response:
        body = json.loads(response.read())
except (OSError, ValueError) as exc:
    print(f"healthcheck failed: {exc}", file=sys.stderr)
    sys.exit(1)
if body.get("status") != "ok":
    print("healthcheck failed: status is not ok", file=sys.stderr)
    sys.exit(1)
