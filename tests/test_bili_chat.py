"""聊天驱动收藏（bili_chat）的回归测试：全程离线，不碰网络，也不碰收藏夹。

    .venv\\Scripts\\python.exe tests\\test_bili_chat.py

为什么连 today_count 也要打桩：bili_chat 会先查"今天收了几条"再决定搜不搜。
真去读 data/bili_fav_log.jsonl 的话，用例结果会随用户当天收了多少条飘
（收满 10 条时"该收藏"的用例会变成 skip），这种测试比没有还糟。

打桩打法：bili_chat 里是 `from bili_fav import add` 这种名字绑定，
所以直接换 bili_chat.search / bili_chat.add / bili_chat.today_count 三个属性，
被测代码一行都不用改，也不会真的发搜索或写收藏。

不变量：on_message 永远返回 dict，连 SystemExit（bili_fav 没登录时抛的）也不许往外跑。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import bili_chat  # noqa: E402

PASS = 0
FAIL = 0

WXID = "wxid_test"
BV_HIGH = "BV1high"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def video(bvid, play, title="装机教程"):
    """一条假搜索结果，字段和 bili_search._norm 对齐。"""
    return {"bvid": bvid, "title": title, "author": "up主",
            "play": play, "pubdate": 0, "desc": ""}


HITS = [video("BV1low", 120, "装机入门"),
        video(BV_HIGH, 99999, "装机从零开始"),
        video("BV1mid", 5000, "装机进阶")]

STATE = {}

ORIG = {"search": bili_chat.search,
        "add": bili_chat.add,
        "today_count": bili_chat.today_count}


def _fake_search(keyword, limit=20, page=1):
    STATE["search"] += 1
    STATE["keywords"].append(keyword)
    if STATE["search_error"]:
        raise STATE["search_error"]
    return list(STATE["results"])


def _fake_add(bvids, reason=""):
    STATE["add"] += 1
    STATE["adds"].append((list(bvids), reason))
    if STATE["add_error"]:
        raise STATE["add_error"]
    return STATE["add_rc"]


def patch(results=None, add_rc=0, add_error=None, search_error=None, today=0):
    """换上计数桩，顺便把计数清零。每个用例开头调一次。"""
    STATE.update({
        "search": 0, "add": 0, "keywords": [], "adds": [],
        "results": list(results or []),
        "add_rc": add_rc, "add_error": add_error,
        "search_error": search_error, "today": today,
    })
    bili_chat.search = _fake_search
    bili_chat.add = _fake_add
    bili_chat.today_count = lambda: STATE["today"]


def restore():
    for name, fn in ORIG.items():
        setattr(bili_chat, name, fn)


def call_safely(text, dry=False):
    """调 on_message，把"有没有抛异常"也变成一个断言项。"""
    try:
        return bili_chat.on_message(text, WXID, dry=dry), None
    except BaseException as exc:      # SystemExit 也要抓住，不能漏
        return None, exc


def test_chitchat():
    print("\n[1] 普通闲聊 / 同名不同意的说法：skip，且一个请求都不发")
    patch(results=HITS)
    for text in ("在吗", "吃饭了吗", "哈哈哈哈", "这个教程讲得真好",
                 "B站又崩了", "晚上打个视频电话吧", "有没有视频通话的教程"):
        r, exc = call_safely(text)
        check(f"「{text}」没抛异常", exc is None, exc)
        check(f"「{text}」skip/no_signal",
              (r or {}).get("action") == "skip" and (r or {}).get("reason") == "no_signal",
              r)
    check("search 一次都没调", STATE["search"] == 0, STATE["keywords"])
    check("add 一次都没调", STATE["add"] == 0, STATE["adds"])


def test_happy_path():
    print("\n[2] 「有没有机械键盘的视频」：搜一次、收一条播放最高的")
    patch(results=HITS)
    r, exc = call_safely("有没有机械键盘的视频")
    check("没抛异常", exc is None, exc)
    check("action=fav 且 ok", (r or {}).get("action") == "fav" and (r or {}).get("ok") is True, r)
    check("search 调了一次", STATE["search"] == 1, STATE["keywords"])
    check("搜的是「机械键盘」", STATE["keywords"] == ["机械键盘"], STATE["keywords"])
    check("add 调了一次", STATE["add"] == 1, STATE["adds"])

    bvids, reason = STATE["adds"][0]
    check("只收一条", len(bvids) == 1, bvids)
    check("收的是播放最高那条", bvids == [BV_HIGH], bvids)
    check("reason 含来源前缀「聊天驱动:」", "聊天驱动:" in reason, reason)
    check("reason 含 wxid", WXID in reason, reason)
    check("reason 含关键词", "机械键盘" in reason, reason)
    check("结果里的 fav_reason 就是写进日志的串", (r or {}).get("fav_reason") == reason, r)
    check("结果带 bvid / 播放数 / 标题",
          (r or {}).get("bvid") == BV_HIGH and (r or {}).get("play") == 99999
          and bool((r or {}).get("title")), r)


def test_empty_results():
    print("\n[3] 搜索返回空列表：skip，不收藏")
    patch(results=[])
    r, exc = call_safely("推荐个装机视频")
    check("没抛异常", exc is None, exc)
    check("action=skip", (r or {}).get("action") == "skip", r)
    check("reason=no_result", (r or {}).get("reason") == "no_result", r)
    check("还是搜了一次", STATE["search"] == 1, STATE["keywords"])
    check("没调 add", STATE["add"] == 0, STATE["adds"])


def test_zero_play():
    print("\n[4] 结果全是 0 播放：skip（0 播放等于没有可靠结果）")
    patch(results=[video("BV0a", 0), video("BV0b", 0)])
    r, exc = call_safely("有没有机械键盘的视频")
    check("没抛异常", exc is None, exc)
    check("action=skip", (r or {}).get("action") == "skip", r)
    check("reason=no_result", (r or {}).get("reason") == "no_result", r)
    check("结果条数记下来方便排查", (r or {}).get("result_count") == 2, r)
    check("没调 add", STATE["add"] == 0, STATE["adds"])


def test_add_raises():
    print("\n[5] add 抛异常：返回 action=error，不向外抛")
    patch(results=HITS, add_error=RuntimeError("csrf 过期了"))
    r, exc = call_safely("有没有机械键盘的视频")
    check("异常被吃掉", exc is None, exc)
    check("action=error 且 ok=False",
          (r or {}).get("action") == "error" and (r or {}).get("ok") is False, r)
    check("reason 带异常类型", "RuntimeError" in (r or {}).get("reason", ""), r)
    check("reason 带关键词，日志能定位", (r or {}).get("keyword") == "机械键盘", r)


def test_add_systemexit():
    print("\n[6] add 抛 SystemExit（bili_fav 没登录）：也不能把线程带走")
    patch(results=HITS, add_error=SystemExit("还没有登录态，先跑 python bili_login.py"))
    r, exc = call_safely("有没有机械键盘的视频")
    check("SystemExit 被吃掉", exc is None, exc)
    check("action=error", (r or {}).get("action") == "error", r)
    check("reason 能看出是登录问题", "no_login" in (r or {}).get("reason", ""), r)


def test_dry_run():
    print("\n[7] dry=True：真搜，但不收藏")
    patch(results=HITS)
    r, exc = call_safely("有没有机械键盘的视频", dry=True)
    check("没抛异常", exc is None, exc)
    check("action=fav（表示将会收）", (r or {}).get("action") == "fav", r)
    check("dry 标记为 True", (r or {}).get("dry") is True, r)
    check("仍然搜了一次（否则看不出会收哪条）", STATE["search"] == 1, STATE["keywords"])
    check("没调 add", STATE["add"] == 0, STATE["adds"])
    check("返回将要收的 bvid", (r or {}).get("bvid") == BV_HIGH, r)


def test_duplicate_message():
    print("\n[8] 同一条消息再来一次：照常交给 add，reason 格式不变（查重是 add 的事）")
    patch(results=HITS)
    r1, _ = call_safely("有没有机械键盘的视频")
    r2, _ = call_safely("有没有机械键盘的视频")
    want = f"聊天驱动:{WXID}:机械键盘"
    check("两次都调了 add", STATE["add"] == 2, STATE["adds"])
    check("两次 reason 完全一致且格式对", [x[1] for x in STATE["adds"]] == [want, want],
          STATE["adds"])
    check("两次都收同一个 bvid", [x[0] for x in STATE["adds"]] == [[BV_HIGH], [BV_HIGH]],
          STATE["adds"])
    check("两次都返回 fav", (r1 or {}).get("action") == "fav" and (r2 or {}).get("action") == "fav",
          (r1, r2))


def test_search_error():
    print("\n[9] 搜索抛异常：action=error，不去收藏")
    patch(results=HITS, search_error=TimeoutError("timed out"))
    r, exc = call_safely("有没有机械键盘的视频")
    check("异常被吃掉", exc is None, exc)
    check("action=error", (r or {}).get("action") == "error", r)
    check("reason 带 search 前缀", (r or {}).get("reason", "").startswith("search:"), r)
    check("没调 add", STATE["add"] == 0, STATE["adds"])


def test_daily_cap():
    print("\n[10] 今天已收满：skip，连搜索都不发")
    patch(results=HITS, today=bili_chat.DAILY_CAP)
    r, exc = call_safely("有没有机械键盘的视频")
    check("没抛异常", exc is None, exc)
    check("action=skip", (r or {}).get("action") == "skip", r)
    check("reason=daily_cap", (r or {}).get("reason") == "daily_cap", r)
    check("没发搜索", STATE["search"] == 0, STATE["keywords"])
    check("没调 add", STATE["add"] == 0, STATE["adds"])


def test_rules():
    print("\n[11] 信号 / 抽词规则表（纯函数，零请求）")
    signal_cases = [
        ("在吗", ""),
        ("吃饭了吗", ""),
        ("哈哈", ""),
        ("这个教程讲得真好", ""),          # 有"教程"，但没有想要的意思
        ("B站又崩了", ""),                  # 有平台名，但不是在问视频
        ("晚上打个视频电话吧", ""),          # 微信视频通话，不是 B 站
        ("有没有视频通话的教程", ""),
        ("有没有机械键盘的视频", "video"),
        ("推荐个视频", "video"),             # 有信号，但后面抽不出词
        ("B站上怎么装机", "platform"),
        ("怎么学吉他", "learn"),
    ]
    for text, want in signal_cases:
        got = bili_chat.detect_signal(text)
        check(f"信号「{text}」-> {want or '无'}", got == want, got)

    keyword_cases = [
        ("有没有机械键盘的视频", "机械键盘"),
        ("B站上怎么装机", "装机"),
        ("怎么学吉他", "吉他"),
        ("我想学做菜", "做菜"),
        ("有没有python教程", "python"),
        ("机械键盘视频有吗", "机械键盘"),
        ("有没有适合新手的机械键盘", "机械键盘"),   # 名词短语中心语在后
        ("我朋友想学吉他", "吉他"),                 # 人称代词打头的碎片要输
        ("推荐个视频", ""),                         # 兜底：抽不出就跳过
        ("有没有好玩的视频", ""),                   # 泛词不是主题
        ("有没有猫的视频", ""),                     # 单字主语太宽泛，宁可不收
        ("B站怎么投币", ""),                        # 站内操作，没有主题
        ("在吗", ""),
    ]
    for text, want in keyword_cases:
        got = bili_chat.extract_keyword(text)
        check(f"抽词「{text}」-> {want or '空'}", got == want, got)


def main():
    tests = (
        test_chitchat, test_happy_path, test_empty_results, test_zero_play,
        test_add_raises, test_add_systemexit, test_dry_run, test_duplicate_message,
        test_search_error, test_daily_cap, test_rules,
    )
    try:
        for t in tests:
            t()
    finally:
        restore()
    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
