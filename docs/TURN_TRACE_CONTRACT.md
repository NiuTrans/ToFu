# Turn Trace 契约 —— 任务级耗时结构的唯一权威稿

> 状态：**owner 已拍板方向（2026-08-20）：推导派 + Request Inspector 标签页 + 全链路一批**。
> 本文是「一个任务的每个阶段花了多久、在做什么」这一问题答案的**单一事实源**。
> 实现：`lib/tasks_pkg/turn_trace.py`（推导器）、`GET /api/v1/tasks/<id>/trace`（端点）、
> Request Inspector 抽屉「耗时分析」入口（前端唯一渲染处）。
> 测试：`tests/test_turn_trace.py` + `tests/test_frontend_turn_trace.py`。

---

## 1. 架构裁定（为什么是推导，不是埋点）

**耗时结构从持久化事件日志推导，不产生第二份事实。**

`task_events` 表已经持久化了每个事件（durable-before-visible，6h TTL），且
自带三样推导所需的全部材料：每个边界（`round_start`/`round_end`）、每个时钟
（工具帧的 `tStart`/`tEnd`/`execStartTs`）、每行的服务器 `ts_ms`。
因此推导器是一个纯函数 fold：**耗时永远与「实际发生了什么」同源，不可能漂移**；
6h 窗口内的历史任务无需等待埋点上线即可出图。

这条裁定是项目 charter 的直接推论（EPIC_SEGMENT_TIMELINE §1.1，owner 2026-07-11）：
后端把决策算好、以类型化事实下发；前端是 reducer，**绝不从客户端瞬时状态推断耗时**。

与事件契约的关系：词表不新增任何事件类型；已有的
`model_request_start` / `model_request_complete` 诊断边界也参与推导，使每次
模型请求的实际网络路径与失败阶段可追溯。相反，**每个已注册的 chat 域 phase
必须在 `_PHASE_TRACE_RULE` 中声明归置规则**（`ttft | retry_wait | compaction |
covered | ignore`），`tests/test_turn_trace.py` 的 drift 守卫双向钉死——
这就是「做稳定做长期」的机制保障。

---

## 2. 端点

```
GET /api/v1/tasks/<task_id>/trace        (scope: tasks)
```

- 未知 / 已过期的任务返回 **200 + `eventsAvailable:false`**（Request Inspector
  的诚实空态先例；前端绝不伪造零值火焰图）。
- 进行中的任务返回 `running:true`、`tEnd:null`，所有未闭合 span 以服务器
  当前时间收口——是「截至当前的快照」，UI 必须如实标注。
- 读路径与 Request Inspector 同纪律：lane-aware（sidecar / legacy +
  write-behind 影子合并）、3s 短 TTL 缓存 + 写侧失效（read-your-writes）。
  `delta` 行只读 ts 脊柱（TTFT 边界），不解析每 token 的 payload。

## 3. 线形（v1，`version: 1`；additive 演进，不 bump）

```jsonc
{
  "version": 1,
  "taskId": "…",
  "eventsAvailable": true,
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
  "gaps": [{ "tStart": 1724000008000, "tEnd": 1724000008200 }]
}
```

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

### 严格账（契约不变量，CI 断言）

1. summary 桶是 turn 区间的**不相交划分**：按优先级
   `approval > compaction > wait > tool > llm` 每毫秒归一类，
   `llmMs + toolMs + waitMs + compactionMs + approvalWaitMs + unattributedMs
   === totalMs`（嵌套 span 不重复计——审批坐在工具里、429 坐在 LLM 窗口里）。
2. `gaps` = 未被任何 span 覆盖的区间，恒等于 `unattributedMs`。
   **任何黑洞必须显式存在，不许沉默。**
3. `coverage` 诚实标注推导不了的部分：Flow 任务（Planner/Critic 不在
   主循环词表内）与无轮次边界的早期日志都是 `partial`。

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
- 诚实标注三件套：进行中快照提示、`coverage:partial` 说明、
  `eventsAvailable:false` 空态（复用 `ri.expired`）。

### 明确不做（本批边界）

- **live 车道**：进行中任务的火焰图来自端点快照（3s 缓存粒度），不做
  客户端逐事件实时折叠——客户端不重新发明归置规则。
- **swarm 子代理内部**：`_suppressEvents` 使子代理工具执行不进父任务日志，
  火焰图呈现的是父循环视角；子代理内部耗时是独立后续 epic。
- **conv 级聚合**：task 轴（Request Inspector 裁定），跨任务汇总后续再说。
