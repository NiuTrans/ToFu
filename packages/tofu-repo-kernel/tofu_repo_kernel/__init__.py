"""Optional native CSR kernel. Import is inert; load_kernel owns one lazy library.

No Tofu, storage, networking, subprocess or numeric-library dependencies.
Call-local arrays and Rust allocations are retired before solve returns.
"""
import json
from pathlib import Path
from threading import Lock

from ._kernel import Kernel

ABI_VERSION = 1
_load_lock = Lock()
_loaded_kernel = None


def load_kernel():
    """Verify the packaged artifact once; safely share immutable C ABI bindings."""
    global _loaded_kernel
    with _load_lock:
        if _loaded_kernel is None:
            directory = Path(__file__).resolve().parent
            record = json.loads((directory / 'build.json').read_text())
            if record['format'] != 'tofu.repo-kernel-build/v1' or record['abi_version'] != ABI_VERSION:
                raise ValueError('unsupported native repository kernel manifest')
            filename = record['library']
            if filename not in ('libtofu_repo_kernel.so', 'libtofu_repo_kernel.dylib', 'tofu_repo_kernel.dll'):
                raise ValueError('invalid packaged repository kernel filename')
            _loaded_kernel = Kernel(directory / filename, record['library_sha256'])
        return _loaded_kernel
