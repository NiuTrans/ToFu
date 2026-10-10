"""One Git operation in a disposable source workspace, launched by GitOperations.

Private input/result files are the only IPC. Upstream tools run unchanged except
for credential transport, per-operation cache ownership and acknowledged pushes.
No exception text or Git output crosses this worker boundary.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import threading
import time

from .git_resources import apply_native_limits
from .writes import WriteError, WriteReceipts, contract


def watch_parent(root):
    """A crashed transport cannot leave its Git operation running indefinitely."""
    from .git_workspace import alive
    parent = json.loads((root / 'owner.json').read_bytes())['parent']
    def watch():
        while alive(parent):
            time.sleep(0.25)
        if os.name != 'nt':
            import signal
            os.killpg(os.getpid(), signal.SIGKILL)
        os._exit(1)  # Windows normally exits through the parent's Job Object.
    threading.Thread(target=watch, daemon=True, name='git-parent-lifetime').start()


def configure_upstream(upstream):
    """Keep account tokens out of native Git arguments and repository config."""
    from overleaf_mcp.config import OVERLEAF_GIT_HOST
    # Naming the project may perform unrelated web I/O after a successful push.
    # Git-only requests have all the identity needed for a concise result.
    upstream.git_client._project_tag = lambda project: project.project_id
    index = int(os.environ.get('GIT_CONFIG_COUNT', '0'))
    def remote_url(project):
        encoded = base64.b64encode(('git:' + project.git_token).encode()).decode()
        os.environ.update({
            'GIT_CONFIG_COUNT': str(index + 1),
            f'GIT_CONFIG_KEY_{index}': 'http.https://' + OVERLEAF_GIT_HOST + '/.extraHeader',
            f'GIT_CONFIG_VALUE_{index}': 'Authorization: Basic ' + encoded,
        })
        return 'https://' + OVERLEAF_GIT_HOST + '/' + project.project_id
    upstream.git_client._git_url = remote_url


def write_result(path, value, maximum):
    """Bound encoded results, including expansion of control characters."""
    size = 0
    with path.open('wb') as stream:
        for chunk in json.JSONEncoder(ensure_ascii=False, separators=(',', ':')).iterencode(value):
            data = chunk.encode()
            size += len(data)
            if size > maximum:
                raise WriteError('git_response_limit')
            stream.write(data)


async def dispatch(request):
    from overleaf_mcp import server as upstream
    configure_upstream(upstream)
    # Every read/modify step uses the clone's original revision. A TTL pull
    # between update_section's read and rewrite could otherwise erase a
    # collaborator's intervening edit while still producing a valid push.
    upstream.git_client._pull_if_stale = lambda repo, project_id: None
    name, arguments = request['name'], request['arguments']
    writer = WriteReceipts(upstream, process_group_owned=True)
    if name in contract()['x-mcp-write-tools']:
        return await writer.invoke(name, arguments)
    if name not in contract()['x-mcp-git-read-tools']:
        raise WriteError('git_tool_invalid')
    return await upstream._dispatch(name, arguments)


def main():
    root = Path(sys.argv[1]).resolve()
    deadline = time.monotonic() + 5
    while not (root / 'admitted').is_file():
        if time.monotonic() >= deadline:
            raise SystemExit(1)
        time.sleep(0.02)
    request = json.loads((root / 'request.json').read_bytes())
    limits = contract()['x-resource-limits']
    apply_native_limits(request['memory_bytes'], request['seconds'],
                        request['workspace_bytes'])
    threading.stack_size(1024 * 1024)
    watch_parent(root)
    os.environ.update({'OVERLEAF_TEMP_DIR': str(root / 'source'),
                       'GIT_TERMINAL_PROMPT': '0',
                       'GIT_CONFIG_COUNT': '3',
                       'GIT_CONFIG_KEY_0': 'pack.threads', 'GIT_CONFIG_VALUE_0': '1',
                       'GIT_CONFIG_KEY_1': 'pack.windowMemory',
                       'GIT_CONFIG_VALUE_1': str(limits['overleaf_pack_window_bytes']),
                       'GIT_CONFIG_KEY_2': 'pack.deltaCacheSize',
                       'GIT_CONFIG_VALUE_2': str(limits['overleaf_pack_delta_bytes'])})
    try:
        result = {'ok': True, 'result': asyncio.run(dispatch(request))}
        write_result(root / 'result.json', result, limits['overleaf_git_response_bytes'])
    except BaseException as exc:
        code = ('git_memory_limit' if isinstance(exc, MemoryError) else
                str(exc) if isinstance(exc, WriteError) else 'git_operation_unconfirmed')
        write_result(root / 'result.json', {'ok': False, 'error': code}, 4096)


if __name__ == '__main__':
    main()
