# PostgreSQL worker clock

This directory owns the optional database-server clock used by SQL worker leases.
The lifecycle contract is `contracts/worker_job_lifecycle_v1.json`; resource and
installation ownership belongs to `docs/STORAGE_OPERATIONS.md`.

- PostgreSQL 17+ named dynamic shared memory owns the clock and its random epoch.
- No caller time, environment clock override, thread, durable clock row or model
  code enters this module. Production SQL cannot set or rewind the clock.
- Keep the output versioned and bounded. The adapter validates it before use.
- Build through PGXS against the actual server version. Never install into a live
  server while testing; load a private compiled library into an owned test cluster.
- Verify separate connections, concurrent initialization, close/reconnect,
  database/server lifecycle, monotonic progression and failure admission.
