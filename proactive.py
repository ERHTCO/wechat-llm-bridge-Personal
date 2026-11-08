"""主动搭话：心跳 + 模型判断（decision 模式）。

老版本是闹钟：到点就发，发什么由模板/模型即兴决定，为什么发没人知道。
新版本把「要不要开口」变成一次判断：

    心跳到 → 收集上下文 → 问模型 → 代码再兜一层闸门 → 才发

模型返回：
    {"action":"speak","reason":"…","text":"…"}
    {"action":"silent","reason":"…"}

silent 什么也不发，只写日志。日志在 data/decisions.jsonl，
跑几天看 log/stats，就知道该调阈值还是改提示词：
    python proactive.py --log 30
    python proactive.py --stats

触发源（为什么这时候问）：
    daily     到点（proactive.daily_at），每天一次
    idle      这段对话静默超过 idle_hours
    awaiting  你最后一条对方一直没回，也超过 idle_hours
    heartbeat 兜底心跳，heartbeat_min 分钟允许问一次，没理由也问

闸门（代码层，模型说了不算）：
    enabled / window / daily_max / min_gap_min / quiet_min / silent 退避
"""
import json
import os as _os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

_HERE = Path(_os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
sys.path.insert(0, str(_HERE))

import backends
import outbox
from outbox import (load_state, save_state, mark_sent, sent_today,
                    record_self_sent, self_sent_file)

# wxuia 会导入 uiautomation（只有 .venv 里装了），所以延迟导入：
# 桥本身不需要它（只有当真的去发消息时才用），这样用系统 Python 起桥也能活。


DEFAULTS = {
    "mode": "decision",        # decision=先判断；timer=老闹钟
    "enabled": False,
    "targets": [],
    "window": ["09:00", "23:00"],
    "daily_max": 2,
    "min_gap_min": 30,         # 两次主动之间至少隔多久
    "quiet_min": 20,           # 会话里刚有消息也算"正在聊"，不插嘴
    "heartbeat_min": 90,       # 两次"问模型"之间至少隔多久
    "silent_backoff_min": 45,  # 判断不说之后的冷却，之后翻倍
    "silent_backoff_max_min": 240,
    "decide_model": "deepseek-chat",
    "daily_at": "",
    "idle_hours": 6,
    "template": "",
    "hint": "",
}


def pro_conf(cfg):
    """取 proactive 配置，缺的字段补默认值（老配置也能跑）。"""
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (cfg.get("proactive") or {}).items() if v is not None})
    return out


def _root(root):
    return Path(root) if root else _HERE


def _decide_file(root):
    return _root(root) / "data" / "decide_state.json"


def decisions_file(root=None):
    return _root(root) / "data" / "decisions.jsonl"


# ---------------------------------------------------------------- 老状态（每日计数）
# load_state / save_state / mark_sent / sent_today 已经搬到 outbox.py：
# 额度和 self_sent 台账都归发送出口管。这里只保留 import 转发，对外名字不变。



# ---------------------------------------------------------------- 判断状态

def load_dstate(root=None):
    """判断层的状态：上次什么时候问的、连着几次不说、退避计数。

    单独一个文件，不去污染 proactive_state.json 的日期结构。
    """
    p = _decide_file(root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_dstate(d, root=None):
    p = _decide_file(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _drec(dstate, target):
    rec = dstate.get(target)
    if not isinstance(rec, dict):
        rec = {}
        dstate[target] = rec
    return rec


# ---------------------------------------------------------------- 时间/格式化

def parse_hhmm(s, default=None):
    try:
        h, m = str(s).strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return default


def in_window(now, window):
    """允许时段。支持跨零点：["22:00","02:00"]。"""
    if not window or len(window) < 2:
        return True
    a = parse_hhmm(window[0])
    b = parse_hhmm(window[1])
    if a is None or b is None:
        return True
    cur = now.hour * 60 + now.minute
    if a <= b:
        return a <= cur <= b
    return cur >= a or cur <= b


def ago(ts, now_ts=None):
    if not ts:
        return "很久以前"
    d = max(0.0, (now_ts or time.time()) - float(ts))
    if d < 90:
        return "刚刚"
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        h = d / 3600
        return (f"{h:.1f}".rstrip("0").rstrip(".") + " 小时前")
    days = d / 86400
    if days < 30:
        return (f"{days:.1f}".rstrip("0").rstrip(".") + " 天前")
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def stamp(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return "--"


def part_of_day(now):
    h = now.hour
    if h < 5:
        return "凌晨"
    if h < 8:
        return "清早"
    if h < 11:
        return "上午"
    if h < 13:
        return "中午"
    if h < 18:
        return "下午"
    if h < 23:
        return "晚上"
    return "深夜"


# ---------------------------------------------------------------- 读材料

def _chat_db(root):
    return _root(root) / "data" / "chat.db"


def recent_messages(root, target, limit=12):
    db = _chat_db(root)
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "select role, text, ts from messages where wxid=? order by ts desc limit ?",
                (target, limit),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [(r, t, float(ts or 0)) for r, t, ts in reversed(rows)]


def last_message_ts(root, target):
    db = _chat_db(root)
    if not db.exists():
        return 0
    try:
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "select max(ts) from messages where wxid=?", (target,)
            ).fetchone()
        finally:
            conn.close()
        return float(row[0] or 0) if row else 0
    except Exception:
        return 0


def recent_history(root, target, turns=8):
    rows = recent_messages(root, target, limit=turns)
    return [("我" if r == "me" else "对方", t) for r, t, _ts in rows]


def last_self_sent(root, target):
    """我们上一次主动/回复发出去的原文（self_sent.jsonl）。

    watcher 也读这个文件来避免自己回自己，所以它一定是最全的发送记录。
    """
    p = _root(root) / "data" / "self_sent.jsonl"
    if not p.exists():
        return 0.0, ""
    try:
        lines = p.read_text(encoding="utf-8").splitlines()[-300:]
    except OSError:
        return 0.0, ""
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("target") == target and rec.get("text"):
            return float(rec.get("ts") or 0), str(rec["text"])
    return 0.0, ""


def recent_videos(root, limit=2, max_age_h=48):
    """最近刷到的内容，当作「由头」的一种（没有就返回空，不编）。"""
    db = _root(root) / "data" / "browse.db"
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "select title, up, fetched_at from videos order by fetched_at desc limit ?",
                (limit * 4,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    cutoff = time.time() - max_age_h * 3600
    out = []
    for title, up, fetched in rows:
        if float(fetched or 0) < cutoff:
            continue
        if title:
            out.append((str(title)[:60], str(up or "")))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 上下文

def gather(cfg, root, target, trigger, now=None, state=None, dstate=None):
    """把「模型做判断需要知道的东西」都收齐。

    没有这一段，模型只能瞎判——它不知道上次说话是什么时候、对方回没回、
    你今天已经主动过几次，自然每次都只会说「在吗」。
    """
    now = now or datetime.now()
    now_ts = now.timestamp()
    pro = pro_conf(cfg)
    state = state if state is not None else load_state(root)
    dstate = dstate if dstate is not None else load_dstate(root)
    day = now.strftime("%Y-%m-%d")

    msgs = recent_messages(root, target)
    last = msgs[-1] if msgs else ("", "", 0.0)
    them = [m for m in msgs if m[0] == "them"]
    mine = [m for m in msgs if m[0] == "me"]
    last_them = them[-1][2] if them else 0.0
    last_me = mine[-1][2] if mine else 0.0

    rec = ((state.get(day) or {}).get(target) or {})
    drec = dstate.get(target) or {}
    speak_ts, speak_text = last_self_sent(root, target)

    return {
        "target": target,
        "trigger": trigger,
        "now": now,
        "now_ts": now_ts,
        "day": day,
        "msgs": msgs,
        "last_any": float(last[2] or 0),
        "last_role": last[0],
        "last_text": last[1],
        "last_them": last_them,
        "last_me": last_me,
        "quiet_min": ((now_ts - float(last[2])) / 60) if last[2] else 99999.0,
        "awaiting": bool(last_me and last_me > last_them),
        "sent_today": sent_today(state, target, day),
        "daily_max": int(pro.get("daily_max") or 0),
        "last_speak_ts": speak_ts,
        "last_speak_text": speak_text,
        "silent_streak": int(drec.get("silent_streak") or 0),
        "last_silent_ts": float(drec.get("last_silent_ts") or 0),
        "last_silent_reason": str(drec.get("last_reason") or ""),
        "last_ask_ts": float(drec.get("last_ask_ts") or 0),
        "kinds": list(rec.get("kinds") or []),
        "videos": recent_videos(root),
        "hint": str(pro.get("hint") or "").strip(),
        "persona": str(cfg.get("persona") or "").strip(),
        "me_name": str(cfg.get("my_name") or "").strip(),
    }


def render_prompt(g):
    """判断用提示词。

    要点：先摆事实（时间、最后一条是谁说的、多久了、今天发过几次），
    再给「该开口 / 该闭嘴」的锚点，最后明确允许 silent。
    只给「发/不发」二选一而不给静默出口，模型会永远觉得该说点什么。
    """
    now = g["now"]
    week = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
    me = g["me_name"] or "我"
    name = g["target"]
    L = []

    L.append(f"你在用微信替「{me}」跟「{name}」聊天。消息列表里没有新消息，"
             "你要自己判断：这会儿要不要主动开个口。")
    L.append("")
    if g["persona"]:
        L.append(f"你的说话风格：{g['persona']}。短句、口语，像随手打字。")
        L.append("")

    L.append("【现在】")
    L.append(f"{now:%Y-%m-%d %H:%M} {week} · {part_of_day(now)}")
    if now.hour >= 23 or now.hour < 6:
        L.append("（这个点大多数人已经休息了）")
    L.append("")

    L.append("【最近的对话】")
    for role, text, ts in g["msgs"][-10:]:
        who = "我" if role == "me" else "对方"
        L.append(f"{stamp(ts)} {who}：{text}")
    if not g["msgs"]:
        L.append("（还没聊过）")
    L.append("")
    if g["last_any"]:
        who_last = "我" if g["last_role"] == "me" else "对方"
        L.append(f"最后一条是{who_last}说的，{ago(g['last_any'], g['now_ts'])}。")
    if g["awaiting"]:
        L.append("你最后那条已经发出去了，对方一直没回。")
    L.append("")

    L.append("【你主动开口的记录】")
    L.append(f"今天已经主动开口 {g['sent_today']} 次（上限 {g['daily_max']} 次）。")
    if g["last_speak_ts"]:
        L.append(f"上一次开口是 {ago(g['last_speak_ts'], g['now_ts'])}，"
                 f"内容是「{g['last_speak_text'][:60]}」。")
    else:
        L.append("最近没有主动开口过。")
    if g["silent_streak"]:
        tail = f"，理由是「{g['last_silent_reason']}」" if g["last_silent_reason"] else ""
        L.append(f"你之前连着判断了 {g['silent_streak']} 次「先不说」{tail}。")
    L.append("")

    if g["videos"]:
        L.append("【最近看到的】")
        for title, up in g["videos"]:
            L.append(f"- {title}" + (f"（{up}）" if up else ""))
        L.append("")

    if g["hint"]:
        L.append(f"【用户的额外要求】{g['hint']}")
        L.append("")

    L.append("【该开口的情况】")
    L.append("1. 有具体由头：对话里提过但没聊完的事、隔了很久想起来、到了某个时间点")
    L.append("2. 沉默够久，而且上次不是聊到自然结束")
    L.append("3. 你要说的话能接上之前的话题，不是凭空找一句")
    L.append("")
    L.append("【该闭嘴的情况，这很常见】")
    L.append("1. 你最后一条对方还没回——别再发，那像催")
    L.append("2. 刚聊完没多久，或者今天已经主动过了")
    L.append("3. 没有由头，只是想「找点话说」")
    L.append("4. 这个点对方可能在忙、在睡")
    L.append("5. 你刚判断过不说，而情况没有变化")
    L.append("")
    L.append("默认倾向是「不说」。一天主动这么一两次已经算多了，宁可少说，也别凑话。")
    L.append("不要编对话里没发生过的事。")
    L.append("")
    L.append("【输出】只输出一个 json 对象，不要解释、不要 markdown：")
    L.append('{"action":"speak","reason":"为什么现在说，20 字内","text":"要发的内容"}')
    L.append("或者")
    L.append('{"action":"silent","reason":"为什么不说，20 字内"}')
    L.append("")
    L.append("action 是 speak 时，text 要求：一到三句，口语，不用列表、不用引号、不提自己是 AI。")
    return "\n".join(L)


# ---------------------------------------------------------------- 问模型 / 解析

def ask(cfg, root, prompt):
    """问一次模型，返回 (原始文本, 模型名, 耗时毫秒)。"""
    pro = pro_conf(cfg)
    model = (pro.get("decide_model") or "deepseek-chat").strip()
    backend = backends.build(cfg, _root(root))
    t0 = time.time()
    if hasattr(backend, "chat"):
        raw = backend.chat(prompt, model=model, temperature=0.85,
                           max_tokens=300, json_mode=True)
    else:
        # 非 API 后端（stub/dsh）没有 chat：把提示词当模板跑一次
        ctx = backends.Context(
            wxid="", name="", text="", history=[], persona="",
            template=prompt, me_name=cfg.get("my_name", ""),
        )
        raw = backend.reply(ctx)
    return raw, model, int((time.time() - t0) * 1000)


def _first_json(s):
    """从模型输出里抠出第一个完整 JSON 对象（能容忍 ```json 包裹和前后废话）。"""
    start = s.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except Exception:
                        break
        start = s.find("{", start + 1)
    return None


def clean_text(t):
    """把要发的内容收拾成人话：最多三行、去掉引号和「我：」前缀。"""
    t = backends.first_line(t or "", max_lines=3, max_chars=220)
    return t.strip().strip("\"“”'‘’").strip()


def parse_decision(raw):
    """返回 (action, reason, text)。看不懂一律当 silent——绝不猜着发。"""
    text = (raw or "").strip()
    if not text:
        return "silent", "模型没输出", ""
    obj = _first_json(text)
    if obj is None:
        return "silent", "输出不是 JSON", ""
    action = str(obj.get("action") or "").strip().lower()
    reason = str(obj.get("reason") or "").strip()[:60]
    body = clean_text(str(obj.get("text") or ""))
    if action in ("speak", "send", "说", "开口"):
        if not body:
            return "silent", reason or "说了要开口但没给内容", ""
        return "speak", reason[:40], body
    if action in ("silent", "drop", "不说", "闭嘴", ""):
        return "silent", reason or "没说理由", ""
    return "silent", f"看不懂的 action：{action}", ""


# ---------------------------------------------------------------- 记日志

def append_decision(rec, root=None):
    try:
        p = decisions_file(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_decisions(root=None, limit=30):
    p = decisions_file(root)
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-max(1, limit):]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def decision_stats(root=None, days=7):
    rows = load_decisions(root, limit=100000)
    cutoff = time.time() - days * 86400
    rows = [r for r in rows if float(r.get("ts") or 0) >= cutoff]
    total = len(rows)
    speak = [r for r in rows if r.get("action") == "speak" and r.get("sent")]
    silent = [r for r in rows if r.get("action") == "silent"]
    errors = [r for r in rows if r.get("action") in ("error", "stale")]
    reasons = {}
    for r in silent:
        key = (r.get("reason") or "没说理由")[:24]
        reasons[key] = reasons.get(key, 0) + 1
    by_target = {}
    for r in rows:
        t = r.get("target") or "?"
        slot = by_target.setdefault(t, {"ask": 0, "speak": 0})
        slot["ask"] += 1
        if r.get("action") == "speak" and r.get("sent"):
            slot["speak"] += 1
    return {
        "days": days,
        "ask": total,
        "speak": len(speak),
        "silent": len(silent),
        "error": len(errors),
        "rate": (len(speak) / total) if total else 0.0,
        "silent_reasons": sorted(reasons.items(), key=lambda kv: -kv[1])[:10],
        "by_target": by_target,
    }


# ---------------------------------------------------------------- 闸门 / 触发

def check_gates(pro, g, state, dstate, now, force=False):
    """代码层的硬闸门。模型再想说，这些不满足也不发。返回拦住的原因或 None。"""
    if force:
        return None
    today = now.strftime("%Y-%m-%d")
    rec = ((state.get(today) or {}).get(g["target"]) or {})

    # 从传进来的 state 现算，别用 gather 时缓存的值：两处不一致时，
    # 上限这道闸就会形同虚设。
    sent = sent_today(state, g["target"], today)
    if g["daily_max"] > 0 and sent >= g["daily_max"]:
        return f"今天已主动 {sent} 次，到上限了"

    min_gap = float(pro.get("min_gap_min") or 0)
    last_auto = float(rec.get("last_auto") or 0)
    if min_gap > 0 and last_auto and (g["now_ts"] - last_auto) < min_gap * 60:
        return f"距上次主动不到 {min_gap:g} 分钟"

    quiet = float(pro.get("quiet_min") or 0)
    if quiet > 0 and g["msgs"] and g["quiet_min"] < quiet:
        return f"刚聊过（{g['quiet_min']:.0f} 分钟前）"

    drec = dstate.get(g["target"]) or {}
    streak = int(drec.get("silent_streak") or 0)
    base = float(pro.get("silent_backoff_min") or 0)
    maxm = float(pro.get("silent_backoff_max_min") or 0)
    if streak > 0 and base > 0:
        wait = min(base * (2 ** (streak - 1)), max(base, maxm))
        last_ask = float(drec.get("last_ask_ts") or 0)
        if last_ask and (g["now_ts"] - last_ask) < wait * 60:
            return f"刚判断过不说，冷却 {wait:.0f} 分钟"
    return None


def pick_trigger(pro, g, state, dstate, now):
    """现在为什么要问一次。返回 (trigger, 说明) 或 (None, 原因)。"""
    now_ts = g["now_ts"]
    kinds = g["kinds"]
    cur = now.hour * 60 + now.minute

    daily_at = (pro.get("daily_at") or "").strip()
    if daily_at:
        dm = parse_hhmm(daily_at)
        if dm is not None and cur >= dm and "daily" not in kinds:
            return "daily", f"到点 {daily_at}"

    idle_h = float(pro.get("idle_hours") or 0)
    if idle_h > 0 and g["msgs"]:
        quiet_h = g["quiet_min"] / 60
        tag = "awaiting" if g["awaiting"] else "idle"
        if quiet_h >= idle_h and tag not in kinds:
            extra = "（对方没回你最后一条）" if g["awaiting"] else ""
            return tag, f"静默 {quiet_h:.1f} 小时{extra}"

    hb = float(pro.get("heartbeat_min") or 0)
    if hb > 0:
        drec = dstate.get(g["target"]) or {}
        last_ask = float(drec.get("last_ask_ts") or 0)
        if now_ts - last_ask >= hb * 60:
            return "heartbeat", "心跳"

    return None, "没有触发条件"


# ---------------------------------------------------------------- 单个联系人

def _skip_log(pro, dstate, target, reason, now, root):
    """闸门拦下的事记一笔，但一小时最多记一次，别把日志刷满。"""
    drec = _drec(dstate, target)
    last = float(drec.get("last_skip_log") or 0)
    if now.timestamp() - last < 3600:
        return
    drec["last_skip_log"] = now.timestamp()
    append_decision({
        "ts": now.timestamp(), "day": now.strftime("%Y-%m-%d"),
        "target": target, "trigger": "", "action": "skip",
        "reason": reason[:60], "text": "", "sent": False, "ms": 0, "model": "",
    }, root)


class Decision(NamedTuple):
    """一次判断的结论。stage 只有两种：问模型，或者不问。"""
    stage: str          # "skip" | "ask"
    reason: str
    trigger: str = ""


def evaluate(pro, g, state, dstate, now, force=False):
    """闸门 + 触发，合成一个纯函数：给事实，返回该不该问。

    不碰文件、不调模型、不发消息——所以可以用表格一行行测。
    以前这两步埋在 decide_one 里，测试只能 patch 模块级变量。
    """
    why = check_gates(pro, g, state, dstate, now, force=force)
    if why:
        return Decision("skip", why)
    trigger, desc = pick_trigger(pro, g, state, dstate, now)
    if not trigger:
        return Decision("skip", desc)
    return Decision("ask", desc, trigger)


def decide_one(cfg, root, target, now=None, force=False, dry=False, show_prompt=False):
    """对一个联系人走一遍：收上下文 → 判断 → 问模型 → （发）。

    返回一个 dict，tick 和命令行都用它。
    """
    root = _root(root)
    now = now or datetime.now()
    pro = pro_conf(cfg)
    state = load_state(root)
    dstate = load_dstate(root)
    out = {"target": target, "stage": "skip", "reason": "", "action": "",
           "text": "", "trigger": "", "ms": 0, "model": "", "prompt": ""}

    g = gather(cfg, root, target, "", now=now, state=state, dstate=dstate)
    if not g["msgs"]:
        out["reason"] = "没有对话记录"
        return out

    d = evaluate(pro, g, state, dstate, now, force=force)
    if d.stage == "skip":
        out["reason"] = d.reason
        _skip_log(pro, dstate, target, d.reason, now, root)
        save_dstate(dstate, root)
        return out

    trigger = d.trigger
    out["trigger"] = trigger
    g["trigger"] = trigger

    prompt = render_prompt(g)
    out["prompt"] = prompt
    if show_prompt:
        out["stage"] = "prompt"
        return out

    try:
        raw, model, ms = ask(cfg, root, prompt)
    except Exception as exc:
        out.update(stage="error", reason=f"{type(exc).__name__}: {exc}"[:200], model="")
        drec = _drec(dstate, target)
        drec["last_ask_ts"] = now.timestamp()
        drec["silent_streak"] = int(drec.get("silent_streak") or 0) + 1
        append_decision({
            "ts": now.timestamp(), "day": now.strftime("%Y-%m-%d"), "target": target,
            "trigger": trigger, "action": "error", "reason": out["reason"],
            "text": "", "sent": False, "ms": 0, "model": "",
        }, root)
        save_dstate(dstate, root)
        return out

    out.update(ms=ms, model=model)
    action, reason, body = parse_decision(raw)

    drec = _drec(dstate, target)
    drec["last_ask_ts"] = now.timestamp()
    drec["last_trigger"] = trigger

    if action == "silent":
        drec["silent_streak"] = int(drec.get("silent_streak") or 0) + 1
        drec["last_silent_ts"] = now.timestamp()
        drec["last_reason"] = reason
        out.update(stage="silent", action="silent", reason=reason)
        append_decision({
            "ts": now.timestamp(), "day": now.strftime("%Y-%m-%d"), "target": target,
            "trigger": trigger, "action": "silent", "reason": reason,
            "text": "", "sent": False, "ms": ms, "model": model,
        }, root)
        save_dstate(dstate, root)
        return out

    # 决定要说了：先看这一刻对话有没有变（判断的几秒里对方可能说话了）。
    # 这一条不受 --force 影响：force 是跳过频率限制，不是允许发一句过期的话。
    latest = last_message_ts(root, target)
    if latest > g["last_any"] + 0.5:
        drec["silent_streak"] = int(drec.get("silent_streak") or 0) + 1
        out.update(stage="stale", action="stale", reason="判断期间来了新消息", text=body)
        append_decision({
            "ts": now.timestamp(), "day": now.strftime("%Y-%m-%d"), "target": target,
            "trigger": trigger, "action": "stale", "reason": "判断期间来了新消息",
            "text": body, "sent": False, "ms": ms, "model": model,
        }, root)
        save_dstate(dstate, root)
        return out

    out.update(stage="speak", action="speak", reason=reason, text=body)
    if dry:
        drec["silent_streak"] = 0
        save_dstate(dstate, root)
        return out

    ok, how = outbox.deliver(target, body, trigger, root=root, now=now)
    if ok:
        drec["silent_streak"] = 0
        drec["last_reason"] = f"说了：{reason}"
        drec["last_send_ts"] = now.timestamp()
    else:
        # 发失败也退避，否则每分钟重试一次
        drec["silent_streak"] = int(drec.get("silent_streak") or 0) + 1
        drec["last_reason"] = f"发送失败：{str(how)[:40]}"

    out.update(stage="speak" if ok else "error", reason=reason if ok else str(how),
               sent=bool(ok))
    append_decision({
        "ts": now.timestamp(), "day": now.strftime("%Y-%m-%d"), "target": target,
        "trigger": trigger, "action": "speak", "reason": reason, "text": body,
        "sent": bool(ok), "how": str(how)[:120], "ms": ms, "model": model,
    }, root)
    save_dstate(dstate, root)
    return out


# ---------------------------------------------------------------- 主循环入口

def tick(cfg, root=None, now=None, force=False):
    """桥里每分钟调一次。返回这一轮做了什么（给控制台看）。"""
    root = _root(root)
    now = now or datetime.now()
    pro = pro_conf(cfg)
    out = []
    if not pro.get("enabled"):
        return out
    if (pro.get("mode") or "decision").lower() == "timer":
        return tick_timer(cfg, root, now)

    if not in_window(now, pro.get("window")) and not force:
        return out

    for target in [t for t in (pro.get("targets") or []) if t]:
        try:
            r = decide_one(cfg, root, target, now=now, force=force)
        except Exception as exc:
            out.append(f"{target}：判断出错 {type(exc).__name__}: {exc}")
            continue
        stage = r.get("stage")
        if stage == "silent":
            out.append(f"{target}：不说（{r['reason']}）")
        elif stage == "speak" and r.get("sent"):
            out.append(f"{target}：说了「{r['text']}」→ 已发送")
        elif stage == "speak":
            out.append(f"{target}：想说但没发（{r['reason']}）")
        elif stage in ("error", "stale"):
            out.append(f"{target}：{r['reason']}")
    return out


def tick_timer(cfg, root=None, now=None):
    """老逻辑：到点就发，不问。保留给 mode="timer"。"""
    root = _root(root)
    now = now or datetime.now()
    pro = pro_conf(cfg)
    out = []
    win = pro.get("window") or ["09:00", "23:00"]
    if not in_window(now, win):
        return out

    state = load_state(root)
    cap = int(pro.get("daily_max", 2))
    daily_at = (pro.get("daily_at") or "").strip()
    idle_h = float(pro.get("idle_hours") or 0)
    hm = now.strftime("%H:%M")

    for target in [t for t in (pro.get("targets") or []) if t]:
        if sent_today(state, target, now.strftime("%Y-%m-%d")) >= cap:
            continue
        today = now.strftime("%Y-%m-%d")
        rec = ((state.get(today) or {}).get(target) or {})
        kinds = rec.get("kinds") or []
        last_auto = float(rec.get("last_auto", 0))
        if last_auto and now.timestamp() - last_auto < 1800:
            continue

        if daily_at and hm >= daily_at and "daily" not in kinds:
            ok, how = fire(cfg, root, target, "daily", now=now)
            out.append(f"[定时 {daily_at}] {target}: {ok} {how}")
            continue

        if idle_h > 0 and "idle" not in kinds:
            last_msg = last_message_ts(root, target)
            quiet_hours = (now.timestamp() - last_msg) / 3600 if last_msg else 999
            if quiet_hours >= idle_h:
                ok, how = fire(cfg, root, target, "idle", now=now)
                out.append(f"[空闲 {quiet_hours:.1f}h] {target}: {ok} {how}")
    return out


# ---------------------------------------------------------------- 发送

def compose(cfg, root, target):
    """生成开场白：有模板用模板，否则让模型按历史写一句（仅 timer/手动用）。"""
    pro = cfg.get("proactive") or {}
    template = (pro.get("template") or "").strip()
    if template:
        return (template
                .replace("{{me}}", cfg.get("my_name", ""))
                .replace("{{name}}", target))

    history = recent_history(root, target)
    hint = (pro.get("hint") or "").strip()
    rule = (
        "现在对话停下来了，你要主动发一条微信，重新起个话头。\n"
        "要求：只发一条；自然，不要像群发；可以参考下面的记录找话题，"
        "没有记录就问一句近况。\n"
    )
    if hint:
        rule += f"额外要求：{hint}\n"
    tpl = backends.load_template(_root(root))
    ctx = backends.Context(
        wxid=target, name=target, text="（现在轮到你主动开口）",
        history=history, persona=cfg.get("persona", ""),
        template=rule + "\n" + tpl, emoji_ok=False,
        me_name=cfg.get("my_name", ""),
    )
    backend = backends.build(cfg, _root(root))
    return backend.reply(ctx)


def find_window(target):
    import wxuia

    for hwnd, name, ctrl in wxuia.find_chat_windows():
        if name == target:
            return hwnd, ctrl
    return None, None


def send_to(target, text, guard=None):
    import wxuia

    hwnd, ctrl = find_window(target)
    if hwnd is None:
        return False, f"没找到「{target}」的独立聊天窗口"
    wxuia.raise_window(hwnd)
    wxuia.ensure_visible(hwnd)
    try:
        ctrl = wxuia.auto.ControlFromHandle(hwnd)
    except Exception:
        pass
    return wxuia.send_text(ctrl, text, hwnd=hwnd, guard=guard)


def fire(cfg, root, target, kind="manual", text=None, now=None):
    """直接发一条（手动 / timer 模式）。text 给了就原样发，否则按模板/模型生成。

    判断模式不走这里——判断模式见 decide_one()。
    """
    pro = cfg.get("proactive") or {}
    now = now or datetime.now()
    state = load_state(root)
    cap = int(pro.get("daily_max", 2))
    if kind != "manual" and sent_today(state, target, now.strftime("%Y-%m-%d")) >= cap:
        return False, f"今天已达上限 {cap} 条"

    if text:
        body = (text
                .replace("{{me}}", cfg.get("my_name", ""))
                .replace("{{name}}", target))
    else:
        try:
            body = compose(cfg, root, target)
        except Exception as exc:
            return False, f"生成失败: {exc}"
    if not body:
        return False, "内容为空"

    ok, how = outbox.deliver(target, body, kind, root=root, now=now)
    return ok, f"{body} | {how}"


# ---------------------------------------------------------------- 命令行

def _fmt_row(r):
    action = r.get("action") or "?"
    when = stamp(r.get("ts"))
    target = r.get("target") or "?"
    trig = r.get("trigger") or "-"
    reason = (r.get("reason") or "")[:40]
    text = (r.get("text") or "").replace("\n", " ")[:50]
    mark = {"speak": "说", "silent": "静", "skip": "拦", "error": "错",
            "stale": "废"}.get(action, action)
    body = f"「{text}」" if text else ""
    return f"{when} [{mark}] {target:<8} {trig:<9} {reason} {body}".rstrip()


def main():
    import argparse

    ap = argparse.ArgumentParser(description="主动搭话（判断版）")
    ap.add_argument("target", nargs="?", help="只处理这个人（默认用配置里的 targets）")
    ap.add_argument("--decide", action="store_true", help="只判断、不发送（建议配合 --show-prompt）")
    ap.add_argument("--send", action="store_true", help="老行为：生成一条并直接发（不判断）")
    ap.add_argument("--dry-run", action="store_true", help="判断完不发送")
    ap.add_argument("--force", action="store_true", help="忽略时段/上限/冷却")
    ap.add_argument("--at", help="假装现在是这个时间，如 2026-10-25 21:30")
    ap.add_argument("--show-prompt", action="store_true", help="把判断用的提示词打出来")
    ap.add_argument("--log", type=int, nargs="?", const=20, metavar="N", help="看最近 N 条判断记录")
    ap.add_argument("--stats", action="store_true", help="看最近 7 天的判断统计")
    ap.add_argument("--days", type=int, default=7, help="--stats 统计几天")
    args = ap.parse_args()

    cfg = json.loads((_HERE / "config.json").read_text(encoding="utf-8-sig"))

    if args.log is not None:
        rows = load_decisions(_HERE, limit=args.log)
        if not rows:
            print("还没有判断记录（data/decisions.jsonl 是空的）")
            return 0
        for r in rows:
            print(_fmt_row(r))
        return 0

    if args.stats:
        s = decision_stats(_HERE, days=args.days)
        print(f"最近 {s['days']} 天：判断 {s['ask']} 次，开口 {s['speak']} 次，"
              f"沉默 {s['silent']} 次，出错 {s['error']} 次，开口率 {s['rate']:.0%}")
        if s["by_target"]:
            print("按联系人：")
            for t, v in s["by_target"].items():
                print(f"  {t}: 问 {v['ask']} 次 / 说 {v['speak']} 次")
        if s["silent_reasons"]:
            print("沉默的理由：")
            for reason, n in s["silent_reasons"]:
                print(f"  {n:>3}× {reason}")
        return 0

    now = datetime.now()
    if args.at:
        try:
            now = datetime.strptime(args.at, "%Y-%m-%d %H:%M")
        except ValueError:
            print("--at 的格式要像 2026-10-25 21:30")
            return 2

    pro = cfg.get("proactive") or {}
    targets = [args.target] if args.target else [t for t in (pro.get("targets") or []) if t]
    if not targets:
        print("没有目标：要么传名字，要么在 config.json 的 proactive.targets 里填")
        return 1

    if args.send:
        for t in targets:
            text = compose(cfg, _HERE, t)
            print(f"[生成] {t}: {text}")
            if args.dry_run:
                continue
            ok, how = fire(cfg, _HERE, t, "manual")
            print(f"[发送] {ok} / {how}")
        return 0

    rc = 0
    for t in targets:
        r = decide_one(cfg, _HERE, t, now=now, force=args.force,
                       dry=args.decide or args.dry_run,
                       show_prompt=args.show_prompt)
        if args.show_prompt:
            print(r.get("prompt") or "（没有提示词：没到判断这一步）")
            print(f"—— {t}: {r.get('stage')} / {r.get('reason')}")
            continue
        stage = r.get("stage")
        if stage == "speak":
            tail = "（dry-run，没发）" if (args.decide or args.dry_run) else "→ 已发送" if r.get("sent") else "→ 没发出去"
            print(f"{t}：开口「{r['text']}」 {tail}\n    理由：{r['reason']} [{r['trigger']} {r['ms']}ms {r['model']}]")
        elif stage == "silent":
            print(f"{t}：不说 —— {r['reason']} [{r['trigger']} {r['ms']}ms]")
        elif stage in ("error", "stale"):
            print(f"{t}：{stage} —— {r['reason']}")
            rc = 1
        else:
            print(f"{t}：跳过 —— {r['reason']}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
