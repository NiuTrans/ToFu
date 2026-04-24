"""Render the Tofu showcase posters (1080×1440, @2x) to PNG.

The 16.9MB NotoSansSC.ttf combined with `font-display: block` in style.css
means `document.fonts.ready` can resolve BEFORE text is actually drawn.
We explicitly load the FontFace and wait for h2 to have non-zero height
before taking the screenshot.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
FILES = [
    "1-cover",
    "2-pain",
    "3-features",
    "4-agents",
    "5-demo",
    "6-setup",
    "7-ecosystem",
]


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": 1080, "height": 1440},
            device_scale_factor=2,
        )
        page = ctx.new_page()
        for name in FILES:
            html = HERE / f"{name}.html"
            png = HERE / f"{name}.png"
            if not html.exists():
                print(f"✗ missing {html.name}")
                continue
            page.goto(html.as_uri(), wait_until="networkidle")
            # Force-load the SC font so text actually renders
            page.evaluate(
                """async () => {
                    const f = new FontFace(
                        'NotoSansSC', 'url(fonts/NotoSansSC.ttf)',
                        {weight: '100 900'});
                    await f.load();
                    document.fonts.add(f);
                    await document.fonts.ready;
                }"""
            )
            page.wait_for_function(
                "document.querySelector('h1, h2').getBoundingClientRect().height > 0",
                timeout=60_000,
            )
            page.wait_for_timeout(400)
            el = page.query_selector(".poster")
            el.screenshot(path=str(png))
            print(f"✓ {png.name}  {png.stat().st_size // 1024} KB")
        browser.close()


if __name__ == "__main__":
    main()
