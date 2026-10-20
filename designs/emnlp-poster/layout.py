"""English A0 research poster: mechanism diagrams and scoped measurements.

Scene owns exporters; this module owns copy, geometry and chart encodings.
Coordinates are millimetres. Font sizes are physical points.
"""
import json
from pathlib import Path

INK = '#17342F'
GREEN = '#176B57'
MINT = '#EAF4EE'
BLUE = '#326EAA'
ICE = '#EAF1F9'
ORANGE = '#C26337'
PEACH = '#FFF0E3'
WHITE = '#FFFFFF'
MUTED = '#53665F'
RULE = '#C7D8CE'
GRAY = '#E9EEEB'
NAVY = '#203C4F'
QR = (748, 1124, 62)
PROOF_SECTIONS = [('header', 0, 205), ('architecture', 205, 502),
                  ('mechanisms', 502, 785), ('tofudb', 785, 1136), ('footer', 1136, 1189)]


def heading(s, n, title, x, y, w, size=35):
    s.rect(x, y, w, 25, GREEN)
    s.rect(x, y, 23, 25, INK)
    s.text(str(n), x+11.5, y+17.7, 32, 'bold', WHITE, align='center')
    s.text(title, x+31, y+17.7, size, 'bold', WHITE)


def folded_sheet(s, x, y, w, h):
    s.rect(x, y, w, h, WHITE, GREEN, .45)
    s.path([(x+w-7, y), (x+w, y), (x+w, y+7)], MINT)
    s.line(x+w-7, y, x+w-7, y+7, GREEN, .4)
    s.line(x+w-7, y+7, x+w, y+7, GREEN, .4)
    for dy, fraction in [(12, .65), (18, .78), (24, .55)]:
        s.line(x+5, y+dy, x+w*fraction, y+dy, RULE, .6)


def make_scene(Scene):
    s = Scene()
    data = json.loads((Path(__file__).parent/'poster-data.json').read_text())
    s.rect(0, 0, 841, 1189, WHITE)
    s.rect(0, 0, 841, 5, GREEN)
    # Official assets remain unmodified; typography accompanies the NiuTrans mark.
    s.asset('neu-faculty-logo.png', 32, 15, 155, 46)
    s.asset('niutrans-avatar.png', 330, 20, 33, 33)
    s.text('NiuTrans', 373, 37, 32, 'bold', '#204E7B')
    s.text('Research', 374, 49, 20, color=MUTED)
    s.asset('longcat.svg', 639, 20, 170, 41)
    s.text('Northeastern University', 32, 66, 16, 'medium', MUTED, tag='credit')
    s.text('NiuTrans Research', 383, 66, 16, 'medium', MUTED, align='center', tag='credit')
    s.text('YourProvider LongCat RSI', 724, 66, 16, 'medium', MUTED, align='center', tag='credit')
    s.logo(32, 78, 77, 72)
    s.text('ToFu: A White-Box, Token-Efficient', 132, 103, 82, 'bold')
    s.text('Agent Harness for Researchers', 132, 139, 82, 'bold')
    s.text('Junhao Ruan¹²* · Yuan Ge¹* · Bei Li²+ · Yongjing Yin² · Yuchun Fan¹² · Xin Chen²',
           32, 159, 23, 'medium')
    s.text('Jingang Wang² · Chenglong Wang¹³ · Jingbo Zhu¹³ · Tong Xiao¹³#',
           32, 173, 23, 'medium')
    s.text('¹ Northeastern University     ² LongCat RSI, YourProvider     ³ NiuTrans Research',
           32, 188, 21, color=MUTED)
    s.text('* Equal contribution   + Project lead   # Corresponding author',
           32, 199, 15, color=MUTED, tag='credit')
    s.label('EMNLP 2026 · SYSTEM DEMONSTRATION', 809, 199, GREEN, 16, align='right')

    heading(s, 1, 'A research workspace with an inspectable runtime', 32, 212, 777, 37)
    s.text('Read papers, run experiments and write manuscripts in one workspace. '
           'Inspect the context and tool decisions behind each model request.',
           32, 253, 28, width=777, leading=12.4)
    # The application workflow sits above its shared runtime, without fake UI.
    for x, name, detail, fill, color in [
        (32, 'READ', 'PDF / arXiv · grounded Q&A', MINT, GREEN),
        (299, 'EXPERIMENT', 'Code · terminal · external tools', ICE, BLUE),
        (566, 'WRITE', 'LaTeX / Overleaf · build & review', PEACH, ORANGE),
    ]:
        s.rect(x, 282, 243, 34, fill)
        s.text(name, x+12, 295, 20, 'bold', color)
        s.text(detail, x+12, 308, 22, 'medium')
    s.arrow(278, 299, 294, 299, MUTED, .7)
    s.arrow(545, 299, 561, 299, MUTED, .7)
    s.label('SHARED AGENT RUNTIME', 32, 336, GREEN, 18)
    s.text('Compact the model’s view; retain the underlying evidence.', 809, 336,
           22, 'medium', MUTED, align='right')
    # Evidence -> composer -> model -> tools -> evidence, with distinct lifecycles.
    s.rect(32, 348, 193, 111, MINT, GREEN, .6, 3)
    s.text('Evidence & task state', 45, 366, 28, 'bold', GREEN)
    for x in [46, 90, 134]:
        folded_sheet(s, x, 378, 37, 37)
    s.text('Turns · results · artifacts', 45, 431, 23, 'medium')
    s.text('Accepted checklist + archives', 45, 446, 21, color=MUTED)
    s.arrow(228, 404, 261, 404, GREEN, .85, 4)
    s.rect(266, 348, 267, 111, ICE, BLUE, .6, 3)
    s.text('Request context', 280, 366, 28, 'bold', BLUE)
    for y, n, label, detail in [
        (377, 'L0', 'Preview', 'Recoverable tool references'),
        (402, 'L1', 'Trim', 'Cache-aware working view'),
        (427, 'L2', 'Summarize', 'Older history + durable archive'),
    ]:
        s.rect(280, y, 22, 19, BLUE)
        s.text(n, 291, y+13, 22, 'bold', WHITE, align='center')
        s.text(label, 312, y+8, 21, 'bold')
        s.text(detail, 312, y+18, 19, color=MUTED)
    s.arrow(536, 380, 573, 380, BLUE, .85, 4)
    s.label('request', 554, 370, BLUE, 15, align='center')
    s.rect(578, 348, 231, 51, NAVY, radius=3)
    s.text('Model', 693.5, 370, 34, 'bold', WHITE, align='center')
    s.text('Selectable provider & backbone', 693.5, 386, 22, color='#D6E6EC', align='center')
    s.arrow(693.5, 401, 693.5, 416, ORANGE, .9, 4)
    s.rect(578, 421, 231, 38, PEACH, ORANGE, .6, 3)
    s.text('Tools & sandboxed programs', 693.5, 444, 25, 'bold', ORANGE, align='center')
    s.line(693.5, 459, 693.5, 478, ORANGE, .8)
    s.line(693.5, 478, 129, 478, ORANGE, .8)
    s.arrow(129, 478, 129, 461, ORANGE, .8, 4)
    s.rect(303, 469, 239, 17, WHITE)
    s.text('Full results + execution receipts', 422.5, 481, 23, 'medium', ORANGE, align='center')
    s.text('Figure 1. Research applications share one harness; durable records and model-visible context have distinct lifecycles.',
           32, 498, 18, color=MUTED, tag='credit')

    heading(s, 2, 'Context has a cache cost', 32, 513, 377, 32)
    heading(s, 3, 'Compute beside the tools', 432, 513, 377, 32)
    s.text('Similar token counts can have very different costs.',
           32, 554, 26, 'bold', width=377, leading=11.5)
    s.text('Filter and join full tool data in an isolated program; '
           'return the evidence needed for the next decision.',
           432, 554, 25, width=377, leading=11.5)
    # Adjacent observed requests, explicitly not an intervention or ablation.
    cols = [32, 161, 232, 303, 409]
    for label, x in zip(['Request', 'Input', 'Output', 'Cache', 'Cost (CNY)'], cols):
        s.text(label, x, 585, 19, 'medium', MUTED, align='left' if x == 32 else 'right')
    s.line(32, 592, 409, 592, INK, .65)
    for y, row in zip([609, 631], data['cache_trace']['requests']):
        if row['label'] == 'B':
            s.rect(32, y-14, 377, 21, MINT)
        for text, x in zip([row['label'], f"{row['input']:,}", str(row['output']),
                            f"{row['cache_read_percent']}%", f"{row['cost_cny']:.4f}"], cols):
            s.text(text, x, y, 23, 'bold' if x == 409 else 'body',
                   GREEN if row['label'] == 'B' else INK, align='left' if x == 32 else 'right')
    s.line(32, 639, 409, 639, INK, .65)
    s.text('Cache-read share', 32, 657, 19, 'medium', MUTED)
    for y, label, fraction in [(670, 'A', .19), (685, 'B', .97)]:
        s.text(label, 32, y+8, 18, 'medium')
        s.rect(49, y, 221, 10, GRAY)
        s.rect(49, y, 221*fraction, 10, GREEN)
    s.text('5.95×', 409, 677, 43, 'bold', GREEN, align='right')
    s.text('cost ratio [1]', 409, 693, 18, color=MUTED, align='right')
    s.text('Compress when future savings repay the summary '
           'and the cost of rebuilding the cached prefix.',
           32, 715, 25, 'medium', width=377, leading=11.6)
    s.text('Inspect every injection: source, placement, token '
           'budget and reason are recorded in Request Inspector.',
           32, 745, 23, width=377, leading=10.8)
    s.text('[1] Two adjacent requests, same unit prices; an observed trace, not an ablation.',
           32, 773, 15, color=MUTED, width=377, leading=7, tag='credit')
    # Small explicit example, visually distinct from measured tables.
    s.label('ILLUSTRATIVE RESULT FILTERING', 432, 589, ORANGE, 17)
    s.rect(432, 599, 143, 58, WHITE, RULE, .5)
    s.rect(432, 599, 143, 14, GRAY)
    s.text('Full tool result', 441, 609, 19, 'bold')
    for y, code, status, color in [(626, 'A01', 'passed', MUTED),
                                   (639, 'A02', 'failed', ORANGE),
                                   (652, 'A03', 'passed', MUTED)]:
        s.text(code, 441, y, 20, 'mono', color)
        s.text(status, 565, y, 20, 'medium', color, align='right')
    s.arrow(582, 628, 631, 628, ORANGE, .9, 4)
    s.text('filter', 606.5, 618, 18, 'medium', ORANGE, align='center')
    s.rect(640, 599, 169, 58, PEACH, ORANGE, .5)
    s.text('Model receives', 650, 609, 19, 'bold', ORANGE)
    s.text('A02 · failed', 650, 630, 23, 'bold')
    s.text('+ the error evidence', 650, 648, 20, color=MUTED)
    s.rect(432, 669, 377, 61, NAVY, radius=2)
    code = [
        ('const rows = await tools.invoke(name, args);', 0),
        ('return rows.filter(r => r.status === "failed")', 0),
        ('.map(r => ({ id: r.id, error: r.error }));', 10),
    ]
    for i, (line, indent) in enumerate(code):
        s.text(line, 444+indent, 687+i*14, 21.3, 'mono', WHITE, tag='code')
    s.text('Default: ordinary / JSON tools. Code is opt-in.', 432, 748, 23, 'bold')
    s.text('Each subcall retains permission checks. Semantic judgments return to the model.',
           432, 764, 23, width=377, leading=10.8)

    heading(s, 4, 'TofuDB: make long-history reads incremental', 32, 791, 777, 37)
    s.text('Long runs repeatedly reload and revalidate the same history. TofuDB builds context at the storage '
           'boundary, removes redundant JSON conversion and reuses verified prefixes without dropping evidence.',
           32, 833, 26, width=777, leading=12)
    s.text('Recheck only the frontier and new turns', 32, 867, 28, 'bold', GREEN)
    s.text('N retained turns', 32, 884, 19, 'medium', MUTED)
    s.text('New turns', 345, 884, 19, 'medium', ORANGE)
    s.rect(32, 895, 237, 24, MINT, GREEN, .45)
    for x in [71, 110, 149, 188, 227]:
        s.line(x, 895, x, 919, RULE, .4)
    s.rect(51, 899, 198, 16, MINT)
    s.text('Certified prefix', 150.5, 912, 23, 'bold', GREEN, align='center')
    for x, text, fill, color in [(276, 'N', GREEN, WHITE),
                                  (323, '+1', PEACH, ORANGE), (370, '+2', PEACH, ORANGE)]:
        s.rect(x, 895, 39, 24, fill, GREEN if text == 'N' else ORANGE, .45)
        s.text(text, x+19.5, 912, 25, 'bold', color, align='center')
    s.line(276, 924, 276, 930, GREEN, .65)
    s.line(276, 930, 409, 930, GREEN, .65)
    s.line(409, 924, 409, 930, GREEN, .65)
    s.text('3 header checks', 342.5, 944, 26, 'bold', GREEN, align='center')
    s.text('Verified at N = 8, 128, 2,048.', 32, 941, 20, 'medium', MUTED)
    s.text('Same epoch; edits or reopen require re-verification.', 32, 956, 18, color=MUTED)
    s.line(430, 862, 430, 958, RULE, .6)
    s.text('Commit before switching the model view', 449, 867, 27, 'bold', GREEN)
    for x, w, title, subtitle in [(449, 104, 'Summary', 'new context'),
                                  (568, 104, 'Archive', 'source evidence'),
                                  (687, 122, 'Receipt', 'adoption record')]:
        s.rect(x, 885, w, 39, MINT, RULE, .45)
        s.text(title, x+w/2, 901, 23, 'bold', GREEN, align='center')
        s.text(subtitle, x+w/2, 916, 18, color=MUTED, align='center')
    s.text('+', 560.5, 911, 24, 'bold', MUTED, align='center')
    s.text('+', 679.5, 911, 24, 'bold', MUTED, align='center')
    s.text('One durable commit', 629, 942, 25, 'bold', align='center')
    s.text('If adoption fails, keep the original context.', 629, 956, 20, color=MUTED, align='center')
    s.text('Context-read latency', 32, 977, 28, 'bold')
    s.text('P50 milliseconds · lower is better · 400 samples per cell [2]',
           809, 977, 20, color=MUTED, align='right')
    # The later four-arm control includes the counterexample in the same table.
    xcols = [32, 366, 482, 589, 697, 809]
    s.rect(421, 985, 77, 107, MINT)
    for i, (x, label) in enumerate(zip(xcols, ['Evidence / history', 'Read', 'TofuDB',
                                              'SQL Sidecar', 'SQL JSON', 'SQL object'])):
        s.text(label, x, 996, 19, 'bold', GREEN if i == 2 else MUTED,
               align='left' if i == 0 else 'right')
    s.line(32, 1003, 809, 1003, INK, .65)
    for i, row in enumerate(data['context_read_control']['rows']):
        y = 1017+i*14
        if i and not i % 2:
            s.line(32, y-10, 809, y-10, RULE, .4)
        s.text(row['label'], 32, y, 21, 'medium' if i % 2 == 0 else 'body')
        s.text(row['mode'], 366, y, 20, color=MUTED, align='right')
        values = [row[k] for k in ['native', 'sidecar', 'sql_json', 'sql_object']]
        for x, value in zip(xcols[2:], values):
            s.text(f'{value:.3f}', x, y, 22, 'bold' if value == min(values) else 'body',
                   GREEN if value == min(values) else INK, align='right')
    s.line(32, 1094, 809, 1094, INK, .65)
    s.text('Trade-off: SQL objects win on escaped full reads. Native continuations win in these fixtures; '
           'the engine and language effects are not isolated.',
           32, 1108, 20, 'medium', width=777, leading=9.2)
    s.text('[2] Warm reads on XFS, 2026-10-19. SQL Sidecar: RPC; SQL JSON / object: in-process returns. '
           'Same semantic contract, Rust/Python differs. Full four-arm matrix: 12,800 exactly validated responses.',
           32, 1123, 15, color=MUTED, width=697, leading=7, tag='credit')
    s.line(32, 1140, 725, 1140, RULE, .65)
    s.text('Code, documentation & live demonstration', 32, 1160, 35, 'bold', GREEN)
    s.text('github.com/NiuTrans/ToFu', 32, 1180, 28, 'bold')
    s.text('MIT licensed · Self-hosted · Current system: Oct 2026', 379, 1180, 19, color=MUTED)
    s.qr(*QR)
    return s
