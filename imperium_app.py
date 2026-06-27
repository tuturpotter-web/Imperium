cat > /home/claude/imperium_app.py << 'PYEOF'
"""
Imperium — Application Windows native (Tkinter)
Interface fidèle au gui.html : device card, drawers, modals,
LED strips, knobs SVG, serial log, métriques, profils, settings.
"""
APP_VERSION = "dev"
import sys, os, ctypes, threading, time, json, subprocess, re
import shutil, glob, logging, datetime, math
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import tkinter.font as tkfont

# ── CACHER CONSOLE ────────────────────────────────────────────────────────────
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

def run_hidden(cmd, **kw):
    kw.setdefault("creationflags", CREATE_NO_WINDOW)
    kw.setdefault("startupinfo", _SI)
    return subprocess.Popen(cmd, **kw)

def run_silent(cmd): return run_hidden(cmd, shell=True)

import psutil, keyboard, mouse

try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL; PYCAW_OK = True
except: PYCAW_OK = False

try: import win32gui, win32process; WIN32_OK = True
except: WIN32_OK = False

try: import wmi as wmilib; WMI_OK = True
except: WMI_OK = False

try: import serial, serial.tools.list_ports; SERIAL_OK = True
except: SERIAL_OK = False

GPU_OK = shutil.which("nvidia-smi") is not None
logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("IMP")

CONFIG_PATH = Path(os.path.expanduser("~")) / ".macrodeck" / "config.json"
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

# ══════════════════════════════════════════════════════════════════════════════
# THÈME — couleurs identiques au gui.html dark theme
# ══════════════════════════════════════════════════════════════════════════════
C = {
    "bg0":    "#08090c",
    "bg1":    "#0d0e12",
    "bg2":    "#12141a",
    "bg3":    "#181b22",
    "bg4":    "#1e212b",
    "card":   "#1c1f29",
    "border": "#ffffff12",
    "accent": "#6366f1",
    "adim":   "#6366f120",
    "text":   "#f1f5f9",
    "text2":  "#94a3b8",
    "text3":  "#475569",
    "green":  "#22c55e",
    "yellow": "#eab308",
    "red":    "#ef4444",
    "blue":   "#3b82f6",
    "purple": "#a855f7",
    "orange": "#f97316",
}
def c(k): return C[k]

# ══════════════════════════════════════════════════════════════════════════════
# AUDIO / SYSTEME
# ══════════════════════════════════════════════════════════════════════════════
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

def set_app_volume(name, level):
    if not PYCAW_OK or not name: return
    try:
        from pycaw.pycaw import ISimpleAudioVolume
        t = name.lower().replace(".exe","")
        for s in AudioUtilities.GetAllSessions():
            if s.Process and s.Process.name().lower().replace(".exe","") == t:
                s._ctl.QueryInterface(ISimpleAudioVolume).SetMasterVolume(max(0,min(100,level))/100.0, None)
    except: pass

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

def open_url(url):
    if not url: return
    if sys.platform == "win32":
        try:
            import winreg
            k=winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice")
            pid,_=winreg.QueryValueEx(k,"ProgId"); winreg.CloseKey(k)
            ck=winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, fr"{pid}\shell\open\command")
            cmd,_=winreg.QueryValueEx(ck,""); winreg.CloseKey(ck)
            run_hidden(cmd.replace("%1",url) if "%1" in cmd else f'{cmd} "{url}"', shell=True)
            return
        except: pass
        try: os.startfile(url); return
        except: pass
    import webbrowser; webbrowser.open(url)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
def empty_profile(name):
    return {
        "name": name, "app_trigger": "",
        "buttons": {str(i):{"icon":"⭐","label":f"Bouton {i+1}","press":[],"long_press":[],"double_click":[]} for i in range(8)},
        "pots": {str(i):{"name":["Volume","App Vol","Luminosité","Custom"][i],"action":["volume_system","volume_app","brightness","custom"][i]} for i in range(4)}
    }

DEFAULT_CONFIG = {
    "profiles": {"default":empty_profile("Global"),"obs":empty_profile("OBS"),"discord":empty_profile("Discord")},
    "active_profile": "default",
    "led_strips": {str(i):{"metric":["cpu","ram","gpu_usage","ssd_usage"][i]} for i in range(4)},
    "serial_port": "AUTO", "theme": "dark",
    "protocol": {"in_press":"btn{i}:on","in_long_press":"","in_double_click":"","in_release":"btn{i}:off","in_pot":"pot{i}:{v}","out_led":"led{i}:{v}"},
    "serial_port2":"","baud_rate":115200,"baud_rate2":115200,
    "overlay":{"cell_size":56,"delay":3,"position":"br","alpha":97}
}

def pattern_to_regex(p):
    e=re.escape(p)
    e=e.replace(re.escape("{i}"),r"(?P<i>-?\d+)")
    e=e.replace(re.escape("{v}"),r"(?P<v>-?\d+)")
    return re.compile("^"+e+"$")

def pattern_format(p,i=None,v=None):
    o=p
    if i is not None: o=o.replace("{i}",str(i))
    if v is not None: o=o.replace("{v}",str(v))
    return o

class ConfigManager:
    def __init__(self):
        self.data = json.loads(json.dumps(DEFAULT_CONFIG))
        self.load()

    def load(self):
        if CONFIG_PATH.exists():
            try:
                saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                self._merge(self.data, saved)
            except: pass

    def _merge(self, base, override):
        for k,v in override.items():
            if k in base and isinstance(base[k],dict) and isinstance(v,dict): self._merge(base[k],v)
            else: base[k]=v

    def save(self):
        CONFIG_PATH.write_text(json.dumps(self.data,indent=2,ensure_ascii=False),encoding="utf-8")

    def active(self):
        n=self.data.get("active_profile","default")
        return self.data["profiles"].get(n, list(self.data["profiles"].values())[0])

# ══════════════════════════════════════════════════════════════════════════════
# MÉTRIQUES
# ══════════════════════════════════════════════════════════════════════════════
class Metrics:
    def __init__(self):
        self._net_prev=psutil.net_io_counters(); self._net_t=time.time()
        self._ohm=None; self._twmi=None
        if WMI_OK:
            try: self._ohm=wmilib.WMI(namespace="root\\OpenHardwareMonitor")
            except: pass
            try: self._twmi=wmilib.WMI(namespace="root\\wmi")
            except: pass

    def _nvidia(self):
        try:
            p=run_hidden(["nvidia-smi","--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,name",
                "--format=csv,noheader,nounits"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            out,_=p.communicate(timeout=2)
            u,mu,mt,t,n=[x.strip() for x in out.strip().split("\n")[0].split(",")]
            return {"usage":float(u),"vram":round(float(mu)/float(mt)*100,1) if float(mt) else 0,"temp":float(t),"name":n}
        except: return None

    def collect(self):
        m={}
        m["cpu"]=psutil.cpu_percent(interval=None)
        m["cpu_cores"]=psutil.cpu_count(logical=True)
        f=psutil.cpu_freq(); m["cpu_freq"]=round(f.current,0) if f else 0
        m["cpu_temp"]=self._temp("Temperature","CPU")
        ram=psutil.virtual_memory()
        m["ram"]=ram.percent; m["ram_used_gb"]=round(ram.used/1e9,1); m["ram_total_gb"]=round(ram.total/1e9,1)
        m["gpu_usage"]=0; m["gpu_vram"]=0; m["gpu_temp"]=None; m["gpu_name"]=""
        if GPU_OK:
            g=self._nvidia()
            if g: m["gpu_usage"]=g["usage"]; m["gpu_vram"]=g["vram"]; m["gpu_temp"]=g["temp"]; m["gpu_name"]=g["name"]
        m["ssd_usage"]=0
        try: m["ssd_usage"]=psutil.disk_usage("C:\\").percent
        except:
            try: m["ssd_usage"]=psutil.disk_usage("/").percent
            except: pass
        m["disks"]=[]
        for p in psutil.disk_partitions(all=False):
            try:
                u=psutil.disk_usage(p.mountpoint)
                m["disks"].append({"mountpoint":p.mountpoint,"total_gb":round(u.total/1e9,1),"used_gb":round(u.used/1e9,1),"percent":u.percent})
            except: pass
        now=time.time(); net=psutil.net_io_counters(); dt=now-self._net_t
        m["net_up"]=round((net.bytes_sent-self._net_prev.bytes_sent)/dt/1024,1) if dt>0 else 0
        m["net_down"]=round((net.bytes_recv-self._net_prev.bytes_recv)/dt/1024,1) if dt>0 else 0
        self._net_prev=net; self._net_t=now
        m["uptime"]=str(datetime.timedelta(seconds=int(time.time()-psutil.boot_time())))
        m["volume"]=get_volume(); m["muted"]=get_mute()
        n=datetime.datetime.now(); m["time"]=n.strftime("%H:%M:%S"); m["date"]=n.strftime("%d/%m/%Y")
        procs=[]
        for p in sorted(psutil.process_iter(["name","cpu_percent","memory_percent"]),
                        key=lambda x:(x.info.get("cpu_percent") or 0),reverse=True)[:8]:
            try: procs.append({"name":p.info["name"],"cpu":round(p.info.get("cpu_percent") or 0,1),"mem":round(p.info.get("memory_percent") or 0,1)})
            except: pass
        m["top_processes"]=procs
        return m

    def _temp(self,typ,frag):
        if self._ohm:
            try:
                for s in self._ohm.Sensor():
                    if s.SensorType==typ and frag.lower() in s.Name.lower(): return round(s.Value,1)
            except: pass
        if typ=="Temperature" and self._twmi:
            try:
                for z in self._twmi.MSAcpi_ThermalZoneTemperature():
                    k=z.CurrentTemperature
                    if k and k>0: return round(k/10.0-273.15,1)
            except: pass
        return None

# ══════════════════════════════════════════════════════════════════════════════
# TRANSPORT SÉRIE
# ══════════════════════════════════════════════════════════════════════════════
class Transport:
    LONG_MS=400; DOUBLE_MS=300
    def __init__(self,on_msg):
        self._cb=on_msg; self._slots=[None,None]; self._port_names=[None,None]; self._btn_state={}

    def start(self,port="AUTO",baud=115200,slot=0):
        if not SERIAL_OK: return
        old=self._slots[slot]
        if old:
            try: old.close()
            except: pass
        self._slots[slot]=None; self._port_names[slot]=None
        if port=="AUTO":
            ports=serial.tools.list_ports.comports()
            other=self._port_names[1-slot]
            cands=[p.device for p in ports if any(k in p.description.upper() for k in ["CP210","CH340","USB","FTDI"]) and p.device!=other]
            if not cands: cands=[p.device for p in ports if p.device!=other]
            port=cands[0] if cands else None
        if not port: return
        try:
            ser=serial.Serial(port,baud,timeout=0.1)
            self._slots[slot]=ser; self._port_names[slot]=port
            threading.Thread(target=self._loop,args=(ser,slot),daemon=True).start()
        except Exception as e: log.error(f"Serial {slot}: {e}")

    def is_connected(self,slot=0): s=self._slots[slot]; return bool(s and s.is_open)

    def _loop(self,ser,slot):
        while ser and ser.is_open:
            try:
                line=ser.readline().decode("utf-8",errors="ignore").strip()
                if line: self._cb(line,slot)
            except: time.sleep(1)
        if self._slots[slot] is ser: self._slots[slot]=None; self._port_names[slot]=None

    def _handle_timing(self,idx,event,dispatch):
        now=time.time(); st=self._btn_state.setdefault(idx,{})
        if event=="on":
            last=st.get("last_on"); st["on_t"]=now; st["last_on"]=now
            if last and (now-last)*1000<self.DOUBLE_MS: st["last_on"]=None; dispatch(idx,"double_click")
        elif event=="off":
            on_t=st.get("on_t")
            if on_t is None: return
            st["on_t"]=None
            dispatch(idx,"long_press" if (now-on_t)*1000>=self.LONG_MS else "press")

    def send_raw(self,line,slot=0):
        ser=self._slots[slot]
        if ser and ser.is_open:
            try: ser.write((line+"\n").encode())
            except: pass

# ══════════════════════════════════════════════════════════════════════════════
# CATALOGUE D'ACTIONS
# ══════════════════════════════════════════════════════════════════════════════
ALL_ACTIONS = [
    {"cat":"Profils","icon":"◈","type":"switch_profile","name":"Aller sur un profil","desc":"Bascule vers un profil spécifique","params":[{"key":"profile","lbl":"Clé profil","ph":"obs"}]},
    {"cat":"Profils","icon":"›","type":"next_profile","name":"Profil suivant","desc":"","params":[]},
    {"cat":"Profils","icon":"‹","type":"prev_profile","name":"Profil précédent","desc":"","params":[]},
    {"cat":"PC","icon":"🚀","type":"open_app","name":"Ouvrir une application","desc":"Lance un logiciel","params":[{"key":"path","lbl":"Chemin","ph":"C:\\app.exe"}]},
    {"cat":"PC","icon":"✖","type":"close_app","name":"Fermer une application","desc":"Termine un processus","params":[{"key":"name","lbl":"Nom processus","ph":"notepad.exe"}]},
    {"cat":"PC","icon":"🗂","type":"open_folder","name":"Ouvrir un dossier","desc":"Explorateur","params":[{"key":"path","lbl":"Chemin","ph":"C:\\"}]},
    {"cat":"PC","icon":"📄","type":"open_file","name":"Ouvrir un fichier","desc":"","params":[{"key":"path","lbl":"Chemin","ph":"C:\\doc.pdf"}]},
    {"cat":"PC","icon":"🌐","type":"open_url","name":"Ouvrir une URL","desc":"","params":[{"key":"url","lbl":"URL","ph":"https://"}]},
    {"cat":"PC","icon":"🔒","type":"lock_session","name":"Verrouiller","desc":"Win+L","params":[]},
    {"cat":"PC","icon":"⏻","type":"shutdown","name":"Éteindre le PC","desc":"","params":[]},
    {"cat":"PC","icon":"🔄","type":"restart","name":"Redémarrer","desc":"","params":[]},
    {"cat":"PC","icon":"💤","type":"sleep","name":"Veille","desc":"","params":[]},
    {"cat":"PC","icon":"🧹","type":"clean_temp","name":"Nettoyer Temp","desc":"","params":[]},
    {"cat":"PC","icon":"📸","type":"screenshot","name":"Capture écran","desc":"Win+Shift+S","params":[]},
    {"cat":"PC","icon":"💻","type":"win_minimize_all","name":"Réduire tout","desc":"Win+D","params":[]},
    {"cat":"PC","icon":"⌨","type":"run_command","name":"Commande terminal","desc":"","params":[{"key":"command","lbl":"Commande","ph":"cmd /c ..."}]},
    {"cat":"PC","icon":"⚡","type":"script_powershell","name":"Script PowerShell","desc":"","params":[{"key":"code","lbl":"Code","ph":"Get-Process"}]},
    {"cat":"PC","icon":"🐍","type":"script_python","name":"Script Python","desc":"","params":[{"key":"code","lbl":"Code","ph":"import os"}]},
    {"cat":"PC","icon":"📜","type":"script_batch","name":"Script Batch","desc":"","params":[{"key":"code","lbl":"Commandes","ph":"@echo off"}]},
    {"cat":"Clavier","icon":"⌨","type":"hotkey","name":"Raccourci clavier","desc":"","params":[{"key":"keys","lbl":"Touches","ph":"ctrl+c"}]},
    {"cat":"Clavier","icon":"T","type":"type_text","name":"Saisir du texte","desc":"","params":[{"key":"text","lbl":"Texte","ph":"Bonjour !"}]},
    {"cat":"Clavier","icon":"🔁","type":"key_sequence","name":"Séquence de touches","desc":"","params":[{"key":"sequence","lbl":"Séquence","ph":"ctrl+a,ctrl+c"}]},
    {"cat":"Clavier","icon":"🖱","type":"mouse_click","name":"Clic souris","desc":"","params":[{"key":"button","lbl":"Bouton","ph":"left"},{"key":"x","lbl":"X","ph":""},{"key":"y","lbl":"Y","ph":""}]},
    {"cat":"Clavier","icon":"⬇","type":"mouse_scroll","name":"Molette","desc":"","params":[{"key":"delta","lbl":"Delta","ph":"3"}]},
    {"cat":"Audio","icon":"🔊","type":"volume_up","name":"Volume +","desc":"","params":[{"key":"step","lbl":"Pas %","ph":"5"}]},
    {"cat":"Audio","icon":"🔉","type":"volume_down","name":"Volume −","desc":"","params":[{"key":"step","lbl":"Pas %","ph":"5"}]},
    {"cat":"Audio","icon":"🔢","type":"volume_set","name":"Volume fixe","desc":"","params":[{"key":"value","lbl":"Niveau 0-100","ph":"50"}]},
    {"cat":"Audio","icon":"🔇","type":"mute_toggle","name":"Mute/Unmute","desc":"","params":[]},
    {"cat":"Audio","icon":"⏯","type":"media_play_pause","name":"Play/Pause","desc":"","params":[]},
    {"cat":"Audio","icon":"⏭","type":"media_next","name":"Piste suivante","desc":"","params":[]},
    {"cat":"Audio","icon":"⏮","type":"media_prev","name":"Piste précédente","desc":"","params":[]},
    {"cat":"Audio","icon":"⏹","type":"media_stop","name":"Stop","desc":"","params":[]},
    {"cat":"Audio","icon":"💡","type":"brightness","name":"Luminosité","desc":"","params":[{"key":"value","lbl":"Niveau 0-100","ph":"75"}]},
    {"cat":"OBS","icon":"🎬","type":"obs_scene","name":"OBS — Changer scène","desc":"","params":[{"key":"scene","lbl":"Nom scène","ph":"Gaming"}]},
    {"cat":"OBS","icon":"📡","type":"obs_stream_start","name":"OBS — Démarrer stream","desc":"","params":[]},
    {"cat":"OBS","icon":"⏹","type":"obs_stream_stop","name":"OBS — Arrêter stream","desc":"","params":[]},
    {"cat":"OBS","icon":"⏺","type":"obs_record_start","name":"OBS — Démarrer enreg.","desc":"","params":[]},
    {"cat":"OBS","icon":"⏹","type":"obs_record_stop","name":"OBS — Arrêter enreg.","desc":"","params":[]},
    {"cat":"OBS","icon":"🎙","type":"obs_mute_toggle","name":"OBS — Mute source","desc":"","params":[{"key":"source","lbl":"Source","ph":"Mic/Aux"}]},
    {"cat":"Visio","icon":"🎙","type":"zoom_mute","name":"Zoom — Mute","desc":"Alt+A","params":[]},
    {"cat":"Visio","icon":"📹","type":"zoom_camera","name":"Zoom — Caméra","desc":"Alt+V","params":[]},
    {"cat":"Visio","icon":"✋","type":"zoom_hand","name":"Zoom — Lever la main","desc":"Alt+Y","params":[]},
    {"cat":"Visio","icon":"🚪","type":"zoom_leave","name":"Zoom — Quitter","desc":"Alt+Q","params":[]},
    {"cat":"Visio","icon":"🎙","type":"teams_mute","name":"Teams — Mute","desc":"Ctrl+Shift+M","params":[]},
    {"cat":"Visio","icon":"🎙","type":"discord_mute","name":"Discord — Mute","desc":"","params":[]},
    {"cat":"Visio","icon":"🔕","type":"discord_deafen","name":"Discord — Sourd","desc":"","params":[]},
    {"cat":"Dev","icon":"🆚","type":"vscode_open","name":"Ouvrir VS Code","desc":"","params":[{"key":"path","lbl":"Dossier","ph":"."}]},
    {"cat":"Dev","icon":"⬇","type":"git_pull","name":"Git Pull","desc":"","params":[{"key":"folder","lbl":"Dossier","ph":"."}]},
    {"cat":"Dev","icon":"⬆","type":"git_push","name":"Git Push","desc":"","params":[{"key":"folder","lbl":"Dossier","ph":"."},{"key":"message","lbl":"Message","ph":"commit"}]},
    {"cat":"Dev","icon":"🐳","type":"docker_start","name":"Docker Start","desc":"","params":[{"key":"name","lbl":"Conteneur","ph":"app"}]},
    {"cat":"Dev","icon":"🐳","type":"docker_stop","name":"Docker Stop","desc":"","params":[{"key":"name","lbl":"Conteneur","ph":"app"}]},
    {"cat":"Web","icon":"🤖","type":"open_chatgpt","name":"Ouvrir ChatGPT","desc":"","params":[]},
    {"cat":"Web","icon":"📧","type":"google_gmail","name":"Ouvrir Gmail","desc":"","params":[]},
    {"cat":"Web","icon":"📹","type":"google_meet","name":"Nouvelle réunion Meet","desc":"","params":[]},
    {"cat":"Temps","icon":"⏱","type":"timer","name":"Timer","desc":"","params":[{"key":"seconds","lbl":"Secondes","ph":"60"},{"key":"label","lbl":"Message","ph":"Terminé !"}]},
    {"cat":"Temps","icon":"🍅","type":"pomodoro","name":"Pomodoro 25min","desc":"","params":[]},
    {"cat":"Réseau","icon":"📡","type":"ping","name":"Ping","desc":"","params":[{"key":"host","lbl":"Hôte","ph":"8.8.8.8"}]},
    {"cat":"Auto","icon":"⏳","type":"delay","name":"Délai","desc":"","params":[{"key":"ms","lbl":"Millisecondes","ph":"500"}]},
    {"cat":"Auto","icon":"🌐","type":"api_call","name":"Appel API REST","desc":"","params":[{"key":"url","lbl":"URL","ph":"https://"},{"key":"method","lbl":"Méthode","ph":"GET"}]},
    {"cat":"Auto","icon":"🪝","type":"webhook","name":"Webhook","desc":"","params":[{"key":"url","lbl":"URL Webhook","ph":"https://"}]},
    {"cat":"Auto","icon":"🏠","type":"home_assistant","name":"Home Assistant","desc":"","params":[{"key":"ha_url","lbl":"URL HA","ph":"http://homeassistant.local:8123"},{"key":"service","lbl":"Service","ph":"light.toggle"},{"key":"entity_id","lbl":"Entity","ph":"light.salon"}]},
]

POT_ACTS = [
    ("volume_system","🔊","Volume système","Audio"),
    ("volume_app","🎚","Volume d'une application","Audio"),
    ("game_volume","🎮","Volume d'un jeu","Audio"),
    ("discord_volume","💬","Volume Discord","Audio"),
    ("spotify_volume","🎵","Volume Spotify","Audio"),
    ("mic_volume","🎙","Volume micro","Audio"),
    ("brightness","💡","Luminosité écran","Écran"),
    ("obs_volume","🎬","Volume source OBS","OBS"),
    ("scroll","⬇","Défilement (scroll)","Navigation"),
    ("zoom_level","🔍","Zoom","Navigation"),
    ("media_seek","⏩","Avance/recul média","Médias"),
    ("playback_speed","🐇","Vitesse de lecture","Médias"),
    ("custom","⚙","Script Python custom","Avancé"),
]

LED_METRICS = [
    ("cpu","🔲","Utilisation CPU",True,None,""),
    ("ram","🧠","Utilisation RAM",True,None,""),
    ("gpu_usage","🎮","Utilisation GPU",True,None,""),
    ("gpu_vram","🟪","VRAM GPU",True,None,""),
    ("ssd_usage","💾","Espace disque (C:)",True,None,""),
    ("cpu_temp","🌡","Température CPU",False,100,"°C"),
    ("gpu_temp","🌡","Température GPU",False,100,"°C"),
    ("net_down","⬇","Réseau ↓",False,5120,"KB/s"),
    ("net_up","⬆","Réseau ↑",False,2048,"KB/s"),
    ("volume","🔊","Volume système",True,None,""),
    ("off","⚫","Éteinte",True,None,""),
]

# ══════════════════════════════════════════════════════════════════════════════
# OVERLAY PROFIL (fenêtre flottante Tkinter, thread séparé)
# ══════════════════════════════════════════════════════════════════════════════
class ProfileOverlay:
    def __init__(self):
        self._q=None; self._root=None; self._popup=None; self._timer=None
        self._ready=threading.Event()
        threading.Thread(target=self._run,daemon=True).start()
        self._ready.wait(timeout=5)

    def _run(self):
        try:
            import queue as _q
            self._q=_q.Queue()
            root=tk.Tk(); root.withdraw(); self._root=root; self._ready.set()
            def poll():
                try:
                    while True:
                        item=self._q.get_nowait()
                        try: self._show(*item)
                        except Exception as e: log.warning(f"Overlay: {e}")
                except: pass
                root.after(30,poll)
            root.after(30,poll); root.mainloop()
        except Exception as e:
            log.warning(f"Overlay init: {e}"); self._ready.set()

    def _show(self,profile,ov_cfg):
        root=self._root
        if not root: return
        if self._timer:
            try: root.after_cancel(self._timer)
            except: pass
        if self._popup:
            try: self._popup.destroy()
            except: pass
            self._popup=None

        ov=ov_cfg or {}; CELL=max(32,min(100,int(ov.get("cell_size",56))))
        DELAY=max(1,min(30,int(ov.get("delay",3))))*1000
        POS=ov.get("position","br"); ALPHA=max(0.2,min(1.0,int(ov.get("alpha",97))/100))
        ACC="#6366f1"; BG="#08090c"; CARD="#1c1f29"; FG="#f1f5f9"; FG3="#94a3b8"
        BG3="#181b22"; BG4="#1e212b"; BDR="#ffffff12"; GAP=4; PAD=10; COLS=4
        W=COLS*CELL+(COLS-1)*GAP+PAD*2
        H=38+1+8+CELL*2+GAP+8+1+8+CELL+12
        sw=root.winfo_screenwidth(); sh=root.winfo_screenheight(); mg=20
        X,Y={"br":(sw-W-mg,sh-H-60),"bl":(mg,sh-H-60),"tr":(sw-W-mg,mg+40)}.get(POS,(mg,mg+40))

        win=tk.Toplevel(root); self._popup=win
        win.overrideredirect(True); win.attributes("-topmost",True)
        try: win.attributes("-alpha",ALPHA)
        except: pass
        win.configure(bg=ACC); win.geometry(f"{W}x{H}+{X}+{Y}"); win._imgs=[]
        main=tk.Frame(win,bg=BG); main.pack(padx=1,pady=1,fill="both",expand=True)

        hdr=tk.Frame(main,bg=BG); hdr.pack(fill="x",padx=PAD,pady=(7,4))
        dot=tk.Canvas(hdr,width=8,height=8,bg=BG,highlightthickness=0); dot.pack(side="left")
        dot.create_oval(0,0,8,8,fill=ACC,outline="")
        tk.Label(hdr,text=profile.get("name","Profil"),fg=FG,bg=BG,font=("Segoe UI",10,"bold")).pack(side="left",padx=(6,0))
        tk.Label(hdr,text="PROFIL",fg=ACC,bg=BG,font=("Segoe UI",7,"bold")).pack(side="right")
        tk.Frame(main,bg=BDR,height=1).pack(fill="x")

        bf=tk.Frame(main,bg=BG); bf.pack(padx=PAD,pady=(8,0))
        for i in range(8):
            b=profile.get("buttons",{}).get(str(i),{}); r,c=divmod(i,COLS)
            outer=tk.Frame(bf,bg=BDR); outer.grid(row=r,column=c,padx=GAP//2,pady=GAP//2)
            cell=tk.Frame(outer,bg=CARD,width=CELL-2,height=CELL-2); cell.pack(padx=1,pady=1); cell.pack_propagate(False)
            lbl=(b.get("label") or f"Btn {i+1}")[:9]
            tk.Label(cell,text=b.get("icon","⭐"),fg=FG,bg=CARD,font=("Segoe UI Emoji",15)).place(relx=.5,rely=.36,anchor="center")
            tk.Label(cell,text=lbl,fg=FG3,bg=CARD,font=("Segoe UI",6)).place(relx=.5,rely=.78,anchor="center")

        tk.Frame(main,bg=BDR,height=1).pack(fill="x",padx=PAD,pady=(8,0))

        pf=tk.Frame(main,bg=BG); pf.pack(padx=PAD,pady=(8,12))
        POT_LBL={k:n for k,_,n,_ in POT_ACTS}
        for i in range(COLS):
            p=profile.get("pots",{}).get(str(i),{})
            name=(p.get("name") or f"Pot {i+1}")[:8]
            action=POT_LBL.get(p.get("action",""),"—")
            outer=tk.Frame(pf,bg=BDR); outer.grid(row=0,column=i,padx=GAP//2)
            cell=tk.Frame(outer,bg=BG3,width=CELL-2,height=CELL-2); cell.pack(padx=1,pady=1); cell.pack_propagate(False)
            cv=tk.Canvas(cell,width=26,height=26,bg=BG3,highlightthickness=0); cv.place(relx=.5,rely=.26,anchor="center")
            cv.create_oval(1,1,25,25,outline=FG3,width=1,fill=BG4)
            cv.create_oval(5,5,21,21,outline=ACC,width=1.5,fill=BG3)
            cv.create_oval(10,10,16,16,fill=ACC,outline="")
            tk.Label(cell,text=name,fg=FG,bg=BG3,font=("Segoe UI",6,"bold")).place(relx=.5,rely=.65,anchor="center")
            tk.Label(cell,text=action[:8],fg=FG3,bg=BG3,font=("Segoe UI",5)).place(relx=.5,rely=.83,anchor="center")

        win.update_idletasks()
        self._timer=root.after(DELAY,self._close)

    def _close(self):
        self._timer=None
        if self._popup:
            try: self._popup.destroy()
            except: pass
            self._popup=None

    def show(self,profile,ov_cfg):
        if self._q:
            try: self._q.put_nowait((profile,ov_cfg))
            except: pass

# ══════════════════════════════════════════════════════════════════════════════
# MOTEUR D'ACTIONS
# ══════════════════════════════════════════════════════════════════════════════
class ActionEngine:
    def __init__(self,cfg,on_profile_change):
        self.cfg=cfg; self._profile_cb=on_profile_change

    def run(self,actions):
        for a in actions:
            try: self._one(a)
            except Exception as e: log.error(f"Action {a.get('type')}: {e}")

    def run_pot(self,pot_cfg,val):
        ac=pot_cfg.get("action","volume_system")
        try:
            if   ac=="volume_system":  set_volume(val)
            elif ac=="volume_app":     set_app_volume(pot_cfg.get("app",""),val)
            elif ac=="discord_volume": set_app_volume("Discord",val)
            elif ac=="spotify_volume": set_app_volume("Spotify",val)
            elif ac in ("game_volume","mic_volume"): set_app_volume(pot_cfg.get("app",""),val)
            elif ac=="brightness":
                run_hidden(["powershell","-Command",f"(Get-WmiObject -NS root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{val})"],creationflags=CREATE_NO_WINDOW)
            elif ac in ("scroll","zoom_level","media_seek","playback_speed"):
                last=pot_cfg.get("_last",50); d=val-last; pot_cfg["_last"]=val
                if not d: return
                if ac=="scroll": mouse.wheel(d/8)
                elif ac=="zoom_level": keyboard.press("ctrl"); mouse.wheel(d/10); keyboard.release("ctrl")
                elif ac=="media_seek":
                    if d>2: keyboard.send("right")
                    elif d<-2: keyboard.send("left")
            elif ac=="custom":
                code=pot_cfg.get("script","")
                if code: exec(code,{"value":val})
        except Exception as e: log.error(f"Pot '{ac}': {e}")

    def _one(self,a):
        if isinstance(a.get("params"),dict):
            m=dict(a); m.update(a["params"]); a=m
        t=a.get("type","")
        if t in ("switch_profile","next_profile","prev_profile"):
            keys=list(self.cfg.data["profiles"].keys())
            cur=self.cfg.data.get("active_profile","default")
            if   t=="switch_profile": n=a.get("profile","")
            elif t=="next_profile":   n=keys[(keys.index(cur)+1)%len(keys)] if cur in keys else keys[0]
            else:                     n=keys[(keys.index(cur)-1)%len(keys)] if cur in keys else keys[0]
            if n in self.cfg.data["profiles"]:
                self.cfg.data["active_profile"]=n; self.cfg.save(); self._profile_cb(n)
        elif t=="open_app":     run_hidden(a.get("path",""),shell=True,creationflags=CREATE_NO_WINDOW)
        elif t=="close_app":    [p.terminate() for p in psutil.process_iter(["name"]) if a.get("name","").lower() in (p.info.get("name") or "").lower()]
        elif t=="open_folder":  os.startfile(a.get("path","."))
        elif t=="open_file":    os.startfile(a.get("path",""))
        elif t=="open_url":     open_url(a.get("url",""))
        elif t=="lock_session": keyboard.send("win+l")
        elif t=="shutdown":     run_silent("shutdown /s /t 0")
        elif t=="restart":      run_silent("shutdown /r /t 0")
        elif t=="sleep":        run_silent("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
        elif t=="logoff":       run_silent("shutdown /l")
        elif t=="run_command":  run_hidden(a.get("command",""),shell=True,creationflags=CREATE_NO_WINDOW)
        elif t=="script_powershell": run_hidden(["powershell","-Command",a.get("code","")],creationflags=CREATE_NO_WINDOW)
        elif t=="script_python":     exec(a.get("code",""),{})
        elif t=="script_batch":
            tmp=os.path.join(os.environ.get("TEMP","."), "imp_tmp.bat")
            open(tmp,"w").write(a.get("code","")); run_hidden(tmp,shell=True,creationflags=CREATE_NO_WINDOW)
        elif t=="clean_temp":
            for f in glob.glob(os.path.join(os.environ.get("TEMP",""),"*")):
                try: os.remove(f) if os.path.isfile(f) else shutil.rmtree(f,ignore_errors=True)
                except: pass
        elif t=="screenshot":        keyboard.send("win+shift+s")
        elif t=="win_minimize_all":  keyboard.send("win+d")
        elif t=="hotkey":            keyboard.send(a.get("keys",""))
        elif t=="type_text":         keyboard.write(a.get("text",""),delay=0.03)
        elif t=="key_sequence":
            for k in a.get("sequence","").split(","):
                keyboard.send(k.strip()); time.sleep(0.05)
        elif t=="mouse_click":
            x,y=a.get("x"),a.get("y")
            if x is not None: mouse.move(x,y,absolute=True)
            mouse.click(a.get("button","left"))
        elif t=="mouse_scroll": mouse.wheel(a.get("delta",1))
        elif t=="volume_up":    set_volume(get_volume()+int(a.get("step",5)))
        elif t=="volume_down":  set_volume(get_volume()-int(a.get("step",5)))
        elif t=="volume_set":   set_volume(int(a.get("value",50)))
        elif t=="mute_toggle":  set_mute(not get_mute())
        elif t=="media_play_pause": keyboard.send("play/pause media")
        elif t=="media_next":   keyboard.send("next track")
        elif t=="media_prev":   keyboard.send("previous track")
        elif t=="media_stop":   keyboard.send("stop media")
        elif t=="brightness":
            run_hidden(["powershell","-Command",f"(Get-WmiObject -NS root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{a.get('value',75)})"],creationflags=CREATE_NO_WINDOW)
        elif t=="obs_scene":          self._obs("SetCurrentScene",{"scene-name":a.get("scene","")})
        elif t=="obs_stream_start":   self._obs("StartStreaming",{})
        elif t=="obs_stream_stop":    self._obs("StopStreaming",{})
        elif t=="obs_record_start":   self._obs("StartRecording",{})
        elif t=="obs_record_stop":    self._obs("StopRecording",{})
        elif t=="obs_mute_toggle":    self._obs("ToggleMute",{"source":a.get("source","Mic/Aux")})
        elif t=="zoom_mute":     keyboard.send("alt+a")
        elif t=="zoom_camera":   keyboard.send("alt+v")
        elif t=="zoom_hand":     keyboard.send("alt+y")
        elif t=="zoom_leave":    keyboard.send("alt+q")
        elif t=="teams_mute":    keyboard.send("ctrl+shift+m")
        elif t=="discord_mute":  keyboard.send("ctrl+shift+m")
        elif t=="discord_deafen":keyboard.send("ctrl+shift+d")
        elif t=="vscode_open":   run_hidden(f'code "{a.get("path",".")}"',shell=True,creationflags=CREATE_NO_WINDOW)
        elif t=="git_pull":      run_hidden(f'git -C "{a.get("folder",".")}" pull',shell=True,creationflags=CREATE_NO_WINDOW)
        elif t=="git_push":
            f=a.get("folder","."); m=a.get("message","commit")
            run_hidden(f'git -C "{f}" add -A && git -C "{f}" commit -m "{m}" && git -C "{f}" push',shell=True,creationflags=CREATE_NO_WINDOW)
        elif t=="docker_start":  run_hidden(f'docker start {a.get("name","")}',shell=True,creationflags=CREATE_NO_WINDOW)
        elif t=="docker_stop":   run_hidden(f'docker stop {a.get("name","")}',shell=True,creationflags=CREATE_NO_WINDOW)
        elif t in ("open_chatgpt","google_gmail","google_meet"):
            urls={"open_chatgpt":"https://chatgpt.com","google_gmail":"https://mail.google.com","google_meet":"https://meet.google.com/new"}
            open_url(urls[t])
        elif t=="timer":
            s=int(a.get("seconds",60)); lbl=a.get("label","Terminé !")
            threading.Thread(target=lambda:(time.sleep(s),ctypes.windll.user32.MessageBoxW(0,lbl,"Imperium ⏱",0x40|0x1000)),daemon=True).start()
        elif t=="pomodoro":
            threading.Thread(target=lambda:(time.sleep(25*60),ctypes.windll.user32.MessageBoxW(0,"🍅 Pomodoro terminé !","Imperium",0x40|0x1000)),daemon=True).start()
        elif t=="delay": time.sleep(a.get("ms",500)/1000)
        elif t=="api_call": threading.Thread(target=self._api,args=(a,),daemon=True).start()
        elif t=="webhook":  threading.Thread(target=self._webhook,args=(a,),daemon=True).start()
        elif t=="home_assistant": threading.Thread(target=self._ha,args=(a,),daemon=True).start()

    def _obs(self,req,data):
        try:
            import websocket; ws=websocket.create_connection("ws://localhost:4444",timeout=3)
            ws.send(json.dumps({"request-type":req,"message-id":"md",**data})); ws.close()
        except: pass

    def _api(self,a):
        import urllib.request
        req=urllib.request.Request(a.get("url",""),method=a.get("method","GET"))
        try:
            with urllib.request.urlopen(req,timeout=10) as r: log.info(f"API {r.status}")
        except: pass

    def _webhook(self,a):
        import urllib.request
        req=urllib.request.Request(a.get("url",""),data=json.dumps(a.get("payload",{})).encode(),method="POST")
        req.add_header("Content-Type","application/json")
        try:
            with urllib.request.urlopen(req,timeout=10) as r: log.info(f"Webhook {r.status}")
        except: pass

    def _ha(self,a):
        import urllib.request
        url=f"{a.get('ha_url','http://homeassistant.local:8123')}/api/services/{a.get('service','').replace('.','/')}"
        req=urllib.request.Request(url,data=json.dumps({"entity_id":a.get("entity_id","")}).encode(),method="POST")
        req.add_header("Authorization",f"Bearer {a.get('token','')}"); req.add_header("Content-Type","application/json")
        try:
            with urllib.request.urlopen(req,timeout=5) as r: log.info(f"HA {r.status}")
        except: pass

# ══════════════════════════════════════════════════════════════════════════════
# WIDGETS RÉUTILISABLES
# ══════════════════════════════════════════════════════════════════════════════
def styled_btn(parent, text, command, style="normal", **kw):
    """Bouton styled : 'normal', 'primary', 'danger', 'ghost'"""
    styles = {
        "normal":  {"bg":c("bg3"),  "fg":c("text2"), "abg":c("bg4"),  "afg":c("text")},
        "primary": {"bg":c("accent"),"fg":"white",   "abg":"#5052d5", "afg":"white"},
        "danger":  {"bg":c("bg3"),  "fg":c("red"),   "abg":"#ef444420","afg":c("red")},
        "ghost":   {"bg":c("bg1"),  "fg":c("text3"), "abg":c("bg2"),  "afg":c("text2")},
    }
    s=styles.get(style,styles["normal"])
    btn=tk.Label(parent,text=text,bg=s["bg"],fg=s["fg"],cursor="hand2",
        font=kw.pop("font",("Segoe UI",9)),padx=kw.pop("padx",10),pady=kw.pop("pady",4),
        relief="flat",**kw)
    btn.bind("<Enter>", lambda e: btn.configure(bg=s["abg"],fg=s["afg"]))
    btn.bind("<Leave>", lambda e: btn.configure(bg=s["bg"],fg=s["fg"]))
    btn.bind("<Button-1>", lambda e: command())
    return btn

def styled_entry(parent, textvariable=None, width=None, font=("Segoe UI",10), **kw):
    frame=tk.Frame(parent,bg=c("bg2"),highlightthickness=1,highlightbackground=c("border"))
    e=tk.Entry(frame,bg=c("bg2"),fg=c("text"),insertbackground=c("text"),
        relief="flat",bd=4,textvariable=textvariable,font=font,**kw)
    if width: e.configure(width=width)
    e.pack(fill="x")
    e.bind("<FocusIn>", lambda ev: frame.configure(highlightbackground=c("accent")))
    e.bind("<FocusOut>", lambda ev: frame.configure(highlightbackground=c("border")))
    frame._entry=e
    return frame

def separator(parent, orient="h", **kw):
    if orient=="h":
        f=tk.Frame(parent,bg=c("border"),height=1); f.pack(fill="x",**kw)
    else:
        f=tk.Frame(parent,bg=c("border"),width=1); f.pack(fill="y",**kw)
    return f

# ══════════════════════════════════════════════════════════════════════════════
# DRAWER — panneau latéral qui slide depuis la droite
# ══════════════════════════════════════════════════════════════════════════════
class Drawer:
    def __init__(self, root, width=320):
        self.root=root; self.width=width; self._open=False
        # Overlay sombre
        self.overlay=tk.Frame(root,bg="#00000088")
        # Panneau
        self.panel=tk.Frame(root,bg=c("bg1"),width=width)
        # Header
        self.hdr=tk.Frame(self.panel,bg=c("bg1"),height=46)
        self.hdr.pack(fill="x"); self.hdr.pack_propagate(False)
        separator(self.hdr,orient="v",side="bottom")
        self.title_lbl=tk.Label(self.hdr,text="",bg=c("bg1"),fg=c("text"),font=("Segoe UI",12,"bold"))
        self.title_lbl.pack(side="left",padx=16,pady=0,fill="y")
        close_btn=tk.Label(self.hdr,text="✕",bg=c("bg3"),fg=c("text2"),cursor="hand2",
            font=("Segoe UI",13),padx=6,pady=2)
        close_btn.pack(side="right",padx=12,pady=8)
        close_btn.bind("<Button-1>",lambda e:self.close())
        close_btn.bind("<Enter>",lambda e:close_btn.configure(bg=c("red"),fg="white"))
        close_btn.bind("<Leave>",lambda e:close_btn.configure(bg=c("bg3"),fg=c("text2")))
        # Body scrollable
        self.canvas=tk.Canvas(self.panel,bg=c("bg1"),highlightthickness=0)
        self.scrollbar=tk.Scrollbar(self.panel,orient="vertical",command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right",fill="y")
        self.canvas.pack(fill="both",expand=True)
        self.body=tk.Frame(self.canvas,bg=c("bg1"))
        self._win_id=self.canvas.create_window((0,0),window=self.body,anchor="nw")
        self.body.bind("<Configure>",self._on_configure)
        self.canvas.bind("<Configure>",self._on_canvas_resize)
        self.canvas.bind("<MouseWheel>",lambda e:self.canvas.yview_scroll(int(-1*(e.delta/120)),"units"))
        self.overlay.bind("<Button-1>",lambda e:self.close())

    def _on_configure(self,e):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_resize(self,e):
        self.canvas.itemconfig(self._win_id,width=e.width)

    def open(self, title=""):
        self.title_lbl.configure(text=title)
        rw=self.root.winfo_width(); rh=self.root.winfo_height()
        rx=self.root.winfo_x(); ry=self.root.winfo_y()
        self.overlay.place(x=0,y=0,width=rw,height=rh)
        self.panel.place(x=rw-self.width,y=0,width=self.width,height=rh)
        self.panel.lift(); self._open=True
        self.canvas.yview_moveto(0)

    def close(self):
        self.overlay.place_forget(); self.panel.place_forget(); self._open=False

    def clear_body(self):
        for w in self.body.winfo_children(): w.destroy()

# ══════════════════════════════════════════════════════════════════════════════
# APPLICATION PRINCIPALE
# ══════════════════════════════════════════════════════════════════════════════
class ImperiumApp:
    def __init__(self, root):
        self.root=root
        self.root.title("Imperium")
        self.root.configure(bg=c("bg0"))
        self.root.geometry("960x660")
        self.root.minsize(700,500)

        self.cfg=ConfigManager()
        self.metrics_engine=Metrics()
        self.overlay=ProfileOverlay()
        self.engine=ActionEngine(self.cfg, self._on_profile_changed)
        self.transport=Transport(self._on_serial)

        self._metrics={}
        self._active_view="device"
        self._pot_pcts=[0,0,0,0]
        self._logs=[]
        self._log_filter="all"
        self._sel_btn=None
        self._sel_pot=None
        self._ev_type="press"  # press / long_press / double_click

        self._btn_canvases={}  # idx → Frame
        self._pot_widgets={}   # idx → {canvas, arc_id, line_id, pct_lbl}
        self._led_widgets={}   # idx → {frame, glow, lbl, pct}

        self._build_ui()
        self.transport.start(self.cfg.data.get("serial_port","AUTO"))
        self._start_metrics_loop()
        self._refresh_device()

    # ══════════════════════════════════════════════════════════════════════════
    # STRUCTURE UI
    # ══════════════════════════════════════════════════════════════════════════
    def _build_ui(self):
        # ── Topbar ──────────────────────────────────────────────────────────
        top=tk.Frame(self.root,bg=c("bg1"),height=46); top.pack(fill="x"); top.pack_propagate(False)
        separator(top,orient="h",side="bottom")

        # Logo
        logo_ic=tk.Label(top,text="🎛",bg=c("accent"),fg="white",font=("Segoe UI Emoji",11),padx=5,pady=2)
        logo_ic.pack(side="left",padx=(12,0),pady=8)
        tk.Label(top,text="Imperium",bg=c("bg1"),fg=c("text"),font=("Segoe UI",12,"bold")).pack(side="left",padx=6)
        ver=tk.Label(top,text=f"V{APP_VERSION}",bg=c("adim"),fg=c("accent"),font=("Segoe UI",8,"bold"),padx=5,pady=1)
        ver.pack(side="left",padx=4)

        # Séparateur vertical
        tk.Frame(top,bg=c("border"),width=1).pack(side="left",fill="y",padx=8,pady=10)

        # Titre de la vue courante
        self._view_title=tk.Label(top,text="Device",bg=c("bg1"),fg=c("text2"),font=("Segoe UI",12,"bold"))
        self._view_title.pack(side="left",padx=4)

        # Droite : statut WS, heure, save
        self._lbl_time=tk.Label(top,text="",bg=c("bg1"),fg=c("text3"),font=("Segoe UI",9))
        self._lbl_time.pack(side="right",padx=12)

        btn_save=styled_btn(top,"💾 Enregistrer",self._save_config,style="primary",padx=10,pady=4)
        btn_save.pack(side="right",padx=4,pady=9)

        # Statut WS/connexion
        ws_frame=tk.Frame(top,bg=c("bg2"),highlightthickness=1,highlightbackground=c("border"))
        ws_frame.pack(side="right",padx=4,pady=11)
        self._ws_dot=tk.Canvas(ws_frame,width=6,height=6,bg=c("bg2"),highlightthickness=0)
        self._ws_dot.pack(side="left",padx=(6,2),pady=5)
        self._ws_dot.create_oval(0,0,6,6,fill=c("text3"),outline="",tags="dot")
        self._ws_lbl=tk.Label(ws_frame,text="Déconnecté",bg=c("bg2"),fg=c("text3"),font=("Segoe UI",9))
        self._ws_lbl.pack(side="left",padx=(0,6),pady=3)

        # ── Shell ────────────────────────────────────────────────────────────
        shell=tk.Frame(self.root,bg=c("bg0")); shell.pack(fill="both",expand=True)

        # Sidebar
        self._sidebar=tk.Frame(shell,bg=c("bg1"),width=48); self._sidebar.pack(side="left",fill="y"); self._sidebar.pack_propagate(False)
        tk.Frame(self._sidebar,bg=c("border"),width=1).place(relx=1,rely=0,relheight=1)
        self._sb_btns={}

        # Contenu
        self._views_frame=tk.Frame(shell,bg=c("bg0")); self._views_frame.pack(side="left",fill="both",expand=True)

        # Vues
        self._views={}
        for name in ("device","metrics","profiles","serial","settings"):
            f=tk.Frame(self._views_frame,bg=c("bg0")); self._views[name]=f

        # Drawers
        self._btn_drawer=Drawer(shell,width=320)
        self._pot_drawer=Drawer(shell,width=320)

        self._build_sidebar()
        self._build_view_device()
        self._build_view_metrics()
        self._build_view_profiles()
        self._build_view_serial()
        self._build_view_settings()

        self._switch_view("device")

    def _build_sidebar(self):
        TABS=[("🎛","device","Device"),("📊","metrics","Métriques"),("◈","profiles","Profils"),
              ("⌨","serial","Serial"),("⚙","settings","Paramètres")]
        tk.Frame(self._sidebar,height=8,bg=c("bg1")).pack()
        for icon,name,tip in TABS:
            btn=tk.Label(self._sidebar,text=icon,bg=c("bg1"),fg=c("text3"),
                font=("Segoe UI Emoji",15),cursor="hand2",width=3,pady=7)
            btn.pack()
            self._sb_btns[name]=btn
            btn.bind("<Button-1>",lambda e,n=name:self._switch_view(n))
            btn.bind("<Enter>",lambda e,b=btn,n=name: b.configure(fg=c("text") if self._active_view!=n else c("accent")))
            btn.bind("<Leave>",lambda e,b=btn,n=name: b.configure(fg=c("accent") if self._active_view==n else c("text3")))
            if name in ("serial","settings"):
                if name=="serial":
                    separator(self._sidebar,orient="h",padx=12,pady=3)

    def _switch_view(self,name):
        for n,f in self._views.items(): f.pack_forget()
        self._views[name].pack(fill="both",expand=True)
        self._active_view=name
        titles={"device":"Device","metrics":"Métriques","profiles":"Profils","serial":"Serial Monitor","settings":"Paramètres"}
        self._view_title.configure(text=titles.get(name,name))
        for n,b in self._sb_btns.items():
            b.configure(fg=c("accent") if n==name else c("text3"),
                bg=c("adim") if n==name else c("bg1"))
        if name=="profiles": self._refresh_profiles()
        if name=="metrics":  self._refresh_metrics_ui()

    # ══════════════════════════════════════════════════════════════════════════
    # VUE DEVICE — device card centrée avec boutons, LEDs, knobs
    # ══════════════════════════════════════════════════════════════════════════
    def _build_view_device(self):
        f=self._views["device"]
        # Zone de défilement
        canvas=tk.Canvas(f,bg=c("bg0"),highlightthickness=0)
        canvas.pack(fill="both",expand=True)
        # La device card est centrée dans le canvas
        self._device_canvas=canvas
        # Frame container centré
        self._device_outer=tk.Frame(canvas,bg=c("bg0"))
        self._device_outer_id=canvas.create_window(0,0,window=self._device_outer,anchor="nw")
        canvas.bind("<Configure>",self._center_device_card)

        # ── Device card ──────────────────────────────────────────────────────
        card=tk.Frame(self._device_outer,bg=c("bg2"),highlightthickness=1,
            highlightbackground=c("border"))
        card.pack(padx=24,pady=24)
        self._device_card=card

        # Header de la card
        hdr=tk.Frame(card,bg=c("bg2")); hdr.pack(fill="x",padx=26,pady=(20,12))
        tk.Label(hdr,text="Imperium — ESP32-S3",bg=c("bg2"),fg=c("text3"),
            font=("Segoe UI",8),letter_spacing=2).pack(side="left")
        self._dev_dot=tk.Canvas(hdr,width=7,height=7,bg=c("bg2"),highlightthickness=0); self._dev_dot.pack(side="right",padx=(0,4))
        self._dev_dot.create_oval(0,0,7,7,fill=c("text3"),outline="",tags="d")
        self._dev_lbl=tk.Label(hdr,text="—",bg=c("bg2"),fg=c("text3"),font=("Segoe UI",9)); self._dev_lbl.pack(side="right")

        # Switcher de profil
        psw=tk.Frame(card,bg=c("bg3"),highlightthickness=1,highlightbackground=c("border"))
        psw.pack(fill="x",padx=26,pady=(0,10))
        tk.Label(psw,text="Profil",bg=c("bg3"),fg=c("text3"),font=("Segoe UI",8)).pack(side="left",padx=(10,4),pady=7)
        self._profile_lbl=tk.Label(psw,text="Global",bg=c("bg3"),fg=c("accent"),font=("Segoe UI",11,"bold"))
        self._profile_lbl.pack(side="left",padx=2)
        for txt,cmd in [("‹",self._prev_profile),("›",self._next_profile)]:
            btn=tk.Label(psw,text=txt,bg=c("bg3"),fg=c("text3"),cursor="hand2",
                font=("Segoe UI",13),padx=6,pady=3)
            btn.pack(side="right",padx=2,pady=4)
            btn.bind("<Button-1>",lambda e,fn=cmd:fn())
            btn.bind("<Enter>",lambda e,b=btn:b.configure(bg=c("bg4"),fg=c("text")))
            btn.bind("<Leave>",lambda e,b=btn:b.configure(bg=c("bg3"),fg=c("text3")))

        # Grille boutons 4×2
        self._btn_grid=tk.Frame(card,bg=c("bg2")); self._btn_grid.pack(padx=26,pady=(0,10))

        # Zone LED (LED strips + USB indicators)
        self._led_zone_frame=tk.Frame(card,bg=c("bg2")); self._led_zone_frame.pack(fill="x",padx=26,pady=(0,10))

        # Rangée potards
        self._pots_row=tk.Frame(card,bg=c("bg2")); self._pots_row.pack(padx=26,pady=(0,20))

    def _center_device_card(self, event):
        cw=event.width; ch=event.height
        self._device_canvas.itemconfig(self._device_outer_id,width=cw)
        self._device_outer.configure(width=cw)
        # Centrer la card
        self._device_canvas.update_idletasks()
        card_w=self._device_card.winfo_reqwidth()
        card_h=self._device_card.winfo_reqheight()
        x=max(0,(cw-card_w)//2); y=max(0,(ch-card_h)//2)
        self._device_canvas.coords(self._device_outer_id,x,y)

    def _refresh_device(self):
        self._btn_canvases.clear()
        self._pot_widgets.clear()
        self._led_widgets.clear()
        for w in self._btn_grid.winfo_children(): w.destroy()
        for w in self._led_zone_frame.winfo_children(): w.destroy()
        for w in self._pots_row.winfo_children(): w.destroy()
        self._build_btn_grid()
        self._build_led_zone()
        self._build_pots_row()

    # ── Grille boutons ────────────────────────────────────────────────────────
    def _build_btn_grid(self):
        profile=self.cfg.active()
        for i in range(8):
            r,col=divmod(i,4)
            b=profile["buttons"].get(str(i),{})
            cell=self._make_btn_card(self._btn_grid,i,b)
            cell.grid(row=r,column=col,padx=4,pady=4)
            self._btn_canvases[i]=cell

    def _make_btn_card(self,parent,idx,btn_data):
        BSIZE=86
        outer=tk.Frame(parent,bg=c("border"),highlightthickness=0)
        frame=tk.Canvas(outer,bg=c("card"),width=BSIZE,height=BSIZE,highlightthickness=0,cursor="hand2")
        frame.pack(padx=1,pady=1)
        # Numéro
        frame.create_text(8,8,text=str(idx+1),fill=c("text3"),font=("Courier",7),anchor="nw")
        # Icône
        icon=btn_data.get("icon","⭐") or "⭐"
        frame.create_text(BSIZE//2,BSIZE//2-10,text=icon,fill=c("text"),font=("Segoe UI Emoji",18),anchor="center",tags="icon")
        # Label
        lbl=(btn_data.get("label") or f"Btn {idx+1}")[:12]
        frame.create_text(BSIZE//2,BSIZE-18,text=lbl,fill=c("text2"),font=("Segoe UI",7),anchor="center",tags="lbl",width=BSIZE-8)
        # Barre accent en bas
        frame.create_rectangle(8,BSIZE-4,BSIZE-8,BSIZE-2,fill=c("accent"),outline="",tags="bar",stipple="")
        # Compteur actions
        n=len(btn_data.get("press",[]))
        if n: frame.create_text(BSIZE-6,8,text=str(n),fill=c("accent"),font=("Courier",7),anchor="ne",tags="cnt")

        frame.bind("<Button-1>",lambda e,i=idx:self._open_btn_drawer(i))
        frame.bind("<Enter>",lambda e,f=frame,o=outer:self._btn_hover(f,o,True))
        frame.bind("<Leave>",lambda e,f=frame,o=outer:self._btn_hover(f,o,False))
        frame._idx=idx; frame._outer=outer
        return outer

    def _btn_hover(self,frame,outer,enter):
        if enter:
            outer.configure(bg=c("border") if c("border") else c("accent"))
            frame.configure(bg=c("bg3"))
        else:
            frame.configure(bg=c("card"))

    def _flash_btn(self,idx):
        if idx not in self._btn_canvases: return
        outer=self._btn_canvases[idx]
        f=outer.winfo_children()[0] if outer.winfo_children() else None
        if not f: return
        orig=c("card"); flash=c("accent")
        f.configure(bg=flash)
        self.root.after(150,lambda:f.configure(bg=orig) if f.winfo_exists() else None)

    # ── LED strips + USB ──────────────────────────────────────────────────────
    def _build_led_zone(self):
        f=self._led_zone_frame
        # 2 colonnes de 2 LEDs, USB au centre
        left=tk.Frame(f,bg=c("bg2")); left.pack(side="left",fill="both",expand=True)
        right=tk.Frame(f,bg=c("bg2")); right.pack(side="right",fill="both",expand=True)

        # USB indicators
        usb_frame=tk.Frame(f,bg=c("bg2"),width=20); usb_frame.pack(side="left",fill="y",padx=4)
        usb_frame.pack_propagate(False)
        self._usb_slots=[]
        for slot in range(2):
            cv=tk.Canvas(usb_frame,width=16,height=28,bg=c("bg2"),highlightthickness=1,
                highlightbackground=c("border"),cursor="hand2")
            cv.pack(pady=2)
            # Corps USB-C
            cv.create_rectangle(2,2,14,26,fill=c("bg3"),outline="",tags="body")
            cv.create_oval(5,4,11,8,fill=c("text3"),outline="",tags="dot")
            cv.create_rectangle(6,10,10,22,fill=c("bg4"),outline="",tags="plug")
            cv.bind("<Button-1>",lambda e,s=slot:self._show_usb_info(s))
            cv.bind("<Enter>",lambda e,cv=cv:cv.configure(highlightbackground=c("accent")))
            cv.bind("<Leave>",lambda e,cv=cv:cv.configure(highlightbackground=c("border")))
            self._usb_slots.append(cv)

        led_keys=[("cpu","left",0),("ram","left",1),("gpu_usage","right",2),("ssd_usage","right",3)]
        METRICS_ICONS={"cpu":"🔲","ram":"🧠","gpu_usage":"🎮","ssd_usage":"💾"}
        strips_cfg=self.cfg.data.get("led_strips",{})
        for _,side,i in led_keys:
            key=strips_cfg.get(str(i),{}).get("metric",["cpu","ram","gpu_usage","ssd_usage"][i])
            parent=left if side=="left" else right
            strip=self._make_led_strip(parent,i,key)
            strip.pack(fill="x",pady=2,padx=2)
            self._led_widgets[i]=strip

    def _make_led_strip(self,parent,idx,metric_key):
        frame=tk.Frame(parent,bg=c("bg4"),height=30,cursor="hand2",
            highlightthickness=1,highlightbackground=c("border"))
        frame.pack_propagate(False)
        # Glow (barre colorée)
        glow=tk.Frame(frame,bg=c("bg4")); glow.place(x=0,y=0,relwidth=0,relheight=1)
        # Labels
        icon,name=next(((m[1],m[2]) for m in LED_METRICS if m[0]==metric_key),("📊",metric_key.upper()))
        lbl=tk.Label(frame,text=f"{icon} {name[:14]}",bg=c("bg4"),fg="white",
            font=("Segoe UI",8,"bold")); lbl.place(x=8,rely=.5,anchor="w")
        pct=tk.Label(frame,text="—",bg=c("bg4"),fg="white",
            font=("Courier",9,"bold")); pct.place(relx=1,x=-8,rely=.5,anchor="e")
        frame._glow=glow; frame._lbl=lbl; frame._pct=pct; frame._metric=metric_key; frame._idx=idx
        frame.bind("<Button-1>",lambda e,i=idx:self._show_led_picker(i))
        for w in (glow,lbl,pct):
            w.bind("<Button-1>",lambda e,i=idx:self._show_led_picker(i))
            w.bind("<Enter>",lambda e,f=frame:f.configure(highlightbackground=c("accent")))
            w.bind("<Leave>",lambda e,f=frame:f.configure(highlightbackground=c("border")))
        return frame

    def _update_led_strips(self):
        m=self._metrics
        strips_cfg=self.cfg.data.get("led_strips",{})
        for i,strip in self._led_widgets.items():
            if not strip.winfo_exists(): continue
            key=strips_cfg.get(str(i),{}).get("metric",["cpu","ram","gpu_usage","ssd_usage"][i])
            # Trouver la métrique
            meta=next((x for x in LED_METRICS if x[0]==key),None)
            raw=m.get(key,0) or 0
            if meta and meta[3]:  # pct=True
                pct=max(0,min(100,int(raw)))
            elif meta:
                cap=meta[4] or 100
                pct=max(0,min(100,int(raw/cap*100)))
            else:
                pct=0
            # Couleur dégradée
            if pct<50:
                h=int(142-(142-48)*(pct/50))
            else:
                h=int(48-(48-4)*((pct-50)/50))
            # HSL → RGB approximatif
            import colorsys
            r,g,b_=colorsys.hls_to_rgb(h/360,.5,.85)
            color=f"#{int(r*255):02x}{int(g*255):02x}{int(b_*255):02x}"
            # Texte
            if meta and meta[5]:
                txt=f"{round(raw)}{meta[5]}"
            else:
                txt=f"{pct}%"
            alpha=0.2+(pct/100)*0.7
            strip._glow.place(x=0,y=0,relwidth=pct/100,relheight=1)
            strip._glow.configure(bg=color)
            strip._pct.configure(text=txt)
            # Mettre à jour label si la métrique a changé
            icon,name=next(((x[1],x[2]) for x in LED_METRICS if x[0]==key),("📊",key))
            strip._lbl.configure(text=f"{icon} {name[:14]}")
            strip._metric=key

    def _show_led_picker(self,idx):
        win=tk.Toplevel(self.root); win.title(f"LED {idx+1} — Choisir la métrique")
        win.geometry("280x400"); win.configure(bg=c("bg1")); win.grab_set()
        win.resizable(False,False)
        tk.Label(win,text=f"Métrique — LED {idx+1}",bg=c("bg1"),fg=c("text"),
            font=("Segoe UI",11,"bold")).pack(pady=(12,8))
        strips_cfg=self.cfg.data.get("led_strips",{}); cur=strips_cfg.get(str(idx),{}).get("metric","cpu")
        for key,icon,name,*_ in LED_METRICS:
            is_cur=key==cur
            row=tk.Frame(win,bg=c("accent" if is_cur else "bg2"),cursor="hand2")
            row.pack(fill="x",padx=12,pady=1)
            tk.Label(row,text=f"{icon} {name}",bg=row.cget("bg"),fg=c("text"),
                font=("Segoe UI",9),anchor="w",padx=10,pady=6).pack(fill="x")
            row.bind("<Button-1>",lambda e,k=key,w=win:self._pick_led_metric(idx,k,w))
            for child in row.winfo_children():
                child.bind("<Button-1>",lambda e,k=key,w=win:self._pick_led_metric(idx,k,w))
                child.bind("<Enter>",lambda e,r=row,c_=c("bg3"):r.configure(bg=c_))
                child.bind("<Leave>",lambda e,r=row,orig=row.cget("bg"):r.configure(bg=orig))

    def _pick_led_metric(self,idx,key,win):
        if not self.cfg.data.get("led_strips"): self.cfg.data["led_strips"]={}
        if str(idx) not in self.cfg.data["led_strips"]: self.cfg.data["led_strips"][str(idx)]={}
        self.cfg.data["led_strips"][str(idx)]["metric"]=key
        self.cfg.save(); win.destroy()
        self._update_led_strips()
        self._toast(f"✓ LED {idx+1} → {next((x[2] for x in LED_METRICS if x[0]==key),key)}")

    def _show_usb_info(self,slot):
        ok=self.transport.is_connected(slot)
        port=self.transport._port_names[slot] or "—"
        win=tk.Toplevel(self.root); win.title(f"Port USB {slot+1}")
        win.geometry("340x200"); win.configure(bg=c("bg1")); win.grab_set()
        win.resizable(False,False)
        tk.Label(win,text=f"Port USB {slot+1}",bg=c("bg1"),fg=c("text"),
            font=("Segoe UI",11,"bold")).pack(pady=(12,8))
        info_frame=tk.Frame(win,bg=c("bg2")); info_frame.pack(fill="x",padx=16,pady=4)
        for lbl,val,col in [("État","● Connecté" if ok else "○ Non connecté",c("green") if ok else c("text3")),
                             ("Port COM",port,c("text")),("Vitesse","115200 bauds" if ok else "—",c("text2"))]:
            row=tk.Frame(info_frame,bg=c("bg2")); row.pack(fill="x",padx=8,pady=3)
            tk.Label(row,text=lbl+":",bg=c("bg2"),fg=c("text3"),font=("Segoe UI",9),width=10,anchor="w").pack(side="left")
            tk.Label(row,text=val,bg=c("bg2"),fg=col,font=("Segoe UI",9,"bold")).pack(side="left",padx=4)
        btn_row=tk.Frame(win,bg=c("bg1")); btn_row.pack(fill="x",padx=16,pady=12)
        styled_btn(btn_row,"🔄 Détecter",lambda:self._refresh_ports_and_close(win),padx=10,pady=5).pack(side="left",padx=4)
        styled_btn(btn_row,"⚙ Configurer",lambda:(win.destroy(),self._switch_view("settings")),style="primary",padx=10,pady=5).pack(side="left",padx=4)

    def _refresh_ports_and_close(self,win):
        ports=[p.device for p in serial.tools.list_ports.comports()] if SERIAL_OK else []
        self._toast(f"Ports : {', '.join(ports) if ports else 'Aucun'}")
        win.destroy()

    # ── Potards (knobs SVG-like avec Canvas) ──────────────────────────────────
    def _build_pots_row(self):
        profile=self.cfg.active()
        for i in range(4):
            p=profile["pots"].get(str(i),{})
            cell=self._make_pot_card(self._pots_row,i,p)
            cell.pack(side="left",padx=5,pady=4)
            self._pot_widgets[i]=cell

    def _make_pot_card(self,parent,idx,pot_data):
        frame=tk.Frame(parent,bg=c("bg2")); 
        SZ=60
        cv=tk.Canvas(frame,width=SZ,height=SZ,bg=c("bg2"),highlightthickness=0,cursor="ns-resize")
        cv.pack()
        # Corps du knob
        cv.create_oval(2,2,SZ-2,SZ-2,fill=c("bg3"),outline=c("border"),width=1.5,tags="body")
        cv.create_oval(8,8,SZ-8,SZ-8,fill=c("bg0"),outline=c("border"),width=1,tags="inner")
        R=SZ//2-4; cx=SZ//2; cy=SZ//2
        circumference=2*math.pi*R
        # Arc accent (stroke-dasharray style, simulé via arc)
        cv.create_arc(cx-R,cy-R,cx+R,cy+R,start=135,extent=0,style="arc",
            outline=c("accent"),width=2.5,tags="arc")
        # Ligne indicatrice
        angle_deg=220; angle_rad=math.radians(angle_deg)
        lx=cx+R*0.6*math.sin(angle_rad); ly=cy-R*0.6*math.cos(angle_rad)
        cv.create_line(cx,cy,lx,ly,fill=c("accent"),width=2,tags="line",capstyle="round")
        cv.create_oval(cx-3,cy-3,cx+3,cy+3,fill=c("accent"),outline="",tags="center")
        # Labels
        name=(pot_data.get("name") or f"Pot {idx+1}")[:9]
        tk.Label(frame,text=name,bg=c("bg2"),fg=c("text3"),font=("Segoe UI",7),padx=0).pack()
        pct_lbl=tk.Label(frame,text="0%",bg=c("bg2"),fg=c("accent"),font=("Courier",8,"bold"))
        pct_lbl.pack()

        frame._cv=cv; frame._pct_lbl=pct_lbl; frame._idx=idx
        frame._R=R; frame._cx=cx; frame._cy=cy; frame._SZ=SZ

        # Interactions
        drag_state={"y":0,"pct":0,"dragging":False}
        def on_down(e,s=drag_state,i=idx): s["y"]=e.y_root; s["pct"]=self._pot_pcts[i]; s["dragging"]=True
        def on_move(e,s=drag_state,i=idx):
            if not s["dragging"]: return
            delta=(s["y"]-e.y_root)*0.6
            self._pot_pcts[i]=max(0,min(100,s["pct"]+delta))
            self._update_knob(i)
        def on_up(e,s=drag_state,i=idx):
            if s["dragging"]: s["dragging"]=False; self._open_pot_drawer(i)
        def on_wheel(e,i=idx):
            self._pot_pcts[i]=max(0,min(100,self._pot_pcts[i]+(4 if e.delta>0 else -4)))
            self._update_knob(i)
        def on_click(e,i=idx): self._open_pot_drawer(i)

        cv.bind("<ButtonPress-1>",on_down); cv.bind("<B1-Motion>",on_move)
        cv.bind("<ButtonRelease-1>",on_up); cv.bind("<MouseWheel>",on_wheel)
        cv.bind("<Double-Button-1>",on_click)

        self._update_knob_widget(idx,frame,self._pot_pcts[idx])
        return frame

    def _update_knob(self,idx):
        if idx in self._pot_widgets:
            self._update_knob_widget(idx,self._pot_widgets[idx],self._pot_pcts[idx])

    def _update_knob_widget(self,idx,frame,pct):
        cv=frame._cv; R=frame._R; cx=frame._cx; cy=frame._cy
        p=max(0,min(100,pct))/100
        # Arc (270° total, commence à 135° et va dans le sens horaire)
        extent=p*270
        cv.itemconfig("arc",start=135,extent=extent)
        # Ligne indicatrice
        angle_deg=220+p*270; angle_rad=math.radians(angle_deg)
        lx=cx+R*0.6*math.sin(angle_rad); ly=cy-R*0.6*math.cos(angle_rad)
        cv.coords("line",cx,cy,lx,ly)
        frame._pct_lbl.configure(text=f"{int(pct)}%")

    def _animate_pot(self,idx,pct):
        self._pot_pcts[idx]=max(0,min(100,pct))
        self._update_knob(idx)

    # ══════════════════════════════════════════════════════════════════════════
    # DRAWER BOUTON
    # ══════════════════════════════════════════════════════════════════════════
    def _open_btn_drawer(self,idx):
        self._sel_btn=idx
        profile=self.cfg.active()
        btn=profile["buttons"].get(str(idx),{})
        self._btn_drawer.open(f"Bouton {idx+1} — {btn.get('label','')}")
        self._render_btn_drawer(idx)

    def _render_btn_drawer(self,idx):
        profile=self.cfg.active()
        btn=profile["buttons"].get(str(idx),{})
        self._btn_drawer.clear_body()
        body=self._btn_drawer.body

        # ── Tabs press/long/double ────────────────────────────────────────────
        tab_frame=tk.Frame(body,bg=c("bg2"),highlightthickness=1,highlightbackground=c("border"))
        tab_frame.pack(fill="x",padx=0,pady=(0,12))
        self._ev_tabs={}
        for ev,label in [("press","Simple"),("long_press","Long"),("double_click","Double")]:
            is_cur=self._ev_type==ev
            tab=tk.Label(tab_frame,text=label,bg=c("bg4" if is_cur else "bg2"),
                fg=c("text" if is_cur else "text3"),font=("Segoe UI",10),
                padx=0,pady=5,cursor="hand2")
            tab.pack(side="left",fill="x",expand=True)
            tab.bind("<Button-1>",lambda e,ev_=ev,i=idx:(self.__setattr__("_ev_type",ev_),self._render_btn_drawer(i)))
            self._ev_tabs[ev]=tab

        # ── Apparence ────────────────────────────────────────────────────────
        sec=self._drawer_section(body,"Apparence")
        row=tk.Frame(sec,bg=c("bg1")); row.pack(fill="x",pady=(0,8))
        tk.Label(row,text="Icône :",bg=c("bg1"),fg=c("text2"),font=("Segoe UI",9),width=8,anchor="w").pack(side="left",padx=8)
        icon_var=tk.StringVar(value=btn.get("icon","⭐"))
        icon_e=styled_entry(row,textvariable=icon_var,width=5,font=("Segoe UI Emoji",14))
        icon_e.pack(side="left")
        icon_var.trace_add("write",lambda *_,i=idx,v=icon_var:(self._set_btn_field(i,"icon",v.get()),self._refresh_btn_card(i)))

        row2=tk.Frame(sec,bg=c("bg1")); row2.pack(fill="x",pady=(0,4))
        tk.Label(row2,text="Nom :",bg=c("bg1"),fg=c("text2"),font=("Segoe UI",9),width=8,anchor="w").pack(side="left",padx=8)
        lbl_var=tk.StringVar(value=btn.get("label",f"Bouton {idx+1}"))
        lbl_e=styled_entry(row2,textvariable=lbl_var,font=("Segoe UI",10))
        lbl_e.pack(side="left",fill="x",expand=True,padx=(0,8))
        lbl_var.trace_add("write",lambda *_,i=idx,v=lbl_var:(self._set_btn_field(i,"label",v.get()),self._refresh_btn_card(i)))

        # ── Actions ──────────────────────────────────────────────────────────
        sec2=self._drawer_section(body,f"Actions — {self._ev_type}")
        acts=btn.get(self._ev_type,[])
        chips_frame=tk.Frame(sec2,bg=c("bg1")); chips_frame.pack(fill="x",pady=(0,6))
        if not acts:
            tk.Label(chips_frame,text="Aucune action",bg=c("bg1"),fg=c("text3"),
                font=("Segoe UI",9,"italic")).pack(anchor="w",padx=8,pady=4)
        for j,act in enumerate(acts):
            adef=next((a for a in ALL_ACTIONS if a["type"]==act.get("type")),None)
            chip=tk.Frame(chips_frame,bg=c("bg3"),highlightthickness=1,highlightbackground=c("border"))
            chip.pack(fill="x",pady=1,padx=4)
            icon=adef["icon"] if adef else "?"
            name=adef["name"] if adef else act.get("type","?")
            tk.Label(chip,text=f"{icon} {name}",bg=c("bg3"),fg=c("text"),
                font=("Segoe UI",9),anchor="w").pack(side="left",padx=8,pady=5,fill="x",expand=True)
            del_btn=tk.Label(chip,text="✕",bg=c("bg3"),fg=c("text3"),cursor="hand2",font=("Segoe UI",10),padx=6)
            del_btn.pack(side="right",padx=4)
            del_btn.bind("<Button-1>",lambda e,j_=j,i=idx:self._del_action(i,j_))
            del_btn.bind("<Enter>",lambda e,b=del_btn:b.configure(fg=c("red")))
            del_btn.bind("<Leave>",lambda e,b=del_btn:b.configure(fg=c("text3")))

        add_btn=tk.Label(sec2,text="＋ Ajouter une action",bg=c("bg1"),fg=c("text3"),
            cursor="hand2",font=("Segoe UI",10),relief="flat",
            highlightthickness=1,highlightbackground=c("border"),pady=6)
        add_btn.pack(fill="x",padx=4,pady=2)
        add_btn.bind("<Button-1>",lambda e,i=idx:self._open_action_picker(i))
        add_btn.bind("<Enter>",lambda e:(add_btn.configure(fg=c("accent"),highlightbackground=c("accent"))))
        add_btn.bind("<Leave>",lambda e:(add_btn.configure(fg=c("text3"),highlightbackground=c("border"))))

        # ── Test ──────────────────────────────────────────────────────────────
        sec3=self._drawer_section(body,"Tester")
        test_row=tk.Frame(sec3,bg=c("bg1")); test_row.pack(fill="x")
        for ev,label in [("press","▶ Simple"),("long_press","⏳ Long"),("double_click","⚡ Double")]:
            btn_t=styled_btn(test_row,label,lambda ev_=ev,i=idx:self._test_btn(i,ev_),padx=8,pady=4)
            btn_t.pack(side="left",padx=3,pady=6)

    def _drawer_section(self,parent,title):
        tk.Label(parent,text=title,bg=c("bg0"),fg=c("text3"),font=("Segoe UI",8,"bold"),
            anchor="w",padx=8,pady=4).pack(fill="x",padx=0,pady=(8,0))
        sec=tk.Frame(parent,bg=c("bg1")); sec.pack(fill="x",padx=0,pady=(0,2))
        return sec

    def _set_btn_field(self,idx,field,val):
        p=self.cfg.data.get("active_profile","default")
        if p not in self.cfg.data["profiles"]: return
        if "buttons" not in self.cfg.data["profiles"][p]: self.cfg.data["profiles"][p]["buttons"]={}
        if str(idx) not in self.cfg.data["profiles"][p]["buttons"]:
            self.cfg.data["profiles"][p]["buttons"][str(idx)]={"icon":"⭐","label":f"Btn {idx+1}","press":[],"long_press":[],"double_click":[]}
        self.cfg.data["profiles"][p]["buttons"][str(idx)][field]=val
        self._autosave()

    def _refresh_btn_card(self,idx):
        if idx not in self._btn_canvases: return
        profile=self.cfg.active()
        btn=profile["buttons"].get(str(idx),{})
        outer=self._btn_canvases[idx]
        f=outer.winfo_children()[0] if outer.winfo_children() else None
        if not f: return
        f.itemconfig("icon",text=btn.get("icon","⭐") or "⭐")
        f.itemconfig("lbl",text=(btn.get("label") or f"Btn {idx+1}")[:12])
        f.delete("cnt")
        n=len(btn.get("press",[]))
        if n: f.create_text(86-6,8,text=str(n),fill=c("accent"),font=("Courier",7),anchor="ne",tags="cnt")

    def _del_action(self,btn_idx,act_idx):
        p=self.cfg.data.get("active_profile","default")
        try:
            self.cfg.data["profiles"][p]["buttons"][str(btn_idx)][self._ev_type].pop(act_idx)
            self._autosave(); self._render_btn_drawer(btn_idx)
        except: pass

    def _test_btn(self,idx,ev):
        profile=self.cfg.active()
        acts=profile["buttons"].get(str(idx),{}).get(ev,[])
        threading.Thread(target=self.engine.run,args=(acts,),daemon=True).start()
        self._add_log("rx",ev,f"BTN{idx+1}")
        self._toast(f"Test BTN{idx+1} [{ev}] → {len(acts)} action(s)")

    # ══════════════════════════════════════════════════════════════════════════
    # SÉLECTEUR D'ACTION (modal)
    # ══════════════════════════════════════════════════════════════════════════
    def _open_action_picker(self,btn_idx):
        win=tk.Toplevel(self.root); win.title("Ajouter une action")
        win.geometry("520x560"); win.configure(bg=c("bg1")); win.grab_set()

        # Header
        tk.Label(win,text="Ajouter une action",bg=c("bg1"),fg=c("text"),
            font=("Segoe UI",12,"bold")).pack(pady=(14,4),padx=20,anchor="w")

        # Recherche
        search_var=tk.StringVar()
        sef=styled_entry(win,textvariable=search_var,font=("Segoe UI",10))
        sef.pack(fill="x",padx=16,pady=(0,8))
        sef._entry.insert(0,"🔍 Rechercher...")
        sef._entry.bind("<FocusIn>",lambda e:(sef._entry.delete(0,"end") if sef._entry.get().startswith("🔍") else None))

        # Liste scrollable
        canvas=tk.Canvas(win,bg=c("bg1"),highlightthickness=0)
        sb=tk.Scrollbar(win,orient="vertical",command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right",fill="y"); canvas.pack(fill="both",expand=True,padx=16,pady=4)
        inner=tk.Frame(canvas,bg=c("bg1"))
        win_id=canvas.create_window((0,0),window=inner,anchor="nw")
        inner.bind("<Configure>",lambda e:canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",lambda e:canvas.itemconfig(win_id,width=e.width))
        canvas.bind("<MouseWheel>",lambda e:canvas.yview_scroll(int(-1*(e.delta/120)),"units"))

        def render(q=""):
            for w in inner.winfo_children(): w.destroy()
            cats={}
            for a in ALL_ACTIONS:
                if q and q.lower() not in a["name"].lower() and q.lower() not in a["cat"].lower(): continue
                cats.setdefault(a["cat"],[]).append(a)
            for cat,acts in sorted(cats.items()):
                tk.Label(inner,text=cat,bg=c("bg1"),fg=c("accent"),
                    font=("Segoe UI",9,"bold"),anchor="w").pack(fill="x",pady=(8,2),padx=4)
                for a in acts:
                    row=tk.Frame(inner,bg=c("bg2"),cursor="hand2",
                        highlightthickness=1,highlightbackground=c("border"))
                    row.pack(fill="x",pady=1)
                    tk.Label(row,text=f"{a['icon']} {a['name']}",bg=c("bg2"),fg=c("text"),
                        font=("Segoe UI",9),anchor="w",padx=10,pady=6).pack(side="left")
                    if a.get("desc"):
                        tk.Label(row,text=a["desc"],bg=c("bg2"),fg=c("text3"),
                            font=("Segoe UI",8)).pack(side="left",padx=4)
                    row.bind("<Button-1>",lambda e,a_=a,w=win:self._on_action_picked(a_,btn_idx,w))
                    for child in row.winfo_children():
                        child.bind("<Button-1>",lambda e,a_=a,w=win:self._on_action_picked(a_,btn_idx,w))
                        child.bind("<Enter>",lambda e,r=row:r.configure(bg=c("adim"),highlightbackground=c("accent")))
                        child.bind("<Leave>",lambda e,r=row:r.configure(bg=c("bg2"),highlightbackground=c("border")))

        search_var.trace_add("write",lambda *_:render(search_var.get() if not search_var.get().startswith("🔍") else ""))
        render()

    def _on_action_picked(self,act_def,btn_idx,picker_win):
        if not act_def.get("params"):
            picker_win.destroy()
            self._finalize_action(act_def,{},btn_idx)
            return
        picker_win.destroy()
        # Fenêtre paramètres
        pw=tk.Toplevel(self.root); pw.title(act_def["name"])
        pw.geometry("380x320"); pw.configure(bg=c("bg1")); pw.grab_set()
        tk.Label(pw,text=f"{act_def['icon']} {act_def['name']}",bg=c("bg1"),fg=c("text"),
            font=("Segoe UI",11,"bold")).pack(pady=(14,10),padx=16,anchor="w")
        entries={}
        for p in act_def["params"]:
            tk.Label(pw,text=p["lbl"]+":",bg=c("bg1"),fg=c("text2"),
                font=("Segoe UI",9),anchor="w").pack(fill="x",padx=16,pady=(4,0))
            var=tk.StringVar()
            ef=styled_entry(pw,textvariable=var,font=("Segoe UI",10))
            ef.pack(fill="x",padx=16,pady=(0,4))
            ef._entry.insert(0,p.get("ph",""))
            entries[p["key"]]=var
        def confirm():
            params={k:v.get() for k,v in entries.items()}
            pw.destroy(); self._finalize_action(act_def,params,btn_idx)
        styled_btn(pw,"✅ Confirmer",confirm,style="primary",padx=12,pady=6).pack(fill="x",padx=16,pady=10)

    def _finalize_action(self,act_def,params,btn_idx):
        action={"type":act_def["type"]}; action.update(params)
        p=self.cfg.data.get("active_profile","default")
        if "buttons" not in self.cfg.data["profiles"][p]: self.cfg.data["profiles"][p]["buttons"]={}
        if str(btn_idx) not in self.cfg.data["profiles"][p]["buttons"]:
            self.cfg.data["profiles"][p]["buttons"][str(btn_idx)]={"icon":"⭐","label":f"Btn {btn_idx+1}","press":[],"long_press":[],"double_click":[]}
        ev=self._ev_type
        if ev not in self.cfg.data["profiles"][p]["buttons"][str(btn_idx)]:
            self.cfg.data["profiles"][p]["buttons"][str(btn_idx)][ev]=[]
        self.cfg.data["profiles"][p]["buttons"][str(btn_idx)][ev].append(action)
        self._autosave(); self._render_btn_drawer(btn_idx); self._refresh_btn_card(btn_idx)
        self._toast(f"✓ Action ajoutée : {act_def['name']}")

    # ══════════════════════════════════════════════════════════════════════════
    # DRAWER POTARD
    # ══════════════════════════════════════════════════════════════════════════
    def _open_pot_drawer(self,idx):
        self._sel_pot=idx
        profile=self.cfg.active()
        pot=profile["pots"].get(str(idx),{})
        self._pot_drawer.open(f"Potard {idx+1} — {pot.get('name','')}")
        self._render_pot_drawer(idx)

    def _render_pot_drawer(self,idx):
        profile=self.cfg.active()
        pot=profile["pots"].get(str(idx),{})
        self._pot_drawer.clear_body()
        body=self._pot_drawer.body

        # Nom
        sec=self._drawer_section2(body,"Nom")
        name_var=tk.StringVar(value=pot.get("name",f"Pot {idx+1}"))
        ef=styled_entry(sec,textvariable=name_var,font=("Segoe UI",10))
        ef.pack(fill="x",padx=8,pady=6)
        name_var.trace_add("write",lambda *_,i=idx,v=name_var:self._set_pot_field(i,"name",v.get()))

        # Action
        sec2=self._drawer_section2(body,"Action assignée")
        cats_done=set()
        for action_key,icon,name,cat in POT_ACTS:
            cur_action=pot.get("action","volume_system")
            if cat not in cats_done:
                cats_done.add(cat)
                tk.Label(sec2,text=cat,bg=c("bg1"),fg=c("text3"),
                    font=("Segoe UI",8,"bold"),anchor="w",padx=8).pack(fill="x",pady=(6,2))
            is_sel=action_key==cur_action
            row=tk.Frame(sec2,bg=c("accent" if is_sel else "bg2"),cursor="hand2")
            row.pack(fill="x",pady=1,padx=4)
            tk.Label(row,text=f"{icon} {name}",bg=row.cget("bg"),fg=c("text"),
                font=("Segoe UI",9),anchor="w",padx=10,pady=5).pack(side="left")
            row.bind("<Button-1>",lambda e,k=action_key,i=idx:(self._set_pot_field(i,"action",k),self._render_pot_drawer(i)))
            for child in row.winfo_children():
                child.bind("<Button-1>",lambda e,k=action_key,i=idx:(self._set_pot_field(i,"action",k),self._render_pot_drawer(i)))
                if not is_sel:
                    child.bind("<Enter>",lambda e,r=row:r.configure(bg=c("bg3")))
                    child.bind("<Leave>",lambda e,r=row:r.configure(bg=c("bg2")))

        # Valeur actuelle
        sec3=self._drawer_section2(body,"Valeur actuelle")
        pct=int(self._pot_pcts[idx])
        tk.Label(sec3,text=f"{pct}%",bg=c("bg1"),fg=c("accent"),
            font=("Courier",12,"bold")).pack(anchor="w",padx=12,pady=2)
        bar_bg=tk.Frame(sec3,bg=c("bg3"),height=4); bar_bg.pack(fill="x",padx=8,pady=4)
        bar_fill=tk.Frame(bar_bg,bg=c("accent"),height=4)
        bar_fill.place(x=0,y=0,relwidth=pct/100,relheight=1)

        # Métriques live mini
        sec4=self._drawer_section2(body,"Métriques live")
        m=self._metrics
        for lbl,key,col in [("CPU",m.get("cpu",0),c("blue")),("RAM",m.get("ram",0),c("green")),
                              ("GPU",m.get("gpu_usage",0),c("purple")),("SSD",m.get("ssd_usage",0),c("yellow"))]:
            row=tk.Frame(sec4,bg=c("bg1")); row.pack(fill="x",padx=8,pady=2)
            tk.Label(row,text=lbl,bg=c("bg1"),fg=c("text2"),font=("Segoe UI",9),width=5,anchor="w").pack(side="left")
            bar=tk.Frame(row,bg=c("bg3"),height=3); bar.pack(side="left",fill="x",expand=True,padx=4)
            v=int(key or 0)
            tk.Frame(bar,bg=col,height=3).place(x=0,y=0,relwidth=v/100,relheight=1)
            tk.Label(row,text=f"{v}%",bg=c("bg1"),fg=col,font=("Courier",9),width=5).pack(side="right")

    def _drawer_section2(self,parent,title):
        tk.Label(parent,text=title,bg=c("bg0"),fg=c("text3"),font=("Segoe UI",8,"bold"),
            anchor="w",padx=8,pady=4).pack(fill="x")
        sec=tk.Frame(parent,bg=c("bg1")); sec.pack(fill="x",pady=(0,2))
        return sec

    def _set_pot_field(self,idx,field,val):
        p=self.cfg.data.get("active_profile","default")
        if "pots" not in self.cfg.data["profiles"][p]: self.cfg.data["profiles"][p]["pots"]={}
        if str(idx) not in self.cfg.data["profiles"][p]["pots"]:
            self.cfg.data["profiles"][p]["pots"][str(idx)]={"name":f"Pot {idx+1}","action":"volume_system"}
        self.cfg.data["profiles"][p]["pots"][str(idx)][field]=val
        self._autosave()

    # ══════════════════════════════════════════════════════════════════════════
    # VUE MÉTRIQUES
    # ══════════════════════════════════════════════════════════════════════════
    def _build_view_metrics(self):
        f=self._views["metrics"]
        canvas=tk.Canvas(f,bg=c("bg0"),highlightthickness=0)
        sb=tk.Scrollbar(f,orient="vertical",command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right",fill="y"); canvas.pack(fill="both",expand=True)
        self._metrics_inner=tk.Frame(canvas,bg=c("bg0"))
        win_id=canvas.create_window((0,0),window=self._metrics_inner,anchor="nw")
        self._metrics_inner.bind("<Configure>",lambda e:canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",lambda e:canvas.itemconfig(win_id,width=e.width))
        canvas.bind("<MouseWheel>",lambda e:canvas.yview_scroll(int(-1*(e.delta/120)),"units"))

    def _refresh_metrics_ui(self):
        m=self._metrics
        f=self._metrics_inner
        for w in f.winfo_children(): w.destroy()
        if not m: return

        tk.Label(f,text="Métriques système",bg=c("bg0"),fg=c("text"),
            font=("Segoe UI",13,"bold")).pack(anchor="w",padx=20,pady=(16,10))

        grid=tk.Frame(f,bg=c("bg0")); grid.pack(fill="x",padx=16)

        def metric_card(parent,row_,col_,title,pct_val,color,details,col_span=1):
            p=int(pct_val or 0)
            col_color=c("green") if p<50 else (c("yellow") if p<80 else c("red"))
            card=tk.Frame(parent,bg=c("bg1"),highlightthickness=1,highlightbackground=c("border"))
            card.grid(row=row_,column=col_,padx=5,pady=5,sticky="nsew",columnspan=col_span)
            hdr=tk.Frame(card,bg=c("bg1")); hdr.pack(fill="x",padx=14,pady=(12,4))
            dot=tk.Canvas(hdr,width=10,height=10,bg=c("bg1"),highlightthickness=0); dot.pack(side="left")
            dot.create_oval(0,0,10,10,fill=col_color,outline="")
            tk.Label(hdr,text=title,bg=c("bg1"),fg=c("text"),font=("Segoe UI",11,"bold")).pack(side="left",padx=6)
            tk.Label(hdr,text=f"{p}%",bg=c("bg1"),fg=col_color,font=("Courier",14,"bold")).pack(side="right")
            bar_bg=tk.Frame(card,bg=c("bg3"),height=5); bar_bg.pack(fill="x",padx=14,pady=(0,8))
            tk.Frame(bar_bg,bg=col_color,height=5).place(x=0,y=0,relwidth=p/100,relheight=1)
            for k,v in details:
                row=tk.Frame(card,bg=c("bg1")); row.pack(fill="x",padx=14,pady=1)
                tk.Label(row,text=k,bg=c("bg1"),fg=c("text3"),font=("Segoe UI",9),anchor="w").pack(side="left")
                tk.Label(row,text=str(v),bg=c("bg1"),fg=c("text"),font=("Courier",9,"bold")).pack(side="right")
            tk.Frame(card,height=8,bg=c("bg1")).pack()
            return card

        for i in range(4): grid.columnconfigure(i,weight=1)

        metric_card(grid,0,0,"CPU",m.get("cpu"),c("blue"),[
            ("Fréquence",f"{m.get('cpu_freq',0):.0f} MHz"),("Cœurs",str(m.get("cpu_cores","—"))),
            ("Température",f"{m.get('cpu_temp','—')}°C" if m.get("cpu_temp") else "—"),("Uptime",m.get("uptime","—"))])
        metric_card(grid,0,1,"RAM",m.get("ram"),c("green"),[
            ("Utilisée",f"{m.get('ram_used_gb',0)} GB"),("Totale",f"{m.get('ram_total_gb',0)} GB"),
            ("Libre",f"{round(m.get('ram_total_gb',0)-m.get('ram_used_gb',0),1)} GB")])
        metric_card(grid,0,2,"GPU",m.get("gpu_usage"),c("purple"),[
            ("Nom",(m.get("gpu_name","—") or "—")[:20]),("VRAM",f"{m.get('gpu_vram',0):.0f}%"),
            ("Température",f"{m.get('gpu_temp','—')}°C" if m.get("gpu_temp") else "—")])
        metric_card(grid,0,3,"Stockage",m.get("ssd_usage"),c("yellow"),[
            (d["mountpoint"],f"{d['used_gb']}/{d['total_gb']}GB") for d in m.get("disks",[])[:2]])

        # Réseau + Système
        nw=int(min(100,(m.get("net_down",0) or 0)/1024*10))
        metric_card(grid,1,0,"Réseau",nw,c("orange"),[
            ("↑ Upload",f"{m.get('net_up',0):.1f} KB/s"),("↓ Download",f"{m.get('net_down',0):.1f} KB/s")])
        sys_card=tk.Frame(grid,bg=c("bg1"),highlightthickness=1,highlightbackground=c("border"))
        sys_card.grid(row=1,column=1,columnspan=3,padx=5,pady=5,sticky="nsew")
        tk.Label(sys_card,text=f"🕐 {m.get('time','—')}",bg=c("bg1"),fg=c("text"),
            font=("Courier",14,"bold")).pack(anchor="w",padx=14,pady=(10,2))
        sys_row=tk.Frame(sys_card,bg=c("bg1")); sys_row.pack(fill="x",padx=14)
        for lbl,val in [("Date",m.get("date","—")),("Volume",f"{m.get('volume',0)}%"),("Uptime",m.get("uptime","—"))]:
            tk.Label(sys_row,text=f"{lbl}: ",bg=c("bg1"),fg=c("text3"),font=("Segoe UI",9)).pack(side="left")
            tk.Label(sys_row,text=str(val),bg=c("bg1"),fg=c("text"),font=("Segoe UI",9,"bold")).pack(side="left",padx=(0,12))

        # Top processus
        tk.Label(f,text="Top processus",bg=c("bg0"),fg=c("text2"),
            font=("Segoe UI",11,"bold")).pack(anchor="w",padx=20,pady=(12,4))
        procs_frame=tk.Frame(f,bg=c("bg1"),highlightthickness=1,highlightbackground=c("border"))
        procs_frame.pack(fill="x",padx=16,pady=(0,16))
        hdr=tk.Frame(procs_frame,bg=c("bg2")); hdr.pack(fill="x")
        tk.Label(hdr,text="Processus",bg=c("bg2"),fg=c("text3"),font=("Segoe UI",9,"bold"),anchor="w").pack(side="left",padx=10,pady=4,fill="x",expand=True)
        tk.Label(hdr,text="CPU",bg=c("bg2"),fg=c("blue"),font=("Segoe UI",9,"bold"),width=7).pack(side="left")
        tk.Label(hdr,text="MEM",bg=c("bg2"),fg=c("purple"),font=("Segoe UI",9,"bold"),width=7).pack(side="left",padx=8)
        for p in m.get("top_processes",[]):
            row=tk.Frame(procs_frame,bg=c("bg1")); row.pack(fill="x")
            tk.Frame(procs_frame,bg=c("border"),height=1).pack(fill="x")
            tk.Label(row,text=p["name"][:30],bg=c("bg1"),fg=c("text"),font=("Segoe UI",9),anchor="w").pack(side="left",padx=10,pady=3,fill="x",expand=True)
            tk.Label(row,text=f"{p['cpu']:.1f}%",bg=c("bg1"),fg=c("blue"),font=("Courier",9,"bold"),width=7).pack(side="left")
            tk.Label(row,text=f"{p['mem']:.1f}%",bg=c("bg1"),fg=c("purple"),font=("Courier",9,"bold"),width=7).pack(side="left",padx=8)

    # ══════════════════════════════════════════════════════════════════════════
    # VUE PROFILS
    # ══════════════════════════════════════════════════════════════════════════
    def _build_view_profiles(self):
        f=self._views["profiles"]
        top=tk.Frame(f,bg=c("bg0")); top.pack(fill="x",padx=20,pady=(14,8))
        tk.Label(top,text="Gestion des profils",bg=c("bg0"),fg=c("text"),
            font=("Segoe UI",13,"bold")).pack(side="left")
        styled_btn(top,"＋ Nouveau profil",self._new_profile,style="primary",padx=10,pady=4).pack(side="right")
        canvas=tk.Canvas(f,bg=c("bg0"),highlightthickness=0); canvas.pack(fill="both",expand=True)
        sb=tk.Scrollbar(f,orient="vertical",command=canvas.yview); sb.pack(side="right",fill="y")
        canvas.configure(yscrollcommand=sb.set)
        self._profiles_inner=tk.Frame(canvas,bg=c("bg0"))
        win_id=canvas.create_window((0,0),window=self._profiles_inner,anchor="nw")
        self._profiles_inner.bind("<Configure>",lambda e:canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",lambda e:canvas.itemconfig(win_id,width=e.width))

    def _refresh_profiles(self):
        f=self._profiles_inner
        for w in f.winfo_children(): w.destroy()
        active=self.cfg.data.get("active_profile","default")

        # Grid 3 colonnes
        grid=tk.Frame(f,bg=c("bg0")); grid.pack(fill="x",padx=20,pady=8)
        for col in range(3): grid.columnconfigure(col,weight=1)
        for i,(key,profile) in enumerate(self.cfg.data["profiles"].items()):
            r,col=divmod(i,3)
            is_active=key==active
            card=tk.Frame(grid,bg=c("accent" if is_active else "bg1"),
                highlightthickness=1,highlightbackground=c("accent" if is_active else "border"),
                cursor="hand2")
            card.grid(row=r,column=col,padx=6,pady=6,sticky="nsew",ipadx=4,ipady=4)
            if is_active:
                badge=tk.Label(card,text="ACTIF",bg=c("accent"),fg="white",
                    font=("Segoe UI",7,"bold"),padx=5,pady=1)
                badge.place(relx=1,x=-8,y=8,anchor="ne")
            tk.Label(card,text=profile.get("name",key),bg=card.cget("bg"),fg=c("text"),
                font=("Segoe UI",11,"bold"),anchor="w").pack(anchor="w",padx=12,pady=(12,2))
            trigger=profile.get("app_trigger","")
            tk.Label(card,text=f"🔗 {trigger}" if trigger else "Pas de déclencheur",
                bg=card.cget("bg"),fg=c("text3"),font=("Segoe UI",8)).pack(anchor="w",padx=12,pady=(0,8))
            btn_row=tk.Frame(card,bg=card.cget("bg")); btn_row.pack(fill="x",padx=8,pady=(0,8))
            if not is_active:
                styled_btn(btn_row,"Activer",lambda k=key:self._set_profile(k),style="primary",padx=6,pady=3).pack(side="left",padx=2)
            styled_btn(btn_row,"✏",lambda k=key,p=profile:self._rename_profile(k,p),padx=6,pady=3).pack(side="left",padx=2)
            if key!="default":
                styled_btn(btn_row,"🗑",lambda k=key:self._delete_profile(k),style="danger",padx=6,pady=3).pack(side="left",padx=2)

    def _set_profile(self,key):
        self.cfg.data["active_profile"]=key; self.cfg.save()
        self._on_profile_changed(key); self._refresh_profiles()

    def _next_profile(self):
        keys=list(self.cfg.data["profiles"].keys()); cur=self.cfg.data.get("active_profile","default")
        n=keys[(keys.index(cur)+1)%len(keys)] if cur in keys else keys[0]
        self._set_profile(n)

    def _prev_profile(self):
        keys=list(self.cfg.data["profiles"].keys()); cur=self.cfg.data.get("active_profile","default")
        n=keys[(keys.index(cur)-1)%len(keys)] if cur in keys else keys[0]
        self._set_profile(n)

    def _new_profile(self):
        win=tk.Toplevel(self.root); win.title("Nouveau profil")
        win.geometry("300x160"); win.configure(bg=c("bg1")); win.grab_set()
        tk.Label(win,text="Nom du nouveau profil :",bg=c("bg1"),fg=c("text2"),
            font=("Segoe UI",9)).pack(padx=16,pady=(16,4),anchor="w")
        var=tk.StringVar()
        ef=styled_entry(win,textvariable=var,font=("Segoe UI",10))
        ef.pack(fill="x",padx=16)
        ef._entry.focus()
        def ok():
            name=var.get().strip()
            if not name: return
            key=re.sub(r"[^a-z0-9_]","",name.lower().replace(" ","_"))
            if key and key not in self.cfg.data["profiles"]:
                self.cfg.data["profiles"][key]=empty_profile(name)
                self.cfg.save(); self._refresh_profiles()
            win.destroy()
        styled_btn(win,"Créer",ok,style="primary",padx=12,pady=6).pack(fill="x",padx=16,pady=12)
        win.bind("<Return>",lambda e:ok())

    def _rename_profile(self,key,profile):
        win=tk.Toplevel(self.root); win.title("Renommer")
        win.geometry("300x130"); win.configure(bg=c("bg1")); win.grab_set()
        var=tk.StringVar(value=profile.get("name",key))
        ef=styled_entry(win,textvariable=var,font=("Segoe UI",10))
        ef.pack(fill="x",padx=16,pady=16)
        def ok():
            self.cfg.data["profiles"][key]["name"]=var.get().strip()
            self.cfg.save(); self._refresh_profiles(); win.destroy()
        styled_btn(win,"OK",ok,style="primary",padx=12,pady=6).pack(fill="x",padx=16,pady=(0,12))
        win.bind("<Return>",lambda e:ok())

    def _delete_profile(self,key):
        if messagebox.askyesno("Supprimer",f"Supprimer le profil «{key}» ?",parent=self.root):
            del self.cfg.data["profiles"][key]
            if self.cfg.data.get("active_profile")==key: self.cfg.data["active_profile"]="default"
            self.cfg.save(); self._refresh_profiles()

    # ══════════════════════════════════════════════════════════════════════════
    # VUE SERIAL LOG
    # ══════════════════════════════════════════════════════════════════════════
    def _build_view_serial(self):
        f=self._views["serial"]
        # Barre supérieure
        bar=tk.Frame(f,bg=c("bg1"),height=38); bar.pack(fill="x"); bar.pack_propagate(False)
        separator(bar,orient="h",side="bottom")
        self._serial_dot=tk.Canvas(bar,width=8,height=8,bg=c("bg1"),highlightthickness=0)
        self._serial_dot.pack(side="left",padx=(12,4),pady=15)
        self._serial_dot.create_oval(0,0,8,8,fill=c("text3"),outline="",tags="d")
        tk.Label(bar,text="SERIAL LOG",bg=c("bg1"),fg=c("text2"),
            font=("Courier",10,"bold")).pack(side="left",padx=4)
        tk.Frame(bar,bg=c("border"),width=1).pack(side="left",fill="y",padx=8,pady=8)

        # Filtres
        self._filter_btns={}
        for filt,label in [("all","Tout"),("press","Press"),("long_press","Long"),
                            ("double_click","Double"),("pot","Pot"),("sys","Sys")]:
            is_cur=self._log_filter==filt
            btn=tk.Label(bar,text=label,bg=c("accent" if is_cur else "bg1"),
                fg=c("white" if is_cur else "text3"),cursor="hand2",
                font=("Segoe UI",9),padx=8,pady=4)
            btn.pack(side="left",padx=1,pady=6)
            btn.bind("<Button-1>",lambda e,f_=filt:self._set_log_filter(f_))
            self._filter_btns[filt]=btn

        # Droite
        styled_btn(bar,"🗑 Vider",self._clear_log,padx=8,pady=3).pack(side="right",padx=4,pady=8)

        # Log text
        log_frame=tk.Frame(f,bg=c("bg0")); log_frame.pack(fill="both",expand=True)
        self._log_text=tk.Text(log_frame,bg=c("bg0"),fg=c("text2"),font=("Courier",10),
            relief="flat",bd=0,state="disabled",wrap="none")
        vsb=tk.Scrollbar(log_frame,orient="vertical",command=self._log_text.yview)
        hsb=tk.Scrollbar(log_frame,orient="horizontal",command=self._log_text.xview)
        self._log_text.configure(yscrollcommand=vsb.set,xscrollcommand=hsb.set)
        vsb.pack(side="right",fill="y"); hsb.pack(side="bottom",fill="x")
        self._log_text.pack(fill="both",expand=True,padx=0,pady=0)
        # Tags couleurs
        self._log_text.tag_configure("ts",foreground=c("text3"))
        self._log_text.tag_configure("rx",foreground=c("green"))
        self._log_text.tag_configure("tx",foreground=c("blue"))
        self._log_text.tag_configure("sys",foreground=c("text3"))
        self._log_text.tag_configure("press",foreground="#818cf8")
        self._log_text.tag_configure("long_press",foreground=c("yellow"))
        self._log_text.tag_configure("double_click",foreground=c("purple"))
        self._log_text.tag_configure("pot",foreground="#34d399")

    def _add_log(self,dir_,type_,frame):
        ts=datetime.datetime.now().strftime("%H:%M:%S.%f")[:12]
        entry={"dir":dir_,"type":type_,"frame":frame,"ts":ts}
        self._logs.append(entry)
        if len(self._logs)>600: self._logs.pop(0)
        self.root.after(0,self._render_log_line,entry)

    def _render_log_line(self,entry):
        filt=self._log_filter
        if filt!="all" and entry["type"]!=filt: return
        t=self._log_text; t.configure(state="normal")
        t.insert("end",f"[{entry['ts']}] ","ts")
        t.insert("end",f"{entry['dir'].upper():3} ",entry["dir"])
        t.insert("end",f"{entry['type']:12} ",entry["type"].replace("_"," "))
        t.insert("end",f"{entry['frame']}\n","ts")
        t.see("end"); t.configure(state="disabled")

    def _set_log_filter(self,filt):
        self._log_filter=filt
        for f,btn in self._filter_btns.items():
            is_cur=f==filt
            btn.configure(bg=c("accent") if is_cur else c("bg1"),
                fg="white" if is_cur else c("text3"))
        self._redraw_log()

    def _redraw_log(self):
        t=self._log_text; t.configure(state="normal"); t.delete("1.0","end")
        for entry in self._logs:
            if self._log_filter!="all" and entry["type"]!=self._log_filter: continue
            t.insert("end",f"[{entry['ts']}] ","ts")
            t.insert("end",f"{entry['dir'].upper():3} ",entry["dir"])
            t.insert("end",f"{entry['type']:12} ",entry["type"].replace("_"," "))
            t.insert("end",f"{entry['frame']}\n","ts")
        t.see("end"); t.configure(state="disabled")

    def _clear_log(self):
        self._logs.clear()
        t=self._log_text; t.configure(state="normal"); t.delete("1.0","end"); t.configure(state="disabled")

    # ══════════════════════════════════════════════════════════════════════════
    # VUE PARAMÈTRES
    # ══════════════════════════════════════════════════════════════════════════
    def _build_view_settings(self):
        f=self._views["settings"]
        # Titre
        hdr=tk.Frame(f,bg=c("bg0")); hdr.pack(fill="x",padx=20,pady=(14,4))
        tk.Label(hdr,text="Paramètres",bg=c("bg0"),fg=c("text"),
            font=("Segoe UI",13,"bold")).pack(anchor="w")
        tk.Label(hdr,text="Configuration de Imperium et de la connexion ESP32",bg=c("bg0"),
            fg=c("text3"),font=("Segoe UI",9)).pack(anchor="w")

        # Tabs
        tabs_frame=tk.Frame(f,bg=c("bg2"),highlightthickness=1,highlightbackground=c("border"))
        tabs_frame.pack(fill="x",padx=20,pady=8)
        self._settings_tab_btns={}
        self._settings_tab_content={}
        for tab_id,label in [("serial","🔌 Connexion"),("proto","📡 Protocole"),("overlay","🪟 Overlay"),("about","ℹ À propos")]:
            btn=tk.Label(tabs_frame,text=label,bg=c("bg2"),fg=c("text3"),cursor="hand2",
                font=("Segoe UI",10),padx=14,pady=6)
            btn.pack(side="left")
            btn.bind("<Button-1>",lambda e,t=tab_id:self._switch_settings_tab(t))
            self._settings_tab_btns[tab_id]=btn

        # Contenu scrollable
        scroll_canvas=tk.Canvas(f,bg=c("bg0"),highlightthickness=0)
        sb=tk.Scrollbar(f,orient="vertical",command=scroll_canvas.yview)
        scroll_canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right",fill="y"); scroll_canvas.pack(fill="both",expand=True)
        content=tk.Frame(scroll_canvas,bg=c("bg0"))
        win_id=scroll_canvas.create_window((0,0),window=content,anchor="nw")
        content.bind("<Configure>",lambda e:scroll_canvas.configure(scrollregion=scroll_canvas.bbox("all")))
        scroll_canvas.bind("<Configure>",lambda e:scroll_canvas.itemconfig(win_id,width=e.width))

        # Construire les onglets
        for tab_id in ("serial","proto","overlay","about"):
            tab=tk.Frame(content,bg=c("bg0")); tab.pack(fill="both",expand=True)
            self._settings_tab_content[tab_id]=tab

        self._build_settings_serial(self._settings_tab_content["serial"])
        self._build_settings_proto(self._settings_tab_content["proto"])
        self._build_settings_overlay(self._settings_tab_content["overlay"])
        self._build_settings_about(self._settings_tab_content["about"])
        self._switch_settings_tab("serial")

    def _switch_settings_tab(self,tab_id):
        for t,f in self._settings_tab_content.items(): f.pack_forget()
        self._settings_tab_content[tab_id].pack(fill="both",expand=True)
        for t,btn in self._settings_tab_btns.items():
            btn.configure(bg=c("bg4") if t==tab_id else c("bg2"),
                fg=c("text") if t==tab_id else c("text3"))

    def _settings_section(self,parent,icon,title,desc):
        s=tk.Frame(parent,bg=c("bg1"),highlightthickness=1,highlightbackground=c("border"))
        s.pack(fill="x",padx=20,pady=6)
        hdr=tk.Frame(s,bg=c("bg1")); hdr.pack(fill="x",padx=16,pady=(14,8))
        ic_lbl=tk.Label(hdr,text=icon,bg=c("adim"),fg=c("accent"),font=("Segoe UI Emoji",13),padx=6,pady=4)
        ic_lbl.pack(side="left")
        info=tk.Frame(hdr,bg=c("bg1")); info.pack(side="left",padx=10)
        tk.Label(info,text=title,bg=c("bg1"),fg=c("text"),font=("Segoe UI",11,"bold")).pack(anchor="w")
        tk.Label(info,text=desc,bg=c("bg1"),fg=c("text3"),font=("Segoe UI",9)).pack(anchor="w")
        separator(s,orient="h",padx=0,pady=0)
        body=tk.Frame(s,bg=c("bg1")); body.pack(fill="x",padx=16,pady=10)
        return body

    def _build_settings_serial(self,f):
        body=self._settings_section(f,"🔌","Port USB 1 — ESP32 principal","Périphérique connecté au premier port USB")
        self._port_var=tk.StringVar(value=self.cfg.data.get("serial_port","AUTO"))
        row=tk.Frame(body,bg=c("bg1")); row.pack(fill="x",pady=4)
        tk.Label(row,text="Port COM :",bg=c("bg1"),fg=c("text2"),font=("Segoe UI",9),width=12,anchor="w").pack(side="left")
        ef=styled_entry(row,textvariable=self._port_var,font=("Segoe UI",10))
        ef.pack(side="left",fill="x",expand=True,padx=(0,8))
        styled_btn(row,"🔄 Détecter",self._detect_ports,padx=8,pady=4).pack(side="left",padx=2)
        styled_btn(row,"⚡ Connecter",lambda:self._connect_serial(0),style="primary",padx=8,pady=4).pack(side="left")

        self._ports_lbl=tk.Label(body,text="",bg=c("bg1"),fg=c("text3"),font=("Segoe UI",9))
        self._ports_lbl.pack(anchor="w",pady=2)

        row2=tk.Frame(body,bg=c("bg1")); row2.pack(fill="x",pady=4)
        tk.Label(row2,text="Baud rate :",bg=c("bg1"),fg=c("text2"),font=("Segoe UI",9),width=12,anchor="w").pack(side="left")
        self._baud_var=tk.StringVar(value=str(self.cfg.data.get("baud_rate",115200)))
        baud_sel=ttk.Combobox(row2,textvariable=self._baud_var,values=["9600","57600","115200","230400"],
            state="readonly",width=12); baud_sel.pack(side="left")

        self._serial_status_lbl=tk.Label(body,text="",bg=c("bg1"),fg=c("text3"),font=("Segoe UI",9))
        self._serial_status_lbl.pack(anchor="w",pady=4)

    def _detect_ports(self):
        if SERIAL_OK:
            ports=[p.device for p in serial.tools.list_ports.comports()]
            self._ports_lbl.configure(text=f"Ports : {', '.join(ports) if ports else 'Aucun détecté'}")
            if ports: self._port_var.set(ports[0])
        else:
            self._ports_lbl.configure(text="pyserial non disponible")

    def _connect_serial(self,slot):
        port=self._port_var.get().strip(); baud=int(self._baud_var.get() or 115200)
        self.cfg.data["serial_port"]=port; self.cfg.data["baud_rate"]=baud; self.cfg.save()
        self.transport.start(port,baud=baud,slot=slot)
        self.root.after(800,self._update_serial_ui)

    def _build_settings_proto(self,f):
        body=self._settings_section(f,"📡","Trames série — Protocole","Patrons d'entrée/sortie entre Imperium et l'ESP32")
        self._proto_vars={}
        for key,label,default,desc in [
            ("in_press","Bouton ON","btn{i}:on","Au press"),
            ("in_release","Bouton OFF","btn{i}:off","Au relâchement"),
            ("in_pot","Potard","pot{i}:{v}","Valeur potard (0-100)"),
            ("in_long_press","Long press (opt.)","","Calculé auto si vide"),
            ("in_double_click","Double clic (opt.)","","Calculé auto si vide"),
            ("out_led","LED (sortie)","led{i}:{v}","Envoyé à l'ESP32"),
        ]:
            row=tk.Frame(body,bg=c("bg1")); row.pack(fill="x",pady=3)
            tk.Label(row,text=label+":",bg=c("bg1"),fg=c("text2"),font=("Segoe UI",9),
                width=20,anchor="w").pack(side="left")
            var=tk.StringVar(value=self.cfg.data.get("protocol",{}).get(key,default))
            ef=styled_entry(row,textvariable=var,font=("Courier",10))
            ef.pack(side="left",fill="x",expand=True)
            self._proto_vars[key]=var

        def save_proto():
            self.cfg.data["protocol"]={k:v.get() for k,v in self._proto_vars.items()}
            self.cfg.save(); self._toast("✓ Protocole enregistré")

        def reset_proto():
            defaults={"in_press":"btn{i}:on","in_release":"btn{i}:off","in_pot":"pot{i}:{v}","in_long_press":"","in_double_click":"","out_led":"led{i}:{v}"}
            for k,var in self._proto_vars.items(): var.set(defaults.get(k,""))
            save_proto()

        btn_row=tk.Frame(body,bg=c("bg1")); btn_row.pack(fill="x",pady=8)
        styled_btn(btn_row,"💾 Enregistrer",save_proto,style="primary",padx=10,pady=5).pack(side="left",padx=4)
        styled_btn(btn_row,"↺ Réinitialiser",reset_proto,padx=10,pady=5).pack(side="left")

    def _build_settings_overlay(self,f):
        body=self._settings_section(f,"🪟","Popup de changement de profil","Affichée au-dessus de tout à chaque changement")
        ov=self.cfg.data.get("overlay",{})
        self._ov_vars={}
        for key,label,default in [("cell_size","Taille cellules (px)",56),("delay","Durée (secondes)",3),("alpha","Opacité (%)",97)]:
            row=tk.Frame(body,bg=c("bg1")); row.pack(fill="x",pady=3)
            tk.Label(row,text=label+":",bg=c("bg1"),fg=c("text2"),font=("Segoe UI",9),width=20,anchor="w").pack(side="left")
            var=tk.StringVar(value=str(ov.get(key,default)))
            ef=styled_entry(row,textvariable=var,width=8,font=("Segoe UI",10))
            ef.pack(side="left")
            self._ov_vars[key]=var

        row_pos=tk.Frame(body,bg=c("bg1")); row_pos.pack(fill="x",pady=3)
        tk.Label(row_pos,text="Position :",bg=c("bg1"),fg=c("text2"),font=("Segoe UI",9),width=20,anchor="w").pack(side="left")
        self._ov_pos=tk.StringVar(value=ov.get("position","br"))
        pos_sel=ttk.Combobox(row_pos,textvariable=self._ov_pos,state="readonly",width=20,
            values=["br — Bas droite","bl — Bas gauche","tr — Haut droite","tl — Haut gauche"])
        pos_sel.pack(side="left")

        def save_ov():
            self.cfg.data["overlay"]={"cell_size":int(self._ov_vars["cell_size"].get() or 56),
                "delay":int(self._ov_vars["delay"].get() or 3),
                "alpha":int(self._ov_vars["alpha"].get() or 97),
                "position":self._ov_pos.get()[:2]}
            self.cfg.save(); self._toast("✓ Overlay enregistré")

        btn_row=tk.Frame(body,bg=c("bg1")); btn_row.pack(fill="x",pady=8)
        styled_btn(btn_row,"💾 Enregistrer",save_ov,style="primary",padx=10,pady=5).pack(side="left",padx=4)
        styled_btn(btn_row,"👁 Prévisualiser",self._preview_overlay,padx=10,pady=5).pack(side="left")

    def _build_settings_about(self,f):
        body=self._settings_section(f,"ℹ","À propos","Version et mises à jour")
        tk.Label(body,text=f"Imperium v{APP_VERSION}",bg=c("bg1"),fg=c("text"),
            font=("Segoe UI",12,"bold")).pack(anchor="w",padx=4,pady=4)
        self._upd_lbl=tk.Label(body,text="",bg=c("bg1"),fg=c("text2"),font=("Segoe UI",9))
        self._upd_lbl.pack(anchor="w",padx=4)
        self._upd_bar_frame=tk.Frame(body,bg=c("bg1")); self._upd_bar_frame.pack(fill="x",padx=4,pady=4)
        styled_btn(body,"🔍 Vérifier les mises à jour",self._check_update,padx=12,pady=5).pack(anchor="w",padx=4,pady=8)

    def _preview_overlay(self):
        key=self.cfg.data.get("active_profile","default")
        profile=self.cfg.data["profiles"].get(key)
        if profile: self.overlay.show(profile,self.cfg.data.get("overlay",{}))

    def _check_update(self):
        self._upd_lbl.configure(text="⏳ Vérification…",fg=c("text3"))
        def _do():
            import urllib.request
            REPO="tuturpotter-web/Imperium"
            try:
                req=urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                    headers={"User-Agent":"Imperium-updater"})
                with urllib.request.urlopen(req,timeout=8) as r: data=json.loads(r.read())
                latest=data.get("tag_name","").lstrip("v")
                if latest and latest!=APP_VERSION:
                    asset=next((a for a in data.get("assets",[]) if a["name"].endswith(".exe")),None)
                    dl=asset["browser_download_url"] if asset else ""
                    self.root.after(0,lambda:self._upd_lbl.configure(text=f"🚀 MAJ disponible : {APP_VERSION} → {latest}",fg=c("yellow")))
                    if dl: self.root.after(0,lambda:self._show_dl_btn(dl))
                else:
                    self.root.after(0,lambda:self._upd_lbl.configure(text=f"✅ À jour ({APP_VERSION})",fg=c("green")))
            except Exception as e:
                self.root.after(0,lambda:self._upd_lbl.configure(text=f"⚠ {e}",fg=c("red")))
        threading.Thread(target=_do,daemon=True).start()

    def _show_dl_btn(self,url):
        for w in self._upd_bar_frame.winfo_children(): w.destroy()
        def _dl():
            for w in self._upd_bar_frame.winfo_children(): w.destroy()
            pb_frame=tk.Frame(self._upd_bar_frame,bg=c("bg3"),height=6)
            pb_frame.pack(fill="x",pady=4)
            pb_fill=tk.Frame(pb_frame,bg=c("accent"),height=6)
            pb_fill.place(x=0,y=0,relwidth=0,relheight=1)
            pct_lbl=tk.Label(self._upd_bar_frame,text="0%",bg=c("bg1"),fg=c("text2"),font=("Segoe UI",9))
            pct_lbl.pack()
            def _do():
                import urllib.request, tempfile
                try:
                    fname=url.split("/")[-1]; tmp=os.path.join(tempfile.gettempdir(),fname)
                    def rep(bn,bs,fs):
                        pct=min(100,int(bn*bs/fs*100)) if fs>0 else 0
                        self.root.after(0,lambda p=pct:(pb_fill.place(relwidth=p/100),pct_lbl.configure(text=f"{p}%")))
                    urllib.request.urlretrieve(url,tmp,rep)
                    self.root.after(0,lambda:pct_lbl.configure(text="✅ Installation…"))
                    self.root.after(500,lambda:(subprocess.Popen([tmp],creationflags=CREATE_NO_WINDOW),self.root.after(1000,lambda:os._exit(0))))
                except Exception as e:
                    self.root.after(0,lambda:pct_lbl.configure(text=f"⚠ {e}",fg=c("red")))
            threading.Thread(target=_do,daemon=True).start()
        styled_btn(self._upd_bar_frame,"⬇ Télécharger et installer",_dl,
            style="primary",padx=12,pady=5).pack(anchor="w")

    # ══════════════════════════════════════════════════════════════════════════
    # TOAST
    # ══════════════════════════════════════════════════════════════════════════
    def _toast(self,msg,ms=2500):
        try:
            if hasattr(self,"_toast_win") and self._toast_win.winfo_exists():
                self._toast_win.destroy()
        except: pass
        w=tk.Toplevel(self.root); w.overrideredirect(True)
        w.configure(bg=c("bg3")); w.attributes("-topmost",True)
        try: w.attributes("-alpha",0.94)
        except: pass
        tk.Label(w,text=msg,fg=c("text"),bg=c("bg3"),
            font=("Segoe UI",9),padx=14,pady=6).pack()
        w.update_idletasks()
        rx=self.root.winfo_x()+self.root.winfo_width()//2-w.winfo_width()//2
        ry=self.root.winfo_y()+self.root.winfo_height()-60
        w.geometry(f"+{rx}+{ry}"); self._toast_win=w
        self.root.after(ms,lambda:w.destroy() if w.winfo_exists() else None)

    # ══════════════════════════════════════════════════════════════════════════
    # CALLBACKS CALLBACKS CALLBACKS
    # ══════════════════════════════════════════════════════════════════════════
    def _on_profile_changed(self,key):
        profile=self.cfg.data["profiles"].get(key)
        if not profile: return
        self._profile_lbl.configure(text=profile.get("name",key))
        self.overlay.show(profile,self.cfg.data.get("overlay",{}))
        self._toast(f"Profil : {profile.get('name',key)}")
        self._refresh_device()
        if self._active_view=="profiles": self._refresh_profiles()
        # Mettre à jour le dot
        self._update_serial_ui()

    def _on_serial(self,raw,slot):
        proto=self.cfg.data.get("protocol",{})
        self._add_log("rx","sys" if "sys" in raw.lower() else "press",raw)

        pat_pot=proto.get("in_pot","")
        if pat_pot:
            try:
                m=pattern_to_regex(pat_pot).match(raw)
                if m:
                    idx=int(m.group("i")); val=int(m.group("v"))
                    self.engine.run_pot(self.cfg.active()["pots"].get(str(idx),{}),val)
                    self.root.after(0,lambda:self._animate_pot(idx,val))
                    self._add_log("rx","pot",f"POT{idx+1} {val}%"); return
            except: pass

        pat_on=proto.get("in_press","btn{i}:on")
        pat_off=proto.get("in_release","btn{i}:off")

        def dispatch(idx,ev):
            actions=self.cfg.active()["buttons"].get(str(idx),{}).get(ev,[])
            if actions: threading.Thread(target=self.engine.run,args=(actions,),daemon=True).start()
            self._add_log("rx",ev,f"BTN{idx+1}")
            self.root.after(0,lambda:self._flash_btn(idx))

        if pat_on:
            try:
                m=pattern_to_regex(pat_on).match(raw)
                if m: self.transport._handle_timing(int(m.group("i")),"on",dispatch); return
            except: pass
        if pat_off:
            try:
                m=pattern_to_regex(pat_off).match(raw)
                if m: self.transport._handle_timing(int(m.group("i")),"off",dispatch); return
            except: pass

        for ev_key,ev_name in [("in_long_press","long_press"),("in_double_click","double_click")]:
            pat=proto.get(ev_key,"")
            if not pat: continue
            try:
                m=pattern_to_regex(pat).match(raw)
                if m: dispatch(int(m.group("i")),ev_name); return
            except: pass

        # Fallback JSON
        try:
            msg=json.loads(raw); t=msg.get("t")
            if t in ("press","long_press","double_click"):
                idx=msg.get("i",0)
                actions=self.cfg.active()["buttons"].get(str(idx),{}).get(t,[])
                threading.Thread(target=self.engine.run,args=(actions,),daemon=True).start()
                dispatch(idx,t)
            elif t=="pot":
                idx=msg.get("i",0); val=msg.get("v",0)
                self.engine.run_pot(self.cfg.active()["pots"].get(str(idx),{}),val)
                self.root.after(0,lambda:self._animate_pot(idx,val))
        except: pass

    def _update_serial_ui(self):
        ok=self.transport.is_connected(0)
        port=self.transport._port_names[0]
        # Dot device card
        self._dev_dot.itemconfig("d",fill=c("green") if ok else c("text3"))
        self._dev_lbl.configure(text=port if ok else "—",fg=c("text2") if ok else c("text3"))
        # Dot serial log
        self._serial_dot.itemconfig("d",fill=c("green") if ok else c("text3"))
        # Status settings
        if hasattr(self,"_serial_status_lbl"):
            self._serial_status_lbl.configure(
                text=f"✅ Connecté : {port}" if ok else "❌ Non connecté",
                fg=c("green") if ok else c("red"))
        # USB icon
        if 0<len(self._usb_slots):
            cv=self._usb_slots[0]
            cv.itemconfig("dot",fill=c("green") if ok else c("text3"))

    def _save_config(self):
        self.cfg.save(); self._toast("✓ Configuration enregistrée")

    def _autosave(self):
        if hasattr(self,"_autosave_timer"):
            try: self.root.after_cancel(self._autosave_timer)
            except: pass
        self._autosave_timer=self.root.after(1000,self.cfg.save)

    # ══════════════════════════════════════════════════════════════════════════
    # BOUCLE MÉTRIQUES
    # ══════════════════════════════════════════════════════════════════════════
    def _start_metrics_loop(self):
        self.metrics_engine.collect()
        def _loop():
            while True:
                time.sleep(1)
                m=self.metrics_engine.collect()
                self._metrics=m
                keys=["cpu","ram","gpu_usage","ssd_usage"]
                out_pat=self.cfg.data.get("protocol",{}).get("out_led","led{i}:{v}")
                for i in range(4):
                    k=self.cfg.data.get("led_strips",{}).get(str(i),{}).get("metric",keys[i])
                    meta=next((x for x in LED_METRICS if x[0]==k),None)
                    raw=m.get(k,0) or 0
                    if meta and meta[3]: val=max(0,min(100,int(raw)))
                    elif meta and meta[4]: val=max(0,min(100,int(raw/meta[4]*100)))
                    else: val=0
                    self.transport.send_raw(pattern_format(out_pat,i=i,v=val))
                self.root.after(0,self._tick_ui)
        threading.Thread(target=_loop,daemon=True).start()

    def _tick_ui(self):
        m=self._metrics
        # Heure topbar
        self._lbl_time.configure(text=m.get("time",""))
        # LEDs
        self._update_led_strips()
        # Serial status
        self._update_serial_ui()
        # Métriques si visible
        if self._active_view=="metrics": self._refresh_metrics_ui()

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def _port_is_free(p):
    import socket as _s; s=_s.socket(_s.AF_INET,_s.SOCK_STREAM)
    try: s.bind(("127.0.0.1",p)); s.close(); return True
    except: s.close(); return False

if __name__=="__main__":
    LOCK=Path(os.path.expanduser("~"))/".macrodeck"/"imperium.lock"
    try:
        if LOCK.exists():
            try:
                pid=int(LOCK.read_text())
                if psutil.pid_exists(pid):
                    if sys.platform=="win32":
                        ctypes.windll.user32.MessageBoxW(0,"Imperium est déjà lancé.","Imperium",0x40|0x1000)
                    sys.exit(0)
            except: pass
        LOCK.write_text(str(os.getpid()))
    except: pass

    root=tk.Tk()
    app=ImperiumApp(root)

    def _on_close():
        try: app.cfg.save()
        except: pass
        try: LOCK.unlink()
        except: pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW",_on_close)
    try: root.mainloop()
    finally:
        try: app.cfg.save()
        except: pass
        try: LOCK.unlink()
        except: pass
PYEOF
echo "done — $(wc -l < /home/claude/imperium_app.py) lignes"
