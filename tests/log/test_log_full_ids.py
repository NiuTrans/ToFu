"""Log text carries whole conversation, task and attempt ids (docs/LOGGING.md).

An eight-character prefix is not an identifier here: every Worker id starts
with ``worker-`` and every durable task id with ``conversation-attempt:``, so
``conv=worker-1`` named about one Worker in sixteen and
``debug/inspect_conversation.py --logs`` attributed other Workers' lines to the
inspected conversation (LONGCTX-ATTRIB 48a9650c, 2026-11-09).
"""
from __future__ import annotations

import ast
import importlib
import logging
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKER = 'worker-1f0e9a2b3c4d5e6f708192a3'
ATTEMPT = '55f61236-291d-41f2-a573-4aed35636e26'

_LOG_METHODS = frozenset(
    {'debug', 'info', 'warning', 'warn', 'error', 'exception', 'critical', 'log'})
_LOG_FUNCTIONS = frozenset({'audit_log', 'log_event', '_warn_throttled'})
# The final identifier of an id: conv_id, conversation_id, task_id, attempt_id,
# execution_id (task runtime sessions use the task id), convId, ...; and a bare
# ``conv`` / ``target_conv`` holding a conversation id.
_ID_NAME = re.compile(
    r'(?:conv|conversation|task|attempt|execution)_?id$|(?:^|_)conv$', re.IGNORECASE)
_PLACEHOLDER = re.compile(
    r'%(?:\([^)]*\))?[-#0 +]*(?P<width>\*|\d+)?(?:\.(?P<precision>\*|\d+))?'
    r'[hlL]?(?P<kind>[diouxXeEfFgGcrsa%])')


def _tracked(*pathspecs: str) -> list[str]:
    output = subprocess.check_output(
        ['git', 'ls-files', '-z', '--', *pathspecs], cwd=ROOT)
    return sorted(path for path in output.decode().split('\0')
                  if path and (ROOT / path).is_file())


SOURCES = _tracked(
    '*.py', ':(exclude)tests/*', ':(exclude)*/tests/*', ':(exclude)benchmarks/*')
# Text a violation cannot lack: a prefix slice or a %-precision, and an id name.
_MAY_SHORTEN = re.compile(rb'\[\s*(?:0\s*)?:\s*\d+\s*\]|%[-#0 +]*\d*\.\d+s')
_MAY_NAME_ID = re.compile(rb'conv|task|attempt|execution', re.IGNORECASE)


def _is_logging_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in _LOG_FUNCTIONS
    if isinstance(func, ast.IfExp):  # (logger.warning if first else logger.debug)(...)
        return _is_logger_method(func.body) and _is_logger_method(func.orelse)
    return _is_logger_method(func)


def _is_logger_method(func: ast.AST) -> bool:
    return (isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS
            and 'log' in ast.unparse(func.value).lower())


def _names_an_id(node: ast.AST) -> bool:
    """Whether ``node`` evaluates to a conversation, task or attempt id."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == 'str' and node.args):
        return _names_an_id(node.args[0])
    if isinstance(node, ast.BoolOp):
        return any(_names_an_id(value) for value in node.values)
    if isinstance(node, ast.IfExp):
        return _names_an_id(node.body) or _names_an_id(node.orelse)
    if isinstance(node, ast.Name):
        return bool(_ID_NAME.search(node.id))
    if isinstance(node, ast.Attribute):
        return bool(_ID_NAME.search(node.attr))
    key = receiver = None
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        key, receiver = node.slice.value, node.value
    elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
          and node.func.attr == 'get' and node.args
          and isinstance(node.args[0], ast.Constant)):
        key, receiver = node.args[0].value, node.func.value
    if not isinstance(key, str):
        return False
    if key == 'id':
        return 'task' in ast.unparse(receiver).lower()
    return bool(_ID_NAME.search(key))


def _prefix_slice(node: ast.Subscript) -> bool:
    piece = node.slice
    return (isinstance(piece, ast.Slice) and piece.step is None
            and (piece.lower is None
                 or (isinstance(piece.lower, ast.Constant) and piece.lower.value == 0))
            and isinstance(piece.upper, ast.Constant) and type(piece.upper.value) is int)


def _shortened_ids(call: ast.Call) -> list[str]:
    found = []
    arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
    for argument in arguments:
        for node in ast.walk(argument):
            if (isinstance(node, ast.Subscript) and _prefix_slice(node)
                    and _names_an_id(node.value)):
                found.append(ast.unparse(node))
    # ``'%.8s' % conv_id`` style precision on an id argument.
    values = list(call.args)
    if isinstance(call.func, ast.Attribute) and call.func.attr == 'log':
        values = values[1:]
    if values and isinstance(values[0], ast.Constant) and isinstance(values[0].value, str):
        index = 1
        for match in _PLACEHOLDER.finditer(values[0].value):
            if match['kind'] == '%':
                continue
            index += (match['width'] == '*') + (match['precision'] == '*')
            if (match['kind'] == 's' and match['precision'] not in (None, '*')
                    and index < len(values) and _names_an_id(values[index])):
                found.append(f"%.{match['precision']}s <- {ast.unparse(values[index])}")
            index += 1
    return found


def test_no_log_call_shortens_a_conversation_task_or_attempt_id():
    assert len(SOURCES) > 1000, 'git ls-files must enumerate the shipped sources'
    violations = []
    for path in SOURCES:
        source = (ROOT / path).read_bytes()
        if not (_MAY_SHORTEN.search(source) and _MAY_NAME_ID.search(source)):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_logging_call(node):
                violations.extend(f'{path}:{node.lineno}: {text}'
                                  for text in _shortened_ids(node))
    assert not violations, (
        f'{len(violations)} log call(s) shorten an id; log it whole '
        '(task ids through lib.log.task_log_tag):\n' + '\n'.join(violations))


def _messages(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def test_cache_stats_line_names_the_whole_attempt_and_conversation(caplog):
    from lib.tasks_pkg.cache_tracking import _roi

    usage = {'input_tokens': 16, 'cache_creation_input_tokens': 2048,
             'cache_read_input_tokens': 4096}
    with caplog.at_level(logging.INFO, logger=_roi.logger.name):
        _roi.log_round_cache_stats(WORKER, 0, usage, 'claude-test',
                                   tid='conversation-attempt:' + ATTEMPT)

    assert any(message.startswith(f'[CacheStats] {ATTEMPT} conv={WORKER} R1 ')
               for message in _messages(caplog)), _messages(caplog)


def test_cache_track_break_line_names_the_whole_conversation(caplog):
    from lib.tasks_pkg.cache_tracking import _report

    report = _report.BreakReport(
        call_count=157, conv_id=WORKER, cache_read=0, cache_write=405282,
        previous_read=405282, previous_write=0, elapsed=25.4, usage=None,
        client_changes={}, prefix_mutation_break=False, turn_boundary_break=False,
        previous_turn_read=0, prefix_mutated=False, prefix_culprits=[],
        wire_proven_identical=False, namespace_switch=[],
        namespace_verified_same=False, wire_culprits=[], no_reuse=False,
        api_break=False, partial_no_reuse=False, previous_prefix_tokens=0)
    with caplog.at_level(logging.INFO, logger=_report.logger.name):
        _report.report_confirmed_break(report)

    assert any(message.startswith(f'[CacheTrack] conv={WORKER} call=157 ')
               for message in _messages(caplog)), _messages(caplog)


def test_model_replay_line_names_the_whole_conversation(caplog):
    from lib.tasks_pkg import model_replay

    with caplog.at_level(logging.INFO, logger=model_replay.logger.name):
        model_replay._publication_status({'convId': WORKER}, 'prepared')

    assert f'[ModelReplay] conv={WORKER} checkpoint=prepared' in _messages(caplog)


@pytest.mark.parametrize('module', [
    'lib.tasks_pkg.compaction._builtin_steps._shared',   # [L1] lines
    'lib.tasks_pkg.compaction._methods._shared',
    'lib.tasks_pkg.compaction._faithful_methods._state',
])
def test_compaction_lines_name_the_whole_conversation(module):
    assert importlib.import_module(module)._log_id(WORKER) == WORKER
