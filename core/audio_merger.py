"""
Audio Merger - Ghép + chuẩn hoá âm lượng cho YouTube.

Vấn đề: Mỗi chunk từ API khác nhau → âm lượng chênh lệch 10-12 dB.
Giải pháp 3 bước:
  1. Normalize từng chunk về cùng mức (-20 dBFS target)
  2. Ghép + silence giữa đoạn
  3. Loudness normalize file cuối theo chuẩn YouTube (-14 LUFS)

YouTube recommend: -14 LUFS, -1 dBFS true peak.
"""

import os
import shutil
import subprocess
import tempfile
from typing import List

from utils.logger import log

# Mức âm lượng mục tiêu
TARGET_DBFS = -20.0       # Mức chuẩn hoá từng chunk
YOUTUBE_LUFS = -14.0      # Chuẩn YouTube
TRUE_PEAK = -1.0          # True peak max


# Thư mục ffmpeg đi kèm dự án (portable: copy đi đâu cũng tìm được, không phụ thuộc máy cũ)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_tool(name: str) -> str:
    # UU TIEN ffmpeg BUNDLED trong du an TRUOC PATH.
    # Ly do: PATH may co the con entry cu/hong (vd D:\upload\ffmpeg\bin thieu ffmpeg.exe
    # hoac tro toi file khong ton tai tren may khac) -> shutil.which tra ve duong dan
    # hong -> pydub goi ffprobe -> WinError 2 khi merge. Bo ffmpeg/ (copy y nguyen theo
    # du an) chac chan co du 3 file -> dung truoc, on dinh moi may.
    candidates = [
        os.path.join(_PROJECT_ROOT, "ffmpeg", "bin", f"{name}.exe"),
        os.path.join(_PROJECT_ROOT, "ffmpeg", "bin", name.upper() + ".EXE"),
        os.path.join(_PROJECT_ROOT, "tools", "ffmpeg", "bin", f"{name}.exe"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    # Khong co bundled -> moi dung PATH
    found = shutil.which(name)
    if found:
        return found
    # Cuoi cung: cac vi tri cai chung tren may
    fallback = os.path.join("D:\\ffmpeg", "bin", f"{name}.exe")
    if os.path.exists(fallback):
        return fallback
    return name


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


def _normalize_chunk(segment, target_dbfs: float = TARGET_DBFS):
    """Chuẩn hoá âm lượng 1 chunk - micro-compression.

    Chia chunk thành segments 2 giây, normalize từng segment.
    → Âm lượng đều suốt chunk, không fade out cuối đoạn.
    → head/mid/tail chênh < 1 dB (thay vì 4-5 dB).
    """
    if segment.dBFS == float('-inf'):
        return segment

    # Bước 1: Normalize tổng thể
    gain = target_dbfs - segment.dBFS
    gain = max(-20.0, min(15.0, gain))
    segment = segment.apply_gain(gain)

    # Bước 2: Micro-compression - chia 2s, normalize từng segment
    # Giữ âm lượng đều, không để fade out
    SEGMENT_MS = 2000
    MAX_ADJUST = 6.0  # Không điều chỉnh quá +/-6 dB mỗi segment

    compressed = segment.__class__.empty()
    for start in range(0, len(segment), SEGMENT_MS):
        seg = segment[start:start + SEGMENT_MS]
        if seg.dBFS > float('-inf') and len(seg) > 200:
            adj = target_dbfs - seg.dBFS
            adj = max(-MAX_ADJUST, min(MAX_ADJUST, adj))
            seg = seg.apply_gain(adj)
        compressed += seg

    return compressed


def _loudness_normalize_ffmpeg(input_file: str, output_file: str,
                                target_lufs: float = YOUTUBE_LUFS,
                                true_peak: float = TRUE_PEAK) -> bool:
    """Dùng ffmpeg loudnorm filter - chuẩn broadcast/YouTube.

    2-pass: đo trước, rồi normalize chính xác.
    """
    try:
        # Pass 1: đo loudness
        cmd1 = [
            FFMPEG, "-i", input_file, "-af",
            f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11:print_format=json",
            "-f", "null", "-"
        ]
        r1 = subprocess.run(cmd1, capture_output=True, text=True, timeout=300)

        # Parse JSON output
        import json, re
        json_match = re.search(r'\{[^}]+\}', r1.stderr, re.DOTALL)
        if not json_match:
            return False

        stats = json.loads(json_match.group())
        measured_i = stats.get("input_i", "-24.0")
        measured_tp = stats.get("input_tp", "-2.0")
        measured_lra = stats.get("input_lra", "7.0")
        measured_thresh = stats.get("input_thresh", "-34.0")

        # Pass 2: apply normalization
        cmd2 = [
            FFMPEG, "-y", "-i", input_file, "-af",
            f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11:"
            f"measured_I={measured_i}:measured_TP={measured_tp}:"
            f"measured_LRA={measured_lra}:measured_thresh={measured_thresh}:"
            f"linear=true",
            "-ar", "44100", "-ab", "128k", output_file
        ]
        r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=300)
        return r2.returncode == 0

    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _merge_ffmpeg_only(input_files: List[str], output_file: str) -> str:
    """Ghép audio bằng ffmpeg (không cần pydub).
    
    Pipeline: concat → loudnorm 2-pass → YouTube-ready output.
    Chất lượng tương đương pydub pipeline.
    """
    temp_dir = tempfile.mkdtemp(prefix="merge_ffmpeg_")
    
    try:
        # Bước 1: Tạo file list cho ffmpeg concat
        list_file = os.path.join(temp_dir, "filelist.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for fp in input_files:
                # ffmpeg concat cần path escaped
                escaped = fp.replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")
        
        # Bước 2: Concat → file tạm
        concat_file = os.path.join(temp_dir, "concat.mp3")
        cmd_concat = [
            FFMPEG, "-y", "-f", "concat", "-safe", "0",
            "-i", list_file, "-c", "copy", concat_file
        ]
        r = subprocess.run(cmd_concat, capture_output=True, 
                          text=True, timeout=120)
        if r.returncode != 0:
            log.error(f"ffmpeg concat fail: {r.stderr[:200]}")
            raise RuntimeError(f"ffmpeg concat failed")
        
        # Bước 3: Loudness normalize 2-pass (-14 LUFS cho YouTube)
        os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
        success = _loudness_normalize_ffmpeg(
            concat_file, output_file, YOUTUBE_LUFS, TRUE_PEAK)
        
        if success:
            log.info(f"ffmpeg merge + loudnorm OK: {output_file} "
                     f"({os.path.getsize(output_file):,} bytes, -14 LUFS)")
        else:
            # Fallback: dùng file concat không loudnorm
            import shutil
            shutil.copy2(concat_file, output_file)
            log.warning(f"ffmpeg loudnorm fail → dùng concat raw: "
                       f"{output_file}")
        
        return output_file
        
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)


def merge_audio_files(
    input_files: List[str],
    output_file: str,
    crossfade_ms: int = 0,
    silence_between_ms: int = 0,
    normalize: bool = True,
) -> str:
    """Ghép + chuẩn hoá âm lượng cho YouTube.

    Quy trình:
    1. Load từng chunk → normalize về -20 dBFS (đồng bộ âm lượng)
    2. Ghép + silence giữa đoạn
    3. Export tạm
    4. ffmpeg loudnorm -14 LUFS (chuẩn YouTube)

    Args:
        input_files: Danh sách file audio (theo thứ tự)
        output_file: Đường dẫn output
        silence_between_ms: Ngắt âm giữa đoạn (ms)
        normalize: Chuẩn hoá âm lượng (mặc định True)

    Returns: Đường dẫn file output
    """
    try:
        from pydub import AudioSegment
        try:
            AudioSegment.converter = FFMPEG
            AudioSegment.ffprobe = FFPROBE
        except Exception:
            pass
    except ImportError:
        log.warning("pydub not found → dùng ffmpeg concat + loudnorm")
        return _merge_ffmpeg_only(input_files, output_file)

    if not input_files:
        raise ValueError("Không có file nào để ghép")

    if len(input_files) == 1 and not normalize:
        import shutil
        os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
        shutil.copy2(input_files[0], output_file)
        return output_file

    n = len(input_files)
    log.info(f"Ghép {n} files (ngắt âm: {silence_between_ms}ms, "
             f"chuẩn hoá: {'có' if normalize else 'không'})...")

    silence = AudioSegment.silent(duration=silence_between_ms) if silence_between_ms > 0 else None

    # Bước 1: Load + normalize từng chunk
    first = AudioSegment.from_file(input_files[0])
    if normalize:
        log.debug(f"  Chunk 1: {first.dBFS:+.1f} dBFS → {TARGET_DBFS:+.1f} dBFS")
        first = _normalize_chunk(first)
    combined = first

    for i, f in enumerate(input_files[1:], 1):
        if not os.path.exists(f):
            raise FileNotFoundError(f"File không tồn tại: {f}")

        segment = AudioSegment.from_file(f)

        if normalize:
            orig_db = segment.dBFS
            segment = _normalize_chunk(segment)
            if abs(orig_db - TARGET_DBFS) > 3:
                log.debug(f"  Chunk {i+1}: {orig_db:+.1f} → {TARGET_DBFS:+.1f} dBFS "
                         f"(điều chỉnh {TARGET_DBFS - orig_db:+.1f} dB)")

        if silence:
            combined = combined + silence
        if crossfade_ms > 0:
            combined = combined.append(segment, crossfade=crossfade_ms)
        else:
            combined = combined + segment

    # Bước 2: Export
    out_dir = os.path.dirname(output_file)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    if normalize:
        # Export tạm → ffmpeg loudnorm → file cuối cùng
        temp_file = output_file + ".tmp.mp3"
        combined.export(temp_file, format="mp3", bitrate="192k")

        # Bước 3: ffmpeg loudness normalization (-14 LUFS cho YouTube)
        success = _loudness_normalize_ffmpeg(temp_file, output_file)

        if success:
            os.remove(temp_file)
            log.info(f"Ghép + chuẩn hoá xong: {output_file} "
                     f"({os.path.getsize(output_file):,} bytes, -14 LUFS)")
        else:
            # Fallback: dùng file đã normalize chunk (không có LUFS)
            shutil.move(temp_file, output_file)
            log.info(f"Ghép xong (không LUFS): {output_file} "
                     f"({os.path.getsize(output_file):,} bytes)")
    else:
        combined.export(output_file, format="mp3", bitrate="128k")
        log.info(f"Ghép xong: {output_file} ({os.path.getsize(output_file):,} bytes)")

    return output_file


def merge_audio_bytes(
    audio_chunks: List[bytes],
    output_file: str,
    input_format: str = "mp3",
    crossfade_ms: int = 0,
    silence_between_ms: int = 500,
) -> str:
    """Ghép nhiều audio bytes thành 1 file.
    
    silence_between_ms: khoảng nghỉ giữa các chunk (mặc định 500ms)
    để tạo nhịp tự nhiên tại điểm ngắt câu/đoạn.
    """
    if not audio_chunks:
        raise ValueError("Không có audio data để ghép")

    if len(audio_chunks) == 1:
        os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
        with open(output_file, "wb") as f:
            f.write(audio_chunks[0])
        return output_file

    temp_files = []
    temp_dir = tempfile.mkdtemp(prefix="elevenlabs_tts_")

    try:
        for i, chunk in enumerate(audio_chunks):
            temp_path = os.path.join(temp_dir, f"chunk_{i:04d}.{input_format}")
            with open(temp_path, "wb") as f:
                f.write(chunk)
            temp_files.append(temp_path)

        return merge_audio_files(temp_files, output_file,
                                crossfade_ms,
                                silence_between_ms=silence_between_ms)
    finally:
        for f in temp_files:
            try:
                os.remove(f)
            except OSError:
                pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass
