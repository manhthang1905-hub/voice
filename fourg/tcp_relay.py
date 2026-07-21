import select
import socket
import sys
import threading
import time


BUF = 65536
RECONNECT_DELAY = 2  # seconds


def relay(a, b):
    socks = [a, b]
    try:
        while True:
            r, _, _ = select.select(socks, [], [], 60)
            if not r:
                return
            for s in r:
                try:
                    data = s.recv(BUF)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                    return
    finally:
        for s in socks:
            try:
                s.close()
            except Exception:
                pass


def main():
    listen_port = int(sys.argv[1]) if len(sys.argv) > 1 else 10002
    target_host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    target_port = int(sys.argv[3]) if len(sys.argv) > 3 else 10001

    while True:  # Outer retry loop - never die
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", listen_port))
            server.listen(200)
            print(f"[TCP-RELAY] 0.0.0.0:{listen_port} -> {target_host}:{target_port}", flush=True)

            while True:  # Accept loop
                try:
                    client, _ = server.accept()
                    try:
                        remote = socket.create_connection((target_host, target_port), timeout=20)
                        threading.Thread(target=relay, args=(client, remote), daemon=True).start()
                    except Exception:
                        try:
                            client.close()
                        except Exception:
                            pass
                except Exception as e:
                    # Accept failed, continue accepting
                    time.sleep(0.1)
                    continue
        except Exception as e:
            # Server socket failed, retry after delay
            print(f"[TCP-RELAY] Server error, retrying in {RECONNECT_DELAY}s...", flush=True)
            time.sleep(RECONNECT_DELAY)
            continue


if __name__ == "__main__":
    main()
