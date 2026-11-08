"""说话风格：把 B 站评论统计成一张「网友怎么说话」的卡。

为什么要这张卡：提示词里写「说话随意点」没有用，模型只会多打两个「哈哈」。
真能让它不端着的，是给它看数出来的证据 —— 一句话多长、结尾挂哪个语气词、
打不打句号、高赞的人是怎么说的。所以这里不写任何「风格描述」，
只统计语料里数得出来的东西，再把这些数字拼进提示词。

口径上有意偏口语，几个取舍都是这个方向：

  - 短语按**出现在多少条评论里**排，不按总次数。一个人刷一百遍「哈哈哈」
    不代表大家都这么说。
  - 只留重复出现的片段，只出现一次的当噪声。
  - 长度、句尾、标点都按**评论条数**算比例。按字数算会被一条长评论带偏。
  - 高赞样本同一个人的不超过两条，否则一条神评就能占满整张卡。

语料是 `bili_reply.py` 落下的 `data/reply_corpus.jsonl`。**文件不存在是常态**
（用户还没抓过），所以 build() 给空结构、card() 给空串，都不抛异常 ——
这张卡是锦上添花，不能因为它让聊天挂了。

这个模块只被 import，没有命令行。
"""
import json
import os as _os
import re
from pathlib import Path

_HERE = Path(_os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
DATA = _HERE / "data"
CORPUS = DATA / "reply_corpus.jsonl"

MAX_CARD = 600        # 这张卡最后要塞进提示词，太长会挤掉正经内容
HOT_N = 8             # 高赞样本最多几条
MIN_GRAM = 2          # 片段最短/最长几个字
MAX_GRAM = 6
MAX_GRAMS = 300000    # 片段计数器的上限，超了先把只出现一次的丢掉
SCAN_CHARS = 120      # 超长评论只拿前这么多字参与片段统计

# 句尾语气词。只收真正当语气用的单字；「了」「的」这种语法助词一律不收 ——
# 它们几乎每句都有，统计出来全是噪声，对「说话像谁」没有区分度。
PARTICLES = set("啊吧呢嘛哦哈呀啦呗咯喔噢呐诶欸嗷捏惹嗯唉哎哟嘿")

# 全是这些字的片段不要：「的是」「不是」「就都」没有信息量，谁都这么说。
# 「哈」特意不在里面 ——「哈哈」正是要找的那种口头禅。
_STOP = set("的了着过地得是在有和与就都也还又才只把被给对跟向从为以而且但"
            "所以这那其之个一不没很太更最我你他她它们您咱啥谁哪怎么吗呢吧啊呀哦嘛")

# 标点按用途分。英文句号不算句号：评论里的 "." 多半是省略号或小数点，
# 算进来会稀释「不爱打句号」这个信号，而那正好是我们想要的证据。
_PUNCT_ALL = set("。，、！？：；…,.!?:;·～~")
_FACE = re.compile(r"\[[\u4e00-\u9fff]{1,6}\]")     # B站表情码 [笑哭]，中文名才认
_PAREN = re.compile(r"[（(][^）)]{0,12}[）)]")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_KAOMOJI_CHARS = set("╯╰╥︿▽ωДд°∀´`﹏≧≦￣∇・･ヾノ彡〃ヮ")

_BUCKETS = [("1-5", 1, 5), ("6-10", 6, 10), ("11-20", 11, 20),
            ("21-40", 21, 40), ("41-80", 41, 80), ("81+", 81, 10 ** 9)]

_PUNCT_KEYS = ("period_share", "bang_share", "comma_share", "question_share",
               "ellipsis_share", "no_punct_share", "bang_per100",
               "kaomoji_share", "face_share")


# ------------------------------------------------------------------ 小工具
def _pct(n, d):
    """占比，三位小数。分母是 0 就返回 0，不返回 nan。"""
    return round(n / d, 3) if d else 0.0


# ------------------------------------------------------------------ 语料
def _corpus_path(root=None):
    """root 既可以是数据根，也可以是 jsonl 路径。

    别的模块的 root 都是数据根，但界面和临时脚本手里偶尔直接是文件路径，
    两种都收下比让调用方猜省事。
    """
    if root is None:
        return CORPUS
    p = Path(root)
    if p.suffix.lower() == ".jsonl" or p.is_file():
        return p
    return p / "data" / "reply_corpus.jsonl"


def _clean(text):
    """压掉换行和多余空白。评论里复制来的空行会把卡片撑得很丑。"""
    return " ".join((text or "").split())


def _load(path, limit=None):
    """按行读，坏行直接跳过；读不到就是空列表。

    文件不存在、路径是目录、编码坏了、磁盘抽风，全都只当没语料 ——
    这张卡不值得为它抛异常。
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        t = r.get("text")
        if not isinstance(t, str) or not t.strip():
            continue
        rows.append(r)
    # 取最新的那批：说话习惯会变，两年前的评论代表不了现在
    if limit and int(limit) > 0:
        rows = rows[-int(limit):]
    return rows


# ------------------------------------------------------------------ 片段
def _plain(text):
    """只留汉字、字母、数字，去掉标点、表情码、空白。

    片段统计得先把「看着不一样、说的是一个词」抹平，否则「确实」「确实！」
    「确实[笑哭]」在计数里是三个不同的片段，重复度就散了。
    """
    t = _FACE.sub("", text or "")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", t.lower())


def _unique(cands, k):
    """同一族的片段只留一个。

    「哈哈」「哈哈哈」「哈哈哈哈」在语料里是同一句口头禅，三个都列出来
    只会让卡变长，信息量一点没多。次数一样时长的先来（「这个up」胜过
    「up」），所以留下的是这族里更完整的那句。

    **不追更长但更少见的片段**：这里没有分词，「有一说一」往后多吃一个字
    就是「有一说一这」，那是词中间的断口，塞进提示词只会显得莫名其妙。
    """
    out = []
    for c in cands:
        t = c["text"]
        if any(t in o["text"] or o["text"] in t for o in out):
            continue
        out.append(c)
        if len(out) >= k:
            break
    return out


def _phrases(rows, total):
    """口头禅：2-6 字的片段，按「多少条评论里出现过」排，取前 8 个。

    排序里掺了一个「跨视频分散度」：只在某一个视频的评论里高频出现的片段，
    多半是那个视频的话题词（"梦泪"、"装机"、"已三连"），不是人说话的习惯。
    真正跨话题都爱用的（"笑死"、"真的"）才该排前面。只有一个视频时分散度
    恒为 1，退化成纯频次排序，不会比原来差。
    """
    cnt, likes, spread = {}, {}, {}
    nvid = max(1, len({(r.get("bvid") or "") for r in rows}))
    for r in rows:
        bv = r.get("bvid") or ""
        p = _plain(r.get("text"))[:SCAN_CHARS]
        if len(p) < MIN_GRAM:
            continue
        like = int(r.get("like") or 0)
        seen = set()
        for n in range(MIN_GRAM, MAX_GRAM + 1):
            for i in range(len(p) - n + 1):
                g = p[i:i + n]
                # 一条评论里同一个片段出现几次都只算一次：这是「多少人这么说」，
                # 不是「说了多少次」
                if g in seen:
                    continue
                seen.add(g)
                cnt[g] = cnt.get(g, 0) + 1
                likes[g] = likes.get(g, 0) + like
                spread.setdefault(g, set()).add(bv)
        # 语料可能上万条，2-6 字全留会吃掉几百兆；只出现一次的片段
        # 本来就过不了门槛，先清掉
        if len(cnt) > MAX_GRAMS:
            cnt = {k: v for k, v in cnt.items() if v > 1}
            likes = {k: likes.get(k, 0) for k in cnt}
            spread = {k: v for k, v in spread.items() if k in cnt}

    # 门槛跟着语料规模走：200 条里出现 4 次差不多算口头禅，两万条里出现
    # 4 次只是巧合
    floor = max(3, int(total * 0.02 + 0.5))
    cand = []
    for g, df in cnt.items():
        if df < floor or re.search(r"\d", g) or all(ch in _STOP for ch in g):
            continue
        s = len(spread.get(g, ())) / nvid
        # 分散度不是硬门槛，只用来压权重：3 个视频里都出现的词不该被一个
        # 只在一个视频里刷屏的词挤掉。
        cand.append({"text": g, "n": df, "likes": likes.get(g, 0),
                     "spread": round(s, 2),
                     "_w": df * (0.35 + 0.65 * s)})
    cand.sort(key=lambda c: (-c["_w"], -c["likes"], -len(c["text"])))
    for c in cand:
        c.pop("_w", None)
    return _unique(cand, 8)


# ------------------------------------------------------------------ 语气
def _is_emoji(ch):
    """粗略认 emoji。这里只用来剥尾巴和算比例，不需要认全。"""
    o = ord(ch)
    return o > 0xFFFF or 0x2190 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF


def _has_kaomoji(t):
    """颜文字：带专门的符号，或者括号里塞的是一串非中文的东西。

    「(笑)」「(认真)」不算 —— 括号里是中文的多半在补字，不是在画脸。
    """
    if any(ch in _KAOMOJI_CHARS for ch in t):
        return True
    m = _PAREN.search(t)
    return bool(m and not _CJK.search(m.group(0)))


def _tail_char(text):
    """这条评论落在哪个语气字上；不是语气词返回 ""。

    结尾的标点、表情码、emoji 都不是语气，先剥掉：「好家伙！！」
    「好家伙[doge]」都是落在「伙」，不是「！」。复读也只算一个 ——
    要的是挂哪个词，不是挂几下。
    """
    t = _clean(text)
    while t:
        if t[-1] in _PUNCT_ALL or t[-1].isspace() or _is_emoji(t[-1]):
            t = t[:-1]
        elif t.endswith("]") and _FACE.search(t[-8:]):
            t = _FACE.sub("", t)
        else:
            break
    if t and t[-1] in PARTICLES:
        return t[-1]
    return ""


def _tails(texts, total):
    """句尾语气词分布。share 占**全部评论**，不是占带语气词的那部分。"""
    cnt = {}
    for t in texts:
        ch = _tail_char(t)
        if ch:
            cnt[ch] = cnt.get(ch, 0) + 1
    items = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"particle": k, "n": v, "share": _pct(v, total)} for k, v in items]


# ------------------------------------------------------------------ 长度
def _length(lens):
    """长度分布。按字算，标点也算一个字 —— 数字得跟人眼看到的一致。"""
    n = len(lens)
    if not n:
        return {"avg": 0.0, "median": 0, "p90": 0, "short_share": 0.0,
                "common": "", "buckets": []}
    s = sorted(lens)
    buckets = []
    for label, lo, hi in _BUCKETS:
        c = sum(1 for L in s if lo <= L <= hi)
        buckets.append({"label": label, "n": c, "share": _pct(c, n)})
    short = sum(1 for L in s if L <= 10)
    return {
        "avg": round(sum(s) / n, 1),
        "median": s[n // 2],
        "p90": s[min(n - 1, int(n * 0.9))],
        "short_share": _pct(short, n),
        "common": max(buckets, key=lambda b: b["n"])["label"],
        "buckets": buckets,
    }


# ------------------------------------------------------------------ 标点
def _punct(texts, chars):
    """标点习惯。分母是评论条数：要的是「多少人说这话时打句号」。"""
    n = len(texts)
    hit = dict.fromkeys(("period", "bang", "comma", "question", "ellipsis",
                         "none", "kaomoji", "face"), 0)
    bangs = 0
    for t in texts:
        if "。" in t:
            hit["period"] += 1
        if "！" in t or "!" in t:
            hit["bang"] += 1
        if "，" in t or "," in t or "、" in t:
            hit["comma"] += 1
        if "？" in t or "?" in t:
            hit["question"] += 1
        if "…" in t or "..." in t or "。。" in t:
            hit["ellipsis"] += 1
        if not any(ch in _PUNCT_ALL for ch in t):
            hit["none"] += 1
        if _has_kaomoji(t):
            hit["kaomoji"] += 1
        if _FACE.search(t) or any(_is_emoji(ch) for ch in t):
            hit["face"] += 1
        bangs += sum(1 for ch in t if ch in "！!")
    return {
        "period_share": _pct(hit["period"], n),
        "bang_share": _pct(hit["bang"], n),
        "comma_share": _pct(hit["comma"], n),
        "question_share": _pct(hit["question"], n),
        "ellipsis_share": _pct(hit["ellipsis"], n),
        "no_punct_share": _pct(hit["none"], n),
        # 每百字几个感叹号：短句里一两个感叹号，密度比占比更能说明「情绪浓度」
        "bang_per100": round(bangs * 100.0 / chars, 1) if chars else 0.0,
        "kaomoji_share": _pct(hit["kaomoji"], n),
        "face_share": _pct(hit["face"], n),
    }


# ------------------------------------------------------------------ 高赞
def _hot(rows, k=HOT_N):
    """按 like 排的高赞样本，同一句话、同一个人都只留一条。

    一条被顶到前面的神评后面常跟着一串复读，直接取前 8 条卡里就全是一个
    味道了；同一个人最多两条，也是为了这个。
    """
    out, by_user, seen = [], {}, set()
    for r in sorted(rows, key=lambda x: -int(x.get("like") or 0)):
        t = _clean(r.get("text"))
        # 太短的没有参考价值（「1」），太长的塞不进提示词
        if not 3 <= len(t) <= 80:
            continue
        key = re.sub(r"\W+", "", t)[:12]
        user = r.get("user") or ""
        if key in seen or (user and by_user.get(user, 0) >= 2):
            continue
        seen.add(key)
        by_user[user] = by_user.get(user, 0) + 1
        out.append({"text": t, "like": int(r.get("like") or 0),
                    "user": user, "bvid": r.get("bvid") or ""})
        if len(out) >= k:
            break
    return out


# ------------------------------------------------------------------ 对外
def _empty():
    """空结构。键和正常返回完全一样，调用方不用写两套分支。"""
    return {
        "total": 0, "videos": 0, "chars": 0, "avg_len": 0.0,
        "catchphrases": [], "tail_particles": [], "ending_rate": 0.0,
        "length": _length([]),
        "punct": dict.fromkeys(_PUNCT_KEYS, 0.0),
        "hot": [], "path": "",
        "note": "还没有评论语料，先抓一批再说",
    }


def build(path=None, limit=None) -> dict:
    """读语料，返回统计结果；没有语料返回空结构（不是 None）。

    键名：
      total / videos / chars / avg_len   规模，长度按字算（含标点）
      catchphrases   [{text, n, likes}]  口头禅，n 是多少条评论里出现过
      tail_particles [{particle, n, share}] 句尾语气词，share 占全部评论
      ending_rate    结尾带语气词的评论占比
      length         {avg, median, p90, short_share, common, buckets}
      punct          {period_share, bang_share, comma_share, question_share,
                      ellipsis_share, no_punct_share, bang_per100,
                      kaomoji_share, face_share}
      hot            [{text, like, user, bvid}] 高赞样本，like 从大到小
      path / note    看的是哪个文件、给界面看的一句话

    limit 只取**最新**的这么多条；None 或 0 表示全部。
    """
    p = Path(path or CORPUS)
    rows = _load(p, limit)
    if not rows:
        out = _empty()
        out["path"] = str(p)
        return out

    texts = [_clean(r.get("text")) for r in rows]
    lens = [len(t) for t in texts]
    total = len(rows)
    chars = sum(lens)
    tails = _tails(texts, total)
    return {
        "total": total,
        "videos": len({r.get("bvid") for r in rows if r.get("bvid")}),
        "chars": chars,
        "avg_len": round(chars / total, 1),
        "catchphrases": _phrases(rows, total),
        "tail_particles": tails,
        "ending_rate": _pct(sum(t["n"] for t in tails), total),
        "length": _length(lens),
        "punct": _punct(texts, chars),
        "hot": _hot(rows),
        "path": str(p),
        "note": "",
    }


def _percent(x):
    """0.123 -> "12%"；小到看不见的给 "<1%"，写 "0%" 会误导成「从来不用」。"""
    if x <= 0:
        return "0%"
    if x < 0.005:
        return "<1%"
    return f"{x * 100:.0f}%"


def _like(n):
    """点赞写成人话：12000 -> "1.2万"。原始数字塞进提示词没人读。"""
    if n >= 10000:
        return f"{n / 10000:.1f}万".replace(".0万", "万")
    return str(n)


def _clip(s, n):
    """截到 n 字以内，断了补省略号。"""
    s = _clean(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def _advice(st):
    """按统计结果给一句「怎么学」。

    这句曾经是写死的，结果出现过「平均 41 字、最常见 21-40 字」配
    「句子短」这种自相矛盾——数据说东、结论说西，模型会照着结论学错。
    """
    L, P = st["length"], st["punct"]
    out = []
    if L["short_share"] >= 0.45:
        out.append("句子短")
    elif L["avg"] >= 30:
        out.append("句子偏长，能一句说完就别两句")
    if P["no_punct_share"] >= 0.40:
        out.append("大多不打标点，别每句都收句号")
    elif P["period_share"] <= 0.15:
        out.append("少打句号")
    if st["ending_rate"] >= 0.35:
        out.append("句尾带个语气词")
    if max(P["kaomoji_share"], P["face_share"]) >= 0.10:
        out.append("偶尔带个表情")
    if not out:
        out.append("口语，别端着")
    return "、".join(out) + "。"


def _render(st, hot_n):
    """拼卡。hot_n 是这次放几条高赞样本 —— 超长时先砍它。"""
    lines = [f"【网友怎么说话】{st['total']} 条 B 站评论"]
    if st["total"] < 50:
        lines[0] += "（样本还少，参考着用）"

    ph = [c["text"] for c in st["catchphrases"][:6]]
    if ph:
        lines.append("口头禅：" + "、".join(ph))

    tp = st["tail_particles"][:3]
    if tp:
        seg = "、".join(f"{t['particle']} {_percent(t['share'])}" for t in tp)
        lines.append(f"句尾：{seg}；{_percent(st['ending_rate'])} 的评论结尾带语气词")

    L = st["length"]
    if L["median"]:
        lines.append(f"长度：平均 {L['avg']:g} 字，最常见 {L['common']} 字，"
                     f"10 字以内占 {_percent(L['short_share'])}")

    P = st["punct"]
    bits = [f"{_percent(P['period_share'])} 打句号",
            f"{_percent(P['bang_share'])} 带感叹号",
            f"{_percent(P['no_punct_share'])} 一个标点都不打"]
    deco = max(P["kaomoji_share"], P["face_share"])
    if deco:
        bits.append(f"颜文字/表情 {_percent(deco)}")
    lines.append("标点：" + "，".join(bits))

    hot = st["hot"][:hot_n]
    if hot:
        lines.append("高赞怎么说：")
        for h in hot:
            lines.append(f"· {_clip(h['text'], 42)}（{_like(h['like'])} 赞）")

    lines.append("学这个说话方式：" + _advice(st))
    return "\n".join(lines)


def card(root=None) -> str:
    """把统计拼成一段能直接塞进 prompt 的中文；没有语料返回空串。

    root 是数据根（跟别的模块一致），实际读 <root>/data/reply_corpus.jsonl；
    直接传 jsonl 路径也认。

    每次现算不缓存：语料就那么大，缓存只会带来「改了要重启」的坑。
    超过 600 字就先把高赞样本一条条砍掉 —— 卡再好看，也不能挤掉提示词本身。
    """
    st = build(_corpus_path(root))
    if not st["total"]:
        return ""
    text = ""
    for n in (5, 4, 3, 2, 0):
        text = _render(st, n)
        if len(text) <= MAX_CARD:
            return text
    return text[:MAX_CARD - 1] + "…"
