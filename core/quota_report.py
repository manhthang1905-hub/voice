"""
quota_report.py — Bao cao & quan ly tai nguyen quota cho ca doi 1500 TK.

Moi TK free: 10.000 ky tu/thang, reset theo ngay rieng. Chay tool hang ngay ->
can biet: con bao nhieu, bao nhieu TK reset ngay nao, moi ngay khai thac duoc bao nhieu.

Dung sau khi 'Kiem tra song/chet' (quet quota that + luu next_reset_unix vao roster).
"""
import time
import collections

CAP_PER_ACC = 10000   # quota/TK/thang (free tier)


def _load_roster():
    from core.mode_b_accounts import load_accounts   # da auto-reset khi qua han
    return load_accounts()


def fleet_report(accounts=None):
    """Tong hop tinh hinh quota toan doi. -> dict de hien thi."""
    accs = accounts if accounts is not None else _load_roster()
    now = time.time()

    usable = [a for a in accs if a.get("status") in ("alive", "exhausted", "pending", "unknown")]
    dead = [a for a in accs if a.get("status") in ("dead", "flagged")]

    total_remaining = sum(int(a.get("chars_remaining", 0) or 0) for a in usable)
    capacity = len(usable) * CAP_PER_ACC

    alive = [a for a in usable if (a.get("chars_remaining", 0) or 0) > 0]
    exhausted = [a for a in usable if (a.get("chars_remaining", 0) or 0) <= 0]

    # Lich reset: dem TK reset trong N ngay toi (theo next_reset_unix)
    by_day = collections.Counter()
    have_reset = 0
    for a in usable:
        r = int(a.get("next_reset_unix", 0) or 0)
        if r > now:
            have_reset += 1
            d = int((r - now) // 86400)
            by_day[d] += 1

    # Uoc tinh dong reset moi ngay: neu reset trai deu -> capacity/30 ky tu/ngay
    daily_inflow_chars = capacity / 30.0            # ky tu/ngay ben vung
    reset_next7 = sum(by_day.get(d, 0) for d in range(0, 7))

    return {
        "total_accounts": len(accs),
        "usable_accounts": len(usable),
        "dead_accounts": len(dead),
        "alive_now": len(alive),                    # con quota dung ngay
        "exhausted_now": len(exhausted),            # het quota, cho reset
        "total_remaining": total_remaining,         # ky tu dung duoc NGAY BAY GIO
        "capacity": capacity,                       # tran thang
        "have_reset_date": have_reset,              # so TK da biet ngay reset
        "reset_by_day": dict(sorted(by_day.items())),
        "reset_next7": reset_next7,
        "daily_sustainable_chars": int(daily_inflow_chars),
    }


def format_report(rep):
    """Bao cao dang text de hien thi/log."""
    def k(n):
        return f"{int(n):,}"
    lines = []
    lines.append(f"📊 QUOTA TOAN DOI ({rep['usable_accounts']} TK dung duoc, "
                 f"{rep['dead_accounts']} chet)")
    lines.append(f"  • Dung duoc NGAY BAY GIO: {k(rep['total_remaining'])} ky tu "
                 f"({rep['alive_now']} TK con quota)")
    lines.append(f"  • Het quota cho reset:    {rep['exhausted_now']} TK")
    lines.append(f"  • Tran thang toi da:      {k(rep['capacity'])} ky tu")
    lines.append(f"  • Ben vung ~{k(rep['daily_sustainable_chars'])} ky tu/ngay "
                 f"(= tran/30, dung qua muc nay se can dan)")
    if rep["have_reset_date"] == 0:
        lines.append("  ⚠ CHUA BIET ngay reset TK nao -> bam 'Kiem tra song/chet' "
                     "de quet quota + luu ngay reset.")
    else:
        nxt = rep["reset_by_day"]
        near = ", ".join(f"{'homnay' if d==0 else f'+{d}d'}:{n}TK"
                         for d, n in list(nxt.items())[:7])
        lines.append(f"  • Reset 7 ngay toi: {rep['reset_next7']} TK  ({near})")
    return "\n".join(lines)
