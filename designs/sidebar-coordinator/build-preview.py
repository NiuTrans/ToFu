#!/usr/bin/env python3
"""Derive an offline, single-file design artifact from retained prototype sources.

No production runtime is embedded. The UI kit is generated from real factories.
Run verify.py first to regenerate bundle/font and then run this script.
"""
import base64
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main():
    html = (HERE / 'index.html').read_text()
    fonts = ROOT / 'frontend/src/styles/fonts.css'

    def inline_font(match):
        target = fonts.parent / match.group(1)
        if not target.is_file():
            raise FileNotFoundError(target)
        mime = 'font/woff2' if target.suffix == '.woff2' else 'font/ttf'
        encoded = base64.b64encode(target.read_bytes()).decode()
        return f'url("data:{mime};base64,{encoded}")'

    font_css = re.sub(r'url\(([^)]+)\)', inline_font, fonts.read_text())
    css = (HERE / 'prototype.css').read_text()
    cjk = base64.b64encode((HERE / 'preview-cjk.woff2').read_bytes()).decode()
    css = css.replace("url('./preview-cjk.woff2')", f'url("data:font/woff2;base64,{cjk}")')
    for href, source in (
        ('../../frontend/src/styles/fonts.css', font_css),
        ('../../static/styles.css', (ROOT / 'static/styles.css').read_text()),
        ('./prototype.css', css),
    ):
        html = html.replace(f'<link rel="stylesheet" href="{href}">', f'<style>\n{source}\n</style>')
    bundle = (HERE / 'ui-kit.bundle.js').read_text()
    exports = re.search(r'export \{([^}]+)\};', bundle)
    if not exports:
        raise ValueError('UI bundle has an unexpected export shape')
    bundle = bundle[:exports.start()] + 'return {' + exports.group(1) + '};' + bundle[exports.end():]
    script = (HERE / 'prototype.js').read_text()
    script = script.replace("import * as ui from './ui-kit.bundle.js';", 'const ui = (() => {\n' + bundle + '\n})();')
    if re.search(r'^\s*(?:import|export)\s', script, re.MULTILINE):
        raise ValueError('Standalone artifact still has external modules')
    html = html.replace('<script type="module" src="./prototype.js"></script>', '<script type="module">\n' + script.replace('</script', '<\\/script') + '\n</script>')
    license_text = (HERE / 'OFL.txt').read_text().replace('--', '—')
    html = html.replace('</head>', '<!-- Preview font license:\n' + license_text + '\n-->\n</head>')
    if len(html.encode()) > 32 * 1024 * 1024:
        raise ValueError('Standalone preview exceeds bounded preview size')
    html = '\n'.join(line.rstrip() for line in html.splitlines()) + '\n'
    (HERE / 'preview.html').write_text(html)
    print(f'Offline preview -> {HERE / "preview.html"} ({len(html.encode())} bytes)')


if __name__ == '__main__':
    main()
