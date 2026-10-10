"""Independent browser comparison of witnessed full-document in-place sanitation.

Default installed DOMPurify config only. Canonical parser/renderer/policy/DOM
oracles remain. CPU is a separate cohort mean, not per-operation CPU vectors.
"""
import inspect

from benchmarks.markdown_worker import browser_measure


def run(browser, origin, variant, case, *, samples=100, memory=False):
    source = inspect.getsource(browser_measure.run)
    changes = {
        "'benchmarks/markdown_worker/inline_cache.js'": "'benchmarks/markdown_worker/inplace_sanitizer.js'",
        '          const environment={document,parser,sanitizer:DOMPurify,':
            "          const sanitation=variant==='inplace'?createInplaceDocumentSanitizerExperiment({document,sanitizer:DOMPurify}):null;\n"
            '          const environment={document,parser,sanitizer:sanitation??DOMPurify,',
        'renderer.dispose();cache?.dispose(900001);node.remove();':
            'renderer.dispose();sanitation?.dispose();cache?.dispose(900001);node.remove();',
        'environment.parser=canonical;renderer.clearCache();':
            'environment.parser=canonical;environment.sanitizer=DOMPurify;renderer.clearCache();',
    }
    for before, after in changes.items():
        assert source.count(before)==1, 'sanitation browser measurement seam changed: '+before
        source=source.replace(before, after)
    namespace=dict(browser_measure.run.__globals__)
    exec(compile(source, '<inplace-sanitation-browser-run>', 'exec'), namespace)
    return namespace['run'](browser, origin, variant, case, samples=samples, memory=memory)


def measure(browser, origin, destination, *, samples=100, pairs=3, cases=None):
    source=inspect.getsource(browser_measure.measure)
    before="'benchmarks/markdown_worker/browser_measure.py', 'benchmarks/markdown_worker/inline_cache.js',"
    after="'benchmarks/markdown_worker/inplace_measure.py', 'benchmarks/markdown_worker/browser_measure.py', 'benchmarks/markdown_worker/inplace_sanitizer.js',"
    assert source.count(before)==1, 'sanitation measurement source inventory changed'
    source=source.replace(before, after)
    source=source.replace("'inline'", "'inplace'")
    namespace=dict(browser_measure.measure.__globals__, run=run, __doc__=__doc__)
    exec(compile(source, '<inplace-sanitation-browser-pairs>', 'exec'), namespace)
    return namespace['measure'](browser, origin, destination, samples=samples, pairs=pairs, cases=cases)
