"""Rich-response figures must reach the shared image lightbox.

A rich figure's bitmap exists only as an object URL created inside the
renderer, so the Markdown lightbox never saw it and body images in rich
responses could not be enlarged. The renderer now exposes the same opener
through an explicit port; verify the click, the keyboard path and the
action-row control, plus the unchanged behavior when no port is supplied.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._jsdom import require_node_modules

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
BUNDLER = ROOT / "scripts" / "vite_test_bundle.mjs"
ENTRY = (
    "export { renderRichResponse, disposeRichResponse } from "
    + json.dumps(str(ROOT / "frontend/src/rich-response/renderer.ts"))
    + ";"
)


@pytest.fixture(scope="module")
def renderer_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    require_node_modules('jsdom', 'vite')
    built = tmp_path_factory.mktemp("rich-image") / "renderer.cjs"
    entry = built.parent / "entry.ts"
    entry.write_text(ENTRY, encoding="utf-8")
    result = subprocess.run(
        [str(BUNDLER), str(entry), "--bundle", "--format=cjs",
         "--platform=node", f"--outfile={built}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return built


def _run(bundle: Path, source: str) -> dict:
    script = f"""
const harnessDom = new (require('jsdom').JSDOM)('', {{url:'http://localhost/'}});
global.window = harnessDom.window;
global.document = harnessDom.window.document;
global.Blob = harnessDom.window.Blob;
const feature = require({json.dumps(str(bundle))});
{source}
"""
    result = subprocess.run(
        [shutil.which("node"), "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


GALLERY = """{version:1,title:'Figures',blocks:[
  {type:'image_gallery',id:'shots',title:'Shots',images:[{id:'img-1',alt:'Paper page'}]}]}"""


def test_rich_figure_opens_the_shared_lightbox_by_pointer_and_keyboard(renderer_bundle: Path):
    result = _run(renderer_bundle, r"""
(async () => {
  const previews = [];
  let revokes = 0;
  const realCreate = URL.createObjectURL;
  URL.createObjectURL = () => 'blob:rich-fixture';
  URL.revokeObjectURL = () => { revokes += 1; };
  const host = document.createElement('div');
  document.body.append(host);
  const payload = """ + GALLERY + r""";
  feature.renderRichResponse(host, payload, {
    text: (key, fallback) => fallback,
    asset: async () => new window.Blob([new Uint8Array([1, 2, 3])], {type: 'image/png'}),
    previewImage: (source) => previews.push(source),
  });
  await new Promise((resolve) => setTimeout(resolve, 0));
  const image = host.querySelector('figure img');
  const beforeLoad = {
    role: image.getAttribute('role'),
    tabIndex: image.tabIndex,
    classes: [...image.classList],
    hidden: image.hidden,
  };
  image.dispatchEvent(new window.Event('load'));
  const afterLoad = { hidden: image.hidden };
  image.dispatchEvent(new window.MouseEvent('click', {bubbles: true, cancelable: true}));
  image.dispatchEvent(new window.KeyboardEvent('keydown', {
    key: 'Enter', bubbles: true, cancelable: true}));
  const enlarge = host.querySelector('[data-rich-control^="enlarge:"]');
  const enlargeLabel = enlarge ? enlarge.textContent : null;
  if (enlarge) enlarge.click();
  const figureActions = [...host.querySelectorAll('.rich-actions button')]
    .map((button) => button.getAttribute('data-rich-control'));
  feature.disposeRichResponse(host);
  URL.createObjectURL = realCreate;
  console.log(JSON.stringify({
    beforeLoad, afterLoad, previews, enlargeLabel, figureActions, revokes,
  }));
})();
""")

    assert result["beforeLoad"]["role"] == "button"
    assert result["beforeLoad"]["tabIndex"] == 0
    assert "rich-image-preview" in result["beforeLoad"]["classes"]
    assert result["beforeLoad"]["hidden"] is True, "an undecoded bitmap is not previewable"
    assert result["afterLoad"]["hidden"] is False
    assert result["previews"] == ["blob:rich-fixture"] * 3
    assert result["enlargeLabel"] == "Enlarge image"
    assert "enlarge:img-1" in result["figureActions"]
    assert result["revokes"] >= 1


def test_rich_figure_stays_inert_without_a_preview_port(renderer_bundle: Path):
    result = _run(renderer_bundle, r"""
(async () => {
  URL.createObjectURL = () => 'blob:rich-fixture';
  URL.revokeObjectURL = () => {};
  const host = document.createElement('div');
  document.body.append(host);
  feature.renderRichResponse(host, """ + GALLERY + r""", {
    text: (key, fallback) => fallback,
    asset: async () => new window.Blob([new Uint8Array([1, 2, 3])], {type: 'image/png'}),
  });
  await new Promise((resolve) => setTimeout(resolve, 0));
  const image = host.querySelector('figure img');
  image.dispatchEvent(new window.Event('load'));
  const controls = [...host.querySelectorAll('.rich-actions button')]
    .map((button) => button.getAttribute('data-rich-control'));
  console.log(JSON.stringify({
    role: image.getAttribute('role'),
    tabIndex: image.tabIndex,
    interactive: image.classList.contains('rich-image-preview'),
    controls,
  }));
})();
""")

    assert result == {
        "role": None,
        "tabIndex": -1,
        "interactive": False,
        "controls": ["download_asset:img-1", "fullscreen:img-1"],
    }
