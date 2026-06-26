# Chrome Web Store submission kit — Tofu Browser Bridge

This folder is everything you need to submit the **Tofu Browser Bridge**
extension to the Chrome Web Store. It does **not** automate the submission —
that requires your Google account, a one-time **$5** developer registration,
and identity verification, none of which can be done from code.

> **⚠️ Read this first — honest expectation setting.**
> This extension uses `debugger`, `<all_urls>`, `cookies`, `history`,
> `bookmarks`, and a tool (`browser_execute_js`) that runs JavaScript sent
> from the Tofu server. The Chrome Web Store's **remote-code** and
> **minimum-permissions / single-purpose** policies are exactly the policies
> this design strains. A rejection on the first pass is the *likely* outcome,
> not a surprise. The materials here are written to give the best possible
> shot and to make the review reasons concrete if it is rejected. See
> `REVIEW_RISKS.md` for the mitigation plan.

## Files

| File | What it is |
|---|---|
| `LISTING.md` | Store-listing copy: name, summary, full description, category, language. Paste into the Developer Dashboard fields. |
| `PERMISSIONS_JUSTIFICATION.md` | Per-permission justification text. The dashboard requires one box per permission — copy each block verbatim. |
| `PRIVACY.md` | Privacy-policy page content + the exact answers for the "Data usage" disclosure form. You must host the privacy policy at a public URL and paste that URL into the dashboard. |
| `manifest.store.json` | A **trimmed** manifest (6 unused permissions removed) intended for the *store* build. See `REVIEW_RISKS.md` for why. |
| `SUBMISSION_CHECKLIST.md` | Step-by-step: register → package → upload → fill forms → submit → respond to review. |
| `ASSETS_CHECKLIST.md` | The exact image assets the store requires (icon, screenshots, tile) with sizes and a shot list. |
| `REVIEW_RISKS.md` | The specific policies this extension trips and how each is mitigated. Read before submitting. |
| `../../scripts/package_extension.sh` | Builds the upload `.zip` from `browser_extension/`, optionally swapping in `manifest.store.json`. |

## The 60-second version

1. Register once at https://chrome.google.com/webstore/devconsole ($5).
2. `bash scripts/package_extension.sh --store` → produces `dist/tofu-browser-bridge-<version>-store.zip`.
3. Host `PRIVACY.md` somewhere public, note the URL.
4. In the dashboard: create item → upload the zip → paste `LISTING.md` fields,
   `PERMISSIONS_JUSTIFICATION.md` blocks, the privacy URL, and the data-usage answers.
5. Upload assets from `ASSETS_CHECKLIST.md`.
6. Submit. Expect a review reply; answer it using `REVIEW_RISKS.md`.
