"""B站搜索 / 热门榜：给订阅和聊天驱动共用的取数层。

    from bili_search import search, popular
    search("机械键盘")      # -> [{bvid, title, author, play, pubdate, desc}, ...]
    popular(20)

不需要登录（实测搜索连发三次都正常）。真正要登录的是收藏，那在 bili_fav.py。
"""
import html
import json
import re
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
API = "https://api.bilibili.com"


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _clean(text):
    """搜索结果的标题带 <em class="keyword"> 高亮标签，去掉。"""
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()


def _norm(v, source):
    stat = v.get("stat") or {}
    return {
        "bvid": v.get("bvid") or "",
        "title": _clean(v.get("title")),
        "author": v.get("author") or (v.get("owner") or {}).get("name") or "",
        "play": int(v.get("play") or stat.get("view") or 0),
        "pubdate": int(v.get("pubdate") or 0),
        "desc": _clean(v.get("description") or v.get("desc") or "")[:200],
        "source": source,
    }


def popular(limit=20):
    """首页热门榜。"""
    j = _get(f"{API}/x/web-interface/popular?ps={min(limit, 50)}&pn=1")
    if j.get("code") != 0:
        raise RuntimeError(f"热门榜失败 code={j.get('code')} {j.get('message')}")
    return [_norm(v, "popular") for v in ((j.get("data") or {}).get("list") or [])]


def search(keyword, limit=20, page=1):
    """按关键词搜视频。返回按播放量降序。"""
    q = urllib.parse.quote(keyword)
    j = _get(f"{API}/x/web-interface/search/all/v2?keyword={q}&page={page}")
    if j.get("code") != 0:
        raise RuntimeError(f"搜索失败 code={j.get('code')} {j.get('message')}")

    videos = []
    for group in ((j.get("data") or {}).get("result") or []):
        if group.get("result_type") == "video":
            videos = group.get("data") or []
            break

    out = [_norm(v, f"search:{keyword}") for v in videos]
    out = [v for v in out if v["bvid"]]
    out.sort(key=lambda v: -v["play"])
    return out[:limit]


def age_days(v, now=None):
    now = now or time.time()
    return (now - (v.get("pubdate") or 0)) / 86400 if v.get("pubdate") else 9999


if __name__ == "__main__":
    import sys

    kw = sys.argv[1] if len(sys.argv) > 1 else "机械键盘"
    print(f"搜索「{kw}」：")
    for v in search(kw, limit=8):
        print(f"  {v['bvid']}  {v['play']:>9,}  {v['title'][:40]}  @{v['author']}")
