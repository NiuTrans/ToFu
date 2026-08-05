# 订阅包装增强设计 —— CLIProxyAPI 深度对标与场景矩阵

> 触发：owner 2026-08-04 指令「深挖 CLIProxyAPI 如何包装 ChatGPT/Claude Code
> 订阅，增强我们的方案以应对各种场景——例如服务器在失去外网的远程开发机上，
> 如何经仍能联网的本地机路由订阅流量」。
> 基线：`../CLIProxyAPI` @ a63da8a（2026-08-04 git pull 最新）；四路并行深读
> （架构 / Claude / Codex / 凭证生命周期），关键结论均带 file:line。
> 前置文档：docs/DESKTOP_EGRESS_DESIGN.md（egress 中继已建成，S1-S4 代码面 DONE）；
> memory `cliproxyapi_订阅机制与_tofu_oauth_失败根因`。

---

## 1. CLIProxyAPI 解剖：一台机器上的「翻译官 + 化妆师 + 车队调度」

它的本质是把「订阅账号」包装成「标准 API 端点」的适配器，三层各司其职：

### 1.1 协议仿真层（翻译官）

- Gin 服务，默认 `:8317`；路由集中在 `internal/api/server_routes.go:39-111`：
  OpenAI `/v1/chat/completions`、Anthropic `/v1/messages`、
  Responses `/v1/responses`（+ `/backend-api/codex/*` 直连别名）、
  Gemini `/v1beta/*`——**按各家 CLI 的原生端点逐一仿真**，所以 Claude Code /
  Codex CLI 只需把 base URL 指过来，零改造接入。
- 翻译注册表 `internal/translator/translator/translator.go:24`
  `Register(from, to, …)`：入口协议 × 上游协议约 20+ 对，双向翻译
  （请求 A→B、SSE 响应 B→A 逐 chunk 翻回）。
- 执行器在 `internal/runtime/executor/`（claude/codex/gemini/antigravity/
  kimi/xai/openai_compat），凭证选择支持
  round-robin / weighted-round-robin / fill-first + 会话亲和。

### 1.2 身份伪装层（化妆师）——2026 军备竞赛前线

**Claude**（`internal/runtime/executor/claude_executor_*.go`）：

| 项 | 现状（2026-08-04 版） |
|---|---|
| UA / 版本 | `claude-cli/2.1.220 (external, cli)`（`helps/claude_device_profile.go:23`），配套 X-Stainless 家族 + **线上 header 大小写顺序复刻**（claudeWireHeaderCasing） |
| 计费头 | `x-anthropic-billing-header: cc_version=2.1.220.<fp3>; cc_entrypoint=<entry>; cch=00000;<workloadPart>`（`claude_executor_cloaking.go:162`；cch=00000 仍是 OAuth 分支，新增 entrypoint 变体 cli/claude-vscode/sdk-cli 与 workload 后缀） |
| 系统提示 | system 整体替换为 [计费块, "You are Claude Code…"(带 cache_control)]；调用方 system 降级为首轮 user 之后的「会话中 system 消息」 |
| beta 列表 | 复刻 2.1.220 实测顺序，新增 `thinking-token-count-2026-05-13`、`effort-2025-11-24`、`extended-cache-ttl-2025-04-11`；**第三方自定义 beta 直接丢弃** |
| 登录/token | `claude.ai/oauth/authorize` + PKCE；token 端点 **`platform.claude.com/v1/oauth/token`**（JSON 字段顺序复刻 2.1.220 线上报文）；控制面调用伪装 `axios/1.15.2` UA；登录后伴生拉 profile/roles |
| TLS | uTLS 浏览器指纹（绕 Cloudflare） |

**Codex**（`internal/auth/codex/` + `codex_executor_*.go`）：

| 项 | 现状 |
|---|---|
| OAuth | `auth.openai.com`，client_id `app_EMoamEEZ73f0CkXaXp7hrann`，回调 `:1455`，附加 `prompt=login` + `id_token_add_organizations=true` + `codex_cli_simplified_flow=true` |
| 身份头 | **Originator 已从 `codex_cli_rs` 迁到 `codex-tui`**；UA `codex-tui/0.146.0 (Mac OS 26.5.0; arm64) iTerm.app/3.6.10 (codex-tui; 0.146.0)`；`Chatgpt-Account-Id` 来自 id_token JWT（不验签解析 `https://api.openai.com/auth` claim） |
| 上游 | `chatgpt.com/backend-api/codex/responses`，强制 `stream:true` + `store:false`；instructions 缺省置空串（**无硬编码提示词**）；非 free 套餐自动追加 image_generation 工具 |
| 配额信号 | `usage_limit_reached` → 改写 429 并从 `resets_at`/`resets_in_seconds` 解冷却时间；`selected model is at capacity` → 429；`refresh_token_reused` → 不可重试 |

### 1.3 凭证车队层（车队调度）

- `auths/` 每账号一个 JSON（0600，原子写），冷却状态独立 `.cds` 文件；
  fsnotify 热加载（防抖 150ms/1s 分档）。
- 调度：smooth weighted RR（按 provider+model 维度）+ 会话亲和 TTL 缓存。
- **按 (auth × model) 粒度的失败账本**（`sdk/cliproxy/auth/conductor_cooldown.go`）：
  401 → 先同步刷新一次重试（per-auth 锁去抖），仍败挂起 30min；
  402/403 → 30min；404 → 12h；429 → 用上游 RetryAfter，否则 1s→30min 指数
  （同一冷却窗内并发失败只升一级）；408/5xx → 1min 瞬时冷却；
  **请求级错误（400/422 语义错）不进冷却账本**。
- 主动刷新循环：5s tick / 16 worker，按 provider lead time 提前刷；
  刷新失败 5min 退避，无效刷新（token 未变）30s 防空转。
- 管理 API：`/v0/management/auth-files` CRUD、配额查询、usage-queue。

### 1.4 Cluster 模式 —— 拓扑与我们的正好相反

CLIProxyAPIHome（控制面 `:8327`）集中持有凭证并**在 Home 侧执行**上游请求，
worker 节点经 Redis 协议连 Home 取活（`internal/home/client.go`，含故障转移）。
它解决的是「多个入口共享一个能上网的执行点」——**执行在中心**。

我们的场景是「服务器（中心）断网，执行必须下沉到边缘」——CLIProxyAPI
的 cluster 帮不上（Home 断网照样全灭），而 tofu 的 bridge（agent→服务器
长轮询，NAT 天然安全）恰好是唯一正确方向。**这不是缺口，是反超。**

---

## 2. 对位实测：tofu vs CLIProxyAPI（2026-08-04）

| 项 | CLIProxyAPI | tofu 现状 | 判定 |
|---|---|---|---|
| 边缘中继（断网服务器→有网本地机） | 无（cluster 是中心执行） | **已建成**：egress_http / open_stream / cancel / 半开看门狗 / 双白名单 | ✅ 反超 |
| Claude 版本指纹 | 2.1.220 | **2.1.63**（outbound.py:59） | ⚠️ 漂移 |
| Claude token 端点 | platform.claude.com | console.anthropic.com | ⚠️ 漂移（白名单需补 platform.claude.com） |
| Codex Originator/UA | `codex-tui` + 版本化 UA | `codex_cli_rs` + `codex_cli_rs/0.20.0`（outbound.py:78-79） | ⚠️ 漂移 |
| beta 列表 | 2.1.220 版（9+ 个，含新 flag） | 2.1.63 版 | ⚠️ 漂移 |
| 计费头算法（fp + cch=00000） | 同（新增 entrypoint/workload 变体） | 已对齐 | ✅ |
| PKCE 参数 / plan_type 门控 / 刷新 singleflight | 同 | 已对齐（codex.py、token_store.py） | ✅ |
| uTLS 浏览器指纹 | 有 | 无（agent 侧裸 requests） | ❌ 缺口（O3 悬案） |
| 多账号车队 + 冷却账本 | 有（auth×model 粒度） | 单账号 + dispatcher 模型级回退 | ❌ 缺口 |
| 订阅配额信号解析（resets_at） | 有 | 无（通用 429 处理） | ❌ 小缺口 |

**核心结论**：手工追平伪装层是一场注定落后的消耗战——我们的快照 4 天前
移植，今天已漂移 4 处。CLIProxyAPI 有专门的社区在跟这场军备竞赛，我们没有。
结构上只有两条出路：**把漂移变成自动警报**（§4.1），或**把伪装层整个外包**
（§4.4）。

---

## 3. 场景矩阵（「各种场景」全覆盖盘点）

| # | 场景 | 现状 | 缺口/动作 |
|---|---|---|---|
| S1 | 服务器断网 + 一台有网本地机 | ✅ 已建成（agent egress 全链：登录/刷新/聊天流式） | 待 owner 真机验收（pt_59b62951aad2463e P4） |
| S2 | 本地机也只有 Clash 类系统代理 | ✅ 已实现：env → Windows 注册表（ProxyEnable/ProxyServer）→ macOS `scutil --proxy` 三级发现（`_egress.py:72-92`，owner 复核 2026-08-04 实证） | 无 |
| S3 | 多台有网机（办公机+笔记本） | ≥2 台在线时强制手工 pin（egress.py `route_request`） | **G1：pin 优先 + 健康度排序的有序 fallback 链**；流在 bootstrap 前死亡可换机重试（对齐 CLIProxyAPI stream bootstrap retry） |
| S4 | 多订阅账号池化配额 | 不支持（每 provider 单 token 文件） | **G2：原生多账号成本高，建议直接走 §4.4 的适配器方案**（CLIProxyAPI 车队现成） |
| S5 | 服务器长期断网 + 伪装层持续漂移 | 手工快照移植，4 天漂移 4 处 | **G3：§4.1 漂移警报（必做）+ §4.4 sidecar 外包（推荐）** |
| S6 | 浏览器扩展当中继 | 已否决（DESKTOP_EGRESS_DESIGN §2.2：CORS/存活面/TLS 指纹三因） | 维持否决 |

---

## 4. 增强方案

### 4.1 G3-A：伪装层漂移自动警报（必做，便宜）

CLIProxyAPI 就克隆在 `../CLIProxyAPI`——**把「追平上游」变成守卫测试**：

- 新套件 `tests/test_oauth_cloaking_drift.py`：解析参照仓库的
  `helps/claude_device_profile.go`（版本号）、`codex_executor_request.go`
  （originator/UA）、`anthropic_auth.go`（token 端点）、beta 列表常量，
  与 `lib/oauth/outbound.py` 对拍；漂移即红，报错信息带同步指引。
  参照仓库缺席时 skip（CI/开源导出不受影响）。
- outbound.py 常量区补「同步源 + 上次同步日期」注释（对齐 requirements.txt
  floor 理由段纪律）。
- 同步动作本身仍人工实测定案（版本号不是改了就算，要看上游收不收）——
  测试只负责**报警**，不负责自动改。

### 4.2 G1：多 agent 有序 fallback（小改）

`route_request` 多 agent 分支：pinned 优先，其余按（最近 egress 成功时间、
注册时长）排序成 fallback 链；`open_stream` 在 **meta 帧到达前**失败允许换
下一台重试一次（meta 后失败绝不换——避免重复计费）。改动面：
egress.py + 设置页提示文案，不碰协议。

### 4.3 G2：原生多账号——评估结论：缓行

token_store 多记录化 + dispatcher 集成 + (account×model) 冷却账本 =
一次中型改造，而 CLIProxyAPI 车队现成且天天在实战。**需要池化的用户走
§4.4；单账号用户无感。** 不在本设计排期。

### 4.4 G3-B / 方案 B：CLIProxyAPI sidecar——「订阅适配器」provider（推荐立项）

> **✅ 已落地（2026-08-04，E4 批）**：agent 看护器 `lib/desktop_agent/_adapter.py`
> （首启下载+checksums.txt SHA-256 校验+周检更新+崩溃看护+resume）；loopback
> 中继类 `target='loopback'`（双端白名单=agent 自身策略端口）；服务器层
> `lib/desktop/adapter.py`（策略库/relay/ensure 编排/managed provider）+
> 路由 `/api/v1/adapter/*`；传输链 adapter 标记镜像 oauth 全链（slot→api→
> stream/astream/chat/probe）；设置页 OAuth 区订阅适配器卡片。真机冒烟实证：
> v7.2.116 下载校验→spawn→健康→/v1/models 200、错误 key 401。唯一偏差：
> api-key/mgmt secret 由**服务器**铸（provider 调用必须持有，agent 侧铸钥
> 只会多一条上传道）——满足 owner「随机、按 agent 存、不裸奔」三要件。

**思路**：伪装层整个外包。在能联网的本地机上跑 CLIProxyAPI，tofu 把它当
一个普通的 OpenAI/Anthropic 兼容上游。订阅 token 从不到服务器——**本机既是
网络出口，也是凭证边界**，比 egress 中继（token 在服务器、裸奔过 bridge）
更干净。

```
[断网服务器]  tofu dispatcher → provider「订阅适配器(claude-pro)」
     │  base_url = bridge://agent/<id>/loopback/8317
     ▼  bridge 帧通道（复用 egress_http_stream 的流式拼帧层）
[有网本地机]  tofu agent → 127.0.0.1:8317（CLIProxyAPI 子进程）
     ▼  uTLS + 最新伪装（上游社区维护）
  chatgpt.com / api.anthropic.com
```

**改动面**：

| 层 | 改动 |
|---|---|
| bridge | 新增 loopback 目标类型（`loopback_http` / `loopback_http_stream`），白名单 = `127.0.0.1:<适配器端口>`，**不复用**订阅域名白名单（语义分离：一个是「放行公网域名」，一个是「只准打我自己脚边」） |
| agent | adapter 命令族：`adapter_ensure`（定位/启动子进程，崩溃看护）、`adapter_status`、`adapter_stop`；二进制分发见 O1（owner 已拍板，见 §6） |
| agent（安全，owner 补充③） | 适配器启动时由 agent 生成**随机 api-key** 写入其 config（tofu 侧按 agent_id 存），`/v0/management` 设独立 secret-key 或整体禁掉——8317 绑 loopback 但本机任意进程可打，不能裸奔 |
| 服务器 | 新 provider 类型 `subscription-adapter`：base_url 走 bridge；模型清单从适配器 `/v1/models` 拉； dispatcher 视角它就是一个普通 OpenAI 兼容 slot，fallback/重试全复用 |
| 设置页 | OAuth 卡片加一行「由本机适配器托管」（状态：适配器在线/版本/账号数） |

**收益**：军备竞赛外包给上游社区（他们跟版本号的速度以天计）；uTLS 免费；
多账号车队免费（S4 顺带解决）；Gemini/Qwen/Kimi/antigravity 等额外订阅源
免费；服务器零凭证。

**最大红利：登录链整个坍缩（owner 补充②）**。适配器自带本机 OAuth 回调
监听（localhost:1455/54545）+ 本机浏览器，登录在有网机器上一次完成——
我们现在维护的三级登录兜底（服务器交换 → B1 浏览器交换 → curl 手工）与
「刷新过 bridge 的 singleflight 竞态」对适配器托管的订阅**全部不再存在**，
这是把 tofu OAuth 代码里最痛的两块直接删除。设置页对适配器托管订阅的
登录按钮改语义为「在本机浏览器完成登录」（适配器侧自带回调页，零手工
贴码）；token 刷新由适配器自管，服务器侧该 provider 的 refresh 路径整体
旁路。

**更新通道（owner 补充④，不做则漂移在边缘重演）**：agent 周期性（每周，
错峰）检查上游 GitHub Releases，新版本经哈希校验后换二进制并重启子进程；
钉版本 + 永不更新 = 三个月后边缘端重演 §2 的漂移，「外包军备竞赛」的
完整语义必须包含跟进机制。

**成本**：agent 要管理一个 Go 子进程（分发/版本/看护）；多一层运维概念。
**与内建 egress 的关系**：不是替代，是分层——内建 cloaking 是零依赖兜底
（没装适配器也能用），适配器是追求完美体验的加强线。落地顺序上
§4.1（警报）先于 §4.4（外包）：即使外包了，兜底层的漂移也要可观测。

### 4.5 顺手果实（dispatcher 级，独立可摘）

- **Codex 配额信号**：`usage_limit_reached` 解析 `resets_at`/`resets_in_seconds`
  做定时冷却，替代通用 429 退避（lib/llm_errors 分类增强）。
- **401 先刷一次再判死**（对齐 `tryRefreshAfterUnauthorized`）：订阅 slot 收到
  401 时先强制刷新 token 重试一次，再上报不可用。
- **请求级错误不进冷却**（对齐 `isRequestScopedResultError`）：400/422 语义错
  不污染模型级回退账本。

---

## 5. 实施分片建议

| 片 | 内容 | 体量 | 可独立验收 |
|---|---|---|---|
| E1 | §4.1 漂移警报测试 + 常量同步实测定案（2.1.220/codex-tui/platform.claude.com） | 小 | 对拍测试红→绿；真 token 实测上游收货 |
| E2 | §4.5 三枚顺手果实 | 小 | 各带单测 |
| E3 | §4.2 agent fallback 链 | 小 | fake bridge 双 agent 演练 |
| E4 | §4.4 订阅适配器（bridge loopback + agent 子进程看护 + provider 类型 + 设置页） | 中大 | 断网服务器全链：登录在适配器侧完成、聊天经 bridge 流通 |

## 6. 开放问题

- **O1 适配器二进制分发（owner 2026-08-04 拍板）**：**首启按需下载 +
  版本钉死 + 哈希校验 + 周期更新检查**（「能上网的本地机」恰好一定能下
  GitHub Releases）；不内嵌安装包（避免 53MB 再涨），不支持用户自装
  （版本失控）。周期更新检查见 §4.4 更新通道段。
- **O2 多用户**：每用户的 agent 跑各自的适配器实例，凭证天然租户隔离
  （bridge user_id 下穿已有）——需要端口分配策略（8317 冲突时递增）。
- **O3 计费头新变体**：entrypoint=claude-vscode / workloadPart 后缀是否
  需要跟进——E1 实测时一并定案。
- **O4 uTLS 兜底**：内建 egress 路径（非适配器）agent 侧 curl_cffi 是否
  必需（旧 O3 悬案），E1 实测 chatgpt.com 是否 1010 后定案。
