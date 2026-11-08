"""自己写的微信 UI 自动化层：不依赖 wxauto，不注入，没有监听锁。

依据实测的控件树（微信 4.1.8.107）：

    WindowControl  class='mmui::ChatSingleWindow'      ← 每个独立聊天窗口
      ...
      ListControl  name='消息'  class='mmui::RecyclerListView'
        ListItemControl  name='文本'  class='mmui::ChatTextItemView'   ← 文本消息
        ListItemControl  name='18:42' class='mmui::ChatItemView'       ← 时间分隔
      EditControl  name='输入'  class='mmui::ChatInputField'
      ButtonControl name='发送' class='mmui::XOutlineButton'

核心约束：只读操作不激活窗口；只有发送时才需要短暂聚焦。
"""
import contextlib
import ctypes
import functools
import msvcrt
import os
import time
from ctypes import wintypes
from pathlib import Path

import uiautomation as auto

# 打包后 __file__ 落进 _internal\，所以路径一律从数据根起算
_HERE = Path(os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
_LOCK_PATH = _HERE / ".ui.lock"

# read_edit() 读失败时返回 "<读不到: XxxError>" 这种哨兵串，不是真实内容。
# 拿它跟要发的文本比较时一定要先识别，否则「读不到」会被当成「输入框变了 = 发成功」。
READ_FAIL_PREFIX = "<读不到"


@contextlib.contextmanager
def ui_lock(timeout=20.0):
    """跨进程互斥：bridge（主动搭话）和 watcher 都会操作微信窗口。

    两边同时 raise_window + SendKeys + 读写剪贴板时，按键会落到错误的窗口、
    剪贴板内容会互相覆盖（发图时还会把用户的剪贴板清空）。
    """
    f = open(_LOCK_PATH, "a+")
    got = False
    deadline = time.time() + timeout
    try:
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                got = True
                break
            except OSError:
                if time.time() > deadline:
                    break
                time.sleep(0.1)
        yield got
    finally:
        if got:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        f.close()


def _ui_locked(fn):
    """给发送类函数加锁的小装饰器。

    拿不到锁就**放弃这次发送**，不要硬跑：两个进程同时抬窗+SendKeys+
    读写剪贴板，按键会落进别的窗口，回复就发错人了。
    宁可少发，不可错发。

    ⚠ 锁不可重入（同一进程两个句柄也会互斥）：
    别给 ensure_foreground / raise_window / find_input 这类会被锁内调用的函数加这个装饰器，
    否则嵌套调用会立刻"拿不到锁"。
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with ui_lock() as got:
            if not got:
                return False, "拿不到窗口锁（另一个进程正在操作微信），本次放弃"
            return fn(*args, **kwargs)
    return wrapper


SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

# 不声明签名的话，GetDC 返回的句柄大于 2^31 时会报 "int too long to convert"
_user32.GetDC.restype = ctypes.c_void_p
_user32.GetDC.argtypes = [ctypes.c_void_p]
_user32.ReleaseDC.restype = ctypes.c_int
_user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_gdi32.GetPixel.restype = ctypes.c_uint32
_gdi32.GetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
_user32.GetClipboardSequenceNumber.restype = ctypes.c_uint
_user32.GetClipboardSequenceNumber.argtypes = []
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                             ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype = wintypes.BOOL
_kernel32 = ctypes.windll.kernel32
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_kernel32.GetCurrentThreadId.argtypes = []


def is_minimized(hwnd):
    """窗口是不是最小化状态。最小化的窗口 WGC 抓不到帧，得先还原。"""
    try:
        return bool(_user32.IsIconic(wintypes.HWND(hwnd)))
    except Exception:
        return False


WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
VK_RETURN = 0x0D


def post_enter(hwnd):
    """把 Enter **投递**给窗口，而不是用全局 SendKeys。

    实测（微信 4.1.8 / Qt 5.15）：顶层窗口会处理这条消息，消息照常发出，
    而且**不需要窗口在前台**。这样 Enter 绝不会打进别的窗口，
    是"发错窗口"这一类事故的根治办法。
    """
    try:
        h = wintypes.HWND(hwnd)
        ok1 = _user32.PostMessageW(h, WM_KEYDOWN, VK_RETURN, 0x001C0001)
        time.sleep(0.08)
        ok2 = _user32.PostMessageW(h, WM_KEYUP, VK_RETURN, 0xC01C0001)
        return bool(ok1 and ok2)
    except Exception:
        return False


def is_foreground(hwnd):
    """这个窗口现在是不是在前台。截屏/按键之前用它校验，避免操作到别的窗口。"""
    try:
        return int(_user32.GetForegroundWindow()) == int(hwnd)
    except Exception:
        return False


def ensure_foreground(hwnd, timeout=1.5):
    """确认目标窗口在前台，不在就抬一下再看。

    发送依赖全局键盘（Enter）和鼠标坐标（点发送按钮/右键图片），
    前台不是目标窗口时，这些输入会落到别的窗口里。
    """
    if hwnd is None:
        return True
    if is_foreground(hwnd):
        return True
    try:
        raise_window(hwnd)
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_foreground(hwnd):
            return True
        time.sleep(0.1)
    return is_foreground(hwnd)


def clipboard_seq():
    """剪贴板序号。只有序号变了，才说明"复制"真的生效；
    否则可能把用户原来就在剪贴板里的图当成对方发的图（隐私问题）。"""
    try:
        return int(_user32.GetClipboardSequenceNumber())
    except Exception:
        return -1

_user32.SetWindowPos.restype = ctypes.c_bool
_user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
_user32.SetForegroundWindow.restype = ctypes.c_bool
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.restype = ctypes.c_bool
_user32.BringWindowToTop.argtypes = [wintypes.HWND]


def enum_qt_windows():
    """列出所有 Qt 顶层窗口的 (hwnd, title)。"""
    rows = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cls, 256)
        if not cls.value.startswith("Qt"):
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        rows.append((hwnd, buf.value, bool(_user32.IsWindowVisible(hwnd))))
        return True

    _user32.EnumWindows(cb, 0)
    return rows


def window_rect(hwnd):
    rect = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


SW_HIDE = 0
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
OFFSCREEN = (-32000, -32000)
ONSCREEN = (120, 60, 860, 900)


def hide_window(hwnd):
    """把窗口移到屏幕外，**不用 SW_HIDE**。

    实测差别很大：
      - 移到屏幕外：窗口仍是"显示"状态，UIA 树完整，输入框还在（21:46 验证过）
      - SW_HIDE：窗口不渲染，输入框消失 → 发不出去（23:14 起连续失败）
    移动失败时退回最小化（控件树通常也还在）。
    """
    ok = _user32.SetWindowPos(
        wintypes.HWND(hwnd), wintypes.HWND(0),
        OFFSCREEN[0], OFFSCREEN[1], 0, 0,
        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
    )
    if ok:
        return True
    try:
        return bool(_user32.ShowWindow(wintypes.HWND(hwnd), SW_MINIMIZE))
    except Exception:
        return False


def show_window(hwnd):
    """把窗口移回屏幕内并置前，供发送使用。"""
    try:
        if _user32.IsIconic(wintypes.HWND(hwnd)):
            _user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
        x, y, w, h = ONSCREEN
        _user32.SetWindowPos(
            wintypes.HWND(hwnd), wintypes.HWND(0), x, y, w, h, SWP_NOZORDER,
        )
        _user32.SetForegroundWindow(wintypes.HWND(hwnd))
        return True
    except Exception:
        return False


def _pixel(hdc, x, y):
    """取屏幕上一点的 RGB。COLORREF 是 0x00BBGGRR。

    坐标必须先夹到合理范围，否则 GetPixel 会抛 OverflowError。
    """
    if not hdc:
        return None
    try:
        ix, iy = int(x), int(y)
    except (OverflowError, ValueError, TypeError):
        return None
    if not (-30000 <= ix <= 30000 and -30000 <= iy <= 30000):
        return None
    try:
        v = _gdi32.GetPixel(hdc, ix, iy)
    except Exception:
        return None
    if v == 0xFFFFFFFF:
        return None
    return (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)


def _dist(a, b):
    if a is None or b is None:
        return 0
    return sum(abs(x - y) for x, y in zip(a, b))


def classify_side(hdc, rect, width):
    """判断一条消息是我发的（靠右）还是对方发的（靠左）。

    消息项本身铺满整行、没有子控件，所以只能看像素：
    微信里自己发的气泡是绿色且贴右边，对方的是白色且贴左边。
    取左右两侧和中间三条采样带对比，偏离中间（背景）更远的一侧就是气泡/头像所在侧。
    """
    cy = (rect.top + rect.bottom) // 2
    if rect.bottom - rect.top < 6:
        return None
    center = _pixel(hdc, rect.left + width * 0.5, cy)
    left_dev = max(
        _dist(_pixel(hdc, rect.left + dx, cy), center) for dx in (10, 20, 32, 44)
    )
    right_dev = max(
        _dist(_pixel(hdc, rect.right - dx, cy), center) for dx in (10, 20, 32, 44)
    )
    return {
        "mine": right_dev > left_dev + 15,
        "left_dev": left_dev,
        "right_dev": right_dev,
        "center_rgb": center,
    }


def new_hdc():
    return _user32.GetDC(None)


def release_hdc(hdc):
    if hdc:
        _user32.ReleaseDC(None, hdc)


def find_chat_windows():
    """找出所有「独立聊天窗口」，返回 [(hwnd, 会话名, 控件对象)]。

    主判据是 UIA 类名 mmui::ChatSingleWindow。但腾讯改过类名
    （4.1.15 里整个无障碍树都被摘掉了），所以加了兜底：
    只要窗口里能找到名为「消息」的列表，就当聊天窗口。
    """
    found = []
    skip_titles = ("微信", "Weixin", "WxTrayIconMessageWindow", "")
    for hwnd, title, _visible in enum_qt_windows():
        if title in skip_titles:
            continue
        try:
            ctrl = auto.ControlFromHandle(hwnd)
            cls = ctrl.ClassName or ""
            # 会话名优先用窗口标题：窗口被隐藏时 ctrl.Name 会退化成 "Weixin"
            name = title or ctrl.Name
            if cls == "mmui::ChatSingleWindow" or cls.startswith("mmui::"):
                found.append((hwnd, name, ctrl))
                continue
            lst = ctrl.ListControl(searchDepth=14, Name="消息")
            if lst.Exists(0, 0):
                found.append((hwnd, name, ctrl))
        except Exception:
            continue
    return found


def diagnose():
    """排错快照：每个 Qt 窗口的 UIA 类名和子控件数量。"""
    rows = []
    for hwnd, title, visible in enum_qt_windows():
        if "Tray" in title or title == "WxTrayIconMessageWindow":
            continue
        try:
            ctrl = auto.ControlFromHandle(hwnd)
            rows.append({
                "hwnd": hwnd,
                "title": title,
                "uia_class": ctrl.ClassName or "",
                "children": len(ctrl.GetChildren()),
                "visible": visible,
            })
        except Exception as exc:
            rows.append({"hwnd": hwnd, "title": title, "error": str(exc)})
    return rows


def _message_list(win):
    lst = win.ListControl(searchDepth=14, Name="消息")
    return lst if lst.Exists(0, 0) else None


def read_messages(win, hwnd, max_items=40):
    """读一个独立聊天窗口的消息，返回 [{text, kind, mine, left_dev, right_dev}, ...]。

    mine 靠像素判断：消息项铺满整行、内部没有子控件，位置判据不可用，
    只能比左右两侧的颜色偏离（气泡/头像在哪边）。
    """
    lst = _message_list(win)
    if lst is None:
        return []

    left, top, right, bottom = window_rect(hwnd)
    width = max(1, right - left)
    hidden = left < -1000 or top < -1000  # 被藏到屏幕外的窗口，读不到像素
    if width <= 1 or width > 20000:
        # 窗口刚刷新时偶尔会拿到无效尺寸，退回屏幕宽度，避免坐标溢出
        width = 1920
    hdc = None if hidden else new_hdc()

    out = []
    try:
        items = lst.GetChildren()
    except Exception:
        release_hdc(hdc)
        return []
    for item in items[-max_items:]:
        try:
            text = (item.Name or "").strip()
            cls = item.ClassName or ""
            rect = item.BoundingRectangle
            side = classify_side(hdc, rect, width) or {}
            is_image = ("Refer" in cls) or (text == "图片")
            out.append({
                "text": text,
                "kind": cls,
                "mine": bool(side.get("mine")),
                "left_dev": side.get("left_dev", 0),
                "right_dev": side.get("right_dev", 0),
                "center_rgb": side.get("center_rgb"),
                "is_time": "ChatItemView" in cls and "Text" not in cls,
                "is_image": is_image,
                "bbox": (rect.left, rect.top, rect.right, rect.bottom),
            })
        except Exception:
            continue
    release_hdc(hdc)
    return out


def dump_tree(ctrl, depth=0, max_depth=4, out=None):
    """把控件树打成几行文本，排错用。"""
    if out is None:
        out = []
    if depth > max_depth:
        return out
    try:
        name = (ctrl.Name or "").replace("\n", " ")[:30]
        out.append(
            f"{'  ' * depth}{ctrl.ControlTypeName} name={name!r} cls={(ctrl.ClassName or '')!r}"
        )
    except Exception:
        return out
    try:
        kids = ctrl.GetChildren()
    except Exception:
        return out
    for kid in kids[:12]:
        dump_tree(kid, depth + 1, max_depth, out)
    return out


def diagnose_window(hwnd, max_lines=45):
    """排错：把窗口里跟发送相关的关键控件都点一遍名。"""
    lines = []
    try:
        ctrl = auto.ControlFromHandle(hwnd)
    except Exception as exc:
        return [f"取窗口失败: {exc}"]

    lines.append(f"窗口 name={ctrl.Name!r} cls={ctrl.ClassName!r}")

    lst = ctrl.ListControl(searchDepth=14, Name="消息")
    lines.append(f"消息列表 name='消息': {'有' if lst.Exists(0, 0) else '无'}")

    edit = ctrl.EditControl(searchDepth=14, Name="输入")
    lines.append(f"输入框 name='输入': {'有' if edit.Exists(0, 0) else '无'}")

    edits, inputs = [], []

    def walk(c, depth=0):
        if depth > 12:
            return
        try:
            ct = c.ControlTypeName
            if ct == "EditControl":
                edits.append((c.Name, c.ClassName))
            if (c.ClassName or "") in ("mmui::ChatInputField", "mmui::InputView"):
                inputs.append((c.ControlTypeName, c.Name, c.ClassName))
            for kid in c.GetChildren():
                walk(kid, depth + 1)
        except Exception:
            return

    walk(ctrl)
    lines.append(f"所有 EditControl: {edits[:10]}")
    lines.append(f"输入相关控件: {inputs[:10]}")
    lines.append("控件树(深度8):")
    lines.extend("  " + ln for ln in dump_tree(ctrl, max_depth=8)[:max_lines])
    return lines


def raise_window(hwnd):
    """把窗口提到最前。窗口叠在一起时，右键会点到被上层窗口遮住的位置。

    SetForegroundWindow 对后台进程经常被 Windows 静默拒绝（前台锁），
    所以失败了再走 AttachThreadInput 的常规绕法，并且**如实返回结果**。
    """
    import time

    try:
        h = wintypes.HWND(hwnd)
        if _user32.IsIconic(h):
            _user32.ShowWindow(h, SW_RESTORE)
            time.sleep(0.3)
        _user32.SetForegroundWindow(h)
        _user32.BringWindowToTop(h)
        time.sleep(0.2)
        if is_foreground(hwnd):
            return True
        # 前台锁：把自己挂到当前前台线程上再试一次
        fg = _user32.GetForegroundWindow()
        if fg and int(fg) != int(hwnd):
            tid_fg = _user32.GetWindowThreadProcessId(wintypes.HWND(fg), None)
            tid_me = _kernel32.GetCurrentThreadId()
            if tid_fg and tid_fg != tid_me:
                _user32.AttachThreadInput(tid_me, tid_fg, True)
                try:
                    _user32.SetForegroundWindow(h)
                    _user32.BringWindowToTop(h)
                    time.sleep(0.2)
                finally:
                    _user32.AttachThreadInput(tid_me, tid_fg, False)
        return is_foreground(hwnd)
    except Exception:
        return False


def ensure_visible(hwnd):
    """发送前保证窗口可用：最小化就还原，被挪出屏幕就移回来。

    读消息不需要窗口可见，但写输入框需要——最小化/离屏时输入框控件不存在。
    """
    import time

    changed = False
    try:
        if _user32.IsIconic(wintypes.HWND(hwnd)):
            _user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
            time.sleep(0.4)
            changed = True
        left, top, right, bottom = window_rect(hwnd)
        if left < -1000 or top < -1000:
            show_window(hwnd)
            time.sleep(0.4)
            changed = True
    except Exception:
        pass
    return changed


def find_any_input(win):
    """兜底：不按名字找，把窗口里所有 EditControl 找出来，优先 ChatInputField。"""
    found = []

    def walk(c, depth=0):
        if depth > 14:
            return
        try:
            if c.ControlTypeName == "EditControl":
                found.append(c)
            for kid in c.GetChildren():
                walk(kid, depth + 1)
        except Exception:
            return

    walk(win)
    for c in found:
        if "ChatInputField" in (c.ClassName or ""):
            return c
    return found[-1] if found else None


def find_input(win):
    edit = win.EditControl(searchDepth=14, Name="输入")
    if edit.Exists(0, 0):
        return edit
    return find_any_input(win)


def wait_input(hwnd, timeout=5.0):
    """等输入框出现，返回 (edit, 新的窗口控件对象)。

    窗口被 SW_HIDE 再显示时，UIA 树会重建，旧的对象句柄失效，
    所以这里每轮都重新 ControlFromHandle 一次，直到找到输入框。
    """
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fresh = auto.ControlFromHandle(hwnd)
            edit = fresh.EditControl(searchDepth=14, Name="输入")
            if edit.Exists(0, 0):
                return edit, fresh
        except Exception:
            pass
        time.sleep(0.25)
    return None, None


def is_visible(hwnd):
    return bool(_user32.IsWindowVisible(hwnd))


def find_send_button(win):
    btn = win.ButtonControl(searchDepth=14, Name="发送")
    return btn if btn.Exists(0, 0) else None


# ---------------------------------------------------------------- 发送节流
# 借鉴 wechatauto-replica 的 rhythm：对外可见的写动作要有间隔和冷却。
# 我们原来是「收到消息 → 防抖 2 秒 → 秒回」，这是最容易被抓的风控特征。
_RHYTHM_PATH = _HERE / "data" / "send_rhythm.json"
_RHYTHM_DEFAULT = {
    "enabled": True,
    "min_interval": [2.5, 6.0],   # 两次发送之间的随机间隔（秒）
    "burst": 7,                    # 窗口内最多几次
    "burst_window": 120.0,         # 窗口长度（秒）
    "cooldown": [30.0, 75.0],      # 超限后冷却多久（秒）
}


def _rhythm_cfg():
    """从 config.json 读节流参数；读不到就用默认值。"""
    cfg = dict(_RHYTHM_DEFAULT)
    try:
        import json as _json

        conf = _json.loads((_RHYTHM_PATH.parent.parent / "config.json")
                           .read_text(encoding="utf-8-sig"))
        cfg.update(conf.get("rhythm") or {})
    except Exception:
        pass
    return cfg


def _rhythm_load():
    import json as _json

    try:
        return _json.loads(_RHYTHM_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"last": 0.0, "times": [], "until": 0.0}


def _rhythm_save(st):
    import json as _json

    try:
        _RHYTHM_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _RHYTHM_PATH.with_suffix(".tmp")
        tmp.write_text(_json.dumps(st), encoding="utf-8")
        tmp.replace(_RHYTHM_PATH)
    except Exception:
        pass


def send_slot(max_wait=8.0):
    """预约一次发送。返回 (ok, 说明)。

    调用方必须已经持有 .ui.lock（send_text / send_image 里都是在锁内调的），
    所以这里的读写状态在两个进程之间是串行的。

    ok=False 表示「现在不该发」，调用方应该稍后重试而不是硬发。
    """
    import random
    import time

    cfg = _rhythm_cfg()
    if not cfg.get("enabled", True):
        return True, ""

    now = time.time()
    st = _rhythm_load()

    # 冷却中：剩下的时间超过 max_wait 就放弃本轮
    until = float(st.get("until") or 0)
    if now < until:
        remain = until - now
        if remain > max_wait:
            return False, f"节流冷却中（还要等 {remain:.0f} 秒），本轮不发"
        time.sleep(remain)
        now = time.time()

    # 两次发送之间要有随机间隔
    gap_lo, gap_hi = (cfg.get("min_interval") or [2.5, 6.0])[:2]
    want = random.uniform(float(gap_lo), float(gap_hi))
    last = float(st.get("last") or 0)
    wait = last + want - now
    if wait > 0:
        if wait > max_wait:
            return False, f"节流间隔中（还要等 {wait:.0f} 秒），本轮不发"
        time.sleep(wait)
        now = time.time()

    # 突发检测：窗口内次数超限 → 进冷却
    window = float(cfg.get("burst_window", 120.0))
    burst = int(cfg.get("burst", 7))
    times = [t for t in (st.get("times") or []) if now - t < window]
    if len(times) >= burst:
        cd_lo, cd_hi = (cfg.get("cooldown") or [30.0, 75.0])[:2]
        cd = random.uniform(float(cd_lo), float(cd_hi))
        # 进冷却时把窗口清零：冷却本身就是惩罚，
        # 不清零的话冷却结束后窗口里还是满的，会立刻再进一次冷却。
        st.update({"last": now, "times": [], "until": now + cd})
        _rhythm_save(st)
        return False, f"短时间内发太多了，冷却 {cd:.0f} 秒"

    times.append(now)
    st.update({"last": now, "times": times})
    _rhythm_save(st)
    return True, ""


def copy_image_to_clipboard(path):
    """把图片放进剪贴板（CF_DIB）。微信支持直接 Ctrl+V 粘贴图片发送。"""
    import io

    import win32clipboard
    import win32con
    from PIL import Image

    img = Image.open(path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "BMP")
    # BMP 文件头 14 字节要去掉，剪贴板要的是裸 DIB
    data = buf.getvalue()[14:]

    win32clipboard.OpenClipboard()
    try:
        # 备份用户剪贴板里的文字，发完恢复（图片没法备份，只能牺牲）
        backup_text = None
        try:
            backup_text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        except Exception:
            backup_text = None
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, data)
        _clipboard_backup["text"] = backup_text
    finally:
        win32clipboard.CloseClipboard()
    return True


_clipboard_backup = {"text": None}


def restore_clipboard_text():
    """把发图前备份的文字放回剪贴板（图片内容无法恢复）。"""
    import win32clipboard
    import win32con

    text = _clipboard_backup.get("text")
    if not text:
        return False
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception:
        return False


@_ui_locked
def send_image(win, path, caption=None, hwnd=None):
    """发一张图：贴到剪贴板 → 输入框 Ctrl+V → Enter。

    caption 非空时先输入文字再粘贴，微信会把图和文字一起发出去。
    hwnd 给了就先等输入框出现（窗口刚从隐藏恢复时需要）。
    """
    import time

    ok_slot, why_slot = send_slot()
    if not ok_slot:
        return False, why_slot

    edit = find_input(win)
    if edit is None and hwnd:
        edit, win = wait_input(hwnd)
    if edit is None:
        return False, "找不到输入框"

    info = {}
    # 微信不认 webp/heic：先转成 PNG 再贴，否则「粘贴成功但没图」，报错还查不出来
    try:
        import imgfmt

        path, note = imgfmt.prepare(path)
        if note:
            info["imgfmt"] = note
    except Exception as exc:
        return False, str(exc)

    try:
        copy_image_to_clipboard(path)
        info["clipboard"] = "ok"
    except Exception as exc:
        return False, f"写剪贴板失败: {type(exc).__name__}: {exc}"

    try:
        # Ctrl+V 和 Enter 都是全局输入，前台不对就会粘贴到别的窗口。
        # 注意：粘贴的是**图片**，贴到别人窗口属于隐私外泄，比发错文字更严重，
        # 所以 Ctrl+V 之前必须确认一次，不能只在 Enter 前查。
        if not ensure_foreground(hwnd):
            return False, "目标窗口不在前台，本次放弃发图"
        edit.Click(simulateMove=False)
        time.sleep(0.3)
        if caption:
            edit.GetValuePattern().SetValue(caption)
            time.sleep(0.2)
        if not ensure_foreground(hwnd):
            try:
                edit.GetValuePattern().SetValue("")
            except Exception:
                pass
            return False, "粘贴前窗口失去前台，已放弃发图（避免图片贴到别的窗口）"
        auto.SendKeys("{Ctrl}v", waitTime=0.05)
        time.sleep(0.9)  # 等缩略图在输入框里渲染出来
        info["after_paste"] = safe_value(edit)
        if not ensure_foreground(hwnd):
            # 粘贴之后焦点跑了：清掉输入框，别让 Enter 把图发到别处
            try:
                edit.GetValuePattern().SetValue("")
            except Exception:
                pass
            return False, "Enter 前窗口失去前台，已放弃发图"
        # 图已经在输入框里了；Enter 用投递，不用全局键盘
        if hwnd and post_enter(hwnd):
            time.sleep(0.8)
        else:
            auto.SendKeys("{Enter}", waitTime=0.05)
            time.sleep(0.8)
        info["after_enter"] = safe_value(edit)
    except Exception as exc:
        return False, f"粘贴/发送失败: {type(exc).__name__}: {exc}"
    finally:
        # 用完把用户原来的文字剪贴板还回去（图片内容确实恢复不了）
        try:
            restore_clipboard_text()
        except Exception:
            pass

    return True, str(info)


def capture_window(hwnd, flags=3):
    """用 PrintWindow 把窗口渲染到内存位图，返回 PNG 字节。

    和截屏的区别：不需要窗口在屏幕上、不被遮挡也能拿到内容。
    这样窗口移到 -32000 之后，依然能拿到画面（看图才成立）。
    失败返回 None。
    """
    import io

    import win32gui
    import win32ui
    from PIL import Image

    try:
        left, top, right, bottom = window_rect(hwnd)
        w, hgt = right - left, bottom - top
        if w < 8 or hgt < 8:
            return None
        hwnd_dc = win32gui.GetWindowDC(wintypes.HWND(hwnd))
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, w, hgt)
        save_dc.SelectObject(bitmap)
        ok = ctypes.windll.user32.PrintWindow(wintypes.HWND(hwnd), save_dc.GetSafeHdc(), flags)
        if not ok:
            return None
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1
        )
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return None
    finally:
        try:
            win32gui.DeleteObject(bitmap.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(wintypes.HWND(hwnd), hwnd_dc)
        except Exception:
            pass


def find_content_box(bbox, padding=8):
    """在消息行里找出内容块的矩形（用于裁表情、缩小截图范围）。

    返回 (left, top, right, bottom)；找不到返回 None。
    """
    from PIL import ImageGrab

    left, top, right, bottom = [int(v) for v in bbox]
    w, h = right - left, bottom - top
    if w < 16 or h < 16:
        return None
    try:
        img = ImageGrab.grab(bbox=(left, top, right, bottom)).convert("L")
    except Exception:
        return None

    sw = min(240, w)
    sh = max(8, min(48, h))
    img = img.resize((sw, sh))
    px = img.load()

    cols = []
    for x in range(sw):
        vals = [px[x, y] for y in range(sh)]
        mean = sum(vals) / sh
        cols.append(sum((v - mean) ** 2 for v in vals) / sh)

    peak = max(cols) if cols else 0
    threshold = max(15.0, peak * 0.12)
    runs, start = [], None
    for x, v in enumerate(cols):
        if v > threshold and start is None:
            start = x
        elif v <= threshold and start is not None:
            runs.append([start, x])
            start = None
    if start is not None:
        runs.append([start, sw])

    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] <= 4:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    if not merged:
        return None

    begin, end = max(merged, key=lambda r: r[1] - r[0])
    if end - begin < 3:
        return None

    x1 = left + int(begin * w / sw) - padding
    x2 = left + int(end * w / sw) + padding
    return (max(left, x1), top, min(right, x2), bottom)


def find_content_center(bbox):
    """在一行消息的矩形里，找出真正有内容的那块，返回它的中心坐标。

    优化点：
      - 先缩到 240 宽再分析，比逐像素快一个数量级
      - 太窄的空隙（< 4 列）合并，避免头像和气泡被拆成两块
    消息项的 UIA 外框铺满整行，直接取中心会落在空白处，所以必须找内容块。
    """
    from PIL import ImageGrab

    left, top, right, bottom = [int(v) for v in bbox]
    w, h = right - left, bottom - top
    if w < 16 or h < 16:
        return None
    try:
        img = ImageGrab.grab(bbox=(left, top, right, bottom)).convert("L")
    except Exception:
        return None

    sw = min(240, w)
    sh = max(8, min(48, h))
    img = img.resize((sw, sh))
    px = img.load()

    cols = []
    for x in range(sw):
        vals = [px[x, y] for y in range(sh)]
        mean = sum(vals) / sh
        cols.append(sum((v - mean) ** 2 for v in vals) / sh)

    peak = max(cols) if cols else 0
    threshold = max(15.0, peak * 0.12)

    runs, start = [], None
    for x, v in enumerate(cols):
        if v > threshold and start is None:
            start = x
        elif v <= threshold and start is not None:
            runs.append([start, x])
            start = None
    if start is not None:
        runs.append([start, sw])

    # 合并间隔很小的相邻块（头像 | 空隙 | 气泡）
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] <= 4:
            merged[-1][1] = r[1]
        else:
            merged.append(r)

    if not merged:
        return None
    begin, end = max(merged, key=lambda r: r[1] - r[0])
    if end - begin < 3:
        return None

    cx = left + int((begin + end) / 2 * w / sw)
    cy = top + h // 2
    return cx, cy


def find_menu_window():
    """找出微信刚弹出的右键菜单窗口（UIA class 以 mmui::XMenu 开头）。"""
    for hwnd, _title, _visible in enum_qt_windows():
        try:
            ctrl = auto.ControlFromHandle(hwnd)
            if (ctrl.ClassName or "").startswith("mmui::XMenu"):
                return ctrl
        except Exception:
            continue
    return None


def find_image_menu(timeout=0.8):
    """等右键菜单出现，并确认它是**图片**的菜单（有「另存为」）。

    文字消息的菜单里只有复制/引用/转发，没有「另存为」——
    用这一点区分，避免把文字复制到剪贴板当成图。
    """
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        menu = find_menu_window()
        if menu is not None:
            try:
                if menu.MenuItemControl(searchDepth=4, Name="另存为..."):
                    save_as = menu.MenuItemControl(searchDepth=4, Name="另存为...")
                    if save_as.Exists(0, 0):
                        return menu
            except Exception:
                pass
        time.sleep(0.12)
    return None


@_ui_locked
def copy_image_via_menu(bbox, timeout=3.0, hwnd=None):
    """右键图片 → 点「复制」→ 从剪贴板取原图。

    返回 (状态, PNG字节)：
      ("ok", data)       拿到原图
      ("not_image", None) 右键菜单出现了，但没有「另存为」——这是表情包/文件等，
                          别再去截屏，否则会把表情当图片发给模型
      ("failed", None)   所有候选点都没弹出图片菜单（点偏/窗口被挡）

    hwnd 给了就先确认窗口在前台：右键是**屏幕坐标**操作，
    窗口被挡或不在前台时，会点到盖在上面的别的程序上。
    """
    import io
    import time

    from PIL import ImageGrab

    if hwnd is not None and not ensure_foreground(hwnd):
        return "failed", None

    left, top, right, bottom = [int(v) for v in bbox]
    w, h = right - left, bottom - top
    if w < 16 or h < 16:
        return "failed", None

    candidates = []
    center = find_content_center(bbox)
    if center:
        candidates.append(center)
    candidates += [
        (left + w // 2, top + h // 2),
        (left + int(w * 0.35), top + h // 2),
        (left + int(w * 0.65), top + h // 2),
        (left + w // 2, top + int(h * 0.35)),
        (left + w // 2, top + int(h * 0.65)),
    ]

    saw_menu = False
    seen = set()
    for cx, cy in candidates:
        if (cx, cy) in seen:
            continue
        seen.add((cx, cy))
        try:
            auto.RightClick(cx, cy, waitTime=0.05)
        except Exception:
            continue

        menu = find_menu_window()
        if menu is None:
            # 没弹出菜单就什么都别按：此时前台是聊天窗口，按 Esc 会把它关掉，
            # 之后所有发送都会「找不到输入框」。
            time.sleep(0.15)
            continue

        saw_menu = True
        item = menu.MenuItemControl(searchDepth=4, Name="复制")
        has_save_as = False
        has_sticker = False
        try:
            save_as = menu.MenuItemControl(searchDepth=4, Name="另存为...")
            has_save_as = save_as.Exists(0, 0)
        except Exception:
            has_save_as = False
        try:
            add_sticker = menu.MenuItemControl(searchDepth=4, Name="添加到表情")
            has_sticker = add_sticker.Exists(0, 0)
        except Exception:
            has_sticker = False

        # 三态判定：
        #   有「另存为」     → 真图片
        #   有「添加到表情」 → 表情包
        #   两个都没有       → 点偏了（点到空白/文字），换下一个候选点，别误判成表情
        if not has_save_as:
            auto.SendKeys("{Esc}", waitTime=0.05)
            time.sleep(0.1)
            if has_sticker:
                return "not_image", None
            continue

        if not item.Exists(0, 0):
            auto.SendKeys("{Esc}", waitTime=0.05)
            continue
        # 先记剪贴板序号：只有序号变了，才说明这次复制真的生效
        seq_before = clipboard_seq()
        try:
            item.Click(simulateMove=False)
        except Exception:
            try:
                item.GetInvokePattern().Invoke()
            except Exception:
                auto.SendKeys("{Esc}", waitTime=0.05)
                continue

        deadline = time.time() + timeout
        while time.time() < deadline:
            if clipboard_seq() != seq_before:
                # 序号变了，等图片数据落进剪贴板（有时比序号慢半拍）
                for _ in range(8):
                    try:
                        data = ImageGrab.grabclipboard()
                    except Exception:
                        data = None
                    if data is not None and not isinstance(data, list):
                        buf = io.BytesIO()
                        data.convert("RGB").save(buf, "PNG")
                        return "ok", buf.getvalue()
                    time.sleep(0.12)
                break
            time.sleep(0.15)
        return "ok", None

    return ("not_image", None) if saw_menu else ("failed", None)


def grab_rect(bbox):
    """截取屏幕上的一块区域，返回 PNG 字节。用于把对方发的图交给视觉模型。"""
    import io

    from PIL import ImageGrab

    left, top, right, bottom = [int(v) for v in bbox]
    if right - left < 8 or bottom - top < 8:
        return None
    img = ImageGrab.grab(bbox=(left, top, right, bottom))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def safe_value(edit):
    try:
        return edit.GetValuePattern().Value
    except Exception as exc:
        return f"{READ_FAIL_PREFIX}: {type(exc).__name__}>"


def _norm_cmp(s):
    """比对用：把空白全去掉。

    微信把换行吃掉、UIA 取到的 Name 偶尔多一个空格，这些都不该算「没发出去」。
    """
    return "".join((s or "").split())


def _matches_sent(texts, text):
    """我们这条 text 在 texts 里出现了没有（容忍空白差异和单条被截断）。"""
    want = _norm_cmp(text)
    if not want:
        return False
    for t in texts or ():
        got = _norm_cmp(t)
        if not got:
            continue
        if got == want or want in got or got in want:
            return True
        # UIA 有时把长文本截断再加个省略号：去掉尾巴还能对上就算
        short = got.rstrip("…·. ")
        if short and short != got and (want.startswith(short) or short in want):
            return True
    return False


def confirm_sent(win, hwnd, text, timeout=2.0, reader=None):
    """发完核对：消息列表里到底有没有出现这条。

    返回 True / False / None，None 表示**核不了**（读不到消息列表），
    这时候不能判发送失败 —— 窗口藏起来、列表还没建好都会读不到。

    为什么要这一步：输入框清空只是**代理信号**。抖音那个桥接项目里踩过同一类坑
    （只看输入框空不空 → 假成功），正确做法是看**效果**：消息列表里多出来没有。
    微信这边 Enter 偶尔会被焦点抢走、点击发送按钮也可能点到别处，
    那时候输入框照样是空的，但消息根本没出去。
    """
    import time

    reader = reader or (lambda w, h: read_messages(w, h, max_items=6))
    deadline = time.time() + max(0.0, timeout)
    while True:
        try:
            items = reader(win, hwnd)
        except Exception:
            return None
        if not items:                      # 读不到列表 ≠ 没发出去
            return None
        texts = [it.get("text") or "" for it in items if not it.get("is_time")]
        if _matches_sent(texts, text):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


@_ui_locked
def send_text(win, text, mode="value", hwnd=None, guard=None):
    """发一条文本，返回 (ok, 诊断信息)。

    实测结论（微信 4.1.8 + 本文档的 Qt 控件）：
      - ValuePattern.SetValue 能把文字写进输入框，且不会掉字
      - 对「发送」按钮 InvokePattern.Invoke 无效，点了不发
      - 所以发送这一步必须用键盘 Enter，失败再退回坐标点击按钮

    hwnd 给了就先等输入框出现（窗口刚从隐藏恢复时 UIA 树要重建）。
    guard 是拿到锁之后才执行的一次检查（比如"用户是不是正在打字"）：
    必须在锁内做，否则检查和真正写之间会留出窗口期。
    """
    import time

    if guard is not None and not guard():
        return False, "你正在操作键盘鼠标，本次不发"

    # 节流：两次发送之间要有间隔，短时间发太多就进冷却（防风控）
    ok_slot, why_slot = send_slot()
    if not ok_slot:
        return False, why_slot

    info = {}
    edit = find_input(win)
    if edit is None and hwnd:
        edit, win = wait_input(hwnd)
        info["waited"] = edit is not None
    if edit is None:
        return False, "找不到输入框"

    # 先记下用户原来在用的窗口。SetValue 会把目标窗口顶到前台，
    # 发完要还回去，否则你正在看的窗口会被抢走。
    prev_fg = int(_user32.GetForegroundWindow() or 0)
    was_fg = is_foreground(hwnd)

    def read_edit():
        try:
            return edit.GetValuePattern().Value
        except Exception as exc:
            return f"{READ_FAIL_PREFIX}: {type(exc).__name__}>"

    if mode == "keyboard":
        edit.Click(simulateMove=False)
        time.sleep(0.3)
        auto.SendKeys("{Ctrl}a", waitTime=0.02)
        auto.SendKeys(text, waitTime=0.05)
    else:
        try:
            # 先清空：输入框里偶尔会残留上次没发干净的字符（见过多出 "ng"）
            edit.GetValuePattern().SetValue("")
            time.sleep(0.1)
            edit.GetValuePattern().SetValue(text)
        except Exception as exc:
            return False, f"SetValue 失败: {type(exc).__name__}: {exc}"
    time.sleep(0.25)
    info["in_box"] = read_edit()

    if info.get("in_box") != text:
        # 写进去的内容不对（残留/掉字），别把垃圾发出去
        try:
            edit.GetValuePattern().SetValue("")
        except Exception:
            pass
        return False, f"输入框内容不符: {info.get('in_box')!r}"

    # 首选：投递 Enter。它不需要前台，Enter 就不可能打进别的窗口。
    if hwnd:
        if not was_fg and prev_fg and prev_fg != int(hwnd):
            # SetValue 把窗口抬起来了，先把焦点还给用户原来在用的窗口
            try:
                raise_window(prev_fg)
            except Exception:
                pass
        if post_enter(hwnd):
            time.sleep(0.5)
            after = read_edit()
            info["after_post"] = after
            if after != text:
                if str(after).startswith(READ_FAIL_PREFIX):
                    # 读不到输入框 ≠ 发送成功。这里**不能**退回去再按一次 Enter
                    # （可能已经发出去了，重按就是对方收到两遍），但至少读一眼
                    # 消息列表，把「到底发出去没」写进日志，别静默当成功。
                    got = confirm_sent(win, hwnd, text)
                    info["confirm_after_post"] = (
                        "列表里看到了" if got else
                        ("核不了（读不到列表）" if got is None else "列表里没看到"))
                return True, str(info)
            info["post_failed"] = True      # 投递没生效，退到下面老办法

    # 焦点必须在输入框里，Enter 才会被当成"发送"
    if not ensure_foreground(hwnd):
        # 写字的这几百毫秒里焦点被抢走了：清空输入框，绝不按 Enter
        try:
            edit.GetValuePattern().SetValue("")
        except Exception:
            pass
        return False, "Enter 前窗口失去前台，已放弃（避免发到别的窗口）"
    try:
        edit.Click(simulateMove=False)
        time.sleep(0.15)
    except Exception as exc:
        info["focus_error"] = str(exc)

    auto.SendKeys("{Enter}", waitTime=0.05)
    time.sleep(0.5)
    info["after_enter"] = read_edit()

    if info.get("after_enter") == text:
        # Enter 没生效，退回：点发送按钮的坐标
        btn = find_send_button(win)
        if btn is not None and ensure_foreground(hwnd):
            try:
                r = btn.BoundingRectangle
                auto.Click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
                time.sleep(0.5)
                info["after_click"] = read_edit()
            except Exception as exc:
                info["click_error"] = str(exc)

    left_over = info.get("after_click", info.get("after_enter"))
    if left_over == text:
        # 还在框里，说明没发出去；清掉免得下次粘进新消息
        try:
            edit.GetValuePattern().SetValue("")
        except Exception:
            pass
        return False, str(info)

    # 输入框空了只是代理信号，最后再看一眼消息列表里有没有真的多出这条。
    # 核不了（None）就照常算成功，只有明确看不到才算失败。
    confirmed = confirm_sent(win, hwnd, text)
    info["confirm"] = "列表里看到了" if confirmed else (
        "核不了（读不到列表）" if confirmed is None else "列表里没看到")
    if confirmed is False:
        return False, f"{info} | 输入框清了但消息列表里没出现"

    return True, str(info)
