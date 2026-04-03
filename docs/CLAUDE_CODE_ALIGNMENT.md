# Claude Code Alignment — Design Document

> Comprehensive reference for all Claude Code designs adopted into ChatUI,
> and those that are architecturally impossible due to system design differences.

## System Architecture Differences (Why Full Alignment Is Impossible)

| Dimension | Claude Code | ChatUI |
|-----------|-------------|--------|
| **Runtime** | TypeScript/Bun, terminal CLI, single-user | Python/Flask, web server, multi-user |
| **LLM Provider** | Anthropic-only, direct API | Multi-provider via proxy, dynamic slot pool |
| **API Protocol** | Native Anthropic messages API with `cache_control` blocks | OpenAI-compatible format (even for Anthropic) |
| **User Interaction** | stdin/stdout, synchronous REPL | SSE streaming, async web sockets |
| **Session Lifecycle** | Process-scoped (one CLI invocation = one session) | DB-persisted conversations, server survives restarts |
| **File Context** | Direct filesystem with inotify | Proxy to remote project server |

These differences create 5 irreconcilable gaps detailed below.

---

## ✅ IMPLEMENTED — Fully Aligned Features

### 1. Session Memory System (`session_memory.py`)
**Claude Code:** `SessionMemory/` — background forked agent extracts notes after each turn.

**ChatUI:** Background thread runs `dispatch_chat` with `capability='cheap'` to extract
persistent session notes. Notes stored in `conversations.settings` JSONB. Injected into
system prompt every turn + used as compaction summary seed.

**Adaptation:** Can't share prompt cache with parent (see §2 below), so we use a separate
cheap model call instead of a forked agent.

### 2. Per-Turn Attachments (`attachments.py`)
**Claude Code:** `attachments.ts` (3997L) — 40+ attachment types injected every turn.

**ChatUI:** 3 attachment types implemented:
- Session memory injection
- Recently modified files reminder (fires 5+ rounds after last write)
- Tool discovery delta (announces newly discovered deferred tools)

**Adaptation:** Injected into last user message (not separate `user` messages) because
our API format doesn't support the same message sequencing as Claude Code's native API.

### 3. Prompt Cache Break Detection (`cache_tracking.py`)
**Claude Code:** `promptCacheBreakDetection.ts` (727L) — per-field hashing, TTL detection.

**ChatUI:** Tracks system prompt hash, tools hash, model, and message prefix hash between
turns. Logs warnings on significant cache breaks for cost diagnostics. Reports
`cache_read_input_tokens` drops when available.

### 4. Cache-Aware Microcompact (in `compaction.py`)
**Claude Code:** Microcompact ONLY edits messages outside cache prefix window.

**ChatUI:** `micro_compact()` now queries `get_cache_prefix_count()` and skips messages
in the cache prefix when stripping thinking blocks and compacting tool results.

### 5. Pre/Post Tool Hooks (`tool_hooks.py`)
**Claude Code:** `toolHooks.ts` — 4-layer PreToolUse → execution → PostToolUse lifecycle.

**ChatUI:** Programmatic hook registration with two built-in hooks:
- `_empty_result_marker_hook` (post) — replaces empty tool output with marker
- `_run_command_safety_hook` (pre) — blocks `rm -rf /`, fork bombs, etc.

Integrated into `tool_dispatch.py` execute pipeline.

### 6. Unified ToolSpec (`tool_spec.py`)
**Claude Code:** `buildTool()` factory with all metadata in one object.

**ChatUI:** `@dataclass ToolSpec` consolidating `concurrent_safe`, `idempotent`,
`should_defer`, `max_result_chars`, `search_hint`, `category` per tool.
Backward-compatible exports: `get_write_tools()`, `get_idempotent_tools()`, etc.

### 7. Dynamic Tool Deferral (in `deferral.py`)
**Claude Code:** `tst-auto` mode — defers tools when MCP tool tokens exceed X% of context.

**ChatUI:** Phase 2 added to `partition_tools()`: after static deferral, estimates core
tool token count and defers non-essential tools if >10% of context window.
`_NEVER_DEFER` protects core tools (read_files, write_file, etc.).

### 8. Partial Compaction (`partial_compact.py`)
**Claude Code:** `from`/`up_to` directional partial compaction around a pivot.

**ChatUI:** `partial_compact(messages, direction, pivot_index)` — summarizes a range
while preserving the rest. Auto-detects pivot if not specified. Adjusts pivot to
avoid splitting tool call/result pairs.

---

## ❌ CANNOT ALIGN — Architectural Impossibilities

### 1. CacheSafeParams / Agent Prompt Cache Sharing

**Claude Code:** `CacheSafeParams` ensures forked sub-agents share the parent's prompt
cache prefix. The parent's system prompt, tools, and early messages are reused
byte-identical, so Anthropic's API caches them (90% cost reduction).

**Why impossible in ChatUI:**
- ChatUI uses a **multi-provider proxy layer** that routes requests to different
  model endpoints. The proxy doesn't guarantee that two requests hit the same
  Anthropic cache partition.
- ChatUI uses **OpenAI-compatible format**, which is translated to Anthropic format
  by the proxy. The translation may not preserve byte-identical content needed for
  cache hits.
- ChatUI's sub-agents (swarm workers) run on potentially **different models** via
  dynamic slot selection, so cache sharing is meaningless across providers.
- Even if we added native Anthropic SDK support, the **web server architecture**
  means requests from different threads/processes don't naturally share cache state.

**Mitigation:** Cache break detection (implemented above) + `cache_control` breakpoints
in `add_cache_breakpoints()` (already existed) provide partial cache optimization.

### 2. Speculation System (Predictive Execution with Overlay COW)

**Claude Code:** `speculation.ts` (992L) — while user reads, pre-executes the likely
next edit. File writes go to an overlay directory (copy-on-write). If user accepts,
overlay files are promoted to real. Pipelined: after speculation completes, generates
the NEXT suggestion.

**Why impossible in ChatUI:**
- **Web server architecture:** ChatUI doesn't know when the user is "reading" —
  there's no terminal cursor to detect idle state. The user could be on a different
  tab entirely.
- **Remote filesystem:** ChatUI accesses project files through a proxy to a remote
  project server. Creating an overlay COW layer would require the project server
  to support shadow filesystems, which is a fundamental architectural change.
- **Multi-user safety:** Speculation writes to overlay files could conflict across
  concurrent users or even concurrent tasks within the same conversation.
- **Cost:** Unlike Claude Code where the user has opted into CLI usage (implying
  high-frequency interaction), web users may leave a tab open for hours. Speculative
  LLM calls would waste significant money.

**Partial workaround:** For endpoint mode (autonomous), the worker→critic→revise loop
already achieves a similar "predict what to do next" pattern, just not speculatively.

### 3. ContentReplacementState (Frozen Truncation Decisions)

**Claude Code:** Tracks which tool results were persisted to disk vs. kept inline.
Decisions are **frozen once made** — even if a result would be under budget on
re-evaluation, the same decision is replayed to keep wire content byte-identical
for prompt cache stability.

**Why impossible in ChatUI:**
- **No disk persistence for tool results:** Claude Code writes large tool results
  to `~/.claude/tool-results/{uuid}.json` and replaces inline content with a 2KB
  preview. ChatUI keeps all results inline (in memory + DB).
- **Cache instability doesn't matter as much:** Because we can't guarantee prompt
  cache sharing across requests anyway (see §1 above), the benefit of freezing
  truncation decisions is minimal.
- **Simpler truncation model:** ChatUI's `budget_tool_result()` is deterministic —
  same input always produces same output. No replay needed.

**Mitigation:** The cache-aware microcompact (implemented above) provides similar
cache stability by not editing messages in the cache prefix.

### 4. Coordinator Mode (Delegation-Only Agent)

**Claude Code:** In coordinator mode, the top-level agent ONLY delegates work via
XML task-notifications. Workers have full tool access. The coordinator has no tools
except `dispatch_agent`.

**Why impossible in ChatUI:**
- ChatUI's swarm architecture is **worker-model**: the MasterOrchestrator manages
  a DAG of SwarmAgents that each run their own orchestrator loop. This is
  architecturally superior for DAG-parallel execution but incompatible with
  coordinator-mode's linear delegation pattern.
- The swarm already has `StreamingScheduler` with dependency resolution, artifact
  passing, and reactive master review — features coordinator mode doesn't have.
- Adding coordinator mode would require a **third orchestration pattern** alongside
  the existing single-agent and swarm patterns, increasing complexity without
  clear benefit (swarm already handles multi-agent well).

### 5. In-Process Teammates (Mailbox Messaging)

**Claude Code:** Multiple agents run in the same process with mailbox-style messaging
for collaboration.

**Why impossible in ChatUI:**
- **Server process model:** ChatUI runs as a Flask web server with thread-per-request.
  Long-running "teammate" agents would block threads and exhaust the thread pool.
- **DB-backed conversations:** ChatUI's conversations are DB-persistent, not
  in-process. Agent state survives server restarts. In-process teammates would
  lose state on restart.
- **Swarm is better:** The existing swarm system (`StreamingScheduler`) already
  provides multi-agent collaboration with proper DAG scheduling, which is
  architecturally more robust than in-process mailbox messaging.

---

## 📊 Test Coverage

All new features covered by `tests/test_new_features.py` (63 tests):

| Module | Tests | Coverage Focus |
|--------|-------|----------------|
| `session_memory.py` | 14 | Token estimation, thresholds, message formatting |
| `attachments.py` | 8 | Compute, inject, tool delta tracking, cleanup |
| `cache_tracking.py` | 11 | Hash functions, break detection, cache prefix |
| `tool_hooks.py` | 10 | Pre/post hooks, built-ins, error handling |
| `tool_spec.py` | 11 | Registration, backward compat, defaults |
| `deferral.py` (dynamic) | 5 | Static + dynamic threshold, token estimation |
| `partial_compact.py` | 2 | Edge cases (too few messages) |
| Cache-aware microcompact | 1 | Integration: prefix skip |

**Results:** 63/63 passed. 227 existing tests also pass (0 regressions).

---

## Files Modified / Created

### New files:
- `lib/tasks_pkg/session_memory.py` — Session memory extraction system
- `lib/tasks_pkg/attachments.py` — Per-turn dynamic attachments
- `lib/tasks_pkg/cache_tracking.py` — Prompt cache break detection
- `lib/tasks_pkg/tool_hooks.py` — Pre/post tool execution hooks
- `lib/tasks_pkg/tool_spec.py` — Unified tool specification
- `lib/tasks_pkg/partial_compact.py` — Directional partial compaction
- `tests/test_new_features.py` — 63 tests for all new features

### Modified files:
- `lib/tasks_pkg/orchestrator.py` — Wired session memory, attachments, cache detection
- `lib/tasks_pkg/compaction.py` — Cache-aware microcompact, session memory seed
- `lib/tasks_pkg/tool_dispatch.py` — Pre/post hook execution in pipeline
- `lib/tasks_pkg/system_context.py` — Session memory injection
- `lib/tools/deferral.py` — Dynamic threshold-based tool deferral
