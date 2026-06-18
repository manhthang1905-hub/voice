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

    def is_alive(self, email):
        """Master con song? (refresh token OK). Cache ket qua."""
        if email in self._dead:
            return False
        mw = self._mw.get(email)
        if not mw:
            return False
        try:
            mw.master_token()   # thu refresh
            return True
        except Exception:
            self._dead.add(email)
            try:
                masters_store.add_master(email, "")  # giu cho, danh dead
            except Exception:
                pass
            return False

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

    def mark_bad_workspace(self, ws_id):
        """Danh dau 1 workspace HONG (subscription disabled/ban) -> khong pick lai.

        Mark exhausted o moi master (chi master so huu co ws do trong list, vo hai)."""
        if not ws_id:
            return
        for mw in self._mw.values():
            try:
                mw.mark_exhausted(ws_id)
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
            except Exception:
                self._dead.add(email)
                continue
            if pick:
                ws_id, tok, remaining = pick
                return email, ws_id, tok, remaining
        return None
