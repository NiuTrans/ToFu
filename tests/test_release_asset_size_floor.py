"""tests/test_release_asset_size_floor.py — a NAMED asset is not a VALID asset,
and a shared table's SHAPE is part of its contract.

WHY THIS EXISTS
---------------
Run 30601806258 produced a correctly-named Windows installer containing
essentially none of the application:

    hollow build     Tofu-Setup-…-win64.exe        48,960,018 bytes
    healthy v0.14.2  Tofu-Setup-0.14.2-win64.exe  115,822,886 bytes

The completeness gate matched it on filename and called the set complete. Only
the other three legs failing at the same time stopped ``make_latest`` from
pinning a 49 MB hollow installer as Latest — luck, not a gate.

So the gate now carries a per-platform size floor (70% of the measured v0.14.2
size), and this module pins both halves of that:

  1. the floor actually rejects the measured hollow artifact and actually
     accepts the measured healthy one — using the REAL byte counts, so the
     thresholds are anchored to observed reality rather than to a round number
     someone liked;
  2. every consumer of ``PLATFORM_ASSETS`` survives the row widening.

WHY (2) IS HERE AND NOT AN AFTERTHOUGHT
----------------------------------------
Adding the floor widened each row from 4 fields to 5, and
``routes/api_v1/desktop.py::_match_platform_assets`` unpacks rows positionally
(``for _os, _arch, label, pattern in rows``). That broke instantly and
SILENTLY: the route wraps its table load in ``except Exception`` and degrades
to the releases page, so a production symptom would have been "the download
button quietly stopped offering direct links" — with the release gates still
green, because they use the table differently.

That is the same defect class as everything else in this batch: a single source
of truth is only single if every consumer agrees on its SHAPE, and positional
unpacking makes shape a load-bearing, invisible contract. Pinning the arity
turns the next widening into a red test instead of a degraded feature.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / 'scripts' / 'release_assets.py'

# Measured on the real repo, 2026-07-31.
#   v0.14.2 = the last release that built with its dependencies intact
#   hollow  = run 30601806258's Windows artifact (pip install had failed)
_V0142_SIZES = {
    'Tofu-Setup-0.14.2-win64.exe': 115_822_886,
    'Tofu-0.14.2-macos-arm64.dmg': 170_577_117,
    'Tofu-0.14.2-macos-x86_64.dmg': 173_626_347,
    'Tofu-0.14.2-linux-x86_64.tar.gz': 193_972_205,
    # .deb is NOT in v0.14.2 (the format shipped after the release line
    # stalled). Measured 2026-08-01 by running desktop/build-deb.sh on the
    # REAL 0.16.0 PyInstaller payload from the local store (the same
    # payload the next release packs) — 175,975,798 bytes. Anchors the
    # headroom band for the floor ac1e598c added at 130 MB (73.9% — in
    # band), observed reality rather than an estimate.
    'Tofu-0.16.0-linux-x86_64.deb': 175_975_798,
}
_HOLLOW_WINDOWS_BYTES = 48_960_018

# The AGENT component's assets (deliberately added 2026-08-02, slice A2b):
# REQUIRED_PLATFORM_ASSETS now spans BOTH tables, so a name-completeness
# fixture must carry these too. Sizes are above the script's floors
# (windows agent measured 53.2 MB on the first server-built leg).
_AGENT_ASSET_SIZES = {
    'TofuAgent-0.16.0-macos-arm64.dmg': 20_000_000,
    'TofuAgent-0.16.0-macos-x86_64.dmg': 20_000_000,
    'TofuAgent-Setup-0.16.0-win64.exe': 53_200_000,
    'TofuAgent-0.16.0-linux-x86_64.tar.gz': 20_000_000,
}


def _load():
    spec = importlib.util.spec_from_file_location('_ra_test', _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hollow_windows_installer_is_rejected():
    """The measured 49 MB artifact must be reported as implausibly small."""
    m = _load()
    sizes = dict(_V0142_SIZES)
    sizes.update(_AGENT_ASSET_SIZES)  # name-complete across BOTH tables
    sizes['Tofu-Setup-0.15.2-win64.exe'] = _HOLLOW_WINDOWS_BYTES
    del sizes['Tofu-Setup-0.14.2-win64.exe']

    # It is NOT missing — that is precisely why a name-only gate passed it.
    assert not m.missing_assets(sizes.keys(), require_checksums=False), (
        'the hollow set should be name-complete; if it is not, this test is '
        'no longer reproducing the defect it was written for'
    )
    undersized = m.undersized_assets(sizes)
    assert len(undersized) == 1, f'expected exactly the Windows asset, got {undersized}'
    assert 'win64.exe' in undersized[0]


def test_healthy_release_is_accepted():
    """The real v0.14.2 sizes must clear every floor.

    The complement of the test above. Without it a floor set absurdly high
    would still "pass" the hollow check while blocking every real release.
    """
    m = _load()
    assert m.undersized_assets(_V0142_SIZES) == []


def test_floors_leave_headroom_below_the_last_good_release():
    """Floors must sit well under measured-good sizes, not just under them.

    A floor grazing the observed size would turn ordinary release-to-release
    shrinkage (a dropped dependency, better compression) into a failed
    release. 60-85% of the last known-good size is the intended band.
    """
    m = _load()
    import fnmatch
    for _os, _arch, label, pattern, min_bytes in m.PLATFORM_ASSETS:
        good = next((s for n, s in _V0142_SIZES.items()
                     if fnmatch.fnmatch(n, pattern)), None)
        assert good, f'no measured v0.14.2 size matches {pattern} ({label})'
        ratio = min_bytes / good
        assert 0.60 <= ratio <= 0.85, (
            f'{label}: floor {min_bytes:,} is {ratio:.0%} of the last good '
            f'build ({good:,}). Too high starves real releases; too low lets '
            'a hollow build through.'
        )


def test_every_consumer_agrees_on_the_row_shape():
    """PLATFORM_ASSETS is unpacked positionally elsewhere — pin the arity.

    ``routes/api_v1/desktop.py`` really did break when the size floor widened
    these rows, and its ``except Exception`` fallback would have hidden it as
    "direct download links quietly stopped appearing".
    """
    m = _load()
    for row in m.PLATFORM_ASSETS + m.AGENT_PLATFORM_ASSETS:
        assert len(row) == 5, (
            f'PLATFORM_ASSETS row {row!r} has {len(row)} fields, expected 5 '
            '(os, arch, label, glob, min_bytes). Widening this row breaks '
            'every consumer that unpacks it positionally — update '
            'routes/api_v1/desktop.py::_match_platform_assets and this test '
            'together.'
        )

    # The positional consumer moved with the 2026-07 extraction
    # (pt_a859c11e75d142d1): routes/api_v1/desktop.py now RE-EXPORTS the
    # matcher from lib/desktop_dist/platforms.py, so that is where the
    # 5-field unpack must be pinned. The re-export is asserted too — a
    # second hand-typed unpacker in the route is exactly the drift class
    # this guard exists to catch.
    import re as _re
    lib_src = (_ROOT / 'lib' / 'desktop_dist' / 'platforms.py').read_text(
        encoding='utf-8')
    assert _re.search(
        r'for _os, _arch, label, pattern, _min_bytes in ', lib_src), (
        'lib/desktop_dist/platforms.py no longer unpacks PLATFORM_ASSETS rows '
        'with 5 fields. If the table changed shape, every consumer must '
        'change with it — the table load degrades SILENTLY (wrapped in '
        'except Exception), so a mismatch shows up as missing download '
        'links, not as an error.'
    )
    route_src = (_ROOT / 'routes' / 'api_v1' / 'desktop.py').read_text(
        encoding='utf-8')
    assert 'from lib.desktop_dist.platforms import' in route_src, (
        'routes/api_v1/desktop.py lost the re-export — did a second '
        'positional unpacker grow back?')
    assert 'for _os, _arch, label, pattern' not in route_src, (
        'a SECOND positional unpacker appeared in the route — the whole '
        'point of the extraction is one consumer of the row shape')


def test_derived_label_glob_view_still_matches_the_table():
    """REQUIRED_PLATFORM_ASSETS is derived; it must not drift from the source."""
    m = _load()
    assert m.REQUIRED_PLATFORM_ASSETS == tuple(
        (label, pattern) for _os, _arch, label, pattern, _min in (
            m.PLATFORM_ASSETS + m.AGENT_PLATFORM_ASSETS))


def test_undetermined_contract_survives_a_non_release_body():
    """A garbage payload must not read as "every asset is 0 bytes".

    Reporting a truncated response as hollow would make the version gate
    rebuild a healthy release and — worse — let the retarget step move a
    published tag.
    """
    m = _load()
    assert m.sizes_from_release_json('{"message":"Not Found"}') is None
    assert m.sizes_from_release_json('not json at all') is None
    assert m.names_from_release_json('{"message":"Not Found"}') is None
