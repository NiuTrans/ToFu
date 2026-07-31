"""Guard: backend errors reach the frontend ONLY as typed envelopes.

WHY
---
Project invariant (owner, 2026-07-26): backend errors are passed to the
frontend truthfully, precisely, and well-presented. The mechanism is the
typed error envelope (``lib/error_envelope/``) + the frontend normalizer
(``static/js/core/error_envelope.js``). Everything else — the classifier,
the i18n parity, the mojibake repair — already has its own suite. This
suite is the RATCHET that keeps the invariant true as the code evolves:

1. AST scan — every ``<x>['error'] = RHS`` assignment in ``lib/`` +
   ``routes/`` must build or pass an envelope (or None). A bare
   ``task['error'] = str(e)`` strips kind/severity/hint and renders as an
   untruthful 'Unknown error' — this scan fails the build on sight.
2. ``worker_lost`` (TaskRuntime stall-reap) is a first-class kind. It used
   to be stamped as an incomplete dict with no ``message`` — the frontend
   ``isErrorEnvelope`` rejected it and the user saw 'Unknown error' + a
   JSON blob.
3. ``TaskRuntime._make_envelope`` completes EVERY error shape (exception /
   complete envelope / incomplete dict / kind-name string / raw string)
   into a full envelope.

NEUTER evidence (manual, 2026-07-26):
  * reverting the dict-completion branch in _make_envelope turns
    test_finish_completes_incomplete_dict + test_reap_produces_complete_
    envelope red (message key missing);
  * dropping 'worker_lost' from KINDS turns the membership pins red AND
    downgrades the behavioral pins to kind='generic';
  * a probe file containing ``task['error'] = str(e)`` under lib/ turns
    the AST scan red naming the file:line.
"""

from __future__ import annotations

import ast
import os
import re
import time
import unittest

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

# RHS source-segment tokens that prove the assigned value is envelope-
# produced or envelope-passed. Anything else (e.g. ``str(e)``) is a
# transparency violation.
_SANCTIONED_TOKENS = (
    'envelope', 'make_env', 'from_exc', 'from_exception',
    'format_llm_error', '_user_err', "['error']", ".get('error'",
    '_err_from_json',
)

# Chat-wire task dicts — assignments onto THESE targets are always policed,
# no grandfathering: the chat bubble renders task['error'] through
# renderErrorEnvelope, so only a typed envelope preserves the truth there.
_TASK_TARGETS = ('task', 'new_task')

# A raw exception being stringified / stored verbatim onto an 'error' field
# — the untruthful-presentation pattern (loses kind/severity/hint).
_RAW_EXCEPTION_RE = re.compile(
    r"""^(?:str\(\s*[A-Za-z_][A-Za-z0-9_]*\s*\)|e|exc|ex)$""", re.X)

# Grandfathered legacy raw-exception sites, each verified (2026-07-26) to be
# a DEDICATED non-chat surface whose own UI already presents the string
# truthfully (OAuth login flow, file-history API, settings probe grid, VLM
# upload badge, an internal thread result box that is re-raised). The chat
# envelope contract does not apply to them. Exact-match ratchet: fixing one
# turns this test red until the entry is removed — the list only shrinks.
#   (relpath, rhs_source) -> allowed occurrence count
_GRANDFATHERED = {
    ('lib/oauth/manager/_exchange.py', 'str(e)'): 2,
    ('lib/file_history/api.py', 'str(e)'): 2,
    ('lib/pdf_parser/vlm/_tasks.py', 'str(exc)'): 1,
    ('lib/provider_probe.py', 'str(e)[:300]'): 1,
    ('lib/memory/prefetch/_rerank.py', 'e'): 1,
    ('routes/paper.py', 'ex'): 1,
}


def _py_files():
    """Tracked Python files under lib/ + routes/.

    Enumerated via ``git ls-files`` (the repo index), NOT os.walk: walking
    the tree stats every untracked artefact on this FUSE mount and takes
    minutes, while the index answers in milliseconds and covers exactly the
    files the ratchet must police (anything committable).
    """
    import subprocess
    out = subprocess.check_output(
        ['git', 'ls-files', 'lib/*.py', 'routes/*.py'],
        cwd=ROOT, text=True)
    return [os.path.join(ROOT, p) for p in out.split()]


def _iter_error_assignments():
    """Yield (path, lineno, rhs_source) for every ``<x>['error'] = RHS``."""
    for path in _py_files():
        with open(path, encoding='utf-8') as f:
            src = f.read()
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not (isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == 'error'):
                    continue
                rhs = ast.get_source_segment(src, node.value) or ''
                base = ast.unparse(target.value)
                yield path, node.lineno, rhs.strip(), base


class TestErrorAssignmentRatchet(unittest.TestCase):

    def test_every_error_assignment_is_envelope_produced(self):
        violations = []
        grandfather_seen: dict[tuple[str, str], int] = {}
        for path, lineno, rhs, base in _iter_error_assignments():
            rel = os.path.relpath(path, ROOT)
            if rhs in ('None',):
                continue  # clearing the error is fine
            if any(tok in rhs for tok in _SANCTIONED_TOKENS):
                continue
            is_task_wire = base in _TASK_TARGETS
            is_raw_exception = bool(_RAW_EXCEPTION_RE.match(rhs))
            if not (is_task_wire or is_raw_exception):
                continue  # truthful literal / internal plumbing variable
            key = (rel, rhs)
            grandfather_seen[key] = grandfather_seen.get(key, 0) + 1
            if grandfather_seen[key] > _GRANDFATHERED.get(key, 0):
                why = 'chat-wire task error must be an envelope' if is_task_wire \
                    else 'raw exception onto a user-facing error field'
                violations.append(
                    f'{rel}:{lineno}: {base}[\'error\'] = {rhs[:80]} ({why})')
        stale = [f'{p}: {r}' for (p, r), allowed in _GRANDFATHERED.items()
                 if grandfather_seen.get((p, r), 0) != allowed]
        self.assertEqual(
            stale, [],
            'grandfathered raw-exception sites changed count — if you FIXED '
            'one, shrink _GRANDFATHERED accordingly:\n' + '\n'.join(stale))
        self.assertEqual(
            violations, [],
            'bare (non-envelope) user-facing error assignments found — '
            'route them through make_envelope / from_exception / '
            'format_llm_error_for_user, or pass an existing envelope:\n'
            + '\n'.join(violations))

    def test_scan_actually_finds_sites(self):
        """Guard against a silently empty scan (regex/AST drift)."""
        sites = list(_iter_error_assignments())
        self.assertTrue(all(len(s) == 4 for s in sites))
        self.assertGreaterEqual(
            len(sites), 10,
            f'expected >=10 error-assignment sites, found {len(sites)} — '
            'the ratchet may be scanning nothing')


class TestWorkerLostFirstClass(unittest.TestCase):

    def test_kind_registered_everywhere(self):
        from lib.error_envelope._constants import (
            KINDS, _RETRYABLE_KINDS, _TITLES, _WARNING_KINDS)
        self.assertIn('worker_lost', KINDS)
        self.assertIn('worker_lost', _WARNING_KINDS)
        self.assertIn('worker_lost', _RETRYABLE_KINDS)
        self.assertIn('worker_lost', _TITLES)

    def test_frontend_chip_label_present(self):
        with open(os.path.join(ROOT, 'static', 'js', 'core',
                               'error_envelope.js'), encoding='utf-8') as f:
            src = f.read()
        self.assertIn("worker_lost:", src,
                      'ERROR_KIND_LABELS missing worker_lost — the chip '
                      'would render the raw kind string')

    def test_i18n_keys_present(self):
        with open(os.path.join(ROOT, 'static', 'js', 'i18n.js'),
                  encoding='utf-8') as f:
            src = f.read()
        for suffix in ('chip', 'title', 'hint'):
            self.assertIn(f"'err.k.worker_lost.{suffix}'", src)


class TestRuntimeEnvelopeCompletion(unittest.TestCase):

    def _runtime(self):
        from lib.agent_core.task_runtime import TaskRuntime
        return TaskRuntime('guard-test', push_channel=None)

    def test_finish_completes_incomplete_dict(self):
        rt = self._runtime()
        task = rt.create()
        rt.finish(task['id'], error={'kind': 'worker_lost',
                                     'detail': 'stalled 42s'})
        env = rt.get(task['id'])['error']
        self.assertEqual(env['kind'], 'worker_lost')
        self.assertEqual(env['detail'], 'stalled 42s')
        self.assertIsInstance(env.get('message'), str)
        self.assertTrue(env['message'], 'message must be non-empty')
        self.assertEqual(env.get('titleKey'), 'err.k.worker_lost.title')
        self.assertTrue(env['retryable'])

    def test_finish_kind_name_string(self):
        """finish(error='worker_lost') — the documented stall contract —
        must produce the worker_lost envelope, not a generic one."""
        rt = self._runtime()
        task = rt.create()
        rt.finish(task['id'], error='worker_lost')
        env = rt.get(task['id'])['error']
        self.assertEqual(env['kind'], 'worker_lost')
        self.assertIsInstance(env.get('message'), str)
        self.assertTrue(env['message'])

    def test_finish_raw_string_stays_generic_but_complete(self):
        rt = self._runtime()
        task = rt.create()
        rt.finish(task['id'], error='something broke')
        env = rt.get(task['id'])['error']
        self.assertEqual(env['kind'], 'generic')
        self.assertEqual(env['detail'], 'something broke')
        self.assertIsInstance(env.get('message'), str)
        self.assertTrue(env['message'])

    def test_finish_complete_envelope_passthrough(self):
        from lib.error_envelope import make_envelope
        rt = self._runtime()
        task = rt.create()
        original = make_envelope('timeout', detail='slow')
        rt.finish(task['id'], error=original)
        self.assertIs(rt.get(task['id'])['error'], original)

    def test_reap_produces_complete_envelope(self):
        """The stall-reap path (podcast/video polling UIs consume
        resp.error.kind === 'worker_lost') must ALSO carry a renderable
        message — kind alone rendered as 'Unknown error'."""
        rt = self._runtime()
        rt.stall_timeout = 0.01
        task = rt.create()
        stale = rt.get(task['id'])
        stale['updated_at'] = time.time() - 100
        resp = rt.poll(task['id'])
        env = resp['error']
        self.assertEqual(env['kind'], 'worker_lost')
        self.assertIsInstance(env.get('message'), str)
        self.assertTrue(env['message'])
        self.assertEqual(env.get('titleKey'), 'err.k.worker_lost.title')
        self.assertIn('no progress events', env.get('detail', ''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
