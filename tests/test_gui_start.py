"""GUI 启动按钮的回归测试。

    .venv\\Scripts\\python.exe tests\\test_gui_start.py

背景：改 Qt 界面时把 `_cmd` 里的入口写成了 `__file__`（= gui.py），
源码模式下点「启动」会又开一个 GUI，而不是起桥；另外失败分支里写了
`QMessageBox.showerror`（Qt 里没有这个方法），异常被吞掉，按钮看起来死了。

这里不点鼠标：直接建窗口、把 Popen 换成假的，看它到底凑出什么命令。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import gui  # noqa: E402

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


class FakeProc:
    """假装已经起来的子进程：poll() 返回 None 表示还活着。"""

    def __init__(self, cmd, cwd):
        self.cmd = cmd
        self.cwd = cwd
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


class Recorder:
    def __init__(self):
        self.started = []
        self.messages = []

    def popen(self, cmd, **kw):
        p = FakeProc(cmd, kw.get("cwd"))
        self.started.append(p)
        return p

    def modal(self, *a, **kw):
        self.messages.append(a[1] if len(a) > 1 else str(a))
        return QMessageBox.Ok


def main():
    app = QApplication.instance() or QApplication([])
    rec = Recorder()

    # 拦住所有模态框：测试里弹框会挂住
    for name in ("information", "warning", "critical", "question"):
        setattr(QMessageBox, name, rec.modal)

    real_popen = gui.subprocess.Popen
    real_port = gui.port_open
    gui.subprocess.Popen = rec.popen
    gui.port_open = lambda *a, **kw: False      # 别让环境里真开着桥影响判断
    try:
        w = gui.MainWindow()
        w.procs.clear()
        w.start_all()
    finally:
        gui.subprocess.Popen = real_popen
        gui.port_open = real_port

    check("启动后起了两个子进程", len(rec.started) == 2, len(rec.started))
    if len(rec.started) == 2:
        bridge_cmd, watch_cmd = rec.started[0].cmd, rec.started[1].cmd
        check("先起桥", bridge_cmd[-1] == "--bridge", bridge_cmd)
        check("再起监听", watch_cmd[-1] == "--watch", watch_cmd)
        check("入口不是 gui.py", not str(bridge_cmd[1]).endswith("gui.py"), bridge_cmd)
        for c in (bridge_cmd, watch_cmd):
            check(f"入口存在：{Path(c[1]).name}", Path(c[1]).exists(), c)
        check("工作目录是数据根", str(rec.started[0].cwd) == str(gui.APP),
              rec.started[0].cwd)

    check("全程没有弹框（说明没走失败分支）", rec.messages == [], rec.messages)

    # 状态判断
    w.procs["--watch"] = FakeProc([], None)
    check("进程活着算运行中", w.watch_state()[0] == "运行中", w.watch_state())
    w.procs["--watch"] = FakeProc([], None)
    w.procs["--watch"].terminated = True

    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
