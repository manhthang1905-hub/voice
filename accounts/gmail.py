"""
Skill: gmail — Đăng nhập Gmail vào Chrome profile.

Bản copy sát 100% flow từ reference/google_login.py (đã test thực tế OK).
Login 1 lần, cookie lưu lại, tháng sau dùng lại không cần login.

Cách dùng:
    stealth = Stealth()
    gmail = GmailLogin(stealth)
    ok = gmail.login("tk001", "email@gmail.com", "pass", totp_secret="...")
"""

import time
import json
import re
from typing import Optional

from utils.logger import log


class GmailLogin:
    """Đăng nhập Gmail vào Chrome profile."""

    GOOGLE_LOGIN_URL = "https://accounts.google.com/signin"
    MYACCOUNT_URL = "https://myaccount.google.com"
    GMAIL_URL = "https://mail.google.com"

    def __init__(self, stealth):
        self.stealth = stealth

    # ============================================================
    # LOGIN — copy sát google_login.py
    # ============================================================

    def login(self, profile_id: str, email: str, password: str,
              totp_secret: str = "", proxy: str = None,
              headless: bool = False, timeout: int = 60) -> bool:
        """Đăng nhập Gmail. Flow copy từ google_login.py đã test OK."""
        page = None
        try:
            self.stealth.create(profile_id, email=email)
            page = self.stealth.open(profile_id, proxy=proxy, headless=headless)

            # KHÔNG inject fingerprint qua CDP cho Google login
            # (Google detect JS override navigator/screen → block login)
            # Fingerprint chỉ dùng qua Chrome args (user-agent, window-size) — đã set trong stealth.open()

            # Vào trang login
            log.info(f"Gmail: navigating to login... {email}")
            page.get(self.GOOGLE_LOGIN_URL)
            time.sleep(3)

            # Đã login sẵn?
            if "myaccount.google.com" in page.url or "google.com/search" in page.url:
                log.info(f"Gmail: already logged in! {email}")
                self.stealth.mark_gmail_logged_in(profile_id)
                return True

            # === BƯỚC 1: ĐIỀN EMAIL (y hệt google_login.py) ===
            log.info("Gmail: finding email input...")
            email_input = page.ele('#identifierId', timeout=5)
            if not email_input:
                email_input = page.ele('input[type="email"]', timeout=3)

            if not email_input:
                log.error("Gmail: email input not found!")
                return False

            email_input.click()
            time.sleep(0.3)

            # JS set value + trigger events (đã test OK với bare Chrome)
            email_input.run_js(f'''
                this.value = "{email}";
                this.dispatchEvent(new Event('input', {{bubbles: true}}));
                this.dispatchEvent(new Event('change', {{bubbles: true}}));
            ''')
            log.info(f"Gmail: email filled via JS")
            time.sleep(0.5)

            # Click Next button — thử nhiều cách
            log.info("Gmail: clicking Next button...")
            clicked_next = False
            # Cách 1: ID chính xác (Google cũ)
            for sel in ['#identifierNext', '#identifierNext button',
                        'button:contains("Next")', 'button:contains("Tiếp theo")',
                        'button:contains("Tiếp tục")',
                        'text=Next', 'text=Tiếp theo', 'text=Tiếp tục']:
                try:
                    btn = page.ele(sel, timeout=1)
                    if btn:
                        btn.click()
                        log.info(f"Gmail: clicked Next ({sel})")
                        clicked_next = True
                        break
                except Exception:
                    pass

            # Cách 2: Tìm tất cả button, click cái có text Next
            if not clicked_next:
                try:
                    buttons = page.eles('tag:button', timeout=2)
                    for btn in buttons:
                        txt = (btn.text or '').strip().lower()
                        if txt in ('next', 'tiếp theo', 'tiếp tục', 'tiep theo'):
                            btn.click()
                            log.info(f"Gmail: clicked Next button (text='{txt}')")
                            clicked_next = True
                            break
                except Exception:
                    pass

            # Cách 3: JS click (giống cách google_login.py dùng Enter)
            if not clicked_next:
                try:
                    from DrissionPage.common import Actions
                    actions_email = Actions(page)
                    actions_email.key_down('enter').key_up('enter')
                    log.info("Gmail: pressed Enter via Actions")
                    clicked_next = True
                except Exception:
                    email_input.input('\n')
                    log.info("Gmail: pressed Enter via input")

            # Screenshot sau khi click Next (debug)
            try:
                import os
                ss_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__)))), "output", "_debug")
                os.makedirs(ss_dir, exist_ok=True)
                page.get_screenshot(os.path.join(ss_dir, f"after_next_{profile_id}.png"))
                log.info(f"Gmail: screenshot saved, URL={page.url[:80]}")
            except Exception as e:
                log.warning(f"Gmail: screenshot error: {e}")

            # Đợi chuyển sang trang password
            log.info("Gmail: waiting for password page...")
            for _wait in range(20):
                url = page.url.lower()
                if 'challenge/pwd' in url or 'challenge/password' in url:
                    log.info(f"Gmail: password page loaded ({_wait+1}s)")
                    break
                if 'signin/rejected' in url or 'myaccount' in url:
                    log.info(f"Gmail: page redirected: {url[:60]}")
                    break
                time.sleep(1)

                # Check CAPTCHA — nếu có thì thử giải
                try:
                    page_text = page.run_js('return document.body.innerText.substring(0, 200)') or ''
                    if 'type the text' in page_text.lower():
                        log.warning("Gmail: CAPTCHA detected! Trying to solve...")
                        solved = self._solve_captcha(page)
                        if solved:
                            time.sleep(3)
                            break
                        else:
                            log.error("Gmail: CAPTCHA solve FAIL — cần đổi IP")
                            return False
                except Exception:
                    pass
            else:
                log.warning("Gmail: password page NOT detected after 20s, continuing...")
                time.sleep(2)

            # Cho page render đầy đủ
            log.info("Gmail: waiting 5s for page to fully render...")
            time.sleep(5)

            # === BƯỚC 2: ĐIỀN PASSWORD (Ctrl+V — y hệt google_login.py) ===
            log.info("Gmail: waiting for password input...")

            # JS detect password input (reliable hơn selector)
            JS_FIND_PW = '''
                var el = document.querySelector('input[type="password"]');
                if (el) { el.focus(); el.click(); return true; }
                el = document.querySelector('input[name="Passwd"]');
                if (el) { el.focus(); el.click(); return true; }
                el = document.querySelector('input[autocomplete="current-password"]');
                if (el) { el.focus(); el.click(); return true; }
                el = document.querySelector('input[aria-label*="assword"]');
                if (el) { el.focus(); el.click(); return true; }
                var div = document.querySelector('#password');
                if (div) { el = div.querySelector('input'); if (el) { el.focus(); el.click(); return true; } }
                var inputs = document.querySelectorAll('input');
                for (var i = 0; i < inputs.length; i++) {
                    if (inputs[i].type === 'password') { inputs[i].focus(); inputs[i].click(); return true; }
                }
                return false;
            '''

            pw_found_js = False
            for pw_wait in range(15):
                try:
                    result = page.run_js(JS_FIND_PW)
                    if result:
                        log.info(f"Gmail: password input found via JS ({pw_wait+1}s)")
                        pw_found_js = True
                        break
                except Exception:
                    pass
                time.sleep(1)

            if not pw_found_js:
                for sel in ['input[type="password"]', 'input[name="Passwd"]']:
                    try:
                        pw_input = page.ele(sel, timeout=1)
                        if pw_input:
                            pw_input.click()
                            time.sleep(0.3)
                            pw_found_js = True
                            log.info(f"Gmail: password input found: {sel}")
                            break
                    except Exception:
                        pass

            if not pw_found_js:
                log.warning(f"Gmail: password input NOT FOUND after 15s!")
                log.warning(f"Gmail: current URL: {page.url[:80]}")

            # Copy password → clipboard → Ctrl+V
            self._clipboard_copy(password)

            if pw_found_js:
                time.sleep(0.3)

            from DrissionPage.common import Actions
            actions = Actions(page)
            actions.key_down('ctrl').key_down('v').key_up('v').key_up('ctrl')
            log.info("Gmail: sent Ctrl+V")
            time.sleep(0.5)

            # Enter submit
            actions.key_down('enter').key_up('enter')
            log.info("Gmail: pressed Enter")

            # === BƯỚC 2.5: ĐỢI PASSWORD ĐƯỢC CHẤP NHẬN ===
            log.info("Gmail: waiting for password to be accepted...")
            url_before = page.url.lower()
            pwd_accepted = False
            for _wait in range(15):
                current = page.url.lower()
                if current != url_before:
                    log.info(f"Gmail: page transitioned ({_wait+1}s)")
                    pwd_accepted = True
                    break
                if 'myaccount' in current or 'signin/rejected' in current:
                    pwd_accepted = True
                    break
                time.sleep(1)
            if not pwd_accepted:
                log.warning(f"Gmail: password may not have been accepted (URL unchanged 15s)")
                log.warning(f"Gmail: URL: {page.url[:80]}")
            time.sleep(1)

            # Kiểm tra cần "Try another way" không
            self._handle_try_another_way(page)

            # === BƯỚC 3: 2FA (nếu có) ===
            if totp_secret:
                self._handle_2fa(page, totp_secret)

            # === BƯỚC 4: VERIFY LOGIN ===
            log.info("Gmail: waiting for redirect after login...")
            login_success = False
            for wait_i in range(timeout):
                url = page.url.lower()
                if "myaccount.google.com" in url or \
                   "google.com/search" in url or \
                   "labs.google" in url:
                    login_success = True
                    log.info(f"Gmail: LOGIN SUCCESS - redirected ({wait_i+1}s)")
                    break
                if "accounts.google.com" in url and \
                   "/signin" not in url and "/challenge" not in url:
                    login_success = True
                    log.info(f"Gmail: LOGIN SUCCESS - left login page ({wait_i+1}s)")
                    break

                # Xử lý: speedbump / passkeys / "Not now"
                if 'speedbump' in url or 'passkey' in url or 'webreauth' in url or 'signinoptions' in url:
                    # Click bằng JS — tìm button có text phù hợp (không phải link/span)
                    clicked_js = page.run_js('''
                        var skip = ["not now", "skip", "no thanks", "dismiss",
                                    "không phải bây giờ", "bỏ qua", "để sau"];
                        var btns = document.querySelectorAll("button");
                        for (var btn of btns) {
                            var t = (btn.textContent || "").trim().toLowerCase();
                            for (var s of skip) {
                                if (t === s || t.includes(s)) {
                                    btn.click();
                                    return "clicked: " + t;
                                }
                            }
                        }
                        return "";
                    ''')
                    if clicked_js:
                        log.info(f"Gmail: {clicked_js}")
                        time.sleep(3)

                if (wait_i + 1) % 10 == 0:
                    log.info(f"Gmail: still waiting... ({wait_i+1}s) URL={url[:50]}")
                time.sleep(1)

            if not login_success:
                log.error(f"Gmail: LOGIN FAILED - still on login page after {timeout}s")
                return False

            log.info("Gmail: page redirected, waiting 5s for stability...")
            time.sleep(5)

            # Verify qua myaccount.google.com
            log.info("Gmail: verifying via myaccount.google.com...")
            try:
                page.get(self.MYACCOUNT_URL)
                time.sleep(3)
                if 'accounts.google.com' in page.url.lower() and 'signin' in page.url.lower():
                    log.error("Gmail: VERIFY FAILED - redirect to login!")
                    return False

                logged_email = page.run_js("""
                    var els = document.querySelectorAll('[data-email]');
                    if (els.length > 0) return els[0].getAttribute('data-email');
                    var all = document.querySelectorAll('header *');
                    for (var i = 0; i < all.length; i++) {
                        var t = all[i].textContent.trim();
                        if (t.indexOf('@') > 0 && t.indexOf('.') > 0 && t.length < 60) return t;
                    }
                    var btns = document.querySelectorAll('[aria-label*="@"]');
                    if (btns.length > 0) {
                        var label = btns[0].getAttribute('aria-label');
                        var match = label.match(/[\\w.-]+@[\\w.-]+/);
                        if (match) return match[0];
                    }
                    return '';
                """)
                logged_email = str(logged_email or '').strip().lower()
                if logged_email:
                    if logged_email == email.lower():
                        log.info(f"Gmail: VERIFY OK - correct account: {logged_email}")
                    else:
                        log.warning(f"Gmail: VERIFY WARN - different account: {logged_email}")
                else:
                    log.info("Gmail: VERIFY OK - logged in but cannot read email")
            except Exception as e:
                log.warning(f"Gmail: verify error (non-critical): {e}")

            self.stealth.mark_gmail_logged_in(profile_id)
            return True

        except Exception as e:
            log.error(f"Gmail login error: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            if page:
                try:
                    page.quit()
                except Exception:
                    pass
                self.stealth.cleanup(profile_id)

    # ============================================================
    # HELPERS
    # ============================================================

    def _solve_captcha(self, page) -> bool:
        """Giải Google CAPTCHA bằng ddddocr (miễn phí, local)."""
        try:
            import ddddocr
            import base64

            # Lấy ảnh CAPTCHA từ page
            img_b64 = page.run_js('''
                var imgs = document.querySelectorAll('img');
                for (var img of imgs) {
                    if (img.width > 100 && img.height > 30 && img.height < 120 && img.src) {
                        var canvas = document.createElement('canvas');
                        canvas.width = img.naturalWidth || img.width;
                        canvas.height = img.naturalHeight || img.height;
                        var ctx = canvas.getContext('2d');
                        ctx.drawImage(img, 0, 0);
                        return canvas.toDataURL('image/png').split(',')[1];
                    }
                }
                return '';
            ''')

            if not img_b64:
                log.warning("Gmail: cannot extract CAPTCHA image")
                return False

            img_bytes = base64.b64decode(img_b64)

            # Giải bằng ddddocr
            ocr = ddddocr.DdddOcr(show_ad=False)
            solution = ocr.classification(img_bytes)
            log.info(f"Gmail: CAPTCHA solution = '{solution}'")

            if not solution:
                return False

            # Nhập solution
            captcha_input = (page.ele('input[type="text"]', timeout=3) or
                             page.ele('input[aria-label*="Type"]', timeout=2))
            if captcha_input:
                captcha_input.clear()
                captcha_input.input(solution)
                time.sleep(0.5)
                page.ele('#identifierNext', timeout=3).click()
                time.sleep(5)

                # Check passed
                if 'challenge/pwd' in page.url.lower() or 'identifier' not in page.url.lower():
                    log.info("Gmail: CAPTCHA PASSED!")
                    return True
                else:
                    log.warning("Gmail: CAPTCHA solution rejected")
                    return False

            return False

        except ImportError:
            log.warning("Gmail: ddddocr not installed (pip install ddddocr)")
            return False
        except Exception as e:
            log.warning(f"Gmail: CAPTCHA solve error: {e}")
            return False

    def _clipboard_copy(self, text: str):
        """Copy text vào clipboard."""
        try:
            import pyperclip
            pyperclip.copy(text)
        except ImportError:
            import subprocess
            subprocess.run(['clip'], input=text.encode(), check=True)

    def _inject_fingerprint_cdp(self, page, fp: dict):
        """Inject fingerprint qua CDP trước khi navigate."""
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
            fp.get("platform", "Win32"),
            json.dumps(fp.get("language", "en-US,en").split(",")),
            fp.get("screen_width", 1920),
            fp.get("screen_height", 1080),
        )
        try:
            page.run_cdp('Page.addScriptToEvaluateOnNewDocument', source=js)
            log.info("Gmail: CDP fingerprint inject OK")
        except Exception:
            try:
                page.run_js(js)
                log.info("Gmail: fallback JS fingerprint inject")
            except Exception:
                pass

    def _handle_try_another_way(self, page):
        """Click 'Try another way' nếu cần."""
        auth_selectors = [
            'text:Google Authenticator',
            'text:Ứng dụng xác thực',
            'text:Authenticator app',
            'text:Use your authenticator app',
            'text:Dùng ứng dụng xác thực',
        ]
        found_2fa = False
        for sel in auth_selectors:
            try:
                el = page.ele(sel, timeout=1)
                if el:
                    log.info(f"Gmail: found 2FA option directly: {sel}")
                    found_2fa = True
                    break
            except Exception:
                pass

        if not found_2fa:
            for sel in ['button:contains("Try another way")', 'button:contains("Thử cách khác")',
                        'text:Try another way', 'text:Thử cách khác']:
                try:
                    btn = page.ele(sel, timeout=2)
                    if btn:
                        btn.click()
                        time.sleep(3)
                        log.info("Gmail: clicked 'Try another way'")
                        break
                except Exception:
                    pass

    def _handle_2fa(self, page, totp_secret: str):
        """Xử lý 2FA bằng TOTP."""
        try:
            import pyotp
            from DrissionPage.common import Actions

            log.info(f"Gmail: 2FA secret found ({len(totp_secret)} chars)")

            # Click Google Authenticator option
            for sel in ['text:Google Authenticator', 'text:Ứng dụng xác thực',
                        'text:Authenticator app', 'text:Use your authenticator app']:
                try:
                    el = page.ele(sel, timeout=1)
                    if el:
                        el.click()
                        time.sleep(1)
                        log.info(f"Gmail: clicked 2FA option: {sel}")
                        break
                except Exception:
                    pass

            # Đợi OTP input
            otp_input = None
            for otp_wait in range(15):
                try:
                    otp_input = page.ele('input[type="tel"]', timeout=1)
                    if not otp_input:
                        otp_input = page.ele('#totpPin', timeout=1)
                    if otp_input:
                        log.info(f"Gmail: OTP input ready ({otp_wait+1}s)")
                        break
                except Exception:
                    pass

            if otp_input:
                log.info("Gmail: waiting 5s for 2FA page to render...")
                time.sleep(5)
            else:
                log.warning("Gmail: OTP input NOT FOUND, trying blind paste...")

            # Generate OTP ngay trước khi nhập
            clean_secret = totp_secret.replace(" ", "").replace("-", "").upper()
            totp = pyotp.TOTP(clean_secret)
            otp_code = totp.now()
            log.info(f"Gmail: generated OTP: {otp_code}")

            self._clipboard_copy(otp_code)

            if otp_input:
                try:
                    otp_input.click()
                    time.sleep(0.3)
                except Exception:
                    pass

            actions = Actions(page)
            actions.key_down('ctrl').key_down('v').key_up('v').key_up('ctrl')
            log.info("Gmail: sent Ctrl+V for OTP")
            time.sleep(0.5)
            actions.key_down('enter').key_up('enter')
            log.info("Gmail: sent Enter for OTP")

        except ImportError:
            log.error("Gmail: pyotp not installed! pip install pyotp")
        except Exception as e:
            log.warning(f"Gmail: 2FA error: {e}")

    # ============================================================
    # CHECK / OTP
    # ============================================================

    def is_logged_in(self, profile_id: str, proxy: str = None) -> bool:
        """Kiểm tra profile đã login Gmail chưa."""
        page = None
        try:
            page = self.stealth.open(profile_id, proxy=proxy)
            page.get(self.MYACCOUNT_URL)
            time.sleep(3)
            url = page.url.lower()
            return "myaccount.google.com" in url and "signin" not in url
        except Exception:
            return False
        finally:
            if page:
                try:
                    page.quit()
                except Exception:
                    pass

    def read_otp(self, profile_id: str, sender: str = "noreply@elevenlabs.io",
                 proxy: str = None, timeout: int = 120) -> Optional[str]:
        """Đọc mã OTP từ Gmail inbox."""
        page = None
        try:
            page = self.stealth.open(profile_id, proxy=proxy)
            page.get(self.GMAIL_URL)
            time.sleep(5)

            start = time.time()
            while time.time() - start < timeout:
                otp = self._extract_otp(page, sender)
                if otp:
                    log.info(f"Gmail: read OTP = {otp}")
                    return otp
                time.sleep(10)
                page.get(self.GMAIL_URL)
                time.sleep(3)

            log.warning(f"Gmail: no OTP received after {timeout}s")
            return None
        except Exception as e:
            log.error(f"Gmail read_otp error: {e}")
            return None
        finally:
            if page:
                try:
                    page.quit()
                except Exception:
                    pass

    def _extract_otp(self, page, sender: str) -> Optional[str]:
        """Tìm OTP từ Gmail inbox."""
        try:
            emails = page.eles(f'text:{sender}', timeout=5)
            if not emails:
                emails = page.eles('text:ElevenLabs', timeout=3)
            if not emails:
                return None
            emails[0].click()
            time.sleep(3)
            body = page.run_js("""
                var msg = document.querySelector('[data-message-id]');
                if (msg) return msg.textContent;
                var main = document.querySelector('[role="main"]');
                if (main) return main.textContent;
                return document.body.textContent;
            """)
            if not body:
                return None
            for pattern in [r'(?:code|mã|otp|verify)[:\s]*(\d{4,8})', r'(\d{6})']:
                match = re.search(pattern, body, re.IGNORECASE)
                if match:
                    return match.group(1)
            return None
        except Exception:
            return None

    # ============================================================
    # BATCH
    # ============================================================

    def login_many(self, accounts: list, proxy: str = None,
                   delay: int = 15, on_progress=None) -> dict:
        """Login nhiều Gmail lần lượt."""
        results = {"success": [], "failed": []}
        for i, acc in enumerate(accounts):
            email = acc["email"]
            pid = acc.get("id", email)
            if on_progress:
                on_progress(f"[{i+1}/{len(accounts)}] {email}")
            ok = self.login(pid, email, acc["password"], acc.get("totp", ""), proxy)
            (results["success"] if ok else results["failed"]).append(email)
            if on_progress:
                on_progress(f"  {'OK' if ok else 'FAIL'}")
            if i < len(accounts) - 1:
                time.sleep(delay)
        return results
