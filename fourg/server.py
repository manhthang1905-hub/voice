"""4G Proxy Manager — API Server
GUI: chay server_gui.py
Port API: 19800 | Port Gateway: 5000
"""
# === AN CUA SO CMD DEN cua moi subprocess (adb, ipconfig, powershell...) ===
# Server 4G goi adb rat nhieu -> moi lan hien 1 cua so console den, phien.
# Patch Popen them CREATE_NO_WINDOW -> chay an hoan toan (giong run.py).
import os as _os
if _os.name == "nt":
    import subprocess as _sp
    _CNW = 0x08000000
    _orig_init = _sp.Popen.__init__
    def _patched_init(self, *a, **k):
        if k.get("creationflags", 0) == 0:
            k["creationflags"] = _CNW
        _orig_init(self, *a, **k)
    _sp.Popen.__init__ = _patched_init

from flask import Flask, jsonify, request
from proxy_manager import ProxyManager
from smart_pool import SmartPool
from gateway import BackconnectGateway
import threading, time, json, socket, struct, uuid

app = Flask(__name__)
manager = ProxyManager()
pool = None
gateway = None

from datetime import datetime
import collections

# Shared log queue - GUI doc tu day
api_log = collections.deque(maxlen=100)

def api_log_add(msg):
    t = datetime.now().strftime("%H:%M:%S")
    entry = f"[{t}] {msg}"
    api_log.append(entry)
    print(entry, flush=True)

@app.before_request
def log_request():
    try:
        if request.path in ('/api-log', '/list', '/'):
            return
        src = request.remote_addr
        api_log_add(f"{request.method} {request.path} from {src}")
    except:
        pass

@app.route('/api-log')
def get_api_log():
    return jsonify({'log': list(api_log)})

def require_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('X-API-Key') or request.args.get('key')
        if not manager.check_auth(key):
            return jsonify({'error': 'Invalid API key'}), 401
        return f(*args, **kwargs)
    return decorated

# ==================== CORE ====================

@app.route('/')
def index():
    return jsonify({
        'name': '4G Proxy Manager',
        'gui': 'python server_gui.py',
        'gateway': f'socks5://{manager.api_key}:x@192.168.88.254:5000',
        'api': 'http://localhost:19800',
    })

@app.route('/list')
@require_auth
def list_proxies():
    return jsonify({'proxies': manager.list_proxies(), 'count': len(manager.phones)})

@app.route('/proxy/<device_id>')
@require_auth
def get_proxy(device_id):
    phone = manager.phones.get(device_id)
    if not phone: return jsonify({'error': 'Not found'}), 404
    return jsonify(phone.to_dict())

@app.route('/proxy/<device_id>/start', methods=['POST'])
@require_auth
def start_one(device_id):
    phone = manager.phones.get(device_id)
    if not phone: return jsonify({'error': 'Not found'}), 404
    manager.start_proxy(phone)
    return jsonify({'ok': True})

@app.route('/proxy/<device_id>/stop', methods=['POST'])
@require_auth
def stop_one(device_id):
    phone = manager.phones.get(device_id)
    if not phone: return jsonify({'error': 'Not found'}), 404
    manager.stop_proxy(phone)
    return jsonify({'ok': True})

@app.route('/rotate/<device_id>', methods=['POST'])
@require_auth
def rotate(device_id):
    wait = request.args.get('wait', 25, type=int)
    src = request.remote_addr
    api_log_add(f"ROTATE {device_id} (wait={wait}s) from {src}")
    new_ip = manager.rotate(device_id, wait=wait)
    if new_ip:
        api_log_add(f"ROTATE OK: {device_id} -> {new_ip}")
        return jsonify({'ok': True, 'device': device_id, 'new_ip': new_ip})
    api_log_add(f"ROTATE FAIL: {device_id}")
    return jsonify({'ok': False, 'error': 'Rotate failed'}), 500

@app.route('/rotate-all', methods=['POST'])
@require_auth
def rotate_all():
    return jsonify({'ok': True, 'results': manager.rotate_all()})

@app.route('/scan', methods=['POST'])
@require_auth
def scan():
    try:
        api_log_add("SCAN started")
        result = manager.smart_scan()
        api_log_add(f"SCAN done: {result.get('ready_count', 0)} devices")
        return jsonify(result)
    except Exception as e:
        api_log_add(f"SCAN error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/heal', methods=['POST'])
@require_auth
def heal():
    """TU FIX moi loi tren dien thoai (watchdog): dialog USB, EveryProxy, stay-awake.

    Goi khi tool phat hien 4G loi. -> ket qua heal + restart forward.
    """
    try:
        from adb_utils import heal_device
        results = []
        for dev in manager.phones.keys():
            r = heal_device(dev, everyproxy_port=manager.EVERYPROXY_PORT,
                            on_log=api_log_add)
            results.append({"device": dev, **r})
        # Sau khi heal -> restart forward (adb socks5) cho cac device
        try:
            manager.start_all()
        except Exception:
            pass
        return jsonify({"ok": True, "heal": results})
    except Exception as e:
        api_log_add(f"HEAL error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/start', methods=['POST'])
@require_auth
def start_all():
    manager.start_all()
    return jsonify({'ok': True, 'proxies': manager.list_proxies()})

@app.route('/stop', methods=['POST'])
@require_auth
def stop_all():
    manager.stop_all()
    return jsonify({'ok': True})

# ==================== TEST ====================

@app.route('/test/<device_id>')
@require_auth
def test_proxy(device_id):
    phone = manager.phones.get(device_id)
    if not phone: return jsonify({'ok': False, 'error': 'Not found'})
    import time as _time
    start = _time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect(('127.0.0.1', phone.port))
        s.sendall(b'\x05\x01\x00'); s.recv(2)
        target = b'ipinfo.io'
        s.sendall(b'\x05\x01\x00\x03' + bytes([len(target)]) + target + struct.pack('!H', 80))
        s.recv(10)
        s.sendall(b'GET /json HTTP/1.1\r\nHost: ipinfo.io\r\nConnection: close\r\n\r\n')
        data = b''
        while True:
            chunk = s.recv(4096)
            if not chunk: break
            data += chunk
        s.close()
        info = json.loads(data.decode().split('\r\n\r\n', 1)[1])
        return jsonify({'ok': True, 'ip': info.get('ip'), 'org': info.get('org'),
                        'city': info.get('city'), 'region': info.get('region'),
                        'latency_ms': int((_time.time() - start) * 1000)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/test/<device_id>/speed')
@require_auth
def test_speed(device_id):
    phone = manager.phones.get(device_id)
    if not phone: return jsonify({'ok': False, 'error': 'Not found'})
    import time as _time
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect(('127.0.0.1', phone.port))
        s.sendall(b'\x05\x01\x00'); s.recv(2)
        target = b'speed.cloudflare.com'
        s.sendall(b'\x05\x01\x00\x03' + bytes([len(target)]) + target + struct.pack('!H', 80))
        s.recv(10)
        s.sendall(b'GET /__down?bytes=102400 HTTP/1.1\r\nHost: speed.cloudflare.com\r\nConnection: close\r\n\r\n')
        start = _time.time()
        data = b''
        while True:
            chunk = s.recv(8192)
            if not chunk: break
            data += chunk
        elapsed = _time.time() - start
        s.close()
        size_kb = len(data) / 1024
        speed = (size_kb / 1024 * 8) / elapsed if elapsed > 0 else 0
        return jsonify({'ok': True, 'size_kb': round(size_kb, 1), 'time_ms': round(elapsed * 1000), 'speed_mbps': round(speed, 2)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

# ==================== CONFIG ====================

@app.route('/config', methods=['GET'])
@require_auth
def get_config():
    return jsonify({'api_key': manager.api_key, 'base_port': manager.base_port})

@app.route('/config', methods=['POST'])
@require_auth
def set_config():
    data = request.json
    if 'api_key' in data: manager.api_key = data['api_key']
    if 'base_port' in data: manager.base_port = data['base_port']
    manager.save_config()
    return jsonify({'ok': True})

# ==================== ACTION LINKS ====================

action_links = {}

@app.route('/action-link/create', methods=['POST'])
@require_auth
def create_action_link():
    data = request.json or {}
    device_id = data.get('device') or (list(manager.phones.keys())[0] if manager.phones else None)
    if not device_id: return jsonify({'error': 'No devices'}), 400
    link_id = uuid.uuid4().hex[:12]
    action_links[link_id] = {'device_id': device_id, 'action': data.get('action', 'rotate'), 'uses': 0}
    return jsonify({'link_id': link_id, 'url': f'http://localhost:19800/action/{link_id}'})

@app.route('/action/<link_id>')
def execute_action_link(link_id):
    link = action_links.get(link_id)
    if not link: return jsonify({'error': 'Invalid link'}), 404
    link['uses'] += 1
    if link['action'] == 'rotate':
        api_log_add(f"ACTION LINK rotate {link['device_id']}")
        new_ip = manager.rotate(link['device_id'], wait=25)
        api_log_add(f"ACTION LINK result: {new_ip}")
        return jsonify({'ok': True, 'new_ip': new_ip})
    return jsonify({'error': 'Unknown action'}), 400

@app.route('/action-links')
@require_auth
def list_action_links():
    return jsonify({'links': action_links})

# ==================== SETUP ====================

@app.route('/setup/<device_id>', methods=['POST'])
@require_auth
def setup_device(device_id):
    from adb_utils import setup_phone
    return jsonify({'ok': True, 'info': setup_phone(device_id)})

# ==================== SMART POOL ====================

@app.route('/pool/session/<client_id>')
@require_auth
def pool_session(client_id):
    if not pool: return jsonify({'error': 'Pool not ready'}), 500
    return jsonify(pool.get_session(client_id))

@app.route('/pool/new-ip/<client_id>', methods=['POST'])
@require_auth
def pool_new_ip(client_id):
    if not pool: return jsonify({'error': 'Pool not ready'}), 500
    return jsonify(pool.request_new_ip(client_id))

@app.route('/pool/release/<client_id>', methods=['POST'])
@require_auth
def pool_release(client_id):
    if pool: pool.release_session(client_id)
    return jsonify({'ok': True})

@app.route('/pool/any')
@require_auth
def pool_any():
    if not pool: return jsonify({'error': 'Pool not ready'}), 500
    result = pool.get_any()
    return jsonify(result) if result else (jsonify({'error': 'No phones'}), 503)

@app.route('/pool/all')
@require_auth
def pool_all():
    if not pool: return jsonify({'error': 'Pool not ready'}), 500
    return jsonify({'phones': pool.get_all()})

@app.route('/pool/stats')
@require_auth
def pool_stats():
    if not pool: return jsonify({'error': 'Pool not ready'}), 500
    return jsonify(pool.stats())

# ==================== MAIN ====================

if __name__ == '__main__':
    print("[4G Proxy] Scanning...", flush=True)
    manager.scan_devices()
    print(f"[4G Proxy] {len(manager.phones)} devices", flush=True)
    manager.start_all()
    pool = SmartPool(manager)

    phones_for_gw = [{'bind_ip': p.bind_ip, 'port': p.port, 'device_id': p.device_id, 'name': p.name}
                     for p in manager.phones.values()]
    if phones_for_gw:
        gateway = BackconnectGateway(port=5000, phones=phones_for_gw, valid_keys={manager.api_key})
        threading.Thread(target=gateway.start, daemon=True).start()
        print(f"[4G Proxy] Gateway: socks5://KEY:x@192.168.88.254:5000", flush=True)

    print("[4G Proxy] API: http://localhost:19800", flush=True)
    app.run(host='0.0.0.0', port=19800, debug=False, threaded=True)
