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
