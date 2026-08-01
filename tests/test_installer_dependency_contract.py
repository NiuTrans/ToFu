"""tests/test_installer_dependency_contract.py — the installer's own logic must
hold regardless of what the world currently serves.

WHY THIS EXISTS
---------------
``install.sh`` is the first thing a real user runs, and it treats tofu-search as
non-optional: ``server.py`` lists ``tofu_search.fetch`` / ``tofu_search.search``
as CRITICAL imports, so a machine without it cannot boot the app at all.

Measured 2026-07-31, this path had NO guard of any kind. Not because it was
green — because nothing ever ran it. With ``requirements.txt`` at
``tofu-search>=0.5.3`` and public PyPI topping out at 0.5.1, the installer's
skip-probe correctly declines to skip (it compares versions, not just symbols:
installed 0.5.2 < floor 0.5.3 → exit 2 → proceed), falls through to the PyPI
branch, and dies at ``fail``. So the installer was BROKEN on the maintainer's
own machine and on any clean host alike, and the only reason nobody knew is that
no automation executes it.

That is a distinct failure shape from the rest of this batch. The others were
"the environment we test in differs from the one that ships". This one is
**"never run" standing in for "works"** — subtler, because there is not even a
single failing observation to notice. Zero observations is not zero defects.

WHAT THIS GUARDS — AND WHAT IT DELIBERATELY DOES NOT
-----------------------------------------------------
It pins properties of the installer's LOGIC that are true independently of what
PyPI happens to serve today:

  1. the floor parser extracts the version actually declared in
     ``requirements.txt`` (never silently degrading to its fallback);
  2. a bundled ``vendor/`` wheel, if present, is byte-identical to the artifact
     we published and verified;
  3. an unsatisfiable tofu-search install ABORTS rather than warning.

It deliberately does NOT try to run the installer end-to-end against the live
index. Such a test would assert PUBLICATION STATE, not code correctness: it
would be red purely because a version is not yet uploaded, and would flip green
the moment it was — permanently unable to distinguish "the installer logic is
right" from "PyPI happens to have stock". ``test_requirements_public_resolvable``
already owns the question of whether the floor is servable, and duplicating it
here would only add a second red light with no new information.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_INSTALL_SH = _ROOT / 'install.sh'
_REQUIREMENTS = _ROOT / 'requirements.txt'
_VENDOR = _ROOT / 'vendor'

# The floor parser as it appears in install.sh. Kept as the literal shell
# pipeline rather than a Python re-implementation: a reimplementation would test
# this file's idea of the parser, not the one that actually runs.
_FLOOR_PIPELINE = (
    r"grep -iE '^[[:space:]]*tofu-search[[:space:]]*>=' %s "
    r"| sed -E 's/.*>=[[:space:]]*//; s/[^0-9.].*//' | head -1"
)
_FALLBACK_FLOOR = '0.4.0'


def _run_floor_parser(requirements_text: str) -> str:
    """Run install.sh's real sed pipeline over the given requirements content."""
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(requirements_text)
        path = fh.name
    try:
        proc = subprocess.run(['bash', '-c', _FLOOR_PIPELINE % path],
                              capture_output=True, text=True, timeout=60)
        return proc.stdout.strip()
    finally:
        Path(path).unlink(missing_ok=True)


def test_the_parser_pipeline_is_still_the_one_install_sh_uses():
    """If install.sh's pipeline changes, the cases below stop being evidence."""
    src = _INSTALL_SH.read_text(encoding='utf-8')
    assert "sed -E 's/.*>=[[:space:]]*//; s/[^0-9.].*//'" in src, (
        'install.sh no longer derives _TS_FLOOR with the sed pipeline this test '
        'exercises. Update _FLOOR_PIPELINE to match the real one, or these '
        'parametrized cases are testing a parser that does not exist.'
    )
    assert f'_TS_FLOOR="{_FALLBACK_FLOOR}"' in src, (
        f'install.sh no longer falls back to {_FALLBACK_FLOOR}; update the '
        'fallback this test asserts against.'
    )


def test_floor_parser_reads_the_declared_version_from_the_real_file():
    """The parsed floor must equal what requirements.txt actually declares.

    Both sides are read FROM THE FILE — nothing is hardcoded — so raising the
    floor never makes this test lie, and never taxes a dependency bump with a
    test edit. What it catches is the parser silently degrading: if the
    declaration is rewritten into a shape the sed cannot read, the installer
    falls back to 0.4.0 and installs a build BELOW the real requirement, and the
    server then dies at boot on a missing symbol.
    """
    text = _REQUIREMENTS.read_text(encoding='utf-8')
    m = re.search(r'^[ \t]*tofu-search[ \t]*>=[ \t]*([0-9][0-9A-Za-z.]*)',
                  text, re.M)
    assert m, 'requirements.txt no longer declares a tofu-search>= floor'
    declared = m.group(1)

    parsed = _run_floor_parser(text)
    assert parsed, (
        'install.sh\'s floor parser produced NOTHING from the real '
        'requirements.txt, so the installer would silently use its '
        f'{_FALLBACK_FLOOR} fallback and install a version below what this '
        'project actually needs.'
    )
    assert parsed == declared.rstrip('.'), (
        f'floor parser produced {parsed!r} but requirements.txt declares '
        f'{declared!r}. The installer would install the wrong version.'
    )
    if declared != _FALLBACK_FLOOR:
        assert parsed != _FALLBACK_FLOOR, (
            f'parser produced the {_FALLBACK_FLOOR} FALLBACK while the real '
            f'declaration is {declared} — it failed to read the line and the '
            'failure is silent.'
        )


@pytest.mark.parametrize('line,expected', [
    ('tofu-search>=0.5.3', '0.5.3'),
    ('tofu-search >= 0.5.3', '0.5.3'),
    ('tofu-search>=0.5.3  # trailing comment', '0.5.3'),
    ('tofu-search>=0.5.3rc1', '0.5.3'),
    ('tofu-search>=0.5.3,<0.6', '0.5.3'),
    ('tofu-search>=1.10.0', '1.10.0'),
])
def test_floor_parser_handles_the_declaration_shapes_we_might_write(line, expected):
    """Whichever way someone writes the pin, the parser must still read it.

    These are the forms a maintainer plausibly types. Pinning them means a
    rewrite of that line fails HERE — loudly, in CI — instead of downgrading a
    real user's install to the fallback floor.
    """
    assert _run_floor_parser(line + '\n') == expected


def _verified_published_digests() -> set[str]:
    """The verified digests owned by tests/test_published_dependency_identity.py.

    Parsed from that file's source rather than IMPORTED. A plain
    ``from test_published_dependency_identity import …`` fails outside a
    rootdir-inserted run (measured: ModuleNotFoundError under
    ``pytest tests/<file>``), and papering over that with a sys.path insert
    would make this guard's reachability depend on how pytest was invoked.
    Reading the text keeps ONE owner for the digests while making this test
    runnable on its own.
    """
    src = (Path(__file__).parent / 'test_published_dependency_identity.py')
    if not src.is_file():
        return set()
    return set(re.findall(r"'([0-9a-f]{64})'", src.read_text(encoding='utf-8')))


def test_a_bundled_vendor_wheel_matches_the_published_artifact():
    """A local wheel must not be able to bypass the published-bytes invariant.

    install.sh PREFERS ``vendor/tofu_search-*.whl`` over PyPI and only falls
    back on failure, so a stale or unverified wheel there silently gives the two
    install paths different code — defeating
    ``test_published_dependency_identity``, which only sees what PyPI serves.

    ABSENCE IS A LEGITIMATE STATE, asserted as such on purpose: opensource
    installs and developer clones have no ``vendor/`` at all (measured: it does
    not exist in this tree), and personal/internal exports may create it for
    OTHER payloads — the bundled MCP servers live under ``vendor/<name>/``. So
    this test passes when there is no directory and when there is a directory
    with no tofu-search wheel. Only a PRESENT wheel whose digest is unknown is a
    failure. Written this way so the guard cannot be "fixed" by loosening it the
    first time someone puts something else in vendor/.
    """
    if not _VENDOR.is_dir():
        pytest.skip('no vendor/ directory — the normal state for a clone')
    wheels = sorted(_VENDOR.glob('tofu_search-*.whl'))
    if not wheels:
        pytest.skip('vendor/ exists but bundles no tofu-search wheel — legitimate')

    known = _verified_published_digests()
    assert known, (
        'could not read _EXPECTED_DIGESTS from '
        'tests/test_published_dependency_identity.py — that file owns the '
        'verified digests; without them this check cannot judge the wheel.'
    )
    for whl in wheels:
        digest = hashlib.sha256(whl.read_bytes()).hexdigest()
        assert digest in known, (
            f'vendor/{whl.name} has sha256 {digest}, which is not among the '
            'verified published digests.\n'
            'install.sh prefers this wheel over PyPI, so the bundled install '
            'path would ship code the published-artifact guard never checked. '
            'Rebuild it from the released tag, or update _EXPECTED_DIGESTS '
            'deliberately after verifying the new artifact end-to-end.'
        )


def test_an_unsatisfiable_tofu_search_install_aborts_rather_than_warns():
    """Failure to install tofu-search must be fatal, not advisory.

    server.py imports tofu_search.fetch / tofu_search.search as CRITICAL, so a
    "warning" here would hand the user a successful-looking install that crashes
    at startup — strictly worse than the current hard stop, because the failure
    surfaces far from its cause.

    Both terminal branches are checked: the bundled-wheel path (which falls back
    to PyPI and then fails) and the PyPI-only path.
    """
    src = _INSTALL_SH.read_text(encoding='utf-8')
    section = src[src.index('# ── tofu-search'):]
    section = section[:section.index('# ── Optional: bundled internal MCP')]

    fails = re.findall(r'^\s*(?:\|\|\s*)?fail "tofu-search install failed[^"]*"',
                       section, re.M)
    assert len(fails) == 2, (
        f'expected BOTH tofu-search install branches (bundled-wheel fallback '
        f'and PyPI) to end in fail(), found {len(fails)}.\n'
        'If one was downgraded to warn(), the installer now reports success on '
        'a machine whose server cannot boot. tofu_search.fetch / .search are '
        'CRITICAL imports in server.py.'
    )
    assert 'warn "tofu-search install failed' not in section, (
        'a tofu-search install failure is reported via warn() — it must abort. '
        'The warn() lines in this section may only carry the retry HINT that '
        'precedes the fail().'
    )
