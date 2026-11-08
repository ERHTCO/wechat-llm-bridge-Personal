"""B站扫码登录：生成二维码 → 你扫 → 我存会话。

为什么不让你给密码或粘 cookie：
  扫码拿到的是可随时在 App 里踢掉的会话，不用交出密码；
  SESSDATA 也不会出现在聊天记录里。

    python bili_login.py            # 生成二维码并等待扫码
    python bili_login.py --check    # 只验证现有会话还有没有效

会话存在 data/bili_session.json（data/ 在 .gitignore 里，不进版本库）。
"""
import argparse
import http.cookiejar
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import os as _os

# 打包成 exe 之后 __file__ 在 _internal 里，data/ 却在 exe 旁边。
# 必须和 bridge/proactive 用同一套根目录，否则读不到会话和配置。
_HERE = Path(_os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
DATA = _HERE / "data"
SESSION = DATA / "bili_session.json"
QR_PNG = DATA / "bili_qr.png"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}

GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV = "https://api.bilibili.com/x/web-interface/nav"


def opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def get_json(op, url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def save_session(jar, extra=None):
    cookies = {c.name: c.value for c in jar}
    keep = {k: cookies[k] for k in ("SESSDATA", "bili_jct", "DedeUserID", "buvid3")
            if k in cookies}
    if "SESSDATA" not in keep:
        return None
    keep["saved_at"] = int(time.time())
    keep.update(extra or {})
    DATA.mkdir(exist_ok=True)
    SESSION.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
    return keep


def load_session():
    try:
        return json.loads(SESSION.read_text(encoding="utf-8"))
    except Exception:
        return {}


def with_session(sess):
    op, jar = opener()
    for k, v in (sess or {}).items():
        if k in ("SESSDATA", "bili_jct", "DedeUserID", "buvid3"):
            jar.set_cookie(http.cookiejar.Cookie(
                0, k, v, None, False, ".bilibili.com", True, True,
                "/", True, False, None, True, None, None, {}))
    return op


def check(sess=None):
    sess = sess if sess is not None else load_session()
    if not sess.get("SESSDATA"):
        print("还没有会话（先跑 python bili_login.py）")
        return 1
    op = with_session(sess)
    j = get_json(op, NAV)
    d = j.get("data") or {}
    if j.get("code") == 0 and d.get("isLogin"):
        print(f"会话有效：{d.get('uname')}（uid {d.get('mid')}，"
              f"等级 {d.get('level_info', {}).get('current_level')}）")
        return 0
    print(f"会话已失效：code={j.get('code')} msg={j.get('message')!r}")
    return 1


def login(timeout=240):
    import qrcode

    op, jar = opener()
    j = get_json(op, GENERATE)
    if j.get("code") != 0:
        print(f"生成二维码失败：{j.get('code')} {j.get('message')}")
        return 1
    data = j["data"]
    key, url = data["qrcode_key"], data["url"]

    DATA.mkdir(exist_ok=True)
    # 文件名带时间戳 + 图上印生成时间：万一界面缓存了旧图，一眼能看出来
    stamp = time.strftime("%H%M%S")
    out = DATA / f"bili_qr_{stamp}.png"
    qr = qrcode.QRCode(box_size=12, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    from PIL import Image, ImageDraw
    w, h = img.size
    canvas = Image.new("RGB", (w, h + 64), "white")
    canvas.paste(img, (0, 0))
    d = ImageDraw.Draw(canvas)
    label = time.strftime("%H:%M:%S")
    d.text((w // 2 - 46, h + 18), f"{label}  generated", fill="black")
    d.text((10, h + 40), "valid ~180s from the time above", fill=(90, 90, 90))
    canvas.save(out)

    for old_png in DATA.glob("bili_qr*.png"):
        if old_png.name != out.name and "bili_qr_" in old_png.name:
            try:
                old_png.unlink()
            except OSError:
                pass
    print(f"二维码已保存: {out}")
    print(f"生成时间: {label}")
    print("用 B 站 App 扫它（180 秒内有效）…")

    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        time.sleep(2)
        q = urllib.parse.urlencode({"qrcode_key": key, "source": "main-fe-header"})
        try:
            r = get_json(op, f"{POLL}?{q}")
        except Exception as exc:
            print(f"  轮询出错（继续）：{type(exc).__name__}")
            continue
        d = r.get("data") or {}
        code = d.get("code")
        if code == 86101:
            if last != "wait":
                print("  等待扫码…")
                last = "wait"
        elif code == 86090:
            if last != "scanned":
                print("  已扫码，请在手机上点确认")
                last = "scanned"
        elif code == 86038:
            print("  二维码过期了，重新跑一次")
            return 1
        elif code == 0:
            keep = save_session(jar, {"refresh_token": d.get("refresh_token", "")[:40]})
            if not keep:
                print("  登录成功但没拿到 SESSDATA")
                return 1
            print(f"  登录成功，会话已存到 {SESSION}")
            return check(keep)
    print("  超时，没等到扫码")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()
    return check() if args.check else login(args.timeout)


if __name__ == "__main__":
    sys.exit(main())

