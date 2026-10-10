# Proof-carrying deliverables design proposal (draft)

**Status:** P0 IMPLEMENTED 2026-09-21 — `contracts/deliverable_proof_v1.schema.json`、
`lib/proof/`、`routes/api_v1/proof.py`（守卫：`tests/proof/test_deliverable_proof_contract.py`、
`tests/proof/test_proof_bundle.py`、`tests/api/test_api_v1_proof.py`）· **Author:** innovation review 2026-09-21 ·
**Context:** Tofu 的每条执行链路都在生产证据，但全部停留在运维面
（排障 / Request Inspector）。本提案把"可验证"从运维副产品升级为交付物的
产品形态：proof bundle 是一个可导出、可离线机验的证据包。它是纯装配层——
只读现有权威，不产生任何新事实。

## Problem

Agent 产出正在泛滥，"这份交付物是测过的还是模型编的"成为消费者（企业采购、
合规、协作转交、乃至用户自己两周后回看）的首要问题。Tofu 手里有全行业少见的
完整证据链，但它们互不组装、无法转交：

| 已有证据权威 | 位置 | 证明什么 |
|---|---|---|
| Turn Trace（per-attempt 永久权威） | `lib/tasks_pkg/turn_trace.py`、[TURN_TRACE_CONTRACT.md](contracts/TURN_TRACE_CONTRACT.md) | 每阶段耗时、状态、span 闭环，coverage full/partial 及原因 |
| Request Inspector 轮次证据 | `inspectorRef {taskId, roundNum, turn}`、`lib/tasks_pkg/round_evidence.py` | 每轮精确出站请求与模型可见结果（含 `degradedReason` 诚实标注） |
| 项目文件收据 / 命令快照 | `contracts/project_file_receipts_v1.schema.json`、[PROJECT_FILE_UNDO.md](PROJECT_FILE_UNDO.md) | 文件修改清单、run_command 作用域快照与 undo 证据 |
| Paper 构建收据 | `contracts/paper_build_v1.schema.json` | TeX 构建的退出码、产物、SyncTeX 证据 |
| Evolution 评审判词 | [EVOLUTION_DESIGN.md](EVOLUTION_DESIGN.md)（`assessed_by=judge_panel`、未知证据不得判成功） | 独立评审对每个 criterion 的 verdict |
| 失败归因与重试链 | `failureAttribution`、`retryChain`（见 [modules/task_engine.md](modules/task_engine.md)） | 失败的结构化归因，无孤儿错误 |
| 路由快照 | RouteSnapshot（见 [modules/llm_io.md](modules/llm_io.md)） | 实际使用的模型/deployment 身份，非显示标签 |

现状下第三方拿到一份 Tofu 交付物，没有任何方式区分"执行轨迹与收据齐全、
可复算"和"纯文本声明"。**证据存在但不可携带，等于不存在。**

## Non-goals

- **不产生新事实。** proof bundle 是装配层：只读上述权威并计算摘要。
  不引入第二事件权威、第二收据、第二评审。
- **不声称正确性证明。** bundle 证明的是"执行轨迹与收据的存在性、一致性、
  未被篡改"；交付物是否正确仍是评审/测试自己的判定（延续
  `assessed_by=judge_panel` 的诚实语义，绝不把 model assessment 标成
  machine proof）。
- **不做密码学签名 / 信任链。** 内容寻址哈希足够起步；签名列为开放问题。
- **不改变 retention/TTL。** 证据过期则显式 `coverage=partial` + reason
  （沿用 Turn Trace 的诚实空态先例：宁可标注不可用，不伪造零值）。
- **bundle 默认不含 prompt/正文。** 只有哈希与结构化收据；显式
  `include_content=true` 才内嵌并显式标注。

## Design

### 契约

`deliverable_proof_v1`（拟新增 machine-readable contract + guard test）：

```jsonc
{
  "version": 1,
  "bundleId": "…",                 // 内容寻址：canonical JSON 的 SHA-256
  "owner": 123,                    // 显式 owner，保持 enterprise seam
  "assembledAt": 1727000000000,
  "subject": {                     // 交付物身份 + 内容哈希
    "kind": "turn | paper_build | evolution_run | project_changeset",
    "identity": { "taskId": "…", "turnId": "…", "attemptId": "…" },
    "contentDigest": "sha256:…"
  },
  "evidence": [
    { "kind": "turn_trace",       "ref": "…", "digest": "sha256:…", "coverage": "full" },
    { "kind": "request_round",    "ref": "{taskId,roundNum,turn}", "digest": "sha256:…" },
    { "kind": "file_receipt",     "ref": "…", "digest": "sha256:…" },
    { "kind": "build_receipt",    "ref": "…", "digest": "sha256:…" },
    { "kind": "review_verdict",   "ref": "…", "digest": "sha256:…" }
  ],
  "verdicts": {                    // 可复算汇总：退出码、criterion 结果、修改清单哈希
    "commands": [{ "receipt": "…", "exitCode": 0 }],
    "review":   [{ "criterion": "…", "result": "pass", "assessedBy": "judge_panel" }]
  },
  "coverage": "full",              // 或 "partial" + coverageReason（Turn Trace 先例）
  "contentIncluded": false
}
```

### 装配器与验证器

- 新 domain 包（application 层，拟名 proof，尚无代码路径）：每种 evidence
  kind 一个只读 adapter。任一证据缺失/过期/不可读 → 该项降级，
  `coverage=partial` + 具体 reason；**绝不伪造，绝不补零**
  （Turn Trace `eventsAvailable:false` 先例）。
- 装配不引入新写路径：默认现装现取；可选留存走 owner-scoped 语义存储
  操作（CAS、有界、显式 TTL），不新写 SQL。
- 验证器两个形态，同一验证核：
  - `GET /api/v4/proof/{bundleId}/verify`（scope: tasks，owner 检查）；
  - 离线：bundle 自描述，验证核作为纯函数可独立分发（不依赖活服务器）——
    "可转交信任"的关键：收件人不需要信任发件人的服务器在线。
- 验证 = 重算每个 evidence 的 digest 与 bundleId，对照 verdicts 的一致性
  规则（如 review verdict 与 build receipt 的 subject 哈希必须匹配
  subject.contentDigest），输出机读 verdict：`valid | tampered | partial |
  unknown_evidence`。篡改任一字节 → `tampered`。

### 出口

- P0：headless/SDK——`tofu.proof.export(task_id)`，single-shot run 的
  result JSON 增加可选 `proof` 引用（eval harness 是天然首批消费者：
  轨迹 + 证据一起归档）。
- P1：浏览器——turn 交付卡片、paper build 页面、evolution result 的
  "Export proof" 动作；Request Inspector 增加 bundle 视图（消费同一
  装配器，不是第二实现）。

## Alternatives considered

- **把证据塞进 Turn projection。** 被拒绝：投影是渲染权威，不是证据载体；
  违反单一权威，且投影会被 compaction/TTL 回收，证据语义不同。
- **等多用户/企业版再做。** 被拒绝：信任问题对个人用户同样真实
  （两周后的自己就是"第三方"）；装配层不依赖身份模型演进，owner 显式即可。
- **人类可读报告（PDF）优先。** 被拒绝：机读为先，人类可读是投影；
  反过来的系统是审计噩梦。paper build receipts 的成功先例就是机读收据先行。
- **签名/公证先行。** 被拒绝：没有签名需求的实锤前，内容寻址 + 离线验证
  已覆盖"防篡改、可转交"；签名是 additive，不需要现在决定。

## Phasing

- **P0 — turn deliverable 一类（2026-09-21 已落地）。** 契约 + guard test、
  proof 装配器（turn_trace / request_round / file_receipt 三个 adapter）、
  `GET /api/v1/tasks/{id}/proof` 导出与 `POST /api/v1/proof/verify` 验证。
  测试：篡改检测（改一字节 → tampered；重封者被 resolver 证据重算捕获）、
  partial 语义与 reason 词表、owner 隔离、契约 drift guard、HTTP 边界。
  **留作紧邻后续：** SDK `tofu.proof.export(task_id)`（server 契约已稳定，
  薄客户端）。
- **P1 — paper build + evolution run 入 bundle；浏览器出口。**
- **P2 — 离线验证器独立分发（单文件）；签名/时间戳（开放问题收敛后）。**

性能约束：装配是请求时只读折叠，有界扫描（沿用 Turn Trace 的 200,000 行 /
1,000 终态事件上限先例），不新增常驻 pool；bundle 留存有界且 TTL 显式。

## Open questions for review

1. bundle 的服务端留存策略：现装现取 vs 留存副本（倾向：默认现装，
   导出即留存一份有界副本，TTL 与 task_results 对齐）。
2. 跨机器验证时 evidence `ref` 的 server-local 身份如何脱敏
   （倾向：ref 仅在原服务器可解析；离线验证只依赖 digest 与内嵌 verdicts，
   并在契约中明示这一语义层级）。
3. swarm / 多 attempt 交付物的 subject 身份：是否允许 bundle 嵌套
   （子 bundle 引用），还是先只支持单 attempt 主体。
