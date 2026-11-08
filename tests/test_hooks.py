"""钩子的回归测试。

    .venv\\Scripts\\python.exe tests\\test_hooks.py

不变量：钩子出错不能连累回复，也不能连累别的钩子；
执行顺序就是注册顺序；两个阶段的参数各管各的。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import hooks  # noqa: E402

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


def test_unknown_stage():
    try:
        hooks.register("on_llm_request", "x", lambda **kw: None)
        check("未知阶段要报错", False)
    except ValueError:
        check("未知阶段要报错", True)
    try:
        hooks.run("on_llm_request")
        check("跑未知阶段也要报错", False)
    except ValueError:
        check("跑未知阶段也要报错", True)


def test_order_and_merge():
    hooks.clear()
    seen = []
    hooks.register("before_reply", "a", lambda **kw: seen.append("a") or {"k": 1})
    hooks.register("before_reply", "b", lambda **kw: seen.append("b") or {"j": 2})
    merged = hooks.collect("before_reply", wxid="wxid_alice", text="hi")
    check("按注册顺序执行", seen == ["a", "b"], seen)
    check("返回值被合并", merged == {"k": 1, "j": 2}, merged)
    out = hooks.run("before_reply")
    check("run 保留每个钩子的键", set(out) == {"a", "b"}, out)


def test_isolation():
    hooks.clear()

    def boom(**kw):
        raise RuntimeError("炸了")

    hooks.register("after_reply", "boom", boom)
    hooks.register("after_reply", "ok", lambda **kw: {"fine": True})
    out = hooks.run("after_reply", text="t")
    check("出错的钩子记 None", out.get("boom") is None, out)
    check("后面的钩子照跑", out.get("ok") == {"fine": True}, out)
    check("没有异常抛出去", True)


def test_replace_same_name():
    hooks.clear()
    calls = []
    hooks.register("before_reply", "m", lambda **kw: calls.append(1))
    hooks.register("before_reply", "m", lambda **kw: calls.append(2))
    hooks.run("before_reply")
    check("同名注册是替换不是叠加", calls == [2], calls)
    check("名字只有一份", hooks.names("before_reply") == ["m"])


def test_params_passed():
    hooks.clear()
    got = {}

    def grab(**kw):
        got.update(kw)
        return None

    hooks.register("after_reply", "grab", grab)
    hooks.run("after_reply", wxid="林岸", text="在吗", reply="在呢",
              msg_ids=[1, 2], history_rows=[])
    check("参数原样传进来", got.get("wxid") == "林岸" and got.get("reply") == "在呢", got)
    check("msg_ids 也在", got.get("msg_ids") == [1, 2], got)


def test_unregister_and_clear():
    hooks.clear()
    hits = []
    hooks.register("before_reply", "a", lambda **kw: hits.append("a"))
    hooks.register("before_reply", "b", lambda **kw: hits.append("b"))
    hooks.unregister("before_reply", "a")
    hooks.run("before_reply")
    check("unregister 生效", hits == ["b"], hits)
    hooks.clear()
    check("clear 清空", hooks.names("before_reply") == [])
    check("另一个阶段不受影响", hooks.names("after_reply") == [])


def test_bad_register():
    hooks.clear()
    try:
        hooks.register("before_reply", "x", "不是函数")
        check("非 callable 要拒绝", False)
    except TypeError:
        check("非 callable 要拒绝", True)


def test_collect_skips_non_dict():
    hooks.clear()
    hooks.register("before_reply", "none", lambda **kw: None)
    hooks.register("before_reply", "str", lambda **kw: "字符串")
    hooks.register("before_reply", "dict", lambda **kw: {"memory_text": "记得"})
    got = hooks.collect("before_reply")
    check("只合并 dict", got == {"memory_text": "记得"}, got)


def main():
    test_unknown_stage()
    test_order_and_merge()
    test_isolation()
    test_replace_same_name()
    test_params_passed()
    test_unregister_and_clear()
    test_bad_register()
    test_collect_skips_non_dict()
    hooks.clear()
    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
