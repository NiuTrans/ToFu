# Tofu Agentic 开发经验：上下文工程、工具调用与 Harness 编排

> 本文从三个维度系统梳理了 Tofu (豆腐) 项目在 agentic AI 应用开发中积累的工程实践经验。

---

## 目录

1. [上下文工程 (Context Engineering)](#1-上下文工程-context-engineering)
   - [1.1 Messages 组织与分层注入](#11-messages-组织与分层注入)
   - [1.2 压缩管线 (Compaction Pipeline)](#12-压缩管线-compaction-pipeline)
   - [1.3 Memory 机制](#13-memory-机制)
   - [1.4 Session Memory — 跨压缩的持久状态](#14-session-memory--跨压缩的持久状态)
   - [1.5 Prompt Cache 优化](#15-prompt-cache-优化)
   - [1.6 Delta Attachment — 增量上下文追踪](#16-delta-attachment--增量上下文追踪)
2. [工具调用 (Tool Calling)](#2-工具调用-tool-calling)
   - [2.1 工具定义与分发架构](#21-工具定义与分发架构)
   - [2.2 流式工具执行 (Streaming Tool Execution)](#22-流式工具执行-streaming-tool-execution)
   - [2.3 工具结果预算管理 (Tool Result Budgeting)](#23-工具结果预算管理-tool-result-budgeting)
   - [2.4 工具钩子系统 (Tool Hooks)](#24-工具钩子系统-tool-hooks)
   - [2.5 省 Token 工具：content_ref](#25-省-token-工具content_ref)
3. [Harness 编排 (Harness Programming)](#3-harness-编排-harness-programming)
   - [3.1 单 Agent 编排：Orchestrator 主循环](#31-单-agent-编排orchestrator-主循环)
   - [3.2 自主模式：Endpoint (Planner → Worker → Critic)](#32-自主模式endpoint-planner--worker--critic)
   - [3.3 多 Agent 编排：Swarm 系统](#33-多-agent-编排swarm-系统)
   - [3.4 可靠性工程：异常检测与自愈](#34-可靠性工程异常检测与自愈)
4. [经验教训与反模式](#4-经验教训与反模式)
5. [对标 Claude Code：差异与借鉴](#5-对标-claude-code差异与借鉴)

---

## 1. 上下文工程 (Context Engineering)

上下文工程的核心目标是：**在有限的 context window 内，以正确的顺序、正确的时机，注入最相关的信息**。这不仅仅是 "把 messages 拼起来发给 LLM" 那么简单——它涉及分层注入策略、渐进式压缩、缓存稳定性、增量追踪等一系列工程决策。

### 1.1 Messages 组织与分层注入

我们的 system message 采用**分层注入**策略（`system_context.py`），每一层的变化频率不同，从静态到动态排列，以最大化 prompt cache 命中率：

```
┌─────────────────────────────────────────────────────┐
│ Layer 1: Project Context (CLAUDE.md + 工具文档)       │  ← 变化频率：低（文件编辑时变化）
│   _prepend_to_system_message()                       │
├─────────────────────────────────────────────────────┤
│ Layer 2: 静态指导（工具使用指南、输出效率指南）          │  ← 变化频率：零（永不变化）
│   as_separate_block=True（独立 cache breakpoint）    │
├─────────────────────────────────────────────────────┤
│ Layer 3: Memory 累积指令 + 计数提示             │  ← 指令零变化，计数在 CRUD 时变化
├─────────────────────────────────────────────────────┤
│ Layer 4: Swarm 并行执行指南（当 swarm 启用时）         │  ← 变化频率：零
├─────────────────────────────────────────────────────┤
│ Layer 4.5: 当前日期（仅日期，UTC，每天变化一次）        │  ← 变化频率：极低
├─────────────────────────────────────────────────────┤
│ Layer 5: Session Memory（自动提取的工作笔记）          │  ← 变化频率：中（每隔几轮更新）
└─────────────────────────────────────────────────────┘
```

**关键设计决策：**

1. **变化频率从低到高排列**：静态内容靠前（cache prefix 更稳定），动态内容靠后（变化不会破坏前面的缓存）。
2. **`as_separate_block=True`**：静态指导内容作为 system message 中的独立 content block 注入。Anthropic 的缓存系统以 content block 为粒度做 breakpoint，独立 block 意味着即使前面的 project context 变化了，静态指导仍可被缓存。
3. **Memory 不再注入为 XML listing，改用工具发现 + prefetch**：早期的 `<available_memories>` 列表注入 user message。后来改为两条路径：显式 `search_memories` 工具（模型主动查） + 隐式 prefetch（round 0 时 BM25→廉价模型筛选→注入 `<relevant_memories>`）。system message 只注入计数提示。
4. **日期格式的 A/B 测试**：我们做了 4 个 arm 的 A/B 测试：
   - Arm A: 完整日期时间注入 user message → 77.9% cache 命中率，$0.49
   - Arm C: 仅日期注入 system prompt → **85.7% cache 命中率，$0.36**（Winner）
   - Arm D: 完整日期时间注入 system prompt → 12.4%，$1.55（灾难性）

   结论：日期只精确到天（UTC），放在 system message 末尾，每天只变一次。

5. **`<system-reminder>` 标签包裹**：所有动态注入的上下文都用 `<system-reminder>` 标签包裹，这与 Claude Code 的约定一致。模型被训练为将这类标签内容视为权威系统指令，与用户内容区分。

**User Message 注入（Round 0 Only）：**

```python
# 只在 Round 0（用户新一轮消息）触发 memory prefetch
# 后续 tool round 不再重复
if round_num > 0:
    return  # ★ 保持 cache prefix 稳定

# Two-stage cascade: BM25 top-N → cheap-LLM top-M
query_text = _extract_last_user_text(messages)
relevant = prefetch_relevant_memories(
    project_path=pp,
    query_text=query_text,
    recent_turns=_last_k_turns(messages, k=3),
    task=task,  # for SSE event emission
)
inject_relevant_memories_block(messages, relevant)
```

### 1.2 压缩管线 (Compaction Pipeline)

当对话超过上下文窗口的 80% 时，我们需要压缩。但压缩不能简单地 "砍掉旧消息"——这会丢失关键上下文。我们设计了一个**四层渐进式压缩管线**：

```
Layer 0: Tool Result Budgeting (入口即压缩，零延迟)
    ↓
Layer 1: Micro-Compaction (每轮执行，零 LLM 成本)
    ↓
Layer 2: Force Compact (触发时执行，需要 LLM 调用)
    ↓
Session Memory (跨压缩存活的持久笔记)
```

#### Layer 0: Tool Result Budgeting

灵感来自 Claude Code 的 `toolResultStorage.ts`。当工具结果产生时，**立即**评估大小：

```python
TOOL_RESULT_MAX_CHARS = {
    'read_files':    0,          # 豁免——永不截断（模型会重新调用）
    'grep_search':   30_000,
    'fetch_url':     50_000,
    'run_command':   40_000,
    # ...
}
MAX_ROUND_TOOL_RESULTS_CHARS = 300_000  # 单轮总预算
```

**关键洞察**：`read_files` 被标记为**豁免工具**（`_BUDGET_EXEMPT_TOOLS`）。截断读取结果是反生产力的——模型会重新调用工具来获取完整内容，反而浪费更多 token。这些工具有自身的内部限制（100K/文件，200K/批次），后续由 Layer 1 的 micro-compact 在它们变"冷"后再压缩。

超出预算的结果不是被截断，而是**持久化到磁盘**：

```python
def _persist_to_disk(content, tool_name, tool_use_id, conv_id):
    # 写入 /tmp/chatui-tool-results/{conv_id}/
    # 返回 preview + 文件路径
    # 模型可以用 read_files 按需读取完整内容
    # 信息永远不会丢失
```

这比旧的 head+tail 截断方案好得多——信息不会不可逆地丢失。

对于 `web_search` 结果，我们生成结构化 preview（标题 + URL + 内容片段），让模型保持对所有搜索结果的感知，按需深入阅读。

#### Layer 1: Micro-Compaction

**每轮 LLM 调用前**执行，零 LLM 成本，四个 Phase：

```
Phase A: 剥离冷 assistant 消息的 reasoning_content（thinking 块可达 10K+ chars）
         保留最近 4 条 assistant 的 thinking
         
Phase B: 压缩冷 tool results（保留最近 30 条不动）
         短结果（<500 chars）跳过
         已压缩的跳过（幂等）
         
Phase C: 激进剥离冷 image 工具结果（base64 数据 URL 可达 1-10MB）
         仅保留最近 2 条图片结果
         
Phase D: 压缩冷 assistant 消息正文（仅在 force_compact 时启用）
         ⚠️ A/B 测试证明常规运行时此操作会破坏 prompt cache
```

**关键的 cache-aware 设计**：

```python
# 获取 cache prefix 中的消息数量
_cache_prefix_count = get_cache_prefix_count(conv_id)

for idx in cold_indices:
    # 跳过在 cache prefix 中的消息——修改它们会破坏缓存
    if idx < _cache_prefix_count:
        skipped_already += 1
        continue
```

Phase D 的 A/B 测试故事值得特别讲：我们发现即使 `get_cache_prefix_count()` 返回了值，Anthropic 实际上缓存了直到 BP4 (conversation tail) 的所有内容。修改该范围内的**任何消息**都会改变 prefix bytes → 完全 cache miss → 以 1.25-2.0x 的代价重新缓存。测试中 Phase D 导致总成本增加了 **57%**（4 轮中有 3 次完全重新缓存）。

#### Layer 2: Force Compact (Query-Aware LLM Summary)

当 token 估算超过可用上下文的 80% 时触发。这是唯一需要 LLM 调用的压缩层：

```python
_SUMMARY_TRIGGER_RATIO = 0.80
_SUMMARY_COOLDOWN = 30.0  # 防止快速重复触发
_KEEP_RECENT_PAIRS = 4    # 至少保留最近 4 轮对话
```

摘要 prompt 要求 LLM 对每个历史轮次做相关性评分：

| 评分 | 处理方式 |
|------|----------|
| 🟢 CRITICAL (3) | 原样保留：文件路径、代码片段、错误信息、决策 |
| 🟡 USEFUL (2) | 压缩到 1-3 句关键信息 |
| 🟠 TANGENTIAL (1) | 一行提及或完全丢弃 |
| ⚪ IRRELEVANT (0) | 完全丢弃 |

输出格式是 **9 个 section 的结构化摘要**（Primary Request → Key Technical Concepts → Files & Code → Errors & Debugging → Problem-Solving Progress → All User Messages → Decisions & Preferences → Current Working State → Pending/Next Steps）。

Section 6 (All User Messages) 是**强制的**——用户消息包含指令、偏好和上下文，永远不能丢失。

摘要以**合成的 tool_call + tool_result 对**注入 messages 中，替换旧消息。

### 1.3 Memory 机制

Memory 系统分为两层：**跨会话持久 memory**（文件系统）和**会话内 session memory**（数据库）。

#### 跨会话 Memory（文件系统，Markdown + YAML frontmatter）

```
<project>/.chatui/skills/          ← project-scoped memories
<project>/.chatui/skills/global/   ← global memories
```

每个 memory 是一个 `.md` 文件：

```markdown
---
name: flask-migration-circular-import
description: Fix for Flask SQLAlchemy circular import when using blueprints
enabled: true
tags: [python, flask, debugging]
created: 2026-03-15T10:00:00Z
---

# Flask + SQLAlchemy Circular Import Fix
When using Flask blueprints with SQLAlchemy models...
```

**发现机制**：不再把 memory 列表注入 prompt。模型通过 `search_memories(query)` 工具按需检索；system message 只注入一行计数提示 (`You have N accumulated memories...`)。

**两种发现路径：**

1. **显式发现（on-demand）**：模型自己判断当前任务可能需要过往经验，调用 `search_memories(query)`。返回 BM25 top-K 的紧凑索引（name + description + path），再用 `read_files` 加载需要的 memory 全文。

2. **隐式预取（proactive prefetch）**：用户新一轮消息到达时，`lib/memory/prefetch.py` 在后台跑一次级联

   ```
   BM25 coarse top-N  →  cheap-LLM 精选  →  注入 <relevant_memories> 块
   ```

   * 构造 query：取最近 K 轮 user+assistant 文本（剥离所有 tool call / tool result / thinking block），与 last user query 拼接。
   * BM25 在 name+description+tags+body 上打分，取 top-N（默认 40）。
   * 廉价模型看到候选摘要和最近对话，输出 JSON `[id, ...]`（最多 M 个，默认 5）。目的是 **precision 优先**——宁可返回 `[]` 也不要误召。
   * 命中的 memory 全文注入最后一条 user message 的 `<relevant_memories>` 块，用 `<system-reminder>` 包裹。
   * 前端会显示 `🧠 prefetched N memories via MODEL (1.4s)` 指示器，点击可展开查看具体注入了哪些 memory。

**BM25 相关性过滤**（`relevance.py`）：纯 stdlib math 实现，无外部依赖。作为 `search_memories` 工具和 prefetch 级联的共同底层。

```python
def search_memories(query, top_k=30):
    # 对 name + description + tags + body(前 2KB) tokenize
    # 计算 BM25 score → 按 score 排序 → 返回 top_k 索引
```

**两个发现路径的分工：**

| 场景 | 机制 |
|------|------|
| 模型已知道自己需要查什么（"this looks like a Flask blueprint issue"） | 显式 `search_memories` |
| 模型根本没意识到有可用的经验（"saved a trap about cuda_graph_scope last week"） | 隐式 prefetch |

隐式 prefetch 是为了解决"模型不会主动想起来去搜"这个痛点——即使写得再好的 memory，如果模型不搜，也等于不存在。

**成本控制：**
- 隐式 prefetch 只在 round 0（用户新一轮消息）触发，不在 tool round 重复。
- 单次调用限时 6-8 秒，超时则 fallback 到 BM25 top-K。
- BM25 零分时直接跳过 cheap-LLM 步骤。
- 注入总字节数上限 8KB，最多 5 条 memory。

### 1.4 Session Memory — 跨压缩的持久状态

灵感来自 Claude Code 的 `SessionMemory/` 系统。解决的核心问题是：**Force Compact 会丢失关键的工作状态**。

```python
# 在每个 tool-heavy turn 之后，后台线程提取持久笔记
def trigger_memory_extraction(conv_id, messages, tool_call_happened):
    # 阈值：首次 15K tokens，后续每 10K tokens + 5 tool calls
    if should_extract_memory(conv_id, messages):
        thread = Thread(target=extract_session_memory, ...)
        thread.start()
```

提取 prompt 指示 LLM 从对话中提取：
- **用户偏好和指令**
- **工作状态**（当前编辑的文件、分支、构建状态）
- **关键决策**
- **错误与解决方案**
- **待办事项**

提取的笔记存储在数据库的 `conversations.settings` JSONB 字段中（无需 schema 迁移），并在**每轮**注入 system message（作为 Layer 5）。

这意味着即使 Force Compact 把 30 轮对话压缩成了一段摘要，关键决策和工作状态仍然以 session memory 的形式存活。

### 1.5 Prompt Cache 优化

Prompt cache 是降本的关键。我们从 Claude Code 的 `promptCacheBreakDetection.ts` 获得灵感，构建了完整的**缓存感知系统**（`cache_tracking.py`）：

**1. Cache Break Detection (两阶段)**

```python
# Phase 1 (pre-call): hash system prompt + tools + message count
# 检测哪些变化"会"导致 cache break
detect_cache_break(conv_id, messages, tools=tools, model=model, usage=usage)

# Phase 2 (post-call): 检查 API 返回的 cache_read_tokens
# 确认 cache break 是否实际发生
log_round_cache_stats(conv_id, round_num, usage, model=model, tid=task_id)
```

**关键教训**：早期代码 hash 了 message PREFIX 的内容，但 micro-compact 每轮都在修改冷 tool results → 每轮都报 false positive。新方案分离了 "真正破坏缓存的东西"（system prompt, tools, model）和 "预期的内容变化"（tool result 压缩、新消息追加）。

**2. Cache-Aware Tool Result Ordering**

```python
# 在每轮 LLM 调用前，对 tool result messages 按 tool_call_id 排序
# 确保 prefix 在不同轮次间保持确定性
sort_tool_results(messages)
```

**3. Session-Stable TTL Latch**

```python
# 锁定 CACHE_EXTENDED_TTL 决策，整个 task 期间不变
# 防止中途切换 beta header 导致 cache key 变化
latch_extended_ttl(task_id)
```

### 1.6 Delta Attachment — 增量上下文追踪

每个 task 从前端收到**全新的 messages 列表**（不包含之前注入的 project/memory context）。但我们不需要每次都重新计算：

```python
# Delta tracking: 对 context 做 MD5 hash
# 如果 hash 没变，复用上次的计算结果（跳过 FUSE I/O）
# 但 text 仍然注入——只是跳过了计算
_last_context_cache: dict[tuple[str, str], tuple[str, str]] = {}

def _get_cached_or_compute(conv_id, category, compute_fn):
    h = _context_hash(text)
    prev = _last_context_cache.get(key)
    if prev and prev[0] == h:
        return prev[1]  # 命中——跳过了昂贵的 FUSE I/O
```

配合**Memory Prefetch**：在工具组装期间，2 线程池并行预加载 project context 和 memory context：

```python
_prefetch_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='mem-prefetch')
_prefetch_project_future = _prefetch_executor.submit(_prefetch_project)
_prefetch_memory_future = _prefetch_executor.submit(_prefetch_memory)
# 存储到 task dict，_inject_system_contexts 消费
task['_prefetch_project'] = _prefetch_project_future
task['_prefetch_memory'] = _prefetch_memory_future
```

---

## 2. 工具调用 (Tool Calling)

### 2.1 工具定义与分发架构

工具系统采用**三层架构**：

```
定义层   lib/tools/           — 各模块定义工具 schema（JSON Schema）
分发层   lib/tasks_pkg/tool_dispatch.py — 解析 tool_calls、审批、并行执行、结果注入
执行层   lib/tasks_pkg/handlers/       — 具体执行逻辑（按工具类别分文件）
```

工具组装是动态的（`model_config.py` → `_assemble_tool_list`）：根据用户启用的 feature flag（search、fetch、project、browser、swarm、code_exec、image_gen 等）组装当前可用的工具列表。

**工具去重缓存**（`tool_dispatch.py`）：

```python
# _make_cache_key(tool_name, args) → 确定性 hash
# 如果同一工具同一参数在同一 task 内被调用过，直接返回缓存结果
_tool_result_cache = task.get('_tool_result_cache', {})
cache_key = _make_cache_key(fn_name, fn_args)
if cache_key in _tool_result_cache:
    return _tool_result_cache[cache_key]  # 秒返回
```

### 2.2 流式工具执行 (Streaming Tool Execution)

这是我们从 Claude Code 的 `StreamingToolExecutor.ts` 学来的最重要的优化之一。

**问题**：传统模式下，模型生成完整响应 → 解析所有 tool calls → 串行/并行执行。如果模型一次调用 5 个 `read_files`，必须等模型说完才能开始执行第一个。

**解决方案**：`StreamingToolAccumulator`（`streaming_tool_executor.py`）：

```python
class StreamingToolAccumulator:
    """在模型 streaming 时，一旦某个 tool call 的参数完成，立即开始执行。"""
    
    _STREAMABLE_TOOLS = frozenset({
        'read_files', 'grep_search', 'find_files', 'list_dir',
        'web_search', 'fetch_url', 'check_error_logs',
    })
    
    def on_tool_call_ready(self, tool_call: dict):
        """SSE 流中检测到新的 tool_call index 出现时，
        说明前一个 tool call 的 args 已完成 → callback 触发"""
        
        # 1. 立即 emit tool_start SSE event
        #    → 前端立刻显示 "Searching..." / "Running..."
        
        # 2. 如果是只读工具，提交到线程池预执行
        if fn_name in _STREAMABLE_TOOLS:
            future = self._pool.submit(self._execute_one, ...)
            self._futures[tc_id] = (future, fn_name, fn_args, time.time())
```

流程：

```
模型 streaming: [tool_call_0: read_files] [tool_call_1: grep_search] [tool_call_2: ...]
                       ↓ args 完成                ↓ args 完成
               Thread Pool:                Thread Pool:
               read_files(...)              grep_search(...)
               (已经在执行了！)               (模型还在生成第3个tool call时已经在执行了)
```

模型 streaming 完成后：

```python
# 从线程池收割结果，注入到去重缓存
hit_count = _stream_acc.inject_into_cache(task)
# 后续 execute_tool_pipeline 发现缓存中已有结果 → 跳过重复执行
```

**还做了一个重要优化**：`on_tool_call_ready` 在检测到 tool call 时就立即发 `tool_start` SSE event，让前端无需等待整个 LLM response 完成就能显示工具状态面板。

### 2.3 工具结果预算管理 (Tool Result Budgeting)

前面 §1.2 已经提到了 Layer 0。这里补充**单轮聚合预算**的逻辑：

```python
MAX_ROUND_TOOL_RESULTS_CHARS = 300_000  # 单轮所有工具结果的总预算

def enforce_round_aggregate_budget(tool_results, conv_id):
    total = sum(len(c) for c, _, _ in tool_results.values())
    if total <= MAX_ROUND_TOOL_RESULTS_CHARS:
        return  # 没超
    
    # 按大小降序排列，最大的非豁免结果优先持久化到磁盘
    candidates.sort(key=lambda x: len(x[1]), reverse=True)
    for tc_id, content, tool_name, tool_use_id in candidates:
        if total <= budget:
            break
        persisted = _persist_to_disk(content, ...)
        total -= (len(content) - len(persisted))
```

### 2.4 工具钩子系统 (Tool Hooks)

灵感来自 Claude Code 的 `toolHooks.ts`（620 行），但做了架构适配：

```python
# Pre-hook: (tool_name, args, task) → HookResult | None
# Post-hook: (tool_name, args, result_content, task) → str | None

# 内置 hooks:
register_pre_hook(_run_command_safety_hook)    # 拦截危险命令
register_post_hook(_empty_result_marker_hook)  # 空结果标记
```

为什么不能完全复制 Claude Code 的 hook 系统：
- Claude Code 的 hooks 是用户可配置的 shell 脚本（`PreToolUse`/`PostToolUse`），在 CLI 环境下运行
- 我们是 web server 架构，用户无法在运行时注册 hook 脚本
- 我们的 approval 系统（`request_write_approval`）已经处理了 "阻止执行" 的场景

### 2.5 省 Token 工具：content_ref

**`content_ref` on `write_file`**：当模型需要将之前的工具结果写入文件时，用 `content_ref={tool_round: N}` 引用，而不是在 response 中重新生成整个文件内容。

---

## 3. Harness 编排 (Harness Programming)

"Harness" 是 agent 外围的编排代码——它决定了 LLM 何时被调用、如何处理 tool calls、何时停止、如何处理异常。

### 3.1 单 Agent 编排：Orchestrator 主循环

核心在 `orchestrator.py` 的 `run_task()`，一个 while 循环：

```
                  ┌────────────────────────────┐
                  │  Section 1: Config & Model  │
                  │  Resolution                 │
                  └─────────┬──────────────────┘
                            ↓
                  ┌────────────────────────────┐
                  │  Section 2: Tool Assembly   │
                  │  + Memory Prefetch          │
                  └─────────┬──────────────────┘
                            ↓
                  ┌────────────────────────────┐
                  │  Section 3: Context         │
                  │  Injection (§1.1)           │
                  └─────────┬──────────────────┘
                            ↓
             ┌──────────────────────────────────────┐
             │ WHILE round_num <= max_tool_rounds:   │
             │                                       │
             │  1. Abort check                       │
             │  2. Emit phase event                  │
             │  3. run_compaction_pipeline() (§1.2)  │
             │  4. compute_turn_attachments()        │
             │  5. (legacy memory-to-user removed)   │
             │  6. Emit messages_snapshot (debug)     │
             │  7. sort_tool_results() (cache-aware) │
             │  8. build_body() → body               │
             │  9. StreamingToolAccumulator (§2.2)   │
             │ 10. _llm_call_with_fallback() (§3.4)  │
             │ 11. Cache break detection              │
             │ 12. inject_into_cache() (streaming)    │
             │ 13. analyse_stream_result()            │
             │ 14. parse_tool_calls()                 │
             │ 15. execute_tool_pipeline()            │
             │ 16. Timeout circuit breaker            │
             │ 17. Crash-recovery checkpoint          │
             └──────────────┬───────────────────────┘
                            ↓
             ┌────────────────────────────────────┐
             │ Post-loop:                         │
             │  - Append final assistant reply     │
             │  - Session memory extraction (§1.4) │
             │  - _finalize_and_emit_done()        │
             │  - Suspicious completion detection  │
             └────────────────────────────────────┘
```

**关键设计决策：**

1. **WHILE 循环而非 FOR 循环**：上限可扩展。当 premature stream close 触发重试时，`_premature_retry_count` 增加，循环上限扩展。FOR 循环的 `range(max_tool_rounds + 1)` 无法做到这一点。

2. **不注入任何 `[SYSTEM NOTE]` 干扰模型**：我们禁止在运行时向 messages 注入反循环警告或预算提示。模型应该自主决定何时停止。

3. **Messages Snapshot for Debug**：每轮 LLM 调用前，emit 一个 `messages_snapshot` event 到前端的 debug panel，让开发者能看到模型实际接收的完整 messages。

### 3.2 自主模式：Endpoint (Planner → Worker → Critic)

这是我们的 "autonomous mode"——模型自主完成复杂任务，无需人工干预。三阶段架构：

```
Phase 0: PLANNER (执行一次)
    │   - 有完整工具访问权限（读代码、搜索、浏览目录）
    │   - 将用户的原始请求重写为结构化 Brief
    │   - Brief 替换原始 user message（Worker 看到的是计划，不是原始请求）
    ↓
Phase 1: WORKER (可循环多次)
    │   - 完整 LLM + 工具能力
    │   - 执行计划
    ↓
Phase 2: CRITIC (可循环多次)
    │   - 完整 LLM + 工具能力
    │   - 逐条验证 Planner 的 Checklist
    │   - 输出 [VERDICT: STOP] 或 [VERDICT: CONTINUE]
    ↓
    ├── STOP → 完成
    └── CONTINUE → 注入 feedback 作为 user message → 回到 Phase 1
```

**Planner 的设计哲学：**

> "Explore first, then plan. Spend your tool rounds reading the codebase to understand the current state before writing the checklist."

Planner 的 system prompt 强调：**先用工具探索，再做计划**。一个基于实际代码的计划远优于凭空猜测的计划。

Planner 输出的结构化格式：
```markdown
## Goal
## Context
## Checklist
1. <action> — **Verify:** <how to confirm>
## Acceptance Criteria
## Key Files / Areas
## Notes
```

**Critic 的设计哲学：**

Critic 不是橡皮图章。它有完整的工具访问权——能读文件、跑测试、grep 代码：

> "Reading code is not verification. Run it." / "Try to break it."

Critic 输出格式：
```markdown
### Checklist Status
- ✅ Item 1: 确认 + 证据
- ❌ Item 2: 缺少什么 + 具体修复

### Verdict
[VERDICT: STOP] 或 [VERDICT: CONTINUE]
```

**防卡死机制**：

```python
if _detect_stuck(feedback_history):
    # 连续 2+ 轮类似的 feedback → 停止
    is_stuck = True
    should_stop = True
    stop_reason = 'stuck'
```

**DB 持久化**：每个 phase 完成后，endpoint turns 立即同步到数据库。这意味着即使 SSE 断连、页面刷新、甚至服务器崩溃，用户刷新页面后仍能看到完整的多轮交互历史。

**`_run_single_turn()` 的复用**：

Worker 和 Critic 都通过 `_run_single_turn()` 执行——这是 `run_task()` 的可复用原语。它重置 per-turn 累积字段，调用完整的 orchestrator 逻辑（包括上下文注入、压缩、工具执行），但**不发 done event**（由 endpoint.py 决定何时整体完成）。

### 3.3 多 Agent 编排：Swarm 系统

Swarm 是我们的多 agent 并行执行系统，经历了两个大版本：

**V1 (Wave-Barrier)**：
```
Wave 1: [Agent A, Agent B] → 等两个都完成 → Wave 2: [Agent C, Agent D]
```

**V2 (Streaming Scheduler)**：
```
A, B 同时启动。A 先完成 → C 立刻启动（B 还在跑）。
B 完成 → D 立刻启动。
没有 wave 屏障，依赖满足就启动。
```

架构分为多个子模块：

```
lib/swarm/
  scheduler.py    — StreamingScheduler（DAG 调度，线程安全）
  master.py       — MasterOrchestrator（反应式编排）
  agent.py        — SubAgent（隔离执行环境）
  review.py       — ReviewMixin（Master 审查逻辑）
  planner.py      — DAG 依赖解析（Kahn 算法）
  protocol.py     — 数据结构（SubTaskSpec, ArtifactStore, SwarmEvent）
  rate_limiter.py — Token-bucket 限流器
  integration.py  — 与主 orchestrator 的胶水层
```

#### StreamingScheduler 的核心

```python
class StreamingScheduler:
    """依赖感知的流式 agent 调度器。"""
    
    def add_specs(self, specs):
        """添加 specs 并立即启动依赖已满足的。"""
        # Kahn 算法检测环
        # 去重（防止 master review 重复 spawn）
        # 依赖注入（前序 agent 的结果注入 context）
    
    def iter_completions(self):
        """生成器：每个 agent 完成时 yield。"""
        # 关键修复：queue.put() 在 _lock 内执行
        # iter_completions 在同一把锁下 drain queue + check idle
        # 防止结果在 queue-drain 和 idle-check 之间滑过
```

**并发竞态的关键修复**：当 agent factory 函数同步完成时（测试中常见），结果在 `iter_completions` 开始 drain 之前就已入队。旧代码的 drain 循环和 idle 检查不是原子的——结果可能在 "queue empty check" 和 "idle check" 之间完成。修复：用单个 `_completion_event` (threading.Event)，idle 检查在保护 `_running` 和 `_pending` 的同一把锁下进行。

#### MasterOrchestrator 的反应式循环

```python
def run_reactive(self, original_query):
    """真正的流式反应式循环。"""
    
    scheduler = self._build_scheduler()
    scheduler.add_specs(self.specs)
    
    for spec, result in scheduler.iter_completions():
        self._record_batch([(spec, result)])
        
        # 每 N 个完成（或 scheduler idle 时），触发 master review
        if should_review:
            # Fast-path: 所有 agent 成功 + 结果干净 → 跳过 review
            if self._check_fast_path_eligible(self._results):
                master_said_done = True
                break
            
            # 增量 review: 已审查的结果压缩，新结果完整展示
            prompt = self._build_incremental_review_prompt(query, watermark)
            # Master LLM 决定: swarm_done 或 spawn_more_agents
            decision = self._master_review(prompt)
            
            if decision == 'spawn_more':
                # 热注入新 specs 到同一个 scheduler 实例
                scheduler.add_specs(new_specs)
                # 新 agent 立刻启动（如果依赖满足）
    
    return self._synthesise(query)  # 最终综合
```

**Master Review 的增量优化**：

```python
def _build_incremental_review_prompt(self, query, last_reviewed_index):
    # 已审查的结果 → 压缩摘要（master 之前看过了）
    # 新结果 → 完整展示
    # 这显著降低了后续 review 的 token 使用量
```

**Fast-Path**：当所有 agent 成功且结果没有错误指标时，跳过 master review → 减少延迟和 token。

#### SubAgent 的设计

每个 SubAgent 是完全隔离的执行环境：

```python
class SubAgent:
    """隔离的工作者 agent。"""
    
    def __init__(self, spec, parent_task, all_tools, ...):
        # 独立的 message history
        self.messages = self._build_initial_messages(system_prompt_base)
        # 按角色过滤工具列表
        self.tools = scope_tools_for_role(spec.role, all_tools)
        # 注入 artifact tools（如果有共享 store）
        if self.artifact_store:
            self._inject_artifact_tools()
    
    def run(self):
        """同步执行。安全网：timeout、abort、max_rounds。"""
        self._run_loop(start_time)
        # 如果 run_loop 结束时仍然 "running" → 提取部分答案
```

**ArtifactStore**：线程安全的共享 key-value 存储，跨 agent 数据传递：

```python
# Agent A: store_artifact(key="analysis_result", content=data)
# Agent B: read_artifact(key="analysis_result")
```

**角色系统**（`registry.py`）：不同角色有不同的工具权限和模型推荐：

```python
# coder: 完整工具 + 强模型
# researcher: 搜索+读取工具 + 标准模型
# analyst: 读取+计算工具 + 经济模型
```

### 3.4 可靠性工程：异常检测与自愈

Agentic 系统的可靠性需要特别关注——LLM 的不确定性加上网络和工具的不稳定性，需要多层防护。

#### 自动 Fallback

```python
def _llm_call_with_fallback(task, body, model, ...):
    try:
        msg, fr, usage = stream_llm_response(task, body, ...)
    except Exception:
        # 主模型失败 → 自动切换到配置的 fallback model
        # 重新 build_body（可能需要调整参数）
        _FALLBACK_MODEL = _get_fallback_model()
        body = build_body(_FALLBACK_MODEL, messages, ...)
        msg, fr, usage = stream_llm_response(task, body, ...)
```

#### Reactive Compact（LLM 返回 Context Too Long 时）

```python
# 当 LLM 返回 "context_length_exceeded" 错误时
# 不是直接失败，而是触发 reactive compact
# 压缩后重试 LLM 调用
_reactive_compact_attempts: dict[str, int] = {}
_REACTIVE_COMPACT_MAX_RETRIES = 2
```

#### 可疑完成检测

```python
def _check_suspicious_completion(task, last_finish_reason, ...):
    # 检测模式：
    # - 空内容 + 空 thinking + 无错误 → 可能安全过滤
    # - 工具调用后内容 < 50 chars → 可能提前停止
    # - max_rounds_exhausted → 可能无限工具循环
    # - finish_reason 为 None → stream 异常
    # - 不到 1 秒完成 + 空内容 → API 错误
    
    if suspicion_reasons:
        logger.warning('⚠️ SUSPICIOUS COMPLETION: %s', suspicion_reasons)
```

#### 连续工具超时断路器

```python
_consecutive_tool_timeouts = 0
_MAX_CONSECUTIVE_TOOL_TIMEOUTS = 3

if _tool_timed_out:
    _consecutive_tool_timeouts += 1
    if _consecutive_tool_timeouts >= _MAX_CONSECUTIVE_TOOL_TIMEOUTS:
        # 强制停止——防止失控任务
        task['error'] = '⚠️ 3 consecutive tool timeouts'
        break
else:
    _consecutive_tool_timeouts = 0  # 成功就重置
```

#### Premature Stream Close 重试

```python
# SSE 流提前关闭（网络抖动）→ 不是立即失败
# 而是重试（上限 2 次），WHILE 循环的上限自动扩展
_premature_retry_count += 1
# round_num + 1 <= max_tool_rounds + _premature_retry_count
continue  # 重试这一轮
```

#### Crash-Recovery Checkpoint

```python
# 每 5 秒（throttled），将当前 content/thinking 持久化到 DB
# 服务器崩溃时，用户刷新页面可以看到之前的部分结果
if _now - _last_checkpoint >= 5:
    checkpoint_task_partial(task)
```

---

## 4. 经验教训与反模式

### ❌ 反模式 1: 用 f-string 在 system prompt 中嵌入高频变化的值

```python
# ❌ 每轮都变 → 破坏 prompt cache → 成本翻倍
system_msg += f"Current time: {datetime.now()}"

# ✅ 只放日期（每天变一次）+ 放在 system msg 末尾
system_msg += f"Current date: {date_str}"  # UTC date-only
```

### ❌ 反模式 2: Micro-compact 修改 cache prefix 内的消息

A/B 测试证明这导致 57% 成本增加。解决方案：micro-compact 跳过 `cache_prefix_count` 内的所有消息。

### ❌ 反模式 3: 截断 read_files 结果

模型会立刻重新调用 → 浪费更多 token。正确做法：豁免 + 后续用 micro-compact 在变"冷"后压缩。

### ❌ 反模式 4: Wave Barrier 调度

V1 swarm 用 "等所有 agent 完成再启动下一波"。一个慢 agent 拖住整个 wave。V2 改成 DAG + 依赖满足即启动，吞吐量提升显著。

### ❌ 反模式 5: 在运行时注入 `[SYSTEM NOTE]` 干扰模型

早期尝试在模型接近工具轮次上限时注入 "WARNING: approaching tool limit" 消息。结果：模型行为变得不稳定，有时提前停止，有时忽略。正确做法：让模型自主决定，靠 hard cap 做安全网。

### ✅ 模式 1: Memory 列表放 user message，指令放 system message

分离可变内容和不可变内容。Memory CRUD 操作不再破坏 system prompt 的 cache。

### ✅ 模式 2: 持久化到磁盘而非截断

信息永不丢失。模型可以用 `read_files` 按需回看。

### ✅ 模式 3: Background Session Memory

最 "invisible" 但最有价值的机制。用户感知不到，但长对话的质量显著提升。

---

## 5. 对标 Claude Code：差异与借鉴

| 维度 | Claude Code | Tofu | 状态 |
|------|------------|------|------|
| **Streaming Tool Exec** | StreamingToolExecutor.ts | StreamingToolAccumulator | ✅ 已对齐 |
| **Tool Result Budgeting** | toolResultStorage.ts | Layer 0 + 磁盘持久化 | ✅ 已对齐 |
| **Micro-compact** | microCompact (edit outside cache prefix) | cache-aware micro_compact | ✅ 已对齐 |
| **Session Memory** | SessionMemory/ + CacheSafeParams | background dispatch_chat | ✅ 已对齐（架构不同） |
| **Memory System** | CLAUDE.md + @include | .chatui/skills/ + BM25 | ✅ 已对齐 |
| **Delta Attachments** | Per-section hash tracking | _get_cached_or_compute | ✅ 已对齐 |
| **Memory Prefetch** | startRelevantMemoryPrefetch() | 2-thread prefetch pool | ✅ 已对齐 |
| **Prompt Cache Detection** | promptCacheBreakDetection.ts | cache_tracking.py | ✅ 已对齐 |
| **Todo Tracking** | TodoWriteTool + continuation enforcer | ❌ 无 | 🔜 Backlog |
| **Planner Write-Block** | disallowedTools on planners | ❌ 无（planner 有完整工具） | 🔜 Backlog |
| **Forked Sub-agent** | prompt cache sharing via fork | 不可能（web server + proxy） | ⛔ 架构限制 |
| **Hook Scripts** | PreToolUse/PostToolUse shell hooks | 程序化 hook registry | ⚠️ 部分对齐 |
| **Speculation/Overlay** | Branch speculation + overlay display | ❌ 无 | 🔜 Backlog |

**Tofu 优于 Claude Code 的方面**：
- **DB 持久化的 endpoint turns**（vs Claude Code 的 boulder.json 文件）
- **StreamingScheduler 的 DAG 调度**（vs 无调度器）
- **反应式 master review + 增量 prompt**
- **跨会话 memory + skills 系统**

---

## 总结

Agentic 开发的三个维度紧密交织：

1. **上下文工程**解决 "模型看到什么" 的问题——分层注入、渐进压缩、缓存稳定性。
2. **工具调用**解决 "模型能做什么" 的问题——流式预执行、预算管理、省 token 机制。
3. **Harness 编排**解决 "谁控制谁" 的问题——单 agent 循环、自主三阶段、多 agent DAG。

三者共同构成了一个在生产环境中可靠运行的 agentic 系统。
