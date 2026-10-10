"""Phase-level owned-browser diagnosis, separate from performance admission.

Private clone of the unchanged paired reader run. CDP phase queries perturb the
fixture, so no latency/CPU numbers from this diagnostic can replace its original
paired evidence. Forced GC occurs only after owned fixture disposal.
"""
import hashlib
import inspect
import json
import os
from pathlib import Path
import platform

from benchmarks.markdown_worker import reader_measure
from tests._runtime_sections import ROOT


def profile_run(browser, origin, compiled, variant, case, samples):
    import psutil
    phases = []
    page_session = None
    browser_session = None
    renderer_pids = None

    def capture(owned, page, phase):
        nonlocal page_session, browser_session, renderer_pids
        if page_session is None:
            page_session = page.context.new_cdp_session(page)
            page_session.send('Performance.enable')
            browser_session = owned.new_browser_cdp_session()
        info = browser_session.send('SystemInfo.getProcessInfo')['processInfo']
        current = [int(row['id']) for row in info if row['type'] == 'renderer']
        if renderer_pids is None:
            renderer_pids = current
        assert current == renderer_pids and current
        metrics = {row['name']: row['value'] for row in page_session.send('Performance.getMetrics')['metrics']}
        high_water = 0
        for pid in current:
            status = (Path('/proc')/str(pid)/'status').read_text()
            row = next(line for line in status.splitlines() if line.startswith('VmHWM:'))
            high_water += int(row.split()[1])*1024
        phases.append({'phase': phase, 'renderer_pids': current,
            'renderer_rss_bytes': sum(psutil.Process(pid).memory_info().rss for pid in current),
            'renderer_high_water_bytes': high_water,
            'js_heap_used_bytes': metrics['JSHeapUsedSize'],
            'js_heap_total_bytes': metrics['JSHeapTotalSize'],
            'dom': page_session.send('Memory.getDOMCounters'),
            'fixture_mounted': page.evaluate("Boolean(document.querySelector('.conversation-text-block'))")})
        if phase == 'disposed':
            page_session.send('HeapProfiler.collectGarbage')
            capture(owned, page, 'disposed_after_gc')

    source = inspect.getsource(reader_measure.run)
    changes = {
        "        page.evaluate('caseName => { globalThis.readerMeasure=createReaderMeasureFixture({caseName}); readerMeasure.warm(0); }', case)":
            "        page.evaluate('caseName => { globalThis.readerMeasure=createReaderMeasureFixture({caseName}); }', case)\n"
            "        capture(owned, page, 'loaded')\n"
            "        page.evaluate('readerMeasure.warm(0)')\n"
            "        capture(owned, page, 'warm')",
        "        page.evaluate('readerMeasure.warm(10000)')":
            "        capture(owned, page, 'latency')\n"
            "        page.evaluate('readerMeasure.warm(10000)')\n        capture(owned, page, 'cpu_warm')",
        "        after = processes()": "        after = processes()\n        capture(owned, page, 'cpu')",
        "        assert not errors, errors": "        assert not errors, errors\n        capture(owned, page, 'oracle')",
        "        page.evaluate('readerMeasure.close()')":
            "        page.evaluate('readerMeasure.close(); delete globalThis.readerMeasure')\n"
            "        capture(owned, page, 'disposed')",
    }
    for before, after in changes.items():
        assert source.count(before) == 1, 'reader profiling seam changed: '+before
        source = source.replace(before, after)
    namespace = dict(reader_measure.run.__globals__, capture=capture)
    exec(compile(source, '<reader-memory-phase-run>', 'exec'), namespace)
    result = namespace['run'](browser, origin, compiled, variant, case, samples, memory=True)
    return {'browser_pid': result['browser_pid'], 'final_source_sha256': result['final_source_sha256'],
            'phases': phases, 'canonical_final_cpu_dom_verified': True}


def profile(browser, origin, folder, destination, *, samples=100, pairs=3):
    assert samples > 0 and pairs > 0
    compiled, reference = reader_measure.bundles(folder)
    names = ('benchmarks/markdown_worker/reader_memory_profile.py',
             'benchmarks/markdown_worker/reader_measure.py', 'benchmarks/markdown_worker/reader_measure.js',
             'static/styles.css')
    sources = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    graphs = {name: hashlib.sha256(Path(path).read_bytes()).hexdigest() for name, path in compiled.items()}
    report = {'scope': __doc__, 'whole_system_verdict': 'incomplete', 'performance_admission': False,
              'browser': browser.version, 'platform': platform.platform(),
              'affinity': sorted(os.sched_getaffinity(0)), 'sources': sources,
              'compiled_owner_graphs_sha256': graphs, 'reference_sources': reference,
              'samples': samples, 'required_pairs': pairs, 'cases': [],
              'limitations': ['CDP phase queries perturb the original fixture; diagnosis only',
                              'Renderer VmHWM since process birth, not full application process tree',
                              'GC only after disposal; no forced-GC performance admission',
                              'Concurrent PDF/repository measurement on this host']}
    identities = set()
    for case in ('mixed_tail', 'completion'):
        row = {'case': case, 'pairs': []}; report['cases'].append(row)
        for pair in range(pairs):
            values = {}
            for variant in (('reference', 'candidate') if pair % 2 == 0 else ('candidate', 'reference')):
                for name, digest in sources.items():
                    assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
                for name, digest in graphs.items():
                    assert hashlib.sha256(Path(compiled[name]).read_bytes()).hexdigest() == digest, name
                value = profile_run(browser, origin, compiled, variant, case, samples)
                assert value['browser_pid'] not in identities
                identities.add(value['browser_pid']); values[variant] = value
            assert values['reference']['final_source_sha256'] == values['candidate']['final_source_sha256']
            row['pairs'].append(values)
            Path(destination).write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
    return report
