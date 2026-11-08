"""第二轮审查修掉的那几条静默失败的回归用例。

    .venv\\Scripts\\python.exe tests\\test_review_fixes.py

只测纯函数和注入进来的假实现：不连微信、不调模型、不碰真 data/。
"""
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import backends  # noqa: E402
import bili_fav  # noqa: E402
import outbox  # noqa: E402
import remind  # noqa: E402
import watch_uia  # noqa: E402

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


# ---------------------------------------------------------------- 颜文字
def test_kaomoji():
    check("算式里的 × 不能被删", backends.strip_kaomoji("27×3=81") == "27×3=81",
          repr(backends.strip_kaomoji("27×3=81")))
    check("区间 ～ 不能被删", backends.strip_kaomoji("大概3～5天") == "大概3～5天",
          repr(backends.strip_kaomoji("大概3～5天")))
    check("幂次 ^ 不能被删", backends.strip_kaomoji("2^10=1024") == "2^10=1024",
          repr(backends.strip_kaomoji("2^10=1024")))
    check("正文里的「益」不能被删", backends.strip_kaomoji("收益不错") == "收益不错",
          repr(backends.strip_kaomoji("收益不错")))
    check("真颜文字还是会被清掉",
          "ᗜ" not in backends.strip_kaomoji("（ᗜᴗᗜ）好耶") and "好耶" in backends.strip_kaomoji("（ᗜᴗᗜ）好耶"),
          repr(backends.strip_kaomoji("（ᗜᴗᗜ）好耶")))
    check("空串照旧", backends.strip_kaomoji("") == "")


# ---------------------------------------------------------------- outbox
def test_outbox():
    tmp = Path(tempfile.mkdtemp())
    p = tmp / "data" / "proactive_state.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")   # 形状不对（列表）
    check("台账是列表也不炸，按空处理", outbox.load_state(root=tmp) == {})
    p.write_text("{坏掉的 json", encoding="utf-8")
    check("台账坏了按空处理", outbox.load_state(root=tmp) == {})

    blocker = tmp / "blocked"
    blocker.write_text("x", encoding="utf-8")
    check("self_sent 写失败返回 False（不抛）",
          outbox.record_self_sent("wxid_alice", "测试", root=blocker) is False)
    check("self_sent 正常时返回 True",
          outbox.record_self_sent("wxid_alice", "测试", root=tmp) is True)


# ---------------------------------------------------------------- watch 的重试判据
def test_watch_lists():
    check("找不到输入框 = 没发出去", watch_uia._not_sent("找不到输入框") is True)
    check("SetValue 失败 = 没发出去",
          watch_uia._not_sent("SetValue 失败: COMError: x") is True)
    check("输入框内容不符 = 没发出去",
          watch_uia._not_sent("输入框内容不符: 'abc'") is True)
    check("可能已发出（消息列表没出现）不能回滚游标",
          watch_uia._not_sent("{'in_box': 'x'} | 输入框清了但消息列表里没出现") is False)
    check("找不到输入框值得马上重试",
          watch_uia._worth_retry("找不到输入框") is True)
    check("可能已发出绝不能重试（否则对方收到两遍）",
          watch_uia._worth_retry("{'in_box': 'x'} | 输入框清了但消息列表里没出现") is False)
    check("不在前台仍然不重试", watch_uia._worth_retry("目标窗口不在前台，本次放弃") is False)


# ---------------------------------------------------------------- B站没登录
def test_bili_not_logged_in():
    saved = bili_fav.L.load_session
    bili_fav.L.load_session = lambda: {}
    try:
        try:
            bili_fav._op()
            ok = False
        except bili_fav.NotLoggedIn:
            ok = True
        except SystemExit:
            ok = False
        check("没登录时抛 NotLoggedIn（不是 SystemExit，后台线程能接住）", ok)
    finally:
        bili_fav.L.load_session = saved


# ---------------------------------------------------------------- remind 并发
def test_remind_race():
    root = Path(tempfile.mkdtemp())
    at = datetime.now() - timedelta(seconds=5)
    it = remind.add(root, "wxid_alice", at, "交作业")
    check("先站着一条到点的提醒", it is not None)

    # 发送过程中（模型写措辞这一步）插进来一条新提醒，
    # 模拟 HTTP 线程在 fire_due 跑着的时候 add()。
    def worder(cfg, rt, item):
        remind.add(rt, "wxid_alice", datetime.now() + timedelta(hours=2), "新的提醒")
        return "交作业啦"

    out = remind.fire_due({}, root=root, sender=lambda target, body: (True, "ok"),
                          worder=worder)
    items = remind.load(root)
    texts = {i.get("text"): i for i in items}
    check("到点的那条发出去了", any("交作业" in s for s in out), out)
    check("新加的提醒没被旧快照覆盖", "新的提醒" in texts, list(texts))
    check("发出去的那条标了 done=fired",
          texts.get("交作业", {}).get("done") == "fired", texts.get("交作业"))

    # 再跑一次不该重复发
    out2 = remind.fire_due({}, root=root, sender=lambda t, b: (True, "ok"),
                           worder=lambda c, r, i: "不该再发")
    check("同一轮跑第二遍不会重发", out2 == [], out2)


def test_config_guard():
    """坏 config.json 不能被静默覆盖：那会连 api_key/token 一起清掉。"""
    import os

    import gui
    import themes

    tmp = Path(tempfile.mkdtemp())
    cfg = tmp / "config.json"
    cfg.write_text('{"api_key": "sk-x", "api_token": "t", "ui_theme": "深色",',
                   encoding="utf-8")          # 尾逗号 = 坏文件
    before = cfg.read_bytes()
    saved = themes.CONFIG
    themes.CONFIG = cfg
    try:
        ok = themes.save("浅色")
        check("坏 config 时主题保存返回 False", ok is False, ok)
        check("坏 config 一个字节都没被改写", cfg.read_bytes() == before)
        check("坏 config 被认成 broken", gui.read_json_strict(cfg)[1] == "broken")
    finally:
        themes.CONFIG = saved

    cfg.write_text('{"api_key": "sk-x", "ui_theme": "深色"}', encoding="utf-8")
    themes.CONFIG = cfg
    try:
        check("好 config 能保存", themes.save("米白") is True)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        check("保存后其它键还在", data.get("api_key") == "sk-x", data)
        check("主题写进去了", data.get("ui_theme") == "米白", data)
    finally:
        themes.CONFIG = saved

    check("不认识的主题名不写文件", themes.save("不存在的主题") is False)

    missing = tmp / "nope.json"
    check("文件不存在时算 missing", gui.read_json_strict(missing)[1] == "missing")
    listed = tmp / "list.json"
    listed.write_text("[1,2]", encoding="utf-8")
    check("JSON 是数组也算 broken", gui.read_json_strict(listed)[1] == "broken")


def test_watch_lock():
    """锁里的人是死的才清；活的锁清掉 = 放行第二份监听。"""
    import gui

    tmp = Path(tempfile.mkdtemp())
    lock = tmp / ".watch.lock"

    check("没有锁文件 → 不是活锁", gui.lock_pid(lock) == (None, False))
    lock.write_text("不是数字", encoding="utf-8")
    check("垃圾内容 → pid=None", gui.lock_pid(lock)[0] is None)
    # 垃圾内容（含刚建好还没写 PID 的空文件）不删：删了会跟「子进程刚写锁」抢
    check("垃圾内容的锁不会被 GUI 清掉", gui.clear_dead_lock(lock) is False)
    check("垃圾锁文件还在", lock.exists())
    lock.unlink()

    lock.write_text("999999", encoding="utf-8")      # 基本不可能存在的 PID
    pid, alive = gui.lock_pid(lock)
    check("死 PID 判成不活", pid == 999999 and alive is False, (pid, alive))
    check("死 PID 的锁会被清掉", gui.clear_dead_lock(lock) is True)

    import os
    lock.write_text(str(os.getpid()), encoding="utf-8")
    pid, alive = gui.lock_pid(lock)
    check("自己的 PID 判成活", pid == os.getpid() and alive is True, (pid, alive))
    check("活锁不会被清掉", gui.clear_dead_lock(lock) is False)
    check("活锁文件还在", lock.exists())


def test_rate_wait():
    """限流改成「等一下」之后，判据本身要算对。"""
    import bridge

    check("间隔够了 → 不等", bridge.rate_wait(100.0, 105.0, 3) == 0.0)
    check("刚发过 1 秒、要求 3 秒 → 等 2 秒",
          abs(bridge.rate_wait(100.0, 101.0, 3) - 2.0) < 1e-6,
          bridge.rate_wait(100.0, 101.0, 3))
    check("等待封顶 3 秒", bridge.rate_wait(100.0, 100.0, 600) == 3.0)
    check("没配限流 → 不等", bridge.rate_wait(100.0, 100.0, 0) == 0.0)
    check("last 是 None 也不炸", bridge.rate_wait(None, 100.0, 3) == 0.0)
    check("垃圾输入不炸", bridge.rate_wait("x", "y", "z") == 0.0)


def main():
    print("== 颜文字 ==")
    test_kaomoji()
    print("== outbox ==")
    test_outbox()
    print("== 监听器重试判据 ==")
    test_watch_lists()
    print("== B站登录态 ==")
    test_bili_not_logged_in()
    print("== 提醒并发 ==")
    test_remind_race()
    print("== 配置被覆盖 ==")
    test_config_guard()
    print("== 监听锁 ==")
    test_watch_lock()
    print("== 发送限流 ==")
    test_rate_wait()
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
