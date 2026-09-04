"""lib/local_serve/_env.py — Isolated per-engine installation.

Engines install into ``data/local_serve/`` — never into Tofu's own venv and
never system-wide:

    data/local_serve/
      tools/uv-venv/        bootstrapped uv (pip-installed when no uv on PATH)
      envs/vllm/            uv venv with vllm==0.28.* (--torch-backend auto)
      envs/sglang/          uv venv with sglang[all]==0.5.*
      envs/ollama/          official linux tarball (only when no ollama on PATH)
      envs/llamacpp/        official GitHub release zip (ubuntu-x64)
      logs/                 managed server logs (owned by _process.py)
      tmp/                  modelfiles and other launch scratch

Budget
------
Two prechecks run before any byte is downloaded:

* free space on the data volume must cover the engine's
  ``ENGINE_SPECS[engine]['disk_need_bytes']`` (with 20% slack);
* total bytes under ``data/local_serve/`` must stay within
  ``TOFU_LOCAL_SERVE_BUDGET_GB`` (default **20 GiB** — owner-ratified).

Both failures are structured dicts the agent relays verbatim; nothing here
raises on a full disk.

Testability: every subprocess goes through an injectable ``runner``
(``subprocess.run`` signature) and every download through an injectable
``fetcher`` — the unit tests never touch the network or a real venv.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import urllib.request
import zipfile

from lib.log import get_logger
from lib.runtime_paths import data_root

from ._plan import ENGINE_SPECS

logger = get_logger(__name__)

__all__ = ['check_disk_budget', 'engine_status', 'ensure_engine',
           'resolve_launcher', 'serve_root']

_OLLAMA_TARBALL_URL = ('https://github.com/ollama/ollama/releases/latest/'
                       'download/ollama-linux-amd64.tar.gz')
_LLAMACPP_LATEST_API = ('https://api.github.com/repos/ggml-org/llama.cpp/'
                        'releases/latest')
_LLAMACPP_ASSET_SUBSTR = ('-bin-ubuntu-x64', '.zip')

_DOWNLOAD_CAP_BYTES = 4 * (1 << 30)
_DOWNLOAD_TIMEOUT = 1800
_UV_INSTALL_TIMEOUT = 600
_ENGINE_INSTALL_TIMEOUT = 5400   # vllm+torch is a multi-GB resolve; keep a ceiling

_UV_ENV_NAMES = {'vllm', 'sglang'}


def _budget_bytes() -> int:
    try:
        gb = float(os.environ.get('TOFU_LOCAL_SERVE_BUDGET_GB', '20'))
    except (TypeError, ValueError):
        gb = 20.0
    return max(1, int(gb)) * (1 << 30)


def serve_root() -> str:
    root = os.path.join(data_root(), 'local_serve')
    os.makedirs(root, exist_ok=True)
    return root


def _tree_bytes(path: str) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return total


def check_disk_budget(engine: str, *, disk_free: int | None = None) -> dict:
    """Precheck one engine's install against free space and the serve budget."""
    spec = ENGINE_SPECS.get(engine)
    if spec is None:
        return {'ok': False, 'error': '未知引擎: %s' % engine}
    need = spec['disk_need_bytes']
    if disk_free is None:
        try:
            disk_free = shutil.disk_usage(serve_root()).free
        except OSError as e:
            return {'ok': False, 'error': '无法读取磁盘余量: %s' % e}
    if disk_free < need * 1.2:
        return {'ok': False,
                'error': '磁盘余量不足：%s 安装约需 %.1f GiB，当前可用 %.1f GiB'
                         % (spec['display'], need / (1 << 30),
                            disk_free / (1 << 30))}
    used = _tree_bytes(serve_root())
    budget = _budget_bytes()
    if used + need > budget:
        return {'ok': False,
                'error': '本地部署目录 %.1f GiB 将超出预算 %.1f GiB'
                         '（TOFU_LOCAL_SERVE_BUDGET_GB 可调）；'
                         '可先删除不再使用的引擎环境'
                         % ((used + need) / (1 << 30), budget / (1 << 30))}
    return {'ok': True, 'need_bytes': need, 'free_bytes': disk_free,
            'used_bytes': used, 'budget_bytes': budget}


# ─────────────────────────── helpers ───────────────────────────

def _fetch(url: str, dest: str, timeout: int = _DOWNLOAD_TIMEOUT) -> None:
    """Stream one URL to disk with a hard size cap. Raises on failure."""
    req = urllib.request.Request(url, headers={'User-Agent': 'tofu-local-serve'})
    with urllib.request.urlopen(req, timeout=timeout) as resp, \
            open(dest, 'wb') as out:
        total = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > _DOWNLOAD_CAP_BYTES:
                out.close()
                os.unlink(dest)
                raise RuntimeError('下载超过 %d GiB 上限，已中止'
                                   % (_DOWNLOAD_CAP_BYTES // (1 << 30)))
            out.write(chunk)


def _fetch_json(url: str, timeout: int = 30) -> dict:
    import json as _json
    req = urllib.request.Request(url, headers={'User-Agent': 'tofu-local-serve'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _json.loads(resp.read().decode('utf-8'))


def _ensure_uv(runner) -> str | None:
    """Path to a usable uv binary; bootstraps a pip venv when PATH has none."""
    uv = shutil.which('uv')
    if uv:
        return uv
    tools = os.path.join(serve_root(), 'tools', 'uv-venv')
    candidate = os.path.join(tools, 'bin', 'uv')
    if os.path.isfile(candidate):
        return candidate
    try:
        runner(['python3', '-m', 'venv', tools],
               check=True, capture_output=True, text=True,
               timeout=_UV_INSTALL_TIMEOUT)
        runner([os.path.join(tools, 'bin', 'pip'), 'install', 'uv'],
               check=True, capture_output=True, text=True,
               timeout=_UV_INSTALL_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning('[LocalServe] uv bootstrap failed: %s', e)
        return None
    return candidate if os.path.isfile(candidate) else None


# ─────────────────────────── per-engine installers ───────────────────────────

def _engine_dir(engine: str) -> str:
    return os.path.join(serve_root(), 'envs', engine)


def _install_uv_env(engine: str, runner, log) -> dict:
    spec = ENGINE_SPECS[engine]
    uv = _ensure_uv(runner)
    if not uv:
        return {'ok': False,
                'error': '无法准备 uv 安装器（PATH 无 uv，且自动引导失败）；'
                         '请手动安装 uv 后重试'}
    env_dir = _engine_dir(engine)
    python = os.path.join(env_dir, 'bin', 'python')
    if not os.path.isfile(python):
        proc = runner([uv, 'venv', env_dir],
                      capture_output=True, text=True, timeout=_UV_INSTALL_TIMEOUT)
        if proc.returncode != 0:
            return {'ok': False,
                    'error': '创建隔离环境失败: %s' % (proc.stderr or '')[-400:]}
    cmd = [uv, 'pip', 'install', '--python', python] + spec['packages']
    if spec['uv_args']:
        cmd += spec['uv_args']
    if log:
        log('运行: %s' % ' '.join(cmd))
    proc = runner(cmd, capture_output=True, text=True,
                  timeout=_ENGINE_INSTALL_TIMEOUT)
    if proc.returncode != 0:
        return {'ok': False,
                'error': '%s 安装失败: %s'
                         % (spec['display'], (proc.stderr or '')[-600:])}
    return {'ok': True, 'env_dir': env_dir}


def _install_ollama(runner, fetcher, log) -> dict:
    found = shutil.which('ollama')
    if found:
        return {'ok': True, 'env_dir': None, 'binary': found, 'system': True}
    env_dir = _engine_dir('ollama')
    binary = os.path.join(env_dir, 'bin', 'ollama')
    if not os.path.isfile(binary):
        tarball = os.path.join(serve_root(), 'tmp', 'ollama-linux-amd64.tar.gz')
        os.makedirs(os.path.dirname(tarball), exist_ok=True)
        if log:
            log('下载 Ollama 官方压缩包…')
        fetcher(_OLLAMA_TARBALL_URL, tarball)
        import tarfile
        os.makedirs(env_dir, exist_ok=True)
        with tarfile.open(tarball, 'r:gz') as tf:
            tf.extractall(env_dir, filter='data')
        os.unlink(tarball)
    if not os.path.isfile(binary):
        return {'ok': False, 'error': 'Ollama 解包后未找到 bin/ollama'}
    os.chmod(binary, os.stat(binary).st_mode | stat.S_IEXEC)
    return {'ok': True, 'env_dir': env_dir, 'binary': binary}


def _install_llamacpp(runner, fetcher, log) -> dict:
    found = shutil.which('llama-server')
    env_dir = _engine_dir('llamacpp')
    binary = None if found else _find_llama_server(env_dir)
    if not found and not binary:
        if log:
            log('查询 llama.cpp 最新发布…')
        try:
            meta = _fetch_json(_LLAMACPP_LATEST_API)
        except Exception as exc:
            logger.debug(
                '[LocalServe] llama.cpp release lookup failed: %s',
                exc,
                exc_info=True,
            )
            return {
                'ok': False,
                'error': '无法查询 llama.cpp 发布信息: %s' % exc,
            }
        asset_url = None
        for asset in meta.get('assets') or []:
            name = asset.get('name', '')
            if all(s in name for s in _LLAMACPP_ASSET_SUBSTR):
                asset_url = asset.get('browser_download_url')
                break
        if not asset_url:
            return {'ok': False,
                    'error': '最新发布中没有 ubuntu-x64 预编译包；'
                             '请手动安装 llama.cpp 后重试'}
        zpath = os.path.join(serve_root(), 'tmp', 'llamacpp.zip')
        os.makedirs(os.path.dirname(zpath), exist_ok=True)
        if log:
            log('下载 %s …' % asset_url.rsplit('/', 1)[-1])
        fetcher(asset_url, zpath)
        os.makedirs(env_dir, exist_ok=True)
        with zipfile.ZipFile(zpath) as zf:
            zf.extractall(env_dir)
        os.unlink(zpath)
        binary = _find_llama_server(env_dir)
    binary = found or binary
    if not binary:
        return {'ok': False, 'error': 'llama.cpp 解包后未找到 llama-server'}
    os.chmod(binary, os.stat(binary).st_mode | stat.S_IEXEC)
    return {'ok': True, 'env_dir': env_dir, 'binary': binary}


def _find_llama_server(env_dir: str) -> str | None:
    if not os.path.isdir(env_dir):
        return None
    for dirpath, _dirnames, filenames in os.walk(env_dir):
        if 'llama-server' in filenames:
            return os.path.join(dirpath, 'llama-server')
    return None


# ─────────────────────────── public API ───────────────────────────

def engine_status(engine: str) -> dict:
    """``{installed, binary}`` without touching the network."""
    spec = ENGINE_SPECS.get(engine)
    if spec is None:
        return {'installed': False, 'error': '未知引擎: %s' % engine}
    if engine in _UV_ENV_NAMES:
        env_dir = _engine_dir(engine)
        exe = ('vllm' if engine == 'vllm' else 'python')
        binary = os.path.join(env_dir, 'bin', exe)
        return {'installed': os.path.isfile(binary), 'binary': binary,
                'system': False}
    if engine == 'ollama':
        found = shutil.which('ollama')
        binary = found or os.path.join(_engine_dir('ollama'), 'bin', 'ollama')
        return {'installed': bool(found) or os.path.isfile(binary),
                'binary': binary, 'system': bool(found)}
    found = shutil.which('llama-server')
    binary = found or _find_llama_server(_engine_dir('llamacpp'))
    return {'installed': bool(binary), 'binary': binary, 'system': bool(found)}


def ensure_engine(engine: str, *, log=None, runner=None, fetcher=None) -> dict:
    """Install *engine* when absent; returns ``{ok, error?, ...}``.

    The disk budget is checked BEFORE any install, and an already-installed
    engine short-circuits — this function is safe to call on every launch.
    """
    spec = ENGINE_SPECS.get(engine)
    if spec is None:
        return {'ok': False, 'error': '未知引擎: %s' % engine}
    status = engine_status(engine)
    if status.get('installed'):
        return {'ok': True, 'installed': False, 'binary': status['binary'],
                'system': status.get('system', False)}
    budget = check_disk_budget(engine)
    if not budget['ok']:
        return budget
    runner = runner or subprocess.run
    fetcher = fetcher or _fetch
    try:
        if engine in _UV_ENV_NAMES:
            result = _install_uv_env(engine, runner, log)
        elif engine == 'ollama':
            result = _install_ollama(runner, fetcher, log)
        else:
            result = _install_llamacpp(runner, fetcher, log)
    except (OSError, subprocess.SubprocessError, RuntimeError) as e:
        logger.warning('[LocalServe] install of %s failed: %s', engine, e)
        return {'ok': False, 'error': '%s 安装失败: %s' % (spec['display'], e)}
    if not result.get('ok'):
        return result
    status = engine_status(engine)
    if not status.get('installed'):
        return {'ok': False,
                'error': '%s 安装后仍找不到可执行文件' % spec['display']}
    result['installed'] = True
    result['binary'] = status['binary']
    result.setdefault('system', status.get('system', False))
    return result


def resolve_launcher(engine: str, argv: list) -> list:
    """Rewrite ``argv[0]`` to the installed binary; extra env for llama.cpp libs."""
    status = engine_status(engine)
    binary = status.get('binary')
    if not binary:
        return list(argv)
    out = [binary] + list(argv[1:])
    return out


def launcher_env(engine: str, base_env: dict) -> dict:
    """Per-engine process env tweaks (llama.cpp ships its shared libs)."""
    env = dict(base_env)
    if engine == 'llamacpp':
        status = engine_status(engine)
        binary = status.get('binary') or ''
        lib_dir = os.path.dirname(binary)
        if lib_dir and not status.get('system'):
            prev = env.get('LD_LIBRARY_PATH', '')
            env['LD_LIBRARY_PATH'] = lib_dir + (':' + prev if prev else '')
    return env
