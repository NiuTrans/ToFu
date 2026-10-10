"""Real typed Timer receipt graph, with fault mutations confined to private copies."""
from pathlib import Path
from tests._runtime_sections import ROOT, native_module_path


def timer_rich_fixture(directory, mode):
    directory = Path(directory)
    owner = ROOT / 'frontend/src/conversation/ui/timer-card'
    copied = directory / 'timer-card'
    copied.mkdir()
    originals = {path.name: path.read_text() for path in owner.glob('*.ts')}
    sources = dict(originals)
    mutation = {
        'neuter_reason': ('poll.ts', 'const fullReason = timerPollReason(p, _t);', 'const fullReason = String(p.reason || "");'),
        'neuter_kind': ('index.ts', 'const isCodeTimer = condKind === "code";', 'const isCodeTimer = false;'),
    }.get(mode)
    if mutation:
        name, anchor, replacement = mutation
        assert sources[name].count(anchor) == 1
        sources[name] = sources[name].replace(anchor, replacement, 1)
    for name, source in sources.items():
        source = source.replace('"../../../html-safety"', repr(str(ROOT / 'frontend/src/html-safety')))
        (copied / name).write_text(source)
    entry = directory / 'entry.ts'
    entry.write_text("export * from './timer-card/index';\nexport * from './timer-card/interactions';\n")
    bundle = Path(native_module_path('.native/timer-fault.js', entry)).read_text()
    rich = directory / 'rich.js'
    rich.write_text(bundle + '\n' + (ROOT / 'frontend/src/runtime/sections/ui/tool_rounds_rich.js').read_text())
    for name, original in originals.items():
        assert (owner / name).read_text() == original
    return str(rich)
