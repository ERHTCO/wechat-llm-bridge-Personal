"""读「显示器现在开着还是关着」，以及核对关屏指令到底有没有生效。

为什么要有这个：`SC_MONITORPOWER` 那条广播只是**请求**，`PostMessage` 返回 True
只说明消息投出去了，不代表屏幕黑了。2026-11-08 凌晨排查「息屏好像坏了」，
最后发现屏幕关得很好，是别的东西把它叫醒 —— 但当时手头没有任何办法**确认**屏幕状态，
只能靠人盯着看。这个模块补的就是这一块。

怎么读状态：注册 `GUID_CONSOLE_DISPLAY_STATE` 的电源通知。注册的瞬间系统会先把
当前状态发一次，所以「注册 + 泵 0.3 秒」就等于一次查询；之后状态变化也会继续收到。

三个坑，都踩过，别再踩：

1. **窗口必须是可见的。** style=0 的隐藏窗口收不到 `WM_POWERBROADCAST`，
   会一直以为屏幕是开的 —— 第一版探针就是这样骗了自己一整晚。
2. **wParam 不一定是你以为的那个值。** 文档里 `PBT_POWERSETTINGCHANGE` 是 0x8014，
   这台机器的 Win10 19045 实际发的是 0x8013。两个都收，别只认一个。
3. **`WM_POWERBROADCAST` 不能 `PostMessage`。** 系统消息区（< WM_USER）里这类消息
   只接受同步投递，`PostMessage` 会返回 `ERROR_MESSAGE_SYNC_ONLY`(1159)；
   自己测试要用 `SendMessage`。

用法：

    import dispstate
    print(dispstate.query_state())          # 0/1/2，读不到是 None
    ok, why = dispstate.monitor_off()       # 发关屏指令，核对，必要时重试
"""
import ctypes
import struct
import time
from ctypes import byref, Structure, WINFUNCTYPE, wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_POWERBROADCAST = 0x0218
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
HWND_BROADCAST = 0xFFFF

# 0x8014 是文档里的 PBT_POWERSETTINGCHANGE；0x8013 是本机实际发的那个
PBT_POWERSETTINGCHANGE = 0x8014
PBT_POWERINFOCHANGE = 0x8013
POWER_WPARAMS = (PBT_POWERSETTINGCHANGE, PBT_POWERINFOCHANGE)

DEVICE_NOTIFY_WINDOW_HANDLE = 0

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

DISPLAY_OFF = 0
DISPLAY_ON = 1
DISPLAY_DIM = 2
STATE_TEXT = {DISPLAY_OFF: "关", DISPLAY_ON: "开", DISPLAY_DIM: "变暗"}

GUID_CONSOLE_DISPLAY_STATE = (0x6FE69556, 0x704A, 0x47A0,
                              (0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0xDA, 0x47))
GUID_MONITOR_POWER_ON = (0x02731015, 0x4510, 0x4526,
                         (0x99, 0xE6, 0xE5, 0xA1, 0x7E, 0xBD, 0x1A, 0xEA))

PROBE_CLASS = "wxbridge_dispstate"


def state_text(value):
    """把状态码翻成人话。读不到（None）说「没读到」。"""
    if value is None:
        return "没读到"
    return STATE_TEXT.get(value, f"未知({value})")


# ---------------------------------------------------------------- 纯函数（好测）
def decode_setting(raw):
    """解 POWERBROADCAST_SETTING 的字节。返回 (GUID 元组, DataLength, 值)。

    GUID 16 字节 + DataLength 4 字节 + Data（这里只取前 4 字节当整数）。
    解不了就抛 ValueError，调用方自己兜。
    """
    if not raw or len(raw) < 20:
        raise ValueError("POWERBROADCAST_SETTING 太短")
    d1, d2, d3 = struct.unpack_from("<IHH", raw, 0)
    tail = tuple(raw[8:16])
    length = struct.unpack_from("<I", raw, 16)[0]
    data = raw[20:20 + min(int(length), 4)]
    value = int.from_bytes(data, "little") if data else None
    return (d1, d2, d3, tail), length, value


# ---------------------------------------------------------------- 窗口 + 消息泵
class _GUID(Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _WNDCLASSEXW(Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM)),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


_keep = {}
_class_ready = False


def _ensure_class(cls):
    """注册窗口类，**整个进程只成功一次**。

    Windows 的窗口类是进程级的：第一次 RegisterClassExW 成功，之后同名再注册
    会返回 0（ERROR_CLASS_ALREADY_EXISTS=1410）。销毁窗口不会注销类。
    第一版每次 Listener.__enter__ 都注册、失败就 raise，结果是**同一进程里
    第二次读显示器状态必然失败** —— watcher 每晚要读很多次，第二次起就全部
    报「读不到显示器状态」，比不核对更误导人。
    """
    global _class_ready
    if _class_ready:
        return True
    if user32.RegisterClassExW(ctypes.byref(cls)):
        _class_ready = True
        return True
    if ctypes.GetLastError() == _ERROR_CLASS_ALREADY_EXISTS:
        _class_ready = True
        return True
    return False


_ERROR_CLASS_ALREADY_EXISTS = 1410


def _guid_struct(key):
    return _GUID(key[0], key[1], key[2], (ctypes.c_ubyte * 8)(*key[3]))


def _declare():
    """64 位下不声明 argtypes，lparam 会溢出（OverflowError）。"""
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
    user32.RegisterClassExW.restype = wintypes.WORD
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                       wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.RegisterPowerSettingNotification.argtypes = [wintypes.HANDLE,
                                                        ctypes.POINTER(_GUID),
                                                        wintypes.DWORD]
    user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                    wintypes.UINT, wintypes.UINT, wintypes.UINT]
    user32.PeekMessageW.restype = wintypes.BOOL


def _pump(seconds):
    """泵消息 seconds 秒。同步投递（SendMessage）进来的广播是在这里被分发的。"""
    msg = wintypes.MSG()
    end = time.time() + seconds
    while time.time() < end:
        if user32.PeekMessageW(byref(msg), None, 0, 0, 1):   # PM_REMOVE
            user32.TranslateMessage(byref(msg))
            user32.DispatchMessageW(byref(msg))
        else:
            time.sleep(0.02)


# 当前正在听的那个 Listener。窗口过程是**进程级**的（窗口类只注册一次），
# 所以必须有一个模块级入口把消息转给「现在这个」，否则第二次 Listener
# 建的窗口会把广播投给第一个（已经销毁的）实例，状态永远是 None。
_current = None


def _dispatch(hwnd, msg, wparam, lparam):
    lst = _current
    if msg == WM_POWERBROADCAST:
        if wparam in POWER_WPARAMS and lst is not None:
            lst._on_broadcast(wparam, lparam)
        return 1
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_DISPATCH = None


def _make_dispatch():
    global _DISPATCH
    if _DISPATCH is None:
        _DISPATCH = WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM)(_dispatch)
    return _DISPATCH


class Listener:
    """建一个可见的隐藏窗口听电源广播。用它 when 需要「等状态变化」。"""

    def __init__(self):
        self.hwnd = 0
        self.state = None
        self._dirty = True

    # -- 内部
    def _on_broadcast(self, wparam, lparam):
        try:
            raw = ctypes.string_at(lparam, 24)
            key, _length, value = decode_setting(raw)
        except Exception:
            return
        if key == GUID_CONSOLE_DISPLAY_STATE:
            self.state = value
            self._dirty = True
        elif key == GUID_MONITOR_POWER_ON and self.state is None:
            # 旧接口只有开/关两种值，没别的信息时当兜底
            self.state = DISPLAY_ON if value else DISPLAY_OFF

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_POWERBROADCAST:
            if wparam in POWER_WPARAMS:
                self._on_broadcast(wparam, lparam)
            return 1
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # -- 对外
    def __enter__(self):
        global _current
        _declare()
        hinst = kernel32.GetModuleHandleW(None)
        proc = _make_dispatch()
        cls = _WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(cls)
        cls.lpfnWndProc = proc
        cls.hInstance = hinst
        cls.lpszClassName = PROBE_CLASS
        if not _ensure_class(cls):
            raise OSError(f"注册窗口类失败: {ctypes.GetLastError()}")
        # 坑 1：必须可见，隐藏窗口收不到电源广播
        self.hwnd = user32.CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                                           PROBE_CLASS, "dispstate",
                                           WS_POPUP | WS_VISIBLE,
                                           0, 0, 1, 1, None, None, hinst, None)
        if not self.hwnd:
            raise OSError(f"CreateWindowExW 失败: {ctypes.GetLastError()}")
        _current = self            # 广播统一转给「现在这个」
        _keep["proc"] = proc
        for key in (GUID_CONSOLE_DISPLAY_STATE, GUID_MONITOR_POWER_ON):
            struct_guid = _guid_struct(key)
            user32.RegisterPowerSettingNotification(wintypes.HANDLE(self.hwnd),
                                                    byref(struct_guid),
                                                    DEVICE_NOTIFY_WINDOW_HANDLE)
        # 注册的瞬间系统会把当前状态发一次
        _pump(0.35)
        return self

    def __exit__(self, *exc):
        global _current
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = 0
        if _current is self:
            _current = None
        return False

    def wait(self, targets, timeout=2.0):
        """等到状态落在 targets 里（或超时）。返回 (是否等到, 最后状态)。"""
        if self.state in targets:
            return True, self.state
        end = time.time() + timeout
        while time.time() < end:
            _pump(0.1)
            if self.state in targets:
                return True, self.state
        return False, self.state


def query_state(timeout=0.6):
    """问一次：0=关 1=开 2=变暗，读不到返回 None。"""
    try:
        with Listener() as lst:
            if lst.state is None and timeout > 0:
                _pump(timeout)
            return lst.state
    except Exception:
        return None


def wait_off(timeout=2.0):
    """等屏幕变成「关」。注册时要是已经关了，立刻返回。

    发送关屏指令后系统要几百毫秒才真正关，所以这里必须**等**，
    不能像 query_state 那样拿一次快照 —— 否则每次都会误判成「没关掉」。
    """
    try:
        with Listener() as lst:
            if lst.state == DISPLAY_OFF:
                return DISPLAY_OFF
            _got, state = lst.wait((DISPLAY_OFF,), timeout)
            return state
    except Exception:
        return None


# ---------------------------------------------------------------- 关屏
def send_off():
    """发「关闭显示器」。返回消息有没有投出去（不代表屏幕真黑了）。"""
    try:
        return bool(user32.PostMessageW(wintypes.HWND(HWND_BROADCAST), WM_SYSCOMMAND,
                                        SC_MONITORPOWER, 2))
    except Exception:
        return False


def send_on():
    """叫醒显示器（等价于动一下鼠标，但不动鼠标）。"""
    try:
        return bool(user32.PostMessageW(wintypes.HWND(HWND_BROADCAST), WM_SYSCOMMAND,
                                        SC_MONITORPOWER, ctypes.c_ssize_t(-1).value))
    except Exception:
        return False


def monitor_off(attempts=3, wait=2.0, send=None, read=None):
    """发关屏指令并**核对**，没关成就重试。

    返回 (状态, 说明)：
        状态 True  = 已确认关了
        状态 False = 发了但屏幕还是亮的（或压根没发出去）
        状态 None  = 发出去了，但读不到显示器状态（没法确认）

    send/read 可以注入，测试用；默认就是真的发、真的等。
    """
    send = send or send_off
    read = read or wait_off
    last = None
    for i in range(1, max(1, attempts) + 1):
        if not send():
            return False, f"第 {i} 次关屏指令没发出去"
        last = read(wait)
        if last == DISPLAY_OFF:
            return True, f"显示器已关（第 {i} 次指令确认）"
        if last is None:
            return None, "指令发出去了，但读不到显示器状态，无法确认"
        if i < attempts:
            time.sleep(0.4)
    return False, f"发了 {attempts} 次，显示器还是{state_text(last)}"
