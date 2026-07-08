"""
master_login.py — Mo Chrome cho user login ElevenLabs (Google OAuth), roi BAT
refresh_token tu localStorage Firebase -> them vao masters_store.

Dung trong GUI (nut "Them Master"): browser mo trong session user nen THAY duoc.
"""
import os
import json
import time
import socket
import tempfile

FB_KEY = "AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys"
LS_KEY = f"firebase:authUser:{FB_KEY}:[DEFAULT]"


def _find_chrome():
    """Tim duong dan chrome.exe (PATH + cac vi tri pho bien + registry)."""
    import shutil
    for name in ("chrome", "chrome.exe"):
        p = shutil.which(name)
        if p:
            return p
    cands = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in cands:
        if p and os.path.exists(p):
            return p
    try:
        import winreg
        for hk in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                k = winreg.OpenKey(
                    hk, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
                v, _ = winreg.QueryValueEx(k, None)
                if v and os.path.exists(v):
                    return v
            except Exception:
                continue
    except Exception:
        pass
    return None


def _fresh_browser():
    """1 Chrome SACH, doc lap (profile rieng + port rieng) -> moi TK 1 Chrome.

    -> (ChromiumPage, profile_dir_de_xoa).
    """
    from DrissionPage import ChromiumPage, ChromiumOptions
    chrome = _find_chrome()
    if not chrome:
        raise RuntimeError(
            "Chua cai Google Chrome! Tai tai https://www.google.com/chrome roi thu lai.")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    profile = tempfile.mkdtemp(prefix="master_")
    co = ChromiumOptions()
    co.set_browser_path(chrome)     # chi ro chrome.exe -> chac chan mo dung
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    co.set_argument("--no-proxy-server")
    co.set_local_port(port)        # port rieng -> KHONG dinh vao Chrome dang mo
    co.set_user_data_path(profile)  # profile trang -> moi browser doc lap
    return ChromiumPage(co), profile


def capture_master_login(timeout=300, on_log=lambda *_: None,
                         should_stop=lambda: False):
    """Mo Chrome -> user login -> doc refresh_token. -> (email, refresh_token) | (None, err).

    timeout: giay cho user login.
    """
    page = None
    tmp_profile = None
    try:
        page, tmp_profile = _fresh_browser()
        on_log("Dang mo trang dang nhap ElevenLabs...")
        page.get("https://elevenlabs.io/app/sign-in")
        on_log("HAY DANG NHAP bang Google (master account) trong cua so Chrome vua mo.")

        start = time.time()
        while time.time() - start < timeout:
            if should_stop():
                return None, "huy"
            try:
                raw = page.run_js(
                    f'return localStorage.getItem({json.dumps(LS_KEY)}) || "";')
            except Exception:
                raw = ""
            if raw:
                try:
                    data = json.loads(raw)
                    stm = data.get("stsTokenManager", {})
                    rt = stm.get("refreshToken", "")
                    email = data.get("email", "")
                    if rt:
                        on_log(f"Bat duoc master: {email}")
                        return email, rt
                except Exception:
                    pass
            time.sleep(2)
        return None, "het thoi gian cho login"
    except Exception as e:
        return None, f"loi browser: {str(e)[:80]}"
    finally:
        if page:
            try:
                page.quit()
            except Exception:
                pass
        if tmp_profile:
            try:
                import shutil
                shutil.rmtree(tmp_profile, ignore_errors=True)
            except Exception:
                pass


def _read_master_creds(email):
    """Doc password + totp cua 1 master tu config/gmail.txt (format: email|password|totp).

    Day la file credential dung chung ca he thong (Register cung doc file nay).
    Chu master chi can them 1 dong 'email|password|totp' -> tool tu login lai duoc.
    -> (password, totp) | (None, None) neu khong co.
    """
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "gmail.txt")
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = [p.strip() for p in line.strip().split("|")]
                if parts and parts[0].lower() == (email or "").lower():
                    pwd = parts[1] if len(parts) >= 2 else ""
                    totp = parts[2] if len(parts) >= 3 else ""
                    return (pwd or None), (totp or "")
    except Exception:
        pass
    return None, None


def _read_ls_refresh_token(page):
    """Doc refresh_token Firebase tu localStorage cua ElevenLabs. -> (email, rt) | ('', '')."""
    try:
        raw = page.run_js(
            f'return localStorage.getItem({json.dumps(LS_KEY)}) || "";')
    except Exception:
        raw = ""
    if not raw:
        return "", ""
    try:
        d = json.loads(raw)
        rt = d.get("stsTokenManager", {}).get("refreshToken", "")
        return d.get("email", ""), rt
    except Exception:
        return "", ""


def _pick_google_account(tab, email):
    """Tren trang/ popup chon tai khoan Google -> bam dung email (hoac TK dau),
    roi bam Continue/Tiep tuc/Allow neu co man hinh xac nhan quyen."""
    try:
        # 1) Bam o tai khoan trung email
        el = tab.ele(f"text:{email}", timeout=6)
        if el:
            el.click()
        else:
            # 2) Bam o tai khoan dau tien (data-identifier / role=link)
            for sel in ("@data-identifier", "tag:div@@data-identifier",
                        "css:[data-identifier]", "css:[role=link]"):
                el = tab.ele(sel, timeout=2)
                if el:
                    el.click()
                    break
        time.sleep(3)
    except Exception:
        pass
    # 3) Man hinh "Continue"/"Tiep tuc"/"Allow" (cap quyen)
    for sel in ("text:Continue", "text:Tiếp tục", "text:Tiep tuc",
                "text:Allow", "text:Cho phép", "#submit_approve_access"):
        try:
            b = tab.ele(sel, timeout=2)
            if b:
                b.click()
                time.sleep(2)
                break
        except Exception:
            pass


def auto_login_master(email, password=None, totp_secret="",
                      on_log=lambda *_: None, should_stop=lambda: False):
    """TU DONG dang nhap lai 1 master -> lay refresh_token moi.

    DON GIAN: mo 1 Chrome KHONG proxy (co mang, giong nut 'Them Master') roi login
    Google (email/pass/2FA) + dang nhap ElevenLabs bang Google + doc token.

    -> (email, refresh_token) khi thanh cong | (None, None) khi that bai (ly do qua on_log).
    """
    from accounts.gmail import GmailLogin

    if not password:
        pwd, totp2 = _read_master_creds(email)
        password = pwd
        if not totp_secret:
            totp_secret = totp2 or ""
    if not password:
        on_log(f"{email}: thieu credential trong config/gmail.txt (email|password|totp)")
        return None, None

    helper = GmailLogin(None)   # chi dung ham phu _handle_2fa/_handle_try_another_way
    page = None
    tmp_profile = None
    try:
        page, tmp_profile = _fresh_browser()   # Chrome sach, KHONG proxy -> co mang

        # ---- 1. Login Google ----
        on_log(f"[1/3] Login Google: {email}")
        page.get("https://accounts.google.com/ServiceLogin?hl=en")
        time.sleep(3)
        if "myaccount.google.com" not in page.url:
            el = page.ele("#identifierId", timeout=10) or page.ele("tag:input@type=email", timeout=3)
            if not el:
                on_log("Google: khong thay o nhap email (trang khong load / mat mang?)")
                return None, None
            el.input(email)
            time.sleep(0.5)
            (page.ele("#identifierNext", timeout=3) or el).click() if page.ele("#identifierNext", timeout=1) else el.input("\n")
            time.sleep(4)
            # password
            pw = None
            for _ in range(15):
                pw = page.ele("tag:input@type=password", timeout=1) or page.ele("@name=Passwd", timeout=1)
                if pw and pw.states.is_displayed:
                    break
                time.sleep(1)
            if not pw:
                on_log("Google: khong thay o nhap password")
                return None, None
            pw.input(password)
            time.sleep(0.5)
            nx = page.ele("#passwordNext", timeout=2)
            (nx or pw).click() if nx else pw.input("\n")
            time.sleep(4)
            # 2FA (tai dung ham da test)
            if totp_secret:
                on_log("[1/3] Nhap 2FA (TOTP)...")
                helper._handle_try_another_way(page)
                helper._handle_2fa(page, totp_secret)
                time.sleep(4)
            # doi login xong
            for _ in range(20):
                u = page.url.lower()
                if "myaccount.google.com" in u or ("accounts.google.com" in u
                        and "/signin" not in u and "/challenge" not in u):
                    break
                time.sleep(1)

        if should_stop():
            return None, None

        # ---- 2. Dang nhap ElevenLabs bang Google ----
        on_log("[2/3] Dang nhap ElevenLabs bang Google...")
        page.get("https://elevenlabs.io/app/sign-in")
        time.sleep(4)
        # Da co token san (Google session con) -> khoi bam
        if not (_read_ls_refresh_token(page)[1]):
            try:
                n_before = len(page.tab_ids)
            except Exception:
                n_before = 1
            gbtn = None
            for sel in ["text:Sign in with Google", "text:Continue with Google",
                        "text:Log in with Google", "@aria-label:Google"]:
                gbtn = page.ele(sel, timeout=3)
                if gbtn:
                    break
            if gbtn:
                gbtn.click()
                time.sleep(4)
                # Truong hop mo POPUP (tab moi) chon tai khoan Google
                try:
                    opened_popup = len(page.tab_ids) > n_before
                except Exception:
                    opened_popup = False
                if opened_popup:
                    try:
                        popup = page.latest_tab
                        _pick_google_account(popup, email)
                    except Exception:
                        pass
                    # doi popup dong (OAuth xong)
                    for _ in range(25):
                        try:
                            if len(page.tab_ids) <= n_before:
                                break
                        except Exception:
                            break
                        time.sleep(1)
                elif "accounts.google.com" in page.url.lower():
                    # Chon tai khoan ngay trong tab hien tai
                    _pick_google_account(page, email)
                time.sleep(4)

        # ---- 3. Doc refresh_token (doi toi da ~60s cho EL login xong) ----
        on_log("[3/3] Doc refresh_token...")
        got_email, rt = "", ""
        for _ in range(30):
            if should_stop():
                return None, None
            # dam bao dang o elevenlabs de doc dung localStorage
            if "elevenlabs.io" not in page.url.lower():
                page.get("https://elevenlabs.io/app/home")
                time.sleep(3)
            got_email, rt = _read_ls_refresh_token(page)
            if rt and len(rt) > 80:
                on_log(f"OK — lay duoc refresh_token moi ({len(rt)} ky tu)")
                return (got_email or email), rt
            time.sleep(2)
        on_log("Khong doc duoc refresh_token (EL login chua xong / bi chan)")
        return None, None
    except Exception as e:
        on_log(f"loi: {str(e)[:100]}")
        return None, None
    finally:
        if page:
            try:
                page.quit()
            except Exception:
                pass
        if tmp_profile:
            try:
                import shutil
                shutil.rmtree(tmp_profile, ignore_errors=True)
            except Exception:
                pass


def _gmail_txt_path():
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "gmail.txt")


def _save_creds_to_gmail_txt(rows):
    """Them cac dong 'email|password|totp' vao config/gmail.txt (dedupe theo email)."""
    path = _gmail_txt_path()
    existing = {}
    order = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    em = line.split("|", 1)[0].strip().lower()
                    if em and em not in existing:
                        existing[em] = line
                        order.append(em)
        except Exception:
            pass
    for email, pwd, totp in rows:
        em = email.lower()
        newline = f"{email}|{pwd}|{totp}"
        if em not in existing:
            order.append(em)
        existing[em] = newline   # ghi de credential moi nhat
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for em in order:
            f.write(existing[em] + "\n")


def parse_master_creds_text(text):
    """Parse text nhieu dong 'email|password|totp' -> [(email,password,totp), ...]."""
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2 and "@" in parts[0]:
            rows.append((parts[0], parts[1], parts[2] if len(parts) >= 3 else ""))
    return rows


def add_masters_bulk(text, auto_login=True,
                     on_log=lambda *_: None, should_stop=lambda: False):
    """NHAP nhieu master 1 lan tu text 'email|password|totp' (moi dong 1 TK).

    1. Luu credential vao config/gmail.txt (de auto-recover ve sau).
    2. Tao entry master (placeholder) cho tung email.
    3. Neu auto_login: tu dang nhap tung TK lay refresh_token -> bat active.

    -> {"added": [...], "logged_in": [...], "need_login": [...], "failed": [(e,ly_do)]}.
    """
    from core import masters_store
    rows = parse_master_creds_text(text)
    out = {"added": [], "logged_in": [], "need_login": [], "failed": []}
    if not rows:
        return out
    # 1 + 2: luu creds + tao placeholder
    _save_creds_to_gmail_txt(rows)
    for email, pwd, totp in rows:
        masters_store.ensure_master(email)
        out["added"].append(email)
    if not auto_login:
        out["need_login"] = list(out["added"])
        return out
    # 3: auto-login tung master lay token
    for email, pwd, totp in rows:
        if should_stop():
            break
        on_log(f"Dang nhap master {email} ...")
        got, rt = auto_login_master(email, password=pwd, totp_secret=totp,
                                    on_log=on_log, should_stop=should_stop)
        if rt:
            masters_store.add_master(got or email, rt)   # luu token + bat active
            out["logged_in"].append(email)
            on_log(f"  OK master song: {email}")
        else:
            out["need_login"].append(email)
            on_log(f"  Chua login duoc {email} (thu lai bang nut Auto login lai)")
    return out


def recover_expired_masters(on_log=lambda *_: None, should_stop=lambda: False):
    """Tu dong login lai TAT CA master dang 'expired' MA co credential trong gmail.txt.

    Dung khi mo tool hoac bam 1 nut -> master het han tu song lai neu co id/pass/2fa.
    -> {"recovered": [...], "skipped": [...], "failed": [(email, ly_do), ...]}.
    """
    from core import masters_store
    out = {"recovered": [], "skipped": [], "failed": []}
    for m in masters_store.list_masters():
        if (m.get("status") or "active") != "expired":
            continue
        email = m.get("email") or ""
        if should_stop():
            break
        pwd, totp = _read_master_creds(email)
        if not pwd:
            out["skipped"].append(email)
            on_log(f"Bo qua {email}: chua co credential trong gmail.txt")
            continue
        on_log(f"Dang khoi phuc {email} ...")
        got_email, rt = auto_login_master(
            email, password=pwd, totp_secret=totp,
            on_log=on_log, should_stop=should_stop)
        if rt:
            masters_store.add_master(got_email or email, rt)  # luu + bat active
            out["recovered"].append(email)
            on_log(f"KHOI PHUC OK: {email}")
        else:
            out["failed"].append((email, rt if isinstance(rt, str) else "fail"))
            on_log(f"KHOI PHUC FAIL {email}: {got_email}")
    return out


def open_master_session(email, refresh_token, timeout=600,
                        on_log=lambda *_: None, should_stop=lambda: False):
    """Mo lai browser DA DANG NHAP san 1 master da co (inject token da luu).

    Dung de xem/thao tac lai master cu. Truoc khi dong, doc lai refresh_token
    (Firebase co the xoay token) -> tra ve token moi de cap nhat.
    -> (email, refresh_token_moi) | (None, err).
    """
    import base64
    import shutil
    from core.api_client import firebase_refresh

    rt = (refresh_token or "").strip()
    if not rt:
        return None, "master khong co refresh_token"
    try:
        res = firebase_refresh(rt, proxy=None)
        idt = res["id_token"]
        rt = res.get("refresh_token", rt)
    except Exception as e:
        return None, f"master da chet (refresh loi): {str(e)[:60]}"

    try:
        pl = json.loads(base64.urlsafe_b64decode(idt.split(".")[1] + "=="))
    except Exception:
        pl = {}
    auth = {
        "uid": pl.get("user_id", ""), "email": email or pl.get("email", ""),
        "emailVerified": True, "isAnonymous": False,
        "providerData": [{"providerId": "google.com",
                          "uid": pl.get("email", ""), "email": email}],
        "stsTokenManager": {"refreshToken": rt, "accessToken": idt,
                            "expirationTime": int(pl.get("exp", 0)) * 1000},
        "createdAt": "0", "lastLoginAt": "0", "apiKey": FB_KEY, "appName": "[DEFAULT]"}

    page = None
    tmp_profile = None
    try:
        page, tmp_profile = _fresh_browser()
        page.get("https://elevenlabs.io/")
        time.sleep(2)
        page.run_js(
            f'localStorage.setItem({json.dumps(LS_KEY)}, {json.dumps(json.dumps(auth))});')
        page.get("https://elevenlabs.io/app/home")
        on_log(f"Da mo session master: {email}")
        # giu mo cho user thao tac; doc lai refresh_token truoc khi dong
        start = time.time()
        latest = rt
        while time.time() - start < timeout:
            if should_stop():
                break
            try:
                raw = page.run_js(
                    f'return localStorage.getItem({json.dumps(LS_KEY)}) || "";')
                if raw:
                    d = json.loads(raw)
                    nr = d.get("stsTokenManager", {}).get("refreshToken", "")
                    if nr:
                        latest = nr
            except Exception:
                pass
            time.sleep(3)
        return (email or auth["email"]), latest
    except Exception as e:
        return None, f"loi browser: {str(e)[:80]}"
    finally:
        if page:
            try:
                page.quit()
            except Exception:
                pass
        if tmp_profile:
            shutil.rmtree(tmp_profile, ignore_errors=True)
