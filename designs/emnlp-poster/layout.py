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
PROOF_SECTIONS = [('header', 0, 205), ('async-tools', 205, 535),
                  ('retained-agents', 535, 785), ('tofudb', 785, 1136), ('footer', 1136, 1189)]


def heading(s, n, title, x, y, w, size=35):
    s.rect(x, y, w, 25, GREEN)
    s.rect(x, y, 23, 25, INK)
    s.text(str(n), x+11.5, y+17.7, 32, 'bold', WHITE, align='center')
    s.text(title, x+31, y+17.7, size, 'bold', WHITE)


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
    s.text('Meituan LongCat RSI', 724, 66, 16, 'medium', MUTED, align='center', tag='credit')
    s.logo(32, 78, 77, 72)
    s.text('ToFu: A White-Box, Token-Efficient', 132, 103, 82, 'bold')
    s.text('Agent Harness for Researchers', 132, 139, 82, 'bold')
    s.text('Junhao Ruan¹²* · Yuan Ge¹* · Bei Li²+ · Yongjing Yin² · Yuchun Fan¹² · Xin Chen²',
           32, 159, 23, 'medium')
    s.text('Jingang Wang² · Chenglong Wang¹³ · Jingbo Zhu¹³ · Tong Xiao¹³#',
           32, 173, 23, 'medium')
    s.text('¹ Northeastern University     ² LongCat RSI, Meituan     ³ NiuTrans Research',
           32, 188, 21, color=MUTED)
    s.text('* Equal contribution   + Project lead   # Corresponding author',
           32, 199, 15, color=MUTED, tag='credit')
    s.label('EMNLP 2026 · SYSTEM DEMONSTRATION', 809, 199, GREEN, 16, align='right')

    heading(s, 1, 'Coordinating synchronous and asynchronous tools', 32, 212, 777, 37)
    s.text('Normal tools return one result. Shell commands and sub-agents can run independently, '
           'with handles for supervision and automatic completion delivery.',
           32, 253, 28, width=777, leading=12.4)
    s.label('ILLUSTRATIVE RESEARCH WORKFLOW', 809, 287, MUTED, 16, align='right')
    # Swimlanes separate model work, the running command, and the child.
    # Horizontal positions show sequence, not measured latency or a shared clock.
    s.text('Main agent', 32, 333, 28, 'bold', GREEN)
    s.text('Command', 32, 405, 26, 'bold', ORANGE)
    s.text('Sub-agent', 32, 463, 26, 'bold', BLUE)
    for x, w, title, detail, fill, color in [
        (171, 83, 'Read files', 'direct result', MINT, GREEN),
        (287, 97, 'Start work', 'pending handles', MINT, GREEN),
        (417, 154, 'Other useful work', 'while workers run', MINT, GREEN),
        (678, 131, 'Integrate', 'returned outcomes', NAVY, WHITE),
    ]:
        s.rect(x, 307, w, 40, fill, radius=2)
        s.text(title, x+w/2, 325, 25, 'bold', color, align='center')
        s.text(detail, x+w/2, 340, 19, color=color, align='center')
    s.arrow(256, 327, 283, 327, GREEN, .8, 4)
    s.arrow(386, 327, 413, 327, GREEN, .8, 4)
    s.arrow(574, 327, 673, 327, GREEN, .8, 4)
    s.label('next model round', 622, 316, MUTED, 15, align='center')
    # Both async launches remain tied to their original invocation.
    s.line(302, 347, 302, 458, MUTED, .7)
    s.arrow(302, 400, 312, 400, ORANGE, .8, 3)
    s.arrow(302, 458, 312, 458, BLUE, .8, 3)
    s.rect(314, 385, 277, 30, PEACH, ORANGE, .45, 2)
    s.text('Evaluate experimental variants', 326, 405, 23, 'medium', ORANGE)
    s.rect(553, 385, 38, 30, ORANGE)
    s.text('done', 572, 405, 18, 'bold', WHITE, align='center')
    s.rect(314, 443, 305, 30, ICE, BLUE, .45, 2)
    s.text('Read the paper / check the method', 326, 463, 23, 'medium', BLUE)
    s.rect(581, 443, 38, 30, BLUE)
    s.text('done', 600, 463, 18, 'bold', WHITE, align='center')
    # Complete evidence enters the durable inbox; it is distinct from acceptance.
    s.arrow(593, 400, 655, 400, ORANGE, .8, 4)
    s.line(621, 458, 734, 458, BLUE, .8)
    s.arrow(734, 458, 734, 434, BLUE, .8, 4)
    s.rect(659, 382, 150, 50, MINT, GREEN, .55, 2)
    s.text('Completion inbox', 734, 402, 24, 'bold', GREEN, align='center')
    s.text('result + receipt', 734, 419, 21, color=MUTED, align='center')
    s.arrow(734, 380, 734, 350, GREEN, .8, 4)
    s.label('delivered', 744, 369, GREEN, 16)
    s.text('poll(id, wait_s)', 32, 493, 24, 'mono', GREEN)
    s.text('Inspect / wait on commands, agents or tasks', 130, 493, 21)
    s.text('kill(id)', 493, 493, 24, 'mono', GREEN)
    s.text('Stop one command or running child', 554, 493, 21)
    s.text('A finished model turn can still be waiting on required child results.',
           32, 509, 23, 'medium')
    s.text('Eligible local shell calls hand off after 60 s without killing the process. '
           'Restart reattachment requires an explicit background launch.',
           32, 524, 16, color=MUTED, width=777, leading=7.5, tag='credit')

    heading(s, 2, 'Persistent sub-agents keep their working context', 32, 540, 777, 37)
    s.text('Create a persistent identity or promote a settled child. Later assignments reuse '
           'its saved dialogue with the caller’s current model, tools and budget.',
           32, 581, 27, width=777, leading=12)
    s.label('EXAMPLE: A REUSABLE PAPER SPECIALIST', 32, 609, MUTED, 16)
    for x, w, title, line1, line2, fill, color in [
        (32, 219, 'Assignment 1', 'Explain the method', 'Ordinary child execution', ICE, BLUE),
        (311, 219, 'Retained identity', 'Working dialogue + result', 'Idle: no resident worker', MINT, GREEN),
        (590, 219, 'Assignment 2', 'Design the ablation', 'Restored context · fresh authority', ICE, BLUE),
    ]:
        s.rect(x, 619, w, 78, fill, color, .5, 2)
        s.text(title, x+13, 639, 28, 'bold', color)
        s.text(line1, x+13, 664, 25, 'medium')
        s.text(line2, x+13, 686, 21, color=MUTED)
    s.arrow(254, 659, 306, 659, GREEN, .8, 4)
    s.label('commit', 280, 649, GREEN, 18, align='center')
    s.arrow(533, 659, 585, 659, GREEN, .8, 4)
    s.label('assign', 559, 649, GREEN, 18, align='center')
    # Stable identity has an explicit serial owner; different identities parallelize.
    s.text('Message an existing colleague', 32, 721, 27, 'bold', GREEN)
    s.text('Idle', 32, 740, 23, 'bold')
    s.arrow(64, 737, 84, 737, GREEN, .7, 3)
    s.text('Start a child with its saved context.', 94, 740, 22)
    s.text('Busy', 32, 758, 23, 'bold')
    s.arrow(64, 755, 84, 755, GREEN, .7, 3)
    s.text('Steer the current assignment.', 94, 758, 22)
    s.line(425, 710, 425, 763, RULE, .5)
    s.text('One active assignment per identity', 447, 721, 26, 'bold', GREEN)
    s.text('Different identities run in parallel. The child commits '
           'its working context before returning to idle.',
           447, 740, 23, width=362, leading=11)
    s.text('Scoped to a project or conversation. A lost launch reply does not free the identity; '
           'reset/abandon prevents stale memory writes. Retention is an explicit choice.',
           32, 778, 16, color=MUTED, width=777, leading=7.5, tag='credit')

    heading(s, 3, 'TofuDB: durable histories, incremental context reads', 32, 791, 777, 37)
    s.text('Tool outcomes and saved agent context create growing histories. TofuDB removes redundant JSON '
           'conversion and reuses verified prefixes, so continuation need not rescan unchanged history.',
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
    s.text('P50 milliseconds · lower is better · 400 samples per cell [1]',
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
    s.text('[1] Warm reads on XFS, 2026-10-19. SQL Sidecar: RPC; SQL JSON / object: in-process returns. '
           'Same semantic contract, Rust/Python differs. Full four-arm matrix: 12,800 exactly validated responses.',
           32, 1123, 15, color=MUTED, width=697, leading=7, tag='credit')
    s.line(32, 1140, 725, 1140, RULE, .65)
    s.text('Code, documentation & live demonstration', 32, 1160, 35, 'bold', GREEN)
    s.text('github.com/NiuTrans/ToFu', 32, 1180, 28, 'bold')
    s.text('Paper Q&A · experiments · LaTeX / Overleaf · MIT', 379, 1180, 19, color=MUTED)
    s.qr(*QR)
    return s
