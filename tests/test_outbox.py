"""Outbox 的回归测试：唯一发送出口的三条规则 + 一条不变量。

    .venv\\Scripts\\python.exe tests\\test_outbox.py

不变量：发出去的文本一定写进 self_sent（否则 watcher 会自己回自己）。
规则：只有 AUTO_KINDS 占每日额度；manual / reminder 不占。
"""
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import outbox  # noqa: E402

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


def env():
    tmp = Path(tempfile.mkdtemp(prefix="wxoutbox-"))
    (tmp / "data").mkdir()
    return tmp


def ok_sender(target, text, guard=None):
    return True, "ok"


def fail_sender(target, text, guard=None):
    return False, "没找到窗口"


def boom_sender(target, text, guard=None):
    raise RuntimeError("agent 断了")


def self_sent_lines(root):
    p = root / "data" / "self_sent.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


NOW = datetime(2026, 10, 26, 14, 0)
DAY = "2026-10-26"


def test_auto_kind():
    print("\n[1] 自动类 kind：写 self_sent + 占额度")
    root = env()
    ok, how = outbox.deliver("wxid_alice", "在忙吗", "heartbeat", root=root,
                             transport=ok_sender, now=NOW)
    check("返回成功", ok and how == "ok", (ok, how))
    lines = self_sent_lines(root)
    check("self_sent 有一行", len(lines) == 1, lines)
    check("self_sent 字段齐全", all(k in lines[0] for k in ("ts", "target", "text")), lines[0])
    check("文本正确", lines[0]["text"] == "在忙吗", lines[0])
    state = outbox.load_state(root)
    check("额度 +1", outbox.sent_today(state, "wxid_alice", DAY) == 1, state)
    check("kinds 记了 trigger", "heartbeat" in (state[DAY]["wxid_alice"]["kinds"]), state)

    outbox.deliver("wxid_alice", "第二句", "idle", root=root, transport=ok_sender, now=NOW)
    state = outbox.load_state(root)
    check("再发一条额度到 2", outbox.sent_today(state, "wxid_alice", DAY) == 2, state)


def test_not_counted():
    print("\n[2] manual / reminder：写 self_sent，不占额度")
    for kind in ("manual", "reminder"):
        root = env()
        outbox.deliver("wxid_alice", f"({kind})", kind, root=root, transport=ok_sender, now=NOW)
        state = outbox.load_state(root)
        check(f"{kind} 写了 self_sent", len(self_sent_lines(root)) == 1)
        check(f"{kind} 不占额度", outbox.sent_today(state, "wxid_alice", DAY) == 0, state)


def test_failures():
    print("\n[3] 发失败 / 抛异常：什么都不写")
    root = env()
    ok, how = outbox.deliver("wxid_alice", "发不出去", "heartbeat", root=root,
                             transport=fail_sender, now=NOW)
    check("返回失败", not ok and "窗口" in how, (ok, how))
    check("没写 self_sent", self_sent_lines(root) == [])
    check("没占额度", outbox.sent_today(outbox.load_state(root), "wxid_alice", DAY) == 0)

    ok, how = outbox.deliver("wxid_alice", "炸了", "heartbeat", root=root,
                             transport=boom_sender, now=NOW)
    check("异常被吃掉并返回失败", not ok and "RuntimeError" in how, (ok, how))
    check("异常也不写 self_sent", self_sent_lines(root) == [])


def test_unknown_kind():
    print("\n[4] 未知 kind：不占额度（宁可少记，不吃用户的每日预算）")
    root = env()
    ok, _ = outbox.deliver("wxid_alice", "谁知道这是什么", "whatever", root=root,
                           transport=ok_sender, now=NOW)
    check("仍会发出去", ok)
    check("写了 self_sent", len(self_sent_lines(root)) == 1)
    check("不占额度", outbox.sent_today(outbox.load_state(root), "wxid_alice", DAY) == 0)


def test_atomic_state():
    print("\n[5] 状态文件原子写：连续发几条之后仍是合法 JSON")
    root = env()
    for i in range(5):
        outbox.deliver("wxid_alice", f"第{i}条", "heartbeat", root=root,
                       transport=ok_sender, now=NOW)
    p = root / "data" / "proactive_state.json"
    check("状态文件存在", p.exists())
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        valid = True
    except Exception as exc:
        data, valid = None, False
        print("    ", exc)
    check("能解析成 JSON", valid)
    check("额度累计到 5", outbox.sent_today(data or {}, "wxid_alice", DAY) == 5, data)
    check("没有残留 .tmp", not list((root / "data").glob("*.tmp")))


def test_watcher_contract():
    print("\n[6] 和 watcher 的约定：self_sent 的格式没变")
    root = env()
    outbox.deliver("wxid_alice", "我说的这句话", "heartbeat", root=root,
                   transport=ok_sender, now=NOW)
    # 复刻 watch_uia.external_sent_map 的解析方式
    items = {}
    for line in (root / "data" / "self_sent.jsonl").read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("text"):
            items[rec["text"]] = float(rec.get("ts") or 0)
    check("watcher 能按文本认出这条", items.get("我说的这句话", 0) > 0, items)


def test_legacy_names():
    print("\n[7] 老名字仍然可用（bridge / CLI / 旧测试都还指着它们）")
    import proactive
    for name in ("load_state", "save_state", "mark_sent", "sent_today",
                 "record_self_sent", "self_sent_file", "send_to"):
        check(f"proactive.{name} 存在", hasattr(proactive, name))
    check("proactive 用的是同一个 load_state", proactive.load_state is outbox.load_state)


def main():
    test_auto_kind()
    test_not_counted()
    test_failures()
    test_unknown_kind()
    test_atomic_state()
    test_watcher_contract()
    test_legacy_names()
    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
