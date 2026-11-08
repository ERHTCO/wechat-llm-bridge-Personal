"""wbi 签名的回归测试。

    .venv\\Scripts\\python.exe tests\\test_wbi.py

签名必须稳定：参数一样、时间一样，签出来的 w_rid 必须一模一样。
下面两组期望值是从真实接口上跑出来的（拿 nav 的 key 算的），
不是从算法自己推的 —— 否则算法错了测试也跟着错。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import wbi  # noqa: E402

PASS = 0
FAIL = 0

IMG = "7cd084941338484aae1ad9425b84077c"
SUB = "4932caff0ff746eab6f01bf08b70ac45"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def test_mixin():
    check("mixin key 是 32 位", len(wbi.mixin_key(IMG, SUB)) == 32,
          len(wbi.mixin_key(IMG, SUB)))
    check("mixin key 固定", wbi.mixin_key(IMG, SUB) == "ea1db124af3c7062474693fa704f4ff8",
          wbi.mixin_key(IMG, SUB))
    check("表一共 64 项", len(wbi.MIXIN_TABLE) == 64, len(wbi.MIXIN_TABLE))
    check("表里没有重复", len(set(wbi.MIXIN_TABLE)) == 64)


def test_sign():
    got = wbi.sign({"oid": 1, "type": 1}, IMG, SUB, now=1700000000)
    check("带上 wts", got["wts"] == 1700000000, got)
    check("签名固定", got["w_rid"] == "c4bc0fb138c1bbd28b3bf00535c0559f", got["w_rid"])


def test_clean():
    got = wbi.sign({"q": "a!b'c(d)e*f", "oid": 1}, IMG, SUB, now=1700000000)
    check("值里的 !'()* 被去掉", got["q"] == "abcdef", got["q"])


def test_order_matters():
    a = wbi.sign({"a": 1, "b": 2}, IMG, SUB, now=1700000000)
    b = wbi.sign({"b": 2, "a": 1}, IMG, SUB, now=1700000000)
    check("参数字典顺序不影响签名", a["w_rid"] == b["w_rid"], (a, b))


def test_no_mutate():
    src = {"oid": 1}
    wbi.sign(src, IMG, SUB, now=1700000000)
    check("不改调用方的字典", src == {"oid": 1}, src)


def test_keys_cache(tmp_path=None):
    import tempfile
    from pathlib import Path as P

    d = P(tempfile.mkdtemp(prefix="wxwbi-"))
    real = wbi.KEYS_CACHE
    wbi.KEYS_CACHE = d / "k.json"
    calls = []

    def fake(sess):
        calls.append(1)
        return {"wbi_img": {"img_url": f"https://x/{IMG}.png",
                            "sub_url": f"https://x/{SUB}.png"}}

    try:
        a = wbi.keys(now=1000, fetch=fake)
        b = wbi.keys(now=1100, fetch=fake)
        check("第一次真的请求了", len(calls) == 1, calls)
        check("第二次走缓存", len(calls) == 1, calls)
        check("key 对得上", a == (IMG, SUB) == b, (a, b))
        c = wbi.keys(now=1000 + 9999, fetch=fake)
        check("过期后重新取", len(calls) == 2, calls)
        check("重取还是对的", c == (IMG, SUB), c)
    finally:
        wbi.KEYS_CACHE = real


def test_keys_bad():
    import tempfile
    from pathlib import Path as P

    # 必须换个空缓存：真缓存里有 key 的话，这个用例根本走不到 fetch
    d = P(tempfile.mkdtemp(prefix="wxwbi-bad-"))
    real = wbi.KEYS_CACHE
    wbi.KEYS_CACHE = d / "k.json"

    def fake(sess):
        return {"wbi_img": {}}

    try:
        wbi.keys(now=1, fetch=fake)
        check("没有 key 要抛错", False)
    except RuntimeError:
        check("没有 key 要抛错", True)
    finally:
        wbi.KEYS_CACHE = real


def test_signed_url():
    url = wbi.signed_url("https://api.bilibili.com/x/y",
                         {"oid": 1, "type": 1}, now=1700000000,
                         fetch=lambda s: {"wbi_img": {
                             "img_url": f"https://x/{IMG}.png",
                             "sub_url": f"https://x/{SUB}.png"}})
    check("带上了 w_rid", "w_rid=" in url, url)
    check("带上了 wts", "wts=1700000000" in url, url)
    check("参数也在", "oid=1" in url and "type=1" in url, url)


def main():
    test_mixin()
    test_sign()
    test_clean()
    test_order_matters()
    test_no_mutate()
    test_keys_cache()
    test_keys_bad()
    test_signed_url()
    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
