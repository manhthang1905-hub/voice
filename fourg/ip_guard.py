"""
IP Guard — Giữ IP luôn sạch, không bao giờ bị spam

3 tầng bảo vệ:
1. PRE-CHECK  — IP mới phải qua kiểm tra trước khi dùng
2. MONITORING — theo dõi realtime, phát hiện sớm dấu hiệu bị flag
3. QUARANTINE — IP có vấn đề tự cách ly, cooldown trước khi dùng lại

IP Pool:
  HOT     → sẵn sàng, đã verify sạch
  WARMING → IP mới, đang warm up (15 phút dùng nhẹ)
  COOLDOWN→ vừa dùng xong, nghỉ 2-4 tiếng
  QUARANTINE → bị flag, nghỉ 24 tiếng, re-check trước khi dùng lại
"""

import socket
import json
import time
import threading
import urllib.request

# ========================
# IP REPUTATION CHECK
# ========================

DNSBL_SERVERS = [
    "zen.spamhaus.org",
    "bl.spamcop.net",
    "b.barracudacentral.org",
    "dnsbl.sorbs.net",
    "cbl.abuseat.org",
    "dnsbl-1.uceprotect.net",
]

def check_dnsbl(ip):
    """Check IP trên các blacklist DNS — MIỄN PHÍ, không cần API key"""
    reversed_ip = ".".join(reversed(ip.split(".")))
    listed_on = []
    for dnsbl in DNSBL_SERVERS:
        try:
            query = f"{reversed_ip}.{dnsbl}"
            socket.getaddrinfo(query, None, socket.AF_INET)
            listed_on.append(dnsbl)
        except socket.gaierror:
            pass  # Không listed = tốt
        except:
            pass
    return listed_on

def check_ipinfo(ip):
    """Check IP type (mobile/residential/datacenter) qua ipinfo.io — miễn phí"""
    try:
        data = json.loads(urllib.request.urlopen(f"http://ipinfo.io/{ip}/json", timeout=5).read())
        return {
            'ip': data.get('ip'),
            'org': data.get('org', ''),
            'city': data.get('city', ''),
            'region': data.get('region', ''),
            'country': data.get('country', ''),
            'is_mobile': any(x in data.get('org', '').lower() for x in
                           ['mobile', 'wireless', 'cellular', 'viettel', 'vinaphone', 'mobifone',
                            'vnpt', 'fpt telecom', 't-mobile', 'at&t', 'verizon']),
        }
    except:
        return None

def quick_reputation_check(ip):
    """Check nhanh IP có sạch không
    Returns: (is_clean: bool, reason: str, score: int)
    Score: 0-100, thấp = sạch
    """
    score = 0
    reasons = []

    # Check blacklists
    blacklists = check_dnsbl(ip)
    if blacklists:
        score += len(blacklists) * 20
        reasons.append(f"Listed on {len(blacklists)} blacklists: {', '.join(blacklists)}")

    # Check IP info
    info = check_ipinfo(ip)
    if info:
        if info.get('is_mobile'):
            score -= 10  # Mobile IP = bonus
        if 'hosting' in info.get('org', '').lower() or 'datacenter' in info.get('org', '').lower():
            score += 30
            reasons.append("Datacenter/hosting IP detected")

    score = max(0, min(100, score))
    is_clean = score < 30
    reason = '; '.join(reasons) if reasons else 'Clean'

    return is_clean, reason, score


# ========================
# IP POOL STATES
# ========================

class IPState:
    HOT = 'hot'           # Sẵn sàng dùng
    WARMING = 'warming'   # Đang warm up
    COOLDOWN = 'cooldown' # Nghỉ ngơi
    QUARANTINE = 'quarantine'  # Bị flag

class IPRecord:
    def __init__(self, ip, device_id):
        self.ip = ip
        self.device_id = device_id
        self.state = IPState.WARMING
        self.reputation_score = 0
        self.blacklists = []
        self.first_seen = time.time()
        self.last_checked = 0
        self.last_used = 0
        self.request_count = 0
        self.captcha_count = 0   # số lần gặp CAPTCHA
        self.error_count = 0     # số lần lỗi 429/403
        self.state_changed = time.time()
        self.cooldown_until = 0
        self.quarantine_until = 0

    def to_dict(self):
        now = time.time()
        return {
            'ip': self.ip,
            'device_id': self.device_id,
            'state': self.state,
            'score': self.reputation_score,
            'blacklists': self.blacklists,
            'requests': self.request_count,
            'captchas': self.captcha_count,
            'errors': self.error_count,
            'age_minutes': round((now - self.first_seen) / 60),
            'cooldown_left': max(0, round(self.cooldown_until - now)),
            'quarantine_left': max(0, round(self.quarantine_until - now)),
        }


class IPGuard:
    """Quản lý IP sạch"""

    def __init__(self):
        self.ips = {}  # ip -> IPRecord
        self.history = {}  # device_id -> set of used IPs (lookback)
        self.lock = threading.Lock()

        # Config
        self.warmup_seconds = 60        # warm up 1 phút (có thể tăng lên 15 phút cho production)
        self.cooldown_seconds = 7200    # 2 tiếng cooldown
        self.quarantine_seconds = 86400 # 24 tiếng quarantine
        self.max_requests_before_rotate = 100  # rotate sau 100 request
        self.max_captchas_before_quarantine = 2  # 2 captcha → quarantine

        # Background checker
        threading.Thread(target=self._background_check, daemon=True).start()

    def register_ip(self, ip, device_id):
        """Đăng ký IP mới (sau khi rotate)"""
        with self.lock:
            record = IPRecord(ip, device_id)
            self.ips[ip] = record

            # Add to history
            if device_id not in self.history:
                self.history[device_id] = set()
            self.history[device_id].add(ip)

        # Check reputation async
        threading.Thread(target=self._check_and_promote, args=(ip,), daemon=True).start()

    def _check_and_promote(self, ip):
        """Check reputation + promote từ WARMING → HOT"""
        record = self.ips.get(ip)
        if not record:
            return

        is_clean, reason, score = quick_reputation_check(ip)
        with self.lock:
            record.reputation_score = score
            record.last_checked = time.time()

            if not is_clean:
                # IP bẩn ngay từ đầu → quarantine
                record.state = IPState.QUARANTINE
                record.quarantine_until = time.time() + self.quarantine_seconds
                record.blacklists = check_dnsbl(ip)
            else:
                # Sạch → warming (chờ hết warmup period)
                record.state = IPState.WARMING
                # Schedule promote to HOT
                threading.Timer(self.warmup_seconds, self._promote_to_hot, args=(ip,)).start()

    def _promote_to_hot(self, ip):
        """Chuyển IP từ WARMING → HOT"""
        with self.lock:
            record = self.ips.get(ip)
            if record and record.state == IPState.WARMING:
                record.state = IPState.HOT
                record.state_changed = time.time()

    def get_clean_ip(self, device_id=None):
        """Lấy 1 IP sạch sẵn sàng dùng"""
        with self.lock:
            for ip, record in self.ips.items():
                if record.state != IPState.HOT:
                    continue
                if device_id and record.device_id != device_id:
                    continue
                return record
        return None

    def report_request(self, ip):
        """Báo cáo 1 request đã dùng IP này"""
        with self.lock:
            record = self.ips.get(ip)
            if record:
                record.request_count += 1
                record.last_used = time.time()

                # Quá nhiều request → cần rotate
                if record.request_count >= self.max_requests_before_rotate:
                    self._move_to_cooldown(record)
                    return 'should_rotate'
        return 'ok'

    def report_captcha(self, ip):
        """Báo cáo gặp CAPTCHA khi dùng IP này → CẢNH BÁO"""
        with self.lock:
            record = self.ips.get(ip)
            if record:
                record.captcha_count += 1
                if record.captcha_count >= self.max_captchas_before_quarantine:
                    self._move_to_quarantine(record)
                    return 'quarantined'
                else:
                    return 'warning'
        return 'unknown'

    def report_error(self, ip, error_code=None):
        """Báo cáo lỗi 429/403/block"""
        with self.lock:
            record = self.ips.get(ip)
            if record:
                record.error_count += 1
                if error_code in (429, 403):
                    # Rate limit hoặc block → cooldown ngay
                    self._move_to_cooldown(record)
                    return 'cooled_down'
                if record.error_count >= 5:
                    self._move_to_quarantine(record)
                    return 'quarantined'
        return 'ok'

    def _move_to_cooldown(self, record):
        record.state = IPState.COOLDOWN
        record.cooldown_until = time.time() + self.cooldown_seconds
        record.state_changed = time.time()

    def _move_to_quarantine(self, record):
        record.state = IPState.QUARANTINE
        record.quarantine_until = time.time() + self.quarantine_seconds
        record.state_changed = time.time()

    def _background_check(self):
        """Background: recover IPs từ cooldown/quarantine"""
        while True:
            try:
                now = time.time()
                with self.lock:
                    for ip, record in list(self.ips.items()):
                        # Cooldown hết hạn → re-check rồi promote
                        if record.state == IPState.COOLDOWN and now >= record.cooldown_until:
                            record.request_count = 0
                            record.error_count = 0
                            record.captcha_count = 0
                            record.state = IPState.HOT
                            record.state_changed = now

                        # Quarantine hết hạn → re-check
                        if record.state == IPState.QUARANTINE and now >= record.quarantine_until:
                            # Re-check reputation
                            threading.Thread(target=self._check_and_promote, args=(ip,), daemon=True).start()
            except:
                pass
            time.sleep(30)

    # ========================
    # STATUS
    # ========================

    def status(self):
        with self.lock:
            states = {IPState.HOT: 0, IPState.WARMING: 0, IPState.COOLDOWN: 0, IPState.QUARANTINE: 0}
            for record in self.ips.values():
                states[record.state] = states.get(record.state, 0) + 1

            return {
                'total_ips': len(self.ips),
                'hot': states[IPState.HOT],
                'warming': states[IPState.WARMING],
                'cooldown': states[IPState.COOLDOWN],
                'quarantine': states[IPState.QUARANTINE],
                'ips': [r.to_dict() for r in self.ips.values()],
            }

    def is_ip_unique(self, device_id, ip, lookback_count=50):
        """Check IP chưa dùng gần đây (unique IP enforcement)"""
        history = self.history.get(device_id, set())
        recent = list(history)[-lookback_count:]
        return ip not in recent
