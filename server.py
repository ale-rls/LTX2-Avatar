"""
server.py  (runs on the RunPod GPU box)
---------------------------------------
WebSocket-over-TCP server that TouchDesigner connects to. One socket, turn-based.
Carries over the FluxRT transport decision: plain WebSocket frames over TCP, so
it works on RunPod (TCP-only) and through an SSH/VS Code tunnel. No WebRTC/UDP.

PER TURN (the "few-second thinking pause" the show wants):
  TD streams mic audio (binary PCM frames)  ->  this server
    -> VAD detects end-of-speech (silence)
    -> faster-whisper transcribes  (local on GPU box)
    -> Brain (LLM API, off-box) writes a short in-character reply
    -> patch the ComfyUI talking-avatar workflow (TTS text + scene + character)
    -> ComfyUI renders a lip-synced mp4 (progress streamed back as JSON)
    -> server sends the mp4 bytes back to TD
  TD writes mp4 to a temp file and plays it via a Movie File In TOP.

PROTOCOL (deliberately tiny):
  TD -> server:
    BINARY frame  = a chunk of mic audio, 16-bit PCM mono @ AUDIO_SR
    TEXT  {"type":"control","cmd":"start"|"stop"|"reset"|"barge_in"}
    TEXT  {"type":"config","character":"alice","voice_ref":"alice_voice.wav",
           "width":544,"height":960,"length":6}
  server -> TD:
    TEXT  {"type":"status","stage":"listening|transcribing|thinking|rendering",
           "frac":0.0..1.0,"text":"..."}
    TEXT  {"type":"transcript","text":"..."}      (what the person said)
    TEXT  {"type":"reply","text":"..."}           (what the avatar will say)
    BINARY frame  = the finished mp4 (preceded by a TEXT 'clip_begin' with size)

Run:
  python server.py --port 8080 --comfy-port 8188
"""

from __future__ import annotations
import os
import json
import time
import asyncio
import argparse
import tempfile
import wave
import struct

import numpy as np
import websockets                       # pip install websockets

from brain import Brain
from comfy_client import ComfyClient
import workflow_adapter as wfa


# ---- audio / VAD config ------------------------------------------------------
AUDIO_SR = int(os.environ.get("AUDIO_SR", "16000"))   # TD must send this rate
VAD_AGGRESSIVENESS = int(os.environ.get("VAD_AGGR", "2"))   # 0..3 (webrtcvad)
SILENCE_MS_END = int(os.environ.get("SILENCE_MS_END", "800"))  # end-of-turn
MIN_SPEECH_MS = int(os.environ.get("MIN_SPEECH_MS", "400"))    # ignore blips
FRAME_MS = 20                                          # webrtcvad needs 10/20/30


class Session:
    """Per-connection state."""
    def __init__(self):
        self.character = os.environ.get("AVATAR_CHARACTER", "default")
        self.character_image = os.environ.get("AVATAR_IMAGE")   # filename in input/
        self.voice_ref = os.environ.get("AVATAR_VOICE_REF")     # filename in input/
        self.width = int(os.environ.get("CLIP_W", "544"))
        self.height = int(os.environ.get("CLIP_H", "960"))
        self.length = int(os.environ.get("CLIP_SECONDS", "6"))
        self.character_hint = os.environ.get("AVATAR_HINT", "")
        self.fast = os.environ.get("FAST_MODE", "0") in ("1", "true", "True")
        self.pcm = bytearray()        # accumulating speech for the current turn
        self.collecting = False


class Backend:
    def __init__(self, comfy: ComfyClient, workflow_api: dict, whisper):
        self.comfy = comfy
        self.workflow_api = workflow_api
        self.whisper = whisper
        self.brain = Brain()
        self.busy = False             # one render at a time per GPU

    # -- STT ----------------------------------------------------------------
    def transcribe(self, pcm: bytes) -> str:
        # write a temp wav for faster-whisper
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            path = tf.name
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(AUDIO_SR)
            w.writeframes(pcm)
        segments, _ = self.whisper.transcribe(path, language=None, vad_filter=True)
        text = " ".join(s.text for s in segments).strip()
        try:
            os.unlink(path)
        except OSError:
            pass
        return text

    # -- one full turn ------------------------------------------------------
    async def handle_turn(self, ws, sess: Session, pcm: bytes):
        if self.busy:
            await send_json(ws, {"type": "status", "stage": "busy"})
            return
        self.busy = True
        loop = asyncio.get_event_loop()
        try:
            await send_json(ws, {"type": "status", "stage": "transcribing", "frac": 0.0})
            text = await loop.run_in_executor(None, self.transcribe, pcm)
            if not text:
                await send_json(ws, {"type": "status", "stage": "listening"})
                return
            await send_json(ws, {"type": "transcript", "text": text})

            await send_json(ws, {"type": "status", "stage": "thinking", "frac": 0.0})
            reply = await loop.run_in_executor(None, self.brain.reply, text)
            await send_json(ws, {"type": "reply", "text": reply})

            # patch workflow for this turn
            turn = wfa.Turn(
                say_text=reply,
                character_image=sess.character_image,
                voice_ref_audio=sess.voice_ref,
                width=sess.width, height=sess.height,
                length_seconds=sess.length,
                seed=int(time.time()) % 2_000_000_000,
                fast=sess.fast,
            )
            patched = wfa.patch(self.workflow_api, turn, sess.character_hint)

            # render, streaming progress back to TD
            def on_progress(stage, frac):
                # called from the comfy thread; schedule a send on the loop
                asyncio.run_coroutine_threadsafe(
                    send_json(ws, {"type": "status", "stage": "rendering",
                                   "frac": max(0.0, frac), "detail": stage}),
                    loop,
                )

            await send_json(ws, {"type": "status", "stage": "rendering", "frac": 0.0})
            prompt_id = await loop.run_in_executor(
                None,
                lambda: self.comfy.run_and_wait(patched, on_progress=on_progress),
            )
            mp4, fname = await loop.run_in_executor(
                None,
                lambda: self.comfy.get_video_bytes(prompt_id, wfa.output_node_id()),
            )

            # ship the clip: a TEXT header then the BINARY payload
            await send_json(ws, {"type": "clip_begin",
                                 "bytes": len(mp4), "filename": fname,
                                 "reply": reply})
            await ws.send(mp4)
            await send_json(ws, {"type": "status", "stage": "listening"})
        except Exception as e:
            await send_json(ws, {"type": "error", "msg": str(e)})
        finally:
            self.busy = False


async def send_json(ws, obj):
    try:
        await ws.send(json.dumps(obj))
    except Exception:
        pass


# ---- VAD-driven receive loop -------------------------------------------------
def make_vad():
    try:
        import webrtcvad
        return webrtcvad.Vad(VAD_AGGRESSIVENESS)
    except Exception:
        return None


async def connection(ws, backend: Backend):
    sess = Session()
    vad = make_vad()
    frame_bytes = int(AUDIO_SR * (FRAME_MS / 1000.0)) * 2   # 16-bit
    ring = bytearray()
    silence_ms = 0
    speech_ms = 0
    print("[server] TD connected")
    await send_json(ws, {"type": "status", "stage": "listening"})

    async for message in ws:
        # TEXT control / config
        if isinstance(message, str):
            try:
                m = json.loads(message)
            except json.JSONDecodeError:
                continue
            if m.get("type") == "config":
                if "character" in m:
                    sess.character = m["character"]
                if "voice_ref" in m:
                    sess.voice_ref = m["voice_ref"]
                if "character_image" in m:
                    sess.character_image = m["character_image"]
                if "width" in m: sess.width = int(m["width"])
                if "height" in m: sess.height = int(m["height"])
                if "length" in m: sess.length = int(m["length"])
                if "fast" in m: sess.fast = bool(m["fast"])
                await send_json(ws, {"type": "status", "stage": "configured"})
            elif m.get("type") == "control":
                cmd = m.get("cmd")
                if cmd == "reset":
                    backend.brain.reset()
                    sess.pcm.clear()
                    await send_json(ws, {"type": "status", "stage": "listening"})
                elif cmd == "stop":   # operator-forced end of turn
                    pcm = bytes(sess.pcm); sess.pcm.clear()
                    if len(pcm) > frame_bytes * (MIN_SPEECH_MS // FRAME_MS):
                        asyncio.create_task(backend.handle_turn(ws, sess, pcm))
            continue

        # BINARY audio chunk
        ring.extend(message)
        # process in fixed VAD frames
        while len(ring) >= frame_bytes:
            frame = bytes(ring[:frame_bytes]); del ring[:frame_bytes]
            is_speech = True
            if vad is not None:
                try:
                    is_speech = vad.is_speech(frame, AUDIO_SR)
                except Exception:
                    is_speech = _energy_gate(frame)
            else:
                is_speech = _energy_gate(frame)

            if is_speech:
                sess.collecting = True
                sess.pcm.extend(frame)
                speech_ms += FRAME_MS
                silence_ms = 0
            elif sess.collecting:
                sess.pcm.extend(frame)   # keep a little trailing silence
                silence_ms += FRAME_MS
                if silence_ms >= SILENCE_MS_END:
                    if speech_ms >= MIN_SPEECH_MS:
                        pcm = bytes(sess.pcm)
                        sess.pcm.clear()
                        sess.collecting = False
                        speech_ms = 0; silence_ms = 0
                        asyncio.create_task(backend.handle_turn(ws, sess, pcm))
                    else:
                        sess.pcm.clear()
                        sess.collecting = False
                        speech_ms = 0; silence_ms = 0
    print("[server] TD disconnected")


def _energy_gate(frame: bytes, thresh: float = 500.0) -> bool:
    """Fallback VAD: RMS energy gate if webrtcvad isn't installed."""
    n = len(frame) // 2
    if n == 0:
        return False
    samples = struct.unpack(f"<{n}h", frame[:n * 2])
    rms = (sum(s * s for s in samples) / n) ** 0.5
    return rms > thresh


# ---- boot --------------------------------------------------------------------
def load_workflow(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        wf = json.load(f)
    if "nodes" in wf and "links" in wf:
        raise SystemExit(
            "That's the EDITOR workflow, not the API format. In ComfyUI: enable "
            "Settings > Dev Mode, then 'Save (API Format)'. Save it as "
            "workflow_api.json and pass that. (See README.)"
        )
    return wf


def load_whisper():
    from faster_whisper import WhisperModel   # pip install faster-whisper
    size = os.environ.get("WHISPER_SIZE", "small")
    device = os.environ.get("WHISPER_DEVICE", "cuda")
    compute = os.environ.get("WHISPER_COMPUTE", "float16")
    print(f"[server] loading faster-whisper {size} on {device} ({compute})")
    return WhisperModel(size, device=device, compute_type=compute)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--comfy-host", default="127.0.0.1")
    ap.add_argument("--comfy-port", type=int, default=8188)
    ap.add_argument("--workflow", default="workflow_api.json")
    ap.add_argument("--fast", action="store_true",
                    help="default new sessions to fast mode (skip upscaler + "
                         "2nd-pass sampler; lower res, faster render)")
    args = ap.parse_args()

    if args.fast:
        os.environ["FAST_MODE"] = "1"

    workflow_api = load_workflow(args.workflow)
    comfy = ComfyClient(host=args.comfy_host, port=args.comfy_port)
    whisper = load_whisper()
    backend = Backend(comfy, workflow_api, whisper)

    print(f"[server] listening on 0.0.0.0:{args.port} (expose this TCP port)")
    async with websockets.serve(lambda ws: connection(ws, backend),
                                "0.0.0.0", args.port,
                                max_size=64 * 1024 * 1024,   # allow big mp4s
                                ping_interval=20, ping_timeout=60):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
