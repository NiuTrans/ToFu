# Extension store submission kit — Tofu Browser Bridge

> ## ⛔ STATUS: NOT SUBMITTING — decided 2026-07-31 by the project owner
>
> **We are staying on "load unpacked". Do not start a submission from this kit
> without a fresh decision from the owner.**
>
> This is a recorded PRODUCT decision, not an unfinished task and not a gap
> waiting to be filled. Everything mechanical here is already done and
> verified — the trimmed manifest is correct, the zip builds, the parity guard
> is green (see "Kit readiness" below). What stopped it was the trade-off in
> `REVIEW_RISKS.md` §"Decision to make NOW": the only realistic path to
> acceptance is shipping a **reduced build** with `browser_execute_js` and
> `debugger` removed, which is a code change that narrows what the extension
> can do. The owner chose to keep the full-capability extension and the
> three-step manual install instead.
>
> **What this means for the "one exe installs everything" idea:** it stays
> closed, and not for want of trying. Chrome permits no other route —
> `update_url` must point at the Web Store on Windows/macOS (local CRX paths
> are Linux-only), non-store `force_installed` requires an AD-domain-joined
> machine, and `--load-extension` was removed in Chrome 137
> (`--disable-extensions-except` in 139). Requesting admin rights does not
> change any of that. Users install the extension in three steps; the
> remaining friction worth attacking is the copy-paste of the bridge secret,
> which `docs/UNIFIED_DEVICE_BRIDGE_DESIGN.md` §B3 (pairing code) addresses
> INDEPENDENTLY of this kit.
>
> **Kit readiness, measured 2026-07-31** — so a future submitter starts from
> facts rather than re-deriving them:
> - `manifest.store.json` = 11 permissions, version 5.0.0, matching the
>   shipped manifest; `downloads` and `webRequest` present, `activeTab` absent.
> - `bash scripts/package_extension.sh --store` produces a valid
>   `tofu-browser-bridge-5.0.0-store.zip` (7 files, `<all_urls>`).
> - `tests/test_chrome_store_manifest_parity.py` — 25/25, and it DERIVES the
>   required permissions from the extension's real `chrome.*` calls, so it
>   keeps this kit honest while it sits idle.
>
> If the decision is revisited, the only open question is the one in
> `REVIEW_RISKS.md`: ship the reduced build, or submit full-capability and
> accept likely rejection from both Chromium stores.

Everything needed to publish the **Tofu Browser Bridge** extension to the two
Chromium extension stores. It does **not** automate submission — that needs
your own account and identity verification, which cannot be done from code.

**One package, two stores.** `scripts/package_extension.sh --store` produces a
single zip that both the Chrome Web Store and Microsoft Edge Add-ons accept;
every justification, listing and privacy answer in this folder is reused for
both.

| | Chrome Web Store | Microsoft Edge Add-ons |
|---|---|---|
| Fee | **$5** one-time | **none** |
| Individual account | yes | yes (shorter verification) |
| Where to start | `SUBMISSION_CHECKLIST.md` | `EDGE_ADDONS.md` |

> **Try Edge first, or in parallel.** It costs nothing, takes the same zip, and
> reuses this entire kit — so it is the cheapest real shot at one-click
> install, not a consolation prize. Registration verification is asynchronous,
> so start it early.

> **⚠️ Honest expectation setting.**
> This extension uses `debugger`, `<all_urls>`, `cookies`, `history`,
> `bookmarks`, and a tool (`browser_execute_js`) that runs JavaScript sent
> from the Tofu server. Both stores' **remote-code** and
> **minimum-permissions / single-purpose** policies are exactly the policies
> this design strains — and Edge's MV3 remote-code wording is *stricter* than
> Chrome's, not looser. A rejection on the first pass is the *likely* outcome,
> not a surprise. `REVIEW_RISKS.md` has the mitigation plan and the outcome
> ladder; read it before spending review cycles.

## Files

| File | What it is |
|---|---|
| `SUBMISSION_CHECKLIST.md` | Chrome, step by step: register → package → upload → fill forms → submit → respond to review. |
| `EDGE_ADDONS.md` | Edge Add-ons: the Partner Center route, what is reused verbatim, and the places Edge differs (incl. where it is stricter). |
| `REVIEW_RISKS.md` | The specific policies this extension trips, how each is mitigated, and the realistic outcome ladder across both stores. |
| `LISTING.md` | Store-listing copy: name, summary, full description, category, language. Used by both stores. |
| `PERMISSIONS_JUSTIFICATION.md` | Per-permission justification text — one box per declared permission on both dashboards. Also records which permissions the store build drops, and why. |
| `PRIVACY.md` | Privacy-policy page content + the exact answers for the data-usage disclosure form. Host it publicly and paste the URL into both dashboards. |
| `manifest.store.json` | The **trimmed** manifest for the store build — only the permissions the code actually calls. Kept in step with `browser_extension/manifest.json` by `tests/test_chrome_store_manifest_parity.py`. |
| `ASSETS_CHECKLIST.md` | The exact image assets each store requires, with sizes and a screenshot shot list. |
| `../../scripts/package_extension.sh` | Builds the upload `.zip` from `browser_extension/`, optionally swapping in `manifest.store.json`. |

## The 60-second version

1. Register: Edge at
   <https://partner.microsoft.com/dashboard/microsoftedge/public/login> (free)
   and/or Chrome at <https://chrome.google.com/webstore/devconsole> ($5).
2. `bash scripts/package_extension.sh --store` → produces
   `dist/tofu-browser-bridge-<version>-store.zip`.
3. Host `PRIVACY.md` somewhere public, note the URL.
4. Create the item → upload the zip → paste `LISTING.md` fields,
   `PERMISSIONS_JUSTIFICATION.md` blocks, the privacy URL, and the data-usage
   answers. Chrome: `SUBMISSION_CHECKLIST.md`. Edge: `EDGE_ADDONS.md`.
5. Upload assets from `ASSETS_CHECKLIST.md`.
6. Submit. Expect a review reply; answer it using `REVIEW_RISKS.md`.

## Before you touch anything here

Run the parity guard — it derives the required permission set from the
extension's real `chrome.*` calls and cross-checks the manifests, the
justification blocks and these docs:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_chrome_store_manifest_parity.py -q
```

It exists because the store manifest once omitted `downloads` while
`background.js` really called `chrome.downloads.download` — the `download`
command would have thrown for every store-installed user.
