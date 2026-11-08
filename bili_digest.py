"""评论区摘要：把一屏评论压成「你们在吵什么」。

bili_reply.py 负责把评论抓下来，这个模块负责把抓到的东西变成人话。
三页 60 条评论里，真正有用的往往只有三五拨立场，剩下的都在复读、玩梗、喊口号。
想知道的不是「有 312 条评论」，而是「这评论区在吵什么」。

三条规矩：

  1. **只读**。不调 bili_reply.save()，不往 data/reply_corpus.jsonl 写任何东西——
     那是给聊天驱动攒语料用的，这里只是看一眼就走。
  2. **不抛异常**。风控 -412、登录态失效、评论区关了、模型抽风，一律写进 error
     字段。界面照常画一页「没看成功 + 原因」，不该因为一次抓取失败就白屏。
  3. **模型只出 JSON**。factions / quotes 由模型给，那段给人看的中文摘要是本地
     拼的：同一批数据拼出来的话永远一样，不用多花一次调用，也不会出现「摘要说
     A 和 B 在吵，阵营列表里却没有 B」这种对不上的情况。

    from bili_digest import digest, format_text
    print(format_text(digest("BV1xx")))

刻意没做的：缓存、并发、把评论区翻到底。三页够看出阵营了，抓全和落库是
bili_reply.run_batch 的活，那边才管去重。
"""
import json
import os as _os
import re
from pathlib import Path

from bili_reply import fetch

_HERE = Path(_os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()

MAX_COMMENTS = 120   # 喂模型最多几条：三页 60 条就到顶，再多只是让模型分心、token 翻倍
MAX_CHARS = 80       # 单条评论只留前 80 字：跑题的话通常在后半段

DIGEST_PROMPT = """下面是一个 B 站视频的评论区，共 {{count}} 条（按热度取的前几页）。

请归纳「评论区在吵什么」。评论区的常态不是一团和气，而是分成几拨：一拨在夸、
一拨在骂、一拨在挑刺、一拨在问别的事。找出 3 到 5 个主要的。

规矩：
- name 是个短标签，不超过 8 个字（例如「原著党」「催更」「路人不买账」），
  不要写成一句话，不要带引号、不要带标点
- gist 一句话说清这一拨人在说什么、站在哪边，不超过 40 个字
- n 是这一拨大概有多少条，按你看到的估，加起来不用等于 {{count}}
- quotes 挑 3 条最能代表现场的原话，原样抄，不要改写、不要加用户名；
  挑有冲突感的，「支持」「顶」这种不算
- 只归纳这 {{count}} 条里真有的东西，不要拿视频标题脑补，不要评价谁对谁错
- 输出只能是 JSON，前后不要写「好的」「如下」这类话

{"factions": [{"name": "原著党", "gist": "觉得改编毁了原作，逐条对比", "n": 12}], "quotes": ["原话一", "原话二"]}

评论：
{{comments}}
"""


# ------------------------------------------------------------------ 归纳
def digest(bvid, pages=3, ask=None, root=None):
    """抓评论 + 归纳「评论区在吵什么」。失败写进 error，不抛异常。

    ask 是可注入的 callable(prompt)->str，测试塞假模型用；None 才去 config.json
    找真模型。pages 由 bili_reply 那边压到上限，这里不重复判断。
    """
    res = {"bvid": (bvid or "").strip(), "total": 0, "factions": [], "quotes": [],
           "text": "", "error": ""}
    if not res["bvid"]:
        res["error"] = "没给 BV 号"
        res["text"] = _text_of(res, "")
        return res

    try:
        got = fetch(res["bvid"], pages=pages)
    except Exception as exc:
        # -412 风控、登录态失效、网络断都在这一条路上：对用户来说都是「没看成」
        res["error"] = f"抓评论失败：{type(exc).__name__}: {exc}"
        res["text"] = _text_of(res, "")
        return res

    rows = _rows_of(got)
    title = (got.get("title") or "").strip() if isinstance(got, dict) else ""
    res["total"] = len(rows)
    if not rows:
        res["error"] = "一条评论都没抓到（评论区可能关了、登录态失效，或者被风控拦了）"
        res["text"] = _text_of(res, title)
        return res

    lines = _pack(rows)
    if not lines:
        # 抓到了行但正文全空：删评之后接口常留这种壳
        res["error"] = "抓到的评论正文都是空的"
        res["text"] = _text_of(res, title)
        return res

    ask_fn = ask if ask is not None else _default_ask(_load_cfg(root), root)
    if ask_fn is None:
        res["error"] = "没有可用的模型（config.json 里 backend 不是 api，或者没配 api_key）"
        res["text"] = _text_of(res, title)
        return res

    prompt = (DIGEST_PROMPT
              .replace("{{count}}", str(len(lines)))
              .replace("{{comments}}", "\n".join(lines)))
    try:
        raw = ask_fn(prompt)
    except Exception as exc:
        res["error"] = f"模型调用失败：{type(exc).__name__}: {exc}"
        res["text"] = _text_of(res, title)
        return res

    data, why = _parse(raw)
    res["factions"], res["quotes"] = _tidy(data)
    if not res["factions"]:
        res["error"] = f"模型没给出可用的阵营（{why or 'factions 是空的'}）"
    res["text"] = _text_of(res, title)
    return res


def _text_of(res, title):
    """拼那段给人看的中文摘要。每条退出路径都得有 text，所以单独一个函数。"""
    label = f"《{title}》" if title else (res.get("bvid") or "这个视频")
    sep = "" if title else " "      # 中文书名号后面不空格，BV 号后面留一格
    total = _int(res.get("total"))
    err = res.get("error") or ""
    if err:
        if total:
            return f"{label}{sep}抓了 {total} 条评论，但没能归纳出来——{err}"
        return f"{label}{sep}没看成功，{err}"

    facs = res.get("factions") or []
    top = facs[0]
    cnt = f"约 {top['n']} 条，" if top["n"] else ""
    out = (f"{label}{sep}的评论区，{total} 条评论大致分成 {len(facs)} 拨。"
           f"声音最大的是「{top['name']}」，{cnt}{top['gist'] or '（它没说清）'}")
    rest = "、".join(f"「{f['name']}」" for f in facs[1:])
    if rest:
        out += f"；其余还有{rest}"
    return out + "。这些只代表抓到的热评，不等于整个评论区。"


# ------------------------------------------------------------------ 解析
def _parse(raw):
    """从模型输出里抠出 JSON 对象；围栏、前后废话、只回一个数组都认。

    返回 (数据, 原因)。原因只在没抠出来时才有值，拼进 error 让用户知道卡在哪。
    """
    if isinstance(raw, dict):
        return raw, ""
    s = str(raw or "").strip()
    if not s:
        return {}, "模型返回了空内容"

    parts = [b.strip() for b in s.split("```") if b.strip()]
    # 围栏第一行可能是语言标记（```json），去掉再试
    parts = [re.sub(r"^json\b\s*", "", b, flags=re.I) for b in parts]
    start, end = s.find("{"), s.rfind("}")
    if 0 <= start < end:
        # 模型偶尔在 JSON 后面补一句「以上」，按最外层括号再抠一次
        parts.append(s[start:end + 1])
    for part in parts + [s]:
        try:
            data = json.loads(part)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data, ""
        if isinstance(data, list):
            return {"factions": data}, ""   # 有的模型直接把 factions 数组甩出来
    return {}, "模型输出里没找到能解析的 JSON"


def _tidy(data):
    """把模型给的收成固定形状：缺字段、类型歪了都不能让整页空掉。"""
    facs = []
    for it in (data.get("factions") or []):
        if isinstance(it, str):
            it = {"name": it}               # 模型省掉 gist 只给名字时也认
        if not isinstance(it, dict):
            continue
        # 名字掐到 12 而不是 8：8 是给模型的软约束，硬切会切在词中间
        name = _short(it.get("name"), 12)
        if not name:
            continue
        facs.append({"name": name, "gist": _short(it.get("gist"), 80),
                     "n": max(0, _int(it.get("n")))})
    quotes = []
    for q in (data.get("quotes") or []):
        if isinstance(q, dict):
            q = q.get("text") or q.get("quote") or q.get("content") or ""
        q = _short(q, 80)
        if q:
            quotes.append(q)
    facs.sort(key=lambda f: -f["n"])        # 声音大的排前面，读者先看这个
    return facs, quotes[:5]


def _short(v, width):
    """压成一行再截断：模型偶尔在 gist 里换行，会把列表排版顶开。"""
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    return s[:width] + "…" if len(s) > width else s


def _int(v):
    """宽容取整数：12、"12"、12.0、None 都能吃，不为一处脏数据抛异常。"""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------ 取数
def _rows_of(got):
    """从 fetch() 的返回值里取评论行。

    函数签名上写的是 list[dict]，实际返回的是 {bvid, aid, title, total, path,
    comments}；两种都收下，省得哪天那边改回来这边跟着崩。
    """
    if isinstance(got, dict):
        got = got.get("comments")
    if isinstance(got, (list, tuple)):
        return [r for r in got if isinstance(r, dict)]
    return []


def _pack(rows, limit=MAX_COMMENTS, width=MAX_CHARS):
    """把评论压成喂模型的行：按赞排，长的截断，空的丢掉。

    按赞排是因为三页 60 条里，高赞的几条基本就是吵起来的源头；
    排序不看接口给什么顺序，同一批数据每次喂进去都长得一样。
    """
    picked = [r for r in rows if str(r.get("text") or "").strip()]
    picked.sort(key=lambda r: -_int(r.get("like")))
    out = []
    for r in picked[:limit]:
        text = re.sub(r"\s+", " ", str(r.get("text") or "")).strip()
        if len(text) > width:
            text = text[:width] + "…"
        out.append(f"- [{_int(r.get('like'))}赞] {text}")
    return out


# ------------------------------------------------------------------ 模型
def _load_cfg(root=None):
    """读 config.json。读不出来给空字典：没有配置是「没模型」，不是「崩了」。"""
    try:
        path = Path(root or _HERE) / "config.json"
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _default_ask(config, root):
    """真模型：照 memory._default_ask 的路子，走 config.json 里配的 api 后端。

    返回 None 表示这条路过不去（backend 不是 api、没 key、backends 起不来），
    由 digest 翻成一句人话，而不是在这里抛。
    """
    try:
        import backends

        if (config.get("backend") or "").lower() != "api":
            return None
        bk = backends.build(config, Path(root or _HERE))
        if not hasattr(bk, "chat"):
            return None
        # 归纳要听话、要便宜，够用就行；想单独换模型就在 config.json 加 digest_model
        model = config.get("digest_model") or config.get("api_model") or "deepseek-chat"

        def ask(prompt):
            return bk.chat(prompt, model=model, temperature=0.3, max_tokens=900)
        return ask
    except Exception:
        return None


# ------------------------------------------------------------------ 输出
def format_text(result):
    """把 digest 的结果排成终端/markdown 文本。出错也照样排，不留一张白纸。"""
    res = result or {}
    bvid = (res.get("bvid") or "").strip() or "?"
    total = _int(res.get("total"))
    head = f"# 评论区在吵什么 · {bvid}"
    if total:
        head += f"（{total} 条评论）"
    out = [head, ""]

    text = (res.get("text") or "").strip()
    if text:
        out += [text, ""]

    facs = res.get("factions") or []
    if facs:
        out.append("## 阵营")
        for i, f in enumerate(facs, 1):
            n = _int(f.get("n"))
            li = f"{i}. **{f.get('name') or '?'}**" + (f"（约 {n} 条）" if n else "")
            out.append(li)
            gist = (f.get("gist") or "").strip()
            if gist:
                out.append(f"   {gist}")    # 缩进 3 格，markdown 里算同一条目的续行
        out.append("")

    quotes = [str(q).strip() for q in (res.get("quotes") or []) if str(q).strip()]
    if quotes:
        out.append("## 原话")
        out += [f"- {_quote(q)}" for q in quotes]
        out.append("")

    err = (res.get("error") or "").strip()
    if err and err not in text:
        # text 里已经讲过一遍就别念第二遍；手拼的 result 没 text 时要能看见原因
        out += ["---", f"出错：{err}"]

    return "\n".join(out).rstrip()


def _quote(q):
    """原话套引号。模型偶尔自带引号，别套成两层。"""
    q = str(q or "").strip()
    if not q:
        return ""
    return q if q[0] in "「“\"'『" else f"「{q}」"
