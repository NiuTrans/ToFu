"""Project simulation holdings and completed steps for model/UI consumers.

Entries: project_holdings and project_step_summary. Inputs are current positions,
executed trades, model text and display names; the engine owns chronology,
accounting and persistent publication.
"""
from __future__ import annotations

import re


def project_holdings(positions: dict[str, dict], symbol_names: dict[str, str]
                     ) -> tuple[list[str], list[dict]]:
    """Render the same surviving positions and known marks for model and UI."""
    prompt_lines, holdings = [], []
    for symbol, position in positions.items():
        price = position.get('current_price', position['buy_price'])
        cost = position['buy_price']
        value = price * position['shares']
        pnl_pct = (price - cost) / cost * 100 if cost > 0 else 0
        prompt_lines.append(
            f"- {symbol} {position.get('name', '')}: {position['shares']:.2f}份, "
            f"成本¥{cost:.4f}, 现价¥{price:.4f}, "
            f"市值¥{value:,.2f}, 盈亏{pnl_pct:+.2f}%"
        )
        holdings.append({
            'symbol': symbol,
            'symbol_name': symbol_names.get(symbol, symbol),
            'pnl_pct': round(pnl_pct, 2),
        })
    return prompt_lines, holdings


def project_step_summary(content: str, trades: list[dict], sim_date: str,
                         symbol_names: dict[str, str]) -> dict:
    # Timeline actions describe executed trades, including automatic exits.
    step_actions = []
    for t in trades:
        if t['date'] == sim_date:
            step_actions.append({
                'action': t['action'],
                'symbol': t['symbol'],
                'symbol_name': symbol_names.get(t['symbol'], t['symbol']),
                'amount': t.get('amount', 0),
                'confidence': 0,
            })

    # Extract a short summary from reasoning (first useful line)
    summary = ''
    if content:
        for line in content.split('\n'):
            line = line.strip()
            if len(line) > 20 and not line.startswith('#') and not line.startswith('<'):
                summary = line[:200]
                break

    # Extract reasoning text for frontend (content without XML blocks)
    reasoning_text = ''
    if content:
        reasoning_text = re.sub(
            r'<decisions>.*?</decisions>', '', content, flags=re.DOTALL
        ).strip()
        reasoning_text = re.sub(
            r'<strategies_used>.*?</strategies_used>', '', reasoning_text, flags=re.DOTALL
        ).strip()
        # Keep full reasoning — no aggressive truncation
        reasoning_text = reasoning_text[:10000]
    return {'actions': step_actions, 'summary': summary, 'reasoning': reasoning_text}
