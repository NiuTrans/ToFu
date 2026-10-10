"""Compile real Project receipt owners; fault cases mutate only a private graph copy."""
from pathlib import Path

from tests._runtime_sections import ROOT, native_module_path

OWNER = ROOT / 'frontend/src/conversation/ui/project-receipt'


def project_receipt_bundle(directory, *, anchor=None, replacement=None):
    directory = Path(directory)
    copied = directory / 'project-receipt'
    copied.mkdir()
    originals = {path.name: path.read_text() for path in OWNER.glob('*.ts')}
    sources = dict(originals)
    if anchor is not None:
        matches = [name for name, source in sources.items() if anchor in source]
        assert len(matches) == 1, f'Expected one owned fault anchor, found {matches}: {anchor!r}'
        name = matches[0]
        assert sources[name].count(anchor) == 1
        sources[name] = sources[name].replace(anchor, replacement, 1)
    for name, source in sources.items():
        # Preserve the shared escaping collaborator when relocating the graph.
        source = source.replace('"../../../html-safety"', repr(str(ROOT / 'frontend/src/html-safety')))
        (copied / name).write_text(source)
    path = native_module_path('.native/project-receipt-fault.js', copied / 'index.ts')
    for source in OWNER.glob('*.ts'):
        # Mutation is confined to the private copy; the shipped owner never changes.
        assert source.read_text() == originals[source.name]
    return Path(path).read_text()


def project_receipt_rich_fixture(directory, *, anchor, replacement):
    directory = Path(directory)
    bundle = project_receipt_bundle(directory, anchor=anchor, replacement=replacement)
    raw = (ROOT / 'frontend/src/runtime/sections/ui/tool_rounds_rich.js').read_text()
    path = directory / 'rich-fault.js'
    path.write_text(bundle + '\n' + raw)
    return str(path)
