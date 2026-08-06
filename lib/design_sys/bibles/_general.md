# General Design Law (all scenarios)

> Distilled from the open-kimi-ppt-skill reference corpus (MIT) and adapted
> for Tofu's slides + motion_video capabilities. These rules apply to EVERY
> page/scene before any scenario bible does.

## Every page has a reader task

Before designing a page, finish this sentence: "after this page, the reader
understands / believes / decides / does X". A page without an answer is
decoration. The page TITLE states the conclusion or the question — never a
bare section label like "Overview" or "Background".

## Rhythm across the artefact

Alternate dense evidence pages with breathing pages (cover, section divider,
big-number hero, conclusion). Two consecutive pages with the same skeleton
read as a template; two consecutive empty pages read as unfinished. End by
answering the opening question — never with a lone "Thank you".

## Universal prohibitions (hard rules)

1. **No card walls.** Rounded-rectangle / bordered cards used to build
   hierarchy or alignment are the #1 AI tell. Use whitespace, thin rules,
   and font-size/weight contrast instead. A card is legal only when it IS
   the object (a ticket, a product SKU), not as a container for everything.
2. **No evenly-divided default layouts.** No one-third columns, no 2×2
   matrices, no "title + three parallel blocks + conclusion" formula — unless
   no other layout can express the relationship.
3. **No formulaic AI palettes.** Forbidden unless the user explicitly asks:
   blue-white corporate pairing, blue-purple gradients, cyan-purple neon,
   rainbow charts, glassmorphism, glowing borders.
4. **No style mixing.** Within one artefact, corners (sharp OR rounded),
   icon stroke system, and decorative grammar stay consistent. A rounded
   icon inside a sharp design is a defect.
5. **One accent color.** Everything else is the structural color, ink, and
   neutrals. The accent appears at most once per page — a number, a bar, a
   word, a node on the critical path.

## Evidence discipline

- Never fabricate data, citations, cases, or sources. Missing material is
  marked as a placeholder / assumption / to-be-supplied — visibly.
- Every external fact carries its source close by (small source line, fixed
  position). Charts are self-standing: axes, units, legend, source,
  measurement basis.
- De-default every chart: restyle series into the theme palette, remove
  heavy frames and gridlines, label key values directly on the chart.

## Type discipline

- Display type and body type have a division of labor (family or weight);
  hierarchy comes from size and weight ratios, never from adding more colors.
- Functional text (numbers, labels, sources, page marks) is set small, often
  with widened letter spacing.
- CJK text uses the staged faces ONLY — the @font-face families handed to
  you. Naming an unstaged family does not get you that family; it silently
  falls back to whatever the host has.
- A headline is a complete phrase. When copy does not fit, REWRITE it shorter
  — never truncate mid-thought, never shrink the hero below its role size.

## Imagery

- A real photograph at the right place beats any decoration (full-bleed
  cover, section divider, evidence). Abuse is forbidden: an image must carry
  information or establish a situation.
- Decorative imagery lives on covers / dividers / closing pages only, never
  crowding body content. Text over imagery demands a scrim (solid or
  gradient overlay) for contrast.

## Motion (video consumers)

- Everything arriving at once is the amateur tell: stagger sibling entrances
  0.08–0.15 s, each element its own offset.
- The accent color is allowed to be the thing that moves: a drawing rule, a
  counting number, a growing bar.
- After the last entrance, keep a slow drift — a frozen frame reads as a bug.
