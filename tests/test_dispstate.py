"""dispstate 的测试：解广播字节 + 关屏核对的重试逻辑。

    .venv\\Scripts\\python.exe tests\\test_dispstate.py

**不建窗口、不碰真屏幕**：窗口只在真正查询时创建，这里全部注入假实现。
真机上「读显示器状态」要人工看一眼才信得过，不适合放进自动测试。
"""
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import dispstate  # noqa: E402

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


def setting_bytes(guid, value, length=4):
    """照着 POWERBROADCAST_SETTING 拼一段字节：GUID(16) + DataLength(4) + Data。"""
    d1, d2, d3, tail = guid
    return (struct.pack("<IHH", d1, d2, d3) + bytes(tail)
            + struct.pack("<I", length) + struct.pack("<I", value))


# ---------------------------------------------------------------- 常量
def test_constants():
    check("关/开/变暗 是 0/1/2",
          (dispstate.DISPLAY_OFF, dispstate.DISPLAY_ON, dispstate.DISPLAY_DIM) == (0, 1, 2))
    check("两个 wParam 都收（0x8013 是本机实测的）",
          0x8014 in dispstate.POWER_WPARAMS and 0x8013 in dispstate.POWER_WPARAMS,
          dispstate.POWER_WPARAMS)
    check("GUID_CONSOLE_DISPLAY_STATE 拼对了",
          dispstate.GUID_CONSOLE_DISPLAY_STATE[0] == 0x6FE69556
          and dispstate.GUID_CONSOLE_DISPLAY_STATE[1] == 0x704A
          and dispstate.GUID_CONSOLE_DISPLAY_STATE[2] == 0x47A0
          and dispstate.GUID_CONSOLE_DISPLAY_STATE[3][0] == 0x8F)
    check("SC_MONITORPOWER 常量没被改坏",
          (dispstate.WM_SYSCOMMAND, dispstate.SC_MONITORPOWER) == (0x0112, 0xF170))


def test_state_text():
    check("0 → 关", dispstate.state_text(0) == "关")
    check("1 → 开", dispstate.state_text(1) == "开")
    check("2 → 变暗", dispstate.state_text(2) == "变暗")
    check("None → 没读到", dispstate.state_text(None) == "没读到")
    check("没见过的值照实说", "未知" in dispstate.state_text(9), dispstate.state_text(9))


# ---------------------------------------------------------------- 解字节
def test_decode():
    raw = setting_bytes(dispstate.GUID_CONSOLE_DISPLAY_STATE, 0)
    key, length, value = dispstate.decode_setting(raw)
    check("解出 GUID", key == dispstate.GUID_CONSOLE_DISPLAY_STATE, key)
    check("解出 DataLength", length == 4, length)
    check("解出 0（关）", value == 0, value)

    key, _l, value = dispstate.decode_setting(
        setting_bytes(dispstate.GUID_CONSOLE_DISPLAY_STATE, 1))
    check("解出 1（开）", value == 1, value)
    key, _l, value = dispstate.decode_setting(
        setting_bytes(dispstate.GUID_CONSOLE_DISPLAY_STATE, 2))
    check("解出 2（变暗）", value == 2, value)

    other = setting_bytes(dispstate.GUID_MONITOR_POWER_ON, 1)
    key, _l, value = dispstate.decode_setting(other)
    check("换一个 GUID 也能解", key == dispstate.GUID_MONITOR_POWER_ON and value == 1)

    try:
        dispstate.decode_setting(b"\x00" * 8)
        ok = False
    except ValueError:
        ok = True
    check("字节太短抛 ValueError", ok)

    key, length, value = dispstate.decode_setting(
        setting_bytes(dispstate.GUID_CONSOLE_DISPLAY_STATE, 0, length=0))
    check("DataLength=0 时值算不出来 → None", value is None, value)


# ---------------------------------------------------------------- 关屏核对
def test_monitor_off():
    sent = []

    def fake_send():
        sent.append(1)
        return True

    ok, why = dispstate.monitor_off(send=fake_send, read=lambda t: dispstate.DISPLAY_OFF)
    check("一关就成 → True", ok is True, (ok, why))
    check("一关就成 → 只发一次", len(sent) == 1, len(sent))
    check("说明里有「第 1 次」", "第 1 次" in why, why)

    sent.clear()
    states = [dispstate.DISPLAY_ON, dispstate.DISPLAY_ON, dispstate.DISPLAY_OFF]

    def fake_read(_timeout):
        return states.pop(0)

    ok, why = dispstate.monitor_off(send=fake_send, read=fake_read)
    check("前两次没关上、第三次关上 → True", ok is True, (ok, why))
    check("重试了三次", len(sent) == 3, len(sent))

    sent.clear()
    ok, why = dispstate.monitor_off(send=fake_send, read=lambda t: dispstate.DISPLAY_ON)
    check("一直关不上 → False", ok is False, (ok, why))
    check("重试次数用满（默认 3）", len(sent) == 3, len(sent))
    check("说明里说「还是开」", "还是开" in why, why)

    sent.clear()
    ok, why = dispstate.monitor_off(attempts=1, send=fake_send,
                                    read=lambda t: dispstate.DISPLAY_ON)
    check("attempts=1 就只试一次", len(sent) == 1, len(sent))
    check("attempts=1 也是 False", ok is False)

    ok, why = dispstate.monitor_off(send=fake_send, read=lambda t: None)
    check("读不到状态 → None（不敢说关上了）", ok is None, (ok, why))
    check("说明里说「无法确认」", "无法确认" in why, why)

    called = []

    ok, why = dispstate.monitor_off(send=lambda: False, read=lambda t: called.append(1))
    check("指令没发出去 → False", ok is False, (ok, why))
    check("没发出去就不读状态", called == [], called)
    check("说明里说「没发出去」", "没发出去" in why, why)


def test_send_guards():
    """send_off/send_on 在异常时不能炸，必须返回 False。"""
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("没有 user32")

    saved = dispstate.user32
    try:
        dispstate.user32 = Boom()
        check("send_off 出错返回 False", dispstate.send_off() is False)
        check("send_on 出错返回 False", dispstate.send_on() is False)
    finally:
        dispstate.user32 = saved


def main():
    print("== 常量 ==")
    test_constants()
    print("== 状态文字 ==")
    test_state_text()
    print("== 解广播字节 ==")
    test_decode()
    print("== 关屏核对 ==")
    test_monitor_off()
    print("== 发送兜底 ==")
    test_send_guards()
    print(f"\n{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
