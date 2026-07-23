"""ADB utilities - dieu khien Android phone qua USB hoac remote"""
import subprocess
import time
import re
import os
import socket
import xml.etree.ElementTree as ET

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tim adb.exe: tools/ hoac tools/platform-tools/ hoac PATH
_ADB_PATHS = [
    os.path.join(_PROJECT_ROOT, "tools", "adb.exe"),
    os.path.join(_PROJECT_ROOT, "tools", "platform-tools", "adb.exe"),
]
ADB = "adb"  # default: from PATH
for _p in _ADB_PATHS:
    if os.path.exists(_p):
        ADB = _p
        break

# Remote ADB: set ADB_HOST=192.168.88.254 de dieu khien phone qua mang
ADB_HOST = os.environ.get("ADB_HOST", "")
ADB_BASE = [ADB] + (["-H", ADB_HOST] if ADB_HOST else [])

# Debug mode: khong chay an cac lenh ADB/PowerShell nua.
CREATE_NO_WINDOW = 0

def run_adb(device_id, *args):
    """Chay ADB command, tra ve output"""
    cmd = ADB_BASE + ['-s', device_id] + list(args)
    try:
        out = subprocess.check_output(cmd, text=True, timeout=10,
                                       stderr=subprocess.DEVNULL,
                                       creationflags=CREATE_NO_WINDOW)
        return out.strip()
    except Exception as e:
        return f"ERROR: {e}"

def list_devices():
    """Liet ke tat ca Android devices dang ket noi"""
    try:
        out = subprocess.check_output(ADB_BASE + ['devices'], text=True, timeout=5,
                                       creationflags=CREATE_NO_WINDOW)
        devices = []
        for line in out.strip().split('\n')[1:]:
            parts = line.strip().split('\t')
            if len(parts) == 2 and parts[1] == 'device':
                devices.append(parts[0])
        return devices
    except:
        return []

def get_device_model(device_id):
    """Lấy tên model điện thoại"""
    return run_adb(device_id, 'shell', 'getprop', 'ro.product.model')

def get_device_ip(device_id):
    """Lấy IP 4G hiện tại của phone"""
    # rmnet_data0 = 4G interface trên hầu hết Android
    out = run_adb(device_id, 'shell', 'ip', 'addr', 'show')
    # Tìm IP trên rmnet (4G) interface
    for match in re.finditer(r'inet (\d+\.\d+\.\d+\.\d+)/\d+.*?(rmnet|ccmni|wwan)', out):
        return match.group(1)
    return None

def get_sim_operator(device_id):
    """Lấy tên nhà mạng"""
    return run_adb(device_id, 'shell', 'getprop', 'gsm.sim.operator.alpha')

def is_airplane_on(device_id):
    """Check airplane mode có bật không"""
    out = run_adb(device_id, 'shell', 'settings', 'get', 'global', 'airplane_mode_on')
    return out.strip() == '1'

def _find_airplane_toggle(device_id):
    """Tim TOA DO tam toggle airplane tu UI dump (khong hardcode - moi may/ROM khac).
    -> (x, y) hoac None."""
    try:
        xml = _dump_ui_xml_generic(device_id)
        if not xml:
            return None
        # Tim node Switch (toggle) - thuong la switch duy nhat tren trang airplane
        for m in re.finditer(r'class="android\.widget\.Switch"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
            x1, y1, x2, y2 = map(int, m.groups())
            return ((x1 + x2) // 2, (y1 + y2) // 2)
    except Exception:
        pass
    return None


def _dump_ui_xml_generic(device_id):
    try:
        run_adb(device_id, 'shell', 'uiautomator', 'dump', '/sdcard/_ap.xml')
        out = subprocess.run(ADB_BASE + ['-s', device_id, 'shell', 'cat', '/sdcard/_ap.xml'],
                             timeout=10, creationflags=CREATE_NO_WINDOW, capture_output=True)
        return out.stdout.decode('utf-8', errors='ignore')
    except Exception:
        return ""


def toggle_airplane(device_id, on=True):
    """Bật/tắt airplane mode qua UI tap (Nokia 3.1 cần cách này, broadcast/cmd cần root).

    Mở Settings > Airplane Mode, TIM toggle tu UI (khong hardcode toa do sai) roi tap.
    """
    # Wake + unlock truoc (dialog/toggle chi bam duoc khi man hinh sang)
    run_adb(device_id, 'shell', 'input', 'keyevent', 'KEYCODE_WAKEUP')
    run_adb(device_id, 'shell', 'wm', 'dismiss-keyguard')
    # Mở trang airplane mode settings
    run_adb(device_id, 'shell', 'am', 'start', '-a',
            'android.settings.AIRPLANE_MODE_SETTINGS')
    time.sleep(1.5)

    # TIM toggle tu UI (dung toa do that). Fallback (641,717) neu khong tim thay
    # (da do tren Nokia 3.1: toggle o [594,690][688,744] -> tam 641,717).
    point = _find_airplane_toggle(device_id) or (641, 717)
    run_adb(device_id, 'shell', 'input', 'tap', str(point[0]), str(point[1]))
    time.sleep(1)


def _re_forward_adb(device_id, port=10001, phone_port=1080):
    """Re-setup ADB forward sau khi airplane mode reset"""
    try:
        subprocess.run(ADB_BASE + ['-s', device_id, 'forward', '--remove-all'],
                      timeout=5, creationflags=CREATE_NO_WINDOW, capture_output=True)
        subprocess.run(ADB_BASE + ['-s', device_id, 'forward',
                       f'tcp:{port}', f'tcp:{phone_port}'],
                      timeout=5, creationflags=CREATE_NO_WINDOW, capture_output=True)
    except:
        pass


def _airplane_is_on(device_id):
    try:
        out = run_adb(device_id, 'shell', 'settings', 'get', 'global', 'airplane_mode_on')
        return (out or '').strip() == '1'
    except Exception:
        return None


def rotate_ip(device_id, wait=25):
    """Đổi IP bằng airplane mode ON/OFF + re-forward ADB.

    QUAN TRONG: VERIFY airplane THUC SU tat sau toggle. Neu tap truot -> airplane ket
    BAT -> data CHET (bug da gap). -> retry tat toi 3 lan cho toi khi airplane_mode_on=0.
    Returns: IP mới hoặc None
    """
    old_ip = get_device_ip(device_id)

    # Bật airplane (tap toggle) — verify da BAT
    toggle_airplane(device_id, on=True)
    time.sleep(2)
    for _ in range(3):
        if _airplane_is_on(device_id):
            break
        toggle_airplane(device_id, on=True)
        time.sleep(2)
    time.sleep(4)

    # Tắt airplane (tap toggle lại) — VERIFY da TAT (khong de ket BAT lam chet data)
    toggle_airplane(device_id, on=False)
    time.sleep(2)
    for _ in range(3):
        if _airplane_is_on(device_id) is False:
            break
        toggle_airplane(device_id, on=False)
        time.sleep(2)

    # Re-setup ADB forward (airplane mode có thể reset)
    time.sleep(3)
    _re_forward_adb(device_id)

    # Chờ kết nối lại — check mỗi 3 giây
    for _ in range(int(wait / 3)):
        time.sleep(3)
        new_ip = get_device_ip(device_id)
        if new_ip and new_ip != old_ip:
            return new_ip

    return get_device_ip(device_id)

def enable_usb_tethering(device_id):
    """Bật USB tethering trên phone"""
    # Cách 1: qua service call (không cần root trên hầu hết phone)
    run_adb(device_id, 'shell', 'svc', 'usb', 'setFunctions', 'rndis')
    time.sleep(2)
    # Cách 2: qua settings
    run_adb(device_id, 'shell', 'am', 'start', '-n',
            'com.android.settings/.TetherSettings')

def get_usb_tethering_interface(device_id):
    """Tìm network interface trên PC tương ứng với phone này
    Khi USB tethering bật, Windows tạo RNDIS adapter
    """
    # Trên Windows, dùng ipconfig để tìm RNDIS adapter
    try:
        out = subprocess.check_output(['ipconfig', '/all'], text=True,
                                       creationflags=CREATE_NO_WINDOW)
        # Tìm các adapter có "RNDIS" hoặc "Remote NDIS" hoặc "Android"
        adapters = []
        current_name = None
        current_ip = None
        for line in out.split('\n'):
            if 'adapter' in line.lower() and ':' in line:
                current_name = line.split(':')[0].strip()
            if 'IPv4' in line and current_name:
                ip = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                if ip:
                    current_ip = ip.group(1)
                    adapters.append({'name': current_name, 'ip': current_ip})
                    current_name = None
                    current_ip = None
        # Filter RNDIS/Android adapters (thường IP 192.168.42.x hoặc 192.168.44.x)
        rndis = [a for a in adapters if a['ip'].startswith('192.168.42.') or
                 a['ip'].startswith('192.168.44.')]
        return rndis
    except:
        return []

def get_device_info(device_id):
    """Lấy tất cả thông tin 1 device"""
    return {
        'id': device_id,
        'model': get_device_model(device_id),
        'operator': get_sim_operator(device_id),
        'ip_4g': get_device_ip(device_id),
        'airplane': is_airplane_on(device_id),
    }

# ========================
# PRO TRICKS
# ========================

def setup_stay_awake(device_id):
    """Giữ phone không tắt màn hình khi cắm USB — QUAN TRỌNG"""
    # Stay on khi cắm AC(1) + USB(2) + Wireless(4) = 7
    run_adb(device_id, 'shell', 'settings', 'put', 'global', 'stay_on_while_plugged_in', '7')
    # Tắt screen timeout
    run_adb(device_id, 'shell', 'settings', 'put', 'system', 'screen_off_timeout', '2147483647')


def dismiss_usb_dialog(device_id):
    """Tu dismiss dialog USB ('Dung USB de...') khi cam lai cap / mat dien.

    Khi cam lai cap USB, Android hien dialog hoi che do USB -> chan ADB/EveryProxy.
    Ham nay: wake + unlock + dismiss dialog (bam nut mac dinh / BACK), va ep USB=mtp.
    Khong can root.
    """
    try:
        # 1. Wake + unlock man hinh (dialog thuong hien khi man hinh sang)
        run_adb(device_id, 'shell', 'input', 'keyevent', 'KEYCODE_WAKEUP')
        run_adb(device_id, 'shell', 'wm', 'dismiss-keyguard')
        # 2. Neu co dialog USB -> BACK de dismiss (giu che do mac dinh)
        win = run_adb(device_id, 'shell', 'dumpsys', 'window', 'windows')
        low = (win or '').lower()
        if any(k in low for k in ('usbconfirm', 'usbconnection', 'usbhandler',
                                   'com.android.systemui/.usb')):
            run_adb(device_id, 'shell', 'input', 'keyevent', 'KEYCODE_BACK')
            time.sleep(0.5)
    except Exception:
        pass


def has_data_signal(device_id):
    """Dien thoai co song DATA thuc su khong? (khac EveryProxy/ADB).

    LUU Y (bug da gap): may 2 SIM -> co NHIEU mDataRegState/mDataConnectionState (moi khe 1).
    Khe trong = POWER_OFF, khe co SIM = connected. PHAI check BAT KY khe nao co data,
    khong chi doc khe dau (de tuong mat song oan -> dung oan).

    Cach chac nhat: mDataConnectionState=2 (CONNECTED) o BAT KY khe nao -> co data.
    -> True neu co data, False neu KHONG khe nao co, None neu khong doc duoc.
    """
    try:
        out = run_adb(device_id, 'shell', 'dumpsys', 'telephony.registry') or ""
        # mDataConnectionState: 0=disconnected,1=connecting,2=connected,3=suspended
        conn = [int(m.group(1)) for m in re.finditer(r'mDataConnectionState=(\d)', out)]
        if any(c == 2 for c in conn):
            return True
        # Hoac mDataRegState=0 (IN_SERVICE) o bat ky khe nao
        regs = [m.group(1) for m in re.finditer(r'mDataRegState=(\d)', out)]
        if any(r == '0' for r in regs):
            return True
        # Co doc duoc nhung KHONG khe nao co data
        if conn or regs:
            return False
    except Exception:
        pass
    return None   # khong doc duoc -> khong ket luan (KHONG dung oan)


def heal_device(device_id, everyproxy_port=1080, on_log=lambda *_: None):
    """TU FIX moi loi tren dien thoai (watchdog) — mien tool con chay.

    Chuoi phuc hoi theo thu tu:
      1. Wake + unlock + dismiss dialog USB (cam lai cap/mat dien -> dialog chan).
      2. Ep USB = mtp (khong hoi lai che do USB).
      3. Stay-awake (khong tat man hinh).
      4. Ensure EveryProxy chay (mo app + bat SOCKS neu tat).
    -> dict {ok, data_signal, everyproxy, note}.
    """
    result = {"ok": False, "data_signal": None, "everyproxy": False, "note": ""}
    # 1. Dialog USB + wake
    dismiss_usb_dialog(device_id)
    # 2. Ep USB mtp (tranh hoi lai) — best effort (co the lam adb reconnect ~vai giay)
    try:
        run_adb(device_id, 'shell', 'svc', 'usb', 'setFunctions', 'mtp')
    except Exception:
        pass
    # 3. Stay awake
    try:
        setup_stay_awake(device_id)
    except Exception:
        pass
    # 4. Data signal
    result["data_signal"] = has_data_signal(device_id)
    # 5. EveryProxy
    try:
        result["everyproxy"] = ensure_everyproxy(device_id, port=everyproxy_port, wait=8)
    except Exception:
        result["everyproxy"] = False

    if result["data_signal"] is False:
        result["note"] = "Dien thoai MAT SONG DATA (SIM het data / mat mang)"
    elif not result["everyproxy"]:
        result["note"] = "EveryProxy chua bat duoc"
    else:
        result["ok"] = True
        result["note"] = "OK"
    on_log(f"[heal] data={result['data_signal']} everyproxy={result['everyproxy']} "
           f"-> {result['note']}")
    return result

def fix_ttl(device_id):
    """Fix TTL = 64 để nhà mạng không biết đang tethering
    CẦN ROOT. Nếu không root thì bỏ qua.
    """
    # Check root
    out = run_adb(device_id, 'shell', 'su', '-c', 'id')
    if 'uid=0' not in out:
        return False  # Không có root

    # Set TTL = 64 cho mọi traffic ra
    run_adb(device_id, 'shell', 'su', '-c',
            'iptables -t mangle -A POSTROUTING -j TTL --ttl-set 64')
    return True

def setup_auto_tethering(device_id):
    """Tự bật USB tethering khi kết nối"""
    # Bật USB tethering qua service call
    # Mỗi Android version có service number khác nhau, thử phổ biến nhất
    run_adb(device_id, 'shell', 'svc', 'usb', 'setFunctions', 'rndis')

def is_everyproxy_running(device_id, port=1080):
    """Check EveryProxy SOCKS listener co dang mo tren phone khong."""
    out = run_adb(
        device_id, 'shell', 'sh', '-c',
        f'netstat -tln 2>/dev/null | grep ":{port} " || echo NOTFOUND')
    if 'NOTFOUND' not in out and str(port) in out:
        return True
    return _probe_everyproxy_via_forward(device_id, port=port)


def _probe_everyproxy_via_forward(device_id, port=1080):
    """Probe that SOCKS5 really answers via a temporary ADB forward."""
    probe_port = 19180
    try:
        subprocess.run(ADB_BASE + ['-s', device_id, 'forward', '--remove', f'tcp:{probe_port}'],
                       timeout=5, creationflags=CREATE_NO_WINDOW, capture_output=True)
        result = subprocess.run(
            ADB_BASE + ['-s', device_id, 'forward', f'tcp:{probe_port}', f'tcp:{port}'],
            timeout=5, creationflags=CREATE_NO_WINDOW, capture_output=True, text=True)
        if result.returncode != 0:
            return False

        with socket.create_connection(('127.0.0.1', probe_port), timeout=3) as s:
            s.settimeout(3)
            s.sendall(b'\x05\x01\x00')
            data = s.recv(2)
            return len(data) == 2 and data[0] == 0x05
    except Exception:
        return False
    finally:
        try:
            subprocess.run(ADB_BASE + ['-s', device_id, 'forward', '--remove', f'tcp:{probe_port}'],
                           timeout=5, creationflags=CREATE_NO_WINDOW, capture_output=True)
        except Exception:
            pass

def launch_everyproxy(device_id):
    """Mo app EveryProxy."""
    out = run_adb(
        device_id, 'shell', 'am', 'start', '-n',
        'com.gorillasoftware.everyproxy/.MainActivity')
    if out.startswith("ERROR"):
        out = run_adb(
            device_id, 'shell', 'monkey', '-p',
            'com.gorillasoftware.everyproxy',
            '-c', 'android.intent.category.LAUNCHER', '1')
    return not out.startswith("ERROR")


def _parse_bounds_center(bounds: str):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def _dump_ui_xml(device_id):
    try:
        subprocess.run(
            ADB_BASE + ['-s', device_id, 'shell', 'uiautomator', 'dump', '/sdcard/uidump.xml'],
            timeout=15, creationflags=CREATE_NO_WINDOW, capture_output=True)
        out = subprocess.run(
            ADB_BASE + ['-s', device_id, 'shell', 'cat', '/sdcard/uidump.xml'],
            timeout=15, creationflags=CREATE_NO_WINDOW, capture_output=True)
        xml = out.stdout.decode('utf-8', errors='ignore')
        return xml or ""
    except Exception:
        return ""


def _find_everyproxy_socks_toggle(xml: str):
    """Tim switch/toggle cua SOCKS Proxy trong UI dump.

    Uu tien:
    - node switch co text/desc lien quan SOCKS
    - switch checked=false o cung parent voi label "SOCKS Proxy"
    - node checkable/checkable=false nhung checked=false gan label SOCKS
    """
    if not xml or 'package="com.gorillasoftware.everyproxy"' not in xml:
        return None

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None

    nodes = list(root.iter('node'))

    def _is_off(node):
        return node.attrib.get('checked') == 'false'

    def _bounds(node):
        return _parse_bounds_center(node.attrib.get('bounds', ''))

    def _looks_like_toggle(node):
        klass = (node.attrib.get('class') or '').lower()
        clickable = node.attrib.get('clickable') == 'true'
        checkable = node.attrib.get('checkable') == 'true'
        return ('switch' in klass or 'toggle' in klass or checkable or clickable)

    def _has_socks_text(node):
        text = f"{node.attrib.get('text', '')} {node.attrib.get('content-desc', '')}".lower()
        return 'socks' in text and 'proxy' in text

    for node in nodes:
        if _has_socks_text(node) and _looks_like_toggle(node) and _is_off(node):
            pt = _bounds(node)
            if pt:
                return pt

    for parent in nodes:
        children = list(parent)
        if not children:
            continue
        has_label = any(_has_socks_text(ch) for ch in children)
        if not has_label:
            continue
        for ch in children:
            if _looks_like_toggle(ch) and _is_off(ch):
                pt = _bounds(ch)
                if pt:
                    return pt

    socks_label = None
    for node in nodes:
        if _has_socks_text(node):
            socks_label = _bounds(node)
            if socks_label:
                break

    if socks_label:
        sx, sy = socks_label
        best = None
        best_dist = None
        for node in nodes:
            if not (_looks_like_toggle(node) and _is_off(node)):
                continue
            pt = _bounds(node)
            if not pt:
                continue
            dist = abs(pt[0] - sx) + abs(pt[1] - sy)
            if best is None or dist < best_dist:
                best = pt
                best_dist = dist
        if best:
            return best

    # EveryProxy UI moi: label "SOCKS Proxy" va toggle la sibling cung parent
    for parent in nodes:
        children = list(parent)
        if not children:
            continue
        label = None
        toggles = []
        for ch in children:
            txt = (ch.attrib.get('text', '') or '').strip().lower()
            if txt == 'socks proxy':
                label = ch
            if ch.attrib.get('checkable') == 'true':
                pt = _bounds(ch)
                if pt:
                    toggles.append((pt, ch))
        if label and toggles:
            # Trên UI mới, toggle luôn nằm bên phải nhất của hàng.
            toggles.sort(key=lambda item: item[0][0], reverse=True)
            off = [item for item in toggles if item[1].attrib.get('checked') == 'false']
            target = (off[0] if off else toggles[0])[0]
            return target

    return None


def _wake_and_unlock(device_id):
    run_adb(device_id, 'shell', 'input', 'keyevent', 'KEYCODE_WAKEUP')
    time.sleep(0.5)
    run_adb(device_id, 'shell', 'wm', 'dismiss-keyguard')
    time.sleep(0.5)
    run_adb(device_id, 'shell', 'input', 'swipe', '300', '1200', '300', '300', '200')
    time.sleep(0.5)

def ensure_everyproxy(device_id, port=1080, wait=8):
    """Best-effort: mo EveryProxy va bat SOCKS Proxy neu dang tat."""
    if is_everyproxy_running(device_id, port=port):
        return True

    _wake_and_unlock(device_id)
    launch_everyproxy(device_id)
    time.sleep(2)

    try:
        xml = _dump_ui_xml(device_id)
        tap_point = _find_everyproxy_socks_toggle(xml)
        # EveryProxy UI moi: fallback toa do toggle SOCKS tren man hinh chinh.
        if not tap_point or tap_point[0] < 300:
            tap_point = (635, 384)
        if tap_point:
            run_adb(device_id, 'shell', 'input', 'tap', str(tap_point[0]), str(tap_point[1]))
            time.sleep(2)
    except Exception:
        pass

    for _ in range(max(1, wait)):
        if is_everyproxy_running(device_id, port=port):
            return True
        time.sleep(1)
    return False

def rotate_ip_verified(device_id, ip_history=None, max_retries=3, wait=25):
    """Đổi IP + VERIFY không trùng IP cũ
    ip_history: set() các IP đã dùng gần đây
    Nếu IP mới trùng → rotate lại (max 3 lần)
    """
    ip_history = ip_history or set()

    for attempt in range(max_retries):
        new_ip = rotate_ip(device_id, wait=wait)
        if new_ip and new_ip not in ip_history:
            return new_ip
        # IP trùng hoặc None → thử lại
        if attempt < max_retries - 1:
            import time
            time.sleep(2)

    return new_ip  # trả về dù trùng, ít nhất có IP

def setup_phone(device_id):
    """Setup 1 lần khi phone kết nối — gọi tất cả tricks"""
    setup_stay_awake(device_id)
    fix_ttl(device_id)
    return get_device_info(device_id)
