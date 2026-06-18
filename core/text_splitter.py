"""
Text Splitter - Chia text thông minh cho voice TTS.

Nguyên tắc cắt (thứ tự ưu tiên):
1. Đoạn trống (\\n\\n) - nghỉ giữa đoạn văn, tự nhiên nhất
2. Xuống dòng (\\n) - nghỉ giữa dòng
3. Dấu kết câu (. ! ?) - nghỉ cuối câu
4. Dấu phẩy/chấm phẩy (, ; :) - nghỉ giữa câu
5. Khoảng trắng - cắt giữa từ (cuối cùng)
6. Cắt cứng - khi không có dấu nào (hiếm khi)

Luôn cắt GẦN max_chars nhất (tận dụng tối đa credit TK).
"""

import re
from typing import List, Dict

DEFAULT_MAX_CHARS = 5000  # Max an toan. Neu vuot → tool tu chia doi


SPECIAL_CHARS_PATTERN = re.compile(r'[^\w\s.,;:!?%\'"()\-\n\r/。！？、，；：「」『』（）\[\]…・·‘’“”–—〜～]', re.UNICODE)

CHAR_NORMALIZE_MAP = str.maketrans({
    '﻿': '',
    '​': '',
    '‌': '',
    '‍': '',
    '\xa0': ' ',
    '　': ' ',
})


_CJK_RANGES = (
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xAC00, 0xD7AF),
)


def _has_cjk(text: str) -> bool:
    return any(
        start <= ord(char) <= end
        for char in text
        for start, end in _CJK_RANGES
    )


def _should_remove_special(text: str, remove_special: bool) -> bool:
    return remove_special and not _has_cjk(text)


def clean_text(text: str, remove_special: bool = True) -> str:
    if not text:
        return ""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = text.translate(CHAR_NORMALIZE_MAP)
    if _should_remove_special(text, remove_special):
        text = SPECIAL_CHARS_PATTERN.sub('', text)
    text = re.sub(r' +', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def add_break_tags(text: str, break_chars: List[Dict] = None) -> str:
    if not break_chars:
        return text
    for bc in break_chars:
        char = bc.get("char", "")
        seconds = bc.get("seconds", 0.3)
        if char and seconds > 0:
            text = re.sub(
                rf'({re.escape(char)})(\s)(?!<break)',
                rf'\1 <break time="{seconds}s" />\2',
                text
            )
    return text


def _find_best_split(text: str, max_chars: int) -> int:
    """Tìm vị trí cắt TỐT NHẤT trong text[:max_chars].

    Ưu tiên: đoạn trống > xuống dòng > dấu chấm > dấu phẩy > khoảng trắng.
    Luôn tìm vị trí GẦN max_chars nhất (tận dụng tối đa).
    """
    window = text[:max_chars]

    # 1. Đoạn trống (\n\n) - tự nhiên nhất
    pos = window.rfind('\n\n')
    if pos > max_chars * 0.3:  # Phải cắt ít nhất 30% (không cắt quá gần đầu)
        return pos + 2  # +2 để bỏ qua \n\n

    # 2. Xuống dòng (\n)
    pos = window.rfind('\n')
    if pos > max_chars * 0.3:
        return pos + 1

    # 3. Dấu kết câu + khoảng trắng sau
    for pattern in [r'[.!?。！？]\s', r'[.!?。！？]"?\s']:
        matches = list(re.finditer(pattern, window))
        if matches:
            last = matches[-1]
            if last.end() > max_chars * 0.3:
                return last.end()

    # 4. Dấu phẩy/chấm phẩy
    for char in ['; ', ', ', ': ', '；', '、', '，', '。', '！', '？']:
        pos = window.rfind(char)
        if pos > max_chars * 0.3:
            return pos + len(char)

    # 5. Khoảng trắng
    pos = window.rfind(' ')
    if pos > max_chars * 0.3:
        return pos + 1

    # 6. Cắt cứng (không có dấu nào)
    return max_chars


def split_text(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> List[str]:
    """Chia text thành chunks, cắt tại chỗ nghỉ tự nhiên.

    Mỗi chunk < max_chars, cắt gần max_chars nhất.
    """
    text = text.strip()
    if not text:
        return []

    if len(text) <= max_chars:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        remaining = remaining.strip()
        if not remaining:
            break

        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break

        split_pos = _find_best_split(remaining, max_chars)
        chunk = remaining[:split_pos].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_pos:]

    return chunks


def prepare_text(text: str, config: dict = None) -> List[str]:
    """Xử lý text: clean + break tags TRƯỚC + split SAU.

    Thêm break tags TRƯỚC khi split → chunk size chính xác.
    Tránh: split 9000 + thêm tags = 12000 → vượt credit 10000.
    """
    if config is None:
        config = {}
    remove_special = config.get("remove_special_chars", True)
    text = clean_text(text, remove_special)

    # Thêm break tags TRƯỚC khi split (size chính xác)
    if config.get("break_by_char", False):
        break_chars = config.get("break_chars", [])
        text = add_break_tags(text, break_chars)

    max_chars = config.get("max_chars_per_line", DEFAULT_MAX_CHARS)
    chunks = split_text(text, max_chars)
    return chunks


def estimate_chunks(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> int:
    text_len = len(text.strip())
    if text_len == 0:
        return 0
    if text_len <= max_chars:
        return 1
    return max(1, int(text_len / (max_chars * 0.85)) + 1)
