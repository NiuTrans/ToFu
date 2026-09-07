# Browser extension guidance

## Scope and first read

The extension is the Chromium-side half of browser automation. Read
`docs/modules/browser_automation.md` and `docs/chrome-web-store/README.md`.
Server protocol and policy live in `lib/browser/` and `routes/browser.py`.

## Editing rules

- Keep `manifest.json`, `background.js`, popup behavior, server capability
  negotiation, and store documentation aligned.
- Treat owner, device, command, claim, lease, and one-time transfer identifiers
  as protocol data. Never replace them with "latest device" or other global
  selection.
- Preserve consent boundaries: reads follow deny policy; mutations require the
  exact approved scope. Re-check redirects and every captured-response URL.
- Bound queues, captures, result payloads, polling, tabs, retries, and retained
  diagnostics. Redact cookies, authorization headers, tokens, and page secrets.
- Request only extension permissions required by implemented behavior and
  update the store permission/privacy documents in the same change.
- Do not commit packaged ZIPs as source; build them with
  `scripts/package_extension.sh`.

## Verification

Run the focused `test_browser_*`, `test_bridge_auth.py`, and extension/store
contract tests relevant to the change. Package once for manifest or store-facing
changes and inspect the archive contents before release.
