"""
anonymous_tts.py — MODE C: Tao voice KHONG DANG NHAP (endpoint /anonymous cua elevenlabs.io).

Nguyen ly (da kiem chung bang request that + doi chieu tool 'Pro Lifetime'):
  POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream/with-timestamps/anonymous
    headers: chi gia lap browser (origin/referer elevenlabs.io) — KHONG auth/api-key.
    body: {text, model_id, voice_settings, hcaptcha_token, language_code}
    resp: {"audio_base64": "..."} (co the nhieu dong stream) -> decode = MP3.

  - voice_id: TUY Y (giong rieng cua ban) — da xac nhan server chap nhan.
  - model_id: eleven_v3 chay duoc (khong can Creator tier).
  - Rao can DUY NHAT: IP. IP bi dot -> 'detected_unusual_activity'. => CHROME SACH + IP SACH.

Cai kho: hcaptcha_token (dung 1 lan) do trang demo sinh khi bam Play.
  -> Dung Playwright: mo trang, bom text, bam Play, INTERCEPT request /anonymous,
     ABORT no (token CHUA bi tieu), lay token -> gui lai tu Python voi voice_id CUA MINH.
  (DrissionPage 4.1 khong abort duoc request truoc khi gui -> token bi widget tieu ->
   phai dung Playwright cho khau lay token. Playwright + chromium da co san.)

Luong 1 file:
  session = AnonymousSession(proxy=..., headless=False)   # 1 chrome sach
  session.open()
  for chunk in chunks:
      token = session.mint_token()          # bam Play -> abort -> token tuoi
      audio = send_anonymous(voice_id, token, chunk, model, lang, proxy)
      ...
  session.close()

Moi 'mint_token' = 1 token = 1 chunk (token one-shot). IP sach cho phep 1 luong token/IP
truoc khi bi flag -> khi flag thi xoay 4G (IP moi) + mo chrome moi.
"""
import os
import sys
import json
import time
import base64
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

try:
    from utils.logger import log
except Exception:
    class _L:
        def info(self, *a): print(*a)
        def warning(self, *a): print(*a)
        def error(self, *a): print(*a)
    log = _L()


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

ANON_URL = ("https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            "/stream/with-timestamps/anonymous")


class AnonUnusualActivity(Exception):
    """IP bi flag (detected_unusual_activity) -> can xoay IP + chrome moi."""


class AnonTokenError(Exception):
    """Khong lay duoc hcaptcha_token."""


class AnonIPExhausted(Exception):
    """IP het luot free (sign_in_required/429) -> phai XOAY IP 4G (token van OK)."""


# ============================================================
# GUI REQUEST (Python, qua proxy) — khau tao audio
# ============================================================
def _anon_headers():
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://elevenlabs.io",
        "referer": "https://elevenlabs.io/",
        "user-agent": UA,
        "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "priority": "u=1, i",
    }


def _decode_audio(txt: str) -> bytes:
    """Response stream with-timestamps: nhieu dong JSON, moi dong 1 audio_base64."""
    audio = b""
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            b64 = json.loads(line).get("audio_base64") or ""
        except Exception:
            b64 = ""
        if b64:
            audio += base64.b64decode(b64)
    if not audio:
        try:
            audio = base64.b64decode(json.loads(txt).get("audio_base64") or "")
        except Exception:
            pass
    return audio


def send_anonymous(voice_id: str, hcaptcha_token: str, text: str,
                   model_id: str = "eleven_v3", language_code: str = "vi",
                   voice_settings: dict = None, proxy: dict = None,
                   timeout: int = 180) -> bytes:
    """Gui request anonymous voi voice_id CUA MINH + token vua bat. -> MP3 bytes.

    proxy: {"http": "...", "https": "..."} (4G) hoac None.
    Raise AnonUnusualActivity neu IP bi flag; Exception khac neu loi khac.
    """
    url = ANON_URL.format(voice_id=voice_id)
    body = {
        "text": text,
        "model_id": model_id,
        "voice_settings": voice_settings or {"speed": 1},
        "hcaptcha_token": hcaptcha_token,
        "language_code": language_code,
    }
    r = requests.post(url, headers=_anon_headers(), json=body,
                      proxies=proxy, timeout=timeout)
    if r.status_code == 200:
        audio = _decode_audio(r.text)
        if audio:
            return audio
        raise Exception(f"200 nhung khong decode duoc audio: {r.text[:200]}")
    # Phan loai loi
    low = r.text.lower()
    if "detected_unusual_activity" in low or "unusual activity" in low:
        raise AnonUnusualActivity(r.text[:200])
    # sign_in_required / 401 auth / 429: IP da het luot free -> phai XOAY IP (khong retry token).
    # (Da do: ~9-10 request/IP roi server tra sign_in_required.)
    if ("sign_in_required" in low or "sign in" in low
            or r.status_code == 429
            or (r.status_code == 401 and "authentication_error" in low)):
        raise AnonIPExhausted(f"IP het luot (HTTP {r.status_code}): {r.text[:150]}")
    if r.status_code == 401:
        raise Exception(f"401 (token het han/da tieu): {r.text[:200]}")
    raise Exception(f"HTTP {r.status_code}: {r.text[:250]}")


# ============================================================
# BROWSER SESSION (Playwright) — khau lay hcaptcha_token
# ============================================================
class AnonymousSession:
    """1 phien Chrome SACH de mint hcaptcha_token tu trang demo.

    Moi lan mint_token(): bom text moi -> bam Play -> intercept + ABORT request
    /anonymous cua widget -> lay token tuoi (chua bi tieu).
    """

    def __init__(self, proxy_server: str = None, headless: bool = True,
                 engine: str = "auto"):
        """proxy_server: 'socks5://127.0.0.1:10001' hoac 'http://ip:port' (4G). None = IP may.

        engine: 'camoufox' (antidetect, fingerprint moi + humanize — giong Gemlogin),
                'playwright' (chromium thuong), 'auto' (uu tien camoufox neu da fetch).
        """
        self.proxy_server = proxy_server
        self.headless = headless
        self.engine = engine
        self._pw = None          # camoufox/playwright context manager
        self._browser = None
        self._ctx = None
        self._page = None
        self._captured = {"token": None, "widget_voice": None}
        self._counter = 0
        self._active_engine = None

    @staticmethod
    def _camoufox_ready() -> bool:
        """Camoufox da cai + fetch browser chua?"""
        try:
            import camoufox  # noqa
            from camoufox.pkgman import installed_path  # ton tai khi da fetch
            return True
        except Exception:
            try:
                import camoufox  # noqa
                # thu cach khac: check thu muc cache
                import os as _os
                base = _os.path.join(_os.environ.get("LOCALAPPDATA", ""), "camoufox")
                return _os.path.isdir(base)
            except Exception:
                return False

    def _install_route(self):
        """Gan intercept + abort request /anonymous de bat token (chung cho 2 engine)."""
        def handle_route(route):
            req = route.request
            if ("text-to-speech" in req.url and "anonymous" in req.url):
                try:
                    d = json.loads(req.post_data)
                    self._captured["token"] = d.get("hcaptcha_token")
                    self._captured["widget_voice"] = \
                        req.url.split("/text-to-speech/")[1].split("/")[0]
                except Exception:
                    pass
                # ABORT -> token chua bi tieu -> ta gui lai tu Python voi voice_id cua minh
                route.abort()
                return
            route.continue_()
        self._page.route("**/text-to-speech/**", handle_route)

    def open(self):
        use_camoufox = (self.engine == "camoufox" or
                        (self.engine == "auto" and self._camoufox_ready()))
        if use_camoufox:
            try:
                self._open_camoufox()
                self._active_engine = "camoufox"
                return self
            except Exception as e:
                log.warning(f"[Anon] Camoufox loi ({str(e)[:80]}), fallback Playwright")
        self._open_playwright()
        self._active_engine = "playwright"
        return self

    def _open_camoufox(self):
        """Camoufox: fingerprint MOI moi launch + humanize (chuot nguoi) + geoip theo IP."""
        from camoufox.sync_api import Camoufox
        kwargs = dict(
            headless=self.headless,
            humanize=True,        # di chuot nguoi that -> qua ai bam Play cua hCaptcha
            os=["windows"],       # ep fingerprint Windows
            # KHONG set fingerprint -> BrowserForge tu sinh MOI moi launch
        )
        if self.proxy_server:
            kwargs["proxy"] = {"server": self.proxy_server}
            kwargs["geoip"] = True   # tu suy timezone/locale tu IP proxy (tranh mismatch)
        self._pw = Camoufox(**kwargs)
        self._browser = self._pw.__enter__()   # tra ve Browser
        self._page = self._browser.new_page()
        # Timeout CUNG cho navigation (24/7: tranh goto treo vinh vien)
        try:
            self._page.set_default_navigation_timeout(60000)
            self._page.set_default_timeout(60000)
        except Exception:
            pass
        self._install_route()
        self._page.goto("https://elevenlabs.io/", wait_until="domcontentloaded")
        self._page.wait_for_timeout(5000)

    def _open_playwright(self):
        """Playwright chromium thuong (fallback khi chua fetch camoufox)."""
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        launch_args = ["--no-first-run", "--no-default-browser-check",
                       "--disable-blink-features=AutomationControlled"]
        launch_kwargs = {"headless": self.headless, "args": launch_args}
        if self.proxy_server:
            launch_kwargs["proxy"] = {"server": self.proxy_server}
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        self._ctx = self._browser.new_context(user_agent=UA, locale="en-US")
        self._page = self._ctx.new_page()
        try:
            self._page.set_default_navigation_timeout(60000)
            self._page.set_default_timeout(60000)
        except Exception:
            pass
        self._install_route()
        self._page.goto("https://elevenlabs.io/", wait_until="domcontentloaded")
        self._page.wait_for_timeout(5000)

    def mint_token(self, timeout_ms: int = 30000) -> str:
        """Bam Play voi text moi -> bat 1 hcaptcha_token tuoi. -> token str."""
        if not self._page:
            raise AnonTokenError("session chua open()")
        self._captured["token"] = None
        self._counter += 1
        # Text moi de nut Play luon active + request moi
        ta = self._page.query_selector("textarea")
        if ta:
            ta.click()
            ta.fill(f"Warm up sentence number {self._counter} at {int(time.time())}.")
        self._page.wait_for_timeout(400)
        # Bam Play
        for b in self._page.query_selector_all("button"):
            try:
                if (b.inner_text() or "").strip().lower() == "play":
                    b.click()
                    break
            except Exception:
                pass
        # Cho intercept
        waited = 0
        while waited < timeout_ms:
            if self._captured["token"]:
                return self._captured["token"]
            self._page.wait_for_timeout(300)
            waited += 300
        raise AnonTokenError("khong bat duoc token (widget chan/captcha hien?)")

    def close(self):
        if self._active_engine == "camoufox":
            # Camoufox la context manager -> __exit__ dong browser
            try:
                if self._pw:
                    self._pw.__exit__(None, None, None)
            except Exception:
                pass
            return
        # Playwright
        for fn in (lambda: self._ctx and self._ctx.close(),
                   lambda: self._browser and self._browser.close(),
                   lambda: self._pw and self._pw.stop()):
            try:
                fn()
            except Exception:
                pass


# ============================================================
# HIGH-LEVEL: tao voice cho 1 doan text (nhieu chunk)
# ============================================================
def generate_anonymous(text_chunks: list, voice_id: str,
                       model_id: str = "eleven_v3", language_code: str = "vi",
                       proxy_server: str = None, proxy_requests: dict = None,
                       headless: bool = True, on_log=lambda *_: None) -> list:
    """Tao audio cho tung chunk qua duong anonymous. -> list[bytes] (audio moi chunk).

    proxy_server: cho Playwright (browser). proxy_requests: cho requests (Python gui).
    Ca 2 nen cung 1 IP 4G. Neu None -> dung IP may (chi hop de test IP sach).
    Raise AnonUnusualActivity neu IP bi flag giua chung -> caller xoay IP + goi lai.
    """
    session = AnonymousSession(proxy_server=proxy_server, headless=headless)
    session.open()
    out = []
    try:
        for i, chunk in enumerate(text_chunks):
            on_log(f"[Anon] chunk {i+1}/{len(text_chunks)}: mint token...")
            token = session.mint_token()
            on_log(f"[Anon] chunk {i+1}: gui voice_id={voice_id[:10]}... ({len(chunk)} chars)")
            audio = send_anonymous(voice_id, token, chunk, model_id,
                                   language_code, proxy=proxy_requests)
            out.append(audio)
            on_log(f"[Anon] chunk {i+1} OK ({len(audio):,} bytes)")
    finally:
        session.close()
    return out


# ============================================================
# TOP-LEVEL: tu dong XOAY 4G khi bi flag (dung cho tool 24/7)
# ============================================================
def generate_anonymous_4g(text_chunks: list, voice_id: str,
                          model_id: str = "eleven_v3", language_code: str = "vi",
                          headless: bool = True, max_ip_rotations: int = 5,
                          on_log=lambda *_: None) -> list:
    """Tao audio qua anonymous + TU XOAY IP 4G khi gap detected_unusual_activity.

    Moi khi bi flag: rotate 4G (IP moi) + mo Chrome SACH moi (fingerprint moi neu Camoufox)
    -> lam tiep tu chunk con do. Chrome moi + IP moi = quota token free moi.
    -> list[bytes] audio moi chunk. Raise neu het luot xoay ma van fail.
    """
    from accounts.proxy import Proxy4G
    p4g = Proxy4G()

    out = []
    idx = 0                 # chunk dang lam
    rotations = 0
    proxy_server = p4g.get_for_chrome()          # socks5://127.0.0.1:10001
    proxy_requests = p4g.get_for_requests()

    while idx < len(text_chunks):
        session = AnonymousSession(proxy_server=proxy_server, headless=headless)
        try:
            session.open()
        except Exception as e:
            on_log(f"[Anon4G] mo Chrome loi: {str(e)[:80]}")
            session.close()
            raise
        try:
            while idx < len(text_chunks):
                chunk = text_chunks[idx]
                on_log(f"[Anon4G] chunk {idx+1}/{len(text_chunks)}: mint token...")
                token = session.mint_token()
                audio = send_anonymous(voice_id, token, chunk, model_id,
                                       language_code, proxy=proxy_requests)
                out.append(audio)
                on_log(f"[Anon4G] chunk {idx+1} OK ({len(audio):,} bytes)")
                idx += 1
        except AnonUnusualActivity:
            session.close()
            rotations += 1
            if rotations > max_ip_rotations:
                raise Exception(f"[Anon4G] da xoay IP {max_ip_rotations} lan van bi flag")
            on_log(f"[Anon4G] IP bi flag -> xoay 4G (lan {rotations})...")
            p4g.rotate(wait=20)
            proxy_server = p4g.get_for_chrome()
            proxy_requests = p4g.get_for_requests()
            new_ip = p4g.get_ip()
            on_log(f"[Anon4G] IP moi: {new_ip} -> mo Chrome sach, lam tiep chunk {idx+1}")
            continue
        except Exception:
            session.close()
            raise
        else:
            session.close()
            break
    return out


# ============================================================
# FILE-LEVEL: 1 file txt -> 1 mp3 (chia 1000 + ghep ffmpeg) — dung cho VoiceWorker
# ============================================================
ANON_MAX_CHARS = 1000   # gioi han cung cua endpoint anonymous (da do)


def generate_mode_c_file(txt_path: str, voice_id: str, output_dir: str,
                         model_id: str = "eleven_v3", language_code: str = "vi",
                         use_4g: bool = True, headless: bool = True,
                         on_log=lambda *_: None) -> str:
    """1 file .txt -> 1 file .mp3 qua Mode C (anonymous, khong dang nhap).

    - Chia text theo ranh gioi tu nhien, moi chunk <= 1000 ky tu (split_text).
    - Moi chunk: mint hcaptcha_token qua Camoufox (fingerprint sach) + gui qua 4G.
    - Ghep chunks bang ffmpeg (audio_merger da fix) -> chuan hoa -14 LUFS.
    - use_4g=True: tu xoay IP khi flag. False: dung IP may (chi de test).

    -> duong dan mp3. Raise neu that bai.
    """
    from core.text_splitter import clean_text, split_text
    from core.audio_merger import merge_audio_bytes

    with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
        raw = f.read()
    text = clean_text(raw)
    if not text.strip():
        raise Exception(f"File rong: {txt_path}")

    chunks = split_text(text, max_chars=ANON_MAX_CHARS)
    base = os.path.splitext(os.path.basename(txt_path))[0]
    on_log(f"[ModeC] {base}: {len(text):,} chars -> {len(chunks)} chunk (<=1000)")

    if use_4g:
        parts = generate_anonymous_4g(chunks, voice_id, model_id, language_code,
                                      headless=headless, on_log=on_log)
    else:
        parts = generate_anonymous(chunks, voice_id, model_id, language_code,
                                   headless=headless, on_log=on_log)

    if len(parts) != len(chunks) or not all(parts):
        raise Exception(f"[ModeC] thieu chunk: {len(parts)}/{len(chunks)}")

    os.makedirs(output_dir, exist_ok=True)
    mp3_path = os.path.join(output_dir, f"{base}.mp3")
    if len(parts) == 1:
        with open(mp3_path, "wb") as f:
            f.write(parts[0])
    else:
        merge_audio_bytes(parts, mp3_path, silence_between_ms=500)
    on_log(f"[ModeC] XONG {mp3_path} ({os.path.getsize(mp3_path):,} bytes)")
    return mp3_path


if __name__ == "__main__":
    # Smoke test (IP may — se bao unusual neu IP da dot; dung 4G de test that)
    chunks = ["Xin chao, day la thu nghiem mode anonymous voi voice id rieng cua toi."]
    vid = "452WrNT9o8dphaYW5YGU"
    try:
        parts = generate_anonymous(chunks, vid, headless=False,
                                   on_log=lambda m: print(m))
        total = os.path.join(os.path.dirname(__file__), "..", "_extract", "mode_c_out.mp3")
        with open(total, "wb") as f:
            for p in parts:
                f.write(p)
        print("THANH CONG:", total, sum(len(p) for p in parts), "bytes")
    except AnonUnusualActivity as e:
        print("IP DA DOT (can 4G/IP sach):", str(e)[:150])
    except Exception as e:
        print("Loi:", str(e)[:200])
