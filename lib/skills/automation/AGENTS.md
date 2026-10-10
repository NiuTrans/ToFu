# Automated skill sources

This package owns explicit owner/tenant provider access and complete frozen
snapshots for automation actions. Start with `docs/modules/skills.md` and
`contracts/automation_skill_sources_v1.json`.

- `mtskills_cli.py` invokes only installed CLI metadata commands through the
  trusted `mtskills_metadata.mjs` bootstrap. Secrets enter through a private
  descriptor, never OS argv; no ambient login/cache, CIBA, or package extraction.
- `mtskills_archive.mjs` reads binary bytes from the policy-selected HTTPS origin
  with an absolute abort deadline and no redirects; it never extracts a package.
- Remote metadata and archives remain untrusted. Archive validation and atomic
  package activation belong to the existing `lib/skills/installer.py` owner.
- Snapshots name exact complete bytes and explicit owner/tenant. A project scope
  binding is metadata and cannot grant filesystem writes or script execution.
- `source_settings.py` owns sealed immutable vault references, public config DTOs
  and source CAS/replay. Generic credential routes cannot expose managed versions.
- `artifacts.py` owns complete private content promotion and read verification;
  database snapshot acceptance/quota remains a separate selected-storage boundary.
- Verify subprocess bounds, cancellation, secret isolation, source identity,
  frozen replay and approval boundaries in `tests/skills/` before installing
  an automation execution port.
