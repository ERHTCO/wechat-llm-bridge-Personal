"""`app.py` 只能有一个 main()，而且 --watch 必须自己退出。

    .venv\\Scripts\\python.exe tests\\test_entry.py

背景：删旧面板时切字符串切重了，文件末尾 `if __name__ == "__main__": main()`
出现了两次。后果是 `--watch` 先跑监听、`run_watch()` 把 sys.argv 改成
`["watch_uia.py"]`，第二次 main() 看到没有参数就当 GUI 起——于是点「启动」
除了起监听，还会弹出一个新窗口，而且那个监听进程永远不退。

这两条都在这里钉住：入口只能有一次 main()，--watch 在没窗口可听时必须退出。
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def _windows_of(pid):
    """列出某个进程的顶层窗口 (类名, 标题)。"""
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    out = []
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        p = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            title = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(hwnd, title, 256)
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, cls, 256)
            out.append((cls.value, title.value))
        return True

    u.EnumWindows(CB(cb), 0)
    return out


def main():
    src = (HERE / "app.py").read_text(encoding="utf-8")
    n = src.count('if __name__ == "__main__":')
    check("入口只有一次 main()", n == 1, f"出现了 {n} 次")
    check("文件末尾是 sys.exit(main())", src.rstrip().endswith("sys.exit(main())"),
          src.rstrip()[-40:])

    # --watch 不能「跑完监听又掉进 GUI」。
    #
    # 别用「进程会不会自己退出」判断：微信里开着聊天窗口时，监听本来就应该
    # 一直跑（实测踩过，40 秒超时误报）。真正要验的是**它有没有开出图形窗口**。
    import time

    lock = HERE / ".watch.lock"
    had_lock = lock.exists()
    p = subprocess.Popen([sys.executable, str(HERE / "app.py"), "--watch"],
                         cwd=str(HERE), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, encoding="utf-8", errors="replace")
    time.sleep(8)
    windows = _windows_of(p.pid)
    try:
        p.terminate()
        p.wait(timeout=10)
    except Exception:
        p.kill()
    # 这次跑出来的锁要清掉，别把「锁残留」留给用户
    if not had_lock and lock.exists():
        lock.unlink(missing_ok=True)

    gui_windows = [w for w in windows
                   if w[0].startswith("Qt") or "微信桥" in w[1]]
    check("--watch 没有开出图形窗口", not gui_windows, gui_windows)
    check("--watch 至少起了个进程", p.pid is not None, p.pid)

    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
