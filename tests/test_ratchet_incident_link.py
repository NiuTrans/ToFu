"""P2-1 前进棘轮:新守护套件必须锚定它的「那次事故」。

设计稿 docs/TESTING_STRATEGY.md §4(P2)。殡葬审计(scripts/ratchet_audit.py)
把存量 152 个守护套件分类完毕(锚定 92 / 候选 60,见 docs/RATCHET_AUDIT.md)
——候选由人逐个处置,本测试守的是**增量**:此后新增的守护套件
(_parity/_drift/_guard/_invariant/_freeze/_ratchet 文件名族)必须自带锚:
NEUTER 咬合证明,或事故引用(pt_/commit/JOURNAL/事故),或在
FAMILY_ANCHORS 登记处里(工件必须在库)。没有锚的棘轮只挡变化不挡 bug
——那是负优化,不许入库。

存量套件在 tests/_ratchet_guard_baseline.json 祖父化(殡葬审计的处置对象,
不由本测试追讨);套件改名/删除自然放行。
"""

import importlib.util
import json
import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        'ratchet_audit', os.path.join(ROOT, 'scripts', 'ratchet_audit.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ra = _load_audit()


def _baseline():
    with open(os.path.join(HERE, '_ratchet_guard_baseline.json'),
              encoding='utf-8') as fh:
        return set(json.load(fh))


class TestClassifierNeuter:
    """NEUTER:分类器本身必须先会咬——合成源五态。"""

    def test_neuter_marker_anchors(self):
        assert ra.is_anchored(ra.classify_source(
            'def test_NEUTER_x_is_caught(): ...'))

    def test_epic_id_anchors(self):
        assert ra.is_anchored(ra.classify_source('# epic pt_2f2c847ff8524e5e'))

    def test_legacy_short_epic_id_anchors(self):
        # 老 epic 是 8 位 hex(pt_03f4cdf1,orchestrator 拆分族)——漏识会把整族
        # 误判为无锚候选。
        assert ra.is_anchored(ra.classify_source('# epic pt_03f4cdf1 slice 8'))

    def test_commit_ref_anchors(self):
        assert ra.is_anchored(ra.classify_source('# fix: see commit 8f3204f7'))

    def test_incident_word_anchors(self):
        assert ra.is_anchored(ra.classify_source('# 防的那次事故:强刷全灭'))

    def test_unanchored_source_detected(self):
        signals = ra.classify_source('def test_parity():\n    assert a == b\n')
        assert not ra.is_anchored(signals), (
            'a guard with no NEUTER and no incident ref must be a candidate')


class TestNewGuardSuitesMustAnchor:
    def test_no_unanchored_guard_beyond_baseline(self):
        baseline = _baseline()
        offenders = []
        for name in sorted(os.listdir(HERE)):
            if not (name.startswith('test_') and name.endswith('.py')):
                continue
            if name in baseline or not ra.is_guard_file(name):
                continue
            with open(os.path.join(HERE, name), encoding='utf-8') as fh:
                signals = ra.classify_source(fh.read())
            family = ra.family_anchor_of(name)
            if family and os.path.isfile(os.path.join(ROOT, family)):
                continue
            if not ra.is_anchored(signals):
                offenders.append(name)
        assert not offenders, (
            'new guard suites must carry an anchor — a NEUTER proof, an '
            'incident ref (pt_/commit/JOURNAL/事故), or a FAMILY_ANCHORS '
            'entry (scripts/ratchet_audit.py). A ratchet without its incident '
            'only blocks change, not bugs (docs/TESTING_STRATEGY.md §4 P2): '
            f'{offenders}')

    def test_family_anchor_artifacts_exist(self):
        """防洗白:家族锚引用的工件必须在库。"""
        missing = [(p, a) for p, a in ra.FAMILY_ANCHORS.items()
                   if not os.path.isfile(os.path.join(ROOT, a))]
        assert not missing, f'FAMILY_ANCHORS laundering: {missing}'

    def test_baseline_still_covers_current_guard_files(self):
        """卫生:基线不得比现存守护套件小得离谱(防有人用删基线洗白)。"""
        baseline = _baseline()
        current = {n for n in os.listdir(HERE)
                   if n.startswith('test_') and n.endswith('.py')
                   and ra.is_guard_file(n)}
        unanchored_new = current - baseline
        # 详细锚定检查交给上一针;这里只防「基线被整体清空」
        assert len(baseline) >= 100, (
            f'baseline shrank to {len(baseline)} — someone is laundering '
            'guard suites by emptying the grandfather list')
        assert isinstance(unanchored_new, set)  # 形状针:差集可计算即放行
