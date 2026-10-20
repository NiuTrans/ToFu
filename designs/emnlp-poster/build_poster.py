#!/usr/bin/env python3
"""Build the English A0 Tofu poster from one editable vector scene.

Entry: python build_poster.py --output-dir ./output
Dependencies: ReportLab, PyMuPDF, qrcode, Pillow; bundled static font instances.
PDF is the print master. SVG uses the same coordinates, line breaks and fonts.
No application server, model, network call, or user database is accessed.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import math
import mimetypes
import re
from pathlib import Path

import fitz
import qrcode
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
MM = 72 / 25.4
WIDTH, HEIGHT = 841.0, 1189.0
INK = "#142C49"
PAPER = "#FFFFFF"
MUTED = "#50645C"
RULE = "#CECFC0"
TEAL = "#1C7166"
MINT = "#C9E6D0"
PALE = "#E8EEDF"
ORANGE = "#B54C2E"
PEACH = "#F4E2D2"
WHITE = "#FFFFFF"
DARK_LINE = "#57766B"
URL = "https://github.com/NiuTrans/ToFu"

FONT_FILES = {
    "body": "Inter-400.ttf",
    "medium": "Inter-600.ttf",
    "bold": "Inter-700.ttf",
    "serif": "Newsreader-400.ttf",
    "serif_medium": "Newsreader-500.ttf",
    "mono": "JetBrainsMono-Regular.ttf",
}
FONT_STYLES = {
    "body": ("Tofu Poster Inter", 400),
    "medium": ("Tofu Poster Inter", 600),
    "bold": ("Tofu Poster Inter", 700),
    "serif": ("Tofu Poster Newsreader", 400),
    "serif_medium": ("Tofu Poster Newsreader", 500),
    "mono": ("JetBrains Mono", 400),
}
for font_name, file_name in FONT_FILES.items():
    pdfmetrics.registerFont(TTFont(font_name, str(ASSETS / file_name)))
_faces = [pdfmetrics.getFont(name).face.name for name in FONT_FILES]
if len(set(_faces)) != len(_faces):
    raise ValueError("Static font instances must have distinct PostScript names. Run prepare_fonts.py.")


class Scene:
    """Millimetre coordinates, top-left origin; text has an explicit baseline."""

    def __init__(self):
        self.items: list[dict] = []

    def rect(self, x, y, w, h, fill, stroke=None, sw=0.4, radius=0):
        self.items.append(dict(kind="rect", x=x, y=y, w=w, h=h, fill=fill,
                               stroke=stroke, sw=sw, radius=radius))

    def line(self, x1, y1, x2, y2, color=RULE, sw=0.4, dash=None):
        self.items.append(dict(kind="line", x1=x1, y1=y1, x2=x2, y2=y2,
                               color=color, sw=sw, dash=dash))

    def path(self, points, fill, stroke=None, sw=0.4):
        self.items.append(dict(kind="polygon", points=points, fill=fill,
                               stroke=stroke, sw=sw))

    def arrow(self, x1, y1, x2, y2, color=TEAL, sw=0.6, head=3):
        self.line(x1, y1, x2, y2, color, sw)
        angle = math.atan2(y2-y1, x2-x1)
        points = [(x2, y2)]
        for offset in (0.50, -0.50):
            points.append((x2-head*math.cos(angle+offset),
                           y2-head*math.sin(angle+offset)))
        self.path(points, color)

    def text(self, text, x, y, size=27, font="body", color=INK,
             width=None, leading=None, align="left", tag="body"):
        """Wrap by actual font metrics; y is the first line's baseline."""
        leading = leading or size / MM * 1.32
        lines = []
        for paragraph in text.split("\n"):
            line = ""
            for word in paragraph.split():
                candidate = f"{line} {word}".strip()
                if width and line and pdfmetrics.stringWidth(candidate, font, size)/MM > width:
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            lines.append(line)
        for i, line in enumerate(lines):
            measured = pdfmetrics.stringWidth(line, font, size)/MM
            if width is not None and measured > width+0.1:
                raise ValueError(f"Unbreakable text exceeds width: {line}")
            actual_x = x - measured/2 if align == "center" else x-measured if align == "right" else x
            self.items.append(dict(kind="text", text=line, x=actual_x,
                                   y=y+i*leading, size=size, font=font,
                                   color=color, width=measured, tag=tag))
        return y+(len(lines)-1)*leading

    def label(self, text, x, y, color=MUTED, size=18, align="left"):
        self.text(text, x, y, size=size, font="medium", color=color, tag="label", align=align)

    def logo(self, x, y, w, h):
        self.items.append(dict(kind="logo", x=x, y=y, w=w, h=h))

    def asset(self, filename, x, y, w, h):
        self.items.append(dict(kind="asset", filename=filename, x=x, y=y, w=w, h=h))

    def qr(self, x, y, size):
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
        qr.add_data(URL)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        module = size/len(matrix)
        self.rect(x, y, size, size, WHITE)
        for row, values in enumerate(matrix):
            for col, value in enumerate(values):
                if value:
                    self.rect(x+col*module, y+row*module, module, module, INK)


from layout import QR, make_scene


def rgb(value):
    return tuple(int(value[i:i+2], 16)/255 for i in (1, 3, 5))


def draw_pdf(scene, destination):
    c = canvas.Canvas(str(destination), pagesize=(WIDTH*MM, HEIGHT*MM),
                      pageCompression=1, initialFontName="body", initialFontSize=27)
    c.setTitle("ToFu: A White-Box, Token-Efficient Agent Harness for Researchers")
    c.setAuthor("Junhao Ruan, Yuan Ge, Bei Li and collaborators")
    c.setSubject("EMNLP 2026 current-system poster: asynchronous tools, persistent sub-agents, and TofuDB")
    # Select a bundled font before the first text object, avoiding unused Helvetica.
    c.setFont("body", 27)
    for a in scene.items:
        if a["kind"] == "text":
            c.setFillColorRGB(*rgb(a["color"]))
            c.setFont(a["font"], a["size"])
            c.drawString(a["x"]*MM, (HEIGHT-a["y"])*MM, a["text"])
        elif a["kind"] == "rect":
            c.setFillColorRGB(*rgb(a["fill"]))
            if a["stroke"]:
                c.setStrokeColorRGB(*rgb(a["stroke"]))
            c.setLineWidth(a["sw"]*MM)
            if a["radius"]:
                c.roundRect(a["x"]*MM, (HEIGHT-a["y"]-a["h"])*MM,
                            a["w"]*MM, a["h"]*MM, a["radius"]*MM,
                            stroke=bool(a["stroke"]), fill=1)
            else:
                c.rect(a["x"]*MM, (HEIGHT-a["y"]-a["h"])*MM,
                       a["w"]*MM, a["h"]*MM, stroke=bool(a["stroke"]), fill=1)
        elif a["kind"] == "line":
            c.setStrokeColorRGB(*rgb(a["color"]))
            c.setLineWidth(a["sw"]*MM)
            c.setDash(a["dash"] or [])
            c.line(a["x1"]*MM, (HEIGHT-a["y1"])*MM,
                   a["x2"]*MM, (HEIGHT-a["y2"])*MM)
            c.setDash([])
        elif a["kind"] == "polygon":
            c.setFillColorRGB(*rgb(a["fill"]))
            p = c.beginPath()
            p.moveTo(a["points"][0][0]*MM, (HEIGHT-a["points"][0][1])*MM)
            for x, y in a["points"][1:]:
                p.lineTo(x*MM, (HEIGHT-y)*MM)
            p.close()
            c.drawPath(p, stroke=0, fill=1)
    c.linkURL(URL, (32*MM, (HEIGHT-1184)*MM, 335*MM, (HEIGHT-1167)*MM), relative=0)
    qx, qy, qs = QR
    c.linkURL(URL, (qx*MM, (HEIGHT-qy-qs)*MM, (qx+qs)*MM, (HEIGHT-qy)*MM), relative=0)
    c.showPage()
    c.save()
    # Import the supplied mascot as vector PDF, never as a screenshot.
    doc = fitz.open(destination)
    logo = fitz.open(ASSETS/"tofu-logo.pdf")
    for a in scene.items:
        if a["kind"] == "logo":
            r = fitz.Rect(a["x"]*MM, a["y"]*MM,
                          (a["x"]+a["w"])*MM, (a["y"]+a["h"])*MM)
            doc[0].show_pdf_page(r, logo, 0, keep_proportion=True, overlay=True)
        elif a["kind"] == "asset":
            r = fitz.Rect(a["x"]*MM, a["y"]*MM,
                          (a["x"]+a["w"])*MM, (a["y"]+a["h"])*MM)
            path = ASSETS/a["filename"]
            if path.suffix == ".svg":
                with fitz.open(path) as vector:
                    with fitz.open("pdf", vector.convert_to_pdf()) as converted:
                        doc[0].show_pdf_page(r, converted, 0, keep_proportion=True)
            else:
                doc[0].insert_image(r, filename=str(path), keep_proportion=True)
    doc.subset_fonts()
    temporary = destination.with_suffix(".final.pdf")
    doc.save(temporary, garbage=4, deflate=True)
    doc.close()
    logo.close()
    temporary.replace(destination)


def draw_svg(scene, destination):
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}mm" '
           f'height="{HEIGHT}mm" viewBox="0 0 {WIDTH} {HEIGHT}" role="img">'
           '<title>ToFu: A White-Box, Token-Efficient Agent Harness for Researchers</title>'
           '<desc>English A0 poster about synchronous and asynchronous tools, persistent '
           'sub-agents, and TofuDB incremental continuation. All prose is editable text.</desc>',
           "<defs><style>"]
    for name, filename in FONT_FILES.items():
        data = base64.b64encode((ASSETS/filename).read_bytes()).decode()
        out.append(f"@font-face{{font-family:'tofu_{name}';src:url(data:font/ttf;base64,{data}) "
                   f"format('truetype');font-weight:{FONT_STYLES[name][1]};}}")
    # Match ReportLab's explicit unkerned string metrics and literal code glyphs.
    out.append("text{font-kerning:none;font-variant-ligatures:none;}</style></defs>")
    for a in scene.items:
        if a["kind"] == "text":
            family, weight = FONT_STYLES[a["font"]]
            out.append(f'<text x="{a["x"]:.4f}" y="{a["y"]:.4f}" '
                       f'font-family="tofu_{a["font"]}, &quot;{family}&quot;" '
                       f'font-weight="{weight}" font-size="{a["size"]/MM:.5f}" '
                       f'fill="{a["color"]}">{html.escape(a["text"])}</text>')
        elif a["kind"] == "rect":
            stroke = f' stroke="{a["stroke"]}" stroke-width="{a["sw"]}"' if a["stroke"] else ""
            out.append(f'<rect x="{a["x"]:.5f}" y="{a["y"]:.5f}" '
                       f'width="{a["w"]:.5f}" height="{a["h"]:.5f}" '
                       f'rx="{a["radius"]}" fill="{a["fill"]}"{stroke}/>')
        elif a["kind"] == "line":
            dash = f' stroke-dasharray="{a["dash"]}"' if a["dash"] else ""
            out.append(f'<line x1="{a["x1"]}" y1="{a["y1"]}" x2="{a["x2"]}" y2="{a["y2"]}" '
                       f'stroke="{a["color"]}" stroke-width="{a["sw"]}"{dash}/>')
        elif a["kind"] == "polygon":
            points = " ".join(f"{x:.5f},{y:.5f}" for x, y in a["points"])
            out.append(f'<polygon points="{points}" fill="{a["fill"]}"/>')
        elif a["kind"] == "logo":
            raw = (ASSETS/"tofu-logo.svg").read_text()
            raw = re.sub(r'<\?xml[^>]*\?>', '', raw)
            viewbox = re.search(r'viewBox="([^"]+)"', raw).group(1)
            body = raw[raw.index(">")+1:raw.rindex("</svg>")]
            out.append(f'<svg x="{a["x"]}" y="{a["y"]}" width="{a["w"]}" height="{a["h"]}" '
                       f'viewBox="{viewbox}" preserveAspectRatio="xMidYMid meet">{body}</svg>')
        elif a["kind"] == "asset":
            path = ASSETS/a["filename"]
            mime = mimetypes.guess_type(path)[0]
            data = base64.b64encode(path.read_bytes()).decode()
            out.append(f'<image x="{a["x"]}" y="{a["y"]}" width="{a["w"]}" height="{a["h"]}" '
                       f'href="data:{mime};base64,{data}" preserveAspectRatio="xMidYMid meet"/>')
    out.append("</svg>")
    destination.write_text("\n".join(out), encoding="utf-8")


def inspect_pdf(scene, pdf_path, output):
    """Check the exported artifact, then render it for visual review."""
    doc = fitz.open(pdf_path)
    page = doc[0]
    failures = []
    if len(doc) != 1:
        failures.append("PDF must contain exactly one page")
    dims = [page.rect.width/MM, page.rect.height/MM]
    if any(abs(a-b) > 0.02 for a, b in zip(dims, [WIDTH, HEIGHT])):
        failures.append("Wrong A0 physical dimensions")
    fonts = []
    for entry in page.get_fonts(full=True):
        xref, ext, font_type, name = entry[:4]
        extracted = doc.extract_font(xref)
        fonts.append(dict(name=name, type=font_type, embedded=bool(extracted[-1])))
        if not extracted[-1]:
            failures.append(f"Unembedded font: {name}")
    spans = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                r = fitz.Rect(span["bbox"])
                if not page.rect.contains(r):
                    failures.append(f'Text outside page: {span["text"]}')
                spans.append(span)
    expected = [a for a in scene.items if a["kind"] == "text"]
    actual_text = page.get_text()
    # Extraction may normalize typographic ligatures; compare stable text anchors.
    for anchor in ["A White-Box, Token-Efficient", "Northeastern University",
                   "NiuTrans Research", "YourProvider LongCat RSI", "3 header checks",
                   "github.com/NiuTrans/ToFu"]:
        if anchor not in actual_text:
            failures.append(f"Missing PDF text: {anchor}")
    text_overlaps = []
    for i, a in enumerate(expected):
        ar = fitz.Rect(a["x"]*MM, a["y"]*MM-a["size"]*0.77,
                       (a["x"]+a["width"])*MM, a["y"]*MM+a["size"]*0.18)
        for b in expected[i+1:]:
            br = fitz.Rect(b["x"]*MM, b["y"]*MM-b["size"]*0.77,
                           (b["x"]+b["width"])*MM, b["y"]*MM+b["size"]*0.18)
            inter = ar & br
            if not inter.is_empty and inter.width > 1 and inter.height > 1:
                text_overlaps.append([a["text"], b["text"]])
    if text_overlaps:
        failures.append("Overlapping text bounds")
    used_font_count = len({a["font"] for a in expected})
    if len(fonts) != used_font_count:
        failures.append("Export lost or unexpectedly added a font face")
    report = dict(page_count=len(doc), dimensions_mm=dims, fonts=fonts,
                  raster_images=len(page.get_images()), text_spans=len(spans),
                  word_count=len(re.findall(r"\b[\w’-]+\b", actual_text)),
                  minimum_main_body_pt=min(a["size"] for a in expected if a["tag"] == "body"),
                  text_overlaps=text_overlaps, failures=failures,
                  qr_target=URL, print_master=pdf_path.name,
                  notes=["Vector text, diagrams, measured tables, QR and mascot; official institutional logos.",
                         "RGB PDF; no PDF/X, CMYK, or printer proof certification.",
                         "A screen preview and full-size PDF must be visually inspected."])
    (output/"preflight.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    page.get_pixmap(matrix=fitz.Matrix(0.63, 0.63), alpha=False).save(str(output/"tofu-poster-preview.png"))
    page.get_pixmap(matrix=fitz.Matrix(1.12, 1.12), alpha=False).save(str(output/"tofu-poster-detail.png"))
    # A4 contact proof preserves the entire A0 composition at 25% scale.
    proof = fitz.open()
    p = proof.new_page(width=210*MM, height=297*MM)
    p.show_pdf_page(p.rect, doc, 0)
    proof.save(output/"tofu-poster-A4-proof.pdf", garbage=4, deflate=True)
    proof.close()
    doc.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT/"output")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    scene = make_scene(Scene)
    for item in scene.items:
        if item["kind"] != "text":
            continue
        cmap = pdfmetrics.getFont(item["font"]).face.charToGlyph
        missing = sorted({char for char in item["text"] if ord(char) not in cmap})
        if missing:
            raise ValueError(f"Font {item['font']} lacks {missing!r} in {item['text']!r}")
    pdf_path = output/"tofu-poster-A0.pdf"
    draw_pdf(scene, pdf_path)
    draw_svg(scene, output/"tofu-poster-editable.svg")
    (output/"scene.json").write_text(json.dumps(scene.items, indent=2, ensure_ascii=False))
    (output/"poster-text.txt").write_text("\n".join(a["text"] for a in scene.items if a["kind"] == "text"))
    report = inspect_pdf(scene, pdf_path, output)
    print(json.dumps({k: report[k] for k in ["page_count", "dimensions_mm", "word_count", "text_overlaps", "failures"]},
                     indent=2))
    if report["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
