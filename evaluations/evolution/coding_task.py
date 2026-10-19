"""Synthetic dependency-planning smoke task and independent behavioral checker.

The model edits only the starter project. This evaluator freezes its delivered
module and checks returned values in the parent, outside model-written tests.
This is bounded local execution, not a security sandbox or a held-out benchmark.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile

TASK_ID = 'dependency-layers-v1'
STARTER = 'def dependency_layers(dependencies):\n    return [sorted(dependencies)]\n'
OBJECTIVE = """Repair planner.py: dependency_layers(dependencies) accepts a dict
mapping string task names to lists of prerequisite names. Return deterministic
parallel execution layers: each layer contains all currently ready tasks sorted
lexicographically. Include names appearing only as prerequisites. Ignore duplicate
edges. Empty input returns []. Cycles, including self-cycles, raise ValueError.
Do not mutate the input. Use only the Python standard library.
Coordinate two Workers through the available collaboration tools: one implements
planner.py; the other writes test_planner.py independently. Let both finish, then
run the tests yourself with python3 -m unittest -v. Fix any failures. Freeze the
saved source and tests with evolution_artifact, and submit the result through
evolution_coordinate with evidence of the executed checks. Stay in this project.
"""
CASES = [
    ({}, []),
    ({'b': [], 'a': []}, [['a', 'b']]),
    ({'deploy': ['test'], 'test': ['build'], 'build': []}, [['build'], ['test'], ['deploy']]),
    ({'release': ['lint', 'test'], 'test': ['build'], 'lint': ['build']},
     [['build'], ['lint', 'test'], ['release']]),
    ({'b': ['a', 'a']}, [['a'], ['b']]),
    ({'z': ['x'], 'a': [], 'x': []}, [['a', 'x'], ['z']]),
    ({'a': ['a']}, 'ValueError'),
    ({'a': ['b'], 'b': ['a'], 'free': []}, 'ValueError'),
    ({'c': ['b'], 'b': ['a'], 'x': ['y']}, [['a', 'y'], ['b', 'x'], ['c']]),
]
MAX_SOURCE_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 64 * 1024

# Limits are installed before loading the candidate. stdout is a bounded file,
# so a noisy or nonterminating module cannot fill the evaluator's memory/disk.
BRIDGE = r'''
import copy, json, resource, sys
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
resource.setrlimit(resource.RLIMIT_AS, (268435456, 268435456))
resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
payload = json.load(sys.stdin)
namespace = {'__name__': 'candidate'}
exec(compile(payload['source'], 'planner.py', 'exec'), namespace)
answers = []
for case in payload['inputs']:
    original = copy.deepcopy(case)
    try:
        result = {'value': namespace['dependency_layers'](case)}
    except Exception as exc:
        result = {'error': type(exc).__name__}
    result['input_unchanged'] = case == original
    answers.append(result)
print(json.dumps(answers, allow_nan=False))
'''


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def task_identity() -> dict:
    return {'task_id': TASK_ID, 'fixture_sha256': digest([STARTER, OBJECTIVE, CASES]),
            'checker_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'case_count': len(CASES)}


def check_delivery(project: Path, *, scratch: Path, timeout_seconds=5) -> dict:
    """Snapshot one regular module, run with no inherited secrets, reap on exit."""
    receipt = {'passed': False, 'passed_cases': 0, 'case_count': len(CASES),
               'timed_out': False, 'exit_code': None}
    try:
        fd = os.open(project / 'planner.py', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
                raise ValueError('candidate must be a bounded regular file')
            source = stream.read(MAX_SOURCE_BYTES + 1)
        if len(source) > MAX_SOURCE_BYTES:
            raise ValueError('candidate exceeds source bound')
        receipt['source_sha256'] = hashlib.sha256(source).hexdigest()
        payload = json.dumps({'source': source.decode('utf-8'),
                              'inputs': [case for case, _ in CASES]}).encode()
    except (OSError, ValueError) as exc:
        return {**receipt, 'error': type(exc).__name__}
    with tempfile.TemporaryDirectory(prefix='coding-check-', dir=scratch) as directory:
        # Keep the private file linked until readback: some FUSE volumes
        # cannot read an unlinked TemporaryFile through its open descriptor.
        with (Path(directory) / 'output').open('x+b') as output:
            process = subprocess.Popen([sys.executable, '-I', '-S', '-c', BRIDGE],
                cwd=directory, env={'PATH': os.defpath, 'HOME': directory, 'TMPDIR': directory},
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                start_new_session=True)
            try:
                process.communicate(payload, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                receipt['timed_out'] = True
            finally:
                # Also stop descendants when their parent has already exited.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # The entire owned process group has already exited.
                process.wait()
                if process.stdin is not None:
                    process.stdin.close()
            receipt['exit_code'] = process.returncode
            output.seek(0)
            raw = output.read(MAX_OUTPUT_BYTES + 1)
    if receipt['timed_out'] or process.returncode != 0 or len(raw) > MAX_OUTPUT_BYTES:
        return {**receipt, 'error': 'candidate_execution_failed'}
    try:
        answers = json.loads(raw)
        if not isinstance(answers, list) or len(answers) != len(CASES):
            raise ValueError('answer shape')
        expected = [dict({'error': expected} if isinstance(expected, str) else
                         {'value': expected}, input_unchanged=True) for _, expected in CASES]
        receipt['passed_cases'] = sum(digest(answer) == digest(want)
                                      for answer, want in zip(answers, expected))
        receipt['passed'] = receipt['passed_cases'] == len(CASES)
    except (ValueError, TypeError, RecursionError):
        receipt['error'] = 'invalid_candidate_output'
    return receipt
