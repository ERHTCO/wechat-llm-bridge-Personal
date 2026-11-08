"""提醒：把「几点提醒我什么」变成一条真会响的记录。

存在的理由很具体：2026-10-26 11:47，它回了「好，12点05分我喊用户一声」，
而系统里根本没有提醒这回事，12:05 什么都没发生。模型顺口许了一个系统给不出的承诺。

要么别答应，要么真做到。这里选后者：

    parse()     把一句话拆成「几点 / 提醒什么」，纯正则，不花模型的钱
    add()       存进 data/reminders.json
    fire_due()  到点了发一条，发完标记，桥的循环每分钟调一次

模型只负责「到点了怎么说」。时间计算、到点判断、发没发出去，全是代码的事。
"""
import json
import os as _os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(_os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
sys.path.insert(0, str(_HERE))

import backends
import outbox

# add() 在 HTTP 线程、fire_due() 在心跳线程，两边都是「读整个文件 → 改 → 写回」。
# 没有这把锁时，提醒到点那十几秒里新增的提醒会被旧快照整表覆盖（模型已经跟
# 用户说「记下了」，到点却什么都没发生）。
_LOCK = threading.RLock()

MAX_LATE_S = 1800          # 晚了半小时以上就不补发了，标 missed
MAX_AHEAD_H = 72           # 超过三天的提醒不收
MAX_PER_DAY = 10


def _root(root):
    return Path(root) if root else _HERE


def _path(root=None):
    return _root(root) / "data" / "reminders.json"


# ---------------------------------------------------------------- 解析

INTENT = re.compile(r"提醒我|提醒一下|提醒下|叫我一声|叫我一下|叫我|喊我|记得提醒|别忘了提醒")

_CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
       "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}

_DAY_WORDS = [("大后天", 3), ("后天", 2), ("明早", 1), ("明晚", 1), ("明天", 1),
              ("今天", 0), ("今早", 0), ("今晚", 0), ("今儿", 0)]

_PERIOD = {"凌晨": 0, "早上": 0, "早晨": 0, "上午": 0, "中午": 12,
           "下午": 12, "傍晚": 12, "晚上": 12, "夜里": 12, "晚": 12}

_NUM = r"\d{1,2}|[一二两三四五六七八九十]{1,3}"


def _num(s):
    s = str(s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in _CN:
        return _CN[s]
    if "十" in s:
        a, b = s.split("十", 1)
        return _CN.get(a, 1) * 10 + (_CN.get(b, 0) if b else 0)
    return _CN.get(s)


def _day_offset(text):
    for word, off in _DAY_WORDS:
        if word in text:
            return off, word
    return None, ""


def _period_hour(text):
    """找到『下午/晚上』这类时段词，返回 0 或 12。"""
    for word, base in _PERIOD.items():
        if word in text:
            return base
    return None


def _period_word(text):
    """返回命中的那个时段词（`_period_hour` 把「中午」和「晚上」都归成 12，
    但「中午12点」是中午、「晚上12点」是午夜，得靠这个词再分一次）。"""
    for word in _PERIOD:
        if word in text:
            return word
    return None


# 「X 叫我 / X 喊我」是转述（我妈叫我起床），不是让我提醒。
# 只看意图词前面 8 个字，免得把「记得叫我」这种正常请求误杀。
_THIRD_PARTY = re.compile(
    r"(我[爸妈哥姐弟妹叔婶爷奶儿]|[他她它]们?|老师|同学|朋友|室友|同事|老板|医生|"
    r"护士|有人|谁|家里|家人|对象|男朋友|女朋友)")


def _intent_match(text):
    """找提醒意图；转述句（第三方主语 + 叫我/喊我）不算。"""
    m = INTENT.search(text)
    if not m:
        return None
    word = m.group(0)
    if word.startswith("叫我") or word.startswith("喊我"):
        if _THIRD_PARTY.search(text[:m.start()][-8:]):
            return None
    return m


def _absolute(text, now):
    """解析『几点』。返回 (datetime, span) 或 (None, None)。"""
    hour = minute = None
    span = None

    m = re.search(rf"({_NUM})\s*[:：]\s*(\d{{2}})", text)
    if m:
        hour = _num(m.group(1))
        minute = int(m.group(2))
        span = m.span()
    else:
        m = re.search(rf"({_NUM})\s*[点时]\s*(半|一刻|三刻|\d{{1,2}})?\s*分?", text)
        if not m:
            return None, None
        hour = _num(m.group(1))
        tail = m.group(2)
        if tail == "半":
            minute = 30
        elif tail == "一刻":
            minute = 15
        elif tail == "三刻":
            minute = 45
        elif tail:
            minute = int(tail)
        else:
            minute = 0
        span = m.span()

    if hour is None or hour > 24 or minute is None or minute > 59:
        return None, None

    # 时段词：下午三点 → 15 点；凌晨十二点 → 0 点
    base = _period_hour(text)
    midnight = False
    if base is not None:
        word = _period_word(text) or ""
        if base == 12 and hour == 12 and word in ("晚上", "夜里", "晚", "傍晚"):
            # 「晚上十二点 / 今晚12点」是 0 点，而且是**次日** 0 点。
            # 以前只判 hour<12，12 点原样留着 → 记成中午 12:00，整整晚 12 小时。
            hour = 0
            midnight = True
        elif base == 12 and hour < 12:
            hour += 12
        elif base == 0 and hour == 12:
            hour = 0
    if hour == 24:
        hour = 0

    off, _ = _day_offset(text)
    day = now.date() + timedelta(days=off or 0)
    try:
        at = datetime(day.year, day.month, day.day, hour, minute)
    except ValueError:
        return None, None
    if midnight and off is not None:
        # 「明晚 12 点」说的是那一天的 24:00，也就是再后一天的 0 点
        at += timedelta(days=1)
    if off is None and at <= now:
        at += timedelta(days=1)      # 「12点」说的时候刚过 → 明天的 12 点
    return at, span


def _class_end(now, period=None):
    """下课时间：正在上的那节，或者下一个有课的节次。"""
    try:
        import schedule
        pt = schedule.periods()
    except Exception:
        return None
    if not pt:
        return None
    if period and period in pt:
        end = pt[period][1]
        return now.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    cur = now.hour * 60 + now.minute
    for n, (s, e) in sorted(pt.items()):
        if s <= cur < e:
            return now.replace(hour=e // 60, minute=e % 60, second=0, microsecond=0)
    for n, (s, e) in sorted(pt.items()):
        if s > cur:
            return now.replace(hour=e // 60, minute=e % 60, second=0, microsecond=0)
    return None


def _clean_text(rest):
    s = rest or ""
    for word in ("提醒我一下", "提醒我", "提醒一下", "提醒下", "叫我一声", "叫我一下",
                 "叫我", "喊我", "记得", "别忘了", "帮忙", "帮我", "给我", "一下",
                 "的时候", "记得提醒", "提醒"):
        s = s.replace(word, "")
    # 时间和日期词不能留在要发的内容里
    for word, _off in _DAY_WORDS:
        s = s.replace(word, "")
    for word in _PERIOD:
        s = s.replace(word, "")
    s = re.sub(r"[，。,.!！?？~～、\s]+", " ", s).strip()
    s = re.sub(r"^(要|在|于|是|把|跟)\s*", "", s)
    return s[:40].strip()


def parse(text, now=None):
    """拆一句话。

    返回 (状态, 信息)：
      ("ok",      {"at": datetime, "text": "...", "kind": "..."})
      ("no_time", {"raw": "..."})    有提醒意图，但没听懂时间
      (None, {})                     不是提醒请求
    """
    now = now or datetime.now()
    t = (text or "").strip()
    if not t or not _intent_match(t):
        return None, {}

    at = span = None
    kind = "at"

    # 1) 相对时间
    m = re.search(rf"({_NUM})\s*个?\s*(?:分钟|分)\s*(?:以)?后", t)
    if m and _num(m.group(1)):
        at, span, kind = now + timedelta(minutes=_num(m.group(1))), m.span(), "relative"
    if at is None and re.search(r"半\s*个?\s*小时\s*(?:以)?后", t):
        m = re.search(r"半\s*个?\s*小时\s*(?:以)?后", t)
        at, span, kind = now + timedelta(minutes=30), m.span(), "relative"
    if at is None:
        m = re.search(rf"({_NUM})\s*个?\s*(?:小时|钟头)\s*(?:以)?后", t)
        if m and _num(m.group(1)):
            at, span, kind = now + timedelta(hours=_num(m.group(1))), m.span(), "relative"

    # 2) 下课
    if at is None and "下课" in t:
        pm = re.search(rf"第\s*({_NUM})\s*节", t)
        period = _num(pm.group(1)) if pm else None
        at = _class_end(now, period)
        if at is not None:
            m = (re.search(rf"第\s*(?:{_NUM})\s*节\s*(?:课)?\s*(?:下课|结束)", t)
                 or re.search(r"下课", t))
            span = m.span() if m else (t.find("下课"), t.find("下课") + 2)
            kind = "class_end"

    # 3) 几点
    if at is None:
        at, span = _absolute(t, now)
        kind = "at"

    if at is None:
        return "no_time", {"raw": t}

    if at <= now:
        if kind == "class_end":
            return "no_time", {"raw": t}
        at += timedelta(days=1)

    # 太长不看
    if at - now > timedelta(hours=MAX_AHEAD_H):
        return "no_time", {"raw": t, "too_far": True}

    rest = t[:span[0]] + " " + t[span[1]:] if span else t
    body = _clean_text(rest)
    if not body:
        body = "下课了" if kind == "class_end" else "到点了"
    return "ok", {"at": at, "text": body, "kind": kind, "raw": t}


# ---------------------------------------------------------------- 存取

def load(root=None):
    p = _path(root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save(items, root=None):
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 只留 7 天内的，避免文件无限长
    cutoff = time.time() - 7 * 86400
    items = [it for it in items if float(it.get("at") or 0) >= cutoff or not it.get("done")]
    # 临时文件名带进程/线程号：固定 .tmp 在并发写时会让后一个 replace 抛 FileNotFoundError
    tmp = p.with_name(f"{p.name}.{_os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def add(root, target, at, text, kind="at", src=""):
    """记一条。同一分钟、同一内容、同一目标视为重复（模型偶尔会重发）。"""
    with _LOCK:
        return _add_locked(root, target, at, text, kind, src)


def _add_locked(root, target, at, text, kind, src):
    items = load(root)
    ts = at.timestamp()
    for it in items:
        if (not it.get("done") and it.get("target") == target
                and abs(float(it.get("at") or 0) - ts) < 60
                and it.get("text") == text):
            return it

    today = datetime.now().strftime("%Y-%m-%d")
    count = sum(1 for it in items
                if datetime.fromtimestamp(float(it.get("at") or 0)).strftime("%Y-%m-%d") == today)
    if count >= MAX_PER_DAY:
        return None

    it = {
        "id": uuid.uuid4().hex[:8],
        "created": time.time(),
        "target": target,
        "at": ts,
        "text": text,
        "kind": kind,
        "src": (src or "")[:120],
        "done": "",
        "tries": 0,
    }
    items.append(it)
    save(items, root)
    return it


def pending(root=None, target=None):
    return [it for it in load(root)
            if not it.get("done") and (target is None or it.get("target") == target)]


def at_text(at, now=None):
    now = now or datetime.now()
    if isinstance(at, (int, float)):
        at = datetime.fromtimestamp(at)
    delta = (at.date() - now.date()).days
    head = {0: "今天", 1: "明天", 2: "后天"}.get(delta)
    if head is None:
        head = f"{at.month}月{at.day}日"
    return f"{head} {at:%H:%M}"


def describe(items, now=None):
    if not items:
        return ""
    return "；".join(f"{at_text(it.get('at'), now)} 提醒「{it.get('text')}」"
                    for it in items[:3])


# ---------------------------------------------------------------- 到点发送

def wording(cfg, root, item):
    """到点了，用平时的口吻说一句。模型挂了就退回一句朴素的。"""
    fallback = (item.get("text") or "到点了").rstrip("。") + "。"
    try:
        backend = backends.build(cfg, _root(root))
        if not hasattr(backend, "chat"):
            return fallback
        me = cfg.get("my_name") or "我"
        persona = (cfg.get("persona") or "自然口语").strip()
        at = at_text(item.get("at"))
        prompt = (
            f"你叫「{me}」，平时说话是这种风格：{persona}。\n"
            f"现在是 {at}，你之前答应到点提醒对方一件事：{item.get('text')}。\n"
            "用你平时的口吻提醒一句。要求：一到两句，口语，可以带一个括号里的动作；"
            "不要写「提醒事项」「系统」「日程」这类词，不要解释为什么提醒，不要提自己是 AI。"
        )
        out = backend.chat(prompt, model=(cfg.get("proactive") or {}).get("decide_model")
                           or "deepseek-chat", temperature=0.9, max_tokens=160)
        return backends.first_line(out or "", max_lines=2, max_chars=120) or fallback
    except Exception:
        return fallback


def _outbox_send(root):
    """到点提醒也走唯一的发送出口。

    kind="reminder" 不占主动搭话的每日额度——这个差别以前只活在代码里，
    现在它是 outbox 里一条显式的规则。
    """
    def send(target, body):
        return outbox.deliver(target, body, "reminder", root=root)
    return send


def _apply_result(root, item_id, fields):
    """只改一条：重读文件再合并。

    以前 fire_due 是整轮跑完再拿内存里的旧快照 save()，把发送这几秒到十几秒里
    add() 存进来的新提醒整表覆盖掉；反过来也会把刚发出去的 done 改回空、
    下一分钟再发一遍。现在每条发完就单独落盘，且只动自己那一条。
    """
    if not item_id:
        return
    with _LOCK:
        cur = load(root)
        for c in cur:
            if c.get("id") == item_id:
                c.update(fields)
                break
        save(cur, root)


def fire_due(cfg, root=None, now=None, sender=None, worder=None):
    """把到点的发出去。返回说明列表（给日志用）。

    发送（调模型写措辞 + 真发）在锁外做，落盘用 _apply_result 合并 ——
    既不长时间占着锁，也不会覆盖别人刚写进来的提醒。
    """
    root = _root(root)
    now = now or datetime.now()
    items = load(root)
    out = []
    for it in items:
        if it.get("done"):
            continue
        ts = float(it.get("at") or 0)
        if ts > now.timestamp():
            continue
        late = now.timestamp() - ts
        if late > MAX_LATE_S:
            _apply_result(root, it.get("id"),
                          {"done": "missed", "fired_ts": now.timestamp()})
            out.append(f"{it.get('target')}：错过「{it.get('text')}」"
                       f"（晚了 {int(late // 60)} 分钟，不补发）")
            continue
        body = (worder or wording)(cfg, root, it)
        try:
            ok, how = (sender or _outbox_send(root))(it.get("target"), body)
        except Exception as exc:
            ok, how = False, f"{type(exc).__name__}: {exc}"
        if ok:
            _apply_result(root, it.get("id"),
                          {"done": "fired", "fired_ts": now.timestamp(), "sent": body})
            out.append(f"{it.get('target')}：{body}")
        else:
            tries = int(it.get("tries") or 0) + 1
            fields = {"tries": tries}
            if tries >= 3:
                fields["done"] = "failed"
            _apply_result(root, it.get("id"), fields)
            out.append(f"{it.get('target')}：发送失败（第 {tries} 次）{how}")
    return out


def main():
    import argparse

    ap = argparse.ArgumentParser(description="提醒：看看现在记着什么 / 试一下解析")
    ap.add_argument("--list", action="store_true", help="还没到点的提醒")
    ap.add_argument("--past", action="store_true", help="发过的、错过的")
    ap.add_argument("--parse", metavar="一句话", help="只解析，不记")
    ap.add_argument("--at", help="假装现在是这个时间，如 2026-10-26 11:47")
    ap.add_argument("--fire", action="store_true", help="立刻跑一次到点检查（会真的发）")
    args = ap.parse_args()

    now = datetime.now()
    if args.at:
        now = datetime.strptime(args.at, "%Y-%m-%d %H:%M")

    if args.parse:
        status, info = parse(args.parse, now)
        if status is None:
            print("不是提醒请求")
        elif status == "no_time":
            print(f"有提醒意图，但没听懂时间：{info}")
        else:
            print(f"记下了：{at_text(info['at'], now)} 提醒「{info['text']}」（{info['kind']}）")
        return 0

    if args.fire:
        cfg = json.loads((_HERE / "config.json").read_text(encoding="utf-8-sig"))
        for line in fire_due(cfg, _HERE, now):
            print(line)
        return 0

    items = load(_HERE)
    rows = [it for it in items if (bool(it.get("done")) == bool(args.past))]
    if not rows:
        print("（空）")
        return 0
    for it in rows:
        when = at_text(it["at"], now)
        state = {"": "待发", "fired": "已发", "missed": "错过", "failed": "失败"}.get(
            it.get("done") or "", it.get("done"))
        print(f"{when}  {it.get('target'):<8} {state:<4} {it.get('text')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
