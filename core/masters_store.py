"""
masters_store.py — Quan ly NHIEU master account (config/masters.json).

Moi master = 1 Google account sach, giu refresh_token Firebase. Nhieu master
de vuot gioi han seat (moi master om ~9 worker) -> scale nhieu worker.

Schema masters.json:
[
  {"email": "...", "refresh_token": "AMf-...", "added_at": 169..., "status": "active"}
]
"""
import os
import json
import time
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTERS_JSON = os.path.join(PROJECT_ROOT, "config", "masters.json")
_LOCK = threading.Lock()


def _load_raw():
    if not os.path.exists(MASTERS_JSON):
        return []
    try:
        with open(MASTERS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_raw(masters):
    os.makedirs(os.path.dirname(MASTERS_JSON), exist_ok=True)
    tmp = MASTERS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(masters, f, indent=2, ensure_ascii=False)
    os.replace(tmp, MASTERS_JSON)


def list_masters():
    """Tra ve list master (gom ca master_account.json cu neu co)."""
    masters = _load_raw()
    # Backward-compat: nap master_account.json (master dau tien) neu chua co
    legacy = os.path.join(PROJECT_ROOT, "config", "master_account.json")
    if os.path.exists(legacy):
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                lc = json.load(f)
            rt = (lc.get("refresh_token") or "").strip()
            em = (lc.get("email") or "").strip()
            if rt and not any(m.get("refresh_token") == rt for m in masters):
                masters.insert(0, {"email": em, "refresh_token": rt,
                                   "added_at": 0, "status": "active"})
        except Exception:
            pass
    return masters


def add_master(email, refresh_token):
    """Them 1 master (dedupe theo email/refresh_token). -> (added: bool, msg)."""
    email = (email or "").strip()
    refresh_token = (refresh_token or "").strip()
    if not refresh_token:
        return False, "thieu refresh_token"
    with _LOCK:
        masters = _load_raw()
        for m in masters:
            if m.get("email") == email or m.get("refresh_token") == refresh_token:
                # cap nhat token moi cho master cu
                m["refresh_token"] = refresh_token
                m["email"] = email or m.get("email", "")
                m["status"] = "active"
                m["added_at"] = int(time.time())
                _save_raw(masters)
                return True, f"cap nhat master {email}"
        masters.append({"email": email, "refresh_token": refresh_token,
                        "added_at": int(time.time()), "status": "active"})
        _save_raw(masters)
        return True, f"them master {email}"


def remove_master(email):
    with _LOCK:
        masters = [m for m in _load_raw() if m.get("email") != email]
        _save_raw(masters)
    # neu la master legacy (master_account.json) -> xoa luon file do
    legacy = os.path.join(PROJECT_ROOT, "config", "master_account.json")
    if os.path.exists(legacy):
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                lc = json.load(f)
            if (lc.get("email") or "").strip() == (email or "").strip():
                os.remove(legacy)
        except Exception:
            pass


def ensure_master(email, status="expired"):
    """Tao 1 entry master rong (chua co refresh_token) neu chua co -> ung vien cho
    auto-login lay token. Dung khi NHAP master moi bang credential (chua login).
    -> True neu vua tao moi.
    """
    email = (email or "").strip()
    if not email:
        return False
    with _LOCK:
        masters = _load_raw()
        for m in masters:
            if m.get("email") == email:
                return False
        masters.append({"email": email, "refresh_token": "",
                        "added_at": int(time.time()), "status": status})
        _save_raw(masters)
    return True


def update_refresh_token(email, refresh_token):
    """Luu refresh_token MOI (Firebase co the xoay token moi lan refresh).

    Chi ghi khi token thuc su doi -> tranh ghi file lien tuc. Giu master song lau
    dai (khong luu token xoay -> token cu dan chet nhu paloukite). -> True neu co ghi.
    """
    email = (email or "").strip()
    refresh_token = (refresh_token or "").strip()
    if not email or not refresh_token:
        return False
    with _LOCK:
        masters = _load_raw()
        for m in masters:
            if m.get("email") == email:
                if m.get("refresh_token") != refresh_token:
                    m["refresh_token"] = refresh_token
                    m["status"] = "active"
                    _save_raw(masters)
                    return True
                return False
    return False


def mark_expired(email, reason="", status="expired"):
    """Danh dau master CHET: status='expired' (het han, login lai duoc) hoac
    'suspended' (bi khoa vinh vien, KHONG cuu duoc). Persist vao file -> phien sau
    tu bo qua + BI LOAI khoi live_masters -> cac TK cua no tu re-link sang master
    song khac o lan 'Lien ket Master'. -> True neu tim thay.
    """
    email = (email or "").strip()
    with _LOCK:
        masters = _load_raw()
        found = False
        for m in masters:
            if m.get("email") == email:
                m["status"] = status
                m["expired_at"] = int(time.time())
                if reason:
                    m["expired_reason"] = str(reason)[:160]
                found = True
        if not found:
            # master legacy chua co trong masters.json -> them vao de luu status
            for m in list_masters():
                if m.get("email") == email:
                    m["status"] = status
                    m["expired_at"] = int(time.time())
                    if reason:
                        m["expired_reason"] = str(reason)[:160]
                    masters.append(m)
                    found = True
                    break
        if found:
            _save_raw(masters)
    return found


def set_status(email, status):
    """Bat/tat 1 master ('active'|'disabled'). Master legacy se duoc copy vao
    masters.json de luu duoc status."""
    with _LOCK:
        masters = _load_raw()
        found = False
        for m in masters:
            if m.get("email") == email:
                m["status"] = status; found = True
        if not found:
            # master legacy chua co trong masters.json -> them vao de luu status
            for m in list_masters():
                if m.get("email") == email:
                    m["status"] = status
                    masters.append(m); found = True
                    break
        if found:
            _save_raw(masters)
    return found


def accounts_per_master():
    """Dem so TK moi master dang quan ly (theo roster master_email). -> {email: n}."""
    roster = os.path.join(PROJECT_ROOT, "config", "1000tk_real_status.json")
    counts = {}
    try:
        with open(roster, "r", encoding="utf-8") as f:
            data = json.load(f)
        for acc in data.get("accounts", []):
            me = acc.get("master_email")
            if me:
                counts[me] = counts.get(me, 0) + 1
    except Exception:
        pass
    return counts


def count_active():
    return len([m for m in list_masters() if m.get("status", "active") == "active"])
