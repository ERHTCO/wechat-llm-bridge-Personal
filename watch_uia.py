"""监听器（纯 UIA 版）：不依赖 wxauto、没有监听锁、可以同时监听多个会话。

工作方式：
1. 找出所有「独立聊天窗口」（class=mmui::ChatSingleWindow）
2. 启动时把每个窗口的现有消息当作基线，全部忽略
3. 之后只处理「最后一条变了」的情况，且排除自己刚发出的回复
4. 你正在打字时完全不读（idle 守护）

已知取舍：微信的 UIA 不暴露消息方向（Role/State/宽高都一样），
所以判断"是不是我发的"只能靠自己的发送记录。你从手机上主动发的消息
可能被误当成对方发的 —— 这是当前版本的已知限制。

用法：
    watch_uia.py --test-send    先测一下能不能发消息（发给文件传输助手）
    watch_uia.py                正式监听所有独立聊天窗口
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import os as _os
_HERE = Path(_os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
sys.path.insert(0, str(_HERE))

import wxuia
import pause
import dispstate

BRIDGE = "http://127.0.0.1:8765/in"
LOG = _HERE / "watch_uia_log.txt"


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except (UnicodeEncodeError, OSError, ValueError):
        # 控制台编码是 GBK 时，群名里的 emoji（💗）会抛异常；
        # 日志文件那条路是 UTF-8，不能因为控制台打不出来就整个崩掉。
        pass
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def idle_seconds():
    import ctypes
    from ctypes import wintypes

    class LI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LI()
    info.cbSize = ctypes.sizeof(info)
    u = ctypes.windll.user32
    if not u.GetLastInputInfo(ctypes.byref(info)):
        return 999.0
    tick = ctypes.windll.kernel32.GetTickCount()
    return ((tick - info.dwTime) & 0xFFFFFFFF) / 1000.0


_api_token_cache = {"mtime": None, "token": ""}


def api_token():
    """从 config.json 读 bridge 的鉴权 token（桥会在首次启动时自动生成）。"""
    try:
        mtime = (_HERE / "config.json").stat().st_mtime
    except OSError:
        return ""
    if _api_token_cache["mtime"] != mtime:
        try:
            cfg = json.loads((_HERE / "config.json").read_text(encoding="utf-8-sig"))
            _api_token_cache["token"] = cfg.get("api_token") or ""
        except Exception:
            _api_token_cache["token"] = ""
        _api_token_cache["mtime"] = mtime
    return _api_token_cache["token"]


def ask_bridge(payload, timeout=180):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    tok = api_token()
    if tok:
        headers["X-Bridge-Token"] = tok
    req = urllib.request.Request(BRIDGE, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def looks_group(name):
    """微信群的窗口标题通常带成员数，如「某某群(3)」。"""
    return bool(re.search(r"\(\d+\)\s*$", name or ""))


IMAGE_MARK = re.compile(r"^\[img[:：]\s*(.+?)\]$")

IMAGE_DIR = _HERE / "images"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def safe_image_path(target):
    """把模型给的图片路径变成可安全发送的绝对路径；不安全就返回 None。

    模型输出会直接进 send_image，而联系人的消息又能影响模型，
    所以这里是 prompt injection 的出站口：必须锁死在 images/ 目录里。
    挡住：绝对路径、.. 穿越、UNC（\\\\server\\share，会触发 SMB 认证）、
    设备路径（\\\\?\\）、非图片扩展名。
    """
    raw = (target or "").strip().strip('"').strip("'")
    if not raw:
        return None
    # 明确拒绝 UNC 与设备路径
    if raw.startswith("\\\\") or raw.startswith("//"):
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = _HERE / raw
    try:
        real = p.resolve(strict=False)
        base = IMAGE_DIR.resolve(strict=False)
    except Exception:
        return None
    # Windows 路径不分大小写
    if str(real.parent).casefold() != str(base).casefold():
        return None
    if real.suffix.casefold() not in IMAGE_EXTS:
        return None
    return real


_external_sent = {"mtime": None, "items": {}}


def external_sent_map():
    """读 proactive 发送记录（data/self_sent.jsonl），按 mtime 缓存。

    主动搭话是 bridge 进程发的，watcher 不知道，不查这份记录就会自己回自己。
    """
    p = _HERE / "data" / "self_sent.jsonl"
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    if _external_sent["mtime"] == mtime:
        return _external_sent["items"]
    items = {}
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                if rec.get("text"):
                    items[rec["text"]] = float(rec.get("ts") or 0)
            except Exception:
                continue
    except OSError:
        return {}
    _external_sent["mtime"] = mtime
    _external_sent["items"] = items
    return items


def is_own_message(text, own_replies, window=600):
    """判断这条是不是自己刚发出去的。

    长文本用"包含"：输入框偶尔会粘上前一条残留字符，发出去的可能和模板不完全一致
    （见过多出 "ng"），严格相等会漏判，然后自己跟自己聊起来。

    短文本只认完全相等：像「图片」「在吗」「1」这种，一旦用包含匹配，
    会把对方发来的内容误判成自己发的（图片的文本就是「图片」两个字，
    而自己的回复里常常也有「图片」）。
    """
    norm = "".join((text or "").split())
    if not norm:
        return False
    short = len(norm) < 8
    now = time.time()
    pool = dict(external_sent_map())
    pool.update(own_replies)
    for old, ts in pool.items():
        if now - ts > window:
            continue
        old_norm = "".join((old or "").split())
        if not old_norm:
            continue
        if norm == old_norm:
            return True
        if short:
            continue          # 短文本不做包含匹配，避免误杀
        if norm in old_norm or old_norm in norm:
            return True
    return False


def room_key(name, rooms):
    """窗口标题 → rooms 里的键。

    微信群的窗口标题常在群名后面带成员数（「测试(3)」），也可能不带；
    两种都要能对上，否则会被当成私聊——群里每句话都回，很容易刷屏。
    """
    if not name:
        return None
    if name in rooms:
        return name
    base = re.sub(r"[\s]*[（(]\s*\d+\s*[)）]\s*$", "", name).strip()
    if base and base in rooms:
        return base
    return None


def mentions_me(text, me_name):
    """消息里是否 @ 了我本人。

    微信 @ 人时形如「@落星河\u2005内容」，中间那个是特殊空白。
    以前只判断「文本里有没有 @」，导致群里 @ 别人也会被当成在叫我。
    全角「＠」也要认——手打的时候常见。
    """
    if not text or not me_name:
        return False
    cleaned = (text.replace("\u2005", " ").replace("\u200b", "")
                   .replace("\u00a0", " ").replace("＠", "@"))
    return f"@{me_name}" in cleaned


EMAIL_LIKE = re.compile(r"\S+@\S+\.\S+")


def mentions_other(text, me_name):
    """这条是不是在 @ 别人（@ 了，但不是叫我）。

    邮箱里的 @ 不算提及（「我邮箱 xxx@qq.com」不该阻断宽限期）。
    """
    if "@" not in text and "＠" not in text:
        return False
    if mentions_me(text, me_name):
        return False
    cleaned = text.replace("＠", "@")
    cleaned = EMAIL_LIKE.sub(" ", cleaned)
    return "@" in cleaned


def _not_sent(how):
    """这个失败是不是「一个字都没发出去」。

    这些情况都是**动 UI 之前**就放弃了，所以消息还在、可以安全重来。
    和「可能已经发出去」的失败（发送后回读不到）必须分开，
    否则回滚游标会导致重复回复。

    「找不到输入框 / SetValue 失败 / 输入框内容不符」也都是**还没按 Enter**
    就放弃的（wxuia.send_text 里这几处 return 都在投递之前），以前漏在名单外，
    结果这些最常见的失败不回滚游标 —— 游标早在 POST 之前就前移了，回复永久丢。
    """
    pre = ("节流", "拿不到窗口锁", "不在前台", "失去前台", "正在操作键盘鼠标",
           "找不到输入框", "SetValue 失败", "输入框内容不符", "写剪贴板失败")
    return any(s in (how or "") for s in pre)


def _worth_retry(how):
    """这个失败值不值得 1.2 秒后原地重试。

    "你正在打字 / 拿不到锁 / 窗口不在前台"都是当前状态问题，
    立刻重试只会在你还在打字时再失败一次，纯粹浪费一轮。
    这些留给下一轮轮询；只有"找不到输入框"这类才值得马上重试。

    **「输入框清了但消息列表里没出现」绝对不能重试**：那是「可能已经发出去」，
    重试就是让真人在微信里收到同一句话两遍。
    """
    skip = ("正在操作键盘鼠标", "拿不到窗口锁", "不在前台", "失去前台", "节流",
            "消息列表里没出现")
    return not any(s in (how or "") for s in skip)


def tail_run(msgs):
    """末尾连续多少条是同一个签名。

    图片的签名全是 (ChatBubbleReferItemView, "图片")，只能靠这个数出新增了几张；
    用总条数不行——read_messages 最多 40 条，条数会封顶。
    """
    items = [m for m in msgs if not m.get("is_time")]
    if not items:
        return 0
    last = (items[-1]["kind"], items[-1]["text"])
    k = 0
    for m in reversed(items):
        if (m["kind"], m["text"]) == last:
            k += 1
        else:
            break
    return k


def collect_new(ctrl, hwnd, last_sig, last_tail=0):
    """收集「上一条处理过的消息」之后的所有新消息。

    返回 (新消息列表, 尾部重复数)，调用方要把尾部重复数存回去。

    为什么不能只看内容：图片的签名都是 (ChatBubbleReferItemView, "图片")，
    完全一样，第二张图会被当成"还是那张"漏掉。

    为什么不能看总条数：read_messages 最多只取 40 条，条数会封顶，
    n > 上次条数 永远不成立。所以改成数「末尾连续多少条是同一个签名」，
    比上次记住的多出来几条，就是新增了几条。
    """
    msgs = wxuia.read_messages(ctrl, hwnd)
    items = [m for m in msgs if not m["is_time"]]
    if not items:
        return [], 0

    sigs = [(m["kind"], m["text"]) for m in items]
    tail_sig = sigs[-1]

    def tail_run():
        k = 0
        for s in reversed(sigs):
            if s == tail_sig:
                k += 1
            else:
                break
        return k

    if last_sig and tail_sig == last_sig:
        k = tail_run()
        if k > last_tail:
            return items[-(k - last_tail):], k
        return [], k

    if last_sig and last_sig in sigs:
        idx = len(sigs) - 1 - sigs[::-1].index(last_sig)
        if idx + 1 < len(sigs):
            return items[idx + 1:], tail_run()
        return [], 0

    # 上次的签名已经被虚拟列表滚掉，只能保守地取最后一条
    return items[-1:], tail_run()


def describe_items(items):
    """把一批消息拆成 (文本列表, 图片列表, 其他类型列表)。

    表情包/语音/文件既不是文字也不是图片，转成一句说明，
    免得模型完全不知道对方发了东西。
    """
    texts, imgs, others = [], [], []
    for m in items:
        if m.get("is_image"):
            imgs.append(m)
        elif m.get("text"):
            texts.append(m["text"])
        elif not m.get("is_time"):
            others.append(m)

    for m in others:
        short = (m["kind"] or "").split("::")[-1] or "未知消息"
        if "Emoji" in short or "Emotion" in short or "Sticker" in short:
            texts.append("[对方发来一个表情包]")
        elif "Voice" in short or "Audio" in short:
            texts.append("[对方发了一条语音]")
        elif "File" in short:
            texts.append("[对方发了一个文件]")
        else:
            texts.append(f"[对方发了一条{short}]")
    return texts, imgs, others


def last_item(ctrl, hwnd):
    """最后一条非时间消息（文本或图片），返回整条记录。"""
    msgs = wxuia.read_messages(ctrl, hwnd)
    items = [m for m in msgs if not m["is_time"]]
    return items[-1] if items else None


def last_text(ctrl, hwnd):
    """最后一条**文本**消息（跳过时间分隔、图片、文件等）。

    图片在控件树里是 ChatBubbleReferItemView、名字叫「图片」，
    如果不过滤，会被当成一条文本消息，导致它对着自己发的图回话。
    """
    msgs = wxuia.read_messages(ctrl, hwnd)
    texts = [
        m for m in msgs
        if m["kind"] == "mmui::ChatTextItemView" and m["text"]
    ]
    return texts[-1]["text"] if texts else ""


def test_send(windows):
    if not windows:
        log("没有独立聊天窗口")
        return 1
    hwnd, name, ctrl = windows[0]
    stamp = time.strftime("%H:%M:%S")
    text = f"UIA 发送测试 {stamp}"
    log(f"[测试] 向「{name}」发送: {text}")
    ok, how = wxuia.send_text(ctrl, text)
    log(f"[测试] send_text -> {ok} ({how})")

    # 消息列表刷新有延迟，轮询等它出现，别只看一眼
    found = False
    for attempt in range(6):
        time.sleep(1.0)
        try:
            msgs = wxuia.read_messages(ctrl, hwnd)
        except Exception as exc:
            log(f"[测试] 读取失败: {exc}")
            continue
        texts = [m["text"] for m in msgs if not m["is_time"]]
        if text in texts:
            found = True
            log(f"[测试] 第 {attempt + 1} 次回读命中")
            break
        log(f"[测试] 第 {attempt + 1} 次未命中，当前最后一条: {texts[-1] if texts else '(空)'!r}")

    if found:
        log("[测试] 成功：消息确实发出去了")
        return 0
    log("[测试] 失败：等 6 秒仍没看到这条消息，需要换发送方式")
    return 1


def monitor_off():
    """让显示器待机（不动系统，动鼠标就回来）。返回 (状态, 说明)。

    以前这里只发一条广播、返回「发成功没」，日志写「已重新关屏」——
    发出去不等于屏幕黑了。现在走 dispstate：发完核对显示器状态，没关成就重试。
    """
    return dispstate.monitor_off()


def stop_running_instance():
    """停掉正在跑的监听（从锁文件里拿 PID）。返回被停掉的 PID 或 None。"""
    import ctypes

    path = _HERE / ".watch.lock"
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
    except Exception:
        path.unlink(missing_ok=True)
        return None

    alive = False
    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            alive = True
    except Exception:
        alive = False

    if not alive:
        path.unlink(missing_ok=True)
        return None

    try:
        os.system(f"taskkill /PID {pid} /F >nul 2>&1")
        time.sleep(1.2)
    except Exception:
        pass
    path.unlink(missing_ok=True)
    return pid


def acquire_single_lock(started_by_restart=False):
    """拿到单实例锁。started_by_restart=True 时先停掉旧的那个。"""
    import atexit

    if started_by_restart:
        old = stop_running_instance()
        if old:
            log(f"[重启] 已停掉旧监听（PID {old}）")

    import ctypes

    path = _HERE / ".watch.lock"
    if path.exists():
        try:
            pid = int(path.read_text().strip())
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return False, pid
        except Exception:
            pass
    path.write_text(str(os.getpid()))
    atexit.register(lambda: path.unlink(missing_ok=True))
    return True, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-send", action="store_true")
    ap.add_argument("--restart", action="store_true",
                    help="启动前先停掉已经在跑的监听（9-开始监听.bat 默认带这个）")
    ap.add_argument("--idle", type=float, default=2.5)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--auto-hide", action="store_true",
                    help="启动后把独立窗口藏起来；只在发送时临时弹出")
    ap.add_argument("--debounce", type=float, default=2.0,
                    help="收到消息后等 N 秒，把连发的几条收齐再回（默认 2）")
    ap.add_argument("--at-window", type=float, default=120.0,
                    help="群里被 @ 之后 N 秒内，后续消息也算在叫我（默认 120）")
    ap.add_argument("--all-windows", action="store_true",
                    help="不按白名单过滤，所有独立窗口都监听")
    ap.add_argument("--no-restore-screen", dest="restore_screen",
                    action="store_false",
                    help="发送后不要重新关屏（默认会关）")
    ap.add_argument("--restore-idle", type=float, default=10.0,
                    help="发送前你有 N 秒没动键鼠，就认为屏幕是关的，发完关回去（默认 10）")
    args = ap.parse_args()

    ok_lock, other_pid = acquire_single_lock(args.restart)
    if not ok_lock:
        log(f"已经有一个监听在跑（PID {other_pid}），先关掉它再启动")
        return 1

    windows = wxuia.find_chat_windows()
    if not windows:
        log("没有找到独立聊天窗口。请先在微信里双击一个会话，让它在屏幕上弹成独立窗口。")
        for row in wxuia.diagnose():
            log(f"  [窗口] {row}")
        log("如果上面这些窗口的 children 都是 0，说明微信版本又变了（需要降到 4.1.8.107）")
        return 1
    log(f"[OK] 发现 {len(windows)} 个独立聊天窗口: {[w[1] for w in windows]}")

    # 白名单前置：不在名单里的窗口压根不读，省得桥那边 drop 刷日志
    private_all = False
    rooms = {}
    global_triggers = []
    vision_conf = {}
    my_name = ""     # 我的微信昵称，用来判断群里 @ 的是不是我
    at_window = args.at_window   # 群里被叫到之后，多久内的消息还算在跟我说话
    if not args.all_windows:
        try:
            cfg = json.loads((_HERE / "config.json").read_text(encoding="utf-8-sig"))
            private_all = bool(cfg.get("private_all"))
            rooms = cfg.get("rooms") or {}
            global_triggers = cfg.get("triggers") or []
            vision_conf = cfg.get("vision") or {}
            my_name = (cfg.get("my_name") or "").strip()
            if cfg.get("at_window") is not None:
                at_window = float(cfg["at_window"])
            allowed = set(cfg.get("whitelist") or []) | set(rooms.keys())
        except (OSError, json.JSONDecodeError) as exc:
            log(f"[配置] 读 config.json 失败，跳过白名单过滤: {exc}")
            allowed = None

        if allowed is not None:
            if private_all:
                # 私聊全放行：只丢掉"看起来是群、但没登记"的窗口
                skipped = [w[1] for w in windows if looks_group(w[1]) and w[1] not in rooms]
                windows = [w for w in windows if not (looks_group(w[1]) and w[1] not in rooms)]
                log("[模式] private_all：私聊全放行，群聊只认 rooms 里登记的")
            else:
                skipped = [w[1] for w in windows if w[1] not in allowed]
                windows = [w for w in windows if w[1] in allowed]
            if skipped:
                log(f"[跳过] 不监听: {skipped}")
            if not windows:
                log("没有可监听的会话，退出。")
                return 1
    log(f"[监听] {[w[1] for w in windows]}")
    for _hwnd, _name, _ctrl in windows:
        _is_group = (_name in rooms) or looks_group(_name)
        _why = "在 rooms 里" if _name in rooms else ("标题带(N)" if looks_group(_name) else "都不是")
        log(f"  · 「{_name}」判定={'群' if _is_group else '私聊'}（依据：{_why}）")

    if args.test_send:
        return test_send(windows)

    last_seen = {}
    tail_count = {}  # 每个窗口末尾连续同签名几条，用来识别"内容相同的新消息"（比如连发图片）
    retry_count = {}  # 桥没响应时的重试次数
    own_replies = {}
    ignore_images_until = {}
    at_me_until = {}  # 群里被 @ 之后的有效期，后续不带 @ 的**文字**消息也算在叫我
    # 识图许可：{name: {"until": 过期时间, "left": 还能读几张}}
    # 语义是"接下来 left 张新图可以读"，过期或用完即失效；扣减发生在桥返回成功之后，
    # 所以桥抖动重试时不会把图丢掉。
    pending_vision = {}

    # 一条回复「一个字都没发出去」时（节流/没锁/不在前台），把游标退回去下轮重来，
    # 避免像以前那样静默丢掉。连续失败 3 次就放弃，防止无限重试烧 token。
    defer_count = {}
    for hwnd, name, ctrl in windows:
        try:
            first = last_item(ctrl, hwnd)
            last_seen[name] = (first["kind"], first["text"]) if first else ("", "")
            # 基线必须连条数一起记，否则第一轮会把历史消息当成新的全处理一遍
            tail_count[name] = tail_run(wxuia.read_messages(ctrl, hwnd))
        except Exception:
            last_seen[name] = ("", "")
            tail_count[name] = 0
        log(f"[基线] {name}: {last_seen[name][1][:30]!r}（尾部同签名 {tail_count.get(name, 0)} 条）")

    if args.auto_hide:
        for hwnd, name, _ctrl in windows:
            wxuia.hide_window(hwnd)
        log(f"[隐藏] {len(windows)} 个窗口已收起，发送时才临时弹出")

    log("开始监听，Ctrl+C 退出")

    def idle_guard():
        """拿到发送锁之后再做一次"你正在打字吗"的检查。

        只在调模型之前查一次不够：模型要跑几秒到几十秒，
        这期间你很可能已经开始打字，SetValue("") 会把你的输入清掉。
        """
        return idle_seconds() >= args.idle

    last_loop = time.time()
    suspend_gap = 90.0  # 两轮间隔超过这个数，就认为中间休眠过
    loop_i = 0          # 轮数，用来定期重扫窗口
    prev_clock = pause.clock_pair()   # (含睡眠的毫秒, 不含睡眠的秒)，用来分「睡了」和「卡了」

    while True:
        try:
            now_ts = time.time()
            gap = now_ts - last_loop
            # 两个时钟一减就知道中间是不是真睡过；跑得慢时两个一起走，差值是 0
            cur_clock = pause.clock_pair()
            slept = 0.0
            if prev_clock and cur_clock:
                slept = pause.slept_seconds(cur_clock[0] - prev_clock[0],
                                            cur_clock[1] - prev_clock[1])
            prev_clock = cur_clock or prev_clock
            kind = pause.gap_kind(gap, slept, suspend_s=suspend_gap)
            if kind == "resume":
                # 电脑休眠/挂起过：微信会把积压消息一次性灌进来，
                # 全回会刷屏并触发风控。这里重置基线，只处理醒来之后的新消息。
                log(f"[唤醒] 睡了 {int(slept)} 秒，跳过积压消息，重置基线")
                for h2, n2, c2 in windows:
                    try:
                        it = last_item(c2, h2)
                        last_seen[n2] = (it["kind"], it["text"]) if it else ("", "")
                        tail_count[n2] = tail_run(wxuia.read_messages(c2, h2))
                    except Exception:
                        pass
                time.sleep(1.0)
                last_loop = time.time()
                continue
            if kind == "slow":
                # 不是睡眠，是我们自己卡了（UIA 调用阻塞）。这时候重置基线
                # 会把卡顿期间的真实消息丢掉，所以只记一笔，继续正常处理。
                log(f"[慢] 距上一轮 {int(gap)} 秒（睡眠 {int(slept)} 秒），不重置基线")
            last_loop = now_ts

            if idle_seconds() < args.idle:
                time.sleep(0.5)
                continue

            # 每 60 轮重扫一次窗口：关掉的会话要移除，新开的会话要补进来。
            # 以前启动时扫一次就再也不变，窗口一关那个会话就永远读不到了。
            loop_i += 1
            if loop_i % 60 == 0:
                try:
                    fresh = {h: (n, c) for h, n, c in wxuia.find_chat_windows()}
                    alive, added = [], []
                    for h, n, c in windows:
                        if h in fresh:
                            alive.append((h, n, fresh[h][1]))
                        else:
                            log(f"[重扫] 窗口没了: {n}")
                            # 窗口关了就把它挂着的状态一起清掉，免得同名窗口
                            # 重开时"继承"一个早就该失效的识图许可
                            pending_vision.pop(n, None)
                            at_me_until.pop(n, None)
                    for h, (n, c) in fresh.items():
                        if not any(h == x[0] for x in alive):
                            added.append((h, n, c))
                            log(f"[重扫] 新窗口: {n}")
                            pending_vision.pop(n, None)   # 新窗口从不带旧许可
                            at_me_until.pop(n, None)
                            item = None
                            try:
                                item = last_item(c, h)
                            except Exception:
                                pass
                            last_seen[n] = (item["kind"], item["text"]) if item else ("", "")
                            try:
                                tail_count[n] = tail_run(wxuia.read_messages(c, h))
                            except Exception:
                                tail_count[n] = 0
                    if added or len(alive) != len(windows):
                        windows[:] = alive + added
                except Exception as exc:
                    log(f"[重扫] 失败: {type(exc).__name__}: {exc}")

            for idx, (hwnd, name, ctrl) in enumerate(windows):
                try:
                    new_items, cur_count = collect_new(
                        ctrl, hwnd, last_seen.get(name), tail_count.get(name, 0)
                    )
                except Exception as exc:
                    log(f"  ! 读消息失败({name}): {type(exc).__name__}")
                    # 控件对象可能失效了，重建一次
                    try:
                        rebuilt = wxuia.auto.ControlFromHandle(hwnd)
                        windows[idx] = (hwnd, name, rebuilt)
                    except Exception:
                        log(f"  ! 「{name}」窗口可能已关闭，等重扫移除")
                    continue
                if not new_items:
                    tail_count[name] = cur_count
                    continue

                # 防抖：对方可能在连发（文字+图片），等这一串收齐再回一条
                time.sleep(args.debounce)
                try:
                    again, cur_count = collect_new(
                        ctrl, hwnd, last_seen.get(name), tail_count.get(name, 0)
                    )
                    if len(again) > len(new_items):
                        new_items = again
                except Exception:
                    pass

                now = time.time()
                last_raw = new_items[-1]   # 过滤前的最后一条，用来推进游标
                # 先逐条剔掉「自己刚发的」，不要等拼成一整段再判：
                # 否则「自己的回复 + 对方的图片」会被当成一整段自己发的，图片跟着丢。
                new_items = [
                    m for m in new_items
                    if m.get("is_image")
                    or not (m.get("text") and is_own_message(m["text"], own_replies))
                ]
                if not new_items:
                    # 这批全是我自己发的：必须把游标推进，否则每秒重取一次，
                    # 等 own_replies 过期后又会把旧消息当新消息回。
                    last_seen[name] = (last_raw["kind"], last_raw["text"])
                    tail_count[name] = cur_count
                    log("  (这批都是我发的，跳过)")
                    continue
                texts, imgs, others = describe_items(new_items)
                if others:
                    log(f"  (非文字消息类型: {sorted({m['kind'] for m in others})})")
                joined = "\n".join(texts)

                # 群里常见的「@我」或「触发词」后面紧跟一张图/一句话：多等一会儿收进来
                rkey = room_key(name, rooms)
                room_conf = rooms.get(rkey or name) or {}
                triggers = [t for t in (room_conf.get("triggers") or []) + global_triggers if t]
                short_mention = (
                    not imgs
                    and len(joined.strip()) < 14
                    and (mentions_me(joined, my_name) or any(t in joined for t in triggers))
                )
                if short_mention:
                    time.sleep(1.0)
                    try:
                        more, cur_count = collect_new(
                            ctrl, hwnd, last_seen.get(name), tail_count.get(name, 0)
                        )
                        if len(more) > len(new_items):
                            new_items = more
                            texts, imgs, _ = describe_items(new_items)
                            joined = "\n".join(texts)
                            now = time.time()
                    except Exception:
                        pass

                last_sig = (new_items[-1]["kind"], new_items[-1]["text"])
                prev_seen = last_seen.get(name)
                prev_tail = tail_count.get(name, 0)
                tail_count[name] = cur_count

                if imgs and now < ignore_images_until.get(name, 0):
                    # 自己刚发出去的那张图，不算对方发来的
                    last_seen[name] = last_sig
                    continue

                last_seen[name] = last_sig

                # 群还是私聊，先判掉，后面识图规则要用
                rkey = room_key(name, rooms)
                is_group = (rkey is not None) or looks_group(name)
                if is_group and rkey is None:
                    # 像群、但没登记：绝不能降级成私聊（private_all 会把群里每句话都放行）
                    log(f"  ! 「{name}」像群但不在 rooms 里，跳过不回复")
                    continue

                # 识图规则：
                #   私聊：总是读图
                #   群聊：必须「@我 / 叫名字」且同时说了开启词（如「识图」）才临时开启，
                #         读完一张自动关，超时也自动关
                vwords = [w for w in (vision_conf.get("enable_words") or []) if w]
                called = mentions_me(joined, my_name) or any(t in joined for t in triggers)
                if is_group:
                    said_enable = next(
                        (w for w in vwords if w in joined), None
                    ) if called else None
                else:
                    said_enable = None   # 私聊不用开

                if said_enable:
                    ttl = float(vision_conf.get("ttl_seconds", 300))
                    pending_vision[name] = {
                        "until": now + ttl,
                        "left": int(vision_conf.get("max_images", 1)),
                    }
                    log(f"  (识图已开启：等 {pending_vision[name]['left']} 张新图，"
                        f"{int(ttl)} 秒内有效)")

                # 过期清理
                pv = pending_vision.get(name)
                if pv and (now >= pv["until"] or pv["left"] <= 0):
                    pending_vision.pop(name, None)
                    log("  (识图许可已失效，回到常态)")

                # 私聊随时可读；群里必须有尚未用掉的许可
                vision_on = (not is_group) or bool(pending_vision.get(name))

                # 只是「@我 识图」、图还没来：直接回一句等图提示，
                # 别去问模型——它会说「我这边没收到图片」，听着像在推脱。
                if is_group and said_enable and not imgs:
                    hint = (vision_conf.get("ask_reply")
                            or "好的，我在等图片，请发过来。")
                    try:
                        wxuia.ensure_visible(hwnd)
                        tctrl = wxuia.auto.ControlFromHandle(hwnd)
                    except Exception:
                        tctrl = ctrl
                    ok, how = wxuia.send_text(tctrl, hint, hwnd=hwnd, guard=idle_guard)
                    log(f"  > {hint}  (等图提示，未调模型)")
                    log(f"  (发送 {ok} / {how})")
                    if ok:
                        own_replies[hint] = time.time()
                        last_seen[name] = ("mmui::ChatTextItemView", hint)
                        try:
                            tail_count[name] = tail_run(wxuia.read_messages(ctrl, hwnd))
                        except Exception:
                            pass
                        at_me_until[name] = time.time() + at_window
                    continue

                if imgs and not vision_on:
                    if not joined.strip():
                        last_seen[name] = last_sig
                        log("  (群里没开启识图，这张图跳过)")
                        continue
                    imgs = []   # 有文字就只回文字，不碰图片

                images = []
                will_consume = 0
                pv_before = None   # 回滚游标时把识图许可原样还回去（见下面的 abort 分支）
                if imgs:
                    # 群里按许可数量决定读几张；私聊不限。
                    # 一个批次可能有多张图（断线重连会一次性涌入），以前只读最后一张。
                    budget = len(imgs) if not is_group else int(
                        (pending_vision.get(name) or {}).get("left", 0))
                    # 重读一次拿最新 bbox（防抖和桥调用可能隔了几秒到几十秒）
                    try:
                        fresh = [m for m in wxuia.read_messages(ctrl, hwnd)
                                 if m.get("is_image") and not m.get("is_time")]
                        if fresh:
                            imgs = fresh[-budget:] if budget > 0 else []
                    except Exception:
                        pass

                    if budget > 0 and imgs:
                        # 右键是屏幕坐标操作，窗口必须在前台；三个窗口叠在一起时
                        # 会点到别人身上。真实结果交给 copy_image_via_menu 内部校验。
                        if not wxuia.raise_window(hwnd):
                            log("  ! 窗口抬不到前台，这张图跳过")

                    for item in imgs[:budget]:
                        # 矩形必须还在窗口里，否则右键/抓图会指到别的消息
                        try:
                            wl, wt, wr, wb = wxuia.window_rect(hwnd)
                            bl, bt, br, bb = item["bbox"]
                            tol = 60
                            if not (bl >= wl - tol and bt >= wt - tol
                                    and br <= wr + tol and bb <= wb + tol):
                                log(f"  ! 图片矩形超出窗口，跳过这张 "
                                    f"图={item['bbox']} 窗口={(wl, wt, wr, wb)}")
                                continue
                        except Exception:
                            pass

                        status, data = "failed", None
                        try:
                            status, data = wxuia.copy_image_via_menu(item["bbox"], hwnd=hwnd)
                        except Exception as exc:
                            log(f"  ! 右键复制失败: {exc}")

                        if status == "not_image":
                            # 表情包：不读图，只告诉模型收到了
                            log("  (这是表情包，不启动识图)")
                            texts.append("[对方发来一个表情包]")
                            joined = "\n".join(texts)
                            continue

                        if not data:
                            if status == "ok":
                                # 「ok」= 右键菜单里确认过有「另存为」，是真图片，
                                # 只是复制没取到 → 可以用 WGC 抓窗口兜底
                                try:
                                    import wgc_capture

                                    if wxuia.is_minimized(hwnd):
                                        wxuia.ensure_visible(hwnd)
                                        time.sleep(0.4)
                                    data, err = wgc_capture.capture_bbox(hwnd, item["bbox"])
                                    if data:
                                        log("  (原图复制失败，改用 WGC 抓窗口)")
                                    elif err:
                                        log(f"  (WGC 也没拿到: {err})")
                                except Exception as exc:
                                    log(f"  ! WGC 失败: {type(exc).__name__}: {exc}")
                            else:
                                # 「failed」= 右键菜单压根没弹出来，不知道这块是图片还是表情包。
                                # 微信不提供表情的图片数据，这时候硬抓窗口，
                                # 就等于把表情截成图发给模型——违反"表情不读"的约定，不抓。
                                log("  (菜单没弹出来，不确定是图片还是表情，不抓)")

                            if not data:
                                # 明确告诉模型"没打开"，它就不会编图里的内容
                                texts.append("[对方发来一张图片，但这边没能打开]")
                                joined = "\n".join(texts)
                                continue

                        if data:
                            images.append(base64.b64encode(data).decode())
                            will_consume += 1
                        else:
                            log("  ! 这张图拿不到内容，跳过")

                text = (joined or "[对方发来一张图片]") if images else joined

                if not text:
                    continue
                if is_own_message(text, own_replies):
                    log("  (这是我自己发的，跳过)")
                    continue

                extra = len(new_items) - 1
                suffix = f"（合并 {len(new_items)} 条）" if extra > 0 else ""
                log(f"[收] {name}: {text[:40].replace(chr(10), ' / ')}"
                    f"{' [+图]' if images else ''}{suffix}")
                # 群里怎么算"在叫我"：
                #   1) 消息里有 @
                #   2) 消息里出现触发词（每个群可单独配，另有全局 triggers）
                #   3) 上面任一条命中后的 --at-window 秒内，后续消息都算在跟我说话
                hit_trigger = next((t for t in triggers if t in text), None)
                now_at = time.time()
                # 这条是不是在 @ 别人（有 @ 但不是叫我）
                mentions_someone_else = mentions_other(text, my_name)
                at_me = mentions_me(text, my_name) if is_group else True
                if is_group and said_enable and "@" not in text:
                    # 群里说「开启识图」这类话，本身就是在叫我
                    at_me = True
                    log(f"  (识图开关词「{said_enable}」)")
                if is_group and imgs and vision_on and not at_me:
                    # 刚被要求识图，紧接着来的图就是给我看的
                    at_me = True
                    log("  (识图已开启，这张图算在叫我)")
                if is_group and hit_trigger and "@" not in text:
                    at_me = True
                    log(f"  (触发词「{hit_trigger}」)")
                if is_group and at_me:
                    at_me_until[name] = now_at + at_window
                elif (is_group and not mentions_someone_else
                      and now_at < at_me_until.get(name, 0)):
                    # 宽限期只接"没 @ 人的普通话"。
                    # 别人之间 @ 来 @ 去的时候不能插嘴——18:32 那次
                    # 「@某同学 桥牌好玩不」就是被宽限期接住回的。
                    at_me = True
                    log(f"  (群内 {int(at_me_until[name] - now_at)} 秒内刚被叫过，继续处理)")
                try:
                    r = ask_bridge({
                        "from": name,
                        "name": name,
                        "text": text,
                        # 群必须带 roomid，桥才会按群规则（要 @）处理
                        "roomid": (rkey or name) if is_group else "",
                        "at_me": at_me,
                        "images": images,
                    })
                except Exception as exc:  # 含 URLError / Timeout / JSON 错
                    # 桥没回话：把游标退回去，下一轮重试；重试 3 次还不行才放弃
                    tries = retry_count.get(name, 0) + 1
                    if tries <= 3:
                        retry_count[name] = tries
                        last_seen[name] = prev_seen
                        tail_count[name] = prev_tail
                        log(f"  ! 桥没响应（第 {tries} 次）: {exc}，下轮重试")
                    else:
                        retry_count[name] = 0
                        log(f"  ! 桥连续没响应，放弃这条: {exc}")
                    time.sleep(1.5)
                    continue
                retry_count[name] = 0

                act = r.get("action") or ""
                reply = r.get("reply") or ""
                log(f"  > {reply or '（无回复）'}  [{r.get('elapsed_ms')}ms]  action={act}")
                # 桥没让发（drop / error / 空回复）就什么都不做。
                # 以前这里只看 reply，模型失败的兜底话术会被直接发出去。
                if act != "send" or not reply:
                    log(f"  (桥没有要发的内容，action={act or '-'}，不发)")
                    continue

                idle_before = idle_seconds()  # 发送前你多久没动键鼠
                if not args.no_send:
                    # 调模型可能花了几秒到几十秒，这里必须再查一次：
                    # 你这会儿开始打字的话，发送时的 SetValue("") 会把你输入的内容清掉。
                    if idle_seconds() < args.idle:
                        log("  (你正在操作，这条先不发，等下一轮)")
                        # 这里回滚了游标，下一轮会重新处理这批。
                        # 所以识图许可不能在这之前扣——否则「@识图 → 图在下一批」
                        # 时许可已经空，下一轮直接判"没开启识图"把图丢掉。
                        last_seen[name] = prev_seen
                        tail_count[name] = prev_tail
                        continue

                    # 确认这一轮真的会发送，才扣识图许可
                    if will_consume:
                        pv_before = dict(pending_vision.get(name) or {})
                        pv = pending_vision.get(name)
                        if pv:
                            pv["left"] = int(pv["left"]) - will_consume
                            if pv["left"] <= 0:
                                pending_vision.pop(name, None)
                                log(f"  (读了 {will_consume} 张，识图许可用完了)")
                            else:
                                log(f"  (读了 {will_consume} 张，还能再读 {pv['left']} 张)")
                    # 发送前保证窗口可用：最小化/离屏时输入框控件不存在
                    if wxuia.ensure_visible(hwnd):
                        log("  (窗口原本不可用，已还原)")
                        try:
                            ctrl = wxuia.auto.ControlFromHandle(hwnd)
                        except Exception:
                            pass
                    if args.auto_hide:
                        wxuia.show_window(hwnd)
                        time.sleep(0.3)
                        # 隐藏再显示会重建 UIA 树，旧控件对象可能已失效
                        try:
                            ctrl = wxuia.auto.ControlFromHandle(hwnd)
                        except Exception:
                            pass
                    # 回复里可能同时有文字和 [img:...]，微信一条消息只能发一种，
                    # 所以拆成多段，按顺序逐条发（文字 → 图片 → 文字…）
                    # 另外支持用单独一行 --- 把回复拆成两条（先回一句，再追问一句）
                    chunks = [c for c in re.split(r"\n\s*[-—]{3,}\s*\n", reply) if c.strip()]
                    segments = []
                    for chunk in chunks:
                        segments.extend(
                            s.strip() for s in re.split(r"(\[img[:：][^\]]+\])", chunk) if s.strip()
                        )
                    sent_any = False
                    last_kind, last_text = None, None
                    aborted, abort_why = False, ""
                    for seg in segments:
                        if not seg:
                            continue
                        try:
                            ctrl = wxuia.auto.ControlFromHandle(hwnd)
                        except Exception:
                            pass
                        m = IMAGE_MARK.match(seg)
                        if m:
                            path = safe_image_path(m.group(1))
                            if path is None:
                                log(f"  ! 拒绝发送这个路径（只允许 images/ 下的图片）: "
                                    f"{m.group(1)[:80]}")
                                continue
                            if not path.exists():
                                log(f"  ! 要发的图不存在: {path}")
                                continue
                            ok, how = wxuia.send_image(ctrl, str(path), hwnd=hwnd)
                            if not ok and _worth_retry(how):
                                time.sleep(1.2)
                                ok, how = wxuia.send_image(ctrl, str(path), hwnd=hwnd)
                                how = f"重试: {how}"
                            log(f"  (发图 {ok} / {how})")
                            if ok:
                                ignore_images_until[name] = time.time() + 4
                                sent_any = True
                                last_kind, last_text = "mmui::ChatBubbleReferItemView", "图片"
                        else:
                            ok, how = wxuia.send_text(ctrl, seg, hwnd=hwnd, guard=idle_guard)
                            if not ok and _worth_retry(how):
                                time.sleep(1.2)
                                try:
                                    ctrl = wxuia.auto.ControlFromHandle(hwnd)
                                except Exception:
                                    pass
                                ok, how = wxuia.send_text(ctrl, seg, hwnd=hwnd, guard=idle_guard)
                                how = f"重试: {how}"
                            log(f"  (发送 {ok} / {how})")
                            if not ok:
                                if _not_sent(how):
                                    aborted, abort_why = True, how
                                log("  ! 发送失败，窗口诊断:")
                                try:
                                    for line in wxuia.diagnose_window(hwnd):
                                        log("    " + line)
                                except Exception as exc:
                                    log(f"    诊断失败: {exc}")
                            else:
                                own_replies[seg] = time.time()
                                sent_any = True
                                last_kind, last_text = "mmui::ChatTextItemView", seg
                        time.sleep(0.35)

                    if aborted and not sent_any:
                        tries = defer_count.get(name, 0) + 1
                        defer_count[name] = tries
                        if tries <= 3:
                            log(f"  (这次一个字都没发出去（{abort_why}），"
                                f"游标回滚，下轮重来 {tries}/3)")
                            last_seen[name] = prev_seen
                            tail_count[name] = prev_tail
                            if pv_before is not None:
                                # 一个字都没发出去、游标回滚了：许可也还回去，
                                # 否则「@识图 → 图 → 发送失败」这一轮用完许可，
                                # 下一轮会判「没开启识图」把图直接丢掉。
                                pending_vision[name] = pv_before
                                log(f"  (识图许可也还回去了：{pv_before.get('left')} 张)")
                            continue
                        log(f"  ! 连续 {tries} 次没发出去，放弃这条：{abort_why}")
                        defer_count[name] = 0
                    else:
                        defer_count[name] = 0

                    if sent_any and last_kind:
                        own_replies[reply] = time.time()
                        last_seen[name] = (last_kind, last_text)

                    # 群里：一次 @ 只回一回。回完就把这次"叫我"作废，
                    # 否则宽限期内每来一句话都会接，很容易刷屏。
                    # 识图许可（pending_vision）是独立的，不受这里影响：
                    # 「@我 识图」之后等图这件事，不会因为回了一句话就作废。
                    if is_group:
                        at_me_until.pop(name, None)
                        if pending_vision.get(name):
                            log("  (识图许可还在，等对方把图发过来)")
                        else:
                            log("  (群里已回，再聊需要重新 @)")
                    if args.auto_hide:
                        time.sleep(0.4)
                        wxuia.hide_window(hwnd)

                    # 屏幕原本是关着的（你很久没动），发送时的模拟输入把它点亮了，
                    # 这里再关回去，避免半夜被屏幕闪醒。
                    if args.restore_screen and idle_before > args.restore_idle:
                        time.sleep(0.6)
                        ok, why = monitor_off()
                        if ok:
                            log(f"  (你已有 {int(idle_before)} 秒没动，已重新关屏：{why})")
                        else:
                            log(f"  ! 关屏没成功：{why}")

            time.sleep(args.interval)
        except KeyboardInterrupt:
            log("bye")
            return 0
        except Exception:
            log("!! 出错：")
            log(traceback.format_exc())
            time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
