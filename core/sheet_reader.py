"""
Sheet Reader — đọc Google Sheet "KA" cho Auto Convert.

Trả về:
- voice_map: folder_name → voice_id (từ tab "THÔNG TIN")
- folder_map: code → folder_name (từ tab "INPUT")
"""

import os
import time
from typing import Dict, Optional, Tuple

from utils.logger import log

# Cache timeout
_CACHE_TTL = 300  # 5 phút
_cache = {
    "voice_map": {},
    "folder_map": {},
    "last_update": 0,
}


def _sheet_cfg():
    """Lay (ten_sheet, tab_thong_tin, tab_input) tu settings.json.

    Moi may co the dung sheet khac -> doi o tab Auto Convert > Cai dat nang cao,
    hoac sua truc tiep config/settings.json: "sheet_name".
    """
    try:
        from utils.config import Config
        c = Config()
        return (
            (c.get("sheet_name", "KA") or "KA"),
            (c.get("sheet_tab_info", "THÔNG TIN") or "THÔNG TIN"),
            (c.get("sheet_tab_input", "INPUT") or "INPUT"),
        )
    except Exception:
        return "KA", "THÔNG TIN", "INPUT"


def _get_client():
    """Tạo gspread client từ creds.json."""
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    # Tìm creds.json
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    creds_path = os.path.join(base, "config", "creds.json")
    if not os.path.exists(creds_path):
        raise FileNotFoundError(f"Không tìm thấy creds.json: {creds_path}")

    creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
    return gspread.authorize(creds)


def read_voice_map(client=None) -> Dict[str, str]:
    """Đọc tab "THÔNG TIN" → {folder_name: voice_id}.
    
    VD: {"KA2-T2": "RILOU7YmBhvwJGDGjNmP", ...}
    """
    if client is None:
        client = _get_client()

    name, tab_info, _ = _sheet_cfg()
    ws = client.open(name).worksheet(tab_info)
    rows = ws.get_all_values()

    voice_map = {}
    for row in rows[1:]:  # skip header
        if len(row) <= 4:
            continue
        folder_name = (row[1] or "").strip()  # Cột B (idx 1)
        voice_id = (row[4] or "").strip()     # Cột E (idx 4)
        if folder_name and voice_id and len(voice_id) > 10:
            voice_map[folder_name] = voice_id

    log.info(f"Sheet THÔNG TIN: {len(voice_map)} voice mappings")
    return voice_map


def read_folder_map(client=None) -> Dict[str, str]:
    """Đọc tab "INPUT" → {code: folder_name}.
    
    VD: {"KA5-0001": "KA5-T2", "KA2-0001": "KA2-T2", ...}
    """
    if client is None:
        client = _get_client()

    name, _, tab_input = _sheet_cfg()
    ws = client.open(name).worksheet(tab_input)
    rows = ws.get_all_values()

    folder_map = {}
    for row in rows[1:]:  # skip header
        if len(row) <= 34:
            continue
        code = (row[0] or "").strip()       # Cột A (idx 0) = mã
        folder = (row[34] or "").strip()    # Cột AI (idx 34) = folder
        if code and folder:
            folder_map[code] = folder

    log.info(f"Sheet INPUT: {len(folder_map)} folder mappings")
    return folder_map


def read_all(force=False) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Đọc cả 2 tab, có cache 5 phút.
    
    Returns: (voice_map, folder_map)
    """
    now = time.time()
    if not force and (now - _cache["last_update"]) < _CACHE_TTL:
        if _cache["voice_map"] and _cache["folder_map"]:
            return _cache["voice_map"], _cache["folder_map"]

    try:
        client = _get_client()
        voice_map = read_voice_map(client)
        folder_map = read_folder_map(client)

        _cache["voice_map"] = voice_map
        _cache["folder_map"] = folder_map
        _cache["last_update"] = now

        return voice_map, folder_map
    except Exception as e:
        log.error(f"Sheet error: {e}")
        # Trả cache cũ nếu có
        if _cache["voice_map"]:
            log.warning("Dùng cache cũ")
            return _cache["voice_map"], _cache["folder_map"]
        raise
