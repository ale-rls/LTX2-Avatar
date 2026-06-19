# LTX-2 Live Interactive Avatar — TouchDesigner ⇄ RunPod GPU

A voice-driven, LLM-brained talking avatar for live theater, rendered with the
**LTX-2.3 talking-avatar ComfyUI workflow** (RuneXX, Qwen-TTS voice clone) on a
rented RunPod GPU, controlled from **TouchDesigner**.

This is the conversational-avatar successor to the FluxRT-TD-TCP project. The
transport layer and all the TouchDesigner gotchas carry over; the model layer is
completely different (see below).

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
                    ┌──────────────────── RunPod GPU box (TCP-only) ─────────────────────┐
 TouchDesigner      │                                                                    │
 ┌────────────┐ WS  │  server.py                                                         │
 │ mic CHOP   │────▶│  ├─ VAD end-of-turn   ┌───────────┐                                 │
 │ (16k mono) │ TCP │  ├─ faster-whisper ──▶│  brain.py │ LLM API (off-box, swappable)    │
 │            │     │  │   (STT, local)     └─────┬─────┘                                 │
 │ status/    │◀────│  │                          │ reply text                            │
 │ transcript │ WS  │  └─ workflow_adapter.patch( reply, character, voice ) ──┐           │
 │ reply      │     │                                                          ▼          │
 │            │     │     comfy_client ──▶ ComfyUI /prompt + ws ──▶ LTX-2.3 talking-avatar │
 │ Movie File │◀────│◀──── mp4 bytes ◀──── VHS_VideoCombine ◀───────────────────┘          │
 │ In TOP     │ WS  │                                                                    │
 └────────────┘     └────────────────────────────────────────────────────────────────────┘
```

**Transport:** plain WebSocket frames over TCP — works on RunPod (no UDP/WebRTC)
and through an SSH tunnel. Same decision as FluxRT, for the same reason.

**Files**
| file | runs where | does what |
|---|---|---|
| `server.py` | GPU box | TD-facing WS server; VAD turn detection; orchestrates STT→brain→render→clip |
| `brain.py` | GPU box (calls out) | swappable LLM brain (OpenAI / Anthropic / OpenAI-compatible) |
| `comfy_client.py` | GPU box | ComfyUI websocket+HTTP client; queue, progress, fetch mp4 |
| `workflow_adapter.py` | GPU box | patches the live values onto the exact workflow node IDs |
| `td_avatar_ext.py` | TouchDesigner | extension: mic out, receive clip, drive Movie File In TOP |
| `ws_callbacks.py` | TouchDesigner | WebSocket DAT callbacks → extension |
| `setup.sh` | GPU box | installs ComfyUI + the exact custom nodes + model checklist |

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
4. **Export the API-format workflow once** (this is required — the editor JSON
   you uploaded is NOT what the API runs):
   - ComfyUI → Settings → enable **Dev mode** → menu **Save (API Format)**
   - save as `workflow_api.json` next to `server.py`
   - verify node IDs survived the export:
     ```
     python workflow_adapter.py --probe workflow_api.json
     ```
     Fix any IDs it flags in `NODE_IDS` (subgraphs flatten + renumber on export).
5. Upload a character image and a voice-reference clip into `ComfyUI/input/`
   (drag onto a LoadImage/LoadAudio node in the UI, or POST to `/upload/image`).
6. Launch the backend:
   ```
   export LLM_PROVIDER=openai          # or anthropic / openai_compatible
   export OPENAI_API_KEY=sk-...
   export AVATAR_IMAGE=alice.png       # filename in ComfyUI/input/
   export AVATAR_VOICE_REF=alice.wav
   export AVATAR_PERSONA="You are Cassandra, a weary oracle. One or two short lines."
   python server.py --port 8080 --comfy-port 8188
   ```
7. Expose TCP **8080** on RunPod, or tunnel it:
   `ssh -L 8080:127.0.0.1:8080 root@<pod-ssh-host> -p <ssh-port>`

---

## Setup (TouchDesigner)

Build a **Base COMP** containing:

- a **Text DAT** `AvatarExt_logic` ← paste `td_avatar_ext.py`
- a **Text DAT** `ws_callbacks` ← paste `ws_callbacks.py`
- a **WebSocket DAT** `ws`
  - Callbacks DAT = `ws_callbacks`
  - Network Address = your `ws://…proxy.runpod.net` or `ws://127.0.0.1:8080`
    (**127.0.0.1, never `localhost`** — IPv6 ::1 gotcha)
- a mic input → **Audio Device In CHOP** → **Resample CHOP** (16000) → keep one
  channel → name the final CHOP `mic_out`
- a **CHOP Execute DAT** on `mic_out` → in `onValueChange`/`whileOn` call
  `parent().ext.AvatarExt.OnAudioCook()`  ← **nothing streams without this**
- a **Movie File In TOP** `avatar_player` (this displays the avatar)
- a **Parameter Execute DAT** to forward custom-par pulses (Connect/Reset/EndTurn)

**Extension naming — must agree in three places (FluxRT lesson):**
- class `AvatarExt`
- COMP `Extension 1` par = `op('./AvatarExt_logic').module.AvatarExt(me)`
- Promote Extension = On; callbacks reach it via `parent().ext.AvatarExt`

**Custom pars on the COMP:** `Connect`(pulse) `Address`(str) `Character`(str)
`Voiceref`(str) `Charimage`(str) `Clipw`/`Cliph`(int) `Cliplen`(int)
`Fastmode`(toggle) `Pushtotalk`(toggle) plus read-only `Status`/`Heard`/`Reply`
strings for monitoring.

Press **Connect**, speak, stop — after the pause the clip appears in
`avatar_player`.

---

## Per-turn flow (what actually happens)

1. TD streams int16 PCM @16k as WS binary while you talk.
2. `server.py` runs VAD; ~800 ms of silence ends the turn.
3. faster-whisper transcribes → `{"type":"transcript"}` to TD.
4. `brain.py` returns a short in-character line → `{"type":"reply"}` to TD.
5. `workflow_adapter.patch()` injects the line into Qwen-TTS `target_text` and
   the LTX scene prompt, plus the character image + voice ref.
6. `comfy_client.run_and_wait()` queues it and streams `{"type":"status",
   "stage":"rendering","frac":…}` to TD during the wait.
7. The finished mp4 is fetched from `VHS_VideoCombine` and sent to TD as one
   binary payload (after a `clip_begin` header).
8. TD writes it to a temp file and plays it in the Movie File In TOP.

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
     - per session from TD: the `Fastmode` toggle (sent in the config message)
   The rewire is topology-based (finds the two samplers/separators by class and
   link structure, not by ID), so it survives API-format re-export. It fails
   *loud* if the expected two-pass shape isn't found rather than shipping a
   broken graph.
3. **Reply length.** `MAX_REPLY_WORDS` (default 40) caps the line → shorter TTS
   → shorter clip → faster render. Keep the persona terse.
4. **Clip resolution / length.** `Clipw/Cliph/Cliplen` (and env `CLIP_*`).
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
- **Qwen-TTS first run downloads weights**; do one warm-up render before a show.
- **One render at a time** per GPU (`self.busy`); barge-in mid-render is not
  supported yet (TD can send `control/reset` to clear the brain between turns).
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
```
