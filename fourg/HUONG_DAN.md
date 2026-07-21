# 📡 4G Proxy — Hướng Dẫn Sử Dụng

## THÔNG TIN KẾT NỐI

```
Server IP:  192.168.88.254
Proxy:      socks5://mimi-4g-proxy-2026:x@192.168.88.254:5000
API:        http://192.168.88.254:19800
API Key:    mimi-4g-proxy-2026
```

**Auth:** Mọi API call cần 1 trong 2:
- Header: `X-API-Key: mimi-4g-proxy-2026`
- Query: `?key=mimi-4g-proxy-2026`

---

## 1. SỬ DỤNG PROXY (chỉ cần 1 dòng)

### IP tự xoay (mỗi request IP khác)
```
socks5://mimi-4g-proxy-2026:x@192.168.88.254:5000
```

### Giữ IP (sticky session) — dùng cho đăng nhập, duyệt web
```
socks5://mimi-4g-proxy-2026-session-abc123:x@192.168.88.254:5000
```
Muốn IP mới? Đổi `abc123` thành chuỗi khác.

### Chọn phone cụ thể
```
socks5://mimi-4g-proxy-2026-phone-1:x@192.168.88.254:5000
```

### Sticky + auto rotate mỗi N phút
```
socks5://mimi-4g-proxy-2026-session-s1-rotate-10:x@192.168.88.254:5000
```

---

## 2. PYTHON

```python
import requests

proxy = "socks5://mimi-4g-proxy-2026:x@192.168.88.254:5000"
proxies = {"http": proxy, "https": proxy}

r = requests.get("https://ipinfo.io/json", proxies=proxies)
print(r.json())
```

### Đổi IP qua API
```python
import requests

API = "http://192.168.88.254:19800"
KEY = "mimi-4g-proxy-2026"
headers = {"X-API-Key": KEY}

# Đổi IP 1 device
r = requests.post(f"{API}/rotate/DEVICE_ID", headers=headers)
print(r.json())  # {"ok": true, "new_ip": "..."}

# Đổi IP tất cả
r = requests.post(f"{API}/rotate-all", headers=headers)
```

---

## 3. CURL

```bash
# Test proxy
curl --proxy socks5://mimi-4g-proxy-2026:x@192.168.88.254:5000 https://ipinfo.io/json

# Đổi IP
curl -X POST "http://192.168.88.254:19800/rotate/DEVICE_ID" -H "X-API-Key: mimi-4g-proxy-2026"

# Danh sách devices
curl "http://192.168.88.254:19800/list?key=mimi-4g-proxy-2026"
```

---

## 4. NODE.JS

```javascript
const SocksProxyAgent = require('socks-proxy-agent');
const fetch = require('node-fetch');

const agent = new SocksProxyAgent('socks5://mimi-4g-proxy-2026:x@192.168.88.254:5000');
const res = await fetch('https://ipinfo.io/json', { agent });
console.log(await res.json());
```

---

## 5. SELENIUM / PLAYWRIGHT

### Selenium
```python
from selenium import webdriver
options = webdriver.ChromeOptions()
options.add_argument('--proxy-server=socks5://192.168.88.254:5000')
driver = webdriver.Chrome(options=options)
```

### Playwright
```python
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    browser = pw.chromium.launch(proxy={
        "server": "socks5://192.168.88.254:5000",
        "username": "mimi-4g-proxy-2026",
        "password": "x"
    })
```

---

## 6. SCRAPY

```python
# settings.py
DOWNLOADER_MIDDLEWARES = {
    'scrapy.downloadermiddlewares.httpproxy.HttpProxyMiddleware': 1,
}
HTTP_PROXY = 'socks5://mimi-4g-proxy-2026:x@192.168.88.254:5000'
```

---

## 7. ĐỔI IP

| Cách | Lệnh |
|---|---|
| Đổi session ID | Đổi `abc` trong proxy URL thành chuỗi khác |
| API rotate | `POST http://192.168.88.254:19800/rotate/DEVICE_ID?key=mimi-4g-proxy-2026` |
| Rotate tất cả | `POST http://192.168.88.254:19800/rotate-all?key=mimi-4g-proxy-2026` |
| Action link | `GET http://192.168.88.254:19800/action/LINK_ID` (không cần key) |

---

## 8. API REFERENCE

Tất cả endpoint dùng base URL: `http://192.168.88.254:19800`

| Endpoint | Method | Auth | Mô tả |
|---|---|---|---|
| `/list` | GET | Key | Danh sách proxy + trạng thái |
| `/proxy/<device>` | GET | Key | Thông tin 1 device |
| `/proxy/<device>/start` | POST | Key | Bật proxy cho device |
| `/proxy/<device>/stop` | POST | Key | Tắt proxy cho device |
| `/rotate/<device>` | POST | Key | Đổi IP (chờ ~25s) |
| `/rotate-all` | POST | Key | Đổi IP tất cả devices |
| `/scan` | POST | Key | Tìm phone mới qua USB |
| `/test/<device>` | GET | Key | Test IP qua ipinfo.io |
| `/test/<device>/speed` | GET | Key | Test tốc độ |
| `/action/<link>` | GET | Không | Đổi IP bằng link (tạo qua API) |
| `/pool/session/<id>` | GET | Key | Đăng ký sticky session |
| `/pool/new-ip/<id>` | POST | Key | Yêu cầu IP mới cho session |
| `/pool/any` | GET | Key | Lấy proxy ngay |
| `/pool/all` | GET | Key | Xem tất cả phone trong pool |
| `/pool/stats` | GET | Key | Thống kê pool |
| `/guard/status` | GET | Key | Trạng thái IP guard |
| `/config` | GET | Key | Xem config |
| `/config` | POST | Key | Sửa config |
| `/api-log` | GET | Không | Xem log API gần nhất |

### Ví dụ response

**GET /list**
```json
{
  "proxies": [
    {
      "id": "ce081608d35d550c05",
      "name": "SM-G930S",
      "model": "SM-G930S",
      "ip_4g": "10.242.45.152",
      "proxy_running": true,
      "port": 10001,
      "rotate_count": 5
    }
  ],
  "count": 1
}
```

**POST /rotate/DEVICE_ID**
```json
{
  "ok": true,
  "device": "ce081608d35d550c05",
  "new_ip": "10.55.123.45"
}
```

---

## 9. GIỮ IP SẠCH

| Platform | An toàn | Nguy hiểm |
|---|---|---|
| Google | 5-10 query/giờ | 50+/giờ |
| Facebook | 20-30 trang/giờ | 100+/giờ |
| Instagram | 20-30 action/giờ | 100+/giờ |
| Amazon | 30-60 trang/giờ | 200+/giờ |

### Nên
- ✅ Random delay 3-15 giây giữa requests
- ✅ Giữ cookies trong session
- ✅ Warm up IP 1-2 phút trước khi dùng nặng
- ✅ Báo captcha/block ngay khi gặp

### Không nên
- ❌ Request liên tục không nghỉ
- ❌ Đổi User-Agent mỗi request
- ❌ Dùng 1 IP quá 100 request liên tục
