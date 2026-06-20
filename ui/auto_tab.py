"""
Auto Convert Tab — chạy liên tục, tự quét + chia + convert.

Flow:
1. Đọc Google Sheet → voice_map + folder_map
2. Quét voice/*.txt → copy vào folder kênh (check ổn định)
3. Quét subfolder → convert TXT chưa có MP3
4. Nghỉ → lặp lại
"""

import os
import re
import time
import shutil

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QLineEdit, QSpinBox, QTextEdit,
    QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QFileDialog,
    QDialog, QFormLayout, QDoubleSpinBox, QComboBox, QCheckBox, QMessageBox,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QColor, QFont

from utils.logger import log
from utils.config import Config
try:
    from utils.lang_detect import detect_language_from_files
    _LANG_DETECT_OK = True
except Exception:
    _LANG_DETECT_OK = False
    def detect_language_from_files(files, **kw):
        return ""

# Path mặc định trên VM
DEFAULT_VOICE_DIR = r"C:\Users\Administrator\Desktop\voice\voice"

# Regex extract code: KA5-0001, SB1-0002, AR1-0003
CODE_REGEX = re.compile(r"([A-Za-z0-9]+-\d{4})")

# TL1 folders have fixed languages from the channel plan.
TL1_LANGUAGE_MAP = {
    "TL1-T1": "es",
    "TL1-T2": "vi",
    "TL1-T3": "en",
    "TL1-T4": "fr",
    "TL1-T5": "de",
    "TL1-T6": "pt",
    "TL1-T7": "ja",
    "TL1-T8": "ko",
    "TL1-T9": "it",
    "TL1-T10": "tr",
}


def extract_code(text):
    m = CODE_REGEX.search(text)
    return m.group(1) if m else None


def file_stable(path, wait=10):
    """Check file không thay đổi trong `wait` giây."""
    try:
        size1 = os.path.getsize(path)
        if size1 == 0:
            return False
        time.sleep(wait)
        size2 = os.path.getsize(path)
        return size1 == size2
    except OSError:
        return False


# ============================================================
# AUTO WORKER
# ============================================================

class AutoWorker(QThread):
    """Background worker cho Auto Convert."""
    log_signal = pyqtSignal(str)
    sheet_loaded = pyqtSignal(int, int)   # n_voice, n_folder
    channel_update = pyqtSignal(list)     # [(folder, voice_id, pending, done)]
    convert_batch = pyqtSignal(list, str, str)  # files, voice_id, output_dir
    scan_done = pyqtSignal()              # 1 lượt scan xong
    cycle_done = pyqtSignal(int)          # seconds until next scan

    def __init__(self, voice_dir, poll_interval=300, stable_wait=10):
        super().__init__()
        self.voice_dir = voice_dir
        self.poll_interval = poll_interval
        self.stable_wait = stable_wait
        self._stopped = False
        self._converting = False
        self._convert_done_flag = False
        self._convert_timeout = False

    def stop(self):
        self._stopped = True

    def on_convert_done(self):
        """Gọi từ main thread khi VoiceWorker xong."""
        self._convert_done_flag = True
        self._converting = False

    def run(self):
        self.log_signal.emit("🔄 Auto Convert bắt đầu")
        self.log_signal.emit(f"📁 Thư mục: {self.voice_dir}")

        cycle = 0
        while not self._stopped:
            cycle += 1
            self.log_signal.emit(f"\n{'='*40}")
            self.log_signal.emit(f"📡 Lượt {cycle}: đọc Sheet...")

            try:
                self._do_cycle()
            except Exception as e:
                self.log_signal.emit(f"❌ Lỗi: {str(e)[:80]}")
                log.error(f"AutoWorker cycle error: {e}", exc_info=True)

            if self._stopped:
                break

            # Đếm ngược
            self.cycle_done.emit(self.poll_interval)
            for i in range(self.poll_interval):
                if self._stopped:
                    break
                time.sleep(1)

        self.log_signal.emit("⏹ Auto Convert đã dừng")

    def _do_cycle(self):
        from core.sheet_reader import read_all

        # 1. Đọc Sheet — LUON doc moi (force) de bat duoc data moi them vao Sheet.
        #    (truoc day dung cache 5 phut -> phai tat/mo lai tool moi thay data moi)
        voice_map, folder_map = read_all(force=True)
        self.sheet_loaded.emit(len(voice_map), len(folder_map))
        self.log_signal.emit(
            f"  Sheet: {len(voice_map)} voice, {len(folder_map)} mã")

        if self._stopped:
            return

        # 2. Chia TXT gốc vào folder kênh
        copied = self._split_txt(folder_map, voice_map)
        if copied:
            self.log_signal.emit(f"  ✂ Đã chia {copied} TXT mới")

        if self._stopped:
            return

        # 3. Quét subfolder cần convert
        channels = self._scan_channels(voice_map)
        self.channel_update.emit(channels)

        # 4. Convert từng channel
        for ch in channels:
            if self._stopped:
                break
            folder_name, voice_id, pending_files, done_count = ch
            if not pending_files:
                continue

            folder_path = os.path.join(self.voice_dir, folder_name)
            self.log_signal.emit(
                f"\n  🎙 {folder_name}: {len(pending_files)} file"
                f" (voice: {voice_id[:12]}...)")

            # Gửi batch convert
            # files format: [(row_idx, filepath)]
            file_list = [(i, f) for i, f in enumerate(pending_files)]
            output_dir = folder_path  # MP3 cùng thư mục TXT

            self._converting = True
            self._convert_done_flag = False
            self._convert_timeout = False
            self.convert_batch.emit(file_list, voice_id, output_dir)

            # Chờ convert xong — có timeout 3600s để tránh treo mãi nếu VoiceWorker crash
            _wait_start = time.time()
            _MAX_WAIT = 3600  # 1 tiếng tối đa cho 1 batch
            while not self._convert_done_flag and not self._stopped:
                time.sleep(1)
                if time.time() - _wait_start > _MAX_WAIT:
                    self.log_signal.emit(
                        f"  ⚠ Timeout {_MAX_WAIT}s chờ VoiceWorker "
                        f"({folder_name}) — yêu cầu dừng batch")
                    self._convert_timeout = True
                    self.convert_batch.emit([], "__CANCEL_STALE__", "")
                    _wait_start = time.time()

            if self._stopped or self._convert_timeout:
                return

        self.scan_done.emit()

    def _split_txt(self, folder_map, voice_map):
        """Copy TXT gốc vào folder kênh (check ổn định)."""
        copied = 0
        root = self.voice_dir

        if not os.path.isdir(root):
            return 0

        for fname in os.listdir(root):
            if self._stopped:
                break
            if not fname.lower().endswith(".txt"):
                continue

            code = extract_code(os.path.splitext(fname)[0])
            if not code:
                continue

            folder_name = folder_map.get(code)
            if not folder_name:
                continue

            # Chỉ chia nếu folder có voice_id
            if folder_name not in voice_map:
                continue

            dest_dir = os.path.join(root, folder_name)
            dest_file = os.path.join(dest_dir, fname)
            src_file = os.path.join(root, fname)

            # Đã chia rồi
            if os.path.exists(dest_file):
                continue

            # Đã có MP3 rồi (ở gốc hoặc trong subfolder)
            mp3_name = os.path.splitext(fname)[0] + ".mp3"
            if (os.path.exists(os.path.join(root, mp3_name))
                    or os.path.exists(os.path.join(dest_dir, mp3_name))):
                continue

            # Check ổn định
            self.log_signal.emit(
                f"  📋 {fname}: check ổn định ({self.stable_wait}s)...")
            if not file_stable(src_file, self.stable_wait):
                self.log_signal.emit(
                    f"  ⏳ {fname}: file đang thay đổi → bỏ qua")
                continue

            # Copy
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(src_file, dest_file)
            self.log_signal.emit(
                f"  ✂ {code} → {folder_name}/")
            copied += 1

        return copied

    def _scan_channels(self, voice_map):
        """Quét subfolder → tìm TXT chưa có MP3.
        
        Returns: [(folder_name, voice_id, [pending_paths], done_count)]
        """
        channels = []
        root = self.voice_dir

        if not os.path.isdir(root):
            return channels

        for folder_name in sorted(os.listdir(root)):
            folder_path = os.path.join(root, folder_name)
            if not os.path.isdir(folder_path):
                continue

            voice_id = voice_map.get(folder_name)
            if not voice_id:
                continue

            # Tìm TXT chưa có MP3
            pending = []
            done = 0
            for f in os.listdir(folder_path):
                if not f.lower().endswith(".txt"):
                    continue
                mp3 = os.path.splitext(f)[0] + ".mp3"
                if os.path.exists(os.path.join(folder_path, mp3)):
                    done += 1
                else:
                    txt_path = os.path.join(folder_path, f)
                    self.log_signal.emit(
                        f"  📋 {folder_name}/{f}: check ổn định "
                        f"({self.stable_wait}s)...")
                    if file_stable(txt_path, self.stable_wait):
                        pending.append(txt_path)
                    else:
                        self.log_signal.emit(
                            f"  ⏳ {folder_name}/{f}: file đang thay đổi → chờ lượt sau")

            channels.append((folder_name, voice_id, pending, done))

        return channels


# ============================================================
# DIALOG CAI DAT NANG CAO (chinh chi so voice)
# ============================================================

class AutoSettingsDialog(QDialog):
    """Chinh cac chi so chinh cua voice (luu settings.json, ap dung lan convert sau)."""

    MODELS = ["eleven_v3", "eleven_multilingual_v2",
              "eleven_flash_v2_5", "eleven_turbo_v2_5"]

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Cài đặt nâng cao (chỉ số voice)")
        self.resize(460, 420)
        self._build()

    def _build(self):
        c = self.config
        v = QVBoxLayout(self)
        info = QLabel("Các chỉ số sinh voice. Lưu xong áp dụng cho lần convert kế tiếp.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#555; font-size:11px;")
        v.addWidget(info)

        form = QFormLayout()

        self.cb_model = QComboBox()
        self.cb_model.addItems(self.MODELS)
        cur = c.get("default_model", "eleven_v3")
        if cur not in self.MODELS:
            self.cb_model.addItem(cur)
        self.cb_model.setCurrentText(cur)
        form.addRow("Model:", self.cb_model)

        self.sp_stab = QDoubleSpinBox()
        self.sp_stab.setRange(0.0, 1.0); self.sp_stab.setSingleStep(0.05)
        self.sp_stab.setDecimals(2)
        self.sp_stab.setValue(float(c.get("voice_stability", 0.5)))
        form.addRow("Stability:", self.sp_stab)

        self.sp_sim = QDoubleSpinBox()
        self.sp_sim.setRange(0.0, 1.0); self.sp_sim.setSingleStep(0.05)
        self.sp_sim.setDecimals(2)
        self.sp_sim.setValue(float(c.get("voice_similarity_boost", 0.8)))
        form.addRow("Similarity boost:", self.sp_sim)

        self.sp_chunk = QSpinBox()
        self.sp_chunk.setRange(500, 5000); self.sp_chunk.setSingleStep(100)
        self.sp_chunk.setValue(int(c.get("max_chunk_size", 5000)))
        form.addRow("Chunk tối đa (ký tự):", self.sp_chunk)

        self.sp_delay_start = QSpinBox()
        self.sp_delay_start.setRange(0, 600); self.sp_delay_start.setSuffix("s")
        self.sp_delay_start.setValue(int(c.get("auto_start_delay_sec", 60)))
        form.addRow("Auto chạy sau khi mở tool:", self.sp_delay_start)

        self.sp_poll = QSpinBox()
        self.sp_poll.setRange(60, 1800); self.sp_poll.setSuffix("s")
        self.sp_poll.setValue(int(c.get("poll_interval", 300)))
        form.addRow("Chu kỳ quét:", self.sp_poll)

        self.sp_stable = QSpinBox()
        self.sp_stable.setRange(5, 60); self.sp_stable.setSuffix("s")
        self.sp_stable.setValue(int(c.get("stable_wait", 10)))
        form.addRow("Ổn định file:", self.sp_stable)

        self.sp_reqdelay = QDoubleSpinBox()
        self.sp_reqdelay.setRange(0.0, 30.0); self.sp_reqdelay.setSingleStep(0.5)
        self.sp_reqdelay.setValue(float(c.get("request_delay", 3.0)))
        form.addRow("Delay mỗi request:", self.sp_reqdelay)

        self.sp_retry = QSpinBox()
        self.sp_retry.setRange(0, 10)
        self.sp_retry.setValue(int(c.get("max_retries", 3)))
        form.addRow("Số lần thử lại:", self.sp_retry)

        self.sp_threads = QSpinBox()
        self.sp_threads.setRange(1, 10)
        self.sp_threads.setValue(int(c.get("max_threads", 3)))
        form.addRow("Số luồng song song:", self.sp_threads)

        self.chk_srt = QCheckBox("Tự tạo phụ đề SRT")
        self.chk_srt.setChecked(bool(c.get("auto_create_srt", True)))
        form.addRow("", self.chk_srt)

        self.ed_sheet = QLineEdit(str(c.get("sheet_name", "KA")))
        self.ed_sheet.setToolTip(
            "Tên Google Sheet đọc voice/folder (tab THÔNG TIN + INPUT).\n"
            "Máy khác dùng sheet khác thì đổi tên ở đây (đã thay creds.json tương ứng).")
        form.addRow("Tên Google Sheet:", self.ed_sheet)

        v.addLayout(form)

        row = QHBoxLayout()
        row.addStretch()
        btn_save = QPushButton("💾 Lưu")
        btn_save.setStyleSheet(
            "font-weight:bold; background:#238636; color:white; padding:6px 16px;")
        btn_save.clicked.connect(self._save)
        row.addWidget(btn_save)
        btn_cancel = QPushButton("Hủy")
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(btn_cancel)
        v.addLayout(row)

    def _save(self):
        c = self.config
        c.set("default_model", self.cb_model.currentText())
        c.set("voice_stability", round(self.sp_stab.value(), 2))
        c.set("voice_similarity_boost", round(self.sp_sim.value(), 2))
        c.set("max_chunk_size", self.sp_chunk.value())
        c.set("auto_start_delay_sec", self.sp_delay_start.value())
        c.set("request_delay", round(self.sp_reqdelay.value(), 2))
        c.set("max_retries", self.sp_retry.value())
        c.set("max_threads", self.sp_threads.value())
        c.set("auto_create_srt", self.chk_srt.isChecked())
        c.set("poll_interval", self.sp_poll.value())
        c.set("stable_wait", self.sp_stable.value())
        sheet = self.ed_sheet.text().strip()
        if sheet:
            c.set("sheet_name", sheet)
        QMessageBox.information(self, "Cài đặt", "✅ Đã lưu chỉ số voice.")
        self.accept()


# ============================================================
# AUTO TAB
# ============================================================

class AutoTab(QWidget):

    def __init__(self):
        super().__init__()
        self.config = Config()
        self.auto_worker = None
        self.voice_worker = None
        self._countdown = 0
        self._countdown_timer = QTimer()
        self._countdown_timer.timeout.connect(self._tick_countdown)
        self._init_ui()

    def _update_cfg_label(self):
        c = self.config
        self.lbl_cfg.setText(
            f"Model: {c.get('default_model','eleven_v3')}  •  "
            f"stab {c.get('voice_stability',0.5)} / sim {c.get('voice_similarity_boost',0.8)}  •  "
            f"chunk {c.get('max_chunk_size',5000)}  •  "
            f"luồng {c.get('max_threads',3)}\n"
            f"auto-start {c.get('auto_start_delay_sec',60)}s  •  "
            f"quét {c.get('poll_interval',300)}s  •  "
            f"ổn định {c.get('stable_wait',10)}s")

    def _open_advanced_settings(self):
        dlg = AutoSettingsDialog(self.config, self)
        if dlg.exec_():
            self._update_cfg_label()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 6)
        layout.setSpacing(6)

        # === ROW 1: Cài đặt ===
        row1 = QHBoxLayout()

        # Thư mục
        g1 = QGroupBox("Thư mục")
        g1l = QGridLayout(g1)
        g1l.setContentsMargins(6, 18, 6, 6)

        g1l.addWidget(QLabel("Voice dir:"), 0, 0)
        self.dir_input = QLineEdit(DEFAULT_VOICE_DIR)
        g1l.addWidget(self.dir_input, 0, 1)
        btn_browse = QPushButton("Chọn")
        btn_browse.setFixedWidth(50)
        btn_browse.clicked.connect(self._browse_dir)
        g1l.addWidget(btn_browse, 0, 2)

        row1.addWidget(g1, 2)

        # Cài đặt (gọn: chỉ nút nâng cao + tóm tắt chỉ số hiện tại)
        g2 = QGroupBox("Cài đặt")
        g2v = QVBoxLayout(g2)
        g2v.setContentsMargins(8, 18, 8, 8)
        g2v.setSpacing(6)

        self.btn_adv = QPushButton("⚙ Cài đặt nâng cao")
        self.btn_adv.setToolTip(
            "Chỉnh model, stability, similarity, chunk, số luồng, "
            "auto-start, chu kỳ quét, ổn định file...")
        self.btn_adv.setStyleSheet(
            "font-weight:bold; background:#34495e; color:white; "
            "padding:7px; border-radius:4px;")
        self.btn_adv.clicked.connect(self._open_advanced_settings)
        g2v.addWidget(self.btn_adv)

        # tom tat chi so hien tai (de biet ngay dang dung gi)
        self.lbl_cfg = QLabel("")
        self.lbl_cfg.setWordWrap(True)
        self.lbl_cfg.setStyleSheet("color:#888; font-size:10px;")
        g2v.addWidget(self.lbl_cfg)
        self._update_cfg_label()

        row1.addWidget(g2, 1)
        layout.addLayout(row1)

        # === ROW 2: Actions ===
        actions = QHBoxLayout()

        self.btn_start = QPushButton("▶ BẮT ĐẦU AUTO")
        self.btn_start.setStyleSheet(
            "font-weight:bold; font-size:14px; color:white; "
            "background:#2980b9; padding:8px 24px; border-radius:4px;")
        self.btn_start.clicked.connect(self._start_auto)
        actions.addWidget(self.btn_start)

        self.btn_stop = QPushButton("⏹ Dừng")
        self.btn_stop.setStyleSheet(
            "color:red; font-weight:bold; padding:8px;")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_auto)
        actions.addWidget(self.btn_stop)

        actions.addStretch()

        self.btn_reload = QPushButton("🔄 Đọc lại Sheet")
        self.btn_reload.clicked.connect(self._reload_sheet)
        actions.addWidget(self.btn_reload)

        layout.addLayout(actions)

        # === ROW 3: Status ===
        status_row = QHBoxLayout()
        self.lbl_status = QLabel("⏸ Chưa chạy")
        self.lbl_status.setStyleSheet(
            "font-size:12px; font-weight:bold; color:#666;")
        status_row.addWidget(self.lbl_status)

        status_row.addStretch()

        self.lbl_sheet = QLabel("Sheet: chưa đọc")
        self.lbl_sheet.setStyleSheet("font-size:11px; color:#888;")
        status_row.addWidget(self.lbl_sheet)

        layout.addLayout(status_row)

        # === ROW 4: Channel table ===
        self.ch_table = QTableWidget()
        self.ch_table.setColumnCount(4)
        self.ch_table.setHorizontalHeaderLabels(
            ["Kênh", "Voice ID", "Chờ", "Xong"])
        h = self.ch_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.ch_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ch_table.setAlternatingRowColors(True)
        self.ch_table.setMaximumHeight(200)
        layout.addWidget(self.ch_table)

        # === ROW 5: Log ===
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setStyleSheet(
            "font-family:Consolas; font-size:10px; "
            "background:#1e1e1e; color:#ddd;")
        layout.addWidget(self.log_area, 1)

    # ============================================================
    # ACTIONS
    # ============================================================

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(
            self, "Chọn thư mục voice", self.dir_input.text())
        if d:
            self.dir_input.setText(d)

    def _start_auto(self):
        if self.auto_worker and self.auto_worker.isRunning():
            return

        voice_dir = self.dir_input.text().strip()
        if not os.path.isdir(voice_dir):
            self._log(f"❌ Thư mục không tồn tại: {voice_dir}")
            return

        poll = int(self.config.get("poll_interval", 300))
        stable = int(self.config.get("stable_wait", 10))
        self.auto_worker = AutoWorker(
            voice_dir=voice_dir,
            poll_interval=poll,
            stable_wait=stable,
        )
        self.auto_worker.log_signal.connect(self._log)
        self.auto_worker.sheet_loaded.connect(self._on_sheet_loaded)
        self.auto_worker.channel_update.connect(self._on_channel_update)
        self.auto_worker.convert_batch.connect(self._on_convert_batch)
        self.auto_worker.scan_done.connect(self._on_scan_done)
        self.auto_worker.cycle_done.connect(self._on_cycle_done)

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.lbl_status.setText("🔄 Đang chạy...")
        self.lbl_status.setStyleSheet(
            "font-size:12px; font-weight:bold; color:#27ae60;")

        self.auto_worker.start()

    def _stop_auto(self):
        if self.auto_worker:
            self.auto_worker.stop()
        if self.voice_worker:
            self.voice_worker.cancel()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_status.setText("⏸ Đã dừng")
        self.lbl_status.setStyleSheet(
            "font-size:12px; font-weight:bold; color:#e74c3c;")
        self._countdown_timer.stop()

    def _reload_sheet(self):
        self._log("🔄 Đọc lại Sheet (force)...")
        try:
            from core.sheet_reader import read_all
            voice_map, folder_map = read_all(force=True)
            self._log(f"  ✓ {len(voice_map)} voice, {len(folder_map)} mã")
            self.lbl_sheet.setText(
                f"Sheet: {len(voice_map)} voice, {len(folder_map)} mã")
        except Exception as e:
            self._log(f"  ❌ {e}")

    # ============================================================
    # CONVERT INTEGRATION
    # ============================================================

    def _on_convert_batch(self, files, voice_id, output_dir):
        """AutoWorker yêu cầu convert 1 batch."""
        from ui.voice_tool import VoiceWorker

        if self.voice_worker and self.voice_worker.isRunning():
            self._log("  ⚠ VoiceWorker cũ còn chạy — cancel trước khi tạo batch mới")
            self.voice_worker.cancel()
            if not self.voice_worker.wait(10000):
                self._log("  ⚠ VoiceWorker cũ chưa dừng, bỏ qua batch này để tránh chạy chồng")
                if self.auto_worker:
                    self.auto_worker.on_convert_done()
                return

        if voice_id == "__CANCEL_STALE__":
            if self.auto_worker:
                self.auto_worker.on_convert_done()
            return

        config = Config()

        # --- Fixed language for TL1 channels, detect only for warning/fallback ---
        pending_paths = [fp for _, fp in files] if files else []
        folder_name = os.path.basename(os.path.normpath(output_dir or ""))
        mapped_language = TL1_LANGUAGE_MAP.get(folder_name)
        language_code = mapped_language
        detected = ""

        if pending_paths:
            try:
                detected = detect_language_from_files(pending_paths, max_files=3)
                if detected:
                    from core.convert import LANGUAGE_NAMES
                    det_name = LANGUAGE_NAMES.get(detected, detected)
                    if mapped_language:
                        map_name = LANGUAGE_NAMES.get(mapped_language, mapped_language)
                        if detected != mapped_language:
                            self._log(
                                f"  [Lang] {folder_name}: dùng fixed {mapped_language} "
                                f"({map_name}); detect lệch {detected} ({det_name})")
                        else:
                            self._log(
                                f"  [Lang] {folder_name}: fixed {mapped_language} "
                                f"({map_name})")
                    else:
                        self._log(f"  [Lang] Detect: {detected} ({det_name})")
                        language_code = detected
                elif mapped_language:
                    from core.convert import LANGUAGE_NAMES
                    map_name = LANGUAGE_NAMES.get(mapped_language, mapped_language)
                    self._log(
                        f"  [Lang] {folder_name}: dùng fixed {mapped_language} "
                        f"({map_name}); detect không ra")
                else:
                    self._log("  [Lang] Khong detect duoc ngon ngu, dung Auto")
            except Exception as e:
                self._log(f"  [Lang] Detect loi: {str(e)[:60]}")
                if mapped_language:
                    from core.convert import LANGUAGE_NAMES
                    map_name = LANGUAGE_NAMES.get(mapped_language, mapped_language)
                    self._log(
                        f"  [Lang] {folder_name}: vẫn dùng fixed "
                        f"{mapped_language} ({map_name})")
        elif mapped_language:
            from core.convert import LANGUAGE_NAMES
            map_name = LANGUAGE_NAMES.get(mapped_language, mapped_language)
            self._log(
                f"  [Lang] {folder_name}: dùng fixed {mapped_language} "
                f"({map_name})")

        self.voice_worker = VoiceWorker(
            files=files,
            output_dir=output_dir,
            voice_id=voice_id,
            model_id=config.get("default_model", "eleven_v3"),
            stability=1.0,
            similarity=1.0,
            speed=1.0,
            output_format="mp3_44100_128",
            mode="b",
            config=config,
            language_code=language_code,
        )
        self.voice_worker.log_signal.connect(self._log)
        self.voice_worker.all_done.connect(self._on_voice_done)

        self.voice_worker.start()

    def _on_voice_done(self, done, error):
        sender = self.sender()
        if sender is not self.voice_worker:
            self._log("  ⚠ Bỏ qua tín hiệu từ VoiceWorker cũ")
            return
        self._log(f"  ✅ Batch xong: {done} OK, {error} lỗi")
        if self.auto_worker:
            self.auto_worker.on_convert_done()

    # ============================================================
    # UI UPDATES
    # ============================================================

    def _log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log_area.append(f"[{ts}] {msg}")
        sb = self.log_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_sheet_loaded(self, n_voice, n_folder):
        self.lbl_sheet.setText(
            f"Sheet: {n_voice} voice, {n_folder} mã")

    def _on_channel_update(self, channels):
        self.ch_table.setRowCount(len(channels))
        for i, (folder, voice_id, pending, done) in enumerate(channels):
            self.ch_table.setItem(i, 0, QTableWidgetItem(folder))
            self.ch_table.setItem(
                i, 1, QTableWidgetItem(voice_id[:16] + "..."))
            
            pending_item = QTableWidgetItem(str(len(pending)))
            if pending:
                pending_item.setForeground(QColor("#e67e22"))
                pending_item.setFont(QFont("", -1, QFont.Bold))
            self.ch_table.setItem(i, 2, pending_item)
            
            done_item = QTableWidgetItem(str(done))
            done_item.setForeground(QColor("#27ae60"))
            self.ch_table.setItem(i, 3, done_item)

    def _on_scan_done(self):
        self.lbl_status.setText("🔄 Đang chạy...")
        self.lbl_status.setStyleSheet(
            "font-size:12px; font-weight:bold; color:#27ae60;")

    def _on_cycle_done(self, seconds):
        self._countdown = seconds
        self._countdown_timer.start(1000)

    def _tick_countdown(self):
        if self._countdown > 0:
            self._countdown -= 1
            self.lbl_status.setText(
                f"⏳ Quét tiếp sau {self._countdown}s...")
            self.lbl_status.setStyleSheet(
                "font-size:12px; font-weight:bold; color:#2980b9;")
        else:
            self._countdown_timer.stop()
            self.lbl_status.setText("🔄 Đang quét...")
            self.lbl_status.setStyleSheet(
                "font-size:12px; font-weight:bold; color:#27ae60;")
