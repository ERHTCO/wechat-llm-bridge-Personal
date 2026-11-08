"""长期记忆：让桥记住「跟这个人聊过什么」。

参考 LivingMemory（AstrBot 插件）的做法，按本项目能承受的规模裁了一版：

  - **记忆原子**：一条一句，带 kind、importance、TTL、访问次数。
  - **自动总结**：每个联系人每聊满 every_turns 轮，后台拿最近的一段对话
    抽几条原子（用便宜模型，不是回复用的 reasoner）。
  - **召回注入**：每轮按当前这句话去算相关度，取 top_k 拼成一段塞进提示词。
  - **生命周期**：按天衰减 importance，到期转 expired，不再注入；
    被召回到的会强化（uses+1、importance 微涨）。

没有做的（性能够用就不再抄）：向量检索、图结构、MMR、rerank。
中文用字符二元组算重合，纯本地、零依赖、不花钱。

数据在 `data/memory.db`，和 chat.db 分开——那边是聊天流水，这边是记忆。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

_HERE = Path(os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
DATA = _HERE / "data"
DB = DATA / "memory.db"

# kind -> TTL 天数（照 LivingMemory 的量级取的）
TTL_DAYS = {"event": 7, "plan": 2, "fact": 180, "relation": 90,
            "preference": 60, "unknown": 30}
KINDS = tuple(TTL_DAYS)

DEFAULT = {
    "enabled": False,
    "every_turns": 8,        # 聊满几轮总结一次
    "top_k": 5,              # 每轮最多注入几条
    "max_chars": 1200,       # 注入文本总长上限
    "model": "deepseek-chat",  # 抽记忆用便宜模型
    "decay_rate": 0.01,      # 每天掉多少 importance
    "min_score": 0.12,       # 相关度低于这个就不注入
}

EXTRACT_PROMPT = """从下面这段聊天里，抽出关于「对方」的、以后还有用的信息。

只抽这几类，没有就不抽：
- fact       稳定的事实（住哪、学什么专业、家里养了什么）
- preference 偏好（喜欢什么、讨厌什么、习惯怎么说话）
- relation   关系与称呼（怎么称呼我、怎么自称）
- plan       计划（约好要做什么、什么时间）
- event      发生的事（今天经历了什么，只在明显重要时抽）

规矩：
- 一条一句，不超过 40 字，写成陈述句，主语用「对方」
- 不复述对话，不写感受，不写寒暄，不把「今天聊了天」这种流水账当记忆
- 已经在下面「已有记忆」里的，不要重复抽
- 最多 4 条，宁缺毋滥；一条都没有就输出 []

输出只能是 JSON 数组，形如：
[{"text": "对方是航空学院的学生", "kind": "fact", "importance": 0.7}]
importance 取 0 到 1，越长期有用越高。

已有记忆：
{{known}}

聊天：
{{chat}}
"""


# ------------------------------------------------------------------ 存储
def connect(root=None):
    p = Path(root or _HERE) / "data" / "memory.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS atoms(
            id INTEGER PRIMARY KEY,
            wxid TEXT NOT NULL,
            text TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'unknown',
            importance REAL NOT NULL DEFAULT 0.5,
            created_at REAL NOT NULL,
            last_used REAL,
            uses INTEGER NOT NULL DEFAULT 0,
            expires_at REAL,
            status TEXT NOT NULL DEFAULT 'active'
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS atoms_wxid ON atoms(wxid, status)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS turns(
            wxid TEXT PRIMARY KEY,
            since INTEGER NOT NULL DEFAULT 0,
            last_decay REAL
        )""")
    # 这条记忆是从哪几条原始消息总结出来的。msg_id 指 chat.db 的 messages.id。
    # 老库升级靠 CREATE IF NOT EXISTS，纯增量；老原子没有来源，界面上照实说。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS atom_sources(
            atom_id INTEGER NOT NULL,
            msg_id  INTEGER NOT NULL,
            PRIMARY KEY(atom_id, msg_id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS atom_sources_msg ON atom_sources(msg_id)")
    conn.commit()
    return conn


def cfg(config=None):
    out = dict(DEFAULT)
    out.update(((config or {}).get("memory") or {}))
    return out


# ------------------------------------------------------------------ 文本
def _bigrams(s):
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", (s or "").lower())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _chars(s):
    """单个汉字/字母的集合。没有 embedding，就用字面重合找相关。

    用字不用二元组：中文里「你学的什么专业」和「对方是航空学院的学生」
    只有「学」「的」重合，二元组一个都对不上，但你确实是在问这件事。
    """
    return set(re.sub(r"[^\w\u4e00-\u9fff]+", "", (s or "").lower()))


def _parts(query, atom_text, importance, created_at, now):
    """相关度的三个分项，调试界面和打分共用，免得两边算得不一样。"""
    q, a = _chars(query), _chars(atom_text)
    if not q or not a:
        return None
    shared = len(q & a)
    age_days = max(0.0, (now - created_at) / 86400)
    return {
        "shared": shared,
        "rel": shared / len(q),
        "important": importance,
        "fresh": 1.0 / (1.0 + age_days / 30.0),   # 30 天减半
    }


def _gate(parts, expired=False):
    """这一条能不能进提示词。score() 和 explain() 都调它，别各写一份。

    返回 None 表示过线，否则返回卡在哪道门槛（给界面显示用）。
    """
    if expired:
        return "expired"
    if not parts:
        return "empty"
    if parts["shared"] < 2:
        return "shared<2"
    if parts["rel"] < 0.15:
        return "rel<0.15"
    return None


def score(query, atom_text, importance, created_at, uses, now, cfg_):
    """相关度：问句里有多少字落在原子里，再接重要性和新鲜度。

    重合不到两个字、或者占问句不到 15% 就直接判 0——否则光靠 importance
    和新鲜度就能过线，等于每条记忆每轮都往提示词里塞，还白给它们强化。
    """
    p = _parts(query, atom_text, importance, created_at, now)
    if _gate(p) is not None:
        return 0.0
    return 0.6 * p["rel"] + 0.25 * p["important"] + 0.15 * p["fresh"]


# ------------------------------------------------------------------ 写入
def add(conn, wxid, items, now=None, sources=()):
    """写入若干原子，去重（同人、同文本或高度重叠就跳过）。返回新增条数。

    sources 是这批原子共同的来源消息 id（chat.db 的 messages.id）；
    同一轮总结出来的原子共享同一个窗口，所以不按条区分。
    """
    now = now or time.time()
    src = [int(x) for x in (sources or []) if str(x).strip().isdigit()]
    have = [r["text"] for r in conn.execute(
        "SELECT text FROM atoms WHERE wxid=? AND status='active'", (wxid,))]
    added = 0
    for it in items or []:
        text = (it.get("text") or "").strip()
        if not text or len(text) > 200:
            continue
        grams = _bigrams(text)
        dup = False
        for old in have:
            og = _bigrams(old)
            if not og:
                continue
            if len(grams & og) / max(1, len(grams)) >= 0.75:
                dup = True
                break
        if dup:
            continue
        kind = (it.get("kind") or "unknown").lower()
        if kind not in TTL_DAYS:
            kind = "unknown"
        imp = it.get("importance")
        try:
            imp = min(1.0, max(0.0, float(imp)))
        except (TypeError, ValueError):
            imp = 0.5
        ttl = TTL_DAYS[kind] * (1.0 + imp)        # 重要的活得久一点
        cur = conn.execute(
            "INSERT INTO atoms(wxid, text, kind, importance, created_at, expires_at) "
            "VALUES(?,?,?,?,?,?)",
            (wxid, text, kind, imp, now, now + ttl * 86400))
        atom_id = cur.lastrowid
        if src:
            conn.executemany(
                "INSERT OR IGNORE INTO atom_sources(atom_id, msg_id) VALUES(?,?)",
                [(atom_id, m) for m in src])
        have.append(text)
        added += 1
    conn.commit()
    return added


def sources_of(atom_id, root=None):
    """这条记忆是从哪几条消息总结出来的（只取 id）。"""
    conn = connect(root)
    try:
        return [r["msg_id"] for r in conn.execute(
            "SELECT msg_id FROM atom_sources WHERE atom_id=? ORDER BY msg_id",
            (int(atom_id),))]
    finally:
        conn.close()


def source_count(root=None):
    """{atom_id: 条数}，给列表页一次性取，避免逐行查。"""
    conn = connect(root)
    try:
        return {r["atom_id"]: r["n"] for r in conn.execute(
            "SELECT atom_id, count(*) n FROM atom_sources GROUP BY atom_id")}
    finally:
        conn.close()


# ------------------------------------------------------------------ 生命周期
def decay(conn, wxid, now=None, rate=None):
    """按天衰减 + 到期转 expired。同一天只做一次。

    第一次见到这个人（还没有 last_decay）不衰减，只把基准时间记下来——
    否则老记忆一上来就被当成放了几百天，一刀砍到地板上。
    """
    now = now or time.time()
    rate = DEFAULT["decay_rate"] if rate is None else rate

    # 到期这件事跟「衰减过没有」无关，每次都查
    conn.execute(
        "UPDATE atoms SET status='expired' "
        "WHERE wxid=? AND status='active' AND expires_at IS NOT NULL AND expires_at<?",
        (wxid, now))

    row = conn.execute("SELECT last_decay FROM turns WHERE wxid=?", (wxid,)).fetchone()
    last = (row["last_decay"] if row and row["last_decay"] else 0) or 0
    if not last:
        conn.execute(
            "INSERT INTO turns(wxid, since, last_decay) VALUES(?,0,?) "
            "ON CONFLICT(wxid) DO UPDATE SET last_decay=excluded.last_decay", (wxid, now))
        conn.commit()
        return 0
    days = (now - last) / 86400
    if days < 1:
        conn.commit()
        return 0
    n = conn.execute(
        "UPDATE atoms SET importance = MAX(0.05, importance - ?) "
        "WHERE wxid=? AND status='active'", (rate * days, wxid)).rowcount
    conn.execute(
        "UPDATE atoms SET status='expired' "
        "WHERE wxid=? AND status='active' AND expires_at IS NOT NULL AND expires_at<?",
        (wxid, now))
    conn.execute(
        "INSERT INTO turns(wxid, since, last_decay) VALUES(?,0,?) "
        "ON CONFLICT(wxid) DO UPDATE SET last_decay=excluded.last_decay", (wxid, now))
    conn.commit()
    return n


def bump(conn, ids, now=None):
    """被召回 = 被想起来一次，强化一点。"""
    if not ids:
        return
    now = now or time.time()
    conn.executemany(
        "UPDATE atoms SET uses=uses+1, last_used=?, "
        "importance=MIN(1.0, importance+0.02) WHERE id=?", [(now, i) for i in ids])
    conn.commit()


def forget(conn, atom_id):
    conn.execute("UPDATE atoms SET status='forgotten' WHERE id=?", (atom_id,))
    conn.commit()


def forget_many(ids, root=None):
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return 0
    conn = connect(root)
    try:
        n = conn.executemany("UPDATE atoms SET status='forgotten' WHERE id=?",
                             [(i,) for i in ids]).rowcount
        conn.commit()
        return max(0, n)
    finally:
        conn.close()


def list_atoms(wxid=None, q="", status="active", limit=300, root=None):
    """给管理界面用的列表。q 按正文模糊匹配，不参与打分。"""
    conn = connect(root)
    try:
        sql = ("SELECT id, wxid, text, kind, importance, uses, status, "
               "datetime(created_at,'unixepoch','localtime') created, "
               "datetime(expires_at,'unixepoch','localtime') expires FROM atoms WHERE 1=1")
        args = []
        if status and status != "all":
            sql += " AND status=?"
            args.append(status)
        if wxid:
            sql += " AND wxid=?"
            args.append(wxid)
        if q:
            sql += " AND text LIKE ?"
            args.append(f"%{q}%")
        sql += " ORDER BY importance DESC, id DESC LIMIT ?"
        args.append(int(limit))
        rows = [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()
    counts = source_count(root)
    for r in rows:
        r["sources"] = counts.get(r["id"], 0)
    return rows


def detail(atom_id, root=None):
    """一条记忆的详情：正文 + 它是从哪几句原文总结出来的。

    来源 id 存在 memory.db，原文在 chat.db（两个库，不 JOIN）。
    原文可能被清过，缺的就如实标出来，不抛异常。
    """
    root = Path(root or _HERE)
    conn = connect(root)
    try:
        row = conn.execute(
            "SELECT id, wxid, text, kind, importance, uses, status, "
            "datetime(created_at,'unixepoch','localtime') created, "
            "datetime(expires_at,'unixepoch','localtime') expires "
            "FROM atoms WHERE id=?", (int(atom_id),)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    atom = dict(row)
    ids = sources_of(atom_id, root)

    msgs = {}
    db = root / "data" / "chat.db"
    if ids and db.exists():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        try:
            q = ("SELECT id, wxid, role, text, ts FROM messages WHERE id IN (%s)"
                 % ",".join("?" * len(ids)))
            msgs = {r["id"]: dict(r) for r in c.execute(q, ids)}
        except sqlite3.Error:
            msgs = {}
        finally:
            c.close()

    out = []
    for mid in ids:
        m = msgs.get(mid)
        if m:
            out.append({
                "id": mid,
                "role": m["role"],
                "who": "我" if m["role"] == "me" else "对方",
                "text": m["text"],
                "at": datetime.fromtimestamp(float(m["ts"])).strftime("%Y-%m-%d %H:%M"),
            })
        else:
            out.append({"id": mid, "role": "", "who": "?", "text": "（原文已删或找不到）",
                        "at": ""})
    atom["sources"] = out
    return atom


def stats(root=None):
    conn = connect(root)
    try:
        out = {}
        for r in conn.execute("SELECT status, count(*) n FROM atoms GROUP BY status"):
            out[r["status"]] = r["n"]
        out["total"] = conn.execute("SELECT count(*) FROM atoms").fetchone()[0]
        out["recalled"] = conn.execute(
            "SELECT count(*) FROM atoms WHERE uses>0").fetchone()[0]
        out["people"] = [dict(r) for r in conn.execute(
            "SELECT wxid, count(*) n, sum(uses) uses FROM atoms "
            "WHERE status='active' GROUP BY wxid ORDER BY n DESC")]
        return out
    finally:
        conn.close()


def explain(wxid, text, config=None, root=None, top_k=8, now=None):
    """召回测试：把所有活跃原子打一遍分，返回每个的分项。

    只算不改：不强化、不衰减，调试多少次都不会影响真实召回。
    也回一段「如果现在真发这句话，会注入什么」，方便对照。
    """
    c = cfg(config)
    now = now or time.time()
    conn = connect(root)
    try:
        rows = conn.execute(
            "SELECT id, text, kind, importance, created_at, uses, expires_at "
            "FROM atoms WHERE wxid=? AND status='active'", (wxid,)).fetchall()
    finally:
        conn.close()

    hits = []
    for r in rows:
        p = _parts(text, r["text"], r["importance"], r["created_at"], now)
        s = score(text, r["text"], r["importance"], r["created_at"], r["uses"], now, c)
        expired = bool(r["expires_at"] and r["expires_at"] < now)
        gate = _gate(p, expired)
        hits.append({
            "id": r["id"],
            "text": r["text"],
            "kind": r["kind"],
            "score": round(s, 4),
            "hit": bool(s >= c["min_score"] and not expired),
            "expired": expired,
            "gate": gate or "",          # 空串 = 过线；否则是卡住的门槛
            "parts": None if not p else {
                "shared": p["shared"],
                "rel": round(p["rel"], 3),
                "important": round(p["important"], 3),
                "fresh": round(p["fresh"], 3),
            },
        })
    hits.sort(key=lambda x: -x["score"])

    picked = [h for h in hits if h["hit"]][:int(top_k)]
    block = ""
    if picked:
        block = ("【你记得的事】\n- 下面是以前跟这个人聊出来、攒下来的。当成你本来就记得的事，"
                 "不要逐条念，也不要说这是记忆库或记录。\n"
                 "- 可能过时或者记错，跟眼下对不上就别用。\n"
                 + "\n".join(f"- {h['text']}" for h in picked))
    return {
        "enabled": c["enabled"],
        "min_score": c["min_score"],
        "top_k": int(top_k),
        "hits": hits,
        "would_inject": [h["id"] for h in picked],
        "block": block,
    }


# ------------------------------------------------------------------ 召回
def recall(wxid, text, config=None, root=None, now=None):
    """给这一轮算出一段要注入的记忆文本。没有就往回的字符串为空。"""
    c = cfg(config)
    if not c["enabled"]:
        return ""
    now = now or time.time()
    conn = connect(root)
    try:
        decay(conn, wxid, now, c["decay_rate"])
        rows = conn.execute(
            "SELECT id, text, kind, importance, created_at, uses FROM atoms "
            "WHERE wxid=? AND status='active'", (wxid,)).fetchall()
        if not rows:
            return ""
        scored = []
        for r in rows:
            s = score(text, r["text"], r["importance"], r["created_at"], r["uses"], now, c)
            if s >= c["min_score"]:
                scored.append((s, r))
        scored.sort(key=lambda x: -x[0])
        picked, used = [], 0
        for s, r in scored[:int(c["top_k"])]:
            line = f"- {r['text']}"
            if used + len(line) > int(c["max_chars"]):
                break
            picked.append(r)
            used += len(line) + 1
        if not picked:
            return ""
        bump(conn, [r["id"] for r in picked], now)
        head = ("【你记得的事】\n"
                "- 下面是以前跟这个人聊出来、攒下来的。当成你本来就记得的事，"
                "不要逐条念，也不要说这是记忆库或记录。\n"
                "- 可能过时或者记错，跟眼下对不上就别用。\n")
        return head + "\n".join(f"- {r['text']}" for r in picked)
    finally:
        conn.close()


# ------------------------------------------------------------------ 自动总结
def tick(wxid, config=None, root=None):
    """这一轮聊完记一次；到阈值就返回 True，表示该总结了。"""
    c = cfg(config)
    if not c["enabled"]:
        return False
    conn = connect(root)
    try:
        conn.execute(
            "INSERT INTO turns(wxid, since) VALUES(?,1) "
            "ON CONFLICT(wxid) DO UPDATE SET since=since+1", (wxid,))
        conn.commit()
        n = conn.execute("SELECT since FROM turns WHERE wxid=?", (wxid,)).fetchone()["since"]
        return n >= int(c["every_turns"])
    finally:
        conn.close()


def pending_atoms(root=None):
    """还没被总结过的轮次，用来在总结成功后清零。"""
    return None


# ------------------------------------------------------------------ 钩子
def hook_before(wxid="", text="", config=None, root=None, **_):
    """before_reply：把召回到的记忆交给 Context（键名 memory_text）。"""
    return {"memory_text": recall(wxid, text, config, root)}


def hook_after(wxid="", text="", reply=None, history_rows=(), msg_ids=(),
               config=None, root=None, **_):
    """after_reply：这一轮记一次；够了就在后台抽记忆。

    抽记忆是另一路模型调用，几秒钟，必须异步——同步会把回复拖到
    Handler 的 30 秒超时边上。
    """
    if not cfg(config)["enabled"]:
        return None
    if not tick(wxid, config, root):
        return None

    rows = list(history_rows or [])
    ids = [r["id"] for r in rows] + [int(i) for i in (msg_ids or [])]

    def run():
        try:
            lines = [f"{'我' if r['role'] == 'me' else '对方'}：{r['text']}" for r in rows]
            lines.append(f"对方：{text}")
            if reply:
                lines.append(f"我：{reply}")
            n = summarize(config, root, wxid, "\n".join(lines[-40:]),
                          sources=ids[-40:])
            if n:
                print(f"[记忆] {wxid} 记得了新的事：{n} 条", flush=True)
        except Exception as exc:
            print(f"[记忆] 抽取出错：{type(exc).__name__}: {exc}", flush=True)

    threading.Thread(target=run, daemon=True).start()
    return None


def summarize(config, root, wxid, transcript, ask=None, now=None, sources=()):
    """从一段对话里抽记忆。ask 是 callable(prompt)->str；默认走配置里的模型。

    sources 是这段 transcript 对应的 messages.id 列表，写进 atom_sources，
    以后能回答「这条记忆是从哪几句聊出来的」。
    """
    c = cfg(config)
    if not c["enabled"]:
        return 0
    now = now or time.time()
    conn = connect(root)
    try:
        known = [r["text"] for r in conn.execute(
            "SELECT text FROM atoms WHERE wxid=? AND status='active' LIMIT 30", (wxid,))]
    finally:
        conn.close()

    prompt = (EXTRACT_PROMPT
              .replace("{{known}}", "\n".join(f"- {t}" for t in known) or "（还没有）")
              .replace("{{chat}}", transcript))

    if ask is None:
        ask = _default_ask(config, root)
    if ask is None:
        return 0
    raw = ask(prompt)
    items = _parse(raw)
    conn = connect(root)
    try:
        n = add(conn, wxid, items, now, sources=sources)
        conn.execute("INSERT INTO turns(wxid, since) VALUES(?,0) "
                     "ON CONFLICT(wxid) DO UPDATE SET since=0", (wxid,))
        conn.commit()
        return n
    finally:
        conn.close()


def _parse(raw):
    if not raw:
        return []
    s = raw.strip()
    start, end = s.find("["), s.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _default_ask(config, root):
    try:
        import backends
        cfg_ = config or {}
        if (cfg_.get("backend") or "").lower() != "api":
            return None
        bk = backends.build(cfg_, Path(root or _HERE))
        model = cfg(config)["model"]

        def ask(prompt):
            return bk.chat(prompt, model=model, temperature=0.3, max_tokens=600)
        return ask
    except Exception:
        return None


# ------------------------------------------------------------------ 管理
def dump(wxid=None, root=None):
    conn = connect(root)
    try:
        sql = ("SELECT id, wxid, text, kind, importance, uses, status, "
               "datetime(created_at,'unixepoch','localtime') t, "
               "datetime(expires_at,'unixepoch','localtime') e FROM atoms")
        rows = conn.execute(sql + (" WHERE wxid=? AND status='active'" if wxid else
                                   " WHERE status='active'")
                            + " ORDER BY importance DESC").fetchall()
        for r in rows:
            print(f"#{r['id']:<4} [{r['kind']:<10}] {r['importance']:.2f} "
                  f"×{r['uses']} {r['t']} 到期 {r['e']}")
            print(f"      {r['text']}")
        print(f"共 {len(rows)} 条")
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="长期记忆")
    ap.add_argument("cmd", choices=["list", "turns", "forget", "decay"])
    ap.add_argument("arg", nargs="?")
    args = ap.parse_args()
    if args.cmd == "list":
        dump(args.arg)
    elif args.cmd == "turns":
        conn = connect()
        for r in conn.execute("SELECT * FROM turns"):
            print(dict(r))
        conn.close()
    elif args.cmd == "forget":
        if not args.arg:
            print("用法: forget <atom_id>")
            return 2
        conn = connect()
        forget(conn, int(args.arg))
        conn.close()
        print("已忘记")
    elif args.cmd == "decay":
        conn = connect()
        for r in conn.execute("SELECT DISTINCT wxid FROM atoms"):
            n = decay(conn, r["wxid"])
            print(f"{r['wxid']}: 衰减 {n} 条")
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
