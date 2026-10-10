---
name: tofu-browser-testing
description: Verify Tofu UI changes in a real browser — run the visual/e2e tier against a locally started server instead of relying on unit tests for rendering behavior.
---

# Browser testing Tofu

Unit tests cannot see rendering. For UI changes, start the app locally and
drive a real browser against it, then run the repository's visual tier as the
gate. Locate the owning verification path via `docs/README.md` and the test
ladder rules in the root `AGENTS.md` (narrowest test first, broad gates once
the worktree is stable).

This package is instruction-only: it has no resource files. Paths named here
are workspace files — open them with ordinary file tools such as read_files
and grep_search, not read_skill_resource.
