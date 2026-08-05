# Desktop Egress — 订阅流量的客户端出口设计

> Epic: `pt_4ea6bf05deaa46f0`（owner 2026-07-31 拍板：方案 2 收敛为「desktop_agent
> 新增 egress 命令」，不新写伴随代理、不新设 WS 隧道协议）。
> 调研档案：memory `cliproxyapi_订阅机制与_tofu_oauth_失败根因`；
> 参照实现：`../CLIProxyAPI`（router-for-me/CLIProxyAPI v7，已克隆到 chatui 同级）。

---

## 1. 背景与根因

### 1.1 失败链（2026-07-31 在 Tofu 容器内实测）

| 层 | 现象 | 实测证据 |
|---|---|---|
| 直连 | 不可能 | `curl --noproxy '*'` → `Could not resolve host`（容器无外网 DNS/出口） |
| 公司代理 | 应用层封锁 | 5 个端点（api.anthropic.com / console.anthropic.com / claude.ai /
auth.openai.com / chatgpt.com）全部 HTTP 403 `{"error":{"type":"forbidden","message":"Request not allowed"}}` |

由此产生**三层失败链**：

1. **登录交换**：服务器侧 token exchange 403 → 已有 B1（浏览器交换）兜底，但依赖
   token 端点 CORS 且实际掉进了 curl 手工兜底（见会话 ms8wzczegia0jq）。
2. **token 刷新**：Claude access token 约 8h 过期，刷新走服务器 → 403 →
   **登录成功 8 小时后必死**。此前完全未修。
3. **API 调用**：即使 token 新鲜，`/v1/messages`、`/backend-api/codex/responses`
   照样 403。**订阅登录成功也无法聊天**。

### 1.2 CLIProxyAPI 参照结论（只列影响设计的）

- Codex 订阅计划从 id_token JWT 的 `https://api.openai.com/auth` claim 解出
  （`chatgpt_plan_type`、`chatgpt_account_id`），驱动分 plan 模型清单；
  **Tofu 的 `_parse_jwt_claims` 未提取 plan_type**。
- 「订阅配额查询」API 不存在；CLIProxyAPI 只做自身 token 统计 + 429 冷却。
- 请求伪装已到 2026 军备竞赛水平（计费头 / 9 个 beta / 完整系统提示 / 工具改名 /
  uTLS Chrome 指纹）。**Tofu `lib/oauth/outbound.py` 全面落后——即使网络通了，
  不补齐这层仍会被风控挡。** 详细参数见 §5。
- Codex 侧 `originator: codex_cli_rs` + 配套 UA + `chatgpt-account-id` 三件套
  Tofu 已对齐，不需要动。

### 1.3 为什么复用 desktop_agent 而不是新写伴随代理

owner 裁定：`lib/desktop_agent` + `lib/desktop/bridge.py` 已是生产级出站桥：

- 方向客户端→服务器（`/api/desktop/poll` 长轮询），NAT 天然安全；
- 已有 v2 注册帧（agent_id + capabilities + **user_id 绑定**，RWA P0/P4a），
  `_deliverable` 租户隔离是 fail-closed 的；
- 已有**流式回传帧通道**（RWA P2：`stream_outbox` 的
  `{cmd_id, seq, stream, data, done}` → 服务器 `resolve_streams` 按 seq 去重拼帧），
  正是中继 SSE 所需的多路复用帧层。

新协议是重复造轮子。本设计只新增**命令类型**与**服务器路由层**。

---

## 2. 目标 / 非目标

### 2.1 目标

1. Claude Pro/Max 与 ChatGPT(Codex) 订阅在「服务器无可用出口」环境下全链路可用：
   登录交换、token 刷新、聊天调用、provider 探测。
2. 出口路由对上层透明：`lib/oauth/*` 与 `lib/llm/*` 的调用方不感知走的是
   直连还是桌面代理。
3. CLIProxyAPI 2026 cloaking 规格移植到 `lib/oauth/outbound.py`（与网络无关，
   任何出口下都必须正确）。
4. Codex 按 `chatgpt_plan_type` 门控模型清单。

### 2.2 非目标

- 不做浏览器 WS 中继（CORS 对 chatgpt.com 不成立，且 desktop_agent 路径严格更强）。
- 不做 CLIProxyAPI 的 429 多凭证冷却/切换体系（Tofu 已有自己的 dispatcher 回退）。
- 不改写 Codex 请求/响应翻译（`codex_translate_request` / SSE 翻译已在位）。
- 不动 v1（无注册帧）agent 的既有行为。

---

## 3. 总体架构

```
lib/oauth/claude.py / codex.py          lib/llm/stream.py (SSE transport)
        │ token 交换 / 刷新                      │ 订阅模型的聊天调用
        ▼                                        ▼
┌───────────────────────────────────────────────────────────┐
│           lib/desktop/egress.py  （新，路由层）            │
│  route_request(): 直连探测(带缓存) → 直连可用?             │
│     ├─ 是 → 返回 None（调用方走原有 http 路径，零改动）     │
│     └─ 否 → 选 agent（user_id 作用域）→ bridge 命令        │
└───────────────────────────────────────────────────────────┘
        │ send_desktop_command('egress_http' / 'egress_http_stream')
        ▼
  lib/desktop/bridge.py（已有：队列/用户作用域/流帧拼帧）
        │ POST /api/desktop/poll（长轮询，客户端→服务器）
        ▼
  lib/desktop_agent/_egress.py（新，agent 侧执行器）
        │ requests / curl_cffi，走用户本机代理环境
        ▼
  api.anthropic.com / chatgpt.com / auth.*.com（域名白名单内）
```

**一个进程，两个改动面**：agent 侧加 2 个命令；服务器侧加 1 个路由模块 +
bridge 的 TTL 扩展。其余全是既有接缝。

---

## 4. 协议设计

### 4.1 `egress_http` —— 一次性请求（token 交换/刷新、非流式调用）

**命令 params**（服务器 → agent）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `url` | str | 完整 URL；host 必须在白名单（§7.1），否则服务器入队前即拒绝 |
| `method` | str | `GET` / `POST` |
| `headers` | dict | 请求头（含 Authorization；agent 日志必须脱敏，§7.4） |
| `body_b64` | str | 请求体 base64（JSON/表单统一编码，避免二次序列化歧义） |
| `timeout_ms` | int | 上限钳到 60_000 |
| `proxy_mode` | str | `env`（默认，agent 进程环境代理）/ `direct` / `auto`（env 失败回落 direct） |

**结果 result**（agent → 服务器，一次性走 `results` 通道）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | int | HTTP 状态码；网络错误时 0 且 `error` 非空 |
| `headers` | dict | 响应头（set-cookie 剥离） |
| `body_b64` | str | 响应体 base64，上限 10 MB（超限报错不截断——截断的 JSON 无法解析） |
| `elapsed_ms` | int | 耗时 |
| `error` | str | 网络层错误描述（DNS/连接重置/超时），HTTP 错误不算 error |

### 4.2 `egress_http_stream` —— 流式请求（SSE 聊天调用）

**命令 params** 同 4.1，另加：

| 字段 | 类型 | 说明 |
|---|---|---|
| `stream_id` | str | = cmd_id（服务器生成命令时显式传入，消费端按它读帧） |

**帧协议**（复用 RWA P2 `stream_outbox` 通道，`{cmd_id, seq, stream, data, done}`）：

| seq 位置 | stream | data | 语义 |
|---|---|---|---|
| 首帧 | `meta` | JSON `{status, headers, error?}` | 响应头到达即发；非 2xx 时此处给出 status，body 帧带错误详情 |
| 中间帧 | `body` | base64 字节块（≤ 64 KB/帧） | SSE 原始字节流，**不按行切**（行重组在服务器消费端） |
| 错误帧 | `error` | JSON `{message, where}` | 网络中断等；帧流就此终止（done=true） |
| 末帧 | `meta` | `''` + `done=true` | 正常结束 |

**服务器消费端**（新，`lib/desktop/egress.py::consume_stream`）：
按 `get_command_stream(cmd_id, since_seq=N)` 增量轮询（200ms 间隔），
base64 解码后喂给既有 SSE 行解析器。`get_command_stream` 目前只按
stdout/stderr 分通道拼帧——**需要扩展为保留帧列表**（见 §6.3 bridge 改动），
或新增 `get_frames(cmd_id, since_seq)` 返回 `[(seq, stream, data)]`，
**不改既有 stdout/stderr 契约**（RWA 消费端不受影响）。

**最终 result** 仍在流结束后到达（`{status, elapsed_ms, bytes}` 统计），
`send_desktop_command` 的阻塞语义不用于流式——路由层用「入队即返 cmd_id +
轮询帧 + 等 done」的消费循环。

**`egress_cancel` —— 中途取消通道（owner S3 补充①，不做则「可停止」在
egress 路径上是假的）**：bridge 原本没有取消概念，用户 Stop 后服务器
消费端停读，但 agent 会继续烧订阅配额拉到自然结束。协议：消费端中止时
（abort/看门狗/异常）入队 `egress_cancel {cmd_id}`（同一 bridge 通道、同一
agent 寻址）；agent 侧维护在飞流注册表 `cmd_id → 运行句柄`，收到取消即
关闭对应 requests session 中断上游。取消是 best-effort：命令可能已完成
（注册表无记录 → 静默忽略），取消帧可能与尾部帧乱序（agent 先记取消标记，
后到的该 cmd 帧一律丢弃）。

### 4.3 延迟与 TTL 预算

- 命令下行：长轮询即时（`take_pending_commands_async`，秒级内）。
- 帧上行（owner 复核修正）：**裸账不是 ≤1s，是 8~9s 一簇**——agent 的 poll
  POST（客户端超时 15s）被服务器 `take_pending_commands_async` 在无新命令时
  长轮询挂住最多 8s（`POLL_WAIT_TIMEOUT`），流帧只能等下一班 poll 上传，
  用户会看到 token 每 8 秒吐一段。**修法（§6.3 第 3 条）**：服务器在
  `desktop_poll` 发现当次 poll body 携带了 streams/results（agent 正在干活）
  时跳过异步长等待立即返回，loop 收紧到 `poll_interval`（~1s）。此改动
  对旧 agent 兼容（只是返回更快），且空闲时（无帧无结果）仍走 8s 长轮询，
  心跳成本不变。rush_ms（agent 流活跃期自缩间隔）留作后续可选优化。
- **`_COMMAND_TTL_S = 90s` 是真约束**：LLM 流可跑 30+ 分钟，90s 即被
  stale cleanup 杀掉。**bridge 必须支持按命令类型覆盖 TTL**：
  `egress_http_stream` TTL = 1800s，`egress_http` TTL = 120s，其余命令保持 90s。
- agent 侧流执行必须像 `_start_project_run_streamed` 一样**脱离 poll 循环线程**，
  否则心跳停摆、15s 连接窗过后被判定离线。
- **半开流看门狗（owner 复核补充）**：agent 中途死掉时消费端不能干等 1800s
  TTL。`consume_stream` 轮询循环内：距上一新帧超过 30s **且**该 agent 已不在
  `online_agents()` → 主动以 `EgressUnavailable` 断开（错误语义同中途断流），
  让 dispatcher 走模型回退，而不是挂半小时。
- **流帧清扫语义（owner S3 补充②，不做则长 thinking 的流被误杀）**：
  `_sweep_streams_locked` 原来一律按全局 `_COMMAND_TTL_S=90s` 清帧，而 LLM 的
  thinking 块可能连续几分钟无帧——静默期超 90s 帧条目被扫掉，流悄悄死透。
  定死两条：(a) 清扫尊重命令自身 ttl（`egress_http_stream` 1800s，取
  `command_queue[cmd_id].ttl` 兜底全局值）；(b) 消费端把「done=false 但帧条目
  已消失」判为流异常终止（EgressUnavailable），绝不继续干等。

---

## 5. Cloaking 移植规格（`lib/oauth/outbound.py`，与出口路径无关）

逐条来自 CLIProxyAPI v7 源码（`internal/runtime/executor/claude_executor_*.go`），
按优先级排序。★ = 不做则 2026 风控下大概率失败。

### 5.1 Claude（OAuth token 走 `/v1/messages`）

| # | 项 | 规格 | 优先级 |
|---|---|---|---|
| 1 | 计费头 ★ | `system[0]` 注入文本块：`x-anthropic-billing-header: cc_version=<V>.<FP>; cc_entrypoint=cli; cch=<CCH>;`。<br>`V` = `2.1.63`（对齐 CLIProxyAPI `DefaultClaudeVersion`）。<br>`FP` = `sha256("59cf53e54c78" + t[4] + t[7] + t[20] + V)` 的 hex 前 3 字符；`t` = 原第一个 system 文本（不足位按 rune 补 `'0'`）。<br>`CCH`：**OAuth token 走签名分支，固定 `cch=00000`**（CLIProxyAPI `useCCHSigning := oauthToken || …`）；payload-hash 分支（`sha256(完整请求体)` hex 前 5 字符）只用于 API-key cloaking，我们不是那条路。 | P0 |
| 2 | 系统提示 ★ | `system[1]` = `"You are Claude Code, Anthropic's official CLI for Claude."`（已有）；<br>`system[2]` = Claude Code 静态提示（`ClaudeCodeIntro + System + DoingTasks + ToneAndStyle + OutputEfficiency`，从 CLIProxyAPI `helps/claude_system_prompt.go` 逐字拷贝）；<br>**用户自己的 system 内容挪进第一条 user 消息**（`<system-reminder>` 包裹），不再留在 `system[]` —— 第三方指纹来源。 | P0 |
| 3 | beta 旗标 ★ | `anthropic-beta` 全量：`claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,context-management-2025-06-27,prompt-caching-scope-2026-01-05,structured-outputs-2025-12-15,fast-mode-2026-02-01,redact-thinking-2026-02-12,token-efficient-tools-2026-03-28`（调用方额外 beta 追加在后） | P0 |
| 4 | 工具名 ★ | 14 项小写→TitleCase 映射（`bash→Bash, read→Read, write→Write, edit→Edit, glob→Glob, grep→Grep, task→Task, webfetch→WebFetch, todowrite→TodoWrite, question→Question, skill→Skill, ls→LS, todoread→TodoRead, notebookedit→NotebookEdit`），作用于 `tools[].name`/`tool_choice`/`tool_use`/`tool_reference`；**响应侧按每请求 reverseMap 反向恢复**（CLIProxyAPI 注释：全局反映射会误伤本来就是 TitleCase 的名字）。**注意：Tofu 自有工具名（read_files 等）不在映射表内——需要实测 Anthropic 是否因未知工具名拒绝/标记；若拒绝，策略是把 Tofu 工具名映射到最近似的官方名**（实现时验证，见 §9 开放问题 O2）。 | P0 |
| 5 | 头部套件 | `x-app: cli`、`X-Stainless-Retry-Count: 0`、`X-Stainless-Runtime: node`、`X-Stainless-Lang: js`、`X-Stainless-Timeout: 600`、`X-Claude-Code-Session-Id`（每 token 稳定的 uuid，进程内缓存）、`x-client-request-id`（每请求新 uuid）、`User-Agent: claude-cli/2.1.63 (external, cli)` | P1 |
| 6 | metadata.user_id | 缺失或非法时注入伪造 id（`user_<hex>_account__session_<hex>` 形态，对齐 CLIProxyAPI `generateFakeUserID`） | P1 |
| 7 | 采样规整 | 删 `temperature`/`top_p`；thinking 开启时删 `top_p`/`top_k`；`tool_choice` 为 any/tool 时删 `thinking`（CLIProxyAPI `normalizeClaudeSamplingForUpstream` 等） | P2 |

**认证头样式（开放问题 O1，实现时用真 token 实测定案）**：
Tofu 现注释「Bearer 于 2026 起 401，token 必须走 x-api-key」；
CLIProxyAPI 对 OAuth 文件凭证仍用 `Authorization: Bearer`。两者矛盾，
可能差异在计费头/beta 是否齐全。实现时保持可切换（配置位），默认维持
x-api-key，实测后收敛并删掉另一路。

### 5.2 Codex（已对齐，仅补 plan_type）

- `_parse_jwt_claims` 增提取 `chatgpt_plan_type` → 存入 token 记录。
- `provision_oauth_provider('codex')` 按 plan 选模型表：
  `pro` → 全量 codex 模型；`plus`/`team`/`business` → 对应子集；
  `free` → free 子集；未知 → 按 pro（对齐 CLIProxyAPI default 分支）。
  模型表数据从 CLIProxyAPI `internal/registry` 的 models.json 抄录为 Tofu 常量，
  注明来源与同步日期。
- token 刷新时 plan 变化 → 重新 provision（幂等，已有机制）。

---

## 6. 服务器侧改动清单

### 6.1 新模块 `lib/desktop/egress.py`（路由层）

**user_id 全链下穿（owner S2 补充③）**：`resolve_oauth_request` 加可选
`user_id=''` 形参，沿 `get_valid_token → refresh → egress 路由` 全链下穿；
遗留单用户部署（`user_id=''`）回落 legacy 语义——空 user_id 时允许任意一台
egress-capable agent 服务（与 bridge v1/单用户世界一致），多用户部署才强制
同租户。

```
route_request(url, *, user_id)  -> 'direct' | EgressTarget | None(不可用)
  - 直连探测：host 级缓存（TTL 300s），5s 超时，**POST 真实端点（不带
    auth、空 body）**——按状态码三态分类：
    ok（401/400/404/405，到达了应用层）/ geo_blocked（403，区域封锁特征）/
    network_fail（DNS/连接异常）。比 GET 根路径可靠：根路径 403 可能是
    WAF 噪音，而 401 是「应用活着只是没认证」的硬证据
  - geo_blocked 或 network_fail → 选 agent：
      online_agents() 过滤 user_id 且 capabilities.egress=True
      0 个 → None（路由层错误，带引导文案）
      1 个 → unaddressed（bridge 单 agent 回落语义）
      ≥2 个 → 读用户设置 oauth_egress_agent_id；未设/不在线 → None（引导去设置页选）
  - direct → None 由调用方走原路径（零侵入）

egress_http(url, method, headers, body, timeout, user_id) -> EgressResult
  白名单校验 → 入队（TTL 120s）→ 等结果 → 解 base64

egress_http_stream(url, ..., user_id) -> 迭代器(字节块) + 首帧 meta
  白名单校验 → 入队（TTL 1800s）→ 轮询 get_frames → 解码产出
```

**域名白名单（服务器入队前校验，agent 执行前再校验一次，双重强制）**：
`api.anthropic.com`、`console.anthropic.com`、`claude.ai`、
`auth.openai.com`、`auth0.openai.com`、`chatgpt.com`、`api.openai.com`。
精确 host 匹配（不后缀匹配，防 `api.anthropic.com.evil.com`）。

### 6.2 接入点

| # | 位置 | 改法 |
|---|---|---|
| A1 | `lib/oauth/claude.py::{claude_exchange_code, claude_refresh_token}`、`codex.py` 同名 | `http_post` 调用点前先 `route_request`；非 direct 则 `egress_http`，响应适配成 requests.Response 形态（或重构为统一返回 (status, json)） |

**A1 旁挂（owner S2 补充①，不做会被生产咬）刷新 singleflight**：OpenAI 的
refresh token 是一次性的。egress 让刷新 RTT 从 ~300ms 涨到 1~2s+，两个并发
请求同时看到过期、同时经 agent 刷新的窗口被放大 4~6 倍——第二个刷新会烧掉
第一个刚换的 refresh_token → `refresh_token_reused` → 订阅被强制登出。
CLIProxyAPI 的 `codexRefreshGroup singleflight.Group`（+ Claude 侧
blocked-until）就是为这个场景存在的。S2 必须给 claude/codex 刷新加进程内
singleflight：同一 refresh_token 的并发刷新合并为一次（后到者等同一份结果），
不是可选加固。
| A2 | `lib/llm/stream.py` + `lib/llm/astream.py`（SSE 传输）、`lib/llm/chat.py`（非流式） | `RequestPlan` 增加 `egress` 标记；transport 分支：egress 时消费帧流喂给 `_sse_core` 既有行解析（解析器零改动）；非流式 `chat()` 走 `egress_http` 一次性往返 |

**A2 禁忌（owner S2 补充②）**：`resolve_oauth_request → get_valid_token →
refresh → egress_http` 是同步阻塞链，egress_http 要等 agent 一个 RTT（最坏
TTL 120s）。sync stream.py 跑在线程池里无碍，但 **`astream.py` 的异步路径上
任何 egress 阻塞调用都必须 `asyncio.to_thread` 包裹**——否则一次刷新就能
冻住 Quart 事件循环、全站请求停摆。写死，不接受「实测应该碰不到」。
| A3 | `lib/provider_probe.py` | 同 A1 的路由包装 |
| A4 | 前端 OAuth 卡片 + 状态接口 | 见下方三点 S4 修订（owner 拍板） |

**A4 三点修订（owner S4 拍板）：**
1. **状态接口绝不同步探测**：页面加载路径上只读探测缓存（300s TTL 内的既有
   判决），无缓存返回 `unknown` 并**异步**触发后台探测；「重新检测」按钮才走
   同步探测（用户已预期等待）。否则设置页打开会卡最坏 5s 白屏。另加一个可辨
   状态：`agent_present_but_capability_off`（agent 在线但
   `capabilities.egress=false`）——这是默认态，卡片须明说「重启 agent 加
   `--allow-egress`」。
2. **Codex 探测从 SKIPPED 升级为流式真探测**：S3 的 `open_stream` 让
   `chatgpt.com/backend-api/codex/responses` 的 1-token 请求成为可能（请求体
   过 `codex_translate_request` 翻译），按状态码分类——全链路
   （cloaking+翻译+egress）端到端验证。
3. **选择器端点**：`GET/POST /api/v1/oauth/egress-agent` 读写
   `data/config/oauth_egress_agents.json`（即 `_pinned_agent` 读的那份，
   键 user_id），不另起存储。

**登录链路顺序（owner 拍板，定死）**：`_completeLogin` 的交换优先级改为
**① 服务器交换（内部自动路由 直连/经 agent）→ ② B1 浏览器交换 → ③ curl 手工兜底**。
理由：egress agent 在线时服务器交换最鲁棒（无 CORS 问题）；B1 依赖浏览器所在
机器自己有 VPN。无 egress agent 在线时 OAuth 卡片退化为现状 B1 行为
（浏览器交换优先），UI 不新增任何新概念。

**回退策略**：egress 执行失败（agent 掉线/超时/网络错）→ 立刻重探测直连
（可能网络环境已变）→ 仍不可用则抛 `EgressUnavailable`（独立异常类型，
dispatcher 按 provider-down 处理走模型回退，不记为模型限流）。
token 刷新 egress 失败 → token 标记 stale，OAuth 卡片提示「启动桌面代理」，
**不删除** refresh_token。

### 6.3 `lib/desktop/bridge.py` 改动

1. **按类型 TTL**：`send_desktop_command(..., ttl=None)` 参数，默认 90s；
   `take_pending_commands` 过期判定用 `cmd.get('ttl', _COMMAND_TTL_S)`。
2. **帧读取 API**：新增 `get_frames(cmd_id, since_seq) -> [(seq, stream, data, done)]`，
   `get_command_stream` 保持原契约（内部改调 get_frames 重组）。
3. **干活中的 poll 不挂长等待**：`routes/desktop.py::desktop_poll` 在
   `body.get('streams') or body.get('results')` 非空时改调同步
   `take_pending_commands`（立即返回），空 body 才走
   `take_pending_commands_async`（8s 长轮询）。这是 §4.3 帧延迟修正的
   落点；对旧 agent 零协议变化。
4. 无其他改动：用户作用域、注册帧、拼帧去重全部复用。

### 6.4 agent 侧改动

1. 新文件 `lib/desktop_agent/_egress.py`：
   - `cmd_egress_http(params)` — 白名单校验 → `requests.request`（`proxy_mode`
     处理）→ base64 结果。日志只记 `method host status elapsed`，**绝不记
     headers/body**。
   - `cmd_egress_http_stream(params, on_chunk, on_exit)` — 流式读取，
     64KB 切帧；meta 首帧在收到响应头时立刻发。
   - dispatch 注册 + **新权限位 `--allow-egress`**（默认关；capability
     上报 `egress: true/false`，路由层据此过滤 agent）。
2. **OS 代理发现（关键，决定方案在真实用户机器上是否生效）**：用户场景的
   客户端代理多为 Clash「系统代理」——它写 Windows 注册表/macOS 系统设置，
   **不会变成 `HTTPS_PROXY` 环境变量，Python requests 默认读不到**。
   `proxy_mode=env` 的发现顺序因此必须是：进程 env → OS 系统代理
   （Windows 读注册表 `HKCU\\...\\Internet Settings` 的 ProxyEnable/ProxyServer；
   macOS 调 `scutil --proxy`）→ direct。没有这一步，agent 在用户机器上
   依然裸连被拒，整个方案空转。
3. TLS 指纹（开放问题 O3）：可选依赖 `curl_cffi`（`impersonate="chrome"`），
   装了就用、没装回落 `requests` 并 warn。chatgpt.com 的 Cloudflare 对裸
   Python TLS 可能 1010——Codex 用户强烈建议安装。agent 打包流程
   （desktop/ 目录的 PyInstaller spec）把 curl_cffi 列入。

---

## 7. 安全边界

### 7.1 双重域名白名单
服务器入队前 + agent 执行前各验一次（精确 host 匹配）。agent 侧清单硬编码，
不信任服务器下发的任何「额外放行」字段。

### 7.2 用户绑定
egress 命令必带 `user_id`。注意 OAuth token 本身是**全局存储**
（`data/config/oauth/<provider>.json`，不属任何用户），所以 user_id 取自
**调用方会话上下文**（登录操作者 / 会话属主），不是 token 记录——谁的
会话就用谁的 agent 出口，订阅本身是部署级共享的（与现状一致）。
bridge `_deliverable` 的租户隔离已 fail-closed：租户 A 的 agent 永远收不到
租户 B 的 egress 命令。多 agent 必须显式 `target_agent_id`（设置页选择），
拒绝「随便挑一台」。

### 7.3 权限与限额
`--allow-egress` 默认关（opt-in）；请求体 ≤ 2 MB；非流式响应 ≤ 10 MB；
帧 ≤ 64 KB；单命令 TTL 上限 1800s；agent 侧单 host 并发 ≤ 4。

### 7.4 脱敏
请求经手 access_token（Authorization/x-api-key 头）：agent 日志只记
`method host status elapsed`；服务器路由层同样不落 body。bridge token
（agents:bridge scope）泄露 ≠ 订阅 token 泄露（egress 命令本身不含 token，
token 只在请求的 headers 里由服务器注入）。

---

## 8. 分片实施计划（每片带守卫测试）

| 片 | 内容 | 可独立验收 |
|---|---|---|
| S1 | §5 cloaking 移植（outbound.py）+ codex plan_type | 纯离线：单测断言请求体/头部形状（计费头指纹算法逐字节对拍 CLIProxyAPI 测试向量）；plan_type 解析与门控 |
| S2 | `egress_http` + 路由层 + OAuth 交换/刷新接入（A1）+ **登录顺序翻转**（§6.2 A4 的 `_completeLogin` 改「服务器交换→B1→curl」，JS 改动需 bundle 重建）+ 刷新 singleflight + user_id 下穿 | fake bridge（内存队列驱动假 agent）端到端：403 直连 → 走 agent → token 落库；白名单拒绝；TTL/超时；并发刷新合并 |
| S3 | `egress_http_stream` + LLM 传输分支（A2）+ bridge TTL/get_frames + `egress_cancel` + 流帧清扫语义 | fake 帧流 → SSE 解析输出与直连路径逐字节一致；30min TTL；断流错误传播；Stop 后 agent 不再烧配额；长 thinking（>90s 无帧）不被误杀 |
| S4 | provider_probe（A3）+ 前端卡片/选择器（A4） | 探测经 agent；卡片三态；多 agent 选择持久化 |

测试纪律：按项目惯例 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`、`@pytest.mark.unit`、
失败先行、NEUTER 变异验证、批后 `--collect-only` 闸。

---

## 9. 开放问题（实现时以实测定案）

- **O1 认证头 + token 端点**：OAuth token 走 `Authorization: Bearer`
  （CLIProxyAPI 现状）还是 `x-api-key`（Tofu 现注释「2026 起 Bearer 401」）？
  同歧：Tofu 的 token 端点是 `console.anthropic.com/v1/oauth/token`，
  CLIProxyAPI 用 `api.anthropic.com/v1/oauth/token`。两者 S1 实现为可切换配置，
  S2 用真 token 一次实测同时定案后收敛。两个 host 均已在白名单，不阻塞。
- **O2 Tofu 自有工具名**：CLIProxyAPI 的改名表只覆盖 Claude Code 官方工具；
  Tofu 工具（`read_files`/`grep_search` 等 30+）不在表内。需要实测：Anthropic
  对未知工具名是放行、标记还是拒绝？若需改名，建立 Tofu→官方最近似名映射
  （如 `read_files→Read`、`grep_search→Grep`、`run_command→Bash`），
  响应侧按 reverseMap 恢复。
- **O3 uTLS**：agent 侧 `curl_cffi` 对 chatgpt.com 是否必需（Cloudflare 1010）？
  S2 先用 requests 实测，被拒则 curl_cffi 升为必选依赖。
- **O4 rush 轮询**：流活跃期 agent 是否缩短 poll 间隔（减少 token 突刺感）？
  v1 不做，实测体感后定。

---

## 10. 验收标准（epic 级）

1. 服务器全程无外网的环境下：Claude + Codex 订阅登录、8h 后自动刷新、
   聊天流式输出全通（agent 在有网机器上）。
2. agent 离线时：OAuth 卡片明确提示而非静默失败；token 不丢。
3. 域名白名单外目标被双重拒绝（服务器/agent 各一次，均有测试）。
4. cloaking 后的 Claude 请求形状与 CLIProxyAPI 对拍一致（计费头、beta、
   system 三段、工具名）。
5. 全量守卫套件绿，含失败先行与 NEUTER 记录。

---

## 11. 部署接线（2026-08-01 实测定案——agent 到底怎么到达服务器）

真机验收暴露的物理层事实（证据见 JOURNAL 2026-08-01「egress 鉴权层定案」
「续·已重启+网段通+agent 已起三项只成立一项」与当日末条）：

1. **401 签发者 = MLP/VS Code 平台代理的 SSO 闸，不是 tofu**。证据：
   `/api/health`（应用层公开路径）经代理也 401；代理 401 为 HTTP/2、无
   hypercorn server 头、CORS 仅 mlp.internal.example.com；本机直连同路径 200；
   **带合法 tofu admin Bearer 打代理照样 401**——平台闸对 tofu 凭证完全失明。
   `*.mlp.internal.example.com/proxy/<port>` 是交互浏览器专用通道（SSO cookie），
   **任何程序化客户端（curl/agent）都过不去，不是凭证问题，不要尝试**。
2. **bridge poll 是「仅凭证、地址盲」端点**（`routes/api_v1/auth.py::_BRIDGE_PATHS`）：
   `X-Bridge-Secret` 三选一——进程内 loopback token（托盘 app 专用）、
   全局 `TOFU_BRIDGE_SECRET`（本部署未设）、**`agents:bridge` scope API key**。
   agent 侧 `--bridge-secret` 即发此头（`_run.py:181`）。
3. **本部署的 key 必须 `user_id=''`**：auth 模式 open ⇒ 浏览器合成 admin ctx
   （`user_id=''`）；`lib/llm/stream.py` egress 路由硬编码 `user_id=''` ⇒
   命令带 `''`；bridge `_deliverable` 对（命令 user_id, agent user_id）
   fail-closed 相等匹配——key 带真实租户 id 则命令**永不可投递**。
4. **铸 key 必须走运行中服务器的 API（`POST /api/v1/keys`）**：`_ensure_loaded`
   每进程只加载一次 key 文件（`lib/api_keys/_store.py:62`），进程外铸 key 对
   运行中服务器**不可见**（重启才生效）；经 API 铸则缓存+落盘同进程生效，
   且覆盖落盘时自动逐出进程外孤儿 key。在册：`k_d1adfa20`（egress-agent-office）
   与 `k_2a687bc6`（egress-bridge-office），均 agents:bridge + user_id=''，
   两把等价可用；现行明文落 `data/config/.egress_bridge_key`（0600）。
5. **服务器绑定与到达路径（两条，已定案主备）**：
   - **主路径（直绑，早前定案；2026-08-04 起 0.0.0.0 已成为默认绑定，
     无需再传 env）**：`./restart_15000.sh` 重启即全接口绑定，办公机直连
     `http://10.128.175.30:15000`。暴露面已评估：open 模式对非 loopback
     拒发合成 admin，bridge 恒要凭证，可接受。（历史注：BIND_HOST 曾必须
     走 shell 脚本注入——UI 重启按钮是 os.execv，继承旧进程 env，
     2026-08-01 实测。）
   - **备选路径（端口转发，免重启，今天就能用）**：办公机
     `ssh -L 15000:127.0.0.1:15000 <codelab-ssh-地址>`（或 VS Code 端口面板
     转发 15000），agent 连 `http://127.0.0.1:15000`，对 tofu 呈现 loopback。
   两条路径的 agent 侧凭证相同（第 2/3 条的 key）。
6. **agent 出口代理 = 环境变量**：`proxy_mode='env'`（默认）按
   `HTTPS_PROXY/https_proxy/HTTP_PROXY/http_proxy` 发现（`_egress.py:131`）。
   办公机如需客户端代理（Clash 等）访问 Anthropic/OpenAI，起 agent 前
   `set HTTPS_PROXY=http://127.0.0.1:7890`（按实际端口）。

**已实测验证（容器内全链，2026-08-01）**：poll 三态（对 key 200 `{"commands":[]}` /
错 key 401 / 无 key 401）；容器内真实 agent `--allow-egress --bridge-secret`
注册上线、`capabilities.egress=true`；`GET /api/v1/oauth/status` 五态机首查
`unknown`（后台探测，设计行为）→ 复核 `state=agent` + `verdict=geo_blocked`
+ agent 在列（**S4 状态面真机首验**）。自测 agent 已停（避免与办公机 agent
双在线触发 route_request 多 agent 拒绝）。

## 11.1 办公机真机 runbook（修正版，替换此前一切口头命令）

```bat
:: ① 到达路径（二选一）
::    主：等 BIND_HOST=0.0.0.0 重启后直连 http://10.128.175.30:15000
::    备（免重启）：ssh -L 15000:127.0.0.1:15000 <codelab-ssh-地址>
::                 然后一律用 http://127.0.0.1:15000

:: ② 办公机装受控端（正式路径，2026-08-02 起；此前的「拷贝 chatui 仓库
::    + pip install requests」临时路径退役）
::    本地控制 →「这台电脑」→ 受控端·轻量（TofuAgent-Setup-x.y.z-win64.exe，
::    服务器直连下载，约 53 MB）。首次启动粘贴连接行（本地控制一键生成，
::    含地址+token），托盘勾选「允许中继订阅流量」（= --allow-egress）；
::    无人值守机器务必再勾「Start with Windows」（重启不断桥）

:: ③ 起 agent（key 在服务器 data/config/.egress_bridge_key，0600）
::    —— 用安装包时跳过本步：TofuAgent 随登录会话常驻（托盘可见状态），
::    以下命令行路径仅供开发机调试
set TOFU_BRIDGE_KEY=<文件内容>
python -m lib.desktop_agent --server http://127.0.0.1:15000 --allow-egress --bridge-secret %TOFU_BRIDGE_KEY%
:: 如需客户端代理：先 set HTTPS_PROXY=http://127.0.0.1:7890
```

验证顺序：agent 控制台无 `Cannot reach server` →
`GET /api/v1/oauth/egress-agent` 出现办公机 agent 且 `egress=true` →
OAuth 卡片出口行显示「经桌面代理」→ Claude 登录 → 流式聊天 → Codex O3 定案。
