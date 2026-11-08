"""主动搭话判断层的回归测试：不连微信、不调真模型。

    .venv\\Scripts\\python.exe tests\\test_decide.py

覆盖：JSON 解析容错、时段窗、相对时间、四道闸门、以及
「判断 → 发送 / 沉默 / 判断期间来了新消息」这几种走法。
"""
import json
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import proactive  # noqa: E402

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


# ---------------------------------------------------------------- 造环境

BASE = datetime(2026, 10, 25, 15, 0)


def make_env(messages, cfg_extra=None, name="wxid_alice"):
    tmp = Path(tempfile.mkdtemp(prefix="wxdecide-"))
    (tmp / "data").mkdir()
    conn = sqlite3.connect(tmp / "data" / "chat.db")
    conn.execute(
        "create table messages(id integer primary key autoincrement, wxid text,"
        " role text, text text, ts real, msg_key text)"
    )
    for role, text, ts in messages:
        conn.execute(
            "insert into messages(wxid, role, text, ts) values(?,?,?,?)",
            (name, role, text, ts.timestamp() if hasattr(ts, "timestamp") else ts),
        )
    conn.commit()
    conn.close()
    cfg = {
        "my_name": "落星河",
        "persona": "含蓄，温柔",
        "backend": "stub",
        "proactive": dict(proactive.DEFAULTS, enabled=True, targets=[name],
                          daily_at="", idle_hours=6),
    }
    if cfg_extra:
        cfg["proactive"].update(cfg_extra)
    return tmp, cfg


def silent_ask(reason="没由头"):
    def _ask(cfg, root, prompt):
        return json.dumps({"action": "silent", "reason": reason}, ensure_ascii=False), "fake", 1
    return _ask


def speak_ask(text="在忙吗", reason="隔了三小时"):
    def _ask(cfg, root, prompt):
        return json.dumps({"action": "speak", "reason": reason, "text": text},
                          ensure_ascii=False), "fake", 1
    return _ask


# ---------------------------------------------------------------- 1. 解析

def test_parse():
    print("\n[1] 模型输出的解析容错")
    p = proactive.parse_decision

    a, r, t = p('```json\n{"action":"speak","reason":"隔了三小时","text":"在忙吗"}\n```')
    check("代码块包裹能解析", (a, t) == ("speak", "在忙吗"), (a, r, t))

    a, r, t = p('好的，我判断一下：{"action":"silent","reason":"刚聊完"}')
    check("前面有废话也能解析", a == "silent" and r == "刚聊完", (a, r, t))

    a, _, _ = p("")
    check("空输出当沉默", a == "silent")

    a, r, _ = p("我觉得现在不太合适")
    check("不是 JSON 当沉默", a == "silent" and "JSON" in r, r)

    a, _, _ = p('{"action":"speak","reason":"想说","text":"   "}')
    check("要开口但没内容 → 沉默", a == "silent")

    a, _, _ = p('{"action":"SILENT","reason":"夜深了"}')
    check("大写也能认", a == "silent")

    a, _, t = p('{"action":"speak","text":"“带引号的”"}')
    check("去掉包裹引号", t == "带引号的", t)

    a, _, _ = p('{"action":"哈哈"}')
    check("看不懂的 action 当沉默", a == "silent")

    a, _, t = p('{"action":"speak","text":"第一行\\n第二行\\n第三行\\n第四行"}')
    check("最多三行", len(t.splitlines()) == 3, repr(t))


# ---------------------------------------------------------------- 2. 时段窗

def test_window():
    print("\n[2] 允许时段")
    def at(h, m=0):
        return datetime(2026, 10, 25, h, m)
    w = ["09:00", "23:00"]
    check("09-23 中午在内", proactive.in_window(at(12), w))
    check("09-23 早上在外", not proactive.in_window(at(7), w))
    check("09-23 23:30 在外", not proactive.in_window(at(23, 30), w))
    w2 = ["22:00", "02:00"]
    check("跨零点 23:30 在内", proactive.in_window(at(23, 30), w2))
    check("跨零点 01:00 在内", proactive.in_window(at(1), w2))
    check("跨零点 12:00 在外", not proactive.in_window(at(12), w2))


# ---------------------------------------------------------------- 3. 相对时间

def test_ago():
    print("\n[3] 相对时间说法")
    now = BASE.timestamp()
    check("刚刚", proactive.ago(now - 30, now) == "刚刚", proactive.ago(now - 30, now))
    check("分钟", proactive.ago(now - 300, now) == "5 分钟前", proactive.ago(now - 300, now))
    check("小时", proactive.ago(now - 3 * 3600, now) == "3 小时前", proactive.ago(now - 3 * 3600, now))
    check("天", proactive.ago(now - 2.5 * 86400, now) == "2.5 天前", proactive.ago(now - 2.5 * 86400, now))


# ---------------------------------------------------------------- 4. 闸门

def test_gates():
    print("\n[4] 闸门（模型说了不算）")
    now = BASE
    pro = proactive.pro_conf({"proactive": dict(proactive.DEFAULTS, quiet_min=20,
                                                min_gap_min=30, daily_max=2)})
    root, cfg = make_env([("me", "晚安", BASE - timedelta(minutes=2))])
    g = proactive.gather(cfg, root, "wxid_alice", "idle", now=now)
    check("刚聊过会被拦", "刚聊过" in (proactive.check_gates(pro, g, {}, {}, now) or ""),
          proactive.check_gates(pro, g, {}, {}, now))

    root, cfg = make_env([("me", "晚安", BASE - timedelta(hours=3))])
    g = proactive.gather(cfg, root, "wxid_alice", "idle", now=now)
    check("聊完 3 小时放行", proactive.check_gates(pro, g, {}, {}, now) is None)

    state = {"2026-10-25": {"wxid_alice": {"count": 2, "kinds": [], "last_auto": 0}}}
    check("到每日上限被拦", "上限" in (proactive.check_gates(pro, g, state, {}, now) or ""))

    state = {"2026-10-25": {"wxid_alice": {"count": 1, "kinds": [], "last_auto": (now - timedelta(minutes=10)).timestamp()}}}
    check("距上次不足 30 分钟被拦", "30 分钟" in (proactive.check_gates(pro, g, state, {}, now) or ""))

    dstate = {"wxid_alice": {"silent_streak": 1, "last_ask_ts": (now - timedelta(minutes=10)).timestamp()}}
    check("判断过不说要冷却", "冷却" in (proactive.check_gates(pro, g, {}, dstate, now) or ""))

    dstate = {"wxid_alice": {"silent_streak": 3, "last_ask_ts": (now - timedelta(minutes=120)).timestamp()}}
    check("退避翻倍后仍未到（45*4=180 分）", "冷却" in (proactive.check_gates(pro, g, {}, dstate, now) or ""))

    check("--force 能强行放行", proactive.check_gates(pro, g, {}, dstate, now, force=True) is None)


# ---------------------------------------------------------------- 5. 触发

def test_trigger():
    print("\n[5] 触发源")
    now = BASE
    pro = proactive.pro_conf({"proactive": dict(proactive.DEFAULTS, daily_at="14:00",
                                                idle_hours=6, heartbeat_min=90)})
    # 静默 7 小时，最后一条是我说的 → awaiting
    root, cfg = make_env([("them", "有空吗", BASE - timedelta(hours=8)),
                          ("me", "有", BASE - timedelta(hours=7))])
    st, dst = {}, {}
    g = proactive.gather(cfg, root, "wxid_alice", "", now=now, state=st, dstate=dst)
    trig, desc = proactive.pick_trigger(pro, g, st, dst, now)
    check("到点优先 daily", trig == "daily", (trig, desc))

    pro2 = proactive.pro_conf({"proactive": dict(proactive.DEFAULTS, daily_at="",
                                                 idle_hours=6, heartbeat_min=90)})
    trig, desc = proactive.pick_trigger(pro2, g, st, dst, now)
    check("我最后一条没回 → awaiting", trig == "awaiting", (trig, desc))

    # 对方最后一条 → idle
    root2, cfg2 = make_env([("me", "早", BASE - timedelta(hours=9)),
                            ("them", "早呀", BASE - timedelta(hours=8))])
    g2 = proactive.gather(cfg2, root2, "wxid_alice", "", now=now, state={}, dstate={})
    trig, desc = proactive.pick_trigger(pro2, g2, {}, {}, now)
    check("最后一条是对方 → idle", trig == "idle", (trig, desc))

    # 刚判断过 → heartbeat 未到
    dst = {"wxid_alice": {"last_ask_ts": (now - timedelta(minutes=10)).timestamp()}}
    g3 = proactive.gather(cfg2, root2, "wxid_alice", "", now=now, state={}, dstate=dst)
    g3["quiet_min"] = 1        # 假装刚聊过，压制 idle
    trig, desc = proactive.pick_trigger(pro2, g3, {}, dst, now)
    check("心跳没到就不问", trig is None, (trig, desc))


# ---------------------------------------------------------------- 6. 整条链路

def test_flow():
    print("\n[6] 判断 → 动作")
    now = BASE
    msgs = [("them", "今天好累", BASE - timedelta(hours=7)),
            ("me", "早点睡", BASE - timedelta(hours=7, minutes=-1))]

    # 6.1 判断为不说：不发、只记日志
    root, cfg = make_env(msgs)
    calls = []
    orig_send = proactive.send_to
    proactive.send_to = lambda t, x, guard=None: calls.append(x) or (True, "ok")
    proactive.ask = silent_ask("对方可能在忙")
    try:
        r = proactive.decide_one(cfg, root, "wxid_alice", now=now)
        check("沉默：stage=silent", r["stage"] == "silent", r)
        check("沉默：没有调用发送", calls == [], calls)
        rows = proactive.load_decisions(root, 10)
        check("沉默：写进了日志", rows and rows[-1]["action"] == "silent", rows[-1:] or None)
        dst = proactive.load_dstate(root)
        check("沉默：退避计数 +1", dst["wxid_alice"]["silent_streak"] == 1, dst)

        # 6.2 紧接着再问一次：应被退避挡住，且不再调模型
        asked = []
        proactive.ask = lambda *a: asked.append(1) or silent_ask()(*a)
        r2 = proactive.decide_one(cfg, root, "wxid_alice", now=now + timedelta(minutes=5))
        check("退避中不重复问模型", r2["stage"] == "skip" and not asked, (r2["stage"], asked))

        # 6.3 判断为开口（dry-run）：不发
        proactive.ask = speak_ask("在忙吗")
        r3 = proactive.decide_one(cfg, root, "wxid_alice", now=now + timedelta(hours=2),
                                  force=True, dry=True)
        check("dry-run：stage=speak", r3["stage"] == "speak", r3)
        check("dry-run：真的没发", calls == [], calls)

        # 6.4 真发：状态、日志、self_sent 都要落
        r4 = proactive.decide_one(cfg, root, "wxid_alice", now=now + timedelta(hours=2), force=True)
        check("真发：stage=speak", r4["stage"] == "speak" and r4.get("sent"), r4)
        check("真发：发送内容正确", calls == ["在忙吗"], calls)
        st = proactive.load_state(root)
        check("真发：每日计数 +1",
              proactive.sent_today(st, "wxid_alice", now.strftime("%Y-%m-%d")) == 1, st)
        selfsent = (root / "data" / "self_sent.jsonl").read_text(encoding="utf-8")
        check("真发：self_sent 有记录（watcher 才不会自己回自己）", "在忙吗" in selfsent, selfsent)
        rows = proactive.load_decisions(root, 50)
        check("真发：日志 sent=true", any(r["action"] == "speak" and r["sent"] for r in rows))

        # 6.5 判断期间来了新消息 → 作废
        def ask_then_new_message(cfg_, root_, prompt):
            conn = sqlite3.connect(Path(root_) / "data" / "chat.db")
            conn.execute("insert into messages(wxid,role,text,ts) values(?,?,?,?)",
                         ("wxid_alice", "them", "我刚回来", time.time() + 5))
            conn.commit()
            conn.close()
            return speak_ask("你回来了？")(cfg_, root_, prompt)

        proactive.ask = ask_then_new_message
        calls.clear()
        r5 = proactive.decide_one(cfg, root, "wxid_alice", now=now + timedelta(hours=5), force=True)
        check("判断期间来新消息 → stale", r5["stage"] == "stale", r5)
        check("stale 不发", calls == [], calls)
    finally:
        proactive.send_to = orig_send


# ---------------------------------------------------------------- 7. 提示词

def test_prompt():
    print("\n[7] 判断提示词里该有的信息")
    root, cfg = make_env([("them", "周末去爬山吗", BASE - timedelta(hours=9)),
                          ("me", "看天气", BASE - timedelta(hours=8))])
    now = BASE
    g = proactive.gather(cfg, root, "wxid_alice", "awaiting", now=now)
    p = proactive.render_prompt(g)
    for needle, why in [
        ("落星河", "名字"),
        ("含蓄", "口吻"),
        ("周末去爬山吗", "最近对话"),
        ("对方一直没回", "对方回没回"),
        ("上限 2 次", "今天发过几条"),
        ("silent", "给静默出口"),
        ("该闭嘴的情况", "闭嘴的锚点"),
        ("不要编", "别编事实"),
    ]:
        check(f"提示词含「{why}」", needle in p, needle)


def main():
    test_parse()
    test_window()
    test_ago()
    test_gates()
    test_trigger()
    test_flow()
    test_prompt()
    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
