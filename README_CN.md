<p align="center">
  <img src="static/icons/tofu-welcome.svg" width="140" height="160" alt="Tofu logo" /><br/>
  <img src="static/icons/tofu-brand-title.svg" width="280" height="78" alt="Tofu" /><br/>
  <sub>豆腐 — 自托管 AI 助手</sub>
</p>

<p align="center">
  <a href="https://github.com/rangehow/ToFu/actions/workflows/ci.yml"><img src="https://github.com/rangehow/ToFu/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.10+-3776ab?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/SQLite-3-003B57?logo=sqlite&logoColor=white" alt="SQLite" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License" />
  <img src="https://img.shields.io/badge/platform-Linux%20·%20macOS%20·%20Windows-lightgrey" alt="Platform" />
</p>

<p align="center">
  <a href="README.md">🇬🇧 English</a>
</p>

<p align="center">
  <img src="propaganda/mainpage.jpg" width="800" alt="主界面" />
</p>

---

## Tofu 是什么？

Tofu 是一个**完全自托管的 AI 助手**，一条命令即可启动。它可以连接任何 OpenAI 兼容的大模型 API，为你提供一个完整的 AI 工作空间 —— 从简单的问答，到能自主搜索网页、编辑代码、读论文、操控浏览器、多智能体协作的全能智能体。

一切都运行在你自己的机器上，数据不会离开你的基础设施。一条命令，开箱即用。

**Tofu 有何不同：**

- **一条命令，全部搞定** —— 安装器会为你装好运行时、依赖、数据库和浏览器引擎。不强制 Docker（但支持），不用手动建库，不用手改配置文件。
- **想用什么模型都行** —— OpenAI、Anthropic、Gemini、DeepSeek、Qwen、GLM，Ollama/vLLM 本地模型，甚至直接登录你已有的 **Claude Pro/Max / ChatGPT 订阅**。加多个密钥，Tofu 会自动轮换与负载均衡。
- **是真正的智能体，不只是聊天框** —— 它能自主跑多步任务：搜索、阅读、写代码、跑命令、生成图片，并用 Planner → Worker → Critic 循环自我检查。
- **完全属于你** —— 自托管、MIT 许可、无遥测。你的对话、密钥和文件都留在你自己的基础设施上。
- **一切皆 API** —— UI 里的每个功能都是有文档的 HTTP 接口，可以脚本化或接入其它工具（见 [无头 API](#无头-apiheadless-api)）。

> **想从 AI 智能体或编码助手驱动 Tofu？** 本文档是写给*人*看的。另有一套面向机器的材料 —— 见底部的 [面向 AI 智能体与开发者](#面向-ai-智能体与开发者)。

---

## 快速开始

挑一个匹配你系统的命令运行，结束后服务器会跑在 **http://localhost:15000**。

| 系统 | 怎么做 |
|---|---|
| **Windows** | 从[最新 Release](https://github.com/rangehow/ToFu/releases/latest)下载 **`Tofu-Setup-x.y.z-win64.exe`**，双击运行。 |
| **Linux / macOS** | `curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh \| bash` |
| **Docker** | `git clone https://github.com/rangehow/ToFu.git && cd ToFu && docker compose up -d` |

> **macOS —— 想要点开即用的桌面应用？** 不走上面的 `install.sh`，可到[最新 Release](https://github.com/rangehow/ToFu/releases/latest)下载 `.dmg`，并按你的芯片选择：
> Apple 芯片（M1/M2/M3…）选 **`Tofu-*-macos-arm64.dmg`**，Intel Mac 选 **`Tofu-*-macos-x86_64.dmg`**。

> **给已运行的服务器再添一台机器？** 完全不用回到 GitHub：运行中的 Tofu 服务器自己就能托管安装包。
> 在应用里打开**本地控制**（桌面版那一行），下载按钮会直接从你的服务器给出匹配这台机器的安装包 ——
> 由后台镜像自最新 Release（Windows/macOS），或在服务器上直接构建（Linux/Windows，从已提交的代码树经
> `POST /api/v1/desktop/build`）。文件相同，但不再依赖公网 GitHub 链路。
>
> 安装包有两个组件，按这台机器的角色选：
>
> | 组件 | 适合 | 体积 | 包含 |
> |---|---|---|---|
> | **受控端（TofuAgent）** | 只想让服务器操作这台电脑（含订阅流量出口） | ~53 MB | 角色窗 + 控制面板（中英双语）；托盘是它的镜像；可设开机自启 |
> | **完整桌面版（Tofu）** | 这台电脑自己也要跑 Tofu（本机同时是服务器+客户端） | ~153 MB | 完整服务器 + 浏览器界面 + 角色窗 + 托盘 |
>
> 本地控制的下载区会按场景把合适的那个放在主位；Release 页面两个都提供（`TofuAgent-Setup-*` / `Tofu-Setup-*`）。
>
> 两个组件启动时都会弹出**角色窗**——完整版写明「这台电脑是 Tofu 服务器」，受控端写明「这台电脑是
> Tofu 受控端」——它同时就是控制面板（权限、连接命令、开机自启），重要功能不再只藏在托盘里。
> 取消勾选「启动时显示此窗口」即可直达托盘；托盘里保留「控制面板…」随时重新打开。

就这一步。每个方式都会自动处理运行时、依赖、数据库、浏览器引擎，并启动服务器 —— 无需任何参数，无需后续操作。Linux/macOS 上安装器默认走 [uv](https://github.com/astral-sh/uv) 快速路径（预编译 wheel，约 1–2 分钟），在较旧系统（glibc < 2.28）上自动回退到 conda；可用 `--use-conda` 强制走 conda。默认存储后端是 **SQLite**（零配置、WAL），PostgreSQL 是同等级、仅由 `TOFU_DB_BACKEND=postgres` 显式启用的选项；两者都由项目内 Storage Sidecar 独占，所选后端不可用时拒绝启动而不切换引擎。详见[存储契约](docs/STORAGE_REDESIGN.md)。

源码目录里的日常启动命令仍然是：

```bash
python server.py
```

它现在会启动或连接项目内的用户态 manager，等唯一的 server 就绪后返回；重复运行不会创建第二个实例。运维时使用 `python serverctl.py status|stop|restart|doctor`，查看控制台日志使用 `python serverctl.py logs -f`。显式 `stop` 后会保持停止；崩溃或 OOM 退出则会自动恢复。

如果这个源码目录还需要在主机或用户会话重启后自动恢复 manager，执行一次 `python serverctl.py install`。它会安全替换旧的 `tofu_guard` cron 项，不会重启健康的 server。

生产环境的内存隔离、告警阈值、备份恢复演练和 Kubernetes 基线见
[《极致稳定运行手册》](docs/RELIABILITY_RUNBOOK.md)。

> 想预设 API 密钥、改端口、或安装失败需要恢复？所有可选参数和故障排查方案都在 **[docs/INSTALL.md](docs/INSTALL.md)**。

---

## 连接你的大模型

<p align="center">
  <img src="propaganda/providersetting.jpg" width="700" alt="服务商设置" />
</p>

点击 **⚙️ 设置 → 🔗 服务商**，添加你的 API 密钥。Tofu 支持任何 OpenAI 兼容的 API：

| 服务商 | 配置方式 |
|---|---|
| OpenAI、Anthropic、Amazon Bedrock、Google Gemini、DeepSeek、Qwen、MiniMax、GLM、Doubao、Mistral、Grok、百度千帆、OpenRouter | 点击 **⚡ 从模板添加** —— 一键完成 |
| Ollama、vLLM 或任何本地模型服务 | 使用默认端口（Ollama `11434`、vLLM `8000`、SGLang `30000`）时启动即**自动发现** —— 或添加为自定义服务商，填入你的本地端点 |
| Azure OpenAI | 模板可用，填入部署专属的 Base URL |

**同一服务商多个密钥** —— 添加多个 API 密钥，当某个密钥触发限速时自动轮换到下一个。跨服务商的智能调度器会根据实时延迟评分和错误率追踪来路由请求。

**模型列表自动与服务商保持一致** —— 模板只负责第一次配置所需的元数据。保存密钥后，Tofu 会定期用该账号已认证的 `/models` 目录对账：新开放的模型自动出现；连续两次成功对账都缺失的模型才会退出。网络失败或空响应始终保留最后一次正确列表，手工添加的私有部署会自动固定，不会被清理。可在每个服务商卡片上单独关闭；`TOFU_MODEL_CATALOG_SYNC=0` 可全局关闭。

**本地引擎自动发现** —— Tofu 启动后约 5 秒、之后每 2 分钟探测一次本机规范端口（Ollama `:11434`、vLLM `:8000`、SGLang `:30000`，另加 `$OLLAMA_HOST`）。引擎返回非空模型列表时会自动注册为普通本地服务商——健康检查和设置卡片与手动添加完全一致。删除自动添加的服务商后其端口会被永久忽略（不会复活）。设 `TOFU_LOCAL_AUTODISCOVER=0` 可关闭。

或者通过环境变量配置（适用于无界面/Docker 部署）：
```bash
export LLM_API_KEY=sk-xxx
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_MODEL=gpt-4o
```

---

## 无头 API（Headless API）

UI 里能做的事情同样以有文档的 HTTP API 形式暴露，你可以从脚本、Agent 或你自己的应用里驱动 Tofu，不需要渲染 Web UI。

**挂载点：**

| 前缀 | 接口面 |
|---|---|
| `/api/v1/*` | Tofu 原生 —— 与 UI 功能打平（chat、conversations、tasks、agents、capabilities、keys、usage、billing …） |
| `/v1/...` | OpenAI 兼容 —— `chat/completions`、`models`、`embeddings`（OpenAI SDK 可直接点过来） |
| `/v1/messages` | Anthropic 兼容 —— Messages API（Anthropic SDK 可直接点过来） |
| `/metrics` | Prometheus 曝露格式（限 admin 作用域） |

**自描述：**`/api/openapi.json` 和 `/api/openapi.yaml`（OpenAPI 3.1）、Swagger UI 位于 `/api/docs`、ReDoc 位于 `/api/redoc`。

在 **设置 → 🔑 API Keys** 中**管理密钥**：创建、划分作用域（`chat`/`admin` 等）、设置每密钥的 RPM 和 TPD 限额、吊销、查看每密钥 30 天使用量图表。POST 请求支持 Idempotency-Key（24 小时缓存，按身份加盐）。所有响应都会返回标准的限速头（`X-RateLimit-*`、`Retry-After`）。

**凭证保管库** 在 **设置 → ⚙️ 高级**：GitHub PAT、PyPI token 这类机器/发布凭证集中存放——Fernet 加密落盘在本机 `data/config/`（密钥独立文件、600 权限），永不进代码仓、永不随导出外发；列表只显示掩码提示（如 `ghp_…3V8`），明文只有点「查看」才出库（全程审计），30 秒自动隐藏。

**客户端 SDK** 在 [`clients/`](clients/) 下：

```bash
# Python —— 同步的 `Tofu` 类 + `tofu` 命令行
cd clients/python && pip install -e ".[cli]"
export TOFU_API_KEY=tofu_admin_xxx TOFU_BASE_URL=http://localhost:15000
tofu chat "你好"

# TypeScript —— Node 18+、浏览器、Cloudflare Workers、Vercel Edge、Deno、Bun
cd clients/typescript && npm install
```

也可直接用任意 OpenAI / Anthropic SDK，只需将 Base URL 指向你的 Tofu 服务、API Key 填 `tofu_admin_*` 即可。

---

## 多租户中继站（付费模式）

当 `auth_mode=multi-user` 时，Tofu 变为一个**付费的 AI 中继站** —— 一份自托管部署服务多个用户，每个用户一个钱包，费用以你上游的 LLM 成本为准计费。

**切换该模式后自动点亮的能力**（在 `open`/`private` 下为 no-op）：

- **每用户钱包**，单位为微点数（1 信用点 = 1,000,000 µ ≈ 美元 0.001），以**追加写账本**为唯一真相源 —— 全部整数运算，无浮点误差。
- **原子预扣 / 清算**：每个聊天请求先做预扣，余额不足直接返回 402；任务结束后按实际 token 用量清算；后台清扫器每 30 分钟释放过期的未清算预扣。
- **面向用户的页面** —— `/login`、`/signup`、`/dashboard`（钱包、密钥、使用量、文档、账号页签）。注册准入策略与迎新信用在 `data/config/relay.json` 中配置。
- **支付** —— Stripe Webhook 与支付宝异步通知，按 `(provider, provider_id)` 幂等。凭证在 `data/config/payments.json` 中配置。
- **兑换码** —— 批量生成，发给用户，换入钱包。
- **价格表** —— 每模型的单价（输入 / 输出 / 缓存）从 `data/config/pricing.json` 热加载，支持按模型族前缀回退与管理员可调的加价率。
- **管理控制台** —— 一个独立的 **`/admin`** 面板（Users、Pricing、Redeem Codes、Payments），仅在 multi-user 模式下、以 `admin` 作用域密钥才能访问。

上述能力的实现全部位于 `lib/billing/` 与 `routes/api_v1/billing.py`；切回 `open` / `private` 后表便为空、面向用户的页面不再提供，闸道行为与上一节描述一致。

---

## 功能详解

### 💬 与任何模型对话

<p align="center">
  <img src="propaganda/chatinner.jpg" width="700" alt="对话界面" />
</p>

核心体验：从下拉菜单选择模型，输入消息，获得流式回复。但 Tofu 远不止一个基础对话框。

**想用不同模型试同一个问题？** 随时在对话中切换模型。每条消息都记住了生成它的模型，方便你自然地对比输出。你还可以对任意助手回复进行分支，用不同模型或参数探索替代答案，所有分支都在同一个对话线程中。

**用中文提问但需要英文资料？** 开启按对话的自动翻译。你的中文问题会被翻译成英文发给模型，英文回复再翻译回中文。原文始终保留，点击即可切换查看。想要更快更便宜的翻译？可以接入专用的[机器翻译服务商](#-机器翻译)来替代 LLM。

**对话太长，上下文快爆了？** Tofu 的 3 层上下文压缩流水线自动处理：
1. **微压缩**（零成本）：旧的工具调用结果被替换为摘要，只保留最近的"热尾巴"
2. **结构化截断**：思考过程块、过大的参数、冗余截图被裁剪
3. **LLM 摘要**（强制触发）：当上下文压力过高时，一个廉价模型评估每轮对话的相关性并据此压缩

智能体工具循环还会默认维持 **128K token 的经济工作集**，即使模型声明了
1M 上下文，也不会等到接近窗口上限才压缩，从而限制每轮反复计费和传输的提示词。
可按请求设置 `compaction.workingSetTokens`，或全局设置
`TOFU_WORKING_CONTEXT_TOKENS`（设为 `0` 恢复旧的“只看窗口上限”行为）。
GPT-5.6 Responses 请求还会使用稳定的哈希缓存命名空间、显式稳定前缀断点、
加密 reasoning 往返，以及无状态服务端压缩兜底。

为了安全验证节省效果，**设置 → 高级**提供默认关闭、按对话稳定分组的
**成本优化 A/B 实验**。它对比兼容基线（MCP schema 全内联、只按窗口压缩）与
当前优化策略；完成的每轮会显示所在组，报表按“提供商 usage × 当轮价格快照”统计
真实成本、定价覆盖率、token、缓存命中、延迟和无错误结束代理指标。显式的按请求
覆盖会自动排除。首次启用默认只灰度 10% 对话；已保存实验若调整流量或分组比例，
必须更换实验 ID，避免旧对话换组。关闭开关即可立即回滚。

**想整理你的对话？** 在侧边栏创建文件夹来分组相关对话。可以在文件夹之间拖拽，也可以不归类。

**想按顺序读懂它的思路？** 已完成的多工具回复会以*交错时间线*渲染：每个工具调用就近显示在引出它的思考与说明文字旁边，让你按操作实际发生的顺序阅读，而不是"所有思考 / 所有工具 / 所有正文"三大块。默认开启，可在 **设置 → 通用 → "按工具内联时间线"** 中切换。

**当对话变长时** —— 一个**上下文健康条**显示你已用掉多少模型窗口，Tofu 会自动**压缩**较早的轮次，让对话不撞上限继续进行。你可以打开**压缩查看器**，看清究竟压缩了哪些内容，不会有"丢东西"的感觉。

---

### 🎙️ 语音输入（语音转文字）

当打字比说话慢 —— 直接对着输入框口述。

**使用方法：** 点击输入框旁的**麦克风按钮**，允许麦克风权限，说话，再点一次停止。你的语音会被转写并插入到光标处 —— **不会自动发送**，你可以先审阅、编辑再发。这是听写，不是语音对话。

**只有**当浏览器支持录音*且*配置了语音转文字模型时，麦克风按钮才会出现，否则保持隐藏。启用方式：在 **设置 → 服务商** 添加一个模型带 `transcription`（或 `audio_chat`）能力的服务商：

| 服务商 | 模型 |
|---|---|
| **OpenAI** | `gpt-4o-transcribe`、`gpt-4o-mini-transcribe`、`whisper-1` |
| **Groq** | `whisper-large-v3-turbo`、`whisper-large-v3`（快、便宜） |
| **全模态对话模型** | Gemini / LongCat 等，走 `audio_chat` |

可选环境变量：`TOFU_AUDIO_MAX_BYTES`（默认 25 MB）、`TOFU_AUDIO_MAX_DURATION_S`（默认 600 秒）。没有单独的语音设置页 —— 是否可用完全取决于所配置的模型。

---

### 🎬 视频上传与分析

把视频拖进输入框（或点 📎 选择），然后直接提问 —— “主讲人关于 X 说了什么？”“总结一下这段视频”“找到图表出现的那一刻”。

**工作原理：** 附件一添加，Tofu 就在后台处理 —— 把视频抽成一组带时间戳的帧（均匀采样打底 + 场景切换检测补充），如果视频有音轨，还会通过你配置的语音转文字模型（与语音输入同一条链）生成台词转录。点击发送时，帧 + 转录随消息发给任何支持视觉的模型 —— 不需要专门的视频模型。附件芯片会显示处理进度，发送会自动等它就绪。

- **格式** —— mp4 / mov / webm / mkv / avi，上限 **512 MB / 15 分钟**
- **模型感知的帧预算** —— 实际发送的帧数随所选模型的视觉能力、上下文窗口和单请求图片上限自适应（例如 Claude 的 100 图上限会跨整个会话记账）
- **没配转录模型？** 视频照用 —— 模型只分析画面帧
- **聊天模型不支持视觉？** 只要配置里有任意视觉模型，Tofu 会在上传时把画面帧讲成文字解说词，纯文本模型也能回答视频相关问题
- 点击已发送消息上的视频卡片可回放原文件

可选环境变量：`TOFU_VIDEO_MAX_BYTES`（默认 512 MB）、`TOFU_VIDEO_MAX_DURATION_S`（默认 900 秒）、`TOFU_VIDEO_ANALYSIS=0` 可整体关闭。抽帧使用内置 ffmpeg（缺失时通过 `imageio-ffmpeg` 自动安装）。

---

### 🔍 网页搜索与内容抓取

当助手需要实时信息 —— 今天的新闻、文档更新、API 参考 —— 它可以搜索网页并阅读页面。

**工作原理：** 在工具栏启用 🔍 开关。助手会并行搜索多个引擎（DuckDuckGo、Brave、Bing、SearXNG），去重后抓取最相关的页面。可选的 LLM 内容过滤器会自动去除导航栏、广告和模板代码。

**直接粘贴 URL？** 助手直接抓取，支持 HTML、PDF 和纯文本。如果页面需要登录认证，使用浏览器插件代替（见下文）。

**配置** —— 在 **设置 → 🔍 搜索与抓取** 中：
- 自动抓取的结果数量（默认：6）
- 每页超时和最大字符数
- 屏蔽域名列表
- 是否启用 LLM 内容过滤（关闭可加速）

---

### 🛠️ 工具调用与自主智能体

这是 Tofu 超越普通聊天机器人的地方。启用工具后，助手可以自主执行多步操作 —— 搜索网页、运行代码、编辑文件、生成图片 —— 将这些串联起来解决复杂任务。

**内置工具：**
| 工具 | 功能 |
|---|---|
| `web_search` | 搜索网页（多引擎并行） |
| `fetch_url` | 读取任意 URL（HTML、PDF、纯文本） |
| `run_command` | 执行 Shell 命令 |
| `generate_image` | 创建或编辑图片（Gemini、GPT-image） |
| `ask_human` | 任务中途暂停并向你提问 |
| `list_conversations` / `get_conversation` | 引用过往对话 |
| `create_memory` / `update_memory` / `delete_memory` | 保存知识供未来使用 |
| `check_error_logs` / `resolve_error` | 检查和解决项目日志中的错误 |
| 浏览器工具 | 操控你的浏览器（通过插件） |
| 桌面工具 | 操控你的本地机器（通过代理） |
| 项目工具 | 浏览、搜索、编辑任意代码库 |
| 定时任务工具 | 创建周期性自动化任务 |
| Swarm 工具 | 启动并行子智能体 |

**需要基于实时数据的快速回答？** —— "英伟达今天的股价是多少？"助手搜索、抓取相关页面并回答。

**需要多步骤工作流？** —— "调研排名前 5 的 React 状态管理库，做个对比，写一份推荐文档。"助手会规划步骤、执行搜索、阅读文档并综合结果 —— 全程自主完成。

**任务太复杂，一次搞不定？** —— 启用 **终端模式**（Planner → Worker → Critic）。规划者将你的需求改写为结构化简报并附上验收标准，执行者执行，审查者对照清单审查。如果结果不通过，审查者提反馈让执行者迭代 —— 最多 10 轮。

**出错了怎么办？** —— 助手会以指数退避策略自动重试。如果主模型完全失败，它会自动切换到配置的备选模型继续执行。

---

### 💻 项目协作（Co-Pilot）

将 Tofu 指向任意代码库，它就变成了一个能读取、搜索、编辑和执行命令的编程助手。

**开始使用：** 点击侧边栏的 **Project**，输入代码库路径（例如 `/home/you/myproject`）。助手将获得以下工具：

| 工具 | 功能 |
|---|---|
| `list_dir` | 浏览目录结构，含文件大小和行数 |
| `read_files` | 读取文件（支持图片、PDF、Office 文档、代码 —— 带行号） |
| `grep_search` | 使用 ripgrep 跨文件搜索（正则、上下文行、计数模式） |
| `find_files` | 按通配符模式查找文件 |
| `write_file` | 创建或覆盖文件 |
| `edit_file` | 替换文本或在锚点前后插入内容，支持混合批量编辑 |
| `run_command` | 在项目目录中执行 Shell 命令 |

**想快速了解一个新代码库？** —— "给我概述一下这个项目的架构。"助手会浏览目录树、阅读关键文件，梳理出整体结构。

**需要修 Bug？** —— "登录页提交后白屏了。"助手会 grep 相关代码、阅读组件、定位问题，然后用 `edit_file` 精确修复。

**想安全地实验？** —— 每次文件修改都按对话跟踪，支持完整撤销。点击撤销按钮即可回滚助手做的任何改动。

**多项目根目录** —— 可添加多个目录作为根（例如前端 + 后端仓库）。助手通过命名空间在所有根目录之间解析路径。

**智能 Token 管理** —— `content_ref` 机制让助手可以将之前的工具结果直接写入文件而无需重新生成。这在处理大文件时能节省大量 Token。

---

### 🧩 项目大脑（Project Brain）—— 让同一项目的多个对话协同起来

当你在同一个项目上开了多个对话，**项目大脑**让它们像一个协同的团队一样工作，而不是各自为战、彼此健忘的聊天。每个对话都能看到其它对话在做什么、共享同样的目标和决策、避免互相踩踏 —— 项目甚至能自主认领并推进待办工作，而这一切始终对你完全可见、可控。

**如何使用：** **只要对话处于项目模式**（挂到某个项目目录）就**自动生效** —— 没有开关。你通过项目栏打开的**项目大脑面板**（实时活动、章程、看板、状态几个页签）来观察和引导它，协作栏里还有一行状态标题。唯一需要*你*的地方：智能体只能*提议*修改共享章程 —— 由你点击 **提交** 或 **拒绝**。

| 能力 | 它给你什么 |
|---|---|
| **章程（Charter）** | 一份共享的北极星文档 + 每个对话都遵循的既定决策。改动需人工批准。 |
| **看板与 Epic** | 一块可认领的工作流看板，让各对话分工、不重复劳动。 |
| **活动流** | 每个对话此刻在做什么的实时脉搏。 |
| **对话间消息** | 一个对话可以给另一个发出建议式提醒 —— 有限速、绝不打断进行中的工作。 |
| **路径租约** | 一个对话可以预定它正在编辑的文件，让其它对话先让开，避免编辑冲突。 |
| **状态通道** | 向项目询问"我们进展如何？有没有跑偏？"并得到综合回答，外加一份大脑会持续复查的关注清单。 |
| **自主调度** | 空闲项目会自动认领并启动就绪、未被阻塞的工作，无需交接。 |

---

### 🤖 多智能体集群（Swarm）

有些任务大到单个智能体难以胜任。Swarm 系统让一个主编排器规划子任务，并将它们分派给并行运行的专家智能体。

**什么时候用：** "把这个微服务拆分成 3 个独立服务，更新 API 文档，写迁移脚本。"与其让一个智能体按顺序做完所有事，主编排器会为每个子任务启动并行智能体。

**工作原理：**
1. 主 LLM 规划子任务并分配角色（编码者、研究者、写作者、审查者……）
2. **流式 DAG 调度器**在依赖完成后立即启动智能体 —— 不等待整波完成
3. 智能体通过**产出物仓库**（所有智能体可见的键值对）共享数据
4. 智能体完成后，主编排器审查结果并可启动后续智能体
5. 最终结果被综合为连贯的输出

**智能体角色** —— 每个智能体获得角色专属的系统提示词、模型层级和限定的工具访问权限。"研究者"有搜索工具；"编码者"有项目工具；"审查者"只有只读权限。

**限速** —— 共享信号量防止智能体用并发请求压垮 LLM API。遇到 429 错误时自动指数退避。

---

### 🌐 机器翻译

当你频繁使用翻译功能，希望更快更省钱 —— 接入专用的机器翻译服务商，替代 LLM 翻译。

**工作原理：** 默认情况下，Tofu 使用廉价 LLM 模型进行自动翻译（能理解上下文，但速度较慢）。配置机器翻译服务商后，所有翻译请求直接走 MT API —— 通常比 LLM 翻译**快 3–5 倍**、**便宜 10–100 倍**，且没有 Prompt 开销。

**配置方法：** 打开 **设置 → 🌐 翻译**，启用机器翻译，选择服务商：

| 服务商 | 说明 | 获取 API Key |
|---|---|---|
| **小牛翻译（NiuTrans）** | 中文机器翻译专家，支持 300+ 语言对 | [niutrans.com/cloud/overview](https://niutrans.com/cloud/overview) |
| **自定义** | 任何兼容的 REST API | 填入你的端点和凭证 |

小牛翻译是默认服务商，中英翻译质量出色。在设置卡片中点击 **"申请 API Key"** 即可注册。

**回退机制：**
- **未配置 MT** → 使用廉价 LLM 模型（默认，开箱即用）
- **已配置 MT** → 使用 MT API；如果失败，自动回退到 LLM 翻译
- **代码块保护** → 翻译前自动提取围栏代码块（` ```...``` `）和行内代码（`` `...` ``），翻译后还原，防止 MT 破坏代码

---

### 🔀 订阅登录（Claude Pro/Max · ChatGPT）

已经在为 **Claude Pro/Max** 或 **ChatGPT** 付费订阅？直接用它登录，Tofu 就把该订阅当作一个模型服务商使用 —— 无需单独的 API 密钥，计费走你已有的订阅套餐。

**使用方法：** 打开 **设置 → 服务商**，点击 **登录 Claude** / **登录 ChatGPT**。Tofu 会走一遍 PKCE OAuth 流程（`lib/oauth/`），保存 token，并由 `lib/oauth/outbound.py` 把已登录的订阅桥接进一个受管理的服务商 slot —— 每次请求时解析成实时 token 加上所需的客户端身份请求头。此后该订阅就与调度器里的其它服务商一样（slot 轮换、回退、延迟评分都适用）。

- **Claude** 请求走 Anthropic Messages API，并带上 Claude-Code 身份请求头。
- **ChatGPT（Codex）** 请求会自动转换为 Responses API 格式。

> 这是一个正常的调度内服务商，**不是** CLI 子进程。（早期的 CLI 子进程式"后端切换"机制 —— `lib/agent_backends/` —— 已于 2026-06 移除，改为这个更简单的"订阅即服务商"路径。）

---

### 🌐 浏览器插件

当你需要助手阅读登录后才能看的页面 —— 内部仪表盘、JIRA 工单、需要认证的管理后台 —— 浏览器插件可以桥接你真实的浏览器会话到 Tofu。

支持 **Chrome、Edge、Chromium** —— 它们同属 Chromium 系，同一份插件无需改动即可加载。（不支持 Firefox：它没有持久的"加载已解压扩展"路径 —— `about:debugging` 加载的扩展重启浏览器就会消失，终端用户只能安装经 Mozilla 签名的扩展包。）

**安装：**
1. 打开浏览器的扩展管理页 —— Chrome/Chromium 是 `chrome://extensions`，Edge 是 `edge://extensions` —— 并启用开发者模式
2. 加载已解压的扩展程序 → 选择 `browser_extension/` 目录
3. 点击插件图标 → 输入你的 Tofu 服务器地址

> Tofu 就跑在你这台机器上？设置 → 本机控制会自动识别你装的是哪个浏览器，并提供一键按钮直接打开对应的扩展管理页，路径也已复制好。

**可以做什么：**

| 工具 | 用途 |
|---|---|
| `browser_list_tabs` | 查看你所有打开的标签页 |
| `browser_read_tab` | 提取文本内容（可选 CSS 选择器） |
| `browser_screenshot` | 截取页面截图 |
| `browser_navigate` | 打开一个 URL |
| `browser_click` | 通过选择器或文本点击元素 |
| `browser_type` | 在输入框中输入文字 |
| `browser_execute_js` | 运行自定义 JavaScript 提取数据 |
| `browser_get_interactive_elements` | 发现可点击/可输入的元素 |
| `browser_get_app_state` | 访问 Vue/React 内部状态 |

点击不只是点击：`text=` 模糊匹配也能命中纯 JS 框架的「卡片式」按钮（cursor:pointer 区域）；点击若打开了新标签页，结果会明确告知（新页编号、标题、URL）并自动把新页设为当前工作页——无需再手动翻标签页列表。

**页面使用 Canvas/SVG 渲染（图表、DAG 图等）？** DOM 文本提取会返回空内容。用 `browser_screenshot` 做视觉分析，`browser_get_app_state` 获取数据，或 `browser_execute_js` 自定义提取。

**多个浏览器**可以同时连接，拥有独立的命令队列 —— 适合你有工作和个人不同浏览器配置文件的场景。

---

### 🖥️ 桌面代理

当你需要助手超越浏览器与本地机器交互 —— 全屏截图、读写本地文件、自动化 GUI 点击、管理剪贴板。

**安装：**
```bash
pip install pyautogui pillow psutil
python -m lib.desktop_agent --server http://your-server:15000 --allow-write --allow-exec
```

代理连接到你的 Tofu 服务器，提供文件操作、剪贴板、截图、GUI 自动化（pyautogui）和系统信息等工具。所有危险操作需要显式启用 `--allow-write` / `--allow-exec` 标志。

#### 远程工作树（Remote Worktree）

让 Studio **直接修改你本地机器上的项目代码**（Windows / macOS 均可）——不共享文件系统，只把「文件意图」安全地路由到本机执行。

**使用旅程：**
1. 在本机启动 agent，用 `--root 名字=路径` 声明共享根（哪些项目目录允许被访问；可重复，持久化到 `~/.tofu/desktop_agent.json`，重启不失）：
   ```bash
   python -m lib.desktop_agent --server https://your-server --allow-write --allow-exec \
       --bridge-secret <第 2 步的令牌> --root myapp=~/code/myapp
   ```；
2. 打开 **设置 → 设备**，颁发一个 bridge 令牌（只显示一次），填进 agent 配置——令牌绑定你的账号，命令只投递给你自己的设备；
3. 在项目选择器顶部的「远程设备」分组里，把某个共享根加进工作区（离线设备灰显不可选）；
4. 之后 Studio 的 `write_file` / `apply_diff` / `run_command` 等全部落在**你的本地磁盘**——写前自动快照（`<项目>/.tofu/file-history/` 可回滚），外部改动会拒写并要求先重读，`run_command` 输出像服务器端一样**实时流式**显示在终端块里。

**安全边界：** 路径只允许根内相对路径（符号链接/`..`/绝对路径逃逸全拒）；删除类命令目标必须落在根内；远程写入默认走 Manual 批准门；每用户令牌在 relay 部署下隔离命令投递。总开关 `TOFU_REMOTE_WORKTREE`（服务端）默认关闭，开启后以上才生效。详见 `docs/REMOTE_WORKTREE_DESIGN.md`。

---

### 📄 论文阅读模式（Beta）

阅读科研论文 —— arXiv PDF、会议论文集、内部白皮书 —— Paper Reader 把 Tofu 变成一个専用的科研阅读伙伴。

**使用方法：** 点击侧边栏的 **📄 Paper** 按钮。页面分屏：**左侧 PDF、右侧对话 + 笔记**。上传 PDF 或粘贴 arXiv 链接（`arxiv.org/abs/XXXX.XXXXX`） —— Tofu 会抓取、解析、索引全文，令助手能基于论文内容精准回答。

**能做什么：**
- **有据可依的问答** —— “表 3 的消融实验结果是什么？”或“解释 4.2 节”，助手会引用具体段落
- **论文库** —— 左侧侧边栏展示你读过的所有论文，按时间分组；切换论文不丢失上下文
- **并排阅读** —— 滚动 PDF 的同时聊天；助手可感知你当前所在页面
- **笔记面板** —— 在论文旁边记录你自己的笔记，跨会话持久保存
- **播客面板** —— 把论文的分析报告变成一档可收听的单人播客（约 5 分钟短版 / 约 15 分钟完整版）。公式只讲直觉（绝不念符号），最关键的图用三段式口播讲解，剧本里的每个数字都经机器溯源校验后才合成语音。可在面板内直接播放（逐字稿点击跳转、睡眠定时），也可下载 MP3 或导出剧本。需先生成报告；未配置 TTS 语音合成槽位时（OpenAI 兼容 `/audio/speech` 服务商、模型声明 `capabilities: ["tts"]`）自动降级为仅生成剧本与逐字稿。详见 `docs/PAPER_PODCAST_DESIGN.md`。

> ⚠️ **Beta：** 论文阅读模式正在持续迭代中，欢迎在 [GitHub Issues](https://github.com/rangehow/ToFu/issues) 反馈。

#### 默认 PDF 解析栈与修复

`install.sh` 会把默认富文本解析器作为一组精确兼容的三件套安装
（`pymupdf`、`pymupdf_layout`、`pymupdf4llm`），并在宣布安装成功前实际跑一页
Markdown 抽取。检查已有环境：

```bash
python scripts/verify_pdf_stack.py
```

若报告缺包或版本分裂，请把 `requirements.txt` 中的三个固定版本一起重装，不要只装
`pymupdf4llm`，然后重新运行检查：

```bash
python -m pip install --upgrade --force-reinstall \
  "pymupdf==1.27.2.3" "pymupdf_layout==1.27.2.3" "pymupdf4llm==1.27.2.3"
python scripts/verify_pdf_stack.py
```

#### 可选：使用 Docling 进行版面感知的 PDF 解析

默认的 PDF 解析管线（`pymupdf4llm`）在大多数论文上表现良好，但在 ML / 理论 CS 论文中常见的**无框表格**和**复杂数学公式**上效果较差。对于这类论文，Tofu 可以路由到
[**Docling**](https://github.com/docling-project/docling)（IBM）——一个版面感知的模型，使用 TableFormer 处理表格，内置方程模型处理公式，输出更干净的 Markdown。

**权衡：** Docling 会拉入 PyTorch + 约 2 GB 的模型权重，所以是**可选安装**：

```bash
pip install docling --extra-index-url https://download.pytorch.org/whl/cpu
```

然后在 `.env` 中设置 `PDF_TEXT_MODE=structured`，或者在 `/api/pdf/parse` 请求中通过表单字段 `textMode=structured` 单次启用。
如果请求 `structured` 但 Docling 未安装，服务器会自动回退到 `pymupdf4llm` —— 上传不会失败。

---

### 🖼️ 图片生成

当你需要视觉内容 —— 插图、图表、Logo、修图 —— 助手可以在对话中直接生成图片。

**使用方法：** 在工具栏启用 🖼️ 开关，然后描述你想要的内容。助手会调用 `generate_image` 并附上详细提示词。

- **从零创建** —— "画一个极简风格的山与日出 Logo"
- **编辑已有图片** —— 上传一张图片并说"把背景换成海滩日落"
- **保存到项目** —— 指定 `output_path` 直接保存到代码库中
- **SVG 转换** —— 添加 `svg: true` 自动将生成的 PNG 转换为可缩放矢量图

多模型调度在 Gemini 和 GPT 图片模型之间轮转，遇到限速自动重试。

---
多模型调度在 Gemini 和 GPT 图片模型之间轮转，遇到限速自动重试。

---

### 📊 演示文稿（可编辑 PPTX）

只要给一个主题，Tofu 就能生成可编辑 PowerPoint，不依赖 Kimi 网站或编辑器。流水线先研究带真实 URL 的事实卡，再规划整套 deck 的叙事与版式节奏，在逐页创作前生成所需主视觉，渲染预览，同时审查单页和整套接触表，最后导出原生 PPTX。

- **整套先规划** —— 每页都有叙事职责、命名版式原型、信息密度、相邻页上下文和素材义务；连续内容页不会悄悄退化成同一种卡片模板
- **事实既有出处也有时效** —— PPT 与主题视频共用同一证据内核：事实性主题并发检索“当前状态 / 官方来源候选 / 背景材料”，PPT 使用近一月 profile，新闻视频使用近一周并为常青主题回退不限时；来源卡保留发布日期、检索车道和研究截止时间，跨站一致金额与单一摘要分开提示，同一个当前事实硬闸会拒绝遗漏或冲突的发布、预售与价格状态
- **素材先于排版** —— 每个必需主视觉先明确它要支撑的判断或可见对象，再在页面创作前生成并缓存；页面若生成后不使用该素材，校验会拒绝
- **整套质量审查** —— 带页码接触表检查视觉身份一致性、版式重复和素材相关性；逐页 VLM 会沿每条引线追到可见端点，防止标注指错物体或部位；浏览器还会按真实字形行框加 PowerPoint 字体度量安全区，检查文字溢出、互相遮挡以及后置图片盖字，并把元素 ID 送回最小修复回路
- **原生交付** —— 文字、形状、表格、图表和图片在 PowerPoint/WPS 中保持可编辑；固定文本框默认写入原生缩小适应，源码缩进不会再生成幽灵空段落，字体按实际字符及粗/斜体槽做最小子集嵌入；后续对话可只重做某一页并重新导出

---

### 🎬 动画视频（MG 动效）

把字幕稿（SRT）变成竖屏 MG 动画视频 —— Tofu 会把字幕按语义分镜、为每个镜头编写 HyperFrames HTML 动画、用无头 Chrome 逐镜头渲染，最后拼接出 `final.mp4`。不依赖任何外部 agent 命令行，也不需要视频剪辑软件。

**使用方法：** 挂载项目（Studio），贴一段 SRT（或给一个口播主题），然后说生成视频 —— 例如“把这份字幕做成竖屏短视频”。助手会在项目的 `.tofu/motion_video/<slug>/` 下工作，并报告成品 MP4 路径。

- **逐镜证据关联** —— 主题视频复用 PPT 的时效研究与当前事实硬闸；每个事实镜头的 `S#` 来源 ID 会一直保留到 `scenes.json`，静音片尾来源卡只负责署名，不再替代逐镜事实关联
- **确定性渲染** —— 每一帧都由时间戳独立计算（可寻址的 GSAP 时间轴）；某个镜头不满意时，只重渲该镜头再重新拼接即可
- **结构化镜头配方** —— 每镜都有 renderer-neutral 的 `motion-shot-v1` 合同：叙事职责、13 种实证配方之一、运动族、能量、建议时长、相位数、落定停留、QA 锚点、约束、标志动作和转场意图；作者开工前直接拿到对应 HyperFrames 蓝图全文，自动规划的相邻镜头还会主动避开同一种运动语法
- **真实重叠转场** —— `motion-timeline-v1` 把口播内容时长与视觉 handle 分开：push / wipe / dissolve 通过 FFmpeg `xfade` 真正重叠，但成片总时长、字幕与旁白时钟保持不变；场景面板会显示实际转场种类和秒数
- **素材先于构图** —— 分镜要求的 subject/diagram 必须写清具体语义目标（素材要证明的可见对象、部位或关系），再在写 HTML 前生成并缓存；生成了却没被构图使用，会进入质量记录
- **配方感知的多时刻质检** —— 不再固定抽 15% / 50% / 90%：每种配方选择 setup / peak / settle / resolved hold 四个语义锚点；截图前必须等字体就绪、全部图片 decode，视觉模型同时拿到接触表和该镜头的语义约束，能发现空白开场、中途证据错配、碰撞和未完成收束
- **零 LLM 质量闸** —— 分镜时间轴校验（完整覆盖、时长和 ±0.1s）、每次渲染前的 HyperFrames lint/validate/inspect、渲染后的 ffprobe 规格复核（分辨率/帧率/时长/静音）
- **工具链自举** —— `motion_video_env_check` 首次使用自动安装钉版 HyperFrames CLI；ffmpeg 来自 `imageio-ffmpeg`、ffprobe 来自静态构建（均免 root）；Chrome 复用 Playwright 缓存
- **失败分类** —— 渲染错误按类返回（`env_missing` / `lint` / `chrome` / `timeout` / `aborted`）并附带上游修复提示，不用啃原始日志
- **专业音频时间线** —— `motion_video_narrate` 负责真实 TTS 时长；可选 `motion-audio-v1` 在整片层加入有授权/来源、经哈希固化的本地 BGM 与 SFX，支持动作峰值、镜头进度和已验证节拍对齐、旁白触发 BGM ducking 与目标响度，并交付音频归因清单；没有 TTS 时仍可生成静音或 BGM/SFX-only 视频
- **无头 API + 并行渲染** —— `POST /api/v1/motion/videos` 在服务端跑完整条流水线（标准化整片计划 + 逐镜作者构图、有界并行渲染、重复请求自动合并入队），成品经支持 Range 的 `/api/v1/motion/videos/<id>/file` 下载，并附对齐后的侧车 SRT
- **单镜重生成 + 字幕烧录** —— 只重渲某一个镜头并自动重组装（`POST …/scenes/<id>/regen`，成品 URL 不变），逐镜状态可查（`GET …/scenes`），可选硬烧字幕（`burn_in: true`，libass，支持中文字体）
- **论文视频摘要** —— 一次调用把论文报告变成一条配音 MG 短视频（`POST /api/v1/paper/video/start`，与播客链同款报告门）
- **论文「视频」页签** —— 论文阅读器第五个页签：生成卡（语言/画质/音色/配音/烧录）、实时相位进度、内嵌播放器，以及逐镜网格——每个镜头的配方/能量/真实转场会和预览、重渲按钮一起显示；可下载音频归因清单，`GET /api/v1/motion/shot-recipes` 与 `/audio-contract` 开放同一合同给未来 Studio 与其他渲染 adapter
- **深度知识包** —— 来自 vibe-motion/auto-motion 的 29 条动效规则、13 个镜头蓝图、20+ 设计帧预设，在 设置 → 技能 里搜 “hyperframes” 一键安装

---

### 🎨 Artifacts（实时画布）

当助手产出你更想*看*而不是滚动略过的东西 —— 一个完整的 HTML 页面、一张 SVG 图、一段类 React 代码，或一篇长文档 —— 它会变成一个 **Artifact（工件）**。

**工作原理：** 消息旁会出现一个可点击的小标签；点开后 Tofu 会在侧边面板里实时渲染 —— HTML/SVG 在沙箱 iframe 中呈现，Markdown 经过安全净化。每个工件都有版本，可在多个修订间切换、**收藏（pin）**、在按对话的**库（library）**里浏览全部、并**导出为 PDF**。非常适合搭一个小网页、一张图表或一份排版文档，并就地迭代。

---

### 🔗 MCP（模型上下文协议）

当你想连接外部工具服务器 —— GitHub、数据库、自定义 API —— MCP 可以把它们桥接到 Tofu 的工具系统中。

**工作原理：** MCP 服务器既可以作为本地子进程运行（stdio），也可以是远程 HTTP 端点（`streamable-http` / `sse`），统一使用 JSON-RPC 2.0 通信。Tofu 将它们的工具翻译成 OpenAI function-calling 格式，让 LLM 可以像使用原生工具一样发现和调用它们。

大型目录会自动采用**渐进式披露**：MCP 工具不超过 16 个时仍直接内联；超过后，
每轮请求只携带 3 个稳定元工具，分别负责目录检索、只读调用和写调用。检索命中后
才返回所需工具的精确 schema；MCP 的只读/写入注解仍继续控制并行与审批边界。
可用 `mcpToolExposure=inline|progressive|auto` 和 `mcpInlineToolLimit`
按请求覆盖，或使用对应的 `TOFU_MCP_TOOL_EXPOSURE` /
`TOFU_MCP_INLINE_TOOL_LIMIT` 环境变量。

**配置：** 在 **设置** 中或编辑 `data/config/mcp_servers.json`：
```json
{
  "github": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": { "GITHUB_TOKEN": "ghp_xxx" }
  }
}
```

**远程服务器**若需要鉴权，密钥只放在 `env` 里；`headers` 与 `url` 只写
`${VAR}` 引用，连接时才代入。这样密钥只有一个存放处，并且会从所有 API
响应与日志中被脱敏掉：
```json
{
  "rollinggo-hotel": {
    "transport": "streamable-http",
    "url": "https://mcp.rollinggo.cn/mcp",
    "headers": { "Authorization": "Bearer ${ROLLINGGO_API_KEY}" },
    "env": { "ROLLINGGO_API_KEY": "你的 key" }
  },
  "amap-maps": {
    "transport": "streamable-http",
    "url": "https://mcp.amap.com/mcp?key=${AMAP_MAPS_API_KEY}",
    "env": { "AMAP_MAPS_API_KEY": "你的 key" }
  }
}
```

之后助手就可以调用 `mcp__github__create_issue`、`mcp__github__search_code` 等工具 —— 任何 MCP 兼容的服务器都能接入。

**本地生活与出行（中国）。** 内置目录里有一类「办日常事」的服务器，个人开发者都能自助拿到
凭证：**高德地图**（路径规划、周边搜索、天气）、**RollingGo** 酒店 + 机票（真实可订库存）、
**途牛**（酒店/机票/火车/门票/邮轮/度假，支持完整下单链路）、**12306**（火车余票查询，本地运行、
无需 key）。**飞猪**是 Skill 形态而非 MCP 服务器，请在 **设置 → Skills** 中查找。
携程与美团**故意未收录**：它们的 AI 开放平台仅对企业客户开放，做成一键安装卡片会点不动。

---

### ☑️ 每日报告与 My Day

点击侧边栏的 **☑️ My Day** 按钮，打开你的个人工作日志 —— 一个由 LLM 驱动的每日看板。

**想看看今天完成了什么？** —— LLM 阅读当天所有对话，将它们聚类为 5–15 个连贯的工作流（如"修复图片渲染 Bug"、"部署测试环境"），标记为*已完成*、*进行中*或*被阻塞*。

**需要明天的计划？** —— LLM 从未完成的工作中综合出 3–8 个可执行的待办事项，每个都附有详细提示词和推荐的工具配置。点击 ▶ 即可将任何待办启动为新对话，预填好内容、开好工具，直接干活。

**日历视图** —— 月度总览，显示每天的对话数量和费用热力图。点击任意日期查看或生成当天报告。

**待办管理** —— 未完成的待办自动顺延到第二天。可手动添加待办、切换完成状态，或启动为新对话。费用追踪显示每天和每个对话的花费（人民币）。

**自动回填** —— 后台调度器在服务器启动时和每天午夜自动生成昨天的报告（如缺失）。

---

### 🕐 定时任务

当你需要自动执行的任务 —— 每日数据拉取、周期性健康检查、定期报告 —— 创建一个按计划运行的主动代理。

**使用方法：** 启用 🕐 定时任务开关，然后说："每 6 小时对我的 API 做一次健康检查"或"每天早上 9 点总结一下昨晚的 GitHub issues。"助手会创建一个类 cron 的定时任务。

**任务类型：** Shell 命令、Python 脚本或 LLM 提示词 —— 都可以使用完整的工具集。

**管理任务：** 点击顶部状态栏的 **SCHEDULER** 徽章，查看所有活跃的主动代理和最近的运行日志。

---

### 🔧 自我调优（每日优化器）

Tofu 会默默观察自己的表现，并提议一些小改进 —— 每一处改动都由你掌控。

**工作原理：** 一个每晚运行的循环会分析近期运行并起草提案（如"屏蔽一个刷屏的搜索域名""调整某个默认值"）。点击顶栏的 **OPTIMIZER** 徽标，逐条查看其理由、严重程度和置信度，然后**批准、拒绝、回滚**，或点击 **立即运行**。未经你同意不会应用任何改动（一小撮安全的微调可自动应用，且一切可回滚）。可在设置里完全关闭。

---

### 🐦 飞书（Lark）机器人

当你的团队在飞书中沟通，希望直接在群聊里使用 AI 助手 —— Tofu 通过 WebSocket 连接为飞书机器人。

**配置：**
1. 在 [open.feishu.cn](https://open.feishu.cn/app) 创建应用，启用机器人能力
2. 打开 **设置 → 🐦 飞书** → 输入 App ID 和 App Secret
3. 重启服务器后机器人自动连接

**功能：** 支持完整工具调用（搜索、代码、项目）的多轮对话，斜杠命令切换模型/模式，对话管理 —— 全部在飞书原生聊天界面中完成。

---

### 🧠 记忆系统

当助手发现了有用的东西 —— 一个 Bug 模式、一个项目规范、你偏好的编码风格 —— 它可以把这些知识保存为**记忆**，供未来的会话使用。

**工作原理：** 项目级记忆以 Markdown 文件形式存储在项目内的 `.tofu/memories/`；全局记忆（跨项目共享）存放在服务端存储 `data/memories/global/`。每个新任务只用本地元数据做高置信匹配，最多自动带入两条；需要时助手仍可显式搜索完整记忆库。自动检索不会暗中调用另一个 LLM。

**工具：** `search_memories`、`create_memory`、`update_memory`、`delete_memory`、`merge_memories` —— 助手跨会话管理自己的知识库。关闭 Memory 会同时移除记忆上下文和这些工具。

**使用场景：** "记住我们的 API 总是返回 snake_case。" —— 助手保存这个规范，并在以后为这个项目生成代码时自动应用。

---

### 📚 技能商店（Skills Store）

当你想给助手一套可复用的、打包好的专项本领 —— 针对某类任务的说明加辅助脚本 —— 安装一个**技能（Skill）**。

**工作原理：** 技能遵循开放的 Claude / OpenClaw / AgentSkills 格式（一个 `SKILL.md` 加可选的参考文件与脚本）。打开 **设置 → Skills**，浏览推荐技能的**目录（Catalog）**（如 Anthropic 的 docx / xlsx / pdf / skill-creator）并**一键安装**，或**拖拽本地 `.zip`** 进来。新安装默认放在**全局**（所有对话可用，也可在头部切换为仅本项目）；已安装的技能出现在**已安装（Installed）**页签，可查看文件、在全局/本项目之间移动、或卸载。附带的 `install.sh` 脚本只作为提示展示 —— 绝不会自动执行。

模型平时只看到精简索引；任务匹配时才用准确 id 调用 `load_skill`，把指南加载到当前任务。“启用”是设置中的持久状态，“加载”不是另一套激活状态。Skill 是用户安装的工作流，Memory 是模型积累的经验记录。

**密钥配置：** 需要 API Key 的技能（如飞猪 FlyAI）会在卡片上列出所需的环境变量，点「配置」填入即可——值加密存放在**凭证保管库**（`data/config/`，600 权限，永不进仓、永不导出），执行工具调用时自动注入子进程环境，无需重启服务，也无需把密钥贴进聊天。卸载技能会一并删除它在保管库里的密钥。

---

### 🔀 对话分支

当你想探索不同方向又不想丢失当前的对话线索 —— 对任意助手回复进行分支。

**工作原理：** 点击任意助手消息上的分支图标。一个新分支在行内打开，从该节点开始拥有独立的历史记录。多个分支可以同时流式输出。每个分支可以使用不同的模型或参数。

**使用场景：**
- 对比不同模型对同一问题的回答
- 尝试另一种方案又不丢失当前进度
- 让一个分支做调研，另一个分支做实现

---

### 🐾 豆腐宠物（纯为好玩）

切换到 **豆腐（Tofu）** 主题，豆腐吉祥物本尊会入驻项目栏 —— 就是应用 logo 那块等轴测奶油色豆腐，用同一套配色绘制。它在一片装饰场景里走来走去，有真实的行走动画和情绪 —— 任务加载时思考、成功时庆祝、入夜后打盹 —— 走过时还会惊动场景（草丛分开、水面起波、云朵流动）。因为它是一块豆腐而不是有手脚的生物，它靠「软」来演：落地时压扁、起跳时拉伸、停下时晃一晃。可以把它拖到栏里任何位置，或者点一下看今天的概况。用项目栏里的 **场景** 按钮（草地 / 水池 / 天空 / 关闭）换它的世界。它会尊重系统的"减少动态效果"设置。纯装饰，随时可关。

---

## 设置参考

所有配置通过 **⚙️ 设置** 面板完成（右上角齿轮图标）。更改即时保存，无需重启。

| 选项卡 | 配置内容 |
|---|---|
| **⚙️ 通用** | 主题（暗色/亮色/豆腐）、温度、最大 Token 数、思维深度、系统提示词、按工具内联时间线 |
| **🔗 服务商** | API 密钥、端点、模型列表（含转写/音频模型）、多密钥轮换、自动发现 |
| **📦 显示** | 下拉列表中显示哪些模型、默认模型、备选模型 |
| **🔍 搜索与抓取** | 结果数量、超时、字符限制、屏蔽域名、内容过滤 |
| **🌐 翻译** | 机器翻译服务商（小牛翻译 / 自定义）、API 密钥、端点 |
| **🌐 网络** | 代理池（有序、分域：仅订阅流量/全局流量，失败自动故障转移，凭证入凭证保管库，逐条连通性测试）、代理绕过域名 |
| **🔀 订阅登录** | 登录 Claude Pro/Max 或 ChatGPT，当作服务商使用 |
| **🐦 飞书** | 应用凭证、默认项目路径、允许的用户 |
| **🔗 MCP** | 模型上下文协议服务器（App-Store 目录 + 自定义） |
| **📚 Skills** | 浏览、安装、管理可复用的技能包 |
| **🧠 记忆与偏好** | 已存记忆和你的长期偏好档案 |
| **🔑 API Keys** | 创建/划分作用域/吊销无头 API 密钥、每密钥限额、认证模式 |
| **`</>` 高级** | 价格覆盖、缓存管理、服务器信息 |

> 多租户中继站的管理面板（Users、Pricing、Redeem Codes、Payments）现在位于独立的 **`/admin`** 控制台，而非设置页签。

### 环境变量（备用）

对于无界面/Docker 部署，可通过环境变量替代设置界面进行配置。复制模板并编辑：

```bash
cp .env.example .env
vim .env   # 填入你的值
```

`.env.example` 文件中包含了所有支持的变量及说明，主要变量如下：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `LLM_API_KEYS` | API 密钥（逗号分隔，支持多个） | *（无）* |
| `LLM_BASE_URL` | API 端点 | `https://api.openai.com/v1` |
| `LLM_MODEL` | 默认模型 | `gpt-4o` |
| `PORT` | 服务器端口 | `15000` |
| `BIND_HOST` | 绑定地址 | `0.0.0.0`（所有网卡） |
| `TOFU_AUTH_MODE` | 强制认证模式并锁定 UI：`open` / `private` / `multi-user` | *（以配置文件为准）* |
| `TOFU_AUTO_KEY` | 设为 `0` 可跳过首次启动的管理员密钥初始化 | `1` |
| `TOFU_MODEL_CATALOG_SYNC` | 自动维护远程服务商模型列表；设为 `0` 可全局关闭 | `1` |
| `TOFU_WORKING_CONTEXT_TOKENS` | 反复重放提示词的经济工作集上限；`0` 表示禁用 | `128000` |
| `TOFU_MCP_TOOL_EXPOSURE` | MCP schema 模式：`auto` / `inline` / `progressive` | `auto` |
| `TOFU_MCP_INLINE_TOOL_LIMIT` | `auto` 模式下允许直接内联的 MCP 工具数 | `16` |
| `TUNNEL_TOKEN` | **已废弃**，仅作向后兼容垫——请改用 API Keys 体系 | *（关闭）* |
| `TRADING_ENABLED` | 启用交易模块（`1`/`0`） | `0` |
| `PDF_TEXT_MODE` | 默认 PDF 文本提取策略：`rich`（pymupdf4llm，默认）、`structured`（Docling，需 `pip install docling`）、`fast` | `rich` |
| `PDF_VLM_BATCH_PAGES` | VLM 单次调用的页数（1–16） | `4` |
| `PDF_VLM_MAX_WORKERS` | 并发 VLM 调用上限（共享密钥时调小可避免 429 风暴） | 不限 |

> **优先级：** 设置界面 > `.env` 文件 > 系统环境变量 > 默认值。你也可以直接用 `export` 设置变量——`.env` 只是一种便捷方式。

---

## 项目结构

```
├── server.py                  Flask 应用入口，中间件，日志
├── bootstrap.py               自动依赖修复（LLM 引导）
├── index.html                 主聊天 UI（单页应用）
│
├── lib/                       核心库
│   ├── agent_core/            可复用智能体基座（运行循环、调度、TaskRuntime、push、profiles）
│   ├── llm/                   LLM API 客户端包（build_body / stream / cache / diagnostics）
│   ├── llm_dispatch/          多密钥多模型智能调度器
│   ├── storage/               storage.v1 客户端与监督器（不加载数据库驱动）
│   ├── storage_sidecar/       独占 SQLite/PostgreSQL 驱动、连接与事务
│   ├── database/              正迁往命名存储操作的仓储层
│   ├── tasks_pkg/             任务编排与上下文压缩
│   │   ├── orchestrator/      LLM ↔ 工具主循环（包）
│   │   ├── executor/          工具执行引擎（包）
│   │   ├── endpoint/          Planner → Worker → Critic 循环（包）
│   │   └── compaction/        3 层上下文压缩（包）
│   ├── tools/                 工具定义与 Schema
│   ├── swarm/                 多智能体编排
│   ├── search_bridge.py      对接外部 `tofu_search` 包的接缝（网页搜索与抓取）
│   ├── research/             自动科研流水线（检索 → 综述 → 选题）
│   ├── longform/             长篇研究报告
│   ├── motion_video/         动画视频流水线
│   ├── production/           生产基座（阶段图、证据/内容契约、崩溃续跑）
│   ├── tts/                  语音合成 / 旁白
│   ├── skills/               用户安装的技能包（AgentSkills 格式）
│   ├── browser/               浏览器插件桥接
│   ├── project_mod/           项目协作（扫描、编辑、撤销）
│   ├── memory/                记忆积累系统
│   ├── mcp/                   模型上下文协议桥接
│   ├── feishu/                飞书机器人集成
│   ├── scheduler/             任务调度（cron、主动代理）
│   ├── image_gen/            图片生成（多模型调度）
│   ├── mt_provider/          机器翻译服务商适配（小牛翻译、自定义）
│   ├── desktop_agent/         桌面自动化代理（本地桥）
│   └── ...
│
├── lib/conversations/         项目大脑 —— 章程、看板、活动流、对话间消息、路径租约、状态通道
├── routes/                    Quart 蓝图 + routes/api_v1/（无头 API）
├── lib/billing/               多租户中继计费（钱包、账本、价格、支付）
├── lib/oauth/                 OAuth 流程（Claude、Codex、PKCE、Token 存储）
├── lib/optimizer/             夜间自调优循环（分析器 → 提议器 → 应用器）
├── clients/                   无头 API SDK（python/、typescript/）
├── static/                    CSS、JS、图标
├── browser_extension/         Chrome 插件（Manifest V3）
├── tests/                     测试套件（单元、API、E2E）
└── data/                      运行时数据（已加入 .gitignore）
```

> Tofu 运行在 **Quart**（异步 Flask）之上，由 **Hypercorn** 承载；已有的同步路由
> 处理器在线程池中原样运行。

---

## 平台支持

| 功能 | Linux | macOS | Windows |
|---|:---:|:---:|:---:|
| 核心对话与工具 | ✅ | ✅ | ✅ |
| SQLite Sidecar 后端 | ✅ | ✅ | ✅ |
| PostgreSQL Sidecar 后端 | ✅ | ✅ | ✅ |
| 项目协作 | ✅ | ✅ | ✅ |
| Shell 命令 | ✅ | ✅ | ✅ (`cmd.exe`) |
| 桌面代理 | ✅ | ✅ | ✅ |
| 浏览器插件 | ✅ | ✅ | ✅ |

烟雾测试：`python debug/test_cross_platform.py`

---

## 测试

```bash
# 全部测试
python tests/run_all.py

# 单独测试套件
python -m pytest tests/test_backend_unit.py
python -m pytest tests/test_api_integration.py
python -m pytest tests/test_visual_e2e.py

# 或用 Makefile（并行；用 JOBS=N 调节）
make test-unit        # 快速单元层
make test-api         # API 集成层
make test-frontend    # jsdom 前端套件
make test-all         # 全部
```

---

## 认证模式与安全

Tofu 采用三态认证模型，持久化在 `data/config/auth.json`，可在 **设置 → 🔑 API Keys** 面板顶部切换：

| 模式 | 闸道 | 适用场景 |
|---|---|---|
| `open`（默认） | 直通；合成本机管理员上下文 | 个人部署、只用前端、仅本机绑定 |
| `private` | 必须携带 Bearer / `x-api-key` / Cookie / `?token=`；`/` 上返回 HTML 提示页 | 单使用者多设备 |
| `multi-user` | 与 `private` 同样闸道，加上每用户钱包 + 注册页面 | 付费中继站，服务多个用户 |

**默认绑定 `0.0.0.0`** —— API 开箱即可从局域网访问（桌面代理配对流程依赖这一点）。传入 `--host 127.0.0.1` 或设置 `BIND_HOST=127.0.0.1` 可仅绑定本机；打包桌面版会自行锁定本机绑定。默认 `open` 认证模式下这意味着局域网无需令牌即可访问——启动横幅会对该组合发出醒目警告，在不可信网络上请切换到 `private` 模式（设置 → API Keys）。

**开放模式限流按暴露面自动决定。** 针对昂贵接口（聊天 / 智能体 / 搜索 / 生成）的每 IP 防刷帽，只在 `TOFU_OPEN_MODE_ALLOW_REMOTE=1` 放行未认证远端访客时才自动启用（120 次/分钟/IP）。仅本机使用的个人部署默认**不限流**——桶里能看到的只有你自己的标签页和后台轮询；即使帽子启用，环境心跳读（任务轮询、状态探测）也从不计入。`TOFU_OPEN_MODE_RPM=<n>` 双向覆盖（n>0 强制启用，`0` 强制关闭）。同机隧道下所有访客都是 `127.0.0.1`，IP 帽无法区分你和陌生人——那种形态的正解是 `private` 模式或 API Key，而不是 IP 限流。

**首次启动初始化**（仅 private/multi-user）—— 当 api_keys 存储为空且 `TUNNEL_TOKEN` 未设置时，Tofu 会在启动时造一把 `tofu_admin_<hex>` 密钥，将明文 + 一次性 `?token=<...>` URL 打印到 stderr，同时将明文写入 `data/config/.first_run_token`（chmod 0600）。设置 `TOFU_AUTO_KEY=0` 可禁用。

**Token 传输顺序**：`Authorization: Bearer` → `x-api-key`（Anthropic SDK）→ `tofu_session` HttpOnly Cookie → `?token=` 查询参数（会被消费后剩余、转为 Cookie，仅 private 模式）。

**`TOFU_AUTH_MODE=<mode>`** 环境变量会锁死模式 —— UI 单选按钮被禁用，`PUT /api/v1/auth/mode` 会返回 409 + `error_kind=env_locked`。

**其他安全要点：**
- 源码中无密钥——所有凭证从环境变量或设置界面加载。
- 工具执行——助手可以运行 Shell 命令和编辑文件；危险模式会被拦截，但请谨慎使用。
- 桌面代理——需要显式启用 `--allow-write` / `--allow-exec` 标志。
- `TUNNEL_TOKEN` 已废弃，仅作为向后兼容垫，启动时会警告——请迁移到 API Keys 体系。

---

## 面向 AI 智能体与开发者

本文档是写给人看的。如果你要从编码助手驱动 Tofu、在它之上二次开发，或贡献代码，面向机器的材料在这里：

| 文档 | 内容 |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | AI 辅助改代码的项目情报与强制规则（日志纪律、代码风格、改动审批闸门、前后端边界）。 |
| [`JOURNAL.md`](JOURNAL.md) | 项目演进日志 —— 试过什么、为何变更、当前状态。 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 完整目录地图 + Mermaid 架构图（可视化版见 `docs/architecture.html`）。 |
| [`docs/HEADLESS_API.md`](docs/HEADLESS_API.md) | 完整的无头 API 参考（`/api/v1/*`、OpenAI、Anthropic 三个接口面）。 |
| [`docs/CUSTOM_TOOLS.md`](docs/CUSTOM_TOOLS.md) · [`docs/TOOL_PLUGINS.md`](docs/TOOL_PLUGINS.md) | 添加自定义工具与插件蓝图。 |
| [`docs/PROJECT_BRAIN.md`](docs/PROJECT_BRAIN.md) | 对话间协同机制深入讲解。 |
| `/api/openapi.json` · `/api/docs` | 运行实例提供的实时 OpenAPI 3.1 规范 + Swagger UI。 |

---

## 贡献

请参阅 [CONTRIBUTING.md](CONTRIBUTING.md) 获取完整指南。简要版：

1. Fork → 创建功能分支
2. `python healthcheck.py && python tests/run_all.py`
3. 提交 Pull Request

---

## 许可证

MIT
