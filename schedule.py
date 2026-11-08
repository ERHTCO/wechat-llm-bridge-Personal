"""作息时间表 + 学生课表 的查询逻辑。

两张表分开存：
  data/timetable.json  第几节课是几点
  data/classes.json    周几第几节上什么课、谁教

对外主要用 answer(text)，命中作息/课表类问题就返回一段现成回复，
不命中返回 None（交给模型自由回答）。
"""
import json
import os as _os
import re
from datetime import datetime
from pathlib import Path

# 打包成 exe 之后 __file__ 在 _internal\ 里，data/ 却在 exe 旁边。
# 所以必须跟 bridge.py / proactive.py 用同一套根目录，否则读不到课表，
# 然后一本正经地回答「今天没课」。
_HERE = Path(_os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
DATA = _HERE / "data"

WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10}

_warned = set()


def _load(name):
    path = DATA / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        # 打一行日志就够了：这个错误以前完全静默，表现成「今天没课」，
        # 从聊天记录里根本看不出是文件没读到。
        if name not in _warned:
            _warned.add(name)
            print(f"[课表] 读不到 {path}（{type(exc).__name__}: {exc}）", flush=True)
        return {}


def load_timetable():
    return _load("timetable.json")


def load_classes():
    return _load("classes.json")


def _to_min(hhmm):
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _fmt(minutes):
    return f"{minutes // 60}:{minutes % 60:02d}"


def periods():
    """{节次: (开始分钟, 结束分钟)}"""
    out = {}
    for name, when in (load_timetable().get("rows") or []):
        m = re.match(r"第(\d+)节课", name)
        if not m or "-" not in when:
            continue
        a, b = when.split("-", 1)
        s, e = _to_min(a), _to_min(b)
        if s is not None and e is not None:
            out[int(m.group(1))] = (s, e)
    return out


def timetable_text():
    data = load_timetable()
    rows = data.get("rows") or []
    if not rows:
        return ""
    lines = [f"{data.get('title', '作息时间表')}："]
    lines += [f"{name} {when}" for name, when in rows]
    return "\n".join(lines)


def classes_text():
    data = load_classes()
    days = data.get("days") or {}
    if not days:
        return ""
    pt = periods()
    lines = [f"{data.get('title', '课表')}（第几节对应的时间见作息表）："]
    for day in WEEK:
        items = days.get(day) or []
        if not items:
            lines.append(f"{day}：没课")
            continue
        seg = []
        for it in items:
            a, b = it.get("from"), it.get("to")
            span = f"第{a}节" if a == b else f"第{a}-{b}节"
            t1, t2 = pt.get(a), pt.get(b)
            if t1 and t2:
                span += f"（{_fmt(t1[0])}-{_fmt(t2[1])}）"
            seg.append(f"{span} {it.get('name', '')}（{it.get('teacher', '')}）")
        lines.append(f"{day}：" + "；".join(seg))
    return "\n".join(lines)


def _cn_to_int(s):
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + _CN_NUM.get(s[1:], 0)
    if s.endswith("十"):
        return _CN_NUM.get(s[:-1], 0) * 10
    if "十" in s:
        a, b = s.split("十", 1)
        return _CN_NUM.get(a, 0) * 10 + _CN_NUM.get(b, 0)
    return _CN_NUM.get(s)


def _which_day(text, now=None):
    """从话里判断问的是哪一天，默认今天。返回 (周一..周日, 标签)。"""
    now = now or datetime.now()
    today = now.weekday()          # 0=周一
    if "后天" in text:
        return WEEK[(today + 2) % 7], "后天"
    if "明天" in text:
        return WEEK[(today + 1) % 7], "明天"
    if "昨天" in text:
        return WEEK[(today - 1) % 7], "昨天"
    if "今天" in text or "今日" in text:
        return WEEK[today], "今天"
    m = re.search(r"(周|星期|礼拜)([一二三四五六日天])", text)
    if m:
        ch = m.group(2)
        idx = 6 if ch in ("日", "天") else _CN_NUM.get(ch, 1) - 1
        return WEEK[idx], WEEK[idx]
    return WEEK[today], "今天"


def _subject(day, period):
    for it in (load_classes().get("days") or {}).get(day) or []:
        if it.get("from", 0) <= period <= it.get("to", 0):
            return it
    return None


def _span_text(frm, to):
    pt = periods()
    a, b = pt.get(frm), pt.get(to)
    span = f"第{frm}节" if frm == to else f"第{frm}-{to}节"
    if a and b:
        span += f"（{_fmt(a[0])}-{_fmt(b[1])}）"
    return span


def _day_label(day, label):
    return day if label == day else f"{label}（{day}）"


def _day_list(day, label):
    items = (load_classes().get("days") or {}).get(day) or []
    who = _day_label(day, label)
    if not items:
        return f"{who}没课。"
    parts = []
    for it in items:
        parts.append(f"{_span_text(it['from'], it['to'])} "
                     f"{it.get('name', '')}（{it.get('teacher', '')}）")
    return f"{who}的课：" + "；".join(parts)


def _next_class(text, now):
    cur = now.hour * 60 + now.minute
    pt = periods()
    if not pt:
        return None
    ordered = sorted(pt.items())
    day, label = _which_day(text, now)
    if label not in ("今天", "明天", "后天"):
        return None

    # 问的是明天/后天的「下一节」：那天还没开始，直接给那天第一节
    if label in ("明天", "后天"):
        items = (load_classes().get("days") or {}).get(day) or []
        if not items:
            return f"{label}（{day}）没课。"
        first = min(items, key=lambda x: x.get("from", 99))
        return (f"{label}（{day}）第一节是{_span_text(first['from'], first['to'])} "
                f"{first.get('name', '')}（{first.get('teacher', '')}）。")

    for n, (s, e) in ordered:
        if s <= cur < e:
            left = e - cur
            subj = _subject(day, n)
            head = f"现在是第{n}节课（{_fmt(s)}-{_fmt(e)}），还有 {left} 分钟下课，"
            if subj:
                head += f"上的是{subj['name']}（{subj.get('teacher', '')}）。"
            else:
                head += "这节课没排课。"
            nxt = next(((n2, se) for n2, se in ordered if se[0] >= e), None)
            if nxt:
                n2, (s2, e2) = nxt
                subj2 = _subject(day, n2)
                tail = f"下一节是第{n2}节课（{_fmt(s2)}-{_fmt(e2)}）"
                tail += f"，{subj2['name']}（{subj2.get('teacher', '')}）。" if subj2 else "，这节没排课。"
                return head + tail
            return head + "今天的课到这就结束了。"

    nxt = next(((n2, se) for n2, se in ordered if se[0] > cur), None)
    if nxt:
        n2, (s2, e2) = nxt
        subj = _subject(day, n2)
        txt = f"下一节是第{n2}节课（{_fmt(s2)}-{_fmt(e2)}）"
        txt += f"，{subj['name']}（{subj.get('teacher', '')}）。" if subj else "，这节没排课。"
        return txt
    return "今天的课已经结束了。"


def answer(text, now=None):
    """作息 / 课表类问题统一入口。命中返回字符串，否则 None。"""
    t = (text or "").strip()
    if not t:
        return None
    now = now or datetime.now()
    days = load_classes().get("days") or {}
    pt = periods()

    # 课表没读到就闭嘴。返回 None 会让 bridge 走它自己的作息表兜底，
    # 或者交给模型——这两种都比斩钉截铁地说「今天没课」强。
    if not days:
        return None

    # 1) 下一节 / 现在第几节
    if any(k in t for k in ("下一节", "下节课", "下节", "接下来", "现在第几节",
                            "现在上到哪", "还剩几分钟", "还有几分钟下课",
                            "下堂", "下一堂", "现在上什么")):
        r = _next_class(t, now)
        if r:
            return r

    # 2) 某天有什么课 / 课表
    if any(k in t for k in ("什么课", "课表", "有什么课", "上什么", "有几节课",
                            "有课吗", "有课么", "上课吗", "有课")):
        if "周末" in t:
            return (_day_list("周六", "周六") + "\n" +
                    _day_list("周日", "周日"))
        day, label = _which_day(t, now)
        m = re.search(r"第\s*([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*节", t)
        if m:
            n = _cn_to_int(m.group(1))
            if n:
                subj = _subject(day, n)
                span = _span_text(n, n)
                who = _day_label(day, label)
                if subj:
                    return f"{who}{span}：{subj['name']}（{subj.get('teacher', '')}）。"
                return f"{who}{span}：没排课。"
        return _day_list(day, label)

    # 3) 第X节课几点（时间为主，顺带给今天上什么）
    m = re.search(r"第\s*([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*节", t)
    if m:
        n = _cn_to_int(m.group(1))
        if n:
            if not pt:
                return None
            if n not in pt:
                return f"没有第{n}节课，一天一共 {max(pt)} 节。"
            s, e = pt[n]
            day, label = _which_day(t, now)
            subj = _subject(day, n)
            base = f"第{n}节课：{_fmt(s)}-{_fmt(e)}"
            who = _day_label(day, label)
            if subj:
                return f"{base}\n{who}这节是：{subj['name']}（{subj.get('teacher', '')}）。"
            return f"{base}\n{who}这节没排课。"

    # 4) 整张作息表
    if any(k in t for k in ("作息", "时间表", "几点上课", "上课时间", "一天的安排")):
        return timetable_text()

    # 5) 具体节点
    for name, when in (load_timetable().get("rows") or []):
        key = name.replace("（下午）", "")
        if key and key in t:
            if key == "预备上课":
                both = [w for n2, w in (load_timetable().get("rows") or [])
                        if n2.startswith("预备上课")]
                return "预备上课：" + "、".join(both)
            return f"{name}：{when}"
    return None
