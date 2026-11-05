"""Generation consumes one revision of each input and publishes only complete output.

Exercise the public generator entry with controlled file I/O. JSON contracts
remain the real input; generated files are captured, never changed by tests.
"""
import ast
from collections import Counter
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

from scripts import gen_tofudb_ir_contract as generator

pytestmark = pytest.mark.unit


def run_generation(change=None, *, check=False, existing=None, published=None):
    reads = Counter()
    writes = {} if published is None else published
    read_text, exists = Path.read_text, Path.exists

    def read(path, *args, **kwargs):
        if existing is not None and path in existing:
            return existing[path]
        content = read_text(path, *args, **kwargs)
        if path.parent == generator.ROOT / 'contracts':
            reads[path.name] += 1
            if change:
                content = json.dumps(change(path.name, json.loads(content), reads[path.name]))
        return content

    def write(path, content, **_kwargs):
        assert path.is_relative_to(generator.ROOT)
        writes[path] = content
        return len(content)

    def present(path):
        return existing[path] is not None if existing is not None and path in existing else exists(path)

    with patch.object(Path, 'read_text', read), patch.object(Path, 'write_text', write), \
            patch.object(Path, 'exists', present), patch.object(sys, 'argv', ['generator', *(['--check'] if check else [])]):
        result = generator.main()
    return result, reads, writes


def receipt_values(outputs):
    python = outputs[generator.ROOT / 'lib/storage_authority_sql/generated_storage_receipts.py']
    values = {node.targets[0].id: ast.literal_eval(node.value)
              for node in ast.parse(python).body if isinstance(node, ast.Assign)}
    rust = outputs[generator.OUTPUT]
    return values, rust


def test_each_contract_is_read_once_per_generation():
    result, reads, outputs = run_generation()
    assert result == 0 and len(outputs) == 8
    assert reads and all(count == 1 for count in reads.values()), reads


def test_cross_language_receipts_share_one_input_revision():
    def changing_revision(name, document, count):
        if name == 'tofudb_ir_v1.json' and count > 1:
            document['commandReceipts']['maximumInlineBytes'] += 1
        return document

    result, _, outputs = run_generation(changing_revision)
    values, rust = receipt_values(outputs)
    assert result == 0
    assert f'pub const MAX_INLINE_RECEIPT_BYTES: usize = {values["MAX_INLINE_RECEIPT_BYTES"]};' in rust


def test_a_new_generation_reads_new_contract_content():
    def revision(name, document, _count):
        if name == 'tofudb_ir_v1.json':
            document['commandReceipts']['maximumInlineBytes'] += 1
        return document

    _, _, before = run_generation()
    result, _, after = run_generation(revision)
    old_values, _ = receipt_values(before)
    values, rust = receipt_values(after)
    assert result == 0
    assert values['MAX_INLINE_RECEIPT_BYTES'] == old_values['MAX_INLINE_RECEIPT_BYTES'] + 1
    assert f'pub const MAX_INLINE_RECEIPT_BYTES: usize = {values["MAX_INLINE_RECEIPT_BYTES"]};' in rust


@pytest.mark.parametrize('fault', ['missing', 'stale'])
def test_check_mode_reports_drift_without_writing(fault, capsys):
    _, _, existing = run_generation()
    existing[generator.OUTPUT] = None if fault == 'missing' else 'stale output\n'
    result, _, writes = run_generation(check=True, existing=existing)
    assert result == 1 and not writes
    assert 'generated_tofudb_ir.rs is stale' in capsys.readouterr().out


def test_check_mode_accepts_all_current_outputs_without_writing():
    _, _, existing = run_generation()
    result, _, writes = run_generation(check=True, existing=existing)
    assert result == 0 and not writes


@pytest.mark.parametrize('contract', [
    'tofudb_ir_v1.json', 'storage_operations_v1.json',
    'conversation_search_v2.json', 'worker_job_lifecycle_v1.json',
    'paper_build_v1.schema.json', 'automation_v1.schema.json',
])
def test_invalid_contract_never_publishes_any_output(contract):
    def invalidate(name, document, _count):
        if name != contract:
            return document
        if name in ('tofudb_ir_v1.json', 'conversation_search_v2.json'):
            document['format'] = 'unsupported-format'
        elif name == 'storage_operations_v1.json':
            document['operations'] = []
        elif name == 'worker_job_lifecycle_v1.json':
            document['terminalStates'] = []
        elif name == 'paper_build_v1.schema.json':
            document['$defs']['PaperBuildReceiptV1']['additionalProperties'] = True
        else:
            document['x-runtime']['terminalRunStates'] = []
        return document

    published = {}
    with pytest.raises(ValueError):
        run_generation(invalidate, published=published)
    assert not published


def test_input_read_failure_propagates_before_publication():
    def unavailable(name, document, _count):
        if name == 'automation_actions_v1.json':
            raise OSError('synthetic contract read failure')
        return document

    published = {}
    with pytest.raises(OSError, match='synthetic contract read failure'):
        run_generation(unavailable, published=published)
    assert not published


def test_failed_generation_does_not_poison_the_next_invocation():
    def invalid_worker(name, document, _count):
        if name == 'worker_job_lifecycle_v1.json':
            document['terminalStates'] = []
        return document

    with pytest.raises(ValueError, match='invalid canonical worker lifecycle'):
        run_generation(invalid_worker)
    result, reads, outputs = run_generation()
    assert result == 0 and len(outputs) == 8
    assert reads['worker_job_lifecycle_v1.json'] == 1
