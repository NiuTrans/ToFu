# Browser automation guidance

## Scope and first read

This package owns browser protocol, owner/device queues and leases, access
policy, high-level page APIs, capture, research, adapters, and diagnostics. Read
`docs/modules/browser_automation.md`.

## Editing rules

- Address every command, claim, result, lease, stream, grant, capture, and
  transfer by explicit owner and device. Never choose global "latest" state.
- Reads are policy-checked per effective URL; mutations require an exact live
  grant. Re-evaluate redirects, frames, captured API URLs, and source/final file
  domains.
- Queue claim and settlement are atomic and idempotent. Timeout, cancellation,
  release, device disconnect, and restart close captures/transfers and dispose
  ephemeral tabs.
- Keep extension/server protocol versions and capability negotiation aligned.
  Unknown capabilities fail explicitly or degrade only as documented.
- Bound polling, queues, leases, tabs, response bodies, DOM/text extraction,
  traversal, DevTools objects, console data, and retained evidence.
- Redact cookies, auth headers, tokens, form values, and page secrets at every
  durable log/result boundary.

## Verification

Run the smallest browser policy/protocol/queue test, then relevant bridge,
extension, file-transfer, network-evidence, research, adapter, and native-runtime
tests listed in the browser domain map.
