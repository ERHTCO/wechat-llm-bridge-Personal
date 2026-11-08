"""统计聚合的测试。

    .venv\\Scripts\\python.exe tests\\test_stats.py

自己造一个 chat.db，不碰真的。重点：
  - summary() 的派生数字（回复率、失败率、直答扣减）
  - 分桶：24 小时按小时，7 天按天，空桶也要留位置（柱状图靠它对齐）
  - reasons() 只留丢弃和失败，正常回复不算「原因」
"""
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import stats  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


SCHEMA = """
create table events(id integer primary key autoincrement, ts real, wxid text,
                    text text, action text, reason text, reply text, elapsed_ms integer);
create table usage(id integer primary key autoincrement, ts real, wxid text,
                   model text, hit integer, miss integer, out_tokens integer);
"""


def make_db(rows=(), usage=()):
    d = Path(tempfile.mkdtemp(prefix="wxstats-"))
    p = d / "chat.db"
    conn = sqlite3.connect(p)
    conn.executescript(SCHEMA)
    conn.executemany("insert into events(ts,wxid,action,reason,elapsed_ms) "
                     "values(?,?,?,?,?)", rows)
    conn.executemany("insert into usage(ts,wxid,model,hit,miss,out_tokens) "
                     "values(?,?,?,?,?,?)", usage)
    conn.commit()
    conn.close()
    return p


def test_summary():
    now = time.time()
    p = make_db(rows=[
        (now, "a", "send", "", 100),
        (now, "a", "send", "", 200),
        (now, "a", "send", "timetable", 1),           # 查表直答，不算模型回的
        (now, "a", "drop", "rate_limited_per_minute", 2),
        (now, "a", "error", "backend_error: boom", 0),
        (now - 3600 * 30, "a", "send", "", 50),       # 30 小时前，24 小时窗口外
    ], usage=[(now, "a", "m", 800, 200, 300)])
    s = stats.summary(24, p)
    check("窗口内总数 5 条", s["total"] == 5, s["total"])
    check("send 3 条", s["sent"] == 3, s["sent"])
    check("drop 1 条", s["dropped"] == 1, s["dropped"])
    check("error 1 条", s["error_count"] == 1, s["error_count"])
    check("直答 1 条", s["direct_answer"] == 1, s["direct_answer"])
    check("模型回了 2 条", s["model_ok"] == 2, s["model_ok"])
    check("模型失败 1 次", s["model_fail"] == 1, s["model_fail"])
    check("回复率 60%", s["reply_rate"] == 60.0, s["reply_rate"])
    check("失败率 = 1/4 = 25%", s["model_fail_rate"] == 25.0, s["model_fail_rate"])
    check("缓存命中 80%", s["tokens"]["cache_hit_rate"] == 80.0, s["tokens"])
    check("调用 1 次", s["tokens"]["calls"] == 1, s["tokens"])


def test_summary_empty():
    d = Path(tempfile.mkdtemp(prefix="wxstats0-"))
    s = stats.summary(24, d / "nope.db")
    check("库不存在也不炸", s["total"] == 0 and s["reply_rate"] == 0.0, s)
    check("token 也是空的", s["tokens"]["cache_hit_rate"] == 0.0, s["tokens"])


def test_series_hourly():
    now = time.time()
    hour = int(now // 3600)
    p = make_db(rows=[
        (hour * 3600 + 60, "a", "send", "", 10),
        (hour * 3600 + 120, "a", "send", "", 10),
        (hour * 3600 + 180, "a", "drop", "x", 1),
    ])
    ser = stats.series(24, p)
    check("24 小时是 25 个桶（含首尾）", len(ser["labels"]) == 25, len(ser["labels"]))
    check("最后一格是当前小时", ser["send"][-1] == 2 and ser["drop"][-1] == 1, ser["send"][-3:])
    check("总量对得上", sum(ser["total"]) == 3, sum(ser["total"]))
    check("标签形如 09:00", ":" in ser["labels"][0], ser["labels"][:2])


def test_series_daily():
    now = time.time()
    p = make_db(rows=[(now, "a", "send", "", 10)])
    ser = stats.series(24 * 7, p)
    check("7 天按天分桶", len(ser["labels"]) == 8, len(ser["labels"]))
    check("标签形如 10/28", "/" in ser["labels"][0], ser["labels"][:2])
    check("今天那条在里面", sum(ser["total"]) == 1, sum(ser["total"]))


def test_reasons():
    now = time.time()
    p = make_db(rows=[
        (now, "a", "drop", "rate_limited_per_minute", 2),
        (now, "a", "drop", "rate_limited_per_minute", 2),
        (now, "a", "error", "backend_error: x", 0),
        (now, "a", "send", "search", 900),      # 正常发送，不是「原因」
        (now, "a", "drop", "", 1),              # 没写原因
    ])
    rs = stats.reasons(24, path=p)
    got = {r["reason"]: r["count"] for r in rs}
    check("正常发送不算原因", "带检索回复" not in got, got)
    check("限流排第一", rs[0]["code"] == "rate_limited_per_minute" and rs[0]["count"] == 2,
          rs)
    check("代号翻成了人话", rs[0]["reason"] == "每分钟限流", rs[0])
    check("没写原因的给了人话", "没写原因（丢弃）" in got, got)
    check("模型报错的代号保留", any(r["code"].startswith("backend_error") for r in rs), got)
    check("占比加起来约 100", abs(sum(r["share"] for r in rs) - 100) < 0.5,
          sum(r["share"] for r in rs))


def test_tokens():
    now = time.time()
    p = make_db(usage=[(now, "a", "m", 10, 10, 5), (now, "a", "m", 30, 10, 5)])
    t = stats.tokens(24, p)
    check("调用 2 次", t["calls"] == 2, t)
    check("命中 40/60 = 66.7%", t["cache_hit_rate"] == 66.7, t)
    check("输出累计 10", t["output"] == 10, t)


def main():
    test_summary()
    test_summary_empty()
    test_series_hourly()
    test_series_daily()
    test_reasons()
    test_tokens()
    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
