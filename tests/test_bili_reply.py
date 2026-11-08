"""B站评论抓取的回归测试。

    .venv\\Scripts\\python.exe tests\\test_bili_reply.py

重点不是「能不能联网」，是这几条规矩：
  - 主路失败要能自动切 wbi（不然老接口一关就全哑）
  - 入库按 (bvid, rpid) 去重（重复抓不能写两遍）
  - 页数有上限、请求有间隔（风控）
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import bili_reply  # noqa: E402

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


def raw(rpid, msg="一句话", like=3, uname="某人", rcount=0):
    return {"rpid": rpid, "like": like, "ctime": 1700000000, "rcount": rcount,
            "content": {"message": msg},
            "member": {"uname": uname, "mid": 42}}


def test_row():
    got = bili_reply._row(raw(1, "你好", 7, "甲"), "BV1x")
    check("字段都搬过来了",
          got["rpid"] == "1" and got["text"] == "你好" and got["like"] == 7
          and got["user"] == "甲" and got["bvid"] == "BV1x", got)
    check("默认没有子回复", got["subs"] == [])


def test_save_dedupe():
    d = Path(tempfile.mkdtemp(prefix="wxreply-"))
    p = d / "corpus.jsonl"
    rows = [bili_reply._row(raw(1), "BV1"), bili_reply._row(raw(2), "BV1")]
    n1 = bili_reply.save(rows, p)
    n2 = bili_reply.save(rows, p)
    n3 = bili_reply.save([bili_reply._row(raw(3), "BV1")], p)
    check("第一次都进去", n1 == 2, n1)
    check("重复的不写第二遍", n2 == 0, n2)
    check("新的一条还能进", n3 == 1, n3)
    lines = p.read_text(encoding="utf-8").splitlines()
    check("文件里一共三行", len(lines) == 3, len(lines))
    check("是合法 JSON", all(json.loads(x) for x in lines))


def test_save_needs_rpid():
    d = Path(tempfile.mkdtemp(prefix="wxreply2-"))
    p = d / "c.jsonl"
    bad = bili_reply._row(raw(""))
    n = bili_reply.save([bad], p)
    check("没有 rpid 的不入库", n == 0, n)


def test_fetch_old_path(monkeypatch=None):
    calls = []

    def fake_old(op, aid, pn, ps=20, sort=2):
        calls.append(pn)
        if pn == 1:
            return [raw(i) for i in range(1, 4)], 3, 0
        return [], 0, 0

    real_old = bili_reply._old_page
    bili_reply._old_page = fake_old
    try:
        res = bili_reply.fetch(bvid="BV1x", aid=117, pages=3)
    finally:
        bili_reply._old_page = real_old
    check("走的是老接口", res["path"] == "old", res["path"])
    check("拿到 3 条", len(res["comments"]) == 3, len(res["comments"]))
    check("不满一页就停", calls == [1], calls)


def test_fetch_falls_back_to_wbi():
    """老接口一坏，自动切 wbi —— 这是整个备用方案的核心。"""
    def boom(*a, **kw):
        raise RuntimeError("老接口挂了")

    def fake_wbi(op, aid, offset="", ps=20, sort=2):
        if not offset:
            return [raw(i) for i in range(1, 3)], 2, "next"
        return [], 2, ""

    real_old, real_wbi = bili_reply._old_page, bili_reply._wbi_page
    bili_reply._old_page = boom
    bili_reply._wbi_page = fake_wbi
    try:
        res = bili_reply.fetch(bvid="BV1x", aid=117, pages=2)
    finally:
        bili_reply._old_page, bili_reply._wbi_page = real_old, real_wbi
    check("切到 wbi 了", res["path"] == "wbi", res["path"])
    check("wbi 抓到的也收下", len(res["comments"]) == 2, len(res["comments"]))


def test_fetch_clamps_pages():
    seen = []

    def fake_old(op, aid, pn, ps=20, sort=2):
        seen.append(pn)
        return [raw(pn * 100 + i) for i in range(20)], 999, 0

    real_old = bili_reply._old_page
    bili_reply._old_page = fake_old
    try:
        bili_reply.fetch(bvid="BV1x", aid=1, pages=99)
    finally:
        bili_reply._old_page = real_old
    check(f"页数被压到 {bili_reply.MAX_PAGES}", len(seen) == bili_reply.MAX_PAGES, seen)


def test_subs():
    def fake_get(op, url, timeout=15):
        return {"code": 0, "data": {"replies": [
            raw(1, "子评论一", uname="乙"), raw(2, "子评论二", uname="丙")]}}

    real = bili_reply._get
    bili_reply._get = fake_get
    try:
        subs = bili_reply.subs_of(None, 1, 999, limit=10)
    finally:
        bili_reply._get = real
    check("拿到子回复", len(subs) >= 2, subs)
    check("带作者", subs[0]["user"] == "乙", subs[0])


def test_throttle():
    import time as _t
    bili_reply._last[0] = 0.0
    t0 = _t.time()
    bili_reply._wait(0.15)
    bili_reply._wait(0.15)
    dt = _t.time() - t0
    check("两次请求之间会等", dt >= 0.13, dt)


def test_targets_from_favs():
    root = Path(tempfile.mkdtemp(prefix="wxreply3-"))
    (root / "data").mkdir()
    log = root / "data" / "bili_fav_log.jsonl"
    log.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"ok": True, "bvid": "BV1a"},
        {"ok": True, "bvid": "BV1b"},
        {"ok": False, "bvid": "BV1c"},          # 没成功的不要
        {"ok": True, "bvid": "BV1a"},           # 重复只留一个
        {"ok": True, "bvid": ""},               # 空的不要
    ]) + "\n", encoding="utf-8")
    check("只留成功的、去重", bili_reply.targets_from_favs(root) == ["BV1a", "BV1b"],
          bili_reply.targets_from_favs(root))

    (root / "data" / "reply_corpus.jsonl").write_text(
        json.dumps({"bvid": "BV1a", "rpid": "1"}) + "\n", encoding="utf-8")
    check("跳过已经抓过的", bili_reply.targets_from_favs(root) == ["BV1b"],
          bili_reply.targets_from_favs(root))
    check("不跳过时都在", bili_reply.targets_from_favs(root, skip_have=False) == ["BV1a", "BV1b"])


class Stop:
    def __init__(self, after=0):
        self.calls = 0
        self.after = after

    def is_set(self):
        self.calls += 1
        return self.after and self.calls > self.after


def _fake_fetch_factory(calls, per_video=3):
    def fake(bvid, pages=2, sort=2, with_subs=False):
        calls.append(bvid)
        return {"bvid": bvid, "aid": 1, "title": f"标题{bvid}", "total": 99,
                "path": "old",
                "comments": [bili_reply._row(raw(i, f"{bvid}-{i}"), bvid)
                             for i in range(per_video)]}
    return fake


def test_run_batch_limits():
    root = Path(tempfile.mkdtemp(prefix="wxbatch-"))
    (root / "data").mkdir()
    real = bili_reply.fetch
    calls = []
    bili_reply.fetch = _fake_fetch_factory(calls)
    try:
        # 最多两个视频
        s = bili_reply.run_batch(["BV1", "BV2", "BV3"], max_videos=2, root=root)
        check("按视频数停", s["videos"] == 2 and "2 个视频" in s["stopped_by"], s)
        check("只抓了前两个", calls == ["BV1", "BV2"], calls)

        calls.clear()
        s = bili_reply.run_batch(["BV1", "BV2", "BV3"], max_comments=4, root=root)
        check("按评论数停", s["comments"] <= 6 and "评论" in s["stopped_by"], s)
        check("截到第二个视频就够了", len(calls) == 2, calls)

        calls.clear()
        s = bili_reply.run_batch(["BV1", "BV2"], max_videos=1, save_it=False, root=root)
        check("不存时不写文件", s["added"] == 0, s)
    finally:
        bili_reply.fetch = real


def test_run_batch_stop_and_errors():
    root = Path(tempfile.mkdtemp(prefix="wxbatch2-"))
    (root / "data").mkdir()
    real = bili_reply.fetch
    calls = []

    def flaky(bvid, pages=2, sort=2, with_subs=False):
        calls.append(bvid)
        if bvid == "BAD":
            raise RuntimeError("这个视频炸了")
        return {"bvid": bvid, "aid": 1, "title": "t", "total": 1, "path": "old",
                "comments": [bili_reply._row(raw(1, "x"), bvid)]}

    events = []
    bili_reply.fetch = flaky
    try:
        s = bili_reply.run_batch(["BAD", "BV1"], on_event=lambda k, **kw: events.append(k),
                                 root=root)
        check("单个视频出错不影响后面", s["videos"] == 1, s)
        check("错误事件发出去了", "error" in events, events)
        check("完成的也记了", "done" in events, events)

        calls.clear()
        s = bili_reply.run_batch(["BV1", "BV2"], stop=Stop(after=1), root=root)
        check("手动停只抓一个", s["videos"] == 1, s)
        check("停的原因是手动", "手动" in s["stopped_by"], s)
    finally:
        bili_reply.fetch = real


def test_corpus_rows():
    root = Path(tempfile.mkdtemp(prefix="wxrows-"))
    (root / "data").mkdir()
    p = root / "data" / "reply_corpus.jsonl"
    p.write_text("\n".join(json.dumps({"bvid": f"BV{i}", "rpid": str(i), "text": f"第{i}条"})
                           for i in range(5)) + "\n", encoding="utf-8")
    rows = bili_reply.corpus_rows(3, p)
    check("新的在前", rows[0]["text"] == "第4条", rows[0])
    check("数量对", len(rows) == 3, len(rows))


def main():
    test_row()
    test_save_dedupe()
    test_save_needs_rpid()
    test_fetch_old_path()
    test_fetch_falls_back_to_wbi()
    test_fetch_clamps_pages()
    test_subs()
    test_throttle()
    test_targets_from_favs()
    test_run_batch_limits()
    test_run_batch_stop_and_errors()
    test_corpus_rows()
    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
