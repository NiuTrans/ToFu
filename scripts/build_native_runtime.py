#!/usr/bin/env python3
"""Build/check a deterministic native archive or explicitly install its executable.

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
import sys
import tarfile
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
if __name__ == '__main__' and not __package__:
    # Direct CLI execution must resolve the same archive authority as imports.
    sys.path.insert(0, str(ROOT))
SPEC = json.loads((ROOT / 'contracts/native_runtime_artifact_v1.json').read_text())
MAX_BYTES = SPEC['maximum_unpacked_bytes']
MAX_FILES = SPEC['maximum_members']
FORMAT = SPEC['archive_format']
COMPRESSION_PROFILES = {'distribution': 9, 'local': 0}


def compression_level(profile: str) -> int:
    if not isinstance(profile, str) or profile not in COMPRESSION_PROFILES:
        raise ValueError('native artifact compression profile must be distribution or local')
    return COMPRESSION_PROFILES[profile]


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


def write_archive(files: dict[str, bytes], output: Path, *, compression_profile: str = 'distribution') -> None:
    level = compression_level(compression_profile)
    output.parent.mkdir(parents=True, exist_ok=True)
    total = sum(map(len, files.values()))
    if total > MAX_BYTES or len(files) > MAX_FILES:
        raise ValueError('native artifact bounds exceeded')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as raw:
            temporary = Path(raw.name)
            with gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0, compresslevel=level) as compressed:
                with tarfile.open(fileobj=compressed, mode='w') as archive:
                    for name, content in sorted(files.items()):
                        entry = tarfile.TarInfo(name)
                        entry.size = len(content)
                        entry.mode = 0o755 if name == 'bin/tofu-native' else 0o644
                        entry.uid = entry.gid = entry.mtime = 0
                        archive.addfile(entry, io.BytesIO(content))
            # Stored blocks retain tar headers/padding, even when member bytes
            # fit. Never publish a source the loader's snapshot bound refuses.
            if raw.tell() > MAX_BYTES:
                raise ValueError('native compressed artifact byte bound exceeded')
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
    from lib.agent_core.native_artifact import inspect_archive
    manifest,image=inspect_archive(path,seal_image=run,verify_run=run)
    if image is not None:image.close()
    return manifest


def build(output_dir: Path, target_dir: Path, *, offline: bool = False,
          compression_profile: str = 'distribution', token_count: bool = False, flow: bool = False,
          html_projection: bool = False, checkpoint_storage: bool = False) -> Path:
    compression_level(compression_profile)  # Refuse before Cargo or publication.
    if any(type(value) is not bool for value in (token_count, flow, html_projection, checkpoint_storage)):
        raise ValueError('native extension selections must be boolean')
    extensions = [SPEC[key] for key, enabled in (('optional_token_count', token_count), ('optional_flow', flow),
        ('optional_html_projection', html_projection), ('optional_checkpoint_storage', checkpoint_storage)) if enabled]
    target = native_target()
    command = ['cargo', 'build', '--locked', '--release', '-p', 'tofu-runtime',
               '--bin', 'tofu-native', '--target', target,
               '--target-dir', str(target_dir.resolve())]
    if extensions:
        command.extend(['--lib', '--features', ','.join(extension['feature'] for extension in extensions)])
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
    for extension in extensions:
        library = binary.parent / 'libtofu_runtime.so'
        content = library.read_bytes()
        if not content.startswith(b'\x7fELF'):
            raise ValueError('native runtime library is not ELF')
        files[extension['member']] = content
        files[extension['contract_member']] = (ROOT / extension['contract_member']).read_bytes()
    metadata = subprocess.run(['cargo', 'metadata', '--locked', '--offline',
                               '--format-version', '1'], cwd=ROOT,
                              capture_output=True, check=True, timeout=60)
    packages = json.loads(metadata.stdout)['packages']
    dependencies = []
    for package in packages:
        if package['name'] != 'tofu-runtime':
            dependencies.append({'name': package['name'], 'version': package['version'],
                                 'license': package['license'], 'source': package['source']})
        source = Path(package['manifest_path']).parent
        licenses = set(source.glob('LICENSE*'))
        # Version-bound upstream notices and data embedded by first-party code.
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
    suffix = '-local' if compression_profile == 'local' else ''
    output = output_dir / f"tofu-native-{info['application_version']}-{target}{suffix}.tar.gz"
    write_archive(files, output, compression_profile=compression_profile)
    verify(output)
    return output


def install_executable(archive: Path, destination: Path, *, archive_sha256: str,
                       application_version: str) -> dict:
    """Exclusively publish checked executable bytes; return its distinct pin.

    Keep the archive for licenses/contracts and optional libraries. This does
    not activate a default backend or replace any existing installation.
    """
    from lib.agent_core.native_artifact import inspect_archive
    if (not isinstance(archive_sha256, str) or len(archive_sha256) != 64
            or any(ch not in '0123456789abcdef' for ch in archive_sha256)
            or not isinstance(application_version, str) or not application_version
            or not isinstance(destination, Path) or not destination.is_absolute()):
        raise ValueError('installation requires archive pin, version and absolute destination')
    manifest, image = inspect_archive(archive, archive_sha256=archive_sha256,
        application_version=application_version, seal_image=True, verify_run=True)
    _publish_image(image, destination)
    return dict(archive_sha256=archive_sha256, executable=str(destination),
                executable_sha256=manifest['files']['bin/tofu-native']['sha256'],
                application_version=application_version)


def install_token_library(archive: Path, destination: Path, *, archive_sha256: str,
                          application_version: str) -> dict:
    """Publish a validated token extension without loading executable library code."""
    from lib.agent_core.native_artifact import seal_token_count_archive
    if not isinstance(destination, Path) or not destination.is_absolute():
        raise ValueError('installation requires absolute destination')
    image = seal_token_count_archive(archive, archive_sha256, application_version, MAX_BYTES)
    digest = _publish_image(image, destination)
    return dict(archive_sha256=archive_sha256, token_library=str(destination),
                token_library_sha256=digest, application_version=application_version)


def install_html_library(archive: Path, destination: Path, *, archive_sha256: str,
                         application_version: str) -> dict:
    """Publish the independently declared HTML capability with its verified pin."""
    from lib.agent_core.native_artifact import seal_html_projection_archive
    if not isinstance(destination, Path) or not destination.is_absolute():
        raise ValueError('installation requires absolute destination')
    with seal_html_projection_archive(archive, archive_sha256, application_version, MAX_BYTES) as image:
        binary_bytes = os.fstat(image.fileno()).st_size
        digest = _publish_image(image, destination)
    return dict(archive_sha256=archive_sha256, html_library=str(destination),
                html_library_sha256=digest, bytes=binary_bytes, application_version=application_version)


def install_checkpoint_library(archive: Path, destination: Path, *, archive_sha256: str,
                               application_version: str) -> dict:
    """Publish the declared SQL checkpoint image, leaving scope selection explicit."""
    from lib.agent_core.native_artifact import seal_checkpoint_storage_archive
    if not isinstance(destination, Path) or not destination.is_absolute():
        raise ValueError('installation requires absolute destination')
    with seal_checkpoint_storage_archive(archive, archive_sha256, application_version, MAX_BYTES) as image:
        binary_bytes = os.fstat(image.fileno()).st_size
        digest = _publish_image(image, destination)
    return dict(archive_sha256=archive_sha256, checkpoint_library=str(destination),
                checkpoint_library_sha256=digest, bytes=binary_bytes, application_version=application_version)


def _publish_image(image, destination: Path) -> str:
    """Consume the sealed image and exclusively publish its exact checked bytes."""
    temporary = None
    try:
        image.seek(0)
        digest = hashlib.sha256()
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            while chunk := image.read(65536):
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fchmod(output.fileno(), 0o500)
            os.fsync(output.fileno())
        os.link(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return digest.hexdigest()
    finally:
        image.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--target-dir', type=Path, default=ROOT / 'target')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--compression-profile', choices=tuple(COMPRESSION_PROFILES), default='distribution',
        help='local uses stored gzip blocks for faster activation and larger local artifacts')
    parser.add_argument('--token-count', action='store_true', help='include the pure native token-count library')
    parser.add_argument('--html-projection', action='store_true', help='include the native HTML projection library and contract')
    parser.add_argument('--flow', action='store_true', help='include the native Flow control library and contract')
    parser.add_argument('--checkpoint-storage', action='store_true', help='include the native SQL checkpoint library and contract')
    parser.add_argument('--check', type=Path)
    parser.add_argument('--install-executable', type=Path, help='publish --check executable to a new absolute path')
    parser.add_argument('--install-token-library', type=Path, help='publish --check token library to a new absolute path')
    parser.add_argument('--install-html-library', type=Path, help='publish --check HTML library to a new absolute path')
    parser.add_argument('--install-checkpoint-library', type=Path, help='publish --check SQL checkpoint library to a new absolute path')
    parser.add_argument('--archive-sha256', help='trusted archive pin required for installation')
    parser.add_argument('--application-version', help='expected application version required for installation')
    args = parser.parse_args()
    destinations = [(args.install_executable, install_executable),
                    (args.install_token_library, install_token_library),
                    (args.install_html_library, install_html_library),
                    (args.install_checkpoint_library, install_checkpoint_library)]
    selected = [(path, function) for path, function in destinations if path is not None]
    if selected:
        if len(selected) != 1:
            parser.error('select one installation destination per command')
        if args.check is None or args.output is not None or args.token_count or args.flow or args.html_projection or args.checkpoint_storage:
            parser.error('installation requires --check without build options')
        if args.archive_sha256 is None or args.application_version is None:
            parser.error('installation requires --archive-sha256 and --application-version')
        destination, install = selected[0]
        print(json.dumps(install(args.check, destination,
            archive_sha256=args.archive_sha256, application_version=args.application_version), sort_keys=True))
        return
    if args.archive_sha256 is not None or args.application_version is not None:
        parser.error('installation identity options require an installation destination')
    if args.check is not None:
        if args.output is not None or args.token_count or args.flow or args.html_projection or args.checkpoint_storage:
            parser.error('--check and --output are mutually exclusive')
        print(json.dumps(verify(args.check), sort_keys=True))
    elif args.output is not None:
        print(build(args.output, args.target_dir, offline=args.offline,
                    compression_profile=args.compression_profile, token_count=args.token_count, flow=args.flow,
                    html_projection=args.html_projection, checkpoint_storage=args.checkpoint_storage))
    else:
        parser.error('provide --output or --check')


if __name__ == '__main__':
    main()
