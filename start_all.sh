#!/usr/bin/env bash
# Levanta FastAPI (:8000) y el puente WhatsApp Web (:3000).
# Primera vez: aparece un QR en la terminal del puente; escanéalo con tu número.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

if [[ ! -d whatsapp-bridge/node_modules ]]; then
  (cd whatsapp-bridge && npm install)
fi

cleanup() {
  jobs -p | xargs -r kill 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Jarvis API → http://127.0.0.1:8000"
uvicorn jarvis.api.main:app --host 127.0.0.1 --port 8000 &

echo "WhatsApp bridge → http://127.0.0.1:3000  (escanea el QR si aparece)"
(cd whatsapp-bridge && npm start) &

wait
