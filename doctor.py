"""一键体检：把当前环境状态全查一遍，写进 体检报告.txt。

不需要懂任何东西，双击 体检.bat 即可。报告我来看。
"""
import ctypes
import json
import subprocess
import sys
import urllib.request
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "体检报告.txt"
_lines = []


def say(msg=""):
    print(msg)
    _lines.append(str(msg))


def section(title):
    say()
    say("=" * 60)
    say(title)
    say("=" * 60)


def check_version():
    section("1. 微信版本")
    exe = Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe")
    if not exe.exists():
        say("[X] 找不到 Weixin.exe")
        return
    try:
        import win32api
        info = win32api.GetFileVersionInfo(str(exe), "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        ver = f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
        say(f"当前版本: {ver}")
        say("期望: 4.1.8.107" + ("  [OK]" if ver == "4.1.8.107" else "  [!!] 版本不对"))
    except Exception as exc:
        say(f"读版本失败: {exc}")

    root = Path(r"C:\Program Files\Tencent\Weixin")
    dirs = [d.name for d in root.iterdir() if d.is_dir()] if root.exists() else []
    say(f"版本目录: {dirs}")


def check_update_block():
    section("2. 自动更新封锁")
    root = Path(r"C:\Program Files\Tencent\Weixin")
    found_any = False
    for d in root.iterdir() if root.exists() else []:
        if not d.is_dir():
            continue
        exe = d / "WeixinUpdate.exe"
        bak = d / "WeixinUpdate.exe.bak"
        if exe.exists():
            say(f"[!!] {d.name}\\WeixinUpdate.exe 还在（没改名）")
            found_any = True
        if bak.exists():
            say(f"[OK] {d.name}\\WeixinUpdate.exe.bak 已改名")
            found_any = True
    if not found_any:
        say("[?] 没找到更新器文件")

    try:
        root2 = Path(r"C:\Program Files\Tencent\Weixin")
        vers = [d.name for d in root2.iterdir() if d.is_dir()] if root2.exists() else []
        hits, need_admin = [], False
        for ver in vers:
            rule = f"BlockWxUpd {ver}"
            proc = subprocess.run(
                f'netsh advfirewall firewall show rule name="{rule}"',
                shell=True, capture_output=True, text=True,
                encoding="gbk", errors="replace", timeout=30,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            if rule in out:
                hits.append(rule)
            elif "elevation" in out or "管理员" in out:
                need_admin = True
        if hits:
            say(f"防火墙规则: {hits}")
        elif need_admin:
            say("防火墙规则: [?] 需要管理员权限才能查（用「以管理员身份运行」再跑一次）")
        else:
            say("防火墙规则: [X] 没有")
    except Exception as exc:
        say(f"查防火墙失败: {exc}")

    hosts = Path(r"C:\Windows\System32\drivers\etc\hosts")
    try:
        text = hosts.read_text(encoding="utf-8", errors="replace")
        hits = [ln for ln in text.splitlines() if "dldir" in ln]
        say(f"hosts: {hits if hits else '[X] 没有 dldir 条目'}")
    except Exception as exc:
        say(f"读 hosts 失败: {exc}")


def qt_windows():
    user32 = ctypes.windll.user32
    rows = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if not cls.value.startswith("Qt"):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        rows.append((hwnd, buf.value, cls.value, bool(user32.IsWindowVisible(hwnd)),
                     r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumWindows(cb, 0)
    return rows


def check_uia():
    section("3. 控件可读性（关键）")
    try:
        import uiautomation as auto
    except ImportError:
        say("[X] uiautomation 没装")
        return

    auto.SetGlobalSearchTimeout(3)
    wins = qt_windows()
    say(f"发现 {len(wins)} 个 Qt 窗口")
    readable = 0
    for hwnd, title, cls, visible, w, h in wins:
        if "Tray" in cls:
            continue
        try:
            ctrl = auto.ControlFromHandle(hwnd)
            uia_cls = ctrl.ClassName or ""
            kids = len(ctrl.GetChildren())
        except Exception as exc:
            say(f"  hwnd={hwnd} title={title!r} 读取失败: {exc}")
            continue
        say(f"  hwnd={hwnd} title={title!r} uia_class={uia_cls!r} 子控件={kids} 可见={visible}")
        if kids > 0 and title not in ("微信", "Weixin"):
            readable += 1

    say()
    if readable:
        say(f"[OK] {readable} 个聊天窗口可读，控件正常")
    else:
        say("[X] 没有任何聊天窗口可读")
        say("     -> 微信版本可能又升级了，需要降到 4.1.8.107")
        say("     -> 或者你没有把会话双击成独立窗口")


def check_bridge():
    section("4. 桥（bridge.py）")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        say(f"[OK] 桥在跑: {data}")
    except Exception as exc:
        say(f"[X] 桥没在跑: {exc}")
        say("     -> 双击 start-bridge.bat")


def check_files():
    section("5. 配置速览")
    cfg = HERE / "config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            say(f"backend={data.get('backend')} model={data.get('api_model')}")
            say(f"private_all={data.get('private_all')} rooms={list((data.get('rooms') or {}).keys())}")
        except Exception as exc:
            say(f"config.json 读取失败: {exc}")
    say(f"prompt.txt 存在: {(HERE / 'prompt.txt').exists()}")
    imgs = HERE / "images"
    if imgs.is_dir():
        names = [p.name for p in imgs.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg")]
        say(f"images/ 里有 {len(names)} 张图: {names[:5]}")


def main():
    say(f"体检时间: {__import__('time').strftime('%Y-%m-%d %H:%M:%S')}")
    check_version()
    check_update_block()
    check_uia()
    check_bridge()
    check_files()
    section("结论")
    say("把 体检报告.txt 发给助手就行")
    OUT.write_text("\n".join(_lines), encoding="utf-8")
    print(f"\n已写入 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
