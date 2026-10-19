"""Optional native CSR kernel. Import is inert; load_kernel owns one lazy library.

No Tofu, storage, networking or numeric-library dependencies. The optional
process transport loads only on explicit request.
Call-local arrays and Rust allocations are retired before solve returns.
"""
import json
from pathlib import Path
from threading import Lock

from ._kernel import Kernel

ABI_VERSION = 1
_load_lock = Lock()
_loaded_kernel = None
_manifest = None


def _package_manifest():
    """Freeze one validated build record under the existing loader lock.

    Loaders alone borrow this private snapshot. Each artifact still verifies its
    own bytes; installing a different package build requires a process restart.
    Invalid base records never publish a snapshot and may be retried.
    """
    global _manifest
    if _manifest is None:
        directory = Path(__file__).resolve().parent
        record = json.loads((directory / 'build.json').read_text())
        if record['format'] != 'tofu.repo-kernel-build/v1' or record['abi_version'] != ABI_VERSION:
            raise ValueError('unsupported native repository kernel manifest')
        _manifest = directory, record
    return _manifest


def load_kernel():
    """Verify the packaged artifact once; safely share immutable C ABI bindings."""
    global _loaded_kernel
    with _load_lock:
        if _loaded_kernel is None:
            directory, record = _package_manifest()
            filename = record['library']
            if filename not in ('libtofu_repo_kernel.so', 'libtofu_repo_kernel.dylib', 'tofu_repo_kernel.dll'):
                raise ValueError('invalid packaged repository kernel filename')
            _loaded_kernel = Kernel(directory / filename, record['library_sha256'])
        return _loaded_kernel


_loaded_discovery = None


def load_discovery():
    """Load the optional discovery artifact independently of the ranking library."""
    global _loaded_discovery
    with _load_lock:
        if _loaded_discovery is None:
            from ._discovery import Discovery
            directory, record = _package_manifest()
            discovery = record['discovery']
            filename = discovery['library']
            if discovery['abi_version'] != 1 or filename not in (
                    'libtofu_repository_discovery.so', 'libtofu_repository_discovery.dylib',
                    'tofu_repository_discovery.dll'):
                raise ValueError('unsupported native repository discovery manifest')
            _loaded_discovery = Discovery(directory / filename, discovery['library_sha256'])
        return _loaded_discovery


_loaded_discovery_process = None


def load_discovery_process():
    """Verify and share only an executable descriptor; every call owns its child."""
    global _loaded_discovery_process
    with _load_lock:
        if _loaded_discovery_process is None:
            from ._process_discovery import ProcessDiscovery
            directory, record = _package_manifest()
            worker = record['discovery']['worker']
            filename = worker['executable']
            if worker['abi_version'] != 1 or filename not in (
                    'tofu-repository-discovery', 'tofu-repository-discovery.exe'):
                raise ValueError('unsupported native repository worker manifest')
            _loaded_discovery_process = ProcessDiscovery(directory / filename, worker['sha256'])
        return _loaded_discovery_process


def open_stream_process():
    """Open a new caller-owned sealed stream image from this installed build.

    No global image, child or parser is retained. Callers explicitly close the
    returned owner after joining its children. Explicit directories remain trusted;
    None selects an installed pinned grammar bundle. Importing the package alone
    does not open any artifact.
    """
    with _load_lock:
        directory, record = _package_manifest()
        worker = record['stream']
        filename = worker['executable']
        if type(worker['protocol_version']) is not int or worker['protocol_version'] != 3 or filename not in (
                'tofu-repository-stream', 'tofu-repository-stream.exe'):
            raise ValueError('unsupported native repository stream manifest')
        digest = worker['sha256']
        bundle = None
        if 'grammar_bundle' in worker:
            from ._grammars import installed_bundle, validate_contract
            validate_contract(worker.get('grammar_contract_sha256'))
            bundle = installed_bundle(directory, worker['grammar_bundle'])
    from ._stream import StreamExecutable
    return StreamExecutable(directory / filename, digest, installed_grammars=bundle)
