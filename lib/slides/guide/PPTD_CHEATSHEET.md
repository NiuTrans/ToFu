# PPTD v1 Cheatsheet (the subset you may use)

A deck is a directory: `deck.pptd` manifest + `pages/*.page` + `media/*`.
The manifest is written FOR you — you only write ONE page's YAML per call.
Geometry: the page is `{W}×{H}` px (1px = 1pt), origin top-left. Every
element carries `bounds: [x, y, w, h]`; later elements stack ABOVE earlier
ones. Theme tokens: `$bg $ink $primary $accent $muted $hairline` in any
color field; `$title $body $caption $bignum` in `content.style`.

## Page skeleton

```yaml
pageType: cover          # cover | table_of_contents | chapter | content | final
background:              # solid | gradient | image
  type: solid
  color: "$bg"
elements:
  - elementId: <unique-id>
    elementType: text|shape|line|image|icon|table
    bounds: [x, y, w, h]
    ...
```

## text

```yaml
- elementId: title
  elementType: text
  bounds: [48, 60, 800, 120]
  content:
    style: "$title"            # optional theme style
    align: [left, middle]      # [left|center|right, top|middle|bottom]
    wrap: true                 # false for single-line labels
    fontSize: 40               # overrides the style
    color: "$ink"
    lineHeight: 1.2
    letterSpacing: 2
    text: |                    # rich text: <p> <strong> <em> <u> <span style="color:$accent;font-size:20px"> <ul><li>
      <p><strong>完整短语,永不截断</strong></p>
      <p><span style="color:$muted">Supporting line</span></p>
```

## shape (no embedded text — overlay a text element)

```yaml
- elementId: rule
  elementType: shape
  bounds: [48, 200, 64, 6]
  shapeName: rect              # rect roundRect ellipse triangle diamond
                               # homePlate chevron donut star5 rightArrow
                               # leftArrow upArrow downArrow pentagon hexagon
                               # parallelogram trapezoid cross heart
                               # lightningBolt cloud + custom(viewBox+path)
  fill: {type: solid, color: "$accent"}
  # gradient: {type: gradient, gradientType: linear, angle: 90,
  #            stops: [{position: 0, color: "#00000000"}, {position: 1, color: "#000000F2"}]}
  border: {style: solid, width: 1, color: "$hairline"}   # solid|dash|dot
  shadow: {blur: 8, color: "#00000033", offset: [0, 3]}
  opacity: 0.9
  rotation: 0
```

## line (connectors, rules, curves)

```yaml
- elementId: divider
  elementType: line
  bounds: [48, 300, 1184, 2]
  viewBox: [1184, 2]
  points: "0,1 1184,1"         # first/last = endpoints, middle = bezier controls
  curve: round                 # sharp | round | smooth
  arrow: [null, arrow]         # arrowheads
  border: {style: solid, width: 1, color: "$hairline"}
```

## image

```yaml
- elementId: hero
  elementType: image
  bounds: [0, 0, 1280, 720]
  src: "media/hero.jpg"        # deck-relative; remote https:// ONLY from the provided list
  fit: {mode: cover}           # cover | contain | fill
  crop: {top: 0.1, bottom: 0.1}
  cropShape: {shapeName: roundRect, adjustments: [12000]}
```

## icon (built-in solid glyph set)

```yaml
- elementId: ic
  elementType: icon
  bounds: [48, 48, 40, 40]
  iconName: "fas:lightbulb"    # fas: lightbulb check star rocket gear shield
                               # users book arrow-right chart-line globe heart
                               # flag lock search
  fill: {type: solid, color: "$accent"}
```

## table

```yaml
- elementId: spec
  elementType: table
  bounds: [80, 160, 1120, 300]
  columnWidths: [0.3, 0.35, 0.35]   # each ∈[0,1], sums to 1
  rowHeights: [0.25, 0.25, 0.25, 0.25]
  style: "$default"
  rows:
    - - text: "指标"           # header row (styled by $default)
      - text: "2024"
      - text: "2025"
    - - text: "营收"
      - text: "82.5"
      - {text: "96.3", rowSpan: 1, colSpan: 1, fill: {type: solid, color: "$accent"}}
```

## chart (bar/column/line/pie — native chart in the PPTX)

```yaml
- elementId: sales
  elementType: chart
  bounds: [80, 140, 600, 380]
  chartType: column             # column | bar(horizontal) | line | pie
  data:
    categories: [Q1, Q2, Q3, Q4]
    series:
      - {name: 营收, values: [82, 96, 88, 104], color: "$primary"}
      - {name: 利润, values: [12, 15, 13, 19]}   # color 缺省走主题轮转
  options: {legend: true, dataLabels: false}    # legend 缺省:多系列才显示
```

`values` 数量必须等于 `categories` 数量。图表系列色只用主题色板
($primary/$accent/$muted/$ink),网格线/边框已由导出器按设计纪律处理。

## Hard rules (the validator enforces these)

1. `bounds` fully inside the page (bleed allowed only for background images).
2. Every `elementId` unique within the page.
3. `$token` must exist: colors = bg/ink/primary/accent/muted/hairline,
   textStyles = title/body/caption/bignum, tableStyles = default.
4. Text is a COMPLETE phrase — rewrite to fit, never truncate mid-thought.
5. `columnWidths`/`rowHeights` sum to 1 (±0.02), one rowHeights entry per row.
6. Charts use the `chart` element (bar/column/line/pie); complex
   structural diagrams are still built from shapes/lines.
