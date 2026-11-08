"""B站的 wbi 签名。

为什么有这个文件：B站把大部分 web 接口迁到了需要签名的形式，评论的
`/x/v2/reply/wbi/main` 就是其中之一。签名算法是公开的（社区文档
socialsisteryi/bilibili-API-collect 里写明），这里按那份说明自己实现一份：
取 `nav` 返回的 img_key / sub_key，按固定表重排出 32 位 mixin key，
参数加 `wts`、按 key 排序、去掉值里的 "!'()*"，再 md5(查询串 + mixin key)。

注释里不抄别人的代码，只留算法本身 —— 那张混排表来自 B站自己的 JS，
是事实常量，不是谁的创作。
"""
import json
import time
import urllib.parse
import urllib.request
from functools import reduce
from hashlib import md5
from pathlib import Path

import os as _os

import bili_login as L

_HERE = Path(_os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
KEYS_CACHE = _HERE / "data" / "bili_wbi_keys.json"

# B站 JS 里那张 64 项混排表
MIXIN_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]
NAV = "https://api.bilibili.com/x/web-interface/nav"


def mixin_key(img_key, sub_key):
    """img_key + sub_key 拼起来，按表重排，取前 32 位。"""
    raw = (img_key or "") + (sub_key or "")
    return reduce(lambda acc, i: acc + raw[i], MIXIN_TABLE, "")[:32]


def _clean(value):
    """值里的 "!'()*" 会被 urlencode 成 %21 之类，签名前后不一致，直接去掉。"""
    return "".join(c for c in str(value) if c not in "!'()*")


def sign(params, img_key, sub_key, now=None):
    """给参数字典签名，返回新字典（含 wts 和 w_rid）。不改原件。"""
    key = mixin_key(img_key, sub_key)
    out = {k: _clean(v) for k, v in dict(params).items()}
    out["wts"] = int(now if now is not None else time.time())
    out = dict(sorted(out.items()))
    query = urllib.parse.urlencode(out)
    out["w_rid"] = md5((query + key).encode()).hexdigest()
    return out


def keys(sess=None, now=None, ttl=1800, fetch=None):
    """拿 img_key / sub_key（带缓存，默认 30 分钟）。

    返回 (img_key, sub_key)；拿不到时抛 RuntimeError。
    """
    now = now or time.time()
    try:
        cached = json.loads(KEYS_CACHE.read_text(encoding="utf-8"))
        if now - float(cached.get("at") or 0) < ttl and cached.get("img_key"):
            return cached["img_key"], cached["sub_key"]
    except Exception:
        pass

    if fetch is None:
        fetch = _nav

    data = fetch(sess)
    wbi = (data or {}).get("wbi_img") or {}
    img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    if not img or not sub:
        raise RuntimeError("nav 里没有 wbi_img，可能没登录或者接口变了")

    try:
        KEYS_CACHE.parent.mkdir(exist_ok=True)
        KEYS_CACHE.write_text(json.dumps({"at": now, "img_key": img, "sub_key": sub},
                                         ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return img, sub


def _nav(sess=None):
    op = L.with_session(sess if sess is not None else L.load_session())
    j = L.get_json(op, NAV)
    if j.get("code") != 0:
        raise RuntimeError(f"nav 失败：{j.get('code')} {j.get('message')}")
    return j.get("data") or {}


def signed_url(base, params, sess=None, now=None, fetch=None):
    """把参数签好，拼出完整 URL。"""
    img, sub = keys(sess, now=now, fetch=fetch)
    q = sign(params, img, sub, now=now)
    return f"{base}?{urllib.parse.urlencode(q)}"


def get_json(op, url, timeout=15):
    req = urllib.request.Request(url, headers=dict(L.HEADERS, Accept="application/json"))
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    import argparse

    ap = argparse.ArgumentParser(description="wbi 签名自检")
    ap.add_argument("--raw", action="store_true", help="打印 nav 的 wbi_img")
    args = ap.parse_args()
    img, sub = keys()
    print("img_key =", img)
    print("sub_key =", sub)
    print("mixin   =", mixin_key(img, sub))
    print("签名示例:", sign({"oid": 1, "type": 1}, img, sub, now=1700000000))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
