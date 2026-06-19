# LTX-2 Live Interactive Avatar — Browser ⇄ RunPod GPU

A voice-driven, LLM-brained talking avatar for live theater, rendered with the
**LTX-2.3 talking-avatar ComfyUI workflow** (RuneXX, Qwen3-TTS voice clone) on a
rented RunPod GPU, driven entirely from a **web browser** — no TouchDesigner, no
extra client. The page captures the mic, streams it to the GPU box, and plays the
rendered avatar clips full-screen. The brain is **Qwen3**, running locally via
**Ollama** on the same box.

> **This `browser` branch** replaces the original TouchDesigner client with a
> self-contained web page that `server.py` serves at `GET /` on the same port as
> the WebSocket. The legacy TD files (`td_avatar_ext.py`, `ws_callbacks.py`,
> `param_exec_callbacks.py`) are still in the repo but unused here.

---

## The one thing to understand first

**LTX-2 is a clip generator, not a real-time streaming model like FluxRT.**

FluxRT streamed frames in and out continuously, so that project needed two
decoupled loops matching the model's frame cadence. LTX-2 instead takes inputs
(a reference image + a spoken line), thinks for several seconds, and emits one
finished lip-synced video clip. So this project is **turn-based**, not streaming:

```
person speaks → [pause: STT → LLM → TTS → LTX-2 render] → avatar plays a clip → repeat
```

You said a few-second "thinking" pause is fine / desirable — that is exactly
what makes this viable. The pause is the character thinking. Design the show
around call-and-response, not snappy back-and-forth.

There is **no frame-cadence problem to solve here** (the deepest FluxRT lesson
doesn't apply). The new hard part is render latency, addressed below.

---

## Architecture

```
                    ┌──────────────────── RunPod GPU box ────────────────────────────────┐
 Browser            │                                                                    │
 ┌────────────┐ WS  │  server.py  (serves the page at GET /, + WS on the same port)      │
 │ mic        │────▶│  ├─ VAD end-of-turn   ┌───────────┐                                 │
 │ (16k mono) │ TCP │  ├─ faster-whisper ──▶│  brain.py │──▶ Qwen3 via Ollama (localhost) │
 │            │     │  │   (STT, local)     └─────┬─────┘                                 │
 │ status/    │◀────│  │                          │ reply text                            │
 │ transcript │ WS  │  └─ workflow_adapter.patch( reply, character, voice ) ──┐           │
 │ reply      │     │                                                          ▼          │
 │            │     │     comfy_client ──▶ ComfyUI /prompt + ws ──▶ LTX-2.3 talking-avatar │
 │ <video>    │◀────│◀──── mp4 bytes ◀──── VHS_VideoCombine ◀───────────────────┘          │
 │ full-screen│ WS  │                                                                    │
 └────────────┘     └────────────────────────────────────────────────────────────────────┘
```

**Transport:** plain WebSocket frames over TCP — works on RunPod (no UDP/WebRTC)
and through an SSH tunnel. Same decision as FluxRT, for the same reason. The HTML
page is served over HTTP on the *same* port (`websockets` `process_request` hook),
so a single exposed TCP port handles both the UI and the socket.

**Files**
| file | runs where | does what |
|---|---|---|
| `server.py` | GPU box | serves the browser UI + WS on one port; VAD turn detection; orchestrates STT→brain→render→clip |
| `static/index.html` | browser | full-screen avatar UI: mic capture, WS client, clip playback, settings drawer |
| `brain.py` | GPU box | swappable LLM brain — **Qwen3 via Ollama** by default (also OpenAI / Anthropic / OpenAI-compatible) |
| `comfy_client.py` | GPU box | ComfyUI websocket+HTTP client; queue, progress, fetch mp4 |
| `workflow_adapter.py` | GPU box | patches the live values onto the exact workflow node IDs |
| `launch.sh` | GPU box | starts Ollama (pulls Qwen3), then ComfyUI, then server.py |
| `setup.sh` | GPU box | installs ComfyUI + the exact custom nodes + Ollama + model checklist |
| `td_avatar_ext.py`, `ws_callbacks.py`, `param_exec_callbacks.py` | (legacy) | original TouchDesigner client — unused on this branch |

---

## Setup (GPU box)

1. `bash setup.sh` — installs ComfyUI and the custom-node packs the workflow
   needs (KJNodes, VideoHelperSuite, QwenTTS, MelBandRoFormer, AudioTools, mtb,
   rgthree, Easy-Use, GGUF). It also **pins your pre-installed CUDA torch** so the
   installs can't swap it, force-installs a CPU **onnxruntime** (faster-whisper's
   VAD needs it), and re-adds the QwenTTS deps the resolver drops — see the gotchas
   list below for why each of those exists.
2. Download models with the companion script (verified HF repos + paths):
   - **H100/H200:** `COMFY_DIR=~/ComfyUI MODE=fp8 ./download_models.sh` — the authored
     fp8/bf16 path, higher res, everything stays resident.
   - **5090 (32GB):** `COMFY_DIR=~/ComfyUI MODE=gguf ./download_models.sh` — GGUF UNet
     + Q2/Q4 Gemma, keep tiled VAE, 768×512–960×544.
   - The uploaded workflow's loader filenames don't all match public files 1:1 (the
     author renamed some). `download_models.sh` handles the known deltas, but always
     `--probe` afterwards (step 4) and reconcile anything it flags.
3. Start ComfyUI: `python main.py --listen 127.0.0.1 --port 8188`
4. **Generate the API-format workflow once** (required — the editor JSON is NOT
   what the API runs). The conversion needs ComfyUI's node registry, so it must
   happen on this box with **all custom nodes installed** (`setup.sh` did that).
   If a node pack is missing, the converted nodes lose their `class_type` and the
   workflow is dead — both paths below guard against / detect that.

   **Easiest — the converter endpoint** (cloned by `setup.sh`):
   ```
   python3 make_workflow_api.py user-inputs/<your-editor-workflow>.json
   ```
   This POSTs the editor JSON to ComfyUI's `/workflow/convert` (handles
   subgraphs), writes `workflow_api.json`, and runs the probe automatically. It
   aborts if any node comes back without a `class_type`.

   **Or by hand:** ComfyUI → Settings → enable **Dev mode** → **Save (API
   Format)** → save as `workflow_api.json` next to `server.py`, then verify:
   ```
   python3 workflow_adapter.py --probe workflow_api.json
   ```
   Either way, fix any IDs the probe flags in `NODE_IDS` (subgraphs flatten +
   renumber on conversion).
5. Upload a character image and a voice-reference clip into `ComfyUI/input/`
   (drag onto a LoadImage/LoadAudio node in the UI, or POST to `/upload/image`).
6. Launch everything with **`./launch.sh`** — it starts Ollama (pulling `qwen3`
   on first run, ~5 GB), waits for ComfyUI, then starts the backend. Or by hand:
   ```
   ollama serve &                      # brain endpoint on :11434
   ollama pull qwen3                   # first run only
   export LLM_PROVIDER=qwen3           # default; talks to localhost:11434
   export AVATAR_IMAGE=alice.png       # filename in ComfyUI/input/
   export AVATAR_VOICE_REF=alice.wav
   export AVATAR_PERSONA="You are Cassandra, a weary oracle. One or two short lines."
   python server.py --port 8080 --comfy-port 8188
   ```
   To use a hosted brain instead of Ollama, set `LLM_PROVIDER=openai|anthropic|
   openai_compatible` with the matching key/URL — see the header of `brain.py`.
7. Expose TCP **8080** on RunPod, or tunnel it:
   `ssh -L 8080:127.0.0.1:8080 root@<pod-ssh-host> -p <ssh-port>`
8. Open **`http://<host>:8080/`** (or `http://127.0.0.1:8080/` through the tunnel)
   in a browser. Allow the mic, press **Connect**, and talk.

---

## Using the browser UI

`server.py` serves the page itself at `GET /` on the same port as the WebSocket —
one exposed port does both. Open it and:

1. The **settings drawer** (⚙, open by default) pre-fills the server URL to the
   page's own origin. Set the **Character ID**, **Voice Reference File**, and
   **Character Image File** (filenames already in `ComfyUI/input/`), plus clip
   **Width/Height/Length**.
2. Press **Connect**. The browser asks for mic permission, opens the WebSocket,
   sends the config, and starts streaming 16 kHz mono PCM continuously.
3. **Just talk** — no push-to-talk. The server's VAD ends your turn on ~800 ms of
   silence. After the thinking pause the avatar clip plays full-screen and holds
   on its last frame until the next turn.
4. **Controls** in the drawer: **Fast Mode** (applies live), **Reset Brain**
   (clears conversation history), **End Turn** (force-end the current turn).
5. **Double-click** anywhere to toggle fullscreen.

Status, the last transcript, and the avatar's reply show in the bottom bar and the
drawer; a thin VU bar on the left edge confirms the mic is live.

> **Mic needs a secure context.** Browsers only allow `getUserMedia` on a secure
> origin. `http://localhost` / `http://127.0.0.1` (e.g. via the SSH tunnel) counts
> as secure, but a plain `http://<remote-ip>` does **not** — tunnel it, or put the
> server behind HTTPS.

---

## Per-turn flow (what actually happens)

1. The browser streams int16 PCM @16k as WS binary, continuously.
2. `server.py` runs VAD; ~800 ms of silence ends the turn.
3. faster-whisper transcribes → `{"type":"transcript"}` to the browser.
4. `brain.py` asks **Qwen3 (via Ollama)** for a short in-character line and strips
   any `<think>` block → `{"type":"reply"}` to the browser.
5. `workflow_adapter.patch()` injects the line into Qwen3-TTS `target_text` and
   the LTX scene prompt, plus the character image + voice ref.
6. `comfy_client.run_and_wait()` queues it and streams `{"type":"status",
   "stage":"rendering","frac":…}` during the wait.
7. The finished mp4 is fetched from `VHS_VideoCombine` and sent to the browser as
   one binary payload (after a `clip_begin` header carrying the byte count).
8. The browser assembles the bytes into a Blob and plays it in a full-screen
   `<video>`, holding the last frame between turns.

---

## Latency levers (in order of impact)

1. **GPU tier.** H100/H200 keeps all models resident → no reload-between-turns
   stall. Biggest single win; bigger than any code change. Prototype on the
   5090, run shows on a bigger card.
2. **`--fast` mode.** Skips the spatial upscaler + the entire second-pass
   sampler, decoding straight from the first-pass latent. Roughly halves the
   sampling work and drops the heaviest VAE-tiled decode to the smaller frame.
   Output is lower resolution and a touch softer, but it's the fastest path to a
   talking clip. Toggle three ways:
     - server default: `python server.py --fast`
     - env: `FAST_MODE=1`
     - live from the browser: the **Fast Mode** toggle in the settings drawer
   The rewire is topology-based (finds the two samplers/separators by class and
   link structure, not by ID), so it survives API-format re-export. It fails
   *loud* if the expected two-pass shape isn't found rather than shipping a
   broken graph.
3. **Reply length.** `MAX_REPLY_WORDS` (default 40) caps the line → shorter TTS
   → shorter clip → faster render. Keep the persona terse.
4. **Clip resolution / length.** Width/Height/Length in the drawer (and env `CLIP_*`).
5. **Quantization.** GGUF/fp8 vs bf16 trades quality for speed.

Suggested workflow: measure a fixed line at full quality first, then with
`--fast`, on your target GPU. That tells you whether fast mode alone gets you
under your acceptable pause, or whether you also need a bigger card.

---

## Known risks / honest caveats

- **API-format export is mandatory and renumbers nodes.** The `--probe` step
  exists precisely for this; do it after every workflow re-export.
- **VRAM thrash on 32GB** is the real latency killer, not framework overhead.
  If renders balloon between turns, you're reloading models → go bigger GPU or
  go GGUF.
- **Qwen3-TTS first run downloads weights**; do one warm-up render before a show —
  run `python3 warmup_render.py` (fires one turn through the real render path).
- **Qwen3 (the brain) shares GPU VRAM with LTX-2.** Ollama loads it (~5 GB for the
  8B default) on the same card. Fine on H100/H200; tight on ≤32 GB alongside the
  fp8 path — use a smaller tag (`OLLAMA_MODEL=qwen3:4b ./launch.sh`), point
  `LLM_BASE_URL` at an off-box endpoint, or use the GGUF LTX path if you hit OOM.
- **One render at a time** per GPU (`self.busy`); barge-in mid-render is not
  supported yet (use **Reset Brain** in the drawer to clear history between turns).
- **Verify, don't guess** (FluxRT process lesson): the ComfyUI API surface used
  here (`/prompt`, `/history`, `/view`, `/ws`, `/upload/image`) is stable, but
  custom-node `class_type`s and widget names can change with updates — re-probe.

### Install gotchas verified on a real clean install (baked into `setup.sh`)

- **torch gets clobbered if you don't pin it.** ComfyUI's `requirements.txt` lists
  `torch`/`torchvision`/`torchaudio` *unpinned*, and **ComfyUI-QwenTTS pins
  `torch>=2.9.1`**. On a pod with a working CUDA torch, an unconstrained install
  swaps it for a mismatched build and kills the GPU. `setup.sh` pins the
  pre-installed versions via `PIP_CONSTRAINT` for every install. (QwenTTS's node
  imports fine on older torch — verified on 2.4.1 — so the pin is safe.)
- **`onnxruntime` is a hidden hard dep of the STT path.** `server.py` transcribes
  with `vad_filter=True`, whose Silero VAD runs on onnxruntime; it's now in
  `requirements.txt`. Worse, `comfy_mtb` pulls **`onnxruntime-gpu`** which can target
  a different CUDA major than torch (we hit `libcudart.so.13` on a CUDA-12 box) and
  then fails to import — `setup.sh` forces a CPU onnxruntime that always loads.
- **QwenTTS deps get silently dropped.** Because of its `torch>=2.9.1` line, pip
  aborts QwenTTS's whole `requirements.txt` under the torch pin and installs *none*
  of it — losing `openai-whisper` + `tiktoken`. `setup.sh` re-adds them explicitly.
- **numpy jumps to 2.x.** ComfyUI's deps upgrade numpy (1.26 → 2.x); fine for our
  stack (numba ≥ 0.61, ctranslate2 ≥ 4.x cope), but it's what *broke the old
  onnxruntime* import above — keep that in mind if a compiled dep starts misbehaving.
- **Don't trust the editor JSON's embedded `extra.prompt`.** It can be a stale,
  *different* graph (we found one missing `LoadImage`/`LoadAudio`/Qwen entirely).
  Always export API format from the UI; `--probe` what you exported.
- **Model filenames drift.** The uploaded graph references `*_KJ`-suffixed VAEs,
  a `Q4_K_S` GGUF, a `spatial-upscaler-x2-1.1`, and a specifically-named vocoder that
  don't all exist verbatim in public repos. `download_models.sh` pulls the verified
  equivalents and renames where it can; reconcile the rest after `--probe`.
- **24 GB (4090) is below the floor for the authored path.** fp8-22B (~22 GB) alone
  nearly fills it; with fp8 Gemma it won't co-reside. Use the GGUF path on ≤32 GB, or
  a bigger card for the fp8/bf16 path.

### More gotchas, verified on a clean H100 80GB bring-up (2026-06)

A full browser-branch bring-up on a fresh H100 hit four more breakages past the list
above — all now handled in `setup.sh` / `download_models.sh`, recorded here so the next
run recognizes them fast. (The fp8 path itself works end-to-end on the H100: a `--fast`
warm-up render produced a lip-synced clip in ~77 s including first-run TTS download.)

- **Ollama's installer needs `zstd`.** It now unpacks a `.tar.zst`; without `zstd` it
  aborts (*"This version requires zstd for extraction"*), and under `set -e` that single
  failure stops `setup.sh` at the Ollama step (everything before it already succeeded).
  `setup.sh` apt-installs `zstd` first (plus `pciutils`/`lshw` so Ollama cleanly detects
  the GPU — it bundles its own CUDA runner regardless).
- **QwenTTS breaks in two ways its `requirements.txt` doesn't cover:**
  1. **`sox` is a hidden hard dep.** The vendored `qwen_tts/.../vq/speech_vq.py` does
     `import sox`, which isn't in its requirements → the first TTS render dies. The node
     reports this as *"qwen_tts is not available … cannot import name
     'Qwen3TTSTokenizerV1Config'"* — a catch-all that **hides the real cause**; read the
     full import traceback. Needs both the `sox` **CLI** (`apt install sox libsox-fmt-all`)
     and the `sox` **pip** package.
  2. **transformers decorator drift.** Its 12 Hz tokenizer decorates `forward` with
     `@check_model_inputs()`, but transformers (4.57.x *and* 5.x) define
     `check_model_inputs(func)` as a **direct** decorator → `TypeError: … missing 1
     required positional argument: 'func'`. Fix = drop the parens (`@check_model_inputs`,
     exactly how transformers' own gemma3 model uses it). `setup.sh` seds this and pins
     `transformers==4.57.1` — QwenTTS's stated floor; ComfyUI only needs `>=4.50.3`,
     doesn't use `check_model_inputs`, and keeps Gemma3 (the LTX text encoder), so the
     LTX path is unaffected (verified by a full render).
- **MelBandRoFormer is on the live audio path here, not optional.** API conversion keeps
  `MelBandRoFormerModelLoader` (node 1937) upstream of the output, so it runs every turn.
  It loads from **`models/diffusion_models/`** (not a `melband` folder) — `download_models.sh`
  now fetches `MelBandRoformer_fp16.safetensors` from `Kijai/MelBandRoFormer_comfy`.
- **Windows widget paths survive conversion and fail on Linux.** This editor JSON was
  saved on Windows, so active loaders came through with back-slashed sub-paths — e.g.
  `LTXVideo\v2\…fp8_scaled.safetensors` (UNETLoader), `vae_approx\taeltx2_3…`,
  `MelBandRoformer\…`. ComfyUI's Linux `get_filename_list` never emits those, so the
  prompt fails validation even with the file on disk. After `--probe`, rewrite each
  model-file widget in `workflow_api.json` to the value the node's `/object_info` lists
  (forward-slash sub-path or bare basename). The spatial upscaler is a separate
  name-only miss (`x2-1.1` wanted, only `x2-1.0` public) → symlink it in
  `models/latent_upscale_models/`; `--fast` orphans that branch anyway.
