"""
net_drive.py — Map o mang (net use) truoc khi chay, hoac khi thu muc khong ton tai.

Cau hinh trong settings.json (chinh o tab Auto Convert > Cai dat nang cao):
  map_drive_enabled : bat/tat
  map_drive_letter  : "Z:"
  map_drive_share   : "\\\\192.168.88.254\\D"
  map_drive_user    : "smbuser"
  map_drive_pass    : "159753"

Tuong duong lenh:
  net use Z: \\192.168.88.254\D /user:smbuser 159753 /persistent:yes
"""
import subprocess


def _flags():
    # Windows: khong hien cua so cmd
    return 0x08000000


def build_cmd(cfg):
    """Tao list args 'net use ...' tu config. -> list | None neu thieu."""
    letter = (cfg.get("map_drive_letter") or "").strip()
    share = (cfg.get("map_drive_share") or "").strip()
    user = (cfg.get("map_drive_user") or "").strip()
    pw = (cfg.get("map_drive_pass") or "").strip()
    if not letter or not share:
        return None
    cmd = ["net", "use", letter, share]
    if user:
        cmd.append("/user:" + user)
    if pw:
        cmd.append(pw)
    cmd.append("/persistent:yes")
    return cmd


def is_enabled(cfg):
    return bool(cfg.get("map_drive_enabled")) and bool(build_cmd(cfg))


def map_drive(cfg, on_log=lambda *_: None, force_remap=False):
    """Chay net use de map o mang. -> (ok, msg).

    force_remap: xoa mapping cu (/delete) truoc roi map lai (khi reconnect).
    """
    if not cfg.get("map_drive_enabled"):
        return False, "tat"
    cmd = build_cmd(cfg)
    if not cmd:
        return False, "thieu thong tin (drive letter / share)"
    letter = (cfg.get("map_drive_letter") or "").strip()

    if force_remap:
        try:
            subprocess.run(["net", "use", letter, "/delete", "/y"],
                           capture_output=True, timeout=15,
                           creationflags=_flags())
        except Exception:
            pass

    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=20, creationflags=_flags())
        if r.returncode == 0:
            on_log(f"Map o mang {letter} OK")
            return True, "ok"
        err = (r.stderr or r.stdout or "").strip()
        # "da co connection" -> coi nhu OK
        if "1219" in err or "already" in err.lower() or "đã" in err:
            return True, "da map san"
        on_log(f"Map o {letter} loi: {err[:100]}")
        return False, err[:160]
    except Exception as e:
        return False, str(e)[:120]
