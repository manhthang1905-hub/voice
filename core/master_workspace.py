"""
master_workspace.py — Generate TTS qua "master account + workspace".

Co che (da verify that):
  - 1 master account Google sach (config/master_account.json) la thanh vien
    (workspace_lite_member) cua nhieu workspace, moi workspace thuoc 1 worker.
  - Generate bang TOKEN CUA MASTER, sau khi sign-into-workspace cua worker
    -> KHONG bao gio bi `detected_unusual_activity`, va TIEU QUOTA cua worker.
  - Master tu no khong bi tru quota -> scale theo so worker.

Luong:
  1. refresh master id_token (tu refresh_token Firebase)
  2. POST /v1/auth-account/sign-into-workspace {workspace_id}
  3. refresh lai -> token gan workspace do
  4. POST /v1/text-to-speech/...  (Bearer master)  -> ra voice

Dung:
    from core.master_workspace import MasterWorkspace
    mw = MasterWorkspace()
    ws_id, token, remaining = mw.next_workspace(need_chars=2000)
    # token la Bearer da gan workspace -> dung de goi TTS nhu binh thuong
"""

import os
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from core.api_client import firebase_refresh, generate_fingerprint

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER_CFG = os.path.join(PROJECT_ROOT, "config", "master_account.json")
API_BASE = "https://api.us.elevenlabs.io"


def _load_master_cfg():
    with open(MASTER_CFG, "r", encoding="utf-8") as f:
        return json.load(f)


class MasterWorkspace:
    """Quan ly master account + cac workspace de generate khong bi flag."""

    def __init__(self, cfg_path: str = MASTER_CFG,
                 refresh_token: str = None, email: str = ""):
        if refresh_token:
            # Dung truc tiep 1 master cu the (cho pool nhieu master)
            self.cfg = {"refresh_token": refresh_token, "email": email}
            self.refresh_token = refresh_token
            self.email = email
        else:
            self.cfg = _load_master_cfg() if cfg_path == MASTER_CFG \
                else json.load(open(cfg_path, encoding="utf-8"))
            self.refresh_token = self.cfg["refresh_token"]
            self.email = self.cfg.get("email", "")
        self.session = requests.Session()
        self.session.trust_env = False   # bo qua proxy he thong (tranh dut mang)

        self._master_tok = None          # id_token "goc" cua master
        self._master_exp = 0
        self._ws_tokens = {}             # ws_id -> (token, exp)
        self._exhausted = set()          # ws_id het quota (TAM: reset se quay lai)
        self._disabled = set()           # ws_id bi ban/vo hieu hoa (VINH VIEN)
        # Pool workspace (probe SONG SONG + cache) -> next_workspace nhanh gap ~14x.
        # Moi phan tu: {"workspace_id","remaining","token"} ; sort remaining giam dan.
        self._pool = None
        self._pool_time = 0
        self._probed = set()      # ws da probe (khoi probe lai o lan build sau)
        self._pool_lock = threading.Lock()

    # ---------- token ----------
    def _headers(self, token: str) -> dict:
        h = generate_fingerprint(0)
        h["sec-fetch-site"] = "same-site"
        h["Content-Type"] = "application/json"
        h["Authorization"] = f"Bearer {token}"
        return h

    def master_token(self) -> str:
        """id_token master con han (>5 phut)."""
        if self._master_tok and self._master_exp > time.time() + 300:
            return self._master_tok
        res = firebase_refresh(self.refresh_token, proxy=None)
        self._master_tok = res["id_token"]
        self._master_exp = time.time() + int(res.get("expires_in", 3600))
        # Firebase co the tra refresh_token MOI (xoay token). Luu lai -> master khong
        # bi chet dan vi token cu (nhu paloukite). Chi ghi khi token thuc su doi.
        new_rt = (res.get("refresh_token") or "").strip()
        if new_rt and new_rt != self.refresh_token:
            self.refresh_token = new_rt
            if self.email:
                try:
                    from core import masters_store
                    masters_store.update_refresh_token(self.email, new_rt)
                except Exception:
                    pass
        return self._master_tok

    # ---------- workspaces ----------
    def list_workspaces(self) -> list:
        """Danh sach workspace master tham gia: [{workspace_id, seat_type, num_members}]."""
        tok = self.master_token()
        r = self.session.get(
            f"{API_BASE}/v1/auth-account/workspace-users",
            headers=self._headers(tok), timeout=20)
        r.raise_for_status()
        return r.json()

    def member_workspace_ids(self) -> set:
        """Tap workspace_id ma master DANG la member (de check account ready)."""
        return set(w.get("workspace_id") for w in self.list_workspaces())

    def sign_into(self, workspace_id: str) -> str:
        """Sign master vao 1 workspace -> tra ve token MASTER da gan workspace do.

        Cache token theo workspace (con han thi tai dung).
        """
        cached = self._ws_tokens.get(workspace_id)
        if cached and cached[1] > time.time() + 300:
            return cached[0]

        tok = self.master_token()
        r = self.session.post(
            f"{API_BASE}/v1/auth-account/sign-into-workspace",
            headers=self._headers(tok), json={"workspace_id": workspace_id},
            timeout=25)
        if r.status_code != 200:
            raise RuntimeError(
                f"sign-into-workspace {workspace_id[:12]} fail "
                f"{r.status_code}: {r.text[:120]}")

        # refresh lai de token gan workspace moi
        res = firebase_refresh(self.refresh_token, proxy=None)
        ws_tok = res["id_token"]
        exp = time.time() + int(res.get("expires_in", 3600))
        self._ws_tokens[workspace_id] = (ws_tok, exp)
        return ws_tok

    def workspace_quota(self, scoped_token: str) -> dict:
        """Quota cua workspace ma token dang gan: {used, limit, remaining, reset_unix}."""
        r = self.session.get(
            f"{API_BASE}/v1/user/subscription",
            headers=self._headers(scoped_token), timeout=20)
        r.raise_for_status()
        s = r.json()
        used = s.get("character_count", 0) or 0
        limit = s.get("character_limit", 0) or 0
        return {
            "used": used,
            "limit": limit,
            "remaining": max(0, limit - used),
            "reset_unix": s.get("next_character_count_reset_unix", 0),
        }

    def workspace_pool(self, refresh: bool = False) -> list:
        """Tra ve [{workspace_id, remaining, limit}] cho moi workspace (co quota)."""
        pool = []
        for w in self.list_workspaces():
            ws = w.get("workspace_id")
            if not ws or ws in self._exhausted:
                continue
            try:
                tok = self.sign_into(ws)
                q = self.workspace_quota(tok)
                pool.append({"workspace_id": ws, **q})
            except Exception:
                continue
        pool.sort(key=lambda x: -x["remaining"])
        return pool

    def _probe_ws(self, ws: str):
        """Sign in + doc quota 1 workspace -> dict (kem token + ngay reset) hoac None."""
        try:
            tok = self.sign_into(ws)
            q = self.workspace_quota(tok)
            return {"workspace_id": ws, "remaining": q["remaining"], "token": tok,
                    "reset_unix": q.get("reset_unix", 0) or 0}
        except Exception:
            return None

    @staticmethod
    def _edf_key(entry):
        """Sort key EDF (Earliest-Deadline-First): TK reset SOM NHAT truoc -> dung
        quota truoc khi bi mat luc reset. reset_unix=0 (chua biet) coi la xa nhat.
        Cung ngay reset -> uu tien con nhieu quota hon."""
        r = entry.get("reset_unix", 0) or 0
        return (r if r > 0 else 9_000_000_000_000, -entry.get("remaining", 0))

    def build_pool(self, max_workers: int = 20, enough: int = 30,
                   min_remaining: int = 500) -> list:
        """Probe THEM workspace SONG SONG -> BO SUNG vao pool (sort remaining giam dan).

        Toi uu: DUNG SOM khi tim du `enough` workspace con quota (>= min_remaining)
        thay vi probe HET vai tram ws (~2 phut). Bo qua ws da probe/exhausted -> moi lan
        goi se probe LO MOI (khong lap lai). Token cache trong pool -> tra ve tuc thi.
        """
        # Nap them ws da CHET/disabled tu roster -> bo qua ngay (khoi probe/thu lai).
        # + GIOI HAN theo roster: chi dung workspace MAY NAY so huu (chia nhieu may).
        roster_ids = None
        try:
            from core.mode_b_accounts import dead_workspace_ids, roster_workspace_ids
            self._disabled |= dead_workspace_ids()
            r = roster_workspace_ids()
            if r:                       # co roster -> chi dung ws trong roster
                roster_ids = r
        except Exception:
            pass
        with self._pool_lock:
            if self._pool is None:
                self._pool = []
            skip = set(self._probed) | set(self._exhausted) | set(self._disabled)
        ids = [w.get("workspace_id") for w in self.list_workspaces()
               if w.get("workspace_id") and w.get("workspace_id") not in skip
               and (roster_ids is None or w.get("workspace_id") in roster_ids)]
        found = []
        good = 0
        if ids:
            batch = max_workers * 2
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                for start in range(0, len(ids), batch):
                    lot = ids[start:start + batch]
                    for ws, r in zip(lot, ex.map(self._probe_ws, lot)):
                        self._probed.add(ws)
                        if r:
                            found.append(r)
                            if r["remaining"] >= min_remaining:
                                good += 1
                    if good >= enough:
                        break
        with self._pool_lock:
            self._pool.extend(found)
            self._pool.sort(key=self._edf_key)   # EDF: reset som nhat truoc
            self._pool_time = time.time()
            return list(self._pool)

    def next_workspace(self, need_chars: int = 500):
        """Tra ve (workspace_id, scoped_token, remaining) — workspace con nhieu quota
        nhat va >= need_chars. None neu het.

        Dung pool probe-song-song + cache: lan dau build_pool (~vai giay cho vai tram
        workspace), cac lan sau tra ve tuc thi. Quota da dung duoc tru dan trong pool;
        workspace bi mark_exhausted (het quota/disabled khi convert) se bi bo qua.
        """
        with self._pool_lock:
            stale = self._pool is not None and (time.time() - self._pool_time > 2400)
            if stale:
                # >40 phut: token gan het han + TK het quota co the DA RESET.
                # Vut pool + XOA _exhausted (cho probe lai -> TK reset xong quay lai).
                # GIU _disabled (chet vinh vien, khoi probe lai). -> hop tool 24/7.
                self._pool = None
                self._probed = set()
                self._exhausted = set()
            need_build = self._pool is None
        if need_build:
            self.build_pool()

        # BIEN AN TOAN: quota trong pool la uoc luong (doc luc build, co the da lech
        # do TK bi dung o cho khac). Chi chon TK con DU THOAI MAI (need + buffer) ->
        # tranh "bao con X nhung het quota" -> bot churn doi TK. Buffer = 20% or 1500.
        buffer = max(1500, int(need_chars * 0.2))
        need_safe = need_chars + buffer

        def _pick():
            with self._pool_lock:
                for entry in self._pool:
                    ws = entry["workspace_id"]
                    if ws in self._exhausted or ws in self._disabled:
                        continue
                    if entry["remaining"] >= need_safe:
                        entry["remaining"] -= need_chars   # tru quota uoc luong da dung
                        return (ws, entry["token"], entry["remaining"] + need_chars)
            return None

        pick = _pick()
        if pick:
            return pick
        # Pool hien tai het workspace dung duoc -> build_pool DUNG SOM nen con nhieu ws
        # chua probe. Build lai (probe them lo tiep, bo qua ws da exhausted) roi thu lai.
        self.build_pool()
        return _pick()

    def ready_count(self) -> int:
        """So workspace SAN SANG dung ngay (con quota, chua exhausted/disabled)."""
        with self._pool_lock:
            if not self._pool:
                return 0
            return sum(1 for e in self._pool
                       if e.get("remaining", 0) > 500
                       and e["workspace_id"] not in self._exhausted
                       and e["workspace_id"] not in self._disabled)

    def ensure_warm(self, min_ready: int = 10):
        """Nap truoc pool neu so token san sang < min_ready -> khi can la co ngay."""
        if self.ready_count() < min_ready:
            self.build_pool()

    def mark_exhausted(self, workspace_id: str):
        """TK het quota (TAM THOI) -> bo qua den khi rebuild (co the da reset)."""
        self._exhausted.add(workspace_id)

    def mark_disabled(self, workspace_id: str):
        """TK bi vo hieu hoa/ban (VINH VIEN) -> khong bao gio probe/dung lai."""
        self._disabled.add(workspace_id)

    # ---------- onboard (sync) account moi ----------
    def invite_master(self, worker_token: str) -> tuple:
        """Worker (admin workspace cua no) moi master vao. -> (ok, msg)."""
        # Payload GIONG het tool kia (bat duoc qua MITM): co seat_type + group_ids
        r = self.session.post(
            f"{API_BASE}/v1/workspace/invites/add",
            headers=self._headers(worker_token),
            json={"email": self.email, "group_ids": [],
                  "seat_type": "workspace_lite_member"}, timeout=25)
        if r.status_code == 200:
            return True, "invited"
        try:
            st = r.json().get("detail", {})
            st = st.get("status", st) if isinstance(st, dict) else st
        except Exception:
            st = r.text[:80]
        if "already_in_workspace" in str(st):
            return True, "already_member"
        if "multiple_invites" in str(st):
            return True, "already_invited"   # da co loi moi cho -> accept o buoc sau
        return False, f"{r.status_code}:{st}"

    def worker_list_members(self, worker_token: str) -> list:
        """Worker liet ke member trong workspace cua no: [{user_id,email,...}]."""
        r = self.session.get(
            f"{API_BASE}/v1/workspace/members-minimal",
            headers=self._headers(worker_token), timeout=20)
        r.raise_for_status()
        return r.json()

    def worker_remove_member(self, worker_token: str, member_email: str) -> tuple:
        """Worker (admin) XOA 1 member (vd master da chet) khoi workspace cua no.

        Tim member theo email -> lay user_id -> DELETE. Giai phong seat de master
        moi vao duoc. -> (ok, msg).
        """
        try:
            members = self.worker_list_members(worker_token)
        except Exception as e:
            return False, f"list_fail:{str(e)[:40]}"
        uid = None
        for m in members:
            if (m.get("email") or "").lower() == (member_email or "").lower():
                uid = m.get("user_id"); break
        if not uid:
            return True, "not_member"   # khong co trong ws -> coi nhu seat da trong
        r = self.session.delete(
            f"{API_BASE}/v1/workspace/members",
            headers=self._headers(worker_token),
            json={"user_id_to_delete": uid}, timeout=25)
        if r.status_code in (200, 201, 204):
            return True, "removed"
        return False, f"{r.status_code}:{r.text[:80]}"

    def worker_token(self, email: str, password: str = "",
                     refresh_token: str = "", proxy=None, on_log=lambda *_: None,
                     on_quota=None, max_quota_retry: int = 5) -> tuple:
        """Lay id_token cua worker. -> (id_token, refresh_token) hoac (None, None).

        Uu tien refresh_token (khong can login -> khong dinh quota). Neu phai login
        password:
          - login QUA 4G proxy (proxy).
          - gap QUOTA_EXCEEDED (gioi han login PER-IP cua Firebase) -> goi on_quota()
            de DOI IP 4G roi login lai. on_quota() do caller cung cap (rotate 4G).
        """
        import time as _t
        from core.api_client import firebase_refresh, firebase_login
        # 1) Thu refresh truoc (nhanh, KHONG dinh quota login)
        if refresh_token:
            try:
                r = firebase_refresh(refresh_token, proxy=proxy)
                return r["id_token"], r.get("refresh_token", refresh_token)
            except Exception:
                pass  # refresh that bai -> login lai
        # 2) Login bang password (login qua 4G; QUOTA -> doi IP roi thu lai)
        if password:
            for attempt in range(max_quota_retry):
                try:
                    res = firebase_login(email, password, proxy=proxy)
                    return res["idToken"], res.get("refreshToken", "")
                except Exception as e:
                    msg = str(e)
                    if "QUOTA_EXCEEDED" in msg:
                        if on_quota:
                            on_log(f"QUOTA per-IP -> doi IP 4G (lan {attempt+1})")
                            try:
                                new_ip = on_quota()  # rotate 4G IP
                                if new_ip:
                                    on_log(f"IP 4G moi: {new_ip}")
                            except Exception as re:
                                on_log(f"rotate loi: {str(re)[:50]}")
                        else:
                            _t.sleep(30 * (attempt + 1))  # khong co 4G -> backoff
                        continue
                    if any(k in msg for k in ("INVALID_LOGIN", "EMAIL_NOT_FOUND",
                                              "INVALID_PASSWORD", "INVALID_LOGIN_CREDENTIALS")):
                        return None, None  # TK chet, khong retry
                    return None, None
        return None, None

    def onboard_worker(self, email: str, password: str = "",
                       refresh_token: str = "", proxy=None, on_log=lambda *_: None,
                       on_quota=None, cleanup_emails=None) -> tuple:
        """Sync 1 worker: lay token (refresh hoac login) roi moi master vao workspace.

        -> (ok, msg, refresh_token_moi, ws_id). refresh_token_moi de luu lai.
        on_quota: callback doi IP 4G khi gap QUOTA_EXCEEDED (login per-IP limit).
        cleanup_emails: list email master CU (da chet) can xoa khoi workspace truoc
                        khi invite master moi (giai phong seat) -> re-link chac chan.
        """
        wtok, new_rt = self.worker_token(
            email, password=password, refresh_token=refresh_token, proxy=proxy,
            on_log=on_log, on_quota=on_quota)
        if not wtok:
            return False, "login_fail", refresh_token, ""
        # workspace_id cua worker (de sau kiem tra master da la member chua)
        ws_id = ""
        try:
            import base64
            pl = json.loads(base64.urlsafe_b64decode(wtok.split(".")[1] + "=="))
            ws_id = pl.get("workspace_id", "")
        except Exception:
            pass
        # CLEANUP: xoa master cu (da chet) de giai phong seat truoc khi invite master moi
        for em in (cleanup_emails or []):
            if em and em != self.email:
                try:
                    ok_r, msg_r = self.worker_remove_member(wtok, em)
                    on_log(f"xoa master cu {em}: {msg_r}")
                except Exception as e:
                    on_log(f"xoa master cu {em} loi: {str(e)[:40]}")
        ok, msg = self.invite_master(wtok)
        return ok, msg, (new_rt or refresh_token), ws_id

    # ---------- accept invite (master nhan loi moi -> thanh member) ----------
    def pending_invites(self) -> list:
        """Danh sach loi moi master dang cho: [{invite_code, inviting_user_email}]."""
        tok = self.master_token()
        r = self.session.get(
            f"{API_BASE}/v1/workspace/invites/user",
            headers=self._headers(tok), timeout=20)
        r.raise_for_status()
        return r.json()

    def accept_invite(self, invite_code: str) -> tuple:
        """Master accept 1 invite -> (ok, status).

        Endpoint that (bat tu frontend): POST .../multi-workspace/invites/{code}/accept
        no_more_seats = master da day seat (can roi bot workspace hoac dung master khac).
        """
        tok = self.master_token()
        r = self.session.post(
            f"{API_BASE}/v1/workspace/multi-workspace/invites/{invite_code}/accept",
            headers=self._headers(tok), timeout=25)
        if r.status_code in (200, 201, 204):
            return True, "accepted"
        try:
            st = r.json().get("detail", {})
            st = st.get("status", st) if isinstance(st, dict) else st
        except Exception:
            st = r.text[:80]
        return False, f"{r.status_code}:{st}"

    def accept_all_pending(self, log=lambda *_: None) -> dict:
        """Accept moi invite dang cho. -> thong ke.

        no_more_seats = workspace do master DA la member (hoac workspace day 2 seat)
        -> BO QUA, chay tiep (khong phai gioi han toan cuc).
        """
        stats = {"accepted": 0, "already": 0, "fail": 0}
        for iv in self.pending_invites():
            code = iv.get("invite_code")
            if not code:
                continue
            ok, st = self.accept_invite(code)
            if ok:
                stats["accepted"] += 1
                log(f"accepted {iv.get('inviting_user_email','')}")
            elif "no_more_seats" in st or "already" in st.lower():
                stats["already"] += 1   # da la member / workspace day -> bo qua
            elif "cannot_join_free_workspace" in st:
                # MASTER da day (~320 workspace free) -> khong join them duoc nua
                stats["full"] = stats.get("full", 0) + 1
            else:
                stats["fail"] += 1
                log(f"fail {iv.get('inviting_user_email','')}: {st}")
        return stats


# ---- self test ----
if __name__ == "__main__":
    import sys
    sys.path.insert(0, PROJECT_ROOT)
    mw = MasterWorkspace()
    print("Master:", mw.email)
    pool = mw.workspace_pool()
    print(f"Workspace pool ({len(pool)}):")
    total = 0
    for p in pool:
        total += p["remaining"]
        print(f"  {p['workspace_id'][:14]} remaining={p['remaining']}/{p['limit']}")
    print(f"Tong quota kha dung: {total:,} chars")
    pick = mw.next_workspace(need_chars=100)
    print("next_workspace(100):", pick[0][:14] if pick else None,
          "remaining", pick[2] if pick else None)
