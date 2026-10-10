# Durable agent loop design proposal (draft)

**Status:** DRAFT — 未实施，等待评审 · **Author:** innovation review 2026-09-21 ·
**Context:** 当前进程重启即杀死一切 in-flight 任务：evolution 只能诚实标注
`needs_attention`（[EVOLUTION_DESIGN.md](../EVOLUTION_DESIGN.md)），长时目标、调度任务、
goal coordinator 全部继承这个天花板。本提案把"恢复"从事后人工 reconcile 升级为
声明式执行语义，且只组装已经存在并已钉死不变量的部件，不新增第二状态机、
第二事件词表或第二执行循环。

## Problem

Tofu 的产品叙事正在从"一次问答"走向"一个目标跑几天"（evolution run、
scheduler、goal coordinator、长时 deliverable production），但执行模型仍是
短命 run：任务字典是 process-local 载体，`TaskRuntime` 有 TTL，重启后
in-flight attempt 只能被 fence 成 `interrupted`。这不是缺陷修复问题，而是
能力天花板：**没有可恢复执行，"长期目标"永远是营销话术。**

关键观察：恢复所需的原材料已经全部存在且有守卫测试——

| 已有资产 | 位置 | 对恢复的意义 |
|---|---|---|
| 持久执行事件日志 | `lib/tasks_pkg/event_log.py` | 每轮模型请求/工具结算已 durable |
| 纯函数日志不变量检查 | `lib/tasks_pkg/event_invariants.py`（`tests/events/test_event_log_invariants.py`） | 能判定"最后一个完整结算的 round 边界"：seq 单调、round 配对、tool start→complete 结算、**model-visible ⟺ logged**（每轮必有 `messages_snapshot(kind='request')`，精确出站请求可重建） |
| checkpoint 续跑机制 | `checkpoint_resume` + canonical wire projector + causal-prefix validator（见 [modules/task_engine.md](../modules/task_engine.md)） | 已经能把中断的 assistant/tool rounds 重建为下一轮请求的合法前缀，且是字节稳定的 cache 前缀 |
| 执行会话生命周期 | `lib/agent_core/execution_session.py` | created→…→settling→terminal 单调路径 + 资源精确释放，续跑 admission 复用同一路径 |
| attempt fence / CAS | 存储权威内 stream-append attempt fence、checkpoint.guard CAS | 旧 attempt 即使线程未死透也只能协作中止；恢复写入有幂等边界 |
| 孤儿重投递先例 | `redispatch_orphaned_queue_on_startup`、swarm rehydrate（`tests/swarm/test_swarm_rehydrate_cancellation.py`） | 启动恢复扫描的既有模式，不是新发明 |

缺的是把这些零件闭合成一个声明式语义：**任务出生时声明可恢复类别，
重启后由系统（而非人）从最后一个 round 边界续跑。**

## Non-goals

- **绝不重放工具副作用。** 恢复点只取"完整结算"的 round 边界；已结算工具
  永不重跑。崩溃瞬间 in-flight 的工具按现有 fence 语义结算为
  aborted/unknown 并让模型在续跑请求中看到该事实。效果至多是 at-most-once。
- **不做全量确定性重放。** LLM 输出非确定，重放事件日志恢复内存状态既不可能
  也无必要——续跑 = 新 attempt 从重建前缀发出下一次模型请求。
- **不改变权威。** `TaskRuntime.status` 仍是任务状态权威；Turn 结算是渲染权威；
  事件日志是执行事实权威。本提案不引入这些之外的任何新权威。
- **聊天 turn 默认不自动恢复。** 用户在场时重新开始更诚实；只有声明式任务类别
  （evolution run、goal run、scheduler 任务、goal coordinator）opt-in。
- **不解决跨副本执行。** distributed mode 的恢复依赖 ROADMAP 第 3 条
  （externalized runtime state），列为 P2。

## Design

### 恢复单元：round 边界

恢复点 = 事件日志中最后一个满足 `event_invariants` 全部闭环条件的 round：
round_start/round_end 配对、该 round 的 tool_start→tool_complete 全部结算、
model_request 跨度闭合、`messages_snapshot(kind='request')` 已落盘。
判定复用现有纯函数，不写第二套折叠逻辑。纯文本 coalescing 窗口
（provider ingress 内的未落盘增量）按现有语义可丢失——崩溃损失上限与今天相同。

### 声明与检查点

- 任务出生时在 task config 携带 `resumption_class`: `none`（默认）|
  `round_boundary`。evolution 执行/评审/coordinator 任务、goal run、scheduler
  任务在创建处声明 `round_boundary`。
- 每个 round 结算后写一份 **bounded resumption checkpoint**，复用
  `tofu.task-results.checkpoint.guard/v1` CAS 通道（不是新存储路径）。内容有界：
  round 计数、累计 usage/token/tool-call（预算连续性）、循环配置快照、
  世界状态指纹、最后一轮 request 投影引用（不是全量 transcript——transcript
  由 canonical wire projector 在续跑时重建）。
- checkpoint 写入失败不杀任务（fail-soft，降级为本 round 不可恢复点），
  但 terminal 结算前的最后一个 checkpoint 享有与 running checkpoint 相同的
  维护车道语义。

### 启动恢复扫描

1. 启动时对 `resumption_class='round_boundary'` 的非终态任务做有界扫描
   （owner 显式、上限如 200 条，与现有学习/索引扫描同量级）。
2. 孤儿 attempt 先按现有 fence 语义结算为 `interrupted`——诚实，不伪装存活。
3. 以确定性幂等键（task birth idempotency key + resume ordinal）创建
   continuation attempt，走 `checkpoint_resume` 已有的重建路径，并在注入一条
   `system_note` 说明恢复点与中断事实（模型可见的诚实，不是隐藏的重试）。
4. 续跑 attempt 的 admission 走同一 execution_session 生命周期与同一有界
   Agent FIFO；恢复扫描自身幂等——重复扫描看到已 `interrupted` +
   已存在的 continuation 幂等键，直接跳过。

### 预算、上限与诚实边界

- **预算连续：** usage/token/tool-call 从 checkpoint 累计恢复，续跑不重置；
  evolution 的 cumulative budget 语义跨恢复延续。
- **恢复上限：** 每任务最多 3 次自动恢复；耗尽后落 `needs_attention`
  （对 evolution 走现有 reconcile 语义）。诚实边界保留——可恢复不是无限韧性。
- **不可恢复点：** in-flight provider 调用、compaction 原子窗口中点——
  分别回退到上一 round 边界 / compaction 前 checkpoint，绝不发明中间状态。
- **模型路由：** 续跑允许重新解析路由（原 slot 可能已不健康），
  RouteSnapshot 记录实际身份变化；`routeDecision` 漂移在 Request Inspector
  可见，不静默。

### 与 evolution 的关系

`reconcile` 的语义从"标 `needs_attention`"变为"触发一次恢复扫描；
恢复失败或超上限才 `needs_attention`"。`sweep_stale_runs` 不变。
EVOLUTION_DESIGN 中"a process restart does not resume an in-flight task"
的限制条款在本提案落地后按阶段删除。

## Alternatives considered

- **全确定性重放（event sourcing 字面意义）。** 被拒绝：LLM 非确定、
  工具副作用不可重放，实际能到达的状态与 round 边界续跑等价，但实现成本
  高一个数量级且引入"重放态"这个第二执行概念。
- **进程内 watchdog 重启线程。** 被拒绝：不解决进程死亡，只解决线程楔死
  （现有 reaper quarantine 已覆盖）。
- **维持现状，把恢复推给调用方。** 被拒绝：`needs_attention` 是诚实的死路；
  旗舰叙事（长时目标、个人舰队）在其上永远无法成立。
- **为恢复新建独立 durable queue / 第二事件日志。** 被拒绝：违反
  "一个概念一个权威"——恢复事实就是现有事件日志 + checkpoint 通道的投影。

## Phasing

- **P0 — personal mode + evolution run 单类别。** resumption checkpoint、
  启动恢复扫描、3 次上限、reconcile 改语义。测试：崩溃注入（round 边界 /
  mid-tool / mid-provider 三点位 kill）、恢复扫描幂等重扫、预算连续性、
  旧 attempt fence 语义、超上限落 `needs_attention`。
- **P1 — goal run / scheduler / coordinator 纳入；swarm 续跑与既有
  rehydrate 语义对齐；Request Inspector 展示 resume lineage（恢复链）。**
- **P2 — distributed mode 跨副本恢复。** 以 ROADMAP 第 3 条的
  externalized runtime state 为前置。

每阶段遵循测试阶梯：最小崩溃注入测试先行，再相邻契约
（event invariants、checkpoint CAS、execution session），最后 broad gate。
性能约束：恢复扫描与 checkpoint 写入全部走有界维护车道，不新增常驻
pool/timer（AGENTS.md 约束 3）。

## Open questions for review

1. 用户离开页面的后台聊天 turn 是否值得 opt-in（有真实用户价值，但
   恢复时的人机交互语义——审批中状态、Ask Human 等待——需要单独定义）。
2. resumption checkpoint 的保留策略与 `task_results` TTL 的关系：
   任务终态后 checkpoint 是否随 TTL 回收（倾向：是，与 terminal 诊断同生命周期）。
3. 恢复链是否进入 Turn Trace 的 span 词表（倾向：作为 `coverage` 的
   一个新 reason，而不是新 span 类型）。
