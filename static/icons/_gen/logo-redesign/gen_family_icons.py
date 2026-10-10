"""Generate the tofu role-icon family from the A2 base (commit 13f8adee design).

One shared hand-authored cube (identical geometry/gradients/rounded silhouette/
face to the shipped tofu-welcome.svg) + a small flat prop overlay per role.
Props keep the current family's identity (beret, hard hat, monocle, ...) but
are redrawn in the same geometric language: #221E2A outlines (~1.6), flat
fills, rounded joins, 1-2 accent colors.

Output: static/icons/_gen/logo-redesign/family/tofu-<role>.svg
"""
import os

INK = '#221E2A'
CREAM_HAND = '#FDF2DA'

DEFS = """<defs>
    <linearGradient id="sTop" x1="0" y1="0" x2="0.55" y2="1">
      <stop offset="0" stop-color="#FFFAEE"/><stop offset="1" stop-color="#FBEFD4"/>
    </linearGradient>
    <linearGradient id="sLeft" x1="0" y1="0" x2="0.25" y2="1">
      <stop offset="0" stop-color="#FCF3DE"/><stop offset="1" stop-color="#F5E3C1"/>
    </linearGradient>
    <linearGradient id="sRight" x1="0" y1="0" x2="1" y2="0.7">
      <stop offset="0" stop-color="#EFDAB7"/><stop offset="1" stop-color="#E3C79B"/>
    </linearGradient>
  </defs>"""

CUBE = """<path d="M32 7 L56 19 L32 31 L8 19 Z" fill="url(#sTop)"/>
  <path d="M8 19 L32 31 L32 55 L8 43 Z" fill="url(#sLeft)"/>
  <path d="M32 31 L56 19 L56 43 L32 55 Z" fill="url(#sRight)"/>"""

SHEEN = """<path d="M15.5 15.6 L25.5 10.6" stroke="#FFFFFF" stroke-width="2.2" stroke-linecap="round" opacity="0.45"/>
  <path d="M27.8 9.4 L30.2 8.2" stroke="#FFFFFF" stroke-width="2.2" stroke-linecap="round" opacity="0.45"/>"""

OUTLINE = ("""<path d="M29.32 8.34 Q32 7 34.68 8.34 L53.32 17.66 Q56 19 56 22 L56 40 """
           """Q56 43 53.32 44.34 L34.68 53.66 Q32 55 29.32 53.66 L10.68 44.34 Q8 43 8 40 """
           """L8 22 Q8 19 10.68 17.66 Z M8 19 L32 31 L56 19 M32 31 L32 55" fill="none" """
           f"""stroke="{INK}" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round"/>""")

EYES = f"""<rect x="4.42" y="6.35" width="5.25" height="7.9" rx="2.6" fill="{INK}"/>
    <rect x="14.32" y="6.35" width="5.25" height="7.9" rx="2.6" fill="{INK}"/>
    <circle cx="8.05" cy="8.8" r="1.2" fill="#FFFFFF"/>
    <circle cx="17.94" cy="8.8" r="1.2" fill="#FFFFFF"/>"""

MOUTH_W = f"""<path d="M9.7 15.6 Q11 17.2 12 15.3 Q13 17.2 14.3 15.6"
      fill="none" stroke="{INK}" stroke-width="1.3" stroke-linecap="round"/>"""

MOUTH_GRIN = f"""<rect x="9.2" y="14.8" width="5.6" height="3.4" rx="1.7"
      fill="#FFFFFF" stroke="{INK}" stroke-width="1.3"/>"""

MOUTH_FLAT = f"""<path d="M9.6 16.1 L14.4 16.1" stroke="{INK}" stroke-width="1.3" stroke-linecap="round"/>"""

BROW_L = f"""<path d="M4.3 4.6 L8.3 3.4" stroke="{INK}" stroke-width="1.2" stroke-linecap="round"/>"""
BROW_R = f"""<path d="M15.7 3.4 L19.7 4.6" stroke="{INK}" stroke-width="1.2" stroke-linecap="round"/>"""
BROW_RAISED_L = f"""<path d="M4.2 3.6 L8.4 4.8" stroke="{INK}" stroke-width="1.2" stroke-linecap="round"/>"""

BLUSH = """<ellipse cx="3.6" cy="17.4" rx="2.3" ry="1.35" fill="#FB9E96" opacity="0.7"/>
    <ellipse cx="20.4" cy="17.4" rx="2.3" ry="1.35" fill="#FB9E96" opacity="0.7"/>"""

MONOCLE = """<circle cx="16.95" cy="10.3" r="5" fill="#BFE3EA" opacity="0.55"/>
    <circle cx="16.95" cy="10.3" r="5" fill="none" stroke="#C9A227" stroke-width="1.7"/>
    <path d="M21.4 13.2 L23.5 21" stroke="#C9A227" stroke-width="1.2" stroke-linecap="round"/>"""

GLASSES = f"""<circle cx="7.05" cy="10.3" r="3.9" fill="#DCEEF5" opacity="0.45"/>
    <circle cx="16.95" cy="10.3" r="3.9" fill="#DCEEF5" opacity="0.45"/>
    <circle cx="7.05" cy="10.3" r="3.9" fill="none" stroke="{INK}" stroke-width="1.4"/>
    <circle cx="16.95" cy="10.3" r="3.9" fill="none" stroke="{INK}" stroke-width="1.4"/>
    <path d="M10.95 10.3 L13.05 10.3" stroke="{INK}" stroke-width="1.4"/>"""


def face(mouth=MOUTH_W, extra=''):
    return (f'<g transform="matrix(1,0.5,0,1,8,19)">{EYES}{extra}{mouth}{BLUSH}</g>')


# ── Prop overlays (screen coords, drawn AFTER the cube+face) ─────────
P = {}
P['planner'] = f"""<g transform="rotate(-16 24 12)"><ellipse cx="24" cy="12" rx="9" ry="4.8" fill="#3E4C8C" stroke="{INK}" stroke-width="1.6"/><circle cx="24" cy="6.6" r="1.4" fill="#3E4C8C" stroke="{INK}" stroke-width="1.2"/></g>
  <g transform="rotate(-10 6 44)"><rect x="0.5" y="38" width="10" height="12" rx="1.5" fill="#C89B6A" stroke="{INK}" stroke-width="1.6"/><rect x="2.2" y="39.8" width="6.6" height="8.6" rx="0.8" fill="#FFFFFF"/><path d="M3.6 42.4 L7.4 42.4 M3.6 44.6 L7.4 44.6 M3.6 46.8 L6.2 46.8" stroke="#9AA3AD" stroke-width="0.9" stroke-linecap="round"/><rect x="3.4" y="36.6" width="4.2" height="2.4" rx="1.2" fill="#8A94A0" stroke="{INK}" stroke-width="1"/></g>
  <ellipse cx="12.5" cy="47" rx="3" ry="2.2" fill="{CREAM_HAND}" stroke="{INK}" stroke-width="1.3"/>"""
P['worker'] = f"""<path d="M25.5 15.5 A9.5 8.5 0 0 1 44.5 15 L44.5 16.5 L25.5 17.5 Z" fill="#F5C542" stroke="{INK}" stroke-width="1.6"/>
  <ellipse cx="35" cy="17" rx="11.5" ry="3.4" fill="#F5C542" stroke="{INK}" stroke-width="1.6"/>
  <path d="M35 6.2 L35 12" stroke="{INK}" stroke-width="1.4" stroke-linecap="round"/>
  <g transform="rotate(32 52 44)"><path d="M49.4 40.5 A3.2 3.2 0 1 0 54.6 41.7 L52.6 44.2 L54 46.4 L49 49.4 L47.6 47 L49.6 44.5 A3.2 3.2 0 0 0 49.4 40.5 Z" fill="#9AA3AD" stroke="{INK}" stroke-width="1.4"/></g>"""
P['critic'] = f"""<g><circle cx="50" cy="46" r="4.4" fill="#E8E2D4" stroke="#C9A227" stroke-width="1.7"/><path d="M50 43.6 L50 46 L52 47" stroke="{INK}" stroke-width="1" stroke-linecap="round"/><rect x="48.9" y="40.2" width="2.2" height="1.8" rx="0.9" fill="#C9A227"/><path d="M46.5 42.5 Q44 41 43 43.5" fill="none" stroke="#C9A227" stroke-width="1.1"/></g>"""
P['thinking'] = f"""<path d="M37.5 8.5 A3.6 3.4 0 0 1 44.5 5.6 A4.4 4 0 0 1 53.5 7.4 A3.4 3.2 0 0 1 52 14 L39 14 A3.2 3 0 0 1 37.5 8.5 Z" fill="#FDF3DC" stroke="{INK}" stroke-width="1.5"/>
  <circle cx="41.5" cy="9.8" r="1.05" fill="{INK}"/><circle cx="45.5" cy="9.8" r="1.05" fill="{INK}"/><circle cx="49.5" cy="9.8" r="1.05" fill="{INK}"/>
  <circle cx="35" cy="16.5" r="1.3" fill="#FDF3DC" stroke="{INK}" stroke-width="1.1"/><circle cx="32.5" cy="19.5" r="0.9" fill="#FDF3DC" stroke="{INK}" stroke-width="1"/>"""
P['browser'] = f"""<g><rect x="41" y="36" width="20" height="14.5" rx="1.8" fill="#FFFFFF" stroke="{INK}" stroke-width="1.6"/><path d="M41 37.8 L41 36.8 Q41 36 41.8 36 L60.2 36 Q61 36 61 36.8 L61 37.8 Z" fill="#8A94A0"/><rect x="41" y="40" width="20" height="1" fill="{INK}" opacity="0.18"/><circle cx="43.4" cy="37.9" r="0.9" fill="#F26D6D"/><circle cx="46.2" cy="37.9" r="0.9" fill="#F5C542"/><circle cx="49" cy="37.9" r="0.9" fill="#6FCF6F"/><circle cx="51" cy="45.4" r="4.2" fill="none" stroke="#4A7BD4" stroke-width="1.3"/><path d="M46.8 45.4 L55.2 45.4 M51 41.2 A8.5 8.5 0 0 0 51 49.6 M51 41.2 A8.5 8.5 0 0 1 51 49.6" stroke="#4A7BD4" stroke-width="1" fill="none"/></g>
  <ellipse cx="40" cy="47" rx="3" ry="2.2" fill="{CREAM_HAND}" stroke="{INK}" stroke-width="1.3"/>"""
P['researcher'] = f"""<g transform="rotate(-8 6 44)"><circle cx="5" cy="43" r="4.6" fill="#BFE3EA" opacity="0.6"/><circle cx="5" cy="43" r="4.6" fill="none" stroke="{INK}" stroke-width="1.6"/><path d="M8.4 46.4 L11.5 49.5" stroke="{INK}" stroke-width="2" stroke-linecap="round"/></g>
  <g><path d="M43 46 L49.5 43.5 L49.5 52 L43 54.5 Z" fill="#FFFFFF" stroke="{INK}" stroke-width="1.5"/><path d="M49.5 43.5 L56 46 L56 54.5 L49.5 52 Z" fill="#F4EFE5" stroke="{INK}" stroke-width="1.5"/><path d="M45 47.5 L47.5 46.6 M45 49.6 L47.5 48.7 M51.5 46.6 L54 47.5 M51.5 48.7 L54 49.6" stroke="#9AA3AD" stroke-width="0.8" stroke-linecap="round"/></g>"""
P['analyst'] = f"""<g><rect x="45" y="39" width="11" height="13" rx="1.2" fill="#FFFFFF" stroke="{INK}" stroke-width="1.5"/><path d="M53 39 L56 42 L53 42 Z" fill="#E3E1DA" stroke="{INK}" stroke-width="1"/><rect x="46.8" y="46.5" width="1.7" height="3.4" fill="#F26D6D"/><rect x="49.4" y="44.8" width="1.7" height="5.1" fill="#F5C542"/><rect x="52" y="43.2" width="1.7" height="6.7" fill="#4A7BD4"/><path d="M46.8 41.6 L52.5 41.6" stroke="#9AA3AD" stroke-width="0.8" stroke-linecap="round"/></g>"""
P['coder'] = f"""<g><rect x="0" y="37" width="20" height="14" rx="2" fill="#2A2733" stroke="{INK}" stroke-width="1.6"/><circle cx="3" cy="39.6" r="0.7" fill="#8A94A0"/><circle cx="5.4" cy="39.6" r="0.7" fill="#8A94A0"/><path d="M4 43.5 L7 45.5 L4 47.5" fill="none" stroke="#7EE787" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M9 48.2 L13.5 48.2" stroke="#7EE787" stroke-width="1.5" stroke-linecap="round"/></g>
  <ellipse cx="21" cy="48" rx="3" ry="2.2" fill="{CREAM_HAND}" stroke="{INK}" stroke-width="1.3"/>"""
P['general'] = f"""<path d="M52.5 4.5 L54.2 8.6 L58.3 10.3 L54.2 12 L52.5 16.1 L50.8 12 L46.7 10.3 L50.8 8.6 Z" fill="#F5C542" stroke="{INK}" stroke-width="1.3" stroke-linejoin="round"/>
  <path d="M58.5 14.5 L59.3 16.4 L61.2 17.2 L59.3 18 L58.5 19.9 L57.7 18 L55.8 17.2 L57.7 16.4 Z" fill="#F5C542" stroke="{INK}" stroke-width="1" stroke-linejoin="round"/>"""
P['router'] = f"""<g><rect x="52.4" y="26" width="2.6" height="22" fill="#A97B4F" stroke="{INK}" stroke-width="1.4"/><path d="M53.7 22 L59.5 22 L62.3 24.6 L59.5 27.2 L46 27.2 L46 22 Z" fill="#C89B6A" stroke="{INK}" stroke-width="1.4" stroke-linejoin="round"/><path d="M53.7 30 L46.8 30 L44 32.6 L46.8 35.2 L59.5 35.2 L59.5 30 Z" fill="#C89B6A" stroke="{INK}" stroke-width="1.4" stroke-linejoin="round"/></g>"""
P['synthesizer'] = f"""<g><path d="M43 38 L57 38 L52 45.5 L52 51 L48 51 L48 45.5 Z" fill="#FDF3DC" stroke="{INK}" stroke-width="1.6" stroke-linejoin="round"/><path d="M44 32 L46.6 35 M56 32 L53.4 35" stroke="{INK}" stroke-width="1.3" stroke-linecap="round"/><circle cx="50" cy="54" r="1.4" fill="#EFDAB7" stroke="{INK}" stroke-width="1"/></g>"""
P['writer'] = f"""<g transform="rotate(-7 7 45)"><rect x="1.5" y="39.5" width="10.5" height="12.5" rx="1.2" fill="#FFFFFF" stroke="{INK}" stroke-width="1.5"/><path d="M3.5 43 L10 43 M3.5 45.6 L10 45.6 M3.5 48.2 L8 48.2" stroke="#9AA3AD" stroke-width="0.9" stroke-linecap="round"/></g>
  <g transform="rotate(38 14 43)"><rect x="12.6" y="36" width="2.8" height="9" fill="#F5C542" stroke="{INK}" stroke-width="1.2"/><path d="M12.6 45 L14 48.4 L15.4 45 Z" fill="#E8C9A0" stroke="{INK}" stroke-width="1.1"/><path d="M13.5 47.5 L14 48.4 L14.5 47.5" fill="{INK}"/></g>"""

FACES = {
    'planner': face(), 'worker': face(mouth=MOUTH_GRIN, extra=BROW_L + BROW_R),
    'critic': face(mouth=MOUTH_FLAT, extra=BROW_RAISED_L + MONOCLE),
    'thinking': face(), 'browser': face(), 'researcher': face(),
    'analyst': face(extra=GLASSES), 'coder': face(), 'general': face(),
    'router': face(), 'synthesizer': face(), 'writer': face(),
}
NO_SHEEN = {'planner', 'worker'}  # hats cover the sheen area

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'family')
os.makedirs(OUT_DIR, exist_ok=True)
for role, prop in P.items():
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">\n  {DEFS}\n  {CUBE}\n'
           + ('  ' + SHEEN + '\n' if role not in NO_SHEEN else '')
           + f'  {OUTLINE}\n  {FACES[role]}\n  {prop}\n</svg>\n')
    out = os.path.join(OUT_DIR, f'tofu-{role}.svg')
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write(svg)
    print(f'{role}: {len(svg)}B')
print(f'wrote {len(P)} icons to {OUT_DIR}')
