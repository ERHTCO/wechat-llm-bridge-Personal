"""evaluate() 的表格驱动测试：闸门 + 触发，纯函数，零 IO。

    .venv\\Scripts\\python.exe tests\\test_evaluate.py

以前要验这些，得先 patch proactive.ask 和 proactive.send_to，再造一个临时
目录塞 chat.db。现在 evaluate 只吃事实字典，一行用例断言一个场景。
"""
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import proactive  # noqa: E402

PASS = 0
FAIL = 0
NOW = datetime(2026, 10, 26, 15, 0)
DAY = "2026-10-26"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def base_g(**over):
    """一份"什么都允许"的上下文，用例只覆盖自己关心的字段。"""
    g = {
        "target": "wxid_alice", "trigger": "", "now": NOW, "now_ts": NOW.timestamp(),
        "day": DAY, "msgs": [("them", "在吗", NOW.timestamp() - 8 * 3600)],
        "last_any": NOW.timestamp() - 8 * 3600,
        "last_role": "them", "last_text": "在吗",
        "last_them": NOW.timestamp() - 8 * 3600, "last_me": 0.0,
        "quiet_min": 480.0, "awaiting": False,
        "sent_today": 0, "daily_max": 2, "last_speak_ts": 0.0,
        "last_speak_text": "", "silent_streak": 0, "last_silent_ts": 0.0,
        "last_silent_reason": "", "last_ask_ts": 0.0, "kinds": [],
        "videos": [], "hint": "", "persona": "", "me_name": "",
    }
    g.update(over)
    return g


def pro(**over):
    conf = dict(proactive.DEFAULTS)
    conf.update({
        "enabled": True, "targets": ["wxid_alice"], "window": ["09:00", "23:00"],
        "daily_max": 2, "min_gap_min": 30, "quiet_min": 20,
        "heartbeat_min": 0, "silent_backoff_min": 0,
        "silent_backoff_max_min": 0, "daily_at": "", "idle_hours": 6,
    })
    conf.update(over)
    return conf


def case(name, want_stage, want_trigger, *, g=None, state=None, dstate=None,
         p=None, force=False):
    got = proactive.evaluate(p or pro(), g or base_g(), state or {}, dstate or {},
                             NOW, force=force)
    ok = got.stage == want_stage and (want_stage == "skip" or got.trigger == want_trigger)
    check(f"{name} → {want_stage}{'/' + want_trigger if want_trigger else ''}",
          ok, got)


def main():
    print("\n[1] 闸门：不满足就不问")
    # 额度是从 state 现算的（不信任 g 里缓存的 sent_today）——这正是想要的
    case("今天已达上限", "skip", "",
         state={DAY: {"wxid_alice": {"count": 2, "kinds": [], "last_auto": 0}}})
    case("距上次主动不足 30 分钟", "skip", "",
         state={DAY: {"wxid_alice": {"count": 1, "kinds": [],
                            "last_auto": NOW.timestamp() - 10 * 60}}})
    case("刚聊过（安静 5 分钟）", "skip", "",
         g=base_g(quiet_min=5))
    case("判断过不说还在冷却", "skip", "",
         dstate={"wxid_alice": {"silent_streak": 1,
                        "last_ask_ts": NOW.timestamp() - 10 * 60}},
         p=pro(silent_backoff_min=45))
    case("force 跳过额度/冷却/间隔", "ask", "idle",
         state={DAY: {"wxid_alice": {"count": 99, "kinds": [],
                             "last_auto": NOW.timestamp()}}},
         dstate={"wxid_alice": {"silent_streak": 3,
                        "last_ask_ts": NOW.timestamp() - 60}},
         p=pro(silent_backoff_min=45), force=True)

    print("\n[2] 触发源：为什么这时候问")
    case("到点 daily 优先", "ask", "daily",
         p=pro(daily_at="14:00"))
    case("静默超时且对方最后说话 → idle", "ask", "idle")
    case("静默超时但自己最后说话 → awaiting", "ask", "awaiting",
         g=base_g(last_role="me", awaiting=True,
                  last_me=NOW.timestamp() - 8 * 3600, last_them=0.0))
    case("刚聊完但心跳到了 → heartbeat", "ask", "heartbeat",
         g=base_g(quiet_min=30), p=pro(heartbeat_min=90))
    case("什么都不满足 → skip", "skip", "",
         g=base_g(quiet_min=5, msgs=[("them", "嗯", NOW.timestamp())],
                  last_any=NOW.timestamp() - 300),
         p=pro(quiet_min=0, idle_hours=0, heartbeat_min=0))

    print("\n[3] 边界")
    case("没有触发器时 reason 说得清", "skip", "",
         g=base_g(quiet_min=5), p=pro(quiet_min=0, idle_hours=0, heartbeat_min=0))
    d = proactive.evaluate(pro(quiet_min=0, idle_hours=0, heartbeat_min=0),
                           base_g(quiet_min=5), {}, {}, NOW)
    check("skip 的 reason 非空", bool(d.reason), d)

    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
