"""Obsidian 库：日记、收藏说明、收件箱，接到微信桥这条链路上。

    python vault.py path                # 打印库路径
    python vault.py today [--force]     # 生成/刷新今天的日记
    python vault.py day 2026-10-22      # 生成某一天的日记
    python vault.py inbox               # 列出收件箱里有什么
    python vault.py backfill            # 把收藏日志里还没写笔记的补上

库就是个普通文件夹，文件就是接口。规矩写在代码里：

  - 只写 UTF-8 **无 BOM**。带 BOM 的话 `---` 前面多一个字符，frontmatter 不生效。
  - `.obsidian\\` 一个字节都不碰（里面是配置和窗口布局）。
  - 先写 `.tmp` 再 `os.replace`，免得 Obsidian 读到半截文件。
  - 日记整篇重写。要手写的东西放「收件箱」或者另建笔记，别写进日记——
    下一次重写就没了。
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import backends

# 打包后 __file__ 落进 _internal\，所以路径一律从数据根起算
_HERE = Path(os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
DATA = _HERE / "data"

INBOX, DIARY, FAV, ATTACH = "收件箱", "日记", "收藏", "附件"
DIRS = (INBOX, DIARY, FAV, ATTACH)

MAX_MSG_CHARS = 160          # 一条消息在日记里最多留这么多字
MAX_MSG_LINES = 300          # 一天最多展开这么多条，多的只计数
BAD_NAME = re.compile(r'[\\/:*?"<>|\[\]#^]')
LINKS = "https://www.bilibili.com/video/"
FAV_NAME_MAX = 70

# 喂给模型看的材料：太长既慢又贵，而且 20 号那天光调试窗口就 200 多条
DIGEST_BUDGET = 4000
DIGEST_LINE = 60

_AUTO = object()            # 「没指定」：用默认的模型摘要（配了 api 后端才有）

SUMMARY_PROMPT = """下面是我和几个联系人在 {{day}} 这一天的聊天记录。请写成日记的一小段。

要求：
- 3 到 6 行，一行一句。不要列表符号，不要加粗，不要小标题
- 写人不写知识点：查课表、查时间、查天气这类一问一答的结论不用抄进来，
  那些查一下就有。要写的是这天跟谁聊了什么、对方什么状态、什么事说清了或没说清
- 只写材料里有的。不推测动机，不评价，不写「总的来说」
- 语气平实，像自己记的备忘，不要写成汇报

聊天记录：

{{material}}

日记正文："""


def _fav_name(bvid, title):
    """收藏笔记的文件名，也是日记里 [[双链]] 的目标。

    两处必须用同一个函数：不然标题一长，截断位置不同，链就断了。"""
    return _clean(f"{bvid} {title or bvid}", FAV_NAME_MAX)


# ------------------------------------------------------------------ 基础
def conf(root=None):
    """读 config.json。用 utf-8-sig：记事本和 PowerShell 的 -Encoding UTF8 都会写 BOM，
    带 BOM 时 json.loads 直接抛异常，读不到就会被当成「没配库」静默空转。"""
    root = Path(root or _HERE)
    try:
        return json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def vault_root(root=None, config=None):
    """配置文件里那个 `vault` 字段。没配就是 None——所有写操作变成空转。"""
    cfg = config if config is not None else conf(root)
    raw = (cfg.get("vault") or "").strip()
    return Path(raw) if raw else None


def ensure_dirs(v):
    for name in DIRS:
        (v / name).mkdir(parents=True, exist_ok=True)


def _write(path, text):
    """UTF-8 无 BOM + LF，原子替换。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def _clean(name, limit=60):
    s = BAD_NAME.sub(" ", (name or "").strip())
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit].strip() or "未命名"


def _front(fields):
    lines = ["---"]
    for k, val in fields.items():
        if isinstance(val, (list, tuple)):
            lines.append(f"{k}: [{', '.join(str(x) for x in val)}]")
        else:
            lines.append(f"{k}: {val}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _wan(n):
    try:
        n = int(n)
    except Exception:
        return "-"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.2f} 亿".replace(".00 亿", " 亿")
    if n >= 10_000:
        return f"{n / 10_000:.1f} 万".replace(".0 万", " 万")
    return str(n)


def _hm(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M")
    except Exception:
        return "--:--"


# ------------------------------------------------------------------ 数据
def _db_list(root, also=()):
    """要读的 chat.db 列表：数据根里的那个，加上 --also 指定的。"""
    out = []
    p = Path(root or _HERE) / "data" / "chat.db"
    if p.exists():
        out.append(p)
    for extra in also or ():
        q = Path(extra)
        if q.is_file() and q not in out:
            out.append(q)
    return out


def _dedupe(rows, keyfn):
    seen, out = set(), []
    for r in rows:
        k = keyfn(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _collect(dbs, day):
    """把几个库里同一天的东西并起来。

    两个 chat.db 是不同时期各自记的，同一条消息可能都有一份
    （比如两边都在监听同一个窗口），所以按「会话+角色+正文+时间」去重。
    """
    msgs, drops = [], []
    for p in dbs:
        conn = sqlite3.connect(p)
        conn.row_factory = sqlite3.Row
        try:
            msgs += [dict(r) for r in conn.execute(
                "select wxid, role, text, ts from messages "
                "where date(ts,'unixepoch','localtime') = ? order by ts", (day,))]
            drops += [dict(r) for r in conn.execute(
                "select wxid, text, ts from events where action='drop' "
                "and date(ts,'unixepoch','localtime') = ?", (day,))]
        except sqlite3.Error:
            continue
        finally:
            conn.close()

    def mkey(m):
        return (m["wxid"], m["role"], (m["text"] or "")[:80], round(float(m["ts"]), 1))

    def dkey(d):
        return (d["wxid"], (d["text"] or "")[:80], round(float(d["ts"]), 1))

    msgs = sorted(_dedupe(msgs, mkey), key=lambda m: float(m["ts"]))
    drops = _dedupe(drops, dkey)

    # 「收到」= 落库的对方消息条数 + 只出现在 events 里的被丢掉的那些。
    # 不要用集合去重来数条数：同一句话可以说两遍，去重会把「回复」数顶到「收到」之上。
    them_rows = [m for m in msgs if m["role"] == "them"]
    them = {(m["wxid"], (m["text"] or "")[:80]) for m in them_rows}
    dropped = {(d["wxid"], (d["text"] or "")[:80]) for d in drops}
    extra = dropped - them
    stat = {"them": len(them_rows) + len(extra),
            "me": sum(1 for m in msgs if m["role"] == "me"),
            "drop": len(extra)}
    return msgs, stat


def _day_rows(root, day, also=()):
    """(聊天记录, 当天统计)。收到/回复数走 messages，没回走 events。

    数字只从 messages 来，这样和下面列出来的聊天记录一定自洽。"""
    return _collect(_db_list(root, also), day)


# ------------------------------------------------------------------ 让模型写一段
def digest(msgs, budget=DIGEST_BUDGET):
    """把一天的聊天压成给模型看的材料。

    会话按条数从多到少排，钱先花在真聊过的那几个上；
    调试时建的几十个测试窗口本来就没内容，排在后面自然被预算挤掉。
    """
    if not msgs:
        return ""
    rooms = {}
    for m in msgs:
        rooms.setdefault(m["wxid"], []).append(m)

    blocks, used, dropped = [], 0, 0
    for wxid, rows in sorted(rooms.items(), key=lambda kv: len(kv[1]), reverse=True):
        lines = []
        for i, m in enumerate(rows):
            who = "我" if m["role"] == "me" else "对方"
            t = re.sub(r"\s+", " ", m["text"] or "").strip()
            if len(t) > DIGEST_LINE:
                t = t[:DIGEST_LINE] + "…"
            line = f"{who}：{t}"
            if used + len(line) + 1 > budget:
                dropped += len(rows) - i
                break
            lines.append(line)
            used += len(line) + 1
        if lines:
            blocks.append(f"【{wxid}】\n" + "\n".join(lines))

    text = "\n\n".join(blocks)
    if dropped:
        text += f"\n\n（还有 {dropped} 条没放进来：材料太长）"
    return text


def default_ask(root=None, config=None):
    """用桥里那个模型（config 的 backend / api_model）。没配 api 后端就返回 None。"""
    root = Path(root or _HERE)
    cfg = config if config is not None else conf(root)
    if (cfg.get("backend") or "").lower() != "api":
        return None
    if not cfg.get("vault_summary", True):
        return None
    try:
        bk = backends.build(cfg, root)
    except Exception:
        return None
    if not hasattr(bk, "chat_think"):
        return None
    model = cfg.get("vault_summary_model") or cfg.get("api_model")

    def ask(prompt):
        return bk.chat_think(prompt, model=model, max_tokens=800)

    return ask


def summarize(msgs, day, ask):
    """(正文, 思考)。ask 是 callable(prompt) -> (text, reasoning)。"""
    material = digest(msgs)
    if not material or ask is None:
        return "", ""
    prompt = SUMMARY_PROMPT.replace("{{day}}", day).replace("{{material}}", material)
    text, think = ask(prompt)
    return (text or "").strip(), (think or "").strip()


def _quote(lines):
    """把一段文本包成 Obsidian 的折叠 callout。"""
    body = [f"> {ln}".rstrip() if ln.strip() else ">" for ln in lines]
    return body


def _fav_rows(root, day, also=()):
    """收藏日志。数据根那份，加上 --also 那些库旁边各自的日志。"""
    logs = []
    p = Path(root or _HERE) / "data" / "bili_fav_log.jsonl"
    if p.exists():
        logs.append(p)
    for extra in also or ():
        q = Path(extra).parent / "bili_fav_log.jsonl"
        if q.is_file() and q not in logs:
            logs.append(q)
    out, seen = [], set()
    for log in logs:
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not r.get("ok") or r.get("day") != day:
                continue
            k = (r.get("bvid"), r.get("day"))
            if k in seen:
                continue
            seen.add(k)
            out.append(r)
    return out


# ------------------------------------------------------------------ 日记
def _render_daily(root, day, msgs, stat, favs, summary="", think=""):
    head = [
        _front({"date": day, "tags": ["日记", "微信桥"]}).rstrip("\n"),
        "",
        f"# {day}",
        "",
        "> 这个文件由程序整篇重写。要手写的东西写进「收件箱」或者另建笔记。",
        "",
        "## 概览",
        "",
        "| 收到 | 回复 | 没回 | 收藏 |",
        "|---|---|---|---|",
        f"| {stat.get('them', 0)} | {stat.get('me', 0)} | "
        f"{stat.get('drop', 0)} | {len(favs)} |",
        "",
    ]

    if summary:
        head += ["## 这天", ""] + summary.splitlines() + [""]

    body = ["## 聊天", ""]
    if not msgs:
        body += ["（这一天没有记录）", ""]
    else:
        rooms = {}
        for m in msgs:
            rooms.setdefault(m["wxid"], []).append(m)
        shown = 0
        for wxid, rows in rooms.items():
            body += [f"### {wxid}（{len(rows)} 条）", ""]
            for m in rows:
                if shown >= MAX_MSG_LINES:
                    body.append(f"- …还有 {len(msgs) - shown} 条没展开")
                    shown = len(msgs)
                    break
                text = (m["text"] or "").replace("\n", " ").strip()
                if len(text) > MAX_MSG_CHARS:
                    text = text[:MAX_MSG_CHARS] + "…"
                who = "我" if m["role"] == "me" else "对方"
                body.append(f"- `{_hm(m['ts'])}` **{who}** {text}")
                shown += 1
            body.append("")
        if shown < len(msgs):
            body += [f"（共 {len(msgs)} 条，只展开了 {shown} 条）", ""]

    body += ["## 收藏", ""]
    if not favs:
        body += ["（这一天没收藏）", ""]
    else:
        for r in favs:
            body.append(
                f"- [[{_fav_name(r.get('bvid'), r.get('title'))}]] — "
                f"{r.get('reason') or '没写理由'}")
        body.append("")

    if think:
        body += ["## 思考", "",
                 f"> [!quote]- 模型怎么想的（点开，{len(think)} 字）"]
        body += _quote(think.splitlines())
        body.append("")

    return "\n".join(head + body)


def daily(root=None, day=None, force=False, also=(), ask=_AUTO):
    """写一天的日记。返回路径；没配库或写不动就返回 None。

    ask 传 None 就不找模型（离线、测试、只想看流水账时）。
    """
    root = Path(root or _HERE)
    cfg = conf(root)
    v = vault_root(root, cfg)
    if not v:
        return None
    day = day or datetime.now().strftime("%Y-%m-%d")
    msgs, stat = _day_rows(root, day, also)
    favs = _fav_rows(root, day, also)
    path = v / DIARY / f"{day}.md"

    asker = default_ask(root, cfg) if ask is _AUTO else ask
    summary, think = "", ""
    if asker is not None and msgs:
        try:
            summary, think = summarize(msgs, day, asker)
        except Exception as exc:
            print(f"[日记] {day} 的模型摘要没写成：{type(exc).__name__}: {exc}",
                  flush=True)

    text = _render_daily(root, day, msgs, stat, favs, summary, think)

    if path.exists() and not force:
        old = path.read_text(encoding="utf-8", errors="replace")
        if old == text:
            return path                      # 没变化就别碰它，省得 Obsidian 重排
    ensure_dirs(v)
    return _write(path, text)


def history(root=None, also=(), force=True, ask=_AUTO):
    """把所有库里有记录的日子都写一遍。补旧账用。"""
    root = Path(root or _HERE)
    cfg = conf(root)
    if ask is _AUTO:
        ask = default_ask(root, cfg)
    days = set()
    for p in _db_list(root, also):
        conn = sqlite3.connect(p)
        try:
            for (d,) in conn.execute(
                    "select distinct date(ts,'unixepoch','localtime') from messages "
                    "where ts is not null"):
                if d:
                    days.add(d)
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    made = []
    for day in sorted(days):
        p = daily(root, day, force=force, also=also, ask=ask)
        if p:
            made.append(p)
    return made


def maybe_daily(config=None, root=None, now=None):
    """给 bridge 的每分钟心跳调的：到点、一天一次、写完记账。"""
    root = Path(root or _HERE)
    cfg = config if config is not None else conf(root)
    if not (cfg.get("vault") or "").strip():
        return None
    now = now or datetime.now()
    hour = int(cfg.get("vault_daily_hour", 23))
    if now.hour < hour:
        return None
    day = now.strftime("%Y-%m-%d")
    state = _load_state(root)
    if state.get("daily_day") == day:
        return None
    path = daily(root, day, force=True)
    if path:
        state["daily_day"] = day
        state["at"] = int(time.time())
        _save_state(root, state)
        return f"日记已写：{path}"
    return None


def _state_path(root):
    return Path(root or _HERE) / "data" / "vault_state.json"


def _load_state(root=None):
    try:
        return json.loads(_state_path(root).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(root, state):
    p = _state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    _write(p, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


# ------------------------------------------------------------------ 收藏说明
def fav_note(info, reason="", root=None):
    """一条收藏一个笔记。已经存在的不覆盖——人可能在里面补过东西。

    info 是 /x/web-interface/view 的 data，最少只要有 bvid。"""
    root = Path(root or _HERE)
    v = vault_root(root)
    if not v:
        return None
    bvid = (info or {}).get("bvid") or ""
    if not bvid:
        return None
    title = info.get("title") or bvid
    name = _fav_name(bvid, title)
    path = v / FAV / f"{name}.md"
    if path.exists():
        return path

    owner = ((info.get("owner") or {}).get("name")) or ""
    views = (info.get("stat") or {}).get("view")
    fields = {"bvid": bvid, "tags": ["收藏", "B站"]}
    if owner:
        fields["up主"] = owner
    if views is not None:
        fields["播放"] = _wan(views)
    if reason:
        fields["收进来的理由"] = reason
    fields["收藏日"] = datetime.now().strftime("%Y-%m-%d")

    lines = [
        _front(fields).rstrip("\n"),
        "",
        f"# {title}",
        "",
        f"{LINKS}{bvid}",
        "",
    ]
    if owner:
        lines.append(f"- up主：{owner}")
    if views is not None:
        lines.append(f"- 播放：{_wan(views)}")
    if reason:
        lines.append(f"- 收进来的理由：{reason}")
    ensure_dirs(v)
    return _write(path, "\n".join(lines) + "\n")


def backfill(root=None):
    """把收藏日志里还没笔记的补上。离线，只用日志里有的字段。"""
    root = Path(root or _HERE)
    v = vault_root(root)
    if not v:
        print("config.json 里没配 vault")
        return 1
    p = root / "data" / "bili_fav_log.jsonl"
    if not p.exists():
        print("还没有收藏日志")
        return 0
    seen, n = set(), 0
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not r.get("ok"):
            continue
        bvid = r.get("bvid")
        if not bvid or bvid in seen:
            continue
        seen.add(bvid)
        hit = list((v / FAV).glob(f"{bvid} *.md"))
        if hit:
            continue
        made = fav_note({"bvid": bvid, "title": r.get("title")}, r.get("reason") or "", root)
        if made:
            print(f"补上：{made.name}")
            n += 1
    print(f"补了 {n} 条")
    return 0


# ------------------------------------------------------------------ 收件箱
def inbox(root=None):
    """收件箱里的 .md，按修改时间新→旧。给 agent 读的入口。"""
    root = Path(root or _HERE)
    v = vault_root(root)
    if not v or not (v / INBOX).exists():
        return []
    out = []
    for p in sorted((v / INBOX).glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            out.append({"name": p.stem, "path": str(p),
                        "text": p.read_text(encoding="utf-8", errors="replace")})
        except OSError:
            continue
    return out


# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser(description="Obsidian 库")
    ap.add_argument("cmd", choices=["path", "today", "day", "history", "inbox", "backfill"])
    ap.add_argument("day", nargs="?")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--also", action="append", default=[], metavar="CHATDB",
                    help="再读一个 chat.db（另一次部署留下的那份）")
    ap.add_argument("--no-summary", action="store_true",
                    help="不找模型，只写流水账")
    args = ap.parse_args()
    also = args.also
    ask = None if args.no_summary else _AUTO

    if args.cmd == "path":
        v = vault_root()
        print(v if v else "（config.json 里没配 vault）")
        return 0 if v else 1

    if args.cmd == "today":
        p = daily(force=args.force, also=also, ask=ask)
        print(p if p else "没配库")
        return 0 if p else 1

    if args.cmd == "day":
        if not args.day:
            print("用法: day 2026-10-22")
            return 2
        p = daily(day=args.day, force=True, also=also, ask=ask)
        print(p if p else "没配库")
        return 0 if p else 1

    if args.cmd == "history":
        made = history(also=also, ask=ask)
        for p in made:
            print(p)
        print(f"共 {len(made)} 天")
        return 0 if made else 1

    if args.cmd == "inbox":
        items = inbox()
        if not items:
            print("收件箱是空的")
            return 0
        for it in items:
            print(f"=== {it['name']} ===")
            print(it["text"].rstrip())
            print()
        return 0

    if args.cmd == "backfill":
        return backfill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
