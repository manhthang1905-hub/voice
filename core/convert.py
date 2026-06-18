"""
Skill: convert — Chuyển text thành voice (MP3).

Cho file .txt + token → gọi API ElevenLabs → ra file .mp3.
Text dài → chia chunks → convert từng phần → ghép lại.

Cách dùng:
    conv = Convert()
    ok = conv.convert_file("bai1.txt", voice_id="...", token="eyJ...",
                           output_dir="output/", proxy={"http":"..."})
"""

import os
import time
import random
import base64
import uuid
import requests
from typing import Optional

from utils.logger import log


LANGUAGE_NAMES = {
    "en": "English",
    "vi": "Vietnamese",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "id": "Indonesian",
    "hi": "Hindi",
    "ar": "Arabic",
    "tr": "Turkish",
    "pl": "Polish",
    "ru": "Russian",
    "nl": "Dutch",
    "th": "Thai",
    "sv": "Swedish",
}


class QuotaExceededError(Exception):
    """Tài khoản hết quota (credit)."""
    pass


class IPFlaggedError(Exception):
    """IP bị flag (unusual_activity) — cần xoay IP."""
    pass


class PlanUpgradeError(Exception):
    """Account plan không đủ để dùng model này."""
    pass


class VoiceNotFoundError(Exception):
    """Voice ID không tồn tại trên account này."""
    pass


# Danh sách fallback models theo thứ tự ưu tiên (cao → thấp)
# eleven_v3: Creator+, eleven_multilingual_v2: Starter+, turbo/flash: Free
MODEL_FALLBACK_CHAIN = [
    "eleven_v3",
    "eleven_multilingual_v2",
    "eleven_turbo_v2_5",
    "eleven_flash_v2_5",
]


API_BASE = "https://api.us.elevenlabs.io"
FERNDOCS_TTS_BASE = "https://proxy.ferndocs.com/https://api.elevenlabs.io/v1/text-to-speech"


class QuotaExceededError(Exception):
    """Tài khoản hết credit."""
    pass


class IPFlaggedError(Exception):
    """IP bị flag / unusual_activity."""
    pass


class VoiceNotFoundError(Exception):
    """Voice ID không tồn tại trên account này (404)."""
    pass


class VoiceRestrictedError(Exception):
    """Voice yêu cầu plan cao hơn (Creator+) — không dùng được với account hiện tại."""
    pass


def check_quota(token: str, proxy: dict = None) -> dict:
    """Query ElevenLabs API trực tiếp để lấy quota thật.

    Returns: {
        "chars_used": int,
        "chars_limit": int,
        "chars_remaining": int,
        "tier": str,
    } hoặc None nếu lỗi.
    """
    try:
        if token.startswith("sk_"):
            headers = {'xi-api-key': token, 'accept': '*/*'}
        else:
            headers = {
                'Authorization': f'Bearer {token}',
                'accept': '*/*',
                'origin': 'https://elevenlabs.io',
                'referer': 'https://elevenlabs.io/',
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36',
            }
        resp = _request_with_proxy_failover(
            "GET",
            f"{API_BASE}/v1/user/subscription",
            proxy=proxy,
            headers=headers,
            timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            used = data.get("character_count", 0)
            limit = data.get("character_limit", 10000)
            return {
                "chars_used": used,
                "chars_limit": limit,
                "chars_remaining": limit - used,
                "tier": data.get("tier", "free"),
                "next_reset_unix": data.get(
                    "next_character_count_reset_unix", 0),
            }
    except Exception as e:
        log.warning(f"check_quota error: {e}")
    return None


def _make_headers(token: str) -> dict:
    """Tạo headers chuẩn cho API ElevenLabs."""
    if token.startswith("sk_"):
        return {'xi-api-key': token, 'accept': '*/*',
                'content-type': 'application/json'}
    return {
        'Authorization': f'Bearer {token}',
        'accept': '*/*',
        'content-type': 'application/json',
        'origin': 'https://elevenlabs.io',
        'referer': 'https://elevenlabs.io/',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }


def get_account_voices(token: str, proxy: dict = None) -> dict:
    """Lấy danh sách voices trong library của account.

    Returns: {voice_id: voice_name} hoặc {} nếu lỗi.
    """
    try:
        resp = _request_with_proxy_failover(
            "GET",
            f"{API_BASE}/v1/voices",
            proxy=proxy,
            headers=_make_headers(token),
            timeout=10)
        if resp.status_code == 200:
            return {v["voice_id"]: v.get("name", "")
                    for v in resp.json().get("voices", [])}
    except Exception as e:
        log.warning(f"get_account_voices error: {e}")
    return {}


def cleanup_library_voices(token: str, keep_voice_id: str = "",
                           proxy: dict = None, max_delete: int = 20) -> int:
    """Xóa voice library/share cũ để giải phóng slot account."""
    try:
        resp = _request_with_proxy_failover(
            "GET",
            f"{API_BASE}/v1/voices",
            proxy=proxy,
            headers=_make_headers(token),
            timeout=10)
        if resp.status_code != 200:
            log.warning(f"cleanup_library_voices: list FAIL {resp.status_code}")
            return 0
        voices = resp.json().get("voices", []) or []
    except Exception as e:
        log.warning(f"cleanup_library_voices list error: {e}")
        return 0

    keep = (keep_voice_id or "").strip()
    deleted = 0
    for voice in voices:
        if deleted >= max_delete:
            break
        vid = (voice.get("voice_id") or "").strip()
        if not vid or vid == keep:
            continue
        category = str(voice.get("category") or "").lower()
        sharing = voice.get("sharing") or {}
        is_library = (
            category in ("cloned", "professional", "generated", "shared")
            or bool(sharing)
            or str(voice.get("name") or "").startswith("lib_")
        )
        if not is_library:
            continue
        try:
            if remove_voice_from_account(token, vid, proxy=proxy):
                deleted += 1
        except Exception:
            pass
    if deleted:
        log.info(f"cleanup_library_voices: removed {deleted} voice(s)")
    return deleted


def get_voice_info(token: str, voice_id: str, proxy: dict = None) -> dict:
    """Lấy metadata của một Voice ID."""
    try:
        resp = _request_with_proxy_failover(
            "GET",
            f"{API_BASE}/v1/voices/{voice_id}",
            proxy=proxy,
            headers=_make_headers(token),
            timeout=15)
        if resp.status_code == 200:
            return resp.json()
        log.warning(f"get_voice_info: HTTP {resp.status_code} for {voice_id}")
    except Exception as e:
        log.warning(f"get_voice_info error: {e}")

    try:
        resp = _request_with_proxy_failover(
            "GET",
            f"{API_BASE}/v1/shared-voices",
            proxy=proxy,
            headers=_make_headers(token),
            params={"search": voice_id, "page_size": 10},
            timeout=15)
        if resp.status_code == 200:
            for voice in resp.json().get("voices", []) or []:
                if (voice.get("voice_id") or "").strip() == voice_id:
                    return voice
        log.warning(f"get_voice_info shared: HTTP {resp.status_code} for {voice_id}")
    except Exception as e:
        log.warning(f"get_voice_info shared error: {e}")
    return {}


def _normalize_language_code(value) -> str:
    if not value:
        return ""
    text = str(value).strip().lower().replace("_", "-")
    if not text:
        return ""
    if text in LANGUAGE_NAMES:
        return text
    base = text.split("-", 1)[0]
    if base in LANGUAGE_NAMES:
        return base
    for code, name in LANGUAGE_NAMES.items():
        if text == name.lower():
            return code
    return ""


def _add_language(languages: list, seen: set, value) -> None:
    code = _normalize_language_code(value)
    if code and code not in seen:
        seen.add(code)
        languages.append({"code": code, "name": LANGUAGE_NAMES.get(code, code)})


def extract_supported_languages(voice_info: dict) -> list:
    """Trích danh sách language từ metadata voice nếu API có trả."""
    languages = []
    seen = set()
    if not isinstance(voice_info, dict):
        return languages

    for item in voice_info.get("verified_languages") or []:
        if isinstance(item, dict):
            _add_language(languages, seen,
                          item.get("language") or item.get("language_code"))
        else:
            _add_language(languages, seen, item)

    labels = voice_info.get("labels") or {}
    if isinstance(labels, dict):
        for key in ("language", "language_code"):
            _add_language(languages, seen, labels.get(key))

    fine_tuning = voice_info.get("fine_tuning") or {}
    if isinstance(fine_tuning, dict):
        for key in ("language", "language_code"):
            _add_language(languages, seen, fine_tuning.get(key))

    for key in ("language", "language_code"):
        _add_language(languages, seen, voice_info.get(key))

    return languages


def _get_voice_public_owner_id(token: str, voice_id: str,
                                proxy: dict = None) -> str:
    """Tìm public_owner_id của voice (cần để add vào account).

    Thử 2 cách: voice info → shared-voices search.
    Returns: owner_id string hoặc None.
    """
    last_error = None
    # Cách 1: GET /v1/voices/{voice_id} → sharing.public_owner_id
    try:
        resp = _request_with_proxy_failover(
            "GET",
            f"{API_BASE}/v1/voices/{voice_id}",
            proxy=proxy,
            headers=_make_headers(token),
            timeout=10)
        if resp.status_code == 200:
            owner = (resp.json().get("sharing") or {}).get("public_owner_id")
            if owner:
                log.info(f"voice_owner: {voice_id[:16]} → {owner[:16]}... (voice info)")
                return owner
    except Exception as exc:
        last_error = exc

    # Cách 2: GET /v1/shared-voices?search={voice_id}
    try:
        resp = _request_with_proxy_failover(
            "GET",
            f"{API_BASE}/v1/shared-voices",
            proxy=proxy,
            headers=_make_headers(token),
            params={"search": voice_id, "page_size": 10},
            timeout=10)
        if resp.status_code == 200:
            for v in resp.json().get("voices", []):
                if v.get("voice_id") == voice_id:
                    owner = v.get("public_owner_id")
                    if owner:
                        log.info(f"voice_owner: {voice_id[:16]} → {owner[:16]}... (shared-voices)")
                        return owner
    except Exception as exc:
        last_error = exc

    if last_error:
        raise ConnectionError(f"Không tìm được owner do lỗi kết nối: {last_error}")
    return None


def add_voice_to_account(token: str, voice_id: str,
                          proxy: dict = None) -> str:
    """Add 1 shared/library voice vào account.

    Endpoint đúng: POST /v1/voices/add/{public_owner_id}/{voice_id}
    Returns: new_voice_id được assign cho account này, hoặc None nếu lỗi.
    """
    # Bước 1: Tìm public_owner_id
    public_owner_id = _get_voice_public_owner_id(token, voice_id, proxy)
    if not public_owner_id:
        log.warning(f"add_voice: không tìm được owner cho {voice_id} "
                    f"(voice private/clone — không share được)")
        return None

    # Bước 2: Add voice với đúng endpoint
    try:
        new_name = f"lib_{voice_id[:12]}"
        resp = _request_with_proxy_failover(
            "POST",
            f"{API_BASE}/v1/voices/add/{public_owner_id}/{voice_id}",
            proxy=proxy,
            headers=_make_headers(token),
            json={"new_name": new_name},
            timeout=15)
        if resp.status_code == 200:
            new_id = resp.json().get("voice_id", voice_id)
            log.info(f"add_voice OK: {voice_id[:16]} → new_id={new_id[:16]}")
            return new_id
        else:
            msg = ''
            try:
                d = resp.json().get('detail', {})
                msg = d.get('message', str(d)) if isinstance(d, dict) else str(d)
            except Exception:
                msg = resp.text[:120]
            log.warning(f"add_voice: FAIL {resp.status_code} — {msg}")
    except Exception as e:
        log.warning(f"add_voice error: {e}")
    return None


def remove_voice_from_account(token: str, voice_id: str,
                               proxy: dict = None) -> bool:
    """Xóa voice khỏi library của account (giải phóng slot).

    Returns: True nếu xóa thành công.
    """
    try:
        resp = _request_with_proxy_failover(
            "DELETE",
            f"{API_BASE}/v1/voices/{voice_id}",
            proxy=proxy,
            headers=_make_headers(token),
            timeout=10)
        if resp.status_code == 200:
            log.info(f"remove_voice: {voice_id} → đã xóa khỏi account")
            return True
        else:
            log.warning(
                f"remove_voice: FAIL {resp.status_code} | voice={voice_id}")
    except Exception as e:
        log.warning(f"remove_voice error: {e}")
    return False


def _build_proxy_candidates(proxy: dict = None) -> list:
    """Tạo danh sách route proxy để failover khi mạng chập chờn."""
    candidates = []
    seen = set()

    def _add(px: dict):
        if px is None:
            key = ("", "")
        else:
            key = (px.get("http", ""), px.get("https", ""))
        if key in seen:
            return
        seen.add(key)
        candidates.append(px)

    _add(proxy)

    # socks5:// -> socks5h:// (resolve DNS ở phía proxy, thường ổn định hơn)
    if proxy:
        p_http = proxy.get("http", "")
        p_https = proxy.get("https", "")
        if p_http.startswith("socks5://") or p_https.startswith("socks5://"):
            _add({
                "http": p_http.replace("socks5://", "socks5h://", 1),
                "https": p_https.replace("socks5://", "socks5h://", 1),
            })

    # Thêm route dự phòng từ Proxy4G nếu có
    try:
        from accounts.proxy import Proxy4G, GATEWAY_DIRECT_DNS
        p4g = Proxy4G()
        _add(p4g.get_for_requests())
        direct_dns = getattr(p4g, "get_for_requests_direct_dns", None)
        if callable(direct_dns):
            _add(direct_dns())
        _add({"http": GATEWAY_DIRECT_DNS, "https": GATEWAY_DIRECT_DNS})
    except Exception:
        pass

    if not candidates:
        candidates = [None]
    return candidates


def _request_with_proxy_failover(method: str, url: str, proxy: dict = None,
                                 timeout=None, **kwargs):
    last_err = None
    for idx, proxy_candidate in enumerate(_build_proxy_candidates(proxy), start=1):
        try:
            return requests.request(method, url, proxies=proxy_candidate,
                                    timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_err = exc
            log.warning(f"Proxy route {idx} fail: {str(exc)[:100]}")
    raise last_err


def _parse_error_response(resp) -> tuple:
    raw_msg = ''
    err_code = ''
    err_type = ''
    try:
        err_json = resp.json()
        detail = err_json.get('detail', {})
        if isinstance(detail, dict):
            raw_msg = detail.get('message', '') or detail.get('status', '')
            err_code = detail.get('code', '') or detail.get('status', '')
            err_type = detail.get('type', '')
        elif isinstance(detail, str):
            raw_msg = detail
    except Exception:
        raw_msg = resp.text[:500]
    return raw_msg, err_code, err_type


def _is_unusual_activity(raw_msg: str, err_code: str = '', err_type: str = '') -> bool:
    msg_l = str(raw_msg).lower()
    code_l = str(err_code).lower()
    type_l = str(err_type).lower()
    return (
        'unusual_activity' in code_l
        or 'detected_unusual_activity' in code_l
        or 'unusual_activity' in type_l
        or 'detected_unusual_activity' in type_l
        or 'unusual_activity' in msg_l
        or 'detected_unusual_activity' in msg_l
        or 'unusual activity' in msg_l
        or 'free tier usage disabled' in msg_l
    )


def _is_quota_error(raw_msg: str, err_code: str = '', err_type: str = '') -> bool:
    msg_l = str(raw_msg).lower()
    code_l = str(err_code).lower()
    type_l = str(err_type).lower()
    return (
        'quota_exceeded' in code_l
        or 'quota_exceeded' in type_l
        or 'quota_exceeded' in msg_l
        or 'exceeds your quota' in msg_l
        or 'exceeds quota' in msg_l
        or 'credits remain' in msg_l
        or 'character limit' in msg_l
    )


def _is_plan_error(raw_msg: str) -> bool:
    msg_l = str(raw_msg).lower()
    return any(k in msg_l for k in (
        'creator', 'plan', 'upgrade', 'subscription',
        'payment_required', 'paid_plan_required', 'paid plan required',
        'free users cannot use library voices'
    ))


def _ferndocs_headers(api_key: str) -> dict:
    return {
        'accept': '*/*',
        'accept-language': 'en-US,en;q=0.9,vi;q=0.8',
        'cache-control': 'no-cache',
        'content-type': 'application/json',
        'origin': 'https://elevenlabs.io',
        'pragma': 'no-cache',
        'referer': 'https://elevenlabs.io/',
        'sec-ch-ua': '"Google Chrome";v="135", "Not-A.Brand";v="8", "Chromium";v="135"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'cross-site',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
        'x-fern-proxy-request-headers': 'Xi-Api-Key,Content-Type',
        'xi-api-key': api_key,
    }


def _convert_text_ferndocs(api_key: str, voice_id: str, text: str,
                           model_id: str, output_format: str,
                           stability: float, similarity: float,
                           speed: float,
                           language_code: str = None) -> Optional[bytes]:
    url = f"{FERNDOCS_TTS_BASE}/{voice_id}/with-timestamps?output_format={output_format}"
    body = {
        'text': text,
        'model_id': model_id,
        'voice_settings': {
            'stability': stability,
            'similarity_boost': similarity,
            'speed': speed,
        }
    }
    if language_code:
        body['language_code'] = language_code
    timeout = max(120, len(text) * 80 // 1000)
    log.info(f"Convert: Ferndocs model={model_id} timeout={timeout}s")
    resp = requests.post(url, headers=_ferndocs_headers(api_key), json=body,
                         timeout=(20, timeout))

    if resp.status_code == 200:
        try:
            data = resp.json()
            audio_b64 = data.get('audio_base64', '')
            if audio_b64:
                audio = base64.b64decode(audio_b64)
                log.info(f"Convert: Ferndocs OK — {len(audio):,} bytes")
                return audio
        except Exception:
            pass
        if len(resp.content) > 100:
            log.info(f"Convert: Ferndocs raw OK — {len(resp.content):,} bytes")
            return resp.content
        raise Exception("Ferndocs không trả audio data")

    raw_msg, err_code, err_type = _parse_error_response(resp)
    log.warning(f"Convert: Ferndocs HTTP {resp.status_code} | code={err_code} | {raw_msg[:160]}")

    if resp.status_code == 404:
        raise VoiceNotFoundError(f"voice_not_found: {raw_msg}")
    if resp.status_code in (401, 403) and _is_unusual_activity(raw_msg, err_code, err_type):
        try:
            from accounts.proxy import flag_current_ip
            flag_current_ip("unusual_activity from Ferndocs/ElevenLabs")
        except Exception:
            pass
        raise IPFlaggedError("IP/route unusual_activity")
    if resp.status_code == 402 or _is_quota_error(raw_msg, err_code, err_type):
        if _is_plan_error(raw_msg):
            raise VoiceRestrictedError(f"Voice/model yêu cầu plan cao hơn: {raw_msg}")
        raise QuotaExceededError(f"Hết credit: {raw_msg[:100]}")
    if resp.status_code == 400 and _is_plan_error(raw_msg):
        raise VoiceRestrictedError(f"Voice/model yêu cầu plan cao hơn: {raw_msg}")
    if resp.status_code == 429:
        raise ConnectionError(f"Rate limited: {raw_msg}")
    raise Exception(f"Ferndocs HTTP {resp.status_code}: {raw_msg}")


def _convert_text_direct_api(api_key: str, voice_id: str, text: str,
                             model_id: str, output_format: str,
                             stability: float, similarity: float,
                             speed: float, proxy: dict = None,
                             language_code: str = None) -> Optional[bytes]:
    url = f"{API_BASE}/v1/text-to-speech/{voice_id}/with-timestamps?output_format={output_format}"
    body = {
        'text': text,
        'model_id': model_id,
        'voice_settings': {
            'stability': stability,
            'similarity_boost': similarity,
            'speed': speed,
        }
    }
    if language_code:
        body['language_code'] = language_code
    timeout = max(120, len(text) * 80 // 1000)
    log.info(f"Convert: Direct API model={model_id} timeout={timeout}s")
    # 4G/VM route có lúc bắt tay HTTPS chậm hơn đáng kể sau rotate.
    # Giữ nguyên logic convert, chỉ nới connect-timeout để bớt nổ Connection #1/#2 oan.
    resp = _request_with_proxy_failover(
        "POST", url, proxy=proxy, headers=_make_headers(api_key), json=body,
        timeout=(40, timeout))

    if resp.status_code == 200:
        try:
            data = resp.json()
            audio_b64 = data.get('audio_base64', '')
            if audio_b64:
                audio = base64.b64decode(audio_b64)
                log.info(f"Convert: Direct API OK — {len(audio):,} bytes")
                return audio
        except Exception:
            pass
        if len(resp.content) > 100:
            log.info(f"Convert: Direct API raw OK — {len(resp.content):,} bytes")
            return resp.content
        raise Exception("Direct API không trả audio data")

    raw_msg, err_code, err_type = _parse_error_response(resp)
    log.warning(f"Convert: Direct API HTTP {resp.status_code} | code={err_code} | {raw_msg[:160]}")

    if resp.status_code == 404:
        raise VoiceNotFoundError(f"voice_not_found: {raw_msg}")
    if resp.status_code in (401, 403) and _is_unusual_activity(raw_msg, err_code, err_type):
        try:
            from accounts.proxy import flag_current_ip
            flag_current_ip("unusual_activity from ElevenLabs direct API")
        except Exception:
            pass
        raise IPFlaggedError("IP/route unusual_activity")
    if resp.status_code == 402 or _is_quota_error(raw_msg, err_code, err_type):
        if _is_plan_error(raw_msg):
            raise VoiceRestrictedError(f"Voice/model yêu cầu plan cao hơn: {raw_msg}")
        raise QuotaExceededError(f"Hết credit: {raw_msg[:100]}")
    if resp.status_code == 400 and _is_plan_error(raw_msg):
        raise VoiceRestrictedError(f"Voice/model yêu cầu plan cao hơn: {raw_msg}")
    if resp.status_code == 429:
        raise ConnectionError(f"Rate limited: {raw_msg}")
    raise Exception(f"Direct API HTTP {resp.status_code}: {raw_msg}")


def _studio_headers(token: str) -> dict:
    return {
        'accept': '*/*',
        'accept-language': 'en-US,en;q=0.9,es-US;q=0.8,es;q=0.7',
        'content-type': 'application/json',
        'origin': 'https://elevenlabs.io',
        'referer': 'https://elevenlabs.io/',
        'sec-ch-ua': '" Not A;Brand";v="99", "Chromium";v="120", "Google Chrome";v="120"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-site',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        'authorization': f'Bearer {token}',
    }


def _decode_audio_response(resp) -> Optional[bytes]:
    try:
        data = resp.json()
        audio_b64 = data.get('audio_base64', '')
        if audio_b64:
            return base64.b64decode(audio_b64)
    except Exception:
        pass
    if len(resp.content) > 100:
        return resp.content
    return None


def _create_studio_project(token: str, voice_id: str, model_id: str,
                           proxy: dict = None) -> tuple:
    headers = _studio_headers(token)
    form_headers = dict(headers)
    form_headers['content-type'] = 'application/x-www-form-urlencoded'
    form = {
        'name': f'api voice {uuid.uuid4().hex[:8]}',
        'default_title_voice_id': voice_id,
        'default_paragraph_voice_id': voice_id,
        'default_model_id': model_id,
        'quality_preset': 'standard',
        'title': 'api voice',
        'author': '',
        'description': '',
        'target_audience': 'all ages',
        'language': 'en',
    }
    resp = _request_with_proxy_failover(
        "POST", f'{API_BASE}/v1/projects/add', proxy=proxy,
        headers=form_headers, data=form, timeout=60)
    if resp.status_code != 200:
        raw_msg, err_code, err_type = _parse_error_response(resp)
        log.warning(f"Studio: create project HTTP {resp.status_code} | {raw_msg[:160]}")
        if resp.status_code in (400, 401, 403, 422) and _is_unusual_activity(raw_msg, err_code, err_type):
            try:
                from accounts.proxy import flag_current_ip
                flag_current_ip('unusual_activity from Studio project create')
            except Exception:
                pass
            raise IPFlaggedError('IP/route unusual_activity')
        if resp.status_code == 402 or _is_quota_error(raw_msg, err_code, err_type):
            raise QuotaExceededError(f'Hết credit: {raw_msg[:100]}')
        if resp.status_code in (400, 403, 422) and _is_plan_error(raw_msg):
            raise VoiceRestrictedError(f'Voice/model yêu cầu plan cao hơn: {raw_msg}')
        if resp.status_code == 404:
            raise VoiceNotFoundError(f'voice_not_found: {raw_msg}')
        raise Exception(f'Studio create project HTTP {resp.status_code}: {raw_msg}')

    project = (resp.json().get('project') or {})
    project_id = project.get('project_id')
    chapters = project.get('chapters') or []
    chapter_id = (chapters[0] or {}).get('chapter_id') if chapters else None
    if not project_id:
        raise Exception('Studio không trả project_id')

    if not chapter_id:
        detail = _request_with_proxy_failover(
            "GET", f'{API_BASE}/v1/projects/{project_id}', proxy=proxy,
            headers=headers, timeout=30)
        if detail.status_code == 200:
            chapters = detail.json().get('chapters') or []
            chapter_id = (chapters[0] or {}).get('chapter_id') if chapters else None
    if not chapter_id:
        raise Exception('Studio không trả chapter_id')
    return project_id, chapter_id


def _get_studio_block_node(token: str, project_id: str, chapter_id: str,
                           proxy: dict = None) -> tuple:
    headers = _studio_headers(token)
    resp = _request_with_proxy_failover(
        "GET",
        f'{API_BASE}/v1/projects/{project_id}/chapters/{chapter_id}/blocks',
        proxy=proxy, headers=headers, timeout=30)
    if resp.status_code != 200:
        raw_msg, _, _ = _parse_error_response(resp)
        raise Exception(f'Studio blocks HTTP {resp.status_code}: {raw_msg}')
    blocks = resp.json().get('blocks') or []
    if not blocks:
        raise Exception('Studio không trả block')
    block = blocks[0]
    children = block.get('children') or []
    if not children:
        raise Exception('Studio block không có node')
    return block.get('block_id'), children[0].get('node_id'), block


def _convert_text_studio(token: str, voice_id: str, text: str,
                         model_id: str, output_format: str,
                         stability: float, similarity: float,
                         speed: float, proxy: dict = None,
                         language_code: str = None) -> Optional[bytes]:
    log.info(f"Convert: Studio API model={model_id} chars={len(text):,}")
    project_id, chapter_id = _create_studio_project(
        token, voice_id, model_id, proxy=proxy)
    block_id, node_id, block = _get_studio_block_node(
        token, project_id, chapter_id, proxy=proxy)
    if not block_id or not node_id:
        raise Exception('Studio thiếu block_id/node_id')

    now_ms = int(time.time() * 1000)
    body = {
        'client_id': uuid.uuid4().hex,
        'force_regeneration': True,
        'stitch_mode': 'text',
        'block_input_data': {
            'type': 'block',
            'is_converted': False,
            'block_snapshot_id': None,
            'locked_state': {'locked': False},
            'last_change_unix_ms': now_ms,
            'block_id': block_id,
            'sub_type': block.get('sub_type') or 'p',
            'children': [{
                'type': 'tts_node',
                'node_id': node_id,
                'text': text,
                'offset_ms': 0,
                'start_time_ms': None,
                'end_time_ms': None,
                'tts_element': None,
                'settings': {
                    'voice_id': voice_id,
                    'global_voice_settings_enabled': False,
                    'voice_settings': {
                        'speed': speed,
                        'stability': stability,
                        'similarity_boost': similarity,
                        'use_speaker_boost': True,
                        'style': 0.0,
                        'volume_gain': 0,
                        'model_id': model_id,
                    },
                },
                'volume_gain_db': 0,
                'muted': False,
                'fade_in_ms': 0,
                'fade_out_ms': 0,
            }],
            'track_id': block.get('track_id') or 'tts0',
            'order': block.get('order') or 'a0',
            'current_block_snapshot_id': None,
            'has_sts_elements': False,
            'last_converted_at_unix_ms': now_ms,
            'regeneration_count': None,
            'is_converting': False,
            'chapter_id': chapter_id,
        },
    }
    if language_code:
        body['block_input_data']['children'][0]['settings']['voice_settings']['language_code'] = language_code

    timeout = max(180, len(text) * 80 // 1000)
    url = (f'{API_BASE}/v1/projects/{project_id}/chapters/{chapter_id}'
           f'/blocks/{block_id}/nodes/{node_id}/audio?save_to_tts_history=false')
    resp = _request_with_proxy_failover(
        "POST", url, proxy=proxy, headers=_studio_headers(token), json=body,
        timeout=(40, timeout))
    if resp.status_code == 200:
        audio = _decode_audio_response(resp)
        if audio:
            log.info(f"Convert: Studio API OK — {len(audio):,} bytes")
            return audio
        raise Exception('Studio không trả audio data')

    raw_msg, err_code, err_type = _parse_error_response(resp)
    log.warning(f"Convert: Studio HTTP {resp.status_code} | code={err_code} | {raw_msg[:160]}")
    if resp.status_code == 404:
        raise VoiceNotFoundError(f'voice_not_found: {raw_msg}')
    if resp.status_code in (400, 401, 403, 422) and _is_unusual_activity(raw_msg, err_code, err_type):
        try:
            from accounts.proxy import flag_current_ip
            flag_current_ip('unusual_activity from Studio API')
        except Exception:
            pass
        raise IPFlaggedError('IP/route unusual_activity')
    if resp.status_code == 402 or _is_quota_error(raw_msg, err_code, err_type):
        raise QuotaExceededError(f'Hết credit: {raw_msg[:100]}')
    if resp.status_code in (400, 403, 422) and _is_plan_error(raw_msg):
        raise VoiceRestrictedError(f'Voice/model yêu cầu plan cao hơn: {raw_msg}')
    if resp.status_code == 429:
        raise ConnectionError(f'Rate limited: {raw_msg}')
    raise Exception(f'Studio HTTP {resp.status_code}: {raw_msg}')


class Convert:
    """Convert text → MP3 qua ElevenLabs API."""

    def __init__(self):
        self.default_model = "eleven_multilingual_v2"
        self.default_format = "mp3_44100_128"
        self.language_code = None
        self.stability = 1.0
        self.similarity = 1.0
        self.speed = 1.0

    def convert_file(self, txt_path: str, voice_id: str, token: str,
                     output_dir: str = None, proxy: dict = None,
                     max_chunk_size: int = 5000) -> Optional[str]:
        """Convert file .txt → .mp3.

        Returns: đường dẫn file MP3 hoặc None
        """
        # Đọc text
        with open(txt_path, 'r', encoding='utf-8') as f:
            text = f.read().strip()

        if not text:
            log.warning(f"Convert: file trống {txt_path}")
            return None

        base_name = os.path.splitext(os.path.basename(txt_path))[0]
        if output_dir is None:
            output_dir = os.path.join(os.path.dirname(txt_path), "output")
        os.makedirs(output_dir, exist_ok=True)

        # Chia chunks nếu dài
        from core.text_splitter import prepare_text, clean_text
        if len(text) > max_chunk_size:
            chunks = prepare_text(text, {"max_chars_per_line": max_chunk_size})
        else:
            chunks = [clean_text(text)]

        log.info(f"Convert: {base_name} — {len(text):,} chars, {len(chunks)} chunks")

        # Convert từng chunk
        audio_parts = []
        for i, chunk in enumerate(chunks):
            log.info(f"Convert: chunk {i+1}/{len(chunks)} ({len(chunk):,} chars)...")

            audio = self.convert_text(chunk, voice_id, token, proxy=proxy)
            if not audio:
                log.error(f"Convert: chunk {i+1} FAIL")
                return None

            audio_parts.append(audio)
            log.info(f"Convert: chunk {i+1} OK ({len(audio):,} bytes)")

            if i < len(chunks) - 1:
                time.sleep(2 + random.uniform(1, 3))

        # Ghép chunks
        if len(audio_parts) == 1:
            final_audio = audio_parts[0]
        else:
            from core.audio_merger import merge_audio_bytes
            final_audio = merge_audio_bytes(audio_parts, silence_between_ms=500)

        # Lưu file
        mp3_path = os.path.join(output_dir, f"{base_name}.mp3")
        with open(mp3_path, 'wb') as f:
            f.write(final_audio)

        size_mb = len(final_audio) / (1024 * 1024)
        log.info(f"Convert: XONG {mp3_path} ({size_mb:.1f} MB)")
        return mp3_path

    def convert_text(self, text: str, voice_id: str, token: str,
                     proxy: dict = None,
                     model_id: str = None) -> Optional[bytes]:
        """Convert text → MP3 bytes qua API.

        token: Bearer token hoặc API key (sk_...)
        model_id: override model (nếu None, dùng self.default_model với fallback)
        Returns: MP3 bytes hoặc None
        """
        if token.startswith("sk_"):
            if model_id:
                models_to_try = [model_id]
            else:
                start_idx = MODEL_FALLBACK_CHAIN.index(self.default_model) \
                    if self.default_model in MODEL_FALLBACK_CHAIN else 0
                models_to_try = MODEL_FALLBACK_CHAIN[start_idx:]

            last_err = None
            for model in models_to_try:
                try:
                    return _convert_text_direct_api(
                        token, voice_id, text, model, self.default_format,
                        self.stability, self.similarity, self.speed, proxy,
                        self.language_code)
                except VoiceRestrictedError as e:
                    last_err = e
                    log.warning(f"Convert: Direct API plan/model issue model={model}: {e}")
                    if model_id:
                        raise
                    continue
                except (QuotaExceededError, IPFlaggedError,
                        VoiceNotFoundError):
                    raise
            if last_err:
                raise last_err
            return None

        model = model_id or self.default_model
        try:
            return _convert_text_studio(
                token, voice_id, text, model, self.default_format,
                self.stability, self.similarity, self.speed, proxy,
                self.language_code)
        except (QuotaExceededError, IPFlaggedError,
                VoiceNotFoundError, VoiceRestrictedError):
            raise
        except Exception as studio_err:
            log.warning(
                f"Convert: Studio API fail, thu Ferndocs: {str(studio_err)[:120]}")

        try:
            log.info("Convert: Library API via Ferndocs")
            return _convert_text_ferndocs(
                token, voice_id, text, model, self.default_format,
                self.stability, self.similarity, self.speed,
                self.language_code)
        except (QuotaExceededError, IPFlaggedError,
                VoiceNotFoundError, VoiceRestrictedError):
            raise
        except Exception as ferndocs_err:
            log.warning(
                f"Convert: Ferndocs fail, thu direct Bearer: {str(ferndocs_err)[:120]}")

        from core.api_client import ApiClient
        client = ApiClient(auth_token=token, proxy=proxy)
        return client.text_to_speech(
            voice_id=voice_id,
            text=text,
            model_id=model,
            stability=self.stability,
            similarity_boost=self.similarity,
            output_format=self.default_format,
            language_code=self.language_code,
        )
