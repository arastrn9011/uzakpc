#!/usr/bin/env python3
"""Uzaktan PC - telefondan bilgisayar ekranini gorme ve kontrol etme sunucusu (Windows)."""
import asyncio
import getpass
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import warnings
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

# Ekran olcekleme (DPI) ile fare koordinatlarinin tutmasi icin en basta ayarlanir
if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import mss
from aiohttp import web, WSMsgType
from PIL import Image, ImageDraw
from pynput.keyboard import Controller as KeyboardController, Key
from pynput.mouse import Controller as MouseController, Button

try:
    import numpy as np
    import soundcard as sc
    AUDIO_OK = True
    AUDIO_WHY = ""
except Exception as exc:  # ses kutuphanesi yoksa program yine calisir
    AUDIO_OK = False
    AUDIO_WHY = str(exc)

AUDIO_RATE = 24000
AUDIO_CH = 2

BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "config.json"
CFG = {}
SESSION = ""
ROOT = Path.home()
SERVICE = "--servis" in sys.argv  # SYSTEM servisi olarak (kilit ekrani dahil) calisiyor

mouse = MouseController()
keyboard = KeyboardController()

# (maksimum genislik, jpeg kalitesi, saniyedeki kare)
LEVELS = [(800, 35, 8), (960, 42, 10), (1280, 55, 12), (1600, 66, 15), (1920, 74, 20)]
FIXED = {1: 1, 2: 2, 3: 3}  # 0 = otomatik; 1/2/3 = Dusuk/Orta/Yuksek


# ----------------------------------------------------------------- ayarlar
def hash_pw(pw, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), 200_000).hex()


def ask_password():
    while True:
        pw = getpass.getpass("Yeni sifre belirle (en az 8 karakter): ")
        if len(pw) < 8:
            print("Cok kisa, tekrar dene.")
            continue
        if pw != getpass.getpass("Sifreyi tekrar yaz: "):
            print("Sifreler uyusmadi, tekrar dene.")
            continue
        return pw


def load_config(reset_password=False):
    cfg = {}
    if CFG_PATH.exists():
        try:
            cfg = json.loads(CFG_PATH.read_text("utf-8"))
        except Exception:
            cfg = {}
    cfg.setdefault("secret", secrets.token_hex(32))
    cfg.setdefault("port", 8765)
    cfg.setdefault("files_root", str(Path.home()))
    cfg.setdefault("keep_awake", True)
    if reset_password or "pw_hash" not in cfg:
        pw = ask_password()
        cfg["salt"] = secrets.token_hex(16)
        cfg["pw_hash"] = hash_pw(pw, cfg["salt"])
        print("Sifre kaydedildi.\n")
    CFG_PATH.write_text(json.dumps(cfg, indent=2), "utf-8")
    return cfg


def session_token():
    msg = ("session-v1:" + CFG["pw_hash"]).encode()
    return hmac.new(CFG["secret"].encode(), msg, hashlib.sha256).hexdigest()


def authed(request):
    return hmac.compare_digest(request.cookies.get("sid", ""), SESSION)


def guard(fn):
    async def wrapper(request):
        if not authed(request):
            return web.json_response({"error": "auth"}, status=401)
        return await fn(request)
    return wrapper


# ----------------------------------------------------------------- masaustu gecisi (kilit ekrani / UAC)
_dtls = threading.local()

if sys.platform == "win32":
    _u32 = ctypes.windll.user32
    _u32.OpenInputDesktop.restype = ctypes.c_void_p
    _u32.OpenInputDesktop.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    _u32.SetThreadDesktop.argtypes = [ctypes.c_void_p]
    _u32.CloseDesktop.argtypes = [ctypes.c_void_p]
    _u32.GetUserObjectInformationW.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]


def sync_desktop(min_gap=0.0):
    """Bu thread'i o an girdi alan masaustune (kilit/giris ekrani dahil) baglar.
    Masaustu degistiyse True doner. Yetki yoksa (SYSTEM degilse) sessizce False doner."""
    if sys.platform != "win32":
        return False
    now = time.monotonic()
    if min_gap and now - getattr(_dtls, "t", 0.0) < min_gap:
        return False
    _dtls.t = now
    h = _u32.OpenInputDesktop(0, 0, 0x10000000)  # GENERIC_ALL
    if not h:
        return False
    buf = ctypes.create_unicode_buffer(256)
    need = ctypes.c_uint32()
    _u32.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(need))  # UOI_NAME
    if buf.value == getattr(_dtls, "name", None):
        _u32.CloseDesktop(h)
        return False
    if _u32.SetThreadDesktop(h):
        old = getattr(_dtls, "h", None)
        _dtls.h, _dtls.name = h, buf.value
        if old:
            _u32.CloseDesktop(old)
        return True
    _u32.CloseDesktop(h)
    return False


# ----------------------------------------------------------------- ekran yakalama
_tls = threading.local()
EXEC = ThreadPoolExecutor(max_workers=1)  # mss ayni thread'de kullanilmali
_MSS = getattr(mss, "MSS", None) or mss.mss


def _sct():
    s = getattr(_tls, "sct", None)
    if s is None:
        s = _tls.sct = _MSS()
    return s


def reset_sct():
    s = getattr(_tls, "sct", None)
    if s is not None:
        try:
            s.close()
        except Exception:
            pass
    _tls.sct = _MSS()


def init_capture():
    sync_desktop()
    reset_sct()
    return [(m["width"], m["height"]) for m in _tls.sct.monitors[1:]] or [(1920, 1080)]


def grab(mon_idx, width, quality):
    if sync_desktop():
        reset_sct()
    sct = _sct()
    mons = sct.monitors
    idx = max(1, min(mon_idx, len(mons) - 1))
    m = mons[idx]
    shot = sct.grab(m)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.BILINEAR, reducing_gap=2.0)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue(), dict(m)


def cursor_pos():
    try:
        return mouse.position
    except Exception:
        return (0, 0)


async def stream(ws, st):
    loop = asyncio.get_running_loop()
    last_crc, last_send = None, 0.0
    last_change = time.monotonic()
    level, ema, last_move, lock_up = 2, 0.05, 0.0, 0.0
    while not ws.closed:
        t0 = time.monotonic()
        if st["q"] in FIXED:
            level = FIXED[st["q"]]
        width, quality, fps = LEVELS[level]
        try:
            jpg, geom = await loop.run_in_executor(EXEC, grab, st["mon"], width, quality)
        except Exception:
            if not st["warned"]:
                st["warned"] = True
                await ws.send_json({"t": "warn", "msg": "Ekran yakalanamiyor (kilit ekrani veya yonetici penceresi olabilir)"})
            await asyncio.sleep(0.7)
            continue
        if st["warned"]:
            st["warned"] = False
            await ws.send_json({"t": "warn", "msg": ""})
        st["geom"] = geom
        x, y = cursor_pos()
        await ws.send_json({
            "t": "cur",
            "x": (x - geom["left"]) / geom["width"],
            "y": (y - geom["top"]) / geom["height"],
        })
        crc = zlib.crc32(jpg)
        changed = crc != last_crc
        send_dt = 0.0
        if changed or t0 - last_send > 1.5:
            ts = time.monotonic()
            await ws.send_bytes(b"\x01\x00" + jpg)
            send_dt = time.monotonic() - ts
            last_crc, last_send = crc, t0
        if changed:
            last_change = t0

        # Otomatik kalite: gonderim yavassa dusur, hizliysa yavasca yukselt
        if st["q"] == 0 and send_dt:
            ema = 0.7 * ema + 0.3 * send_dt
            now = time.monotonic()
            if ema > 0.10 and level > 0 and now - last_move > 1.0:
                level -= 1
                last_move, lock_up, ema = now, now + 10, 0.05
            elif ema < 0.02 and level < len(LEVELS) - 1 and now > lock_up and now - last_move > 3.0:
                level += 1
                last_move, ema = now, 0.05

        now = time.monotonic()
        idle = now - max(last_change, st["active"]) > 2.0
        if idle:
            # Ekran durgunken saniyede 4 kareye in; dokunma gelince hemen uyan
            try:
                await asyncio.wait_for(st["kick"].wait(), 0.25)
            except asyncio.TimeoutError:
                pass
            st["kick"].clear()
        else:
            await asyncio.sleep(max(0.0, 1.0 / fps - (now - t0)))


# ----------------------------------------------------------------- ses
class AudioStreamer:
    def __init__(self, ws, loop):
        self.ws, self.loop = ws, loop
        self.stop_ev = None
        self.task = None

    def start(self):
        if self.task or not AUDIO_OK:
            return
        q = asyncio.Queue(maxsize=12)
        self.stop_ev = threading.Event()
        threading.Thread(target=self._worker, args=(self.stop_ev, q), daemon=True).start()
        self.task = asyncio.create_task(self._sender(q))

    def stop(self):
        if self.stop_ev:
            self.stop_ev.set()
        if self.task:
            self.task.cancel()
        self.stop_ev = self.task = None

    @staticmethod
    def _push(q, pcm):
        if q.full():
            try:
                q.get_nowait()  # gecikme birikmesin: eski veriyi at
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(pcm)

    def _worker(self, stop_ev, q):
        warnings.filterwarnings("ignore", message=".*discontinuity.*")
        try:
            speaker = sc.default_speaker()
            mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            with mic.recorder(samplerate=AUDIO_RATE, channels=AUDIO_CH) as rec:
                while not stop_ev.is_set():
                    data = rec.record(numframes=AUDIO_RATE // 20)  # 50 ms
                    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2").tobytes()
                    self.loop.call_soon_threadsafe(self._push, q, pcm)
        except Exception as exc:
            asyncio.run_coroutine_threadsafe(
                self.ws.send_json({"t": "warn", "msg": "Ses baslatilamadi: %s" % exc}), self.loop)

    async def _sender(self, q):
        try:
            while True:
                pcm = await q.get()
                await self.ws.send_bytes(b"\x02\x00" + pcm)
        except (asyncio.CancelledError, ConnectionResetError):
            pass


# ----------------------------------------------------------------- girdi (fare / klavye)
SPECIAL = {
    "enter": Key.enter, "esc": Key.esc, "tab": Key.tab, "backspace": Key.backspace,
    "delete": Key.delete, "up": Key.up, "down": Key.down, "left": Key.left,
    "right": Key.right, "home": Key.home, "end": Key.end, "pageup": Key.page_up,
    "pagedown": Key.page_down, "space": Key.space, "win": Key.cmd, "ctrl": Key.ctrl,
    "alt": Key.alt, "shift": Key.shift,
}
for _i in range(1, 13):
    SPECIAL["f%d" % _i] = getattr(Key, "f%d" % _i)
for _name, _attr in [("caps", "caps_lock"), ("insert", "insert"), ("prtsc", "print_screen"),
                     ("menu", "menu"), ("numlock", "num_lock"), ("pause", "pause"),
                     ("volup", "media_volume_up"), ("voldown", "media_volume_down"),
                     ("mute", "media_volume_mute"), ("playpause", "media_play_pause"),
                     ("next", "media_next"), ("prev", "media_previous")]:
    if hasattr(Key, _attr):
        SPECIAL[_name] = getattr(Key, _attr)
BUTTONS = {"left": Button.left, "right": Button.right, "middle": Button.middle}


def press_combo(k, mods):
    held = [SPECIAL[m] for m in mods if m in SPECIAL]
    key = SPECIAL.get(k, k if len(k) == 1 else None)
    if key is None:
        return
    for m in held:
        keyboard.press(m)
    try:
        keyboard.press(key)
        keyboard.release(key)
    finally:
        for m in reversed(held):
            keyboard.release(m)


def handle_input(d, st):
    sync_desktop(0.25)
    st["active"] = time.monotonic()
    st["kick"].set()
    t = d.get("t")
    if t == "move":
        mouse.move(int(d["dx"]), int(d["dy"]))
    elif t == "abs":
        g = st.get("geom")
        if g:
            x = min(max(float(d["x"]), 0.0), 1.0)
            y = min(max(float(d["y"]), 0.0), 1.0)
            mouse.position = (int(g["left"] + x * (g["width"] - 1)), int(g["top"] + y * (g["height"] - 1)))
    elif t == "click":
        mouse.click(BUTTONS.get(d.get("b", "left"), Button.left), int(d.get("n", 1)))
    elif t == "down":
        mouse.press(BUTTONS.get(d.get("b", "left"), Button.left))
    elif t == "up":
        mouse.release(BUTTONS.get(d.get("b", "left"), Button.left))
    elif t == "scroll":
        mouse.scroll(int(d.get("dx", 0)), int(d.get("dy", 0)))
    elif t == "text":
        keyboard.type(str(d.get("s", ""))[:2000])
    elif t == "key":
        press_combo(str(d.get("k", "")), [str(m) for m in d.get("mods", [])])


async def send_info(ws, st, mons):
    i = max(1, min(st["mon"], len(mons)))
    st["mon"] = i
    mw, mh = mons[i - 1]
    await ws.send_json({
        "t": "info", "mons": len(mons), "mon": i, "mw": mw, "mh": mh,
        "audio": AUDIO_OK, "rate": AUDIO_RATE, "ch": AUDIO_CH,
    })


async def ws_handler(request):
    if not authed(request):
        return web.Response(status=401)
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20, compress=False)
    await ws.prepare(request)
    loop = asyncio.get_running_loop()
    mons = await loop.run_in_executor(EXEC, init_capture)
    st = {"q": 0, "mon": 1, "geom": None, "warned": False,
          "kick": asyncio.Event(), "active": time.monotonic()}
    await send_info(ws, st, mons)
    stream_task = asyncio.create_task(stream(ws, st))
    audio = AudioStreamer(ws, loop)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
                t = d.get("t")
                if t == "audio":
                    audio.start() if d.get("on") else audio.stop()
                elif t == "quality":
                    st["q"] = int(d["q"]) % 4
                elif t == "monitor":
                    st["mon"] = int(d["i"])
                    await send_info(ws, st, mons)
                elif t == "sas":
                    if SERVICE:
                        (BASE / "sas.trigger").write_text("1")  # servis yoneticisi Ctrl+Alt+Del gonderir
                    else:
                        await ws.send_json({"t": "toast", "msg": "Ctrl+Alt+Del icin servis modu gerekli (kur_servis.bat)"})
                else:
                    handle_input(d, st)
            except (ValueError, KeyError, TypeError):
                continue
            except Exception as exc:
                print("Girdi hatasi:", exc)
    finally:
        stream_task.cancel()
        audio.stop()
    return ws


# ----------------------------------------------------------------- giris
FAILS = {}


async def api_login(request):
    ip = request.remote
    now = time.time()
    rec = FAILS.get(ip, [0, now])
    if now - rec[1] > 300:
        rec = [0, now]
    if rec[0] >= 5:
        return web.json_response({"ok": False, "error": "Cok fazla deneme. 5 dakika bekleyin."}, status=429)
    try:
        data = await request.json()
    except Exception:
        data = {}
    pw = str(data.get("password", ""))
    if hmac.compare_digest(hash_pw(pw, CFG["salt"]), CFG["pw_hash"]):
        FAILS.pop(ip, None)
        resp = web.json_response({"ok": True})
        resp.set_cookie("sid", SESSION, max_age=60 * 60 * 24 * 90, httponly=True, samesite="Lax", path="/")
        return resp
    rec[0] += 1
    FAILS[ip] = rec
    await asyncio.sleep(1)
    return web.json_response({"ok": False, "error": "Sifre yanlis."}, status=401)


@guard
async def api_me(request):
    return web.json_response({"ok": True})


# ----------------------------------------------------------------- dosyalar
def safe_path(rel):
    rel = (rel or "").replace("\\", "/").strip("/")
    p = (ROOT / rel).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise web.HTTPForbidden()
    return p


def rel_of(p):
    return "" if p == ROOT else p.relative_to(ROOT).as_posix()


@guard
async def api_ls(request):
    p = safe_path(request.query.get("path", ""))
    if not p.is_dir():
        raise web.HTTPNotFound()
    items = []
    try:
        for e in os.scandir(p):
            try:
                is_dir = e.is_dir()
                items.append({"name": e.name, "dir": is_dir,
                              "size": 0 if is_dir else e.stat().st_size})
            except OSError:
                pass
    except PermissionError:
        raise web.HTTPForbidden()
    items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
    return web.json_response({
        "path": rel_of(p),
        "parent": None if p == ROOT else rel_of(p.parent),
        "items": items,
    })


@guard
async def api_dl(request):
    p = safe_path(request.query.get("path", ""))
    if not p.is_file():
        raise web.HTTPNotFound()
    headers = {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(p.name)}
    return web.FileResponse(p, headers=headers)


def unique_path(p):
    if not p.exists():
        return p
    n = 1
    while True:
        c = p.with_name("%s (%d)%s" % (p.stem, n, p.suffix))
        if not c.exists():
            return c
        n += 1


@guard
async def api_ul(request):
    d = safe_path(request.query.get("dir", ""))
    if not d.is_dir():
        raise web.HTTPNotFound()
    saved = []
    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:
            break
        if not part.filename:
            continue
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(part.filename).name) or "dosya"
        dest = unique_path(d / name)
        with open(dest, "wb") as f:
            while True:
                chunk = await part.read_chunk(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        saved.append(dest.name)
    return web.json_response({"ok": True, "saved": saved})


# ----------------------------------------------------------------- statik sayfalar
_ICON = None


def make_icon():
    s = 180
    img = Image.new("RGB", (s, s), (11, 13, 16))
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle((28, 40, 152, 114), radius=10, outline=(77, 163, 255), width=8)
    dr.rectangle((78, 114, 102, 134), fill=(77, 163, 255))
    dr.rounded_rectangle((54, 132, 126, 142), radius=4, fill=(77, 163, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def index(request):
    return web.FileResponse(BASE / "index.html", headers={"Cache-Control": "no-cache"})


async def manifest(request):
    return web.json_response({
        "name": "Uzaktan PC", "short_name": "Uzaktan PC", "start_url": "/",
        "display": "standalone", "background_color": "#0b0d10", "theme_color": "#0b0d10",
        "icons": [{"src": "/icon.png", "sizes": "180x180", "type": "image/png"}],
    })


async def icon(request):
    global _ICON
    if _ICON is None:
        _ICON = make_icon()
    return web.Response(body=_ICON, content_type="image/png")


# ----------------------------------------------------------------- baslat
def local_ips():
    out = []
    try:
        import psutil
        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                ip = a.address
                if a.family == socket.AF_INET and not ip.startswith(("127.", "169.254.")):
                    out.append((name, ip))
    except Exception:
        try:
            out.append(("", socket.gethostbyname(socket.gethostname())))
        except Exception:
            pass
    return out


def is_tailscale(ip):
    try:
        a, b = [int(x) for x in ip.split(".")[:2]]
        return a == 100 and 64 <= b <= 127
    except ValueError:
        return False


def main():
    global CFG, SESSION, ROOT
    if SERVICE:
        sys.stdout = sys.stderr = open(BASE / "server.log", "w", buffering=1, encoding="utf-8")
        (BASE / "server.pid").write_text(str(os.getpid()))
        if not CFG_PATH.exists():
            print("config.json yok. Once kur_servis.bat ile sifre belirleyin.")
            sys.exit(1)
    CFG = load_config(reset_password="--sifre" in sys.argv)
    if "--ayarla" in sys.argv:
        print("Ayarlar kaydedildi.")
        return
    SESSION = session_token()
    ROOT = Path(CFG["files_root"]).expanduser().resolve()
    port = int(CFG["port"])

    if CFG.get("keep_awake") and sys.platform == "win32":
        # Program calisirken bilgisayar uykuya gecmesin (ekran kapanabilir)
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)

    app = web.Application()
    app.add_routes([
        web.get("/", index), web.get("/manifest.json", manifest), web.get("/icon.png", icon),
        web.post("/api/login", api_login), web.get("/api/me", api_me),
        web.get("/ws", ws_handler),
        web.get("/api/ls", api_ls), web.get("/api/dl", api_dl), web.post("/api/ul", api_ul),
    ])

    print("=" * 60)
    print(" Uzaktan PC calisiyor%s.  Kapatmak icin bu pencereyi kapat." % (" (servis modu)" if SERVICE else ""))
    print("=" * 60)
    ips = local_ips()
    ts = [ip for _, ip in ips if is_tailscale(ip)]
    for ip in ts:
        print(" Her yerden (Tailscale):  http://%s:%d" % (ip, port))
    for name, ip in ips:
        if not is_tailscale(ip):
            print(" Ayni Wi-Fi'de:           http://%s:%d   (%s)" % (ip, port, name))
    if not ts:
        print(" ! Tailscale adresi bulunamadi. Disaridan baglanmak icin Tailscale kur.")
    if not AUDIO_OK:
        print(" ! Ses aktarimi kapali (%s)" % AUDIO_WHY)
    print(" Dosya klasoru: %s" % ROOT)
    print(" Sifreyi degistirmek icin:  python server.py --sifre\n")
    web.run_app(app, host="0.0.0.0", port=port, print=None, access_log=None)


if __name__ == "__main__":
    main()
