"""发送出口：所有「发一条消息给某个人」都从这里走。

以前有三处各自实现「发送 → 记账 → 写 self_sent」：

    proactive.decide_one()   判断版主动搭话
    proactive.fire()         手动 / timer 模式
    remind._default_send()   到点提醒

第三处还特意跳过了每日额度——这个差异只活在代码里，没有任何地方写明。

现在收口成一个 module，对外只有一个动作：

    deliver(target, text, kind, ...) -> (ok, how)

不变量（以前只写在 record_self_sent 的注释里，现在住在调用链里）：
**发出去的文本必须写进 data/self_sent.jsonl**，否则 watcher 会把机器人
自己发的话当成对方说的，再回一遍（自己回自己）。这条 bug 真发生过。
"""
import json
import os as _os
import time
from datetime import datetime
from pathlib import Path

_HERE = Path(_os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()

# 占每日额度的 kind。unknown / manual / reminder 都不占。
AUTO_KINDS = frozenset({
    "daily", "idle", "awaiting", "heartbeat",      # 判断模式
    "timer-daily", "timer-idle",                   # 老闹钟模式
})
NOT_COUNTED = frozenset({"manual", "reminder"})


def _root(root):
    return Path(root) if root else _HERE


def _state_file(root):
    return _root(root) / "data" / "proactive_state.json"


def self_sent_file(root=None):
    return _root(root) / "data" / "self_sent.jsonl"


# ---------------------------------------------------------------- 每日额度

def load_state(root=None):
    p = _state_file(root)
    if not p.exists():
        return {}
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        # 静默归零会让今天已经发过的条数变成 0（额度失效），至少要留一行证据
        print(f"[outbox] 额度台账读不动（按空处理）：{type(exc).__name__}: {exc}",
              flush=True)
        return {}
    if not isinstance(state, dict):
        print(f"[outbox] 额度台账形状不对（{type(state).__name__}），按空处理", flush=True)
        return {}
    # 老版本把 kinds 存成了字符串，统一成列表，避免判断出错
    for day in state.values():
        if not isinstance(day, dict):
            continue
        for rec in day.values():
            if isinstance(rec, dict):
                kinds = rec.get("kinds")
                if isinstance(kinds, str):
                    rec["kinds"] = [kinds] if kinds else []
                elif not isinstance(kinds, list):
                    rec["kinds"] = []
    return state


def save_state(state, root=None):
    """原子写：先写临时文件再替换。

    以前是直接 write_text，CLI 和桥进程同时写的时候可能留下截断的 JSON，
    下次启动 load_state 就静默返回空，额度直接从零开始算。
    """
    p = _state_file(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    _os.replace(tmp, p)


def mark_sent(state, target, kind, now=None):
    """记一次发送。手动发送不占自动发送的每日额度。"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    day = state.setdefault(today, {})
    rec = day.setdefault(target, {"count": 0, "kinds": [], "last_auto": 0, "last_manual": 0})
    kinds = rec.get("kinds") or []
    if kind not in kinds:
        kinds.append(kind)
    rec["kinds"] = kinds
    if kind == "manual":
        rec["last_manual"] = now.timestamp()
    else:
        rec["count"] = int(rec.get("count", 0)) + 1
        rec["last_auto"] = now.timestamp()


def sent_today(state, target, day=None):
    """今天已经自动发过几条（手动的不算）。"""
    day = day or datetime.now().strftime("%Y-%m-%d")
    return int(((state.get(day) or {}).get(target) or {}).get("count", 0))


# ---------------------------------------------------------------- self_sent 台账

def record_self_sent(target, text, root=None):
    """把我们主动发出去的文本记下来，供 watcher 识别"这是自己发的"。

    写失败不能静默：这份台账是 watcher 判断己方的**唯一**凭据，丢了就会
    自己回自己（模块开头那条不变量）。这里仍然不抛（消息已经发出去了，
    抛出去只会让调用方以为没发），但必须在 bridge.log 留一行。
    """
    try:
        p = self_sent_file(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "target": target, "text": text},
                               ensure_ascii=False) + "\n")
        return True
    except Exception as exc:
        print(f"[outbox] !! self_sent 台账写失败（watcher 可能把自己发的话当对方说的）："
              f"{type(exc).__name__}: {exc}", flush=True)
        return False


# ---------------------------------------------------------------- 发送

def _default_transport(target, text, guard=None):
    """真正发到微信。延迟导入 proactive：它持有 wxuia 那层窗口逻辑。

    这里故意在调用时才 `import proactive` 并取属性，这样测试里
    `proactive.send_to = fake` 依然能拦住——不破坏已有的测试手法。
    """
    import proactive
    return proactive.send_to(target, text, guard=guard)


def deliver(target, text, kind, *, root=None, transport=None, now=None, guard=None):
    """唯一的发送出口。返回 (ok, how)。

    transport(target, text, guard=...) -> (ok, how)，默认走微信。
    成功时：一定写 self_sent；只有 kind 在 AUTO_KINDS 里才记每日额度。
    """
    if kind not in AUTO_KINDS and kind not in NOT_COUNTED:
        print(f"[outbox] 未知 kind={kind!r}，不记每日额度", flush=True)

    fn = transport or _default_transport
    try:
        ok, how = fn(target, text, guard=guard)
    except Exception as exc:      # transport 自己炸了，也不能炸掉调用方
        return False, f"{type(exc).__name__}: {exc}"
    if not ok:
        return False, str(how)

    record_self_sent(target, text, root)
    if kind in AUTO_KINDS:
        state = load_state(root)
        mark_sent(state, target, kind, now=now)
        save_state(state, root)
    return True, str(how)
