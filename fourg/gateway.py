"""
Backconnect Gateway v2 — KHÔNG ĐỂ TOOL KHÁC BỊ LỖI

Nguyên tắc:
1. Tool gửi request → LUÔN CÓ RESPONSE, không bao giờ connection refused
2. Phone chết → tự chuyển phone khác, tool không biết
3. Đang rotate → chờ xong rồi mới route, không để timeout
4. IP m��i phải VERIFY trước khi phục vụ
5. Gateway tự heal, tự restart

Cách dùng:
    socks5://KEY:x@localhost:5000                    → random phone, IP mới mỗi request
    socks5://KEY-session-abc123:x@localhost:5000      → sticky IP
    socks5://KEY-session-NEW:x@localhost:5000          → đổi session = đổi IP
    socks5://KEY-phone-1:x@localhost:5000              → chọn phone
"""

import socket
import select
import struct
import threading
import time
import hashlib
import json
import urllib.request

BUF_SIZE = 8192
SOCKS5 = 0x05

class PhoneHealth:
    """Theo dõi sức khỏe phone"""
    def __init__(self, index, phone_info):
        self.index = index
        self.info = phone_info
        self.bind_ip = phone_info.get('bind_ip')  # legacy
        self.socks_port = phone_info.get('port', 10001)  # ADB forward port
        self.healthy = True
        self.last_check = 0
        self.last_healthy = time.time()
        self.fail_count = 0
        self.current_ip = None        # IP 4G thật (verified)
        self.is_rotating = False       # đang trong quá trình rotate
        self.ready = True              # sẵn sàng phục vụ traffic
        self.active_connections = 0
        self.total_connections = 0
        self.lock = threading.Lock()

    def mark_healthy(self, real_ip=None):
        self.healthy = True
        self.fail_count = 0
        self.last_healthy = time.time()
        self.last_check = time.time()
        self.ready = True
        if real_ip:
            self.current_ip = real_ip

    def mark_unhealthy(self):
        self.fail_count += 1
        self.last_check = time.time()
        if self.fail_count >= 3:
            self.healthy = False
            self.ready = False

    def start_rotate(self):
        self.is_rotating = True
        self.ready = False

    def end_rotate(self, new_ip=None):
        self.is_rotating = False
        if new_ip and new_ip != 'ERROR':
            self.current_ip = new_ip
            self.ready = True
            self.healthy = True
            self.fail_count = 0
        else:
            self.mark_unhealthy()

    def connect(self):
        with self.lock:
            self.active_connections += 1
            self.total_connections += 1

    def disconnect(self):
        with self.lock:
            self.active_connections = max(0, self.active_connections - 1)


class BackconnectGateway:

    def __init__(self, port=5000, phones=None, valid_keys=None, rotate_fn=None):
        self.port = port
        self.valid_keys = valid_keys or {'mimi-4g-proxy-2026'}
        self.rotate_fn = rotate_fn  # function(device_id) → new_ip

        # Phone health tracking
        self.phones = []
        for i, p in enumerate(phones or []):
            self.phones.append(PhoneHealth(i, p))

        self.sessions = {}
        self.round_robin = 0
        self.lock = threading.Lock()

        # Background workers
        threading.Thread(target=self._health_checker, daemon=True).start()
        threading.Thread(target=self._session_cleanup, daemon=True).start()

    # ========================
    # HEALTH CHECK — phát hiện phone chết
    # ========================

    def _health_checker(self):
        """Mỗi 15s kiểm tra phone qua SOCKS5 (ADB forward → EveryProxy)"""
        while True:
            for ph in self.phones:
                if ph.is_rotating:
                    continue
                try:
                    # Test connect qua ADB-forwarded SOCKS5
                    remote = self._connect_via_socks(ph, '1.1.1.1', 80)
                    if remote:
                        remote.close()
                        ph.mark_healthy()
                    else:
                        ph.mark_unhealthy()
                except:
                    ph.mark_unhealthy()
            time.sleep(15)

    def _connect_via_socks(self, ph, target_addr, target_port):
        """Kết nối tới target qua SOCKS5 proxy trên localhost (ADB forward → EveryProxy)"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect(('127.0.0.1', ph.socks_port))

            # SOCKS5 greeting (no auth)
            s.sendall(struct.pack('BBB', 0x05, 1, 0x00))
            resp = self._recv_exact(s, 2)
            if not resp or resp[1] != 0x00:
                s.close()
                return None

            # SOCKS5 connect request
            if isinstance(target_addr, str) and not target_addr.replace('.','').isdigit():
                # Domain
                encoded = target_addr.encode()
                req = struct.pack('BBBB', 0x05, 0x01, 0x00, 0x03)
                req += struct.pack('B', len(encoded)) + encoded
                req += struct.pack('!H', target_port)
            else:
                # IPv4
                req = struct.pack('BBBB', 0x05, 0x01, 0x00, 0x01)
                req += socket.inet_aton(target_addr)
                req += struct.pack('!H', target_port)
            s.sendall(req)

            # Read SOCKS5 reply — phải đọc ĐỦ bytes theo address type
            header = self._recv_exact(s, 4)  # VER, REP, RSV, ATYP
            if not header or header[1] != 0x00:
                s.close()
                return None
            atyp = header[3]
            if atyp == 0x01:    # IPv4: 4 + 2 = 6 bytes
                self._recv_exact(s, 6)
            elif atyp == 0x04:  # IPv6: 16 + 2 = 18 bytes
                self._recv_exact(s, 18)
            elif atyp == 0x03:  # Domain: 1(len) + domain + 2(port)
                dlen_bytes = self._recv_exact(s, 1)
                if dlen_bytes:
                    self._recv_exact(s, dlen_bytes[0] + 2)
            return s
        except:
            try: s.close()
            except: pass
            return None

    @staticmethod
    def _recv_exact(sock, n):
        """Đọc chính xác n bytes từ socket"""
        data = b''
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _verify_ip(self, phone_health):
        """Verify IP thật qua SOCKS5 — gọi SAU rotate"""
        try:
            s = self._connect_via_socks(phone_health, 'ipinfo.io', 80)
            if not s:
                return None
            s.sendall(b'GET /json HTTP/1.1\r\nHost: ipinfo.io\r\nConnection: close\r\n\r\n')
            data = b''
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            s.close()
            body = data.decode().split('\r\n\r\n', 1)[1]
            info = json.loads(body)
            return info.get('ip')
        except:
            return None

    # ========================
    # CHỌN PHONE THÔNG MINH
    # ========================

    def _get_healthy_phones(self):
        """Chỉ trả về phone sẵn sàng"""
        return [ph for ph in self.phones if ph.ready and ph.healthy and not ph.is_rotating]

    def _pick_phone(self, params):
        """Chọn phone — CÓ FAILOVER"""
        healthy = self._get_healthy_phones()
        if not healthy:
            # Không có phone nào healthy → thử phone ít lỗi nhất
            candidates = [ph for ph in self.phones if not ph.is_rotating]
            if candidates:
                candidates.sort(key=lambda p: p.fail_count)
                return candidates[0]
            return None

        # Chọn phone cụ thể
        if params.get('phone') is not None:
            idx = params['phone']
            # Tìm phone requested
            target = None
            for ph in self.phones:
                if ph.index == idx:
                    target = ph
                    break
            # Nếu phone requested healthy → dùng
            if target and target.ready and target.healthy:
                return target
            # FAILOVER: phone requested chết → chọn phone khác
            return healthy[0] if healthy else None

        # Sticky session
        if params.get('session'):
            sid = f"{params['key']}-{params['session']}"
            with self.lock:
                if sid in self.sessions:
                    s = self.sessions[sid]
                    # Check phone còn healthy không
                    ph = self.phones[s['phone_index']] if s['phone_index'] < len(self.phones) else None
                    if ph and ph.ready and ph.healthy:
                        s['last_used'] = time.time()
                        return ph
                    # Phone chết → xóa session, chọn phone mới
                    del self.sessions[sid]

                # Tạo session mới — chọn phone ít connection nhất
                healthy.sort(key=lambda p: p.active_connections)
                ph = healthy[0]
                expire = params.get('rotate', 5) or 5
                self.sessions[sid] = {
                    'phone_index': ph.index,
                    'created': time.time(),
                    'last_used': time.time(),
                    'expire_minutes': expire,
                }
                return ph

        # Round-robin
        with self.lock:
            ph = healthy[self.round_robin % len(healthy)]
            self.round_robin += 1
            return ph

    # ========================
    # PARSE USERNAME
    # ========================

    def _parse_username(self, username):
        params = {'key': '', 'session': None, 'rotate': 0, 'phone': None}
        parts = username.split('-')
        params['key'] = parts[0]
        i = 1
        while i < len(parts):
            if parts[i] == 'session' and i+1 < len(parts):
                params['session'] = parts[i+1]
                i += 2
            elif parts[i] == 'rotate' and i+1 < len(parts):
                try: params['rotate'] = int(parts[i+1])
                except: pass
                i += 2
            elif parts[i] == 'phone' and i+1 < len(parts):
                try: params['phone'] = int(parts[i+1]) - 1
                except: pass
                i += 2
            else:
                params['key'] += '-' + parts[i]
                i += 1
        return params

    # ========================
    # SESSION CLEANUP
    # ========================

    def _session_cleanup(self):
        while True:
            try:
                now = time.time()
                with self.lock:
                    expired = []
                    for sid, s in self.sessions.items():
                        if now - s['last_used'] > s['expire_minutes'] * 60:
                            expired.append(sid)
                    for sid in expired:
                        del self.sessions[sid]
            except:
                pass
            time.sleep(10)

    # ========================
    # MAIN SERVER
    # ========================

    def start(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('0.0.0.0', self.port))
        server.listen(512)
        print(f"[Gateway v2] :{self.port} | {len(self.phones)} phones", flush=True)

        while True:
            try:
                client, addr = server.accept()
                threading.Thread(target=self._handle, args=(client,), daemon=True).start()
            except:
                pass

    def _handle(self, client):
        try:
            self._process(client)
        except:
            pass
        finally:
            try: client.close()
            except: pass

    def _process(self, client):
        client.settimeout(30)

        # === Greeting ===
        data = client.recv(BUF_SIZE)
        if not data or data[0] != SOCKS5:
            return
        client.sendall(struct.pack('BB', SOCKS5, 0x02))

        # === Auth ===
        auth = client.recv(BUF_SIZE)
        if not auth or auth[0] != 0x01:
            client.sendall(b'\x01\x01')
            return
        ulen = auth[1]
        username = auth[2:2+ulen].decode('utf-8', errors='ignore')
        plen = auth[2+ulen]

        params = self._parse_username(username)
        if params['key'] not in self.valid_keys:
            client.sendall(b'\x01\x01')
            return
        client.sendall(b'\x01\x00')

        # === Request ===
        data = client.recv(BUF_SIZE)
        if not data or len(data) < 7:
            return
        ver, cmd, _, atyp = struct.unpack('BBBB', data[:4])
        if cmd != 0x01:
            client.sendall(struct.pack('BBBBIH', SOCKS5, 0x07, 0, 1, 0, 0))
            return

        if atyp == 0x01:
            target_addr = socket.inet_ntoa(data[4:8])
            target_port = struct.unpack('!H', data[8:10])[0]
        elif atyp == 0x03:
            dlen = data[4]
            target_addr = data[5:5+dlen].decode()
            target_port = struct.unpack('!H', data[5+dlen:7+dlen])[0]
        elif atyp == 0x04:
            target_addr = socket.inet_ntop(socket.AF_INET6, data[4:20])
            target_port = struct.unpack('!H', data[20:22])[0]
        else:
            return

        # === Chọn phone + RETRY ===
        ph = self._pick_phone(params)
        if not ph:
            client.sendall(struct.pack('BBBBIH', SOCKS5, 0x04, 0, 1, 0, 0))
            return

        # Nếu phone đang rotate → CHỜ (max 15s)
        waited = 0
        while ph.is_rotating and waited < 15:
            time.sleep(1)
            waited += 1

        # Try connect qua SOCKS5 (ADB forward → EveryProxy) — có FAILOVER
        remote = None
        tried = set()
        for attempt in range(min(3, len(self.phones))):
            if ph.index in tried:
                healthy = [p for p in self._get_healthy_phones() if p.index not in tried]
                if not healthy:
                    break
                ph = healthy[0]
            tried.add(ph.index)

            remote = self._connect_via_socks(ph, target_addr, target_port)
            if remote:
                break  # thành công
            ph.mark_unhealthy()

        if not remote:
            client.sendall(struct.pack('BBBBIH', SOCKS5, 0x05, 0, 1, 0, 0))
            return

        # Success
        ph.connect()
        bind = remote.getsockname()
        client.sendall(
            struct.pack('BBBB', SOCKS5, 0x00, 0x00, 0x01) +
            socket.inet_aton(bind[0]) + struct.pack('!H', bind[1])
        )

        # === Relay ===
        try:
            socks = [client, remote]
            while True:
                readable, _, err = select.select(socks, [], socks, 60)
                if err:
                    break
                if not readable:
                    break  # timeout
                for s in readable:
                    data = s.recv(BUF_SIZE)
                    if not data:
                        ph.disconnect()
                        remote.close()
                        return
                    target = remote if s is client else client
                    target.sendall(data)
        except:
            pass
        finally:
            ph.disconnect()
            try: remote.close()
            except: pass

    # ========================
    # STATUS (cho GUI/API)
    # ========================

    def status(self):
        return {
            'port': self.port,
            'phones': [{
                'index': ph.index + 1,
                'name': ph.info.get('name', ''),
                'bind_ip': ph.bind_ip,
                'real_ip': ph.current_ip,
                'healthy': ph.healthy,
                'ready': ph.ready,
                'rotating': ph.is_rotating,
                'active': ph.active_connections,
                'total': ph.total_connections,
                'fail_count': ph.fail_count,
            } for ph in self.phones],
            'sessions': len(self.sessions),
            'healthy_count': len(self._get_healthy_phones()),
        }


if __name__ == '__main__':
    phones = [{'bind_ip': '192.168.42.177', 'device_id': 'test', 'name': 'Nokia 3.1'}]
    gw = BackconnectGateway(port=5000, phones=phones)
    gw.start()
