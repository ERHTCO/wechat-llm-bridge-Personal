"""DeepSeek 自带的搜索 / 抓网页接口（官方文档：https://api-docs.deepseek.com/zh-cn/guides/search）。

用法：
    python tools_search.py 杭州明天天气
    python tools_search.py 杭州明天天气 -n 3            # 只要 3 条
    python tools_search.py 杭州明天天气 --content        # 顺带返回网页正文
    python tools_search.py "DeepSeek site:github.com lang:zh after:2025"
    python tools_search.py --fetch https://example.com
    python tools_search.py 搜索词 --json                 # 原始 JSON

计费：搜索 / 抓取各 5 元 / 1000 次成功请求；失败或无结果不计费。
需要 DEEPSEEK_API_KEY（或 config.json 里的 api_key）。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
FETCH_URL = "https://api.deepseek.com/v1/tools/fetch"


def api_key():
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    try:
        cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8-sig"))
        return cfg.get("api_key") or ""
    except Exception:
        return ""


def post(url, payload, timeout=40):
    key = api_key()
    if not key:
        raise SystemExit("没有 DEEPSEEK_API_KEY")
    # 必须按 UTF-8 编码：中文查询用别的编码会变乱码，搜索结果也跟着跑偏
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json; charset=utf-8",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"HTTP {exc.code}: {detail}")
    return json.loads(body), int((time.time() - t0) * 1000)


def do_search(query, count=5, need_content=False, question=None, raw=False):
    payload = {"query": query, "count": count, "need_content": need_content}
    if question:
        payload["ori_question"] = question
    result, ms = post(SEARCH_URL, payload)
    items = result.get("results") or []
    if raw:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"查询：{query}")
    print(f"返回 {len(items)} 条，{ms} ms\n")
    for i, it in enumerate(items, 1):
        title = (it.get("title") or "").strip()
        url = it.get("url") or ""
        summary = (it.get("summary") or "").strip()
        snippet = (it.get("snippet") or "").strip()
        site = it.get("site_name") or ""
        date = it.get("date") or ""
        score = it.get("rerank_score")
        auth = it.get("authority")
        meta = " ".join(x for x in (site, date[:10] if date else "",
                                    f"相关度 {score:.1f}" if isinstance(score, (int, float)) else "",
                                    f"权威 {auth}" if isinstance(auth, int) else "") if x)
        print(f"{i}. {title}")
        if meta:
            print(f"   {meta}")
        print(f"   {url}")
        text = summary or snippet
        if text:
            print(f"   {text[:220].replace(chr(10), ' ')}")
        if need_content and it.get("content"):
            print(f"   [正文 {len(it['content'])} 字]")
        print()


def do_fetch(url, raw=False, limit=3000):
    result, ms = post(FETCH_URL, {"url": url})
    title = result.get("title") or ""
    content = result.get("content") or ""
    if raw:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"抓取：{url}")
    if title:
        print(f"标题：{title}")
    print(f"{len(content)} 字，{ms} ms\n")
    print(content[:limit])
    if len(content) > limit:
        print(f"\n…（后面还有 {len(content) - limit} 字）")


def main():
    ap = argparse.ArgumentParser(description="DeepSeek 搜索 / 抓网页")
    ap.add_argument("query", nargs="?", help="搜索词（支持 site: / lang: / after: 等语法）")
    ap.add_argument("--fetch", metavar="URL", help="抓取某个网页的正文")
    ap.add_argument("-n", "--num", type=int, default=5, help="返回条数 1~50（默认 5）")
    ap.add_argument("--content", action="store_true", help="同时返回网页正文（更慢更贵）")
    ap.add_argument("--question", help="用户原始问题，帮助理解搜索意图")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = ap.parse_args()

    if args.fetch:
        do_fetch(args.fetch, raw=args.json)
    elif args.query:
        do_search(args.query, count=args.num, need_content=args.content,
                  question=args.question, raw=args.json)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
