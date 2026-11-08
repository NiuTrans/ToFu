"""Payload assembly (README §6): python, site-packages, Tofu tree, uv, launcher.

Entry points: ``PROFILES`` (ABI -> target triple, wheel platform, ELF
machine), ``assemble_payload`` (stages the payload on disk and returns the
staging directory plus its report). Host tools: the x86_64
python-build-standalone and uv from ``pins.json`` (they also serve the
x86_64 test payload), ``git``. Dependencies: stdlib.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tomllib

from .fetch import fetch_pinned

HOST_TRIPLE = 'x86_64-unknown-linux-gnu'
PYTHON_MINOR = '3.12'
GUEST_ROOT = '/opt/tofu'
RUNTIME_DIR = Path(__file__).resolve().parents[1]
LAUNCHER = RUNTIME_DIR / 'payload' / 'bin' / 'tofu-local-kernel'
COMPILE_SCRIPT = Path(__file__).resolve().parent / 'compile_pyc.py'
# Tracked trees no runtime code reads; README §6 explains each.
EXCLUDED_SOURCE_TREES = (
    '.github', '.vscode', 'android', 'benchmarks', 'designs', 'ios',
    'packages/tofu-db', 'packages/tofu-runtime', 'packages/vendor',
    'screenshots', 'tests',
)
# The API image's closure minus `scale` (README §6).
EXTRAS = ('app',)
# bin/uv is the release binary; the wheel would ship a second copy.
OMITTED_DISTRIBUTIONS = ('uv',)
DEFAULT_INDEX = 'https://pypi.org/simple'
TOFUDB_LIBRARY = 'libtofu_db.so'
PROXY_VARIABLES = ('http_proxy', 'https_proxy', 'no_proxy', 'HTTP_PROXY', 'HTTPS_PROXY',
                   'NO_PROXY', 'all_proxy', 'ALL_PROXY')


@dataclass(frozen=True)
class Profile:
    abi: str
    triple: str
    python_platform: str
    machine: str


PROFILES = {
    # Ubuntu 24.04 ships glibc 2.39.
    'arm64-v8a': Profile('arm64-v8a', 'aarch64-unknown-linux-gnu', 'aarch64-manylinux_2_39',
                         'aarch64'),
    # Host boot test only: glibc 2.35 is Ubuntu 22.04, the oldest supported build host.
    'x86_64': Profile('x86_64', 'x86_64-unknown-linux-gnu', 'x86_64-manylinux_2_35', 'x86_64'),
}


class PayloadError(RuntimeError):
    """The payload cannot be assembled from the given inputs."""


def run(command: list[str], *, env: dict | None = None, cwd: Path | None = None,
        capture: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(command, env=env, cwd=cwd, capture_output=capture, check=False)
    if result.returncode != 0:
        detail = ''
        if capture:
            detail = (result.stderr or b'').decode('utf-8', 'replace').strip()[-4000:]
        raise PayloadError(f'{" ".join(map(str, command))} exited {result.returncode}\n{detail}')
    return result


def tool_environment(home: Path, cache: Path) -> dict:
    """A clean environment for host tools: no inherited TOFU_*, UV_* or PYTHON* state."""
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'LANG': 'C.UTF-8',
           'UV_CACHE_DIR': str(cache / 'uv'), 'PYTHONHASHSEED': '0',
           'PYTHONDONTWRITEBYTECODE': '1'}
    env.update({name: os.environ[name] for name in PROXY_VARIABLES if name in os.environ})
    return env


def extract_tarball(tarball: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball) as archive:
        archive.extractall(destination, filter='tar')


def host_tools(pins: dict, cache: Path, work: Path) -> tuple[Path, Path]:
    """The x86_64 python and uv that run on the build host."""
    python_asset = pins['python_build_standalone']['assets'][HOST_TRIPLE]
    uv_asset = pins['uv']['assets'][HOST_TRIPLE]
    python_root = work / 'host-python'
    if not (python_root / 'python' / 'bin' / 'python3').exists():
        extract_tarball(fetch_pinned(python_asset['url'], python_asset['sha256'], None, cache),
                        python_root)
    uv_root = work / 'host-uv'
    if not (uv_root / f'uv-{HOST_TRIPLE}' / 'uv').exists():
        extract_tarball(fetch_pinned(uv_asset['url'], uv_asset['sha256'], None, cache), uv_root)
    return python_root / 'python' / 'bin' / 'python3', uv_root / f'uv-{HOST_TRIPLE}' / 'uv'


def resolve_commit(repo: Path, revision: str) -> str:
    out = run(['git', '-C', str(repo), 'rev-parse', '--verify', f'{revision}^{{commit}}'],
              capture=True).stdout
    return out.decode('ascii').strip()


def _excluded(path: str) -> bool:
    return any(path == tree or path.startswith(tree + '/') for tree in EXCLUDED_SOURCE_TREES)


def export_source(repo: Path, commit: str, destination: Path, work: Path) -> None:
    """The commit's tracked files (``git archive``) minus the excluded trees."""
    archive_path = work / 'source.tar'
    run(['git', '-C', str(repo), 'archive', '--format=tar', '-o', str(archive_path), commit])
    destination.mkdir(parents=True)
    with tarfile.open(archive_path) as archive:
        members = [member for member in archive.getmembers()
                   if member.name != 'pax_global_header' and not _excluded(member.name.rstrip('/'))]
        archive.extractall(destination, members=members, filter='tar')
    archive_path.unlink()


@dataclass
class Requirements:
    """``uv export`` output split by how each requirement can be installed."""
    wheels: list[str]
    sdists: list[str]
    sdist_names: list[str]
    workspace: list[str]


def split_requirements(exported: str, lock: dict) -> Requirements:
    """Hashed registry requirements with wheels, sdist-only ones, workspace paths.

    A package whose ``uv.lock`` entry lists no wheels can only come from its
    sdist. Workspace members are local paths and carry no hashes.
    """
    sdist_only = {package['name'] for package in lock['package']
                  if 'version' in package and not package.get('wheels') and 'sdist' in package}
    split = Requirements([], [], [], [])
    block: list[str] = []

    def flush() -> None:
        if not block:
            return
        head = block[0].strip()
        if head.startswith('./'):
            split.workspace.append(head.split(';', 1)[0].strip().rstrip('\\').strip())
        else:
            name = re.split(r'[=<>!~;\s\[]', head, maxsplit=1)[0].lower().replace('_', '-')
            if name in sdist_only:
                split.sdists.extend(block)
                split.sdist_names.append(name)
            else:
                split.wheels.extend(block)
        block.clear()

    for line in exported.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if not line[0].isspace():
            flush()
        block.append(line)
    flush()
    split.sdist_names.sort()
    return split


def build_constraints(lock: dict) -> str:
    """Pin the build backend of locally built sdists to uv.lock's setuptools and wheel."""
    rows = []
    for package in sorted(lock['package'], key=lambda package: package['name']):
        if package['name'] in ('setuptools', 'wheel') and package.get('wheels'):
            hashes = ' '.join(f'--hash={wheel["hash"]}' for wheel in package['wheels'])
            rows.append(f'{package["name"]}=={package["version"]} {hashes}')
    return '\n'.join(rows) + '\n'


def install_site_packages(*, uv: Path, host_python: Path, source: Path, site: Path,
                          profile: Profile, env: dict, work: Path) -> dict:
    lock = tomllib.loads((source / 'uv.lock').read_text(encoding='utf-8'))
    exported = work / 'requirements-export.txt'
    command = [str(uv), 'export', '--frozen', '--no-config', '--format', 'requirements-txt',
               '--no-dev', '--no-emit-project', '--no-editable', '--no-header', '--no-annotate',
               '-o', str(exported)]
    for extra in EXTRAS:
        command += ['--extra', extra]
    for distribution in OMITTED_DISTRIBUTIONS:
        command += ['--no-emit-package', distribution]
    run(command, env=env, cwd=source, capture=True)
    split = split_requirements(exported.read_text(encoding='utf-8'), lock)
    constraints = work / 'build-constraints.txt'
    constraints.write_text(build_constraints(lock), encoding='utf-8')
    common = [str(uv), 'pip', 'install', '--no-config', '--target', str(site),
              '--python', str(host_python), '--python-platform', profile.python_platform,
              '--no-deps', '--link-mode', 'copy', '--default-index', DEFAULT_INDEX]
    wheels = work / 'requirements-wheels.txt'
    wheels.write_text('\n'.join(split.wheels) + '\n', encoding='utf-8')
    run(common + ['--require-hashes', '--no-build', '-r', str(wheels)], env=env, capture=True)
    if split.sdists:
        sdists = work / 'requirements-sdists.txt'
        sdists.write_text('\n'.join(split.sdists) + '\n', encoding='utf-8')
        run(common + ['--require-hashes', '--build-constraints', str(constraints), '-r', str(sdists)],
            env=env, capture=True)
    if split.workspace:
        install_workspace_members(uv=uv, host_python=host_python, source=source,
                                  members=split.workspace, constraints=constraints,
                                  common=common, env=env, work=work)
    drop_build_host_metadata(site)
    return {
        'requirements_sha256': hashlib.sha256(exported.read_bytes()).hexdigest(),
        'built_from_sdist': split.sdist_names + [PurePosixPath(member).name
                                                 for member in split.workspace],
        'distributions': installed_distributions(site),
        'omitted': list(OMITTED_DISTRIBUTIONS),
    }


def drop_build_host_metadata(site: Path) -> None:
    """Remove what records the build host instead of the payload.

    Console scripts name the build host's interpreter in their shebang (Tofu
    runs modules), and uv's uv_cache.json records when a local wheel was
    installed. Both go, together with their RECORD rows, so RECORD still
    lists exactly the installed files and two builds stay byte-identical.
    """
    if (site / 'bin').exists():
        shutil.rmtree(site / 'bin')
    for record in sorted(site.glob('*.dist-info/RECORD')):
        cache = record.parent / 'uv_cache.json'
        if cache.exists():
            cache.unlink()
        dropped = (f'{record.parent.name}/uv_cache.json',)
        lines = record.read_text(encoding='utf-8').splitlines(keepends=True)
        kept = [line for line in lines
                if not line.startswith('bin/') and line.split(',', 1)[0] not in dropped]
        if kept != lines:
            record.write_text(''.join(kept), encoding='utf-8')


def install_workspace_members(*, uv: Path, host_python: Path, source: Path, members: list[str],
                              constraints: Path, common: list[str], env: dict, work: Path) -> None:
    """Build workspace members (pure Python) into wheels, then install those.

    Building from a copy keeps setuptools' build/ and egg-info out of the
    payload's source tree. Installing from a find-links wheelhouse rather
    than a path keeps the build directory out of direct_url.json.
    """
    wheelhouse = work / 'wheelhouse'
    wheelhouse.mkdir()
    for member in members:
        copy = work / 'workspace' / PurePosixPath(member).name
        shutil.copytree(source / member, copy)
        run([str(uv), 'build', '--no-config', '--wheel', '--out-dir', str(wheelhouse),
             '--build-constraints', str(constraints), '--python', str(host_python), str(copy)],
            env=env, capture=True)
    requirements = []
    for wheel in sorted(wheelhouse.glob('*.whl')):
        name, version = wheel.name.split('-')[:2]
        requirements.append(f'{name}=={version}')
    run(common + ['--no-index', '--find-links', str(wheelhouse), *requirements], env=env,
        capture=True)


def installed_distributions(site: Path) -> list[dict]:
    rows = []
    for metadata in sorted(site.glob('*.dist-info/WHEEL')):
        dist = metadata.parent.name[:-len('.dist-info')]
        name, _, version = dist.rpartition('-')
        tags = [line.split(':', 1)[1].strip() for line in metadata.read_text().splitlines()
                if line.startswith('Tag:')]
        pure = 'Root-Is-Purelib: true' in metadata.read_text()
        rows.append({'name': name, 'version': version, 'tags': tags, 'pure': pure})
    return rows


def require_pure(distributions: list[dict], names: list[str]) -> None:
    """An sdist built on the x86_64 host is usable only if it is pure Python."""
    wanted = {name.lower().replace('-', '_') for name in names}
    for row in distributions:
        if row['name'].lower().replace('-', '_') in wanted and not row['pure']:
            raise PayloadError(f'{row["name"]} has no wheel for the target and its sdist builds '
                               f'a native extension ({", ".join(row["tags"])})')


def install_frontend(dist: Path, tofu: Path, host_python: Path, env: dict, work: Path) -> dict:
    """Copy the prebuilt static/vite and validate it against this commit's sources."""
    target = tofu / 'static' / 'vite'
    if target.exists():
        raise PayloadError('the source tree already contains static/vite')
    shutil.copytree(dist, target, symlinks=False)
    validator_env = dict(env, TOFU_DATA_DIR=str(work / 'validator-data'))
    code = ('import json, sys\n'
            'sys.path.insert(0, ".")\n'
            'from lib import vite_assets\n'
            'manifest = vite_assets.validate_source_vite_artifact()\n'
            'main = manifest[vite_assets.VITE_ENTRY]\n'
            'print(json.dumps({"authoring_sha256": main[vite_assets.VITE_AUTHORING_DIGEST_FIELD],'
            ' "i18n_sha256": main[vite_assets.I18N_CATALOG_DIGEST_FIELD]}))\n')
    result = run([str(host_python), '-B', '-c', code], env=validator_env, cwd=tofu, capture=True)
    return json.loads(result.stdout.decode('utf-8').strip().splitlines()[-1])


def compile_bytecode(host_python: Path, stage: Path, roots: list[str], env: dict) -> list[str]:
    """unchecked-hash .pyc next to every .py; returns files that do not compile."""
    result = run([str(host_python), str(COMPILE_SCRIPT), str(stage), GUEST_ROOT, *roots],
                 env=env, capture=True)
    return json.loads(result.stdout.decode('utf-8'))


def remove_bytecode(root: Path) -> None:
    for cache in sorted(root.rglob('__pycache__'), reverse=True):
        shutil.rmtree(cache)
    for stray in root.rglob('*.pyc'):
        stray.unlink()


def assemble_payload(*, pins: dict, profile: Profile, repo: Path, commit: str,
                     frontend_dist: Path, tofudb: Path | None, cache: Path,
                     work: Path) -> tuple[Path, dict]:
    stage = work / 'payload'
    if stage.exists():
        shutil.rmtree(stage)
    home = work / 'home'
    home.mkdir(parents=True, exist_ok=True)
    env = tool_environment(home, cache)
    host_python, host_uv = host_tools(pins, cache, work)

    pbs = pins['python_build_standalone']
    python_asset = pbs['assets'][profile.triple]
    extract_tarball(fetch_pinned(python_asset['url'], python_asset['sha256'], None, cache), stage)
    remove_bytecode(stage / 'python')
    site = stage / 'python' / 'lib' / f'python{PYTHON_MINOR}' / 'site-packages'

    tofu = stage / 'tofu'
    export_source(repo, commit, tofu, work)
    if (tofu / '.python-version').read_text().strip() != PYTHON_MINOR or \
            not pbs['version'].startswith(PYTHON_MINOR + '.'):
        raise PayloadError(f'.python-version and pinned CPython {pbs["version"]} disagree')
    lock_uv = next((package['version'] for package in tomllib.loads(
        (tofu / 'uv.lock').read_text(encoding='utf-8'))['package'] if package['name'] == 'uv'), None)
    if lock_uv != pins['uv']['version']:
        raise PayloadError(f'uv.lock pins uv {lock_uv}, pins.json pins {pins["uv"]["version"]}; '
                           'run resolve')
    wheels = install_site_packages(uv=host_uv, host_python=host_python, source=tofu, site=site,
                                   profile=profile, env=env, work=work)
    require_pure(wheels['distributions'], wheels['built_from_sdist'])
    frontend = install_frontend(frontend_dist, tofu, host_python, env, work)

    bin_dir = stage / 'bin'
    bin_dir.mkdir()
    shutil.copyfile(LAUNCHER, bin_dir / 'tofu-local-kernel')
    (bin_dir / 'tofu-local-kernel').chmod(0o755)
    uv_asset = pins['uv']['assets'][profile.triple]
    uv_root = work / 'target-uv'
    if uv_root.exists():
        shutil.rmtree(uv_root)
    extract_tarball(fetch_pinned(uv_asset['url'], uv_asset['sha256'], None, cache), uv_root)
    for name in ('uv', 'uvx'):
        shutil.copyfile(uv_root / f'uv-{profile.triple}' / name, bin_dir / name)
        (bin_dir / name).chmod(0o755)
    tofudb_record = None
    if tofudb is not None:
        # The embedded transport loads libtofu_db.so from beside the engine
        # (lib/storage/supervisor.py _resolve_tofudb_embedded_engine).
        tofudb_record = {}
        for source, name in ((tofudb, 'tofu-db'), (tofudb.parent / TOFUDB_LIBRARY, TOFUDB_LIBRARY)):
            if not source.is_file():
                continue
            shutil.copyfile(source, bin_dir / name)
            (bin_dir / name).chmod(0o755)
            tofudb_record[name] = hashlib.sha256((bin_dir / name).read_bytes()).hexdigest()

    failures = compile_bytecode(host_python, stage, ['python/lib', 'tofu'], env)
    version = (tofu / 'VERSION').read_text(encoding='utf-8').strip()
    report = {
        'tofu': {'version': version, 'commit': commit},
        'python': {'version': pbs['version'], 'release': pbs['release'], 'triple': profile.triple},
        'uv': pins['uv']['version'],
        'python_platform': profile.python_platform,
        'wheels': wheels,
        'frontend': frontend,
        'tofudb': tofudb_record,
        'excluded_source_trees': list(EXCLUDED_SOURCE_TREES),
        'bytecode_failures': failures,
    }
    return stage, report
