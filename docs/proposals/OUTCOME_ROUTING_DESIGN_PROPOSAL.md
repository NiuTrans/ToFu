# Outcome-feedback routing design proposal (draft)

**Status:** DRAFT — 未实施，等待评审 · **Author:** innovation review 2026-09-21 ·
**Context:** 路由器按"声明的能力 + 探测健康"选模型；evolution 评审产出
per-criterion verdict；两者从未闭环。本提案把"哪个模型在哪类任务上真的
交付了"变成路由的派生输入，同时把排序变更本身关进 experiments 框架
已有的统计闸门（promotion 默认拒绝）。

## Problem

model routing v2 的候选编译是声明驱动：能力、上下文、协议、预算、健康
（[modules/llm_io.md](../modules/llm_io.md)）。但同标称能力的模型在不同任务
类型（代码 / 研究 / 翻译 / 长文写作）上的实测方差极大——这正是
[MODEL_REGISTRATION.md](../MODEL_REGISTRATION.md) 用精确 catalog 规则守护
"声明"的原因，却也正是声明的天花板：**声明告诉你模型能做什么，
不告诉你它在这台机器、这个 owner 的任务分布上做得怎样。**

闭环所需的三样东西系统里都已存在，唯独没有连接：

| 已有资产 | 位置 | 闭环角色 |
|---|---|---|
| RouteSnapshot（requested/actual v2 身份、routeDecision） | [modules/llm_io.md](../modules/llm_io.md) 请求流第 7 步 | 每次 terminal settlement 已知"实际用了哪个模型" |
| terminal verdict（TurnVerdict fail-closed、typed task error、evolution review per-criterion 结果） | [modules/task_engine.md](../modules/task_engine.md)、[EVOLUTION_DESIGN.md](../EVOLUTION_DESIGN.md) | 每次结算已知"这次交付成没成" |
| cost priors / 实测消耗 | `contracts/evolution_cost_priors_v1.schema.json` | 成本维度已是实测，不是价目表外推 |
| experiments 框架（immutable spec、assignment、metric extractor、analyzer、promotion 默认拒绝、非劣效 + 成本 + P90 闸门） | [modules/experiments.md](../modules/experiments.md) | 排序变更的统计闸门——不用发明任何新机制 |

也就是说：**数据在、统计闸门在、缺的是一个 owner-scoped 的 outcome
投影和把它接进候选排序的那一段。** 这是本仓库能力图谱里少见的
"低成本、高杠杆"缺口。

## Non-goals

- **不动硬过滤。** v2 的 access/authorization/capability/protocol/budget/health
  硬过滤永远是 gate；outcome 只在通过 gate 的候选之间影响排序。
  probe 证据"diagnostic, never an access gate"的先例原样延续到 outcome 证据。
- **不做在线 bandit / 实时探索。** 个人部署样本量不足；固定队列 + 非劣效
  检验才是诚实统计（experiments 决策契约的既有姿态）。
- **不引入第二套路由配置权威。** outcome 档案是 v2 aggregate 的派生只读
  输入，不是平行配置；排序参数变更走 experiments promotion 闸 +
  人工确认，与 evolution mutation 的"行为平面"姿态一致。
- **不让 evolution 学习回路直接改路由。** 行为平面闸门（lineage、gate、
  holdout、人工确认、rollback）原样适用。
- **观测率不是因果。** outcome 档案的措辞沿用 evolution 的诚实规则：
  observational rates, not causal lifts。

## Design

### Outcome 投影（新写路径，一处）

terminal settlement 处追加一个 owner-scoped 有界投影（新语义存储操作 +
CAS，走 sidecar，routes 无 SQL）：

```jsonc
{
  "bucket": { "model": "<actual v2 identity>", "signature": "<task-signature>" },
  "observations": [ /* 最近 64 条，与 evolution strategy 观测同量级 */ ],
  "passRate": 0.0, "costP50": 0, "latencyP90Ms": 0
}
```

每条 observation = { terminalVerdict, usage, latencyMs, routeDecision,
failureKind? }。全部字段在现有结算路径已存在；投影是折叠，不是新采集。
task-signature 从既有事实派生（会话配置类别 / evolution domain /
工具使用指纹），不为签名发明新的分类学。

### 档案生成与消费

- maintenance-lane 周期任务把投影折叠成 per-(model, signature) 档案
  （fail-soft，单条坏记录不中止整轮，friction miner 先例）。
- 候选编译阶段：通过硬过滤的候选，若存在足够样本（阈值如 ≥8 条）的
  档案，按 passRate → costP50 → latencyP90 字典序重排；**cold-start 或
  样本不足 = 现有声明顺序，完全向后兼容。**
- 每个被 outcome 影响的选择在其 RouteSnapshot 记录 `orderingEvidence`
  {bucket, sampleSize, passRate}——Request Inspector 可见，可审计，
  与 `routeDecision` 同级。

### 统计闸门

排序的启用与参数（开关、样本阈值、维度权重）本身是一个 experiment：
固定队列、server-owned start/seal、完整实价覆盖、质量非劣效间隔、
成本间隔上界 < 0、P90 护栏、untruncated source——直接复用
context-cost analyzer 家族（[modules/experiments.md](../modules/experiments.md)
决策契约）。`promotionEligible` 之前，排序变更不生效；之后也需人工确认
才落 v2 aggregate。默认配置 = 关闭。

### 与 evolution 的关系

evolution 的 per-role 模型选择（executor/reviewer/coordinator）在 P2 消费
同一档案；cost priors 仍是 evolution 内部学习的权威，outcome 投影不是
第二份成本账本——它只折叠路由视角需要的三元组，且回链到 episode 身份。

## Alternatives considered

- **在线 bandit / 实时自适应路由。** 被拒绝：个人部署样本量与诚实统计
  冲突；experiments 固定队列先例更稳，且已有全部机制。
- **用公开 benchmark 分数代替本机实测。** 被拒绝：benchmark 是"声明"的
  另一种形式——一次性、非本机、非本 owner 任务分布；本提案的整个立论
  就是分布内实测。
- **让 evolution 学习回路直接改路由配置。** 被拒绝：违反行为平面闸门；
  outcome 档案必须过同一 promotion 闸，不接受旁路。
- **把 outcome 写进 v2 aggregate 本体。** 被拒绝：aggregate 是 owner
  配置权威；档案是派生只读输入，分开才能在配置迁移/重置时保持语义清晰。

## Phasing

- **P0 — 只读投影与档案。** outcome 投影、档案生成、Settings 只读面板
  （"实测档案"：pass 率/成本/延迟 per (model, signature)）。路由行为零变化。
  测试：owner 隔离、有界折叠、cold-start、脱敏（无 prompt/内容）、
  存储契约守卫。
- **P1 — 排序消费（默认 off）+ 闸门接线。** `orderingEvidence` 审计、
  experiments 定义与 analyzer 复用、Settings 开关。测试：硬过滤不可越过、
  样本阈值、向后兼容（关闭 = 现有顺序逐字节一致）、promotion 拒绝路径。
- **P2 — evolution per-role 选择消费同一档案；task-signature 细化评审。**

性能约束：投影写入在 terminal 结算的既有事务边界内追加一个有界操作；
档案生成走维护车道；候选编译只多一次有界内存查找——热路径无新增 I/O。
所有规模参数（64 条/桶、200 桶扫描）遵循 launch-probed 有界先例。

## Open questions for review

1. task-signature 的最小充分定义：太粗信号稀释，太细永远 cold-start。
   倾向从三个已存在的类别轴派生（会话类别 / evolution domain / 工具指纹），
   P0 面板数据会回答哪个轴有区分度。
2. 多 owner 时代是否允许跨 owner 聚合：默认否（owner-scoped 保持
   enterprise seam 与隐私），但签名/档案 schema 不硬编码单 owner 假设。
3. 上下文策略（context_composer 注入必要性）是否作为 outcome 的一个
   维度纳入：立论同源，但先不做——P0 数据足够后再评估。
