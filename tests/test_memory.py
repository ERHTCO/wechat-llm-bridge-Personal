"""长期记忆的回归测试。

    .venv\\Scripts\\python.exe tests\\test_memory.py

不变量：记住的东西一定是「跟这个人有关、以后还用得上」的一句；召回只给相关的，
到期的不会再冒出来；被召回过一次的记忆会变强。
"""
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import memory  # noqa: E402

PASS = 0
FAIL = 0
NOW = time.time()


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def env(enabled=True):
    root = Path(tempfile.mkdtemp(prefix="wxmem-"))
    (root / "data").mkdir()
    cfg = {"memory": {"enabled": enabled, "every_turns": 3, "top_k": 5,
                      "min_score": 0.12, "model": "deepseek-chat"}}
    (root / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return root, cfg


def test_add_and_dedupe():
    root, _ = env()
    conn = memory.connect(root)
    n = memory.add(conn, "wxid_alice", [
        {"text": "对方是航空学院的学生", "kind": "fact", "importance": 0.8},
        {"text": "对方是航空学院的学生", "kind": "fact", "importance": 0.8},
        {"text": "对方喜欢白米饭", "kind": "preference", "importance": 0.6},
    ], NOW)
    check("同一句只收一次", n == 2, f"got {n}")
    total = conn.execute("select count(*) from atoms").fetchone()[0]
    check("库里两条", total == 2)
    n2 = memory.add(conn, "林岸", [{"text": "对方是航空学院的学生", "kind": "fact"}], NOW)
    check("不同人各自记", n2 == 1)
    conn.close()


def test_ttl_by_kind():
    root, _ = env()
    conn = memory.connect(root)
    memory.add(conn, "wxid_alice", [
        {"text": "对方明天要去交表", "kind": "plan", "importance": 0.5},
        {"text": "对方是学生", "kind": "fact", "importance": 0.5},
    ], NOW)
    rows = {r["text"]: r for r in conn.execute("select * from atoms")}
    plan = rows["对方明天要去交表"]
    fact = rows["对方是学生"]
    check("plan 的 TTL 比 fact 短", plan["expires_at"] < fact["expires_at"])
    check("plan 的 TTL 按重要性放大：2 天 x 1.5", abs(plan["expires_at"] - NOW - 3 * 86400) < 60)
    conn.close()


def test_recall():
    root, cfg = env()
    conn = memory.connect(root)
    memory.add(conn, "wxid_alice", [
        {"text": "对方是航空学院的学生", "kind": "fact", "importance": 0.8},
        {"text": "对方喜欢机械键盘", "kind": "preference", "importance": 0.7},
        {"text": "对方养了一只叫毛球的猫", "kind": "fact", "importance": 0.6},
    ], NOW)
    conn.close()
    block = memory.recall("wxid_alice", "你学的什么专业来着", cfg, root, NOW)
    check("有相关记忆就注入", block.startswith("【你记得的事】"), repr(block[:40]))
    check("挑对了那条", "航空学院" in block, block)
    check("不相关的没进来", "毛球" not in block, block)
    block2 = memory.recall("wxid_alice", "今天天气怎么样啊", cfg, root, NOW)
    check("完全不相关就不注入", block2 == "" or "航空" not in block2, repr(block2))
    conn = memory.connect(root)
    used = conn.execute("select uses from atoms where text like '%航空%'").fetchone()["uses"]
    check("被召回的强化了一次", used == 1, used)
    conn.close()


def test_recall_disabled():
    root, cfg = env(enabled=False)
    conn = memory.connect(root)
    memory.add(conn, "wxid_alice", [{"text": "对方是学生", "kind": "fact"}], NOW)
    conn.close()
    check("关掉就不召回", memory.recall("wxid_alice", "你是学生吗", cfg, root, NOW) == "")
    check("关掉也不计数", memory.tick("wxid_alice", cfg, root) is False)


def test_expired_not_recalled():
    root, cfg = env()
    conn = memory.connect(root)
    memory.add(conn, "wxid_alice", [{"text": "对方下周要考驾照", "kind": "plan", "importance": 0.9}], NOW)
    conn.execute("update atoms set expires_at=?", (NOW - 1,))
    conn.commit()
    conn.close()
    memory.decay(memory.connect(root), "wxid_alice", NOW + 2 * 86400)
    conn = memory.connect(root)
    st = conn.execute("select status from atoms").fetchone()["status"]
    conn.close()
    check("过期后转 expired", st == "expired", st)
    check("过期的不会被召回", memory.recall("wxid_alice", "驾照考了吗", cfg, root, NOW + 2 * 86400) == "")


def test_decay():
    root, cfg = env()
    conn = memory.connect(root)
    memory.add(conn, "wxid_alice", [{"text": "对方是学生", "kind": "fact", "importance": 1.0}], NOW)
    conn.execute("INSERT INTO turns(wxid, since, last_decay) VALUES('wxid_alice',0,?) ON CONFLICT(wxid) DO UPDATE SET last_decay=excluded.last_decay", (NOW - 10 * 86400,))
    conn.commit()
    conn.close()
    conn = memory.connect(root)
    memory.decay(conn, "wxid_alice", NOW)
    imp = conn.execute("select importance from atoms").fetchone()["importance"]
    conn.close()
    check("十天掉 0.1 左右", abs(imp - 0.9) < 0.02, imp)
    conn = memory.connect(root)
    check("同一天不会重复衰减", memory.decay(conn, "wxid_alice", NOW + 3600) == 0)
    conn.close()


def test_tick_threshold():
    root, cfg = env()
    check("第一次不够", memory.tick("wxid_alice", cfg, root) is False)
    check("第二次不够", memory.tick("wxid_alice", cfg, root) is False)
    check("第三次到阈值", memory.tick("wxid_alice", cfg, root) is True)
    check("过了阈值还在涨", memory.tick("wxid_alice", cfg, root) is True)


def test_parse():
    check("纯 JSON", memory._parse('[{"text":"a"}]') == [{"text": "a"}])
    check("带代码块", memory._parse('```json\n[{"text":"a"}]\n```') == [{"text": "a"}])
    check("空数组", memory._parse("[]") == [])
    check("废话里夹数组", memory._parse('好的，结果：[{"text":"a"}] 完毕') == [{"text": "a"}])
    check("没有数组就给空", memory._parse("我不知道") == [])
    check("坏 JSON 给空", memory._parse("[{oops") == [])


def test_summarize():
    root, cfg = env()
    seen = {}

    def ask(prompt):
        seen["p"] = prompt
        return '[{"text":"对方是航空学院的学生","kind":"fact","importance":0.8},' \
               '{"text":"对方喜欢机械键盘","kind":"preference","importance":0.6}]'

    n = memory.summarize(cfg, root, "wxid_alice", "对方：我学航空的\n我：哦", ask=ask, now=NOW)
    check("抽出两条", n == 2, n)
    check("提示词里有对话", "我学航空的" in seen["p"])
    again = memory.summarize(cfg, root, "wxid_alice", "对方：我学航空的\n我：哦", ask=ask, now=NOW)
    check("重复的不再收", again == 0, again)
    conn = memory.connect(root)
    since = conn.execute("select since from turns where wxid='wxid_alice'").fetchone()["since"]
    conn.close()
    check("总结完轮次清零", since == 0, since)


def test_summarize_no_backend():
    root, cfg = env()
    n = memory.summarize(cfg, root, "wxid_alice", "对方：嗨", ask=None, now=NOW)
    check("没有可用后端就静默跳过", n == 0)


def test_prompt_injection():
    import backends
    tpl = backends.load_template(HERE)
    if "{{memory}}" not in tpl:
        # 公开仓库里只有 prompt.example.txt，没有 {{memory}} 占位符；本条跳过
        check("模板里没有 {{memory}}（跳过）", True)
        return
    clean = backends.Context(wxid="x", name="x", text="t", history=[], persona="p",
                             template=tpl, memory_text="")
    check("没有记忆时不留占位符", "{{memory}}" not in clean.prompt())
    full = backends.Context(wxid="x", name="x", text="t", history=[], persona="p",
                            template=tpl, memory_text="【你记得的事】\n- 对方是学生")
    out = full.prompt()
    check("有记忆时写进提示词", "对方是学生" in out)
    check("位置在聊天记录之前", out.index("对方是学生") < out.index("聊天记录"))


def test_explain_is_readonly():
    """召回测试只算不改：调多少次都不能强化，也不能改状态。"""
    root, cfg = env()
    conn = memory.connect(root)
    memory.add(conn, "wxid_alice", [
        {"text": "对方是航空学院的学生", "kind": "fact", "importance": 0.8},
        {"text": "对方养了一只叫毛球的猫", "kind": "fact", "importance": 0.6},
    ], NOW)
    conn.close()

    r = memory.explain("wxid_alice", "你学的什么专业", cfg, root, now=NOW)
    check("回报开关状态", r["enabled"] is True)
    check("有分项", r["hits"][0]["parts"] is not None)
    check("命中的里有航空那条", any("航空" in h["text"] and h["hit"] for h in r["hits"]))
    check("不相关的没命中", all(not h["hit"] for h in r["hits"] if "毛球" in h["text"]))
    check("给了会注入的 id", len(r["would_inject"]) >= 1)
    check("block 带表头", r["block"].startswith("【你记得的事】"))
    check("block 条数等于注入条数", r["block"].count("- 对方") == len(r["would_inject"]))

    conn = memory.connect(root)
    uses = conn.execute("select sum(uses) from atoms").fetchone()[0]
    conn.close()
    check("算分不会强化", uses == 0, uses)
    memory.explain("wxid_alice", "你学的什么专业", cfg, root, now=NOW)
    conn = memory.connect(root)
    uses = conn.execute("select sum(uses) from atoms").fetchone()[0]
    conn.close()
    check("算两次还是不强化", uses == 0, uses)

    empty = memory.explain("陌生人", "你学的什么专业", cfg, root, now=NOW)
    check("没记忆的人返回空", empty["hits"] == [] and empty["block"] == "")


def test_list_atoms():
    root, cfg = env()
    conn = memory.connect(root)
    memory.add(conn, "wxid_alice", [
        {"text": "对方喜欢机械键盘", "kind": "preference", "importance": 0.9},
        {"text": "对方是学生", "kind": "fact", "importance": 0.3},
    ], NOW)
    memory.add(conn, "林岸", [{"text": "对方养了只橘猫", "kind": "fact"}], NOW)
    conn.close()

    allrows = memory.list_atoms(root=root)
    check("默认只看活跃", len(allrows) == 3)
    check("按重要性排序", allrows[0]["text"] == "对方喜欢机械键盘")
    mine = memory.list_atoms("林岸", root=root)
    check("按人筛", len(mine) == 1 and mine[0]["wxid"] == "林岸")
    kw = memory.list_atoms(None, q="猫", root=root)
    check("按正文筛", len(kw) == 1 and "橘猫" in kw[0]["text"])
    memory.forget_many([mine[0]["id"]], root)
    check("删掉的从默认列表消失", len(memory.list_atoms(root=root)) == 2)
    gone = memory.list_atoms(None, status="forgotten", root=root)
    check("能在已忘记里查到", len(gone) == 1)


def test_stats():
    root, cfg = env()
    conn = memory.connect(root)
    memory.add(conn, "wxid_alice", [
        {"text": "对方喜欢机械键盘", "kind": "preference"},
        {"text": "对方是学生", "kind": "fact"},
    ], NOW)
    conn.close()
    s = memory.stats(root)
    check("总数对", s["total"] == 2)
    check("活跃数对", s.get("active") == 2)
    check("还没被召回", s["recalled"] == 0)
    check("按人分组", s["people"][0]["wxid"] == "wxid_alice" and s["people"][0]["n"] == 2)


def make_chatdb(root, rows):
    """造一个有 messages 表的 chat.db；rows = [(role, text, ts), ...]，返回 id 列表。"""
    p = root / "data" / "chat.db"
    c = sqlite3.connect(p)
    c.execute("create table if not exists messages("
              "id integer primary key, wxid text, role text, text text, ts real, msg_key text)")
    ids = []
    for role, text, ts in rows:
        cur = c.execute("insert into messages(wxid, role, text, ts) values(?,?,?,?)",
                        ("林岸", role, text, ts))
        ids.append(cur.lastrowid)
    c.commit()
    c.close()
    return ids


def test_sources_recorded():
    root, _ = env()
    conn = memory.connect(root)
    n = memory.add(conn, "wxid_alice", [
        {"text": "对方养了只橘猫", "kind": "fact", "importance": 0.7},
        {"text": "对方常熬夜", "kind": "preference", "importance": 0.5},
    ], NOW, sources=[11, 12, 13])
    conn.close()
    check("两条都写进去了", n == 2, n)
    counts = memory.source_count(root)
    check("每条都记了三个来源", sorted(counts.values()) == [3, 3], counts)
    atom_id = min(counts)
    check("sources_of 拿到 id", memory.sources_of(atom_id, root) == [11, 12, 13])

    conn = memory.connect(root)
    memory.add(conn, "wxid_alice", [{"text": "对方养了只橘猫", "kind": "fact"}], NOW, sources=[99])
    conn.close()
    check("重复的原子不额外记来源", memory.sources_of(atom_id, root) == [11, 12, 13])

    conn = memory.connect(root)
    memory.add(conn, "wxid_alice", [{"text": "对方喜欢白米饭", "kind": "fact"}], NOW, sources=[])
    conn.close()
    check("不给来源就一条都不记", len(memory.source_count(root)) == 2)


def test_detail_with_chatdb():
    root, _ = env()
    ids = make_chatdb(root, [("them", "我叫林岸，学航空的", NOW),
                             ("me", "林岸，记住了", NOW + 1)])
    conn = memory.connect(root)
    memory.add(conn, "林岸", [{"text": "对方叫林岸，学航空", "kind": "fact"}],
               NOW + 2, sources=ids)
    conn.close()
    atom_id = memory.list_atoms(root=root)[0]["id"]
    d = memory.detail(atom_id, root)
    check("detail 拿得到原子", d and d["text"] == "对方叫林岸，学航空")
    check("来源两条", len(d["sources"]) == 2, d.get("sources"))
    check("第一条是对方", d["sources"][0]["who"] == "对方")
    check("原文取回了", "我叫林岸" in d["sources"][0]["text"], d["sources"][0])
    check("第二条是我", d["sources"][1]["who"] == "我")
    check("带时间", bool(d["sources"][0]["at"]))


def test_detail_missing_sources():
    root, _ = env()
    conn = memory.connect(root)
    memory.add(conn, "林岸", [{"text": "对方叫林岸", "kind": "fact"}], NOW, sources=[999])
    conn.close()
    atom_id = memory.list_atoms(root=root)[0]["id"]
    d = memory.detail(atom_id, root)
    check("chat.db 不存在也不炸", d is not None)
    check("缺失的来源标出来", "原文已删" in d["sources"][0]["text"], d["sources"])
    check("detail 找不到的 id 返回 None", memory.detail(99999, root) is None)


def test_list_atoms_has_source_count():
    root, _ = env()
    conn = memory.connect(root)
    memory.add(conn, "林岸", [{"text": "对方叫林岸", "kind": "fact"}], NOW, sources=[1, 2])
    memory.add(conn, "林岸", [{"text": "对方喜欢白米饭", "kind": "fact"}], NOW)
    conn.close()
    rows = {r["text"]: r["sources"] for r in memory.list_atoms(root=root)}
    check("有来源的显示条数", rows["对方叫林岸"] == 2, rows)
    check("没来源的是 0", rows["对方喜欢白米饭"] == 0, rows)


def test_gate_matches_score():
    """_gate 和 score 必须同一套判定，否则界面说「过线」而实际不注入。"""
    cases = [("你学的什么专业", "对方是航空学院的学生"),     # 过线
             ("我那只猫叫什么来着", "对方养了只橘猫叫毛球"),  # 过线
             ("今天天气", "对方养了只橘猫叫毛球")]           # 不过线
    for q, a in cases:
        parts = memory._parts(q, a, 0.8, NOW, NOW)
        gated = memory._gate(parts)
        s = memory.score(q, a, 0.8, NOW, NOW, NOW, {})
        if gated is None:
            check(f"过线时 score>0 [{a}]", s > 0, s)
        else:
            check(f"卡住时 score=0 [{a}]", s == 0.0, s)
    check("过期优先于重合", memory._gate({"shared": 9, "rel": 0.9}, expired=True) == "expired")
    check("少于两字卡住", memory._gate({"shared": 1, "rel": 0.9}) == "shared<2")
    check("覆盖率低卡住", memory._gate({"shared": 3, "rel": 0.05}) == "rel<0.15")
    check("没字对卡住", memory._gate(None) == "empty")


def test_explain_carries_gate():
    root, cfg = env()
    conn = memory.connect(root)
    memory.add(conn, "wxid_alice", [
        {"text": "对方是航空学院的学生", "kind": "fact", "importance": 0.8},
        {"text": "对方喜欢白米饭", "kind": "fact", "importance": 0.6},
    ], NOW)
    conn.close()
    r = memory.explain("wxid_alice", "你学的什么专业", cfg, root, now=NOW)
    by_text = {h["text"]: h for h in r["hits"]}
    check("命中的 gate 是空串", by_text["对方是航空学院的学生"]["gate"] == "", by_text)
    check("没命中的给出原因", by_text["对方喜欢白米饭"]["gate"] == "shared<2", by_text)


def test_summarize_passes_sources():
    root, cfg = env()
    seen = {}

    def ask(prompt):
        return '[{"text":"对方叫林岸","kind":"fact","importance":0.8}]'

    def fake_add(conn, wxid, items, now=None, sources=()):
        seen["sources"] = list(sources)
        return 0

    real_add = memory.add
    memory.add = fake_add
    try:
        memory.summarize(cfg, root, "林岸", "对方：我叫林岸", ask=ask, now=NOW,
                         sources=[7, 8])
    finally:
        memory.add = real_add
    check("sources 一路传到 add", seen.get("sources") == [7, 8], seen)


def main():
    test_add_and_dedupe()
    test_ttl_by_kind()
    test_recall()
    test_recall_disabled()
    test_expired_not_recalled()
    test_decay()
    test_tick_threshold()
    test_parse()
    test_summarize()
    test_summarize_no_backend()
    test_prompt_injection()
    test_explain_is_readonly()
    test_list_atoms()
    test_stats()
    test_sources_recorded()
    test_detail_with_chatdb()
    test_detail_missing_sources()
    test_list_atoms_has_source_count()
    test_gate_matches_score()
    test_explain_carries_gate()
    test_summarize_passes_sources()
    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
