"""
Skill: register — Đăng ký ElevenLabs.

Flow đơn giản nhất (như người dùng thật):
  1. Mở Chrome (Gmail đã login) → vào elevenlabs.io sign-in
  2. Click "Sign in with Google" → popup Google chọn TK
  3. Chọn TK → popup đóng → về ElevenLabs
  4. Qua onboarding → XONG

Dùng Chromium (browser) để bắt popup Google qua tab_ids.
"""

import time
import os
import random
import string
from typing import Tuple

from utils.logger import log


class Register:
    """Đăng ký TK ElevenLabs."""

    SIGNIN_URL = "https://elevenlabs.io/app/sign-in"

    def __init__(self, stealth, gmail_login=None):
        self.stealth = stealth
        self.gmail = gmail_login

    def register(self, profile_id: str, proxy: str = None,
                 method: str = "google", timeout: int = 120) -> Tuple[bool, str]:
        """Đăng ký ElevenLabs.

        Args:
            method: "google" (OAuth popup) hoặc "direct" (email+pass, verify qua Gmail)
        """
        if method == "direct":
            return self._register_direct(profile_id, proxy, timeout)
        return self._register_google(profile_id, proxy, timeout)

    def _register_google(self, profile_id: str, proxy: str = None,
                         timeout: int = 120) -> Tuple[bool, str]:
        """Đăng ký bằng Google OAuth (popup). Tự retry nếu lỗi."""
        profile = self.stealth.get_profile(profile_id)
        if not profile:
            return False, "Profile not found"
        if profile.get("elevenlabs_registered"):
            return True, "Already registered"
        if not profile.get("gmail_logged_in"):
            return False, "Gmail not logged in"

        email = profile.get("email", "")
        totp_secret = self._get_totp_for_email(email)

        # Check Gmail đã login chưa — nếu chưa thì login lại
        self._ensure_gmail_logged_in(profile_id, email, totp_secret, proxy)

        # Retry tối đa 3 lần
        for attempt in range(3):
            browser = None
            try:
                browser, tab = self.stealth.open_browser(profile_id, proxy=proxy)

                # 1. Vào ElevenLabs
                log.info(f"Register: [{attempt+1}/3] vào ElevenLabs... {email}")
                tab.get(self.SIGNIN_URL)
                time.sleep(5)

                if self._is_logged_in(tab):
                    self.stealth.mark_elevenlabs_registered(profile_id)
                    return True, "Already logged in"

                # 2. Click Sign in with Google
                google_btn = None
                for _ in range(3):
                    google_btn = self._find_google_button(tab)
                    if google_btn:
                        break
                    time.sleep(5)

                if not google_btn:
                    log.warning("Register: Google button not found, retry...")
                    continue

                google_btn.click()
                log.info("Register: clicked Sign in with Google")

                # 3. Xử lý popup Google
                google_tab = self._wait_for_google_tab(browser, timeout=20)
                if google_tab:
                    self._handle_full_google_flow(google_tab, browser, email, totp_secret)

                # 4. Sau popup đóng → kiểm tra kết quả
                time.sleep(5)
                try:
                    tab = self._find_elevenlabs_tab(browser) or tab
                    url = (tab.url or '').lower()

                    # Đã login?
                    if self._is_logged_in(tab):
                        self._do_onboarding(tab, email)
                        self.stealth.mark_elevenlabs_registered(profile_id)
                        log.info(f"Register: OK - {email}")
                        return True, "OK"

                    # Onboarding?
                    if 'onboarding' in url:
                        self._do_onboarding(tab, email)
                        if self._is_logged_in(tab):
                            self.stealth.mark_elevenlabs_registered(profile_id)
                            log.info(f"Register: OK (onboarding) - {email}")
                            return True, "OK"

                    # Vẫn ở sign-in → thử reload
                    if 'sign-in' in url:
                        log.info("Register: vẫn ở sign-in, reload...")
                        tab.get('https://elevenlabs.io/app/speech-synthesis')
                        time.sleep(8)
                        url = (tab.url or '').lower()

                        if self._is_logged_in(tab):
                            self._do_onboarding(tab, email)
                            self.stealth.mark_elevenlabs_registered(profile_id)
                            log.info(f"Register: OK (reload) - {email}")
                            return True, "OK"

                        if 'onboarding' in url:
                            self._do_onboarding(tab, email)
                            if self._is_logged_in(tab):
                                self.stealth.mark_elevenlabs_registered(profile_id)
                                log.info(f"Register: OK (reload+onboarding) - {email}")
                                return True, "OK"

                        # Vẫn chưa → retry lần tiếp
                        log.warning(f"Register: lỗi ĐK, retry... URL={url[:50]}")

                except Exception as e:
                    log.warning(f"Register: verify error: {str(e)[:40]}")

            except Exception as e:
                log.warning(f"Register: attempt {attempt+1} error: {str(e)[:40]}")
            finally:
                if browser:
                    try:
                        browser.quit()
                    except Exception:
                        pass

            # Delay trước retry
            time.sleep(5)

        self.stealth.cleanup(profile_id)
        return False, f"Failed after 3 attempts"

    def _register_direct(self, profile_id: str, proxy: str = None,
                         timeout: int = 120) -> Tuple[bool, str]:
        """Đăng ký bằng email+password trực tiếp.

        Flow:
        1. Vào sign-up → nhập email+pass → click Sign Up
        2. ElevenLabs gửi email verify → mở Gmail → click link
        3. Onboarding → XONG
        """
        page = None
        try:
            profile = self.stealth.get_profile(profile_id)
            if not profile:
                return False, "Profile not found"
            if profile.get("elevenlabs_registered"):
                return True, "Already registered"

            email = profile.get("email", "")
            if not email:
                return False, "No email in profile"

            page = self.stealth.open(profile_id, proxy=proxy)

            # 1. Vào sign-up
            log.info(f"Register: vào sign-up... {email}")
            page.get(self.SIGNUP_URL)
            time.sleep(5)

            if self._is_logged_in(page):
                self.stealth.mark_elevenlabs_registered(profile_id)
                return True, "Already logged in"

            # 2. Nhập email (data-testid="sign-up-email-input")
            email_input = page.ele('[data-testid="sign-up-email-input"]', timeout=5)
            if not email_input:
                email_input = page.ele('input[type="email"]', timeout=3)
            if not email_input:
                return False, "Email input not found"

            email_input.click()
            time.sleep(0.3)
            email_input.input(email)
            log.info(f"Register: nhập email {email}")
            time.sleep(0.5)

            # 3. Nhập password (dùng email làm password hoặc random)
            password = email.split('@')[0] + "Aa1!"  # simple password
            pass_input = page.ele('[data-testid="sign-up-password-input"]', timeout=3)
            if not pass_input:
                pass_input = page.ele('input[type="password"]', timeout=3)
            if pass_input:
                pass_input.click()
                time.sleep(0.3)
                pass_input.input(password)
                log.info("Register: nhập password")
                time.sleep(0.5)

            # 4. Click Sign Up / Create account
            submit = page.ele('[data-testid="sign-in-submit-button"]', timeout=3)
            if not submit:
                submit = page.ele('tag:button@@text():Sign up', timeout=3)
            if not submit:
                submit = page.ele('tag:button@@text():Create', timeout=3)
            if submit:
                submit.click()
                log.info("Register: clicked Sign Up")
                time.sleep(5)

            # 5. Đợi verify email
            log.info("Register: đợi email verify...")
            # Cần mở Gmail tab → tìm email ElevenLabs → click link
            if self.gmail:
                otp_or_link = self._verify_via_gmail(profile_id, proxy)
                if not otp_or_link:
                    return False, "Email verification failed"

            # 6. Email Verification popup → Continue
            self._click_continue(page)
            time.sleep(3)

            # 7. Onboarding
            self._do_onboarding(page)

            # 8. Verify
            for i in range(timeout):
                if self._is_logged_in(page):
                    self.stealth.mark_elevenlabs_registered(profile_id)
                    log.info(f"Register: OK - {profile_id}")
                    return True, "OK"
                time.sleep(1)

            return False, f"Timeout URL={page.url[:60]}"

        except Exception as e:
            log.error(f"Register: {e}")
            return False, str(e)
        finally:
            if page:
                try:
                    page.quit()
                except Exception:
                    pass
                self.stealth.cleanup(profile_id)

    def _verify_via_gmail(self, profile_id: str, proxy: str = None) -> bool:
        """Mở Gmail → tìm email ElevenLabs → click link verify."""
        if not self.gmail:
            return False

        # Đọc link verify từ Gmail
        # ElevenLabs gửi email với link verify, không phải OTP
        page = None
        try:
            page = self.stealth.open(profile_id, proxy=proxy)
            page.get("https://mail.google.com")
            time.sleep(5)

            # Đợi email đến (max 60s)
            for i in range(12):
                try:
                    # Tìm email từ ElevenLabs
                    el_email = page.ele('text:ElevenLabs', timeout=3)
                    if not el_email:
                        el_email = page.ele('text:elevenlabs', timeout=2)
                    if not el_email:
                        el_email = page.ele('text:Verify', timeout=2)

                    if el_email:
                        el_email.click()
                        time.sleep(3)

                        # Tìm link verify trong email
                        verify_link = page.ele('text=Verify', timeout=3)
                        if not verify_link:
                            verify_link = page.ele('tag:a@@text():verify', timeout=3)
                        if not verify_link:
                            verify_link = page.ele('tag:a@@text():Confirm', timeout=3)

                        if verify_link:
                            verify_link.click()
                            log.info("Register: clicked verify link in Gmail")
                            time.sleep(5)
                            return True
                except Exception:
                    pass

                time.sleep(5)
                page.get("https://mail.google.com")
                time.sleep(3)

            log.warning("Register: email verify not found after 60s")
            return False
        except Exception as e:
            log.error(f"Register verify Gmail: {e}")
            return False
        finally:
            if page:
                try:
                    page.quit()
                except Exception:
                    pass

    # ============================================================
    # TÌM POPUP GOOGLE
    # ============================================================

    def _wait_for_google_tab(self, browser, timeout=30):
        """Scan tất cả tabs tìm accounts.google.com."""
        for i in range(timeout):
            for tid in browser.tab_ids:
                try:
                    t = browser.get_tab(tid)
                    if 'accounts.google.com' in (t.url or '').lower():
                        return t
                except Exception:
                    pass
            time.sleep(1)
        return None

    def _handle_full_google_flow(self, popup, browser, email: str,
                                totp_secret: str = ""):
        """Xử lý toàn bộ Google popup: chọn TK → 2FA → consent → đợi đóng."""
        import json
        account_clicked = False
        totp_entered = False
        consent_attempts = 0
        MAX_CONSENT_ATTEMPTS = 5  # Không loop consent quá 5 lần

        for i in range(30):
            # Popup đã đóng?
            try:
                if popup.tab_id not in browser.tab_ids:
                    log.info("Register: popup đã đóng")
                    return
                url = popup.url.lower()
            except Exception:
                log.info("Register: popup đóng (exception)")
                return

            # Redirect về ElevenLabs → xong
            if 'elevenlabs.io' in url:
                log.info("Register: popup redirect → ElevenLabs")
                return

            # Chỉ xử lý khi ở accounts.google.com
            if 'accounts.google.com' not in url:
                time.sleep(2)
                continue

            # === DETECT bằng element trên page (không dựa URL) ===
            page_info = popup.run_js('''
                var result = {};
                // Account list?
                result.hasAccountList = document.querySelectorAll("li").length > 0
                    && document.body.innerText.includes("@");
                // Password input?
                result.hasPassword = !!document.querySelector('input[type="password"]');
                // OTP/2FA input?
                result.hasOTP = !!document.querySelector('input[type="tel"]')
                    || !!document.querySelector('#totpPin');
                // 2FA text?
                var t = document.body.innerText.toLowerCase();
                result.has2FAText = t.includes('authenticator') || t.includes('enter code')
                    || t.includes('verify it') || t.includes('verification');
                // Consent buttons?
                result.hasConsent = t.includes('continue to elevenlabs')
                    || t.includes('lanjutkan ke elevenlabs')
                    || t.includes('elevenlabs.io');
                // Get visible buttons
                var btns = [];
                document.querySelectorAll("button").forEach(function(b) {
                    if (b.offsetHeight > 0) btns.push(b.textContent.trim().substring(0, 30));
                });
                result.buttons = btns;
                return JSON.stringify(result);
            ''')

            import json
            try:
                info = json.loads(page_info)
            except Exception:
                time.sleep(2)
                continue

            log.info(f"Register: popup detect: {page_info[:100]}")

            # 1. Account chooser → click TK
            if info.get("hasAccountList") and not account_clicked:
                time.sleep(2)
                self._select_google_account(popup, email)
                account_clicked = True
                time.sleep(3)
                continue

            # 2. Password → nhập password
            if info.get("hasPassword"):
                log.info("Register: popup yêu cầu password!")
                self._handle_popup_password(popup, email)
                time.sleep(5)
                continue

            # 3. 2FA/OTP → nhập OTP
            if (info.get("hasOTP") or info.get("has2FAText")) and totp_secret:
                log.info("Register: popup yêu cầu 2FA!")
                self._handle_popup_2fa(popup, totp_secret)
                time.sleep(5)
                continue

            # 4. Consent → click Continue/Allow (LỌC Cancel)
            consent_attempts += 1
            if consent_attempts <= MAX_CONSENT_ATTEMPTS:
                clicked = popup.run_js('''
                    var skip = ["cancel", "cancelar", "hủy", "huỷ", "back", "quay",
                                "deny", "refuse", "từ chối", "more ways", "cara lain"];
                    var btns = document.querySelectorAll("button");
                    for (var btn of btns) {
                        var rect = btn.getBoundingClientRect();
                        var txt = (btn.textContent || "").trim().toLowerCase();
                        var isSkip = false;
                        for (var s of skip) { if (txt.includes(s)) isSkip = true; }
                        if (isSkip) continue;
                        if (rect.height > 20 && rect.height < 60 && rect.y > 200 && txt.length > 0) {
                            btn.click();
                            return "clicked: " + txt.substring(0, 30);
                        }
                    }
                    return "";
                ''')
                if clicked:
                    log.info(f"Register: popup click: {clicked}")
                time.sleep(3)
                continue
            else:
                log.warning(f"Register: popup stuck {consent_attempts}x, bỏ qua")
                return

        log.warning("Register: popup timeout 30 iterations")

    def _handle_popup_password(self, popup, email: str):
        """Nhập password trong popup Google."""
        try:
            # Đọc password từ gmail.txt
            password = ""
            import os
            gmail_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config", "gmail.txt")
            if os.path.exists(gmail_file):
                with open(gmail_file, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        parts = line.strip().split('|')
                        if len(parts) >= 2 and parts[0].strip().lower() == email.lower():
                            password = parts[1].strip()
                            break

            if not password:
                log.warning(f"Register: không tìm password cho {email}")
                return

            # Copy password → Ctrl+V → Enter
            try:
                import pyperclip
                pyperclip.copy(password)
            except ImportError:
                import subprocess
                subprocess.run(['clip'], input=password.encode(), check=True)

            from DrissionPage.common import Actions
            actions = Actions(popup)
            actions.key_down('ctrl').key_down('v').key_up('v').key_up('ctrl')
            time.sleep(0.5)
            actions.key_down('enter').key_up('enter')
            log.info("Register: password entered in popup")

        except Exception as e:
            log.warning(f"Register: popup password error: {e}")

    def _handle_popup_2fa(self, popup, totp_secret: str):
        """Xử lý 2FA trong popup Google.

        Page text mẫu: "Verify it's you ... Enter code ... [Next]"
        """
        try:
            import pyotp

            # Đợi OTP input xuất hiện
            otp_input = None
            for wait in range(15):
                try:
                    otp_input = popup.ele('input[type="tel"]', timeout=1)
                    if not otp_input:
                        otp_input = popup.ele('#totpPin', timeout=1)
                    if otp_input:
                        log.info(f"Register: OTP input ready ({wait+1}s)")
                        break
                except Exception:
                    pass

                # Click "Google Authenticator" nếu cần chọn method
                for sel in ['text:Google Authenticator', 'text:Authenticator']:
                    try:
                        el = popup.ele(sel, timeout=1)
                        if el:
                            el.click()
                            log.info("Register: clicked Authenticator option")
                            time.sleep(2)
                            break
                    except Exception:
                        pass

            time.sleep(2)

            # Generate OTP ngay trước khi nhập
            clean_secret = totp_secret.replace(" ", "").replace("-", "").upper()
            totp = pyotp.TOTP(clean_secret)
            otp_code = totp.now()
            log.info(f"Register: OTP = {otp_code}")

            # Click vào OTP input nếu tìm thấy
            if otp_input:
                try:
                    otp_input.click()
                    time.sleep(0.3)
                except Exception:
                    pass

            # Clipboard paste
            try:
                import pyperclip
                pyperclip.copy(otp_code)
            except ImportError:
                import subprocess
                subprocess.run(['clip'], input=otp_code.encode(), check=True)

            from DrissionPage.common import Actions
            actions = Actions(popup)
            actions.key_down('ctrl').key_down('v').key_up('v').key_up('ctrl')
            log.info("Register: OTP pasted")
            time.sleep(0.5)

            # Click "Next" button (Google dùng "Next" không phải Enter)
            next_clicked = False
            for sel in ['text=Next', 'text=Tiếp theo', 'text=Tiếp tục']:
                try:
                    btn = popup.ele(sel, timeout=2)
                    if btn:
                        btn.click()
                        log.info(f"Register: clicked {sel} after OTP")
                        next_clicked = True
                        break
                except Exception:
                    pass

            if not next_clicked:
                # Fallback: Enter key
                actions.key_down('enter').key_up('enter')
                log.info("Register: pressed Enter after OTP")

        except Exception as e:
            log.error(f"Register: popup 2FA error: {e}")

    def _cdp_click_button(self, tab, keywords: list):
        """Click nút bằng CDP (giống chuột thật)."""
        import json
        kw_js = json.dumps(keywords)
        coords = tab.run_js(f'''
            var keywords = {kw_js};
            var btns = document.querySelectorAll("button, [role=button], input[type=submit]");
            for (var btn of btns) {{
                var t = (btn.textContent || btn.value || "").toLowerCase().trim();
                for (var kw of keywords) {{
                    if (t.includes(kw)) {{
                        var rect = btn.getBoundingClientRect();
                        if (rect.height > 0) return JSON.stringify({{x: rect.x + rect.width/2, y: rect.y + rect.height/2}});
                    }}
                }}
            }}
            return "";
        ''')
        if coords:
            c = json.loads(coords)
            try:
                tab.run_cdp('Input.dispatchMouseEvent', type='mousePressed',
                            x=int(c['x']), y=int(c['y']), button='left', clickCount=1)
                time.sleep(0.1)
                tab.run_cdp('Input.dispatchMouseEvent', type='mouseReleased',
                            x=int(c['x']), y=int(c['y']), button='left', clickCount=1)
                log.info(f"Register: CDP click at ({c['x']:.0f},{c['y']:.0f})")
            except Exception as e:
                log.warning(f"Register: CDP click error: {e}")
        else:
            log.info("Register: no button found for keywords")

    def _select_google_account(self, popup, email: str):
        """Chọn TK Gmail trong popup Google.

        Cách 1: Click trực tiếp LI element
        Cách 2: Lấy href link từ LI → mở trong tab chính
        """
        time.sleep(3)

        email_short = email.split('@')[0] if email else ''

        # Lấy toạ độ LI element → click bằng CDP Input (thật nhất)
        coords = popup.run_js(f'''
            var lis = document.querySelectorAll("li");
            for (var li of lis) {{
                var t = li.textContent || "";
                if (t.includes("{email}") || t.includes("{email_short}")) {{
                    var rect = li.getBoundingClientRect();
                    return JSON.stringify({{x: rect.x + rect.width/2, y: rect.y + rect.height/2, w: rect.width, h: rect.height}});
                }}
            }}
            return "";
        ''')

        if coords:
            import json
            c = json.loads(coords)
            log.info(f"Register: LI tại x={c['x']:.0f} y={c['y']:.0f} ({c['w']:.0f}x{c['h']:.0f})")

            # CDP click — giống chuột thật, không bị chặn
            try:
                popup.run_cdp('Input.dispatchMouseEvent',
                              type='mousePressed', x=int(c['x']), y=int(c['y']),
                              button='left', clickCount=1)
                time.sleep(0.1)
                popup.run_cdp('Input.dispatchMouseEvent',
                              type='mouseReleased', x=int(c['x']), y=int(c['y']),
                              button='left', clickCount=1)
                log.info("Register: CDP click sent")
            except Exception as e:
                log.warning(f"Register: CDP click error: {e}")
                # Fallback: DrissionPage click
                try:
                    lis = popup.eles('tag:li', timeout=3)
                    for li in lis:
                        if email_short in (li.text or ''):
                            li.click(by_js=True)
                            log.info("Register: fallback JS click")
                            break
                except Exception:
                    pass
        else:
            log.warning("Register: cannot find account LI element")

        time.sleep(5)

    def _wait_popup_close(self, popup, browser, timeout=30):
        """Đợi popup Google đóng."""
        for i in range(timeout):
            try:
                if popup.tab_id not in browser.tab_ids:
                    log.info(f"Register: popup đóng ({i+1}s)")
                    return
            except Exception:
                log.info("Register: popup đã đóng")
                return
            time.sleep(1)

    # ============================================================
    # ONBOARDING
    # ============================================================

    def _do_onboarding(self, tab, email: str = ""):
        """Qua hết onboarding ElevenLabs.

        Flow (user đã demo):
        1. Choose platform (ElevenCreative đã chọn sẵn) → Continue
        2. Điền tên + tick 18+ → Next
        3. Skip → Skip → Skip → Skip
        → URL = /app/speech-synthesis = XONG
        """
        log.info("Register: onboarding...")

        for step in range(15):
            time.sleep(3)

            # Đã ra khỏi onboarding?
            url = (tab.url or '').lower()
            if 'onboarding' not in url and 'elevenlabs.io/app' in url:
                log.info(f"Register: onboarding XONG (step {step+1})")
                return
            if 'elevenlabs.io' not in url:
                return

            # Bước 2: Điền tên (dùng phần trước @ của email)
            try:
                inp = tab.ele('tag:input@@type=text', timeout=1)
                if not inp:
                    inp = tab.ele('tag:input:not([type=checkbox]):not([type=hidden])', timeout=1)
                if inp:
                    # Lấy email từ profile
                    _profile = self.stealth.get_profile(tab.url.split('/')[-1]) if hasattr(self, 'stealth') else None
                    # Dùng phần trước @ của email, hoặc random
                    name = email.split('@')[0] if email else ''.join(random.choices(string.ascii_lowercase, k=6)).capitalize()
                    inp.clear()
                    inp.input(name)
            except Exception:
                pass

            # Tick checkbox 18+ — nhiều cách
            try:
                # Cách 1: JS click
                tab.run_js("document.querySelectorAll('input[type=checkbox]').forEach(c=>{if(!c.checked)c.click()})")
            except Exception:
                pass
            try:
                # Cách 2: DrissionPage click label/checkbox
                cb = tab.ele('tag:input@@type=checkbox', timeout=1)
                if cb:
                    is_checked = cb.run_js('return this.checked')
                    if not is_checked:
                        cb.click()
                        log.info("Register: ticked checkbox 18+")
            except Exception:
                pass
            try:
                # Cách 3: Click text "18 years" hoặc "checking this box"
                for label_text in ['text:18 years', 'text:checking this box', 'text:legal']:
                    lbl = tab.ele(label_text, timeout=1)
                    if lbl:
                        lbl.click()
                        log.info("Register: clicked checkbox label")
                        break
            except Exception:
                pass

            # Click Continue / Next / Skip / Done / Get started
            clicked = False
            for sel in ['text=Continue', 'text=Next', 'text=Skip',
                        'text=Get started', 'text=Done']:
                try:
                    btn = tab.ele(sel, timeout=2)
                    if btn:
                        btn.click()
                        log.info(f"Register: onboarding click {sel} (step {step+1})")
                        clicked = True
                        time.sleep(1)
                        # Kiểm tra page có thay đổi không — nếu không → checkbox chưa tick
                        new_url = (tab.url or '').lower()
                        if 'onboarding' not in new_url and 'elevenlabs.io/app' in new_url:
                            return  # Đã qua
                        break
                except Exception:
                    pass

            if not clicked:
                log.info(f"Register: onboarding — no button found (step {step+1})")

        log.warning("Register: onboarding chưa xong sau 15 bước")

    # ============================================================
    # HELPERS
    # ============================================================

    def _ensure_gmail_logged_in(self, profile_id: str, email: str,
                                totp_secret: str, proxy: str = None):
        """Kiểm tra Gmail đã login chưa, nếu chưa thì login lại."""
        page = None
        try:
            page = self.stealth.open(profile_id, proxy=proxy)
            page.get("https://myaccount.google.com")
            time.sleep(5)
            url = (page.url or '').lower()

            if 'myaccount.google.com' in url and 'signin' not in url:
                log.info(f"Register: Gmail vẫn login OK - {email}")
                return

            # Gmail bị thoát → login lại
            log.warning(f"Register: Gmail bị thoát, login lại {email}...")
            page.quit()
            page = None

            from accounts.gmail import GmailLogin
            gmail = GmailLogin(self.stealth)

            # Đọc password từ gmail.txt
            gmail_password = ""
            import os
            gmail_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config", "gmail.txt")
            if os.path.exists(gmail_file):
                with open(gmail_file, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        parts = line.strip().split('|')
                        if len(parts) >= 2 and parts[0].strip().lower() == email.lower():
                            gmail_password = parts[1].strip()
                            break

            if gmail_password:
                ok = gmail.login(profile_id, email, gmail_password,
                                 totp_secret=totp_secret, proxy=proxy)
                if ok:
                    log.info(f"Register: Gmail re-login OK - {email}")
                else:
                    log.error(f"Register: Gmail re-login FAIL - {email}")
            else:
                log.error(f"Register: không tìm thấy password cho {email}")

        except Exception as e:
            log.warning(f"Register: check Gmail error: {str(e)[:40]}")
        finally:
            if page:
                try:
                    page.quit()
                except Exception:
                    pass

    def _get_totp_for_email(self, email: str) -> str:
        """Tìm TOTP secret cho email từ gmail.txt."""
        import os
        gmail_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "gmail.txt")
        if not os.path.exists(gmail_file):
            return ""
        try:
            with open(gmail_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    parts = line.strip().split('|')
                    if len(parts) >= 1 and parts[0].strip().lower() == email.lower():
                        return parts[2].strip() if len(parts) >= 3 else ""
        except Exception:
            pass
        return ""

    def _find_google_button(self, tab):
        for sel in ['text=Sign in with Google', 'text:Sign in with Google',
                    'text=Continue with Google']:
            try:
                btn = tab.ele(sel, timeout=3)
                if btn:
                    return btn
            except Exception:
                pass
        return None

    def _is_logged_in(self, tab):
        url = (tab.url or '').lower()
        return ('elevenlabs.io/app' in url
                and 'sign-in' not in url
                and 'sign-up' not in url
                and 'onboarding' not in url)

    def _find_elevenlabs_tab(self, browser):
        for tid in browser.tab_ids:
            try:
                t = browser.get_tab(tid)
                if 'elevenlabs.io' in (t.url or '').lower():
                    return t
            except Exception:
                pass
        return None

    def _screenshot(self, tab, name: str):
        try:
            ss_dir = os.path.join(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))), "output", "_debug")
            os.makedirs(ss_dir, exist_ok=True)
            tab.get_screenshot(os.path.join(ss_dir, f"{name}.png"))
        except Exception:
            pass

    def _click_continue(self, tab):
        for sel in ['text=Continue', 'text=Next', 'text=Skip',
                    'text=Get started', 'text=Done']:
            try:
                btn = tab.ele(sel, timeout=1)
                if btn:
                    btn.click()
                    log.info(f"Register: clicked {sel}")
                    time.sleep(1)
                    return
            except Exception:
                pass

    # ============================================================
    # BATCH
    # ============================================================

    def register_many(self, profile_ids: list, proxy: str = None,
                      delay: int = 15, on_progress=None) -> dict:
        results = {"success": [], "failed": []}
        for i, pid in enumerate(profile_ids):
            if on_progress:
                on_progress(f"[{i+1}/{len(profile_ids)}] {pid}")
            ok, msg = self.register(pid, proxy=proxy)
            (results["success"] if ok else results["failed"]).append(pid if ok else (pid, msg))
            if on_progress:
                on_progress(f"  {'OK' if ok else 'FAIL'}: {msg}")
            if i < len(profile_ids) - 1:
                time.sleep(delay)
        return results
