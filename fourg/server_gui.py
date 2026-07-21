"""
4G Proxy Manager — All-in-One
Mo GUI = chay server + gateway + proxy
Dong GUI = tat het

Double-click file nay hoac chay: pythonw server_gui.py
"""
import customtkinter as ctk
import threading
import json
import time
import os
import sys
import subprocess
import signal

# ========================
# SERVER EMBEDDED
# ========================

SERVER_PROC = None
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))

def _encode_ps(script: str) -> str:
    """Encode PowerShell script to Base64 (avoids $_ issues in subprocess)."""
    import base64
    return base64.b64encode(script.encode('utf-16-le')).decode('ascii')

def kill_old():
    """Tat tat ca process cu lien quan 4G proxy (tru chinh minh)"""
    if os.name == 'nt':
        my_pid = os.getpid()
        # Kill python dang chay server.py hoac socks5_server.py (tru PID hien tai)
        try:
            ps_script = (
                f"Get-WmiObject Win32_Process -Filter \"Name='python.exe'\" | "
                f"Where-Object {{ ($_.CommandLine -like '*server.py*' -or $_.CommandLine -like '*socks5*') "
                f"-and $_.ProcessId -ne {my_pid} }} | "
                f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
            )
            subprocess.run(['powershell', '-NoProfile', '-EncodedCommand', _encode_ps(ps_script)],
                           timeout=10, creationflags=0x08000000, capture_output=True)
        except:
            pass
        # Free server ports only (KHÔNG kill 10001+ vì đó là ADB forward)
        for port in [19800, 5000]:
            try:
                ps_free = (
                    f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | "
                    f"Where-Object {{ $_.OwningProcess -ne {my_pid} }} | "
                    f"ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}"
                )
                subprocess.run(['powershell', '-NoProfile', '-EncodedCommand', _encode_ps(ps_free)],
                               timeout=5, creationflags=0x08000000, capture_output=True)
            except:
                pass
        time.sleep(2)

def start_server():
    """Kill cu + restart ADB + chay server.py trong background"""
    global SERVER_PROC
    kill_old()
    # Restart ADB daemon để đảm bảo device discoverable
    try:
        adb = os.path.join(os.path.dirname(SERVER_DIR), "tools", "platform-tools", "adb.exe")
        subprocess.run([adb, 'start-server'], timeout=10, creationflags=0x08000000, capture_output=True)
        time.sleep(2)
    except:
        pass
    SERVER_PROC = subprocess.Popen(
        [sys.executable, os.path.join(SERVER_DIR, "server.py")],
        cwd=SERVER_DIR,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x08000000 if os.name == 'nt' else 0
    )
    return SERVER_PROC

def stop_server():
    """Tat server"""
    global SERVER_PROC
    if SERVER_PROC:
        SERVER_PROC.terminate()
        try:
            SERVER_PROC.wait(timeout=5)
        except:
            SERVER_PROC.kill()
        SERVER_PROC = None

def wait_server_ready(timeout=20):
    """Cho server san sang"""
    import urllib.request
    for _ in range(timeout * 2):
        try:
            urllib.request.urlopen("http://127.0.0.1:19800/", timeout=2)
            return True
        except:
            time.sleep(0.5)
    return False

# ========================
# API HELPER
# ========================

import urllib.request as urlreq

API_URL = "http://127.0.0.1:19800"
API_KEY = "mimi-4g-proxy-2026"

def api(path, method="GET", timeout=5):
    req = urlreq.Request(f"{API_URL}{path}", method=method,
                          headers={"X-API-Key": API_KEY, "Content-Type": "application/json"})
    if method == "POST":
        req.data = b"{}"
    return json.loads(urlreq.urlopen(req, timeout=timeout).read())

# ========================
# GUI
# ========================

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("4G Proxy Manager")
        self.geometry("1000x750")
        self.minsize(800, 600)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Start server
        self.server_status = "Starting..."
        self._start_server_bg()

        # Tabs
        self.tabs = ctk.CTkTabview(self, anchor="nw")
        self.tabs.pack(fill="both", expand=True, padx=10, pady=10)

        self.tab_dash = self.tabs.add("Dashboard")
        self.tab_guide = self.tabs.add("Huong Dan")
        self.tab_api = self.tabs.add("API Docs")

        self.build_dashboard()
        self.build_guide()
        self.build_api_docs()
        # KHONG refresh ngay - cho server ready

    def _start_server_bg(self):
        def _do():
            start_server()
            if wait_server_ready(timeout=20):
                self.server_status = "Online"
                self.after(0, self.log, "Server + Gateway started")
                self.after(0, self.refresh_loop)  # Bat dau refresh SAU KHI server ready
            else:
                self.server_status = "Failed"
                self.after(0, self.log, "Server failed to start!")
        threading.Thread(target=_do, daemon=True).start()

    def on_close(self):
        """Dong GUI = tat server + gateway + proxy"""
        stop_server()
        # Kill any remaining socks5 proxies
        if os.name == 'nt':
            os.system('taskkill /F /FI "WINDOWTITLE eq socks5*" >nul 2>&1')
        self.destroy()

    # ========================
    # DASHBOARD
    # ========================
    def build_dashboard(self):
        tab = self.tab_dash

        # Toolbar
        tb = ctk.CTkFrame(tab)
        tb.pack(fill="x", padx=5, pady=5)
        ctk.CTkButton(tb, text="Scan", command=self.scan, width=80).pack(side="left", padx=3)
        ctk.CTkButton(tb, text="Start All", command=self.start_all, width=90, fg_color="#238636").pack(side="left", padx=3)
        ctk.CTkButton(tb, text="Stop All", command=self.stop_all, width=90, fg_color="#da3633").pack(side="left", padx=3)
        ctk.CTkButton(tb, text="Rotate All", command=self.rotate_all, width=100, fg_color="#9e6a03").pack(side="left", padx=3)
        ctk.CTkButton(tb, text="Refresh", command=self.refresh, width=80).pack(side="left", padx=3)
        self.status_lbl = ctk.CTkLabel(tb, text="Starting...", font=("Consolas", 12))
        self.status_lbl.pack(side="right", padx=10)

        # Phone list
        self.phone_frame = ctk.CTkScrollableFrame(tab, height=280)
        self.phone_frame.pack(fill="both", expand=True, padx=5, pady=5)

        # Gateway copy
        cf = ctk.CTkFrame(tab)
        cf.pack(fill="x", padx=5, pady=5)
        ctk.CTkLabel(cf, text="Gateway:", font=("Consolas", 12)).pack(side="left", padx=5)
        self.gw_entry = ctk.CTkEntry(cf, width=450, font=("Consolas", 13))
        self.gw_entry.pack(side="left", padx=5)
        self.gw_entry.insert(0, f"socks5://{API_KEY}:x@192.168.88.254:5000")
        ctk.CTkButton(cf, text="Copy", width=60, command=lambda: self._copy(self.gw_entry.get())).pack(side="left", padx=3)

        # Test
        tf = ctk.CTkFrame(tab)
        tf.pack(fill="x", padx=5, pady=5)
        ctk.CTkLabel(tf, text="Test:", font=("", 12, "bold")).pack(side="left", padx=5)
        self.test_cb = ctk.CTkComboBox(tf, values=["--"], width=200)
        self.test_cb.pack(side="left", padx=5)
        ctk.CTkButton(tf, text="Test", width=60, command=self.test_proxy).pack(side="left", padx=3)
        ctk.CTkButton(tf, text="Speed", width=60, command=self.test_speed).pack(side="left", padx=3)
        self.test_lbl = ctk.CTkLabel(tf, text="", font=("Consolas", 12), wraplength=400)
        self.test_lbl.pack(side="left", padx=10)

        # Log
        self.log_box = ctk.CTkTextbox(tab, height=100, font=("Consolas", 11))
        self.log_box.pack(fill="x", padx=5, pady=5)
        self.log("Starting server...")

    def log(self, msg):
        t = time.strftime("%H:%M:%S")
        self.log_box.insert("0.0", f"[{t}] {msg}\n")

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.log(f"Copied: {text}")

    def refresh(self):
        def _do():
            try:
                data = api("/list")
                phones = data.get("proxies", [])
                self.after(0, self._render, phones)
            except:
                self.after(0, self.status_lbl.configure, {"text": f"Server: {self.server_status}"})
        threading.Thread(target=_do, daemon=True).start()

    def refresh_loop(self):
        self.refresh()
        self.after(5000, self.refresh_loop)

    def _render(self, phones):
        for w in self.phone_frame.winfo_children():
            w.destroy()

        on = sum(1 for p in phones if p.get("proxy_running"))
        rot = sum(p.get("rotate_count", 0) for p in phones)
        self.status_lbl.configure(text=f"Devices: {len(phones)} | Online: {on} | Rotations: {rot}")

        vals = [f"{p['name']}|{p['id']}" for p in phones] if phones else ["--"]
        self.test_cb.configure(values=vals)

        if not phones:
            ctk.CTkLabel(self.phone_frame, text="Cam phone USB va bam Scan", font=("", 14), text_color="gray").pack(pady=30)
            return

        for p in phones:
            row = ctk.CTkFrame(self.phone_frame)
            row.pack(fill="x", padx=5, pady=3)

            ip = p.get('current_4g_ip')
            running = p.get("proxy_running")
            if running and ip:
                color = "#3fb950"; status = "Online"
            elif running and not ip:
                color = "#d29922"; status = "No IP"
            else:
                color = "#da3633"; status = "Offline"

            ctk.CTkLabel(row, text="*", font=("", 16), text_color=color, width=20).pack(side="left", padx=5)

            txt = f"{p.get('name','?')}  [{status}]  IP: {ip or 'N/A'}  :{p.get('port','')}  x{p.get('rotate_count',0)}"
            ctk.CTkLabel(row, text=txt, font=("Consolas", 12), anchor="w").pack(side="left", fill="x", expand=True, padx=5)

            did = p["id"]
            ctk.CTkButton(row, text="Rotate", width=60, fg_color="#9e6a03", command=lambda d=did: self._rotate(d)).pack(side="right", padx=2)
            ctk.CTkButton(row, text="Stop", width=50, fg_color="#da3633", command=lambda d=did: self._api_bg(f"/proxy/{d}/stop", "POST", f"Stopped")).pack(side="right", padx=2)
            ctk.CTkButton(row, text="Start", width=50, fg_color="#238636", command=lambda d=did: self._api_bg(f"/proxy/{d}/start", "POST", f"Started")).pack(side="right", padx=2)
            ctk.CTkButton(row, text="Copy", width=50, command=lambda port=p.get("port"): self._copy(f"socks5://192.168.88.254:{port}")).pack(side="right", padx=2)

    def _api_bg(self, path, method, msg):
        def _do():
            try:
                api(path, method, timeout=40)
                self.after(0, self.log, msg)
                self.after(500, self.refresh)
            except Exception as e:
                self.after(0, self.log, f"Error: {e}")
        threading.Thread(target=_do, daemon=True).start()

    def _rotate(self, did):
        self.log(f"Rotating {did}...")
        def _do():
            try:
                d = api(f"/rotate/{did}", "POST", timeout=40)
                self.after(0, self.log, f"New IP: {d.get('new_ip','?')}")
                self.after(500, self.refresh)
            except Exception as e:
                self.after(0, self.log, f"Error: {e}")
        threading.Thread(target=_do, daemon=True).start()

    def scan(self):
        self.log("Scanning...")
        def _do():
            try:
                data = api("/scan", "POST", timeout=30)
                lines = []
                # Steps tong quat
                for step in data.get('steps', []):
                    s = 'OK' if step['status']=='ok' else 'FAIL' if step['status']=='fail' else 'WARN'
                    lines.append(f"[{s}] {step['step']}: {step['message']}")
                    if 'fix' in step:
                        for fix_line in step['fix'].split('\n'):
                            lines.append(f"  -> {fix_line}")

                # Chi tiet tung device
                for dev in data.get('devices', []):
                    lines.append(f"--- {dev.get('model','?')} ({dev['id'][:12]}) ---")
                    for s in dev.get('steps', []):
                        st = 'OK' if s['status']=='ok' else 'FAIL' if s['status']=='fail' else 'WARN'
                        lines.append(f"  [{st}] {s['step']}: {s['message']}")
                        if 'fix' in s:
                            for fix_line in s['fix'].split('\n'):
                                lines.append(f"    -> {fix_line}")

                lines.append(f"San sang: {data.get('ready_count', 0)}")
                for line in lines:
                    self.after(0, self.log, line)
                self.after(500, self.refresh)
            except Exception as e:
                self.after(0, self.log, f"Scan error: {e}")
        threading.Thread(target=_do, daemon=True).start()
    def start_all(self): self._api_bg("/start", "POST", "All started")
    def stop_all(self): self._api_bg("/stop", "POST", "All stopped")
    def rotate_all(self):
        self.log("Rotating all...")
        self._api_bg("/rotate-all", "POST", "All rotated")

    def test_proxy(self):
        val = self.test_cb.get()
        if not val or val == "--": return
        did = val.split("|")[-1]
        self.test_lbl.configure(text="Testing...")
        def _do():
            try:
                d = api(f"/test/{did}")
                txt = f"OK {d['ip']} | {d.get('org','')} | {d.get('latency_ms','')}ms" if d.get("ok") else f"FAIL {d.get('error','')}"
                self.after(0, self.test_lbl.configure, {"text": txt})
            except Exception as e:
                self.after(0, self.test_lbl.configure, {"text": str(e)})
        threading.Thread(target=_do, daemon=True).start()

    def test_speed(self):
        val = self.test_cb.get()
        if not val or val == "--": return
        did = val.split("|")[-1]
        self.test_lbl.configure(text="Speed test...")
        def _do():
            try:
                d = api(f"/test/{did}/speed")
                txt = f"{d.get('speed_mbps',0)} Mbps | {d.get('size_kb',0)}KB in {d.get('time_ms',0)}ms" if d.get("ok") else d.get("error","")
                self.after(0, self.test_lbl.configure, {"text": txt})
            except Exception as e:
                self.after(0, self.test_lbl.configure, {"text": str(e)})
        threading.Thread(target=_do, daemon=True).start()

    # ========================
    # HUONG DAN
    # ========================
    def build_guide(self):
        text = ctk.CTkTextbox(self.tab_guide, font=("Consolas", 13), wrap="word")
        text.pack(fill="both", expand=True, padx=5, pady=5)
        text.insert("0.0", GUIDE_TEXT)
        text.configure(state="disabled")

    # ========================
    # API DOCS
    # ========================
    def build_api_docs(self):
        text = ctk.CTkTextbox(self.tab_api, font=("Consolas", 13), wrap="word")
        text.pack(fill="both", expand=True, padx=5, pady=5)
        text.insert("0.0", API_DOCS_TEXT)
        text.configure(state="disabled")


# ========================
# TEXT CONTENT
# ========================

GUIDE_TEXT = """HUONG DAN SU DUNG 4G PROXY
======================================

1. COPY PROXY — DAN VAO BAT KY TOOL NAO
────────────────────────────────────────
  socks5://mimi-4g-proxy-2026:x@192.168.88.254:5000


2. CAC CHE DO
────────────────────────────────────────
  Rotating (IP moi moi request):
    socks5://KEY:x@192.168.88.254:5000

  Sticky (giu IP):
    socks5://KEY-session-abc123:x@192.168.88.254:5000

  Doi IP = doi session ID:
    socks5://KEY-session-phien1:x@192.168.88.254:5000  -> IP A
    socks5://KEY-session-phien2:x@192.168.88.254:5000  -> IP B

  Chon phone:
    socks5://KEY-phone-1:x@192.168.88.254:5000

  Auto rotate 10 phut:
    socks5://KEY-session-s1-rotate-10:x@192.168.88.254:5000


3. PYTHON
────────────────────────────────────────
  import requests

  proxy = "socks5://mimi-4g-proxy-2026:x@192.168.88.254:5000"
  proxies = {"http": proxy, "https": proxy}
  r = requests.get("https://example.com", proxies=proxies)

  # SDK
  from proxy4g import Proxy4G
  p = Proxy4G()
  r = requests.get(url, proxies=p.for_requests())
  p.rotate()


4. GIU IP SACH
────────────────────────────────────────
  from proxy4g import Proxy4G
  p = Proxy4G()

  if "captcha" in r.text.lower():
      p.report_captcha()

  if r.status_code == 429:
      p.report_blocked(429)


5. CURL
────────────────────────────────────────
  curl --proxy socks5://mimi-4g-proxy-2026:x@192.168.88.254:5000 https://ipinfo.io/json
  curl http://192.168.88.254:19800/action/LINK_ID


6. NODE.JS
────────────────────────────────────────
  const agent = new SocksProxyAgent('socks5://mimi-4g-proxy-2026:x@192.168.88.254:5000');
  const res = await fetch('https://example.com', { agent });


7. SELENIUM / PLAYWRIGHT
────────────────────────────────────────
  options.add_argument('--proxy-server=socks5://192.168.88.254:5000')


8. DOI IP
────────────────────────────────────────
  1) Doi session ID trong proxy string
  2) curl -X POST 192.168.88.254:19800/rotate/DEVICE?key=KEY
  3) curl 192.168.88.254:19800/action/LINK_ID
  4) p.rotate()


9. TOC DO AN TOAN
────────────────────────────────────────
  Google:    5-10/gio   (nguy hiem: 50+)
  Facebook:  20-30/gio  (nguy hiem: 100+)
  Instagram: 20-30/gio  (nguy hiem: 100+)
  Amazon:    30-60/gio  (nguy hiem: 200+)
  Web khac:  5-10/phut  (nguy hiem: 60+)

  NEN: random delay 3-15s, giu UA co dinh, bao captcha
  KHONG: spam, doi UA, dung 1 IP > 100 request


KET NOI
────────────────────────────────────────
  Gateway: socks5://KEY:x@192.168.88.254:5000
  API:     http://192.168.88.254:19800
  Key:     mimi-4g-proxy-2026
"""

API_DOCS_TEXT = """API REFERENCE
======================================
Base: http://192.168.88.254:19800
Auth: Header X-API-Key hoac ?key=KEY

PROXY:
  GET  /list              Danh sach proxy
  GET  /proxy/<dev>       Thong tin proxy
  POST /rotate/<dev>      Doi IP
  POST /rotate-all        Doi tat ca
  POST /scan              Tim phone
  POST /start             Bat proxy
  POST /stop              Tat proxy
  GET  /test/<dev>        Test IP, latency
  GET  /test/<dev>/speed  Test toc do

ACTION LINKS (khong can auth):
  POST /action-link/create   Tao link
  GET  /action/<link_id>     Doi IP
  GET  /action-links         DS links

SMART POOL:
  GET  /pool/session/<id>    Session
  POST /pool/new-ip/<id>     IP moi
  GET  /pool/any             Proxy ngay
  GET  /pool/stats           Thong ke

IP GUARD:
  GET  /guard/status         Trang thai
  POST /guard/captcha        Bao CAPTCHA
  POST /guard/error?code=429 Bao block

SETUP:
  POST /setup/<dev>          Setup phone
  GET  /config               Xem config
  POST /config               Sua config

VI DU:
  curl "192.168.88.254:19800/list?key=mimi-4g-proxy-2026"
  curl -X POST "192.168.88.254:19800/rotate/DEV?key=mimi-4g-proxy-2026"
  curl "192.168.88.254:19800/pool/any?key=mimi-4g-proxy-2026"
"""


# ========================
# SHORTCUT: Double-click chay
# ========================
if __name__ == "__main__":
    app = App()
    app.mainloop()
