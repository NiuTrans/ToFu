"""tests/test_published_dependency_identity.py — the dependency CI installs must
be the artifact we actually validated.

WHY THIS EXISTS (the third instance of one defect class)
--------------------------------------------------------
This batch closed two forms of "the thing we checked is not the thing that
ships":

  * ``test_published_pipeline_drift`` — the workflow file we validate vs the one
    GitHub runs;
  * ``test_requirements_public_resolvable`` — the dependency floor we declare vs
    what the public index can actually serve.

A third gap sat between them. ``requirements.txt`` does not say "tofu-search
0.5.3 as built and verified on the maintainer's disk"; it says "whatever public
PyPI serves for ``>=0.5.3``". Those are different artifacts and nothing compared
them. So a resolvability check can be GREEN — 0.5.3 exists! — while CI installs
a build nobody ever imported:

  * a partial upload lands the sdist but not the wheel (or vice versa), and pip
    silently builds from whichever half arrived;
  * a stale copy from a scratch directory gets uploaded instead of the artifact
    that passed ``twine check`` and the isolated-import probe;
  * the version is re-cut and re-uploaded later, and the bytes drift from the
    ones this repo's guards were run against.

None of those are exotic. All of them are silent, because every existing gate
asks about the NAME or the VERSION, never the BYTES.

WHAT THIS PINS
--------------
The SHA256 of each published file of the pinned tofu-search release, measured
locally from the artifacts that passed:

  * ``python -m build`` from a clean ``git archive HEAD`` export (NOT the
    working tree — that is what hid the unimportable-facade defect);
  * ``twine check`` PASSED on both files;
  * installed into an isolated ``--target`` and imported from a neutral cwd with
    ``PYTHONNOUSERSITE=1``, with every facade ``__all__`` symbol resolving.

If the published bytes differ from these, the thing CI installs is NOT the thing
that cleared those checks, and the release must stop.

WHY THE DIGEST COMES FROM THE API, NOT A DOWNLOAD
-------------------------------------------------
PyPI publishes ``digests.sha256`` per file in ``/pypi/<name>/<version>/json``,
and that digest is what pip itself verifies a download against. Reading it is
therefore equivalent to hashing the file, at a fraction of the bytes — and it
cannot be fooled by a proxy that rewrites content, because a rewritten body
would no longer match the digest pip checks.

Deliberately NOT via ``pip download``: this host's pip is configured against an
internal mirror (``pip.internal.example.com``), so a pip-mediated check could hash a
DIFFERENT artifact than the public one CI resolves. Same discipline as the
resolvability guard — verify the deployed artifact, on the index that actually
serves CI.

MAINTENANCE
-----------
Raising the tofu-search floor means republishing and updating both digests here,
in the same commit. That is the point: the hash is a deliberate speed bump on
changing a hard dependency, and it forces the new artifact to be verified the
same way. When the pin and ``requirements.txt`` disagree, the test says so
rather than silently checking a version nobody depends on any more.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# The published-fixture reads the PUBLIC PyPI index over a real, unmocked HTTPS
# request (skip only when unreachable). Tier it `slow` so it leaves the default
# unit lane; verifying the deployed bytes is the behavior under test.
pytestmark = pytest.mark.slow

_ROOT = Path(__file__).resolve().parent.parent
_REQUIREMENTS = _ROOT / 'requirements.txt'

_PACKAGE = 'tofu-search'
_PINNED_VERSION = '0.10.3'

# Measured 2026-08-21 from the artifacts built off tofu-search e2cbcfc (tag
# v0.10.3) via clean `git archive HEAD` + `python -m build --no-isolation`,
# twine check PASSED on both files, isolated import probe from a neutral cwd
# (wheel unpacked by hand — this env's pip enforces require-virtualenv) with
# __file__ asserted inside the probe dir and all 37 facade __all__ symbols
# resolving; the digests below are PyPI's own /pypi/tofu-search/0.10.3/json
# values AND match the local sha256 of the uploaded files byte-for-byte.
_EXPECTED_DIGESTS = {
    'tofu_search-0.10.3-py3-none-any.whl':
        '7af4ef7ffec6c384407cf05fdac2034890813dc5aa5ddc9094f7a850a1c76f52',
    'tofu_search-0.10.3.tar.gz':
        '10c6c177c4fce22ac1af93e87c4f3c3b07cc1e77e2acfad030773595bd3d08bf',
}

_PYPI_VERSION_JSON = 'https://pypi.org/pypi/{name}/{version}/json'
_FETCH_TIMEOUT = 25


@pytest.fixture(scope='module')
def published():
    """The pinned release's files from PUBLIC PyPI, or a loud skip.

    A 404 means the version is not published YET, which is a real and expected
    state while a release is being cut — the resolvability guard is the one that
    fails on it. This guard's job is narrower: IF it is published, the bytes
    must be ours.
    """
    if os.environ.get('TOFU_SKIP_NETWORK_TESTS'):
        pytest.skip('TOFU_SKIP_NETWORK_TESTS set')
    url = _PYPI_VERSION_JSON.format(name=_PACKAGE, version=_PINNED_VERSION)
    try:
        with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT) as resp:
            doc = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            pytest.skip(
                f'{_PACKAGE} {_PINNED_VERSION} is not on public PyPI yet — '
                'publish it, then this guard verifies the bytes. '
                '(test_requirements_public_resolvable is the gate that fails '
                'on an unpublished floor.)')
        pytest.skip(f'PyPI HTTP {e.code} for {_PACKAGE} {_PINNED_VERSION}')
    except OSError as e:
        pytest.skip(f'public PyPI unreachable ({e}) — identity NOT checked, '
                    'which is not the same as verified')
    return {u['filename']: u for u in doc.get('urls', [])}


def test_the_pin_matches_the_declared_floor():
    """The hash pin must describe the version requirements.txt actually needs.

    Without this, bumping the floor to 0.5.4 would leave the digests pinned to
    0.5.3 — still green, while verifying an artifact nobody installs. A guard
    checking the wrong subject is the failure mode this whole batch is about.
    """
    text = _REQUIREMENTS.read_text(encoding='utf-8')
    m = re.search(rf'^{re.escape(_PACKAGE)}\s*>=\s*([0-9][0-9A-Za-z.]*)',
                  text, re.M)
    assert m, f'requirements.txt no longer declares a {_PACKAGE}>= floor'
    assert m.group(1) == _PINNED_VERSION, (
        f'requirements.txt floors {_PACKAGE} at {m.group(1)} but this file '
        f'pins digests for {_PINNED_VERSION}. Republish the new version, verify '
        'it the same way (clean-checkout build + twine check + isolated '
        'import), and update _EXPECTED_DIGESTS in the SAME commit.'
    )


def test_every_expected_file_is_actually_published(published):
    """A partial upload must not read as a successful one.

    pip resolves a wheel when present and falls back to building the sdist when
    not, so half an upload changes WHAT CI INSTALLS without changing the version
    it reports.
    """
    missing = [n for n in _EXPECTED_DIGESTS if n not in published]
    assert not missing, (
        f'{_PACKAGE} {_PINNED_VERSION} is published but these files are '
        f'absent: {missing}. Published files: {sorted(published)}.\n'
        'A partial upload silently changes what CI installs — pip prefers the '
        'wheel and builds the sdist when it is missing. Upload the rest.'
    )


@pytest.mark.parametrize('filename,expected', sorted(_EXPECTED_DIGESTS.items()))
def test_published_artifact_is_byte_identical_to_what_we_verified(
        published, filename, expected):
    """The published bytes must be the artifact this repo actually validated."""
    entry = published.get(filename)
    if entry is None:
        pytest.skip(f'{filename} not published (covered by the sibling test)')
    actual = (entry.get('digests') or {}).get('sha256', '')
    assert actual == expected, (
        f'PUBLISHED ARTIFACT MISMATCH for {filename}\n'
        f'  expected (verified locally): {expected}\n'
        f'  published on PyPI:          {actual or "<no sha256 in payload>"}\n\n'
        'The dependency CI installs is NOT the artifact that passed the '
        'clean-checkout build, twine check and isolated-import probe. Do NOT '
        'cut a release against it.\n'
        'Either re-upload the verified artifact, or re-verify the published one '
        'end-to-end and update _EXPECTED_DIGESTS deliberately — never edit the '
        'hash just to make this pass.'
    )
