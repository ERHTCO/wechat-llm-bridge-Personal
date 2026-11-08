"""发图前的格式兜底。

微信的输入框只认常见位图。塞个 webp/heic 进去，剪贴板会「成功」，
但粘贴后是空白 —— 报错只会说「没发出去」，查半天才知道是格式问题。
抖音那个桥接项目里踩的是同一类坑（发图不认 webp，且报错完全不提格式），
它的结论很直接：**发之前转成对方认的格式，别指望错误信息告诉你为什么**。

做法：扩展名不在白名单里就先用 Qt 解码，再存成 PNG 发出去。解不开就报
「格式不支持：xxx」，把扩展名带上。
"""
import os
import tempfile
from pathlib import Path

# 微信输入框直接认的
SAFE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}


class ImageFormatError(ValueError):
    """这张图发不了，错在格式。"""


def ext_of(path):
    return Path(str(path)).suffix.lower()


def needs_convert(path):
    """要不要转。没有扩展名的也转（猜不出来就交给解码器判断）。"""
    return ext_of(path) not in SAFE_EXT


def _to_png(src, dst):
    """用 Qt 解码再存 PNG。Qt 是必装依赖（界面在用），不额外引 Pillow。"""
    from PySide6.QtGui import QImage

    img = QImage(str(src))
    if img.isNull():
        raise ImageFormatError(f"格式不支持：{ext_of(src) or Path(str(src)).name}")
    if not img.save(str(dst), "PNG"):
        raise ImageFormatError(f"转 PNG 失败：{Path(str(src)).name}")


def prepare(path, tmp_dir=None, converter=None):
    """返回 (可以贴剪贴板的路径, 说明)。不需要转就原样返回，说明是空串。

    转出来的文件放 tmp_dir（默认系统临时目录下一个固定子目录），
    **不动原文件** —— 用户可能还要用。
    """
    p = Path(str(path))
    if not p.exists():
        raise ImageFormatError(f"文件不存在：{p.name}")
    if not needs_convert(p):
        return p, ""

    out_dir = Path(tmp_dir) if tmp_dir else Path(tempfile.gettempdir()) / "wxbridge-imgfmt"
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / (p.stem + ".png")
    if dst.exists() and dst.stat().st_mtime >= p.stat().st_mtime:
        return dst, f"{ext_of(p)} → png（用过缓存）"

    (converter or _to_png)(p, dst)
    return dst, f"{ext_of(p)} → png（微信不认这个格式）"


def main():
    import argparse

    ap = argparse.ArgumentParser(description="发图前的格式检查/转换")
    ap.add_argument("path")
    ap.add_argument("--keep", action="store_true", help="只检查，不真的转")
    args = ap.parse_args()
    if args.keep:
        print("需要转换" if needs_convert(args.path) else "可以直接发", args.path)
        return 0
    out, note = prepare(args.path)
    print(out)
    if note:
        print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
