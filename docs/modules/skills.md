# Skills

This domain owns discoverable instruction packages. A skill can improve model behavior by supplying task-specific guidance and bounded resources; it does not grant execution authority, install dependencies, or bypass tool policy. Discovery separates lexical relevance, registry semantic order, and release verification. Installed status cannot create a lexical match. Delimited tool names match complete words; single-word prefix-only online noise (such as Frida/Friday) is discarded before verification without blocking semantic synonyms. Empty queries with four or more distinct words can recover using the first and middle original-query anchors, stopping at the first nonempty candidate list. Each call performs at most three search requests and twelve release verifications; search errors (including 429) stop recovery — a request-count budget, not a whole-call deadline — and query caches also cover fallback requests. Model results expose source status, explicit-only flags, recovery queries, rejected/unavailable counts, verification errors and retry metadata; search cards distinguish matches, no matches and partial discovery.

## Ownership

| Concern | Owner |
|---|---|
| Compact task index | `lib/skills/injection.py` |
| Installed-skill registry and eligibility | `lib/skills/registry.py` |
| On-demand instruction/resource reads | `lib/skills/load.py` |
| Explicit selection and usage receipts | `lib/skills/activation.py` |
| Repository development guides | `.agents/skills/` |
| Offline curated catalog and merged discovery | `lib/skills/catalog.py`, `discovery.py` |
| Bounded ClawHub search and exact-version resolution | `lib/skills/online_catalog.py` |
| Package validation and atomic activation | `lib/skills/installer.py` |
| Verified catalog download service | `lib/skills/catalog_install.py` |
| Owned provider settings, transport and private complete artifacts | `lib/skills/automation/`, [automation sources](../AUTOMATION_SKILL_SOURCES.md) |
| Model tool schemas and execution | `lib/skills/tools.py` |
| Authenticated HTTP/settings surface | `routes/api_v1/skills.py` |
| Lazy domain entry | `frontend/src/features/skills.ts` |
| Settings UI state, view, commands, rollback, lifecycle | `frontend/src/features/skills/panel.ts` |
| Shared bounded browser package-upload transport | `frontend/src/features/skills/package-installer.ts` |
| Package-upload presentation | `frontend/src/features/skills/package-install-panel.ts`, `frontend/src/features/memory/skill-package-install.ts` |

## Context lifecycle

At task composition the runtime builds a deterministic, token-bounded index of
eligible installed skills — identifiers and compact descriptions, never full
instructions — frozen for the task so prompt-cache prefixes stay stable.

Project discovery reads `.tofu/skills`, `.agents/skills`, and `.codex/skills` in order.
Index, selection and tools extract `path` from records or legacy strings;
invalid entries are ignored and project access off excludes all project roots.
Escaping roots are rejected; host-home paths are not inferred. Packages enter
through ZIP upload. The three repository development guides ship SKILL.md only;
paths they name are workspace files, not `skill://` resources.

A package may opt out of implicit routing with `allow_implicit_invocation:
false` in frontmatter (or Codex sidecar `agents/openai.yaml`
`policy.allow_implicit_invocation`; frontmatter wins): it stays indexed as
`explicit-only` and activates only on an exact user `$skill-id` mention. A
same-id package is won by the highest-precedence root; the index names ids
shadowing masked duplicates. The header carries trigger rules: use a named or
clearly matching guide this turn (explain skips), never carry guides across
turns, announce usage, and read chosen guides fully, never via sub-agents.

Exact `$skill-id` mentions in current user prose deterministically disclose
enabled, eligible, owner-visible instruction pages through `selected_skills`;
fenced code is excluded. A task snapshot is keyed by the current request and
its owner/project/model context. At most three guides are read with a
6,500-token combined target under an 8,000-token block ceiling; pages that
cannot fit stay explicitly deferred with a load handle. A guide is disclosed
whole — the loader never pages SKILL.md; model-context overflow is paged by
the shared tool-result artifact layer, not a skill-specific cursor. Empty
manifests are instruction-only; provenance (id, trigger, status, content
hash) lands in the Inspector.

Implicit use remains model-selected from descriptions. No matching guide is a
normal outcome, not a requirement to search; search is reserved for a material
capability gap or explicit user discovery/installation requests. Generic words
such as design/plan cannot alone route a specific request to an unrelated
package. Tool result metadata distinguishes discovery from instruction
disclosure; the content-free receipt tail retains at most 32 entries. Spawned
sub-agents load their own guides — no spawn-time preloading.

The model can call `search_skills` for local lexical catalog discovery and, by
default, an on-demand ClawHub query (at most two original-query anchor
fallbacks for empty multi-concept searches), `load_skill` for one whole
guide plus a resource manifest, and `read_skill_resource` only for listed
`skill://` packaged references/scripts (not the main guide). A missing file
lists available resources instead of inviting invented URIs; workspace paths
named in a guide stay ordinary files. Results expose `skill://` identifiers, never server paths.
Resource reads expose no output-character budget: an optional `cursor`
selects the UTF-8 suffix, returned whole for shared canonical capture/L0.
The 2 MiB resource admission ceiling, binary rejection, symlink/path/owner
checks and opaque package references remain independent safety boundaries.
Inputs, metadata, instruction files, and the resident index have independent
limits; selection never injects a whole catalog into a request.

A package may declare tool dependencies via top-level `requires_tools` (or
`metadata.openclaw.requires.tools`): built-in names or MCP `mcp__server__tool`
ids. `load_skill` marks each available/unavailable/unknown against the live
registry and any running owner MCP bridge (never starting one); advisory only,
unlike the `requires_bins`/`requires_env` gates — a missing tool never blocks.

Online discovery is not part of task composition. Only the bounded capability
phrase supplied to an explicit search call leaves the server; the tool schema
warns against secrets, source code, or user data. ClawHub names and summaries
are normalized, truncated, and labeled untrusted routing metadata, never
executable instructions; the model only receives verified install coordinates
for exact releases. `online=false` and `TOFU_SKILLS_ONLINE_DISCOVERY=0` are
request- and deployment-level off switches. Route registration and offline
catalog reads keep the shared HTTP stack dormant; online modules load on
explicit network use.

Settings renders the 15-entry offline catalog first; typing two characters
starts a debounced online search — empty browsing never downloads the remote
registry. Results keep ClawHub order and show provider, verification, version, publisher, and canonical registry link.

The three read/discovery schemas remain direct for low-latency routing. The
installation mutator is Tool-Search deferred on larger tool surfaces while
remaining in the task-frozen executable catalog; small surfaces keep it direct.

## Installation trust boundary

`request_skill_install` accepts only an exact catalog id, immutable source
revision when supplied by discovery, and scope. It is a serial write governed
by the task's Auto / Manual mode: Auto authorizes verified catalog
installation, including unattended tasks; Manual requires an attended decision
and fails closed without one. The gateway issues an exact, expiring,
single-use receipt under either authority; direct handler calls cannot bypass
it. Each task permits three installation attempts, including failed downloads.
Success immediately returns the first instruction page and resource manifest
and never executes scripts or installs dependencies. With the 25 MiB package
ceiling one task adds at most 75 MiB. Existing packages remain durable and are
never evicted to make room.

Catalog entries pin an immutable revision and canonical selected-package
SHA-256; the service downloads within a fixed compressed-byte budget. The
installer rejects traversal, symlinks, special/encrypted entries, Zip64 and
multi-disk archives, ambiguous roots, reserved origin metadata, portable path
collisions, and oversize packages. It hashes normalized selected content,
stages beside the destination, then swaps with rollback protection; bundled
setup scripts never execute automatically.

ClawHub identities use the stable, path-safe `clawhub.<publisher>.<slug>`
catalog id while approval separately binds the exact release version.
Installation bypasses discovery caches: it requests a fresh owner-qualified
verification envelope and requires `ok=true`, `decision=pass`, and
`security.status=clean` for that exact version. The complete publisher-file
manifest is checked by path, size, and SHA-256 inside the atomic installer;
hosted registry-only `_meta.json` / generated skill-card files are not
activated. A public GitHub handoff binds an allowlisted codeload URL, exact
repository, 40-hex commit, selected subdir, well-formed declared content hash,
and the same verified file manifest. The file manifest is the byte-level
integrity authority. Neither route executes bundled scripts.

Public reads are fixed to `https://clawhub.ai`; callers cannot supply a host
or URL. JSON, archives, paths, files, directories, expanded bytes, timeouts,
and result counts all have independent limits. Relevance search and compact
verification caches expire after five minutes with at most 64 and 256 entries;
a 30-second, 32-entry failure cache bounds repeated failures. Only successful
verification enters the five-minute cache; failures retain error code and
retry metadata. Concurrent searches share one lazy four-worker verification
generation closed by the last active search, so an explicit search leaves no
resident threads and never briefly doubles the hard concurrency bound.
Reclaimable caches stay registered with host memory pressure. `429` and
`Retry-After` are preserved as bounded retry metadata, never busy-retried.

The settings API is a separate explicit-user surface accepting multipart
package bytes, never an arbitrary server path. Over-budget catalog entries
stay visible with an unavailable reason rather than weakening validation.

Memory and Skills load the same typed package-upload transport only with their
lazy feature chunks. Picker and OS-drop inputs share ZIP validation, FormData
construction, response parsing, and a one-active-upload guard. Each surface
installs one fixed page-lifetime set of four drag listeners; feature adapters
retain scope selection, localized toasts, diagnostics, and refresh policy.

## Ownership and activation

Every durable operation receives an explicit owner id. Global packages live in
an owner-scoped store (the legacy personal store reads only for its declared
compatibility owner); project packages remain under project authority. Enabled
state is owner-specific globally and project-authoritative in projects. All
routes resolve an explicit identity at the auth boundary.

A successful install returns the installed skill id, immutable origin fields,
content digest, and initial instruction page. A failed post-install read
reports that installation succeeded and provides the exact id for retry.
Ordinary discovery observes it on the next task snapshot; installation does
not rewrite the frozen index mid-task.

## Invariants

- Instructions are context, never authority.
- Explicit-only skills never activate on description matches alone; an exact
  user `$skill-id` mention is their only selection channel.
- Resident discovery is local and deterministic; online discovery is explicit,
  read-only, bounded, cached, and absent from the prompt until requested.
- Model-driven installation has no arbitrary URL or filesystem-path argument.
- Catalog installation consumes exact gateway approval and verified origin data; ClawHub approval includes the exact version returned by search.
- Package scripts are retained for inspection and never run automatically.
- Installed content activation is atomic and rollback-safe.
- Browser package upload permits one active request and one fixed listener set per lazy surface.
- Model-visible results never reveal an absolute package path.
- Owner, project, and enabled-state boundaries are explicit at every entry.

## Change routing and tests

| Change | Start here | Verify |
|---|---|---|
| Context/index budget | `injection.py`, context provider | complete XML, token cap, frozen snapshot |
| Discovery/read behavior | `discovery.py`, `load.py` | stable ranking, opaque ids, paging, symlink rejection, empty-manifest notice, missing-resource recovery |
| Online discovery | `online_catalog.py`, search API, feature module | no preload, query/result/cache caps, fail-soft outage, untrusted metadata label |
| Package policy | `installer.py`, `catalog.py` | digest, archive limits, collision, rollback tests |
| Approval/dispatch | ToolSpec, dispatch pipeline, handler | auto-mode gate, unattended rejection, one-use receipt |
| Browser package upload | `features/skills/package-installer.ts`, feature adapters | ZIP rejection, scope, one active upload, fixed drag listeners, visible failure |
| Owner/API/UI | paths, registry, API route, feature module | owner isolation, multipart-only route, unavailable card |

```bash
pytest -q tests/skills/test_skill_search_recovery.py tests/skills/test_skill_channel.py \
  tests/skills/test_skill_activation.py tests/skills/test_skill_project_roots.py tests/skills/test_skill_installation_contract.py \
  tests/skills/test_skill_online_catalog.py tests/skills/test_skills_api_split.py \
  tests/skills/test_skill_env_vault.py tests/tools/test_tool_registry.py \
  tests/tasks_pkg/test_write_approval_gate.py
pytest -q tests/skills/test_skill_package_installer.py \
  tests/frontend/test_frontend_memory_vite.py tests/test_frontend_skills_vite.py
```

Online verification demand — Problem: concurrent catalog searches queued exact-version verification behind four local workers. Decision: one shared demand generation owns each eligible candidate; the legacy worker value is an idle hint. Genuine unstarted worker failure preserves that candidate as unverified/uninstallable with the existing safe catalog-unavailable code, while siblings still verify in input order. Batch counts belong to each executor generation, so closing an old batch cannot retire a new generation. Installation admission, archive/path/digest limits, search evidence and cache lifetimes remain. Alternatives: larger quotas retain queues; unknown verification cannot authorize installation; detached workers lose teardown ownership. Rendezvous, failure and lifecycle tests verify the boundary. Measurement: 32 exact-version verifications across eight concurrent searches completed in 19.81 ms; zero workers remain after shutdown.
