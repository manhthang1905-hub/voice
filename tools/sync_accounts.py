"""
sync_accounts.py — "Sync" account moi: dang nhap tung worker va moi master
vao workspace cua no (de master generate khong bi flag).

Chay sau khi nhap account moi vao tool:
    python tools/sync_accounts.py

Doc roster config/1000tk_real_status.json (email + password), voi moi worker
chua onboard thi login + invite master. Danh dau 'master_onboarded' de lan sau
bo qua. In ket qua.
"""

import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.master_workspace import MasterWorkspace  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROSTER = os.path.join(PROJECT_ROOT, "config", "1000tk_real_status.json")


def _load():
    with open(ROSTER, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    tmp = ROSTER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, ROSTER)


def _accept_and_mark_ready(accounts, pool, masters, proxy, with_quota, log):
    """Tung master accept invite -> cap nhat master_ready cho cac account.

    with_quota=True thi lay them chars_remaining (cuoi cung). -> (ready, alive).
    """
    for em, m in masters:
        try:
            m.accept_all_pending(log=lambda s: None)
        except Exception:
            pass
    # member set theo tung master
    members = {}
    for em, m in masters:
        try:
            members[em] = m.member_workspace_ids()
        except Exception:
            members[em] = set()
    from core.convert import check_quota
    ready = alive = 0
    for acc in accounts:
        wsid = (acc.get("workspace_id") or "").strip()
        me = acc.get("master_email")
        is_ready = bool(wsid) and me in members and wsid in members[me]
        acc["master_ready"] = is_ready
        if not is_ready:
            if acc.get("status") not in ("dead",):
                acc["status"] = "pending"
            continue
        ready += 1
        if with_quota:
            api = (acc.get("api_key") or "").strip()
            chars = None
            if api:
                try:
                    q = check_quota(api, proxy=proxy)
                    chars = q.get("chars_remaining") if q else None
                except Exception:
                    pass
            if chars is not None:
                acc["chars_remaining"] = chars
                acc["status"] = "alive" if chars > 0 else "exhausted"
                if chars > 0:
                    alive += 1
            else:
                acc["status"] = "alive"; alive += 1
        else:
            acc["status"] = "alive"; alive += 1
    return ready, alive


def sync(force=False, proxy=None, log=print, batch=50, on_progress=None):
    """Onboard worker: CHIA DEU cho nhieu master + accept theo dot. -> thong ke.

    on_progress(done, total, email, counts_text): callback bao tien do truc quan
    (thanh tien do + dem OK/dead/fail) -> GUI hien ro dang chay den dau.
    """
    if not os.path.exists(ROSTER):
        log("Khong thay roster.")
        return {"invited": 0, "already": 0, "fail": 0, "skip": 0}

    data = _load()
    accounts = data.get("accounts", [])

    from core.master_pool import MasterPool
    pool = MasterPool()
    masters = pool.live_masters()        # [(email, MasterWorkspace)]
    if not masters:
        log("Khong co master song! Them master (nut 'Them Master') truoc.")
        return {"invited": 0, "already": 0, "fail": 0, "skip": 0, "dead": 0}
    live_emails = [e for e, _ in masters]
    log(f"{len(masters)} master song: {live_emails} | {len(accounts)} account")

    # Dem TONG so TK CAN xu ly (de hien X/Y truc quan)
    def _is_candidate(acc):
        em = (acc.get("email") or "").strip()
        has = ((acc.get("password") or "").strip()
               or (acc.get("login_refresh_token") or "").strip())
        if not em or not has:
            return False
        if acc.get("status") == "dead" and not force:
            return False
        me = acc.get("master_email")
        orphan = bool(me) and me not in live_emails
        if acc.get("master_onboarded") and not force and not orphan:
            return False
        return True
    total_todo = sum(1 for a in accounts if _is_candidate(a))
    already_done = sum(1 for a in accounts if a.get("master_onboarded")) \
        if not force else 0
    log(f"Can lien ket: {total_todo} TK (da xong tu truoc: {already_done})")

    # XAC DINH master nao CON NHAN duoc TK moi.
    # ElevenLabs chan: 1 account free chi duoc o 1 workspace free -> master da co
    # nhieu workspace se bi loi 'cannot_join_free_workspace' khi accept.
    # Do bang cach: thu accept 1 invite dang cho cua moi master; neu 403 cannot_join
    # => master do BI CHAN (khong nhan TK moi duoc nua).
    blocked = set()
    for em, mw in masters:
        try:
            pend = mw.pending_invites()
        except Exception:
            pend = []
        if not pend:
            continue
        try:
            ok, st = mw.accept_invite(pend[0].get("invite_code"))
            if (not ok) and "cannot_join_free_workspace" in st:
                blocked.add(em)
        except Exception:
            pass
    open_masters = [e for e in live_emails if e not in blocked]
    if blocked:
        log("⚠ Master BỊ CHẶN nhận thêm (ElevenLabs: 1 free account chỉ 1 workspace): "
            + ", ".join(b.split("@")[0] for b in blocked))
    log("Master còn nhận TK mới: "
        + (", ".join(e.split("@")[0] for e in open_masters) or "KHÔNG CÒN"))

    _rr_open = [0]

    def _pick_open_master():
        """Round-robin trong cac master con nhan duoc. -> email | None."""
        if not open_masters:
            return None
        m = open_masters[_rr_open[0] % len(open_masters)]
        _rr_open[0] += 1
        return m

    # 4G login + rotate khi QUOTA per-IP
    p4g = None
    if proxy is None:
        try:
            from accounts.proxy import Proxy4G
            p4g = Proxy4G()
            proxy = p4g.get_for_requests()
            log(f"Login qua 4G: {proxy.get('https','') if proxy else 'direct'}")
        except Exception as e:
            log(f"Khong co 4G ({str(e)[:40]}) -> login truc tiep")

    def _rotate_4g():
        if not p4g:
            return None
        try:
            p4g.rotate(wait=20); return p4g.get_ip()
        except Exception:
            return None

    stats = {"invited": 0, "already": 0, "fail": 0, "skip": 0, "dead": 0,
             "no_room": 0}
    rr = 0  # round-robin index
    processed = 0
    for acc in accounts:
        email = (acc.get("email") or "").strip()
        pw = (acc.get("password") or "").strip()
        rt = (acc.get("login_refresh_token") or "").strip()
        if not email or (not pw and not rt):
            stats["skip"] += 1
            continue
        if acc.get("status") == "dead" and not force:
            stats["skip"] += 1
            continue

        # Da READY (master da accept) -> xong, bo qua.
        if acc.get("master_ready") and not force:
            stats["skip"] += 1
            continue

        # GAN MASTER: doi master neu chua co / master chet / master BI CHAN.
        cleanup = []
        assigned = acc.get("master_email")
        if assigned and assigned not in live_emails:
            cleanup = [assigned]   # master chet -> xoa khoi workspace
        need_new = (not assigned) or (assigned not in live_emails) or (assigned in blocked)
        if need_new:
            newm = _pick_open_master()
            if not newm:
                # Khong con master nao nhan duoc -> can them master (moi/paid)
                stats["no_room"] += 1
                continue
            if newm != assigned:
                if assigned:
                    log(f"  CHUYEN {email}: {assigned.split('@')[0]} (chặn/chết)"
                        f" -> {newm.split('@')[0]}")
                acc["master_email"] = newm
                acc["master_onboarded"] = False
                acc["master_ready"] = False
            assigned = newm

        # RESUME: da login+invite roi (master con nhan duoc) -> de accept xu ly, khong re-login.
        if acc.get("master_onboarded") and not force:
            stats["skip"] += 1
            continue
        mw = pool.get(assigned)

        ok, msg, new_rt, ws_id = mw.onboard_worker(
            email, password=pw, refresh_token=rt, proxy=proxy,
            on_log=lambda m: log("    " + m), on_quota=_rotate_4g,
            cleanup_emails=cleanup)
        if new_rt:
            acc["login_refresh_token"] = new_rt
        if ws_id:
            acc["workspace_id"] = ws_id
        statusword = "OK"
        if ok:
            acc["master_onboarded"] = True
            acc.pop("onboard_error", None)
            stats["already" if msg in ("already_member", "already_invited") else "invited"] += 1
        else:
            acc["master_onboarded"] = False
            acc["onboard_error"] = msg
            if msg == "login_fail":
                acc["status"] = "dead"; stats["dead"] += 1; statusword = "DEAD (sai pass)"
            else:
                stats["fail"] += 1; statusword = f"FAIL ({msg[:30]})"
        processed += 1
        # BAO TIEN DO TRUC QUAN: X/Y + dem OK/dead/fail + TK hien tai
        if on_progress:
            okc = stats["invited"] + stats["already"]
            counts = (f"✅OK:{okc}  ☠dead:{stats['dead']}  ⚠fail:{stats['fail']}"
                      f"   |   {statusword}")
            on_progress(processed, total_todo, email, counts)
        # ACCEPT THEO DOT: moi `batch` account -> accept + mark ready (dung duoc DAN)
        if processed % batch == 0:
            r, a = _accept_and_mark_ready(accounts, pool, masters, proxy, False, log)
            _save(data)
            log(f"  >>> {processed}/{total_todo} da onboard | SAN SANG dung duoc: {a}")
        elif processed % 10 == 0:
            _save(data)
        time.sleep(0.3)

    # CUOI: accept + readiness + quota day du
    ready, alive = _accept_and_mark_ready(accounts, pool, masters, proxy, True, log)
    _save(data)
    log(f"XONG: invited={stats['invited']} already={stats['already']} "
        f"fail={stats['fail']} dead={stats['dead']} | READY={ready} ALIVE={alive}")
    if stats["no_room"]:
        log(f"⛔ {stats['no_room']} TK chưa link được: TẤT CẢ master free đang bị "
            f"ElevenLabs chặn ('1 free account = 1 free workspace'). "
            f"Cần master CÒN nhận được (master mới chưa dùng, hoặc master PAID).")
    stats.update({"ready": ready, "alive": alive, "accepted": stats["invited"]})
    return stats


if __name__ == "__main__":
    force = "--force" in sys.argv
    sync(force=force)
