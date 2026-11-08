"""后端：把一条聊天消息变成一句回复。

两种实现，接口一致，换后端只改 config.json 的 "backend" 字段。
"""
import os
import subprocess
from pathlib import Path

# 打包后 __file__ 在 _internal 里，config.json 和 data/ 都在 exe 旁边。
# 和 bridge.py 用同一套根目录。
_HERE = Path(os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()


class BackendError(RuntimeError):
    pass


DEFAULT_PROMPT = """你是一个微信代回复助手。你的任务是根据聊天记录，以账号用户的口吻，写一条可以直接发给对方的回复。

要求：
1. 只输出回复内容本身：不要引号、不要前缀、不要任何解释。
2. 口语化，一到两句，像日常打字；不用列表、不用 markdown。
3. 不要向用户索要信息，不要描述你在做什么，不要自称 AI 或助手。
4. 对方在说正事、或者情绪不好的时候，不要卖萌，认真回。

说话风格：{{persona}}

本条消息的表情要求：{{emoji}}
如果要带，从这里挑一个最贴合当下心情的（最多一个，不要重复同一段对话里用过的）：
- 平静：ᗜ - ᗜ
- 开心：ᗜ ᴗ ᗜ / ᗜ v ᗜ
- 心里暗爽但不外露：ᗜ ⁻̫ ᗜ（也可以只放在心里，不写出来）
- 疑惑：ᗜ ο ᗜ
- 无语：ᗜ ε ᗜ / ᗜ～ᗜ
- 生气但忍着：ᗜ ⌓ ᗜ
- emoji 一次最多一个：💤 ❗ 💧 ✨

聊天记录（最后一条是本次需要回复的消息）：
{{history}}
对方：{{text}}

请输出这条回复："""


def load_template(root=None):
    """每次调用都重新读 prompt.txt，改完立刻生效，不用重启。

    占位符：
      {{persona}}  config.json 里的 persona
      {{history}}  之前的对话，每行「对方：…」或「我：…」
      {{text}}     对方刚发来的这句
      {{name}}     对方昵称
    """
    path = Path(root or ".") / "prompt.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return DEFAULT_PROMPT


# 只放**基本不会出现在正文里**的颜文字字符。
# 曾经把 × ～ ^ ・ 益 皿 凸 也塞进来，结果「27×3=81」被删成「273=81」、
# 「3～5天」变成「35天」—— 关了表情功能（kaomoji=false）时每条回复都过这一遍，
# 等于静默改写要发出去的话。删字表时只删字符，不删片段，所以宁可保守。
KAOMOJI_CHARS = "ᗜᴗ⁻̫ωο꒫ε⌓Д⌑▽∀⌒‿◡ᵕᵔಠ￣▔"


def strip_kaomoji(text):
    """把颜文字和 emoji 从回复里清掉（关了表情功能时用）。

    只删特殊字符，不动正文标点，避免把正常文字弄坏。
    """
    import re

    if not text:
        return text
    out = text
    for ch in KAOMOJI_CHARS:
        out = out.replace(ch, "")
    # 剩下的常见 emoji 区段
    out = re.sub(
        r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]",
        "",
        out,
    )
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def first_line(stdout, max_lines=8, max_chars=600):
    """清理模型输出，但**保留多行**（数学/解释类回答会被截断）。

    只做三件事：去掉「我：」「回复：」这种前缀、去掉包裹引号、
    最多保留 max_lines 行（默认 8）且总长不超过 max_chars（默认 600）。

    2026-10-22：上限从 6 行 / 400 字放宽到 8 行 / 600 字。
    原来是拍脑袋定的，实测会把第 7 行起的结论整行丢掉（「…27×3=81」后面没有 2187）。
    """
    lines = [ln.rstrip() for ln in (stdout or "").splitlines()]
    # 去掉开头结尾的空行
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""

    text = "\n".join(lines)
    for prefix in ("我：", "我:", "回复：", "回复:"):
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip()
            break
    text = text.strip()

    # 模型有时会整段加引号
    if len(text) > 1 and text[0] in "\"“'" and text[-1] in "\"”'":
        text = text[1:-1].strip()

    kept = text.splitlines()[:max_lines]
    out = "\n".join(kept).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rstrip() + "…"
    return out


class Context:
    """一次回复请求需要的全部信息。"""

    def now_text(self):
        """当下的时间。每次调用都重算，保证「现在几点」是真的。"""
        from datetime import datetime

        now = datetime.now()
        week = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
        return f"{now:%Y-%m-%d %H:%M} {week}"

    def __init__(self, wxid, name, text, history, persona, at_me=False, template=None,
                 emoji_ok=False, images_text="", images=None, me_name="", timetable="",
                 search_text="", remind_text="", memory_text=""):
        self.wxid = wxid
        self.name = name
        self.text = text
        self.history = history  # [(role, text), ...] role 为 "对方" / "我"
        self.persona = persona
        self.at_me = at_me
        self.template = template
        self.emoji_ok = emoji_ok
        self.images_text = images_text
        self.images = images or []  # 路径或 bytes，随请求一起发给模型
        self.me_name = me_name      # 我自己的微信昵称
        self.timetable = timetable  # 作息时间表纯文本
        self.search_text = search_text  # 联网检索结果（已包成"资料"块）
        self.remind_text = remind_text  # 系统记下的提醒（程序算的，不是模型编的）
        self.memory_text = memory_text  # 长期记忆（已包成"你记得的事"块，可能为空）

    def prompt(self):
        """按 prompt.txt 渲染提示词；文件不存在时用内置默认模板。

        模板每次请求都会重新读入，所以你改完 prompt.txt 直接生效，不用重启桥。
        """
        tpl = self.template or DEFAULT_PROMPT
        history = "\n".join(f"{role}：{text}" for role, text in self.history)
        emoji = (
            "这一条要带一个表情：从下面的清单里挑最贴合当下心情的一个，放在句子里（最多一个）"
            if self.emoji_ok
            else "这一条不要带任何表情符号、颜文字或 emoji"
        )
        out = (
            tpl.replace("{{persona}}", self.persona or "")
            .replace("{{history}}", history)
            .replace("{{name}}", self.name or self.wxid)
            .replace("{{emoji}}", emoji)
            .replace("{{me}}", self.me_name or "我")
            .replace("{{now}}", self.now_text())
            .replace("{{timetable}}", self.timetable or "（暂无）")
            .replace("{{images}}", self.images_text or "（暂无图片）")
            .replace("{{search}}", self.search_text or "（这次没有联网检索）")
            .replace("{{remind}}", self.remind_text or "（这一轮系统没有记下提醒）")
            .replace("{{memory}}", self.memory_text or "")
            .replace("{{text}}", self.text)
        )
        # history 为空时模板里会留下连续空行，收一下
        while "\n\n\n" in out:
            out = out.replace("\n\n\n", "\n\n")
        return out.strip()


class StubBackend:
    """离线后端，用来验证链路和规则，不花钱、不联网。"""

    name = "stub"

    def reply(self, ctx):
        head = f"[stub] 收到「{ctx.text}」"
        if ctx.text.startswith("回顾"):
            return f"{head}｜历史上文 {len(ctx.history)} 条"
        return head + "，链路是通的。"


class DshBackend:
    """调本机的 dsh headless，一个联系人一次独立会话。

    注意：headless 每次都要冷启动一个 agent，几秒到几十秒，聊天场景偏慢；
    真上线建议换成常驻进程直连模型 API。
    """

    name = "dsh"

    def __init__(self, cli=None, patch=None, timeout=120):
        self.cli = cli or os.environ.get("DSH_CLI") or r"E:\DSH\resources\runtime\cli\bin\dsh.cmd"
        self.patch = patch
        self.timeout = timeout

    def reply(self, ctx):
        cmd = [self.cli, "headless"]
        if self.patch:
            cmd += ["--patch", self.patch]
        try:
            # 提示词走 stdin：多行文本经 argv 传会被截断/转义搞坏，
            # 而 dsh headless 不带任务参数时就是从 stdin 读。
            proc = subprocess.run(
                cmd,
                input=ctx.prompt(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise BackendError(f"timeout after {self.timeout}s")
        out = first_line(proc.stdout)
        if proc.returncode != 0 and not out:
            err = (proc.stderr or "").strip().splitlines()
            raise BackendError(err[-1] if err else f"exit {proc.returncode}")
        if not out:
            raise BackendError("empty stdout")
        return out


class ApiBackend:
    """直连 DeepSeek API（而不是起一个 dsh headless agent）。

    好处：
      1. 快 —— 省掉冷启动 agent，通常 1-2 秒
      2. 能看图 —— 可以把图片作为 image_url 塞进消息里
    """

    name = "api"

    def __init__(self, api_key=None, model="deepseek-chat",
                 base_url="https://api.deepseek.com", timeout=120,
                 vision_model=""):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or ""
        self.model = model
        # 带图请求要换一个能收图的模型：
        # deepseek-reasoner 收到图要么返回 HTTP 400(unsupported image)，
        # 要么思考吃满 max_tokens 后正文为空，两种都等于识图失败。
        self.vision_model = vision_model or "deepseek-chat"
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.last_usage = {}   # 最近一次调用的 token 用量（缓存命中/未命中/输出）

    def reply(self, ctx):
        import json as _json
        import urllib.error
        import urllib.request

        if not self.api_key:
            raise BackendError("没有 DEEPSEEK_API_KEY")

        content = [{"type": "text", "text": ctx.prompt()}]
        for img in (ctx.images or []):
            content.append({
                "type": "image_url",
                "image_url": {"url": image_to_data_uri(img)},
            })

        # 带图就换成能做视觉的模型
        model = self.model
        if ctx.images and "reasoner" in model:
            model = self.vision_model

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
        }
        # 思考模型（deepseek-reasoner）不接受 temperature，而且思考过程也占 token，
        # 所以给它的上限要放宽；普通 chat 模型保持原样。
        if "reasoner" in model:
            payload["max_tokens"] = 2400
        elif ctx.images:
            # 看图要说清楚画面，200 token 经常写到一半被截断，
            # 截断的半句话会被当成正常回复发出去。
            payload["temperature"] = 1.0
            payload["max_tokens"] = 1000
        else:
            payload["temperature"] = 1.0
            payload["max_tokens"] = 200

        body = self._post(payload)
        self.last_usage = body.get("usage") or {}

        try:
            choice = body["choices"][0]
            out = first_line(choice["message"].get("content") or "")
            finish = choice.get("finish_reason")
        except (KeyError, IndexError):
            raise BackendError(f"返回格式异常: {str(body)[:200]}")

        # 思考把 token 用光时会返回空正文（finish=length）——加预算重试一次
        if not out and finish == "length" and "reasoner" in model:
            payload["max_tokens"] = 4000
            body = self._post(payload)
            self.last_usage = body.get("usage") or {}
            try:
                choice = body["choices"][0]
                out = first_line(choice["message"].get("content") or "")
                finish = choice.get("finish_reason")
            except (KeyError, IndexError):
                pass

        if not out:
            usage = body.get("usage") or {}
            detail = usage.get("completion_tokens_details") or {}
            raise BackendError(
                f"空回复(finish={finish}, 思考token={detail.get('reasoning_tokens')})"
            )
        return out

    def chat(self, prompt, model=None, temperature=0.8, max_tokens=400, json_mode=False):
        """一次性纯文本调用，不经过 prompt.txt。

        给「判断要不要主动开口」这类内部任务用：它们的提示词是现拼的，
        不该被 reply() 里那套历史/表情/占位符逻辑搅进去。
        """
        return self.chat_think(prompt, model=model, temperature=temperature,
                               max_tokens=max_tokens, json_mode=json_mode)[0]

    def chat_think(self, prompt, model=None, temperature=0.8, max_tokens=400,
                   json_mode=False, think_budget=6000):
        """和 chat() 一样，但把思考也带回来：(正文, 思考)。

        deepseek-reasoner 把思考放在 message.reasoning_content 里，正文才有
        message.content；别的模型没有这个字段，思考返回空串。
        """
        model = model or self.model
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        if "reasoner" in model:
            # 思考模型不吃 temperature、也不支持 json_object；思考本身还要占预算
            payload["max_tokens"] = max(max_tokens, 2400)
        else:
            payload["temperature"] = temperature
            if json_mode:
                payload["response_format"] = {"type": "json_object"}

        body = self._post(payload)
        self.last_usage = body.get("usage") or {}
        try:
            choice = body["choices"][0]
        except (KeyError, IndexError):
            raise BackendError(f"返回格式异常: {str(body)[:200]}")
        msg = choice.get("message") or {}
        out = msg.get("content") or ""
        think = msg.get("reasoning_content") or ""

        # 思考把 token 吃光时会返回空正文，加预算再来一次
        if not out and choice.get("finish_reason") == "length" and "reasoner" in model:
            payload["max_tokens"] = think_budget
            body = self._post(payload)
            self.last_usage = body.get("usage") or {}
            try:
                msg = body["choices"][0]["message"] or {}
                out = msg.get("content") or ""
                think = msg.get("reasoning_content") or think
            except (KeyError, IndexError):
                out = ""
        return out, think

    def _post(self, payload):
        import json as _json
        import urllib.error
        import urllib.request

        data = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return _json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise BackendError(f"HTTP {exc.code}: {detail}")
        except urllib.error.URLError as exc:
            raise BackendError(f"连接失败: {exc}")


def balance(api_key=None, base_url="https://api.deepseek.com", timeout=10, fetch=None):
    """查 DeepSeek 余额。

    返回 `{ok, total, currency, granted, topped_up, error, at}`：`ok=False` 时
    看 `error`，别把失败当成余额 0 —— 界面上一个是「没钱了」，一个是「查不到」。

    没传 api_key 就按 config 的顺序找：环境变量 → 项目根目录的 config.json。
    """
    import json as _json
    import time as _time
    import urllib.error
    import urllib.request

    key = api_key or os.environ.get("DEEPSEEK_API_KEY") or ""
    if not key:
        try:
            cfg_path = Path(_HERE) / "config.json"
            cfg = _json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            key = cfg.get("api_key") or ""
            base_url = cfg.get("api_base") or base_url
        except Exception:
            pass

    out = {"ok": False, "total": "", "currency": "", "granted": "", "topped_up": "",
           "error": "", "at": int(_time.time())}
    if not key:
        out["error"] = "没有 api_key（config.json 或 DEEPSEEK_API_KEY）"
        return out

    url = base_url.rstrip("/") + "/user/balance"
    try:
        if fetch is not None:
            body = fetch(key, url)
        else:
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {key}", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:160]
        out["error"] = f"HTTP {exc.code}: {detail}"
        return out
    except urllib.error.URLError as exc:
        out["error"] = f"连接失败: {exc}"
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    infos = (body or {}).get("balance_infos") or []
    if not infos:
        out["error"] = "接口没返回 balance_infos"
        return out
    info = infos[0]
    out.update({
        "ok": True,
        "total": info.get("total_balance") or "0",
        "currency": info.get("currency") or "CNY",
        "granted": info.get("granted_balance") or "",
        "topped_up": info.get("topped_up_balance") or "",
    })
    return out


def image_to_data_uri(img):
    """把图片（路径或 bytes）转成 data URI。"""
    import base64

    if isinstance(img, (bytes, bytearray)):
        raw = bytes(img)
    else:
        with open(img, "rb") as fh:
            raw = fh.read()
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def build(config, root):
    kind = (config.get("backend") or "stub").lower()
    patch = config.get("dsh_patch")
    if patch and not os.path.isabs(patch):
        patch = str((root / patch).resolve())  # 相对路径按项目目录解析，不依赖 cwd
    if kind == "stub":
        return StubBackend()
    if kind == "api":
        return ApiBackend(
            api_key=config.get("api_key") or os.environ.get("DEEPSEEK_API_KEY"),
            model=config.get("api_model", "deepseek-chat"),
            vision_model=config.get("api_model_vision", "deepseek-chat"),
            base_url=config.get("api_base", "https://api.deepseek.com"),
            timeout=config.get("reply_timeout_s", 120),
        )
    if kind == "dsh":
        return DshBackend(
            cli=config.get("dsh_cli"),
            patch=patch,
            timeout=config.get("reply_timeout_s", 120),
        )
    raise BackendError(f"unknown backend: {kind}")
