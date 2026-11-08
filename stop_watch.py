"""停掉正在跑的监听（读 .watch.lock 里的 PID）。"""
import ctypes
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCK = HERE / ".watch.lock"


def main():
    if not LOCK.exists():
        print("没有找到锁文件，监听可能没在跑。")
        return 0
    try:
        pid = int(LOCK.read_text().strip())
    except Exception:
        LOCK.unlink(missing_ok=True)
        print("锁文件内容异常，已清除。")
        return 0

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        LOCK.unlink(missing_ok=True)
        print(f"PID {pid} 已不存在，清掉锁文件。")
        return 0
    ctypes.windll.kernel32.CloseHandle(handle)

    print(f"正在停止监听（PID {pid}）…")
    os.system(f"taskkill /PID {pid} /F >nul 2>&1")
    time.sleep(1.0)

    if LOCK.exists():
        LOCK.unlink(missing_ok=True)
    print("已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
