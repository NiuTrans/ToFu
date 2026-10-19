# HTTP transport guidance

This package owns shared HTTP clients, inbound body admission, compatibility
middleware, compression, structured HTTP errors and request timing. The domain
map is `docs/modules/infra_runtime.md`; transport policy remains independent of
routes and storage implementations. `lib/app/app_assembly.py` registers inbound
owners, and `client.py` owns lazy outbound sessions and joined shutdown.

Preserve streamed request bodies, cancellation, typed errors and response byte
accounting. Run the HTTP tests under `tests/server/`, then application assembly,
server shutdown and frontend/API serving tests.
