"""tests/test_requirements_public_resolvable.py — a published tree must be
INSTALLABLE, not merely identical.

WHY THIS EXISTS (the second instance of one defect class)
----------------------------------------------------------
``tests/test_published_pipeline_drift.py`` closed "the file we validate is not
the file that runs". This closes its sibling: **"the dependency we declare is
not a dependency anyone can install."**

Measured 2026-07-31 on run 30601806258 (the first run that actually reached a
verdict after the macos-13 starvation was fixed): three of four platform legs
failed at ``Install dependencies``. ``requirements.txt`` pins
``tofu-search>=0.5.3``; public PyPI's newest release is ``0.5.1``. The
constraint is UNSATISFIABLE from a clean machine, so the release could not be
built — and, worse, neither could a first-time user's ``pip install -r``.

The drift guard cannot see this, and that is not a bug in it. The published
``requirements.txt`` genuinely matches the local one (modulo the sanitizer), so
byte-equality is GREEN while the tree is uninstallable. Byte-equality is a
necessary condition, never a sufficient one: what makes a requirement valid is a
fact about a THIRD PARTY (the public index) that neither copy models.

WHY THE LOCAL ENVIRONMENT HID IT
--------------------------------
This machine has ``tofu-search 0.5.2`` installed and its pip is configured
against an internal mirror (``pip.internal.example.com``). Every local check therefore
looked satisfiable. The defect is only visible from a machine that resolves
against the PUBLIC index, which is exactly what a GitHub runner — and every
external user — is.

That is why this test pins ``https://pypi.org/pypi/...`` EXPLICITLY and never
shells out to ``pip``: a ``pip`` subprocess would inherit this host's
``index-url`` and quietly answer a different question, producing a green light
for the wrong index. A guard that measures the wrong thing is the failure mode
both of these modules exist to eliminate.

WHY NOT JUST LOWER THE FLOOR
-----------------------------
Deliberately NOT the fix. The comment block above the pin records that ``0.5.0``
is a HARD floor (an older ``configure()`` raises TypeError on the deadline
kwargs, so the server dies at boot) and ``0.5.2`` carries the identity-fallback
seams. Lowering the floor would trade "installable" for "installs something
degraded" — the failure would move from the build log into runtime behaviour,
where nobody is watching. The correct fix is always to PUBLISH the dependency.

WHAT THIS DOES NOT CLAIM
------------------------
It checks that each declared constraint has at least one candidate on the public
index. It is NOT a full resolver: it cannot prove the transitive graph is
co-satisfiable, and it does not check wheels-per-platform. Those need a real
resolve in CI. This catches the specific, recurring, silent case — a floor
raised against a version that was never published — at zero build cost.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_REQUIREMENTS = _ROOT / 'requirements.txt'

# The PUBLIC index, hardcoded on purpose. See the module docstring: resolving via
# `pip` would inherit this host's internal mirror and answer a different question.
_PYPI_JSON = 'https://pypi.org/pypi/{name}/json'
_FETCH_TIMEOUT = 25

_REQ_RE = re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)$')
_CLAUSE_RE = re.compile(r'^(>=|==|>|~=)\s*([0-9][0-9A-Za-z.*]*)$')


def _version_key(v: str) -> tuple:
    """Coarse PEP440-ish ordering: numeric release segments only.

    Deliberately simple. A pre-release or local segment sorts as its numeric
    prefix, which is fine here — we only ever ask "does SOME published version
    reach this floor", never "which exact version would pip pick".
    """
    head = re.split(r'[^0-9.]', v.split('+')[0], maxsplit=1)[0]
    return tuple(int(p) for p in head.strip('.').split('.') if p.isdigit())


def _parse_requirements() -> list[tuple[str, str, int]]:
    """Yield (name, specifier, line_number), skipping comments and blanks."""
    out = []
    for lineno, raw in enumerate(_REQUIREMENTS.read_text(encoding='utf-8').splitlines(), 1):
        line = raw.split('#', 1)[0].strip()
        if not line or line.startswith('-'):
            continue
        line = line.split(';', 1)[0].strip()  # drop environment markers
        m = _REQ_RE.match(line)
        if m:
            out.append((m.group(1), m.group(2).strip(), lineno))
    return out


_REQS = _parse_requirements()


@pytest.fixture(scope='module')
def _network():
    if os.environ.get('TOFU_SKIP_NETWORK_TESTS'):
        pytest.skip('TOFU_SKIP_NETWORK_TESTS set')
    try:
        urllib.request.urlopen(_PYPI_JSON.format(name='requests'),
                               timeout=_FETCH_TIMEOUT).read()
    except Exception as e:
        pytest.skip(f'public PyPI unreachable ({type(e).__name__}: {e}) — '
                    'resolvability NOT checked, which is not the same as resolvable')


def test_requirements_file_was_parsed():
    """A parser that silently matches nothing would make every case vacuous."""
    assert len(_REQS) >= 20, (
        f'only parsed {len(_REQS)} requirements from requirements.txt — the '
        'parser is broken, so the per-package checks below prove nothing'
    )


@pytest.mark.parametrize('name,spec,lineno',
                         _REQS, ids=[r[0] for r in _REQS])
def test_requirement_is_satisfiable_on_public_pypi(_network, name, spec, lineno):
    """Every declared constraint must have a candidate on the PUBLIC index.

    This is the check that would have caught ``tofu-search>=0.5.3`` before it
    cost three failed platform legs and a blocked release.
    """
    try:
        with urllib.request.urlopen(_PYPI_JSON.format(name=name),
                                    timeout=_FETCH_TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            pytest.fail(
                f'requirements.txt:{lineno} declares {name!r}, which does NOT '
                'exist on public PyPI. A clean `pip install -r requirements.txt` '
                'cannot work for any external user or CI runner.'
            )
        pytest.skip(f'PyPI HTTP {e.code} for {name} — not checked')
    except OSError as e:
        pytest.skip(f'could not query PyPI for {name} ({e}) — not checked')

    available = list(data['releases'].keys())
    latest = data['info']['version']

    for clause in (c.strip() for c in spec.split(',') if c.strip()):
        m = _CLAUSE_RE.match(clause)
        if not m:
            continue  # <, <=, != only ever REMOVE candidates; not our concern
        op, want = m.group(1), m.group(2)
        if op in ('>=', '>', '~='):
            reachable = any(_version_key(a) >= _version_key(want) for a in available)
            assert reachable, (
                f'requirements.txt:{lineno} requires {name}{clause}, but the '
                f'newest release on PUBLIC PyPI is {latest}. The constraint is '
                'UNSATISFIABLE from a clean machine, so CI and every external '
                f'user fail at install time.\n\n'
                f'FIX: publish {name} {want} to PyPI. Do NOT lower the floor to '
                'make this pass — the floor encodes a real runtime requirement, '
                'and lowering it moves the failure from the build log into '
                'silent runtime degradation.'
            )
        elif op == '==':
            assert want in data['releases'], (
                f'requirements.txt:{lineno} pins {name}=={want}, which is not '
                f'published on public PyPI (newest is {latest}).'
            )
