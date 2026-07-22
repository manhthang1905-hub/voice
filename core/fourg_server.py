"""
fourg_server.py — TU DONG chay 4G proxy server NEN khi mo tool voice.

Truoc day phai chay rieng "START_4G_SERVER.bat". Gio tool tu lo:
  - Neu API server 4G da chay (port 19800 listen) -> KHONG chay lai (may khac/da co).
  - Neu may CO dien thoai cam ADB -> chay server.py + tcp_relay nen (nhu .bat).
  - Neu may KHONG co dien thoai (vd may phu dung LAN cua may chinh) -> bo qua,
    KHONG bao loi (may do tro proxy sang IP LAN may chinh o tab 4G Proxy).

Goi tu run.py / main() luc khoi dong. Chay 1 lan, khong block GUI (thread nen).
"""
import os
import sys
import time
import socket
import subprocess

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FOURG_DIR = os.path.join(_PROJECT_ROOT, "fourg")
_ADB = os.path.join(_PROJECT_ROOT, "tools", "platform-tools", "adb.exe")

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    """Port co ai listen khong (nhanh)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        r = s.connect_ex((host, port))
        s.close()
        return r == 0
    except Exception:
        return False


def _has_adb_device() -> bool:
    """May nay co dien thoai cam ADB khong? (co -> chay 4G server o day)."""
    if not os.path.exists(_ADB):
        return False
    try:
        out = subprocess.run([_ADB, "devices"], capture_output=True, text=True,
                             timeout=15, creationflags=_CREATE_NO_WINDOW)
        # Dong dang 'XXXXX\tdevice' (bo dong header 'List of devices')
        for line in (out.stdout or "").splitlines()[1:]:
            if line.strip().endswith("\tdevice"):
                return True
    except Exception:
        pass
    return False


def _spawn(args, cwd=None):
    """Chay 1 tien trinh nen, an cua so, khong giu tham chieu (fire-and-forget)."""
    try:
        return subprocess.Popen(
            args, cwd=cwd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
            close_fds=True)
    except Exception:
        return None


def ensure_4g_server(on_log=lambda *_: None) -> str:
    """Dam bao 4G server dang chay (neu may co dien thoai). -> trang thai (str).

    Tra:
      'already'   - API 19800 da chay san (khong lam gi).
      'started'   - vua chay server + relay tren may nay.
      'no_device' - may khong co dien thoai (dung LAN cua may chinh) -> bo qua.
      'no_files'  - thieu file 4g (chua copy) -> bo qua.
    """
    if not os.path.isdir(_FOURG_DIR) or not os.path.exists(os.path.join(_FOURG_DIR, "server.py")):
        return "no_files"

    # 1. API server da chay roi? -> khong chay lai
    if _port_listening(19800):
        on_log("[4G] Server da chay (port 19800) — dung lai.")
        return "already"

    # 2. May nay co dien thoai ADB khong? khong -> may phu dung LAN, bo qua
    if not _has_adb_device():
        on_log("[4G] May nay khong co dien thoai ADB -> bo qua (dung LAN may chinh neu can).")
        return "no_device"

    # 3. Chay: TCP relay (LAN) + server.py (API + gateway + adb socks5)
    # UU TIEN pythonw.exe (khong cua so) du tool dang chay bang python.exe -> tranh CMD den.
    python = sys.executable
    if os.name == "nt" and python.lower().endswith("python.exe"):
        _pw = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.path.exists(_pw):
            python = _pw
    on_log("[4G] May co dien thoai -> tu chay 4G server nen...")

    # TCP relay: 0.0.0.0:10002 -> 127.0.0.1:10001 (cho may phu dung qua LAN)
    _spawn([python, os.path.join(_FOURG_DIR, "tcp_relay.py"), "10002", "127.0.0.1", "10001"],
           cwd=_FOURG_DIR)
    time.sleep(0.5)

    # server.py: scan device + start adb socks5 (:10001) + gateway (:5000) + API (:19800)
    _spawn([python, os.path.join(_FOURG_DIR, "server.py")], cwd=_FOURG_DIR)

    # Doi API len (toi da ~20s)
    for _ in range(40):
        if _port_listening(19800):
            on_log("[4G] Server 4G da san sang (API 19800).")
            return "started"
        time.sleep(0.5)
    on_log("[4G] Da khoi dong server nhung API chua phan hoi (co the dang scan device).")
    return "started"


if __name__ == "__main__":
    print("Test ensure_4g_server:")
    print("->", ensure_4g_server(on_log=lambda m: print(m)))
