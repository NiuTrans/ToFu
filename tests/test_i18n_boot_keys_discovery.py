"""Tests for lib/i18n_boot_keys.discover_boot_keys — the atom Epic-E sub-part
1 slice 3 will use to split the per-lang i18n pack into a boot-critical
subset + a rest pack.

The invariants pinned here are the ones the next slice depends on:

  * Every discovered key EXISTS in the source dictionary. A false positive
    (grep matches a comment / string literal that never becomes a t() call)
    is safe — it just adds a harmless row to the boot pack; but a KEY WE
    RETURN that isn't in the dict means the extractor drifted and the boot
    pack would ship a genuinely-missing key.
  * The union count is a small FRACTION of the total dictionary (the whole
    point of the split — if it isn't small, we get no win).
  * Coverage is COMPLETE for the HTML pass: every ``data-i18n*`` attribute in
    index.html appears in the union.
  * The scan is deterministic and does NOT depend on node (it's a pure Python
    regex over the source tree).
"""

from __future__ import annotations

import os
import pathlib

import pytest


pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_discover_boot_keys_returns_expected_shape():
    from lib.i18n_boot_keys import discover_boot_keys
    result = discover_boot_keys(str(ROOT))
    assert set(result.keys()) == {'html', 'js', 'dynamic_prefixes', 'union'}
    for k in ('html', 'js', 'dynamic_prefixes', 'union'):
        assert isinstance(result[k], list)
        # sorted, deduped
        assert result[k] == sorted(set(result[k]))


def test_html_pass_finds_every_data_i18n_attribute():
    """Every data-i18n* attribute in index.html appears in the html list.

    This is the FLOOR: the ``_applyI18n()`` walker reads exactly these
    attributes on DOMContentLoaded, so a boot pack missing any of them would
    render a raw key string on screen for that element.
    """
    import re
    from lib.i18n_boot_keys import discover_boot_keys, HTML_ATTR_KEY_RE
    result = discover_boot_keys(str(ROOT))
    with open(ROOT / 'index.html', encoding='utf-8') as f:
        src = f.read()
    expected = sorted({m.group(1) for m in HTML_ATTR_KEY_RE.finditer(src)})
    assert result['html'] == expected
    # sanity: the html-key set should be non-trivial (~200+)
    assert len(expected) > 100


def test_union_is_a_small_fraction_of_total_dictionary():
    """The whole point of the split: boot-critical keys are a small fraction
    of the full 3000+ key dictionary. If this ratio ever exceeds 60% the
    boot-pack win has evaporated and the pipeline should be reconsidered —
    a hard-but-not-absurd ceiling.
    """
    from lib.i18n_boot_keys import discover_boot_keys
    from lib.i18n_packs import extract_dictionary
    try:
        total = extract_dictionary()
    except Exception as e:
        pytest.skip(f'i18n extraction (needs node) unavailable: {e}')
    # Measure with the FULL expansion (dynamic prefixes → real keys), which
    # is what the boot pack would actually carry. Without source_keys the
    # ratio understates by omitting namespace expansion.
    result = discover_boot_keys(str(ROOT), source_keys=list(total.keys()))
    ratio = len(result['union']) / max(len(total), 1)
    # Current measured: ~1302/3041 = 42.8%. Cap at 60% so a drift that
    # substantially inflates the boot set (e.g. accidentally pulling
    # paper-reader.js into _BUNDLE_FILES) trips the guard, but the natural
    # baseline stays comfortably under.
    assert ratio < 0.60, (
        f'boot key ratio {ratio:.1%} exceeds 60% — the boot/rest split '
        f'no longer buys enough; re-scope the boot subset or shrink the '
        f'core bundle')


def test_every_discovered_key_exists_in_source_dictionary():
    """A key we ship in the boot pack MUST exist in the source dict —
    otherwise the boot-pack row is a made-up key with no value, and t()
    would fall back to rendering the key string.

    Failure signals scan drift: either a t('literal') call site names a key
    that was renamed / never added to _i18n, or an HTML attribute is wrong.
    Either is a real bug the next slice needs surfaced.
    """
    from lib.i18n_boot_keys import discover_boot_keys
    from lib.i18n_packs import extract_dictionary
    try:
        total = extract_dictionary()
    except Exception as e:
        pytest.skip(f'i18n extraction (needs node) unavailable: {e}')
    # Pass source_keys so dynamic prefixes expand to real keys — otherwise
    # the ``prefix.`` string itself (which is NOT in the source dict) would
    # be misreported as "missing".
    result = discover_boot_keys(str(ROOT), source_keys=list(total.keys()))
    missing = [k for k in result['union'] if k not in total]
    assert not missing, (
        f'{len(missing)} boot-critical key(s) not in source _i18n dict '
        f'(sample: {missing[:10]}) — scan drift OR a genuinely-missing '
        f'translation. Fix at the CALL SITE (or add the key to i18n.js), '
        f'do not silence this guard.'
    )


def test_dynamic_prefixes_detected_for_known_call_sites():
    """The documented BOOT-critical dynamic call sites (net.state.<state>,
    finishInfo.cb.<k>, tool.label.<name>) MUST appear in the dynamic_prefixes
    list. If one drops out, either the file was moved OUT of the core bundle
    (fine — reflect that) or the regex drifted (regression).

    History: finishInfo.cbState.<state> used to be pinned here too, but its
    call site moved to DEFERRED ui/finish_info_rich.js (Epic-E sub-8, commit
    48c1651f — the cost popover builds lazily) and is therefore correctly
    ABSENT from the boot-critical set; the deferred rest pack carries it.
    tool.label. joined the boot set 2026-08-03 (streaming_ui.js phase-row
    tool labels) — a dynamic t('tool.label.' + name) written INLINE, since
    the scan cannot see t(identifier).
    """
    from lib.i18n_boot_keys import discover_boot_keys
    result = discover_boot_keys(str(ROOT))
    prefixes = set(result['dynamic_prefixes'])
    # net.state. is in net-latency.js which IS in the core bundle (boot).
    assert 'net.state.' in prefixes, (
        'net-latency.js\'s t("net.state." + state) call site is boot-critical '
        '— dynamic-prefix regex must catch it')
    # finishInfo.cb. is in ui/finish_info.js (core, slimmed in Epic-E sub-8).
    assert 'finishInfo.cb.' in prefixes
    # streaming_ui.js's inline t('tool.label.' + name) (phase-row tool labels).
    assert 'tool.label.' in prefixes, (
        'streaming_ui.js\'s inline t("tool.label." + name) call site is '
        'boot-critical (streaming phase rows) — the dynamic-prefix regex '
        'must catch it; t(identifier) is invisible to the scan')


def test_dynamic_prefix_expansion_bounded_by_source_dict():
    """expand_dynamic_prefixes returns ONLY keys that actually exist in the
    passed source_keys iterable — never fabricates a ``prefix.``-shaped key.
    """
    from lib.i18n_boot_keys import expand_dynamic_prefixes
    src = {'net.state.online', 'net.state.slow', 'net.state.offline',
           'chat.hello', 'chat.bye'}
    expanded = expand_dynamic_prefixes(['net.state.'], src)
    assert set(expanded) == {'net.state.online', 'net.state.slow',
                             'net.state.offline'}
    # An unknown prefix expands to nothing (no fabrication).
    expanded_none = expand_dynamic_prefixes(['no.such.prefix.'], src)
    assert expanded_none == []


def test_convenience_wrapper_matches_explicit_call():
    from lib.i18n_boot_keys import (discover_boot_keys,
                                    discover_boot_keys_from_bundle_manifest)
    explicit = discover_boot_keys(str(ROOT))
    convenience = discover_boot_keys_from_bundle_manifest()
    assert explicit == convenience
