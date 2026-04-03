<p align="center">
  <img src="static/icons/tofu-welcome.svg" width="140" height="160" alt="Tofu logo" /><br/>
  <img src="static/icons/tofu-brand-title.svg" width="280" height="78" alt="Tofu" /><br/>
  <sub>豆腐 — 自托管 AI 助手</sub>
</p>

<p align="center">
  多模型对话 · 自主智能体 · 项目协作 · 多智能体集群<br/>
  每日报告与待办 · 浏览器插件 · 桌面代理 · 飞书机器人
</p>

<p align="center">
  <a href="README.md">🇬🇧 English</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-3776ab?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/PostgreSQL-18+-336791?logo=postgresql&logoColor=white" alt="PostgreSQL" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License" />
  <img src="https://img.shields.io/badge/platform-Linux%20·%20macOS%20·%20Windows-lightgrey" alt="Platform" />
</p>

---

## Tofu 是什么？

Tofu 是一个完全自托管的 AI 助手，采用 **Flask 后端** + **原生 JS 前端**。它可以连接任何 OpenAI 兼容的 LLM API，提供自主工具调用智能体、代码项目协作、多智能体集群编排、浏览器插件和桌面代理 —— 只需一条命令 `python server.py` 即可启动。

---

## 功能特性

### 多模型对话

- **20+ 大语言模型** — OpenAI、Anthropic、Google Gemini、Qwen、DeepSeek、MiniMax、Doubao、GLM、Mistral、Grok，以及任何 OpenAI 兼容 API
- **智能调度** — 多密钥、多服务商路由，支持实时延迟评分、错误率追踪和按密钥限速冷却
- **流式响应**，支持按模型计费跟踪（输入/输出/缓存 token × 阶梯定价）
- **多模型对比** — 同一提示词同时发送给多个模型，并排对比输出
- **自动翻译** — 按对话设置中英文双向自动翻译

### 工具调用与智能体模式

- **内置工具** — 网页搜索（多引擎并行：DDG、Brave、Bing、SearXNG）、网页抓取、PDF 解析（文本 + VLM）、图片上传与生成、Shell 命令、Python 执行
- **项目协作** — 将任意代码库设为工作区，支持文件浏览、grep 搜索、代码编辑、Git 操作和 AI 驱动的文件索引
- **自主任务执行** — 多步骤工具链，自动重试，3 层上下文压缩，可配置模型回退链
- **终端模式** — Planner → Worker → Critic 审查循环，适用于长时间运行的任务
- **定时任务** — 类 cron 定时或一次性任务（Shell、Python、LLM 提示词）
- **图片生成** — 多模型调度（Gemini + GPT 图片模型），自动 429 重试轮转

### 多智能体集群

- **集群编排** — MasterOrchestrator 制定计划并分发给多个并行运行的 SubAgent
- **流式调度** — 响应式任务调度，无波次屏障，支持智能体间的产出物共享
- **审查与综合** — 自动审查智能体输出并综合为连贯的最终结果

### 浏览器插件

- Chrome 插件连接助手与你的浏览器，实现实时网页阅读和交互
- 按客户端路由 — 多个浏览器可同时连接并拥有独立的命令队列
- 支持导航、截图、点击、输入、提取内容

### 桌面代理

- 运行在你的本地机器上，连接回服务器
- 文件系统操作、剪贴板、截图、GUI 自动化（pyautogui）、系统信息
- 安全保障：需要显式启用 `--allow-write` / `--allow-exec` 标志

### 飞书机器人

- 通过 WebSocket 完整集成飞书机器人 — 在团队聊天中直接进行多轮 LLM 对话，支持工具调用
- 支持斜杠命令、模型/模式切换、对话管理

### 每日报告与待办事项

点击侧边栏顶部的 ☑️ **My Day** 按钮打开每日看板 — 一个由 LLM 驱动的个人工作日志。

- **自动生成工作流** — LLM 阅读当天所有对话，将它们聚类为 5–15 个连贯的工作流（如"修复图片回显"、"项目部署调试"），并标记为*已完成*、*进行中*或*被阻塞*
- **日历视图** — 月度总览日历，显示每天的对话数量和费用热力图；点击任意日期查看或生成报告
- **明日计划** — LLM 从未完成的工作中综合出 3–8 个可执行的待办事项，每个都带有详细提示词和推荐的工具配置
- **一键启动** — 点击待办事项旁的 ▶ 按钮，立即打开一个预填任务提示词并开启相应工具的新对话（搜索、代码、浏览器、项目等）
- **待办自动继承** — 未完成的待办事项会自动顺延到第二天作为"今日待办"；LLM 会追踪哪些已完成并自动标记
- **手动添加待办** — 通过底部的 ＋ 输入框添加自定义待办事项；可标记完成/未完成、删除或启动为新对话
- **费用追踪** — 按天和按对话的费用明细（人民币），根据 token 用量和模型定价计算
- **自动回填** — 后台调度器在服务器启动时和每天午夜自动生成昨天的报告（如缺失）
- **每日小语** — 每份报告顶部随机显示一句励志小语（"人生苦短，我用 AI" 🧈）

### 定时任务

- **主动代理调度器** — 通过对话或调度器面板创建类 cron 定时或一次性任务（Shell、Python、LLM 提示词）
- **SCHEDULER 徽章** — 显示在顶部状态栏；点击可查看所有活跃的主动代理及其最近运行日志
- 通过工具子菜单中的 🕐 **定时任务** 开关启用

### 更多特性

- **技能系统** — 持久化的可复用知识（Markdown 文件）—— 助手跨会话学习项目规范、Bug 模式和工作流
- **3 层上下文压缩** — 微压缩 → 结构化截断 → LLM 摘要，适用于超长对话
- **IndexedDB 缓存** — 读穿式对话缓存，LRU 淘汰策略，页面加载更快
- **错误追踪** — 通用项目错误追踪器，支持指纹识别、解决状态追踪和摘要报告
- **暗色主题 UI**，响应式布局，语法高亮，LaTeX 渲染，图片预览
- **跨平台** — 支持 Linux、macOS 和 Windows（详见[平台支持](#平台支持)）
- **自动依赖修复** — `bootstrap.py` 通过 LLM 诊断自动安装缺失的 pip 包

---

## 快速开始

### 前提条件

- Python 3.10+
- conda（用于安装 PostgreSQL 服务器）

### 1. 克隆并安装

```bash
git clone https://github.com/rangehow/ToFu.git
cd ToFu

# 更新 conda 到最新版本
conda update -n base -c defaults conda

# 创建名为 'tofu' 的新环境，安装 Python 3.12
conda create -n tofu python=3.12 -y

# 激活环境
conda activate tofu

# 安装 PostgreSQL 18+ 服务器（用户态，无需 root 权限）
conda install -c conda-forge postgresql>=18

# 安装 Python 依赖
pip install -r requirements.txt

# 安装浏览器自动化引擎（高级抓取功能需要）
playwright install chromium
```

> PostgreSQL 以本地用户态进程运行 — 无需 `sudo`，无需系统服务。
> 首次运行 `python server.py` 时，数据库会自动初始化（`initdb`、建表、端口选择）。

### 2. 启动

```bash
python server.py
```

在浏览器中打开 **http://localhost:15000** — 就这么简单！所有配置都可以在设置界面中完成。

#### 自动依赖修复

如果缺少任何 Python 包，`server.py` 会自动委托给 `bootstrap.py` 处理：

1. 检测到 `ImportError`，移交给 `bootstrap.py`
2. 在相同端口打开一个实时状态页面
3. LLM API 诊断错误堆栈，确定需要安装哪些包
4. 自动执行 `pip install`，最多重试 10 轮
5. 所有依赖解决后，正式服务器启动

此过程仅使用 Python 标准库 — 即使*所有* pip 包都缺失也能正常工作。

---

## 配置

**所有配置都通过设置界面完成** — 点击聊天界面右上角的 ⚙️ 齿轮图标即可打开。更改会即时保存到服务器，无需重启（除非特别说明）。

设置面板包含 **7 个选项卡**，每个在左侧边栏都有专属图标：

### ⚙️ 通用

核心模型参数和全局偏好设置。

- **主题** — 暗色、亮色或豆腐（Tofu）主题
- **温度 (Temperature)** — 控制回复随机性（0 = 确定性，1 = 创意性）
- **最大 Token 数** — 输出 token 上限
- **图片最大宽度** — 自动压缩上传的图片（0 = 不压缩）
- **PDF 最大页数** — 解析 PDF 时的页数限制
- **思维深度** — 新对话的默认思维预算（关闭 / 中等 / 深度 / 最大）
- **系统提示词** — 自定义指令，会添加在每次对话之前

### 🔗 服务商

多服务商 API 管理 — 在此添加你的 LLM API 密钥。

- **⚡ 从模板添加** — 一键配置 OpenAI、Anthropic、Google Gemini、DeepSeek、Qwen、MiniMax、GLM、Doubao、Mistral、Grok、OpenRouter、Azure、Ollama 等
- **自定义服务商** — 添加任何 OpenAI 兼容的端点和自定义 Base URL
- **按服务商配置** — 每个服务商有独立的 API 密钥、Base URL 和模型列表
- **自动发现模型** — 从服务商的 `/v1/models` 端点自动获取可用模型
- **多密钥轮转** — 每个服务商可添加多个 API 密钥，自动进行限速轮转

### 📦 显示

控制模型选择器和图片生成选择器中显示的内容。

- **图片生成模型** — 在图片生成选择器中显示/隐藏特定模型
- **模型下拉列表** — 在主聊天模型切换器中显示/隐藏模型
- **回退模型** — 主模型请求失败时自动切换到此模型
- **默认模型** — 覆盖新对话的默认模型

### 🔍 搜索与抓取

网页搜索和内容抓取行为。

- **LLM 内容过滤** — 使用模型过滤抓取页面中的导航栏/广告（关闭可加速抓取）
- **抓取前 N 条** — 搜索后自动抓取排名靠前的网页数量（默认：6）
- **抓取超时** — 每页超时秒数（默认：15）
- **最大字符数** — 分别设置搜索结果、直接抓取 URL 和 PDF 文件的字符限制
- **最大下载大小** — 抓取内容的字节上限（默认：20 MB）
- **屏蔽域名** — 抓取器永远不会访问的域名（每行一个）

### 🌐 网络

所有出站请求的代理配置。

- **HTTP / HTTPS 代理** — 用于 LLM API 调用、搜索和网页抓取的代理 URL
- **不代理域名** — 完全绕过代理的域名后缀（每行一个，后缀匹配）
- 💡 **提示**：如果你的企业/VPN 代理会静默断开 SSE 长连接导致 `BrokenPipeError`，请在此添加你的 LLM API 域名

### 🐦 飞书 (Lark)

飞书机器人集成设置。

- **连接状态** — 实时显示机器人连接状态的指示灯
- **App ID / App Secret** — 从 [open.feishu.cn](https://open.feishu.cn/app) 获取的凭证（修改后需重启服务器）
- **默认项目路径** — 飞书对话中项目协作的根目录
- **工作空间根目录** — 用于项目切换的基础目录
- **允许的用户** — 限制特定飞书用户 ID 使用机器人（留空 = 允许所有人）

### `</>` 高级

定价和缓存管理。

- **价格覆盖** — 以 JSON 格式自定义按模型定价（美元/每百万 token）
- **本地缓存** — 查看 IndexedDB 缓存统计并清除缓存的对话
- **服务器信息** — 服务器状态和版本信息

---

### 环境变量（备用方式）

在首次安装、无界面服务器或 Docker 部署时，你也可以通过环境变量进行配置。**设置界面始终优先** — 环境变量仅作为初始备用值。

```bash
cp .env.example .env
```

| 变量 | 说明 | 示例 |
|---|---|---|
| `LLM_API_KEY` | LLM 服务商 API 密钥（备用） | `sk-abc123...` |
| `LLM_BASE_URL` | Chat Completions 端点（备用） | `https://api.openai.com/v1` |
| `LLM_MODEL` | 默认模型（备用） | `gpt-4o` |
| `PORT` | 服务器端口 | `15000` |
| `BIND_HOST` | 绑定地址 | `0.0.0.0` |
| `PROXY_BYPASS_DOMAINS` | 逗号分隔的代理绕过域名 | `.corp.net,.internal.com` |
| `FEISHU_APP_ID` | 飞书机器人 App ID | `cli_xxxx` |
| `FEISHU_APP_SECRET` | 飞书机器人 App Secret | |

> 💡 **首次启动后，我们建议通过设置界面完成所有配置。**
> 它更直观、更改立即生效，并且支持服务商模板和模型自动发现等环境变量无法提供的功能。

---

## 项目结构

```
├── server.py                  Flask 应用入口，中间件，日志
├── bootstrap.py               自动依赖修复（LLM 引导）
├── index.html                 主聊天 UI（单页应用）
├── .env.example               环境变量模板
│
├── lib/                       核心库
│   ├── llm_client.py          LLM API 客户端（流式，重试）
│   ├── llm_dispatch/          多密钥多模型调度器
│   ├── database.py            PostgreSQL（自动初始化）
│   ├── tasks_pkg/             任务编排与压缩
│   │   ├── orchestrator.py    LLM ↔ 工具主循环
│   │   ├── executor.py        工具执行引擎
│   │   ├── endpoint.py        Planner → Worker → Critic 循环
│   │   └── compaction.py      3 层上下文压缩
│   ├── tools/                 工具定义与 schema
│   ├── swarm/                 多智能体编排
│   ├── fetch/                 内容抓取与提取
│   ├── search/                多引擎网页搜索
│   ├── browser/               浏览器插件桥接
│   ├── project_mod/           项目协作（扫描、编辑、撤销）
│   ├── skills/                技能积累系统
│   ├── feishu/                飞书机器人集成
│   └── ...
│
├── routes/                    Flask 蓝图（21 个模块）
├── static/                    CSS、JS、图标
├── browser_extension/         Chrome 插件（MV3）
├── tests/                     测试套件（单元、API、E2E）
└── data/                      运行时数据（已加入 .gitignore）
```

---

## 高级用法

### 项目协作

点击侧边栏中的 **Project**，输入任意代码库的路径。助手可以浏览文件、搜索代码、编辑文件、执行命令，并支持按轮次撤销修改。

### 多智能体集群

面对复杂任务时，助手会自动规划子任务并分发给多个并行运行的专家智能体。结果会经过审查并综合为连贯的最终输出。

### 浏览器插件

1. 打开 `chrome://extensions` → 启用开发者模式
2. 加载已解压的扩展程序 → 选择 `browser_extension/` 目录
3. 点击插件图标 → 输入你的服务器 URL
4. 助手现在可以阅读和操作你的浏览器标签页了

### 桌面代理

```bash
pip install pyautogui pillow psutil
python lib/desktop_agent.py --server http://your-server:15000 --allow-write --allow-exec
```

### 飞书机器人

1. 在 [open.feishu.cn](https://open.feishu.cn/app) 创建应用，启用机器人能力
2. 打开设置 → 🐦 飞书选项卡 → 输入 **App ID** 和 **App Secret**
3. 重启服务器后机器人自动连接

### 定时任务

告诉助手"创建一个定时任务"或"设置一个每日 cron 作业"— 它会创建一个按指定时间表运行的主动代理。可以通过状态栏的 SCHEDULER 徽章管理所有任务。

### 健康检查

```bash
python healthcheck.py
```

---

## 平台支持

Tofu 支持 **Linux**、**macOS** 和 **Windows** 运行。所有平台特定代码都隔离在 `lib/compat.py` 中。

| 功能 | Linux | macOS | Windows |
|---|:---:|:---:|:---:|
| 核心对话与工具 | ✅ | ✅ | ✅ |
| PostgreSQL 自动初始化 | ✅ | ✅ | ✅（PG bin/ 需在 PATH 中） |
| 项目协作（文件工具） | ✅ | ✅ | ✅ |
| `run_command`（基础） | ✅ | ✅ | ✅（使用 `cmd.exe`） |
| `run_command` 交互式标准输入 | ✅（通过 `/proc`） | ❌（非交互式） | ❌（非交互式） |
| FUSE 保活守护进程 | ✅（DolphinFS） | —（不需要） | —（不需要） |
| 桌面代理 | ✅ | ✅ | ✅ |
| 浏览器插件 | ✅ | ✅ | ✅ |
| 危险命令拦截 | ✅（Unix + Windows 模式） | ✅ | ✅ |

**烟雾测试**：`python debug/test_cross_platform.py` 可在任何平台验证兼容层。

---

## 测试

```bash
# 全部测试
python tests/run_all.py

# 单独测试套件
python -m pytest tests/test_backend_unit.py
python -m pytest tests/test_api_integration.py
python -m pytest tests/test_visual_e2e.py
python -m pytest tests/test_db_bug_regressions.py
```

---

## 安全

- **源码中无密钥** — 所有凭证从环境变量或设置界面加载
- **单用户模式** — 无多租户认证；请在 VPN 或反向代理后面部署
- **工具执行** — 助手可以运行 Shell 命令和编辑文件，请谨慎使用
- **桌面代理** — 需要显式启用 `--allow-write` / `--allow-exec` 标志

---

## 贡献

请参阅 [CONTRIBUTING.md](CONTRIBUTING.md) 获取完整指南。简要版：

1. Fork → 创建功能分支
2. `python healthcheck.py && python tests/run_all.py`
3. 提交 Pull Request

---

## 许可证

MIT
