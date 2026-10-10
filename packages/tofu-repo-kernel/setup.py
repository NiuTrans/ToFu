"""Build the standalone Rust C ABI into a platform wheel; no host imports."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from setuptools import Distribution, setup
from setuptools.command.build_py import build_py
from setuptools.command.bdist_wheel import bdist_wheel


class NativeDistribution(Distribution):
    def has_ext_modules(self):
        return True


class NativeWheel(bdist_wheel):
    def get_tag(self):
        # ctypes uses a C ABI, independent of the running CPython ABI.
        _, _, platform = super().get_tag()
        return 'py3', 'none', platform


class NativeBuild(build_py):
    def run(self):
        super().run()
        root = Path(__file__).resolve().parent
        destination = Path(self.build_lib) / 'tofu_repo_kernel'
        filename = ('tofu_repo_kernel.dll' if sys.platform == 'win32' else
                    'libtofu_repo_kernel.dylib' if sys.platform == 'darwin' else
                    'libtofu_repo_kernel.so')
        destination.mkdir(parents=True, exist_ok=True)
        compiler = os.environ.get('RUSTC', 'rustc')
        source = root / 'rust/kernel.rs'
        command = [compiler, '--edition=2021', '--crate-type', 'cdylib',
                   '-C', 'opt-level=3', str(source), '-o', str(destination / filename)]
        subprocess.run(command, check=True, timeout=120)
        compiler_version = subprocess.check_output([compiler, '--version'], text=True, timeout=10).strip()
        record = {'format': 'tofu.repo-kernel-build/v1', 'abi_version': 1,
                  'library': filename, 'library_sha256': hashlib.sha256((destination / filename).read_bytes()).hexdigest(),
                  'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                  'compiler': compiler_version, 'edition': '2021', 'optimization_level': 3}
        (destination / 'build.json').write_text(json.dumps(record, indent=2) + '\n')


setup(distclass=NativeDistribution, cmdclass={'build_py': NativeBuild, 'bdist_wheel': NativeWheel})
