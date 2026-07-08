"""
Mode B Account Manager.
File: config/1000tk_real_status.json
"""

import json
import os
import time


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS_JSON = os.path.join(PROJECT_ROOT, "config", "1000tk_real_status.json")


def _load_json_file(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(raw)
        return data


def load_accounts():
    """Load accounts and auto-reset exhausted accounts after reset time."""
    if not os.path.exists(STATUS_JSON):
        return []

    data = _load_json_file(STATUS_JSON)

    accounts = data.get("accounts", [])
    now = time.time()
    reset_count = 0

    for acc in accounts:
        reset_unix = acc.get("next_reset_unix", 0)
        if reset_unix and now >= reset_unix:
            if acc.get("status") in ("exhausted", "alive"):
                acc["chars_remaining"] = 10000
                acc["status"] = "alive"
                acc["next_reset_unix"] = 0
                reset_count += 1

    if reset_count > 0:
        _save(data)

    return accounts


def roster_workspace_ids():
    """Tap workspace_id cua CAC TK MAY NAY SO HUU (trong roster, khong tinh dead).

    Dung de GIOI HAN pool: master chi generate cac workspace co trong roster may nay
    -> chia roster giua nhieu may = chia THAT (khong đụng TK cua may khac).
    Rong -> khong gioi han (dung tat ca workspace cua master, tuong thich cu).
    """
    out = set()
    if not os.path.exists(STATUS_JSON):
        return out
    try:
        data = _load_json_file(STATUS_JSON)
        for acc in data.get("accounts", []):
            if acc.get("status") in ("dead", "flagged"):
                continue
            w = (acc.get("workspace_id") or "").strip()
            if w:
                out.add(w)
    except Exception:
        pass
    return out


def dead_workspace_ids():
    """Tap workspace_id cua cac TK da CHET/disabled (roster) -> pool bo qua ngay,
    khong probe/thu lai (bot churn 'TK loi'). Tich luy qua cac phien."""
    out = set()
    if not os.path.exists(STATUS_JSON):
        return out
    try:
        data = _load_json_file(STATUS_JSON)
        for acc in data.get("accounts", []):
            if acc.get("status") in ("dead", "flagged"):
                w = (acc.get("workspace_id") or "").strip()
                if w:
                    out.add(w)
    except Exception:
        pass
    return out


def get_alive_accounts(min_chars=500):
    """Return accounts that can enter the Mode B queue.

    - `alive`: must have at least `min_chars`
    - `unknown`: allowed if credentials exist; quota will be checked at runtime
    """
    accounts = load_accounts()
    ready = []

    for acc in accounts:
        status = acc.get("status")
        chars_remaining = acc.get("chars_remaining", 0) or 0
        has_api_key = bool((acc.get("api_key") or "").strip())
        has_password = bool((acc.get("password") or "").strip())

        if status == "alive" and chars_remaining >= min_chars:
            ready.append(acc)
        elif status == "unknown" and (has_api_key or has_password):
            ready.append(acc)

    ready.sort(key=lambda a: (
        0 if a.get("status") == "alive" else 1,
        -a.get("chars_remaining", 0)
    ))
    return ready


def update_account_usage(email, chars_used, next_reset_unix=0):
    """Update used chars for an account."""
    if not os.path.exists(STATUS_JSON):
        return

    data = _load_json_file(STATUS_JSON)

    accounts = data.get("accounts", [])
    for acc in accounts:
        if acc["email"] == email:
            remaining = acc.get("chars_remaining", 10000)
            remaining = max(0, remaining - chars_used)
            acc["chars_remaining"] = remaining

            if next_reset_unix:
                acc["next_reset_unix"] = next_reset_unix

            if remaining <= 0:
                acc["status"] = "exhausted"
                acc["last_exhausted"] = time.strftime("%Y-%m-%d %H:%M:%S")
            break

    _sort_and_save(data)


def set_remaining(email, chars_remaining, next_reset_unix=0):
    """Sync remaining chars directly from API data."""
    if not os.path.exists(STATUS_JSON):
        return

    data = _load_json_file(STATUS_JSON)

    for acc in data.get("accounts", []):
        if acc["email"] == email:
            acc["chars_remaining"] = chars_remaining
            if next_reset_unix:
                acc["next_reset_unix"] = next_reset_unix
            if chars_remaining <= 0:
                acc["status"] = "exhausted"
                acc["last_exhausted"] = time.strftime("%Y-%m-%d %H:%M:%S")
            elif acc.get("status") in ("exhausted", "unknown"):
                acc["status"] = "alive"
            break

    _sort_and_save(data)


def set_remaining_by_workspace(workspace_id, chars_remaining):
    """Cap nhat quota cho account theo workspace_id (master mode).

    Master generate qua workspace cua worker -> can ghi quota that ve roster
    de tab Accounts hien dung (khong con full nhu cu).
    """
    if not workspace_id or not os.path.exists(STATUS_JSON):
        return
    data = _load_json_file(STATUS_JSON)
    changed = False
    for acc in data.get("accounts", []):
        if acc.get("workspace_id") == workspace_id:
            acc["chars_remaining"] = chars_remaining
            if chars_remaining <= 0:
                acc["status"] = "exhausted"
            elif acc.get("status") in ("ready", "unknown", "alive", "pending"):
                acc["status"] = "alive"
            changed = True
            break
    if changed:
        _save(data)


def mark_dead_by_workspace(workspace_id, reason=""):
    """Danh dau TK chet theo workspace_id (vd subscription disabled/ban)."""
    if not workspace_id or not os.path.exists(STATUS_JSON):
        return
    data = _load_json_file(STATUS_JSON)
    changed = False
    for acc in data.get("accounts", []):
        if acc.get("workspace_id") == workspace_id:
            acc["status"] = "dead"
            acc["chars_remaining"] = 0
            acc["last_dead"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if reason:
                acc["dead_reason"] = reason[:120]
            changed = True
            break
    if changed:
        _update_summary(data)
        _save(data)


def mark_flagged(email):
    """Mark an account as flagged."""
    if not os.path.exists(STATUS_JSON):
        return

    data = _load_json_file(STATUS_JSON)

    for acc in data.get("accounts", []):
        if acc["email"] == email:
            acc["status"] = "flagged"
            acc["chars_remaining"] = 0
            break

    _update_summary(data)
    _save(data)


def mark_dead(email, reason=""):
    """Mark an account as unusable for Firebase login."""
    if not os.path.exists(STATUS_JSON):
        return

    data = _load_json_file(STATUS_JSON)

    for acc in data.get("accounts", []):
        if acc["email"] == email:
            acc["status"] = "dead"
            acc["chars_remaining"] = 0
            acc["last_dead"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if reason:
                acc["dead_reason"] = reason[:120]
            break

    _update_summary(data)
    _save(data)


def _sort_and_save(data):
    """Sort accounts and save."""
    accounts = data.get("accounts", [])
    accounts.sort(key=lambda a: (
        0 if a.get("status") == "alive" else
        1 if a.get("status") == "unknown" else
        2 if a.get("status") == "exhausted" else
        3 if a.get("status") == "flagged" else 4,
        -a.get("chars_remaining", 0)
    ))

    data["accounts"] = accounts
    _update_summary(data)
    _save(data)


def _update_summary(data):
    """Update summary counts."""
    stats = {"alive": 0, "exhausted": 0, "flagged": 0, "dead": 0}
    for acc in data.get("accounts", []):
        status = acc.get("status", "unknown")
        if status in stats:
            stats[status] += 1
    data["summary"] = stats


def _save(data):
    """Atomic save."""
    data["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = STATUS_JSON + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        if os.path.exists(STATUS_JSON):
            backup = STATUS_JSON + ".bak"
            try:
                if os.path.exists(backup):
                    os.remove(backup)
                os.rename(STATUS_JSON, backup)
            except Exception:
                pass
        os.rename(tmp, STATUS_JSON)
    except OSError:
        try:
            os.remove(tmp)
        except Exception:
            pass
