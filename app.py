"""微信桥 · 桌面版入口。

不带参数 → 打开控制面板
--bridge  → 只跑 HTTP 桥（后台）
--watch   → 只跑监听（后台）

打包成 exe 之后，数据（config.json / prompt.txt / data/ / images/）就放在 exe 旁边。
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

APP = (Path(sys.executable).resolve().parent
       if getattr(sys, "frozen", False)
       else Path(__file__).resolve().parent)
os.environ.setdefault("WXBRIDGE_ROOT", str(APP))

PORT = 8765
BRIDGE_LOG = APP / "bridge.log"
WATCH_LOG = APP / "watch_uia_log.txt"


def _cmd(kind):
    flag = f"--{kind}"
    if getattr(sys, "frozen", False):
        return [sys.executable, flag]
    return [sys.executable, str(Path(__file__).resolve()), flag]


def port_open(port=PORT, timeout=0.3):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _ensure_streams(name):
    """打包成无控制台 exe 后 sys.stdout/stderr 可能是 None，print 会直接抛异常；
    也可能继承了父进程的句柄，按系统默认编码（中文 Windows 是 GBK）解码，
    一遇到群名里的 emoji（💗）就 UnicodeEncodeError。

    这里统一改成 UTF-8；没有流就接到 exe 旁边的日志文件。
    """
    path = APP / f"{name}.log"
    f = None
    for attr in ("stdout", "stderr"):
        stream = getattr(sys, attr, None)
        if stream is None:
            if f is None:
                f = open(path, "a", encoding="utf-8", buffering=1, errors="replace")
            setattr(sys, attr, f)
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def run_bridge():
    _ensure_streams("bridge")
    import bridge

    bridge.main()


def run_watch():
    _ensure_streams("watch")
    sys.argv = ["watch_uia.py"]      # watch_uia.main() 会解析命令行
    import watch_uia

    # 返回值要传出去：找不到窗口时它是 1，丢掉的话外面只能看到退出码 0，
    # GUI 判断「起来就退了」时就没法区分正常结束和失败。
    return watch_uia.main()


def _crash_log():
    """窗口化 exe 没有控制台，崩了就什么都不显示。把栈写到 exe 旁边。"""
    import traceback

    path = APP / "crash.log"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            f.write(traceback.format_exc())
            f.write(f"\nfrozen={getattr(sys, 'frozen', False)} argv={sys.argv}\n")
    except Exception:
        pass


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if arg == "--bridge":
            run_bridge()
        elif arg == "--watch":
            return run_watch()
        else:
            # 桌面控制台是 Qt 写的（gui.py）。旧的 tkinter 面板已经删掉，
            # 需要时从 git 历史里找回（提交 4d28818 之前）。
            import gui as gui_qt

            return gui_qt.main()
    except SystemExit:
        raise
    except BaseException:
        _crash_log()
        raise


if __name__ == "__main__":
    sys.exit(main())
