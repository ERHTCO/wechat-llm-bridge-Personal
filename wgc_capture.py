"""用 Windows.Graphics.Capture 抓窗口画面。

和截屏的区别：
  - 抓的是 DWM 合成后的窗口 surface，**不需要窗口在前台**
  - 被别的窗口遮挡也能拿到内容
  - 不碰剪贴板、不抢焦点

限制（待实测确认）：最小化的窗口大概率是黑帧。

依赖：pip install windows-capture
"""
import io
import threading


def _bgr_to_png(bgr):
    from PIL import Image

    # windows-capture 2.0.1 的 convert_to_bgr() 返回 Frame，要取 .frame_buffer
    buf_arr = getattr(bgr, "frame_buffer", bgr)
    # convert_to_bgr 给的是 numpy 的 BGR 数组
    img = Image.fromarray(buf_arr[:, :, ::-1])
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def capture_window_png(hwnd, timeout=4.0):
    """抓这个窗口当前的一帧，返回 (PNG字节, 错误说明)。

    失败返回 (None, 原因)，不抛异常。
    """
    try:
        from windows_capture import WindowsCapture
    except Exception as exc:
        return None, f"未安装 windows-capture: {exc}"

    holder = {}

    def run():
        try:
            cap = WindowsCapture(window_hwnd=int(hwnd),
                                 cursor_capture=False)

            @cap.event
            def on_frame_arrived(frame, capture_control):
                try:
                    if holder.get("png") is None:
                        holder["png"] = _bgr_to_png(frame.convert_to_bgr())
                except Exception as exc:
                    holder["error"] = f"转图失败: {exc}"
                finally:
                    try:
                        capture_control.stop()
                    except Exception:
                        pass

            @cap.event
            def on_closed():
                holder.setdefault("closed", True)

            cap.start()
        except Exception as exc:
            holder["error"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    png = holder.get("png")
    if png:
        return png, ""
    if holder.get("error"):
        return None, holder["error"]
    if t.is_alive():
        return None, "超时：没等到帧（可能窗口被最小化，或该窗口不支持捕获）"
    return None, "没拿到帧"


def capture_bbox(hwnd, bbox, padding=8):
    """抓窗口一帧，并按屏幕坐标 bbox 裁出那一块。返回 (PNG字节, 错误说明)。

    用途：替掉「截屏兜底」。截屏抓的是屏幕像素，窗口被遮挡或不在前台时
    会把桌面/别的窗口发给模型；WGC 抓的是窗口自己的 surface，不会有这个问题。
    """
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    rect = wintypes.RECT()
    if not u.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None, "拿不到窗口位置"
    ww, wh = rect.right - rect.left, rect.bottom - rect.top
    if ww <= 0 or wh <= 0:
        return None, "窗口尺寸异常"

    png, err = capture_window_png(hwnd)
    if not png:
        return None, err

    try:
        from PIL import Image

        img = Image.open(io.BytesIO(png))
        fw, fh = img.size
        # 窗口矩形 → 截图像素的比例（可能有 DPI 缩放或边框差异）
        sx, sy = fw / ww, fh / wh
        bl, bt, br, bb = [int(v) for v in bbox]
        left = max(0, int((bl - rect.left - padding) * sx))
        top = max(0, int((bt - rect.top - padding) * sy))
        right = min(fw, int((br - rect.left + padding) * sx))
        bottom = min(fh, int((bb - rect.top + padding) * sy))
        if right - left < 8 or bottom - top < 8:
            return None, "裁剪区域太小"
        crop = img.crop((left, top, right, bottom))
        buf = io.BytesIO()
        crop.save(buf, "PNG")
        return buf.getvalue(), ""
    except Exception as exc:
        return None, f"裁剪失败: {exc}"


def _main():
    """命令行自测：python wgc_capture.py <窗口标题的一部分>"""
    import sys
    import time

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    import wxuia

    want = sys.argv[1] if len(sys.argv) > 1 else ""
    target = None
    for hwnd, title, visible in wxuia.enum_qt_windows():
        if want and want not in title:
            continue
        if title in ("Weixin", "WxTrayIconMessageWindow", ""):
            continue
        target = (hwnd, title, visible)
        break
    if not target:
        print("没找到符合条件的窗口")
        return 1

    hwnd, title, visible = target
    print(f"目标：{title!r} hwnd={hwnd} 可见={visible}")
    t0 = time.time()
    png, err = capture_window_png(hwnd)
    dt = int((time.time() - t0) * 1000)
    if not png:
        print(f"失败（{dt}ms）：{err}")
        return 1

    from PIL import Image
    img = Image.open(__import__("io").BytesIO(png))
    gray = img.convert("L")
    hist = gray.histogram()
    colors = sum(1 for c in hist if c)
    print(f"成功（{dt}ms）：{img.size[0]}x{img.size[1]}，{len(png)} 字节，灰度不同取值 {colors} 种")
    print("（颜色种类很少 = 可能是黑帧）")
    out = __import__("pathlib").Path(__file__).resolve().parent / "wgc_probe.png"
    out.write_bytes(png)
    print("已保存", out.name)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
