"""Proxy Manager - quản lý phones + SOCKS5 proxies"""
import subprocess
import socket
import json
import os
import time
import threading
from adb_utils import (list_devices, get_device_info, rotate_ip,
                        enable_usb_tethering, get_usb_tethering_interface)

# Debug mode: khong chay an cac lenh phu tro proxy nua.
CREATE_NO_WINDOW = 0
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')

class Phone:
    def __init__(self, device_id, port, bind_ip=None, name=None):
        self.device_id = device_id
        self.port = port          # SOCKS5 proxy port
        self.bind_ip = bind_ip    # IP của RNDIS adapter trên PC
        self.name = name or device_id
        self.proxy_proc = None
        self.current_ip = None
        self.rotate_count = 0
        self.last_rotate = None

    def to_dict(self):
        proxy_running = False
        try:
            if self.proxy_proc is not None and self.proxy_proc.poll() is None:
                proxy_running = True
            else:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1.5)
                proxy_running = s.connect_ex(('127.0.0.1', self.port)) == 0
                s.close()
        except Exception:
            proxy_running = False
        return {
            'id': self.device_id,
            'name': self.name,
            'port': self.port,
            'bind_ip': self.bind_ip,
            'current_4g_ip': self.current_ip,
            'rotate_count': self.rotate_count,
            'last_rotate': self.last_rotate,
            'proxy_running': proxy_running,
            'proxy_addr': f'socks5://127.0.0.1:{self.port}',
        }

class ProxyManager:
    def __init__(self):
        self.phones = {}  # device_id -> Phone
        self.api_key = None
        self.base_port = 10001
        self.load_config()

    def load_config(self):
        """Load config từ file"""
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
            self.api_key = cfg.get('api_key', 'mimi-4g-proxy-2026')
            self.base_port = cfg.get('base_port', 10001)
            # Load saved phones
            for p in cfg.get('phones', []):
                phone = Phone(p['device_id'], p['port'], p.get('bind_ip'), p.get('name'))
                self.phones[p['device_id']] = phone
        else:
            self.api_key = 'mimi-4g-proxy-2026'
            self.save_config()

    def save_config(self):
        """Lưu config"""
        cfg = {
            'api_key': self.api_key,
            'base_port': self.base_port,
            'phones': [
                {
                    'device_id': p.device_id,
                    'port': p.port,
                    'bind_ip': p.bind_ip,
                    'name': p.name,
                }
                for p in self.phones.values()
            ]
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2)

    def scan_devices(self):
        """Quét tìm phones — thêm mới, xóa đã rút"""
        devices = list_devices()
        changed = False

        # Xóa phone đã rút
        removed = [did for did in list(self.phones.keys()) if did not in devices]
        for did in removed:
            self.stop_proxy(self.phones[did])
            del self.phones[did]
            changed = True

        # Thêm phone mới
        new_devices = []
        for dev_id in devices:
            if dev_id not in self.phones:
                port = self.base_port + len(self.phones)
                info = get_device_info(dev_id)
                phone = Phone(dev_id, port, name=info.get('model', dev_id))
                phone.current_ip = info.get('ip_4g')
                self.phones[dev_id] = phone
                new_devices.append(phone)
                changed = True

        if changed:
            self.save_config()
        return new_devices

    def smart_scan(self):
        """Scan thong minh — check tung buoc, bao loi ro rang, tu fix khi co the

        Returns: {
            'devices': [...],
            'steps': [{'step': '...', 'status': 'ok'/'fail'/'warn', 'message': '...', 'fix': '...'}],
            'ready_count': int
        }
        """
        import subprocess, re
        ADB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "tools", "platform-tools", "adb.exe")
        C = 0x08000000
        steps = []
        devices_detail = []

        # STEP 1: ADB co chay khong
        try:
            subprocess.check_output([ADB, 'version'], text=True, timeout=5, creationflags=C)
            steps.append({'step': 'ADB', 'status': 'ok', 'message': 'ADB hoat dong'})
        except:
            steps.append({'step': 'ADB', 'status': 'fail', 'message': 'ADB khong chay',
                         'fix': 'Kiem tra D:\\11lab\\tools\\platform-tools\\adb.exe'})
            return {'devices': [], 'steps': steps, 'ready_count': 0}

        # STEP 2: Tim USB devices
        try:
            out = subprocess.check_output([ADB, 'devices', '-l'], text=True, timeout=10, creationflags=C)
            lines = [l.strip() for l in out.strip().split('\n')[1:] if l.strip()]
        except:
            lines = []

        if not lines:
            # Restart ADB server
            subprocess.run([ADB, 'kill-server'], creationflags=C, capture_output=True)
            import time; time.sleep(2)
            try:
                out = subprocess.check_output([ADB, 'devices', '-l'], text=True, timeout=10, creationflags=C)
                lines = [l.strip() for l in out.strip().split('\n')[1:] if l.strip()]
            except:
                lines = []

        if not lines:
            steps.append({'step': 'USB Devices', 'status': 'fail', 'message': 'Khong thay phone nao',
                         'fix': '1) Cam cap USB co truyen data (khong phai cap chi sac)\n'
                                '2) Tren phone: Settings > About Phone > bam Build Number 7 lan\n'
                                '3) Settings > Developer Options > bat USB Debugging\n'
                                '4) Khi cam USB, bam ALLOW tren popup "Allow USB Debugging?"'})
            return {'devices': [], 'steps': steps, 'ready_count': 0}

        steps.append({'step': 'USB Devices', 'status': 'ok', 'message': f'Tim thay {len(lines)} thiet bi'})

        # STEP 3: Check tung device
        ready_count = 0
        for line in lines:
            parts = line.split()
            dev_id = parts[0]
            dev_status = parts[1] if len(parts) > 1 else 'unknown'

            device = {'id': dev_id, 'status': dev_status, 'steps': []}

            # Check authorized
            if dev_status == 'unauthorized':
                device['steps'].append({'step': 'Auth', 'status': 'fail',
                    'message': 'Phone chua cho phep USB Debugging',
                    'fix': 'Tren phone: bam ALLOW/CHO PHEP tren popup "Allow USB Debugging?"'})
                devices_detail.append(device)
                continue
            elif dev_status == 'offline':
                device['steps'].append({'step': 'Auth', 'status': 'fail',
                    'message': 'Phone offline — rut cap cam lai',
                    'fix': 'Rut cap USB, doi 5 giay, cam lai'})
                devices_detail.append(device)
                continue
            elif dev_status != 'device':
                device['steps'].append({'step': 'Auth', 'status': 'fail',
                    'message': f'Trang thai la: {dev_status}',
                    'fix': 'Rut cap, bat lai USB Debugging, cam lai'})
                devices_detail.append(device)
                continue

            device['steps'].append({'step': 'Auth', 'status': 'ok', 'message': 'Da ket noi ADB'})

            # Get model
            try:
                model = subprocess.check_output([ADB, '-s', dev_id, 'shell', 'getprop', 'ro.product.model'],
                    text=True, timeout=5, creationflags=C).strip()
                device['model'] = model
            except:
                device['model'] = 'Unknown'

            # Get Android version
            try:
                android_ver = subprocess.check_output([ADB, '-s', dev_id, 'shell', 'getprop', 'ro.build.version.release'],
                    text=True, timeout=5, creationflags=C).strip()
                device['android'] = android_ver
            except:
                device['android'] = '?'

            # Check SIM
            try:
                operator = subprocess.check_output([ADB, '-s', dev_id, 'shell', 'getprop', 'gsm.sim.operator.alpha'],
                    text=True, timeout=5, creationflags=C).strip()
                sim_state = subprocess.check_output([ADB, '-s', dev_id, 'shell', 'getprop', 'gsm.sim.state'],
                    text=True, timeout=5, creationflags=C).strip()
            except:
                operator = ''
                sim_state = ''

            if not operator or sim_state != 'READY':
                device['steps'].append({'step': 'SIM', 'status': 'fail',
                    'message': f'SIM khong san sang (state={sim_state})',
                    'fix': 'Kiem tra SIM da cam dung, co data, khong bi khoa'})
            else:
                device['operator'] = operator
                device['steps'].append({'step': 'SIM', 'status': 'ok', 'message': f'SIM {operator}'})

            # Check 4G
            try:
                ip_out = subprocess.check_output([ADB, '-s', dev_id, 'shell', 'ip', 'addr', 'show'],
                    text=True, timeout=5, creationflags=C)
                ips_4g = re.findall(r'inet (\d+\.\d+\.\d+\.\d+)/\d+.*?(rmnet|ccmni|wwan)', ip_out)
            except:
                ips_4g = []

            if not ips_4g:
                device['steps'].append({'step': '4G', 'status': 'warn',
                    'message': 'Chua thay IP 4G — co the data tat hoac chua ket noi mang',
                    'fix': 'Tren phone: bat Data/4G, kiem tra co song khong'})
            else:
                device['ip_4g'] = ips_4g[0][0]
                device['steps'].append({'step': '4G', 'status': 'ok', 'message': f'IP 4G: {ips_4g[0][0]}'})

            # Check EveryProxy — kiểm tra SOCKS5 trên phone
            def _check_everyproxy():
                try:
                    check = subprocess.check_output(
                        [ADB, '-s', dev_id, 'shell',
                         f'netstat -tlnp 2>/dev/null | grep ":{self.EVERYPROXY_PORT}" || echo NOTFOUND'],
                        text=True, timeout=5, creationflags=C).strip()
                    return 'NOTFOUND' not in check and str(self.EVERYPROXY_PORT) in check
                except Exception:
                    return False

            everyproxy_ok = _check_everyproxy()
            # TU DONG BAT LAI neu EveryProxy tat (user hay phai bat tay -> tu lo).
            if not everyproxy_ok:
                try:
                    from adb_utils import ensure_everyproxy
                    if ensure_everyproxy(dev_id, port=self.EVERYPROXY_PORT, wait=8):
                        everyproxy_ok = True
                        device['steps'].append({'step': 'EveryProxy', 'status': 'ok',
                            'message': 'Tu bat lai EveryProxy SOCKS (khong can mo tay)'})
                except Exception:
                    pass

            if everyproxy_ok:
                device['steps'].append({'step': 'EveryProxy', 'status': 'ok',
                    'message': f'EveryProxy SOCKS5 dang chay tren port {self.EVERYPROXY_PORT}'})

                # Tắt RNDIS nếu còn bật
                try:
                    subprocess.run([ADB, '-s', dev_id, 'shell',
                                   'svc', 'usb', 'setFunctions', 'mtp'],
                                  timeout=5, creationflags=C, capture_output=True)
                except:
                    pass

                # Setup phone (stay awake)
                try:
                    from adb_utils import setup_stay_awake
                    setup_stay_awake(dev_id)
                except:
                    pass

                ready_count += 1
            else:
                device['steps'].append({'step': 'EveryProxy', 'status': 'fail',
                    'message': 'EveryProxy chua chay tren phone',
                    'fix': 'Mo app EveryProxy tren phone → bat SOCKS Proxy (toggle ON)'})

            devices_detail.append(device)

        steps.append({'step': 'Ket qua', 'status': 'ok' if ready_count > 0 else 'warn',
                      'message': f'{ready_count}/{len(lines)} thiet bi san sang'})

        # Add ready devices to manager
        new_devices = self.scan_devices()

        return {
            'devices': devices_detail,
            'steps': steps,
            'ready_count': ready_count,
            'new_phones': [p.to_dict() for p in new_devices],
        }

    def _detect_tether_ip(self):
        """Tu dong tim IP cua USB tethering interface (192.168.42.x)
        + Fix routing: đảm bảo traffic từ interface này đi qua phone gateway
        """
        try:
            out = subprocess.check_output(['ipconfig'], text=True,
                                          timeout=5, creationflags=CREATE_NO_WINDOW)
            import re
            ips = re.findall(r'192\.168\.42\.\d+', out)
            if not ips:
                return None

            pc_ip = ips[0]  # IP phía PC (VD: 192.168.42.117)

            # Tìm phone gateway IP từ route table
            route_out = subprocess.check_output(['route', 'print'], text=True,
                                                 timeout=5, creationflags=CREATE_NO_WINDOW)
            # Tìm dòng: 0.0.0.0 ... 192.168.42.xxx (gateway) ... 192.168.42.yyy (PC)
            gw_match = re.search(r'0\.0\.0\.0\s+0\.0\.0\.0\s+(192\.168\.42\.\d+)\s+(192\.168\.42\.\d+)', route_out)
            phone_gw = gw_match.group(1) if gw_match else '192.168.42.129'

            print(f"[4G] Tethering: PC={pc_ip}, Phone GW={phone_gw}", flush=True)

            # Fix routing: Windows KHÔNG tự route qua RNDIS dù bind source IP.
            # Phải set RNDIS metric THẤP HƠN Ethernet để OS chọn RNDIS cho traffic
            # bind trên 192.168.42.x. Ethernet metric = 25, RNDIS metric = 5.
            try:
                ps_script = (
                    # Set Ethernet metric = 25 
                    "Get-NetAdapter | Where-Object { $_.InterfaceDescription -notlike '*RNDIS*' "
                    "-and $_.InterfaceDescription -notlike '*Remote NDIS*' "
                    "-and $_.Status -eq 'Up' } | "
                    "ForEach-Object { Set-NetIPInterface -InterfaceIndex $_.ifIndex -InterfaceMetric 25 }; "
                    # Set RNDIS metric = 5 (ưu tiên cao nhất)
                    "Get-NetAdapter | Where-Object { $_.InterfaceDescription -like '*RNDIS*' "
                    "-or $_.InterfaceDescription -like '*Remote NDIS*' } | "
                    "ForEach-Object { Set-NetIPInterface -InterfaceIndex $_.ifIndex -InterfaceMetric 5 }"
                )
                subprocess.run(['powershell', '-NoProfile', '-EncodedCommand',
                                self._encode_ps(ps_script)],
                               timeout=10, creationflags=CREATE_NO_WINDOW, capture_output=True)
            except:
                pass

            return pc_ip
        except:
            pass
        return None

    @staticmethod
    def _encode_ps(script: str) -> str:
        """Encode PowerShell script to Base64 for -EncodedCommand (avoids $_ escaping issues)."""
        import base64
        return base64.b64encode(script.encode('utf-16-le')).decode('ascii')

    # EveryProxy SOCKS5 port trên phone (mặc định 1080)
    EVERYPROXY_PORT = 1080

    def start_proxy(self, phone):
        """Khởi động SOCKS5 proxy cho 1 phone.

        Dùng ADB forward: PC port → phone EveryProxy (SOCKS5).
        Traffic đi thẳng từ phone ra 4G, KHÔNG qua USB tethering.
        Yêu cầu: EveryProxy app bật SOCKS trên phone.
        """
        from adb_utils import ADB

        # 1. Tắt RNDIS (USB tethering) để phone route qua 4G
        try:
            subprocess.run([ADB, '-s', phone.device_id, 'shell',
                           'svc', 'usb', 'setFunctions', 'mtp'],
                          timeout=5, creationflags=CREATE_NO_WINDOW,
                          capture_output=True)
        except:
            pass

        # 2. Setup ADB forward: PC port → phone EveryProxy port
        try:
            # Remove old forward
            subprocess.run([ADB, '-s', phone.device_id, 'forward',
                           '--remove', f'tcp:{phone.port}'],
                          timeout=5, creationflags=CREATE_NO_WINDOW,
                          capture_output=True)
            # Create new forward
            result = subprocess.run(
                [ADB, '-s', phone.device_id, 'forward',
                 f'tcp:{phone.port}', f'tcp:{self.EVERYPROXY_PORT}'],
                timeout=5, creationflags=CREATE_NO_WINDOW,
                capture_output=True, text=True)
            if result.returncode != 0:
                print(f"[!] ADB forward fail: {result.stderr}", flush=True)
                return False
        except Exception as e:
            print(f"[!] ADB forward error: {e}", flush=True)
            return False

        # Không cần start process — ADB forward là proxy
        phone.proxy_proc = None  # Marker: không có process, dùng ADB forward
        phone.bind_ip = '127.0.0.1'  # Traffic qua localhost ADB forward
        print(f"[SOCKS5] :{phone.port} -> ADB forward {phone.device_id} -> EveryProxy :{self.EVERYPROXY_PORT}", flush=True)
        return True

    def stop_proxy(self, phone):
        """Dừng proxy — remove ADB forward"""
        from adb_utils import ADB
        if phone.proxy_proc:
            phone.proxy_proc.terminate()
            phone.proxy_proc = None
        # Remove ADB forward
        try:
            subprocess.run([ADB, '-s', phone.device_id, 'forward',
                           '--remove', f'tcp:{phone.port}'],
                          timeout=5, creationflags=CREATE_NO_WINDOW,
                          capture_output=True)
        except:
            pass

    def start_all(self):
        """Khởi động proxy CHỈ cho phone đang cắm"""
        connected = list_devices()
        # Xóa phone đã rút trước
        removed = [did for did in list(self.phones.keys()) if did not in connected]
        if removed:
            for did in removed:
                self.stop_proxy(self.phones[did])
                del self.phones[did]
            self.save_config()
        # Start còn lại + lấy IP public
        for phone in self.phones.values():
            self.start_proxy(phone)
            pub = self._get_public_ip(phone)
            if pub:
                phone.current_ip = pub

    def stop_all(self):
        """Dừng tất cả"""
        for phone in self.phones.values():
            self.stop_proxy(phone)

    def rotate(self, device_id, wait=8, unique=True):
        """Đổi IP 1 phone — có verify không trùng IP cũ"""
        phone = self.phones.get(device_id)
        if not phone:
            return None

        if unique:
            from adb_utils import rotate_ip_verified
            if not hasattr(phone, 'ip_history'):
                phone.ip_history = set()
            new_ip = rotate_ip_verified(device_id, phone.ip_history, max_retries=3, wait=wait)
            if new_ip:
                phone.ip_history.add(new_ip)
                if len(phone.ip_history) > 50:
                    phone.ip_history = set(list(phone.ip_history)[-50:])
        else:
            new_ip = rotate_ip(device_id, wait=wait)

        # Re-setup ADB forward sau rotate
        self.start_proxy(phone)

        # Chi bao OK khi route SOCKS da that su ra internet + ElevenLabs.
        public_ip = self._wait_proxy_ready(phone, timeout=max(30, wait + 25))
        phone.current_ip = public_ip or new_ip
        phone.rotate_count += 1
        phone.last_rotate = time.strftime('%Y-%m-%d %H:%M:%S')

        return phone.current_ip

    def _get_public_ip(self, phone):
        """Lay IP public qua SOCKS proxy (IP ma website thay)"""
        try:
            import requests
            proxy = f'socks5h://127.0.0.1:{phone.port}'
            r = requests.get('http://checkip.amazonaws.com',
                           proxies={'http': proxy, 'https': proxy}, timeout=15)
            return r.text.strip()
        except:
            return None

    def _probe_elevenlabs_route(self, phone):
        """Kiem tra route HTTPS toi ElevenLabs da thong that chua."""
        try:
            import requests
            proxy = f'socks5h://127.0.0.1:{phone.port}'
            r = requests.get(
                'https://api.us.elevenlabs.io/v1/user/subscription',
                proxies={'http': proxy, 'https': proxy},
                timeout=20,
                headers={'accept': '*/*'})
            return r.status_code in (200, 401)
        except:
            return False

    def _wait_proxy_ready(self, phone, timeout=60):
        """Chi coi proxy ready khi co public IP va di duoc toi ElevenLabs."""
        deadline = time.time() + max(10, timeout)
        last_ip = None
        while time.time() < deadline:
            pub = self._get_public_ip(phone)
            if pub:
                last_ip = pub
                if self._probe_elevenlabs_route(phone):
                    return pub
            time.sleep(3)
        return last_ip

    def rotate_all(self, wait=8):
        """Đổi IP tất cả phones (song song)"""
        results = {}
        threads = []
        def _rotate(dev_id):
            results[dev_id] = self.rotate(dev_id, wait)
        for dev_id in self.phones:
            t = threading.Thread(target=_rotate, args=(dev_id,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=30)
        return results

    def list_proxies(self):
        """Liệt kê proxy — CHỈ hiện phone đang cắm, tự xóa phone đã rút"""
        connected = list_devices()
        # Xóa phone đã rút
        removed = [did for did in list(self.phones.keys()) if did not in connected]
        if removed:
            for did in removed:
                self.stop_proxy(self.phones[did])
                del self.phones[did]
            self.save_config()
        return [p.to_dict() for p in self.phones.values()]

    def check_auth(self, key):
        """Kiểm tra API key"""
        return key == self.api_key
