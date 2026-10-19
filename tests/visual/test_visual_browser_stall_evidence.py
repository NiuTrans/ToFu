"""Recovered browser stalls and search links through the shipped app/relay.

Synthetic DOM only; the local isolated app owns the actual observers and action
registry. No provider calls, production state or external navigation.
"""
import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


def test_recovered_click_stall_and_search_disclosures(browser, live_server):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals

    context = browser.new_context(service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done', '1');")
    page = context.new_page()
    evidence, errors, relays = [], [], []

    def console(message):
        if message.text.startswith('[browser-responsiveness] '):
            evidence.append(json.loads(message.text.split('] ', 1)[1]))
        if message.type == 'error' and '[actions]' in message.text:
            errors.append(message.text)

    page.on('console', console)
    page.on('response', lambda response: relays.append(response.status)
            if '/api/v4/logs/client' in response.url else None)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        expect(page.locator('#chatContainer')).to_be_visible(timeout=20000)
        page.wait_for_function("window.Api && typeof window.Api.logs?.clientRelay === 'function'")
        _dismiss_onboarding_modals(page)
        # Render with the source owner; clicks go through the shipped registry.
        fixture = subprocess.check_output(['node', '-e', """
          const esbuild=require('esbuild');
          process.stdout.write(esbuild.buildSync({stdin:{resolveDir:process.cwd(),contents:`
            import {createToolSearchPresentation} from './frontend/src/conversation/presentation/tool-search-presentation';
            const owner=createToolSearchPresentation({translate:key=>key,iconHtml:()=>''});
            const root=document.createElement('section');root.id='diagnostic-search-fixture';
            root.innerHTML=owner.renderSearchHtml({toolName:'web_search',roundNum:1,
              engineBreakdown:{google:[{url:'https://example.invalid',title:'Synthetic result'}]}},
              [{title:'Synthetic result',url:'https://example.invalid',snippet:'Fixture'}],
              {queryHtml:'Synthetic query',iconHtml:'',rightControlsHtml:
                '<a class="ri-tool-anchor" href="#fixture">Inspect</a>'});
            root.addEventListener('click',event=>{if(event.target.closest('a'))event.preventDefault();});
            document.body.append(root);
          `},bundle:true,write:false,format:'iife'}).outputFiles[0].text);
        """], cwd=ROOT, text=True)
        page.add_script_tag(content=fixture)
        header = page.locator('#diagnostic-search-fixture .ptool-results-header')
        header.click()
        expect(header).to_have_attribute('aria-expanded', 'true')
        engines = page.locator('#diagnostic-search-fixture .eb-toggle')
        engines.click()
        expect(engines).to_have_attribute('aria-expanded', 'true')
        engines.click()
        expect(engines).to_have_attribute('aria-expanded', 'false')
        page.locator('#diagnostic-search-fixture .ri-tool-anchor').click()
        expect(header).to_have_attribute('aria-expanded', 'true')
        header.click()
        expect(header).to_have_attribute('aria-expanded', 'false')
        assert not errors, errors

        page.evaluate("""() => {
          const link=document.createElement('a');link.id='diagnostic-stall-link';
          link.href='#fixture';link.textContent='Synthetic link';
          link.addEventListener('click',event=>{
            event.preventDefault();
            requestAnimationFrame(function recoveredDiagnosticStall(){
              const start=performance.now();while(performance.now()-start<1100){}
            });
          });document.body.append(link);
        }""")
        page.locator('#diagnostic-stall-link').click()
        page.wait_for_timeout(1600)
        recovered = [entry for entry in evidence if entry['durationMs'] >= 1000
                     and entry.get('interaction') == 'link']
        assert recovered, evidence
        assert all(entry['observedAtMs'] > 0 for entry in recovered)
        assert any(entry['kind'] == 'long-animation-frame' for entry in recovered), recovered
        assert all(len('[browser-responsiveness] ' + json.dumps(entry, separators=(',', ':'))) <= 800
                   for entry in recovered)
        assert 200 in relays, relays
        assert not errors, errors
    finally:
        context.close()
