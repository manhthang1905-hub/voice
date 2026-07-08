"""
11Lab Voice Tool — GUI chính.

Chuyển text thành voice. 2 mode:
- Mode A (Browser): Dùng Chrome + TK từ Account Manager
- Mode B (API): Dùng Firebase login trực tiếp

Chạy: python run.py
"""

import os
import sys
import time

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QSpinBox, QDoubleSpinBox,
    QLineEdit, QGroupBox, QCheckBox, QFileDialog, QTextEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QApplication,
    QAbstractItemView, QProgressBar, QMessageBox, QTabWidget,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QElapsedTimer
from PyQt5.QtGui import QColor

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Chunk LON = it lan cat = GIONG NHAT QUAN hon (master generate di thang).
# Endpoint truc tiep gioi han 5000 ky tu; library voice qua Studio node cho dai hon.
# Neu chunk vuot -> tool tu split (text_too_long). Cac gia tri an toan duoi 5000:
MAX_CHUNK_V3 = 4500            # eleven_v3 (alpha)
MAX_CHUNK_MULTILINGUAL = 5000  # eleven_multilingual_v2
MAX_CHUNK_CJK = 3000           # ja/ko (ky tu day hon)

from core.api_client import AVAILABLE_MODELS, OUTPUT_FORMATS
from core.convert import (
    QuotaExceededError, IPFlaggedError, VoiceNotFoundError,
    VoiceRestrictedError, check_quota, get_voice_info,
    extract_supported_languages, LANGUAGE_NAMES,
)
from utils.config import Config
from utils.logger import log
from utils.audit_log import audit
try:
    from utils.lang_detect import detect_language_from_files as _detect_lang_from_files
except Exception:
    def _detect_lang_from_files(files, **kw):
        return ""

DEFAULT_OUTPUT_DIR = r"D:\AUTO\voice"
POPULAR_LANGUAGES = [
    "en", "vi", "es", "fr", "de", "it", "pt", "ja", "ko", "zh",
    "id", "hi", "ar", "tr", "pl", "ru", "nl", "th",
]


# ============================================================
# VOICE LANGUAGE WORKER
# ============================================================

class VoiceLanguagesWorker(QThread):
    done = pyqtSignal(str, list)
    error = pyqtSignal(str, str)

    def __init__(self, voice_id: str):
        super().__init__()
        self.voice_id = voice_id

    def run(self):
        try:
            from core.mode_b_accounts import get_alive_accounts
            accounts = get_alive_accounts(min_chars=1)
            token = ""
            proxy = None
            for acc in accounts:
                token = (acc.get("api_key") or "").strip()
                if token:
                    break
            if not token:
                try:
                    from core.api_client import firebase_login
                    for acc in accounts:
                        email = (acc.get("email") or "").strip()
                        password = (acc.get("password") or "").strip()
                        if not email or not password:
                            continue
                        try:
                            from accounts.proxy import Proxy4G
                            proxy = Proxy4G().get_for_firebase()
                        except Exception:
                            proxy = None
                        result = firebase_login(email, password, proxy=proxy)
                        token = result.get("idToken", "")
                        if token:
                            break
                except Exception:
                    token = ""
            if not token:
                self.error.emit(self.voice_id, "không có token/API key để đọc voice")
                return
            info = get_voice_info(token, self.voice_id, proxy=proxy)
            if not info:
                self.error.emit(self.voice_id, "không lấy được metadata voice")
                return
            self.done.emit(self.voice_id, extract_supported_languages(info))
        except Exception as e:
            self.error.emit(self.voice_id, str(e)[:120])


# ============================================================
# BATCH WORKER
# ============================================================

class VoiceWorker(QThread):
    """Convert batch: txt → mp3."""
    log_signal = pyqtSignal(str)
    file_started = pyqtSignal(int, str)
    file_progress = pyqtSignal(int, int, int)
    file_done = pyqtSignal(int, str, int)
    file_error = pyqtSignal(int, str)
    all_done = pyqtSignal(int, int)

    def __init__(self, files, output_dir, voice_id, model_id,
                 stability, similarity, speed, output_format,
                 mode, config, language_code=None):
        super().__init__()
        self.files = files
        self.output_dir = output_dir
        self.voice_id = voice_id
        self.model_id = model_id
        self.language_code = language_code
        self.stability = stability
        self.similarity = similarity
        self.speed = speed
        self.output_format = output_format
        self.mode = mode  # "a" or "b"
        self.config = config
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from core.convert import Convert
        from core.text_splitter import prepare_text, clean_text

        conv = Convert()
        conv.default_model = self.model_id
        conv.default_format = self.output_format
        conv.language_code = self.language_code
        conv.stability = self.stability
        conv.similarity = self.similarity
        conv.speed = self.speed

        done = 0
        error = 0

        # Token hiện tại (instance vars để _convert_chunk_with_retry truy cập)
        self._current_token = None
        self._current_email = None
        self._current_api_key = ""
        self._current_password = ""
        self._current_auth_data = None
        self._chars_used = 0  # Credits đã dùng với TK hiện tại
        self._skipped_emails = set()  # TK đã skip (hết quota thật)
        self._consecutive_conn_errors = 0  # Lỗi connection liên tiếp
        self._consecutive_flag_errors = 0  # IP flag liên tiếp
        self._token_time = 0  # Th�?i điểm lấy token (check expiry)
        # Voices đã add tạm vào account; lưu token/email để cleanup đúng TK
        self._added_voices = []
        # Voices vĩnh viễn không dùng được (private clone, bị xóa, hoặc cần Creator plan)
        # Skip ngay tất cả file còn lại có voice này
        self._dead_voice_ids = set()
        self._known_premade_voice_ids = {
            "CwhRBWXzGAHq8TQ4Fs17",
            "JBFqnCBsd6RMkjVDRZzb",
            "EXAVITQu4vr4xnSDxMaL",
            "FGY2WhTYpPnrIDTdsKH5",
            "IKne3meq5aSn9XLyUdCD",
            "N2lVS1w4EtoT3dr4eOWO",
            "SAz9YHcvj6GT2YYXdXww",
            "SOYHLrjzK2X1ezoPC6cr",
            "TX3LPaxmHKxFdv7VOQHJ",
            "Xb7hH8MSUJpSbSDYk0k2",
            "XrExE9yKIg1WjnnlVkGX",
            "bIHbv24MWmeRgasZH58o",
            "cgSgspJ2msm6clMCkdW9",
            "cjVigY5qzO86Huf0OWal",
            "hpp4J3VqNfWAUOO0d1Us",
            "iP95p4xoKVk53GoZ742B",
            "nPczCjzI2devNBz1zQrb",
            "onwK4e9ZLuTAKqWW03F9",
            "pFZP5JQG7iQjIQuC4Bku",
            "pNInz6obpgDQGcFmaJgB",
            "pqHfZKP75CvOlQylNhV4",
        }
        self._voice_category_checked = False

        # === PRE-FLIGHT: kiểm tra proxy ===
        if self.mode == "b":
            ip = self._ensure_proxy()
            if not ip:
                self.log_signal.emit("  �?� Proxy chết — dừng!")
                audit("proxy_dead", phase="preflight")
                self.all_done.emit(0, 0)
                return

        _run_start = time.time()

        for file_idx, filepath in self.files:
            if self._cancelled:
                break

            filename = os.path.basename(filepath)
            base_name = os.path.splitext(filename)[0]
            self.file_started.emit(file_idx, filename)

            # Skip nếu voice đã bị đánh dấu là dead (private/restricted)
            if self.voice_id in self._dead_voice_ids:
                self.log_signal.emit(
                    f"--- {filename} → skip (voice dead: {self.voice_id[:16]}...) ---")
                self.file_error.emit(file_idx, "Voice không khả dụng")
                error += 1
                continue

            # Skip nếu đã có MP3 đầu ra
            mp3_path = os.path.join(self.output_dir,
                                    f"{base_name}.mp3")
            if os.path.exists(mp3_path) and \
               os.path.getsize(mp3_path) > 10000:
                self.log_signal.emit(
                    f"--- {filename} → đã có MP3, b�? qua ---")
                done += 1
                self.file_done.emit(file_idx, mp3_path, os.path.getsize(mp3_path))
                continue

            self.log_signal.emit(f"--- {filename} ---")

            try:
                # Debug: log config
                if file_idx == self.files[0][0]:  # first file only
                    lang_info = self.language_code or "auto"
                    effective_model = (self._library_studio_model()
                                       if self.mode == "b" and self._is_library_voice()
                                       else self.model_id)
                    self.log_signal.emit(
                        f"  [config] voice={self.voice_id} "
                        f"model={self.model_id} studio_model={effective_model} "
                        f"language={lang_info}")

                # �?�?c và chia chunks
                with open(filepath, 'r', encoding='utf-8') as f:
                    text = f.read().strip()
                if not text:
                    self.file_error.emit(file_idx, "File trống")
                    error += 1
                    continue

                # Model-aware chunk size
                # TESTED: v3 = 1x credit, API limit ~5000 chars
                # 4000 OK, 8789 text_too_long
                credit_mult = 1
                if self.mode == "b" and self._is_library_voice():
                    # Master generate DI THANG (khong qua 4G) -> chunk LON duoc.
                    # Chunk cang lon = cang it lan cat = GIONG NHAT QUAN hon.
                    # Neu chunk vuot gioi han model -> tool tu split (text_too_long).
                    # eleven_v3 (alpha) gioi han ~3000/request; multilingual_v2 ~5000.
                    studio = self._library_studio_model()
                    # Gioi han thuc te /request: se test va dat o MAX_CHUNK_* duoi.
                    if "v3" in studio:
                        base = MAX_CHUNK_V3          # eleven_v3 (alpha)
                    else:
                        base = MAX_CHUNK_MULTILINGUAL  # multilingual_v2
                    max_chunk = (base if self.language_code not in ("ja", "ko")
                                 else min(base, MAX_CHUNK_CJK))
                elif 'flash' in self.model_id or 'turbo' in self.model_id:
                    # VM + 4G route ổn định hơn nhiều khi chunk nhỏ hơn.
                    # 10k chars tạo request lớn, dễ lộ lỗi cold-start/HTTPS qua SOCKS.
                    max_chunk = 5000
                else:
                    max_chunk = 4500   # v3: an toan, 2 chunks/TK = 9000 (90%)
                self._credit_multiplier = credit_mult

                if len(text) > max_chunk:
                    chunks = prepare_text(text,
                                          {"max_chars_per_line": max_chunk})
                else:
                    chunks = [clean_text(text)]

                total_chars = sum(len(c) for c in chunks)
                effective_chars = total_chars * credit_mult
                accounts_needed = max(1,
                    (effective_chars + 9999) // 10000)

                # === PRE-FLIGHT: kiểm tra đủ TK ===
                if self.mode == "b" and hasattr(self, '_mode_b_accounts'):
                    available = len(self._mode_b_accounts) - \
                        getattr(self, '_mode_b_idx', 0)
                    if available < accounts_needed:
                        self.log_signal.emit(
                            f"  ⚠ Cần ~{accounts_needed} TK, "
                            f"chỉ còn {available}!")

                credit_info = (f" (x{credit_mult})" 
                               if credit_mult > 1 else "")
                self.log_signal.emit(
                    f"  KẾ HOẠCH: {total_chars:,} chars{credit_info}"
                    f" | {len(chunks)} chunks"
                    f" | cần ~{accounts_needed} TK")

                # === CHECKPOINT: bat buoc du toan bo chunk moi cho merge ===
                checkpoint_dir = os.path.join(
                    self.output_dir, ".chunks", base_name)
                os.makedirs(checkpoint_dir, exist_ok=True)

                file_completed = False
                file_failed = False
                max_resume_passes = 3
                max_full_rebuilds = 2

                for rebuild_round in range(1, max_full_rebuilds + 1):
                    if rebuild_round > 1:
                        self.log_signal.emit(
                            f"  [RESET] Final loi, xoa du lieu cu va lam lai tu dau "
                            f"({rebuild_round}/{max_full_rebuilds})")

                    # Retry cac chunk thieu trong cung 1 lan chay
                    for pass_no in range(1, max_resume_passes + 1):
                        missing = self._find_missing_chunk_indexes(
                            checkpoint_dir, len(chunks))
                        if not missing:
                            break

                        # === SONG SONG (fast-path): generate nhieu chunk cung luc ===
                        # An toan: chunk nao song song KHONG xong se roi xuong vong tuan
                        # tu ben duoi lam not -> khong the sai ket qua, chi nhanh hon.
                        if (len(missing) > 1 and self._master_workspace_enabled()
                                and self._parallel_chunks_enabled()):
                            self._convert_chunks_parallel(
                                conv, chunks, checkpoint_dir, missing)
                            missing = self._find_missing_chunk_indexes(
                                checkpoint_dir, len(chunks))
                            if not missing:
                                break

                        start_chunk = missing[0]
                        if start_chunk > 0:
                            self.log_signal.emit(
                                f"  Resume: {start_chunk}/{len(chunks)} chunks da co")

                        file_failed = False
                        for i in range(start_chunk, len(chunks)):
                            if self._cancelled:
                                file_failed = True
                                break

                            chunk_file = self._chunk_file_path(
                                checkpoint_dir, i)
                            if i not in missing and os.path.exists(chunk_file):
                                continue

                            chunk = chunks[i]
                            chunk_chars = len(chunk)

                            # Kiem tra TK hien tai con du credit khong (co BIEN AN TOAN
                            # de tranh tai dung TK sat nguong -> het quota giua chung).
                            tk_remaining = getattr(
                                self, '_tk_quota', 10000) - self._chars_used
                            mult = getattr(self, '_credit_multiplier', 1)
                            need_credits = chunk_chars * mult
                            buffer = max(1500, int(need_credits * 0.2))
                            need_new = (not self._current_token
                                        or tk_remaining < need_credits + buffer)

                            if need_new:
                                if self._added_voices:
                                    self._cleanup_added_voices()
                                t, e = self._get_token(
                                    need_chars=chunk_chars)
                                self._current_token = t
                                self._current_email = e
                                self._chars_used = 0
                                if not self._current_token:
                                    self.file_error.emit(
                                        file_idx,
                                        f"Khong lay duoc token "
                                        f"(done {i}/{len(chunks)})")
                                    file_failed = True
                                    break
                                self.log_signal.emit(
                                    f"  TK: {self._current_email}")

                            # Convert chunk voi retry
                            audio = self._convert_chunk_with_retry(
                                conv, chunk, i, len(chunks), chunk_chars)

                            # text_too_long -> tu split chunk nho hon
                            if audio == 'SPLIT':
                                mid = len(chunk) // 2
                                nl = chunk.rfind('\n', mid - 500, mid + 500)
                                if nl > 0:
                                    mid = nl + 1
                                part1, part2 = chunk[:mid], chunk[mid:]
                                self.log_signal.emit(
                                    f"  Auto-split chunk {i+1}: "
                                    f"{len(part1):,} + {len(part2):,} chars")
                                sub_parts = [part1, part2]
                                sub_audio_parts = []
                                sub_ok = True
                                for si, sub in enumerate(sub_parts):
                                    sub_audio = self._convert_chunk_with_retry(
                                        conv, sub, i, len(chunks), len(sub))
                                    if sub_audio and sub_audio != 'SPLIT':
                                        sub_audio_parts.append(sub_audio)
                                        self._chars_used += len(sub) * getattr(
                                            self, '_credit_multiplier', 1)
                                    else:
                                        sub_ok = False
                                        break
                                    if si < len(sub_parts) - 1:
                                        import random
                                        time.sleep(2 + random.uniform(1, 3))

                                if sub_ok:
                                    # Merge 2 phan thanh 1 chunk file hop le
                                    from core.audio_merger import merge_audio_bytes
                                    tmp_split = chunk_file + ".split.tmp.mp3"
                                    merge_audio_bytes(
                                        sub_audio_parts, tmp_split,
                                        silence_between_ms=0)
                                    with open(tmp_split, "rb") as sf:
                                        merged_chunk = sf.read()
                                    try:
                                        os.remove(tmp_split)
                                    except Exception:
                                        pass
                                    with open(chunk_file, 'wb') as cf:
                                        cf.write(merged_chunk)
                                    self.log_signal.emit(
                                        f"  chunk {i+1}/{len(chunks)} OK "
                                        f"(split, {len(merged_chunk):,} bytes)")
                                else:
                                    self.log_signal.emit(
                                        f"  chunk {i+1}/{len(chunks)} FAIL")
                                    file_failed = True
                                    break
                                if i < len(chunks) - 1:
                                    import random
                                    time.sleep(2 + random.uniform(1, 3))
                                continue

                            if audio:
                                self._chars_used += chunk_chars * getattr(
                                    self, '_credit_multiplier', 1)

                                # Checkpoint: luu chunk ra disk ngay
                                with open(chunk_file, 'wb') as cf:
                                    cf.write(audio)

                                # Cap nhat usage
                                if self._current_email:
                                    if self.mode == "a":
                                        try:
                                            from accounts.bridge import mark_used
                                            mark_used("elevenlabs",
                                                      self._current_email,
                                                      chunk_chars)
                                        except Exception:
                                            pass
                                    elif self.mode == "b":
                                        try:
                                            from core.mode_b_accounts import (
                                                update_account_usage)
                                            mult = getattr(
                                                self, '_credit_multiplier', 1)
                                            update_account_usage(
                                                self._current_email,
                                                chunk_chars * mult,
                                                getattr(self,
                                                        '_current_reset_unix', 0))
                                        except Exception:
                                            pass

                                audit("chunk_ok",
                                      file=filename, chunk=i+1,
                                      total=len(chunks),
                                      chars=chunk_chars,
                                      email=self._current_email or "",
                                      bytes=len(audio))

                                self.log_signal.emit(
                                    f"  chunk {i+1}/{len(chunks)} OK "
                                    f"({len(audio):,} bytes)")

                                # Re-check token expiry (KHONG check_quota moi chunk nua:
                                # pool da theo doi quota + tu roi TK khi het -> bo goi
                                # /v1/user/subscription ~2-3s/chunk = nhanh hon).
                                if self.mode == "b" and self._current_token:
                                    if time.time() - self._token_time > 3000:
                                        self.log_signal.emit(
                                            "  Token > 50 phut -> refresh")
                                        if self._added_voices:
                                            self._cleanup_added_voices()
                                        t, e = self._get_token(
                                            need_chars=chunk_chars)
                                        if t:
                                            self._current_token = t
                                            self._current_email = e
                                            self._chars_used = 0
                            else:
                                saved = len(chunks) - len(
                                    self._find_missing_chunk_indexes(
                                        checkpoint_dir, len(chunks)))
                                self.log_signal.emit(
                                    f"  chunk {i+1}/{len(chunks)} FAIL "
                                    f"- da luu {saved}/{len(chunks)} chunks, retry tiep")
                                file_failed = True
                                break

                            if i < len(chunks) - 1:
                                # Master mode (workspace token, khong bi flag) -> nghi
                                # ngan. Mode khac giu 3-5s de tranh rate-limit/flag.
                                if self._master_workspace_enabled():
                                    time.sleep(0.5)
                                else:
                                    import random
                                    time.sleep(2 + random.uniform(1, 3))

                        if self._cancelled:
                            break

                        remaining = self._find_missing_chunk_indexes(
                            checkpoint_dir, len(chunks))
                        if not remaining:
                            file_failed = False
                            break
                        if pass_no < max_resume_passes:
                            self.log_signal.emit(
                                f"  Con thieu {len(remaining)} chunk(s), "
                                f"retry pass {pass_no+1}/{max_resume_passes}")
                            time.sleep(2)

                    if self._cancelled:
                        file_failed = True
                        break

                    # Chot chan: tuyet doi khong merge neu thieu chunk
                    missing_after = self._find_missing_chunk_indexes(
                        checkpoint_dir, len(chunks))
                    if missing_after:
                        file_failed = True
                        self.log_signal.emit(
                            f"  Thieu {len(missing_after)} chunk(s), khong merge file cuoi")
                    else:
                        # Ghep chunks va luu file
                        os.makedirs(self.output_dir, exist_ok=True)
                        mp3_path = os.path.join(self.output_dir,
                                                f"{base_name}.mp3")
                        audio_parts = self._load_checkpoint_audio_parts(
                            checkpoint_dir, len(chunks))
                        if len(audio_parts) != len(chunks):
                            file_failed = True
                            self.log_signal.emit(
                                "  Khong load du checkpoint chunks, khong merge")
                        else:
                            try:
                                if len(audio_parts) == 1:
                                    with open(mp3_path, 'wb') as f:
                                        f.write(audio_parts[0])
                                else:
                                    from core.audio_merger import merge_audio_bytes
                                    merge_audio_bytes(audio_parts, mp3_path)
                            except Exception as merge_err:
                                file_failed = True
                                self.log_signal.emit(
                                    f"  Merge loi: {merge_err}")

                            # Chot chan cuoi: mp3 final phai hop le, du thoi luong
                            if not file_failed and not self._validate_final_mp3(
                                    mp3_path, checkpoint_dir, len(chunks)):
                                file_failed = True
                                self.log_signal.emit(
                                    "  MP3 cuoi loi/thieu thoi luong")

                    if not file_failed:
                        size = os.path.getsize(mp3_path)
                        self.file_done.emit(file_idx, mp3_path, size)
                        done += 1
                        self.log_signal.emit(
                            f"  OK {size/1024/1024:.1f} MB")
                        audit("file_done", file=filename,
                              chunks=len(chunks), size=size)

                        # Xoa checkpoint sau khi hoan thanh
                        try:
                            import shutil
                            shutil.rmtree(checkpoint_dir, ignore_errors=True)
                        except Exception:
                            pass

                        # Xoa voice da add tam (giai phong slot)
                        self._cleanup_added_voices()
                        file_completed = True
                        break

                    # Final loi: reset toan bo du lieu ma nay roi chay lai tu dau
                    if rebuild_round < max_full_rebuilds:
                        self._reset_code_outputs(base_name, checkpoint_dir)
                        time.sleep(2)

                if not file_completed:
                    if not self._cancelled:
                        error += 1
                    continue

            except Exception as e:
                self.file_error.emit(file_idx, str(e)[:50])
                error += 1
            finally:
                self._cleanup_added_voices()

        # Summary
        duration = time.time() - _run_start
        self.log_signal.emit(
            f"\n=== XONG: {done} OK, {error} loi"
            f" | {duration/60:.0f} phut ===")
        audit("run_done", done=done, error=error,
              duration_s=int(duration),
              skipped=len(self._skipped_emails))
        self.all_done.emit(done, error)

    def _track_added_voice(self, original_vid: str, new_vid: str):
        """Ghi lại voice đã add để cleanup bằng đúng token account."""
        if not new_vid:
            return
        for item in self._added_voices:
            if (item.get("added") == new_vid and
                    item.get("email", "") == (self._current_email or "")):
                return
        self._added_voices.append({
            "original": original_vid,
            "added": new_vid,
            "token": self._current_token,
            "email": self._current_email or "",
        })

    def _has_added_voice_for_current_account(self, voice_id: str) -> bool:
        email = self._current_email or ""
        for item in self._added_voices:
            if item.get("email", "") != email:
                continue
            if voice_id in (item.get("original", ""), item.get("added", "")):
                return True
        return False

    def _library_studio_model(self) -> str:
        if self.language_code in ("ja", "ko", "tr"):
            return "eleven_multilingual_v2"
        return "eleven_v3"

    def _cleanup_added_voices(self):
        """Xóa các voice đã add tạm vào account để giải phóng slot.

        Được gọi sau khi mỗi file hoàn tất (thành công hoặc thất bại).
        Accounts free/starter chỉ có 3 voice slots — cần giải phóng ngay.
        """
        if not self._added_voices:
            return

        proxy_dict = self._get_proxy()
        from core.convert import remove_voice_from_account

        remaining = []
        for item in list(self._added_voices):
            original_vid = item.get("original", "")
            new_vid = item.get("added", "")
            token = item.get("token")
            email = item.get("email", "")
            if self.voice_id == new_vid:
                self.voice_id = original_vid
            if not new_vid or not token:
                continue
            try:
                ok = remove_voice_from_account(
                    token, new_vid, proxy=proxy_dict)
                if ok:
                    self.log_signal.emit(
                        f"  Voice {new_vid[:16]}... đã xóa khỏi TK "
                        f"{email} (slot giải phóng)")
                else:
                    remaining.append(item)
                    self.log_signal.emit(
                        f"  Không xóa được voice {new_vid[:16]}... "
                        f"khỏi TK {email}, sẽ thử lại sau")
            except Exception as e:
                remaining.append(item)
                self.log_signal.emit(
                    f"  cleanup_voice error: {str(e)[:40]}")

        self._added_voices = remaining

    def _ensure_proxy(self):
        """Verify proxy alive. Retry 3x with rotate.
        Returns IP string or None if dead."""
        from accounts.proxy import Proxy4G, API_BASE, API_KEY
        import requests as _req
        p = Proxy4G()

        # Bước 1: kiểm tra API server máy chính có reach được không
        try:
            r = _req.get(f"{API_BASE}/list?key={API_KEY}", timeout=10)
            if r.status_code != 200:
                self.log_signal.emit(f"  �?� API server lỗi {r.status_code}")
                audit("proxy_dead", phase="api_check")
                return None
            proxies_info = r.json().get("proxies", [])
            if not proxies_info:
                self.log_signal.emit("  �?� Không có phone nào trong server")
                audit("proxy_dead", phase="no_phone")
                return None
            ph = proxies_info[0]
            reported_ip = (ph.get("current_4g_ip") or "").strip()
            if not ph.get("proxy_running", False):
                self.log_signal.emit(
                    f"  ⚠ Proxy local chưa sẵn sàng cho {ph.get('name','')} "
                    f"— thử restart...")
                # G�?i start để restart ADB forward
                _req.post(
                    f"{API_BASE}/proxy/{ph['id']}/start?key={API_KEY}",
                    timeout=10)
                time.sleep(2)

                # Nếu phone chưa bật EveryProxy/SIM/4G thì báo đúng nguyên nhân,
                # không rotate mù vì rotate không sửa được lỗi này.
                try:
                    scan = _req.post(
                        f"{API_BASE}/scan?key={API_KEY}",
                        timeout=25)
                    if scan.status_code == 200:
                        scan_data = scan.json()
                        devices = scan_data.get("devices", [])
                        if devices:
                            steps = devices[0].get("steps", [])
                            for step in steps:
                                if step.get("status") == "fail":
                                    step_name = step.get("step", "Unknown")
                                    message = step.get("message", "")
                                    fix = step.get("fix", "")
                                    self.log_signal.emit(
                                        f"  �?� {step_name}: {message}")
                                    if fix:
                                        self.log_signal.emit(
                                            f"     Fix: {fix}")
                                    audit("proxy_dead",
                                          phase=f"scan_{step_name.lower()}",
                                          message=message)
                                    return None
                except Exception:
                    pass
            elif reported_ip:
                self.log_signal.emit(
                    f"  ✓ Proxy OK (server IP: {reported_ip})")
                audit("proxy_ok", ip=reported_ip, phase="server_report")
                return reported_ip
        except Exception as e:
            self.log_signal.emit(f"  �?� Không reach được API server: {e}")
            audit("proxy_dead", phase="api_unreachable")
            return None

        for attempt in range(3):
            try:
                ip = p.get_ip()
                if ip:
                    self.log_signal.emit(
                        f"  ✓ Proxy OK (IP: {ip})")
                    audit("proxy_ok", ip=ip, attempt=attempt+1)
                    return ip
            except Exception:
                pass
            self.log_signal.emit(
                f"  ⚠ Proxy không phản hồi, rotate "
                f"({attempt+1}/3)...")
            try:
                p.rotate(wait=25)
            except Exception:
                pass
        return None

    def _wait_proxy_ip(self, previous_ip: str = "", timeout: int = 30):
        """Wait until server/proxy reports a usable IP.

        Prefer server-reported current_4g_ip because it updates right after rotate.
        Fallback to direct proxy probe if needed.
        """
        from accounts.proxy import Proxy4G, API_BASE, API_KEY
        import requests as _req

        previous_ip = (previous_ip or "").strip()
        p = Proxy4G()
        deadline = time.time() + max(5, timeout)

        while time.time() < deadline:
            try:
                r = _req.get(f"{API_BASE}/list?key={API_KEY}", timeout=10)
                if r.status_code == 200:
                    proxies_info = r.json().get("proxies", [])
                    if proxies_info:
                        server_ip = (proxies_info[0].get("current_4g_ip") or "").strip()
                        if server_ip and server_ip != previous_ip:
                            return server_ip
            except Exception:
                pass

            try:
                ip = (p.get_ip() or "").strip()
                if ip and ip != previous_ip:
                    return ip
            except Exception:
                pass

            time.sleep(2)

        return ""

    def _warm_tts_route(self, timeout: int = 20) -> bool:
        """Warm up the current proxy route quietly before first TTS call."""
        import requests as _req

        proxy = self._get_proxy()
        deadline = time.time() + max(5, timeout)
        while time.time() < deadline:
            try:
                resp = _req.get(
                    "https://api.us.elevenlabs.io/v1/user/subscription",
                    proxies=proxy,
                    timeout=12,
                    headers={"accept": "*/*"},
                )
                if resp.status_code in (200, 401):
                    return True
            except Exception:
                pass
            time.sleep(2)
        return False

    def _wait_tts_route_ready(self, timeout: int = 60) -> bool:
        """Wait until SOCKS route can reach ElevenLabs API stably."""
        import requests as _req

        proxy = self._get_proxy()
        deadline = time.time() + max(5, timeout)
        last_err = ""

        while time.time() < deadline:
            try:
                resp = _req.get(
                    "https://api.us.elevenlabs.io/v1/user/subscription",
                    proxies=proxy,
                    timeout=20,
                    headers={"accept": "*/*"},
                )
                # 401/200 đều chứng minh route tới ElevenLabs đã thông.
                if resp.status_code in (200, 401):
                    return True
                last_err = f"http_{resp.status_code}"
            except Exception as exc:
                last_err = str(exc)[:80]
            time.sleep(3)

        self.log_signal.emit(
            f"  ⚠ Route TTS chưa ổn định: {last_err}")
        return False

    def _try_browser_library_convert(self, chunk):
        if self.mode == "b":
            return None
        if not self._is_library_voice():
            return None
        if not self._current_email or not self._current_auth_data:
            return None

        try:
            from accounts.proxy import Proxy4G
            from accounts.stealth import Stealth
            from core.browser_convert import BrowserConvert

            profiles_dir = os.path.join(
                PROJECT_ROOT, "config", "browser_convert_profiles")
            os.makedirs(profiles_dir, exist_ok=True)

            stealth = Stealth(profiles_dir)
            browser = BrowserConvert(stealth)
            profile_id = f"bc_{self._current_email}"
            chrome_proxy = Proxy4G().get_for_chrome()

            self.log_signal.emit(
                f"  Browser fallback: {self._current_email} -> web generate")
            audio = browser.convert(
                profile_id=profile_id,
                text=chunk,
                voice_id=self.voice_id,
                proxy=chrome_proxy,
                timeout=600,
                auth_data=self._current_auth_data,
                email=self._current_email,
                api_key=self._current_api_key,
            )
            if audio and self._validate_audio(audio):
                self._record_browser_fallback_result(
                    self._current_email, True, "")
                self.log_signal.emit(
                    f"  Browser fallback OK: {len(audio):,} bytes")
                return audio
            err = getattr(browser, "last_error", "") or "unknown"
            self._record_browser_fallback_result(
                self._current_email, False, err)
            self.log_signal.emit(
                f"  Browser fallback fail: {err[:120]}")
        except Exception as exc:
            self._record_browser_fallback_result(
                self._current_email, False, str(exc))
            self.log_signal.emit(
                f"  Browser fallback error: {str(exc)[:60]}")
        return None

    def _handle_ip_flagged(self, chunk, chunk_chars, account_switches, try_browser=False):
        if try_browser:
            browser_audio = self._try_browser_library_convert(chunk)
            if browser_audio:
                self._consecutive_conn_errors = 0
                self._consecutive_flag_errors = 0
                return browser_audio, account_switches

        account_switches += 1
        flagged = self._current_email
        if flagged:
            self._skipped_emails.add(flagged)
        self.log_signal.emit(
            f"  ⛔ IP/route unusual_activity khi dùng {flagged} "
            f"[{account_switches}] — đưa vào danh sách flagged, đổi route/TK")
        audit("ip_flagged", email=flagged or "")

        if self.mode == "a" and flagged:
            try:
                from accounts.bridge import _get_db
                db = _get_db()
                db.set_error(flagged, "elevenlabs",
                             error_step=99,
                             error_msg="unusual_activity_route")
            except Exception:
                pass
        elif self.mode == "b" and flagged:
            try:
                from core.mode_b_accounts import mark_flagged
                mark_flagged(flagged)
            except Exception:
                pass
            if hasattr(self, '_batch_verified'):
                self._batch_verified = False
                self._batch_start = getattr(
                    self, '_mode_b_idx', 0)

        if self._added_voices:
            self._cleanup_added_voices()

        self._current_token = None
        self._current_email = None
        self._current_api_key = ""
        self._current_password = ""
        self._current_auth_data = None
        token, email = self._get_token(
            need_chars=chunk_chars)
        if token:
            self._current_token = token
            self._current_email = email
            self._chars_used = 0
            return None, account_switches

        self.log_signal.emit("  Không còn TK!")
        return "__NO_ACCOUNTS__", account_switches

    def _validate_audio(self, audio):
        """Validate audio: MP3 header + minimum size."""
        if not audio or len(audio) < 1000:
            return False
        # MP3: FF FB / FF F3 / ID3
        if (audio[:3] == b'ID3' or
            audio[:2] in (b'\xff\xfb', b'\xff\xf3',
                          b'\xff\xf2')):
            return True
        # Có thể là RIFF/WAV header
        if audio[:4] == b'RIFF':
            return True
        return False

    def _browser_fallback_stats_path(self):
        return os.path.join(
            PROJECT_ROOT, "config", "browser_fallback_stats.json")

    def _load_browser_fallback_stats(self):
        if hasattr(self, "_browser_fallback_stats"):
            return self._browser_fallback_stats
        import json
        path = self._browser_fallback_stats_path()
        defaults = {
            "gadisonrouillier70639@hotmail.com": {
                "success": 3,
                "fail": 0,
                "last_success_ts": int(time.time()),
                "last_error": "",
            },
            "barralaga43723@hotmail.com": {
                "success": 2,
                "fail": 0,
                "last_success_ts": int(time.time()),
                "last_error": "",
            },
        }
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for email, seed in defaults.items():
                        data.setdefault(email, seed.copy())
                    self._browser_fallback_stats = data
                    return data
        except Exception:
            pass
        self._browser_fallback_stats = defaults
        return self._browser_fallback_stats

    def _save_browser_fallback_stats(self):
        import json
        path = self._browser_fallback_stats_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = self._load_browser_fallback_stats()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def _record_browser_fallback_result(self, email: str, ok: bool, err: str):
        if not email:
            return
        data = self._load_browser_fallback_stats()
        row = data.setdefault(email, {
            "success": 0,
            "fail": 0,
            "last_success_ts": 0,
            "last_error": "",
        })
        if ok:
            row["success"] = int(row.get("success", 0)) + 1
            row["last_success_ts"] = int(time.time())
            row["last_error"] = ""
        else:
            row["fail"] = int(row.get("fail", 0)) + 1
            row["last_error"] = (err or "")[:200]
        try:
            self._save_browser_fallback_stats()
        except Exception:
            pass

    def _reorder_mode_b_accounts_for_default_api(self):
        # Default voice: giữ nguyên thứ tự đã có trong JSON status.
        # Chỉ cần đưa lô tài khoản cần ưu tiên lên đầu dữ liệu là đủ.
        return

    def _reorder_mode_b_accounts_for_browser(self):
        if not self._is_library_voice():
            return
        if not hasattr(self, "_mode_b_accounts"):
            return
        stats = self._load_browser_fallback_stats()

        def _key(acc):
            row = stats.get(acc.get("email", ""), {})
            succ = int(row.get("success", 0) or 0)
            fail = int(row.get("fail", 0) or 0)
            last = int(row.get("last_success_ts", 0) or 0)
            chars = int(acc.get("chars_remaining", 0) or 0)
            return (
                0 if succ > 0 else 1,
                -succ,
                fail,
                -last,
                -chars,
            )

        self._mode_b_accounts.sort(key=_key)
        top = [a.get("email", "") for a in self._mode_b_accounts[:5]]
        if top:
            self.log_signal.emit(
                "  Browser-priority: " + ", ".join(top))

    def _chunk_file_path(self, checkpoint_dir: str, idx: int) -> str:
        return os.path.join(checkpoint_dir, f"chunk_{idx:04d}.mp3")

    def _find_missing_chunk_indexes(self, checkpoint_dir: str,
                                    total_chunks: int) -> list:
        missing = []
        for i in range(total_chunks):
            p = self._chunk_file_path(checkpoint_dir, i)
            if (not os.path.exists(p)) or os.path.getsize(p) <= 1000:
                missing.append(i)
                continue
            try:
                with open(p, "rb") as f:
                    head = f.read(4096)
                if not self._validate_audio(head):
                    missing.append(i)
            except Exception:
                missing.append(i)
        return missing

    def _load_checkpoint_audio_parts(self, checkpoint_dir: str,
                                     total_chunks: int) -> list:
        parts = []
        for i in range(total_chunks):
            p = self._chunk_file_path(checkpoint_dir, i)
            if not os.path.exists(p):
                return []
            with open(p, "rb") as f:
                b = f.read()
            if not self._validate_audio(b):
                return []
            parts.append(b)
        return parts

    def _audio_duration_ms(self, audio_path: str) -> int:
        try:
            from pydub import AudioSegment
            return len(AudioSegment.from_file(audio_path))
        except Exception:
            pass
        try:
            import subprocess
            r = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    audio_path,
                ],
                capture_output=True, text=True, timeout=20
            )
            if r.returncode == 0:
                return int(float((r.stdout or "0").strip()) * 1000)
        except Exception:
            pass
        return 0

    def _validate_final_mp3(self, mp3_path: str, checkpoint_dir: str,
                            total_chunks: int) -> bool:
        if (not os.path.exists(mp3_path)) or os.path.getsize(mp3_path) <= 1000:
            return False
        try:
            with open(mp3_path, "rb") as f:
                head = f.read(4096)
            if not self._validate_audio(head):
                return False
        except Exception:
            return False

        final_ms = self._audio_duration_ms(mp3_path)
        if final_ms <= 1000:
            return False

        expected_ms = 0
        for i in range(total_chunks):
            p = self._chunk_file_path(checkpoint_dir, i)
            if not os.path.exists(p):
                return False
            d = self._audio_duration_ms(p)
            if d <= 200:
                return False
            expected_ms += d

        # Final file co silence giua chunk, nen chi can dat nguong toi thieu.
        return final_ms >= int(expected_ms * 0.90)

    def _reset_code_outputs(self, base_name: str, checkpoint_dir: str):
        import shutil
        mp3_path = os.path.join(self.output_dir, f"{base_name}.mp3")
        try:
            if os.path.exists(mp3_path):
                os.remove(mp3_path)
        except Exception:
            pass
        try:
            shutil.rmtree(checkpoint_dir, ignore_errors=True)
        except Exception:
            pass
        os.makedirs(checkpoint_dir, exist_ok=True)

    def _parallel_chunks_enabled(self):
        """Option: bat generate SONG SONG cac chunk (config 'parallel_chunks').

        Mac dinh TAT (giu luong tuan tu cu an toan). Bat o Auto Convert > Cai dat
        nang cao. Chi ap dung cho master mode + file nhieu chunk.
        """
        try:
            return bool(self.config.get("parallel_chunks", True))
        except Exception:
            return True

    def _convert_chunks_parallel(self, conv, chunks, checkpoint_dir, missing):
        """Generate cac chunk THIEU cung luc (moi chunk 1 token rieng tu pool).

        DOC LAP, khong dung self._current_token/self.voice_id-mutate -> an toan chay
        song song. Chunk nao khong xong -> de trong, vong tuan tu ben ngoai lam not.
        """
        from concurrent.futures import ThreadPoolExecutor
        from core.convert import (add_voice_to_account, QuotaExceededError,
                                   IPFlaggedError, VoiceNotFoundError,
                                   VoiceRestrictedError)
        from core.api_client import ElevenLabsError

        pool = self._get_master_pool()
        proxy = self._get_proxy()
        is_lib = self._is_library_voice()
        model = self._library_studio_model() if is_lib else None
        mult = getattr(self, '_credit_multiplier', 1)
        try:
            workers = int(self.config.get("parallel_chunk_workers", 3))
        except Exception:
            workers = 3
        workers = max(2, min(workers, len(missing), 6))

        def _gen_one(idx):
            chunk = chunks[idx]
            chunk_file = self._chunk_file_path(checkpoint_dir, idx)
            for _ in range(20):
                if self._cancelled:
                    return idx, False
                pick = pool.next_workspace(need_chars=len(chunk) * mult)
                if not pick:
                    return idx, False
                _email, ws, token, _rem = pick
                try:
                    vid = self.voice_id
                    if is_lib:
                        nid = add_voice_to_account(token, vid, proxy=proxy)
                        if nid:
                            vid = nid
                    audio = conv.convert_text(chunk, vid, token,
                                              proxy=proxy, model_id=model)
                    if audio and self._validate_audio(audio):
                        with open(chunk_file, 'wb') as f:
                            f.write(audio)
                        self.log_signal.emit(
                            f"  [//] chunk {idx+1}/{len(chunks)} OK ({len(audio):,} bytes)")
                        return idx, True
                    pool.mark_bad_workspace(ws)   # audio invalid -> doi ws
                except VoiceRestrictedError:
                    return idx, False             # voice can plan cao -> bo
                except (QuotaExceededError, IPFlaggedError, VoiceNotFoundError):
                    pool.mark_bad_workspace(ws)   # tam thoi -> reset se quay lai
                except ElevenLabsError as e:
                    if getattr(e, 'disabled', False):
                        pool.mark_disabled_workspace(ws)   # vinh vien
                    elif (getattr(e, 'quota', False) or getattr(e, 'flagged', False)
                            or getattr(e, 'status_code', 0) == 404):
                        pool.mark_bad_workspace(ws)        # tam thoi
                    # loi khac -> thu ws khac
                except Exception:
                    pass   # network... -> thu lai
            return idx, False

        ok = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for idx, done in ex.map(_gen_one, missing):
                if done:
                    ok += 1
        self.log_signal.emit(
            f"  [//] Song song {workers} luong: {ok}/{len(missing)} chunk xong")
        return ok

    def _convert_chunk_with_retry(self, conv, chunk, chunk_idx,
                                  total_chunks, chunk_chars):
        """Convert 1 chunk. Xử lý m�?i lỗi, tự recover.
        
        Quota/Flag = đổi TK (không giới hạn, miễn còn TK)
        Network/Server = retry tối đa 8 lần
        """
        import random
        max_real_retries = 8   # Lỗi thật (network, server)
        max_account_switches = 12 if self._is_library_voice() else 30
        
        real_retries = 0
        account_switches = 0

        while real_retries < max_real_retries and \
              account_switches < max_account_switches:
            if self._cancelled:
                return None

            try:
                if self._is_library_voice():
                    proxy_dict = self._get_proxy()
                    self.log_signal.emit("  Library API: Studio node audio...")
                    audio = conv.convert_text(
                        chunk, self.voice_id,
                        self._current_token, proxy=proxy_dict,
                        model_id=self._library_studio_model())
                    if audio and self._validate_audio(audio):
                        self._consecutive_conn_errors = 0
                        self.log_signal.emit(
                            f"  Library API OK: {len(audio):,} bytes")
                        return audio
                    self.log_signal.emit(
                        f"  Library API audio invalid ({len(audio) if audio else 0} bytes)"
                        " → đổi IP/TK")
                    result, account_switches = self._handle_ip_flagged(
                        chunk, chunk_chars, account_switches, try_browser=False)
                    if isinstance(result, (bytes, bytearray)):
                        return result
                    if result == "__NO_ACCOUNTS__":
                        return None
                    continue

                proxy_dict = self._get_proxy()
                audio = conv.convert_text(
                    chunk, self.voice_id,
                    self._current_token, proxy=proxy_dict)

                # Validate audio
                if not self._validate_audio(audio):
                    self.log_signal.emit(
                        f"  ⚠ Audio invalid ({len(audio) if audio else 0} bytes)"
                        f" → retry {real_retries+1}")
                    real_retries += 1
                    time.sleep(3)
                    continue

                self._consecutive_conn_errors = 0
                return audio

            except VoiceNotFoundError:
                if self._has_added_voice_for_current_account(self.voice_id):
                    account_switches += 1
                    failed_email = self._current_email or ""
                    self.log_signal.emit(
                        f"  [VOICE] Đã add nhưng Studio vẫn không thấy voice"
                        f" → đổi TK [{account_switches}]")
                    audit("voice_added_but_not_found",
                          voice_id=self.voice_id,
                          email=failed_email)
                    if failed_email:
                        self._skipped_emails.add(failed_email)
                    if self._added_voices:
                        self._cleanup_added_voices()
                    token, email = self._get_token(
                        need_chars=chunk_chars)
                    if token:
                        self._current_token = token
                        self._current_email = email
                        self._chars_used = 0
                        self.log_signal.emit(
                            f"  Thử TK mới: {email}")
                        continue
                    self._dead_voice_ids.add(self.voice_id)
                    return None

                # Thử add voice vào account hiện tại
                proxy_dict = self._get_proxy()
                new_vid = None
                if self._current_token:
                    from core.convert import (
                        add_voice_to_account,
                        cleanup_library_voices)
                    self.log_signal.emit(
                        f"  🎤 [VOICE] Voice chưa có trong TK "
                        f"→ dọn voice cũ rồi add {self.voice_id[:16]}...")
                    removed = cleanup_library_voices(
                        self._current_token, self.voice_id,
                        proxy=proxy_dict)
                    if removed:
                        self.log_signal.emit(
                            f"  Đã xóa {removed} voice thư viện cũ")
                    new_vid = add_voice_to_account(
                        self._current_token, self.voice_id,
                        proxy=proxy_dict)

                if new_vid:
                    # Lưu lại để xóa sau khi xong file
                    self._track_added_voice(self.voice_id, new_vid)
                    # Cập nhật voice_id đang dùng
                    old_vid = self.voice_id
                    self.voice_id = new_vid
                    self.log_signal.emit(
                        f"  ✅ Added! {old_vid[:16]}... "
                        f"→ new_id={new_vid[:16]}... → retry")
                    audit("voice_added", original=old_vid,
                          new_id=new_vid,
                          email=self._current_email or "")
                    # Không tăng real_retries — đây không phải lỗi thật
                    continue
                else:
                    account_switches += 1
                    failed_email = self._current_email or ""
                    self.log_signal.emit(
                        f"  [VOICE] TK {failed_email} không add được voice"
                        f" → đổi TK [{account_switches}]")
                    audit("voice_add_failed",
                          voice_id=self.voice_id,
                          email=failed_email)
                    if failed_email:
                        self._skipped_emails.add(failed_email)
                    if self._added_voices:
                        self._cleanup_added_voices()
                    token, email = self._get_token(
                        need_chars=chunk_chars)
                    if token:
                        self._current_token = token
                        self._current_email = email
                        self._chars_used = 0
                        self.log_signal.emit(
                            f"  Thử TK mới: {email}")
                        continue
                    self.log_signal.emit(
                        f"  [VOICE] Không còn TK nào add được"
                        f" cho voice {self.voice_id[:16]}...")
                    self._dead_voice_ids.add(self.voice_id)
                    return None

            except VoiceRestrictedError:
                if self._has_added_voice_for_current_account(self.voice_id):
                    account_switches += 1
                    failed_email = self._current_email or ""
                    self.log_signal.emit(
                        f"  [VOICE_PLAN] Đã add nhưng Studio vẫn báo plan"
                        f" → đổi TK [{account_switches}]")
                    audit("voice_added_still_restricted",
                          voice_id=self.voice_id,
                          email=failed_email)
                    if failed_email:
                        self._skipped_emails.add(failed_email)
                    if self._added_voices:
                        self._cleanup_added_voices()
                    token, email = self._get_token(
                        need_chars=chunk_chars)
                    if token:
                        self._current_token = token
                        self._current_email = email
                        self._chars_used = 0
                        self.log_signal.emit(
                            f"  Thử TK mới: {email}")
                        continue
                    self._dead_voice_ids.add(self.voice_id)
                    return None

                # Voice professional/shared cần được add vào library của từng TK trước.
                if self._is_library_voice() and self._current_token:
                    proxy_dict = self._get_proxy()
                    try:
                        from core.convert import (
                            add_voice_to_account,
                            cleanup_library_voices)
                        self.log_signal.emit(
                            f"  [VOICE_PLAN] Dọn voice cũ rồi add vào TK "
                            f"{self._current_email or ''}...")
                        removed = cleanup_library_voices(
                            self._current_token, self.voice_id,
                            proxy=proxy_dict)
                        if removed:
                            self.log_signal.emit(
                                f"  Đã xóa {removed} voice thư viện cũ")
                        new_vid = add_voice_to_account(
                            self._current_token, self.voice_id,
                            proxy=proxy_dict)
                    except Exception as add_err:
                        new_vid = None
                        self.log_signal.emit(
                            f"  Add voice lỗi: {str(add_err)[:60]}")

                    if new_vid:
                        self._track_added_voice(self.voice_id, new_vid)
                        old_vid = self.voice_id
                        self.voice_id = new_vid
                        audit("voice_added_for_plan",
                              original=old_vid,
                              new_id=new_vid,
                              email=self._current_email or "")
                        self.log_signal.emit(
                            f"  Add voice OK → retry API Studio")
                        continue

                # Nếu add không được thì mới coi là TK hiện tại không hợp lệ.
                account_switches += 1
                flagged_email = self._current_email or ""
                self.log_signal.emit(
                    f"  [VOICE_PLAN] TK {flagged_email} chưa dùng được voice"
                    f" → đổi TK [{account_switches}]")
                audit("voice_restricted",
                      voice_id=self.voice_id,
                      email=flagged_email)
                if flagged_email:
                    self._skipped_emails.add(flagged_email)
                if self._added_voices:
                    self._cleanup_added_voices()
                token, email = self._get_token(
                    need_chars=chunk_chars)
                if token:
                    self._current_token = token
                    self._current_email = email
                    self._chars_used = 0
                    self.log_signal.emit(
                        f"  Thử TK mới: {email}")
                    continue
                self.log_signal.emit(
                    f"  [VOICE_PLAN] Không còn TK nào dùng được"
                    f" cho voice {self.voice_id[:16]}...")
                self._dead_voice_ids.add(self.voice_id)
                return None

            except QuotaExceededError:
                account_switches += 1
                # Check quota thật
                real_left = "?"
                if self._current_token:
                    try:
                        q = check_quota(
                            self._current_token, proxy=None)
                        if q:
                            real_left = q["chars_remaining"]
                    except Exception:
                        pass
                self.log_signal.emit(
                    f"  💰 Hết quota "
                    f"(dùng ~{self._chars_used:,}, "
                    f"API còn: {real_left})"
                    f" → đổi TK [{account_switches}]")
                if self._current_email:
                    if self.mode == "a":
                        try:
                            from accounts.bridge import mark_used
                            mark_used("elevenlabs",
                                      self._current_email, 999999)
                        except Exception:
                            pass
                    elif self.mode == "b":
                        try:
                            from core.mode_b_accounts import (
                                set_remaining)
                            set_remaining(
                                self._current_email, 0,
                                getattr(self,
                                        '_current_reset_unix', 0))
                        except Exception:
                            pass
                self._skipped_emails.add(self._current_email)
                # Danh dau workspace het quota (TAM THOI) -> pool khong offer lai
                # (het churn "hết quota → đổi TK" lien tuc); reset xong se quay lai.
                _qws = getattr(self, '_current_ws_id', "") or ""
                if _qws:
                    try:
                        self._get_master_pool().mark_bad_workspace(_qws)
                    except Exception:
                        pass
                audit("quota_exceeded",
                      email=self._current_email or "",
                      chars_used=self._chars_used,
                      api_remaining=str(real_left))
                if self._added_voices:
                    self._cleanup_added_voices()
                token, email = self._get_token(
                    need_chars=chunk_chars)
                if token:
                    self._current_token = token
                    self._current_email = email
                    self._chars_used = 0
                else:
                    self.log_signal.emit("  Không còn TK!")
                    return None

            except IPFlaggedError:
                result, account_switches = self._handle_ip_flagged(
                    chunk, chunk_chars, account_switches)
                if isinstance(result, (bytes, bytearray)):
                    return result
                if result == "__NO_ACCOUNTS__":
                    return None
                continue

            except (ConnectionError, ConnectionRefusedError,
                    ConnectionResetError, OSError) as e:
                real_retries += 1
                self._consecutive_conn_errors += 1
                err = str(e)[:60]
                self.log_signal.emit(
                    f"  🔌 Connection #{self._consecutive_conn_errors}"
                    f" (retry {real_retries}): {err}")

                if self._consecutive_conn_errors >= 3:
                    # 3 lỗi liên tiếp → rotate IP
                    self.log_signal.emit(
                        "  3 connection lỗi → rotate IP...")
                    try:
                        from accounts.proxy import Proxy4G
                        p = Proxy4G()
                        old_ip = self._ensure_proxy() or ""
                        p.rotate(wait=45)
                        new_ip = self._wait_proxy_ip(previous_ip=old_ip, timeout=60)
                        self.log_signal.emit(f"  IP: {new_ip}")
                        self._wait_tts_route_ready(timeout=60)
                    except Exception:
                        pass
                    self._consecutive_conn_errors = 0
                else:
                    wait = 10 * self._consecutive_conn_errors
                    self.log_signal.emit(
                        f"  �?ợi {wait}s...")
                    time.sleep(wait)

            except Exception as e:
                ename = type(e).__name__
                emsg = str(e)[:80]
                emsg_l = str(e).lower()
                # HET QUOTA (co the tra ve 401) -> DOI TK, khong retry cung TK
                if (
                    getattr(e, 'quota', False)
                    or 'exceeds your quota' in emsg_l
                    or 'exceeds quota' in emsg_l
                    or 'quota_exceeded' in emsg_l
                    or 'het quota' in emsg_l
                ):
                    account_switches += 1
                    failed_email = self._current_email or ""
                    self.log_signal.emit(
                        f"  💰 Hết quota (TK {failed_email}) → đổi TK"
                        f" [{account_switches}]")
                    if failed_email:
                        self._skipped_emails.add(failed_email)
                        if self.mode == "b":
                            try:
                                from core.mode_b_accounts import set_remaining
                                set_remaining(
                                    failed_email, 0,
                                    getattr(self, '_current_reset_unix', 0))
                            except Exception:
                                pass
                    audit("quota_exceeded_401",
                          email=failed_email,
                          chars_used=self._chars_used)
                    if self._added_voices:
                        self._cleanup_added_voices()
                    token, email = self._get_token(need_chars=chunk_chars)
                    if token:
                        self._current_token = token
                        self._current_email = email
                        self._chars_used = 0
                        self.log_signal.emit(f"  Thử TK mới: {email}")
                        continue
                    self.log_signal.emit("  Không còn TK!")
                    return None
                # SUBSCRIPTION BI VO HIEU HOA (ban) -> TK chet: bo qua workspace + doi TK
                if (
                    getattr(e, 'disabled', False)
                    or 'subscription has been disabled' in emsg_l
                    or ('subscription' in emsg_l and 'disabled' in emsg_l)
                ):
                    account_switches += 1
                    failed_email = self._current_email or ""
                    bad_ws = getattr(self, '_current_ws_id', "") or ""
                    self.log_signal.emit(
                        f"  🚫 TK {failed_email} bị vô hiệu hóa (subscription "
                        f"disabled) → bỏ qua, đổi TK [{account_switches}]")
                    # master mode: workspace bi VO HIEU HOA (vinh vien) -> pool loai han
                    if bad_ws:
                        try:
                            self._get_master_pool().mark_disabled_workspace(bad_ws)
                            from core.mode_b_accounts import (
                                set_remaining_by_workspace, mark_dead_by_workspace)
                            mark_dead_by_workspace(bad_ws, "subscription_disabled")
                        except Exception:
                            pass
                    if failed_email:
                        self._skipped_emails.add(failed_email)
                    audit("subscription_disabled",
                          email=failed_email, workspace=bad_ws)
                    if self._added_voices:
                        self._cleanup_added_voices()
                    token, email = self._get_token(need_chars=chunk_chars)
                    if token:
                        self._current_token = token
                        self._current_email = email
                        self._chars_used = 0
                        self.log_signal.emit(f"  Thử TK mới: {email}")
                        continue
                    self.log_signal.emit("  Không còn TK!")
                    return None
                real_retries += 1
                if (
                    'unusual activity' in emsg_l
                    or 'unusual_activity' in emsg_l
                    or 'free tier usage disabled' in emsg_l
                    or 'detected_unusual_activity' in emsg_l
                    or 'tk bi flag:' in emsg_l
                ):
                    self.log_signal.emit(
                        "  ⚠ 401 unusual activity → chuyển sang luồng flag/IP")
                    result, account_switches = self._handle_ip_flagged(
                        chunk, chunk_chars, account_switches)
                    if isinstance(result, (bytes, bytearray)):
                        return result
                    if result == "__NO_ACCOUNTS__":
                        return None
                    continue
                self.log_signal.emit(
                    f"  �?� {ename} (retry {real_retries}): {emsg}")

                # API timeout
                if 'timeout' in emsg.lower() or \
                   'Timeout' in ename:
                    wait = 15 + real_retries * 5
                    self.log_signal.emit(
                        f"  Timeout, đợi {wait}s...")
                    time.sleep(wait)
                # text_too_long → chunk quá dài
                elif 'text_too_long' in emsg:
                    self.log_signal.emit(
                        f"  Text quá dài ({chunk_chars:,} chars)"
                        f" → tự động chia nh�?")
                    return 'SPLIT'
                # Rate limit 429
                elif '429' in emsg:
                    wait = 30 + real_retries * 10
                    self.log_signal.emit(
                        f"  Rate limit, đợi {wait}s...")
                    time.sleep(wait)
                # Server error 500
                elif '500' in emsg or '502' in emsg \
                        or '503' in emsg:
                    wait = 10 + real_retries * 5
                    self.log_signal.emit(
                        f"  Server error, đợi {wait}s...")
                    time.sleep(wait)
                else:
                    time.sleep(5 + random.uniform(3, 8))

        # Hết retry
        self.log_signal.emit(
            f"  ⚠ Chunk {chunk_idx+1} thất bại sau "
            f"{real_retries} retries, {account_switches} đổi TK")
        return None

    def _should_switch_account(self, email, chunk_chars,
                               chars_used):
        """Kiểm tra TK hiện tại có đủ credit cho chunk tiếp không."""
        if not self._current_token:
            return True

        # Mode B: dùng quota thật (lưu từ check_quota)
        if self.mode == "b":
            tk_quota = getattr(self, '_tk_quota', 10000)
            remaining = tk_quota - self._chars_used
            return remaining < chunk_chars * getattr(
                self, '_credit_multiplier', 1)

        # Mode A: query API
        try:
            proxy_dict = self._get_proxy()
            quota = check_quota(self._current_token,
                                proxy=proxy_dict)
            if quota:
                remaining = quota["chars_remaining"]
                if remaining < chunk_chars:
                    self.log_signal.emit(
                        f"  TK {email}: còn {remaining:,}"
                        f" < cần {chunk_chars:,} → đổi TK")
                    try:
                        from accounts.bridge import _get_db
                        db = _get_db()
                        db.set_service(email, "elevenlabs",
                                       credits_used=quota[
                                           "chars_used"])
                    except Exception:
                        pass
                    return True
                return False
        except Exception:
            pass
        return False

    def _master_workspace_enabled(self):
        """True neu co it nhat 1 master (masters.json hoac master_account.json)."""
        try:
            from core.masters_store import count_active
            return count_active() > 0
        except Exception:
            return False

    def _get_master_pool(self):
        # Dung POOL CHUNG (build 1 lan, tai dung cho moi batch) -> khong build lai
        # ~2 phut/channel nhu truoc. Token + quota cache trong pool.
        if not getattr(self, "_mpool", None):
            from core.master_pool import get_shared_pool
            self._mpool = get_shared_pool()
        return self._mpool

    def _get_token(self, need_chars=1000):
        """Lấy token theo mode. Ch�?n TK có đủ credit cho need_chars."""
        # Tính credit thực tế cần (v3 = 2x)
        mult = getattr(self, '_credit_multiplier', 1)
        need_chars = need_chars * mult

        # ── MASTER WORKSPACE MODE ──
        # Generate bang token master + sign-into-workspace cua worker.
        # Khong bi detected_unusual_activity, tieu quota cua worker.
        if self._master_workspace_enabled():
            try:
                pool = self._get_master_pool()
                # Onboard (login+invite+accept) lam o nut "Lien ket Master".
                # O day chon workspace con quota tu BAT KY master nao -> generate.
                pick = pool.next_workspace(need_chars=need_chars)
                if not pick:
                    self.log_signal.emit(
                        "  Master: hết workspace còn quota! "
                        "(bấm 'Liên kết Master' để thêm account)")
                    return None, None
                m_email, ws_id, scoped_token, remaining = pick
                self._current_api_key = ""
                self._current_password = ""
                self._current_auth_data = None
                self._current_ws_id = ws_id
                self._token_time = time.time()   # tranh refresh nham moi chunk
                # ĐỒNG BỘ quota TK voi pool + reset dem -> logic tai-dung-token (need_new)
                # tinh dung, khong tai dung TK sat nguong -> het "bao con nhung het quota".
                self._tk_quota = remaining
                self._chars_used = 0
                # Ghi quota THAT ve roster (theo workspace_id) -> tab Accounts hien dung
                try:
                    from core.mode_b_accounts import set_remaining_by_workspace
                    set_remaining_by_workspace(ws_id, remaining)
                except Exception:
                    pass
                self.log_signal.emit(
                    f"  ✓ Master WS {ws_id[:12]} (còn {remaining:,} chars)")
                return scoped_token, f"ws:{ws_id[:8]}"
            except Exception as e:
                self.log_signal.emit(
                    f"  Master mode error: {str(e)[:80]}")
                return None, None

        import subprocess
        subprocess.run(['powershell', 'Stop-Process', '-Name', 'chrome',
                        '-Force', '-ErrorAction', 'SilentlyContinue'],
                       capture_output=True)
        time.sleep(2)

        if self.mode == "a":
            # Mode A: lấy từ Account Manager
            try:
                from accounts.bridge import get_ready_account, get_token
                tk = get_ready_account("elevenlabs",
                                       need_chars=need_chars)
                if not tk or tk['email'] in self._skipped_emails:
                    # Thử fallback: TK nào còn credit bất kỳ
                    from accounts.bridge import get_ready_accounts
                    all_ready = get_ready_accounts("elevenlabs",
                                                   min_chars=1)
                    available = [a for a in all_ready
                                 if a['email'] not in
                                 self._skipped_emails]
                    if available:
                        tk = available[0]
                    else:
                        self.log_signal.emit(
                            f"  Không còn TK nào đủ credit!")
                        return None, None
                self.log_signal.emit(
                    f"  Ch�?n TK: {tk['email']} "
                    f"(DB: còn {tk['credits_remaining']:,} chars)")
                result = get_token(tk["email"])
                subprocess.run(['powershell', 'Stop-Process', '-Name', 'chrome',
                                '-Force', '-ErrorAction', 'SilentlyContinue'],
                               capture_output=True)
                time.sleep(2)
                if result:
                    token = result["token"]
                    email = tk["email"]
                    # Check quota thật từ API
                    proxy_dict = self._get_proxy()
                    quota = check_quota(token, proxy=proxy_dict)
                    if quota:
                        real_remaining = quota["chars_remaining"]
                        self.log_signal.emit(
                            f"  API: còn {real_remaining:,}/"
                            f"{quota['chars_limit']:,} chars")
                        # Sync DB
                        try:
                            from accounts.bridge import _get_db
                            db = _get_db()
                            db.set_service(email, "elevenlabs",
                                           credits_used=quota["chars_used"])
                        except Exception:
                            pass
                        # Nếu hết quota thật → b�? qua TK này, lấy TK khác
                        if real_remaining < need_chars:
                            self.log_signal.emit(
                                f"  TK {email} chỉ còn "
                                f"{real_remaining:,} chars < "
                                f"cần {need_chars:,}, thử TK khác...")
                            self._skipped_emails.add(email)
                            return self._get_token(need_chars)
                    self._current_api_key = ""
                    self._current_password = ""
                    self._current_auth_data = None
                    return token, email
            except RecursionError:
                self.log_signal.emit(
                    "  Không còn TK nào đủ credit!")
                return None, None
            except Exception as e:
                self.log_signal.emit(f"  Token error: {str(e)[:40]}")
            return None, None

        else:
            # ── Mode B: 1 TK = 1 IP (đơn giản) ──
            try:
                # Init danh sách TK
                if not hasattr(self, '_mode_b_accounts'):
                    self._mode_b_accounts = []
                    self._mode_b_idx = 0
                    self._current_reset_unix = 0
                    from core.mode_b_accounts import (
                        get_alive_accounts)
                    self._mode_b_accounts = get_alive_accounts(
                        min_chars=500)
                    self._reorder_mode_b_accounts_for_default_api()
                    self._reorder_mode_b_accounts_for_browser()
                    self.log_signal.emit(
                        f"  Mode B: {len(self._mode_b_accounts)}"
                        f" TK sẵn sàng")

                while self._mode_b_idx < len(self._mode_b_accounts):
                    acc = self._mode_b_accounts[self._mode_b_idx]
                    self._mode_b_idx += 1
                    email = acc["email"]

                    if email in self._skipped_emails:
                        continue

                    # ① CHECK QUOTA trước (không cần IP)
                    api_key = acc.get("api_key", "")
                    remaining = acc.get("chars_remaining", 0)  # default
                    auth_result = None  # default, tránh NameError
                    if api_key:
                        quota = check_quota(api_key, proxy=None)
                        if quota:
                            remaining = quota["chars_remaining"]
                            self._current_reset_unix = quota.get(
                                "next_reset_unix", 0)
                            self._tk_quota = remaining
                            # Update JSON
                            try:
                                from core.mode_b_accounts import (
                                    set_remaining)
                                set_remaining(
                                    email, remaining,
                                    self._current_reset_unix)
                            except Exception:
                                pass
                            # Update in-memory
                            acc["chars_remaining"] = remaining
                            
                            if remaining < need_chars:
                                self.log_signal.emit(
                                    f"  �?� {email}: "
                                    f"còn {remaining:,} < "
                                    f"cần {need_chars:,} → skip")
                                self._skipped_emails.add(email)
                                
                                # Re-sort: đẩy TK ít credit xuống cuối
                                # Chỉ sort phần chưa duyệt
                                remaining_list = \
                                    self._mode_b_accounts[
                                        self._mode_b_idx:]
                                remaining_list.sort(
                                    key=lambda a: -a.get(
                                        "chars_remaining", 0))
                                self._mode_b_accounts[
                                    self._mode_b_idx:] = \
                                    remaining_list
                                continue
                            self.log_signal.emit(
                                f"  ✓ {email}: "
                                f"còn {remaining:,} chars")
                        else:
                            # Không check được quota → skip TK
                            self.log_signal.emit(
                                f"  �?� {email}: "
                                f"không check được quota → skip")
                            continue

                    # ② Rule dung: moi tai khoan = 1 IP.
                    # Cung tai khoan thi giu IP cho cac chunk tiep theo.
                    # Sang tai khoan khac thi doi IP truoc.
                    if self._is_library_voice():
                        self.log_signal.emit(f"  TK: {email} → đổi IP...")
                        try:
                            from accounts.proxy import Proxy4G
                            p = Proxy4G()
                            old_ip = self._ensure_proxy() or ""
                            p.rotate(wait=25)
                            new_ip = self._wait_proxy_ip(
                                previous_ip=old_ip, timeout=60)
                            self.log_signal.emit(f"  IP: {new_ip}")
                            time.sleep(8)
                            self._wait_tts_route_ready(timeout=25, quiet=True)
                        except Exception as e:
                            self.log_signal.emit(
                                f"  Rotate lỗi: {str(e)[:40]}")
                    else:
                        prev_email = (getattr(self, "_current_email", "") or "").strip().lower()
                        next_email = (email or "").strip().lower()
                        if prev_email != next_email:
                            self.log_signal.emit(f"  TK: {email} → đổi IP...")
                            try:
                                from accounts.proxy import Proxy4G
                                p = Proxy4G()
                                old_ip = self._ensure_proxy() or ""
                                p.rotate(wait=25)
                                new_ip = self._wait_proxy_ip(
                                    previous_ip=old_ip, timeout=60)
                                self.log_signal.emit(f"  IP: {new_ip}")
                                time.sleep(8)
                            except Exception as e:
                                self.log_signal.emit(
                                    f"  Rotate lỗi: {str(e)[:40]}")
                        else:
                            current_ip = self._ensure_proxy() or ""
                            self.log_signal.emit(
                                f"  TK: {email} | giữ IP hiện tại: {current_ip}")
                    proxy_dict = self._get_proxy()

                    # ③ AUTH (Firebase / API key)
                    use_firebase = self._is_library_voice(api_key)
                    if use_firebase:
                        from core.api_client import firebase_login
                        from accounts.proxy import Proxy4G as _P4G
                        _fb = _P4G()
                        fb_proxy = (
                            _fb.get_for_firebase()
                            if hasattr(_fb, "get_for_firebase")
                            else _fb.get_for_requests()
                        )
                        token = None
                        auth_result = None
                        last_fb_msg = ""
                        for fb_try in range(2):
                            try:
                                result = firebase_login(
                                    email, acc["password"],
                                    proxy=fb_proxy)
                                token = result["idToken"]
                                auth_result = result
                                break
                            except Exception as fb_err:
                                fb_msg = str(fb_err)
                                last_fb_msg = fb_msg
                                self.log_signal.emit(
                                    f"  ⚠ {email}: "
                                    f"Firebase err ({fb_msg[:60]})")
                                if 'INVALID_LOGIN' in fb_msg \
                                   or 'EMAIL_NOT_FOUND' in fb_msg \
                                   or 'INVALID_LOGIN_CREDENTIALS' in fb_msg:
                                    self.log_signal.emit(
                                        f"  ⚠ {email}: "
                                        f"Firebase FAIL ({fb_msg[:30]})")
                                    try:
                                        from core.mode_b_accounts import mark_dead
                                        mark_dead(email, fb_msg)
                                    except Exception:
                                        pass
                                    self._skipped_emails.add(email)
                                    break
                                if 'QUOTA_EXCEEDED' in fb_msg:
                                    self.log_signal.emit(
                                        f"  �?� {email}: Firebase quota exceeded")
                                    break
                                if fb_try == 0:
                                    time.sleep(3)
                        if not token:
                            self.log_signal.emit(
                                f"  �?� {email}: Firebase login fail "
                                f"({last_fb_msg[:50]})")
                            self._skipped_emails.add(email)
                            continue
                    else:
                        token = api_key

                    # ④ READY
                    self._current_api_key = api_key
                    self._current_password = acc.get("password", "")
                    self._current_auth_data = auth_result if use_firebase else None  # auth_result luôn được init ở trên
                    self._token_time = time.time()
                    self.log_signal.emit(
                        f"  ✓ TK: {email} "
                        f"({remaining:,} chars)")
                    return token, email

                self.log_signal.emit(
                    "  Không còn TK Mode B!")
                return None, None

            except Exception as e:
                self.log_signal.emit(
                    f"  Mode B error: {str(e)[:60]}")
                return None, None

    def _probe_default_tts_ready(self, token: str, email: str) -> bool:
        """Run a tiny real TTS probe before spending a big chunk."""
        if self._is_library_voice():
            return True
        try:
            from core.convert import _convert_text_direct_api, QuotaExceededError, IPFlaggedError
        except Exception:
            return True

        proxy = self._get_proxy()
        last_err = ""
        for attempt in range(2):
            try:
                _convert_text_direct_api(
                    token, self.voice_id, "ok",
                    self.model_id, self.output_format,
                    self.stability, self.similarity, self.speed,
                    proxy, self.language_code,
                )
                self.log_signal.emit(f"  Probe TTS OK: {email}")
                return True
            except QuotaExceededError:
                self.log_signal.emit(f"  Probe quota fail: {email}")
                return False
            except IPFlaggedError as e:
                last_err = str(e)
            except Exception as e:
                last_err = str(e)[:80]
            if attempt == 0:
                self.log_signal.emit(f"  Probe TTS retry: {email}")
                time.sleep(8)
        self.log_signal.emit(f"  Probe TTS fail: {email} | {last_err}")
        return False

    def _get_proxy(self):
        # Master-workspace mode: di thang, KHONG can 4G/proxy (khong bi flag)
        if self._master_workspace_enabled():
            return None
        try:
            from accounts.proxy import Proxy4G as Proxy
            proxy = Proxy().get_for_requests()
            try:
                https_proxy = (proxy or {}).get("https", "")
                if https_proxy:
                    self.log_signal.emit(f"  Proxy route: {https_proxy}")
            except Exception:
                pass
            return proxy
        except Exception:
            return {}

    def _server_test_proxy(self, device_id: str) -> str:
        return ""

    def _ensure_proxy(self):
        """Verify proxy status from 4G server first, without auto-rotate."""
        from accounts.proxy import Proxy4G
        p = Proxy4G()
        try:
            info = p.get_info()
            proxies = info.get("proxies", info.get("devices", []))
            if proxies:
                first = proxies[0]
                if first.get("proxy_running"):
                    ip = (first.get("current_4g_ip") or "").strip() or p.get_ip()
                    if ip:
                        self.log_signal.emit(f"  ? Proxy OK (IP: {ip})")
                        audit("proxy_ok", ip=ip, source="server")
                        return ip
        except Exception:
            pass
        try:
            ip = p.get_ip()
            if ip:
                self.log_signal.emit(f"  ? Proxy OK (IP: {ip})")
                audit("proxy_ok", ip=ip, source="fallback")
                return ip
        except Exception:
            pass
        self.log_signal.emit("  ? Proxy khong phan hoi")
        return None

    def _wait_proxy_ip(self, previous_ip: str = "", timeout: int = 30):
        from accounts.proxy import Proxy4G, API_BASE, API_KEY
        import requests as _req

        previous_ip = (previous_ip or "").strip()
        p = Proxy4G()
        deadline = time.time() + max(5, timeout)

        while time.time() < deadline:
            try:
                r = _req.get(f"{API_BASE}/list?key={API_KEY}", timeout=10)
                if r.status_code == 200:
                    proxies_info = r.json().get("proxies", [])
                    if proxies_info:
                        device_id = proxies_info[0].get("id", "")
                        test_ip = self._server_test_proxy(device_id)
                        if test_ip and test_ip != previous_ip:
                            return test_ip
                        server_ip = (proxies_info[0].get("current_4g_ip") or "").strip()
                        if server_ip and server_ip != previous_ip:
                            return server_ip
            except Exception:
                pass

            try:
                ip = (p.get_ip() or "").strip()
                if ip and ip != previous_ip:
                    return ip
            except Exception:
                pass
            time.sleep(2)
        return ""

    def _wait_tts_route_ready(self, timeout: int = 25, quiet: bool = False) -> bool:
        import requests as _req

        proxy = self._get_proxy()
        deadline = time.time() + max(5, timeout)
        last_err = ""

        while time.time() < deadline:
            try:
                resp = _req.get(
                    "https://api.us.elevenlabs.io/v1/user/subscription",
                    proxies=proxy,
                    timeout=12,
                    headers={"accept": "*/*"},
                )
                if resp.status_code in (200, 401):
                    return True
                last_err = f"http_{resp.status_code}"
            except Exception as exc:
                last_err = str(exc)[:80]
            time.sleep(2)

        if not quiet:
            self.log_signal.emit(f"  Route TTS chua on dinh: {last_err}")
        return False

    def _need_new_token(self):
        return False  # TODO: check token expiry

    def _is_library_voice(self, api_key: str = ""):
        """Check xem voice_id có phải library voice không.
        Default voices dùng được API Key, library voices cần Firebase.
        """
        # ElevenLabs default voice IDs (built-in)
        DEFAULT_VOICE_IDS = {
            "JBFqnCBsd6RMkjVDRZzb",  # George
            "EXAVITQu4vr4xnSDxMaL",  # Sarah
            "IKne3meq5aSn9XLyUdCD",  # Charlie
            "XB0fDUnXU5powFXDhCwa",  # Charlotte
            "pFZP5JQG7iQjIQuC4Bku",  # Lily
            "TX3LPaxmHKxFdv7VOQHJ",  # Liam
            "bIHbv24MWmeRgasZH58o",  # Will
            "nPczCjzI2devNBz1zQrb",  # Brian
            "SAz9YHcvj6GT2YYXdXww",  # River
            "Xb7hH8MSUJpSbSDYk0k2",  # Alice
            "onwK4e9ZLuTAKqWW03F9",  # Daniel
            "cgSgspJ2msm6clMCkdW9",  # Jessica
            "iP95p4xoKVk53GoZ742B",  # Chris
            "N2lVS1w4EtoT3dr4eOWO",  # Callum
            "cjVigY5qzO86Huf0OWal",  # Eric
            "FGY2WhTYpPnrIDTdsKH5",  # Laura
        }
        voice_id = (self.voice_id or "").strip()
        if not voice_id:
            return True
        known = set(DEFAULT_VOICE_IDS) | set(getattr(self, "_known_premade_voice_ids", set()))
        if voice_id in known:
            return False
        if api_key and not getattr(self, "_voice_category_checked", False):
            try:
                import requests as _req
                resp = _req.get(
                    "https://api.us.elevenlabs.io/v1/voices",
                    headers={"xi-api-key": api_key, "accept": "application/json"},
                    proxies=self._get_proxy(),
                    timeout=20,
                )
                if resp.status_code == 200:
                    for row in resp.json().get("voices", []):
                        if (row.get("category") or "").strip().lower() == "premade":
                            vid = (row.get("voice_id") or "").strip()
                            if vid:
                                known.add(vid)
            except Exception:
                pass
            self._voice_category_checked = True
            self._known_premade_voice_ids = known
        return voice_id not in known


# ============================================================
# MAIN WINDOW
# ============================================================

class UpdateWorker(QThread):
    """Tai code moi tu GitHub (chay nen, khong treo GUI)."""
    log_signal = pyqtSignal(str)
    done = pyqtSignal(bool, str)   # ok, msg

    def run(self):
        try:
            from core.updater import update_from_github
            ok, msg, n = update_from_github(
                on_log=lambda m: self.log_signal.emit(m))
            self.done.emit(ok, msg)
        except Exception as e:
            self.done.emit(False, f"loi: {str(e)[:100]}")


class StartupRecoverWorker(QThread):
    """Luc mo tool: tu dong login lai master HET HAN — nhung CHI khi THIEU master song.

    QUAN TRONG: login master mo Chrome + 2FA rat NANG (~2 phut/master) va tranh
    mang/proxy voi Auto Convert -> lam CHAM generate. Vi vay:
      - Chi chay khi so master SONG < MIN (mac dinh 2) -> luc that su can them master.
      - Con du master song thi KHONG chay (de Auto Convert chay nhanh). Nguoi dung
        chu dong bam '🔧 Tu dong login lai' khi tool ranh de nap them master moi.
    """
    log_signal = pyqtSignal(str)
    done = pyqtSignal(dict)
    MIN_LIVE = 2

    def run(self):
        try:
            from core.masters_store import list_masters, count_active
            from core.master_login import _read_master_creds, recover_expired_masters
            # Du master song -> KHONG khoi phuc nen (tranh lam cham Auto Convert)
            if count_active() >= self.MIN_LIVE:
                return
            targets = [m.get("email") for m in list_masters()
                       if (m.get("status") or "active") == "expired"
                       and _read_master_creds(m.get("email"))[0]]
            if not targets:
                return
            self.log_signal.emit(
                f"[Auto-recover] Thieu master song -> login lai {len(targets)} master nen...")
            res = recover_expired_masters(
                on_log=lambda m: self.log_signal.emit("[Auto-recover] " + m))
            self.done.emit(res)
        except Exception as e:
            self.log_signal.emit(f"[Auto-recover] loi: {str(e)[:100]}")


class MaintenanceWorker(QThread):
    """Bao tri nen dinh ky (24/7): quet quota + luu ngay reset + canh bao can nguon
    + (tuy chon) tu lien ket master. Doc-only voi ElevenLabs, khong ton credit."""
    log_signal = pyqtSignal(str)
    done = pyqtSignal(dict)

    def __init__(self, do_relink=False):
        super().__init__()
        self.do_relink = do_relink
        self._stop = False

    def cancel(self):
        self._stop = True

    def run(self):
        try:
            from core.maintenance import run_maintenance
            res = run_maintenance(
                on_log=lambda m: self.log_signal.emit(m),
                should_stop=lambda: self._stop,
                do_relink=self.do_relink)
            self.done.emit(res or {})
        except Exception as e:
            self.log_signal.emit(f"[Maintenance] loi: {str(e)[:100]}")


class PoolWarmerWorker(QThread):
    """CHUAN BI TRUOC token: nap san pool workspace o NEN -> khi Auto Convert can
    token la co NGAY, khong phai doi build pool (~50s) giua chung."""
    log_signal = pyqtSignal(str)

    def __init__(self, target=40):
        super().__init__()
        self.target = target

    def run(self):
        try:
            from core.master_pool import get_shared_pool
            n = get_shared_pool().warm(target_ready=self.target)
            self.log_signal.emit(f"[Pool] San sang {n} token (nap truoc)")
        except Exception as e:
            self.log_signal.emit(f"[Pool] warm loi: {str(e)[:80]}")


class VoiceTool(QMainWindow):

    def __init__(self):
        super().__init__()
        self.config = Config()
        self.worker = None
        self.language_worker = None
        self._last_language_voice_id = ""
        self._file_list = []
        self._elapsed_timer = QElapsedTimer()
        self._timer = QTimer()
        self._timer.timeout.connect(self._update_elapsed)

        self._init_ui()
        self._update_stats()

    def _init_ui(self):
        self.setWindowTitle("11Lab Voice Tool")
        self.setMinimumSize(960, 700)
        self.resize(1000, 750)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(4, 4, 4, 4)

        # === TAB WIDGET ===
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        # --- Tab 1: Voice Convert ---
        voice_tab = QWidget()
        layout = QVBoxLayout(voice_tab)
        layout.setContentsMargins(8, 8, 8, 6)
        layout.setSpacing(4)

        # === ROW 1: Voice + Settings + Mode ===
        row1 = QHBoxLayout()

        # -- Voice --
        vg = QGroupBox("Voice")
        vl = QGridLayout(vg)
        vl.setContentsMargins(6, 18, 6, 6)

        vl.addWidget(QLabel("Voice ID:"), 0, 0)
        self.voice_input = QLineEdit()
        self.voice_input.setPlaceholderText("Nhap Voice ID (vd: RGb96Dcl0k5eVje8EBch)")
        self.voice_input.editingFinished.connect(self._load_voice_languages)
        self.voice_input.returnPressed.connect(self._load_voice_languages)
        vl.addWidget(self.voice_input, 0, 1, 1, 2)

        vl.addWidget(QLabel("Model:"), 1, 0)
        self.model_combo = QComboBox()
        for m in AVAILABLE_MODELS:
            self.model_combo.addItem(m)
        self.model_combo.setCurrentText(self.config.get("default_model", "eleven_v3"))
        vl.addWidget(self.model_combo, 1, 1, 1, 2)

        vl.addWidget(QLabel("Language:"), 2, 0)
        self.language_combo = QComboBox()
        self._populate_language_combo([])
        vl.addWidget(self.language_combo, 2, 1, 1, 2)

        row1.addWidget(vg)

        # -- Settings --
        sg = QGroupBox("Settings")
        sl = QGridLayout(sg)
        sl.setContentsMargins(6, 18, 6, 6)

        sl.addWidget(QLabel("Stability:"), 0, 0)
        self.stability_spin = QSpinBox()
        self.stability_spin.setRange(0, 100)
        self.stability_spin.setValue(
            int(float(self.config.get("voice_stability", 0.5)) * 100))
        self.stability_spin.setSuffix("%")
        self.stability_spin.setFixedWidth(60)
        sl.addWidget(self.stability_spin, 0, 1)

        sl.addWidget(QLabel("Similarity:"), 0, 2)
        self.similarity_spin = QSpinBox()
        self.similarity_spin.setRange(0, 100)
        self.similarity_spin.setValue(
            int(float(self.config.get("voice_similarity_boost", 0.8)) * 100))
        self.similarity_spin.setSuffix("%")
        self.similarity_spin.setFixedWidth(60)
        sl.addWidget(self.similarity_spin, 0, 3)

        sl.addWidget(QLabel("Speed:"), 1, 0)
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.7, 1.2)
        self.speed_spin.setSingleStep(0.05)
        self.speed_spin.setValue(1.0)
        self.speed_spin.setFixedWidth(60)
        sl.addWidget(self.speed_spin, 1, 1)

        sl.addWidget(QLabel("Quality:"), 1, 2)
        self.quality_combo = QComboBox()
        for fmt in OUTPUT_FORMATS:
            self.quality_combo.addItem(fmt)
        self.quality_combo.setCurrentText(
            self.config.get("output_format", "mp3_44100_128"))
        sl.addWidget(self.quality_combo, 1, 3)

        row1.addWidget(sg)

        # -- Mode --
        mg = QGroupBox("Mode")
        ml = QVBoxLayout(mg)
        ml.setContentsMargins(6, 18, 6, 6)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Mode A - Browser (auto accounts)", "a")
        self.mode_combo.addItem("Mode B - API (Firebase login)", "b")
        self.mode_combo.setCurrentIndex(1)  # Default Mode B
        self.mode_combo.currentIndexChanged.connect(self._on_mode_change)
        ml.addWidget(self.mode_combo)

        self.lbl_mode_info = QLabel("Ready accounts: 0 | Credit: 0")
        self.lbl_mode_info.setStyleSheet("font-size:11px; color:#666;")
        ml.addWidget(self.lbl_mode_info)

        btn_accounts = QPushButton("Manage Accounts")
        btn_accounts.setStyleSheet("font-size:11px;")
        btn_accounts.clicked.connect(self._open_accounts)
        ml.addWidget(btn_accounts)

        row1.addWidget(mg)
        layout.addLayout(row1)

        # === ROW 2: Input/Output ===
        io_row = QGridLayout()

        io_row.addWidget(QLabel("Input:"), 0, 0)
        self.folder_input = QLineEdit()
        self.folder_input.setPlaceholderText("Chon thu muc chua file .txt")
        io_row.addWidget(self.folder_input, 0, 1)
        btn_browse = QPushButton("Browse")
        btn_browse.setFixedWidth(50)
        btn_browse.clicked.connect(self._browse_input)
        io_row.addWidget(btn_browse, 0, 2)

        io_row.addWidget(QLabel("Output:"), 1, 0)
        self.output_input = QLineEdit(DEFAULT_OUTPUT_DIR)
        io_row.addWidget(self.output_input, 1, 1)
        btn_out = QPushButton("Browse")
        btn_out.setFixedWidth(50)
        btn_out.clicked.connect(self._browse_output)
        io_row.addWidget(btn_out, 1, 2)

        layout.addLayout(io_row)

        # === ROW 3: Actions ===
        actions = QHBoxLayout()

        self.btn_start = QPushButton("START")
        self.btn_start.setStyleSheet(
            "font-weight:bold; font-size:14px; color:white; "
            "background:#27ae60; padding:8px 24px; border-radius:4px;")
        self.btn_start.clicked.connect(self._start_batch)
        actions.addWidget(self.btn_start)

        self.btn_stop = QPushButton("STOP")
        self.btn_stop.setStyleSheet("color:red; font-weight:bold; padding:8px;")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_batch)
        actions.addWidget(self.btn_stop)

        actions.addStretch()

        btn_add_files = QPushButton("Add Files")
        btn_add_files.clicked.connect(self._add_files)
        actions.addWidget(btn_add_files)

        btn_clear = QPushButton("Clear List")
        btn_clear.clicked.connect(self._clear_list)
        actions.addWidget(btn_clear)

        layout.addLayout(actions)

        # === ROW 4: File table ===
        self.file_table = QTableWidget()
        self.file_table.setColumnCount(4)
        self.file_table.setHorizontalHeaderLabels(
            ["File", "Status", "Size", "Progress"])
        h = self.file_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 4):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.file_table.setAlternatingRowColors(True)
        layout.addWidget(self.file_table, 1)

        # === ROW 5: Log ===
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMaximumHeight(100)
        self.log_area.setStyleSheet(
            "font-family:Consolas; font-size:10px; background:#1e1e1e; color:#ddd;")
        layout.addWidget(self.log_area)

        # === ROW 6: Status ===
        status = QHBoxLayout()
        self.lbl_status = QLabel("Ready")
        self.lbl_status.setStyleSheet("font-size:11px;")
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(16)
        self.lbl_mode_status = QLabel("")
        self.lbl_mode_status.setStyleSheet("font-size:11px; color:#666;")
        status.addWidget(self.lbl_status, 1)
        status.addWidget(self.progress_bar, 1)
        status.addWidget(self.lbl_mode_status)
        layout.addLayout(status)

        # --- Tab 0: Auto Convert (LIÊN TỤC) ---
        from ui.auto_tab import AutoTab
        self.auto_tab = AutoTab()
        self.tabs.addTab(self.auto_tab, "Auto Convert")

        self.tabs.addTab(voice_tab, "Voice Convert")

        # --- Tab 2: Quản lý TK Mode B ---
        from ui.accounts_tab import AccountsTab
        self.accounts_tab = AccountsTab()
        self.tabs.addTab(self.accounts_tab, "Accounts")

        # --- Tab 3: 4G Proxy ---
        from ui.proxy_tab import ProxyTab
        self.proxy_tab = ProxyTab()
        self.tabs.addTab(self.proxy_tab, "4G Proxy")

        # --- Nut UPDATE (goc phai tab bar): tai code moi tu GitHub ---
        self.btn_update = QPushButton("⬆ Update")
        self.btn_update.setToolTip(
            "Tai code moi nhat tu GitHub. GIU NGUYEN config (TK/master) + log.\n"
            "Cap nhat xong can tat & mo lai tool.")
        self.btn_update.setStyleSheet(
            "font-weight:bold; background:#16a085; color:white; "
            "padding:4px 14px; border-radius:4px;")
        self.btn_update.clicked.connect(self._start_update)
        self.tabs.setCornerWidget(self.btn_update, Qt.TopRightCorner)
        self._update_worker = None

        # Tu dong chay Auto Convert sau N giay khi mo tool (mac dinh 60s).
        # Tat: dat "auto_start_delay_sec": 0 trong config/settings.json.
        try:
            self._auto_start_timer = QTimer(self)
            self._auto_start_timer.setSingleShot(True)
            self._auto_start_timer.timeout.connect(self._auto_start_auto_convert)
            delay = int(self.config.get("auto_start_delay_sec", 60))
            if delay > 0:
                self._auto_start_timer.start(delay * 1000)
        except Exception:
            pass

    def _start_update(self):
        """Tai code moi tu GitHub (giu config + log)."""
        if self._update_worker and self._update_worker.isRunning():
            return
        reply = QMessageBox.question(
            self, "Update tool",
            "Tải code mới nhất từ GitHub?\n\n"
            "• GIỮ NGUYÊN tài khoản, master, settings, log của máy này.\n"
            "• Chỉ cập nhật code (ui/core/accounts/...).\n"
            "• Xong cần TẮT & MỞ LẠI tool.\n\n"
            "Nên DỪNG Auto Convert trước khi update.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        self.btn_update.setEnabled(False)
        self.btn_update.setText("⏳ Đang update...")
        self._update_worker = UpdateWorker()
        self._update_worker.log_signal.connect(
            lambda m: self.btn_update.setToolTip(m))
        self._update_worker.done.connect(self._on_update_done)
        self._update_worker.start()

    def _on_update_done(self, ok, msg):
        self.btn_update.setEnabled(True)
        self.btn_update.setText("⬆ Update")
        if ok:
            QMessageBox.information(
                self, "Update xong",
                f"✅ {msg}\n\nHÃY TẮT & MỞ LẠI tool để dùng code mới.\n"
                "(Nếu requirements.txt đổi thì chạy lại setup.bat.)")
        else:
            QMessageBox.warning(self, "Update lỗi", f"❌ {msg}")

    def _auto_start_auto_convert(self):
        """Tu dong bam BAT DAU AUTO o tab Auto Convert (sau delay khi mo tool)."""
        try:
            if not hasattr(self, "auto_tab"):
                return
            w = getattr(self.auto_tab, "auto_worker", None)
            if w and w.isRunning():
                return  # dang chay roi
            self.tabs.setCurrentWidget(self.auto_tab)
            try:
                self.auto_tab._log("⏱ Tự động chạy Auto Convert (sau khi mở tool)")
            except Exception:
                pass
            self.auto_tab._start_auto()
        except Exception:
            pass

    # ============================================================
    # ACTIONS
    # ============================================================

    def _populate_language_combo(self, languages=None, loading=False):
        self.language_combo.clear()
        if loading:
            self.language_combo.addItem("Loading...", None)
            self.language_combo.setEnabled(False)
            return
        self.language_combo.setEnabled(True)
        self.language_combo.addItem("Auto", None)
        items = languages or [
            {"code": code, "name": LANGUAGE_NAMES.get(code, code)}
            for code in POPULAR_LANGUAGES
        ]
        seen = set()
        for item in items:
            code = (item.get("code") or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            name = item.get("name") or LANGUAGE_NAMES.get(code, code)
            self.language_combo.addItem(f"{code} - {name}", code)

    def _load_voice_languages(self):
        voice_id = self.voice_input.text().strip()
        if not voice_id:
            self._last_language_voice_id = ""
            self._populate_language_combo([])
            return
        if voice_id == self._last_language_voice_id:
            return
        self._last_language_voice_id = voice_id
        if self.language_worker and self.language_worker.isRunning():
            self.language_worker.quit()
            self.language_worker.wait(100)
        self._populate_language_combo(loading=True)
        self._log(f"Dang lay ngon ngu ho tro cho voice {voice_id[:16]}...")
        self.language_worker = VoiceLanguagesWorker(voice_id)
        self.language_worker.done.connect(self._on_voice_languages_loaded)
        self.language_worker.error.connect(self._on_voice_languages_error)
        self.language_worker.start()

    def _on_voice_languages_loaded(self, voice_id, languages):
        if voice_id != self.voice_input.text().strip():
            return
        if languages:
            self._populate_language_combo(languages)
            codes = ", ".join(item["code"] for item in languages)
            self._log(f"Voice {voice_id[:16]} ho tro ngon ngu: {codes}")
        else:
            self._populate_language_combo([])
            self._log("Voice khong tra danh sach ngon ngu rieng, dung danh sach pho bien")

    def _on_voice_languages_error(self, voice_id, msg):
        if voice_id != self.voice_input.text().strip():
            return
        self._populate_language_combo([])
        self._log(f"Khong lay duoc ngon ngu voice: {msg}")

    def _browse_input(self):
        folder = QFileDialog.getExistingDirectory(self, "Select TXT folder")
        if folder:
            self.folder_input.setText(folder)
            self._load_folder(folder)

    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output folder")
        if folder:
            self.output_input.setText(folder)

    def _add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select TXT files", "", "Text files (*.txt)")
        for f in files:
            self._add_file_to_list(f)

    def _load_folder(self, folder):
        self._file_list.clear()
        self.file_table.setRowCount(0)
        txt_files = sorted([
            os.path.join(folder, f) for f in os.listdir(folder)
            if f.endswith('.txt')])
        for f in txt_files:
            self._add_file_to_list(f)

    def _add_file_to_list(self, filepath):
        row = self.file_table.rowCount()
        self.file_table.insertRow(row)
        self.file_table.setItem(row, 0, QTableWidgetItem(os.path.basename(filepath)))
        self.file_table.setItem(row, 1, QTableWidgetItem("Pending"))

        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            size = len(f.read().strip())
        self.file_table.setItem(row, 2, QTableWidgetItem(f"{size:,} chars"))
        self.file_table.setItem(row, 3, QTableWidgetItem(""))

        self._file_list.append((row, filepath))

    def _clear_list(self):
        self._file_list.clear()
        self.file_table.setRowCount(0)

    def _start_batch(self):
        if self.worker and self.worker.isRunning():
            return

        if not self._file_list:
            self._log("Chua co file nao!")
            return

        voice_id = self.voice_input.text().strip()
        if not voice_id:
            self._log("Chua nhap Voice ID!")
            return

        output_dir = self.output_input.text().strip()
        os.makedirs(output_dir, exist_ok=True)

        mode = self.mode_combo.currentData()
        if self.language_worker and self.language_worker.isRunning():
            self._log("Dang lay ngon ngu voice, vui long doi...")
            return
        language_code = self.language_combo.currentData()

        # Auto-detect ngon ngu neu Language combo dang o "Auto"
        if language_code is None and self._file_list:
            try:
                file_paths = [fp for _, fp in self._file_list]
                detected = _detect_lang_from_files(file_paths, max_files=3)
                if detected:
                    from core.convert import LANGUAGE_NAMES
                    lang_name = LANGUAGE_NAMES.get(detected, detected)
                    self._log(f"[Lang] Auto-detect: {detected} ({lang_name})")
                    language_code = detected
                else:
                    self._log("[Lang] Khong detect duoc, de ElevenLabs tu xu ly")
            except Exception as det_err:
                self._log(f"[Lang] Detect loi: {str(det_err)[:60]}")

        self.worker = VoiceWorker(
            files=list(self._file_list),
            output_dir=output_dir,
            voice_id=voice_id,
            model_id=self.model_combo.currentText(),
            stability=self.stability_spin.value() / 100,
            similarity=self.similarity_spin.value() / 100,
            speed=self.speed_spin.value(),
            output_format=self.quality_combo.currentText(),
            mode=mode,
            config=self.config,
            language_code=language_code,
        )

        self.worker.log_signal.connect(self._log)
        self.worker.file_started.connect(self._on_file_started)
        self.worker.file_done.connect(self._on_file_done)
        self.worker.file_error.connect(self._on_file_error)
        self.worker.all_done.connect(self._on_all_done)

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.lbl_status.setText("Dang convert...")
        self._elapsed_timer.start()
        self._timer.start(1000)

        self.worker.start()

    def _stop_batch(self):
        if self.worker:
            self.worker.cancel()
            self._log("Dang dung...")

    def _on_file_started(self, idx, name):
        if idx < self.file_table.rowCount():
            self.file_table.item(idx, 1).setText("Dang xu ly")
            self.file_table.item(idx, 1).setForeground(QColor("#2980b9"))

    def _on_file_done(self, idx, path, size):
        if idx < self.file_table.rowCount():
            self.file_table.item(idx, 1).setText("Hoan thanh")
            self.file_table.item(idx, 1).setForeground(QColor("#27ae60"))
            self.file_table.item(idx, 3).setText(f"{size/1024/1024:.1f} MB")

    def _on_file_error(self, idx, msg):
        if idx < self.file_table.rowCount():
            self.file_table.item(idx, 1).setText(f"Loi: {msg[:20]}")
            self.file_table.item(idx, 1).setForeground(QColor("#e74c3c"))

    def _on_all_done(self, done, error):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._timer.stop()
        elapsed = self._elapsed_timer.elapsed() // 1000
        self.lbl_status.setText(
            f"Xong: {done} | Loi: {error} | Thoi gian: {elapsed}s")
        self._log(f"XONG - {done} OK, {error} loi, {elapsed}s")
        self._update_stats()

    def _update_elapsed(self):
        if self._elapsed_timer.isValid():
            t = self._elapsed_timer.elapsed() // 1000
            total = self.file_table.rowCount()
            self.lbl_status.setText(f"Dang convert... | {total} files | {t}s")

    # ============================================================
    # MODE
    # ============================================================

    def _on_mode_change(self, idx):
        self._update_stats()

    def _update_stats(self):
        mode = self.mode_combo.currentData()
        if mode == "a":
            try:
                from accounts.bridge import get_ready_accounts, get_stats
                stats = get_stats("elevenlabs")
                ready = len(get_ready_accounts("elevenlabs"))
                remaining = stats.get("credits_limit", 0) - stats.get("credits_used", 0)
                self.lbl_mode_info.setText(
                    f"Ready accounts: {ready} | Credit: {remaining:,} chars")
                self.lbl_mode_status.setText(
                    f"Mode A | {ready} TK | {remaining:,} chars")
            except Exception:
                self.lbl_mode_info.setText("TK: chua co du lieu")
                self.lbl_mode_status.setText("Mode A | chua co TK")
        else:
            # Mode B: đ�?c từ JSON status
            try:
                import json
                json_file = os.path.join(
                    PROJECT_ROOT, "config",
                    "1000tk_real_status.json")
                with open(json_file, 'r') as f:
                    data = json.load(f)
                s = data.get("summary", {})
                alive = s.get("alive", 0)
                total_chars = sum(
                    a.get("chars_remaining", 0)
                    for a in data.get("accounts", [])
                    if a.get("status") == "alive")
                self.lbl_mode_info.setText(
                    f"TK alive: {alive} | "
                    f"Chars: {total_chars:,}")
                self.lbl_mode_status.setText(
                    f"Mode B | {alive} TK | "
                    f"{total_chars:,} chars")
            except Exception:
                self.lbl_mode_info.setText(
                    "Chua scan TK (tab Accounts)")
                self.lbl_mode_status.setText(
                    "Mode B | chua scan")

    def _open_accounts(self):
        """Mở Account Manager."""
        import subprocess
        subprocess.Popen([sys.executable, "run_accounts.py"],
                         cwd=PROJECT_ROOT)

    # ============================================================
    # LOG
    # ============================================================

    def _log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log_area.append(f"[{ts}] {msg}")
        self.log_area.verticalScrollBar().setValue(
            self.log_area.verticalScrollBar().maximum())

    def closeEvent(self, event):
        # Kiểm tra Auto Convert có đang chạy không
        auto_running = (
            hasattr(self, 'auto_tab')
            and self.auto_tab.auto_worker is not None
            and self.auto_tab.auto_worker.isRunning()
        )

        if auto_running:
            reply = QMessageBox.question(
                self,
                "Xác nhận thoát",
                "Auto Convert đang chạy!\n\nBạn có chắc muốn thoát không?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return

        # Dừng manual worker
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(3000)

        # Dừng Auto Convert worker + voice worker
        if hasattr(self, 'auto_tab'):
            try:
                self.auto_tab._stop_auto()
                if self.auto_tab.auto_worker:
                    self.auto_tab.auto_worker.wait(3000)
            except Exception:
                pass

        event.accept()
        # Thoat HAN: force-kill moi thread con con lai (AutoWorker, VoiceWorker...)
        # -> khong con pythonw.exe "zombie" trong Task Manager.
        import os as _os
        _os._exit(0)


def main():
    import datetime
    import traceback

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    # Dong cua so = thoat app HAN (khong thu nho xuong tray nua).
    app.setQuitOnLastWindowClosed(True)

    # Bat loi khong xu ly duoc trong Qt slots (main thread)
    _crash_log = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs", "crash.log")
    os.makedirs(os.path.dirname(_crash_log), exist_ok=True)

    def _qt_excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, SystemExit):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log_line = f"\n[{ts}] QT EXCEPTION:\n{msg}\n"
        try:
            with open(_crash_log, "a", encoding="utf-8") as f:
                f.write(log_line)
        except Exception:
            pass
        print(log_line)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _qt_excepthook

    # Log khi Qt event loop sap thoat
    def _on_about_to_quit():
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stack = "".join(traceback.format_stack())
        msg = f"\n[{ts}] APP ABOUT TO QUIT\nStack:\n{stack}\n"
        try:
            with open(_crash_log, "a", encoding="utf-8") as f:
                f.write(msg)
        except Exception:
            pass
        print(msg)

    app.aboutToQuit.connect(_on_about_to_quit)

    window = VoiceTool()

    # === HIEN THI WINDOW ===
    window.show()

    # Dam bao window nam trong man hinh (fix RDP reconnect)
    screen = app.primaryScreen().availableGeometry()
    geo = window.frameGeometry()
    if not screen.intersects(geo):
        window.move(
            screen.x() + (screen.width() - window.width()) // 2,
            screen.y() + (screen.height() - window.height()) // 2,
        )

    # === TU DONG KHOI PHUC MASTER HET HAN (chay nen, 5s sau khi mo tool) ===
    # Giu tham chieu tren window de worker khong bi GC giua chung.
    def _start_master_recover():
        try:
            window._startup_recover = StartupRecoverWorker()
            window._startup_recover.log_signal.connect(lambda m: log.info(m))
            window._startup_recover.start()
        except Exception as _e:
            print(f"[Auto-recover] khong khoi dong duoc: {_e}")
    QTimer.singleShot(5000, _start_master_recover)

    # === BAO TRI NEN DINH KY (tool 24/7): quet quota + reset + canh bao + re-link ===
    def _is_converting():
        try:
            if getattr(window, "worker", None) and window.worker.isRunning():
                return True
            at = getattr(window, "auto_tab", None)
            if at and getattr(at, "voice_worker", None) and at.voice_worker.isRunning():
                return True
        except Exception:
            pass
        return False

    def _maybe_maintenance():
        try:
            cfg = Config()
            if not cfg.get("auto_maintenance", True):
                return
            if _is_converting():
                return   # dang convert -> de lan sau, tranh tranh tai nguyen
            mw = getattr(window, "_maint_worker", None)
            if mw and mw.isRunning():
                return
            window._maint_worker = MaintenanceWorker(
                do_relink=bool(cfg.get("auto_relink", False)))
            window._maint_worker.log_signal.connect(lambda m: log.info(m))
            window._maint_worker.start()
        except Exception as _e:
            print(f"[Maintenance] khong khoi dong duoc: {_e}")

    try:
        _interval_h = float(Config().get("maintenance_interval_hours", 12) or 12)
    except Exception:
        _interval_h = 12
    window._maint_timer = QTimer()
    window._maint_timer.timeout.connect(_maybe_maintenance)
    window._maint_timer.start(max(1, int(_interval_h * 3600 * 1000)))
    # Chay 1 lan ~5 phut sau khi mo tool (sau khi recover master xong)
    QTimer.singleShot(300000, _maybe_maintenance)

    # === CHUAN BI TRUOC TOKEN (pool warmer): nap san token o nen ===
    def _warm_pool():
        w = getattr(window, "_pool_warmer", None)
        if w and w.isRunning():
            return
        window._pool_warmer = PoolWarmerWorker(target=40)
        window._pool_warmer.log_signal.connect(lambda m: log.info(m))
        window._pool_warmer.start()

    def _topup_pool():
        # Nap them khi token san sang xuong thap -> luon co san truoc khi can
        try:
            from core.master_pool import shared_pool_ready
            r = shared_pool_ready()
            if 0 <= r < 15:
                _warm_pool()
        except Exception:
            pass

    QTimer.singleShot(8000, _warm_pool)          # nap truoc ~8s sau khi mo tool
    window._warm_timer = QTimer()
    window._warm_timer.timeout.connect(_topup_pool)
    window._warm_timer.start(90000)              # kiem tra top-up moi 90s

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
