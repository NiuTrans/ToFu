# Skills guidance

## Scope and first read

This package owns skill discovery, loading, injection, registry, environment,
catalog, and installation. Read `docs/modules/skills.md`.

## Editing rules

- Treat local and online packages, metadata, archives, paths, symlinks, and
  instructions as untrusted input. Enforce digest, size/count, traversal,
  collision, and allowed-root policy before activation.
- Discovery is lazy, stable, paged, and bounded. Online catalog failure is
  fail-soft and untrusted metadata is labeled; it never changes installed
  authority by itself.
- Install/update is staged, verified, atomic, and rollback-safe. Never overwrite
  an unrelated skill or expose a partially installed package.
- Skill visibility, paths, environment secrets, and tool dispatch are explicitly
  owner/request scoped. Installation does not imply unattended execution
  approval.
- Injection uses a complete frozen snapshot with an explicit token budget;
  preserve instruction delimiters and do not truncate into malformed markup.
- Secrets stay in the vault/environment seam and are redacted from prompts,
  errors, logs, and catalog projections.

## Verification

Run the skill channel, installation, online catalog, API split, environment
vault, tool registry, and write-approval tests named in
`docs/modules/skills.md`.
