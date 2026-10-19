"""Native extraction through canonical transient repository tool publication."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

from native_domain_worker import CASES, OWNER, Workload, digest, require_weighted_ranking
from native_extraction_scan import NativeBatchScanner
from lib.repo_map import outline
from lib.tasks_pkg.manager.runtime import chat_task_runtime


class Scope:
    def __init__(self, root, owner, scanner):
        self.root, self.owner, self.scanner = root, owner, scanner

    def scan(self, owner, **options):
        if owner != self.owner:
            raise PermissionError('native extraction belongs to another owner')
        return self.scanner(self.root, **options)


def check(binary, grammar_directory, *, scanner_type=NativeBatchScanner, initialize_git=False):
    def initialize(root):
        if initialize_git:
            subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
    traces = {}
    calls = 0
    with tempfile.TemporaryDirectory(prefix='tofu-native-extraction-tool-') as temporary, require_weighted_ranking():
        base = Path(temporary)
        (base / 'foreign').mkdir()
        for case in CASES:
            root = base / case
            root.mkdir()
            initialize(root)
            reference, candidate = Workload(root, case), Workload(root, case)
            scanner = scanner_type(binary, grammar_directory)
            try:
                for options in ({}, dict(focus='MODULE_1'), dict(path='module_1.js'),
                                dict(include_refs=False), dict(include_tests=True)):
                    assert asdict(scanner(str(root), **options)) == asdict(outline.scan(str(root), **options))
                rows = []
                for index in range(2):
                    expected = reference.operation(index)
                    if candidate.task is None:
                        candidate.start()
                        candidate.port.scope = Scope(root, OWNER, scanner)
                    actual = candidate.operation(index)
                    assert actual == expected, (case, index, actual, expected)
                    rows.append(actual)
                if case != 'cold_scan':
                    events = chat_task_runtime.poll(candidate.task['id'])['events']
                    assert len([event for event in events if event['type'] == 'tool_result']) == 2
                    statuses = []
                    calls_before_refusal = scanner.calls
                    refused = candidate.port.invoke(candidate.task, {}, lambda *_args, **kw: statuses.append(kw['status']),
                                                    project_root=base / 'foreign')
                    assert refused[2] and statuses == ['error']
                    assert scanner.calls == calls_before_refusal
                    try:
                        candidate.port.scope.scan(OWNER + 1)
                    except PermissionError:
                        pass
                    else:
                        raise AssertionError('foreign owner reached native extraction')
                traces[case] = digest(rows)
            finally:
                primary, cleanup_errors = sys.exception(), []
                for work in (candidate, reference):
                    try:
                        work.close()
                    except BaseException as error:
                        cleanup_errors.append(error)
                if cleanup_errors:
                    if primary is not None:
                        primary.add_note('tool check cleanup failed: ' + ', '.join(type(error).__name__ for error in cleanup_errors))
                    else:
                        raise BaseExceptionGroup('tool check cleanup failed', cleanup_errors)
            assert candidate.runtime.task_count == reference.runtime.task_count == 0
            if case == 'python_extraction':
                assert scanner.calls == 0
            else:
                assert scanner.calls > 0
            calls += scanner.calls

        def concurrent(index):
            root = base / f'concurrent-{index}'
            root.mkdir()
            initialize(root)
            (root / 'module.js').write_text(f'function ownerSymbol{index}() {{ return {index}; }}')
            scanner = scanner_type(binary, grammar_directory)
            assert asdict(scanner(str(root))) == asdict(outline.scan(str(root)))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(concurrent, range(16)))

        # Canonical source admission and expected file-local grammar failures.
        boundary = base / 'admission'
        boundary.mkdir()
        initialize(boundary)
        (boundary / 'healthy.py').write_text('def healthy():\n    return referenced\n')
        (boundary / 'broken.rs').write_text('fn broken() {}\n')
        (boundary / 'upper.PY').write_text('def uppercase(): pass\n')
        (boundary / 'generated.min.js').write_text('function ignoredGenerated() {}')
        (boundary / 'data.json').write_text('{"ignored":1}')
        (boundary / 'large.js').write_text('function not_admitted() {}\n' + ' ' * outline._MAX_SOURCE_BYTES)
        (boundary / 'tests').mkdir()
        (boundary / 'tests/spec.js').write_text('function testOnly() {}')
        grammar_fault = base / 'grammar-fault'
        grammar_fault.mkdir()
        shutil.copyfile(grammar_directory / 'libtree_sitter_javascript.so', grammar_fault / 'libtree_sitter_javascript.so')
        (grammar_fault / 'libtree_sitter_rust.so').write_bytes(b'broken grammar')
        import grep_ast.tsl
        original_parser = grep_ast.tsl.get_parser
        def oracle_parser(language):
            if language == 'rust':
                raise RuntimeError('injected same missing Rust grammar')
            return original_parser(language)
        scanner = scanner_type(binary, grammar_fault, strict=False)
        for options in ({}, dict(include_tests=True), dict(include_refs=False), dict(focus='HEALTHY')):
            with patch.object(grep_ast.tsl, 'get_parser', oracle_parser):
                expected = outline.scan(str(boundary), **options)
            assert asdict(scanner(str(boundary), **options)) == asdict(expected)
        try:
            scanner_type(binary, grammar_fault)(str(boundary))
        except RuntimeError:
            pass
        else:
            raise AssertionError('strict measurement accepted a missing grammar')
    return dict(format='tofu.native-extraction-tool-oracle/v1', cases=traces,
                native_scan_calls=calls, candidate_successful_tool_publications=10,
                reference_successful_tool_publications=10, candidate_refusal_publications=4,
                independent_concurrent_roots=16, foreign_owner_refused=True,
                foreign_root_refused=True, joined_cleanup=True,
                admission_and_file_local_failure_parity=True, missing_grammar_measurement_refused=True,
                performance_admission=False, default_selection_admitted=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--grammar-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = check(args.binary, args.grammar_dir)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
