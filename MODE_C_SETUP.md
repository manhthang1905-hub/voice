# Mode C — Tạo voice KHÔNG cần tài khoản (anonymous + 4G)

Mode C tạo voice qua web demo elevenlabs.io **không đăng nhập**, KHÔNG cần master/TK.
Đây là mode **mặc định** của Auto Convert.

## Nguyên lý
- Gọi endpoint `POST /v1/text-to-speech/{voice_id}/stream/with-timestamps/anonymous`.
- `hcaptcha_token` lấy qua trình duyệt sạch (Camoufox — antidetect, fingerprint mới mỗi lần).
- Browser mint token qua **IP máy** (KHÔNG qua 4G); chỉ **request cuối** gửi qua **4G**.
- Giới hạn: **1000 ký tự/request**, **16 request/IP** (tự xoay 4G ở 15).

## Cài đặt trên máy mới
```bash
pip install -r requirements.txt          # đã có camoufox + playwright
python -m camoufox fetch                 # tải browser antidetect (~100MB, 1 lần)
```
Cần: **4G proxy đang chạy** (socks5 127.0.0.1:10001) — như Mode master.

## Bật/tắt
Tab Auto Convert → Cài đặt nâng cao:
- ☑ **MODE C: Tạo voice KHÔNG cần tài khoản** (mặc định BẬT)
- **Số Chrome song song**: 1-8 (máy khỏe tăng 3-6 để nhanh hơn)

Tắt Mode C = quay lại đường master (TK + workspace) như cũ.

## Đặc điểm 24/7 (tự phục hồi)
- **Checkpoint**: mỗi chunk lưu ra `.modec_ckpt/` (atomic). Crash/tắt máy → lần sau
  làm tiếp chunk thiếu, không mất công.
- **Validate voice cuối**: kiểm tra từng chunk + file cuối đủ thời lượng (ffprobe)
  trước khi ghi đè (atomic). Voice cuối luôn chuẩn + đủ.
- **Tự xoay IP 4G** khi cạn (16 req) hoặc lỗi. **Tự đổi Chrome** khi hỏng/flag.
- **Tự dọn process** camoufox mồ côi (chống rò RAM).
- **File lỗi → bỏ qua**, làm file khác (không treo hàng đợi).

## Kiến trúc code
- `core/anonymous_tts.py` — AnonymousSession (mint token), send_anonymous (gửi qua 4G).
- `core/mode_c_engine.py` — ModeCEngine (pool nhiều Chrome + xoay IP) + generate_file
  (checkpoint + validate + ghép).
- `ui/auto_tab.py` — AutoWorker._convert_mode_c (tích hợp vào Auto Convert).
