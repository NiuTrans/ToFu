"""Package the isolated research design as portable HTML and a review gallery.

No application files, external URLs, or dependencies are needed by the result.
"""
from pathlib import Path
from html import escape
import json

HERE = Path(__file__).resolve().parent


def package():
    html = (HERE / 'index.html').read_text()
    css = (HERE / 'prototype.css').read_text()
    js = (HERE / 'prototype.js').read_text()
    html = html.replace('<link rel="stylesheet" href="prototype.css">', '<style>\n' + css + '\n</style>')
    html = html.replace('<script src="prototype.js" defer></script>', '')
    html = html.replace('</body>', '<script>\n' + js + '\n</script>\n</body>')
    destination = HERE / 'research-workbench.html'
    destination.write_text(html)
    labels = {
        '01-brief-desktop.png': '研究简报 / Desktop brief',
        '02-evidence-inspector.png': '证据检查器 / Evidence inspector',
        '03-evidence-table.png': '可排序文献列表 / Sortable literature table',
        '04-approval.png': '限定范围的批准 / Exact approval',
        '05-state-initial.png': '开始研究 / Initial state',
        '05-state-interrupted.png': '恢复研究 / Interrupted',
        '05-state-stale.png': '依赖变化 / Stale evidence',
        '06-brief-mobile.png': '移动端简报 / Mobile',
        '07-source-mobile.png': '移动端原文 / Source on mobile',
        '08-home-desktop.png': '研究首页 / Home',
        '09-hypotheses.png': '候选解释 / Hypotheses',
        '10-outputs.png': '研究产物 / Outputs',

        '11-bibliography-provenance.png': '会议与引用依据 / Bibliographic provenance',
        '12-literature-mobile.png': '移动端文献列表 / Literature on mobile',
        '13-evidence-comparison.png': '证据比较视图 / Shared evidence comparison',
    }
    shots = [name for name in labels if (HERE / 'previews' / name).is_file()]
    rows = ''.join('<article><h2>'+escape(labels[name])+'</h2><a href="previews/'+name+'"><img loading="lazy" src="previews/'+name+'" alt="'+escape(labels[name])+'"></a></article>' for name in shots)
    gallery = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Research design · Review gallery</title><style>body{margin:0;background:#f3f4ee;color:#242a27;font:15px/1.7 system-ui,sans-serif;padding:30px}h1{font-size:28px}p{color:#58675b}a{color:#345b48}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px}article{padding:16px;background:white;border:1px solid #d8dfd2;border-radius:6px}h2{font-size:14px;margin:0 0 12px}img{width:100%;height:330px;object-fit:contain;object-position:top;background:#f8f9f4}</style><h1>Tofu Research · 设计评审</h1><p>全部为演示数据 · 无真实执行。<a href="research-workbench.html">打开交互原型 →</a> · <a href="README.md">设计规格</a></p><main>'+rows+'</main></html>'
    (HERE / 'gallery.html').write_text(gallery)
    print(json.dumps({'portable_html': str(destination.relative_to(HERE.parent.parent)), 'bytes': destination.stat().st_size, 'gallery_images': len(shots), 'external_assets': False}, indent=2))


if __name__ == '__main__':
    package()
