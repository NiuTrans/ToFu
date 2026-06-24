"""Unit tests for debug/triage_errors.py — record-aware error-log clustering.

The triage tool groups logs/error.log into timestamp-anchored *records* so a
multi-line Python traceback counts as ONE event (bucketed by its terminal
exception type) instead of one ``Traceback`` line plus several stray ``OTHER``
frame lines. These tests pin that behavior down.

``debug/`` is a namespace package (no __init__.py), so the module is loaded
directly from its file path.
"""

from __future__ import annotations

import importlib.util
import os
from datetime import datetime

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_PATH = os.path.join(_REPO, 'debug', 'triage_errors.py')


def _load_module():
    spec = importlib.util.spec_from_file_location('_triage_errors_under_test', _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


te = _load_module()


# Realistic multi-line slice mirroring the live error.log structure.
_SAMPLE = """\
2026-06-22 13:03:22 [WARNING] lib.tasks_pkg.tool_hooks [MainThread]: [Hooks] Pre-hook bad_hook failed for test_tool: hook failed
Traceback (most recent call last):
  File "/repo/lib/tasks_pkg/tool_hooks.py", line 123, in run_pre_hooks
    result = hook(tool_name, args, task)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/repo/tests/test_new_features.py", line 378, in bad_hook
    raise RuntimeError('hook failed')
RuntimeError: hook failed
2026-06-23 11:18:33 [ERROR] routes.tenants [MainThread]: lookup failed
Traceback (most recent call last):
  File "/repo/routes/tenants.py", line 42, in lookup
    cur.execute('select * from tenant_users')
sqlite3.OperationalError: no such table: tenant_users
2026-06-22 13:57:01 [WARNING] lib.llm._sse_core [run_task-9efe]: [R11] ⚠ PREMATURE STREAM CLOSE: Server never sent [DONE] marker.
2026-06-23 16:02:31 [ERROR] lib.mcp.client [MainThread]: mcp call failed
Traceback (most recent call last):
  File "/repo/lib/mcp/client.py", line 88, in call
    raise McpError(...)
mcp.shared.exceptions.McpError: Timed out while waiting for response.
2026-06-22 12:53:09 [WARNING] server.memwatch [memwatch]: [MEMWATCH] cgroup memory 99.1% of limit
"""


@pytest.fixture()
def sample_log(tmp_path):
    p = tmp_path / 'error.log'
    p.write_text(_SAMPLE, encoding='utf-8')
    return str(p)


@pytest.mark.unit
class TestRecordGrouping:
    def test_records_are_timestamp_anchored(self):
        records = list(te._iter_records(_SAMPLE.splitlines(keepends=True)))
        # 5 records: 2 tracebacks, 1 premature-close, 1 mcp traceback, 1 memwatch.
        assert len(records) == 5
        # The first record absorbs its whole traceback (header + frames + exc).
        assert records[0][0].startswith('2026-06-22 13:03:22')
        assert records[0][-1] == 'RuntimeError: hook failed'
        assert len(records[0]) == 8

    def test_leading_orphan_lines_form_a_record(self):
        # A file that starts mid-traceback (no leading timestamp) must not crash.
        text = "  File 'x.py', line 1\nRuntimeError: boom\n2026-06-22 13:03:22 [INFO] ok\n"
        records = list(te._iter_records(text.splitlines(keepends=True)))
        assert len(records) == 2
        assert records[0][-1] == 'RuntimeError: boom'


@pytest.mark.unit
class TestExceptionTyping:
    def test_terminal_exception_extracted(self):
        rec = _SAMPLE.splitlines()[0:8]
        assert te._exception_type(rec) == 'RuntimeError'

    def test_dotted_exception_name(self):
        rec = [
            '2026-06-23 16:02:31 [ERROR] x: boom',
            'Traceback (most recent call last):',
            '  File "y.py", line 1, in f',
            'mcp.shared.exceptions.McpError: timed out',
        ]
        assert te._exception_type(rec) == 'mcp.shared.exceptions.McpError'

    def test_no_traceback_returns_none(self):
        assert te._exception_type(['2026-06-22 13:57:01 [WARNING] just a warning']) is None

    def test_prose_after_traceback_not_mistaken_for_exception(self):
        # A traceback whose terminal line isn't an exception-shaped name.
        rec = [
            '2026-06-23 11:00:00 [ERROR] x: boom',
            'Traceback (most recent call last):',
            '  File "y.py", line 1, in f',
            'Some non-exception trailing prose line',
        ]
        assert te._exception_type(rec) is None

    def test_chained_traceback_reports_outermost(self):
        rec = [
            '2026-06-23 11:00:00 [ERROR] x: boom',
            'Traceback (most recent call last):',
            '  File "a.py", line 1, in f',
            'ValueError: inner',
            'During handling of the above exception, another occurred:',
            'Traceback (most recent call last):',
            '  File "b.py", line 2, in g',
            'KeyError: outer',
        ]
        assert te._exception_type(rec) == 'KeyError'


@pytest.mark.unit
class TestClassify:
    def test_traceback_classified_by_exception_type(self):
        rec = _SAMPLE.splitlines()[0:8]
        assert te._classify(rec) == 'Traceback: RuntimeError'

    def test_semantic_signature_wins_over_traceback(self):
        # A premature-close record that ALSO carries a traceback keeps the
        # semantic label (it names the actionable category).
        rec = [
            '2026-06-22 13:57:01 [WARNING] x: PREMATURE STREAM CLOSE: no [DONE]',
            'Traceback (most recent call last):',
            '  File "z.py", line 1, in h',
            'RuntimeError: boom',
        ]
        assert te._classify(rec) == 'PREMATURE STREAM CLOSE'

    def test_unparsed_traceback_bucket(self):
        rec = [
            '2026-06-23 19:59:57 [ERROR] x: boom',
            'Traceback (most recent call last):',
            '  File "z.py", line 1, in h',
            '    do_thing()',
        ]
        assert te._classify(rec) == 'Traceback: <unparsed>'

    def test_generic_signature_fallback(self):
        rec = ['2026-06-23 10:00:00 [WARNING] x: AttributeError somewhere inline']
        assert te._classify(rec) == 'AttributeError'

    def test_other_bucket(self):
        rec = ['2026-06-22 12:53:09 [WARNING] x: [MEMWATCH] cgroup memory 99.1%']
        assert te._classify(rec) == 'OTHER'


@pytest.mark.unit
class TestTriageEndToEnd:
    def test_buckets_and_counts(self, sample_log):
        stats = te.triage(sample_log, since=None)
        meta = stats.pop('_meta')
        assert not meta.get('missing')
        counts = {k: v['count'] for k, v in stats.items()}
        assert counts.get('Traceback: RuntimeError') == 1
        assert counts.get('Traceback: sqlite3.OperationalError') == 1
        assert counts.get('Traceback: mcp.shared.exceptions.McpError') == 1
        assert counts.get('PREMATURE STREAM CLOSE') == 1
        assert counts.get('OTHER') == 1
        # No stray traceback frame/code lines leaked into OTHER.
        assert counts['OTHER'] == 1

    def test_traceback_example_is_the_exception_line(self, sample_log):
        stats = te.triage(sample_log, since=None)
        assert stats['Traceback: sqlite3.OperationalError']['example'] == \
            'sqlite3.OperationalError: no such table: tenant_users'

    def test_since_cutoff_filters_old_records(self, sample_log):
        # Cut off everything before 2026-06-23: only the two 06-23 records remain.
        stats = te.triage(sample_log, since=datetime(2026, 6, 23))
        meta = stats.pop('_meta')
        assert meta['skipped_pre_cutoff'] == 3
        labels = set(stats)
        assert 'Traceback: sqlite3.OperationalError' in labels
        assert 'Traceback: mcp.shared.exceptions.McpError' in labels
        assert 'Traceback: RuntimeError' not in labels

    def test_missing_log(self, tmp_path):
        stats = te.triage(str(tmp_path / 'nope.log'), since=None)
        assert stats['_meta']['missing'] is True

    def test_render_smoke(self, sample_log):
        stats = te.triage(sample_log, since=None)
        out = te.render(stats, top_n=5)
        assert 'Error-log triage' in out
        assert 'Traceback: RuntimeError' in out


@pytest.mark.unit
class TestSince:
    def test_parse_since_units(self):
        assert te._parse_since('') is None
        assert te._parse_since('24h') is not None
        with pytest.raises(ValueError):
            te._parse_since('banana')
