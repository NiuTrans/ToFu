# LLM transport guidance

## Scope

This package owns canonical model requests, provider wire translation, streaming
parsers, and shared HTTP transport. Provider choice, slots, health, and retry
policy live in `lib/llm_dispatch/`. Read `docs/modules/llm_io.md`.

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
- Redact credentials and sensitive request/response content from errors and
  logs. Preserve fault-injection seams and deterministic fake transports.
- Optional provider dependencies load lazily; no import-time network calls.

## Verification

Run focused outbound adapter and stream/non-stream parity tests, then transport
reuse, timeout, cancellation, tool/reasoning, and usage-accounting neighbors
listed in `docs/modules/llm_io.md`.
