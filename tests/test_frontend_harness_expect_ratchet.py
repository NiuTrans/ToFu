"""P0-2 棘轮：``run_harness`` 调用点未申报 ``expect_pass`` 的数量只降不升。

设计稿 docs/TESTING_STRATEGY.md §4。owner 拍板：每套件申报期望断言数，
少于即红（``expect_pass`` 精确语义）。存量 78 个调用点（53 文件，2026-08-04
AST 实测）走收敛——本基线只准往下拧；新增 jsdom 套件必须带
``expect_pass=N``，否则本测试红并指名文件：行号。
"""

import ast
import glob
import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))

# 2026-08-04 钉:AST 实测 78 个未申报调用点 / 53 个文件。迁移一批就往下拧。
BASELINE_UNDECLARED_CALL_SITES = 78


def find_undeclared_call_sites(source, filename='<src>'):
    """Return the 1-based line numbers of ``run_harness(...)`` calls that do
    NOT pass ``expect_pass=``. Matches both bare and attribute calls
    (``run_harness(...)`` / ``_jsdom.run_harness(...)``)."""
    tree = ast.parse(source, filename)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            continue
        if name != 'run_harness':
            continue
        if not any(kw.arg == 'expect_pass' for kw in node.keywords):
            hits.append(node.lineno)
    return hits


def test_neuter_scanner_bites_on_synthetic_source():
    """NEUTER:合成源里未申报/已申报/属性调用三种形态必须分类正确——
    扫描器若瞎了,本针先红。"""
    src = (
        "run_harness('a', body)\n"                        # line 1: 未申报 → 咬
        "run_harness('a', body, min_pass=2)\n"            # line 2: 只有弱地板 → 咬
        "run_harness('a', body, expect_pass=2)\n"         # line 3: 已申报 → 放
        "_jsdom.run_harness('a', body)\n"                 # line 4: 属性调用未申报 → 咬
    )
    assert find_undeclared_call_sites(src) == [1, 2, 4]


def test_undeclared_call_sites_only_shrink():
    offenders = {}
    total = 0
    for path in sorted(glob.glob(os.path.join(HERE, 'test_frontend_*.py'))):
        if os.path.abspath(path) == os.path.abspath(__file__):
            continue
        with open(path, encoding='utf-8') as fh:
            hits = find_undeclared_call_sites(fh.read(), path)
        if hits:
            offenders[os.path.basename(path)] = hits
            total += len(hits)
    assert total <= BASELINE_UNDECLARED_CALL_SITES, (
        f'undeclared run_harness call sites rose to {total} '
        f'(baseline {BASELINE_UNDECLARED_CALL_SITES}). New jsdom suites must '
        f'declare expect_pass=N (see docs/TESTING_STRATEGY.md §6). '
        f'Offenders: {offenders}'
    )
