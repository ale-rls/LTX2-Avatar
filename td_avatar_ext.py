"""
td_avatar_ext.py  -- TouchDesigner Extension (paste into a Base COMP)
---------------------------------------------------------------------
Client side of the live interactive avatar. Runs inside TouchDesigner.

TRANSPORT (FluxRT lesson -- no WebSocket DAT):
  We use a Web Server DAT + Web Render TOP (relay HTML), not a WebSocket DAT.
  The relay page's JavaScript owns the actual WebSocket to the GPU server.
  Two channels share one local WebSocket between TD and the relay page:
    TD -> relay (binary)  = raw int16 PCM audio chunks from mic_out CHOP
    TD -> relay (text)    = JSON control messages (config, reset, stop)
    relay -> TD (text)    = JSON status / transcript / reply from server
    relay -> TD (binary)  = mp4 clip bytes from server

REQUIRED OPERATORS inside the Base COMP:
  AvatarExt_logic   Text DAT         this file
  webserver_cbs     Text DAT         paste ws_callbacks.py here
  web_server        Web Server DAT   Callbacks DAT = webserver_cbs; port set by ext
  web_render        Web Render TOP   URL set by ext on Connect; can be small/hidden
  mic_out           CHOP             mono 16 kHz float (Audio Device In -> Resample
                                     CHOP 16000 -> 1 channel -> named mic_out)
  avatar_player     Movie File In TOP  plays finished mp4 clips
  param_exec        Parameter Execute DAT  (optional) ext wires this automatically

WIRING:
  - web_server Callbacks DAT = webserver_cbs
  - CHOP Execute DAT on mic_out -> whileOn / onValueChange:
        parent().ext.AvatarExt.OnAudioCook()
  - Parameter Execute DAT named param_exec (Custom + Value Change + Pulse):
        def onPulse(par):
            ext = parent().ext.AvatarExt
            if par.name == 'Connect':   ext.Connect()
            elif par.name == 'Reset':   ext.Reset()
            elif par.name == 'Endturn': ext.EndTurn()
        def onValueChange(par, prev):
            pass  # Fastmode / Pushtotalk changes read live via params property

EXTENSION NAMING -- must agree in three places (FluxRT lesson):
  1) class AvatarExt
  2) COMP Extension 1 par = op('./AvatarExt_logic').module.AvatarExt(me)
  3) Promote Extension = On  ->  parent().ext.AvatarExt
"""

import json
import os
import socket
import tempfile

VERSION = "1.0.0-ltx2-relay"

PARAM_DEFAULTS = {
    'Address':    'ws://127.0.0.1:8080',
    'Character':  'default',
    'Voiceref':   'B.wav',
    'Charimage':  'ref-img.png',
    'Clipw':      960,
    'Cliph':      1280,
    'Cliplen':    6,
    'Fastmode':   False,
    'Pushtotalk': False,
}


# ---------------------------------------------------------------------------
# ParameterManager  (ported from FluxRT-TD-TCP pattern)
# ---------------------------------------------------------------------------
class ParameterManager:
    """Owns the custom parameter page on the Base COMP.

    setup()                 idempotent: create fresh or patch missing params
    _ensure_missing_params  adds any params absent from an existing page
    create_all              builds the page from scratch with section headers
    update_states           enables / disables params based on connection state
    setup_param_exec        wires the param_exec DAT to watch the right pars
    @property accessors     typed reads so callers never touch .par directly
    """

    def __init__(self, owner_comp):
        self.ownerComp = owner_comp

    # ---- typed accessors ------------------------------------------------------
    def _get(self, name, default=None):
        p = self.ownerComp.par
        if hasattr(p, name):
            return getattr(p, name).eval()
        return PARAM_DEFAULTS.get(name) if default is None else default

    def _get_bool(self, name, default=False):
        return bool(self._get(name, default))

    @property
    def Address(self):    return self._get('Address',    PARAM_DEFAULTS['Address'])
    @property
    def Character(self):  return self._get('Character',  PARAM_DEFAULTS['Character'])
    @property
    def Voiceref(self):   return self._get('Voiceref',   PARAM_DEFAULTS['Voiceref'])
    @property
    def Charimage(self):  return self._get('Charimage',  PARAM_DEFAULTS['Charimage'])
    @property
    def Clipw(self):      return int(self._get('Clipw',  PARAM_DEFAULTS['Clipw']))
    @property
    def Cliph(self):      return int(self._get('Cliph',  PARAM_DEFAULTS['Cliph']))
    @property
    def Cliplen(self):    return int(self._get('Cliplen', PARAM_DEFAULTS['Cliplen']))
    @property
    def Fastmode(self):   return self._get_bool('Fastmode',   PARAM_DEFAULTS['Fastmode'])
    @property
    def Pushtotalk(self): return self._get_bool('Pushtotalk', PARAM_DEFAULTS['Pushtotalk'])

    # ---- page helpers ---------------------------------------------------------
    def _get_page(self, name):
        for p in self.ownerComp.customPages:
            if p.name == name:
                return p
        return None

    def setup(self):
        """Idempotent: create page if absent, or patch any missing params."""
        page = self._get_page('Avatar')
        if not page:
            self.create_all()
        else:
            self._ensure_missing_params(page)

    def create_all(self):
        """Build the Avatar custom-parameter page from scratch."""
        page = self.ownerComp.appendCustomPage('Avatar')

        p = page.appendStr('Version', label='Version')[0]
        p.default = p.val = VERSION
        p.readOnly = True

        page.appendHeader('Connection')
        page.appendPulse('Connect', label='Connect / Disconnect')
        p = page.appendStr('Address', label='Server WS URL')[0]
        p.default = p.val = PARAM_DEFAULTS['Address']

        page.appendHeader('Character')
        p = page.appendStr('Character', label='Character ID')[0]
        p.default = p.val = PARAM_DEFAULTS['Character']
        p = page.appendStr('Voiceref', label='Voice Ref File')[0]
        p.default = p.val = PARAM_DEFAULTS['Voiceref']
        p = page.appendStr('Charimage', label='Char Image File')[0]
        p.default = p.val = PARAM_DEFAULTS['Charimage']

        page.appendHeader('Clip')
        p = page.appendInt('Clipw', label='Width')[0]
        p.default = p.val = PARAM_DEFAULTS['Clipw']
        p = page.appendInt('Cliph', label='Height')[0]
        p.default = p.val = PARAM_DEFAULTS['Cliph']
        p = page.appendInt('Cliplen', label='Length (sec)')[0]
        p.default = p.val = PARAM_DEFAULTS['Cliplen']

        page.appendHeader('Controls')
        p = page.appendToggle('Fastmode', label='Fast Mode')[0]
        p.default = p.val = PARAM_DEFAULTS['Fastmode']
        p = page.appendToggle('Pushtotalk', label='Push To Talk')[0]
        p.default = p.val = PARAM_DEFAULTS['Pushtotalk']
        page.appendPulse('Reset',   label='Reset Brain')
        page.appendPulse('Endturn', label='End Turn')

        page.appendHeader('Monitor')
        p = page.appendStr('Status', label='Status')[0]; p.readOnly = True
        p = page.appendStr('Heard',  label='Heard')[0];  p.readOnly = True
        p = page.appendStr('Reply',  label='Reply')[0];  p.readOnly = True

    def _ensure_missing_params(self, page):
        """Add any params absent from an existing Avatar page (safe to re-run)."""
        par = self.ownerComp.par
        specs = [
            # (name,        method,          default,                          read_only)
            ('Version',    'appendStr',    VERSION,                            True),
            ('Connect',    'appendPulse',  None,                               False),
            ('Address',    'appendStr',    PARAM_DEFAULTS['Address'],          False),
            ('Character',  'appendStr',    PARAM_DEFAULTS['Character'],        False),
            ('Voiceref',   'appendStr',    PARAM_DEFAULTS['Voiceref'],         False),
            ('Charimage',  'appendStr',    PARAM_DEFAULTS['Charimage'],        False),
            ('Clipw',      'appendInt',    PARAM_DEFAULTS['Clipw'],            False),
            ('Cliph',      'appendInt',    PARAM_DEFAULTS['Cliph'],            False),
            ('Cliplen',    'appendInt',    PARAM_DEFAULTS['Cliplen'],          False),
            ('Fastmode',   'appendToggle', PARAM_DEFAULTS['Fastmode'],         False),
            ('Pushtotalk', 'appendToggle', PARAM_DEFAULTS['Pushtotalk'],       False),
            ('Reset',      'appendPulse',  None,                               False),
            ('Endturn',    'appendPulse',  None,                               False),
            ('Status',     'appendStr',    '',                                 True),
            ('Heard',      'appendStr',    '',                                 True),
            ('Reply',      'appendStr',    '',                                 True),
        ]
        for name, method, default, read_only in specs:
            if not hasattr(par, name):
                p = getattr(page, method)(name, label=name)[0]
                if default is not None:
                    p.default = p.val = default
                p.readOnly = read_only

    def update_states(self, connected):
        """Enable/disable params based on connection state."""
        par = self.ownerComp.par
        # Editable only while disconnected
        for name in ['Address', 'Character', 'Voiceref', 'Charimage',
                     'Clipw', 'Cliph', 'Cliplen', 'Fastmode', 'Pushtotalk']:
            if hasattr(par, name):
                getattr(par, name).enable = not connected
        # Active only while connected
        for name in ['Reset', 'Endturn']:
            if hasattr(par, name):
                getattr(par, name).enable = connected

    def setup_param_exec(self):
        """Wire the param_exec DAT to watch all Avatar custom pars."""
        param_exec = self.ownerComp.op('param_exec')
        if param_exec and hasattr(param_exec.par, 'pars'):
            param_exec.par.pars = 'Connect Reset Endturn Fastmode Pushtotalk Address Character Voiceref Charimage Clipw Cliph Cliplen'


# ---------------------------------------------------------------------------
# AvatarExt
# ---------------------------------------------------------------------------
class AvatarExt:
    def __init__(self, ownerComp):
        self.Owner = ownerComp
        self.params = ParameterManager(ownerComp)
        self._ws_clients = set()        # relay page local-WS clients
        self._buf = bytearray()         # incoming mp4 assembly buffer
        self._expecting = 0             # bytes expected for current clip
        self._last_reply = ""
        self._clip_dir = tempfile.mkdtemp(prefix="ltx_avatar_")
        self._clip_index = 0
        self._relay_cache = None        # cached rendered relay HTML bytes
        self._local_port = self._alloc_port()
        self.params.setup()
        self.params.update_states(False)
        self.params.setup_param_exec()
        self._status("idle")
        self._start_web_server()
        print(f"[avatar] AvatarExt v{VERSION} ready -- web_server on :{self._local_port}")

    # ---- port -----------------------------------------------------------------
    def _alloc_port(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        return port

    # ---- web server -----------------------------------------------------------
    def _start_web_server(self):
        dat = self.Owner.op('web_server')
        if dat is None:
            self._log("ERROR: no 'web_server' DAT found in this COMP")
            return
        dat.par.active = 0
        dat.par.port = self._local_port
        dat.par.active = 1

    # ---- connect / disconnect -------------------------------------------------
    def Connect(self):
        """Called by the Connect pulse parameter."""
        self._relay_cache = None        # rebuild HTML with current Address
        web_render = self.Owner.op('web_render')
        if web_render is None:
            self._log("ERROR: no 'web_render' TOP found")
            return
        web_render.par.url = 'about:blank'
        web_render.par.url = f'http://127.0.0.1:{self._local_port}/relay'
        self._status("connecting")

    def Reset(self):
        """Clear the avatar's conversation history on the server."""
        self._broadcast_text({"type": "control", "cmd": "reset"})

    def EndTurn(self):
        """Operator-forced end of turn (push-to-talk release)."""
        self._broadcast_text({"type": "control", "cmd": "stop"})

    def OnParameterChange(self, par):
        """Called by param_exec for all value changes (mirrors Daydream pattern)."""
        if par.name == 'Fastmode':
            # Push updated config if already connected so fast mode takes effect next turn
            if self._ws_clients:
                self._broadcast_text({
                    "type": "config",
                    "fast": self.params.Fastmode,
                })
        elif par.name == 'Address':
            # Invalidate relay cache so next Connect picks up the new URL
            self._relay_cache = None

    # ---- audio out ------------------------------------------------------------
    def OnAudioCook(self):
        """Called by a CHOP Execute DAT on mic_out each cook.
        mic_out must be mono, 16 kHz, float -1..1.
        Converts to int16 LE PCM and broadcasts to all relay clients."""
        if not self._ws_clients:
            return
        if self.params.Pushtotalk:
            pass  # only stream while toggle held; release triggers EndTurn()
        chop = self.Owner.op('mic_out')
        if chop is None or chop.numChans == 0:
            return
        ch = chop[0]
        n = len(ch)
        if n == 0:
            return
        pcm = bytearray(n * 2)
        for i in range(n):
            v = max(-1.0, min(1.0, ch[i]))
            s = int(v * 32767.0)
            pcm[2 * i]     = s & 0xFF
            pcm[2 * i + 1] = (s >> 8) & 0xFF
        self._broadcast_bytes(bytes(pcm))

    # ---- inbound: web_server callbacks ----------------------------------------
    def OnHTTPRequest(self, request, response):
        """Serve the relay HTML page at /relay."""
        uri = request.get('uri', '').split('?')[0]
        if uri == '/relay':
            response['statusCode'] = 200
            response['statusReason'] = 'OK'
            response['content-type'] = 'text/html; charset=utf-8'
            response['data'] = self._relay_html()
        else:
            response['statusCode'] = 404
            response['data'] = b'Not Found'
        return response

    def OnWebSocketOpen(self, client, uri):
        self._ws_clients.add(client)
        self._send_config(client)
        self.params.update_states(True)
        self._status("connected")

    def OnWebSocketClose(self, client):
        self._ws_clients.discard(client)
        if not self._ws_clients:
            self.params.update_states(False)
            self._status("idle")

    def OnWebSocketReceiveText(self, client, message):
        """JSON messages relayed from the server."""
        try:
            m = json.loads(message)
        except Exception:
            return
        t = m.get("type")
        if t == "status":
            stage = m.get("stage", "")
            frac = m.get("frac", None)
            self._status(stage if frac is None else f"{stage} {int(frac * 100)}%")
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

    def OnWebSocketReceiveBinary(self, client, data):
        """Binary mp4 bytes relayed from the server."""
        self._buf.extend(data)
        if self._expecting and len(self._buf) >= self._expecting:
            self._finish_clip(bytes(self._buf[:self._expecting]))
            self._buf = bytearray()
            self._expecting = 0

    # ---- clip playback --------------------------------------------------------
    def _finish_clip(self, mp4):
        self._clip_index = (self._clip_index + 1) % 4
        path = os.path.join(self._clip_dir, f"clip_{self._clip_index}.mp4")
        with open(path, "wb") as f:
            f.write(mp4)
        player = self.Owner.op("avatar_player")
        if player is not None:
            player.par.file = path
            try:
                player.par.reloadpulse.pulse()
            except Exception:
                pass
            try:
                player.par.cuepulse.pulse()
                player.par.play = True
            except Exception:
                pass
        self._status("playing")

    # ---- helpers --------------------------------------------------------------
    def _send_config(self, client):
        """Send the current parameter values as a config message to the server."""
        cfg = {
            "type":            "config",
            "character":       self.params.Character,
            "voice_ref":       self.params.Voiceref,
            "character_image": self.params.Charimage,
            "width":           self.params.Clipw,
            "height":          self.params.Cliph,
            "length":          self.params.Cliplen,
            "fast":            self.params.Fastmode,
        }
        dat = self.Owner.op('web_server')
        if dat:
            dat.webSocketSendText(client, json.dumps(cfg))

    def _broadcast_text(self, obj):
        dat = self.Owner.op('web_server')
        if dat is None or not self._ws_clients:
            return
        text = json.dumps(obj)
        for c in list(self._ws_clients):
            try:
                dat.webSocketSendText(c, text)
            except Exception:
                self._ws_clients.discard(c)

    def _broadcast_bytes(self, data):
        dat = self.Owner.op('web_server')
        if dat is None or not self._ws_clients:
            return
        for c in list(self._ws_clients):
            try:
                dat.webSocketSendBinary(c, data)
            except Exception:
                self._ws_clients.discard(c)

    def _set_str(self, par_name, value):
        if hasattr(self.Owner.par, par_name):
            getattr(self.Owner.par, par_name).val = value

    def _status(self, s):
        self._set_str("Status", s)

    def _log(self, msg):
        print("[avatar]", msg)

    def _relay_html(self):
        if self._relay_cache is None:
            html = RELAY_HTML_TEMPLATE
            html = html.replace('{{LOCAL_PORT}}',    str(self._local_port))
            html = html.replace('{{REMOTE_WS_URL}}', self.params.Address)
            self._relay_cache = html.encode('utf-8')
        return self._relay_cache

    def InvalidateRelayCache(self):
        """Force the relay page to rebuild (picks up a new Address on next Connect)."""
        self._relay_cache = None

    def Destroy(self):
        pass


# ---------------------------------------------------------------------------
# Relay HTML -- served at http://127.0.0.1:{LOCAL_PORT}/relay
# Loaded by the web_render TOP. Bridges local WS (TD) <-> remote WS (server).
#
# Routing:
#   LOCAL  binary -> REMOTE binary  (PCM audio: TD mic -> server)
#   LOCAL  text   -> REMOTE text    (JSON config / control: TD -> server)
#   REMOTE text   -> LOCAL  text    (JSON status/transcript/reply: server -> TD)
#   REMOTE binary -> LOCAL  binary  (mp4 clip: server -> TD)
# ---------------------------------------------------------------------------
RELAY_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>LTX-2 Avatar Relay</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#111; color:#aaa; font:12px/1.5 monospace; padding:8px; }
#s { color:#0f0; }
</style>
</head>
<body>
<div id="s">Initializing...</div>
<script>
/*
 * LTX-2 Avatar relay page (loaded by TouchDesigner's web_render TOP).
 * Same Web Server DAT + Web Render TOP pattern as FluxRT-TD-TCP.
 *
 * Template variables filled by td_avatar_ext.py before serving:
 *   {{LOCAL_PORT}}    -- TD's web_server DAT port
 *   {{REMOTE_WS_URL}} -- ws:// URL of the GPU avatar server
 *
 * Message routing (all types, both directions):
 *   LOCAL  binary -> REMOTE binary  (PCM audio from TD mic)
 *   LOCAL  text   -> REMOTE text    (JSON config + control from TD)
 *   REMOTE text   -> LOCAL  text    (JSON status/transcript/reply for TD)
 *   REMOTE binary -> LOCAL  binary  (mp4 clip bytes for TD)
 */

const LOCAL_URL  = "ws://127.0.0.1:{{LOCAL_PORT}}/ws";
const REMOTE_URL = "{{REMOTE_WS_URL}}";

const statusEl = document.getElementById('s');
function log(msg) { console.log('[relay]', msg); statusEl.textContent = msg; }

let localWs  = null;
let remoteWs = null;

// ---- REMOTE (GPU avatar server) ----------------------------------------
function connectRemote() {
  log('Connecting to server: ' + REMOTE_URL);
  remoteWs = new WebSocket(REMOTE_URL);
  remoteWs.binaryType = 'arraybuffer';

  remoteWs.onopen = () => log('Server connected.');

  remoteWs.onmessage = (ev) => {
    if (!localWs || localWs.readyState !== WebSocket.OPEN) return;
    localWs.send(ev.data);  // text or ArrayBuffer, forwarded as-is
    if (typeof ev.data === 'string') {
      try {
        const m = JSON.parse(ev.data);
        const frac = m.frac != null ? ' ' + Math.round(m.frac * 100) + '%' : '';
        if (m.stage) log('stage: ' + m.stage + frac);
      } catch(e) {}
    }
  };

  remoteWs.onclose = () => {
    log('Server disconnected -- reconnecting in 1.5s...');
    setTimeout(connectRemote, 1500);
  };
  remoteWs.onerror = () => log('Server WS error.');
}

// ---- LOCAL (TD web_server DAT) -----------------------------------------
function connectLocal() {
  log('Connecting to TouchDesigner...');
  localWs = new WebSocket(LOCAL_URL);
  localWs.binaryType = 'arraybuffer';

  localWs.onopen = () => {
    log('TouchDesigner connected.');
    connectRemote();
  };

  localWs.onmessage = (ev) => {
    if (!remoteWs || remoteWs.readyState !== WebSocket.OPEN) return;
    remoteWs.send(ev.data);  // PCM binary or JSON text, forwarded as-is
  };

  localWs.onclose = () => {
    log('TouchDesigner disconnected -- reconnecting in 1s...');
    if (remoteWs) { remoteWs.close(); remoteWs = null; }
    setTimeout(connectLocal, 1000);
  };
}

connectLocal();
</script>
</body>
</html>
"""
