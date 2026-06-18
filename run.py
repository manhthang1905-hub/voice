"""
11Lab Voice Tool — Chuyển text thành voice.
Chạy: python run.py

Ghi chú: 4G proxy là TOOL RIÊNG (chạy server 4G ở máy/điện thoại riêng).
Tool này chỉ KẾT NỐI tới 4G qua config/proxy.json (chỉnh ở tab "4G Proxy").
"""
import sys
import os
import traceback
import datetime

# Khi chay an (VBS launcher), stdout/stderr co the la None -> redirect ve devnull
if sys.stdout is None or sys.stderr is None:
    _devnull = open(os.devnull, 'w', encoding='utf-8')
    if sys.stdout is None:
        sys.stdout = _devnull
    if sys.stderr is None:
        sys.stderr = _devnull

sys.path.insert(0, os.path.dirname(__file__))


if __name__ == "__main__":
    # Bat moi loi Python khong xu ly duoc -> ghi log ra file
    _log_path = os.path.join(os.path.dirname(__file__), "logs", "crash.log")
    os.makedirs(os.path.dirname(_log_path), exist_ok=True)

    def _excepthook(exc_type, exc_value, exc_tb):
        msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(_log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[{ts}] UNHANDLED EXCEPTION:\n{msg}\n")
        except Exception:
            pass
        print(f"[CRASH] {ts}\n{msg}")
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    try:
        from ui.voice_tool import main
        main()
    except SystemExit:
        pass  # app.exec_() goi sys.exit() -> binh thuong
    except Exception as e:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(_log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[{ts}] MAIN CRASH: {traceback.format_exc()}\n")
        except Exception:
            pass
        print(f"[CRASH] main(): {e}")
    finally:
        # Force kill tat ca thread con lai (AutoWorker, v.v.) -> tranh zombie
        os._exit(0)
