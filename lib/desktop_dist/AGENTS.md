# Desktop distribution guidance

## Scope

This package owns desktop-agent artifact discovery, metadata, download, checksum,
and installation/update preparation. Build workflows and platform installers are
separate source owners.

## Editing rules

- Identify artifacts by explicit platform, architecture, kind, version, source,
  size, and checksum. Never install an ambiguous "latest" payload without
  verified metadata.
- Validate URLs through the canonical egress policy; bound redirects, download
  size/time, retries, and temporary disk.
- Verify checksum/signature before extraction or execution. Reject traversal,
  unsafe links, duplicate members, incompatible formats, and partial archives.
- Stage updates atomically with rollback. Do not overwrite a running install or
  delete the previous usable version before the new one is certified.
- Keep published workflow asset names and installer metadata in lockstep.
  Credentials and private endpoints never enter manifests or logs.

## Verification

Run `tests/test_desktop_dist.py`, desktop build workflow, release asset, installer
parity, checksum, traversal, partial-download, and rollback tests. Smoke-test the
affected platform package in its supported environment.
