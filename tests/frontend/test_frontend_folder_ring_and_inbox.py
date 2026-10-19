"""Real Chromium rail geometry, quiet inbox and durable streaming selection.

Local synthetic rail serves compiled interaction owner and composed production
CSS; no native storage or full-app boot. Full integration lives in e2e tests.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading

import pytest

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def rail_server(tmp_path_factory):
    root = tmp_path_factory.mktemp('rail-css-browser')
    owner = ROOT / 'frontend/src/conversation/ui/folder-config-control.ts'
    entry = root / 'entry.ts'
    entry.write_text(f'''import {{mountFolderConfigControls,installFolderConfigInteractions,makeFolderContextMenuAccessible}} from {json.dumps(str(owner))};
const rail=document.querySelector('#folderTabs');
mountFolderConfigControls(rail,'Settings');
const open=id=>{{document.querySelector('dialog input').value=id;document.querySelector('dialog').showModal();}};
installFolderConfigInteractions(rail,{{settings:open,context:(id)=>{{
const menu=document.createElement('div');menu.innerHTML='<div class="folder-tab-menu-item">Settings</div>';
document.body.append(menu);makeFolderContextMenuAccessible(menu);
menu.addEventListener('click',()=>{{menu.remove();open(id);}});
}}}});
window.ownerReady=true;''')
    result = subprocess.run(['node', '-e', 'require("esbuild").buildSync({entryPoints:[process.argv[1]],bundle:true,format:"esm",outfile:process.argv[2]})', str(entry), str(root / 'owner.js')], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    source = ROOT / 'frontend/src/styles/application'
    manifest = json.loads((source / 'manifest.json').read_text())
    (root / 'styles.css').write_text('\n'.join((source / row['path']).read_text() for row in manifest['sections']))
    tiles = [('folder-tab-uncat', '', 'folder-tab-inbox-dot', 'Inbox'), ('active', 'a', 'streaming', 'Agent tools'), ('', 'b', '', 'Long folder')] + [('', f'f{i}', '', name) for i, name in enumerate(['Renderer', 'Workspace', 'Projects', 'Research', 'Scripts', 'Browser tests', 'Storage', 'Mobile rail', 'Documentation', 'QA checks'])]
    rows = []
    for kind, folder, dot, name in tiles:
        if not folder:
            rows.append('<button class="folder-tab folder-tab-uncat" data-folder-id="" title="Inbox"><span class="folder-tab-dot folder-tab-inbox-dot"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg></span><span class="folder-tab-name">Inbox</span></button>')
            continue
        mono = ''.join(word[0] for word in name.split())[:2].upper() if ' ' in name else name[:2].upper()
        color = f'hsl({(len(rows) * 137) % 360},60%,48%)'
        rows.append(f'<div class="folder-tab-row"><button class="folder-tab {kind}" data-folder-id="{folder}" title="{name}"><span class="folder-tab-dot {dot}" style="background:{color}" data-initial="{mono}" data-mono-len="{len(mono)}"></span><span class="folder-tab-copy"><span class="folder-tab-name">{name}</span><span class="folder-tab-summary">No default directories</span></span></button></div>')
    (root / 'index.html').write_text('''<!doctype html><html data-theme="dark"><head><link rel="stylesheet" href="styles.css"></head><body>
<aside id="sidebar" class="sidebar has-rail rail-collapsed"><div class="sidebar-body"><nav id="folderTabs" class="project-rail"><div class="project-rail-list">'''+''.join(rows)+'''</div>
<button class="folder-tab folder-tab-add"><span class="folder-tab-dot folder-tab-add-dot"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></span><span class="folder-tab-name">New folder</span></button></nav></div></aside>
<dialog><input></dialog><script type="module" src="owner.js"></script></body></html>''')
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture()
def rail_browser_page(browser, rail_server):
    context = browser.new_context(viewport={'width': 1280, 'height': 900})
    page = context.new_page()
    page.goto(rail_server)
    page.wait_for_function('window.ownerReady === true')
    try:
        yield page
    finally:
        context.close()


@pytest.mark.parametrize('theme', ['dark', 'light', 'tofu'])
def test_compact_uniform_tiles_quiet_inbox_and_streaming_ring(rail_browser_page, theme):
    page = rail_browser_page
    page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
    out = ROOT / 'output/playwright/folder-rail-owner'
    out.mkdir(parents=True, exist_ok=True)
    page.locator('#folderTabs').screenshot(path=str(out / f'{theme}-compact.png'))
    measurements = page.locator('.folder-tab').evaluate_all('''tabs=>tabs.map(tab=>{
const tile=tab.querySelector('.folder-tab-dot');const r=tile.getBoundingClientRect();const row=tab.getBoundingClientRect();const s=getComputedStyle(tile);
return {x:r.x,width:r.width,height:r.height,rowWidth:row.width,rowHeight:row.height,outlineWidth:parseFloat(s.outlineWidth),outlineStyle:s.outlineStyle};})''')
    assert len(measurements) == 14
    for key in ('x', 'width', 'height', 'rowWidth', 'rowHeight'):
        values = [row[key] for row in measurements]
        assert max(values) > 0 and max(values) - min(values) <= 1, (key, measurements)
    assert measurements[1]['outlineWidth'] > 0 and measurements[1]['outlineStyle'] != 'none', measurements
    assert page.locator('.folder-tab-inbox-dot svg').evaluate('(el)=>getComputedStyle(el).animationName') == 'none'
    assert page.locator('.folder-tab-add svg').count() == 1
    controls = page.locator('.folder-tab-settings').evaluate_all('''buttons=>buttons.map(button=>{
const s=getComputedStyle(button);return {display:s.display,opacity:Number(s.opacity),position:s.position};})''')
    assert all(c['display'] == 'none' or (c['opacity'] == 0 and c['position'] == 'absolute') for c in controls), controls
    out = ROOT / 'output/playwright/folder-rail-owner'
    out.mkdir(parents=True, exist_ok=True)
    page.locator('#folderTabs').screenshot(path=str(out / f'{theme}-compact.png'))


@pytest.mark.parametrize('access', ['dblclick', 'Shift+F10', 'ContextMenu'])
def test_settings_owner_pointer_keyboard_and_focus(rail_browser_page, access):
    page = rail_browser_page
    tab = page.locator('.folder-tab[data-folder-id="b"]')
    tab.locator('.folder-tab-dot').click()
    assert page.locator('dialog:modal').count() == 0
    if access == 'dblclick':
        tab.locator('.folder-tab-dot').dblclick(timeout=5000)
    else:
        tab.focus()
        tab.press(access)
        item = page.get_by_role('menuitem')
        assert item.evaluate('(el)=>el===document.activeElement')
        item.press('Escape')
        assert page.get_by_role('menu').count() == 0
        assert tab.evaluate('(el)=>el===document.activeElement')
        tab.press(access)
        page.get_by_role('menuitem').press('Enter')
    assert page.locator('dialog:modal input').input_value() == 'b'
