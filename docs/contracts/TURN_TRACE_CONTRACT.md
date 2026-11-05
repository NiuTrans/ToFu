# Turn Trace 契约 —— 可事后追溯的用户感知耗时

本文是「一次运行每阶段花了多久、用户当时看到了什么提示、浏览器是否真的及时收到并绘制」
的事实源。机器可读线形由 `contracts/conversation_sync_v3.yaml` 的 `TurnTimingTrace`
拥有；`lib/tasks_pkg/turn_trace.py` 负责投影，每个 generation attempt 是永久权威，
当前 Turn 只保留终态镜像，Request Inspector 通过 task id 读取并合并该文档。关键测试是
`tests/turn_lifecycle/test_turn_trace.py`、
`tests/turn_lifecycle/test_turn_event_carried_task_event.py`、`tests/turn_lifecycle/test_turn_runtime_perception_commit.py`
和 `tests/frontend/test_frontend_conversation_surface_vite.py`。

---

## 1. 两条证据车道，一个 attempt 持久化文档

**服务端执行耗时从权威事件推导，不产生第二套执行事实。** `round_start` /
`round_end`、工具时钟、模型请求边界和每行服务器时间共同折叠 span。每个已注册
chat phase 必须在 `_PHASE_TRACE_RULE` 中声明归置规则，漂移测试双向钉死。

问题：异步观察线程的落库时间会混入观察队列延迟，系统校时也会改变墙钟差值。
决定：HTTP 请求生命周期入口捕获配对的 epoch / monotonic ns，在鉴权和正文校验前打点。
新建 Turn、重试 Attempt、执行计划三个入口显式传递一个执行时钟，TaskRuntime 保留
同一个锚点/身份，不在注册或执行启动时重置。非 HTTP 启动从 executor 准备上下文前
打点；其他 TaskRuntime 调用保留注册起点。执行时钟不能来自客户端配置，也不作为
运行配置持久化，不依赖会传播到后续请求/子任务的 ambient context。
执行线程在入队前捕获 `executionAtMs`、`executionOriginAtMs` 和 `executionClockId`；
重复交付保留原捕获值，父显示载体不重写子任务时钟。SQL 和原生存储验证同一字段边界，
将捕获值保存为事件的 `created_at_ms` 元数据，逐 token 时间脊柱不依赖正文内容。
SQL 和原生 `event.list(payload_types=...)` 都在存储投影处省略不需要的正文。
原生从已认证 stream 的类型/时间元数据投影脊柱，不解码被省略的 JSON，超大正文
也不取 blob；`event.latest(metadata_only=true)` 同样保留生产时间而不取正文。
默认完整回放保持原有正文摘要/格式校验，元数据读不宣称证明被省略的正文内容。
逐 token 的 `delta` 只携带 `executionAtMs`，时钟身份/锚点由结构边界证明，避免重复开销。
工具和程序调用的开始/结束也使用这个任务时钟；`emittedAt` 仍是传输回执的墙钟锚点。
替代方案：在落库时重新计时无法区分执行和观察等待；用墙钟补偿或客户端推算不能证明
真实间隔。旧事件缺少生产端时钟时保留旧语义，`clockSource:legacy-wall` 明示来源。

新 HTTP 执行 trace 的 `tStart` 从服务端请求生命周期入口开始，涵盖鉴权/校验、
对话命令提交、构建上下文、注册/绑定与所有执行轮次；首条事件之前的时间是
`startup` 的 `dispatch` 阶段（见“编排窗口”）。幂等命令的重发不启动新执行，也不替换原请求的时钟。没有活入口打点的
排队激活/崩溃恢复从实际 executor 启动开始，不能把持久化墙钟的排队等待伪装成
连续单调证据。浏览器发送到服务端入口的网络时间仍属于客户端证据车道。
运行中快照使用同一个仍存活的任务时钟；热任务缺失时只截到最后
可证明的事件，并标 `clock-unavailable`。跨时钟域或混入旧事件标 `clock-domain-change`，
不能将重启期间的墙钟间隔伪装为连续单调时间。完整终态快照无需热任务继续存在。

`benchmarks/execution_clock.py` 离线测量捕获成本和事件增长：Python 3.12.13 的五次
100,000 事件样本中，事件复制加打点的中位数为 503.7 ns；每个 delta 的紧凑 JSON
增加 30 字节。时钟身份每任务只分配一次，结构边界携带来源。现有事件队列/回放的
字节上限同时计入这些字段，时钟不创建新线程、队列、定时器或持久化实体。
HTTP 入口复用中间件已有的配对时间；额外持有一个请求级整数，无新增 resident 队列。
同样五次 100,000 样本中，从捕获时间构造一个时钟（含 UUID）的中位数为 2384.0 ns，
每次新执行仅构造一次；`benchmarks/execution_clock.py` 同时复测这项开销。
这些是局部测量，不是整段对话的延迟或存储容量结论。

**用户感知只能由浏览器证明。** 会真正影响体验的 phase、终态和连接健康变化在
现有 reducer 接受权威状态且 Surface 的 `afterConversationCommit` 已提交对应
DOM 后等待绘制机会，再通过
`POST /api/v3/conversations/{conversationId}/turns/{turnId}/perception`
提交 `receivedAt` / `paintedAt` 等无内容回执。只有双 `requestAnimationFrame`
确认过可见绘制机会才记为 painted；隐藏/阻塞页的 2 秒兜底只释放有界槽位并累计
丢失数，不伪造成用户已看到。客户端时钟只产生 `renderMs` 和带
时钟偏差标记的 `transportMs`，绝不改写服务端 span、状态或结算。


回执必须在调度和回调时同时满足：当前活动会话、对应 turn 实际挂载、attempt 与
projectionRevision 等于已提交 DOM 和当前权威状态，且本地挂载令牌仍属同一 DOM 寿命。
仅调用 `renderState` 或页面可见不构成证据；窗口化移除、切换会话、修订替换，乃至同一
节点移除后重新插入，都退休旧机会并要求两帧新机会；原地同修订提交保持去重。
`conversation/application/turn-mount-tracker.ts` 使用弱节点键和一个惰性观察器，随页面
composition 销毁；缺 DOM/MutationObserver/RAF 时不造 painted，正常读取不受影响。
令牌只作本地校验，绝不进入回执 schema。这里证明的是
**活动 DOM 加可见绘制机会**，不是屏幕像素、视口相交或人类实际阅读。
快照恢复可提交 phase/terminal 回执，但没有本页精确收包证据时省略 `receivedAt`，
也没有 `renderMs/transportMs`；Inspector 明示耗时不可用，不能补零或据此推导首 token。
运行时只保留 128 个挂载身份、64 条无内容收包时钟及 recorder 的有界去重/回调状态，
不为追踪另保留一份完整 TurnState。

phase 回执按实际提示转换去重，连接状态按转换记录；失败发送走最多 32 条的队列、
最多 5 次重试和 15 秒退避上限。同一 `observationId` 以 owner + attempt + id 的
摘要作为稳定命令身份，并在 attempt 文档内做语义幂等，丢 ACK 重试不重复写。
请求 schema 不接受 `content`、`detail`
或任意额外字段，诊断链不能成为对话内容的旁路副本。
队列淘汰或重试耗尽会累计到后续回执的 `clientDroppedBefore`；Inspector 展示迄今
至少丢失的数量，不把有缺口的浏览器证据伪装成完整记录。

部分 provider-ingress phase 为保护主写队列会暂留内存，因此 canonical
`append_event` chokepoint 同时维护有界的用户可见提示历史。它不是新事件词表，
只投影同一 phase；终态与事件 fold 一起原子冻结到 attempt 的
`timing_trace_json`，并镜像到当时的 `TurnProjection.timingTrace`。终态之后的新浏览器
回执只扩展 attempt 小文档，不改写大 Turn。

---

## 2. 端点

```
GET /api/v1/tasks/<task_id>/trace        (scope: tasks)
GET /api/v1/tasks/by-conv/<conv_id>      (scope: tasks; discovery)
```

- 未知 / 已过期的任务返回 **200 + `eventsAvailable:false`**（Request Inspector
  的诚实空态先例；前端绝不伪造零值火焰图）。
- 若只剩永久浏览器回执，返回 `eventsAvailable:true + source:"attempt-receipts"`；
  前端展示回执和“服务端明细不可用”说明，但不绘制虚假的服务端时间轴。
- 进行中的任务返回 `running:true`、`tEnd:null`，所有未闭合 span 以服务器
  当前时间收口——是「截至当前的快照」，UI 必须如实标注。
- 已结算任务优先读 owner-scoped attempt 永久快照；再生成后的旧 task 也不会被当前
  Turn 覆盖，低层事件即使已清理仍可追溯。
  `source:"turn-snapshot"` 与 `eventLogAvailable:false` 明示来源，不伪装成原始日志。
- 会话任务列表优先走 owner-scoped `turn.timing_trace.list`：只读 attempt 身份、状态与
  时钟，不读 trace JSON/消息；按 `(conversation_id, created_at, attempt_id)` 索引限页。
  因此热 TaskRuntime 淘汰、低层事件清理或全局 `task_results` 扫描封顶，都不会让仍在
  的 attempt 证据从排障入口消失。`task_results` 只补旧版/非 attempt 任务。
- 进行中才从事件权威读取：3 秒短缓存 + 写侧失效；`delta` 只读时间脊柱，
  不解析逐 token payload。公共端点/attempt 查询保留 owner 检查；内部事件投影沿用
  已授权 task ID 边界，不引入新的直接数据库访问。
- 事件读取失败不是「没有事件」：读者抛出 `TurnTraceEventReadError`，不返回空列表，
  失败也不进入短缓存。端点此时若有已结算 attempt 文档（含 `event-read-failed` 标记
  快照）返回 200 + 该文档，否则返回可重试的 **503**（与 Request Inspector 逐轮端点
  一致），绝不降级为 `eventsAvailable:false` 的「已过期」空态。缺 `summary` 的已结算
  文档不是永久权威：端点先重新折叠仍保留的事件日志，读取成功即返回真实 fold。
- `event.list(payload_types=...)` 在存储投影处只解码所需类型，其余保留时间/类型/序号。
  最多扫描 200,000 行；达到上限后独立读取最新边界和最多 1,000 个后续终态事件，
  不把已结束任务误判为 running，也不将截断明细标 full。

## 3. 线形（v1，`version: 1`；additive 演进，不 bump）

```jsonc
{
  "version": 1,
  "taskId": "…",
  "eventsAvailable": true,
  "eventLogAvailable": false,
  "source": "turn-snapshot", // event-log | live-projection | turn-snapshot | attempt-receipts
  "status": "done",            // done | error | aborted | running
  "running": false,
  "coverage": "full",          // full | partial
  "coverageReason": "flow",    // partial: flow | no-round-markers | event-limit | clock-regression | clock-domain-change | clock-unavailable | observer-gap | event-read-failed
  "clockSource": "server-monotonic", // server-monotonic | legacy-wall | mixed
  "tStart": 1724000000000,     // epoch 锚定的单调轴；旧记录从首行开始
  "tEnd":   1724000080000,     // epoch ms，终帧；进行中为 null
  "totalMs": 80000,            // 进行中 = now - tStart
  "summary": {
    "totalMs": 80000,
    "llmMs": 27000,            // 模型占用（含 ttft；已扣除其中等待段）
    "toolMs": 32000,           // 工具真实执行（已扣除审批/调度等待）
    "waitMs": 19000,           // 重试/限流/调度等待
    "compactionMs": 0,
    "approvalWaitMs": 0,
    "flowStepMs": 0,           // Flow 节点窗口内未被 llm/tool/wait 认领的调度/协调余量
    "orchestrationMs": 1500,   // 模型请求/工具/等待之外的 agent 循环工作（桶）
    "admissionWaitMs": 12000,  // waitMs 的分种类拆分（子分账，不是桶）：租约排队
    "retryWaitMs": 6000,       //   重试/限流退避
    "spawnWaitMs": 1000,       //   announce→spawn 调度余量；三者之和 === waitMs
    "startupMs": 900,          // orchestrationMs 的子分账：首个执行边界之前
    "roundPrepMs": 300,        //   round_start → 首个逻辑请求
    "roundSettleMs": 200,      //   末个逻辑请求 → 下一 round_start / 末轮 round_end
    "finalizeMs": 100,         //   末轮 round_end → 终态；四者之和 === orchestrationMs
    "unattributedMs": 500,     // 恒等于 gaps 之和 —— 严格账
    "ttftMs": 21300,           // 信息项：⊂ llmMs，不参与加总
    "overBudget": [{ "spanId": "r2.tool.c2", "kind": "tool",
                     "name": "write_file", "elapsedMs": 30000, "budgetMs": 15000 }]
  },
  "spans": [
    { "spanId": "r2.tool.c2", "parent": "r2", "depth": 2,
      "kind": "tool", "name": "write_file",
      "tStart": 1724000040500, "tEnd": 1724000070500,
      "status": "done",                       // done|error|aborted|running|unknown
      "attrs": { "toolName": "write_file", "query": "…", "roundKey": "2" },
      "budgetMs": 15000, "overBudget": true } // 仅声明了预算且超限时出现
  ],
  "gaps": [{ "tStart": 1724000008000, "tEnd": 1724000008200 }],
  "statusHistory": [{
    "id": "status.3", "phase": "stream_stalled", "attention": "stall",
    "tStart": 1724000030000, "tEnd": 1724000035000,
    "lastObservedAt": 1724000034000, "count": 2,
    "detailKey": "stream.phase.stalled", "detailArgs": {"seconds": 5}
  }],
  "clientObservations": [{
    "observationId": "p…:1", "kind": "phase_painted",
    "taskId": "…", "attemptId": "…", "clientId": "page-a",
    "phase": "stream_stalled", "serverEmittedAt": 1724000030000,
    "receivedAt": 1724000030125, "paintedAt": 1724000030160,
    "transportMs": 125, "renderMs": 35, "visibility": "visible"
  }],
  "droppedSpans": 0,
  "statusDroppedCount": 0,
  "clientObservationDroppedCount": 0,
  "overBudgetDroppedCount": 0,
  "compacted": false
}
```

`statusHistory` 保存当时可能显示的精确 `detailKey/detailArgs`、持续区间、重复次数和
`attention`（`progress | wait | stall`）。浏览器回执种类固定为
`phase_painted | terminal_painted | transport_degraded | transport_recovered`。
跨机器 `transportMs` 受墙钟偏差影响；超出可信窗口或出现负值时设置
`clockSkewSuspected`，不得把它当精确网络耗时。

旧库兼容：attempt 追踪上线前持久化的回执行没有 `attemptId`。会话同步读取适配层
（`lib/conversation_sync/service.py::_backfill_legacy_observation_attempt_ids`）在快照/分页
解码前把这些行的 `attemptId` 回填为空串，不回写存储；缺其他必填字段的行仍然解码失败。
新写入的回执始终携带真实 `attemptId`。

### Span 种类与归置规则

| kind | 开 → 关 | 说明 |
|---|---|---|
| `turn` | 首行 → `done`/`error` | 任务全程 |
| `round` | `round_start` → `round_end`；**工具子段会把足迹延长到工具落槌** | 工具在轮次间隙执行，归属作者轮 |
| `llm` | `round_start` → `round_end` | 轮次级包络段；有逻辑请求证据时其中的准备/结算窗口由 `round_prep`/`round_settle` 认领。`attrs.attempts` 挂本轮 `round_usage` join（`streamElapsedMs` 权威值） |
| `llm`（逻辑请求） | `model_request_start` → `model_request_complete` | `requestLevel:logical`；未标 level 的旧数据同样只证明逻辑请求；归属**开始时打开的轮次** |
| `startup` | 执行时钟起点 → 首个执行边界（`round_start`/`step_start`/`model_request_start`/`tool_start`/终态） | 父段 + 已命名阶段子段：`dispatch`（首条事件前）、`executor_queue`、`worker_start`、`workspace`、`translation`、`config`、`tools`、`memory_load`、`memory_select`、`context`、`handoff` |
| `round_prep` | `round_start` → 本轮首个逻辑请求开始（无请求则到 `round_end`/当前） | 仅在轨迹含逻辑请求证据时派生；与 `[RoundPrepTiming]` 同一窗口 |
| `round_settle` | 本轮末个逻辑请求结束（无请求证据则 `round_end`）→ 下一 `round_start`（末轮到其 `round_end`） | 工具派发、结果落盘与循环簿记；内部工具/等待保留自己的种类 |
| `finalize` | 末轮 `round_end`（或末个 `step_complete`）→ 终态事件 | 仅已结算任务；收尾结算 |
| `llm`（dispatcher 子段） | 同上 | `requestLevel:provider_attempt`、`attemptLevel:true`、`attemptScope:dispatcher`；按 `parentSpanId` 挂在逻辑请求下，每次调用独立 ordinal，失败先关闭再等待重试；不宣称覆盖 transport 内部每次 HTTP/路由尝试 |
| `llm_ttft` | `waiting_model` → 首个 `delta`/`tool_start` | 首 token 窗口 |
| `tool` | `tool_start.tStart` → `tool_result.tEnd`（缺时钟回退 `ts_ms`） | verdict 来自 `status`/`isError` |
| `retry_wait` | 真实 `retrying` phase → 下一实质事件；**同一重试合并为一段**（保留真实 attempt） | 429/退避；`waiting_model` / `stream_stalled` 心跳不入账 |
| `compaction` | `compacting` phase 或 `compaction` 事件 → `compaction_done` | 两条发射路径都折叠 |
| `approval_wait` | `write_approval_request` → 真实 spawn（`execStartTs`）或工具落槌 | 人的时间单列 |
| `spawn_wait` | `tool_start` → `execStartTs`，>500ms 且无审批才产出 | 调度/串行等待；`attrs`: `toolName`/`toolCallId`/`roundKey`/`query`（与父工具同值，父段被有界文档丢弃时仍自描述） |
| `admission_wait` | `workspace_admission_wait` 证据的 `waitStartedAtMs` → `waitEndedAtMs`（**止于准入，不是工具落槌**） | Tofu 自身工作区操作（依赖准备、集成快照）的租约排队，模型工具调用自 2026-10-28 起不取租约；有 `toolCallId` 时挂在该工具 span 下（并被夹在工具区间内），否则为 turn 级；`attrs`: `waiterTool`/`waiterCallId`/`waiterRoundKey`/`waiterQuery`（等待的调用实例，父段被丢弃时仍自描述）/`holderTool`/`holderConversation`/`heldMs`/`mode`/`lock`（`domain:basename`），证据存在时另有 `waiterClaims`/`holderTask`/`holderMode`/`holderClaims`/`holderCommand` |
| `flow_step` | `step_start` → `step_complete`（按 `node_id` 配对） | Flow 节点执行（Planner/Critic/…）；包裹子代理工作但只认领余量 |

`admission_wait` 是唯一由**等待所有者产生的精确证据**折叠的等待段：
`lib/project_mod/workspace_lease._publish_admission_wait` 在首次探测被阻塞时记下起点，
在准入/取消/拒绝时把一条 `AdmissionWait` 交给显式观察端口
`set_admission_wait_observer`（project_mod 不导入 tasks_pkg；由
`lib.tasks_pkg.executor` 在导入时安装）；任务层用任务执行时钟
（`ExecutionClock.at_monotonic`）把单调时刻投影成 `waitStartedAtMs/waitEndedAtMs`，追加
一条已注册的 `workspace_admission_wait` 事件。立即准入不产生任何证据；观察失败
永不影响准入；没有执行时钟的任务载体不猜时间，直接不发。证据在等待**结束**时
才发出，所以运行中快照里仍在排队的等待只表现为其工具 span（实时活性由
`_workspaceAdmission` 标记与 `[WorkspaceLease]` WARNING 负责）。后台化的命令在工具
落槌后仍在排队的部分不阻塞本 turn，被夹到工具结束。持有者是等待期间被观察到
阻塞最久的本进程持有者；跨进程 flock 的持有者未知，字段缺省。`heldMs`
是该持有者最后一次被观察到阻塞时已持有的时长（从其准入时刻起算）。
阻塞者也可能是等待者不得超越的更早排队请求：此时 `holderMode` 为
`queued-<mode>`，`heldMs` 是它已排队的时长。一条 span 同时写明两侧：
`waiterClaims`/`holderClaims` 是 `scope:basename[规则]` 标签（最多四个），
`holderCommand` 是阻塞者的一行命令摘要（不超过 160 字符），仅当阻塞者任务与
等待者同一属主时出现；决策见 `docs/runbooks/GIT_WORKSPACE_GUARD.md`
“Both sides named”。
问题：租约排队没有进程、没有输出，整段藏在工具 span 里被记成 `toolMs`，只能从
`[WorkspaceLease]` 日志手工重建。决定：由唯一的等待所有者产生证据，经显式端口
变成已注册事件，再由 fold 派生 span。否决的备选：各调用方自行计时（会漏掉文件写入与
MCP 准备等路径）、从日志反推（不是权威事件，轮转即丢）、project_mod 直接导入任务层
（违反分层）、把等待并入 `spawn_wait`（它只是 announce→spawn 的不可解释余量，不含
持有者，也覆盖不到不 spawn 进程的工具）。

工具 span 以 `(roundNum, toolCallId, occurrence)` 配对；裸 `toolCallId`
不是任务级主键。重复 ID 按事件出现次序结算，下一轮开始时仍缺终止事件的
旧 span 必须以 `unknown + truncated` 关闭，旧审批也不得附着到新轮调用。
Flow step span 以 `node_id` 配对；引擎 walk 是顺序的，新 `step_start`
出现时仍未闭合的旧 step 以 `unknown + truncated` 关闭，丢失起点的
`step_complete` 不伪造时长（窗口保持未归因）。运行中任务的未闭合 step
按服务器当前时间收口并标 `running`。
仍开放的审批子段在工具的隐式/终态边界同时关闭并分账；已结束审批不被重写，运行中
快照只截到当前时钟，已结算 trace 不得留 running/null-end 审批。

原始模型请求 ID 只作相关键，投影 `spanId` 在所有种类中按出现次序唯一化；完成事件
匹配最新活动的原始 ID，dispatcher 父引用指向当时活动的逻辑请求实例。没有活动父段
时保留旧兼容回退，不凭缺失事件猜测归属；轮次相关语义不变。

模型逻辑/dispatcher `durationMs` 用单调时钟，墙钟只定位时间线；顺序以 event sequence
为准。回拨时间被夹紧并标 `clock-regression`，不能把修正后的区间当精确时钟。
等待/停滞心跳不拆分真实退避，也不重开已存在的 TTFT；后续 TTFT 用独立 span ID。
缺失的 route 信息保持缺失，不推断秘密 URL、凭据或底层请求次数。

### 编排窗口与锚点身份

问题：2026-10-27 审计 12 个已结算任务（33,024 s），219.1 s 未归因，其中 181.5 s（83%）
在首轮之前：首条事件前的命令窗口、AI 任务队列、worker/工作区绑定、输入翻译、记忆库
加载（最长单次 10.4 s）；33.6 s 在轮次之间，3.5 s 在末轮之后。队列等待被记成重试等待；轮次级 llm 包络
段把轮次准备和模型返回后的工具派发/结算都算成模型时间；且三套编号互不对齐——
`round_start` 是 0 起的循环序号，`model_request_start`/`round_usage` 的 `roundNum` 是 1 起的
请求标签，工具事件的 `roundNum` 是逐调用分配号——导致请求与工具平铺在 `turn` 下、
用量挂到下一轮。

决定：不新增事件，由已有锚点派生命名窗口，归入新桶 `orchestrationMs` 及其子分账。
`startup` 覆盖执行时钟起点到首个执行边界，阶段由当时浏览器可见的启动提示
（`_STARTUP_STAGE_BY_DETAIL_KEY`）与 `memory_prefetch` 的 started/done 边界切分；
`executor_queued` 是 `executor_queue` 阶段而不再是 `retry_wait`（规则 `startup`，首轮后同
`ignore`）。有逻辑请求证据的轮次切出 `round_prep` 与 `round_settle`，优先级高于包络它们
的轮次级 llm 段、低于工具与等待；`finalize` 覆盖末轮结束到终态。派生窗口只认领其他
种类未认领的余量，故严格账不变。归属按事件发生时**打开的轮次**：请求与用量归开始
时的打开轮次，工具归打开轮次或刚结束的轮次；工具 `roundKey` 仍是配对用的载荷分配号。
等待段携带自身的调用/命令身份（见表），有界文档丢掉父工具段后仍可解释。

否决的备选：每个阶段新增 start/stop 事件（热路径上更多持久行，而启动提示已经定界）；
把启动留在未归因只加文档说明（83% 仍无法解释）；四个窗口各设顶层桶（与 `waitMs`
子分账先例不一致，UI 多四个筹码）；为执行队列加第四个等待子分账（队列只存在于 worker
进入前，作为启动阶段不改变等待不变量）；按载荷 `roundNum` 加减一推算归属（回退标签和
工具分配号会错，打开轮次天然精确）。

重放证据：用 `debug/turn_trace_audit.py --events` 重新折叠 3 个完整导出的事件日志（1,500.3 s），
未归因 43.9 s → 0.0 s；llm 1,175.5 s → 1,090.6 s；wait 103.1 s → 101.9 s（执行队列不再计为
重试）；新桶 orchestration 130.0 s = 启动 43.8 + 轮次准备 43.2 + 轮次结算 42.3 + 收尾 0.7。
fold 中位数：1,466 行 21.7 ms → 21.8 ms，1,242 行 4.2 ms → 4.5 ms。启动推进只处理首个执行
边界之前的行，轮次窗口为 O(轮次 + 请求)，无逐 token 工作。Spec：
`tests/turn_lifecycle/test_turn_trace_orchestration.py`。

### 严格账（契约不变量，CI 断言）

1. summary 桶是 turn 区间的**不相交划分**：按优先级
   `admission_wait > approval > compaction > retry_wait > spawn_wait > tool >
   round_prep > round_settle > llm > llm_ttft > flow_step > startup > finalize`
   每毫秒归一种 span，再归入桶（三种等待都进 `waitMs`；精确的租约排队压过
   只是包住它的审批/调度段，因为同一调用的审批先于准入完成；四种编排窗口都进
   `orchestrationMs`），`llmMs + toolMs + waitMs + compactionMs + approvalWaitMs +
   flowStepMs + orchestrationMs + unattributedMs === totalMs`（嵌套 span 不重复计——
   审批坐在工具里、429 坐在 LLM 窗口里、LLM/工具坐在 Flow step 窗口里）。
   `flowStepMs`/`orchestrationMs` 是 additive 字段，旧载荷缺省按 0 计。
   `admissionWaitMs + retryWaitMs + spawnWaitMs === waitMs` 与 `startupMs + roundPrepMs
   + roundSettleMs + finalizeMs === orchestrationMs` 是同一次分账的**子分账**，绝不再
   计入桶的加总；同为 additive，旧载荷缺失。
2. `gaps` = 未被任何 span 覆盖的区间，恒等于 `unattributedMs`。
   **任何黑洞必须显式存在，不许沉默。**
3. `coverage` 诚实标注推导不了的部分：Flow 词表出现但无可配对的
   `step_start`（旧 `endpoint_*` 日志或首个 step 前崩溃）、无轮次/节点
   边界、读上限截断、时钟回拨和 provider observer 丢失都是
   `partial`。多种问题同时存在时 `coverageReason` 优先显示回拨、读上限、observer 缺口。
   结算时事件读取失败标 `event-read-failed`，且不产出 summary/spans/gaps（见下文）。
4. 不相交 summary 是**按优先级分配的墙钟账**，不是所有并行阶段累计用时，也不是
   完整关键路径证明。LLM 父窗口不证明其中每毫秒都在上游计算；内层准备/调度仍可能
   未细分。`ttftMs` 是信息项，与等待可能重叠，不能再加到总耗时。

### 持久化与资源上限

- 每次 generation attempt 的 trace 是 durable user state；当前 Turn 的终态镜像便于
  普通会话快照展示，但不是旧 task 的查找权威。task/attempt replay 是可重建传输，
  依既有 TTL 清理，不得反向删除 attempt trace。
- 可发现性与明细同属 attempt 权威：列表查询最多返回 100 条元数据并用 `has_more`
  分页；partial index 只为非空 task ID 保存一条派生索引项，不复制 trace 内容。
- 浏览器回执与终态结算共用 attempt 锁：先到的回执被终态原子合并，后到的回执在
  已冻结文档上追加。单条回执只更新有界 `timing_trace_json`，不推进 Turn/Conversation
  revision、不写 conversation-sync change，也不产生逐条 command receipt。
- **结算时事件读取失败（2026-10-27）。** 问题：读者把存储错误（`database_protocol_error`）
  吞成空列表，fold 只看到待写的终态事件，把一次 10,586 s、实际 217 轮的运行冻结成
  「0 轮、整段未归因」的终态快照；端点优先返回快照，任务从此永远显示 0 轮。决定：读失败抛出类型化错误；终态结算冻结一份显式标记的 attempt
  文档——`coverage:partial`、`coverageReason:event-read-failed`、`eventLogAvailable:false`，
  只保留不依赖这次读取的证据：终态 status、终态事件执行时钟给出的 `tStart/tEnd/totalMs`
  （无时钟时只有 `tEnd`、`clockSource:legacy-wall`）、进程内提示历史与浏览器回执；
  **不写** `summary/spans/gaps`。因此端点不把它当永久权威，而是重新折叠仍保留的事件
  日志（结构行按既有 TTL 保留），日志不可读或已清理时才返回这份如实标注的文档；
  同一终态投影重建（CAS rebase）若读取成功，则冻结正常 fold。否决的备选：跳过持久化
  （终态会留下 `running:true` 的运行中镜像，或丢掉提示历史/回执这些 durable 证据）；
  持久化退化 fold 只加标记（`summary` 让端点继续把它当权威，审计工具也会把整段计为
  未归因）；结算时重试读取（拖慢结算，对确定性失败无效）；读取成功后由 GET 回写修复
  （需要 Sidecar 与原生 TofuDB 新增存储写操作，超出本修复）。
- 每个文档最多 256 spans、128 gaps、128 提示、64 浏览器回执和 96 KiB。
  超限保留首尾或最近证据，并累计 `dropped*`；聚合 summary 与顶层时钟不丢。
- 最终字节守卫会先收缩明细、清空 verbose span attrs；若各车道同时达到最坏字段
  长度，继续在保留聚合真值的前提下淘汰行和超预算工作项，直至序列化结果确实
  `<= 96 KiB`，并设置 `detailCompacted/compacted` 与累计 dropped 计数。UI 必须
  披露，不得把压缩后证据说成完整日志。

### 聚合工作预算与观察隔离

问题：一个 921 行 fold 同时读取时钟证据、配对事件、闭合 span 并生成汇总，
局部修改难以核对丢失事件和重复相关 ID 的处理顺序。决定：`turn_trace.py`
保留读取、时钟归一、覆盖率、严格分账和持久化入口；`turn_trace_spans.py`
只把已排序、已归一的事件投影为 span，每种事件有显式处理入口。其发生实例状态
仅属于一次读取，不引用 Task，不执行工具，不写存储，不形成第二套运行状态。
预算和 phase 策略由入口显式传入；公开入口和故障注入边界保持不变。
否决的备选：各事件另设独立 reducer 会分散轮次/工具/审批闭合关系；仅搬走
整块长函数仍无法单独审查模型请求、工具和 Flow 的配对规则。验证同时比较
未压缩完整树、有界文档和 ledger，不能只比较已截断的展示结果。

工具配对按 `(roundNum, toolCallId)` 索引仍开放的发生实例，组内保持声明顺序；
闭合同时移除两份索引中的引用，不复制 span 正文。旧日志缺少选择字段时继续按
原规则查找，完全无选择字段且有多个候选时拒绝猜测。审批和实际启动仍各自选择
最早未匹配实例，相关 ID 重用不会覆盖旧 span。问题是先前每次进度、准入和完成
都扫描整个未完成列表；仅改成生成器仍会随工具数量反复线性扫描。
`benchmarks/turn_trace_compare.py` 保留冻结源码对照、106 组完整/有界/ledger 一致性
检查及交替采样。1,024 工具、4,462 事件的合成输入中，fold CPU 中位数由
280.12 ms 降至 62.27 ms；单工具约 0.53 ms 持平，独立测量的临时分配峰值由
5.81 MB 到 5.96 MB。索引随本次读取释放，不改变持久化大小或事件保留策略。
这只衡量投影 CPU，不包含存储读取、真实工具耗时或整段对话延迟。

`turn_trace_ledger.py` 用每区间两个边界的扫描线，一次排序加固定优先级计数，分账为
`O(n log n)` 时间、`O(n)` 临时内存；随机参考对照和 10,000 span 访问计数测试保证
结果不变。这个复杂度只覆盖分账器，不宣称完整 fold 的所有历史路径都线性。

整个 read→fold→有界序列化另有无内容 `[TurnTraceTiming]` 单调计时；超过 1 秒或失败
才记 INFO，其余 DEBUG。该指标不是任务执行的额外 span，日志失败不能掩盖原始错误。

终态结算（`finalize_trace_projection`）每个任务只记一条 INFO
`[TurnTraceSummary] task=<完整 id> conv=… status=… totalMs=… llmMs=… toolMs=… waitMs=…
admissionWaitMs=… retryWaitMs=… compactionMs=… approvalWaitMs=… unattributedMs=… ttftMs=…
rounds=… spawnWaitMs=… flowStepMs=… orchestrationMs=… startupMs=… roundPrepMs=…
roundSettleMs=… finalizeMs=… coverage=…`。所有数字来自被冻结的同一次 fold：
桶与 `waitMs`/`orchestrationMs` 的拆分都取自文档 summary（同一次不相交分账），只有
`rounds` 取自 `ledger_out`，故 `admissionWaitMs + retryWaitMs + spawnWaitMs == waitMs`、
`startupMs + roundPrepMs + roundSettleMs + finalizeMs == orchestrationMs`。同一任务的终态
投影重建（CAS rebase、保留回执重试）不重复记录。线形与聚合工具见 `docs/LOGGING.md`。
问题：并行工具同时排队时，各等待 span 的原始求和会超过墙钟，无法与 `waitMs` 对账。
决定：拆分沿用 summary 的同一优先级不相交分账（单个工具的串行等待与原始求和相等）。
否决的备选：原始 span 求和（并行重复计）；在 summary 中新增桶（破坏桶的不相交
加总）；从有界文档重算（256 span 上限会丢段，只是下界）。

拆分为什么也持久化到 summary：问题——长任务必然超过 256 span；2026-10-25 按 API 审计最近 15 个
已结算任务（62,750 s 墙钟），12 个 `droppedSpans>0`，4,886 s（7.8%）的 `waitMs` 无法归到
租约/重试/调度中的任何一种，而精确的 `[TurnTraceSummary]` 日志只保留约两天，并发与并发修复
的效果因此无法从持久证据证明。决定——`TurnTimingSummary` 增加可选的
`admissionWaitMs/retryWaitMs/spawnWaitMs`，它们是 `waitMs` 的子分账而不是新桶，和聚合 summary
一样在所有字节守卫（含绝对兜底）下保留；审计工具优先读它，只有旧文档才从保留的 span 重算
下界。否决的备选：继续只写日志（轮转即丢）；提高 span 上限（只推迟丢段，还增大每个文档）；
把等待 span 单独免于淘汰（仍受 96 KiB 字节守卫约束，且三个整数就是全部所需）。绝对兜底
按固定键表重建 summary，此前该表漏掉了 `flowStepMs`，现在一并保留；`orchestrationMs` 及其
四个子分账同样在键表内。

结算时事件读取失败就没有 fold 可报：该行只有 `task/conv/status/coverage=partial
coverageReason=event-read-failed`，不含任何可被求和的数字（`debug/turn_trace_audit.py --logs`
缺必需字段即跳过，不会把整段计为未归因）；同一终态投影重建若读取成功，再记一次
正常行。读失败本身由读者记一条 WARNING `[TurnTrace] Sidecar event read failed task=<完整 id>
code=… message='…'`（`describe_storage_failure` 渲染，截断到 400 字符），不再只有错误码。

provider ingress 的持久化→push 由现有 256 项有界队列隔离。退出用 stop event 加
非阻塞唤醒，再最多 join 30 秒；满队列不能卡在 sentinel 入队，也不为了退出额外
丢事件。正常保留项 FIFO；已卡住的同步回调不能被强停。逻辑完成携带累计
`observerIsolation.droppedEvents/deliveryFailures/diagnosticFailures/flushIncomplete`；任何缺口导致
`observer-gap`。超时后的回调仍可能迟到，不能承诺已恢复顺序。累计正文收敛不能重建
丢失的独立尝试 span，此处完整性与任务正文持久性是不同判断。

### 预算表（声明，不是判决）

`_TOOL_BUDGETS_MS` / `_KIND_BUDGETS_MS`（turn_trace.py）声明**有预期的**
环节上限：`None` = 火焰图不做超预算判决（LLM、用户 run_command 负载、审批
人的时间、上游限流等待、等待别人负载的租约排队、包裹子代理工作的 Flow step）；这不等于传输层无限等待。模型请求可由
`TOFU_LLM_IDLE_STREAM_TIMEOUT_S` 的**滚动传输空闲窗口**约束——该窗口**默认关闭（`0`），部署方显式设置为正值才启用**，而不是受请求
总墙钟约束。收到任意 SSE 字节/注释/keepalive 或 WebSocket 消息都会续期；reasoning、
正文与工具增量仍记录为语义诊断，但“没有语义进展”不再终止请求。只有“当前单调时钟 −
最近传输活动时钟”达到窗口才关闭 attempt，并按 `premature_close` / `midstream_close`
进入既有的有界流中断重试。只要上游持续发事件，单次请求就不设额外总墙钟，用户 Stop
始终有效。`semantic_progress_timeout`、`semanticProgressTimeout`、
`lastSemanticProgressAgeMs`、`semanticStallWindowMs` 与 `no_actionable_output` 仅用于
历史记录/插件兼容；新的 live attempt 不会产生它们。旧环境变量
`TOFU_LLM_SEMANTIC_IDLE_TIMEOUT_S` / `TOFU_LLM_NO_ACTIONABLE_TIMEOUT_S` 只作为新传输
空闲时长的废弃别名读取，keepalive 会续期；
本地确定性工具（read_files/write_file/edit_file/search_files/glob_search/
list_dir）有保守上限，超限只挂 `overBudget:true`——**它是日后优化的工作清单，
不是错误**。未声明的工具不携带 `budgetMs` 键（沉默不是判决；要表态就加一行）。

---

## 4. 前端呈现（Request Inspector 抽屉 · 耗时分析）

- 执行中的命令卡片只格式化后端最近一次 `tool_progress` 采样的 `execElapsedMs` /
  `deadlineRemainingMs`，`execTimingAtMs` 保留采样点。无新进度时数值不推进，tooltip
  明示后端采样；旧记录没有这组证据时不补客户端计时。浏览器不调用 `Date.now()`
  推算命令耗时，也不通过任务轮询补造定时事件。现有 watcher 触发倒计时不属于执行证据。
- 完成栏只格式化同一 task、终态、完整 `server-monotonic` trace 的 `totalMs`；
  缺失/部分/旧时钟证据不通过 Turn 生命周期时间相减补出耗时。
- **入口**：Request Inspector 扁平消息流中，每个 Turn 分隔栏的「耗时分析」
  控件打开该 task 的 Trace，返回控件恢复消息流。前端只投影端点折好的 span 树，
  不自行推导（reducer）。读取与当前面板身份归 `inspector-details.ts`；
  `inspector-trace-projection.ts` 复用生成的 `TurnTimingTrace` 类型，
  `inspector-trace-view.ts` 通过公共 `safeHtml` 渲染。
- **形态**：时间轴瀑布火焰图（x 轴 = 真实时间，行 = span 深度），不是经典
  聚合火焰图——「先等模型 40s 再跑工具 2s」的时序故事正是用户要的。
  无图表库依赖，绝对定位 div 自绘。所有实际返回的 span 深度均保留，
  不截断第四层之后的 provider attempt，也不为空缺的中间深度分配行。
  起点与总时长必须是有限非负数且 summary 存在才展示服务端轴；零是有效值。
  只有浏览器回执或时钟缺失时展示来源提示和已有证据，不补造服务端轴。
- **颜色即种类**：模型/首 token 使用信息色，工具使用成功色，等待/审批使用
  警示色，压缩使用强调色，编排使用次要文字色，未归因使用斜纹；error 描边、
  超限虚线、进行中条纹、截断断边独立保留。颜色与文字消费主题语义 token，
  Trace 样式由 diagnostics 模块按需加载。
- **summary 芯片行**直接回答「时间花哪了」；`overBudget` 名单单列一行警示。
- 额外展示用户可见提示的区间与精确文案，以及浏览器收到→绘制、连接降级→恢复
  回执。终态永久来源与明细压缩必须披露。
- 诚实标注：进行中快照、`coverage:partial`、永久快照来源、压缩计数和
  `eventsAvailable:false` 空态。

### 明确不做（本批边界）

- **客户端执行 fold**：进行中火焰图仍来自服务端端点快照；客户端只提交感知
  回执，不重新发明 span 归置或完成判断。
- **swarm 子代理内部**：`_suppressEvents` 使子代理工具执行不进父任务日志，
  火焰图呈现的是父循环视角；子代理内部耗时是独立后续 epic。
- **conv 级聚合**：task 轴（Request Inspector 裁定），跨任务汇总后续再说。
