#!/usr/bin/env bash
# launch.sh — start ComfyUI + avatar backend
# Edit AVATAR_IMAGE, AVATAR_VOICE_REF, and AVATAR_PERSONA below, then run this.
set -euo pipefail

COMFY_DIR="${COMFY_DIR:-$HOME/ComfyUI}"
BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- LLM: Pollinations AI (free, no key required) ----------------------------
export LLM_PROVIDER=openai_compatible
export LLM_BASE_URL=https://text.pollinations.ai/openai
export LLM_API_KEY=dummy
export LLM_MODEL=openai          # Pollinations free GPT-4o endpoint
# Swap to OpenRouter later: LLM_BASE_URL=https://openrouter.ai/api/v1, LLM_API_KEY=sk-or-...

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
