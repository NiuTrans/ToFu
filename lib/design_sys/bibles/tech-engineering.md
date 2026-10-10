# Tech & Engineering Bible

> Architecture reviews, platform proposals, incident retrospectives, tech
> selection, AI/data/ops/security. Readers may be engineers OR product
> partners: they must see how the system works, judge whether the solution is
> good, and read trade-offs, risks, and next steps clearly.

## Core character

- **Professional**: no empty "high availability" slogans. Write the metrics,
  environment, boundaries, dependencies, failure conditions, recovery paths.
- **Concrete**: architecture diagrams show call directions, data flow,
  control flow, and trust/failure boundaries. Code/config/logs appear only
  when they genuinely support the argument.
- **Evidence first**: every page advances ONE judgment. Title = conclusion,
  body = evidence, and what the evidence MEANS for the decision.
- **Restrained**: the wow is a complex problem made clear — never glow,
  particles, card walls, or hollow "tech vibes".

## Narrative skeletons (pick ONE primary)

| Task | Order |
|---|---|
| Architecture review | Goals & constraints → current state → problems → alternatives → target architecture → migration & validation |
| AI/data platform | User tasks → data/model pipeline → core mechanisms → evaluation evidence → risk guardrails → launch loop |
| Security review | Assets & boundaries → threats → attack paths → controls → residual risk → monitoring |
| Incident retrospective | Impact → timeline → direct cause → systemic factors → fixes → prevention validation |
| Tech selection | Goals & criteria → candidates → same-scale comparison → trade-offs → recommendation → exit conditions |

## Diagrams (the relationship decides the graphic)

- System regions/boundaries → right-angled nested regions; calls/data flow →
  directed node chains labeled with protocol & payload; multi-role → sequence
  diagrams; deployment/failure domains → topology; state → state machines.
- Arrows have direction AND meaning (protocol, frequency, trigger). Data vs
  control vs exception paths use stable, distinct grammars (color + dash +
  label redundancy).
- Connectors terminate at node edges, never cross text. Critical path gets
  the accent color or a bolder stroke; everything else recedes to neutral.
- Complex architectures: overview first, then zoom — keep naming, colors,
  and coordinate cues IDENTICAL across zoom levels.

## Charts, tables, code

- Trends → lines; comparisons → bars or dot plots; composition → stacks;
  latency distributions → histogram/box/quantile; no 3D, no default frames,
  no heavy gridlines. Main series in the structural color; baselines in gray.
- Key values, inflections, anomalies labeled directly on the chart with
  "why" + "what it means". Test environment, version, load, sample, units,
  baseline sit beside the chart.
- Tables: thin horizontal separators only; numbers right-aligned, text left;
  header dark with light text; no zebra, no thick frames.
- Code/config monospace, minimal excerpt, key lines highlighted; screenshots
  cropped tight to the evidence boundary, right-angled, no device frames.

## Type & color

- Highly legible sans for titles/body (MiSans stack); restrained serif OK
  when a document feel is needed; monospace for code. ≤ 2 families per deck.
- Light backgrounds for review/print; a restrained dark ground is legal for
  launch-style presentation. Status colors fixed: each color carries ONE
  stable meaning, redundantly encoded with text/line-style.
- Body surfaces clean: no noise, particles, grid-light, glass, gradients.

## Motion notes (video)

- Preferred archetypes: step chain (process), split frame (comparison),
  stat card (metrics). Diagram builds should ANIMATE ALONG the flow direction
  (a packet traveling the critical path earns the accent color).
- Reveal evidence in argument order: normal path first, then exception
  paths, then the fix — not all at once.

## Pre-delivery checklist

- Inputs/outputs/dependencies/boundaries/normal+exception+fallback paths clear?
- Metrics carry environment, version, sample, load, units, baseline, window?
- One main judgment per page; evidence actually supports the title?
- No unsourced data, default Office charts, card walls, decorative tech-vibe?
