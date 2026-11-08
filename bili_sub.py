"""关键词订阅：每隔几小时，按你给的词去 B 站搜，挑一条收进「AI收藏」。

和聊天驱动的分工：
  这个模块   只认「订阅词」，跟谁在聊天无关
  聊天驱动   bili_chat.py，只在消息明确要视频时才动

两个都往 bili_fav.add() 写，所以共用同一套查重和「每天 10 条」的上限——
不会出现两个模块各收 10 条的情况。

配置在 data/bili_sub.json（data/ 不进版本库）：

    enabled         总开关，默认 false
    window          允许时段 ["09:00","23:00"]
    interval_hours  同一个词多久搜一次
    per_keyword     每个词每轮最多收几条
    per_run         每轮总共最多收几条
    min_play        播放量下限，低于这个不收（挡掉搬运和冷门）
    max_age_days    发布时间超过这个天数的不要
    keywords        订阅词

命令行：
    python bili_sub.py status
    python bili_sub.py add 机械键盘
    python bili_sub.py rm 机械键盘
    python bili_sub.py run --dry     # 只看看会收哪条，不真收
    python bili_sub.py on / off
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import bili_fav
import bili_search

import os as _os

# 打包成 exe 之后 __file__ 在 _internal 里，data/ 却在 exe 旁边。
# 必须和 bridge/proactive 用同一套根目录，否则读不到会话和配置。
_HERE = Path(_os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
DATA = _HERE / "data"
CONF = DATA / "bili_sub.json"
STATE = DATA / "bili_sub_state.json"

DEFAULT = {
    "enabled": False,
    "window": ["09:00", "23:00"],
    "interval_hours": 6,
    "per_keyword": 1,
    "per_run": 3,
    "min_play": 10000,
    "max_age_days": 30,
    "keywords": [],
}


# ---------------------------------------------------------------- 配置 / 状态

def load_conf():
    conf = dict(DEFAULT)
    try:
        conf.update(json.loads(CONF.read_text(encoding="utf-8")))
    except Exception:
        pass
    return conf


def save_conf(conf):
    DATA.mkdir(exist_ok=True)
    CONF.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    DATA.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def in_window(now, window):
    try:
        a, b = window[0].split(":"), window[1].split(":")
        start, end = int(a[0]) * 60 + int(a[1]), int(b[0]) * 60 + int(b[1])
    except Exception:
        return True
    cur = now.hour * 60 + now.minute
    return start <= cur <= end if start <= end else (cur >= start or cur <= end)


# ---------------------------------------------------------------- 选题

def pick(keyword, conf, state, seen_extra=None):
    """给一个词挑一条：够新、够热、没见过。挑不到返回 None。"""
    try:
        results = bili_search.search(keyword, limit=20)
    except Exception as exc:
        print(f"[订阅] 搜「{keyword}」失败：{type(exc).__name__}: {exc}", flush=True)
        return None

    seen = set((state.get(keyword) or {}).get("seen") or [])
    seen |= (seen_extra or set())

    pool = [v for v in results
            if v["bvid"] not in seen
            and v["play"] >= int(conf["min_play"])
            and bili_search.age_days(v) <= float(conf["max_age_days"])]
    if not pool:
        return None
    return max(pool, key=lambda v: v["play"])


def due_keywords(conf, state, now):
    """这一轮该搜哪些词。"""
    out = []
    interval = float(conf["interval_hours"]) * 3600
    for kw in conf.get("keywords") or []:
        last = float((state.get(kw) or {}).get("last_run") or 0)
        if now.timestamp() - last >= interval:
            out.append(kw)
    return out


# ---------------------------------------------------------------- 主流程

def run(dry=False, force=False, now=None):
    now = now or datetime.now()
    conf = load_conf()
    if not conf.get("enabled") and not force:
        print("订阅没开（data/bili_sub.json 里 enabled=false）")
        return 0
    if not force and not in_window(now, conf.get("window") or DEFAULT["window"]):
        print(f"不在允许时段 {conf.get('window')}，跳过")
        return 0

    state = load_state()
    keywords = due_keywords(conf, state, now) if not force else list(conf.get("keywords") or [])
    if not keywords:
        print("没有到期的订阅词")
        return 0

    # 已经在收藏夹里的就别再挑（这一步是省搜索，真正查重在 bili_fav.add 里）
    picked = 0
    for kw in keywords:
        if picked >= int(conf["per_run"]):
            break
        item = pick(kw, conf, state)
        if not item:
            print(f"「{kw}」没有合适的新视频")
            if not dry:
                state.setdefault(kw, {})["last_run"] = now.timestamp()
            continue

        # 每个词最多收 per_keyword 条（目前就是 1）
        per = int(conf["per_keyword"])
        chosen = [item] if per >= 1 else []
        if per > 1:
            try:
                more = [v for v in bili_search.search(kw, limit=20)
                        if v["bvid"] != item["bvid"] and v["play"] >= int(conf["min_play"])]
                chosen += more[:per - 1]
            except Exception:
                pass

        for v in chosen:
            if picked >= int(conf["per_run"]):
                break
            print(f"「{kw}」→ {v['bvid']} 播放{v['play']:,} {v['title'][:36]}")
            if dry:
                # 预演不写状态：写了 seen 就等于把候选"吃掉"，真跑时反而挑不到
                print("  （--dry，不真收，也不改状态）")
                picked += 1
                continue
            # 额度满了就别再调 add（它内部 break，rc 仍是 0，会被当成收藏成功）
            if bili_fav.today_count() >= bili_fav.DAILY_CAP:
                print("  ! 今天额度满了，剩下的词留给下一轮")
                break
            # add 的返回值必须看：登录态过期、查不到 aid 都返回非 0，
            # 以前直接丢掉返回值，失败也照样写 seen/推进 last_run，这条视频永久错过
            rc = bili_fav.add([v["bvid"]], reason=f"关键词订阅:{kw}")
            if rc:
                print(f"  ! 没进收藏夹（rc={rc}），不记 seen，下一轮还能再挑")
                continue
            rec = state.setdefault(kw, {})
            seen = rec.setdefault("seen", [])
            seen.append(v["bvid"])
            rec["seen"] = seen[-200:]          # 只留最近 200 个，别让状态文件无限长
            rec["last_run"] = now.timestamp()
            picked += 1
            time.sleep(1.0)

    save_state(state)
    print(f"本轮收了 {picked} 条")
    return 0


def status():
    conf = load_conf()
    state = load_state()
    print(f"订阅开关: {'开' if conf['enabled'] else '关'}")
    print(f"时段: {conf['window']} · 每 {conf['interval_hours']} 小时搜一次 · "
          f"每轮最多 {conf['per_run']} 条")
    print(f"门槛: 播放 ≥ {conf['min_play']:,} · 发布在 {conf['max_age_days']} 天内")
    print(f"今天已收: {bili_fav.today_count()}/{bili_fav.DAILY_CAP}")
    kws = conf.get("keywords") or []
    if not kws:
        print("订阅词: （空）")
        return 0
    print("订阅词:")
    for kw in kws:
        rec = state.get(kw) or {}
        last = rec.get("last_run")
        when = time.strftime("%m-%d %H:%M", time.localtime(last)) if last else "从没搜过"
        print(f"  {kw:<12} 上次 {when}  已收 {len(rec.get('seen') or [])} 条")
    return 0


def add_keyword(kw):
    conf = load_conf()
    kws = conf.setdefault("keywords", [])
    if kw in kws:
        print(f"「{kw}」已经在订阅里")
        return 0
    kws.append(kw)
    save_conf(conf)
    print(f"已订阅「{kw}」，现在一共 {len(kws)} 个词")


def rm_keyword(kw):
    conf = load_conf()
    kws = conf.setdefault("keywords", [])
    if kw not in kws:
        print(f"「{kw}」不在订阅里")
        return 1
    kws.remove(kw)
    save_conf(conf)
    print(f"已取消「{kw}」")


def set_enabled(on):
    conf = load_conf()
    conf["enabled"] = bool(on)
    save_conf(conf)
    print(f"订阅已{'开启' if on else '关闭'}")


# ---------------------------------------------------------------- 给桥用的循环

def loop(interval_s=600):
    """桥里的后台线程：每 interval_s 看一眼该不该跑。

    所有异常都吃掉——订阅只是个附属功能，不能带崩桥。
    SystemExit 也要吃：它不是 Exception 的子类，漏掉的话线程会被 CPython
    静默杀掉（不打日志、不重启），订阅从此永不运行。
    """
    time.sleep(20)
    while True:
        try:
            conf = load_conf()
            if conf.get("enabled") and in_window(datetime.now(),
                                                 conf.get("window") or DEFAULT["window"]):
                if due_keywords(conf, load_state(), datetime.now()):
                    run()
        except (Exception, SystemExit) as exc:
            print(f"[订阅] 出错：{type(exc).__name__}: {exc}", flush=True)
        time.sleep(interval_s)


def main():
    ap = argparse.ArgumentParser(description="B站关键词订阅")
    ap.add_argument("cmd", choices=["status", "add", "rm", "run", "on", "off"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.cmd == "status":
        return status()
    if args.cmd == "add":
        if not args.arg:
            print("用法: add <关键词>")
            return 2
        return add_keyword(args.arg)
    if args.cmd == "rm":
        return rm_keyword(args.arg or "")
    if args.cmd == "on":
        return set_enabled(True)
    if args.cmd == "off":
        return set_enabled(False)
    if args.cmd == "run":
        return run(dry=args.dry, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
