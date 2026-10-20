#!/usr/bin/env bash
# ===========================================================
#  Qoder proxy - launcher (CN + Intl)   [Linux / macOS]
#
#  Usage:  ./start-qoder-proxy.sh [port]
#
#  Same design as start-qoder-proxy.bat: this terminal window
#  IS the process lifetime - Ctrl+C (or closing the terminal)
#  stops the server, no background leftovers.
# ===========================================================
set -u

# `sh start-qoder-proxy.sh` would run this under dash, where the bash-isms
# below fail with "Bad substitution". Hand ourselves over to bash instead.
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

PORT="${1:-8790}"
SRC="${BASH_SOURCE[0]:-$0}"
HERE="$(cd -- "$(dirname -- "$SRC")" && pwd)"
SCRIPT="$HERE/qoder_proxy.py"

if [ ! -f "$SCRIPT" ]; then
    echo "[ERROR] qoder_proxy.py not found next to this script."
    echo "        expected: $SCRIPT"
    exit 1
fi

cd "$HERE" || exit 1

# A candidate is usable when it exists, is executable and is Python >= 3.9
# (a stray python2 on PATH would otherwise be picked and fail in odd ways).
usable() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
        >/dev/null 2>&1
}

# An explicit interpreter wins over auto-detection:
#     QD_PROXY_PYTHON=/usr/bin/python3.12 ./start-qoder-proxy.sh
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
    echo "       QD_PROXY_PYTHON=/usr/bin/python3.12 ./start-qoder-proxy.sh"
    exit 1
fi

echo "==========================================================="
echo "  Qoder proxy"
echo
echo "  API      : http://127.0.0.1:$PORT/v1"
echo "  Dashboard: http://127.0.0.1:$PORT/"
echo
echo "  Python   : $PYEXE"
echo
echo "  Keep this terminal open. Ctrl+C stops the server."
echo "==========================================================="
echo

# exec (not a child process) so the server's lifetime is exactly this
# terminal's: Ctrl+C or closing the terminal stops it, and killing the
# launcher can never leave an orphan holding the port.
exec "$PYEXE" "$SCRIPT" --port "$PORT"
