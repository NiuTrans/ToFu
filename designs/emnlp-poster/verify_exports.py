"""Independently render the SVG and decode the QR from the PDF print master."""

import argparse
import json
from pathlib import Path

import fitz
from PIL import Image, ImageChops, ImageStat
from playwright.sync_api import sync_playwright
import zxingcpp
from layout import PROOF_SECTIONS, QR


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", required=True)
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).parent/"output")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    with fitz.open(output/"tofu-poster-A0.pdf") as pdf:
        mm = 72/25.4
        qx, qy, qs = QR
        clip = fitz.Rect(qx*mm, qy*mm, (qx+qs)*mm, (qy+qs)*mm)
        pix = pdf[0].get_pixmap(matrix=fitz.Matrix(300/72, 300/72), clip=clip)
        raster = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        decoded = [code.text for code in zxingcpp.read_barcodes(raster)]
        expected = "https://github.com/NiuTrans/ToFu"
        if decoded != [expected]:
            raise RuntimeError(f"QR decode mismatch: {decoded!r}")
        (output/"qr-verification.json").write_text(json.dumps({
            "source": "final PDF at 300 dpi", "decoded": decoded, "passed": True,
        }, indent=2))
        for name, y0, y1 in PROOF_SECTIONS:
            pix = pdf[0].get_pixmap(matrix=fitz.Matrix(1, 1),
                                   clip=fitz.Rect(0, y0*mm, 841*mm, y1*mm))
            pix.save(output/f"proof-{name}.png")
    reference = Image.open(output/"tofu-poster-preview.png").convert("RGB")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=args.browser, headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={
            "width": reference.width, "height": reference.height},
            device_scale_factor=1)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        svg = (output/"tofu-poster-editable.svg").read_text()
        page.set_content(
            '<html><head><style>html,body{margin:0;background:white;}'
            f'body>svg{{display:block;width:{reference.width}px;height:{reference.height}px}}'
            '</style></head><body>'+svg+'</body></html>')
        page.evaluate("document.fonts.ready")
        # All images are embedded. Wait for each decoded asset before rasterizing.
        page.evaluate("""async () => {
          await Promise.all([...document.querySelectorAll('image')].map(node =>
            new Promise((resolve,reject) => {
              const image = new Image();
              image.onload=resolve; image.onerror=reject;
              image.src=node.getAttribute('href');
            })));
        }""")
        geometry = page.evaluate("""() => {
          const invalid=[...document.querySelectorAll('body>svg>text')].filter(node=>{
            const b=node.getBBox();
            return b.x < -.1 || b.y < -.1 || b.x+b.width > 841.1 || b.y+b.height > 1189.1;
          }).map(node=>node.textContent);
          return {textOutsidePage:invalid, loadedFontFaces:[...document.fonts]
            .filter(f=>f.status==='loaded').map(f=>f.family)};
        }""")
        page.screenshot(path=str(output/"tofu-poster-svg-proof.png"))
        browser.close()
    if errors or geometry["textOutsidePage"]:
        raise RuntimeError({"browser_errors": errors, **geometry})
    alternative = Image.open(output/"tofu-poster-svg-proof.png").convert("RGB")
    diff = ImageChops.difference(reference, alternative)
    difference = sum(ImageStat.Stat(diff).mean)/3
    (output/"svg-preflight.json").write_text(json.dumps({
        "browser_errors": errors, **geometry,
        "same_size_as_pdf_render": alternative.size == reference.size,
        "mean_absolute_channel_difference_0_to_255": difference,
        "note": "Antialiasing differs between engines; visual review remains required.",
    }, indent=2))
    print(json.dumps({"QR": decoded, **geometry,
                      "mean_image_difference": difference}, indent=2))


if __name__ == "__main__":
    main()
