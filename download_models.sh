#!/usr/bin/env bash
# download_models.sh -- fetch the model files the RuneXX LTX-2.3 talking-avatar
# workflow loads, into ComfyUI/models/. Companion to setup.sh.
#
#   MODE=fp8  ./download_models.sh     # H100/H200: the authored fp8/bf16 path (default)
#   MODE=gguf ./download_models.sh     # 5090 / <=32GB: quantized GGUF path
#
# Repos below were verified live against the Hugging Face API. The EXACT filenames
# in the uploaded workflow don't all map 1:1 to public files (the author renamed some
# locally) -- the known deltas are called out inline. After downloading, ALWAYS run
#     python workflow_adapter.py --probe workflow_api.json
# and reconcile any loader-widget filename that doesn't match what landed on disk
# (rename the file, or edit the widget). This is the "verify, don't guess" step.
set -euo pipefail

COMFY_DIR="${COMFY_DIR:-$HOME/ComfyUI}"
MODE="${MODE:-fp8}"
M="$COMFY_DIR/models"
HF="${HF:-hf}"   # huggingface-cli also works: HF="huggingface-cli"

mkdir -p "$M/diffusion_models" "$M/text_encoders" "$M/vae/vae_approx" \
         "$M/loras" "$M/latent_upscale_models" "$M/clip"

# get <repo> <path-in-repo> <dest-file>   (anonymous works for all repos below)
get () {
  local repo="$1" path="$2" dest="$3"
  if [ -f "$dest" ]; then echo "   have $(basename "$dest")"; return; fi
  echo "   fetch $repo :: $path"
  local tmp; tmp="$(mktemp -d)"
  "$HF" download "$repo" "$path" --local-dir "$tmp" >/dev/null
  mkdir -p "$(dirname "$dest")"
  mv "$tmp/$path" "$dest"
  rm -rf "$tmp"
}

echo "==> shared models (both modes)"
# VAEs: repo has them WITHOUT the _KJ suffix the workflow's loader widgets expect, so
# we rename on the way in. (VAELoader + VAELoaderKJ both read models/vae/.)
get Kijai/LTX2.3_comfy vae/LTX23_video_vae_bf16.safetensors  "$M/vae/LTX23_video_vae_bf16_KJ.safetensors"
get Kijai/LTX2.3_comfy vae/LTX23_audio_vae_bf16.safetensors  "$M/vae/LTX23_audio_vae_bf16_KJ.safetensors"
get Kijai/LTX2.3_comfy vae/taeltx2_3.safetensors             "$M/vae/vae_approx/taeltx2_3.safetensors"
get Kijai/LTX2.3_comfy text_encoders/ltx-2.3_text_projection_bf16.safetensors "$M/text_encoders/ltx-2.3_text_projection_bf16.safetensors"
# Spatial upscaler: only x2-1.0 is public; the workflow names x2-1.1. Needed ONLY when
# you do NOT run --fast (fast mode orphans the upscaler branch entirely).
get Lightricks/LTX-2 ltx-2-spatial-upscaler-x2-1.0.safetensors "$M/latent_upscale_models/ltx-2-spatial-upscaler-x2-1.0.safetensors"
# ...and expose it under the 1.1 name the graph's loader widget asks for (a symlink, since
# only x2-1.0 is public). LatentUpscaleModelLoader reads models/latent_upscale_models/.
ln -sf ltx-2-spatial-upscaler-x2-1.0.safetensors \
   "$M/latent_upscale_models/ltx-2.3-spatial-upscaler-x2-1.1.safetensors"

# MelBandRoformer: this talking-avatar graph runs vocal isolation on the AUDIO path every
# turn (node 1937 stays upstream of the output after API conversion) -- NOT optional here.
# MelBandRoFormerModelLoader reads models/diffusion_models/ (not a 'melband' folder).
get Kijai/MelBandRoFormer_comfy MelBandRoformer_fp16.safetensors "$M/diffusion_models/MelBandRoformer_fp16.safetensors"

if [ "$MODE" = "fp8" ]; then
  echo "==> fp8 / bf16 path (H100/H200 -- the authored graph, everything resident)"
  get Kijai/LTX2.3_comfy diffusion_models/ltx-2.3-22b-distilled_transformer_only_fp8_scaled.safetensors \
      "$M/diffusion_models/ltx-2.3-22b-distilled_transformer_only_fp8_scaled.safetensors"
  get Comfy-Org/ltx-2 split_files/text_encoders/gemma_3_12B_it_fp8_scaled.safetensors \
      "$M/text_encoders/gemma_3_12B_it_fp8_scaled.safetensors"
elif [ "$MODE" = "gguf" ]; then
  echo "==> GGUF path (5090 / <=32GB). NB: the active graph is wired to UNETLoader, so to"
  echo "    actually USE these you must switch the model path to UnetLoaderGGUF +"
  echo "    CLIPLoaderGGUF in the UI (node 1571 is already present, just rewire to it)."
  # Public GGUF is the 1.1 build at Q4_K_M (workflow named Q4_K_S from a quantstack repo
  # we couldn't resolve publicly; Q4_K_M is a hair larger/better). Bump to Q6_K/Q8_0 if
  # you have headroom.
  get Abiray/LTX-2.3-22B-DISTILLED-1.1-GGUF LTX-2.3-22B-distilled-1.1-Q4_K_M.gguf \
      "$M/diffusion_models/LTX-2.3-22B-distilled-1.1-Q4_K_M.gguf"
  get unsloth/gemma-3-12b-it-GGUF gemma-3-12b-it-Q4_K_M.gguf \
      "$M/text_encoders/gemma-3-12b-it-Q4_K_M.gguf"   # or gemma-3-12b-it-Q2_K.gguf to save VRAM
else
  echo "unknown MODE=$MODE (use fp8 or gguf)"; exit 2
fi

cat <<EOF

==> done ($MODE). Still NOT auto-fetched (source/name unconfirmed -- grab if your graph uses them):
    - audio vocoder: workflow names 'ltx-av-step-1751000_vocoder_24K.safetensors'.
      Closest public is Lightricks/LTX-2 :: vocoder/diffusion_pytorch_model.safetensors
      -- confirm the loader's expected folder/name before placing.
    - (MelBandRoformer_fp16.safetensors is now auto-fetched above -- it is on the active
      audio path for this graph, not optional.)
    - Qwen3-TTS weights: auto-download on first run of the AILab_Qwen3TTSVoiceClone node.

Next: export workflow_api.json from the UI, then
      python workflow_adapter.py --probe workflow_api.json
and reconcile any filename the probe/validation flags.
EOF
