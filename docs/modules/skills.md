# Skills

This domain owns discoverable instruction packages. A skill can improve model
behavior by supplying task-specific guidance and bounded resources; it does not
grant execution authority, install dependencies, or bypass tool policy.

## Ownership

| Concern | Owner |
|---|---|
| Compact task index | `lib/skills/injection.py` |
| Installed-skill registry and eligibility | `lib/skills/registry.py` |
| On-demand instruction/resource reads | `lib/skills/load.py` |
| Offline curated catalog and merged discovery | `lib/skills/catalog.py`, `discovery.py` |
| Bounded ClawHub search and exact-version resolution | `lib/skills/online_catalog.py` |
| Package validation and atomic activation | `lib/skills/installer.py` |
| Verified catalog download service | `lib/skills/catalog_install.py` |
| Model tool schemas and execution | `lib/skills/tools.py` |
| Authenticated HTTP/settings surface | `routes/api_v1/skills.py` |
| Lazy domain entry | `frontend/src/features/skills.ts` |
| Settings UI state, view, commands, rollback, lifecycle | `frontend/src/features/skills/panel.ts` |
| Shared bounded browser package-upload transport | `frontend/src/features/skills/package-installer.ts` |
| Skills package-upload presentation | `frontend/src/features/skills/package-install-panel.ts` |
| Memory package-upload presentation | `frontend/src/features/memory/skill-package-install.ts` |

## Context lifecycle

At task composition, the runtime creates a deterministic, token-bounded index
of eligible installed skills. The index contains identifiers and compact
descriptions, not full instructions. It is frozen for the task so prompt-cache
prefixes do not change during a turn.

The model can call `search_skills` for local lexical catalog discovery and,
by default, one on-demand ClawHub query,
`load_skill` for one full instruction file, and `read_skill_resource` for a
paged text resource. Results expose `skill://` identifiers rather than server
filesystem paths. Inputs, output pages, metadata, instruction files, and the
resident index each have independent limits. Skill selection never injects an
entire catalog or package tree into every model request.

Online discovery is not part of task composition. Only the bounded capability
phrase supplied to an explicit search call leaves the server; the tool schema
warns against including secrets, source code, or user data. ClawHub names and
summaries are normalized, truncated, and labeled as untrusted routing metadata,
never executable instructions. The model only receives verified install
coordinates for exact releases. `online=false` and
`TOFU_SKILLS_ONLINE_DISCOVERY=0` provide request-level and deployment-level
off switches. Route registration and offline catalog reads keep the shared HTTP
stack dormant; online catalog/install modules load on explicit network use.

Settings initially renders the 15-entry offline catalog. Typing at least two
characters starts a debounced online search; empty catalog browsing never
downloads the remote registry. UI results retain ClawHub relevance order and
show provider, verification, version, publisher, and canonical registry link.

The three read/discovery schemas remain direct for low-latency routing. The
installation mutator is Tool-Search deferred on larger tool surfaces while
remaining in the task-frozen executable catalog; small surfaces retain it
directly when a discovery round would cost more than the schema.

## Installation trust boundary

`request_skill_install` accepts only an exact catalog id, immutable source
revision when supplied by discovery, and scope. It
is a serial write that always needs attended human confirmation, even when
automatic writes are otherwise enabled; unattended execution fails closed.
The approval receipt binds tool name, call id, and canonical arguments, expires
quickly, and is consumed once by the handler.

Catalog entries pin an immutable revision and canonical selected-package
SHA-256. The service downloads within a fixed compressed-byte budget. The
installer rejects traversal, symlinks, special/encrypted entries, Zip64 and
multi-disk archives, ambiguous roots, reserved origin metadata, portable path
collisions, and packages beyond file/directory/unpacked-byte limits. It hashes
normalized selected content, stages beside the destination, then swaps with
rollback protection. Bundled setup scripts are never executed automatically.

ClawHub identities use the stable, path-safe
`clawhub.<publisher>.<slug>` catalog id while approval separately binds the
exact release version. Installation bypasses discovery caches: it requests a
fresh owner-qualified verification envelope and requires `ok=true`,
`decision=pass`, and `security.status=clean` for that exact version. The
complete publisher-file manifest is checked by path, size, and SHA-256 inside
the atomic installer. Hosted registry-only `_meta.json` / generated skill-card
files are not activated. A public GitHub handoff is accepted only when it binds
an allowlisted codeload URL, exact repository, 40-hex commit, selected subdir,
well-formed declared content hash, and the same verified file manifest. The
file manifest is the byte-level integrity authority. Neither route executes
bundled scripts.

Public reads are fixed to `https://clawhub.ai`; callers cannot supply a host or
URL. JSON, archives, paths, files, directories, expanded bytes, timeouts, and
result counts all have independent limits. Relevance search and compact
verification caches expire after five minutes and hold at most 64 and 256
entries respectively; a 30-second, 32-entry failure cache prevents outage
hammering. Concurrent searches share one lazy four-worker verification
generation; the last active search closes it before a new generation can be
published, so an explicit search does not leave threads resident or briefly
double the hard concurrency bound. Reclaimable caches remain registered with
the host memory-pressure mechanism. `429` and `Retry-After` are preserved as
bounded retry metadata instead of being busy-retried.

The settings API is a separate explicit-user surface: it accepts multipart
package bytes, never an arbitrary server path. Catalog entries that exceed the
product resource budget remain visible with an unavailable reason rather than
silently weakening validation.

Memory and Skills load the same typed package-upload transport only with their
lazy feature chunks. Picker and OS-drop inputs share ZIP validation, FormData
construction, response parsing, and a one-active-upload guard. Each surface
installs one fixed page-lifetime set of four drag listeners; feature adapters
retain scope selection, localized toast placement, diagnostics, and
post-install refresh policy.

## Ownership and activation

Every durable operation receives an explicit owner id. Global packages live in
an owner-scoped store; project packages remain under project authority. The
legacy personal store is readable only for its declared compatibility owner.
Global enabled state is owner-specific; project state follows project
authority. All routes still resolve an explicit identity at the auth boundary.

A successful install returns the installed skill id, immutable origin fields,
and content digest. The current task may explicitly load that id. Ordinary
discovery observes it on the next task snapshot; installation does not rewrite
the frozen index mid-task.

## Invariants

- Instructions are context, never authority.
- Resident discovery is local and deterministic; online discovery is explicit,
  read-only, bounded, cached, and absent from the prompt until requested.
- Model-driven installation has no arbitrary URL or filesystem-path argument.
- Catalog installation consumes exact human approval and verified origin data;
  ClawHub approval includes the exact version returned by search.
- Package scripts are retained for inspection and never run automatically.
- Installed content activation is atomic and rollback-safe.
- Browser package upload permits one active request and one fixed listener set
  per lazy surface.
- Model-visible results never reveal an absolute package path.
- Owner, project, and enabled-state boundaries are explicit at every entry.

## Change routing and tests

| Change | Start here | Verify |
|---|---|---|
| Context/index budget | `injection.py`, context provider | complete XML, token cap, frozen snapshot |
| Discovery/read behavior | `discovery.py`, `load.py` | stable ranking, opaque ids, paging, symlink rejection |
| Online discovery | `online_catalog.py`, search API, feature module | no preload, query/result/cache caps, fail-soft outage, untrusted metadata label |
| Package policy | `installer.py`, `catalog.py` | digest, archive limits, collision, rollback tests |
| Approval/dispatch | ToolSpec, dispatch pipeline, handler | auto-mode gate, unattended rejection, one-use receipt |
| Browser package upload | `features/skills/package-installer.ts`, feature adapters | ZIP rejection, scope, one active upload, fixed drag listeners, visible failure |
| Owner/API/UI | paths, registry, API route, feature module | owner isolation, multipart-only route, unavailable card |

```bash
pytest -q tests/test_skill_channel.py \
  tests/test_skill_installation_contract.py tests/test_skill_online_catalog.py \
  tests/test_skills_api_split.py
pytest -q tests/test_skill_env_vault.py tests/test_tool_registry.py \
  tests/test_write_approval_gate.py
pytest -q tests/test_skill_package_installer.py \
  tests/test_frontend_memory_vite.py tests/test_frontend_skills_vite.py
```
