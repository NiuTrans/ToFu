"""Desktop Agent — local config persistence.

Tiny JSON store for agent-local settings that must survive restarts —
currently the stable ``agent_id`` (RWA P0 registration frame, see
docs/REMOTE_WORKTREE_DESIGN.md §3.2). Path: ``TOFU_DESKTOP_CONFIG`` env
var, else ``~/.tofu/desktop_agent.json``.
"""

import os

from lib.json_store import read_json, write_json_atomic
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
    """Parse the connect line the web UI hands the user → ``(url, secret)``.

    THE single owner of this format. The remote setup flow is a closed loop:
    ``static/js/local-control.js::_lcConnectLine`` renders
    ``<server-url><whitespace><token>`` into a click-to-copy box, and the user
    pastes that ONE string here. Both halves are required — a token with no
    address is unusable because nothing on the user's machine knows which
    server to poll, which is exactly why they travel together.

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
    cfg = load_config()
    if (url or '').strip():
        cfg['remote_server'] = {'url': url.strip().rstrip('/'),
                                'secret': (secret or '').strip()}
    else:
        cfg['remote_server'] = {}
    save_config(cfg)
    return cfg['remote_server']


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
    cfg = load_config()
    cfg['computer_control'] = {
        'enabled': bool(enabled),
        'perms': {str(k): bool(v) for k, v in (perms or {}).items()},
    }
    save_config(cfg)
    return cfg['computer_control']


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
    data = read_json(config_path(), default=None)
    if isinstance(data, dict):
        cfg.update(data)
    return cfg


def save_config(cfg):
    """Persist the agent config atomically (creates the parent dir)."""
    path = config_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    write_json_atomic(path, cfg)
    logger.debug('[Agent] config saved to %s', path)
