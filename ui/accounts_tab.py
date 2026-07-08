"""
Tab quan ly tai khoan Mode B.
Hien thi danh sach 1000 TK, trang thai, quota.
Co the scan lai, quick check quota.
"""

import os
import sys
import json
import time

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar,
    QAbstractItemView, QMessageBox, QComboBox, QGroupBox,
    QFileDialog, QInputDialog, QDialog, QTextEdit, QCheckBox,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QFont

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

STATUS_JSON = os.path.join(PROJECT_ROOT, "config", "1000tk_real_status.json")
RAW_TXT = os.path.join(PROJECT_ROOT, "config", "1000tk.txt")

COLORS = {
    "alive": "#27ae60",
    "exhausted": "#f39c12",
    "flagged": "#e74c3c",
    "dead": "#95a5a6",
    "unknown": "#95a5a6",
}


def _parse_accounts_text(raw_text):
    """Parse text thanh danh sach TK.

    Ho tro:
    - email|password|api_key
    - email|password
    - bo qua dong rong / comment / header
    """
    parsed = []
    seen = set()

    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        lower = line.lower()
        # Bo qua dong header (vd: "email|pass|api_key|login_refresh_token")
        first = lower.replace("\t", "|").split("|")[0].strip()
        if first in ("mail", "email"):
            continue

        # Ho tro phan cach bang "|" hoac TAB (giong export tool khac)
        import re as _re
        parts = [p.strip() for p in _re.split(r"[|\t]", line)]
        if len(parts) < 2:
            raise ValueError(
                f"Dong {line_no} khong hop le. Dinh dang can: "
                f"email|password|api_key|login_refresh_token"
            )

        email = parts[0]
        password = parts[1]
        api_key = parts[2] if len(parts) >= 3 else ""
        # Cot 4: login_refresh_token (Firebase refresh token cua worker).
        # Co thi onboard khong can login lai; khong co thi login bang password.
        login_refresh_token = parts[3] if len(parts) >= 4 else ""

        if "@" not in email:
            raise ValueError(f"Dong {line_no}: email khong hop le: {email}")
        if not password and not login_refresh_token:
            raise ValueError(
                f"Dong {line_no}: can password hoac login_refresh_token")

        key = email.lower()
        if key in seen:
            continue
        seen.add(key)

        parsed.append({
            "email": email,
            "password": password,
            "api_key": api_key,
            "login_refresh_token": login_refresh_token,
            "status": "unknown",
            "chars_remaining": 0,
            "error": "",
        })

    return parsed


def _write_raw_accounts(accounts):
    os.makedirs(os.path.dirname(RAW_TXT), exist_ok=True)
    with open(RAW_TXT, "w", encoding="utf-8") as f:
        for acc in accounts:
            f.write(
                f"{acc.get('email', '')}|{acc.get('password', '')}|"
                f"{acc.get('api_key', '')}|"
                f"{acc.get('login_refresh_token', '')}\n"
            )


def _save_accounts_json(accounts, scan_time=""):
    os.makedirs(os.path.dirname(STATUS_JSON), exist_ok=True)

    stats = {"alive": 0, "exhausted": 0, "flagged": 0, "dead": 0}
    for acc in accounts:
        status = acc.get("status", "unknown")
        if status in stats:
            stats[status] += 1

    data = {
        "scan_time": scan_time,
        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": stats,
        "accounts": accounts,
    }

    with open(STATUS_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


class AddMasterWorker(QThread):
    """Mo Chrome cho user login -> bat refresh_token -> luu master moi."""
    log = pyqtSignal(str)
    done = pyqtSignal(bool, str)   # ok, msg

    def __init__(self):
        super().__init__()
        self._stop = False

    def cancel(self):
        self._stop = True

    def run(self):
        try:
            from core.master_login import capture_master_login
            from core.masters_store import add_master, count_active
            email, rt = capture_master_login(
                timeout=300,
                on_log=lambda m: self.log.emit(m),
                should_stop=lambda: self._stop)
            if not rt:
                self.done.emit(False, f"Khong lay duoc master ({email})")
                return
            ok, msg = add_master(email, rt)
            self.done.emit(ok, f"{msg} | tong master: {count_active()}")
        except Exception as e:
            self.done.emit(False, f"loi: {str(e)[:100]}")


class MasterCheckWorker(QThread):
    """Kiem tra tung master con song khong (refresh token OK)."""
    result = pyqtSignal(str, bool)   # email, alive
    done = pyqtSignal()

    def __init__(self, masters):
        super().__init__()
        self.masters = masters

    def run(self):
        from core.master_workspace import MasterWorkspace
        for m in self.masters:
            rt = (m.get("refresh_token") or "").strip()
            alive = False
            if rt:
                try:
                    MasterWorkspace(refresh_token=rt,
                                    email=m.get("email", "")).master_token()
                    alive = True
                except Exception:
                    alive = False
            self.result.emit(m.get("email", ""), alive)
        self.done.emit()


class ReopenMasterWorker(QThread):
    """Mo lai browser da dang nhap san 1 master da co (giu mo cho user thao tac)."""
    log = pyqtSignal(str)
    done = pyqtSignal(bool, str)   # ok, msg

    def __init__(self, email, refresh_token):
        super().__init__()
        self.email = email
        self.refresh_token = refresh_token
        self._stop = False

    def cancel(self):
        self._stop = True

    def run(self):
        try:
            from core.master_login import open_master_session
            from core.masters_store import add_master
            email, rt = open_master_session(
                self.email, self.refresh_token,
                on_log=lambda m: self.log.emit(m),
                should_stop=lambda: self._stop)
            if not email:
                self.done.emit(False, str(rt))
                return
            # token co the da xoay -> cap nhat lai
            if rt and rt != self.refresh_token:
                add_master(email, rt)
            self.done.emit(True, f"da dong session {email}")
        except Exception as e:
            self.done.emit(False, f"loi: {str(e)[:100]}")


class AutoRecoverWorker(QThread):
    """TU DONG login lai cac master 'expired' co credential trong gmail.txt."""
    log = pyqtSignal(str)
    done = pyqtSignal(dict)   # {"recovered":[...], "skipped":[...], "failed":[...]}

    def __init__(self, only_email=None):
        super().__init__()
        self.only_email = only_email
        self._stop = False

    def cancel(self):
        self._stop = True

    def run(self):
        try:
            from core.master_login import auto_login_master, recover_expired_masters
            from core.masters_store import add_master
            if self.only_email:
                # Login lai DUNG 1 master (du dang active hay expired)
                email, rt = auto_login_master(
                    self.only_email,
                    on_log=lambda m: self.log.emit(m),
                    should_stop=lambda: self._stop)
                if rt:
                    add_master(email or self.only_email, rt)
                    self.done.emit({"recovered": [self.only_email],
                                    "skipped": [], "failed": []})
                else:
                    self.done.emit({"recovered": [], "skipped": [],
                                    "failed": [(self.only_email, str(email))]})
                return
            res = recover_expired_masters(
                on_log=lambda m: self.log.emit(m),
                should_stop=lambda: self._stop)
            self.done.emit(res)
        except Exception as e:
            self.done.emit({"recovered": [], "skipped": [],
                            "failed": [("?", str(e)[:80])]})


class BulkMasterWorker(QThread):
    """Nhap NHIEU master 1 lan: luu creds -> auto login lay token -> bat active."""
    log = pyqtSignal(str)
    done = pyqtSignal(dict)

    def __init__(self, text, auto_login=True):
        super().__init__()
        self.text = text
        self.auto_login = auto_login
        self._stop = False

    def cancel(self):
        self._stop = True

    def run(self):
        try:
            from core.master_login import add_masters_bulk
            res = add_masters_bulk(
                self.text, auto_login=self.auto_login,
                on_log=lambda m: self.log.emit(m),
                should_stop=lambda: self._stop)
            self.done.emit(res)
        except Exception as e:
            self.done.emit({"added": [], "logged_in": [], "need_login": [],
                            "failed": [("?", str(e)[:100])]})


class MasterBulkInputDialog(QDialog):
    """Cho dan nhieu master (email|password|totp), moi dong 1 TK -> nhap 1 luot."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Nhập nhiều master")
        self.resize(560, 420)
        self.worker = None
        v = QVBoxLayout(self)
        v.addWidget(QLabel(
            "Dán danh sách master, mỗi dòng: <b>email|password|totp</b>\n"
            "(TOTP = mã 2FA secret). Tool sẽ lưu và tự đăng nhập lấy token."))
        self.edit = QTextEdit()
        self.edit.setPlaceholderText(
            "vd:\nabc@gmail.com|matkhau|totpsecret\nxyz@gmail.com|matkhau|totpsecret")
        self.edit.setStyleSheet("font-family:Consolas,monospace; font-size:12px;")
        v.addWidget(self.edit, 1)
        self.chk_login = QCheckBox("Tự đăng nhập ngay để lấy token (mở Chrome)")
        self.chk_login.setChecked(True)
        v.addWidget(self.chk_login)
        self.lbl = QLabel("")
        self.lbl.setStyleSheet("color:#666; font-size:11px;")
        self.lbl.setWordWrap(True)
        v.addWidget(self.lbl)
        row = QHBoxLayout()
        self.btn_ok = QPushButton("➕ Nhập")
        self.btn_ok.setStyleSheet(
            "font-weight:bold; background:#27ae60; color:white; padding:6px 14px;")
        self.btn_ok.clicked.connect(self._submit)
        row.addWidget(self.btn_ok)
        row.addStretch(1)
        btn_close = QPushButton("Đóng")
        btn_close.clicked.connect(self.reject)
        row.addWidget(btn_close)
        v.addLayout(row)

    def _submit(self):
        if self.worker and self.worker.isRunning():
            return
        from core.master_login import parse_master_creds_text
        rows = parse_master_creds_text(self.edit.toPlainText())
        if not rows:
            QMessageBox.warning(self, "Nhập master",
                                "Chưa có dòng hợp lệ (email|password|totp).")
            return
        self.btn_ok.setEnabled(False)
        self.lbl.setText(f"Đang xử lý {len(rows)} master...")
        self.worker = BulkMasterWorker(
            self.edit.toPlainText(), auto_login=self.chk_login.isChecked())
        self.worker.log.connect(lambda m: self.lbl.setText(m))
        self.worker.done.connect(self._on_done)
        self.worker.start()

    def _on_done(self, res):
        self.btn_ok.setEnabled(True)
        added = res.get("added", [])
        li = res.get("logged_in", [])
        need = res.get("need_login", [])
        fail = res.get("failed", [])
        parts = [f"Đã nhập {len(added)} master."]
        if li:
            parts.append(f"✅ Đăng nhập được ({len(li)}): {', '.join(li)}")
        if need:
            parts.append(f"⏳ Chưa lấy được token ({len(need)}) — bấm "
                         f"'🔧 Tự động login lại' sau: {', '.join(need)}")
        if fail:
            parts.append("❌ Lỗi: " + ", ".join(str(e) for e, _ in fail))
        if li:
            parts.append("\n➡ Giờ bấm '🔗 Liên kết Master' để chuyển các TK từ "
                         "master chết sang master mới (tool tự re-link).")
        summary = "\n".join(parts)
        self.lbl.setText(summary.replace("\n", " | "))
        try:
            from core.master_pool import reset_shared_pool
            reset_shared_pool()   # co master moi -> pool nap lai
        except Exception:
            pass
        QMessageBox.information(self, "Nhập master", summary)
        self.accept()


class MasterManagerDialog(QDialog):
    """Bang quan ly master: xem danh sach, trang thai song/chet, so TK, bat/tat, xoa."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Quản lý Master")
        self.resize(680, 400)
        self.add_worker = None
        self.check_worker = None
        self.reopen_worker = None
        self.recover_worker = None
        self._alive = {}    # email -> True/False sau khi kiem tra
        self._build()
        self._reload()

    def _build(self):
        v = QVBoxLayout(self)
        info = QLabel(
            "Master = Google account sạch để generate KHÔNG bị flag. "
            "Nhiều master = chia tải đều; 1 master die thì các TK của nó "
            "tự re-link sang master còn sống ở lần 'Liên kết Master' sau.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#555; font-size:11px;")
        v.addWidget(info)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["Email", "Trạng thái", "Số TK", "Thêm lúc",
             "Mở lại", "Bật/Tắt", "Xóa"])
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3, 4, 5, 6):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        v.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.btn_add = QPushButton("➕ Thêm master mới")
        self.btn_add.setStyleSheet(
            "font-weight:bold; background:#d35400; color:white; padding:6px 14px;")
        self.btn_add.clicked.connect(self._add_master)
        row.addWidget(self.btn_add)

        self.btn_bulk = QPushButton("📋 Nhập nhiều master")
        self.btn_bulk.setToolTip(
            "Dan nhieu master (email|password|totp) 1 luot -> tool tu login lay token.")
        self.btn_bulk.setStyleSheet("background:#2980b9; color:white; padding:6px 12px;")
        self.btn_bulk.clicked.connect(self._bulk_master)
        row.addWidget(self.btn_bulk)

        self.btn_check = QPushButton("🔄 Kiểm tra sống/chết")
        self.btn_check.clicked.connect(self._check_alive)
        row.addWidget(self.btn_check)

        self.btn_recover = QPushButton("🔧 Tự động login lại (hết hạn)")
        self.btn_recover.setToolTip(
            "Tu dong dang nhap lai cac master HET HAN co credential\n"
            "(them dong 'email|password|totp' vao config/gmail.txt).")
        self.btn_recover.setStyleSheet("background:#e67e22; color:white; padding:6px 10px;")
        self.btn_recover.clicked.connect(self._auto_recover)
        row.addWidget(self.btn_recover)

        self.lbl = QLabel("")
        self.lbl.setStyleSheet("color:#666; font-size:11px;")
        row.addWidget(self.lbl, 1)

        btn_close = QPushButton("Đóng")
        btn_close.clicked.connect(self.accept)
        row.addWidget(btn_close)
        v.addLayout(row)

    def _reload(self):
        from core.masters_store import list_masters, accounts_per_master
        masters = list_masters()
        counts = accounts_per_master()
        self.table.setRowCount(0)
        for m in masters:
            email = m.get("email", "")
            status = m.get("status", "active")
            row = self.table.rowCount()
            self.table.insertRow(row)

            self.table.setItem(row, 0, QTableWidgetItem(email))

            # trang thai: uu tien ket qua kiem tra song/chet
            if email in self._alive:
                txt = "🟢 SỐNG" if self._alive[email] else "🔴 CHẾT"
                color = "#27ae60" if self._alive[email] else "#e74c3c"
            elif status == "expired":
                txt, color = "🔑 HẾT HẠN", "#e67e22"
            elif status == "disabled":
                txt, color = "⏸ TẮT", "#95a5a6"
            else:
                txt, color = "active", "#8e44ad"
            st_item = QTableWidgetItem(txt)
            st_item.setForeground(QColor(color))
            self.table.setItem(row, 1, st_item)

            n = counts.get(email, 0)
            n_item = QTableWidgetItem(str(n))
            n_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 2, n_item)

            at = m.get("added_at", 0)
            at_txt = "legacy" if not at else time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(at))
            self.table.setItem(row, 3, QTableWidgetItem(at_txt))

            # Mo lai (mo browser da dang nhap san master nay)
            rt = (m.get("refresh_token") or "").strip()
            btn_o = QPushButton("Mở lại")
            btn_o.setToolTip("Mo Chrome da dang nhap san master nay de xem/thao tac.")
            btn_o.setEnabled(bool(rt))
            btn_o.clicked.connect(
                lambda _, e=email, r=rt: self._reopen(e, r))
            self.table.setCellWidget(row, 4, btn_o)

            # Bat/Tat
            btn_t = QPushButton("Tắt" if status != "disabled" else "Bật")
            btn_t.clicked.connect(
                lambda _, e=email, s=status: self._toggle(e, s))
            self.table.setCellWidget(row, 5, btn_t)

            # Xoa
            btn_d = QPushButton("Xóa")
            btn_d.setStyleSheet("color:#e74c3c;")
            btn_d.clicked.connect(lambda _, e=email, c=n: self._delete(e, c))
            self.table.setCellWidget(row, 6, btn_d)

        self.lbl.setText(f"{len(masters)} master")

    def closeEvent(self, e):
        for w in (self.reopen_worker, self.check_worker, self.add_worker,
                  self.recover_worker):
            try:
                if w and w.isRunning():
                    w.cancel() if hasattr(w, "cancel") else None
            except Exception:
                pass
        super().closeEvent(e)

    def _auto_recover(self):
        if self.recover_worker and self.recover_worker.isRunning():
            return
        from core.masters_store import list_masters
        expired = [m.get("email") for m in list_masters()
                   if (m.get("status") or "active") == "expired"]
        if not expired:
            QMessageBox.information(
                self, "Tự động login lại",
                "Không có master nào ở trạng thái HẾT HẠN.")
            return
        QMessageBox.information(
            self, "Tự động login lại",
            "Tool sẽ tự mở Chrome đăng nhập lại các master hết hạn "
            f"({len(expired)}) bằng credential trong config/gmail.txt "
            "(dòng 'email|password|totp').\n\nChrome tự đóng khi xong.")
        self.btn_recover.setEnabled(False)
        self.lbl.setText("Đang tự động login lại master hết hạn...")
        self.recover_worker = AutoRecoverWorker()
        self.recover_worker.log.connect(lambda m: self.lbl.setText(m))
        self.recover_worker.done.connect(self._on_recover_done)
        self.recover_worker.start()

    def _on_recover_done(self, res):
        self.btn_recover.setEnabled(True)
        rec = res.get("recovered", [])
        skip = res.get("skipped", [])
        fail = res.get("failed", [])
        parts = []
        if rec:
            parts.append(f"✅ Khôi phục: {', '.join(rec)}")
        if skip:
            parts.append(f"⏭ Thiếu credential (thêm vào gmail.txt): {', '.join(skip)}")
        if fail:
            parts.append("❌ Lỗi: " + ", ".join(f"{e}" for e, _ in fail))
        summary = "\n".join(parts) or "Không có gì để làm."
        self.lbl.setText(summary.replace("\n", " | "))
        try:
            from core.master_pool import reset_shared_pool
            reset_shared_pool()   # master vua khoi phuc -> pool nap lai
        except Exception:
            pass
        QMessageBox.information(self, "Tự động login lại", summary)
        self._alive.clear()
        self._reload()

    def _reopen(self, email, refresh_token):
        if self.reopen_worker and self.reopen_worker.isRunning():
            QMessageBox.information(
                self, "Mở lại", "Đang có 1 session master mở rồi. Đóng nó trước.")
            return
        self.lbl.setText(f"Đang mở session master {email}...")
        self.reopen_worker = ReopenMasterWorker(email, refresh_token)
        self.reopen_worker.log.connect(lambda m: self.lbl.setText(m))
        self.reopen_worker.done.connect(self._on_reopen_done)
        self.reopen_worker.start()

    def _on_reopen_done(self, ok, msg):
        self.lbl.setText(msg)
        if not ok:
            hint = ""
            if any(k in str(msg).lower() for k in ("chet", "chết", "expired",
                                                   "token_expired", "refresh")):
                hint = ("\n\n➡ Master này HẾT HẠN refresh token. Bấm "
                        "'🔧 Tự động login lại (hết hạn)' để tool tự đăng nhập lại "
                        "(cần dòng 'email|password|totp' trong config/gmail.txt).")
            QMessageBox.warning(self, "Mở lại", "❌ " + msg + hint)
        self._reload()

    def _toggle(self, email, status):
        from core.masters_store import set_status
        set_status(email, "disabled" if status != "disabled" else "active")
        self._reload()

    def _delete(self, email, n_accounts):
        msg = f"Xóa master {email}?"
        if n_accounts:
            msg += (f"\n\n⚠ {n_accounts} TK đang gắn master này sẽ thành orphan "
                    f"-> lần 'Liên kết Master' sau sẽ tự re-link sang master khác "
                    f"(xóa master cũ khỏi workspace + mời master mới).")
        if QMessageBox.question(self, "Xóa master", msg,
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        from core.masters_store import remove_master
        remove_master(email)
        self._alive.pop(email, None)
        self._reload()

    def _bulk_master(self):
        dlg = MasterBulkInputDialog(self)
        dlg.exec_()
        self._alive.clear()
        self._reload()

    def _add_master(self):
        if self.add_worker and self.add_worker.isRunning():
            return
        QMessageBox.information(
            self, "Thêm Master",
            "Chrome sẽ mở — bạn ĐĂNG NHẬP bằng Google (1 account master sạch).\n"
            "Tool tự bắt refresh_token và lưu. (Chrome tự đóng khi xong.)")
        self.btn_add.setEnabled(False)
        self.lbl.setText("Đang mở Chrome để login master...")
        self.add_worker = AddMasterWorker()
        self.add_worker.log.connect(lambda m: self.lbl.setText(m))
        self.add_worker.done.connect(self._on_add_done)
        self.add_worker.start()

    def _on_add_done(self, ok, msg):
        self.btn_add.setEnabled(True)
        self.lbl.setText(msg)
        self._reload()
        if not ok:
            QMessageBox.warning(self, "Thêm Master", "❌ " + msg)

    def _check_alive(self):
        if self.check_worker and self.check_worker.isRunning():
            return
        from core.masters_store import list_masters
        masters = list_masters()
        if not masters:
            return
        self.btn_check.setEnabled(False)
        self.lbl.setText("Đang kiểm tra...")
        self.check_worker = MasterCheckWorker(masters)
        self.check_worker.result.connect(self._on_check_result)
        self.check_worker.done.connect(self._on_check_done)
        self.check_worker.start()

    def _on_check_result(self, email, alive):
        self._alive[email] = alive
        self._reload()

    def _on_check_done(self):
        self.btn_check.setEnabled(True)
        live = sum(1 for v in self._alive.values() if v)
        self.lbl.setText(f"Kiểm tra xong: {live} sống / {len(self._alive)}")


class SyncMasterWorker(QThread):
    """Lien ket TK voi master: login worker + moi master vao workspace.

    Chay song song nhieu luong. Luu login_refresh_token + master_onboarded.
    """
    progress = pyqtSignal(int, int, str, str)   # done, total, email, counts_text
    logmsg = pyqtSignal(str)                     # log chi tiet (IP, rotate, login)
    done = pyqtSignal(dict)

    def __init__(self, accounts, force=False, max_workers=1):
        super().__init__()
        self.accounts = accounts
        self.force = force
        # Login password co QUOTA_EXCEEDED -> phai TUAN TU (max_workers=1) + backoff.
        self.max_workers = max_workers
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        # Dung luong THONG NHAT: sync_accounts.sync (login 4G tuan tu + rotate khi
        # QUOTA + invite + accept + check ready). Khong chay song song (tranh quota).
        try:
            from tools.sync_accounts import sync
            stats = sync(
                force=self.force,
                log=lambda m: self.logmsg.emit(str(m)),
                on_progress=lambda d, t, e, c: self.progress.emit(d, t, e, c))
            self.done.emit(stats)
        except Exception as e:
            self.done.emit({"invited": 0, "already": 0, "fail": 0,
                            "ready": 0, "error": str(e)[:120]})


class QuickCheckWorker(QThread):
    """Check quota nhanh - KHONG can proxy, KHONG ton credit."""
    progress = pyqtSignal(int, int, str, str, int)
    done = pyqtSignal(dict)

    def __init__(self, accounts):
        super().__init__()
        self.accounts = accounts
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from core.convert import check_quota
        from core.mode_b_accounts import set_remaining

        stats = {"alive": 0, "exhausted": 0, "error": 0,
                 "total_chars": 0}
        total = len(self.accounts)

        def check_one(acc):
            email = acc["email"]
            api_key = acc.get("api_key", "")
            if not api_key:
                return email, "error", 0, 0
            try:
                q = check_quota(api_key, proxy=None)
                if q is None:
                    return email, "error", -1, 0
                remaining = q["chars_remaining"]
                reset = q.get("next_reset_unix", 0)   # LUU ngay reset -> auto-reset chay
                if remaining <= 0:
                    return email, "exhausted", 0, reset
                return email, "alive", remaining, reset
            except Exception:
                return email, "error", -1, 0

        checked = 0
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(check_one, acc): acc
                       for acc in self.accounts}
            for f in as_completed(futures):
                if self._cancelled:
                    break
                email, status, remaining, reset = f.result()
                checked += 1
                if status == "alive":
                    stats["alive"] += 1
                    stats["total_chars"] += remaining
                    set_remaining(email, remaining, reset)
                elif status == "exhausted":
                    stats["exhausted"] += 1
                    set_remaining(email, 0, reset)
                else:
                    stats["error"] += 1
                self.progress.emit(checked, total, email,
                                   status, remaining)
        self.done.emit(stats)


class ScanWorker(QThread):
    """Scan TK bang API Key + TTS test."""
    progress = pyqtSignal(int, int, dict)
    log = pyqtSignal(str)
    done = pyqtSignal(dict)

    def __init__(self, accounts, batch_size=20):
        super().__init__()
        self.accounts = accounts
        self.batch_size = batch_size
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        import requests
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from accounts.proxy import Proxy4G

        VOICE = "JBFqnCBsd6RMkjVDRZzb"
        p4g = Proxy4G()
        self.log.emit("Rotate IP truoc scan...")
        try:
            p4g.rotate(wait=45)
            ip = p4g.get_ip()
            self.log.emit(f"IP: {ip}")
        except Exception as e:
            self.log.emit(f"Rotate loi: {e}")

        proxy = p4g.get_for_requests()
        need_rotate = False
        lock = __import__('threading').Lock()

        def check_tk(acc):
            nonlocal need_rotate
            email = acc["email"]
            api_key = acc.get("api_key", "")
            result = {
                "email": email,
                "password": acc.get("password", ""),
                "api_key": api_key,
                "status": "unknown",
                "chars_remaining": 0,
                "error": "",
            }
            try:
                r = requests.get(
                    "https://api.elevenlabs.io/v1/user/subscription",
                    headers={"xi-api-key": api_key}, timeout=10)
                if r.status_code == 200:
                    d = r.json()
                    remaining = d["character_limit"] - d["character_count"]
                    result["chars_remaining"] = remaining
                    if remaining <= 0:
                        result["status"] = "exhausted"
                        return result
                else:
                    result["status"] = "dead"
                    return result
            except Exception:
                result["status"] = "dead"
                return result

            headers = {"xi-api-key": api_key,
                        "content-type": "application/json"}
            body = {"text": "Hi", "model_id": "eleven_flash_v2_5",
                    "voice_settings": {"stability": 0.5,
                                       "similarity_boost": 0.75}}
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}"
            try:
                r = requests.post(url, headers=headers, json=body,
                                  proxies=proxy, timeout=20)
                if r.status_code == 200 and len(r.content) > 100:
                    result["status"] = "alive"
                elif r.status_code == 401:
                    detail = r.json().get("detail", {})
                    status = detail.get("status", "")
                    if "unusual_activity" in status:
                        result["status"] = "flagged"
                        with lock:
                            need_rotate = True
                    else:
                        result["status"] = "dead"
                        result["error"] = status
                elif r.status_code == 402:
                    result["status"] = "alive"
                else:
                    result["status"] = "dead"
                    result["error"] = str(r.status_code)
            except Exception as e:
                result["status"] = "dead"
                result["error"] = str(e)[:30]
            return result

        results = []
        stats = {"alive": 0, "flagged": 0, "exhausted": 0, "dead": 0}
        i = 0
        while i < len(self.accounts) and not self._cancelled:
            batch = self.accounts[i:i + self.batch_size]
            with ThreadPoolExecutor(max_workers=5) as ex:
                futures = {ex.submit(check_tk, a): a for a in batch}
                for f in as_completed(futures):
                    if self._cancelled:
                        break
                    r = f.result()
                    results.append(r)
                    s = r["status"]
                    if s in stats:
                        stats[s] += 1
                    self.progress.emit(len(results),
                                       len(self.accounts), r)
            i += len(batch)
            if need_rotate and not self._cancelled:
                self.log.emit("IP flag -> rotate...")
                try:
                    p4g.rotate(wait=45)
                    proxy = p4g.get_for_requests()
                    self.log.emit(f"IP: {p4g.get_ip()}")
                except Exception:
                    pass
                need_rotate = False

        results.sort(key=lambda x: (
            0 if x["status"] == "alive" else
            1 if x["status"] == "exhausted" else
            2 if x["status"] == "flagged" else 3,
            -x["chars_remaining"]))
        with open(STATUS_JSON, 'w', encoding='utf-8') as f:
            json.dump({
                "scan_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "summary": stats,
                "accounts": results,
            }, f, indent=2, ensure_ascii=False)
        self.done.emit(stats)


class AccountsTab(QWidget):
    """Tab quan ly TK Mode B."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scan_worker = None
        self.check_worker = None
        self.sync_worker = None
        self._accounts = []
        self._init_ui()
        self._load_data()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # === Summary ===
        summary_box = QGroupBox("Tong quan")
        sl = QHBoxLayout(summary_box)

        self.lbl_total = QLabel("Tong: 0")
        self.lbl_total.setFont(QFont("", 11, QFont.Bold))
        sl.addWidget(self.lbl_total)

        self.lbl_alive = QLabel("Alive: 0")
        self.lbl_alive.setStyleSheet(
            f"color:{COLORS['alive']}; font-weight:bold;")
        sl.addWidget(self.lbl_alive)

        self.lbl_exhausted = QLabel("Exhausted: 0")
        self.lbl_exhausted.setStyleSheet(
            f"color:{COLORS['exhausted']}; font-weight:bold;")
        sl.addWidget(self.lbl_exhausted)

        self.lbl_flagged = QLabel("Flagged: 0")
        self.lbl_flagged.setStyleSheet(
            f"color:{COLORS['flagged']}; font-weight:bold;")
        sl.addWidget(self.lbl_flagged)

        self.lbl_dead = QLabel("Dead: 0")
        self.lbl_dead.setStyleSheet(
            f"color:{COLORS['dead']}; font-weight:bold;")
        sl.addWidget(self.lbl_dead)

        self.lbl_chars = QLabel("Chars: 0")
        self.lbl_chars.setStyleSheet("font-weight:bold; color:#2980b9;")
        sl.addWidget(self.lbl_chars)

        self.lbl_scan_time = QLabel("")
        self.lbl_scan_time.setStyleSheet("color:#888; font-size:10px;")
        sl.addWidget(self.lbl_scan_time)

        layout.addWidget(summary_box)

        # === MASTER (chong flag) - khu rieng, noi bat ===
        master_box = QGroupBox("MASTER (chong flag) — them master roi lien ket")
        master_box.setStyleSheet(
            "QGroupBox{font-weight:bold; border:2px solid #8e44ad; "
            "border-radius:6px; margin-top:8px; padding-top:6px;} "
            "QGroupBox::title{subcontrol-origin:margin; left:10px; "
            "color:#8e44ad;}")
        ml = QHBoxLayout(master_box)

        self.lbl_master_count = QLabel("Master: 0")
        self.lbl_master_count.setStyleSheet(
            "font-weight:bold; color:#8e44ad;")
        ml.addWidget(self.lbl_master_count)

        self.btn_sync_master = QPushButton("🔗 Liên kết Master")
        self.btn_sync_master.setStyleSheet(
            "font-weight:bold; background:#8e44ad; color:white; "
            "padding:6px 16px; border-radius:4px;")
        self.btn_sync_master.setToolTip(
            "Login tung TK + moi master vao workspace cua no (chia deu cho cac master).\n"
            "Sau buoc nay, generate se chay qua master (khong bi flag).\n"
            "Dung giua chung mo lai van chay tiep (resume), khong lam lai tu dau.")
        self.btn_sync_master.clicked.connect(self._start_sync_master)
        ml.addWidget(self.btn_sync_master)

        self.btn_manage_master = QPushButton("⚙ Quản lý Master")
        self.btn_manage_master.setStyleSheet(
            "font-weight:bold; background:#34495e; color:white; "
            "padding:6px 16px; border-radius:4px;")
        self.btn_manage_master.setToolTip(
            "Xem danh sach master, trang thai song/chet, so TK moi master,\n"
            "bat/tat hoac xoa master.")
        self.btn_manage_master.clicked.connect(self._open_master_manager)
        ml.addWidget(self.btn_manage_master)

        ml.addStretch()
        layout.addWidget(master_box)

        # === Buttons ===
        btn_row = QHBoxLayout()

        self.filter_combo = QComboBox()
        self.filter_combo.addItem("Tat ca", "all")
        self.filter_combo.addItem("Alive", "alive")
        self.filter_combo.addItem("Exhausted", "exhausted")
        self.filter_combo.addItem("Flagged", "flagged")
        self.filter_combo.addItem("Dead", "dead")
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)
        btn_row.addWidget(QLabel("Loc:"))
        btn_row.addWidget(self.filter_combo)

        btn_row.addStretch()

        self.btn_reload = QPushButton("Tai lai")
        self.btn_reload.clicked.connect(self._load_data)
        btn_row.addWidget(self.btn_reload)

        self.btn_import_file = QPushButton("Import file")
        self.btn_import_file.clicked.connect(self._import_from_file)
        btn_row.addWidget(self.btn_import_file)

        self.btn_import_text = QPushButton("Nhap TK")
        self.btn_import_text.clicked.connect(self._import_from_text)
        btn_row.addWidget(self.btn_import_text)

        self.btn_export_flagged = QPushButton("Export flagged TXT")
        self.btn_export_flagged.clicked.connect(self._export_flagged_txt)
        btn_row.addWidget(self.btn_export_flagged)

        self.btn_clear_all = QPushButton("🗑 Xóa hết TK")
        self.btn_clear_all.setToolTip(
            "Xóa TOÀN BỘ tài khoản worker (roster + danh sách thô + cache token).\n"
            "Dùng khi copy sang máy khác để nhập bộ TK mới.\n"
            "KHÔNG xóa master (quản riêng ở 'Quản lý Master').")
        self.btn_clear_all.setStyleSheet(
            "background:#c0392b; color:white; padding:6px 12px; border-radius:4px;")
        self.btn_clear_all.clicked.connect(self._clear_all_accounts)
        btn_row.addWidget(self.btn_clear_all)

        self.btn_quick = QPushButton("Check nhanh (quota)")
        self.btn_quick.setStyleSheet(
            "font-weight:bold; background:#27ae60; color:white; "
            "padding:6px 16px; border-radius:4px;")
        self.btn_quick.setToolTip(
            "Check quota tat ca TK alive\n"
            "Khong can proxy, khong ton credit\n"
            "Nhanh ~1 phut")
        self.btn_quick.clicked.connect(self._start_quick_check)
        btn_row.addWidget(self.btn_quick)

        self.btn_scan = QPushButton("Scan day du (TTS)")
        self.btn_scan.setStyleSheet(
            "font-weight:bold; background:#2980b9; color:white; "
            "padding:6px 16px; border-radius:4px;")
        self.btn_scan.setToolTip(
            "Scan tat ca TK + test TTS\n"
            "Can proxy, ton 1 credit/TK\n"
            "Phat hien TK flagged")
        self.btn_scan.clicked.connect(self._start_scan)
        btn_row.addWidget(self.btn_scan)

        self.btn_stop = QPushButton("Dung")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_all)
        btn_row.addWidget(self.btn_stop)

        layout.addLayout(btn_row)

        # === Progress ===
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(18)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.lbl_progress = QLabel("")
        self.lbl_progress.setStyleSheet(
            "font-size:12px; font-weight:bold; color:#2c3e50;")
        layout.addWidget(self.lbl_progress)

        # dong phu: log chi tiet (IP 4G, rotate, login...) - mo nhat
        self.lbl_detail = QLabel("")
        self.lbl_detail.setStyleSheet("font-size:10px; color:#999;")
        layout.addWidget(self.lbl_detail)

        # === Table ===
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ["Email", "Trang thai", "Chars con", "API Key", "Loi"])
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Fixed)
        h.resizeSection(3, 120)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.setStyleSheet(
            "QTableWidget { font-size:11px; }"
            "QTableWidget::item { padding: 2px 4px; }")
        layout.addWidget(self.table, 1)

    def _current_scan_time(self):
        text = self.lbl_scan_time.text().strip()
        prefix = "Cap nhat: "
        if text.startswith(prefix):
            return text[len(prefix):].strip()
        return ""

    def _merge_accounts(self, imported):
        existing = {}
        for acc in self._accounts:
            email = acc.get("email", "").strip().lower()
            if email:
                existing[email] = dict(acc)

        merged = []
        used_keys = set()
        for acc in imported:
            key = acc["email"].strip().lower()
            used_keys.add(key)
            prev = existing.get(key)
            if prev:
                prev_password = (prev.get("password") or "").strip()
                prev_api_key = (prev.get("api_key") or "").strip()
                new_password = (acc.get("password") or "").strip()
                new_api_key = (acc.get("api_key") or "").strip()
                credential_changed = (
                    prev_password != new_password or prev_api_key != new_api_key
                )

                prev.update({
                    "email": acc["email"],
                    "password": new_password,
                    "api_key": new_api_key,
                })
                if credential_changed:
                    prev["status"] = "unknown"
                    prev["chars_remaining"] = 0
                    prev["error"] = ""
                merged.append(prev)
            else:
                merged.append(dict(acc))

        for key, acc in existing.items():
            if key not in used_keys:
                merged.append(acc)

        return merged

    def _import_accounts(self, raw_text, source_name=""):
        imported = _parse_accounts_text(raw_text)
        if not imported:
            QMessageBox.information(
                self, "Info", "Khong tim thay dong tai khoan hop le.")
            return

        merged = self._merge_accounts(imported)
        _write_raw_accounts(merged)
        _save_accounts_json(merged, scan_time=self._current_scan_time())

        self._load_data()
        src = f" tu {source_name}" if source_name else ""
        QMessageBox.information(
            self,
            "Thanh cong",
            f"Da nap {len(imported)} TK{src}.\n"
            f"Danh sach hien tai: {len(merged)} TK.\n"
            "TK moi duoc dat trang thai UNKNOWN, co the bam 'Check nhanh' de cap nhat quota.",
        )

    def _import_from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Chon file tai khoan",
            PROJECT_ROOT,
            "Text files (*.txt *.csv);;All files (*.*)",
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                raw_text = f.read()
            self._import_accounts(raw_text, source_name=os.path.basename(path))
        except Exception as e:
            QMessageBox.warning(self, "Loi import", str(e))

    def _import_from_text(self):
        sample = "email1@gmail.com|matkhau1|api_key_1\nemail2@gmail.com|matkhau2|api_key_2"
        text, ok = QInputDialog.getMultiLineText(
            self,
            "Nhap danh sach tai khoan",
            "Moi dong: email|password|api_key\n(api_key co the de trong neu chua co)",
            sample,
        )
        if not ok or not text.strip():
            return
        try:
            self._import_accounts(text, source_name="noi dung da dan")
        except Exception as e:
            QMessageBox.warning(self, "Loi import", str(e))

    def _clear_all_accounts(self):
        """Xoa toan bo TK worker (roster + txt tho + cache token). Giu master."""
        n = len(self._accounts)
        reply = QMessageBox.warning(
            self, "Xóa hết tài khoản",
            f"Sẽ XÓA TOÀN BỘ {n} tài khoản worker:\n"
            f"  • config/1000tk_real_status.json (roster)\n"
            f"  • config/1000tk.txt (danh sách thô)\n"
            f"  • config/firebase_tokens.db (cache token)\n\n"
            f"Master KHÔNG bị xóa (quản ở 'Quản lý Master').\n\n"
            f"Hành động này KHÔNG hoàn tác được. Tiếp tục?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        # xac nhan lan 2 (tranh bam nham)
        reply2 = QMessageBox.question(
            self, "Xác nhận lần cuối",
            "Bạn CHẮC CHẮN xóa hết tài khoản?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply2 != QMessageBox.Yes:
            return

        errs = []
        # 1) roster -> rong
        try:
            with open(STATUS_JSON, "w", encoding="utf-8") as f:
                json.dump({"scan_time": "", "summary": {}, "accounts": []},
                          f, indent=2, ensure_ascii=False)
        except Exception as e:
            errs.append(f"roster: {e}")
        # 2) txt tho -> rong
        try:
            if os.path.exists(RAW_TXT):
                open(RAW_TXT, "w", encoding="utf-8").close()
        except Exception as e:
            errs.append(f"txt: {e}")
        # 3) cache token -> xoa file (tu tao lai)
        try:
            dbp = os.path.join(PROJECT_ROOT, "config", "firebase_tokens.db")
            if os.path.exists(dbp):
                os.remove(dbp)
        except Exception as e:
            errs.append(f"token db: {e} (co the dang mo, khong sao)")

        self._load_data()
        if errs:
            QMessageBox.information(
                self, "Đã xóa",
                "Đã xóa TK. Một vài mục cần khởi động lại tool:\n" + "\n".join(errs))
        else:
            QMessageBox.information(
                self, "Đã xóa",
                "✅ Đã xóa toàn bộ tài khoản worker.\n"
                "Giờ bấm 'Nhập TK' để nhập bộ tài khoản mới.")

    def _export_flagged_txt(self):
        flagged = [a for a in self._accounts if a.get("status") == "flagged"]
        if not flagged:
            QMessageBox.information(self, "Info", "Khong co TK flagged de xuat.")
            return

        default_name = os.path.join(
            PROJECT_ROOT,
            "config",
            f"flagged_accounts_{time.strftime('%Y%m%d_%H%M%S')}.txt",
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Luu danh sach TK flagged",
            default_name,
            "Text files (*.txt);;All files (*.*)",
        )
        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8") as f:
                for acc in flagged:
                    f.write(
                        f"{acc.get('email', '')}|{acc.get('password', '')}|{acc.get('api_key', '')}\n"
                    )
            QMessageBox.information(
                self,
                "Thanh cong",
                f"Da xuat {len(flagged)} TK flagged ra:\n{path}",
            )
        except Exception as e:
            QMessageBox.warning(self, "Loi export", str(e))

    def _load_data(self):
        """Load tu JSON."""
        self._accounts = []

        if os.path.exists(STATUS_JSON):
            with open(STATUS_JSON, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self._accounts = data.get("accounts", [])
            scan_time = data.get("scan_time",
                                 data.get("last_updated", ""))
            self.lbl_scan_time.setText(f"Cap nhat: {scan_time}")
        elif os.path.exists(RAW_TXT):
            with open(RAW_TXT, 'r', encoding='utf-8', errors='ignore') as f:
                self._accounts = _parse_accounts_text(f.read())
            self.lbl_scan_time.setText("Chua scan")

        self._update_summary()
        self._update_master_count()
        self._apply_filter()

    def _update_master_count(self):
        """Hien thi so master dang co (de biet da them master chua)."""
        try:
            from core.masters_store import list_masters, count_active
            total = len(list_masters())
            active = count_active()
            self.lbl_master_count.setText(f"Master: {active}/{total}")
            if active == 0:
                self.lbl_master_count.setStyleSheet(
                    "font-weight:bold; color:#e74c3c;")
                self.lbl_master_count.setToolTip(
                    "Chua co master! Bam 'Them Master' truoc khi 'Lien ket Master'.")
            else:
                self.lbl_master_count.setStyleSheet(
                    "font-weight:bold; color:#8e44ad;")
                self.lbl_master_count.setToolTip("")
        except Exception:
            self.lbl_master_count.setText("Master: ?")

    def _update_summary(self):
        stats = {"alive": 0, "exhausted": 0, "flagged": 0, "dead": 0}
        total_chars = 0
        for acc in self._accounts:
            s = acc.get("status", "unknown")
            if s in stats:
                stats[s] += 1
            if s == "alive":
                total_chars += acc.get("chars_remaining", 0)

        total = len(self._accounts)
        self.lbl_total.setText(f"Tong: {total}")
        self.lbl_alive.setText(f"Alive: {stats['alive']}")
        self.lbl_exhausted.setText(f"Exhausted: {stats['exhausted']}")
        self.lbl_flagged.setText(f"Flagged: {stats['flagged']}")
        self.lbl_dead.setText(f"Dead: {stats['dead']}")
        self.lbl_chars.setText(f"Chars: {total_chars:,}")

    def _apply_filter(self):
        """Loc bang theo trang thai."""
        filt = self.filter_combo.currentData()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)

        for acc in self._accounts:
            status = acc.get("status", "unknown")
            if filt != "all" and status != filt:
                continue

            row = self.table.rowCount()
            self.table.insertRow(row)

            self.table.setItem(row, 0,
                               QTableWidgetItem(acc["email"]))

            status_item = QTableWidgetItem(status.upper())
            color = COLORS.get(status, "#888")
            status_item.setForeground(QColor(color))
            status_item.setFont(QFont("", -1, QFont.Bold))
            self.table.setItem(row, 1, status_item)

            chars = acc.get("chars_remaining", 0)
            chars_item = QTableWidgetItem(f"{chars:,}")
            chars_item.setTextAlignment(
                Qt.AlignRight | Qt.AlignVCenter)
            if chars > 0:
                chars_item.setForeground(QColor(COLORS["alive"]))
            self.table.setItem(row, 2, chars_item)

            key = acc.get("api_key", "")
            masked = key[:8] + "..." + key[-4:] if len(key) > 12 else key
            self.table.setItem(row, 3,
                               QTableWidgetItem(masked))

            self.table.setItem(row, 4,
                               QTableWidgetItem(acc.get("error", "")))

        self.table.setSortingEnabled(True)

    # === QUICK CHECK ===

    def _start_quick_check(self):
        if (self.check_worker and self.check_worker.isRunning()) or \
           (self.scan_worker and self.scan_worker.isRunning()):
            return

        alive = [a for a in self._accounts
                 if a.get("status") in ("alive", "unknown")]
        if not alive:
            self._load_data()
            alive = [a for a in self._accounts
                     if a.get("status") in ("alive", "unknown")]
        if not alive:
            QMessageBox.information(self, "Info",
                                    "Khong co TK alive de check!")
            return

        self.btn_quick.setEnabled(False)
        self.btn_scan.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(alive))
        self.progress_bar.setValue(0)
        self.lbl_progress.setText(f"Check {len(alive)} TK...")

        self.check_worker = QuickCheckWorker(alive)
        self.check_worker.progress.connect(self._on_quick_progress)
        self.check_worker.done.connect(self._on_quick_done)
        self.check_worker.start()

    def _on_quick_progress(self, current, total, email, status,
                           remaining):
        self.progress_bar.setValue(current)
        if status == "alive":
            self.lbl_progress.setText(
                f"{current}/{total} | {email}: {remaining:,} chars")
        else:
            self.lbl_progress.setText(
                f"{current}/{total} | {email}: {status}")

    def _on_quick_done(self, stats):
        self.btn_quick.setEnabled(True)
        self.btn_scan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_bar.setVisible(False)
        total_chars = stats.get("total_chars", 0)
        self.lbl_progress.setText(
            f"Check xong! Alive: {stats['alive']} "
            f"({total_chars:,} chars) | "
            f"Exhausted: {stats['exhausted']} | "
            f"Error: {stats['error']}")
        self._load_data()
        # BAO CAO QUOTA TOAN DOI (con bao nhieu, reset ngay nao, budget/ngay)
        try:
            from core.quota_report import fleet_report, format_report
            rep = fleet_report()
            QMessageBox.information(self, "📊 Báo cáo Quota toàn đội",
                                    format_report(rep))
        except Exception:
            pass

    # === QUAN LY MASTER (them / xem / xoa / mo lai) ===

    def _open_master_manager(self):
        dlg = MasterManagerDialog(self)
        dlg.exec_()
        self._update_master_count()

    # === LIEN KET MASTER (onboard) ===

    def _start_sync_master(self):
        if self.sync_worker and self.sync_worker.isRunning():
            return
        if not self._accounts:
            self._load_data()
        cand = [a for a in self._accounts
                if ((a.get("password") or "").strip()
                    or (a.get("login_refresh_token") or "").strip())]
        if not cand:
            QMessageBox.warning(
                self, "Lỗi",
                "Không có TK nào có password hoặc login_refresh_token để liên kết!")
            return

        # CHUA READY = master chua accept (gom ca TK da moi nhung accept chua xong).
        not_done = [a for a in cand if not a.get("master_ready")
                    and a.get("status") != "dead"]
        force = False
        if not not_done:
            reply = QMessageBox.question(
                self, "Liên kết Master",
                f"Tất cả {len(cand)} TK đã sẵn sàng.\n"
                f"Liên kết lại (force) toàn bộ?",
                QMessageBox.Yes | QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
            force = True

        n = len(cand) if force else len(not_done)
        self.btn_sync_master.setEnabled(False)
        self.btn_quick.setEnabled(False)
        self.btn_scan.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(max(1, n))
        self.progress_bar.setValue(0)
        self.lbl_progress.setText(f"Đang liên kết {n} TK với master... (đang khởi động)")
        self.lbl_detail.setText("")

        self.sync_worker = SyncMasterWorker(self._accounts, force=force)
        self.sync_worker.progress.connect(self._on_sync_progress)
        self.sync_worker.logmsg.connect(self._on_sync_log)
        self.sync_worker.done.connect(self._on_sync_done)
        self.sync_worker.start()

    def _on_sync_progress(self, done_n, total, email, counts):
        # tien do truc quan: thanh chay + dem OK/dead/fail + TK hien tai
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(done_n)
            pct = int(done_n * 100 / total)
            self.lbl_progress.setText(
                f"{done_n}/{total} ({pct}%)   {counts}   →  {email}")
        else:
            self.lbl_progress.setText(f"{counts}  →  {email}")

    def _on_sync_log(self, msg):
        # log chi tiet (IP 4G, rotate, login OK...) o dong phu mo nhat
        self.lbl_detail.setText(str(msg)[:140])

    def _on_sync_done(self, stats):
        self.btn_sync_master.setEnabled(True)
        self.btn_quick.setEnabled(True)
        self.btn_scan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_bar.setVisible(False)
        ready = stats.get("ready", 0)
        alive = stats.get("alive", 0)
        err = stats.get("error", "")
        self.lbl_progress.setText(
            f"Liên kết xong! ALIVE(còn quota): {alive} | READY: {ready} | "
            f"dead: {stats.get('dead',0)}")
        msg = (f"Hoàn tất liên kết master.\n\n"
               f"✅ ALIVE (còn quota, dùng được): {alive}\n"
               f"READY (master đã là member): {ready}\n"
               f"Mới mời: {stats.get('invited',0)}\n"
               f"Đã thành viên: {stats.get('already',0)}\n"
               f"TK chết (sai pass): {stats.get('dead',0)}\n"
               f"Lỗi khác: {stats.get('fail',0)}\n")
        if err:
            msg += f"\n⚠ Lỗi: {err}"
        msg += "\nGiờ sang Auto/Voice Convert để tạo voice."
        QMessageBox.information(self, "Liên kết Master", msg)
        self._load_data()

    # === FULL SCAN ===

    def _start_scan(self):
        if (self.scan_worker and self.scan_worker.isRunning()) or \
           (self.check_worker and self.check_worker.isRunning()):
            return

        if not self._accounts:
            self._load_data()
        if not self._accounts:
            QMessageBox.warning(self, "Loi",
                                "Khong co TK nao de scan!")
            return

        reply = QMessageBox.question(
            self, "Scan",
            f"Scan {len(self._accounts)} TK?\n"
            f"Can proxy + ton 1 credit/TK.\n"
            f"Se mat ~{len(self._accounts) // 20}+ phut.",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        self.btn_scan.setEnabled(False)
        self.btn_quick.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(self._accounts))
        self.progress_bar.setValue(0)

        self.scan_worker = ScanWorker(self._accounts)
        self.scan_worker.progress.connect(self._on_scan_progress)
        self.scan_worker.log.connect(self._on_scan_log)
        self.scan_worker.done.connect(self._on_scan_done)
        self.scan_worker.start()

    def _stop_all(self):
        if self.scan_worker:
            self.scan_worker.cancel()
        if self.check_worker:
            self.check_worker.cancel()
        if self.sync_worker:
            self.sync_worker.cancel()

    def _on_scan_progress(self, current, total, result):
        self.progress_bar.setValue(current)
        self.lbl_progress.setText(
            f"{current}/{total} -- {result['email']}: "
            f"{result['status']}")

    def _on_scan_log(self, msg):
        self.lbl_progress.setText(msg)

    def _on_scan_done(self, stats):
        self.btn_scan.setEnabled(True)
        self.btn_quick.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_bar.setVisible(False)
        self.lbl_progress.setText(
            f"Scan xong! alive={stats.get('alive', 0)} "
            f"flagged={stats.get('flagged', 0)} "
            f"exhausted={stats.get('exhausted', 0)} "
            f"dead={stats.get('dead', 0)}")
        self._load_data()
