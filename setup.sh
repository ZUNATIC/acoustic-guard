#!/usr/bin/env bash
# One-time setup for Linux: environment, packages, models and configuration.
#
#   ./setup.sh            standard install (~2.3 GB of models, includes the second-opinion model)
#   ./setup.sh --light    for machines with 8 GB RAM or less (~0.7 GB of models)
#   ./setup.sh --lan      also make the dashboard reachable from other computers (HTTPS + token)
#
# Flags can be combined. Re-running is safe: finished steps are skipped.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

PROFILE=standard
LAN=0
for arg in "$@"; do
    case "$arg" in
        --light) PROFILE=light ;;
        --lan) LAN=1 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg (use --light and/or --lan)"; exit 1 ;;
    esac
done

has_portaudio() {
    for f in /usr/lib/*/libportaudio.so.2 /usr/lib/libportaudio.so.2 /usr/lib64/libportaudio.so.2 /usr/local/lib/libportaudio.so.2; do
        [ -e "$f" ] && return 0
    done
    { command -v ldconfig >/dev/null && ldconfig -p; } 2>/dev/null | grep -q libportaudio && return 0
    /sbin/ldconfig -p 2>/dev/null | grep -q libportaudio
}

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31mSetup stopped:\033[0m %s\n' "$1"; exit 1; }

step "1/6  Checking Python"
PY=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        PY="$candidate"; break
    fi
done
[ -n "$PY" ] || fail "Python 3.10 or newer is required (e.g. sudo apt install python3 python3-venv)."
echo "using $($PY --version) ($PY)"
"$PY" -c 'import venv, ensurepip' 2>/dev/null || fail "Python's venv module is missing. Install it with: sudo apt install python3-venv"

step "2/6  Audio library (PortAudio)"
if has_portaudio; then
    echo "PortAudio already installed"
else
    echo "PortAudio is needed for the endpoint microphone. Installing it (you may be asked for your password)..."
    if command -v apt-get >/dev/null; then sudo apt-get install -y libportaudio2 || true
    elif command -v dnf >/dev/null; then sudo dnf install -y portaudio || true
    elif command -v pacman >/dev/null; then sudo pacman -S --noconfirm portaudio || true
    elif command -v zypper >/dev/null; then sudo zypper install -y portaudio || true
    fi
    has_portaudio \
        && echo "PortAudio installed" \
        || echo "PortAudio could not be installed automatically. Browser microphones still work; install 'libportaudio2' later for the endpoint microphone."
fi

step "3/6  Python environment and packages"
[ -d venv ] || "$PY" -m venv venv
./venv/bin/python -m pip install --quiet --upgrade pip
./venv/bin/python -m pip install --quiet -r requirements.txt || fail "package installation failed (check the internet connection and re-run ./setup.sh)"
echo "packages installed"

step "4/6  Speech models (one-time download, profile: $PROFILE)"
./venv/bin/python scripts/fetch_models.py --profile "$PROFILE" || fail "model download failed (re-run ./setup.sh to resume)"

step "5/6  Configuration"
CONF_ARGS=()
[ "$PROFILE" = light ] && CONF_ARGS+=(--light)
[ "$LAN" = 1 ] && CONF_ARGS+=(--lan)
./venv/bin/python scripts/configure.py ${CONF_ARGS[@]+"${CONF_ARGS[@]}"}

step "6/6  Checking the installation"
./venv/bin/python -c "import app.main" || fail "the service does not start - see the error above"
./venv/bin/python scripts/fetch_models.py --check --profile "$PROFILE" >/dev/null || fail "some models are missing - re-run ./setup.sh"
chmod +x run.sh
echo "everything is in place"

URL=$(./venv/bin/python - <<'PY'
import socket
from app.config import get_settings
s = get_settings()
scheme = "https" if s.ssl_certfile else "http"
host = "127.0.0.1"
if s.host not in ("127.0.0.1", "localhost"):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
        try:
            u.connect(("10.255.255.255", 1)); host = u.getsockname()[0]
        except OSError:
            pass
print(f"{scheme}://{host}:{s.port}")
PY
)

printf '\n\033[32mSetup complete.\033[0m\n\n'
echo "To start Acoustic Guard, run:"
echo
echo "    cd \"$ROOT\" && ./run.sh"
echo
echo "then open  $URL  in a browser."
