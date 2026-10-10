"""Real browser keys with controlled synchronous production reader contention.

Metric is keydown timestamp to second animation frame, not physical input-to-
display, INP or full-application transport. Terminal stream preparation is part
of this workload. CPU is a browser-process cohort mean, not raw per-key CPU.
"""
import hashlib
import json
import os
from pathlib import Path
import statistics

from tests._runtime_sections import ROOT
from benchmarks.markdown_worker.reader_measure import bundles


def run(browser, origin, compiled, variant, case, samples):
    options = dict(headless=True, args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'])
    if os.environ.get('TOFU_E2E_EXECUTABLE_PATH'):
        options['executable_path'] = os.environ['TOFU_E2E_EXECUTABLE_PATH']
    owned = browser.browser_type.launch(**options)
    try:
        page = owned.new_page(viewport={'width': 1180, 'height': 900})
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(origin, wait_until='load')
        for source in ('node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js'):
            page.add_script_tag(path=str(ROOT / source))
        for key in ('policy', 'markdown', 'highlighter', variant):
            page.add_script_tag(path=compiled[key])
        page.add_script_tag(path=str(ROOT / 'benchmarks/markdown_worker/reader_input.js'))
        page.evaluate('document.fonts.ready')
        page.evaluate('caseName => { globalThis.readerInput=createReaderInputFixture({caseName}); }', case)
        session = owned.new_browser_cdp_session()
        def info(): return session.send('SystemInfo.getProcessInfo')['processInfo']
        identity = info(); before = info()
        for index in range(samples + 20):
            page.keyboard.press('a')
            page.wait_for_function('expected => readerInput.count() === expected', arg=index + 1)
        after = info(); result = page.evaluate('readerInput.results()')
        assert result['warmupKeys'] == 20 and result['completedKeys'] == samples + 20
        assert result['inputValue'] == 'a' * (samples + 20)
        assert page.evaluate('readerInput.oracle()') == samples + 20
        assert not errors, errors
        assert {r['id'] for r in before} == {r['id'] for r in after}
        page.evaluate('readerInput.close()')
        walls = sorted(r['keydown_to_second_raf_ms'] for r in result['raw'])
        return dict(browser_pid=next(int(r['id']) for r in identity if r['type'] == 'browser'),
                    renderer_pids=[int(r['id']) for r in identity if r['type'] == 'renderer'],
                    raw=result['raw'], p50_ms=statistics.median(walls), p95_ms=walls[int((samples - 1) * .95)],
                    p99_ms=walls[int((samples - 1) * .99)], warmup_keys=20,
                    browser_cpu_ms_per_key_including_warmup=(sum(r['cpuTime'] for r in after) - sum(r['cpuTime'] for r in before)) * 1000 / (samples + 20),
                    final_source_sha256=hashlib.sha256(result['finalSource'].encode()).hexdigest())
    finally:
        owned.close()


def measure(browser, origin, folder, destination, samples=100, pairs=3):
    compiled, reference = bundles(folder)
    compiled_hashes = {key: hashlib.sha256(Path(path).read_bytes()).hexdigest() for key, path in compiled.items()}
    names = ['benchmarks/markdown_worker/reader_input.py', 'benchmarks/markdown_worker/reader_input.js',
             'benchmarks/markdown_worker/reader_measure.py', 'static/styles.css',
             'node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js',
             'frontend/src/conversation/ui/renderers/text-blocks.ts', 'frontend/src/conversation/ui/renderers/stable-dom.ts']
    sources = {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in names}
    report = dict(format='tofu.reader-input-contention/v1', scope=__doc__, whole_system_verdict='incomplete',
                  performance_admission=False, samples=samples, required_pairs=pairs, browser=browser.version,
                  sources=sources, reference_sources=reference, compiled_sha256=compiled_hashes,
                  limitations=['controlled render inside real keydown handler, not actual app scheduling',
                               'two RAF callbacks are not proof of physical presentation',
                               'no peak/retained memory or raw CPU/op admission'], cases=[])
    identities = set()
    for case in ('mixed_tail', 'completion'):
        row = dict(case=case, pairs=[]); report['cases'].append(row)
        for pair in range(pairs):
            values = {}
            for variant in (('reference', 'candidate') if pair % 2 == 0 else ('candidate', 'reference')):
                for n, h in sources.items(): assert hashlib.sha256((ROOT / n).read_bytes()).hexdigest() == h
                for key, h in compiled_hashes.items(): assert hashlib.sha256(Path(compiled[key]).read_bytes()).hexdigest() == h
                result = run(browser, origin, compiled, variant, case, samples)
                assert result['browser_pid'] not in identities; identities.add(result['browser_pid'])
                values[variant] = result
            assert values['reference']['final_source_sha256'] == values['candidate']['final_source_sha256']
            row['pairs'].append(values); Path(destination).write_text(json.dumps(report, indent=2) + '\n')
    return report
