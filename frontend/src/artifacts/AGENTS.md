# Standalone artifact rendering

`math-auto-render.js` owns the existing classic adapter for sandboxed artifact
views. `scripts/frontend_vendor_assets.mjs` copies it into the generated static
delivery path alongside the lockfile's KaTeX. Edit this source, then run
`npm run generate:vendor`; never edit the static copy.

Preserve delimiter behavior, ignored elements, inert fallback text and the
artifact route's script ordering/CSP. Verify vendor generation, math dependency
delivery and real sandboxed browser rendering before the full frontend gate.
