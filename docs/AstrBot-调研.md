# AstrBot / LivingMemory 调研：能学什么，不能拿什么

> 2026-10-28。这份笔记是「借设计」的依据，不是引入依赖的决定。
> 结论先写：**可以读、可以照着重写，不能把代码搬进来。**

## 一、许可（用 GitHub license API 查的，不是看 README 猜的）

| 仓库 | License | spdx |
|---|---|---|
| `AstrBotDevs/AstrBot` | GNU Affero General Public License v3.0 | `AGPL-3.0` |
| `lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory` | GNU Affero General Public License v3.0 | `AGPL-3.0` |

**这决定了边界。** AGPL-3.0 有传染性：把它们的代码并进 `本项目`，`本项目`
整体就得按 AGPL 发布；而且 AGPL 第 13 条对**网络服务**也要求向使用者提供源码。
所以：

- ✅ 可以读源码、抄思路、抄数据结构、抄交互设计（思想不受版权保护）
- ❌ 不能复制文件、函数体、CSS、HTML 模板进本仓库或打包产物

`tmp/dash/` 那份是上一轮为了「看一眼长什么样」而下载的 dashboard（36 个文件
1.38 MB，AGPL 代码），**只作本地参考**：`tmp/` 已在 `.gitignore` 里，不进版本库、
不进 exe，看完就该删。

## 二、它的钩子 → 我们的对应物

AstrBot 的插件靠钩子插进对话流程。我们只有两个介入点，但**这两个点是我们自己
的代码**，不用求人：

| AstrBot | 作用 | 本项目 对应 |
|---|---|---|
| `on_llm_request` | 请求前注入（LivingMemory 往 `extra_user_content` 塞记忆） | `hooks` 的 `before_reply`，返回值合并进 `backends.Context` |
| `on_llm_response` | 回复后写回（写助手消息、攒够轮次触发总结） | `hooks` 的 `after_reply`，只读，不参与回复 |

差别：AstrBot 的钩子是**插件发现 + 事件总线**（装了就自动生效）；我们的是一张
固定两阶段的注册表，注册在代码里显式写一行。刻意不做动态加载——本项目没有
「用户装插件」的需求，多一层反而难查。

跑完的结果：`bridge.handle()` 里不再出现 `memory.xxx()`，只有
`hooks.collect("before_reply", ...)` 和 `hooks.run("after_reply", ...)`。

## 三、它的插件视图协议 → 我们不采用

AstrBot 让插件在前端出页面，做法是：

- 视图放 `views/<view_name>/index.html`（兼容旧目录名 `pages/`）；
- AstrBot 把它塞进一个**受限 iframe**，并自动注入 `/api/plugin/page/bridge-sdk.js`；
- 页面通过 `window.AstrBotPluginView`（别名 `AstrBotPluginPage`）通信：
  `ready()`、`getContext()`、`apiGet(endpoint, params)`、`apiPost(endpoint, body)`；
- 前端只写插件内相对路径（`apiGet("items/123")`），Dashboard 转发到
  `/api/v1/plugins/extensions/<plugin_name>/items/123`；
- 后端用 `context.register_web_api(route, handler, methods, desc)` 注册，
  handler 里用 `astrbot.api.web.request` / `json_response` / `error_response`，
  还支持 SSE（`stream_response`）。

**我们不要这层。** 原因是**同源**：我们的页面就是 `bridge` 自己 served 的
`/`，`fetch('/api/memory')` 直达，不需要 iframe 桥、不需要 endpoint 前缀重写、
不需要 SDK。加一层 bridge 只会让调试多一跳。

值得抄的是它暴露的**能力清单**，而不是机制：

- 单条记忆的**详情面板**（LivingMemory 的 `peek-panel`）→ 我们做了 `#mem_detail`；
- **来源消息**可追溯（`memory_sources` 表）→ 我们做了 `atom_sources`；
- 召回结果的**分项得分**（它分四条检索路线，我们只有字面一路）→ 我们给了
  「卡在哪道门槛」。

## 四、LivingMemory 的表结构：借了哪几列

借：

| 它的列 | 我们的做法 |
|---|---|
| `memory_sources` | `atom_sources(atom_id, msg_id)`，指向 `chat.db` 的 `messages.id` |
| `ttl_days` / `expires_at` | 按 `kind` 给 TTL，重要度放大（`plan` 2 天、`event` 7 天……） |
| `status`（active/expired/forgotten…） | 同名字段，软删用 `forgotten` |
| `reinforcement_count` | 我们的 `uses` + 每次 +0.02 importance |
| `decay_type`（线性/指数/阶跃） | 只留线性，`decay_rate` 每天固定扣 |

不借（明确写下来，免得以后又想起来）：

- FAISS / 向量检索、BM25 双路融合、RRF
- 图结构（`graph_nodes` / `graph_edges` / trigram 检索）
- MMR 多样性、rerank
- 会话修订号与回滚对账（我们不是多轮 Agent，没有历史回滚的概念）

## 五、如果哪天真要上 AstrBot

前置三个硬门槛（文档里有人踩过，说明不是偶发）：

1. Embedding Provider 必填（推荐 `BAAI/bge-m3`，1024 维）——没有它就没有语义召回；
2. 主程序 ≥ 4.24.2 才有「插件视图」，否则管理页不可用；
3. FAISS 在 Windows 上有 `Illegal instruction`、`SuperKMeans 未定义`、
   `faiss-cpu` 版本与核心依赖冲突这几类问题。

而且它的**个人微信适配器**（4.22.0+）走的是扫码 + 长轮询，要求手机微信
iOS ≥ 8.0.70 / Android ≥ 8.0.69 且带 ClawBot 插件——和本项目「驱动 PC 微信
4.1.8 控件树」是两条完全不同的路，换过去等于把最难的那部分成果丢掉。

## 六、一句话

学它的**问题划分**（钩子在哪、记忆从哪来、凭据怎么管），不学它的**代码**；
它的前端之所以要那一层桥，是 iframe 逼出来的，我们没有那个约束。
