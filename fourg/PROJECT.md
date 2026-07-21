# Tool 4G Proxy v2

## Kiến trúc (chuẩn ngành)
```
Tool khác ��� socks5://KEY:x@192.168.88.254:5000 → Gateway ��� Phone → 4G → Internet
                                              ↑
                                    Health check + Failover
                                    Sticky session + Round-robin
                                    Retry 3 phone nếu fail
```

## Tính năng
- **Backconnect Gateway** — 1 endpoint duy nhất, tham số trong username
- **Smart Pool** — phân bổ phone, queue rotate, cooldown, traffic tracking
- **Health Check** — 15s/lần, phone chết tự loại, tự recover
- **Failover** — phone fail → thử phone khác, tool không bị lỗi
- **Sticky Session** — giữ IP, đổi session ID = đổi IP
- **Unique IP** — verify không trùng IP cũ, retry 3 lần
- **Action Links** — URL đổi IP không cần auth
- **TTL Fix** — ẩn tethering khỏi nhà mạng (cần root)
- **Stay Awake** — phone không tắt màn hình
- **Traffic Tracking** — đếm bytes/connection per port

## Files
```
4g/
├── gateway.py         ← Backconnect proxy (port 5000)
├── server.py          ← API + GUI (port 19800)  
├── proxy_manager.py   ← Quản lý phones + proxies
├── smart_pool.py      ← Smart pool (phân bổ, queue, cooldown)
├── socks5_server.py   ← SOCKS5 server + traffic tracking
├── adb_utils.py       ← ADB commands + pro tricks
├── proxy4g.py         ← SDK cho tool khác
├── config.json        ← Config
├─�� examples.py        ← Ví dụ sử dụng
└── refs/              ← Repos tham khảo
```

## Cách dùng
```
# Tool khác — chỉ cần 1 dòng:
socks5://KEY:x@192.168.88.254:5000

# Sticky session:
socks5://KEY-session-abc:x@192.168.88.254:5000

# Đổi IP = đổi session:
socks5://KEY-session-xyz:x@192.168.88.254:5000

# Action link (không cần auth):
curl http://localhost:19800/action/LINK_ID
```

## Tham khảo
- iProxy.online — 15 port/phone, TCP fingerprint, action links
- Proxidize — tunnel architecture, shared proxy, bandwidth limits  
- XProxy.io — Vietnamese, REST API, lifetime license
- Bright Data — backconnect gateway, session params in username
- SOAX — session length, unique IP enforcement
