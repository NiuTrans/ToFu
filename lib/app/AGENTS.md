# Application assembly

- `app_factory.py` and `app_lifecycle.py` own the native Quart shell and ordered
  lifespan. `app_assembly.py` is the only backend composition root allowed to
  import routes; registration never starts production services.
- `plugin_registry.py` owns all four application plugin entry-point groups.
  HTTP assembly mounts discovered blueprints; process services invoke hooks;
  task endpoints discover runtimes. Keep plugin discovery lazy and fail-soft,
  with exact loader-keyed caching only for task runtimes.
- Current ownership and verification: `docs/modules/infra_runtime.md`,
  `tests/server/test_server_import_worker_isolation.py`,
  `tests/agent_core/test_push_hub_and_plugin_registry.py`, and
  `tests/api/test_plugin_v4_projection.py`.
