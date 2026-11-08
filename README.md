# wechat-llm-bridge

把微信 PC 端接上大模型：用 Windows UI Automation 读独立聊天窗口，交给模型生成回复，
再走同一条 UIA 路径发出去。附带 B 站订阅/收藏、长期记忆、Obsidian 日记、桌面控制台和运行统计。

一句话：**不用破解协议、不注入客户端，让机器人像人一样在微信里收发消息。**

不注入微信进程，不改客户端，也不依赖 wxauto —— 控件树是自己读的。

> **只支持微信 4.1.8.x。** 从 4.1.15 起，微信移除了窗口的辅助功能树（主窗口只暴露
> 一个空的 Pane，MSAA `accChildCount=0`，内容画在自绘 surface 上），本项目的前提就不存在了。
> 升级微信 = 项目直接失效，这一点没有绕过去的办法。

---

## 它能做什么

| 模块 | 干什么 |
|---|---|
| 桥（`bridge.py`） | HTTP 服务 + 规则判断 + 提示词渲染 + 模型调用 + 提醒/主动搭话的编排 |
| 监听器（`watch_uia.py`） | 轮询若干个「独立聊天窗口」，攒够一句就 POST 给桥 |
| 发送（`outbox.py`） | **唯一出口**：额度、失败计数、自己的发送记账、原子写都在里面 |
| B 站（`bili_*.py`） | 关键词订阅、收藏夹写入（每日额度）、评论区抓取、摘要 |
| 记忆（`memory.py`） | 记忆原子 + 召回注入 + TTL 衰减 + 来源追溯，存 `data/memory.db` |
| 日记（`vault.py`） | 写进 Obsidian 库：每日日记、收藏说明、收件箱 |
| 桌面版（`gui.py`） | PySide6 控制台：概览/订阅/收藏/语料/统计/记忆/星云/设置 |

## 为什么是「独立聊天窗口」

微信主窗口的控件树在 4.1.8 上也不完整，但**双击会话弹出来的独立窗口**（`mmui::ChatSingleWindow`）
是完整的：消息列表、输入框、发送按钮都在。所以用法是：先把要监听的会话都双击弹出来，
再启动监听器。

## 快速开始

```powershell
# 1. 依赖
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install PySide6 uiautomation requests

# 2. 配置：复制样例，填自己的
Copy-Item config.example.json config.json
Copy-Item prompt.example.txt prompt.txt
#   config.json 里至少要填：api_key（或用环境变量 DEEPSEEK_API_KEY）、
#   watch_targets / rooms、my_name、persona

# 3. 先跑测试（不碰微信、不调真模型）
Get-ChildItem tests\test_*.py | ForEach-Object { .\.venv\Scripts\python.exe $_.FullName }

# 4. 开界面 / 只起桥 / 只起监听
.\.venv\Scripts\python.exe app.py
.\.venv\Scripts\python.exe app.py --bridge
.\.venv\Scripts\python.exe app.py --watch
```

数据根：开发时是仓库目录，打包后是 exe 所在目录（`app.py` 会把 `WXBRIDGE_ROOT` 指过去）。
所有模块都写 `_HERE = Path(os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent)`——
**新增模块必须照抄这一行**，否则打包后会静默读不到 `data/`。

## 配置要点

```jsonc
{
  "backend": "api",           // api = 真模型；stub = 固定回复，调试用
  "api_model": "deepseek-chat",
  "api_key": "",              // 或环境变量 DEEPSEEK_API_KEY
  "api_token": "",            // 本机接口鉴权，首次启动会生成；不设的话局域网/浏览器都能 POST
  "my_name": "我",
  "persona": "...",           // 写进提示词的自我介绍
  "watch_targets": ["文件传输助手"],
  "rooms": {"某个群": {"require_at": true}},
  "rate_limit": {"min_interval_s": 3, "max_per_minute": 6},
  "vision": {"enabled": false},
  "memory": {"enabled": true, "every_turns": 8}
}
```

`config.example.json` 里有全部字段的默认值。

## 目录

```
bridge.py        桥：规则 / 提示词 / 模型 / 提醒 / 主动搭话
watch_uia.py     监听器：多会话轮询、防抖、拆条、发送、发送后关屏
wxuia.py         UIA 层：控件树、发送、发图、节流、跨进程锁
outbox.py        唯一发送出口
backends.py      api / stub 两种后端；Context 是提示词渲染的入参
memory.py        长期记忆；vault.py Obsidian；stats.py 统计；charts.py 手画图表
proactive.py     主动搭话（判断版）；remind.py 提醒；schedule.py 作息/课表
bili_*.py        B 站：搜索 / 收藏 / 订阅 / 评论语料 / 登录 / wbi 签名
dispstate.py     读显示器开关（电源通知），核对关屏指令真生效
pause.py         区分「系统睡了」和「自己卡了」
imgfmt.py        发图前格式兜底（webp/heic → PNG）
gui.py           桌面控制台
tests/           23 个自跑脚本（没有 pytest）
```

## 测试

没有 pytest。每个 `tests/test_*.py` 是自跑脚本，`check(name, cond, detail)` 累加
PASS/FAIL，`main()` 返回非零表示有用例挂了。

```powershell
Get-ChildItem tests\test_*.py | ForEach-Object { .\.venv\Scripts\python.exe $_.FullName; if($LASTEXITCODE){ "挂了: $($_.Name)" } }
```

不碰微信、不调真模型（`backend=stub` 或注入假实现），临时目录用 `tempfile.mkdtemp()`。

## 已知限制

- **微信版本锁死 4.1.8.x**，见开头。
- 消息方向靠「自己的发送记录 + 文本回声」判断，手机上主动发的消息可能被误当成对方发的。
- 游标是按窗口标题存的，两个同名备注的会话会共享状态。
- 发送依赖前台窗口和全局键盘/剪贴板，被别的窗口抢焦点时会失败（会记日志，不会重发）。
- B 站评论接口有风控，一次最多 5 页、间隔 ≥1 秒，连打会 `-412`。

## 免责声明

- 与腾讯 / 微信官方**没有任何关系**，未获授权。
- 用自动化操作微信**违反其用户协议**，可能导致账号被限制或封禁，风险自负。
  **建议只用小号做实验，不要在主号上用。**
- 仓库里**不含任何账号数据**：没有聊天记录、没有 cookie、没有 API key。
  你自己运行时产生的 `data/`、`config.json`、日志都在 `.gitignore` 里。
- 仅用于个人学习和自用场景。请自行确认所在地法律与你所用服务的条款。

## 致谢

- 提示词与钩子的设计参考了 [AstrBot](https://github.com/AstrBotDevs/AstrBot)（AGPL-3.0）。
  **只借鉴设计，没有搬运它的代码**，所以本仓库不继承 AGPL；如果你要往里放 AGPL 代码，
  请把整个仓库改成 AGPL。
- `docs/抖音桥接-学习笔记.md` 来自对 [pcmxiao/dsh-douyin-bridge](https://github.com/pcmxiao/dsh-douyin-bridge)（MIT）的阅读。

## License

MIT，见 [LICENSE](LICENSE)。
