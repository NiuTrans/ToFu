"""Desktop Agent — CLIProxyAPI sidecar supervisor (E4, subscription adapter).

Runs a pinned, hash-verified CLIProxyAPI binary as a loopback-only
subprocess: the upstream community maintains the 2026 cloaking arms race
(uTLS, billing headers, beta lists), subscription tokens never leave THIS
machine, and the Tofu server reaches the adapter exclusively through the
bridge's ``target='loopback'`` relay (lib/desktop_agent/_egress.py).

Owner-ratified distribution (docs/SUBSCRIPTION_RELAY_SCENARIOS_DESIGN.md
§4.4/O1): download on first ensure from GitHub Releases + version pin +
checksums.txt SHA-256 verification + weekly update check. The SERVER owns
policy (port / random api-key / management secret — it needs the api-key
to authenticate provider calls, so generating it agent-side would only add
an upload path); the agent persists it in ``adapter/policy.json`` so the
sidecar resumes across agent restarts without a fresh ensure.

Attack-surface rules (owner 2026-08-04): the adapter binds 127.0.0.1 ONLY,
the Management API gets a random secret-key, and every lifecycle command
is gated on ``--allow-egress``. Download URLs are host-pinned to GitHub.
"""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile

import requests

from lib.json_store import read_json, write_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

_GH_API = 'https://api.github.com/repos/router-for-me/CLIProxyAPI/releases'
_ALLOWED_DOWNLOAD_HOSTS = {'github.com', 'api.github.com', 'objects.githubusercontent.com'}
_DEFAULT_PORT = 8317
_HEALTH_TIMEOUT_S = 30
_UPDATE_INTERVAL_S = 7 * 86400
_UPDATE_JITTER_S = 86400
_RESTART_BACKOFF_CAP_S = 300
_MAX_ADAPTER_DOWNLOAD_BYTES = 256 * 1024 * 1024
_MAX_ADAPTER_ARCHIVE_MEMBERS = 10_000

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_proc_started_at = 0.0
_supervise_stop = threading.Event()
_supervise_thread: threading.Thread | None = None
_update_thread: threading.Thread | None = None
_last_update_check = 0.0
_update_available = ''


# ══════════════════════════════════════════════════════════
#  Paths & policy
# ══════════════════════════════════════════════════════════

def _adapter_root() -> str:
    from lib.desktop_agent.config import config_path
    root = os.path.join(os.path.dirname(config_path()), 'adapter')
    os.makedirs(root, exist_ok=True)
    return root


def _binary_path() -> str:
    name = 'cliproxyapi.exe' if sys.platform.startswith('win') else 'cliproxyapi'
    return os.path.join(_adapter_root(), name)


def _policy_path() -> str:
    return os.path.join(_adapter_root(), 'policy.json')


def _read_policy() -> dict:
    return read_json(_policy_path(), default={}) or {}


def _write_policy(policy: dict) -> None:
    write_json_atomic(_policy_path(), policy)


def _version_file() -> str:
    return os.path.join(_adapter_root(), 'version.txt')


def _installed_version() -> str:
    try:
        with open(_version_file()) as f:
            return f.read().strip()
    except OSError as e:
        logger.debug('[Adapter] version file unreadable: %s', e)
        return ''


def adapter_loopback_port() -> int:
    """The port the loopback egress whitelist accepts (0 = no adapter)."""
    try:
        return int(_read_policy().get('port') or 0)
    except (TypeError, ValueError) as e:
        logger.debug('[Adapter] policy port unparsable: %s', e)
        return 0


# ══════════════════════════════════════════════════════════
#  Download + verify (version pin + checksums.txt SHA-256)
# ══════════════════════════════════════════════════════════

def _gh_get(url: str, timeout: int = 20) -> requests.Response:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or '').lower()
    if host not in _ALLOWED_DOWNLOAD_HOSTS:
        raise ValueError(f'download host not allowed: {host}')
    from lib.desktop_agent._egress import _resolve_proxies
    resp = requests.get(url, timeout=timeout, proxies=_resolve_proxies('env'),
                        headers={'User-Agent': 'tofu-agent-adapter/1.0',
                                 'Accept': 'application/vnd.github+json'},
                        stream=True)
    resp.raise_for_status()
    return resp


def _resolve_release(version: str) -> tuple[str, list]:
    """('latest'|pin) → (tag_name, assets). Raises on lookup failure."""
    version = (version or 'latest').strip()
    if version in ('', 'latest'):
        url = f'{_GH_API}/latest'
    else:
        tag = version if version.startswith('v') else f'v{version}'
        url = f'{_GH_API}/tags/{tag}'
    data = _gh_get(url).json()
    return data.get('tag_name', ''), data.get('assets') or []


def _asset_for_platform(assets: list) -> tuple[str, str]:
    """Pick the release asset for this OS/arch → (name, download_url)."""
    machine = os.uname().machine.lower() if hasattr(os, 'uname') else 'amd64'
    if sys.platform.startswith('win'):
        arch = 'aarch64' if machine in ('arm64', 'aarch64') else 'amd64'
        want = f'windows_{arch}.zip'
    elif sys.platform == 'darwin':
        arch = 'aarch64' if machine in ('arm64', 'aarch64') else 'amd64'
        want = f'darwin_{arch}.tar.gz'
    else:
        arch = 'aarch64' if machine in ('arm64', 'aarch64') else 'amd64'
        want = f'linux_{arch}.tar.gz'
    for a in assets:
        name = a.get('name', '')
        if name.endswith(want) and 'no-plugin' not in name:
            return name, a.get('browser_download_url', '')
    raise ValueError(f'no release asset matching *{want}')


def _parse_checksums(text: str) -> dict:
    """goreleaser checksums.txt → {asset_name: sha256_hex}."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            out[parts[1]] = parts[0].lower()
    return out


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _safe_archive_name(name: str) -> bool:
    """Cross-platform traversal check for zip/tar member names."""
    import ntpath
    import posixpath

    portable = (name or '').replace('\\', '/')
    return bool(name and not posixpath.isabs(portable)
                and not ntpath.isabs(name)
                and '..' not in portable.split('/'))


def _is_adapter_binary(name: str) -> bool:
    base = os.path.basename(name.replace('\\', '/'))
    return base.lower().replace('-', '').startswith('cliproxyapi')


def _install_binary_stream(src, declared_size: int, dest_dir: str) -> str:
    """Copy one verified archive member and atomically replace the binary."""
    if declared_size <= 0 or declared_size > _MAX_ADAPTER_DOWNLOAD_BYTES:
        raise ValueError('adapter binary has an unsafe declared size')

    os.makedirs(dest_dir, exist_ok=True)
    dst = os.path.join(dest_dir,
                       'cliproxyapi.exe'
                       if sys.platform.startswith('win') else 'cliproxyapi')
    fd, staged = tempfile.mkstemp(prefix='.cliproxyapi-', dir=dest_dir)
    total = 0
    try:
        with os.fdopen(fd, 'wb') as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_ADAPTER_DOWNLOAD_BYTES:
                    raise ValueError('adapter binary exceeds the safe size limit')
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if total != declared_size:
            raise ValueError(
                f'adapter binary size mismatch: expected {declared_size}, got {total}')
        os.chmod(staged, 0o755)
        os.replace(staged, dst)
        return dst
    finally:
        try:
            os.unlink(staged)
        except FileNotFoundError as exc:
            logger.debug('[Adapter] staged binary already absent: %s', exc)


def _extract_binary(archive: str, dest_dir: str) -> str:
    """Install only the binary member; never materialise arbitrary paths."""
    if archive.endswith('.zip'):
        with zipfile.ZipFile(archive) as z:
            members = z.infolist()
            if len(members) > _MAX_ADAPTER_ARCHIVE_MEMBERS:
                raise ValueError('adapter archive has too many members')
            for member in members:
                if not _safe_archive_name(member.filename):
                    raise ValueError(
                        f'unsafe adapter archive path: {member.filename!r}')
                mode = (member.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise ValueError('adapter archive contains a symbolic link')
                if not member.is_dir() and _is_adapter_binary(member.filename):
                    with z.open(member, 'r') as src:
                        return _install_binary_stream(
                            src, member.file_size, dest_dir)
    else:
        with tarfile.open(archive, 'r:gz') as tf:
            for index, member in enumerate(tf, start=1):
                if index > _MAX_ADAPTER_ARCHIVE_MEMBERS:
                    raise ValueError('adapter archive has too many members')
                if not _safe_archive_name(member.name):
                    raise ValueError(
                        f'unsafe adapter archive path: {member.name!r}')
                if (member.issym() or member.islnk() or member.isdev()
                        or member.isfifo()):
                    raise ValueError('adapter archive contains a special member')
                if member.isfile() and _is_adapter_binary(member.name):
                    src = tf.extractfile(member)
                    if src is None:
                        raise ValueError('adapter binary member is unreadable')
                    with src:
                        return _install_binary_stream(src, member.size, dest_dir)
    raise ValueError('cliproxyapi binary not found in archive')


def install_binary(version: str) -> str:
    """Download + verify + install the pinned (or latest) release.

    Returns the installed tag. Raises on any verification failure — a bad
    download NEVER replaces a working binary (verify happens entirely in a
    temp dir before the swap).
    """
    tag, assets = _resolve_release(version)
    if not tag:
        raise ValueError('release lookup returned no tag_name')
    name, url = _asset_for_platform(assets)
    sums_asset = next((a for a in assets if a.get('name') == 'checksums.txt'),
                      None)
    if not sums_asset:
        raise ValueError('release carries no checksums.txt — refusing '
                         'unverifiable download')
    tmpdir = tempfile.mkdtemp(prefix='adapter-dl-')
    try:
        sums_text = _gh_get(sums_asset['browser_download_url']).text
        expected = _parse_checksums(sums_text).get(name)
        if not expected:
            raise ValueError(f'{name} not listed in checksums.txt')
        archive = os.path.join(tmpdir, name)
        logger.info('[Adapter] downloading %s (%s)', name, tag)
        with _gh_get(url, timeout=600) as resp, open(archive, 'wb') as f:
            try:
                content_len = int(
                    (getattr(resp, 'headers', {}) or {}).get('Content-Length') or 0)
            except (TypeError, ValueError) as exc:
                logger.debug('[Adapter] invalid Content-Length ignored: %s', exc)
                content_len = 0
            if content_len > _MAX_ADAPTER_DOWNLOAD_BYTES:
                raise ValueError('adapter archive exceeds the 256 MiB limit')
            downloaded = 0
            for chunk in resp.iter_content(1 << 20):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > _MAX_ADAPTER_DOWNLOAD_BYTES:
                    raise ValueError('adapter archive exceeds the 256 MiB limit')
                f.write(chunk)
        actual = _sha256_file(archive)
        if actual != expected:
            raise ValueError(
                f'SHA-256 mismatch for {name}: expected {expected}, got {actual}')
        dst = _extract_binary(archive, _adapter_root())
        with open(_version_file(), 'w') as f:
            f.write(tag)
        logger.info('[Adapter] installed %s → %s', tag, dst)
        return tag
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ══════════════════════════════════════════════════════════
#  Config + process lifecycle
# ══════════════════════════════════════════════════════════

def _write_config(policy: dict) -> str:
    """Write config.yaml — loopback-only, keyed, management locked."""
    root = _adapter_root()
    auth_dir = os.path.join(root, 'auth').replace('\\', '/')
    os.makedirs(auth_dir, exist_ok=True)
    cfg = os.path.join(root, 'config.yaml')
    content = (
        '# Written by tofu-agent (lib/desktop_agent/_adapter.py) — do not edit.\n'
        'host: "127.0.0.1"\n'
        f'port: {int(policy["port"])}\n'
        'tls:\n  enable: false\n  cert: ""\n  key: ""\n'
        'remote-management:\n'
        '  allow-remote: false\n'
        f'  secret-key: "{policy["mgmt_secret"]}"\n'
        '  disable-control-panel: true\n'
        f'auth-dir: "{auth_dir}"\n'
        'api-keys:\n'
        f'  - "{policy["api_key"]}"\n'
        'debug: false\n'
    )
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(content)
    try:
        os.chmod(cfg, 0o600)
    except OSError as e:
        logger.debug('[Adapter] chmod config failed: %s', e)
    return cfg


def _is_running() -> bool:
    return _proc is not None and _proc.poll() is None


def _spawn(policy: dict) -> None:
    global _proc, _proc_started_at
    cfg = os.path.join(_adapter_root(), 'config.yaml')
    log_path = os.path.join(_adapter_root(), 'adapter.log')
    logf = open(log_path, 'ab')
    kwargs = {}
    if sys.platform.startswith('win'):
        kwargs['creationflags'] = (getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                                   | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    else:
        kwargs['start_new_session'] = True
    _proc = subprocess.Popen(
        [_binary_path(), '--config', cfg],
        stdout=logf, stderr=subprocess.STDOUT,
        cwd=_adapter_root(), **kwargs)
    _proc_started_at = time.time()
    logger.info('[Adapter] spawned pid=%s port=%s', _proc.pid, policy.get('port'))


def _healthy(port: int, api_key: str, timeout_s: float = _HEALTH_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            resp = requests.get(
                f'http://127.0.0.1:{port}/v1/models',
                headers={'Authorization': f'Bearer {api_key}'}, timeout=3)
            if resp.status_code == 200:
                return True
        except requests.RequestException as e:
            logger.debug('[Adapter] health probe failed: %s', e)
        if not _is_running():
            return False
        time.sleep(0.5)
    return False


def _stop_proc() -> None:
    global _proc
    with _lock:
        proc, _proc = _proc, None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception as e:
            logger.debug('[Adapter] terminate failed, killing: %s', e)
            try:
                proc.kill()
            except Exception as e2:
                logger.warning('[Adapter] kill failed: %s', e2)
    logger.info('[Adapter] stopped')


def _supervise_loop() -> None:
    """Crash watch: restart with capped backoff while a policy is active."""
    backoff = 5
    while not _supervise_stop.wait(backoff):
        policy = _read_policy()
        if not policy.get('active'):
            continue
        if _is_running():
            backoff = 5
            continue
        logger.warning('[Adapter] sidecar died — restarting (backoff %ss)', backoff)
        try:
            _spawn(policy)
        except Exception as e:
            logger.error('[Adapter] respawn failed: %s', e, exc_info=True)
        backoff = min(backoff * 2, _RESTART_BACKOFF_CAP_S)


def _start_supervisor() -> None:
    global _supervise_thread
    if _supervise_thread and _supervise_thread.is_alive():
        return
    _supervise_stop.clear()
    _supervise_thread = threading.Thread(
        target=_supervise_loop, daemon=True, name='adapter-supervise')
    _supervise_thread.start()


def _update_loop() -> None:
    """Weekly (+jitter) release check — the arms-race follow-through the
    owner made a hard requirement: a pinned-forever binary would re-create
    the cloaking drift at the edge."""
    global _last_update_check, _update_available
    time.sleep(random.uniform(60, _UPDATE_JITTER_S))
    while not _supervise_stop.wait(_UPDATE_INTERVAL_S):
        policy = _read_policy()
        if not policy.get('active') or not policy.get('auto_update'):
            continue
        _last_update_check = time.time()
        try:
            tag, _assets = _resolve_release('latest')
            current = _installed_version()
            if tag and current and tag != current:
                _update_available = tag
                logger.info('[Adapter] update available: %s → %s, applying',
                            current, tag)
                install_binary('latest')
                _stop_proc()  # supervisor respawns on the new binary
                _update_available = ''
            else:
                logger.debug('[Adapter] update check: %s is current', current)
        except Exception as e:
            logger.warning('[Adapter] update check failed: %s', e)


def _start_update_thread() -> None:
    global _update_thread
    if _update_thread and _update_thread.is_alive():
        return
    _update_thread = threading.Thread(
        target=_update_loop, daemon=True, name='adapter-update')
    _update_thread.start()


def _pick_port(desired: int) -> int:
    """Desired port, or the next free one within +8 (multi-user machines —
    design O2). A port is 'free' when nothing answers connect_ex."""
    import socket
    for port in range(desired, desired + 8):
        with socket.socket() as s:
            if s.connect_ex(('127.0.0.1', port)) != 0:
                return port
    raise ValueError(f'no free loopback port in {desired}..{desired + 7}')


# ══════════════════════════════════════════════════════════
#  Bridge commands (gated on --allow-egress in _dispatch)
# ══════════════════════════════════════════════════════════

def _status_dict() -> dict:
    policy = _read_policy()
    running = _is_running()
    auth_dir = os.path.join(_adapter_root(), 'auth')
    try:
        accounts = len([f for f in os.listdir(auth_dir)
                        if f.endswith('.json')]) if os.path.isdir(auth_dir) else 0
    except OSError as e:
        logger.debug('[Adapter] auth dir unreadable: %s', e)
        accounts = 0
    return {
        'installed': os.path.isfile(_binary_path()),
        'running': running,
        'version': _installed_version(),
        'port': int(policy.get('port') or 0),
        'pid': _proc.pid if running else 0,
        'uptime_s': int(time.time() - _proc_started_at) if running else 0,
        'accounts': accounts,
        'auto_update': bool(policy.get('auto_update')),
        'active': bool(policy.get('active')),
        'last_update_check': _last_update_check,
        'update_available': _update_available,
    }


def cmd_adapter_ensure(params: dict) -> dict:
    """Idempotent bring-up: policy → binary (download+verify if needed) →
    config → spawn → health gate. LONG (first download ~20 MB) — the server
    sends it with a 600s TTL."""
    params = params or {}
    policy = _read_policy()
    policy.update({
        'port': int(params.get('port') or policy.get('port') or _DEFAULT_PORT),
        'api_key': params.get('api_key') or policy.get('api_key') or '',
        'mgmt_secret': (params.get('mgmt_secret')
                        or policy.get('mgmt_secret') or ''),
        'version': params.get('version') or policy.get('version') or 'latest',
        'auto_update': bool(params.get('auto_update', True)),
        'active': True,
    })
    if not policy['api_key'] or not policy['mgmt_secret']:
        return {'error': 'adapter policy incomplete (api_key/mgmt_secret)'}
    try:
        policy['port'] = _pick_port(policy['port']) if not _is_running() \
            else policy['port']
    except ValueError as e:
        logger.warning('[Adapter] invalid policy: %s', e)
        return {'error': str(e)}
    _write_policy(policy)

    binary_missing = not os.path.isfile(_binary_path())
    pin = policy['version']
    version_mismatch = (pin not in ('', 'latest')
                        and _installed_version() not in ('', pin, f'v{pin}'))
    if binary_missing or version_mismatch:
        try:
            install_binary(pin)
        except Exception as e:
            logger.error('[Adapter] binary install failed: %s', e, exc_info=True)
            return {'error': f'adapter download/verify failed: {e}'}
    _write_config(policy)
    if not _is_running():
        try:
            _spawn(policy)
        except Exception as e:
            logger.error('[Adapter] spawn failed: %s', e, exc_info=True)
            return {'error': f'adapter spawn failed: {e}'}
    _start_supervisor()
    _start_update_thread()
    if not _healthy(policy['port'], policy['api_key']):
        return {'error': 'adapter failed health check within '
                f'{_HEALTH_TIMEOUT_S}s — see adapter.log',
                **_status_dict()}
    return _status_dict()


def cmd_adapter_status(_params: dict) -> dict:
    return _status_dict()


def cmd_adapter_stop(_params: dict) -> dict:
    policy = _read_policy()
    policy['active'] = False
    _write_policy(policy)
    _stop_proc()
    return _status_dict()


def maybe_resume_adapter() -> None:
    """Agent-startup hook: a previously-ensured sidecar comes back without
    waiting for a fresh ensure (the subscription path must survive an agent
    reboot — same 'second day must not die' class as the tunnel resume)."""
    policy = _read_policy()
    if not policy.get('active'):
        return
    logger.info('[Adapter] resuming sidecar (port=%s)', policy.get('port'))
    try:
        if not os.path.isfile(_binary_path()):
            logger.warning('[Adapter] binary missing on resume — waiting for '
                           'next ensure')
            return
        _write_config(policy)
        if not _is_running():
            _spawn(policy)
        _start_supervisor()
        _start_update_thread()
    except Exception as e:
        logger.error('[Adapter] resume failed: %s', e, exc_info=True)
