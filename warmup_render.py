"""warmup_render.py — fire ONE render through the exact path server.py uses, to
pre-load the models (and trigger Qwen3-TTS's first-run weight download) before a
show. See README "Known risks": do one warm-up render before going live.

  python3 warmup_render.py            # fast mode (default; skips upscaler + 2nd pass)
  python3 warmup_render.py --full     # full authored path

Reads AVATAR_IMAGE / AVATAR_VOICE_REF (filenames already in ComfyUI/input/),
defaulting to launch.sh's values. Writes warmup_<mode>.mp4 next to this file.
"""
import os, sys, json, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from comfy_client import ComfyClient
import workflow_adapter as wfa

FAST = "--full" not in sys.argv
wf = json.load(open(os.path.join(HERE, "workflow_api.json")))
turn = wfa.Turn(
    say_text=os.environ.get(
        "WARMUP_LINE",
        "Hello, traveler. The stars have waited a long time for your arrival."),
    character_image=os.environ.get("AVATAR_IMAGE", "ref-img.png"),
    voice_ref_audio=os.environ.get("AVATAR_VOICE_REF", "B.wav"),
    width=int(os.environ.get("CLIP_W", "544")),
    height=int(os.environ.get("CLIP_H", "960")),
    length_seconds=int(os.environ.get("CLIP_SECONDS", "4")),
    seed=12345, fast=FAST,
)
patched = wfa.patch(wf, turn, "")
print(f"patched ok; fast={FAST}; output_node={wfa.output_node_id()}", flush=True)

comfy = ComfyClient(host=os.environ.get("COMFY_HOST", "127.0.0.1"),
                    port=int(os.environ.get("COMFY_PORT", "8188")))
t0 = time.time(); last = [0.0]
def prog(stage, frac):
    now = time.time() - t0
    if now - last[0] > 1.5 or frac >= 1.0:
        last[0] = now
        print(f"[{now:6.1f}s] {stage} {frac:.2f}", flush=True)

pid = comfy.run_and_wait(patched, on_progress=prog)
mp4, fname = comfy.get_video_bytes(pid, wfa.output_node_id())
out = os.path.join(HERE, "warmup_%s.mp4" % ("fast" if FAST else "full"))
with open(out, "wb") as f:
    f.write(mp4)
print(f"DONE in {time.time()-t0:.1f}s -> {out} ({len(mp4)} bytes; comfy file {fname})", flush=True)
