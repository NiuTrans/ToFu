# LLM transport guidance

## Scope

This package owns canonical model requests, provider wire translation, streaming
parsers, and shared HTTP transport. Provider choice, slots, health, and retry
policy live in `lib/llm_dispatch/`. Read `docs/modules/llm_io.md`.

tool_wire owns final discovery/program/multi-agent schema visibility shared by
Python transport and native preparation. It grants no execution authority;
cache/protocol translation remain later stages. Preserve diagnostics and latches.

## Editing rules

- Normalize once into the canonical request/body model, then translate in the
  focused outbound adapter. Do not leak provider-specific fields into unrelated
  callers.
- Keep streaming and non-streaming behavior aligned for text, reasoning, tool
  calls, finish reasons, usage, errors, and cancellation.
- Incremental parsers tolerate documented frame fragmentation but fail clearly
  on invalid or truncated terminal data. Never manufacture successful
  settlement after a broken stream.
- Transport owns connection reuse, TLS/proxy behavior, timeouts, idle-stream
  detection, decompression, response bounds, and safe teardown—not model
  fallback policy.
- Diagnostic values and request archives follow docs/LOGGING.md. Preserve
  their resource bounds, fault-injection seams and deterministic fake transports.
- Optional provider dependencies load lazily; no import-time network calls.
- Sealed request ingress honors an injected model-budget port. Sync/async
  attempt scopes must settle it once; abandoned unsent plans void their lease.
  Input confirmation requires a verified provider outcome. Sealed requests
  retain the dispatch-owned logical model separately from the wire alias;
  private identity/ports never enter provider JSON.

## Verification

Run focused outbound adapter and stream/non-stream parity tests, then transport
reuse, timeout, cancellation, tool/reasoning, and usage-accounting neighbors
listed in `docs/modules/llm_io.md`.
