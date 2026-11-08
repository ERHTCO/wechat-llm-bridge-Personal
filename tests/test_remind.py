"""提醒模块的回归测试：解析、存取、到点发送。不连微信、不调真模型。

    .venv\\Scripts\\python.exe tests\\test_remind.py
"""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import backends  # noqa: E402
import remind   # noqa: E402

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


def has_timetable():
    """有没有 data/timetable.json。

    「下课提醒我」那条用例依赖真实课表；刚克隆的仓库里没有 data/，
    这时候要跳过而不是判失败 —— 否则公开仓库一跑测试就是红的。
    """
    try:
        import schedule
        return bool(schedule.periods())
    except Exception:
        return False


def env():
    tmp = Path(tempfile.mkdtemp(prefix="wxremind-"))
    (tmp / "data").mkdir()
    return tmp


NOW = datetime(2026, 10, 26, 13, 45)      # 周一，正在第6节（13:30-14:10）


# ---------------------------------------------------------------- 1. 解析

def test_parse():
    print("\n[1] 解析：几点 / 提醒什么")
    cases = [
        # 句子, 期望 at, 期望内容, 期望 kind
        ("下课提醒我", datetime(2026, 10, 26, 14, 10), "下课了", "class_end"),
        ("12点05分提醒我交作业", datetime(2026, 10, 27, 12, 5), "交作业", "at"),
        ("下午3点半提醒我交作业", datetime(2026, 10, 26, 15, 30), "交作业", "at"),
        ("5分钟后提醒我关火", datetime(2026, 10, 26, 13, 50), "关火", "relative"),
        ("半个小时后提醒我看锅", datetime(2026, 10, 26, 14, 15), "看锅", "relative"),
        ("两小时后提醒我", datetime(2026, 10, 26, 15, 45), "到点了", "relative"),
        ("晚上10点提醒我泡脚", datetime(2026, 10, 26, 22, 0), "泡脚", "at"),
        ("今晚十点提醒我收衣服", datetime(2026, 10, 26, 22, 0), "收衣服", "at"),
        ("明天早上8点提醒我带身份证", datetime(2026, 10, 27, 8, 0), "带身份证", "at"),
        ("明早7点叫我起床", datetime(2026, 10, 27, 7, 0), "起床", "at"),
        ("九点提醒我开会", datetime(2026, 10, 27, 9, 0), "开会", "at"),
        ("14:20 提醒我拿外卖", datetime(2026, 10, 26, 14, 20), "拿外卖", "at"),
    ]
    for text, at, body, kind in cases:
        if kind == "class_end" and not has_timetable():
            check(f"{text} → 跳过（没有 data/timetable.json）", True)
            continue
        status, info = remind.parse(text, NOW)
        ok = (status == "ok" and info["at"] == at
              and info["text"] == body and info["kind"] == kind)
        check(f"{text} → {at:%m-%d %H:%M}「{body}」", ok,
              "" if ok else f"得到 {status} {info}")

    status, _ = remind.parse("我提醒过你了", NOW)
    check("「我提醒过你了」不算提醒请求", status is None)

    status, _ = remind.parse("今天天气不错", NOW)
    check("无关的话不算提醒", status is None)

    status, info = remind.parse("提醒我交作业", NOW)
    check("有意图没时间 → no_time", status == "no_time", (status, info))

    status, _ = remind.parse("第三节课下课提醒我", NOW)
    check("已经过去的下课时间 → no_time", status == "no_time")

    status, _ = remind.parse("十天后提醒我交报告", NOW)
    check("超过三天 → 不收", status == "no_time")


def test_review_round2():
    """第二轮审查修的两处：「晚上12点」算成中午、转述句被当成提醒请求。"""
    print("\n[1b] 晚上12点 / 转述句")
    late = datetime(2026, 11, 8, 22, 50)
    cases = [
        ("今晚12点提醒我交作业", datetime(2026, 11, 9, 0, 0), "交作业"),
        ("晚上十二点提醒我签退", datetime(2026, 11, 9, 0, 0), "签退"),
        ("明天晚上12点提醒我抢票", datetime(2026, 11, 10, 0, 0), "抢票"),
    ]
    for text, at, body in cases:
        status, info = remind.parse(text, late)
        ok = status == "ok" and info.get("at") == at
        check(f"{text} → {at:%m-%d %H:%M}", ok, "" if ok else f"得到 {status} {info}")

    status, info = remind.parse("中午12点提醒我吃饭", NOW)
    check("「中午12点」还得是 12:00", status == "ok" and info["at"].hour == 12,
          (status, info))

    for text in ("我妈叫我明天八点起床", "他喊我下午三点去打球", "老师叫我明早七点交作业"):
        status, info = remind.parse(text, NOW)
        check(f"转述句不算提醒：{text}", status is None, (status, info))

    status, _ = remind.parse("明早7点叫我起床", NOW)
    check("「叫我起床」这种正常请求还在", status == "ok", status)


# ---------------------------------------------------------------- 2. 存取

def test_store():
    print("\n[2] 存、去重、上限")
    root = env()
    # 必须用「今天」，不能用写死的日期：每日上限按 now() 的日期统计，
    # 写死 2026-10-26 的话，跨过零点再跑就永远数不到 10 条（这个坑真踩过）。
    at = datetime.now().replace(hour=18, minute=0, second=0, microsecond=0)
    a = remind.add(root, "wxid_alice", at, "吃饭")
    check("记下一条", a is not None and a["id"])
    check("读得回来", len(remind.load(root)) == 1)

    b = remind.add(root, "wxid_alice", at, "吃饭")
    check("同一分钟同一内容去重", b["id"] == a["id"] and len(remind.load(root)) == 1)

    c = remind.add(root, "wxid_alice", at, "喝水")
    check("内容不同就是两条", len(remind.load(root)) == 2)

    check("未到点的算 pending", len(remind.pending(root, "wxid_alice")) == 2)

    # 塞满今天的上限
    for i in range(remind.MAX_PER_DAY + 3):
        remind.add(root, "wxid_alice", at + timedelta(minutes=i + 1), f"事{i}")
    check("当天上限生效", len(remind.load(root)) <= remind.MAX_PER_DAY + 2,
          len(remind.load(root)))


# ---------------------------------------------------------------- 3. 到点

def test_fire():
    print("\n[3] 到点发送")
    root = env()
    sent = []

    def sender(target, body):
        sent.append((target, body))
        return True, "ok"

    def worder(cfg, r, item):
        return item["text"] + "啦。"

    # 时间基准必须是「相对现在」的，不能写死日期。
    # 原来写的是 datetime(2026, 10, 26, 12, 0)，而 remind.save() 会把
    # 「已完成且超过 7 天」的条目丢掉（防止文件无限长）—— 于是这个测试在
    # 写下的第 8 天开始自己挂：fired 的那条被裁掉，load()[0] 变成别人或者空。
    base = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=1)
    remind.add(root, "wxid_alice", base, "下课")
    future = remind.add(root, "wxid_alice", base + timedelta(hours=3), "晚点的事")

    out = remind.fire_due({}, root, now=base + timedelta(seconds=30),
                          sender=sender, worder=worder)
    check("到点发了一条", sent == [("wxid_alice", "下课啦。")], sent)
    check("标记成 fired", remind.load(root)[0]["done"] == "fired")
    check("日志有记录", any("下课啦" in x for x in out), out)

    sent.clear()
    remind.fire_due({}, root, now=base + timedelta(seconds=90),
                    sender=sender, worder=worder)
    check("发过的不重复发", sent == [], sent)

    # 晚了半小时以上：不补发
    root2 = env()
    remind.add(root2, "wxid_alice", base, "过期的事")
    out = remind.fire_due({}, root2, now=base + timedelta(hours=2),
                          sender=sender, worder=worder)
    check("超时标 missed", remind.load(root2)[0]["done"] == "missed", out)
    check("超时不发", not any("过期的事" in b for _t, b in sent))

    # 发失败：重试三次后放弃
    root3 = env()
    remind.add(root3, "wxid_alice", base, "发不出去")
    fail = lambda t, b: (False, "没找到窗口")   # noqa: E731
    for i in range(3):
        remind.fire_due({}, root3, now=base + timedelta(seconds=10 + i),
                        sender=fail, worder=worder)
    item = remind.load(root3)[0]
    check("失败三次后标 failed", item["done"] == "failed" and item["tries"] == 3, item)


# ---------------------------------------------------------------- 4. 说法

def test_text():
    print("\n[4] 时间怎么说给人听")
    now = datetime(2026, 10, 26, 13, 45)
    check("今天", remind.at_text(datetime(2026, 10, 26, 18, 5), now) == "今天 18:05")
    check("明天", remind.at_text(datetime(2026, 10, 27, 8, 0), now) == "明天 08:00")
    check("后天", remind.at_text(datetime(2026, 10, 28, 8, 0), now) == "后天 08:00")
    check("更远给日期",
          remind.at_text(datetime(2026, 11, 2, 8, 0), now) == "11月2日 08:00")


# ---------------------------------------------------------------- 5. 接进提示词

def test_prompt_wire():
    print("\n[5] 接进提示词")
    tpl = "【提醒】{{remind}}\n正文"
    ctx = backends.Context(wxid="wxid_alice", name="wxid_alice", text="下课提醒我", history=[],
                           persona="", template=tpl, remind_text="系统已经记下：今天 14:10")
    check("{{remind}} 被替换", "14:10" in ctx.prompt(), ctx.prompt())
    ctx2 = backends.Context(wxid="wxid_alice", name="wxid_alice", text="在吗", history=[],
                            persona="", template=tpl)
    check("没记下时有占位说明", "没有记下" in ctx2.prompt(), ctx2.prompt())

    prompt_path = HERE / "prompt.txt"
    if not prompt_path.exists():
        # 公开仓库里只有 prompt.example.txt；没有自己的提示词就跳过这两条
        check("prompt.txt 不在（跳过两条接线检查）", True)
        return
    real = prompt_path.read_text(encoding="utf-8")
    check("prompt.txt 里加了 {{remind}}", "{{remind}}" in real)
    check("prompt.txt 里加了「做不到的事不能应下来」", "做不到的事不能应下来" in real)


# ---------------------------------------------------------------- 6. 桥的入口

def test_bridge_hook():
    print("\n[6] 桥里的入口（记下 / 没听懂 / 群聊不记）")
    import bridge
    from datetime import datetime as _dt
    real_root = bridge.ROOT
    bridge.ROOT = env()
    # 固定到周一 13:45（第 6 节 13:30-14:10），否则「下课提醒」会随真实钟点飘
    fixed = _dt(2026, 10, 26, 13, 45)
    try:
        if has_timetable():
            note = bridge.handle_reminder("wxid_alice", "下课提醒我", {}, now=fixed)
            check("下课提醒 → 告诉模型已记下", "已经记下" in note and "14:10" in note, note)
            check("确实写进了文件", len(remind.load(bridge.ROOT)) == 1)
        else:
            # 没有 data/timetable.json 时算不出下课时间，这两条跳过
            check("下课提醒 跳过（没有 data/timetable.json）", True)
            check("确实写进了文件 跳过（同上）", True)

        note = bridge.handle_reminder("wxid_alice", "提醒我交作业", {}, now=fixed)
        check("没听懂时间 → 让模型去问", "没听懂" in note, note)

        note = bridge.handle_reminder("wxid_alice", "在吗", {}, now=fixed)
        check("普通消息不啰嗦", note == "", note)

        n_before = len(remind.load(bridge.ROOT))
        note = bridge.handle_reminder("wxid_alice", "10点提醒我开会", {"roomid": "123@chatroom"}, now=fixed)
        check("群聊不记提醒", note == "" and len(remind.load(bridge.ROOT)) == n_before, note)
    finally:
        bridge.ROOT = real_root


def main():
    test_parse()
    test_review_round2()
    test_store()
    test_fire()
    test_text()
    test_prompt_wire()
    test_bridge_hook()
    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
