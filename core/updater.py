"""
updater.py — Cap nhat tool tu GitHub (nut "Update").

Tai zip cua repo -> giai nen -> ghi de CODE, GIU NGUYEN du lieu may
(config/, logs/, ffmpeg/, output/). Khong can cai git.

Repo mac dinh: https://github.com/manhthang1905-hub/voice  (nhanh main/master)
"""
import os
import io
import shutil
import zipfile
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REPO_OWNER = "manhthang1905-hub"
REPO_NAME = "voice"

# Thu muc/file KHONG dong vao (du lieu rieng tung may + binary lon)
PROTECT = {"config", "logs", "ffmpeg", "output", ".git", ".claude"}


def _zip_url(branch):
    return f"https://github.com/{REPO_OWNER}/{REPO_NAME}/archive/refs/heads/{branch}.zip"


def update_from_github(on_log=lambda *_: None, branch=None):
    """Tai code moi nhat tu GitHub va ghi de. -> (ok, msg, so_file_cap_nhat).

    Giu nguyen: config/ (TK, master, settings, proxy), logs/, ffmpeg/, output/.
    """
    branches = [branch] if branch else ["main", "master"]
    data = None
    used_branch = None
    last_err = ""
    for br in branches:
        try:
            on_log(f"Tai code tu GitHub (nhanh {br})...")
            req = urllib.request.Request(
                _zip_url(br), headers={"User-Agent": "11lab-updater"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            used_branch = br
            break
        except Exception as e:
            last_err = str(e)[:80]
            continue
    if data is None:
        return False, f"Khong tai duoc repo: {last_err}", 0

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        return False, f"File zip loi: {str(e)[:60]}", 0

    # zip co 1 thu muc goc: voice-<branch>/
    names = zf.namelist()
    if not names:
        return False, "Zip rong", 0
    root_prefix = names[0].split("/")[0] + "/"

    updated = 0
    skipped_protect = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        rel = info.filename[len(root_prefix):] if info.filename.startswith(root_prefix) else info.filename
        if not rel:
            continue
        rel = rel.replace("/", os.sep)
        top = rel.split(os.sep)[0]
        if top in PROTECT:
            skipped_protect += 1
            continue
        dst = os.path.join(PROJECT_ROOT, rel)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with zf.open(info) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
            updated += 1
        except Exception as e:
            on_log(f"  Bo qua {rel}: {str(e)[:40]}")

    on_log(f"Da cap nhat {updated} file (giu nguyen {skipped_protect} file du lieu).")
    return True, f"Cap nhat xong tu nhanh {used_branch}: {updated} file", updated
