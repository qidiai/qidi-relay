#!/usr/bin/env bash
# ===========================================================
#  Qoder proxy - LAN mode (CN + Intl)   [Linux / macOS]
#
#  Listens on every network interface so phones, laptops and
#  other machines on the same network can use it.
#
#  An API key is REQUIRED in this mode.
#
#  Usage:  ./start-qoder-proxy-lan.sh [port] [key]
# ===========================================================
set -u

# `sh start-qoder-proxy-lan.sh` would run this under dash, where the bash-isms
# below fail with "Bad substitution". Hand ourselves over to bash instead.
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

PORT="${1:-8790}"
KEY="${2:-}"
SRC="${BASH_SOURCE[0]:-$0}"
HERE="$(cd -- "$(dirname -- "$SRC")" && pwd)"
SCRIPT="$HERE/qoder_proxy.py"

if [ ! -f "$SCRIPT" ]; then
    echo "[ERROR] qoder_proxy.py not found next to this script."
    echo "        expected: $SCRIPT"
    exit 1
fi

cd "$HERE" || exit 1

# Usable = exists, executable, Python >= 3.9.
usable() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
        >/dev/null 2>&1
}

# An explicit interpreter wins over auto-detection:
#     QD_PROXY_PYTHON=/usr/bin/python3.12 ./start-qoder-proxy-lan.sh
PYEXE="${QD_PROXY_PYTHON:-}"

if [ -n "$PYEXE" ]; then
    if [ ! -x "$PYEXE" ] || ! usable "$PYEXE"; then
        echo "[ERROR] QD_PROXY_PYTHON is set to '$PYEXE', but that is not"
        echo "        an executable Python 3.9+ interpreter."
        exit 1
    fi
fi

# ---- 1) bundled runtime (ships with the portable package, if present) ----
for cand in "$HERE/python/bin/python3" "$HERE/python/bin/python"; do
    [ -n "$PYEXE" ] && break
    if [ -x "$cand" ] && usable "$cand"; then
        PYEXE="$cand"
        break
    fi
done

# ---- 2) python3 on PATH ----
if [ -z "$PYEXE" ] && usable python3; then
    PYEXE="python3"
fi

# ---- 3) python on PATH ----
if [ -z "$PYEXE" ] && usable python; then
    PYEXE="python"
fi

# ---- 4) Codex bundled runtimes (parity with the .bat fallback) ----
if [ -z "$PYEXE" ]; then
    for cand in "$HOME"/.cache/codex-runtimes/*/dependencies/python/bin/python3; do
        if [ -x "$cand" ] && usable "$cand"; then
            PYEXE="$cand"
            break
        fi
    done
fi

if [ -z "$PYEXE" ]; then
    echo "[ERROR] No usable Python found."
    echo
    echo "Options:"
    echo "  1. Use the packaged bundle, which already contains python/"
    echo "  2. Install Python 3.9+ (apt install python3 / dnf install python3)"
    echo "  3. Or point the script at an interpreter explicitly:"
    echo "       QD_PROXY_PYTHON=/usr/bin/python3.12 ./start-qoder-proxy-lan.sh"
    echo
    exit 1
fi

echo "==========================================================="
echo "  Qoder proxy - LAN MODE"
echo
echo "  Port $PORT - your API address, dashboard link and API"
echo "  key are printed below once the server is up."
echo
echo "  If other devices cannot connect, run once with sudo:"
echo "      ./allow-firewall.sh $PORT"
echo
echo "  Keep this terminal open. Ctrl+C stops the server."
echo "==========================================================="
echo

# Pass --api-key only when the user supplied one; otherwise the gateway
# mints a random key on first run and prints it below.
#
# exec (not a child process) so the server's lifetime is exactly this
# terminal's: Ctrl+C or closing the terminal stops it, and killing the
# launcher can never leave an orphan holding the port.
if [ -n "$KEY" ]; then
    exec "$PYEXE" "$SCRIPT" --port "$PORT" --lan --api-key "$KEY"
else
    exec "$PYEXE" "$SCRIPT" --port "$PORT" --lan
fi
