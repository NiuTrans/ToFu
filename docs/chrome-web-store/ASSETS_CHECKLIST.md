# Store assets checklist

Both Chromium stores require specific images, and sizes are enforced — wrong
dimensions are rejected at upload. Prepare these before you start the listing.
Unless a row says otherwise, one asset satisfies both stores.

## Required

| Asset | Size (px) | Format | Notes |
|---|---|---|---|
| **Store icon / extension logo** | 128 × 128 (Chrome) · **300 × 300 preferred** (Edge, 128 min) | PNG | You already have `browser_extension/icon128.png`. Confirm it is exactly 128×128 and has no alpha-edge artifacts. Edge wants a 1:1 logo and recommends 300 × 300, so render one if you publish there too. |
| **Screenshot(s)** | 1280 × 800 **or** 640 × 400 | PNG/JPEG | At least **1**, up to 5. 1280×800 strongly preferred. Optional on Edge, but include them. See shot list below. |

## Optional but recommended

| Asset | Size (px) | Notes |
|---|---|---|
| **Small promo tile** | 440 × 280 | Shown in store search/category. Improves listing quality. Same size on both stores. |
| **Marquee / large promo tile** | 1400 × 560 | Only used if featured; skip unless you want it. Same size on both stores. |

> **Edge listing copy has a floor Chrome does not:** the description must be at
> least **250 characters**. Use the detailed description from `LISTING.md`, not
> the short summary.

## Verify the icon size

```bash
python3 - <<'PY'
from PIL import Image
im = Image.open('browser_extension/icon128.png')
print('icon128:', im.size, im.mode)  # expect (128, 128)
PY
```

(If Pillow isn't available: `file browser_extension/icon128.png` prints the
dimensions too.)

## Screenshot shot list (1280 × 800)

Take these from a running Tofu instance. They must show the *real* product — no
mockups, no competitor logos, no "Chrome" branding.

1. **The bridge setup modal** — open the Browser Bridge modal in the Tofu app
   (`index.html` `#browserModal`): the 3 setup steps + connection status. This
   is the clearest "what is this" shot.
2. **The extension popup, connected** — click the toolbar icon; show the green
   "Connected" dot, the server URL field, and the stats grid.
3. **An assistant task using a tab** — a Tofu chat where the assistant called
   `browser_read_tab` or `browser_screenshot` and returned a result. Shows the
   value.
4. *(optional)* **A full-page screenshot result** — demonstrates the
   `debugger`-backed capture feature that justifies that permission.
5. *(optional)* **Pause control** — the popup with the Pause button,
   illustrating user control (good for the review narrative).

## Tips that prevent rejection

- No text that implies Google endorsement; no Chrome logo.
- Don't show other people's private data in screenshots — use a demo page.
- Keep the icon visually distinct; it must not imitate another known product.
- 1280×800 PNGs compress fine; keep each under a few hundred KB.
