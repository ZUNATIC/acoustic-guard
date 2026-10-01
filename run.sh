#!/usr/bin/env bash
# Starts Acoustic Guard. Run ./setup.sh once before the first start.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x venv/bin/python ] || [ ! -f .env ]; then
    echo "Acoustic Guard is not set up yet. Run this first:"
    echo "    ./setup.sh"
    exit 1
fi

./venv/bin/python - <<'PY'
import socket
from app.config import get_settings
s = get_settings()
scheme = "https" if s.ssl_certfile else "http"
hosts = ["127.0.0.1"]
if s.host not in ("127.0.0.1", "localhost"):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
        try:
            u.connect(("10.255.255.255", 1)); hosts.append(u.getsockname()[0])
        except OSError:
            pass
print("Acoustic Guard is starting (press Ctrl+C to stop)")
for h in hosts:
    print(f"  dashboard: {scheme}://{h}:{s.port}")
if s.api_token:
    print(f"  access token: {s.api_token}")
PY

exec ./venv/bin/python -m app
