"""「发完核对」的测试。

    .venv\\Scripts\\python.exe tests\\test_send_confirm.py

学的是 dsh-douyin-bridge 里的教训：**输入框空了只是代理信号**，
真正要看的是消息列表里有没有多出这条。这里不碰 UIA，只测判据本身。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import wxuia  # noqa: E402

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


def items(*texts, time_marks=()):
    """造 read_messages 那样的列表。"""
    out = [{"text": t, "is_time": False} for t in texts]
    for i in time_marks:
        out.insert(i, {"text": "10:24", "is_time": True})
    return out


def reader_of(row):
    def read(win, hwnd):
        if isinstance(row, Exception):
            raise row
        return row
    return read


def test_norm():
    check("空白全去掉", wxuia._norm_cmp(" 你 好\n世界 ") == "你好世界",
          wxuia._norm_cmp(" 你 好\n世界 "))
    check("空值不炸", wxuia._norm_cmp(None) == "" and wxuia._norm_cmp("") == "")


def test_match():
    check("完全一样", wxuia._matches_sent(["在吗", "到了"], "到了"))
    check("换行/空格不同也算", wxuia._matches_sent(["我已经 到了\n楼下"], "我已经到了楼下"))
    check("被截断的长消息也算", wxuia._matches_sent(["这是一条很长很长的消息…"], "这是一条很长很长的消息，后面还有"))
    check("没出现就是 False", not wxuia._matches_sent(["在吗", "好的"], "到了"))
    check("空文本不匹配", not wxuia._matches_sent(["在吗"], ""))
    check("列表空不匹配", not wxuia._matches_sent([], "到了"))
    check("列表里有空串也不炸", not wxuia._matches_sent([""], "到了"))


def test_confirm_present():
    got = wxuia.confirm_sent(None, None, "到了", timeout=0,
                             reader=reader_of(items("在吗", "到了")))
    check("列表里有 → True", got is True, got)


def test_confirm_missing():
    got = wxuia.confirm_sent(None, None, "到了", timeout=0,
                             reader=reader_of(items("在吗", "好的")))
    check("列表里没有 → False", got is False, got)


def test_confirm_unknown():
    got = wxuia.confirm_sent(None, None, "到了", timeout=0, reader=reader_of([]))
    check("读不到列表 → None", got is None, got)
    got = wxuia.confirm_sent(None, None, "到了", timeout=0,
                             reader=reader_of(RuntimeError("UIA 炸了")))
    check("读的时候抛异常 → None", got is None, got)


def test_confirm_ignores_time_marks():
    rows = items("在吗", "到了", time_marks=(1,))
    got = wxuia.confirm_sent(None, None, "到了", timeout=0, reader=reader_of(rows))
    check("时间分隔行不影响判断", got is True, got)


def test_retry_until_appears():
    """第一次还没刷出来，之后出现了 —— 要等到，不能立刻判失败。"""
    calls = {"n": 0}

    def read(win, hwnd):
        calls["n"] += 1
        return items("在吗") if calls["n"] < 3 else items("在吗", "到了")

    got = wxuia.confirm_sent(None, None, "到了", timeout=3.0, reader=read)
    check("会重试到看见为止", got is True, got)
    check("确实重试了", calls["n"] >= 3, calls["n"])


def main():
    test_norm()
    test_match()
    test_confirm_present()
    test_confirm_missing()
    test_confirm_unknown()
    test_confirm_ignores_time_marks()
    test_retry_until_appears()
    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
