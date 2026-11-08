"""提示词试验台：用一批固定用例跑当前（或候选）提示词，只看回复长什么样。

用法：
    python prompt_lab.py                    # 用 prompt.txt
    python prompt_lab.py prompt.new.txt     # 用候选文件，不改线上

它只调模型、打印回复，不碰微信、不写数据库、不发消息。
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import backends  # noqa: E402
import schedule  # noqa: E402

# 和 watch_uia.py 里拆条用的正则保持一致
SPLIT = re.compile(r"\n\s*[-—]{3,}\s*\n")
MAX_LINES, MAX_CHARS = 8, 600

CASES = [
    ("在吗", False, ""),
    ("你是谁", False, ""),
    ("哈哈哈哈你好搞笑", False, ""),
    ("@落星河 你是傻逼", True, ""),
    ("今天第几节课", False, ""),
    ("[对方发来一个表情包]", False, ""),
    ("图片看不懂", False, ""),
    ("我最近真的烦死了，天天上课写作业，老师还一直催，"
     "晚上回宿舍还要开会，压根没有自己的时间", False, ""),
]


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "prompt.txt"
    if not path.is_absolute():
        path = HERE / path
    template = path.read_text(encoding="utf-8")
    print(f"提示词：{path.name}（{len(template.splitlines())} 行）\n")

    cfg = {}
    cfg_path = HERE / "config.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    backend = backends.build(cfg, HERE)
    timetable = schedule.timetable_text() + "\n\n" + schedule.classes_text()
    persona = cfg.get("persona", "")      # 和线上一致，别硬编码成空串

    # 渲染自检：拼错的占位符不会被替换，会原样发给模型（模型不会照抄回来，看不出来）
    probe = backends.Context(
        wxid="试验", name="wxid_alice", text="", history=[], persona=persona,
        at_me=False, template=template, emoji_ok=False,
        images_text="- images/示例.png", me_name="落星河", timetable=timetable,
    )
    rendered = probe.prompt()
    leftovers = sorted(set(re.findall(r"\{\{[^}]*\}\}", rendered)))
    if leftovers:
        print(f"⚠ 有占位符没被替换：{leftovers}")
        print("  拼错或代码不支持 → 会原样发给模型，请检查\n")

    for i, (text, at_me, images_text) in enumerate(CASES, 1):
        ctx = backends.Context(
            wxid="试验", name="wxid_alice", text=text, history=[], persona=persona,
            at_me=at_me, template=template, emoji_ok=False,
            images_text=images_text, me_name="落星河", timetable=timetable,
        )
        print(f"[{i}] 对方：{text}")
        try:
            reply = backend.reply(ctx) or ""
        except Exception as exc:
            print(f"    失败：{type(exc).__name__}: {exc}\n")
            continue

        # 拆条预览：线上按 --- 拆成多条依次发，lab 里也照这个规则看
        chunks = [c.strip() for c in SPLIT.split(reply) if c.strip()]
        if len(chunks) > 1:
            for j, chunk in enumerate(chunks, 1):
                print(f"    【第{j}条】")
                for line in chunk.splitlines():
                    print(f"      {line}")
        else:
            for line in reply.splitlines() or [""]:
                print(f"    {line}")

        if len(reply) > MAX_CHARS or len(reply.splitlines()) > MAX_LINES:
            print(f"    ⚠ 超过 {MAX_LINES} 行 / {MAX_CHARS} 字，线上会被截断")
        if "[img:" in reply or "[img：" in reply:
            bad = [c for c in re.findall(r"\[img[:：]\s*([^\]]+)\]", reply)
                   if not c.strip().startswith("images/")]
            if bad:
                print(f"    ⚠ 发图路径不是 images/ 开头，线上会被拒发：{bad}")
        print()


if __name__ == "__main__":
    main()
