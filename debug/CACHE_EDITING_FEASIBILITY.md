# Cache-Editing Feasibility — Empirical Probe (2026-06-03)

**Question:** Can Tofu use Anthropic's *cache editing* (Claude Code's
`cachedMicrocompact`) — shrinking the cached prefix by sending
`cache_edits`/`cache_reference` deletions at the API layer, so the prompt
cache stays valid *and* gets smaller — on our gateway?

**Answer: No.** Verified live against the real gateway.

---

## How cache editing works (from claude-code source)

`src/services/api/claude.ts:3052` + `src/services/compact/microCompact.ts`:

- A `cache_edits` block is spliced into the last user message:
  `{"type":"cache_edits","edits":[{"type":"delete","cache_reference":"<tool_use_id>"}]}`
- Cached `tool_result` blocks carry `cache_reference: <tool_use_id>` for
  addressable deletion.
- The server deletes that content from the KV cache and reports
  `cache_deleted_input_tokens` in `usage`.
- Gated 1P-only: in the **external** Claude Code build the entire
  implementation is dead-code-eliminated; `CACHE_EDITING_BETA_HEADER` is
  an "ant-only" constant (`claude.ts:1187`). The closest *public* beta is
  `context-management-2025-06-27`.

## Our transport reality

- Default path is **OpenAI Chat Completions** (`/chat/completions`).
- We DO have an Anthropic-native translator (`lib/llm/anthropic_outbound.py`,
  `api_protocol='anthropic'`).
- Gateway Anthropic-native Messages endpoint (discovered by probing):
  **`https://api.openai.com/v1/anthropic/v1/messages`** (HTTP 200,
  returns Bedrock-backed `msg_bdrk_…`, `claude-opus-4-7`).
  - `…/v1/openai/native/v1/messages` → 404 (the configured base is
    OpenAI-format only).
- Claude models are `aws.claude-*` → **AWS Bedrock** behind the gateway.

## Probe results (live, key 0, aws.claude-opus-4.7)

| Call | beta | HTTP | cache_creation | cache_read | **cache_deleted** |
|---|---|---|---|---|---|
| warm (tool_result in prefix) | context-management | 200 | 13436 | – | **– (absent)** |
| edit (`cache_edits` delete) | context-management | 200 | – | – | **– (absent)** |
| warm | none | 200 | 13436 | – | **– (absent)** |
| edit (`cache_edits` delete) | none | 200 | – | – | **– (absent)** |

Controls:
- Identical warm body sent twice → call 2 again reports
  `cache_creation=12035`, **never** `cache_read`. The gateway surfaces
  `cache_creation_input_tokens` but **not** `cache_read_input_tokens` at
  all on this path.

## Verdict

1. The gateway **accepts** a request containing a `cache_edits` block
   (HTTP 200, no error) but **silently ignores it** — `cache_deleted_input_tokens`
   is never returned, with or without the beta header. That is case (b):
   accepted-but-ignored, i.e. **not feasible**.
2. More fundamentally, even **ordinary cache read** does not surface
   (`cache_read_input_tokens` always absent), so the cache-editing
   premise — "ride cheap cache reads of a shrunken prefix" — has no
   measurable hook on this gateway anyway.
3. Cache editing is a **server-side KV capability**. It cannot be faked
   client-side: deleting content from the middle of a cached prefix
   changes the prefix bytes, so without server cooperation the cache is
   invalid from that point regardless.

**Conclusion:** Cache editing is not available to us. Our two real levers
remain: (A) the default **prefix-skip** L1 (don't bust), (B) the
**aggressive** arm (`ignore_cache_prefix`, one bust then smaller). The
elegant info-preserving methods (M1/M2) and the structural/LLM advanced
host are where our wins live.

## Reproduce

```
python3 debug/probe_cache_editing.py            # dry-run (shows payload)
python3 debug/probe_cache_editing.py --send      # live (few cents)
```
Update the URL in the probe to `…/v1/anthropic/v1/messages` (the working
native path discovered above) before `--send`.
```
