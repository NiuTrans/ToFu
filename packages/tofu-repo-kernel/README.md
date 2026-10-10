# Tofu repository kernel

Standalone weighted CSR PageRank with the canonical Tofu/NetworkX iteration
defaults and dangling-node behavior. It has no Tofu, database or NumPy dependency.
Import is inert; the first `load_kernel()` verifies the packaged binary digest
and initializes one immutable C ABI binding. Calls release the GIL, own their
buffers and retire Rust allocations on return. No graph/cache/thread pool is
retained. Restart the process after installing or replacing the native package.

Install a platform wheel, or build from source with a Rust compiler available:

```sh
python -m pip install ./packages/tofu-repo-kernel
python -m build packages/tofu-repo-kernel
python -m tofu_repo_kernel
```

Source builds require Rust; wheel consumers require only Python. Wheels carry a
platform tag and `py3-none` ABI tag; a build is not automatically manylinux
certified. The package resolves independently of the shared uv workspace.
Tofu automatically prefers this package for repository ranking when installed.
Missing or invalid artifacts retain the canonical NetworkX path. There is no
runtime compiler, download, storage authority or model-visible backend option.
Container builds install the wheel into API, Worker and Headless environments;
Rust stays in the build stage. The module command certifies a digest-verified
installed artifact and computation without a checkout or host application.

`rust/kernel.rs` owns the algorithm. `_kernel.py` owns buffer validation and the
C ABI. The application owns repository graph semantics and fallback policy in
`lib/repo_map/_native_rank.py`; this package imports no application code.
