"""微信桥：把收到的消息过一遍规则，交给后端生成回复。

它不碰微信本身——真实客户端（hook / 官方回调 / 模拟器）只要 POST /in 就行。
这样微信侧怎么接都可以换，规则和回复逻辑只有一份。
"""
import base64
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import backends
import bili_sub
import hooks
import memory
import proactive
import remind
import vault
import websearch

import os as _os
ROOT = Path(_os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
def _load_config():
    """读 config.json。**文件不存在**时按空配置启动（新克隆的仓库就是这种状态：
    只有 config.example.json），但要说一句，别让人对着「什么都不回」干瞪眼。
    文件存在却读不动时仍然抛 —— 那是真坏了，静默降级比崩更难查。
    """
    path = ROOT / "config.json"
    if not path.exists():
        print("[bridge] 没有 config.json：先把它复制出来（config.example.json → config.json），"
              "现在按空配置启动，不会回任何消息", flush=True)
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


CONFIG = _load_config()
DB_PATH = ROOT / "data" / "chat.db"
LOCK = threading.Lock()
BILI_LOCK = threading.Lock()      # 聊天驱动收藏：每人冷却
BILI_LAST = {}                    # wxid -> 上次触发时间戳
DAILY_LOCK = threading.Lock()     # 日记：同一时间只跑一个
TOKEN = ""   # 本机接口的共享密钥，main() 里初始化
# 搜索限流：每小时最多几次，防止某句话触发一堆调用烧钱
SEARCH_LIMITER = websearch.RateLimiter(
    int((CONFIG.get("search") or {}).get("max_per_hour", 30)))

# 扩展点登记：记忆挂在这两个阶段上；以后加功能不用再改 handle()
hooks.register("before_reply", "memory", memory.hook_before)
hooks.register("after_reply", "memory", memory.hook_after)


def write_config_atomic(cfg):
    """原子写 config.json：先写临时文件再替换。

    直接 write_text 写到一半崩溃/断电，会留下截断的 JSON，
    下次启动 json.loads 直接崩，连 token 一起没。
    """
    tmp = ROOT / "config.json.tmp"
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, ROOT / "config.json")


def ensure_token():
    """取（或首次生成）接口鉴权 token。

    没有 token 时，浏览器里任意网页都能 no-cors POST /in 伪造消息，
    甚至 POST /api/proactive/send 直接把任意文本发进真人微信。
    """
    global TOKEN
    tok = (CONFIG.get("api_token") or "").strip()
    if not tok:
        tok = secrets.token_hex(16)
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
        cfg["api_token"] = tok
        write_config_atomic(cfg)
        reload_config()
    TOKEN = tok
    return tok


def reload_config():
    """从磁盘重读配置，热更新到内存（改完不用重启桥）。

    整体替换引用，不要 clear 再 update：中间那段窗口里并发请求会读到空配置。
    token 也要跟着刷：否则手改 config.json 后调 /api/reload 会全站 403。
    """
    global CONFIG, TOKEN
    fresh = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    CONFIG = fresh
    tok = (fresh.get("api_token") or "").strip()
    if tok:
        TOKEN = tok
    return CONFIG

SCHEMA = """
create table if not exists messages(
  id integer primary key autoincrement,
  wxid text not null, role text not null, text text not null,
  ts real not null, msg_key text
);
create index if not exists idx_msg_wxid on messages(wxid, ts);
create table if not exists state(k text primary key, v text);
create table if not exists events(
  id integer primary key autoincrement, ts real, wxid text,
  text text, action text, reason text, reply text, elapsed_ms integer
);
create table if not exists usage(
  id integer primary key autoincrement, ts real, wxid text, model text,
  hit integer, miss integer, out_tokens integer
);
"""


_schema_ready = False


def connect():
    """开一个数据库连接。

    schema 只建一次（以前每次 connect 都 executescript，等于每个请求都要拿写锁，
    并发时就是 database is locked）；顺便开 WAL 和 busy_timeout。
    """
    global _schema_ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=8000")
    except Exception:
        pass
    if not _schema_ready:
        conn.executescript(SCHEMA)
        _schema_ready = True
    return conn


def state_get(conn, key, default=None):
    row = conn.execute("select v from state where k=?", (key,)).fetchone()
    return row[0] if row else default


def state_set(conn, key, value):
    conn.execute("insert into state(k,v) values(?,?) on conflict(k) do update set v=excluded.v", (key, value))


def msg_key(payload):
    """同一条微信消息可能被重发，用原始 id 去重；没有 id 就退回内容指纹。

    指纹里必须带上图片本身的哈希：连发两张图时文本都是「[对方发来一张图片]」，
    只按文本去重会把第二张当重复丢掉。
    """
    raw = payload.get("msg_id")
    if not raw:
        raw = f"{payload.get('roomid','')}|{payload.get('from')}|{payload.get('text')}"
        images = payload.get("images") or []
        if images:
            raw += "|" + hashlib.sha1(str(images[0])[:2000].encode("utf-8")).hexdigest()
    return hashlib.sha1(str(raw).encode("utf-8")).hexdigest()


def history_rows(conn, wxid, turns):
    """最近 turns 条消息的原始行（带 id）。记忆溯源要用 id，别只有正文。

    按位置取列，不依赖 row_factory —— connect() 没设 Row，用 dict(row) 会炸。
    """
    rows = conn.execute(
        "select id, role, text, ts from messages where wxid=? order by ts desc limit ?",
        (wxid, turns)
    ).fetchall()
    return [{"id": r[0], "role": r[1], "text": r[2], "ts": r[3]}
            for r in reversed(rows)]


def history(conn, wxid, turns):
    """给提示词用的 (说话人, 正文) 列表。"""
    return [("我" if r["role"] == "me" else "对方", r["text"])
            for r in history_rows(conn, wxid, turns)]


def rate_wait(last_ts, now, min_interval_s, cap=3.0):
    """两条消息之间至少要隔 min_interval_s。返回还要等多少秒（封顶 cap）。

    以前这里直接 `return "drop"` —— 而 watcher 的游标在 POST 之前就前移了，
    拿到 drop 只打一行日志、不回滚，这条消息就**永久丢了**。
    对方在机器人「正在输入」时补的那句话正好落在窗口里，最常被吃掉。
    限流的目的是别连发太快，等一下就好，没必要丢消息。
    """
    try:
        gap = float(min_interval_s or 0) - (float(now) - float(last_ts or 0))
    except (TypeError, ValueError):
        return 0.0
    if gap <= 0:
        return 0.0
    return min(gap, float(cap))


def decide(payload):
    """返回 (action, reason)。只做规则判断，不产生回复。"""
    cfg = CONFIG
    text = (payload.get("text") or "").strip()
    wxid = payload.get("from") or ""
    roomid = payload.get("roomid") or ""

    if not text:
        return "drop", "empty_text"

    conn = connect()
    try:
        if state_get(conn, "paused") == "1":
            return "drop", "paused_by_owner"

        if roomid:
            room = (cfg.get("rooms") or {}).get(roomid)
            if room is None:
                return "drop", "room_not_allowed"
            if room.get("require_at") and not payload.get("at_me"):
                return "drop", "no_mention"
        elif wxid not in (cfg.get("whitelist") or []):
            if not cfg.get("private_all"):
                return "drop", "not_whitelisted"

        key = msg_key(payload)
        window = cfg.get("dedupe_window_s", 20)
        dup = conn.execute(
            "select 1 from messages where msg_key=? and ts>?", (key, time.time() - window)
        ).fetchone()
        if dup:
            return "drop", "duplicate"

        rl = cfg.get("rate_limit") or {}
        now = time.time()
        last = conn.execute(
            "select max(ts) from messages where wxid=? and role='me'", (wxid,)
        ).fetchone()[0]
        if last and now - last < rl.get("min_interval_s", 0):
            # 以前这里是 return "drop", "rate_limited_interval"：游标已经前移，
            # 这条消息就永久丢了。改成等一下再发（最多 3 秒）。
            wait = rate_wait(last, now, rl.get("min_interval_s", 0))
            if wait > 0:
                print(f"[bridge] 距上一条太近，等 {wait:.1f} 秒再回（不丢消息）", flush=True)
                time.sleep(wait)
                now = time.time()
        sent = conn.execute(
            "select count(*) from messages where wxid=? and role='me' and ts>?", (wxid, now - 60)
        ).fetchone()[0]
        if sent >= rl.get("max_per_minute", 999):
            return "drop", "rate_limited_per_minute"

        return "send", ""
    finally:
        conn.close()


LOW_MOOD_WORDS = (
    # 情绪
    "不好", "难受", "累", "哭", "生气", "烦", "压力", "焦虑", "失望", "难过",
    "崩溃", "吵架", "分手", "委屈", "郁闷", "心累", "自闭", "抑郁", "想哭",
    "被骂", "挨骂", "失恋", "去世", "出事", "吵起来",
    # 生病 / 受伤
    "病", "疼", "痛", "发烧", "感冒", "头疼", "头痛", "胃疼", "肚子疼", "拉肚子",
    "受伤", "住院", "手术", "打针", "吃药", "失眠", "睡不着", "过敏", "发炎",
)
KAOMOJI_MARKS = ("ᗜ", "💤", "❗", "💧", "✨")


def emoji_allowed(conn, wxid, text):
    """决定这一条要不要带表情。

    三条规则：
    1. 对方情绪不好/在说正事 -> 不带
    2. 上一条回复已经带过 -> 不带（避免连着用）
    3. 其余情况按概率偶尔带
    """
    if any(w in text for w in LOW_MOOD_WORDS):
        return False
    row = conn.execute(
        "select text from messages where wxid=? and role='me' order by id desc limit 1",
        (wxid,),
    ).fetchone()
    if row and any(m in (row[0] or "") for m in KAOMOJI_MARKS):
        return False
    return random.random() < 0.4


def list_images():
    """列出 images/ 里可发的图，给模型挑。"""
    folder = ROOT / "images"
    if not folder.is_dir():
        return ""
    names = sorted(
        p.name for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")
    )
    return "\n".join(f"- images/{n}" for n in names[:30])


TT_PATH = ROOT / "data" / "timetable.json"
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10}


def load_timetable():
    try:
        data = json.loads(TT_PATH.read_text(encoding="utf-8"))
        return data
    except Exception:
        return {}


def timetable_text():
    """给提示词用的纯文本：作息时间表 + 课表。"""
    import schedule as _sched

    parts = []
    try:
        tt = _sched.timetable_text()
        if tt:
            parts.append(tt)
        cc = _sched.classes_text()
        if cc:
            parts.append(cc)
    except Exception:
        pass
    if parts:
        return "\n\n".join(parts)

    data = load_timetable()
    rows = data.get("rows") or []
    if not rows:
        return ""
    lines = [data.get("title", "作息时间表") + "："]
    lines += [f"{name} {when}" for name, when in rows]
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


def _to_min(hhmm):
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _fmt(minutes):
    return f"{minutes // 60}:{minutes % 60:02d}"


def next_class_answer(text, now_min=None):
    """按当前时间回答「下一节几点」「现在第几节」这类问题。"""
    keys = ("下一节", "下节课", "下节", "接下来", "现在第几节",
            "现在上到哪", "还剩几分钟", "还有几分钟下课", "下堂", "下一堂")
    if not any(k in text for k in keys):
        return None

    data = load_timetable()
    periods = []
    for name, when in (data.get("rows") or []):
        m = re.match(r"第(\d+)节课", name)
        if not m:
            continue
        if "-" in when:
            a, b = when.split("-", 1)
            s, e = _to_min(a), _to_min(b)
            if s is not None and e is not None:
                periods.append((int(m.group(1)), s, e))
    if not periods:
        return None
    periods.sort()

    from datetime import datetime as _dt
    now = _dt.now()
    cur = now.hour * 60 + now.minute if now_min is None else now_min

    # 正在上课
    for n, s, e in periods:
        if s <= cur < e:
            left = e - cur
            nxt = next((p for p in periods if p[1] >= e), None)
            head = f"现在是第{n}节课（{_fmt(s)}-{_fmt(e)}），还有 {left} 分钟下课。"
            if nxt:
                return head + f"下一节是第{nxt[0]}节课，{_fmt(nxt[1])} 开始。"
            return head + "今天的课已经上完了。"

    nxt = next((p for p in periods if p[1] > cur), None)
    if nxt:
        wait = nxt[1] - cur
        return (f"下一节是第{nxt[0]}节课，{_fmt(nxt[1])}-{_fmt(nxt[2])}"
                f"（还有 {wait} 分钟）。")
    return "今天的课已经结束了，明天第1节课 8:10 开始。"


def normalize_timetable_text(text):
    """把几种常见说法归一成 schedule.answer 认得的句式。

    实测「今天第几节课」「第几节课了」「今天上啥课」原本都拦不住，会漏给模型，
    模型读表时容易串节次。归一化之后这些走查表短路，不再由模型保证事实。

    关键：**不能**把「明天第几节」也归一到「现在第几节」——那会答错天。
    """
    s = (text or "").strip()
    if not s:
        return s
    other_day = re.search(
        r"明天|后天|大后天|昨天|前天|下周|上周|这周|周[一二三四五六日天]|"
        r"星期[一二三四五六日天]|礼拜[一二三四五六日天]|上午|下午|晚上|早上",
        s,
    )
    if re.search(r"第几节", s) and not other_day:
        return "现在第几节"
    if re.search(r"今天.*(?:啥课|什么课|什么课程|几节课|的课)", s):
        return "今天有什么课"
    if re.search(r"(?:啥课|什么课).*今天", s):
        return "今天有什么课"
    return s


def timetable_answer(text):
    """作息类问题直接查表回答，避免模型编时间。命中返回字符串，否则 None。"""
    # 新逻辑在 schedule.py：两张表对照，还带「下一节」「明天有什么课」
    import schedule as _sched

    text = normalize_timetable_text(text)

    try:
        hit = _sched.answer(text)
        if hit:
            return hit
    except Exception:
        pass

    data = load_timetable()
    rows = data.get("rows") or []
    if not rows:
        return None
    t = (text or "").strip()
    if not t:
        return None

    # 「下一节 / 现在第几节」要先按当前时间算，放在前面
    nxt = next_class_answer(t)
    if nxt:
        return nxt

    periods = {}
    for name, when in rows:
        m = re.match(r"第(\d+)节课", name)
        if m:
            periods[int(m.group(1))] = when

    # 第X节课 / 第X节
    m = re.search(r"第\s*([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*节", t)
    if m:
        n = _cn_to_int(m.group(1))
        if n:
            if n in periods:
                return f"{data.get('title', '作息时间表')}\n第{n}节课：{periods[n]}"
            return f"没有第{n}节课，一天一共 {max(periods)} 节。"

    # 整张表
    if any(k in t for k in ("作息", "时间表", "几点上课", "上课时间", "一天的安排")):
        return timetable_text()

    # 具体节点
    for name, when in rows:
        key = name.replace("（下午）", "")
        if key and key in t:
            if key == "预备上课":
                both = [w for n2, w in rows if n2.startswith("预备上课")]
                return "预备上课：" + "、".join(both)
            return f"{name}：{when}"
    return None


def stats(hours=24):
    """近 N 小时的运行统计：回复了多少、为什么丢弃、模型失败多少。

    聚合逻辑搬去了 stats.py —— 界面进程要用同一份数据，总不能为了几个数
    就把整个桥（配置、钩子、记忆、订阅）都 import 起来。这里只做转调。
    """
    import stats as stats_mod

    return stats_mod.summary(hours)


def context_stats():
    """列出每个联系人攒了多少条对话，供管理页展示。"""
    conn = connect()
    try:
        rows = conn.execute(
            "select wxid, count(*) as n, max(ts) from messages group by wxid order by n desc"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for wxid, n, last_ts in rows:
        out.append({
            "wxid": wxid,
            "count": n,
            "last": datetime.fromtimestamp(last_ts).strftime("%m-%d %H:%M") if last_ts else "",
        })
    return out


def clear_context(wxid=None):
    """清空上下文。wxid 为空则清空全部联系人。返回删掉的行数。"""
    conn = connect()
    try:
        if wxid:
            cur = conn.execute("delete from messages where wxid=?", (wxid,))
        else:
            cur = conn.execute("delete from messages")
        deleted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return deleted


def save_settings(data):
    """保存管理页提交的设置：提示词写 prompt.txt，其余写 config.json。"""
    changed = []

    if "prompt" in data:
        text = (data.get("prompt") or "").strip()
        if text:
            (ROOT / "prompt.txt").write_text(text + "\n", encoding="utf-8")
            changed.append("prompt.txt")

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    for key in ("my_name", "persona"):
        if key in data:
            cfg[key] = str(data[key]).strip()
            changed.append(key)
    if "private_all" in data:
        cfg["private_all"] = bool(data["private_all"])
        changed.append("private_all")

    if "rooms_text" in data:
        raw = (data.get("rooms_text") or "").strip()
        if raw:
            rooms = json.loads(raw)          # 格式错就抛出去，前端会显示
            if not isinstance(rooms, dict):
                raise ValueError("群白名单必须是 JSON 对象")
            cfg["rooms"] = rooms
        else:
            cfg["rooms"] = {}
        changed.append("rooms")

    if "daily_max" in data:
        pro = cfg.setdefault("proactive", {})
        pro["daily_max"] = int(data["daily_max"])
        changed.append("daily_max")

    if "proactive" in data:
        pro = cfg.setdefault("proactive", {})
        incoming = data.get("proactive") or {}
        nums = ("daily_max", "min_gap_min", "quiet_min", "heartbeat_min",
                "silent_backoff_min", "silent_backoff_max_min", "idle_hours")
        for key in ("enabled", "targets", "daily_max", "window",
                    "daily_at", "idle_hours", "template", "hint",
                    "mode", "min_gap_min", "quiet_min", "heartbeat_min",
                    "silent_backoff_min", "silent_backoff_max_min", "decide_model"):
            if key not in incoming:
                continue
            val = incoming[key]
            if key in nums:
                # 前端可能传字符串，留着后面比较时才会炸，这里先转成数字
                try:
                    val = float(val or 0)
                    if key == "daily_max":
                        val = int(val)
                except (TypeError, ValueError):
                    continue
            pro[key] = val
        changed.append("proactive")

    write_config_atomic(cfg)
    reload_config()
    return changed


def handle_reminder(wxid, text, payload, now=None):
    """要不要记一条提醒，以及怎么告诉模型。

    模型的嘴和系统的能力必须一致：只有这里真记下了，它才可以说「到点叫你」。
    没记下就明确告诉它没记下，它才会反问时间，而不是顺口答应。

    now 是给测试用的：「下课提醒我」要按当前时间算，
    不注入的话测试会随真实钟点飘（晚上跑就永远算不出下课时间）。
    """
    import remind

    if payload.get("roomid"):
        return ""          # 群里不记，免得替一群人的日程打工
    try:
        status, info = remind.parse(text, now)
    except Exception as exc:
        print(f"[提醒] 解析出错：{type(exc).__name__}: {exc}", flush=True)
        return ""

    if status == "ok":
        saved = remind.add(ROOT, wxid, info["at"], info["text"], info.get("kind", "at"), text)
        if not saved:
            print("[提醒] 今天已经记满，这条没收", flush=True)
            return "今天的提醒记满了，这条没收下，要让对方明天再来找你。"
        print(f"[提醒] 记下：{remind.at_text(info['at'])} {wxid} 「{saved['text']}」", flush=True)
        return f"系统已经记下：{remind.at_text(info['at'])} 提醒「{saved['text']}」。"

    if status == "no_time":
        return "对方想要提醒，但系统没听懂具体时间，需要让对方说清楚几点。"

    if any(k in text for k in ("提醒", "叫我", "喊我", "记着")):
        pend = [r for r in remind.load(ROOT) if not r.get("done") and r.get("target") == wxid]
        if pend:
            return "还没到点的：" + remind.describe(pend)
    return ""


def handle(payload):
    started = time.time()
    action, reason = decide(payload)
    wxid = payload.get("from") or ""
    text = (payload.get("text") or "").strip()
    reply = None
    backend = backends.build(CONFIG, ROOT)

    if action == "send":
        conn = connect()
        try:
            # 提醒先记账：后面要不要联网、以及怎么跟模型说，都取决于这一步的结果
            remind_note = handle_reminder(wxid, text, payload)

            # 聊天驱动收藏：消息里明确想要视频才动，后台跑，不占回复线程
            if not payload.get("roomid"):
                kick_bili_chat(text, wxid)

            # 对方发来的图片（base64），随请求一起交给视觉模型
            images = []
            for item in (payload.get("images") or []):
                try:
                    images.append(base64.b64decode(item))
                except Exception:
                    pass

            # 联网搜索：只在「需要新鲜信息」的问题上触发，失败就不搜。
            # 群里默认不搜（要快、要短），私聊才查。
            search_block = ""
            sc = CONFIG.get("search") or {}
            if sc.get("enabled") and not remind_note:
                is_group = bool(payload.get("roomid"))
                allowed_here = (not is_group) or not sc.get("private_only", True)
                want, why = websearch.needs_search(text)
                if allowed_here and want and SEARCH_LIMITER.allow():
                    items, err = websearch.search(
                        text, config=CONFIG,
                        count=int(sc.get("count", 3)),
                        timeout=float(sc.get("timeout", 4.0)),
                    )
                    if items:
                        search_block = websearch.as_prompt_block(items)
                        reason = (reason + "+search") if reason else "search"
                        print(f"[搜索] {why} -> {len(items)} 条："
                              f"{items[0]['title'][:40]}", flush=True)
                    else:
                        print(f"[搜索] {why} 失败：{err}", flush=True)

            hist_rows = history_rows(conn, wxid, CONFIG.get("context_turns", 8))
            # 调模型之前的扩展点：记忆在这里把「你记得的事」塞进来
            before = hooks.collect("before_reply", wxid=wxid, text=text,
                                   config=CONFIG, root=ROOT)
            ctx = backends.Context(
                wxid=wxid,
                name=payload.get("name") or wxid,
                text=text,
                history=[("我" if r["role"] == "me" else "对方", r["text"])
                         for r in hist_rows],
                persona=CONFIG.get("persona", ""),
                at_me=bool(payload.get("at_me")),
                # 每条消息都重新读 prompt.txt，改完立刻生效
                template=backends.load_template(ROOT),
                # 表情由程序决定，不让模型自己数"第几条"
                emoji_ok=(emoji_allowed(conn, wxid, text)
                          and bool(CONFIG.get("kaomoji", False))),
                images_text=list_images(),
                images=images,
                me_name=CONFIG.get("my_name", ""),
                timetable=timetable_text(),
                search_text=search_block,
                remind_text=remind_note,
                memory_text=before.get("memory_text") or "",
            )
            # 作息类问题直接查表，别让模型去算时间
            tt_reply = timetable_answer(text)
            if tt_reply:
                reply = tt_reply
                reason = "timetable"
            else:
                try:
                    reply = backend.reply(ctx)
                except Exception as exc:
                    # 模型挂了**不要**拿兜底话术去骚扰真人。
                    # 以前这里发「刚走神了，你再说一遍？」，48 小时内已经
                    # 往真人/真群发了 8 条这种内容。
                    reply = None
                    action = "error"
                    reason = f"backend_error:{exc}"

                # 它自己说"不知道"，而这话又确实是个问题：去查一次再答。
                # 只管这一轮，不递归，避免死循环烧钱。
                if (reply and not search_block
                        and websearch.looks_uncertain(reply)
                        and websearch.looks_question(text)
                        and SEARCH_LIMITER.allow()):
                    items, err = websearch.search(
                        text, config=CONFIG,
                        count=int(sc.get("count", 3)),
                        timeout=float(sc.get("timeout", 4.0)),
                    )
                    if items:
                        print(f"[搜索] 模型说不知道，补查 {len(items)} 条", flush=True)
                        ctx.search_text = websearch.as_prompt_block(items)
                        try:
                            reply = backend.reply(ctx)
                            reason = (reason + "+retry") if reason else "search_retry"
                        except Exception as exc:
                            print(f"[搜索] 补查后仍失败：{exc}", flush=True)
                    else:
                        print(f"[搜索] 补查失败：{err}", flush=True)
            # 关了颜文字就兜底清理：模型偶尔会自作主张带出来
            if reply and not CONFIG.get("kaomoji", False):
                cleaned = backends.strip_kaomoji(reply)
                if cleaned:
                    reply = cleaned
            turn_ids = []
            cur = conn.execute(
                "insert into messages(wxid, role, text, ts, msg_key) values(?,?,?,?,?)",
                (wxid, "them", text, started, msg_key(payload)),
            )
            turn_ids.append(cur.lastrowid)
            # messages.text 是 NOT NULL：没生成出回复就不能插 me 行，
            # 否则 IntegrityError 会让整个 handle 抛异常、连 them 行都丢。
            if reply:
                cur = conn.execute(
                    "insert into messages(wxid, role, text, ts, msg_key) values(?,?,?,?,?)",
                    (wxid, "me", reply, time.time(), None),
                )
                turn_ids.append(cur.lastrowid)
            state_set(conn, f"backend:{wxid}", backend.name)
            # 记 token 用量：缓存命中 / 未缓存输入 / 输出
            usage = getattr(backend, "last_usage", None) or {}
            if usage:
                try:
                    conn.execute(
                        "insert into usage(ts, wxid, model, hit, miss, out_tokens) "
                        "values(?,?,?,?,?,?)",
                        (started, wxid, getattr(backend, "model", ""),
                         int(usage.get("prompt_cache_hit_tokens") or 0),
                         int(usage.get("prompt_cache_miss_tokens") or 0),
                         int(usage.get("completion_tokens") or 0)),
                    )
                except Exception:
                    pass
            conn.commit()
            # 回复落库之后的扩展点：记忆在这里记轮次、攒够就后台抽取
            hooks.run("after_reply", wxid=wxid, text=text, reply=reply,
                      history_rows=hist_rows, msg_ids=turn_ids,
                      config=CONFIG, root=ROOT)
        finally:
            conn.close()
    elapsed = int((time.time() - started) * 1000)

    with LOCK:
        conn = connect()
        try:
            conn.execute(
                "insert into events(ts, wxid, text, action, reason, reply, elapsed_ms) values(?,?,?,?,?,?,?)",
                (started, wxid, text, action, reason, reply, elapsed),
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "action": action,
        "reason": reason,
        "reply": reply,
        "backend": backend.name,
        "elapsed_ms": elapsed,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30          # 慢连接最多占一个线程 30 秒

    def log_message(self, fmt, *args):  # 安静点，日志我们自己记
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 12 * 1024 * 1024:      # 12MB 上限：图片 base64 也够用
            raise json.JSONDecodeError("body too large", "", 0)
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def _host_ok(self):
        """只接受本机访问：挡 DNS rebinding（恶意域名解析到 127.0.0.1）。

        用**实际绑定的端口**，不用配置里的 port：改了配置又热载之后，
        两者会不一致，结果把自己锁在 403 外面。
        """
        host = (self.headers.get("Host") or "").strip().lower()
        try:
            port = self.server.server_address[1]
        except Exception:
            port = int(CONFIG.get("port", 8765))
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _token_ok(self):
        """共享密钥校验（常量时间比较）。"""
        given = self.headers.get("X-Bridge-Token") or ""
        # 非 ASCII header 会让 compare_digest 抛 TypeError（直接打断连接），
        # 所以统一按字节比较。
        return bool(TOKEN) and hmac.compare_digest(
            given.encode("utf-8", "ignore"), TOKEN.encode("utf-8"))

    PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>微信桥 · 试聊</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif;
         background:#111318; color:#e6e6e6; display:flex; flex-direction:column; height:100vh; }
  header { padding:14px 18px; border-bottom:1px solid #23262e; }
  header b { font-size:16px; }
  header span { display:block; color:#8b93a3; font-size:12.5px; margin-top:4px; }
  #log { flex:1; overflow:auto; padding:18px; display:flex; flex-direction:column; gap:10px; }
  .row { display:flex; }
  .row.me { justify-content:flex-end; }
  .bubble { max-width:76%; padding:9px 13px; border-radius:12px; white-space:pre-wrap; word-break:break-word; }
  .them .bubble { background:#23262e; border-top-left-radius:4px; }
  .me .bubble { background:#2b6de5; border-top-right-radius:4px; }
  .meta { color:#6f7787; font-size:12px; margin-top:4px; }
  .drop .bubble { background:#3a2328; color:#ff9aa2; }
  form { display:flex; gap:8px; padding:12px 18px 18px; border-top:1px solid #23262e; }
  input { flex:1; padding:11px 13px; border-radius:9px; border:1px solid #2c303a;
          background:#181b21; color:#e6e6e6; font:inherit; outline:none; }
  input:focus { border-color:#2b6de5; }
  button { padding:11px 18px; border:0; border-radius:9px; background:#2b6de5; color:#fff;
           font:inherit; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  #tools { padding:0 18px 10px; display:flex; gap:10px; }
  #tools button { background:#23262e; color:#9aa3b2; font-size:12.5px; padding:6px 11px; }
</style></head>
<body>
<header>
  <b>微信桥 · 试聊窗口</b>
  <a href="/" style="float:right;color:#7aa2ff;text-decoration:none;">设置</a>
  <span>这不是真微信，只是让「替身」在本地陪你练口吻。左边填对方名字。</span>
</header>
<div id="log"></div>
<div id="tools">
  <button onclick="toggle()" id="pauseBtn">暂停替身</button>
  <button onclick="document.getElementById('log').innerHTML=''">清屏</button>
</div>
<form id="f">
  <input id="who" value="测试" placeholder="对方名字" style="width:110px;flex:0 0 auto">
  <input id="t" placeholder="打一句对方发来的话，回车发送" autocomplete="off">
  <button id="go">发送</button>
</form>
<script>
// 接口需要共享 token；这里把本页带的 token 自动加到所有 fetch 请求上。
const TOKEN = '%%TOKEN%%';
(function () {
  const orig = window.fetch;
  window.fetch = function (url, opts) {
    opts = opts || {};
    opts.headers = Object.assign({}, opts.headers || {},
                                 {'X-Bridge-Token': TOKEN});
    return orig.call(window, url, opts);
  };
})();
const log = document.getElementById('log');
const input = document.getElementById('t');
const go = document.getElementById('go');
let paused = false;

function add(who, text, note) {
  const row = document.createElement('div');
  row.className = 'row ' + who + (note ? ' drop' : '');
  const box = document.createElement('div');
  const b = document.createElement('div');
  b.className = 'bubble';
  b.textContent = text;
  box.appendChild(b);
  if (note) {
    const m = document.createElement('div');
    m.className = 'meta';
    m.textContent = note;
    box.appendChild(m);
  }
  row.appendChild(box);
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
}

async function send(text) {
  add('me', text);
  go.disabled = true;
  const t0 = performance.now();
  try {
    const res = await fetch('/in', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        from: (document.getElementById('who').value || '测试').trim(),
        name: (document.getElementById('who').value || '测试').trim(),
        text
      })
    });
    const j = await res.json();
    const ms = Math.round(performance.now() - t0);
    if (j.reply) add('them', j.reply, ms + 'ms');
    else add('them', '（没有回复）', j.action + ' / ' + j.reason + ' · ' + ms + 'ms');
  } catch (e) {
    add('them', '请求失败：' + e, null);
  }
  go.disabled = false;
  input.focus();
}

document.getElementById('f').addEventListener('submit', e => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  send(text);
});

async function toggle() {
  paused = !paused;
  await fetch(paused ? '/pause' : '/resume', {method: 'POST'});
  document.getElementById('pauseBtn').textContent = paused ? '恢复替身' : '暂停替身';
  add('them', paused ? '（已暂停，我现在不说话）' : '（恢复说话）', null);
}

input.focus();
</script>
</body></html>"""

    def _html(self, text):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 页面里带 token，绝不能进缓存
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    SETTINGS_TMPL = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>微信桥 · 设置</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; padding:22px; font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif;
         background:#111318; color:#e6e6e6; }
  h1 { font-size:18px; margin:0 0 4px; }
  p.hint, span.hint { color:#8b93a3; font-size:12.5px; }
  p.hint { margin:0 0 18px; }
  label { display:block; margin:14px 0 6px; color:#c8cedb; font-size:13px; }
  input[type=text], input[type=number], textarea {
    width:100%; box-sizing:border-box; padding:9px 11px; border-radius:8px;
    border:1px solid #2c303a; background:#181b21; color:#e6e6e6; font:inherit; }
  textarea { resize:vertical; line-height:1.55; }
  textarea#prompt { font-family:Consolas,monospace; font-size:13px; }
  .row { display:flex; gap:14px; align-items:center; margin-top:14px; flex-wrap:wrap; }
  .row label { margin:0; }
  button { padding:10px 20px; border:0; border-radius:8px; background:#2b6de5;
           color:#fff; font:inherit; cursor:pointer; }
  button:disabled { opacity:.5; }
  button.mini { padding:5px 13px; font-size:12.5px; background:#3a3f4b; }
  a { color:#7aa2ff; margin-left:12px; text-decoration:none; }
  #msg, #cmsg { margin-left:12px; color:#7fd18b; }
  .box { max-width:1000px; }
  .ctxbox { border:1px solid #2c303a; border-radius:10px; overflow:hidden; }
  .ctxrow { display:flex; gap:12px; align-items:center; padding:9px 14px;
            border-bottom:1px solid #22262e; font-size:13.5px; }
  .ctxrow:last-child { border-bottom:0; }
  .ctxrow .who { flex:1 1 auto; }
  .ctxrow .num { color:#8b93a3; min-width:64px; text-align:right; }
  .ctxrow .when { color:#5f6675; min-width:82px; text-align:right; }
  #clearAll { background:#8a2f36; }

  /* 柱状图 */
  .tabs { display:flex; gap:8px; margin:10px 0 12px; }
  .tab { padding:7px 16px; border-radius:8px; background:#242832; color:#c8cedb;
         border:1px solid #2c303a; font-size:13px; cursor:pointer; }
  .tab:hover { background:#2c323e; }
  .tab.on { background:#2b6de5; color:#fff; border-color:#2b6de5; }
  .chartbox { border:1px solid #2c303a; border-radius:10px; padding:12px 4px;
              min-height:150px; }
  .barrow { display:flex; align-items:center; gap:10px; margin:9px 14px;
            padding:3px 6px; border-radius:6px; }
  .barrow:hover { background:#1b1f27; }
  .barlabel { width:110px; color:#c8cedb; font-size:13px; flex:0 0 auto;
              white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .bartrack { flex:1 1 auto; height:14px; background:#1d212a; border-radius:7px;
              overflow:hidden; }
  .barfill { display:block; height:100%; border-radius:7px; min-width:2px;
             transition:width .4s ease; }
  .barval { width:84px; text-align:right; color:#e6e6e6; font-size:13px;
            font-variant-numeric:tabular-nums; flex:0 0 auto; }
  .bignum { margin-top:16px; padding:16px 20px; border-radius:12px;
            background:#151922; border:1px solid #2c303a; display:flex;
            align-items:baseline; gap:16px; }
  .biglabel { color:#8b93a3; font-size:13px; }
  .bigvalue { font-size:44px; font-weight:600; line-height:1;
              font-variant-numeric:tabular-nums; }
</style></head>
<body><div class="box">
<h1>微信桥 · 设置 <span style="font-size:12px;color:#6f7787;font-weight:400">页面版本 2026-10-22 鉴权版</span></h1>
<p class="hint">保存后立即生效：提示词写进 prompt.txt，其余写进 config.json。
  如果下面的统计/上下文显示“被拒绝（403）”，说明浏览器用的是旧缓存，按 <b>Ctrl+F5</b> 强制刷新。</p>

<form id="f">
  <label>微信昵称（它叫自己什么）</label>
  <input type="text" id="my_name" value="%%MYNAME%%">

  <label>性格 / 口吻（一行字）</label>
  <input type="text" id="persona" value="%%PERSONA%%">

  <label>群白名单（JSON。require_at=必须@；triggers=触发词，说到就回应）</label>
  <textarea id="rooms" rows="6" spellcheck="false">%%ROOMS%%</textarea>

  <div class="row">
    <label>主动搭话每天上限</label>
    <input type="number" id="daily_max" value="%%DAILY%%" style="width:100px">
    <label><input type="checkbox" id="private_all" %%PRIVATE%%> 私聊全放行</label>
  </div>

  <label>提示词（prompt.txt 全文）</label>
  <textarea id="prompt" rows="28" spellcheck="false">%%PROMPT%%</textarea>

  <div class="row">
    <button type="submit" id="save">保存</button>
    <a href="/chat">去试聊</a>
    <span id="msg"></span>
  </div>
</form>

<label style="margin-top:30px">上下文（每个联系人攒了多少对话）</label>
<div id="ctx" class="ctxbox"><div class="ctxrow">加载中…</div></div>
<div class="row">
  <button type="button" id="clearAll">清空全部上下文</button>
  <span class="hint" style="margin:0">清空后它就不记得跟这些人聊过什么，下次回复从头开始。</span>
</div>
<div class="row"><span id="cmsg" class="hint" style="margin:0;color:#7fd18b"></span></div>

<label style="margin-top:30px">主动搭话</label>
<div class="pbox">
  <div class="row">
    <label><input type="checkbox" id="pro_enabled" %%PRO_ON%%> 开启自动搭话</label>
    <label><input type="checkbox" id="pro_mode" %%PRO_MODE_ON%%> 先问模型「该不该开口」</label>
    <span class="hint" style="margin:0">不勾=到点就发（老闹钟）。关闭自动时「立即发送」仍可用</span>
  </div>

  <div class="row">
    <label>心跳间隔(分)</label><input type="number" id="pro_hb" value="%%PRO_HB%%" style="width:80px">
    <label>判断后冷却(分)</label><input type="number" id="pro_backoff" value="%%PRO_BACKOFF%%" style="width:80px">
    <label>刚聊过(分)内不插嘴</label><input type="number" id="pro_quiet" value="%%PRO_QUIET%%" style="width:80px">
    <label>两次主动至少隔(分)</label><input type="number" id="pro_gap" value="%%PRO_GAP%%" style="width:80px">
  </div>

  <label>发给谁（逗号分隔，需要有对应的独立聊天窗口）</label>
  <input type="text" id="pro_targets" value="%%PRO_TARGETS%%">

  <div class="row">
    <label>每天定时</label><input type="text" id="pro_daily_at" value="%%PRO_DAILY_AT%%" placeholder="20:00，留空=不定时" style="width:140px">
    <label>空闲几小时后发</label><input type="number" id="pro_idle" value="%%PRO_IDLE%%" style="width:90px">
    <label>每天上限</label><input type="number" id="pro_max" value="%%PRO_MAX%%" style="width:90px">
  </div>

  <div class="row">
    <label>允许时段</label>
    <input type="text" id="pro_win0" value="%%PRO_WIN0%%" style="width:90px">
    <span class="hint">至</span>
    <input type="text" id="pro_win1" value="%%PRO_WIN1%%" style="width:90px">
  </div>

  <label>固定开场白（留空则由模型按聊天记录生成，可用 {{me}} {{name}}）</label>
  <textarea id="pro_template" rows="2" spellcheck="false">%%PRO_TEMPLATE%%</textarea>

  <label>给模型的附加要求（可选）</label>
  <textarea id="pro_hint" rows="2" spellcheck="false">%%PRO_HINT%%</textarea>

  <div class="row">
    <button type="button" id="pro_save">保存主动设置</button>
    <span id="pro_msg" class="hint" style="margin:0;color:#7fd18b"></span>
  </div>

  <label>手动发送的内容（留空则由模型生成；填了就原样发，支持 {{me}} {{name}}）</label>
  <textarea id="pro_text" rows="2" spellcheck="false" placeholder="想说什么就写什么，留空则自动生成"></textarea>

  <div class="row">
    <label>立即发给</label>
    <input type="text" id="pro_send_to" placeholder="窗口标题，如 wxid_alice" style="width:170px">
    <button type="button" id="pro_send">立即发送</button>
    <span id="pro_smsg" class="hint" style="margin:0"></span>
  </div>
</div>

<div class="row" style="margin-top:14px">
  <label style="margin:0">最近的判断（说 / 静 / 拦 / 错）</label>
  <button type="button" id="pro_log_refresh">刷新</button>
  <span id="pro_log_stat" class="hint" style="margin:0"></span>
</div>
<pre id="pro_log" style="max-height:240px;overflow:auto;font-size:12px;line-height:1.65;
  color:#9aa3b2;background:#0f1218;border:1px solid #232833;border-radius:8px;
  padding:10px 12px;white-space:pre-wrap;word-break:break-all;margin:0 0 8px">加载中…</pre>

<label style="margin-top:30px">长期记忆</label>
<div class="pbox">
  <div class="row">
    <label>谁</label><input type="text" id="mem_wxid" placeholder="留空=所有人" style="width:140px">
    <label>找</label><input type="text" id="mem_q" placeholder="正文关键词" style="width:140px">
    <select id="mem_status">
      <option value="active">活跃</option>
      <option value="expired">已过期</option>
      <option value="forgotten">已忘记</option>
      <option value="all">全部</option>
    </select>
    <button type="button" id="mem_refresh">列出</button>
    <span id="mem_msg" class="hint" style="margin:0"></span>
  </div>
  <div id="mem_list" style="max-height:320px;overflow:auto;border:1px solid #232833;
    border-radius:8px;padding:6px 10px;background:#0f1218;margin:8px 0"></div>
  <pre id="mem_detail" style="display:none;max-height:260px;overflow:auto;font-size:12px;
    line-height:1.65;color:#9aa3b2;background:#0f1218;border:1px solid #232833;
    border-radius:8px;padding:10px 12px;white-space:pre-wrap;margin:0 0 8px"></pre>
  <div class="row">
    <label>召回测试</label>
    <input type="text" id="mem_probe" placeholder="打一句话，看会召回到哪几条" style="flex:1">
    <button type="button" id="mem_probe_btn">算一下</button>
  </div>
  <pre id="mem_probe_out" style="max-height:280px;overflow:auto;font-size:12px;
    line-height:1.6;color:#9aa3b2;background:#0f1218;border:1px solid #232833;
    border-radius:8px;padding:10px 12px;white-space:pre-wrap;margin:8px 0 0">还没算过</pre>
</div>

<label style="margin-top:30px">运行统计（近 24 小时）</label>
<div class="tabs" id="stat_tabs">
  <button type="button" class="tab on" data-tab="flow">处理</button>
  <button type="button" class="tab" data-tab="tokens">Token</button>
  <button type="button" class="tab" data-tab="drops">丢弃原因</button>
</div>
<div id="chart" class="chartbox"><div class="ctxrow">加载中…</div></div>
<div class="bignum">
  <div class="biglabel">缓存命中率</div>
  <div class="bigvalue" id="hitrate">--</div>
</div>
<div class="row">
  <button type="button" id="stats_refresh">刷新</button>
  <span class="hint" style="margin:0">每 30 秒自动刷新；鼠标悬停看占比</span>
</div>

</div>
<script>
// 接口需要共享 token；这里把本页带的 token 自动加到所有 fetch 请求上。
const TOKEN = '%%TOKEN%%';
(function () {
  const orig = window.fetch;
  window.fetch = function (url, opts) {
    opts = opts || {};
    opts.headers = Object.assign({}, opts.headers || {},
                                 {'X-Bridge-Token': TOKEN});
    return orig.call(window, url, opts);
  };
})();
document.getElementById('f').addEventListener('submit', async e => {
  e.preventDefault();
  const msg = document.getElementById('msg');
  const btn = document.getElementById('save');
  const payload = {
    my_name: document.getElementById('my_name').value,
    persona: document.getElementById('persona').value,
    rooms_text: document.getElementById('rooms').value,
    daily_max: document.getElementById('daily_max').value,
    private_all: document.getElementById('private_all').checked,
    prompt: document.getElementById('prompt').value
  };
  btn.disabled = true; msg.style.color = '#8b93a3'; msg.textContent = '保存中…';
  try {
    const res = await fetch('/api/settings', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const j = await res.json();
    if (j.ok) { msg.style.color = '#7fd18b'; msg.textContent = '已保存：' + (j.changed || []).join('、'); }
    else { msg.style.color = '#ff9aa2'; msg.textContent = '保存失败：' + (j.error || ''); }
  } catch (err) {
    msg.style.color = '#ff9aa2'; msg.textContent = '请求失败：' + err;
  }
  btn.disabled = false;
});

async function loadContext() {
  const box = document.getElementById('ctx');
  const esc = s => String(s).replace(/[&<>"']/g,
    ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  try {
    const res = await fetch('/api/context');
    if (res.status === 403) {
      box.innerHTML = '<div class="ctxrow" style="color:#ff9aa2">' +
        '被拒绝了（403）：这个页面是浏览器缓存的旧版本，或者缺 token。' +
        '按 Ctrl+F5 强制刷新一次即可。</div>';
      return;
    }
    const j = await res.json();
    const list = j.contacts || [];
    if (!list.length) { box.innerHTML = '<div class="ctxrow">（空，没有任何历史）</div>'; return; }
    box.innerHTML = list.map(c =>
      '<div class="ctxrow"><span class="who">' + esc(c.wxid) + '</span>' +
      '<span class="num">' + c.count + ' 条</span>' +
      '<span class="when">' + esc(c.last || '') + '</span>' +
      '<button type="button" class="mini" data-wxid="' + esc(c.wxid) + '">清空</button></div>'
    ).join('');
    box.querySelectorAll('button.mini').forEach(btn => {
      btn.addEventListener('click', () => clearOne(btn.dataset.wxid));
    });
  } catch (err) {
    box.innerHTML = '<div class="ctxrow">读取失败：' + err + '</div>';
  }
}

async function doClear(wxid) {
  const msg = document.getElementById('cmsg');
  msg.style.color = '#8b93a3';
  msg.textContent = '清空中…';
  try {
    const res = await fetch('/api/context', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(wxid ? {wxid: wxid} : {})
    });
    const j = await res.json();
    if (j.ok) { msg.style.color = '#7fd18b'; msg.textContent = '已清空 ' + j.deleted + ' 条'; }
    else { msg.style.color = '#ff9aa2'; msg.textContent = '失败：' + (j.error || ''); }
    loadContext();
  } catch (err) {
    msg.style.color = '#ff9aa2'; msg.textContent = '请求失败：' + err;
  }
}

function clearOne(wxid) {
  if (!confirm('清空和「' + wxid + '」的全部上下文？')) return;
  doClear(wxid);
}

document.getElementById('clearAll').addEventListener('click', () => {
  if (!confirm('清空所有联系人的上下文？这个操作不能撤销。')) return;
  doClear(null);
});

loadContext();

// ---- 运行统计（单图表区 + 分类按钮） ----
let STATS = null;
let TAB = 'flow';
const TAB_COLORS = ['#4c9aff', '#57c785', '#f28b82', '#fbbc04', '#aecbfa', '#c58af9'];

function barsFor(tab) {
  const t = (STATS && STATS.tokens) || {};
  if (tab === 'tokens') {
    return [
      ['缓存命中', t.cache_hit || 0],
      ['未缓存输入', t.cache_miss || 0],
      ['输出', t.output || 0],
      ['总 tokens', t.total || 0]
    ];
  }
  if (tab === 'drops') {
    const label = {
      no_mention: '群里没 @ 我',
      room_not_allowed: '群没登记',
      duplicate: '重复消息',
      rate_limited_interval: '限流（间隔太短）',
      rate_limited_minute: '限流（每分钟上限）',
      paused_by_owner: '手动暂停'
    };
    return ((STATS && STATS.items) || [])
      .filter(i => i.action === 'drop')
      .map(i => [(label[i.reason] || ('丢弃：' + i.reason)), i.count]);
  }
  return [
    ['处理总数', STATS ? STATS.total : 0],
    ['已回复', STATS ? STATS.sent : 0],
    ['模型失败', STATS ? STATS.model_fail : 0],
    ['已丢弃', STATS ? STATS.dropped : 0],
    ['查表直答', STATS ? STATS.direct_answer : 0],
    ['发送报错', STATS ? (STATS.error_count || 0) : 0]
  ];
}

function renderChart() {
  const box = document.getElementById('chart');
  const escq = s => String(s).replace(/[&<>"']/g,
    ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const bars = barsFor(TAB);
  if (!bars.length) {
    box.innerHTML = '<div class="ctxrow">（这一类暂时没有数据）</div>';
    return;
  }
  const max = Math.max(1, ...bars.map(b => b[1]));
  const base = (TAB === 'tokens')
    ? ((STATS.tokens && STATS.tokens.total) || 1)
    : ((STATS && STATS.total) || 1);
  box.innerHTML = bars.map((b, i) => {
    const pct = b[1] > 0 ? Math.max(2, Math.round(b[1] / max * 100)) : 0;
    const share = Math.round(b[1] / base * 100);
    const color = TAB_COLORS[i % TAB_COLORS.length];
    const label = escq(b[0]);          // 丢弃原因的文案可能来自外部，必须转义
    return '<div class="barrow" title="' + label + '：' + b[1] +
      '（占 ' + share + '%）"><span class="barlabel">' + label + '</span>' +
      '<span class="bartrack"><span class="barfill" style="width:' + pct +
      '%;background:' + color + '"></span></span>' +
      '<span class="barval">' + b[1] + '</span></div>';
  }).join('');
}

function renderBigNumber() {
  const hr = (STATS && STATS.tokens && STATS.tokens.cache_hit_rate) || 0;
  const el = document.getElementById('hitrate');
  el.textContent = hr + '%';
  el.style.color = hr >= 50 ? '#57c785' : (hr >= 20 ? '#ffd166' : '#f28b82');
}

async function loadStats() {
  try {
    const res = await fetch('/api/stats');
    if (res.status === 403) {
      document.getElementById('chart').innerHTML =
        '<div class="ctxrow" style="color:#ff9aa2">被拒绝了（403）：' +
        '设置页是浏览器缓存的旧版本，或者缺 token。按 Ctrl+F5 强制刷新。</div>';
      return;
    }
    const j = await res.json();
    if (j.error) {
      document.getElementById('chart').textContent = '读取失败：' + j.error;
      return;
    }
    STATS = j;
    renderChart();
    renderBigNumber();
  } catch (e) {
    document.getElementById('chart').textContent = '读取失败：' + e;
  }
}

document.getElementById('stat_tabs').addEventListener('click', e => {
  const btn = e.target.closest('button.tab');
  if (!btn) return;
  TAB = btn.dataset.tab;
  document.querySelectorAll('#stat_tabs .tab').forEach(b =>
    b.classList.toggle('on', b === btn));
  renderChart();
});

document.getElementById('stats_refresh').addEventListener('click', loadStats);
loadStats();
setInterval(loadStats, 30000);

// ---- 主动搭话 ----
document.getElementById('pro_save').addEventListener('click', async () => {
  const msg = document.getElementById('pro_msg');
  const targets = document.getElementById('pro_targets').value
    .split(/[,，\s]+/).map(s => s.trim()).filter(Boolean);
  const payload = {proactive: {
    enabled: document.getElementById('pro_enabled').checked,
    mode: document.getElementById('pro_mode').checked ? 'decision' : 'timer',
    targets: targets,
    daily_at: document.getElementById('pro_daily_at').value.trim(),
    idle_hours: parseFloat(document.getElementById('pro_idle').value || '0') || 0,
    daily_max: parseInt(document.getElementById('pro_max').value || '2', 10) || 2,
    heartbeat_min: parseFloat(document.getElementById('pro_hb').value || '90') || 90,
    silent_backoff_min: parseFloat(document.getElementById('pro_backoff').value || '45') || 45,
    quiet_min: parseFloat(document.getElementById('pro_quiet').value || '20') || 20,
    min_gap_min: parseFloat(document.getElementById('pro_gap').value || '30') || 30,
    window: [
      document.getElementById('pro_win0').value.trim() || '09:00',
      document.getElementById('pro_win1').value.trim() || '23:00'
    ],
    template: document.getElementById('pro_template').value,
    hint: document.getElementById('pro_hint').value
  }};
  msg.style.color = '#8b93a3'; msg.textContent = '保存中…';
  try {
    const res = await fetch('/api/settings', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const j = await res.json();
    if (j.ok) { msg.style.color = '#7fd18b'; msg.textContent = '已保存（60 秒内生效）'; }
    else { msg.style.color = '#ff9aa2'; msg.textContent = '保存失败：' + (j.error || ''); }
  } catch (err) { msg.style.color = '#ff9aa2'; msg.textContent = '请求失败：' + err; }
});

document.getElementById('pro_send').addEventListener('click', async () => {
  const target = document.getElementById('pro_send_to').value.trim();
  const msg = document.getElementById('pro_smsg');
  if (!target) { msg.style.color = '#ff9aa2'; msg.textContent = '先填发给谁'; return; }
  msg.style.color = '#8b93a3'; msg.textContent = '发送中…';
  try {
    const res = await fetch('/api/proactive/send', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
      target: target,
      text: document.getElementById('pro_text').value
    })
    });
    const j = await res.json();
    if (j.ok) { msg.style.color = '#7fd18b'; msg.textContent = '已发送：' + (j.detail || ''); }
    else { msg.style.color = '#ff9aa2'; msg.textContent = '失败：' + (j.detail || j.error || ''); }
  } catch (err) { msg.style.color = '#ff9aa2'; msg.textContent = '请求失败：' + err; }
});

// ---- 判断日志 ----
function fmtDecision(r) {
  const mark = {speak:'说', silent:'静', skip:'拦', error:'错', stale:'废'}[r.action] || (r.action || '?');
  const d = new Date((r.ts || 0) * 1000);
  const p = n => String(n).padStart(2, '0');
  const when = `${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  const reason = (r.reason || '').slice(0, 32);
  const text = (r.text || '').replace(/\s+/g, ' ').slice(0, 40);
  return `${when} [${mark}] ${r.target || '?'}  ${r.trigger || '-'}  ${reason}${text ? ' 「' + text + '」' : ''}`;
}

async function loadProLog() {
  const box = document.getElementById('pro_log');
  const stat = document.getElementById('pro_log_stat');
  try {
    const res = await fetch('/api/proactive');
    const j = await res.json();
    const rows = (j.decisions || []).slice().reverse();   // 新的在上
    box.textContent = rows.length
      ? rows.map(fmtDecision).join('\n')
      : '还没有判断记录。开启后跑一段时间再看。';
    const s = j.stats || {};
    stat.textContent = s.ask
      ? `近 7 天：判断 ${s.ask} 次 / 开口 ${s.speak} 次 / 沉默 ${s.silent} 次，开口率 ${Math.round((s.rate || 0) * 100)}%`
      : '';
  } catch (err) {
    box.textContent = '读取失败：' + err;
  }
}
document.getElementById('pro_log_refresh').addEventListener('click', loadProLog);
loadProLog();

// ---------------------------------------------------------------- 长期记忆
async function memLoad() {
  const box = document.getElementById('mem_list');
  const msg = document.getElementById('mem_msg');
  const wxid = document.getElementById('mem_wxid').value.trim();
  const q = document.getElementById('mem_q').value.trim();
  const status = document.getElementById('mem_status').value;
  const qs = new URLSearchParams({wxid: wxid, q: q, status: status});
  msg.style.color = '#8b93a3'; msg.textContent = '读取中…';
  try {
    const j = await (await fetch('/api/memory?' + qs)).json();
    if (!j.ok) { msg.style.color = '#ff9aa2'; msg.textContent = '读取失败：' + (j.error || ''); return; }
    const s = j.stats || {};
    msg.style.color = '#8b93a3';
    msg.textContent = `活跃 ${s.active || 0} · 过期 ${s.expired || 0} · 忘记 ${s.forgotten || 0}`
                    + ` · 被召回过 ${s.recalled || 0} 条`;
    if (!j.atoms.length) { box.innerHTML = '<div class="hint">没有匹配的记忆</div>'; return; }
    box.innerHTML = '';
    for (const a of j.atoms) {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;gap:8px;align-items:baseline;padding:5px 4px;'
                        + 'border-bottom:1px solid #1c1f27';
      const tag = document.createElement('span');
      tag.textContent = '#' + a.id + ' [' + a.kind + '] ' + Number(a.importance).toFixed(2)
                      + ' ×' + a.uses + ' ' + a.wxid;
      tag.style.cssText = 'color:#6f7787;font-size:12px;white-space:nowrap';
      const body = document.createElement('span');
      body.textContent = a.text;
      body.style.cssText = 'flex:1;word-break:break-all';
      const src = document.createElement('button');
      src.type = 'button';
      src.textContent = '来源 ' + (a.sources || 0);
      src.disabled = !a.sources;
      src.style.cssText = 'background:#1d2a3a;color:#7aa2ff;padding:3px 9px;font-size:12px';
      const del = document.createElement('button');
      del.type = 'button'; del.textContent = '忘掉';
      del.style.cssText = 'background:#3a2328;color:#ff9aa2;padding:3px 9px;font-size:12px';
      del.onclick = () => memForget(a.id);
      src.onclick = () => memDetail(a.id);
      row.append(tag, body, src, del);
      box.append(row);
    }
  } catch (err) {
    msg.style.color = '#ff9aa2'; msg.textContent = '请求失败：' + err;
  }
}

async function memForget(id) {
  if (!confirm('忘掉 #' + id + '？这条以后不会再被召回。')) return;
  try {
    const j = await (await fetch('/api/memory/forget', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids: [id]})
    })).json();
    if (!j.ok) { alert('失败：' + (j.error || '')); return; }
    memLoad();
  } catch (err) { alert('请求失败：' + err); }
}

async function memDetail(id) {
  const box = document.getElementById('mem_detail');
  box.style.display = 'block';
  box.textContent = '读取中…';
  try {
    const j = await (await fetch('/api/memory/detail?id=' + id)).json();
    if (!j.ok) { box.textContent = '失败：' + (j.error || ''); return; }
    const a = j.atom;
    const lines = [`#${a.id} [${a.kind}] 重要性 ${Number(a.importance).toFixed(2)}`
                 + `  用了 ${a.uses} 次  建${a.created}  到期${a.expires}`,
                 '『' + a.text + '』', ''];
    if (!a.sources.length) {
      lines.push('这条没有来源记录（早于溯源功能，或者原文已删）。');
    } else {
      lines.push(`来源 ${a.sources.length} 条（总结时送给模型的那几句）：`);
      for (const s of a.sources) {
        lines.push(`  ${s.at || '--:--'}  ${s.who}  ${(s.text || '').replace(/\\s+/g, ' ').slice(0, 120)}`);
      }
    }
    box.textContent = lines.join('\\n');
  } catch (err) { box.textContent = '请求失败：' + err; }
}

async function memProbe() {
  const out = document.getElementById('mem_probe_out');
  const wxid = document.getElementById('mem_wxid').value.trim();
  const text = document.getElementById('mem_probe').value.trim();
  if (!wxid || !text) { out.textContent = '上面「谁」和这里都要填。'; return; }
  out.textContent = '算分中…';
  try {
    const j = await (await fetch('/api/memory/recall', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({wxid: wxid, text: text})
    })).json();
    if (!j.ok) { out.textContent = '失败：' + (j.error || ''); return; }
    const lines = [`开关=${j.enabled ? '开' : '关'}  门槛=${j.min_score}  最多注入 ${j.top_k} 条`, ''];
    const why = { 'shared<2': '卡在重合不足（少于 2 个字）',
                  'rel<0.15': '卡在覆盖率不够（不到问句的 15%）',
                  'expired': '已过期',
                  'empty': '没有可比的字' };
    for (const h of j.hits.slice(0, 12)) {
      const p = h.parts || {};
      lines.push(`${h.hit ? '✓' : '·'} ${h.score.toFixed(3)}  #${h.id} [${h.kind}]`
               + `  重合 ${p.shared || 0} 字 占比 ${p.rel || 0}`
               + ` 重要 ${p.important || 0} 新鲜 ${p.fresh || 0}`
               + (h.gate ? '  → ' + (why[h.gate] || h.gate) : ''));
      lines.push('    ' + h.text);
    }
    lines.push('', j.would_inject.length
      ? '会注入：' + j.would_inject.map(i => '#' + i).join(' ')
      : '这一句什么都召不回来（门槛没过）');
    out.textContent = lines.join('\n');
  } catch (err) { out.textContent = '请求失败：' + err; }
}

document.getElementById('mem_refresh').addEventListener('click', memLoad);
document.getElementById('mem_probe_btn').addEventListener('click', memProbe);
document.getElementById('mem_probe').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); memProbe(); }
});
memLoad();
</script>
</body></html>"""

    def _settings_html(self):
        import html as _html

        prompt_path = ROOT / "prompt.txt"
        try:
            prompt_text = prompt_path.read_text(encoding="utf-8")
        except OSError:
            prompt_text = ""
        rooms = json.dumps(CONFIG.get("rooms") or {}, ensure_ascii=False, indent=2)
        pro = CONFIG.get("proactive") or {}
        win = pro.get("window") or ["09:00", "23:00"]
        return (
            self.SETTINGS_TMPL
            .replace("%%MYNAME%%", _html.escape(CONFIG.get("my_name", "")))
            .replace("%%PERSONA%%", _html.escape(CONFIG.get("persona", "")))
            .replace("%%ROOMS%%", _html.escape(rooms))
            .replace("%%DAILY%%", str(pro.get("daily_max", 2)))
            .replace("%%PRIVATE%%", "checked" if CONFIG.get("private_all") else "")
            .replace("%%PROMPT%%", _html.escape(prompt_text))
            .replace("%%PRO_ON%%", "checked" if pro.get("enabled") else "")
            .replace("%%PRO_TARGETS%%", _html.escape(", ".join(pro.get("targets") or [])))
            .replace("%%PRO_DAILY_AT%%", _html.escape(str(pro.get("daily_at") or "")))
            .replace("%%PRO_IDLE%%", str(pro.get("idle_hours") or 0))
            .replace("%%PRO_MAX%%", str(pro.get("daily_max", 2)))
            .replace("%%PRO_WIN0%%", _html.escape(str(win[0])))
            .replace("%%PRO_WIN1%%", _html.escape(str(win[1])))
            .replace("%%PRO_TEMPLATE%%", _html.escape(pro.get("template") or ""))
            .replace("%%PRO_HINT%%", _html.escape(pro.get("hint") or ""))
            .replace("%%PRO_MODE_ON%%", "checked" if (pro.get("mode") or "decision") == "decision" else "")
            .replace("%%PRO_HB%%", str(pro.get("heartbeat_min") or 90))
            .replace("%%PRO_BACKOFF%%", str(pro.get("silent_backoff_min") or 45))
            .replace("%%PRO_QUIET%%", str(pro.get("quiet_min") or 20))
            .replace("%%PRO_GAP%%", str(pro.get("min_gap_min") or 30))
            .replace("%%TOKEN%%", TOKEN)
        )

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        path = urlsplit(self.path).path
        # 默认拒绝：只有明确列出的公开路径不需要 token。
        # 以后新增路由时忘了加鉴权，也不会裸奔。
        is_public = (path in ("/", "/index.html", "/settings", "/health")
                     or path.startswith("/chat"))
        if not is_public and not self._token_ok():
            return self._json(403, {"error": "missing or bad token"})
        if path in ("/", "/index.html", "/settings"):
            return self._html(self._settings_html())
        if path.startswith("/chat"):
            return self._html(self.PAGE.replace("%%TOKEN%%", TOKEN))
        if path.startswith("/api/context"):
            return self._json(200, {"contacts": context_stats()})
        if path.startswith("/api/stats"):
            try:
                return self._json(200, stats())
            except Exception as exc:
                return self._json(200, {"error": f"{type(exc).__name__}: {exc}"})
        if path.startswith("/api/memory/detail"):
            try:
                qs = parse_qs(urlsplit(self.path).query)
                raw = (qs.get("id", [""])[0] or "").strip()
                if not raw.isdigit():
                    return self._json(200, {"ok": False, "error": "要给 id"})
                atom = memory.detail(int(raw), ROOT)
                if not atom:
                    return self._json(200, {"ok": False, "error": "没有这条记忆"})
                return self._json(200, {"ok": True, "atom": atom})
            except Exception as exc:
                return self._json(200, {"ok": False,
                                        "error": f"{type(exc).__name__}: {exc}"})
        if path.startswith("/api/memory"):
            try:
                qs = parse_qs(urlsplit(self.path).query)
                wxid = (qs.get("wxid", [""])[0] or "").strip()
                q = (qs.get("q", [""])[0] or "").strip()
                status = (qs.get("status", ["active"])[0] or "active").strip()
                return self._json(200, {
                    "ok": True,
                    "atoms": memory.list_atoms(wxid or None, q, status, root=ROOT),
                    "stats": memory.stats(ROOT),
                    "config": memory.cfg(CONFIG),
                })
            except Exception as exc:
                return self._json(200, {"ok": False,
                                        "error": f"{type(exc).__name__}: {exc}"})
        if path.startswith("/api/reload"):
            try:
                reload_config()
                return self._json(200, {"ok": True, "rooms": list(CONFIG.get("rooms") or {})})
            except Exception as exc:
                return self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        if path.startswith("/api/proactive"):
            return self._json(200, {
                "config": CONFIG.get("proactive") or {},
                "state": proactive.load_state(ROOT),
                "decisions": proactive.load_decisions(ROOT, limit=20),
                "stats": proactive.decision_stats(ROOT, days=7),
            })
        if path.startswith("/health"):
            conn = connect()
            try:
                paused = state_get(conn, "paused") == "1"
            finally:
                conn.close()
            return self._json(200, {"ok": True, "backend": CONFIG.get("backend"), "paused": paused})
        if path.startswith("/log"):
            limit = 20
            if "limit=" in self.path:
                try:
                    limit = max(1, int(self.path.split("limit=")[1].split("&")[0]))
                except ValueError:
                    pass
            conn = connect()
            try:
                rows = conn.execute(
                    "select ts, wxid, text, action, reason, reply, elapsed_ms from events order by id desc limit ?",
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
            return self._json(200, [
                {"ts": r[0], "wxid": r[1], "text": r[2], "action": r[3],
                 "reason": r[4], "reply": r[5], "elapsed_ms": r[6]} for r in rows
            ])
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        if not self._token_ok():
            return self._json(403, {"error": "missing or bad token"})
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid json"})

        if self.path.startswith("/in"):
            try:
                return self._json(200, handle(body))
            except Exception as exc:  # 别让一个异常把连接直接掐断
                # 不落日志的话，用户只看到「桥不回消息」，而 events 表里一行都没有
                # （handle 写 events 在函数最后，异常提前抛出就整行跳过）。
                import traceback as _tb
                print(f"[bridge] /in 出错：{type(exc).__name__}: {exc}", flush=True)
                print(_tb.format_exc(), flush=True)
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return

        if self.path.startswith("/api/memory/recall"):
            wxid = ((body or {}).get("wxid") or "").strip()
            text = ((body or {}).get("text") or "").strip()
            if not wxid or not text:
                return self._json(200, {"ok": False, "error": "要给 wxid 和 text"})
            try:
                return self._json(200, {"ok": True, **memory.explain(
                    wxid, text, CONFIG, ROOT,
                    top_k=int(memory.cfg(CONFIG)["top_k"]))})
            except Exception as exc:
                return self._json(200, {"ok": False,
                                        "error": f"{type(exc).__name__}: {exc}"})

        if self.path.startswith("/api/memory/forget"):
            try:
                ids = (body or {}).get("ids") or []
                return self._json(200, {"ok": True,
                                        "n": memory.forget_many(ids, ROOT)})
            except Exception as exc:
                return self._json(200, {"ok": False,
                                        "error": f"{type(exc).__name__}: {exc}"})

        if self.path.startswith("/api/proactive/send"):
            target = (body or {}).get("target") or ""
            text = (body or {}).get("text") or ""
            if not target:
                return self._json(200, {"ok": False, "error": "缺少 target"})
            try:
                ok, how = proactive.fire(CONFIG, ROOT, target, "manual", text or None)
                return self._json(200, {"ok": ok, "detail": how})
            except Exception as exc:
                return self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        if self.path.startswith("/api/context"):
            try:
                deleted = clear_context((body or {}).get("wxid") or None)
                return self._json(200, {"ok": True, "deleted": deleted})
            except Exception as exc:
                return self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        if self.path.startswith("/api/settings"):
            try:
                changed = save_settings(body)
                return self._json(200, {"ok": True, "changed": changed})
            except Exception as exc:
                return self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        if self.path in ("/pause", "/resume"):
            conn = connect()
            try:
                state_set(conn, "paused", "1" if self.path == "/pause" else "0")
                conn.commit()
            finally:
                conn.close()
            return self._json(200, {"ok": True, "paused": self.path == "/pause"})

        return self._json(404, {"error": "not found"})


def kick_bili_chat(text, wxid, cooldown_s=60):
    """聊天里提到想要视频 → 后台搜一条收进「AI收藏」。
    为什么必须异步：搜索 + 收藏要 3~5 秒（bili_fav.add 里还有 1.2 秒节流），
    同步跑会把回复拖到接近 Handler 的 30 秒超时。
    为什么带冷却：bili_chat 自己没有限流，对方连发五条消息就会搜五次。
    """
    now = time.time()
    with BILI_LOCK:
        if now - BILI_LAST.get(wxid, 0) < cooldown_s:
            return
        BILI_LAST[wxid] = now

    def run():
        try:
            import bili_chat          # 延迟导入：收藏这块坏了也不能连累桥启动
            res = bili_chat.on_message(text, wxid)
            action = res.get("action")
            if action == "fav":
                print(f"[收藏] {wxid}: {res.get('bvid')} "
                      f"{(res.get('title') or '')[:30]}", flush=True)
            elif action == "error":
                print(f"[收藏] {wxid} 出错：{res.get('reason')}", flush=True)
        except Exception as exc:
            print(f"[收藏] 线程异常：{type(exc).__name__}: {exc}", flush=True)

    threading.Thread(target=run, daemon=True).start()


def kick_daily():
    """到点写 Obsidian 日记，放后台。

    为什么必须异步：日记里那一段是模型写的，deepseek-reasoner 跑一次要几十秒；
    同步写会把整个 proactive_loop 卡住，后面的提醒就跟着晚发。
    为什么用非阻塞锁：loop 每分钟都调，模型还没回来之前不能重复起线程。
    """
    if not DAILY_LOCK.acquire(blocking=False):
        return

    def run():
        try:
            made = vault.maybe_daily(CONFIG, ROOT)
            if made:
                print(f"[日记] {made}", flush=True)
        except Exception:
            # 一天只跑一次，出问题得留全栈，不然事后完全查不出为什么没写
            import traceback
            print(f"[日记] 线程异常：\n{traceback.format_exc()}", flush=True)
        finally:
            DAILY_LOCK.release()

    threading.Thread(target=run, daemon=True).start()


def proactive_loop():
    """每分钟看一眼该不该主动发消息。

    判断模式下，这一分钟只是「心跳」：tick() 会自己决定
    要不要真的去问模型一次（最小间隔 heartbeat_min + 退避）。
    """
    time.sleep(8)
    while True:
        try:
            for msg in (proactive.tick(CONFIG, ROOT) or []):
                print(f"[主动] {msg}", flush=True)
            # 提醒：到点就发。跟主动搭话分开——那个要判断，这个不用判断。
            for msg in (remind.fire_due(CONFIG, ROOT) or []):
                print(f"[提醒] {msg}", flush=True)
            # Obsidian 日记：到点写一次，一天只写一遍（异步，别堵住提醒）
            kick_daily()
        except Exception as exc:
            print(f"[主动] 出错: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(60)


def main():
    port = int(CONFIG.get("port", 8765))
    ensure_token()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[bridge] listening on http://127.0.0.1:{port} backend={CONFIG.get('backend')}", flush=True)
    print("[bridge] 接口 token 已启用（见 config.json 的 api_token）", flush=True)
    threading.Thread(target=proactive_loop, daemon=True).start()
    # B站订阅：每 10 分钟看一眼有没有到期的词（enabled=false 时什么也不做）
    threading.Thread(target=bili_sub.loop, daemon=True).start()
    bs = bili_sub.load_conf()
    print(f"[bridge] B站订阅: {'开' if bs.get('enabled') else '关'} "
          f"词={bs.get('keywords')} 每 {bs.get('interval_hours')} 小时", flush=True)
    pro = proactive.pro_conf(CONFIG)
    print(f"[bridge] 主动搭话: {'开' if pro.get('enabled') else '关'} "
          f"mode={pro.get('mode')} targets={pro.get('targets')} "
          f"daily_at={pro.get('daily_at')} idle_hours={pro.get('idle_hours')} "
          f"心跳={pro.get('heartbeat_min')}分", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[bridge] bye", flush=True)


if __name__ == "__main__":
    sys.exit(main())
