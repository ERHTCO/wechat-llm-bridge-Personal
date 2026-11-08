"""列出当前所有聊天窗口，方便复制名字填进 config.json 的 rooms。

群聊会被标出来（标题带 (数字) 的，或者你自己认得的）。
"""
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import wxuia


def main():
    cfg = json.loads((_HERE / "config.json").read_text(encoding="utf-8"))
    rooms = cfg.get("rooms") or {}

    wins = wxuia.find_chat_windows()
    if not wins:
        print("没有找到聊天窗口。")
        print("先把想监听的会话（包括群）在微信里双击成独立窗口，再跑这个。")
        return 1

    print(f"当前有 {len(wins)} 个聊天窗口：\n")
    print(f"{'名字':<28} {'像群?':<6} {'已在 rooms?':<10}")
    print("-" * 52)
    for _hwnd, name, _ctrl in wins:
        looks_group = bool(re.search(r"\(\d+\)\s*$", name))
        in_rooms = "是" if name in rooms else "否"
        print(f"{name:<28} {'是' if looks_group else '不是':<6} {in_rooms:<10}")

    print("\n要加进群白名单，就把上面某一行「名字」原样填进 config.json：")
    print('  "rooms": { "<这个名字>": { "require_at": true } }')
    print("\n注意：加完要重启监听（9-开始监听.bat）才生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
