"""
Smart Proxy Pool — 5 phone phục vụ nhiều tool

Tư duy:
- Client KHÔNG chọn phone, pool TỰ CHỌN phone tốt nhất
- Mỗi phone có cooldown sau khi rotate (tránh nhà mạng block)
- Xếp hàng rotate: 3 tool cùng đòi đổi IP → lần lượt, không đè nhau
- Mỗi client có session: cùng 1 session giữ nguyên phone (giống user thật)
- Load balancing: phone ít dùng nhất được ưu tiên
- Rate limit: max N rotate/phút/phone

Cách dùng:
    pool = SmartPool(manager)

    # Tool đăng ký session
    session = pool.get_session("tool-scraper-1")
    # → trả về proxy + phone được phân bổ

    # Khi cần IP mới
    pool.request_new_ip("tool-scraper-1")
    # → pool tự xử lý: cooldown, queue, chọn phone khác nếu cần

    # Tool hỏi "cho tôi 1 proxy bất kỳ, IP nào cũng được"
    proxy = pool.get_any()
"""

import threading
import time
import uuid
from collections import defaultdict
import ipaddress

class Session:
    """1 session = 1 tool đang dùng 1 phone"""
    def __init__(self, client_id, device_id, port):
        self.client_id = client_id
        self.device_id = device_id
        self.port = port
        self.created = time.time()
        self.last_used = time.time()
        self.request_count = 0
        self.rotate_count = 0

    def touch(self):
        self.last_used = time.time()
        self.request_count += 1

class PhoneState:
    """Trạng thái mở rộng cho mỗi phone"""
    def __init__(self, device_id):
        self.device_id = device_id
        self.active_sessions = 0       # bao nhiêu tool đang dùng
        self.last_rotate = 0           # timestamp lần rotate cuối
        self.rotate_count_minute = 0   # số lần rotate trong phút hiện tại
        self.minute_start = time.time()
        self.is_rotating = False       # đang trong quá trình rotate
        self.cooldown = 30             # giây chờ sau mỗi lần rotate
        self.ip_history = []           # lịch sử IP
        self.healthy = True

    def can_rotate(self, max_per_minute=3):
        """Phone có thể rotate không?"""
        if self.is_rotating:
            return False
        # Cooldown
        if time.time() - self.last_rotate < self.cooldown:
            return False
        # Rate limit
        now = time.time()
        if now - self.minute_start > 60:
            self.rotate_count_minute = 0
            self.minute_start = now
        if self.rotate_count_minute >= max_per_minute:
            return False
        return True

    def record_rotate(self, new_ip):
        self.last_rotate = time.time()
        self.rotate_count_minute += 1
        self.is_rotating = False
        if new_ip:
            self.ip_history.append({'ip': new_ip, 'time': time.time()})
            if len(self.ip_history) > 50:
                self.ip_history = self.ip_history[-50:]

class SmartPool:
    def __init__(self, proxy_manager):
        self.manager = proxy_manager
        self.sessions = {}           # client_id -> Session
        self.phone_states = {}       # device_id -> PhoneState
        self.rotate_queue = []       # [(client_id, callback), ...]
        self.lock = threading.Lock()
        self.max_rotate_per_minute = 3  # giới hạn mỗi phone
        self.session_timeout = 300      # 5 phút không gọi API → thu hồi
        self.idle_timeout = 60          # 60s không có traffic qua proxy → thu hồi
        self.max_sessions_per_phone = 5 # tối đa 5 tool/phone

        # Init phone states
        for dev_id in self.manager.phones:
            self.phone_states[dev_id] = PhoneState(dev_id)

        # Background: xử lý queue + cleanup
        self._start_workers()

    def _start_workers(self):
        threading.Thread(target=self._queue_worker, daemon=True).start()
        threading.Thread(target=self._cleanup_worker, daemon=True).start()

    # ========================
    # PHÂN BỔ PHONE THÔNG MINH
    # ========================

    def _pick_best_phone(self, exclude_devices=None):
        """Chọn phone tốt nhất: ít session nhất, không đang rotate, healthy"""
        exclude = exclude_devices or set()
        candidates = []
        for dev_id, state in self.phone_states.items():
            if dev_id in exclude:
                continue
            if not state.healthy:
                continue
            if state.active_sessions >= self.max_sessions_per_phone:
                continue
            # Score: ít session = tốt hơn
            score = state.active_sessions * 10
            # Bonus: phone lâu không rotate = IP "già" hơn = tốt hơn
            age = time.time() - state.last_rotate if state.last_rotate else 9999
            score -= min(age / 60, 10)  # tối đa -10 điểm
            candidates.append((score, dev_id))

        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]

    # ========================
    # SESSION MANAGEMENT
    # ========================

    def get_session(self, client_id):
        """Tool đăng ký session — được phân bổ 1 phone

        Returns: {
            'session_id': 'tool-scraper-1',
            'proxy': 'socks5://127.0.0.1:10001',
            'device': 'ES2BA...',
            'ip': '113.185.44.43'
        }
        """
        def _public_or_none(ip_val):
            try:
                ip_obj = ipaddress.ip_address(str(ip_val or "").strip())
                if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
                    return None
                return str(ip_obj)
            except Exception:
                return None

        with self.lock:
            # Đã có session?
            if client_id in self.sessions:
                s = self.sessions[client_id]
                s.touch()
                phone = self.manager.phones.get(s.device_id)
                if phone and not _public_or_none(phone.current_ip):
                    phone.current_ip = self.manager._get_public_ip(phone)
                return {
                    'session_id': client_id,
                    'proxy': f'socks5://127.0.0.1:{s.port}',
                    'device': s.device_id,
                    'ip': phone.current_ip if phone else None,
                    'request_count': s.request_count,
                }

            # Phân bổ phone mới
            dev_id = self._pick_best_phone()
            if not dev_id:
                return {'error': 'Không có phone trống'}

            phone = self.manager.phones[dev_id]
            if not _public_or_none(phone.current_ip):
                phone.current_ip = self.manager._get_public_ip(phone)
            session = Session(client_id, dev_id, phone.port)
            self.sessions[client_id] = session
            self.phone_states[dev_id].active_sessions += 1

            return {
                'session_id': client_id,
                'proxy': f'socks5://127.0.0.1:{phone.port}',
                'device': dev_id,
                'ip': phone.current_ip,
                'request_count': 0,
            }

    def release_session(self, client_id):
        """Tool trả session"""
        with self.lock:
            if client_id in self.sessions:
                s = self.sessions.pop(client_id)
                if s.device_id in self.phone_states:
                    self.phone_states[s.device_id].active_sessions -= 1

    # ========================
    # SMART ROTATE
    # ========================

    def request_new_ip(self, client_id, callback=None):
        """Tool yêu cầu IP mới

        Logic:
        1. Phone hiện tại có thể rotate? → rotate
        2. Phone đang cooldown? → chuyển sang phone khác
        3. Tất cả phone đang bận? → xếp hàng chờ

        Returns: {'status': 'rotating'/'queued'/'switched', ...}
        """
        with self.lock:
            session = self.sessions.get(client_id)
            if not session:
                return {'error': 'Chưa có session. Gọi get_session() trước.'}

            state = self.phone_states.get(session.device_id)

            # TH1: Phone hiện tại có thể rotate
            if state and state.can_rotate(self.max_rotate_per_minute):
                state.is_rotating = True
                threading.Thread(target=self._do_rotate,
                               args=(client_id, session.device_id, callback),
                               daemon=True).start()
                return {'status': 'rotating', 'device': session.device_id}

            # TH2: Chuyển sang phone khác có IP khác
            alt_dev = self._pick_best_phone(exclude_devices={session.device_id})
            if alt_dev:
                # Chuyển session sang phone mới
                old_dev = session.device_id
                self.phone_states[old_dev].active_sessions -= 1

                new_phone = self.manager.phones[alt_dev]
                session.device_id = alt_dev
                session.port = new_phone.port
                self.phone_states[alt_dev].active_sessions += 1

                result = {
                    'status': 'switched',
                    'old_device': old_dev,
                    'new_device': alt_dev,
                    'proxy': f'socks5://127.0.0.1:{new_phone.port}',
                    'ip': new_phone.current_ip,
                }
                if callback:
                    callback(result)
                return result

            # TH3: Tất cả bận → xếp hàng
            self.rotate_queue.append((client_id, callback))
            return {
                'status': 'queued',
                'position': len(self.rotate_queue),
                'message': f'Tất cả phone đang bận. Vị trí hàng đợi: {len(self.rotate_queue)}'
            }

    def _do_rotate(self, client_id, device_id, callback=None):
        """Thực hiện rotate (chạy trong thread)"""
        try:
            new_ip = self.manager.rotate(device_id, wait=8)
            with self.lock:
                state = self.phone_states.get(device_id)
                if state:
                    state.record_rotate(new_ip)
            result = {
                'status': 'done',
                'device': device_id,
                'new_ip': new_ip,
                'client': client_id,
            }
            if callback:
                callback(result)
        except Exception as e:
            with self.lock:
                state = self.phone_states.get(device_id)
                if state:
                    state.is_rotating = False
            if callback:
                callback({'status': 'error', 'error': str(e)})

    # ========================
    # QUICK ACCESS (không cần session)
    # ========================

    def get_any(self):
        """Lấy 1 proxy bất kỳ, IP nào cũng được
        Dùng cho tool chỉ cần 1 request nhanh
        """
        dev_id = self._pick_best_phone()
        if not dev_id:
            return None
        phone = self.manager.phones[dev_id]
        return {
            'proxy': f'socks5://127.0.0.1:{phone.port}',
            'device': dev_id,
            'ip': phone.current_ip,
        }

    def get_all(self):
        """Lấy tất cả proxy + trạng thái + traffic"""
        try:
            from socks5_server import get_port_stats
        except:
            get_port_stats = None

        result = []
        for dev_id, state in self.phone_states.items():
            phone = self.manager.phones.get(dev_id)
            if not phone:
                continue
            cooldown_left = max(0, state.cooldown - (time.time() - state.last_rotate)) if state.last_rotate else 0

            # Traffic stats
            traffic = {}
            if get_port_stats:
                stats = get_port_stats(phone.port)
                last_active = stats.get('last_active', 0)
                traffic = {
                    'total_connections': stats.get('connections', 0),
                    'active_connections': stats.get('active', 0),
                    'bytes_in': stats.get('bytes_in', 0),
                    'bytes_out': stats.get('bytes_out', 0),
                    'last_traffic': round(time.time() - last_active) if last_active else None,
                    'is_idle': (time.time() - last_active > self.idle_timeout) if last_active else True,
                }

            # Sessions dùng phone này
            using_keys = [cid for cid, s in self.sessions.items() if s.device_id == dev_id]

            result.append({
                'device': dev_id,
                'name': phone.name,
                'proxy': f'socks5://127.0.0.1:{phone.port}',
                'ip': phone.current_ip,
                'sessions': state.active_sessions,
                'max_sessions': self.max_sessions_per_phone,
                'using_keys': using_keys,
                'is_rotating': state.is_rotating,
                'cooldown_left': round(cooldown_left),
                'rotates_this_minute': state.rotate_count_minute,
                'max_rotates_minute': self.max_rotate_per_minute,
                'healthy': state.healthy,
                'ip_history': state.ip_history[-5:],
                'traffic': traffic,
            })
        return result

    # ========================
    # BACKGROUND WORKERS
    # ========================

    def _queue_worker(self):
        """Xử lý hàng đợi rotate"""
        while True:
            try:
                with self.lock:
                    if not self.rotate_queue:
                        pass
                    else:
                        # Tìm phone có thể rotate
                        for dev_id, state in self.phone_states.items():
                            if state.can_rotate(self.max_rotate_per_minute):
                                client_id, callback = self.rotate_queue.pop(0)
                                session = self.sessions.get(client_id)
                                if session:
                                    # Chuyển session sang phone này
                                    old_dev = session.device_id
                                    self.phone_states[old_dev].active_sessions -= 1
                                    session.device_id = dev_id
                                    phone = self.manager.phones[dev_id]
                                    session.port = phone.port
                                    self.phone_states[dev_id].active_sessions += 1
                                    state.is_rotating = True
                                    threading.Thread(target=self._do_rotate,
                                                   args=(client_id, dev_id, callback),
                                                   daemon=True).start()
                                break
            except:
                pass
            time.sleep(1)

    def _cleanup_worker(self):
        """Dọn session hết hạn — DỰA VÀO TRAFFIC THẬT, không phải API call"""
        while True:
            try:
                now = time.time()
                expired = []

                # Import stats từ SOCKS5 server
                try:
                    from socks5_server import get_port_stats
                except:
                    get_port_stats = None

                with self.lock:
                    for cid, session in self.sessions.items():
                        idle = True

                        # Check traffic thật qua proxy port
                        if get_port_stats:
                            stats = get_port_stats(session.port)
                            last_active = stats.get('last_active', 0)
                            if last_active > 0 and now - last_active < self.idle_timeout:
                                idle = False
                                session.touch()  # cập nhật last_used

                        # Fallback: check API last_used
                        if idle and now - session.last_used > self.idle_timeout:
                            expired.append(cid)

                for cid in expired:
                    self.release_session(cid)
                    # Log
                    print(f"[Pool] Released idle session: {cid}", flush=True)

            except:
                pass
            time.sleep(10)

    # ========================
    # STATS
    # ========================

    def stats(self):
        """Thống kê tổng quan"""
        total_phones = len(self.phone_states)
        active_sessions = len(self.sessions)
        queue_size = len(self.rotate_queue)
        healthy = sum(1 for s in self.phone_states.values() if s.healthy)
        available = sum(1 for s in self.phone_states.values()
                       if s.can_rotate(self.max_rotate_per_minute))
        return {
            'total_phones': total_phones,
            'healthy_phones': healthy,
            'active_sessions': active_sessions,
            'queue_size': queue_size,
            'phones_can_rotate': available,
            'max_sessions_per_phone': self.max_sessions_per_phone,
            'max_total_sessions': total_phones * self.max_sessions_per_phone,
        }
