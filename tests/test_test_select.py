"""P2-3 针:scripts/test_select.py 的索引与选择逻辑(纯函数,不依赖真仓库)。

设计稿 docs/TESTING_STRATEGY.md §4。单元层全量实测 19m58s 超 15min 阈值,
迭代内环改「静态反向索引选择」:测试文件 → 它引用的源文件(AST import +
字面量路径),改动 ∩ 引用 = 入选。不上 ML(透明映射可审计,15k 规模用不上
预测模型)。本套件钉纯函数语义;脚本加载走 spec_from_file_location(scripts/
无 __init__.py,与 server.py 同款载入)。
"""

import importlib.util
import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_SCRIPT = os.path.join(ROOT, 'scripts', 'test_select.py')


def _load():
    spec = importlib.util.spec_from_file_location('test_select', _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ts = _load()


def _p(head, rest):
    """Synthetic fixture path, built in TWO segments on purpose.

    The paths below point NOWHERE — they are inputs/expected-outputs of the
    pure refs-extraction/selection functions, not references to repo files.
    Written as 'dir' + 'rest' so the suite-health census (_PATH_RE dead-anchor
    scan, scripts/audit_tests.py category F) does not mistake them for real
    stale anchors — a plain 'lib/foo/bar.py' literal would be flagged as a
    missing-path reference, which is exactly what these strings are NOT.
    """
    return head + '/' + rest


class TestRefsOfTestFile:
    def test_import_maps_to_repo_path(self):
        src = "import lib.foo.bar\nfrom routes.api_v1 import chat\n"
        refs = ts.refs_of_test_file(src)
        assert _p('lib', 'foo/bar.py') in refs
        assert (_p('routes', 'api_v1/__init__.py') in refs
                or _p('routes', 'api_v1.py') in refs)

    def test_from_import_of_module_maps_to_module_path(self):
        src = "from lib.tasks_pkg.manager import get_task\n"
        refs = ts.refs_of_test_file(src)
        assert (_p('lib', 'tasks_pkg/manager.py') in refs
                or _p('lib', 'tasks_pkg/manager/__init__.py') in refs)

    def test_tests_helper_import_maps(self):
        src = "from tests._jsdom import run_harness\n"
        refs = ts.refs_of_test_file(src)
        assert 'tests/_jsdom.py' in refs

    def test_literal_path_refs_captured(self):
        src = "JS = os.path.join(ROOT, 'frontend', 'src', 'main.ts')\n" \
              "X = 'frontend/src/api/transport.ts'\n" \
              "DOC = 'docs/API_CONTRACT.md'\n"
        refs = ts.refs_of_test_file(src)
        assert _p('frontend', 'src/api/transport.ts') in refs
        assert _p('docs', 'API_CONTRACT.md') in refs

    def test_external_packages_not_mapped(self):
        src = "import pytest\nimport tofu_search\nimport requests\n"
        refs = ts.refs_of_test_file(src)
        assert refs == set() or all(
            r.startswith(('lib/', 'routes/', 'tests/', 'static/', 'docs/'))
            for r in refs)


class TestSelectTests:
    A = _p('tests', 'test_a.py')
    B = _p('tests', 'test_b.py')
    C = _p('tests', 'test_frontend_c.py')
    D = _p('tests', 'test_d.py')
    LIB_X = _p('lib', 'x.py')
    LIB_W = _p('lib', 'w.py')
    UI_Y = _p('frontend', 'src/features/y.ts')
    CORE_Z = _p('frontend', 'src/runtime/app-runtime.js')
    JSDOM_HELPER = _p('tests', '_jsdom.py')
    API_JS = _p('frontend', 'src/api/transport.ts')
    CONFTEST = _p('tests', 'conftest.py')
    INDEX = {
        A: {LIB_X},
        B: {UI_Y},
        C: {CORE_Z},
        D: {LIB_X, LIB_W},
    }

    def test_changed_source_selects_importers(self):
        selected, _ = ts.select_tests(self.INDEX, [self.LIB_X])
        assert selected >= {self.A, self.D}
        assert self.B not in selected

    def test_changed_test_file_selects_itself(self):
        selected, _ = ts.select_tests(self.INDEX, [self.B])
        assert self.B in selected

    def test_blast_radius_jsdom_helper_pulls_frontend_family(self):
        selected, _ = ts.select_tests(self.INDEX, [self.JSDOM_HELPER])
        assert self.C in selected

    def test_blast_radius_api_transport_pulls_frontend_family(self):
        selected, _ = ts.select_tests(self.INDEX, [self.API_JS])
        assert self.C in selected

    def test_guard_core_always_runs(self):
        selected, _ = ts.select_tests(self.INDEX, [self.LIB_X])
        assert any('contract' in os.path.basename(f) for f in selected), (
            'guard core must always be in the selection')

    def test_unknown_change_falls_back_to_guard_core(self):
        selected, reasons = ts.select_tests(self.INDEX, ['README.md'])
        direct = {f for f in selected if f in self.INDEX}
        assert not direct, 'unrelated change must not pull behaviour suites'
        assert selected, 'guard core must still run as the smoke floor'

    def test_conftest_change_selects_everything(self):
        selected, _ = ts.select_tests(self.INDEX, [self.CONFTEST])
        assert selected >= set(self.INDEX), (
            'conftest touches every session — the blast radius is the whole suite')


class TestPytestCommand:
    def test_parallel_command_explicitly_loads_xdist(self):
        cmd = ts.build_pytest_command([_p('tests', 'test_a.py')], '2')

        plugin = cmd.index('xdist.plugin')
        workers = cmd.index('-n')
        assert cmd[plugin - 1] == '-p'
        assert plugin < workers
        assert cmd[workers + 1] == '2'

    def test_serial_command_does_not_load_xdist_or_pass_worker_flags(self):
        cmd = ts.build_pytest_command(None, '0')

        assert 'xdist.plugin' not in cmd
        assert '-n' not in cmd
        assert cmd[-5:-3] == ['-m', 'unit']

    def test_implicit_single_file_selection_stays_serial(self):
        cmd = ts.build_pytest_command([_p('tests', 'test_a.py')], None)

        assert 'xdist.plugin' not in cmd
        assert '-n' not in cmd

    def test_implicit_multi_file_selection_caps_workers_at_make_default(self):
        selected = [f'tests/test_{i}.py' for i in range(10)]
        cmd = ts.build_pytest_command(selected, None)

        assert cmd[cmd.index('-n') + 1] == '4'
