<p align="center">
  <img src="static/icons/tofu-welcome.svg" width="140" height="160" alt="Tofu logo" /><br/>
  <img src="static/icons/tofu-brand-title.svg" width="280" height="78" alt="Tofu" /><br/>
  <sub>一套生产 Agent 内核：可嵌入、可独立服务，也可运行完整工作空间。</sub>
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="docs/DEVELOPER_RUNTIME.md">开发者运行时</a> ·
  <a href="docs/README.md">文档导航</a> ·
  <a href="CONTRIBUTING.md">开发指南</a>
</p>

# Tofu

Tofu 是一套完整的 Agent 运行时，包含模型路由、流式输出、网页搜索与抓取、MCP、
代码/项目工具、文档与媒体工具、重试、上下文压缩、取消和结构化事件。其他项目
现在不需要再 clone 整个仓库才能使用这些能力。

先选择适合你的接入边界：

| 把 Tofu 用作 | 安装方式 | 业务应用需要提供 | 状态 |
|---|---|---|---|
| Python 嵌入式运行时 | `pip install tofu-agent` | 一次配置 Provider，之后只传消息 | 进程内存 |
| 任意语言 Sidecar | `tofu-agent serve` 或 OCI 镜像 | Tofu URL / Token | 任务进程内存；Provider 加密文件 |
| 远程 Python/TypeScript 服务 | `tofu-sdk` / `@rangehow/tofu-sdk` | Tofu URL / Token | 由服务端决定 |
| 完整 AI 工作空间 | 安装器或源码部署 | 在设置或环境变量中配置 Provider | SQLite/PostgreSQL + 前端 |

前三种方式都不启动完整 Tofu 应用前端、不要求数据库，但执行的仍是完整应用使用的
同一套 `lib.tasks_pkg` Agent 内核，并不是另写的精简 Agent。Sidecar 只额外携带一个
很小的 `/setup` 静态控制面板，用来无代码配置默认模型。

## 60 秒启动 Tofu Agent

安装发布包并启动即可；首次启动不要求先准备任何 Provider 环境变量：

```bash
pip install tofu-agent
tofu-agent serve
```

打开 <http://127.0.0.1:15001/setup>，然后按界面完成四步：选择 OpenAI、
OpenRouter、DeepSeek、本地模型或自定义模板；填写 endpoint 和 key；发现并选择模型；
发送一次最小真实请求，测试通过后保存。配置会立即用于新任务，并在重启后自动恢复。

保存后，业务调用只需要消息：

```bash
curl http://127.0.0.1:15001/api/v1/agent/run \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"你好"}]}'
```

全新安装时 `tofu-agent doctor` 返回 `ready: false` 是正常的可配置状态，不是安装
失败；保存 Provider 后 readiness 会变为 200。

## 进程内嵌入

不需要 HTTP 边界的 Python 应用可以直接传 `ProviderConfig`，也可以由进程环境统一
注入 Provider：

```bash
export TOFU_AGENT_PROVIDER_BASE_URL=https://api.openai.com/v1
export TOFU_AGENT_PROVIDER_API_KEY=sk-...
export TOFU_AGENT_PROVIDER_MODEL=gpt-5.6
```

```python
from tofu_agent import AgentRuntime

with AgentRuntime.local() as agent:
    result = agent.run(
        [{"role": "user", "content": "调研这个问题并给出修复方案"}],
        config={"tools": ["search", "fetch"], "thinking": "high"},
    )

print(result.content)
print(result.usage)
```

Provider 只配置一次，后续调用可以不再传 endpoint、key 和 model。公开运行时同时
提供 `run_async`、`stream`、`stream_async`、`start`、事件回放、取消、自定义工具和
请求级 Provider 覆盖。如果业务希望完全不写 Provider 配置代码，使用上面的 Sidecar
和远程 SDK 即可。

本地 vLLM、Ollama、SGLang 等 OpenAI-compatible 服务可以留空 key：

```python
from tofu_agent import AgentRuntime, ProviderConfig

provider = ProviderConfig(
    base_url="http://127.0.0.1:8000/v1",
    api_key="",
    model="Qwen3.5-32B",
)

with AgentRuntime.local(provider=provider) as agent:
    print(agent.run([{"role": "user", "content": "你好"}]).content)
```

## 生产部署 Sidecar

CLI 会读取 `.env`、输出脱敏诊断，并启动无数据库 HTTP/SSE 服务：

```bash
tofu-agent doctor
tofu-agent serve                         # 默认只监听本机
```

远程监听默认拒绝匿名启动，必须先设置 Bearer Token：

```bash
TOFU_AGENT_HOST=0.0.0.0 \
TOFU_AGENT_TOKEN='replace-with-a-secret' \
tofu-agent serve
```

新服务器也可以不 clone 仓库，直接运行发布镜像：

```bash
docker run --rm --name tofu-agent \
  -p 127.0.0.1:15001:15001 \
  -e TOFU_AGENT_TOKEN='replace-with-a-secret' \
  -v tofu-agent-config:/home/tofu/.config/tofu-agent \
  ghcr.io/rangehow/tofu-agent:0.17.0
```

打开 `/setup` 并输入 Sidecar Token 即可完成模型配置。命名卷保存加密配置和它的
密钥，因此容器替换后仍能恢复。Agent 镜像只含 wheel、Agent 依赖和小型设置页，
不含源码 checkout、完整 Tofu 应用前端、应用数据、SQLAlchemy 或数据库驱动。

## 让 Tofu 完全托管模型

这是推荐的产品接入方式。默认由运维方在 `/setup` 里配置一次 endpoint、API key
和 model；所有下游业务之后只需要：

```text
TOFU_BASE_URL=https://tofu-agent.internal
TOFU_API_KEY=<sidecar bearer token>
```

无人值守部署仍可使用环境变量：

```text
TOFU_AGENT_PROVIDER_BASE_URL
TOFU_AGENT_PROVIDER_API_KEY
TOFU_AGENT_PROVIDER_MODEL
```

命令行参数优先于环境变量，环境变量优先于 `/setup` 保存的配置。环境变量或命令行
接管 Provider 时，设置页会明确显示只读，避免界面看似保存成功却被启动配置覆盖。
业务代码不必传模型名；运维方可以升级或替换默认模型，无需重新部署所有调用方。

默认配置保存在 `~/.config/tofu-agent/provider.json`（可用
`TOFU_AGENT_CONFIG_PATH` 修改）。API key 与自定义 header 值通过 Fernet 加密，
页面和 API 永不回传明文；默认加密密钥是相邻的 `.provider.json.key`，也可通过
`TOFU_AGENT_CONFIG_KEY` 注入。迁移服务器时应同时迁移配置文件与密钥，或在新机器
重新保存一次。

如果某个调用方必须自带模型，也只需在单次请求中传三个字段；`endpoint` 是
`base_url` 的友好别名：

```json
{
  "messages": [{"role": "user", "content": "你好"}],
  "provider": {
    "endpoint": "https://models.example/v1",
    "api_key": "sk-...",
    "model": "model-name"
  }
}
```

Provider 密钥使用默认脱敏的数据类型，不会出现在结果或 capabilities 中；请求级
一次性 Provider 会在任务终态时释放。

## 使用远程 SDK

Python 同步/异步客户端：

```bash
pip install tofu-sdk
```

```python
from tofu_sdk import AsyncTofu

async with AsyncTofu(
    base_url="https://tofu-agent.internal",
    api_key="sidecar-token",
) as tofu:
    result = await tofu.agents.run(
        messages=[{"role": "user", "content": "总结这个仓库"}],
        config={"tools": ["search", "fetch"]},
    )
    print(result["content"])
```

TypeScript/JavaScript（Node 18+、浏览器、Worker、Deno、Bun）：

```bash
npm install @rangehow/tofu-sdk
```

```ts
import { Tofu } from '@rangehow/tofu-sdk';

const tofu = new Tofu({
  baseUrl: 'https://tofu-agent.internal',
  apiKey: 'sidecar-token',
});

const result = await tofu.agents.run({
  messages: [{ role: 'user', content: '总结这个仓库' }],
  config: { tools: ['search', 'fetch'] },
});
console.log(result.content);
```

两套 SDK 都会为自动重试生成稳定的幂等键。`agents.start` 立即返回 HTTP 202 task
handle；`agents.stream` 只提交一次，网络断开后从最后一个绝对事件序号继续，不会
重复执行工具副作用。

## Headless 状态保证

轻量运行时的状态契约刻意保持清楚：

- Run、幂等记录和回放事件保存在有界进程内存中。
- 进程存活期间，网络断线后可以继续回放。
- 进程重启后不保留，而且绝不会暗中创建数据库。
- `/setup` 保存的默认 Provider 是唯一刻意持久化的 Sidecar 配置；它使用独立的加密
  文件，不会把任务、消息或对话带入数据库。
- 因此持久记忆、知识库、跨会话状态和长期调度归完整应用所有；联网、项目文件、
  MCP、自定义工具、媒体工具和进程内编排仍可直接使用。
- 需要持久对话、跨进程 worker、计费、账号或长期调度时，使用完整应用。

`GET /api/v1/capabilities` 会返回当前安装的精确能力与状态语义。请求、事件和迁移
说明见[开发者运行时](docs/DEVELOPER_RUNTIME.md)及
[Headless API](docs/HEADLESS_API.md)。

## 运行完整工作空间

完整产品在 Agent 内核之上增加对话、浏览器 UI、持久存储、账号、论文/媒体库、
定时任务与运维控制。

| 平台 | 启动方式 |
|---|---|
| Windows | 从[最新 Release](https://github.com/rangehow/ToFu/releases/latest)下载 `Tofu-Setup-*-win64.exe`。 |
| macOS | 从[最新 Release](https://github.com/rangehow/ToFu/releases/latest)下载 `Tofu-<ver>-macos-arm64.dmg` 或 `Tofu-<ver>-macos-x86_64.dmg`，或使用 `install.sh`。 |
| Linux | `curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh \| bash` |
| Android | 从 [Android Releases](https://github.com/rangehow/tofu-android/releases/latest/download/tofu-android.apk) 安装 `tofu-android.apk`。 |
| 源码 / 完整 Docker | `git clone https://github.com/rangehow/ToFu.git && cd ToFu && docker compose up -d` |

打开 <http://localhost:15000>，在**设置 → 服务商**中添加模型；无人值守部署也可用
原有的 `LLM_BASE_URL`、`LLM_API_KEYS` 和 `LLM_MODEL`。SQLite 是个人部署默认值，
PostgreSQL 是同一存储契约后的显式分布式选项。首次登录后请安装下方的
浏览器插件——所有依赖浏览器的能力都由它提供。

## 浏览器插件

首次登录后安装一次配套浏览器插件。它是让 Agent 进入你真实浏览器的桥梁，
而不是一个空白、未登录的抓取器；以下能力只有装了插件才能用：

- **浏览器控制**：Agent 租用你浏览器里的真实标签页，完成导航、阅读、滚动、
  点击和填表。
- **需要登录和验证的页面**：任务在你已登录的会话里执行，拦截服务端抓取的
  页面——登录墙、验证码或反爬检查、付费墙、仅内网可达的页面——仍然可用。
- **Web 调试证据**：DevTools Bridge、console 读取、网络抓包和截图，让 Agent
  直接拿到被测页面的证据，提高调试能力的边界。
- **依赖 Cookie 的文件传输**：需要浏览器 Cookie 才能访问的 URL，由插件流式
  写入服务器的有界暂存区。

同一份未打包插件同时支持 Chrome 和 Edge。请从
**设置 → 本机控制**进入统一的安装或升级流程；当 Tofu 与浏览器在
同一台机器上时，界面会识别可用浏览器并打开它自己的扩展管理页。

本机手动安装时，在 Chrome 中打开 `chrome://extensions`，或在 Edge 中打开
`edge://extensions`，开启「开发者模式」，再把仓库的 `browser_extension/`
目录作为已解压扩展加载。如果浏览器与 Tofu 不在同一台机器上，请改用
**设置 → 本机控制 → 下载扩展 ZIP**；远程浏览器无法读取服务器文件系统。

## 开发与发布

要求 Python 3.12 和 Node 20.19.x：

```bash
uv sync --frozen --extra dev
uv run make docs-check
npm run check:frontend
uv run make test-unit
```

`vX.Y.Z` tag 会统一构建并校验 `tofu-agent`、`tofu-sdk`、
`@rangehow/tofu-sdk` 和多架构 `tofu-agent` OCI 镜像，然后再发布。四者版本与
[`VERSION`](VERSION) 保持一致。

[文档导航](docs/README.md)给出了每个子系统的当前契约和 owner；仓库开发规则见
[AGENTS.md](AGENTS.md)。

## 项目结构

以下是可分发 Agent 运行时及其接入边界的首跳导航；完整子系统目录见
`docs/README.md`。

```text
├── tofu_agent/
│   ├── __init__.py
│   ├── cli.py
│   ├── models.py
│   ├── runtime.py
│   ├── server.py
│   ├── provider_setup.py
│   ├── provider_store.py
│   └── setup_ui/
│       ├── index.html
│       ├── setup.css
│       └── setup.js
├── clients/
│   ├── python/
│   │   └── tofu_sdk/
│   └── typescript/
│       └── src/
├── lib/
│   ├── llm/
│   ├── llm_dispatch/
│   ├── tasks_pkg/
│   │   ├── compaction/
│   │   ├── handlers/
│   │   └── orchestrator/
│   ├── byo_egress.py
│   └── provider_probe.py
├── docs/
│   ├── README.md
│   ├── DEVELOPER_RUNTIME.md
│   ├── HEADLESS_API.md
│   └── API_CONTRACT.md
├── tests/
│   ├── test_agent_provider_setup.py
│   ├── test_headless_agent_server.py
│   └── test_public_agent_runtime.py
├── scripts/
│   └── check_developer_runtime_artifacts.py
├── frontend/
├── routes/
├── server.py
├── serverctl.py
├── Dockerfile
├── pyproject.toml
└── MANIFEST.in
```

## 安全

- 无 Token 的 headless 模式只接受 loopback；CLI 会拒绝匿名的非本机监听。
- `/setup` 页面资源可以公开加载，但它读取、测试或修改 Provider 的 API 与 Agent
  API 使用同一 Bearer Token；跨站设置请求会被拒绝。
- 完整应用在统一中间件边界处理认证授权，并向 repository 传递显式 owner 身份。
- Provider endpoint 受统一出站访问策略校验。
- 不要把 API key 写进命令行参数或提交到仓库的环境文件；备份加密 Provider 时要把
  配置文件和密钥作为同一份机密处理。

详见 [API 契约](docs/API_CONTRACT.md)、[身份契约](docs/IDENTITY.md)和
[可靠性手册](docs/RELIABILITY_RUNBOOK.md)。

## 许可证

MIT，见 [LICENSE](LICENSE)。
