# Deployment guidance

## Scope and first reads

This directory owns Helm and supervisor deployment assets. Read
`docs/ARCHITECTURE.md`, `docs/modules/infra_runtime.md`, `docs/STORAGE.md`, and
`docs/RELIABILITY_RUNBOOK.md` before changing runtime topology.

## Editing rules

- `lib/process_roles.py` is the application lifecycle ownership table. Charts
  and service definitions express those roles; they do not invent alternate
  startup or shutdown paths.
- `supervisor/tofu.conf.template` is the host-neutral Supervisor source of
  truth. `supervisor/render_config.py` validates target-host values and
  `supervisor/install.sh` publishes the rendered config transactionally; never
  commit a rendered `tofu.conf` or hand-edit one into the repository.
- Personal SQLite and distributed PostgreSQL modes must fail closed when their
  declared authority is unavailable. Never add an implicit backend fallback.
- Keep identity, network policy, service accounts, secrets, probes, migration
  jobs, maintenance jobs, and worker roles explicit and least-privilege.
- Resource requests, limits, replicas, queues, disruption policy, retention,
  and autoscaling are product budgets. Lean defaults must fit the documented
  personal-computer profile; distributed overrides stay schema-validated.
- Schema migration and destructive maintenance remain one-shot, serialized,
  observable operations with rollback/recovery evidence.
- Keep `values.yaml`, `values.schema.json`, templates, and operator docs aligned.

## Verification

Run `python3 scripts/check_helm_render.py` for chart changes and the focused
process-role, deployment, storage-portability, and lifecycle tests. Render every
supported profile and inspect security contexts, volumes, probes, and resource
budgets before release. For Supervisor changes, run
`pytest -q tests/test_supervisor_conf.py` and inspect `install.sh --dry-run`
from a checkout path containing spaces or Unicode.
