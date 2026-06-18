"""
ElevenLabs API Client - Ho tro 2 che do:

MODE A - Direct API (chi 21 default voices):
  xi-api-key -> api.elevenlabs.io

MODE B - Firebase Auth + Ferndocs Proxy (TAT CA voices):
  1. Login Firebase: email+password -> idToken
  2. Goi TTS qua proxy.ferndocs.com (forward den api.elevenlabs.io)
  3. Header: xi-api-key (tu idToken hoac API key)

DgtAutoTTS dung MODE B:
  - Firebase API Key: AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys
  - Login URL: identitytoolkit.googleapis.com/v1/accounts:signInWithPassword
  - Proxy URL: proxy.ferndocs.com/https://api.elevenlabs.io/...
  - Headers gia lap Chrome browser
"""

import requests
from typing import Optional

from utils.logger import log

AVAILABLE_MODELS = [
    "eleven_v3",                   # MỚI: 74 ngôn ngữ, expressive nhất
    "eleven_flash_v2_5",           # Nhanh, 32 ngôn ngữ
    "eleven_turbo_v2_5",           # Chất lượng cao + nhanh, 32 ngôn ngữ
    "eleven_multilingual_v2",      # Sống động nhất, 29 ngôn ngữ
    "eleven_turbo_v2",             # Chỉ tiếng Anh, nhanh
    "eleven_flash_v2",             # Chỉ tiếng Anh, siêu nhanh
    "eleven_multilingual_sts_v2",  # Speech-to-speech, 29 ngôn ngữ
]

OUTPUT_FORMATS = [
    "mp3_22050_32",
    "mp3_44100_32",
    "mp3_44100_64",
    "mp3_44100_96",
    "mp3_44100_128",
    "mp3_44100_192",
    "pcm_16000",
    "pcm_22050",
    "pcm_24000",
    "pcm_44100",
    "ulaw_8000",
]

# Firebase config (giong DgtAutoTTS)
FIREBASE_API_KEY = "AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys"
FIREBASE_LOGIN_URL = (
    f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
    f"?key={FIREBASE_API_KEY}"
)

# Ferndocs proxy (giong DgtAutoTTS)
FERNDOCS_PROXY_BASE = "https://proxy.ferndocs.com/https://api.elevenlabs.io"

# Pool fingerprints - moi account se duoc gan 1 fingerprint khac nhau
# Giong cach DgtAutoTTS dung nhieu Chrome version + language
_CHROME_VERSIONS = [
    ("130", "130.0.0.0"),
    ("131", "131.0.0.0"),
    ("132", "132.0.0.0"),
    ("133", "133.0.0.0"),
    ("134", "134.0.0.0"),
    ("135", "135.0.0.0"),
]

_ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.9,es-US;q=0.8,es;q=0.7",
    "en-US,en;q=0.9,vi;q=0.8",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "en-US,en;q=0.9,fr;q=0.8",
    "en-US,en;q=0.9,de;q=0.8",
    "en-US,en;q=0.9,ja;q=0.8",
    "en-US,en;q=0.9,ko;q=0.8",
    "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
]

_PLATFORMS = ['"Windows"', '"macOS"', '"Linux"']


def generate_fingerprint(account_id: int = 0) -> dict:
    """Tạo fingerprint unique cho mỗi account.

    Dùng account_id để đảm bảo cùng account luôn có cùng fingerprint
    (nhất quán, không đổi giữa các request).
    """
    import hashlib
    # Hash account_id để random nhưng nhất quán
    h = int(hashlib.md5(str(account_id).encode()).hexdigest(), 16)

    cv = _CHROME_VERSIONS[h % len(_CHROME_VERSIONS)]
    lang = _ACCEPT_LANGUAGES[h % len(_ACCEPT_LANGUAGES)]
    plat = _PLATFORMS[h % len(_PLATFORMS)]

    # CHINH XAC giong DgtAutoTTS - khong them khong bot
    return {
        "accept": "*/*",
        "accept-language": lang,
        "dnt": "1",
        "origin": "https://elevenlabs.io",
        "referer": "https://elevenlabs.io/",
        "sec-ch-ua": f'"Google Chrome";v="{cv[0]}", "Not-A.Brand";v="8", "Chromium";v="{cv[0]}"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": plat,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
        "user-agent": (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{cv[1]} Safari/537.36"
        ),
    }


# Default headers (cho khi không có account_id)
BROWSER_HEADERS = generate_fingerprint(0)


class ElevenLabsError(Exception):
    def __init__(self, message: str, status_code: int = None,
                 response_data: dict = None, flagged: bool = False,
                 quota: bool = False, disabled: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data
        self.flagged = flagged  # True = TK bi detected_unusual_activity
        self.quota = quota      # True = het quota/credit (can doi TK, KHONG phai token sai)
        self.disabled = disabled  # True = subscription bi vo hieu hoa/ban (TK chet)


def _proxy_key(proxy: Optional[dict]) -> tuple:
    if not proxy:
        return ()
    return (proxy.get("http", ""), proxy.get("https", ""))


def _firebase_proxy_candidates(proxy: Optional[dict]) -> list:
    candidates = []
    seen = set()

    def add(px: Optional[dict]):
        key = _proxy_key(px)
        if key in seen:
            return
        seen.add(key)
        candidates.append(px)

    add(proxy)
    try:
        from accounts.proxy import SOCKS5_HOST, SOCKS5_PORT, GATEWAY_DIRECT_DNS
        direct = {
            "http": f"socks5://{SOCKS5_HOST}:{SOCKS5_PORT}",
            "https": f"socks5://{SOCKS5_HOST}:{SOCKS5_PORT}",
        }
        gateway = {
            "http": GATEWAY_DIRECT_DNS,
            "https": GATEWAY_DIRECT_DNS,
        }
        add(direct)
        add(gateway)
    except Exception:
        pass
    return candidates


# ============================================================
# FIREBASE AUTH
# ============================================================

def firebase_login(email: str, password: str, proxy: dict = None) -> dict:
    """Login ElevenLabs qua Firebase Auth.

    Day la cach DgtAutoTTS login de truy cap voice thu vien.

    Args:
        email: Email tai khoan ElevenLabs
        password: Mat khau
        proxy: HTTP proxy dict

    Returns: {
        "idToken": str,      # Firebase ID token (dung lam auth)
        "email": str,
        "localId": str,      # Firebase user ID
        "refreshToken": str, # Token de refresh session
        "expiresIn": str,    # Thoi gian het han (giay)
    }

    Raises:
        ElevenLabsError: Login that bai
    """
    payload = {
        "email": email,
        "password": password,
        "returnSecureToken": True,
        "clientType": "CLIENT_TYPE_WEB",
    }

    headers = {
        "content-type": "application/json",
        "origin": "https://elevenlabs.io",
        "referer": "https://elevenlabs.io/",
        "user-agent": BROWSER_HEADERS["user-agent"],
        "sec-ch-ua": BROWSER_HEADERS["sec-ch-ua"],
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
        "accept-language": "en-US,en;q=0.9",
    }

    resp = None
    last_err = None
    candidates = _firebase_proxy_candidates(proxy)
    if proxy is not None:
        candidates.append(None)

    unique_candidates = []
    seen_candidates = set()
    for candidate in candidates:
        key = _proxy_key(candidate)
        if key not in seen_candidates:
            seen_candidates.add(key)
            unique_candidates.append(candidate)
    candidates = unique_candidates

    for idx, proxy_candidate in enumerate(candidates, start=1):
        try:
            resp = requests.post(
                FIREBASE_LOGIN_URL,
                json=payload,
                headers=headers,
                proxies=proxy_candidate,
                timeout=30,
            )
            break
        except requests.ConnectionError as e:
            last_err = ElevenLabsError(f"Khong ket noi duoc Firebase: {e}")
            if idx < len(candidates):
                log.warning(f"Firebase route {idx} fail, thu route khac")
            continue
        except requests.Timeout:
            last_err = ElevenLabsError("Firebase login timeout")
            if idx < len(candidates):
                log.warning(f"Firebase route {idx} timeout, thu route khac")
            continue

    if resp is None:
        raise last_err or ElevenLabsError("Khong ket noi duoc Firebase")

    if resp.status_code != 200:
        try:
            error = resp.json().get("error", {})
            msg = error.get("message", "Unknown error")
        except Exception:
            msg = resp.text
        raise ElevenLabsError(f"Firebase login that bai: {msg}", resp.status_code)

    data = resp.json()
    log.info(f"Firebase login OK: {email}")
    return data


def firebase_refresh(refresh_token: str, proxy: dict = None) -> dict:
    """Gia hạn token bằng refresh_token - KHÔNG cần email/password.

    Firebase token hết hạn sau 1 giờ. Dùng refresh_token để lấy
    idToken mới vô hạn lần (refresh_token không hết hạn).

    Args:
        refresh_token: refreshToken từ lần login trước
        proxy: HTTP proxy

    Returns: {
        "id_token": str,      # Token mới (1 giờ nữa)
        "refresh_token": str,  # Refresh token mới
        "expires_in": str,     # "3600"
    }
    """
    url = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"

    resp = requests.post(url, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "origin": "https://elevenlabs.io",
        "referer": "https://elevenlabs.io/",
        "user-agent": BROWSER_HEADERS["user-agent"],
    }, proxies=proxy, timeout=15)

    if resp.status_code != 200:
        try:
            msg = resp.json().get("error", {}).get("message", "Unknown")
        except Exception:
            msg = resp.text[:100]
        raise ElevenLabsError(f"Refresh token thất bại: {msg}", resp.status_code)

    data = resp.json()
    log.debug(f"Firebase refresh OK")
    return data


# ============================================================
# API CLIENT
# ============================================================

class ApiClient:
    """Client gọi ElevenLabs API.

    Flow đã test thành công:
    1. Proxy xoay (IP 4G sạch)
    2. Firebase login qua proxy → Bearer token
    3. Bearer + direct API + /with-timestamps
    4. sec-fetch-site: same-site
    → Library voice + v3 + tất cả models OK
    """

    # api.us.elevenlabs.io la endpoint chinh cua web app
    # api.elevenlabs.io cung hoat dong nhung co the bi redirect
    BASE_URL = "https://api.us.elevenlabs.io"

    def __init__(self, api_key: str = None, auth_token: str = None,
                 proxy: dict = None, account_id: int = 0):
        self.api_key = api_key
        self.auth_token = auth_token
        self.session = requests.Session()

        # Headers giống browser thật
        fp = generate_fingerprint(account_id)
        fp["sec-fetch-site"] = "same-site"
        self.session.headers.update(fp)
        self.session.headers["Content-Type"] = "application/json"

        if auth_token:
            self.session.headers["Authorization"] = f"Bearer {auth_token}"
        elif api_key:
            self.session.headers["xi-api-key"] = api_key

        self.base_url = self.BASE_URL
        self.mode = "bearer" if auth_token else "api_key"

        if proxy:
            self.session.proxies.update(proxy)

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        log.debug(f"API [{self.mode}] {method} {path}")
        timeout = kwargs.pop("timeout", 60)

        # Retry connection errors (proxy không ổn định)
        import time as _time
        last_err = None
        for attempt in range(3):
            try:
                # Mỗi retry tạo connection mới (tránh stale connection qua proxy)
                if attempt > 0:
                    self.session.close()
                    new_session = requests.Session()
                    new_session.headers.update(self.session.headers)
                    new_session.proxies.update(self.session.proxies)
                    self.session = new_session

                resp = self.session.request(method, url, timeout=timeout, **kwargs)
                return self._check_response(resp)
            except requests.ConnectionError as e:
                last_err = e
                log.warning(f"Connection error (lần {attempt+1}): {str(e)[:80]}")
                if attempt < 2:
                    _time.sleep(5 + attempt * 3)
                    continue
            except requests.Timeout:
                last_err = Exception("timeout")
                log.warning(f"Timeout (lần {attempt+1})")
                if attempt < 2:
                    _time.sleep(5)
                    continue

        raise ElevenLabsError(f"Connection error sau 3 lần: {last_err}")

    def _check_response(self, resp: requests.Response) -> requests.Response:

        if resp.status_code == 401:
            # Phan biet giua token het han va account bi flag
            try:
                data = resp.json()
                detail = data.get("detail", {})
                status = detail.get("status", "") if isinstance(detail, dict) else ""
                msg = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
            except Exception:
                status = ""
                msg = resp.text[:300]

            if status == "detected_unusual_activity":
                raise ElevenLabsError(
                    f"TK bi flag: {msg}", 401, flagged=True)
            # HET QUOTA tra ve 401 nhung KHONG phai token sai -> can doi TK
            _ql = (str(status) + " " + str(msg)).lower()
            if ("quota_exceeded" in _ql or "exceeds your quota" in _ql
                    or "exceeds quota" in _ql):
                raise ElevenLabsError(
                    f"Het quota: {msg}", 402, quota=True)
            # SUBSCRIPTION BI VO HIEU HOA (ban) -> TK chet, can bo qua + doi TK
            if ("subscription" in _ql and "disabled" in _ql) \
                    or "has been disabled" in _ql:
                raise ElevenLabsError(
                    f"Subscription disabled: {msg}", 401, disabled=True)
            raise ElevenLabsError(
                f"API key/token khong hop le: {msg}", 401)

        if resp.status_code == 429:
            raise ElevenLabsError("Rate limited", 429)
        if resp.status_code >= 400:
            try:
                data = resp.json()
                detail = data.get("detail", {})
                msg = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
            except Exception:
                msg = resp.text[:300]
            log.warning(f"API {resp.status_code}: {msg}")
            raise ElevenLabsError(f"API error {resp.status_code}: {msg}", resp.status_code)
        return resp

    # === USER INFO ===

    def get_user(self) -> dict:
        return self._request("GET", "/v1/user").json()

    def get_subscription(self) -> dict:
        return self._request("GET", "/v1/user/subscription").json()

    # === VOICES ===

    def get_voices(self) -> list[dict]:
        """Lay danh sach voices.
        Mode proxy: tra ve TAT CA voices (ke ca thu vien)
        Mode direct: chi 21 default voices
        """
        resp = self._request("GET", "/v1/voices")
        return resp.json().get("voices", [])

    def get_voice(self, voice_id: str) -> dict:
        resp = self._request("GET", f"/v1/voices/{voice_id}")
        return resp.json()

    def get_models(self) -> list[dict]:
        return self._request("GET", "/v1/models").json()

    # === VOICE LIBRARY (shared voices) ===

    def search_shared_voices(self, search: str = "", language: str = "",
                              gender: str = "", page_size: int = 25,
                              page: int = 0, category: str = "") -> dict:
        """Tim kiem giong trong thu vien ElevenLabs (hang ngan giong).

        Args:
            search: Tu khoa tim kiem
            language: en, vi, ja, ko, ...
            gender: male, female
            page_size: So ket qua / trang (max 100)
            page: Trang (bat dau tu 0)
            category: professional, famous, high_quality

        Returns: {
            "voices": [...],
            "has_more": bool,
            "last_sort_id": str
        }
        """
        params = {"page_size": page_size, "page": page}
        if search:
            params["search"] = search
        if language:
            params["language"] = language
        if gender:
            params["gender"] = gender
        if category:
            params["category"] = category

        resp = self._request("GET", "/v1/shared-voices", params=params)
        return resp.json()

    def add_shared_voice(self, public_user_id: str, voice_id: str,
                          new_name: str) -> dict:
        """Them giong thu vien vao danh sach ca nhan.

        Args:
            public_user_id: Owner ID cua voice
            voice_id: Voice ID
            new_name: Ten moi cho voice

        Returns: voice_id moi (trong account cua minh)
        """
        resp = self._request("POST",
            f"/v1/voices/add/{public_user_id}/{voice_id}",
            json={"new_name": new_name})
        return resp.json()

    # === TEXT TO SPEECH ===

    def text_to_speech(
        self,
        voice_id: str,
        text: str,
        model_id: str = "eleven_flash_v2_5",
        stability: float = 0.5,
        similarity_boost: float = 0.75,
        output_format: str = "mp3_44100_128",
        language_code: str = None,
    ) -> bytes:
        """Convert text → audio. Dùng /with-timestamps endpoint.

        Endpoint này:
        - Trả JSON {audio_base64: "...", alignment: {...}}
        - Hoạt động với TẤT CẢ voices (library + default)
        - Hoạt động với TẤT CẢ models (v3, flash, multilingual)
        - Cần Bearer token (từ Firebase login)
        """
        import base64

        payload = {
            "text": text,
            "model_id": model_id,
            "voice_settings": {
                "stability": stability,
                "similarity_boost": similarity_boost,
            }
        }
        if language_code:
            payload["language_code"] = language_code

        # /with-timestamps trả JSON có audio_base64
        # Dùng requests.post() trực tiếp (không session) - tránh stale connection qua proxy
        import time as _time
        url = f"{self.base_url}/v1/text-to-speech/{voice_id}/with-timestamps?output_format={output_format}"
        headers = dict(self.session.headers)
        proxies = dict(self.session.proxies) if self.session.proxies else None

        resp = None
        for attempt in range(3):
            try:
                resp = requests.post(url, json=payload, headers=headers,
                                    proxies=proxies, timeout=120)
                break
            except (requests.ConnectionError, requests.Timeout) as e:
                log.warning(f"TTS connection error (lần {attempt+1}): {str(e)[:60]}")
                if attempt < 2:
                    _time.sleep(5 + attempt * 3)
                else:
                    raise ElevenLabsError(f"TTS connection error sau 3 lần: {e}")

        resp = self._check_response(resp)

        # Decode audio từ base64
        try:
            data = resp.json()
            audio_b64 = data.get("audio_base64", "")
            if audio_b64:
                audio = base64.b64decode(audio_b64)
                log.info(f"TTS: {len(text)} chars → {len(audio):,} bytes")
                return audio
        except (ValueError, KeyError):
            pass

        # Fallback: response có thể là raw binary
        if len(resp.content) > 100:
            log.info(f"TTS (raw): {len(text)} chars → {len(resp.content):,} bytes")
            return resp.content

        raise ElevenLabsError("TTS: không nhận được audio data")

    # === HELPERS ===

    def check_key(self) -> dict:
        """Kiem tra API key/token.
        Mot so key chi co quyen TTS, khong co quyen user_read.
        Trong truong hop do van coi la valid.
        """
        try:
            sub = self.get_subscription()
            used = sub.get("character_count", 0)
            limit = sub.get("character_limit", 0)
            return {
                "valid": True,
                "tier": sub.get("tier", "unknown"),
                "chars_used": used,
                "chars_limit": limit,
                "chars_remaining": limit - used,
            }
        except ElevenLabsError as e:
            if e.status_code == 401:
                # Co the key chi co quyen TTS, khong co user_read
                # Thu goi voices de kiem tra
                try:
                    voices = self.get_voices()
                    return {
                        "valid": True,
                        "tier": "restricted",
                        "chars_used": 0,
                        "chars_limit": 0,
                        "chars_remaining": 0,
                        "note": "Key chi co quyen TTS, khong doc duoc subscription",
                    }
                except Exception:
                    pass
                return {"valid": False, "error": "Key/token khong hop le"}
            return {"valid": False, "error": str(e)}
