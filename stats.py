"""运行统计：从 chat.db 里读出来，给界面画图。

单独的模块，不 import bridge —— 界面进程只要几个数，没必要把整个桥
（配置、钩子、记忆、订阅）都建起来。bridge.stats() 现在只是转调这里。

数据都在两张表里：
  events(ts, wxid, action, reason, reply, elapsed_ms)   action = send / drop / error
  usage (ts, wxid, model, hit, miss, out_tokens)        DeepSeek 的缓存命中与输出
"""
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import os as _os

_HERE = Path(_os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
DB_PATH = _HERE / "data" / "chat.db"

SEND, DROP, ERROR = "send", "drop", "error"

# 数据库里存的是代号，画图给人看得翻一下。新增原因记得补这里，
# 没补的会原样显示（难看但不丢信息）。
REASON_TEXT = {
    "no_mention": "群里没 @ 我",
    "duplicate": "重复消息",
    "rate_limited_per_minute": "每分钟限流",
    "rate_limited_daily": "当天限流",
    "rate_limited": "限流",
    "timetable": "查表直答",
    "search": "带检索回复",
    "empty": "空消息",
    "blacklist": "黑名单",
    "bot": "像机器人",
    "self": "自己发的",
    "sleeping": "免打扰时段",
}


def reason_text(action, reason):
    """把 (action, reason) 翻成一句人话。"""
    r = reason or ""
    if not r:
        return {SEND: "正常回复", DROP: "没写原因（丢弃）",
                ERROR: "没写原因（报错）"}.get(action, "没写原因")
    if r.startswith("backend_error"):
        tail = r.split(":", 1)[1].strip() if ":" in r else ""
        return f"模型报错：{tail[:18]}" if tail else "模型报错"
    return REASON_TEXT.get(r, r)


def connect(path=None):
    path = Path(path or DB_PATH)
    if not path.exists():
        return None
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("pragma busy_timeout=3000")
    try:
        conn.execute("pragma query_only=on")     # 只读，别在界面里误写
    except sqlite3.DatabaseError:
        pass
    return conn


def _items(conn, cutoff):
    rows = conn.execute(
        "select action, reason, count(*) as n, avg(elapsed_ms) "
        "from events where ts>=? group by action, reason order by n desc",
        (cutoff,),
    ).fetchall()
    return [{"action": a or "", "reason": r or "", "count": n, "avg_ms": int(avg or 0)}
            for a, r, n, avg in rows]


def summary(hours=24, path=None):
    """和原来 bridge.stats() 一样的结构，接口那边不用改。"""
    empty = {"hours": hours, "total": 0, "sent": 0, "dropped": 0, "error_count": 0,
             "model_ok": 0, "model_fail": 0, "direct_answer": 0, "reply_rate": 0.0,
             "model_fail_rate": 0.0,
             "tokens": {"calls": 0, "cache_hit": 0, "cache_miss": 0, "output": 0,
                        "cache_hit_rate": 0.0, "total": 0},
             "items": []}
    conn = connect(path)
    if conn is None:
        return empty
    try:
        cutoff = time.time() - hours * 3600
        items = _items(conn, cutoff)
        u = conn.execute(
            "select coalesce(sum(hit),0), coalesce(sum(miss),0), "
            "coalesce(sum(out_tokens),0), count(*) from usage where ts>=?",
            (cutoff,)).fetchone()
    finally:
        conn.close()

    total = sum(it["count"] for it in items)
    by_action = {}
    for it in items:
        by_action[it["action"]] = by_action.get(it["action"], 0) + it["count"]

    sent = by_action.get(SEND, 0)
    dropped = by_action.get(DROP, 0)
    errored = by_action.get(ERROR, 0)
    model_fail = sum(it["count"] for it in items if it["reason"].startswith("backend_error"))
    # 老数据里失败也记成 send，扣掉，免得 model_ok 被扣两次
    fail_in_send = sum(it["count"] for it in items
                       if it["action"] == SEND and it["reason"].startswith("backend_error"))
    direct = sum(it["count"] for it in items if it["reason"] == "timetable")
    model_ok = max(0, sent - direct - fail_in_send)
    attempts = sent - fail_in_send + model_fail

    hit, miss, out_tokens, calls = u
    base = hit + miss
    return {
        "hours": hours,
        "total": total,
        "sent": sent,
        "dropped": dropped,
        "error_count": errored,
        "model_ok": model_ok,
        "model_fail": model_fail,
        "direct_answer": direct,
        "reply_rate": round(sent / total * 100, 1) if total else 0.0,
        "model_fail_rate": round(model_fail / attempts * 100, 1) if attempts else 0.0,
        "tokens": {"calls": calls, "cache_hit": hit, "cache_miss": miss,
                   "output": out_tokens,
                   "cache_hit_rate": round(hit / base * 100, 1) if base else 0.0,
                   "total": hit + miss + out_tokens},
        "items": items,
    }


def _bucket(ts, hours):
    """按小时（<=48 小时窗口）或按天（更长）归桶，返回桶的起点时间戳。"""
    if hours <= 48:
        return int(ts // 3600) * 3600
    d = datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp())


def _label(ts, hours):
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%H:00") if hours <= 48 else f"{dt.month}/{dt.day}"


def series(hours=24, path=None):
    """时间序列，给柱状图用。返回 {labels, send, drop, error, total}。"""
    now = time.time()
    start = now - hours * 3600
    step = 3600 if hours <= 48 else 86400
    first = _bucket(start, hours)

    labels, sends, drops, errors = [], [], [], []
    t = first
    while t <= now:
        labels.append(_label(t, hours))
        sends.append(0)
        drops.append(0)
        errors.append(0)
        t += step

    conn = connect(path)
    if conn is None:
        return {"labels": labels, "send": sends, "drop": drops, "error": errors,
                "total": [0] * len(labels)}
    try:
        rows = conn.execute(
            "select ts, action, count(*) from events where ts>=? group by ts, action",
            (start,)).fetchall()
    finally:
        conn.close()

    index = {start_t: i for i, start_t in enumerate(
        [first + i * step for i in range(len(labels))])}
    for ts, action, n in rows:
        i = index.get(_bucket(ts, hours))
        if i is None:
            continue
        if action == SEND:
            sends[i] += n
        elif action == DROP:
            drops[i] += n
        elif action == ERROR:
            errors[i] += n
    total = [sends[i] + drops[i] + errors[i] for i in range(len(labels))]
    return {"labels": labels, "send": sends, "drop": drops, "error": errors,
            "total": total}


def reasons(hours=24, top=8, path=None, actions=(DROP, ERROR)):
    """为什么没回成：横向条形图用。默认只看丢弃和失败。

    正常发送也带 reason（比如 `search` 表示这轮检索过），但那不是「原因」，
    放进来会把图稀释成一片「正常」。要看全量用 summary()['items']。
    """
    conn = connect(path)
    if conn is None:
        return []
    try:
        items = _items(conn, time.time() - hours * 3600)
    finally:
        conn.close()

    items = [it for it in items if it["count"] and (not actions or it["action"] in actions)]
    whole = sum(it["count"] for it in items) or 1
    items.sort(key=lambda it: -it["count"])
    out = []
    for it in items[:top]:
        out.append({"reason": reason_text(it["action"], it["reason"]),
                    "code": it["reason"],
                    "count": it["count"], "avg_ms": it["avg_ms"],
                    "action": it["action"],
                    "share": round(it["count"] / whole * 100, 1)})
    return out


def tokens(hours=24, path=None):
    """token 用量，环形图用。"""
    conn = connect(path)
    if conn is None:
        return {"calls": 0, "cache_hit": 0, "cache_miss": 0, "output": 0,
                "cache_hit_rate": 0.0}
    try:
        hit, miss, out_tokens, calls = conn.execute(
            "select coalesce(sum(hit),0), coalesce(sum(miss),0), "
            "coalesce(sum(out_tokens),0), count(*) from usage where ts>=?",
            (time.time() - hours * 3600,)).fetchone()
    finally:
        conn.close()
    base = hit + miss
    return {"calls": calls, "cache_hit": hit, "cache_miss": miss, "output": out_tokens,
            "cache_hit_rate": round(hit / base * 100, 1) if base else 0.0}


def hourly_load(hours=24, path=None):
    """每小时平均耗时，看模型慢在哪。返回 {labels, ms}。"""
    s = series(hours, path)
    conn = connect(path)
    if conn is None:
        return {"labels": s["labels"], "ms": [0] * len(s["labels"])}
    try:
        rows = conn.execute(
            "select ts, avg(elapsed_ms) from events where ts>=? and action='send' "
            "group by ts", (time.time() - hours * 3600,)).fetchall()
    finally:
        conn.close()
    step = 3600 if hours <= 48 else 86400
    first = _bucket(time.time() - hours * 3600, hours)
    index = {first + i * step: i for i in range(len(s["labels"]))}
    acc = [[] for _ in s["labels"]]
    for ts, ms in rows:
        i = index.get(_bucket(ts, hours))
        if i is not None and ms:
            acc[i].append(ms)
    return {"labels": s["labels"],
            "ms": [int(sum(x) / len(x)) if x else 0 for x in acc]}


def main():
    for h in (24, 168):
        s = summary(h)
        print(f"=== 近 {h} 小时 ===")
        print(f"  处理 {s['total']} · 回复 {s['sent']} · 丢弃 {s['dropped']} · "
              f"失败 {s['error_count']} · 回复率 {s['reply_rate']}%")
        print(f"  token: 调用 {s['tokens']['calls']} 次 · 命中率 "
              f"{s['tokens']['cache_hit_rate']}% · 输出 {s['tokens']['output']}")
    ser = series(24)
    print("24 小时桶:", len(ser["labels"]), "有数据的:",
          [(ser["labels"][i], ser["total"][i]) for i in range(len(ser["labels"]))
           if ser["total"][i]])
    print("丢弃原因:", reasons(24)[:5])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
