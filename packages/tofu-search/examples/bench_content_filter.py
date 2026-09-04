"""Benchmark: step-5 LLM content-filter cost BEFORE vs AFTER the 0.6.0 rework.

No real LLM is called — a fake callable simulates per-request latency with a
documented model and produces mode-appropriate responses:

  prompt processing : in_chars  / 20_000 chars/s
  generation        : out_chars /    600 chars/s   (cheap-model order of magnitude)

Wall time is scaled by TIME_SCALE so the bench finishes in seconds; reported
"simulated seconds" = wall / TIME_SCALE.

Rows:
  BEFORE      — pre-0.6 behaviour: rewrite mode, orchestrator's min_chars=0
                override (every page through the LLM), no result cache.
  AFTER cold  — 0.6.0 defaults: gate mode (verdict-only, capped input),
                filter_min_chars short-circuit restored, cold cache.
  AFTER warm  — same, second identical search: all verdicts served from the
                result cache, zero LLM calls.

Run:  python examples/bench_content_filter.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tofu_search.fetch.content_filter as cf
from tofu_search.config import SearchConfig
from tofu_search.fetch.content_filter import filter_web_contents_batch

PROMPT_RATE = 20_000   # chars/s prompt processing
OUT_RATE = 600         # chars/s generation
TIME_SCALE = 0.01      # wall = simulated * scale

PAGES = [
    ('https://example.com/long1', 'A' * 60_000),
    ('https://example.com/long2', 'B' * 60_000),
    ('https://example.com/long3', 'C' * 60_000),
    ('https://example.com/long4', 'D' * 60_000),
    ('https://example.com/mid',   'E' * 10_000),
    ('https://example.com/short', 'F' * 1_500),   # below filter_min_chars=3000
]


def _make_latency_llm(stats):
    """Fake LLM: sleeps per the latency model, answers per mode."""
    def fn(messages, **kwargs):
        stats['calls'] += 1
        system = messages[0]['content']
        user = messages[1]['content']
        in_chars = len(system) + len(user)
        if 'relevance judge' in system:                # gate mode
            out_chars = len('[USEFUL]')
            response = '[USEFUL]'
        else:                                          # rewrite mode
            # cleaned output ≈ 85% of the page body sent
            out_chars = int(in_chars * 0.85)
            response = '[USEFUL]\n' + 'CleanedPassage. ' * (out_chars // 16)
        simulated = in_chars / PROMPT_RATE + out_chars / OUT_RATE
        stats['simulated_llm_s'] += simulated
        time.sleep(simulated * TIME_SCALE)
        return response
    return fn


def _run(label, *, mode, min_chars, cache_ttl, reset_cache=True):
    if reset_cache:
        cf._reset_filter_cache()
    stats = {'calls': 0, 'simulated_llm_s': 0.0}
    cfg = SearchConfig(
        filter_enabled=True,
        llm_function=_make_latency_llm(stats),
        filter_mode=mode,
        filter_min_chars=3_000,
        filter_timeout=45,
        gate_input_max_chars=12_000,
        filter_cache_ttl=cache_ttl,
        filter_cache_max_size=500,
    )
    t0 = time.time()
    results = filter_web_contents_batch(PAGES, query='benchmark query',
                                        min_chars=min_chars, config=cfg)
    wall = time.time() - t0
    irrelevant = sum(1 for v in results.values() if v == cf.IRRELEVANT_SENTINEL)
    print(f'{label:<22} llm_calls={stats["calls"]:<3} '
          f'step5_simulated={wall / TIME_SCALE:7.2f}s  '
          f'(wall {wall:5.2f}s)  pages_served={len(results) - irrelevant}/{len(PAGES)}')
    return wall / TIME_SCALE


def main():
    print(f'latency model: prompt {PROMPT_RATE:,} chars/s, generation {OUT_RATE:,} chars/s, '
          f'TIME_SCALE={TIME_SCALE}\n')
    before = _run('BEFORE (0.5.x)', mode='rewrite', min_chars=0, cache_ttl=0)
    after_cold = _run('AFTER gate, cold cache', mode='gate', min_chars=None, cache_ttl=600)
    after_warm = _run('AFTER gate, warm cache', mode='gate', min_chars=None,
                      cache_ttl=600, reset_cache=False)
    print(f'\nspeedup cold: {before / max(after_cold, 1e-9):.0f}x   '
          f'warm: {before / max(after_warm, 1e-9):.0f}x')


if __name__ == '__main__':
    main()
