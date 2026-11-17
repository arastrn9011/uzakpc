"""Uzaktan PC servis yoneticisi.

SYSTEM hesabiyla bilgisayar acilirken baslar (zamanlanmis gorev). Sunucuyu (server.py)
aktif konsol oturumunda SYSTEM yetkisiyle calistirir; boylece giris/kilit ekrani da
gorunur ve kontrol edilir. Oturum degisirse (cikis, kullanici degistirme) sunucuyu yeniden baslatir.
Ctrl+Alt+Del istegini (sas.trigger dosyasi) burada, session 0'da gonderir.
"""
import ctypes
import subprocess
import sys
import time
from pathlib import Path

import win32api
import win32con
import win32event
import win32process
import win32profile
import win32security
import win32ts

BASE = Path(__file__).resolve().parent
LOG = BASE / "servis.log"
PIDF = BASE / "server.pid"
TRIGGER = BASE / "sas.trigger"
NO_SESSION = 0xFFFFFFFF


def log(msg):
    try:
        if LOG.exists() and LOG.stat().st_size > 1_000_000:
            LOG.unlink()
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def kill_tree(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                   creationflags=0x08000000)


def kill_stale():
    try:
        pid = int(PIDF.read_text().strip())
        kill_tree(pid)
        PIDF.unlink()
    except Exception:
        pass


def launch(session_id):
    proc_tok = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_ALL_ACCESS)
    for priv in ("SeTcbPrivilege", "SeAssignPrimaryTokenPrivilege", "SeIncreaseQuotaPrivilege"):
        try:
            win32security.AdjustTokenPrivileges(
                proc_tok, False, [(win32security.LookupPrivilegeValue(None, priv), win32con.SE_PRIVILEGE_ENABLED)])
        except Exception:
            pass
    tok = win32security.DuplicateTokenEx(
        proc_tok, win32security.SecurityImpersonation, win32con.TOKEN_ALL_ACCESS, win32security.TokenPrimary)
    win32security.SetTokenInformation(tok, win32security.TokenSessionId, session_id)
    env = win32profile.CreateEnvironmentBlock(tok, False)
    si = win32process.STARTUPINFO()
    si.lpDesktop = "winsta0\\default"
    cmd = '"%s" "%s" --servis' % (sys.executable, BASE / "server.py")
    hp, ht, pid, _ = win32process.CreateProcessAsUser(
        tok, None, cmd, None, None, False,
        win32con.CREATE_NO_WINDOW | win32con.CREATE_UNICODE_ENVIRONMENT, env, str(BASE), si)
    win32api.CloseHandle(ht)
    return hp, pid


def alive(handle):
    return win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_TIMEOUT


def main():
    log("Servis yoneticisi basladi")
    kill_stale()
    child, child_sid, started = None, None, 0.0
    while True:
        try:
            sid = win32ts.WTSGetActiveConsoleSessionId()
            if child and (not alive(child[0]) or sid != child_sid):
                if alive(child[0]):
                    log("Oturum degisti (%s -> %s), sunucu yeniden baslatiliyor" % (child_sid, sid))
                    kill_tree(child[1])
                else:
                    log("Sunucu kapandi")
                    if time.time() - started < 10:
                        time.sleep(10)  # surekli coken sunucuyu hizla yeniden baslatma
                win32api.CloseHandle(child[0])
                child = None
            if child is None and sid != NO_SESSION:
                child = launch(sid)
                child_sid, started = sid, time.time()
                log("Sunucu baslatildi (oturum %s, pid %s)" % (sid, child[1]))
            if TRIGGER.exists():
                TRIGGER.unlink()
                try:
                    ctypes.WinDLL("sas").SendSAS(0)
                    log("Ctrl+Alt+Del gonderildi")
                except Exception as exc:
                    log("Ctrl+Alt+Del gonderilemedi: %s" % exc)
        except Exception as exc:
            log("Hata: %s" % exc)
            time.sleep(3)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
