# MCP 共享 SSO 会话适配器

## 责任与模式

`tofu_mtsso_auth` 只适配上游认证实现，不实现 OAuth grant。业务请求的票据仅通过内部管道返回调用进程，不进入日志、模型结果或新的票据文件。票据缓存按显式 owner、环境、认证来源、MIS、SDK 和源会话隔离，进程结束清除；修改会话/登出会隔离在途结果。

- `MTSSO_AUTH_MODE=auto`：存在官方 Agent 注册时优先且固定使用官方 `mtsso-moa-local-exchange`。安装 CLI 本身不构成注册。不声明官方配置时，可发现已安装的 DataFE SSO SDK 和已有、当前 MIS 的 MLP/LLM 登录会话，统一给三项 MCP 换票。自动选定的共享来源也固定到显式 `clear()`；源文件消失或失效不会退回 legacy。
- `official`：必须使用官方 Agent SSO；任何失败都不降级为另一种登录。
- `shared`：显式使用同一 DataFE 登录会话；token exchange 与 introspection 委托 `@mtfe/mtsso-auth-official` 的 API；CIBA 登录与续期委托 SDK。业务调用按下文契约自愈。不能用旧用户会话为新用户换票。
- `legacy`：显式保留各 MCP 原有认证实现，不自动迁移或复制凭据。

## 中台 MCP 认证契约

学城、Hope（浏览/容器日志）、LLM 三个 MCP 及 `exec` 启动器对模型暴露同一套行为；各 MCP 的 legacy 认证也遵守同样的对外语义。

- **一个状态词表**：`auth_state` 取 `ready`（已在线验证，可直接调用业务）、`login_required`（需要用户在大象批准一次登录）、`pending`（同一账号已有授权在进行）、`blocked`（权限、身份、配置或用户拒绝，需人工处理）、`unknown`（官方 Agent SSO 尚未换票，无法无交互验证）。`logged_in=true` 当且仅当 `ready`，此时 `identity_verified=true`，`subject` 为在线验证的 MIS。
- **状态是业务凭据路径的无交互演练**：`session_status(owner, audiences)` 走业务调用同一路径（必要时 SDK 续期 → 官方 introspection → token exchange），从不发送授权；`ready` 时顺带缓存业务票据。状态与业务结果只可能因两次调用之间的服务端吊销而不一致，且该情况由业务调用自愈。
- **业务调用自愈**：`user_token(...)`（默认 `interactive=True`）在共享会话缺失、无法续期或已在服务端失效（`SSO_SESSION_REQUIRED`）时，在同一把源会话锁内升级为 SDK CIBA 登录：向目标 MIS 的大象发送一次授权，等待 SSO 授权有效期（服务端 `expires_in`，SDK 默认 120 秒；单次桥接上限 `_LOGIN_TIMEOUT`=180 秒含续期与换票），批准后继续原调用。登录发生在业务请求发出之前，写操作不会重放。进程内同一来源的并发调用共享这一次授权及其结果；其他 MCP 进程在源会话锁上等待整个授权窗口后复用新会话；SSO 服务端也复用在途的同一授权请求。权限拒绝、身份不一致、配置错误不会触发登录。
- **未完成的授权**：未在有效期内批准（`SSO_LOGIN_NOT_APPROVED`）只与等待该次授权的并发调用共享；之后的新调用视为用户确认后的重试，重新发送一次授权。用户拒绝（`MOA_USER_REJECTED`）和其他终止错误在进程内保留 30 秒，不重复推送；服务端另有 `MOA_REJECT_COOLDOWN`。状态探测的失败不能阻止业务调用自愈。
- **显式登录**：`login(...)` 与交互式业务调用同路径，只是不发业务请求，用于批量操作前预先授权；已有可用会话时不推送（`login_performed=false`）。模型不需要先调用它。
- **授权来源**：共享会话的 CIBA 由 DataFE CLI SDK 用其登录应用（默认 MLP CLI 专属 SSO 应用，可由 `MLP_SSO_CLIENT_ID` 覆盖）发起，请求不带 MCP 信息，因此大象卡片显示该应用（用户看到的是 MLP/Hope），与触发它的 MCP 无关；批准一次即服务所有共享此会话的 MCP。结果中的 `approval` 说明渠道、登录目标和会话文件。Hope 提交后端的 `hope login`（万象机器学习推送）是独立协议，仍单独审批。
- **错误形状**：`{ok:false, error_code, error, auth_source, auth_state, login_attempted, retryable, user_action_required, model_action, recovery_action[, approval]}`。`login_required`/`pending` → `retryable=true`、`model_action=ask_user_to_approve_then_retry`（告诉用户在大象批准后重试原操作）；`blocked` → `stop_and_report`。适配器错误不含 `login_required` 字段：MCP 不得据此启动另一套登录；`retryable` 仅是给模型的信息，不触发自动重试。

## 共享会话配置

`MTSSO_MIS` 指定期望的美团账号；可复用一致的 `MLP_MIS` / `LLM_MIS` / `HOPE_USERNAME` / `XUECHENG_MIS`。MIS 是登录目标，不是身份认证证明。没有显式目标时，只可从已选源会话的 MIS 元数据发现目标；业务调用仍须官方 introspection 验证 active、ACCOUNT 主体及实际 MIS。不同 MCP 的配置目标不一致时拒绝，不猜测账号。

可选 `MTSSO_SESSION_SDK` 是已安装 `@datafe/llm-cli/dist/lib/auth.js` 的绝对路径；不配置时只查本地 CLI/已知安装目录，不在线安装。该上游模块拥有登录 client 的配置。本包不复制其默认 client secret。可选 `MTSSO_SESSION_TOKEN_FILE` 是已有登录会话的绝对路径；不配置时选 `~/.mlp-cli-token.json`，不存在才选 `~/.llm-cli-token.json`。显式文件不存在、不可读或身份不匹配时不换另一个文件。

仅接收当前进程用户拥有的普通私有文件，拒绝符号链接、FIFO、大文件及组/其他用户可访问的票据文件。共享 SDK 的生产登录能力已核验，因此共享路径仅支持 `PROD`；`TEST` 必须使用配套的官方 Agent 配置，不能把生产会话换成测试票据。`MTSSO_NODE_EXECUTABLE` 可显式指定兼容 Node；上游官方 SDK 要求 Node 20+。

所有共享访问用同一个私有源会话锁协调 MCP 进程，刷新和登录交给 SDK 写回其原会话文件，不在 MCP 之间复制 token。SDK 写入前要求源目录由当前用户拥有且不可被组/其他用户写入，并以独占 0600 文件预留上游固定 `.tmp` 路径，预留内记录本进程（pid、Linux 启动时间、主机名）。已有 scratch 文件只在它是无人使用的预留时回收：当前用户的单链接私有普通文件，且为空（旧版本的预留，或刚创建就被中断）或所记本机进程已不存在；所记进程仍在运行（例如启动器已退出、在自己会话里等待授权的登录）时报 `SSO_AUTH_REQUEST_PENDING`；其他已有文件一律拒绝，不跟随链接、不覆盖遗留内容。会话和 SDK 路径在操作前后重新核对，未经调用的 SDK 不能承接旧结果。进程内同一来源的并发调用合并；不同 audience 的缓存命中无需等待锁。交互式调用在锁上最多等待一次换票加一个授权窗口（`_TIMEOUT + _LOGIN_TIMEOUT`），状态探测只等待一次换票（`_TIMEOUT`），超时报 `pending`。外部 CLI 不遵守本包锁，源文件内容变化会导致本包丢弃旧缓存。

问题：等待大象授权的登录拿着预留可达数分钟，截止时间的 SIGKILL 或重启时整组终止都不执行清理；留下的空 `.tmp` 曾让之后每次 SDK 写入都以 `SSO_SESSION_CONFIGURATION` 拒绝，共享会话既不能续期也发不出那一次授权，所有共享此会话的业务调用（含大象桥）持续失败，直到有人手工删除。决定：由记录的进程是否存在来判定。否决：按文件年龄回收（是调参阈值，且分不清仍在等授权的登录）；信号处理清理（SIGKILL 无法处理）；覆盖任何已有文件（会丢掉防符号链接与遗留内容的保护）。

## 公共边界

- `enabled()` / `provider_status()`：本地配置发现，无网络、无 CLI 探测、无登录。
- `session_status(owner, audiences, mis=...)`：按上文契约无交互验证，返回 `auth_state`；`login_required`/`pending` 是成功的状态答复（`ok=true`），`blocked` 返回错误 envelope。官方 Agent SSO 只报告本进程已换得的票据（`ready`），否则 `unknown`。
- `user_token(owner, audiences, mis=..., interactive=True)`：业务票据，默认自愈；`interactive=False` 只用于状态探测。显式 MIS 必须与环境/源会话目标一致，不能只用作缓存键。
- `login(owner, audiences, mis=..., timeout_sec=None)`：显式预授权，返回 `approved`、`login_performed`、`subject` 与 `approval`；`timeout_sec` 只能缩短授权等待，默认等整个授权窗口。权限拒绝和用户拒绝都停止，不重复、不切换认证来源。
- `verified_subject(owner)`：只返回本进程成功换票时经官方 introspection 验证的共享身份；配置或环境变化、缓存过期和清理后不返回旧身份；内省的主体/类型与 JWT 别名存在冲突时拒绝，不能让本地别名覆盖服务端主体。
- `invalidate()`：清票据，不改变已固定的官方认证来源。
- `clear()`：显式结束进程会话并清所有缓存；不删除上游已有登录会话文件。
- `python -m tofu_mtsso_auth exec --owner <owner> --ticket ENV=AUD[,AUD...] -- <command>`：为单个命令（如市场 skill 的 CLI）按上述规则换取同一用户票据，再 exec 替换当前进程；票据只进入该命令的环境变量，不进 argv、输出、日志或文件。ENV 名须含 `TOKEN`/`TICKET`/`SSOID`，拒绝 `NODE_OPTIONS`、`LD_PRELOAD` 等会把值回显到诊断里的运行时变量。它是业务调用，同样自愈（需要时发送一次大象授权并等待）；失败时 stderr 输出脱敏 envelope，`user_action_required`（如 `SSO_LOGIN_NOT_APPROVED`、权限拒绝）退出码 42，否则 1，命令不会执行。

错误保留官方要求人工处理的文案并脱敏；exit 42 或已知权限拒绝码均为终止错误，禁止 force-refresh 绕过。一般错误只投影固定安全文案。网关占位符不是 bearer token；未适配的 HTTP 传输拒绝转发。

## 决策

**共享会话（0.2）——问题**：各 MCP 使用不同文件重复请求授权；泛用 SDK 会自动进入浏览器登录。**决定**：同一显式用户的上游会话共用，官方 API 换票/验证，只显式调用 SDK 的 `refreshAccessToken`/`loginWithCiba`；缓存带来源指纹及会话栅栏。**未采用**：复制 token 到每个 MCP（多个状态源）、重新实现 CIBA/Token Exchange（第二认证实现）、在权限拒绝后尝试另一个来源（身份/权限绕过）、SDK 的 `getAccessToken`/`forceLogin`（会静默进入 MOA/浏览器登录）。Hope 提交后端的专用 session 与 MLP SSO 是不同协议，仍独立处理。

**自愈契约（0.3，2026-11-02 owner 要求）——问题**：0.2 的状态只看本地文件是否有 token，服务端已吊销的会话仍报 `logged_in=true`；业务调用随后以“请通过显式登录工具完成授权”失败，却同时标注 `login_required=false`、`stop_and_report`，模型需要额外回合猜测恢复方式；refresh token 失效时连显式登录也无法恢复；大象卡片显示 MLP/Hope 应用，用户无从知道它来自学城调用。**决定**：状态走无交互的业务凭据路径；业务调用在会话需要登录时于同一把锁内发起一次大象授权并等待；错误与状态共用 `auth_state` 词表；结果携带 `approval` 说明卡片来源。**未采用**：保持业务快速失败并由模型调用 login（多一轮、依赖模型理解认证细节，即被报告的问题）；状态只做 introspection 不续期（可续期的会话会被误报为需要登录）；业务调用一律用 `login` 动作（正常换票卡死时要等 180 秒而非 30 秒）；在单独进程里后台登录、业务调用先返回 `pending`（仍需模型重试）；每个 MCP 用自己的登录应用让卡片显示正确名称（每个 MCP 各需一次批准，并恢复多套会话）。

## 验证

`PYTHONPATH=packages/tofu-mtsso-auth/src python -m pytest packages/tofu-mtsso-auth/tests`；Node 桥使用无网上游 fixture 验证身份、官方退出码、输入/输出、状态探测无登录、业务调用自愈及并发调用共享单次授权。线上业务验证是独立证据，不能由本地测试推定。
