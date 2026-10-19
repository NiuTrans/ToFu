# Working facts UI

`dialog.ts` is loaded by the conversation context meter on demand. The meter
closes its mount on navigation/teardown; `panel.ts` owns request cancellation,
drafts and bounded pages. `view.ts` projects generated API DTOs using the UI kit.
The backend contract is `docs/contracts/WORKING_FACTS.md`; the UI never changes
task state or retries a write after an unknown outcome.

Verify `panel.test.ts`, the working-facts generator check, and the real browser
journey in `tests/visual/test_visual_working_facts.py`, then frontend gates.
