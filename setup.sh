#!/usr/bin/env bash
# setup.sh -- prepare a RunPod GPU box for the LTX-2 talking-avatar backend.
# Run on the pod (Ubuntu). Idempotent-ish; safe to re-run.
set -euo pipefail

COMFY_DIR="${COMFY_DIR:-$HOME/ComfyUI}"
PY="${PY:-python3}"

# This script's own directory IS the backend repo (server.py lives next to it). Used
# for step 3 + the launch hints below. (Previously hardcoded to $HOME/ltx2-avatar,
# which almost never matches where you actually cloned this.)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${BACKEND_DIR:-$SCRIPT_DIR}"

# Single-purpose GPU box: let pip write to the system env without the PEP-668 nag.
export PIP_BREAK_SYSTEM_PACKAGES=1

echo "==> 0. Pin the pre-installed torch so the installs below can't swap it"
# Hard lesson: ComfyUI's requirements.txt lists torch/torchvision/torchaudio UNPINNED,
# and some custom nodes (notably ComfyUI-QwenTTS) demand torch>=2.9.1. On a pod that
# already ships a working CUDA torch, an unconstrained resolve will cheerfully replace
# it with a wheel that doesn't match the box's CUDA -> dead GPU. We pin to whatever is
# already installed and export it as a GLOBAL pip constraint for every install below.
# (No torch yet? We skip the pin and let ComfyUI install its own — fine on a fresh box.)
TORCH_PIN="$(mktemp)"
$PY - <<'PYEOF' > "$TORCH_PIN" 2>/dev/null || true
import importlib.metadata as md
for p in ("torch", "torchvision", "torchaudio"):
    try:
        print(f"{p}=={md.version(p)}")
    except Exception:
        pass
PYEOF
if [ -s "$TORCH_PIN" ]; then
  export PIP_CONSTRAINT="$TORCH_PIN"
  echo "   pinned (ComfyUI/QwenTTS may not change these):"
  sed 's/^/     /' "$TORCH_PIN"
else
  echo "   (no torch pre-installed; ComfyUI will pull its own)"
fi

echo "==> 1. ComfyUI"
if [ ! -d "$COMFY_DIR" ]; then
  git clone https://github.com/comfyanonymous/ComfyUI "$COMFY_DIR"
fi
cd "$COMFY_DIR"
$PY -m pip install --break-system-packages -r requirements.txt

echo "==> 2. Custom nodes required by the RuneXX LTX-2.3 talking-avatar workflow"
cd "$COMFY_DIR/custom_nodes"
clone() { [ -d "$(basename "$1" .git)" ] || git clone "$1"; }

# Core node packs the graph references (class_types seen in the workflow):
clone https://github.com/ltdrdata/ComfyUI-Manager.git
clone https://github.com/kijai/ComfyUI-KJNodes.git              # LTXV*, VAELoaderKJ, NAG, tuners
clone https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git  # VHS_VideoCombine
clone https://github.com/rgthree/rgthree-comfy.git              # Power Lora Loader, Label
clone https://github.com/yolain/ComfyUI-Easy-Use.git            # easy showAnything
clone https://github.com/1038lab/ComfyUI-QwenTTS.git            # AILab_Qwen3TTSVoiceClone
clone https://github.com/kijai/ComfyUI-MelBandRoFormer.git      # vocal isolation (optional path)
clone https://github.com/Urabewe/ComfyUI-AudioTools.git         # AudioEnhancement / NormalizeLUFS
clone https://github.com/melMass/comfy_mtb.git                  # Audio Duration (mtb)
clone https://github.com/city96/ComfyUI-GGUF.git                # GGUF loaders (low-VRAM path)
clone https://github.com/SethRobinson/comfyui-workflow-to-api-converter-endpoint.git  # adds POST /workflow/convert (editor->API, handles subgraphs)

# install each node pack's python deps if present
for d in */ ; do
  if [ -f "$d/requirements.txt" ]; then
    echo "   - deps for $d"
    $PY -m pip install --break-system-packages -r "$d/requirements.txt" || true
  fi
done

echo "==> 2b. Re-add QwenTTS python deps the resolver silently dropped"
# ComfyUI-QwenTTS/requirements.txt pins torch>=2.9.1 + torchaudio>=2.9.1. Under our
# torch pin that whole file fails to resolve (ResolutionImpossible), so pip installs
# NONE of it -- quietly losing openai-whisper + tiktoken (its other deps are already
# present via ComfyUI/transformers). The Qwen3-TTS node itself imports fine on older
# torch (verified on torch 2.4.1), so we just add the two missing pure-python deps.
$PY -m pip install openai-whisper tiktoken || true

echo "==> 2c. Guarantee an importable onnxruntime (faster-whisper VAD depends on it)"
# comfy_mtb pulls onnxruntime-gpu, which may target a different CUDA major than torch
# and then fail to import (we hit libcudart.so.13 on a CUDA-12 box). That breaks
# server.py's vad_filter=True on the very first turn. Force a CPU onnxruntime that
# always imports; the Silero VAD is tiny so CPU is plenty. --force-reinstall clears the
# stale dist-info the gpu-package uninstall leaves behind.
if ! $PY -c "import onnxruntime" >/dev/null 2>&1; then
  $PY -m pip uninstall -y onnxruntime-gpu onnxruntime >/dev/null 2>&1 || true
  $PY -m pip install --force-reinstall "onnxruntime>=1.20" || true
fi

echo "==> 3. Backend (this repo) requirements"
if [ -f "$BACKEND_DIR/requirements.txt" ]; then
  $PY -m pip install -r "$BACKEND_DIR/requirements.txt"
fi

echo "==> 3b. LLM brain: Ollama (serves Qwen3 for the avatar's spoken replies)"
# brain.py defaults to Qwen3 via an OpenAI-compatible endpoint on localhost:11434
# -- i.e. Ollama on THIS box. We install the binary here; launch.sh starts the
# server and pulls the model on first run. (Distinct from ComfyUI-QwenTTS above,
# which is the Qwen3 *voice* node -- different model, different job.)
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
else
  echo "   ollama already installed: $(ollama --version 2>/dev/null | head -1)"
fi

cat <<'EOF'

==> 4. MODELS — large files, into ComfyUI/models/.
    Easiest: run the companion script (exact HF repos + target paths, verified):
        COMFY_DIR="$COMFY_DIR" MODE=fp8  ./download_models.sh     # H100/H200 (authored path)
        COMFY_DIR="$COMFY_DIR" MODE=gguf ./download_models.sh     # 5090/<=32GB
    Or place manually. Source repos (verified live):
      UNet/VAE/text-proj:  https://huggingface.co/Kijai/LTX2.3_comfy
      Gemma text encoder:  https://huggingface.co/Comfy-Org/ltx-2  (split_files/text_encoders/)
      GGUF UNet:           https://huggingface.co/Abiray/LTX-2.3-22B-DISTILLED-1.1-GGUF
      GGUF Gemma:          https://huggingface.co/unsloth/gemma-3-12b-it-GGUF
      upscaler + vocoder:  https://huggingface.co/Lightricks/LTX-2

    Place (folder -> file), matching the loader widget paths in the workflow:
      models/diffusion_models/  ltx-2.3-22b-distilled_transformer_only_fp8_scaled.safetensors
      models/loras/             ltx-2.3-22b-distilled-lora-384.safetensors
      models/vae/               LTX23_video_vae_bf16_KJ.safetensors
      models/vae/               LTX23_audio_vae_bf16_KJ.safetensors
      models/vae_approx/        taeltx2_3.safetensors
      models/latent_upscale_models/  ltx-2.3-spatial-upscaler-x2-1.1.safetensors
      models/clip/ (or text_encoders/)  gemma_3_12B_it_fp8_scaled.safetensors
      models/clip/              ltx-2.3_text_projection_bf16.safetensors
      # LOW-VRAM (5090) alternative, GGUF path:
      models/diffusion_models/  LTX-2.3-distilled-Q4_K_S.gguf
      models/clip/              gemma-3-12b-it-Q2_K.gguf   (or Q4)
      # Qwen-TTS weights download on first node run (ComfyUI-QwenTTS handles it)
      # MelBandRoFormer (optional): models/diffusion_models/MelBandRoformer_fp16.safetensors

    VRAM guidance (from the workflow notes):
      5090 (32GB): use the GGUF UNet + Q2/Q4 Gemma, keep tiled VAE decode,
                   resolution 768x512 / 832x480 / 960x544.
      H100/H200 (80-141GB): run the fp8/bf16 path, 1280x736+; everything stays
                   resident -> no reload-between-turns penalty (biggest latency win).

==> 5. LAUNCH (two processes on the pod)

    # ComfyUI (listen on localhost; the backend talks to it in-box)
    cd "$COMFY_DIR"
    python main.py --listen 127.0.0.1 --port 8188 &

    # Export the API-format workflow ONCE from the ComfyUI UI:
    #   Settings > enable Dev mode  ->  menu  Save (API Format)
    #   save as  workflow_api.json  in this repo dir, then verify node IDs:
    #     python workflow_adapter.py --probe workflow_api.json
    #   fix any mismatched IDs it reports (subgraphs get flattened on export).
    #   NB: do NOT reuse the editor .json's embedded extra.prompt as the API file --
    #   it is a stale/different graph (we found it missing LoadImage/Qwen entirely).

    # Brain: Qwen3 on this box via Ollama
    ollama serve &                      # if not already running
    ollama pull qwen3                   # first run only (~5GB; cached after)
    export LLM_PROVIDER=qwen3           # default; talks to localhost:11434
    export AVATAR_IMAGE=alice.png       # filename you uploaded to ComfyUI/input/
    export AVATAR_VOICE_REF=alice.wav   # filename you uploaded to ComfyUI/input/
    cd  <this repo>                     # where server.py lives
    python server.py --port 8080 --comfy-port 8188

    # Easiest of all: just run  ./launch.sh  (starts Ollama + ComfyUI + backend).

==> 6. EXPOSE TCP 8080 on RunPod (HTTP/TCP port), or SSH-tunnel it:
    ssh -L 8080:127.0.0.1:8080 root@<pod-ssh-host> -p <ssh-port>
    Then open  http://127.0.0.1:8080/  in a browser -- the avatar runs full-screen.
EOF

echo "==> setup.sh done."
