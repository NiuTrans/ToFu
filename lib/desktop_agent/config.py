"""Desktop Agent — local config persistence.

Tiny JSON store for agent-local settings that must survive restarts —
currently the stable ``agent_id`` (RWA P0 registration frame, see
docs/modules/remote_execution.md). Path: ``TOFU_DESKTOP_CONFIG`` env
var, else ``~/.tofu/desktop_agent.json``.
"""

import copy
import os

from lib.json_store import JsonStoreReadError, read_json, update_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

_DEFAULT_CONFIG = {
    'agent_id': '',  # generated on first start by _run._ensure_agent_id
    # RWA P1: [{name, path}] — the agent's declared project worktrees;
    # project_* commands are confined to these roots (constraint ⑤).
    'share_roots': [],
    # Remote attachment: {url, secret} written by the tray's
    # "Connect to remote Tofu…" dialog. EMPTY means "poll my own loopback
    # server", which is the packaged app's default and must stay that way —
    # a tray user never touches this.
    'remote_server': {},
    # Route candidates persisted from the imported attach bundle
    # (desktop/connect_ui.py::import_attach_bundle): resume_attachment
    # re-walks them when the saved address dies, before the discovery
    # ladder.
    'attach_candidates': [],
    # Tray-persisted computer-control state: {enabled: bool, perms: {...}}.
    # ABSENT (empty dict) = the user never chose = fresh-install default
    # OFF — deny-by-default is preserved; only an explicit user choice is
    # ever restored.
    'computer_control': {},
}


class ConnectLineError(ValueError):
    """A parse_connect_line refusal, CODED for the UI boundary.

    The desktop dialog maps ``code`` to a bilingual message
    (``desktop._tk_theme.connect_error_text``) — a lib module must not own
    user-facing prose, and an English-only sentence in a Chinese dialog is
    exactly the leak the 2026-08-04 i18n sweep was ordered to kill. str()
    stays a non-empty, secret-free token (``connect_line:<code>[:<detail>]``)
    so refusals remain greppable in logs and the contract suite's「refusal
    carries a message / never echoes the secret」pins keep holding.
    """

    def __init__(self, code, detail=''):
        self.code = code
        self.detail = detail
        super().__init__('connect_line:%s%s'
                         % (code, (':' + detail) if detail else ''))


def parse_connect_line(line):
    """Parse a legacy connect line → ``(url, secret)``.

    Current downloads receive both values inside the personalized installer;
    this remains the single owner of the old ``<url> <token>`` format so an
    already-shipped client can still complete a manual repair.

    Deliberately whitespace-tolerant rather than pinned to a specific
    separator: the string makes a round trip through a clipboard, a terminal
    and possibly a chat window, any of which may re-wrap it or collapse runs
    of spaces. Splitting on arbitrary whitespace absorbs that without asking
    the user to notice. Surrounding whitespace and a trailing slash on the URL
    are also normalised, since both are common paste artefacts.

    Raises:
        ConnectLineError: coded refusal (``missing_parts`` /
            ``too_many_parts`` / ``bad_url``) — the dialog localises it;
            ``detail`` carries at most the URL half, never the secret.
    """
    parts = (line or '').split()
    if len(parts) < 2:
        raise ConnectLineError('missing_parts')
    if len(parts) > 2:
        raise ConnectLineError('too_many_parts')
    url, secret = parts[0].strip(), parts[1].strip()
    if not url.startswith(('http://', 'https://')):
        raise ConnectLineError('bad_url', detail=url[:40])
    return url.rstrip('/'), secret


def remote_server():
    """Return the configured ``(url, secret)``, or ``('', '')`` when unset.

    ``('', '')`` is the signal to fall back to the local loopback server, so
    an unconfigured tray app behaves exactly as it did before this existed.
    """
    cfg = load_config()
    rs = cfg.get('remote_server')
    if not isinstance(rs, dict):
        return '', ''
    return (rs.get('url') or '').strip(), (rs.get('secret') or '').strip()


def save_remote_server(url, secret):
    """Persist the remote attachment so it survives an app restart.

    Lives in the SAME file as ``agent_id`` / ``share_roots``
    (``~/.tofu/desktop_agent.json``) — that file already exists precisely for
    agent-local settings that must outlive the process, so this needs no new
    storage location. Passing an empty url clears the attachment and returns
    the agent to its local server.
    """
    return save_attachment(url, secret)


def save_attachment(url, secret, *, attach_candidates=None):
    """Atomically persist the active route and optional fallback candidates."""
    remote = ({'url': url.strip().rstrip('/'),
               'secret': (secret or '').strip()}
              if (url or '').strip() else {})

    def _mutate(cfg):
        cfg['remote_server'] = remote
        if attach_candidates is not None:
            cfg['attach_candidates'] = list(attach_candidates)
        return cfg

    _update_config(_mutate)
    return remote


def load_computer_control():
    """Return ``(enabled, perms)`` for the tray's launch-time restore.

    ``(False, {})`` when the user never toggled anything (or the blob is
    malformed) — a fresh install must come up deny-by-default. ``perms``
    carries only the canonical tier keys that were actually persisted;
    the caller merges it over its own deny-all baseline so tiers added
    later still default OFF for old config files.
    """
    cfg = load_config()
    cc = cfg.get('computer_control')
    if not isinstance(cc, dict):
        return False, {}
    raw = cc.get('perms')
    perms = {}
    if isinstance(raw, dict):
        from lib.desktop_agent._permissions import PERMISSION_KEYS
        perms = {k: bool(raw[k]) for k in PERMISSION_KEYS if k in raw}
    return bool(cc.get('enabled')), perms


def save_computer_control(enabled, perms):
    """Persist the tray's computer-control state so it survives restarts.

    Lives in the same agent config file as ``agent_id`` / ``remote_server``.
    Only ever called from an explicit user action (the tray enable toggle
    or a permission-tier click) — never from startup/quit paths, so a
    crash or a normal Quit cannot erase the user's choice.
    """
    state = {
        'enabled': bool(enabled),
        'perms': {str(k): bool(v) for k, v in (perms or {}).items()},
    }

    def _mutate(cfg):
        cfg['computer_control'] = state
        return cfg

    _update_config(_mutate)
    return state


def config_path():
    return (os.environ.get('TOFU_DESKTOP_CONFIG')
            or os.path.expanduser('~/.tofu/desktop_agent.json'))


def merge_cli_roots(existing_roots, cli_specs):
    """Merge ``--root NAME=PATH`` specs into the persisted share_roots list.

    Pure logic (no I/O) so the CLI merge is unit-testable. The CLI wins on a
    name collision (path updated in place, list position kept); new names
    append in declaration order. ``~`` is expanded. Raises ValueError on a
    malformed spec (missing ``=`` / empty name / empty path).
    """
    roots = [dict(r) for r in (existing_roots or [])
             if isinstance(r, dict) and r.get('name')]
    by_name = {str(r['name']): r for r in roots}
    order = [str(r['name']) for r in roots]
    for spec in cli_specs or []:
        if not spec or '=' not in spec:
            raise ValueError(f'--root must be NAME=PATH, got {spec!r}')
        name, path = spec.split('=', 1)
        name = name.strip()
        path = os.path.expanduser(path.strip())
        if not name or not path:
            raise ValueError(f'--root must be NAME=PATH, got {spec!r}')
        if name in by_name:
            by_name[name]['path'] = path
        else:
            by_name[name] = {'name': name, 'path': path}
            order.append(name)
    return [by_name[n] for n in order]


def load_config():
    """Read the agent config, merged over defaults. Never raises."""
    cfg = dict(_DEFAULT_CONFIG)
    path = config_path()
    if os.path.exists(path):
        try:
            os.chmod(path, 0o600)
        except OSError as e:
            logger.warning('[Agent] could not restrict config permissions: %s', e)
    data = read_json(path, default=None)
    if isinstance(data, dict):
        cfg.update(data)
    return cfg


def save_config(cfg):
    """Atomically replace the agent config with an explicit full snapshot."""
    candidate = copy.deepcopy(cfg)
    return _update_config(lambda _current: candidate)


def update_config(mutator):
    """Public locked field-update primitive for agent lifecycle callers."""
    return _update_config(mutator)


def _update_config(mutator):
    """Run one strict read-modify-write transaction with private permissions."""
    path = config_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError as e:
            logger.warning('[Agent] could not restrict config directory: %s', e)

    def _mutate(current):
        if not isinstance(current, dict):
            raise JsonStoreReadError(
                f'desktop agent config is not a JSON object: {path}')
        cfg = copy.deepcopy(_DEFAULT_CONFIG)
        cfg.update(current)
        return mutator(cfg)

    saved = update_json_atomic(
        path, _mutate, default={}, strict=True, indent=2, mode=0o600)
    logger.debug('[Agent] config saved to %s', path)
    return saved
