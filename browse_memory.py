"""A 层实验：把「刷到的视频」记下来，然后能查。

只抓 B站公开的排行榜/热门接口（无需登录、只读），存进本地 SQLite。
不碰抖音（需要签名逆向），不做任何绕过。

用法：
    python browse_memory.py collect        # 抓一次，入库
    python browse_memory.py query 原神      # 查历史
    python browse_memory.py stats          # 看有多少条、占多大
    python browse_memory.py list           # 最近 10 条
"""
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# 打包后 __file__ 落进 _internal\，所以路径一律从数据根起算
HERE = Path(os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
DB_PATH = HERE / "data" / "browse.db"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

POPULAR = "https://api.bilibili.com/x/web-interface/popular?ps=50&pn={pn}"
RANKING = "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all"


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS videos(
            bvid       TEXT PRIMARY KEY,
            title      TEXT,
            url        TEXT,
            up         TEXT,
            duration   INTEGER,
            pubdate    INTEGER,
            view       INTEGER,
            like_cnt   INTEGER,
            danmaku    INTEGER,
            `desc`     TEXT,
            source     TEXT,
            fetched_at INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_time ON videos(fetched_at)")
    conn.commit()
    return conn


def _get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _norm(item, source):
    owner = item.get("owner") or {}
    stat = item.get("stat") or {}
    bvid = item.get("bvid") or ""
    if not bvid:
        return None
    return {
        "bvid": bvid,
        "title": (item.get("title") or "").strip(),
        "url": f"https://www.bilibili.com/video/{bvid}",
        "up": owner.get("name") or "",
        "duration": int(item.get("duration") or 0),
        "pubdate": int(item.get("pubdate") or 0),
        "view": int(stat.get("view") or 0),
        "like_cnt": int(stat.get("like") or 0),
        "danmaku": int(stat.get("danmaku") or 0),
        "desc": (item.get("desc") or "").strip()[:500],
        "source": source,
        "fetched_at": int(time.time()),
    }


def collect(pages=3):
    """抓 popular 的前 pages 页（每页 50 条）。

    ranking/v2 现在返回 -352（B站风控，需要 wbi 签名），所以不靠它。
    """
    conn = connect()
    got, new = 0, 0
    sources = [(POPULAR.format(pn=pn), f"popular-p{pn}") for pn in range(1, pages + 1)]
    sources.append((RANKING, "ranking"))
    for url, source in sources:
        try:
            data = _get(url)
        except Exception as exc:
            print(f"[跳过] {source}: {type(exc).__name__}: {exc}")
            continue
        if data.get("code") not in (0, None):
            print(f"[跳过] {source}: code={data.get('code')} {data.get('message')}")
            continue
        rows = (data.get("data") or {}).get("list") or []
        for item in rows:
            rec = _norm(item, source)
            if not rec:
                continue
            got += 1
            cur = conn.execute(
                "INSERT INTO videos(bvid,title,url,up,duration,pubdate,view,"
                "like_cnt,danmaku,`desc`,source,fetched_at) "
                "VALUES(:bvid,:title,:url,:up,:duration,:pubdate,:view,"
                ":like_cnt,:danmaku,:desc,:source,:fetched_at) "
                "ON CONFLICT(bvid) DO UPDATE SET "
                "view=excluded.view, like_cnt=excluded.like_cnt, "
                "danmaku=excluded.danmaku, fetched_at=excluded.fetched_at",
                rec,
            )
            new += cur.rowcount
        time.sleep(1.2)          # 每次请求之间停一下，别打太快

    conn.commit()
    total = conn.execute("SELECT count(*) FROM videos").fetchone()[0]
    conn.close()
    print(f"抓到 {got} 条，新增 {new} 条，库里共 {total} 条")


def query(keyword, limit=10):
    conn = connect()
    kw = f"%{keyword}%"
    rows = conn.execute(
        "SELECT title,up,view,like_cnt,url,pubdate,source FROM videos "
        "WHERE title LIKE ? OR up LIKE ? OR `desc` LIKE ? "
        "ORDER BY fetched_at DESC, view DESC LIMIT ?",
        (kw, kw, kw, limit),
    ).fetchall()
    conn.close()
    if not rows:
        print(f"没有匹配「{keyword}」的记录")
        return
    print(f"「{keyword}」命中 {len(rows)} 条：\n")
    for i, (title, up, view, like, url, pubdate, source) in enumerate(rows, 1):
        when = time.strftime("%m-%d", time.localtime(pubdate)) if pubdate else "-"
        print(f"{i}. {title}")
        print(f"   {up} · {when} · 播放 {view:,} · 点赞 {like:,} · [{source}]")
        print(f"   {url}")


def stats():
    if not DB_PATH.exists():
        print("还没有数据库，先跑 collect")
        return
    conn = connect()
    total = conn.execute("SELECT count(*) FROM videos").fetchone()[0]
    oldest = conn.execute("SELECT min(fetched_at) FROM videos").fetchone()[0]
    conn.close()
    size = DB_PATH.stat().st_size / 1024
    print(f"记录数: {total}")
    print(f"数据库: {size:.0f} KB（{DB_PATH}）")
    if oldest:
        print(f"最早一条: {time.strftime('%Y-%m-%d %H:%M', time.localtime(oldest))}")
    if total:
        print(f"平均每条: {size * 1024 / total:.0f} 字节")


def show_list(limit=10):
    conn = connect()
    rows = conn.execute(
        "SELECT title,up,view,url FROM videos ORDER BY fetched_at DESC, view DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    for i, (title, up, view, url) in enumerate(rows, 1):
        print(f"{i}. {title}  ({up}, 播放 {view:,})")
        print(f"   {url}")


def answer(question, limit=8):
    """把库里的记录当「资料」交给模型，模仿"问它今天刷了什么"的效果。

    注意：模型没有真的浏览过，它只是读了我们给它的列表——
    所以提示词里明确写"这是你刚才刷到的一批视频"，并且不许编造列表外的内容。
    """
    import os
    import re

    conn = connect()
    rows = conn.execute(
        "SELECT title,up,view,like_cnt,url,`desc` FROM videos "
        "ORDER BY fetched_at DESC, view DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    if not rows:
        print("库里还没有数据，先跑 collect")
        return

    lines = []
    for i, (title, up, view, like, url, desc) in enumerate(rows, 1):
        lines.append(f"{i}. {title}（UP：{up}，播放 {view:,}）{url}")
        if desc:
            lines.append(f"   简介：{desc[:120]}")
    material = "\n".join(lines)

    prompt = (
        "下面是你刚才刷 B 站时看过的视频列表（资料）：\n"
        f"{material}\n\n"
        f"用户问：{question}\n"
        "要求：用中文口语回答，两到三句，可以挑一两条印象最深的说；"
        "只能依据上面这份列表，不要编造列表里没有的视频；"
        "不要贴链接，不要罗列全部条目。"
    )

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        try:
            cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8-sig"))
            key = cfg.get("api_key", "")
        except Exception:
            key = ""
    if not key:
        print("没有 api_key")
        return

    payload = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 1.0,
        "max_tokens": 300,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions", data=payload, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json; charset=utf-8"},
    )
    print(f"（交给模型的资料：{len(rows)} 条）\n")
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    reply = body["choices"][0]["message"]["content"]
    print(reply.strip())


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "collect"
    if cmd == "collect":
        collect()
    elif cmd == "query":
        query(" ".join(sys.argv[2:]) or "的")
    elif cmd == "stats":
        stats()
    elif cmd == "list":
        show_list()
    elif cmd == "answer":
        answer(" ".join(sys.argv[2:]) or "你今天刷了什么视频")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
