"""Place extracted figures into report Markdown deterministically."""

import re

from lib.log import get_logger

logger = get_logger(__name__)


def inject_images_into_report(report_md, images, lang='en', appendix=True,
                               allow_images=True):
    """Auto-insert extracted figures/tables into the report markdown.

    LLMs frequently ignore "please embed ``![caption](url)``" instructions in
    the manifest, so we do it deterministically: for each image whose caption
    begins with a figure/table number (e.g. ``Figure 3: …`` / ``Table 1 …`` /
    ``图 3 …``), find the first paragraph in the report that mentions that
    number and insert the image right after it. Any images that can't be
    matched to a mention are appended as an appendix at the end.

    If the model *did* embed images correctly (unlikely but possible) we
    bail out to avoid duplicates.

    Args:
        report_md: The generated report Markdown.
        images: Manifest entries ``[{url, caption, page, source, ...}]``.
        lang: 'zh' or 'en' — controls the appendix heading.
        appendix: When True (default, the explainer-report case) every
            unreferenced figure is appended as an appendix gallery so nothing
            is lost. When False the appendix is SUPPRESSED — only figures the
            text actually cites (``Figure N``) are placed inline. Ignored when
            ``allow_images`` is False.
        allow_images: When True (default) figures are injected/kept as above.
            When False (Review Mode) the report is TEXT-ONLY: no figure is
            injected AND any paper-image embed the model emitted itself is
            stripped to its alt text. A peer review is a decision document,
            not an illustrated explainer — it must carry no figures at all.

    Returns:
        Enriched report Markdown, or the original string on failure / no-op.
    """
    if not report_md:
        return report_md
    if not allow_images:
        # Text-only mode (peer review): a review is a Markdown PROSE decision
        # document — there is NO legitimate raw HTML in it. Rather than chase
        # an ever-growing denylist of image-bearing tags (<img>/<picture>/
        # <svg>/<object>/<embed>/<input type=image>/<image>/<video poster>/
        # <iframe>/background=… — whack-a-mole, and the backstop must not lean
        # on a downstream sanitizer), we degrade the Markdown image forms to
        # *alt*, drop orphaned image link-defs, then neutralize ALL remaining
        # raw HTML. That collapses the entire image-vector class (including any
        # tag invented later) into one rule. Fenced/inline code, math, and
        # escaped entities (&lt;img&gt; — no literal '<', so never matched) are
        # preserved.
        try:
            # Protect code + math spans so their contents are never mangled
            # (a code example showing <img> is text, not a rendered image).
            protected: list[str] = []

            def _mask(m):
                protected.append(m.group(0))
                return f'\x00{len(protected) - 1}\x00'

            _PROTECT_RE = re.compile(
                r'```.*?```'          # fenced code (```)
                r'|~~~.*?~~~'         # fenced code (~~~)
                r'|`[^`\n]+`'         # inline code
                r'|\$\$.*?\$\$'       # display math
                r'|\$[^$\n]+\$',      # inline math
                re.DOTALL)
            stripped = _PROTECT_RE.sub(_mask, report_md)

            image_ref_labels = set()

            def _strip_alt(m):
                alt = (m.group(1) or '').strip()
                return f'*{alt}*' if alt else ''

            # 1) Inline Markdown image: ![alt](any-url) → italic alt (or drop).
            stripped = re.sub(r'!\[([^\]]*)\]\([^)]*\)', _strip_alt, stripped)

            # 2) Full / collapsed reference image: ![alt][ref] / ![alt][].
            def _strip_ref_img(m):
                alt = (m.group(1) or '').strip()
                ref = (m.group(2) or '').strip()
                image_ref_labels.add((ref or alt).lower())
                return f'*{alt}*' if alt else ''
            stripped = re.sub(r'!\[([^\]]*)\]\[([^\]]*)\]', _strip_ref_img, stripped)

            # 3) Shortcut reference image: ![alt] (label == alt text).
            def _strip_shortcut_img(m):
                alt = (m.group(1) or '').strip()
                image_ref_labels.add(alt.lower())
                return f'*{alt}*' if alt else ''
            stripped = re.sub(r'!\[([^\]]*)\]', _strip_shortcut_img, stripped)

            # 4) Drop orphaned link-definition lines that only fed an image:
            #    those referenced by an image above, or whose target is itself
            #    an image URL. Non-image link defs are kept.
            _img_url_re = re.compile(
                r'\.(?:png|jpe?g|gif|webp|svg|bmp|tiff?|avif)(?:[?#]|$)', re.IGNORECASE)

            def _keep_line(line):
                m = re.match(r'^\s*\[([^\]]+)\]:\s*(\S+)', line)
                if not m:
                    return True
                label, url = m.group(1).strip().lower(), m.group(2).strip()
                is_img_url = bool(_img_url_re.search(url)
                                  or url.lower().startswith('data:image')
                                  or '/api/paper/images/' in url)
                return not (label in image_ref_labels or is_img_url)
            stripped = '\n'.join(ln for ln in stripped.split('\n') if _keep_line(ln))

            # 5) Neutralize ALL remaining raw HTML tags (opening / closing /
            #    void), keeping the inner text between them (e.g. an <object>
            #    fallback or a <figcaption>'s prose). The tag name must start
            #    with a letter, so an inequality (``a < b``, ``n <5``) and an
            #    escaped entity (``&lt;img&gt;``, which has no literal ``<``)
            #    are left untouched. The attribute span matches a full quoted
            #    string OR any non-``>`` char, so a legal ``>`` INSIDE a quoted
            #    attribute value (``<img alt="a>b" src=x>``) does NOT terminate
            #    the tag early; it also spans newlines for a multi-line tag.
            stripped = re.sub(
                r'''</?[A-Za-z][A-Za-z0-9:-]*(?:\s(?:"[^"]*"|'[^']*'|[^>])*)?/?>''',
                '', stripped)

            # Restore protected code/math spans verbatim.
            def _unmask(m):
                return protected[int(m.group(1))]
            stripped = re.sub(r'\x00(\d+)\x00', _unmask, stripped)
            return stripped
        except Exception as e:
            logger.warning('[Paper:Report] Image strip failed (returning original): %s',
                           e, exc_info=True)
            return report_md
    try:
        # Strip "fake" image references the model invents when we ask it to
        # embed figures but no matching manifest entry exists.  E.g. for a
        # Table 5 that wasn't extracted, models like to write
        #   ![表 5 — 设计选择消融](表 5 数据见正文)
        # marked.js refuses to render the bracketed text as a URL (it
        # contains spaces) and falls back to the literal `![...](...)` —
        # which the user sees as a broken/un-rendered placeholder.  Detect
        # any `![alt](url)` whose URL is not http(s)://, /, or data: and
        # downgrade it to its alt text in italics.
        def _strip_fake_img(m):
            alt, url = m.group(1), m.group(2).strip()
            if re.match(r'^(?:https?://|/|data:|#)', url):
                return m.group(0)
            return f'*{alt}*' if alt.strip() else ''
        report_md = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', _strip_fake_img, report_md)

        if not images:
            return report_md

        # If the model already embedded any paper image, trust it and skip.
        if re.search(r'!\[[^\]]*\]\(/api/paper/images/', report_md):
            return report_md

        # Parse each caption for kind + number so we can find textual mentions.
        fig_re = re.compile(r'^\s*(?:Figure|Fig\.?|图)\s*\.?\s*(\d+)', re.IGNORECASE)
        tab_re = re.compile(r'^\s*(?:Table|Tab\.?|表)\s*\.?\s*(\d+)', re.IGNORECASE)
        parsed = []
        for img in images:
            url = (img.get('url') or '').strip()
            cap = (img.get('caption') or '').strip()
            if not url:
                continue
            kind, num = None, None
            m = fig_re.match(cap)
            if m:
                kind, num = 'figure', int(m.group(1))
            else:
                m = tab_re.match(cap)
                if m:
                    kind, num = 'table', int(m.group(1))
            # Alt text must not contain newlines or ] that would break syntax.
            alt = (cap.replace('\n', ' ')
                      .replace(']', ')')
                      .replace('[', '(')).strip()[:200] or (
                      ('Figure' if kind == 'figure' else 'Table' if kind == 'table' else 'Figure')
                      + (f' {num}' if num else ''))
            parsed.append({'url': url, 'caption': cap, 'alt': alt,
                           'kind': kind, 'num': num})

        # Split report into paragraphs preserving separators.
        # paras = [p0, sep0, p1, sep1, ...]
        paras = re.split(r'(\n\n+)', report_md)

        # Pick the paragraph that DISCUSSES each figure most thoroughly,
        # not just the first one that mentions it.  Models often write a
        # one-line drive-by mention near the top ("...as shown in Figure 4")
        # and then return to the figure with substantive analysis several
        # paragraphs later.  Inserting next to the drive-by mention puts the
        # image far from its discussion, which the user explicitly flagged.
        # Heuristic: among paragraphs that mention the figure, pick the
        # longest one (proxy for "most detailed discussion").
        placed = set()
        by_para: dict[int, list[str]] = {}
        # First, pick best paragraph for each parsed image
        candidate_paras: list[tuple[int, int]] = []  # (para_index, length)
        for i in range(0, len(paras), 2):
            p = paras[i]
            stripped = p.strip()
            if not stripped:
                continue
            if stripped.startswith('```') or stripped.startswith('|'):
                continue
            candidate_paras.append((i, len(stripped)))
        for pi, img in enumerate(parsed):
            if img['kind'] is None or img['num'] is None:
                continue
            if img['kind'] == 'figure':
                pat = rf'(?:Figure|Fig\.?|图)\s*\.?\s*{img["num"]}\b'
            else:
                pat = rf'(?:Table|Tab\.?|表)\s*\.?\s*{img["num"]}\b'
            best_idx, best_len = -1, -1
            for idx, plen in candidate_paras:
                if re.search(pat, paras[idx], re.IGNORECASE) and plen > best_len:
                    best_idx, best_len = idx, plen
            if best_idx >= 0:
                by_para.setdefault(best_idx, []).append(
                    f'\n\n![{img["alt"]}]({img["url"]})\n\n')
                placed.add(pi)

        # Insert from the end so earlier indices stay valid.
        for i in sorted(by_para.keys(), reverse=True):
            paras.insert(i + 1, ''.join(by_para[i]))
        out = ''.join(paras)

        # Append any unreferenced images as an appendix gallery — UNLESS the
        # caller opted out (Review Mode), where a wall of every extracted
        # figure would be exactly the kind of padding the review must avoid.
        unplaced = [p for pi, p in enumerate(parsed) if pi not in placed]
        if unplaced and appendix:
            title = '图表附录' if lang == 'zh' else 'Figures & Tables (Appendix)'
            blurb = ('论文中未在报告正文中显式引用的图表：'
                     if lang == 'zh'
                     else 'Figures and tables from the paper not referenced above:')
            out = out.rstrip() + f'\n\n---\n\n## 📎 {title}\n\n{blurb}\n\n'
            for img in unplaced:
                out += f'![{img["alt"]}]({img["url"]})\n\n'
                if img['caption']:
                    cap_clean = img['caption'].replace('\n', ' ').strip()
                    out += f'*{cap_clean}*\n\n'

        logger.info('[Paper:Report] Image inject — %d placed inline, %d in appendix '
                    '(%d total)', len(placed), len(unplaced), len(parsed))
        return out
    except Exception as e:
        logger.warning('[Paper:Report] Image injection failed (returning original): %s',
                       e, exc_info=True)
        return report_md
