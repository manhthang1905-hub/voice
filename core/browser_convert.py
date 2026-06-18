"""
Browser Convert — Paste text vào web ElevenLabs → Generate → Download MP3.

Dùng khi API bị flag (unusual_activity). Web UI vẫn hoạt động.

Flow:
1. Mở Chrome (profile TK đã login ElevenLabs)
2. Vào speech-synthesis/text-to-speech
3. Chọn voice (nếu cần)
4. Paste text vào textarea
5. Click "Generate speech"
6. Đợi generate xong
7. Download MP3

Cách dùng:
    bc = BrowserConvert(stealth)
    audio = bc.convert("tk001", "Hello world", voice_id="...")
    # → bytes MP3
"""

import os
import time
import json
import base64
import ast
import glob as glob_module
from typing import Optional
import requests

from utils.logger import log
from accounts.voice import Voice


class BrowserConvert:
    """Convert voice qua web browser (bypass API flag)."""

    TTS_URL = "https://elevenlabs.io/app/speech-synthesis/text-to-speech"
    PUBLIC_TTS_URL = "https://elevenlabs.io/text-to-speech"
    SIGNIN_URL = "https://elevenlabs.io/app/sign-in"
    FIREBASE_LS_KEY = "firebase:authUser:AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys:[DEFAULT]"
    def __init__(self, stealth):
        self.stealth = stealth
        self.last_error = ""

    def convert(self, profile_id: str, text: str, voice_id: str = None,
                proxy: str = None, timeout: int = 600, auth_data: dict = None,
                email: str = "", api_key: str = "") -> Optional[bytes]:
        """Convert text → MP3 qua web browser.

        Args:
            profile_id: Chrome profile (TK đã login ElevenLabs)
            text: Text cần convert (max ~5000 chars)
            voice_id: Nếu có → thêm voice trước
            proxy: Proxy string
            timeout: Thời gian chờ generate (giây)

        Returns: MP3 bytes hoặc None
        """
        page = None
        cleanup_voice_id = None
        try:
            self.last_error = ""
            try:
                self.stealth.create(profile_id, email=email)
            except Exception:
                pass
            page = self.stealth.open(profile_id, proxy=proxy)
            self._show_window(page)
            log.info(f"BrowserConvert: opened Chrome for {email or profile_id}")
            self._log_page_state(page, "after_open")

            if auth_data:
                self._seed_firebase_auth(page, auth_data, email=email)
                if voice_id:
                    preflight_ok = self._preflight_web_generate(page, proxy=proxy)
                    if not preflight_ok:
                        return None
                    self._reset_tts_editor_state(page)

            selected_voice = False
            if voice_id:
                voice_name = ""
                if auth_data:
                    self._cleanup_old_library_voices(
                        auth_data=auth_data, api_key=api_key, proxy=proxy
                    )
                    voice_helper = Voice(self.stealth)
                    ok, msg = voice_helper.add_and_use_on_page(page, voice_id)
                    cleanup_voice_id = voice_id
                    log.info(f"BrowserConvert: add_and_use({voice_id}) -> {ok} | {msg}")
                    if not ok:
                        self.last_error = f"voice_add_fail:{msg}"
                        log.error(f"BrowserConvert: add_and_use fail -> {msg}")
                        return None
                    self._show_window(page)
                    self._log_page_state(page, "tts_page_reopen")
                    self._dismiss_popups(page)
                    self._reset_tts_editor_state(page)
                    voice_name = self._resolve_voice_name(api_key, voice_id, proxy)
                    if voice_name:
                        if self._voice_name_visible(page, voice_name):
                            selected_voice = True
                            log.info(
                                f"BrowserConvert: voice already visible after add/use '{voice_name}'"
                            )
                        else:
                            selected_voice = self._ensure_voice_selected(page, voice_name)
                            log.info(
                                f"BrowserConvert: ensure voice after add/use '{voice_name}' -> {selected_voice}"
                            )
                    if voice_name and not selected_voice:
                        self.last_error = f"voice_not_selected:{voice_name}"
                        log.error(
                            f"BrowserConvert: add/use xong nhung chua chon duoc voice '{voice_name}'"
                        )
                        return None
                else:
                    page.get(self.TTS_URL)
                    self._show_window(page)
                    time.sleep(8)
                    self._log_page_state(page, "tts_page")
                    if 'sign-in' in page.url.lower():
                        self.last_error = "not_logged_in"
                        log.error("BrowserConvert: chưa login ElevenLabs")
                        return None
                    self._dismiss_popups(page)
                    self._reset_tts_editor_state(page)
                    voice_name = self._resolve_voice_name(api_key, voice_id, proxy)
                if voice_name and not auth_data:
                    selected_voice = self._ensure_voice_selected(page, voice_name)
                    log.info(f"BrowserConvert: select voice '{voice_name}' -> {selected_voice}")
            else:
                page.get(self.TTS_URL)
                self._show_window(page)
                time.sleep(8)
                self._log_page_state(page, "tts_page")
                if 'sign-in' in page.url.lower():
                    self.last_error = "not_logged_in"
                    log.error("BrowserConvert: chưa login ElevenLabs")
                    return None
                self._dismiss_popups(page)
                self._reset_tts_editor_state(page)

            # === Paste text ===
            injected = self._inject_text(page, text)
            if not injected:
                textarea = self._find_textarea(page)
                if not textarea:
                    self.last_error = "textarea_not_found"
                    log.error("BrowserConvert: không tìm thấy textarea")
                    return None

                textarea.click()
                time.sleep(0.5)
                textarea.clear()
                textarea.input(text)
                time.sleep(1)
            log.info(f"BrowserConvert: đã nhập {len(text):,} chars")

            try:
                page.listen.start("api.us.elevenlabs.io/v1/text-to-speech")
            except Exception:
                pass

            # === Ghi nhận file cũ trong download dir ===
            download_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "output", "_browser_downloads")
            os.makedirs(download_dir, exist_ok=True)
            old_files = set(os.listdir(download_dir))

            # === Click Generate ===
            gen_btn = self._find_generate_button(page)
            if not gen_btn:
                self.last_error = "generate_button_not_found"
                log.error("BrowserConvert: không tìm thấy nút Generate")
                return None

            if gen_btn is True:
                log.info("BrowserConvert: generate via JS fallback")
            else:
                gen_btn.click()
            log.info("BrowserConvert: đang generate...")
            time.sleep(3)

            packet_audio = self._capture_stream_audio(
                page, proxy=proxy, wait_timeout=min(90, max(45, timeout // 6))
            )
            if packet_audio:
                return packet_audio

            # === Đợi generate xong ===
            start = time.time()
            while time.time() - start < timeout:
                time.sleep(3)
                elapsed = int(time.time() - start)

                # Kiểm tra lỗi
                error = self._check_error(page)
                if error:
                    self.last_error = f"web_error: {error}"
                    log.error(f"BrowserConvert: lỗi — {error}")
                    return None

                # Tìm nút Download
                dl_btn = (page.ele('@aria-label=Download Audio', timeout=1) or
                          page.ele('@data-testid=audio-player-download-button', timeout=1))

                if dl_btn:
                    log.info(f"BrowserConvert: generate xong ({elapsed}s), downloading...")

                    # Set download path
                    try:
                        page.run_cdp('Page.setDownloadBehavior',
                                     behavior='allow',
                                     downloadPath=download_dir)
                    except Exception:
                        pass

                    dl_btn.click()
                    time.sleep(10)

                    # Tìm file mới
                    download_wait = max(300, min(900, timeout))
                    dl_start = time.time()
                    while time.time() - dl_start < download_wait:
                        new_files = set(os.listdir(download_dir)) - old_files
                        mp3_files = [f for f in new_files
                                     if (f.endswith('.mp3') or f.endswith('.wav'))
                                     and not f.endswith('.crdownload')]
                        if mp3_files:
                            mp3_path = os.path.join(download_dir, mp3_files[0])
                            time.sleep(2)
                            with open(mp3_path, 'rb') as f:
                                audio = f.read()
                            try:
                                os.remove(mp3_path)
                            except Exception:
                                pass
                            log.info(f"BrowserConvert: OK {len(audio):,} bytes")
                            return audio
                        time.sleep(3)

                    # Fallback: lấy từ blob URL
                    audio = self._download_blob(page)
                    if audio:
                        return audio

                    log.error(f"BrowserConvert: download timeout ({download_wait}s)")
                    self.last_error = f"download_timeout_{download_wait}s"
                    return None

                # Kiểm tra blob audio (backup)
                try:
                    has_audio = page.run_js(
                        "return !!document.querySelector('audio')?.src")
                    if has_audio:
                        # Có audio nhưng chưa có nút download → đợi thêm
                        pass
                except Exception:
                    pass

                if elapsed % 15 == 0:
                    log.info(f"BrowserConvert: đợi... {elapsed}s")

            log.error(f"BrowserConvert: timeout {timeout}s")
            self.last_error = f"timeout_{timeout}s"
            return None

        except Exception as e:
            self.last_error = str(e)[:200]
            log.error(f"BrowserConvert: {e}")
            return None
        finally:
            if cleanup_voice_id and auth_data:
                self._cleanup_added_voice(cleanup_voice_id, auth_data, api_key=api_key, proxy=proxy)
            if page:
                try:
                    page.quit()
                except Exception:
                    pass
            try:
                self.stealth.cleanup(profile_id)
            except Exception:
                pass

    def convert_file(self, profile_id: str, txt_path: str,
                     voice_id: str = None, output_dir: str = None,
                     proxy: str = None, max_chunk: int = 5000) -> Optional[str]:
        """Convert file .txt → .mp3 qua browser.

        Text dài → chia chunks → convert từng phần → ghép.
        """
        with open(txt_path, 'r', encoding='utf-8') as f:
            text = f.read().strip()

        if not text:
            return None

        base_name = os.path.splitext(os.path.basename(txt_path))[0]
        if output_dir is None:
            output_dir = os.path.join(os.path.dirname(txt_path), "output")
        os.makedirs(output_dir, exist_ok=True)

        # Chia chunks
        from core.text_splitter import prepare_text, clean_text
        if len(text) > max_chunk:
            chunks = prepare_text(text, {"max_chars_per_line": max_chunk})
        else:
            chunks = [clean_text(text)]

        log.info(f"BrowserConvert: {base_name} — {len(text):,} chars, {len(chunks)} chunks")

        # Convert từng chunk
        audio_parts = []
        for i, chunk in enumerate(chunks):
            log.info(f"BrowserConvert: chunk {i+1}/{len(chunks)} ({len(chunk):,} chars)")

            audio = self.convert(profile_id, chunk, voice_id=voice_id, proxy=proxy)
            if not audio:
                log.error(f"BrowserConvert: chunk {i+1} FAIL")
                return None

            audio_parts.append(audio)
            time.sleep(3)

        # Ghép
        if len(audio_parts) == 1:
            final = audio_parts[0]
        else:
            from core.audio_merger import merge_audio_bytes
            final = merge_audio_bytes(audio_parts, silence_between_ms=500)

        mp3_path = os.path.join(output_dir, f"{base_name}.mp3")
        with open(mp3_path, 'wb') as f:
            f.write(final)

        log.info(f"BrowserConvert: XONG {mp3_path} ({len(final)/1024/1024:.1f} MB)")
        return mp3_path

    # ============================================================
    # HELPERS
    # ============================================================

    def _find_textarea(self, page):
        """Tìm textarea trên trang TTS."""
        for sel in ['@aria-label=Main textarea',
                    'tag:textarea@@placeholder:Start typing',
                    'tag:textarea']:
            try:
                el = page.ele(sel, timeout=3)
                if el and el.tag.lower() == 'textarea':
                    return el
            except Exception:
                pass
        return None

    def _inject_text(self, page, text: str) -> bool:
        try:
            result = page.run_js(
                """
                const ta = document.querySelector('textarea');
                if (!ta) return false;
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                ).set;
                setter.call(ta, arguments[0]);
                ta.dispatchEvent(new Event('input', { bubbles: true }));
                ta.dispatchEvent(new Event('change', { bubbles: true }));
                ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
                ta.dispatchEvent(new KeyboardEvent('keyup', { key: 'a', bubbles: true }));
                ta.focus();
                return true;
                """,
                text,
            )
            time.sleep(1.5)
            return bool(result)
        except Exception as exc:
            log.warning(f"BrowserConvert: inject text fail: {exc}")
            return False

    def _reset_tts_editor_state(self, page) -> None:
        """Xóa draft/text còn sót để mỗi account vào browser flow với editor sạch."""
        try:
            page.run_js(
                """
                try {
                  const keys = [];
                  for (let i = 0; i < localStorage.length; i++) keys.push(localStorage.key(i));
                  for (let i = 0; i < sessionStorage.length; i++) keys.push(sessionStorage.key(i));
                  const uniq = [...new Set(keys)];
                  for (const k of uniq) {
                    const lk = (k || '').toLowerCase();
                    if (lk.includes('draft') || lk.includes('speech') || lk.includes('tts') || lk.includes('text')) {
                      try { localStorage.removeItem(k); } catch (e) {}
                      try { sessionStorage.removeItem(k); } catch (e) {}
                    }
                  }
                } catch (e) {}
                const ta = document.querySelector('textarea');
                if (ta) {
                  const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                  ).set;
                  setter.call(ta, '');
                  ta.dispatchEvent(new Event('input', { bubbles: true }));
                  ta.dispatchEvent(new Event('change', { bubbles: true }));
                }
                const editable = document.querySelector('[contenteditable=\"true\"]');
                if (editable) {
                  editable.innerHTML = '';
                  editable.dispatchEvent(new Event('input', { bubbles: true }));
                }
                return true;
                """
            )
            time.sleep(0.8)
            log.info("BrowserConvert: reset TTS editor state")
        except Exception as exc:
            log.warning(f"BrowserConvert: reset TTS state fail: {exc}")

    def _preflight_web_generate(self, page, proxy: str = None) -> bool:
        """Thử generate text rất ngắn bằng default voice để bắt unusual_activity sớm."""
        try:
            page.get(self.TTS_URL)
            time.sleep(4)
            self._dismiss_popups(page)

            if not self._inject_text(page, "ok"):
                textarea = self._find_textarea(page)
                if not textarea:
                    self.last_error = "preflight_textarea_not_found"
                    log.error("BrowserConvert: preflight textarea not found")
                    return False
                textarea.click()
                time.sleep(0.3)
                textarea.clear()
                textarea.input("ok")
                time.sleep(0.8)

            try:
                page.listen.start("api.us.elevenlabs.io/v1/text-to-speech")
            except Exception:
                pass

            gen_btn = self._find_generate_button(page)
            if not gen_btn:
                self.last_error = "preflight_generate_button_not_found"
                log.error("BrowserConvert: preflight generate button not found")
                return False

            if gen_btn is True:
                log.info("BrowserConvert: preflight generate via JS fallback")
            else:
                gen_btn.click()
            log.info("BrowserConvert: preflight generate...")
            time.sleep(2)

            packet_audio = self._capture_stream_audio(page, proxy=proxy, wait_timeout=18)
            if packet_audio:
                log.info("BrowserConvert: preflight OK (packet audio)")
                return True

            start = time.time()
            while time.time() - start < 20:
                error = self._check_error(page)
                if error:
                    self.last_error = f"web_error: {error}"
                    log.error(f"BrowserConvert: preflight fail - {error}")
                    return False
                try:
                    dl_btn = (page.ele('@aria-label=Download Audio', timeout=1) or
                              page.ele('@data-testid=audio-player-download-button', timeout=1))
                    if dl_btn:
                        log.info("BrowserConvert: preflight OK (download ready)")
                        return True
                except Exception:
                    pass
                time.sleep(2)

            log.info("BrowserConvert: preflight no explicit flag -> continue")
            return True
        except Exception as exc:
            self.last_error = f"preflight_error:{str(exc)[:80]}"
            log.warning(f"BrowserConvert: preflight error: {exc}")
            return False

    def _show_window(self, page) -> None:
        try:
            info = page.run_cdp("Browser.getWindowForTarget")
            win_id = info.get("windowId")
            if win_id:
                page.run_cdp(
                    "Browser.setWindowBounds",
                    windowId=win_id,
                    bounds={"windowState": "maximized"},
                )
        except Exception:
            pass

    def _capture_stream_audio(self, page, proxy: str = None,
                              wait_timeout: int = 45) -> Optional[bytes]:
        try:
            item = page.listen.wait(timeout=wait_timeout)
        except Exception:
            return None
        if not item:
            return None

        resp = getattr(item, "response", None)
        status = getattr(resp, "status", None)
        if not resp or status is None:
            return None

        try:
            body = resp.body
        except Exception as exc:
            log.warning(f"BrowserConvert: packet body error: {exc}")
            return None

        if status == 200:
            if isinstance(body, (bytes, bytearray)) and len(body) > 1000:
                log.info(f"BrowserConvert: packet audio OK {len(body):,} bytes")
                return bytes(body)
            if isinstance(body, dict):
                audio_b64 = body.get("audio_base64", "")
                if audio_b64:
                    audio = base64.b64decode(audio_b64)
                    log.info(f"BrowserConvert: packet audio(base64) OK {len(audio):,} bytes")
                    return audio
        elif isinstance(body, dict):
            detail = body.get("detail", {})
            if isinstance(detail, dict):
                msg = detail.get("message") or detail.get("status") or str(detail)
            else:
                msg = str(detail or body)
            self.last_error = f"stream_http_{status}: {msg[:160]}"
            log.error(f"BrowserConvert: stream packet HTTP {status} - {msg}")
        else:
            self.last_error = f"stream_http_{status}"
            log.error(f"BrowserConvert: stream packet HTTP {status}")
        return None

    def _replay_stream_packet(self, page, item, proxy: str = None) -> Optional[bytes]:
        req = getattr(item, "request", None)
        url = getattr(item, "url", "")
        if not req or not url:
            return None

        req_headers = getattr(req, "headers", {}) or {}
        if isinstance(req_headers, str):
            try:
                req_headers = ast.literal_eval(req_headers)
            except Exception:
                req_headers = {}
        post_data = getattr(req, "postData", None) or {}
        if not isinstance(post_data, dict) or not post_data.get("hcaptcha_token"):
            return None

        replay_headers = {}
        for key in (
            "Authorization",
            "Referer",
            "User-Agent",
            "sec-ch-ua",
            "sec-ch-ua-mobile",
            "sec-ch-ua-platform",
            "accept",
            "accept-language",
        ):
            value = req_headers.get(key)
            if value:
                replay_headers[key] = value
        replay_headers["Origin"] = "https://elevenlabs.io"
        replay_headers["Content-Type"] = "application/json"
        replay_headers["sec-fetch-dest"] = "empty"
        replay_headers["sec-fetch-mode"] = "cors"
        replay_headers["sec-fetch-site"] = "same-site"

        try:
            cookies = page.cookies()
        except Exception:
            cookies = []
        if cookies:
            replay_headers["Cookie"] = "; ".join(
                f"{cookie.get('name')}={cookie.get('value')}"
                for cookie in cookies
                if cookie.get("name")
            )

        proxies = None
        if proxy:
            proxies = {"http": proxy, "https": proxy}

        try:
            resp = requests.post(
                url,
                headers=replay_headers,
                json=post_data,
                proxies=proxies,
                timeout=300,
            )
        except Exception as exc:
            log.warning(f"BrowserConvert: replay request fail: {exc}")
            return None

        if resp.status_code != 200:
            msg = resp.text[:300]
            try:
                data = resp.json()
                detail = data.get("detail", {})
                if isinstance(detail, dict):
                    msg = detail.get("message") or detail.get("status") or str(detail)
                elif detail:
                    msg = str(detail)
            except Exception:
                pass
            log.error(f"BrowserConvert: replay HTTP {resp.status_code} - {msg}")
            return None

        try:
            data = resp.json()
            audio_b64 = data.get("audio_base64", "")
            if audio_b64:
                audio = base64.b64decode(audio_b64)
                log.info(f"BrowserConvert: replay audio(base64) OK {len(audio):,} bytes")
                return audio
        except Exception:
            pass

        if len(resp.content) > 1000:
            log.info(f"BrowserConvert: replay audio OK {len(resp.content):,} bytes")
            return resp.content
        return None

    def _resolve_voice_name(self, api_key: str, voice_id: str, proxy: str = None) -> str:
        if not api_key or not voice_id:
            return ""
        proxies = None
        if proxy:
            proxies = {"http": proxy, "https": proxy}
        try:
            resp = requests.get(
                f"https://api.elevenlabs.io/v1/voices/{voice_id}",
                headers={"xi-api-key": api_key, "accept": "application/json"},
                proxies=proxies,
                timeout=30,
            )
            if resp.status_code == 200:
                return (resp.json().get("name") or "").strip()
        except Exception as exc:
            log.warning(f"BrowserConvert: resolve voice name fail: {exc}")
        return ""

    def _cleanup_added_voice(self, voice_id: str, auth_data: dict,
                             api_key: str = "", proxy: str = None) -> bool:
        if not voice_id:
            return False
        headers_list = []
        id_token = (auth_data or {}).get("idToken", "")
        if id_token:
            headers_list.append({
                "Authorization": f"Bearer {id_token}",
                "accept": "*/*",
                "origin": "https://elevenlabs.io",
                "referer": "https://elevenlabs.io/",
            })
        if api_key:
            headers_list.append({
                "xi-api-key": api_key,
                "accept": "*/*",
                "origin": "https://elevenlabs.io",
                "referer": "https://elevenlabs.io/",
            })

        proxies = None
        if proxy:
            proxies = {"http": proxy, "https": proxy}

        for headers in headers_list:
            try:
                resp = requests.delete(
                    f"https://api.us.elevenlabs.io/v1/voices/{voice_id}",
                    headers=headers,
                    proxies=proxies,
                    timeout=30,
                )
                if resp.status_code == 200:
                    log.info(f"BrowserConvert: cleanup voice OK -> {voice_id}")
                    return True
                log.warning(
                    f"BrowserConvert: cleanup voice HTTP {resp.status_code} -> {voice_id}"
                )
            except Exception as exc:
                log.warning(f"BrowserConvert: cleanup voice fail: {exc}")
        return False

    def _list_account_voices(self, auth_data: dict, api_key: str = "", proxy: str = None) -> list:
        headers_list = []
        id_token = (auth_data or {}).get("idToken", "")
        if id_token:
            headers_list.append({"Authorization": f"Bearer {id_token}", "accept": "application/json"})
        if api_key:
            headers_list.append({"xi-api-key": api_key, "accept": "application/json"})

        proxies = None
        if proxy:
            proxies = {"http": proxy, "https": proxy}

        for headers in headers_list:
            try:
                resp = requests.get(
                    "https://api.us.elevenlabs.io/v1/voices",
                    headers=headers,
                    proxies=proxies,
                    timeout=45,
                )
                if resp.status_code == 200:
                    return resp.json().get("voices", []) or []
            except Exception as exc:
                log.warning(f"BrowserConvert: list voices fail: {exc}")
        return []

    def _cleanup_old_library_voices(self, auth_data: dict, api_key: str = "",
                                    proxy: str = None) -> int:
        removed = 0
        voices = self._list_account_voices(auth_data, api_key=api_key, proxy=proxy)
        for voice in voices:
            vid = (voice.get("voice_id") or "").strip()
            category = (voice.get("category") or "").strip().lower()
            if not vid:
                continue
            if category == "premade":
                continue
            if self._cleanup_added_voice(vid, auth_data, api_key=api_key, proxy=proxy):
                removed += 1
        if removed:
            log.info(f"BrowserConvert: cleanup old library voices -> {removed}")
        return removed

    def _select_voice_name(self, page, voice_name: str) -> bool:
        if not voice_name:
            return False
        try:
            for sel in [
                '@aria-label=Voice',
                'text=Voice',
            ]:
                try:
                    el = page.ele(sel, timeout=2)
                    if el:
                        el.click()
                        time.sleep(1.5)
                        break
                except Exception:
                    pass
            for sel in [f'text={voice_name}', f'text:{voice_name}']:
                try:
                    el = page.ele(sel, timeout=3)
                    if el:
                        el.click()
                        time.sleep(2)
                        return True
                except Exception:
                    pass
        except Exception as exc:
            log.warning(f"BrowserConvert: select voice fail: {exc}")
        return False

    def _voice_name_visible(self, page, voice_name: str) -> bool:
        if not voice_name:
            return False
        try:
            js = """
                const name = arguments[0].toLowerCase();
                const nodes = Array.from(document.querySelectorAll('button,div,span'));
                for (const el of nodes) {
                    const txt = (el.textContent || '').trim().toLowerCase();
                    if (!txt) continue;
                    if (txt === name || txt.includes(name)) return true;
                }
                return false;
            """
            return bool(page.run_js(js, voice_name))
        except Exception:
            return False

    def _ensure_voice_selected(self, page, voice_name: str) -> bool:
        if not voice_name:
            return False
        for _ in range(4):
            if self._select_voice_name(page, voice_name):
                time.sleep(2)
                if self._voice_name_visible(page, voice_name):
                    return True
            elif self._voice_name_visible(page, voice_name):
                return True
            time.sleep(2)
        return False

    def _seed_firebase_auth(self, page, auth_data: dict, email: str = "") -> None:
        """Inject Firebase auth trên đúng app origin để web app nhận session."""
        try:
            ready = False
            for url in (self.PUBLIC_TTS_URL, self.SIGNIN_URL, self.TTS_URL):
                page.get(url)
                time.sleep(4)
                self._log_page_state(page, f"seed_probe:{url}")
                if "elevenlabs.io" not in (page.url or ""):
                    continue
                if self._local_storage_ready(page):
                    ready = True
                    break
            if not ready:
                raise RuntimeError(f"localStorage unavailable at url: {page.url}")
            local_id = auth_data.get("localId", "")
            refresh_token = auth_data.get("refreshToken", "")
            id_token = auth_data.get("idToken", "")
            expires_in = int(auth_data.get("expiresIn", 3600) or 3600)
            auth_email = email or auth_data.get("email", "")
            payload = {
                "uid": local_id,
                "displayName": None,
                "email": auth_email,
                "emailVerified": True,
                "phoneNumber": None,
                "photoURL": None,
                "isAnonymous": False,
                "tenantId": None,
                "providerData": [{
                    "providerId": "password",
                    "uid": auth_email,
                    "displayName": None,
                    "email": auth_email,
                    "phoneNumber": None,
                    "photoURL": None,
                }],
                "stsTokenManager": {
                    "refreshToken": refresh_token,
                    "accessToken": id_token,
                    "expirationTime": int(time.time() * 1000) + expires_in * 1000,
                },
                "createdAt": str(int(time.time() * 1000)),
                "lastLoginAt": str(int(time.time() * 1000)),
                "apiKey": "AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys",
                "appName": "[DEFAULT]",
            }
            payload_json = json.dumps(payload, separators=(",", ":"))
            saved = page.run_js(
                f"""
                localStorage.setItem({json.dumps(self.FIREBASE_LS_KEY)}, {json.dumps(payload_json)});
                window.dispatchEvent(new Event('storage'));
                return localStorage.getItem({json.dumps(self.FIREBASE_LS_KEY)}) || "";
                """
            )
            if not saved:
                raise RuntimeError("firebase auth not persisted to localStorage")
            page.get(self.TTS_URL)
            self._enforce_window(page, "post_seed_tts")
            time.sleep(6)
            self._log_page_state(page, "post_seed_tts")
            log.info(f"BrowserConvert: injected firebase auth for {auth_email or 'profile'}")
        except Exception as exc:
            log.warning(f"BrowserConvert: inject auth fail: {exc}")

    def _local_storage_ready(self, page) -> bool:
        try:
            result = page.run_js(
                """
                try {
                  const k = "__bc_probe__";
                  localStorage.setItem(k, "1");
                  const v = localStorage.getItem(k);
                  localStorage.removeItem(k);
                  return v === "1";
                } catch (e) {
                  return false;
                }
                """
            )
            return bool(result)
        except Exception:
            return False

    def _log_page_state(self, page, label: str) -> None:
        try:
            title = ""
            try:
                title = (page.title or "")[:80]
            except Exception:
                pass
            state = page.run_js(
                """
                try {
                  return {
                    href: location.href || "",
                    origin: location.origin || "",
                    online: navigator.onLine,
                    ready: document.readyState || "",
                    bodyText: (document.body && document.body.innerText || "").slice(0, 120),
                    bodyLen: (document.body && document.body.innerText || "").length
                  };
                } catch (e) {
                  return { err: String(e) };
                }
                """
            ) or {}
            log.info(
                "BrowserConvert: state[%s] title=%s url=%s online=%s ready=%s bodyLen=%s body=%s"
                % (
                    label,
                    title,
                    str(state.get("href", ""))[:120],
                    state.get("online"),
                    state.get("ready"),
                    state.get("bodyLen"),
                    str(state.get("bodyText", "")).replace("\n", " ")[:120],
                )
            )
        except Exception as exc:
            log.warning(f"BrowserConvert: state[{label}] fail: {exc}")

    def _find_generate_button(self, page):
        """Tìm nút Generate speech (xuất hiện sau khi nhập text)."""
        for _ in range(10):
            try:
                clicked = page.run_js(
                    """
                    const btn = document.querySelector('[data-testid="tts-generate"]');
                    if (btn && !btn.disabled) { btn.click(); return true; }
                    return false;
                    """
                )
                if clicked:
                    return True
            except Exception:
                pass
            time.sleep(1)

        # Đợi button xuất hiện
        for _ in range(10):
            for sel in ['@aria-label:Generate speech',
                        '@aria-label:Regenerate speech',
                        '@data-testid=tts-generate',
                        '@data-testid=generate-button',
                        'text=Generate speech',
                        'text=Regenerate']:
                try:
                    el = page.ele(sel, timeout=1)
                    if el:
                        return el
                except Exception:
                    pass
            time.sleep(1)

        # Fallback: tìm bằng JS
        try:
            page.run_js('''
                var btns = document.querySelectorAll("button");
                for (var b of btns) {
                    var al = (b.getAttribute("aria-label") || "").toLowerCase();
                    if (al.includes("generate")) { b.click(); return true; }
                }
                return false;
            ''')
            return True  # Đã click bằng JS
        except Exception:
            pass

        return None

    def _dismiss_popups(self, page):
        """Đóng popup nếu có."""
        for sel in ['text=Remind me later', '@aria-label=Close popup',
                    '@aria-label=Dismiss']:
            try:
                el = page.ele(sel, timeout=1)
                if el:
                    el.click()
                    time.sleep(0.5)
            except Exception:
                pass

    def _check_error(self, page):
        """Kiểm tra lỗi trên web."""
        for keyword in ['Unusual activity', 'usage disabled',
                        'Insufficient', 'limit reached']:
            try:
                el = page.ele(f'text:{keyword}', timeout=1)
                if el:
                    return el.text[:80] if el.text else keyword
            except Exception:
                pass
        return None

    def _download_blob(self, page) -> Optional[bytes]:
        """Download audio từ blob URL."""
        try:
            audio_b64 = page.run_js("""
                return new Promise((resolve, reject) => {
                    const audio = document.querySelector('audio');
                    if (!audio || !audio.src) { reject('No audio'); return; }
                    fetch(audio.src)
                        .then(r => r.blob())
                        .then(blob => {
                            const reader = new FileReader();
                            reader.onloadend = () => resolve(reader.result.split(',')[1]);
                            reader.readAsDataURL(blob);
                        }).catch(reject);
                });
            """)
            if audio_b64:
                audio = base64.b64decode(audio_b64)
                log.info(f"BrowserConvert: blob OK {len(audio):,} bytes")
                return audio
        except Exception as e:
            log.error(f"BrowserConvert: blob error: {e}")
        return None
