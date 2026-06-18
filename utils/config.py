"""
Config manager - Doc/ghi settings tu JSON file.
Luu tat ca cai dat ung dung: output dir, default model, quality, proxy, etc.
"""

import json
import os
from typing import Any

# Duong dan mac dinh
PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")

# Cai dat mac dinh
DEFAULT_CONFIG = {
    "output_dir": os.path.join(PROJECT_ROOT, "output"),
    "default_model": "eleven_v3",

    "output_format": "mp3_44100_128",
    "max_chunk_size": 5000,       # So ky tu toi da moi chunk
    "voice_stability": 1.0,
    "voice_similarity_boost": 1.0,
    "auto_rotate_accounts": True,  # Tu dong chuyen account khi het token
    "proxy_static": {
        "file": "",                         # File proxy tinh: host:port:user:pass
        "accounts_per_ip": 5,
    },
    "proxy_4g": {
        "enabled": False,
        "api_url": "",                      # API 4G modem
        "key": "",                          # Key 4G
    },
    "api_base_url": "https://api.us.elevenlabs.io",
    "max_retries": 3,
    "retry_delay": 3.0,
    "request_delay": 3.0,          # + random 0~5s = 3~8s tong
    "accounts_per_ip": 5,
    "max_threads": 3,              # So luong song song (giong nThread)
    # Cai dat nang cao (giong DgtAutoTTS)
    "break_between_paragraphs": True,
    "break_paragraph_seconds": 0.5,
    "break_paragraph_gap": 0,       # Cach nhau X doan (0 = moi doan)
    "break_by_char": True,
    "break_chars": [
        {"char": ",", "seconds": 0.3},
        {"char": ".", "seconds": 0.5},
    ],
    "remove_special_chars": True,
    "max_chars_per_line": 5000,     # Max an toan. 2 chunks x 5000 = 10000 = het credit 1 TK
    "auto_delete_temp": True,       # Xoa file tam sau khi ghep xong
    "auto_create_srt": True,        # Tu dong tao SRT phu de
    "srt_model": "base",            # Whisper model: tiny/base/small/medium
    "srt_language": None,           # None = tu dong phat hien
}


class Config:
    """Quan ly cai dat ung dung."""

    def __init__(self, config_file: str = None):
        self._file = config_file or CONFIG_FILE
        self._data = {}
        self._load()

    def _load(self):
        """Doc config tu file. Tao file mac dinh neu chua co."""
        os.makedirs(os.path.dirname(self._file), exist_ok=True)

        if os.path.exists(self._file):
            with open(self._file, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            # Them cac key moi tu DEFAULT_CONFIG neu thieu
            updated = False
            for key, value in DEFAULT_CONFIG.items():
                if key not in self._data:
                    self._data[key] = value
                    updated = True
            if updated:
                self._save()
        else:
            self._data = DEFAULT_CONFIG.copy()
            self._save()

    def _save(self):
        """Ghi config ra file."""
        with open(self._file, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def get(self, key: str, default: Any = None) -> Any:
        """Lay gia tri config theo key. Ho tro nested key voi dau cham.

        Vi du: config.get("proxy.host") → lay self._data["proxy"]["host"]
        """
        keys = key.split(".")
        value = self._data
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    def set(self, key: str, value: Any):
        """Dat gia tri config. Ho tro nested key voi dau cham."""
        keys = key.split(".")
        data = self._data
        for k in keys[:-1]:
            if k not in data or not isinstance(data[k], dict):
                data[k] = {}
            data = data[k]
        data[keys[-1]] = value
        self._save()

    @property
    def data(self) -> dict:
        """Tra ve toan bo config data (read-only copy)."""
        return self._data.copy()
