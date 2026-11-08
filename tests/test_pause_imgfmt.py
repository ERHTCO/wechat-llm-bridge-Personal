"""两个新兜底的测试：休眠判定、发图格式。

    .venv\\Scripts\\python.exe tests\\test_pause_imgfmt.py

都不碰微信、不碰 UIA：pause 只做算术，imgfmt 用 Qt 在临时目录里真转一张图。
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import imgfmt  # noqa: E402
import pause  # noqa: E402

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


# ---------------------------------------------------------------- pause
def test_slept():
    check("都走了 100 秒 → 没睡", pause.slept_seconds(100_000, 100.0) == 0.0,
          pause.slept_seconds(100_000, 100.0))
    check("墙钟多走 60 秒 → 睡了 60", pause.slept_seconds(160_000, 100.0) == 60.0,
          pause.slept_seconds(160_000, 100.0))
    check("时钟精度误差按 0 算", pause.slept_seconds(99_900, 100.0) == 0.0,
          pause.slept_seconds(99_900, 100.0))
    check("垃圾输入不炸", pause.slept_seconds(None, None) == 0.0)


def test_gap_kind():
    check("真睡了 → resume", pause.gap_kind(120, 120) == "resume", pause.gap_kind(120, 120))
    check("没睡但卡了 120 秒 → slow", pause.gap_kind(120, 0) == "slow", pause.gap_kind(120, 0))
    check("正常 3 秒 → normal", pause.gap_kind(3, 0) == "normal", pause.gap_kind(3, 0))
    check("卡 30 秒还没到门槛 → normal", pause.gap_kind(30, 0) == "normal", pause.gap_kind(30, 0))
    check("睡 95 秒刚好过线 → resume", pause.gap_kind(95, 95) == "resume")
    check("睡 89 秒不算 → 但间隔大算 slow", pause.gap_kind(89, 89) == "slow", pause.gap_kind(89, 89))
    check("负数当 0", pause.gap_kind(-5, -5) == "normal")


# ---------------------------------------------------------------- imgfmt
def make_png(path):
    from PySide6.QtGui import QImage

    img = QImage(8, 8, QImage.Format_RGB32)
    img.fill(0x336699)
    assert img.save(str(path), "PNG")


def test_ext():
    check(".png 不用转", not imgfmt.needs_convert("a.png"))
    check(".JPG 大写也认", not imgfmt.needs_convert("a.JPG"))
    check(".webp 要转", imgfmt.needs_convert("a.webp"))
    check(".heic 要转", imgfmt.needs_convert("a.heic"))
    check("没有扩展名也要转", imgfmt.needs_convert("下载的文件"))


def test_prepare_passthrough():
    d = Path(tempfile.mkdtemp(prefix="wxfmt-"))
    p = d / "ok.png"
    make_png(p)
    out, note = imgfmt.prepare(p)
    check("png 原样返回", out == p and note == "", (out, note))


def test_prepare_convert():
    d = Path(tempfile.mkdtemp(prefix="wxfmt2-"))
    src = d / "图.webp"          # 内容是 PNG 字节，扩展名是 webp
    make_png(src)
    out, note = imgfmt.prepare(src, tmp_dir=d / "out")
    check("转出来了", out.exists() and out.suffix == ".png", out)
    check("说明里写了 webp", "webp" in note, note)
    check("原文件还在", src.exists())
    check("转出来的是真 PNG", out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n",
          out.read_bytes()[:8])
    again, note2 = imgfmt.prepare(src, tmp_dir=d / "out")
    check("第二次走缓存", "缓存" in note2, note2)
    check("还是同一个文件", again == out, (again, out))


def test_prepare_bad():
    d = Path(tempfile.mkdtemp(prefix="wxfmt3-"))
    bad = d / "坏的.heic"
    bad.write_bytes(b"not an image at all")
    try:
        imgfmt.prepare(bad, tmp_dir=d / "out")
        check("解不开要报错", False)
    except imgfmt.ImageFormatError as exc:
        check("解不开要报错", True)
        check("错误里带扩展名", ".heic" in str(exc), str(exc))


def test_prepare_missing():
    try:
        imgfmt.prepare("Z:/nope/x.png")
        check("文件不存在要报错", False)
    except imgfmt.ImageFormatError as exc:
        check("文件不存在要报错", "不存在" in str(exc), str(exc))


def main():
    test_slept()
    test_gap_kind()
    test_ext()
    test_prepare_passthrough()
    test_prepare_convert()
    test_prepare_bad()
    test_prepare_missing()
    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
