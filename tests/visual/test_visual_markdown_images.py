"""Real Chromium specifications for Markdown image sizing and preview admission.

Uses actual Marked/DOMPurify, the production Markdown projection pipeline and
ConversationSurface delegation. Canvas bitmaps keep images hermetic; the local
frontend fixture serves shipped CSS without booting storage or a live app.
"""
from __future__ import annotations

import json

import pytest

from tests._runtime_sections import native_module_path, runtime_section
from tests.support.frontend_visual import (
    ROOT,
    frontend_visual_origin,  # noqa: F401 -- fixture dependency
    frontend_visual_page as frontend_visual_page,  # noqa: F401 -- fixture registration
)

pytestmark = pytest.mark.visual


@pytest.fixture
def markdown_image_page(frontend_visual_page):
    page = frontend_visual_page
    for name, source in [
        ('renderer', 'frontend/src/markdown/index.ts'),
        ('policy', 'frontend/src/markdown-policy.ts'),
        ('surface', 'frontend/src/conversation/ui/conversation-surface.ts'),
    ]:
        page.add_script_tag(path=native_module_path(
            f'.native/markdown-image-visual-{name}.js', ROOT / source))
    page.add_script_tag(path=str(ROOT / 'node_modules/marked/lib/marked.umd.js'))
    page.add_script_tag(path=str(ROOT / 'node_modules/dompurify/dist/purify.min.js'))
    page.add_style_tag(path=str(
        ROOT / 'frontend/src/conversation/ui/conversation-surface.css'))
    # Fixture shell only: no image/paragraph styling that could mask regressions.
    page.add_style_tag(content='''
      body { display:block; height:auto; overflow:auto; padding:16px; }
      #markdown-image-host { max-width:760px; margin:0 auto; }
    ''')
    page.evaluate(r'''() => {
      installMarkdownPolicy(marked);
      window.imageSources = {};
      for (const [name, width, height] of [
        ['portrait', 420, 840], ['landscape', 1200, 300], ['small', 64, 40],
      ]) {
        const canvas = document.createElement('canvas');
        canvas.width = width; canvas.height = height;
        const context = canvas.getContext('2d');
        context.fillStyle = '#497ca8'; context.fillRect(0, 0, width, height);
        context.fillStyle = '#eddca5';
        context.fillRect(0, 0, width / 2, height / 2);
        context.fillStyle = '#804e6a';
        context.fillRect(width * .75, height * .75, width / 4, height / 4);
        imageSources[name] = canvas.toDataURL('image/png');
      }
      window.mountMarkdownImages = ({theme, language, messages}) => {
        window.imageSurface?.dispose();
        window.imageRenderer?.dispose();
        document.documentElement.dataset.theme = theme;
        document.documentElement.lang = language;
        document.documentElement.classList.toggle('dark', theme === 'dark');
        document.body.innerHTML = '<main id="markdown-image-host"></main>';
        window.previewImages = [];
        window.imageLocalizationCalls = [];
        window.imageMessages = messages;
        const environment = {
          document, parser: marked, sanitizer: DOMPurify,
          highlighter: null, mathRenderer: null, basePath: '',
          projectActive: false, language,
          text: (key, fallback) => {
            const value = messages[key] ?? fallback;
            imageLocalizationCalls.push({key, value});
            return value;
          },
        };
        window.imageRenderer = createMarkdownRenderer({
          maximumCacheEntries: 16,
          getEnvironment: () => environment,
          ensureMath: () => {},
          reportFailure: error => { throw error; },
        });
        window.imageSurface = createConversationSurface(
          document.getElementById('markdown-image-host'), {
            onPreviewImage: source => previewImages.push(source),
          });
        window.appendImageMarkdown = (id, markdown) => {
          const block = document.createElement('section');
          // Include the legacy ancestor so its generic image rules participate.
          block.className = 'conversation-block conversation-block--text message-body';
          block.id = id;
          const content = document.createElement('div');
          content.className = 'md-content';
          content.innerHTML = imageRenderer.render(markdown);
          block.append(content);
          imageSurface.root.append(block);
          return content;
        };
        appendImageMarkdown('image-ratios', [
          `![Portrait](${imageSources.portrait})`, 'After portrait.',
          `![Landscape](${imageSources.landscape})`, 'After landscape.',
          `![Small](${imageSources.small})`, 'After small.',
        ].join('\n\n'));
        appendImageMarkdown('image-prose', [
          `[![Linked image](${imageSources.landscape})](#linked-image)`,
          `Before ![Inline image](${imageSources.small}) after **emphasis** and [source](#reference).`,
        ].join('\n\n'));
      };
    }''')
    try:
        yield page
    finally:
        page.evaluate('''() => {
          window.imageSurface?.dispose();
          window.imageRenderer?.dispose();
        }''')


def _mount(page, width=1280, theme='tofu', language='en'):
    page.set_viewport_size({'width': width, 'height': 900})
    messages = json.loads((
        ROOT / 'frontend/src/i18n/locales' / f'{language}.json').read_text())
    page.evaluate('options => mountMarkdownImages(options)', {
        'theme': theme, 'language': language, 'messages': messages,
    })
    page.wait_for_function('''() => {
      const images = [...document.querySelectorAll('#markdown-image-host img')];
      return images.length === 5 && images.every(image => image.complete
        && image.naturalWidth > 0 && image.naturalHeight > 0);
    }''', timeout=10000)
    return messages


@pytest.mark.parametrize('width', [1280, 390])
@pytest.mark.parametrize('theme', ['tofu', 'dark', 'light'])
def test_markdown_images_keep_intrinsic_ratio_and_one_paragraph_gap(
        markdown_image_page, width, theme, tmp_path):
    page = markdown_image_page
    language = 'en' if width == 1280 else 'zh'
    messages = _mount(page, width, theme, language)
    other_language = 'zh' if language == 'en' else 'en'
    other_messages = json.loads((
        ROOT / 'frontend/src/i18n/locales' / f'{other_language}.json').read_text())
    measurements = page.locator('#image-ratios img').evaluate_all('''images =>
      images.map(image => {
        const paragraph = image.closest('p');
        const following = paragraph.nextElementSibling;
        const content = image.closest('.md-content');
        const rect = image.getBoundingClientRect();
        const pRect = paragraph.getBoundingClientRect();
        const nextRect = following.getBoundingClientRect();
        const style = getComputedStyle(image);
        const pStyle = getComputedStyle(paragraph);
        const nextStyle = getComputedStyle(following);
        const number = value => parseFloat(value) || 0;

        const imageChromeX = number(style.paddingLeft) + number(style.paddingRight)
          + number(style.borderLeftWidth) + number(style.borderRightWidth);
        const imageChromeY = number(style.paddingTop) + number(style.paddingBottom)
          + number(style.borderTopWidth) + number(style.borderBottomWidth);
        const label = image.getAttribute('aria-label') || '';
        return {
          alt: image.alt, classes: [...image.classList],
          paragraphClasses: [...paragraph.classList],
          naturalWidth: image.naturalWidth, naturalHeight: image.naturalHeight,
          width: rect.width, height: rect.height, paragraphHeight: pRect.height,

          bitmapWidth: rect.width - imageChromeX, bitmapHeight: rect.height - imageChromeY,
          imageChromeX, imageChromeY,
          contentWidth: content.clientWidth, right: rect.right,
          maxWidth: style.maxWidth, aspectRatio: style.aspectRatio,
          imageMargin: number(style.marginTop) + number(style.marginBottom),
          paragraphChrome: number(pStyle.paddingTop) + number(pStyle.paddingBottom)
            + number(pStyle.borderTopWidth) + number(pStyle.borderBottomWidth),
          gap: nextRect.top - rect.bottom,
          expectedGap: Math.max(number(pStyle.marginBottom), number(nextStyle.marginTop)),
          role: image.getAttribute('role'), tabindex: image.getAttribute('tabindex'),
          label, localized: imageLocalizationCalls.filter(call =>
            imageMessages[call.key] === call.value && call.value && label.includes(call.value)),
        };
      })''')
    assert [image['alt'] for image in measurements] == ['Portrait', 'Landscape', 'Small']
    for image in measurements:
        assert 'md-image' in image['classes'], image
        assert 'md-preview-image' in image['classes'], image
        assert 'md-image-only' in image['paragraphClasses'], image
        assert image['role'] == 'button' and image['tabindex'] == '0', image
        assert image['label'] and image['localized'], image
        assert any(messages[call['key']] != other_messages.get(call['key'])
                   for call in image['localized']), image
        assert image['width'] > 0 and image['height'] > 0, image
        assert image['bitmapWidth'] / image['bitmapHeight'] == pytest.approx(
            image['naturalWidth'] / image['naturalHeight'], rel=.01), image
        assert image['aspectRatio'] not in ('3 / 2', '1.5 / 1'), image
        assert image['maxWidth'] == '100%', image
        assert image['width'] <= image['contentWidth'] + 1, image
        assert image['right'] <= width + 1, image
        # Detect inline-image descender/baseline space as well as extra margins.
        assert image['imageMargin'] == pytest.approx(0, abs=.5), image
        assert image['paragraphHeight'] - image['paragraphChrome'] == pytest.approx(
            image['height'], abs=1), image
        assert image['gap'] == pytest.approx(image['expectedGap'], abs=1), image
        assert image['expectedGap'] > 0, image
    small = measurements[-1]
    assert small['bitmapWidth'] <= small['naturalWidth'] + 1, small
    assert small['bitmapHeight'] <= small['naturalHeight'] + 1, small
    landscape = measurements[1]
    # Defeat the legacy 300px/200px thumbnail cap, not just the 3:2 box.
    assert landscape['width'] == pytest.approx(
        min(landscape['naturalWidth'] + landscape['imageChromeX'],
            landscape['contentWidth']), abs=1), landscape

    prose = page.locator('#image-prose')
    linked = prose.locator('a[href="#linked-image"] img')
    assert linked.get_attribute('class') and linked.evaluate(
        "image => image.classList.contains('md-image')")
    assert not linked.evaluate("image => image.classList.contains('md-preview-image')")
    assert linked.get_attribute('role') is None
    assert linked.get_attribute('tabindex') is None

    assert linked.evaluate('image => image.getBoundingClientRect().width <= image.closest(".md-content").clientWidth + 1')
    mixed = prose.locator('p').nth(1)
    assert not mixed.evaluate("paragraph => paragraph.classList.contains('md-image-only')")
    assert 'Before' in mixed.inner_text() and 'after' in mixed.inner_text()
    assert mixed.locator('strong').inner_text() == 'emphasis'
    assert mixed.locator('a[href="#reference"]').inner_text() == 'source'
    assert mixed.locator('img.md-image.md-preview-image').count() == 1
    page.locator('#markdown-image-host').screenshot(
        path=str(tmp_path / f'markdown-images-{width}-{theme}.png'))


@pytest.mark.parametrize('width,language', [(1280, 'en'), (390, 'zh')])
def test_surface_opens_current_image_with_pointer_and_keyboard_then_disposes(
        markdown_image_page, width, language):
    page = markdown_image_page
    _mount(page, width, 'tofu', language)
    image = page.locator('#image-ratios img').first
    initial_source = image.evaluate('image => image.src')
    image.click()
    assert page.evaluate('previewImages') == [initial_source]
    image.focus()
    assert image.evaluate('image => document.activeElement === image')
    page.keyboard.press('Enter')

    page.keyboard.press('Space')
    assert page.evaluate('previewImages') == [initial_source] * 3
    focus = image.evaluate('image => { const s=getComputedStyle(image); return {style:s.outlineStyle,width:parseFloat(s.outlineWidth)}; }')
    assert focus['style'] == 'solid' and focus['width'] >= 2
    # Real Chromium repeat events: holding either activation key opens once.
    for key in ('Enter', 'Space'):
        before = page.evaluate('previewImages.length')
        page.keyboard.down(key)
        page.keyboard.down(key)
        page.keyboard.up(key)
        assert page.evaluate('previewImages.length') == before + 1
    before = page.evaluate('previewImages.length')
    page.keyboard.press('ArrowRight')
    assert page.evaluate('previewImages.length') == before

    image.evaluate('image => { image.src = imageSources.landscape; }')
    current_source = image.evaluate('image => image.src')
    assert current_source != initial_source
    image.click()
    assert page.evaluate('previewImages.at(-1)') == current_source
    # Markup changes after projection must not reuse a formerly admitted source.
    image.evaluate("image => { image.src = 'javascript:alert(1)'; image.click(); }")
    assert page.evaluate('previewImages.length') == before + 1
    image.focus()
    page.keyboard.press('Enter')
    page.keyboard.press('Space')
    assert page.evaluate('previewImages.length') == before + 1
    image.evaluate('image => { image.src = imageSources.portrait; }')

    linked = page.locator('#image-prose a[href="#linked-image"] img')
    linked.click()
    assert page.evaluate('previewImages.length') == before + 1
    assert page.evaluate('location.hash') == '#linked-image'
    # Even a dynamically linked image with the projected class retains navigation.
    image.evaluate('''image => {
      const link = document.createElement('a'); link.href = '#dynamic-link';
      image.replaceWith(link); link.append(image);
    }''')
    image.click()
    assert page.evaluate('previewImages.length') == before + 1
    assert page.evaluate('location.hash') == '#dynamic-link'
    image.evaluate('image => image.parentElement.replaceWith(image)')

    page.evaluate('''() => {
      const rendered = imageRenderer.render(`![Outside](${imageSources.small})`);
      const outside = document.createElement('div');
      outside.id = 'outside-image-surface'; outside.className = 'md-content';
      outside.innerHTML = rendered; document.body.append(outside);
      const unscoped = document.querySelector('#image-ratios img').cloneNode();
      unscoped.id = 'outside-markdown-content'; imageSurface.root.append(unscoped);
    }''')
    page.locator('#outside-image-surface img').click()
    page.locator('#outside-markdown-content').click()
    assert page.evaluate('previewImages.length') == before + 1

    # Preserve the injection-lane data-preview-src pathway on the real Surface.
    page.evaluate('''() => {
      const thumbnail = document.createElement('img');
      thumbnail.id = 'injection-preview'; thumbnail.src = imageSources.small;
      thumbnail.dataset.previewSrc = imageSources.landscape;
      imageSurface.root.append(thumbnail);
    }''')
    page.locator('#injection-preview').click()
    assert page.evaluate('previewImages.at(-1)') == current_source
    assert page.evaluate('previewImages.length') == before + 2
    page.evaluate('''() => {
      const root = imageSurface.root;
      imageSurface.dispose();
      // Reattach the retired root so leaked listeners can actually receive events.
      document.getElementById('markdown-image-host').append(root);
    }''')
    image.click()
    image.focus()
    page.keyboard.press('Enter')
    page.keyboard.press('Space')
    page.locator('#injection-preview').click()
    assert page.evaluate('previewImages.length') == before + 2


def test_unsafe_or_missing_sources_never_gain_markdown_preview(markdown_image_page):
    page = markdown_image_page
    _mount(page)
    page.evaluate(r'''() => {
      const svg = 'data:image/svg+xml;base64,' + btoa('<svg xmlns="http://www.w3.org/2000/svg"/>');
      const unsafe = [
        '<img src="javascript:alert(1)" alt="JavaScript" onerror="alert(1)">',
        '<img src="vbscript:msgbox(1)" alt="VBScript">',
        '<img src="file:///tmp/workspace-image.png" alt="Workspace">',
        '<img src="data:text/html;base64,PGgxPm5vdCBhbiBpbWFnZTwvaDE+" alt="HTML">',
        `<img src="${svg}" alt="SVG">`,
        '<img alt="No source">', '<img src="" alt="Empty source">',
        '<img src="javascript:alert(1)" class="md-image md-preview-image" role="button" tabindex="0" alt="Forged preview">',
      ];
      unsafe.forEach((source, index) => appendImageMarkdown(`unsafe-image-${index}`, source));
    }''')
    cases = page.locator('[id^="unsafe-image-"]')
    assert cases.count() == 8
    assert cases.locator('img.md-preview-image').count() == 0
    assert cases.locator('[onerror], [onclick], script').count() == 0
    # SVG may remain displayable, but unsafe data must never be an opener control.
    assert cases.locator('img[role="button"], img[tabindex="0"]').count() == 0
    page.evaluate('''() => {
      for (const image of document.querySelectorAll('[id^="unsafe-image-"] img')) {
        image.click();
        image.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'Enter', bubbles: true, cancelable: true,
        }));
      }
    }''')
    assert page.evaluate('previewImages') == []


def test_markdown_click_reaches_existing_preview_modal_and_restores_focus(markdown_image_page):
    page = markdown_image_page
    _mount(page)
    page.add_script_tag(content=runtime_section('upload_preview.js', scope_prelude=False))
    page.evaluate('''() => {
      const modal = document.createElement('div'); modal.id='previewModal';
      modal.className='preview-modal'; modal.innerHTML='<div id="previewBody" class="preview-body"></div>';
      document.body.append(modal);
      imageSurface.dispose();
      const host = document.getElementById('markdown-image-host');
      window.imageSurface=createConversationSurface(host, {onPreviewImage:openImagePreview});
      const content=document.createElement('div'); content.className='md-content';
      content.innerHTML=imageRenderer.render(`![Screenshot](${imageSources.small})`);
      imageSurface.root.append(content);
      modal.addEventListener('click', event => {
        if(event.target.closest('.preview-close-btn')) closePreview();
      });
      modal.addEventListener('keydown', event => {
        if(event.key==='Escape') { closePreview(); event.preventDefault(); }
      });
    }''')
    image = page.locator('#markdown-image-host img')
    image.focus()
    page.keyboard.press('Enter')
    modal = page.locator('#previewModal')
    assert modal.evaluate("element => element.classList.contains('open')")
    assert modal.get_attribute('role') == 'dialog'
    assert page.locator('#previewBody .preview-image').evaluate('image => image.src') == image.evaluate('image => image.src')
    close = modal.locator('.preview-close-btn')
    assert close.evaluate('button => document.activeElement === button')
    page.keyboard.press('Tab')
    assert close.evaluate('button => document.activeElement === button')
    page.keyboard.press('Shift+Tab')
    assert close.evaluate('button => document.activeElement === button')
    page.keyboard.press('Escape')
    assert image.evaluate('image => document.activeElement === image')
    # Reopen before the old 300ms body cleanup: new content must survive.
    page.keyboard.press('Space')
    page.wait_for_timeout(350)
    assert modal.evaluate("element => element.classList.contains('open')")
    assert modal.locator('.preview-image').count() == 1
    close.click()
    assert image.evaluate('image => document.activeElement === image')
    page.wait_for_timeout(350)
    assert modal.locator('.preview-image').count() == 0
