"""B站评论：把评论区当语料库。

两条路，先走哪条是明确的：

  1. **老接口** `/x/v2/reply?type=1&oid=<aid>&pn=N&ps=20` —— 免签名，但要登录态
     （匿名只返回 3 条，等于没用）。这是当前的主路，简单、稳。
  2. **备用** `/x/v2/reply/wbi/main` —— 官方现行接口，要 wbi 签名（见 wbi.py）。
     主路哪天被关掉，就自动切这条。

规矩：
  - 一次最多 5 页，请求间隔 ≥1 秒（B站对评论接口的风控比对搜索严）
  - 只读，不点赞、不回复、不发评论
  - 落 `data/reply_corpus.jsonl`，按 (bvid, rpid) 去重，重复抓不会写两遍

用法：
    python bili_reply.py BV1xx --pages 3
    python bili_reply.py BV1xx --pages 5 --subs --save
    python bili_reply.py --stats
"""
import argparse
import json
import sys
import time
from pathlib import Path

import os as _os

import bili_login as L
import wbi

_HERE = Path(_os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
DATA = _HERE / "data"
CORPUS = DATA / "reply_corpus.jsonl"

API = "https://api.bilibili.com"
MAX_PAGES = 5
GAP = 1.0            # 请求间隔

_last = [0.0]        # 上次请求时间，保证全局节流


def _wait(gap=GAP):
    dt = time.time() - _last[0]
    if dt < gap:
        time.sleep(gap - dt)
    _last[0] = time.time()


def _get(op, url, timeout=15):
    _wait()
    return wbi.get_json(op, url, timeout=timeout)


def aid_of(op, bvid):
    j = _get(op, f"{API}/x/web-interface/view?bvid={bvid}")
    if j.get("code") != 0:
        raise RuntimeError(f"查不到 {bvid}：{j.get('code')} {j.get('message')}")
    return j["data"]["aid"], j["data"].get("title") or ""


def _row(r, bvid=""):
    content = r.get("content") or {}
    member = r.get("member") or {}
    return {
        "bvid": bvid,
        "rpid": str(r.get("rpid") or ""),
        "user": member.get("uname") or "",
        "uid": str(member.get("mid") or ""),
        "like": int(r.get("like") or 0),
        "time": int(r.get("ctime") or 0),
        "text": (content.get("message") or "").strip(),
        "rcount": int(r.get("rcount") or 0),
        "subs": [],
    }


def _old_page(op, aid, pn, ps=20, sort=2):
    """老接口；返回 (评论列表, 总数, code)。"""
    j = _get(op, f"{API}/x/v2/reply?type=1&oid={aid}&pn={pn}&ps={ps}&sort={sort}")
    if j.get("code") != 0:
        return [], 0, j.get("code")
    d = j.get("data") or {}
    page = d.get("page") or {}
    total = int(page.get("count") or 0)
    return [dict(r) for r in (d.get("replies") or [])], total, 0


def _wbi_page(op, aid, offset="", ps=20, sort=2):
    """备用：签名调 /wbi/main；返回 (评论列表, 总数, 下个 offset)。"""
    params = {
        "oid": aid, "type": 1, "mode": 3 if sort == 2 else 2, "plat": 1, "ps": ps,
        "pagination_str": json.dumps({"offset": offset}, separators=(",", ":")),
    }
    url = wbi.signed_url(f"{API}/x/v2/reply/wbi/main", params)
    j = _get(op, url)
    if j.get("code") != 0:
        raise RuntimeError(f"wbi 接口失败：{j.get('code')} {j.get('message')}")
    d = j.get("data") or {}
    cur = d.get("cursor") or {}
    nxt = ((cur.get("pagination_reply") or {}).get("next_offset")) or ""
    return [dict(r) for r in (d.get("replies") or [])], int(cur.get("all_count") or 0), nxt


def subs_of(op, aid, root, limit=20):
    """楼中楼。limit 按 10 一页向上取。"""
    out = []
    pn = 1
    while len(out) < limit and pn <= 3:
        j = _get(op, f"{API}/x/v2/reply/reply?type=1&oid={aid}&root={root}"
                     f"&pn={pn}&ps=10")
        if j.get("code") != 0:
            break
        rs = ((j.get("data") or {}).get("replies")) or []
        if not rs:
            break
        for r in rs:
            out.append({"user": (r.get("member") or {}).get("uname") or "",
                        "text": ((r.get("content") or {}).get("message") or "").strip()})
        pn += 1
    return out[:limit]


def fetch(bvid=None, aid=None, pages=3, ps=20, sort=2, with_subs=False,
          sess=None, allow_wbi=True):
    """抓一个视频的评论。返回 dict：{bvid, aid, title, total, path, comments}。

    path 说明这次走的是哪条：`old` 或 `wbi`。
    """
    pages = max(1, min(int(pages), MAX_PAGES))
    op = L.with_session(sess if sess is not None else L.load_session())
    if not aid:
        aid, title = aid_of(op, bvid)
    else:
        title = ""

    rows, total, path = [], 0, "old"
    for pn in range(1, pages + 1):
        try:
            raw, total, code = _old_page(op, aid, pn, ps, sort)
            if code != 0:
                raise RuntimeError(f"老接口 code={code}")
        except Exception as exc:
            if not allow_wbi:
                raise
            print(f"[评论] 老接口第 {pn} 页不行（{exc}），换 wbi", flush=True)
            path = "wbi"
            raw = []
            break
        rows += [_row(r, bvid or "") for r in raw]
        if len(raw) < ps:
            break

    if path == "wbi":
        offset = ""
        for _ in range(pages):
            raw, total, offset = _wbi_page(op, aid, offset, ps, sort)
            rows += [_row(r, bvid or "") for r in raw]
            if not offset or len(raw) < ps:
                break

    if with_subs:
        for row in rows:
            if row["rcount"]:
                try:
                    row["subs"] = subs_of(op, aid, row["rpid"])
                except Exception as exc:
                    print(f"[评论] 子回复失败 {row['rpid']}：{exc}", flush=True)

    return {"bvid": bvid or "", "aid": aid, "title": title,
            "total": total, "path": path, "comments": rows}


def save(comments, path=CORPUS):
    """追加到语料库，按 (bvid, rpid) 去重。返回新增条数。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    have = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                have.add((r.get("bvid") or "", str(r.get("rpid") or "")))
            except Exception:
                continue
    added = 0
    with path.open("a", encoding="utf-8") as f:
        for c in comments:
            key = (c.get("bvid") or "", str(c.get("rpid") or ""))
            if not key[1] or key in have:
                continue
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
            have.add(key)
            added += 1
    return added


def targets_from_favs(root=None, skip_have=True):
    """从收藏日志里拿收过的视频，默认跳过语料库里已经有的。"""
    root = Path(root or _HERE)
    log = root / "data" / "bili_fav_log.jsonl"
    if not log.exists():
        return []
    have = set()
    corpus = root / "data" / "reply_corpus.jsonl"
    if skip_have and corpus.exists():
        for line in corpus.read_text(encoding="utf-8").splitlines():
            try:
                have.add(json.loads(line).get("bvid") or "")
            except Exception:
                continue
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        b = r.get("bvid") or ""
        if r.get("ok") and b and b not in out and b not in have:
            out.append(b)
    return out


def corpus_rows(limit=200, path=None):
    """最近入库的评论，新的在前。给界面列表用。"""
    path = Path(path or CORPUS)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return list(reversed(rows))[:limit]


def run_batch(bvids, pages=2, sort=2, subs=False, save_it=True,
              max_videos=0, max_comments=0, max_seconds=0,
              on_event=None, stop=None, root=None):
    """按顺序抓一批视频，满足任一条件就停。0 表示不限。

    这是给界面用的：返回值是这次跑了多少。
    stop 是个带 is_set() 的对象（threading.Event），每抓完一个视频看一次 ——
    单个视频内部没法中断，因为 fetch() 是一次跑完的。
    """
    import time as _t

    def emit(kind, **kw):
        if on_event:
            try:
                on_event(kind, **kw)
            except Exception:
                pass

    def stopped():
        return bool(stop is not None and stop.is_set())

    t0 = _t.time()
    summary = {"videos": 0, "comments": 0, "added": 0, "stopped_by": ""}
    corpus = Path(root or _HERE) / "data" / "reply_corpus.jsonl"

    for bvid in bvids:
        if stopped():
            summary["stopped_by"] = "手动停止"
            break
        if max_videos and summary["videos"] >= max_videos:
            summary["stopped_by"] = f"抓够 {max_videos} 个视频"
            break
        if max_comments and summary["comments"] >= max_comments:
            summary["stopped_by"] = f"抓够 {max_comments} 条评论"
            break
        if max_seconds and _t.time() - t0 >= max_seconds:
            summary["stopped_by"] = f"跑够 {max_seconds} 秒"
            break

        emit("start", bvid=bvid)
        try:
            res = fetch(bvid, pages=pages, sort=sort, with_subs=subs)
        except Exception as exc:
            emit("error", bvid=bvid, error=f"{type(exc).__name__}: {exc}")
            continue

        rows = res["comments"]
        summary["videos"] += 1
        summary["comments"] += len(rows)
        emit("video", bvid=bvid, title=res.get("title") or "",
             got=len(rows), total=res.get("total") or 0, path=res.get("path") or "")

        if save_it and rows:
            n = save(rows, corpus)
            summary["added"] += n
            emit("save", bvid=bvid, added=n)

    emit("done", **summary)
    return summary


def stats(path=CORPUS):
    path = Path(path)
    if not path.exists():
        print("语料库还是空的")
        return 0
    n, by_video, chars = 0, {}, 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        n += 1
        by_video[r.get("bvid") or "?"] = by_video.get(r.get("bvid") or "?", 0) + 1
        chars += len(r.get("text") or "")
    print(f"共 {n} 条评论，{chars} 字，来自 {len(by_video)} 个视频")
    for k, v in sorted(by_video.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {k}  {v} 条")
    return 0


def main():
    ap = argparse.ArgumentParser(description="B站评论语料")
    ap.add_argument("bvid", nargs="?")
    ap.add_argument("--pages", type=int, default=3, help=f"最多 {MAX_PAGES} 页，一页 20 条")
    ap.add_argument("--sort", type=int, default=2, choices=[0, 1, 2],
                    help="0=按时间 1=按点赞 2=按热度")
    ap.add_argument("--subs", action="store_true", help="连楼中楼一起抓")
    ap.add_argument("--save", action="store_true", help="写进 data/reply_corpus.jsonl")
    ap.add_argument("--stats", action="store_true", help="看语料库现状")
    ap.add_argument("--preview", type=int, default=5, help="打印前几条")
    args = ap.parse_args()

    if args.stats:
        return stats()
    if not args.bvid:
        ap.print_help()
        return 2

    t0 = time.time()
    res = fetch(args.bvid, pages=args.pages, sort=args.sort, with_subs=args.subs)
    dt = time.time() - t0
    print(f"[评论] {res['title'][:30] or res['bvid']}  走={res['path']}  "
          f"总数={res['total']}  抓到 {len(res['comments'])} 条  {dt:.1f}s")
    for c in res["comments"][:max(0, args.preview)]:
        print(f"  [{c['like']:>4}赞] {c['user']}: {c['text'][:50]}")
    if args.save:
        n = save(res["comments"])
        print(f"[评论] 入库 {n} 条（重复的没算）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
