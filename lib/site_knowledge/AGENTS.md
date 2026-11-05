# Learned site rule ownership

- `repository.py` owns explicit principal/browser identity and record CAS over
  the configured storage client. `contracts/site_knowledge_v1.schema.json` is
  the persisted-shape authority; no SQL, file store, browser or model fallback.
- `__init__.py` exposes scoped reads/publications and explicit legacy adoption.
  Unassigned legacy JSON is read-only migration input, never runtime authority.
- Live execution/proof belongs to `../site_doctor/`; browser authorization stays
  in `../browser/access.py`. See `docs/modules/browser_automation.md`.
- Verify owned repository and repair-ownership tests under `tests/llm_dispatch/`,
  then auth-source, search-provider and browser-scrape contracts.
