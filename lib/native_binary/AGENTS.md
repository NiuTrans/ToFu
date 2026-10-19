# Native image admission owner

`__init__.py` owns dependency-free regular-file, trusted digest and sealed-image
validation shared by executor and SQL authority adapters. Keep import identity
stable through `lib.agent_core.native_binary`; no backend selection, native
loading or executor imports belong here. Verify pinned-image mutation and
allocation failures plus native SQL controller close/reentry tests. The retained
contract and rationale live in `docs/DEVELOPER_RUNTIME.md`.

`generated_checkpoint_storage_contract.py` is the dependency-neutral ABI spec,
generated from `contracts/native_checkpoint_storage_abi_v1.json` by
`scripts/gen_native_checkpoint_storage_abi.py`; the Sidecar import is a generated
compatibility facade. Archive validation may import this spec without importing
SQL adapters. The Sidecar lazily loads code only after explicit pinned admission.
