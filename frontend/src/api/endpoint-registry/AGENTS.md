# Endpoint facade ownership

`index.ts` constructs one registry from explicit transport and live language
ports. The module named after each public domain owns that domain's existing
wire paths, options and result projection. `contracts.ts` describes retained
open JSON inputs; generated domain schemas remain their own authority.

`ports.ts` delegates HTTP, `ApiError`, affinity and IDs to `api/transport.ts`.
`idempotent-write.ts` retries only declared target-state writes with one key;
`resolver-recovery.ts` recovers only read-only resolvers and unattributed
worker/gateway failures. `retry-delay.ts` owns abortable wait cleanup.
`project-path-singleflight.ts` owns bounded, registry-scoped path writes and clones
responses for independent consumers. Conversation GET single flight belongs
only to transport, uses complete request identity and releases on settlement.

No module performs network work or starts timers at construction. No browser
global, permanent response cache, fallback transport or new durable state is
allowed. The retained `runtime/sections/api.js` binds this registry until its
last classic caller migrates. Keep generated Orchestration installation intact.

Verify native lifecycle outcomes with `test_frontend_endpoint_registry.py`,
paired methods/production routes with `test_frontend_api_contract.py`, malformed
lists with `frontend_api_lists.test.mjs`, transport/recovery with the neighboring
API suites, then `npm run check:frontend`.
