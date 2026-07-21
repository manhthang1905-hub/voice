"""
maintenance.py — Tu quan tri tai nguyen cho tool chay 24/7.

3 viec dinh ky (chay nen, khong tat tool):
  1. Quet quota + luu ngay reset  -> pool EDF + bao cao chinh xac.
  2. Canh bao can nguon           -> dung qua muc ben vung thi bao.
  3. (tuy chon) Re-link master     -> gom TK pending/mo coi ve master song.

Goi tu MaintenanceWorker (QThread) theo QTimer trong GUI.
"""
import os
import json
import time
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAP_PER_ACC = 10000


def todays_usage_chars():
    """Tong ky tu da generate HOM NAY (tu audit log chunk_ok)."""
    fpath = os.path.join(PROJECT_ROOT, "logs",
                         f"audit_{datetime.now().strftime('%Y%m%d')}.jsonl")
    if not os.path.exists(fpath):
        return 0
    total = 0
    try:
        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or '"chunk_ok"' not in line:
                    continue
                try:
                    e = json.loads(line)
                    if e.get("event") == "chunk_ok":
                        total += int(e.get("chars", 0) or 0)
                except Exception:
                    pass
    except Exception:
        pass
    return total


def scan_quota(on_log=lambda *_: None, should_stop=lambda: False,
               max_workers=10):
    """Quet quota THAT cua moi TK (qua api_key) + luu chars_remaining + ngay reset
    vao roster. -> so TK da cap nhat. (Doc-only voi ElevenLabs, khong ton credit.)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from core.convert import check_quota
    from core.mode_b_accounts import load_accounts, set_remaining

    accounts = [a for a in load_accounts()
                if (a.get("api_key") or "").strip()
                and a.get("status") not in ("dead", "flagged")]
    if not accounts:
        return 0
    on_log(f"[Maintenance] Quet quota {len(accounts)} TK...")

    def _one(acc):
        try:
            q = check_quota(acc["api_key"], proxy=None)
            if not q:
                return acc["email"], None, 0
            return acc["email"], q["chars_remaining"], q.get("next_reset_unix", 0)
        except Exception:
            return acc["email"], None, 0

    updated = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_one, a) for a in accounts]
        for f in as_completed(futs):
            if should_stop():
                break
            email, remaining, reset = f.result()
            if remaining is not None:
                set_remaining(email, remaining, reset)
                updated += 1
    on_log(f"[Maintenance] Da cap nhat quota {updated} TK")
    return updated


def depletion_note(report, used_today):
    """Sinh canh bao can nguon (neu co). -> str (rong neu on)."""
    remaining = report.get("total_remaining", 0)
    sustainable = report.get("daily_sustainable_chars", 1) or 1
    alive = report.get("alive_now", 0)
    reset7 = report.get("reset_next7", 0)

    notes = []
    # Con lai du bao nhieu ngay o muc dung hom nay
    if used_today > 0:
        runway_days = remaining / used_today
        if runway_days < 1.5:
            notes.append(
                f"⚠ CAN NGUON: con {remaining:,} ky tu, hom nay da dung "
                f"{used_today:,} -> chi con ~{runway_days:.1f} ngay. "
                f"Nen 'Lien ket Master' de mo them TK, hoac giam tai.")
        elif used_today > sustainable * 1.5:
            notes.append(
                f"⚠ Dung {used_today:,}/ngay > muc ben vung {sustainable:,} "
                f"-> se can dan. Buffer con ~{runway_days:.0f} ngay.")
    if alive < 50:
        notes.append(f"⚠ Chi con {alive} TK con quota + {reset7} TK reset trong 7 ngay. "
                     f"Nen nap them TK / master.")
    return "  |  ".join(notes)


def run_maintenance(on_log=lambda *_: None, should_stop=lambda: False,
                    do_relink=False, do_health_check=False):
    """1 chu ky bao tri: quet quota -> bao cao -> canh bao -> (tuy chon) re-link.
    -> dict ket qua.

    do_health_check=True: kiem tra suc khoe master + loai master chet (AUTO).
    """
    from core.quota_report import fleet_report, format_report

    # 1. Health-check master (neu bat)
    hc_result = None
    if do_health_check:
        try:
            from core.master_pool import get_shared_pool
            pool = get_shared_pool()
            on_log("[Maintenance] Health-check master...")
            hc_result = pool.health_check_all(on_log=on_log)
            if hc_result["dead"]:
                on_log(f"[Maintenance] {len(hc_result['dead'])} master CHET, loai bo...")
                pool.auto_remove_dead_masters()
        except Exception as e:
            on_log(f"[Maintenance] health-check loi: {str(e)[:80]}")

    # 2. Quet quota
    scan_quota(on_log=on_log, should_stop=should_stop)
    rep = fleet_report()
    used = todays_usage_chars()
    on_log(format_report(rep))
    on_log(f"[Maintenance] Hom nay da dung: {used:,} ky tu")
    warn = depletion_note(rep, used)
    if warn:
        on_log(warn)

    # 3. Re-link (neu bat)
    relink = None
    if do_relink and not should_stop():
        # Chi re-link khi co TK chua san sang (pending/mo coi) de tranh chay vo ich
        try:
            from core.mode_b_accounts import load_accounts
            from core.masters_store import list_masters
            live = {m["email"] for m in list_masters()
                    if m.get("status", "active") == "active"}
            need = sum(1 for a in load_accounts()
                       if a.get("status") != "dead"
                       and (not a.get("master_ready")
                            or a.get("master_email") not in live))
            if need > 0:
                on_log(f"[Maintenance] {need} TK chua san sang -> tu Lien ket Master...")
                from tools.sync_accounts import sync
                relink = sync(log=on_log)
            else:
                on_log("[Maintenance] Tat ca TK da lien ket, khoi re-link.")
        except Exception as e:
            on_log(f"[Maintenance] re-link loi: {str(e)[:80]}")

    return {"report": rep, "used_today": used, "warn": warn,
            "relink": relink, "health_check": hc_result}
