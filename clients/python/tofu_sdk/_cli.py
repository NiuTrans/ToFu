"""tofu_sdk._cli — Command-line interface for the Tofu API.

Auth resolution order:
  1. ``--api-key`` / ``--base-url`` CLI flags
  2. ``TOFU_API_KEY`` / ``TOFU_BASE_URL`` env vars
  3. ``~/.tofu/config.toml``  (``[default]`` section)

Subcommands:

    tofu chat "prompt..." [--model M] [--stream] [--config k=v ...]
    tofu capabilities
    tofu keys list
    tofu keys create --name N --scope S [--scope S2 ...] [--rpm 60]
    tofu keys revoke <key_id>
    tofu tasks list [--kind K] [--status S]
    tofu tasks watch <task_id>
    tofu tasks abort <task_id>
    tofu agents translate <text> [--target zh]
    tofu agents fetch <url>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from . import Tofu, TofuError, __version__


def _resolve_config() -> tuple[str, str]:
    base = os.environ.get('TOFU_BASE_URL', '')
    key = os.environ.get('TOFU_API_KEY', '')
    cfg_path = os.path.expanduser('~/.tofu/config.toml')
    if os.path.isfile(cfg_path) and (not base or not key):
        try:
            try:
                import tomllib  # 3.11+
            except ImportError:
                import tomli as tomllib  # type: ignore
            with open(cfg_path, 'rb') as f:
                data = tomllib.load(f)
            sec = data.get('default', {})
            base = base or sec.get('base_url', '')
            key = key or sec.get('api_key', '')
        except Exception as e:
            print(f'[warn] could not read {cfg_path}: {e}', file=sys.stderr)
    return base, key


def _client(args) -> Tofu:
    base = args.base_url or _resolve_config()[0]
    key = args.api_key or _resolve_config()[1]
    if not base or not key:
        sys.stderr.write('error: --base-url and --api-key (or TOFU_BASE_URL/'
                         'TOFU_API_KEY env, or ~/.tofu/config.toml) are '
                         'required\n')
        sys.exit(2)
    return Tofu(base_url=base, api_key=key,
                 verify=not args.insecure)


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ── Subcommand handlers ───────────────────────────────────────────

def cmd_capabilities(args):
    _print(_client(args).capabilities())


def cmd_chat(args):
    cli = _client(args)
    cfg: dict = {}
    for kv in args.config or []:
        if '=' not in kv:
            sys.stderr.write(f'invalid --config {kv!r} (use k=v)\n')
            sys.exit(2)
        k, v = kv.split('=', 1)
        # try to parse as JSON for booleans/numbers
        try:
            cfg[k] = json.loads(v)
        except (ValueError, json.JSONDecodeError):
            cfg[k] = v
    messages = [{'role': 'user', 'content': args.prompt}]
    if args.system:
        messages.insert(0, {'role': 'system', 'content': args.system})
    if args.stream:
        for ev in cli.stream(messages=messages, model=args.model,
                              config=cfg or None):
            choice = (ev.get('choices') or [{}])[0]
            delta = choice.get('delta') or {}
            content = delta.get('content')
            if content:
                sys.stdout.write(content)
                sys.stdout.flush()
            if choice.get('finish_reason'):
                sys.stdout.write('\n')
                sys.stdout.flush()
    else:
        resp = cli.chat(messages=messages, model=args.model,
                         config=cfg or None,
                         max_tokens=args.max_tokens,
                         temperature=args.temperature)
        if args.json:
            _print(resp)
        else:
            for ch in resp.get('choices', []):
                msg = ch.get('message', {})
                if msg.get('reasoning_content') and args.show_thinking:
                    sys.stderr.write('=== thinking ===\n')
                    sys.stderr.write(msg['reasoning_content'])
                    sys.stderr.write('\n=== /thinking ===\n')
                print(msg.get('content', ''))


def cmd_keys_list(args):
    _print(_client(args).keys.list())


def cmd_keys_create(args):
    out = _client(args).keys.create(
        name=args.name, scopes=args.scope or [],
        rate_limit_rpm=args.rpm, rate_limit_tpd=args.tpd,
        admin=args.admin)
    _print(out)
    if 'token' in out:
        sys.stderr.write('\n*** Save this token NOW — it cannot be '
                         'recovered ***\n\n')


def cmd_keys_revoke(args):
    _print(_client(args).keys.revoke(args.key_id))


def cmd_keys_whoami(args):
    _print(_client(args).keys.whoami())


def cmd_tasks_list(args):
    _print(_client(args).tasks.list(kind=args.kind or '',
                                      status=args.status or ''))


def cmd_tasks_get(args):
    _print(_client(args).tasks.get(args.task_id))


def cmd_tasks_watch(args):
    cli = _client(args)
    for ev in cli.tasks.stream(args.task_id):
        _print(ev)
        if ev.get('type') in ('done', 'error', 'aborted'):
            break


def cmd_tasks_abort(args):
    _print(_client(args).tasks.abort(args.task_id))


def cmd_agents_translate(args):
    cli = _client(args)
    if args.wait:
        out = cli.agents.translate_and_wait(
            text=args.text, target_lang=args.target,
            timeout_s=args.timeout)
        if args.text_only:
            sys.stdout.write(out.get('translated', '') or '')
            sys.stdout.write('\n')
        else:
            _print(out)
    else:
        out = cli.agents.translate(text=args.text, target_lang=args.target)
        _print(out)


def cmd_agents_fetch(args):
    out = _client(args).agents.fetch(url=args.url)
    if args.text_only:
        sys.stdout.write(out.get('text', '') or '')
    else:
        _print(out)


def cmd_agents_search(args):
    cli = _client(args)
    if args.async_mode:
        out = cli.agents.search_async(query=args.query,
                                       max_results=args.max_results,
                                       freshness=args.freshness or '')
        _print(out)
        return
    out = cli.agents.search(query=args.query,
                             max_results=args.max_results,
                             freshness=args.freshness or '',
                             include_summary=args.summary)
    if args.json:
        _print(out)
        return
    # Pretty-print top results.
    results = out.get('results') or []
    for i, r in enumerate(results, 1):
        title = r.get('title', '').strip()
        url = r.get('url', '').strip()
        snippet = (r.get('snippet') or r.get('content') or '').strip()
        sys.stdout.write(f'{i}. {title}\n   {url}\n')
        if snippet:
            short = snippet[:200].replace('\n', ' ')
            sys.stdout.write(f'   {short}\n')
        sys.stdout.write('\n')
    summary = out.get('summary')
    if summary:
        sys.stdout.write('=== summary ===\n')
        sys.stdout.write(summary + '\n')


def cmd_run(args):
    """Start a task of any kind and stream its events."""
    cli = _client(args)
    params: dict = {}
    for kv in args.param or []:
        if '=' not in kv:
            sys.stderr.write(f'invalid --param {kv!r} (use k=v)\n')
            sys.exit(2)
        k, v = kv.split('=', 1)
        try:
            params[k] = json.loads(v)
        except (ValueError, json.JSONDecodeError):
            params[k] = v
    if args.wait:
        out = cli.tasks.run(kind=args.kind, params=params,
                             timeout_s=args.timeout)
        _print(out)
        return
    # Stream by default — most useful for long-running tasks.
    for ev in cli.tasks.start_and_stream(kind=args.kind, params=params):
        _print(ev)
        if ev.get('type') in ('done', 'error', 'aborted',
                                'message_stop'):
            break


# ── Argument parsing ──────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='tofu',
                                 description='Tofu API command-line client')
    p.add_argument('--base-url', default='',
                   help='Tofu server base URL (or env TOFU_BASE_URL)')
    p.add_argument('--api-key', default='',
                   help='Tofu API key (or env TOFU_API_KEY)')
    p.add_argument('--insecure', action='store_true',
                   help='Skip TLS verification (self-signed certs)')
    p.add_argument('--version', action='version',
                   version=f'tofu-sdk {__version__}')
    sub = p.add_subparsers(dest='cmd', required=True)

    # capabilities
    sub.add_parser('capabilities',
                   help='Show runtime model/tool/agent registry'
                   ).set_defaults(func=cmd_capabilities)

    # chat
    pchat = sub.add_parser('chat', help='Run a chat completion')
    pchat.add_argument('prompt')
    pchat.add_argument('--model', default='')
    pchat.add_argument('--system', default='')
    pchat.add_argument('--max-tokens', type=int, default=32768)
    pchat.add_argument('--temperature', type=float, default=1.0)
    pchat.add_argument('--stream', action='store_true')
    pchat.add_argument('--show-thinking', action='store_true')
    pchat.add_argument('--json', action='store_true',
                       help='Print raw JSON response')
    pchat.add_argument('--config', action='append', metavar='k=v',
                       help='Repeat for each key (e.g. --config thinkingDepth=high)')
    pchat.set_defaults(func=cmd_chat)

    # keys
    pkeys = sub.add_parser('keys', help='API key administration')
    sk = pkeys.add_subparsers(dest='subcmd', required=True)
    sk.add_parser('list').set_defaults(func=cmd_keys_list)
    sk.add_parser('whoami').set_defaults(func=cmd_keys_whoami)
    pcreate = sk.add_parser('create')
    pcreate.add_argument('--name', required=True)
    pcreate.add_argument('--scope', action='append', default=[])
    pcreate.add_argument('--rpm', type=int, default=60)
    pcreate.add_argument('--tpd', type=int, default=0)
    pcreate.add_argument('--admin', action='store_true')
    pcreate.set_defaults(func=cmd_keys_create)
    prev = sk.add_parser('revoke')
    prev.add_argument('key_id')
    prev.set_defaults(func=cmd_keys_revoke)

    # tasks
    ptasks = sub.add_parser('tasks', help='Task lifecycle')
    st = ptasks.add_subparsers(dest='subcmd', required=True)
    plst = st.add_parser('list')
    plst.add_argument('--kind', default='')
    plst.add_argument('--status', default='')
    plst.set_defaults(func=cmd_tasks_list)
    pget = st.add_parser('get')
    pget.add_argument('task_id')
    pget.set_defaults(func=cmd_tasks_get)
    pwatch = st.add_parser('watch')
    pwatch.add_argument('task_id')
    pwatch.set_defaults(func=cmd_tasks_watch)
    pabt = st.add_parser('abort')
    pabt.add_argument('task_id')
    pabt.set_defaults(func=cmd_tasks_abort)

    # agents
    pa = sub.add_parser('agents', help='Higher-level agents')
    sa = pa.add_subparsers(dest='subcmd', required=True)
    ptr = sa.add_parser('translate')
    ptr.add_argument('text')
    ptr.add_argument('--target', default='zh')
    ptr.add_argument('--wait', action='store_true',
                      help='Block until translation completes (poll loop).')
    ptr.add_argument('--text-only', action='store_true',
                      help='Print only the translated text (with --wait).')
    ptr.add_argument('--timeout', type=float, default=180.0)
    ptr.set_defaults(func=cmd_agents_translate)
    pft = sa.add_parser('fetch')
    pft.add_argument('url')
    pft.add_argument('--text-only', action='store_true')
    pft.set_defaults(func=cmd_agents_fetch)
    psr = sa.add_parser('search', help='Web search via the Tofu agent')
    psr.add_argument('query')
    psr.add_argument('--max-results', type=int, default=10)
    psr.add_argument('--freshness', default='',
                      help='day | week | month | year (optional)')
    psr.add_argument('--summary', action='store_true',
                      help='Request an LLM-generated summary of results.')
    psr.add_argument('--async', dest='async_mode', action='store_true',
                      help='Return task_id instead of blocking.')
    psr.add_argument('--json', action='store_true')
    psr.set_defaults(func=cmd_agents_search)

    # run — start any task kind and (by default) stream events
    prun = sub.add_parser(
        'run', help='Start any task kind and stream events')
    prun.add_argument('kind',
                       help='Task kind: paper-report | paper-translate | '
                            'translate | image-gen | memory-search | search | chat')
    prun.add_argument('--param', action='append', metavar='k=v',
                       help='Repeat for each param (JSON values supported).')
    prun.add_argument('--wait', action='store_true',
                       help='Block for terminal state instead of streaming.')
    prun.add_argument('--timeout', type=float, default=600.0)
    prun.set_defaults(func=cmd_run)

    return p


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except TofuError as e:
        sys.stderr.write(f'error: {e}\n')
        return 1
    except KeyboardInterrupt:
        sys.stderr.write('\nInterrupted.\n')
        return 130
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
