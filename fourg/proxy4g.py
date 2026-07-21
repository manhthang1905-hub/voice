"""
4G Proxy Client SDK — Import vào tool khác, dùng ngay.

Cách dùng đơn giản nhất:
    from proxy4g import Proxy4G
    p = Proxy4G()
    proxy = p.get()           # lấy proxy string
    p.rotate()                # đổi IP
    new_proxy = p.get()       # proxy mới

Cách dùng với requests:
    import requests
    from proxy4g import Proxy4G
    p = Proxy4G()
    r = requests.get("https://example.com", proxies=p.for_requests())

Cách dùng auto-rotate (tự đổi IP sau mỗi N request):
    p = Proxy4G(auto_rotate_every=10)
    for url in urls:
        r = requests.get(url, proxies=p.use())  # tự đổi IP sau 10 lần

Nhiều phone:
    p = Proxy4G()
    proxies = p.list()                    # danh sách tất cả proxy
    p.rotate(device="ES2BA80614012869")   # đổi IP phone cụ thể
    p.rotate_all()                        # đổi IP tất cả

CÁCH MỚI — Backconnect Gateway (chuẩn ngành, đơn giản nhất):
    Tool khác chỉ cần ĐIỀN 1 DÒNG PROXY, không cần SDK:

    # IP mới mỗi request
    proxy = "socks5://mykey:x@192.168.88.254:5000"

    # Sticky session (giữ IP)
    proxy = "socks5://mykey-session-abc123:x@192.168.88.254:5000"

    # Đổi IP = đổi session ID bất kỳ
    proxy = "socks5://mykey-session-xyz789:x@192.168.88.254:5000"

    # Chọn phone cụ thể
    proxy = "socks5://mykey-phone-1:x@192.168.88.254:5000"

    # Dùng với requests/curl/bất kỳ tool nào:
    requests.get(url, proxies={"http": proxy, "https": proxy})
    # curl --proxy socks5://mykey:x@192.168.88.254:5000 https://ipinfo.io
"""

import urllib.request
import json
import time
import threading

class Proxy4G:
    def __init__(self, host="127.0.0.1", port=19800, key="mimi-4g-proxy-2026",
                 device=None, auto_rotate_every=0):
        """
        Args:
            host: IP của máy chạy 4G Proxy Manager
            port: Port API server (default 19800)
            key: API key
            device: Device ID cụ thể (None = dùng phone đầu tiên)
            auto_rotate_every: Tự đổi IP sau N lần gọi use() (0 = tắt)
        """
        self.base_url = f"http://{host}:{port}"
        self.key = key
        self.device = device
        self.auto_rotate_every = auto_rotate_every
        self._use_count = 0
        self._lock = threading.Lock()
        self._callbacks = []  # on_rotate callbacks
        self._cache = None
        self._cache_time = 0

    def _api(self, path, method="GET", timeout=30):
        """Gọi API server"""
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method=method,
                                      headers={"X-API-Key": self.key})
        if method == "POST":
            req.data = b"{}"
            req.add_header("Content-Type", "application/json")
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())

    def _get_device(self):
        """Lấy device ID (dùng phone đầu tiên nếu chưa chỉ định)"""
        if self.device:
            return self.device
        data = self._api("/list")
        proxies = data.get("proxies", [])
        if proxies:
            self.device = proxies[0]["id"]
            return self.device
        raise Exception("Không có phone nào kết nối")

    # ========================
    # CÁC HÀM CHÍNH
    # ========================

    def get(self, device=None):
        """Lấy proxy string: socks5://127.0.0.1:10001"""
        dev = device or self._get_device()
        data = self._api(f"/proxy/{dev}")
        return data.get("proxy_addr", f"socks5://127.0.0.1:{data.get('port', 10001)}")

    def get_ip(self, device=None):
        """Lấy IP 4G hiện tại"""
        dev = device or self._get_device()
        data = self._api(f"/proxy/{dev}")
        return data.get("current_4g_ip")

    def get_port(self, device=None):
        """Lấy port proxy"""
        dev = device or self._get_device()
        data = self._api(f"/proxy/{dev}")
        return data.get("port", 10001)

    def rotate(self, device=None, wait=8):
        """Đổi IP. Trả về IP mới."""
        dev = device or self._get_device()
        data = self._api(f"/rotate/{dev}?wait={wait}", method="POST", timeout=60)
        new_ip = data.get("new_ip")
        # Gọi callbacks
        for cb in self._callbacks:
            try:
                cb(dev, new_ip)
            except:
                pass
        return new_ip

    def rotate_all(self, wait=8):
        """Đổi IP tất cả phones. Trả về dict {device_id: new_ip}"""
        data = self._api(f"/rotate-all?wait={wait}", method="POST", timeout=120)
        return data.get("results", {})

    def list(self):
        """Danh sách tất cả proxy"""
        data = self._api("/list")
        return data.get("proxies", [])

    def test(self, device=None):
        """Test proxy, trả về IP thật + latency"""
        dev = device or self._get_device()
        return self._api(f"/test/{dev}")

    # ========================
    # DÙNG VỚI REQUESTS / HTTPX
    # ========================

    def for_requests(self, device=None):
        """Trả về dict proxies cho requests library
        Usage: requests.get(url, proxies=p.for_requests())
        """
        proxy = self.get(device)
        return {"http": proxy, "https": proxy}

    def for_httpx(self, device=None):
        """Trả về proxy string cho httpx
        Usage: httpx.get(url, proxy=p.for_httpx())
        """
        return self.get(device)

    # ========================
    # AUTO ROTATE
    # ========================

    def use(self, device=None):
        """Dùng proxy + tự đổi IP nếu đạt auto_rotate_every
        Usage: requests.get(url, proxies=p.use())
        """
        with self._lock:
            self._use_count += 1
            if self.auto_rotate_every > 0 and self._use_count >= self.auto_rotate_every:
                self._use_count = 0
                # Rotate trong background
                threading.Thread(target=self.rotate, args=(device,), daemon=True).start()
                time.sleep(0.5)  # Chờ chút
        return self.for_requests(device)

    # ========================
    # CALLBACKS / WEBHOOKS
    # ========================

    def on_rotate(self, callback):
        """Đăng ký callback khi IP đổi
        callback(device_id, new_ip)
        """
        self._callbacks.append(callback)

    # ========================
    # QUEUE - NHIỀU TOOL CÙNG DÙNG
    # ========================

    def request_rotate(self, device=None, callback=None):
        """Gửi yêu cầu đổi IP (non-blocking)
        Callback sẽ được gọi khi xong: callback(device_id, new_ip)
        """
        def _do():
            new_ip = self.rotate(device)
            if callback:
                callback(device or self.device, new_ip)
        t = threading.Thread(target=_do, daemon=True)
        t.start()
        return t

    # ========================
    # TIỆN ÍCH
    # ========================

    def wait_ready(self, timeout=30):
        """Chờ proxy sẵn sàng"""
        start = time.time()
        while time.time() - start < timeout:
            try:
                data = self.test()
                if data.get("ok"):
                    return True
            except:
                pass
            time.sleep(2)
        return False

    # ========================
    # SMART POOL MODE
    # ========================

    def session(self, client_id=None):
        """Đăng ký session với smart pool
        Pool tự chọn phone tốt nhất, cân bằng tải

        Usage:
            p = Proxy4G()
            s = p.session("my-scraper")
            proxies = {"http": s["proxy"], "https": s["proxy"]}
            requests.get(url, proxies=proxies)
        """
        cid = client_id or f"auto-{id(self)}"
        return self._api(f"/pool/session/{cid}")

    def new_ip(self, client_id=None):
        """Yêu cầu IP mới qua smart pool
        Pool tự xử lý: cooldown, queue, chuyển phone nếu cần

        Returns:
            status='rotating' → đang đổi, chờ chút
            status='switched' → đã chuyển sang phone khác có IP khác
            status='queued'   → tất cả phone bận, đang xếp hàng
        """
        cid = client_id or f"auto-{id(self)}"
        return self._api(f"/pool/new-ip/{cid}", method="POST")

    def pool_status(self):
        """Xem trạng thái pool: bao nhiêu phone, session, queue"""
        return self._api("/pool/stats")

    def pool_all(self):
        """Xem tất cả phone + trạng thái chi tiết"""
        return self._api("/pool/all")

    def smart_proxy(self):
        """Lấy 1 proxy ngay lập tức, pool tự chọn phone tốt nhất
        Không cần session, dùng cho request đơn lẻ
        """
        data = self._api("/pool/any")
        return data.get("proxy")

    # ========================
    # IP GUARD — BÁO CÁO ĐỂ GIỮ IP SẠCH
    # ========================

    def report_ok(self):
        """Báo request thành công (đếm usage)"""
        try: self._api("/guard/request", method="POST")
        except: pass

    def report_captcha(self):
        """Báo gặp CAPTCHA → IP sẽ bị cảnh báo/quarantine
        GỌI NGAY khi tool thấy CAPTCHA, reCAPTCHA, hCaptcha
        """
        try: return self._api("/guard/captcha", method="POST")
        except: return {}

    def report_blocked(self, error_code=429):
        """Báo bị block/rate limit → IP sẽ cooldown
        GỌI NGAY khi nhận HTTP 429, 403, hoặc bị redirect tới trang block
        """
        try: return self._api(f"/guard/error?code={error_code}", method="POST")
        except: return {}

    def ip_status(self):
        """Xem trạng thái tất cả IP: sạch/warming/cooldown/quarantine"""
        return self._api("/guard/status")

    def __repr__(self):
        return f"Proxy4G(device={self.device}, url={self.base_url})"


# ========================
# CHẠY TRỰC TIẾP ĐỂ TEST
# ========================
if __name__ == "__main__":
    p = Proxy4G()
    print("=== 4G Proxy Client Test ===")
    print(f"Proxies: {p.list()}")
    print(f"Proxy: {p.get()}")
    print(f"For requests: {p.for_requests()}")
    result = p.test()
    print(f"Test: IP={result.get('ip')} Latency={result.get('latency_ms')}ms")
    print(f"\nPool status: {p.pool_status()}")
