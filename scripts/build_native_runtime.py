#!/usr/bin/env python3
"""Build/check a deterministic native runtime archive; never publish or deploy.

Owner: docs/DEVELOPER_RUNTIME.md. Cargo.lock owns dependency resolution. Output
publication is exclusive/atomic; checking rejects malformed/tampered archives
before extracting or executing. Build needs Python/Rust; the runtime needs none.
"""
from __future__ import annotations
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / 'contracts/native_runtime_artifact_v1.json').read_text())
MAX_BYTES = SPEC['maximum_unpacked_bytes']
MAX_FILES = SPEC['maximum_members']
FORMAT = SPEC['archive_format']


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def native_target() -> str:
    arch = {'x86_64': 'x86_64', 'aarch64': 'aarch64'}.get(platform.machine())
    if platform.system() != 'Linux' or arch is None:
        raise ValueError('native GNU/Linux x86_64 or aarch64 host required')
    return arch + '-unknown-linux-gnu'


def execute(binary: Path, command: str, cwd: Path) -> dict:
    # No Python, compiler or checkout lookup is possible through this PATH.
    result = subprocess.run([str(binary), command], cwd=cwd,
                            env={'PATH': str(cwd / 'empty-path')},
                            capture_output=True, timeout=30, check=True)
    return json.loads(result.stdout)


def write_archive(files: dict[str, bytes], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    total = sum(map(len, files.values()))
    if total > MAX_BYTES or len(files) > MAX_FILES:
        raise ValueError('native artifact bounds exceeded')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as raw:
            temporary = Path(raw.name)
            with gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode='w') as archive:
                    for name, content in sorted(files.items()):
                        entry = tarfile.TarInfo(name)
                        entry.size = len(content)
                        entry.mode = 0o755 if name == 'bin/tofu-native' else 0o644
                        entry.uid = entry.gid = entry.mtime = 0
                        archive.addfile(entry, io.BytesIO(content))
            raw.flush()
            os.fsync(raw.fileno())
        # link refuses an existing path and cannot replace a concurrent artifact.
        os.link(temporary, output)
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def verify(path: Path, *, run: bool = True) -> dict:
    files: dict[str, bytes] = {}
    total = 0
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('archive byte bound exceeded')
    with tarfile.open(path, 'r:gz') as archive:
        for entry in archive:
            name = entry.name
            if (not entry.isfile() or name in files or name.startswith('/')
                    or any(part in {'', '.', '..'} for part in name.split('/'))
                    or entry.size < 0 or len(files) >= MAX_FILES):
                raise ValueError('invalid native archive member')
            total += entry.size
            if total > MAX_BYTES:
                raise ValueError('unpacked byte bound exceeded')
            stream = archive.extractfile(entry)
            if stream is None:
                raise ValueError('missing native member bytes')
            content = stream.read(entry.size + 1)
            if len(content) != entry.size:
                raise ValueError('truncated native archive member')
            files[name] = content
    manifest = json.loads(files['manifest.json'])
    if manifest.get('format') != FORMAT or not isinstance(manifest.get('files'), dict):
        raise ValueError('invalid native archive manifest')
    if set(files) != {'manifest.json', *manifest['files']}:
        raise ValueError('native archive member inventory differs')
    for name, metadata in manifest['files'].items():
        if metadata != {'sha256': digest(files[name]), 'bytes': len(files[name])}:
            raise ValueError('native artifact checksum differs: ' + name)
    required = set(SPEC['required_members'])
    if not required.issubset(files) or not files['bin/tofu-native'].startswith(b'\x7fELF'):
        raise ValueError('required native release member missing')
    if run:
        if manifest['target'] != native_target():
            raise ValueError('artifact target does not match this host')
        with tempfile.TemporaryDirectory(prefix='tofu-native-installed-') as temporary:
            installed = Path(temporary)
            for name, content in files.items():
                destination = installed / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            binary = installed / 'bin' / 'tofu-native'
            binary.chmod(0o755)
            if execute(binary, 'build-info', installed) != manifest['build']:
                raise ValueError('installed native build identity differs')
            session = execute(binary, 'check-session', installed)
            gateway = execute(binary, 'check-tool-gateway', installed)
            if not session['invariants_satisfied'] or not gateway['default_deny']:
                raise ValueError('installed native runtime gate failed')
    return manifest


def build(output_dir: Path, target_dir: Path, *, offline: bool = False) -> Path:
    target = native_target()
    command = ['cargo', 'build', '--locked', '--release', '-p', 'tofu-runtime',
               '--bin', 'tofu-native', '--target', target,
               '--target-dir', str(target_dir.resolve())]
    if offline:
        command.append('--offline')
    subprocess.run(command, cwd=ROOT, check=True, timeout=900)
    binary = target_dir.resolve() / target / 'release' / 'tofu-native'
    with tempfile.TemporaryDirectory(prefix='tofu-native-build-info-') as temporary:
        info = execute(binary, 'build-info', Path(temporary))
    expected_arch = target.split('-')[0]
    version = tomllib.loads((ROOT / 'packages/tofu-runtime/Cargo.toml').read_text())['package']['version']
    protocol = json.loads((ROOT / 'contracts/native_agent_stdio_v1.schema.json').read_text())['$id']
    if (info['format'] != SPEC['build_format'] or info['runtime_version'] != version
            or info['stdio_contract'] != protocol
            or info['application_version'] != (ROOT / 'VERSION').read_text().strip()
            or info['target_os'] != 'linux' or info['target_arch'] != expected_arch):
        raise ValueError('native build version/target differs')
    files = {'bin/tofu-native': binary.read_bytes(),
             'LICENSE': (ROOT / 'LICENSE').read_bytes(),
             'Cargo.lock': (ROOT / 'Cargo.lock').read_bytes(),
             'contracts/native_agent_stdio_v1.schema.json':
             (ROOT / 'contracts/native_agent_stdio_v1.schema.json').read_bytes(),
             'contracts/native_runtime_artifact_v1.json':
             (ROOT / 'contracts/native_runtime_artifact_v1.json').read_bytes(),
             'README.txt': b'Native Tofu transient runtime. See the bundled stdio contract.\n'
                           b'Run bin/tofu-native build-info or check-session.\n'
                           b'run-http-agent CONFIG_FRAME_BYTES consumes a configure JSON line\n'
                           b'and exchanges declared read-only host tools over exclusive pipes.\n'
                           b'GNU/Linux dynamic loader/libc/libgcc are required; no Python or Rust tools.\n'
                           b'Production routing/write approval/storage integration remains pending.\n'}
    metadata = subprocess.run(['cargo', 'metadata', '--locked', '--offline',
                               '--format-version', '1'], cwd=ROOT,
                              capture_output=True, check=True, timeout=60)
    packages = json.loads(metadata.stdout)['packages']
    dependencies = []
    for package in packages:
        if package['name'] == 'tofu-runtime':
            continue
        dependencies.append({'name': package['name'], 'version': package['version'],
                             'license': package['license'], 'source': package['source']})
        source = Path(package['manifest_path']).parent
        licenses = set(source.glob('LICENSE*'))
        # Version-bound notices omitted from some upstream crate packages.
        overlay = ROOT / 'packages/tofu-runtime/licenses' / f"{package['name']}-{package['version']}"
        licenses.update(overlay.glob('LICENSE*'))
        if package.get('license_file'):
            license_file = Path(package['license_file'])
            licenses.add(license_file if license_file.is_absolute() else source / license_file)
        for license_path in sorted(licenses):
            if license_path.is_file() and not license_path.is_symlink():
                name = f"licenses/{package['name']}-{package['version']}/{license_path.name}"
                files[name] = license_path.read_bytes()
    compiler = subprocess.run(['rustc', '--version'], capture_output=True, check=True,
                              timeout=30).stdout.decode().strip()
    manifest = {'format': FORMAT, 'target': target, 'build': info, 'compiler': compiler,
                'dependency_inventory': sorted(dependencies, key=lambda p: (p['name'], p['version'])),
                'files': {name: {'sha256': digest(raw), 'bytes': len(raw)}
                          for name, raw in sorted(files.items())}}
    files['manifest.json'] = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    output = output_dir / f"tofu-native-{info['application_version']}-{target}.tar.gz"
    write_archive(files, output)
    verify(output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--target-dir', type=Path, default=ROOT / 'target')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--check', type=Path)
    args = parser.parse_args()
    if args.check is not None:
        if args.output is not None:
            parser.error('--check and --output are mutually exclusive')
        print(json.dumps(verify(args.check), sort_keys=True))
    elif args.output is not None:
        print(build(args.output, args.target_dir, offline=args.offline))
    else:
        parser.error('provide --output or --check')


if __name__ == '__main__':
    main()
