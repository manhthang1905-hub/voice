"""Audit log — ghi event production vào file JSONL.

Dùng: 
    from utils.audit_log import audit
    audit("chunk_ok", file="KA5-0068.txt", chunk=3, email="abc@hotmail.com")
    
Output: logs/audit_20260410.jsonl
"""
import os
import json
import time
from datetime import datetime

_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs")


def audit(event: str, **data):
    """Ghi 1 event vào audit log."""
    os.makedirs(_LOG_DIR, exist_ok=True)
    
    fname = f"audit_{datetime.now().strftime('%Y%m%d')}.jsonl"
    fpath = os.path.join(_LOG_DIR, fname)
    
    entry = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
    }
    entry.update(data)
    
    try:
        with open(fpath, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
