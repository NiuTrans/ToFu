"""Executable spec for the model release watch (docs/MODEL_REGISTRATION.md,
"New-model intake workflow").

The watch reports exactly the identities a live public catalogue lists and
world-catalogue sync withholds, minus standing listings and reviewed
decisions. A standing listing was listed when its catalogue was first
observed, or was listed upstream before that observation: an old row that
rotated into the bounded feed window later. Fixtures write synthetic
OpenRouter-shaped rows into a per-test feed cache and read them back through
the real feed projection, loader and merge.
"""

from __future__ import annotations

from datetime import datetime
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
ROTATED = 'google/gemini-8-rotated-test'
RELEASED = 'google/gemini-9-released-test'
DAY = 86_400
# Fetch time of the catalogue snapshot the watch first classifies (2026-11-01).
FIRST_SEEN = 1_793_500_000.0
# OpenRouter listing times: openai/gpt-5-nano (2025-08-07) rotated into the
# seed window on 2026-10-30; tencent/hy4 (2026-11-04) is a genuine release.
GPT_5_NANO_LISTED = 1_754_524_800
HY4_LISTED = 1_793_766_031
MISSING = object()


def _row(model_id: str, **overrides) -> dict:
    """An OpenRouter catalogue row; without ``created`` its listing time is unknown."""
    return {
        'id': model_id, 'context_length': 1_048_576,
        'architecture': {'input_modalities': ['text', 'image'], 'output_modalities': ['text']},
        'supported_parameters': ['reasoning'],
        'pricing': {'prompt': '0.000002', 'completion': '0.00001'}, **overrides,
    }


@pytest.fixture
def shipped(monkeypatch, tmp_path):
    """Shipped gemini (official) and openrouter (relay) templates; the feed cache is per test."""
    import lib.provider.provider_template_feed as feed
    from lib.provider.provider_template_recipes import load_provider_templates

    monkeypatch.setenv('TOFU_TEMPLATE_FEED', '1')
    monkeypatch.setattr(feed, '_CACHE_PATH', str(tmp_path / 'provider_template_feed.json'))
    monkeypatch.setattr(feed, 'maybe_refresh_async', lambda *_args, **_kwargs: False)
    return [row for row in load_provider_templates() if row['key'] in ('gemini', 'openrouter')]


def _listing(templates: list[dict], *rows, fetched_at: float | None = None) -> list[dict]:
    """The templates once OpenRouter's live catalogue, fetched at *fetched_at*, lists *rows*.

    A row is a model id (unknown listing time) or a catalogue row. The cache
    keeps each row's raw upstream listing time; the real loader validates it.
    """
    import lib.provider.provider_template_feed as feed

    fetched_at = time.time() if fetched_at is None else fetched_at
    rows = [_row(row) if isinstance(row, str) else row for row in rows]
    source = {'url': feed.PUBLIC_CATALOGS['openrouter']['url'], 'fetched_at': fetched_at,
              'listed_at': {row['id']: row['created'] for row in rows if 'created' in row}}
    with open(feed._CACHE_PATH, 'w') as handle:
        json.dump({'version': 2, 'templates': feed.recipes_from_catalog(rows),
                   'sources': {'openrouter': source}}, handle)
    return feed.merge_feed_recipes(templates, feed.load_cached_feed(), now=fetched_at)


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

    templates = _listing(shipped, _row(UNKNOWN, created=HY4_LISTED),
                         _row('google/gemini-4-argon', created=HY4_LISTED))
    monkeypatch.setattr('lib.provider.provider_template_recipes.load_provider_templates',
                        lambda: templates)
    repository, boundary = InMemoryModelRoutingRepository(), OwnerBoundary.create(1)
    sync_world_model_catalog(boundary=boundary, repository=repository)

    document = repository.get(boundary).document
    published = {(row['creator_id'], row['model_id']) for row in document['models']}
    withheld = {(row['creator_id'], row['model_id']) for row in withheld_catalog_models(templates)}
    assert ('google', 'gemini-4-argon') in published
    assert ('google', 'gemini-9-test') in withheld
    assert not published & withheld
    # Upstream listing times are watch evidence, never owner registry data.
    assert 'listed_at' not in json.dumps(document) and str(HY4_LISTED) not in json.dumps(document)


def test_first_report_records_the_standing_listings_as_baseline(shipped, tmp_path):
    state_path = str(tmp_path / 'watch.json')

    report = release_watch_report(templates=_listing(shipped, UNKNOWN, fetched_at=FIRST_SEEN),
                                  state_path=state_path)

    assert report['status'] == 'initialized'
    assert report['new'] == [] and report['baseline'] == 1
    assert baseline_identities(state_path=state_path) == [UNKNOWN]
    # Each catalogue's first observation is the fetch time of the snapshot classified.
    assert json.loads((tmp_path / 'watch.json').read_text())['sources'] == {
        'openrouter': datetime.fromtimestamp(FIRST_SEEN).isoformat(timespec='seconds')}


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

    report = release_watch_report(templates=_listing(shipped, UNKNOWN, fetched_at=FIRST_SEEN),
                                  state_path=str(state_path))

    assert report['status'] == 'ok' and report['new'] == []
    state = json.loads(state_path.read_text())
    assert state['sources'] == {
        'cerebras': '2026-10-29T09:17:00',
        'openrouter': datetime.fromtimestamp(FIRST_SEEN).isoformat(timespec='seconds')}
    assert state['baseline'] == [UNKNOWN]
    later = release_watch_report(templates=_listing(shipped, UNKNOWN, LATER),
                                 state_path=str(state_path))
    assert _ids(later['new']) == [LATER]


def test_no_live_catalogue_is_unavailable_and_records_no_baseline(shipped, tmp_path):
    state_path = tmp_path / 'watch.json'

    report = release_watch_report(templates=shipped, state_path=str(state_path))

    assert report['status'] == 'unavailable'
    assert not state_path.exists()


def test_an_old_listing_rotating_into_the_feed_window_is_standing(shipped, tmp_path, monkeypatch):
    """OpenRouter lists more models than the newest-first seed window keeps. When
    newer rows leave it an old one enters (openai/gpt-5-nano, listed 2025-08-07,
    reported as new on 2026-10-30); it joins the baseline instead."""
    import lib.provider.provider_template_feed as feed

    monkeypatch.setattr(feed, '_MAX_FEED_ROWS_PER_TEMPLATE', 2)
    state_path = str(tmp_path / 'watch.json')
    rotated = _row(ROTATED, created=GPT_5_NANO_LISTED)
    newer = [_row(UNKNOWN, created=FIRST_SEEN - DAY), _row(LATER, created=FIRST_SEEN - 2 * DAY)]
    first = release_watch_report(templates=_listing(shipped, rotated, *newer, fetched_at=FIRST_SEEN),
                                 state_path=state_path)
    assert first['status'] == 'initialized'
    assert baseline_identities(state_path=state_path) == [LATER, UNKNOWN]

    # Both newer rows leave the window; the old row rotates in beside a release.
    released = _row(RELEASED, created=HY4_LISTED)
    report = release_watch_report(
        templates=_listing(shipped, rotated, released, fetched_at=HY4_LISTED + DAY),
        state_path=state_path)

    assert _ids(report['new']) == [RELEASED]
    assert report['new'][0]['leads'][0]['listed_at'] == HY4_LISTED
    assert ROTATED in baseline_identities(state_path=state_path)
    # It joined for good: a cache without listing times keeps it standing.
    report = release_watch_report(templates=_listing(shipped, ROTATED, RELEASED),
                                  state_path=state_path)
    assert _ids(report['new']) == [RELEASED]


@pytest.mark.parametrize('created', [MISSING, None, 0, -DAY, 'nan', True, 'soon'],
                         ids=['missing', 'null', 'zero', 'negative', 'nan', 'bool', 'text'])
def test_an_unknown_listing_time_is_never_old(shipped, tmp_path, created):
    state_path = str(tmp_path / 'watch.json')
    standing = _row(UNKNOWN, created=FIRST_SEEN - DAY)
    release_watch_report(templates=_listing(shipped, standing, fetched_at=FIRST_SEEN),
                         state_path=state_path)
    unknown = _row(LATER) if created is MISSING else _row(LATER, created=created)

    # Only the listing time tells the rotated row from the unknown one.
    report = release_watch_report(
        templates=_listing(shipped, standing, _row(ROTATED, created=GPT_5_NANO_LISTED), unknown,
                           fetched_at=FIRST_SEEN + DAY),
        state_path=state_path)

    assert _ids(report['new']) == [LATER]
    assert 'listed_at' not in report['new'][0]['leads'][0]
    assert ROTATED in baseline_identities(state_path=state_path)


def test_a_listing_after_the_first_snapshot_is_new(shipped, tmp_path):
    """Listed upstream after the snapshot the baseline came from, even though
    that was before the first report ran: no baseline could have held it."""
    state_path = str(tmp_path / 'watch.json')
    standing = _row(UNKNOWN, created=FIRST_SEEN - DAY)
    release_watch_report(templates=_listing(shipped, standing, fetched_at=FIRST_SEEN),
                         state_path=state_path, now=datetime.fromtimestamp(FIRST_SEEN + 3600))
    released = _row(RELEASED, created=FIRST_SEEN + 1800)

    report = release_watch_report(
        templates=_listing(shipped, standing, released, fetched_at=FIRST_SEEN + DAY),
        state_path=state_path)

    assert _ids(report['new']) == [RELEASED]
    assert report['new'][0]['leads'][0]['listed_at'] == FIRST_SEEN + 1800


def test_a_version_one_state_upgrades_losslessly_without_rebaselining(shipped, tmp_path):
    state_path = tmp_path / 'watch.json'
    review = {'decision': 'declined', 'reason': 'relay-only listing', 'checked_at': '2026-10-30',
              'source_urls': []}
    v1 = {'version': 1, 'initialized_at': '2026-10-29T16:15:29', 'sources': ['openrouter'],
          'baseline': [UNKNOWN], 'reviews': {LATER: review}}
    state_path.write_text(json.dumps(v1))
    assert baseline_identities(state_path=str(state_path)) == [UNKNOWN]
    templates = _listing(shipped, UNKNOWN, LATER, _row(ROTATED, created=GPT_5_NANO_LISTED),
                         _row(RELEASED, created=HY4_LISTED))

    report = release_watch_report(templates=templates, state_path=str(state_path))

    # Every catalogue a version-1 state knew was first observed at initialization.
    assert report['status'] == 'ok' and _ids(report['new']) == [RELEASED]
    assert json.loads(state_path.read_text()) == {
        **v1, 'version': 2, 'sources': {'openrouter': '2026-10-29T16:15:29'},
        'baseline': [UNKNOWN, ROTATED]}


@pytest.mark.parametrize('raw', [
    '{"version": 1, "baseline": [',
    json.dumps({'version': 1, 'sources': ['openrouter'], 'baseline': [], 'reviews': {}}),
    json.dumps({'version': 2, 'initialized_at': '2026-10-29T16:15:29', 'sources': ['openrouter'],
                'baseline': [], 'reviews': {}}),
    json.dumps({'version': 2, 'initialized_at': '2026-10-29T16:15:29',
                'sources': {'openrouter': 'yesterday'}, 'baseline': [], 'reviews': {}}),
    json.dumps({'version': 3, 'initialized_at': '2026-10-29T16:15:29',
                'sources': {'openrouter': '2026-10-29T16:15:29'}, 'baseline': [], 'reviews': {}}),
], ids=['truncated', 'v1-without-initialization-time', 'v2-source-list', 'v2-bad-first-seen',
        'future-version'])
def test_unreadable_state_fails_closed_without_rebaselining(shipped, tmp_path, raw):
    state_path = tmp_path / 'watch.json'
    state_path.write_text(raw)
    templates = _listing(shipped, UNKNOWN, _row(ROTATED, created=GPT_5_NANO_LISTED))

    with pytest.raises(ReleaseWatchError):
        release_watch_report(templates=templates, state_path=str(state_path))
    with pytest.raises(ReleaseWatchError):
        baseline_identities(state_path=str(state_path))
    with pytest.raises(ReleaseWatchError):
        record_review(UNKNOWN, 'deferred', 'no official page', templates=templates,
                      state_path=str(state_path))
    assert state_path.read_text() == raw


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
