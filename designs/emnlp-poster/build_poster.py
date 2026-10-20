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
INK = "#193731"
PAPER = "#FBF7EE"
MUTED = "#50645C"
RULE = "#CECFC0"
TEAL = "#1C7166"
MINT = "#C9E6D0"
PALE = "#E8EEDF"
ORANGE = "#B54C2E"
PEACH = "#F4E2D2"
WHITE = "#FFFDF7"
DARK_LINE = "#57766B"
URL = "https://github.com/NiuTrans/ToFu"

FONT_FILES = {
    "body": "Inter-400.ttf",
    "medium": "Inter-600.ttf",
    "bold": "Inter-700.ttf",
    "serif": "Newsreader-400.ttf",
    "serif_medium": "Newsreader-500.ttf",
}
FONT_STYLES = {
    "body": ("Tofu Poster Inter", 400),
    "medium": ("Tofu Poster Inter", 600),
    "bold": ("Tofu Poster Inter", 700),
    "serif": ("Tofu Poster Newsreader", 400),
    "serif_medium": ("Tofu Poster Newsreader", 500),
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

    def label(self, text, x, y, color=MUTED, size=18):
        self.text(text, x, y, size=size, font="medium", color=color, tag="label")

    def logo(self, x, y, w, h):
        self.items.append(dict(kind="logo", x=x, y=y, w=w, h=h))

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


def make_scene():
    s = Scene()
    s.rect(0, 0, WIDTH, HEIGHT, PAPER)
    s.rect(0, 0, WIDTH, 5, INK)
    s.logo(29, 22, 30, 34)
    s.text("ToFu", 68, 52, 76, "serif_medium")
    s.label("EMNLP 2026  /  SYSTEM DEMO", 561, 32, size=19)
    s.label("CURRENT SYSTEM · OCTOBER 2026", 561, 44, size=16)
    s.text("A White-Box, Token-Efficient Agent Harness for Researchers",
           29, 78, 31, "medium")

    s.text("Use the agent.", 27, 122, 96, "serif")
    s.text("Study the harness.", 27, 161, 96, "serif", ORANGE)
    s.text("A research workbench where context, tool execution, and durable state "
           "are inspectable design choices.",
           564, 110, 29, width=244, leading=13.2)
    s.text("Junhao Ruan¹²* · Yuan Ge¹* · Bei Li²+ · Yongjing Yin² · Yuchun Fan¹² · Xin Chen²",
           29, 189, 21, "medium")
    s.text("Jingang Wang² · Chenglong Wang¹³ · Jingbo Zhu¹³ · Tong Xiao¹³#",
           29, 200, 21, "medium")
    s.text("¹ Northeastern University    ² LongCat RSI, YourProvider    ³ NiuTrans Research",
           29, 215, 20, color=MUTED)
    s.line(29, 235, 812, 235, INK, 0.65)

    # Two technical stories share a reading row, without a repeated card grid.
    s.label("01   CONTEXT ENGINEERING", 29, 256, ORANGE)
    s.text("Make context a decision.", 29, 280, 42, "serif_medium")
    s.text("Choose what enters the next request.\nKeep original evidence recoverable.",
           29, 300, 27, width=366, leading=12)
    s.line(421, 252, 421, 490, RULE, 0.4)
    s.label("02   PROGRAMMABLE EXECUTION", 447, 256, ORANGE)
    s.text("Let code handle the glue.", 447, 280, 42, "serif_medium")
    s.text("Discover tools on demand. Run joins,\nfilters, and branches in isolated JavaScript.",
           447, 300, 27, width=365, leading=12)

    # Three layers of context, with deliberately non-quantitative evidence marks.
    steps = [
        (29, "Externalize", "Large outputs", PEACH, 10),
        (159, "Trim", "Cold tool results", PALE, 6),
        (289, "Summarize", "Older context", MINT, 3),
    ]
    for x, title, caption, fill, bars in steps:
        s.rect(x, 331, 113, 48, fill)
        for i in range(bars):
            bx = x+9+i*8.2
            s.rect(bx, 342, 5.2, 19, TEAL if i < 3 else MUTED)
        s.text(title, x, 394, 27, "medium")
        s.text(caption, x, 407, 21, color=MUTED)
    s.arrow(146, 354, 155, 354, MUTED, 0.55)
    s.arrow(276, 354, 285, 354, MUTED, 0.55)
    s.label("THE TRADEOFF", 29, 430, TEAL, 17)
    s.text("A smaller prompt can cost more if it breaks a warm cache. "
           "Compaction weighs future savings against summary and cache-rebuild costs.",
           29, 447, 25, width=369, leading=11.8)
    s.text("Inspect: request · injections · cache reads · cost",
           29, 488, 20, "medium", TEAL)

    # One program surrounds multiple tool calls; authority remains at each call.
    s.rect(447, 331, 365, 77, PALE)
    s.label("MODEL", 459, 346, size=16)
    s.label("ISOLATED WASM PROGRAM", 524, 346, TEAL, 16)
    s.label("MODEL", 772, 346, size=16)
    s.rect(458, 362, 42, 27, WHITE, RULE)
    s.text("Plan", 479, 379, 24, "medium", align="center")
    s.arrow(503, 376, 518, 376)
    s.rect(521, 356, 234, 39, WHITE, TEAL, 0.5)
    for x, name in [(532, "call"), (601, "filter"), (677, "return")]:
        s.text(name, x, 380, 25, "medium")
    s.arrow(574, 376, 593, 376)
    s.arrow(647, 376, 669, 376)
    s.arrow(758, 376, 770, 376)
    s.text("Read", 790, 379, 24, "medium", align="center")
    s.text("Every sub-call retains its permission checks and execution receipt.",
           447, 429, 25, width=365, leading=11.8)
    s.text("Uncertain decisions return to the model;\nfailed or partial results stop the chain.",
           447, 459, 25, width=365, leading=11.8)
    s.text("Inspect: nested calls · receipts · returned evidence",
           447, 488, 20, "medium", TEAL)

    # Main technical centerpiece: evidence/working-view separation and frontier.
    s.rect(18, 516, 805, 377, INK)
    s.label("03   AGENT-AWARE STORAGE", 39, 539, MINT)
    s.text("Why build TofuDB?", 38, 567, 61, "serif", WHITE)
    s.text("Long tasks need more than a transcript.", 39, 589, 32, color=MINT)

    s.text("The model needs a small working view.\n\n"
           "The system needs exact history, committed progress, and evidence "
           "for what can safely continue.",
           39, 622, 27, color=WHITE, width=242, leading=12.8)
    s.label("CORE IDEA", 39, 727, MINT, 17)
    s.text("Let storage verify\nwhich history\ncan be reused.",
           39, 751, 39, "serif", WHITE, leading=17)

    s.line(309, 615, 309, 792, DARK_LINE, 0.4)
    s.label("VERIFY WHAT CHANGED", 332, 621, MINT, 18)
    s.label("Certified earlier prefix", 332, 640, WHITE, 22)
    s.label("Boundary", 612, 640, WHITE, 20)
    s.label("New turns", 705, 640, WHITE, 20)
    for i in range(7):
        x = 332+i*35
        s.rect(x, 650, 29, 36, "#2F5146", DARK_LINE, 0.3)
        for y in (660, 666, 672):
            s.line(x+6, y, x+22, y, "#769087", 0.65)
    s.text("…", 585, 673, 30, color=MINT)
    s.rect(612, 650, 63, 36, MINT)
    s.text("last", 643.5, 673, 25, "medium", INK, align="center")
    for x, label in [(697, "+1"), (751, "+2")]:
        s.rect(x, 650, 45, 36, "#F0D0AF")
        s.text(label, x+22.5, 673, 25, "medium", INK, align="center")
    s.line(332, 697, 604, 697, MINT, 0.55)
    s.line(332, 692, 332, 697, MINT, 0.55)
    s.line(604, 692, 604, 697, MINT, 0.55)
    s.text("Reuse the certified prefix", 332, 712, 22, color=MINT)
    s.line(612, 697, 796, 697, "#F0D0AF", 0.55)
    s.line(612, 692, 612, 697, "#F0D0AF", 0.55)
    s.line(796, 692, 796, 697, "#F0D0AF", 0.55)
    s.text("Verify boundary + suffix", 612, 712, 22, color="#F0D0AF")

    s.text("N retained turns + 2 new", 332, 744, 29, "medium", WHITE)
    s.arrow(469, 741, 487, 741, WHITE, 0.6)
    s.text("3 header checks*", 495, 744, 29, "medium", WHITE)
    s.text("Edits to earlier task evidence invalidate the certificate.\n"
           "Reopening the database requires full revalidation.",
           332, 763, 24, color=MINT, leading=11.5, width=463)
    s.text("* After certification, until the database is reopened.\n"
           "Header counts do not establish an end-to-end speedup.",
           332, 787, 18, color=MINT, leading=8)
    s.line(39, 813, 800, 813, DARK_LINE, 0.4)
    s.label("WHY NATIVE?", 39, 830, MINT, 17)
    s.text("Remove repeated history scans and JSON copies across the "
           "storage/application boundary.",
           39, 847, 25, color=WHITE, width=351, leading=11.8)
    s.label("THE COST", 445, 830, MINT, 17)
    s.text("Own recovery, migrations and fault testing.\n"
           "SQLite remains a supported, meaningful baseline.",
           445, 847, 25, color=WHITE, width=355, leading=11.8)
    s.text("Native TofuDB, SQLite and PostgreSQL share a semantic storage contract.",
           39, 881, 21, color=MINT)

    # Closing row expresses audience fit as a concrete research workflow.
    s.label("04   A WORKBENCH YOU CAN EXPERIMENT ON", 29, 923, ORANGE)
    s.text("Bring a research question. Change the system.", 29, 949, 46, "serif_medium")
    stages = [
        (29, "Read a paper", "Grounded Q&A\nin the paper reader"),
        (231, "Inspect a run", "Actual requests,\ncontext and tool traces"),
        (433, "Change a policy", "Context, models,\ntools or orchestration"),
        (635, "Check the result", "Inspect evidence;\nverify with tests"),
    ]
    for index, (x, title, caption) in enumerate(stages):
        s.line(x, 966, x+177, 966, TEAL, 1.0)
        s.label(f"0{index+1}", x, 983, TEAL, 17)
        s.text(title, x, 1000, 29, "medium")
        s.text(caption, x, 1017, 24, color=MUTED, leading=11.5)
        if index < 3:
            s.arrow(x+181, 999, x+194, 999, TEAL, 0.6)
    s.text("Ask us to inspect a live request—and change its context policy.",
           29, 1061, 31, "serif_medium")

    s.line(29, 1085, 812, 1085, INK, 0.65)
    s.text("Try Tofu. Bring your model.", 29, 1118, 46, "serif_medium")
    s.text("github.com/NiuTrans/ToFu", 29, 1139, 29, "medium", TEAL)
    s.text("MIT licensed · Self-hosted · Model adapters · MCP + skills",
           29, 1157, 22, color=MUTED)
    s.text("* Equal contribution    + Project leader    # Corresponding author",
           29, 1175, 15, color=MUTED, tag="credit")
    s.qr(739, 1097, 69)
    s.label("CODE + GET STARTED", 739, 1174, size=13)
    return s


def rgb(value):
    return tuple(int(value[i:i+2], 16)/255 for i in (1, 3, 5))


def draw_pdf(scene, destination):
    c = canvas.Canvas(str(destination), pagesize=(WIDTH*MM, HEIGHT*MM),
                      pageCompression=1, initialFontName="body", initialFontSize=27)
    c.setTitle("ToFu — Use the agent. Study the harness.")
    c.setAuthor("Junhao Ruan, Yuan Ge, Bei Li and collaborators")
    c.setSubject("EMNLP 2026 current-system poster: context, execution, and TofuDB")
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
    c.linkURL(URL, (29*MM, (HEIGHT-1160)*MM, 490*MM, (HEIGHT-1124)*MM), relative=0)
    c.linkURL(URL, (739*MM, (HEIGHT-1166)*MM, 808*MM, (HEIGHT-1097)*MM), relative=0)
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
    doc.subset_fonts()
    temporary = destination.with_suffix(".final.pdf")
    doc.save(temporary, garbage=4, deflate=True)
    doc.close()
    logo.close()
    temporary.replace(destination)


def draw_svg(scene, destination):
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}mm" '
           f'height="{HEIGHT}mm" viewBox="0 0 {WIDTH} {HEIGHT}" role="img">'
           '<title>ToFu: Use the agent. Study the harness.</title>'
           '<desc>English A0 poster about inspectable context, programmable tool '
           'execution, and TofuDB incremental continuation. All prose is editable text.</desc>',
           "<defs><style>"]
    for name, filename in FONT_FILES.items():
        data = base64.b64encode((ASSETS/filename).read_bytes()).decode()
        out.append(f"@font-face{{font-family:'tofu_{name}';src:url(data:font/ttf;base64,{data}) "
                   f"format('truetype');font-weight:{FONT_STYLES[name][1]};}}")
    out.append("</style></defs>")
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
    for anchor in ["Use the agent.", "Study the harness.", "Why build TofuDB?",
                   "3 header checks", "SQLite remains a supported",
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
                  notes=["Vector figures and mascot; no scientific data rasterization.",
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
    scene = make_scene()
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
