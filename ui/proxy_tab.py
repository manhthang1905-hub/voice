"""
Tab 4G Proxy — Quản lý & giám sát 4G proxy.
Gọi API tới gateway 192.168.88.254:19800.
"""

import json
import time
import threading

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QGridLayout, QTextEdit, QFrame,
    QDialog, QLineEdit, QFormLayout, QMessageBox,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont

import urllib.request as urlreq


def _api(path, method="GET", timeout=10):
    """Gọi API 4G gateway (đọc host/port/key động từ config -> đổi được runtime)."""
    from accounts.proxy import API_BASE, API_KEY
    req = urlreq.Request(
        f"{API_BASE}{path}", method=method,
        headers={"X-API-Key": API_KEY,
                 "Content-Type": "application/json"})
    if method == "POST":
        req.data = b"{}"
    return json.loads(urlreq.urlopen(req, timeout=timeout).read())


class ProxySettingsDialog(QDialog):
    """Chinh cau hinh 4G proxy (host/port/key) -> luu proxy.json, ap dung ngay."""

    FIELDS = [
        ("socks5_host", "SOCKS5 Host (IP máy chạy 4G)",
         "PC có phone: 127.0.0.1  |  Máy ảo/khác: IP máy chính (vd 192.168.88.254)"),
        ("socks5_port", "SOCKS5 Port",
         "PC: 10001  |  Máy ảo qua LAN relay: 10002"),
        ("api_host", "API Host (IP máy chạy 4G server)",
         "Thường giống SOCKS5 Host"),
        ("api_port", "API Port", "Mặc định 19800"),
        ("gateway_port", "Gateway Port", "Mặc định 5000"),
        ("api_key", "API Key", "Mặc định mimi-4g-proxy-2026"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cài đặt 4G Proxy")
        self.resize(480, 360)
        self.edits = {}
        self._build()

    def _build(self):
        v = QVBoxLayout(self)
        info = QLabel(
            "Sửa thông số 4G khi copy tool sang máy khác (IP/cổng khác nhau). "
            "Lưu xong áp dụng ngay, không cần khởi động lại.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#555; font-size:11px;")
        v.addWidget(info)

        from accounts.proxy import get_config
        cfg = get_config()

        form = QFormLayout()
        for key, label, hint in self.FIELDS:
            e = QLineEdit(str(cfg.get(key, "")))
            e.setToolTip(hint)
            e.setPlaceholderText(hint)
            self.edits[key] = e
            form.addRow(QLabel(label), e)
        v.addLayout(form)

        row = QHBoxLayout()
        btn_test = QPushButton("🧪 Test kết nối")
        btn_test.clicked.connect(self._test)
        row.addWidget(btn_test)
        self.lbl_test = QLabel("")
        self.lbl_test.setStyleSheet("font-size:11px;")
        row.addWidget(self.lbl_test, 1)
        btn_save = QPushButton("💾 Lưu & áp dụng")
        btn_save.setStyleSheet(
            "font-weight:bold; background:#238636; color:white; padding:6px 14px;")
        btn_save.clicked.connect(self._save)
        row.addWidget(btn_save)
        btn_cancel = QPushButton("Hủy")
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(btn_cancel)
        v.addLayout(row)

    def _collect(self):
        out = {}
        for key, _, _ in self.FIELDS:
            txt = self.edits[key].text().strip()
            if key in ("socks5_port", "api_port", "gateway_port"):
                try:
                    out[key] = int(txt)
                except ValueError:
                    out[key] = None
            else:
                out[key] = txt
        return out

    def _save(self):
        from accounts.proxy import save_config
        try:
            save_config(self._collect())
            QMessageBox.information(
                self, "Cài đặt 4G", "✅ Đã lưu & áp dụng. (Có thể bấm Refresh để kiểm tra)")
            self.accept()
        except Exception as e:
            QMessageBox.warning(self, "Lỗi", f"Lưu lỗi: {str(e)[:120]}")

    def _test(self):
        """Luu tam roi goi /list de kiem tra ket noi."""
        from accounts.proxy import save_config
        self.lbl_test.setText("Đang test...")
        try:
            save_config(self._collect())
            data = _api("/list", timeout=8)
            n = len(data.get("proxies", []))
            self.lbl_test.setText(f"✅ OK — {n} thiết bị")
            self.lbl_test.setStyleSheet("color:#27ae60; font-size:11px;")
        except Exception as e:
            self.lbl_test.setText(f"❌ {str(e)[:50]}")
            self.lbl_test.setStyleSheet("color:#e74c3c; font-size:11px;")


class DeviceCard(QFrame):
    """Card hiển thị 1 phone/device."""
    action_signal = pyqtSignal(str, str)  # action, device_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self.device_id = ""
        self.setFrameStyle(QFrame.StyledPanel)
        self.setStyleSheet("""
            DeviceCard {
                background: #2d2d2d;
                border: 1px solid #444;
                border-radius: 6px;
                padding: 4px;
            }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)

        # Status dot
        self.dot = QLabel("●")
        self.dot.setFixedWidth(20)
        self.dot.setFont(QFont("", 14))
        layout.addWidget(self.dot)

        # Info
        self.lbl_info = QLabel()
        self.lbl_info.setFont(QFont("Consolas", 10))
        self.lbl_info.setStyleSheet("color: #ddd;")
        layout.addWidget(self.lbl_info, 1)

        # Buttons
        btn_rotate = QPushButton("🔄 Rotate")
        btn_rotate.setFixedWidth(80)
        btn_rotate.setStyleSheet(
            "background:#9e6a03; color:white; border-radius:3px; "
            "padding:4px; font-size:11px;")
        btn_rotate.clicked.connect(
            lambda: self.action_signal.emit("rotate", self.device_id))
        layout.addWidget(btn_rotate)

        btn_test = QPushButton("🧪 Test")
        btn_test.setFixedWidth(60)
        btn_test.setStyleSheet(
            "background:#2980b9; color:white; border-radius:3px; "
            "padding:4px; font-size:11px;")
        btn_test.clicked.connect(
            lambda: self.action_signal.emit("test", self.device_id))
        layout.addWidget(btn_test)

    def update_data(self, phone):
        self.device_id = phone.get("id", "")
        ip = phone.get("current_4g_ip", "")
        running = phone.get("proxy_running", False)
        name = phone.get("name", "?")
        port = phone.get("port", "")
        rotates = phone.get("rotate_count", 0)

        if running and ip:
            self.dot.setStyleSheet("color: #3fb950;")
            status = "Online"
        elif running:
            self.dot.setStyleSheet("color: #d29922;")
            status = "No IP"
        else:
            self.dot.setStyleSheet("color: #da3633;")
            status = "Offline"

        self.lbl_info.setText(
            f"{name}  [{status}]  "
            f"IP: {ip or 'N/A'}  "
            f":{port}  x{rotates}")


class ProxyTab(QWidget):
    """Tab 4G Proxy trong main GUI."""
    _log_signal = pyqtSignal(str)
    _update_signal = pyqtSignal(list)
    _status_signal = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards = []
        self._init_ui()
        self._log_signal.connect(self._append_log)
        self._update_signal.connect(self._render_phones)
        self._status_signal.connect(
            lambda s: self.lbl_status.setText(s))

        # Auto-refresh mỗi 10s
        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh_bg)
        self._timer.start(10000)

        # Refresh lần đầu
        self._refresh_bg()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # === Summary row ===
        summary = QGroupBox("4G Proxy")
        sl = QHBoxLayout(summary)

        self.lbl_status = QLabel("Đang kết nối...")
        self.lbl_status.setFont(QFont("", 11, QFont.Bold))
        sl.addWidget(self.lbl_status)

        self.lbl_ip = QLabel("IP: --")
        self.lbl_ip.setStyleSheet(
            "color:#3fb950; font-weight:bold; font-size:12px;")
        sl.addWidget(self.lbl_ip)

        self.lbl_gateway = QLabel("")
        self.lbl_gateway.setStyleSheet("color:#888; font-size:10px;")
        sl.addWidget(self.lbl_gateway, 1)
        self._update_gateway_label()

        btn_settings = QPushButton("⚙ Cài đặt 4G")
        btn_settings.setToolTip(
            "Đổi IP/cổng 4G khi copy tool sang máy khác.")
        btn_settings.setStyleSheet(
            "font-weight:bold; background:#34495e; color:white; "
            "padding:5px 12px; border-radius:4px;")
        btn_settings.clicked.connect(self._open_settings)
        sl.addWidget(btn_settings)

        layout.addWidget(summary)

        # === Buttons ===
        btn_row = QHBoxLayout()

        btn_refresh = QPushButton("🔄 Refresh")
        btn_refresh.clicked.connect(self._refresh_bg)
        btn_row.addWidget(btn_refresh)

        btn_rotate = QPushButton("🔄 Rotate All")
        btn_rotate.setStyleSheet(
            "background:#9e6a03; color:white; font-weight:bold; "
            "padding:6px 16px; border-radius:4px;")
        btn_rotate.clicked.connect(self._rotate_all)
        btn_row.addWidget(btn_rotate)

        btn_scan = QPushButton("📡 Scan Devices")
        btn_scan.setStyleSheet(
            "background:#2980b9; color:white; "
            "padding:6px 12px; border-radius:4px;")
        btn_scan.clicked.connect(self._scan)
        btn_row.addWidget(btn_scan)

        btn_start = QPushButton("▶ Start All")
        btn_start.setStyleSheet(
            "background:#238636; color:white; "
            "padding:6px 12px; border-radius:4px;")
        btn_start.clicked.connect(
            lambda: self._api_action("/start", "POST", "Started"))
        btn_row.addWidget(btn_start)

        btn_stop = QPushButton("⏹ Stop All")
        btn_stop.setStyleSheet(
            "background:#da3633; color:white; "
            "padding:6px 12px; border-radius:4px;")
        btn_stop.clicked.connect(
            lambda: self._api_action("/stop", "POST", "Stopped"))
        btn_row.addWidget(btn_stop)

        btn_row.addStretch()
        layout.addLayout(btn_row)

        # === Devices ===
        self.devices_layout = QVBoxLayout()
        self.devices_layout.setSpacing(4)
        layout.addLayout(self.devices_layout)

        # Placeholder
        self.placeholder = QLabel(
            "Đang tải danh sách thiết bị...")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet(
            "color:#888; font-size:12px; padding:20px;")
        self.devices_layout.addWidget(self.placeholder)

        layout.addStretch(1)

        # === Log ===
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMaximumHeight(120)
        self.log_area.setStyleSheet(
            "font-family:Consolas; font-size:10px; "
            "background:#1e1e1e; color:#ddd;")
        layout.addWidget(self.log_area)

    def _update_gateway_label(self):
        try:
            from accounts.proxy import GATEWAY, API_BASE
            self.lbl_gateway.setText(f"Gateway: {GATEWAY}  |  API: {API_BASE}")
        except Exception:
            self.lbl_gateway.setText("")

    def _open_settings(self):
        dlg = ProxySettingsDialog(self)
        if dlg.exec_():
            self._update_gateway_label()
            self._refresh_bg()

    def _append_log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log_area.append(f"[{ts}] {msg}")
        sb = self.log_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _refresh_bg(self):
        def _do():
            try:
                data = _api("/list")
                phones = data.get("proxies", [])
                self._update_signal.emit(phones)

                # Get current IP
                from accounts.proxy import Proxy4G
                ip = Proxy4G().get_ip()
                self._status_signal.emit(
                    f"Online — {len(phones)} thiết bị")
                self._log_signal.emit(f"IP: {ip}")
                # Update IP label from main thread
                self._update_signal.emit(phones)
            except Exception as e:
                self._status_signal.emit(f"⚠ Lỗi: {str(e)[:40]}")
        threading.Thread(target=_do, daemon=True).start()

    def _render_phones(self, phones):
        # Clear old cards
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()

        if self.placeholder:
            self.placeholder.setParent(None)
            self.placeholder = None

        if not phones:
            lbl = QLabel("Không có thiết bị. Bấm 'Scan Devices'.")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color:#888; padding:20px;")
            self.devices_layout.addWidget(lbl)
            self._cards.append(lbl)
            return

        on = sum(1 for p in phones if p.get("proxy_running"))
        rot = sum(p.get("rotate_count", 0) for p in phones)

        # Get IP (đã lấy từ background thread, không gọi lại ở main thread)
        ip_text = "IP: --"
        for p in phones:
            ip = p.get("current_4g_ip", "")
            if ip:
                ip_text = f"IP: {ip}"
                break
        self.lbl_ip.setText(ip_text)
        self.lbl_status.setText(
            f"Online — {len(phones)} thiết bị | "
            f"{on} online | {rot} rotations")

        for phone in phones:
            card = DeviceCard()
            card.update_data(phone)
            card.action_signal.connect(self._on_device_action)
            self.devices_layout.addWidget(card)
            self._cards.append(card)

    def _on_device_action(self, action, device_id):
        if action == "rotate":
            self._log_signal.emit(f"Rotating {device_id}...")
            def _do():
                try:
                    d = _api(f"/rotate/{device_id}", "POST",
                             timeout=40)
                    self._log_signal.emit(
                        f"New IP: {d.get('new_ip', '?')}")
                    self._refresh_bg()
                except Exception as e:
                    self._log_signal.emit(f"Rotate lỗi: {e}")
            threading.Thread(target=_do, daemon=True).start()

        elif action == "test":
            self._log_signal.emit(f"Testing {device_id}...")
            def _do():
                try:
                    d = _api(f"/test/{device_id}", timeout=15)
                    if d.get("ok"):
                        self._log_signal.emit(
                            f"Test OK: {d['ip']} | "
                            f"{d.get('org', '')} | "
                            f"{d.get('latency_ms', '')}ms")
                    else:
                        self._log_signal.emit(
                            f"Test FAIL: {d.get('error', '')}")
                except Exception as e:
                    self._log_signal.emit(f"Test lỗi: {e}")
            threading.Thread(target=_do, daemon=True).start()

    def _rotate_all(self):
        self._log_signal.emit("Rotating all devices...")
        self._api_action("/rotate-all", "POST", "All rotated")

    def _scan(self):
        self._log_signal.emit("Scanning for devices...")
        def _do():
            try:
                data = _api("/scan", "POST", timeout=30)
                ready = data.get("ready_count", 0)
                self._log_signal.emit(
                    f"Scan done: {ready} devices ready")
                self._refresh_bg()
            except Exception as e:
                self._log_signal.emit(f"Scan lỗi: {e}")
        threading.Thread(target=_do, daemon=True).start()

    def _api_action(self, path, method, msg):
        def _do():
            try:
                _api(path, method, timeout=40)
                self._log_signal.emit(msg)
                self._refresh_bg()
            except Exception as e:
                self._log_signal.emit(f"Lỗi: {e}")
        threading.Thread(target=_do, daemon=True).start()
