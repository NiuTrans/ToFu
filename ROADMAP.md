# Roadmap

> **This file is a pointer.** The canonical, §10-aware development plan lives at
> [`docs/DEVELOPMENT_DIRECTION.md`](docs/DEVELOPMENT_DIRECTION.md).

Tofu's roadmap is organized in execution phases. Each phase has a gate flag
indicating whether CLAUDE.md §10 approval (hyperparameters, model routing,
DB schema, security-sensitive code) is required, and an exit criterion that
can be objectively verified.

## Current focus

- **Phase A — Hygiene & P0 security** _(in progress)_
  Flask `secret_key` randomisation, SVG upload removal, Feishu secret
  scrub verification, uniform 500 error envelope, empty-stub cleanup,
  audit-log coverage expansion, error-log triage tool.
  See `docs/DEVELOPMENT_DIRECTION.md §5 Phase A`.

## Next up

- **Phase B — Compaction split + Todo tool + Reactive-compact hardening.**
  Split `lib/tasks_pkg/compaction.py` (2620 LOC) into focused modules,
  ship `todo_write` tool + continuation enforcer hook, and add regression
  tests for reactive-compact retry caps.
  See `docs/DEVELOPMENT_DIRECTION.md §5 Phase B`.

## Later phases

- **Phase C** — Routes splits (`paper.py`, `daily_report.py`), persistent
  rate-limiter, `X-Bridge-Secret` auth on browser/desktop bridges.
- **Phase D** — `tasks_pkg` monoliths + `llm_client.py` + frontend splits
  (separate approval gate for frontend per `refactor_decomposition_proposal.md`).
- **Phase E** — New capabilities (Critic discipline, swarm delegation
  template, planner write-block hook, speculation UI, MT provider
  expansion, CLI-backend parity, mobile polish).
- **Phase F** — Trading consolidation (user-led decision per
  `refactor_inventory.md §8a`).

## Ground rules

- **No dates, SLOs, or dollar figures** — Tofu does not measure uptime,
  cost ceilings, or latency targets at the project level today. Inventing
  those here would be unfounded. Concrete numbers belong in a separate
  proposal once metrics exist. See `docs/DEVELOPMENT_DIRECTION.md §6`.
- **§10 approvals are per-item** — every change that touches a
  hyperparameter, the model-routing table, the DB schema, or
  security-sensitive code requires an explicit user sign-off and an
  `audit_log('config_change', …, approved_by='user')` entry
  (CLAUDE.md §10.5).
- **Export hygiene is synchronous** — any new secret literal, internal
  endpoint, provider identifier, or absolute path added to source must
  be mirrored in `export.py` in the same change set (CLAUDE.md §11).

## Reference documents

| Document | Purpose |
|---|---|
| `docs/DEVELOPMENT_DIRECTION.md` | Canonical direction, §10-gated item list, phase sequencing |
| `docs/refactor_decomposition_proposal.md` | Plan-only refactor splits for oversized modules |
| `docs/refactor_inventory.md` | LOC inventory and Phase 1–2 execution history |
| `docs/SECURITY_AUDIT_REPORT.md` | Security findings and remediation status |
| `docs/RATE_LIMITING_DOS_AUDIT_REPORT.md` | Rate-limit audit and future recommendations |
| `docs/agentic-development-experience.md` | Agent-loop architecture + backlog |
| `docs/omc-claude-code-backport-analysis.md` | Claude Code feature comparison / backport ideas |
| `CHANGELOG.md` | Released versions |
| `CLAUDE.md` | Project intelligence & mandatory rules for AI assistants |
