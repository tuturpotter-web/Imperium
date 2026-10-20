"""
Imperium Backend
- Console cachée (SW_HIDE + FreeConsole)
- Fenêtre native via pywebview (Edge WebView2)
- Profils indépendants avec création/suppression/renommage
- WebSocket :8765 — HTTP :8766
"""
APP_VERSION = "dev"
import sys, os, ctypes, threading, time, asyncio, json, subprocess, re
import webbrowser, shutil, glob, logging, datetime, tempfile, base64, uuid, struct, hashlib, math
from pathlib import Path

# ── CACHER CONSOLE ──────────────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd: ctypes.windll.user32.ShowWindow(hwnd, 0)
        ctypes.windll.kernel32.FreeConsole()
    except: pass

CREATE_NO_WINDOW = 0x08000000
_SI = None
if sys.platform == "win32":
    _SI = subprocess.STARTUPINFO()
    _SI.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _SI.wShowWindow = 0

def run_hidden(cmd, **kwargs):
    kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    kwargs.setdefault("startupinfo", _SI)
    return subprocess.Popen(cmd, **kwargs)

def run_silent(cmd: str):
    return run_hidden(cmd, shell=True)

def open_url_default(url: str):
    """Ouvre une URL dans le vrai navigateur par défaut Windows (registre UserChoice)."""
    if not url: return
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice")
            prog_id, _ = winreg.QueryValueEx(key, "ProgId"); winreg.CloseKey(key)
            cmd_key = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, fr"{prog_id}\shell\open\command")
            cmd, _ = winreg.QueryValueEx(cmd_key, ""); winreg.CloseKey(cmd_key)
            run_hidden(cmd.replace("%1", url) if "%1" in cmd else f'{cmd} "{url}"', shell=True)
            return
        except: pass
        try: os.startfile(url); return
        except: pass
    try: webbrowser.open(url)
    except: pass

import psutil, keyboard, mouse, websockets
try:
    from websockets.server import WebSocketServerProtocol
except ImportError:
    try:
        from websockets.legacy.server import WebSocketServerProtocol
    except ImportError:
        # websockets v14+ — pas de WebSocketServerProtocol à importer séparément
        WebSocketServerProtocol = object

try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL
    PYCAW_OK = True
except: PYCAW_OK = False

try:
    import win32gui, win32process
    WIN32_OK = True
except: WIN32_OK = False

GPU_OK = shutil.which("nvidia-smi") is not None

try:
    import wmi as wmilib; WMI_OK = True
except: WMI_OK = False

try:
    import serial, serial.tools.list_ports; SERIAL_OK = True
except: SERIAL_OK = False

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("MD")

WS_PORT   = 8765
HTTP_PORT = 8766
CONFIG_PATH = Path(os.path.expanduser("~")) / ".macrodeck" / "config.json"
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── VOLUME ───────────────────────────────────────────────────────────────────
# ── COM / AUDIO ─────────────────────────────────────────────────────────────
# pycaw/comtypes exigent COM initialisé dans CHAQUE thread, et les pointeurs COM sont liés à leur
# thread : on garde donc un cache par thread (au lieu de tout recréer à chaque appel, très lent).
_tls = threading.local()

def com_init():
    if getattr(_tls, "com", False): return
    try:
        import comtypes; comtypes.CoInitialize()
    except Exception: pass
    _tls.com = True

_MMDE_CLSID = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"

def _endpoint(capture=False):
    key = "mic" if capture else "spk"
    v = getattr(_tls, key, None)
    if v is None:
        com_init()
        if capture:
            from comtypes import CoCreateInstance, GUID
            from pycaw.pycaw import IMMDeviceEnumerator
            dev = CoCreateInstance(GUID(_MMDE_CLSID), IMMDeviceEnumerator, CLSCTX_ALL).GetDefaultAudioEndpoint(1, 0)  # eCapture, eConsole
        else:
            dev = AudioUtilities.GetSpeakers()
        v = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)
        setattr(_tls, key, v)
    return v

def _vif(): return _endpoint(False) if PYCAW_OK else None

def _with_endpoint(capture, fn):
    """Exécute fn(endpoint). Un seul nouvel essai avec un endpoint neuf si le périphérique audio a changé."""
    if not PYCAW_OK: return None
    for attempt in (0, 1):
        try: return fn(_endpoint(capture))
        except Exception as e:
            setattr(_tls, "mic" if capture else "spk", None)
            if attempt: log.error(f"audio: {e}")
    return None

def _clamp100(v): return max(0, min(100, int(round(v))))

def get_volume():
    r = _with_endpoint(False, lambda v: int(round(v.GetMasterVolumeLevelScalar() * 100))); return 0 if r is None else r
def set_volume(lv): _with_endpoint(False, lambda v: v.SetMasterVolumeLevelScalar(_clamp100(lv) / 100.0, None))
def get_mute():
    r = _with_endpoint(False, lambda v: bool(v.GetMute())); return bool(r)
def set_mute(s): _with_endpoint(False, lambda v: v.SetMute(bool(s), None))

def get_mic_volume():
    r = _with_endpoint(True, lambda v: int(round(v.GetMasterVolumeLevelScalar() * 100))); return 0 if r is None else r
def set_mic_volume(lv): _with_endpoint(True, lambda v: v.SetMasterVolumeLevelScalar(_clamp100(lv) / 100.0, None))
def get_mic_mute():
    r = _with_endpoint(True, lambda v: bool(v.GetMute())); return bool(r)
def set_mic_mute(s): _with_endpoint(True, lambda v: v.SetMute(bool(s), None))

# Sessions audio par application (cache de 2 s par thread)
def _sessions(force=False):
    now = time.time(); cache = getattr(_tls, "sess", None)
    if force or cache is None or now - getattr(_tls, "sess_t", 0) > 2.0:
        com_init(); out = {}
        from pycaw.pycaw import ISimpleAudioVolume
        for s in AudioUtilities.GetAllSessions():
            try:
                if not s.Process: continue
                n = s.Process.name().lower(); n = n[:-4] if n.endswith(".exe") else n
                out.setdefault(n, []).append(s._ctl.QueryInterface(ISimpleAudioVolume))
            except Exception: pass
        _tls.sess = out; _tls.sess_t = now
    return _tls.sess

def _norm_app(n):
    n = (n or "").strip().lower(); return n[:-4] if n.endswith(".exe") else n

def _app_vols(name):
    """Sessions audio d'une app. Accepte plusieurs noms séparés par | ou , (ex : « ms-teams|teams »)."""
    if not PYCAW_OK: return []
    wanted = [_norm_app(x) for x in re.split(r"[|,]", name or "") if x.strip()]
    if not wanted: return []
    for attempt in (0, 1):
        try: sess = _sessions(force=bool(attempt))
        except Exception as e: log.error(f"sessions audio: {e}"); return []
        vols = [v for w in wanted for v in sess.get(w, [])]
        if vols or attempt: return vols
    return []

def set_app_volume(name, level):
    for v in _app_vols(name):
        try: v.SetMasterVolume(_clamp100(level) / 100.0, None)
        except Exception: _tls.sess = None

def get_app_volume(name):
    vols = _app_vols(name)
    try: return int(round(vols[0].GetMasterVolume() * 100)) if vols else None
    except Exception: _tls.sess = None; return None

def set_app_mute(name, state=None):
    vols = _app_vols(name)
    if not vols: return
    try:
        new = (not bool(vols[0].GetMute())) if state is None else bool(state)
        for v in vols: v.SetMute(new, None)
    except Exception: _tls.sess = None

def set_all_apps_volume(level):
    try:
        for vols in _sessions().values():
            for v in vols:
                try: v.SetMasterVolume(_clamp100(level) / 100.0, None)
                except Exception: pass
    except Exception: _tls.sess = None

def foreground_process():
    """Nom (sans .exe, en minuscules) du processus de la fenêtre au premier plan."""
    if not WIN32_OK: return ""
    try:
        _, pid = win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())
        return _norm_app(psutil.Process(pid).name())
    except Exception: return ""

def list_audio_sessions():
    out, seen = [], set()
    if not PYCAW_OK: return out
    try:
        com_init()
        for s in AudioUtilities.GetAllSessions():
            if s.Process:
                n = s.Process.name()
                if n.lower() not in seen: seen.add(n.lower()); out.append(n)
    except Exception: pass
    return sorted(out)

# ── LUMINOSITÉ (DDC/CI pour les écrans externes, WMI pour les portables) ─────
_ps_proc = None
def ps_fire(cmd):
    """Envoie une commande à une session PowerShell persistante (évite ~500 ms de démarrage à chaque fois)."""
    global _ps_proc
    for _ in (0, 1):
        try:
            if _ps_proc is None or _ps_proc.poll() is not None:
                _ps_proc = subprocess.Popen(["powershell", "-NoProfile", "-NoLogo", "-NonInteractive", "-Command", "-"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, startupinfo=_SI)
            _ps_proc.stdin.write((cmd + "\n").encode("utf-8")); _ps_proc.stdin.flush(); return True
        except Exception: _ps_proc = None
    return False

_ddc = {"t": 0, "mons": []}
def _ddc_monitors():
    if sys.platform != "win32": return []
    if time.time() - _ddc["t"] < 30 and _ddc["mons"]: return _ddc["mons"]
    from ctypes import wintypes
    class PM(ctypes.Structure): _fields_ = [("h", wintypes.HANDLE), ("d", ctypes.c_wchar * 128)]
    dx, u32 = ctypes.windll.dxva2, ctypes.windll.user32
    hmons = []
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.LPARAM)
    u32.EnumDisplayMonitors(None, None, CB(lambda hm, hdc, rc, lp: hmons.append(hm) or True), 0)
    mons = []
    for hm in hmons:
        n = wintypes.DWORD()
        if not dx.GetNumberOfPhysicalMonitorsFromHMONITOR(ctypes.c_void_p(hm), ctypes.byref(n)) or not n.value: continue
        arr = (PM * n.value)()
        if not dx.GetPhysicalMonitorsFromHMONITOR(ctypes.c_void_p(hm), n.value, arr): continue
        for pm in arr:
            mn, cur, mx = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
            if dx.GetMonitorBrightness(pm.h, ctypes.byref(mn), ctypes.byref(cur), ctypes.byref(mx)) and mx.value > mn.value:
                mons.append((pm.h, mn.value, mx.value, cur.value))
    _ddc["t"] = time.time(); _ddc["mons"] = mons
    return mons

_bright = {"v": None}
def set_brightness(level):
    level = _clamp100(level); _bright["v"] = level
    try:
        ok = False
        for h, mn, mx, _ in _ddc_monitors():
            if ctypes.windll.dxva2.SetMonitorBrightness(h, int(mn + (mx - mn) * level / 100.0)): ok = True
        if ok: return
    except Exception as e: log.warning(f"DDC/CI: {e}"); _ddc["mons"] = []
    ps_fire(f"(Get-WmiObject -NS root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{level})")

def get_brightness():
    if _bright["v"] is not None: return _bright["v"]
    try:
        mons = _ddc_monitors()
        if mons: _bright["v"] = _clamp100((mons[0][3] - mons[0][1]) * 100.0 / (mons[0][2] - mons[0][1])); return _bright["v"]
    except Exception: pass
    return None

# ── OBS (obs-websocket v5 : OBS ≥ 28, port 4455 — repli sur v4 : port 4444) ─────
_V4 = {"StartStream": "StartStreaming", "StopStream": "StopStreaming", "ToggleStream": "StartStopStreaming",
       "StartRecord": "StartRecording", "StopRecord": "StopRecording", "ToggleRecord": "StartStopRecording",
       "PauseRecord": "PauseRecording", "ResumeRecord": "ResumeRecording", "SaveReplayBuffer": "SaveReplayBuffer",
       "ToggleReplayBuffer": "StartStopReplayBuffer", "TriggerStudioModeTransition": "TransitionToProgram",
       "ToggleVirtualCam": "StartStopVirtualCam", "TriggerHotkeyByName": "TriggerHotkeyByName"}

class OBSClient:
    def __init__(self, get_cfg):
        self._cfg = get_cfg; self._ws = None; self.ver = 0; self._lock = threading.RLock(); self._next = 0; self.error = ""

    def close(self):
        with self._lock:
            try: self._ws and self._ws.close()
            except Exception: pass
            self._ws = None; self.ver = 0

    def _connect(self):
        import websocket
        c = self._cfg(); host = c.get("obs_host") or "localhost"; pwd = c.get("obs_password") or ""
        try: port = int(c.get("obs_port") or 4455)
        except Exception: port = 4455
        last = None
        for p, v in ((port, 5), (4444, 4)) if port != 4444 else ((4444, 4),):
            try:
                ws = websocket.create_connection(f"ws://{host}:{p}", timeout=3, subprotocols=["obswebsocket.json"] if v == 5 else None)
                if v == 5:
                    hello = json.loads(ws.recv()); d = {"rpcVersion": 1}
                    a = (hello.get("d") or {}).get("authentication")
                    if a:
                        sec = base64.b64encode(hashlib.sha256((pwd + a["salt"]).encode()).digest()).decode()
                        d["authentication"] = base64.b64encode(hashlib.sha256((sec + a["challenge"]).encode()).digest()).decode()
                    ws.send(json.dumps({"op": 1, "d": d})); r = json.loads(ws.recv())
                    if r.get("op") != 2: raise RuntimeError("authentification OBS refusée (mot de passe ?)")
                else:
                    ws.send(json.dumps({"request-type": "GetAuthRequired", "message-id": "a"})); r = json.loads(ws.recv())
                    if r.get("authRequired"):
                        sec = base64.b64encode(hashlib.sha256((pwd + r["salt"]).encode()).digest()).decode()
                        au = base64.b64encode(hashlib.sha256((sec + r["challenge"]).encode()).digest()).decode()
                        ws.send(json.dumps({"request-type": "Authenticate", "message-id": "b", "auth": au})); r = json.loads(ws.recv())
                        if r.get("status") != "ok": raise RuntimeError("authentification OBS refusée (mot de passe ?)")
                self._ws = ws; self.ver = v; self.error = ""; return
            except Exception as e: last = e
        raise ConnectionError(str(last) or "OBS injoignable")

    def request(self, req, data=None):
        """Envoie une requête (nom v5). Retourne les données de réponse, ou None en cas d'échec."""
        data = data or {}
        with self._lock:
            for attempt in (0, 1):
                try:
                    if self._ws is None:
                        if time.time() < self._next: return None
                        self._connect()
                    return self._send(req, data)
                except _Soft as e:
                    self.error = str(e); return None
                except Exception as e:
                    self.error = str(e); self.close()
                    if attempt: self._next = time.time() + 2; log.warning(f"OBS: {e}")
        return None

    def _send(self, req, data):
        rid = str(uuid.uuid4())[:8]
        if self.ver == 5:
            self._ws.send(json.dumps({"op": 6, "d": {"requestType": req, "requestId": rid, "requestData": data}}))
            while True:
                m = json.loads(self._ws.recv())
                if m.get("op") == 7 and m["d"].get("requestId") == rid:
                    st = m["d"].get("requestStatus", {})
                    if not st.get("result"):
                        if st.get("code") == 207: raise RuntimeError(f"{req}: OBS n'est pas prêt")
                        raise _Soft(f"{req}: {st.get('comment', 'échec')}")
                    return m["d"].get("responseData") or {}
        name, extra = self._to_v4(req, data)
        if name is None: return None
        self._ws.send(json.dumps({"request-type": name, "message-id": rid, **extra}))
        while True:
            m = json.loads(self._ws.recv())
            if m.get("message-id") == rid:
                if m.get("status") != "ok": raise _Soft(m.get("error", ""))
                return self._from_v4(req, m)

    @staticmethod
    def _to_v4(req, d):
        if req in _V4: return _V4[req], ({"hotkeyName": d["hotkeyName"]} if req == "TriggerHotkeyByName" else {})
        if req == "SetCurrentProgramScene": return "SetCurrentScene", {"scene-name": d["sceneName"]}
        if req == "ToggleInputMute": return "ToggleMute", {"source": d["inputName"]}
        if req == "SetInputMute": return "SetMute", {"source": d["inputName"], "mute": d["inputMuted"]}
        if req == "SetInputVolume": return "SetVolume", {"source": d["inputName"], "volume": d["inputVolumeMul"], "useDecibel": False}
        if req == "GetInputVolume": return "GetVolume", {"source": d["inputName"]}
        if req == "GetSceneList": return "GetSceneList", {}
        if req == "GetStudioModeEnabled": return "GetStudioModeStatus", {}
        if req == "SetStudioModeEnabled": return ("EnableStudioMode" if d["studioModeEnabled"] else "DisableStudioMode"), {}
        if req == "SaveSourceScreenshot": return "TakeSourceScreenshot", {"sourceName": d["sourceName"], "saveToFilePath": d["imageFilePath"]}
        if req == "GetSourceVisible": return "GetSceneItemProperties", {"scene-name": d["sceneName"], "item": d["sourceName"]}
        if req == "SetSourceVisible": return "SetSceneItemProperties", {"scene-name": d["sceneName"], "item": d["sourceName"], "visible": d["visible"]}
        return None, {}

    @staticmethod
    def _from_v4(req, m):
        if req == "GetInputVolume": return {"inputVolumeMul": m.get("volume", 0)}
        if req == "GetSceneList": return {"currentProgramSceneName": m.get("current-scene"), "scenes": [{"sceneName": s["name"]} for s in m.get("scenes", [])][::-1]}
        if req == "GetStudioModeEnabled": return {"studioModeEnabled": m.get("studio-mode")}
        if req == "GetSourceVisible": return {"visible": m.get("visible")}
        return {}

    # ── Raccourcis de haut niveau ────────────────────────────────────────────
    def ping(self):
        r = self.request("GetSceneList") is not None
        return {"ok": r, "version": self.ver, "error": "" if r else (self.error or "OBS injoignable")}

    def scenes(self):
        r = self.request("GetSceneList") or {}
        return [s["sceneName"] for s in r.get("scenes", [])], r.get("currentProgramSceneName")

    def cycle_scene(self, n):
        names, cur = self.scenes()
        if not names: return
        i = names.index(cur) if cur in names else 0
        self.request("SetCurrentProgramScene", {"sceneName": names[(i + n) % len(names)]})

    def toggle_studio(self):
        r = self.request("GetStudioModeEnabled") or {}
        self.request("SetStudioModeEnabled", {"studioModeEnabled": not r.get("studioModeEnabled", False)})

    def toggle_source(self, scene, source):
        if self.request("GetSceneList") is None: return        # assure la connexion
        if self.ver == 4:
            cur = (self.request("GetSourceVisible", {"sceneName": scene, "sourceName": source}) or {}).get("visible", True)
            self.request("SetSourceVisible", {"sceneName": scene, "sourceName": source, "visible": not cur}); return
        r = self.request("GetSceneItemId", {"sceneName": scene, "sourceName": source})
        if not r: return
        sid = r.get("sceneItemId")
        cur = (self.request("GetSceneItemEnabled", {"sceneName": scene, "sceneItemId": sid}) or {}).get("sceneItemEnabled", True)
        self.request("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": sid, "sceneItemEnabled": not cur})

    @staticmethod
    def pct_to_mul(p):
        """0-100 -> multiplicateur OBS, courbe en dB (-60 dB .. 0 dB) comme le fader d'OBS."""
        p = max(0.0, min(100.0, float(p)))
        return 0.0 if p <= 0 else 10 ** ((-60 + p * 0.6) / 20.0)

    @staticmethod
    def mul_to_pct(m):
        m = float(m or 0)
        return 0.0 if m <= 0 else max(0.0, min(100.0, (20 * math.log10(m) + 60) / 0.6))

    def set_volume_pct(self, src, p): self.request("SetInputVolume", {"inputName": src, "inputVolumeMul": self.pct_to_mul(p)})
    def get_volume_pct(self, src):
        r = self.request("GetInputVolume", {"inputName": src}); return None if not r else self.mul_to_pct(r.get("inputVolumeMul"))
    def step_volume(self, src, d):
        cur = self.get_volume_pct(src)
        if cur is not None: self.set_volume_pct(src, cur + d)

class _Soft(Exception):
    """Erreur OBS « normale » (ex. source introuvable) : ne ferme pas la connexion."""

# ── DISCORD (API locale IPC : volume par utilisateur, mute, sourd, salons) ───────
# Nécessite une application sur discord.com/developers (client_id + secret, redirection http://localhost,
# votre compte ajouté dans « App Testers »). L'autorisation se valide une seule fois dans Discord.
class _PipeIO:
    def __init__(self, f): self.f = f
    def write(self, b): self.f.write(b)
    def read_exact(self, n):
        buf = b""
        while len(buf) < n:
            c = self.f.read(n - len(buf))
            if not c: raise EOFError("Discord a fermé la connexion")
            buf += c
        return buf
    def close(self):
        try: self.f.close()
        except Exception: pass

class _SockIO(_PipeIO):
    def write(self, b): self.f.sendall(b)
    def read_exact(self, n):
        buf = b""
        while len(buf) < n:
            c = self.f.recv(n - len(buf))
            if not c: raise EOFError("Discord a fermé la connexion")
            buf += c
        return buf

class DiscordRPC:
    SCOPES = ["rpc", "rpc.voice.read", "rpc.voice.write"]
    def __init__(self, get_cfg, notify=None):
        self._cfg = get_cfg; self._notify = notify or (lambda m: None); self._io = None; self.ready = False
        self.user = None; self.error = ""; self._lock = threading.RLock(); self._sel = None; self._try = 0
        self._ucache = (0, [])

    # transport ---------------------------------------------------------------
    def _open(self):
        for n in range(10):
            try:
                if os.name == "nt": return _PipeIO(open(r"\\?\pipe\discord-ipc-%d" % n, "r+b", buffering=0))
                import socket
                s = socket.socket(socket.AF_UNIX); s.settimeout(5)
                s.connect(os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "discord-ipc-%d" % n)); return _SockIO(s)
            except Exception: continue
        raise ConnectionError("Discord n'est pas ouvert")

    def _send(self, op, payload):
        d = json.dumps(payload).encode(); self._io.write(struct.pack("<II", op, len(d)) + d)

    def _recv(self):
        op, ln = struct.unpack("<II", self._io.read_exact(8)); return op, json.loads(self._io.read_exact(ln) or b"{}")

    def _cmd(self, cmd, args=None):
        nonce = str(uuid.uuid4()); self._send(1, {"cmd": cmd, "args": args or {}, "nonce": nonce})
        while True:
            op, m = self._recv()
            if op == 3: self._send(4, m); continue
            if op == 2: raise EOFError(m.get("message", "Discord a fermé la connexion"))
            if m.get("nonce") == nonce:
                if m.get("evt") == "ERROR": raise RuntimeError((m.get("data") or {}).get("message", "erreur Discord"))
                return m.get("data") or {}

    # authentification ----------------------------------------------------------
    def _token_path(self): return os.path.join(_app_dir_persistent(), "discord_token.json")

    def _oauth(self, form):
        import urllib.request, urllib.parse
        c = self._cfg(); form = {"client_id": str(c.get("discord_client_id", "")).strip(), "client_secret": str(c.get("discord_client_secret", "")).strip(), **form}
        req = urllib.request.Request("https://discord.com/api/v10/oauth2/token", data=urllib.parse.urlencode(form).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Imperium"})
        with urllib.request.urlopen(req, timeout=10) as r: tok = json.loads(r.read().decode())
        tok["expires_at"] = time.time() + int(tok.get("expires_in", 0)); return tok

    def _auth(self, access):
        d = self._cmd("AUTHENTICATE", {"access_token": access}); self.user = d.get("user") or {}

    def connect(self, interactive=False):
        with self._lock:
            c = self._cfg(); cid = str(c.get("discord_client_id", "")).strip()
            if not cid: raise RuntimeError("client_id Discord manquant (Paramètres → Intégrations)")
            self.close(); self._io = self._open()
            self._send(0, {"v": 1, "client_id": cid}); self._recv()
            tok = None
            try:
                with open(self._token_path(), encoding="utf-8") as f: tok = json.load(f)
            except Exception: pass
            ok = False
            if tok:
                try:
                    if tok.get("expires_at", 0) < time.time() + 60 and tok.get("refresh_token"):
                        tok = self._oauth({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
                        with open(self._token_path(), "w", encoding="utf-8") as f: json.dump(tok, f)
                    self._auth(tok["access_token"]); ok = True
                except Exception as e: log.info(f"Discord : jeton refusé ({e})")
            if not ok:
                if not interactive: raise RuntimeError("autorisation Discord requise (Paramètres → Intégrations)")
                code = self._cmd("AUTHORIZE", {"client_id": cid, "scopes": self.SCOPES})["code"]
                tok = self._oauth({"grant_type": "authorization_code", "code": code, "redirect_uri": "http://localhost"})
                with open(self._token_path(), "w", encoding="utf-8") as f: json.dump(tok, f)
                self._auth(tok["access_token"])
            self.ready = True; self.error = ""

    def close(self):
        self.ready = False
        if self._io: self._io.close()
        self._io = None

    def call(self, cmd, args=None):
        with self._lock:
            for attempt in (0, 1):
                try:
                    if not self.ready:
                        if time.time() - self._try < 3 and attempt == 0 and self.error: raise RuntimeError(self.error)
                        self._try = time.time(); self.connect(False)
                    return self._cmd(cmd, args)
                except (OSError, EOFError, ConnectionError) as e:
                    self.ready = False; self.error = str(e)
                    if attempt: raise
                except Exception as e:
                    self.error = str(e); raise
        return {}

    # voix ------------------------------------------------------------------------
    def voice(self): return self.call("GET_VOICE_SETTINGS")
    def set_voice(self, **kw): return self.call("SET_VOICE_SETTINGS", kw)

    def toggle(self, key):
        v = self.voice(); new = not v.get(key); self.set_voice(**{key: new})
        self._notify({"mute": "Micro Discord " + ("coupé" if new else "actif"), "deaf": "Discord " + ("sourd" if new else "audio actif")}[key])

    def step_output(self, d):
        v = ((self.voice().get("output") or {}).get("volume", 100)); n = max(0, min(200, v + d)); self.set_voice(output={"volume": n}); self._notify(f"Sortie Discord : {int(n)} %")
    def step_input(self, d):
        v = ((self.voice().get("input") or {}).get("volume", 100)); n = max(0, min(100, v + d)); self.set_voice(input={"volume": n}); self._notify(f"Micro Discord : {int(n)} %")

    def users(self, fresh=True):
        if not fresh and time.time() - self._ucache[0] < 2.0: return self._ucache[1]
        ch = self.call("GET_SELECTED_VOICE_CHANNEL") or {}
        me = (self.user or {}).get("id"); out = []
        for vs in ch.get("voice_states", []):
            u = vs.get("user") or {}
            if not u.get("id") or u["id"] == me: continue
            out.append({"id": u["id"], "name": vs.get("nick") or u.get("global_name") or u.get("username") or u["id"],
                        "volume": vs.get("volume", 100), "mute": bool(vs.get("mute"))})
        out.sort(key=lambda x: x["name"].lower()); self._ucache = (time.time(), out); return out

    def _find(self, name="", fresh=True):
        users = self.users(fresh)
        if not users: raise RuntimeError("personne d'autre dans le salon vocal")
        if name:
            n = name.strip().lower(); u = next((x for x in users if n in x["name"].lower()), None)
            if not u: raise RuntimeError(f"utilisateur « {name} » introuvable")
            return u
        return next((x for x in users if x["id"] == self._sel), users[0])

    def select(self, delta):
        users = self.users()
        if not users: raise RuntimeError("personne d'autre dans le salon vocal")
        ids = [u["id"] for u in users]; i = ids.index(self._sel) if self._sel in ids else (-1 if delta > 0 else 0)
        self._sel = ids[(i + delta) % len(ids)]; self._notify("Discord : " + users[(i + delta) % len(users)]["name"])

    def user_set(self, name, pct, fresh=False):
        u = self._find(name, fresh); pct = max(0, min(200, int(round(pct))))
        self.call("SET_USER_VOICE_SETTINGS", {"user_id": u["id"], "volume": pct}); self._ucache = (0, [])
        if fresh: self._notify(f"{u['name']} : {pct} %")
    def user_step(self, name, d): u = self._find(name); self.user_set(name, u["volume"] + d, True)
    def user_mute(self, name):
        u = self._find(name); self.call("SET_USER_VOICE_SETTINGS", {"user_id": u["id"], "mute": not u["mute"]}); self._notify(f"{u['name']} : " + ("muet" if not u["mute"] else "son actif"))
    def user_reset(self, name):
        u = self._find(name); self.call("SET_USER_VOICE_SETTINGS", {"user_id": u["id"], "volume": 100, "mute": False}); self._notify(f"{u['name']} : 100 %")
    def leave(self): self.call("SELECT_VOICE_CHANNEL", {"channel_id": None})
    def join(self, cid): self.call("SELECT_VOICE_CHANNEL", {"channel_id": str(cid), "force": True})

# ── ACTIONS SUPPLÉMENTAIRES ──────────────────────────────────────────────────
# Raccourcis clavier prédéfinis : type d'action -> touches envoyées
SHORTCUTS = {
    'win_maximize': 'win+up',
    'win_restore': 'win+down',
    'win_snap_left': 'win+left',
    'win_snap_right': 'win+right',
    'win_switch': 'alt+tab',
    'win_task_view': 'win+tab',
    'win_close': 'alt+f4',
    'desktop_next': 'ctrl+win+right',
    'desktop_prev': 'ctrl+win+left',
    'desktop_new': 'ctrl+win+d',
    'desktop_close': 'ctrl+win+f4',
    'win_explorer': 'win+e',
    'win_run': 'win+r',
    'win_settings': 'win+i',
    'win_search': 'win+s',
    'win_task_manager': 'ctrl+shift+esc',
    'win_emoji': 'win+.',
    'win_clipboard': 'win+v',
    'win_notifications': 'win+n',
    'win_quick_settings': 'win+a',
    'win_widgets': 'win+w',
    'win_project': 'win+p',
    'win_voice_typing': 'win+h',
    'game_bar': 'win+g',
    'game_record_toggle': 'win+alt+r',
    'game_record_last': 'win+alt+g',
    'game_screenshot': 'win+alt+print screen',
    'nvidia_overlay': 'alt+z',
    'nvidia_replay_save': 'alt+f10',
    'steam_overlay': 'shift+tab',
    'browser_new_tab': 'ctrl+t',
    'browser_close_tab': 'ctrl+w',
    'browser_reopen_tab': 'ctrl+shift+t',
    'browser_next_tab': 'ctrl+tab',
    'browser_prev_tab': 'ctrl+shift+tab',
    'browser_refresh': 'f5',
    'browser_hard_refresh': 'ctrl+f5',
    'browser_back': 'alt+left',
    'browser_forward': 'alt+right',
    'browser_private': 'ctrl+shift+n',
    'browser_devtools': 'f12',
    'browser_fullscreen': 'f11',
    'browser_address': 'ctrl+l',
    'browser_bookmark': 'ctrl+d',
    'browser_find': 'ctrl+f',
    'edit_copy': 'ctrl+c',
    'edit_cut': 'ctrl+x',
    'edit_paste': 'ctrl+v',
    'edit_paste_plain': 'ctrl+shift+v',
    'edit_undo': 'ctrl+z',
    'edit_redo': 'ctrl+y',
    'edit_select_all': 'ctrl+a',
    'edit_save': 'ctrl+s',
    'edit_new': 'ctrl+n',
    'edit_open': 'ctrl+o',
    'edit_print': 'ctrl+p',
    'edit_enter': 'enter',
    'edit_escape': 'esc',
    'edit_delete': 'delete',
    'edit_tab': 'tab',
    'video_play': 'k',
    'video_mute': 'm',
    'video_fullscreen': 'f',
    'video_captions': 'c',
    'video_seek_back': 'j',
    'video_seek_fwd': 'l',
    'video_next': 'shift+n',
    'win_move_monitor_left': 'win+shift+left',
    'win_move_monitor_right': 'win+shift+right',
    'win_minimize_key': 'win+down',
    'win_print_screen': 'print screen',
    'win_copilot': 'win+c',
    'win_accessibility': 'win+u',
    'browser_downloads': 'ctrl+j',
    'browser_history': 'ctrl+h',
    'browser_new_window': 'ctrl+n',
    'browser_close_window': 'ctrl+shift+w',
    'browser_view_source': 'ctrl+u',
    'browser_tab_last': 'ctrl+9',
    'edit_bold': 'ctrl+b',
    'edit_italic': 'ctrl+i',
    'edit_underline': 'ctrl+u',
    'edit_replace': 'ctrl+h',
    'edit_home': 'home',
    'edit_end': 'end',
    'edit_pageup': 'page up',
    'edit_pagedown': 'page down',
    'edit_rename': 'f2',
    'edit_refresh': 'f5',
    'video_speed_up': 'shift+.',
    'video_speed_down': 'shift+,',
    'video_prev': 'shift+p',
    'video_theater': 't',
    'video_miniplayer': 'i',
    'video_back5': 'left',
    'video_fwd5': 'right',
    'video_vol_up': 'up',
    'video_vol_down': 'down',
    'video_next_frame': '.',
    'video_prev_frame': ',',
    'media_key_vol_up': 'volume up',
    'media_key_vol_down': 'volume down',
    'media_key_mute': 'volume mute',
    'spotify_like': 'alt+shift+b',
    'spotify_shuffle': 'ctrl+s',
    'spotify_repeat': 'ctrl+r',
    'spotify_seek_fwd': 'shift+right',
    'spotify_seek_back': 'shift+left',
    'discord_quick_switch': 'ctrl+k',
    'discord_chan_prev': 'alt+up',
    'discord_chan_next': 'alt+down',
    'discord_server_prev': 'ctrl+alt+up',
    'discord_server_next': 'ctrl+alt+down',
    'discord_unread_next': 'alt+shift+down',
    'zoom_mute': 'alt+a',
    'zoom_camera': 'alt+v',
    'zoom_hand': 'alt+y',
    'zoom_share': 'alt+s',
    'zoom_record': 'alt+r',
    'zoom_chat': 'alt+h',
    'zoom_participants': 'alt+u',
    'zoom_leave': 'alt+q',
    'teams_mute': 'ctrl+shift+m',
    'teams_camera': 'ctrl+shift+o',
    'teams_share': 'ctrl+shift+e',
    'teams_hand': 'ctrl+shift+k',
    'teams_leave': 'ctrl+shift+h',
    'meet_mute': 'ctrl+d',
    'meet_camera': 'ctrl+e',
    'meet_hand': 'ctrl+alt+h',
    'meet_chat': 'ctrl+alt+c',
    'vscode_terminal': 'ctrl+`',
    'vscode_palette': 'ctrl+shift+p',
    'vscode_quick_open': 'ctrl+p',
    'vscode_sidebar': 'ctrl+b',
    'vscode_debug': 'f5',
    'vscode_comment': 'ctrl+/'
}

LAUNCHERS = {
    'app_calc': 'calc',
    'app_notepad': 'notepad',
    'app_paint': 'mspaint',
    'app_terminal': 'wt',
    'app_cmd': 'cmd',
    'app_powershell': 'powershell',
    'app_snipping': 'snippingtool',
    'app_taskmgr': 'taskmgr',
    'app_control': 'control',
    'app_devmgmt': 'devmgmt.msc',
    'app_diskmgmt': 'diskmgmt.msc',
    'app_services': 'services.msc',
    'app_regedit': 'regedit',
    'app_resmon': 'resmon',
    'app_msinfo': 'msinfo32',
    'app_network': 'ncpa.cpl',
    'app_sound': 'mmsys.cpl',
    'app_power': 'powercfg.cpl',
    'app_env': 'rundll32 sysdm.cpl,EditEnvironmentVariables',
    'app_steam': 'steam://open/main',
    'app_store': 'ms-windows-store:',
    'app_camera': 'microsoft.windows.camera:',
    'app_clock': 'ms-clock:',
    'app_photos': 'ms-photos:',
    'app_xbox': 'xbox:'
}

WEB_LINKS = {
    'open_chatgpt': 'https://chatgpt.com',
    'web_claude': 'https://claude.ai',
    'web_gemini': 'https://gemini.google.com',
    'google_gmail': 'https://mail.google.com',
    'google_meet': 'https://meet.google.com/new',
    'google_calendar': 'https://calendar.google.com',
    'web_drive': 'https://drive.google.com',
    'web_maps': 'https://maps.google.com',
    'web_translate': 'https://translate.google.com',
    'web_youtube': 'https://www.youtube.com',
    'web_twitch': 'https://www.twitch.tv',
    'web_netflix': 'https://www.netflix.com',
    'web_prime': 'https://www.primevideo.com',
    'web_disney': 'https://www.disneyplus.com',
    'web_spotify': 'https://open.spotify.com',
    'web_github': 'https://github.com',
    'web_reddit': 'https://www.reddit.com',
    'web_x': 'https://x.com',
    'web_whatsapp': 'https://web.whatsapp.com',
    'web_outlook': 'https://outlook.live.com',
    'web_notion': 'https://www.notion.so',
    'web_figma': 'https://www.figma.com',
    'web_linkedin': 'https://www.linkedin.com',
    'web_wikipedia': 'https://fr.wikipedia.org'
}

PRESET_APPS = {   # volume d'application : clé -> noms de processus
    'chrome': 'chrome',
    'firefox': 'firefox',
    'edge': 'msedge',
    'brave': 'brave',
    'opera': 'opera',
    'vlc': 'vlc',
    'teams': 'ms-teams|teams',
    'zoom': 'zoom',
    'steam': 'steam|steamwebhelper',
    'telegram': 'telegram',
    'whatsapp': 'whatsapp',
    'obs': 'obs64|obs32',
    'slack': 'slack',
    'tidal': 'tidal',
    'deezer': 'deezer',
    'foobar': 'foobar2000',
    'itunes': 'itunes|applemusic',
    'minecraft': 'javaw|minecraft',
    'cs2': 'cs2',
    'valorant': 'valorant-win64-shipping',
    'fortnite': 'fortniteclient-win64-shipping',
    'apex': 'r5apex',
    'rocketleague': 'rocketleague'
}

def launch_cmd(cmd):
    """Lance une application, un .msc/.cpl ou un lien (steam://, ms-settings:…)."""
    if re.match(r"^[a-z][\w.+-]*:(//|[^\\/])", cmd, re.I) and not re.match(r"^[a-z]:[\\/]", cmd, re.I): os.startfile(cmd)
    else: run_hidden(f'start "" {cmd}', shell=True, creationflags=CREATE_NO_WINDOW)

def _ps(code: str):
    return run_hidden(["powershell", "-NoProfile", "-Command", code], creationflags=CREATE_NO_WINDOW)

def _ps_text(template: str, text: str):
    """Exécute du PowerShell avec un texte arbitraire passé en base64 (accents, guillemets, etc.)."""
    b64 = base64.b64encode((text or "").encode("utf-8")).decode()
    code = "$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + b64 + "'));" + template
    return subprocess.run(["powershell", "-NoProfile", "-Command", code],
                          creationflags=CREATE_NO_WINDOW, startupinfo=_SI, timeout=30)

def set_clipboard(text: str):
    _ps_text("Set-Clipboard -Value $t", text)

def speak_text(text: str):
    if text:
        _ps_text("Add-Type -AssemblyName System.Speech;"
                 "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak($t)", text)

def toggle_dark_mode():
    import winreg
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", 0,
        winreg.KEY_READ | winreg.KEY_SET_VALUE)
    cur, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
    new = 0 if cur else 1
    for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
        winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, new)
    winreg.CloseKey(k)
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "ImmersiveColorSet", 2, 1000, None)

def kill_active_app():
    """Ferme de force l'application au premier plan (sauf l'explorateur et Imperium)."""
    if not WIN32_OK: return
    hwnd = win32gui.GetForegroundWindow()
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    if not pid or pid == os.getpid(): return
    proc = psutil.Process(pid)
    if (proc.name() or "").lower() in ("explorer.exe", "imperium.exe"): return
    proc.kill()

def toggle_app_mute(process_name: str): set_app_mute(process_name)

# ── APPS ─────────────────────────────────────────────────────────────────────
def get_installed_apps():
    apps, seen = [], set()
    def add(name, path, kind):
        k = name.lower().strip()
        if k and k not in seen: seen.add(k); apps.append({"name":name,"path":path,"type":kind})

    for base in [os.path.join(os.environ.get("APPDATA",""),"Microsoft","Windows","Start Menu","Programs"),
                 r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs"]:
        try:
            for r,_,files in os.walk(base):
                for f in files:
                    if f.endswith(".lnk"): add(f[:-4], os.path.join(r,f), "lnk")
        except: pass

    try:
        import winreg
        for hive in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
            for p in [r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                      r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"]:
                try:
                    key=winreg.OpenKey(hive,p); i=0
                    while True:
                        try:
                            sub=winreg.OpenKey(key,winreg.EnumKey(key,i))
                            try:
                                n=winreg.QueryValueEx(sub,"DisplayName")[0]
                                e=winreg.QueryValueEx(sub,"DisplayIcon")[0].split(",")[0].strip('"')
                                if e.endswith(".exe") and os.path.isfile(e): add(n,e,"exe")
                            except: pass
                            i+=1
                        except OSError: break
                except: pass
    except: pass

    for base in [r"C:\Program Files",r"C:\Program Files (x86)",
                 os.path.join(os.environ.get("LOCALAPPDATA",""),"Programs")]:
        try:
            for d in os.listdir(base):
                try:
                    full=os.path.join(base,d)
                    if os.path.isdir(full):
                        for f in os.listdir(full):
                            if f.endswith(".exe"): add(f[:-4],os.path.join(full,f),"exe")
                except: pass
        except: pass

    apps.sort(key=lambda x:x["name"].lower())
    return apps

# ── CONFIG ───────────────────────────────────────────────────────────────────
def empty_profile(name):
    return {
        "name": name, "app_trigger": "",
        "buttons": {str(i):{"icon":"⭐","label":"Bouton "+str(i+1),"press":[],"long_press":[],"double_click":[]} for i in range(8)},
        "pots": {str(i):{"name":["Volume","App Vol","Luminosité","Custom"][i],"action":["volume_system","volume_app","brightness","custom"][i]} for i in range(4)}
    }

DEFAULT_CONFIG = {
    "profiles": {
        "default": empty_profile("Global"),
        "obs": empty_profile("OBS"),
        "discord": empty_profile("Discord"),
    },
    "active_profile": "default",
    "led_strips": {str(i):{"metric":["cpu","ram","gpu_usage","ssd_usage"][i]} for i in range(4)},
    "serial_port": "AUTO", "theme": "dark", "tint": "cuivre",
    "obs_host": "localhost", "obs_port": 4455, "obs_password": "", "discord_client_id": "", "discord_client_secret": "",
    "serial_port2": "", "serial_port3": "", "baud_rate": 115200, "baud_rate2": 115200, "baud_rate3": 115200
}

# ── TRAMES ESP32 ─────────────────────────────────────────────────────────────
# Une trame = une ligne de texte. Format unique et fixe :
#   ESP32 -> PC   E<n>:<±pas>   encodeur n : crans tournés (ex. E0:1 ou E0:-1)   (n = 0..3)
#                 B<n>:1        bouton n enfoncé       (n = 0..7)
#                 B<n>:0        bouton n relâché
#                 P<n>:<0-100>  potentiomètre n        (n = 0..3)
#   PC -> ESP32   L<n>:<0-100>  niveau de la LED n     (n = 0..3)
# Appui long et double clic sont détectés par l'application (pas par l'ESP32).
# L'ancien format (btn0:on / btn0:off / pot0:50) reste accepté.
_RX_BTN = re.compile(r"^(?:btn|b)(\d+)\s*[:=]\s*(1|0|on|off)$", re.I)
_RX_POT = re.compile(r"^(?:pot|p)(\d+)\s*[:=]\s*(\d+)$", re.I)
_RX_ENC = re.compile(r"^(?:enc|e)(\d+)\s*[:=]\s*([+-]?\d+)$", re.I)

def parse_frame(raw: str):
    """('btn', index, enfoncé: bool) | ('pot', index, valeur 0-100) | None"""
    raw = (raw or "").strip()
    m = _RX_BTN.match(raw)
    if m: return ("btn", int(m.group(1)), m.group(2).lower() in ("1", "on"))
    m = _RX_POT.match(raw)
    if m: return ("pot", int(m.group(1)), max(0, min(100, int(m.group(2)))))
    m = _RX_ENC.match(raw)
    if m: return ("enc", int(m.group(1)), max(-50, min(50, int(m.group(2)))))
    return None

class ConfigManager:
    def __init__(self):
        self.data = DEFAULT_CONFIG.copy()
        self.load()

    def load(self):
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH,"r",encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except: pass

    def save(self):
        with open(CONFIG_PATH,"w",encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def active(self):
        name = self.data.get("active_profile","default")
        return self.data["profiles"].get(name, list(self.data["profiles"].values())[0])

# ── MOTEUR D'ACTIONS ─────────────────────────────────────────────────────────

# ── POTARDS / ENCODEURS ──────────────────────────────────────────────────────
class PotWorker:
    """Exécute les actions de potards dans un thread dédié (COM initialisé).
    Seule la dernière valeur de chaque potard compte : aucun retard n'est accumulé, même en tournant vite."""
    def __init__(self, engine):
        self.engine = engine; self._q = {}; self._cv = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    def push(self, idx, cfg, kind, v):
        with self._cv:
            old = self._q.get(idx)
            if kind == "rel" and old and old[1] == "rel": v += old[2]     # on cumule les crans
            self._q[idx] = (cfg, kind, v); self._cv.notify()

    def _run(self):
        com_init()
        while True:
            with self._cv:
                while not self._q: self._cv.wait()
                items = list(self._q.items()); self._q.clear()
            for idx, (cfg, kind, v) in items:
                try: self.engine.pot_event(idx, cfg, kind, v)
                except Exception as e: log.error(f"Potard {idx}: {e}")

def _acc(st, steps):
    """Cumule des pas fractionnaires ; renvoie le nombre entier de pas à exécuter."""
    st["acc"] = st.get("acc", 0.0) + steps; n = int(st["acc"]); st["acc"] -= n; return n

def _keys(st, steps, pos, neg):
    n = _acc(st, steps)
    for _ in range(min(abs(n), 40)): keyboard.send(pos if n > 0 else neg)

def _hwheel(d):
    if sys.platform == "win32": ctypes.windll.user32.mouse_event(0x1000, 0, 0, int(d * 120), 0)

def _ctrl_wheel(d): keyboard.press("ctrl"); mouse.wheel(d); keyboard.release("ctrl")

# type d'action -> fonction(moteur, config du potard, pas (float, signé), état)
DELTA_ACTIONS = {
    "scroll":            lambda e, c, n, st: mouse.wheel(-n),
    "scroll_horizontal": lambda e, c, n, st: _hwheel(n),
    "zoom_level":        lambda e, c, n, st: _ctrl_wheel(n),
    "page_scroll":       lambda e, c, n, st: _keys(st, n, "page down", "page up"),
    "arrow_vertical":    lambda e, c, n, st: _keys(st, n, "down", "up"),
    "arrow_horizontal":  lambda e, c, n, st: _keys(st, n, "right", "left"),
    "tab_switch":        lambda e, c, n, st: _keys(st, n, "ctrl+tab", "ctrl+shift+tab"),
    "desktop_switch":    lambda e, c, n, st: _keys(st, n, "ctrl+win+right", "ctrl+win+left"),
    "mouse_move_x":      lambda e, c, n, st: mouse.move(n * 12, 0, absolute=False),
    "mouse_move_y":      lambda e, c, n, st: mouse.move(0, n * 12, absolute=False),
    "media_seek":        lambda e, c, n, st: _keys(st, n, "right", "left"),
    "seek_10s":          lambda e, c, n, st: _keys(st, n, "l", "j"),
    "playback_speed":    lambda e, c, n, st: _keys(st, n, "shift+.", "shift+,"),
    "vlc_speed":         lambda e, c, n, st: _keys(st, n, "]", "["),
    "media_track":       lambda e, c, n, st: _keys(st, n, "next track", "previous track"),
    "volume_keys":       lambda e, c, n, st: _keys(st, n, "volume up", "volume down"),
    "undo_redo":         lambda e, c, n, st: _keys(st, n, "ctrl+y", "ctrl+z"),
    "brush_size":        lambda e, c, n, st: _keys(st, n, "]", "["),
    "timeline_frame":    lambda e, c, n, st: _keys(st, n, ".", ","),
    "profile_cycle":     lambda e, c, n, st: e._cycle_profile(_acc(st, n)),
    "obs_scene_cycle":   lambda e, c, n, st: (lambda k: k and e.obs.cycle_scene(k))(_acc(st, n)),
}

class ActionEngine:
    def __init__(self, cfg: ConfigManager, broadcast_fn, plugins=None):
        self.cfg = cfg; self.broadcast = broadcast_fn; self.plugins = plugins
        self._pot_state = {}; self._autoclick = None
        self.obs = OBSClient(lambda: self.cfg.data)
        self.discord = DiscordRPC(lambda: self.cfg.data, lambda m: self.broadcast({"type": "toast", "message": m}))
        self.potq = PotWorker(self)

    def run(self, actions: list):
        com_init()
        for a in actions:
            try: self._one(a)
            except Exception as e: log.error(f"Action {a.get('type')}: {e}")

    # ── Potards et encodeurs ────────────────────────────────────────────────────
    ABS_PER_STEP = 2.0     # potard à valeur absolue : un « cran » équivaut à 2 %

    def pot_push(self, idx, cfg, kind, v): self.potq.push(idx, cfg, kind, v)
    def run_pot(self, pot_cfg: dict, val: int): self.pot_event(-1, pot_cfg, "abs", val)

    def _cycle_profile(self, n):
        for _ in range(min(abs(n), 10)): self._one({"type": "next_profile" if n > 0 else "prev_profile"})

    def _app_level(self, app):
        return (lambda: get_app_volume(app), lambda v: set_app_volume(app, v), 2)

    def _level_spec(self, a, c):
        """Actions « niveau » : (lecture, écriture, pas par cran en %) ou None."""
        if a == "volume_system": return (get_volume, set_volume, 2)
        if a in ("volume_app", "game_volume"): return self._app_level(c.get("app", ""))
        if a == "discord_volume": return self._app_level("Discord")
        if a == "spotify_volume": return self._app_level("Spotify")
        if a.startswith("volume_app_") and a[11:] in PRESET_APPS: return self._app_level(PRESET_APPS[a[11:]])
        if a == "volume_active_app": return (lambda: get_app_volume(foreground_process()), lambda v: set_app_volume(foreground_process(), v), 2)
        if a == "volume_all_apps": return (None, set_all_apps_volume, 2)
        if a == "mic_volume":
            if c.get("app"): return self._app_level(c.get("app"))       # anciennes configs : micro d'une application
            return (get_mic_volume, set_mic_volume, 2)
        if a == "brightness": return (get_brightness, set_brightness, 5)
        if a == "obs_volume":
            src = c.get("source", "Mic/Aux"); return (lambda: self.obs.get_volume_pct(src), lambda v: self.obs.set_volume_pct(src, v), 2)
        if a == "ha_light_brightness": return (None, lambda v: self._ha_brightness(c, v), 5)
        if a == "discord_rpc_output_volume": return (None, lambda v: self.discord.set_voice(output={"volume": v * 2}), 4)
        if a == "discord_rpc_input_volume": return (None, lambda v: self.discord.set_voice(input={"volume": v}), 4)
        if a == "discord_rpc_user_volume": return (None, lambda v: self.discord.user_set(c.get("user", ""), v * 2), 4)
        if a == "led_strip_color": return (None, lambda v: self.broadcast({"type": "led_strip_set", "strip": int(c.get("strip", 0) or 0), "value": v}), 5)
        if a == "custom":
            code = c.get("script", ""); return (None, (lambda v: exec(code, {"value": v})) if code else (lambda v: None), 2)
        if self.plugins and a in self.plugins.actions:
            params = {k: x for k, x in c.items() if not k.startswith("_") and k != "action"}
            return (None, lambda v: self.plugins.run(a, params, value=v), 2)
        return None

    def pot_event(self, idx, cfg, kind, v):
        """kind = "abs" (valeur 0-100 d'un potentiomètre) ou "rel" (crans d'un encodeur, signés)."""
        action = cfg.get("action", "volume_system")
        try: sens = max(0.05, min(20.0, float(cfg.get("sensitivity", 1) or 1)))
        except (TypeError, ValueError): sens = 1.0
        inv = str(cfg.get("invert", "")).lower() in ("1", "true", "yes", "on")
        st = self._pot_state.setdefault(idx, {})
        if st.get("action") != action: st.clear(); st["action"] = action
        try:
            spec = self._level_spec(action, cfg)
            if spec:
                get_, set_, base = spec
                if kind == "abs":
                    val = max(0, min(100, v)); val = 100 - val if inv else val
                    set_(val); st["f"] = float(val); return
                cur = st.get("f"); real = None
                if get_:
                    try: real = get_()
                    except Exception: real = None
                if real is not None and (cur is None or abs(real - cur) > 1.5): cur = float(real)
                if cur is None: cur = 50.0
                new = max(0.0, min(100.0, cur + (-v if inv else v) * base * sens)); st["f"] = new
                if round(new) != round(cur) or new in (0.0, 100.0): set_(int(round(new)))
                return
            fn = DELTA_ACTIONS.get(action)
            if fn:
                if kind == "abs":
                    last = st.get("last"); st["last"] = v
                    if last is None: return
                    steps = (v - last) / self.ABS_PER_STEP
                else: steps = float(v)
                fn(self, cfg, (-steps if inv else steps) * sens, st)
        except Exception as e: log.error(f"Pot action '{action}': {e}")

    def _ha_brightness(self, c, v):
        if v <= 0: self._ha({**c, "service": "light.turn_off"}); return
        self._ha({**c, "service": "light.turn_on"}, {"brightness_pct": int(v)})

    def _one(self, a: dict):
        if isinstance(a.get("params"), dict):
            merged = dict(a); merged.update(a["params"]); a = merged
        t = a.get("type","")
        # L'interface envoie tous les paramètres sous forme de texte : on convertit les nombres.
        for k in ("step","value","seconds","ms","delta","x","y","delay","interval","minutes"):
            v = a.get(k)
            if isinstance(v, str):
                v = v.strip()
                try: a[k] = float(v) if "." in v else int(v)
                except ValueError: a.pop(k, None)

        # Profils
        if t in ("switch_profile","next_profile","prev_profile"):
            keys = list(self.cfg.data["profiles"].keys())
            cur  = self.cfg.data.get("active_profile","default")
            if   t == "switch_profile": name = a.get("profile","")
            elif t == "next_profile":   name = keys[(keys.index(cur)+1) % len(keys)] if cur in keys else keys[0]
            else:                       name = keys[(keys.index(cur)-1) % len(keys)] if cur in keys else keys[0]
            if name in self.cfg.data["profiles"]:
                self.cfg.data["active_profile"] = name
                self.cfg.save()
                self.broadcast({"type":"profile_changed","profile":name})

        # PC
        elif t == "open_app":     run_hidden(a.get("path",""), shell=True, creationflags=CREATE_NO_WINDOW)
        elif t == "close_app":
            n = _norm_app(a.get("name", ""))
            if n:      # nom vide = on ne touche à rien (avant : tous les processus étaient visés)
                for p in psutil.process_iter(["name"]):
                    if _norm_app(p.info.get("name") or "") == n:
                        try: p.terminate()
                        except Exception: pass
        elif t == "open_folder":  os.startfile(a.get("path","."))
        elif t == "open_file":    os.startfile(a.get("path",""))
        elif t == "open_url":     open_url_default(a.get("url",""))
        elif t == "lock_session": ctypes.windll.user32.LockWorkStation()
        elif t == "shutdown":     run_silent("shutdown /s /t 0")
        elif t == "restart":      run_silent("shutdown /r /t 0")
        elif t == "sleep":        run_silent("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
        elif t == "logoff":       run_silent("shutdown /l")
        elif t == "run_command":  run_hidden(a.get("command",""), shell=True, creationflags=CREATE_NO_WINDOW)
        elif t == "script_powershell": run_hidden(["powershell","-Command",a.get("code","")], creationflags=CREATE_NO_WINDOW)
        elif t == "script_python":     exec(a.get("code",""), {})
        elif t == "script_batch":
            tmp=os.path.join(tempfile.gettempdir(), "md_tmp.bat")
            open(tmp,"w").write(a.get("code","")); run_hidden(tmp, shell=True, creationflags=CREATE_NO_WINDOW)
        elif t == "clean_temp":
            tmp = tempfile.gettempdir()
            if tmp and os.path.isabs(tmp) and len(tmp) > 4:       # garde-fou : jamais un dossier vide ou relatif
                for f in glob.glob(os.path.join(tmp, "*")):
                    try: os.remove(f) if os.path.isfile(f) else shutil.rmtree(f, ignore_errors=True)
                    except Exception: pass
        elif t == "screenshot":       keyboard.send("win+shift+s")
        elif t == "win_minimize_all": keyboard.send("win+d")

        # Clavier / souris
        elif t == "hotkey":       keyboard.send(a.get("keys",""))
        elif t == "type_text":    keyboard.write(a.get("text",""), delay=a.get("delay",0.03))
        elif t == "key_sequence":
            for k in a.get("sequence","").split(","):
                keyboard.send(k.strip()); time.sleep(a.get("interval",0.05))
        elif t == "mouse_click":
            x,y=a.get("x"),a.get("y")
            if x is not None: mouse.move(x,y,absolute=True)
            mouse.click(a.get("button","left"))
        elif t == "mouse_move":   mouse.move(a.get("x",0),a.get("y",0),absolute=a.get("absolute",True))
        elif t == "mouse_scroll": mouse.wheel(a.get("delta",1))

        # Audio
        elif t == "volume_up":   set_volume(get_volume()+a.get("step",5))
        elif t == "volume_down": set_volume(get_volume()-a.get("step",5))
        elif t == "volume_set":  set_volume(int(a.get("value",50)))
        elif t == "mute_toggle": set_mute(not get_mute())
        elif t == "media_play_pause": keyboard.send("play/pause media")
        elif t == "media_next":  keyboard.send("next track")
        elif t == "media_prev":  keyboard.send("previous track")
        elif t == "media_stop":  keyboard.send("stop media")
        elif t == "brightness": set_brightness(a.get("value", 75))

        # Visio
        elif t == "zoom_mute":     keyboard.send("alt+a")
        elif t == "zoom_camera":   keyboard.send("alt+v")
        elif t == "zoom_hand":     keyboard.send("alt+y")
        elif t == "zoom_share":    keyboard.send("alt+s")
        elif t == "zoom_leave":    keyboard.send("alt+q")
        elif t == "teams_mute":    keyboard.send("ctrl+shift+m")
        elif t == "teams_camera":  keyboard.send("ctrl+shift+o")
        elif t == "teams_share":   keyboard.send("ctrl+shift+e")
        elif t == "meet_mute":     keyboard.send("ctrl+d")
        elif t == "meet_camera":   keyboard.send("ctrl+e")
        elif t in ("discord_mute", "discord_deafen"): self._discord_toggle(t, a)

        # Dev
        elif t == "vscode_open":  run_hidden(f'code "{a.get("path",".")}"', shell=True, creationflags=CREATE_NO_WINDOW)
        elif t == "git_pull":     run_hidden(f'git -C "{a.get("folder",".")}" pull', shell=True, creationflags=CREATE_NO_WINDOW)
        elif t == "git_push":
            f=a.get("folder","."); m=a.get("message","commit")
            run_hidden(f'git -C "{f}" add -A && git -C "{f}" commit -m "{m}" && git -C "{f}" push', shell=True, creationflags=CREATE_NO_WINDOW)
        elif t == "git_commit":
            f=a.get("folder","."); m=a.get("message","commit")
            run_hidden(f'git -C "{f}" add -A && git -C "{f}" commit -m "{m}"', shell=True, creationflags=CREATE_NO_WINDOW)
        elif t == "docker_start": run_hidden(f'docker start {a.get("name","")}', shell=True, creationflags=CREATE_NO_WINDOW)
        elif t == "docker_stop":  run_hidden(f'docker stop {a.get("name","")}', shell=True, creationflags=CREATE_NO_WINDOW)
        elif t == "ssh":          subprocess.Popen(f'start cmd /k ssh {a.get("user","")}@{a.get("host","")}', shell=True)

        # Web
        elif t == "open_chatgpt":    open_url_default("https://chatgpt.com")
        elif t == "google_gmail":    open_url_default("https://mail.google.com")
        elif t == "google_meet":     open_url_default("https://meet.google.com/new")
        elif t == "google_calendar": open_url_default("https://calendar.google.com")

        # Timer
        elif t == "timer":
            s=int(a.get("seconds",60)); lbl=a.get("label","Timer terminé !")
            threading.Thread(target=lambda:(_timer(s,lbl)), daemon=True).start()
        elif t == "pomodoro":
            threading.Thread(target=lambda:(_timer(25*60,"🍅 Pomodoro terminé !")), daemon=True).start()

        # Réseau / Auto
        elif t == "ping":    subprocess.Popen(f'start cmd /k ping -t {a.get("host","8.8.8.8")}', shell=True)
        elif t == "api_call":       threading.Thread(target=self._api, args=(a,), daemon=True).start()
        elif t == "webhook":        threading.Thread(target=self._webhook, args=(a,), daemon=True).start()
        elif t == "home_assistant": threading.Thread(target=self._ha, args=(a,), daemon=True).start()
        elif t == "delay":          time.sleep(a.get("ms",500)/1000.0)
        elif t == "multi_action":
            delay=a.get("delay",0)
            def _run():
                for act in a.get("actions",[]): self._one(act); time.sleep(delay/1000.0) if delay else None
            threading.Thread(target=_run, daemon=True).start()

        # Raccourcis prédéfinis (Fenêtres, Windows, Jeu, Navigateur, Édition, Vidéo)
        elif t in SHORTCUTS:            keyboard.send(SHORTCUTS[t])
        elif t == "browser_zoom_in":    keyboard.press("ctrl"); mouse.wheel(1);  keyboard.release("ctrl")
        elif t == "browser_zoom_out":   keyboard.press("ctrl"); mouse.wheel(-1); keyboard.release("ctrl")
        elif t == "browser_zoom_reset": keyboard.send("ctrl+0")

        # Texte / presse-papiers
        elif t == "paste_text":    set_clipboard(a.get("text","")); time.sleep(0.08); keyboard.send("ctrl+v")
        elif t == "clipboard_set": set_clipboard(a.get("text",""))
        elif t == "type_datetime": keyboard.write(datetime.datetime.now().strftime(a.get("format") or "%d/%m/%Y %H:%M"))
        elif t == "say_text":      speak_text(a.get("text",""))

        # Souris
        elif t == "mouse_double_click": mouse.double_click("left")
        elif t == "mouse_right_click":  mouse.click("right")
        elif t == "mouse_middle_click": mouse.click("middle")

        # Système
        elif t == "open_uri":          os.startfile(a.get("uri",""))
        elif t == "open_settings":     os.startfile("ms-settings:" + (a.get("page") or ""))
        elif t == "toggle_dark_mode":  toggle_dark_mode()
        elif t == "monitor_off":       ctypes.windll.user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)
        elif t == "empty_recycle_bin": _ps("Clear-RecycleBin -Force -ErrorAction SilentlyContinue")
        elif t == "flush_dns":         run_silent("ipconfig /flushdns")
        elif t == "restart_explorer":  run_silent("taskkill /f /im explorer.exe & start explorer.exe")
        elif t == "kill_active_app":   kill_active_app()
        elif t == "hibernate":         run_silent("shutdown /h")
        elif t == "shutdown_timer":    run_silent(f"shutdown /s /t {int(a.get('minutes',30)*60)}")
        elif t == "cancel_shutdown":   run_silent("shutdown /a")

        # Audio
        elif t == "mute_on":          set_mute(True)
        elif t == "mute_off":         set_mute(False)
        elif t == "app_mute_toggle":  set_app_mute(a.get("app") or a.get("name", ""))

        # OBS (suite)

        elif self._v2(t, a): pass
        elif self.plugins and t in self.plugins.actions:
            self.plugins.run(t, {k:v for k,v in a.items() if k != "type"})
        else:
            log.warning(f"Action inconnue : {t}")

    def _toast(self, msg): self.broadcast({"type": "toast", "message": msg})

    def _discord_toggle(self, t, a):
        try: self.discord.toggle("mute" if t == "discord_mute" else "deaf")
        except Exception as e:
            log.info(f"Discord API indisponible ({e}) : raccourci clavier")
            keyboard.send(a.get("keys") or ("ctrl+shift+m" if t == "discord_mute" else "ctrl+shift+d"))

    def _discord_action(self, t, a):
        d = self.discord; user = a.get("user", ""); step = a.get("step", 10)
        try:
            if   t == "discord_rpc_user_next": d.select(1)
            elif t == "discord_rpc_user_prev": d.select(-1)
            elif t == "discord_rpc_user_vol_up": d.user_step(user, step)
            elif t == "discord_rpc_user_vol_down": d.user_step(user, -step)
            elif t == "discord_rpc_user_mute": d.user_mute(user)
            elif t == "discord_rpc_user_reset": d.user_reset(user)
            elif t == "discord_rpc_output_up": d.step_output(step)
            elif t == "discord_rpc_output_down": d.step_output(-step)
            elif t == "discord_rpc_input_up": d.step_input(step)
            elif t == "discord_rpc_input_down": d.step_input(-step)
            elif t == "discord_rpc_leave": d.leave()
            elif t == "discord_rpc_join": d.join(a.get("channel_id", ""))
        except Exception as e: self._toast(f"Discord : {e}")

    def _obs_action(self, t, a):
        o = self.obs; src = a.get("source", "Mic/Aux"); step = a.get("step", 5)
        simple = {"obs_stream_start": "StartStream", "obs_stream_stop": "StopStream", "obs_stream_toggle": "ToggleStream",
                  "obs_record_start": "StartRecord", "obs_record_stop": "StopRecord", "obs_record_toggle": "ToggleRecord",
                  "obs_record_pause": "PauseRecord", "obs_record_resume": "ResumeRecord", "obs_replay_save": "SaveReplayBuffer",
                  "obs_replay_toggle": "ToggleReplayBuffer", "obs_transition": "TriggerStudioModeTransition", "obs_virtualcam_toggle": "ToggleVirtualCam"}
        if t in simple: o.request(simple[t])
        elif t == "obs_scene": o.request("SetCurrentProgramScene", {"sceneName": a.get("scene", "")})
        elif t == "obs_scene_next": o.cycle_scene(1)
        elif t == "obs_scene_prev": o.cycle_scene(-1)
        elif t == "obs_studio_mode": o.toggle_studio()
        elif t == "obs_mute_toggle": o.request("ToggleInputMute", {"inputName": src})
        elif t == "obs_mute_on": o.request("SetInputMute", {"inputName": src, "inputMuted": True})
        elif t == "obs_mute_off": o.request("SetInputMute", {"inputName": src, "inputMuted": False})
        elif t == "obs_volume_up": o.step_volume(src, step)
        elif t == "obs_volume_down": o.step_volume(src, -step)
        elif t == "obs_source_toggle": o.toggle_source(a.get("scene", ""), a.get("source", ""))
        elif t == "obs_screenshot": o.request("SaveSourceScreenshot", {"sourceName": a.get("source", ""), "imageFormat": "png", "imageFilePath": a.get("path", "")})
        elif t == "obs_hotkey": o.request("TriggerHotkeyByName", {"hotkeyName": a.get("hotkey", "")})
        else: return False
        return True

    def _v2(self, t, a):
        """Actions ajoutées. Retourne True si l'action a été traitée."""
        step = a.get("step", 5)
        if t.startswith("obs_"): return self._obs_action(t, a)
        if t.startswith("discord_rpc_"): self._discord_action(t, a); return True
        if t in LAUNCHERS: launch_cmd(LAUNCHERS[t])
        elif t in WEB_LINKS: open_url_default(WEB_LINKS[t])
        elif t == "mic_mute_toggle": set_mic_mute(not get_mic_mute())
        elif t == "mic_mute_on": set_mic_mute(True)
        elif t == "mic_mute_off": set_mic_mute(False)
        elif t == "mic_volume_up": set_mic_volume(get_mic_volume() + step)
        elif t == "mic_volume_down": set_mic_volume(get_mic_volume() - step)
        elif t == "mic_volume_set": set_mic_volume(int(a.get("value", 80)))
        elif t in ("app_volume_up", "app_volume_down", "app_volume_set"):
            app = a.get("app", ""); cur = get_app_volume(app)
            if t == "app_volume_set": set_app_volume(app, int(a.get("value", 50)))
            elif cur is not None: set_app_volume(app, cur + (step if t == "app_volume_up" else -step))
        elif t == "app_volume_active_mute": set_app_mute(foreground_process())
        elif t in ("brightness_up", "brightness_down"):
            cur = get_brightness(); set_brightness((50 if cur is None else cur) + (step if t == "brightness_up" else -step))
        elif t == "copy_public_ip":
            import urllib.request
            with urllib.request.urlopen("https://api.ipify.org", timeout=6) as r: ip = r.read().decode().strip()
            set_clipboard(ip); self._toast(f"IP publique copiée : {ip}")
        elif t == "copy_local_ip":
            import socket
            so = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try: so.connect(("8.8.8.8", 80)); ip = so.getsockname()[0]
            finally: so.close()
            set_clipboard(ip); self._toast(f"IP locale copiée : {ip}")
        elif t == "notify": self._toast(a.get("text", ""))
        elif t == "speak_time": speak_text(datetime.datetime.now().strftime("Il est %H heures %M"))
        elif t == "type_uuid": keyboard.write(str(uuid.uuid4()))
        elif t == "clipboard_clear": set_clipboard("")
        elif t in ("win_always_on_top", "win_minimize_active", "win_maximize_active"):
            if not WIN32_OK: return True
            h = win32gui.GetForegroundWindow()
            if t == "win_minimize_active": win32gui.ShowWindow(h, 6)
            elif t == "win_maximize_active": win32gui.ShowWindow(h, 3)
            else:
                top = bool(win32gui.GetWindowLong(h, -20) & 0x8)
                win32gui.SetWindowPos(h, -2 if top else -1, 0, 0, 0, 0, 0x0003)
        elif t == "key_down": keyboard.press(a.get("key", ""))
        elif t == "key_up": keyboard.release(a.get("key", ""))
        elif t == "key_hold":
            keyboard.press(a.get("key", "")); time.sleep(max(0, a.get("ms", 500)) / 1000.0); keyboard.release(a.get("key", ""))
        elif t == "autoclick_toggle":
            if self._autoclick and self._autoclick.is_set(): self._autoclick.clear(); self._toast("Auto-clic arrêté")
            else:
                ev = threading.Event(); ev.set(); self._autoclick = ev; gap = max(0.01, float(a.get("interval", 0.1) or 0.1))
                def _clicker():
                    while ev.is_set(): mouse.click("left"); time.sleep(gap)
                threading.Thread(target=_clicker, daemon=True).start(); self._toast("Auto-clic démarré")
        elif t == "ha_light_toggle": threading.Thread(target=self._ha, args=({**a, "service": "light.toggle"},), daemon=True).start()
        else: return False
        return True

    @staticmethod
    def _jload(x):
        if isinstance(x, (dict, list)): return x
        try: return json.loads(x) if x and str(x).strip() else None
        except Exception: return None

    def _api(self, a):
        import urllib.request
        req = urllib.request.Request(a.get("url", ""), method=a.get("method", "GET") or "GET")
        for k, v in (self._jload(a.get("headers")) or {}).items(): req.add_header(k, v)
        body = self._jload(a.get("body"))
        if body is not None: req.data = json.dumps(body).encode(); req.add_header("Content-Type", "application/json")
        elif a.get("body"): req.data = str(a["body"]).encode()
        try:
            with urllib.request.urlopen(req, timeout=10) as r: log.info(f"API {r.status}")
        except Exception as e: log.error(f"API: {e}")

    def _webhook(self, a):
        import urllib.request
        req = urllib.request.Request(a.get("url", ""), data=json.dumps(self._jload(a.get("payload")) or {}).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r: log.info(f"Webhook {r.status}")
        except Exception as e: log.error(f"Webhook: {e}")

    def _ha(self, a, extra=None):
        import urllib.request
        base = (a.get("ha_url") or "http://homeassistant.local:8123").rstrip("/")
        payload = {"entity_id": a.get("entity_id", ""), **(extra or {})}
        req = urllib.request.Request(f"{base}/api/services/{str(a.get('service', '')).replace('.', '/')}", data=json.dumps(payload).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {a.get('token', '')}"); req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as r: log.info(f"HA {r.status}")
        except Exception as e: log.error(f"HA: {e}")

def _timer(s, lbl):
    time.sleep(s)
    try: ctypes.windll.user32.MessageBoxW(0,lbl,"Imperium ⏱",0x40|0x1000)
    except: pass

# ── MÉTRIQUES ────────────────────────────────────────────────────────────────
class Metrics:
    """
    Métriques système 100% psutil + nvidia-smi, sans OpenHardwareMonitor.
    Le CPU % est mesuré en tâche de fond toutes les secondes pour être précis.
    Les températures CPU sont lues via psutil.sensors_temperatures() ou WMI ACPI.
    """
    def __init__(self):
        self._net_prev=psutil.net_io_counters(); self._net_t=time.time()
        self._cpu_pct=0.0
        # Démarrer la mesure CPU en fond (interval=1s donne des valeurs correctes)
        threading.Thread(target=self._cpu_loop, daemon=True).start()

    def _cpu_loop(self):
        """Mesure le CPU toutes les secondes avec interval=1 pour avoir de vraies valeurs."""
        while True:
            try: self._cpu_pct = psutil.cpu_percent(interval=1)
            except: time.sleep(1)

    def _cpu_temp(self):
        """Température CPU — psutil.sensors_temperatures() sur Linux/Mac,
        WMI MSAcpi sur Windows, sinon None."""
        # Méthode 1 : psutil (marche sur Linux, certains Windows avec drivers)
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for key in ("coretemp","k10temp","zenpower","cpu_thermal","acpitz"):
                    if key in temps:
                        vals = [t.current for t in temps[key] if t.current and t.current > 0]
                        if vals: return round(max(vals), 1)
                # Fallback : premier capteur disponible
                for key, entries in temps.items():
                    vals = [t.current for t in entries if t.current and 0 < t.current < 150]
                    if vals: return round(max(vals), 1)
        except: pass
        # Méthode 2 : WMI ACPI natif Windows (sans OpenHardwareMonitor)
        if WMI_OK:
            try:
                w = wmilib.WMI(namespace=r"root\wmi")
                for zone in w.MSAcpi_ThermalZoneTemperature():
                    k = zone.CurrentTemperature
                    if k and k > 2731:  # > 0°C en dixièmes de Kelvin
                        return round(k / 10.0 - 273.15, 1)
            except: pass
        return None

    def _gpu(self):
        """GPU via nvidia-smi (NVIDIA) ou wmi Win32_VideoController (basique)."""
        if GPU_OK:
            try:
                p = run_hidden(
                    ["nvidia-smi","--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,name",
                     "--format=csv,noheader,nounits"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                out, _ = p.communicate(timeout=2)
                line = out.strip().split("\n")[0]
                usage, mu, mt, temp, name = [x.strip() for x in line.split(",")]
                mt_f = float(mt)
                return {
                    "usage": float(usage),
                    "vram": round(float(mu)/mt_f*100, 1) if mt_f else 0,
                    "temp": float(temp),
                    "name": name.strip()
                }
            except: pass
        # Fallback : WMI Win32_VideoController (usage non dispo mais au moins le nom)
        if WMI_OK:
            try:
                w = wmilib.WMI()
                for gpu in w.Win32_VideoController():
                    if gpu.Name:
                        return {"usage":0,"vram":0,"temp":None,"name":gpu.Name}
            except: pass
        return None

    def collect(self):
        com_init()
        m = {}

        # CPU — valeur mesurée en fond, toujours fraîche
        m["cpu"]       = self._cpu_pct
        m["cpu_cores"] = psutil.cpu_count(logical=True) or 0
        freq = psutil.cpu_freq()
        m["cpu_freq"]  = round(freq.current, 0) if freq else 0
        m["cpu_temp"]  = self._cpu_temp()

        # RAM
        ram = psutil.virtual_memory()
        m["ram"]          = round(ram.percent, 1)
        m["ram_used_gb"]  = round(ram.used  / 1e9, 1)
        m["ram_total_gb"] = round(ram.total / 1e9, 1)

        # GPU
        m["gpu_usage"] = 0; m["gpu_vram"] = 0; m["gpu_temp"] = None; m["gpu_name"] = ""
        gpu = self._gpu()
        if gpu:
            m["gpu_usage"] = gpu["usage"]; m["gpu_vram"] = gpu["vram"]
            m["gpu_temp"]  = gpu["temp"];  m["gpu_name"] = gpu["name"]

        # Disques
        m["ssd_usage"] = 0
        try:    m["ssd_usage"] = psutil.disk_usage("C:\\").percent
        except:
            try: m["ssd_usage"] = psutil.disk_usage("/").percent
            except: pass
        m["disks"] = []
        for part in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(part.mountpoint)
                m["disks"].append({
                    "device": part.device, "mountpoint": part.mountpoint,
                    "total_gb": round(u.total/1e9,1),
                    "used_gb":  round(u.used /1e9,1),
                    "percent":  u.percent
                })
            except: pass

        # Réseau
        now = time.time(); net = psutil.net_io_counters(); dt = now - self._net_t
        m["net_up"]   = round((net.bytes_sent - self._net_prev.bytes_sent) / dt / 1024, 1) if dt > 0 else 0
        m["net_down"] = round((net.bytes_recv - self._net_prev.bytes_recv) / dt / 1024, 1) if dt > 0 else 0
        self._net_prev = net; self._net_t = now

        # Système
        m["uptime"] = str(datetime.timedelta(seconds=int(time.time()-psutil.boot_time())))
        m["volume"] = get_volume(); m["muted"] = get_mute()
        n = datetime.datetime.now()
        m["time"] = n.strftime("%H:%M:%S"); m["date"] = n.strftime("%d/%m/%Y")

        # Top processus (cpu_percent() sans interval = valeur depuis dernier appel = correct ici)
        procs = []
        for p in sorted(psutil.process_iter(["name","cpu_percent","memory_percent"]),
                        key=lambda x: (x.info.get("cpu_percent") or 0), reverse=True)[:8]:
            try:
                procs.append({
                    "name": p.info["name"],
                    "cpu":  round(p.info.get("cpu_percent")  or 0, 1),
                    "mem":  round(p.info.get("memory_percent") or 0, 1)
                })
            except: pass
        m["top_processes"] = procs
        return m

# ── MISE À JOUR (GitHub Releases) ────────────────────────────────────────────
GITHUB_REPO = "tuturpotter-web/Imperium"

def _ver_tuple(v):
    """'v0.0.96' -> (0, 0, 96). Une version non numérique ('dev') vaut (0,)."""
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(n) for n in nums) if nums else (0,)

def fetch_latest_release():
    """Release publiée la plus récente qui contient un installateur .exe.
    Retourne {version, url, filename, page}. Lève une Exception avec un message lisible."""
    import urllib.request, urllib.error
    api = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=15"
    req = urllib.request.Request(api, headers={"User-Agent": "Imperium-updater",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            releases = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404: raise Exception("dépôt GitHub introuvable ou privé")
        if e.code == 403: raise Exception("limite GitHub atteinte, réessaie dans quelques minutes")
        raise Exception(f"GitHub a répondu {e.code}")
    except OSError as e:
        raise Exception(f"pas de connexion à GitHub ({getattr(e, 'reason', e)})")
    best = None
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"): continue
        assets = [a for a in rel.get("assets", [])
                  if a.get("name", "").lower().endswith(".exe") and a.get("state", "uploaded") == "uploaded"]
        if not assets: continue
        asset = next((a for a in assets if "setup" in a["name"].lower()), assets[0])
        cand = {"version": (rel.get("tag_name") or "").lstrip("vV"),
                "url": asset["browser_download_url"], "filename": asset["name"],
                "page": rel.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases"}
        if best is None or _ver_tuple(cand["version"]) > _ver_tuple(best["version"]): best = cand
    if not best: raise Exception("aucune version téléchargeable trouvée sur GitHub")
    return best

def launch_installer(path):
    """Lance l'installateur Inno Setup en mode silencieux (ShellExecute gère l'élévation UAC)."""
    args = "/SILENT /CLOSEAPPLICATIONS /NORESTART /SUPPRESSMSGBOXES"
    if sys.platform == "win32":
        sh = ctypes.windll.shell32.ShellExecuteW
        sh.restype = ctypes.c_void_p
        r = sh(None, "open", path, args, os.path.dirname(path), 1)
        if (r or 0) <= 32: raise OSError(f"impossible de lancer l'installateur (code {r})")
    else:
        subprocess.Popen([path] + args.split())

# ── POPUP DE PROFIL ──────────────────────────────────────────────────────────
_OV_PAL = {   # teinte -> (ACC, FG, BG, CARD, FG3, BG3, BG4, BDR)
    "cuivre":     ("#cd9468", "#f3eee4", "#1c1b18", "#2a2823", "#8f8779", "#232220", "#37342e", "#3b372f"),
    "marine":     ("#6fa3ef", "#e8eefb", "#0e1629", "#1a2640", "#7a8cb2", "#131d34", "#243456", "#263554"),
    "anthracite": ("#55bfae", "#eceef0", "#181a1c", "#282b2f", "#868c95", "#1f2124", "#33373c", "#34373c"),
}
_img_cache = {}     # data URL (hash) -> image PIL décodée
_ov_cache = {}      # signature de la popup -> image rendue
_font_cache = {}

def _decode_data_url(url):
    if not url or not url.startswith("data:image"): return None
    k = hashlib.md5(url.encode("utf-8", "ignore")).hexdigest()
    im = _img_cache.get(k)
    if im is None:
        try:
            from PIL import Image as PI
            import io
            im = PI.open(io.BytesIO(base64.b64decode(url[url.index(",") + 1:]))).convert("RGBA")
            if max(im.size) > 512: im.thumbnail((512, 512), PI.LANCZOS)      # inutile de garder plus gros
        except Exception as e:
            log.warning(f"Popup : image illisible ({e})"); im = False
        if len(_img_cache) > 80: _img_cache.clear()
        _img_cache[k] = im
    return im or None

def _font(size, bold=False):
    key = (size, bold)
    if key not in _font_cache:
        from PIL import ImageFont
        names = (["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold else ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"])
        f = None
        for n in names:
            try: f = ImageFont.truetype(n, size); break
            except Exception: continue
        _font_cache[key] = f or ImageFont.load_default()
    return _font_cache[key]

def _emoji_img(ch, size):
    """Emoji couleur (Segoe UI Emoji) en image ; None si indisponible."""
    try:
        from PIL import Image as PI, ImageDraw, ImageFont
        f = ImageFont.truetype("seguiemj.ttf", 109)
        im = PI.new("RGBA", (160, 160), (0, 0, 0, 0)); ImageDraw.Draw(im).text((8, 8), ch, font=f, embedded_color=True)
        bb = im.getbbox()
        if not bb: return None
        im = im.crop(bb); im.thumbnail((size, size), PI.LANCZOS); return im
    except Exception: return None

def _rgba(h, a=255):
    h = h.lstrip("#"); return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)

def render_overlay(profile, ov, S=3):
    """Dessine toute la popup. Retourne (image RGBA avec ombre, largeur carte, hauteur carte, marge)."""
    from PIL import Image as PI, ImageDraw, ImageFilter, ImageOps
    pal = _OV_PAL.get(ov.get("tint", "cuivre"), _OV_PAL["cuivre"])
    ACC, FG, BG, CARD, FG3, BG3, BG4, BDR = [_rgba(c) for c in pal]
    CELL = max(32, min(100, int(ov.get("cell_size", 56)))); COLS = 4; GAP = 6; PAD = 14; HDR = 40; M = 18
    CW = COLS * CELL + (COLS - 1) * GAP + PAD * 2
    CH = HDR + PAD * 0 + 2 * CELL + GAP + 14 + CELL + PAD
    R = 20; RC = max(10, CELL // 4)
    s = lambda v: int(round(v * S))
    W, H = CW + 2 * M, CH + 2 * M
    img = PI.new("RGBA", (s(W), s(H)), (0, 0, 0, 0))
    # ombre portée
    sh = PI.new("RGBA", img.size, (0, 0, 0, 0)); ImageDraw.Draw(sh).rounded_rectangle([s(M), s(M + 5), s(M + CW), s(M + CH + 5)], radius=s(R), fill=(0, 0, 0, 120))
    img = PI.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(s(8))))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([s(M), s(M), s(M + CW) - 1, s(M + CH) - 1], radius=s(R), fill=BG, outline=BDR, width=max(1, S))

    def tile(x, y, fill, content=None, r=RC):
        """Tuile arrondie ; le contenu (image) est rogné aux mêmes coins pour ne jamais déborder du cadre."""
        box = [s(x), s(y), s(x + CELL) - 1, s(y + CELL) - 1]
        d.rounded_rectangle(box, radius=s(r), fill=fill)
        if content is not None:
            layer = PI.new("RGBA", img.size, (0, 0, 0, 0)); layer.paste(content, (box[0], box[1]))
            mask = PI.new("L", img.size, 0); ImageDraw.Draw(mask).rounded_rectangle(box, radius=s(r), fill=255)
            layer.putalpha(PI.composite(layer.getchannel("A"), PI.new("L", img.size, 0), mask))
            img.alpha_composite(layer)
        d.rounded_rectangle(box, radius=s(r), outline=BDR, width=max(1, S))

    def cover(im, size):   # remplit le carré (rognage centré), sans bande vide
        return ImageOps.fit(im, (size, size), PI.LANCZOS, centering=(.5, .5))

    # en-tête
    cy = M + HDR // 2
    d.ellipse([s(M + PAD), s(cy - 4), s(M + PAD + 8), s(cy + 4)], fill=ACC)
    d.text((s(M + PAD + 16), s(cy)), str(profile.get("name", "Profil")), font=_font(s(13), True), fill=FG, anchor="lm")
    d.text((s(M + CW - PAD), s(cy)), "PROFIL", font=_font(s(8), True), fill=ACC, anchor="rm")
    d.line([s(M + 1), s(M + HDR), s(M + CW - 1), s(M + HDR)], fill=BDR, width=max(1, S))

    # touches (image seule, sans nom)
    buttons = profile.get("buttons", {}); x0 = M + PAD; y0 = M + HDR + 10
    for i in range(8):
        b = buttons.get(str(i), {}); r_, c_ = divmod(i, COLS); x = x0 + c_ * (CELL + GAP); y = y0 + r_ * (CELL + GAP)
        photo = _decode_data_url(b.get("iconImage", ""))
        if photo is not None: tile(x, y, CARD, cover(photo, s(CELL)))
        else:
            tile(x, y, CARD)
            ic = _decode_data_url(b.get("iconRaster", ""))
            if ic is None and b.get("icon"): ic = _emoji_img(b["icon"], s(CELL * 0.55))
            if ic is not None:
                ic = ic.copy(); ic.thumbnail((s(CELL * 0.58), s(CELL * 0.58)), PI.LANCZOS)
                img.alpha_composite(ic, (s(x + CELL / 2) - ic.width // 2, s(y + CELL / 2) - ic.height // 2))
            else: d.ellipse([s(x + CELL / 2 - 4), s(y + CELL / 2 - 4), s(x + CELL / 2 + 4), s(y + CELL / 2 + 4)], fill=ACC)

    # séparateur + potards (image seule)
    ys = y0 + 2 * CELL + GAP + 7
    d.line([s(M + PAD), s(ys), s(M + CW - PAD), s(ys)], fill=BDR, width=max(1, S))
    pots = profile.get("pots", {}); ovp = ov.get("pots", {}); yp = ys + 7
    for i in range(COLS):
        p = pots.get(str(i), {}); x = x0 + i * (CELL + GAP)
        o = ovp.get(str(i), ovp.get(i, {})) if isinstance(ovp, dict) else {}
        photo = _decode_data_url(p.get("image", "") or (o.get("image", "") if isinstance(o, dict) else ""))
        if photo is not None: tile(x, yp, BG3, cover(photo, s(CELL)))
        else:
            tile(x, yp, BG3); cx, cy2, rr = x + CELL / 2, yp + CELL / 2, CELL * 0.30
            d.ellipse([s(cx - rr), s(cy2 - rr), s(cx + rr), s(cy2 + rr)], fill=BG4, outline=FG3, width=max(1, S))
            d.arc([s(cx - rr + 3), s(cy2 - rr + 3), s(cx + rr - 3), s(cy2 + rr - 3)], 135, 405, fill=BDR, width=s(2))
            d.arc([s(cx - rr + 3), s(cy2 - rr + 3), s(cx + rr - 3), s(cy2 + rr - 3)], 135, 300, fill=ACC, width=s(2))
            d.ellipse([s(cx - 2.5), s(cy2 - 2.5), s(cx + 2.5), s(cy2 + 2.5)], fill=ACC)
    return img.resize((W, H), PI.LANCZOS), CW, CH, M

def _ov_signature(profile, ov):
    return hashlib.md5(json.dumps([profile.get("name"), profile.get("buttons"), profile.get("pots"),
        {k: ov.get(k) for k in ("cell_size", "tint", "pots")}], sort_keys=True, default=str).encode("utf-8", "ignore")).hexdigest()

def get_overlay_image(profile, ov):
    sig = _ov_signature(profile, ov); r = _ov_cache.get(sig)
    if r is None:
        r = render_overlay(profile, ov)
        if len(_ov_cache) > 24: _ov_cache.clear()
        _ov_cache[sig] = r
    return r

def _layered_present(hwnd, im, x, y, alpha):
    """Affiche une image RGBA (alpha par pixel) dans une fenêtre en couches ; retourne True si OK."""
    from ctypes import wintypes
    w, h = im.size
    r, g, b, a = im.convert("RGBa").split()     # RGBa = alpha prémultiplié, requis par Windows
    from PIL import Image as PI
    raw = PI.merge("RGBA", (b, g, r, a)).tobytes()
    class BIH(ctypes.Structure): _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD), ("a", wintypes.LONG), ("b", wintypes.LONG), ("c", wintypes.DWORD), ("d", wintypes.DWORD)]
    class BLEND(ctypes.Structure): _fields_ = [("op", ctypes.c_ubyte), ("fl", ctypes.c_ubyte), ("sca", ctypes.c_ubyte), ("af", ctypes.c_ubyte)]
    class PT(ctypes.Structure): _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]
    u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
    u32.GetDC.restype = ctypes.c_void_p; g32.CreateCompatibleDC.restype = ctypes.c_void_p; g32.CreateDIBSection.restype = ctypes.c_void_p
    g32.SelectObject.restype = ctypes.c_void_p
    screen = u32.GetDC(None); mem = g32.CreateCompatibleDC(ctypes.c_void_p(screen))
    bih = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0); bits = ctypes.c_void_p()
    bmp = g32.CreateDIBSection(ctypes.c_void_p(screen), ctypes.byref(bih), 0, ctypes.byref(bits), None, 0)
    try:
        ctypes.memmove(bits, raw, len(raw)); old = g32.SelectObject(ctypes.c_void_p(mem), ctypes.c_void_p(bmp))
        sz = (wintypes.LONG * 2)(w, h); pos = PT(x, y); src = PT(0, 0); bl = BLEND(0, 0, max(0, min(255, int(alpha))), 1)
        u32.UpdateLayeredWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        ok = u32.UpdateLayeredWindow(hwnd, ctypes.c_void_p(screen), ctypes.byref(pos), sz, ctypes.c_void_p(mem), ctypes.byref(src), 0, ctypes.byref(bl), 2)
        g32.SelectObject(ctypes.c_void_p(mem), ctypes.c_void_p(old))
        return bool(ok)
    finally:
        g32.DeleteObject(ctypes.c_void_p(bmp)); g32.DeleteDC(ctypes.c_void_p(mem)); u32.ReleaseDC(None, ctypes.c_void_p(screen))

class ProfileOverlayWindow:
    """Popup de profil. Rendu : pré-calculé en arrière-plan puis simplement affiché (aucune latence)."""
    def __init__(self):
        self._queue = None; self._root = None; self._win = None; self._label = None; self._timer = None; self._fade = None
        self._ready = threading.Event(); self._job = None; self._jcv = threading.Condition(); self._layered_ok = sys.platform == "win32"
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._render_loop, daemon=True).start()
        self._ready.wait(timeout=5)

    # ── API ──────────────────────────────────────────────────────────────────
    def show_profile(self, profile: dict, ov_cfg: dict = None):
        with self._jcv: self._job = ("show", profile, ov_cfg or {}); self._jcv.notify()

    def prewarm(self, profiles: dict, ov_cfg: dict = None):
        """Pré-rend les popups de tous les profils (appelé au démarrage et à chaque sauvegarde)."""
        with self._jcv: self._job = ("warm", profiles, ov_cfg or {}); self._jcv.notify()

    # ── thread de rendu (jamais le thread d'interface) ─────────────────────────
    def _render_loop(self):
        while True:
            with self._jcv:
                while self._job is None: self._jcv.wait()
                kind, data, ov = self._job; self._job = None
            try:
                if kind == "warm":
                    for p in list(data.values()): get_overlay_image(p, ov)
                else:
                    im, cw, ch, m = get_overlay_image(data, ov)
                    if self._queue is not None: self._queue.put_nowait((im, cw, ch, m, ov))
            except Exception as e: log.warning(f"Popup : rendu impossible ({e})")

    # ── thread Tk ──────────────────────────────────────────────────────────────
    def _run(self):
        try:
            import tkinter as tk, queue as _queue
        except Exception as e:
            log.warning(f"Tkinter indisponible: {e}"); self._ready.set(); return
        self._queue = _queue.Queue()
        try: root = tk.Tk(); root.withdraw()
        except Exception as e:
            log.warning(f"Tkinter init échoué: {e}"); self._queue = None; self._ready.set(); return
        self._root = root; self._ready.set()
        def poll():
            try:
                item = None
                while True: item = self._queue.get_nowait()       # on ne garde que la plus récente
            except _queue.Empty: pass
            if item:
                try: self._display(*item)
                except Exception as e: log.warning(f"Popup affichage : {e}")
            root.after(15, poll)
        root.after(15, poll); root.mainloop()

    def _ensure_window(self):
        import tkinter as tk
        if self._win is not None:
            try:
                if self._win.winfo_exists(): return
            except Exception: pass
        win = tk.Toplevel(self._root); win.withdraw(); win.overrideredirect(True); win.attributes("-topmost", True)
        self._win = win; self._label = tk.Label(win, bd=0, highlightthickness=0); self._label.pack()
        self._hwnd = None

    def _display(self, im, cw, ch, m, ov):
        from PIL import ImageTk, Image as PI
        root = self._root; self._ensure_window(); win = self._win
        for t in (self._timer, self._fade):
            if t:
                try: root.after_cancel(t)
                except Exception: pass
        self._timer = self._fade = None
        DELAY = max(1, min(30, int(ov.get("delay", 3)))) * 1000
        ALPHA = max(0.2, min(1.0, int(ov.get("alpha", 97)) / 100))
        W, H = im.size; sw, sh = root.winfo_screenwidth(), root.winfo_screenheight(); mg = 20
        X, Y = {"br": (sw - cw - mg, sh - ch - 60), "bl": (mg, sh - ch - 60), "tr": (sw - cw - mg, mg + 40)}.get(ov.get("position", "br"), (mg, mg + 40))
        X, Y = X - m, Y - m
        layered = self._layered_ok and not ov.get("legacy")
        if layered:
            try:
                win.update_idletasks(); hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
                ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
                # en couches + fenêtre outil + sans activation + laisse passer les clics
                ctypes.windll.user32.SetWindowLongW(hwnd, -20, ex | 0x00080000 | 0x00000080 | 0x08000000 | 0x00000020)
                if not _layered_present(hwnd, im, X, Y, 0): raise OSError("UpdateLayeredWindow a échoué")
                ctypes.windll.user32.ShowWindow(hwnd, 4)             # SW_SHOWNOACTIVATE
                ctypes.windll.user32.SetWindowPos(hwnd, -1, X, Y, W, H, 0x0010)   # toujours au-dessus, sans prendre le focus
                steps = 5
                def fade(i=0, up=True):
                    a = ALPHA * 255 * (i / steps if up else 1 - i / steps)
                    _layered_present(hwnd, im, X, Y, a)
                    if i < steps: self._fade = root.after(14, lambda: fade(i + 1, up))
                    elif not up: ctypes.windll.user32.ShowWindow(hwnd, 0)
                fade(1, True)
                self._timer = root.after(DELAY, lambda: fade(0, False)); return
            except Exception as e:
                log.warning(f"Popup : mode transparent indisponible ({e}) -> mode compatibilité"); self._layered_ok = False
        # Mode compatibilité : image sur fond uni, coins arrondis par région Windows
        bg = _rgba(_OV_PAL.get(ov.get("tint", "cuivre"), _OV_PAL["cuivre"])[2])
        flat = PI.new("RGBA", im.size, bg); flat.alpha_composite(im); card = flat.crop((m, m, m + cw, m + ch)).convert("RGB")
        photo = ImageTk.PhotoImage(card); self._label.configure(image=photo); self._label.image = photo
        win.geometry(f"{cw}x{ch}+{X + m}+{Y + m}")
        try: win.attributes("-alpha", ALPHA)
        except Exception: pass
        win.deiconify(); win.lift()
        if sys.platform == "win32":
            try:
                win.update_idletasks(); hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
                rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, cw + 1, ch + 1, 40, 40); ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
            except Exception: pass
        self._timer = root.after(DELAY, win.withdraw)


# ── APP WATCHER ──────────────────────────────────────────────────────────────
class AppWatcher:
    def __init__(self, on_change):
        self._cb=on_change; self._cur=""; threading.Thread(target=self._loop,daemon=True).start()

    def _loop(self):
        while True:
            app=""
            if WIN32_OK:
                try:
                    hwnd=win32gui.GetForegroundWindow()
                    _,pid=win32process.GetWindowThreadProcessId(hwnd)
                    app=psutil.Process(pid).name().lower().replace(".exe","")
                except: pass
            if app!=self._cur: self._cur=app; self._cb(app)
            time.sleep(0.5)

# ── SERIAL ────────────────────────────────────────────────────────────────────
class Transport:
    """
    3 périphériques COM :
      slot 0 = ESP32  (macropad : trames de boutons/potards, envoi des LEDs)
      slot 1 = port USB 1, slot 2 = port USB 2
    Un slot 1/2 sans port configuré reste en surveillance passive (détection seule).
    Un slot 1/2 avec un port configuré ouvre vraiment la liaison COM.
    """
    LONG_MS=400; DOUBLE_MS=300
    N_SLOTS = 3

    def __init__(self, on_msg):
        self._cb       = on_msg
        self._slots    = [None] * self.N_SLOTS
        self._port_names = [None] * self.N_SLOTS
        self._btn_state  = {}
        self._baud       = [115200] * self.N_SLOTS
        # Démarrer le monitoring passif des ports USB 1 et 2
        threading.Thread(target=self._usb_monitor_loop, daemon=True).start()

    # ── Slot 0 (ESP32) ────────────────────────────────────────────────────────
    def start(self, port="AUTO", baud=115200, slot=0):
        if not SERIAL_OK: return
        old = self._slots[slot]
        if old:
            try: old.close()
            except: pass
        self._slots[slot] = None; self._port_names[slot] = None
        if port == "AUTO":
            ports = serial.tools.list_ports.comports()
            # Éviter un port déjà OUVERT par un autre slot (un simple port détecté ne compte pas)
            used  = {self._port_names[i] for i in range(self.N_SLOTS) if i != slot and self._slots[i] is not None and self._port_names[i]}
            # Préférer les puces connues ESP32
            cands = [p.device for p in ports
                     if any(k in p.description.upper() for k in ["CP210","CH340","FTDI","USB SERIAL"])
                     and p.device not in used]
            if not cands:
                cands = [p.device for p in ports if p.device not in used]
            port = cands[0] if cands else None
        if not port: return
        try:
            ser = serial.Serial(port, baud, timeout=0.02)
            self._slots[slot] = ser; self._port_names[slot] = port; self._baud[slot] = baud
            threading.Thread(target=self._loop, args=(ser, slot), daemon=True).start()
            log.info(f"Serial slot {slot}: {port} @ {baud}")
        except Exception as e:
            log.error(f"Serial slot {slot}: {e}")
        self._refresh_passive()

    def stop(self, slot):
        """Ferme la liaison COM d'un slot (sans toucher à la configuration)."""
        ser = self._slots[slot]
        self._slots[slot] = None; self._port_names[slot] = None
        if ser:
            try: ser.close()
            except: pass
        self._refresh_passive()

    def is_connected(self, slot=0):
        s = self._slots[slot]; return bool(s and s.is_open)

    def _loop(self, ser, slot):
        """Lecture série en continu : on lit tout ce qui arrive et on traite chaque ligne aussitôt."""
        buf = b""; errors = 0
        while ser and ser.is_open:
            try:
                chunk = ser.read(max(1, ser.in_waiting))
                errors = 0
                if not chunk: continue
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.decode("utf-8", errors="ignore").strip()
                    if line: self._cb(line, slot)
                if len(buf) > 4096: buf = b""
            except Exception:
                errors += 1
                if errors > 20:
                    try: ser.close()
                    except Exception: pass
                    break
                time.sleep(0.05)
        if self._slots[slot] is ser:
            self._slots[slot] = None; self._port_names[slot] = None

    def send_raw(self, line: str, slot=0):
        """Envoie une trame série — uniquement slot 0 (ESP32)."""
        ser = self._slots[slot]
        if ser and ser.is_open:
            try: ser.write((line + "\n").encode())
            except: pass

    # ── Slots 1 & 2 (USB passifs) ────────────────────────────────────────────
    def _usb_monitor_loop(self):
        """Détecte les ports COM présents et attribue les ports libres aux slots 1 et 2
        qui ne sont pas ouverts. Ne lit aucune trame et ne bloque pas."""
        if not SERIAL_OK: return
        while True:
            self._refresh_passive()
            time.sleep(2)

    def _refresh_passive(self):
        try:
            present = [p.device for p in serial.tools.list_ports.comports()]
            used    = {self._port_names[i] for i in range(self.N_SLOTS) if self._slots[i] is not None}
            free    = [d for d in present if d not in used]
            for k, slot in enumerate([x for x in (1, 2) if self._slots[x] is None]):
                self._port_names[slot] = free[k] if k < len(free) else None
        except: pass

    def usb_info(self, slot):
        """Infos du port USB 1 ou 2 : détecté (passif) ou liaison COM ouverte."""
        empty = {"connected": False, "opened": False, "port": None, "description": "", "speed": "", "baud": 0}
        if not SERIAL_OK or slot not in (1, 2): return empty
        port = self._port_names[slot]
        if not port: return empty
        opened = self.is_connected(slot)
        desc = ""; speed = ""
        try:
            info = next((p for p in serial.tools.list_ports.comports() if p.device == port), None)
            if info:
                desc = info.description or ""
                dl = desc.lower()
                if   "super" in dl or "3.0" in dl or "3.1" in dl or "3.2" in dl: speed = "SuperSpeed (5-20 Gb/s)"
                elif "hi"   in dl or "2.0" in dl:                               speed = "Hi-Speed (480 Mb/s)"
                elif "full" in dl or "1.1" in dl:                               speed = "Full-Speed (12 Mb/s)"
                elif "low"  in dl or "1.0" in dl:                               speed = "Low-Speed (1.5 Mb/s)"
        except: pass
        return {"connected": True, "opened": opened, "port": port, "description": desc, "speed": speed,
                "baud": self._baud[slot] if opened else 0}

    # ── Timing boutons (slot 0 uniquement) ───────────────────────────────────
    def _handle_timing(self, btn_idx, event, on_msg_fn):
        now = time.time(); st = self._btn_state.setdefault(btn_idx, {})
        if event == "on":
            last_on = st.get("last_on"); st["on_t"] = now; st["last_on"] = now
            if last_on and (now-last_on)*1000 < self.DOUBLE_MS:
                st["last_on"] = None; on_msg_fn(btn_idx, "double_click")
        elif event == "off":
            on_t = st.get("on_t")
            if on_t is None: return
            st["on_t"] = None
            on_msg_fn(btn_idx, "long_press" if (now-on_t)*1000 >= self.LONG_MS else "press")

# ── PLUGINS ────────────────────────────────────────────────────────────────────
class PluginManager:
    def __init__(self):
        self.plugins=[]; self.actions={}; self.reload()

    def _plugins_dir(self):
        d=Path(_app_dir_persistent())/"plugins"; d.mkdir(exist_ok=True); return d

    def reload(self):
        self.plugins=[]; self.actions={}
        for f in sorted(self._plugins_dir().glob("*.json")):
            try:
                with open(f,"r",encoding="utf-8") as fp: manifest=json.load(fp)
                manifest["_file"]=f.name; self.plugins.append(manifest)
                for act in manifest.get("actions",[]):
                    if act.get("type"): self.actions[act["type"]]=act
                log.info(f"Plugin: {manifest.get('name',f.name)}")
            except Exception as e: log.error(f"Plugin {f.name}: {e}")

    def catalog(self):
        out=[]
        for p in self.plugins:
            for act in p.get("actions",[]):
                out.append({"cat":"Plugins","icon":act.get("icon","🧩"),"type":act.get("type"),
                    "name":act.get("name","Action plugin"),"desc":act.get("desc",p.get("name","")),
                    "params":act.get("params",[]),"plugin":p.get("name",p.get("_file",""))})
        return out

    def run(self, action_type, params, value=None):
        act=self.actions.get(action_type)
        if not act: return
        run_def=act.get("run",{}); kind=run_def.get("kind","shell")
        def _sub(s):
            if not isinstance(s,str): return s
            for k,v in params.items(): s=s.replace("{"+k+"}",str(v))
            if value is not None: s=s.replace("{value}",str(value))
            return s
        try:
            if kind=="shell":
                cmd=_sub(run_def.get("command",""))
                if cmd: run_silent(cmd)
            elif kind=="powershell":
                cmd=_sub(run_def.get("command",""))
                if cmd: run_hidden(["powershell","-NoProfile","-Command",cmd],creationflags=CREATE_NO_WINDOW)
            elif kind=="http":
                import urllib.request
                url=_sub(run_def.get("url","")); body=run_def.get("body")
                req=urllib.request.Request(url,method=run_def.get("method","GET"))
                if body:
                    req.data=json.dumps(json.loads(_sub(json.dumps(body)))).encode()
                    req.add_header("Content-Type","application/json")
                with urllib.request.urlopen(req,timeout=10) as r: log.info(f"Plugin HTTP {r.status}")
        except Exception as e: log.error(f"Plugin '{action_type}': {e}")

# ── MACRODECK CORE ────────────────────────────────────────────────────────────
SERIAL_KEYS = (("serial_port","baud_rate"), ("serial_port2","baud_rate2"), ("serial_port3","baud_rate3"))

class MacroDeck:
    def __init__(self):
        self.cfg=ConfigManager(); self.ws_clients=set(); self._loop=None; self._pot_disp={}
        self.plugins=PluginManager()
        self.engine=ActionEngine(self.cfg,self._broadcast,self.plugins)
        self.metrics=Metrics(); self.transport=Transport(self._on_esp32)
        self.watcher=AppWatcher(self._on_app); self.overlay=ProfileOverlayWindow()

    def _broadcast(self, obj):
        if obj.get("type")=="profile_changed":
            key=obj.get("profile")
            profile=self.cfg.data.get("profiles",{}).get(key)
            if profile and self.overlay:
                self.overlay.show_profile(profile,self._ov_cfg())
        raw=json.dumps(obj)
        loop=self._loop
        if loop and loop.is_running():
            for ws in list(self.ws_clients):
                loop.call_soon_threadsafe(asyncio.ensure_future, ws.send(raw))
        else:
            for ws in list(self.ws_clients):
                try: asyncio.ensure_future(ws.send(raw))
                except: pass

    def _on_app(self, app):
        for name,profile in self.cfg.data["profiles"].items():
            trigger=(profile.get("app_trigger") or "").lower()
            if trigger and trigger in app.lower() and self.cfg.data.get("active_profile")!=name:
                self.cfg.data["active_profile"]=name
                self._broadcast({"type":"profile_changed","profile":name})
                return

    def _ov_cfg(self):
        """Config de la popup de profil + teinte de l'interface."""
        return {**self.cfg.data.get("overlay", {}), "tint": self.cfg.data.get("tint", "cuivre")}

    def _dispatch_button(self, idx, ev):
        actions = self.cfg.active()["buttons"].get(str(idx), {}).get(ev, [])
        if actions: threading.Thread(target=self.engine.run, args=(actions,), daemon=True).start()
        self._broadcast({"type": "button_event", "button": idx, "event": ev})

    def _on_esp32(self, raw: str, slot: int = 0):
        if slot: self._broadcast({"type": "serial_line", "slot": slot, "line": raw})
        fr = parse_frame(raw)
        if not fr: return
        kind, idx, val = fr
        pot_cfg = self.cfg.active()["pots"].get(str(idx), {})
        if kind == "pot":
            self.engine.pot_push(idx, pot_cfg, "abs", val)
            self._broadcast({"type": "pot_event", "pot": idx, "value": val})
        elif kind == "enc":
            self.engine.pot_push(idx, pot_cfg, "rel", val)
            disp = max(0, min(100, self._pot_disp.get(idx, 50) + val * 2)); self._pot_disp[idx] = disp
            self._broadcast({"type": "pot_event", "pot": idx, "value": disp})
        elif val:   # bouton enfoncé
            self._dispatch_button(idx, "down")
            self.transport._handle_timing(idx, "on", self._dispatch_button)
        else:       # bouton relâché
            self._dispatch_button(idx, "up")
            self.transport._handle_timing(idx, "off", self._dispatch_button)
            self._broadcast({"type": "button_event", "button": idx, "event": "release"})

    async def _metrics_loop(self):
        # collect() interroge WMI / nvidia-smi : il tourne dans un thread, jamais dans la boucle
        # d'événements (avant, elle était bloquée chaque seconde et retardait tous les messages).
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.metrics.collect)
        while True:
            await asyncio.sleep(1)
            m = await loop.run_in_executor(None, self.metrics.collect); self._broadcast({"type": "metrics", "data": m})
            keys = ["cpu", "ram", "gpu_usage", "ssd_usage"]
            for i in range(4):
                k = self.cfg.data.get("led_strips", {}).get(str(i), {}).get("metric", keys[i])
                self.transport.send_raw(f"L{i}:{min(100,int(float(m.get(k,0) or 0)))}")

    async def _ws_handler(self, ws: WebSocketServerProtocol):
        self.ws_clients.add(ws)
        await ws.send(json.dumps({"type":"config","data":self.cfg.data}))
        await ws.send(json.dumps({"type":"app_info","version":APP_VERSION}))
        try:
            async for raw in ws: await self._handle(json.loads(raw),ws)
        except: pass
        finally: self.ws_clients.discard(ws)

    def _serial_status(self):
        return {
            "type": "serial_status",
            # Slot 0 — ESP32
            "connected": self.transport.is_connected(0),
            "port":      self.transport._port_names[0],
            "baud":      self.transport._baud[0] if self.transport.is_connected(0) else 0,
            # Slots 1 & 2 — USB passifs (clés USB, hubs…)
            "usb1": self.transport.usb_info(1),
            "usb2": self.transport.usb_info(2),
        }

    def _plugins_msg(self):
        return {"type":"plugins","data":self.plugins.catalog(),
            "meta":[{"name":p.get("name"),"file":p.get("_file"),"version":p.get("version","")} for p in self.plugins.plugins]}

    # ── Mise à jour ─────────────────────────────────────────────────────────
    def _check_update(self, silent=False):
        try:
            rel = fetch_latest_release()
        except Exception as e:
            if not silent: self._broadcast({"type":"toast","message":f"⚠ MAJ impossible : {e}"})
            return
        if _ver_tuple(rel["version"]) > _ver_tuple(APP_VERSION):
            self._broadcast({"type":"update_available","current":APP_VERSION,"latest":rel["version"],
                             "download_url":rel["url"],"filename":rel["filename"],"page":rel["page"]})
        elif not silent:
            self._broadcast({"type":"toast","message":f"✓ Imperium {APP_VERSION} est à jour"})

    def _install_update(self, url, filename, page):
        if getattr(self, "_updating", False): return
        self._updating = True
        def prog(status, pct=0):
            self._broadcast({"type":"update_progress","status":status,"pct":pct,"filename":filename})
        try:
            import urllib.request
            if not url:
                rel = fetch_latest_release(); url, filename, page = rel["url"], rel["filename"], rel["page"]
            folder = os.path.join(tempfile.gettempdir(), "ImperiumUpdate"); os.makedirs(folder, exist_ok=True)
            dest = os.path.join(folder, filename); part = dest + ".part"
            for f in (dest, part):
                try: os.remove(f)
                except OSError: pass
            prog("downloading", 0)
            req = urllib.request.Request(url, headers={"User-Agent":"Imperium-updater","Accept":"application/octet-stream"})
            with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as out:
                total = int(r.headers.get("Content-Length") or 0); done = 0; last = -1
                while True:
                    chunk = r.read(65536)
                    if not chunk: break
                    out.write(chunk); done += len(chunk)
                    if total:
                        pct = min(99, done * 100 // total)
                        if pct != last: last = pct; prog("downloading", pct)
            if total and done != total: raise IOError(f"téléchargement incomplet ({done}/{total} octets)")
            with open(part, "rb") as f:
                if f.read(2) != b"MZ": raise IOError("le fichier téléchargé n'est pas un installateur valide")
            os.replace(part, dest)
            prog("launching", 100)
            launch_installer(dest)
            time.sleep(1.5); os._exit(0)
        except Exception as e:
            log.error(f"Mise à jour: {e}")
            self._updating = False
            self._broadcast({"type":"update_progress","error":str(e),"page":page or f"https://github.com/{GITHUB_REPO}/releases"})

    async def _handle(self, msg, ws):
        t=msg.get("type")

        if t=="save_config":
            self.cfg.data=msg["data"]; self.cfg.save()
            if self.overlay: self.overlay.prewarm(self.cfg.data.get("profiles",{}),self._ov_cfg())
            await ws.send(json.dumps({"type":"config_saved"}))
        elif t=="create_profile":
            name=msg.get("key",""); label=msg.get("label","Nouveau profil")
            if name and name not in self.cfg.data["profiles"]:
                self.cfg.data["profiles"][name]=empty_profile(label); self.cfg.save()
                self._broadcast({"type":"config","data":self.cfg.data})
        elif t=="delete_profile":
            name=msg.get("key","")
            if name in self.cfg.data["profiles"] and name!="default":
                del self.cfg.data["profiles"][name]
                if self.cfg.data.get("active_profile")==name: self.cfg.data["active_profile"]="default"
                self.cfg.save(); self._broadcast({"type":"config","data":self.cfg.data})
        elif t=="rename_profile":
            name=msg.get("key",""); label=msg.get("label","")
            if name in self.cfg.data["profiles"] and label:
                self.cfg.data["profiles"][name]["name"]=label; self.cfg.save()
                self._broadcast({"type":"config","data":self.cfg.data})
        elif t=="set_profile":
            name=msg.get("profile","")
            if name in self.cfg.data["profiles"]:
                self.cfg.data["active_profile"]=name; self.cfg.save()
                self._broadcast({"type":"profile_changed","profile":name})
        elif t=="test_action":
            threading.Thread(target=self.engine.run,args=(msg.get("actions",[]),),daemon=True).start()
        elif t=="get_ports":
            ports=[p.device for p in serial.tools.list_ports.comports()] if SERIAL_OK else []
            await ws.send(json.dumps({"type":"ports","data":ports}))
        elif t=="get_apps":
            async def _do():
                apps=await asyncio.get_event_loop().run_in_executor(None,get_installed_apps)
                await ws.send(json.dumps({"type":"apps","data":apps}))
            asyncio.ensure_future(_do())
        elif t=="get_plugins":
            await ws.send(json.dumps(self._plugins_msg()))
        elif t=="reload_plugins":
            self.plugins.reload(); await ws.send(json.dumps(self._plugins_msg()))
            self._broadcast({"type":"toast","message":f"✓ {len(self.plugins.plugins)} plugin(s) chargé(s)"})
        elif t=="get_processes":
            seen=set(); procs=[]
            for p in psutil.process_iter(["name"]):
                try:
                    n=p.info["name"]
                    if n and n.lower() not in seen: seen.add(n.lower()); procs.append(n)
                except: pass
            await ws.send(json.dumps({"type":"processes","data":sorted(procs)}))
        elif t=="get_audio_sessions":
            await ws.send(json.dumps({"type":"audio_sessions","data":list_audio_sessions()}))
        elif t in ("pick_folder","pick_file"):
            field=msg.get("field","")
            async def _do():
                path=await asyncio.get_event_loop().run_in_executor(None,self._native_picker,t=="pick_folder")
                await ws.send(json.dumps({"type":"picked_path","field":field,"path":path}))
            asyncio.ensure_future(_do())
        elif t=="connect_serial":
            slot=max(0,min(2,int(msg.get("slot",0))))
            pk,bk=SERIAL_KEYS[slot]
            port=msg.get("port","AUTO" if slot==0 else ""); baud=int(msg.get("baud",115200))
            self.cfg.data[pk]=port; self.cfg.data[bk]=baud; self.cfg.save()
            self.transport.start(port,baud=baud,slot=slot)
            await asyncio.sleep(0.3)
            await ws.send(json.dumps(self._serial_status()))
        elif t=="disconnect_serial":
            self.transport.stop(max(0,min(2,int(msg.get("slot",0)))))
            await ws.send(json.dumps(self._serial_status()))
        elif t=="get_serial_status":
            await ws.send(json.dumps(self._serial_status()))
        elif t=="simulate_esp32_frame":
            raw=msg.get("raw","").strip(); fr=parse_frame(raw)
            if not fr:
                await ws.send(json.dumps({"type":"protocol_test_result","ok":False,
                    "error":f"Trame non reconnue : \"{raw}\" — formats attendus : B0:1, B0:0, P0:50"})); return
            self._on_esp32(raw)
            kind,idx,val=fr
            what=(f"Bouton {idx+1} {'enfoncé' if val else 'relâché'}" if kind=="btn" else f"Potard {idx+1} = {val}%")
            await ws.send(json.dumps({"type":"protocol_test_result","ok":True,"message":what}))
        elif t=="obs_test":
            def _t(): self.engine.obs.close(); self._broadcast({"type":"obs_status",**self.engine.obs.ping()})
            threading.Thread(target=_t,daemon=True).start()
        elif t=="discord_connect":
            def _d():
                try:
                    self.engine.discord.connect(interactive=True)
                    self._broadcast({"type":"discord_status","ok":True,"user":(self.engine.discord.user or {}).get("username","")})
                except Exception as e:
                    self.engine.discord.error=str(e); self._broadcast({"type":"discord_status","ok":False,"error":str(e)})
            threading.Thread(target=_d,daemon=True).start()
        elif t=="discord_status":
            d=self.engine.discord
            await ws.send(json.dumps({"type":"discord_status","ok":d.ready,"user":(d.user or {}).get("username",""),"error":d.error}))
        elif t=="discord_users":
            def _u():
                try: self._broadcast({"type":"discord_users","ok":True,"data":self.engine.discord.users()})
                except Exception as e: self._broadcast({"type":"discord_users","ok":False,"error":str(e)})
            threading.Thread(target=_u,daemon=True).start()
        elif t=="set_tint":
            tint=msg.get("tint","cuivre")
            if tint in ("cuivre","marine","anthracite"):
                self.cfg.data["tint"]=tint; self.cfg.save()
        elif t=="check_update":
            threading.Thread(target=self._check_update,args=(bool(msg.get("silent")),),daemon=True).start()
        elif t=="install_update":
            threading.Thread(target=self._install_update,
                args=(msg.get("download_url",""),msg.get("filename","Imperium_Setup.exe"),msg.get("page","")),daemon=True).start()
        elif t=="open_external":
            open_url_default(msg.get("url",""))
        elif t=="preview_overlay":
            key=self.cfg.data.get("active_profile","default")
            profile=self.cfg.data.get("profiles",{}).get(key)
            if profile and self.overlay:
                self.overlay.show_profile(profile,self._ov_cfg())
        elif t=="update_button":
            pid=msg.get("profile","default"); bid=str(msg.get("button",0))
            if pid in self.cfg.data["profiles"]:
                self.cfg.data["profiles"][pid]["buttons"][bid]=msg.get("data",{}); self.cfg.save()

    def _native_picker(self, folder: bool) -> str:
        try:
            if folder:
                ps=("Add-Type -AssemblyName System.Windows.Forms;"
                    "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
                    "if($f.ShowDialog() -eq 'OK'){Write-Output $f.SelectedPath}")
            else:
                ps=("Add-Type -AssemblyName System.Windows.Forms;"
                    "$f=New-Object System.Windows.Forms.OpenFileDialog;"
                    "if($f.ShowDialog() -eq 'OK'){Write-Output $f.FileName}")
            return subprocess.run(["powershell","-NoProfile","-Command",ps],
                capture_output=True,text=True,timeout=120).stdout.strip()
        except Exception as e: log.error(f"picker: {e}"); return ""

    async def run(self):
        self._loop = asyncio.get_event_loop()
        if self.overlay: self.overlay.prewarm(self.cfg.data.get("profiles",{}),self._ov_cfg())
        for slot,(pk,bk) in enumerate(SERIAL_KEYS):
            port=self.cfg.data.get(pk,"AUTO" if slot==0 else "")
            if port: self.transport.start(port,baud=int(self.cfg.data.get(bk,115200)),slot=slot)
        srv=await websockets.serve(self._ws_handler,"localhost",WS_PORT,max_size=10*1024*1024)
        await asyncio.gather(self._metrics_loop(),srv.wait_closed())

# ── HTTP SERVER ───────────────────────────────────────────────────────────────
def _app_dir():
    if getattr(sys,"frozen",False): return getattr(sys,"_MEIPASS",os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

def _app_dir_persistent():
    if getattr(sys,"frozen",False): return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def _http_server():
    import http.server, socketserver
    class Q(http.server.SimpleHTTPRequestHandler):
        def __init__(self,*a,**kw): super().__init__(*a,directory=_app_dir(),**kw)
        def log_message(self,*a): pass
        def end_headers(self):
            self.send_header("Cache-Control","no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma","no-cache"); self.send_header("Expires","0")
            super().end_headers()
    try:
        with socketserver.TCPServer(("127.0.0.1",HTTP_PORT),Q) as h: h.serve_forever()
    except OSError: pass

def _port_is_free(port):
    import socket as _s
    s=_s.socket(_s.AF_INET,_s.SOCK_STREAM)
    try: s.bind(("127.0.0.1",port)); s.close(); return True
    except OSError: s.close(); return False

def _notify_already_running():
    if sys.platform=="win32":
        try:
            ctypes.windll.user32.MessageBoxW(0,
                "Imperium est déjà lancé.\n\nVérifie la barre des tâches.",
                "Imperium — déjà en cours",0x40|0x1000)
        except: pass

def _wait_for_http():
    import urllib.request
    url = f"http://127.0.0.1:{HTTP_PORT}/gui.html"
    for _ in range(40):
        try: urllib.request.urlopen(url, timeout=0.5); return
        except: time.sleep(0.25)

def _run_backend(deck):
    try:
        asyncio.run(deck.run())
    except KeyboardInterrupt: pass
    except OSError: _notify_already_running()
    finally:
        try: deck.cfg.save()
        except: pass
    os._exit(0)

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__=="__main__":
    if not _port_is_free(WS_PORT):
        _notify_already_running(); sys.exit(0)

    threading.Thread(target=_http_server, daemon=True).start()
    deck = MacroDeck()
    threading.Thread(target=_run_backend, args=(deck,), daemon=True).start()

    # webview.start() DOIT etre sur le thread principal (contrainte COM Windows)
    _wait_for_http()
    try:
        import webview
        webview.create_window(
            "Imperium",
            f"http://127.0.0.1:{HTTP_PORT}/gui.html",
            width=820, height=680, min_size=(640, 480)
        )
        webview.start(gui="edgechromium")
        try: deck.cfg.save()
        except: pass
        os._exit(0)
    except Exception as e:
        log.warning(f"pywebview indisponible ({e}), fallback navigateur")
        try: webbrowser.open(f"http://127.0.0.1:{HTTP_PORT}/gui.html")
        except: pass
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt: pass
        finally:
            try: deck.cfg.save()
            except: pass
