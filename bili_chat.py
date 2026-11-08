"""聊天驱动收藏：从一句微信消息里看出「想要视频」，去 B 站搜一条收进收藏夹。

    from bili_chat import on_message

    on_message("有没有机械键盘的视频", "wxid_xxx")
    # {"ok": True, "action": "fav", "reason": "faved:机械键盘", "dry": False,
    #  "keyword": "机械键盘", "signal": "video", "bvid": "BV...", "title": "...",
    #  "play": 123456, "fav_reason": "聊天驱动:wxid_xxx:机械键盘", "fav_today": 3}

    on_message(text, wxid, dry=True)     # 演练：真搜一次，但不写收藏夹

为什么长成这个形状：
  - bridge 的回复链路不能被收藏功能带崩。on_message 只返回 dict，永不抛异常；
    连 bili_fav 没登录时抛的 SystemExit（BaseException，except Exception 兜不住）
    也单独拦下，否则 daemon 线程会安静地死掉，事后还查不到原因。
  - 聊天里绝大多数话和视频无关，而搜一次要一次网络往返。所以先纯正则筛信号，
    没信号就一个请求都不发（"在吗"/"吃饭了吗"/"哈哈" 到不了搜索那一步）。
  - 写入口只有 bili_fav.add()：它内部查重、每天限 DAILY_CAP 条、失败不抛、写日志。
    这里绝不自己拼收藏 API——收藏要 csrf，拼错就是往用户账号里写脏数据。
  - 一条消息最多收 MAX_FAV_PER_MSG 条，不做"多收几条"的花活。

返回值的 action：
  fav   真收了（dry=True 时是"将要收"，dry 字段为 True）
  skip  正常跳过：没信号 / 抽不出词 / 没结果 / 今天满额
  error 搜索或收藏出错。建议 bridge 只记日志，别把这种失败说给对方听。
"""
import re

# 只在模块顶部绑定一次，测试里直接把这三个名字换成计数桩。
# 注：bili_search 不需要登录，实测连发 3 次正常（见 bili_search.py 顶部）；
# 收藏要登录 + csrf，全部由 bili_fav.add 负责。
from bili_fav import DAILY_CAP, add, today_count
from bili_search import search

__all__ = ["on_message"]

MAX_FAV_PER_MSG = 1      # 一条消息最多收一条：这是产品规矩，不跟搜索结果多少走
MIN_KW_LEN = 2           # 单字主语（"猫"）太宽泛，搜出来全是无关的高播放，宁可不收
MAX_KW_LEN = 20
MAX_TEXT_LEN = 100       # 超长消息多半是转发的长文，不像在求推荐

REASON_PREFIX = "聊天驱动"   # 写进 bili_fav 日志的理由前缀，事后能按来源筛出来

# ---- 信号：这句话是不是在"想要视频" ----------------------------------------

# 平台名：提到 B 站，但还得带个"想要 / 在问"的语气才算数
PLATFORM_PAT = re.compile(r"(?:b\s*站|bilibili|哔哩哔哩|哔哩|小破站)", re.I)

# 视频载体本身。单独出现不够（"这个教程讲得真好"是闲聊），要配下面的语气词
VIDEO_PAT = re.compile(
    r"(?:视频|短片|教程|课程|讲解|测评|开箱|合集|录播|vlog|up\s*主|番剧)", re.I)

# 想要 / 求推荐
WANT_PAT = re.compile(
    r"(?:有没有|有木有|求推荐|求个|求|推荐|安利|想看|想找|想学|想要|找个|"
    r"来一个|来个|来点|来几个|整一个|给个|发个|有吗|有么|"
    r"搜一下|搜下|找一下|找找|帮我找|带我看)")

# 在问 / 想知道。和平台名一起用（"B站上怎么装机"）
ASK_PAT = re.compile(
    r"(?:怎么|如何|怎样|哪种|哪个|哪些|哪里|什么|啥|吗|嘛|呢|能不能|可不可以|可以)")

# 学习类提问：B 站教程是最自然的答案，不看有没有"视频"两个字
LEARN_PAT = re.compile(
    r"(?:怎么学|如何学|怎样学|怎么入门|如何入门|怎么上手|想学|想自学|想入门|"
    r"零基础|学习路线|自学|从哪学)")

# "视频"两个字一样，意思完全不是"想找个视频看"的说法，先放过
NEGATIVE_PAT = re.compile(
    r"(?:视频通话|视频电话|视频聊天|打视频|视频号|不要视频|别发视频|别推荐|不用推)")

# ---- 抽词 ----------------------------------------------------------------

# 直接当分隔符切掉的噪音：请求语气、平台名、视频载体、B 站站内动作、量词、助词、标点。
# 长的必须写在短的前面（正则取最左匹配，同位置取先列出的），
# 否则「有没有」会被「求」之类的短词先切碎。
NOISE_RE = re.compile(
    r"b\s*站|bilibili|哔哩哔哩|哔哩|小破站"
    r"|有没有|有木有|能不能|可不可以|可以|帮我找|帮我|帮忙|给我|麻烦|请问"
    r"|求推荐|求个|求|推荐|安利|想看|想找|想学|想要|找个|来一个|来个|来点|来几个|整一个"
    r"|发个|给个|搜一下|搜下|找一下|找找|带我看|一下|有吗|有么"
    r"|我想学|我想|我要|怎么学|如何学|怎样学|怎么|如何|怎样"
    r"|教程|视频|短片|课程|讲解|测评|开箱|合集|录播|vlog|up\s*主|番剧"
    r"|投币|充电|大会员|会员|弹幕|直播间|直播|私信|三连|收藏|点赞|关注"
    r"|注册|登录|注销|封号|审核|下架|客服"
    r"|什么|啥|哪种|哪个|哪些|哪里|相关|之类|方面"
    r"|最近|现在|平时|有空"
    r"|讲讲|讲|聊聊|聊|说说|介绍|科普"
    r"|吗|呢|吧|啊|呀|嘛|哦|呗|的|了|着|过"
    r"|个|条|部|集|篇|支"
    r"|[\s，。！？、；：,.!?;:()（）\[\]【】\"'“”‘’~～\-—_/\\|*]+",
    re.I)

# 碎片两端还挂着的虚词/量词，切完再削一遍
EDGE_CHARS = " \t　的了吗呢吧啊呀嘛哦呗个条部集篇支点些"

# 以这些字打头的碎片，多半是把半句话当成了关键词（"都不行"、"打不开"）
BAD_START = "不没别都也就还又才太很挺真总老最更该怎那这哪"

# 这些"词"搜出来必然是垃圾（描述心情/状态，不是主题），宁可 skip
BAD_KEYWORDS = frozenset({
    "好玩", "好看", "有意思", "有趣", "精彩", "厉害", "东西", "玩意", "啥的",
    "为什么", "为啥", "咋办", "怎么办", "打不开", "进不去", "登不上", "崩了",
    "卡了", "一样", "时候", "最近", "现在", "感觉", "觉得", "好像", "知道",
    "需要", "想要", "推荐", "视频", "教程", "一下", "一点", "有吗", "在吗",
    "哈哈", "谢谢", "多谢", "感谢", "拜托", "在线等",
})

# 主语一般不会以人称代词开头；"我朋友想学吉他"里的"我朋友"要输给"吉他"。
# 只是排序偏好，不是硬拒绝：整句只剩"我的世界"时仍然会选它。
PRONOUN_START = "我你他她咱俺您"


def detect_signal(text):
    """这句话是不是在"想要视频"。命中返回信号名，没命中返回 ""。

    分层的顺序是刻意的：
      1. 先排掉"视频通话"这类同名不同意的说法
      2. 提到 B 站，得带"想要 / 在问"的语气（"B站又崩了"不算）
      3. 提到视频/教程，得带"想要 / 求推荐"（"这个教程不错"不算）
      4. 纯学习类提问（"怎么学吉他"）也算——B 站教程是最自然的答案

    纯函数，零网络请求。聊天里九成的话在这一步就结束了。
    """
    t = (text or "").strip()
    if not t:
        return ""
    if NEGATIVE_PAT.search(t):
        return ""
    intent = WANT_PAT.search(t) or LEARN_PAT.search(t)
    if PLATFORM_PAT.search(t) and (intent or ASK_PAT.search(t)):
        return "platform"
    if VIDEO_PAT.search(t) and intent:
        return "video"
    if LEARN_PAT.search(t) and len(t) >= 4:
        return "learn"
    return ""


def _valid_keyword(kw):
    """这个词能不能拿去搜。挡掉太短、太长、以虚词打头、整段是泛词的碎片。"""
    if not kw or len(kw) < MIN_KW_LEN or len(kw) > MAX_KW_LEN:
        return False
    if kw in BAD_KEYWORDS or kw[0] in BAD_START:
        return False
    # 至少得有一个实字：纯标点/空白没有搜索价值
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9]", kw))


def extract_keyword(text):
    """从一句话里抽"要搜什么"。抽不出返回 ""（调用方就 skip）。

    做法是"切 + 捡"：把请求语气、平台名、视频载体全当分隔符切掉，
    再从剩下的碎片里挑一个。挑的偏好（这个顺序有讲究）：
      1. 不以人称代词打头的优先（"我朋友想学吉他" -> 吉他，不是"我朋友"）
      2. 其次挑最长的（实词通常是句子里最长的碎片）
      3. 一样长取靠后的：中文名词短语中心语在后（"适合新手的机械键盘"）

    规则不追求完美——抽歪了最多搜到一条不相关的视频，抽不出就 skip，
    后者比瞎猜安全。
    """
    t = (text or "").strip()
    if not t:
        return ""

    picked, picked_key = "", None
    for seg in NOISE_RE.split(t):
        kw = (seg or "").strip().strip(EDGE_CHARS).strip()
        if not _valid_keyword(kw):
            continue
        key = (kw[0] not in PRONOUN_START, len(kw))
        if picked_key is None or key >= picked_key:
            picked, picked_key = kw, key
    return picked[:MAX_KW_LEN]


def pick_best(results):
    """播放量最高的一条，返回 (视频, 播放数)；没有可用的返回 (None, 0)。

    0 播放的一律不要：搜索结果里 0 播放基本是刚上传或完全不相关的内容，
    收进收藏夹只会让"AI收藏"变成垃圾堆。
    """
    best, best_play = None, 0
    for v in results or []:
        if not isinstance(v, dict) or not v.get("bvid"):
            continue
        try:
            play = int(v.get("play") or 0)
        except (TypeError, ValueError):
            play = 0
        if play <= 0:
            continue
        if play > best_play:
            best, best_play = v, play
    return best, best_play


def _skip(reason, **extra):
    out = {"ok": False, "action": "skip", "reason": reason}
    out.update(extra)
    return out


def _error(reason, **extra):
    out = {"ok": False, "action": "error", "reason": reason}
    out.update(extra)
    return out


def _run(text, wxid, dry):
    """真正的流程，抛异常由 on_message 兜底。"""
    t = (text or "").strip()
    if not t:
        return _skip("empty_text")
    if len(t) > MAX_TEXT_LEN:
        return _skip("too_long")

    signal = detect_signal(t)
    if not signal:
        return _skip("no_signal")

    kw = extract_keyword(t)
    if not kw:
        return _skip("no_keyword", signal=signal)

    # 先看额度再搜：满额时搜了也收不进去，白花一次请求。
    # 顺带避免 add 在满额时返回 0（它内部 break 掉，rc 仍是 0）被当成收藏成功。
    used = today_count()
    if used >= DAILY_CAP:
        return _skip("daily_cap", keyword=kw, signal=signal, fav_today=used)

    try:
        results = list(search(kw) or [])
    except Exception as exc:
        return _error(f"search:{type(exc).__name__}: {exc}", keyword=kw, signal=signal)

    best, play = pick_best(results)
    if not best:
        # 空结果和"全是 0 播放"走同一条路：都不收
        return _skip("no_result", keyword=kw, signal=signal, result_count=len(results))

    fav_reason = f"{REASON_PREFIX}:{wxid or 'unknown'}:{kw}"
    out = {
        "keyword": kw,
        "signal": signal,
        "bvid": best.get("bvid", ""),
        "title": best.get("title", ""),
        "author": best.get("author", ""),
        "play": play,
        "fav_reason": fav_reason,
    }
    if dry:
        # 演练也真搜：不然看不出"真跑会收哪一条"。
        # 但绝不调 add，把"将要做的事"原样返回。
        out.update({"ok": True, "action": "fav", "reason": "dry_run", "dry": True})
        return out

    bvids = [out["bvid"]][:MAX_FAV_PER_MSG]
    ctx = {k: out[k] for k in ("keyword", "signal", "bvid", "title")}
    try:
        rc = add(bvids, reason=fav_reason)
    except SystemExit as exc:
        # bili_fav 没登录时用 SystemExit 报错，它不是 Exception 的子类
        return _error(f"no_login:{exc}", **ctx)
    except Exception as exc:
        return _error(f"fav:{type(exc).__name__}: {exc}", **ctx)

    if rc:
        # add 只在真出错/查不到 aid 时返回非 0；重复收藏会安静跳过并返回 0
        return _error(f"fav_failed:rc={rc}", **ctx)

    # fav_today 是动手之前今天已收的条数（含别的模块收的）
    out.update({"ok": True, "action": "fav", "reason": f"faved:{kw}",
                "dry": False, "fav_today": used})
    return out


def on_message(text, wxid, dry=False):
    """收到一条微信消息时调用，返回 dict，**绝对不抛异常**。

    dry=True 时只搜索、返回"将要收藏哪一条"，不调 add——手动试跑和
    新机器第一天上岗时用它，免得先把用户账号弄脏。

    reason 是状态码（no_signal / no_keyword / no_result / daily_cap /
    dry_run / faved:<关键词> / search:... / fav:...），
    真正写进 bili_fav 日志的理由在 fav_reason 里，形如
    "聊天驱动:wxid_xxx:机械键盘"。

    命中的结果还会带：keyword / signal / bvid / title / author / play /
    fav_reason（收藏时会带 fav_today=动手前今天已收几条）。
    """
    try:
        return _run(text, wxid, dry)
    except SystemExit as exc:
        # 收藏模块还没登录时走这里：让它活着返回，别把调用线程带走
        return _error(f"no_login:{exc}")
    except Exception as exc:
        return _error(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    # 手动试：默认演练（只搜不收），确认没问题再加 --yes 真收一条。
    #   python bili_chat.py "有没有机械键盘的视频"
    #   python bili_chat.py "有没有机械键盘的视频" --yes
    import json
    import sys

    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    msg = argv[0] if argv else "有没有机械键盘的视频"
    print(json.dumps(on_message(msg, "cli", dry="--yes" not in sys.argv),
                     ensure_ascii=False, indent=2))
