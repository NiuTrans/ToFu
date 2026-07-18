# 设计文档：主动上下文压缩（Manual `/compact`）

> 状态：**设计草案（等待评审）** · 作者：AI 协作 · 日期：2026-07-12
> 关联子系统：`lib/tasks_pkg/compaction/*`、`routes/conversations_compaction.py`、
> `static/js/context-bar.js`、`static/js/compaction-viewer.js`
> 关联文档：`docs/CLAUDE_CODE_ALIGNMENT.md`、`docs/EVENTS.md`

---

## 1. 目标与动机

用户希望有一个**主动压缩按钮**，效果类似 Claude Code 的 `/compact`：一键把当前会话的
历史上下文**大幅压缩**成一段工作状态摘要，让后续对话从一个很小的上下文重新出发。

诉求里有一句关键约束：**"要考虑复用现有压缩机制，可能涉及直接进行 LLM 压缩"** —— 也就是
说，不要另起炉灶写第二套摘要引擎，而是复用 L2 已经打磨很久的
`_generate_query_aware_summary`（分级评分、9 段结构化摘要、目标锚点保护、CJK 预算等）。

本设计要回答两个问题：

1. **后端**：如何在不重写摘要引擎的前提下，把"临时的 L2 压缩"变成"**持久化**的主动压缩"。
2. **前端**：这个按钮放哪、交互什么样、结果怎么呈现给用户。

---

## 2. 现状调研（决定复用边界的关键事实）

在动手前先把现有机制读清楚，避免误解。以下每条都来自代码，不是猜测。

### 2.1 现有四层压缩流水线（L0–L3）

见 `lib/tasks_pkg/compaction/__init__.py` 顶部 docstring：

| 层 | 触发时机 | LLM 成本 | 作用 |
|----|----------|----------|------|
| L0 | 工具结果入场（`tool_dispatch.py`） | 0 | 超大工具结果落盘+预览替换 |
| L1 | 每轮 LLM 调用前（`_layer1.micro_compact`） | 0 | 冷工具结果替换为占位符、剥离旧 thinking |
| **L2** | orchestrator 检测 token 超阈值时**强制注入**（`_layer2.force_compact_if_needed`） | **1 次 cheap 模型调用** | 分级评分 + 结构化摘要，替换 boundary 之前的旧消息 |
| L3 | API 报 400/413 后的应急恢复（`_reactive.reactive_compact`） | 视情况 | 归档→剥图→激进 micro→force→head-truncate |

### 2.2 ⚠️ 最关键的架构事实：**L2 是"每轮临时"的，不落库**

这是本设计所有决策的地基：

- 每一轮任务，orchestrator 都调用
  `conv_message_builder.build_api_messages_from_db(conv_id, cfg)`，**从数据库
  重新加载全部原始 `conversations.messages`**，再跑一遍 L1/L2 流水线
  （`orchestrator.py:1794 run_compaction_pipeline`）。
- L2 的 `execute_compact_tool` 只是**就地改写这一次请求用的 in-flight `messages` 列表**
  （`messages.clear(); messages.extend(new_messages)`），注入一对合成的
  `context_compact` tool_call/tool_result。
- **数据库里的原始会话从来没有被缩小**。L2 唯一的持久化动作是把压缩前的快照写进
  `transcript_archive` 表（供压缩查看器回看），以及一个 per-conv 冷却时间戳。

**推论**：现有 L2 是"内存态、每轮重算、防止爆窗"的临时机制。而用户要的主动 `/compact`
本质是一个**持久化操作**——它必须把 `conversations.messages` 里的旧历史**真正替换**成
摘要，这样后续每一轮 `build_api_messages_from_db` 加载到的就已经是小上下文了。

> 这就是主动压缩与 L2 的根本区别：**L2 改的是"请求"，主动压缩改的是"会话"。**

### 2.3 基础设施已经为 manual 预留了钩子

- `_archive_transcript(..., trigger='manual')` 已经接受 `'manual'` 触发类型
  （`_archive.py` docstring 明确列出 `force / reactive / manual`）。
- 压缩查看器 `compaction-viewer.js` 已经有 `manual` 的 SVG 图标 + i18n label
  （`_TRIGGER_ICON.manual`、`compactionViewer.trigger.manual`）。
- context-bar 的"液体气泡"chip 已经会读取 `_compactions[]` 标记、显示计数徽章、
  点击打开查看器。

也就是说，**前端"呈现压缩结果"的通道已经存在**，主动压缩只要产出同样形状的 archive +
marker，就能免费复用查看器和气泡。

### 2.4 可复用的后端零件（无需重写）

| 零件 | 位置 | 复用方式 |
|------|------|----------|
| 摘要引擎 | `_layer2._generate_query_aware_summary` | 直接调用，产出 9 段摘要 |
| 摘要系统提示 | `_layer2._SUMMARY_SYSTEM_PROMPT` | 不动 |
| 保留边界计算 | `_layer2._find_turn_boundary` | **仿写**为 `_raw_turn_boundary`（原始空间；见 §4.1）——不直接调用 |
| 目标锚点保护 | `_layer2._objective_anchor_index` | 直接调用（对原始形状安全），保护首条真实 user 消息 |
| 最近文件提取 | `_layer2._extract_recently_accessed_files` | 直接调用 |
| 快照归档 + SSE | `_archive._archive_transcript` | 传 `trigger='manual'` |
| token 估算 | `_tokens._estimate_total_tokens` / `_get_context_limit` | 直接调用；但**每-turn** 估算需 raw-aware（见 §4.1 第 3 点） |
| 前端查看器 | `compaction-viewer.js` | 免费复用（trigger=manual 已支持） |
| 前端气泡 chip | `context-bar.js` | 免费复用 |

---

## 3. 核心设计决策

### 决策 A：主动压缩是**持久化**操作，改写 `conversations.messages`

不同于 L2 的每轮临时改写，主动压缩必须把结果写回 DB。落库后：

- 后续每一轮 `build_api_messages_from_db` 天然加载到的就是小上下文；
- 无需 orchestrator 参与、无需任务在跑（用户在**空闲态**点按钮）；
- 用户在主聊天窗里能立刻看到历史被折叠成一条摘要。

### 决策 B：复用 L2 摘要引擎，但在**会话消息层**产出，而非请求层

新增一个薄薄的服务函数（暂名 `compact_conversation_now`），它做的事：

1. 从 DB 读原始 `conversations.messages`（**不是** api-form，是持久化的原始形状）。
2. 在**原始空间**用 `_raw_turn_boundary`（见 §4.1 硬约束）+ `_objective_anchor_index` 算出
   保留边界（保留最近 N 轮 + 目标锚点原文）。**下标只在原始列表上取、切。**
3. 把 boundary 之前的旧消息喂给 `_generate_query_aware_summary` 得到摘要文本。
4. **归档**压缩前的完整快照（`_archive_transcript(trigger='manual')`）→ 拿到 `archiveId`。
5. **重建** `conversations.messages`：
   ```
   [目标锚点原文(若在旧区)] + [一条摘要消息] + [最近保留的原始消息...]
   ```
6. 原子写回 DB（`write_json_atomic` 语义 / `save_conv` 路径）。
7. 返回 `{archiveId, tokensBefore, tokensAfter, msgsBefore, msgsAfter, summary}`。

### 决策 C：摘要以什么形状存进会话消息？—— **一条带 `_isCompactionSummary` 标记的 assistant 消息**

这是最需要斟酌的地方。候选方案：

| 方案 | 说明 | 取舍 |
|------|------|------|
| **C1（推荐）** 单条 assistant 消息 + `_isCompactionSummary:true` + `_compactions[]` marker | 摘要作为一条普通 assistant 文本消息落库；带元数据标记让前端渲染成"压缩边界卡片"而非普通气泡 | ✅ 与 `conv_message_builder` 的 `_build_assistant_messages` 天然兼容（无 toolRounds → 单条 plain assistant）；✅ 前端可用标记特殊渲染；✅ 后续压缩幂等 |
| C2 合成 tool_call/tool_result 对（照抄 L2 in-flight 形状） | 与 L2 内存态一致 | ❌ tool_call 落库需要配套 `toolRounds`/`toolCallId`，`_reconstruct_tool_call_messages` 校验严格，易碎；过度工程 |
| C3 存进 conversation `settings`（边界指针+摘要文本） | 不动 messages，加载时拼接 | ❌ 需要改 `_transform_messages` 的加载逻辑，侵入面大；与"用户看到历史被折叠"的直觉不符 |

**推荐 C1**。摘要消息形如：

```jsonc
{
  "role": "assistant",
  "content": "## 上下文已压缩（主动）\n\n<9段结构化摘要...>",
  "_isCompactionSummary": true,
  "_compactionArchiveId": 1234,       // 指向 transcript_archive，供"展开原文"
  "_compactions": [ { "archiveId": 1234, "trigger": "manual", ... } ],
  "timestamp": 1752300000000
}
```

`_build_assistant_messages` 对"无 toolRounds 的 assistant"直接返回单条 plain 消息
（已验证，见 `conv_message_builder.py:_build_assistant_messages` 短路分支），因此**发给
LLM 时摘要就是一段普通 assistant 文本**——正是我们想要的语义（"这是之前发生过的事的浓缩"）。

### 决策 D：目标锚点（首条真实 user 消息）必须**逐字保留**

复用 `_objective_anchor_index` 的既有语义：若首条真实 user 消息落在被摘要区，把它原文
拎出来放到摘要**之前**，避免多次压缩导致目标漂移（这是 L2 已经打磨过的不变量，直接沿用）。

### 决策 E：并发与竞态防护

- 主动压缩**只允许在会话空闲（无 activeTaskId）时触发**——若有任务在跑，返回 409 并提示
  "任务进行中，无法压缩"。理由：正在跑的任务持有一份 in-flight messages，压缩会话 DB 会与之
  竞态。
- 写回用 CAS/乐观锁（复用 conversation PUT 的 `409 stale-checkpoint` 语义），失败则整体回滚
  （已归档的快照无害，可保留）。
- 遵循 `CLAUDE.md §2`：全程 `logger` + `audit_log('manual_compaction', conv_id=..., archiveId=...)`。

---

## 4. 后端实现方案

### 4.1 新增服务函数（复用引擎，薄封装）

建议放在 `lib/tasks_pkg/compaction/_layer2.py` 旁边或新文件
`lib/tasks_pkg/compaction/_manual.py`（保持 L2 文件聚焦于自动路径）：

```python
def compact_conversation_now(conv_id: str, *, config: dict,
                             keep_recent_turns: int | None = None) -> dict:
    """用户主动压缩：复用 L2 摘要引擎，把旧历史持久化替换成摘要。

    与 execute_compact_tool 的区别：
      - 作用于 DB 的原始 conversations.messages（不是 in-flight api-form）
      - 结果写回 DB（持久化），而非仅改本次请求
      - trigger='manual'，无冷却门槛（用户显式意图）
    返回 {ok, archiveId, tokensBefore, tokensAfter, msgsBefore, msgsAfter, summary}。
    """
```

内部严格复用：`_generate_query_aware_summary` / `_extract_recently_accessed_files` /
`_archive_transcript`。**不新写摘要逻辑。**

#### ⚠️ 硬约束（阻塞项）：boundary 的计算与应用必须在**同一个索引空间**完成

这是本设计能否成立的关键正确性约束——评审时点破的隐藏 bug 就在这里。

**问题**：`_find_turn_boundary` / `_objective_anchor_index` 返回的是"在某个消息列表里的
**下标**"。而 `_transform_messages`（api-form 投影）会对原始列表做**改变长度/顺序的变换**：

- 把**一条**带 N 个 `toolRounds` 的原始 assistant 消息**展开成多条** api 消息
  （`_build_assistant_messages` → assistant(tool_calls) + 多条 tool + 尾部 assistant）；
- 折叠历史 endpoint 会话（`_collapse_historical_endpoint_sessions`）；
- 去重同时间戳 user 行（`_dedup_duplicate_user_messages`）；
- 合并连续同角色（`_merge_consecutive_same_role`）。

因此**在 api-form 上算出的 boundary 下标无法 1:1 映射回原始 `conversations.messages` 的
下标**。若在 transformed 空间算 boundary、却去切原始列表，会切错位置——保留区可能吃掉或
漏掉半个工具轮次，产出**孤儿 `tool` 消息或被劈开的 tool_call/result 对**，直接 HTTP 400。

**规则（必须遵守）**：**全程在原始存储形状上定义"轮(turn)"、boundary 与锚点。**

1. `turn` 定义：以原始列表里的 `user` 消息为界（一条 user + 其后所有非 user 原始消息）。
   boundary 永远落在**原始列表**的 user 下标上，切片也只切原始列表。
2. 锚点：直接在**原始列表**上跑 `_objective_anchor_index`（它只看 `role`/`content`，对原始
   形状同样适用，不受 `toolRounds` 影响——安全）。
3. **token 估算不能直接对原始行用 `_estimate_msg_tokens`**：该函数（`_tokens.py:29`）读
   `content`/`tool_calls`/content-blocks，是 **api-form 感知**的，对原始 assistant 行的
   `toolRounds` **视而不见**——一条带 8 个工具轮的原始行会被严重低估，导致保留预算算错、
   多留一堆本该压掉的历史。因此需要一个 **raw-aware 的每-turn token 估算**：把该 turn 的原始
   消息**临时**投影成 api-form（对这一段跑 `_transform_messages`）再 `_estimate_total_tokens`，
   或直接累加原始 assistant 行的 `content` + 每个 `toolRounds` 项的
   `assistantContent`/`toolArgs`/`toolContent` 文本长度。
4. **喂摘要引擎**：对被摘区（原始下标 `[system_end : boundary]`，剔除锚点）临时投影成
   api-form 文本喂 `_generate_query_aware_summary`。
5. **重建写回**：用**原始形状**拼接 `[锚点原始行?] + [C1 摘要 assistant 行] + [原始列表
   boundary 之后的行原样]`，写回 DB。

口诀：**读投影、写原始、算在原始空间**。`_transform_messages` 只在第 3、4 点这两个**只读**
用途中被短暂调用（计 turn token、生成摘要输入文本），其产物的下标**永不用于切原始列表**。

> 因此本设计**不直接复用** `_find_turn_boundary`（它内部用 api-form 感知的
> `_estimate_msg_tokens`），而是新增一个在原始空间工作的等价 `_raw_turn_boundary`：turn
> 划分逻辑照搬，但把 token 估算换成上面第 3 点的 raw-aware 版本。这是唯一需要"仿写"而非
> "直接调用"的零件，且必须由测试 1b 逐字校验其边界落点。

### 4.2 新增 REST 端点

复用 `conversations_compaction.py`（已挂在 `conversations_bp` 上）：

```
POST /api/v1/conversations/<conv_id>/compact
  body: { keep_recent_turns?: int }
  200:  { ok, archiveId, tokensBefore, tokensAfter, msgsBefore, msgsAfter, summaryPreview }
  409:  { error: 'task_active' }        # 会话有任务在跑
  404:  { error: 'not_found' }
  422:  { error: 'nothing_to_compact' } # 历史太短，boundary 会保留全部
```

- 用 `@require_scope('conversations')` + `api_ok/api_error`。
- 触发前校验 `settings.activeTaskId` 为空。
- 成功后 `_invalidate_meta_cache()`。

### 4.3 `Api.compactions` 前端客户端扩展

`static/js/api.js` 的 `compactions` 对象加一个方法（唯一允许发起 `/api/...` 的文件）：

```js
const compactions = {
  list: (convId)            => get(`/api/v1/conversations/${enc(convId)}/compactions`),
  get:  (convId, archiveId) => get(`/api/v1/conversations/${enc(convId)}/compactions/${enc(archiveId)}`),
  compactNow: (convId, opts) => post(`/api/v1/conversations/${enc(convId)}/compact`, opts || {}),
};
```

---

## 5. 前端交互设计

### 5.1 入口放哪？—— **复用已有的"液体气泡"context-bar chip**

现状：`context-bar.js` 的气泡 chip 已经挂在 `.chat-wrapper` 左侧，显示上下文占用百分比 +
压缩计数徽章，点击打开查看器。它是**上下文健康**的语义中心，主动压缩按钮天然属于这里。

**方案（推荐）**：在气泡 chip 上增加一个**"立即压缩"动作**，两种呈现二选一：

- **5.1-A（推荐）** chip 悬停/点击时弹出一个极简 popover，含两个动作：
  「查看压缩历史」（现有）+「立即压缩上下文」（新增）。避免与现有"点击=打开查看器"冲突。
- 5.1-B 在 chip 旁边加一个独立的小图标按钮（剪刀/压缩 SVG），仅在 `zone==='warn'|'hot'|'crit'`
  时高亮提示"建议压缩"。

无论哪种，**图标必须是 SVG**（`CLAUDE.md §3.4` 禁止 emoji/unicode 字形作为控件）。

### 5.2 交互流程

```
用户点「立即压缩」
  → 若会话有任务在跑：toast「任务进行中，无法压缩」，禁用按钮
  → 否则弹确认（轻量）：显示预估「当前 ~X 轮 / ~Y tokens → 保留最近 N 轮」
  → 确认后：chip 进入 loading 态（气泡上覆盖一个 1s ticker/spinner，SVG）
  → POST /compact
  → 成功：
      · toast「已压缩：Y → Y' tokens（-Z%）」
      · 主聊天窗把被折叠的旧消息替换为一张「压缩边界卡片」
      · 压缩计数徽章 +1（复用 flashGaugeForArchive）
  → 失败：toast 错误（中性文案，不泄漏异常栈；§Error/UI 偏好）
```

> **气泡液面何时下降？（修正评审 problem 2）** context-bar 的 `_lastUsageTokens`
> （`context-bar.js:_lastUsageTokens`）是从**最近一条 assistant 消息的 `usage`/`apiRounds`**
> 读的。而我们新插入的摘要消息**没有 usage 字段**，因此**在下一轮真实对话产生新 usage 之前，
> 气泡不会自动下降**——若声称"压缩后气泡立刻降"是不准确的。二选一（本设计取 B）：
> - **A**：文案改为"下次对话后上下文占用会明显下降"，气泡保持不动直到下一轮真实 usage 到达。
> - **B（推荐）**：`/compact` 端点返回 `tokensAfter` 估算值，前端据此**临时**把气泡刷到该值
>   （给摘要 assistant 消息挂一个 `_estimatedPromptTokens` 之类的轻量字段，让 `_lastUsageTokens`
>   在无真实 usage 时回退读它）。下一轮真实 usage 到达后自然接管。选 B 能给出"立即下降"的直觉
>   反馈，代价是 `_lastUsageTokens` 多一个回退分支——需在前端测试里覆盖"无真实 usage 时读估算值"。

### 5.3 主聊天窗如何呈现压缩结果 —— **"压缩边界卡片"**

复用 `chat_render.js` 已有的 `_compactions` 渲染路径（`chat_render.js:1244` 已经会为带
`_compactions[]` 的 assistant 消息渲染可点击的压缩 chip）。对 `_isCompactionSummary` 的
消息额外渲染成一张**折叠卡片**：

```
┌─ ✂ 上下文已压缩（主动） · 12 条消息 → 1 条 · 48k → 9k tokens (-81%) ──┐
│  [默认折叠] 点击展开查看摘要正文                                        │
│  [查看压缩前完整快照 →]  ← 打开 compaction-viewer 到该 archiveId       │
└──────────────────────────────────────────────────────────────────────┘
```

- 默认**折叠**（遵循用户偏好"默认折叠命令代码/长内容"）。
- "查看压缩前完整快照"直接调 `window.openCompactionViewer(convId, archiveId)`——**零新增
  查看器代码**。
- 卡片是一个**后端下发的稳定事实**（`_isCompactionSummary` + `_compactionArchiveId`），前端
  纯渲染，不做任何生命周期推断（遵循项目 charter 的前/后端契约不变量：放置/边界由后端算，
  前端是 reducer）。

### 5.4 刷新/重载后的持久性

- 摘要消息已落库 → 重载后主聊天窗直接显示压缩卡片（因为它就是一条持久化 assistant 消息）。
- `_compactions[]` marker 通过现有的 `attachCompactionMarkersToConversation` 在会话加载时
  从 `/compactions` 列表重新挂上 → 气泡计数、查看器历史 tab 都自动正确。

---

## 6. 与自动 L2 的关系（避免语义打架）

| 维度 | 自动 L2（现有） | 主动 `/compact`（本设计） |
|------|------------------|----------------------------|
| 触发 | orchestrator 检测超阈值 | 用户点按钮 |
| 时机 | 任务运行中，每轮重算 | 会话空闲态 |
| 作用对象 | in-flight 请求 messages | 持久化 conversations.messages |
| 是否落库 | 否（只归档快照） | **是（改写会话）** |
| 冷却 | 有 per-conv 冷却 | 无（显式意图） |
| 摘要引擎 | `_generate_query_aware_summary` | **同一个** |
| 归档 trigger | `force` | `manual` |

两者**共用摘要引擎和归档/查看器基础设施**，只在"改请求 vs 改会话"这一层分叉。主动压缩后，
后续轮次 token 已降，自动 L2 自然不会再触发——两者协同，不冲突。

---

## 7. 测试计划（RED-first）

1. `test_manual_compaction_persists_summary`：调 `compact_conversation_now` 后，DB 里
   `conversations.messages` 被替换为 `[锚点?] + [摘要消息] + [最近N轮]`，`msgsAfter < msgsBefore`。
1b. **`test_raw_turn_boundary_never_splits_tool_round`（守卫 §4.1 硬约束）**：构造一条含**多个
   `toolRounds`** 的原始 assistant 会话，断言 `_raw_turn_boundary` 的落点始终是**原始列表的
   user 下标**；压缩后经 `build_api_messages_from_db` 展开，保留区**不出现孤儿 `tool` 消息、
   也不劈开任何一个工具轮**（每个 tool_call 都有配对 tool_result）。同时断言 raw-aware token
   估算对该多工具轮行的计数**远大于** `_estimate_msg_tokens`（证明没退回 api-form 感知的低估）。
2. `test_manual_compaction_preserves_objective_anchor`：首条真实 user 消息逐字保留在摘要前。
3. `test_manual_compaction_archives_before_rewrite`：`transcript_archive` 有一条
   `trigger='manual'` 的完整压缩前快照，且写快照发生在改写 DB 之前。
4. `test_manual_compaction_idempotent`：对已压缩会话再压缩一次，锚点不重复、不破坏结构。
5. `test_manual_compaction_refuses_when_task_active`：`activeTaskId` 非空时端点返回 409。
6. `test_manual_compaction_nothing_to_compact`：历史过短（boundary 保留全部）返回 422，DB 不变。
7. `test_summary_message_survives_api_rebuild`：含 `_isCompactionSummary` 的会话经
   `build_api_messages_from_db` 后，摘要是一条 plain assistant 文本，结构合法（无孤儿 tool 消息）。
7b. **`test_summary_reserve_join_no_double_assistant`（补 problem 3 拼接不变量）**：重建序列是
   `[锚点 user] + [摘要 assistant] + [最近原始...]`。若"最近原始"区**首条恰好是 assistant**，
   则会出现 **assistant-assistant 相邻**，被 `_merge_consecutive_same_role` 合并——把摘要正文和
   保留区首条 assistant 粘成一条，损坏边界卡片的独立性。测试须构造"保留区首条为 assistant"的
   会话，断言经 `build_api_messages_from_db` 后摘要消息**独立成条、未被合并**。
   **实现对策**：重建时若 boundary 落点使保留区首条为 assistant，需保证摘要与其之间角色交替
   合法——最稳妥是让 `_raw_turn_boundary` 永远落在 user 下标上（turn 以 user 起头，保留区首条
   天然是 user），从而摘要 assistant 后紧跟 user，永不相邻同角色。此为 §4.1 "boundary 落在 user
   下标"约束的直接推论，本测试是它的守卫。
8. 前端：`_isCompactionSummary` 消息渲染成折叠卡片；点击"查看快照"调
   `openCompactionViewer(convId, archiveId)`；气泡计数 +1。

> 运行：`python -B -m pytest -p no:napari tests/test_manual_compaction*.py`

---

## 8. 分步落地顺序（strangler-fig，每步可独立验证）

1. **后端引擎薄封装** `compact_conversation_now`（复用 L2 引擎）+ 单测 1–4、7。**先绿再走。**
2. **REST 端点** `POST /compact` + 空闲态校验 + 单测 5、6。
3. **Api.js** `compactions.compactNow`（不违反前端 API 隔离 ratchet）。
4. **前端呈现**：`_isCompactionSummary` 折叠卡片渲染（chat_render.js）+ 前端测试 8。
5. **前端入口**：context-bar chip 的「立即压缩」popover + loading/toast。
6. `_BUNDLE_FILES` 检查（若新增顶层 JS 文件才需要；本设计尽量只改现有文件，避免 bundler churn）。

---

## 9. 待评审的开放问题

1. **保留窗口 N 的默认值**：`_MAX_PRESERVE_TURNS` 是 L2 的默认；主动压缩是否让用户可调
   （"保留最近 3/5/10 轮"）？建议默认沿用 L2 常量，UI 上先不暴露旋钮（避免过度设计）。
2. **摘要卡片是否可"撤销"**：压缩前快照已归档，理论上可提供"恢复到压缩前"。但恢复=再写回
   一大堆消息，且与后续新消息如何拼接有歧义。**建议 v1 不做撤销**，只提供"查看快照"。
3. **入口最终形态**：5.1-A（popover）还是 5.1-B（独立按钮）？倾向 A，避免与现有点击语义冲突。
4. **多轮主动压缩的摘要叠加**：第二次主动压缩时，第一条摘要消息会被当作"旧历史"再次摘要
   （被摘要的摘要）。这是可接受的（信息单调收敛），但需在测试 4 里确认不产生结构损坏。

---

## 10. 一句话总结

主动 `/compact` = **把 L2 那套已经打磨好的"查询感知分级摘要"引擎，从"每轮临时改请求"抬升为
"用户一键持久化改会话"**；前端完全复用现有的液体气泡 chip + 压缩查看器 + `_compactions`
渲染通道，只新增一个「立即压缩」入口和一张「压缩边界卡片」。核心新增代码是一个薄薄的
`compact_conversation_now`（读投影、写原始）+ 一个 REST 端点，**摘要逻辑零重写**。
