# 执行模式统一：一张图上的执行流与上下文管理

> **文档性质**：临时工作文档（working plan），记录 2026-09-20 的架构统一调查结论与收编计划。
> 目标架构落地后，仍成立的契约与 rationale 应迁入 `docs/` 权威文档（见 `docs/README.md` 文档生命周期），本文档随即删除。
> 关联文档：`docs/TASK_EXECUTION_MODES.md`、`docs/modules/task_engine.md`、`docs/modules/orchestration_dag.md`、`docs/modules/evolution.md`。

---

## 1. 目标（Vision）

所有执行模式——常规聊天、Plan mode、Goal mode、Swarm 子代理——本质上是**同一张图**上的两种规定的组合：

- **执行流**：节点是 worker（一次 invocation），边是消息注入。
- **上下文管理**：每个节点声明自己的上下文组装策略、工具权威、结算判停规则。

模式差异应归约为**图节点 = 同一个 invocation + 四个声明式策略**：

| 策略 | 常规聊天 | Plan mode | Goal mode | Swarm 子代理 |
|---|---|---|---|---|
| **user 源**（对面是谁） | 人类 | 人类 | VU（agent 模拟） | 父 worker |
| **上下文组装** | 全量会话历史 | 全量会话历史 | launch 上下文 + 迭代累积工作集 | objective+context 单条 + 角色 prompt |
| **工具权威** | 全量 | 只读 fail-closed | 全量 − ask_human | 角色 scoped − denylist |
| **结算/判停** | turn 完成 | 铸造 proposedPlan | verdict 门禁（机审） | 结果交付父节点 |

**前后端不该因模式不同而有不同实现**：不同的只是图配置，不是执行引擎、存储实体、事件词汇或 UI 组件树。

---

## 2. 现状调查结论（2026-09-20，证据见 §7）

### 2.1 已经统一的部分（不要重做）

执行内核早已统一，这是全部工作的地基：

- Chat 直进 `execute_invocation`；Goal 图角色与 Swarm `SubAgent` 都经 `create_child_invocation → run_child_invocation → execute_invocation`（`lib/tasks_pkg/child_invocation.py`、`lib/tasks_pkg/invocation.py:19-58`）。
- 同一个 `run_agent_loop`、工具 gateway、compaction、checkpoint、事件管线。Swarm 无私有请求构建/模型派发/工具池（有 `tests/swarm/test_swarm_agent_loop_chassis.py` 守护）。
- Plan mode 不是独立循环：只是 `planMode: true` 在 dispatch 层只读拒绝（`lib/tasks_pkg/tool_dispatch/_pipeline.py:1809-1826`）+ 终态铸造 `proposedPlan` sidecar（`lib/turn_lifecycle.py:1564-1575`）。
- 角色目录单一来源：Flow 的角色/工具/tier 路由直接复用 `lib/swarm/registry.py`。

**结论：分裂不在执行层，在"图没有实体化"。**

### 2.2 阻碍统一的六堵墙

1. **子代理/分支不是一等实体**：swarm 子代理是 transient task（不落 turn/conversation 权威），持久化走私有的 `swarm_sessions`/`swarm_agents` 表 + 磁盘日志 + 进程内注册表（`lib/swarm/persistence.py`、`lib/swarm/integration/_state.py:63-66`）；而 goal 图节点反而投影为普通 v3 turn（`lib/orchestration/chat_turn_sync.py`）。同是"分支"，两套实体模型。
2. **结果注入走三条私有协议而不是"边"**：swarm 的 `agent_inbox`+`<swarm-update>` 合成 user 消息（`lib/tasks_pkg/orchestrator/_swarm_inbox.py`）、goal 的 `goal_worker_review` 注入块（`lib/goal_runs/worker_context.py:65-76`）、evolution 的 coordination journal（`lib/evolution/coordination.py:120-150`）。
3. **"user 换成 agent"被实现了三遍**：Chat GoalRun（Flow 图 + VU 哨兵，`lib/goal_runs/` + `lib/orchestration/`）、Evolution runs（judge_panel JSON + 独立 stage 状态机 + 周期 sweep，`lib/evolution/runs.py`）、Legacy autopilot（约 3400 行已退役但仍接线的第三套，`lib/tasks_pkg/autopilot.py` 等）。概念同构，代码零共享。
4. **调度/生命周期未统一**：swarm 私有 `MasterOrchestrator`+`StreamingScheduler` 线程池；evolution stage 机 + 90s sweep（`lib/server_loop_runtime.py`）；chat 走 `spawn.py` 有界 executor。图的遍历器有三种。
5. **事件与前端词汇分裂**：普通 turn 标准 v3 事件流；swarm 一族 `swarm_*` 事件 + 7 个专属前端文件做嵌套面板；goal 靠 `_isVirtualUser`/`turnRole` 等 marker 特判；evolution 整个独立 workbench（`frontend/src/features/evolution/`）。
6. **模式多重表示 + 防御性重复归一化**：`planMode` 同存三处（settings、attempt config、turn actor/kind）；`normalize_interaction_mode_runtime_config` 在 ≥5 个边界重复调用；前端双布尔 ↔ 三态 radio 镜像归一化。

### 2.3 Flow 引擎 vs Swarm 深对比结论

引擎设计意图本就包含统一："It unifies verifier loops and swarm fan-out under one declarative engine"（`lib/orchestration/engine.py:5-6`）。

**关键不对等**：

| 维度 | Flow | Swarm |
|---|---|---|
| 图模型 | **静态图**：构造期校验+展开，之后不可变（`engine.py:178-264`） | **动态图**：模型运行时决定 fan-out 数量/objective/depends_on，可追加波次 |
| 调度 | 单线程阻塞解释 + parallel fork-join（最慢分支门控） | 流式调度：依赖就绪即启动、完成即回传、守护线程驱动 |
| 父子通信 | 阻塞等待 + typed I/O；无 any-of 等待、无 inbox、无 fire-and-forget | inbox + await_agents(any/all, 60s 帽) + get_agent_result + auto-continue |
| 崩溃恢复 | 非终态 run 启动时判死 `worker_lost`（`startup_recovery.py:16-42`），**无 mid-run 续跑** | 轮级 checkpoint + 启动 rehydrate 续跑 + 未投递结果重投（`master.py:1254-1349`） |
| 预算 | 代理计数预算（默认 200） | token/tool_calls/deadline 按波切片 + usage rollup |
| 僵死检测 | 行为守卫（zero-deliverable/stuck/no-progress） | ProgressBeacon 900s 静默 + 工具心跳 |

**可行性判断：部分可行。**

- **形态（a）同步动态 fan-out 节点**：新增 control kind（如 `fanout`），节点内由模型产出 specs → 嵌入 `StreamingScheduler`（其构造全注入式，`scheduler.py:67-151`，可直接嵌入）→ fork-join 收敛进 dataflow。**中改**，覆盖绝大多数实际用例（并行研究→综合）。扩展缝明确：`CONTROL_KINDS` 注册表三处（`_control_specs.py:22-52`、`engine.py:446-468`、validation）。
- **形态（b）异步挂起/恢复节点**：保留 inbox/await/auto-continue/轮级续跑全语义。**大改**，前置条件是 Flow run 崩溃恢复升级为可恢复 + 解决 run 生命周期与跨 turn 会话冲突。
- human gate 已证明引擎可在节点内挂起等外部条件恢复（`human_gate_runtime.py:64-120`），是 await 式节点的现成模板。

完整能力差集（Swarm 独有 15 项 A1–A15、Flow 独有 10 项 B1–B10）见调查存档，关键项已反映在下文计划中。

---

## 3. 目标架构

```
图（Graph）
 ├─ 节点 = Invocation + 四策略声明
 │    ├─ user 源策略：human | virtual_user(persona) | parent_worker
 │    ├─ 上下文策略：full_history | conversation_workset | handoff_seed
 │    ├─ 工具权威策略：full | read_only | role_scoped(allowlist) 
 │    └─ 结算策略：turn_complete | plan_mint | verdict_contract | deliver_to_parent
 ├─ 边 = 统一的 durable message 投递通道
 │    （VU 评审、swarm 结果、coordinator updates、人类消息全是同一种边）
 ├─ 遍历器 = 一个图运行时（FlowExecutor）
 │    fan-out / loop / branch / await 全是节点类型
 └─ 实体 = 每个节点是持久的一等实体（turn 或嵌套 turn），
     结算、重载恢复、Inspector 全部走既有权威
```

机审（verdict）统一为**一种 contract**：VU 的 persona、judge_panel 的 rubric 都只是该 contract 的 prompt/解析配置，判停门禁、证据、挑战逻辑只写一遍。

---

## 4. 收编计划（分阶段，每阶段独立可交付）

### Phase 0 —— 退役与归一（无争议清理，小改）

1. 拆除 legacy autopilot 接线（`lib/tasks_pkg/autopilot.py` 及伴生模块约 3400 行）：它只服务已构造的 legacy carrier（`docs/TASK_EXECUTION_MODES.md:59-64`），arm/disarm 已中和其 marker，可安全移除。
2. 模式归一化收敛到单一 admission 边界：`normalize_interaction_mode_runtime_config` 从 ≥5 个重复调用点收为一处 + "已归一化"类型标记。
3. 模式单字段契约：消除 settings 布尔 / attempt config / 前端双布尔↔三态 radio 的多重表示（turn actor/kind 作为持久溯源保留）。

### Phase 1 —— 无争议底座进 Flow（小改，不改图语义）

4. 节点级 `model_override` 与 per-node 工具 allowlist 轴（A13：swarm 的 per-spec 能力，`lib/swarm/types.py:103,110-115` → Flow node params + `agent_runner` 消费）。
5. Flow 预算从代理计数扩展到 token/tool_calls 切片（A10：`lib/swarm/integration/_tools.py:62-121` → `lib/orchestration/budget.py`）。
6. ProgressBeacon liveness 作为节点运行时服务（A11：`lib/swarm/liveness.py`）。
7. presence announce/heartbeat 挂到节点运行时（A15）。

### Phase 2 —— 动态 fan-out 节点（形态 a，中改，Swarm 收编主体）

8. 新增 control kind `fanout`：注册三处（`_control_specs.py`、`engine.py:_walk` 分派、validation）；节点内由模型产出 specs（保留 `spawn_agents` 工具语义作为图的动态展开器）。
9. 节点运行时嵌入 `StreamingScheduler`：依赖就绪即启动、完成流式回传限定在节点内部（A1/A2 完整保留）；对节点外暴露 fork-join join 后结果进 dataflow typed I/O。
10. ArtifactStore 节点作用域化（A9）；read_only 编排模式对齐（A14，runner 已部分复用过滤器）。
11. 显式放弃项（形态 a 不做）：fire-and-forget inbox、await_agents any-of、跨 turn 会话注册表、auto-continue（A3/A4/A7/A8 的异步部分）。绝大多数用例只到 fork-join。

### Phase 3 —— 子代理升格与投影对齐（中改，拆墙 1/2/5）

12. 子代理成为持久嵌套实体：fan-out 节点的每个分支投影为父 turn 下的嵌套 turn（走 v3 turn 结算/attempt 栅栏/projection），swarm 私有表/磁盘日志/进程注册表退役。
13. 结果注入统一为 durable message 边：`<swarm-update>`、`goal_worker_review`、coordinator updates 全部走同一条通道；`_autocontinue.py` 的伪造 virtual_user turn 由"边投递到新 turn"取代。
14. 前端统一：swarm 嵌套面板改为普通嵌套 turn 渲染（或过渡期双写事件）；durable 锚从"spawn_agents 工具轮的 `_swarmSnapshot`"迁到节点级 durable 事件（`events.py:143-151` 对未知类型 fail-open，可平滑扩展）。
15. 崩溃恢复：fan-out 节点内分支用嵌套 turn 的 attempt 语义续跑，替代 swarm checkpoint/rehydrate 私有机制（A6/A12 换锚）。

### Phase 4 —— verdict 统一与 evolution 收编（拆墙 3/4）

16. verdict contract 单一化：VU 哨兵 + 进度收据门禁 与 judge_panel JSON + checkers 合并为一种 contract（persona/rubric 为配置）；`lib/agent_verdict/` 为唯一权威。
17. evolution runs 收编为 Flow 图：executing/reviewing/settling stage 机 → 图节点；周期 sweep/reconcile 由图运行时的终态回调取代；`record.*` KV（18 个 namespace）迁移到 run/turn 权威。
18. evolution workbench 与聊天 UI 合流：goal 会话渲染为带 marker 的普通会话（Chat GoalRun 已证明可行）；人机通道统一为 turn command（updates/answers API 退役）。
19. `evolution` 工具组显隐从 config 绑定特判改为节点工具权威策略的通用表达。

### Phase 5 —— 异步挂起/恢复（条件性大改，仅当产品确认需要 fire-and-forget）

20. 前置：Flow run 崩溃恢复从 `worker_lost`-only 升级为可恢复（独立有价值）。
21. 挂起/恢复节点（模板 = human gate）、inbox（A3）、会话注册表（A7）、auto-continue（A8）。

---

## 5. 关键决策点（需产品/架构确认）

| # | 决策 | 选项 | 影响 |
|---|---|---|---|
| D1 | 子代理实体模型 | (a) 嵌套 turn（推荐）/ (b) 保留独立 task 实体 | 决定 Phase 3 全部工作 |
| D2 | swarm 异步协议去留 | (a) 形态 a 放弃异步（推荐先落地）/ (b) 直接做形态 b | 决定是否需要 Phase 5 |
| D3 | verdict 统一方向 | 以 `agent_verdict` 为权威吸收 judge_panel / 反向 | Phase 4 的工作方向 |
| D4 | evolution workbench | 合流入聊天 UI / 保留为图的可视化皮肤 | 前端合并幅度 |
| D5 | 图定义来源 | swarm 的动态 specs 是否要求落入可 replay 的 durable 图定义（Studio 可视化白得） | Phase 2 事件/快照设计 |

## 6. 风险与不变量

- **守护测试先行**：`test_swarm_agent_loop_chassis.py`（禁私有执行路径）、`test_vu_prompt_single_source.py`（persona 单一来源）、`test_goal_worker_context.py`（工作集策略）是统一过程中必须持续通过的契约。
- **不弱化崩溃恢复**：Phase 3 用嵌套 turn attempt 语义替代 swarm 轮级 checkpoint 前，必须证明恢复粒度不回退（当前 swarm 是轮级，Flow 是 run 级判死——这是不对称最大的一点）。
- **事件契约平滑**：durable 事件注册表 fail-open，允许新旧词汇双写过过渡；但每个旧词汇必须有明确退役条件。
- **存储权威唯一**：任何阶段不得出现第三种"一次目标运行"的持久化范式（现有：orchestration_runs、record.* KV、conversation settings 三种，目标是收敛到一种）。

## 7. 证据索引（核心文件）

| 主题 | 文件 |
|---|---|
| 共享执行内核 | `lib/tasks_pkg/invocation.py`、`lib/tasks_pkg/child_invocation.py`、`lib/agent_loop.py`、`lib/tasks_pkg/orchestrator/_run.py` |
| Plan mode | `lib/tasks_pkg/plan_mode.py`、`lib/plan_contract.py`、`lib/turn_lifecycle.py:1564-1575` |
| Goal（chat） | `lib/goal_runs/`（worker_context.py、contract.py、continuation.py）、`lib/orchestration/_builtin_definitions.py`、`lib/orchestration/loop_runtime.py` |
| Goal（evolution） | `lib/evolution/runs.py`、`lib/evolution/coordinator.py`、`lib/evolution/coordination.py`、`lib/evolution/records.py` |
| Swarm | `lib/swarm/master.py`、`lib/swarm/scheduler.py`、`lib/swarm/agent.py`、`lib/swarm/registry.py`、`lib/swarm/persistence.py`、`lib/swarm/integration/_tools.py` |
| Flow 引擎 | `lib/orchestration/engine.py`、`agent_runner.py`、`parallel_runtime.py`、`human_gate_runtime.py`、`dataflow.py`、`budget.py` |
| 结果注入 | `lib/tasks_pkg/orchestrator/_swarm_inbox.py`、`lib/agent_inbox/`、`lib/swarm/integration/_autocontinue.py` |
| 前端 | `frontend/src/runtime/sections/ui/streaming_swarm_panel.js`、`frontend/src/features/evolution/`、`frontend/src/conversation/application/agent-mode.ts` |
