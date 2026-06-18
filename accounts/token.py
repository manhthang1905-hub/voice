"""
Skill: token — Lấy Bearer token từ ElevenLabs.

2 cách lấy token:
  1. Từ browser (localStorage) — cho TK đăng ký Google OAuth (không có password ElevenLabs)
  2. Firebase login (email+password) — cho TK đăng ký trực tiếp

Token hết hạn 1 giờ → refresh bằng refreshToken (vĩnh viễn).

Cách dùng:
    tok = Token(stealth)
    result = tok.get_from_browser("tk001")           # lấy từ browser
    result = tok.get_from_firebase("email", "pass")  # lấy từ Firebase
    tok.refresh(result["refresh_token"])              # refresh
"""

import time
from typing import Optional, Dict

from utils.logger import log


FIREBASE_LS_KEY = "firebase:authUser:AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys:[DEFAULT]"


class Token:
    """Lấy và quản lý Bearer token cho ElevenLabs API."""

    def __init__(self, stealth=None):
        self.stealth = stealth
        self._cache: Dict[str, dict] = {}  # email → {token, refresh_token, expires_at}
        # Lop luu tru ben vung (SQLite) — token song xuyen phien
        try:
            from core.token_store import get_store, MAX_LOGIN_FAILURES
            self._store = get_store()
            self._max_failures = MAX_LOGIN_FAILURES
        except Exception as e:
            log.warning(f"Token: token_store unavailable ({e}) — chi cache RAM")
            self._store = None
            self._max_failures = 3

    def _persist(self, email: str, token_data: dict, api_key: str = None):
        """Ghi token xuong SQLite (neu co store) + reset dem loi."""
        if not self._store or not email:
            return
        try:
            self._store.save(
                email=email,
                refresh_token=token_data.get("refresh_token", ""),
                id_token=token_data.get("token", ""),
                expires_at=time.time() + int(token_data.get("expires_in", 3600)),
                api_key=api_key,
                reset_failures=True,
            )
        except Exception as e:
            log.warning(f"Token: persist failed {email}: {e}")

    def _on_login_failure(self, email: str):
        """Tang dem loi; neu vuot nguong -> tu danh dau TK chet."""
        if not self._store or not email:
            return
        try:
            n = self._store.bump_failure(email)
            if n >= self._max_failures:
                log.warning(
                    f"Token: {email} that bai login {n} lan -> mark_dead")
                try:
                    from core.mode_b_accounts import mark_dead
                    mark_dead(email, f"login_failed x{n}")
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"Token: bump_failure error {email}: {e}")

    def get_from_browser(self, profile_id: str, proxy: str = None) -> Optional[dict]:
        """Lấy token từ browser localStorage (cho TK Google OAuth).

        Mở Chrome với profile đã login ElevenLabs → đọc localStorage.
        """
        if not self.stealth:
            log.error("Token: stealth required for browser method")
            return None

        page = None
        try:
            page = self.stealth.open(profile_id, proxy=proxy)
            page.get('https://elevenlabs.io/app/speech-synthesis')
            time.sleep(8)

            import json
            auth_json = page.run_js(f'return localStorage.getItem("{FIREBASE_LS_KEY}") || ""')
            if not auth_json:
                log.warning(f"Token: no auth data in localStorage for {profile_id}")
                return None

            data = json.loads(auth_json)
            stm = data.get('stsTokenManager', {})
            token = stm.get('accessToken', '')
            refresh = stm.get('refreshToken', '')
            exp_time = stm.get('expirationTime', 0)

            if not token:
                log.warning(f"Token: no accessToken for {profile_id}")
                return None

            email = data.get('email', '')
            expires_in = max(0, int((exp_time - time.time() * 1000) / 1000))

            # Cache RAM + ben vung
            self._cache[email] = {
                "token": token,
                "refresh_token": refresh,
                "expires_at": time.time() + expires_in,
            }
            self._persist(email, {
                "token": token,
                "refresh_token": refresh,
                "expires_in": expires_in,
            })

            log.info(f"Token: browser OK {email} (expires {expires_in}s, chars available)")
            return {
                "token": token,
                "refresh_token": refresh,
                "expires_in": expires_in,
                "email": email,
            }

        except Exception as e:
            log.error(f"Token: browser error {profile_id}: {e}")
            return None
        finally:
            if page:
                try:
                    page.quit()
                except Exception:
                    pass

    def get(self, email: str, password: str,
            proxy: dict = None) -> Optional[dict]:
        """Lấy Bearer token qua Firebase login.

        Args:
            email: Email TK ElevenLabs
            password: Password
            proxy: Proxy dict cho requests {"http": "...", "https": "..."}

        Returns: {"token": str, "refresh_token": str, "expires_in": int} hoặc None
        """
        # Cache hit (RAM)?
        cached = self._cache.get(email)
        if cached and cached["expires_at"] > time.time() + 300:  # còn >5 phút
            log.info(f"Token: cache hit {email} (còn {int(cached['expires_at'] - time.time())}s)")
            return {
                "token": cached["token"],
                "refresh_token": cached["refresh_token"],
                "expires_in": int(cached["expires_at"] - time.time()),
            }

        # Cache hit (SQLite ben vung) — token con han tu phien truoc
        if self._store and not cached:
            persisted = self._store.get(email)
            if persisted:
                exp = persisted.get("token_expires_at") or 0
                rt = persisted.get("refresh_token") or ""
                if persisted.get("id_token") and exp > time.time() + 300:
                    log.info(f"Token: DB hit {email} (còn {int(exp - time.time())}s)")
                    self._cache[email] = {
                        "token": persisted["id_token"],
                        "refresh_token": rt,
                        "expires_at": exp,
                    }
                    return {
                        "token": persisted["id_token"],
                        "refresh_token": rt,
                        "expires_in": int(exp - time.time()),
                    }
                # Token het han nhung con refresh_token -> dung de refresh
                if rt:
                    cached = {"refresh_token": rt}

        # Thử refresh nếu có refresh_token
        if cached and cached.get("refresh_token"):
            refreshed = self.refresh(cached["refresh_token"], proxy)
            if refreshed:
                self._cache[email] = {
                    "token": refreshed["token"],
                    "refresh_token": refreshed["refresh_token"],
                    "expires_at": time.time() + refreshed["expires_in"],
                }
                self._persist(email, refreshed)
                return refreshed

        # Login mới
        try:
            from core.api_client import firebase_login
            result = firebase_login(email, password, proxy=proxy)

            token_data = {
                "token": result["idToken"],
                "refresh_token": result["refreshToken"],
                "expires_in": int(result.get("expiresIn", 3600)),
            }

            # Cache RAM + ben vung
            self._cache[email] = {
                "token": token_data["token"],
                "refresh_token": token_data["refresh_token"],
                "expires_at": time.time() + token_data["expires_in"],
            }
            self._persist(email, token_data)

            log.info(f"Token: login OK {email} (expires {token_data['expires_in']}s)")
            return token_data

        except Exception as e:
            log.error(f"Token: login failed {email}: {e}")
            self._on_login_failure(email)
            return None

    def refresh(self, refresh_token: str, proxy: dict = None) -> Optional[dict]:
        """Refresh token (không cần email/password)."""
        try:
            from core.api_client import firebase_refresh
            result = firebase_refresh(refresh_token, proxy=proxy)

            token_data = {
                "token": result["id_token"],
                "refresh_token": result["refresh_token"],
                "expires_in": int(result.get("expires_in", 3600)),
            }

            log.info(f"Token: refresh OK (expires {token_data['expires_in']}s)")
            return token_data

        except Exception as e:
            log.error(f"Token: refresh failed: {e}")
            return None

    def get_many(self, accounts: list, proxy: dict = None) -> list:
        """Lấy tokens cho nhiều TK.

        Args:
            accounts: [{"email": str, "password": str}, ...]

        Returns: [{"email": str, "token": str, "refresh_token": str}, ...]
        """
        results = []
        for acc in accounts:
            tok = self.get(acc["email"], acc["password"], proxy)
            if tok:
                results.append({
                    "email": acc["email"],
                    **tok,
                })
                log.info(f"Token: {acc['email']} OK")
            else:
                log.warning(f"Token: {acc['email']} FAIL")
            time.sleep(2)  # delay giữa các login
        return results

    def clear_cache(self):
        self._cache.clear()
