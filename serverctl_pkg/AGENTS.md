# Server control support guidance

## Scope

This package contains reusable support for `serverctl.py`. Server lifecycle
ownership remains in the application lifecycle modules; conversation diagnosis
begins with `debug/inspect_conversation.py`.

`native_engine.py` owns compiled source identity, bounded staging/build/download, and
pre-shutdown preparation. It never opens user databases or restarts a service.
Manifest parses are reused only within one layout/fingerprint operation; retain
child timeout/failure isolation, standard-library-only startup (`-I -S`), and
fresh reads on the next call. Verify identity,
root-input and manifest-read tests before measuring startup/recovery costs.
Source hashing streams the existing length-prefixed protocol with bounded reads;
file growth, truncation or changed metadata refuses identity. Verify
`tests/server/test_native_engine_streamed_identity.py` and compiled identity parity.

`application_restart.py` runs artifact preparation and deferred manager handoff
from current disk code in bounded child interpreters; the application retains
approval and task-interruption policy.

## Editing rules

- Keep control commands thin wrappers over declared health, lifecycle,
  diagnostic, and maintenance interfaces.
- Support bundles are bounded, deterministic inventories. Redact credentials,
  cookies, request bodies, prompts, user documents, database contents, and
  machine-specific secrets before any archive is written.
- Distinguish durable user state from reconstructible logs/cache. Collection or
  cleanup must never silently remove durable state.
- Validate every output/archive path and member name; prevent traversal and
  symlink escape. Clean only temporary paths created by the current command.
- Partial collection must report omitted/failed components instead of claiming
  a complete bundle.

## Verification

Run focused `serverctl` and support-bundle tests, including redaction, size
limits, missing-service behavior, unsafe paths, and partial-failure reporting.
