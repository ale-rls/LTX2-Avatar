"""
comfy_client.py
---------------
Talks to a running ComfyUI instance on the GPU box via its websocket + HTTP API.
Submits a patched API-format workflow, follows real-time progress events, and
retrieves the finished mp4 bytes.

Why websocket (your choice): you get 'progress' and 'executing' events so the
backend can tell TouchDesigner "thinking... 40%..." during the render pause,
instead of a blind wait. Good for a live show.

ComfyUI API surface used (stable, verified against ComfyUI server.py routes):
  POST /prompt                      -> {"prompt_id": ...}        queue a job
  GET  /history/{prompt_id}         -> outputs incl. saved files
  GET  /view?filename=&subfolder=&type=output  -> raw file bytes
  WS   /ws?clientId=...             -> {"type":"progress"|"executing"|...}

Upload helpers:
  POST /upload/image  (multipart)   -> put a character image / audio into input/
ComfyUI's /upload/image accepts audio too (it just lands in input/).
"""

from __future__ import annotations
import json
import time
import uuid
import urllib.parse
import requests
from websocket import create_connection   # pip install websocket-client


class ComfyClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8188,
                 client_id: str | None = None):
        # 127.0.0.1 not localhost (FluxRT IPv6 gotcha carries over to any tunnel)
        self.http = f"http://{host}:{port}"
        self.ws_url = f"ws://{host}:{port}/ws"
        self.client_id = client_id or str(uuid.uuid4())

    # -- inputs -------------------------------------------------------------
    def upload_file(self, local_path: str, dest_name: str | None = None,
                    overwrite: bool = True, subfolder: str = "") -> str:
        """Upload an image or audio file into ComfyUI's input/ dir.
        Returns the filename to reference from LoadImage / LoadAudio."""
        name = dest_name or local_path.split("/")[-1]
        with open(local_path, "rb") as f:
            files = {"image": (name, f, "application/octet-stream")}
            data = {"overwrite": "true" if overwrite else "false",
                    "subfolder": subfolder}
            r = requests.post(f"{self.http}/upload/image", files=files, data=data,
                              timeout=60)
        r.raise_for_status()
        j = r.json()
        return j.get("name", name)

    def upload_bytes(self, raw: bytes, dest_name: str,
                     subfolder: str = "") -> str:
        files = {"image": (dest_name, raw, "application/octet-stream")}
        data = {"overwrite": "true", "subfolder": subfolder}
        r = requests.post(f"{self.http}/upload/image", files=files, data=data,
                          timeout=60)
        r.raise_for_status()
        return r.json().get("name", dest_name)

    # -- run ----------------------------------------------------------------
    def queue(self, workflow_api: dict) -> str:
        r = requests.post(f"{self.http}/prompt",
                          json={"prompt": workflow_api,
                                "client_id": self.client_id},
                          timeout=30)
        if r.status_code != 200:
            # ComfyUI returns rich validation errors; surface them.
            raise RuntimeError(f"/prompt rejected: {r.status_code} {r.text}")
        return r.json()["prompt_id"]

    def run_and_wait(self, workflow_api: dict, on_progress=None,
                     timeout: float = 600.0) -> str:
        """Queue a job and block until it finishes. Calls
        on_progress(stage:str, frac:float) as events arrive. Returns prompt_id."""
        ws = create_connection(f"{self.ws_url}?clientId={self.client_id}",
                               timeout=timeout)
        try:
            prompt_id = self.queue(workflow_api)
            deadline = time.time() + timeout
            while time.time() < deadline:
                msg = ws.recv()
                if not isinstance(msg, str):
                    continue  # binary preview frames; ignore
                evt = json.loads(msg)
                etype = evt.get("type")
                data = evt.get("data", {})
                if etype == "progress" and on_progress:
                    mx = data.get("max", 1) or 1
                    on_progress("sampling", data.get("value", 0) / mx)
                elif etype == "executing":
                    if data.get("node") is None and data.get("prompt_id") == prompt_id:
                        return prompt_id  # done
                    if on_progress and data.get("node") is not None:
                        on_progress(f"node:{data.get('node')}", -1.0)
                elif etype == "execution_error":
                    raise RuntimeError(f"ComfyUI execution error: {data}")
            raise TimeoutError("ComfyUI render timed out")
        finally:
            ws.close()

    # -- outputs ------------------------------------------------------------
    def get_video_bytes(self, prompt_id: str, node_id: str) -> tuple[bytes, str]:
        """Pull the mp4 produced by a VHS_VideoCombine node. Returns (bytes,
        filename). VHS reports its file under 'gifs' in the history outputs."""
        r = requests.get(f"{self.http}/history/{prompt_id}", timeout=30)
        r.raise_for_status()
        hist = r.json().get(prompt_id, {})
        outputs = hist.get("outputs", {})
        node_out = outputs.get(str(node_id), {})

        # VHS_VideoCombine lists results under "gifs"; fall back to any list of
        # {filename, subfolder, type} dicts found in this node's outputs.
        candidates = node_out.get("gifs") or []
        if not candidates:
            for v in node_out.values():
                if isinstance(v, list) and v and isinstance(v[0], dict) \
                        and "filename" in v[0]:
                    candidates = v
                    break
        # prefer an actual video file
        vids = [c for c in candidates
                if str(c.get("filename", "")).lower().endswith((".mp4", ".webm", ".mov"))]
        chosen = (vids or candidates)
        if not chosen:
            raise RuntimeError(f"No video output found for node {node_id}: {node_out}")
        item = chosen[-1]
        params = urllib.parse.urlencode({
            "filename": item["filename"],
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        })
        v = requests.get(f"{self.http}/view?{params}", timeout=120)
        v.raise_for_status()
        return v.content, item["filename"]
