# local_serve — managed local model deployment

Owns the model-PATH → running-local-provider pipeline: probe → plan → env →
process → ledger → register. Registration and operator-started endpoint
discovery both use the owner-scoped model-routing v2 authority through
`lib/model_routing/local_provider.py`; never recreate a `server_config`
provider row here.

- Domain doc: `docs/modules/llm_io.md` § Managed local deployment (package
  layout, safety rails, engine flag policy audit).
- `_plan.py` is the ONLY launch-flag authority — never let the chat model
  invent engine flags; removed upstream flags (see the module docstring) must
  never reappear. OOM ladders only reduce memory pressure.
- Agent surface: `tool_defs.py` schemas + `lib/tasks_pkg/handlers/local_serve.py`
  + the `local_serve` spec in `lib/tools/registry/_build.py`; install/start are
  approval-gated in `lib/tasks_pkg/tool_dispatch/_approval.py`.
- Durable vs reconstructible: the ledger (`data/config/local_serve.json`) is
  durable; envs/logs under `data/local_serve/` are reclaimable. Managed host
  processes are personal-mode only until an enterprise host-resource scheduler
  and owner-scoped ledger replace that explicit guard.

Verify:

```bash
pytest -q tests/test_local_serve_probe.py tests/test_local_serve_plan.py \
  tests/test_local_serve_process.py tests/test_local_serve_env_store_register.py \
  tests/test_local_serve_tools.py
```
