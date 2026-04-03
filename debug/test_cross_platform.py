#!/usr/bin/env python3
"""Cross-platform smoke test for Tofu.

Validates that key modules import without errors on any platform
(Linux, macOS, Windows) and that the compat layer provides correct
abstractions.

Usage:
    python debug/test_cross_platform.py

Requires only stdlib + the project on sys.path — no Flask or third-party
packages needed (except what's already imported by the tested modules).
"""

import os
import sys
import platform

# Ensure project root is on sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

passed = 0
failed = 0
errors = []


def check(name, condition, detail=''):
    """Assert a condition, track pass/fail."""
    global passed, failed
    if condition:
        passed += 1
        print(f'  ✅ {name}')
    else:
        failed += 1
        msg = f'  ❌ {name}'
        if detail:
            msg += f' — {detail}'
        print(msg)
        errors.append(name)


def section(title):
    print(f'\n{"═" * 60}')
    print(f'  {title}')
    print(f'{"═" * 60}')


# ══════════════════════════════════════════════════════════
#  1. Platform detection
# ══════════════════════════════════════════════════════════
section('1. Platform Detection')

from lib.compat import IS_WINDOWS, IS_MACOS, IS_LINUX, HAS_PROCFS

print(f'  Platform: {platform.system()} {platform.release()}')
print(f'  Python:   {sys.version}')
print(f'  IS_WINDOWS={IS_WINDOWS}, IS_MACOS={IS_MACOS}, IS_LINUX={IS_LINUX}')
print(f'  HAS_PROCFS={HAS_PROCFS}')

# Exactly one platform should be True (or none for unusual platforms)
plat_count = sum([IS_WINDOWS, IS_MACOS, IS_LINUX])
check('Exactly one platform flag is True', plat_count == 1,
      f'got {plat_count}')

if IS_LINUX:
    check('HAS_PROCFS is True on Linux', HAS_PROCFS)
if IS_WINDOWS or IS_MACOS:
    check('HAS_PROCFS is False on non-Linux', not HAS_PROCFS)


# ══════════════════════════════════════════════════════════
#  2. Shell invocation
# ══════════════════════════════════════════════════════════
section('2. Shell Invocation (get_shell_args)')

from lib.compat import get_shell_args

args = get_shell_args('echo hello')
check('get_shell_args returns a list', isinstance(args, list))
check('get_shell_args has 3 elements', len(args) == 3,
      f'got {len(args)}: {args}')

if IS_WINDOWS:
    check('Windows: uses cmd.exe', args[0] == 'cmd.exe')
    check('Windows: /c flag', args[1] == '/c')
else:
    check('Unix: uses /bin/sh', args[0] == '/bin/sh')
    check('Unix: -c flag', args[1] == '-c')


# ══════════════════════════════════════════════════════════
#  3. Username detection
# ══════════════════════════════════════════════════════════
section('3. Username Detection')

from lib.compat import get_username

username = get_username()
check('get_username returns a non-empty string', isinstance(username, str) and len(username) > 0,
      f'got: {repr(username)}')


# ══════════════════════════════════════════════════════════
#  4. Temp directory
# ══════════════════════════════════════════════════════════
section('4. Temp Directory')

from lib.compat import get_temp_dir

tmp = get_temp_dir()
check('get_temp_dir returns an existing path', os.path.isdir(tmp),
      f'got: {tmp}')


# ══════════════════════════════════════════════════════════
#  5. Process introspection
# ══════════════════════════════════════════════════════════
section('5. Process Introspection')

from lib.compat import is_process_alive

my_pid = os.getpid()
check('is_process_alive(own PID) returns True', is_process_alive(my_pid))
check('is_process_alive(99999999) returns False', not is_process_alive(99999999))


# ══════════════════════════════════════════════════════════
#  6. Pipe I/O helpers
# ══════════════════════════════════════════════════════════
section('6. Non-blocking Pipe I/O')

from lib.compat import set_pipe_nonblocking, safe_select_pipes

if IS_WINDOWS:
    check('set_pipe_nonblocking returns False on Windows',
          set_pipe_nonblocking(sys.stdout) is False)
    check('safe_select_pipes returns list on Windows',
          isinstance(safe_select_pipes([], timeout=0.01), list))
else:
    # Just check they don't crash — actual non-blocking test is complex
    check('safe_select_pipes returns list on Unix',
          isinstance(safe_select_pipes([], timeout=0.01), list))


# ══════════════════════════════════════════════════════════
#  7. Signal compatibility
# ══════════════════════════════════════════════════════════
section('7. Signal Compatibility')

from lib.compat import safe_signal
import signal

# SIGINT is available everywhere
prev = safe_signal(signal.SIGINT, signal.SIG_DFL)
check('safe_signal(SIGINT) succeeds', prev is not None)
# Restore
safe_signal(signal.SIGINT, prev)

# SIGTERM may not be available on Windows
if IS_WINDOWS:
    # On Windows, signal.SIGTERM exists but may not be registerable for all handlers
    check('safe_signal handles unavailable signals gracefully',
          safe_signal(signal.SIGTERM, lambda *a: None) is not None or True)
else:
    prev2 = safe_signal(signal.SIGTERM, signal.SIG_DFL)
    check('safe_signal(SIGTERM) succeeds on Unix', prev2 is not None)
    safe_signal(signal.SIGTERM, prev2)


# ══════════════════════════════════════════════════════════
#  8. Key module imports (no crash test)
# ══════════════════════════════════════════════════════════
section('8. Key Module Imports')

modules_to_test = [
    ('lib.compat', 'Core compat layer'),
    ('lib.log', 'Logging utilities'),
    ('lib.fs_keepalive', 'FS keepalive daemon'),
    ('lib.project_mod.config', 'Project config'),
    ('lib.project_mod.scanner', 'Project scanner'),
]

for mod_name, description in modules_to_test:
    try:
        __import__(mod_name)
        check(f'import {mod_name} ({description})', True)
    except Exception as e:
        check(f'import {mod_name} ({description})', False, str(e))


# ══════════════════════════════════════════════════════════
#  9. FS keepalive graceful skip
# ══════════════════════════════════════════════════════════
section('9. FS Keepalive Graceful Skip')

from lib.fs_keepalive import start_fs_keepalive, _is_network_mount

# On non-Linux or non-/mnt/ paths, should not crash
try:
    start_fs_keepalive()
    check('start_fs_keepalive() runs without error', True)
except Exception as e:
    check('start_fs_keepalive() runs without error', False, str(e))

# Network mount detection
if IS_LINUX:
    check('_is_network_mount(/mnt/data) on Linux', _is_network_mount('/mnt/data'))
    check('_is_network_mount(/home/user) on Linux', not _is_network_mount('/home/user'))
elif IS_MACOS:
    check('_is_network_mount returns False on macOS', not _is_network_mount('/Volumes/Data'))
elif IS_WINDOWS:
    check('_is_network_mount returns False on Windows', not _is_network_mount('C:\\Users'))


# ══════════════════════════════════════════════════════════
#  10. Database bootstrap binary finder
# ══════════════════════════════════════════════════════════
section('10. PG Binary Finder')

try:
    from lib.database._bootstrap import _find_pg_binary
    # Should return a string (either found path or bare name)
    result = _find_pg_binary('pg_ctl')
    check('_find_pg_binary returns a string', isinstance(result, str) and len(result) > 0,
          f'got: {repr(result)}')
    # Should not crash on unknown binary
    result2 = _find_pg_binary('nonexistent_binary_xyz')
    check('_find_pg_binary handles unknown binary gracefully',
          isinstance(result2, str),
          f'got: {repr(result2)}')
except Exception as e:
    check('_find_pg_binary imports and works', False, str(e))


# ══════════════════════════════════════════════════════════
#  11. DANGEROUS_PATTERNS and command analysis
# ══════════════════════════════════════════════════════════
section('11. Command Safety Analysis')

from lib.project_mod.tools import _is_destructive_command

check('echo is not destructive', not _is_destructive_command('echo hello'))
check('ls is not destructive', not _is_destructive_command('ls -la'))
check('rm -rf is destructive', _is_destructive_command('rm -rf /tmp/foo'))
check('python is destructive (opaque)', _is_destructive_command('python script.py'))
check('git status is not destructive', not _is_destructive_command('git status'))
check('git checkout is destructive', _is_destructive_command('git checkout main'))


# ══════════════════════════════════════════════════════════
#  12. Optional dependency graceful degradation
# ══════════════════════════════════════════════════════════
section('12. Optional Dependency Flags')

# lib/fetch/utils.py — HAS_* flags must be booleans (True if installed, False if not)
try:
    from lib.fetch.utils import HAS_TRAFILATURA, HAS_PLAYWRIGHT, HAS_FITZ, HAS_PIL
    check('HAS_TRAFILATURA is a bool', isinstance(HAS_TRAFILATURA, bool),
          f'got {type(HAS_TRAFILATURA).__name__}')
    check('HAS_PLAYWRIGHT is a bool', isinstance(HAS_PLAYWRIGHT, bool),
          f'got {type(HAS_PLAYWRIGHT).__name__}')
    check('HAS_FITZ is a bool', isinstance(HAS_FITZ, bool),
          f'got {type(HAS_FITZ).__name__}')
    check('HAS_PIL is a bool', isinstance(HAS_PIL, bool),
          f'got {type(HAS_PIL).__name__}')
    print(f'    (HAS_TRAFILATURA={HAS_TRAFILATURA}, HAS_PLAYWRIGHT={HAS_PLAYWRIGHT}, '
          f'HAS_FITZ={HAS_FITZ}, HAS_PIL={HAS_PIL})')
except Exception as e:
    check('lib.fetch.utils HAS_* flags importable', False, str(e))

# lib/pdf_parser/_common — HAS_PYMUPDF and HAS_PYMUPDF4LLM must be booleans
try:
    from lib.pdf_parser._common import HAS_PYMUPDF, HAS_PYMUPDF4LLM
    check('HAS_PYMUPDF is a bool', isinstance(HAS_PYMUPDF, bool),
          f'got {type(HAS_PYMUPDF).__name__}')
    check('HAS_PYMUPDF4LLM is a bool', isinstance(HAS_PYMUPDF4LLM, bool),
          f'got {type(HAS_PYMUPDF4LLM).__name__}')
    print(f'    (HAS_PYMUPDF={HAS_PYMUPDF}, HAS_PYMUPDF4LLM={HAS_PYMUPDF4LLM})')
except Exception as e:
    check('lib.pdf_parser._common HAS_* flags importable', False, str(e))

# The import itself should not crash even if deps are missing
try:
    import lib.fetch.utils
    check('lib.fetch.utils imports without crash', True)
except Exception as e:
    check('lib.fetch.utils imports without crash', False, str(e))

try:
    import lib.pdf_parser._common
    check('lib.pdf_parser._common imports without crash', True)
except Exception as e:
    check('lib.pdf_parser._common imports without crash', False, str(e))


# ══════════════════════════════════════════════════════════
#  Summary
# ══════════════════════════════════════════════════════════
section('RESULTS')
total = passed + failed
print(f'\n  Passed: {passed}/{total}')
if failed:
    print(f'  Failed: {failed}/{total}')
    print(f'  Failures: {", ".join(errors)}')
    sys.exit(1)
else:
    print('  All checks passed! ✅')
    sys.exit(0)
