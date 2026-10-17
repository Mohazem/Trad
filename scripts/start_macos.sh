#!/usr/bin/env bash
set -euo pipefail
trad_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$trad_root"
if [[ "$(uname -s)" != Darwin ]]; then
    echo 'Ce lanceur est destiné à macOS. Voir LOCAL_MODEL.md pour Linux.' >&2
    exit 1
fi
command -v python3 >/dev/null || { echo 'Installer Python 3.11+ : voir MACOS.md' >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ requis"'
command -v ollama >/dev/null || { echo 'Installer Ollama : voir MACOS.md' >&2; exit 1; }
trad_ollama_child=
trad_web_child=
cleanup() {
    for trad_pid in "$trad_web_child" "$trad_ollama_child"; do
        if [[ -n "$trad_pid" ]]; then
            kill "$trad_pid" 2>/dev/null || true
            wait "$trad_pid" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export OLLAMA_HOST=127.0.0.1:11434
export TRAD_AI_PROVIDER=ollama
export TRAD_LOCAL_MODEL="${TRAD_LOCAL_MODEL:-qwen2.5:1.5b}"
if ! python3 -c 'from trading.connected import local_json; local_json("/api/tags", timeout=1)' 2>/dev/null; then
    mkdir -p outputs
    OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 ollama serve > outputs/ollama-macos.log 2>&1 &
    trad_ollama_child=$!
    python3 - <<'PY'
import time
from trading.connected import ConnectedError, local_json
for attempt in range(100):
    try:
        local_json('/api/tags', timeout=1)
        break
    except ConnectedError:
        time.sleep(.2)
else:
    raise SystemExit('Ollama ne démarre pas : consulter outputs/ollama-macos.log.')
PY
fi
if ! python3 -c 'from trading.connected import configuration_status; raise SystemExit(0 if configuration_status()["ai_ready"] else 1)'; then
    ollama pull "$TRAD_LOCAL_MODEL"
fi
python3 -c 'from trading.connected import configuration_status; assert configuration_status()["ai_ready"], "Modèle local indisponible"'
python3 -m unittest discover -s tests -q
python3 -m trading.web "$@" &
trad_web_child=$!
wait "$trad_web_child"
