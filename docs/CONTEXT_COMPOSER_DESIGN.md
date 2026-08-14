# Context Composer, Skills, and Memory

**Status:** implemented 2026-08-12
**Runtime owner:** `lib/tasks_pkg/context_composer/`

## 1. Three different nouns

| Concept | Author / trust | Purpose | Discovery | Lifetime in model context |
|---|---|---|---|---|
| Skill | User-installed, trusted workflow guidance | How to perform a class of task | `<available_skills>` metadata index, then exact `load_skill(skill_id)` | Current task; cold full results compact to a receipt and may be loaded again |
| Memory | Model-authored experience note | What was learned from past work | Explicit `search_memories`, plus local high-confidence metadata retrieval | Selected evidence for the current task/turn |
| Preference profile | User facts and working preferences | How the user generally wants work done | Profile core plus relevance-gated detail | Conversation/task context while Memory is enabled |

A skill is not “activated state”. Settings enable or disable an installed
package; `load_skill` only discloses its full `SKILL.md` to the current task.
There is no runtime activation registry and no `activate_skill` callable alias.
Old persisted calls remain displayable through a display-only migration shim.

The skill workflow follows the useful part of OpenCode-style progressive
disclosure: keep a small index in ambient context and load the full guide only
when its description matches. Tofu names the runtime operation `load` to avoid
conflating it with persistent Settings state.

## 2. Authority

The logical order is:

1. platform safety and permissions;
2. explicit current user intent;
3. user-supplied/project rules;
4. loaded workflow/role guidance;
5. preferences and ambient state;
6. retrieved evidence.

Loaded skills explicitly state this boundary. A skill can describe a workflow;
it cannot override safety, tool authority, the user's current request, or
project rules.

## 3. One context owner

Every conversational LLM role—ordinary Agent, endpoint Planner/Worker/Critic,
post-compaction reconstruction, and cold debug reconstruction—uses Context
Composer. Providers return typed `ContextBlock` values and do not splice
messages themselves.

Each block declares:

- stable id and source;
- authority;
- physical placement (`system`, `head`, `tail`, or `tool_result`);
- stability (`static`, `conversation`, `turn`, or `round`);
- lifecycle (`conversation`, `task`, or `round`);
- priority, optional token budget, dedupe key, and provenance.

The renderer owns ordering, one outer `<system-reminder>` envelope,
deduplication, truncation, and replacement on re-entry. Round attachments use
the same renderer's append path so the stable prefix is not rewritten.

## 4. Source policy

- Platform prompt, eligible skill index, credential names, memory guidance,
  and parallel-execution guidance are stable system blocks.
- Project rules and core preferences are head blocks.
- Charter and open goals are task context.
- The project board is ambient only when it has claimed work; backlog and done
  rows remain pull-based tools.
- Related conversations are relevance-filtered.
- Peer messaging protocol lives in the corresponding tool descriptions, so it
  consumes no context when the tools are absent.
- Local memory selection searches metadata only, selects at most two
  high-confidence matches, and performs zero auxiliary LLM calls. Bodies remain
  available through explicit memory search.
- Endpoint role prompts are round-scoped workflow blocks, not fabricated user
  messages.

## 5. Compaction and observability

Cold `load_skill` results compact to a receipt containing the skill id and
content hash. The receipt tells the model to call `load_skill` again if it needs
the full workflow after compaction.

Every composition produces `task['_contextManifest']`. Live
`messages_snapshot` events, Request Inspector payloads, and the cold
`debug-messages` reconstruction carry the manifest. The debug panel groups it
by placement and shows deterministic order, source, authority,
injected/suppressed reason, size, hash, and provenance. This is the
authoritative answer to “why did the model receive this context?”
