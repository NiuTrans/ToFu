# Browser automation guidance

## Scope and first read

This package owns browser protocol, owner/device queues and leases, access
policy, high-level page APIs, capture, research, adapters, and diagnostics. Read
`docs/modules/browser_automation.md`.
Native operation schemas live in `adapters.py`; `site_tool.py` only projects
and invokes them. Read targets/receipts live in `content_contract.py`;
`content.py` is the sole acceptance owner for DOM, snapshot and captured data.
`read_pipeline.read_json_payload` admits dedicated attributes using
`content_contract.JsonReadSource`, the same content cleaner, and live
`BrowserPage.authorize_read_source` policy; no guessed response URLs.
`content.collect_content_and_network` feeds accepted, cleaned responses
directly to diagnostic inventory; never re-admit raw responses by receipt URL.
`data_islands.py` reads embedded page data with one fixed `execute_js` script
and hands decoded islands to `content.py` as `hydrated_state`; never evaluate
page script text or render raw island JSON.
`site_scope.py` owns declared adapter-domain matching; `BrowserPage.site_scope`
binds it for one invocation, including workflow-owned pages. The live access
policy and lease lifecycle remain separate authorities.
`BrowserPage._run` also owns iframe authorization: obtain the target document
from `page_state`, check its URL, and pin `expectedDocumentId` for execution.
Never substitute the parent tab URL for a child document's permission or source.

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
