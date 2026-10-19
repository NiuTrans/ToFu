# Standalone Tofu-DB client

`_frame_body.py` owns anonymous overflow buffers shared by v1/v2 readers;
decoding must return owned values before mappings close. `wire.py` owns
storage.v2 framing, authentication, bounded streaming and session
lifetime. The public import surface is `tofu_db`; this directory also builds the
independent `tofu-db-client` wheel. `_crc32c_accelerator.py` owns the optional
lazy compiler, bounded cache, process-group cleanup and atomic publication. Never import `lib`, `scripts`, or application
configuration. `_loopback.py` is the stdlib-only owner of local allocation
recovery shared with the lifecycle manager and SQL client. Package exports stay
lazy so that control-plane imports never load wire schemas or accelerators.
Semantic adaptation and supervision belong in `lib/storage/`.

`generated_contract.py` comes from `scripts/gen_storage_operation_catalog.py`;
never change the schema version by hand. `generated_v2.py` comes from
`scripts/gen_storage_v2_contract.py` and owns committed-envelope limits. Wire authority remains
`contracts/storage_v2.json`. Keep errors typed and do not retry ambiguous writes.

EmbeddedRuntime.checkpoint_control is explicit native checkpoint C ABI, not a
second Python journal. Generated constants come from gen_native_checkpoint_control.py.
The generator also owns daemon transport constants and the packaged schema.
Verify test_checkpoint_control and actual private library/binary probes; capacity
is admitted before effects and commands never grow-replay. Close joins callers;
forget does not release a durable lease. Whole Goal claim/handoff commands reuse
this port; only explicit identical successful group delivery may retrieve its
existing IDs before first member use. No automatic retry is added to the client.
Package remains application-independent.
Verify optional acceleration with `tests/tofudb/test_tofudb_crc_accelerator.py`.
Verify the protocol with `tests/storage/test_storage_public_boundary.py`,
`tests/tofudb/test_tofudb_v2_client.py`, `tests/storage/test_storage_v2_client.py`, and the native
differential gate. Build the wheel and import it outside the checkout.
