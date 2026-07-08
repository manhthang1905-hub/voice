"""
master_pool.py — Quan ly NHIEU master (resilience + chia tai).

- Moi account gan 1 master (master_email). Lien ket chia deu cho cac master song.
- Generate: gop workspace cua tat ca master, pick cai con quota -> dung token master tuong ung.
- Master die (refresh hong) -> danh dau dead; account cua no se duoc gan lai master khac.
"""
import time
from core import masters_store
from core.master_workspace import MasterWorkspace


class MasterPool:
    def __init__(self):
        self._mw = {}          # email -> MasterWorkspace
        self._dead = set()     # email master da chet (refresh fail)
        self._reload()

    def _reload(self):
        self._mw = {}
        for m in masters_store.list_masters():
            if m.get("status", "active") != "active":
                continue
            email = m.get("email") or ""
            rt = (m.get("refresh_token") or "").strip()
            if rt:
                self._mw[email] = MasterWorkspace(refresh_token=rt, email=email)

    def emails(self):
        return [e for e in self._mw if e not in self._dead]

    def get(self, email):
        return self._mw.get(email)

    @staticmethod
    def _classify_death(err):
        """Phan loai master chet: 'suspended' | 'expired' | None (loi mang tam thoi).

        - suspended: bi ElevenLabs KHOA vinh vien (vi pham policy) -> KHONG cuu duoc,
          phai chuyen TK sang master khac.
        - expired: refresh token het han -> dang nhap lai duoc.
        - None: loi mang/tam thoi -> chi bo qua phien nay.
        """
        s = str(err).lower()
        if any(k in s for k in (
                "suspended", "prohibited use", "account_suspended",
                "has been suspended", "banned", "terminated")):
            return "suspended"
        if any(k in s for k in (
                "token_expired", "invalid_refresh_token", "invalid_grant",
                "user_disabled", "user_not_found", "credential",
                "invalid_argument", "subscription", "disabled")):
            return "expired"
        return None

    def _handle_master_error(self, email, err):
        """Master loi khi refresh/next_workspace/generate -> phan loai + persist."""
        self._dead.add(email)
        kind = self._classify_death(err)
        if kind:
            # Chet that -> luu vao file (status suspended/expired), phien sau tu bo qua
            # + BI LOAI khoi live -> TK cua no tu re-link sang master song khac.
            try:
                masters_store.mark_expired(email, str(err)[:150], status=kind)
            except Exception:
                pass

    def is_alive(self, email):
        """Master con song? Refresh token OK VA account chua bi suspend.

        Refresh thanh cong VAN co the bi suspend (khoa) -> check them subscription
        de bat suspension (loi 'account suspended' khi goi API).
        """
        if email in self._dead:
            return False
        mw = self._mw.get(email)
        if not mw:
            return False
        try:
            mw.master_token()   # thu refresh
        except Exception as e:
            self._handle_master_error(email, e)
            return False
        # Refresh OK -> check suspension bang 1 call nhe (list workspaces).
        try:
            mw.list_workspaces()
            return True
        except Exception as e:
            if self._classify_death(e) == "suspended":
                self._handle_master_error(email, e)
                return False
            # Loi khac (mang/quyen) -> coi nhu con song (refresh da OK)
            return True

    def live_masters(self):
        """Danh sach (email, MasterWorkspace) con song."""
        out = []
        for email, mw in self._mw.items():
            if email in self._dead:
                continue
            out.append((email, mw))
        return out

    def pick_master_round_robin(self, idx):
        """Chon master theo round-robin (cho phan phoi khi onboard)."""
        live = self.live_masters()
        if not live:
            return None, None
        return live[idx % len(live)]

    def total_ready(self):
        """Tong so token SAN SANG dung ngay tren tat ca master song."""
        n = 0
        for email, mw in self._mw.items():
            if email in self._dead:
                continue
            try:
                n += mw.ready_count()
            except Exception:
                pass
        return n

    def warm(self, target_ready=40):
        """CHUAN BI TRUOC: nap san token cho toi khi co >= target_ready token san sang
        (build pool cua tung master song, dung khi du). -> so token san sang.

        Goi nen luc mo tool + dinh ky -> khi Auto Convert can token la co NGAY,
        khong phai doi build pool (~50s) giua chung.
        """
        total = 0
        for email, mw in list(self._mw.items()):
            if email in self._dead:
                continue
            try:
                mw.ensure_warm(min_ready=12)
                total += mw.ready_count()
            except Exception as e:
                self._handle_master_error(email, e)
            if total >= target_ready:
                break
        return total

    def mark_bad_workspace(self, ws_id):
        """TK het quota (TAM THOI) -> bo qua den luot rebuild (reset xong quay lai)."""
        if not ws_id:
            return
        for mw in self._mw.values():
            try:
                mw.mark_exhausted(ws_id)
            except Exception:
                pass

    def mark_disabled_workspace(self, ws_id):
        """TK bi vo hieu hoa/ban (VINH VIEN) -> khong bao gio dung lai."""
        if not ws_id:
            return
        for mw in self._mw.values():
            try:
                mw.mark_disabled(ws_id)
            except Exception:
                pass

    def next_workspace(self, need_chars=500, prefer_master=None):
        """Tim 1 workspace con quota tu BAT KY master nao (uu tien prefer_master).

        -> (master_email, workspace_id, scoped_token, remaining) | None
        """
        order = list(self._mw.items())
        if prefer_master and prefer_master in self._mw:
            order.sort(key=lambda kv: 0 if kv[0] == prefer_master else 1)
        for email, mw in order:
            if email in self._dead:
                continue
            try:
                pick = mw.next_workspace(need_chars=need_chars)
            except Exception as e:
                self._handle_master_error(email, e)
                continue
            if pick:
                ws_id, tok, remaining = pick
                return email, ws_id, tok, remaining
        return None


# ============================================================
# SHARED POOL (dung chung 1 pool cho MOI batch Auto Convert)
# ============================================================
# Truoc day moi VoiceWorker/batch tao 1 MasterPool moi -> build_pool lai (~2 phut
# probe 300+ workspace) cho TUNG channel -> phi hang gio. Dung chung 1 pool: build
# 1 lan, cac batch sau tra token tuc thi (token + quota da cache trong pool).
_SHARED_POOL = None


def get_shared_pool():
    """Tra ve MasterPool dung chung (tao lan dau, tai dung ve sau)."""
    global _SHARED_POOL
    if _SHARED_POOL is None:
        _SHARED_POOL = MasterPool()
    return _SHARED_POOL


def reset_shared_pool():
    """Xoa pool dung chung -> lan sau tao moi (goi khi them/doi master)."""
    global _SHARED_POOL
    _SHARED_POOL = None


def shared_pool_ready():
    """So token san sang cua pool chung (KHONG tao neu chua co). -1 = chua tao."""
    return _SHARED_POOL.total_ready() if _SHARED_POOL is not None else -1
