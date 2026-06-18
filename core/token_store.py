"""
token_store.py — Lop luu tru token Firebase ben vung (SQLite).

Truoc day token chi cache trong RAM (accounts/token.py -> self._cache) nen
mat sach moi lan khoi dong lai -> phai login Firebase lai tu dau -> de dinh
co "detected_unusual_activity".

Module nay luu token xuong SQLite (config/firebase_tokens.db) de:
  - Token song xuyen phien (tai dung refresh_token, login it di).
  - Dem so lan login that bai -> tu loai TK chet (auto-disable).

Schema giong cong cu tham chieu:
    firebase_tokens(
        api_key, email PK, password_hash, refresh_token, id_token,
        token_expires_at, last_updated, login_failed_count)

Cach dung:
    from core.token_store import TokenStore
    store = TokenStore()
    rec = store.get("a@b.com")               # dict hoac None
    store.save("a@b.com", refresh_token, id_token, expires_at, api_key=...)
    n = store.bump_failure("a@b.com")        # tang dem loi, tra ve so moi
    store.reset_failure("a@b.com")           # login OK -> reset ve 0
"""

import os
import time
import sqlite3
import threading
from contextlib import contextmanager

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "config", "firebase_tokens.db")

# So lan login that bai lien tiep truoc khi coi TK la chet
MAX_LOGIN_FAILURES = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS firebase_tokens (
    email              TEXT PRIMARY KEY,
    api_key            TEXT,
    password_hash      TEXT,
    refresh_token      TEXT,
    id_token           TEXT,
    token_expires_at   REAL,
    last_updated       REAL,
    login_failed_count INTEGER DEFAULT 0
)
"""


class TokenStore:
    """Luu tru token Firebase ben vung bang SQLite (thread-safe)."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        """Mo connection, commit khi xong, va LUON dong (tranh ro ri handle)."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL: cho phep nhieu reader song song khi dang ghi
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            with conn:          # commit/rollback theo transaction
                yield conn
        finally:
            conn.close()        # dong handle -> khong giu file

    def _init_db(self):
        with self._lock, self._connect() as conn:
            conn.execute(_SCHEMA)

    # ----- doc -----
    def get(self, email: str):
        """Tra ve dict token cho 1 email, hoac None neu chua co."""
        if not email:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM firebase_tokens WHERE email = ?", (email,)
            ).fetchone()
        return dict(row) if row else None

    def get_valid_token(self, email: str, min_ttl: int = 300):
        """Tra ve id_token con han (>= min_ttl giay), nguoc lai None."""
        rec = self.get(email)
        if not rec:
            return None
        exp = rec.get("token_expires_at") or 0
        if rec.get("id_token") and exp > time.time() + min_ttl:
            return {
                "token": rec["id_token"],
                "refresh_token": rec.get("refresh_token") or "",
                "expires_in": int(exp - time.time()),
            }
        return None

    def all_emails(self):
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT email FROM firebase_tokens").fetchall()
        return [r["email"] for r in rows]

    # ----- ghi -----
    def save(self, email: str, refresh_token: str, id_token: str,
             expires_at: float, api_key: str = None,
             password_hash: str = None, reset_failures: bool = True):
        """Luu/cap nhat token cho 1 email. Login OK -> reset dem loi ve 0."""
        if not email:
            return
        now = time.time()
        with self._lock, self._connect() as conn:
            # Giu nguyen api_key/password_hash cu neu lan nay khong truyen
            existing = conn.execute(
                "SELECT api_key, password_hash, login_failed_count "
                "FROM firebase_tokens WHERE email = ?", (email,)
            ).fetchone()

            api_key = api_key if api_key is not None else (
                existing["api_key"] if existing else None)
            password_hash = password_hash if password_hash is not None else (
                existing["password_hash"] if existing else None)
            failed = 0 if reset_failures else (
                existing["login_failed_count"] if existing else 0)

            conn.execute(
                """INSERT INTO firebase_tokens
                    (email, api_key, password_hash, refresh_token, id_token,
                     token_expires_at, last_updated, login_failed_count)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(email) DO UPDATE SET
                     api_key=excluded.api_key,
                     password_hash=excluded.password_hash,
                     refresh_token=excluded.refresh_token,
                     id_token=excluded.id_token,
                     token_expires_at=excluded.token_expires_at,
                     last_updated=excluded.last_updated,
                     login_failed_count=excluded.login_failed_count""",
                (email, api_key, password_hash, refresh_token, id_token,
                 float(expires_at), now, failed),
            )

    def bump_failure(self, email: str) -> int:
        """Tang dem login that bai len 1. Tra ve so dem moi."""
        if not email:
            return 0
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO firebase_tokens
                       (email, last_updated, login_failed_count)
                   VALUES (?, ?, 1)
                   ON CONFLICT(email) DO UPDATE SET
                       login_failed_count = login_failed_count + 1,
                       last_updated = ?""",
                (email, now, now),
            )
            row = conn.execute(
                "SELECT login_failed_count FROM firebase_tokens "
                "WHERE email = ?", (email,)
            ).fetchone()
        return row["login_failed_count"] if row else 0

    def reset_failure(self, email: str):
        if not email:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE firebase_tokens SET login_failed_count = 0 "
                "WHERE email = ?", (email,))

    def is_dead(self, email: str, max_failures: int = MAX_LOGIN_FAILURES):
        """True neu TK da that bai login >= max_failures lan lien tiep."""
        rec = self.get(email)
        if not rec:
            return False
        return (rec.get("login_failed_count") or 0) >= max_failures

    def purge_expired(self, older_than_days: float = 30):
        """Xoa cac ban ghi token qua cu (don dep DB)."""
        cutoff = time.time() - older_than_days * 86400
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM firebase_tokens WHERE last_updated < ?", (cutoff,))
            return cur.rowcount

    def delete(self, email: str):
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM firebase_tokens WHERE email = ?", (email,))

    def stats(self):
        with self._lock, self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) c FROM firebase_tokens").fetchone()["c"]
            valid = conn.execute(
                "SELECT COUNT(*) c FROM firebase_tokens "
                "WHERE token_expires_at > ?", (time.time(),)).fetchone()["c"]
            dead = conn.execute(
                "SELECT COUNT(*) c FROM firebase_tokens "
                "WHERE login_failed_count >= ?",
                (MAX_LOGIN_FAILURES,)).fetchone()["c"]
        return {"total": total, "valid": valid, "dead": dead}


# Singleton dung chung toan app
_default_store = None
_default_lock = threading.Lock()


def get_store() -> TokenStore:
    global _default_store
    if _default_store is None:
        with _default_lock:
            if _default_store is None:
                _default_store = TokenStore()
    return _default_store
