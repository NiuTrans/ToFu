"""Build the standalone Rust C ABI into a platform wheel; no host imports."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil

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
                   '-C', 'opt-level=3', '-C', 'strip=symbols', str(source), '-o', str(destination / filename)]
        subprocess.run(command, check=True, timeout=120)
        compiler_version = subprocess.check_output([compiler, '--version'], text=True, timeout=10).strip()
        record = {'format': 'tofu.repo-kernel-build/v1', 'abi_version': 1,
                  'library': filename, 'library_sha256': hashlib.sha256((destination / filename).read_bytes()).hexdigest(),
                  'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                  'compiler': compiler_version, 'edition': '2021', 'optimization_level': 3, 'strip': 'symbols'}
        discovery = root / 'rust/discovery'
        cargo = os.environ.get('CARGO', 'cargo')
        target = Path(self.build_lib).resolve().parent / 'discovery-cargo'
        subprocess.run([cargo, 'build', '--locked', '--release', '--manifest-path',
                        str(discovery / 'Cargo.toml'), '--target-dir', str(target)],
                       check=True, timeout=1800)
        discovery_filename = filename.replace('tofu_repo_kernel', 'tofu_repository_discovery')
        shutil.copyfile(target / 'release' / discovery_filename, destination / discovery_filename)
        record['discovery'] = {
            'abi_version': 1, 'library': discovery_filename,
            'library_sha256': hashlib.sha256((destination / discovery_filename).read_bytes()).hexdigest(),
            'sources': {name: hashlib.sha256((discovery / name).read_bytes()).hexdigest()
                        for name in ('Cargo.toml', 'Cargo.lock', 'lib.rs', 'main.rs')},
            'cargo': subprocess.check_output([cargo, '--version'], text=True, timeout=10).strip(),
        }
        executable = 'tofu-repository-discovery' + ('.exe' if sys.platform == 'win32' else '')
        shutil.copy2(target / 'release' / executable, destination / executable)
        record['discovery']['worker'] = {
            'abi_version': 1, 'executable': executable,
            'sha256': hashlib.sha256((destination / executable).read_bytes()).hexdigest(),
        }
        extraction = root / 'rust/extraction'
        build_environment = dict(os.environ, TSLP_OFFLINE='1')
        for key in ('TSLP_LANGUAGES', 'TSLP_SKIP_GRAMMAR_PATCHES', 'PROJECT_ROOT'):
            build_environment.pop(key, None)
        subprocess.run([cargo, 'build', '--locked', '--release', '--bin',
                        'tofu-repository-stream', '--manifest-path',
                        str(extraction / 'Cargo.toml'), '--target-dir', str(target)],
                       check=True, timeout=1800, env=build_environment)
        stream_executable = 'tofu-repository-stream' + ('.exe' if sys.platform == 'win32' else '')
        shutil.copy2(target / 'release' / stream_executable, destination / stream_executable)
        license_name = 'LICENSE.grep-ast.txt'
        shutil.copyfile(extraction / license_name, destination / license_name)
        record['stream'] = {
            'catalog_license': license_name,
            'catalog_license_sha256': hashlib.sha256((destination / license_name).read_bytes()).hexdigest(),
            'grammar_contract_sha256': hashlib.sha256((root / 'tofu_repo_kernel/grammar_bundle.schema.json').read_bytes()).hexdigest(),
            'protocol_version': 3, 'executable': stream_executable,
            'sha256': hashlib.sha256((destination / stream_executable).read_bytes()).hexdigest(),
            'sources': {str(path.relative_to(extraction)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted(extraction.rglob('*'))
                        if path.is_file() and (path.suffix == '.rs' or path.name in ('Cargo.toml', 'Cargo.lock'))},
            'grammar_registry': {'name': 'tree-sitter-language-pack', 'version': '1.20.0',
                                 'runtime_downloads': False, 'grammars_bundled': False},
        }
        grammar_source = os.environ.get('TOFU_REPO_GRAMMAR_BUNDLE')
        grammar_pin = os.environ.get('TOFU_REPO_GRAMMAR_MANIFEST_SHA256')
        if bool(grammar_source) != bool(grammar_pin):
            raise ValueError('native grammar bundle requires both an explicit directory and manifest SHA256')
        if grammar_source:
            import importlib.util
            spec = importlib.util.spec_from_file_location('_tofu_grammar_build', root / 'tofu_repo_kernel/_grammars.py')
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            record['stream']['grammar_bundle'] = helper.package_bundle(grammar_source, destination, grammar_pin)
            record['stream']['grammar_registry']['grammars_bundled'] = True
        (destination / 'build.json').write_text(json.dumps(record, indent=2) + '\n')


setup(distclass=NativeDistribution, cmdclass={'build_py': NativeBuild, 'bdist_wheel': NativeWheel})
