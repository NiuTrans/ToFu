# Site repair ownership

- `__init__.py` exposes the search listener, single-run entry and tool schemas.
- `catalog.py` owns supported-site briefs, model schemas and repair instructions.
- `verification.py` owns one run's live proof and terminal tool decisions. Its
  browser and knowledge-write callables carry the caller's existing authority;
  this module must not discover users, devices or credentials.
- `runner.py` composes the shared agent loop, transcript and model accounting.
  It never implements a second model/tool loop.
- `ownership.py` captures task/browser scope and defers effects until the admitted
  search completes; workers borrow that scope without settling it. Speculative
  reads never receive repair admission.
- `trigger.py` records drift and deduplicates exact owned attempts. The handler
  owns execution lifetime; do not launch detached workers here.
- `../site_knowledge/repository.py` owns versioned scoped rule storage; the
  facade preserves legacy candidates only for explicit, live-verified adoption.

Behavior and rationale: `docs/modules/browser_automation.md`. Verify
`tests/llm_dispatch/test_site_doctor.py`, the owned repository/repair tests, search drift/provider and
auth-source registry contracts, then the shared agent-loop suite.
