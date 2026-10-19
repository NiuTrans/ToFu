# Historical LLM canvas links

## Problem

Old tool-round snapshots predate `_mcpLinks`; rebuilding only future backend
metadata cannot repair their browser presentation. A successful fork may return
an ID that never appears in the historical title.

## Decision

`llm-canvas-links.ts::recoverLlmCanvasLinks(round)` returns readonly
`{label, href, role}` records, with roles `input`, `source`, `resolved`, and `new`.
It performs no HTML rendering, network lookup, storage write, or name caching.
The authority is `contracts/llm_canvas_links_v1.json`, imported directly by
TypeScript (`resolveJsonModule` is enabled) and loaded by Python's MCP display.
No generated constants or generator are necessary.

The emitted path route is verified in registered llm-mcp
`src/llm_mcp/experiment_results.py::canvas_metadata`. No unverified alternate
route is reconstructed or accepted; backend emission semantics remain unchanged. All recovered hrefs target the policy's HTTPS canvas route, never a
run/eval/task-detail URL.

Nonempty `_mcpLinks` remains primary, including metadata with rejected unsafe
hrefs: fallback must not bypass an available authority. Empty/missing metadata
permits exact numeric inputs or verified canonical input URLs. Resolved names
and new fork IDs require explicit success in bounded, complete structured
`toolContent` (or `result` when toolContent is absent). Error text, title text,
nested config IDs and unsafe/inconsistent result URLs cannot resolve identities.
Failed/cancelled rounds and dry runs never contribute output IDs. Inputs remain
linkable on failure because they identify the requested canvas, not success.

A deferred snapshot preview is eligible only when its declared `toolContent`
character count proves the entire bounded original string is present. Truncated
prefixes and private result references never resolve output identities.

Historical sparse structured-result wrappers are admitted only by the shared
policy's exact `summary`/`items` shape: the owning compaction
`_candidate_items` projection uses that summary for one structured value, and
`result_envelope._model_projection` distinguishes success from error/partial
projections. The sole item must itself carry explicit success; wrapper text is
not success evidence. Only one wrapper level is decoded. Extra outer fields,
multiple items, nested/serialized items, contradictory failure/cancellation,
dry-run or truncation evidence, and inconsistent URLs remain ineligible in
both TypeScript recovery and Python display. Direct success envelopes still
work. This repairs historical shape loss without weakening result authority.

## Caller integration

`mcp-resource-links.ts::renderMcpResourceLinks` implements this recovery for both
retained and typed headers, appending escaped role-labelled chips and preserving
existing anchors. Recovery is idempotent and does not require a backend restart.

Keep the existing metadata renderer first. For fallback chips:

```ts
import { recoverLlmCanvasLinks } from './llm-canvas-links';
const chips = recoverLlmCanvasLinks(round);
// For each chip, create an anchor with textContent = chip.label,
// href = chip.href, target = '_blank', rel = 'noopener noreferrer'.
// For HTML-string owners, escape BOTH label and href with escapeHtmlText.
// Append chips rather than requiring label substrings in the old title.
```

Do not convert the result to a label-keyed dictionary: identical names and
source/new roles must stay distinct. Labels are raw data and may contain HTML;
only the caller's established escaping/DOM owner may render them.

## Alternatives rejected

Guessing IDs from names or error text can link the wrong canvas. Scraping old
titles loses successful fork outputs. Restarting backend services only fixes
new calls and is not required for browser-only recovery. Durable backfill would
needlessly mutate conversation authority.

Recursive or arbitrary `items` harvesting was rejected: it cannot establish
which item belongs to the successful call or whether the outer result failed.

## Verification

- `npx vitest run frontend/src/conversation/presentation/llm-canvas-links.test.ts`
- `npm run typecheck:modules`
- application Python: `-m pytest -q tests/mcp_protocol/test_llm_canvas_link_policy.py tests/mcp_protocol/test_llm_canvas_links.py tests/mcp_protocol/test_mcp_tool_links.py`
