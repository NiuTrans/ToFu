"""Pre-import server entry: hand human launches to the lifecycle manager.

Called only by the executable server.py wrapper. Standard-library imports
plus serverctl keep application configuration and storage out of this path.
Managed, bootstrap, container and in-place re-exec workers continue directly.
"""

from __future__ import annotations

import json
import os
import sys

_PROJ_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def delegate_server_start(server_args: list[str]) -> None:
    """Fast-path a human ``python server.py`` into the sole lifecycle owner."""
    external_owner = (
        os.environ.get('TOFU_SERVER_WORKER') == '1'
        or os.environ.get('_TOFU_VIA_BOOTSTRAP') == '1'
        or os.environ.get('TOFU_RUN_SERVER') == '1'
        # In-place update/HEAD re-exec keeps the original worker PID and sets
        # this port handoff marker before execv. It is already the worker; a
        # manager handoff here would strand that PID holding the instance lock
        # while it waits for itself to become ready.
        or bool(os.environ.get('_TOFU_REEXEC_PORT'))
        or os.getpid() == 1  # Docker/container entrypoint owns this process
    )
    if external_owner:
        return
    if '--version' in server_args:
        try:
            with open(os.path.join(_PROJ_DIR, 'VERSION'),
                      encoding='utf-8') as version_file:
                version = version_file.read().strip() or 'unknown'
        except OSError:
            version = 'unknown'
        sys.stdout.write(f'Tofu {version}\n')
        raise SystemExit(0)
    if any(arg in ('-h', '--help') for arg in server_args):
        sys.stdout.write(
            'usage: python server.py [--host HOST] [--port PORT] [--no-tls] '
            '[--certfile FILE --keyfile FILE]\n\n'
            'Starts Tofu through the project-local manager.\n'
            'Lifecycle operations: python serverctl.py --help\n'
            'Start diagnostics   : python serverctl.py doctor\n')
        raise SystemExit(0)

    # Preserve the established environment-selection contract without loading
    # the application first. The re-executed wrapper will immediately hand off
    # to serverctl; the eventual worker still runs the full native-path setup.
    marker = os.path.join(_PROJ_DIR, '.tofu_env.json')
    try:
        with open(marker, encoding='utf-8') as fh:
            cfg = json.load(fh)
        target = cfg.get('python') or ''
        prefix = cfg.get('env_prefix') or ''
        in_target = (
            os.path.realpath(sys.prefix) == os.path.realpath(prefix)
            if prefix else os.path.realpath(sys.executable) == os.path.realpath(target)
        )
        if target and os.access(target, os.X_OK) and not in_target \
                and os.environ.get('_TOFU_ENV_REEXEC') != '1':
            os.environ['_TOFU_ENV_REEXEC'] = '1'
            os.execv(target, [target, *sys.argv])
    except (OSError, ValueError, TypeError):
        pass  # An absent/invalid optional env marker leaves manager startup available.

    try:
        from serverctl import managed_start
        code = managed_start(server_args, wait=180.0, source='python-server.py')
    except Exception as exc:
        sys.stderr.write(
            '[server.py] Could not hand startup to the Tofu manager: %s\n'
            'Diagnose with: %s serverctl.py doctor\n' % (exc, sys.executable))
        code = 1
    raise SystemExit(code)


def load_early_resource_environment() -> None:
    """Expose project resource/data knobs before native pools are imported."""
    from tofu_dotenv import read_dotenv_values

    os.environ.setdefault('TOFU_PROJECT_PATH', _PROJ_DIR)
    try:
        values = read_dotenv_values(os.path.join(_PROJ_DIR, '.env'))
    except OSError:
        # The canonical loader below emits the established actionable startup
        # error. Early policy installation must not replace it with a traceback.
        return
    from runtime_guards import RESOURCE_BUDGET_ENV_KEYS
    early_names = RESOURCE_BUDGET_ENV_KEYS | {
        'TOFU_DEPLOYMENT_MODE',
        'TOFU_DATA_DIR',
        'TOFU_DATA_LAYOUT',
        'XDG_DATA_HOME',
        'LOCALAPPDATA',
    }
    for name in early_names:
        if name not in os.environ and name in values:
            os.environ[name] = values[name]
