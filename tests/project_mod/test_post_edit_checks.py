"""Executable specification for the report-only post-edit checks
(lib/project_mod/write_tools/_post_edit_checks.py).

The product contract is accuracy first: a warning fires ONLY when the check
is confident the edit introduced the defect. Every inconclusive situation —
missing toolchain, unknown flags, pre-existing breakage, include resolution,
timeout — must stay silent.
"""

import json
import shutil
import subprocess

import pytest

from lib.project_mod.write_tools import _post_edit_checks as checks
from lib.project_mod.write_tools._ops import tool_edit_file, tool_write_file

HAS_CC = shutil.which('cc') is not None
HAS_CXX = shutil.which('c++') is not None
needs_cc = pytest.mark.skipif(not HAS_CC, reason='no C compiler on this host')
needs_cxx = pytest.mark.skipif(not HAS_CXX, reason='no C++ compiler on this host')


# ─────────────────────────────────────────────────────────────────────
#  JSONC tolerant parsing (pure)
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_blank_jsonc_strips_line_and_block_comments():
    text = '{\n  // note\n  "a": 1, /* inline */ "b": 2\n}'
    ok, err = checks._json_parses(text)
    assert ok and err is None


@pytest.mark.unit
def test_blank_jsonc_ignores_comment_tokens_inside_strings():
    text = '{"url": "http://x//y", "glob": "/*", "esc": "a\\"//b"}'
    ok, _ = checks._json_parses(text)
    assert ok
    # A real syntax error after the string must still be caught.
    bad = '{"url": "http://x" "b": 1}'
    ok, err = checks._json_parses(bad)
    assert not ok and err.lineno == 1


@pytest.mark.unit
def test_blank_jsonc_tolerates_trailing_commas_but_not_string_commas():
    assert checks._json_parses('{"a": [1, 2,], "b": 3,}')[0]
    # A comma before } INSIDE a string is content, not a trailing comma.
    assert checks._json_parses('{"a": ",}"}')[0]


@pytest.mark.unit
def test_blank_jsonc_block_comment_preserves_line_numbers():
    text = '{\n/* multi\nline */\n"x": 1 oops\n}'
    ok, err = checks._json_parses(text)
    assert not ok
    assert err.lineno == 4


# ─────────────────────────────────────────────────────────────────────
#  JSON check (report-only, regression-gated)
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_json_check_warns_on_new_broken_file():
    out = checks.post_edit_check('cfg.json', None, '{"a": 1,,}')
    assert out.startswith('JSON CHECK:')
    assert 'cfg.json' in out and 'created' in out


@pytest.mark.unit
def test_json_check_warns_only_on_regression():
    assert checks.post_edit_check('cfg.json', '{"a": 1}', '{"a": 1,,}')
    assert checks.post_edit_check('cfg.json', '{"a": 1,,}', '{"a": 1,,,}') == ''


@pytest.mark.unit
def test_json_check_silent_for_jsonc_dialect_files():
    tsconfig = '{\n  // compiler options\n  "compilerOptions": {\n    "strict": true,\n  },\n}'
    assert checks.post_edit_check('tsconfig.json', None, tsconfig) == ''
    assert checks.post_edit_check('settings.jsonc', None, tsconfig) == ''


@pytest.mark.unit
def test_json_check_ignores_jsonl_and_other_extensions():
    assert checks.post_edit_check('data.jsonl', None, '{"a":}\n{"b":}') == ''
    assert checks.post_edit_check('notes.txt', None, '{"a":}') == ''


@pytest.mark.unit
def test_post_edit_check_kill_switch(monkeypatch):
    monkeypatch.setenv('TOFU_EDIT_SYNTAX_GUARD', '0')
    assert checks.post_edit_check('cfg.json', None, '{"a":}') == ''


# ─────────────────────────────────────────────────────────────────────
#  C/C++ diagnostic classification (pure)
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_classify_reports_stdin_errors_only():
    stderr = ('<stdin>:3:5: error: expected \';\' after expression\n'
              'hdr.h:9:1: error: unknown type name \'x\'\n')
    out = checks._classify_c_diagnostics(stderr, 1)
    assert out == [(3, "expected ';' after expression")]


@pytest.mark.unit
def test_classify_clean_on_success_or_only_header_errors():
    assert checks._classify_c_diagnostics('', 0) == []
    assert checks._classify_c_diagnostics('<stdin>:1:1: warning: w\n', 0) == []
    header_only = 'hdr.h:9:1: error: unknown type name \'x\'\n'
    assert checks._classify_c_diagnostics(header_only, 1) == []


@pytest.mark.unit
def test_classify_inconclusive_on_environment_failures():
    assert checks._classify_c_diagnostics(
        "<stdin>:1:10: fatal error: 'foo.h' file not found\n", 1) is None
    assert checks._classify_c_diagnostics(
        'fatal error: foo.h: No such file or directory\n', 1) is None
    assert checks._classify_c_diagnostics(
        "cc: error: unrecognized command-line option '-fno-bar'\n", 1) is None
    assert checks._classify_c_diagnostics(
        "<stdin>:4:2: error: #error Unsupported platform\n", 1) is None
    assert checks._classify_c_diagnostics('', 1) is None


# ─────────────────────────────────────────────────────────────────────
#  compile_commands argument sanitizing (pure)
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_sanitize_db_args_keeps_include_flags_drops_outputs():
    args = ['/usr/bin/c++', '-I', 'include', '-DFOO=1', '-std=c++17',
            '-c', '-o', 'main.o', '-MF', 'main.d', '-MMD', 'src/main.cpp']
    out = checks._sanitize_db_args(args, '/proj', '/proj/src/main.cpp')
    assert out == ['-I', 'include', '-DFOO=1', '-std=c++17']


@pytest.mark.unit
def test_sanitize_db_args_rejects_response_files():
    assert checks._sanitize_db_args(['cc', '@rsp.txt', 'a.c'], '/p', '/p/a.c') is None


# ─────────────────────────────────────────────────────────────────────
#  C/C++ check against a real compiler (host capability permitting)
# ─────────────────────────────────────────────────────────────────────

@needs_cc
@pytest.mark.unit
def test_c_check_warns_on_new_file_with_syntax_error(tmp_path):
    target = tmp_path / 'bad.c'
    target.write_text('int main(void) { return 0;;;\n', encoding='utf-8')
    broken = 'int main(void) { return 0\n}\n'  # missing ';'
    out = checks.post_edit_check('bad.c', None, broken, abs_path=str(target))
    assert out.startswith('COMPILE CHECK:')
    # GCC points at the '}' on line 2 where the missing ';' becomes visible.
    assert 'bad.c' in out and 'created' in out and 'line 2' in out


@needs_cc
@pytest.mark.unit
def test_c_check_regression_gate(tmp_path):
    target = tmp_path / 'x.c'
    target.write_text('int a = 1;\n', encoding='utf-8')
    clean = 'int a = 1;\n'
    broken = 'int a = ;\n'
    # clean → broken: report
    assert 'COMPILE CHECK' in checks.post_edit_check(
        'x.c', clean, broken, abs_path=str(target))
    # already broken before: silent
    assert checks.post_edit_check(
        'x.c', broken, 'int a = ;;\n', abs_path=str(target)) == ''
    # clean → clean: silent
    assert checks.post_edit_check(
        'x.c', clean, 'int a = 2;\n', abs_path=str(target)) == ''


@needs_cc
@pytest.mark.unit
def test_c_check_silent_when_include_cannot_resolve(tmp_path):
    target = tmp_path / 'uses_dep.c'
    target.write_text('', encoding='utf-8')
    text = '#include "nonexistent/dep.h"\nint broken = ;\n'
    assert checks.post_edit_check('uses_dep.c', None, text,
                                  abs_path=str(target)) == ''


@needs_cc
@pytest.mark.unit
def test_c_check_silent_for_ambiguous_header_without_db(tmp_path):
    target = tmp_path / 'lib.h'
    target.write_text('', encoding='utf-8')
    assert checks.post_edit_check('lib.h', None, 'int broken = ;\n',
                                  abs_path=str(target)) == ''


@needs_cc
@pytest.mark.unit
def test_c_check_uses_compile_commands_include_flags(tmp_path):
    inc = tmp_path / 'include'
    inc.mkdir()
    (inc / 'dep.h').write_text('#define DEP 1\n', encoding='utf-8')
    src = tmp_path / 'main.c'
    src.write_text('', encoding='utf-8')
    db = [{
        'directory': str(tmp_path),
        'arguments': ['cc', '-I', 'include', '-c', '-o', 'main.o', 'main.c'],
        'file': 'main.c',
    }]
    (tmp_path / 'compile_commands.json').write_text(
        json.dumps(db), encoding='utf-8')
    broken = '#include "dep.h"\nint broken = ;\n'
    # With the db's -I flag the include resolves, so the real syntax error
    # is attributable — reported.
    out = checks.post_edit_check('main.c', None, broken, abs_path=str(src))
    assert out.startswith('COMPILE CHECK:') and 'line 2' in out
    # Without the db the same text is inconclusive (missing include) → silent.
    (tmp_path / 'compile_commands.json').unlink()
    checks._CCDB_CACHE.clear()
    assert checks.post_edit_check('main.c', None, broken,
                                  abs_path=str(src)) == ''


@needs_cxx
@pytest.mark.unit
def test_cpp_check_hpp_extension(tmp_path):
    target = tmp_path / 'vec.hpp'
    target.write_text('', encoding='utf-8')
    broken = '#include <vector>\nstd::vector<int> f( {\n'
    out = checks.post_edit_check('vec.hpp', None, broken, abs_path=str(target))
    assert out.startswith('COMPILE CHECK:')


@pytest.mark.unit
def test_c_check_timeout_is_inconclusive(monkeypatch, tmp_path):
    monkeypatch.setattr(checks, '_compiler_for', lambda *a, **k: '/usr/bin/cc')

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd='cc', timeout=5)
    monkeypatch.setattr(checks.subprocess, 'run', boom)
    assert checks.post_edit_check('x.c', None, 'int broken = ;\n',
                                  abs_path=str(tmp_path / 'x.c')) == ''


# ─────────────────────────────────────────────────────────────────────
#  End-to-end through the write/edit tools
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_write_file_reports_invalid_json(tmp_path):
    result = tool_write_file(str(tmp_path), 'cfg.json', '{"a": 1,,}')
    assert result['ok']
    assert result['syntaxWarning'].startswith('JSON CHECK:')


@pytest.mark.unit
def test_write_file_valid_json_has_no_warning(tmp_path):
    result = tool_write_file(str(tmp_path), 'cfg.json', '{"a": 1}\n')
    assert result['ok']
    assert 'syntaxWarning' not in result


@needs_cc
@pytest.mark.unit
def test_edit_file_reports_c_compile_regression(tmp_path):
    (tmp_path / 'x.c').write_text('int a = 1;\n', encoding='utf-8')
    result = tool_edit_file(str(tmp_path), [{
        'path': 'x.c', 'operation': 'replace',
        'anchor': 'int a = 1;', 'content': 'int a = ;',
    }])
    assert result.startswith('Applied 1/1 edits')
    assert 'COMPILE CHECK:' in result
    assert 'x.c' in result


@needs_cc
@pytest.mark.unit
def test_edit_file_silent_when_c_file_was_already_broken(tmp_path):
    (tmp_path / 'x.c').write_text('int a = ;\n', encoding='utf-8')
    result = tool_edit_file(str(tmp_path), [{
        'path': 'x.c', 'operation': 'replace',
        'anchor': 'int a = ;', 'content': 'int a = ;;',
    }])
    assert result.startswith('Applied 1/1 edits')
    assert 'COMPILE CHECK:' not in result
