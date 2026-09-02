# Bootstrap guidance

## Scope

`bootstrap_pkg/` owns the zero-configuration install and launch path used before
the full application can be assumed importable. Runtime assembly after boot
belongs to the owners named in `docs/modules/infra_runtime.md`.

## Editing rules

- Keep import-time dependencies minimal and tolerate the documented lean
  fallback environment. Do not import application modules merely for constants.
- Preserve explicit environment re-exec, provider setup, install status, and
  error reporting. A partial install must be recoverable and must not be
  reported as healthy.
- Derive resource defaults through the canonical runtime probe; do not add a
  second CPU, memory, or disk sizing heuristic.
- Make downloads, subprocesses, retries, temporary files, and cleanup bounded.
  Never print tokens or provider credentials.
- Keep launcher behavior consistent across `bootstrap.py`, packaging entry
  points, and the developer runtime.

## Verification

Run focused tests located with
`rg --files tests | rg '(bootstrap|install|launcher|runtime_probe)'`, then
`python3 scripts/check_developer_runtime_artifacts.py` when packaged bootstrap
behavior changes.
