"""Standalone workspace smoke for the canonical worker execution guards.

Runs the real request/tool pipeline with synthetic provider/tool bodies, plus
negative controls against reintroducing private Swarm execution. No live model
or running service is touched; deployment adoption needs verification after a
managed restart. Old round-log spelling is not evidence of current execution.
"""
from pathlib import Path
import argparse
import json
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    options = parser.parse_args()
    result = subprocess.run([
        sys.executable, '-m', 'pytest', '-q', '-m', 'unit', '--tb=short',
        'tests/swarm/test_swarm_invocation.py', 'tests/swarm/test_swarm_agent_loop_chassis.py',
    ], cwd=Path(__file__).resolve().parents[1], text=True,
       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if options.json:
        print(json.dumps({'ok': result.returncode == 0, 'scope': 'workspace',
                          'evidence': result.stdout[-16000:]}, ensure_ascii=False))
    else:
        print(result.stdout)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
