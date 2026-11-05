"""Chart measurements need evidence regardless of magnitude or page count;
research-card ids stay internal to the page author."""

import re
from types import SimpleNamespace

import pytest
import yaml

from lib.slides.author import _author_prompt, _brief_fidelity_findings

pytestmark = pytest.mark.unit

_DECK = SimpleNamespace(title='Two years of LLM research', width=1280,
                        height=720)
_CARD_ID_RE = re.compile(r'\[S\d+\]')
_INTERNAL_ID_RULE = {'zh': '内部来源编号', 'en': 'internal source ids'}
_NO_SOURCE_FOOTER = {'zh': '不要添加来源页脚',
                     'en': 'Do not add a source footer'}
_NO_PLACEHOLDER = {'zh': '待补充', 'en': 'TBD'}


def _chart(values):
    return yaml.safe_dump({'elements': [{
        'elementType': 'chart', 'data': {
            'categories': [str(index) for index in range(len(values))],
            'series': [{'name': 'Measured', 'values': values}],
        },
    }]})


@pytest.mark.parametrize('value', [0, 1, 3, 12, 20, 85])
def test_page_numbers_and_small_values_do_not_authorize_measurements(value):
    findings = _brief_fidelity_findings(
        {'content_notes': 'No measurements supplied; source [S1].'},
        _chart([value]), page_index=2, total=12)
    assert findings, f'unsupported measurement {value} was accepted'


@pytest.mark.parametrize(('evidence', 'values'), [
    ('Loss changed by -2.5 and +3.2.', [-2.5, 3.2]),
    ('Sample size: 1,200. Learning rate: 1e-3.', [1200, 0.001]),
    ('误差为0.5，基线为0，实验结果为12。', [0.5, 0, 12]),
])
def test_signed_grouped_and_scientific_measurements_keep_their_values(evidence, values):
    assert _brief_fidelity_findings(
        {'sources': [{'point': evidence}]}, _chart(values),
        page_index=0, total=3) == []


def test_negative_measurement_does_not_authorize_opposite_result():
    assert _brief_fidelity_findings(
        {'content_notes': 'Effect was -2.5%.'}, _chart([2.5]),
        page_index=0, total=3)


@pytest.mark.parametrize('lang', ['zh', 'en'])
@pytest.mark.parametrize('sources', [
    [], [{'id': 'S11', 'title': 'Supplied material',
          'point': 'Training cost 1.0x'}],
], ids=['no-sources', 'url-less-card'])
def test_author_sees_no_card_ids_or_footer_demand_for_supplied_material(
        lang, sources):
    brief = {'pageType': 'content',
             'key_message': 'CARD keeps the KV cache [S11]',
             'content_notes': ('Training cost 1.0x / 1.5x / 3.0x [S11]; '
                               'parallel decoding [S11].'),
             'sources': sources}

    _theme, prompt = _author_prompt(_DECK, brief, 4, 20, lang=lang)

    assert _CARD_ID_RE.search(prompt) is None
    assert 'Training cost 1.0x / 1.5x / 3.0x' in prompt
    assert _INTERNAL_ID_RULE[lang] in prompt
    assert _NO_SOURCE_FOOTER[lang] in prompt
    assert _NO_PLACEHOLDER[lang] in prompt


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_author_cites_url_source_domains_without_card_ids(lang):
    brief = {'pageType': 'content', 'key_message': 'Presale opened at 25.99',
             'content_notes': 'Presale price 25.99 [S3]; delivery in Q4 [S11].',
             'sources': [{'id': 'S3', 'title': 'Official presale',
                          'point': 'Presale price 25.99',
                          'url': 'https://www.motors.example/n70/presale',
                          'host': 'motors.example'}]}

    _theme, prompt = _author_prompt(_DECK, brief, 4, 20, lang=lang)

    assert _CARD_ID_RE.search(prompt) is None
    assert 'motors.example' in prompt
    assert ('页脚标注来源域名' if lang == 'zh'
            else 'cite the source domains') in prompt
    assert _NO_SOURCE_FOOTER[lang] not in prompt
