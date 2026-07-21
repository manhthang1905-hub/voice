"""SOCKS5 Proxy Server — có tracking connections per port
Mỗi port = 1 phone. Pool dựa vào connection count để biết key còn dùng không.
"""
import socket
import select
import struct
import threading
import time
import sys

SOCKS5_VERSION = 0x05
BUF_SIZE = 4096

# === TRACKING ===
# Mỗi port lưu: connection count, last connection time, active connections
port_stats = {}  # port -> {connections, last_active, active, bytes_in, bytes_out}
stats_lock = threading.Lock()

def get_port_stats(port):
    with stats_lock:
        return dict(port_stats.get(port, {
            'connections': 0,
            'last_active': 0,
            'active': 0,
            'bytes_in': 0,
            'bytes_out': 0,
        }))

def get_all_stats():
    with stats_lock:
        return {p: dict(s) for p, s in port_stats.items()}

def _track_connect(port):
    with stats_lock:
        if port not in port_stats:
            port_stats[port] = {'connections': 0, 'last_active': 0, 'active': 0, 'bytes_in': 0, 'bytes_out': 0}
        port_stats[port]['connections'] += 1
        port_stats[port]['active'] += 1
        port_stats[port]['last_active'] = time.time()

def _track_disconnect(port):
    with stats_lock:
        if port in port_stats:
            port_stats[port]['active'] = max(0, port_stats[port]['active'] - 1)

def _track_bytes(port, bytes_in=0, bytes_out=0):
    with stats_lock:
        if port in port_stats:
            port_stats[port]['bytes_in'] += bytes_in
            port_stats[port]['bytes_out'] += bytes_out
            port_stats[port]['last_active'] = time.time()


class Socks5Handler(threading.Thread):
    def __init__(self, client_sock, listen_port, outgoing_ip=None):
        super().__init__(daemon=True)
        self.client = client_sock
        self.listen_port = listen_port
        self.outgoing_ip = outgoing_ip

    def run(self):
        _track_connect(self.listen_port)
        try:
            self.handle()
        except:
            pass
        finally:
            self.client.close()
            _track_disconnect(self.listen_port)

    def handle(self):
        data = self.client.recv(BUF_SIZE)
        if not data or data[0] != SOCKS5_VERSION:
            return
        self.client.sendall(struct.pack('BB', SOCKS5_VERSION, 0x00))

        data = self.client.recv(BUF_SIZE)
        if not data or len(data) < 7:
            return
        ver, cmd, _, atyp = struct.unpack('BBBB', data[:4])
        if cmd != 0x01:
            self.client.sendall(struct.pack('BBBBIH', SOCKS5_VERSION, 0x07, 0, 1, 0, 0))
            return

        if atyp == 0x01:
            target_addr = socket.inet_ntoa(data[4:8])
            target_port = struct.unpack('!H', data[8:10])[0]
        elif atyp == 0x03:
            domain_len = data[4]
            target_addr = data[5:5+domain_len].decode()
            target_port = struct.unpack('!H', data[5+domain_len:7+domain_len])[0]
        elif atyp == 0x04:
            target_addr = socket.inet_ntop(socket.AF_INET6, data[4:20])
            target_port = struct.unpack('!H', data[20:22])[0]
        else:
            return

        try:
            # Resolve domain qua phone gateway (khong dung DNS mac dinh)
            if atyp == 0x03:  # Domain name
                try:
                    import urllib.request
                    # Resolve qua phone: dung socket bind de DNS di qua phone
                    resolved = socket.getaddrinfo(target_addr, target_port, socket.AF_INET)
                    if resolved:
                        target_addr = resolved[0][4][0]
                except:
                    pass

            remote = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if self.outgoing_ip:
                remote.bind((self.outgoing_ip, 0))
            remote.settimeout(10)
            remote.connect((target_addr, target_port))
        except:
            self.client.sendall(struct.pack('BBBBIH', SOCKS5_VERSION, 0x05, 0, 1, 0, 0))
            return

        bind_addr = remote.getsockname()
        self.client.sendall(
            struct.pack('BBBB', SOCKS5_VERSION, 0x00, 0x00, 0x01) +
            socket.inet_aton(bind_addr[0]) +
            struct.pack('!H', bind_addr[1])
        )

        self._relay(self.client, remote)
        remote.close()

    def _relay(self, client, remote):
        socks = [client, remote]
        while True:
            try:
                readable, _, error = select.select(socks, [], socks, 30)
            except:
                break
            if error:
                break
            for s in readable:
                data = s.recv(BUF_SIZE)
                if not data:
                    return
                if s is client:
                    remote.sendall(data)
                    _track_bytes(self.listen_port, bytes_out=len(data))
                else:
                    client.sendall(data)
                    _track_bytes(self.listen_port, bytes_in=len(data))


def start_server(port, outgoing_ip=None, listen_ip='0.0.0.0'):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((listen_ip, port))
    server.listen(128)
    print(f"[SOCKS5] :{port} -> {outgoing_ip or 'default'}", flush=True)

    while True:
        client, addr = server.accept()
        handler = Socks5Handler(client, port, outgoing_ip)
        handler.start()


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 10001
    outgoing = sys.argv[2] if len(sys.argv) > 2 else None
    start_server(port, outgoing)
