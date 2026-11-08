"""配色的测试。

    .venv\\Scripts\\python.exe tests\\test_themes.py

换主题最容易出的错是「少定义了一个颜色」——QSS 里 KeyError 直接把窗口崩掉，
或者某块地方还留着上一套的深色。所以这里逐套检查键齐不齐、QSS 能不能铺出来。
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import themes  # noqa: E402

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


def test_keys():
    for name in themes.names():
        c = themes.get(name)
        missing = [k for k in themes.KEYS if k not in c]
        check(f"{name} 颜色齐全", not missing, missing)
        extra = [k for k in c if k not in themes.KEYS]
        check(f"{name} 没有多余的键", not extra, extra)


def test_qss():
    for name in themes.names():
        q = themes.qss(themes.get(name))
        check(f"{name} QSS 能生成", len(q) > 800, len(q))
        check(f"{name} 没留花括号", "{{" not in q and "}}" not in q)
        check(f"{name} 用上了自己的底色", themes.get(name)["bg"] in q, "")
        check(f"{name} 有 Log 样式", "#Log " in q, "")


def test_cycle():
    seen = [themes.DEFAULT]
    for _ in range(len(themes.ORDER) - 1):
        seen.append(themes.next_name(seen[-1]))
    check("循环覆盖了每一套", sorted(seen) == sorted(themes.ORDER), seen)
    check("转一圈回到起点", themes.next_name(seen[-1]) == themes.DEFAULT, seen)
    check("不认识的名字给默认", themes.next_name("乱写") == themes.ORDER[1],
          themes.next_name("乱写"))


def test_get():
    check("get(None) 给默认", themes.get(None) is themes.get(themes.DEFAULT))
    check("get(乱写) 给默认", themes.get("乱写") is themes.get(themes.DEFAULT))
    check("get 认识的名字原样给", themes.get("米白")["bg"].startswith("#f"))


def test_save_load():
    real = themes.CONFIG
    d = Path(tempfile.mkdtemp(prefix="wxtheme-"))
    themes.CONFIG = d / "config.json"
    try:
        check("没有配置时给默认", themes.saved() == themes.DEFAULT, themes.saved())
        check("写进配置", themes.save("米白") is True)
        check("读得回来", themes.saved() == "米白", themes.saved())
        cfg = json.loads(themes.CONFIG.read_text(encoding="utf-8"))
        check("只加了 ui_theme", cfg == {"ui_theme": "米白"}, cfg)
        themes.save("夜蓝")
        check("换一套也记住", themes.saved() == "夜蓝", themes.saved())
        check("不认识的拒绝写入", themes.save("乱写") is False)
        check("配置没被写坏", themes.saved() == "夜蓝", themes.saved())

        # 已有配置不能丢
        themes.CONFIG.write_text(json.dumps({"api_key": "sk-x", "ui_theme": "米白"}),
                                 encoding="utf-8")
        themes.save("深色")
        cfg = json.loads(themes.CONFIG.read_text(encoding="utf-8"))
        check("别的设置没被冲掉", cfg.get("api_key") == "sk-x", cfg)
        check("主题改了", cfg.get("ui_theme") == "深色", cfg)
    finally:
        themes.CONFIG = real


def test_saved_bad_config():
    real = themes.CONFIG
    d = Path(tempfile.mkdtemp(prefix="wxtheme2-"))
    themes.CONFIG = d / "config.json"
    try:
        themes.CONFIG.write_text("这不是 json", encoding="utf-8")
        check("配置坏了也给默认", themes.saved() == themes.DEFAULT, themes.saved())
        themes.CONFIG.write_text(json.dumps({"ui_theme": "不存在的"}), encoding="utf-8")
        check("主题名不认识给默认", themes.saved() == themes.DEFAULT, themes.saved())
    finally:
        themes.CONFIG = real


def main():
    test_keys()
    test_qss()
    test_cycle()
    test_get()
    test_save_load()
    test_saved_bad_config()
    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
