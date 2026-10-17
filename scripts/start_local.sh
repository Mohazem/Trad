#!/usr/bin/env bash
set -euo pipefail

trad_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
trad_ollama=/workspace/tools/ollama/bin/ollama
trad_child=
trad_web_child=
cleanup() {
    if [[ -n "$trad_web_child" ]]; then
        kill "$trad_web_child" 2>/dev/null || true
        wait "$trad_web_child" 2>/dev/null || true
    fi
    if [[ -n "$trad_child" ]]; then
        kill "$trad_child" 2>/dev/null || true
        wait "$trad_child" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cd "$trad_root"
if [[ ! -x "$trad_ollama" ]]; then
    echo 'Installer Ollama avec : bash scripts/install_local.sh' >&2
    exit 1
fi
# Reuse an existing server; only stop the process started by this script.
if ! python -c 'from trading.connected import local_json; local_json("/api/tags", timeout=1)' 2>/dev/null; then
    mkdir -p outputs
    OLLAMA_MODELS=/workspace/models/ollama OLLAMA_HOST=127.0.0.1:11434 \
        OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 \
        "$trad_ollama" serve > outputs/ollama.log 2>&1 &
    trad_child=$!
    python - <<'PY'
import time
from trading.connected import ConnectedError, local_json
for attempt in range(50):
    try:
        local_json('/api/tags', timeout=1)
        break
    except ConnectedError:
        time.sleep(0.1)
else:
    raise SystemExit('Ollama ne démarre pas ; consulter outputs/ollama.log.')
PY
fi
# Model downloads are explicit, never triggered by opening the dashboard.
TRAD_AI_PROVIDER=ollama python - <<'PY'
from trading.connected import configuration_status
status = configuration_status()
if not status['ai_ready']:
    print('Modèle absent : le tableau de bord reste accessible sans analyse IA.')
    print('Télécharger avec : /workspace/tools/ollama/bin/ollama pull ' + status['ai_model'])
PY
TRAD_AI_PROVIDER=ollama python -m trading.web "$@" &
trad_web_child=$!
wait "$trad_web_child"
