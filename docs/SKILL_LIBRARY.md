# Skill library and filesystem scope

Owner: `lib/skills/settings.py`, `lib/skills/registry.py`,
`frontend/src/features/skills/{library,panel,package-install-panel}.ts`.
Wire ownership remains the skills route metadata and generated
`contracts/api_endpoint_index_v1.json`.

## Problem

A settings request without project context used to fall back to the Tofu server
checkout. Skills in another workspace were absent even when that workspace was
already in the user's recent projects. An id-only action could select a different
same-id package, and “move to project” neither identified the project nor
explained that it removed the source directory.

## Decision

The settings library enumerates the authenticated owner's added projects using
the existing recent-project repository, plus the owner's personal skill store.
“Add project directory” registers a directory in that same navigation history;
it does not attach the directory to a conversation. Discovery scans only the
roots declared in `lib/skills/paths.py`, never arbitrary sibling directories or
the machine's home on behalf of every web user. Refresh reports partial source
failures and keeps the successful sources visible.

Settings and runtime have different queries over the same scanner. The library
requests `include_shadowed=1` and keeps same-id packages separately. Runtime
selection still uses the precedence-ordered winner from projects attached to
that task and the owner's personal store. Looking at a skill in settings never
makes another project's instructions available to the current conversation.

Each settings entry includes its `project_path`, display-only `source_path`, and
opaque `skill_ref`. The latter hashes the canonical package location and is
resolved again against the request's owner/context; it is not an authorization
token or an arbitrary source-path input. Missing/stale references fail closed.
Settings file management may show absolute paths; model discovery/resource
results continue to use the existing `skill://` boundary.

Every settings command carries the selected project's query parameter and,
for existing packages, its `skill_ref`. No project parameter means the owner's
personal store only. Skills are owner-global (owner decision 2026-10-31): the
Skills destination chooser resolves every catalog, ZIP install and move to the
personal store without offering projects, and runtime lets the personal
package win its id over same-id project copies (see `docs/modules/skills.md`).
Personal means all this owner's conversations/projects, not every server user.

“Move files…” appears only on project packages and confirms the exact source,
the personal-store destination and the resulting availability.
`POST /api/v4/skills/<id>/scope` still accepts `target_project_path` for API
callers that explicitly request a project destination. It moves the actual
directory, including resources, and rejects a collision or escaping
destination. Cancellation or closing the panel does not move files. Enable/disable changes the package's enabled flag in `SKILL.md`; it
neither relocates files nor changes the package's project scope.

## Alternatives considered

- Sweeping the server filesystem would conflate discovery with owner authority,
  expose unrelated workspaces, and make settings depend on host layout.
- Importing every project's skills into every chat would defeat project scope.
- Treating scope as a visibility-only flag while still calling `shutil.move`
  would misrepresent the user's filesystem change.
- Hiding duplicate ids in settings would retain ambiguous file management,
  even though precedence remains appropriate for runtime invocation.

The distinction follows Codex's documented project/user skill locations and
separate enable state, and its app-server's explicit per-CWD skill listing.
Tofu keeps its own owner-scoped server storage instead of assuming that a web
principal owns the server process's home directory.
Sources: [Codex skills](https://learn.chatgpt.com/docs/build-skills),
[Codex app server](https://learn.chatgpt.com/docs/app-server).

## Verification

`tests/skills/test_skills_api_split.py` exercises exact-source mutations,
no-default-project behavior, duplicate listing and physical project moves.
`frontend/src/features/skills/panel.test.ts` exercises cross-project discovery,
filtering, global-only movement confirmation, cancellation, teardown and partial
errors. `tests/skills/test_skill_global_policy.py` pins global precedence and
promotion.
The existing skill-channel, owner-vault, attached-root, installer and API parity
suites protect runtime visibility and shared package boundaries.
