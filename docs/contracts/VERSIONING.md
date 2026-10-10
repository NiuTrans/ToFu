# Versioning — independent namespaces

Tofu has **no single "version"**; it has five orthogonal version axes that
happen to share the `vN` spelling. Confusing them is what produced the "V2?
V3? is there a V4?" chaos. This index names each axis, its current value, its
authoritative file, and who is allowed to bump it. For the API-major migration
itself see `docs/contracts/API_V4_MIGRATION.md`.

The axes are independent. Bumping one says nothing about the others.

## The index

| Axis | Current | Authoritative source | Bump policy |
|---|---|---|---|
| **A. HTTP API major** (`/api/vN`) | `4` serving (cutover); `1`/`3` → 426 | `contracts/api_v4.yaml` (v4), `lib/api_v4.py` | Cutover complete (stage `cutover`, `activeByDefault: true`). `v2`/`v3` are **permanently skipped** (burned by axes B and C) so this axis never collides with them. Explicit `TOFU_API_MAJOR=1` selects a legacy assembly; the default regression ladder uses production v4. |
| **B. Storage wire** (`storage_v2`) | `2` | `contracts/storage_v2.json`, `lib/storage/v2_client.py` | Bump when the durable storage/on-the-wire protocol changes. Independent of the HTTP API. |
| **C. Conversation sync** (`conversation_sync_v3`) | `3` | `contracts/conversation_sync_v3.yaml`, `routes/conversation_sync_v3.py` | Bump on a conversation-sync schema change. |
| **D. Per-domain contracts** | pinned per file | `contracts/*_vN.{yaml,json}` | Bump a single domain's schema without touching any API major. Many coexist (see below). |
| **E. Plugin web** (`plugins/*/web/v1`) | `1` per plugin | each plugin's `web/vN/` package | Plugin-owned; not part of the core API major. |

## Why the numbers look "chaotic"

* `/api/v2` does **not** exist and never will for the HTTP API — axis B
  already uses `2`, so axis A skips it.
* `/api/v3` does **not** exist as a product surface — axis C uses `3` for
  conversation sync. Again skipped on axis A.
* `/api/v4` is the **next** HTTP API major precisely because `2` and `3` were
  burned by the storage-wire and conversation-sync axes.

So the sequence `1 → (skip 2, 3) → 4` on the HTTP axis is deliberate hygiene,
not missing work.

## Per-domain contracts (axis D)

These are schema files, not HTTP majors, and each pins its own version:

`api_v1_model_routing`, `browser_site_observation_v1`, `compaction_handoff_v1`,
`control_rpc_v1`, `conversation_lifecycle_v1`, `conversation_search_v2`,
`experiments_v1`, `frontend_conversation_architecture_v1`, `identity_v1`,
`log_incident_v1`, `model_routing_v2`, `paper_build_v1`, `project_brain_v1`,
`project_file_receipts_v1`, `provider_offering_recipe_v1`,
`research_program_v1`, `research_progress_v1`, `research_record_v1`,
`storage_operations_v1`, `tofudb_ir_v1`, `tofusql_catalog_v1`,
`tool_result_query_v1`.

A `_v2` here (e.g. `conversation_search_v2`, `model_routing_v2`) means *that
domain's* schema is on its second revision — it does **not** mean there is a
`/api/v2` HTTP surface.

## Rules

1. **Never invent a new top-level `/api/vN`** without a migration runbook and
   contracts-check gate; follow the v4 model.
2. **Bump the axis that actually changed.** A storage-protocol change bumps B,
   not the HTTP major.
3. **Retire a namespace by marking it in its authoritative contract**, not by
   leaving a stale file beside the new one.
