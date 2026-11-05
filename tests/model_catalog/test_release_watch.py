"""Executable spec for the model release watch (docs/MODEL_REGISTRATION.md,
"New-model intake workflow").

The watch reports exactly the identities a live public catalogue lists and
world-catalogue sync withholds, minus the standing listings recorded when the
catalogue was first seen and minus reviewed decisions. Fixtures feed
synthetic OpenRouter-shaped rows through the real feed projection and merge.
"""

from __future__ import annotations

import json
import time

import pytest

from lib.model_catalog.release_watch import (
    MAX_REASON_CHARS, MAX_SOURCE_URLS, ReleaseWatchError, baseline_identities, record_review,
    release_candidates, release_watch_report,
)


pytestmark = pytest.mark.unit

UNKNOWN = 'google/gemini-9-test'
LATER = 'google/gemini-9-later-test'


def _row(model_id: str) -> dict:
    return {
        'id': model_id, 'created': 1, 'context_length': 1_048_576,
        'architecture': {'input_modalities': ['text', 'image'], 'output_modalities': ['text']},
        'supported_parameters': ['reasoning'],
        'pricing': {'prompt': '0.000002', 'completion': '0.00001'},
    }


@pytest.fixture
def shipped(monkeypatch):
    """Shipped gemini (official) and openrouter (relay) templates, no cached feed."""
    import lib.provider.provider_template_feed as feed
    from lib.provider.provider_template_recipes import load_provider_templates

    monkeypatch.setenv('TOFU_TEMPLATE_FEED', '1')
    monkeypatch.setattr(feed, 'load_cached_feed', lambda: None)
    monkeypatch.setattr(feed, 'maybe_refresh_async', lambda *_args, **_kwargs: False)
    return [row for row in load_provider_templates() if row['key'] in ('gemini', 'openrouter')]


def _listing(templates: list[dict], *model_ids: str) -> list[dict]:
    """The templates as they look once OpenRouter's live catalogue lists *model_ids*."""
    from lib.provider.provider_template_feed import (
        PUBLIC_CATALOGS, merge_feed_recipes, recipes_from_catalog,
    )
    feed = {
        'version': 2,
        'templates': recipes_from_catalog([_row(model_id) for model_id in model_ids]),
        'sources': {'openrouter': {'url': PUBLIC_CATALOGS['openrouter']['url'],
                                   'fetched_at': time.time()}},
    }
    return merge_feed_recipes(templates, feed)


def _ids(rows: list[dict]) -> list[str]:
    return [row['id'] for row in rows]


def test_only_withheld_feed_listings_are_candidates(shipped):
    from lib.model_routing.world_catalog import withheld_catalog_models

    authored = next(row for row in withheld_catalog_models(shipped)
                    if {supply['template'] for supply in row['supply']} == {'gemini'})
    templates = _listing(shipped, UNKNOWN, 'google/gemini-4-argon',
                         f"google/{authored['model_id']}")

    feeds, candidates = release_candidates(templates)

    assert set(feeds) == {'openrouter'}
    # Argon's maintained knowledge makes it publishable, so it is no release
    # candidate; the gemini template authors the third identity itself.
    assert _ids(candidates) == [UNKNOWN]
    lead = candidates[0]['leads'][0]
    assert lead['template'] == 'openrouter' and lead['wire_model_ids'] == [UNKNOWN]
    assert lead['context_window'] == 1_048_576
    assert lead['actual_pricing']['input'] == 2 and lead['actual_pricing']['output'] == 10
    assert any(field.startswith('registration_metadata.release_date')
               for field in candidates[0]['missing'])


def test_withheld_list_is_exactly_what_world_sync_refuses_to_publish(shipped, monkeypatch):
    from lib.model_routing import OwnerBoundary
    from lib.model_routing.repository import InMemoryModelRoutingRepository
    from lib.model_routing.world_catalog import sync_world_model_catalog, withheld_catalog_models

    templates = _listing(shipped, UNKNOWN, 'google/gemini-4-argon')
    monkeypatch.setattr('lib.provider.provider_template_recipes.load_provider_templates',
                        lambda: templates)
    repository, boundary = InMemoryModelRoutingRepository(), OwnerBoundary.create(1)
    sync_world_model_catalog(boundary=boundary, repository=repository)

    published = {(row['creator_id'], row['model_id'])
                 for row in repository.get(boundary).document['models']}
    withheld = {(row['creator_id'], row['model_id']) for row in withheld_catalog_models(templates)}
    assert ('google', 'gemini-4-argon') in published
    assert ('google', 'gemini-9-test') in withheld
    assert not published & withheld


def test_first_report_records_the_standing_listings_as_baseline(shipped, tmp_path):
    state_path = str(tmp_path / 'watch.json')

    report = release_watch_report(templates=_listing(shipped, UNKNOWN), state_path=state_path)

    assert report['status'] == 'initialized'
    assert report['new'] == [] and report['baseline'] == 1
    assert baseline_identities(state_path=state_path) == [UNKNOWN]
    assert json.loads((tmp_path / 'watch.json').read_text())['sources'] == ['openrouter']


def test_a_later_listing_is_new_until_reviewed(shipped, tmp_path):
    state_path = str(tmp_path / 'watch.json')
    release_watch_report(templates=_listing(shipped, UNKNOWN), state_path=state_path)
    templates = _listing(shipped, UNKNOWN, LATER)

    assert _ids(release_watch_report(templates=templates, state_path=state_path)['new']) == [LATER]

    review = record_review(LATER, 'deferred', 'no public API model page yet',
                           ['https://ai.google.dev/gemini-api/docs/models'],
                           templates=templates, state_path=state_path)
    report = release_watch_report(templates=templates, state_path=state_path)
    assert report['new'] == []
    assert _ids(report['deferred']) == [LATER]
    assert report['deferred'][0]['review'] == review

    record_review(LATER, 'declined', 'relay-only listing', templates=templates,
                  state_path=state_path)
    report = release_watch_report(templates=templates, state_path=state_path)
    assert report['new'] == [] and report['deferred'] == []


def test_reviews_never_pre_suppress_an_unlisted_model(shipped, tmp_path):
    state_path = str(tmp_path / 'watch.json')
    templates = _listing(shipped, UNKNOWN)
    release_watch_report(templates=templates, state_path=state_path)

    with pytest.raises(ReleaseWatchError, match='not a current release candidate'):
        record_review(LATER, 'deferred', 'announced, not listed', templates=templates,
                      state_path=state_path)
    for decision, reason, sources in (('registered', 'done', []), ('deferred', ' ', []),
                                      ('deferred', 'gap', ['http://example.com/x'])):
        with pytest.raises(ReleaseWatchError):
            record_review(UNKNOWN, decision, reason, sources, templates=templates,
                          state_path=state_path)

    # A model listed for the first time after that is still new.
    report = release_watch_report(templates=_listing(shipped, UNKNOWN, LATER),
                                  state_path=state_path)
    assert _ids(report['new']) == [LATER]


def test_a_catalogue_first_seen_later_joins_the_baseline(shipped, tmp_path):
    state_path = tmp_path / 'watch.json'
    state_path.write_text(json.dumps({
        'version': 1, 'initialized_at': '2026-10-29T09:17:00', 'sources': ['cerebras'],
        'baseline': [], 'reviews': {}}))

    report = release_watch_report(templates=_listing(shipped, UNKNOWN), state_path=str(state_path))

    assert report['status'] == 'ok' and report['new'] == []
    state = json.loads(state_path.read_text())
    assert state['sources'] == ['cerebras', 'openrouter'] and state['baseline'] == [UNKNOWN]
    later = release_watch_report(templates=_listing(shipped, UNKNOWN, LATER),
                                 state_path=str(state_path))
    assert _ids(later['new']) == [LATER]


def test_no_live_catalogue_is_unavailable_and_records_no_baseline(shipped, tmp_path):
    state_path = tmp_path / 'watch.json'

    report = release_watch_report(templates=shipped, state_path=str(state_path))

    assert report['status'] == 'unavailable'
    assert not state_path.exists()


def test_unreadable_state_fails_closed_without_rebaselining(shipped, tmp_path):
    state_path = tmp_path / 'watch.json'
    state_path.write_text('{"version": 1, "baseline": [')

    with pytest.raises(ReleaseWatchError):
        release_watch_report(templates=_listing(shipped, UNKNOWN), state_path=str(state_path))
    assert state_path.read_text() == '{"version": 1, "baseline": ['


@pytest.mark.parametrize('report,code', [
    ({'status': 'unavailable', 'feeds': {}, 'new': [], 'deferred': [], 'baseline': 0}, 2),
    ({'status': 'initialized', 'feeds': {}, 'new': [], 'deferred': [], 'baseline': 7}, 1),
    ({'status': 'ok', 'feeds': {}, 'new': [], 'deferred': [], 'baseline': 7}, 1),
    ({'status': 'ok', 'feeds': {}, 'baseline': 7, 'deferred': [], 'new': [
        {'id': UNKNOWN, 'missing': ['list_pricing.input/output/currency/unit'],
         'leads': [{'template': 'openrouter', 'wire_model_ids': [UNKNOWN]}]}]}, 0),
])
def test_cli_exit_codes_follow_the_predicate_contract(monkeypatch, capsys, report, code):
    from scripts import model_release_watch as cli

    monkeypatch.setattr(cli, 'release_watch_report', lambda: report)

    assert cli.main([]) == code
    if report['new']:
        assert f'NEW {UNKNOWN}' in capsys.readouterr().out


def test_cli_reports_unreadable_state_as_ambiguous(monkeypatch, capsys):
    from scripts import model_release_watch as cli

    def unreadable():
        raise ReleaseWatchError('release watch state is unreadable')

    monkeypatch.setattr(cli, 'release_watch_report', unreadable)

    assert cli.main([]) == 2
    assert 'unreadable' in capsys.readouterr().err


def test_cli_review_and_baseline_exit_zero_once_done(shipped, tmp_path, monkeypatch, capsys):
    from functools import partial

    from scripts import model_release_watch as cli

    state_path = str(tmp_path / 'watch.json')
    release_watch_report(templates=_listing(shipped, UNKNOWN), state_path=state_path)
    templates = _listing(shipped, UNKNOWN, LATER)
    monkeypatch.setattr(cli, 'record_review',
                        partial(record_review, templates=templates, state_path=state_path))
    monkeypatch.setattr(cli, 'baseline_identities',
                        partial(baseline_identities, state_path=state_path))

    assert cli.main(['--review', LATER, '--decision', 'deferred', '--reason', 'no API date',
                     '--source', 'https://ai.google.dev/gemini-api/docs/models']) == 0
    assert json.loads(capsys.readouterr().out)[LATER]['decision'] == 'deferred'
    report = release_watch_report(templates=templates, state_path=state_path)
    assert report['new'] == [] and _ids(report['deferred']) == [LATER]

    assert cli.main(['--baseline']) == 0
    assert capsys.readouterr().out.split() == [UNKNOWN]

    assert cli.main(['--review', LATER, '--decision', 'declined', '--reason', 'relay-only',
                     '--source', 'http://example.com/x']) == 2
    assert 'HTTPS' in capsys.readouterr().err
    report = release_watch_report(templates=templates, state_path=state_path)
    assert report['deferred'][0]['review']['decision'] == 'deferred'


def test_cli_help_states_the_review_limits(capsys):
    from scripts import model_release_watch as cli

    with pytest.raises(SystemExit):
        cli.main(['--help'])

    usage = ' '.join(capsys.readouterr().out.split())
    assert f'at most {MAX_REASON_CHARS} characters' in usage
    assert f'repeatable, at most {MAX_SOURCE_URLS}' in usage
