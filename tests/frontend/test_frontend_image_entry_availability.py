"""The AI-drawing entry follows the routing authority, never a boot placeholder.

Boot applies server config before owner routing; the config phase populates
the model list with ``[]`` while no routing has arrived yet. That placeholder
must not count as an authoritative absence of image Models, and a later
routing authority that supplies image Models must show the entry again.
Before this was specified, the placeholder marked the list loaded and hid
``#imageGenModeBtn``, and the non-empty routing population never re-applied
availability, so the entry stayed hidden with image Models registered.

Drives the shipped ``_populateModelDropdown``, ``_hasImageGenModel`` and
``_applyImageGenAvailability`` under jsdom.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests._jsdom import require_node_modules
from tests._runtime_sections import native_module_path, runtime_section

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent.parent


def _slice_fn(src: str, signature: str) -> str:
    start = src.index(signature)
    i = src.index('{', start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f'could not slice {signature}')


def _shipped_functions() -> str:
    source = runtime_section('main/main_toolbar_ui.js')
    layout = Path(native_module_path(
        '.native/picker-layout.js',
        ROOT / 'frontend/src/application-shell/model-picker/index.ts')).read_text()
    return '\n'.join([
        layout,
        'var _modelPickerUi = null;',
        _slice_fn(source, 'function _getModelPickerUi('),
        _slice_fn(source, 'function _populateModelDropdown('),
        _slice_fn(source, 'function _hasImageGenModel('),
        _slice_fn(source, 'function _applyImageGenAvailability('),
    ])


HARNESS = textwrap.dedent("""
    const { JSDOM } = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><body>
      <div class="preset-dropdown" id="presetDropdown">
        <div class="preset-dropdown-list" id="presetDropdownList">
          <div class="ps-dd-loading">loading</div>
        </div>
      </div>
      <div class="submenu-item" id="imageGenModeBtn">Creative</div>
    </body>`);
    global.document = dom.window.document;
    global.window = dom.window;
    global.config = { model: '' };
    global.serverModel = '';
    global._hiddenModels = new Set();
    global._detectBrand = () => 'generic';
    global._brandSvg = () => '';
    global._modelShortName = (id) => id;
    global.selectModel = () => {};
    global.t = (key) => key;
    global.runtimeScope = {
      isChatModel: () => true,
      modelGroupKey: (provider) => provider.brand || 'generic',
      modelGroupLabel: (key, fallback) => fallback || key,
    };
    global._warnModelCapsMissing = () => {};
    var _registeredModels = [];
    var _registeredModelsLoaded = false;
    var imageGenEnabled = false;

    __SHIPPED__

    const entry = () => document.getElementById('imageGenModeBtn').style.display;
    const image = { model_id: 'gemini-2.5-flash-image', provider_id: 'p', provider_name: 'P',
                    capabilities: ['image_gen'] };
    const chat = { model_id: 'chat-model', provider_id: 'p', provider_name: 'P',
                   capabilities: ['text'] };
    const out = {};
    // Boot: config applies before any routing, then routing supplies image Models.
    _populateModelDropdown([], false);
    out.afterConfigPlaceholder = entry();
    _populateModelDropdown([chat, image]);
    out.afterRoutingWithImage = entry();
    // A successfully loaded empty authority hides the entry...
    _populateModelDropdown([]);
    out.afterEmptyAuthority = entry();
    // ...a later authority with image Models shows it again...
    _populateModelDropdown([image]);
    out.afterLaterImageAuthority = entry();
    // ...and an authority without image Models hides it.
    _populateModelDropdown([chat]);
    out.afterRoutingWithoutImage = entry();
    console.log(JSON.stringify(out));
""")


def _run(shipped: str) -> dict:
    require_node_modules('jsdom', 'vite')
    script = HARNESS.replace('__SHIPPED__', shipped)
    proc = subprocess.run([shutil.which('node'), '-e', script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f'node failed: {proc.stderr}'
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_image_entry_follows_routing_authority_not_the_boot_placeholder():
    assert _run(_shipped_functions()) == {
        'afterConfigPlaceholder': '',
        'afterRoutingWithImage': '',
        'afterEmptyAuthority': 'none',
        'afterLaterImageAuthority': '',
        'afterRoutingWithoutImage': 'none',
    }


def test_neuter_placeholder_counted_as_authority_hides_the_entry_at_boot():
    shipped = _shipped_functions()
    neutered = shipped.replace('if (authoritative) _registeredModelsLoaded = true;',
                               '_registeredModelsLoaded = true;')
    assert neutered != shipped, 'neuter substitution did not apply'
    assert _run(neutered)['afterConfigPlaceholder'] == 'none'


def test_neuter_availability_only_on_empty_lists_never_reshows_the_entry():
    shipped = _shipped_functions()
    neutered = shipped.replace(
        "  if (typeof _applyImageGenAvailability === 'function') _applyImageGenAvailability();",
        "  if (!models.length && typeof _applyImageGenAvailability === 'function') "
        "_applyImageGenAvailability();")
    assert neutered != shipped, 'neuter substitution did not apply'
    assert _run(neutered)['afterLaterImageAuthority'] == 'none'
