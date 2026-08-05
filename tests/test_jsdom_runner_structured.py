"""P0-1/P0-2 针：jsdom runner 结构化断言地板 + skip 响亮化。

设计稿 docs/TESTING_STRATEGY.md §4。覆盖：

* ``parse_harness_result`` — 结构化尾行优先；无尾行走「PASS 行锚定」计数，
  绝杀旧的 ``output.count('PASS')`` 子串膨胀（"BYPASS"/"PASSword" 曾计为 PASS）。
* ``skip_or_fail`` / ``frontend_required`` — TOFU_REQUIRE_FRONTEND=1 时
  node/jsdom 缺席从 pytest.skip 变硬失败（skip 必须响亮）。
* ``is_frontend_dep_skip`` — conftest 会话末哨兵的分类器（纯逻辑可测）。
* ``run_harness(expect_pass=...)`` — 精确申报断言数，少了红、多了也红。

node -backed 用例在无 node 的车道按既有约定 skip（单元车道无 node）。
"""

import os

import pytest

from tests import _jsdom
from tests._jsdom import (
    JS_DIR,
    frontend_required,
    is_frontend_dep_skip,
    parse_harness_result,
    run_harness,
    skip_or_fail,
)

pytestmark = pytest.mark.unit

# run_harness 需要 target_js 作 argv[2]，但下面的 body 用 targets=[] 不 eval 它，
# 传任意真实路径即可。
_TARGET = os.path.join(JS_DIR, 'core', 'icons.js')

_BODY_TWO_CHECKS = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [],
  globals: {},
});
check('one', true);
check('two', 1 + 1 === 2);
report();
'''


class TestParseHarnessResult:
    def test_structured_trailer_authoritative(self):
        out = 'PASS a\nPASS b\n__JSDOM_RESULT__ {"pass": 2, "fail": 0}'
        assert parse_harness_result(out) == (2, 0, True)

    def test_structured_fail_count(self):
        out = 'PASS a\nFAIL b\n__JSDOM_RESULT__ {"pass": 1, "fail": 1}'
        assert parse_harness_result(out) == (1, 1, True)

    def test_trailer_wins_over_line_count(self):
        # 尾行是 check() 的权威计数；行打印只是渲染。
        out = 'PASS a\n__JSDOM_RESULT__ {"pass": 3, "fail": 0}'
        assert parse_harness_result(out) == (3, 0, True)

    def test_legacy_line_anchored_not_substring(self):
        # 'BYPASS' / 'PASSword' 里的 PASS 子串绝不可计数（旧 count('PASS') 的洞）。
        out = 'note: BYPASS gate\nPASSword reset\nPASS real\n'
        assert parse_harness_result(out) == (1, 0, False)

    def test_empty_output_is_zero(self):
        assert parse_harness_result('') == (0, 0, False)


class TestSkipOrFail:
    def test_skips_when_not_required(self, monkeypatch):
        monkeypatch.delenv('TOFU_REQUIRE_FRONTEND', raising=False)
        with pytest.raises(pytest.skip.Exception):
            skip_or_fail('node + jsdom dev-deps not installed')

    def test_fails_loudly_when_required(self, monkeypatch):
        monkeypatch.setenv('TOFU_REQUIRE_FRONTEND', '1')
        with pytest.raises(pytest.fail.Exception, match='TOFU_REQUIRE_FRONTEND'):
            skip_or_fail('node + jsdom dev-deps not installed')

    @pytest.mark.parametrize('val', ['1', 'true', 'yes', 'on'])
    def test_required_truthy_variants(self, monkeypatch, val):
        monkeypatch.setenv('TOFU_REQUIRE_FRONTEND', val)
        assert frontend_required() is True

    @pytest.mark.parametrize('val', ['0', '', 'false', 'off'])
    def test_required_falsy_variants(self, monkeypatch, val):
        monkeypatch.setenv('TOFU_REQUIRE_FRONTEND', val)
        assert frontend_required() is False


class TestFrontendDepSkipSentinel:
    @pytest.mark.parametrize(
        'nodeid, reason, expected',
        [
            ('tests/test_frontend_x.py::test_a', 'Skipped: node + jsdom dev-deps not installed', True),
            ('tests/test_frontend_x.py::test_a', 'Skipped: node not on PATH', True),
            ('tests/test_frontend_x.py::test_a', 'Skipped: tsc not available', True),
            ('tests/test_frontend_x.py::test_a', 'Skipped: npm install first', True),
            # 数据条件 skip 不归哨兵（防误红）
            ('tests/test_frontend_x.py::test_a', 'Skipped: no unsent run records in the live DB yet', False),
            # 非前端文件的 node skip 不归哨兵
            ('tests/test_api_integration.py::test_a', 'Skipped: node not on PATH', False),
        ],
    )
    def test_classification(self, nodeid, reason, expected):
        assert is_frontend_dep_skip(nodeid, reason) is expected


class TestFrontendModuleGuard:
    def test_fails_loud_when_required_and_node_gone(self, monkeypatch):
        monkeypatch.setenv('TOFU_REQUIRE_FRONTEND', '1')
        monkeypatch.setattr(_jsdom.shutil, 'which', lambda _name: None)
        with pytest.raises(pytest.fail.Exception, match='TOFU_REQUIRE_FRONTEND'):
            _jsdom.frontend_module_guard()

    def test_skips_cleanly_when_not_required_and_node_gone(self, monkeypatch):
        monkeypatch.delenv('TOFU_REQUIRE_FRONTEND', raising=False)
        monkeypatch.setattr(_jsdom.shutil, 'which', lambda _name: None)
        with pytest.raises(pytest.skip.Exception):
            _jsdom.frontend_module_guard()

    def test_passes_silently_when_node_present(self, monkeypatch):
        monkeypatch.setattr(_jsdom.shutil, 'which', lambda _name: '/usr/bin/node')
        _jsdom.frontend_module_guard()  # must not raise

    def test_jsdom_variant_checks_jsdom_dir(self, monkeypatch):
        monkeypatch.delenv('TOFU_REQUIRE_FRONTEND', raising=False)
        monkeypatch.setattr(_jsdom.shutil, 'which', lambda _name: '/usr/bin/node')
        monkeypatch.setattr(_jsdom.os.path, 'isdir', lambda _p: False)
        with pytest.raises(pytest.skip.Exception):
            _jsdom.frontend_module_guard(need_jsdom=True)


class TestRunHarnessExpectPass:
    def test_exact_match_green_and_trailer_visible(self):
        out = run_harness(_TARGET, _BODY_TWO_CHECKS, expect_pass=2)
        assert '__JSDOM_RESULT__' in out

    def test_neuter_declaring_more_than_actual_is_red(self):
        # NEUTER:申报 3 实际 2 必须红——这正是「地板虚报」的反面锁。
        with pytest.raises(AssertionError, match='expect_pass=3'):
            run_harness(_TARGET, _BODY_TWO_CHECKS, expect_pass=3)

    def test_neuter_declaring_fewer_than_actual_is_red(self):
        # 申报少于实际也红：精确语义,防「申报 1 躺平」。
        with pytest.raises(AssertionError, match='expect_pass=1'):
            run_harness(_TARGET, _BODY_TWO_CHECKS, expect_pass=1)

    def test_structured_fail_count_red_even_without_fail_lines(self):
        body = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, out, report } = setup({
  root: process.argv[3], html: '<!DOCTYPE html><body></body>',
  targets: [], globals: {},
});
check('bad', false);
out.length = 0;  // 抹掉 FAIL 行,只剩尾行计数——尾行也必须咬人
report();
'''
        with pytest.raises(AssertionError):
            run_harness(_TARGET, body, expect_pass=1)


class TestRunHarnessLegacyPath:
    def test_legacy_body_without_trailer_counts_pass_lines(self):
        out = run_harness(_TARGET, "console.log('PASS a');\nconsole.log('PASS b');", min_pass=2)
        assert 'PASS a' in out

    def test_neuter_substring_inflation_killed(self):
        # 旧实现 count('PASS') 会把这行当 2 个 PASS → 绿;行锚定后必红。
        with pytest.raises(AssertionError):
            run_harness(_TARGET, "console.log('BYPASS note PASSword');", min_pass=1)
