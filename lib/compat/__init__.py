"""lib/compat/ — Compatibility adapters for popular ecosystems.

Each submodule translates a foreign API surface (OpenAI Chat
Completions, Anthropic Messages, etc.) into the Tofu native task
pipeline so that drop-in clients (openai-python, langchain, anthropic
SDK, Cline, Continue.dev, OpenWebUI) work unchanged when pointed at
this server.

Import-time rule: this package MUST stay free of route/Blueprint
references. The ``routes/compat_*.py`` blueprints import from here.

This package also re-exports the cross-platform OS shim that
historically lived at ``lib/compat.py`` (now ``lib/compat/_platform.py``)
so existing ``from lib.compat import safe_signal`` etc. keep working.
"""

from lib.compat._platform import (
    IS_WINDOWS,
    IS_MACOS,
    IS_LINUX,
    HAS_PROCFS,
    get_shell_args,
    get_username,
    get_temp_dir,
    is_process_alive,
    is_process_named,
    set_pipe_nonblocking,
    safe_select_pipes,
    safe_signal,
    is_network_mount,
    safe_shlex_split,
)

__all__ = [
    'IS_WINDOWS',
    'IS_MACOS',
    'IS_LINUX',
    'HAS_PROCFS',
    'get_shell_args',
    'get_username',
    'get_temp_dir',
    'is_process_alive',
    'is_process_named',
    'set_pipe_nonblocking',
    'safe_select_pipes',
    'safe_signal',
    'is_network_mount',
    'safe_shlex_split',
]
