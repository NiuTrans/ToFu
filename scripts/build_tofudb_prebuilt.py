#!/usr/bin/env python3
"""Build and package the prebuilt tofu-db engine assets install.sh downloads.

WHY THIS SCRIPT EXISTS
----------------------
``install.sh`` installs tofu-db from a per-platform release asset
(``tofu-db-<version>-<rust-triple>.tar.gz`` + ``.sha256`` sidecar) instead of
compiling on the user's machine. This script is the producer half of that
supply chain, run by a maintainer (or CI) at release time:

  * builds ``packages/tofu-db`` with ``cargo build --release --target
    <triple>`` — defaulting to the musl static triples on Linux so the
    published binary has no glibc floor (the same trick that lets the Codex
    CLI ship one binary for every Linux);
  * smoke-runs the produced engine when the host can execute it;
  * packages a deterministic gzip tarball (fixed mtime/ownership/order) whose
    only member is the engine binary, plus a ``<asset>.sha256`` sidecar in
    the exact ``<hex>  <name>`` format ``install_tofudb_prebuilt.py``
    verifies;
  * optionally appends the assets to a SHA256SUMS manifest (the format
    ``scripts/release_sign.py`` signs and ``tofu-db certify-release``
    verifies) and/or uploads them to a GitHub release with ``gh``.

Stdlib-only. ``--upload`` is the only networked mode and is always explicit.
Assets land in ``dist/tofudb-prebuilt/`` (gitignored).

Examples:
  python3 scripts/build_tofudb_prebuilt.py                      # host-appropriate static triple
  python3 scripts/build_tofudb_prebuilt.py --target aarch64-unknown-linux-musl
  python3 scripts/build_tofudb_prebuilt.py --sums-out SHA256SUMS --upload v0.17.4
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile

REPO_ROOT = Path(__file__).resolve().parents[1]
CRATE_MANIFEST = REPO_ROOT / 'packages/tofu-db/Cargo.toml'
CRATE_TARGET_DIR = REPO_ROOT / 'packages/tofu-db/target'
DEFAULT_OUT_DIR = REPO_ROOT / 'dist/tofudb-prebuilt'
CARGO_TIMEOUT_S = 3600
SMOKE_TIMEOUT_S = 10
# Deterministic archive metadata: the same engine bytes must produce the
# same asset bytes on any build host.
ARCHIVE_MTIME = 0

LINUX_STATIC_TRIPLES = {
    'x86_64': 'x86_64-unknown-linux-musl',
    'aarch64': 'aarch64-unknown-linux-musl',
}
DARWIN_TRIPLES = {
    'x86_64': 'x86_64-apple-darwin',
    'arm64': 'aarch64-apple-darwin',
}
KNOWN_TRIPLES = sorted({
    *LINUX_STATIC_TRIPLES.values(),
    *DARWIN_TRIPLES.values(),
    'x86_64-unknown-linux-gnu',
    'aarch64-unknown-linux-gnu',
})


class BuildError(RuntimeError):
    """An actionable build/packaging failure."""


def _run(command: list[str], *, timeout: int, env: dict | None = None,
         capture: bool = False, cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command, timeout=timeout, env=env, check=False, cwd=cwd,
            capture_output=capture, text=capture,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f'{" ".join(command)} exceeded its {timeout}s bound') from exc


def _host_static_triple() -> str:
    machine = platform.machine().lower()
    if sys.platform == 'darwin':
        triple = DARWIN_TRIPLES.get(machine)
    elif sys.platform.startswith('linux'):
        triple = LINUX_STATIC_TRIPLES.get(machine)
    else:
        triple = None
    if triple is None:
        raise BuildError(
            f'no default prebuilt triple for {sys.platform}/{machine}; '
            f'pass --target explicitly (known: {", ".join(KNOWN_TRIPLES)})')
    return triple


def _rust_target_installed(triple: str) -> bool:
    rustup = shutil.which('rustup')
    if rustup is None:
        return True  # no rustup: let cargo itself report a missing target
    listing = _run([rustup, 'target', 'list', '--installed'], timeout=30, capture=True)
    return triple in (listing.stdout or '').split()


def _musl_cross_env(triple: str) -> dict:
    """Resolve the musl C toolchain for the bundled C deps (sqlite/mimalloc/zstd)."""
    arch = triple.split('-', 1)[0]
    host_arch = {'arm64': 'aarch64', 'amd64': 'x86_64'}.get(platform.machine().lower(), platform.machine().lower())
    candidates = [f'{arch}-linux-musl-gcc']
    if host_arch == arch:
        candidates.append('musl-gcc')
    compiler = next((shutil.which(name) for name in candidates if shutil.which(name)), None)
    if compiler is None:
        raise BuildError(
            f'{triple} needs a musl C toolchain for the bundled C sources '
            '(libsqlite3-sys/libmimalloc-sys/zstd-sys). Install one — Debian/Ubuntu: '
            f'`apt install musl-tools` (x86_64) or `gcc-{arch}-linux-musl`; macOS: '
            '`brew install FiloSottile/musl-cross/musl-cross` — or pass a gnu --target '
            'to produce a glibc-linked build for this host only.')
    env = dict(os.environ)
    upper = triple.upper().replace('-', '_')
    env[f'CARGO_TARGET_{upper}_LINKER'] = compiler
    env[f'CC_{triple.replace("-", "_")}'] = compiler
    return env


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def build_engine(triple: str) -> Path:
    cargo = shutil.which('cargo')
    fallback = Path.home() / '.cargo/bin/cargo'
    if cargo is None and fallback.is_file():
        cargo = str(fallback)
    if cargo is None:
        raise BuildError('no cargo on PATH and no ~/.cargo/bin/cargo; install rustup first')
    if not _rust_target_installed(triple):
        raise BuildError(
            f'rust target {triple} is not installed; run `rustup target add {triple}`')
    env = _musl_cross_env(triple) if triple.endswith('-unknown-linux-musl') else dict(os.environ)
    # cwd = the crate dir: cargo discovers .cargo/config.toml from the
    # working directory upward (never from --manifest-path), so a crate-local
    # host toolchain pin (old base images) is honoured exactly as a manual
    # `cd packages/tofu-db && cargo build` would honour it.
    result = _run(
        [cargo, 'build', '--locked', '--release', '--manifest-path', str(CRATE_MANIFEST),
         '--target', triple],
        timeout=CARGO_TIMEOUT_S, env=env, cwd=CRATE_MANIFEST.parent)
    if result.returncode != 0:
        raise BuildError(f'cargo build --release --target {triple} failed (see output above)')
    binary = CRATE_TARGET_DIR / triple / 'release' / ('tofu-db.exe' if 'windows' in triple else 'tofu-db')
    if not binary.is_file():
        raise BuildError(f'cargo reported success but {binary} was not produced')
    return binary


def _host_executes(triple: str) -> bool:
    machine = platform.machine().lower()
    host_arch = {'arm64': 'aarch64'}.get(machine, machine)
    target_os = 'darwin' if 'apple-darwin' in triple else 'linux' if 'linux' in triple else None
    host_os = 'darwin' if sys.platform == 'darwin' else 'linux' if sys.platform.startswith('linux') else None
    return host_os == target_os and triple.startswith(host_arch + '-')


def _smoke(binary: Path) -> None:
    result = _run([str(binary)], timeout=SMOKE_TIMEOUT_S, capture=True)
    output = (result.stdout or '') + (result.stderr or '')
    if 'tofu-db' not in output:
        raise BuildError(
            f'{binary} did not identify as tofu-db on a no-arg run (exit={result.returncode})')


def package_asset(binary: Path, version: str, triple: str, out_dir: Path) -> tuple[Path, Path]:
    """Write the deterministic archive + sha256 sidecar; return both paths."""
    asset = out_dir / f'tofu-db-{version}-{triple}.tar.gz'
    out_dir.mkdir(parents=True, exist_ok=True)
    info = tarfile.TarInfo(name=binary.name)
    info.size = binary.stat().st_size
    info.mode = 0o755
    info.mtime = ARCHIVE_MTIME
    info.uid = info.gid = 0
    info.uname = info.gname = ''
    # GzipFile(mtime=0) keeps the whole asset byte-identical across hosts and
    # Python versions (tarfile's own mtime kwarg is 3.12+ only).
    with asset.open('wb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw,
                           compresslevel=9, mtime=ARCHIVE_MTIME) as gz:
            with tarfile.open(fileobj=gz, mode='w') as bundle:
                with binary.open('rb') as source:
                    bundle.addfile(info, source)
    digest = _sha256_file(asset)
    sidecar = asset.with_name(asset.name + '.sha256')
    sidecar.write_text(f'{digest}  {asset.name}\n', encoding='ascii')
    return asset, sidecar


def _append_sums(sums_out: Path, assets: list[Path]) -> None:
    lines = ''.join(f'{_sha256_file(asset)}  {asset.name}\n' for asset in assets)
    with sums_out.open('a', encoding='ascii') as manifest:
        manifest.write(lines)


def _upload(tag: str, assets: list[Path]) -> None:
    gh = shutil.which('gh')
    if gh is None:
        raise BuildError('--upload needs the GitHub CLI (`gh`) on PATH')
    result = _run(
        [gh, 'release', 'upload', tag, *[str(asset) for asset in assets], '--clobber'],
        timeout=1800)
    if result.returncode != 0:
        raise BuildError(f'gh release upload {tag} failed (see output above)')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--target', action='append', choices=KNOWN_TRIPLES,
                        help='rust triple to build (repeatable); default: the '
                             'host-appropriate static triple')
    parser.add_argument('--version', help='asset version (default: repo VERSION file)')
    parser.add_argument('--out-dir', type=Path, default=DEFAULT_OUT_DIR,
                        help=f'asset output directory (default: {DEFAULT_OUT_DIR})')
    parser.add_argument('--sums-out', type=Path,
                        help='also append the assets to this SHA256SUMS manifest')
    parser.add_argument('--upload', metavar='TAG',
                        help='upload the assets to this GitHub release tag via `gh`')
    parser.add_argument('--no-smoke', action='store_true',
                        help='skip the no-arg smoke run even when the host can execute the build')
    arguments = parser.parse_args(argv)
    try:
        version = arguments.version
        if version is None:
            version = (REPO_ROOT / 'VERSION').read_text(encoding='ascii').strip()
            if not version:
                raise BuildError('repo VERSION file is empty; pass --version')
        triples = arguments.target or [_host_static_triple()]
        produced: list[Path] = []
        for triple in triples:
            binary = build_engine(triple)
            if arguments.no_smoke or not _host_executes(triple):
                if not arguments.no_smoke:
                    print(f'build_tofudb_prebuilt: skipping smoke for foreign target {triple}')
            else:
                _smoke(binary)
            asset, sidecar = package_asset(binary, version, triple, arguments.out_dir)
            produced += [asset, sidecar]
            print(f'build_tofudb_prebuilt: {asset} (+ {sidecar.name})')
        if arguments.sums_out:
            _append_sums(arguments.sums_out, [p for p in produced if p.suffix != '.sha256'])
            print(f'build_tofudb_prebuilt: appended to {arguments.sums_out}')
        if arguments.upload:
            _upload(arguments.upload, produced)
            print(f'build_tofudb_prebuilt: uploaded to release {arguments.upload}')
    except (BuildError, OSError) as exc:
        print(f'build_tofudb_prebuilt: FAILED: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
