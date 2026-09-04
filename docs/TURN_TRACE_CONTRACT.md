# Turn Trace 契约 —— 可事后追溯的用户感知耗时

本文是「一次运行每阶段花了多久、用户当时看到了什么提示、浏览器是否真的及时收到并绘制」
的事实源。机器可读线形由 `contracts/conversation_sync_v3.yaml` 的 `TurnTimingTrace`
拥有；`lib/tasks_pkg/turn_trace.py` 负责投影，每个 generation attempt 是永久权威，
当前 Turn 只保留终态镜像，Request Inspector 通过 task id 读取并合并该文档。关键测试是
`tests/test_turn_trace.py`、
`tests/test_turn_event_carried_task_event.py`、`tests/test_frontend_perception_recorder.py`
和 `tests/test_frontend_turn_trace.py`。

---

## 1. 两条证据车道，一个 attempt 持久化文档

**服务端执行耗时从权威事件推导，不产生第二套执行事实。** `round_start` /
`round_end`、工具时钟、模型请求边界和每行服务器时间共同折叠 span。每个已注册
chat phase 必须在 `_PHASE_TRACE_RULE` 中声明归置规则，漂移测试双向钉死。

**用户感知只能由浏览器证明。** 会真正影响体验的 phase、终态和连接健康变化在
现有 reducer/render 完成后等待下一次绘制机会，再通过
`POST /api/v3/conversations/{conversationId}/turns/{turnId}/perception`
提交 `receivedAt` / `paintedAt` 等无内容回执。只有双 `requestAnimationFrame`
确认过可见绘制机会才记为 painted；隐藏/阻塞页的 2 秒兜底只释放有界槽位并累计
丢失数，不伪造成用户已看到。客户端时钟只产生 `renderMs` 和带
时钟偏差标记的 `transportMs`，绝不改写服务端 span、状态或结算。

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
  不解析逐 token payload。所有仓储查询显式携带 `user_id`。

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
  "coverageReason": "flow",    // 仅 partial 时：flow | no-round-markers
  "tStart": 1724000000000,     // epoch ms，首行事件
  "tEnd":   1724000080000,     // epoch ms，终帧；进行中为 null
  "totalMs": 80000,            // 进行中 = now - tStart
  "summary": {
    "totalMs": 80000,
    "llmMs": 27000,            // 模型占用（含 ttft；已扣除其中等待段）
    "toolMs": 32000,           // 工具真实执行（已扣除审批/调度等待）
    "waitMs": 19000,           // 重试/限流/调度等待
    "compactionMs": 0,
    "approvalWaitMs": 0,
    "unattributedMs": 2000,    // 恒等于 gaps 之和 —— 严格账
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

### Span 种类与归置规则

| kind | 开 → 关 | 说明 |
|---|---|---|
| `turn` | 首行 → `done`/`error` | 任务全程 |
| `round` | `round_start` → `round_end`；**工具子段会把足迹延长到工具落槌** | 工具在轮次间隙执行，归属作者轮 |
| `llm` | `round_start` → `round_end` | `attrs.attempts` 挂 `round_usage` join（`streamElapsedMs` 权威值） |
| `llm`（attempt 子段） | `model_request_start` → `model_request_complete` | 位于轮次 `llm` 下；`attrs.attemptLevel:true`，携带 `routeId` / `routeMode` / `routeDecision` / `failureStage`，异常流不得标成成功 |
| `llm_ttft` | `waiting_model` → 首个 `delta`/`tool_start` | 首 token 窗口 |
| `tool` | `tool_start.tStart` → `tool_result.tEnd`（缺时钟回退 `ts_ms`） | verdict 来自 `status`/`isError` |
| `retry_wait` | 真实 `retrying` phase → 下一实质事件；**同一重试合并为一段**（保留真实 attempt） | 429/退避；`waiting_model` / `stream_stalled` 心跳不入账 |
| `compaction` | `compacting` phase 或 `compaction` 事件 → `compaction_done` | 两条发射路径都折叠 |
| `approval_wait` | `write_approval_request` → 真实 spawn（`execStartTs`）或工具落槌 | 人的时间单列 |
| `spawn_wait` | `tool_start` → `execStartTs`，>500ms 且无审批才产出 | 调度/串行等待 |

工具 span 以 `(roundNum, toolCallId, occurrence)` 配对；裸 `toolCallId`
不是任务级主键。重复 ID 按事件出现次序结算，下一轮开始时仍缺终止事件的
旧 span 必须以 `unknown + truncated` 关闭，旧审批也不得附着到新轮调用。

### 严格账（契约不变量，CI 断言）

1. summary 桶是 turn 区间的**不相交划分**：按优先级
   `approval > compaction > wait > tool > llm` 每毫秒归一类，
   `llmMs + toolMs + waitMs + compactionMs + approvalWaitMs + unattributedMs
   === totalMs`（嵌套 span 不重复计——审批坐在工具里、429 坐在 LLM 窗口里）。
2. `gaps` = 未被任何 span 覆盖的区间，恒等于 `unattributedMs`。
   **任何黑洞必须显式存在，不许沉默。**
3. `coverage` 诚实标注推导不了的部分：Flow 任务（Planner/Critic 不在
   主循环词表内）与无轮次边界的早期日志都是 `partial`。

### 持久化与资源上限

- 每次 generation attempt 的 trace 是 durable user state；当前 Turn 的终态镜像便于
  普通会话快照展示，但不是旧 task 的查找权威。task/attempt replay 是可重建传输，
  依既有 TTL 清理，不得反向删除 attempt trace。
- 可发现性与明细同属 attempt 权威：列表查询最多返回 100 条元数据并用 `has_more`
  分页；partial index 只为非空 task ID 保存一条派生索引项，不复制 trace 内容。
- 浏览器回执与终态结算共用 attempt 锁：先到的回执被终态原子合并，后到的回执在
  已冻结文档上追加。单条回执只更新有界 `timing_trace_json`，不推进 Turn/Conversation
  revision、不写 conversation-sync change，也不产生逐条 command receipt。
- 每个文档最多 256 spans、128 gaps、128 提示、64 浏览器回执和 96 KiB。
  超限保留首尾或最近证据，并累计 `dropped*`；聚合 summary 与顶层时钟不丢。
- 最终字节守卫会先收缩明细、清空 verbose span attrs；若各车道同时达到最坏字段
  长度，继续在保留聚合真值的前提下淘汰行和超预算工作项，直至序列化结果确实
  `<= 96 KiB`，并设置 `detailCompacted/compacted` 与累计 dropped 计数。UI 必须
  披露，不得把压缩后证据说成完整日志。

### 预算表（声明，不是判决）

`_TOOL_BUDGETS_MS` / `_KIND_BUDGETS_MS`（turn_trace.py）声明**有预期的**
环节上限：`None` = 火焰图不做超预算判决（LLM、用户 run_command 负载、审批
人的时间、上游限流等待）；这不等于传输层无限等待。模型请求全程受
`TOFU_LLM_IDLE_STREAM_TIMEOUT_S` 的**滚动传输空闲窗口**约束，默认 300s，而不是受请求
总墙钟约束。收到任意 SSE 字节/注释/keepalive 或 WebSocket 消息都会续期；reasoning、
正文与工具增量仍记录为语义诊断，但“没有语义进展”不再终止请求。只有“当前单调时钟 −
最近传输活动时钟”达到窗口才关闭 attempt，并按 `premature_close` / `midstream_close`
进入既有的有界流中断重试。只要上游持续发事件，单次请求就不设额外总墙钟，用户 Stop
始终有效。`semantic_progress_timeout`、`semanticProgressTimeout`、
`lastSemanticProgressAgeMs`、`semanticStallWindowMs` 与 `no_actionable_output` 仅用于
历史记录/插件兼容；新的 live attempt 不会产生它们。旧环境变量
`TOFU_LLM_SEMANTIC_IDLE_TIMEOUT_S` / `TOFU_LLM_NO_ACTIONABLE_TIMEOUT_S` 只作为新传输
空闲时长的废弃别名读取，keepalive 会续期；
本地确定性工具（read_files/write_file/edit_file/grep_search/glob_search/
list_dir）有保守上限，超限只挂 `overBudget:true`——**它是日后优化的工作清单，
不是错误**。未声明的工具不携带 `budgetMs` 键（沉默不是判决；要表态就加一行）。

---

## 4. 前端呈现（Request Inspector 抽屉 · 耗时分析）

- **入口**：任务摘要卡与技术详情之间的「耗时分析」行（两级轴不变：
  conv → task → [请求轮次 | 耗时分析]）。前端只渲染端点折好的 span 树，
  不自行推导（reducer）。
- **形态**：时间轴瀑布火焰图（x 轴 = 真实时间，行 = span 深度），不是经典
  聚合火焰图——「先等模型 40s 再跑工具 2s」的时序故事正是用户要的。
  无图表库依赖（仓库 vanilla JS 纪律），绝对定位 div 自绘。
- **颜色即种类**：模型蓝 / 首 token 青 / 工具绿 / 等待橙 / 压缩紫 /
  审批黄 / 未归因灰斜纹；error 红边、超限红虚线、进行中条纹、截断红断边。
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
