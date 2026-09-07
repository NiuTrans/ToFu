# Server control support guidance

## Scope

This package contains reusable support for `serverctl.py`. Server lifecycle
ownership remains in the application lifecycle modules; conversation diagnosis
begins with `debug/inspect_conversation.py`.

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
