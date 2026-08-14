# OMC / Claude Code → Tofu: Orchestration Backport Analysis

> **Date:** 2026-04-03  
> **Scope:** Oh My OpenCode (Atlas/Sisyphus), Claude Code (AgentTool/TodoWriteTool), Tofu (endpoint mode + swarm)  
> **Purpose:** Identify high-value orchestration patterns to backport into Tofu

---

## Table of Contents

1. [System Comparison Matrix](#1-system-comparison-matrix)
2. [Concept Mapping: OMC → Claude Code → Tofu](#2-concept-mapping)
3. [Gap Analysis](#3-gap-analysis)
4. [Recommendations (Prioritized)](#4-recommendations)
5. [Already Well-Covered by Tofu](#5-already-well-covered)
6. [Not Applicable to Tofu](#6-not-applicable)

---

## 1. System Comparison Matrix

| Dimension | OMC (Atlas/Sisyphus) | Claude Code | Tofu (chatui) |
|---|---|---|---|
| **Architecture** | 3-layer: Planning → Orchestration → Workers | Single agent + forked subagents | Endpoint mode (Planner→Worker→Critic) + Swarm (Master→SubAgents) |
| **Planning** | Prometheus (interviewer) + Metis (gap-finder) + Momus (reviewer) | Plan agent (read-only, system prompt enforced) | Planner phase in endpoint mode (full tools, structured brief) |
| **Orchestration** | Atlas: conductor, never writes code, only delegates | Parent agent manages forks, todo tracking | MasterOrchestrator: reactive DAG scheduling, review loop |
| **Workers** | Sisyphus-Junior + specialists (Oracle, Librarian, Frontend, Explore) | General-purpose fork, explore agent, verification agent | SubAgent with role-based system suffix, model tier resolution |
| **Task tracking** | TodoWrite + boulder.json persistence, todo continuation enforcer | TodoWriteTool (pending/in_progress/completed), verification nudge | Endpoint checklist in planner brief (text, not structured) |
| **Verification** | Atlas never trusts claims; lsp_diagnostics + test execution + visual QA | Verification agent (read-only, adversarial probes, VERDICT format) | Critic phase (full tools, checklist verification, [VERDICT: STOP/CONTINUE]) |
| **Cross-task learning** | Wisdom accumulation (conventions, successes, failures, gotchas, commands) | None built-in | Session memory extraction + skills system |
| **Delegation discipline** | Runtime hooks block Atlas from Write/Edit; 6-section prompt structure | Tool allowlist/denylist per agent; disallowedTools enforcement | Tool scoping via `scope_tools_for_role()` in swarm registry |
| **Model routing** | Category-based (visual-engineering, ultrabrain, quick, etc.) | Model inheritance (fork shares parent's model for cache reuse) | Model tier system (light/standard/heavy) derived from parent model |
| **Persistence** | boulder.json survives session crashes/restarts | Session-local only (todos cleared on exit) | DB-persisted endpoint turns, session memory in conversations.settings |

---

## 2. Concept Mapping

### OMC → Tofu

| OMC Concept | OMC Implementation | Tofu Equivalent | File | Gap? |
|---|---|---|---|---|
| **Atlas (conductor)** | `src/agents/atlas.ts` — orchestrator that only delegates, never writes code | `MasterOrchestrator` in swarm; Planner in endpoint mode | `lib/swarm/master.py`, `lib/tasks_pkg/endpoint.py` | **Partial** — Tofu's master CAN spawn agents but doesn't enforce "conductor-only" discipline |
| **Sisyphus-Junior (worker)** | `src/agents/sisyphus-junior.ts` — focused executor, can't delegate, obsessive todo tracking | `SubAgent` in swarm; Worker turn in endpoint mode | `lib/swarm/agent.py`, `lib/tasks_pkg/orchestrator.py` | **Close** — SubAgent already scoped, but no structured todo tracking |
| **6-section delegation prompt** | Task, Expected Outcome, Required Tools, Must Do, Must Not Do, Context | Ad-hoc objective string in `SubTaskSpec` | `lib/swarm/protocol.py` | **Gap** — no structured delegation template |
| **Todo continuation enforcer** | Hook injects "You have incomplete todos!" reminder into context | No equivalent — Critic provides feedback but doesn't inject todo reminders mid-turn | `lib/tasks_pkg/tool_hooks.py` (hook system exists but no todo hook) | **Gap** |
| **Boulder state (crash persistence)** | `boulder.json` — tracks plan + session ID for resume | Endpoint turns persisted to DB via `_sync_endpoint_turns_to_conversation` | `lib/tasks_pkg/endpoint.py` | **Covered** — DB persistence is more robust than file-based |
| **Wisdom accumulation** | After each task, extract conventions/successes/failures/gotchas/commands → pass to next task | `session_memory.py` + skills system | `lib/tasks_pkg/session_memory.py`, `lib/tasks_pkg/handlers/memory.py` | **Mostly covered** — session memory is cross-turn, skills are cross-session; but no automatic "pass wisdom to next subtask" in swarm |
| **Atlas runtime hooks** | `src/hooks/atlas/index.ts` — blocks Write/Edit tools, injects boulder state | `tool_hooks.py` — pre/post hooks exist; `_run_command_safety_hook` blocks dangerous commands | `lib/tasks_pkg/tool_hooks.py` | **Partial** — hook infrastructure exists but not used for orchestrator discipline |
| **Category-based delegation** | visual-engineering, ultrabrain, quick, etc. (semantic, not model names) | `lib/swarm/registry.py` — role-based (researcher, coder, analyst) + model tiers (light/standard/heavy) | `lib/swarm/registry.py` | **Partial** — tiers exist but categories are less semantic |
| **Verification protocol** | lsp_diagnostics + test execution + visual QA + manual file reading | Critic phase uses full tools to verify | `lib/tasks_pkg/endpoint_review.py` | **Partial** — Critic CAN use tools but not prompted for adversarial verification |
| **"Never trust subagent claims"** | Atlas always verifies independently before marking task done | Critic verifies but is a separate phase, not embedded in swarm master review | `lib/swarm/review.py` | **Partial** — master review exists but doesn't have explicit "don't trust" posture |

### Claude Code → Tofu

| Claude Code Concept | Claude Code Implementation | Tofu Equivalent | File | Gap? |
|---|---|---|---|---|
| **TodoWriteTool** | Structured tool: pending→in_progress→completed states, exactly 1 in_progress at a time | No structured todo tool | None | **Gap** |
| **Verification agent** | Built-in read-only agent with adversarial verification protocol, VERDICT output | Critic in endpoint mode (similar verdict) | `lib/tasks_pkg/endpoint_review.py` | **Partial** — Critic is good but not adversarial/read-only enforced |
| **Verification nudge** | When 3+ todos completed without verification step, tool result appends reminder | No equivalent | None | **Gap** |
| **Fork subagent** | "Don't peek" philosophy, inherits parent context, directive-style prompts, shares prompt cache | SubAgent in swarm (separate context, not forked) | `lib/swarm/agent.py` | **Different approach** — Tofu uses isolated agents, not forks |
| **Explore agent** | Read-only, glob+grep specialist | Swarm role "researcher" with scoped tools | `lib/swarm/registry.py` | **Close** — similar concept |
| **Plan agent** | Read-only architect, designs implementation plans, no file modifications | Planner phase in endpoint mode | `lib/tasks_pkg/endpoint_review.py` | **Close** — same idea |
| **Agent tool allowlist/denylist** | `tools` and `disallowedTools` per agent definition | `scope_tools_for_role()` | `lib/swarm/registry.py` | **Covered** |
| **shouldDefer (tool batching)** | TodoWriteTool deferred to avoid blocking main turn | No tool batching concept | N/A | **Minor gap** |
| **Agent listing as attachment** | Dynamic agent list moved from tool description to message attachment for cache stability | Skills listing already in user message (not system) for cache stability | `lib/tasks_pkg/system_context.py` | **Covered** (via memory `memory-listing-user-message-cache-stability`) |

---

## 3. Gap Analysis

### 3.1 🔴 Structured Todo Tracking (HIGH PRIORITY)

**What OMC/CC do:**
- OMC: TodoWrite tool with obsessive tracking; todo continuation enforcer hook injects `[SYSTEM REMINDER - TODO CONTINUATION] You have incomplete todos!` whenever the agent tries to stop with uncompleted items.
- Claude Code: TodoWriteTool with pending/in_progress/completed states, verification nudge when 3+ items closed without verification step.

**What Tofu does:**
- Endpoint mode: Planner produces a text checklist. Critic evaluates checklist items. But there's no structured, machine-readable todo state that persists across rounds.
- Swarm: No todo tracking at all — each SubAgent runs until it's "done" (early-stop patterns).

**Impact:** Without structured todo tracking, agents frequently claim completion prematurely. The Critic catches this post-hoc, but a mid-turn enforcement (like OMC's continuation enforcer) would be more effective and cheaper (avoids a full Critic round for premature stops).

### 3.2 🔴 Mid-Turn Completion Prevention (HIGH PRIORITY)

**What OMC does:**
- The todo continuation enforcer hook fires at every `Stop` event. If incomplete todos exist, it injects a system reminder into the context, preventing the model from generating a final response. This is the "boulder pushing" mechanism — the agent can never put the boulder down until ALL items are checked off.

**What Tofu does:**
- Endpoint mode: The Critic catches premature completion AFTER the worker finishes. This costs a full Critic turn (expensive).
- Orchestrator: `_check_suspicious_completion()` detects empty content and other anomalies, but only logs warnings — doesn't prevent premature stops.

**Impact:** Each unnecessary Critic round costs thousands of tokens and 10-30 seconds. A pre-emptive "you're not done yet" injection would be cheaper and faster.

### 3.3 🟡 Structured Delegation Prompts (MEDIUM PRIORITY)

**What OMC does:**
- Atlas uses a mandatory 6-section delegation prompt structure: Task, Expected Outcome, Required Tools, Must Do, Must Not Do, Context. This ensures every sub-agent receives unambiguous instructions.

**What Tofu does:**
- `SubTaskSpec` has `role`, `objective`, `context`, and `depends_on`. The system prompt is built from the base system prompt + role suffix + agent identity. But the delegation prompt (what the agent sees as its task) is just a free-form `objective` string.

**Impact:** Free-form objectives lead to scope drift and ambiguous success criteria. A structured template would improve first-pass quality.

### 3.4 🟡 Adversarial Verification Posture (MEDIUM PRIORITY)

**What Claude Code does:**
- The verification agent has an explicit "try to break it" mandate with anti-rationalization prompts ("If you catch yourself writing an explanation instead of a command, stop. Run the command."). It runs actual commands, not just code review. Its verdict format (PASS/FAIL/PARTIAL with evidence) is strictly enforced.

**What Tofu does:**
- The Critic checks against the planner's checklist and CAN use tools. But the Critic prompt doesn't enforce adversarial testing, doesn't have anti-rationalization language, and doesn't require command output as evidence.

**Impact:** The Critic often "reviews" by reading the worker's claims rather than independently verifying. Adding adversarial language and evidence requirements would improve verification quality.

### 3.5 🟡 Orchestrator Discipline Hooks (MEDIUM PRIORITY)

**What OMC does:**
- Runtime hooks intercept Atlas tool calls:
  - If Atlas tries to use Write/Edit on files outside `.sisyphus/`, the hook blocks and reminds Atlas to delegate.
  - Progress tracking hook injects boulder state ("3/10 tasks done") into context.

**What Tofu does:**
- `tool_hooks.py` has the infrastructure (pre/post hooks, HookResult with block/modify) but only uses it for: (a) empty result markers, (b) dangerous command blocking.

**Impact:** The hook infrastructure is already built — it just needs new hooks for orchestrator discipline.

### 3.6 🟢 Wisdom Passing Between Sub-Tasks (LOW PRIORITY)

**What OMC does:**
- After each task, Atlas extracts learnings into `.sisyphus/notepads/{plan-name}/` categories: conventions, successes, failures, gotchas, commands. These are passed to ALL subsequent tasks in the same plan.

**What Tofu does:**
- Session memory extracts working state after tool-heavy turns and stores it in the conversation's settings. Skills accumulate across sessions. But within a swarm run, there's no explicit "pass learnings from completed agents to upcoming agents."

**Impact:** Low priority because Tofu's swarm already passes dependency results between agents (`_inject_dependency_context`), and the swarm master review provides centralized learning. However, for very large swarms with many sequential tasks, explicit wisdom passing could reduce repeated mistakes.

---

## 4. Recommendations (Prioritized)

### Recommendation 1: Structured Todo Tracking Tool ⭐⭐⭐

**What:** Add a `todo_write` tool that maintains a structured, machine-readable task list within each task's context. States: `pending`, `in_progress`, `completed`.

**Why Tofu needs it:** Currently, task tracking is informal (text checklist in planner brief). Structured tracking enables:
- Machine-readable progress (for UI display)
- Automated "you're not done" enforcement
- Verification nudges (like Claude Code's 3+ item check)

**Where to implement:**
- Define tool: `lib/tools/todo.py` (new file)
- Schema: `{todos: [{id, content, status, activeForm}]}`
- Storage: on `task` dict as `task['_todos']`
- Executor handler: `lib/tasks_pkg/executor.py` (add todo tool handler)
- UI: Render todo progress in the frontend's endpoint iteration panel

**Implementation sketch:**
```python
# lib/tools/todo.py
TODO_WRITE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'todo_write',
        'description': 'Update the task checklist. Track progress...',
        'parameters': {
            'type': 'object',
            'properties': {
                'todos': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'id': {'type': 'string'},
                            'content': {'type': 'string'},
                            'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'completed']},
                        },
                        'required': ['id', 'content', 'status'],
                    },
                },
            },
            'required': ['todos'],
        },
    },
}
```

**Effort:** Medium (M) — tool definition + executor handler + optional UI rendering

---

### Recommendation 2: Todo Continuation Enforcer Hook ⭐⭐⭐

**What:** A post-tool hook (or pre-completion check) that detects when the model is about to finish its turn but has incomplete todos. Injects a system reminder: "You have incomplete tasks. Complete ALL before responding."

**Why Tofu needs it:** The most common failure mode in endpoint mode is the worker claiming "done" with uncompleted checklist items. The Critic catches this, but at the cost of a full review cycle. A pre-emptive injection is cheaper.

**Where to implement:**
- Hook: `lib/tasks_pkg/tool_hooks.py` — new pre-hook or integrate into the orchestrator's tool loop
- Alternative: In `lib/tasks_pkg/orchestrator.py`, after `analyse_stream_result` detects `finish_reason='stop'`, check `task['_todos']` for incomplete items. If found, inject a user message and continue the loop instead of breaking.

**Implementation sketch:**
```python
# In orchestrator.py, after the model says "stop" but before breaking the loop:
if finish_reason == 'stop' and not tool_calls:
    incomplete_todos = [t for t in task.get('_todos', []) if t['status'] != 'completed']
    if incomplete_todos:
        todo_list = '\n'.join(f"- {'[x]' if t['status']=='completed' else '[ ]'} {t['content']}" for t in task.get('_todos', []))
        messages.append({
            'role': 'user',
            'content': f'[SYSTEM: TODO CONTINUATION REQUIRED]\n'
                       f'You have {len(incomplete_todos)} incomplete task(s):\n{todo_list}\n'
                       f'Complete ALL tasks before providing your final answer.',
        })
        continue  # Re-enter the tool loop
```

**Effort:** Small (S) — integrates with existing orchestrator loop and hook system

**Dependency:** Requires Recommendation 1 (structured todos) to have data to check.

---

### Recommendation 3: Adversarial Critic Prompt Enhancement ⭐⭐

**What:** Enhance the Critic system prompt with adversarial verification language inspired by Claude Code's verification agent.

**Why Tofu needs it:** The current Critic prompt says "Use tools to actually verify — don't just take the worker's word for it" but lacks the strong anti-rationalization prompts and evidence format requirements that make Claude Code's verification agent effective.

**Where to implement:**
- `lib/tasks_pkg/endpoint_prompts.py` — update `CRITIC_SYSTEM_PROMPT`

**Key additions from Claude Code's verification agent:**
1. "Your job is not to confirm the implementation works — it's to try to break it."
2. Anti-rationalization: "Reading code is not verification. Run it."
3. Evidence format: each check must have Command run + Output observed + Result
4. Adversarial probes: boundary values, concurrency, idempotency
5. Before issuing STOP: "check you haven't missed why it's actually fine" (avoid false FAILs)

**Implementation sketch — additions to CRITIC_SYSTEM_PROMPT:**
```python
# Add after "## Decision guidelines":

## Verification Discipline

- **Run, don't read.** If a checklist item can be verified by running a command
  (test, build, grep, curl), RUN IT. Reading the worker's output and narrating
  what you *would* test is not verification.
- **Evidence required.** For each checklist item you mark ✅, include the command
  you ran and the relevant output. A ✅ without evidence is not a pass.
- **Try to break it.** After confirming the happy path, try at least one
  adversarial probe: boundary values, missing files, empty inputs, or
  duplicate operations.
- **Recognize rationalization.** If you catch yourself writing "the code looks
  correct" or "this should work," STOP — that's a sign you haven't actually
  verified. Run a command instead.
```

**Effort:** Small (S) — prompt-only change, no code logic changes

**Note:** This changes a system prompt, which is a hyperparameter-adjacent change. Per §10 of CLAUDE.md, the user should review and approve the specific wording.

---

### Recommendation 4: 6-Section Delegation Template for Swarm ⭐⭐

**What:** Define a structured delegation prompt template that the swarm master uses when spawning sub-agents, replacing free-form `objective` strings.

**Why Tofu needs it:** OMC's 6-section structure (Task, Expected Outcome, Required Tools, Must Do, Must Not Do, Context) dramatically reduces ambiguity. Currently, Tofu's `SubTaskSpec.objective` is a free-form string that often lacks scope boundaries and success criteria.

**Where to implement:**
- `lib/swarm/protocol.py` — extend `SubTaskSpec` with optional structured fields
- `lib/swarm/agent.py` — build user message from structured template
- `lib/swarm/planner.py` — instruct the planning LLM to produce structured specs

**Implementation sketch:**
```python
# In SubTaskSpec (lib/swarm/protocol.py):
@dataclass
class SubTaskSpec:
    role: str
    objective: str
    context: str = ''
    expected_outcome: str = ''   # NEW: What "done" looks like
    must_do: list[str] = None    # NEW: Non-negotiable requirements
    must_not_do: list[str] = None  # NEW: Explicit boundaries
    depends_on: list[str] = None
    ...

# In SubAgent._build_initial_messages (lib/swarm/agent.py):
def _build_user_task_message(self) -> str:
    parts = [f'## Task\n{self.spec.objective}']
    if self.spec.expected_outcome:
        parts.append(f'## Expected Outcome\n{self.spec.expected_outcome}')
    if self.spec.must_do:
        parts.append(f'## Must Do\n' + '\n'.join(f'- {m}' for m in self.spec.must_do))
    if self.spec.must_not_do:
        parts.append(f'## Must Not Do\n' + '\n'.join(f'- {m}' for m in self.spec.must_not_do))
    if self.spec.context:
        parts.append(f'## Context\n{self.spec.context}')
    return '\n\n'.join(parts)
```

**Effort:** Medium (M) — touches protocol, agent, and planner

---

### Recommendation 5: Orchestrator Write-Block Hook for Endpoint Planner ⭐⭐

**What:** When the Planner phase is active, register a pre-tool hook that blocks file-writing tools (write_file, apply_diff, run_command with mutation). The Planner should explore, not execute.

**Why Tofu needs it:** The Planner prompt says "plan, don't execute" but currently has full tool access. The planner CAN and sometimes DOES make file modifications, violating the separation of concerns. OMC enforces this via hooks; Claude Code enforces it via disallowedTools.

**Where to implement:**
- `lib/tasks_pkg/tool_hooks.py` — new hook `_planner_write_block_hook`
- `lib/tasks_pkg/endpoint_review.py` — set a flag on the task during planner phase

**Implementation sketch:**
```python
# In tool_hooks.py:
def _planner_write_block_hook(tool_name: str, args: dict, task: dict) -> HookResult | None:
    """Block file-writing tools during the planner phase."""
    if task.get('_endpoint_phase') != 'planning':
        return None
    
    WRITE_TOOLS = {'write_file', 'apply_diff'}
    if tool_name in WRITE_TOOLS:
        return HookResult(
            action='block',
            message=f'You are in PLANNING phase. {tool_name} is not allowed. '
                    f'Explore and plan — do not modify files. Delegate execution '
                    f'to the worker phase.',
        )
    return None

register_pre_hook(_planner_write_block_hook)
```

**Effort:** Small (S) — single hook function, leverages existing infrastructure

---

### Recommendation 6: Swarm Wisdom Passing ⭐

**What:** After each sub-agent completes in a swarm, extract a short "learnings" summary and inject it into the context of subsequent agents (not just dependency data, but gotchas and conventions discovered).

**Why Tofu needs it:** Currently `_inject_dependency_context` passes result data but not meta-learnings. If agent 1 discovers "this project uses tabs not spaces" or "the test framework requires --forceExit," agent 2 doesn't benefit.

**Where to implement:**
- `lib/swarm/agent.py` — at completion, extract learnings from the agent's final answer
- `lib/swarm/planner.py` — `_inject_dependency_context()` includes learnings alongside results

**Effort:** Medium (M) — requires an LLM call per completed agent for extraction

**Priority:** Low — session memory already handles this for sequential conversations, and the master review provides centralized learning. Most beneficial for large swarms with 5+ agents.

---

### Summary Table

| # | Recommendation | Priority | Effort | Dependencies |
|---|---|---|---|---|
| 1 | Structured Todo Tracking Tool | ✅ SHIPPED (2026-07-06) | M | None |
| 2 | Todo Continuation Enforcer Hook | ✅ SHIPPED (2026-07-06) | S | Rec 1 |
| 3 | Adversarial Critic Prompt | 🟡 Medium | S | None |
| 4 | 6-Section Delegation Template | 🟡 Medium | M | None |
| 5 | Planner Write-Block Hook | 🟡 Medium | S | None |
| 6 | Swarm Wisdom Passing | 🟢 Low | M | None |

**Recommended implementation order:** 1 → 2 → 3 → 5 → 4 → 6

Recommendations 3 and 5 are independent quick wins that can be done in parallel with 1+2.

---

## 5. Already Well-Covered by Tofu

These areas are where Tofu is at parity with or ahead of OMC/Claude Code:

### 5.1 Crash-Persistent Task State ✅
- Tofu: `_sync_endpoint_turns_to_conversation()` writes all endpoint turns to PostgreSQL after every phase. Survives server crashes, SSE timeouts, and page reloads.
- OMC: `boulder.json` file — less robust (single point of failure, no atomicity).
- **Verdict:** Tofu is ahead.

### 5.2 Session Memory / Cross-Session Learning ✅
- Tofu: `session_memory.py` (background extraction after tool-heavy turns, stored in DB) + skills system (persistent across sessions, BM25 relevance filtering).
- OMC: Wisdom accumulation within a single plan; no cross-session persistence.
- Claude Code: No built-in cross-session learning.
- **Verdict:** Tofu is ahead.

### 5.3 DAG Scheduling with Streaming ✅
- Tofu: `StreamingScheduler` with no wave barriers — agents start as soon as deps complete. Rate limiter with backoff. Kahn's algorithm topological sort.
- OMC: Atlas delegates sequentially or in parallel, but no formal DAG scheduler.
- Claude Code: Fork-based parallelism, no DAG concept.
- **Verdict:** Tofu is ahead.

### 5.4 Reactive Master Review ✅
- Tofu: Master review after each batch with incremental prompts (compressed history + full new results). Fast-path skip for clean batches. Background review thread.
- OMC: Atlas reviews after each task delegation, but no incremental review optimization.
- **Verdict:** Tofu is ahead.

### 5.5 Multi-Phase Endpoint Loop ✅
- Tofu: Planner → Worker → Critic loop with stuck detection (Jaccard similarity), max iterations safety valve, DB persistence per phase.
- OMC: Atlas delegates → verifies → marks done (similar but less structured).
- Claude Code: No built-in multi-phase loop (user drives).
- **Verdict:** Tofu is ahead.

### 5.6 Tool Scoping per Role ✅
- Tofu: `scope_tools_for_role()` in `lib/swarm/registry.py` — each role gets a filtered tool list.
- Claude Code: `tools` / `disallowedTools` per agent definition.
- **Verdict:** Parity.

### 5.7 Suspicious Completion Detection ✅
- Tofu: `_check_suspicious_completion()` detects empty content, short content after tool calls, and fast completion with no content. Tool use itself has no numeric round cap.
- OMC/CC: No equivalent (rely on todo enforcement instead).
- **Verdict:** Tofu is ahead (but different approach).

---

## 6. Not Applicable to Tofu

These features are terminal-specific or architectural mismatches:

| Feature | Why Not Applicable |
|---|---|
| **Hashline (hash-anchored edits)** | Terminal-only; Tofu uses `apply_diff` with exact string matching, which works in web context |
| **LSP tools (lsp_diagnostics, lsp_rename)** | Requires IDE integration; Tofu operates on remote files via project mode — could add `run_command('npx tsc --noEmit')` as a lighter alternative |
| **Tmux/iTerm backend for swarm** | Terminal multiplexer for visual agent monitoring; Tofu has web-based swarm dashboard |
| **Fork subagent (shared prompt cache)** | Requires API-level cache sharing across requests; Tofu's multi-provider dispatch doesn't guarantee this |
| **AGENTS.md hierarchy injection** | Directory-level context files auto-injected when reading files; Tofu uses CLAUDE.md (project-level only) — extending to per-directory context is possible but different architecture |
| **Boulder persistence file** | File-based state; Tofu uses PostgreSQL (strictly better for web server) |
| **Interactive bash (TMUX)** | Terminal-specific visual QA; Tofu can use browser tool or run_command instead |
| **Comment checker hook** | Prevents excessive AI-generated comments; less critical in web-based assistant |

---

## Appendix A: OMC Atlas 6-Section Delegation Format

For reference, this is the template Atlas must follow for every delegation:

```
## Task
[What the sub-agent must accomplish]

## Expected Outcome  
[What "done" looks like — measurable, concrete]

## Required Tools
[Which tools the sub-agent should use]

## Must Do
- [Non-negotiable requirement 1]
- [Non-negotiable requirement 2]

## Must Not Do  
- [Explicit boundary 1]
- [Explicit boundary 2]

## Context
[Relevant background, dependency results, conventions discovered so far]
```

## Appendix B: Claude Code Verification Agent Anti-Rationalization Prompts

Key phrases that could be adapted for Tofu's Critic:

1. "Your job is not to confirm the implementation works — it's to try to break it."
2. "Reading code is not verification. Run it."
3. "If you catch yourself writing an explanation instead of a command, stop. Run the command."
4. "The implementer is an LLM. Verify independently."
5. "A check without a Command run block is not a PASS — it's a skip."
6. Recognize rationalizations: "The code looks correct" → reading is not verification; "This is probably fine" → probably is not verified; "I don't have a browser" → did you check for browser tools?

## Appendix C: Claude Code TodoWriteTool Verification Nudge

When 3+ todos are marked completed and none of them was a "verification" step:

```
NOTE: You just closed out 3+ tasks and none of them was a verification step.
Before writing your final summary, spawn the verification agent.
You cannot self-assign PARTIAL by listing caveats in your summary —
only the verifier issues a verdict.
```

This is a structural nudge, not a prompt instruction — it fires from the tool result, making it hard for the model to ignore.
