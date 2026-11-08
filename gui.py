"""桌面控制台（PySide6）。

不带参数打开这个窗口；`--bridge` / `--watch` 仍然是后台进程的入口。

页面分左侧导航：概览 / 订阅 / 收藏 / 记忆 / 设置。
微信相关的东西全部封存，从主界面移到一个独立窗口（左下角那个按钮弹出来）。

读数据直接 import 项目模块（同一个数据根，只读为主）；写配置自己读改写，
不 import bridge —— 那个模块一导入就把 CONFIG、钩子、限流器全建起来，
GUI 进程不需要这些。
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from charts import Donut, HBars, MemoryGraph, StackedBars

APP = (Path(sys.executable).resolve().parent
       if getattr(sys, "frozen", False) else Path(__file__).resolve().parent)
os.environ.setdefault("WXBRIDGE_ROOT", str(APP))
PORT = 8765
BRIDGE_LOG = APP / "bridge.log"
WATCH_LOG = APP / "watch_uia_log.txt"

import themes
import charts

# 颜色不再写死在这里：从 themes.py 取，切主题时整体换一套。
_C = themes.get(themes.saved())
ACCENT, BG, PANEL = _C["accent"], _C["bg"], _C["panel"]
LINE, TEXT, MUTED = _C["line"], _C["text"], _C["muted"]
OK, WARN, BAD = _C["ok"], _C["warn"], _C["bad"]
THEME = themes.saved()


def apply_theme(name, app=None):
    """换配色：全局颜色、QSS、图表调色板一起换，再把界面重刷一遍。"""
    global ACCENT, BG, PANEL, LINE, TEXT, MUTED, OK, WARN, BAD, THEME
    if name not in themes.THEMES:
        name = themes.DEFAULT
    c = themes.get(name)
    THEME = name
    ACCENT, BG, PANEL = c["accent"], c["bg"], c["panel"]
    LINE, TEXT, MUTED = c["line"], c["text"], c["muted"]
    OK, WARN, BAD = c["ok"], c["warn"], c["bad"]
    charts.set_palette(c)
    if app is not None:
        app.setStyleSheet(themes.qss(c))
    themes.save(name)
    return c


# ------------------------------------------------------------------ 小工具
def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return default if default is not None else {}


def read_json_strict(path):
    """读配置，并把「文件不存在」和「读不动」分开。返回 (data, 状态)。

    状态是 'ok' / 'missing' / 'broken'。
    写配置之前必须走这个：拿 read_json 的 {} 当基线写回去，会把表单里没有的键
    （api_key、api_token、rooms、rate_limit、search…）全清掉，界面还显示「已保存」。
    """
    p = Path(path)
    if not p.exists():
        return {}, "missing"
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"[gui] {p.name} 读不动：{type(exc).__name__}: {exc}", flush=True)
        return {}, "broken"
    if not isinstance(data, dict):
        print(f"[gui] {p.name} 不是对象（{type(data).__name__}），当坏文件处理", flush=True)
        return {}, "broken"
    return data, "ok"


def write_json(path, data):
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    os.replace(tmp, p)


def port_open(port=PORT, timeout=0.25):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def lock_pid(path):
    """读 .watch.lock 里的 PID。返回 (pid 或 None, 这个 PID 还活着吗)。

    锁里只有一个 PID，没有时间戳/镜像名。删锁之前必须先判活：
    否则会把**别人正在用**的锁删掉（外部启动的监听还活着，GUI 一删锁，
    下次「启动」就能再起一份，两个监听同时轮询 → 同一条消息回两遍）。
    """
    try:
        pid = int(Path(path).read_text(encoding="utf-8").strip())
    except Exception:
        return None, False
    if pid <= 0:
        return None, False
    if os.name != "nt":
        return pid, False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not handle:
            return pid, False
        kernel32.CloseHandle(handle)
        return pid, True
    except Exception:
        return pid, False


def clear_dead_lock(path):
    """锁里的 PID 已经死了才删锁。返回是否删了。"""
    pid, alive = lock_pid(path)
    if pid is None or alive:
        return False
    Path(path).unlink(missing_ok=True)
    return True


def card(label, value="—", button=None, slot=None):
    """一张数字卡。给了 button 就在右上角塞一个小按钮（管这张卡自己的刷新）。"""
    box = QFrame()
    box.setObjectName("Card")
    grid = QGridLayout(box)
    grid.setContentsMargins(16, 12, 12, 14)
    grid.setHorizontalSpacing(8)
    grid.setVerticalSpacing(2)
    num = QLabel(value)
    num.setObjectName("CardNum")
    cap = QLabel(label)
    cap.setObjectName("CardLabel")
    grid.addWidget(num, 0, 0)
    btn = None
    if button:
        btn = QPushButton(button)
        btn.setObjectName("Mini")
        btn.setCursor(Qt.PointingHandCursor)
        if slot:
            btn.clicked.connect(slot)
        grid.addWidget(btn, 0, 1, Qt.AlignTop | Qt.AlignRight)
    grid.addWidget(cap, 1, 0, 1, 2)
    grid.setColumnStretch(0, 1)
    if btn:
        return box, btn, num
    return box, num


def title_row(title, sub=""):
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 8)
    lay.setSpacing(2)
    t = QLabel(title)
    t.setObjectName("Title")
    lay.addWidget(t)
    if sub:
        s = QLabel(sub)
        s.setObjectName("Sub")
        lay.addWidget(s)
    return box


def tail(path, lines=200):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except Exception:
        return "（还没有日志）"


# ------------------------------------------------------------------ 后台线程
class Runner(QThread):
    """把慢动作（跑订阅、抓数据）丢到线程里，别卡界面。"""
    line = Signal(str)
    done = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            out = self.fn(self.line.emit)
            self.done.emit(out or "完成")
        except Exception as exc:
            self.done.emit(f"出错：{type(exc).__name__}: {exc}")


class LogTail(QPlainTextEdit):
    def __init__(self, path, interval_ms=2000):
        super().__init__()
        self.path = path
        self.setReadOnly(True)
        self.setMaximumBlockCount(2000)
        self.setObjectName("Log")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(interval_ms)
        self.refresh()

    def refresh(self):
        text = tail(self.path)
        if text != self.toPlainText():
            self.setPlainText(text)
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


class BalanceWorker(QThread):
    """查余额要走网络，放线程里，别卡住界面。"""

    done = Signal(dict)

    def run(self):
        try:
            import backends
            self.done.emit(backends.balance())
        except Exception as exc:
            self.done.emit({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                            "at": int(time.time())})


# ------------------------------------------------------------------ 概览
class OverviewPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(16)
        lay.addWidget(title_row("概览", "「启动」先把桥拉起来，再起微信监听；微信那边封存，只是不再单独占一页。"))

        # 第一行：两个「状态」卡，启动/停止紧挨在它们右边 —— 按钮管的就是这两张卡，
        # 别和「数字」混在一排里。
        row = QHBoxLayout()
        row.setSpacing(12)
        self.cards = {}
        for key, label in (("bridge", "桥"), ("watch", "微信监听")):
            box, num = card(label)
            self.cards[key] = num
            row.addWidget(box, 0)

        self.btn_start = QPushButton("启动")
        self.btn_start.setObjectName("Primary")
        self.btn_start.setToolTip("先起桥，再起微信监听")
        self.btn_start.clicked.connect(self.win.start_all)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setToolTip("两个都停")
        self.btn_stop.clicked.connect(self.win.stop_all)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addWidget(self.btn_start)
        btns.addWidget(self.btn_stop)
        wrap = QVBoxLayout()
        wrap.addStretch(1)
        wrap.addLayout(btns)
        wrap.addStretch(1)
        row.addLayout(wrap)
        row.addStretch(1)
        lay.addLayout(row)

        self.hint = QLabel("")
        self.hint.setObjectName("Sub")
        lay.addWidget(self.hint)

        # 第二行：纯数字。余额是唯一需要自己去问一次的，所以按钮长在卡片里。
        row2 = QHBoxLayout()
        row2.setSpacing(12)
        for key, label in (("fav", "今天收藏"), ("mem", "活跃记忆"), ("sub", "订阅词")):
            box, num = card(label)
            self.cards[key] = num
            row2.addWidget(box, 1)
        box, self.btn_balance, num = card(label="余额", value="—", button="刷新",
                                          slot=self.win.query_balance)
        self.cards["balance"] = num
        self.btn_balance.setToolTip("问 DeepSeek 要一次账户余额")
        row2.addWidget(box, 1)
        lay.addLayout(row2)

        cap = QLabel("bridge.log")
        cap.setObjectName("Sub")
        lay.addWidget(cap)
        self.log = LogTail(BRIDGE_LOG)
        lay.addWidget(self.log, 1)

    def refresh(self):
        running = port_open()
        self.cards["bridge"].setText("运行中" if running else "已停")
        self.cards["bridge"].setStyleSheet(f"color:{OK if running else MUTED}")

        state, note = self.win.watch_state()
        self.cards["watch"].setText(state)
        self.cards["watch"].setStyleSheet(
            f"color:{OK if state == '运行中' else (WARN if state == '锁残留' else MUTED)}")
        self.hint.setText(note)
        self.btn_start.setEnabled(not (running and state == "运行中"))
        self.btn_stop.setEnabled(running or state != "已停")
        self.show_balance()

    def show_balance(self):
        """余额只从缓存里显示；真要刷新是「查余额」按钮的事。"""
        b = self.win.balance_cache
        if b.get("ok"):
            self.cards["balance"].setText(f"¥{b['total']}")
            self.cards["balance"].setStyleSheet(f"color:{ACCENT}")
            when = time.strftime("%H:%M", time.localtime(b.get("at") or 0))
            self.cards["balance"].setToolTip(f"{when} 查的 · 充值 "
                                             f"{b.get('topped_up') or '?'} · 赠送 "
                                             f"{b.get('granted') or '?'}")
        elif b.get("error"):
            self.cards["balance"].setText("查不到")
            self.cards["balance"].setStyleSheet(f"color:{WARN}")
            self.cards["balance"].setToolTip(b["error"])
        else:
            self.cards["balance"].setText("—")
            self.cards["balance"].setStyleSheet(f"color:{MUTED}")
            self.cards["balance"].setToolTip("点「查余额」")
        try:
            import bili_fav
            self.cards["fav"].setText(f"{bili_fav.today_count()}/{bili_fav.DAILY_CAP}")
        except Exception:
            self.cards["fav"].setText("—")
        try:
            import memory
            s = memory.stats()
            self.cards["mem"].setText(str(s.get("active", 0)))
        except Exception:
            self.cards["mem"].setText("—")
        try:
            import bili_sub
            cfg = bili_sub.load_conf()
            n = len(cfg.get("keywords") or [])
            self.cards["sub"].setText(str(n))
        except Exception:
            self.cards["sub"].setText("—")


# ------------------------------------------------------------------ 订阅
class SubsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(14)
        lay.addWidget(title_row("关键词订阅", "每几小时按词搜一次，挑一条收进「AI收藏」。"))

        top = QHBoxLayout()
        self.enabled = QCheckBox("开启订阅")
        self.enabled.clicked.connect(self.save_enabled)
        self.btn_run = QPushButton("立即跑一轮")
        self.btn_run.setObjectName("Primary")
        self.btn_run.clicked.connect(self.run_now)
        top.addWidget(self.enabled)
        top.addStretch(1)
        top.addWidget(self.btn_run)
        lay.addLayout(top)

        kwbox = QFrame()
        kwbox.setObjectName("Card")
        kbl = QVBoxLayout(kwbox)
        kbl.setContentsMargins(16, 14, 16, 14)
        kbl.setSpacing(8)
        kbl.addWidget(QLabel("订阅词"))
        self.kw_list = QListWidget()
        self.kw_list.setObjectName("KwList")
        self.kw_list.setMaximumHeight(150)
        kbl.addWidget(self.kw_list)
        addrow = QHBoxLayout()
        self.kw_input = QLineEdit()
        self.kw_input.setPlaceholderText("加一个词，回车确认")
        self.kw_input.returnPressed.connect(self.add_kw)
        b_add = QPushButton("添加")
        b_add.clicked.connect(self.add_kw)
        b_del = QPushButton("删掉选中的")
        b_del.setObjectName("Danger")
        b_del.clicked.connect(self.del_kw)
        addrow.addWidget(self.kw_input, 1)
        addrow.addWidget(b_add)
        addrow.addWidget(b_del)
        kbl.addLayout(addrow)
        lay.addWidget(kwbox)

        self.hint = QLabel("")
        self.hint.setObjectName("Sub")
        lay.addWidget(self.hint)

        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setObjectName("Log")
        lay.addWidget(self.out, 1)
        self.reload()

    def reload(self):
        try:
            import bili_sub
            cfg = bili_sub.load_conf()
            self.enabled.setChecked(bool(cfg.get("enabled")))
            self.kw_list.clear()
            for kw in cfg.get("keywords") or []:
                self.kw_list.addItem(QListWidgetItem(kw))
            win_s = " - ".join(cfg.get("window") or [])
            self.hint.setText(
                f"时段 {win_s} · 每 {cfg.get('interval_hours')} 小时 · "
                f"每轮最多 {cfg.get('per_run')} 条 · 播放 ≥ {cfg.get('min_play'):,} · "
                f"{cfg.get('max_age_days')} 天内")
        except Exception as exc:
            self.hint.setText(f"读不到订阅配置：{exc}")

    def _save(self, cfg):
        import bili_sub
        write_json(Path(bili_sub._HERE) / "data" / "bili_sub.json", cfg)

    def save_enabled(self):
        try:
            import bili_sub
            cfg = bili_sub.load_conf()
            cfg["enabled"] = self.enabled.isChecked()
            self._save(cfg)
            self.out.appendPlainText(
                f"[{time.strftime('%H:%M:%S')}] 订阅已{'开启' if cfg['enabled'] else '关闭'}")
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    def add_kw(self):
        kw = self.kw_input.text().strip()
        if not kw:
            return
        try:
            import bili_sub
            cfg = bili_sub.load_conf()
            kws = cfg.get("keywords") or []
            if kw in kws:
                QMessageBox.information(self, "已经有了", f"「{kw}」已经在列表里。")
                return
            kws.append(kw)
            cfg["keywords"] = kws
            self._save(cfg)
            self.kw_input.clear()
            self.reload()
        except Exception as exc:
            QMessageBox.warning(self, "添加失败", str(exc))

    def del_kw(self):
        item = self.kw_list.currentItem()
        if not item:
            QMessageBox.information(self, "没选中", "先在列表里点一个词。")
            return
        try:
            import bili_sub
            cfg = bili_sub.load_conf()
            cfg["keywords"] = [k for k in (cfg.get("keywords") or []) if k != item.text()]
            self._save(cfg)
            self.reload()
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", str(exc))

    def run_now(self):
        self.btn_run.setEnabled(False)
        self.out.appendPlainText(f"[{time.strftime('%H:%M:%S')}] 开始跑一轮…")

        def job(emit):
            import bili_sub
            old_print = print

            def hook(*args, **kwargs):
                msg = " ".join(str(a) for a in args)
                emit(msg)
                old_print(*args, **kwargs)
            builtins_print = __builtins__["print"] if isinstance(__builtins__, dict) else print
            try:
                import builtins
                builtins.print = hook
                bili_sub.run(force=True)
            finally:
                import builtins
                builtins.print = builtins_print
            return "这一轮结束"

        self.worker = Runner(job)
        self.worker.line.connect(lambda s: self.out.appendPlainText(s))
        self.worker.done.connect(self._done)
        self.worker.start()

    def _done(self, msg):
        self.out.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {msg}")
        self.btn_run.setEnabled(True)
        self.win.overview.refresh()


# ------------------------------------------------------------------ 收藏
class FavsPage(QWidget):
    COLS = ("时间", "BVID", "标题", "播放", "理由")

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(14)
        lay.addWidget(title_row("收藏", "都在 B站「AI收藏」夹子里，这里只是本地日志。"))

        top = QHBoxLayout()
        b_refresh = QPushButton("刷新")
        b_refresh.clicked.connect(self.reload)
        b_open = QPushButton("打开 B站收藏夹")
        b_open.clicked.connect(self.open_folder)
        self.cap = QLabel("")
        self.cap.setObjectName("Sub")
        top.addWidget(b_refresh)
        top.addWidget(b_open)
        top.addStretch(1)
        top.addWidget(self.cap)
        lay.addLayout(top)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 120)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(3, 90)
        self.table.setColumnWidth(4, 200)
        lay.addWidget(self.table, 1)
        self.reload()

    def reload(self):
        try:
            import bili_fav
            log = Path(bili_fav.LOG)
            rows = []
            if log.exists():
                for line in log.read_text(encoding="utf-8").splitlines():
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("ok"):
                        rows.append(r)
            rows.reverse()
            self.table.setRowCount(len(rows))
            for i, r in enumerate(rows):
                when = time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0)))
                play = r.get("play")
                vals = (when, r.get("bvid", ""), r.get("title", "（无标题）"),
                        f"{int(play):,}" if isinstance(play, int) else "—",
                        r.get("reason") or "—")
                for c, v in enumerate(vals):
                    item = QTableWidgetItem(str(v))
                    if c == 4:
                        item.setForeground(QColor(MUTED))
                    self.table.setItem(i, c, item)
            self.cap.setText(f"共 {len(rows)} 条 · 今天 {bili_fav.today_count()}/"
                             f"{bili_fav.DAILY_CAP}")
        except Exception as exc:
            self.cap.setText(f"读不到：{exc}")

    def open_folder(self):
        import webbrowser
        user = read_json(Path(os.environ.get("WXBRIDGE_ROOT", APP)) /
                         "data" / "bili_session.json", {})
        uid = user.get("DedeUserID") or ""
        webbrowser.open(f"https://space.bilibili.com/{uid}/favlist")


# ------------------------------------------------------------------ 语料
class BatchWorker(QThread):
    """跑一批视频的评论抓取。界面只收事件，不碰网络。"""

    event = Signal(str, dict)

    def __init__(self, bvids, opts):
        super().__init__()
        self.bvids = list(bvids)
        self.opts = dict(opts)
        self.stop = threading.Event()

    def run(self):
        try:
            import bili_reply
            bili_reply.run_batch(
                self.bvids,
                pages=self.opts["pages"], sort=self.opts["sort"],
                subs=self.opts["subs"], save_it=self.opts["save"],
                max_videos=self.opts["max_videos"],
                max_comments=self.opts["max_comments"],
                max_seconds=self.opts["max_seconds"],
                on_event=lambda kind, **kw: self.event.emit(kind, kw),
                stop=self.stop)
        except Exception as exc:
            self.event.emit("fatal", {"error": f"{type(exc).__name__}: {exc}"})


class CorpusPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.worker = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(12)
        lay.addWidget(title_row("评论语料", "把评论区当语料库。只读，不点赞、不回复、不发评论。"))

        # 目标
        box = QFrame()
        box.setObjectName("Card")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(16, 14, 16, 14)
        bl.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(QLabel("抓哪些视频"))
        b_fav = QPushButton("用收藏夹里还没抓的")
        b_fav.clicked.connect(self.fill_from_favs)
        b_clear = QPushButton("清空")
        b_clear.clicked.connect(lambda: self.targets.setPlainText(""))
        head.addStretch(1)
        head.addWidget(b_fav)
        head.addWidget(b_clear)
        bl.addLayout(head)
        self.targets = QPlainTextEdit()
        self.targets.setPlaceholderText("一行一个 BV 号，也可以用上面那个按钮批量填")
        self.targets.setMaximumHeight(96)
        self.targets.setObjectName("LogMono")
        bl.addWidget(self.targets)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("每个视频抓"))
        self.pages = QSpinBox()
        self.pages.setRange(1, 5)
        self.pages.setValue(2)
        self.pages.setSuffix(" 页")
        row.addWidget(self.pages)
        row.addWidget(QLabel("排序"))
        self.sort = QComboBox()
        self.sort.addItems(["按热度", "按时间", "按点赞"])
        row.addWidget(self.sort)
        self.subs = QCheckBox("连楼中楼")
        row.addWidget(self.subs)
        self.save_it = QCheckBox("入库")
        self.save_it.setChecked(True)
        row.addWidget(self.save_it)
        row.addStretch(1)
        bl.addLayout(row)

        stoprow = QHBoxLayout()
        stoprow.setSpacing(10)
        stoprow.addWidget(QLabel("停到什么时候（0 = 不限）"))
        self.max_videos = QSpinBox()
        self.max_videos.setRange(0, 9999)
        self.max_videos.setSpecialValueText("不限")
        self.max_videos.setSuffix(" 个视频")
        self.max_comments = QSpinBox()
        self.max_comments.setRange(0, 99999)
        self.max_comments.setValue(200)
        self.max_comments.setSingleStep(50)
        self.max_comments.setSuffix(" 条评论")
        self.max_seconds = QSpinBox()
        self.max_seconds.setRange(0, 36000)
        self.max_seconds.setValue(300)
        self.max_seconds.setSingleStep(60)
        self.max_seconds.setSuffix(" 秒")
        for w in (self.max_videos, self.max_comments, self.max_seconds):
            stoprow.addWidget(w)
        stoprow.addStretch(1)
        bl.addLayout(stoprow)

        act = QHBoxLayout()
        act.setSpacing(10)
        self.btn_start = QPushButton("开始抓")
        self.btn_start.setObjectName("Primary")
        self.btn_start.clicked.connect(self.start)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop)
        self.cap = QLabel("")
        self.cap.setObjectName("Sub")
        act.addWidget(self.btn_start)
        act.addWidget(self.btn_stop)
        act.addWidget(self.cap, 1)
        bl.addLayout(act)
        lay.addWidget(box)

        # 日志 + 语料
        split = QHBoxLayout()
        split.setSpacing(12)
        left = QVBoxLayout()
        left.addWidget(QLabel("这次跑的"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("Log")
        left.addWidget(self.log, 1)
        split.addLayout(left, 3)

        right = QVBoxLayout()
        head2 = QHBoxLayout()
        head2.addWidget(QLabel("语料库里最近的"))
        b_ref = QPushButton("刷新")
        b_ref.clicked.connect(self.reload)
        head2.addStretch(1)
        head2.addWidget(b_ref)
        right.addLayout(head2)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["赞", "谁", "内容"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(1, 110)
        right.addWidget(self.table, 1)
        split.addLayout(right, 4)
        lay.addLayout(split, 1)
        self.reload()

    # ---- 数据
    def parse_targets(self):
        raw = self.targets.toPlainText().replace(",", " ").replace("，", " ").split()
        out = []
        for x in raw:
            b = x.strip()
            if b.lower().startswith("bv") and b not in out:
                out.append(b)
        return out

    def fill_from_favs(self):
        try:
            import bili_reply
            bvids = bili_reply.targets_from_favs()
        except Exception as exc:
            QMessageBox.warning(self, "读收藏失败", str(exc))
            return
        if not bvids:
            self.cap.setText("收藏夹里没有新的了（都抓过了）")
            return
        self.targets.setPlainText("\n".join(bvids))
        self.cap.setText(f"填了 {len(bvids)} 个")

    def reload(self):
        try:
            import bili_reply
            rows = bili_reply.corpus_rows(200)
            self.table.setRowCount(len(rows))
            for i, r in enumerate(rows):
                for c, v in enumerate((str(r.get("like", 0)), r.get("user") or "",
                                       (r.get("text") or "").replace("\n", " "))):
                    item = QTableWidgetItem(v)
                    if c == 2:
                        item.setToolTip(f"{r.get('bvid')} · rpid {r.get('rpid')}")
                    self.table.setItem(i, c, item)
            total = len(bili_reply.corpus_rows(100000))
            self.cap.setText(f"库里有 {total} 条")
        except Exception as exc:
            self.cap.setText(f"读不到：{exc}")

    # ---- 抓取
    def start(self):
        bvids = self.parse_targets()
        if not bvids:
            QMessageBox.information(self, "没有目标", "先填 BV 号，或者点「用收藏夹里还没抓的」。")
            return
        opts = {
            "pages": self.pages.value(),
            "sort": [2, 0, 1][self.sort.currentIndex()],
            "subs": self.subs.isChecked(),
            "save": self.save_it.isChecked(),
            "max_videos": self.max_videos.value(),
            "max_comments": self.max_comments.value(),
            "max_seconds": self.max_seconds.value(),
        }
        self.log.clear()
        self.log.appendPlainText(f"目标 {len(bvids)} 个视频 · 每个最多 {opts['pages']} 页 · "
                                 f"停：{self.rule_text(opts)}")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.worker = BatchWorker(bvids, opts)
        self.worker.event.connect(self.on_event)
        self.worker.start()

    def rule_text(self, o):
        bits = []
        if o["max_videos"]:
            bits.append(f"{o['max_videos']} 个视频")
        if o["max_comments"]:
            bits.append(f"{o['max_comments']} 条评论")
        if o["max_seconds"]:
            bits.append(f"{o['max_seconds']} 秒")
        return " / ".join(bits) or "不限"

    def stop(self):
        if self.worker:
            self.worker.stop.set()
            self.cap.setText("正在停…（当前这个视频抓完就停）")

    def on_event(self, kind, kw):
        ts = time.strftime("%H:%M:%S")
        if kind == "start":
            self.log.appendPlainText(f"[{ts}] 开始 {kw.get('bvid')}")
        elif kind == "video":
            self.log.appendPlainText(
                f"[{ts}] {kw.get('bvid')} {kw.get('title', '')[:24]}  "
                f"抓到 {kw.get('got')} 条（总共 {kw.get('total')}）走={kw.get('path')}")
        elif kind == "save":
            self.log.appendPlainText(f"[{ts}]   入库 {kw.get('added')} 条")
        elif kind == "error":
            self.log.appendPlainText(f"[{ts}] 失败 {kw.get('bvid')}：{kw.get('error')}")
        elif kind == "done":
            self.log.appendPlainText(
                f"[{ts}] 结束：{kw.get('videos')} 个视频 / {kw.get('comments')} 条评论 / "
                f"入库 {kw.get('added')} 条（{kw.get('stopped_by') or '跑完了'}）")
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self.reload()
        elif kind == "fatal":
            self.log.appendPlainText(f"[{ts}] 出错：{kw.get('error')}")
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)


# ------------------------------------------------------------------ 统计
class StatsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(title_row("统计", "桥处理了多少、为什么没回、缓存省了多少。"))
        head.addStretch(1)
        self.range = QComboBox()
        self.range.addItems(["近 24 小时", "近 7 天", "近 30 天"])
        self.range.currentIndexChanged.connect(self.reload)
        b = QPushButton("刷新")
        b.clicked.connect(self.reload)
        head.addWidget(self.range, 0, Qt.AlignTop)
        head.addWidget(b, 0, Qt.AlignTop)
        lay.addLayout(head)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.cards = {}
        for key, label in (("total", "处理"), ("sent", "已回复"), ("dropped", "丢弃"),
                           ("fail", "模型失败率"), ("cache", "缓存命中")):
            box, num = card(label)
            self.cards[key] = num
            row.addWidget(box, 1)
        lay.addLayout(row)

        mid = QHBoxLayout()
        mid.setSpacing(12)

        bars_box = QFrame()
        bars_box.setObjectName("Card")
        bl = QVBoxLayout(bars_box)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.setSpacing(4)
        bl.addWidget(QLabel("处理量"))
        self.bars = StackedBars()
        bl.addWidget(self.bars, 1)
        mid.addWidget(bars_box, 3)

        donut_box = QFrame()
        donut_box.setObjectName("Card")
        dl = QVBoxLayout(donut_box)
        dl.setContentsMargins(14, 10, 14, 10)
        dl.setSpacing(4)
        dl.addWidget(QLabel("缓存命中率"))
        self.donut = Donut()
        dl.addWidget(self.donut, 1)
        self.token_note = QLabel("")
        self.token_note.setObjectName("Sub")
        self.token_note.setAlignment(Qt.AlignCenter)
        dl.addWidget(self.token_note)
        mid.addWidget(donut_box, 2)
        lay.addLayout(mid, 3)

        low = QHBoxLayout()
        low.setSpacing(12)

        why_box = QFrame()
        why_box.setObjectName("Card")
        wl = QVBoxLayout(why_box)
        wl.setContentsMargins(14, 10, 14, 10)
        wl.setSpacing(4)
        self.why_title = QLabel("为什么没回成")
        wl.addWidget(self.why_title)
        self.hbars = HBars()
        wl.addWidget(self.hbars, 1)
        low.addWidget(why_box, 1)

        tb_box = QFrame()
        tb_box.setObjectName("Card")
        tl = QVBoxLayout(tb_box)
        tl.setContentsMargins(14, 10, 14, 10)
        tl.setSpacing(4)
        tl.addWidget(QLabel("明细"))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["动作", "原因", "次数", "平均耗时"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 70)
        self.table.setColumnWidth(2, 70)
        self.table.setColumnWidth(3, 90)
        tl.addWidget(self.table, 1)
        low.addWidget(tb_box, 2)
        lay.addLayout(low, 2)
        self.reload()

    def hours(self):
        return [24, 24 * 7, 24 * 30][self.range.currentIndex()]

    def reload(self):
        try:
            import stats as st
            h = self.hours()
            s = st.summary(h)
            ser = st.series(h)
            why = st.reasons(h)
            tok = st.tokens(h)
        except Exception as exc:
            self.cards["total"].setText("—")
            self.why_title.setText(f"读不到统计：{exc}")
            return

        self.cards["total"].setText(str(s["total"]))
        self.cards["sent"].setText(str(s["sent"]))
        self.cards["dropped"].setText(str(s["dropped"]))
        self.cards["fail"].setText(f"{s['model_fail_rate']}%")
        self.cards["cache"].setText(f"{tok['cache_hit_rate']}%")
        for key, color in (("total", TEXT), ("sent", OK), ("dropped", WARN),
                           ("fail", BAD if s["model_fail"] else MUTED), ("cache", ACCENT)):
            self.cards[key].setStyleSheet(f"color:{color}")

        self.bars.set_data(ser["labels"], [
            ("回复", OK, ser["send"]),
            ("丢弃", WARN, ser["drop"]),
            ("失败", BAD, ser["error"]),
        ])

        self.donut.set_data(
            [("命中", ACCENT, tok["cache_hit"]),
             ("未命中", themes.get(THEME)["scroll"], tok["cache_miss"])],
            center_top=f"{tok['cache_hit_rate']}%", center_bottom="缓存命中")
        self.token_note.setText(f"{tok['calls']} 次调用 · 输出 {tok['output']:,} token")

        self.hbars.set_data([(r["reason"], r["count"],
                              BAD if r["action"] == "error" else WARN) for r in why])
        self.why_title.setText(f"为什么没回成（共 {sum(r['count'] for r in why)} 次）")

        items = s["items"]
        self.table.setRowCount(len(items))
        for i, it in enumerate(items):
            vals = (it["action"], st.reason_text(it["action"], it["reason"]),
                    str(it["count"]), f"{it['avg_ms']} ms")
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c == 0:
                    item.setForeground(QColor(
                        OK if it["action"] == "send" else
                        (WARN if it["action"] == "drop" else BAD)))
                self.table.setItem(i, c, item)


# ------------------------------------------------------------------ 记忆
class MemoryPage(QWidget):
    COLS = ("#", "类型", "重要", "次", "来源", "谁", "内容")

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(12)
        lay.addWidget(title_row("长期记忆", "聊满几轮后台抽一次；只留跟人有关的、以后还用得上的。"))

        bar = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("按正文搜")
        self.q.returnPressed.connect(self.reload)
        self.status = QComboBox()
        self.status.addItems(["active", "expired", "forgotten", "all"])
        self.status.currentIndexChanged.connect(self.reload)
        b = QPushButton("刷新")
        b.clicked.connect(self.reload)
        self.cap = QLabel("")
        self.cap.setObjectName("Sub")
        bar.addWidget(self.q, 1)
        bar.addWidget(self.status)
        bar.addWidget(b)
        bar.addWidget(self.cap)
        lay.addLayout(bar)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(6, QHeaderView.Stretch)
        for c, w in ((0, 50), (1, 90), (2, 60), (3, 50), (4, 60), (5, 90)):
            self.table.setColumnWidth(c, w)
        # 图不在这里了，搬到了独立的「星云」页：这张表是拿来管理和删除的，
        # 要的是行高和列宽，图要的是面积，挤在一页两边都不够用。
        lay.addWidget(self.table, 3)

        probe = QFrame()
        probe.setObjectName("Card")
        pl = QVBoxLayout(probe)
        pl.setContentsMargins(16, 12, 16, 12)
        pl.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(QLabel("召回测试"))
        self.probe_who = QLineEdit()
        self.probe_who.setPlaceholderText("谁（wxid）")
        self.probe_who.setMaximumWidth(160)
        self.probe_text = QLineEdit()
        self.probe_text.setPlaceholderText("打一句话，看会召回到哪几条")
        self.probe_text.returnPressed.connect(self.probe)
        b_probe = QPushButton("算一下")
        b_probe.setObjectName("Primary")
        b_probe.clicked.connect(self.probe)
        head.addWidget(self.probe_who)
        head.addWidget(self.probe_text, 1)
        head.addWidget(b_probe)
        pl.addLayout(head)
        self.probe_out = QPlainTextEdit()
        self.probe_out.setReadOnly(True)
        self.probe_out.setMaximumHeight(150)
        self.probe_out.setObjectName("LogMini")
        pl.addWidget(self.probe_out)
        lay.addWidget(probe, 1)

        row = QHBoxLayout()
        b_del = QPushButton("忘掉选中的")
        b_del.setObjectName("Danger")
        b_del.clicked.connect(self.forget)
        row.addWidget(b_del)
        row.addStretch(1)
        lay.addLayout(row)
        self.reload()

    def reload(self):
        try:
            import memory
            # list_atoms 的头一个参数是 wxid，搜索词是第二个。原来把搜索词
            # 当 wxid 传了，「按正文搜」实际在按 wxid 精确匹配，永远搜不到。
            rows = memory.list_atoms(None, self.q.text().strip(),
                                     self.status.currentText())
            self.table.setRowCount(len(rows))
            for i, r in enumerate(rows):
                vals = (r["id"], r["kind"], f"{r['importance']:.2f}", r["uses"],
                        r.get("sources", 0), r["wxid"], r["text"])
                for c, v in enumerate(vals):
                    item = QTableWidgetItem(str(v))
                    if c == 4 and not r.get("sources"):
                        item.setForeground(QColor("#4a5262"))
                    self.table.setItem(i, c, item)
            self.cap.setText(f"{len(rows)} 条")
        except Exception as exc:
            self.cap.setText(f"读不到：{exc}")

    def forget(self):
        items = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not items:
            QMessageBox.information(self, "没选中", "先点一行。")
            return
        ids = [int(self.table.item(i.row(), 0).text()) for i in items]
        if QMessageBox.question(self, "确认", f"忘掉 {len(ids)} 条？以后不会再被召回。") \
                != QMessageBox.Yes:
            return
        try:
            import memory
            memory.forget_many(ids)
            self.reload()
        except Exception as exc:
            QMessageBox.warning(self, "失败", str(exc))

    def probe(self):
        who = self.probe_who.text().strip()
        text = self.probe_text.text().strip()
        if not who or not text:
            self.probe_out.setPlainText("谁 和 一句话 都要填。")
            return
        try:
            import memory
            cfg = read_json(APP / "config.json")
            r = memory.explain(who, text, cfg)
            why = {"shared<2": "卡在重合不足（少于 2 个字）",
                   "rel<0.15": "卡在覆盖率不够（不到问句的 15%）",
                   "expired": "已过期", "empty": "没有可比的字"}
            lines = [f"开关={'开' if r['enabled'] else '关'}  门槛={r['min_score']}  "
                     f"最多注入 {r['top_k']} 条", ""]
            for h in r["hits"][:10]:
                p = h.get("parts") or {}
                tail_ = ("  → " + why.get(h["gate"], h["gate"])) if h["gate"] else ""
                lines.append(f"{'✓' if h['hit'] else '·'} {h['score']:.3f}  "
                             f"#{h['id']} [{h['kind']}]  重合 {p.get('shared', 0)} 字 "
                             f"占比 {p.get('rel', 0)}{tail_}")
                lines.append("    " + h["text"])
            lines.append("")
            lines.append("会注入：" + (" ".join("#" + str(i) for i in r["would_inject"])
                                     or "（没有）"))
            self.probe_out.setPlainText("\n".join(lines))
        except Exception as exc:
            self.probe_out.setPlainText(f"失败：{type(exc).__name__}: {exc}")


# ------------------------------------------------------------------ 星云
class MemoryMapPage(QWidget):
    """一整页只放星云图。

    和「记忆」页分工：那边是表格，用来查正文、看来源、批量删除；这边只负责
    「看出结构」。挤在一页里两边都不够用 —— 表要行高和列宽，图要面积。
    """

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(12)
        lay.addWidget(title_row(
            "记忆星云",
            "一条记忆一个光点，越大越重要、越往中间收；说得像的之间连线。"
            "3D 下拖空白旋转、滚轮缩放。要看正文和删除，去「记忆」那页。"))

        bar = QHBoxLayout()
        self.mode_btn = QPushButton("2D")
        self.mode_btn.setObjectName("Mini")
        self.mode_btn.setMinimumWidth(56)
        self.mode_btn.setToolTip("在平面星云和可旋转的 3D 球之间切换")
        self.mode_btn.clicked.connect(self.toggle_mode)
        b = QPushButton("刷新")
        b.clicked.connect(lambda: self.reload(force=True))
        self.cap = QLabel("")
        self.cap.setObjectName("Sub")
        bar.addWidget(self.mode_btn)
        bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(self.cap)
        lay.addLayout(bar)

        self.graph = MemoryGraph(400)
        lay.addWidget(self.graph, 1)
        # 不在这里 reload：MainWindow 会把八个页面全建出来，在这儿算布局等于
        # 每次开窗口都先跑一遍 O(n²) 的连线和力导向，哪怕根本不去看这一页。
        # 第一次切过来时 _switch() 会调 reload()。
        self._loaded = False

    def reload(self, force=False):
        if self._loaded and not force:
            return
        try:
            import memory
            rows = memory.list_atoms(None, "", "active", limit=300)
            self.graph.set_data(rows)
            self._loaded = True
            self.cap.setText(f"{len(rows)} 条 · {len(self.graph.edges)} 条连线")
        except Exception as exc:
            self.cap.setText(f"读不到：{exc}")

    def toggle_mode(self):
        """切换由 MemoryGraph 自己重新加热，点会飞过去，不是瞬移。"""
        self.graph.set_mode("3d" if self.graph.mode == "2d" else "2d")
        self.mode_btn.setText(self.graph.mode_name())


# ------------------------------------------------------------------ 设置
class SettingsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 22)
        outer.setSpacing(14)
        outer.addWidget(title_row("设置", "改完点保存；桥在跑的话需要重启才完全生效。"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        form = QGridLayout(inner)
        form.setContentsMargins(0, 0, 8, 0)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(12)

        self.f = {}
        rows = [
            ("my_name", "微信昵称", "text"),
            ("persona", "性格 / 口吻", "text"),
            ("backend", "后端", ["api", "stub", "dsh"]),
            ("api_model", "回复模型", "text"),
            ("api_key", "API Key", "secret"),
            ("api_token", "本机接口 token", "text"),
            ("vault", "Obsidian 库路径", "text"),
            ("vault_daily_hour", "日记几点写", "spin"),
            ("vault_summary_model", "日记摘要模型", "text"),
        ]
        r = 0
        for key, label, kind in rows:
            form.addWidget(QLabel(label), r, 0)
            if isinstance(kind, list):
                w = QComboBox()
                w.addItems(kind)
            elif kind == "spin":
                w = QSpinBox()
                w.setRange(0, 23)
            else:
                w = QLineEdit()
                if kind == "secret":
                    w.setEchoMode(QLineEdit.Password)
            self.f[key] = w
            form.addWidget(w, r, 1)
            r += 1

        form.addWidget(QLabel("记忆"), r, 0)
        self.mem_on = QCheckBox("开启长期记忆")
        form.addWidget(self.mem_on, r, 1)
        r += 1
        form.addWidget(QLabel("日记摘要"), r, 0)
        self.sum_on = QCheckBox("日记里让模型写「这天」和「思考」")
        form.addWidget(self.sum_on, r, 1)
        r += 1
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        bar = QHBoxLayout()
        b_save = QPushButton("保存")
        b_save.setObjectName("Primary")
        b_save.clicked.connect(self.save)
        b_reload = QPushButton("重新读入")
        b_reload.clicked.connect(self.reload)
        self.msg = QLabel("")
        self.msg.setObjectName("Sub")
        bar.addWidget(b_save)
        bar.addWidget(b_reload)
        bar.addWidget(self.msg, 1)
        outer.addLayout(bar)
        self.reload()

    def reload(self):
        cfg, how = read_json_strict(APP / "config.json")
        for key, w in self.f.items():
            v = cfg.get(key, "")
            if isinstance(w, QComboBox):
                w.setCurrentText(str(v))
            elif isinstance(w, QSpinBox):
                try:
                    w.setValue(int(v))
                except (TypeError, ValueError):
                    w.setValue(23)
            else:
                w.setText("" if v is None else str(v))
        self.mem_on.setChecked(bool((cfg.get("memory") or {}).get("enabled")))
        self.sum_on.setChecked(bool(cfg.get("vault_summary", True)))
        self.msg.setText("")

    def save(self):
        path = APP / "config.json"
        cfg, how = read_json_strict(path)
        if how == "broken":
            # 读不动 ≠ 空配置。这时候写回去 = 把 api_key / api_token / rooms 等
            # 表单里没有的键全清掉，界面还显示「已保存」。
            QMessageBox.warning(
                self, "保存失败",
                "config.json 现在读不动（可能被改坏了）。\n"
                "为了不把里面的 api_key / token 等设置清掉，这次不写入。\n"
                "先把文件修好（或改名备份），再点「重新读入」。")
            self.msg.setText("config.json 读不动，没有保存")
            return
        for key, w in self.f.items():
            if isinstance(w, QComboBox):
                cfg[key] = w.currentText()
            elif isinstance(w, QSpinBox):
                cfg[key] = w.value()
            else:
                cfg[key] = w.text()
        cfg.setdefault("memory", {})["enabled"] = self.mem_on.isChecked()
        cfg["vault_summary"] = self.sum_on.isChecked()
        try:
            write_json(APP / "config.json", cfg)
            self.msg.setText(f"已保存 · {time.strftime('%H:%M:%S')}")
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))


# ------------------------------------------------------------------ 微信（封存）
class WeChatWindow(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setWindowTitle("微信（已封存）")
        self.resize(720, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)

        banner = QLabel("微信这块封存了，功能没删：主页的「启动」照样会把它拉起来，"
                        "这里只是单独控制它的地方。")
        banner.setWordWrap(True)
        banner.setObjectName("Banner")
        lay.addWidget(banner)

        info = QLabel("要真用起来：微信必须 4.1.8.x（4.1.15 起辅助功能树被移除），"
                      "并且先把要监听的会话双击成独立窗口。")
        info.setWordWrap(True)
        info.setObjectName("Sub")
        lay.addWidget(info)

        row = QHBoxLayout()
        self.b_start = QPushButton("启动监听")
        self.b_start.clicked.connect(lambda: self.win.start_watch(True))
        self.b_stop = QPushButton("停止监听")
        self.b_stop.setObjectName("Danger")
        self.b_stop.clicked.connect(lambda: self.win.stop_watch(True))
        row.addWidget(self.b_start)
        row.addWidget(self.b_stop)
        row.addStretch(1)
        lay.addLayout(row)

        cap = QLabel("watch_uia_log.txt")
        cap.setObjectName("Sub")
        lay.addWidget(cap)
        self.log = LogTail(WATCH_LOG, interval_ms=1500)
        lay.addWidget(self.log, 1)


# ------------------------------------------------------------------ 主窗口
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("微信桥 · 控制台")
        self.resize(1080, 720)
        self.procs = {}
        self.watch_win = None
        self.balance_cache = {}          # 最近一次查到的余额，show_balance() 只读它

        root = QWidget()
        self.setCentralWidget(root)
        lay = QHBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(210)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 12)
        sl.setSpacing(0)
        brand = QLabel("微信桥")
        brand.setObjectName("Brand")
        sub = QLabel("B站 · 记忆 · 日记")
        sub.setObjectName("BrandSub")
        sl.addWidget(brand)
        sl.addWidget(sub)

        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        for name in ("概览", "订阅", "收藏", "语料", "统计", "记忆", "星云", "设置"):
            self.nav.addItem(QListWidgetItem(name))
        self.nav.currentRowChanged.connect(self._switch)
        sl.addWidget(self.nav, 1)

        self.btn_theme = QPushButton(f"配色 · {THEME}")
        self.btn_theme.setToolTip("点一下换一套颜色：深色 → 米白 → 夜蓝")
        self.btn_theme.clicked.connect(self.cycle_theme)
        sl.addWidget(self.btn_theme)

        wxbtn = QPushButton("微信（已封存）")
        wxbtn.setToolTip("打开独立窗口；主界面不会自动启动它")
        wxbtn.clicked.connect(self.open_wechat)
        sl.addWidget(wxbtn)
        lay.addWidget(side)

        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)

        self.overview = OverviewPage(self)
        self.subs = SubsPage(self)
        self.favs = FavsPage(self)
        self.corpus = CorpusPage(self)
        self.stats = StatsPage(self)
        self.mem = MemoryPage(self)
        self.map = MemoryMapPage(self)
        self.setting = SettingsPage(self)
        for page in (self.overview, self.subs, self.favs, self.corpus,
                     self.stats, self.mem, self.map, self.setting):
            self.stack.addWidget(page)

        self.nav.setCurrentRow(0)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.overview.refresh)
        self.timer.start(3000)

    def _switch(self, i):
        if i < 0:
            return
        self.stack.setCurrentIndex(i)
        page = self.stack.currentWidget()
        if hasattr(page, "reload"):
            page.reload()
        elif hasattr(page, "refresh"):
            page.refresh()

    def cycle_theme(self):
        """换一套配色：QSS、全局颜色、图表调色板一起换，再刷当前页。"""
        name = themes.next_name(THEME)
        apply_theme(name, QApplication.instance())
        self.btn_theme.setText(f"配色 · {name}")
        page = self.stack.currentWidget()
        for fn in ("reload", "refresh", "reload_log"):
            if hasattr(page, fn):
                try:
                    getattr(page, fn)()
                except Exception:
                    pass
        self._say(f"配色换成「{name}」，下次启动还是它")

    def open_wechat(self):
        if self.watch_win is None:
            self.watch_win = WeChatWindow(self)
        self.watch_win.show()
        self.watch_win.raise_()
        self.watch_win.activateWindow()

    # ---- 进程控制
    def _cmd(self, flag):
        """起子进程。失败要说出来 —— 静默失败是这套东西最坑的地方。"""
        if self.procs.get(flag) and self.procs[flag].poll() is None:
            return
        if flag == "--bridge" and port_open():
            self._say("桥已经在跑了（8765 被占用）。可能是上次没关干净，先点「停止」。")
            return
        # 源码模式下入口是 app.py，不是 gui.py —— 用 __file__ 会又起一个 GUI
        cmd = ([sys.executable, flag] if getattr(sys, "frozen", False)
               else [sys.executable, str(APP / "app.py"), flag])
        if flag == "--watch":
            # 只清「死的」锁：活的锁说明有监听在跑，清了就等于放行第二份。
            # 清锁必须在**起子进程之前** —— 以前是起完 0.6 秒再删，
            # 正好可能删掉子进程刚写进去的活锁。
            if clear_dead_lock(APP / ".watch.lock"):
                self._say("清掉了一个残留的监听锁（持有它的进程已经不在）")
        try:
            log = open(BRIDGE_LOG if flag == "--bridge" else APP / "watch.out.log", "ab")
            no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            self.procs[flag] = subprocess.Popen(cmd, stdout=log, stderr=log,
                                                cwd=str(APP), creationflags=no_win)
        except Exception as exc:
            QMessageBox.critical(self, "启动失败", f"{type(exc).__name__}: {exc}")
            self._say(f"启动{flag}失败：{exc}")
            return
        # 立刻退出的情况（缺模块、端口冲突）在窗口里看不出来，主动报一下
        time.sleep(0.6)
        if self.procs[flag].poll() is not None:
            tail_text = tail(BRIDGE_LOG if flag == "--bridge"
                             else APP / "watch.out.log", 12).strip()
            QMessageBox.critical(self, "启动后马上退出了",
                                 f"命令：{' '.join(cmd)}\n\n最后几行：\n{tail_text[-1200:]}")
            self._say(f"{flag} 起来就退了，看日志尾巴")
        self.overview.refresh()

    def _say(self, text):
        try:
            self.overview.hint.setText(text)
        except Exception:
            pass

    # ---- 余额
    def query_balance(self):
        if getattr(self, "balance_worker", None) and self.balance_worker.isRunning():
            return
        self.overview.btn_balance.setEnabled(False)
        self.balance_worker = BalanceWorker()
        self.balance_worker.done.connect(self.on_balance)
        self.balance_worker.start()

    def on_balance(self, res):
        self.balance_cache = res or {}
        self.overview.btn_balance.setEnabled(True)
        self.overview.show_balance()
        if res.get("ok"):
            self._say(f"余额 ¥{res.get('total')}（{time.strftime('%H:%M:%S')}）")
        else:
            self._say(f"查余额失败：{res.get('error')}")

    def _stop(self, flag):
        p = self.procs.get(flag)
        if p and p.poll() is None:
            p.terminate()
            for _ in range(20):
                if p.poll() is not None:
                    break
                time.sleep(0.1)
            if p.poll() is None:
                p.kill()
                for _ in range(20):
                    if p.poll() is not None:
                        break
                    time.sleep(0.1)
        if flag == "--watch":
            # 停完只清「死的」锁：外部起的监听可能还活着，删了它的锁就等于放行第二份
            clear_dead_lock(APP / ".watch.lock")
        self.overview.refresh()

    def start_bridge(self):
        self._cmd("--bridge")
        time.sleep(0.8)

    def stop_bridge(self):
        self._stop("--bridge")

    def start_watch(self, from_wx=False):
        self._cmd("--watch")

    def stop_watch(self, from_wx=False):
        self._stop("--watch")

    def start_all(self):
        """和以前一样：先起桥，等一秒再起监听。封存只是不主动弹窗，不是不给起。"""
        self.start_bridge()
        time.sleep(1.2)
        self.start_watch()
        self.overview.refresh()

    def stop_all(self):
        self.stop_watch()
        self.stop_bridge()
        self.overview.refresh()

    def watch_state(self):
        """(状态, 提示)。锁要分三种：自己的子进程在跑 / 别人的进程在跑 / 锁里的人是死的。"""
        p = self.procs.get("--watch")
        if p and p.poll() is None:
            return "运行中", ""
        pid, alive = lock_pid(APP / ".watch.lock")
        if pid is not None and alive:
            return "外部在跑", f"锁属于进程 {pid}（不是这个窗口起的），这里停不掉它"
        if pid is not None:
            return "锁残留", "持有锁的进程已经不在，点「停止」清掉锁再启动"
        return "已停", ""

    def closeEvent(self, ev):
        alive = [p for p in self.procs.values() if p.poll() is None]
        if alive:
            if QMessageBox.question(self, "退出", "桥/监听还在跑，一起停掉？") != QMessageBox.Yes:
                ev.ignore()
                return
            for flag in list(self.procs):
                self._stop(flag)
        ev.accept()


def main():
    app = QApplication(sys.argv)
    apply_theme(themes.saved(), app)      # 上次选的那套；首次运行是深色
    app.setFont(QFont("Microsoft YaHei UI", 9))
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
