# Review risks & mitigation — read before submitting

This is the honest risk assessment. The extension's design (a server-driven
automation bridge with `debugger`, `<all_urls>`, `cookies`, and remote JS
execution) directly strains several Chromium-store policies. Going in with
eyes open is better than a surprise rejection.

The risks below apply to **both** the Chrome Web Store and Microsoft Edge
Add-ons — same package, near-identical policies. Where Edge is stricter
(remote code under MV3) it is called out in `EDGE_ADDONS.md`.

## Risk 1 — Remote code execution (HIGHEST)

**Policy:** "Including executable code (e.g., by calling its own server to
retrieve code) is not allowed." The Web Store wants all logic in the package.

**How we trip it:** the `browser_execute_js` command runs JS the Tofu server
sends. Functionally that is "retrieve code and run it."

**Mitigation / argument:**
- The code comes from the **user's own** server that the user runs and
  controls — not from an author-controlled remote endpoint. This is the crux;
  state it everywhere (listing, privacy remote-code box, any review reply).
- The capability is the same primitive Selenium/Playwright `.evaluate()`
  expose; it is the extension's declared single purpose (automation), not a
  hidden updater.
- The package contains a **complete, self-contained** background script; it
  does not download additional extension logic to extend itself.

**If still rejected:** the realistic fallback is to ship a Chrome build that
**omits `browser_execute_js`** (and any other "run arbitrary JS" command) and
keeps only structured commands (read text, click selector, fill form,
screenshot). The structured commands cover most real tasks. Keep the full
power version as the "load unpacked" / Firefox build. This is a code change,
not a doc change — decide before investing in resubmission.

## Risk 2 — `debugger` permission

**Policy:** extensions using `debugger` get heightened manual scrutiny and are
frequently rejected for consumer distribution.

**Mitigation:** it is used for exactly one thing — full-page screenshots via
`Page.captureScreenshot` — and the justification says so precisely. If the
reviewer balks, the screenshot feature can fall back to `captureVisibleTab`
(viewport-only) and `debugger` can be dropped entirely. That is a small code
change in `background.js` (the `fullPage=false` path already exists).

## Risk 3 — Broad permissions vs. minimum-permissions policy

**Policy:** request the narrowest permissions that work.

**Mitigation already applied:** `manifest.store.json` drops every permission
the code cannot justify — `webNavigation`, `clipboardRead`, `clipboardWrite`,
`declarativeNetRequest`, `management`, `offscreen` (all zero-call), plus
`activeTab` (which is gesture-granted and can never be granted here, since
every command arrives from the server long-poll rather than a user click).
`management` in particular is a classic rejection trigger, and it was pure
dead weight. Each remaining permission has a concrete, code-backed
justification — see the REMOVED table in `PERMISSIONS_JUSTIFICATION.md`, which
`tests/test_chrome_store_manifest_parity.py` keeps in step with the manifest.

`<all_urls>` + `cookies` + `history` + `bookmarks` together still constitute a
broad request. The defense is the genuinely general-purpose single purpose
(automate any page the user directs). There is no way to narrow `<all_urls>`
for a tool whose whole job is "work on whatever page my task is about."

## Risk 4 — Single purpose

**Policy:** an item must have one narrow purpose.

**Mitigation:** the single-purpose statement frames everything as one purpose
(a bridge for the user's assistant). Reading tabs, screenshots, form-fill,
cookies, history, bookmarks are all *means* to that one purpose, not separate
features. Keep the listing framed that way; do not describe them as a grab-bag
of independent tools.

## Risk 5 — "Companion app required" usefulness

**Policy/CRX3-era practice:** reviewers sometimes flag extensions that "do
nothing on their own."

**Mitigation:** the listing states up front this is a companion to a
self-hosted app and is only useful with a running Tofu server. That honesty is
allowed — many legitimate companion extensions exist — but expect a reviewer
note; answer that the popup clearly instructs the user to enter their server
URL and shows status.

## Realistic outcome ladder

Applies to BOTH Chromium stores — the Chrome Web Store and Microsoft Edge
Add-ons take the same package, so a rung reached in one is usually reachable
in the other. See `EDGE_ADDONS.md`.

1. **Best case:** accepted after a manual review round, possibly after one
   clarifying reply about remote code. One-click install achieved.
2. **Middle case:** accepted only after shipping the **no-`browser_execute_js`,
   no-`debugger`** reduced build (Risk 1 + 2 fallbacks). Slightly less
   powerful, but a real store listing with one-click install.
3. **If Chrome rejects on remote code — next stop is Edge Add-ons, not
   Firefox.** Same zip, **no registration fee**, individual accounts
   supported, and the whole justification kit is reused verbatim, so the
   marginal cost is close to zero. Be honest about the odds though: Edge's MV3
   rule on remote code is worded MORE absolutely than Chrome's (see
   `EDGE_ADDONS.md`), so this is most likely to succeed with the reduced build
   from rung 2 — not as a way to keep `browser_execute_js`.
4. **Worst case:** rejected by both Chromium stores. Firefox AMO is the next
   option (signed, self-hostable `.xpi`, one-click, historically tolerant of
   these permissions) but it is the MOST expensive path, not the fallback of
   first resort: Firefox needs a real code port (no `chrome.debugger`, so
   full-page screenshots must be rebuilt on scroll-and-stitch
   `captureVisibleTab`, plus a `background.scripts`/`service_worker` dual
   declaration) AND a signing pipeline, because Firefox has no persistent
   "load unpacked" — an `about:debugging` add-on disappears on browser
   restart. Otherwise stay on "load unpacked" for Chromium, which is what the
   in-app Local Control guidance already supports.

## Decision to make NOW

Before you spend review cycles: are you willing to ship the **reduced** build
(no remote JS, no debugger) if asked? If yes, the odds of an eventual
acceptance on a Chromium store are decent, and you get two shots at it for one
package — Chrome and Edge Add-ons. If no (you need full `browser_execute_js`
in the store build), the honest expectation is rejection from BOTH Chromium
stores, and the question becomes whether the Firefox port + AMO signing
pipeline is worth building.
