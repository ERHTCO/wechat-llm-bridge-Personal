"""B站收藏：把专属收藏夹当存储点。

    python bili_fav.py folders              # 看有哪些收藏夹
    python bili_fav.py ensure               # 找到或创建「AI收藏」
    python bili_fav.py add BV1xx BV1yy      # 收藏（可多个）
    python bili_fav.py add BV1xx --reason "用户提到键盘"
    python bili_fav.py log                  # 看看收过什么、为什么

规矩（写在代码里，不靠自觉）：
  - 只写目标收藏夹，不碰别的夹子
  - 每天最多 DAILY_CAP 条
  - 同一个 aid 不重复收
  - 不做取消收藏 / 点赞 / 评论 / 弹幕
  - 每次收藏都记一条本地日志，事后能查理由

接口坑：
  - 收藏要 csrf（就是登录拿到的 bili_jct）
  - rid 用的是 aid 不是 bvid，得先查一次 view
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import bili_login as L

import os as _os

# 打包成 exe 之后 __file__ 在 _internal 里，data/ 却在 exe 旁边。
# 必须和 bridge/proactive 用同一套根目录，否则读不到会话和配置。
_HERE = Path(_os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
DATA = _HERE / "data"
LOG = DATA / "bili_fav_log.jsonl"
FOLDER_NAME = "AI收藏"
DAILY_CAP = 10

API = "https://api.bilibili.com"
FORM = {"Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www.bilibili.com"}


class NotLoggedIn(RuntimeError):
    """没有 B站登录态。

    以前这里 raise SystemExit：它不是 Exception 的子类，后台订阅线程
    （bili_sub.loop 的 except Exception）接不住，CPython 会把线程静默杀掉、
    一行日志都不留 —— 订阅从此永不再跑。GUI 里点「跑一轮」也是同样下场。
    """


def _op():
    sess = L.load_session()
    if not sess.get("SESSDATA"):
        raise NotLoggedIn("还没有登录态，先跑 python bili_login.py")
    return sess, L.with_session(sess)


def _req(op, url, data=None):
    headers = dict(L.HEADERS)
    body = None
    if data is not None:
        headers.update(FORM)
        body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    with op.open(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def folders():
    sess, op = _op()
    j = _req(op, f"{API}/x/v3/fav/folder/created/list-all?up_mid={sess['DedeUserID']}")
    if j.get("code") != 0:
        print(f"取收藏夹失败：{j.get('code')} {j.get('message')}")
        return []
    return (j.get("data") or {}).get("list") or []


def ensure_folder(name=FOLDER_NAME):
    sess, op = _op()
    for f in folders():
        if f.get("title") == name:
            print(f"已有收藏夹「{name}」 id={f.get('id')} 现有 {f.get('media_count')} 条")
            return f["id"]
    j = _req(op, f"{API}/x/v3/fav/folder/add",
             {"title": name, "privacy": 0, "csrf": sess["bili_jct"]})
    if j.get("code") != 0:
        print(f"创建失败：{j.get('code')} {j.get('message')}")
        return None
    fid = (j.get("data") or {}).get("id")
    print(f"已创建收藏夹「{name}」 id={fid}")
    return fid


def aid_of(op, bvid):
    """返回 (aid, title, view 的 data)。data 给 Obsidian 笔记用（up主、播放量）。"""
    j = _req(op, f"{API}/x/web-interface/view?bvid={bvid}")
    if j.get("code") != 0:
        return None, None, {}
    d = j.get("data") or {}
    return d.get("aid"), d.get("title"), d


def folder_aids(op, fid):
    j = _req(op, f"{API}/x/v3/fav/resource/ids?media_id={fid}")
    if j.get("code") != 0:
        return set()
    return {str(x.get("id")) for x in ((j.get("data") or []))}


def today_count():
    if not LOG.exists():
        return 0
    day = time.strftime("%Y-%m-%d")
    n = 0
    for line in LOG.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("ok") and rec.get("day") == day:
            n += 1
    return n


def log_add(rec):
    DATA.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def add(bvids, reason=""):
    sess, op = _op()
    fid = ensure_folder()
    if not fid:
        return 1
    have = folder_aids(op, fid)
    used = today_count()
    rc = 0
    for bvid in bvids:
        aid, title, info = aid_of(op, bvid)
        if not aid:
            print(f"{bvid}: 查不到这个视频（bvid 不对或已下架）")
            rc = 1
            continue
        # 先判重再判额度：已经收过的，提示「已在夹子里」比「到上限」准确
        if str(aid) in have:
            print(f"{bvid}: 已经在「{FOLDER_NAME}」里了，跳过")
            continue
        if used >= DAILY_CAP:
            print(f"今天已收 {used} 条，到上限 {DAILY_CAP}，剩下的明天再说")
            break
        j = _req(op, f"{API}/x/v3/fav/resource/deal", {
            "rid": aid, "type": 2, "add_media_ids": fid,
            "del_media_ids": "", "csrf": sess["bili_jct"],
        })
        ok = j.get("code") == 0
        detail = "" if ok else f" code={j.get('code')} {j.get('message')}"
        print(f"{bvid}: {'已收藏' if ok else '失败'} 「{(title or '')[:36]}」{detail}")
        log_add({"ts": int(time.time()), "day": time.strftime("%Y-%m-%d"),
                 "bvid": bvid, "aid": str(aid), "fid": fid,
                 "title": title or "", "reason": reason, "ok": ok,
                 "msg": j.get("message", "")})
        if ok:
            used += 1
            have.add(str(aid))
            # 顺手在 Obsidian 库里写一条收藏说明。写失败不影响收藏本身。
            try:
                import vault
                if vault.fav_note(info or {"bvid": bvid, "title": title}, reason):
                    print(f"{bvid}: 收藏说明已写进库")
            except Exception as exc:
                print(f"{bvid}: 收藏说明没写进去（{type(exc).__name__}: {exc}）")
        else:
            rc = 1
        time.sleep(1.2)
    return rc


def show_log(limit=20):
    if not LOG.exists():
        print("（还没有记录）")
        return 0
    lines = LOG.read_text(encoding="utf-8").splitlines()[-limit:]
    print(f"今天已收 {today_count()} 条（上限 {DAILY_CAP}）")
    for line in lines:
        try:
            r = json.loads(line)
        except Exception:
            continue
        mark = "✅" if r.get("ok") else "❌"
        print(f"  {mark} {r.get('bvid')} {(r.get('title') or '')[:30]} "
              f"理由={r.get('reason') or '-'}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="B站收藏")
    ap.add_argument("cmd", choices=["folders", "ensure", "add", "log"])
    ap.add_argument("bvids", nargs="*")
    ap.add_argument("--reason", default="")
    args = ap.parse_args()

    if args.cmd == "folders":
        for f in folders():
            print(f"  id={f.get('id'):<10} {f.get('title')}  ({f.get('media_count')} 条)")
        return 0
    if args.cmd == "ensure":
        return 0 if ensure_folder() else 1
    if args.cmd == "add":
        if not args.bvids:
            print("用法: add BV1xx [BV1yy] [--reason ...]")
            return 2
        return add(args.bvids, args.reason)
    if args.cmd == "log":
        return show_log()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except NotLoggedIn as exc:
        # 命令行还是给一句人话，不要甩栈
        print(exc)
        sys.exit(1)
