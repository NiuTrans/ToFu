"""Pure bounded-priority timing ledger; no storage or execution dependencies.

``disjoint_summary`` sorts two edges per interval, then maintains active counts
for the fixed priority vocabulary. Work is O(n log n + k*n), k fixed by contract.
"""


def disjoint_summary(spans, t0, t1, priorities):
    rank = {kind: index for index, (kind, _) in enumerate(priorities)}
    edges = []
    for span in spans:
        index = rank.get(span['kind'])
        if index is None:
            continue
        a, b = span.get('tStart'), span.get('tEnd')
        if a is None or b is None:
            continue
        a, b = max(t0, a), min(t1, b)
        if b > a:
            edges.append((a, index, 1))
            edges.append((b, index, -1))
    edges.sort()
    active = [0] * len(priorities)
    out = {bucket: 0 for _, bucket in priorities}
    cursor = t0
    for timestamp, index, delta in edges:
        if timestamp > cursor:
            for priority, count in enumerate(active):
                if count:
                    out[priorities[priority][1]] += timestamp - cursor
                    break
            cursor = timestamp
        active[index] += delta
    out['unattributedMs'] = max(0, t1 - t0 - sum(out.values()))
    return out
