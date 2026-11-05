"""Reader-visible scroll stability of the shipped conversation Surface.

Seeds realistic synthetic transcripts through the public settled-Turn API and
installs an in-page sampler that records, after every painted frame, the
viewport-relative geometry of visible blocks plus every programmatic write to
the chat scroll offset. ``summarize_frames`` turns one gesture's trace into
reader-visible discontinuities. Measurement only: callers own assertions.
Used by ``tests/visual/test_visual_conversation_scroll_stability.py``.
"""
import json
import random

WORDS = (
    'stream render anchor window history layout surface viewport turn block '
    'budget frame commit reader scroll latency paint compositor markdown '
    'highlight table cache index storage sync projection lane settle'
).split()


def _sentence(rng):
    words = [rng.choice(WORDS) for _ in range(rng.randint(8, 18))]
    return ' '.join(words).capitalize() + '.'


def _paragraph(rng, low=2, high=5):
    return ' '.join(_sentence(rng) for _ in range(rng.randint(low, high)))


def _code(rng, language, index):
    if language == 'python':
        lines = [f'def handler_{index}(request, *, budget={rng.randint(2, 64)}):',
                 '    """Synthetic handler."""']
        lines += [f'    value_{k} = request.get("{rng.choice(WORDS)}", {k}) * budget'
                  for k in range(rng.randint(4, 18))]
        lines.append('    return {"ok": True, "value": value_0}')
    elif language == 'javascript':
        lines = [f'export function render{index}(node, state) {{']
        lines += [f'  node.dataset.{rng.choice(WORDS)} = String(state.{rng.choice(WORDS)} ?? {k});'
                  for k in range(rng.randint(4, 16))]
        lines.append('  return node;\n}')
    else:
        lines = [f'git log --oneline -{rng.randint(3, 40)} -- frontend/src/{rng.choice(WORDS)}.ts']
        lines += [f'npm run {rng.choice(WORDS)} -- --budget {rng.randint(1, 99)}'
                  for _ in range(rng.randint(2, 8))]
    return '\n'.join(lines)


def _assistant_markdown(rng, index):
    size = rng.choice(['short', 'medium', 'medium', 'long'])
    if size == 'short':
        return _paragraph(rng, 1, 3)
    parts = [f'## Step {index}: {rng.choice(WORDS)} {rng.choice(WORDS)}', _paragraph(rng)]
    language = rng.choice(['python', 'javascript', 'bash'])
    parts.append(f'```{language}\n{_code(rng, language, index)}\n```')
    parts.append('\n'.join(f'- **{rng.choice(WORDS)}**: {_sentence(rng)}'
                           for _ in range(rng.randint(3, 6))))
    if size == 'long':
        parts.append(_paragraph(rng, 3, 6))
        rows = '\n'.join(f'| {rng.choice(WORDS)} | {rng.randint(1, 999)} ms | {rng.randint(1, 999)} ms |'
                         for _ in range(rng.randint(3, 7)))
        parts.append('| Metric | Before | After |\n|---|---|---|\n' + rows)
        parts.append(f'Inline math $O(n \\log n)$ and display math:\n\n'
                     f'$$\\sum_{{k=0}}^{{{index}}} k^2 = \\frac{{{index}({index}+1)(2\\cdot {index}+1)}}{{6}}$$')
        language = rng.choice(['python', 'javascript', 'bash'])
        parts.append(f'```{language}\n{_code(rng, language, index + 1000)}\n```')
    parts.append(_paragraph(rng, 1, 3))
    return '\n\n'.join(parts)


def _context_snapshot(rng, index):
    return {'blockId': f'ctx-{index}', 'snapshot': {
        'model': rng.choice(['claude-sonnet-4.5', 'gpt-5', 'qwen3-max']),
        'depth': rng.choice(['low', 'medium', 'high']),
        'modes': rng.sample(['agent', 'search', 'project'], rng.randint(1, 3)),
        'tools': [f'{rng.choice(WORDS)}_{k}' for k in range(rng.randint(2, 6))],
        'roots': ['tofu'],
    }}


def seed_transcript(context, origin, cid, rounds, seed=7, profile='mixed'):
    """Append ``rounds`` human/assistant pairs, each human Turn with a context rail.

    ``mixed`` replies carry Markdown/code/tables/math and every fourth uses
    tools; ``short`` is a brisk chat whose Turns are shorter than the
    window's per-Turn height estimate; ``tiny`` exchanges one-line Turns, so
    a count-sized window spans about one viewport.
    """
    rng = random.Random(seed)

    def append(command, actor, projection, create=False):
        body = {'commandId': f'{cid}-{command}', 'actor': actor, 'kind': 'text',
                'projection': projection}
        if create:
            body['conversation'] = {'allowCreate': True, 'title': cid,
                                    'createdAt': 100, 'settings': {}}
        response = context.request.post(
            f'{origin}/api/v4/conversations/{cid}/turns/settled', data=body)
        assert response.status == 200, response.text()

    for index in range(rounds):
        question = ' '.join(_sentence(rng) for _ in range(
            1 if profile == 'tiny' else rng.randint(1, 3)))
        if profile == 'tiny':
            question = ' '.join(question.split()[:6])
        append(f'q{index}', 'human', {'content': f'Question {index}. {question}',
                                      'contextSnapshot': _context_snapshot(rng, index)},
               create=index == 0)
        if profile == 'tiny':
            body = ' '.join(_sentence(rng).split()[:6])
        elif profile == 'short':
            body = _paragraph(rng, 1, 3)
        else:
            body = _assistant_markdown(rng, index)
        answer = f'Answer {index}. ' + body
        segments, tool_rounds = [], []
        if profile == 'mixed' and index % 4 == 3:
            for step in range(rng.randint(3, 6)):
                call_id = f'call-{index}-{step}'
                identity = {'llmRound': step, 'attemptId': f'attempt-{index}',
                            'taskId': f'task-{index}'}
                record = {**identity, 'roundNum': step + 1, 'toolCallId': call_id,
                          'toolName': 'read_files', 'status': 'done',
                          'toolArgs': {'path': f'src/{rng.choice(WORDS)}.ts'},
                          'toolContent': 'Synthetic file contents. ' * 40}
                tool_rounds.append(record)
                segments += [
                    {'type': 'thinking', 'blockId': f'thinking-{step}',
                     'text': _paragraph(rng, 1, 2), **identity},
                    {'type': 'tool_use', 'blockId': f'tool-{step}', 'id': call_id,
                     'name': 'read_files', 'input': record['toolArgs'],
                     'result': {'status': 'done', 'content': record['toolContent']},
                     **identity},
                ]
        segments.append({'type': 'text', 'blockId': 'answer', 'text': answer,
                         'terminal': True, 'deliverable': True})
        projection = {'content': answer, 'segments': segments}
        if tool_rounds:
            projection['toolRounds'] = tool_rounds
        append(f'a{index}', 'assistant', projection)


# Samples after each frame's paint (rAF -> task), so geometry is what the
# reader just saw. A programmatic scroll write cancels native smooth/momentum
# scrolling and, from offset 0, marks a reader who reached the mounted edge.
SAMPLER = r"""
(() => {
  const viewport = document.querySelector('#chatContainer');
  const surface = () => document.querySelector('#chatInner .conversation-surface');
  const state = {frames: [], loafs: [], writes: [], running: false};
  const native = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
  // Each write records the loaded Turn count, so a correction after a remote
  // page landed (the reader outran the network) is told apart from one where
  // loaded history was left unmounted.
  Object.defineProperty(viewport, 'scrollTop', {
    configurable: true,
    get() { return native.get.call(this); },
    set(value) {
      const before = native.get.call(this);
      native.set.call(this, value);
      if (state.running) state.writes.push([+performance.now().toFixed(1),
        +before.toFixed(1), +native.get.call(this).toFixed(1),
        Number(surface()?.dataset.windowTotal ?? -1)]);
    },
  });
  const sample = () => {
    const bounds = viewport.getBoundingClientRect();
    const root = surface();
    const blocks = [];
    let turns = 0;
    for (const node of root?.querySelectorAll('[data-turn-id]') || []) {
      turns += 1;
      const rect = node.getBoundingClientRect();
      if (rect.bottom <= bounds.top || rect.top >= bounds.bottom) continue;
      const id = node.dataset.presentationId || node.dataset.turnId;
      const seen = new Set();
      for (const block of node.querySelectorAll('[data-block-id]')) {
        const key = id + '/' + block.dataset.blockId;
        if (seen.has(key)) continue;
        seen.add(key);
        const r = block.getBoundingClientRect();
        if (!r.height || r.bottom <= bounds.top || r.top >= bounds.bottom) continue;
        blocks.push([key, +(r.top - bounds.top).toFixed(1), +r.height.toFixed(1)]);
      }
    }
    state.frames.push({
      t: +performance.now().toFixed(1), st: +viewport.scrollTop.toFixed(1),
      ch: viewport.clientHeight,
      ws: root ? Number(root.dataset.windowStart) : -1,
      we: root ? Number(root.dataset.windowEnd) : -1,
      wt: root ? Number(root.dataset.windowTotal) : -1,
      nodes: document.getElementsByTagName('*').length,
      turns, blocks,
    });
  };
  const loop = () => {
    if (!state.running) return;
    requestAnimationFrame(() => { setTimeout(sample, 0); loop(); });
  };
  try {
    new PerformanceObserver((list) => {
      if (!state.running) return;
      for (const entry of list.getEntries()) state.loafs.push(entry.duration);
    }).observe({type: 'long-animation-frame'});
  } catch (_) {}
  window.__scrollProbe = {
    start() {
      state.frames = []; state.loafs = []; state.writes = [];
      state.running = true; loop();
    },
    stop() { state.running = false; return JSON.parse(JSON.stringify(state)); },
  };
})();
"""


def summarize_frames(data, direction, step):
    """Reader-visible discontinuities between consecutive painted frames.

    ``direction`` is the gesture: 'up' moves content down by at most ``step``
    pixels per input event. A frame may carry two events, so only movement
    against the gesture, content moving unevenly within the viewport, or a
    visible block vanishing counts as a jump.
    """
    frames = data['frames']
    writes = data['writes']
    edge = [w for w in writes if w[1] <= 0.5 and abs(w[2] - w[1]) > 0.5]

    def loaded_before(at):
        earlier = [frame['wt'] for frame in frames if frame['t'] < at]
        return earlier[-1] if earlier else -1

    report = {
        'frames': len(frames),
        # The reader reached the mounted top although loaded history remained.
        'edge_writes': [w for w in edge if w[3] <= loaded_before(w[0])],
        # The reader outran a remote page; it landed at the loaded top.
        'page_edge_writes': len([w for w in edge if w[3] > loaded_before(w[0])]),
        'correction_px': round(sum(abs(w[2] - w[1]) for w in writes), 1),
        'reverse': [], 'uneven': [], 'vanished': [],
        'commits': 0, 'max_turns': 0, 'max_nodes': 0,
        'long_frames': len(data['loafs']),
        'max_long_frame': round(max(data['loafs'], default=0), 1),
    }
    previous = None
    for frame in frames:
        report['max_turns'] = max(report['max_turns'], frame['turns'])
        report['max_nodes'] = max(report['max_nodes'], frame['nodes'])
        if previous is not None:
            if (frame['ws'], frame['we']) != (previous['ws'], previous['we']):
                report['commits'] += 1
            before = {key: (top, height) for key, top, height in previous['blocks']}
            after = {key: top for key, top, _ in frame['blocks']}
            moves = sorted(after[key] - before[key][0] for key in before if key in after)
            if moves:
                median = moves[len(moves) // 2]
                if moves[-1] - moves[0] > 1.5:
                    report['uneven'].append([frame['t'], round(moves[-1] - moves[0], 1)])
                if (median < -1.5) if direction == 'up' else (median > 1.5):
                    report['reverse'].append([frame['t'], round(median, 1)])
                for key, (top, height) in before.items():
                    predicted = top + median
                    if key not in after and predicted < frame['ch'] - 40 and predicted + height > 40:
                        report['vanished'].append([frame['t'], key])
        previous = frame
    return report


def dump(path, payload):
    path.write_text(json.dumps(payload, indent=1) + '\n')
