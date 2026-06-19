#!/usr/bin/env bash
# setup.sh -- prepare a RunPod GPU box for the LTX-2 talking-avatar backend.
# Run on the pod (Ubuntu). Idempotent-ish; safe to re-run.
set -euo pipefail

COMFY_DIR="${COMFY_DIR:-$HOME/ComfyUI}"
PY="${PY:-python3}"

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

# install each node pack's python deps if present
for d in */ ; do
  if [ -f "$d/requirements.txt" ]; then
    echo "   - deps for $d"
    $PY -m pip install --break-system-packages -r "$d/requirements.txt" || true
  fi
done

echo "==> 3. Backend (this repo) requirements"
# run from wherever you copied this repo; adjust if needed
if [ -f "${BACKEND_DIR:-$HOME/ltx2-avatar}/requirements.txt" ]; then
  $PY -m pip install --break-system-packages -r "${BACKEND_DIR:-$HOME/ltx2-avatar}/requirements.txt"
fi

cat <<'EOF'

==> 4. MODELS — download into ComfyUI/models/ (NOT automated; large files).
    Sources are listed inside the workflow's own notes:
      Kijai LTX-2.3 builds:   https://huggingface.co/Kijai/LTX2.3_comfy
      Text encoder (Gemma):   https://huggingface.co/Comfy-Org/ltx-2

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
    #   save as  workflow_api.json  in the backend dir, then verify node IDs:
    #     python workflow_adapter.py --probe workflow_api.json
    #   fix any mismatched IDs it reports (subgraphs get flattened on export).

    # Backend (set provider + key first)
    export LLM_PROVIDER=openai          # or anthropic / openai_compatible
    export OPENAI_API_KEY=sk-...        # matching key
    export AVATAR_IMAGE=alice.png       # filename you uploaded to ComfyUI/input/
    export AVATAR_VOICE_REF=alice.wav   # filename you uploaded to ComfyUI/input/
    cd "${BACKEND_DIR:-$HOME/ltx2-avatar}"
    python server.py --port 8080 --comfy-port 8188

==> 6. EXPOSE TCP 8080 on RunPod (HTTP/TCP port), or SSH-tunnel it:
    ssh -L 8080:127.0.0.1:8080 root@<pod-ssh-host> -p <ssh-port>
    Then point TouchDesigner at ws://127.0.0.1:8080  (127.0.0.1, not localhost).
EOF

echo "==> setup.sh done."
