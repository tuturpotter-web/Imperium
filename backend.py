"""
Imperium Backend
- Console cachée (SW_HIDE + FreeConsole)
- Fenêtre native via pywebview (Edge WebView2)
- Profils indépendants avec création/suppression/renommage
- WebSocket :8765 — HTTP :8766
"""
APP_VERSION = "dev"
import sys, os, ctypes, threading, time, asyncio, json, subprocess, re
import webbrowser, shutil, glob, logging, datetime, tempfile, base64
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
def _vif():
    if not PYCAW_OK: return None
    try:
        d = AudioUtilities.GetSpeakers()
        return d.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)
    except: return None

def get_volume():
    try: v=_vif(); return int(v.GetMasterVolumeLevelScalar()*100) if v else 0
    except: return 0

def set_volume(lv):
    try: v=_vif(); v and v.SetMasterVolumeLevelScalar(max(0,min(100,lv))/100.0, None)
    except: pass

def get_mute():
    try: v=_vif(); return bool(v.GetMute()) if v else False
    except: return False

def set_mute(s):
    try: v=_vif(); v and v.SetMute(s, None)
    except: pass

def set_app_volume(process_name: str, level: int):
    if not PYCAW_OK or not process_name: return
    try:
        from pycaw.pycaw import ISimpleAudioVolume
        target = process_name.lower().replace(".exe","")
        for session in AudioUtilities.GetAllSessions():
            if session.Process and session.Process.name().lower().replace(".exe","") == target:
                session._ctl.QueryInterface(ISimpleAudioVolume).SetMasterVolume(max(0,min(100,level))/100.0, None)
    except Exception as e: log.error(f"set_app_volume: {e}")

def list_audio_sessions():
    out, seen = [], set()
    if not PYCAW_OK: return out
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.Process:
                n = s.Process.name()
                if n.lower() not in seen: seen.add(n.lower()); out.append(n)
    except: pass
    return sorted(out)

# ── ACTIONS SUPPLÉMENTAIRES ──────────────────────────────────────────────────
# Raccourcis clavier prédéfinis : type d'action -> touches envoyées
SHORTCUTS = {
    "win_maximize": "win+up",
    "win_restore": "win+down",
    "win_snap_left": "win+left",
    "win_snap_right": "win+right",
    "win_switch": "alt+tab",
    "win_task_view": "win+tab",
    "win_close": "alt+f4",
    "desktop_next": "ctrl+win+right",
    "desktop_prev": "ctrl+win+left",
    "desktop_new": "ctrl+win+d",
    "desktop_close": "ctrl+win+f4",
    "win_explorer": "win+e",
    "win_run": "win+r",
    "win_settings": "win+i",
    "win_search": "win+s",
    "win_task_manager": "ctrl+shift+esc",
    "win_emoji": "win+.",
    "win_clipboard": "win+v",
    "win_notifications": "win+n",
    "win_quick_settings": "win+a",
    "win_widgets": "win+w",
    "win_project": "win+p",
    "win_voice_typing": "win+h",
    "game_bar": "win+g",
    "game_record_toggle": "win+alt+r",
    "game_record_last": "win+alt+g",
    "game_screenshot": "win+alt+print screen",
    "nvidia_overlay": "alt+z",
    "nvidia_replay_save": "alt+f10",
    "steam_overlay": "shift+tab",
    "browser_new_tab": "ctrl+t",
    "browser_close_tab": "ctrl+w",
    "browser_reopen_tab": "ctrl+shift+t",
    "browser_next_tab": "ctrl+tab",
    "browser_prev_tab": "ctrl+shift+tab",
    "browser_refresh": "f5",
    "browser_hard_refresh": "ctrl+f5",
    "browser_back": "alt+left",
    "browser_forward": "alt+right",
    "browser_private": "ctrl+shift+n",
    "browser_devtools": "f12",
    "browser_fullscreen": "f11",
    "browser_address": "ctrl+l",
    "browser_bookmark": "ctrl+d",
    "browser_find": "ctrl+f",
    "edit_copy": "ctrl+c",
    "edit_cut": "ctrl+x",
    "edit_paste": "ctrl+v",
    "edit_paste_plain": "ctrl+shift+v",
    "edit_undo": "ctrl+z",
    "edit_redo": "ctrl+y",
    "edit_select_all": "ctrl+a",
    "edit_save": "ctrl+s",
    "edit_new": "ctrl+n",
    "edit_open": "ctrl+o",
    "edit_print": "ctrl+p",
    "edit_enter": "enter",
    "edit_escape": "esc",
    "edit_delete": "delete",
    "edit_tab": "tab",
    "video_play": "k",
    "video_mute": "m",
    "video_fullscreen": "f",
    "video_captions": "c",
    "video_seek_back": "j",
    "video_seek_fwd": "l",
    "video_next": "shift+n"
}

# Requêtes OBS (obs-websocket v4, port 4444)
OBS_SIMPLE = {
    "obs_stream_toggle": "StartStopStreaming", "obs_record_toggle": "StartStopRecording",
    "obs_record_pause": "PauseRecording", "obs_record_resume": "ResumeRecording",
    "obs_replay_save": "SaveReplayBuffer", "obs_replay_toggle": "StartStopReplayBuffer",
    "obs_studio_mode": "ToggleStudioMode", "obs_transition": "TransitionToProgram",
    "obs_virtualcam_toggle": "StartStopVirtualCam",
}

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

def toggle_app_mute(process_name: str):
    if not PYCAW_OK or not process_name: return
    try:
        from pycaw.pycaw import ISimpleAudioVolume
        target = process_name.lower().replace(".exe", "")
        vols = [s._ctl.QueryInterface(ISimpleAudioVolume) for s in AudioUtilities.GetAllSessions()
                if s.Process and s.Process.name().lower().replace(".exe", "") == target]
        if not vols: return
        new = not bool(vols[0].GetMute())
        for v in vols: v.SetMute(new, None)
    except Exception as e: log.error(f"toggle_app_mute: {e}")

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
    "serial_port": "AUTO", "theme": "dark",
    "serial_port2": "", "baud_rate": 115200, "baud_rate2": 115200
}

# ── TRAMES ESP32 ─────────────────────────────────────────────────────────────
# Une trame = une ligne de texte. Format unique et fixe :
#   ESP32 -> PC   B<n>:1        bouton n enfoncé       (n = 0..7)
#                 B<n>:0        bouton n relâché
#                 P<n>:<0-100>  potentiomètre n        (n = 0..3)
#   PC -> ESP32   L<n>:<0-100>  niveau de la LED n     (n = 0..3)
# Appui long et double clic sont détectés par l'application (pas par l'ESP32).
# L'ancien format (btn0:on / btn0:off / pot0:50) reste accepté.
_RX_BTN = re.compile(r"^(?:btn|b)(\d+)\s*[:=]\s*(1|0|on|off)$", re.I)
_RX_POT = re.compile(r"^(?:pot|p)(\d+)\s*[:=]\s*(\d+)$", re.I)

def parse_frame(raw: str):
    """('btn', index, enfoncé: bool) | ('pot', index, valeur 0-100) | None"""
    raw = (raw or "").strip()
    m = _RX_BTN.match(raw)
    if m: return ("btn", int(m.group(1)), m.group(2).lower() in ("1", "on"))
    m = _RX_POT.match(raw)
    if m: return ("pot", int(m.group(1)), max(0, min(100, int(m.group(2)))))
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
class ActionEngine:
    def __init__(self, cfg: ConfigManager, broadcast_fn, plugins=None):
        self.cfg = cfg; self.broadcast = broadcast_fn; self.plugins = plugins

    def run(self, actions: list):
        for a in actions:
            try: self._one(a)
            except Exception as e: log.error(f"Action {a.get('type')}: {e}")

    def run_pot(self, pot_cfg: dict, val: int):
        action = pot_cfg.get("action","volume_system")
        try:
            if   action == "volume_system":  set_volume(val)
            elif action == "volume_app":     set_app_volume(pot_cfg.get("app",""), val)
            elif action == "discord_volume": set_app_volume("Discord", val)
            elif action == "spotify_volume": set_app_volume("Spotify", val)
            elif action == "game_volume":    set_app_volume(pot_cfg.get("app",""), val)
            elif action == "mic_volume":     set_app_volume(pot_cfg.get("app","") or "Discord", val)
            elif action == "brightness":
                run_hidden(["powershell","-Command",
                    f"(Get-WmiObject -NS root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{val})"],
                    creationflags=CREATE_NO_WINDOW)
            elif action == "obs_volume":
                self._obs_vol(pot_cfg.get("source","Mic/Aux"), val)
            elif action in ("scroll","zoom_level","media_seek","playback_speed"):
                last = pot_cfg.get("_last", 50); delta = val - last; pot_cfg["_last"] = val
                if not delta: return
                if   action == "scroll":        mouse.wheel(delta/8)
                elif action == "zoom_level":    keyboard.press("ctrl"); mouse.wheel(delta/10); keyboard.release("ctrl")
                elif action == "media_seek":
                    if delta > 2: keyboard.send("right")
                    elif delta < -2: keyboard.send("left")
                elif action == "playback_speed":
                    if delta > 3: keyboard.send("shift+.")
                    elif delta < -3: keyboard.send("shift+,")
            elif action == "led_strip_color":
                self.broadcast({"type":"led_strip_set","strip":pot_cfg.get("strip",0),"value":val})
            elif action == "custom":
                code = pot_cfg.get("script","")
                if code: exec(code, {"value": val})
            elif self.plugins and action in self.plugins.actions:
                params = {k:v for k,v in pot_cfg.items() if not k.startswith("_") and k != "action"}
                self.plugins.run(action, params, value=val)
        except Exception as e: log.error(f"Pot action '{action}': {e}")

    def _obs_vol(self, source, val):
        try:
            import websocket as _ws
            db = round((val/100)*100 - 100, 1)
            ws = _ws.create_connection("ws://localhost:4444", timeout=2)
            ws.send(json.dumps({"request-type":"SetVolume","message-id":"pot","source":source,"volume":db,"useDecibel":True}))
            ws.close()
        except: pass

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
            n=a.get("name","").lower()
            [p.terminate() for p in psutil.process_iter(["name"]) if n in (p.info.get("name") or "").lower()]
        elif t == "open_folder":  os.startfile(a.get("path","."))
        elif t == "open_file":    os.startfile(a.get("path",""))
        elif t == "open_url":     open_url_default(a.get("url",""))
        elif t == "lock_session": keyboard.send("win+l")
        elif t == "shutdown":     run_silent("shutdown /s /t 0")
        elif t == "restart":      run_silent("shutdown /r /t 0")
        elif t == "sleep":        run_silent("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
        elif t == "logoff":       run_silent("shutdown /l")
        elif t == "run_command":  run_hidden(a.get("command",""), shell=True, creationflags=CREATE_NO_WINDOW)
        elif t == "script_powershell": run_hidden(["powershell","-Command",a.get("code","")], creationflags=CREATE_NO_WINDOW)
        elif t == "script_python":     exec(a.get("code",""), {})
        elif t == "script_batch":
            tmp=os.path.join(os.environ.get("TEMP","."), "md_tmp.bat")
            open(tmp,"w").write(a.get("code","")); run_hidden(tmp, shell=True, creationflags=CREATE_NO_WINDOW)
        elif t == "clean_temp":
            for f in glob.glob(os.path.join(os.environ.get("TEMP",""),"*")):
                try: os.remove(f) if os.path.isfile(f) else shutil.rmtree(f,ignore_errors=True)
                except: pass
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
        elif t == "brightness":
            run_hidden(["powershell","-Command",
                f"(Get-WmiObject -NS root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{a.get('value',75)})"],
                creationflags=CREATE_NO_WINDOW)

        # OBS
        elif t == "obs_scene":        self._obs("SetCurrentScene",{"scene-name":a.get("scene","")})
        elif t == "obs_stream_start": self._obs("StartStreaming",{})
        elif t == "obs_stream_stop":  self._obs("StopStreaming",{})
        elif t == "obs_record_start": self._obs("StartRecording",{})
        elif t == "obs_record_stop":  self._obs("StopRecording",{})
        elif t == "obs_mute_toggle":  self._obs("ToggleMute",{"source":a.get("source","Mic/Aux")})

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
        elif t == "discord_mute":  keyboard.send("ctrl+shift+m")
        elif t == "discord_deafen":keyboard.send("ctrl+shift+d")

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
        elif t == "app_mute_toggle":  toggle_app_mute(a.get("name",""))

        # OBS (suite)
        elif t in OBS_SIMPLE:         self._obs(OBS_SIMPLE[t], {})

        elif self.plugins and t in self.plugins.actions:
            self.plugins.run(t, {k:v for k,v in a.items() if k != "type"})
        else:
            log.warning(f"Action inconnue : {t}")

    def _obs(self, req, data):
        try:
            import websocket
            ws=websocket.create_connection("ws://localhost:4444",timeout=3)
            ws.send(json.dumps({"request-type":req,"message-id":"md",**data})); ws.close()
        except: pass

    def _api(self, a):
        import urllib.request
        req=urllib.request.Request(a.get("url",""),method=a.get("method","GET"))
        for k,v in a.get("headers",{}).items(): req.add_header(k,v)
        if a.get("body"): req.data=json.dumps(a["body"]).encode(); req.add_header("Content-Type","application/json")
        try:
            with urllib.request.urlopen(req,timeout=10) as r: log.info(f"API {r.status}")
        except Exception as e: log.error(f"API: {e}")

    def _webhook(self, a):
        import urllib.request
        req=urllib.request.Request(a.get("url",""),data=json.dumps(a.get("payload",{})).encode(),method="POST")
        req.add_header("Content-Type","application/json")
        try:
            with urllib.request.urlopen(req,timeout=10) as r: log.info(f"Webhook {r.status}")
        except Exception as e: log.error(f"Webhook: {e}")

    def _ha(self, a):
        import urllib.request
        url=f"{a.get('ha_url','http://homeassistant.local:8123')}/api/services/{a.get('service','').replace('.','/')}"
        req=urllib.request.Request(url,data=json.dumps({"entity_id":a.get("entity_id","")}).encode(),method="POST")
        req.add_header("Authorization",f"Bearer {a.get('token','')}"); req.add_header("Content-Type","application/json")
        try:
            with urllib.request.urlopen(req,timeout=5) as r: log.info(f"HA {r.status}")
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

# ── OVERLAY (mini-streamdeck Tkinter, au-dessus de tout) ─────────────────────
class ProfileOverlayWindow:
    def __init__(self):
        self._queue = None; self._root = None; self._popup = None; self._close_timer = None
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(timeout=5)

    def _run(self):
        try:
            import tkinter as tk, queue as _queue
        except Exception as e:
            log.warning(f"Tkinter indisponible: {e}"); self._ready.set(); return
        self._queue = _queue.Queue()
        try:
            root = tk.Tk(); root.withdraw()
        except Exception as e:
            log.warning(f"Tkinter init échoué: {e}"); self._queue=None; self._ready.set(); return
        self._root = root; self._ready.set()

        def poll():
            try:
                while True:
                    item = self._queue.get_nowait()
                    profile, ov_cfg = item if isinstance(item, tuple) else (item, {})
                    try: self._show(profile, ov_cfg)
                    except Exception as e: log.warning(f"Overlay show: {e}")
            except _queue.Empty: pass
            root.after(30, poll)

        root.after(30, poll); root.mainloop()

    def _show(self, profile: dict, ov_cfg: dict = None):
        import tkinter as tk
        root = self._root
        if not root: return
        if self._close_timer:
            try: root.after_cancel(self._close_timer)
            except: pass
            self._close_timer = None
        if self._popup:
            try: self._popup.destroy()
            except: pass
            self._popup = None

        ov       = ov_cfg or {}
        CELL     = max(32, min(100, int(ov.get("cell_size", 56))))
        DELAY    = max(1,  min(30,  int(ov.get("delay", 3)))) * 1000
        POSITION = ov.get("position", "br")
        ALPHA    = max(0.2, min(1.0, int(ov.get("alpha", 97)) / 100))
        OV_POTS  = ov.get("pots", {})
        
        ACC="#6366f1"; FG="#f1f5f9"; BG="#0d0e12"; CARD="#1c1f29"; FG3="#94a3b8"; BG3="#181b22"; BG4="#1e212b"; BDR="#2a2d3a"
        PAD=10; GAP=4; COLS=4
        W = COLS*CELL + (COLS-1)*GAP + PAD*2
        H = 38 + 1 + 8 + CELL*2 + GAP + 8 + 1 + 8 + CELL + 12
        sw=root.winfo_screenwidth(); sh=root.winfo_screenheight(); mg=20
        X,Y = {"br":(sw-W-mg,sh-H-60),"bl":(mg,sh-H-60),"tr":(sw-W-mg,mg+40)}.get(POSITION,(mg,mg+40))

        win = tk.Toplevel(root); self._popup = win
        win.overrideredirect(True); win.attributes("-topmost",True)
        try: win.attributes("-alpha",ALPHA)
        except: pass
        win.configure(bg=ACC); win.geometry(f"{W}x{H}+{X}+{Y}"); win.update_idletasks()

        main = tk.Frame(win,bg=BG); main.pack(padx=1,pady=1,fill="both",expand=True)
        win._imgs = []

        # Header
        hdr=tk.Frame(main,bg=BG); hdr.pack(fill="x",padx=PAD,pady=(7,4))
        dot=tk.Canvas(hdr,width=8,height=8,bg=BG,highlightthickness=0); dot.pack(side="left",pady=1)
        dot.create_oval(0,0,8,8,fill=ACC,outline="")
        tk.Label(hdr,text=profile.get("name","Profil"),fg=FG,bg=BG,font=("Segoe UI",10,"bold")).pack(side="left",padx=(6,0))
        tk.Label(hdr,text="PROFIL",fg=ACC,bg=BG,font=("Segoe UI",7,"bold")).pack(side="right")
        tk.Frame(main,bg=BDR,height=1).pack(fill="x")

        def _tk_img(data_url, sz):
            """Charge une data:image/* en PhotoImage Tkinter, avec PIL si dispo."""
            import io, base64 as b64
            raw = b64.b64decode(data_url[data_url.index(",")+1:])
            try:
                from PIL import Image as PI, ImageTk
                return ImageTk.PhotoImage(PI.open(io.BytesIO(raw)).resize((sz,sz), PI.LANCZOS))
            except ImportError: pass
            try:
                tkimg = tk.PhotoImage(data=b64.b64encode(raw).decode())
                tw,th = tkimg.width(),tkimg.height()
                if tw>sz or th>sz:
                    f=max(tw//sz,th//sz,1)
                    if f>1: tkimg=tkimg.subsample(f,f)
                return tkimg
            except: return None

        # Boutons 4×2 — image seule, aucun nom
        bf=tk.Frame(main,bg=BG); bf.pack(padx=PAD,pady=(8,0))
        buttons=profile.get("buttons",{})
        for i in range(8):
            b=buttons.get(str(i),{}); r,c=divmod(i,COLS)
            outer=tk.Frame(bf,bg=BDR); outer.grid(row=r,column=c,padx=GAP//2,pady=GAP//2)
            cell=tk.Frame(outer,bg=CARD,width=CELL-2,height=CELL-2); cell.pack(padx=1,pady=1); cell.pack_propagate(False)
            img_data=b.get("iconImage",""); placed=False
            if img_data and img_data.startswith("data:image"):
                try:
                    tkimg=_tk_img(img_data,max(CELL-12,18))
                    if tkimg:
                        win._imgs.append(tkimg)
                        tk.Label(cell,image=tkimg,bg=CARD).place(relx=.5,rely=.5,anchor="center")
                        placed=True
                except Exception as e: log.warning(f"Overlay btn{i} img: {e}")
            if not placed:
                tk.Label(cell,text=b.get("icon","") or "●",fg=FG,bg=CARD,
                         font=("Segoe UI Emoji",max(14,int(CELL*0.36)))).place(relx=.5,rely=.5,anchor="center")

        tk.Frame(main,bg=BDR,height=1).pack(fill="x",padx=PAD,pady=(8,0))

        # Potards — image seule, aucun nom
        pf=tk.Frame(main,bg=BG); pf.pack(padx=PAD,pady=(8,12))
        pots=profile.get("pots",{})
        for i in range(COLS):
            p=pots.get(str(i),{})
            pot_ov=OV_POTS.get(str(i),OV_POTS.get(i,{})) if isinstance(OV_POTS,dict) else {}
            img_data=p.get("image","") or (pot_ov.get("image","") if isinstance(pot_ov,dict) else "")

            outer=tk.Frame(pf,bg=BDR); outer.grid(row=0,column=i,padx=GAP//2)
            cell=tk.Frame(outer,bg=BG3,width=CELL-2,height=CELL-2); cell.pack(padx=1,pady=1); cell.pack_propagate(False)
            placed=False
            if img_data and img_data.startswith("data:image"):
                try:
                    tkimg=_tk_img(img_data,max(CELL-12,18))
                    if tkimg:
                        win._imgs.append(tkimg)
                        tk.Label(cell,image=tkimg,bg=BG3).place(relx=.5,rely=.5,anchor="center")
                        placed=True
                except Exception as e: log.warning(f"Overlay pot{i} img: {e}")
            if not placed:
                sz=max(24,int(CELL*0.6)); mid=sz//2
                cv=tk.Canvas(cell,width=sz,height=sz,bg=BG3,highlightthickness=0); cv.place(relx=.5,rely=.5,anchor="center")
                cv.create_oval(1,1,sz-1,sz-1,outline=FG3,width=1,fill=BG4)
                cv.create_oval(int(sz*.2),int(sz*.2),int(sz*.8),int(sz*.8),outline=ACC,width=2,fill=BG3)
                cv.create_oval(mid-3,mid-3,mid+3,mid+3,fill=ACC,outline="")

        def _close():
            self._close_timer=None
            try:
                if win.winfo_exists(): win.destroy()
            except: pass
            if self._popup is win: self._popup=None
        self._close_timer=root.after(DELAY,_close)
        win.update_idletasks()

    def show_profile(self, profile: dict, ov_cfg: dict = None):
        if self._queue is not None:
            try: self._queue.put_nowait((profile, ov_cfg or {}))
            except Exception as e: log.warning(f"Overlay queue: {e}")

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
    3 slots USB :
      slot 0 = ESP32 — reçoit et envoie des trames série
      slot 1 & 2 = USB passifs — monitoring seulement (clés USB, hubs, etc.)
    """
    LONG_MS=400; DOUBLE_MS=300
    N_SLOTS = 3

    def __init__(self, on_msg):
        self._cb       = on_msg
        self._slots    = [None] * self.N_SLOTS
        self._port_names = [None] * self.N_SLOTS
        self._btn_state  = {}
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
            # Éviter de prendre un port déjà utilisé par un autre slot
            used  = {self._port_names[i] for i in range(self.N_SLOTS) if i != slot and self._port_names[i]}
            # Préférer les puces connues ESP32
            cands = [p.device for p in ports
                     if any(k in p.description.upper() for k in ["CP210","CH340","FTDI","USB SERIAL"])
                     and p.device not in used]
            if not cands:
                cands = [p.device for p in ports if p.device not in used]
            port = cands[0] if cands else None
        if not port: return
        try:
            ser = serial.Serial(port, baud, timeout=0.1)
            self._slots[slot] = ser; self._port_names[slot] = port
            threading.Thread(target=self._loop, args=(ser, slot), daemon=True).start()
            log.info(f"Serial slot {slot}: {port} @ {baud}")
        except Exception as e:
            log.error(f"Serial slot {slot}: {e}")

    def is_connected(self, slot=0):
        s = self._slots[slot]; return bool(s and s.is_open)

    def _loop(self, ser, slot):
        """Boucle de lecture — uniquement slot 0 (ESP32)."""
        while ser and ser.is_open:
            try:
                line = ser.readline().decode("utf-8", errors="ignore").strip()
                if line: self._cb(line, slot)
            except: time.sleep(1)
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
        """Surveille les ports USB 1 et 2 en mode passif.
        Détecte connexion/déconnexion et vitesse (Full/Hi/Super Speed).
        Ne lit pas de trames, ne bloque pas."""
        if not SERIAL_OK: return
        while True:
            try:
                all_ports = {p.device: p for p in serial.tools.list_ports.comports()}
                esp_port  = self._port_names[0]
                passive   = [p for dev, p in all_ports.items() if dev != esp_port]
                for slot in (1, 2):
                    idx = slot - 1  # index dans passive[]
                    if idx < len(passive):
                        self._port_names[slot] = passive[idx].device
                    else:
                        self._port_names[slot] = None
            except: pass
            time.sleep(2)

    def usb_info(self, slot):
        """Retourne les infos du port USB passif (slot 1 ou 2)."""
        if not SERIAL_OK or slot not in (1, 2):
            return {"connected": False, "port": None, "description": "", "speed": ""}
        port = self._port_names[slot]
        if not port:
            return {"connected": False, "port": None, "description": "", "speed": ""}
        try:
            ports = serial.tools.list_ports.comports()
            info  = next((p for p in ports if p.device == port), None)
            if info:
                desc = info.description or ""
                # Détecter la vitesse USB depuis la description ou vid/pid
                speed = ""
                dl = desc.lower()
                if "super" in dl or "3.0" in dl or "3.1" in dl or "3.2" in dl: speed = "SuperSpeed (5-20 Gb/s)"
                elif "hi"   in dl or "2.0" in dl:                               speed = "Hi-Speed (480 Mb/s)"
                elif "full" in dl or "1.1" in dl:                               speed = "Full-Speed (12 Mb/s)"
                elif "low"  in dl or "1.0" in dl:                               speed = "Low-Speed (1.5 Mb/s)"
                return {"connected": True, "port": port, "description": desc, "speed": speed}
        except: pass
        return {"connected": True, "port": port, "description": "", "speed": ""}

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
class MacroDeck:
    def __init__(self):
        self.cfg=ConfigManager(); self.ws_clients=set(); self._loop=None
        self.plugins=PluginManager()
        self.engine=ActionEngine(self.cfg,self._broadcast,self.plugins)
        self.metrics=Metrics(); self.transport=Transport(self._on_esp32)
        self.watcher=AppWatcher(self._on_app); self.overlay=ProfileOverlayWindow()

    def _broadcast(self, obj):
        if obj.get("type")=="profile_changed":
            key=obj.get("profile")
            profile=self.cfg.data.get("profiles",{}).get(key)
            if profile and self.overlay:
                self.overlay.show_profile(profile,self.cfg.data.get("overlay",{}))
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

    def _dispatch_button(self, idx, ev):
        actions=self.cfg.active()["buttons"].get(str(idx),{}).get(ev,[])
        if actions: threading.Thread(target=self.engine.run,args=(actions,),daemon=True).start()
        self._broadcast({"type":"button_event","button":idx,"event":ev})

    def _on_esp32(self, raw: str, slot: int = 0):
        fr=parse_frame(raw)
        if not fr: return
        kind,idx,val=fr
        if kind=="pot":
            self.engine.run_pot(self.cfg.active()["pots"].get(str(idx),{}),val)
            self._broadcast({"type":"pot_event","pot":idx,"value":val})
        elif val:   # bouton enfoncé
            self.transport._handle_timing(idx,"on",self._dispatch_button)
        else:       # bouton relâché
            self.transport._handle_timing(idx,"off",self._dispatch_button)
            self._broadcast({"type":"button_event","button":idx,"event":"release"})

    async def _metrics_loop(self):
        self.metrics.collect()
        while True:
            await asyncio.sleep(1)
            m=self.metrics.collect(); self._broadcast({"type":"metrics","data":m})
            keys=["cpu","ram","gpu_usage","ssd_usage"]
            for i in range(4):
                k=self.cfg.data.get("led_strips",{}).get(str(i),{}).get("metric",keys[i])
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
            self.transport.start(msg.get("port","AUTO"),baud=int(msg.get("baud",115200)),slot=int(msg.get("slot",0)))
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
                self.overlay.show_profile(profile,self.cfg.data.get("overlay",{}))
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
        self.transport.start(self.cfg.data.get("serial_port","AUTO"))
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
