#!/usr/bin/env bash
set -euo pipefail

# CPU-only Ollama installation, pinned archive with official SHA256.
trad_runtime=/workspace/tools/ollama
trad_archive_url=https://github.com/ollama/ollama/releases/download/v0.6.8/ollama-linux-amd64.tgz
trad_archive_sha=9f9ff52b8297d6e40904f22bac09eac587a05111762bf278359621fbdb53dbb9
mkdir -p "$trad_runtime" /workspace/models/ollama "$HOME/.ollama"
if [[ ! -x "$trad_runtime/bin/ollama" ]]; then
    trad_temp=$(mktemp -d /tmp/trad-ollama-install-XXXXXX)
    trap 'rm -rf "$trad_temp"' EXIT
    curl --fail --location --show-error --retry 0 "$trad_archive_url" -o "$trad_temp/ollama.tgz"
    echo "$trad_archive_sha  $trad_temp/ollama.tgz" | sha256sum --check --status
    tar -xzf "$trad_temp/ollama.tgz" -C "$trad_runtime" --exclude='*cuda*' --exclude='*rocm*'
fi
"$trad_runtime/bin/ollama" --version
echo 'Ollama installé. Démarrer le serveur selon LOCAL_MODEL.md puis exécuter :'
echo '/workspace/tools/ollama/bin/ollama pull qwen2.5:1.5b'
