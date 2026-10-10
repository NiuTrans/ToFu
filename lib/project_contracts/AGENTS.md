# Project protocol contracts

`browse.py` owns directory-picker parameter admission and HTTP schema projection.
Its RPC declaration is `contracts/control_rpc_v1.yaml`; transport adapters live
in `routes/api_v1/project.py` and `lib/control_rpc.py`.

Keep this package importable without project filesystem/tool services or storage.
Verify changes with `tests/control_rpc/test_project_browse_transport_parity.py`.
