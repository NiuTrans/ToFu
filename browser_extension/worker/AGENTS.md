# Browser worker owners

- `result-outbox.js`: transient server/device/profile queues and single-settlement
  receipts; leased results count as pending until acknowledged.
- `poll-transport.js`: one request, abort/deadline, retry timer, HTTP classification
  and public connection status. Every post-await action verifies request ownership.
- `command-execution.js`: scoped in-flight deduplication, command deadline,
  temporary-resource cleanup, counters and result enqueue.
- `../background.js` supplies explicit Chrome/network/timer ports. These are
  classic worker scripts, loaded synchronously before registering listeners;
  never capture these classes from a function serialized into a page.

Verification: `tests/browser/test_browser_worker_owners.py`,
`test_browser_poll_lifecycle.py`, `test_browser_temporary_tabs.py`,
`test_browser_bridge_auth_backoff.py`, `test_browser_bridge_auto_repair.py`;
then browser/auth contracts and `tests/release/test_chrome_store_manifest_parity.py`.
`test_browser_extension_worker_package.py` loads packaged dev/store workers in
real Chromium (`visual`). The standard worker probe uses the shipped import
graph through `tests/support/browser_worker_loader.cjs`.
