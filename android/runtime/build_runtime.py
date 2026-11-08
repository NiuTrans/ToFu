#!/usr/bin/env python3
"""Build the on-device kernel runtime images (android/runtime/README.md).

    python3 android/runtime/build_runtime.py resolve
    python3 android/runtime/build_runtime.py build --rev <commit> --frontend-dist <static/vite dir>
        [--abi arm64-v8a|x86_64] [--tofudb <aarch64 tofu-db>] [--out DIR] [--cache DIR]

``resolve`` refreshes pins.json from publisher checksum files and signed
indexes (network). ``build`` is deterministic from pins.json, the commit and
the prebuilt frontend: it writes assets/kernel/ (archives and index.json)
and report.json under ``<out>/<abi>/``. The x86_64 ABI builds a payload for
the host boot test only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tarfile

RUNTIME_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(RUNTIME_DIR))

from runtime_builder import pins as pins_module  # noqa: E402
from runtime_builder.debs import ArchiveError  # noqa: E402
from runtime_builder.elf import inspect_elf  # noqa: E402
from runtime_builder.fetch import FetchError, fetch_pinned  # noqa: E402
from runtime_builder.payload import (  # noqa: E402
    PROFILES, TOFUDB_LIBRARY, PayloadError, assemble_payload, resolve_commit)
from runtime_builder.report import elf_report, payload_component, size_report  # noqa: E402
from runtime_builder.rootfs import assemble_rootfs  # noqa: E402
from runtime_builder.tree import BlobStore, Tree, TreeError, canonical_json, write_archive  # noqa: E402

DEFAULT_PINS = RUNTIME_DIR / 'pins.json'
DEFAULT_OUT = RUNTIME_DIR / 'out'
DEFAULT_CACHE = RUNTIME_DIR / 'cache'
INDEX_FORMAT = 1
HOST_KEYRING = '/usr/share/keyrings/ubuntu-archive-keyring.gpg'


def build_rootfs(pins: dict, cache: Path, blobs: BlobStore, abi: str,
                 assets: Path) -> tuple[Tree, dict, object]:
    ubuntu = pins['ubuntu_base']
    base = fetch_pinned(ubuntu['url'], ubuntu['sha256'], None, cache)
    apt = pins['apt']
    packages = [(row['name'], str(fetch_pinned(f'{apt["archive"]}/{row["filename"]}', row['sha256'],
                                                row['size'], cache)))
                for row in apt['packages']]
    tree = Tree(blobs)
    report = assemble_rootfs(tree, str(base), packages)
    inputs = {
        'ubuntu_base': {'version': ubuntu['version'], 'sha256': ubuntu['sha256']},
        'packages': {row['name']: row['version'] for row in apt['packages']},
    }
    result = write_archive(tree, kind='rootfs', abi=abi, label=ubuntu['version'], inputs=inputs,
                           out_dir=assets)
    return tree, report, result


def rootfs_components(tree: Tree, base_paths: dict) -> dict:
    components: dict[str, list] = {'ubuntu-base': [], 'added': []}
    for entry in tree.ordered():
        unchanged = base_paths.get(entry.path) == (entry.type, entry.sha256, entry.target)
        components['ubuntu-base' if unchanged else 'added'].append(entry)
    return components


def cmd_build(args: argparse.Namespace) -> dict:
    pins = pins_module.load_pins(args.pins)
    profile = PROFILES[args.abi]
    out = args.out / profile.abi
    assets = out / 'assets' / 'kernel'
    work = out / 'work'
    for directory in (assets, work):
        if directory.exists():
            shutil.rmtree(directory)
    assets.mkdir(parents=True)
    work.mkdir(parents=True)
    cache = args.cache
    blobs = BlobStore(work / 'blobs')
    repo = args.repo
    commit = resolve_commit(repo, args.rev)
    if args.tofudb is not None:
        for engine in (args.tofudb, args.tofudb.parent / TOFUDB_LIBRARY):
            if engine == args.tofudb or engine.is_file():
                info = inspect_elf(engine)
                if info is None or info['machine'] != profile.machine:
                    raise PayloadError(f'{engine} is not a {profile.machine} ELF file')

    report: dict = {'abi': profile.abi, 'commit': commit}
    archives = {}
    if profile.abi == 'arm64-v8a':
        base_tree = Tree(blobs)
        with tarfile.open(fetch_pinned(pins['ubuntu_base']['url'], pins['ubuntu_base']['sha256'],
                                       None, cache)) as archive:
            base_tree.import_tar(archive)
        base_paths = {entry.path: (entry.type, entry.sha256, entry.target)
                      for entry in base_tree.ordered()}
        rootfs_tree, rootfs_report, rootfs = build_rootfs(pins, cache, blobs, profile.abi, assets)
        archives['rootfs'] = rootfs
        report['rootfs'] = {**rootfs_report, 'base_packages_with_updates':
                            pins['apt'].get('base_packages_with_updates', [])}
        report['rootfs_elf'] = elf_report(rootfs_tree.ordered(), profile.machine)
        rootfs_sizes = size_report(rootfs_components(rootfs_tree, base_paths), work)
    stage, payload_report = assemble_payload(
        pins=pins, profile=profile, repo=repo, commit=commit, frontend_dist=args.frontend_dist,
        tofudb=args.tofudb, cache=cache, work=work)
    payload_tree = Tree(blobs)
    payload_tree.import_dir(stage, prefix='')
    label = f'{payload_report["tofu"]["version"]}-{commit[:10]}'
    inputs = {
        'tofu': payload_report['tofu'],
        'python': payload_report['python'],
        'uv': payload_report['uv'],
        'python_platform': payload_report['python_platform'],
        'requirements_sha256': payload_report['wheels']['requirements_sha256'],
        'frontend': payload_report['frontend'],
        'tofudb': payload_report['tofudb'],
    }
    archives['payload'] = write_archive(payload_tree, kind='payload', abi=profile.abi, label=label,
                                        inputs=inputs, out_dir=assets)
    report['payload'] = payload_report
    report['payload_elf'] = elf_report(payload_tree.ordered(), profile.machine)
    components: dict[str, list] = {}
    for entry in payload_tree.ordered():
        components.setdefault(payload_component(entry.path), []).append(entry)
    components['bytecode (included above)'] = [
        entry for entry in payload_tree.ordered() if entry.path.endswith('.pyc')]
    report['sizes'] = {'payload': size_report(components, work)}
    if profile.abi == 'arm64-v8a':
        report['sizes']['rootfs'] = rootfs_sizes
    report['archives'] = {kind: {'file': result.path.name, 'version': result.version,
                                 'sha256': result.sha256, 'size': result.size,
                                 'tar_bytes': result.tar_size}
                          for kind, result in archives.items()}
    if profile.abi == 'arm64-v8a':
        index = {'abi': profile.abi, 'format': INDEX_FORMAT}
        for kind, result in archives.items():
            index[kind] = {'file': result.path.name, 'sha256': result.sha256, 'size': result.size,
                           'version': result.version}
        (assets / 'index.json').write_text(json.dumps(index, indent=2, sort_keys=True) + '\n',
                                           encoding='utf-8')
    foreign = report['payload_elf']['foreign_machine'] + report.get('rootfs_elf', {}).get(
        'foreign_machine', [])
    (out / 'report.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n',
                                     encoding='utf-8')
    shutil.rmtree(work)
    if foreign:
        raise PayloadError(f'ELF files for another machine: {foreign[:20]}')
    return report


def cmd_resolve(args: argparse.Namespace) -> dict:
    current = pins_module.load_pins(args.pins)
    refreshed = pins_module.resolve_pins(current, args.cache, args.keyring)
    pins_module.write_pins(args.pins, refreshed)
    return {'packages': len(refreshed['apt']['packages'])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--pins', type=Path, default=DEFAULT_PINS)
    parser.add_argument('--cache', type=Path, default=DEFAULT_CACHE,
                        help='download cache, keyed by SHA-256')
    commands = parser.add_subparsers(dest='command', required=True)
    resolve = commands.add_parser('resolve', help='refresh pins.json (network)')
    resolve.add_argument('--keyring', default=HOST_KEYRING,
                         help='keyring holding the Ubuntu CD image signing key')
    build = commands.add_parser('build', help='build the archives from pins.json')
    build.add_argument('--rev', required=True, help='Tofu commit to package')
    build.add_argument('--frontend-dist', type=Path, required=True,
                       help='static/vite built from that commit (npm run build:frontend)')
    build.add_argument('--abi', choices=sorted(PROFILES), default='arm64-v8a')
    build.add_argument('--tofudb', type=Path,
                       help='tofu-db engine built for the target; a libtofu_db.so beside it ships too')
    build.add_argument('--out', type=Path, default=DEFAULT_OUT)
    build.add_argument('--repo', type=Path, default=RUNTIME_DIR.parents[1],
                       help='git repository that contains --rev')
    args = parser.parse_args(argv)
    try:
        result = cmd_build(args) if args.command == 'build' else cmd_resolve(args)
    except (ArchiveError, FetchError, PayloadError, TreeError, pins_module.PinsError) as error:
        print(f'build_runtime: FAILED: {error}', file=sys.stderr)
        return 1
    if args.command == 'build':
        summary = {kind: {key: row[key] for key in ('file', 'sha256', 'size')}
                   for kind, row in result['archives'].items()}
        print(canonical_json(summary))
    else:
        print(canonical_json(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
