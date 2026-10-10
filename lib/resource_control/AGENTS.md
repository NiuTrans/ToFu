# Resource control

`contracts/resource_policy_v1.json` owns the catalog and control policy;
`scripts/gen_resource_policy.py` generates the stdlib bootstrap defaults.
`controller.py` plans changes and records acknowledged capacities. Adapters
read and resize existing resource owners; never introduce a second counter,
provider selector, storage authority, task lifecycle, or per-user global.
`runtime.py` is attached to the serving loop, with no import-time sampler.
See `docs/RESOURCE_CONTROL.md` and `tests/resource_control/`.
