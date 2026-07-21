"""SOCKS5 Proxy via ADB — bypass USB tethering NAT issues.

Thay vì dùng USB tethering (cần NAT trên phone), proxy này:
1. Nhận kết nối SOCKS5 từ client (trên PC)
2. Dùng ADB để tạo TCP connection từ phone ra internet
3. Traffic đi thẳng qua phone's 4G — KHÔNG cần NAT, root, hay app

Cách dùng:
    python adb_socks5.py 10001 ES2BA80614012869
"""

import socket
import struct
import threading
import subprocess
import select
import sys
import os
import time

SOCKS5 = 0x05
BUF_SIZE = 65536

# ADB path (portable)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADB = os.path.join(_PROJECT_ROOT, "tools", "platform-tools", "adb.exe")

# Stats
port_stats = {}
stats_lock = threading.Lock()

def get_port_stats(port):
    with stats_lock:
        return dict(port_stats.get(port, {
            'connections': 0, 'last_active': 0, 'active': 0,
            'bytes_in': 0, 'bytes_out': 0,
        }))

def get_all_stats():
    with stats_lock:
        return {p: dict(s) for p, s in port_stats.items()}


class AdbSocksHandler(threading.Thread):
    """Handle 1 SOCKS5 connection: relay qua ADB shell trên phone."""

    def __init__(self, client_sock, listen_port, device_id):
        super().__init__(daemon=True)
        self.client = client_sock
        self.listen_port = listen_port
        self.device_id = device_id

    def run(self):
        with stats_lock:
            if self.listen_port not in port_stats:
                port_stats[self.listen_port] = {
                    'connections': 0, 'last_active': 0, 'active': 0,
                    'bytes_in': 0, 'bytes_out': 0
                }
            port_stats[self.listen_port]['connections'] += 1
            port_stats[self.listen_port]['active'] += 1
            port_stats[self.listen_port]['last_active'] = time.time()
        try:
            self._handle()
        except Exception:
            pass
        finally:
            self.client.close()
            with stats_lock:
                if self.listen_port in port_stats:
                    port_stats[self.listen_port]['active'] = max(
                        0, port_stats[self.listen_port]['active'] - 1)

    def _handle(self):
        # === SOCKS5 greeting ===
        data = self.client.recv(BUF_SIZE)
        if not data or data[0] != SOCKS5:
            return
        # No auth required
        self.client.sendall(struct.pack('BB', SOCKS5, 0x00))

        # === SOCKS5 request ===
        data = self.client.recv(BUF_SIZE)
        if not data or len(data) < 7:
            return
        ver, cmd, _, atyp = struct.unpack('BBBB', data[:4])
        if cmd != 0x01:  # CONNECT only
            self.client.sendall(struct.pack('BBBBIH', SOCKS5, 0x07, 0, 1, 0, 0))
            return

        # Parse target
        if atyp == 0x01:  # IPv4
            target_addr = socket.inet_ntoa(data[4:8])
            target_port = struct.unpack('!H', data[8:10])[0]
        elif atyp == 0x03:  # Domain
            domain_len = data[4]
            target_addr = data[5:5 + domain_len].decode()
            target_port = struct.unpack('!H', data[5 + domain_len:7 + domain_len])[0]
        elif atyp == 0x04:  # IPv6
            target_addr = socket.inet_ntop(socket.AF_INET6, data[4:20])
            target_port = struct.unpack('!H', data[20:22])[0]
        else:
            return

        # === Connect via ADB shell ===
        # Dùng toybox/busybox nc (netcat) trên phone để tạo TCP tunnel
        try:
            proc = subprocess.Popen(
                [ADB, '-s', self.device_id, 'exec-out',
                 f'exec 2>/dev/null; '
                 f'toybox nc {target_addr} {target_port} || '
                 f'busybox nc {target_addr} {target_port} || '
                 f'nc {target_addr} {target_port}'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000 if os.name == 'nt' else 0
            )
        except Exception:
            self.client.sendall(struct.pack('BBBBIH', SOCKS5, 0x01, 0, 1, 0, 0))
            return

        # Send SOCKS5 success reply
        self.client.sendall(
            struct.pack('BBBB', SOCKS5, 0x00, 0x00, 0x01) +
            socket.inet_aton('0.0.0.0') +
            struct.pack('!H', 0)
        )

        # === Relay data ===
        self._relay(self.client, proc)
        proc.terminate()

    def _relay(self, client, proc):
        """Relay data between SOCKS client and ADB process."""
        import msvcrt
        client.setblocking(False)

        stdout_fd = proc.stdout
        stdin_fd = proc.stdin

        while proc.poll() is None:
            # Read from client → write to phone
            try:
                data = client.recv(BUF_SIZE)
                if not data:
                    break
                stdin_fd.write(data)
                stdin_fd.flush()
                with stats_lock:
                    if self.listen_port in port_stats:
                        port_stats[self.listen_port]['bytes_out'] += len(data)
                        port_stats[self.listen_port]['last_active'] = time.time()
            except BlockingIOError:
                pass
            except Exception:
                break

            # Read from phone → write to client
            try:
                data = stdout_fd.read1(BUF_SIZE)  # Non-blocking read
                if not data:
                    break
                client.sendall(data)
                with stats_lock:
                    if self.listen_port in port_stats:
                        port_stats[self.listen_port]['bytes_in'] += len(data)
                        port_stats[self.listen_port]['last_active'] = time.time()
            except Exception:
                break


def start_server(port, device_id, listen_ip='0.0.0.0'):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((listen_ip, port))
    server.listen(128)
    print(f"[ADB-SOCKS5] :{port} -> device {device_id}", flush=True)

    while True:
        client, addr = server.accept()
        handler = AdbSocksHandler(client, port, device_id)
        handler.start()


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 10001
    device = sys.argv[2] if len(sys.argv) > 2 else None
    if not device:
        # Auto detect first device
        try:
            out = subprocess.check_output([ADB, 'devices'], text=True)
            for line in out.strip().split('\n')[1:]:
                if '\tdevice' in line:
                    device = line.split('\t')[0]
                    break
        except:
            pass
    if not device:
        print("Usage: python adb_socks5.py <port> <device_id>")
        sys.exit(1)
    start_server(port, device)
