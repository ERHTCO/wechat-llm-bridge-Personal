"""bili_digest 的回归测试：不联网、不碰 data/、不写语料库。

    .venv\\Scripts\\python.exe tests\\test_bili_digest.py

为什么一条都不联网：这个模块唯一的副作用是打 B 站接口，风控一响就是 -412，
测试会变成看运气。所以 fetch 和 ask 全部注入假的，只验归纳、容错和排版。

    py %TEMP%\\wxdigest-check\\test_digest.py
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import bili_digest  # noqa: E402
import bili_reply   # noqa: E402

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


def row(i, text, like=10, user="某人"):
    return {"bvid": "BV1xx", "rpid": str(i), "user": user, "uid": "42",
            "like": like, "time": 1700000000, "text": text, "rcount": 0, "subs": []}


ROWS = [row(1, "这改编是拿原作当素材包吗，细节全没了", 980),
        row(2, "没看过原作，我觉得挺好看的", 620),
        row(3, "第二集那场打戏是手绘吗", 300),
        row(4, "   ", 5),                      # 空正文，得被丢掉
        row(5, "原著党别急，动画本来就只能这样", 210)]

# 模型输出：前后有废话、带 ```json 围栏、n 是字符串、有空名字/无名字的脏数据
FAKE_REPLY = """好的，我看了下这批评论，核心是这几拨人：

```json
{
  "factions": [
    {"name": "原著党", "gist": "觉得改编把原作的细节全砍了，逐条举例", "n": 26},
    {"name": "路人觉得好看", "gist": "没看过原作，单纯觉得节奏舒服、画面在线", "n": 14},
    {"name": "打戏党", "gist": "只关心第二集那场打戏是不是手绘", "n": "7"},
    {"name": "", "gist": "没有名字的脏数据", "n": 3},
    {"gist": "只有 gist 没有 name"}
  ],
  "quotes": ["这改编是拿原作当素材包吗", {"text": "没看过原作，我觉得挺好看的"}, ""]
}
```

需要的话我还能按时间线再排一版。
"""


def fake_rows(*_a, **_kw):
    return {"bvid": "BV1xx", "aid": 1, "title": "某部改编动画 第一集",
            "total": 312, "path": "old", "comments": ROWS}


def test_happy_path():
    print("\n[1] 假模型 + 围栏 JSON")
    calls = {}

    def ask(prompt):
        calls["prompt"] = prompt
        return FAKE_REPLY

    real = bili_digest.fetch
    bili_digest.fetch = fake_rows
    try:
        res = bili_digest.digest("BV1xx", pages=2, ask=ask)
    finally:
        bili_digest.fetch = real

    check("没抛异常", isinstance(res, dict))
    check("六个键齐了",
          set(res) == {"bvid", "total", "factions", "quotes", "text", "error"}, sorted(res))
    check("error 是空的", res["error"] == "", res["error"])
    check("total = 抓到的条数（含空正文那条，算抓到的）", res["total"] == 5, res["total"])
    check("名字为空/缺名字的被丢掉，剩 3 拨", len(res["factions"]) == 3,
          res["factions"])
    check("按 n 从大到小排", [f["n"] for f in res["factions"]] == [26, 14, 7],
          [f["n"] for f in res["factions"]])
    check("n 的字符串 '7' 转成了整数",
          res["factions"][2]["n"] == 7 and isinstance(res["factions"][2]["n"], int),
          res["factions"][2])
    check("gist 都留着", all(f["gist"] for f in res["factions"]), res["factions"])
    check("引号里的对象也认，空的跳过，剩 2 条", len(res["quotes"]) == 2, res["quotes"])
    check("text 是一整段中文", len(res["text"]) > 20 and "3 拨" in res["text"], res["text"])
    check("text 里点了最响的那拨", "原著党" in res["text"], res["text"])
    check("喂模型的 prompt 带条数", "共 4 条" in calls["prompt"], calls["prompt"][:40])
    check("空正文没进 prompt", "   \n" not in calls["prompt"] and "- [5赞]" not in calls["prompt"])
    check("prompt 要求 JSON", '"factions"' in calls["prompt"] and '"quotes"' in calls["prompt"])

    out = bili_digest.format_text(res)
    print("\n----- format_text 实际输出（假数据，正常路径）-----")
    print(out)
    print("----- 输出结束 -----")
    check("format_text 有标题", out.startswith("# 评论区在吵什么 · BV1xx"), out[:40])
    check("format_text 标了条数", "（5 条评论）" in out, out[:60])
    check("format_text 有阵营段", "## 阵营" in out and "1. **原著党**（约 26 条）" in out, out)
    check("format_text 有原话段", "## 原话" in out and "「这改编是拿原作当素材包吗」" in out, out)
    check("format_text 没重复报错", "出错：" not in out, out)
    check("format_text 不返回 None", isinstance(out, str) and out.strip() != "")


def test_no_comments():
    print("\n[2] 空评论列表")
    real = bili_digest.fetch
    bili_digest.fetch = lambda bvid, pages=3: {"bvid": bvid, "title": "", "total": 0,
                                               "comments": []}
    try:
        res = bili_digest.digest("BV1xx", ask=lambda p: "不该被调用")
    finally:
        bili_digest.fetch = real
    check("没抛异常", isinstance(res, dict))
    check("error 有内容", bool(res["error"]), res["error"])
    check("total 是 0", res["total"] == 0, res["total"])
    check("factions 是空列表", res["factions"] == [] and res["quotes"] == [])
    check("text 也写了原因", "没看成功" in res["text"], res["text"])
    out = bili_digest.format_text(res)
    print("\n----- format_text 实际输出（空评论）-----")
    print(out)
    print("----- 输出结束 -----")
    check("format_text 没抛异常且带原因", "没看成功" in out, out)


def test_fetch_raises():
    print("\n[3] 抓取抛异常（模拟 -412 风控）")

    def boom(bvid, pages=3):
        raise RuntimeError("查不到 BV1xx：-412 请求被拦截")

    real = bili_digest.fetch
    bili_digest.fetch = boom
    try:
        res = bili_digest.digest("BV1xx", ask=lambda p: "{}")
    finally:
        bili_digest.fetch = real
    check("没抛异常", isinstance(res, dict))
    check("error 里有原因", "抓评论失败" in res["error"] and "-412" in res["error"], res["error"])
    check("total 是 0", res["total"] == 0, res["total"])
    check("text 不是空的", bool(res["text"]), res["text"])
    out = bili_digest.format_text(res)
    print("\n----- format_text 实际输出（抓取失败）-----")
    print(out)
    print("----- 输出结束 -----")
    check("format_text 没抛异常", isinstance(out, str) and out.strip() != "")


def test_ask_raises():
    print("\n[4] 模型抛异常")
    real = bili_digest.fetch
    bili_digest.fetch = fake_rows
    try:
        def boom(prompt):
            raise RuntimeError("HTTP 401: invalid api key")
        res = bili_digest.digest("BV1xx", ask=boom)
    finally:
        bili_digest.fetch = real
    check("没抛异常", isinstance(res, dict))
    check("error 说清是模型挂了", "模型调用失败" in res["error"], res["error"])
    check("评论条数还留着", res["total"] == 5, res["total"])
    check("text 里有条数也有原因", "5 条" in res["text"] and "归纳" in res["text"], res["text"])


def test_ask_garbage():
    print("\n[5] 模型胡言乱语（没有 JSON）")
    real = bili_digest.fetch
    bili_digest.fetch = fake_rows
    try:
        res = bili_digest.digest("BV1xx", ask=lambda p: "我觉得这视频挺好看的，没什么好吵的。")
    finally:
        bili_digest.fetch = real
    check("没抛异常", isinstance(res, dict))
    check("error 有内容", "没给出可用的阵营" in res["error"], res["error"])
    check("factions 空着不硬编", res["factions"] == [], res["factions"])
    check("text 有原因", "归纳" in res["text"], res["text"])


def test_list_shape_and_no_save():
    print("\n[6] fetch 返回 list（docstring 里的老形状）+ 确认不写语料库")
    real_fetch, real_save = bili_digest.fetch, bili_reply.save
    saved = []
    bili_digest.fetch = lambda bvid, pages=3: ROWS          # 老形状：直接一个列表
    bili_reply.save = lambda *a, **kw: saved.append(a) or 1
    try:
        res = bili_digest.digest("BV1xx", ask=lambda p: '{"factions": [{"name":"A","n":1}], '
                                                        '"quotes": []}')
    finally:
        bili_digest.fetch, bili_reply.save = real_fetch, real_save
    check("list 形状也认，抓到 5 条", res["total"] == 5, res["total"])
    check("没调 bili_reply.save()", saved == [], saved)
    check("没有 title 时 text 用 bvid", res["text"].startswith("BV1xx"), res["text"][:20])


def test_no_model():
    print("\n[7] ask=None 且没有可用的模型")
    root = Path(tempfile.mkdtemp(prefix="wxdigest-"))
    real = bili_digest.fetch
    bili_digest.fetch = fake_rows
    try:
        res = bili_digest.digest("BV1xx", root=root)        # 空 root，没有 config.json
    finally:
        bili_digest.fetch = real
    check("没抛异常", isinstance(res, dict))
    check("error 说没模型", "没有可用的模型" in res["error"], res["error"])
    check("评论还是抓着了", res["total"] == 5, res["total"])


def test_empty_bvid_and_missing_title():
    print("\n[8] 空 BV 号 / 缺 title 的边界")
    res = bili_digest.digest("   ")
    check("空 BV 号不抛异常", isinstance(res, dict))
    check("error 有内容", bool(res["error"]), res["error"])
    check("format_text 顶得住空结果", "?" in bili_digest.format_text(res))
    check("format_text 顶得住 {} ", isinstance(bili_digest.format_text({}), str))
    check("format_text 顶得住 None", isinstance(bili_digest.format_text(None), str))


def main():
    test_happy_path()
    test_no_comments()
    test_fetch_raises()
    test_ask_raises()
    test_ask_garbage()
    test_list_shape_and_no_save()
    test_no_model()
    test_empty_bvid_and_missing_title()
    print(f"\n{'=' * 44}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
