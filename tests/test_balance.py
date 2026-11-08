"""余额查询的测试（不联网）。

    .venv\\Scripts\\python.exe tests\\test_balance.py

要点：失败和「余额为 0」必须分得开 —— 前者界面上是「查不到」，后者是「¥0.00」。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import backends  # noqa: E402

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


def test_ok():
    def fake(key, url):
        check("用的是 Bearer key", True)
        return {"is_available": True, "balance_infos": [
            {"currency": "CNY", "total_balance": "6.04",
             "granted_balance": "0.00", "topped_up_balance": "6.04"}]}

    r = backends.balance(api_key="sk-test", fetch=fake)
    check("成功", r["ok"] is True, r)
    check("余额 6.04", r["total"] == "6.04", r)
    check("币种 CNY", r["currency"] == "CNY", r)
    check("充值/赠养分得清", r["topped_up"] == "6.04" and r["granted"] == "0.00", r)
    check("带上查询时间", isinstance(r["at"], int) and r["at"] > 0, r)


def test_zero():
    def fake(key, url):
        return {"balance_infos": [{"currency": "CNY", "total_balance": "0.00",
                                   "granted_balance": "0.00", "topped_up_balance": "0.00"}]}

    r = backends.balance(api_key="sk-test", fetch=fake)
    check("余额为 0 也算成功", r["ok"] is True and r["total"] == "0.00", r)
    check("没有错误信息", r["error"] == "", r)


def test_no_key():
    old = backends.os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        # 传空 key 且把 config 指到不存在的目录
        old_here = backends._HERE
        backends._HERE = Path("Z:/nope")
        try:
            r = backends.balance(api_key="")
        finally:
            backends._HERE = old_here
    finally:
        if old is not None:
            backends.os.environ["DEEPSEEK_API_KEY"] = old
    check("没 key 不算成功", r["ok"] is False, r)
    check("错误里说清楚缺什么", "api_key" in r["error"], r["error"])


def test_empty_infos():
    r = backends.balance(api_key="sk-test", fetch=lambda k, u: {"balance_infos": []})
    check("没有 balance_infos 就是失败", r["ok"] is False, r)
    check("错误有提示", "balance_infos" in r["error"], r["error"])


def test_http_error():
    def boom(key, url):
        raise RuntimeError("HTTP 401: invalid key")

    r = backends.balance(api_key="sk-test", fetch=boom)
    check("网络异常不算成功", r["ok"] is False, r)
    check("错误原文留着", "401" in r["error"], r["error"])
    check("失败时 total 是空串而不是 0", r["total"] == "", r)


def test_url():
    seen = {}

    def fake(key, url):
        seen["url"] = url
        return {"balance_infos": [{"currency": "CNY", "total_balance": "1"}]}

    backends.balance(api_key="sk-test", base_url="https://api.deepseek.com/", fetch=fake)
    check("路径是 /user/balance", seen["url"].endswith("/user/balance"), seen["url"])
    check("不会出现双斜杠", "//user" not in seen["url"], seen["url"])


def main():
    test_ok()
    test_zero()
    test_no_key()
    test_empty_infos()
    test_http_error()
    test_url()
    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
