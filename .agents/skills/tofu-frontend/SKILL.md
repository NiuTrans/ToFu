---
name: tofu-frontend
description: Change the Tofu browser UI — edit retained sources under frontend/src/ and rebuild with npm run build:frontend, never editing generated bundles under static/ or frontend/src/runtime/app-runtime.js.
---

# Tofu frontend changes

Edit retained browser code in `frontend/src/runtime/sections/` and styles in
`frontend/src/styles/{application,settings}/`. The generated runtime
`frontend/src/runtime/app-runtime.js` and everything under `static/` are
build outputs — change their source owners instead, then rebuild:

```bash
npm run build:frontend
```

Domain map: `docs/README.md`; architecture: `docs/ARCHITECTURE.md`.

This package is instruction-only: it has no resource files. Paths named here
are workspace files — open them with ordinary file tools such as read_files
and grep_search, not read_skill_resource.
