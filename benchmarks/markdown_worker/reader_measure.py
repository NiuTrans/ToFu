"""Owned-browser mixed-tail/completion comparison against frozen prior owners.

Actual Marked/DOMPurify/highlight.js plus production text renderer and forced
layout. No backend/CSP/input-paint claim. CPU is a separate transition-cohort
mean (includes preparatory stream render for completion), not raw CPU/op.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import threading

from tests._runtime_sections import ROOT, native_module_path

REFERENCE_COMMIT = '5c55a9d1b'
OWNER = 'frontend/src/conversation/ui/renderers/'


def bundles(folder):
    folder = Path(folder)
    reference = {}
    for name in ('stable-dom.ts', 'text-blocks.ts'):
        original = subprocess.check_output(['/usr/bin/git', 'show', REFERENCE_COMMIT + ':' + OWNER + name], cwd=ROOT, text=True)
        def imports(match):
            target = folder / 'stable-dom.ts' if name == 'text-blocks.ts' and match[2] == './stable-dom' else (ROOT / OWNER / match[2]).resolve()
            return 'from ' + json.dumps(str(target))
        rewritten = re.sub(r"from (['\"])(\.[^'\"]+)\1", imports, original)
        (folder/name).write_text(rewritten)
        reference[name] = hashlib.sha256(original.encode()).hexdigest()
    highlighter = folder/'highlighter.ts'
    highlighter.write_text('import hljs from ' + json.dumps(str(ROOT/'node_modules/highlight.js/lib/common.js')) + '; export const readerHighlighter=hljs;\n')
    return {
        'reference': native_module_path('.native/reader-reference.js', folder/'text-blocks.ts'),
        'candidate': native_module_path('.native/reader-candidate.js', ROOT/OWNER/'text-blocks.ts'),
        'highlighter': native_module_path('.native/reader-highlighter.js', highlighter),
        'policy': native_module_path('.native/reader-policy.js', ROOT/'frontend/src/markdown-policy.ts'),
        'markdown': native_module_path('.native/reader-markdown.js', ROOT/'frontend/src/markdown/index.ts'),
    }, reference


def run(browser, origin, compiled, variant, case, samples, memory=False):
    options = {'headless': True, 'args': ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage']}
    if os.environ.get('TOFU_E2E_EXECUTABLE_PATH'):
        options['executable_path'] = os.environ['TOFU_E2E_EXECUTABLE_PATH']
    owned = browser.browser_type.launch(**options)
    stop = threading.Event(); sampler = None; peak = [0]; observations = [0]; errors = []
    try:
        page = owned.new_page(viewport={'width': 1180, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(origin, wait_until='load')
        for source in ('node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js'):
            page.add_script_tag(path=str(ROOT/source))
        for key in ('policy', 'markdown', 'highlighter', variant):
            page.add_script_tag(path=compiled[key])
        page.add_script_tag(path=str(ROOT/'benchmarks/markdown_worker/reader_measure.js'))
        page.evaluate('document.fonts.ready')
        page.evaluate('caseName => { globalThis.readerMeasure=createReaderMeasureFixture({caseName}); readerMeasure.warm(0); }', case)
        session = owned.new_browser_cdp_session()
        def processes():
            return session.send('SystemInfo.getProcessInfo')['processInfo']
        initial = processes()
        pids = [int(row['id']) for row in initial if row['type'] == 'renderer']
        assert pids
        if memory:
            import psutil
            def sample():
                while not stop.is_set():
                    try:
                        peak[0] = max(peak[0], sum(psutil.Process(pid).memory_info().rss for pid in pids))
                        observations[0] += 1
                    except Exception as error:
                        errors.append(str(error)); return
                    stop.wait(.005)
            sampler = threading.Thread(target=sample, name='reader-rss-observer')
            sampler.start()
        result = page.evaluate('args => readerMeasure.latency(args.count,args.capture)', {'count': samples, 'capture': not memory})
        stop.set()
        if sampler:
            sampler.join(timeout=5); assert not sampler.is_alive()
            assert observations[0] > 0
        assert pids == [int(row['id']) for row in processes() if row['type'] == 'renderer']
        page.evaluate('readerMeasure.warm(10000)')
        before = processes()
        page.evaluate('count => readerMeasure.cpu(count)', samples)
        after = processes()
        assert {row['id'] for row in before} == {row['id'] for row in after}
        cpu = (sum(row['cpuTime'] for row in after) - sum(row['cpuTime'] for row in before)) * 1000 / samples
        assert page.evaluate('readerMeasure.oracle()') == (0 if memory else samples)
        assert not errors, errors
        page.evaluate('readerMeasure.close()')
        raw = result['raw']; ordered = sorted(raw)
        return {'browser_pid': next(int(row['id']) for row in initial if row['type'] == 'browser'),
                'renderer_pids': pids, 'wall_samples_ms': raw,
                'p50_ms': statistics.median(raw), 'p95_ms': ordered[int((samples-1)*.95)],
                'p99_ms': ordered[int((samples-1)*.99)],
                'cpu_ms_per_transition_separate_cohort': cpu,
                'sampled_peak_renderer_rss_bytes': peak[0] if memory else None,
                'rss_samples': observations[0], 'latency_oracle_frames': 0 if memory else samples,
                'final_source_sha256': hashlib.sha256(result['finalSource'].encode()).hexdigest()}
    finally:
        stop.set()
        if sampler:
            sampler.join(timeout=5)
        owned.close()


def measure(browser, origin, folder, destination, *, samples=100, pairs=3, cases=None):
    assert samples > 0 and pairs > 0
    compiled, reference = bundles(folder)
    compiled_digests = {name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                        for name, path in compiled.items()}
    sources = ['benchmarks/markdown_worker/reader_measure.py', 'benchmarks/markdown_worker/reader_measure.js',
               'frontend/src/markdown-policy.ts', OWNER+'text-blocks.ts', OWNER+'stable-dom.ts',
               'node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js',
               'node_modules/highlight.js/package.json', 'static/styles.css']
    sources += [str(path.relative_to(ROOT)) for path in sorted((ROOT/'frontend/src/markdown').glob('*.ts'))]
    # Includes all bundled highlight grammars, not just a package version label.
    sources += [str(path.relative_to(ROOT)) for path in sorted((ROOT/'node_modules/highlight.js/lib').rglob('*.js'))]
    digests = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}
    report = {'format': 'tofu.reader-reconciliation-measurement/v1', 'scope': __doc__,
              'whole_system_verdict': 'incomplete', 'performance_admission': False,
              'browser': browser.version, 'python': sys.version, 'platform': platform.platform(),
              'affinity': sorted(os.sched_getaffinity(0)), 'sources': digests,
              'reference_commit': REFERENCE_COMMIT, 'reference_sources': reference,
              'compiled_owner_graphs_sha256': compiled_digests,
              'samples': samples, 'warmup_operations': 20, 'required_pairs': pairs, 'cases': [],
              'limitations': ['CPU is a transition-cohort mean, not per-operation CPU vectors',
                              'Completion CPU includes stream preparation; latency times terminal render only',
                              'RSS sampled every5ms; no retained memory or cold-start admission',
                              'Owned frontend origin; full app/CSP/input-to-paint absent']}
    identities = set()
    for case in cases or ('mixed_tail', 'completion'):
        row = {'case': case, 'pairs': []}; report['cases'].append(row)
        for pair in range(pairs):
            values = {}
            for variant in (('reference', 'candidate') if pair % 2 == 0 else ('candidate', 'reference')):
                for name, digest in compiled_digests.items():
                    assert hashlib.sha256(Path(compiled[name]).read_bytes()).hexdigest() == digest, name
                for name, digest in digests.items():
                    assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
                result = run(browser, origin, compiled, variant, case, samples)
                memory = run(browser, origin, compiled, variant, case, samples, memory=True)
                for name, digest in digests.items():
                    assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
                for cohort in (result, memory):
                    assert cohort['browser_pid'] not in identities
                    identities.add(cohort['browser_pid'])
                assert result['final_source_sha256'] == memory['final_source_sha256']
                result.update(memory_browser_pid=memory['browser_pid'],
                              sampled_peak_renderer_rss_bytes=memory['sampled_peak_renderer_rss_bytes'],
                              rss_samples=memory['rss_samples'])
                values[variant] = result
            assert values['reference']['final_source_sha256'] == values['candidate']['final_source_sha256']
            row['pairs'].append(values)
            Path(destination).write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
    return report
