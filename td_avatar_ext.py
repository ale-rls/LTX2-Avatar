"""
td_avatar_ext.py  -- TouchDesigner Extension (paste into a Base COMP)
---------------------------------------------------------------------
Client side of the live interactive avatar. Runs inside TouchDesigner.

Responsibilities:
  - capture mic audio, stream it as 16-bit PCM mono @ 16 kHz to the backend
  - receive status/transcript/reply JSON and the finished mp4 bytes
  - write the mp4 to a temp file and point a Movie File In TOP at it (this is
    the verified way to get a *whole clip* into TD; there is no TOP method to
    load raw video bytes directly)

=====================  READ THIS — TD SETUP (carries the FluxRT lessons)  ======

THREE-PLACE NAMING (must all agree, or you get silent failpropertyures /
cryptic AttributeErrors):
  1) This class is named  AvatarExt
  2) The Base COMP's  Extension 1  parameter =  op('./AvatarExt_logic').module.AvatarExt(me)
       (a Text DAT named 'AvatarExt_logic' holding THIS code)
  3) Promote Extension = On, and callbacks reach it via  parent().ext.AvatarExt

WIRING (nothing fires on its own in TD):
  - A  WebSocket DAT  named 'ws'  -> its callbacks DAT must forward to the
    extension. Set the WebSocket DAT 'Received Text'/'Received Binary' to call
    the functions in the companion 'ws_callbacks' DAT below.
  - A  Parameter Execute DAT  to forward custom-par changes (Connect pulse etc).
  - A  CHOP Execute DAT  on your mic CHOP (cooking) to push audio each cook:
        it must call  parent().ext.AvatarExt.OnAudioCook()  in onValueChange/
        whileOn. A frame timer alone does NOTHING without this.

NETWORK:
  - Use the EXTERNAL RunPod host:port (its exposed TCP port), e.g.
        ws://<pod-id>-8080.proxy.runpod.net   or your SSH-tunnel 127.0.0.1:8080
  - If tunneling locally, use 127.0.0.1 explicitly, NOT 'localhost' (TD's embedded
    Chromium / some resolvers pick IPv6 ::1 and miss an IPv4-only listener).

THREAD-SAFETY:
  - TD's Python API is not thread-safe. The WebSocket DAT callbacks already run
    on the main thread, so we do all op()/.par work there. We never touch ops
    from a background thread.

CUSTOM PARS to add to the Base COMP (Component Editor):
    Connect      (pulse)   -> connect/disconnect the socket
    Address      (str)     -> ws://host:port
    Character    (str)     -> character id (sent in config)
    Voiceref     (str)     -> filename of voice-ref already uploaded to ComfyUI
    Charimage    (str)     -> filename of character image already in ComfyUI
    Clipw/Cliph  (int)     -> render width/height
    Cliplen      (int)     -> clip length seconds
    Fastmode     (toggle)  -> skip upscaler + 2nd-pass sampler (faster, lower res)
    Pushtotalk   (toggle)  -> if On, only stream audio while held (operator turn)
================================================================================
"""

import json
import os
import tempfile


class AvatarExt:
    def __init__(self, ownerComp):
        self.Owner = ownerComp
        self._buf = bytearray()          # incoming mp4 assembly
        self._expecting = 0              # bytes expected for current clip
        self._last_reply = ""
        self._clip_dir = tempfile.mkdtemp(prefix="ltx_avatar_")
        self._clip_index = 0
        self._status("idle")

    # ---- connection -------------------------------------------------------
    def Connect(self):
        ws = self._ws()
        if ws is None:
            self._log("No WebSocket DAT named 'ws' found."); return
        addr = self.Owner.par.Address.eval() if hasattr(self.Owner.par, "Address") else ""
        # par.active toggles the DAT connection; set the address on the DAT pars.
        try:
            ws.par.netaddress = addr or ws.par.netaddress.eval()
        except Exception:
            pass
        ws.par.active = not bool(ws.par.active.eval())
        if ws.par.active.eval():
            self._status("connecting")
        else:
            self._status("idle")

    def OnConnect(self):
        self._status("connected")
        self._send_config()

    def OnDisconnect(self):
        self._status("idle")

    def _send_config(self):
        p = self.Owner.par
        cfg = {"type": "config"}
        if hasattr(p, "Character"):  cfg["character"] = p.Character.eval()
        if hasattr(p, "Voiceref"):   cfg["voice_ref"] = p.Voiceref.eval()
        if hasattr(p, "Charimage"):  cfg["character_image"] = p.Charimage.eval()
        if hasattr(p, "Clipw"):      cfg["width"] = int(p.Clipw.eval())
        if hasattr(p, "Cliph"):      cfg["height"] = int(p.Cliph.eval())
        if hasattr(p, "Cliplen"):    cfg["length"] = int(p.Cliplen.eval())
        if hasattr(p, "Fastmode"):   cfg["fast"] = bool(p.Fastmode.eval())
        self._send_text(cfg)

    def Reset(self):
        self._send_text({"type": "control", "cmd": "reset"})

    def EndTurn(self):
        """Operator-forced end of turn (push-to-talk release)."""
        self._send_text({"type": "control", "cmd": "stop"})

    # ---- audio out --------------------------------------------------------
    def OnAudioCook(self):
        """
        Called by a CHOP Execute DAT on the mic CHOP each cook. Reads the latest
        samples (resolved on the MAIN thread here) and streams them as 16-bit
        PCM. Expect a CHOP that is mono and resampled to 16 kHz upstream (use a
        Resample CHOP + a single channel). TD audio samples are float -1..1.
        """
        ws = self._ws()
        if ws is None or not self._connected():
            return
        if hasattr(self.Owner.par, "Pushtotalk") and self.Owner.par.Pushtotalk.eval():
            # only stream while the toggle is held; release calls EndTurn()
            pass
        chop = self.op_mic()
        if chop is None or chop.numChans == 0:
            return
        ch = chop[0]
        n = len(ch)
        if n == 0:
            return
        # float -> int16 little-endian
        pcm = bytearray(n * 2)
        for i in range(n):
            v = ch[i]
            if v > 1.0: v = 1.0
            elif v < -1.0: v = -1.0
            s = int(v * 32767.0)
            pcm[2 * i] = s & 0xFF
            pcm[2 * i + 1] = (s >> 8) & 0xFF
        try:
            ws.sendBytes(bytes(pcm))
        except Exception as e:
            self._log(f"sendBytes failed: {e}")

    # ---- inbound from server ---------------------------------------------
    def OnReceiveText(self, text):
        try:
            m = json.loads(text)
        except Exception:
            return
        t = m.get("type")
        if t == "status":
            stage = m.get("stage", "")
            frac = m.get("frac", None)
            self._status(stage if frac is None else f"{stage} {int(frac*100)}%")
        elif t == "transcript":
            self._set_str("Heard", m.get("text", ""))
        elif t == "reply":
            self._last_reply = m.get("text", "")
            self._set_str("Reply", self._last_reply)
        elif t == "clip_begin":
            self._expecting = int(m.get("bytes", 0))
            self._buf = bytearray()
            self._status("receiving clip")
        elif t == "error":
            self._log("server error: " + m.get("msg", ""))
            self._status("error")

    def OnReceiveBinary(self, data):
        # mp4 payload (may arrive in one or several binary messages)
        self._buf.extend(data)
        if self._expecting and len(self._buf) >= self._expecting:
            self._finish_clip(bytes(self._buf[:self._expecting]))
            self._buf = bytearray()
            self._expecting = 0

    def _finish_clip(self, mp4: bytes):
        # rotate temp files so the player can keep the previous one open briefly
        self._clip_index = (self._clip_index + 1) % 4
        path = os.path.join(self._clip_dir, f"clip_{self._clip_index}.mp4")
        with open(path, "wb") as f:
            f.write(mp4)
        player = self.op_player()
        if player is not None:
            player.par.file = path
            try:
                player.par.reloadpulse.pulse()
            except Exception:
                pass
            try:
                player.par.cuepulse.pulse()   # restart from frame 0
                player.par.play = True
            except Exception:
                pass
        self._status("playing")

    # ---- helpers (all main-thread) ---------------------------------------
    def _ws(self):
        return self.Owner.op("ws")

    def _connected(self):
        ws = self._ws()
        try:
            return bool(ws.par.active.eval())
        except Exception:
            return False

    def op_mic(self):
        # a CHOP named 'mic_out': mono, 16 kHz, float samples
        return self.Owner.op("mic_out")

    def op_player(self):
        # a Movie File In TOP named 'avatar_player'
        return self.Owner.op("avatar_player")

    def _send_text(self, obj):
        ws = self._ws()
        if ws is None or not self._connected():
            return
        try:
            ws.sendText(json.dumps(obj))
        except Exception as e:
            self._log(f"sendText failed: {e}")

    def _set_str(self, par_name, value):
        if hasattr(self.Owner.par, par_name):
            getattr(self.Owner.par, par_name).val = value

    def _status(self, s):
        self._set_str("Status", s)

    def _log(self, msg):
        print("[avatar]", msg)
        self._set_str("Status", str(msg)[:120])
