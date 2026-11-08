"""style.py 的回归测试。

    .venv\\Scripts\\python.exe tests\\test_style.py

全部走临时路径，**不读真实的 data/reply_corpus.jsonl** —— 真语料有没有、
有多少条，都不该让测试挂。
"""
import json
import os
import random
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


import style

# ---------------------------------------------------------------- 1 没有语料
print("=" * 70)
print("1 没有语料")
print("=" * 70)

# 用一个肯定不存在的路径模拟「没有语料」，而不是断言真实语料不存在 ——
# 后者在用户抓过语料之后必然挂。
_NOPE = str(Path(tempfile.gettempdir()) / "wx_style_check" / "__no_such_file__.jsonl")

empty = style.build(_NOPE)
print("build() ->", json.dumps(empty, ensure_ascii=False))
check("build() 空结构 total=0", empty["total"] == 0)
check("build() 空结构不是 None", isinstance(empty, dict))
check("build() 空结构各列表都空",
      empty["catchphrases"] == [] and empty["tail_particles"] == []
      and empty["hot"] == [] and empty["length"]["buckets"] == [])
check("build() 空结构键齐全",
      set(empty) == {"total", "videos", "chars", "avg_len", "catchphrases",
                     "tail_particles", "ending_rate", "length", "punct",
                     "hot", "path", "note"})
check("build() 空结构 punct 全是 0",
      all(v == 0 for v in empty["punct"].values()) and len(empty["punct"]) == 9)
check("card() 空串", style.card(_NOPE) == "", repr(style.card(_NOPE)))

check("build(不存在的路径) 不抛", style.build(str(Path(os.environ["TEMP"]) / "nope" / "x.jsonl"))["total"] == 0)
check("build(目录) 不抛", style.build(str(PROJ))["total"] == 0)
check("card(不存在的 root) 空串", style.card(str(Path(os.environ["TEMP"]) / "nope")) == "")

# 空文件 / 全是坏行
tmp_root = Path(tempfile.gettempdir()) / "wx_style_check" / "data_root"
if tmp_root.exists():
    import shutil
    shutil.rmtree(tmp_root)
tmp_root.mkdir(parents=True)
(tmp_root / "data").mkdir(parents=True, exist_ok=True)
(tmp_root / "data" / "reply_corpus.jsonl").write_text("", encoding="utf-8", newline="\n")
check("空文件 -> 空结构", style.build(str(tmp_root / "data" / "reply_corpus.jsonl"))["total"] == 0)
check("空文件 -> card 空串", style.card(str(tmp_root)) == "")

(tmp_root / "data" / "reply_corpus.jsonl").write_text(
    'not json\n{"text": ""}\n[1,2]\n{"no_text": 1}\n',
    encoding="utf-8", newline="\n")
check("坏行不抛，且无有效评论 -> 空结构",
      style.build(str(tmp_root / "data" / "reply_corpus.jsonl"))["total"] == 0)

# ---------------------------------------------------------------- 2 造 200 条
print()
print("=" * 70)
print("2 造 200 条中文评论")
print("=" * 70)

rnd = random.Random(20261101)

TOPIC = ["这个视频", "这段剪辑", "这期", "这操作", "这个up", "这剧情", "这个标题",
         "这结局", "这配乐", "这字幕", "这段发言", "这个转场", "这波节奏"]
ADJ = ["离谱", "真实", "好笑", "顶", "强", "绝", "稳", "上头", "细节", "牛"]
CATCH = ["确实", "笑死", "离谱", "有一说一", "绷不住", "好家伙", "太真实了",
         "真的绝了", "哈哈哈哈", "关键是"]
TAIL = ["", "啊", "吧", "呢", "嘛", "哈", "呀", "啦", "哦", "咯"]
USER = ["路人甲", "土豆炖土豆", "半夜不睡", "aaa", "吃瓜群众", "一只猫"]
BVID = ["BV1xx411c7mD", "BV1yy411c7mE", "BV1zz411c7mF"]

texts = []
for i in range(200):
    k = i % 12
    t = rnd.choice(TOPIC)
    a = rnd.choice(ADJ)
    c = rnd.choice(CATCH)
    e = rnd.choice(TAIL)
    if k == 0:
        s = f"{t}真的{a}{e}"
    elif k == 1:
        s = f"{c}，{t}太{a}了{e}"
    elif k == 2:
        s = f"{c}"
    elif k == 3:
        s = f"有一说一，{t}是真的{a}，我看三遍了{e}"
    elif k == 4:
        s = f"{t}是怎么做到的？"
    elif k == 5:
        s = f"笑死我了（╯▽╰）" if i % 24 == 5 else f"{t}给我看笑了{e}"
    elif k == 6:
        s = f"{t}确实{a}[doge]"
    elif k == 7:
        s = f"这个up又鸽了{e}"
    elif k == 8:
        s = f"{t}，{a}啊！"
    elif k == 9:
        s = f"绷不住了，{t}怎么能这么{a}的呢{e}"
    elif k == 10:
        s = f"{t}，我笑死，哈哈哈哈哈哈{e}"
    else:
        s = f"确实，{t}{a}，说得好{e}。"
    texts.append(s)

rows = []
for i, s in enumerate(texts):
    like = int(rnd.expovariate(1 / 900))
    if i % 37 == 0:
        like += 12000
    if i % 53 == 0:
        like += 23000
    rows.append({"bvid": BVID[i % 3], "rpid": str(900000 + i),
                 "user": USER[i % 6], "uid": str(1000 + i % 6),
                 "like": like, "time": 1700000000 + i,
                 "text": s, "rcount": 0, "subs": []})

corpus = tmp_root / "data" / "reply_corpus.jsonl"
with corpus.open("w", encoding="utf-8", newline="\n") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

st = style.build(str(corpus))
print("total =", st["total"], " videos =", st["videos"], " chars =", st["chars"],
      " avg_len =", st["avg_len"])
print("catchphrases:", json.dumps(st["catchphrases"], ensure_ascii=False))
print("tail_particles:", json.dumps(st["tail_particles"], ensure_ascii=False))
print("ending_rate:", st["ending_rate"])
print("length:", json.dumps(st["length"], ensure_ascii=False))
print("punct:", json.dumps(st["punct"], ensure_ascii=False))
print("hot:", json.dumps(st["hot"], ensure_ascii=False))

check("200 条全读进来", st["total"] == 200, str(st["total"]))
check("来源 3 个视频", st["videos"] == 3, str(st["videos"]))
check("口头禅非空", len(st["catchphrases"]) > 0, str(len(st["catchphrases"])))
check("口头禅是 2-6 字", all(2 <= len(c["text"]) <= 6 for c in st["catchphrases"]))
check("口头禅带 n 和 likes", all({"text", "n", "likes"} <= set(c) for c in st["catchphrases"]))
check("口头禅有「确实」或「有一说一」等真实重复",
      any(c["text"] in ("确实", "有一说一", "离谱", "笑死", "哈哈哈哈") or
          "确实" in c["text"] or "有一说一" in c["text"] for c in st["catchphrases"]),
      str([c["text"] for c in st["catchphrases"]]))
check("口头禅无重复族（不出现两个互为子串的）",
      all(not (a["text"] in b["text"] or b["text"] in a["text"])
          for i, a in enumerate(st["catchphrases"])
          for b in st["catchphrases"][i + 1:]))
check("句尾语气词非空", len(st["tail_particles"]) > 0, str(len(st["tail_particles"])))
check("语气词 share 在 0-1 之间",
      all(0 < t["share"] <= 1 for t in st["tail_particles"]))
check("语气词 n 之和 == ending_rate", round(sum(t["n"] for t in st["tail_particles"]) / 200, 3) == st["ending_rate"])
check("语气词 share 加起来 == ending_rate",
      abs(sum(t["share"] for t in st["tail_particles"]) - st["ending_rate"]) < 0.02)
check("长度分布 6 个桶",
      len(st["length"]["buckets"]) == 6 and
      abs(sum(b["share"] for b in st["length"]["buckets"]) - 1) < 0.01)
check("短句占比 > 0 且 < 1", 0 < st["length"]["short_share"] < 1,
      str(st["length"]["short_share"]))
check("最常见长度有值", bool(st["length"]["common"]), st["length"]["common"])
check("p90 >= median", st["length"]["p90"] >= st["length"]["median"])
check("标点键齐全", set(st["punct"]) == {
    "period_share", "bang_share", "comma_share", "question_share",
    "ellipsis_share", "no_punct_share", "bang_per100", "kaomoji_share",
    "face_share"})
check("句号占比很低（这批评论本来就少打句号）", st["punct"]["period_share"] < 0.3,
      str(st["punct"]["period_share"]))
check("感叹号密度 > 0", st["punct"]["bang_per100"] > 0, str(st["punct"]["bang_per100"]))
check("颜文字被抓到", st["punct"]["kaomoji_share"] > 0, str(st["punct"]["kaomoji_share"]))
check("高赞样本 5-10 条", 5 <= len(st["hot"]) <= 10, str(len(st["hot"])))
check("高赞按 like 降序", all(st["hot"][i]["like"] >= st["hot"][i + 1]["like"]
                            for i in range(len(st["hot"]) - 1)))
check("高赞同一人不超过 2 条",
      max([sum(1 for h in st["hot"] if h["user"] == u) for u in USER]) <= 2)
check("limit=50 只取最新 50 条", style.build(str(corpus), limit=50)["total"] == 50)
check("build(path) 与 card(root) 走的同一份语料", st["path"] == str(corpus))

# ---------------------------------------------------------------- 3 card
print()
print("=" * 70)
print("3 card() 输出")
print("=" * 70)

c = style.card(str(tmp_root))
c2 = style.card(str(corpus))
print(c)
print("-" * 70)
print("长度:", len(c), "字")
print()
check("card 非空", bool(c))
check("card 不超过 600 字", len(c) <= 600, str(len(c)))
check("card 传数据根和传 jsonl 路径结果一致", c == c2)
check("card 有换行、是分段的", c.count("\n") >= 4, str(c.count("\n")))
check("card 提到口头禅/长度/标点/高赞",
      all(k in c for k in ("口头禅", "长度", "标点", "高赞")))
check("card 不含明显机器痕迹（无 {{ }} 无 None 无 nan）",
      not any(x in c for x in ("{{", "}}", "None", "nan")))
check("card 高赞样本都来自语料",
      all(h["text"][:10] in c for h in st["hot"][:5]))

# 语料很小的时候也不能崩
small = tmp_root / "small.jsonl"
with small.open("w", encoding="utf-8", newline="\n") as f:
    f.write(json.dumps({"bvid": "BV1", "rpid": "1", "user": "u", "like": 1,
                        "text": "确实"}, ensure_ascii=False) + "\n")
sst = style.build(str(small))
print("3 条以下的语料 ->", json.dumps(sst, ensure_ascii=False)[:300])
check("小语料 build 不崩", sst["total"] == 1)
check("小语料 card 不崩", isinstance(style.card(str(small)), str))

# ---------------------------------------------------------------- 4 文件本身
print()
print("=" * 70)
print("4 文件本身")
print("=" * 70)

raw = (PROJ / "style.py").read_bytes()
check("UTF-8 无 BOM", not raw.startswith(b"\xef\xbb\xbf"))
check("换行全是 LF", b"\r\n" not in raw)
check("能按 UTF-8 解码", bool(raw.decode("utf-8")))
src = raw.decode("utf-8")
check("没有 main() / CLI", "def main(" not in src and "__main__" not in src
      and "argparse" not in src)
check("两个签名对得上",
      "def build(path=None, limit=None) -> dict:" in src
      and "def card(root=None) -> str:" in src)
for mod in ("numpy", "pandas", "requests", "PySide6", "urllib3"):
    check(f"没引 {mod}", mod not in src)

print()
print("=" * 40)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
