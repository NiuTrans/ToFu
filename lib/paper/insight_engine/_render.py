"""Rendering — a grounded insight dict → a Markdown section.

Blockquote callouts reuse the report renderer's styled-callout convention;
grounded refs render as inline arXiv links, ungrounded refs as prose only.
"""

from lib.log import get_logger

logger = get_logger(__name__)

_HEADINGS = {
    'en': {
        'section': '## 💡 Insight & Ideas',
        'thesis': '### The Bet',
        'connections': '### Connections to Your Reading',
        'opinion': '### A Take',
        'open': '### Open Problems Worth Your Monday',
        'prov': '### Provocations',
    },
    'zh': {
        'section': '## 💡 洞见与灵感',
        'thesis': '### 这篇论文的赌注',
        'connections': '### 与你读过的工作的联系',
        'opinion': '### 一个观点',
        'open': '### 值得你周一动手的开放问题',
        'prov': '### 挑衅式追问',
    },
}


def _ref_md(card):
    """Render a grounded ref as a Markdown link, or '' when ungrounded/absent."""
    if not isinstance(card, dict) or not card.get('arxiv_id'):
        return ''
    url = card.get('abs_url') or f'https://arxiv.org/abs/{card["arxiv_id"]}'
    title = card.get('title') or card['arxiv_id']
    return f' ([{title}]({url}))'


def render_insight_markdown(insight, ui_lang='en'):
    """Render a grounded insight dict to a Markdown section.

    Blockquote callouts (``> Key takeaway:`` / ``> 关键结论：``) reuse the
    report renderer's styled-callout convention. Grounded refs render as inline
    arXiv links; ungrounded refs render as prose only.
    """
    if not isinstance(insight, dict):
        return ''
    h = _HEADINGS.get(ui_lang, _HEADINGS['en'])
    zh = ui_lang == 'zh'
    out = [h['section'], '']

    thesis = (insight.get('thesis') or '').strip()
    if thesis:
        kw = '关键结论：' if zh else 'Key takeaway:'
        out += [h['thesis'], '', f'> {kw} {thesis}', '']

    conns = [c for c in (insight.get('connections') or []) if isinstance(c, dict) and (c.get('text') or '').strip()]
    if conns:
        out += [h['connections'], '']
        for c in conns:
            out.append(f"- {c['text'].strip()}{_ref_md(c.get('paper'))}")
        out.append('')

    opinion = (insight.get('opinion') or '').strip()
    if opinion:
        out += [h['opinion'], '', opinion, '']

    ops = [o for o in (insight.get('open_problems') or []) if isinstance(o, dict) and (o.get('text') or '').strip()]
    if ops:
        out += [h['open'], '']
        for o in ops:
            out.append(f"- {o['text'].strip()}{_ref_md(o.get('grounded_by'))}")
        out.append('')

    # Provocations tolerate both the legacy plain-string schema and the v2
    # {'text', 'anchor'} object schema (the anchor is consumed by the reader,
    # not rendered here).
    provs = []
    for p in (insight.get('provocations') or []):
        if isinstance(p, str) and p.strip():
            provs.append(p.strip())
        elif isinstance(p, dict) and (p.get('text') or '').strip():
            provs.append(p['text'].strip())
    if provs:
        out += [h['prov'], '']
        for p in provs:
            out.append(f"- {p}")
        out.append('')

    return '\n'.join(out).rstrip() + '\n'
