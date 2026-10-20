"""English A0 composition: research workspace, context, programs and TofuDB.

The exporter supplies Scene; this file owns every visible label and position.
Coordinates are millimetres and font sizes are physical points.
"""

INK = "#142C49"
BLUE = "#285D9C"
CYAN = "#EAF3FA"
MUTED = "#506278"
RULE = "#CBD7E1"
WHITE = "#FFFFFF"
GOLD = "#F5CE72"
PALE_GOLD = "#FFF7E5"
SOFT = "#F4F7FA"


def make_scene(Scene):
    s = Scene()
    s.rect(0, 0, 841, 1189, WHITE)
    s.rect(0, 0, 841, 5, BLUE)

    # Institutional masthead. Official marks retain their original colors.
    s.asset("neu-faculty-logo.png", 32, 18, 200, 59)
    s.asset("niutrans-avatar.png", 327, 21, 47, 47)
    s.text("NiuTrans", 386, 42, 38, "bold", BLUE)
    s.text("Research", 386, 58, 25, color=MUTED)
    s.asset("longcat.svg", 601, 27, 205, 54)
    s.text("Northeastern University", 132, 86, 19, "medium", MUTED, align="center")
    s.text("NiuTrans Research", 421, 86, 19, "medium", MUTED, align="center")
    s.text("YourProvider LongCat RSI", 704, 86, 19, "medium", MUTED, align="center")
    s.line(32, 98, 809, 98, RULE, 0.65)

    s.logo(31, 108, 51, 59)
    s.text("ToFu", 94, 154, 108, "bold")
    s.line(218, 113, 218, 159, RULE, 0.65)
    s.text("A White-Box, Token-Efficient", 240, 129, 58, "bold")
    s.text("Agent Harness for Researchers", 240, 156, 58, "bold")
    s.text("Junhao Ruan¹²* · Yuan Ge¹* · Bei Li²+ · Yongjing Yin² · Yuchun Fan¹² · Xin Chen²",
           32, 183, 23, "medium")
    s.text("Jingang Wang² · Chenglong Wang¹³ · Jingbo Zhu¹³ · Tong Xiao¹³#",
           32, 196, 23, "medium")
    s.text("¹ Northeastern University    ² LongCat RSI, YourProvider    ³ NiuTrans Research",
           32, 210, 21, color=MUTED)
    s.text("* Equal contribution    + Project lead    # Corresponding author",
           32, 222, 15, color=MUTED, tag="credit")
    s.label("EMNLP 2026 · SYSTEM DEMO", 809, 222, BLUE, 16, align="right")
    s.line(32, 234, 809, 234, BLUE, 1.1)

    # One real product figure, with a readable explanation outside the image.
    s.label("01 / THE RESEARCH WORKSPACE", 32, 251, BLUE, 19)
    s.text("Research tools above an inspectable agent runtime", 32, 274, 40, "bold")
    s.label("PAPER READER", 32, 295, BLUE, 18)
    s.label("REQUEST INSPECTOR · CONTEXT", 514, 295, BLUE, 18)
    s.rect(32, 307, 459, 223, SOFT, RULE, 0.45)
    s.asset("paper.png", 33, 308, 457, 221.84)
    s.asset("inspector-context.png", 508, 307, 307, 202)
    s.text("Read, question, translate and write papers.", 32, 543, 25, "bold")
    s.text("Paper-reader screenshot supplied with the manuscript.", 32, 552,
           16, color=MUTED, tag="credit")
    s.text("Trace a request back to its sources.", 514, 525, 25, "bold", width=295)
    s.text("Production component · illustrative receipt", 514, 538, 17, color=MUTED, tag="credit")
    s.text("Change context and tool policies without rebuilding the research application.",
           32, 562, 26, "medium", BLUE, width=777)

    s.line(32, 573, 809, 573, RULE, 0.6)
    s.line(420, 590, 420, 805, RULE, 0.5)

    # Two concrete runtime mechanisms. Large labels stay outside the figures.
    s.label("02 / CONTEXT ENGINEERING", 32, 594, BLUE, 19)
    s.text("Keep long tasks within a token budget", 32, 617, 34, "bold", width=369)
    s.text("Three layers keep original evidence recoverable.", 32, 633, 23, width=368)
    for y, tier, title, description in [
        (651, "L0", "Externalize large tool outputs", "A preview now; full evidence by reference."),
        (682, "L1", "Trim cold tool results", "Reduce the request without rewriting history."),
        (713, "L2", "Summarize older context", "Commit the summary and preserve its archive."),
    ]:
        s.rect(32, y-1, 23, 23, CYAN)
        s.text(tier, 43.5, y+14, 24, "bold", BLUE, align="center")
        s.text(title, 65, y+8, 25, "bold", width=337)
        s.text(description, 65, y+20, 21, color=MUTED, width=337)
    s.label("DESIGN CHOICE", 32, 757, BLUE, 17)
    s.text("A shorter prompt can cost more if it breaks a warm cache. "
           "Weigh future savings against summary and cache-rebuild costs.",
           32, 772, 25, width=367, leading=11.4)

    s.label("03 / PROGRAMMATIC TOOL USE", 443, 594, BLUE, 19)
    s.text("Process data before it hits the model", 443, 617, 33, "bold", width=366)
    s.text("Discover tools, run code, return selected evidence.", 443, 633, 23, width=366)
    s.rect(443, 651, 366, 83, INK)
    s.label("execute_program · illustrative aggregation", 455, 665, "#A9CEE8", 18)
    code = [
        ('const rows = await tools.invoke(name, args);', 0),
        ('return rows', 0),
        ('.filter(r => r.status === "failed")', 10),
        ('.map(r => ({ id: r.id, error: r.error }));', 10),
    ]
    for i, (line, indent) in enumerate(code):
        s.text(line, 455+indent, 681+i*11, 22, "mono", WHITE, width=342-indent, tag="code")
    s.text("Full tool data", 443, 748, 19, "medium", MUTED)
    s.arrow(534, 745, 558, 745, BLUE, 0.6)
    s.text("Isolated JavaScript", 567, 748, 19, "medium", BLUE)
    s.arrow(689, 745, 709, 745, BLUE, 0.6)
    s.text("Evidence", 718, 748, 19, "medium", MUTED)
    s.label("DESIGN CHOICE", 443, 768, BLUE, 17)
    s.text("Enable code explicitly for aggregation. Hand semantic judgments "
           "back to the model; every tool call keeps its permission checks.",
           443, 783, 25, width=366, leading=11.4)

    # TofuDB: a mechanism and its exact work count, not an agent leaderboard.
    s.rect(0, 826, 841, 278, SOFT)
    s.line(32, 826, 809, 826, BLUE, 1.1)
    s.label("04 / TOFUDB", 32, 847, BLUE, 19)
    s.text("Long tasks should not repeatedly reread their past.", 32, 873, 41, "bold")
    s.text("TofuDB moves context reconstruction into native, agent-aware storage operations. "
           "It removes repeated JSON conversion and can reuse a verified history prefix.",
           32, 892, 25, width=777, leading=11.4)

    s.text("Continue from a certified prefix", 32, 927, 28, "bold")
    s.text("N retained turns", 32, 942, 19, color=MUTED)
    s.text("2 new turns", 360, 942, 19, color=MUTED)
    s.rect(32, 951, 282, 27, "#DCE8F3")
    # Segments are schematic; no encoded latency or storage-size measurement.
    for x in [70, 108, 146, 184, 222, 260]:
        s.line(x, 951, x, 978, WHITE, 1)
    s.rect(44, 954, 235, 20, "#DCE8F3")
    s.text("Previously verified history", 162, 969, 23, "medium", BLUE, align="center")
    for x, label, fill, color in [
        (319, "N", BLUE, WHITE), (360, "+1", GOLD, INK), (401, "+2", GOLD, INK)
    ]:
        s.rect(x, 951, 34, 27, fill)
        s.text(label, x+17, 969, 25, "bold", color, align="center")
    s.line(319, 982, 319, 987, BLUE, 0.7)
    s.line(319, 987, 435, 987, BLUE, 0.7)
    s.line(435, 982, 435, 987, BLUE, 0.7)
    s.text("Reuse the stored proof", 32, 1000, 22, "medium", BLUE)
    s.text("3 header checks", 377, 1002, 26, "bold", BLUE, align="center")

    s.line(484, 921, 484, 1006, RULE, 0.5)
    s.text("Two-turn extension", 516, 927, 28, "bold")
    s.text("Retained turns", 516, 945, 20, "medium", MUTED)
    s.text("Headers checked", 807, 945, 20, "medium", MUTED, align="right")
    for y, retained in [(967, "8"), (986, "128"), (1005, "2,048")]:
        s.line(516, y+6, 809, y+6, RULE, 0.4)
        s.text(retained, 516, y, 28, "medium")
        s.text("3", 784, y, 30, "bold", BLUE, align="center")

    s.text("Why a storage layer for agents?", 32, 1033, 25, "bold")
    s.text("Context, continuations and their proofs share one owner-scoped "
           "operation, reducing work across the application/storage boundary.",
           32, 1049, 23, width=367, leading=10.7)
    s.text("A compact prompt; a durable record.", 443, 1033, 25, "bold")
    s.text("Summary, archive and receipt commit before the model switches "
           "views. Reducing context does not delete the original evidence.",
           443, 1049, 23, width=366, leading=10.7)
    s.text("Scope: verified same-epoch continuation. First certification and reopening scan the prefix; "
           "source edits invalidate reuse. Counts are header work, not end-to-end speed.",
           32, 1090, 18, color=MUTED, width=777, leading=8.5, tag="credit")

    # A usable invitation, with the code URL written as well as encoded.
    s.text("Source code & live demonstration", 32, 1134, 37, "bold")
    s.text("Run the research workbench, or make its harness your next experiment.",
           32, 1153, 24, width=673)
    s.text("github.com/NiuTrans/ToFu", 32, 1174, 28, "bold", BLUE)
    s.text("MIT licensed · Self-hosted · Multiple model providers", 350, 1174, 19, color=MUTED)
    s.qr(745, 1114, 65)
    s.text("CODE & DEMO", 777.5, 1184, 14, "medium", BLUE, align="center", tag="credit")
    return s
