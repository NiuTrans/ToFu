# Native repository kernel

Standalone package outside the shared uv workspace. `rust/kernel.rs` is the
single algorithm owner; `tofu_repo_kernel/_kernel.py` owns the C ABI and input
buffers. `setup.py` builds platform wheels and their digest/ABI receipts.
Runtime imports stay standard-library-only, inert and independent of Tofu.

Build/install a wheel into an isolated target before running
`benchmarks/repository_index/check_native_csr.py --library PATH`,
`tests/repo_map/test_native_rank.py`, and real task-publication measurements.
Build outputs stay outside retained source and are never committed.

`python -m tofu_repo_kernel` certifies installed artifact integrity/computation
without a checkout. Docker role builds and immutable platform-image release
gates must fail if this certification or native application selection fails.
