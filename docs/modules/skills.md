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

Settings library discovery, source identity and explicit filesystem destinations
are specified in [Skill library and filesystem scope](../SKILL_LIBRARY.md).

## Context lifecycle

At task composition the runtime builds a deterministic, token-bounded index of
eligible installed skills — identifiers and compact descriptions, never full
instructions — frozen for the task so prompt-cache prefixes stay stable.

Installed discovery takes a fresh root snapshot on every call, even when parsed
instructions are cached. Problem: regular-file admission and fingerprinting
separately probed each `SKILL.md` with `lstat`, `isfile` and an mtime `stat`.
Decision: one no-follow stat result supplies both regular-file admission and
mtime for that snapshot. Missing, unreadable, linked and special instruction
files remain rejected; precedence and shadowed-source projection stay unchanged.
The cache fingerprint is still refreshed after migrations, and cached results
still re-derive environment/vault eligibility without rereading instructions.
No snapshot is reused across calls and no cache, knob or admission limit is added.
Alternatives rejected: directory-mtime-only caching misses in-place instruction
edits; skipping file-type validation weakens the symlink boundary. The executable
specification `tests/skills/test_skill_discovery_snapshot.py` measures 384 to 128
instruction-file metadata probes for an unchanged 128-package listing (three to
one per package), with exact result equality. This is a filesystem-operation
reduction, not an end-to-end latency claim.

Project discovery reads `.tofu/skills`, `.agents/skills`, `.codex/skills`, then
`bundles/meituan/skills`, in the order declared by `lib/skills/paths.py`.
Owner-global packages live outside any project, under the server data root's
`skills/users/<owner>` store. Internal-only source packages under `bundles/`
reach internal/personal source exports, never the opensource mirror. Their
content manifest records reviewed file hashes; it is not an upstream signature,
a credential store, or live-platform acceptance. Installed packages may keep
owner-specific overrides. Skills are owner-global (owner decision
2026-10-31): the owner-global store precedes every project root, so the
global package wins its id in every conversation and a same-id project copy is
reported as shadowed; among project roots the earlier root wins.
`python -m lib.skills.promote --owner <id> --scan <project> [--apply]` moves
project packages into the global store through the validated installer
(`missing` installs, `identical` skips, `differs` needs `--replace`; the
project source is never removed). Problem: a skill kept in one project's
`.agents/skills` was invisible to every other conversation, and where a global
copy existed the project copy replaced it inside that project, so edits landed
in a copy other conversations never saw. Decision: one runtime authority per id
(the owner-global package); installs and settings moves always target it.
Alternatives rejected: scanning every recent project for every conversation
(loads unrelated workspaces and makes runtime depend on navigation history);
keeping project-wins precedence after promotion (the stale project copy would
keep masking the promoted package in that project). Too-many-skills selection
is a later design concern, not a reason for project-only skills.
`tests/skills/test_skill_global_policy.py` pins precedence and promotion.

Promotion keeps provenance and never reverts global edits. A package Tofu
installed into a project carries its own origin markers; the installer
excludes them only when the promoter asks (uploads still cannot carry them),
and while their content seal matches the source bytes the recorded catalog id,
revision, registry and URL are passed into the global install. An edited
source carries none, and every promoted install is pinned to the inspected
digest. `global-edited` means the global package was installed from content
identical to this source and changed afterwards: the source is the older copy,
so promotion refuses it even with `--replace`. Problem: promotion rejected
every installer-made project package as carrying reserved metadata, copies
made around that lost the upstream URL and revision, and `--replace` could
revert global edits from a stale project copy. Decision: provenance travels as
verified installer arguments and the install seal decides direction.
Alternatives rejected: copying the markers (a stale record would describe
bytes it no longer seals); letting `--replace` override `global-edited` (a bulk
`--scan --apply --replace` cannot review each row). Packages without a seal,
such as hand copies, stay `differs`, whose direction the owner must check.
`test_skill_global_policy.py` and `test_skill_installation_contract.py` pin
this.

Connector companion skills (2026-11-01). Problem: an MCP connector supplies
tools but no workflow judgment; no default skill root reaches every owner; and
the vendors' own skill packages (Tencent Docs, Kingsoft Docs) wrap the same MCP
endpoint behind a CLI that hides read/write intent and stores tokens outside
the owner's MCP configuration. Decision: a curated MCP catalog entry may name an
in-tree package under `lib/mcp/companion_skills/` (`companion_skill`), and the
catalog install route calls `lib/skills/companion.ensure_companion_skill` for
the requesting owner. It reuses `promote.inspect_package` and the digest-pinned
installer with `source_registry='tofu-companion'`: a missing guide is installed,
an identical one is left alone, an unedited copy from an earlier companion
revision is updated, and any other same-id package (the owner's own or edited
copy) is kept. The outcome is returned as `companion_skill.action`; a failure
never fails the connector install, and uninstalling the connector leaves the
guide in place as owner data. Alternatives rejected: a built-in skill root
visible to every owner (it needs read-only scope semantics, per-owner disable
state and settings/export changes), installing the vendors' CLI-wrapped skills,
and a curated skills-catalog entry (it requires a pinned GitHub archive of this
repository). `tests/skills/test_skill_companion.py` pins install, update, keep,
owner isolation and failure isolation.

Internal exports carry installed packages. An internal `export.py` run restores
both install stores — `.tofu/skills` and `data/skills` — after its bulk copy,
which strips them along with every other `*` `.tofu` artifact and the root
`data/` tree. The restore merges one package directory at a time: a package
directory holding `SKILL.md` is replaced whole, other directories are descended
into, and destination-only packages survive. Before replacing any package,
the restore validates all carried roots and refuses source symlinks (including
broken links) and destination symlink components. Copy-time checks repeat this
boundary: links are neither preserved as escaping packages nor dereferenced
into unrelated host files. This follows ZIP installation's link prohibition;
silently skipping linked resources would ship incomplete packages.
`tests/export/test_installed_skill_copy_safety.py` checks refusal without
replacing existing packages or writing outside the destination.

Two content rules decide whether a carried package actually WORKS on the
destination (owner decision 2026-11-01): vendored `node_modules` IS carried,
because packages such as dx-pura-api ship their engine there and dropping it
produced a skill whose CLI cannot start; and a package's own `.skillignore`
plus `.cache/`/`.tofu/` is NOT carried, because those hold the operator's live
session tickets and identity binding, which would both leak a session and
misidentify the colleague. `tests/export/test_internal_carried_state.py` pins
both, alongside the carried vault, MCP and memory rules.
Opensource and personal exports carry no owner-installed skill catalogue;
personal exports can still carry the reviewed internal source bundle above. `PROJECT_SKILL_ROOTS` therefore keeps
`exported_in=()` for the runtime root (it describes the tree, not the restore
pass); `tests/skills/test_skill_roots_contract.py` locks the pass to roots the
scanner actually reads. `export.py --handoff` reaches the same state through
`tofu_state.tar.gz`, which lists both stores in its carried state dirs.
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

Hidden package entries — Problem: the manifest skipped dot-prefixed files and
folders, but `read_skill_resource` served them by exact path, so package-private
state such as `.cache/` session tickets or `.secrets/` credentials could enter
model context, including Delegate Mode, whose root reads packages through
`read_skill_resource` without file tools. Decision: reads apply the listing
rule; any dot-prefixed path component is refused with an explicit
`Invalid skill resource request` before the filesystem is consulted, so present
and absent hidden entries are indistinguishable. Alternatives rejected: listing
hidden entries (publishes private state on every load); redacting secret-shaped
content (opaque tickets evade patterns and other private state still leaks);
denying only known names such as `.cache`/`.secrets` (every other dot entry
stays readable). `tests/skills/test_skill_installation_contract.py` pins the
refusal.

A package may declare tool dependencies via top-level `requires_tools` (or
`metadata.openclaw.requires.tools`): built-in names or MCP `mcp__server__tool`
ids. `load_skill` marks each available/unavailable/unknown against the live
registry and any running owner MCP bridge (never starting one); advisory only,
unlike the `requires_bins`/`requires_env` gates — a missing tool never blocks.

Every `run_command` child receives the configured env bindings of each enabled
skill (`lib/skills/env.py` `exec_env_overlay`). The overlay resolves all of them
from one vault store read (`credentials_vault.get_entries_with_prefixes`).
Problem: it used to re-read and re-parse the vault file once per enabled skill
and bound entry, under the vault lock, before each command — about 4.7 s on the
FUSE host (py-spy, 2026-11-01), serialized across concurrent commands.
Rejected: caching decrypted values (it would put secrets in a long-lived cache
and need invalidation on every vault write).

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
revision when supplied by discovery, and scope. It is a write governed
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

## Internal platform skills and MCP boundary

The Meituan workflow set keeps separate instruction owners for Hope/MLP jobs,
LongCat canvas evaluation, 学城 documents, EnvSandbox read-only forensics,
CodeLab SSH, image/container provisioning, and FRIDAY/AIGC LLM-gateway usage
for synthesis/distillation traffic. Team operating evidence (dated, cited to
its 学城 source) lives in each owner's `references/rsi-*-2026q4.md`, never as an
admission gate. Knowledge belongs in those
skills; frequent executable capabilities belong in their native MCP catalogues.
CodeLab container-management guidance is consolidated into image/provisioning
rather than retaining a competing UI-only container skill. Raw marketplace
archives are evidence, not executable installs: local safety/redaction overrides
survive updates, and embedded upstream credentials require issuer rotation.

`packages/tofu-mtsso-auth/README.md` owns internal authentication. Three isolated
MCP launches share that adapter: registered official Agent SSO remains first and
cannot downgrade after denial; without a registration, the same-user installed
DataFE session may be reused through the official exchange/introspection APIs.
Business calls never start shared interactive login; only explicit login tools
may do so. Local status is not identity verification. Tickets never enter model
results or copied MCP stores; cache partitions carry the launching Tofu owner,
source digest, environment and exact audience set. Connecting an MCP or loading
a skill does not authorize job submission, container creation, document writes,
SSH execution or publication. An unsupported upstream API stays unsupported;
platform acceptance, runtime completion and experiment verdict are distinct.

The portable internal bundle retains reviewed guides, selected transport/read
helpers and their references without activating any helper. It excludes marketplace archives, signatures,
installed dependencies, sessions, cached credentials and executable legacy
build/auth workflows. EnvSandbox read auth remains a caller-injection contract,
not an activated live transport. CodeLab SSH requires trusted credential stdin
and host identity; bundling the helper neither installs Paramiko nor grants
remote access. `tests/skills/test_internal_platform_bundle.py` checks manifest
integrity, resource availability and the private export boundary.

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

Explicit programme/run selections use `activation.bind_skill_selection` and the
same `explicit_skill_context` owner as Chat's `$skill` requests. The former pins
installed guide IDs/hashes, never installs or changes enabled state. Task config
`skillSelection` carries those references into the registered `selected_skills`
block; provider failure, version drift and whole-group budget overflow cannot
silently omit a required guide. `skillSelectionMode=review` makes the guides
criteria to inspect, not procedures to execute. `skillSelectionProjectPath` grants
only package discovery/resource reads when ordinary project tools are disabled.
These scoped reads do not grant shell, installation or additional tool rights.
See [EVOLUTION_DESIGN.md](../EVOLUTION_DESIGN.md) for programme acceptance and handoff semantics, and
`tests/skills/test_skill_selection.py` for scope, hash, budget and failure probes.
