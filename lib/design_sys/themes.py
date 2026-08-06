"""lib/design_sys/themes.py — per-scenario themes + design bibles.

What this module is FOR (docs/SLIDES_CAPABILITY_DESIGN.md §3.2): the single
biggest measured quality gap between our films and a designer deck was not
rendering — it was that every scene/page invented its own palette and type
choices, so the artefact had no THEME. Here one film/deck gets one Theme:
palette + font pairing + the scenario's design bible, chosen ONCE at recipe
time and injected into every scene/page author verbatim.

The palettes are not invented here: they are distilled from the ported design
bibles (``bibles/*.md``, adapted from open-kimi-ppt-skill's reference corpus,
MIT), which themselves encode "colors expected in spirit, unexpected in
choice" — every scenario rejects its formulaic defaults (tech blue-purple,
hospital blue-white, festive red-gold) in favour of editorial pairings.

Two consumers, one contract:

  * motion_video ``_recipe``/``_scene_author`` — film-level theme block;
  * slides ``recipe``/page author — deck-level theme block.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['Theme', 'SCENARIOS', 'THEMES', 'BIBLE_INDEX', 'classify_scenario',
           'get_theme', 'list_themes', 'default_theme_id', 'design_bible_text',
           'theme_prompt_block']


# ── Scenarios ─────────────────────────────────────────────

SCENARIOS: dict = {
    'tech-engineering': {
        'label': '科技/工程',
        'reader_task': '看清结构、依赖、指标与取舍',
        'bible': 'tech-engineering.md',
    },
    'analysis-decision': {
        'label': '分析/决策',
        'reader_task': '比较选项、形成判断、支撑决策',
        'bible': 'analysis-decision.md',
    },
    'business-plan': {
        'label': '商业提案',
        'reader_task': '理解价值、相信方案、采取行动',
        'bible': 'business-plan.md',
    },
    'management-report': {
        'label': '管理汇报',
        'reader_task': '掌握现状、暴露问题、确认行动',
        'bible': 'management-report.md',
    },
    'academic-research': {
        'label': '学术研究',
        'reader_task': '评审问题、方法、证据与贡献',
        'bible': 'academic-research.md',
    },
    'education-training': {
        'label': '教育培训',
        'reader_task': '理解、记住、能复述、会操作',
        'bible': 'education-training.md',
    },
    'brand-creative': {
        'label': '品牌/创意',
        'reader_task': '建立感知、留下记忆、形成认同',
        'bible': 'brand-creative.md',
    },
}


# ── Theme registry ────────────────────────────────────────

@dataclass(frozen=True)
class Theme:
    """One coherent visual system: palette + font pairing + mood.

    ``colors`` keys are fixed: ``bg`` page ground / ``ink`` primary text /
    ``primary`` structural color (titles, skeleton, key series) /
    ``accent`` the ONE emphasis color, used once per page /
    ``muted`` secondary text / ``hairline`` rules and separators.
    ``fonts`` holds design_sys font ids for display/body/latin roles.
    """
    id: str
    scenario: str
    label: str
    colors: dict
    fonts: dict                             # {'display','body','latin'} → font id
    mood: str = ''
    dark: bool = False


THEMES: tuple = (
    # ── tech-engineering ──
    Theme(
        id='paper-engineer', scenario='tech-engineering',
        label='纸面工程(浅)',
        colors={'bg': '#F7F7F5', 'ink': '#1B2430', 'primary': '#16283C',
                'accent': '#C0652B', 'muted': '#6B7280', 'hairline': '#D8D5CE'},
        fonts={'display': 'misans', 'body': 'misans', 'latin': 'liter'},
        mood='白底黑字,暖色担标题与关键路径,冷色担结构;适合评审与打印。'),
    Theme(
        id='deep-console', scenario='tech-engineering',
        label='深空控制台(深)',
        colors={'bg': '#101418', 'ink': '#F4F7F6', 'primary': '#3FA68C',
                'accent': '#F5B700', 'muted': '#8A93A3', 'hairline': '#26303A'},
        fonts={'display': 'misans', 'body': 'misans', 'latin': 'liter'},
        mood='墨黑底+青瓷绿结构+信号黄点缀;发布会/暗厅演示。',
        dark=True),
    # ── analysis-decision ──
    Theme(
        id='ink-green-ledger', scenario='analysis-decision',
        label='墨绿账簿(浅)',
        colors={'bg': '#F7F3E8', 'ink': '#1A1A1A', 'primary': '#123B2F',
                'accent': '#B08D3E', 'muted': '#7A7568', 'hairline': '#DDD5C3'},
        fonts={'display': 'noto-serif-sc', 'body': 'noto-sans-sc',
               'latin': 'liter'},
        mood='咨询/研报骨架:宋体判断句标题+黑体证据,单一墨绿贯穿。'),
    Theme(
        id='midnight-navy', scenario='analysis-decision',
        label='子夜藏青(深)',
        colors={'bg': '#101D42', 'ink': '#F2F4F7', 'primary': '#D9DDE3',
                'accent': '#F5B700', 'muted': '#868E96', 'hairline': '#2A3A66'},
        fonts={'display': 'misans', 'body': 'noto-sans-sc', 'latin': 'liter'},
        mood='深藏青底+信号黄关键值;夜间投屏的数据决策页。',
        dark=True),
    # ── business-plan ──
    Theme(
        id='ink-lime-launch', scenario='business-plan',
        label='墨黑荧光(深)',
        colors={'bg': '#141414', 'ink': '#F5F5F2', 'primary': '#C6F24E',
                'accent': '#C6F24E', 'muted': '#9AA0A6', 'hairline': '#2E2E2E'},
        fonts={'display': 'alimama-shuheiti', 'body': 'misans',
               'latin': 'liter'},
        mood='发布会撞色:墨黑底+荧光黄绿,单一强调色制造记忆点。',
        dark=True),
    Theme(
        id='cream-terracotta', scenario='business-plan',
        label='奶油陶土(浅)',
        colors={'bg': '#F6EFE2', 'ink': '#2B2118', 'primary': '#1E3B33',
                'accent': '#C15F3C', 'muted': '#8A7B68', 'hairline': '#E0D5C0'},
        fonts={'display': 'alimama-shuheiti', 'body': 'misans',
               'latin': 'liter'},
        mood='米白+墨绿+陶土;消费/餐饮/加盟等生活商业题材。'),
    # ── management-report ──
    Theme(
        id='board-brass', scenario='management-report',
        label='董事会黄铜(浅)',
        colors={'bg': '#F7F3E8', 'ink': '#1A1A2E', 'primary': '#123B2F',
                'accent': '#B08D3E', 'muted': '#7A7568', 'hairline': '#DDD5C3'},
        fonts={'display': 'alimama-shuheiti', 'body': 'noto-sans-sc',
               'latin': 'liter'},
        mood='米白+墨绿+黄铜,克制稳定;经营/财务/董事会汇报。'),
    Theme(
        id='graphite-pine', scenario='management-report',
        label='石墨松绿(浅)',
        colors={'bg': '#F4F7F6', 'ink': '#2B2D42', 'primary': '#2B2D42',
                'accent': '#4C7A5A', 'muted': '#7B8494', 'hairline': '#DDE4E2'},
        fonts={'display': 'alimama-shuheiti', 'body': 'noto-sans-sc',
               'latin': 'liter'},
        mood='冷白+石墨+松绿;互联网/增长/月度经营复盘。'),
    # ── academic-research ──
    Theme(
        id='archive-paper', scenario='academic-research',
        label='档案纸(浅)',
        colors={'bg': '#F1E9DA', 'ink': '#27231F', 'primary': '#355C52',
                'accent': '#8C3B36', 'muted': '#9B9489', 'hairline': '#D8CDB8'},
        fonts={'display': 'noto-serif-sc', 'body': 'noto-serif-sc',
               'latin': 'oranienbaum'},
        mood='档案纸+装订墨绿+批注朱红;人文社科的特辑式学术版面。'),
    Theme(
        id='swiss-lab', scenario='academic-research',
        label='瑞士实验室(浅)',
        colors={'bg': '#E7E8E5', 'ink': '#14171A', 'primary': '#1D3557',
                'accent': '#A63D2F', 'muted': '#7C8287', 'hairline': '#C9CCC6'},
        fonts={'display': 'noto-serif-sc', 'body': 'noto-sans-sc',
               'latin': 'liter'},
        mood='钛灰底+严格网格+大色块;理工答辩/海报式研究结果页。'),
    # ── education-training ──
    Theme(
        id='warm-classroom', scenario='education-training',
        label='暖白课堂(浅)',
        colors={'bg': '#FAF6EE', 'ink': '#2B2622', 'primary': '#3D2C4F',
                'accent': '#F2C14E', 'muted': '#8B8175', 'hairline': '#E4DCCB'},
        fonts={'display': 'lxgw-wenkai', 'body': 'lxgw-wenkai',
               'latin': 'liter'},
        mood='暖白+墨紫+奶油黄;知识付费/课程的温润学习感。'),
    Theme(
        id='pine-study', scenario='education-training',
        label='松绿自习(浅)',
        colors={'bg': '#F7F3E8', 'ink': '#23301F', 'primary': '#2F5233',
                'accent': '#E8B54A', 'muted': '#8A8F7E', 'hairline': '#DED7C2'},
        fonts={'display': 'lxgw-wenkai', 'body': 'lxgw-wenkai',
               'latin': 'liter'},
        mood='纸白+松绿+杏黄;科普/培训的安静纸面。'),
    # ── brand-creative ──
    Theme(
        id='klein-poster', scenario='brand-creative',
        label='克莱因蓝海报(浅)',
        colors={'bg': '#F7F5EF', 'ink': '#101010', 'primary': '#0038B8',
                'accent': '#0038B8', 'muted': '#8F8E89', 'hairline': '#D8D7D2'},
        fonts={'display': 'smiley-sans', 'body': 'zcool-wenyiti',
               'latin': 'oranienbaum'},
        mood='纸白+近黑+克莱因蓝单色;瑞士海报式严格左对齐与极端字号差。'),
    Theme(
        id='editorial-ink', scenario='brand-creative',
        label='墨黑编辑部(深)',
        colors={'bg': '#0D0D0D', 'ink': '#F5F0E6', 'primary': '#F5F0E6',
                'accent': '#6D1F2C', 'muted': '#8A8578', 'hairline': '#2A2A2A'},
        fonts={'display': 'smiley-sans', 'body': 'zcool-wenyiti',
               'latin': 'oranienbaum'},
        mood='墨黑+象牙白+勃艮第红;奢侈/时尚/品牌年鉴的编辑部版面。',
        dark=True),
)

_THEMES_BY_ID = {t.id: t for t in THEMES}

#: Default theme per scenario (the first registered for that scenario).
_DEFAULT_BY_SCENARIO: dict = {}
for _t in THEMES:
    _DEFAULT_BY_SCENARIO.setdefault(_t.scenario, _t.id)


def get_theme(theme_id: str) -> Theme | None:
    return _THEMES_BY_ID.get(theme_id)


def default_theme_id(scenario: str) -> str:
    return _DEFAULT_BY_SCENARIO.get(scenario, 'paper-engineer')


def list_themes(*, scenario: str = '') -> list:
    return [t for t in THEMES if not scenario or t.scenario == scenario]


# ── Scenario classification (zero-LLM first pass) ────────

#: keyword → scenario. Scored by count; the recipe's LLM outline stage may
#: override — this is the deterministic default, never the final word.
_CLASSIFY_KEYWORDS: dict = {
    'tech-engineering': ('架构', '系统', '工程', '算法', '模型', '代码', 'api',
                         'sdk', '运维', '安全', '数据库', '分布式', '微服务',
                         'architecture', 'engineering', 'infrastructure',
                         'protocol', 'compiler', 'kernel', '部署', '性能优化'),
    'analysis-decision': ('研报', '行研', '投资', '咨询', '战略', '市场分析',
                          '竞品', '趋势', 'forecast', 'valuation', '市场规模',
                          '决策', '可行性', 'roi', '调研报告'),
    'business-plan': ('商业计划', '路演', '融资', '招商', '加盟', '营销方案',
                      '销售', '提案', 'pitch', 'roadshow', 'bp', '产品发布',
                      '品牌发布', '推广方案'),
    'management-report': ('汇报', '总结', '复盘', '周报', '月报', '季报',
                          '年报', 'okr', 'kpi', '述职', '经营分析', 'q1', 'q2',
                          'q3', 'q4', '回顾'),
    'academic-research': ('论文', '答辩', '课题', '开题', '中期', '结题',
                          '实验', '文献', 'thesis', 'paper', 'research',
                          '实验结果', '学术', '研究生', '博士', '硕士'),
    'education-training': ('教程', '课程', '培训', '教学', '课件', '科普',
                           '入门', '学习', 'training', 'course', '指南',
                           '手册', '新手指南', '知识'),
    'brand-creative': ('品牌', '创意', '设计', '作品集', '艺术', '时尚',
                       '发布会', 'portfolio', 'brand', 'creative', '国潮',
                       '文化', '展览', '活动'),
}


def classify_scenario(text: str) -> str:
    """Best-guess scenario for a topic/outline by keyword voting.

    Deterministic and deliberately simple: the LLM outline stage in each
    recipe re-decides with real understanding; this exists so a zero-LLM path
    (and tests) still lands on ONE coherent scenario.
    """
    low = (text or '').lower()
    scores = {sid: 0 for sid in SCENARIOS}
    for sid, words in _CLASSIFY_KEYWORDS.items():
        for w in words:
            if w in low:
                scores[sid] += 1
    best = max(scores.items(), key=lambda kv: kv[1])
    return best[0] if best[1] > 0 else 'tech-engineering'


# ── Bibles ────────────────────────────────────────────────

def _bible_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bibles')


def BIBLE_INDEX() -> dict:
    """scenario → bible path on disk (empty when a bible file is missing)."""
    out = {}
    for sid, meta in SCENARIOS.items():
        path = os.path.join(_bible_dir(), meta['bible'])
        out[sid] = path if os.path.isfile(path) else ''
    return out


def design_bible_text(scenario: str, *, include_general: bool = True,
                      limit: int = 0) -> str:
    """The design bible for a scenario (general rules + scenario guide).

    Returns '' when the bible files are absent — the caller then authors with
    the theme block alone, which is still one coherent visual system.
    """
    parts = []
    if include_general:
        parts.append(_read_bible_file('_general.md'))
    meta = SCENARIOS.get(scenario) or {}
    parts.append(_read_bible_file(meta.get('bible', '')))
    text = '\n\n'.join(p for p in parts if p)
    if limit and len(text) > limit:
        text = text[:limit]
    return text


def _read_bible_file(name: str) -> str:
    if not name:
        return ''
    path = os.path.join(_bible_dir(), name)
    try:
        with open(path, encoding='utf-8') as f:
            return f.read().strip()
    except OSError as e:
        logger.warning('[Themes] bible %s unreadable: %s', name, e)
        return ''


# ── Prompt blocks ─────────────────────────────────────────

def theme_prompt_block(theme: Theme, *, for_video: bool = False,
                       staged_font_ids=None) -> str:
    """The verbatim block injected into every scene/page author's prompt.

    Compact by design (~350 tokens): it must survive being prepended to
    dozens of per-scene loops without dominating their context. The FULL
    reasoning lives in the bible; this block carries only the binding
    decisions (palette hexes, font families, the universal prohibitions).

    ``staged_font_ids``: when given, a role whose face failed to stage is
    remapped to a face that DID stage — naming an unstaged family is the
    silent-substitution trap, and this block may never cause it.
    """
    c = theme.colors
    f = theme.fonts
    from lib.design_sys.fonts import get_font

    def _fam(role: str) -> str:
        fid = f.get(role, '')
        if staged_font_ids is not None and fid not in staged_font_ids:
            fid = ''
            for alt in ('display', 'body', 'latin'):
                if f.get(alt) in staged_font_ids:
                    fid = f[alt]
                    break
        if not fid:
            return ''
        face = get_font(fid)
        return face.family if face else fid

    fam = {role: _fam(role) for role in ('display', 'body', 'latin')}
    if not any(fam.values()):
        typeface_line = ('- typefaces: the scene-staged CJK face only (see '
                         'the @font-face rule); never name an unstaged font')
    else:
        typeface_line = (
            f'- typefaces: display={fam["display"]}, body={fam["body"]}, '
            f'latin/digits={fam["latin"]} (already staged via @font-face — '
            f'use these family names exactly; never name an unstaged font)')
    lines = [
        f'## BINDING THEME — "{theme.label}" ({theme.id})',
        'This artefact has ONE visual system, already decided. Do not '
        'improvise colors or fonts outside it.',
        f'- mood: {theme.mood}',
        f'- background: {c["bg"]}   text/ink: {c["ink"]}',
        f'- structural color (titles, rules, key series): {c["primary"]}',
        f'- THE one accent (use it at most once per page — a number, a bar, '
        f'a word): {c["accent"]}',
        f'- secondary text: {c["muted"]}   hairlines: {c["hairline"]}',
        typeface_line,
        '- hard prohibitions: no rounded-rectangle card walls to build '
        'hierarchy (use whitespace, thin rules, size contrast); no '
        'blue-purple gradients; no cyan-purple neon; no glassmorphism; no '
        'glow; no 2x2-matrix / three-even-columns default layouts; no '
        'second accent color.',
    ]
    if for_video:
        lines.append(
            '- motion: entrances staggered 0.08-0.15s; the accent color may '
            'be the thing that MOVES (a drawing rule, a counting number).')
    return '\n'.join(lines)
