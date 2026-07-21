import select
import socket
import struct
import sys
import threading

SOCKS5 = 0x05
BUF = 65536


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def _relay(a, b):
    socks = [a, b]
    try:
        while True:
            r, _, _ = select.select(socks, [], [], 60)
            if not r:
                break
            for s in r:
                data = s.recv(BUF)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    finally:
        for s in socks:
            try:
                s.close()
            except Exception:
                pass


def _handle(client):
    remote = None
    try:
        hello = _recv_exact(client, 2)
        if not hello or hello[0] != SOCKS5:
            return
        nmethods = hello[1]
        if nmethods:
            _recv_exact(client, nmethods)
        client.sendall(b"\x05\x00")

        req = _recv_exact(client, 4)
        if not req or req[0] != SOCKS5 or req[1] != 0x01:
            client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            return
        atyp = req[3]
        if atyp == 0x01:
            addr = socket.inet_ntoa(_recv_exact(client, 4))
        elif atyp == 0x03:
            dlen = _recv_exact(client, 1)[0]
            addr = _recv_exact(client, dlen).decode("utf-8", errors="ignore")
        elif atyp == 0x04:
            addr = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16))
        else:
            client.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
            return
        port = struct.unpack("!H", _recv_exact(client, 2))[0]

        remote = socket.create_connection((addr, port), timeout=20)
        bind_host, bind_port = remote.getsockname()[:2]
        try:
            bind_ip = socket.inet_aton(bind_host)
            client.sendall(b"\x05\x00\x00\x01" + bind_ip + struct.pack("!H", bind_port))
        except OSError:
            client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        _relay(client, remote)
        remote = None
    except Exception as e:
        try:
            print(f"[PC-SOCKS5] handle error: {e}", flush=True)
        except Exception:
            pass
        try:
            client.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
        except Exception:
            pass
    finally:
        try:
            client.close()
        except Exception:
            pass
        if remote is not None:
            try:
                remote.close()
            except Exception:
                pass


def start_server(port):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(200)
    print(f"[PC-SOCKS5] :{port}", flush=True)
    while True:
        client, _ = server.accept()
        threading.Thread(target=_handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 10001
    start_server(port)
