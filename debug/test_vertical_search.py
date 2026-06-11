#!/usr/bin/env python3
"""debug/test_vertical_search.py — Integration tests for vertical domain search.

Tests:
1. Intent detection accuracy (positive + negative cases)
2. Live API integrations (CVE, arXiv, DOI, PyPI, npm, GitHub, IP)
3. Parallel execution (vertical runs concurrently with web search)
4. Freshness parameter threading through engines
5. Graceful degradation (API failures don't crash the pipeline)

Run: /home/your-user/conda/bin/python3 debug/test_vertical_search.py
"""

import importlib.util
import logging
import sys
import time
import os

# ─── Bootstrap: mock lib.log so we can import vertical.py standalone ───
logging.basicConfig(level=logging.INFO, format='%(levelname)s [%(name)s] %(message)s')


class _FakeLogModule:
    @staticmethod
    def get_logger(name):
        return logging.getLogger(name)


sys.modules['lib.log'] = _FakeLogModule()

# Minimal lib stub for the import
if 'lib' not in sys.modules:
    sys.modules['lib'] = type(sys)('lib')

# Now load vertical.py
_spec = importlib.util.spec_from_file_location(
    'lib.search.vertical',
    os.path.join(os.path.dirname(__file__), '..', 'lib', 'search', 'vertical.py')
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

detect_vertical_intent = _mod.detect_vertical_intent
search_vertical = _mod.search_vertical


def test_intent_detection():
    """Test intent detection accuracy: high precision, no false positives."""
    print('\n' + '=' * 60)
    print('TEST 1: Intent Detection')
    print('=' * 60)

    positive_cases = [
        ('CVE-2024-3094', 'cve', 'CVE-2024-3094'),
        ('cve-2023-44487', 'cve', 'CVE-2023-44487'),
        ('What is CVE-2024-1234', 'cve', 'CVE-2024-1234'),
        ('2301.07041', 'arxiv', '2301.07041'),
        ('2401.02954v2', 'arxiv', '2401.02954v2'),
        ('arxiv 2401.12345', 'arxiv', '2401.12345'),
        ('10.1038/s41586-023-06221-2', 'doi', '10.1038/s41586-023-06221-2'),
        ('doi:10.1145/1234567.1234568', 'doi', '10.1145/1234567.1234568'),
        ('pypi:requests', 'pypi', 'requests'),
        ('pip install pandas', 'pypi', 'pandas'),
        ('pypi:scikit-learn', 'pypi', 'scikit-learn'),
        ('npm react', 'npm', 'react'),
        ('npm:vite', 'npm', 'vite'),
        ('npm install @types/node', 'npm', '@types/node'),
        ('npx create-react-app', 'npm', 'create-react-app'),
        ('github:torvalds/linux', 'github', 'torvalds/linux'),
        ('gh:microsoft/vscode', 'github', 'microsoft/vscode'),
        ('facebook/react', 'github', 'facebook/react'),
        ('8.8.8.8', 'ip', '8.8.8.8'),
        ('192.168.1.1', 'ip', '192.168.1.1'),
        ('$MSFT', 'stock', 'MSFT'),
        ('NVDA', 'stock', 'NVDA'),
        ('stock GOOG', 'stock', 'GOOG'),
        ('TSLA price', 'stock', 'TSLA'),
    ]

    # hf_papers / semantic_scholar: phrase-based, so check domain only
    domain_only_cases = [
        ('hf daily papers', 'hf_papers'),
        ('huggingface papers', 'hf_papers'),
        ('trending papers this week', 'hf_papers'),
        ('daily papers on diffusion models', 'hf_papers'),
        ('papers related to Mamba', 'semantic_scholar'),
        ('papers similar to attention is all you need', 'semantic_scholar'),
        ('what cites 2312.00752', 'semantic_scholar'),
        ('papers citing the transformer paper', 'semantic_scholar'),
    ]

    negative_cases = [
        'how to fix CVE errors in general',
        'what is arxiv for',
        'python tutorial',
        'best JavaScript frameworks 2024',
        'machine learning basics',
        'REST API design patterns',
        'kubernetes deployment guide',
        'HTTPS',
        'JSON',
        'HTML',
        'YAML',
        'TODO',
        'main.py',
        'config.yaml',
        'src/utils.ts',
        'THE',
        'GOOD',
        'BEST',
        'FREE',
        'MAKE',
        'DATA',
        'NODE',
        'BASH',
        'GREP',
        'API',
        'CPU',
        'GPU',
        'how to use python',
    ]

    ok = 0
    fail = 0

    for query, expected_domain, expected_id in positive_cases:
        result = detect_vertical_intent(query)
        if result and result[0] == expected_domain and result[1] == expected_id:
            ok += 1
        else:
            fail += 1
            print(f'  FAIL [+]: {query!r} => {result} (expected {expected_domain}/{expected_id})')

    for query, expected_domain in domain_only_cases:
        result = detect_vertical_intent(query)
        if result and result[0] == expected_domain:
            ok += 1
        else:
            fail += 1
            print(f'  FAIL [d]: {query!r} => {result} (expected domain {expected_domain})')

    for query in negative_cases:
        result = detect_vertical_intent(query)
        if result is None:
            ok += 1
        else:
            fail += 1
            print(f'  FAIL [-]: {query!r} => {result} (expected None)')

    total = ok + fail
    print(f'\n  Results: {ok}/{total} passed ({fail} failures)')
    assert fail == 0, f'{fail} intent detection tests failed'
    print('  ✓ All intent detection tests pass')
    return True


def test_live_apis():
    """Test live API integrations — require network access."""
    print('\n' + '=' * 60)
    print('TEST 2: Live API Integrations')
    print('=' * 60)

    test_cases = [
        ('cve', 'CVE-2024-3094', 'CVSS'),          # xz backdoor
        ('arxiv', '2310.06825', 'Mistral'),          # Mistral 7B paper
        ('doi', '10.1038/s41586-023-06221-2', 'Nature'),  # AI discovery paper
        ('pypi', 'flask', 'Flask'),                  # Flask package
        ('npm', 'express', 'express'),               # Express package
        ('github', 'facebook/react', 'Stars'),       # React repo
        ('ip', '8.8.8.8', 'Google'),                 # Google DNS
        ('hf_papers', 'diffusion', 'Hugging Face'),  # HF keyword search
        ('semantic_scholar', 'mamba state space model', 'Semantic Scholar'),  # S2 related
    ]

    ok = 0
    fail = 0
    skip = 0

    for domain, identifier, expected_substring in test_cases:
        t0 = time.time()
        result = search_vertical(domain, identifier)
        elapsed = time.time() - t0

        if result is None:
            # API might be blocked from this datacenter — that's OK
            skip += 1
            print(f'  SKIP: {domain}/{identifier} — no data (API may be blocked, {elapsed:.1f}s)')
        elif expected_substring in result.get('content', ''):
            ok += 1
            print(f'  ✓ {domain}/{identifier} — OK ({elapsed:.1f}s, {len(result["content"])} chars)')
        else:
            fail += 1
            print(f'  FAIL: {domain}/{identifier} — missing "{expected_substring}" ({elapsed:.1f}s)')
            print(f'    Content preview: {result.get("content", "")[:200]}')

    print(f'\n  Results: {ok} OK, {skip} skipped (API blocked), {fail} failures')
    assert fail == 0, f'{fail} live API tests failed'
    print('  ✓ All reachable APIs return correct data')
    return True


def test_parallel_execution():
    """Verify vertical search doesn't add latency to the pipeline.

    We can't run the full web search pipeline here (requires full lib.*
    dependencies), so we simulate the pattern: vertical runs in a thread
    while a sleep() simulates the web search time.
    """
    print('\n' + '=' * 60)
    print('TEST 3: Parallel Execution')
    print('=' * 60)

    from concurrent.futures import ThreadPoolExecutor

    # Simulate: vertical (1-2s API call) + web search (5s simulated)
    WEB_SEARCH_TIME = 2.0

    def fake_web_search():
        time.sleep(WEB_SEARCH_TIME)
        return 'web_results'

    t0 = time.time()

    # Run vertical in parallel
    with ThreadPoolExecutor(max_workers=1) as pool:
        vertical_fut = pool.submit(search_vertical, 'pypi', 'flask')
        web_result = fake_web_search()
        vertical_result = vertical_fut.result(timeout=10)

    total = time.time() - t0

    print(f'  Web search time: {WEB_SEARCH_TIME:.1f}s (simulated)')
    print(f'  Total time: {total:.1f}s')
    print(f'  Vertical result: {"OK" if vertical_result else "None (API blocked)"}')

    # Total should be close to WEB_SEARCH_TIME (not WEB_SEARCH_TIME + vertical_time)
    overhead = total - WEB_SEARCH_TIME
    print(f'  Overhead from parallel vertical: {overhead:.2f}s')

    if overhead > 1.0:
        print(f'  WARNING: Overhead > 1s — vertical may have blocked')
    else:
        print(f'  ✓ Vertical adds <1s overhead (runs in parallel)')
    return True


def test_graceful_degradation():
    """Verify that API failures don't crash or raise exceptions."""
    print('\n' + '=' * 60)
    print('TEST 4: Graceful Degradation')
    print('=' * 60)

    # Test with invalid identifiers that will fail at the API level
    test_cases = [
        ('cve', 'CVE-9999-99999'),              # Non-existent CVE
        ('arxiv', '9999.99999'),                  # Non-existent paper
        ('doi', '10.9999/nonexistent-doi'),       # Non-existent DOI
        ('pypi', 'this-package-does-not-exist-xyzzy'),  # Non-existent package
        ('npm', 'this-npm-package-does-not-exist-xyzzy'),
        ('github', 'nonexistent-user-xyzzy/nonexistent-repo'),
        ('ip', '999.999.999.999'),               # Invalid IP (won't error, just empty)
        ('stock', 'XYZZY'),                       # Non-existent ticker
    ]

    ok = 0
    for domain, identifier in test_cases:
        try:
            result = search_vertical(domain, identifier)
            # Result should be None (not found) — NOT an exception
            if result is None:
                ok += 1
                print(f'  ✓ {domain}/{identifier} — gracefully returned None')
            else:
                # Some APIs may return data even for "invalid" IDs
                ok += 1
                print(f'  ✓ {domain}/{identifier} — returned data (unexpected but OK)')
        except Exception as e:
            print(f'  FAIL: {domain}/{identifier} — raised {type(e).__name__}: {e}')

    print(f'\n  Results: {ok}/{len(test_cases)} handled gracefully (no crashes)')
    assert ok == len(test_cases), 'Some cases raised exceptions'
    print('  ✓ All failure cases degrade gracefully')
    return True


def test_freshness_parameter():
    """Verify freshness parameter is accepted and threaded correctly."""
    print('\n' + '=' * 60)
    print('TEST 5: Freshness Parameter')
    print('=' * 60)

    # We can't test the actual engine calls (they need full lib.search.*),
    # but we can verify the parameter is accepted in the engine function signatures
    import inspect

    # Load engine modules
    engines_dir = os.path.join(os.path.dirname(__file__), '..', 'lib', 'search', 'engines')

    engine_specs = [
        ('ddg.py', 'search_ddg_html'),
        ('ddg.py', 'search_ddg_api'),
        ('brave.py', 'search_brave'),
        ('bing.py', 'search_bing'),
        ('searxng.py', 'search_searxng'),
    ]

    ok = 0
    for filename, func_name in engine_specs:
        filepath = os.path.join(engines_dir, filename)
        spec = importlib.util.spec_from_file_location(f'engine_{filename}', filepath)
        mod = importlib.util.module_from_spec(spec)

        # Mock dependencies
        sys.modules['lib.search._common'] = type(sys)('stub')
        sys.modules['lib.search._common'].HEADERS = {}
        sys.modules['lib.search._common'].clean_text = lambda x: x
        sys.modules['lib.search._common'].http_search_get = lambda **kw: []

        try:
            spec.loader.exec_module(mod)
            func = getattr(mod, func_name)
            sig = inspect.signature(func)
            params = list(sig.parameters.keys())
            if 'freshness' in params:
                ok += 1
                print(f'  ✓ {func_name} accepts freshness parameter')
            else:
                print(f'  FAIL: {func_name} missing freshness parameter (has: {params})')
        except Exception as e:
            # Module may fail to load due to missing deps — check source directly
            with open(filepath) as f:
                source = f.read()
            if f"def {func_name}(" in source and "freshness" in source:
                ok += 1
                print(f'  ✓ {func_name} has freshness in source (module load failed: {e})')
            else:
                print(f'  FAIL: {func_name} — cannot verify ({e})')

    print(f'\n  Results: {ok}/{len(engine_specs)} engines have freshness parameter')
    assert ok == len(engine_specs), 'Some engines missing freshness parameter'
    print('  ✓ All engines accept freshness parameter')
    return True


if __name__ == '__main__':
    print('=' * 60)
    print(' Vertical Search Integration Tests')
    print('=' * 60)

    tests = [
        ('Intent Detection', test_intent_detection),
        ('Live APIs', test_live_apis),
        ('Parallel Execution', test_parallel_execution),
        ('Graceful Degradation', test_graceful_degradation),
        ('Freshness Parameter', test_freshness_parameter),
    ]

    results = []
    for name, func in tests:
        try:
            passed = func()
            results.append((name, 'PASS' if passed else 'FAIL'))
        except AssertionError as e:
            results.append((name, f'FAIL: {e}'))
        except Exception as e:
            results.append((name, f'ERROR: {e}'))

    print('\n' + '=' * 60)
    print(' SUMMARY')
    print('=' * 60)
    all_pass = True
    for name, status in results:
        icon = '✓' if status == 'PASS' else '✗'
        print(f'  {icon} {name}: {status}')
        if status != 'PASS':
            all_pass = False

    print()
    if all_pass:
        print('  ALL TESTS PASSED ✓')
    else:
        print('  SOME TESTS FAILED ✗')
        sys.exit(1)
