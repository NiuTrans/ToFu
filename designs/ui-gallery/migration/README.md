# Phase-4 token sweep — before/after evidence

Authority: `docs/DESIGN_SYSTEM.md` (section "Phase-4 token sweep").

A value-identical codemod converted 3,478 legacy declarations to design
tokens (2,293 spacing + 1,156 font-size + 29 scoped colors). This
directory holds the proof that the sweep changed zero rendering.

## Method

1. `capture_app.py /tmp/sweep-before` — boots the real app against a
   disposable storage root (same isolation as `tests/conftest.py`),
   screenshots the welcome surface and the settings modal in all three
   themes with reduced motion.
2. Codemod applied, stylesheet recomposed.
3. `capture_app.py /tmp/sweep-after`, then
   `diff_app.py before after` — per-channel pixel diff (tolerance 8/255).
4. Control: `capture_app.py` run TWICE on the post-sweep code and diffed
   against itself to measure each surface's dynamic-content noise floor.

## Result

| Surface | before/after drift | identical-code control | Verdict |
|---|---|---|---|
| welcome tofu / dark / light | 0.003–0.009% | 0.006–0.031% | pixel-stable |
| settings dark / light | 0.001–0.003% | 0.003–0.007% | pixel-stable |
| settings tofu | 0.334% | 0.339% | stable — drift equals the surface's dynamic-content noise floor |

`settings-tofu` repaints asynchronously (its changed-pixel clusters are
text bands plus a scrollbar thumb, reproducible between identical-code
runs); the codemod contribution is the delta between the two columns,
i.e. zero.

`before/` and `after/` keep the raw PNG pairs for eyeball review.
