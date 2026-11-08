"""Obsidian 库的回归测试。

    .venv\\Scripts\\python.exe tests\\test_vault.py

不变量：写真进库的东西一定是 UTF-8 无 BOM，且不碰 .obsidian\\。
规则：日记一天只自动写一次；收藏说明已经存在就不覆盖（人可能补过字）。
"""
import json
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import vault  # noqa: E402

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


def make_env(vault_on=True):
    """造一个数据根 + 一个空的库。"""
    root = Path(tempfile.mkdtemp(prefix="wxvault-"))
    (root / "data").mkdir()
    v = root / "vault"
    v.mkdir()
    cfg = {"vault": str(v) if vault_on else "", "vault_daily_hour": 23}
    (root / "config.json").write_text(json.dumps(cfg, ensure_ascii=False),
                                      encoding="utf-8")
    conn = sqlite3.connect(root / "data" / "chat.db")
    conn.execute("create table messages(id integer primary key, wxid text, role text, text text, ts real, msg_key text)")
    conn.execute("create table events(id integer primary key, ts real, wxid text, text text, action text, reason text, reply text, elapsed_ms integer)")
    conn.commit()
    conn.close()
    return root, v


def add_msg(root, wxid, role, text, ts):
    conn = sqlite3.connect(root / "data" / "chat.db")
    conn.execute("insert into messages(wxid,role,text,ts) values(?,?,?,?)",
                 (wxid, role, text, ts))
    conn.commit()
    conn.close()


def no_bom(p):
    b = Path(p).read_bytes()
    return not b.startswith(b"\xef\xbb\xbf")


def test_front():
    f = vault._front({"date": "2026-10-22", "tags": ["日记", "微信桥"]})
    check("frontmatter 以 --- 开头", f.startswith("---\n"))
    check("frontmatter 数组用方括号", "tags: [日记, 微信桥]" in f)
    check("frontmatter 结尾是 ---", f.rstrip("\n").endswith("---"))


def test_clean():
    check("去掉非法字符", vault._clean("BV1 a/b:c*d?e\"f<g>h|i") == "BV1 a b c d e f g h i")
    check("吃掉方括号", vault._clean("[[双链]]") == "双链")
    check("空名字有兜底", vault._clean("   ") == "未命名")
    check("过长截断", len(vault._clean("字" * 200, 60)) == 60)


def test_wan():
    check("一万是 1 万", vault._wan(10000) == "1 万")
    check("两万五", vault._wan(25000) == "2.5 万")
    check("一亿", vault._wan(100000000) == "1 亿")
    check("三千万", vault._wan(31000123) == "3100 万")
    check("非数字兜底", vault._wan(None) == "-")


def test_daily_writes():
    root, v = make_env()
    add_msg(root, "wxid_alice", "them", "在吗", 1793000000)
    add_msg(root, "wxid_alice", "me", "在呢", 1793000010)
    p = vault.daily(root, "2026-10-26")
    check("日记写出来了", p is not None and p.exists())
    check("落在 日记/ 下", p.parent.name == "日记")
    check("文件名是日期", p.name == "2026-10-26.md")
    text = p.read_text(encoding="utf-8")
    check("没有 BOM", no_bom(p))
    check("有日期标题", "# 2026-10-26" in text)
    check("有概览表", "| 收到 | 回复 | 没回 | 收藏 |" in text)
    check("统计来自 messages", "| 1 | 1 | 0 | 0 |" in text)
    check("分出了会话", "### wxid_alice" in text)
    check("带了时间", "**对方** 在吗" in text and "**我** 在呢" in text)
    check("四个目录都建了", all((v / d).is_dir() for d in vault.DIRS))


def test_daily_idempotent():
    root, v = make_env()
    add_msg(root, "wxid_alice", "them", "嗨", 1793000000)
    p = vault.daily(root, "2026-10-26")
    m1 = p.stat().st_mtime_ns
    vault.daily(root, "2026-10-26")
    check("内容没变就不重写", p.stat().st_mtime_ns == m1)
    add_msg(root, "wxid_alice", "them", "又一句", 1793000100)
    vault.daily(root, "2026-10-26")
    check("内容变了才重写", p.stat().st_mtime_ns != m1)
    check("新消息进去了", "又一句" in p.read_text(encoding="utf-8"))


def test_daily_long():
    root, v = make_env()
    for i in range(vault.MAX_MSG_LINES + 20):
        add_msg(root, "wxid_alice", "them", f"第{i}句", 1793000000 + i)
    p = vault.daily(root, "2026-10-26")
    text = p.read_text(encoding="utf-8")
    check("超长消息被截断", "…" in text)
    check("给出了省略提示", "没展开" in text or "只展开了" in text)


def test_daily_no_vault():
    root, v = make_env(vault_on=False)
    check("没配库时返回 None", vault.daily(root, "2026-10-26") is None)


def test_maybe_daily():
    root, v = make_env()
    add_msg(root, "wxid_alice", "them", "在吗", 1793000000)
    early = datetime(2026, 10, 26, 9, 0)
    check("没到点不写", vault.maybe_daily(None, root, early) is None)
    late = datetime(2026, 10, 26, 23, 30)
    r1 = vault.maybe_daily(None, root, late)
    check("到点写了一次", r1 is not None and "日记已写" in r1)
    check("日记文件在", (v / "日记" / "2026-10-26.md").exists())
    check("同一天不写第二遍", vault.maybe_daily(None, root, late) is None)
    check("状态记在数据根，不在库", (root / "data" / "vault_state.json").exists())
    nxt = datetime(2026, 10, 27, 23, 5)
    check("第二天又能写", vault.maybe_daily(None, root, nxt) is not None)


def test_fav_note():
    root, v = make_env()
    info = {"bvid": "BV1GEnG63EkH", "title": "【全748集】Python 全套教程",
            "owner": {"name": "Python中国官方"}, "stat": {"view": 26146}}
    p = vault.fav_note(info, "关键词订阅:Python", root)
    check("收藏笔记写出来了", p is not None and p.exists())
    check("落在 收藏/ 下", p.parent.name == "收藏")
    check("文件名带 bvid", p.name.startswith("BV1GEnG63EkH "))
    text = p.read_text(encoding="utf-8")
    check("没有 BOM", no_bom(p))
    check("有链接", "https://www.bilibili.com/video/BV1GEnG63EkH" in text)
    check("有 up主", "up主：Python中国官方" in text)
    check("播放量转万", "播放：2.6 万" in text)
    check("理由进 frontmatter", "收进来的理由: 关键词订阅:Python" in text)


def test_fav_note_no_overwrite():
    root, v = make_env()
    info = {"bvid": "BV1xx", "title": "标题/带斜杠"}
    p = vault.fav_note(info, "第一次", root)
    p.write_text("人手改过的内容", encoding="utf-8")
    again = vault.fav_note(info, "第二次", root)
    check("不覆盖已有笔记", again.read_text(encoding="utf-8") == "人手改过的内容")
    check("斜杠被换掉", "/" not in p.name)


def test_fav_note_minimal():
    root, v = make_env()
    p = vault.fav_note({"bvid": "BV1mini"}, "", root)
    check("只有 bvid 也能写", p is not None and p.exists())
    text = p.read_text(encoding="utf-8")
    check("缺 up主就不写那行", "up主：" not in text)
    check("没 bvid 就什么都不写", vault.fav_note({}, "", root) is None)


def test_backfill():
    root, v = make_env()
    log = root / "data" / "bili_fav_log.jsonl"
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"ok": True, "day": "2026-10-27", "bvid": "BV1a", "title": "第一个", "reason": "关键词订阅:装机"},
        {"ok": True, "day": "2026-10-27", "bvid": "BV1b", "title": "第二个", "reason": "聊天驱动:wxid_alice"},
        {"ok": False, "day": "2026-10-27", "bvid": "BV1c", "title": "失败的不算"},
        {"ok": True, "day": "2026-10-27", "bvid": "BV1a", "title": "第一个", "reason": "重复"},
    ]) + "\n", encoding="utf-8")
    vault.backfill(root)
    made = sorted(p.name for p in (v / "收藏").glob("*.md"))
    check("只补成功的", len(made) == 2)
    check("重复的只写一份", sum("BV1a" in n for n in made) == 1)
    check("失败的不写", not any("BV1c" in n for n in made))


def test_fav_link_matches_file():
    """日记里的 [[双链]] 必须指得到真正的笔记文件，标题长了也不能断。"""
    root, v = make_env()
    long_title = "【全748集】绝对是B站最全最细的Python零基础全套教程（包含爬虫+数据分析），从入门到精通，两周学完"
    vault.fav_note({"bvid": "BV1GEnG63EkH", "title": long_title}, "关键词订阅:Python", root)
    log = root / "data" / "bili_fav_log.jsonl"
    log.write_text(json.dumps({"ok": True, "day": "2026-10-27", "bvid": "BV1GEnG63EkH",
                               "title": long_title, "reason": "关键词订阅:Python"},
                              ensure_ascii=False) + "\n", encoding="utf-8")
    p = vault.daily(root, "2026-10-27")
    text = p.read_text(encoding="utf-8")
    import re
    links = re.findall(r"\[\[([^\]]+)\]\]", text)
    check("日记里有双链", len(links) == 1)
    check("双链指得到文件", (v / "收藏" / f"{links[0]}.md").exists(),
          f"目标 {links[0]}")


def make_db(path, msgs=(), drops=()):
    """造一个只有最小表结构的 chat.db。"""
    conn = sqlite3.connect(path)
    conn.execute("create table if not exists messages(id integer primary key, wxid text, role text, text text, ts real, msg_key text)")
    conn.execute("create table if not exists events(id integer primary key, ts real, wxid text, text text, action text, reason text, reply text, elapsed_ms integer)")
    for wxid, role, text, ts in msgs:
        conn.execute("insert into messages(wxid,role,text,ts) values(?,?,?,?)",
                     (wxid, role, text, ts))
    for wxid, text, ts in drops:
        conn.execute("insert into events(ts,wxid,text,action) values(?,?,?,'drop')",
                     (ts, wxid, text))
    conn.commit()
    conn.close()
    return path


def test_merge_two_dbs():
    """两个 chat.db 并起来：重合的去重，各自独有的都进得来。"""
    root, v = make_env()
    add_msg(root, "wxid_alice", "them", "重合的一句", 1793000000)
    add_msg(root, "wxid_alice", "me", "源码独有的回复", 1793000010)
    other = make_db(root / "other.db",
                    msgs=[("wxid_alice", "them", "重合的一句", 1793000000),
                          ("wxid_alice", "them", "只有另一个库有", 1793000100)])
    p = vault.daily(root, "2026-10-26", force=True, also=[other])
    text = p.read_text(encoding="utf-8")
    check("另一库的消息进来了", "只有另一个库有" in text)
    check("重合的只出现一次", text.count("重合的一句") == 1)
    check("源码独有还在", "源码独有的回复" in text)
    check("统计按去重后算", "| 2 | 1 | 0 | 0 |" in text)


def test_history():
    """history 把所有库里的日子都写一遍。"""
    root, v = make_env()
    add_msg(root, "wxid_alice", "them", "第一天的", 1793000000)
    other = make_db(root / "other.db",
                    msgs=[("wxid_alice", "them", "第二天的", 1793086400)])
    made = vault.history(root, also=[other])
    names = sorted(p.name for p in made)
    check("两天都写了", names == ["2026-10-26.md", "2026-10-27.md"], str(names))
    check("第一天有内容", "第一天的" in (v / "日记" / "2026-10-26.md").read_text(encoding="utf-8"))
    check("第二天有内容", "第二天的" in (v / "日记" / "2026-10-27.md").read_text(encoding="utf-8"))


def test_drop_stats():
    """没回 = 被丢掉、而且最终也没回过的那些。被丢后又回了的只算收到。"""
    root, v = make_env()
    add_msg(root, "wxid_alice", "them", "被回的", 1793000000)
    add_msg(root, "wxid_alice", "me", "回了", 1793000010)
    conn = sqlite3.connect(root / "data" / "chat.db")
    conn.execute("insert into events(ts,wxid,text,action) values(?,?,?,'drop')",
                 (1793000005, "wxid_alice", "被回的"))
    conn.execute("insert into events(ts,wxid,text,action) values(?,?,?,'drop')",
                 (1793000006, "wxid_alice", "没人理的"))
    conn.commit()
    conn.close()
    text = vault.daily(root, "2026-10-26", force=True).read_text(encoding="utf-8")
    check("收到=落库与被丢的并集", "| 2 | 1 | 1 | 0 |" in text,
          [ln for ln in text.splitlines() if ln.startswith("| ")][:3])


def test_digest():
    root, v = make_env()
    msgs = ([{"wxid": "wxid_alice", "role": "them", "text": "嗨", "ts": i} for i in range(6)]
            + [{"wxid": "测试", "role": "them", "text": "你好", "ts": 100}])
    d = vault.digest(msgs)
    check("会话按条数从多到少", d.index("【wxid_alice】") < d.index("【测试】"))
    check("带角色前缀", "对方：嗨" in d)
    check("超长正文被截断", len(vault.digest(
        [{"wxid": "wxid_alice", "role": "them", "text": "字" * 200, "ts": 1}])) < 200)
    tiny = vault.digest([{"wxid": "wxid_alice", "role": "them", "text": f"第{i}句" * 20, "ts": i}
                         for i in range(200)], budget=200)
    check("超预算时说明有多少没放进去", "没放进来" in tiny)
    check("超预算时材料确实短", len(tiny) < 400)
    check("空输入返回空串", vault.digest([]) == "")


def test_summarize_with_fake():
    root, v = make_env()
    seen = {}

    def ask(prompt):
        seen["prompt"] = prompt
        return "  今天定了周三交表。\n晚上聊了装机。  ", "  先看材料，再挑重点。  "

    msgs = [{"wxid": "wxid_alice", "role": "them", "text": "周三交表", "ts": 1}]
    text, think = vault.summarize(msgs, "2026-10-26", ask)
    check("摘要去掉了首尾空白", text == "今天定了周三交表。\n晚上聊了装机。")
    check("思考也带回来了", think == "先看材料，再挑重点。")
    check("提示词里有日期", "2026-10-26" in seen["prompt"])
    check("提示词里有材料", "周三交表" in seen["prompt"])


def test_daily_with_summary():
    root, v = make_env()
    add_msg(root, "wxid_alice", "them", "周三交表", 1793000000)
    made = []

    def ask(prompt):
        made.append(1)
        return "今天定了周三交表。", "看了材料，只有一句，直接记。"
    p = vault.daily(root, "2026-10-26", force=True, ask=ask)
    text = p.read_text(encoding="utf-8")
    check("模型只叫了一次", len(made) == 1)
    check("有「这天」段", "## 这天" in text)
    check("摘要进去了", "今天定了周三交表。" in text)
    check("有「思考」段", "## 思考" in text)
    check("思考用折叠 callout", "> [!quote]- 模型怎么想的" in text)
    check("思考正文在引用里", "> 看了材料，只有一句，直接记。" in text)
    check("思考在结尾", text.rstrip().splitlines()[-1].startswith(">"))


def test_daily_summary_failure():
    root, v = make_env()
    add_msg(root, "wxid_alice", "them", "在吗", 1793000000)

    def boom(prompt):
        raise RuntimeError("agent 断了")
    p = vault.daily(root, "2026-10-26", force=True, ask=boom)
    text = p.read_text(encoding="utf-8")
    check("模型挂了日记照样写", p.exists() and "在吗" in text)
    check("模型挂了就没有那段", "## 这天" not in text and "## 思考" not in text)


def test_daily_no_summary():
    root, v = make_env()
    add_msg(root, "wxid_alice", "them", "在吗", 1793000000)
    text = vault.daily(root, "2026-10-26", force=True, ask=None).read_text(encoding="utf-8")
    check("ask=None 不写摘要段", "## 这天" not in text)


def test_config_with_bom():
    """记事本存的 config.json 带 BOM，读不到就等于「没配库」，会静默空转。"""
    root, v = make_env()
    add_msg(root, "wxid_alice", "them", "在吗", 1793000000)
    raw = (root / "config.json").read_text(encoding="utf-8")
    (root / "config.json").write_bytes(b"\xef\xbb\xbf" + raw.encode("utf-8"))
    p = vault.daily(root, "2026-10-26", force=True, ask=None)
    check("带 BOM 的配置也读得到", p is not None and p.exists())
    check("库路径取到了", vault.vault_root(root) == v)


def test_inbox():
    root, v = make_env()
    vault.ensure_dirs(v)
    (v / "收件箱" / "想法.md").write_text("帮我查一下这个", encoding="utf-8")
    (v / "收件箱" / "另一个.md").write_text("还有这个", encoding="utf-8")
    (v / "收件箱" / "不是笔记.txt").write_text("忽略", encoding="utf-8")
    items = vault.inbox(root)
    check("只收 .md", len(items) == 2)
    check("带内容", any("帮我查一下这个" in it["text"] for it in items))
    check("带名字", {it["name"] for it in items} == {"想法", "另一个"})


def test_dot_obsidian_untouched():
    root, v = make_env()
    (v / ".obsidian").mkdir()
    (v / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in (v / ".obsidian").iterdir()}
    vault.daily(root, "2026-10-26")
    vault.fav_note({"bvid": "BV1x"}, "理由", root)
    after = {p.name: p.read_bytes() for p in (v / ".obsidian").iterdir()}
    check(".obsidian 一个字节都没动", before == after)


def main():
    test_front()
    test_clean()
    test_wan()
    test_daily_writes()
    test_daily_idempotent()
    test_daily_long()
    test_daily_no_vault()
    test_maybe_daily()
    test_fav_note()
    test_fav_note_no_overwrite()
    test_fav_note_minimal()
    test_fav_link_matches_file()
    test_merge_two_dbs()
    test_history()
    test_drop_stats()
    test_digest()
    test_summarize_with_fake()
    test_daily_with_summary()
    test_daily_summary_failure()
    test_daily_no_summary()
    test_config_with_bom()
    test_backfill()
    test_inbox()
    test_dot_obsidian_untouched()
    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
