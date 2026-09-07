# Enterprise evolution contract

This file keeps the current system evolvable from a single-user deployment to
an enterprise multi-user deployment. It is a design constraint, not a dated
audit or delivery plan.

## Product stance

Tofu optimizes for one user and one managed installation today. New work must
not make tenant isolation or horizontal execution require a rewrite. Do not
build speculative tenant administration; preserve the seams below.

## Required seams

### Identity and authorization

- Authentication produces one `AuthContext` at middleware entry.
- Authorization defaults to deny and is decided before a route calls a domain
  service.
- `user_id`/principal identity is an explicit service, repository, storage,
  task, event, and push parameter. It is never read from a module global.
- Background work copies a verified principal into its durable carrier before
  request context ends.
- Poll, replay, abort, upload, project, and artifact reads enforce the same
  owner as their create command.
- A temporary single-user default is isolated at the access boundary and
  marked `TODO(enterprise)`; core domain code does not embed user `1`.

### Persistence

- Application code uses user-scoped repository protocols and semantic storage
  operations.
- The storage sidecar owns SQL, transactions, schema, maintenance, deadlines,
  and backend selection.
- SQLite and PostgreSQL expose the same operation catalog and error semantics.
- Storage RPC identity is part of the trusted envelope; payload identity may
  not broaden it.
- Mutable JSON/file stores declare whether they are process-local caches or
  durable shared state. Shared state has a migration path into the sidecar.
- Idempotency receipts, leases, and monotonic sequences use atomic shared
  storage rather than process memory when correctness crosses replicas.

### Runtime and scale-out

- HTTP handlers are stateless. In-memory maps are caches or leases of durable
  facts and have explicit invalidation/expiry.
- A task, conversation stream, human-input wait, scheduler tick, and background
  loop each have one owner and one takeover protocol.
- Abort is durable and owner-scoped; a process-local signal may reduce latency
  but is not the authority.
- User-visible scheduled work is protected by an atomic lease so replicas do
  not duplicate it.
- Push fanout is filtered by principal before delivery. Client-side filtering
  is defense in depth, not authorization.
- Multi-replica deployment requires the certified shared backend and shared
  runtime-state configuration; boot fails closed when those prerequisites are
  absent.

## Current architecture support

The repository already provides the main seams:

- auth middleware and explicit `AuthContext`;
- a process-isolated storage sidecar with SQLite/PostgreSQL adapters;
- semantic storage clients and repository protocols;
- owner columns on conversation, turn, attempt, task, project, and billing
  domains;
- committed event and push buses;
- runtime-state and lease abstractions for work that must cross processes;
- boot guards for invalid replica/backend combinations.

These abstractions are only useful when callers use them. A new direct SQL
query, implicit user default, module-global session, or broadcast-without-owner
is an architecture regression even if single-user tests pass.

## Review checklist

For every new durable or asynchronous capability, answer:

1. Which verified principal owns it?
2. At which boundary is access denied?
3. Which repository/semantic operation stores it?
4. What is atomic with its event or idempotency receipt?
5. What happens after process death or replica takeover?
6. How is cancellation observed durably?
7. How are subscriptions, leases, and caches disposed or expired?
8. Can SQLite and PostgreSQL implement the same semantics?

If an answer is “the only process/user knows”, the design is incomplete.

## Verification owners

- Storage boundaries: `tests/test_storage_process_boundary.py`,
  `tests/test_database_access_boundary.py`, and storage certification tests.
- Request identity: auth, owner-isolation, bridge, upload, task, and push tests.
- Replica safety: runtime guard, lease, committed-event, and push-bus tests.
- Conversation isolation: [CONVERSATION_SYNC_V3.md](CONVERSATION_SYNC_V3.md)
  and its wrong-owner/atomic replay contracts.
- Operational activation order: [EPIC_D_SCALE_ROLLOUT_RUNBOOK.md](EPIC_D_SCALE_ROLLOUT_RUNBOOK.md).

When an enterprise gap is discovered, update the owning domain contract or add
a `TODO(enterprise)` at the narrow seam. Do not append a point-in-time audit
table here; Git history and issue tracking hold that evidence.
