"""关键词订阅的离线测试：不发网络请求、不碰真实收藏夹。

    .venv\\Scripts\\python.exe tests\\test_bili_sub.py
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import bili_sub  # noqa: E402

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


def video(bvid, play, age_days=1.0):
    return {"bvid": bvid, "title": f"video {bvid}", "author": "up",
            "play": play, "pubdate": int(datetime.now().timestamp() - age_days * 86400),
            "desc": "", "source": "test"}


class Fake:
    """把模块级的搜索/收藏换成计数器。"""

    def __init__(self, results=None):
        self.results = results or {}
        self.searches = []
        self.added = []

    def search(self, kw, limit=20, page=1):
        self.searches.append(kw)
        return list(self.results.get(kw, []))

    def add(self, bvids, reason=""):
        self.added.append((list(bvids), reason))
        return 0


def with_env(conf, state=None):
    """临时目录 + 假配置，避免碰真实 data/。"""
    tmp = Path(tempfile.mkdtemp(prefix="bili-sub-"))
    (tmp / "data").mkdir()
    bili_sub.DATA = tmp / "data"
    bili_sub.CONF = tmp / "data" / "bili_sub.json"
    bili_sub.STATE = tmp / "data" / "bili_sub_state.json"
    bili_sub.save_conf(conf)
    if state:
        bili_sub.save_state(state)
    return tmp


def main():
    real = (bili_sub.DATA, bili_sub.CONF, bili_sub.STATE,
            bili_sub.bili_search.search, bili_sub.bili_fav.add)

    print("\n[1] 时段判断")
    w = ["09:00", "23:00"]
    check("12:00 在内", bili_sub.in_window(datetime(2026, 10, 26, 12, 0), w))
    check("07:00 在外", not bili_sub.in_window(datetime(2026, 10, 26, 7, 0), w))
    w2 = ["22:00", "02:00"]
    check("跨零点 01:00 在内", bili_sub.in_window(datetime(2026, 10, 26, 1, 0), w2))
    check("写坏了也不崩", bili_sub.in_window(datetime(2026, 10, 26, 12, 0), ["乱写", "x"]))

    print("\n[2] 到期判断")
    now = datetime(2026, 10, 26, 15, 0)
    conf = {"interval_hours": 6, "keywords": ["机械键盘", "装机"]}
    check("从没搜过 → 到期",
          set(bili_sub.due_keywords(conf, {}, now)) == {"机械键盘", "装机"})
    state = {"机械键盘": {"last_run": now.timestamp() - 3600},
             "装机": {"last_run": now.timestamp() - 7 * 3600}}
    check("刚搜过的不到期", bili_sub.due_keywords(conf, state, now) == ["装机"])

    print("\n[3] 选题：门槛 / 去重 / 挑最热")
    fake = Fake({"机械键盘": [video("BVlow", 100), video("BVhot", 500000),
                              video("BVold", 900000, age_days=90),
                              video("BVseen", 800000)]})
    bili_sub.bili_search.search = fake.search
    conf2 = dict(bili_sub.DEFAULT, keywords=["机械键盘"], min_play=10000, max_age_days=30)
    state2 = {"机械键盘": {"seen": ["BVseen"]}}
    pick = bili_sub.pick("机械键盘", conf2, state2)
    check("挑到最热的那个（不是最旧/已见/低播放）",
          pick and pick["bvid"] == "BVhot", pick)

    fake.results["机械键盘"] = [video("BVlow", 100)]
    check("全都不达标 → None", bili_sub.pick("机械键盘", conf2, state2) is None)

    print("\n[4] dry-run 不收藏")
    with_env(dict(bili_sub.DEFAULT, enabled=True, keywords=["机械键盘"]))
    fake = Fake({"机械键盘": [video("BVhot", 500000)]})
    bili_sub.bili_search.search = fake.search
    bili_sub.bili_fav.add = fake.add
    rc = bili_sub.run(dry=True, force=True)
    check("返回 0", rc == 0)
    check("搜了 1 次", len(fake.searches) == 1, fake.searches)
    check("一次都没收藏", fake.added == [], fake.added)
    # 这条是踩过的坑：早版本 dry-run 会把候选写进 seen，等于把料吃掉了
    check("dry-run 不改状态", bili_sub.load_state() == {}, bili_sub.load_state())

    print("\n[5] 真跑（add 是假的）：receive per_run 限制")
    with_env(dict(bili_sub.DEFAULT, enabled=True, keywords=["机械键盘", "装机"],
                  per_run=1))
    fake = Fake({"机械键盘": [video("BVa", 500000)],
                 "装机": [video("BVb", 600000)]})
    bili_sub.bili_search.search = fake.search
    bili_sub.bili_fav.add = fake.add
    bili_sub.run(dry=False, force=True)
    check("per_run=1 只收 1 条", len(fake.added) == 1, fake.added)
    check("reason 带订阅来源",
          fake.added and fake.added[0][1].startswith("关键词订阅:"), fake.added)
    state3 = bili_sub.load_state()
    check("状态记了 seen", any((state3.get(k) or {}).get("seen") for k in state3), state3)

    print("\n[6] 关闭时不动")
    with_env(dict(bili_sub.DEFAULT, enabled=False, keywords=["机械键盘"]))
    fake = Fake({"机械键盘": [video("BVa", 500000)]})
    bili_sub.bili_search.search = fake.search
    bili_sub.bili_fav.add = fake.add
    bili_sub.run(dry=False, force=False)
    check("关了就不搜", fake.searches == [], fake.searches)
    check("关了就不收", fake.added == [], fake.added)

    print("\n[7] 搜索炸了不影响别的词")
    with_env(dict(bili_sub.DEFAULT, enabled=True, keywords=["坏的", "好的"], per_run=3))

    def boom(kw, limit=20, page=1):
        if kw == "坏的":
            raise RuntimeError("network down")
        return [video("BVok", 500000)]

    fake = Fake()
    bili_sub.bili_search.search = boom
    bili_sub.bili_fav.add = fake.add
    rc = bili_sub.run(dry=False, force=True)
    check("整体不抛异常", rc == 0)
    check("好的那个照样收", len(fake.added) == 1 and fake.added[0][0] == ["BVok"], fake.added)

    # 还原
    (bili_sub.DATA, bili_sub.CONF, bili_sub.STATE,
     bili_sub.bili_search.search, bili_sub.bili_fav.add) = real

    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
