"""
Database cho Account Manager — SQLite.

Bảng accounts: Gmail core
Bảng services: Dịch vụ đã ĐK (ElevenLabs, ...)
Bảng logs: Log mọi thao tác
"""

import os
import sqlite3
import json
from datetime import datetime
from typing import Optional, List, Dict


DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "accounts.db"
)


class AccountDB:
    """SQLite database cho Account Manager."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password TEXT,
                totp_secret TEXT,
                gmail_status TEXT DEFAULT 'new',
                profile_dir TEXT,
                profile_size_mb REAL DEFAULT 0,
                proxy TEXT,
                last_used TEXT,
                created_at TEXT,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER REFERENCES accounts(id),
                service_name TEXT NOT NULL,
                status TEXT DEFAULT 'new',
                step INTEGER DEFAULT 0,
                error_step INTEGER,
                error_msg TEXT,
                retry_count INTEGER DEFAULT 0,
                token TEXT,
                refresh_token TEXT,
                token_expires_at TEXT,
                credits_used INTEGER DEFAULT 0,
                credits_limit INTEGER DEFAULT 0,
                voice_id TEXT,
                extra_data TEXT,
                registered_at TEXT,
                last_used TEXT,
                UNIQUE(account_id, service_name)
            );

            CREATE TABLE IF NOT EXISTS ip_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                config TEXT,
                status TEXT DEFAULT 'active',
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS ip_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER REFERENCES ip_sources(id),
                name TEXT,
                ip_address TEXT,
                max_accounts INTEGER DEFAULT 5,
                status TEXT DEFAULT 'active',
                flagged_at TEXT,
                last_used TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                account_email TEXT,
                action TEXT,
                result TEXT,
                details TEXT
            );
        """)
        self._conn.commit()

    # ============================================================
    # ACCOUNTS
    # ============================================================

    def import_accounts(self, filepath: str) -> int:
        """Import TK từ file (email|password|totp)."""
        count = 0
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('|')
                if len(parts) < 2:
                    continue
                email = parts[0].strip()
                password = parts[1].strip()
                totp = parts[2].strip() if len(parts) >= 3 else ""

                try:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO accounts (email, password, totp_secret, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (email, password, totp, datetime.now().isoformat()))
                    count += 1
                except sqlite3.IntegrityError:
                    pass
        self._conn.commit()
        return count

    def get_all_accounts(self, gmail_status: str = None) -> List[dict]:
        if gmail_status:
            rows = self._conn.execute(
                "SELECT * FROM accounts WHERE gmail_status = ? ORDER BY id", (gmail_status,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def get_account(self, email: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM accounts WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None

    def update_gmail_status(self, email: str, status: str):
        self._conn.execute(
            "UPDATE accounts SET gmail_status = ? WHERE email = ?", (status, email))
        self._conn.commit()

    def update_account(self, email: str, **kwargs):
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [email]
        self._conn.execute(f"UPDATE accounts SET {sets} WHERE email = ?", vals)
        self._conn.commit()

    def delete_account(self, email: str):
        self._conn.execute("DELETE FROM services WHERE account_id = "
                           "(SELECT id FROM accounts WHERE email = ?)", (email,))
        self._conn.execute("DELETE FROM accounts WHERE email = ?", (email,))
        self._conn.commit()

    def get_account_count(self) -> dict:
        rows = self._conn.execute(
            "SELECT gmail_status, COUNT(*) as cnt FROM accounts GROUP BY gmail_status"
        ).fetchall()
        result = {"total": 0}
        for r in rows:
            result[r["gmail_status"]] = r["cnt"]
            result["total"] += r["cnt"]
        return result

    # ============================================================
    # SERVICES
    # ============================================================

    def set_service(self, email: str, service_name: str, **kwargs):
        """Cập nhật hoặc tạo service cho TK."""
        acc = self.get_account(email)
        if not acc:
            return

        existing = self._conn.execute(
            "SELECT id FROM services WHERE account_id = ? AND service_name = ?",
            (acc["id"], service_name)).fetchone()

        if existing:
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            vals = list(kwargs.values()) + [existing["id"]]
            self._conn.execute(f"UPDATE services SET {sets} WHERE id = ?", vals)
        else:
            kwargs["account_id"] = acc["id"]
            kwargs["service_name"] = service_name
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" * len(kwargs))
            self._conn.execute(
                f"INSERT INTO services ({cols}) VALUES ({placeholders})",
                list(kwargs.values()))
        self._conn.commit()

    def get_service(self, email: str, service_name: str) -> Optional[dict]:
        acc = self.get_account(email)
        if not acc:
            return None
        row = self._conn.execute(
            "SELECT * FROM services WHERE account_id = ? AND service_name = ?",
            (acc["id"], service_name)).fetchone()
        return dict(row) if row else None

    def get_service_stats(self, service_name: str) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) as cnt, SUM(credits_used) as used, "
            "SUM(credits_limit) as total FROM services WHERE service_name = ? "
            "GROUP BY status", (service_name,)).fetchall()
        result = {"total": 0, "registered": 0, "credits_used": 0, "credits_limit": 0}
        for r in rows:
            result[r["status"]] = r["cnt"]
            result["total"] += r["cnt"]
            result["credits_used"] += r["used"] or 0
            result["credits_limit"] += r["total"] or 0
        return result

    def get_accounts_with_service(self, service_name: str) -> List[dict]:
        """Lấy tất cả TK với thông tin dịch vụ."""
        rows = self._conn.execute("""
            SELECT a.*, s.status as svc_status, s.credits_used, s.credits_limit,
                   s.token, s.refresh_token, s.last_used as svc_last_used
            FROM accounts a
            LEFT JOIN services s ON a.id = s.account_id AND s.service_name = ?
            ORDER BY a.id
        """, (service_name,)).fetchall()
        return [dict(r) for r in rows]

    def set_step(self, email: str, service_name: str, step: int):
        """Cập nhật bước pipeline."""
        self.set_service(email, service_name, step=step, error_step=None, error_msg=None)

    def set_error(self, email: str, service_name: str, error_step: int, error_msg: str):
        """Ghi lỗi tại bước nào."""
        svc = self.get_service(email, service_name)
        retry = (svc.get("retry_count") or 0) + 1 if svc else 1
        self.set_service(email, service_name,
                         error_step=error_step, error_msg=error_msg, retry_count=retry)

    def get_step_counts(self, service_name: str) -> dict:
        """Đếm TK theo step + error."""
        rows = self._conn.execute("""
            SELECT
                COALESCE(s.step, 0) as step,
                CASE WHEN s.error_step IS NOT NULL THEN 1 ELSE 0 END as has_error,
                COUNT(*) as cnt
            FROM accounts a
            LEFT JOIN services s ON a.id = s.account_id AND s.service_name = ?
            WHERE a.gmail_status != 'die'
            GROUP BY step, has_error
        """, (service_name,)).fetchall()

        result = {f"step_{i}": 0 for i in range(6)}
        result["errors"] = 0
        result["total"] = 0
        for r in rows:
            step = r["step"] or 0
            if r["has_error"]:
                result["errors"] += r["cnt"]
            else:
                result[f"step_{step}"] += r["cnt"]
            result["total"] += r["cnt"]
        return result

    def get_accounts_at_step(self, service_name: str, step: int = None,
                              errors_only: bool = False) -> List[dict]:
        """Lấy TK ở bước cụ thể hoặc TK bị lỗi."""
        if errors_only:
            rows = self._conn.execute("""
                SELECT a.*, s.step, s.error_step, s.error_msg, s.retry_count,
                       s.credits_used, s.credits_limit, s.voice_id
                FROM accounts a
                LEFT JOIN services s ON a.id = s.account_id AND s.service_name = ?
                WHERE s.error_step IS NOT NULL AND a.gmail_status != 'die'
                ORDER BY a.id
            """, (service_name,)).fetchall()
        elif step is not None:
            rows = self._conn.execute("""
                SELECT a.*, s.step, s.error_step, s.error_msg, s.retry_count,
                       s.credits_used, s.credits_limit, s.voice_id
                FROM accounts a
                LEFT JOIN services s ON a.id = s.account_id AND s.service_name = ?
                WHERE COALESCE(s.step, 0) = ? AND s.error_step IS NULL
                      AND a.gmail_status != 'die'
                ORDER BY a.id
            """, (service_name, step)).fetchall()
        else:
            rows = self._conn.execute("""
                SELECT a.*, COALESCE(s.step, 0) as step, s.error_step, s.error_msg,
                       s.retry_count, s.credits_used, s.credits_limit, s.voice_id
                FROM accounts a
                LEFT JOIN services s ON a.id = s.account_id AND s.service_name = ?
                WHERE a.gmail_status != 'die'
                ORDER BY a.id
            """, (service_name,)).fetchall()
        return [dict(r) for r in rows]

    # ============================================================
    # LOGS
    # ============================================================

    def add_log(self, email: str, action: str, result: str, details: str = ""):
        self._conn.execute(
            "INSERT INTO logs (timestamp, account_email, action, result, details) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), email, action, result, details))
        self._conn.commit()

    def get_logs(self, limit: int = 100, email: str = None) -> List[dict]:
        if email:
            rows = self._conn.execute(
                "SELECT * FROM logs WHERE account_email = ? ORDER BY id DESC LIMIT ?",
                (email, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ============================================================
    # IP SOURCES
    # ============================================================

    def add_ip_source(self, name: str, source_type: str, config: dict = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO ip_sources (name, type, config, created_at) VALUES (?, ?, ?, ?)",
            (name, source_type, json.dumps(config or {}), datetime.now().isoformat()))
        self._conn.commit()
        return cur.lastrowid

    def get_ip_sources(self) -> List[dict]:
        rows = self._conn.execute("SELECT * FROM ip_sources ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def update_ip_source(self, source_id: int, **kwargs):
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [source_id]
        self._conn.execute(f"UPDATE ip_sources SET {sets} WHERE id = ?", vals)
        self._conn.commit()

    def delete_ip_source(self, source_id: int):
        self._conn.execute("DELETE FROM ip_groups WHERE source_id = ?", (source_id,))
        self._conn.execute("DELETE FROM ip_sources WHERE id = ?", (source_id,))
        self._conn.commit()

    # ============================================================
    # IP GROUPS
    # ============================================================

    def add_ip_group(self, source_id: int, name: str, max_accounts: int = 5) -> int:
        cur = self._conn.execute(
            "INSERT INTO ip_groups (source_id, name, max_accounts, created_at) VALUES (?, ?, ?, ?)",
            (source_id, name, max_accounts, datetime.now().isoformat()))
        self._conn.commit()
        return cur.lastrowid

    def get_ip_groups(self, source_id: int = None) -> List[dict]:
        if source_id:
            rows = self._conn.execute(
                "SELECT g.*, s.name as source_name, s.type as source_type, "
                "(SELECT COUNT(*) FROM accounts a WHERE a.group_id = g.id) as account_count "
                "FROM ip_groups g JOIN ip_sources s ON g.source_id = s.id "
                "WHERE g.source_id = ? ORDER BY g.id", (source_id,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT g.*, s.name as source_name, s.type as source_type, "
                "(SELECT COUNT(*) FROM accounts a WHERE a.group_id = g.id) as account_count "
                "FROM ip_groups g JOIN ip_sources s ON g.source_id = s.id "
                "ORDER BY g.id").fetchall()
        return [dict(r) for r in rows]

    def update_ip_group(self, group_id: int, **kwargs):
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [group_id]
        self._conn.execute(f"UPDATE ip_groups SET {sets} WHERE id = ?", vals)
        self._conn.commit()

    def assign_account_to_group(self, email: str, group_id: int):
        self._conn.execute("UPDATE accounts SET group_id = ? WHERE email = ?",
                           (group_id, email))
        self._conn.commit()

    def get_group_accounts(self, group_id: int) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM accounts WHERE group_id = ? ORDER BY id",
            (group_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_unassigned_accounts(self) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM accounts WHERE group_id IS NULL AND gmail_status != 'die' "
            "ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def auto_assign_groups(self, source_id: int, accounts_per_group: int = 5) -> dict:
        """Phân TK chưa gán vào nhóm.

        Logic:
        1. Nhóm hiện tại đang thiếu → điền vào trước
        2. Nếu vẫn còn TK chưa gán → tạo nhóm mới

        Returns: {"filled": n, "created": n}
        """
        unassigned = self.get_unassigned_accounts()
        if not unassigned:
            return {"filled": 0, "created": 0}

        filled = 0
        created = 0
        remaining = list(unassigned)

        # 1. Điền vào nhóm đang thiếu (cùng source)
        groups = self.get_ip_groups(source_id)
        for grp in groups:
            if not remaining:
                break
            if grp.get("status") == "flagged":
                continue  # Không gán vào nhóm bị flag
            slots = grp["max_accounts"] - grp.get("account_count", 0)
            if slots > 0:
                batch = remaining[:slots]
                remaining = remaining[slots:]
                for acc in batch:
                    self.assign_account_to_group(acc["email"], grp["id"])
                    filled += 1

        # 2. Tạo nhóm mới cho TK còn lại
        # Đếm nhóm hiện tại để đặt tên tiếp
        existing_count = len(groups)
        for i in range(0, len(remaining), accounts_per_group):
            batch = remaining[i:i + accounts_per_group]
            existing_count += 1
            group_id = self.add_ip_group(
                source_id, f"Nhóm {existing_count}", accounts_per_group)
            for acc in batch:
                self.assign_account_to_group(acc["email"], group_id)
            created += 1

        return {"filled": filled, "created": created}

    def flag_group(self, group_id: int):
        self.update_ip_group(group_id, status="flagged",
                             flagged_at=datetime.now().isoformat())

    def get_proxy_for_account(self, email: str) -> Optional[str]:
        """Lấy proxy phù hợp cho TK dựa trên nhóm."""
        row = self._conn.execute("""
            SELECT g.ip_address, s.type, s.config
            FROM accounts a
            JOIN ip_groups g ON a.group_id = g.id
            JOIN ip_sources s ON g.source_id = s.id
            WHERE a.email = ? AND g.status = 'active'
        """, (email,)).fetchone()
        if row:
            return dict(row).get("ip_address")
        return None

    def close(self):
        self._conn.close()
