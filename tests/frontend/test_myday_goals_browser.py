"""Real app + isolated storage: the My Day goal journey in a real browser.

Write a goal, see its decomposition, adopt and check items off, reload, open
the page again in a fresh browser context, and come back the next day (the
browser clock moves to tomorrow). The model is the only seam: a deterministic
decomposition stands in for the provider. Set TOFU_MYDAY_SCREENSHOTS to a
directory to keep the screenshots of each step.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import re

import pytest
from playwright.sync_api import expect

from tests.visual.test_visual_surfaces import _invoke_action, _wait_ready

pytestmark = pytest.mark.visual

GOAL = '把 My Day 改成目标追踪'
SUGGESTED = ['写设计文档', '实现目标存储', '做浏览器验证']


def _shot(page, name):
    directory = os.environ.get('TOFU_MYDAY_SCREENSHOTS')
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.locator('.myday-modal').screenshot(path=str(Path(directory) / f'{name}.png'))


def _open(page):
    from tests.conftest import _dismiss_onboarding_modals
    page.evaluate('localStorage.setItem("tofu_ui_lang", "zh")')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1280, 'height': 900})
    _wait_ready(page)
    _invoke_action(page, 'openDailyReport')
    page.wait_for_selector('#dailyReportModal.open .myday-goal-composer input')


def _row(page, text):
    return page.locator('.myday-goal .myday-goal-item', has_text=text)


def test_goal_journey_survives_reload_restart_and_the_next_day(page, browser, live_server, monkeypatch,
                                                                 assert_no_js_errors):
    prompts = []

    def decompose(messages, owner):
        prompts.append(messages[-1]['content'])
        return '{"items": ' + str(SUGGESTED).replace("'", '"') + '}'
    monkeypatch.setattr('lib.myday.decompose._dispatch', decompose)
    origin = live_server.rstrip('/')

    _open(page)
    expect(page.locator('.myday-empty')).to_be_visible()
    _shot(page, '01-empty')

    composer = page.locator('.myday-goal-composer input')
    composer.fill(GOAL)
    composer.press('Enter')
    card = page.locator('.myday-goal').first
    expect(card.locator('.myday-goal-title')).to_have_text(GOAL)
    expect(card.locator('.myday-goal-item.suggested')).to_have_count(3)
    expect(card.locator('.myday-goal-pct-value')).to_have_text('—')
    _shot(page, '02-decomposed')

    card.get_by_role('button', name='全部采纳').click()
    expect(card.locator('.myday-goal-item.suggested')).to_have_count(0)
    add = card.locator('.myday-goal-add input')
    add.fill('和负责人确认验收')
    add.press('Enter')
    expect(_row(page, '和负责人确认验收')).to_have_count(1)
    _row(page, '写设计文档').locator('.myday-todo-check').click()
    _row(page, '实现目标存储').get_by_role('button', name='开始').click()
    expect(card.locator('.myday-goal-pct-value')).to_have_text('≈25%')
    expect(_row(page, '写设计文档').locator('.myday-goal-badge.today')).to_have_text('今天')
    expect(page.locator('#mydayProgress')).to_have_text('1 个目标 · 今天完成 1 项 · 推进中 1 项')
    _shot(page, '03-progress')

    page.reload()  # reload: the board is read back from the server
    _wait_ready(page)
    _invoke_action(page, 'openDailyReport')
    card = page.locator('.myday-goal').first
    expect(card.locator('.myday-goal-pct-value')).to_have_text('≈25%')
    expect(_row(page, '写设计文档')).to_have_class(re.compile(r'\bdone\b'))
    expect(_row(page, '实现目标存储').locator('.myday-goal-chip.on')).to_have_count(1)
    _shot(page, '04-after-reload')

    tomorrow = (dt.datetime.now() + dt.timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    context = browser.new_context()  # a restarted browser, opened the next day
    try:
        next_day = context.new_page()
        next_day.clock.install(time=tomorrow)
        next_day.goto(origin, wait_until='domcontentloaded')
        _open(next_day)
        card = next_day.locator('.myday-goal').first
        expect(card.locator('.myday-goal-pct-value')).to_have_text('≈25%')
        expect(card.locator('.myday-goal-item')).to_have_count(4)
        done = _row(next_day, '写设计文档')
        expect(done.locator('.myday-goal-badge.today')).to_have_count(0)
        expect(done.locator('.myday-goal-badge.when')).to_have_count(1)
        expect(_row(next_day, '做浏览器验证').locator('.myday-goal-badge.since')).to_have_count(1)
        expect(next_day.locator('#mydayProgress')).to_have_text('1 个目标 · 今天完成 0 项 · 推进中 1 项')
        _shot(next_day, '05-next-day')

        _row(next_day, '实现目标存储').locator('.myday-todo-check').click()
        expect(card.locator('.myday-goal-pct-value')).to_have_text('≈50%')
        card.get_by_role('button', name='重新拆解').click()
        expect(card.locator('.myday-goal-working')).to_have_count(0)
        expect(card.locator('.myday-goal-item')).to_have_count(4)
        _shot(next_day, '06-next-day-regenerated')

        # A legacy generated report of that day, written through its storage owner.
        from lib.daily_report.storage import _save_report
        from lib.identity import PERSONAL_USER_ID
        yesterday = dt.date.today()
        _save_report(yesterday.isoformat(), {
            'streams': [{'id': 'stream-legacy', 'title': '旧日报里的工作流', 'summary': '只读归档',
                         'status': 'done', 'conv_ids': []}],
            'tomorrow': [{'id': 'todo-legacy', 'text': '旧日报里的次日计划', 'done': False}],
        }, owner_user_id=PERSONAL_USER_ID)
        if yesterday.month != tomorrow.month:
            next_day.locator('#mydayCalHeader .mcal-nav').first.click()
        next_day.locator(f'#mydayCalGrid .mcal-d[data-tofu-action="_mydaySelectDay({yesterday.day})"]').click()
        expect(next_day.locator('#mydayTasks')).to_contain_text('这天的目标进展')
        expect(next_day.locator('#mydayTasks .myday-goal-compact')).to_contain_text('写设计文档')
        archive = next_day.locator('#mydayTasks .myday-goal-archive')
        expect(archive).to_contain_text('旧日报里的工作流')
        expect(archive).to_contain_text('当天写下的次日计划')
        expect(archive).to_contain_text('旧日报里的次日计划')
        expect(archive.locator('button')).to_have_count(0)
        _shot(next_day, '07-yesterday-recap')
    finally:
        context.close()

    goals = page.request.get(origin + '/api/v4/myday/goals').json()['data']['goals']
    [stored] = [goal for goal in goals if goal['text'] == GOAL]
    states = {item['text']: (item['origin'], item.get('proposal'), item['status'], item['manual'])
              for item in stored['items']}
    assert states == {
        '写设计文档': ('agent', 'accepted', 'completed', True),
        '实现目标存储': ('agent', 'accepted', 'completed', True),
        '做浏览器验证': ('agent', 'accepted', 'pending', False),
        '和负责人确认验收': ('owner', None, 'pending', False),
    }
    assert stored['progress']['percent'] == 50 and len(prompts) == 2
    assert '写设计文档 (done)' in prompts[1], 'the regeneration saw the adopted items'


def test_linked_goal_shows_worker_evidence_and_the_owner_override_wins(page, monkeypatch, tmp_path,
                                                                        assert_no_js_errors):
    """The dispatch read is the seam: its rows come from the request cohort shape
    (tests/myday/test_myday_dispatch_evidence.py maps real delegation rows)."""
    from tests.myday.test_myday_evidence import row
    import lib.myday.evidence as evidence

    project = str(tmp_path / 'tofu')
    rows = [row('a1', review='accept', brief='[DB] Measure the load\nDetails'),
            row('a2', brief='[GOALS] Redesign My Day\nDetails'),
            row('a3', execution=False, brief='[DOCS] Update the runbook\nDetails')]
    busy = {'worker-a2'}
    monkeypatch.setattr(evidence, '_bound_projects', lambda owner: [(project, 'coordinator-1')])
    monkeypatch.setattr(evidence, '_newest_rows', lambda owner, path: rows)
    monkeypatch.setattr(evidence, '_request_text', lambda owner, coordinator, turn: '看一下 tofu 的稳定性')
    monkeypatch.setattr(evidence, '_request_cohort',
                        lambda owner, path, source: {'rows': rows, 'settled': False})
    monkeypatch.setattr(evidence, '_live_conversations', lambda owner: frozenset(busy))
    monkeypatch.setattr('lib.myday.decompose._dispatch', lambda messages, owner: '{"items": []}')

    _open(page)
    composer = page.locator('.myday-goal-composer input')
    composer.fill('让 tofu 稳定下来')
    composer.press('Enter')
    card = page.locator('.myday-goal', has_text='让 tofu 稳定下来')
    expect(card.locator('.myday-goal-btn', has_text='关联项目请求')).to_be_visible()
    card.get_by_role('button', name='关联项目请求').click()
    option = card.locator('.myday-goal-option', has_text='看一下 tofu 的稳定性')
    expect(option).to_contain_text('3 个 Worker')
    _shot(page, '08-link-picker')
    option.click()

    expect(card.locator('.myday-goal-request')).to_contain_text('关联请求：看一下 tofu 的稳定性')
    workers = card.locator('.myday-goal-item', has=page.locator('.myday-goal-badge.worker'))
    expect(workers).to_have_count(3)
    expect(card.locator('.myday-goal-pct-value')).to_have_text('≈33%')
    expect(_row(page, '[GOALS] Redesign My Day').locator('.myday-goal-badge.worker')).to_have_text('运行中')
    queued = _row(page, '[DOCS] Update the runbook')
    expect(queued.locator('.myday-goal-badge.worker')).to_have_text('排队中')

    queued.locator('.myday-todo-check').click()  # the owner already did it by hand
    expect(card.locator('.myday-goal-pct-value')).to_have_text('≈66%')
    expect(queued.locator('.myday-goal-badge.manual')).to_have_text('手动')
    expect(queued.locator('.myday-goal-badge.worker')).to_have_text('排队中')
    _shot(page, '09-linked-override')

    rows[2] = row('a3', brief='[DOCS] Update the runbook\nDetails')  # the Worker starts later
    page.locator('#mydayRefreshBtn').click()
    expect(queued.locator('.myday-goal-badge.worker')).to_have_text('待验收')
    expect(card.locator('.myday-goal-pct-value')).to_have_text('≈66%')
    queued.get_by_role('button', name='跟随 Worker 状态').click()
    expect(queued.locator('.myday-goal-badge.manual')).to_have_count(0)
    expect(card.locator('.myday-goal-pct-value')).to_have_text('≈33%')
    _shot(page, '10-follow-worker')
