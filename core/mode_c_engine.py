"""
mode_c_engine.py — Engine BEN BI cho Mode C (anonymous TTS).

Yeu cau cot loi (theo dung nhu cau nguoi dung):
  1. Lam nhieu tren 1 Chrome/IP CHAC CHAN bi loi -> phai TU DOI Chrome + DOI IP 4G.
  2. Nhieu Chrome SONG SONG (dung chung 1 4G) de ra voice nhanh.
  3. Xu ly DU LOAI loi: flag IP, token fail, timeout/mang, chunk loi.

Kien truc:
  - ModeCEngine: giu 1 Proxy4G CHUNG + lock xoay IP (generation counter tranh xoay trung).
  - N BrowserSlot: moi slot = 1 Chrome (Camoufox) rieng, chay trong 1 thread.
  - Chia chunks (co index) cho cac slot qua hang doi; ghep ket qua theo dung thu tu.
  - Loi -> phan loai -> hanh dong:
      * AnonUnusualActivity (IP flag)  -> xoay 4G (ca pool dung IP moi) + mo Chrome moi.
      * AnonTokenError (khong lay token)-> Chrome hong -> mo Chrome moi (cung IP).
      * timeout/network                 -> retry ngan (cung Chrome/IP).
      * text_too_long/validation        -> loi that (khong retry, raise).
  - Moi Chrome tu DONG mo lai sau X token (chu dong lam moi truoc khi bi flag).
"""
import os
import time
import threading
import queue
import subprocess

try:
    from utils.logger import log
except Exception:
    class _L:
        def info(self, *a): print(*a)
        def warning(self, *a): print(*a)
    log = _L()


def kill_orphan_browsers(older_than_sec: int = 0):
    """Kill process camoufox/firefox mo coi (24/7 chong ro ri RAM).

    older_than_sec=0: kill het. >0: chi kill process gia hon (dang chay lau -> treo).
    Chi goi khi CHAC chan khong con slot nao dang dung browser (giua cac file/batch).
    """
    if os.name != "nt":
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "camoufox.exe", "/T"],
            capture_output=True, timeout=15)
    except Exception:
        pass
    # Camoufox chay tren firefox engine -> ten process co the la firefox
    # KHONG kill firefox cua nguoi dung: chi kill neu path thuoc camoufox cache.
    try:
        import glob
        cam_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "camoufox").lower()
        out = subprocess.run(
            ["wmic", "process", "where", "name='firefox.exe'",
             "get", "ProcessId,ExecutablePath", "/format:csv"],
            capture_output=True, text=True, timeout=15)
        for line in (out.stdout or "").splitlines():
            parts = line.strip().split(",")
            if len(parts) >= 3 and cam_dir and cam_dir in (parts[1] or "").lower():
                pid = parts[-1].strip()
                if pid.isdigit():
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=10)
    except Exception:
        pass


# ============================================================
# VALIDATE AUDIO — dam bao chunk + file cuoi CHUAN, DU, khong hong (chat luong triệu do)
# ============================================================
def _valid_mp3_bytes(audio: bytes, min_bytes: int = 2000) -> bool:
    """Audio bytes co phai MP3 hop le toi thieu? (header + du kich thuoc)."""
    if not audio or len(audio) < min_bytes:
        return False
    # MP3 header: ID3 tag hoac frame sync 0xFFEx/0xFFFx
    if audio[:3] == b"ID3":
        return True
    if len(audio) >= 2 and audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0:
        return True
    return False


def _audio_duration(path: str) -> float:
    """Thoi luong audio (giay) qua ffprobe. -1 neu loi/khong doc duoc."""
    try:
        from core.audio_merger import FFPROBE
        out = subprocess.run(
            [FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30)
        s = (out.stdout or "").strip()
        return float(s) if s else -1
    except Exception:
        return -1


def _valid_mp3_file(path: str, min_dur: float = 0.3) -> bool:
    """File mp3 tren disk co hop le + du thoi luong khong? (dung khi resume checkpoint)."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < 2000:
            return False
        return _audio_duration(path) >= min_dur
    except Exception:
        return False


def _audio_duration_bytes(audio: bytes) -> float:
    """Thoi luong (giay) cua audio bytes (ghi file tam roi ffprobe). 0 neu loi."""
    if not audio:
        return 0
    try:
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".mp3")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(audio)
            d = _audio_duration(tmp)
            return d if d > 0 else 0
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass
    except Exception:
        return 0

from core.anonymous_tts import (
    AnonymousSession, send_anonymous,
    AnonUnusualActivity, AnonTokenError, AnonIPExhausted,
)


# NGUONG DO DUOC (2026-07-21, 4G Viettel, voice tieng Viet):
#   - 1 IP lam duoc 16 request roi sign_in_required. -> xoay CHU DONG o 15 (chua cham loi).
#   - 1 Chrome mint >=16 token khong hong (chua cham nguong Chrome trong test IP).
IP_REQUEST_BUDGET = 15        # xoay 4G CHU DONG sau bao nhieu request/IP (< 16 de an toan)
# Chu dong lam moi Chrome sau bao nhieu token (tranh dung 1 fingerprint qua lau)
REFRESH_BROWSER_EVERY = 30
# So lan thu lai toi da cho 1 chunk truoc khi bo
MAX_CHUNK_ATTEMPTS = 6
# Cooldown giua 2 lan xoay IP (giay) — tranh xoay lien tuc khi nhieu thread cung flag
ROTATE_COOLDOWN = 8

# Tai nguyen 1 Camoufox chiem (uoc tinh de tinh so Chrome linh hoat)
RAM_PER_CHROME_MB = 350       # RAM 1 Camoufox (~250-350MB) + buffer
RAM_KEEP_FREE_MB = 3000       # chua lai cho he thong (khong an het RAM)


def auto_browser_count(n_chunks: int, cfg_max: int, on_log=lambda *_: None) -> int:
    """Tinh so Chrome TOI UU theo tai nguyen may LUC NAY + so chunk + tran cau hinh.

    Muc dich: may khoe/ranh -> nhieu Chrome (nhanh); may yeu/ban -> it lai (khong treo).
    - RAM: moi Chrome ~350MB, chua lai 3GB cho he thong.
    - CPU: khong vuot so core (mint token ton CPU) va chua 2 core cho he thong.
    - Khong bao gio nhieu hon so chunk (thua Chrome vo ich).
    - Khong vuot tran nguoi dung dat (cfg_max).
    -> so Chrome (>=1).
    """
    hard_max = max(1, int(cfg_max))
    try:
        import psutil
        vm = psutil.virtual_memory()
        free_mb = vm.available / (1024 * 1024)
        by_ram = int((free_mb - RAM_KEEP_FREE_MB) / RAM_PER_CHROME_MB)
        cores = psutil.cpu_count(logical=True) or 4
        cpu_busy = psutil.cpu_percent(interval=0.5)   # % dang dung
        # CPU con ranh -> cho nhieu Chrome; ban -> it lai
        by_cpu = int((cores - 2) * (1 - cpu_busy / 100.0)) + 1
        n = min(hard_max, n_chunks, max(1, by_ram), max(1, by_cpu))
        n = max(1, n)
        on_log(f"⚙ [Auto] {n} Chrome (RAM trong {free_mb/1024:.1f}GB->{by_ram}, "
               f"CPU {cpu_busy:.0f}%->{by_cpu}, chunk {n_chunks}, tran {hard_max})")
        return n
    except Exception:
        # Khong co psutil -> dung min(tran, chunk)
        return max(1, min(hard_max, n_chunks))


class ModeCEngine:
    """Quan ly pool nhieu Chrome + 1 4G chung, xoay IP khi flag."""

    def __init__(self, voice_id, model_id="eleven_v3", language_code="vi",
                 use_4g=True, n_browsers=2, headless=False,
                 on_log=lambda *_: None):
        self.voice_id = voice_id
        self.model_id = model_id
        self.language_code = language_code
        self.use_4g = use_4g
        self.n_browsers = max(1, int(n_browsers))
        self.headless = headless
        self.on_log = on_log

        self._p4g = None
        if use_4g:
            from accounts.proxy import Proxy4G
            self._p4g = Proxy4G()

        self._rotate_lock = threading.Lock()
        self._ip_generation = 0        # tang moi lan xoay IP
        self._last_rotate = 0.0
        self._cancelled = False
        # Ngan sach IP: dem so request da dung tren IP hien tai (CHUNG cho moi Chrome).
        # Dat IP_REQUEST_BUDGET (15) -> xoay CHU DONG truoc khi cham loi (16).
        self._ip_used = 0
        self._budget_lock = threading.Lock()
        self._cur_ip = "?"             # IP 4G hien tai (cache de log, khong goi API lien tuc)
        if self._p4g:
            try:
                self._cur_ip = self._p4g.get_ip() or "?"
            except Exception:
                pass
        # Thong ke phien (de log tong ket + GUI doc)
        self.stat = {
            "chunk_ok": 0, "chunk_fail": 0, "ip_rotations": 0,
            "chrome_reopens": 0, "retries": 0, "flags": 0,
        }

    def current_ip(self):
        return self._cur_ip

    def recover_4g(self):
        """Luong socks5 4G reset (ConnectionReset) -> scan/reconnect. Thread-safe +
        chong spam: nhieu slot cung loi chi scan 1 lan trong 15s.
        """
        if not self._p4g:
            return
        now = time.time()
        with self._rotate_lock:
            if now - getattr(self, "_last_recover", 0) < 15:
                return   # vua recover xong -> khoi lam lai
            self._last_recover = now
            try:
                ok = self._p4g.ensure_alive(on_log=self.on_log)
                if ok:
                    self._cur_ip = self._p4g.get_ip() or self._cur_ip
                    self.on_log(f"  ✓ 4G reconnect OK (IP {self._cur_ip})")
                else:
                    self.on_log("  ⚠ 4G VAN chua thong sau scan — kiem tra tab '4G Proxy' "
                                "(dien thoai/ADB/EveryProxy)")
            except Exception as e:
                self.on_log(f"  ⚠ recover 4G loi: {str(e)[:60]}")

    def cancel(self):
        self._cancelled = True

    def start_file(self, n_chunks: int):
        """Goi DAU moi file: dam bao file lam TRON tren 1 IP (khong dut giua chung).

        Neu ngan sach IP CON LAI khong du cho ca file (ma file vua trong 1 IP) -> xoay
        IP MOI ngay tu dau. -> file khong bao gio bi 'lam do roi het IP' giua chung.
        (File > 16 chunk khong the vua 1 IP -> van xoay giua chung, nhung checkpoint lo.)
        """
        if not self.use_4g:
            return
        with self._budget_lock:
            remaining = IP_REQUEST_BUDGET - self._ip_used
            need = min(n_chunks, IP_REQUEST_BUDGET)   # file lon: can it nhat full 1 IP
            if remaining < need:
                self.on_log(
                    f"🆕 [4G] File can {n_chunks} chunk, IP hien tai chi con {remaining} req "
                    f"-> xoay IP MOI de lam tron file (moi voice/file 1 IP sach)")
                self.rotate_ip(self._ip_generation)
                self._ip_used = 0

    def acquire_ip_slot(self, seen_generation: int):
        """Xin 1 'suat' request tren IP hien tai. Neu IP da dung >= budget -> xoay truoc.

        -> (generation hien tai). Worker dung gen nay de biet IP nao dang xai.
        Goi TRUOC moi request. Thread-safe.
        """
        with self._budget_lock:
            if self.use_4g and self._ip_used >= IP_REQUEST_BUDGET:
                # Het ngan sach IP -> xoay CHU DONG (truoc khi server tra sign_in_required)
                self.on_log(f"[ModeC] IP dung {self._ip_used}/{IP_REQUEST_BUDGET} req "
                            f"-> xoay CHU DONG")
                new_gen = self.rotate_ip(self._ip_generation)
                self._ip_used = 0
            self._ip_used += 1
            return self._ip_generation

    # ---------- proxy ----------
    def _proxies(self):
        """(proxy_server cho Chrome, proxy_requests cho Python). None neu khong 4G."""
        if self._p4g:
            return self._p4g.get_for_chrome(), self._p4g.get_for_requests()
        return None, None

    def rotate_ip(self, seen_generation: int) -> int:
        """Xoay IP 1 lan cho TOAN POOL. seen_generation = gen ma worker thay luc bi flag.

        Neu da co worker khac xoay (gen hien tai > seen) -> khong xoay lai, tra gen moi.
        -> generation moi (de worker biet phai mo Chrome voi IP moi).
        """
        with self._rotate_lock:
            if self._ip_generation > seen_generation:
                return self._ip_generation      # worker khac da xoay roi
            if not self._p4g:
                # Khong 4G -> khong xoay duoc, chi tang gen de mo Chrome moi
                self._ip_generation += 1
                self._ip_used = 0
                return self._ip_generation
            # Cooldown
            wait = ROTATE_COOLDOWN - (time.time() - self._last_rotate)
            if wait > 0:
                time.sleep(wait)
            try:
                old_ip = self._p4g.get_ip()
            except Exception:
                old_ip = ""
            # Xoay + VERIFY IP that su doi (24/7: neu 4G rot ADB, rotate co the khong doi IP).
            # Thu toi 3 lan; neu 4G chet han -> cho hoi (khong bo het file).
            new_ip = old_ip
            for attempt in range(3):
                try:
                    self._p4g.rotate(wait=20)
                    time.sleep(2)
                    new_ip = self._p4g.get_ip()
                except Exception as e:
                    self.on_log(f"[ModeC] xoay 4G loi lan {attempt+1}: {str(e)[:70]}")
                    new_ip = ""
                if new_ip and new_ip != old_ip:
                    break   # IP da doi that su
                # Chua doi / 4G 'chet gia' -> SCAN THIET BI (bug: chi can scan la song)
                self.on_log(f"[ModeC] IP chua doi ({old_ip}->{new_ip or '?'}), scan 4G...")
                try:
                    self._p4g.ensure_alive(on_log=self.on_log)
                except Exception:
                    pass
                time.sleep(6)
            self._ip_generation += 1
            self._ip_used = 0                    # IP moi -> reset ngan sach
            self._last_rotate = time.time()
            self._cur_ip = new_ip or "?"
            self.stat["ip_rotations"] += 1
            self.on_log(f"🔄 [4G] Xoay IP: {old_ip or '?'} -> {new_ip or '?'} "
                        f"(lan xoay #{self.stat['ip_rotations']}, gen {self._ip_generation})")
            return self._ip_generation


class _BrowserSlot:
    """1 Chrome (Camoufox) chay trong 1 thread. Tu mo lai khi hong/flag."""

    def __init__(self, engine: ModeCEngine, slot_id: int):
        self.engine = engine
        self.slot_id = slot_id
        self.session = None
        self.ip_gen = engine._ip_generation
        self.tokens_minted = 0

    def _open(self):
        # QUAN TRONG: browser mint token dung IP MAY (KHONG qua 4G).
        # Ly do (da test): Camoufox qua 4G socks5 -> hCaptcha nghi -> token bi flag.
        # Chi REQUEST CUOI (send_anonymous) di qua 4G. Giong tool "Pro Lifetime".
        self.session = AnonymousSession(
            engine="camoufox", headless=self.engine.headless,
            proxy_server=None)
        self.session.open()
        self.ip_gen = self.engine._ip_generation
        self.tokens_minted = 0
        self.engine.on_log(f"[ModeC] slot{self.slot_id}: Chrome sach (mint qua IP may)")

    def _reopen(self):
        # LUU Y: Playwright/Camoufox sync API gan lien voi 1 THREAD -> KHONG duoc mo
        # trong thread phu (loi 'cannot switch to a different thread'). Mo truc tiep
        # trong thread cua slot. Timeout treo goto duoc dat trong _open (page timeout).
        self.close()
        for _ in range(3):
            try:
                self._open()
                return
            except Exception as e:
                self.engine.on_log(f"[ModeC] slot{self.slot_id}: mo Chrome loi, thu lai: {str(e)[:60]}")
                time.sleep(3)
        raise Exception(f"slot{self.slot_id}: khong mo duoc Chrome")

    def ensure_open(self):
        # Mo lai CHI KHI: chua co Chrome, HOAC da dung qua nhieu token (lam moi fingerprint).
        # KHONG mo lai khi xoay IP -> browser mint qua IP MAY, doc lap voi 4G -> doi IP 4G
        # KHONG anh huong browser. (Truoc day mo lai moi lan xoay IP -> cham x3 vi mo lai
        # ca 3 Chrome ~15s moi lan.) -> tiet kiem rat nhieu thoi gian.
        if (self.session is None
                or self.tokens_minted >= REFRESH_BROWSER_EVERY):
            self._reopen()
        # Cap nhat gen (khong reopen) de dong bo — browser dung tiep binh thuong
        self.ip_gen = self.engine._ip_generation

    def make_audio(self, chunk: str, tag: str = "") -> bytes:
        """Tao audio cho 1 chunk, tu xu ly loi (doi Chrome/IP). -> bytes.

        tag: nhan de log (vd 'chunk 3/13') -> log giau du lieu de phat hien loi.
        """
        last_err = None
        for attempt in range(MAX_CHUNK_ATTEMPTS):
            if self.engine._cancelled:
                raise Exception("cancelled")
            t0 = time.time()
            try:
                # Xin suat IP (xoay CHU DONG neu IP da dung >= 15 req) -> lay gen + proxy MOI NHAT
                self.ip_gen = self.engine.acquire_ip_slot(self.ip_gen)
                _, proxy_requests = self.engine._proxies()
                self.ensure_open()
                t_mint = time.time()
                token = self.session.mint_token()
                self.tokens_minted += 1
                mint_s = time.time() - t_mint
                t_send = time.time()
                audio = send_anonymous(
                    self.engine.voice_id,
                    token, chunk, self.engine.model_id,
                    self.engine.language_code, proxy=proxy_requests)
                send_s = time.time() - t_send
                # VALIDATE: audio phai hop le (khong nhan mp3 hong/cut) -> voice cuoi chuan
                if not _valid_mp3_bytes(audio):
                    last_err = Exception(f"audio khong hop le ({len(audio or b'')}b)")
                    self.engine.stat["retries"] += 1
                    self.engine.on_log(f"  ⚠ slot{self.slot_id} {tag}: audio HONG "
                                       f"({len(audio or b'')}b) -> lam lai (thu {attempt+1})")
                    time.sleep(1)
                    continue
                # LOG CHI TIET: IP dung, thoi gian mint/send, so chars
                if attempt > 0:
                    self.engine.stat["retries"] += attempt
                self.engine.on_log(
                    f"  ✓ slot{self.slot_id} {tag}: {len(audio):,}b | IP {self.engine.current_ip()} | "
                    f"mint {mint_s:.0f}s + tts {send_s:.0f}s"
                    + (f" | thu lan {attempt+1}" if attempt > 0 else ""))
                return audio
            except AnonIPExhausted as e:
                # IP het luot free -> XOAY IP 4G. Token/browser van OK (browser mint qua
                # IP may, doc lap 4G) -> KHONG mo lai Chrome (nhanh hon nhieu). Chunk sau
                # tu dung IP moi qua proxy_requests.
                last_err = e
                self.engine.on_log(f"  💰 slot{self.slot_id} {tag}: IP het luot (16 req) -> xoay 4G")
                self.ip_gen = self.engine.rotate_ip(self.ip_gen)
            except AnonUnusualActivity as e:
                # Flag unusual_activity -> doi fingerprint = mo Chrome MOI (4G sach).
                last_err = e
                self.engine.stat["flags"] += 1
                self.engine.on_log(f"  🚩 slot{self.slot_id} {tag}: FLAG unusual_activity "
                                   f"-> doi Chrome (fingerprint moi)")
                try:
                    self._reopen()
                    self.engine.stat["chrome_reopens"] += 1
                except Exception as re:
                    last_err = re
            except AnonTokenError as e:
                # Khong lay duoc token -> Chrome hong -> mo Chrome moi (cung IP)
                last_err = e
                self.engine.on_log(f"  🔧 slot{self.slot_id} {tag}: khong lay duoc token "
                                   f"({str(e)[:50]}) -> mo Chrome moi")
                try:
                    self._reopen()
                    self.engine.stat["chrome_reopens"] += 1
                except Exception as re:
                    last_err = re
            except Exception as e:
                msg = str(e).lower()
                if "text_too_long" in msg or "validation" in msg:
                    raise    # loi that su -> khong retry
                last_err = e
                self.engine.stat["retries"] += 1
                # LOI KET NOI PROXY 4G (ConnectionReset/aborted/timeout socks5): luong 4G
                # hong, KHONG phai loi IP -> retry mu vo ich. Thu SCAN/reconnect 4G.
                is_conn = any(k in msg for k in (
                    "connection aborted", "connectionreset", "10054", "connection reset",
                    "socks", "max retries", "timed out", "connectionpool"))
                if is_conn:
                    self.engine.on_log(f"  🔌 slot{self.slot_id} {tag}: LOI KET NOI 4G "
                                       f"(luong socks5 reset) -> scan/reconnect 4G "
                                       f"({attempt+1}/{MAX_CHUNK_ATTEMPTS})")
                    self.engine.recover_4g()
                    time.sleep(3)
                else:
                    self.engine.on_log(f"  ⚠ slot{self.slot_id} {tag}: loi tam ({str(e)[:70]}) "
                                       f"-> thu lai ({attempt+1}/{MAX_CHUNK_ATTEMPTS})")
                    time.sleep(2)
        self.engine.stat["chunk_fail"] += 1
        raise Exception(f"slot{self.slot_id} {tag}: FAIL sau {MAX_CHUNK_ATTEMPTS} lan: {str(last_err)[:100]}")

    def close(self):
        if self.session:
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None


def generate_file(engine: ModeCEngine, txt_path: str, output_dir: str) -> str:
    """1 file txt -> 1 mp3 qua pool nhieu Chrome (song song) + 4G tu xoay IP.

    CHECKPOINT (24/7): moi chunk OK duoc luu ra file tam (.modec_ckpt/). Neu tool
    crash/tat giua chung, lan sau chi lam LAI chunk THIEU (khong mat cong chunk da xong).
    -> duong dan mp3. Raise neu that bai.
    """
    from core.text_splitter import clean_text, split_text
    from core.audio_merger import merge_audio_bytes
    from core.anonymous_tts import ANON_MAX_CHARS

    t_start = time.time()
    with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
        text = clean_text(f.read())
    if not text.strip():
        raise Exception(f"File rong: {txt_path}")

    chunks = split_text(text, max_chars=ANON_MAX_CHARS)
    base = os.path.splitext(os.path.basename(txt_path))[0]

    # Checkpoint dir: luu tung chunk audio da xong
    ckpt_dir = os.path.join(output_dir, ".modec_ckpt", base)
    os.makedirs(ckpt_dir, exist_ok=True)

    def _ckpt_path(i):
        return os.path.join(ckpt_dir, f"chunk_{i:04d}.mp3")

    # Load chunk da co tu phien truoc (checkpoint) -> chi lam chunk thieu.
    # RESUME SACH: chi nhan checkpoint HOP LE (mp3 doc duoc + du thoi luong). Chunk hong
    # -> xoa + lam lai -> voice cuoi khong the loi do checkpoint rac.
    results = [None] * len(chunks)
    todo = []
    for i in range(len(chunks)):
        cp = _ckpt_path(i)
        if _valid_mp3_file(cp):
            with open(cp, "rb") as f:
                results[i] = f.read()
        else:
            if os.path.exists(cp):
                try:
                    os.remove(cp)   # xoa checkpoint hong
                except Exception:
                    pass
            todo.append(i)

    done_before = len(chunks) - len(todo)
    # So Chrome LINH HOAT theo tai nguyen may LUC NAY (RAM/CPU) + so chunk + tran cau hinh.
    # -> may khoe/ranh chay nhieu Chrome (nhanh), may yeu/ban it lai (khong treo).
    n_slots = auto_browser_count(max(1, len(todo)), engine.n_browsers, on_log=engine.on_log)
    engine.on_log(f"[ModeC] {base}: {len(text):,} chars -> {len(chunks)} chunk "
                  f"({done_before} da co, {len(todo)} can lam), {n_slots} Chrome song song")

    if todo:
        # DAM BAO file lam tron tren 1 IP (moi voice/file 1 IP sach) -> khong dut giua chung
        engine.start_file(len(todo))
        q = queue.Queue()
        for i in todo:
            q.put((i, chunks[i]))
        errors = []
        err_lock = threading.Lock()

        def worker(slot_id):
            slot = _BrowserSlot(engine, slot_id)
            try:
                while not engine._cancelled:
                    try:
                        idx, chunk = q.get_nowait()
                    except queue.Empty:
                        break
                    tag = f"chunk {idx+1}/{len(chunks)}"
                    try:
                        audio = slot.make_audio(chunk, tag=tag)
                        results[idx] = audio
                        engine.stat["chunk_ok"] += 1
                        # Luu checkpoint NGAY, ATOMIC (ghi .tmp roi rename) + fsync
                        # -> khong bao gio co checkpoint ghi do dang khi crash.
                        try:
                            cp = _ckpt_path(idx)
                            tmp = cp + ".tmp"
                            with open(tmp, "wb") as cf:
                                cf.write(audio)
                                cf.flush()
                                os.fsync(cf.fileno())
                            os.replace(tmp, cp)
                        except Exception:
                            pass
                    except Exception as e:
                        with err_lock:
                            errors.append((idx, str(e)[:120]))
                        engine.on_log(f"  ❌ {tag} THAT BAI: {str(e)[:100]}")
                    finally:
                        q.task_done()
            finally:
                slot.close()

        threads = [threading.Thread(target=worker, args=(i+1,)) for i in range(n_slots)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if not all(r is not None for r in results):
            missing = [i+1 for i, r in enumerate(results) if r is None]
            # KHONG xoa checkpoint -> lan sau lam tiep chunk thieu
            raise Exception(f"[ModeC] {base}: con thieu chunk {missing[:10]}, loi: {errors[:2]}")

    # Double-check: moi chunk audio hop le truoc khi ghep (khong ghep rac)
    for i, r in enumerate(results):
        if not _valid_mp3_bytes(r):
            raise Exception(f"[ModeC] {base}: chunk {i+1} audio hong truoc ghep -> khong ghep")

    os.makedirs(output_dir, exist_ok=True)
    mp3_path = os.path.join(output_dir, f"{base}.mp3")
    tmp_mp3 = mp3_path + ".building.mp3"
    if os.path.exists(tmp_mp3):
        try:
            os.remove(tmp_mp3)
        except Exception:
            pass
    if len(results) == 1:
        with open(tmp_mp3, "wb") as f:
            f.write(results[0])
    else:
        merge_audio_bytes(results, tmp_mp3, silence_between_ms=500)

    # VALIDATE FILE CUOI: doc duoc + DU thoi luong (~ tong cac chunk, tru hao 30%).
    # -> voice cuoi CHAC CHAN du, khong thieu doan. Neu thieu -> GIU checkpoint, raise.
    final_dur = _audio_duration(tmp_mp3)
    exp_dur = sum(_audio_duration_bytes(r) for r in results)   # tong thoi luong chunk
    if final_dur < 0:
        raise Exception(f"[ModeC] {base}: file cuoi khong doc duoc -> huy")
    if exp_dur > 0 and final_dur < exp_dur * 0.7:
        raise Exception(f"[ModeC] {base}: file cuoi thieu ({final_dur:.0f}s < mong doi {exp_dur:.0f}s)")

    # OK -> doi ten atomic sang file that
    os.replace(tmp_mp3, mp3_path)
    dt = int(time.time() - t_start)
    st = engine.stat
    engine.on_log(
        f"✅ XONG {base}.mp3 | {os.path.getsize(mp3_path)//1024:,}KB, {final_dur:.0f}s "
        f"(~{final_dur/60:.1f} phut) | {dt}s | IP {engine.current_ip()} | "
        f"xoay IP {st['ip_rotations']}x, doi Chrome {st['chrome_reopens']}x, "
        f"retry {st['retries']}, flag {st['flags']}")

    # Xong + file cuoi HOP LE -> xoa checkpoint dir
    try:
        import shutil
        shutil.rmtree(ckpt_dir, ignore_errors=True)
    except Exception:
        pass
    return mp3_path
