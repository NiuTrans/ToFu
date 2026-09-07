"""tests/test_release_chain_tofu_search.py — the release chain must be able
to SATISFY the tofu-search floor, not merely declare it.

WHY THIS EXISTS (pt_84e6828ee5f44a7c)
-------------------------------------
``requirements.txt`` pins a moving ``tofu-search>=X.Y.Z`` floor (0.5.3 when
this suite landed, since bumped 0.6.0 → 0.7.3 — each bump's rationale lives
in the comment block above the pin). Recent floors are on NO pip index:
public PyPI topped at 0.5.1 (measured 2026-07-31, pinned by
tests/test_requirements_public_resolvable.py — RED until the publish), and
the internal mirror carries none. The only verified artifacts live in the
monorepo member's ``dist/``.

So the chain must carry the floor itself, at all three of its points:

  1. **export** — ``_bundle_tofu_search_wheel`` used to skip opensource on
     the assumption "a vanilla host reaches public PyPI fine". Measured
     false; the wheel is bundled for EVERY mode now.
  2. **CI** — each build-desktop.yml leg installs the bundled wheel BEFORE
     ``pip install -r requirements.txt``, so pip sees the floor as already
     satisfied. Absence + an index that lacks the floor fails LOUD at the
     install step, never a hollow build (run 30601806258).
  3. **the floor itself is documented** — the 0.5.3 bump landed with the
     comment block ending at 0.5.2, an undocumented pin that begged to be
     "fixed" by lowering. The rationale is now written down and marked HARD.

The actual PUBLISH (push v0.5.3 to GitHub + PyPI) is human-gated and
tracked on the board; these guards hold the line in the meantime and keep
holding it afterwards (the wheel bundle is then simply redundant, and the
CI step harmless).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / '.github' / 'workflows' / 'build-desktop.yml'
_REQUIREMENTS = _ROOT / 'requirements.txt'


# ═══════════════════════════════════════════════════════════════════
#  1. export bundles the wheel for EVERY mode
# ═══════════════════════════════════════════════════════════════════


def _fake_tree(tmp_path, monkeypatch, wheels=('tofu_search-0.5.3-py3-none-any.whl',),
               floor='0.5.3', src_version='0.5.3'):
    """A minimal ROOT/workspace/dest tree around the bundling function."""
    internal = pytest.importorskip(
        'export_pkg._internal', reason='export internals not shipped in opensource')
    root = tmp_path / 'tofu'
    member = root / 'packages' / 'tofu-search'
    (member / 'dist').mkdir(parents=True)
    (member / 'pyproject.toml').write_text(
        f'[project]\nversion = "{src_version}"\n', encoding='utf-8')
    for w in wheels:
        (member / 'dist' / w).write_bytes(b'WHEEL:' + w.encode())
    (root / 'requirements.txt').write_text(
        f'tofu-search>={floor}\n', encoding='utf-8')
    dest = tmp_path / 'dest'
    dest.mkdir()
    # Patch the helper's canonical module so the test cannot accidentally read
    # a developer's real sibling checkout. Importing the full publish facade
    # would also cross into secret-bearing release setup that this unit does
    # not exercise.
    monkeypatch.setattr(internal, 'ROOT', root)
    return internal, dest


def test_the_wheel_is_bundled_for_opensource_too(tmp_path, monkeypatch):
    """The opensource skip assumed public PyPI carries the floor — it does
    not (max 0.5.1 vs a 0.5.3 HARD floor), so skipping produced an export
    whose install.sh could never satisfy requirements.txt."""
    ex, dest = _fake_tree(tmp_path, monkeypatch)
    ex._bundle_tofu_search_wheel(dest, 'opensource')
    shipped = list((dest / 'vendor').glob('tofu_search-*.whl'))
    assert len(shipped) == 1, (
        f'opensource export carries no tofu-search wheel: {shipped}. '
        'With the floor unsatisfiable from every index, that export cannot '
        'be installed at all')


@pytest.mark.parametrize('mode', ['personal', 'internal'])
def test_the_wheel_is_still_bundled_for_internal_modes(tmp_path,
                                                       monkeypatch, mode):
    """Complement: widening the gate must not drop the existing modes."""
    ex, dest = _fake_tree(tmp_path, monkeypatch)
    ex._bundle_tofu_search_wheel(dest, mode)
    assert len(list((dest / 'vendor').glob('tofu_search-*.whl'))) == 1


def test_the_newest_wheel_wins(tmp_path, monkeypatch):
    ex, dest = _fake_tree(
        tmp_path, monkeypatch,
        wheels=('tofu_search-0.5.1-py3-none-any.whl',
                'tofu_search-0.5.3-py3-none-any.whl'))
    ex._bundle_tofu_search_wheel(dest, 'opensource')
    names = [p.name for p in (dest / 'vendor').glob('tofu_search-*.whl')]
    assert names == ['tofu_search-0.5.3-py3-none-any.whl'], names


def test_a_missing_dist_dir_is_a_clean_skip(tmp_path, monkeypatch):
    """A host without the sibling repo must not crash the export — it just
    gets no wheel (and install.sh then reports the gap loudly)."""
    internal = pytest.importorskip(
        'export_pkg._internal', reason='export internals not shipped in opensource')
    root = tmp_path / 'tofu'
    root.mkdir()
    (root / 'requirements.txt').write_text('tofu-search>=0.5.3\n',
                                           encoding='utf-8')
    dest = tmp_path / 'dest'
    dest.mkdir()
    monkeypatch.setattr(internal, 'ROOT', root)
    internal._bundle_tofu_search_wheel(dest, 'opensource')
    assert not list(dest.glob('vendor/tofu_search-*.whl'))


# ═══════════════════════════════════════════════════════════════════
#  2. CI installs the bundled wheel BEFORE requirements
# ═══════════════════════════════════════════════════════════════════

_STEP_HEADER_RE = re.compile(r'^      - name: ', re.M)


def _install_bodies():
    """The `run:` body of every 'Install dependencies' step.

    Sliced by step-header boundaries (a blank line separates steps, so an
    indentation-anchored regex stops one line early and the lookahead never
    fires — measured by this guard going vacuously red on its first run).
    """
    text = _WORKFLOW.read_text(encoding='utf-8')
    heads = [m.start() for m in _STEP_HEADER_RE.finditer(text)]
    bodies = []
    for i, start in enumerate(heads):
        header_end = text.index('\n', start) + 1
        end = heads[i + 1] if i + 1 < len(heads) else len(text)
        header = text[start:header_end]
        if 'Install dependencies' in header:
            bodies.append(text[header_end:end])
    assert len(bodies) == 3, (
        f'expected exactly 3 "Install dependencies" steps (one per platform '
        f'leg), found {len(bodies)} — the scan surface moved, re-point this '
        'guard before trusting it')
    return bodies


def test_every_leg_installs_the_bundled_wheel_before_requirements():
    """Order is load-bearing: only an ALREADY-INSTALLED tofu-search lets pip
    treat ``tofu-search>=0.5.3`` as satisfied without consulting the index —
    where no satisfying candidate exists."""
    for i, body in enumerate(_install_bodies(), 1):
        wheel = body.find('pip install --no-deps vendor/tofu_search-*.whl')
        reqs = body.find('pip install -r requirements.txt')
        assert wheel != -1, (
            f'leg {i} never installs the bundled tofu-search wheel — with '
            'the floor on no index, this leg fails (or worse, hollow-builds)')
        assert reqs != -1 and wheel < reqs, (
            f'leg {i} installs the wheel AFTER requirements.txt — the solve '
            'hits the unsatisfiable floor before the wheel can satisfy it')


def test_the_wheel_install_is_guarded_not_fatal_when_absent():
    """An export that somehow lost vendor/ must fail at the REQUIREMENTS
    step (loud), not at a hard glob — and the size floor stays the backstop."""
    for i, body in enumerate(_install_bodies(), 1):
        assert 'if ls vendor/tofu_search-*.whl' in body, (
            f'leg {i} installs the wheel unconditionally — a missing vendor/ '
            'would abort the step with a confusing error instead of the loud '
            'requirements failure that names the real gap')


# ═══════════════════════════════════════════════════════════════════
#  3. the floor pin is documented, and marked HARD/SOFT
# ═══════════════════════════════════════════════════════════════════


def test_the_floor_pin_carries_its_rationale():
    """Every tofu-search floor must be documented where the pin lives.

    Drift note: this guard used to pin the LITERAL ``>=0.5.3`` line (and was
    named ``test_the_053_floor_carries_its_rationale``). The floor has moved
    twice since (0.6.0 filter_mode HARD, then 0.7.3 replay no-op SOFT) and
    each bump broke the literal pin even though the rationale discipline was
    upheld — the version was over-pinned, the INTENT was not. The guard now
    reads whatever floor is pinned and pins the discipline itself: a
    ``# >=<floor>:`` paragraph that marks the floor HARD or SOFT and names
    the capability that requires it. Lowering is never the fix for an
    unsatisfiable floor (the bridge crashes at boot without the new kwarg on
    HARD floors), so "just lower it" must never read as a safe fix.
    """
    text = _REQUIREMENTS.read_text(encoding='utf-8')
    pin = re.search(r'(?m)^tofu-search>=(\d+\.\d+\.\d+)\s*$', text)
    assert pin, 'the tofu-search floor pin itself is gone — re-aim this guard'
    floor = pin.group(1)
    para = re.search(
        rf'(?ms)^# >={re.escape(floor)}:(.*?)^tofu-search>=', text)
    assert para, (
        f'the {floor} floor has no rationale paragraph — an undocumented '
        'floor invites "just lower it" as a fix, which is never safe here')
    body = para.group(1).strip()
    assert re.search(r'(HARD|SOFT) floor', body), (
        f'the {floor} floor is not marked HARD/SOFT — without that, '
        '"lower the floor" reads as a safe fix for an unsatisfiable pin, '
        'and on HARD floors it is anything but (boot-time TypeError in '
        'configure())')
    assert len(body) >= 80, (
        f'the {floor} rationale is a stub ({len(body)} chars) — name the '
        'capability that requires the floor')
