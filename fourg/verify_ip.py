import requests

services = [
    'http://checkip.amazonaws.com',
    'http://ifconfig.me/ip',
    'http://ipecho.net/plain',
    'http://api.ipify.org',
]

print("=== IP MAY (KHONG PROXY) ===")
for url in services:
    try:
        r = requests.get(url, timeout=10)
        print(f"  {url}: {r.text.strip()}")
    except Exception as e:
        print(f"  {url}: ERROR {e}")

print("\n=== IP QUA PROXY (port 10001) ===")
p = {'http': 'socks5h://127.0.0.1:10001', 'https': 'socks5h://127.0.0.1:10001'}
for url in services:
    try:
        r = requests.get(url, timeout=15, proxies=p)
        print(f"  {url}: {r.text.strip()}")
    except Exception as e:
        print(f"  {url}: ERROR {e}")

print("\n=== TAT PROXY, test lai IP MAY nhieu lan ===")
for i in range(3):
    try:
        r = requests.get('http://checkip.amazonaws.com', timeout=10)
        print(f"  Try {i+1}: {r.text.strip()}")
    except Exception as e:
        print(f"  Try {i+1}: ERROR {e}")
