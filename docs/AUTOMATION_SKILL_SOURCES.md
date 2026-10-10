# Owned automation skill sources

Responsibility: explicit provider settings, bounded provider transport, complete
private packages and their durable artifact boundary. Entry points live under
`lib/skills/automation/`; [skill ownership](modules/skills.md) remains canonical
for ordinary installed packages and execution policy.

Authorities: [source policy](../contracts/automation_skill_sources_v1.json),
[management DTOs](../contracts/automation_management_v1.schema.json) and
[storage operations](../contracts/automation_storage_v1.schema.json).

## Source configuration

`GET/PUT /api/v4/automations/skill-source` reads or revises the authenticated
owner/tenant's mtskills binding. Reads require `automations:read`; updates also
require `automations:manage` and `skills:install`. The closed request chooses an
allowlisted environment, active state, expected revision and stable request ID.
Omitting the credential retains its version; null explicitly removes it.
An active binding requires a sealed credential. Public responses contain only
configuration state, revision and timestamps, never token, reference or seal.

`automation.skills.source.update` atomically preserves immutable identity and
created time, applies revision CAS and returns the original semantic result on
an identical request. Its result has exactly `version`, `value` and
`updated_at_ms` in native TofuDB and the SQL Sidecar. Generic record mutation
cannot overwrite or delete this reserved namespace. Retirement stops new source
access while retaining previously accepted user state.

## Credential lifecycle

Problem: mutable credentials let a competing write or retry change the secret
behind an accepted source revision. Decision: stage an unpredictable private
vault version, bind its content seal to owner/tenant, and atomically select the
reference in storage. Alternatives: overwriting a deterministic vault key races
with active readers; copying plaintext into storage exposes it through records.

The existing encrypted vault owns key material and its 128-entry quota.
Cross-process creation, updates and deletion use its atomic JSON lock; staged
creation never overwrites. Identical requests hash the content seal rather than
the random reference, so concurrent duplicates select the first accepted version.
Already accepted replay allocates nothing, including when the vault is full.
Definite rejection deletes only that caller's stage. Unknown acknowledgements
retain it until a later authoritative revision proves its CAS cannot win.
Cleanup never deletes the current reference. Generic credential HTTP listing,
reveal, update and deletion exclude this managed namespace; generic prompt and
subprocess credential discovery already exclude skill-scoped entries.

Provider access requires `skills:read`, the current owned active binding and an
exact vault lookup whose token still matches its stored private seal. No ambient
token, personal legacy entry or interactive login supplies missing authority.

## Provider and artifact boundaries

Metadata runs only installed CLI read commands through a trusted Node bootstrap.
Credentials enter via a private descriptor and never OS argv. The process has a
private home, temporary directory and empty SSO cache, bounded output and an
absolute deadline. Cancellation kills the private Linux process group, reaps
the leader and waits for live descendants before removing those directories.
This port currently requires Linux process observation; it has no unsafe fallback.

Archive transport uses allowlisted HTTPS origins, bounded streamed bytes and
an absolute abort deadline; redirects and bundled script execution are denied.
It avoids the CLI's extraction-before-validation path. The installation owner
validates ZIP policy, unambiguous guide/frontmatter and complete resource seals
in private staging, without mutating the installed catalog.

`SkillArtifactStore` copies every sealed file into an explicit private owned
scope, verifies copied bytes, fsyncs files and directories, then atomically
publishes immutable content. Its manifest lives beside the package. An existing
digest is reverified and synced before reuse; a changed package fails closed.
Deployment supplies package and volume-headroom bounds. Promotion alone grants
no database acceptance, activation, filesystem writes in a project or script
execution. Durable accepted artifacts are never implicitly reclaimed here.

Persistent growth includes every sealed resource plus the adjacent manifest;
deduplication adds no content bytes for an already verified scoped digest.
The executable 10 MiB capacity fixture preserves 10,485,842 content bytes plus
a 449-byte manifest and measures allocated file bytes separately from logical
bytes. Copying streams through 64 KiB buffers; bounded manifests scale with file
count. Headroom is checked before copying and again before publication. This
helper starts no resident worker or discovery loop; application composition owns
launch-derived concurrency and persistent acceptance budgets.

Verification: actual subprocess/HTTPS fixtures in `tests/skills/`, selected
authority source CAS and lost-ack tests, cross-process vault admission,
authenticated v4 secret isolation and complete artifact corruption/fsync probes.
