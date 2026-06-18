"""
Skill: voice — Thêm giọng vào thư viện + chọn voice + tùy chỉnh settings.

Flow:
  1. Vào voice-library?voiceId=xxx → click "Add to My Voices"
  2. Click "Use voice" → redirect trang TTS
  3. Tùy chỉnh: Model, Speed, Stability, Similarity, Output Format

Skill này sẽ dùng đi dùng lại, thay đổi nhiều → tách riêng để tinh chỉnh.

Cách dùng:
    v = Voice(stealth)
    v.add_to_library("tk001", "RGb96Dcl0k5eVje8EBch")
    v.set_model("tk001", "eleven_v3")
    v.set_speed("tk001", 1.0)
    v.set_stability("tk001", 0.5)
    v.set_similarity("tk001", 0.75)
    v.set_output_format("tk001", "MP3 (128kbps)")
"""

import os
import time
from typing import Optional, Tuple

from utils.logger import log


VOICE_LIBRARY_URL = "https://elevenlabs.io/app/voice-library?voiceId={voice_id}"
TTS_URL = "https://elevenlabs.io/app/speech-synthesis/text-to-speech"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADD_VOICE_ICON = os.path.join(PROJECT_ROOT, "icon", "addvoice.PNG")


class Voice:
    """Quản lý voice cho ElevenLabs."""

    def __init__(self, stealth):
        self.stealth = stealth

    # ============================================================
    # ADD VOICE TO LIBRARY + USE
    # ============================================================

    def add_and_use(self, profile_id: str, voice_id: str,
                    proxy: str = None) -> Tuple[bool, str]:
        """Thêm voice vào thư viện TK + chọn dùng.

        Flow:
        1. Vào voice-library?voiceId=xxx
        2. Click "Add to My Voices"
        3. Click "Use voice" → redirect trang TTS

        Returns: (success, message)
        """
        page = None
        try:
            page = self.stealth.open(profile_id, proxy=proxy)

            # 1. Vào voice library
            url = VOICE_LIBRARY_URL.format(voice_id=voice_id)
            log.info(f"Voice: vào {url[:60]}...")
            page.get(url)
            time.sleep(5)

            # 2. Tìm và click "Add to My Voices"
            add_btn = (page.ele('@aria-label=Add to My Voices', timeout=5) or
                       page.ele('@data-testid=voices-add-button', timeout=2))
            clicked_add = False
            popup_confirmed = False
            if add_btn:
                add_btn.click()
                log.info("Voice: clicked page button 'Add to My Voices'")
                clicked_add = True
                popup_confirmed = self._confirm_add_popup(page)
                time.sleep(3)
            else:
                log.info("Voice: 'Add' button not found, maybe already added")

            added_ok = self._wait_voice_added(
                page, clicked_add=clicked_add, popup_confirmed=popup_confirmed
            )
            log.info(f"Voice: verify added -> {added_ok}")
            if not added_ok:
                return False, "Voice not confirmed in library"

            # 3. Đợi "Use voice" xuất hiện → click
            use_btn = None
            for i in range(10):
                use_btn = (page.ele('@aria-label=Use voice', timeout=2) or
                           page.ele('@data-testid=voices-use-button', timeout=1))
                if use_btn:
                    break
                time.sleep(1)

            if use_btn:
                use_btn.click()
                log.info("Voice: clicked 'Use voice'")
                time.sleep(5)
            else:
                log.warning("Voice: 'Use voice' button not found")
                return False, "Use voice button not found"

            # 4. Verify redirect → trang TTS
            if 'speech-synthesis' in page.url.lower():
                log.info(f"Voice: OK → {page.url[:60]}")
                return True, "Voice added and selected"
            else:
                try:
                    page.get(TTS_URL)
                    time.sleep(5)
                except Exception:
                    pass
                if 'speech-synthesis' in page.url.lower():
                    log.info(f"Voice: forced TTS OK → {page.url[:60]}")
                    return True, "Voice added and selected"
                log.warning(f"Voice: unexpected URL: {page.url[:60]}")
                return False, f"Voice add/use unverified, URL: {page.url[:60]}"

        except Exception as e:
            log.error(f"Voice: {e}")
            return False, str(e)
        finally:
            if page:
                try:
                    page.quit()
                except Exception:
                    pass

    def add_and_use_on_page(self, page, voice_id: str) -> Tuple[bool, str]:
        """Dùng page hiện tại để add/use voice, không mở lại Chrome."""
        try:
            url = VOICE_LIBRARY_URL.format(voice_id=voice_id)
            log.info(f"Voice: vào {url[:60]}...")
            page.get(url)
            time.sleep(5)

            add_btn = (page.ele('@aria-label=Add to My Voices', timeout=5) or
                       page.ele('@data-testid=voices-add-button', timeout=2))
            clicked_add = False
            popup_confirmed = False
            if add_btn:
                add_btn.click()
                log.info("Voice: clicked page button 'Add to My Voices'")
                clicked_add = True
                popup_confirmed = self._confirm_add_popup(page)
                time.sleep(3)
            else:
                log.info("Voice: 'Add' button not found, maybe already added")

            added_ok = self._wait_voice_added(
                page, clicked_add=clicked_add, popup_confirmed=popup_confirmed
            )
            log.info(f"Voice: verify added -> {added_ok}")
            if not added_ok:
                return False, "Voice not confirmed in library"

            use_btn = None
            for _ in range(10):
                use_btn = (page.ele('@aria-label=Use voice', timeout=2) or
                           page.ele('@data-testid=voices-use-button', timeout=1))
                if use_btn:
                    break
                time.sleep(1)

            if not use_btn:
                log.warning("Voice: 'Use voice' button not found")
                return False, "Use voice button not found"

            use_btn.click()
            log.info("Voice: clicked 'Use voice'")
            time.sleep(5)

            if 'speech-synthesis' not in page.url.lower():
                page.get(TTS_URL)
                time.sleep(5)

            if 'speech-synthesis' in page.url.lower():
                log.info(f"Voice: OK -> {page.url[:60]}")
                return True, "Voice added and selected"

            log.warning(f"Voice: unexpected URL: {page.url[:60]}")
            return False, f"Voice add/use unverified, URL: {page.url[:60]}"
        except Exception as e:
            log.error(f"Voice: {e}")
            return False, str(e)

    def _wait_voice_added(self, page, clicked_add: bool = False,
                          popup_confirmed: bool = False) -> bool:
        """Xác nhận voice đã vào library thật trên trang voice-library."""
        for _ in range(18):
            if not popup_confirmed:
                popup_confirmed = self._confirm_add_popup(page) or popup_confirmed
            try:
                use_btn = (page.ele('@aria-label=Use voice', timeout=1) or
                           page.ele('@data-testid=voices-use-button', timeout=1))
                if use_btn:
                    return True
            except Exception:
                pass

            try:
                add_btn = (page.ele('@aria-label=Add to My Voices', timeout=1) or
                           page.ele('@data-testid=voices-add-button', timeout=1))
                if clicked_add and not popup_confirmed and not add_btn:
                    return True
            except Exception:
                pass

            try:
                added_text = (page.ele('text:Added', timeout=1) or
                              page.ele('text:Added to My Voices', timeout=1) or
                              page.ele('text:In your library', timeout=1))
                if added_text:
                    return True
            except Exception:
                pass

            time.sleep(1.5)
        return False

    def _confirm_add_popup(self, page) -> bool:
        """Nếu sau khi Add hiện popup chọn danh mục/folder thì bấm xác nhận."""
        clicked = False
        try:
            page.wait.load_start(timeout=1)
        except Exception:
            pass

        for _ in range(4):
            dialog = None
            try:
                dialog = (
                    page.ele('@role=dialog', timeout=1) or
                    page.ele('text:Add voice to My Voices', timeout=1)
                )
            except Exception:
                dialog = None

            scope = dialog or page

            try:
                # Một số account hiện popup cần chọn collection/folder trước khi nút Add voice khả dụng.
                choice = (
                    scope.ele('@role=option', timeout=1) or
                    scope.ele('@role=radio', timeout=1) or
                    scope.ele('@role=checkbox', timeout=1) or
                    scope.ele('@data-state=unchecked', timeout=1) or
                    scope.ele('@data-testid=collection-item', timeout=1) or
                    scope.ele('@data-testid=folder-item', timeout=1)
                )
                if choice and dialog:
                    try:
                        choice.click()
                        log.info("Voice: selected first add-popup target")
                        time.sleep(0.8)
                    except Exception:
                        pass
            except Exception:
                pass

            # Ưu tiên click theo ảnh ngay khi popup vừa hiện, không phụ thuộc selector.
            if dialog and self._click_add_popup_by_image():
                log.info("Voice: popup image-click succeeded")
                clicked = True
                time.sleep(1.2)
                break

            confirm_btn = None
            prefer_add_voice = []
            try:
                prefer_add_voice = [
                    scope.ele('tag:button@@text()=Add voice', timeout=1),
                    scope.ele('tag:button@@text():Add voice', timeout=1),
                    scope.ele('text:Add voice', timeout=1),
                ]
            except Exception:
                prefer_add_voice = []
            for btn in prefer_add_voice:
                if btn:
                    confirm_btn = btn
                    break

            if not confirm_btn:
                for sel in (
                    '@aria-label=Add voice',
                    '@data-testid=confirm-button',
                    '@data-testid=modal-confirm-button',
                    '@data-testid=save-button',
                    '@data-testid=submit-button',
                    '@type=submit',
                    '@aria-label=Save',
                    '@aria-label=Done',
                    '@aria-label=Confirm',
                    'text:Save',
                    'text:Done',
                    'text:Confirm',
                    'text:Add to folder',
                    'text:Add to collection',
                ):
                    try:
                        confirm_btn = scope.ele(sel, timeout=1)
                        if confirm_btn:
                            break
                    except Exception:
                        continue

            if confirm_btn:
                try:
                    txt = ""
                    try:
                        txt = (confirm_btn.text or "").strip()
                    except Exception:
                        pass
                    clicked_now = False
                    clicked_now = self._click_add_popup_by_image()
                    try:
                        if not clicked_now:
                            confirm_btn.click()
                            clicked_now = True
                    except Exception:
                        pass
                    if not clicked_now:
                        try:
                            page.run_js("""
                                const dialog = [...document.querySelectorAll('[role="dialog"]')].find(Boolean);
                                const root = dialog || document;
                                const btns = [...root.querySelectorAll('button')];
                                const btn = btns.find(b => (b.innerText || '').trim().toLowerCase() === 'add voice');
                                if (btn) {
                                    btn.click();
                                    return true;
                                }
                                return false;
                            """)
                            clicked_now = True
                        except Exception:
                            pass
                    if not clicked_now:
                        try:
                            page.run_js("""
                                const dialog = [...document.querySelectorAll('[role="dialog"]')].find(Boolean);
                                const root = dialog || document;
                                const btns = [...root.querySelectorAll('button')];
                                const btn = btns.find(b => {
                                    const txt = (b.innerText || '').trim().toLowerCase();
                                    const style = window.getComputedStyle(b);
                                    return txt === 'add voice' ||
                                           (txt.includes('add voice') &&
                                            (style.backgroundColor.includes('rgb(0') || style.color.includes('rgb(255')));
                                });
                                if (btn) {
                                    btn.dispatchEvent(new MouseEvent('click', {bubbles:true,cancelable:true,view:window}));
                                    return true;
                                }
                                return false;
                            """)
                            clicked_now = True
                        except Exception:
                            pass
                    if not clicked_now:
                        continue
                    label = txt or "Add voice"
                    log.info(f"Voice: clicked popup button '{label}'")
                    clicked = True
                    time.sleep(1.2)
                    break
                except Exception:
                    pass

            time.sleep(0.8)

        return clicked

    def _click_add_popup_by_image(self) -> bool:
        """Fallback: click nút Add voice bằng ảnh nếu selector/JS không ăn."""
        if not os.path.exists(ADD_VOICE_ICON):
            return False
        try:
            import pyautogui
        except Exception as exc:
            log.warning(f"Voice: image fallback unavailable: {exc}")
            return False

        deadline = time.time() + 8
        while time.time() < deadline:
            for confidence in (0.82, 0.80, 0.78, 0.75):
                try:
                    pos = pyautogui.locateCenterOnScreen(
                        ADD_VOICE_ICON,
                        confidence=confidence,
                        grayscale=False,
                    )
                except Exception:
                    pos = None
                if not pos:
                    continue
                try:
                    pyautogui.moveTo(pos.x, pos.y, duration=0.1)
                    pyautogui.click(pos.x, pos.y)
                    log.info(
                        f"Voice: clicked popup button by image -> addvoice.PNG (conf={confidence})"
                    )
                    time.sleep(1.2)
                    return True
                except Exception as exc:
                    log.warning(f"Voice: image click fail: {exc}")
            time.sleep(0.6)
        return False

    # ============================================================
    # SETTINGS — Tùy chỉnh voice trên trang TTS
    # ============================================================

    def configure(self, profile_id: str, voice_id: str = None,
                  model: str = None, speed: float = None,
                  stability: float = None, similarity: float = None,
                  output_format: str = None,
                  proxy: str = None) -> Tuple[bool, str]:
        """Tùy chỉnh voice settings trên trang TTS.

        Args:
            voice_id: Nếu có → add voice trước, rồi configure
            model: "eleven_v3", "eleven_flash_v2_5", etc.
            speed: 0.0 → 1.0 (slider)
            stability: 0.0 → 1.0 (slider)
            similarity: 0.0 → 1.0 (slider)
            output_format: "MP3 (128kbps)", "MP3 (192kbps)", etc.
        """
        page = None
        try:
            page = self.stealth.open(profile_id, proxy=proxy)

            # Add voice trước nếu cần
            if voice_id:
                url = VOICE_LIBRARY_URL.format(voice_id=voice_id)
                page.get(url)
                time.sleep(5)

                # Add
                add_btn = page.ele('[data-testid="voices-add-button"]', timeout=3)
                if add_btn:
                    add_btn.click()
                    time.sleep(2)

                # Use
                for _ in range(5):
                    use_btn = page.ele('[data-testid="voices-use-button"]', timeout=2)
                    if use_btn:
                        use_btn.click()
                        time.sleep(5)
                        break
                    time.sleep(1)

            # Đảm bảo ở trang TTS
            if 'speech-synthesis' not in page.url.lower():
                page.get(TTS_URL)
                time.sleep(5)

            # === Tùy chỉnh Model ===
            if model:
                self._set_model(page, model)

            # === Tùy chỉnh Speed ===
            if speed is not None:
                self._set_slider(page, "Speed", speed)

            # === Tùy chỉnh Stability ===
            if stability is not None:
                self._set_slider(page, "Stability", stability)

            # === Tùy chỉnh Similarity ===
            if similarity is not None:
                self._set_slider(page, "Similarity", similarity)

            # === Tùy chỉnh Output Format ===
            if output_format:
                self._set_output_format(page, output_format)

            log.info(f"Voice: configure OK (model={model}, speed={speed}, "
                     f"stability={stability}, similarity={similarity})")
            return True, "Configured"

        except Exception as e:
            log.error(f"Voice configure: {e}")
            return False, str(e)
        finally:
            if page:
                try:
                    page.quit()
                except Exception:
                    pass

    # ============================================================
    # INTERNAL — Điều chỉnh elements trên trang
    # ============================================================

    def _set_model(self, page, model_name: str):
        """Chọn model (click dropdown → chọn model)."""
        try:
            # Click vào model selector (hiện "Eleven Flash v2.5" etc.)
            model_btn = page.ele('text:Model', timeout=3)
            if not model_btn:
                model_btn = page.ele('text:Eleven', timeout=3)

            # Tìm button/div chứa tên model hiện tại
            model_selector = page.ele('text:Eleven Flash', timeout=2)
            if not model_selector:
                model_selector = page.ele('text:Eleven Multilingual', timeout=2)
            if not model_selector:
                # Tìm selector gần label "Model"
                model_selector = model_btn

            if model_selector:
                model_selector.click()
                time.sleep(2)

                # Chọn model trong dropdown
                model_map = {
                    "eleven_v3": "Eleven v3",
                    "eleven_flash_v2_5": "Eleven Flash v2.5",
                    "eleven_turbo_v2_5": "Eleven Turbo v2.5",
                    "eleven_multilingual_v2": "Eleven Multilingual v2",
                }
                display_name = model_map.get(model_name, model_name)

                option = page.ele(f'text={display_name}', timeout=3)
                if not option:
                    option = page.ele(f'text:{display_name}', timeout=2)
                if option:
                    option.click()
                    time.sleep(1)
                    log.info(f"Voice: model → {display_name}")
                else:
                    log.warning(f"Voice: model '{display_name}' not found in dropdown")
            else:
                log.warning("Voice: model selector not found")

        except Exception as e:
            log.warning(f"Voice: set model error: {e}")

    def _set_slider(self, page, label: str, value: float):
        """Set slider value (Speed/Stability/Similarity).

        value: 0.0 → 1.0 (trái → phải)
        """
        try:
            # Tìm slider bằng JS — reliable nhất
            result = page.run_js(f'''
                // Tìm label text
                var labels = document.querySelectorAll("*");
                for (var el of labels) {{
                    if (el.textContent.trim() === "{label}" && el.offsetHeight > 0 && el.offsetHeight < 40) {{
                        // Tìm slider (input[type=range]) gần label
                        var parent = el.parentElement;
                        for (var i = 0; i < 5; i++) {{
                            if (!parent) break;
                            var slider = parent.querySelector('input[type="range"]');
                            if (slider) {{
                                // Set value
                                var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                                    window.HTMLInputElement.prototype, "value").set;
                                nativeInputValueSetter.call(slider, {value});
                                slider.dispatchEvent(new Event("input", {{bubbles: true}}));
                                slider.dispatchEvent(new Event("change", {{bubbles: true}}));
                                return "set " + "{label}" + " = " + {value};
                            }}
                            parent = parent.parentElement;
                        }}
                    }}
                }}
                return "not found";
            ''')
            log.info(f"Voice: slider {label} → {result}")

        except Exception as e:
            log.warning(f"Voice: set slider {label} error: {e}")

    def _set_output_format(self, page, format_name: str):
        """Chọn output format (dropdown)."""
        try:
            # Tìm dropdown "Output Format"
            fmt_label = page.ele('text:Output Format', timeout=3)
            if fmt_label:
                # Click dropdown gần label
                parent = fmt_label
                for _ in range(5):
                    parent = parent.parent()
                    select = parent.ele('tag:select', timeout=1)
                    if select:
                        select.click()
                        time.sleep(1)
                        break
                    btn = parent.ele('tag:button', timeout=1)
                    if btn:
                        btn.click()
                        time.sleep(1)
                        break

                # Chọn option
                option = page.ele(f'text={format_name}', timeout=3)
                if option:
                    option.click()
                    log.info(f"Voice: format → {format_name}")
                else:
                    log.warning(f"Voice: format '{format_name}' not found")

        except Exception as e:
            log.warning(f"Voice: set output format error: {e}")

    # ============================================================
    # BATCH — Thêm voice cho nhiều TK
    # ============================================================

    def add_for_all(self, profile_ids: list, voice_id: str,
                    proxy: str = None, delay: int = 10,
                    on_progress=None) -> dict:
        """Thêm voice vào thư viện cho nhiều TK."""
        results = {"success": [], "failed": []}
        for i, pid in enumerate(profile_ids):
            if on_progress:
                on_progress(f"[{i+1}/{len(profile_ids)}] {pid}")
            ok, msg = self.add_and_use(pid, voice_id, proxy=proxy)
            if ok:
                results["success"].append(pid)
            else:
                results["failed"].append((pid, msg))
            if on_progress:
                on_progress(f"  {'OK' if ok else 'FAIL'}: {msg}")
            if i < len(profile_ids) - 1:
                time.sleep(delay)
        return results
