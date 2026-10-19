# Sidebar Coordinator prototype owner

This directory is a standalone design surface, not application runtime. Root
rules apply. Its product rationale and explicit simulation limits live in
`README.md`; current production authority remains `docs/PROJECT_COORDINATION.md`.

Edit `index.html`, `prototype.css`, `prototype.js`, `verify.py`,
`check-style.mjs` and `build-preview.py`. Generate `ui-kit.bundle.js`,
`preview-cjk.woff2`, `preview.html` and `previews/*` through `verify.py`.
Do not hand-edit generated artifacts or production bundles.

Use the real factories from `frontend/src/ui/` and token policy from
`scripts/style_token_policy.mjs`. Keep the prototype isolated: no credentials,
application API, real conversation IDs, provider calls or production storage.
The narrow gate is `python3 designs/sidebar-coordinator/verify.py`; inspect the
resulting screenshots as well as the executable interaction checks.
