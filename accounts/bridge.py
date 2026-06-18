"""
Bridge — Kết nối Account Manager DB với các tool khác.

Các tool (11Lab voice, YouTube, ...) dùng module này để:
- Lấy TK sẵn sàng (đã ĐK dịch vụ, còn credit)
- Lấy token
- Cập nhật usage
- Lấy proxy theo nhóm TK

Cách dùng:
    from accounts.bridge import get_ready_account, get_token, mark_used

    # Lấy TK ElevenLabs còn credit
    tk = get_ready_account("elevenlabs", need_chars=5000)
    # → {"email": "...", "token": "...", "proxy": "..."}

    # Sau khi convert xong
    mark_used("elevenlabs", tk["email"], chars=5000)
"""

import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from accounts.database import AccountDB
from accounts.stealth import Stealth
from accounts.token import Token
from accounts.proxy import Proxy4G as Proxy


_db = None
_stealth = None


def _get_db():
    global _db
    if _db is None:
        _db = AccountDB()
    return _db


def _get_stealth():
    global _stealth
    if _stealth is None:
        _stealth = Stealth()
    return _stealth


def get_ready_accounts(service_name: str, min_chars: int = 0) -> list:
    """Lấy tất cả TK sẵn sàng (step=5, còn credit)."""
    db = _get_db()
    accounts = db.get_accounts_at_step(service_name, step=5)
    result = []
    for acc in accounts:
        remaining = (acc.get("credits_limit") or 10000) - (acc.get("credits_used") or 0)
        if remaining >= min_chars:
            result.append({
                "email": acc["email"],
                "credits_remaining": remaining,
                "credits_limit": acc.get("credits_limit") or 10000,
                "credits_used": acc.get("credits_used") or 0,
                "group_id": acc.get("group_id"),
            })
    return result


def get_ready_account(service_name: str, need_chars: int = 0) -> dict:
    """Lấy 1 TK sẵn sàng có đủ credit.
    Nếu need_chars > 10000 (1 TK không đủ) → lấy TK còn credit > 0.
    """
    # Thử tìm TK đủ chars
    accounts = get_ready_accounts(service_name, need_chars)
    if accounts:
        return accounts[0]
    # Fallback: lấy TK còn credit bất kỳ (sẽ dùng nhiều TK)
    if need_chars > 0:
        accounts = get_ready_accounts(service_name, min_chars=1)
        return accounts[0] if accounts else None
    return None


def get_token(email: str) -> dict:
    """Lấy Bearer token cho TK (mở Chrome lấy từ localStorage)."""
    stealth = _get_stealth()
    tok = Token(stealth)
    return tok.get_from_browser(email)


def get_proxy(email: str = None) -> str:
    """Lấy proxy cho TK (theo nhóm) hoặc proxy chung."""
    if email:
        db = _get_db()
        group_proxy = db.get_proxy_for_account(email)
        if group_proxy:
            return group_proxy
    return Proxy().get()


def mark_used(service_name: str, email: str, chars: int):
    """Cập nhật credit đã dùng."""
    db = _get_db()
    svc = db.get_service(email, service_name)
    current_used = (svc.get("credits_used") or 0) if svc else 0
    db.set_service(email, service_name, credits_used=current_used + chars)


def get_stats(service_name: str) -> dict:
    """Thống kê dịch vụ."""
    return _get_db().get_service_stats(service_name)
