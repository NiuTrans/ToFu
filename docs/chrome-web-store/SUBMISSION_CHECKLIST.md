# Submission checklist

Work top to bottom. Items marked **[you]** require your account/identity and
cannot be done from code. Items marked **[done]** are already prepared in this
kit.

## 0. Decide the build (do this first)

- [ ] Read `REVIEW_RISKS.md`. Decide: are you willing to ship the **reduced**
      Chrome build (no `browser_execute_js`, no `debugger`) if the reviewer
      rejects the full one for remote code? If **no**, stop — Firefox AMO is
      the better path for full power. If **yes**, continue.

## 1. Register **[you]**

- [ ] Go to https://chrome.google.com/webstore/devconsole.
- [ ] Pay the one-time **$5** developer registration fee.
- [ ] Complete identity verification if prompted (can take a day or two).

## 2. Build the upload package **[done — re-run to refresh]**

- [ ] `bash scripts/package_extension.sh --store`
- [ ] Confirm output: `dist/tofu-browser-bridge-4.3.0-store.zip`
- [ ] (Sanity) the zip's `manifest.json` has **10** permissions, not 16.

## 3. Host the privacy policy **[you]**

- [ ] Publish `PRIVACY.md` Part 1 at a public URL (GitHub Pages / project site).
- [ ] Replace the `<your contact …>` placeholder before publishing.
- [ ] Note the URL — you paste it in step 5.

## 4. Prepare assets **[you — see ASSETS_CHECKLIST.md]**

- [ ] 128×128 store icon (already have `icon128.png`).
- [ ] At least one 1280×800 screenshot (shot list in `ASSETS_CHECKLIST.md`).
- [ ] (Optional) 440×280 promo tile.

## 5. Create the item & fill the listing **[you — copy from this kit]**

- [ ] Dashboard → **New item** → upload `tofu-browser-bridge-4.3.0-store.zip`.
- [ ] **Store listing** tab: paste name / summary / description / category /
      language from `LISTING.md`. Upload icon + screenshots.
- [ ] **Privacy practices** tab:
  - [ ] Single-purpose statement (from `PERMISSIONS_JUSTIFICATION.md`).
  - [ ] One justification per permission (paste each block).
  - [ ] Data-usage checkboxes + 3 certifications (from `PRIVACY.md` Part 2).
  - [ ] Remote-code question → **Yes** + the explanation from `PRIVACY.md`.
  - [ ] Privacy-policy URL (from step 3).
- [ ] **Distribution**: choose visibility (Unlisted is a good first choice —
      installable by link, not surfaced in search, while you iterate on review).

## 6. Submit **[you]**

- [ ] Click **Submit for review**.
- [ ] Record the submission date; manual review for high-permission items can
      take several days.

## 7. Respond to review **[you — script in REVIEW_RISKS.md]**

- [ ] If you get a rejection, read which policy it cites.
- [ ] Remote-code rejection → either argue the "user's own server" point once,
      or fall back to the reduced build (REVIEW_RISKS.md, outcome ladder #2).
- [ ] `debugger` rejection → drop `debugger`, switch screenshots to the
      viewport-only path, resubmit.
- [ ] Re-package (`--store`), bump `version` in BOTH
      `browser_extension/manifest.json` and `manifest.store.json`, re-upload.

## 8. After acceptance — wire one-click install **[code, optional follow-up]**

Once you have a public item id (`https://chrome.google.com/webstore/detail/<id>`):

- [ ] Update the Browser Bridge modal (`index.html` `#browserModal`) to show a
      prominent **"Add from Chrome Web Store"** button linking to the listing,
      with the existing "load unpacked" steps kept as a fallback for Firefox /
      offline / privacy-conscious users.
- [ ] Keep `GET /api/browser/download` as the manual/unpacked fallback.
- [ ] Tell me the item id and I'll do that frontend change.

---

### Version-bump reminder

The store rejects re-uploads with the same version. To resubmit, bump
`"version"` in **both** manifests and re-run `--store`.
