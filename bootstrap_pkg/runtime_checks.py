"""Native capability diagnostics and explicit transactional installer admission.

Entry points: check_javascript_runtime(), ensure_javascript_runtime(), CLI.
Dependencies: stdlib only in the caller; application imports stay in a bounded
child process so native libraries and compiler threads never burden server idle.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Exercise the shipped reactor and host bridge, not just package metadata/import.
_JAVASCRIPT_PROBE = '''
from lib.tools.javascript import execute_javascript_program, JavaScriptLimits
value, stats = execute_javascript_program(
    'return (await tools.invoke("probe", {value: 6})).value * 7;',
    search=lambda *args: [],
    call=lambda name, arguments, call_id=None: {
        'status': 'done', 'output': arguments, 'output_truncated': False},
    call_many=lambda *args: [],
    limits=JavaScriptLimits())
if value != 42 or stats['tool_calls'] != 1:
    raise RuntimeError('JavaScript host bridge smoke failed')
print('JavaScript WASM execution and host bridge verified')
'''


def check_javascript_runtime(*, python: str = sys.executable,
                             root: Path | None = None,
                             timeout: float = 30,
                             package_root: Path | None = None) -> tuple[bool, str]:
    """Check the selected interpreter without downloading or repairing anything."""
    root = root or Path(__file__).resolve().parent.parent
    try:
        probe = _JAVASCRIPT_PROBE
        if package_root is not None:
            probe = f'import sys; sys.path.insert(0, {json.dumps(str(package_root))})\n' + probe
        result = subprocess.run([python, '-c', probe], cwd=root,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, 'JavaScript runtime check timed out; installation is not verified.'
    except OSError as exc:
        return False, f'Cannot start JavaScript runtime check: {exc}'
    if result.returncode:
        detail = (result.stderr or result.stdout)[-4096:].strip()
        return False, (
            f'JavaScript runtime check failed (exit {result.returncode}): {detail}\n'
            'Use a Wasmtime build compatible with this host or a supported runtime '
            'environment. Reinstalling the same incompatible wheel or changing tool '
            'arguments will not fix a native loader failure.')
    return True, result.stdout[-4096:].strip()


def ensure_javascript_runtime(*, root: Path | None = None, emit=None) -> tuple[bool, str]:
    """Installer admission: execution probe, one compatible repair, verification."""
    root = root or Path(__file__).resolve().parent.parent
    healthy, detail = check_javascript_runtime(root=root)
    if healthy:
        return healthy, detail
    from .runtime_repair import repair_javascript_runtime
    return repair_javascript_runtime(detail, root=root, emit=emit)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repair', action='store_true', help='Installer only: build a pinned host-compatible library on glibc loader failure')
    args = parser.parse_args()
    healthy, detail = (ensure_javascript_runtime() if args.repair else check_javascript_runtime())
    print(detail, file=sys.stdout if healthy else sys.stderr)
    return 0 if healthy else 1


if __name__ == '__main__':
    raise SystemExit(main())
