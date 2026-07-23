"""
Skill: proxy — Quản lý 4G proxy.

Gateway: socks5://11lab:x@localhost:5000
API:     http://localhost:19800

Mỗi nhóm TK = 1 session = 1 IP cố định.
Đổi IP = đổi session ID.

Cách dùng:
    proxy = Proxy4G()

    # Proxy chung (IP xoay mỗi request)
    proxy.get()                   # "socks5://11lab:x@localhost:5000"
    proxy.get_for_requests()      # dict cho requests

    # Proxy sticky (giữ IP cho 1 nhóm TK)
    proxy.get_sticky("nhom-1")    # "socks5://11lab-session-nhom-1:x@localhost:5000"

    # Đổi IP
    proxy.rotate()                # đổi IP (session mới)
    proxy.new_session("nhom-2")   # session mới cho nhóm

    # Báo cáo IP xấu
    proxy.report_captcha()        # IP bị CAPTCHA → cooldown
    proxy.report_blocked(429)     # IP bị block
"""

import os
import time
import requests as req
from typing import Optional, Dict
from utils.logger import log

# Load config
import json as _json
_CFG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "proxy.json")

def _load_cfg():
    try:
        with open(_CFG_PATH, 'r') as f:
            return _json.load(f)
    except Exception:
        return {}

_cfg = _load_cfg()

# Default config (khi copy may khac -> sua qua nut 'Cai dat 4G' o tab 4G Proxy)
_DEFAULTS = {
    "socks5_host": "127.0.0.1",   # PC chinh: 127.0.0.1 | May ao: IP may chinh (192.168.x.x)
    "socks5_port": 10001,         # PC: 10001 | VM relay: 10002
    "api_host": "192.168.88.254", # IP may chay 4G server
    "api_port": 19800,            # cong API gateway
    "gateway_port": 5000,         # cong SOCKS5 gateway
    "api_key": "mimi-4g-proxy-2026",
    "vm_sticky_session": "vmdefault",
}


def _g(key):
    return _cfg.get(key, _DEFAULTS.get(key))


# PC chính (có phone): SOCKS5_HOST = 127.0.0.1
# Máy ảo: SOCKS5_HOST = 192.168.88.254 (IP máy chính)
SOCKS5_HOST = _g("socks5_host")
SOCKS5_PORT = _g("socks5_port")
GATEWAY_HOST = _g("api_host")
GATEWAY_PORT = _g("gateway_port")
GATEWAY_KEY = _g("api_key")
GATEWAY = f"socks5h://{GATEWAY_KEY}:x@{GATEWAY_HOST}:{GATEWAY_PORT}"
GATEWAY_DIRECT_DNS = GATEWAY
VM_STICKY_SESSION = _g("vm_sticky_session")
API_BASE = f"http://{GATEWAY_HOST}:{_g('api_port')}"
API_KEY = GATEWAY_KEY


def reload_config():
    """Doc lai config/proxy.json va cap nhat cac bien module (ap dung NGAY,
    khong can restart) -> sau khi sua qua dialog 'Cai dat 4G'."""
    global _cfg, SOCKS5_HOST, SOCKS5_PORT, GATEWAY_HOST, GATEWAY_PORT
    global GATEWAY_KEY, GATEWAY, GATEWAY_DIRECT_DNS, VM_STICKY_SESSION
    global API_BASE, API_KEY
    _cfg = _load_cfg()
    SOCKS5_HOST = _g("socks5_host")
    SOCKS5_PORT = _g("socks5_port")
    GATEWAY_HOST = _g("api_host")
    GATEWAY_PORT = _g("gateway_port")
    GATEWAY_KEY = _g("api_key")
    GATEWAY = f"socks5h://{GATEWAY_KEY}:x@{GATEWAY_HOST}:{GATEWAY_PORT}"
    GATEWAY_DIRECT_DNS = GATEWAY
    VM_STICKY_SESSION = _g("vm_sticky_session")
    API_BASE = f"http://{GATEWAY_HOST}:{_g('api_port')}"
    API_KEY = GATEWAY_KEY
    return dict(_cfg)


def save_config(updates: dict):
    """Ghi config/proxy.json (merge) roi reload. -> dict config moi."""
    cur = _load_cfg()
    cur.update({k: v for k, v in updates.items() if v is not None})
    with open(_CFG_PATH, "w", encoding="utf-8") as f:
        _json.dump(cur, f, indent=2, ensure_ascii=False)
    return reload_config()


def get_config():
    """Config hien tai (da merge default) de hien len dialog."""
    out = dict(_DEFAULTS)
    out.update(_load_cfg())
    return out


_flagged_ips = set()
_current_ip = ""


def get_chrome_proxy() -> str:
    """Lấy proxy cho Chrome (port 10001 = ADB forward → EveryProxy trên phone)."""
    return f"socks5://{SOCKS5_HOST}:{SOCKS5_PORT}"


def get_clean_ip(proxy4g=None) -> str:
    """Lấy IP sạch — đổi nếu IP hiện tại bị flag."""
    global _current_ip
    if not proxy4g:
        proxy4g = Proxy4G()

    ip = proxy4g.get_ip()
    if ip and ip in _flagged_ips:
        log.warning(f"IP {ip} đã bị flag, đổi IP mới...")
        proxy4g.rotate(wait=45)
        ip = proxy4g.get_ip()

    _current_ip = ip or ""
    return _current_ip


def flag_current_ip(reason: str = ""):
    """Đánh dấu IP hiện tại bị flag — lần sau sẽ tự đổi."""
    global _current_ip
    if _current_ip:
        _flagged_ips.add(_current_ip)
        log.warning(f"IP {_current_ip} flagged: {reason}")
        log.info(f"Flagged IPs: {_flagged_ips}")


class Proxy4G:
    """4G Proxy qua gateway local."""

    def __init__(self):
        self._session_counter = 0

    # ============================================================
    # GET PROXY
    # ============================================================

    def get(self) -> str:
        """Proxy rotating (IP mới mỗi request)."""
        return GATEWAY

    def get_sticky(self, session_id: str) -> str:
        """Proxy sticky (giữ IP cho 1 session/nhóm TK).

        Cùng session_id = cùng IP.
        Đổi session_id = IP khác.
        """
        safe = "".join(ch for ch in str(session_id or "") if ch.isalnum())
        if not safe:
            safe = VM_STICKY_SESSION
        return f"socks5h://{GATEWAY_KEY}-session-{safe}:x@{GATEWAY_HOST}:{GATEWAY_PORT}"

    def get_for_requests(self, session_id: str = None) -> dict:
        """Return a dict for requests.

        PC chinh co phone: di truc tiep 127.0.0.1:10001.
        May ao/LAN: di truc tiep qua LAN relay socks5 (10002),
        bo qua gateway :5000 de tranh rot route giua cac request TTS.
        """
        proxy = f"socks5h://{SOCKS5_HOST}:{SOCKS5_PORT}"
        return {"http": proxy, "https": proxy}

    def get_for_firebase(self, session_id: str = None) -> dict:
        """Firebase/login route.

        Giu dong nhat voi requests route hien tai tren VM:
        di qua LAN relay socks5 de tranh gateway :5000.
        """
        return self.get_for_requests(session_id)


    def get_for_chrome(self, session_id: str = None) -> str:
        """Proxy cho Chrome — ADB forward port 10001 → EveryProxy trên phone.
        Không cần auth. ADB forward listen trên localhost.
        """
        return f"socks5://{SOCKS5_HOST}:{SOCKS5_PORT}"

    def get_chrome_args(self, session_id: str = None) -> list:
        """Chrome arguments cho proxy."""
        return [f"--proxy-server=socks5://{SOCKS5_HOST}:{SOCKS5_PORT}"]

    # ============================================================
    # ROTATE / SESSION
    # ============================================================

    def rotate(self, device: str = None, wait: int = 20) -> bool:
        """Đổi IP. Server-side wait (ADB airplane toggle + reconnect)."""
        try:
            # Lấy device_id nếu không chỉ định
            if not device:
                info = self.get_info()
                proxies = info.get("proxies", info.get("devices", []))
                if proxies:
                    device = proxies[0].get("id", "")
            
            if device:
                url = (f"{API_BASE}/rotate/{device}"
                       f"?key={API_KEY}&wait={wait}")
            else:
                url = f"{API_BASE}/rotate-all?key={API_KEY}"
            
            r = req.post(url, timeout=90)
            if r.status_code == 200:
                data = r.json()
                new_ip = data.get("new_ip", "")
                log.info(f"4G: rotate OK → {new_ip}")
                return True
            log.warning(f"4G: rotate fail {r.status_code}")
            return False
        except Exception as e:
            log.warning(f"4G: rotate error {e}")
            return False

    def new_session(self, session_id: str = None) -> str:
        """Tạo session mới (= IP mới).

        Returns: proxy string cho session mới
        """
        if not session_id:
            self._session_counter += 1
            session_id = f"s{self._session_counter}"
        return self.get_sticky(session_id)

    # ============================================================
    # REPORT (giữ IP sạch)
    # ============================================================

    def report_captcha(self):
        """Báo gặp CAPTCHA → IP cooldown."""
        try:
            r = req.post(f"{API_BASE}/guard/captcha?key={API_KEY}", timeout=5)
            log.info(f"4G: reported CAPTCHA")
        except Exception:
            pass

    def report_blocked(self, code: int = 429):
        """Báo bị block → IP cooldown."""
        try:
            r = req.post(f"{API_BASE}/guard/error?code={code}&key={API_KEY}", timeout=5)
            log.info(f"4G: reported block {code}")
        except Exception:
            pass

    # ============================================================
    # INFO
    # ============================================================

    def is_available(self) -> bool:
        """4G proxy có sẵn không (gateway đang chạy)."""
        try:
            r = req.get(f"{API_BASE}/list?key={API_KEY}", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def get_ip(self) -> str:
        """IP hien tai qua 4G.

        Uu tien IP do 4G server bao ve de VM khong tu danh gia proxy chet oan.
        Chi fallback sang ipify khi server chua co current_4g_ip.
        """
        try:
            info = self.get_info()
            proxies = info.get("proxies", info.get("devices", []))
            if proxies:
                current_ip = (proxies[0].get("current_4g_ip") or "").strip()
                if current_ip:
                    return current_ip
        except Exception:
            pass
        try:
            r = req.get("https://api.ipify.org",
                        proxies=self.get_for_requests(), timeout=10)
            return r.text.strip()
        except Exception:
            return ""

    def get_info(self) -> dict:
        """Thông tin 4G proxy."""
        try:
            r = req.get(f"{API_BASE}/list?key={API_KEY}", timeout=5)
            return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}

    def get_devices(self) -> list:
        """Danh sách phone/device."""
        info = self.get_info()
        if isinstance(info, list):
            return info
        return info.get("devices", info.get("proxies", []))

    def scan_devices(self, timeout: int = 30) -> dict:
        """Goi /scan de server QUET LAI + KHOI DONG forward cho cac device.

        Dung khi proxy 'chet gia' (socks5 chua forward nhung phone van ket noi) —
        chi can scan la song lai. -> dict ket qua scan (hoac {} neu loi).
        """
        try:
            r = req.post(f"{API_BASE}/scan?key={API_KEY}", timeout=timeout)
            return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}

    def heal(self, timeout: int = 60) -> dict:
        """Goi /heal — TU FIX moi loi tren dien thoai (watchdog):
        dismiss dialog USB (cam lai cap), tu bat EveryProxy, stay-awake, restart forward.
        -> dict ket qua (hoac {} neu loi).
        """
        try:
            r = req.post(f"{API_BASE}/heal?key={API_KEY}", timeout=timeout)
            return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}

    def ensure_alive(self, on_log=lambda *_: None, max_try: int = 2) -> bool:
        """Dam bao 4G THUC SU forward duoc (khong bao chet oan).

        Bug da gap: server bao proxy_running=false / get_ip rong, nhung chi can SCAN
        thiet bi la song lai. Ham nay: neu chua san sang -> scan + start + re-check.
        -> True neu 4G san sang forward, False neu that su chet.
        """
        for attempt in range(max_try):
            info = self.get_info()
            proxies = info.get("proxies", info.get("devices", []))
            if not proxies:
                on_log("  ⚠ 4G: khong co phone -> scan lai...")
                self.scan_devices()
                time.sleep(2)
                continue
            ph = proxies[0]
            running = ph.get("proxy_running", False)
            ip = (ph.get("current_4g_ip") or "").strip()
            # API bao san sang -> VAN test LUONG THAT (API co the bao OK nhung socks5 reset)
            if running and ip:
                if self.probe_socks5():
                    return True
                on_log("  ⚠ 4G: API bao OK nhung luong socks5 RESET -> tu HEAL dien thoai...")
            else:
                on_log(f"  ⚠ 4G chua san sang (running={running}) -> tu HEAL...")
            # TU HEAL: dismiss dialog USB + bat EveryProxy + stay-awake + restart forward
            # (manh hon scan don thuan — xu ly ca truong hop cam lai cap/EveryProxy chet).
            self.heal()
            time.sleep(3)
            if self.probe_socks5():
                on_log(f"  ✓ 4G luong thong lai sau heal (IP {self.get_ip()})")
                return True
        return False

    def probe_socks5(self, timeout: int = 12) -> bool:
        """TEST LUONG THAT: gui 1 request nho qua socks5 :10001 -> True neu di duoc.

        Khac get_ip() (chi hoi API server) — cai nay test forward THAT su den internet.
        Bat duoc truong hop 'API bao OK nhung socks5 reset' (ConnectionReset 10054).
        """
        try:
            r = req.get("http://api.ipify.org", proxies=self.get_for_requests(),
                        timeout=timeout)
            return r.status_code == 200 and len(r.text) >= 7
        except Exception:
            return False

    def test_device(self, device: str) -> dict:
        """Test 1 device (IP, latency)."""
        try:
            r = req.get(f"{API_BASE}/test/{device}?key={API_KEY}", timeout=15)
            return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}
