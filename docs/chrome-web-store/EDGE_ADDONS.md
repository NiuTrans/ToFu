# Microsoft Edge Add-ons — submission path

> **Same package as Chrome.** `scripts/package_extension.sh --store` produces
> ONE zip that both stores accept; there is no Edge-specific build. Everything
> in `PERMISSIONS_JUSTIFICATION.md`, `PRIVACY.md` and `LISTING.md` is reused
> verbatim. This file only records where Edge DIFFERS from
> `SUBMISSION_CHECKLIST.md`.

Edge is Chromium: the same `chrome.*` namespace, the same MV3 service-worker
background, the same `manifest.json`. Microsoft publishes an explicit
"port a Chrome extension" path, and the Partner Center uploader takes the
Chrome zip as-is.

---

## Why Edge is the FIRST store to try, not the fallback

| | Chrome Web Store | **Edge Add-ons** |
|---|---|---|
| Registration fee | **$5** one-time | **none** |
| Individual (non-company) account | yes | **yes** — shorter verification; it only checks the publisher display name is available |
| Package | the `-store.zip` | **the same `-store.zip`** |
| Company account | — | longer: Microsoft phones a named "company approver", days to weeks |

Edge costs nothing to attempt and reuses work already done, so **attempt Edge
first or in parallel** — not only after Chrome rejects. Registration is the
long pole (account verification runs asynchronously), so start it early: you
can keep developing while it verifies.

⚠️ Choose **Individual** at registration unless you specifically want the
company listing. Account type and account country/region are **read-only after
enrollment** and switching company → individual is not supported.

---

## The one place Edge is STRICTER than Chrome — read before submitting

`REVIEW_RISKS.md` Risk 1 (remote code) does **not** get easier here. It gets
harder, and stated as an absolute:

> Microsoft: "Remote code is only supported for Manifest V2, not Manifest V3.
> In Manifest V3, loading and executing remotely hosted code is not permitted."

We ship MV3, and `browser_execute_js` plus DevTools expression/call-frame
evaluation run JS the user's own Tofu server
sends. Chrome's policy leaves argument room ("the user's own server, not an
author-controlled endpoint"); Edge's MV3 wording leaves noticeably less. The
Privacy page has an explicit **"Are you using remote code?"** radio with a
justification box — answer it **honestly**; the developer policy states that
inaccurate disclosure can itself cause rejection, so a wrong "No" is worse
than a declared "Yes".

Practical consequence: the **reduced build** contemplated in
`REVIEW_RISKS.md` outcome ladder #2 (no `browser_execute_js` or
`browser_devtools`, and optionally
no `debugger`) is MORE likely to be required for Edge than for Chrome. Decide
that before spending review cycles, not after.

Everything else transfers unchanged: `debugger` still draws heightened
scrutiny, `<all_urls>` is still a broad request with the same single-purpose
defence.

---

## Delta vs. the Chrome checklist

Do `SUBMISSION_CHECKLIST.md` steps 0–2 first (decide the build, build the
zip). Then, instead of the Chrome dashboard:

1. **Register [you]** — <https://partner.microsoft.com/dashboard/microsoftedge/public/login>
   with a Microsoft account (MSA). A GitHub account also works: signing in
   with it creates the MSA for you. Work/school accounts are **not** accepted
   for registration. No fee.
2. **Create the item** — Partner Center → Home → Workspaces → **Edge** card →
   **Create new extension** → drag in the `-store.zip`.
3. **Availability** — `Hidden` is the Edge equivalent of Chrome's "Unlisted":
   installable by link, absent from search. Good first choice while iterating.
   Users who installed while it was Public keep the extension and keep getting
   updates if you later switch to Hidden.
4. **Properties** — Category, and a Privacy-policy URL (same URL as Chrome —
   `PRIVACY.md` Part 1).
5. **Privacy page** — the four sections map onto files already in this kit:

   | Edge Privacy section | Reuse from |
   |---|---|
   | Single Purpose | `PERMISSIONS_JUSTIFICATION.md` single-purpose statement |
   | Permission justification (one box per declared permission) | the per-permission blocks in `PERMISSIONS_JUSTIFICATION.md` |
   | Are you using remote code? | `PRIVACY.md` remote-code explanation — see the warning above |
   | Data usage + certifications | `PRIVACY.md` Part 2 |

   Because the boxes are generated from the manifest, the permission trim in
   `manifest.store.json` directly shrinks this form. It is also why a
   permission the code never calls is a liability, not a spare capability —
   `tests/test_chrome_store_manifest_parity.py` enforces that.
6. **Store listings** — reuse `LISTING.md`. Two Edge-specific size rules that
   differ from Chrome (see `ASSETS_CHECKLIST.md`):
   - **Description minimum 250 characters** (Chrome has no floor). The
     detailed description in `LISTING.md` is well over this — reuse it, do not
     paste the short summary.
   - **Extension logo 1:1, 300 × 300 recommended, 128 × 128 minimum.** Our
     `icon128.png` meets the minimum; a 300 × 300 render is preferred.
   - Promo tiles are the same sizes as Chrome (440 × 280 small, 1400 × 560
     large), so anything produced for Chrome carries over.
7. **Certification notes & submit** — state that the extension is a companion
   to a self-hosted server and give the reviewer a way to see it working;
   a reviewer who cannot run it may reject it as non-functional (the same
   "companion app required" risk as Chrome, `REVIEW_RISKS.md` Risk 5).

---

## Version bumps

Same rule as Chrome: a re-upload needs a higher `"version"`, bumped in **both**
`browser_extension/manifest.json` and `manifest.store.json`. The two are kept
in lock-step by `tests/test_chrome_store_manifest_parity.py`, which fails if
they drift — that guard exists because the store manifest once sat at 4.3.0
while the shipped code was 4.5.0, which would have published newer code under
an older version label.
