# Storage operation-domain guidance

## Scope

This directory groups the semantic storage catalog by domain. It owns operation
names, validation, transaction/authority metadata, and routing to focused
implementations—not delivery-layer policy.

## Editing rules

- Each operation has one stable name and explicit request/result shape,
  ownership keys, transaction mode, idempotency/natural key, retry class, bounds,
  and receipt behavior.
- Register an operation in exactly one domain. Avoid generic escape hatches or
  raw-query operations that leak persistence vocabulary upward.
- Owner filtering and authorization-relevant existence behavior are part of the
  semantic contract and cannot be optional adapter flags.
- Command operations define atomic event/outbox effects and conflict behavior;
  query operations declare deterministic order and apply filters before limits.
- Keep domain registration declarative and import-safe. Heavy backend work and
  SQL remain in `operations_pkg/` and adapters.

## Verification

Run operation-registry/manifest validation and the focused Sidecar contract
cases for the domain, including unknown operation, malformed input, ownership,
idempotency, conflict, and bound behavior.
