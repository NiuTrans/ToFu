# Webhook delivery coordination

`coordinator.py` owns ordered references per explicit owner/subscription identity,
independent demand HTTP dispatch and completion/retry timing. Routes inject auth,
current configuration, HTTP and clock ports; `tofu_workers/json_frames.py` owns
encoding and physical worker close. No queue/active-count admission belongs here.
Verify webhook concurrency, fan-out, revocation, retry and shutdown tests under
`tests/server/`; durable subscriptions remain in the existing configuration store.
