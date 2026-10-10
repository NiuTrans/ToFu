#!/usr/bin/env python3
"""Read-only diagnosis of the actual command/MCP launch environment.

Only path selection and resolved executables are printed, never credential
values. This does not run installers, shell startup scripts or tool processes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.tools.process_environment import environment_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=('command', 'mcp'), default='command')
    parser.add_argument('--cwd', default=str(Path.cwd()))
    parser.add_argument('--project-root')
    parser.add_argument('--mcp-config', type=Path,
                        help='JSON file containing one MCP server configuration')
    parser.add_argument('--launcher', action='append', default=[])
    args = parser.parse_args()
    try:
        if args.kind == 'mcp':
            from lib.mcp.stdio_environment import build_stdio_environment
            config = json.loads(args.mcp_config.read_text()) if args.mcp_config else {}
            environment = build_stdio_environment(config, prepare_directories=False)
            launchers = args.launcher or [config.get('command', 'uvx')]
        else:
            from lib.project_mod.run_command import _get_cmd_env
            environment = _get_cmd_env(args.cwd, project_root=args.project_root)
            launchers = args.launcher or ['python', 'pip', 'uv', 'node', 'npx']
        report = environment_report(environment, launchers,
                                    cwd=args.cwd if args.kind == 'command' else None)
    except (ValueError, OSError):
        # Config parsing and filesystem errors can embed secret-bearing input.
        print('Invalid environment or configuration; check its mapping and project paths.', file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(report['executables'].values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
