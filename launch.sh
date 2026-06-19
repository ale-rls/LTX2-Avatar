#!/usr/bin/env bash
# launch.sh — start ComfyUI + avatar backend
# Edit AVATAR_IMAGE, AVATAR_VOICE_REF, and AVATAR_PERSONA below, then run this.
set -euo pipefail

COMFY_DIR="${COMFY_DIR:-$HOME/ComfyUI}"
BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- LLM brain: Qwen3 on this box via Ollama ---------------------------------
export LLM_PROVIDER=qwen3
export LLM_MODEL="${OLLAMA_MODEL:-qwen3}"   # e.g. qwen3:8b, qwen3:14b — must be pulled
# brain.py defaults LLM_BASE_URL to http://localhost:11434/v1 and strips <think> blocks.
# To point elsewhere (vLLM, a remote Ollama): set LLM_BASE_URL / LLM_API_KEY here.

# ---- Avatar assets (filenames in ComfyUI/input/) -----------------------------
export AVATAR_IMAGE="${AVATAR_IMAGE:-ref-img.png}"
export AVATAR_VOICE_REF="${AVATAR_VOICE_REF:-B.wav}"
export AVATAR_PERSONA="${AVATAR_PERSONA:-You are a mysterious oracle who speaks in vivid, short sentences. One or two lines only.}"

# ---- Clip settings (H100 fp8 path, full resolution) --------------------------
export CLIP_W=960
export CLIP_H=1280
export CLIP_SECONDS=6

# ---- Optional: enable fast mode by default (halves render time) ---------------
# export FAST_MODE=1

# ---- Start Ollama + ensure the Qwen3 model is present ------------------------
OLLAMA_HOST_URL="${OLLAMA_HOST_URL:-http://127.0.0.1:11434}"
if ! curl -sf "$OLLAMA_HOST_URL/api/tags" >/dev/null 2>&1; then
  echo "[launch] Starting ollama serve ..."
  ollama serve >/tmp/ollama.log 2>&1 &
  for i in $(seq 1 30); do
    curl -sf "$OLLAMA_HOST_URL/api/tags" >/dev/null 2>&1 && { echo "[launch] Ollama ready."; break; }
    sleep 1
  done
else
  echo "[launch] Ollama already running."
fi
# Pull the model if it isn't cached yet (no-op once present).
if ! ollama list 2>/dev/null | grep -q "^${LLM_MODEL}\b"; then
  echo "[launch] Pulling $LLM_MODEL (first run only) ..."
  ollama pull "$LLM_MODEL"
fi

# ---- Start ComfyUI in background ---------------------------------------------
echo "[launch] Starting ComfyUI on 127.0.0.1:8188 ..."
cd "$COMFY_DIR"
python main.py --listen 127.0.0.1 --port 8188 &
COMFY_PID=$!
echo "[launch] ComfyUI PID=$COMFY_PID — waiting for it to be ready ..."

# Wait until ComfyUI API responds
for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then
    echo "[launch] ComfyUI ready."
    break
  fi
  sleep 2
done

# ---- Start avatar backend ----------------------------------------------------
echo "[launch] Starting avatar backend on 0.0.0.0:8080 ..."
cd "$BACKEND_DIR"
python server.py --port 8080 --comfy-port 8188 --workflow workflow_api.json

# (ComfyUI will keep running in background; kill $COMFY_PID to stop it)
