"""debug/test_key_stats_last_resort.py — last-resort guard unit tests.

Verifies that `lib/key_stats.py` never auto-disables a provider's only
remaining usable key, while still respecting explicit user overrides.

Run: ``python3 debug/test_key_stats_last_resort.py``  (exits 0 on success)
"""

import os
import sys
import tempfile

# Allow running from repo root without install
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.key_stats as ks  # noqa: E402

# Redirect the on-disk stats file to a throwaway tempdir so tests never
# touch the real data/config/key_stats.json.
_TMP_DIR = tempfile.mkdtemp(prefix='keystats-test-')
ks._STATS_PATH = os.path.join(_TMP_DIR, 'key_stats.json')


def _reset_state():
    """Wipe the in-memory cache and any persisted file between scenarios."""
    with ks._lock:
        ks._cache['day'] = ks._today()
        ks._cache['stats'] = {}
        ks._cache['overrides'] = {}
        ks._cache['loaded'] = True
    with ks._siblings_lock:
        ks._siblings_cache['ts'] = 0.0
        ks._siblings_cache['by_provider'] = {}
    ks._last_resort_logged.clear()
    path = ks._STATS_PATH
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _patch_siblings(mapping: dict):
    """Force _list_siblings to return the given mapping.

    mapping = {provider_id: [key_name, ...]}
    """
    def fake_list_siblings(provider_id):
        names = mapping.get(provider_id or 'default', [])
        return [ks._pair_key(provider_id or 'default', n) for n in names]
    ks._list_siblings = fake_list_siblings


def _force_exhaust(provider_id, key_name):
    """Directly stamp exhausted=True without spamming 100 429s."""
    pk = ks._pair_key(provider_id, key_name)
    with ks._lock:
        ks._ensure_fresh_unlocked()
        entry = ks._cache['stats'].get(pk) or ks._new_entry()
        entry['exhausted'] = True
        ks._cache['stats'][pk] = entry


def _force_bad_success_rate(provider_id, key_name):
    """Drive success-rate below threshold (5 failures, 0 success)."""
    pk = ks._pair_key(provider_id, key_name)
    with ks._lock:
        ks._ensure_fresh_unlocked()
        entry = ks._cache['stats'].get(pk) or ks._new_entry()
        entry['success'] = 0
        entry['failure'] = ks.MIN_ATTEMPTS
        ks._cache['stats'][pk] = entry


def _assert(cond, msg):
    if not cond:
        print('FAIL:', msg)
        sys.exit(1)
    print('  PASS:', msg)


# ══════════════════════════════════════════════════════════════
#  Scenario (a): two keys, one exhausted → other stays enabled
# ══════════════════════════════════════════════════════════════
def scenario_a():
    print('\n[a] two keys, one exhausted → sibling unaffected')
    _reset_state()
    _patch_siblings({'p1': ['p1_key_0', 'p1_key_1']})
    _force_exhaust('p1', 'p1_key_0')
    _assert(ks.is_key_enabled('p1', 'p1_key_0') is False,
            'exhausted key is disabled (sibling still raw-enabled)')
    _assert(ks.is_key_enabled('p1', 'p1_key_1') is True,
            'healthy sibling remains enabled (no regression)')
    # Stats row should NOT mark exhausted key as last_resort because sibling is healthy.
    row0 = ks.get_today_stats('p1', 'p1_key_0')
    _assert(row0['exhausted'] is True, 'row reports exhausted=True')
    _assert(row0['last_resort'] is False,
            'last_resort=False when sibling is raw-enabled')
    _assert(row0['enabled'] is False, 'enabled=False when sibling is healthy')


# ══════════════════════════════════════════════════════════════
#  Scenario (b): two keys, both would auto-disable → exactly ONE
#                is promoted (healthier beats exhausted)
# ══════════════════════════════════════════════════════════════
def scenario_b():
    print('\n[b] two keys both would auto-disable → healthier wins, other stays off')
    _reset_state()
    _patch_siblings({'p1': ['p1_key_0', 'p1_key_1']})
    _force_exhaust('p1', 'p1_key_0')           # billing-exhausted (worse)
    _force_bad_success_rate('p1', 'p1_key_1')  # bad rate but not exhausted
    enabled0 = ks.is_key_enabled('p1', 'p1_key_0')
    enabled1 = ks.is_key_enabled('p1', 'p1_key_1')
    _assert(enabled0 is False, 'exhausted key_0 stays disabled')
    _assert(enabled1 is True, 'healthier key_1 promoted as last-resort')
    row0 = ks.get_today_stats('p1', 'p1_key_0')
    row1 = ks.get_today_stats('p1', 'p1_key_1')
    _assert(row0['last_resort'] is False, 'row0 last_resort=False')
    _assert(row1['last_resort'] is True, 'row1 last_resort=True')
    _assert(row0['enabled'] is False,
            'row0 enabled=False (not the chosen last-resort)')
    _assert(row1['enabled'] is True, 'row1 enabled=True (chosen)')


# ══════════════════════════════════════════════════════════════
#  Scenario (b2): two keys, same health → tie-break to LAST
#                 (matches user intuition "keep the last one")
# ══════════════════════════════════════════════════════════════
def scenario_b2():
    print('\n[b2] two equally-bad keys → LAST key wins the tie-break')
    _reset_state()
    _patch_siblings({'p1': ['p1_key_0', 'p1_key_1']})
    _force_exhaust('p1', 'p1_key_0')
    _force_exhaust('p1', 'p1_key_1')
    _assert(ks.is_key_enabled('p1', 'p1_key_0') is False,
            'earlier duplicate stays disabled')
    _assert(ks.is_key_enabled('p1', 'p1_key_1') is True,
            'last duplicate is the chosen last-resort')


# ══════════════════════════════════════════════════════════════
#  Scenario (b3): reported bug — 100% success key alongside
#                 invalid/bad one must NOT be touched, and the
#                 bad one must stay disabled.
# ══════════════════════════════════════════════════════════════
def scenario_b3():
    print('\n[b3] healthy + invalid siblings → healthy keeps working, invalid stays off')
    _reset_state()
    _patch_siblings({'p1': ['p1_key_0', 'p1_key_1']})
    # key_0 is perfectly healthy (>= MIN_ATTEMPTS success)
    with ks._lock:
        ks._ensure_fresh_unlocked()
        entry = ks._cache['stats'].setdefault('p1::p1_key_0', ks._new_entry())
        entry['success'] = 3794
        entry['failure'] = 13
    # key_1 is invalid — fails every call
    _force_bad_success_rate('p1', 'p1_key_1')
    _assert(ks.is_key_enabled('p1', 'p1_key_0') is True,
            'healthy key_0 remains enabled (raw-enabled path)')
    _assert(ks.is_key_enabled('p1', 'p1_key_1') is False,
            'invalid key_1 stays disabled (sibling is healthy)')
    row0 = ks.get_today_stats('p1', 'p1_key_0')
    row1 = ks.get_today_stats('p1', 'p1_key_1')
    _assert(row0['last_resort'] is False,
            'healthy key NOT flagged as last_resort')
    _assert(row1['last_resort'] is False,
            'invalid key NOT flagged as last_resort (sibling healthy)')


# ══════════════════════════════════════════════════════════════
#  Scenario (b4): reported screenshot — key_0 has huge success
#                 history but a sticky exhausted flag, key_1 is
#                 truly broken.  key_0 must win last-resort.
# ══════════════════════════════════════════════════════════════
def scenario_b4():
    print('\n[b4] screenshot case — exhausted-but-high-success wins over truly-broken')
    _reset_state()
    _patch_siblings({'p1': ['p1_key_0', 'p1_key_1']})
    # key_0 mirrors the screenshot: 3794 successes, 13 failures,
    # but an earlier 429-streak left exhausted=True.
    with ks._lock:
        ks._ensure_fresh_unlocked()
        e0 = ks._cache['stats'].setdefault('p1::p1_key_0', ks._new_entry())
        e0['success'] = 3794
        e0['failure'] = 13
        e0['rate_limited'] = 134
        e0['exhausted'] = True  # sticky from prior streak
    # key_1 mirrors the second row: 0 success, many 429s, no real calls.
    with ks._lock:
        e1 = ks._cache['stats'].setdefault('p1::p1_key_1', ks._new_entry())
        e1['success'] = 0
        e1['failure'] = 0
        e1['rate_limited'] = 254
        e1['exhausted'] = True
    # Both are raw-disabled → healthier (key_0) must win.
    _assert(ks.is_key_enabled('p1', 'p1_key_0') is True,
            'key_0 (3794 successes) promoted as last-resort')
    _assert(ks.is_key_enabled('p1', 'p1_key_1') is False,
            'key_1 (0 successes) stays disabled')


# ══════════════════════════════════════════════════════════════
#  Scenario (c): manual override=False on would-be-last-resort
#                key still returns False
# ══════════════════════════════════════════════════════════════
def scenario_c():
    print('\n[c] manual override=False wins over last-resort guard')
    _reset_state()
    _patch_siblings({'p1': ['p1_key_0']})
    _force_exhaust('p1', 'p1_key_0')
    ks.set_key_override('p1', 'p1_key_0', False)
    _assert(ks.is_key_enabled('p1', 'p1_key_0') is False,
            'explicit False override force-disables the only key')
    row = ks.get_today_stats('p1', 'p1_key_0')
    _assert(row['enabled'] is False, 'stats row reflects user disable')


# ══════════════════════════════════════════════════════════════
#  Scenario (d): single-key provider never auto-disables
# ══════════════════════════════════════════════════════════════
def scenario_d():
    print('\n[d] single-key provider never auto-disables')
    _reset_state()
    _patch_siblings({'p_solo': ['p_solo_key_0']})
    # success-rate path
    _force_bad_success_rate('p_solo', 'p_solo_key_0')
    _assert(ks.is_key_enabled('p_solo', 'p_solo_key_0') is True,
            'sole key stays enabled under bad success rate')
    # exhausted path
    _force_exhaust('p_solo', 'p_solo_key_0')
    _assert(ks.is_key_enabled('p_solo', 'p_solo_key_0') is True,
            'sole key stays enabled even when exhausted')
    row = ks.get_today_stats('p_solo', 'p_solo_key_0')
    _assert(row['last_resort'] is True, 'row flagged as last_resort')
    _assert(row['enabled'] is True, 'row enabled=True (last-resort override)')


# ══════════════════════════════════════════════════════════════
#  Scenario (e): providers are isolated — auto-disable in A is
#                unaffected by keys in B
# ══════════════════════════════════════════════════════════════
def scenario_e():
    print('\n[e] cross-provider isolation')
    _reset_state()
    _patch_siblings({
        'p_your-provider': ['p_your-provider_key_0'],
        'p_openai':  ['p_openai_key_0', 'p_openai_key_1'],
    })
    _force_exhaust('p_openai', 'p_openai_key_0')
    # OpenAI still has key_1, so key_0 must stay disabled.
    _assert(ks.is_key_enabled('p_openai', 'p_openai_key_0') is False,
            'openai key_0 disabled (key_1 healthy)')
    # YourProvider is untouched by OpenAI's state.
    _assert(ks.is_key_enabled('p_your-provider', 'p_your-provider_key_0') is True,
            'your-provider key unaffected by openai state')


def main():
    try:
        scenario_a()
        scenario_b()
        scenario_b2()
        scenario_b3()
        scenario_b4()
        scenario_c()
        scenario_d()
        scenario_e()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        print('UNEXPECTED ERROR:', e)
        sys.exit(2)
    print('\nAll last-resort guard scenarios passed ✅')
    sys.exit(0)


if __name__ == '__main__':
    main()
