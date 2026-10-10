# 工具调用成功率与渲染设计提案（draft）

**Status:** 已评审 — B2（PTC 降 batch 相关）按评审意见剔除，现有
`_toolScriptBatchFallback` 运行时机制保持现状、不属于本提案范围。
**实施状态（2026-09-22）：** Phase 0（A1–A3 度量）、Phase 1（C1–C4
前端）、B1（codex freeform apply_patch）已交付；B3 等待 Phase 0 报表
数据回填；B4 维持延后决策。 · **Context:** 对照项目内 Codex 的
programmatic tool calling（PTC）与 OpenCode 的工具调用形态，Tofu 的
工具调用首轮通过率与 PTC 程序成功率体感偏低、且无任何度量；前端在
fallback 渲染路径上直接倾倒裸 JSON。本提案先立度量，再按数据驱动后端
改进；前端 fallback 修复独立可交付。

## Problem

三个症状共享一条因果链：模型发出的工具调用形态（词表、参数结构、程序）
与 Tofu 规范表面之间的距离，由 repair/重试/降级在运行时弥补；每一次
弥补都是一轮浪费，且失败路径在 UI 上最丑。

1. **工具调用首轮通过率无度量。** repair 层只有散点 `audit_log`
   （`tool_gateway_repaired`、`toolscript_syntax_repaired`），没有按
   模型×工具×失败类别聚合的通过率视图，"成功率低"无法证实也无法证伪。
2. **PTC 程序成功率无度量。** local ToolScript 已有
   `_toolScriptConsecutiveAuthoringFailures` → 自动降 batch 档的运行时
   机制（`lib/tasks_pkg/handlers/tool_gateway.py`），hosted PTC 有
   `reject_programmatic_call` / `settle_programmatic_call`
   （`lib/tasks_pkg/orchestrator/_programmatic.py`），两侧都没有聚合报表。
3. **前端 fallback 路径裸 JSON。** rich port 返回空时
   `renderToolBlock` / `renderProgramBlock`
   （`frontend/src/conversation/ui/classic-conversation-renderers.ts:1432/1481`）
   把 `JSON.stringify(input, null, 2)` 倒进裸 `<pre>`，CSS 仅一条边框
   （`conversation-surface.css:71-74`）；通用结果查看器单 `<pre>` 最长
   120k 字符、无语法高亮（`tool-result-presentation.ts`）。

## 参考系：Codex 与 OpenCode 怎么做（调研结论）

### Codex PTC（`lib/tools/programmatic.py` 及下游）

一个语义能力、两个执行后端，fail-closed 解析
（`resolve_programmatic_backend`）：

| 后端 | 适用 | 机制 |
|---|---|---|
| `native_openai` | 仅官方 GPT-5.6 + Responses + openai profile | 程序跑在上游托管 V8，子调用带 `caller.type='program'` 流回 |
| `local` | 其余所有模型（含 Codex OAuth profile） | 模型写 ToolScript 给 `execute_tools.program`，服务端解释执行，仅压缩结果回上下文 |

值得保持的设计：`caller` 全链路透传（`responses_outbound/_sse.py`）+
准入边界（预算 16 calls / 8 并发 / 1 MiB / 4 续传）；上游 400/404/422
点名 `programmatic_tool_calling` 时自动回退 local（`_sse_core.py`），
一次原生失败不毁整轮；指导文本字节稳定以保护 prompt 缓存前缀
（`local_ptc_guidance` 刻意不插工具名）；小模型不降级，畸形程序得
带类型、可重试的 `ToolScriptError`。

### OpenCode

OpenCode 本体极简：纯 chat/completions + 小写扁平词表
（`bash/read/write/edit/task/question/skill/todowrite`）+ camelCase 参数。
**其高通过率的本质是工具词表落在模型训练分布内。** Tofu 规范名
（`read_files/grep_search/apply_diff`）不在分布里，靠
`lib/tool_input_repair/` 事后补救（别名表 + 结构转换 + 幻觉分类器给
"did you mean X"）；日志审计中 `read_file` 曾 73 次/天撞墙。Codex 的
freeform `apply_patch` 在 B1 交付前完全不映射——模型以 codex 形态打
补丁时直接失败。

## Non-goals

- **不重写 rich renderer。** 已知工具家族（cmd/diff/todo/browser/…）的
  富渲染是好的；本提案只修 fallback 与通用查看器。
- **不改规范工具名 SSOT。** 是否按训练分布方言化"模型可见的工具名"是
  一个独立决策（B4），需要 A 的数据支撑，不在本提案内拍板。
- **不做在线自动调参。** 度量报表是离线诊断证据，不构成运行时准入闸门
  （延续 probe 证据 "diagnostic, never an access gate" 的姿态）。
- **不新增持久存储。** 统计走既有 `audit_log` + 派生只读报表，属可重建
  诊断数据，不落 durable 用户状态。

## Proposal A — 度量先行（Phase 0）

- **A1 首轮通过率统一分类埋点。** 在统一网关的 normalize/settle 单点
  （`tool_gateway.py` + `executor/_finalize.py`）为每个工具轮次记录
  outcome：`ok` / `repaired(alias|structural|syntax)` /
  `rejected(hallucinated|schema)` / `error(typed code)`，维度
  model × tool_name（修复后名 + 原始名）。复用既有 `_repaired` 徽章
  数据与 `audit_log`，不新开存储。
- **A2 PTC 双侧程序成功率。** local 侧已有 `audit_log('ptc_local_program',
  status=...)`，补记 `ToolScriptError.code` 与 batch 降级事件；hosted 侧
  在 `reject_programmatic_call` / `settle_programmatic_call` 补对称
  `audit_log`。产出"程序一次写对率 = 无语法修复且无重试即 completed"。
- **A3 离线报表脚本。** 新增 `debug/tool_call_success_report.py`（类比
  `audit_codex_session.py` 的只读姿态）：扫 `logs/app.log` 的 audit 行，
  按模型×工具×失败类别聚合，输出首轮通过率与 top 失败原因。这就是
  "成功率低"的证实/证伪工具，也是 B 系列改动的验收尺。

## Proposal B — 后端成功率改进（数据驱动，Phase 2）

- **B1（已交付）支持 codex freeform `apply_patch`。** 原状：模型以
  codex 形态打补丁直接撞墙。交付：`lib/tool_input_repair/_codex_patch.py`
  把 patch 文本解析为 `edit_file` 的 `edits=[...]`（仅 `Update File`
  逐 hunk 转 `replace`；`Add File`/`Delete File`/`Move to`/无上下文锚点
  的纯插入失败闭合、给出指向 write_file/edit_file 的纠正提示），名称经
  `edit_file` 收敛集路由，ingest 与 gateway 两条路径对称拦截，UI 显示
  `codex_apply_patch` auto-fixed 徽章。
- **B2（评审剔除）PTC 降级可见性与阈值评审。** 评审结论：不涉及 PTC
  降 batch 的任何改动；既有 `_toolScriptBatchFallback` 机制维持现状。
- **B3 repair 长尾数据驱动扩充。** 用 A3 报表的 top 未映射 hallucinated
  名回填 `_TOOL_NAME_ALIASES`（保持"仅 1:1 无歧义"纪律）。
- **B4（决策点，延后）模型可见工具名方言化。** 若 A 数据显示规范名本身
  是首要失败源，评估在 schema 宣告层对开放模型直接给训练分布名（把
  repair 从运行时移到编译期）。需独立提案与 experiments 闸门。

## Proposal C — 前端渲染改进（独立可交付，Phase 1）

- **C1 消灭裸 JSON fallback。** `renderToolBlock` / `renderProgramBlock`
  的 fallback 改为设计系统卡片：图标 + 工具标签 + 状态 chip + 可折叠
  美化 JSON。全部走 `frontend/src/ui/` 工厂与设计 token，禁止手写
  hex/px（遵守 `docs/DESIGN_SYSTEM.md` 与 RENDER_CONTRACT 的
  "attention ledger, not a raw event dump"）。
- **C2 通用结果查看器升级。** `renderGenericResultHtml`：按内容语言
  检测 + 语法高亮 + 超长输出折叠/分页（保留 120k 字符硬上限与截断
  标注）。
- **C3 program 块富化。** 展示 ToolScript 源码（高亮）+ 子调用列表
  （名称 + 状态 glyph），与 `_programSynthetic` 轮次数据对齐；hosted
  与 local 同构（两者本就共享 `program_start`/`program_output` 投影）。
- **C4 兜底轮次最小完成卡片。** 无结果体的 settled 轮次不再落裸
  `.ptool-line`，给"已完成"最小卡片；恢复重建轮次的标题从"原始参数
  碎片"改为语义化标签（`tool_rounds.js:361-382`）。

## Alternatives considered

- **跳过度量直接改后端（驳回）。** 无基线则改进无法验收，且可能在
  错误的失败类别上花力气（别名长尾 vs 结构转换 vs PTC authoring）。
- **前端工具渲染全面重写（驳回）。** 违反最小改动；rich renderer 覆盖
  已知家族，病灶集中在 fallback 与通用查看器。
- **直接把规范工具名改成 OpenCode 词表（驳回）。** 规范名是 SSOT，
  repair 层已承担映射；但是否在宣告层方言化留作 B4 数据决策。
- **hosted PTC 扩大到非 GPT-5.6 模型（驳回）。** 上游能力边界，非
  Tofu 可控；local 后端已是等价语义通道。

## 验证路径

- 后端：`tests/` 中 tool_input_repair、tool_gateway PTC、programmatic
  准入/结算的 focused 测试；A3 报表对既有日志离线可跑、确定性输出。
- 前端：`npm run check:conversation-sync` + focused presentation 测试 +
  `npm run check:frontend`；fallback/长输出/程序块走浏览器 visual gate。
- 验收：A3 报表在改动前后各跑一份，首轮通过率与 PTC 一次写对率的
  delta 即为 B 系列验收证据；C 系列以 visual gate 截图为证据。

## 实施顺序与依赖

Phase 0（A1–A3，度量）与 Phase 1（C1–C4，前端）互相独立、可并行；
Phase 2（B1、B3）依赖 Phase 0 的报表定位失败类别；B4 依赖 Phase 2
完成后仍残存的命名类失败证据。
