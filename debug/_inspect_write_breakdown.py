"""One-shot: find the task whose apiRounds match the user's screenshot and
verify the `write` breakdown forward from the raw recorded usage.

Screenshot facts to match:
  round 7: input 2 -> 1.5k, cache 117.3k, write 11.4k;
           write src = prevOutput 6.2k + toolResults 3.3k + envelope 1.9k;
           tools: find_files, run_command  (2 tools)
  round 8: input 1.6k -> 8.6k, cache 123.1k, write 5.9k;
           write src = prevOutput 1.5k + toolResults 905 + envelope 3.6k;
           tools: grep_search x2, find_files  (3 tools)
"""
import json
import sys

sys.path.insert(0, '.')

from lib.database._core import _pool_get


def fmt(n):
    n = int(n or 0)
    if n >= 1000:
        return f'{n/1000:.1f}k'
    return str(n)


def round_tools(rd):
    tc = rd.get('toolCalls')
    if isinstance(tc, list) and tc:
        return tc
    return []


def main():
    db = _pool_get()
    # Pull recent task_results that carry apiRounds in metadata.
    rows = db.execute(
        "SELECT task_id, conv_id, created_at, metadata FROM task_results "
        "WHERE metadata LIKE '%apiRounds%' "
        "ORDER BY created_at DESC LIMIT 4000"
    ).fetchall()
    print(f'scanned {len(rows)} task_results rows with apiRounds')

    candidates = []
    for r in rows:
        try:
            meta = json.loads(r['metadata'] or '{}')
        except Exception:
            continue
        ar = meta.get('apiRounds') or []
        if len(ar) < 8:
            continue
        # Look at round index 6 (=第7轮) and 7 (=第8轮)
        r7 = ar[6]
        r8 = ar[7]
        wb7 = r7.get('writeBreakdown') or {}
        wb8 = r8.get('writeBreakdown') or {}
        w7 = int(wb7.get('write') or 0)
        w8 = int(wb8.get('write') or 0)
        # match write ~11.4k and ~5.9k
        if 11000 <= w7 <= 11900 and 5500 <= w8 <= 6300:
            t7 = round_tools(r7)
            t8 = round_tools(r8)
            candidates.append((r['task_id'], r['conv_id'], r['created_at'], ar))
            print('\n=== CANDIDATE task_id=%s conv=%s ===' % (r['task_id'], r['conv_id']))
            print('  r7 tools=%s  r8 tools=%s' % (t7, t8))

    if not candidates:
        # Relax: just show the most-recent task with >=8 rounds, decompose r7/r8
        print('\nNo exact match; dumping most recent task with >=8 rounds for inspection.')
        for r in rows:
            try:
                meta = json.loads(r['metadata'] or '{}')
            except Exception:
                continue
            ar = meta.get('apiRounds') or []
            if len(ar) >= 8:
                candidates.append((r['task_id'], r['conv_id'], r['created_at'], ar))
                break

    for task_id, conv_id, created_at, ar in candidates[:1]:
        print('\n' + '#' * 70)
        print('TASK', task_id, 'conv', conv_id)
        print('#' * 70)
        decompose(ar)


def decompose(ar):
    """Forward-verify: for each round, recompute write components from raw usage
    of the PREVIOUS round + the stored writeBreakdown, and check they sum."""
    for i, rd in enumerate(ar):
        u = rd.get('usage') or {}
        ri = u.get('prompt_tokens') or u.get('input_tokens') or 0
        ro = u.get('completion_tokens') or u.get('output_tokens') or 0
        rt = u.get('reasoning_tokens') or u.get('thinking_tokens') or 0
        rcr = u.get('cache_read_tokens') or u.get('cache_read_input_tokens') or 0
        rcw = u.get('cache_write_tokens') or u.get('cache_creation_input_tokens') or 0
        wb = rd.get('writeBreakdown') or {}
        tools = round_tools(rd)
        print('\n第%d轮  round=%s  tag=%s' % (i + 1, rd.get('round'), rd.get('tag') or ''))
        print('  usage: input %s -> output %s  (reasoning %s)  cache_read %s  cache_write %s'
              % (fmt(ri), fmt(ro), fmt(rt), fmt(rcr), fmt(rcw)))
        if tools:
            print('  toolCalls: %s' % (tools,))
        # previous round's output (the forward-computed prevOutput)
        if i > 0:
            pu = (ar[i - 1].get('usage') or {})
            prev_out_calc = int(pu.get('completion_tokens') or pu.get('output_tokens') or 0) \
                + int(pu.get('reasoning_tokens') or pu.get('thinking_tokens') or 0)
        else:
            prev_out_calc = 0
        if wb:
            comp = int(wb.get('prevOutput', 0)) + int(wb.get('toolResults', 0)) \
                + int(wb.get('recacheBody', 0)) + int(wb.get('envelope', 0))
            print('  writeBreakdown: write=%s | prevOutput=%s toolResults=%s recacheBody=%s envelope=%s'
                  % (fmt(wb.get('write')), fmt(wb.get('prevOutput')),
                     fmt(wb.get('toolResults')), fmt(wb.get('recacheBody')),
                     fmt(wb.get('envelope'))))
            print('    -> components sum = %d   stored write = %d   MATCH=%s'
                  % (comp, int(wb.get('write') or 0), comp == int(wb.get('write') or 0)))
            print('    -> forward prevOutput from round %d raw usage = %d   (stored prevOutput=%d  MATCH=%s)'
                  % (i, prev_out_calc, int(wb.get('prevOutput') or 0),
                     prev_out_calc == int(wb.get('prevOutput') or 0)
                     or int(wb.get('prevOutput') or 0) == min(prev_out_calc, int(wb.get('write') or 0))))
            print('    -> envelope = write - prevOutput - toolResults - recacheBody = %d'
                  % (int(wb.get('write') or 0) - int(wb.get('prevOutput') or 0)
                     - int(wb.get('toolResults') or 0) - int(wb.get('recacheBody') or 0)))
            if wb.get('capped'):
                print('    -> NOTE: capped=True (output-side tokenizer overshoot, components calibrated to write)')


if __name__ == '__main__':
    main()
