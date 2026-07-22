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

# Path mặc định: đọc động từ settings.json ("output_dir") -> đổi ở tab/cấu hình,
# không hardcode theo máy cũ. Fallback về D:\AUTO\voice nếu chưa cấu hình.
def _default_voice_dir():
    try:
        d = (Config().get("output_dir") or "").strip()
        if d:
            return d
    except Exception:
        pass
    return r"D:\AUTO\voice"

DEFAULT_VOICE_DIR = _default_voice_dir()

# Quy doi ky tu -> gio voice (uoc tinh tu do do thuc te: ~15-19 ky tu/giay tuy
# ngon ngu/model -> lay TB ~16/giay = 57.600/gio). Chi de hien thi tuong doi.
CHARS_PER_HOUR = 57600

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
    """Check file không thay đổi trong `wait` giây.

    Toi uu: neu file da LAU khong doi (mtime cach day >= wait giay) thi chac chan
    on dinh roi -> tra True NGAY, khoi cho 10s. Chi cho + recheck voi file vua ghi.
    (Truoc day file nao cung sleep(wait) -> quet 48 file mat ~8 phut vo ich.)
    """
    try:
        st = os.stat(path)
        if st.st_size == 0:
            return False
        if time.time() - st.st_mtime >= wait:
            return True
        size1 = st.st_size
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

    def __init__(self, voice_dir, poll_interval=300, stable_wait=10,
                 mode_c=False, mode_c_browsers=2):
        super().__init__()
        self.voice_dir = voice_dir
        self.poll_interval = poll_interval
        self.stable_wait = stable_wait
        self.mode_c = mode_c                    # True -> tao voice qua anonymous (khong master)
        self.mode_c_browsers = mode_c_browsers  # so Chrome song song
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

        # 0. Neu thu muc mat (o mang rot) -> thu map lai roi kiem tra
        if not os.path.isdir(self.voice_dir):
            try:
                from utils.config import Config
                from core.net_drive import map_drive, is_enabled
                cfg = Config().data
                if is_enabled(cfg):
                    self.log_signal.emit("🔌 Mất thư mục → map lại ổ mạng...")
                    ok, msg = map_drive(cfg, force_remap=True)
                    self.log_signal.emit(f"   Map ổ: {'OK' if ok else 'lỗi'} ({msg})")
            except Exception as e:
                self.log_signal.emit(f"   Map ổ lỗi: {str(e)[:60]}")
            if not os.path.isdir(self.voice_dir):
                self.log_signal.emit(f"❌ Thư mục vẫn không tồn tại: {self.voice_dir}")
                return

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
        # BAT DAU PHIEN: danh dau de lam SACH (xoay IP moi + kill browser cu) o file dau.
        if self.mode_c and any(c[2] for c in channels):
            try:
                from core.mode_c_engine import begin_session
                begin_session()
            except Exception:
                pass
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

            output_dir = folder_path  # MP3 cùng thư mục TXT

            # === MODE C (anonymous): tao voice khong master, chay ngay trong worker nay ===
            if self.mode_c:
                self._convert_mode_c(folder_name, voice_id, pending_files, output_dir)
                if self._stopped:
                    return
                continue

            # === MODE MASTER (cu): gui batch cho VoiceWorker ===
            file_list = [(i, f) for i, f in enumerate(pending_files)]
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

    def _convert_mode_c(self, folder_name, voice_id, pending_files, output_dir):
        """Tao voice qua Mode C (anonymous). BEN BI: 1 file loi -> bo qua, lam file khac.

        Tai dung 1 ModeCEngine cho ca folder (cung voice_id) -> pool Chrome + 4G chung.
        """
        try:
            from core.mode_c_engine import ModeCEngine, generate_file, kill_orphan_browsers
        except Exception as e:
            self.log_signal.emit(f"  ❌ Mode C khong load duoc: {str(e)[:80]}")
            return

        kill_orphan_browsers()   # don process rac tu phien truoc
        engine = ModeCEngine(
            voice_id=voice_id, model_id="eleven_v3", language_code="vi",
            use_4g=True, n_browsers=self.mode_c_browsers, headless=True,
            on_log=lambda m: self.log_signal.emit(f"  {m}"))

        ok = 0
        for txt_path in pending_files:
            if self._stopped:
                break
            # 4G chet han (dien thoai mat song) -> DUNG ca loat, khong cay file tiep
            if getattr(engine._sh, "p4g_dead", False) if engine._sh else False:
                self.log_signal.emit("  🛑 4G chet (dien thoai mat song) -> dung luot nay, "
                                     "cho 4G hoi (kiem tra dien thoai).")
                break
            base = os.path.basename(txt_path)
            try:
                generate_file(engine, txt_path, output_dir)
                ok += 1
            except Exception as e:
                self.log_signal.emit(f"  ⚠ {base}: loi ({str(e)[:80]}) -> bo qua, lam file khac")
        try:
            engine.cancel()
            kill_orphan_browsers()   # don process sau khi xong folder
        except Exception:
            pass
        self.log_signal.emit(f"  ✅ {folder_name}: {ok}/{len(pending_files)} file OK (Mode C)")

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


class MapDriveWorker(QThread):
    """Map o mang (net use) chay nen luc mo tool -> khong treo GUI."""
    log_signal = pyqtSignal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def run(self):
        try:
            from core.net_drive import map_drive
            letter = self.cfg.get("map_drive_letter", "")
            self.log_signal.emit(f"🔌 Map ổ mạng {letter} lúc mở tool...")
            ok, msg = map_drive(self.cfg, force_remap=False)
            self.log_signal.emit(f"   Map ổ: {'OK' if ok else 'lỗi'} ({msg})")
        except Exception as e:
            self.log_signal.emit(f"   Map ổ lỗi: {str(e)[:60]}")


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

        # Thu muc voice (chuyen tu tab chinh vao day)
        dir_row = QHBoxLayout()
        self.ed_voice_dir = QLineEdit(str(c.get("output_dir", "") or ""))
        dir_row.addWidget(self.ed_voice_dir, 1)
        btn_vd = QPushButton("Chọn")
        btn_vd.setFixedWidth(50)
        btn_vd.clicked.connect(self._browse_voice_dir)
        dir_row.addWidget(btn_vd)
        form.addRow("Thư mục voice:", dir_row)

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

        self.chk_autostart = QCheckBox("Tự động chạy Auto Convert khi mở tool")
        self.chk_autostart.setToolTip(
            "TẮT ô này -> mở tool KHÔNG tự chạy (phải bấm BẮT ĐẦU AUTO thủ công).\n"
            "Dùng cho máy chỉ để dự phòng / không muốn tự convert.")
        self.chk_autostart.setChecked(bool(c.get("auto_start_enabled", True)))
        form.addRow("", self.chk_autostart)

        self.sp_delay_start = QSpinBox()
        self.sp_delay_start.setRange(0, 600); self.sp_delay_start.setSuffix("s")
        self.sp_delay_start.setValue(int(c.get("auto_start_delay_sec", 60)))
        form.addRow("  Chạy sau khi mở (giây):", self.sp_delay_start)

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

        self.chk_parallel = QCheckBox("⚡ Chạy SONG SONG các chunk (nhanh ~3x)")
        self.chk_parallel.setToolTip(
            "Generate nhiều chunk cùng lúc thay vì tuần tự -> nhanh hơn nhiều với file dài.\n"
            "Chỉ áp dụng master mode. Chunk nào lỗi sẽ tự làm lại tuần tự (an toàn).")
        self.chk_parallel.setChecked(bool(c.get("parallel_chunks", True)))
        form.addRow("", self.chk_parallel)

        self.sp_pworkers = QSpinBox()
        self.sp_pworkers.setRange(2, 6)
        self.sp_pworkers.setValue(int(c.get("parallel_chunk_workers", 3)))
        self.sp_pworkers.setToolTip("Số chunk generate cùng lúc (2-6).")
        form.addRow("  Số chunk song song:", self.sp_pworkers)

        # === MODE C (anonymous - khong can master/TK) ===
        self.chk_mode_c = QCheckBox("🆕 MODE C: Tạo voice KHÔNG cần tài khoản (anonymous + 4G)")
        self.chk_mode_c.setToolTip(
            "Tạo voice qua web demo elevenlabs.io (không đăng nhập). KHÔNG cần master/TK.\n"
            "- Dùng Chrome sạch (Camoufox) mint token + gửi qua 4G.\n"
            "- Tự xoay IP 4G sau 15 request, tự đổi Chrome khi lỗi.\n"
            "- Giới hạn 1000 ký tự/chunk (tự chia). Cần 4G proxy đang chạy.\n"
            "BẬT = bỏ qua hoàn toàn đường master, chỉ dùng anonymous.")
        self.chk_mode_c.setChecked(bool(c.get("mode_c_enabled", True)))
        self.chk_mode_c.setStyleSheet("font-weight:bold; color:#8e44ad;")
        form.addRow("", self.chk_mode_c)

        self.sp_mode_c_browsers = QSpinBox()
        self.sp_mode_c_browsers.setRange(1, 8)
        self.sp_mode_c_browsers.setValue(int(c.get("mode_c_browsers", 3)))
        self.sp_mode_c_browsers.setToolTip(
            "Số Chrome TỐI ĐA (1-8). Tool TỰ điều chỉnh theo tài nguyên máy lúc chạy:\n"
            "máy khỏe/rảnh -> chạy nhiều Chrome (nhanh), máy yếu/bận -> ít lại (không treo).\n"
            "Không bao giờ vượt số này, cũng không nhiều hơn số chunk cần làm.\n"
            "Chung 1 IP 4G (16 req/IP -> tự xoay). Mỗi Chrome ~350MB RAM.")
        form.addRow("  Số Chrome tối đa (tự điều chỉnh):", self.sp_mode_c_browsers)

        self.chk_maint = QCheckBox("🤖 Tự bảo trì nền (quét quota + reset + cảnh báo)")
        self.chk_maint.setToolTip(
            "Tool 24/7: định kỳ quét quota thật, lưu ngày reset, cảnh báo cạn nguồn.\n"
            "Chạy khi KHÔNG convert. Đọc-only, không tốn credit.")
        self.chk_maint.setChecked(bool(c.get("auto_maintenance", True)))
        form.addRow("", self.chk_maint)

        self.sp_maint_h = QSpinBox()
        self.sp_maint_h.setRange(1, 48)
        self.sp_maint_h.setValue(int(c.get("maintenance_interval_hours", 12)))
        self.sp_maint_h.setToolTip("Bao lâu bảo trì 1 lần (giờ).")
        form.addRow("  Chu kỳ bảo trì (giờ):", self.sp_maint_h)

        self.chk_relink = QCheckBox("Tự Liên kết Master khi bảo trì (nặng)")
        self.chk_relink.setToolTip(
            "Khi bảo trì, tự gom TK pending/mồ côi về master sống.\n"
            "NẶNG (login nhiều worker qua 4G) -> chỉ bật nếu muốn hoàn toàn tự động.")
        self.chk_relink.setChecked(bool(c.get("auto_relink", False)))
        form.addRow("", self.chk_relink)

        self.ed_sheet = QLineEdit(str(c.get("sheet_name", "KA")))
        self.ed_sheet.setToolTip(
            "Tên Google Sheet đọc voice/folder (tab THÔNG TIN + INPUT).\n"
            "Máy khác dùng sheet khác thì đổi tên ở đây (đã thay creds.json tương ứng).")
        form.addRow("Tên Google Sheet:", self.ed_sheet)

        v.addLayout(form)

        # === Map o mang (net use) — chay khi mo tool / khi thu muc khong ton tai ===
        gbox = QGroupBox("Ổ mạng (net use) — tự map khi mở tool / khi mất thư mục")
        gl = QFormLayout(gbox)
        self.chk_map = QCheckBox("Bật tự map ổ mạng")
        self.chk_map.setChecked(bool(c.get("map_drive_enabled", False)))
        gl.addRow("", self.chk_map)
        self.ed_map_letter = QLineEdit(str(c.get("map_drive_letter", "Z:")))
        gl.addRow("Ổ (drive):", self.ed_map_letter)
        self.ed_map_share = QLineEdit(str(c.get("map_drive_share", r"\\192.168.88.254\D")))
        gl.addRow("Đường dẫn share:", self.ed_map_share)
        self.ed_map_user = QLineEdit(str(c.get("map_drive_user", "smbuser")))
        gl.addRow("User:", self.ed_map_user)
        self.ed_map_pass = QLineEdit(str(c.get("map_drive_pass", "")))
        gl.addRow("Mật khẩu:", self.ed_map_pass)
        hint = QLabel("VD: ổ Z: , share \\\\192.168.88.254\\D , user smbuser")
        hint.setStyleSheet("color:#888; font-size:10px;")
        gl.addRow("", hint)
        v.addWidget(gbox)

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

    def _browse_voice_dir(self):
        d = QFileDialog.getExistingDirectory(
            self, "Chọn thư mục voice", self.ed_voice_dir.text().strip())
        if d:
            self.ed_voice_dir.setText(d)

    def _save(self):
        c = self.config
        _vd = self.ed_voice_dir.text().strip()
        if _vd:
            c.set("output_dir", _vd)
        c.set("default_model", self.cb_model.currentText())
        c.set("voice_stability", round(self.sp_stab.value(), 2))
        c.set("voice_similarity_boost", round(self.sp_sim.value(), 2))
        c.set("max_chunk_size", self.sp_chunk.value())
        c.set("auto_start_enabled", self.chk_autostart.isChecked())
        c.set("auto_start_delay_sec", self.sp_delay_start.value())
        c.set("request_delay", round(self.sp_reqdelay.value(), 2))
        c.set("max_retries", self.sp_retry.value())
        c.set("max_threads", self.sp_threads.value())
        c.set("auto_create_srt", self.chk_srt.isChecked())
        c.set("parallel_chunks", self.chk_parallel.isChecked())
        c.set("parallel_chunk_workers", self.sp_pworkers.value())
        c.set("mode_c_enabled", self.chk_mode_c.isChecked())
        c.set("mode_c_browsers", self.sp_mode_c_browsers.value())
        c.set("auto_maintenance", self.chk_maint.isChecked())
        c.set("maintenance_interval_hours", self.sp_maint_h.value())
        c.set("auto_relink", self.chk_relink.isChecked())
        c.set("poll_interval", self.sp_poll.value())
        c.set("stable_wait", self.sp_stable.value())
        sheet = self.ed_sheet.text().strip()
        if sheet:
            c.set("sheet_name", sheet)
        # map o mang
        c.set("map_drive_enabled", self.chk_map.isChecked())
        c.set("map_drive_letter", self.ed_map_letter.text().strip())
        c.set("map_drive_share", self.ed_map_share.text().strip())
        c.set("map_drive_user", self.ed_map_user.text().strip())
        c.set("map_drive_pass", self.ed_map_pass.text().strip())
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
        self._map_worker = None
        self._init_ui()
        # Map o mang luc mo tool (neu bat) - chay nen, khong treo GUI
        QTimer.singleShot(800, self._startup_map)

    def _startup_map(self):
        try:
            from core.net_drive import is_enabled
            if not is_enabled(self.config.data):
                return
            self._map_worker = MapDriveWorker(self.config.data)
            self._map_worker.log_signal.connect(self._log)
            self._map_worker.start()
        except Exception:
            pass

    def _update_cfg_label(self):
        c = self.config
        mode_c = bool(c.get("mode_c_enabled", True))
        if mode_c:
            # Che do MODE C: hien thong so lien quan (khong master)
            nb = c.get("mode_c_browsers", 3)
            self.lbl_cfg.setText(
                f"🆕 MODE C (anonymous, KHÔNG cần tài khoản)  •  "
                f"model eleven_v3  •  tối đa {nb} Chrome (tự điều chỉnh theo máy)  •  "
                f"1000 ký tự/chunk  •  tự xoay 4G (16 req/IP)\n"
                f"auto-start {c.get('auto_start_delay_sec',60)}s  •  "
                f"quét {c.get('poll_interval',300)}s  •  "
                f"ổn định {c.get('stable_wait',10)}s")
            self.lbl_cfg.setStyleSheet("color:#8e44ad; font-size:10px; font-weight:bold;")
        else:
            self.lbl_cfg.setText(
                f"Master mode  •  Model: {c.get('default_model','eleven_v3')}  •  "
                f"stab {c.get('voice_stability',0.5)} / sim {c.get('voice_similarity_boost',0.8)}  •  "
                f"chunk {c.get('max_chunk_size',5000)}  •  "
                f"luồng {c.get('max_threads',3)}\n"
                f"auto-start {c.get('auto_start_delay_sec',60)}s  •  "
                f"quét {c.get('poll_interval',300)}s  •  "
                f"ổn định {c.get('stable_wait',10)}s")
            self.lbl_cfg.setStyleSheet("color:#888; font-size:10px;")

    def _open_advanced_settings(self):
        dlg = AutoSettingsDialog(self.config, self)
        if dlg.exec_():
            # Voice dir co the vua doi trong dialog -> dong bo lai
            vd = (self.config.get("output_dir") or "").strip()
            if vd:
                self.dir_input.setText(vd)
            self._update_cfg_label()
            self._update_overview()

    def _try_map_drive(self, force=False):
        """Map o mang (net use) neu bat trong cai dat. -> True neu chay/OK."""
        try:
            from core.net_drive import map_drive, is_enabled
            cfg = self.config.data
            if not is_enabled(cfg):
                return False
            self._log(f"🔌 Đang map ổ mạng {cfg.get('map_drive_letter','')}...")
            ok, msg = map_drive(cfg, on_log=lambda m: None, force_remap=force)
            self._log(f"   Map ổ: {'OK' if ok else 'lỗi'} ({msg})")
            return ok
        except Exception as e:
            self._log(f"   Map ổ lỗi: {str(e)[:60]}")
            return False

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 6)
        layout.setSpacing(6)

        # === TONG QUAN (ngay duoi tabs): bao cao SAN XUAT + TAI NGUYEN ===
        ov = QGroupBox("📊 Tổng quan")
        ov.setStyleSheet("QGroupBox{font-weight:bold; font-size:11px;}")
        ovl = QHBoxLayout(ov)
        ovl.setContentsMargins(12, 14, 12, 8)
        ovl.setSpacing(16)

        def _stat(title, tip="", big=False):
            box = QVBoxLayout()
            box.setSpacing(0)
            val = QLabel("—")
            val.setStyleSheet(
                "font-size:%dpx; font-weight:bold;" % (18 if big else 15))
            cap = QLabel(title)
            cap.setStyleSheet("font-size:9px; color:#888;")
            if tip:
                val.setToolTip(tip)
                cap.setToolTip(tip)
            box.addWidget(val)
            box.addWidget(cap)
            ovl.addLayout(box)
            return val, cap

        def _sep(text="│"):
            s = QLabel(text)
            s.setStyleSheet("color:#ccc; font-size:16px;")
            ovl.addWidget(s)

        # --- SAN XUAT (voice thuc te tren dia) ---
        self.ov_today, _ = _stat("Xử lý hôm nay", "Số file voice đã tạo xong HÔM NAY", big=True)
        self.ov_pending, _ = _stat("Còn tồn", "File chưa xử lý (TXT chưa có MP3)", big=True)
        self.ov_donetotal, _ = _stat("Tổng đã làm", "Tổng file đã có MP3 (mọi thời điểm)")
        _sep()
        # --- TAI NGUYEN (nhan doi theo mode: master vs Mode C) ---
        self.ov_capacity, self._ov_cap_lbl = _stat("Nguồn voice", "Mode C: không giới hạn TK. Master: giờ voice từ quota.")
        self.ov_alive, self._ov_alive_lbl = _stat("IP 4G", "Mode C: IP 4G hiện tại. Master: số TK sống.")
        self.ov_master, self._ov_master_lbl = _stat("Chrome", "Mode C: số Chrome. Master: số master.")
        self.ov_runway, self._ov_runway_lbl = _stat("4G", "Mode C: 4G còn sống? Master: còn ~ngày.")
        ovl.addStretch(1)
        self.btn_refresh_ov = QPushButton("↻")
        self.btn_refresh_ov.setFixedWidth(28)
        self.btn_refresh_ov.setToolTip("Cập nhật tổng quan (đọc file/roster, không tốn credit).")
        self.btn_refresh_ov.clicked.connect(self._update_overview)
        ovl.addWidget(self.btn_refresh_ov)
        # Nut Cai dat nang cao NGAY tren dong Tong quan
        self.btn_adv = QPushButton("⚙ Cài đặt nâng cao")
        self.btn_adv.setToolTip(
            "Thư mục voice, model, stability, chunk, song song, tự bảo trì...")
        self.btn_adv.setStyleSheet(
            "font-weight:bold; background:#34495e; color:white; "
            "padding:6px 12px; border-radius:4px;")
        self.btn_adv.clicked.connect(self._open_advanced_settings)
        ovl.addWidget(self.btn_adv)
        layout.addWidget(ov)

        self._ov_timer = QTimer(self)
        self._ov_timer.timeout.connect(self._update_overview)
        self._ov_timer.start(60000)
        QTimer.singleShot(1500, self._update_overview)

        # Voice dir: KHONG hien o day nua -> chinh trong 'Cai dat nang cao'.
        # Giu QLineEdit an lam noi luu gia tri (code khac doc self.dir_input.text()).
        self.dir_input = QLineEdit(DEFAULT_VOICE_DIR, self)
        self.dir_input.setVisible(False)

        # Tom tat cau hinh hien tai (dong mong ngay duoi Tong quan)
        self.lbl_cfg = QLabel("")
        self.lbl_cfg.setStyleSheet("color:#888; font-size:10px;")
        layout.addWidget(self.lbl_cfg)
        self._update_cfg_label()

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
            # Thu map o mang roi kiem tra lai (thuong do o mang chua ket noi)
            from core.net_drive import is_enabled
            if not is_enabled(self.config.data):
                self._log(f"❌ Thư mục không tồn tại: {voice_dir}")
                self._log("   → Vào ⚙ Cài đặt nâng cao > Ổ mạng: BẬT 'tự map ổ mạng' "
                          "+ điền drive/share/user/mật khẩu (192.168.88.254) rồi Lưu.")
                return
            self._try_map_drive(force=True)
            if not os.path.isdir(voice_dir):
                self._log(f"❌ Thư mục không tồn tại: {voice_dir}")
                self._log("   (Đã map ổ mạng nhưng vẫn không thấy — kiểm tra "
                          "user/mật khẩu/share đúng chưa, máy chủ 192.168.88.254 có bật không)")
                return

        poll = int(self.config.get("poll_interval", 300))
        stable = int(self.config.get("stable_wait", 10))
        mode_c = bool(self.config.get("mode_c_enabled", True))
        mode_c_browsers = int(self.config.get("mode_c_browsers", 3))
        if mode_c:
            self._log("🆕 MODE C (anonymous, khong master) DANG BAT")
        self.auto_worker = AutoWorker(
            voice_dir=voice_dir,
            poll_interval=poll,
            stable_wait=stable,
            mode_c=mode_c,
            mode_c_browsers=mode_c_browsers,
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
        line = f"[{ts}] {msg}"
        self.log_area.append(line)
        sb = self.log_area.verticalScrollBar()
        sb.setValue(sb.maximum())
        # Ghi ra FILE de xem lai / debug (logs/auto_convert.log, xoay vong khi >5MB)
        try:
            logdir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
            os.makedirs(logdir, exist_ok=True)
            fpath = os.path.join(logdir, "auto_convert.log")
            if os.path.exists(fpath) and os.path.getsize(fpath) > 5 * 1024 * 1024:
                try:
                    os.replace(fpath, fpath + ".old")
                except Exception:
                    pass
            with open(fpath, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            pass

    def _on_sheet_loaded(self, n_voice, n_folder):
        self.lbl_sheet.setText(
            f"Sheet: {n_voice} voice, {n_folder} mã")

    def _on_channel_update(self, channels):
        self.ch_table.setRowCount(len(channels))
        pend_total = 0
        for i, (folder, voice_id, pending, done) in enumerate(channels):
            pend_total += len(pending)
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
        self._pending_files = pend_total
        self._update_overview()

    def _fmt_hours(self, chars):
        """Quy doi ky tu -> gio voice (uoc tinh ~CHARS_PER_HOUR). -> '12.3h' / '45m'."""
        h = (int(chars or 0)) / CHARS_PER_HOUR
        if h >= 1:
            return f"{h:.1f}h"
        return f"{int(h*60)}m"

    def _scan_production(self, root):
        """Quet thu muc voice -> dict {done_files, pend_files, today_files,
        done_chars, pend_chars, today_chars}.

        Tinh theo MA (stem) tren TOAN CAY: 1 ma co MP3 o bat ky dau = DA LAM
        (txt goc da chia vao folder kenh -> mp3 o kenh van tinh la xong, khong bi
        dem nham "con ton"). Ma co txt ma KHONG co mp3 = con ton. Bo qua SRT_/.chunks.
        """
        r = {"done_files": 0, "pend_files": 0, "today_files": 0,
             "done_chars": 0, "pend_chars": 0, "today_chars": 0}
        if not root or not os.path.isdir(root):
            return r
        lt = time.localtime()
        today_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                   0, 0, 0, 0, 0, -1))
        txt_chars = {}     # stem -> so ky tu
        mp3_mtime = {}     # stem -> mtime moi nhat cua mp3
        for dirpath, dirnames, filenames in os.walk(root):
            base = os.path.basename(dirpath)
            if base == ".chunks" or base.startswith("SRT_"):
                dirnames[:] = []
                continue
            for fn in filenames:
                low = fn.lower()
                stem = fn[:-4]
                if low.endswith(".mp3"):
                    try:
                        mt = os.path.getmtime(os.path.join(dirpath, fn))
                    except OSError:
                        mt = 0
                    if mt >= mp3_mtime.get(stem, 0):
                        mp3_mtime[stem] = mt
                elif low.endswith(".txt") and stem not in txt_chars:
                    try:
                        with open(os.path.join(dirpath, fn), "r",
                                  encoding="utf-8", errors="ignore") as _tf:
                            txt_chars[stem] = len(_tf.read())
                    except OSError:
                        txt_chars[stem] = 0

        for stem, chars in txt_chars.items():
            if stem in mp3_mtime:
                r["done_files"] += 1
                r["done_chars"] += chars
                if mp3_mtime[stem] >= today_start:
                    r["today_files"] += 1
                    r["today_chars"] += chars
            else:
                r["pend_files"] += 1
                r["pend_chars"] += chars
        return r

    def _update_overview(self):
        """Cap nhat bang tong quan: SAN XUAT (gio voice/file) + TAI NGUYEN. Khong goi API."""
        try:
            from core.quota_report import fleet_report
            from core.maintenance import todays_usage_chars
            from core.masters_store import count_active

            # --- SAN XUAT (voice thuc te tren dia) ---
            root = self.dir_input.text().strip() if hasattr(self, "dir_input") else ""
            p = self._scan_production(root)
            pf = p["pend_files"]
            self._pending_files = pf
            self.ov_today.setText(f"{p['today_files']} file")
            self.ov_today.setToolTip(
                f"Hôm nay đã xử lý {p['today_files']} file (~{self._fmt_hours(p['today_chars'])})")
            self.ov_pending.setText(f"{pf} file")
            self.ov_pending.setToolTip(
                f"Còn tồn {pf} file chưa xử lý (~{self._fmt_hours(p['pend_chars'])})")
            self.ov_pending.setStyleSheet(
                "font-size:18px; font-weight:bold; color:%s;"
                % ("#e67e22" if pf > 0 else "#27ae60"))
            self.ov_donetotal.setText(f"{p['done_files']} file")

            # --- TAI NGUYEN: re nhanh theo mode ---
            mode_c = bool(self.config.get("mode_c_enabled", True))
            if mode_c:
                self._update_overview_mode_c()
            else:
                # Nhan master
                try:
                    self._ov_cap_lbl.setText("Quota tạo được")
                    self._ov_alive_lbl.setText("TK sống")
                    self._ov_master_lbl.setText("Master")
                    self._ov_runway_lbl.setText("Còn ~ngày")
                except Exception:
                    pass
                rep = fleet_report()
                remaining = rep.get("total_remaining", 0)
                used = todays_usage_chars()
                masters = count_active()
                self.ov_capacity.setText(self._fmt_hours(remaining))
                self.ov_alive.setText(str(rep.get("alive_now", 0)))
                self.ov_master.setText(str(masters))
                self.ov_capacity.setStyleSheet(
                    "font-size:15px; font-weight:bold; color:%s;"
                    % ("#e74c3c" if remaining < 500_000 else
                       ("#e67e22" if remaining < 2_000_000 else "#27ae60")))
                self.ov_master.setStyleSheet(
                    "font-size:15px; font-weight:bold; color:%s;"
                    % ("#e74c3c" if masters < 2 else "#27ae60"))
                if used > 0:
                    days = remaining / used
                    self.ov_runway.setText(f"{days:.1f}")
                    rc = "#e74c3c" if days < 1.5 else ("#e67e22" if days < 3 else "#27ae60")
                else:
                    self.ov_runway.setText("∞")
                    rc = "#27ae60"
                self.ov_runway.setStyleSheet(f"font-size:15px; font-weight:bold; color:{rc};")
        except Exception:
            pass

    def _update_overview_mode_c(self):
        """Tai nguyen cho MODE C: IP 4G, so Chrome, trang thai 4G (thay Quota/TK/Master)."""
        # Doi nhan cot tai nguyen sang ngu canh Mode C
        try:
            self._ov_cap_lbl.setText("Nguồn voice")
            self._ov_alive_lbl.setText("IP 4G")
            self._ov_master_lbl.setText("Chrome")
            self._ov_runway_lbl.setText("4G")
        except Exception:
            pass
        # Nguon voice: Mode C = khong gioi han TK
        self.ov_capacity.setText("∞ (anon)")
        self.ov_capacity.setToolTip("Mode C: tạo voice không cần tài khoản, không giới hạn quota TK.")
        self.ov_capacity.setStyleSheet("font-size:15px; font-weight:bold; color:#8e44ad;")
        # IP 4G hien tai (tu engine dang chay, hoac hoi nhanh)
        cur_ip = "—"
        try:
            eng = getattr(self, "_active_mode_c_engine", None)
            if eng is not None:
                cur_ip = eng.current_ip()
            else:
                from accounts.proxy import Proxy4G
                cur_ip = Proxy4G().get_ip() or "—"
        except Exception:
            pass
        self.ov_alive.setText(str(cur_ip))
        self.ov_alive.setToolTip("IP 4G hiện tại (tự xoay sau 16 request/IP).")
        # So Chrome song song cau hinh
        nb = int(self.config.get("mode_c_browsers", 3))
        self.ov_master.setText(f"{nb}")
        self.ov_master.setToolTip("Số Chrome (Camoufox) chạy song song. Đổi ở Cài đặt nâng cao.")
        self.ov_master.setStyleSheet("font-size:15px; font-weight:bold; color:#27ae60;")
        # Trang thai 4G: kiem tra port 10001 (nhanh, khong block)
        alive = self._quick_4g_alive()
        self.ov_runway.setText("OK" if alive else "✗")
        self.ov_runway.setToolTip("4G proxy còn sống? (socks5 127.0.0.1:10001)")
        self.ov_runway.setStyleSheet(
            "font-size:15px; font-weight:bold; color:%s;" % ("#27ae60" if alive else "#e74c3c"))

    @staticmethod
    def _quick_4g_alive():
        """Check nhanh 4G socks5 port 10001 co listen khong (khong gui request)."""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            r = s.connect_ex(("127.0.0.1", 10001))
            s.close()
            return r == 0
        except Exception:
            return False

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
