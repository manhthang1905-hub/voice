"""
Skill: stealth — Chrome ẩn danh siêu nhẹ.

Mỗi TK = 1 Chrome profile riêng + fingerprint riêng.
Dùng DrissionPage (CDP, không phải Selenium → không bị detect webdriver).
Profile siêu nhẹ: cleanup xong chỉ ~3-5MB (giữ cookie login).

Cách dùng:
    stealth = Stealth()
    stealth.create("tk001")                    # tạo profile mới
    page = stealth.open("tk001", proxy=...)    # mở Chrome
    # ... làm gì đó ...
    page.quit()                                 # đóng Chrome
    stealth.cleanup("tk001")                    # dọn rác, 225MB → 3MB
"""

import os
import json
import shutil
import hashlib
from typing import Optional, Dict, List

from utils.logger import log

# ============================================================
# PROFILE DIR
# ============================================================

DEFAULT_PROFILES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "chrome_profiles"
)

# Port range cho Chrome instances (mỗi Chrome 1 port riêng)
BASE_PORT = 9600

# ============================================================
# JUNK — xoá sau mỗi session để profile nhẹ
# ============================================================

_JUNK_TOPLEVEL = [
    "optimization_guide_model_store", "component_crx_cache",
    "Safe Browsing", "WasmTtsEngine", "OnDeviceHeadSuggestModel",
    "GrShaderCache", "BrowserMetrics", "ShaderCache",
    "hyphen-data", "ZxcvbnData", "CertificateRevocation",
    "GraphiteDawnCache", "OptimizationHints", "Subresource Filter",
    "PKIMetadata", "Crowd Deny", "SafetyTips", "MEIPreloadData",
    "SSLErrorAssistant", "OriginTrials", "segmentation_platform",
    "ActorSafetyLists",
]

_JUNK_DEFAULT = [
    "Code Cache", "Cache", "GPUCache", "DawnWebGPUCache",
    "DawnGraphiteCache", "Service Worker", "Web Applications",
    "Shared Dictionary", "BrowsingTopicsSiteData",
    "Feature Engagement Tracker", "Download Service",
    "AutofillAiModelCache",
]

_JUNK_FILES = [
    "BrowserMetrics-spare.pma", "CrashpadMetrics-active.pma",
]

# ============================================================
# CHROME FLAGS — ngăn sinh rác ngay từ đầu
# ============================================================

# Chỉ giữ flags KHÔNG ảnh hưởng đến hoạt động web
# (đã test: --disable-background-networking phá login Google)
_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-popup-blocking",
    "--mute-audio",
]


class Stealth:
    """Quản lý Chrome profiles ẩn danh siêu nhẹ."""

    INDEX_FILE = "stealth_index.json"

    def __init__(self, profiles_dir: str = None):
        self.profiles_dir = profiles_dir or DEFAULT_PROFILES_DIR
        self._index_file = os.path.join(self.profiles_dir, self.INDEX_FILE)
        self._index: Dict[str, dict] = {}
        self._port_map: Dict[str, int] = {}  # id → port đang dùng

        os.makedirs(self.profiles_dir, exist_ok=True)
        self._load_index()

    # ============================================================
    # INDEX
    # ============================================================

    def _load_index(self):
        if os.path.exists(self._index_file):
            with open(self._index_file, "r", encoding="utf-8") as f:
                self._index = json.load(f)

    def _save_index(self):
        with open(self._index_file, "w", encoding="utf-8") as f:
            json.dump(self._index, f, indent=2, ensure_ascii=False)

    # ============================================================
    # CREATE — tạo profile mới
    # ============================================================

    def create(self, profile_id: str, email: str = "") -> dict:
        """Tạo Chrome profile mới + fingerprint riêng.

        Args:
            profile_id: ID duy nhất (vd: "tk001", email, ...)
            email: Email gắn với profile (tuỳ chọn)

        Returns: profile dict
        """
        key = profile_id.lower()
        if key in self._index:
            if email and self._index[key].get("email") != email:
                self._index[key]["email"] = email
                self._save_index()
            return self._index[key]

        # Tên folder ngắn, không chứa ký tự đặc biệt
        safe_name = hashlib.md5(key.encode()).hexdigest()[:10]
        profile_dir = os.path.join(self.profiles_dir, f"p_{safe_name}")
        os.makedirs(profile_dir, exist_ok=True)

        # Fingerprint bằng browserforge (thật hơn hardcode)
        fingerprint = self._generate_fingerprint(key)

        from datetime import datetime
        profile = {
            "id": profile_id,
            "email": email,
            "profile_dir": profile_dir,
            "fingerprint": fingerprint,
            "created_at": datetime.now().isoformat(),
            "last_used": None,
            "times_used": 0,
            "size_mb": 0,
            "gmail_logged_in": False,
            "elevenlabs_registered": False,
        }

        self._index[key] = profile
        self._save_index()
        log.info(f"Stealth: tạo profile {profile_id} → {profile_dir}")
        return profile

    # ============================================================
    # OPEN — mở Chrome với profile
    # ============================================================

    def open(self, profile_id: str, proxy: str = None,
             headless: bool = False, port: int = None):
        """Mở Chrome với profile đã tạo.

        Args:
            profile_id: ID profile
            proxy: "ip:port" (không auth) hoặc None
            headless: Chạy ẩn
            port: Port cụ thể (mặc định tự gán)

        Returns: DrissionPage.ChromiumPage
        """
        from DrissionPage import ChromiumOptions, ChromiumPage

        key = profile_id.lower()
        profile = self._index.get(key)
        if not profile:
            profile = self.create(profile_id)

        fp = profile["fingerprint"]

        # === ChromiumOptions (giống google_login.py — KHÔNG dùng read_file=False) ===
        co = ChromiumOptions()

        # Profile dir — cookie, login state tự lưu ở đây
        co.set_user_data_path(profile["profile_dir"])

        # Port riêng — mỗi Chrome 1 port
        if port is None:
            port = self._find_free_port(key)
        co.set_local_port(port)
        self._port_map[key] = port

        # Proxy — LUÔN dùng 4G, không bao giờ để Chrome dùng IP máy
        if not proxy:
            from accounts.proxy import get_chrome_proxy
            proxy = get_chrome_proxy()
        if proxy.startswith("socks"):
            chrome_proxy = self._sanitize_socks_for_chrome(proxy)
            co.set_argument(f"--proxy-server={chrome_proxy}")
        else:
            if not proxy.startswith("http"):
                proxy = f"http://{proxy}"
            co.set_proxy(proxy)

        # Fingerprint
        co.set_user_agent(fp["user_agent"])
        if key.startswith("bc_"):
            co.set_argument("--start-maximized")
        else:
            co.set_argument(f"--window-size={fp['screen_width']},{fp['screen_height']}")
        lang_code = fp["language"].split(",")[0]
        co.set_argument(f"--lang={lang_code}")

        # Stealth flags
        for arg in _STEALTH_ARGS:
            co.set_argument(arg)

        # Headless
        if headless:
            co.headless(True)

        # KHÔNG dùng eager mode — gây lỗi click Next trên Google login
        # (Google cần load đầy đủ JS/event handlers trước khi tương tác)

        # === Mở Chrome ===
        page = ChromiumPage(co)

        # KHÔNG inject fingerprint JS — Google detect và block login
        # Fingerprint chỉ qua Chrome args (user-agent, window-size, lang)

        # Cập nhật index
        from datetime import datetime
        profile["last_used"] = datetime.now().isoformat()
        profile["times_used"] += 1
        self._save_index()

        log.info(f"Stealth: mở Chrome {profile_id} (port {port})")
        return page

    def open_browser(self, profile_id: str, proxy: str = None,
                     headless: bool = False, port: int = None):
        """Mở Chrome trả về Chromium object (hỗ trợ multi-tab/popup).

        Dùng khi cần xử lý popup windows (VD: Google OAuth).
        Khác open(): trả về Chromium thay vì ChromiumPage.

        Returns: (browser, tab) — browser = Chromium, tab = MixTab chính
        """
        from DrissionPage import Chromium, ChromiumOptions

        key = profile_id.lower()
        profile = self._index.get(key)
        if not profile:
            profile = self.create(profile_id)

        fp = profile["fingerprint"]

        co = ChromiumOptions()
        co.set_user_data_path(profile["profile_dir"])

        if port is None:
            port = self._find_free_port(key)
        co.set_local_port(port)
        self._port_map[key] = port

        # Proxy — LUÔN dùng 4G
        if not proxy:
            from accounts.proxy import get_chrome_proxy
            proxy = get_chrome_proxy()
        if proxy.startswith("socks"):
            chrome_proxy = self._sanitize_socks_for_chrome(proxy)
            co.set_argument(f"--proxy-server={chrome_proxy}")
        else:
            if not proxy.startswith("http"):
                proxy = f"http://{proxy}"
            co.set_proxy(proxy)

        co.set_user_agent(fp["user_agent"])
        if key.startswith("bc_"):
            co.set_argument("--start-maximized")
        else:
            co.set_argument(f"--window-size={fp['screen_width']},{fp['screen_height']}")
        lang_code = fp["language"].split(",")[0]
        co.set_argument(f"--lang={lang_code}")

        for arg in _STEALTH_ARGS:
            co.set_argument(arg)

        if headless:
            co.headless(True)

        browser = Chromium(co)
        tab = browser.latest_tab

        from datetime import datetime
        profile["last_used"] = datetime.now().isoformat()
        profile["times_used"] += 1
        self._save_index()

        log.info(f"Stealth: mở Browser {profile_id} (port {port})")
        return browser, tab

    # ============================================================
    # CLEANUP — dọn rác, giữ cookie
    # ============================================================

    def cleanup(self, profile_id: str) -> float:
        """Xoá rác trong profile, giữ cookie + login state.

        Returns: MB đã tiết kiệm
        """
        key = profile_id.lower()
        profile = self._index.get(key)
        if not profile:
            return 0

        profile_dir = profile["profile_dir"]
        if not os.path.exists(profile_dir):
            return 0

        saved = 0

        # Xoá folders rác top-level
        for dirname in _JUNK_TOPLEVEL:
            path = os.path.join(profile_dir, dirname)
            if os.path.exists(path):
                saved += self._dir_size(path)
                shutil.rmtree(path, ignore_errors=True)

        # Xoá files rác top-level
        for fname in _JUNK_FILES:
            path = os.path.join(profile_dir, fname)
            if os.path.exists(path):
                try:
                    saved += os.path.getsize(path)
                    os.remove(path)
                except OSError:
                    pass

        # Xoá folders rác trong Default/
        default_dir = os.path.join(profile_dir, "Default")
        if os.path.exists(default_dir):
            for dirname in _JUNK_DEFAULT:
                path = os.path.join(default_dir, dirname)
                if os.path.exists(path):
                    saved += self._dir_size(path)
                    shutil.rmtree(path, ignore_errors=True)

        saved_mb = saved / (1024 * 1024)
        profile["size_mb"] = round(self._dir_size(profile_dir) / (1024 * 1024), 1)
        self._save_index()

        if saved_mb > 0.1:
            log.info(f"Stealth: cleanup {profile_id}, tiết kiệm {saved_mb:.1f}MB "
                     f"→ còn {profile['size_mb']}MB")
        return saved_mb

    def cleanup_all(self) -> float:
        """Dọn rác tất cả profiles."""
        total = 0
        for key in list(self._index.keys()):
            total += self.cleanup(key)
        return total

    # ============================================================
    # DELETE — xoá hoàn toàn profile
    # ============================================================

    def delete(self, profile_id: str):
        """Xoá hoàn toàn 1 profile (khi TK die)."""
        key = profile_id.lower()
        profile = self._index.get(key)
        if not profile:
            return

        if os.path.exists(profile["profile_dir"]):
            shutil.rmtree(profile["profile_dir"], ignore_errors=True)

        del self._index[key]
        self._port_map.pop(key, None)
        self._save_index()
        log.info(f"Stealth: xoá profile {profile_id}")

    # ============================================================
    # LIST / STATS
    # ============================================================

    def list_profiles(self) -> List[dict]:
        """Danh sách tất cả profiles."""
        return list(self._index.values())

    def get_profile(self, profile_id: str) -> Optional[dict]:
        return self._index.get(profile_id.lower())

    def get_stats(self) -> dict:
        """Thống kê tổng."""
        total = len(self._index)
        total_size = sum(p.get("size_mb", 0) for p in self._index.values())
        gmail_ok = sum(1 for p in self._index.values() if p.get("gmail_logged_in"))
        el_ok = sum(1 for p in self._index.values() if p.get("elevenlabs_registered"))
        return {
            "total_profiles": total,
            "total_size_mb": round(total_size, 1),
            "gmail_logged_in": gmail_ok,
            "elevenlabs_registered": el_ok,
        }

    def mark_gmail_logged_in(self, profile_id: str):
        key = profile_id.lower()
        if key in self._index:
            self._index[key]["gmail_logged_in"] = True
            self._save_index()

    def mark_elevenlabs_registered(self, profile_id: str):
        key = profile_id.lower()
        if key in self._index:
            self._index[key]["elevenlabs_registered"] = True
            self._save_index()

    # ============================================================
    # FINGERPRINT
    # ============================================================

    def _generate_fingerprint(self, seed_str: str) -> dict:
        """Tạo fingerprint bằng browserforge (thật hơn hardcode)."""
        try:
            from browserforge.fingerprints import FingerprintGenerator

            fg = FingerprintGenerator()
            fp = fg.generate(browser="chrome", os="windows")

            return {
                "user_agent": fp.navigator.userAgent,
                "screen_width": fp.screen.width,
                "screen_height": fp.screen.height,
                "language": ",".join(fp.navigator.languages) if fp.navigator.languages else "en-US,en",
                "platform": fp.navigator.platform or "Win32",
                "timezone": "America/New_York",
                "webgl_vendor": fp.videoCard.vendor if fp.videoCard else "Google Inc. (NVIDIA)",
                "webgl_renderer": fp.videoCard.renderer if fp.videoCard else "ANGLE (NVIDIA GeForce GTX 1060)",
            }
        except Exception as e:
            log.warning(f"browserforge lỗi ({e}), dùng fingerprint mặc định")
            return self._fallback_fingerprint(seed_str)

    def _fallback_fingerprint(self, seed_str: str) -> dict:
        """Fingerprint dự phòng khi browserforge lỗi."""
        import random
        rng = random.Random(hash(seed_str))

        uas = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        ]
        screens = [(1920, 1080), (1366, 768), (1536, 864), (1440, 900)]

        w, h = rng.choice(screens)
        return {
            "user_agent": rng.choice(uas),
            "screen_width": w,
            "screen_height": h,
            "language": "en-US,en",
            "platform": "Win32",
            "timezone": rng.choice(["America/New_York", "America/Chicago", "Europe/London"]),
            "webgl_vendor": "Google Inc. (NVIDIA)",
            "webgl_renderer": "ANGLE (NVIDIA GeForce GTX 1060)",
        }

    def _inject_fingerprint(self, page, fp: dict):
        """Inject JS để override fingerprint trên trang web."""
        js = """
        Object.defineProperty(navigator, 'platform', {get: () => '%s'});
        Object.defineProperty(navigator, 'languages', {get: () => %s});
        Object.defineProperty(screen, 'width', {get: () => %d});
        Object.defineProperty(screen, 'height', {get: () => %d});
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {
            get: () => [{name:'Chrome PDF Plugin'},{name:'Chrome PDF Viewer'},{name:'Native Client'}]
        });
        """ % (
            fp["platform"],
            json.dumps(fp["language"].split(",")),
            fp["screen_width"], fp["screen_height"],
        )
        try:
            page.run_js(js)
        except Exception:
            pass

    # ============================================================
    # HELPERS
    # ============================================================

    @staticmethod
    def _sanitize_socks_for_chrome(proxy_url: str) -> str:
        """Chuyển SOCKS5 proxy URL thành format Chrome hiểu.

        Chrome không hỗ trợ:
        - socks5h:// (DNS qua proxy) → chuyển thành socks5://
        - SOCKS5 auth (user:pass@) → strip auth
        - Gateway port 5000 (cần auth) → chuyển về port 10001 (direct, no auth)

        Máy chính có thể dùng localhost; máy khác cùng mạng phải dùng SOCKS host trong proxy.json.

        Ví dụ:
            socks5h://KEY:x@192.168.88.254:5000 → socks5://192.168.88.254:10001
            socks5://192.168.88.254:10001        → socks5://192.168.88.254:10001
        """
        from accounts.proxy import SOCKS5_HOST, SOCKS5_PORT
        return f"socks5://{SOCKS5_HOST}:{SOCKS5_PORT}"

    def _find_free_port(self, key: str) -> int:
        """Tìm port chưa dùng."""
        used_ports = set(self._port_map.values())
        # Hash key để mỗi profile có port ổn định
        preferred = BASE_PORT + (hash(key) % 10000)
        if preferred not in used_ports:
            return preferred
        # Fallback: tìm port trống
        for p in range(BASE_PORT, BASE_PORT + 10000):
            if p not in used_ports:
                return p
        return BASE_PORT

    @staticmethod
    def _dir_size(path: str) -> int:
        """Tính dung lượng thư mục (bytes)."""
        total = 0
        try:
            for dirpath, _, filenames in os.walk(path):
                for f in filenames:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, f))
                    except OSError:
                        pass
        except OSError:
            pass
        return total
